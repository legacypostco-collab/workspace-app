from decimal import Decimal
from functools import wraps
import csv
import io
import json
import logging
import os
from datetime import timedelta
from uuid import uuid4
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.conf import settings
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, F, Q
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.text import slugify
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .forms import CheckoutForm, LoginForm, RegisterForm, RFQCreateForm, SellerBulkUploadForm, SellerPartForm
from .models import (
    Brand,
    Category,
    Order,
    OrderClaim,
    OrderDocument,
    OrderEvent,
    OrderItem,
    Part,
    RFQ,
    RFQItem,
    SupplierRatingEvent,
    UserProfile,
    WebhookDeliveryLog,
)
from .rules import AutoModeInputs, decide_auto_mode
from .services.imports import UploadLimitError, process_seller_csv_upload
from .services.logistics import logistics_estimate
from .services.observability import Timer, log_api_error, metric_inc

CART_SESSION_KEY = "cart"
COMPARE_SESSION_KEY = "compare_parts"
ORDER_TRANSITIONS = {
    "pending": {"reserve_paid", "cancelled"},
    "reserve_paid": {"confirmed", "cancelled"},
    "confirmed": {"in_production", "cancelled"},
    "in_production": {"ready_to_ship", "cancelled"},
    "ready_to_ship": {"shipped", "cancelled"},
    "shipped": {"delivered", "cancelled"},
    "delivered": {"completed"},
    "completed": set(),
    "cancelled": set(),
}

logger = logging.getLogger("marketplace")


def _get_cart(request: HttpRequest) -> dict[str, int]:
    return request.session.get(CART_SESSION_KEY, {})


def _set_cart(request: HttpRequest, cart: dict[str, int]) -> None:
    request.session[CART_SESSION_KEY] = cart
    request.session.modified = True


def _get_compare_ids(request: HttpRequest) -> list[int]:
    raw = request.session.get(COMPARE_SESSION_KEY, [])
    out: list[int] = []
    for val in raw:
        try:
            out.append(int(val))
        except Exception:
            continue
    return out


def _set_compare_ids(request: HttpRequest, ids: list[int]) -> None:
    request.session[COMPARE_SESSION_KEY] = [int(x) for x in ids]
    request.session.modified = True


def _log_order_event(order: Order, event_type: str, source: str = "system", actor: User | None = None, meta: dict | None = None):
    event = OrderEvent.objects.create(
        order=order,
        event_type=event_type,
        source=source,
        actor=actor,
        meta=meta or {},
    )
    _emit_webhooks_for_order_event(event)


def _recalc_order_sla(order: Order):
    previous = order.sla_status
    now = timezone.now()
    status = "on_track"

    if order.status == "pending" and order.supplier_confirm_deadline:
        if now > order.supplier_confirm_deadline:
            status = "breached"
        elif (order.supplier_confirm_deadline - now) <= timedelta(hours=4):
            status = "at_risk"
    elif order.status == "confirmed" and order.ship_deadline:
        if now > order.ship_deadline:
            status = "breached"
        elif (order.ship_deadline - now) <= timedelta(hours=12):
            status = "at_risk"

    if status != previous:
        order.sla_status = status
        if status == "breached":
            order.sla_breaches_count += 1
        order.save(update_fields=["sla_status", "sla_breaches_count"])
        _log_order_event(order, "sla_status_changed", source="system", meta={"from": previous, "to": status})


def _create_order_from_rows(
    *,
    rows,
    total: Decimal,
    customer_name: str,
    customer_email: str,
    customer_phone: str,
    delivery_address: str,
    buyer: User | None,
    source: str,
    source_id: int | None = None,
    logistics_override_cost: Decimal | None = None,
):
    reserve_percent = Decimal("10.00")
    total_weight = Decimal("0.00")
    total_volume = Decimal("0.00")
    for row in rows:
        part = row["part"]
        qty = Decimal(row["quantity"])
        total_weight += (Decimal(part.gross_weight_kg or 0) * qty)
        cm3 = Decimal(part.length_cm or 0) * Decimal(part.width_cm or 0) * Decimal(part.height_cm or 0)
        total_volume += ((cm3 / Decimal("1000000")) * qty)

    if logistics_override_cost is not None:
        logistics_result = {
            "ok": True,
            "provider": "manual_override",
            "currency": "USD",
            "cost": str(logistics_override_cost.quantize(Decimal("0.01"))),
        }
    else:
        logistics_payload = {
            "origin": settings.LOGISTICS_DEFAULT_ORIGIN,
            "destination": delivery_address or settings.LOGISTICS_DEFAULT_DESTINATION,
            "mode": settings.LOGISTICS_DEFAULT_MODE,
            "incoterm": settings.LOGISTICS_DEFAULT_INCOTERM,
            "weight_kg": str(total_weight.quantize(Decimal("0.01"))),
            "volume_m3": str(total_volume.quantize(Decimal("0000000.01"))),
            "currency": "USD",
        }
        logistics_result = logistics_estimate(logistics_payload)
        if settings.LOGISTICS_STRICT_MODE and not logistics_result.get("ok", False):
            raise ValueError(logistics_result.get("error", "Logistics calculation failed"))

    logistics_cost = Decimal("0.00")
    logistics_currency = "USD"
    logistics_provider = "internal_fallback"
    if logistics_result.get("ok"):
        try:
            logistics_cost = Decimal(str(logistics_result.get("cost", "0"))).quantize(Decimal("0.01"))
            if logistics_cost < 0:
                logistics_cost = Decimal("0.00")
        except Exception:
            logistics_cost = Decimal("0.00")
        logistics_currency = str(logistics_result.get("currency") or "USD")
        logistics_provider = str(logistics_result.get("provider") or "internal_fallback")

    grand_total = (total + logistics_cost).quantize(Decimal("0.01"))
    reserve_amount = ((grand_total * reserve_percent) / Decimal("100")).quantize(Decimal("0.01"))

    with transaction.atomic():
        order = Order.objects.create(
            customer_name=customer_name,
            customer_email=customer_email,
            customer_phone=customer_phone,
            delivery_address=delivery_address,
            buyer=buyer,
            total_amount=grand_total,
            supplier_confirm_deadline=timezone.now() + timedelta(hours=24),
            sla_status="on_track",
            logistics_cost=logistics_cost,
            logistics_currency=logistics_currency,
            logistics_provider=logistics_provider,
            logistics_meta=logistics_result,
            reserve_percent=reserve_percent,
            reserve_amount=reserve_amount,
            payment_status="awaiting_reserve",
            )
        order.invoice_number = f"INV-{timezone.now():%Y%m%d}-{order.id}"
        order.save(update_fields=["invoice_number"])
        order_items = []
        for row in rows:
            part = row["part"]
            qty = row["quantity"]
            order_items.append(
                OrderItem(
                    order=order,
                    part=part,
                    quantity=qty,
                    unit_price=part.price,
                )
            )
            Part.objects.filter(id=part.id).update(stock_quantity=F("stock_quantity") - qty)
        OrderItem.objects.bulk_create(order_items)
        _log_order_event(
            order,
            "order_created",
            source=source,
            actor=buyer if buyer and buyer.is_authenticated else None,
            meta={
                "items_count": len(order_items),
                "base_total": str(total),
                "logistics_cost": str(logistics_cost),
                "reserve_amount": str(reserve_amount),
                "total_amount": str(grand_total),
                "source_id": source_id,
            },
        )
        return order


def _role_for(user: User | None) -> str | None:
    if not user or not user.is_authenticated:
        return None
    if user.is_superuser:
        return "seller"
    profile = getattr(user, "profile", None)
    return profile.role if profile else "buyer"


def _profile_for(user: User | None):
    if not user or not user.is_authenticated:
        return None
    return getattr(user, "profile", None)


def _has_order_access(user: User, order: Order, role: str | None) -> bool:
    if user.is_superuser:
        return True
    if role == "seller":
        return order.items.filter(part__seller=user).exists()
    return order.buyer_id == user.id


def _allowed_regions_set(user: User) -> set[str]:
    profile = _profile_for(user)
    if not profile or not profile.allowed_regions:
        return set()
    return {x.strip().lower() for x in profile.allowed_regions.split(",") if x.strip()}


def _operator_can_access_part(user: User, part: Part) -> bool:
    if user.is_superuser:
        return True
    profile = _profile_for(user)
    if not profile or profile.role != "seller":
        return False
    if not _has_seller_permission(user, "can_manage_orders"):
        return False

    allowed_brand_ids = set(profile.allowed_brands.values_list("id", flat=True))
    if allowed_brand_ids and part.brand_id and part.brand_id not in allowed_brand_ids:
        return False
    allowed_regions = _allowed_regions_set(user)
    if allowed_regions and part.brand and (part.brand.region or "").lower() not in allowed_regions:
        return False
    return True


def _webhook_payload_for_event(event: OrderEvent) -> dict:
    return {
        "event": event.event_type,
        "source": event.source,
        "created_at": event.created_at.isoformat(),
        "order": {
            "id": event.order_id,
            "status": event.order.status,
            "payment_status": event.order.payment_status,
            "total_amount": str(event.order.total_amount),
            "logistics_cost": str(event.order.logistics_cost),
        },
        "meta": event.meta or {},
    }


def _send_webhook_attempt(*, event: OrderEvent, endpoint: str, payload: dict, attempt: int) -> bool:
    headers = {"Content-Type": "application/json"}
    secret = getattr(settings, "WEBHOOK_SECRET", "") or ""
    if secret:
        headers["X-Webhook-Secret"] = secret
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    log = WebhookDeliveryLog.objects.create(
        order_event=event,
        order=event.order,
        endpoint=endpoint,
        success=False,
        attempt=attempt,
        request_payload=payload,
    )
    try:
        req = Request(endpoint, data=body, headers=headers, method="POST")
        with urlopen(req, timeout=float(getattr(settings, "WEBHOOK_TIMEOUT_SEC", 2))) as resp:
            status_code = int(getattr(resp, "status", 200))
            response_body = resp.read().decode("utf-8", errors="ignore")[:4000]
        is_ok = 200 <= status_code < 300
        log.success = is_ok
        log.status_code = status_code
        log.response_body = response_body
        log.save(update_fields=["success", "status_code", "response_body", "updated_at"])
        return is_ok
    except HTTPError as exc:
        err_body = ""
        try:
            err_body = exc.read().decode("utf-8", errors="ignore")[:4000]
        except Exception:
            err_body = ""
        log.error = f"HTTPError: {exc}"
        log.status_code = int(getattr(exc, "code", 0) or 0)
        log.response_body = err_body
        log.save(update_fields=["error", "status_code", "response_body", "updated_at"])
        return False
    except URLError as exc:
        log.error = f"URLError: {exc}"
        log.save(update_fields=["error", "updated_at"])
        return False
    except Exception as exc:
        log.error = f"Exception: {exc}"
        log.save(update_fields=["error", "updated_at"])
        return False


def _emit_webhooks_for_order_event(event: OrderEvent) -> None:
    endpoints = [x.strip() for x in (getattr(settings, "WEBHOOK_ENDPOINTS", "") or "").split(",") if x.strip()]
    if not endpoints:
        return

    payload = _webhook_payload_for_event(event)
    max_attempts = max(1, int(getattr(settings, "WEBHOOK_RETRY_MAX_ATTEMPTS", 5) or 5))
    for endpoint in endpoints:
        for attempt in range(1, max_attempts + 1):
            if _send_webhook_attempt(event=event, endpoint=endpoint, payload=payload, attempt=attempt):
                break


def _retry_webhook_log(log: WebhookDeliveryLog) -> bool:
    max_attempts = max(1, int(getattr(settings, "WEBHOOK_RETRY_MAX_ATTEMPTS", 5) or 5))
    if log.success or int(log.attempt) >= max_attempts:
        return False

    payload = log.request_payload or {}
    if not payload and log.order_event:
        payload = _webhook_payload_for_event(log.order_event)
    if not payload:
        payload = {
            "event": "unknown",
            "source": "system",
            "created_at": timezone.now().isoformat(),
            "order": {
                "id": log.order_id,
                "status": log.order.status,
                "payment_status": log.order.payment_status,
                "total_amount": str(log.order.total_amount),
                "logistics_cost": str(log.order.logistics_cost),
            },
            "meta": {},
        }

    headers = {"Content-Type": "application/json"}
    secret = getattr(settings, "WEBHOOK_SECRET", "") or ""
    if secret:
        headers["X-Webhook-Secret"] = secret
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    attempt = int(log.attempt) + 1
    retry_log = WebhookDeliveryLog.objects.create(
        order_event=log.order_event,
        order=log.order,
        endpoint=log.endpoint,
        success=False,
        attempt=attempt,
        request_payload=payload,
    )
    try:
        req = Request(log.endpoint, data=body, headers=headers, method="POST")
        with urlopen(req, timeout=float(getattr(settings, "WEBHOOK_TIMEOUT_SEC", 2))) as resp:
            status_code = int(getattr(resp, "status", 200))
            response_body = resp.read().decode("utf-8", errors="ignore")[:4000]
        ok = 200 <= status_code < 300
        retry_log.success = ok
        retry_log.status_code = status_code
        retry_log.response_body = response_body
        retry_log.save(update_fields=["success", "status_code", "response_body", "updated_at"])
        return ok
    except HTTPError as exc:
        err_body = ""
        try:
            err_body = exc.read().decode("utf-8", errors="ignore")[:4000]
        except Exception:
            err_body = ""
        retry_log.error = f"HTTPError: {exc}"
        retry_log.status_code = int(getattr(exc, "code", 0) or 0)
        retry_log.response_body = err_body
        retry_log.save(update_fields=["error", "status_code", "response_body", "updated_at"])
        return False
    except URLError as exc:
        retry_log.error = f"URLError: {exc}"
        retry_log.save(update_fields=["error", "updated_at"])
        return False
    except Exception as exc:
        retry_log.error = f"Exception: {exc}"
        retry_log.save(update_fields=["error", "updated_at"])
        return False


def _can_upload_order_documents(user: User, role: str | None) -> bool:
    if user.is_superuser:
        return True
    if role == "seller":
        return _has_seller_permission(user, "can_manage_orders")
    return role == "buyer"


def _can_manage_claims(user: User, role: str | None) -> bool:
    if user.is_superuser:
        return True
    if role == "seller":
        return _has_seller_permission(user, "can_manage_orders")
    return role == "buyer"


def _build_payment_url(order: Order) -> tuple[str, str]:
    payment_ref = f"INV-{order.id}-{order.created_at:%Y%m%d}"
    payment_base = (settings.PAYMENT_PROVIDER_URL or "").strip()
    payment_currency = settings.PAYMENT_CURRENCY or "USD"
    payment_query = urlencode(
        {
            "merchant": settings.PAYMENT_MERCHANT_ID or "demo-merchant",
            "invoice": payment_ref,
            "order_id": order.id,
            "amount": str(order.total_amount),
            "currency": payment_currency,
            "customer_email": order.customer_email,
        }
    )
    if payment_base:
        delimiter = "&" if "?" in payment_base else "?"
        return f"{payment_base}{delimiter}{payment_query}", payment_ref
    return f"https://pay.consolidator.parts/pay?{payment_query}", payment_ref


def _has_seller_permission(user: User, permission: str) -> bool:
    if user.is_superuser:
        return True
    profile = _profile_for(user)
    if not profile or profile.role != "seller":
        return False
    return bool(getattr(profile, permission, False))


def _apply_seller_brand_scope(user: User, qs):
    profile = _profile_for(user)
    if not profile or profile.role != "seller":
        return qs.none()
    allowed_brand_ids = list(profile.allowed_brands.values_list("id", flat=True))
    if allowed_brand_ids:
        return qs.filter(brand_id__in=allowed_brand_ids)
    return qs


def seller_required(view):
    @wraps(view)
    def wrapped(request: HttpRequest, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect("login")
        if _role_for(request.user) != "seller":
            messages.error(request, "Доступно только для seller.")
            return redirect("dashboard")
        return view(request, *args, **kwargs)

    return wrapped


def operator_required(view):
    @wraps(view)
    def wrapped(request: HttpRequest, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect("login")
        role = _role_for(request.user)
        if not (request.user.is_superuser or role == "seller"):
            messages.error(request, "Доступно только оператору.")
            return redirect("dashboard")
        return view(request, *args, **kwargs)

    return wrapped


def _cart_rows(request: HttpRequest):
    cart = _get_cart(request)
    if not cart:
        return [], Decimal("0.00")

    part_ids = [int(k) for k in cart.keys()]
    parts = Part.objects.filter(id__in=part_ids, is_active=True, price__gt=0)
    rows = []
    total = Decimal("0.00")

    for part in parts:
        qty = int(cart.get(str(part.id), 0))
        line_total = part.price * qty
        total += line_total
        rows.append({"part": part, "quantity": qty, "line_total": line_total})
    return rows, total


def _seed_if_empty() -> None:
    if Category.objects.exists() or Part.objects.exists():
        return
    engine = Category.objects.create(name="Engine", slug="engine")
    hydraulic = Category.objects.create(name="Hydraulic", slug="hydraulic")
    filters = Category.objects.create(name="Filters", slug="filters")

    Part.objects.bulk_create(
        [
            Part(
                title="Piston Assembly CAT 3306",
                slug="piston-assembly-cat-3306",
                oem_number="1234567890",
                description="Premium piston assembly for CAT 3306. Industrial-grade steel.",
                price=Decimal("15000.00"),
                stock_quantity=8,
                condition="oem",
                category=engine,
                image_url="https://images.unsplash.com/photo-1581094271901-8022df4466f9",
            ),
            Part(
                title="Hydraulic Pump Komatsu PC200",
                slug="hydraulic-pump-komatsu-pc200",
                oem_number="HP-KM-88211",
                description="Reliable hydraulic pump, tested for heavy-duty cycles.",
                price=Decimal("48900.00"),
                stock_quantity=3,
                condition="reman",
                category=hydraulic,
                image_url="https://images.unsplash.com/photo-1581092921461-eab62e97a780",
            ),
            Part(
                title="Fuel Filter John Deere 6140",
                slug="fuel-filter-jd-6140",
                oem_number="RE48786",
                description="OEM-compatible filter with high filtration efficiency.",
                price=Decimal("2950.00"),
                stock_quantity=40,
                condition="oem",
                category=filters,
                image_url="https://images.unsplash.com/photo-1613214150388-81f6b8f8246a",
            ),
        ]
    )


def _mixed_featured_parts(limit: int = 12):
    base_qs = Part.objects.filter(is_active=True, price__gt=0).select_related("category", "brand")
    brands = (
        Brand.objects.annotate(parts_count=Count("parts", filter=Q(parts__is_active=True, parts__price__gt=0)))
        .filter(parts_count__gt=0)
        .order_by("-parts_count", "name")
    )

    featured = []
    used_ids = set()

    # One representative item per brand first -> guarantees brand mix.
    for brand in brands:
        part = base_qs.filter(brand=brand).order_by("-updated_at", "-id").first()
        if not part:
            continue
        featured.append(part)
        used_ids.add(part.id)
        if len(featured) >= limit:
            return featured

    # Fill remaining slots with strongest recent items.
    remainder = base_qs.exclude(id__in=used_ids).order_by("-updated_at", "-id")[: max(0, limit - len(featured))]
    featured.extend(list(remainder))
    return featured


def home(request: HttpRequest) -> HttpResponse:
    _seed_if_empty()
    featured = _mixed_featured_parts(limit=12)
    top_categories = (
        Category.objects.annotate(parts_count=Count("parts", filter=Q(parts__is_active=True, parts__price__gt=0)))
        .filter(parts_count__gt=0)
        .order_by("-parts_count", "name")[:12]
    )
    top_brands = (
        Brand.objects.annotate(parts_count=Count("parts", filter=Q(parts__is_active=True, parts__price__gt=0)))
        .filter(parts_count__gt=0)
        .order_by("-parts_count", "name")[:12]
    )
    return render(
        request,
        "marketplace/home.html",
        {"featured": featured, "top_categories": top_categories, "top_brands": top_brands},
    )


def demo_center(request: HttpRequest) -> HttpResponse:
    buyer = User.objects.filter(username="demo_buyer").first()
    seller = User.objects.filter(username="demo_seller").first()
    operator = User.objects.filter(username="demo_operator").first()

    demo_rfq = (
        RFQ.objects.filter(created_by=buyer).order_by("-id").first()
        if buyer
        else None
    )
    demo_orders = (
        Order.objects.filter(buyer=buyer).order_by("-id")[:8]
        if buyer
        else []
    )
    return render(
        request,
        "marketplace/demo_center.html",
        {
            "buyer_user": buyer,
            "seller_user": seller,
            "operator_user": operator,
            "demo_rfq": demo_rfq,
            "demo_orders": demo_orders,
        },
    )


def register_view(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        form = RegisterForm(request.POST)
        if form.is_valid():
            user = form.save(commit=False)
            user.email = form.cleaned_data["email"]
            user.first_name = form.cleaned_data["first_name"]
            user.last_name = form.cleaned_data["last_name"]
            user.save()
            UserProfile.objects.create(
                user=user,
                role=form.cleaned_data["role"],
                company_name=form.cleaned_data["company_name"],
            )
            login(request, user)
            messages.success(request, "Регистрация завершена.")
            return redirect("dashboard")
    else:
        form = RegisterForm()
    return render(request, "marketplace/register.html", {"form": form})


def login_view(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        data = request.POST.copy()
        raw_login = data.get("username", "").strip()
        if "@" in raw_login:
            user = User.objects.filter(email__iexact=raw_login).first()
            if user:
                data["username"] = user.username
        form = LoginForm(request, data=data)
        if form.is_valid():
            login(request, form.get_user())
            messages.success(request, "Вы вошли в систему.")
            return redirect("dashboard")
    else:
        form = LoginForm(request)
    return render(request, "marketplace/login.html", {"form": form})


def logout_view(request: HttpRequest) -> HttpResponse:
    logout(request)
    messages.info(request, "Вы вышли из системы.")
    return redirect("home")


def catalog(request: HttpRequest) -> HttpResponse:
    _seed_if_empty()
    query = request.GET.get("q", "").strip()
    condition = request.GET.get("condition", "").strip()
    category_slug = request.GET.get("category", "").strip()
    brand_slug = request.GET.get("brand", "").strip()
    min_price_raw = request.GET.get("min_price", "").strip()
    max_price_raw = request.GET.get("max_price", "").strip()
    in_stock = request.GET.get("in_stock", "").strip() == "1"
    sort = request.GET.get("sort", "newest").strip() or "newest"

    parts = Part.objects.filter(is_active=True, price__gt=0).select_related("category", "brand")
    if query:
        parts = parts.filter(Q(title__icontains=query) | Q(oem_number__icontains=query))
    if condition:
        parts = parts.filter(condition=condition)
    if category_slug:
        parts = parts.filter(category__slug=category_slug)
    if brand_slug:
        parts = parts.filter(brand__slug=brand_slug)
    if in_stock:
        parts = parts.filter(stock_quantity__gt=0)
    if min_price_raw:
        try:
            parts = parts.filter(price__gte=Decimal(min_price_raw))
        except Exception:
            pass
    if max_price_raw:
        try:
            parts = parts.filter(price__lte=Decimal(max_price_raw))
        except Exception:
            pass

    ordering_map = {
        "newest": "-created_at",
        "price_asc": "price",
        "price_desc": "-price",
        "stock_desc": "-stock_quantity",
    }
    ordering = ordering_map.get(sort, "-created_at")

    paginator = Paginator(parts.order_by(ordering), 48)
    page_obj = paginator.get_page(request.GET.get("page"))
    categories = Category.objects.all().order_by("name")
    brands = Brand.objects.all().order_by("name")
    compare_ids = set(_get_compare_ids(request))
    page_params = request.GET.copy()
    page_params.pop("page", None)
    pagination_query = page_params.urlencode()
    return render(
        request,
        "marketplace/catalog.html",
        {
            "parts": page_obj.object_list,
            "page_obj": page_obj,
            "categories": categories,
            "brands": brands,
            "query": query,
            "selected_condition": condition,
            "selected_category": category_slug,
            "selected_brand": brand_slug,
            "selected_min_price": min_price_raw,
            "selected_max_price": max_price_raw,
            "selected_in_stock": in_stock,
            "selected_sort": sort,
            "pagination_query": pagination_query,
            "compare_ids": compare_ids,
        },
    )


def part_detail(request: HttpRequest, slug: str) -> HttpResponse:
    _seed_if_empty()
    part = get_object_or_404(Part.objects.select_related("category", "brand"), slug=slug, is_active=True, price__gt=0)
    related = Part.objects.filter(category=part.category, is_active=True, price__gt=0).select_related("brand").exclude(id=part.id)[:4]
    return render(request, "marketplace/part_detail.html", {"part": part, "related": related})


@require_POST
def cart_add(request: HttpRequest, part_id: int) -> HttpResponse:
    part = get_object_or_404(Part, id=part_id, is_active=True, price__gt=0)
    cart = _get_cart(request)
    current = int(cart.get(str(part.id), 0))
    if current < part.stock_quantity:
        cart[str(part.id)] = current + 1
    _set_cart(request, cart)
    return redirect(request.POST.get("next") or "cart")


@require_POST
def cart_remove(request: HttpRequest, part_id: int) -> HttpResponse:
    cart = _get_cart(request)
    if str(part_id) in cart:
        del cart[str(part_id)]
        _set_cart(request, cart)
    return redirect("cart")


def cart_view(request: HttpRequest) -> HttpResponse:
    _seed_if_empty()
    rows, total = _cart_rows(request)
    return render(request, "marketplace/cart.html", {"rows": rows, "total": total})


def compare_view(request: HttpRequest) -> HttpResponse:
    _seed_if_empty()
    ids = _get_compare_ids(request)
    if not ids:
        return render(request, "marketplace/compare.html", {"parts": [], "has_items": False})
    parts = list(Part.objects.filter(id__in=ids, is_active=True, price__gt=0).select_related("brand", "category"))
    parts.sort(key=lambda p: ids.index(p.id) if p.id in ids else 10**9)
    return render(request, "marketplace/compare.html", {"parts": parts, "has_items": bool(parts)})


@require_POST
def compare_add(request: HttpRequest, part_id: int) -> HttpResponse:
    part = get_object_or_404(Part, id=part_id, is_active=True, price__gt=0)
    ids = _get_compare_ids(request)
    if part.id not in ids:
        if len(ids) >= 4:
            ids.pop(0)
        ids.append(part.id)
    _set_compare_ids(request, ids)
    return redirect(request.POST.get("next") or "compare")


@require_POST
def compare_remove(request: HttpRequest, part_id: int) -> HttpResponse:
    ids = _get_compare_ids(request)
    ids = [x for x in ids if x != int(part_id)]
    _set_compare_ids(request, ids)
    return redirect(request.POST.get("next") or "compare")


@require_POST
def compare_clear(request: HttpRequest) -> HttpResponse:
    _set_compare_ids(request, [])
    return redirect("compare")


def checkout(request: HttpRequest) -> HttpResponse:
    _seed_if_empty()
    rows, total = _cart_rows(request)
    if not rows:
        return redirect("catalog")

    initial = {}
    if request.user.is_authenticated:
        full_name = " ".join(x for x in [request.user.first_name, request.user.last_name] if x).strip()
        initial = {
            "customer_name": full_name or request.user.username,
            "customer_email": request.user.email,
        }

    if request.method == "POST":
        form = CheckoutForm(request.POST)
        if form.is_valid():
            try:
                order = _create_order_from_rows(
                    rows=rows,
                    total=total,
                    customer_name=form.cleaned_data["customer_name"],
                    customer_email=form.cleaned_data["customer_email"],
                    customer_phone=form.cleaned_data["customer_phone"],
                    delivery_address=form.cleaned_data["delivery_address"],
                    buyer=request.user if request.user.is_authenticated else None,
                    source="buyer" if request.user.is_authenticated else "system",
                )
                _set_cart(request, {})
                return redirect(f"/dashboard/?order_created={order.id}")
            except ValueError as exc:
                messages.error(request, f"Заказ не создан: {exc}")
    else:
        form = CheckoutForm(initial=initial)

    return render(request, "marketplace/checkout.html", {"rows": rows, "total": total, "form": form})


@login_required
def dashboard(request: HttpRequest) -> HttpResponse:
    role = _role_for(request.user)
    if role == "seller":
        return redirect("dashboard_seller")
    return redirect("dashboard_buyer")


@login_required
def kpi_reports(request: HttpRequest) -> HttpResponse:
    role = _role_for(request.user)
    is_seller = role == "seller"
    if is_seller and not _has_seller_permission(request.user, "can_view_analytics"):
        messages.error(request, "Нет прав на аналитику.")
        return redirect("dashboard")

    if is_seller:
        scoped_orders = Order.objects.filter(items__part__seller=request.user).distinct()
        scoped_parts = Part.objects.filter(seller=request.user)
    else:
        scoped_orders = Order.objects.filter(buyer=request.user)
        scoped_parts = Part.objects.none()

    total_orders = scoped_orders.count()
    completed_orders = scoped_orders.filter(status="completed").count()
    cancelled_orders = scoped_orders.filter(status="cancelled").count()
    delivered_orders = scoped_orders.filter(status="delivered").count()
    breached_orders = scoped_orders.filter(sla_status="breached").count()
    total_revenue = sum((o.total_amount for o in scoped_orders[:500]), Decimal("0.00"))
    total_claims = OrderClaim.objects.filter(order__in=scoped_orders).count()
    open_claims = OrderClaim.objects.filter(order__in=scoped_orders, status__in=["open", "in_review"]).count()

    conversion = Decimal("0.00")
    if total_orders:
        conversion = (Decimal(completed_orders) / Decimal(total_orders) * Decimal("100")).quantize(Decimal("0.01"))

    cards = [
        {"label": "Orders", "value": total_orders},
        {"label": "Completed", "value": completed_orders},
        {"label": "Delivered", "value": delivered_orders},
        {"label": "Cancelled", "value": cancelled_orders},
        {"label": "SLA Breached", "value": breached_orders},
        {"label": "Open Claims", "value": open_claims},
        {"label": "Total Claims", "value": total_claims},
        {"label": "Revenue (USD)", "value": total_revenue},
        {"label": "Completion rate %", "value": conversion},
    ]

    return render(
        request,
        "marketplace/kpi_reports.html",
        {
            "role": role,
            "cards": cards,
            "parts_count": scoped_parts.count() if is_seller else 0,
            "recent_orders": scoped_orders.order_by("-id")[:20],
        },
    )


@login_required
def kpi_reports_export_csv(request: HttpRequest) -> HttpResponse:
    role = _role_for(request.user)
    is_seller = role == "seller"
    if is_seller and not _has_seller_permission(request.user, "can_view_analytics"):
        messages.error(request, "Нет прав на аналитику.")
        return redirect("dashboard")
    scoped_orders = Order.objects.filter(items__part__seller=request.user).distinct() if is_seller else Order.objects.filter(buyer=request.user)

    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="kpi_orders_export.csv"'
    writer = csv.writer(response)
    writer.writerow(["order_id", "status", "payment_status", "sla_status", "total_amount", "logistics_cost", "created_at"])
    for order in scoped_orders.order_by("-id")[:5000]:
        writer.writerow(
            [
                order.id,
                order.status,
                order.payment_status,
                order.sla_status,
                order.total_amount,
                order.logistics_cost,
                order.created_at.isoformat(),
            ]
        )
    return response


@login_required
def claims_export_csv(request: HttpRequest) -> HttpResponse:
    role = _role_for(request.user)
    is_seller = role == "seller"
    if is_seller and not _has_seller_permission(request.user, "can_view_analytics"):
        messages.error(request, "Нет прав на аналитику.")
        return redirect("dashboard")
    scoped_orders = Order.objects.filter(items__part__seller=request.user).distinct() if is_seller else Order.objects.filter(buyer=request.user)
    claims = OrderClaim.objects.filter(order__in=scoped_orders).select_related("order", "opened_by", "resolved_by").order_by("-id")[:5000]

    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="claims_export.csv"'
    writer = csv.writer(response)
    writer.writerow(["claim_id", "order_id", "status", "title", "opened_by", "resolved_by", "created_at", "updated_at"])
    for claim in claims:
        writer.writerow(
            [
                claim.id,
                claim.order_id,
                claim.status,
                claim.title,
                claim.opened_by.username if claim.opened_by else "",
                claim.resolved_by.username if claim.resolved_by else "",
                claim.created_at.isoformat(),
                claim.updated_at.isoformat(),
            ]
        )
    return response


@login_required
def dashboard_buyer(request: HttpRequest) -> HttpResponse:
    role = _role_for(request.user)
    orders = list(Order.objects.filter(buyer=request.user).prefetch_related("items__part")[:20])
    for order in orders:
        _recalc_order_sla(order)
    rfqs_count = RFQ.objects.filter(created_by=request.user).count()
    total_spent = sum((o.total_amount for o in orders), Decimal("0.00"))
    return render(
        request,
        "marketplace/dashboard_buyer.html",
        {
            "orders": orders,
            "created_order_id": request.GET.get("order_created"),
            "role": role,
            "orders_count": len(orders),
            "rfqs_count": rfqs_count,
            "total_spent": total_spent,
        },
    )


@seller_required
def dashboard_seller(request: HttpRequest) -> HttpResponse:
    role = _role_for(request.user)
    scoped_parts = _apply_seller_brand_scope(request.user, Part.objects.filter(seller=request.user))
    orders = list(
        Order.objects.filter(items__part__seller=request.user)
        .distinct()
        .prefetch_related("items__part")[:20]
    )
    for order in orders:
        _recalc_order_sla(order)
    parts_count = scoped_parts.count()
    return render(
        request,
        "marketplace/dashboard_seller.html",
        {
            "orders": orders,
            "created_order_id": request.GET.get("order_created"),
            "role": role,
            "orders_count": len(orders),
            "parts_count": parts_count,
        },
    )


@seller_required
def seller_dashboard(request: HttpRequest) -> HttpResponse:
    parts = _apply_seller_brand_scope(
        request.user,
        Part.objects.filter(seller=request.user).select_related("category", "brand"),
    )
    bulk_form = SellerBulkUploadForm()
    profile = _profile_for(request.user)
    module_cards = [
        {"title": "Учетные записи и права", "status": "partial", "note": "Базовые роли готовы, детальный RBAC — следующий этап"},
        {"title": "Ассортимент и прайс-листы", "status": "done", "note": "CSV/XLSX импорт и управление позициями"},
        {"title": "RFQ / Подбор / Котировки", "status": "done", "note": "AUTO/SEMI/MANUAL + матрица статусов"},
        {"title": "Operator Queue", "status": "done", "note": "Подтверждения sandbox/risky, Manual OEM"},
        {"title": "Рейтинг поставщиков", "status": "done", "note": "Формула 60/40 + авто-статусы"},
        {"title": "Заказы / Invoice", "status": "done", "note": "Order flow и документный invoice"},
        {"title": "SLA / События", "status": "done", "note": "События, таймлайн и SLA-контроль включены"},
        {"title": "Документы и рекламации", "status": "done", "note": "Документы заказа и claim workflow в карточке заказа"},
        {"title": "Логистика / Финансы / KPI", "status": "done", "note": "Логистика на заказе, платежный график, KPI-отчеты"},
    ]
    return render(
        request,
        "marketplace/seller_dashboard.html",
        {
            "parts": parts,
            "bulk_form": bulk_form,
            "module_cards": module_cards,
            "profile": profile,
            "upload_report": request.session.get("seller_upload_report"),
        },
    )


def brands_directory(request: HttpRequest) -> HttpResponse:
    query = (request.GET.get("q") or "").strip()
    regions = [
        ("europe", "Europe"),
        ("china", "China"),
        ("components", "Component Manufacturers"),
    ]
    grouped = []
    for key, title in regions:
        brands_qs = (
            Brand.objects.filter(region=key)
            .annotate(parts_count=Count("parts", filter=Q(parts__is_active=True, parts__price__gt=0)))
            .filter(parts_count__gt=0)
        )
        if query:
            brands_qs = brands_qs.filter(name__icontains=query)
        brands = brands_qs.order_by("-parts_count", "name")
        grouped.append({"key": key, "title": title, "brands": brands, "count": brands.count()})
    return render(request, "marketplace/brands_directory.html", {"groups": grouped, "query": query})


def categories_directory(request: HttpRequest) -> HttpResponse:
    categories = (
        Category.objects.all()
        .order_by("name")
        .annotate(parts_count=Count("parts", filter=Q(parts__is_active=True, parts__price__gt=0)))
    )
    total_parts = sum(c.parts_count for c in categories)
    return render(
        request,
        "marketplace/categories_directory.html",
        {"categories": categories, "total_parts": total_parts},
    )


def _match_part_for_query(query: str):
    normalized = (query or "").strip()
    if not normalized:
        return None

    base_qs = _eligible_parts_qs()

    exact = (
        base_qs.filter(oem_number__iexact=normalized)
        .select_related("brand", "category")
        .first()
    )
    if exact:
        return exact

    contains_oem = (
        base_qs.filter(oem_number__icontains=normalized)
        .select_related("brand", "category")
        .first()
    )
    if contains_oem:
        return contains_oem

    contains_title = (
        base_qs.filter(title__icontains=normalized)
        .select_related("brand", "category")
        .first()
    )
    return contains_title


def _supplier_profile_for_part(part: Part):
    seller = part.seller
    if not seller:
        return None
    return getattr(seller, "profile", None)


def _supplier_status_for_part(part: Part) -> str:
    profile = _supplier_profile_for_part(part)
    if not profile or profile.role != "seller":
        return "sandbox"
    return profile.supplier_status or "sandbox"


def _supplier_rating_for_part(part: Part) -> Decimal:
    profile = _supplier_profile_for_part(part)
    if not profile or profile.role != "seller":
        return Decimal("0.00")
    return Decimal(profile.rating_score or 0)


def _is_offer_fresh(part: Part, max_age_days: int = 30) -> bool:
    return part.updated_at >= timezone.now() - timedelta(days=max_age_days)


def _eligible_parts_qs():
    return Part.objects.filter(
        is_active=True,
        price__gt=0,
        currency__isnull=False,
        incoterm__isnull=False,
        moq__gt=0,
        gross_weight_kg__gt=0,
        length_cm__gt=0,
        width_cm__gt=0,
        height_cm__gt=0,
    ).exclude(availability_status__in=["blocked", "discontinued"]).exclude(mapping_status="needs_review")


def _match_confidence_and_pool(query: str):
    normalized = (query or "").strip()
    if not normalized:
        return Decimal("0.00"), Part.objects.none()

    base_qs = _eligible_parts_qs()

    exact = base_qs.filter(oem_number__iexact=normalized)
    if exact.exists():
        return Decimal("95.00"), exact

    by_oem = base_qs.filter(oem_number__icontains=normalized)
    if by_oem.exists():
        return Decimal("75.00"), by_oem

    by_title = base_qs.filter(title__icontains=normalized)
    if by_title.exists():
        return Decimal("65.00"), by_title

    return Decimal("0.00"), Part.objects.none()


def _split_offers_by_status(parts_qs):
    offers = {
        "trusted": [],
        "sandbox": [],
        "risky": [],
        "rejected": [],
    }
    for part in parts_qs.select_related("brand", "category", "seller__profile"):
        if not _is_offer_fresh(part):
            continue
        status = _supplier_status_for_part(part)
        offers.setdefault(status, []).append(part)
    for key in offers:
        offers[key] = sorted(offers[key], key=lambda p: (p.price, -_supplier_rating_for_part(p), p.id))
    return offers


def _select_best_part(candidates):
    if not candidates:
        return None
    return sorted(candidates, key=lambda p: (p.price, -_supplier_rating_for_part(p), p.id))[0]


def _build_rfq_item_decision(query: str, requested_mode: str):
    confidence, raw_pool = _match_confidence_and_pool(query)
    offers = _split_offers_by_status(raw_pool)
    trusted = offers["trusted"]
    sandbox = offers["sandbox"]
    risky = offers["risky"]

    auto_decision = decide_auto_mode(
        AutoModeInputs(
            part_found=bool(raw_pool.exists()),
            confidence=float(confidence),
            trusted_count=len(trusted),
            sandbox_count=len(sandbox),
            fresh_data=True,
        ),
        confidence_threshold=70.0,
    )

    if requested_mode == "manual_oem":
        return {
            "state": "oem_manual",
            "matched_part": None,
            "confidence": confidence,
            "decision_reason": "Manual OEM mode selected.",
            "recommended_supplier_status": "",
            "offers": offers,
        }

    if requested_mode == "auto" and auto_decision.eligible_auto:
        matched = _select_best_part(trusted)
        return {
            "state": "auto_matched",
            "matched_part": matched,
            "confidence": confidence,
            "decision_reason": auto_decision.reason,
            "recommended_supplier_status": "trusted",
            "offers": offers,
        }

    # AUTO fallback or explicit SEMI: operator review required.
    if requested_mode == "auto":
        reason = auto_decision.reason
    else:
        reason = "Semi mode selected, operator confirmation required."

    preferred = _select_best_part(trusted) or _select_best_part(sandbox) or None
    preferred_status = _supplier_status_for_part(preferred) if preferred else ""
    if not preferred and risky:
        preferred = _select_best_part(risky)
        preferred_status = "risky"

    return {
        "state": "needs_review",
        "matched_part": preferred,
        "confidence": confidence,
        "decision_reason": reason,
        "recommended_supplier_status": preferred_status,
        "offers": offers,
    }


def _parse_rfq_items(raw: str) -> list[tuple[str, int]]:
    items: list[tuple[str, int]] = []
    for line in (raw or "").splitlines():
        text = line.strip()
        if not text:
            continue
        left, sep, right = text.partition(";")
        query = left.strip()
        if not query:
            continue
        quantity = 1
        if sep:
            try:
                quantity = max(1, int(right.strip()))
            except Exception:
                quantity = 1
        items.append((query, quantity))
    return items


def _rfq_rows(rfq: RFQ):
    rows = []
    total = Decimal("0.00")
    for item in rfq.items.select_related("matched_part__seller__profile"):
        part = item.matched_part
        if not part or not part.is_eligible_for_matching:
            continue
        supplier_status = _supplier_status_for_part(part)
        if supplier_status in {"rejected", "risky"}:
            continue
        line_total = part.price * item.quantity
        total += line_total
        rows.append(
            {
                "item": item,
                "part": part,
                "quantity": item.quantity,
                "line_total": line_total,
                "supplier_status": supplier_status,
            }
        )
    return rows, total


@login_required
def rfq_list(request: HttpRequest) -> HttpResponse:
    role = _role_for(request.user)
    if role == "seller":
        rfqs = RFQ.objects.all().prefetch_related("items")[:50]
    else:
        rfqs = RFQ.objects.filter(created_by=request.user).prefetch_related("items")[:50]
    return render(request, "marketplace/rfq_list.html", {"rfqs": rfqs, "role": role})


@login_required
def rfq_new(request: HttpRequest) -> HttpResponse:
    initial = {}
    if request.user.is_authenticated:
        full_name = " ".join(x for x in [request.user.first_name, request.user.last_name] if x).strip()
        initial = {
            "customer_name": full_name or request.user.username,
            "customer_email": request.user.email,
        }

    if request.method == "POST":
        form = RFQCreateForm(request.POST)
        if form.is_valid():
            items_data = _parse_rfq_items(form.cleaned_data["items_text"])
            if not items_data:
                form.add_error("items_text", "Добавьте хотя бы одну позицию.")
            else:
                with transaction.atomic():
                    rfq = RFQ.objects.create(
                        created_by=request.user,
                        customer_name=form.cleaned_data["customer_name"],
                        customer_email=form.cleaned_data["customer_email"],
                        company_name=form.cleaned_data["company_name"],
                        mode=form.cleaned_data["mode"],
                        urgency=form.cleaned_data["urgency"],
                        notes=form.cleaned_data["notes"],
                        status="new",
                    )
                    all_auto_approved = True
                    for query, quantity in items_data:
                        decision = _build_rfq_item_decision(query, form.cleaned_data["mode"])
                        item = RFQItem.objects.create(
                            rfq=rfq,
                            query=query,
                            quantity=quantity,
                            matched_part=decision["matched_part"],
                            state=decision["state"],
                            confidence=decision["confidence"],
                            decision_reason=decision["decision_reason"],
                            recommended_supplier_status=decision["recommended_supplier_status"],
                        )
                        if decision["state"] != "auto_matched":
                            all_auto_approved = False

                        # Log risk-related decisions into rating events.
                        matched_part = decision["matched_part"]
                        if matched_part and matched_part.seller:
                            supplier_status = _supplier_status_for_part(matched_part)
                            if supplier_status == "sandbox":
                                SupplierRatingEvent.objects.create(
                                    supplier=matched_part.seller,
                                    event_type="sandbox_selected",
                                    impact_score=Decimal("0.00"),
                                    meta={"rfq_id": rfq.id, "rfq_item_id": item.id, "query": query},
                                )
                            elif supplier_status == "risky":
                                SupplierRatingEvent.objects.create(
                                    supplier=matched_part.seller,
                                    event_type="risky_selected",
                                    impact_score=Decimal("-1.00"),
                                    meta={"rfq_id": rfq.id, "rfq_item_id": item.id, "query": query},
                                )

                    rfq.status = "quoted" if all_auto_approved else "needs_review"
                    rfq.save(update_fields=["status"])

                messages.success(request, f"RFQ #{rfq.id} создан.")
                return redirect("rfq_detail", rfq_id=rfq.id)
    else:
        form = RFQCreateForm(initial=initial)

    return render(request, "marketplace/rfq_new.html", {"form": form})


@login_required
def rfq_detail(request: HttpRequest, rfq_id: int) -> HttpResponse:
    rfq = get_object_or_404(RFQ.objects.prefetch_related("items__matched_part__brand", "items__matched_part__category"), id=rfq_id)
    role = _role_for(request.user)
    if role != "seller" and rfq.created_by_id != request.user.id:
        messages.error(request, "Нет доступа к этому RFQ.")
        return redirect("dashboard")

    rows, total = _rfq_rows(rfq)
    item_cards = []
    for item in rfq.items.all():
        decision = _build_rfq_item_decision(item.query, rfq.mode)
        item_cards.append(
            {
                "item": item,
                "trusted": decision["offers"]["trusted"][:3],
                "sandbox": decision["offers"]["sandbox"][:3],
                "risky": decision["offers"]["risky"][:3],
            }
        )
    return render(
        request,
        "marketplace/rfq_detail.html",
        {"rfq": rfq, "role": role, "rows": rows, "total": total, "item_cards": item_cards},
    )


@login_required
def rfq_proposal(request: HttpRequest, rfq_id: int) -> HttpResponse:
    rfq = get_object_or_404(RFQ.objects.prefetch_related("items__matched_part"), id=rfq_id)
    role = _role_for(request.user)
    if role == "seller" and not request.user.is_superuser:
        messages.error(request, "КП доступно для клиента.")
        return redirect("rfq_detail", rfq_id=rfq.id)
    if rfq.created_by_id and rfq.created_by_id != request.user.id and not request.user.is_superuser:
        messages.error(request, "Нет доступа к этому RFQ.")
        return redirect("rfq_list")

    rows, total = _rfq_rows(rfq)
    if not rows:
        messages.error(request, "Нет доступных позиций для формирования КП.")
        return redirect("rfq_detail", rfq_id=rfq.id)

    initial = {
        "customer_name": rfq.customer_name,
        "customer_email": rfq.customer_email,
    }
    if request.user.is_authenticated:
        initial["customer_email"] = request.user.email or rfq.customer_email

    if request.method == "POST":
        form = CheckoutForm(request.POST)
        if form.is_valid():
            logistics_cost = Decimal("0.00")
            logistics_raw = (request.POST.get("logistics_cost") or "").strip()
            if logistics_raw:
                try:
                    logistics_cost = Decimal(logistics_raw).quantize(Decimal("0.01"))
                    if logistics_cost < 0:
                        logistics_cost = Decimal("0.00")
                except Exception:
                    logistics_cost = Decimal("0.00")
            try:
                order = _create_order_from_rows(
                    rows=rows,
                    total=total,
                    customer_name=form.cleaned_data["customer_name"],
                    customer_email=form.cleaned_data["customer_email"],
                    customer_phone=form.cleaned_data["customer_phone"],
                    delivery_address=form.cleaned_data["delivery_address"],
                    buyer=request.user if request.user.is_authenticated else None,
                    source="buyer",
                    source_id=rfq.id,
                    logistics_override_cost=logistics_cost if logistics_cost > 0 else None,
                )
                _log_order_event(
                    order,
                    "status_changed",
                    source="system",
                    actor=request.user if request.user.is_authenticated else None,
                    meta={"note": "Proposal accepted", "base_total": str(total), "logistics_cost": str(logistics_cost)},
                )
                rfq.status = "quoted"
                rfq.save(update_fields=["status"])
                messages.success(request, f"КП принято. Заказ #{order.id} создан.")
                return redirect("order_invoice", order_id=order.id)
            except ValueError as exc:
                messages.error(request, f"КП не может быть принято: {exc}")
    else:
        form = CheckoutForm(initial=initial)

    return render(
        request,
        "marketplace/rfq_proposal.html",
        {"rfq": rfq, "rows": rows, "total": total, "form": form},
    )


@login_required
@require_POST
def rfq_logistics_estimate(request: HttpRequest, rfq_id: int) -> JsonResponse:
    rfq = get_object_or_404(RFQ, id=rfq_id)
    role = _role_for(request.user)
    if role == "seller" and not request.user.is_superuser:
        return JsonResponse({"ok": False, "error": "forbidden"}, status=403)
    if rfq.created_by_id and rfq.created_by_id != request.user.id and not request.user.is_superuser:
        return JsonResponse({"ok": False, "error": "forbidden"}, status=403)

    payload = {
        "origin": (request.POST.get("origin") or "").strip(),
        "destination": (request.POST.get("destination") or "").strip(),
        "mode": (request.POST.get("mode") or "sea").strip().lower(),
        "incoterm": (request.POST.get("incoterm") or "FOB").strip().upper(),
        "weight_kg": (request.POST.get("weight_kg") or "0").strip(),
        "volume_m3": (request.POST.get("volume_m3") or "0").strip(),
        "currency": (request.POST.get("currency") or "USD").strip().upper(),
    }
    result = logistics_estimate(payload)
    if not result.get("ok", False):
        return JsonResponse({"ok": False, "error": result.get("error", "logistics_calculation_failed"), "result": result}, status=502)
    return JsonResponse({"ok": True, "result": result})


@login_required
def rfq_proposal_pdf(request: HttpRequest, rfq_id: int) -> HttpResponse:
    rfq = get_object_or_404(RFQ.objects.prefetch_related("items__matched_part"), id=rfq_id)
    role = _role_for(request.user)
    if role == "seller" and not request.user.is_superuser:
        messages.error(request, "КП доступно для клиента.")
        return redirect("rfq_detail", rfq_id=rfq.id)
    if rfq.created_by_id and rfq.created_by_id != request.user.id and not request.user.is_superuser:
        messages.error(request, "Нет доступа к этому RFQ.")
        return redirect("rfq_list")

    rows, total = _rfq_rows(rfq)
    if not rows:
        messages.error(request, "Нет доступных позиций для формирования КП.")
        return redirect("rfq_detail", rfq_id=rfq.id)

    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.units import mm
        from reportlab.pdfgen import canvas
    except Exception:
        messages.error(request, "PDF-экспорт требует пакет reportlab. Выполните: pip install reportlab")
        return redirect("rfq_proposal", rfq_id=rfq.id)

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    doc_no = f"KP-{rfq.created_at:%Y%m%d}-{rfq.id}"

    left = 15 * mm
    right = 195 * mm
    y = height - 15 * mm

    # Header band
    pdf.setFillColor(colors.HexColor("#0f2f66"))
    pdf.roundRect(left, y - 16 * mm, right - left, 16 * mm, 4 * mm, fill=1, stroke=0)
    pdf.setFillColor(colors.white)
    pdf.setFont("Helvetica-Bold", 14)
    pdf.drawString(left + 4 * mm, y - 9 * mm, "CONSOLIDATOR PARTS")
    pdf.setFont("Helvetica", 10)
    pdf.drawString(left + 4 * mm, y - 13 * mm, "Commercial Proposal / Коммерческое предложение")

    y -= 23 * mm
    pdf.setFillColor(colors.HexColor("#0c1530"))
    pdf.setFont("Helvetica-Bold", 11)
    pdf.drawString(left, y, f"KP No: {doc_no}")
    pdf.setFont("Helvetica", 9)
    pdf.drawRightString(right, y, f"Date: {rfq.created_at:%d.%m.%Y}")

    y -= 8 * mm
    pdf.setFillColor(colors.HexColor("#1a2748"))
    pdf.roundRect(left, y - 20 * mm, right - left, 20 * mm, 2 * mm, fill=0, stroke=1)
    pdf.setFont("Helvetica-Bold", 9)
    pdf.drawString(left + 3 * mm, y - 5 * mm, "Customer")
    pdf.setFont("Helvetica", 9)
    pdf.drawString(left + 3 * mm, y - 10 * mm, rfq.customer_name[:70])
    pdf.drawString(left + 3 * mm, y - 14 * mm, rfq.customer_email[:70])
    pdf.drawString(left + 3 * mm, y - 18 * mm, (rfq.company_name or "-")[:70])

    y -= 27 * mm
    # Table header
    pdf.setFillColor(colors.HexColor("#e9f0ff"))
    pdf.rect(left, y - 7 * mm, right - left, 7 * mm, fill=1, stroke=0)
    pdf.setFillColor(colors.HexColor("#1b2d57"))
    pdf.setFont("Helvetica-Bold", 8)
    pdf.drawString(left + 2 * mm, y - 4.8 * mm, "#")
    pdf.drawString(left + 8 * mm, y - 4.8 * mm, "Part")
    pdf.drawString(left + 88 * mm, y - 4.8 * mm, "OEM")
    pdf.drawString(left + 120 * mm, y - 4.8 * mm, "Qty")
    pdf.drawString(left + 136 * mm, y - 4.8 * mm, "Lead")
    pdf.drawString(left + 154 * mm, y - 4.8 * mm, "Price")
    pdf.drawString(left + 176 * mm, y - 4.8 * mm, "Line Total")
    y -= 9 * mm

    pdf.setFont("Helvetica", 8)
    for idx, row in enumerate(rows, start=1):
        if y < 36 * mm:
            pdf.showPage()
            y = height - 20 * mm
            pdf.setFont("Helvetica", 8)
        part_title = (row["part"].title or "")[:34]
        oem = (row["part"].oem_number or "")[:20]
        lead = f"{row['part'].production_lead_days} d"
        pdf.setFillColor(colors.HexColor("#0f1f42"))
        pdf.drawString(left + 2 * mm, y, str(idx))
        pdf.drawString(left + 8 * mm, y, part_title)
        pdf.drawString(left + 88 * mm, y, oem)
        pdf.drawRightString(left + 131 * mm, y, str(row["quantity"]))
        pdf.drawRightString(left + 148 * mm, y, lead)
        pdf.drawRightString(left + 171 * mm, y, f"${row['part'].price}")
        pdf.drawRightString(right, y, f"${row['line_total']}")
        y -= 5.2 * mm

    y -= 2 * mm
    pdf.setStrokeColor(colors.HexColor("#b7c7e8"))
    pdf.line(left + 130 * mm, y, right, y)
    y -= 7 * mm
    pdf.setFont("Helvetica-Bold", 11)
    pdf.setFillColor(colors.HexColor("#0f2f66"))
    pdf.drawRightString(right, y, f"TOTAL: ${total}")

    y -= 10 * mm
    pdf.setFont("Helvetica", 8)
    pdf.setFillColor(colors.HexColor("#253c72"))
    pdf.drawString(left, y, "Terms: Prices are valid for 3 business days. Delivery terms are confirmed at order stage.")
    y -= 4.5 * mm
    pdf.drawString(left, y, "Условия: КП действительно 3 рабочих дня. Финальные условия поставки подтверждаются при заказе.")

    y -= 12 * mm
    pdf.setStrokeColor(colors.HexColor("#7d95c6"))
    pdf.line(left, y, left + 70 * mm, y)
    pdf.line(left + 95 * mm, y, right, y)
    pdf.setFont("Helvetica", 8)
    pdf.drawString(left, y - 4 * mm, "Authorized Signature (Seller)")
    pdf.drawString(left + 95 * mm, y - 4 * mm, "Authorized Signature (Buyer)")

    pdf.showPage()
    pdf.save()
    pdf_data = buffer.getvalue()
    buffer.close()

    response = HttpResponse(pdf_data, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{doc_no}.pdf"'
    return response


@login_required
def rfq_checkout(request: HttpRequest, rfq_id: int) -> HttpResponse:
    rfq = get_object_or_404(RFQ.objects.prefetch_related("items__matched_part"), id=rfq_id)
    role = _role_for(request.user)
    if role == "seller" and not request.user.is_superuser:
        messages.error(request, "Оформление из RFQ доступно только buyer.")
        return redirect("rfq_detail", rfq_id=rfq.id)
    if rfq.created_by_id and rfq.created_by_id != request.user.id and not request.user.is_superuser:
        messages.error(request, "Нет доступа к этому RFQ.")
        return redirect("rfq_list")

    rows, total = _rfq_rows(rfq)
    if not rows:
        messages.error(request, "В RFQ нет доступных позиций для заказа.")
        return redirect("rfq_detail", rfq_id=rfq.id)

    initial = {
        "customer_name": rfq.customer_name,
        "customer_email": rfq.customer_email,
    }

    if request.method == "POST":
        form = CheckoutForm(request.POST)
        if form.is_valid():
            try:
                order = _create_order_from_rows(
                    rows=rows,
                    total=total,
                    customer_name=form.cleaned_data["customer_name"],
                    customer_email=form.cleaned_data["customer_email"],
                    customer_phone=form.cleaned_data["customer_phone"],
                    delivery_address=form.cleaned_data["delivery_address"],
                    buyer=request.user if request.user.is_authenticated else None,
                    source="buyer",
                    source_id=rfq.id,
                )
                rfq.status = "quoted"
                rfq.save(update_fields=["status"])
                messages.success(request, f"Заказ #{order.id} создан из RFQ #{rfq.id}.")
                return redirect("dashboard_buyer")
            except ValueError as exc:
                messages.error(request, f"Заказ не создан: {exc}")
    else:
        form = CheckoutForm(initial=initial)

    return render(
        request,
        "marketplace/rfq_checkout.html",
        {"rfq": rfq, "rows": rows, "total": total, "form": form},
    )


@operator_required
def operator_queue(request: HttpRequest) -> HttpResponse:
    active_tab = (request.GET.get("tab") or "queue").strip().lower()
    if active_tab not in {"queue", "manual", "risky"}:
        active_tab = "queue"

    rfq_items = (
        RFQItem.objects.filter(state="needs_review")
        .select_related("rfq", "matched_part__brand", "matched_part__category")
        .order_by("-rfq__created_at", "id")[:200]
    )
    queue_rows = []
    risky_rows = []
    for item in rfq_items:
        decision = _build_rfq_item_decision(item.query, "semi")
        trusted = [p for p in decision["offers"]["trusted"] if _operator_can_access_part(request.user, p)][:5]
        sandbox = [p for p in decision["offers"]["sandbox"] if _operator_can_access_part(request.user, p)][:5]
        risky = [p for p in decision["offers"]["risky"] if _operator_can_access_part(request.user, p)][:5]
        row = (
            {
                "item": item,
                "rfq": item.rfq,
                "trusted": trusted,
                "sandbox": sandbox,
                "risky": risky,
            }
        )
        queue_rows.append(row)
        if risky:
            risky_rows.append(row)

    manual_rows = list(
        RFQItem.objects.filter(state="oem_manual")
        .select_related("rfq")
        .order_by("-rfq__created_at", "id")[:200]
    )
    metrics = {
        "needs_review_count": len(queue_rows),
        "manual_oem_count": len(manual_rows),
        "risky_candidates_count": len(risky_rows),
    }
    return render(
        request,
        "marketplace/operator_queue.html",
        {
            "active_tab": active_tab,
            "queue_rows": queue_rows,
            "manual_rows": manual_rows,
            "risky_rows": risky_rows,
            "metrics": metrics,
        },
    )


@operator_required
def operator_webhooks(request: HttpRequest) -> HttpResponse:
    state = (request.GET.get("state") or "failed").strip().lower()
    if state not in {"all", "failed", "success"}:
        state = "failed"
    endpoint_filter = (request.GET.get("endpoint") or "").strip()

    logs_qs = WebhookDeliveryLog.objects.select_related("order", "order_event")
    if state == "failed":
        logs_qs = logs_qs.filter(success=False)
    elif state == "success":
        logs_qs = logs_qs.filter(success=True)
    if endpoint_filter:
        logs_qs = logs_qs.filter(endpoint__icontains=endpoint_filter)
    logs_qs = logs_qs.order_by("-created_at")

    paginator = Paginator(logs_qs, 40)
    page_obj = paginator.get_page(request.GET.get("page") or 1)
    max_attempts = max(1, int(getattr(settings, "WEBHOOK_RETRY_MAX_ATTEMPTS", 5) or 5))
    metrics = {
        "total": WebhookDeliveryLog.objects.count(),
        "failed": WebhookDeliveryLog.objects.filter(success=False).count(),
        "success": WebhookDeliveryLog.objects.filter(success=True).count(),
    }

    return render(
        request,
        "marketplace/operator_webhooks.html",
        {
            "state": state,
            "endpoint_filter": endpoint_filter,
            "page_obj": page_obj,
            "max_attempts": max_attempts,
            "metrics": metrics,
        },
    )


@operator_required
@require_POST
def operator_retry_webhook(request: HttpRequest, log_id: int) -> HttpResponse:
    log = get_object_or_404(WebhookDeliveryLog.objects.select_related("order", "order_event"), id=log_id)
    ok = _retry_webhook_log(log)
    if ok:
        messages.success(request, f"Webhook #{log_id} успешно доставлен при ретрае.")
    else:
        messages.error(request, f"Webhook #{log_id} не доставлен. Проверь endpoint/секрет/сеть.")
    return redirect(f"{reverse('operator_webhooks')}?state=failed")


@operator_required
@require_POST
def operator_retry_failed_webhooks(request: HttpRequest) -> HttpResponse:
    limit_raw = (request.POST.get("limit") or "30").strip()
    try:
        limit = max(1, min(200, int(limit_raw)))
    except Exception:
        limit = 30
    max_attempts = max(1, int(getattr(settings, "WEBHOOK_RETRY_MAX_ATTEMPTS", 5) or 5))
    failed_logs = list(
        WebhookDeliveryLog.objects.select_related("order", "order_event")
        .filter(success=False, attempt__lt=max_attempts)
        .order_by("created_at")[:limit]
    )
    if not failed_logs:
        messages.info(request, "Нет webhook-ошибок для ретрая.")
        return redirect(f"{reverse('operator_webhooks')}?state=failed")

    ok_count = 0
    fail_count = 0
    for log in failed_logs:
        if _retry_webhook_log(log):
            ok_count += 1
        else:
            fail_count += 1
    messages.success(request, f"Ретрай завершен: успешно {ok_count}, ошибок {fail_count}.")
    return redirect(f"{reverse('operator_webhooks')}?state=failed")


@operator_required
@require_POST
def operator_assign_supplier(request: HttpRequest, rfq_item_id: int) -> HttpResponse:
    item = get_object_or_404(RFQItem.objects.select_related("rfq"), id=rfq_item_id)
    part_id_raw = (request.POST.get("part_id") or "").strip()
    if not part_id_raw.isdigit():
        messages.error(request, "Не выбран поставщик/позиция.")
        return redirect(f"{reverse('operator_queue')}?tab=queue")

    selected_part = get_object_or_404(Part.objects.select_related("seller__profile"), id=int(part_id_raw), is_active=True, price__gt=0)
    if not _operator_can_access_part(request.user, selected_part):
        messages.error(request, "Этот бренд/регион недоступен вашей операторской роли.")
        return redirect(f"{reverse('operator_queue')}?tab=queue")
    status = _supplier_status_for_part(selected_part)
    if status == "rejected":
        messages.error(request, "Исключённый поставщик не может быть выбран.")
        return redirect(f"{reverse('operator_queue')}?tab=queue")

    sandbox_confirm = request.POST.get("sandbox_confirm") == "1"
    risky_confirm = request.POST.get("risky_confirm") == "1"
    risky_double_confirm = request.POST.get("risky_double_confirm") == "1"

    if status == "sandbox" and not sandbox_confirm:
        messages.error(request, "Для Песочницы требуется подтверждение оператора.")
        return redirect(f"{reverse('operator_queue')}?tab=queue")

    if status == "risky" and not (risky_confirm and risky_double_confirm):
        messages.error(request, "Для Рискового поставщика требуется двойное подтверждение.")
        return redirect(f"{reverse('operator_queue')}?tab=risky")

    item.matched_part = selected_part
    item.recommended_supplier_status = status
    item.state = "auto_matched" if status == "trusted" else "needs_review"
    item.decision_reason = f"Operator assigned supplier status={status}, part_id={selected_part.id}"
    item.save(update_fields=["matched_part", "recommended_supplier_status", "state", "decision_reason"])

    if selected_part.seller:
        if status == "sandbox":
            SupplierRatingEvent.objects.create(
                supplier=selected_part.seller,
                event_type="sandbox_selected",
                impact_score=Decimal("0.00"),
                meta={"rfq_id": item.rfq_id, "rfq_item_id": item.id, "actor": request.user.username},
            )
        elif status == "risky":
            SupplierRatingEvent.objects.create(
                supplier=selected_part.seller,
                event_type="risky_selected",
                impact_score=Decimal("-1.00"),
                meta={"rfq_id": item.rfq_id, "rfq_item_id": item.id, "actor": request.user.username},
            )

    # If all items resolved with matched parts, mark RFQ quoted.
    unresolved_exists = item.rfq.items.filter(matched_part__isnull=True).exists()
    if not unresolved_exists:
        item.rfq.status = "quoted"
        item.rfq.save(update_fields=["status"])

    messages.success(request, f"Поставщик назначен для позиции RFQ #{item.rfq_id}.")
    return redirect(f"{reverse('operator_queue')}?tab=queue")


@operator_required
@require_POST
def operator_escalate_manual_oem(request: HttpRequest, rfq_item_id: int) -> HttpResponse:
    item = get_object_or_404(RFQItem.objects.select_related("rfq"), id=rfq_item_id)
    reason = (request.POST.get("manual_reason") or "").strip()
    if not reason:
        messages.error(request, "Укажи причину перевода в ручной OEM-поиск.")
        return redirect(f"{reverse('operator_queue')}?tab=manual")

    item.state = "oem_manual"
    item.matched_part = None
    item.recommended_supplier_status = ""
    item.decision_reason = f"Manual OEM escalation by {request.user.username}: {reason}"
    item.save(update_fields=["state", "matched_part", "recommended_supplier_status", "decision_reason"])

    item.rfq.status = "needs_review"
    item.rfq.save(update_fields=["status"])

    # Audit event (supplier is optional in this event, so we anchor to operator user).
    SupplierRatingEvent.objects.create(
        supplier=request.user,
        event_type="manual_oem_escalation",
        impact_score=Decimal("0.00"),
        meta={"rfq_id": item.rfq_id, "rfq_item_id": item.id, "reason": reason},
    )

    messages.success(request, f"Позиция RFQ #{item.rfq_id} переведена в ручной OEM-поиск.")
    return redirect(f"{reverse('operator_queue')}?tab=manual")


@seller_required
def seller_part_create(request: HttpRequest) -> HttpResponse:
    if not _has_seller_permission(request.user, "can_manage_assortment"):
        messages.error(request, "Нет прав на управление ассортиментом.")
        return redirect("seller_dashboard")
    if not _has_seller_permission(request.user, "can_manage_pricing"):
        messages.error(request, "Нет прав на создание позиций с ценой.")
        return redirect("seller_dashboard")

    if request.method == "POST":
        form = SellerPartForm(request.POST)
        if form.is_valid():
            part = form.save(commit=False)
            base = slugify(part.title)[:220] or "part"
            part.slug = f"{base}-{uuid4().hex[:8]}"
            part.seller = request.user
            part.save()
            messages.success(request, "Товар создан.")
            return redirect("seller_dashboard")
    else:
        form = SellerPartForm()
    return render(request, "marketplace/seller_part_form.html", {"form": form, "mode": "create"})


@seller_required
def seller_part_edit(request: HttpRequest, part_id: int) -> HttpResponse:
    if not _has_seller_permission(request.user, "can_manage_assortment"):
        messages.error(request, "Нет прав на редактирование ассортимента.")
        return redirect("seller_dashboard")

    part = get_object_or_404(_apply_seller_brand_scope(request.user, Part.objects.all()), id=part_id, seller=request.user)
    if request.method == "POST":
        old_price = part.price
        form = SellerPartForm(request.POST, instance=part)
        if form.is_valid():
            updated = form.save(commit=False)
            if not _has_seller_permission(request.user, "can_manage_pricing"):
                updated.price = old_price
            updated.save()
            messages.success(request, "Товар обновлен.")
            return redirect("seller_dashboard")
    else:
        form = SellerPartForm(instance=part)
    return render(request, "marketplace/seller_part_form.html", {"form": form, "mode": "edit", "part": part})


@seller_required
@require_POST
def seller_bulk_upload(request: HttpRequest) -> HttpResponse:
    if not _has_seller_permission(request.user, "can_manage_assortment"):
        messages.error(request, "Нет прав на bulk upload.")
        return redirect("seller_dashboard")
    if not _has_seller_permission(request.user, "can_manage_pricing"):
        messages.error(request, "Нет прав на bulk upload цен.")
        return redirect("seller_dashboard")

    form = SellerBulkUploadForm(request.POST, request.FILES)
    if not form.is_valid():
        messages.error(request, "Некорректная форма загрузки.")
        return redirect("seller_dashboard")
    import_mode = (request.POST.get("import_mode") or "apply").strip().lower()
    if import_mode not in {"preview", "apply"}:
        import_mode = "apply"

    upload = form.cleaned_data["file"]
    metric_inc("import_attempts_total")
    timer = Timer()
    try:
        report = process_seller_csv_upload(
            seller=request.user,
            upload=upload,
            category_name=form.cleaned_data["category"].strip() or "Epiroc",
            default_stock=form.cleaned_data["default_stock"],
            import_mode=import_mode,
        )
    except UploadLimitError as exc:
        metric_inc("import_limits_triggered_total")
        logger.warning(
            "import_limit_exceeded",
            extra={"seller_id": request.user.id, "status": exc.status_code, "reason": str(exc)},
        )
        return JsonResponse({"error": str(exc)}, status=exc.status_code)
    except ValueError as exc:
        metric_inc("import_validation_errors_total")
        logger.warning("import_validation_error", extra={"seller_id": request.user.id, "reason": str(exc)})
        return JsonResponse({"error": str(exc)}, status=400)
    except Exception:
        metric_inc("import_internal_errors_total")
        logger.exception("import_internal_error", extra={"seller_id": request.user.id})
        return JsonResponse({"error": "Ошибка обработки импорта."}, status=500)

    request.session["seller_upload_report"] = {
        "mode": report.mode,
        "created": report.created,
        "updated": report.updated,
        "skipped_no_price": report.skipped_no_price,
        "skipped_invalid": report.skipped_invalid,
        "errors": report.errors,
    }
    metric_inc("import_success_total")
    logger.info(
        "import_finished",
        extra={
            "seller_id": request.user.id,
            "mode": report.mode,
            "created": report.created,
            "updated": report.updated,
            "skipped_no_price": report.skipped_no_price,
            "skipped_invalid": report.skipped_invalid,
            "latency_ms": timer.elapsed_ms(),
        },
    )
    if import_mode == "preview":
        messages.info(
            request,
            f"Предпросмотр: создастся {report.created}, обновится {report.updated}, пропущено без цены {report.skipped_no_price}, пропущено по ошибкам {report.skipped_invalid}.",
        )
    else:
        messages.success(
            request,
            f"Импорт завершен. Создано: {report.created}, обновлено: {report.updated}, пропущено без цены: {report.skipped_no_price}, пропущено по ошибкам: {report.skipped_invalid}.",
        )
    return redirect("seller_dashboard")


@seller_required
def seller_csv_template(request: HttpRequest) -> HttpResponse:
    content = "Part Number,Description,Unitprice,Currency,Stock,OEM\nRE48786,MAIN SWITCH,295.00,USD,10,RE48786\n"
    response = HttpResponse(content, content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="consolidator_template.csv"'
    return response


@seller_required
@require_POST
def seller_order_status_update(request: HttpRequest, order_id: int) -> HttpResponse:
    if not _has_seller_permission(request.user, "can_manage_orders"):
        messages.error(request, "Нет прав на управление заказами.")
        return redirect("dashboard_seller")

    order = get_object_or_404(Order, id=order_id)
    has_access = order.items.filter(part__seller=request.user).exists()
    if not has_access:
        messages.error(request, "Вы не можете менять этот заказ.")
        return redirect("dashboard_seller")

    allowed = {key for key, _ in Order.STATUS_CHOICES}
    status = (request.POST.get("status") or "").strip()
    if status not in allowed:
        messages.error(request, "Неверный статус.")
        return redirect("dashboard_seller")
    seller_allowed_statuses = {"confirmed", "in_production", "ready_to_ship", "shipped", "delivered", "cancelled"}
    if status not in seller_allowed_statuses:
        messages.error(request, "Этот статус может быть изменен только клиентом или системой.")
        return redirect("dashboard_seller")

    current = order.status
    if status != current:
        next_allowed = ORDER_TRANSITIONS.get(current, set())
        if status not in next_allowed:
            messages.error(request, f"Недопустимый переход статуса: {current} -> {status}")
            return redirect("dashboard_seller")

    update_fields = ["status"]
    order.status = status
    if status == "confirmed" and not order.ship_deadline:
        order.ship_deadline = timezone.now() + timedelta(days=5)
        update_fields.append("ship_deadline")
    order.save(update_fields=update_fields)
    _log_order_event(
        order,
        "status_changed",
        source="seller",
        actor=request.user,
        meta={"from": current, "to": status},
    )
    _recalc_order_sla(order)

    if status == "cancelled":
        seller_ids = (
            order.items.values_list("part__seller_id", flat=True)
            .exclude(part__seller_id__isnull=True)
            .distinct()
        )
        for seller_id in seller_ids:
            SupplierRatingEvent.objects.create(
                supplier_id=seller_id,
                event_type="order_cancellation",
                impact_score=Decimal("-8.00"),
                meta={"order_id": order.id},
            )
    if status == "shipped" and order.ship_deadline and timezone.now() > order.ship_deadline:
        seller_ids = (
            order.items.values_list("part__seller_id", flat=True)
            .exclude(part__seller_id__isnull=True)
            .distinct()
        )
        for seller_id in seller_ids:
            SupplierRatingEvent.objects.create(
                supplier_id=seller_id,
                event_type="delivery_delay",
                impact_score=Decimal("-5.00"),
                meta={"order_id": order.id, "deadline": order.ship_deadline.isoformat()},
            )
    messages.success(request, f"Статус заказа #{order.id} обновлен: {order.get_status_display()}")
    return redirect("dashboard_seller")


@login_required
def order_detail(request: HttpRequest, order_id: int) -> HttpResponse:
    order = get_object_or_404(
        Order.objects.prefetch_related("items__part", "events", "documents", "claims"),
        id=order_id,
    )
    role = _role_for(request.user)

    if not _has_order_access(request.user, order, role):
        messages.error(request, "Нет доступа к заказу.")
        return redirect("dashboard")

    _recalc_order_sla(order)
    is_buyer = order.buyer_id == request.user.id
    can_upload_docs = _can_upload_order_documents(request.user, role)
    can_manage_claims = _can_manage_claims(request.user, role)
    can_open_claim = can_manage_claims and is_buyer and order.status in {"delivered", "completed"}
    return render(
        request,
        "marketplace/order_detail.html",
        {
            "order": order,
            "events": order.events.all()[:100],
            "documents": order.documents.all()[:100],
            "claims": order.claims.all()[:100],
            "is_buyer": is_buyer,
            "can_upload_docs": can_upload_docs,
            "can_manage_claims": can_manage_claims,
            "can_open_claim": can_open_claim,
            "claim_status_choices": OrderClaim.STATUS_CHOICES,
            "document_type_choices": OrderDocument.DOC_TYPE_CHOICES,
        },
    )


@login_required
def order_invoice(request: HttpRequest, order_id: int) -> HttpResponse:
    order = get_object_or_404(Order.objects.prefetch_related("items__part"), id=order_id)
    role = _role_for(request.user)

    if not _has_order_access(request.user, order, role):
        messages.error(request, "Нет доступа к инвойсу этого заказа.")
        return redirect("dashboard")

    _log_order_event(
        order,
        "invoice_opened",
        source="seller" if _role_for(request.user) == "seller" else "buyer",
        actor=request.user,
    )
    subtotal = sum((item.total_price for item in order.items.all()), Decimal("0.00"))
    reserve_due = max(Decimal("0.00"), (order.reserve_amount or Decimal("0.00")))
    final_due = max(Decimal("0.00"), (order.total_amount or Decimal("0.00")) - reserve_due)
    payment_url, payment_ref = _build_payment_url(order)
    is_buyer = order.buyer_id == request.user.id
    return render(
        request,
        "marketplace/order_invoice.html",
        {
            "order": order,
            "subtotal": subtotal,
            "reserve_due": reserve_due,
            "final_due": final_due,
            "payment_url": payment_url,
            "payment_ref": payment_ref,
            "is_buyer": is_buyer,
        },
    )


@login_required
def order_invoice_pdf(request: HttpRequest, order_id: int) -> HttpResponse:
    order = get_object_or_404(Order.objects.prefetch_related("items__part"), id=order_id)
    role = _role_for(request.user)
    if not _has_order_access(request.user, order, role):
        messages.error(request, "Нет доступа к инвойсу этого заказа.")
        return redirect("dashboard")

    _log_order_event(
        order,
        "invoice_opened",
        source="seller" if _role_for(request.user) == "seller" else "buyer",
        actor=request.user,
        meta={"channel": "pdf"},
    )

    subtotal = sum((item.total_price for item in order.items.all()), Decimal("0.00"))
    reserve_due = max(Decimal("0.00"), (order.reserve_amount or Decimal("0.00")))
    final_due = max(Decimal("0.00"), (order.total_amount or Decimal("0.00")) - reserve_due)
    reserve_due_date = order.created_at + timedelta(days=1)
    final_due_date = order.ship_deadline or (order.created_at + timedelta(days=7))
    payment_url, payment_ref = _build_payment_url(order)

    try:
        from reportlab.graphics.barcode.qr import QrCodeWidget
        from reportlab.graphics import renderPDF
        from reportlab.graphics.shapes import Drawing
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.units import mm
        from reportlab.pdfgen import canvas
    except Exception:
        messages.error(request, "PDF-экспорт требует пакет reportlab. Выполните: pip install reportlab")
        return redirect("order_invoice", order_id=order.id)

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    left = 15 * mm
    right = 195 * mm
    y = height - 15 * mm

    # Header
    pdf.setFillColor(colors.HexColor("#0f2f66"))
    pdf.roundRect(left, y - 16 * mm, right - left, 16 * mm, 4 * mm, fill=1, stroke=0)
    pdf.setFillColor(colors.white)
    pdf.setFont("Helvetica-Bold", 14)
    pdf.drawString(left + 4 * mm, y - 9 * mm, "CONSOLIDATOR PARTS")
    pdf.setFont("Helvetica", 10)
    pdf.drawString(left + 4 * mm, y - 13 * mm, "Invoice / Счет на оплату")
    y -= 23 * mm

    pdf.setFillColor(colors.HexColor("#0c1530"))
    pdf.setFont("Helvetica-Bold", 11)
    invoice_no = order.invoice_number or f"INV-{order.created_at:%Y%m%d}-{order.id}"
    pdf.drawString(left, y, f"Invoice No: {invoice_no}")
    pdf.setFont("Helvetica", 9)
    pdf.drawRightString(right, y, f"Date: {order.created_at:%d.%m.%Y}")
    y -= 7 * mm
    pdf.drawString(left, y, f"Order: #{order.id}")
    pdf.drawString(left + 48 * mm, y, f"Status: {order.get_status_display()}")
    pdf.drawString(left + 95 * mm, y, f"Payment: {order.get_payment_status_display()}")

    y -= 10 * mm
    pdf.setStrokeColor(colors.HexColor("#1a2748"))
    pdf.roundRect(left, y - 21 * mm, right - left, 21 * mm, 2 * mm, fill=0, stroke=1)
    pdf.setFont("Helvetica-Bold", 9)
    pdf.drawString(left + 3 * mm, y - 5 * mm, "Buyer")
    pdf.setFont("Helvetica", 9)
    pdf.drawString(left + 3 * mm, y - 10 * mm, order.customer_name[:70])
    pdf.drawString(left + 3 * mm, y - 14 * mm, order.customer_email[:70])
    pdf.drawString(left + 3 * mm, y - 18 * mm, order.delivery_address[:90])

    y -= 27 * mm
    # Items table
    pdf.setFillColor(colors.HexColor("#e9f0ff"))
    pdf.rect(left, y - 7 * mm, right - left, 7 * mm, fill=1, stroke=0)
    pdf.setFillColor(colors.HexColor("#1b2d57"))
    pdf.setFont("Helvetica-Bold", 8)
    pdf.drawString(left + 2 * mm, y - 4.8 * mm, "#")
    pdf.drawString(left + 8 * mm, y - 4.8 * mm, "Part")
    pdf.drawString(left + 96 * mm, y - 4.8 * mm, "OEM")
    pdf.drawString(left + 130 * mm, y - 4.8 * mm, "Qty")
    pdf.drawString(left + 147 * mm, y - 4.8 * mm, "Price")
    pdf.drawString(left + 176 * mm, y - 4.8 * mm, "Line")
    y -= 9 * mm
    pdf.setFont("Helvetica", 8)

    for idx, item in enumerate(order.items.all(), start=1):
        if y < 50 * mm:
            pdf.showPage()
            y = height - 20 * mm
            pdf.setFont("Helvetica", 8)
        pdf.setFillColor(colors.HexColor("#0f1f42"))
        pdf.drawString(left + 2 * mm, y, str(idx))
        pdf.drawString(left + 8 * mm, y, (item.part.title or "")[:40])
        pdf.drawString(left + 96 * mm, y, (item.part.oem_number or "")[:20])
        pdf.drawRightString(left + 143 * mm, y, str(item.quantity))
        pdf.drawRightString(left + 170 * mm, y, f"${item.unit_price}")
        pdf.drawRightString(right, y, f"${item.total_price}")
        y -= 5.1 * mm

    y -= 1 * mm
    pdf.setStrokeColor(colors.HexColor("#b7c7e8"))
    pdf.line(left + 118 * mm, y, right, y)
    y -= 6 * mm
    pdf.setFont("Helvetica", 9)
    pdf.setFillColor(colors.HexColor("#253c72"))
    pdf.drawRightString(right, y, f"Subtotal: ${subtotal}")
    y -= 5 * mm
    pdf.drawRightString(right, y, f"Logistics: ${order.logistics_cost} {order.logistics_currency} ({order.logistics_provider})")
    y -= 5 * mm
    pdf.setFont("Helvetica-Bold", 11)
    pdf.setFillColor(colors.HexColor("#0f2f66"))
    pdf.drawRightString(right, y, f"TOTAL: ${order.total_amount}")

    # Payment schedule + QR
    y -= 11 * mm
    pdf.setFont("Helvetica-Bold", 10)
    pdf.setFillColor(colors.HexColor("#0c1530"))
    pdf.drawString(left, y, "Payment Schedule / График платежей")
    y -= 6 * mm
    pdf.setFont("Helvetica", 9)
    pdf.drawString(left, y, f"1) Reserve {order.reserve_percent}%: ${reserve_due}  due {reserve_due_date:%d.%m.%Y}")
    y -= 5 * mm
    pdf.drawString(left, y, f"2) Final payment: ${final_due}  due {final_due_date:%d.%m.%Y}")
    y -= 5 * mm
    pdf.drawString(left, y, f"Payment reference: {payment_ref}")

    qr_size = 28 * mm
    qr = QrCodeWidget(payment_url)
    bounds = qr.getBounds()
    qr_width = bounds[2] - bounds[0]
    qr_height = bounds[3] - bounds[1]
    drawing = Drawing(qr_size, qr_size, transform=[qr_size / qr_width, 0, 0, qr_size / qr_height, 0, 0])
    drawing.add(qr)
    renderPDF.draw(drawing, pdf, right - qr_size, y - qr_size + 3 * mm)
    pdf.setFont("Helvetica", 7)
    pdf.drawString(right - qr_size, y - qr_size - 1 * mm, "Scan for payment link")

    y -= 35 * mm
    pdf.setFont("Helvetica", 8)
    pdf.setFillColor(colors.HexColor("#253c72"))
    pdf.drawString(left, y, "This invoice is generated automatically by Consolidator Parts.")
    y -= 4.5 * mm
    pdf.drawString(left, y, "Настоящий счет сформирован автоматически системой Consolidator Parts.")

    pdf.showPage()
    pdf.save()
    pdf_data = buffer.getvalue()
    buffer.close()

    response = HttpResponse(pdf_data, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{invoice_no}.pdf"'
    return response


@login_required
@require_POST
def order_mark_reserve_paid(request: HttpRequest, order_id: int) -> HttpResponse:
    order = get_object_or_404(Order, id=order_id)
    role = _role_for(request.user)
    if order.buyer_id != request.user.id and not request.user.is_superuser:
        messages.error(request, "Только клиент заказа может подтверждать оплату резерва.")
        return redirect("order_invoice", order_id=order.id)
    if not _has_order_access(request.user, order, role):
        messages.error(request, "Нет доступа к заказу.")
        return redirect("dashboard")
    if order.status in {"cancelled", "completed"}:
        messages.error(request, "Заказ закрыт для изменения оплаты.")
        return redirect("order_invoice", order_id=order.id)
    if order.payment_status in {"reserve_paid", "paid"}:
        messages.info(request, "Резерв уже зафиксирован.")
        return redirect("order_invoice", order_id=order.id)

    previous = order.status
    order.payment_status = "reserve_paid"
    order.reserve_paid_at = timezone.now()
    if order.status == "pending":
        order.status = "reserve_paid"
    order.save(update_fields=["payment_status", "reserve_paid_at", "status"])
    _log_order_event(order, "reserve_paid", source="buyer", actor=request.user, meta={"reserve_amount": str(order.reserve_amount)})
    if previous != order.status:
        _log_order_event(order, "status_changed", source="buyer", actor=request.user, meta={"from": previous, "to": order.status})
    messages.success(request, "Резерв 10% зафиксирован.")
    return redirect("order_invoice", order_id=order.id)


@login_required
@require_POST
def order_mark_final_paid(request: HttpRequest, order_id: int) -> HttpResponse:
    order = get_object_or_404(Order, id=order_id)
    role = _role_for(request.user)
    if order.buyer_id != request.user.id and not request.user.is_superuser:
        messages.error(request, "Только клиент заказа может подтверждать финальную оплату.")
        return redirect("order_invoice", order_id=order.id)
    if not _has_order_access(request.user, order, role):
        messages.error(request, "Нет доступа к заказу.")
        return redirect("dashboard")
    if order.status in {"cancelled", "completed"}:
        messages.error(request, "Заказ закрыт для изменения оплаты.")
        return redirect("order_invoice", order_id=order.id)
    if order.payment_status == "paid":
        messages.info(request, "Финальная оплата уже зафиксирована.")
        return redirect("order_invoice", order_id=order.id)
    if order.payment_status != "reserve_paid":
        messages.error(request, "Сначала нужно зафиксировать резерв 10%.")
        return redirect("order_invoice", order_id=order.id)

    order.payment_status = "paid"
    order.final_paid_at = timezone.now()
    order.save(update_fields=["payment_status", "final_paid_at"])
    _log_order_event(order, "final_payment_paid", source="buyer", actor=request.user, meta={"total_amount": str(order.total_amount)})
    messages.success(request, "Финальная оплата зафиксирована.")
    return redirect("order_invoice", order_id=order.id)


@login_required
@require_POST
def order_confirm_quality(request: HttpRequest, order_id: int) -> HttpResponse:
    order = get_object_or_404(Order, id=order_id)
    role = _role_for(request.user)
    if order.buyer_id != request.user.id and not request.user.is_superuser:
        messages.error(request, "Только клиент заказа может подтвердить качество.")
        return redirect("order_detail", order_id=order.id)
    if not _has_order_access(request.user, order, role):
        messages.error(request, "Нет доступа к заказу.")
        return redirect("dashboard")
    if order.status != "delivered":
        messages.error(request, "Качество можно подтвердить только после статуса Delivered.")
        return redirect("order_detail", order_id=order.id)
    if order.payment_status != "paid":
        messages.error(request, "Перед закрытием заказа нужна финальная оплата.")
        return redirect("order_detail", order_id=order.id)

    previous = order.status
    order.status = "completed"
    order.save(update_fields=["status"])
    _log_order_event(order, "quality_confirmed", source="buyer", actor=request.user)
    _log_order_event(order, "status_changed", source="buyer", actor=request.user, meta={"from": previous, "to": order.status})
    messages.success(request, "Качество подтверждено. Заказ закрыт.")
    return redirect("order_detail", order_id=order.id)


@login_required
@require_POST
def order_add_document(request: HttpRequest, order_id: int) -> HttpResponse:
    order = get_object_or_404(Order, id=order_id)
    role = _role_for(request.user)
    if not _has_order_access(request.user, order, role):
        messages.error(request, "Нет доступа к заказу.")
        return redirect("dashboard")
    if not _can_upload_order_documents(request.user, role):
        messages.error(request, "Нет прав на загрузку документов.")
        return redirect("order_detail", order_id=order.id)

    doc_type = (request.POST.get("doc_type") or "other").strip()
    title = (request.POST.get("title") or "").strip()
    file_url = (request.POST.get("file_url") or "").strip()
    file_obj = request.FILES.get("file_obj")
    allowed_doc_types = {key for key, _ in OrderDocument.DOC_TYPE_CHOICES}
    blocked_extensions = {
        ".exe",
        ".sh",
        ".bat",
        ".cmd",
        ".msi",
        ".php",
        ".js",
        ".jar",
        ".com",
        ".scr",
    }
    allowed_extensions = {
        ".pdf",
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
        ".txt",
        ".csv",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
    }

    if doc_type not in allowed_doc_types:
        messages.error(request, "Некорректный тип документа.")
        return redirect("order_detail", order_id=order.id)
    if not title:
        messages.error(request, "Укажите название документа.")
        return redirect("order_detail", order_id=order.id)
    if not file_url and not file_obj:
        messages.error(request, "Добавьте файл или ссылку на документ.")
        return redirect("order_detail", order_id=order.id)
    if file_obj:
        ext = os.path.splitext(file_obj.name or "")[1].lower()
        if ext in blocked_extensions or ext not in allowed_extensions:
            messages.error(request, "Тип файла не разрешен.")
            return redirect("order_detail", order_id=order.id)
        if int(file_obj.size or 0) > int(settings.MAX_ORDER_DOCUMENT_BYTES):
            messages.error(request, f"Файл слишком большой (макс. {settings.MAX_ORDER_DOCUMENT_BYTES} байт).")
            return redirect("order_detail", order_id=order.id)
        # Normalize filename for storage.
        safe_name = slugify(os.path.splitext(file_obj.name)[0]) or "document"
        file_obj.name = f"{safe_name}{ext}"

    doc = OrderDocument.objects.create(
        order=order,
        doc_type=doc_type,
        title=title,
        file_url=file_url,
        file_obj=file_obj,
        uploaded_by=request.user,
    )
    _log_order_event(
        order,
        "document_uploaded",
        source="seller" if role == "seller" else "buyer",
        actor=request.user,
        meta={"document_id": doc.id, "doc_type": doc_type, "title": title},
    )
    messages.success(request, "Документ добавлен к заказу.")
    return redirect("order_detail", order_id=order.id)


@csrf_exempt
@require_POST
def payment_callback(request: HttpRequest) -> HttpResponse:
    configured_secret = (getattr(settings, "PAYMENT_CALLBACK_SECRET", "") or "").strip()
    provided_secret = (
        request.headers.get("X-Payment-Secret")
        or request.POST.get("secret")
        or request.GET.get("secret")
        or ""
    ).strip()
    if configured_secret and configured_secret != provided_secret:
        return JsonResponse({"ok": False, "error": "invalid_secret"}, status=403)

    payload: dict = {}
    if request.content_type and "application/json" in request.content_type.lower():
        try:
            payload = json.loads(request.body.decode("utf-8") or "{}")
        except Exception:
            payload = {}
    if not payload:
        payload = request.POST.dict()

    order_id_raw = payload.get("order_id") or payload.get("orderId")
    invoice_number = (payload.get("invoice_number") or payload.get("invoice") or "").strip()
    callback_status = (payload.get("status") or payload.get("payment_status") or "").strip().lower()
    transaction_id = (payload.get("transaction_id") or payload.get("tx_id") or "").strip()

    order = None
    if order_id_raw:
        try:
            order = Order.objects.filter(id=int(order_id_raw)).first()
        except Exception:
            order = None
    if not order and invoice_number:
        order = Order.objects.filter(invoice_number=invoice_number).first()
    if not order:
        return JsonResponse({"ok": False, "error": "order_not_found"}, status=404)

    meta = {
        "callback_status": callback_status,
        "transaction_id": transaction_id,
        "invoice_number": order.invoice_number,
    }
    changed_fields: list[str] = []
    if callback_status in {"reserve_paid", "reserve_success", "deposit_paid"}:
        if order.payment_status not in {"reserve_paid", "paid"}:
            order.payment_status = "reserve_paid"
            changed_fields.append("payment_status")
        if not order.reserve_paid_at:
            order.reserve_paid_at = timezone.now()
            changed_fields.append("reserve_paid_at")
        if order.status == "pending":
            prev_status = order.status
            order.status = "reserve_paid"
            changed_fields.append("status")
            _log_order_event(order, "status_changed", source="system", meta={"from": prev_status, "to": order.status, **meta})
        order.save(update_fields=list(set(changed_fields)))
        _log_order_event(order, "reserve_paid", source="system", meta=meta)
    elif callback_status in {"paid", "success", "final_paid", "full_paid"}:
        if order.payment_status != "paid":
            order.payment_status = "paid"
            changed_fields.append("payment_status")
        if not order.final_paid_at:
            order.final_paid_at = timezone.now()
            changed_fields.append("final_paid_at")
        order.save(update_fields=list(set(changed_fields)))
        _log_order_event(order, "final_payment_paid", source="system", meta=meta)
    elif callback_status in {"refunded", "refund"}:
        if order.payment_status != "refunded":
            order.payment_status = "refunded"
            order.save(update_fields=["payment_status"])
        _log_order_event(order, "status_changed", source="system", meta={"from": order.status, "to": order.status, **meta})
    else:
        return JsonResponse({"ok": False, "error": "unsupported_status", "status": callback_status}, status=400)

    return JsonResponse({"ok": True, "order_id": order.id, "payment_status": order.payment_status})


@login_required
@require_POST
def order_open_claim(request: HttpRequest, order_id: int) -> HttpResponse:
    order = get_object_or_404(Order, id=order_id)
    role = _role_for(request.user)
    if not _has_order_access(request.user, order, role):
        messages.error(request, "Нет доступа к заказу.")
        return redirect("dashboard")
    if not _can_manage_claims(request.user, role):
        messages.error(request, "Нет прав на работу с рекламациями.")
        return redirect("order_detail", order_id=order.id)
    if order.status not in {"delivered", "completed"}:
        messages.error(request, "Рекламация доступна после доставки.")
        return redirect("order_detail", order_id=order.id)

    title = (request.POST.get("title") or "").strip()
    description = (request.POST.get("description") or "").strip()
    if not title or not description:
        messages.error(request, "Заполните тему и описание рекламации.")
        return redirect("order_detail", order_id=order.id)

    claim = OrderClaim.objects.create(
        order=order,
        title=title,
        description=description,
        status="open",
        opened_by=request.user,
    )
    _log_order_event(
        order,
        "claim_opened",
        source="seller" if role == "seller" else "buyer",
        actor=request.user,
        meta={"claim_id": claim.id, "title": title},
    )
    messages.success(request, "Рекламация открыта.")
    return redirect("order_detail", order_id=order.id)


@login_required
@require_POST
def order_update_claim_status(request: HttpRequest, claim_id: int) -> HttpResponse:
    claim = get_object_or_404(OrderClaim.objects.select_related("order"), id=claim_id)
    order = claim.order
    role = _role_for(request.user)
    if not _has_order_access(request.user, order, role):
        messages.error(request, "Нет доступа к заказу.")
        return redirect("dashboard")
    if not _can_manage_claims(request.user, role):
        messages.error(request, "Нет прав на работу с рекламациями.")
        return redirect("order_detail", order_id=order.id)

    new_status = (request.POST.get("status") or "").strip()
    allowed_statuses = {key for key, _ in OrderClaim.STATUS_CHOICES}
    if new_status not in allowed_statuses:
        messages.error(request, "Некорректный статус рекламации.")
        return redirect("order_detail", order_id=order.id)

    prev_status = claim.status
    if prev_status == new_status:
        messages.info(request, "Статус рекламации не изменился.")
        return redirect("order_detail", order_id=order.id)

    claim.status = new_status
    claim.resolved_by = request.user if new_status in {"approved", "rejected", "closed"} else claim.resolved_by
    claim.save(update_fields=["status", "resolved_by", "updated_at"])
    _log_order_event(
        order,
        "claim_status_changed",
        source="seller" if role == "seller" else "buyer",
        actor=request.user,
        meta={"claim_id": claim.id, "from": prev_status, "to": new_status},
    )
    if new_status == "approved":
        order.payment_status = "refund_pending"
        order.save(update_fields=["payment_status"])
    if new_status in {"rejected", "closed"} and order.payment_status == "refund_pending":
        order.payment_status = "paid"
        order.save(update_fields=["payment_status"])

    messages.success(request, "Статус рекламации обновлён.")
    return redirect("order_detail", order_id=order.id)

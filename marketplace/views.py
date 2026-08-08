import csv
import hashlib
import json
import logging
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from urllib.error import HTTPError, URLError
from urllib.request import Request

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core import signing
from django.core.cache import cache
from django.core.exceptions import RequestDataTooBig
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import F, Sum
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST


from .forms import (
    LoginForm,
    RegisterForm,
)
from .export_security import safe_spreadsheet_row
from .models import (
    RFQ,
    Order,
    OrderClaim,
    OrderEvent,
    OrderItem,
    Part,
    RFQItem,
    UserProfile,
    WebhookDeliveryLog,
)
from .services.logistics import logistics_estimate

ORDER_TRANSITIONS = {
    "pending": {"reserve_paid", "cancelled"},
    "reserve_paid": {"pending", "confirmed", "cancelled"},
    "confirmed": {"reserve_paid", "in_production", "cancelled"},
    "in_production": {"confirmed", "ready_to_ship", "cancelled"},
    "ready_to_ship": {"in_production", "transit_abroad", "shipped", "cancelled"},
    "transit_abroad": {"ready_to_ship", "customs", "cancelled"},
    "customs": {"transit_abroad", "transit_rf", "cancelled"},
    "transit_rf": {"customs", "issuing", "cancelled"},
    "issuing": {"transit_rf", "shipped", "cancelled"},
    "shipped": {"issuing", "delivered", "cancelled"},
    "delivered": {"shipped", "completed"},
    "completed": set(),
    "cancelled": set(),
}


logger = logging.getLogger("marketplace")


def _safe_next_url(request: HttpRequest, fallback: str) -> str:
    target = (request.POST.get("next") or request.GET.get("next") or "").strip()
    if target and url_has_allowed_host_and_scheme(
        target,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return target
    return fallback




def _log_order_event(order: Order, event_type: str, source: str = "system", actor: User | None = None, meta: dict | None = None):
    event = OrderEvent.objects.create(
        order=order,
        event_type=event_type,
        source=source,
        actor=actor,
        meta=meta or {},
    )
    _emit_webhooks_for_order_event(event)


# SLA нормативы по этапам (в часах) из таблицы "Этапы ЛК"
SLA_STAGE_NORMS: dict[str, int] = {
    "pending": 48,          # Ожидание оплаты: ≤ 48 ч
    "reserve_paid": 48,
    "confirmed": 168,       # Формирование заказа: ≤ 7 дн (2+5)
    "in_production": 168,
    "ready_to_ship": 48,
    "transit_abroad": 240,  # Транзит (авто, КНР): ≤ 10 дн
    "customs": 48,          # Таможня: ≤ 2 рабочих дня
    "transit_rf": 24,       # Транзит РФ: ≤ 1 рабочий день
    "issuing": 24,          # Выдача: ≤ 1 рабочий день
    "shipped": 24,
    "delivered": 72,        # Приёмка: ≤ 3 рабочих дня
}


def _recalc_order_sla(order: Order):
    previous = order.sla_status
    now = timezone.now()
    status = "on_track"

    norm_hours = SLA_STAGE_NORMS.get(order.status)
    if norm_hours:
        # Use prefetched events if available, otherwise query DB
        entered_at = order.created_at
        try:
            cached_events = order.events.all()  # uses prefetch cache if set
            last_event = next(
                (e for e in cached_events
                 if e.event_type == "status_changed" and e.meta.get("to") == order.status),
                None,
            )
        except Exception:
            last_event = (
                OrderEvent.objects.filter(
                    order=order, event_type="status_changed", meta__to=order.status
                ).order_by("-created_at").first()
            )
        if last_event:
            entered_at = last_event.created_at
        elapsed_hours = (now - entered_at).total_seconds() / 3600

        if elapsed_hours >= norm_hours:
            status = "breached"
        elif elapsed_hours >= norm_hours * 0.75:
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
        if not logistics_result.get("ok", False):
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

    # Определяем схему оплаты (request передаётся caller'ом если есть POST)
    # NB: `request` НЕ в сигнатуре функции — берём из caller-scope если есть.
    # Если функция вызвана без request (внутренний flow) — fallback на simple.
    _req = locals().get("request") or globals().get("_current_request")
    payment_scheme = "simple"
    if _req is not None and hasattr(_req, "POST"):
        payment_scheme = _req.POST.get("payment_scheme", "simple")
    if payment_scheme not in ("simple", "staged"):
        payment_scheme = "simple"

    mid_payment_amount = Decimal("0.00")
    customs_payment_amount = Decimal("0.00")
    if payment_scheme == "staged":
        mid_payment_amount = (grand_total * Decimal("0.50")).quantize(Decimal("0.01"))
        customs_payment_amount = (grand_total * Decimal("0.40")).quantize(Decimal("0.01"))

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
            payment_scheme=payment_scheme,
            mid_payment_amount=mid_payment_amount,
            customs_payment_amount=customs_payment_amount,
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
    active_role = getattr(user, "_assistant_active_role", None)
    if active_role:
        return active_role
    from assistant.permissions import detect_user_role
    return detect_user_role(user)






def _profile_for(user: User | None):
    if not user or not user.is_authenticated:
        return None
    return getattr(user, "profile", None)








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
    from assistant.security import safe_outbound_url, urlopen_no_redirect

    ok_url, _url_reason = safe_outbound_url(
        endpoint,
        allow_query=False,
    )
    if not ok_url:
        WebhookDeliveryLog.objects.create(
            order_event=event,
            order=event.order,
            endpoint=endpoint,
            success=False,
            attempt=attempt,
            request_payload=payload,
            error="Webhook endpoint was blocked by the security policy.",
        )
        return False

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
        # The target passed safe_outbound_url with a production allowlist.
        with urlopen_no_redirect(
            req,
            timeout=float(getattr(settings, "WEBHOOK_TIMEOUT_SEC", 2)),
            allow_private=bool(getattr(settings, "WEBHOOK_ALLOW_PRIVATE_IPS", False)),
        ) as resp:
            status_code = int(getattr(resp, "status", 200))
        is_ok = 200 <= status_code < 300
        log.success = is_ok
        log.status_code = status_code
        log.save(update_fields=["success", "status_code", "updated_at"])
        return is_ok
    except HTTPError as exc:
        log.error = "Remote endpoint returned an HTTP error."
        log.status_code = int(getattr(exc, "code", 0) or 0)
        log.save(update_fields=["error", "status_code", "updated_at"])
        return False
    except URLError:
        log.error = "Webhook transport failed."
        log.save(update_fields=["error", "updated_at"])
        return False
    except Exception:
        logger.exception(
            "Webhook delivery failed order_id=%s",
            event.order_id,
        )
        log.error = "Webhook delivery failed."
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










def _has_seller_permission(user: User, permission: str) -> bool:
    if user.is_superuser:
        return True
    profile = _profile_for(user)
    if not profile or _role_for(user) != "seller":
        return False
    return bool(getattr(profile, permission, False))


def _apply_seller_brand_scope(user: User, qs):
    profile = _profile_for(user)
    if not profile or _role_for(user) != "seller":
        return qs.none()
    allowed_brand_ids = list(profile.allowed_brands.values_list("id", flat=True))
    if allowed_brand_ids:
        return qs.filter(brand_id__in=allowed_brand_ids)
    return qs


def _seller_rfqs_qs(user: User):
    return (
        RFQ.objects.filter(items__matched_part__seller=user)
        .distinct()
        .select_related("created_by__profile")
        .prefetch_related("items__matched_part__brand", "items__matched_part__category")
        .order_by("-created_at")
    )


def _part_stale_snapshot(part: Part) -> dict[str, object]:
    updated_at = part.data_updated_at or part.updated_at or timezone.now()
    age_days = max(0, (timezone.now() - updated_at).days)
    if age_days > 180:
        state = "blocked"
        label = "Blocked"
    elif age_days > 90:
        state = "limited"
        label = "Limited"
    else:
        state = "fresh"
        label = "Fresh"
    return {
        "days": age_days,
        "state": state,
        "label": label,
        "is_stale": age_days > 90,
    }


def _part_price_history(part: Part) -> list[dict[str, object]]:
    points: list[dict[str, object]] = []
    order_items = (
        OrderItem.objects.filter(part=part)
        .select_related("order")
        .order_by("order__created_at")[:24]
    )
    for item in order_items:
        points.append(
            {
                "date": item.order.created_at,
                "price": item.unit_price,
                "source": f"order#{item.order_id}",
            }
        )
    current_point = {
        "date": part.data_updated_at or part.updated_at or timezone.now(),
        "price": part.price,
        "source": "current_catalog",
    }
    if not points or points[-1]["price"] != current_point["price"]:
        points.append(current_point)
    return points


def _part_demand_stats(part: Part) -> dict[str, int]:
    rfq_items = RFQItem.objects.filter(matched_part=part)
    order_items = OrderItem.objects.filter(part=part).select_related("order")
    return {
        "rfq_count": rfq_items.count(),
        "quoted_count": rfq_items.exclude(rfq__status="new").count(),
        "orders_count": order_items.values("order_id").distinct().count(),
        "ordered_units": order_items.aggregate(total=Sum("quantity"))["total"] or 0,
        "delivered_orders": order_items.filter(order__status__in=["delivered", "completed"]).values("order_id").distinct().count(),
    }






























def _landing_context() -> dict:
    """Контекст лендинга. B-09: promo-баннер с датой окончания берётся
    отсюда, а не хардкодится в шаблоне."""
    from datetime import date
    expires_at = date(2026, 6, 21)
    return {
        "promo": {
            "active": date.today() <= expires_at,
            "expires_at": expires_at,
        },
        # Лендинг видят анонимы: залогиненных home() редиректит в /chat/.
        # Поэтому персонального счётчика заказов здесь нет. 0 скрывает бейдж.
        "nav_badge": 0,
    }


def home(request: HttpRequest) -> HttpResponse:
    """Главная: новый landing 11site-v3 с большой формой поиска.
    Для НЕ-залогиненных — показываем landing.
    Для залогиненных — сразу в /chat/ (chat-first единственный UI).
    """
    if request.user.is_authenticated:
        from django.shortcuts import redirect
        return redirect("/chat/")
    return render(request, "landing.html", _landing_context())


def landing_view(request: HttpRequest) -> HttpResponse:
    """Главная-маркетинг (/landing/) — показывается ВСЕМ, включая залогиненных.

    Это явный маршрут «вернуться на главную»: на него ведёт логотип в шапке чата.
    В отличие от home (/), который для залогиненного редиректит в /chat/ (вход в
    приложение = chat-first), здесь редиректа нет — продавец/покупатель может
    открыть лендинг и оттуда уйти куда нужно. Все CTA лендинга и так ведут в /chat/.
    """
    return render(request, "landing.html", _landing_context())


def _legal_context(page_key: str) -> dict:
    from django.conf import settings

    return {
        "page_key": page_key,
        "legal_updated_at": "08.08.2026",
        "personal_consent_version": "PD-2026-08-08",
        "operator": {
            "name": getattr(settings, "PLATFORM_LEGAL_NAME", "")
            or "Innovation Idea FZ-LLC",
            "address": getattr(settings, "PLATFORM_LEGAL_ADDRESS", "")
            or (
                "Compass Building, Al Shohada Road, AL Hamra Industrial "
                "Zone-FZ, Ras Al Khaimah, 10055, United Arab Emirates"
            ),
            "tax_id": getattr(settings, "PLATFORM_TAX_ID", "") or "104683265300001",
            "registration_no": getattr(settings, "PLATFORM_REGISTRATION_NO", "")
            or "5022051",
            "email": getattr(settings, "PLATFORM_PAYMENT_CONTACT_EMAIL", "")
            or getattr(settings, "DEFAULT_FROM_EMAIL", "")
            or "contact@innovationidea.ae",
        },
    }


def terms_view(request: HttpRequest) -> HttpResponse:
    return render(request, "marketplace/legal.html", _legal_context("terms"))


def privacy_view(request: HttpRequest) -> HttpResponse:
    return render(request, "marketplace/legal.html", _legal_context("privacy"))


def cookies_view(request: HttpRequest) -> HttpResponse:
    return render(request, "marketplace/legal.html", _legal_context("cookies"))


def personal_data_consent_view(request: HttpRequest) -> HttpResponse:
    return render(request, "marketplace/legal.html", _legal_context("consent"))


def help_center_view(request: HttpRequest) -> HttpResponse:
    """Публичный /help/ — SEO-страница с FAQ + Schema.org FAQPage.

    Read-only витрина KnowledgeBaseEntry. Без auth — индексируется Google.
    Поддержка:
      • ?q=...  — поиск по вопросу/ответу
      • ?cat=...  — фильтр по категории
    """
    import json as _json

    from django.utils.html import escape

    from marketplace.models import KnowledgeBaseEntry
    from assistant.support_hub import _CATEGORY_LABEL  # noqa
    from assistant.kb_markdown import render_kb_markdown

    query = (request.GET.get("q") or "").strip()
    active_category = (request.GET.get("cat") or "").strip() or None

    qs = KnowledgeBaseEntry.objects.filter(is_active=True)
    if active_category:
        qs = qs.filter(category=active_category)
    if query:
        qs = KnowledgeBaseEntry.search(query, limit=200)
        if active_category:
            qs = qs.filter(category=active_category)

    entries = list(qs)

    # Category navigation (со счётчиками — только active entries)
    from django.db.models import Count
    cat_counts = dict(
        KnowledgeBaseEntry.objects.filter(is_active=True)
        .values_list("category")
        .annotate(n=Count("id"))
        .values_list("category", "n")
    )
    categories_nav = [
        (slug, _CATEGORY_LABEL.get(slug, slug), cnt)
        for slug, cnt in sorted(cat_counts.items(), key=lambda x: -x[1])
    ]

    # Группировка для рендера (по категории)
    by_cat = {}
    for e in entries:
        label = _CATEGORY_LABEL.get(e.category, "❓ Другое")
        by_cat.setdefault(label, []).append({
            "question": e.question,
            "answer":   e.answer,
            "answer_html": render_kb_markdown(e.answer),
        })
    entries_by_cat = list(by_cat.items())

    # Schema.org FAQPage JSON-LD (rich-snippet в Google)
    schema = {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [
            {
                "@type": "Question",
                "name": e.question,
                "acceptedAnswer": {"@type": "Answer", "text": e.answer},
            }
            for e in entries
        ],
    }
    # meta-description: ёмкая фраза из первых 2 вопросов
    md = (
        f"{entries[0].question} · {entries[1].question}"
        if len(entries) >= 2 else
        "Справочник Consolidator Parts: 16+ ответов по платформе."
    )[:300]

    return render(request, "marketplace/help.html", {
        "entries": entries,
        "entries_by_cat": entries_by_cat,
        "categories_nav": categories_nav,
        "active_query": query,
        "active_category": active_category,
        "schema_json": (
            _json.dumps(schema, ensure_ascii=False)
            .replace("&", "\\u0026")
            .replace("<", "\\u003C")
            .replace(">", "\\u003E")
        ),
        "meta_description": escape(md),
    })




# ─── Rate limiting helpers ───────────────────────────────────────────────────

def _client_ip(request: HttpRequest) -> str:
    from assistant.security import client_ip

    return client_ip(request)






def _rl_reset(request: HttpRequest, prefix: str) -> None:
    cache.delete(f"rl:{prefix}:{_client_ip(request)}")


def _rl_consume(
    request: HttpRequest,
    prefix: str,
    max_hits: int,
    window: int,
    *,
    identity: str | None = None,
) -> bool:
    """Atomically consume one attempt and return whether it is allowed."""
    subject = identity or _client_ip(request)
    key = f"rl:{prefix}:{subject}"
    if cache.add(key, 1, window):
        return True
    try:
        return cache.incr(key) <= max_hits
    except ValueError:
        cache.set(key, 1, window)
        return True


# Rate-limited subclass of PasswordResetView (Django stock view игнорирует наш
# limiter, поэтому оборачиваем). Лимит действует по IP и по хэшу email.
from django.contrib.auth.views import PasswordResetView as _DjangoPwReset


class RateLimitedPasswordResetView(_DjangoPwReset):
    def post(self, request, *args, **kwargs):
        email = (request.POST.get("email") or "").strip().lower()
        email_digest = hashlib.sha256(email.encode("utf-8")).hexdigest()
        ip_allowed = _rl_consume(
            request,
            "password_reset",
            3,
            3600,
        )
        email_allowed = _rl_consume(
            request,
            "password_reset_email",
            3,
            3600,
            identity=email_digest,
        )
        if not ip_allowed or not email_allowed:
            messages.error(request,
                "Слишком много запросов на восстановление пароля. Попробуйте через час.")
            return self.get(request, *args, **kwargs)
        return super().post(request, *args, **kwargs)


# ─── Email verification helpers ──────────────────────────────────────────────

_EMAIL_VERIFY_SALT = "consolidator-email-verify-v1"
_EMAIL_VERIFY_MAX_AGE = 86400  # 24 h


def _make_verify_token(user_id: int, email: str) -> str:
    return signing.dumps({"uid": user_id, "email": email}, salt=_EMAIL_VERIFY_SALT)


def _decode_verify_token(token: str):
    """Return (uid, email) or raise signing.BadSignature / signing.SignatureExpired."""
    data = signing.loads(token, salt=_EMAIL_VERIFY_SALT, max_age=_EMAIL_VERIFY_MAX_AGE)
    return data["uid"], data["email"]


def _send_verification_email(request: HttpRequest, user: User) -> bool:
    token = _make_verify_token(user.id, user.email)
    site_url = (getattr(settings, "SITE_URL", "") or "").strip().rstrip("/")
    verify_path = f"/verify-email/{token}/"
    verify_url = f"{site_url}{verify_path}" if site_url else request.build_absolute_uri(verify_path)
    subject = "Подтвердите email — Consolidator Parts"
    body = (
        f"Здравствуйте, {user.first_name or user.username}!\n\n"
        f"Для завершения регистрации перейдите по ссылке:\n{verify_url}\n\n"
        f"Ссылка действительна 24 часа.\n\n"
        f"Если вы не регистрировались на Consolidator Parts — просто игнорируйте это письмо."
    )
    try:
        send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [user.email], fail_silently=False)
        return True
    except Exception:
        logger.exception("registration verification email failed for user_id=%s", user.id)
        return False


def register_view(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        # Rate limit: 5 registrations per hour per IP
        if not _rl_consume(request, "register", 5, 3600):
            messages.error(request, "Слишком много попыток регистрации. Попробуйте через час.")
            return redirect("/chat/?action=start_registration")

        form = RegisterForm(request.POST)
        if form.is_valid():
            user = form.save(commit=False)
            user.email = form.cleaned_data["email"]
            user.first_name = form.cleaned_data["first_name"]
            user.last_name = form.cleaned_data["last_name"]

            email_verification_required = bool(
                getattr(settings, "EMAIL_VERIFICATION_REQUIRED", not settings.DEBUG)
            )
            if email_verification_required:
                user.is_active = False

            with transaction.atomic():
                user.save()
                UserProfile.objects.create(
                    user=user,
                    role=form.cleaned_data["role"],
                    company_name=form.cleaned_data["company_name"],
                    language=form.cleaned_data.get("language") or "ru",
                )
            # Активируем выбранный язык сразу
            try:
                request.session[settings.LANGUAGE_COOKIE_NAME or "django_language"] = form.cleaned_data.get("language") or "ru"
            except Exception:
                pass
            if email_verification_required:
                delivered = _send_verification_email(request, user)
                if not delivered:
                    messages.error(
                        request,
                        "Аккаунт создан, но письмо подтверждения не отправлено. "
                        "Обратитесь в поддержку.",
                    )
                return render(
                    request,
                    "marketplace/email_verification_sent.html",
                    {"email": user.email, "delivery_failed": not delivered},
                )

            login(request, user, backend="django.contrib.auth.backends.ModelBackend")
            messages.success(request, "Регистрация завершена.")
            return redirect("dashboard")
    # GET /register/ → редирект в chat-native регистрацию.
    # POST /register/ оставлен для backward-compat (старые формы) — после
    # успеха редиректит на /chat/. На самой странице форму больше не рендерим.
    nxt = request.GET.get("action") or ""
    role = request.GET.get("role") or ""
    target = "/chat/?action=start_registration"
    if role:
        target += f"&role={role}"
    if nxt == "login":
        target = "/chat/?action=start_login"
    return redirect(target)


def verify_email_view(request: HttpRequest, token: str) -> HttpResponse:
    try:
        uid, email = _decode_verify_token(token)
    except signing.SignatureExpired:
        messages.error(request, "Ссылка подтверждения устарела (24 ч). Зарегистрируйтесь заново.")
        return redirect("register")
    except Exception:
        messages.error(request, "Недействительная ссылка подтверждения.")
        return redirect("register")

    user = User.objects.filter(id=uid, email=email, is_active=False).first()
    if not user:
        # Already activated or doesn't exist
        messages.info(request, "Email уже подтверждён или аккаунт не найден.")
        return redirect("login")

    user.is_active = True
    user.save(update_fields=["is_active"])
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    messages.success(request, "Email подтверждён! Добро пожаловать в Consolidator Parts.")
    return redirect("dashboard")


def login_view(request: HttpRequest) -> HttpResponse:
    """Login: отдельной страницы /login/ больше нет — chat-native.

    GET /login/  → 302 на /chat/?action=start_login (форма прямо в чате).
    POST /login/ → авторизация. При успехе → /chat/, при ошибке —
                   тоже на /chat/ (юзер увидит ошибку в чат-форме).
    """
    if request.method == "GET":
        nxt = _safe_next_url(request, "")
        url = "/chat/?action=start_login"
        if nxt:
            from urllib.parse import quote
            url += f"&next={quote(nxt)}"
        return redirect(url)

    # POST: тот же flow, но в случае ошибок редиректим обратно в чат.
    if not _rl_consume(request, "login", 10, 600):
        messages.error(request, "Слишком много попыток входа. Подождите 10 минут.")
        return redirect("/chat/?action=start_login")

    data = request.POST.copy()
    raw_login = data.get("username", "").strip()
    if "@" in raw_login:
        user = User.objects.filter(email__iexact=raw_login).first()
        if user:
            data["username"] = user.username
    form = LoginForm(request, data=data)
    if form.is_valid():
        user = form.get_user()
        try:
            from assistant.security import user_has_enabled_2fa, verify_user_2fa
            if user_has_enabled_2fa(user) and not verify_user_2fa(user, request.POST.get("otp_code") or ""):
                messages.error(request, "Для аккаунта включена 2FA. Введите одноразовый код.")
                return redirect("/chat/?action=start_login")
        except Exception:
            logger.exception("2FA check failed on login")
            messages.error(request, "Не удалось проверить 2FA. Попробуйте позже.")
            return redirect("/chat/?action=start_login")
        _rl_reset(request, "login")
        login(request, user, backend="django.contrib.auth.backends.ModelBackend")
        next_url = _safe_next_url(request, "")
        if next_url:
            return redirect(next_url)
        return redirect("/chat/")
    # На ошибке: messages + редирект в чат (форма откроется снова).
    messages.error(request, "Неверный логин или пароль.")
    return redirect("/chat/?action=start_login")


@require_POST
def logout_view(request: HttpRequest) -> HttpResponse:
    # Выход меняет состояние сессии → только POST с CSRF-токеном.
    # GET-ссылка позволяла бы внешнему переходу принудительно разлогинить.
    logout(request)
    messages.info(request, "Вы вышли из системы.")
    return redirect("home")


























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
            safe_spreadsheet_row([
                order.id,
                order.status,
                order.payment_status,
                order.sla_status,
                order.total_amount,
                order.logistics_cost,
                order.created_at.isoformat(),
            ])
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
            safe_spreadsheet_row([
                claim.id,
                claim.order_id,
                claim.status,
                claim.title,
                claim.opened_by.username if claim.opened_by else "",
                claim.resolved_by.username if claim.resolved_by else "",
                claim.created_at.isoformat(),
                claim.updated_at.isoformat(),
            ])
        )
    return response



































# ═══════════════════════════════════════════════════════════════════
# BUYER CABINET
# ═══════════════════════════════════════════════════════════════════


























@require_POST
def set_language_api(request: HttpRequest) -> JsonResponse:
    """
    Сохраняет выбор языка пользователя:
    — в UserProfile.language (если залогинен)
    — в cookie django_language (всегда), чтобы LocaleMiddleware подхватил при reload
    """
    import json as _json
    try:
        payload = _json.loads(request.body or "{}")
    except Exception:
        return JsonResponse({"ok": False, "error": "invalid json"}, status=400)

    lang = (payload.get("language") or "").strip().lower()
    allowed = {code for code, _label in settings.LANGUAGES}
    if lang not in allowed:
        return JsonResponse({"ok": False, "error": "unsupported language"}, status=400)

    # Сохраняем в профиле
    if request.user.is_authenticated:
        try:
            profile = request.user.profile
            profile.language = lang
            profile.save(update_fields=["language"])
        except Exception:
            pass

    resp = JsonResponse({"ok": True, "language": lang})
    # Cookie на год; имя django_language — стандартное для Django i18n
    resp.set_cookie(
        settings.LANGUAGE_COOKIE_NAME if hasattr(settings, "LANGUAGE_COOKIE_NAME") else "django_language",
        lang,
        max_age=60 * 60 * 24 * 365,
        path="/",
        samesite="Lax",
    )
    return resp


















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




















































































@csrf_exempt
@require_POST
@transaction.atomic
def payment_callback(request: HttpRequest) -> HttpResponse:
    import hmac

    configured_secret = (getattr(settings, "PAYMENT_CALLBACK_SECRET", "") or "").strip()
    if not configured_secret:
        return JsonResponse(
            {"ok": False, "error": "PAYMENT_CALLBACK_SECRET not configured"},
            status=503,
        )
    provided_secret = (request.headers.get("X-Payment-Secret") or "").strip()
    if not hmac.compare_digest(configured_secret, provided_secret):
        return JsonResponse({"ok": False, "error": "invalid_secret"}, status=403)

    max_body_bytes = int(
        getattr(settings, "PAYMENT_CALLBACK_MAX_BODY_BYTES", 64 * 1024)
    )
    try:
        content_length = int(request.META.get("CONTENT_LENGTH") or 0)
    except (TypeError, ValueError):
        content_length = 0
    if content_length > max_body_bytes:
        return JsonResponse(
            {"ok": False, "error": "payload_too_large"},
            status=413,
        )

    payload: dict = {}
    is_json = bool(
        request.content_type
        and "application/json" in request.content_type.lower()
    )
    if is_json:
        try:
            raw_body = request.body
        except RequestDataTooBig:
            return JsonResponse(
                {"ok": False, "error": "payload_too_large"},
                status=413,
            )
        if len(raw_body) > max_body_bytes:
            return JsonResponse(
                {"ok": False, "error": "payload_too_large"},
                status=413,
            )
        try:
            payload = json.loads(raw_body.decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JsonResponse(
                {"ok": False, "error": "invalid_json"},
                status=400,
            )
    if not is_json:
        try:
            payload = request.POST.dict()
        except RequestDataTooBig:
            return JsonResponse(
                {"ok": False, "error": "payload_too_large"},
                status=413,
            )
    if not isinstance(payload, dict):
        return JsonResponse(
            {"ok": False, "error": "invalid_payload"},
            status=400,
        )

    def callback_text(*names, max_length):
        for name in names:
            value = payload.get(name)
            if isinstance(value, (str, int, float, Decimal)):
                return str(value).strip()[:max_length]
        return ""

    order_id_raw = callback_text("order_id", "orderId", max_length=32)
    invoice_number = callback_text(
        "invoice_number",
        "invoice",
        max_length=80,
    )
    callback_status = callback_text(
        "status",
        "payment_status",
        max_length=40,
    ).lower()
    transaction_id = callback_text(
        "transaction_id",
        "tx_id",
        max_length=200,
    )

    order = None
    if order_id_raw:
        try:
            order = Order.objects.select_for_update().filter(id=int(order_id_raw)).first()
        except Exception:
            order = None
    if not order and invoice_number:
        order = (
            Order.objects.select_for_update()
            .filter(invoice_number=invoice_number)
            .first()
        )
    if not order:
        return JsonResponse({"ok": False, "error": "order_not_found"}, status=404)
    if invoice_number and order.invoice_number != invoice_number:
        return JsonResponse(
            {"ok": False, "error": "order_invoice_mismatch"},
            status=400,
        )
    if not transaction_id:
        return JsonResponse(
            {"ok": False, "error": "transaction_id_required"},
            status=400,
        )
    if not order.invoice_number or not invoice_number:
        return JsonResponse(
            {"ok": False, "error": "invoice_number_required"},
            status=400,
        )

    status_aliases = {
        "reserve_paid": "reserve_paid",
        "reserve_success": "reserve_paid",
        "deposit_paid": "reserve_paid",
        "mid_paid": "mid_paid",
        "mid_payment": "mid_paid",
        "confirmation_paid": "mid_paid",
        "customs_paid": "customs_paid",
        "customs_payment": "customs_paid",
        "paid": "paid",
        "success": "paid",
        "final_paid": "paid",
        "full_paid": "paid",
        "refunded": "refunded",
        "refund": "refunded",
    }
    target_status = status_aliases.get(callback_status)
    if not target_status:
        return JsonResponse(
            {
                "ok": False,
                "error": "unsupported_status",
                "status": callback_status,
            },
            status=400,
        )

    amount_raw = callback_text("amount", max_length=40)
    currency = callback_text("currency", max_length=10).upper()
    if not amount_raw or not currency:
        return JsonResponse(
            {"ok": False, "error": "amount_and_currency_required"},
            status=400,
        )
    if currency and currency != (settings.PAYMENT_CURRENCY or "USD").upper():
        return JsonResponse(
            {"ok": False, "error": "currency_mismatch"},
            status=400,
        )
    if amount_raw:
        try:
            callback_amount = Decimal(amount_raw).quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError):
            return JsonResponse(
                {"ok": False, "error": "invalid_amount"},
                status=400,
            )
        reserve_amount = (
            order.reserve_amount
            or (
                Decimal(order.total_amount or 0)
                * Decimal(order.reserve_percent or 0)
                / Decimal("100")
            ).quantize(Decimal("0.01"))
        )
        expected_amounts = {
            "reserve_paid": Decimal(reserve_amount),
            "mid_paid": Decimal(order.mid_payment_amount or 0),
            "customs_paid": Decimal(order.customs_payment_amount or 0),
            "paid": max(
                Decimal("0.00"),
                Decimal(order.total_amount or 0)
                - Decimal(reserve_amount)
                - (
                    Decimal(order.mid_payment_amount or 0)
                    + Decimal(order.customs_payment_amount or 0)
                    if order.payment_scheme == "staged"
                    else Decimal("0.00")
                ),
            ),
            "refunded": Decimal(order.total_amount or 0),
        }
        expected_amount = expected_amounts[target_status].quantize(
            Decimal("0.01")
        )
        if callback_amount != expected_amount:
            return JsonResponse(
                {
                    "ok": False,
                    "error": "amount_mismatch",
                },
                status=400,
            )

    meta = {
        "callback_status": callback_status,
        "target_status": target_status,
        "transaction_id": transaction_id,
        "invoice_number": order.invoice_number,
        "amount": amount_raw,
        "currency": currency,
    }
    previous_transaction = OrderEvent.objects.filter(
        meta__transaction_id=transaction_id,
    ).only("order_id").first() if transaction_id else None
    if previous_transaction:
        if previous_transaction.order_id != order.id:
            return JsonResponse(
                {"ok": False, "error": "transaction_id_reused"},
                status=409,
            )
        return JsonResponse(
            {
                "ok": True,
                "order_id": order.id,
                "payment_status": order.payment_status,
                "idempotent_replay": True,
            }
        )
    if order.payment_status == target_status:
        return JsonResponse(
            {
                "ok": True,
                "order_id": order.id,
                "payment_status": order.payment_status,
                "idempotent_replay": True,
            }
        )

    allowed_current = {
        "reserve_paid": {"awaiting_reserve", "pending"},
        "mid_paid": {"reserve_paid"} if order.payment_scheme == "staged" else set(),
        "customs_paid": {"mid_paid"} if order.payment_scheme == "staged" else set(),
        "paid": (
            {"customs_paid"}
            if order.payment_scheme == "staged"
            else {"reserve_paid"}
        ),
        "refunded": {"paid", "refund_pending"},
    }
    if order.status == "cancelled" and target_status != "refunded":
        return JsonResponse(
            {"ok": False, "error": "cancelled_order"},
            status=409,
        )
    if order.payment_status not in allowed_current[target_status]:
        return JsonResponse(
            {
                "ok": False,
                "error": "invalid_payment_transition",
                "from": order.payment_status,
                "to": target_status,
            },
            status=409,
        )

    now = timezone.now()
    changed_fields = ["payment_status"]
    order.payment_status = target_status
    event_type = "status_changed"
    if target_status == "reserve_paid":
        order.reserve_paid_at = now
        changed_fields.append("reserve_paid_at")
        event_type = "reserve_paid"
        if order.status == "pending":
            prev_status = order.status
            order.status = "reserve_paid"
            changed_fields.append("status")
            _log_order_event(
                order,
                "status_changed",
                source="system",
                meta={"from": prev_status, "to": order.status, **meta},
            )
    elif target_status == "mid_paid":
        order.mid_paid_at = now
        changed_fields.append("mid_paid_at")
        event_type = "mid_payment_paid"
    elif target_status == "customs_paid":
        order.customs_paid_at = now
        changed_fields.append("customs_paid_at")
        event_type = "customs_payment_paid"
    elif target_status == "paid":
        order.final_paid_at = now
        changed_fields.append("final_paid_at")
        event_type = "final_payment_paid"

    order.save(update_fields=changed_fields)
    _log_order_event(order, event_type, source="system", meta=meta)

    return JsonResponse({"ok": True, "order_id": order.id, "payment_status": order.payment_status})






# ═══ Operator cabinet views ═══


















































# ═══ Admin panel ═══









































# ── Notifications API ──────────────────────────────────────
from .models import Notification


def _safe_local_notification_url(value) -> str:
    url = str(value or "").strip()
    if not url.startswith("/") or url.startswith("//"):
        return ""
    if not url_has_allowed_host_and_scheme(url, allowed_hosts=set()):
        return ""
    return url


@login_required
def notifications_list(request):
    """JSON API for notification dropdown."""
    qs = Notification.objects.filter(user=request.user)[:30]
    items = [{
        "id": n.id, "kind": n.kind, "title": n.title, "body": n.body[:120],
        "url": _safe_local_notification_url(n.url), "is_read": n.is_read,
        "created_at": n.created_at.strftime("%d.%m %H:%M"),
    } for n in qs]
    unread = Notification.objects.filter(user=request.user, is_read=False).count()
    return JsonResponse({"items": items, "unread": unread})


@login_required
@require_POST
def notifications_mark_read(request, notif_id=None):
    if notif_id:
        Notification.objects.filter(user=request.user, id=notif_id).update(is_read=True)
    else:
        Notification.objects.filter(user=request.user, is_read=False).update(is_read=True)
    return JsonResponse({"ok": True})




# ── KYB Verification ───────────────────────────────────────


# help_view — alias на публичный help_center_view (без login, для SEO).
# Раньше требовался login и рендерил пустую help.html — теперь нормальный
# FAQ-центр с Schema.org FAQPage rich-snippet.
help_view = help_center_view


# ── 2FA (TOTP) ─────────────────────────────────────────────






def chat_first_view(request):
    """Chat-First single-page UI — открыт для anonymous (guest buyer-mode).

    Стратегия: anonymous посетитель сразу видит чат в режиме покупателя.
    Может искать запчасти, смотреть каталог, общаться с AI. Когда дело
    доходит до mutating-действий (создать RFQ, заказать, оплатить) —
    backend возвращает gating-card «Зарегистрируйтесь, чтобы продолжить».
    """
    # Allowlist действий по ролям → фронт фильтрует каталог пилюль, чтобы не
    # предлагать пилюли, недоступные роли (клик по ним всё равно дал бы «нет прав»).
    role_actions_data = {}
    try:
        from assistant.actions import ROLE_ACTIONS
        role_actions_data = {r: list(a) for r, a in ROLE_ACTIONS.items()}
    except Exception:
        pass
    role_commands_data = {}
    try:
        from assistant.commands import commands_for_all_roles
        role_commands_data = commands_for_all_roles()
    except Exception:
        pass
    ctx = {
        "role_actions_data": role_actions_data,
        "role_commands_data": role_commands_data,
    }
    # Анти-мелькание ТОЛЬКО для залогиненного: серверно отдаём его роль/идентичность/
    # welcome, чтобы первый кадр при F5 был уже его кабинетом, а не дефолтным (buyer)
    # экраном. На проде 600КБ chat-first.js + widget-config грузятся ~1-2с — всё это
    # время иначе виден «гостевой/покупательский» welcome (на локалке незаметно).
    # Для АНОНИМА ctx остаётся пустым → шаблон рендерит исходный гостевой вид
    # (ветки {% else %} / default-фильтры) — ровно как было, ничего лишнего.
    if request.user.is_authenticated:
        try:
            from assistant.permissions import (
                detect_user_role,
                display_role_label,
                user_allowed_role_tabs,
            )
            initial_role = detect_user_role(request.user, request=request) or "buyer"
            role_tabs = user_allowed_role_tabs(request.user)
            initial_role_label = display_role_label(initial_role)
        except Exception:
            initial_role = "buyer"
            role_tabs = [{"role": "buyer", "label": "Покупатель"}]
            initial_role_label = "Покупатель"
        base_role = (
            "admin" if initial_role == "admin"
            else "operator" if initial_role.startswith("operator")
            else "seller" if initial_role == "seller"
            else "buyer"
        )
        # Дублируем ru-строки welcome только для первого кадра; дальше JS-i18n синхронит.
        _WELCOME = {
            "buyer": ("welcome.buyer.title", "Какую запчасть найти?"),
            "seller": ("welcome.seller.title", "Что в работе сегодня?"),
            "operator": ("welcome.operator.title", "Что в работе на платформе?"),
            "admin": ("welcome.admin.title", "Управление платформой"),
        }
        _SUB = {
            "buyer": "Загрузите спецификацию в Excel, перетащите фото детали или опишите словами — соберу и сравню предложения поставщиков.",
            "seller": "Срочные задачи, входящие RFQ и отгрузки. Каталог, финансы и команда — по запросу.",
            "operator": "Вы управляете всей сделкой: ведёте заказ от оплаты до доставки, координируете логистов, таможенных брокеров и контролируете платежи.",
            "admin": "Контролируйте пользователей, каталог, модерацию и показатели платформы из одного рабочего окна.",
        }
        _wt_key, _wt = _WELCOME[base_role]
        uname = (request.user.get_full_name() or request.user.username or "").strip()
        # Команды роли приходят из единого серверного реестра. Тот же набор
        # используется в первом HTML-кадре, welcome-экране и меню команд.
        import json as _json2
        _e = _json2.dumps  # короткий алиас для params → JSON
        from assistant.commands import commands_for_role
        auth_pills = [
            {
                "emoji": command["icon"],
                "label": command["label"],
                "action": command["action"],
                "params": _e(command["params"]),
            }
            for command in commands_for_role(initial_role)
        ]
        ctx.update({
            "auth_role": initial_role,
            "auth_base_role": base_role,
            "auth_role_tabs": role_tabs,
            "auth_welcome_title_key": _wt_key,
            "auth_welcome_title": _wt,
            "auth_welcome_subtitle": _SUB[base_role],
            "auth_user_name": uname,
            "auth_user_initial": (uname[:1].upper() if uname else ""),
            "auth_user_role_label": initial_role_label,
            "auth_pills": auth_pills,
        })
    resp = render(request, "chat/index.html", ctx)
    # Auth-зависимый HTML (шапка/роль/идентичность) НЕ кэшируем — ни CF, ни браузер,
    # ни bfcache — иначе залогиненному может отдаться гостевой кадр. Статика (JS) при
    # этом кэшируется отдельно по хэшу имени, так что no-store на лёгком HTML дёшев.
    resp["Cache-Control"] = "private, no-store"
    return resp


def invite_redirect(request, code):
    """Короткая реф-ссылка /i/<code>/ → /chat/?ref=<code>.

    Фронт (autoTriggerFromUrl) применит реферал. Редирект всегда на ВНУТРЕННИЙ
    относительный путь (не open-redirect). Код жёстко санируем по алфавиту
    реф-кода (ASCII, верхний регистр) — не протаскиваем ничего лишнего в query.
    """
    from marketplace.models import ReferralCode
    allowed = set(ReferralCode.ALPHABET)
    safe = "".join(ch for ch in (code or "").upper() if ch in allowed)[:16]
    if safe:
        ReferralCode.objects.filter(code=safe).update(
            clicks=F("clicks") + 1,
            last_clicked_at=timezone.now(),
        )
    return redirect(f"/chat/?ref={safe}")


@login_required
def chat_project_view(request, project_id):
    """Project detail page within chat-first layout."""
    from assistant.permissions import detect_user_role, user_allowed_role_tabs

    active_role = detect_user_role(request.user, request=request)
    active_role_tab = (
        "operator" if active_role.startswith("operator")
        else active_role
    )
    response = render(request, "chat/project.html", {
        "project_id": project_id,
        "active_role": active_role_tab,
        "role_tabs": user_allowed_role_tabs(request.user),
    })
    # Anti-cache: страница активно меняется (тема/уведомления/JS-bundle)
    response["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response["Pragma"] = "no-cache"
    response["Expires"] = "0"
    return response




@login_required
def chat_rfq_view(request, rfq_id):
    """RFQ detail — теперь редирект в chat-first inline (action=get_rfq_status).
    Отдельный standalone-экран /chat/rfq/<id>/ упразднён: дублировал данные
    из chat-first карточки, имел свои проблемы со стилями/i18n/темой и уводил
    юзера из контекста диалога. Все deep-link'и (email/WS/notifications/тесты)
    продолжают работать через 301 редирект.
    """
    from django.shortcuts import redirect
    return redirect(
        f"/chat/?new=1&run=get_rfq_status&rfq_id={rfq_id}",
        permanent=True,
    )

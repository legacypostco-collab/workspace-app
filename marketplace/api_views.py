from datetime import timedelta
from decimal import Decimal
import hmac

from django.conf import settings
from django.db import connection, transaction
from django.db.models import Count, DecimalField, F, Q, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from drf_spectacular.utils import extend_schema
from drf_spectacular.types import OpenApiTypes

from .models import (
    RFQ,
    Category,
    NewsletterSubscriber,
    Order,
    OrderClaim,
    OrderEvent,
    OrderItem,
    Part,
    RFQItem,
    WebhookDeliveryLog,
)
from .participant_identity import (
    customer_label,
    public_party_code,
    redact_party_contacts,
    redact_party_payload,
)
from .serializers import CategorySerializer, OrderSerializer, PartSerializer
from .order_access import (
    seller_can_access_claim,
    seller_company_user_ids,
    seller_ids_for_order,
    seller_principal,
    seller_visible_claims,
    seller_visible_documents,
    seller_visible_events,
)
from .views import (
    ORDER_TRANSITIONS,
    _apply_seller_brand_scope,
    _eligible_parts_qs,
    _has_seller_permission,
    _log_order_event,
    _part_demand_stats,
    _part_price_history,
    _part_stale_snapshot,
    _recalc_order_sla,
    _role_for,
    _seller_rfqs_qs,
)


class LookupThrottle(ScopedRateThrottle):
    scope = "lookup"


def _seller_api_forbidden():
    return Response({"error": "seller role required"}, status=403)


def _refresh_seller_dashboard_projection(user):
    try:
        from dashboard.services import refresh_dashboard_projection_for_user

        refresh_dashboard_projection_for_user(user)
    except Exception:
        # Dashboard refresh should not break primary action.
        pass


def _seller_parts_queryset(user):
    seller = seller_principal(user)
    return _apply_seller_brand_scope(user, Part.objects.filter(seller=seller)).select_related("category", "brand")


def _serialize_seller_part(part: Part) -> dict:
    payload = PartSerializer(part).data
    payload["stale"] = _part_stale_snapshot(part)
    payload["demand"] = _part_demand_stats(part)
    return payload


def _seller_requests_queryset(user):
    return _seller_rfqs_qs(seller_principal(user))


def _serialize_seller_rfq(rfq: RFQ, seller_user) -> dict:
    seller_items = [item for item in rfq.items.all() if item.matched_part and item.matched_part.seller_id == seller_user.id]
    buyer_label = customer_label(rfq.created_by, fallback_id=rfq.id)
    return {
        "id": rfq.id,
        "customer_name": buyer_label,
        "customer_code": public_party_code(rfq.created_by, "buyer", fallback_id=rfq.id),
        "mode": rfq.mode,
        "urgency": rfq.urgency,
        "status": rfq.status,
        "created_at": rfq.created_at.isoformat(),
        "seller_items_count": len(seller_items),
        "total_quantity": sum(item.quantity for item in seller_items),
        "estimated_total": sum(item.estimated_line_total for item in seller_items),
        "items": [
            {
                "id": item.id,
                "query": redact_party_contacts(item.query),
                "quantity": item.quantity,
                "state": item.state,
                "confidence": item.confidence,
                "decision_reason": redact_party_contacts(item.decision_reason),
                "matched_part_id": item.matched_part_id,
                "matched_part_title": item.matched_part.title if item.matched_part else "",
                "matched_part_oem": item.matched_part.oem_number if item.matched_part else "",
            }
            for item in seller_items
        ],
    }


def _seller_orders_queryset(user):
    seller = seller_principal(user)
    return (
        Order.objects.filter(items__part__seller=seller)
        .distinct()
        .select_related("buyer__profile")
        .prefetch_related("items__part", "events", "documents", "claims")
        .order_by("-created_at")
    )


def _serialize_seller_order(order: Order, seller_user) -> dict:
    seller_user = seller_principal(seller_user)
    seller_items = [item for item in order.items.all() if item.part and item.part.seller_id == seller_user.id]
    seller_total = sum(
        (item.total_price for item in seller_items),
        Decimal("0.00"),
    )
    reserve_percent = Decimal(str(order.reserve_percent or 0))
    seller_reserve = (
        seller_total * reserve_percent / Decimal("100")
    ).quantize(Decimal("0.01"))
    visible_claims = seller_visible_claims(order, seller_user)
    open_claims = [
        claim
        for claim in visible_claims
        if claim.status in {"open", "in_review"}
    ]
    visible_documents = seller_visible_documents(order, seller_user)
    buyer_label = customer_label(order.buyer, fallback_id=order.id)
    return {
        "id": order.id,
        "customer_name": buyer_label,
        "customer_code": public_party_code(order.buyer, "buyer", fallback_id=order.id),
        "status": order.status,
        "payment_status": order.payment_status,
        "sla_status": order.sla_status,
        "sla_breaches_count": order.sla_breaches_count,
        "supplier_confirm_deadline": order.supplier_confirm_deadline.isoformat() if order.supplier_confirm_deadline else None,
        "ship_deadline": order.ship_deadline.isoformat() if order.ship_deadline else None,
        "invoice_number": order.invoice_number,
        "total_amount": str(seller_total),
        "seller_subtotal": str(seller_total),
        "reserve_amount": str(seller_reserve),
        "reserve_percent": str(order.reserve_percent),
        "created_at": order.created_at.isoformat(),
        "items_count": len(seller_items),
        "units_total": sum(int(item.quantity) for item in seller_items),
        "documents_count": visible_documents.count(),
        "open_claims_count": len(open_claims),
        "seller_items": [
            {
                "id": item.id,
                "part_id": item.part_id,
                "part_title": item.part.title if item.part else "",
                "part_oem": item.part.oem_number if item.part else "",
                "quantity": item.quantity,
                "unit_price": str(item.unit_price),
                "total_price": str(item.total_price),
            }
            for item in seller_items
        ],
    }


def _serialize_seller_order_event(event: OrderEvent, order: Order, seller_user) -> dict:
    seller_ids = seller_company_user_ids(seller_user)
    if event.actor_id == order.buyer_id:
        actor_name = customer_label(order.buyer, fallback_id=order.id)
    elif event.actor_id in seller_ids:
        actor_name = "Ваша команда"
    elif event.actor_id:
        actor_name = "Оператор платформы"
    else:
        actor_name = "Система"
    sensitive_meta_keys = {
        "actor_id", "buyer", "buyer_id", "seller", "seller_id", "username",
        "customer_name", "customer_email", "company_name", "email", "phone", "by",
    }
    safe_meta = {
        key: value
        for key, value in (event.meta or {}).items()
        if str(key).lower() not in sensitive_meta_keys
    }
    return {
        "id": event.id,
        "event_type": event.event_type,
        "source": event.source,
        "actor_name": actor_name,
        "meta": redact_party_payload(safe_meta),
        "created_at": event.created_at.isoformat(),
    }


def _serialize_seller_order_claim(claim: OrderClaim, seller_user) -> dict:
    seller_ids = seller_company_user_ids(seller_user)
    if claim.opened_by_id == claim.order.buyer_id:
        opened_by = customer_label(claim.order.buyer, fallback_id=claim.order_id)
    elif claim.opened_by_id in seller_ids:
        opened_by = "Ваша команда"
    else:
        opened_by = "Оператор платформы"
    return {
        "id": claim.id,
        "order_id": claim.order_id,
        "title": redact_party_contacts(claim.title),
        "description": redact_party_contacts(claim.description),
        "status": claim.status,
        "opened_by": opened_by,
        "created_at": claim.created_at.isoformat(),
        "updated_at": claim.updated_at.isoformat(),
    }


@extend_schema(responses=OpenApiTypes.OBJECT)
@api_view(["GET"])
@permission_classes([AllowAny])
@throttle_classes([LookupThrottle])
def api_categories(_request):
    categories = Category.objects.all().order_by("name")
    return Response({"items": CategorySerializer(categories, many=True).data})


@extend_schema(responses=OpenApiTypes.OBJECT)
@api_view(["GET"])
@permission_classes([AllowAny])
@throttle_classes([LookupThrottle])
def api_parts(request):
    qs = _eligible_parts_qs().select_related("category", "brand", "seller")
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(title__icontains=q) | Q(oem_number__icontains=q))
    return Response({"items": PartSerializer(qs.order_by("-created_at")[:200], many=True).data})


@extend_schema(responses=OpenApiTypes.OBJECT)
@api_view(["GET"])
@permission_classes([AllowAny])
@throttle_classes([LookupThrottle])
def api_part_detail(_request, part_id: int):
    part = get_object_or_404(
        _eligible_parts_qs().select_related("category", "brand"),
        id=part_id,
    )
    return Response(PartSerializer(part).data)


@extend_schema(responses=OpenApiTypes.OBJECT)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def api_my_orders(request):
    role = _role_for(request.user)
    if role == "seller":
        orders = _seller_orders_queryset(request.user)
        return Response({
            "items": [
                _serialize_seller_order(order, request.user)
                for order in orders[:100]
            ],
        })
    qs = (
        Order.objects.filter(buyer=request.user)
        .prefetch_related("items__part")
    )
    return Response({"items": OrderSerializer(qs[:100], many=True).data})


@extend_schema(responses=OpenApiTypes.OBJECT)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def api_seller_parts(request):
    if _role_for(request.user) != "seller":
        return _seller_api_forbidden()
    parts = _seller_parts_queryset(request.user)
    q = request.GET.get("q", "").strip()
    availability_status = request.GET.get("status", "").strip()
    stale = request.GET.get("stale", "").strip()
    if q:
        parts = parts.filter(Q(title__icontains=q) | Q(oem_number__icontains=q) | Q(brand__name__icontains=q) | Q(cross_numbers__icontains=q))
    if availability_status:
        parts = parts.filter(availability_status=availability_status)
    items = []
    for part in parts.order_by("-data_updated_at", "-id"):
        payload = _serialize_seller_part(part)
        if stale == "fresh" and payload["stale"]["state"] != "fresh":
            continue
        if stale == "limited" and payload["stale"]["state"] != "limited":
            continue
        if stale == "blocked" and payload["stale"]["state"] != "blocked":
            continue
        items.append(payload)
    return Response({"items": items})


@extend_schema(responses=OpenApiTypes.OBJECT)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def api_seller_part_detail(request, part_id: int):
    if _role_for(request.user) != "seller":
        return _seller_api_forbidden()
    part = get_object_or_404(_seller_parts_queryset(request.user), id=part_id)
    return Response(_serialize_seller_part(part))


@extend_schema(responses=OpenApiTypes.OBJECT)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def api_seller_part_price_history(request, part_id: int):
    if _role_for(request.user) != "seller":
        return _seller_api_forbidden()
    part = get_object_or_404(_seller_parts_queryset(request.user), id=part_id)
    history = _part_price_history(part)
    return Response(
        {
            "part_id": part.id,
            "current_price": part.price,
            "currency": part.currency,
            "items": [
                {
                    "date": point["date"].isoformat() if hasattr(point["date"], "isoformat") else str(point["date"]),
                    "price": point["price"],
                    "source": point["source"],
                }
                for point in history
            ],
        }
    )


@extend_schema(responses=OpenApiTypes.OBJECT)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def api_seller_part_demand(request, part_id: int):
    if _role_for(request.user) != "seller":
        return _seller_api_forbidden()
    part = get_object_or_404(_seller_parts_queryset(request.user), id=part_id)
    return Response({"part_id": part.id, **_part_demand_stats(part)})


@extend_schema(responses=OpenApiTypes.OBJECT)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def api_seller_product_bulk_update(request):
    if _role_for(request.user) != "seller":
        return _seller_api_forbidden()
    if not _has_seller_permission(request.user, "can_manage_assortment"):
        return Response({"error": "assortment permission required"}, status=403)

    action = (request.data.get("action") or "").strip()
    ids = request.data.get("part_ids") or request.data.get("product_ids") or []
    if not isinstance(ids, list):
        return Response({"error": "part_ids must be a list"}, status=400)
    if len(ids) > 500:
        return Response({"error": "too many part_ids; maximum is 500"}, status=413)
    try:
        selected_ids = list(dict.fromkeys(int(value) for value in ids))
    except (TypeError, ValueError):
        selected_ids = []
    if not selected_ids:
        return Response({"error": "part_ids required"}, status=400)

    qs = _seller_parts_queryset(request.user).filter(id__in=selected_ids)
    now = timezone.now()
    if action == "hide":
        updated = qs.update(is_active=False, data_updated_at=now)
    elif action == "unhide":
        updated = qs.update(is_active=True, data_updated_at=now)
    elif action == "status":
        status_value = (request.data.get("availability_status") or "").strip()
        allowed = {code for code, _ in Part.AVAILABILITY_STATUS_CHOICES}
        if status_value not in allowed:
            return Response({"error": "invalid availability_status"}, status=400)
        updated = qs.update(availability_status=status_value, data_updated_at=now)
    elif action == "stock":
        if not _has_seller_permission(request.user, "can_manage_pricing"):
            return Response({"error": "pricing permission required"}, status=403)
        try:
            stock_value = int(request.data.get("stock_quantity"))
            if stock_value < 0:
                raise ValueError
        except (TypeError, ValueError):
            return Response({"error": "stock_quantity must be integer >= 0"}, status=400)
        updated = qs.update(stock_quantity=stock_value, data_updated_at=now)
    else:
        return Response({"error": "unknown action"}, status=400)
    return Response({"ok": True, "updated_count": updated, "action": action})


@extend_schema(responses=OpenApiTypes.OBJECT)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def api_seller_product_export(request):
    if _role_for(request.user) != "seller":
        return _seller_api_forbidden()
    parts = _seller_parts_queryset(request.user).order_by("oem_number", "title")
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="seller_products_export.csv"'
    response.write("id,title,oem_number,brand,price,currency,stock_quantity,availability_status,data_updated_at\r\n")
    for part in parts:
        response.write(
            f'{part.id},"{(part.title or "").replace("\"", "\"\"")}",'
            f'"{(part.oem_number or "").replace("\"", "\"\"")}",'
            f'"{(part.brand.name if part.brand else "").replace("\"", "\"\"")}",'
            f"{part.price},{part.currency},{part.stock_quantity},{part.availability_status},{part.data_updated_at.isoformat()}\r\n"
        )
    return response


@extend_schema(responses=OpenApiTypes.OBJECT)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def api_seller_requests(request):
    if _role_for(request.user) != "seller":
        return _seller_api_forbidden()
    rfqs = _seller_requests_queryset(request.user)
    q = request.GET.get("q", "").strip()
    status = request.GET.get("status", "").strip()
    if status:
        rfqs = rfqs.filter(status=status)
    if q:
        request_filter = (
            Q(created_by__profile__customer_public_code__icontains=q)
            | Q(items__query__icontains=q)
            | Q(items__matched_part__oem_number__icontains=q)
            | Q(items__matched_part__title__icontains=q)
        )
        if q.isdigit():
            request_filter |= Q(id=int(q))
        rfqs = rfqs.filter(request_filter).distinct()
    seller = seller_principal(request.user)
    items = [_serialize_seller_rfq(rfq, seller) for rfq in rfqs[:100]]
    return Response({"items": items})


@extend_schema(responses=OpenApiTypes.OBJECT)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def api_seller_request_detail(request, rfq_id: int):
    if _role_for(request.user) != "seller":
        return _seller_api_forbidden()
    rfq = get_object_or_404(_seller_requests_queryset(request.user), id=rfq_id)
    return Response(_serialize_seller_rfq(rfq, seller_principal(request.user)))


@extend_schema(responses=OpenApiTypes.OBJECT)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def api_seller_request_quote(request, rfq_id: int):
    if _role_for(request.user) != "seller":
        return _seller_api_forbidden()
    if not _has_seller_permission(request.user, "can_manage_orders"):
        return Response({"error": "orders permission required"}, status=403)
    rfq = get_object_or_404(_seller_requests_queryset(request.user), id=rfq_id)
    supplier_comment = (request.data.get("comment") or "").strip()
    if len(supplier_comment) > 2_000:
        return Response({"error": "comment is too long"}, status=400)
    seller_items = RFQItem.objects.filter(
        rfq=rfq,
        matched_part__seller=seller_principal(request.user),
    )
    seller_items.update(
        decision_reason=f"seller_quote:{supplier_comment}" if supplier_comment else "seller_quote",
        state="auto_matched",
    )
    _refresh_seller_dashboard_projection(request.user)
    return Response({"ok": True, "rfq_id": rfq.id, "status": rfq.status})


@extend_schema(responses=OpenApiTypes.OBJECT)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def api_seller_request_decline(request, rfq_id: int):
    if _role_for(request.user) != "seller":
        return _seller_api_forbidden()
    if not _has_seller_permission(request.user, "can_manage_orders"):
        return Response({"error": "orders permission required"}, status=403)
    rfq = get_object_or_404(_seller_requests_queryset(request.user), id=rfq_id)
    supplier_comment = (request.data.get("reason") or "").strip()
    if len(supplier_comment) > 2_000:
        return Response({"error": "reason is too long"}, status=400)
    seller_items = RFQItem.objects.filter(
        rfq=rfq,
        matched_part__seller=seller_principal(request.user),
    )
    seller_items.update(
        decision_reason=f"seller_decline:{supplier_comment}" if supplier_comment else "seller_decline",
        state="needs_review",
    )
    _refresh_seller_dashboard_projection(request.user)
    return Response({"ok": True, "rfq_id": rfq.id, "status": rfq.status})


@extend_schema(responses=OpenApiTypes.OBJECT)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def api_seller_request_renegotiate(request, rfq_id: int):
    if _role_for(request.user) != "seller":
        return _seller_api_forbidden()
    if not _has_seller_permission(request.user, "can_manage_orders"):
        return Response({"error": "orders permission required"}, status=403)
    rfq = get_object_or_404(_seller_requests_queryset(request.user), id=rfq_id)
    supplier_comment = (request.data.get("comment") or "").strip()
    if len(supplier_comment) > 2_000:
        return Response({"error": "comment is too long"}, status=400)
    seller_items = RFQItem.objects.filter(
        rfq=rfq,
        matched_part__seller=seller_principal(request.user),
    )
    seller_items.update(
        decision_reason=f"seller_renegotiate:{supplier_comment}" if supplier_comment else "seller_renegotiate",
        state="needs_review",
    )
    _refresh_seller_dashboard_projection(request.user)
    return Response({"ok": True, "rfq_id": rfq.id, "status": rfq.status})


@extend_schema(responses=OpenApiTypes.OBJECT)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def api_seller_orders(request):
    if _role_for(request.user) != "seller":
        return _seller_api_forbidden()
    orders = _seller_orders_queryset(request.user)
    q = request.GET.get("q", "").strip()
    status = request.GET.get("status", "").strip()
    needs_action = request.GET.get("needs_action", "").strip()
    sla = request.GET.get("sla", "").strip()
    if status:
        orders = orders.filter(status=status)
    if needs_action in {"1", "true", "yes"}:
        orders = orders.filter(sla_status__in=["at_risk", "breached"])
    if sla:
        orders = orders.filter(sla_status=sla)
    if q:
        order_filter = (
            Q(buyer__profile__customer_public_code__icontains=q)
            | Q(items__part__oem_number__icontains=q)
            | Q(items__part__title__icontains=q)
        )
        if q.isdigit():
            order_filter |= Q(id=int(q))
        orders = orders.filter(order_filter).distinct()
    seller = seller_principal(request.user)
    return Response({"items": [_serialize_seller_order(order, seller) for order in orders[:100]]})


@extend_schema(responses=OpenApiTypes.OBJECT)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def api_seller_order_detail(request, order_id: int):
    if _role_for(request.user) != "seller":
        return _seller_api_forbidden()
    order = get_object_or_404(_seller_orders_queryset(request.user), id=order_id)
    _recalc_order_sla(order)
    seller = seller_principal(request.user)
    payload = _serialize_seller_order(order, seller)
    payload["events"] = [
        _serialize_seller_order_event(event, order, seller)
        for event in seller_visible_events(order, seller)[:100]
    ]
    visible_documents = seller_visible_documents(order, seller)
    payload["documents"] = [
        {
            "id": doc.id,
            "doc_type": doc.doc_type,
            "title": doc.title,
            "file_url": (
                f"/api/assistant/orders/{order.id}/documents/{doc.id}/file/"
                if doc.file_obj
                else ""
            ),
            "created_at": doc.created_at.isoformat(),
        }
        for doc in visible_documents[:100]
    ]
    payload["claims"] = [
        _serialize_seller_order_claim(claim, seller)
        for claim in seller_visible_claims(order, seller)[:100]
    ]
    return Response(payload)


@extend_schema(responses=OpenApiTypes.OBJECT)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def api_seller_order_timeline(request, order_id: int):
    if _role_for(request.user) != "seller":
        return _seller_api_forbidden()
    order = get_object_or_404(_seller_orders_queryset(request.user), id=order_id)
    seller = seller_principal(request.user)
    return Response({
        "order_id": order.id,
        "items": [
            _serialize_seller_order_event(event, order, seller)
            for event in seller_visible_events(order, seller)[:100]
        ],
    })


@extend_schema(responses=OpenApiTypes.OBJECT)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def api_seller_order_action(request, order_id: int):
    if _role_for(request.user) != "seller":
        return _seller_api_forbidden()
    if not _has_seller_permission(request.user, "can_manage_orders"):
        return Response({"error": "orders permission required"}, status=403)
    status = (request.data.get("status") or request.data.get("action") or "").strip()
    allowed = {key for key, _ in Order.STATUS_CHOICES}
    seller_allowed_statuses = {"confirmed", "in_production", "ready_to_ship"}
    if status not in allowed:
        return Response({"error": "invalid status"}, status=400)
    if status not in seller_allowed_statuses:
        return Response({"error": "status cannot be changed by seller"}, status=400)

    with transaction.atomic():
        if not _seller_orders_queryset(request.user).filter(id=order_id).exists():
            return Response({"detail": "Not found."}, status=404)
        order = get_object_or_404(
            Order.objects.select_for_update().prefetch_related("items__part"),
            id=order_id,
        )
        if len(seller_ids_for_order(order)) > 1:
            return Response(
                {"error": "multi-supplier order status is managed per item"},
                status=409,
            )
        current = order.status
        if status == current:
            return Response({
                "ok": True,
                "order_id": order.id,
                "status": order.status,
                "sla_status": order.sla_status,
                "no_change": True,
            })
        next_allowed = ORDER_TRANSITIONS.get(current, set())
        if status not in next_allowed:
            return Response({"error": f"invalid transition: {current} -> {status}"}, status=400)
        update_fields = ["status"]
        order.status = status
        if status == "confirmed" and not order.ship_deadline:
            order.ship_deadline = timezone.now() + timedelta(days=5)
            update_fields.append("ship_deadline")
        order.save(update_fields=update_fields)
        _log_order_event(
            order, "status_changed", source="seller", actor=request.user,
            meta={"from": current, "to": status},
        )
        _recalc_order_sla(order)

    _refresh_seller_dashboard_projection(request.user)
    return Response({"ok": True, "order_id": order.id, "status": order.status, "sla_status": order.sla_status})


@extend_schema(responses=OpenApiTypes.OBJECT)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def api_seller_claims(request):
    if _role_for(request.user) != "seller":
        return _seller_api_forbidden()
    seller = seller_principal(request.user)
    claims = (
        OrderClaim.objects.filter(order__items__part__seller=seller)
        .distinct()
        .select_related("order__buyer__profile", "opened_by", "resolved_by")
        .prefetch_related("order__items__part")
        .order_by("-created_at")
    )
    status = request.GET.get("status", "").strip()
    if status:
        claims = claims.filter(status=status)
    visible = [
        claim
        for claim in claims[:300]
        if seller_can_access_claim(seller, claim)
    ][:100]
    return Response({
        "items": [
            _serialize_seller_order_claim(claim, seller)
            for claim in visible
        ],
    })


@extend_schema(responses=OpenApiTypes.OBJECT)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def api_seller_claim_respond(request, claim_id: int):
    if _role_for(request.user) != "seller":
        return _seller_api_forbidden()
    if not _has_seller_permission(request.user, "can_manage_orders"):
        return Response({"error": "orders permission required"}, status=403)
    seller = seller_principal(request.user)
    claim = get_object_or_404(
        OrderClaim.objects.filter(order__items__part__seller=seller).distinct(),
        id=claim_id,
    )
    if not seller_can_access_claim(seller, claim):
        return Response({"error": "claim access denied"}, status=403)
    status = (request.data.get("status") or "").strip()
    comment = (request.data.get("comment") or "").strip()
    if status != "in_review":
        return Response(
            {"error": "claim resolution is managed by platform operator"},
            status=403,
        )
    if len(comment) > 2_000:
        return Response({"error": "comment is too long"}, status=400)
    if redact_party_contacts(comment) != comment:
        return Response(
            {"error": "contact details are not allowed"},
            status=400,
        )
    with transaction.atomic():
        claim = (
            OrderClaim.objects.select_for_update(of=("self",))
            .select_related("order__buyer__profile")
            .get(id=claim.id)
        )
        if claim.status not in {"open", "in_review"}:
            return Response(
                {"error": "claim can no longer be accepted for review"},
                status=409,
            )
        update_fields = ["updated_at"]
        if claim.status == "open":
            claim.status = "in_review"
            update_fields.append("status")
        if comment:
            claim.description = (
                f"{claim.description}\n\nSeller response: {comment}"
            ).strip()
            update_fields.append("description")
        claim.save(update_fields=update_fields)
    _log_order_event(
        claim.order,
        "claim_status_changed",
        source="seller",
        actor=request.user,
        meta={"claim_id": claim.id, "status": status},
    )
    _refresh_seller_dashboard_projection(request.user)
    return Response({
        "ok": True,
        "claim": _serialize_seller_order_claim(claim, seller_principal(request.user)),
    })


@extend_schema(responses=OpenApiTypes.OBJECT)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def api_dashboard_summary(request):
    role = _role_for(request.user)
    if role == "seller":
        seller = seller_principal(request.user)
        scoped = _apply_seller_brand_scope(
            request.user,
            Part.objects.filter(seller=seller),
        )
        metrics = scoped.aggregate(
            parts_count=Count("id"),
            inventory_value=Sum("price"),
        )
        order_count = Order.objects.filter(items__part__seller=seller).distinct().count()
        return Response(
            {
                "role": "seller",
                "parts_count": metrics["parts_count"] or 0,
                "inventory_value": metrics["inventory_value"] or 0,
                "order_count": order_count,
            }
        )

    metrics = Order.objects.filter(buyer=request.user).aggregate(
        order_count=Count("id"),
        total_spent=Sum("total_amount"),
    )
    return Response(
        {
            "role": "buyer",
            "order_count": metrics["order_count"] or 0,
            "total_spent": metrics["total_spent"] or 0,
        }
    )


@extend_schema(responses=OpenApiTypes.OBJECT)
@api_view(["GET"])
@permission_classes([AllowAny])
def api_health(_request):
    return Response({"ok": True, "service": "hybrid_marketplace"}, status=200)


@extend_schema(responses=OpenApiTypes.OBJECT)
@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([LookupThrottle])
def api_newsletter_subscribe(request):
    """Validate and persist a public newsletter subscription."""
    from django.core.validators import validate_email
    from django.core.exceptions import ValidationError

    email = (request.data.get("email") or "").strip().lower()
    try:
        validate_email(email)
    except ValidationError:
        return Response({"ok": False, "error": "invalid_email"}, status=400)

    subscriber, created = NewsletterSubscriber.objects.get_or_create(email=email)
    if not created and not subscriber.is_active:
        subscriber.is_active = True
        subscriber.save(update_fields=["is_active", "updated_at"])
    # Do not reveal whether an address was already present in the database.
    return Response({"ok": True}, status=202)


@extend_schema(responses=OpenApiTypes.OBJECT)
@api_view(["GET"])
@permission_classes([AllowAny])
def api_readiness(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception as exc:
        ok = False
        detail = exc.__class__.__name__
    else:
        ok = True
        detail = ""

    token = (getattr(settings, "HEALTHCHECK_TOKEN", "") or "").strip()
    provided = (request.headers.get("X-Healthcheck-Token") or "").strip()
    if token and hmac.compare_digest(provided, token):
        payload = {"ok": ok, "checks": {"database": ok}}
        if detail:
            payload["error"] = detail
        return Response(payload, status=200 if ok else 503)
    return Response({"ok": ok, "status": "ready" if ok else "unavailable"}, status=200 if ok else 503)


@extend_schema(responses=OpenApiTypes.OBJECT)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def api_hybrid_analytics(request):
    role = _role_for(request.user)
    if role == "seller" and not _has_seller_permission(request.user, "can_view_analytics"):
        return Response({"error": "analytics permission required"}, status=403)

    days_raw = request.GET.get("days", "30").strip()
    try:
        days = max(1, min(365, int(days_raw)))
    except ValueError:
        days = 30
    start = timezone.now() - timedelta(days=days)

    order_qs = Order.objects.filter(created_at__gte=start)
    rfq_qs = RFQ.objects.filter(created_at__gte=start)
    claim_qs = OrderClaim.objects.filter(created_at__gte=start)
    webhook_qs = WebhookDeliveryLog.objects.filter(created_at__gte=start)

    if role == "seller":
        seller = seller_principal(request.user)
        company_ids = seller_company_user_ids(seller)
        order_qs = order_qs.filter(items__part__seller=seller).distinct()
        rfq_qs = rfq_qs.filter(items__matched_part__seller=seller).distinct()
        claim_qs = (
            claim_qs.filter(order__items__part__seller=seller)
            .annotate(
                order_seller_count=Count(
                    "order__items__part__seller",
                    distinct=True,
                ),
            )
            .filter(
                Q(order_seller_count=1)
                | Q(opened_by_id__in=company_ids)
            )
            .distinct()
        )
        webhook_qs = (
            webhook_qs.filter(order__items__part__seller=seller)
            .annotate(
                order_seller_count=Count(
                    "order__items__part__seller",
                    distinct=True,
                ),
            )
            .filter(order_seller_count=1)
            .distinct()
        )
    elif role == "buyer":
        order_qs = order_qs.filter(buyer=request.user)
        rfq_qs = rfq_qs.filter(created_by=request.user)
        claim_qs = claim_qs.filter(order__buyer=request.user)
        webhook_qs = webhook_qs.filter(order__buyer=request.user)

    orders_total = order_qs.count()
    rfq_total = rfq_qs.count()
    claims_open = claim_qs.exclude(status__in=["closed", "rejected"]).count()
    webhooks_failed = webhook_qs.filter(success=False).count()
    if role == "seller":
        revenue_total = (
            OrderItem.objects.filter(order__in=order_qs, part__seller=seller)
            .aggregate(
                total=Sum(
                    F("unit_price") * F("quantity"),
                    output_field=DecimalField(
                        max_digits=18,
                        decimal_places=2,
                    ),
                ),
            )["total"]
            or 0
        )
    else:
        revenue_total = order_qs.aggregate(total=Sum("total_amount"))["total"] or 0

    payload = {
        "window_days": days,
        "role": role or "anonymous",
        "orders_total": orders_total,
        "orders_by_status": {
            row["status"]: row["count"]
            for row in order_qs.values("status").annotate(count=Count("id"))
        },
        "rfq_total": rfq_total,
        "claims_open": claims_open,
        "webhooks_failed": webhooks_failed,
        "revenue_total": revenue_total,
    }
    if role == "admin" or (role or "").startswith("operator"):
        payload["suppliers_at_risk"] = (
            Part.objects.filter(seller__profile__supplier_status__in=["risky", "rejected"], is_active=True)
            .values("seller_id")
            .distinct()
            .count()
        )
    payload["max_import_rows"] = settings.MAX_IMPORT_ROWS
    payload["max_quote_items"] = settings.MAX_QUOTE_ITEMS
    return Response(payload, status=200)


@extend_schema(responses=OpenApiTypes.OBJECT)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def api_hybrid_funnel(request):
    role = _role_for(request.user)
    if role == "seller" and not _has_seller_permission(request.user, "can_view_analytics"):
        return Response({"error": "analytics permission required"}, status=403)

    days_raw = request.GET.get("days", "30").strip()
    try:
        days = max(1, min(365, int(days_raw)))
    except ValueError:
        days = 30
    start = timezone.now() - timedelta(days=days)

    rfq_qs = RFQ.objects.filter(created_at__gte=start)
    order_qs = Order.objects.filter(created_at__gte=start)
    claim_qs = OrderClaim.objects.filter(created_at__gte=start)

    if role == "seller":
        seller = seller_principal(request.user)
        company_ids = seller_company_user_ids(seller)
        rfq_qs = rfq_qs.filter(items__matched_part__seller=seller).distinct()
        order_qs = order_qs.filter(items__part__seller=seller).distinct()
        claim_qs = (
            claim_qs.filter(order__items__part__seller=seller)
            .annotate(
                order_seller_count=Count(
                    "order__items__part__seller",
                    distinct=True,
                ),
            )
            .filter(
                Q(order_seller_count=1)
                | Q(opened_by_id__in=company_ids)
            )
            .distinct()
        )
    elif role == "buyer":
        rfq_qs = rfq_qs.filter(created_by=request.user)
        order_qs = order_qs.filter(buyer=request.user)
        claim_qs = claim_qs.filter(order__buyer=request.user)

    rfq_total = rfq_qs.count()
    order_total = order_qs.count()
    claim_total = claim_qs.count()
    delivered_total = order_qs.filter(status__in=["delivered", "completed"]).count()

    rfq_to_order = round((order_total / rfq_total) * 100, 2) if rfq_total else 0.0
    order_to_delivery = round((delivered_total / order_total) * 100, 2) if order_total else 0.0
    claim_rate = round((claim_total / order_total) * 100, 2) if order_total else 0.0

    return Response(
        {
            "window_days": days,
            "role": role or "anonymous",
            "funnel": {
                "rfq_total": rfq_total,
                "order_total": order_total,
                "delivered_total": delivered_total,
                "claim_total": claim_total,
            },
            "conversion": {
                "rfq_to_order_pct": rfq_to_order,
                "order_to_delivery_pct": order_to_delivery,
                "claims_per_order_pct": claim_rate,
            },
        },
        status=200,
    )

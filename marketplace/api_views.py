from datetime import timedelta

from django.conf import settings
from django.db import connection
from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from django.http import HttpResponse

from .models import Category, Order, OrderClaim, Part, RFQ, RFQItem, WebhookDeliveryLog
from .serializers import CategorySerializer, OrderSerializer, PartSerializer
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
    return _apply_seller_brand_scope(user, Part.objects.filter(seller=user)).select_related("category", "brand")


def _serialize_seller_part(part: Part) -> dict:
    payload = PartSerializer(part).data
    payload["stale"] = _part_stale_snapshot(part)
    payload["demand"] = _part_demand_stats(part)
    return payload


def _seller_requests_queryset(user):
    return _seller_rfqs_qs(user)


def _serialize_seller_rfq(rfq: RFQ, seller_user) -> dict:
    seller_items = [item for item in rfq.items.all() if item.matched_part and item.matched_part.seller_id == seller_user.id]
    return {
        "id": rfq.id,
        "customer_name": rfq.customer_name,
        "customer_email": rfq.customer_email,
        "company_name": rfq.company_name,
        "mode": rfq.mode,
        "urgency": rfq.urgency,
        "status": rfq.status,
        "notes": rfq.notes,
        "created_at": rfq.created_at.isoformat(),
        "seller_items_count": len(seller_items),
        "total_quantity": sum(item.quantity for item in seller_items),
        "estimated_total": sum(item.estimated_line_total for item in seller_items),
        "items": [
            {
                "id": item.id,
                "query": item.query,
                "quantity": item.quantity,
                "state": item.state,
                "confidence": item.confidence,
                "decision_reason": item.decision_reason,
                "matched_part_id": item.matched_part_id,
                "matched_part_title": item.matched_part.title if item.matched_part else "",
                "matched_part_oem": item.matched_part.oem_number if item.matched_part else "",
            }
            for item in seller_items
        ],
    }


def _seller_orders_queryset(user):
    return (
        Order.objects.filter(items__part__seller=user)
        .distinct()
        .prefetch_related("items__part", "events", "documents", "claims")
        .order_by("-created_at")
    )


def _serialize_seller_order(order: Order, seller_user) -> dict:
    seller_items = [item for item in order.items.all() if item.part and item.part.seller_id == seller_user.id]
    open_claims = [claim for claim in order.claims.all() if claim.status in {"open", "in_review"}]
    return {
        "id": order.id,
        "customer_name": order.customer_name,
        "customer_email": order.customer_email,
        "status": order.status,
        "payment_status": order.payment_status,
        "sla_status": order.sla_status,
        "sla_breaches_count": order.sla_breaches_count,
        "supplier_confirm_deadline": order.supplier_confirm_deadline.isoformat() if order.supplier_confirm_deadline else None,
        "ship_deadline": order.ship_deadline.isoformat() if order.ship_deadline else None,
        "invoice_number": order.invoice_number,
        "total_amount": str(order.total_amount),
        "reserve_amount": str(order.reserve_amount),
        "reserve_percent": str(order.reserve_percent),
        "created_at": order.created_at.isoformat(),
        "items_count": len(seller_items),
        "units_total": sum(int(item.quantity) for item in seller_items),
        "documents_count": len(order.documents.all()),
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


def _serialize_order_event(event: OrderEvent) -> dict:
    return {
        "id": event.id,
        "event_type": event.event_type,
        "source": event.source,
        "actor_id": event.actor_id,
        "actor_name": event.actor.username if event.actor else "",
        "meta": event.meta,
        "created_at": event.created_at.isoformat(),
    }


def _serialize_order_claim(claim: OrderClaim) -> dict:
    return {
        "id": claim.id,
        "order_id": claim.order_id,
        "title": claim.title,
        "description": claim.description,
        "status": claim.status,
        "opened_by_id": claim.opened_by_id,
        "resolved_by_id": claim.resolved_by_id,
        "created_at": claim.created_at.isoformat(),
        "updated_at": claim.updated_at.isoformat(),
    }


@api_view(["GET"])
@permission_classes([AllowAny])
@throttle_classes([LookupThrottle])
def api_categories(_request):
    categories = Category.objects.all().order_by("name")
    return Response({"items": CategorySerializer(categories, many=True).data})


@api_view(["GET"])
@permission_classes([AllowAny])
@throttle_classes([LookupThrottle])
def api_parts(request):
    qs = _eligible_parts_qs().select_related("category", "brand", "seller")
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(title__icontains=q) | Q(oem_number__icontains=q))
    return Response({"items": PartSerializer(qs.order_by("-created_at")[:200], many=True).data})


@api_view(["GET"])
@permission_classes([AllowAny])
@throttle_classes([LookupThrottle])
def api_part_detail(_request, part_id: int):
    part = get_object_or_404(Part.objects.select_related("category", "brand", "seller"), id=part_id)
    return Response(PartSerializer(part).data)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def api_my_orders(request):
    role = _role_for(request.user)
    if role == "seller":
        qs = Order.objects.filter(items__part__seller=request.user).distinct()
    else:
        qs = Order.objects.filter(buyer=request.user)
    qs = qs.prefetch_related("items__part")
    return Response({"items": OrderSerializer(qs[:100], many=True).data})


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


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def api_seller_part_detail(request, part_id: int):
    if _role_for(request.user) != "seller":
        return _seller_api_forbidden()
    part = get_object_or_404(_seller_parts_queryset(request.user), id=part_id)
    return Response(_serialize_seller_part(part))


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


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def api_seller_part_demand(request, part_id: int):
    if _role_for(request.user) != "seller":
        return _seller_api_forbidden()
    part = get_object_or_404(_seller_parts_queryset(request.user), id=part_id)
    return Response({"part_id": part.id, **_part_demand_stats(part)})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def api_seller_product_bulk_update(request):
    if _role_for(request.user) != "seller":
        return _seller_api_forbidden()
    if not _has_seller_permission(request.user, "can_manage_assortment"):
        return Response({"error": "assortment permission required"}, status=403)

    action = (request.data.get("action") or "").strip()
    ids = request.data.get("part_ids") or request.data.get("product_ids") or []
    try:
        selected_ids = [int(value) for value in ids]
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
            f'"{((part.brand.name if part.brand else "")).replace("\"", "\"\"")}",'
            f"{part.price},{part.currency},{part.stock_quantity},{part.availability_status},{part.data_updated_at.isoformat()}\r\n"
        )
    return response


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
        rfqs = rfqs.filter(
            Q(customer_name__icontains=q)
            | Q(company_name__icontains=q)
            | Q(customer_email__icontains=q)
            | Q(items__query__icontains=q)
            | Q(items__matched_part__oem_number__icontains=q)
            | Q(items__matched_part__title__icontains=q)
        ).distinct()
    items = [_serialize_seller_rfq(rfq, request.user) for rfq in rfqs[:100]]
    return Response({"items": items})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def api_seller_request_detail(request, rfq_id: int):
    if _role_for(request.user) != "seller":
        return _seller_api_forbidden()
    rfq = get_object_or_404(_seller_requests_queryset(request.user), id=rfq_id)
    return Response(_serialize_seller_rfq(rfq, request.user))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def api_seller_request_quote(request, rfq_id: int):
    if _role_for(request.user) != "seller":
        return _seller_api_forbidden()
    if not _has_seller_permission(request.user, "can_manage_orders"):
        return Response({"error": "orders permission required"}, status=403)
    rfq = get_object_or_404(_seller_requests_queryset(request.user), id=rfq_id)
    supplier_comment = (request.data.get("comment") or "").strip()
    seller_items = RFQItem.objects.filter(rfq=rfq, matched_part__seller=request.user)
    seller_items.update(
        decision_reason=f"seller_quote:{supplier_comment}" if supplier_comment else "seller_quote",
        state="auto_matched",
    )
    rfq.status = "quoted"
    rfq.save(update_fields=["status"])
    _refresh_seller_dashboard_projection(request.user)
    return Response({"ok": True, "rfq_id": rfq.id, "status": rfq.status})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def api_seller_request_decline(request, rfq_id: int):
    if _role_for(request.user) != "seller":
        return _seller_api_forbidden()
    if not _has_seller_permission(request.user, "can_manage_orders"):
        return Response({"error": "orders permission required"}, status=403)
    rfq = get_object_or_404(_seller_requests_queryset(request.user), id=rfq_id)
    supplier_comment = (request.data.get("reason") or "").strip()
    seller_items = RFQItem.objects.filter(rfq=rfq, matched_part__seller=request.user)
    seller_items.update(
        decision_reason=f"seller_decline:{supplier_comment}" if supplier_comment else "seller_decline",
        state="needs_review",
    )
    rfq.status = "cancelled"
    rfq.save(update_fields=["status"])
    _refresh_seller_dashboard_projection(request.user)
    return Response({"ok": True, "rfq_id": rfq.id, "status": rfq.status})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def api_seller_request_renegotiate(request, rfq_id: int):
    if _role_for(request.user) != "seller":
        return _seller_api_forbidden()
    if not _has_seller_permission(request.user, "can_manage_orders"):
        return Response({"error": "orders permission required"}, status=403)
    rfq = get_object_or_404(_seller_requests_queryset(request.user), id=rfq_id)
    supplier_comment = (request.data.get("comment") or "").strip()
    seller_items = RFQItem.objects.filter(rfq=rfq, matched_part__seller=request.user)
    seller_items.update(
        decision_reason=f"seller_renegotiate:{supplier_comment}" if supplier_comment else "seller_renegotiate",
        state="needs_review",
    )
    rfq.status = "needs_review"
    rfq.save(update_fields=["status"])
    _refresh_seller_dashboard_projection(request.user)
    return Response({"ok": True, "rfq_id": rfq.id, "status": rfq.status})


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
        orders = orders.filter(
            Q(customer_name__icontains=q)
            | Q(customer_email__icontains=q)
            | Q(items__part__oem_number__icontains=q)
            | Q(items__part__title__icontains=q)
        ).distinct()
    return Response({"items": [_serialize_seller_order(order, request.user) for order in orders[:100]]})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def api_seller_order_detail(request, order_id: int):
    if _role_for(request.user) != "seller":
        return _seller_api_forbidden()
    order = get_object_or_404(_seller_orders_queryset(request.user), id=order_id)
    _recalc_order_sla(order)
    payload = _serialize_seller_order(order, request.user)
    payload["events"] = [_serialize_order_event(event) for event in order.events.all()[:100]]
    payload["documents"] = [
        {
            "id": doc.id,
            "doc_type": doc.doc_type,
            "title": doc.title,
            "file_url": doc.file_url,
            "created_at": doc.created_at.isoformat(),
        }
        for doc in order.documents.all()[:100]
    ]
    payload["claims"] = [_serialize_order_claim(claim) for claim in order.claims.all()[:100]]
    return Response(payload)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def api_seller_order_timeline(request, order_id: int):
    if _role_for(request.user) != "seller":
        return _seller_api_forbidden()
    order = get_object_or_404(_seller_orders_queryset(request.user), id=order_id)
    return Response({"order_id": order.id, "items": [_serialize_order_event(event) for event in order.events.all()[:100]]})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def api_seller_order_action(request, order_id: int):
    if _role_for(request.user) != "seller":
        return _seller_api_forbidden()
    if not _has_seller_permission(request.user, "can_manage_orders"):
        return Response({"error": "orders permission required"}, status=403)
    order = get_object_or_404(_seller_orders_queryset(request.user), id=order_id)
    status = (request.data.get("status") or request.data.get("action") or "").strip()
    allowed = {key for key, _ in Order.STATUS_CHOICES}
    seller_allowed_statuses = {"confirmed", "in_production", "ready_to_ship", "shipped", "delivered", "cancelled"}
    if status not in allowed:
        return Response({"error": "invalid status"}, status=400)
    if status not in seller_allowed_statuses:
        return Response({"error": "status cannot be changed by seller"}, status=400)
    current = order.status
    if status != current:
        next_allowed = ORDER_TRANSITIONS.get(current, set())
        if status not in next_allowed:
            return Response({"error": f"invalid transition: {current} -> {status}"}, status=400)
    update_fields = ["status"]
    order.status = status
    if status == "confirmed" and not order.ship_deadline:
        order.ship_deadline = timezone.now() + timedelta(days=5)
        update_fields.append("ship_deadline")
    order.save(update_fields=update_fields)
    _log_order_event(order, "status_changed", source="seller", actor=request.user, meta={"from": current, "to": status})
    _recalc_order_sla(order)
    _refresh_seller_dashboard_projection(request.user)
    return Response({"ok": True, "order_id": order.id, "status": order.status, "sla_status": order.sla_status})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def api_seller_claims(request):
    if _role_for(request.user) != "seller":
        return _seller_api_forbidden()
    claims = (
        OrderClaim.objects.filter(order__items__part__seller=request.user)
        .distinct()
        .select_related("order", "opened_by", "resolved_by")
        .order_by("-created_at")
    )
    status = request.GET.get("status", "").strip()
    if status:
        claims = claims.filter(status=status)
    return Response({"items": [_serialize_order_claim(claim) for claim in claims[:100]]})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def api_seller_claim_respond(request, claim_id: int):
    if _role_for(request.user) != "seller":
        return _seller_api_forbidden()
    if not _has_seller_permission(request.user, "can_manage_orders"):
        return Response({"error": "orders permission required"}, status=403)
    claim = get_object_or_404(
        OrderClaim.objects.filter(order__items__part__seller=request.user).distinct(),
        id=claim_id,
    )
    status = (request.data.get("status") or "").strip()
    comment = (request.data.get("comment") or "").strip()
    allowed = {"in_review", "approved", "rejected", "closed"}
    if status not in allowed:
        return Response({"error": "invalid claim status"}, status=400)
    claim.status = status
    if comment:
        claim.description = f"{claim.description}\n\nSeller response: {comment}".strip()
    claim.resolved_by = request.user
    claim.save(update_fields=["status", "description", "resolved_by", "updated_at"])
    _log_order_event(
        claim.order,
        "claim_status_changed",
        source="seller",
        actor=request.user,
        meta={"claim_id": claim.id, "status": status},
    )
    _refresh_seller_dashboard_projection(request.user)
    return Response({"ok": True, "claim": _serialize_order_claim(claim)})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def api_dashboard_summary(request):
    role = _role_for(request.user)
    if role == "seller":
        scoped = _apply_seller_brand_scope(request.user, Part.objects.filter(seller=request.user))
        metrics = scoped.aggregate(
            parts_count=Count("id"),
            inventory_value=Sum("price"),
        )
        order_count = Order.objects.filter(items__part__seller=request.user).distinct().count()
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


@api_view(["GET"])
@permission_classes([AllowAny])
def api_health(_request):
    return Response({"ok": True, "service": "hybrid_marketplace"}, status=200)


@api_view(["GET"])
@permission_classes([AllowAny])
def api_readiness(_request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception as exc:
        return Response({"ok": False, "database": "down", "error": exc.__class__.__name__}, status=503)
    return Response({"ok": True, "database": "up", "time": timezone.now().isoformat()}, status=200)


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
        order_qs = order_qs.filter(items__part__seller=request.user).distinct()
        rfq_qs = rfq_qs.filter(items__matched_part__seller=request.user).distinct()
        claim_qs = claim_qs.filter(order__items__part__seller=request.user).distinct()
        webhook_qs = webhook_qs.filter(order__items__part__seller=request.user).distinct()
    elif role == "buyer":
        order_qs = order_qs.filter(buyer=request.user)
        rfq_qs = rfq_qs.filter(created_by=request.user)
        claim_qs = claim_qs.filter(order__buyer=request.user)
        webhook_qs = webhook_qs.filter(order__buyer=request.user)

    orders_total = order_qs.count()
    rfq_total = rfq_qs.count()
    claims_open = claim_qs.exclude(status__in=["closed", "rejected"]).count()
    webhooks_failed = webhook_qs.filter(success=False).count()
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
    if role != "seller":
        payload["suppliers_at_risk"] = (
            Part.objects.filter(seller__profile__supplier_status__in=["risky", "rejected"], is_active=True)
            .values("seller_id")
            .distinct()
            .count()
        )
    payload["max_import_rows"] = settings.MAX_IMPORT_ROWS
    payload["max_quote_items"] = settings.MAX_QUOTE_ITEMS
    return Response(payload, status=200)


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
        rfq_qs = rfq_qs.filter(items__matched_part__seller=request.user).distinct()
        order_qs = order_qs.filter(items__part__seller=request.user).distinct()
        claim_qs = claim_qs.filter(order__items__part__seller=request.user).distinct()
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

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

from .models import Category, Order, OrderClaim, Part, RFQ, WebhookDeliveryLog
from .serializers import CategorySerializer, OrderSerializer, PartSerializer
from .views import _apply_seller_brand_scope, _eligible_parts_qs, _has_seller_permission, _role_for


class LookupThrottle(ScopedRateThrottle):
    scope = "lookup"


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
        return Response({"error": "seller role required"}, status=403)
    parts = _apply_seller_brand_scope(request.user, Part.objects.filter(seller=request.user)).select_related("category", "brand")
    return Response({"items": PartSerializer(parts, many=True).data})


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

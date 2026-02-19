from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import Category, Order, Part
from .serializers import CategorySerializer, OrderSerializer, PartSerializer
from .views import _apply_seller_brand_scope, _eligible_parts_qs, _role_for


@api_view(["GET"])
def api_categories(_request):
    categories = Category.objects.all().order_by("name")
    return Response({"items": CategorySerializer(categories, many=True).data})


@api_view(["GET"])
def api_parts(request):
    qs = _eligible_parts_qs().select_related("category", "brand", "seller")
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(title__icontains=q) | Q(oem_number__icontains=q))
    return Response({"items": PartSerializer(qs.order_by("-created_at")[:200], many=True).data})


@api_view(["GET"])
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

from __future__ import annotations

from decimal import Decimal
from time import monotonic, sleep

from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle, SimpleRateThrottle, UserRateThrottle

from .models import Category, Order, Part
from .serializers import CategorySerializer, OrderSerializer, PartSerializer
from .services.observability import log_api_error, metric_get, metric_inc
from .views import _apply_seller_brand_scope, _eligible_parts_qs, _role_for


class QuoteRateThrottle(SimpleRateThrottle):
    scope = "quote"

    def get_cache_key(self, request, view):
        ident = self.get_ident(request)
        return self.cache_format % {"scope": self.scope, "ident": ident}


class ImportRateThrottle(SimpleRateThrottle):
    scope = "import"

    def get_cache_key(self, request, view):
        ident = self.get_ident(request)
        return self.cache_format % {"scope": self.scope, "ident": ident}


class LookupRateThrottle(SimpleRateThrottle):
    scope = "lookup"

    def get_cache_key(self, request, view):
        ident = self.get_ident(request)
        return self.cache_format % {"scope": self.scope, "ident": ident}


@api_view(["GET"])
@throttle_classes([AnonRateThrottle, UserRateThrottle])
def api_categories(_request):
    categories = Category.objects.all().order_by("name")
    return Response({"items": CategorySerializer(categories, many=True).data})


@api_view(["GET"])
@throttle_classes([AnonRateThrottle, UserRateThrottle])
def api_parts(request):
    qs = _eligible_parts_qs().select_related("category", "brand", "seller")
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(title__icontains=q) | Q(oem_number__icontains=q))
    return Response({"items": PartSerializer(qs.order_by("-created_at")[:200], many=True).data})


@api_view(["GET"])
@throttle_classes([AnonRateThrottle, UserRateThrottle])
def api_part_detail(_request, part_id: int):
    part = get_object_or_404(Part.objects.select_related("category", "brand", "seller"), id=part_id)
    return Response(PartSerializer(part).data)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@throttle_classes([UserRateThrottle])
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
@throttle_classes([UserRateThrottle])
def api_seller_parts(request):
    if _role_for(request.user) != "seller":
        return Response({"error": "seller role required"}, status=403)
    parts = _apply_seller_brand_scope(request.user, Part.objects.filter(seller=request.user)).select_related(
        "category", "brand"
    )
    return Response({"items": PartSerializer(parts, many=True).data})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@throttle_classes([UserRateThrottle])
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


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([QuoteRateThrottle, UserRateThrottle])
def api_quote_preview(request):
    payload = request.data if isinstance(request.data, dict) else {}
    items = payload.get("items") or []
    if not isinstance(items, list):
        log_api_error("api_quote_preview", 400, "invalid_items_type")
        return Response({"error": "items must be a list"}, status=400)
    if not items:
        log_api_error("api_quote_preview", 400, "empty_items")
        return Response({"error": "items is required"}, status=400)
    if len(items) > int(settings.MAX_QUOTE_ITEMS):
        metric_inc("quote_limits_triggered_total")
        return Response({"error": f"too many items, max {settings.MAX_QUOTE_ITEMS}"}, status=413)

    line_items = []
    total = Decimal("0.00")
    for idx, raw in enumerate(items, start=1):
        if not isinstance(raw, dict):
            log_api_error("api_quote_preview", 400, "invalid_row", {"row": idx})
            return Response({"error": f"row {idx} must be an object"}, status=400)
        part_id = raw.get("part_id")
        qty_raw = raw.get("qty", 1)
        try:
            part_id_int = int(part_id)
            qty = int(qty_raw)
        except (TypeError, ValueError):
            log_api_error("api_quote_preview", 400, "invalid_row_fields", {"row": idx})
            return Response({"error": f"row {idx} has invalid part_id/qty"}, status=400)
        if qty <= 0:
            return Response({"error": f"row {idx} qty must be > 0"}, status=400)

        part = Part.objects.filter(id=part_id_int, is_active=True, price__gt=0).first()
        if not part:
            return Response({"error": f"row {idx} part not found"}, status=400)
        line_total = (part.price * qty).quantize(Decimal("0.01"))
        total += line_total
        line_items.append(
            {
                "part_id": part.id,
                "title": part.title,
                "qty": qty,
                "unit_price": str(part.price),
                "line_total": str(line_total),
            }
        )
    return Response({"items": line_items, "total": str(total.quantize(Decimal('0.01')))})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([UserRateThrottle])
def api_update_template(request):
    payload = request.data if isinstance(request.data, dict) else {}
    try:
        template_name = (payload.get("template") or "").strip()
        if not template_name:
            raise ValueError("template is required")
        if template_name not in {"default", "compact", "enterprise"}:
            raise ValueError("unsupported template")
    except ValueError:
        log_api_error("api_update_template", 400, "invalid_payload")
        return Response({"error": "invalid payload"}, status=400)
    except Exception:
        log_api_error("api_update_template", 400, "invalid_payload")
        return Response({"error": "invalid payload"}, status=400)
    return Response({"ok": True, "template": template_name})


def _lookup_provider(inn: str) -> dict:
    # Local deterministic stub for MVP while keeping timeout and circuit behavior.
    data = {
        "7707083893": {"name": "PAO Sberbank", "status": "active"},
        "7704340310": {"name": "OOO Demo Parts", "status": "active"},
    }
    sleep(0.01)
    return data.get(inn) or {"name": "", "status": "not_found"}


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@throttle_classes([LookupRateThrottle, UserRateThrottle])
def api_legal_entity_lookup(request):
    inn = (request.GET.get("inn") or "").strip()
    if not inn:
        return Response({"error": "inn is required"}, status=400)
    if len(inn) not in {10, 12} or not inn.isdigit():
        return Response({"error": "invalid inn"}, status=400)

    circuit_key = "legal_lookup:circuit_open_until"
    open_until = float(cache.get(circuit_key, 0) or 0)
    now = monotonic()
    if open_until > now:
        metric_inc("legal_lookup_circuit_open_total")
        return Response({"error": "service temporarily unavailable"}, status=503)

    started = monotonic()
    try:
        data = _lookup_provider(inn)
        elapsed = monotonic() - started
        if elapsed > float(settings.LEGAL_LOOKUP_TIMEOUT_SEC):
            metric_inc("legal_lookup_timeout_total")
            cache.set(circuit_key, monotonic() + int(settings.LEGAL_LOOKUP_CIRCUIT_SECONDS), timeout=int(settings.LEGAL_LOOKUP_CIRCUIT_SECONDS))
            return Response({"error": "lookup timeout"}, status=504)
    except Exception:
        metric_inc("legal_lookup_errors_total")
        cache.set(circuit_key, monotonic() + int(settings.LEGAL_LOOKUP_CIRCUIT_SECONDS), timeout=int(settings.LEGAL_LOOKUP_CIRCUIT_SECONDS))
        log_api_error("api_legal_entity_lookup", 503, "lookup_failed")
        return Response({"error": "lookup failed"}, status=503)

    if data.get("status") == "not_found":
        return Response({"error": "not found"}, status=404)
    return Response({"inn": inn, "entity": data, "latency_ms": int((monotonic() - started) * 1000)})


@api_view(["GET"])
@throttle_classes([AnonRateThrottle, UserRateThrottle])
def api_health(_request):
    return Response({"ok": True})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@throttle_classes([UserRateThrottle])
def api_readiness(_request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:
        log_api_error("api_readiness", 503, "db_unavailable")
        return Response({"ready": False, "db": "down"}, status=503)

    return Response(
        {
            "ready": True,
            "db": "up",
            "metrics": {
                "api_errors_total": metric_get("api_errors_total"),
                "import_limits_triggered_total": metric_get("import_limits_triggered_total"),
                "quote_limits_triggered_total": metric_get("quote_limits_triggered_total"),
            },
        }
    )

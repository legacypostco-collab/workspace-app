"""ТЗ §17.2: двусторонний обмен с 1С/ERP по REST.

Endpoints (auth — заголовок ``X-Api-Token`` против ``ApiToken``):

  POST /api/erp/sync/parts/             — 1С пушит обновления цен/остатков
       body: [{"oem_number":"…","price":…,"stock":…,"currency":"USD"}]
       resp: {"updated":N, "failed":N, "log_id":N}

  GET  /api/erp/sync/orders/?since=…    — 1С пуллит заказы своего seller'а,
                                          созданные после ``since`` (ISO date)
       resp: {"orders":[{id,total_amount,status,items:[…]}], "log_id":N}

  POST /api/erp/sync/orders/<id>/ack/   — 1С подтверждает приёмку заказа
       body: {"erp_order_id":"…","note":"…"}
       resp: {"ok":true, "log_id":N}

  POST /api/erp/sync/orders/<id>/status/ — 1С обновляет статус заказа
       body: {"status":"ready_to_ship","note":"…"}
       resp: {"ok":true, "log_id":N}

Все обращения логируются в ErpSyncLog (audit + идемпотентность).
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from marketplace.models import (
    ApiToken,
    ErpSyncLog,
    Order,
    OrderItem,
    Part,
)
from .security import token_has_permission

logger = logging.getLogger(__name__)


# ── Auth helper ────────────────────────────────────────────────

def _hash_token(plain: str) -> str:
    return hashlib.sha256(plain.encode("utf-8")).hexdigest()


def _auth_user(request):
    """Проверяет X-Api-Token, возвращает (user, token) или (None, None)."""
    token = (
        request.META.get("HTTP_X_API_TOKEN")
        or request.META.get("HTTP_AUTHORIZATION", "").removeprefix("Bearer ").strip()
    )
    if not token:
        return None, None
    rec = (ApiToken.objects.select_related("user")
           .filter(hashed_token=_hash_token(token), revoked_at__isnull=True)
           .first())
    if not rec:
        return None, None
    rec.last_used_at = timezone.now()
    rec.save(update_fields=["last_used_at"])
    return rec.user, rec


def _err(msg: str, status: int = 400, log: ErpSyncLog | None = None):
    if log:
        log.status = "failed"
        log.error = msg[:500]
        log.save(update_fields=["status", "error"])
    return JsonResponse({"error": msg}, status=status)


# ══════════════════════════════════════════════════════════
# 1. POST /api/erp/sync/parts/  — 1С пушит цены/остатки
# ══════════════════════════════════════════════════════════

@csrf_exempt
@require_http_methods(["POST"])
def sync_parts_push(request):
    user, token = _auth_user(request)
    if not user:
        return _err("Authentication required (X-Api-Token).", 401)
    if not token_has_permission(token, "write"):
        return _err("Token does not allow write operations.", 403)

    try:
        rows = json.loads(request.body or "[]")
    except json.JSONDecodeError as e:
        return _err(f"Invalid JSON: {e}")
    if not isinstance(rows, list):
        return _err("Body must be a JSON array of part-rows.")

    log = ErpSyncLog.objects.create(
        user=user, direction="pull", kind="parts", items_count=len(rows),
        payload={"sample": rows[:3]},
    )

    updated, failed = 0, 0
    failed_details = []
    for row in rows:
        try:
            oem = (row.get("oem_number") or row.get("article") or "").strip()
            if not oem:
                failed += 1
                failed_details.append({"row": row, "reason": "missing oem_number"})
                continue
            # Только parts этого seller'а — нельзя апдейтить чужой каталог
            p = Part.objects.filter(seller=user, oem_number__iexact=oem).first()
            if not p:
                failed += 1
                failed_details.append({"oem": oem, "reason": "not_found"})
                continue
            updated_fields = []
            if "price" in row:
                try:
                    new_price = Decimal(str(row["price"]))
                    if new_price > 0 and new_price != p.price:
                        p.price = new_price
                        updated_fields.append("price")
                except (InvalidOperation, TypeError):
                    pass
            if "stock" in row:
                try:
                    new_stock = int(row["stock"])
                    if new_stock >= 0 and new_stock != p.stock_quantity:
                        p.stock_quantity = new_stock
                        updated_fields.append("stock_quantity")
                except (ValueError, TypeError):
                    pass
            if "currency" in row and row["currency"] in {"USD", "EUR", "RUB", "CNY"}:
                if p.currency != row["currency"]:
                    p.currency = row["currency"]
                    updated_fields.append("currency")
            if updated_fields:
                p.data_updated_at = timezone.now()
                updated_fields.append("data_updated_at")
                p.save(update_fields=updated_fields)
                updated += 1
        except Exception as e:
            logger.exception("sync_parts row failed")
            failed += 1
            failed_details.append({"row": row, "reason": str(e)})

    log.status = ("failed" if updated == 0 and failed > 0 else
                  "partial" if failed > 0 else "ok")
    log.items_failed = failed
    if failed_details:
        log.payload = {**log.payload, "failed_details": failed_details[:20]}
    log.save(update_fields=["status", "items_failed", "payload"])

    return JsonResponse({
        "updated": updated, "failed": failed, "total": len(rows),
        "log_id": log.id,
    })


# ══════════════════════════════════════════════════════════
# 2. GET /api/erp/sync/orders/?since=YYYY-MM-DD
#    1С пуллит свои заказы для дальнейшей отгрузки
# ══════════════════════════════════════════════════════════

@csrf_exempt
@require_http_methods(["GET"])
def sync_orders_pull(request):
    user, token = _auth_user(request)
    if not user:
        return _err("Authentication required.", 401)
    if not token_has_permission(token, "read"):
        return _err("Token does not allow read operations.", 403)

    since_raw = request.GET.get("since")
    qs = (Order.objects
          .filter(items__part__seller=user,
                  status__in=("reserve_paid", "confirmed", "in_production",
                              "ready_to_ship"))
          .distinct().order_by("-created_at"))
    if since_raw:
        try:
            since = datetime.fromisoformat(since_raw.replace("Z", "+00:00"))
            if since.tzinfo is None:
                since = since.replace(tzinfo=UTC)
            qs = qs.filter(created_at__gte=since)
        except ValueError:
            return _err(f"Invalid 'since' date: {since_raw}")

    qs = qs[:200]
    orders_data = []
    for o in qs:
        seller_items = [oi for oi in o.items.select_related("part")
                        if oi.part and oi.part.seller_id == user.id]
        orders_data.append({
            "id": o.id,
            "external_ref": f"ORD-{o.id}",
            "buyer": o.customer_name,
            "status": o.status,
            "payment_status": o.payment_status,
            "total_amount": float(o.total_amount or 0),
            "created_at": o.created_at.isoformat() if o.created_at else None,
            "items": [{
                "oem_number": oi.part.oem_number if oi.part else "",
                "title":      oi.part.title if oi.part else "",
                "quantity":   oi.quantity,
                "unit_price": float(oi.unit_price or 0),
            } for oi in seller_items],
        })

    log = ErpSyncLog.objects.create(
        user=user, direction="push", kind="orders",
        items_count=len(orders_data), status="ok",
        payload={"sample_ids": [o["id"] for o in orders_data[:5]]},
    )
    return JsonResponse({"orders": orders_data, "log_id": log.id})


# ══════════════════════════════════════════════════════════
# 3. POST /api/erp/sync/orders/<id>/ack/  — приёмка от 1С
# ══════════════════════════════════════════════════════════

@csrf_exempt
@require_http_methods(["POST"])
def sync_order_ack(request, order_id):
    user, token = _auth_user(request)
    if not user:
        return _err("Authentication required.", 401)
    if not token_has_permission(token, "write"):
        return _err("Token does not allow write operations.", 403)
    try:
        order = Order.objects.get(id=order_id)
    except Order.DoesNotExist:
        return _err(f"Order {order_id} not found.", 404)

    # User must be a seller of at least one item in the order
    if not OrderItem.objects.filter(order=order, part__seller=user).exists():
        return _err("Order does not contain this seller's items.", 403)

    try:
        body = json.loads(request.body or "{}")
    except json.JSONDecodeError as e:
        return _err(f"Invalid JSON: {e}")
    erp_ref = (body.get("erp_order_id") or "").strip()
    note = (body.get("note") or "").strip()

    log = ErpSyncLog.objects.create(
        user=user, direction="pull", kind="order_ack",
        external_ref=erp_ref or f"ORD-{order.id}",
        items_count=1, status="ok",
        payload={"order_id": order.id, "erp_ref": erp_ref, "note": note[:200]},
    )

    # Запись в OrderEvent (audit)
    try:
        from marketplace.models import OrderEvent
        OrderEvent.objects.create(
            order=order, event_type="document_uploaded", actor=user,
            source="seller",
            meta={"kind": "erp_order_ack", "erp_order_id": erp_ref,
                  "note": note[:200], "log_id": log.id},
        )
    except Exception:
        logger.exception("OrderEvent create failed in sync_order_ack")

    return JsonResponse({"ok": True, "log_id": log.id})


# ══════════════════════════════════════════════════════════
# 4. POST /api/erp/sync/orders/<id>/status/  — обновление статуса
# ══════════════════════════════════════════════════════════

ALLOWED_STATUS_VALUES = {
    "confirmed", "in_production", "ready_to_ship", "transit_abroad",
    "customs", "transit_rf", "issuing", "delivered",
}

@csrf_exempt
@require_http_methods(["POST"])
def sync_order_status(request, order_id):
    user, token = _auth_user(request)
    if not user:
        return _err("Authentication required.", 401)
    if not token_has_permission(token, "write"):
        return _err("Token does not allow write operations.", 403)
    try:
        order = Order.objects.get(id=order_id)
    except Order.DoesNotExist:
        return _err(f"Order {order_id} not found.", 404)
    if not OrderItem.objects.filter(order=order, part__seller=user).exists():
        return _err("Forbidden.", 403)

    try:
        body = json.loads(request.body or "{}")
    except json.JSONDecodeError as e:
        return _err(f"Invalid JSON: {e}")
    new_status = (body.get("status") or "").strip()
    if new_status not in ALLOWED_STATUS_VALUES:
        return _err(f"Status not allowed: {new_status}")

    old = order.status
    order.status = new_status
    order.save(update_fields=["status"])

    log = ErpSyncLog.objects.create(
        user=user, direction="pull", kind="status",
        items_count=1, status="ok",
        payload={"order_id": order.id, "from": old, "to": new_status},
    )
    try:
        from marketplace.models import OrderEvent
        OrderEvent.objects.create(
            order=order, event_type="status_changed", actor=user, source="seller",
            meta={"kind": "erp_status_change", "from": old, "to": new_status,
                  "log_id": log.id},
        )
    except Exception:
        logger.exception("OrderEvent create failed in sync_order_status")

    return JsonResponse({"ok": True, "log_id": log.id, "status": new_status})

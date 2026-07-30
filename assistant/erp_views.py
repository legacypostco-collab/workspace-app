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

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import RequestDataTooBig
from django.db import transaction
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

ERP_MAX_BODY_BYTES = int(getattr(settings, "ERP_MAX_BODY_BYTES", 2 * 1024 * 1024))
ERP_MAX_SYNC_ROWS = int(getattr(settings, "ERP_MAX_SYNC_ROWS", 1_000))
ERP_RATE_LIMIT_PER_MINUTE = int(
    getattr(settings, "ERP_RATE_LIMIT_PER_MINUTE", 120)
)
MAX_PRICE = Decimal("9999999999.99")
MAX_STOCK = 2_147_483_647


class ErpPayloadError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


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
           .filter(
               hashed_token=_hash_token(token),
               revoked_at__isnull=True,
               user__is_active=True,
           )
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


def _rate_limit_exceeded(token: ApiToken, scope: str) -> bool:
    key = f"erp_rate:{token.pk}:{scope}"
    if cache.add(key, 1, timeout=60):
        return False
    try:
        count = cache.incr(key)
    except ValueError:
        cache.set(key, 1, timeout=60)
        count = 1
    return count > ERP_RATE_LIMIT_PER_MINUTE


def _read_json_body(request, expected_type):
    try:
        content_length = int(request.META.get("CONTENT_LENGTH") or 0)
    except (TypeError, ValueError):
        content_length = 0
    if content_length > ERP_MAX_BODY_BYTES:
        raise ErpPayloadError("Request body is too large.", 413)
    try:
        raw_body = request.body
    except RequestDataTooBig as exc:
        raise ErpPayloadError("Request body is too large.", 413) from exc
    if len(raw_body) > ERP_MAX_BODY_BYTES:
        raise ErpPayloadError("Request body is too large.", 413)
    try:
        payload = json.loads(raw_body or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ErpPayloadError("Invalid JSON.") from exc
    if not isinstance(payload, expected_type):
        expected = "array" if expected_type is list else "object"
        raise ErpPayloadError(f"Body must be a JSON {expected}.")
    return payload


def _bounded_text(value, max_length: int) -> str:
    text = value if isinstance(value, str) else ""
    return text.strip()[:max_length]


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
    if _rate_limit_exceeded(token, "parts_push"):
        return _err("Rate limit exceeded.", 429)

    try:
        rows = _read_json_body(request, list)
    except ErpPayloadError as exc:
        return _err(str(exc), exc.status)
    if len(rows) > ERP_MAX_SYNC_ROWS:
        return _err(
            f"Too many rows. Maximum is {ERP_MAX_SYNC_ROWS}.",
            413,
        )

    sample_oems = []
    for row in rows[:3]:
        if isinstance(row, dict):
            sample_oems.append(
                _bounded_text(row.get("oem_number") or row.get("article"), 100)
            )

    log = ErpSyncLog.objects.create(
        user=user, direction="pull", kind="parts", items_count=len(rows),
        payload={"sample_oems": sample_oems},
    )

    updated, failed = 0, 0
    failed_details = []
    for row_index, row in enumerate(rows):
        if not isinstance(row, dict):
            failed += 1
            failed_details.append({"index": row_index, "reason": "invalid_row"})
            continue
        try:
            oem = _bounded_text(
                row.get("oem_number") or row.get("article"),
                100,
            )
            if not oem:
                failed += 1
                failed_details.append(
                    {"index": row_index, "reason": "missing_oem_number"}
                )
                continue
            # Только parts этого seller'а — нельзя апдейтить чужой каталог
            p = Part.objects.filter(seller=user, oem_number__iexact=oem).first()
            if not p:
                failed += 1
                failed_details.append(
                    {"index": row_index, "oem": oem, "reason": "not_found"}
                )
                continue
            updated_fields = []
            if "price" in row:
                try:
                    new_price = Decimal(str(row["price"]))
                    if 0 < new_price <= MAX_PRICE and new_price != p.price:
                        p.price = new_price
                        updated_fields.append("price")
                except (InvalidOperation, TypeError, ValueError):
                    pass
            if "stock" in row:
                try:
                    new_stock = int(row["stock"])
                    if (
                        0 <= new_stock <= MAX_STOCK
                        and new_stock != p.stock_quantity
                    ):
                        p.stock_quantity = new_stock
                        updated_fields.append("stock_quantity")
                except (ValueError, TypeError, OverflowError):
                    pass
            currency = _bounded_text(row.get("currency"), 3).upper()
            if currency in {"USD", "EUR", "RUB", "CNY"}:
                if p.currency != currency:
                    p.currency = currency
                    updated_fields.append("currency")
            if updated_fields:
                p.data_updated_at = timezone.now()
                updated_fields.append("data_updated_at")
                p.save(update_fields=updated_fields)
                updated += 1
        except Exception:
            logger.exception(
                "sync_parts row failed user_id=%s row_index=%s",
                user.pk,
                row_index,
            )
            failed += 1
            failed_details.append(
                {"index": row_index, "oem": oem, "reason": "processing_error"}
            )

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
    if _rate_limit_exceeded(token, "orders_pull"):
        return _err("Rate limit exceeded.", 429)

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
            return _err("Invalid 'since' date.")

    qs = qs[:200]
    orders_data = []
    for o in qs:
        seller_items = [oi for oi in o.items.select_related("part")
                        if oi.part and oi.part.seller_id == user.id]
        seller_total = sum(
            (item.total_price for item in seller_items),
            Decimal("0.00"),
        )
        item_statuses = {
            item.status or o.status
            for item in seller_items
        }
        seller_status = (
            next(iter(item_statuses))
            if len(item_statuses) == 1
            else "mixed"
        )
        orders_data.append({
            "id": o.id,
            "external_ref": f"ORD-{o.id}",
            "buyer": "Buyer",
            "status": seller_status,
            "payment_status": o.payment_status,
            "total_amount": float(seller_total),
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
    if _rate_limit_exceeded(token, "order_ack"):
        return _err("Rate limit exceeded.", 429)
    try:
        order = Order.objects.get(id=order_id)
    except Order.DoesNotExist:
        return _err(f"Order {order_id} not found.", 404)

    # User must be a seller of at least one item in the order
    if not OrderItem.objects.filter(order=order, part__seller=user).exists():
        return _err("Order does not contain this seller's items.", 403)

    try:
        body = _read_json_body(request, dict)
    except ErpPayloadError as exc:
        return _err(str(exc), exc.status)
    erp_ref = _bounded_text(body.get("erp_order_id"), 120)
    note = _bounded_text(body.get("note"), 2_000)

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

ERP_STATUS_TRANSITIONS = {
    "reserve_paid": "confirmed",
    "confirmed": "in_production",
    "in_production": "ready_to_ship",
}

@csrf_exempt
@require_http_methods(["POST"])
def sync_order_status(request, order_id):
    user, token = _auth_user(request)
    if not user:
        return _err("Authentication required.", 401)
    if not token_has_permission(token, "write"):
        return _err("Token does not allow write operations.", 403)
    if _rate_limit_exceeded(token, "order_status"):
        return _err("Rate limit exceeded.", 429)
    try:
        body = _read_json_body(request, dict)
    except ErpPayloadError as exc:
        return _err(str(exc), exc.status)
    new_status = _bounded_text(body.get("status"), 40)
    try:
        from marketplace.models import OrderEvent

        with transaction.atomic():
            order = Order.objects.select_for_update().get(id=order_id)
            seller_items = OrderItem.objects.filter(
                order=order,
                part__seller=user,
            )
            if not seller_items.exists():
                return _err("Forbidden.", 403)
            if (
                OrderItem.objects.filter(order=order)
                .values("part__seller_id")
                .distinct()
                .count() > 1
            ):
                return _err(
                    "Multi-supplier order status is managed per item.",
                    409,
                )
            expected_status = ERP_STATUS_TRANSITIONS.get(order.status)
            if new_status != expected_status:
                return _err(
                    f"Status transition not allowed: {order.status} -> {new_status}. "
                    "ERP can only move seller production stages one step at a time."
                )
            old = order.status
            order.status = new_status
            order.save(update_fields=["status"])
            log = ErpSyncLog.objects.create(
                user=user, direction="pull", kind="status",
                items_count=1, status="ok",
                payload={"order_id": order.id, "from": old, "to": new_status},
            )
            OrderEvent.objects.create(
                order=order, event_type="status_changed", actor=user, source="seller",
                meta={"kind": "erp_status_change", "from": old, "to": new_status,
                      "log_id": log.id},
            )
    except Order.DoesNotExist:
        return _err(f"Order {order_id} not found.", 404)
    except Exception:
        logger.exception("Atomic ERP status update failed order=%s", order_id)
        return _err("Failed to update order status.", 500)

    return JsonResponse({"ok": True, "log_id": log.id, "status": new_status})

"""ТЗ §6.2: QR-scan endpoint для приёмки/отгрузки/проверки.

Поток:
  1. Заказ имеет QR (генерируется generate_qr action) — содержит
     scan-URL: /api/assistant/qr/scan/<code>/
  2. Логист на складе сканирует код через мобильник
  3. Endpoint находит Order по коду + текущий status seller'а →
     записывает событие (OrderEvent + SLA recalc)
  4. Возвращает простую страницу с подтверждением

QR-коды формата ORD-<id>-<hash> где hash = HMAC-SHA256(secret, order_id)[:32].
QR_SECRET обязателен во всех режимах.

API:
  GET  /api/assistant/qr/scan/<code>/  — простая HTML-страница «вы успешно
                                          отсканировали; событие записано»
  POST /api/assistant/qr/scan/<code>/  — JSON для мобильного клиента
       Body: {action: 'received'|'shipped'|'inspected'|'delivered'}
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re

from django.conf import settings
from django.core.cache import cache
from django.http import HttpResponse, JsonResponse
from django.middleware.csrf import get_token
from django.utils import timezone
from django.views.generic import View

logger = logging.getLogger(__name__)


# ── Code generation / verification ────────────────────────────

_CODE_RE = re.compile(r"^ORD-(\d+)-([a-f0-9]{32})$")


def _secret() -> str | None:
    secret = (os.getenv("QR_SECRET") or getattr(settings, "QR_SECRET", "") or "").strip()
    if secret:
        return secret
    return None


def encode_qr_code(order_id: int) -> str:
    """ORD-<id>-<hmac32>."""
    secret = _secret()
    if not secret:
        raise RuntimeError("QR_SECRET is required")
    h = hmac.new(secret.encode(), str(order_id).encode(), hashlib.sha256).hexdigest()[:32]
    return f"ORD-{order_id}-{h}"


def decode_qr_code(code: str) -> int | None:
    """Возвращает order_id если код валиден, иначе None."""
    m = _CODE_RE.match((code or "").strip())
    if not m:
        return None
    order_id = int(m.group(1))
    try:
        expected = encode_qr_code(order_id)
    except RuntimeError:
        return None
    if not hmac.compare_digest(expected, code):
        return None
    return order_id


# ── Action mapping: scan_action → status transition ──────────

# Допустимые scan-actions и их влияние на Order.status
SCAN_TRANSITIONS = {
    "shipped":   ("ready_to_ship", "shipped"),
    "transit":   ("shipped", "transit_abroad"),
    "customs":   ("transit_abroad", "customs"),
    "delivered": ("transit_rf", "delivered"),
    "received":  ("delivered", "completed"),  # buyer scans = receipt confirmed
    "inspected": (None, None),   # event-only (записываем audit, статус не меняется)
}


def _scan_permission(user, order, action: str) -> tuple[bool, str]:
    from assistant.permissions import user_allowed_roles
    from marketplace.order_access import seller_ids_for_order, seller_principal

    roles = set(user_allowed_roles(user))
    is_operator = "admin" in roles or any(
        role == "operator" or role.startswith("operator_")
        for role in roles
    )
    is_buyer = order.buyer_id == user.id
    sellers = seller_ids_for_order(order)
    is_seller = seller_principal(user).id in sellers

    if action == "received":
        return is_buyer, "buyer"
    if action in {"transit", "customs", "delivered"}:
        return is_operator, "operator"
    if action == "shipped":
        allowed = is_operator or (is_seller and len(sellers) == 1)
        return allowed, "operator" if is_operator else "seller"
    if action == "inspected":
        allowed = is_operator or is_buyer or is_seller
        if is_operator:
            return allowed, "operator"
        return allowed, "buyer" if is_buyer else "seller"
    return False, ""


# ── View ─────────────────────────────────────────────────────

class QRScanView(View):
    """GET/POST /api/assistant/qr/scan/<code>/

    GET — для пользователя со смартфона: HTML-страница с кнопками
         «Отгружено» / «Получено» (зависит от текущего status'а заказа)
    POST — для мобильного клиента: JSON {action: 'received'|...} → событие
    """

    def get(self, request, code):
        from marketplace.models import Order
        if self._rate_limited(request, code):
            return self._html_error("Слишком много попыток. Попробуйте позже.", status=429)
        order_id = decode_qr_code(code)
        if not order_id:
            return self._html_error("Неверный QR-код")
        try:
            order = Order.objects.get(id=order_id)
        except Order.DoesNotExist:
            return self._html_error(f"Заказ #{order_id} не найден")
        if not request.user.is_authenticated:
            return self._html_error(
                "Войдите в систему под аккаунтом участника поставки.",
                status=401,
            )
        if not _scan_permission(request.user, order, "inspected")[0]:
            return self._html_error("Доступ к заказу запрещён.", status=403)

        # Какие действия доступны на этом этапе
        available = []
        for action, (need_status, _) in SCAN_TRANSITIONS.items():
            allowed = _scan_permission(request.user, order, action)[0]
            if allowed and (need_status is None or order.status == need_status):
                available.append(action)

        csrf_token = get_token(request)
        action_buttons = "".join(
            f'<form method="post" style="display:inline;margin:4px;">'
            f'<input type="hidden" name="action" value="{a}">'
            f'<input type="hidden" name="csrfmiddlewaretoken" value="{csrf_token}">'
            f'<button type="submit" style="padding:14px 22px;background:#1a1a1a;color:#fff;'
            f'border:none;border-radius:10px;font-size:16px;font-weight:700;'
            f'font-family:system-ui,sans-serif;cursor:pointer;">'
            f'{a.upper()}</button></form>'
            for a in available
        )
        return HttpResponse(
            f"""<!DOCTYPE html><html><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>QR · Order #{order_id}</title></head>
<body style="font-family:system-ui;padding:30px;max-width:600px;margin:0 auto;background:#f5f5f5;">
<h1>📦 Заказ #{order_id}</h1>
<p style="color:#666;">Статус: {order.get_status_display()}</p>
<hr style="margin:24px 0;border:none;border-top:1px solid #ddd;">
{action_buttons}
</body></html>""",
            content_type="text/html; charset=utf-8",
        )

    def post(self, request, code):
        from marketplace.models import Order, OrderEvent
        if not request.user.is_authenticated:
            return JsonResponse({"ok": False, "error": "authentication required"}, status=401)
        if self._rate_limited(request, code):
            return JsonResponse({"ok": False, "error": "rate limited"}, status=429)
        order_id = decode_qr_code(code)
        if not order_id:
            return JsonResponse({"ok": False, "error": "invalid code"}, status=400)
        try:
            order = Order.objects.get(id=order_id)
        except Order.DoesNotExist:
            return JsonResponse({"ok": False, "error": "order not found"}, status=404)

        scan_action = (request.POST.get("action")
                       or (request.body and self._parse_json_action(request.body)) or "").strip()
        if scan_action not in SCAN_TRANSITIONS:
            return JsonResponse({"ok": False, "error": f"unknown action {scan_action!r}"},
                                  status=400)

        need_status, target_status = SCAN_TRANSITIONS[scan_action]
        if need_status and order.status != need_status:
            return JsonResponse({
                "ok": False,
                "error": (
                    f"Действие {scan_action!r} требует статус '{need_status}', "
                    f"текущий '{order.status}'."
                ),
            }, status=409)
        allowed, actor_source = _scan_permission(
            request.user,
            order,
            scan_action,
        )
        if not allowed:
            return JsonResponse(
                {"ok": False, "error": "action not allowed"},
                status=403,
            )
        if scan_action == "shipped" and order.payment_status != "paid":
            return JsonResponse(
                {"ok": False, "error": "order is not fully paid"},
                status=409,
            )
        from .security import client_ip

        if scan_action == "received":
            from .actions import confirm_delivery

            result = confirm_delivery(
                {"order_id": order.id, "confirmed": True},
                request.user,
                "buyer",
            )
            order.refresh_from_db(fields=["status"])
            if order.status != "completed":
                return JsonResponse(
                    {"ok": False, "error": str(result.text)},
                    status=409,
                )
            OrderEvent.objects.create(
                order=order,
                event_type="quality_confirmed",
                source="buyer",
                actor=request.user,
                meta={
                    "qr_scan_action": scan_action,
                    "ip": client_ip(request)[:64],
                },
            )
            return JsonResponse({
                "ok": True,
                "order_id": order_id,
                "scan_action": scan_action,
                "from_status": "delivered",
                "to_status": "completed",
                "scanned_at": timezone.now().isoformat(),
            })

        # Записать event и статус атомарно: параллельные сканы не должны
        # дважды провести один этап.
        from django.db import transaction

        with transaction.atomic():
            order = Order.objects.select_for_update().get(id=order.id)
            if need_status and order.status != need_status:
                return JsonResponse(
                    {"ok": False, "error": "order status already changed"},
                    status=409,
                )
            old_status = order.status
            OrderEvent.objects.create(
                order=order,
                event_type="status_changed",
                source=actor_source,
                actor=request.user,
                meta={
                    "qr_scan_action": scan_action,
                    "from": old_status,
                    "to": target_status,
                    "ip": client_ip(request)[:64],
                },
            )
            if target_status:
                order.status = target_status
                order.save(update_fields=["status"])

        return JsonResponse({
            "ok": True,
            "order_id": order_id,
            "scan_action": scan_action,
            "from_status": need_status or old_status,
            "to_status": target_status or order.status,
            "scanned_at": timezone.now().isoformat(),
        })

    def _parse_json_action(self, body: bytes) -> str:
        import json
        try:
            data = json.loads(body or b"{}")
            return data.get("action") or ""
        except Exception:
            return ""

    def _rate_limited(self, request, code: str) -> bool:
        from .security import client_ip

        ip = client_ip(request)
        key = f"qr-scan:{ip}:{hashlib.sha256((code or '').encode()).hexdigest()[:16]}"
        try:
            hits = cache.get(key, 0)
            if hits >= 20:
                return True
            cache.set(key, hits + 1, 900)
        except Exception:
            return False
        return False

    def _html_error(self, msg: str, status: int = 400) -> HttpResponse:
        return HttpResponse(
            f"""<!DOCTYPE html><html><body style="font-family:system-ui;padding:30px;">
<h1>⚠️ Ошибка</h1><p>{msg}</p></body></html>""",
            status=status, content_type="text/html; charset=utf-8",
        )

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
import secrets

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
    "delivered": ("issuing", "delivered"),
    "received":  ("delivered", None),
    "inspected": (None, None),   # event-only (записываем audit, статус не меняется)
}


def _qr_trigger_for_status(status: str, action: str) -> str | None:
    if action == "received" and status == "delivered":
        return "qr_received"
    if status == "ready_to_ship" and action in {"inspected", "shipped"}:
        return "fob_handoff_qr"
    if status == "transit_rf" and action == "inspected":
        return "qr_rf"
    if status == "issuing" and action in {"inspected", "delivered"}:
        return "qr_issuing"
    return None


def _record_qr_evidence(order, trigger_id: str, *, actor, actor_source: str, ip: str):
    meta = order.logistics_meta or {}
    triggers = meta.get("triggers") or {}
    stage_triggers = triggers.get(order.status) or {}
    stage_triggers[trigger_id] = {
        "completed_at": timezone.now().isoformat(),
        "kind": "qr_scan",
        "actor_id": actor.id,
        "actor_role": actor_source,
        "ip": ip[:64],
    }
    triggers[order.status] = stage_triggers
    meta["triggers"] = triggers
    order.logistics_meta = meta


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
        if order.status == "ready_to_ship":
            allowed = is_operator or (is_seller and len(sellers) == 1)
            return allowed, "operator" if is_operator else "seller"
        if order.status in {"transit_rf", "issuing"}:
            return is_operator, "operator"
        return False, ""
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
        can_view = any(
            _scan_permission(request.user, order, action)[0]
            for action in SCAN_TRANSITIONS
        )
        if not can_view:
            return self._html_error("Доступ к заказу запрещён.", status=403)

        # Какие действия доступны на этом этапе
        available = []
        for action, (need_status, _) in SCAN_TRANSITIONS.items():
            allowed = _scan_permission(request.user, order, action)[0]
            if allowed and (need_status is None or order.status == need_status):
                available.append(action)

        csrf_token = get_token(request)
        action_buttons = "".join(
            f'<form method="post" class="qr-action">'
            f'<input type="hidden" name="action" value="{a}">'
            f'<input type="hidden" name="csrfmiddlewaretoken" value="{csrf_token}">'
            f'<button type="submit" class="qr-button">'
            f'{a.upper()}</button></form>'
            for a in available
        )
        return self._html_response(
            f'<h1>Заказ #{order_id}</h1>'
            f'<p class="qr-status">Статус: {order.get_status_display()}</p>'
            f'<hr>{action_buttons}',
            title=f"QR · Заказ #{order_id}",
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
            scan_ip = client_ip(request)[:64]
            from django.db import transaction

            with transaction.atomic():
                locked_order = Order.objects.select_for_update().get(id=order.id)
                if locked_order.status != "delivered":
                    return JsonResponse(
                        {"ok": False, "error": "order status already changed"},
                        status=409,
                    )
                _record_qr_evidence(
                    locked_order,
                    "qr_received",
                    actor=request.user,
                    actor_source="buyer",
                    ip=scan_ip,
                )
                locked_order.save(update_fields=["logistics_meta"])
                OrderEvent.objects.create(
                    order=locked_order,
                    event_type="quality_confirmed",
                    source="buyer",
                    actor=request.user,
                    meta={
                        "qr_scan_action": scan_action,
                        "trigger_id": "qr_received",
                        "ip": scan_ip,
                    },
                )

            return JsonResponse({
                "ok": True,
                "order_id": order_id,
                "scan_action": scan_action,
                "from_status": "delivered",
                "to_status": "delivered",
                "trigger_id": "qr_received",
                "requires_confirmation": True,
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
            scan_ip = client_ip(request)[:64]
            trigger_id = _qr_trigger_for_status(old_status, scan_action)
            if trigger_id:
                _record_qr_evidence(
                    order,
                    trigger_id,
                    actor=request.user,
                    actor_source=actor_source,
                    ip=scan_ip,
                )
            OrderEvent.objects.create(
                order=order,
                event_type="status_changed",
                source=actor_source,
                actor=request.user,
                meta={
                    "qr_scan_action": scan_action,
                    "from": old_status,
                    "to": target_status,
                    "trigger_id": trigger_id,
                    "ip": scan_ip,
                },
            )
            if scan_action == "shipped":
                from .actions import _stage_checklist, _verified_trigger_ids

                ready_checklist = _stage_checklist(
                    "ready_to_ship",
                    order.incoterm or "FOB",
                )
                stage_done = _verified_trigger_ids(
                    order,
                    "ready_to_ship",
                    ready_checklist,
                )
                missing = [
                    item for item in ready_checklist
                    if item["id"] not in stage_done
                ]
                if missing:
                    order.save(update_fields=["logistics_meta"])
                    return JsonResponse(
                        {
                            "ok": False,
                            "error": "ready checklist is incomplete",
                            "missing": [item["id"] for item in missing],
                            "trigger_id": trigger_id,
                        },
                        status=409,
                    )
            if target_status:
                order.status = target_status
                order.save(update_fields=["status", "logistics_meta"])
            elif trigger_id:
                order.save(update_fields=["logistics_meta"])

        return JsonResponse({
            "ok": True,
            "order_id": order_id,
            "scan_action": scan_action,
            "from_status": need_status or old_status,
            "to_status": target_status or order.status,
            "trigger_id": trigger_id,
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
        from html import escape

        return self._html_response(
            f"<h1>Ошибка</h1><p>{escape(msg)}</p>",
            title="Ошибка QR-кода",
            status=status,
        )

    @staticmethod
    def _html_response(body: str, *, title: str, status: int = 200) -> HttpResponse:
        from html import escape

        nonce = secrets.token_urlsafe(24)
        css = (
            "body{font-family:system-ui,sans-serif;padding:30px;max-width:600px;"
            "margin:0 auto;background:#f5f5f5;color:#1a1a1a}"
            ".qr-status{color:#666}hr{margin:24px 0;border:0;border-top:1px solid #ddd}"
            ".qr-action{display:inline;margin:4px}.qr-button{padding:14px 22px;"
            "background:#1a1a1a;color:#fff;border:0;border-radius:10px;font-size:16px;"
            "font-weight:700;font-family:system-ui,sans-serif;cursor:pointer}"
        )
        response = HttpResponse(
            '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{escape(title)}</title><style nonce="{nonce}">{css}</style>'
            f'</head><body>{body}</body></html>',
            status=status,
            content_type="text/html; charset=utf-8",
        )
        response["Content-Security-Policy"] = (
            f"default-src 'none'; style-src 'nonce-{nonce}'; "
            "style-src-attr 'none'; script-src 'none'; frame-ancestors 'none'"
        )
        return response

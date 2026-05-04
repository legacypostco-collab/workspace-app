"""Operator-specific chat actions: dashboard, queue, assignment, dispute, notes.

Operator-кабинет в chat-first парадигме: оператор не ведёт свой каталог и не
оплачивает заказы — он надзирает за процессом, перераспределяет задачи между
суб-ролями (logist / customs / payments / manager) и закрывает спорные кейсы.

Все writing-действия идут через DraftCard preview → confirm.
Все события ложатся в OrderEvent с meta={"kind": ...} для аудита и для
подсчётов в `op_dashboard`.
"""
from __future__ import annotations

import logging
from decimal import Decimal

from django.utils import timezone

from .actions import ActionResult, _log_event, _notify, register

logger = logging.getLogger(__name__)


# ── Канонические значения ─────────────────────────────────────
OP_SUBROLES = ("manager", "logist", "customs", "payments")
DISPUTE_RESOLUTIONS = ("refund", "release", "no_action", "partial_refund")

# Open-statuses — те, что считаем «в работе»
OPEN_STATUSES = (
    "pending", "reserve_paid", "confirmed", "in_production",
    "ready_to_ship", "transit_abroad", "customs", "transit_rf", "issuing",
)


def _is_operator(role: str) -> bool:
    return bool(role) and (role == "operator" or role.startswith("operator_"))


def _ensure_operator(role: str):
    if not _is_operator(role):
        return ActionResult(text="Это действие доступно только оператору.")
    return None


# ══════════════════════════════════════════════════════════
# 1. Dashboard — операторская сводка
# ══════════════════════════════════════════════════════════

@register("op_dashboard")
def op_dashboard(params, user, role):
    err = _ensure_operator(role)
    if err:
        return err
    from marketplace.models import Order

    in_flight = Order.objects.filter(status__in=OPEN_STATUSES).count()
    at_risk = Order.objects.filter(sla_status="at_risk").count()
    breached = Order.objects.filter(sla_status="breached").count()
    refund_pending = Order.objects.filter(payment_status="refund_pending").count()
    awaiting_reserve = Order.objects.filter(payment_status="awaiting_reserve").count()

    # Total $ в обороте — сумма по открытым
    total_in_flight = Order.objects.filter(status__in=OPEN_STATUSES).values_list("total_amount", flat=True)
    total_usd = sum((Decimal(x) for x in total_in_flight), Decimal("0"))

    # Recent priority — топ 5 «горячих»
    hot = (
        Order.objects.filter(sla_status__in=("at_risk", "breached"))
        .order_by("sla_status", "-created_at")[:5]
    )
    rows = [
        {
            "id": o.id,
            "status": o.get_status_display(),
            "sla": o.sla_status,
            "buyer": o.customer_name,
            "total": float(o.total_amount or 0),
            "url": f"/chat/?order={o.id}",
        }
        for o in hot
    ]

    return ActionResult(
        text=(
            f"Сводка оператора · в работе {in_flight} заказов на ${total_usd:,.0f}.\n"
            f"SLA: {at_risk} под угрозой, {breached} нарушено · возвратов в обработке: {refund_pending}."
        ),
        cards=[
            {
                "type": "kpi_grid",
                "data": {
                    "title": "Операторская панель",
                    "items": [
                        {"label": "В работе", "value": str(in_flight), "tone": "info"},
                        {"label": "SLA: под угрозой", "value": str(at_risk), "tone": "warn" if at_risk else "ok"},
                        {"label": "SLA: нарушено", "value": str(breached), "tone": "bad" if breached else "ok"},
                        {"label": "Ожидает резерва", "value": str(awaiting_reserve)},
                        {"label": "Возвраты", "value": str(refund_pending), "tone": "warn" if refund_pending else "ok"},
                        {"label": "Оборот ($)", "value": f"{total_usd:,.0f}"},
                    ],
                },
            },
            {
                "type": "list",
                "data": {
                    "title": "🔥 Приоритетная очередь",
                    "items": [
                        {
                            "title": f"#{r['id']} · {r['buyer']}",
                            "subtitle": f"{r['status']} · SLA {r['sla']} · ${r['total']:,.0f}",
                            "url": r["url"],
                        }
                        for r in rows
                    ] or [{"title": "Нет горящих заказов", "subtitle": "Все SLA в норме"}],
                },
            },
        ],
        contextual_actions=[
            {"action": "op_queue", "label": "📋 Полная очередь", "params": {"filter": "all"}},
            {"action": "op_sla_breach", "label": "⏱ Нарушения SLA"},
            {"action": "get_analytics", "label": "📈 Аналитика"},
        ],
        suggestions=[
            "Покажи очередь нарушений SLA",
            "Какие заказы ждут резерва дольше суток",
            "Сводка по таможне за неделю",
        ],
    )


# ══════════════════════════════════════════════════════════
# 2. Queue — очередь требующих внимания
# ══════════════════════════════════════════════════════════

@register("op_queue")
def op_queue(params, user, role):
    err = _ensure_operator(role)
    if err:
        return err
    from marketplace.models import Order

    flt = (params.get("filter") or "all").strip().lower()
    qs = Order.objects.all()
    if flt == "breached":
        qs = qs.filter(sla_status="breached")
    elif flt == "at_risk":
        qs = qs.filter(sla_status="at_risk")
    elif flt == "refund":
        qs = qs.filter(payment_status="refund_pending")
    elif flt == "awaiting_reserve":
        qs = qs.filter(payment_status="awaiting_reserve")
    elif flt == "open":
        qs = qs.filter(status__in=OPEN_STATUSES)
    # else: all
    qs = qs.order_by("sla_status", "-created_at")[:20]

    items = [
        {
            "title": f"#{o.id} · {o.customer_name}",
            "subtitle": (
                f"{o.get_status_display()} · {o.get_payment_status_display()} · "
                f"SLA {o.sla_status} · ${(o.total_amount or 0):,.0f}"
            ),
            "url": f"/chat/?order={o.id}",
        }
        for o in qs
    ]

    return ActionResult(
        text=f"Очередь · фильтр «{flt}» · {len(items)} заказов.",
        cards=[{
            "type": "list",
            "data": {
                "title": f"📋 Очередь · {flt}",
                "items": items or [{"title": "Пусто", "subtitle": "Под этот фильтр ничего не попадает"}],
            },
        }],
        contextual_actions=[
            {"action": "op_queue", "label": "Все", "params": {"filter": "all"}},
            {"action": "op_queue", "label": "Нарушенные SLA", "params": {"filter": "breached"}},
            {"action": "op_queue", "label": "Под угрозой", "params": {"filter": "at_risk"}},
            {"action": "op_queue", "label": "Возвраты", "params": {"filter": "refund"}},
        ],
    )


# ══════════════════════════════════════════════════════════
# 3. SLA breaches — список + причины
# ══════════════════════════════════════════════════════════

@register("op_sla_breach")
def op_sla_breach(params, user, role):
    err = _ensure_operator(role)
    if err:
        return err
    from marketplace.models import Order
    from django.utils import timezone as tz

    now = tz.now()
    qs = Order.objects.filter(sla_status__in=("at_risk", "breached")).order_by("sla_status", "ship_deadline")[:30]
    rows = []
    for o in qs:
        dl = o.ship_deadline
        delta = None
        if dl:
            delta_seconds = int((now - dl).total_seconds())
            if delta_seconds > 0:
                delta = f"+{delta_seconds // 3600}ч просрочки"
            else:
                delta = f"{(-delta_seconds) // 3600}ч до дедлайна"
        rows.append({
            "title": f"#{o.id} · {o.customer_name}",
            "subtitle": f"{o.get_status_display()} · {o.sla_status} · {delta or 'нет дедлайна'}",
            "url": f"/chat/?order={o.id}",
        })

    return ActionResult(
        text=f"Нарушения SLA · {len(rows)} заказов.",
        cards=[{
            "type": "list",
            "data": {"title": "⏱ SLA: нарушенные и под угрозой", "items": rows or [{"title": "Все SLA в норме"}]},
        }],
    )


# ══════════════════════════════════════════════════════════
# 4. Order detail (operator-view) — расширенный
# ══════════════════════════════════════════════════════════

def _latest_assignment(order):
    """Последний `op_assigned` event на этом заказе."""
    from marketplace.models import OrderEvent
    e = OrderEvent.objects.filter(
        order=order, event_type="operator_action", meta__kind="assigned"
    ).order_by("-created_at").first()
    if not e:
        return None
    return {
        "to_role": e.meta.get("to_role"),
        "to_user_id": e.meta.get("to_user_id"),
        "by": e.actor.username if e.actor else "—",
        "at": e.created_at.isoformat(),
    }


@register("op_order_detail")
def op_order_detail(params, user, role):
    err = _ensure_operator(role)
    if err:
        return err
    from marketplace.models import Order, OrderEvent
    try:
        order = Order.objects.get(id=int(params.get("order_id") or 0))
    except (Order.DoesNotExist, ValueError, TypeError):
        return ActionResult(text="Заказ не найден.")

    assignment = _latest_assignment(order)
    events = OrderEvent.objects.filter(order=order).order_by("-created_at")[:15]

    asg_line = (
        f"Назначен: {assignment['to_role']} (by {assignment['by']})"
        if assignment else "Не назначен оператору"
    )

    return ActionResult(
        text=(
            f"Заказ #{order.id} · {order.customer_name}\n"
            f"{order.get_status_display()} · {order.get_payment_status_display()} · SLA {order.sla_status}\n"
            f"${(order.total_amount or 0):,.0f} · {asg_line}"
        ),
        cards=[{
            "type": "list",
            "data": {
                "title": f"📜 Аудит #{order.id}",
                "items": [
                    {
                        "title": e.event_type,
                        "subtitle": f"{e.source} · {e.actor.username if e.actor else 'system'} · {e.created_at:%Y-%m-%d %H:%M}",
                    }
                    for e in events
                ],
            },
        }],
        contextual_actions=[
            {"action": "op_assign", "label": "👥 Назначить", "params": {"order_id": order.id}},
            {"action": "op_add_note", "label": "📝 Заметка", "params": {"order_id": order.id}},
            {"action": "op_resolve_dispute", "label": "⚖️ Закрыть спор", "params": {"order_id": order.id}},
            {"action": "track_order", "label": "📦 Трекинг", "params": {"order_id": order.id}},
        ],
    )


# ══════════════════════════════════════════════════════════
# 5. Assign — назначить оператора (writing → DraftCard)
# ══════════════════════════════════════════════════════════

@register("op_assign")
def op_assign(params, user, role):
    err = _ensure_operator(role)
    if err:
        return err
    from marketplace.models import Order

    try:
        order = Order.objects.get(id=int(params.get("order_id") or 0))
    except (Order.DoesNotExist, ValueError, TypeError):
        return ActionResult(text="Заказ не найден.")

    to_role = (params.get("to_role") or "").strip().lower()
    confirmed = bool(params.get("confirmed"))

    # Шаг 1: предпросмотр (DraftCard с формой)
    if not confirmed or to_role not in OP_SUBROLES:
        return ActionResult(
            text=f"Кому назначить заказ #{order.id}?",
            cards=[{
                "type": "form",
                "data": {
                    "title": f"👥 Назначение · #{order.id}",
                    "submit_action": "op_assign",
                    "fields": [
                        {
                            "name": "to_role", "label": "Суб-роль", "required": True,
                            "type": "select",
                            "options": [
                                {"value": "manager", "label": "Менеджер"},
                                {"value": "logist", "label": "Логист"},
                                {"value": "customs", "label": "Таможня"},
                                {"value": "payments", "label": "Платежи"},
                            ],
                        },
                        {"name": "comment", "label": "Комментарий (необязательно)"},
                    ],
                    "fixed_params": {"order_id": order.id, "confirmed": True},
                },
            }],
        )

    # Шаг 2: запись
    comment = (params.get("comment") or "").strip()
    _log_event(
        order, "operator_action", actor=user, source="operator",
        meta={"kind": "assigned", "to_role": to_role, "by": user.username, "comment": comment},
    )
    # Уведомим самого оператора (что он теперь owner) — best-effort: ищем
    # любого пользователя с username demo_operator или с operator_role==to_role.
    try:
        from django.contrib.auth import get_user_model
        target = (
            get_user_model().objects.filter(username="demo_operator").first()
            or get_user_model().objects.filter(is_staff=True).first()
        )
        if target:
            _notify(
                target, kind="order",
                title=f"Вам назначен заказ #{order.id}",
                body=f"{user.username} назначил роль «{to_role}» по заказу {order.customer_name}.",
                url=f"/chat/?order={order.id}",
            )
    except Exception:
        logger.exception("op_assign notify failed")

    return ActionResult(
        text=f"✓ Заказ #{order.id} назначен на «{to_role}».",
        contextual_actions=[
            {"action": "op_order_detail", "label": "Открыть заказ", "params": {"order_id": order.id}},
            {"action": "op_dashboard", "label": "← Сводка"},
        ],
    )


# ══════════════════════════════════════════════════════════
# 6. Add note — операторская заметка (writing → DraftCard)
# ══════════════════════════════════════════════════════════

@register("op_add_note")
def op_add_note(params, user, role):
    err = _ensure_operator(role)
    if err:
        return err
    from marketplace.models import Order

    try:
        order = Order.objects.get(id=int(params.get("order_id") or 0))
    except (Order.DoesNotExist, ValueError, TypeError):
        return ActionResult(text="Заказ не найден.")

    text = (params.get("text") or "").strip()
    confirmed = bool(params.get("confirmed"))

    if not confirmed or not text:
        return ActionResult(
            text=f"Заметка к заказу #{order.id}",
            cards=[{
                "type": "form",
                "data": {
                    "title": f"📝 Заметка · #{order.id}",
                    "submit_action": "op_add_note",
                    "fields": [
                        {"name": "text", "label": "Текст", "type": "textarea", "required": True},
                    ],
                    "fixed_params": {"order_id": order.id, "confirmed": True},
                },
            }],
        )

    _log_event(
        order, "operator_action", actor=user, source="operator",
        meta={"kind": "note", "text": text, "by": user.username},
    )
    return ActionResult(
        text=f"✓ Заметка к #{order.id} сохранена.",
        contextual_actions=[
            {"action": "op_order_detail", "label": "Открыть заказ", "params": {"order_id": order.id}},
        ],
    )


# ══════════════════════════════════════════════════════════
# 7. Resolve dispute — закрыть спор (writing → DraftCard)
# ══════════════════════════════════════════════════════════

@register("op_resolve_dispute")
def op_resolve_dispute(params, user, role):
    err = _ensure_operator(role)
    if err:
        return err
    from marketplace.models import Order

    try:
        order = Order.objects.get(id=int(params.get("order_id") or 0))
    except (Order.DoesNotExist, ValueError, TypeError):
        return ActionResult(text="Заказ не найден.")

    resolution = (params.get("resolution") or "").strip().lower()
    refund_amount_raw = params.get("refund_amount") or "0"
    confirmed = bool(params.get("confirmed"))

    if not confirmed or resolution not in DISPUTE_RESOLUTIONS:
        return ActionResult(
            text=f"Резолюция по спору · #{order.id}",
            cards=[{
                "type": "form",
                "data": {
                    "title": f"⚖️ Закрыть спор · #{order.id}",
                    "submit_action": "op_resolve_dispute",
                    "fields": [
                        {
                            "name": "resolution", "label": "Решение", "required": True,
                            "type": "select",
                            "options": [
                                {"value": "refund", "label": "Полный возврат"},
                                {"value": "partial_refund", "label": "Частичный возврат"},
                                {"value": "release", "label": "Выпустить платёж продавцу"},
                                {"value": "no_action", "label": "Закрыть без действий"},
                            ],
                        },
                        {"name": "refund_amount", "label": "Сумма возврата ($)", "type": "number"},
                        {"name": "reason", "label": "Обоснование", "type": "textarea", "required": True},
                    ],
                    "fixed_params": {"order_id": order.id, "confirmed": True},
                },
            }],
        )

    try:
        refund_amount = Decimal(str(refund_amount_raw)) if refund_amount_raw else Decimal("0")
    except Exception:
        refund_amount = Decimal("0")
    reason = (params.get("reason") or "").strip()

    # Side-effects: статус оплаты + реальное движение эскроу
    from . import payments as _pay
    from marketplace.models import OrderItem

    money_moved = ""
    new_payment_status = order.payment_status
    try:
        if resolution == "refund":
            res = _pay.refund_to_buyer(order=order, buyer=order.buyer)
            if res.get("ok"):
                money_moved = f" · возврат ${res['amount']:,.2f} → покупатель"
            new_payment_status = "refunded"
        elif resolution == "partial_refund":
            if refund_amount > 0 and order.buyer:
                res = _pay.refund_to_buyer(order=order, buyer=order.buyer, amount=refund_amount)
                if res.get("ok"):
                    money_moved = f" · возврат ${res['amount']:,.2f} → покупатель"
            new_payment_status = "refund_pending"
        elif resolution == "release":
            seller_obj = None
            first = OrderItem.objects.filter(order=order).select_related("part__seller").first()
            if first and first.part:
                seller_obj = first.part.seller
            if seller_obj:
                res = _pay.release_to_seller(order=order, seller=seller_obj)
                if res.get("ok"):
                    money_moved = f" · выплата ${res['amount']:,.2f} → продавец"
            new_payment_status = "paid"
    except Exception:
        logger.exception("escrow move on dispute resolution failed")

    if new_payment_status != order.payment_status:
        order.payment_status = new_payment_status
        order.save(update_fields=["payment_status"])

    _log_event(
        order, "operator_action", actor=user, source="operator",
        meta={
            "kind": "dispute_resolved",
            "resolution": resolution,
            "refund_amount": float(refund_amount),
            "reason": reason,
            "by": user.username,
            "money_moved": money_moved,
        },
    )

    # Уведомим обе стороны
    if order.buyer:
        _notify(
            order.buyer, kind="payment",
            title=f"Спор по заказу #{order.id} закрыт",
            body=f"Решение: {resolution}.{money_moved} {reason[:120]}",
            url=f"/chat/?order={order.id}",
        )

    return ActionResult(
        text=(
            f"✓ Спор по #{order.id} закрыт · решение «{resolution}»"
            + (f" · возврат ${refund_amount:,.0f}" if refund_amount else "")
            + "."
        ),
        contextual_actions=[
            {"action": "op_order_detail", "label": "Открыть заказ", "params": {"order_id": order.id}},
            {"action": "op_dashboard", "label": "← Сводка"},
        ],
    )


# ══════════════════════════════════════════════════════════
# 8. Customs / Compliance flow
# ══════════════════════════════════════════════════════════

def _customs_meta(order) -> dict:
    """Read customs section from logistics_meta JSON; default empty."""
    meta = order.logistics_meta or {}
    return dict(meta.get("customs") or {})


def _save_customs_meta(order, customs: dict):
    """Persist customs dict back into logistics_meta."""
    meta = dict(order.logistics_meta or {})
    meta["customs"] = customs
    order.logistics_meta = meta
    order.save(update_fields=["logistics_meta"])


@register("op_hs_lookup")
def op_hs_lookup(params, user, role):
    """Поиск ТН ВЭД по описанию или артикулу."""
    err = _ensure_operator(role)
    if err:
        return err
    from .customs_data import find_hs_codes

    query = (params.get("query") or "").strip()
    if not query:
        return ActionResult(
            text="Введите описание детали или артикул для поиска ТН ВЭД.",
            cards=[{
                "type": "form",
                "data": {
                    "title": "🔎 Поиск ТН ВЭД",
                    "submit_action": "op_hs_lookup",
                    "fields": [{"name": "query", "label": "Описание / артикул", "required": True}],
                },
            }],
        )
    hits = find_hs_codes(query, limit=5)
    if not hits:
        return ActionResult(
            text=f"Не нашёл ТН ВЭД для «{query}». Попробуйте другие ключевые слова.",
            suggestions=["насос", "фильтр", "подшипник", "гидроцилиндр", "шестерня"],
        )
    return ActionResult(
        text=f"Найдено {len(hits)} ТН ВЭД-кодов по запросу «{query}».",
        cards=[{
            "type": "list",
            "data": {
                "title": "🔎 Кандидаты ТН ВЭД",
                "items": [
                    {"title": f"{h['code']} · {h['title']}",
                     "subtitle": "Ключевые слова: " + ", ".join(h["keywords"][:4])}
                    for h in hits
                ],
            },
        }],
        suggestions=[f"присвой {hits[0]['code']} заказу" if hits else "уточни запрос"],
    )


@register("op_hs_assign")
def op_hs_assign(params, user, role):
    """Присвоить HS-код заказу. Two-step DraftCard."""
    err = _ensure_operator(role)
    if err:
        return err
    from marketplace.models import Order
    try:
        order = Order.objects.get(id=int(params.get("order_id") or 0))
    except (Order.DoesNotExist, ValueError, TypeError):
        return ActionResult(text="Заказ не найден.")

    hs_code = (params.get("hs_code") or "").strip()
    country = (params.get("country") or "RU").strip().upper()
    confirmed = bool(params.get("confirmed"))

    if not confirmed or not hs_code:
        cur = _customs_meta(order)
        return ActionResult(
            text=f"Присвоить ТН ВЭД заказу #{order.id}",
            cards=[{
                "type": "form",
                "data": {
                    "title": f"📋 ТН ВЭД · #{order.id}",
                    "submit_action": "op_hs_assign",
                    "fields": [
                        {"name": "hs_code", "label": "Код ТН ВЭД (например 8413.50)",
                         "required": True, "value": cur.get("hs_code", "")},
                        {"name": "country", "label": "Страна импорта (ISO-2, RU/BY/KZ/AM/KG)",
                         "value": cur.get("country", "RU")},
                    ],
                    "fixed_params": {"order_id": order.id, "confirmed": True},
                },
            }],
        )

    customs = _customs_meta(order)
    customs.update({"hs_code": hs_code, "country": country})
    _save_customs_meta(order, customs)
    _log_event(
        order, "operator_action", actor=user, source="operator",
        meta={"kind": "customs_hs_assigned", "hs_code": hs_code, "country": country},
    )
    return ActionResult(
        text=f"✓ Заказу #{order.id} присвоен ТН ВЭД {hs_code} (страна {country}).",
        contextual_actions=[
            {"action": "op_calc_duty", "label": "💰 Рассчитать пошлину", "params": {"order_id": order.id}},
            {"action": "op_certs_check", "label": "📑 Проверить сертификаты", "params": {"order_id": order.id}},
        ],
    )


@register("op_calc_duty")
def op_calc_duty(params, user, role):
    """Расчёт таможенной пошлины + НДС + сборов по заказу."""
    err = _ensure_operator(role)
    if err:
        return err
    from marketplace.models import Order
    from .customs_data import duty_rate_for, vat_rate_for, fees_for

    try:
        order = Order.objects.get(id=int(params.get("order_id") or 0))
    except (Order.DoesNotExist, ValueError, TypeError):
        return ActionResult(text="Заказ не найден.")

    customs = _customs_meta(order)
    hs_code = (params.get("hs_code") or customs.get("hs_code") or "").strip()
    country = (params.get("country") or customs.get("country") or "RU").upper()
    if not hs_code:
        return ActionResult(
            text=f"Сначала присвойте ТН ВЭД заказу #{order.id}.",
            contextual_actions=[
                {"action": "op_hs_assign", "label": "📋 Присвоить ТН ВЭД", "params": {"order_id": order.id}},
            ],
        )

    base = Decimal(str(order.total_amount or 0))
    duty_pct = duty_rate_for(hs_code)
    vat_pct = vat_rate_for(country)
    fees = fees_for(country)

    duty = (base * duty_pct / Decimal("100")).quantize(Decimal("0.01"))
    vat_base = base + duty
    vat = (vat_base * vat_pct / Decimal("100")).quantize(Decimal("0.01"))
    broker = fees["broker"]
    terminal = fees["terminal"]
    total = (duty + vat + broker + terminal).quantize(Decimal("0.01"))

    # Сохраним расчёт
    customs.update({
        "hs_code": hs_code, "country": country,
        "duty_pct": float(duty_pct), "duty": float(duty),
        "vat_pct": float(vat_pct), "vat": float(vat),
        "broker": float(broker), "terminal": float(terminal),
        "duty_total": float(total),
    })
    _save_customs_meta(order, customs)
    _log_event(order, "operator_action", actor=user, source="operator",
               meta={"kind": "customs_duty_calculated", "duty_total": float(total)})

    return ActionResult(
        text=(
            f"Пошлина по заказу #{order.id} ({hs_code} → {country})\n"
            f"База: ${base:,.2f} · пошлина {duty_pct}% = ${duty:,.2f} · "
            f"НДС {vat_pct}% = ${vat:,.2f}\n"
            f"Брокер ${broker} · терминал ${terminal} · ИТОГО ${total:,.2f}"
        ),
        cards=[{
            "type": "kpi_grid",
            "data": {
                "title": f"💰 Расчёт пошлины · #{order.id}",
                "items": [
                    {"label": "База", "value": f"${base:,.2f}"},
                    {"label": f"Пошлина {duty_pct}%", "value": f"${duty:,.2f}"},
                    {"label": f"НДС {vat_pct}%", "value": f"${vat:,.2f}"},
                    {"label": "Брокер", "value": f"${broker}"},
                    {"label": "Терминал", "value": f"${terminal}"},
                    {"label": "ИТОГО", "value": f"${total:,.2f}", "tone": "info"},
                ],
            },
        }],
        contextual_actions=[
            {"action": "op_certs_check", "label": "📑 Сертификаты", "params": {"order_id": order.id}},
            {"action": "op_customs_release", "label": "🚚 Выпустить с таможни", "params": {"order_id": order.id}},
        ],
    )


@register("op_certs_check")
def op_certs_check(params, user, role):
    """Проверка наличия сертификатов для заказа."""
    err = _ensure_operator(role)
    if err:
        return err
    from marketplace.models import Order
    from .customs_data import required_certs_for

    try:
        order = Order.objects.get(id=int(params.get("order_id") or 0))
    except (Order.DoesNotExist, ValueError, TypeError):
        return ActionResult(text="Заказ не найден.")

    customs = _customs_meta(order)
    hs_code = customs.get("hs_code", "")
    required = required_certs_for(hs_code)
    have = list(customs.get("certs") or [])
    missing = [c for c in required if c not in have]

    text = (
        f"Сертификаты по заказу #{order.id} (ТН ВЭД {hs_code or '—'})\n"
        f"Требуется: {', '.join(required) or '—'}\n"
        f"В наличии: {', '.join(have) or 'нет'}\n"
        + (f"❗ Не хватает: {', '.join(missing)}" if missing else "✓ Все сертификаты на месте")
    )
    items = [{"label": c, "value": "✓" if c in have else "✗",
              "tone": "ok" if c in have else "warn"} for c in required]

    return ActionResult(
        text=text,
        cards=[{"type": "kpi_grid", "data": {"title": f"📑 Сертификаты #{order.id}", "items": items}}],
        contextual_actions=(
            [{"action": "op_cert_upload", "label": f"⬆ Загрузить {missing[0]}",
              "params": {"order_id": order.id, "cert": missing[0]}}] if missing else
            [{"action": "op_customs_release", "label": "🚚 Выпустить с таможни",
              "params": {"order_id": order.id}}]
        ),
    )


@register("op_cert_upload")
def op_cert_upload(params, user, role):
    """Зафиксировать загрузку сертификата (writing → DraftCard)."""
    err = _ensure_operator(role)
    if err:
        return err
    from marketplace.models import Order

    try:
        order = Order.objects.get(id=int(params.get("order_id") or 0))
    except (Order.DoesNotExist, ValueError, TypeError):
        return ActionResult(text="Заказ не найден.")

    cert = (params.get("cert") or "").strip()
    number = (params.get("number") or "").strip()
    confirmed = bool(params.get("confirmed"))

    if not confirmed or not cert:
        return ActionResult(
            text=f"Загрузка сертификата · #{order.id}",
            cards=[{
                "type": "form",
                "data": {
                    "title": f"⬆ Сертификат · #{order.id}",
                    "submit_action": "op_cert_upload",
                    "fields": [
                        {"name": "cert", "label": "Тип (EAC, ТР ТС 010/2011, ...)", "required": True,
                         "value": cert},
                        {"name": "number", "label": "Номер документа"},
                    ],
                    "fixed_params": {"order_id": order.id, "confirmed": True},
                },
            }],
        )

    customs = _customs_meta(order)
    have = list(customs.get("certs") or [])
    if cert not in have:
        have.append(cert)
    customs["certs"] = have
    customs.setdefault("cert_numbers", {})[cert] = number
    _save_customs_meta(order, customs)
    _log_event(order, "operator_action", actor=user, source="operator",
               meta={"kind": "customs_cert_uploaded", "cert": cert, "number": number})

    return ActionResult(
        text=f"✓ Сертификат «{cert}» добавлен к заказу #{order.id}.",
        contextual_actions=[
            {"action": "op_certs_check", "label": "📑 Проверить", "params": {"order_id": order.id}},
        ],
    )


@register("op_sanctions_check")
def op_sanctions_check(params, user, role):
    """Проверить контрагента/страну/категорию на санкции."""
    err = _ensure_operator(role)
    if err:
        return err
    from .customs_data import sanctions_check

    country = (params.get("country") or "").strip()
    entity = (params.get("entity") or "").strip()
    category = (params.get("category") or "").strip()
    if not (country or entity or category):
        return ActionResult(
            text="Что проверить на санкции?",
            cards=[{
                "type": "form",
                "data": {
                    "title": "🚫 Санкционная проверка",
                    "submit_action": "op_sanctions_check",
                    "fields": [
                        {"name": "country", "label": "Страна (ISO-2, например IR)"},
                        {"name": "entity", "label": "Контрагент / организация"},
                        {"name": "category", "label": "Категория (dual_use_chip)"},
                    ],
                },
            }],
        )
    res = sanctions_check(country=country, entity=entity, category=category)
    level = res["level"]
    icon = {"high": "🚫", "medium": "⚠️", "low": "ℹ️", "none": "✓"}[level]
    label = {"high": "Запрещено", "medium": "Под контролем", "low": "Серая зона", "none": "Чисто"}[level]
    items = [
        {"label": "Уровень", "value": label,
         "tone": {"high": "bad", "medium": "warn", "low": "warn", "none": "ok"}[level]},
    ]
    if country:  items.append({"label": "Страна", "value": country.upper()})
    if entity:   items.append({"label": "Контрагент", "value": entity})
    if category: items.append({"label": "Категория", "value": category})

    text_lines = [f"{icon} Санкционная проверка: {label}."]
    text_lines.extend("• " + r for r in (res["reasons"] or ["Нет совпадений в списках."]))

    return ActionResult(
        text="\n".join(text_lines),
        cards=[{"type": "kpi_grid", "data": {"title": "🚫 Санкции", "items": items}}],
    )


@register("op_customs_dashboard")
def op_customs_dashboard(params, user, role):
    """Сводка по таможне: на оформлении, документы готовы, недостающие, средний срок."""
    err = _ensure_operator(role)
    if err:
        return err
    from marketplace.models import Order

    on_customs = Order.objects.filter(status="customs")
    awaiting_docs = 0
    ready_to_release = 0
    total = 0
    for o in on_customs:
        total += 1
        cm = _customs_meta(o)
        if not cm.get("hs_code") or not cm.get("certs"):
            awaiting_docs += 1
        else:
            ready_to_release += 1

    in_transit = Order.objects.filter(status__in=("transit_abroad", "transit_rf")).count()
    rows = [
        {"title": f"#{o.id} · {o.customer_name}",
         "subtitle": f"{o.get_status_display()} · ТН ВЭД {(_customs_meta(o).get('hs_code') or '—')}",
         "url": f"/chat/?order={o.id}"}
        for o in on_customs[:10]
    ]
    return ActionResult(
        text=(
            f"Таможня · {total} грузов на оформлении · готовы к выпуску: {ready_to_release} · "
            f"ждут документы: {awaiting_docs} · в транзите: {in_transit}."
        ),
        cards=[
            {"type": "kpi_grid", "data": {"title": "🛂 Таможня", "items": [
                {"label": "На оформлении", "value": str(total), "tone": "info"},
                {"label": "К выпуску", "value": str(ready_to_release), "tone": "ok" if ready_to_release else "warn"},
                {"label": "Не хватает доков", "value": str(awaiting_docs),
                 "tone": "warn" if awaiting_docs else "ok"},
                {"label": "В транзите", "value": str(in_transit)},
            ]}},
            {"type": "list", "data": {"title": "На таможне сейчас",
                "items": rows or [{"title": "Нет грузов на таможне"}]}},
        ],
        contextual_actions=[
            {"action": "op_queue", "label": "📋 Очередь оператора", "params": {"filter": "open"}},
        ],
    )


@register("op_customs_release")
def op_customs_release(params, user, role):
    """Выпустить груз с таможни — переводит status `customs` → `transit_rf`."""
    err = _ensure_operator(role)
    if err:
        return err
    from marketplace.models import Order
    from .customs_data import required_certs_for

    try:
        order = Order.objects.get(id=int(params.get("order_id") or 0))
    except (Order.DoesNotExist, ValueError, TypeError):
        return ActionResult(text="Заказ не найден.")

    confirmed = bool(params.get("confirmed"))
    customs = _customs_meta(order)
    hs_code = customs.get("hs_code", "")
    have = list(customs.get("certs") or [])
    required = required_certs_for(hs_code)
    missing = [c for c in required if c not in have]

    if not confirmed:
        warn = ""
        if not hs_code:
            warn = "❗ ТН ВЭД не присвоен."
        elif missing:
            warn = f"❗ Нет сертификатов: {', '.join(missing)}"
        elif not customs.get("duty_total"):
            warn = "ℹ️ Пошлина не рассчитана."
        return ActionResult(
            text=f"Выпустить груз #{order.id} с таможни?\n{warn}",
            cards=[{
                "type": "form",
                "data": {
                    "title": f"🚚 Выпуск с таможни · #{order.id}",
                    "submit_action": "op_customs_release",
                    "fields": [
                        {"name": "comment", "label": "Комментарий (необязательно)"},
                    ],
                    "fixed_params": {"order_id": order.id, "confirmed": True},
                },
            }],
            contextual_actions=(
                [{"action": "op_hs_assign", "label": "📋 Присвоить ТН ВЭД",
                  "params": {"order_id": order.id}}] if not hs_code else
                [{"action": "op_certs_check", "label": "📑 Проверить сертификаты",
                  "params": {"order_id": order.id}}] if missing else
                [{"action": "op_calc_duty", "label": "💰 Рассчитать пошлину",
                  "params": {"order_id": order.id}}] if not customs.get("duty_total") else []
            ),
        )

    # Жёсткая проверка перед записью
    blockers = []
    if not hs_code:
        blockers.append("нет ТН ВЭД")
    if missing:
        blockers.append("не хватает сертификатов: " + ", ".join(missing))
    if blockers:
        return ActionResult(
            text="⚠️ Нельзя выпустить: " + "; ".join(blockers) + ".",
            contextual_actions=[
                {"action": "op_customs_release", "label": "Открыть форму выпуска",
                 "params": {"order_id": order.id}},
            ],
        )

    if order.status == "customs":
        order.status = "transit_rf"
        order.save(update_fields=["status"])
        _log_event(order, "status_changed", actor=user, source="operator",
                   meta={"from": "customs", "to": "transit_rf", "by": "op_customs_release"})

    comment = (params.get("comment") or "").strip()
    _log_event(order, "operator_action", actor=user, source="operator",
               meta={"kind": "customs_released", "comment": comment})

    if order.buyer:
        _notify(order.buyer, kind="order",
                title=f"Груз #{order.id} выпущен с таможни",
                body="Едет к вам · следите за трекингом.",
                url=f"/chat/?order={order.id}")

    return ActionResult(
        text=f"✓ Заказ #{order.id} выпущен с таможни → транзит РФ.",
        contextual_actions=[
            {"action": "op_customs_dashboard", "label": "← Сводка таможни"},
            {"action": "op_order_detail", "label": "Открыть заказ", "params": {"order_id": order.id}},
        ],
    )


# ══════════════════════════════════════════════════════════
# 9. Payments dashboard — эскроу + outstanding holds
# ══════════════════════════════════════════════════════════

@register("op_payments_dashboard")
def op_payments_dashboard(params, user, role):
    err = _ensure_operator(role)
    if err:
        return err
    from . import payments as _pay
    from marketplace.models import Order

    s = _pay.escrow_summary()
    holds = s.get("open_holds", {})
    rows = []
    for oid, amt in sorted(holds.items(), key=lambda x: -x[1])[:10]:
        try:
            o = Order.objects.get(id=oid)
            rows.append({
                "title": f"#{oid} · {o.customer_name}",
                "subtitle": f"${amt:,.2f} · {o.get_status_display()} · {o.get_payment_status_display()}",
                "url": f"/chat/?order={oid}",
            })
        except Order.DoesNotExist:
            rows.append({"title": f"#{oid}", "subtitle": f"${amt:,.2f}"})

    return ActionResult(
        text=(
            f"Эскроу платформы · удерживается ${s['outstanding_balance']:,.2f} "
            f"по {len(holds)} заказам.\n"
            f"За всё время · принято ${s['total_held_ever']:,.0f} · "
            f"выплачено продавцам ${s['total_released_ever']:,.0f} · "
            f"возвращено покупателям ${s['total_refunded_ever']:,.0f}."
        ),
        cards=[
            {"type": "kpi_grid", "data": {"title": "💰 Эскроу платформы", "items": [
                {"label": "Сейчас удерживается", "value": f"${s['outstanding_balance']:,.0f}", "tone": "info"},
                {"label": "Открытых холдов", "value": str(len(holds))},
                {"label": "Принято за всё время", "value": f"${s['total_held_ever']:,.0f}"},
                {"label": "Выплачено продавцам", "value": f"${s['total_released_ever']:,.0f}", "tone": "ok"},
                {"label": "Возвращено покупателям", "value": f"${s['total_refunded_ever']:,.0f}",
                 "tone": "warn" if s['total_refunded_ever'] else "ok"},
                {"label": "Баланс sentinel-кошелька", "value": f"${s['platform_balance']:,.2f}"},
            ]}},
            {"type": "list", "data": {"title": "Топ открытых холдов",
                "items": rows or [{"title": "Эскроу пуст"}]}},
        ],
        contextual_actions=[
            {"action": "op_queue", "label": "📋 Очередь", "params": {"filter": "refund"}},
        ],
    )

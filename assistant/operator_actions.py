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

    # Side-effects: если refund — переводим payment_status; если release — paid
    new_payment_status = order.payment_status
    if resolution == "refund":
        new_payment_status = "refunded"
    elif resolution == "partial_refund":
        new_payment_status = "refund_pending"
    elif resolution == "release":
        new_payment_status = "paid"

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
        },
    )

    # Уведомим обе стороны
    if order.buyer:
        _notify(
            order.buyer, kind="payment",
            title=f"Спор по заказу #{order.id} закрыт",
            body=f"Решение: {resolution}. {reason[:120]}",
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

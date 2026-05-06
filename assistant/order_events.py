"""Broadcast события заказа в shipment-чат buyer'а.

Все 4 действия по продвижению заказа (advance_order / ship_order /
confirm_delivery / pay_final / pay_reserve) дёргают `notify_order_event()`
который пишет в `Conversation(category='shipment', title=Сделка ORD-N)`
buyer'а системное сообщение + карточку «order_timeline» с
прогрессом этапов и контекстной кнопкой «следующее действие».

Схема событий:
  reserve_paid    — после confirm_kp_and_reserve / pay_reserve
  confirmed       — seller подтвердил
  in_production   — seller запустил производство
  ready_to_ship   — seller отметил готовность
  pay_final       — buyer оплатил 90%
  shipped         — seller отгрузил с tracking
  customs         — товар на таможне
  transit_rf      — едет по РФ
  issuing         — на выдаче
  delivered       — доставлен (buyer должен подтвердить)
  completed       — buyer подтвердил приёмку, эскроу освобождён
"""
from __future__ import annotations

import logging
from decimal import Decimal

from django.utils import timezone

logger = logging.getLogger(__name__)


# Pipeline этапы для timeline-карточки. (status, label, who_acts_next)
PIPELINE_STAGES = [
    ("reserve_paid",   "Резерв оплачен",          "seller"),
    ("confirmed",      "Подтверждён продавцом",   "seller"),
    ("in_production",  "В производстве",          "seller"),
    ("ready_to_ship",  "Готов к отгрузке",        "buyer"),  # buyer платит 90%
    ("transit_abroad", "Транзит за рубеж",        "operator"),
    ("customs",        "На таможне",              "operator"),
    ("transit_rf",     "Транзит по РФ",           "operator"),
    ("issuing",        "На выдаче",               "operator"),
    ("delivered",      "Доставлен",               "buyer"),  # buyer подтверждает
    ("completed",      "Завершён",                None),
]

STAGE_INDEX = {s: i for i, (s, _, _) in enumerate(PIPELINE_STAGES)}


def _build_timeline_card(order) -> dict:
    """Карточка type=order_timeline для frontend-renderer'а."""
    cur_idx = STAGE_INDEX.get(order.status, -1)
    stages = []
    for i, (code, label, _who) in enumerate(PIPELINE_STAGES):
        state = ("done" if i < cur_idx
                 else "current" if i == cur_idx
                 else "pending")
        stages.append({"code": code, "label": label, "state": state})

    # Next-step CTA по текущему статусу + payment_status
    next_action = None
    cur = order.status
    pay = order.payment_status
    if cur == "ready_to_ship" and pay != "paid":
        rem = (Decimal(str(order.total_amount or 0))
                - Decimal(str(order.reserve_amount or 0))).quantize(Decimal("0.01"))
        next_action = {
            "label": f"💳 Оплатить остаток ${rem:,.0f} (90%)",
            "action": "pay_final",
            "params": {"order_id": order.id},
            "actor": "buyer",
        }
    elif cur == "delivered":
        next_action = {
            "label": "✓ Подтвердить приёмку",
            "action": "confirm_delivery",
            "params": {"order_id": order.id},
            "actor": "buyer",
        }
    elif cur in ("transit_abroad", "customs", "transit_rf", "issuing"):
        next_action = {
            "label": "📦 Трекинг",
            "action": "track_shipment",
            "params": {"order_id": order.id},
            "actor": "any",
        }

    progress_pct = (
        int((cur_idx + 1) / len(PIPELINE_STAGES) * 100)
        if cur_idx >= 0 else 0
    )
    return {
        "type": "order_timeline",
        "data": {
            "order_id": order.id,
            "title": f"Сделка ORD-{order.id}",
            "status_label": order.get_status_display() if hasattr(order, "get_status_display") else str(order.status),
            "total": float(order.total_amount or 0),
            "reserve_amount": float(order.reserve_amount or 0),
            "logistics_cost": float(getattr(order, "logistics_cost", 0) or 0),
            "currency": "USD",
            "stages": stages,
            "progress_pct": progress_pct,
            "current_index": cur_idx,
            "next_action": next_action,
        },
    }


def _shipment_conv(buyer, order):
    """Найти/создать conversation(category='shipment') для buyer×order."""
    from .models import Conversation
    title_prefix = f"Сделка ORD-{order.id}"
    conv = (Conversation.objects.filter(
        user=buyer, category="shipment",
        title__startswith=title_prefix, is_active=True,
    ).order_by("-updated_at").first())
    if conv:
        return conv
    return Conversation.objects.create(
        user=buyer, role="buyer", category="shipment",
        title=title_prefix[:200],
    )


def notify_order_event(order, event: str, *, actor=None,
                        text: str | None = None,
                        extra_actions: list | None = None) -> None:
    """Главный broadcaster. Пишет системное сообщение в shipment-чат
    buyer'а с обновлённым order_timeline + контекстной кнопкой.

    event: «confirmed» / «in_production» / «ready_to_ship» / «shipped» /
            «customs» / «transit_rf» / «issuing» / «delivered» /
            «pay_final» / «completed»
    actor: кто инициировал (для аудита)
    text: сообщение в чат buyer'а. Если None — сгенерируется из event.
    extra_actions: доп. action-кнопки сверх timeline.next_action
    """
    from .models import Message

    if not order or not order.buyer:
        return
    conv = _shipment_conv(order.buyer, order)
    if not conv:
        return

    # Подбор текста по событию
    EVENT_TEXTS = {
        "confirmed":      f"✅ Поставщик подтвердил заказ ORD-{order.id} — запускают производство.",
        "in_production":  f"🏭 ORD-{order.id} в производстве. Сообщим когда готов к отгрузке.",
        "ready_to_ship":  f"📦 ORD-{order.id} готов к отгрузке. Оплатите остаток 90% — поедет.",
        "pay_final":      f"💳 Остаток 90% оплачен по ORD-{order.id} — заказ отгружают.",
        "shipped":        f"🚚 ORD-{order.id} отгружен и в пути.",
        "transit_abroad": f"🛫 ORD-{order.id} в транзите за рубеж.",
        "customs":        f"🛃 ORD-{order.id} проходит таможню.",
        "transit_rf":     f"🚛 ORD-{order.id} в транзите по РФ.",
        "issuing":        f"📬 ORD-{order.id} на выдаче — забирайте.",
        "delivered":      f"🏁 ORD-{order.id} доставлен. Подтвердите приёмку — деньги уйдут продавцу.",
        "completed":      f"🎉 ORD-{order.id} завершён. Эскроу освобождён продавцу.",
    }
    msg_text = text or EVENT_TEXTS.get(event,
        f"Обновление по заказу ORD-{order.id}: {event}")

    cards = [_build_timeline_card(order)]
    actions = list(extra_actions or [])
    Message.objects.create(
        conversation=conv,
        role=Message.Role.SYSTEM,
        content=msg_text,
        cards=cards,
        actions=actions,
    )
    logger.info(
        f"order_event: ORD-{order.id} {event} → buyer {order.buyer_id} "
        f"conv {conv.id}"
    )

    # WebSocket push для live-обновления (если канал активен)
    try:
        from channels.layers import get_channel_layer
        from asgiref.sync import async_to_sync
        layer = get_channel_layer()
        if layer:
            async_to_sync(layer.group_send)(
                f"user_{order.buyer_id}",
                {"type": "order.update", "order_id": order.id, "event": event},
            )
    except Exception:
        # Channels не критичен — сообщение уже в БД
        pass

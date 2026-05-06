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


def _shipment_conv(user, order, role="buyer"):
    """Найти/создать conversation(category='shipment') для user×order.

    Один conv на (user, ORD-N). Используется и для buyer'а, и для seller'а,
    и для operator'а — все видят одну и ту же сделку в собственных конвах.
    """
    from .models import Conversation
    title_prefix = f"Сделка ORD-{order.id}"
    conv = (Conversation.objects.filter(
        user=user, category="shipment",
        title__startswith=title_prefix, is_active=True,
    ).order_by("-updated_at").first())
    if conv:
        return conv
    return Conversation.objects.create(
        user=user, role=role, category="shipment",
        title=title_prefix[:200],
    )


def _order_sellers(order):
    """Возвращает уникальных seller'ов по позициям заказа."""
    from django.contrib.auth import get_user_model
    User = get_user_model()
    seller_ids = set()
    for it in order.items.select_related("part").all():
        if it.part and it.part.seller_id:
            seller_ids.add(it.part.seller_id)
    return list(User.objects.filter(id__in=seller_ids))


def _operator_users():
    """Возвращает операторов которым пушим SLA-эскалации.

    Сейчас — все is_staff пользователи (≤5 для не перегружать).
    В проде — отдельная группа Operator + on-call rotation.
    """
    from django.contrib.auth import get_user_model
    User = get_user_model()
    return list(User.objects.filter(is_staff=True, is_active=True)[:5])


# Тексты сообщений для разных аудиторий
_EVENT_TEXTS_BUYER = {
    "confirmed":      "✅ Поставщик подтвердил заказ ORD-{id} — запускают производство.",
    "in_production":  "🏭 ORD-{id} в производстве. Сообщим когда готов к отгрузке.",
    "ready_to_ship":  "📦 ORD-{id} готов к отгрузке. Оплатите остаток 90% — поедет.",
    "pay_final":      "💳 Остаток 90% оплачен по ORD-{id} — заказ отгружают.",
    "shipped":        "🚚 ORD-{id} отгружен и в пути.",
    "transit_abroad": "🛫 ORD-{id} в транзите за рубеж.",
    "customs":        "🛃 ORD-{id} проходит таможню.",
    "transit_rf":     "🚛 ORD-{id} в транзите по РФ.",
    "issuing":        "📬 ORD-{id} на выдаче — забирайте.",
    "delivered":      "🏁 ORD-{id} доставлен. Подтвердите приёмку — деньги уйдут продавцу.",
    "completed":      "🎉 ORD-{id} завершён. Эскроу освобождён продавцу.",
}
_EVENT_TEXTS_SELLER = {
    "reserve_paid":   "💰 ORD-{id}: резерв 10% оплачен покупателем — можно подтверждать заказ.",
    "confirmed":      "✅ ORD-{id} подтверждён — запустите производство.",
    "in_production":  "🏭 ORD-{id} в производстве (статус обновлён).",
    "ready_to_ship":  "📦 ORD-{id} помечен «готов к отгрузке». Ждём оплаты 90% от покупателя.",
    "pay_final":      "💳 Покупатель оплатил остаток 90% по ORD-{id}. Можно отгружать.",
    "shipped":        "🚚 ORD-{id}: вы отгрузили. Покупатель уведомлён.",
    "transit_abroad": "🛫 ORD-{id}: транзит за рубеж — следите за трекингом.",
    "customs":        "🛃 ORD-{id}: на таможне (оператор оформляет).",
    "transit_rf":     "🚛 ORD-{id}: транзит по РФ.",
    "issuing":        "📬 ORD-{id}: передан на выдачу — покупатель заберёт.",
    "delivered":      "🏁 ORD-{id} доставлен. Покупатель должен подтвердить приёмку.",
    "completed":      "🎉 ORD-{id}: покупатель подтвердил приёмку — деньги переведены вам из эскроу.",
}
_EVENT_TEXTS_OPERATOR = {
    "sla_semi_overdue":   "⚠️ SEMI RFQ #{rfq_id} — просрочен 15-минутный SLA. Approve или эскалация.",
    "sla_manual_overdue": "⚠️ MANUAL RFQ #{rfq_id} — собирается КП >48ч. Эскалация.",
    "sla_breach":         "⚠️ ORD-{id}: SLA breach (статус {status}). Нужна реакция.",
    "claim_opened":       "🛡 ORD-{id}: открыта рекламация. Требуется review.",
}


def notify_order_event(order, event: str, *, actor=None,
                        text: str | None = None,
                        extra_actions: list | None = None,
                        targets: tuple = ("buyer", "seller")) -> None:
    """Главный broadcaster. Пишет системное сообщение в shipment-чат
    каждой из targets (buyer / seller / operator) с обновлённым
    order_timeline.

    targets: какие роли уведомляем (по умолчанию buyer + seller).
             Operator уведомляется отдельно через notify_operator_alert().
    """
    from .models import Message

    if not order:
        return

    cards = [_build_timeline_card(order)]
    extra = list(extra_actions or [])

    def _post(user, role_label, text_template_dict):
        if not user:
            return
        conv = _shipment_conv(user, order, role=role_label)
        if not conv:
            return
        body = (text or text_template_dict.get(event, "")).format(id=order.id) \
               or f"Обновление по заказу ORD-{order.id}: {event}"
        Message.objects.create(
            conversation=conv,
            role=Message.Role.SYSTEM,
            content=body,
            cards=cards,
            actions=extra,
        )
        # WebSocket для live-update
        try:
            from channels.layers import get_channel_layer
            from asgiref.sync import async_to_sync
            layer = get_channel_layer()
            if layer:
                async_to_sync(layer.group_send)(
                    f"notif_user_{user.id}",
                    {"type": "order_update", "order_id": order.id,
                     "event": event, "conversation_id": str(conv.id)},
                )
        except Exception:
            pass
        logger.info(
            f"order_event: ORD-{order.id} {event} → {role_label}"
            f" {user.id} conv {conv.id}"
        )

    if "buyer" in targets and order.buyer:
        _post(order.buyer, "buyer", _EVENT_TEXTS_BUYER)
    if "seller" in targets:
        for s in _order_sellers(order):
            _post(s, "seller", _EVENT_TEXTS_SELLER)
    if "operator" in targets:
        for op in _operator_users():
            _post(op, "operator", _EVENT_TEXTS_OPERATOR)


def notify_operator_alert(*, rfq=None, order=None, claim=None,
                            event: str, text: str | None = None) -> None:
    """Эскалация в операторские shipment-чаты — SLA breach, SEMI overdue,
    MANUAL >48ч, claim opened.
    """
    from .models import Conversation, Message

    body = text or _EVENT_TEXTS_OPERATOR.get(event, f"Alert: {event}")
    body = body.format(
        id=(order.id if order else "—"),
        rfq_id=(rfq.id if rfq else "—"),
        status=(order.status if order else "—"),
    )
    cards = []
    actions = []
    title_prefix = ""
    if order:
        cards.append(_build_timeline_card(order))
        title_prefix = f"Сделка ORD-{order.id}"
        actions.append({"action": "get_order_detail", "label": "📋 Открыть заказ",
                          "params": {"order_id": order.id}})
    elif rfq:
        title_prefix = f"RFQ #{rfq.id}"
        if event == "sla_semi_overdue":
            actions.append({"action": "op_approve_kp", "label": "▶️ Approve КП",
                              "params": {"rfq_id": rfq.id}})
        if event == "sla_manual_overdue":
            actions.append({"action": "op_dispatch_manual_rfq",
                              "label": "▶️ Сформировать КП",
                              "params": {"rfq_id": rfq.id}})
    elif claim:
        title_prefix = f"Claim #{claim.id}"
        actions.append({"action": "claim_detail", "label": "📋 Открыть",
                          "params": {"claim_id": claim.id}})

    for op in _operator_users():
        # Для оператора — отдельный support-conv, чтобы не плодить
        # бесконечно shipment-чаты (только при наличии order у нас уже
        # есть один). Группируем все алерты в category='support'.
        conv = Conversation.objects.filter(
            user=op, category="support", title__startswith="Алерты",
            is_active=True,
        ).order_by("-updated_at").first()
        if not conv:
            conv = Conversation.objects.create(
                user=op, role="operator", category="support",
                title="Алерты оператора",
            )
        Message.objects.create(
            conversation=conv,
            role=Message.Role.SYSTEM,
            content=f"{title_prefix} · {body}" if title_prefix else body,
            cards=cards,
            actions=actions,
        )
        try:
            from channels.layers import get_channel_layer
            from asgiref.sync import async_to_sync
            layer = get_channel_layer()
            if layer:
                async_to_sync(layer.group_send)(
                    f"notif_user_{op.id}",
                    {"type": "operator_alert", "event": event,
                     "rfq_id": rfq.id if rfq else None,
                     "order_id": order.id if order else None,
                     "claim_id": claim.id if claim else None},
                )
        except Exception:
            pass
    logger.info(f"operator_alert: {event} → {len(_operator_users())} ops")

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
from django.utils.translation import gettext as _
from decimal import Decimal

logger = logging.getLogger(__name__)


from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _lazy

# Pipeline этапы для timeline-карточки. (status, label, who_acts_next)
# Лейблы — функции (lambda), чтобы вычислялись на каждом рендере под текущим
# активным языком (translation.activate() сделан в middleware).
PIPELINE_STAGES = [
    ("reserve_paid",   lambda: gettext("Резерв оплачен"),          "seller"),
    ("confirmed",      lambda: gettext("Подтверждён продавцом"),   "seller"),
    ("in_production",  lambda: gettext("В производстве"),          "seller"),
    ("ready_to_ship",  lambda: gettext("Готов к отгрузке"),        "buyer"),
    ("transit_abroad", lambda: gettext("Транзит за рубеж"),        "operator"),
    ("customs",        lambda: gettext("На таможне"),              "operator"),
    ("transit_rf",     lambda: gettext("Транзит по РФ"),           "operator"),
    ("issuing",        lambda: gettext("На выдаче"),               "operator"),
    ("delivered",      lambda: gettext("Доставлен"),               "buyer"),
    ("completed",      lambda: gettext("Завершён"),                None),
]

STAGE_INDEX = {s: i for i, (s, _, _) in enumerate(PIPELINE_STAGES)}


def _build_timeline_card(order) -> dict:
    """Карточка type=order_timeline для frontend-renderer'а."""
    cur_idx = STAGE_INDEX.get(order.status, -1)
    stages = []
    for i, (code, label_fn, _who) in enumerate(PIPELINE_STAGES):
        state = ("done" if i < cur_idx
                 else "current" if i == cur_idx
                 else "pending")
        # label_fn — callable, чтобы перевод применился под текущим языком запроса.
        label = label_fn() if callable(label_fn) else label_fn
        stages.append({"code": code, "label": label, "state": state})

    # Next-step CTA по текущему статусу + payment_status
    next_action = None
    cur = order.status
    pay = order.payment_status
    if cur == "ready_to_ship" and pay != "paid":
        rem = (Decimal(str(order.total_amount or 0))
                - Decimal(str(order.reserve_amount or 0))).quantize(Decimal("0.01"))
        next_action = {
            "label": gettext("💳 Оплатить остаток ${rem} (90%)").format(rem=f"{rem:,.0f}"),
            "action": "pay_final",
            "params": {"order_id": order.id},
            "actor": "buyer",
        }
    elif cur == "delivered":
        next_action = {
            "label": gettext("✓ Подтвердить приёмку"),
            "action": "confirm_delivery",
            "params": {"order_id": order.id},
            "actor": "buyer",
        }
    elif cur in ("transit_abroad", "customs", "transit_rf", "issuing"):
        next_action = {
            "label": gettext("📦 Трекинг"),
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
            "title": gettext("Сделка ORD-{id}").format(id=order.id),
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
        user=user, role=role, category="shipment",
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

    Кандидаты: активные пользователи с явно выданной ролью оператора и
    superuser. Технический флаг доступа к панели управления права не даёт.
    Дедуплицируем по id, ограничиваем 10 чтобы не флудить алертами.
    """
    from django.contrib.auth import get_user_model
    from django.db.models import Q
    from marketplace.models import UserProfile
    User = get_user_model()
    operator_user_ids = set(
        UserProfile.objects.filter(role="operator")
        .values_list("user_id", flat=True)
    )
    qs = User.objects.filter(
        Q(is_superuser=True) | Q(id__in=operator_user_ids),
        is_active=True,
    ).distinct()[:10]
    return list(qs)


# Тексты сообщений для разных аудиторий
# Тексты — функции, чтобы перевод применялся под текущим языком запроса.
def _event_text_buyer(code: str) -> str | None:
    table = {
        "confirmed":      gettext("✅ Поставщик подтвердил заказ ORD-{id} — запускают производство."),
        "in_production":  gettext("🏭 ORD-{id} в производстве. Сообщим когда готов к отгрузке."),
        "ready_to_ship":  gettext("📦 ORD-{id} готов к отгрузке. Оплатите остаток 90% — поедет."),
        "pay_final":      gettext("💳 Остаток 90% оплачен по ORD-{id} — заказ отгружают."),
        "shipped":        gettext("🚚 ORD-{id} отгружен и в пути."),
        "transit_abroad": gettext("🛫 ORD-{id} в транзите за рубеж."),
        "customs":        gettext("🛃 ORD-{id} проходит таможню."),
        "transit_rf":     gettext("🚛 ORD-{id} в транзите по РФ."),
        "issuing":        gettext("📬 ORD-{id} на выдаче — забирайте."),
        "delivered":      gettext("🏁 ORD-{id} доставлен. Подтвердите приёмку — деньги уйдут продавцу."),
        "completed":      gettext("🎉 ORD-{id} завершён. Эскроу освобождён продавцу."),
    }
    return table.get(code)


def _event_text_seller(code: str) -> str | None:
    table = {
        "reserve_paid":   gettext("💰 ORD-{id}: резерв 10% оплачен покупателем — можно подтверждать заказ."),
        "confirmed":      gettext("✅ ORD-{id} подтверждён — запустите производство."),
        "in_production":  gettext("🏭 ORD-{id} в производстве (статус обновлён)."),
        "ready_to_ship":  gettext("📦 ORD-{id} помечен «готов к отгрузке». Ждём оплаты 90% от покупателя."),
        "pay_final":      gettext("💳 Покупатель оплатил остаток 90% по ORD-{id}. Можно отгружать."),
        "shipped":        gettext("🚚 ORD-{id}: вы отгрузили. Покупатель уведомлён."),
        "transit_abroad": gettext("🛫 ORD-{id}: транзит за рубеж — следите за трекингом."),
        "customs":        gettext("🛃 ORD-{id}: на таможне (оператор оформляет)."),
        "transit_rf":     gettext("🚛 ORD-{id}: транзит по РФ."),
        "issuing":        gettext("📬 ORD-{id}: передан на выдачу — покупатель заберёт."),
        "delivered":      gettext("🏁 ORD-{id} доставлен. Покупатель должен подтвердить приёмку."),
        "completed":      gettext("🎉 ORD-{id}: покупатель подтвердил приёмку — деньги переведены вам из эскроу."),
    }
    return table.get(code)


# Обратная совместимость для внешних импортов
_EVENT_TEXTS_BUYER = {}    # deprecated, see _event_text_buyer()
_EVENT_TEXTS_SELLER = {}   # deprecated, see _event_text_seller()
_EVENT_TEXTS_OPERATOR = {
    "sla_semi_overdue":   _lazy("⚠️ Заявка #{rfq_id} ждёт подтверждения больше 15 минут. Нужна проверка или эскалация."),
    "sla_manual_overdue": _lazy("⚠️ Ручной подбор по заявке #{rfq_id} длится больше 48 часов. Нужна эскалация."),
    "sla_breach":         _lazy("⚠️ Заказ {id}: нарушен срок этапа «{status}». Нужна реакция."),
    "claim_opened":       _lazy("🛡 Заказ {id}: открыта рекламация. Требуется проверка."),
    "claim_escalated":    _lazy("🚨 ЭСКАЛАЦИЯ · рекламация открыта >7 дней без решения."),
    "user_registered":    _lazy("👤 Новый пользователь: {username} · {role} · {email}"),
    "kyb_submitted":      _lazy("🛡 {username} отправил KYB на проверку — компания «{legal_name}»."),
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

    def _post(user, role_label, text_resolver):
        if not user:
            return
        conv = _shipment_conv(user, order, role=role_label)
        if not conv:
            return
        # text_resolver — либо dict (старый) либо callable(event)->str|None (новый)
        if callable(text_resolver):
            tpl = text_resolver(event) or ""
        elif isinstance(text_resolver, dict):
            tpl = text_resolver.get(event, "")
        else:
            tpl = ""
        body = (text or tpl).format(id=order.id) \
               or gettext("Обновление по заказу ORD-{id}: {event}").format(id=order.id, event=event)
        Message.objects.create(
            conversation=conv,
            role=Message.Role.SYSTEM,
            content=body,
            cards=cards,
            actions=extra,
        )
        # Сначала сохраняем уведомление, затем посылаем только сигнал о нём.
        try:
            from marketplace.models import Notification
            from .consumers import push_notification_to_user
            notification = Notification.objects.create(
                user=user,
                kind="order",
                title=gettext("Обновление заказа ORD-{id}").format(id=order.id),
                body=body,
                url=f"/chat/?conv={conv.id}",
            )
            push_notification_to_user(user.id, {
                "id": notification.id,
                "kind": "order",
                "title": notification.title,
                "body": notification.body,
                "url": notification.url,
                "order_id": order.id,
                "event": event,
                "conversation_id": str(conv.id),
            })
        except Exception:
            logger.exception("order notification delivery failed")
        logger.info(
            f"order_event: ORD-{order.id} {event} → {role_label}"
            f" {user.id} conv {conv.id}"
        )

    if "buyer" in targets and order.buyer:
        _post(order.buyer, "buyer", _event_text_buyer)
    if "seller" in targets:
        for s in _order_sellers(order):
            _post(s, "seller", _event_text_seller)
    if "operator" in targets:
        for op in _operator_users():
            _post(op, "operator", _EVENT_TEXTS_OPERATOR)


def notify_operator_alert(*, rfq=None, order=None, claim=None, user_obj=None,
                            event: str, text: str | None = None,
                            extra: dict | None = None) -> None:
    """Эскалация в операторские shipment-чаты — SLA breach, SEMI overdue,
    MANUAL >48ч, claim opened, регистрация нового пользователя, KYB submit.

    `user_obj` — для user-related events (user_registered, kyb_submitted).
    `extra` — дополнительные плейсхолдеры для format() (например legal_name).
    """
    from .models import Conversation, Message

    fmt = {
        "id": (order.id if order else "—"),
        "rfq_id": (rfq.id if rfq else "—"),
        "status": (order.status if order else "—"),
        "username": (user_obj.username if user_obj else "—"),
        "email":    (user_obj.email if user_obj else "—"),
        "role":     {
            "buyer": gettext("Покупатель"),
            "seller": gettext("Поставщик"),
            "operator": gettext("Оператор"),
        }.get((extra or {}).get("role"), (extra or {}).get("role") or "—"),
        "legal_name": (extra or {}).get("legal_name", "—"),
    }
    body = text or _EVENT_TEXTS_OPERATOR.get(event, gettext("Системное уведомление"))
    try:
        body = body.format(**fmt)
    except KeyError:
        pass  # неполные плейсхолдеры — оставляем как есть
    cards = []
    actions = []
    title_prefix = ""
    if order:
        cards.append(_build_timeline_card(order))
        title_prefix = f"Сделка ORD-{order.id}"
        actions.append({"action": "get_order_detail", "label": gettext("📋 Открыть заказ"),
                          "params": {"order_id": order.id}})
    elif rfq:
        title_prefix = f"RFQ #{rfq.id}"
        if event == "sla_semi_overdue":
            actions.append({"action": "op_approve_kp", "label": gettext("▶️ Проверить предложение"),
                              "params": {"rfq_id": rfq.id}})
        if event == "sla_manual_overdue":
            actions.append({"action": "op_dispatch_manual_rfq",
                              "label": gettext("▶️ Сформировать КП"),
                              "params": {"rfq_id": rfq.id}})
    elif claim:
        title_prefix = f"Рекламация #{claim.id}"
        actions.append({"action": "claim_detail", "label": gettext("📋 Открыть"),
                          "params": {"claim_id": claim.id}})
    elif user_obj:
        title_prefix = f"@{user_obj.username}"
        if event == "user_registered":
            actions.append({"action": "admin_user_detail", "label": gettext("👤 Открыть профиль"),
                              "params": {"user_id": user_obj.id}})
        elif event == "kyb_submitted":
            actions.append({"action": "op_kyb_review", "label": gettext("🛡 Проверить KYB"),
                              "params": {"user_id": user_obj.id}})

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
                title=_("Алерты оператора"),
            )
        Message.objects.create(
            conversation=conv,
            role=Message.Role.SYSTEM,
            content=f"{title_prefix} · {body}" if title_prefix else body,
            cards=cards,
            actions=actions,
        )
        try:
            from marketplace.models import Notification
            from .consumers import push_notification_to_user
            notification = Notification.objects.create(
                user=op,
                kind="sla" if event.startswith("sla_") else "info",
                title=gettext("Операторское уведомление"),
                body=f"{title_prefix} · {body}" if title_prefix else body,
                url=f"/chat/?conv={conv.id}",
            )
            push_notification_to_user(op.id, {
                "id": notification.id,
                "kind": notification.kind,
                "title": notification.title,
                "body": notification.body,
                "url": notification.url,
                "event": event,
                "rfq_id": rfq.id if rfq else None,
                "order_id": order.id if order else None,
                "claim_id": claim.id if claim else None,
                "conversation_id": str(conv.id),
            })
        except Exception:
            logger.exception("operator notification delivery failed")

    # ── Bonus: всем операторам с подключённым Telegram — push в TG ──
    # Это даёт реальный канал доставки помимо WS/admin-chat (если оператор
    # сейчас не в чате — увидит в смартфоне).
    try:
        from .notif_settings import send_telegram_to_operators
        tg_text = f"{title_prefix} · {body}" if title_prefix else body
        send_telegram_to_operators(f"🚨 {event}\n\n{tg_text[:600]}")
    except Exception:
        logger.exception("operator_alert telegram fanout failed (non-fatal)")

    logger.info(f"operator_alert: {event} → {len(_operator_users())} ops")

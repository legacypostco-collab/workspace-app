from django import template

from marketplace.models import Order

register = template.Library()
ORDER_STATUS_LABELS = dict(Order.STATUS_CHOICES)
ROLE_LABELS = {
    "admin": "Администратор",
    "buyer": "Покупатель",
    "seller": "Поставщик",
    "operator": "Оператор",
    "operator_manager": "Менеджер по работе с клиентами",
    "operator_logist": "Логист",
    "operator_customs": "Таможенный специалист",
    "operator_payment": "Финансовый специалист",
    "system": "Система",
}
RFQ_STATUS_LABELS = {
    "new": "Новая",
    "quoted": "Предложения собраны",
    "needs_review": "Требует проверки",
    "cancelled": "Отменена",
}
AVAILABILITY_LABELS = {
    "active": "Доступна",
    "limited": "Ограниченный остаток",
    "made_to_order": "Под заказ",
    "discontinued": "Снята с производства",
    "blocked": "Заблокирована",
}
ACTIVITY_LABELS = {
    "order": "Заказ",
    "rfq": "Заявка",
    "pricelist": "Загрузка прайс-листа",
    "topup_confirmed": "Пополнение подтверждено",
    "topup_rejected": "Пополнение отклонено",
    "withdrawal_approved": "Выплата одобрена",
    "withdrawal_completed": "Выплата выполнена",
    "withdrawal_rejected": "Выплата отклонена",
    "admin_action": "Действие сотрудника",
    "payment_confirmed": "Платёж подтверждён",
    "payment_reversed": "Проводка отменена",
    "user_blocked": "Доступ пользователя заблокирован",
    "user_unblocked": "Доступ пользователя восстановлен",
    "part_publish": "Позиция опубликована",
    "part_hide": "Позиция скрыта",
}


@register.simple_tag(takes_context=True)
def query_replace(context, **kwargs):
    query = context["request"].GET.copy()
    for key, value in kwargs.items():
        if value in (None, ""):
            query.pop(key, None)
        else:
            query[key] = value
    return query.urlencode()


@register.filter
def initials(user):
    if not user:
        return "--"
    values = [getattr(user, "first_name", ""), getattr(user, "last_name", "")]
    letters = "".join(value[:1] for value in values if value).upper()
    return letters or (getattr(user, "username", "?")[:2].upper())


@register.filter
def status_tone(value):
    value = str(value or "").lower()
    if value in {
        "paid",
        "confirmed",
        "completed",
        "delivered",
        "verified",
        "active",
        "on_track",
        "trusted",
        "closed",
    }:
        return "success"
    if value in {
        "cancelled",
        "rejected",
        "blocked",
        "breached",
        "overdue",
        "risky",
        "failed",
        "reversed",
    }:
        return "danger"
    if value in {
        "awaiting_confirmation",
        "partially_paid",
        "pending",
        "needs_review",
        "at_risk",
        "open",
        "in_review",
    }:
        return "warning"
    return "neutral"


@register.filter
def order_status_label(value):
    return ORDER_STATUS_LABELS.get(str(value or ""), str(value or "Не указано"))


@register.filter
def role_label(value):
    value = str(value or "system")
    return ROLE_LABELS.get(value, value.replace("_", " ").capitalize())


@register.filter
def rfq_status_label(value):
    value = str(value or "")
    return RFQ_STATUS_LABELS.get(value, value.replace("_", " ").capitalize())


@register.filter
def availability_label(value):
    value = str(value or "")
    return AVAILABILITY_LABELS.get(value, value.replace("_", " ").capitalize())


@register.filter
def activity_label(value):
    value = str(value or "")
    return ACTIVITY_LABELS.get(value, value.replace("_", " ").capitalize())


EVENT_LABELS = {
    "order_created": "Создан заказ",
    "status_changed": "Изменён статус",
    "sla_status_changed": "Изменён контрольный срок",
    "payment_reported": "Покупатель сообщил об оплате",
    "payment_confirmed": "Подтверждён платёж",
    "payment_reversed": "Отменена проводка",
    "reserve_paid": "Подтверждён первый платёж",
    "final_payment_paid": "Подтверждён окончательный платёж",
    "document_uploaded": "Добавлен документ",
    "claim_opened": "Открыта рекламация",
    "claim_status_changed": "Изменена рекламация",
}


@register.filter
def event_label(value):
    return EVENT_LABELS.get(
        str(value or ""), str(value or "Событие").replace("_", " ").capitalize()
    )

"""Email notifications for key business events."""

import logging
from django.conf import settings
from django.core.mail import send_mail
from django.template.loader import render_to_string

logger = logging.getLogger(__name__)


def _send(subject: str, message: str, to: list[str]) -> bool:
    try:
        send_mail(subject, message, settings.DEFAULT_FROM_EMAIL, to, fail_silently=True)
        return True
    except Exception as e:
        logger.error("Email send failed: %s", e)
        return False


def notify_registration(user) -> bool:
    """Send welcome email after registration."""
    profile = getattr(user, "profile", None)
    role_label = dict(profile.ROLE_CHOICES).get(profile.role, profile.role) if profile else "пользователь"
    return _send(
        subject="Добро пожаловать в Consolidator Parts",
        message=(
            f"Здравствуйте, {user.get_full_name() or user.username}!\n\n"
            f"Ваш аккаунт создан.\n"
            f"Роль: {role_label}\n"
            f"Компания: {profile.company_name if profile else '—'}\n\n"
            f"Войти: {settings.DEFAULT_FROM_EMAIL.replace('noreply@', 'https://')}\n\n"
            f"С уважением,\nConsolidator Parts"
        ),
        to=[user.email],
    )


def notify_order_created(order) -> bool:
    """Notify buyer that order was created."""
    return _send(
        subject=f"Заказ #{order.id} создан — Consolidator Parts",
        message=(
            f"Здравствуйте, {order.customer_name}!\n\n"
            f"Ваш заказ #{order.id} успешно создан.\n"
            f"Сумма: ${order.total_amount}\n"
            f"Позиций: {order.items.count()}\n"
            f"Логистика: ${order.logistics_cost} ({order.logistics_provider})\n\n"
            f"Резерв к оплате (10%): ${(order.total_amount * 10 / 100):.2f}\n\n"
            f"С уважением,\nConsolidator Parts"
        ),
        to=[order.customer_email],
    )


def notify_order_status_changed(order, old_status: str, new_status: str) -> bool:
    """Notify buyer about order status change."""
    STATUS_LABELS = {
        "pending": "Ожидание оплаты",
        "reserve_paid": "Резерв оплачен",
        "confirmed": "Подтверждён поставщиком",
        "in_production": "В производстве",
        "ready_to_ship": "Готов к отгрузке",
        "shipped": "Отгружен",
        "transit_abroad": "В пути (за рубежом)",
        "customs": "На таможне",
        "transit_rf": "В пути (РФ)",
        "delivered": "Доставлен",
        "completed": "Завершён",
        "cancelled": "Отменён",
    }
    return _send(
        subject=f"Заказ #{order.id}: {STATUS_LABELS.get(new_status, new_status)}",
        message=(
            f"Здравствуйте, {order.customer_name}!\n\n"
            f"Статус заказа #{order.id} обновлён:\n"
            f"  {STATUS_LABELS.get(old_status, old_status)} → {STATUS_LABELS.get(new_status, new_status)}\n\n"
            f"С уважением,\nConsolidator Parts"
        ),
        to=[order.customer_email],
    )


def notify_payment_received(order, payment_type: str, amount) -> bool:
    """Notify about payment received."""
    labels = {"reserve": "Резерв", "final": "Финальная оплата", "mid": "Промежуточный платёж", "customs": "Таможенный платёж"}
    return _send(
        subject=f"Оплата получена — Заказ #{order.id}",
        message=(
            f"Здравствуйте, {order.customer_name}!\n\n"
            f"Получена оплата по заказу #{order.id}:\n"
            f"  Тип: {labels.get(payment_type, payment_type)}\n"
            f"  Сумма: ${amount}\n\n"
            f"С уважением,\nConsolidator Parts"
        ),
        to=[order.customer_email],
    )


def notify_seller_new_rfq(seller_user, rfq) -> bool:
    """Notify seller about new RFQ with their parts."""
    return _send(
        subject=f"Новый запрос RFQ #{rfq.id} — Consolidator Parts",
        message=(
            f"Здравствуйте, {seller_user.get_full_name() or seller_user.username}!\n\n"
            f"Получен новый запрос RFQ #{rfq.id}.\n"
            f"Клиент: {rfq.customer_name} ({rfq.company_name or '—'})\n"
            f"Позиций: {rfq.items.count()}\n"
            f"Срочность: {rfq.urgency}\n\n"
            f"Перейдите в кабинет поставщика для ответа.\n\n"
            f"С уважением,\nConsolidator Parts"
        ),
        to=[seller_user.email],
    )


def notify_seller_new_order(seller_user, order) -> bool:
    """Notify seller about new order with their parts."""
    return _send(
        subject=f"Новый заказ #{order.id} — Consolidator Parts",
        message=(
            f"Здравствуйте, {seller_user.get_full_name() or seller_user.username}!\n\n"
            f"Получен новый заказ #{order.id}.\n"
            f"Клиент: {order.customer_name}\n"
            f"Сумма: ${order.total_amount}\n\n"
            f"Перейдите в кабинет поставщика для обработки.\n\n"
            f"С уважением,\nConsolidator Parts"
        ),
        to=[seller_user.email],
    )


def notify_claim_opened(order, claim) -> bool:
    """Notify about new claim."""
    # Notify all sellers for this order
    seller_emails = set()
    for item in order.items.select_related("part__seller"):
        if item.part and item.part.seller and item.part.seller.email:
            seller_emails.add(item.part.seller.email)
    if not seller_emails:
        return False
    return _send(
        subject=f"Рекламация по заказу #{order.id} — {claim.title}",
        message=(
            f"Открыта рекламация по заказу #{order.id}.\n\n"
            f"Тема: {claim.title}\n"
            f"Описание: {claim.description}\n\n"
            f"Перейдите в карточку заказа для ответа.\n\n"
            f"С уважением,\nConsolidator Parts"
        ),
        to=list(seller_emails),
    )

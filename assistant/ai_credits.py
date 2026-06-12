"""AI-кредиты: лимит бесплатных AI-запросов на пользователя.

Идея (юнит-экономика): новый покупатель получает стартовый грант запросов к
Claude. Когда баланс исчерпан — AI больше не зовётся (не тратим деньги на тех,
кто не покупает), а пользователю показывается сообщение «оформите заказ». При
оплате заказа баланс пополняется. Плюс мягкий ежемесячный долив, чтобы
вернувшийся лид не был заблокирован навсегда.

Гейтим ТОЛЬКО покупателей. Операторы/продавцы/staff — без лимита.
Списываем только на ПЛАТНОМ slow-path (вызов Claude); fast-path и кэш бесплатны.

fail-open: любая ошибка учёта НЕ блокирует пользователя (лучше изредка
перерасходовать, чем сломать чат из-за бага).
"""
import logging

logger = logging.getLogger(__name__)

START_GRANT = 25       # стартовый грант новому покупателю (default поля в модели)
PURCHASE_GRANT = 50    # пополнение за каждый оплаченный заказ (резерв)
MONTHLY_REFILL = 10    # мягкий ежемесячный долив до этого минимума


def _profile(user):
    return getattr(user, "profile", None)


def is_gated(user, role=None):
    """True только для реальных покупателей. Операторы/продавцы/staff/аноним — нет."""
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_staff", False) or getattr(user, "is_superuser", False):
        return False
    if role and role != "buyer":
        return False
    p = _profile(user)
    if p is None:
        return False
    if getattr(p, "role", "buyer") in ("operator", "seller"):
        return False
    return True


def _maybe_monthly_refill(p):
    """Раз в ~30 дней доводим баланс минимум до MONTHLY_REFILL (не суммируем)."""
    if MONTHLY_REFILL <= 0:
        return
    try:
        from django.utils import timezone
        from marketplace.models import UserProfile
        now = timezone.now()
        last = p.ai_credits_refilled_at
        if last is None or (now - last).days >= 30:
            new_credits = max(int(p.ai_credits or 0), MONTHLY_REFILL)
            UserProfile.objects.filter(pk=p.pk).update(
                ai_credits=new_credits, ai_credits_refilled_at=now)
            p.ai_credits = new_credits
            p.ai_credits_refilled_at = now
    except Exception:
        logger.exception("ai_credits monthly refill failed")


def remaining(user, role=None):
    if not is_gated(user, role):
        return -1  # без лимита
    p = _profile(user)
    return int(getattr(p, "ai_credits", 0) or 0)


def try_consume(user, role=None, n=1):
    """Списать n кредитов перед платным вызовом Claude.

    Возвращает (allowed: bool, remaining: int).
      • не-гейтим (оператор/продавец/staff) → (True, -1), без списания.
      • гейтим и хватает → списываем, (True, остаток).
      • гейтим и не хватает → (False, 0), вызывать Claude НЕ нужно.
    """
    if not is_gated(user, role):
        return True, -1
    p = _profile(user)
    if p is None:
        return True, -1
    try:
        _maybe_monthly_refill(p)
        from django.db.models import F
        from marketplace.models import UserProfile
        if int(p.ai_credits or 0) < n:
            return False, 0
        # Атомарный декремент с условием — без гонок при двойном клике.
        updated = UserProfile.objects.filter(pk=p.pk, ai_credits__gte=n).update(
            ai_credits=F("ai_credits") - n)
        if not updated:
            return False, 0
        p.refresh_from_db(fields=["ai_credits"])
        return True, int(p.ai_credits)
    except Exception:
        logger.exception("ai_credits consume failed — fail-open")
        return True, -1


def grant_on_purchase(user, amount=PURCHASE_GRANT):
    """Пополнить баланс при оплате заказа (покупка = обновление лимита)."""
    p = _profile(user)
    if p is None:
        return
    try:
        from django.db.models import F
        from marketplace.models import UserProfile
        UserProfile.objects.filter(pk=p.pk).update(ai_credits=F("ai_credits") + amount)
        logger.info("ai_credits +%s granted to user %s (purchase)", amount,
                    getattr(user, "id", "?"))
    except Exception:
        logger.exception("ai_credits grant failed")


def limit_message():
    """Сообщение + действия, когда лимит исчерпан. Навигация/оплата — кнопками
    (они НЕ зовут AI), так что купить можно всегда."""
    return {
        "text": (
            "🔒 Лимит бесплатных AI-запросов исчерпан.\n"
            "Оформите заказ — и помощник снова станет доступен. "
            "Каталог, цены, ваши заказы и оплата работают как обычно."
        ),
        "actions": [
            {"label": "📦 Мои заказы", "action": "get_orders", "params": {}},
            {"label": "💼 Мои сделки", "action": "get_my_deals", "params": {}},
        ],
        "suggestions": [],
    }

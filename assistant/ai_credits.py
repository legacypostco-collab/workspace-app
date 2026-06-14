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

START_GRANT = 25       # стартовый грант новому пользователю (default поля в модели)
PURCHASE_GRANT = 50    # +запросов за оплаченный заказ (покупатель) / завершённую продажу (продавец)
MONTHLY_REFILL = 10    # мягкий ежемесячный долив покупателю/продавцу (до этого минимума)
OPERATOR_MONTHLY = 100 # ежемесячный долив оператору (внутренний сотрудник, депозита нет)


def _profile(user):
    return getattr(user, "profile", None)


def is_gated(user, role=None):
    """True для покупателей, продавцов И операторов — лимитируем всех.
    НЕ гейтим: аноним (slow-path ему и так недоступен), суперюзер (владелец
    платформы), роль admin."""
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return False
    p = _profile(user)
    if p is None:
        return False
    eff_role = role or getattr(p, "role", "buyer")
    if eff_role == "admin":
        return False
    return True


def _maybe_monthly_refill(p):
    """Раз в ~30 дней доводим баланс до минимума (не суммируем). Оператору —
    до OPERATOR_MONTHLY (у него нет депозита, пополнять нечем), покупателю/
    продавцу — до MONTHLY_REFILL (мягкая страховка от вечной блокировки)."""
    target = OPERATOR_MONTHLY if getattr(p, "role", "") == "operator" else MONTHLY_REFILL
    if target <= 0:
        return
    try:
        from django.utils import timezone
        from marketplace.models import UserProfile
        now = timezone.now()
        last = p.ai_credits_refilled_at
        if last is None or (now - last).days >= 30:
            new_credits = max(int(p.ai_credits or 0), target)
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


def grant_on_purchase(user, amount=PURCHASE_GRANT, reason="purchase"):
    """Начислить запросы (покупателю за оплату заказа / продавцу за продажу)."""
    p = _profile(user)
    if p is None:
        return
    try:
        from django.db.models import F
        from marketplace.models import UserProfile
        UserProfile.objects.filter(pk=p.pk).update(ai_credits=F("ai_credits") + amount)
        logger.info("ai_credits +%s granted to user %s (%s)", amount,
                    getattr(user, "id", "?"), reason)
    except Exception:
        logger.exception("ai_credits grant failed")


def grant_on_sale(user, amount=PURCHASE_GRANT):
    """Начислить продавцу запросы за завершённую продажу (эскроу освобождён)."""
    return grant_on_purchase(user, amount, reason="sale")


def rate_ok(user, bucket, limit, window_s):
    """Грубый per-user лимит вызовов тяжёлых AI-эндпоинтов (защита от
    «сжигания» токенов циклом). Fixed-window через cache. fail-open при ошибке.

    Гейтит ВСЕХ (в т.ч. продавцов/операторов, которых ai_credits не лимитирует),
    т.к. дорогие вызовы (vision, прайс-оценка) есть и у не-покупателей.
    Возвращает True, пока в окне меньше `limit` вызовов.
    """
    try:
        from django.core.cache import cache
        uid = getattr(user, "id", None) or "anon"
        key = f"airl:{bucket}:{uid}"
        # add создаёт счётчик с TTL только при первом вызове в окне
        if cache.add(key, 1, window_s):
            return True
        try:
            return cache.incr(key) <= limit
        except ValueError:
            # ключ истёк между add и incr — начинаем окно заново
            cache.set(key, 1, window_s)
            return True
    except Exception:
        logger.exception("rate_ok failed — fail-open")
        return True


def limit_message(role=None):
    """Сообщение + действия при исчерпании лимита, по роли. Навигация/оплата —
    кнопками (они НЕ зовут AI), так что работать с платформой можно всегда."""
    r = role or "buyer"
    if r == "seller":
        return {
            "text": (
                "🔒 Бесплатные AI-запросы закончились.\n"
                "Завершите продажу (+50 запросов) или купите пакет с депозита. "
                "Каталог, сделки и выплаты работают как обычно."
            ),
            "actions": [
                {"label": "💳 Купить запросы", "action": "buy_ai_requests", "params": {}},
                {"label": "💼 Мои сделки", "action": "get_my_deals", "params": {}},
            ],
            "suggestions": [],
        }
    if r.startswith("operator"):
        return {
            "text": (
                "🔒 Лимит AI-запросов на этот месяц исчерпан.\n"
                "Он обновится автоматически в начале следующего месяца."
            ),
            "actions": [],
            "suggestions": [],
        }
    return {
        "text": (
            "🔒 Бесплатные AI-запросы закончились.\n"
            "Оформите заказ (+50 запросов) или купите пакет с депозита. "
            "Каталог, цены, заказы и оплата работают как обычно."
        ),
        "actions": [
            {"label": "💳 Купить запросы", "action": "buy_ai_requests", "params": {}},
            {"label": "📦 Мои заказы", "action": "get_orders", "params": {}},
        ],
        "suggestions": [],
    }

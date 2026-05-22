"""Per-user AI budget enforcer.

Защищает от ситуации «один тестер за час выжег весь Anthropic-budget».
Считает потраченные центы по `usage.input_tokens` + `usage.output_tokens` от
Anthropic / OpenAI и блокирует, когда суточный лимит превышен.

Стоимости (на Claude Sonnet 4.5, Nov 2025):
  input:  $3.00 / 1M tokens
  output: $15.00 / 1M tokens

Хранится в Django cache (Redis в prod), ключ `ai_budget:<user_id>:<YYYY-MM-DD>`,
TTL 36 часов (с запасом на день+ночь).

Использование:
  from assistant.ai_budget import check_budget_or_raise, record_usage

  check_budget_or_raise(user)  # raises BudgetExceeded если >= limit
  resp = client.messages.create(...)
  record_usage(user, input_tokens=resp.usage.input_tokens,
                     output_tokens=resp.usage.output_tokens)
"""
from __future__ import annotations

import logging
from datetime import date

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)


class BudgetExceeded(Exception):
    """Raised when user has exceeded their daily AI budget."""
    def __init__(self, user_id: int, spent_usd: float, limit_usd: float):
        self.user_id = user_id
        self.spent_usd = spent_usd
        self.limit_usd = limit_usd
        super().__init__(
            f"Daily AI budget exceeded for user {user_id}: "
            f"${spent_usd:.4f} >= ${limit_usd:.2f}"
        )


# ── Pricing (USD per 1M tokens) ──────────────────────────────────
# Можно переопределить через env: AI_PRICE_IN_PER_MTOK / AI_PRICE_OUT_PER_MTOK
def _price_in_per_mtok() -> float:
    import os
    return float(os.environ.get("AI_PRICE_IN_PER_MTOK", "3.00"))


def _price_out_per_mtok() -> float:
    import os
    return float(os.environ.get("AI_PRICE_OUT_PER_MTOK", "15.00"))


def _cache_key(user_id: int) -> str:
    return f"ai_budget:{user_id}:{date.today().isoformat()}"


def get_spent_usd(user) -> float:
    """Сколько user уже потратил сегодня (в долларах)."""
    if user is None or not getattr(user, "is_authenticated", False):
        return 0.0
    try:
        cents = cache.get(_cache_key(user.id), 0)
    except Exception:
        cents = 0
    return float(cents) / 100.0


def get_limit_usd() -> float:
    return float(getattr(settings, "AI_DAILY_BUDGET_USD", 5.00))


def check_budget_or_raise(user) -> None:
    """Поднимает `BudgetExceeded` если лимит превышен. Иначе — pass-through.

    Для анонимных и системных вызовов (user=None) — всегда разрешает (но
    учитывать ещё может быть полезно отдельно через global counter).
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return
    spent = get_spent_usd(user)
    limit = get_limit_usd()
    if spent >= limit:
        raise BudgetExceeded(user.id, spent, limit)


def record_usage(user, *, input_tokens: int = 0, output_tokens: int = 0) -> float:
    """Зафиксировать использование. Возвращает сколько центов добавлено.

    Безопасно при кэш-сбое: ошибка логируется, но не блокирует ответ юзеру.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return 0.0
    # cost = (in/1M * price_in) + (out/1M * price_out), в долларах → центы
    cost_usd = (
        (input_tokens / 1_000_000.0) * _price_in_per_mtok()
        + (output_tokens / 1_000_000.0) * _price_out_per_mtok()
    )
    cost_cents = int(round(cost_usd * 100))
    if cost_cents <= 0:
        return 0.0
    try:
        key = _cache_key(user.id)
        # Atomic increment с TTL. Если ключа нет — set→add 1 + cost_cents - 1
        try:
            new_val = cache.incr(key, cost_cents)
        except ValueError:
            # Key doesn't exist
            cache.set(key, cost_cents, timeout=60 * 60 * 36)  # 36h
            new_val = cost_cents
        if new_val >= get_limit_usd() * 100:
            logger.warning(
                "user %s exceeded AI daily budget: $%.4f >= $%.2f",
                user.id, new_val / 100.0, get_limit_usd(),
            )
    except Exception:
        logger.exception("record_usage cache failure (non-fatal)")
        return 0.0
    return float(cost_cents)

"""Unit tests for assistant.ai_budget — per-user AI cost-cap."""
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import override_settings

from assistant.ai_budget import (
    BudgetExceeded, check_budget_or_raise, get_limit_usd,
    get_spent_usd, record_usage,
)

User = get_user_model()


@pytest.fixture
def user(db):
    cache.clear()
    return User.objects.create_user(username="budget_test", password="x")


def test_anonymous_user_never_blocked(user):
    """Анонимные / None юзеры всегда проходят."""
    check_budget_or_raise(None)  # no-op
    assert get_spent_usd(None) == 0.0
    assert record_usage(None, input_tokens=1000, output_tokens=500) == 0.0


@override_settings(AI_DAILY_BUDGET_USD=1.00)
def test_record_usage_calculates_cents_correctly(user):
    """1M input tokens на $3 = $3.00 = 300 центов."""
    cost = record_usage(user, input_tokens=1_000_000, output_tokens=0)
    assert cost == 300.0  # cents
    assert get_spent_usd(user) == 3.0
    # И будет блокировать
    with pytest.raises(BudgetExceeded):
        check_budget_or_raise(user)


@override_settings(AI_DAILY_BUDGET_USD=5.00)
def test_check_passes_below_limit(user):
    """Юзер потратил $0.50 — проходит."""
    record_usage(user, input_tokens=50_000, output_tokens=10_000)  # ~$0.30
    check_budget_or_raise(user)  # no raise


@override_settings(AI_DAILY_BUDGET_USD=0.01)
def test_check_raises_at_limit(user):
    """Превышение лимита → BudgetExceeded."""
    record_usage(user, input_tokens=10_000, output_tokens=0)  # $0.03
    with pytest.raises(BudgetExceeded) as exc:
        check_budget_or_raise(user)
    assert exc.value.user_id == user.id
    assert exc.value.spent_usd >= 0.01


def test_record_usage_zero_tokens_no_op(user):
    """0 токенов → 0 центов."""
    assert record_usage(user, input_tokens=0, output_tokens=0) == 0.0


def test_record_usage_handles_cache_failure_gracefully(user):
    """Если cache сбойнул — не упадём."""
    with patch("django.core.cache.cache.incr", side_effect=Exception("Redis down")):
        # Не должен поднять исключение
        result = record_usage(user, input_tokens=1000, output_tokens=500)
        # Возвращает 0 либо сколько-то — главное не raise


@override_settings()
def test_get_limit_defaults_to_5_usd():
    """Если AI_DAILY_BUDGET_USD не задан — fallback $5."""
    from django.conf import settings
    if hasattr(settings, "AI_DAILY_BUDGET_USD"):
        del settings.AI_DAILY_BUDGET_USD
    assert get_limit_usd() == 5.00

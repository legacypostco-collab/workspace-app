"""Tests for assistant.order_limits — минимум $7000."""
from decimal import Decimal

import pytest
from django.test import override_settings

from assistant.order_limits import check_min_order, min_order_usd


def test_default_min_is_7000():
    assert min_order_usd() == Decimal("7000")


@override_settings(MIN_ORDER_USD="9999")
def test_env_override():
    assert min_order_usd() == Decimal("9999")


def test_above_limit_returns_none():
    """≥ лимита — None (заказ можно создавать)."""
    assert check_min_order(7000) is None
    assert check_min_order(Decimal("7000.00")) is None
    assert check_min_order(10000) is None
    assert check_min_order(1_000_000) is None


def test_below_limit_returns_block():
    """< лимита — dict с warning-card."""
    block = check_min_order(6999)
    assert block is not None
    assert "7,000" in block["text"]
    assert "$1" in block["text"]  # shortage = $1
    assert any(c.get("type") == "kpi_grid" for c in block["cards"])
    # Кнопки помогают buyer'у добрать
    labels = [a["label"] for a in block["actions"]]
    assert any("Добавить" in l or "📎" in l for l in labels)


def test_zero_returns_block():
    block = check_min_order(0)
    assert block is not None
    assert "Добрать" in str(block["cards"]) or "Добрать" in block["text"]


def test_invalid_input_fails_closed():
    """Непроверенная сумма не должна обходить финансовое ограничение."""
    for value in ("not-a-number", None, Decimal("NaN"), Decimal("Infinity")):
        block = check_min_order(value)  # type: ignore[arg-type]
        assert block is not None
        assert "$0.00" in block["text"]

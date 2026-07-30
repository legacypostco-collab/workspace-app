"""E2E: order/rfq cards в чате должны быть кликабельны."""
from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect


def _first_order_card(page: Page):
    page.locator(".pill[data-pid^='get_my_deals#']:visible").last.click()
    page.wait_for_selector(".msg-assistant", timeout=15000)
    cards = page.locator(".card-clickable[data-action='track_order']:visible")
    if cards.count() == 0:
        pytest.skip("the configured buyer has no active orders")
    return cards.first


def test_order_cards_have_clickable_class(buyer_page: Page):
    """После «Мои заказы» карточки имеют класс .card-clickable + data-action."""
    page = buyer_page
    card = _first_order_card(page)
    expect(card).to_be_visible()


def test_clicking_order_card_triggers_track_order(buyer_page: Page):
    """Клик по карточке заказа открывает действие track_order (новое сообщение)."""
    page = buyer_page
    card = _first_order_card(page)

    # Считаем сообщения до клика
    msgs_before = page.locator(".msg-assistant").count()

    # Клик по первой карточке
    card.click()

    # Ждём нового assistant-сообщения (track_order ответ)
    page.wait_for_function(
        f"() => document.querySelectorAll('.msg-assistant').length > {msgs_before}",
        timeout=20000,
    )


def test_card_clickable_keyboard_accessibility(buyer_page: Page):
    """Карточки имеют tabindex и role=button для accessibility."""
    page = buyer_page
    card = _first_order_card(page)
    assert card.get_attribute("tabindex") == "0"
    assert card.get_attribute("role") == "button"

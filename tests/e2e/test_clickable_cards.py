"""E2E: order/rfq cards в чате должны быть кликабельны."""
from __future__ import annotations

from playwright.sync_api import Page, expect


def test_order_cards_have_clickable_class(buyer_page: Page):
    """После «Мои заказы» карточки имеют класс .card-clickable + data-action."""
    page = buyer_page
    page.locator(".pill", has_text="Мои заказы").first.click()
    # ждём карточки заказа
    page.wait_for_selector(".card-clickable[data-action='track_order']", timeout=20000)
    cards = page.locator(".card-clickable[data-action='track_order']")
    n = cards.count()
    assert n >= 1, "expected at least one clickable order card"


def test_clicking_order_card_triggers_track_order(buyer_page: Page):
    """Клик по карточке заказа открывает действие track_order (новое сообщение)."""
    page = buyer_page
    page.locator(".pill", has_text="Мои заказы").first.click()
    page.wait_for_selector(".card-clickable[data-action='track_order']", timeout=20000)

    # Считаем сообщения до клика
    msgs_before = page.locator(".msg-assistant").count()

    # Клик по первой карточке
    page.locator(".card-clickable[data-action='track_order']").first.click()

    # Ждём нового assistant-сообщения (track_order ответ)
    page.wait_for_function(
        f"() => document.querySelectorAll('.msg-assistant').length > {msgs_before}",
        timeout=20000,
    )


def test_card_clickable_keyboard_accessibility(buyer_page: Page):
    """Карточки имеют tabindex и role=button для accessibility."""
    page = buyer_page
    page.locator(".pill", has_text="Мои заказы").first.click()
    page.wait_for_selector(".card-clickable[data-action='track_order']", timeout=20000)
    card = page.locator(".card-clickable[data-action='track_order']").first
    assert card.get_attribute("tabindex") == "0"
    assert card.get_attribute("role") == "button"

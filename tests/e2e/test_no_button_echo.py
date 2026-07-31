"""E2E: клик по кнопке/карточке не пишет ярлык в чат как user-сообщение."""
from __future__ import annotations

import pytest
from playwright.sync_api import Page


def test_pill_click_does_not_echo_label_to_chat(buyer_page: Page):
    """После клика на pill «Мои заказы» в чате нет user-сообщения с её текстом."""
    page = buyer_page
    page.locator(".pill[data-pid^='get_my_deals#']").first.click()
    # Ждём ответ assistant
    page.wait_for_selector(".msg-assistant", timeout=15000)
    # Служебная action-запись хранится на сервере, но в ленту не выводится.
    assert page.locator(".msg-action-tag, .msg-action").count() == 0, \
        "не должно быть msg-action после клика по pill — ярлык кнопки не падает в чат"


def test_card_click_does_not_echo_label_to_chat(buyer_page: Page):
    """Клик по order-карточке не пишет «▸ ORD-N» в чат."""
    page = buyer_page
    page.locator(".pill[data-pid^='get_my_deals#']").first.click()
    page.wait_for_selector(".msg-assistant", timeout=15000)
    cards = page.locator(".card-clickable[data-action='track_order']")
    if cards.count() == 0:
        pytest.skip("the configured buyer has no active orders")
    # Считаем msg-action до клика (должно быть 0)
    assert page.locator(".msg-action-tag, .msg-action").count() == 0
    # Кликнем карточку
    cards.first.click()
    # Дождёмся новых cards (трекинг)
    page.wait_for_timeout(2000)
    # И всё ещё нет msg-action
    assert page.locator(".msg-action-tag, .msg-action").count() == 0, \
        "клик по карточке не должен оставлять msg-action в чате"

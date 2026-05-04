"""E2E: клик по кнопке/карточке не пишет ярлык в чат как user-сообщение."""
from __future__ import annotations

from playwright.sync_api import Page


def test_pill_click_does_not_echo_label_to_chat(buyer_page: Page):
    """После клика на pill «Мои заказы» в чате нет user-сообщения с её текстом."""
    page = buyer_page
    page.locator(".pill", has_text="Мои заказы").first.click()
    # Ждём ответ assistant
    page.wait_for_selector(".msg-assistant", timeout=15000)
    # msg-action (старый «▸ Label») не должен появляться
    assert page.locator(".msg-action").count() == 0, \
        "не должно быть msg-action после клика по pill — ярлык кнопки не падает в чат"


def test_card_click_does_not_echo_label_to_chat(buyer_page: Page):
    """Клик по order-карточке не пишет «▸ ORD-N» в чат."""
    page = buyer_page
    page.locator(".pill", has_text="Мои заказы").first.click()
    page.wait_for_selector(".card-clickable[data-action='track_order']", timeout=20000)
    # Считаем msg-action до клика (должно быть 0)
    assert page.locator(".msg-action").count() == 0
    # Кликнем карточку
    page.locator(".card-clickable[data-action='track_order']").first.click()
    # Дождёмся новых cards (трекинг)
    page.wait_for_timeout(2000)
    # И всё ещё нет msg-action
    assert page.locator(".msg-action").count() == 0, \
        "клик по карточке не должен оставлять msg-action в чате"

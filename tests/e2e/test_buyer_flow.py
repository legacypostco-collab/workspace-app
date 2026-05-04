"""E2E: buyer click-pill → action → response card."""
from __future__ import annotations

from playwright.sync_api import Page, expect


def test_balance_pill_returns_kpi_card(buyer_page: Page):
    """Клик по «Баланс депозита» → AI отвечает kpi-карточкой с депозитом."""
    page = buyer_page
    # Найти и кликнуть pill с «Баланс»
    page.locator(".pill", has_text="Баланс").click()
    # Дождаться сообщения assistant — может быть как сообщение или KPI-grid
    page.wait_for_selector(".msg-assistant, .kpi-grid", timeout=15000)
    # Проверим что в ответе упомянут депозит
    body_text = page.locator("#streamInner").inner_text(timeout=2000).lower()
    assert "депозит" in body_text or "$" in body_text, f"expected balance info, got: {body_text[:200]}"


def test_orders_pill_lists_orders_or_empty(buyer_page: Page):
    """«Мои заказы» возвращает либо список карточек, либо «нет заказов»."""
    page = buyer_page
    page.locator(".pill", has_text="Мои заказы").click()
    page.wait_for_selector(".msg-assistant", timeout=15000)
    body_text = page.locator("#streamInner").inner_text(timeout=2000).lower()
    # У demo_buyer обычно есть заказы из seed; в худшем — текст что заказов нет
    assert "заказ" in body_text or "order" in body_text, \
        f"expected orders info, got: {body_text[:200]}"


def test_text_input_message(buyer_page: Page):
    """Отправить произвольное сообщение через input — assistant отвечает."""
    page = buyer_page
    # Найти heroInput (он в welcome) или input (в chat-active)
    input_el = page.locator("#heroInput, #input").first
    input_el.fill("какой у меня баланс?")
    # Enter
    input_el.press("Enter")
    # Дождаться что появилось user-message
    page.wait_for_selector(".msg-user", timeout=10000)
    # И assistant ответил
    page.wait_for_selector(".msg-assistant", timeout=20000)

"""E2E: buyer click-pill → action → response card."""
from __future__ import annotations

from playwright.sync_api import Page


def test_settlements_pill_returns_documents_or_empty_state(buyer_page: Page):
    """Раздел расчётов показывает счета и договоры либо пустое состояние."""
    page = buyer_page
    page.locator(".pill[data-pid^='settlement_my_documents#']").click()
    page.wait_for_selector(".msg-assistant", timeout=15000)
    body_text = page.locator("#streamInner").inner_text(timeout=2000).lower()
    assert any(
        marker in body_text
        for marker in ("счёт", "счет", "договор", "расчёт", "расчет")
    ), f"expected settlement info, got: {body_text[:200]}"


def test_deals_pill_lists_requests_orders_or_empty(buyer_page: Page):
    """Действие сделок возвращает список карточек или корректное empty-state."""
    page = buyer_page
    page.locator(".pill[data-pid^='get_my_deals#']").click()
    page.wait_for_selector(".msg-assistant", timeout=15000)
    body_text = page.locator("#streamInner").inner_text(timeout=2000).lower()
    # Тестовый покупатель может иметь заказы; иначе ожидается корректное empty-state.
    assert "сделк" in body_text and any(
        marker in body_text for marker in ("rfq", "заказ", "нет активных")
    ), \
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

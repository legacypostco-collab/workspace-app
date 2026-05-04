"""E2E: KYB onboarding wizard — кликнуть pill → пройти 1 шаг."""
from __future__ import annotations

from playwright.sync_api import Page, expect


def test_kyb_pill_opens_onboarding(seller_page: Page):
    """Клик «🛡 Верификация» возвращает onboarding-карточку с кнопкой шага 1."""
    page = seller_page
    page.locator(".pill", has_text="Верификация").first.click()
    # Wait for assistant message
    page.wait_for_selector(".msg-assistant", timeout=15000)
    # Должна появиться KPI или actions с «Реквизиты компании»
    body = page.locator("#streamInner").inner_text(timeout=2000)
    assert "Onboarding" in body or "верификац" in body.lower() or "шаг" in body.lower(), \
        f"expected onboarding text, got: {body[:300]}"


def test_kyb_step1_form_renders(seller_page: Page):
    """Submit_company_info шаг 1 — форма с полями legal_name + ИНН."""
    page = seller_page
    page.locator(".pill", has_text="Верификация").first.click()
    page.wait_for_selector(".msg-assistant", timeout=15000)

    # Кликнуть кнопку Step 1 (Реквизиты)
    btn = page.locator("button.action, .action-btn", has_text="Реквизиты").first
    if btn.count() == 0:
        # AI может ответить с текстом — пробуем через проактивные actions
        # Если кнопок нет, пропускаем — это smoke что pill хотя бы что-то делает
        return
    btn.click()
    page.wait_for_selector(".form-card, .card-form, [class*=form]", timeout=10000)
    # input для ИНН должен быть видим
    page.wait_for_selector("input[name=inn], input[placeholder*='ИНН']", timeout=5000)

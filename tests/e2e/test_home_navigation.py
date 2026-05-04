"""E2E: 🏠 Home button + breadcrumbs."""
from __future__ import annotations

from playwright.sync_api import Page, expect


def test_home_button_visible_in_topbar(buyer_page: Page):
    """Кнопка 🏠 видна в topbar."""
    page = buyer_page
    expect(page.locator("#topHome")).to_be_visible(timeout=5000)


def test_home_button_returns_to_welcome(buyer_page: Page):
    """После клика на pill (открыли действие) → клик 🏠 возвращает к welcome."""
    page = buyer_page
    welcome = page.locator("#welcomeStage")
    # Изначально welcome видим (после fixture'ы)
    welcome_classes_before = welcome.get_attribute("class") or ""
    assert "hidden" not in welcome_classes_before

    # Клик по пилюле → conv stage
    page.locator(".pill", has_text="Мои заказы").first.click()
    page.wait_for_selector(".msg-assistant", timeout=15000)
    welcome_classes_mid = welcome.get_attribute("class") or ""
    assert "hidden" in welcome_classes_mid, "welcome должен скрыться после действия"

    # Клик 🏠 → welcome снова видим
    page.locator("#topHome").click()
    page.wait_for_timeout(300)
    welcome_classes_after = welcome.get_attribute("class") or ""
    assert "hidden" not in welcome_classes_after, \
        f"welcome должен снова показаться, classes={welcome_classes_after!r}"


def test_breadcrumbs_appear_on_pill_click(buyer_page: Page):
    """После клика на pill в topCrumbs появляется крошка с её ярлыком."""
    page = buyer_page
    page.locator(".pill", has_text="Мои заказы").first.click()
    page.wait_for_selector(".msg-assistant", timeout=15000)
    # В крошках должен быть «Мои заказы»
    crumbs = page.locator("#topCrumbs .crumb").all_text_contents()
    assert any("Мои заказы" in c for c in crumbs), f"crumbs не содержат Мои заказы: {crumbs}"
    # И «🏠» как первая
    assert "🏠" in (crumbs[0] if crumbs else ""), f"первая крошка не 🏠: {crumbs}"


def test_home_action_in_contextual_actions(buyer_page: Page):
    """Auto-attached «🏠 Главная» появляется в contextual_actions блоке."""
    page = buyer_page
    page.locator(".pill", has_text="Мои заказы").first.click()
    page.wait_for_selector(".msg-assistant", timeout=15000)
    # В msg-ctx-actions должна быть кнопка с «Главная»
    ctx_buttons = page.locator(".msg-ctx-actions").all_text_contents()
    joined = " ".join(ctx_buttons)
    assert "Главная" in joined, f"нет «Главная» в contextual actions: {joined!r}"


def test_breadcrumbs_clear_after_home(buyer_page: Page):
    """После клика 🏠 крошки сбрасываются."""
    page = buyer_page
    page.locator(".pill", has_text="Мои заказы").first.click()
    page.wait_for_selector(".msg-assistant", timeout=15000)
    page.locator("#topHome").click()
    page.wait_for_timeout(300)
    crumbs = page.locator("#topCrumbs .crumb").all_text_contents()
    # После home крошки должны быть пусты или показывать только 🏠 (нет, при пустом stack render возвращает '')
    assert len([c for c in crumbs if c.strip() and "🏠" not in c]) == 0, \
        f"crumbs не очистились: {crumbs}"

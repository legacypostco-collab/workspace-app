"""E2E: settings drawer — открыть, переключить тёмную тему."""
from __future__ import annotations

from playwright.sync_api import Page, expect


def test_settings_drawer_opens_and_closes(buyer_page: Page):
    """Шестерёнка в side-foot открывает drawer; outside-click закрывает."""
    page = buyer_page

    drawer = page.locator("#settingsPanel")
    # Изначально скрыт
    expect(drawer).to_be_hidden()

    # Кликнуть кнопку настроек
    page.locator(".side-settings").click()
    expect(drawer).to_be_visible(timeout=2000)

    # Проверим наличие тумблеров
    expect(page.locator("#settingNotifSound")).to_be_visible()
    expect(page.locator("#settingDarkMode")).to_be_visible()
    expect(page.locator("#settingLang")).to_be_visible()

    # Outside click закрывает (клик в topbar — всегда видим)
    page.locator(".topbar").first.click(force=True)
    page.wait_for_timeout(400)
    expect(drawer).to_be_hidden()


def test_dark_mode_toggle_applies(buyer_page: Page):
    """Тумблер тёмной темы → body получает класс dark-mode."""
    page = buyer_page

    page.locator(".side-settings").click()
    page.wait_for_selector("#settingsPanel:not([hidden])", timeout=2000)

    # Тумблер dark mode
    dark = page.locator("#settingDarkMode")
    is_checked = dark.is_checked()
    if is_checked:
        # выключим если уже включено, чтобы стартовать с known state
        dark.click()
        page.wait_for_timeout(200)
        assert "dark-mode" not in (page.locator("body").get_attribute("class") or "")
    # Включаем
    dark.click()
    page.wait_for_timeout(300)
    body_classes = page.locator("body").get_attribute("class") or ""
    assert "dark-mode" in body_classes, f"expected dark-mode class, got: {body_classes!r}"


def test_notif_sound_toggle_persists_to_localstorage(buyer_page: Page):
    """Тумблер звука пишет cf_notif_sound=0/1 в localStorage."""
    page = buyer_page
    page.locator(".side-settings").click()
    page.wait_for_selector("#settingsPanel:not([hidden])", timeout=2000)

    snd = page.locator("#settingNotifSound")
    # Стартовое значение: должно быть включено (default)
    initial = snd.is_checked()
    snd.click()
    page.wait_for_timeout(200)
    expected_after = "0" if initial else "1"
    val = page.evaluate("() => localStorage.getItem('cf_notif_sound')")
    assert val == expected_after, f"expected localStorage cf_notif_sound={expected_after}, got {val!r}"

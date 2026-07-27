"""E2E: workspace, command menu and explicit new conversations."""
from __future__ import annotations

from playwright.sync_api import Page, expect


def test_home_button_visible_in_topbar(buyer_page: Page):
    expect(buyer_page.locator("#topHome")).to_be_visible(timeout=5000)


def test_home_preserves_current_conversation(buyer_page: Page):
    page = buyer_page
    welcome = page.locator("#welcomeStage")

    page.locator(".pill", has_text="Мои сделки").first.click()
    page.wait_for_selector(".msg-assistant", timeout=15000)
    conv_id = page.evaluate("window.sessionStorage.getItem('cf_active_conv')")
    message_count = page.locator("#streamInner .msg").count()
    assert conv_id

    page.locator("#topHome").click()
    expect(welcome).to_be_visible()
    assert page.evaluate("window.sessionStorage.getItem('cf_active_conv')") == conv_id
    assert page.locator("#streamInner .msg").count() == message_count
    expect(page.locator("#resumeConversation")).to_be_visible()

    page.locator("#resumeConversation").click()
    expect(page.locator("#convStage")).to_be_visible()
    assert page.locator("#streamInner .msg").count() == message_count


def test_navigation_is_not_duplicated_under_each_answer(buyer_page: Page):
    page = buyer_page
    page.locator(".pill", has_text="Мои сделки").first.click()
    page.wait_for_selector(".msg-assistant", timeout=15000)

    joined = " ".join(page.locator(".msg-ctx-actions").all_text_contents())
    assert "Главная" not in joined


def test_new_chat_gets_a_new_conversation_id(buyer_page: Page):
    page = buyer_page
    page.locator(".pill", has_text="Мои сделки").first.click()
    page.wait_for_selector(".msg-assistant", timeout=15000)
    first_id = page.evaluate("window.sessionStorage.getItem('cf_active_conv')")

    page.locator(".side-new-btn").click()
    second_id = None
    for _ in range(40):
        second_id = page.evaluate("window.sessionStorage.getItem('cf_active_conv')")
        if second_id and second_id != first_id:
            break
        page.wait_for_timeout(250)

    assert second_id and second_id != first_id
    assert page.locator("#streamInner .msg").count() == 0


def test_command_palette_contains_role_commands(buyer_page: Page):
    page = buyer_page
    page.locator("#topCommands").click()

    expect(page.locator("#commandPalette")).to_be_visible()
    expect(page.locator("#commandPaletteList .command-item")).to_have_count(7)
    expect(page.locator("#commandPaletteList")).to_contain_text("Мои сделки")
    expect(page.locator("#commandPaletteList")).to_contain_text("Заявки")
    expect(page.locator("#commandPaletteList")).to_contain_text("Поддержка")


def test_input_command_button_keeps_current_conversation(buyer_page: Page):
    page = buyer_page
    page.locator(".pill", has_text="Мои сделки").first.click()
    page.wait_for_selector(".msg-assistant", timeout=15000)
    conv_id = page.evaluate("window.sessionStorage.getItem('cf_active_conv')")
    message_count = page.locator(".msg-assistant").count()

    page.locator("#input").fill("/")
    expect(page.locator("#commandPalette")).to_be_hidden()
    expect(page.locator("#input")).to_have_value("/")
    page.locator("#input").fill("")

    page.locator("#inputCommands").click()
    expect(page.locator("#commandPalette")).to_be_visible()
    expect(page.locator("#commandPaletteSearch")).to_be_focused()
    page.locator("#commandPaletteSearch").fill("заяв")
    expect(page.locator("#commandPaletteList .command-item")).to_have_count(1)
    page.locator("#commandPaletteList .command-item").click()

    for _ in range(60):
        if page.locator(".msg-assistant").count() > message_count:
            break
        page.wait_for_timeout(250)

    assert page.evaluate("window.sessionStorage.getItem('cf_active_conv')") == conv_id
    assert page.locator(".msg-assistant").count() > message_count


def test_direct_action_link_starts_after_initialization(page: Page, base_url: str):
    page.goto(
        f"{base_url}/demo-login/?role=buyer",
        wait_until="domcontentloaded",
    )
    page.goto(
        f"{base_url}/chat/?new=1&run=get_orders",
        wait_until="domcontentloaded",
    )

    page.wait_for_selector(".msg-assistant", timeout=15000)
    conv_id = page.evaluate("window.sessionStorage.getItem('cf_active_conv')")

    assert conv_id
    assert page.url.rstrip("/") == f"{base_url}/chat"
    expect(page.locator("#convStage")).to_be_visible()

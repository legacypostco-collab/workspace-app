"""Browser checks for separate registration consents."""
from __future__ import annotations

from playwright.sync_api import Page, expect


def _next(page: Page) -> None:
    page.locator("[data-wizard-next]").click()


def test_buyer_registration_requires_two_separate_consents(
    page: Page,
    base_url: str,
):
    page.goto(f"{base_url}/chat/?workspace=1", wait_until="networkidle")
    banner = page.locator("#cookie-banner")
    if banner.is_visible():
        page.locator("[data-cookie-action='necessary']").click()

    page.wait_for_function(
        "() => window.authModal && typeof window.authModal.open === 'function'"
    )
    page.evaluate("window.authModal.open('start_registration', {role: 'buyer'})")
    expect(page.locator("#authModal")).to_be_visible()

    page.locator("input[name='tax_id']").fill("7708123456")
    _next(page)
    page.locator("input[name='contact_name']").fill("Тестовый пользователь")
    page.locator("input[name='email']").fill("consent-browser@example.test")
    page.locator("input[name='phone_e164']").fill("+79990000001")
    _next(page)
    page.locator("input[name='messenger_handle']").fill("@consent_browser")
    _next(page)
    page.locator("textarea[name='equipment_fleet']").fill("Тестовый парк")
    _next(page)

    consent_boxes = page.locator(".auth-consent .auth-checkbox")
    expect(consent_boxes).to_have_count(2)
    expect(page.locator("a[href='/terms/']", has_text="Открыть условия")).to_be_visible()
    expect(
        page.locator(
            "a[href='/personal-data-consent/']",
            has_text="Открыть текст согласия",
        )
    ).to_be_visible()

    page.locator("input[name='username']").fill("consent_browser")
    page.locator("input[name='password1']").fill("VeryStr0ngPass!42")
    page.locator("input[name='password2']").fill("VeryStr0ngPass!42")
    _next(page)
    expect(page.locator("#authModal")).to_be_visible()
    expect(page.locator(".auth-checkbox.is-error")).to_have_count(2)

    dialog_box = page.locator(".auth-dialog").bounding_box()
    assert dialog_box
    for consent in page.locator(".auth-consent").all():
        box = consent.bounding_box()
        assert box
        assert box["x"] >= dialog_box["x"]
        assert box["x"] + box["width"] <= dialog_box["x"] + dialog_box["width"] + 1

    page.locator("input[name='accept_terms']").check()
    page.locator("input[name='personal_data_consent']").check()
    for consent_box in consent_boxes.all():
        expect(consent_box).to_be_checked()

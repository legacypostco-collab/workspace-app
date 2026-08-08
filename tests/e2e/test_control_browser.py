"""Browser acceptance checks for the internal control workspace."""

from __future__ import annotations

import re

from playwright.sync_api import Page, expect

CONTROL_SECTIONS = (
    "/control/",
    "/control/search/",
    "/control/notifications/",
    "/control/finance/?status=paid",
    "/control/orders/",
    "/control/users/",
    "/control/moderation/",
    "/control/catalog/",
    "/control/support/",
    "/control/audit/",
    "/control/settings/",
)


def test_admin_control_sections_render_without_browser_errors(
    admin_page: Page,
    base_url: str,
):
    errors: list[str] = []
    failed_assets: list[str] = []
    admin_page.on("pageerror", lambda exc: errors.append(str(exc)))
    admin_page.on(
        "response",
        lambda response: failed_assets.append(response.url)
        if response.status >= 400 and "/static/" in response.url
        else None,
    )

    for path in CONTROL_SECTIONS:
        response = admin_page.goto(f"{base_url}{path}", wait_until="networkidle")
        assert response and response.status == 200, (path, response.status if response else None)
        expect(admin_page.locator(".control-shell")).to_be_visible()
        assert admin_page.url.startswith(f"{base_url}/control/"), admin_page.url

    assert not errors, errors
    assert not failed_assets, failed_assets


def test_finance_workspace_handles_paid_records_or_empty_state(
    admin_page: Page,
    base_url: str,
):
    response = admin_page.goto(
        f"{base_url}/control/finance/?status=paid",
        wait_until="networkidle",
    )
    assert response and response.status == 200

    invoice_link = admin_page.locator(
        ".control-table .table-primary[href^='/control/finance/']"
    ).first
    if invoice_link.count() == 0:
        expect(
            admin_page.locator(".control-empty", has_text="Счета не найдены")
        ).to_be_visible()
        return

    expect(invoice_link).to_be_visible()
    invoice_link.click()
    admin_page.wait_for_url(re.compile(r".*/control/finance/\d+/$"))
    expect(admin_page.locator("h2", has_text="Счёт и договор")).to_be_visible()
    expect(admin_page.locator("h2", has_text="Проводки")).to_be_visible()

    pdf_link = admin_page.locator(
        "a[href^='/api/assistant/orders/'][href$='/file/']",
        has_text="Скачать счёт",
    )
    expect(pdf_link).to_be_visible()
    pdf_response = admin_page.context.request.get(f"{base_url}{pdf_link.get_attribute('href')}")
    assert pdf_response.status == 200
    assert pdf_response.body().startswith(b"%PDF")

    order_link = admin_page.locator(
        ".data-grid a[href^='/control/orders/']"
    ).first
    expect(order_link).to_be_visible()
    order_link.click()
    admin_page.wait_for_url(re.compile(r".*/control/orders/\d+/$"))
    expect(admin_page.locator("#items")).to_be_visible()
    expect(admin_page.locator("#settlements")).to_be_visible()
    expect(admin_page.locator("#documents")).to_be_visible()
    expect(admin_page.locator("#events")).to_be_visible()
    assert admin_page.locator(
        "#settlements a[href^='/control/finance/']"
    ).count() >= 1


def test_buyer_cannot_open_control_workspace(
    page: Page,
    base_url: str,
    login_as,
):
    login_as(page, "buyer")
    response = page.goto(f"{base_url}/control/", wait_until="domcontentloaded")
    assert response and response.status == 403
    assert page.locator(".control-shell").count() == 0

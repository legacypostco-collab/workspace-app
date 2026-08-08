"""E2E: публичные маршруты и ссылки не должны вести на битые страницы."""
from __future__ import annotations

from urllib.parse import urljoin, urlparse

from playwright.sync_api import Page, expect

PUBLIC_PAGES = (
    "/",
    "/landing/",
    "/terms/",
    "/privacy/",
    "/cookies/",
    "/personal-data-consent/",
    "/help/",
    "/faq/",
    "/chat/",
    "/chat/?workspace=1",
)

REDIRECTS = {
    "/favicon.ico": "/static/brand/logo-icon-orange.svg",
    "/demo-center/": "/chat/?workspace=1",
    "/demo/": "/chat/?workspace=1",
    "/register/": "/chat/?action=start_registration",
    "/login/": "/chat/?action=start_login",
    "/catalog/": "/chat/?new=1&run=search_parts",
    "/directory/brands/": "/chat/?new=1&run=browse_brands",
    "/directory/suppliers/": "/chat/?new=1&run=top_suppliers",
    "/directory/categories/": "/chat/?new=1&run=browse_categories",
    "/rfq/": "/chat/?new=1&run=get_rfq_status",
    "/rfq/new/": "/chat/?new=1&run=create_rfq",
    "/compare/": "/chat/?new=1&run=compare_products",
    "/cart/": "/chat/?new=1&run=get_my_deals",
    "/checkout/": "/chat/?new=1&run=get_my_deals",
    "/notifications/": "/chat/?new=1&run=notifications",
    "/kyb/": "/chat/?new=1&run=kyb_status",
    "/2fa/": "/chat/?new=1&run=setup_2fa",
}

REMOVED_PORTALS = (
    "/buyer/",
    "/seller/",
    "/operator/",
    "/admin-panel/",
)


def test_public_route_contract(page: Page, base_url: str):
    for path in PUBLIC_PAGES:
        response = page.request.get(f"{base_url}{path}", max_redirects=0)
        assert response.status == 200, f"{path} returned HTTP {response.status}"

    for path, target in REDIRECTS.items():
        response = page.request.get(f"{base_url}{path}", max_redirects=0)
        assert response.status in (301, 302), (
            f"{path} returned HTTP {response.status} instead of a redirect"
        )
        location = response.headers.get("location", "")
        assert location == target, f"{path} redirects to {location!r}, expected {target!r}"

    for path in REMOVED_PORTALS:
        response = page.request.get(f"{base_url}{path}", max_redirects=0)
        assert response.status == 410, f"{path} returned HTTP {response.status}"


def test_public_pages_have_no_broken_internal_links(page: Page, base_url: str):
    origin = urlparse(base_url)
    discovered: set[str] = set()

    for path in PUBLIC_PAGES:
        page.goto(f"{base_url}{path}", wait_until="domcontentloaded")
        hrefs = page.locator("a[href]").evaluate_all(
            "elements => elements.map(element => element.getAttribute('href'))"
        )
        for href in hrefs:
            if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
                continue
            absolute = urljoin(f"{base_url}{path}", href)
            parsed = urlparse(absolute)
            if (parsed.scheme, parsed.netloc) != (origin.scheme, origin.netloc):
                continue
            if parsed.path == "/logout/":
                continue
            discovered.add(absolute)

    assert discovered, "no internal links were discovered on public pages"
    failures: list[str] = []
    for url in sorted(discovered):
        response = page.request.get(url, max_redirects=5)
        if response.status >= 400:
            failures.append(f"{response.status} {url}")

    assert not failures, "broken public links:\n" + "\n".join(failures)


def test_legal_theme_is_shared_with_landing(page: Page, base_url: str):
    page.goto(f"{base_url}/privacy/", wait_until="domcontentloaded")
    page.evaluate("localStorage.setItem('cf_dark_mode', '0')")
    page.reload(wait_until="domcontentloaded")
    expect(page.locator("html")).not_to_have_class("dark-mode")

    page.locator(".legal-theme").click()
    expect(page.locator("html")).to_have_class("dark-mode")
    assert page.evaluate("localStorage.getItem('cf_dark_mode')") == "1"

    page.goto(f"{base_url}/landing/", wait_until="domcontentloaded")
    expect(page.locator("body")).to_have_class("dark-mode")

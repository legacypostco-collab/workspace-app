"""E2E checks for self-hosted browser dependencies."""
from __future__ import annotations

from playwright.sync_api import Page


FORBIDDEN_ASSET_HOSTS = (
    "unpkg.com",
    "cdnjs.cloudflare.com",
    "cdn.jsdelivr.net",
    "fonts.googleapis.com",
    "fonts.gstatic.com",
)


def _watch_asset_violations(page: Page) -> tuple[list[str], list[str]]:
    external: list[str] = []
    policy_errors: list[str] = []

    def on_request(request):
        if any(host in request.url for host in FORBIDDEN_ASSET_HOSTS):
            external.append(request.url)

    def on_console(message):
        text = message.text
        if message.type == "error" and (
            "Content Security Policy" in text or "Refused to load" in text
        ):
            policy_errors.append(text)

    page.on("request", on_request)
    page.on("console", on_console)
    return external, policy_errors


def _assert_clean(external: list[str], policy_errors: list[str]) -> None:
    assert not external, f"page requested forbidden third-party assets: {external}"
    assert not policy_errors, f"page triggered CSP violations: {policy_errors}"


def test_legacy_role_routes_are_explicitly_removed(
    seller_page: Page,
    base_url: str,
):
    page = seller_page
    external, policy_errors = _watch_asset_violations(page)

    for path in ("/buyer/", "/seller/", "/seller/logistics/", "/seller/qr/"):
        response = page.goto(f"{base_url}{path}", wait_until="domcontentloaded")
        assert response and response.status == 410
        assert page.url.rstrip("/").endswith(path.rstrip("/"))

    _assert_clean(external, policy_errors)


def test_vendored_assets_are_served_locally(page: Page, base_url: str):
    paths = (
        "/static/css/fonts.css",
        "/static/vendor/fonts/inter/files/inter-cyrillic-wght-normal.woff2",
        "/static/vendor/react/18.3.1/umd/react.production.min.js",
        "/static/vendor/react-dom/18.3.1/umd/react-dom.production.min.js",
        "/static/vendor/babel/7.23.10/babel.min.js",
        "/static/vendor/leaflet/1.9.4/dist/leaflet.js",
        "/static/vendor/qrcodejs/1.0.0/qrcode.min.js",
    )
    for path in paths:
        response = page.request.get(f"{base_url}{path}")
        assert response.ok, f"{path} returned HTTP {response.status}"

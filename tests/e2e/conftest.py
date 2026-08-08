"""Playwright fixtures for chat-first E2E tests.

The suite targets a running Django server and is opt-in: use
``tests/e2e/run.sh`` or set ``E2E_RUN=1``. Authenticated scenarios use the
normal login action with credentials supplied through environment variables;
there is no privileged test-only login route.
"""
from __future__ import annotations

import json
import os
from urllib.parse import quote

import pytest

if os.getenv("E2E_RUN") != "1":
    pytest.skip(
        "browser E2E is opt-in; run tests/e2e/run.sh or set E2E_RUN=1",
        allow_module_level=True,
    )

playwright_api = pytest.importorskip(
    "playwright.sync_api",
    reason="install requirements-e2e.txt before running browser E2E",
)

Browser = playwright_api.Browser
BrowserContext = playwright_api.BrowserContext
Page = playwright_api.Page
sync_playwright = playwright_api.sync_playwright


BASE_URL = os.getenv("E2E_BASE_URL", "http://127.0.0.1:8003")
ROLE_ENV_PREFIX = {
    "buyer": "E2E_BUYER",
    "seller": "E2E_SELLER",
    "operator": "E2E_OPERATOR",
    "admin": "E2E_ADMIN",
}


@pytest.fixture(scope="session")
def base_url() -> str:
    return BASE_URL


@pytest.fixture(scope="session")
def playwright_instance():
    with sync_playwright() as playwright:
        yield playwright


@pytest.fixture(scope="session")
def browser(playwright_instance):
    browser = playwright_instance.chromium.launch(
        headless=os.getenv("E2E_HEADED") != "1",
        slow_mo=int(os.getenv("E2E_SLOW_MO", "0")),
    )
    yield browser
    browser.close()


@pytest.fixture
def context(browser: Browser) -> BrowserContext:
    ctx = browser.new_context(
        viewport={"width": 1280, "height": 800},
        ignore_https_errors=True,
    )
    consent = quote(json.dumps({
        "version": "2026-08-09",
        "necessary": True,
        "analytics": False,
        "decided_at": "e2e-fixture",
    }, separators=(",", ":")))
    ctx.add_cookies([{
        "name": "cookie_consent",
        "value": consent,
        "url": BASE_URL,
        "secure": BASE_URL.startswith("https://"),
        "sameSite": "Lax",
    }])
    yield ctx
    ctx.close()


@pytest.fixture
def page(context: BrowserContext) -> Page:
    p = context.new_page()
    yield p
    p.close()


# Auth helpers

def _role_credentials(role: str) -> tuple[str, str]:
    prefix = ROLE_ENV_PREFIX.get(role)
    if not prefix:
        raise AssertionError(f"unknown role {role}")
    username = os.getenv(f"{prefix}_USERNAME", "").strip()
    password = os.getenv(f"{prefix}_PASSWORD") or os.getenv("E2E_PASSWORD", "")
    if not username or not password:
        pytest.skip(
            f"{prefix}_USERNAME and {prefix}_PASSWORD (or E2E_PASSWORD) are required"
        )
    return username, password


def login_role(page: Page, role: str, base_url: str) -> None:
    """Authenticate through the same CSRF-protected action used by the UI."""
    username, password = _role_credentials(role)
    page.context.clear_cookies()
    page.goto(f"{base_url}/chat/", wait_until="domcontentloaded")
    cookie_banner = page.locator("#cookie-banner")
    if cookie_banner.is_visible():
        page.locator("[data-cookie-action='necessary']").click()
    result = page.evaluate(
        """async ({username, password, role}) => {
          const match = document.cookie.match(/(?:^|;\\s*)csrftoken=([^;]+)/);
          const csrf = match ? decodeURIComponent(match[1]) : "";
          const response = await fetch("/api/assistant/action/", {
            method: "POST",
            credentials: "same-origin",
            headers: {
              "Content-Type": "application/json",
              "X-CSRFToken": csrf,
            },
            body: JSON.stringify({
              action: "start_login",
              params: {confirmed: true, role, username, password},
            }),
          });
          let data = {};
          try { data = await response.json(); } catch (_) {}
          return {ok: response.ok, status: response.status, data};
        }""",
        {"username": username, "password": password, "role": role},
    )
    data = result.get("data") or {}
    assert result.get("ok"), (
        f"login for {role} failed with HTTP {result.get('status')}: "
        f"{data.get('error') or data.get('text') or 'unknown response'}"
    )
    assert data.get("_post_action") == "reload", (
        f"login for {role} was not completed: {data.get('text') or data}"
    )
    page.goto(f"{base_url}/chat/", wait_until="networkidle")


@pytest.fixture
def login_as(base_url: str):
    """Return a login helper that works with any Playwright page."""
    def _login(page: Page, role: str) -> None:
        login_role(page, role, base_url)

    return _login


def _enter_fresh_chat(page: Page, base_url: str) -> Page:
    """Зайти в /chat/ и сбросить активный conversation, чтобы welcome-stage был
    видим (там pills'ы для тестов)."""
    page.goto(f"{base_url}/chat/", wait_until="networkidle")
    # Welcome-stage всегда attached, но может быть hidden если есть существующие convs
    page.wait_for_selector("#welcomeStage", state="attached", timeout=10000)
    # Если скрыт — кликаем «+ Новый чат» в sidebar чтобы попасть в welcome
    welcome_class = page.locator("#welcomeStage").get_attribute("class") or ""
    if "hidden" in welcome_class:
        new_btn = page.locator(".side-new-btn")
        if new_btn.count() > 0:
            new_btn.first.click()
            try:
                page.wait_for_function(
                    "() => !document.getElementById('welcomeStage').classList.contains('hidden')",
                    timeout=5000,
                )
            except Exception:
                pass  # если не получилось — тесты сами разберутся
    return page


@pytest.fixture
def buyer_page(page: Page, base_url: str) -> Page:
    login_role(page, "buyer", base_url)
    return _enter_fresh_chat(page, base_url)


@pytest.fixture
def seller_page(page: Page, base_url: str) -> Page:
    login_role(page, "seller", base_url)
    return _enter_fresh_chat(page, base_url)


@pytest.fixture
def operator_page(page: Page, base_url: str) -> Page:
    login_role(page, "operator", base_url)
    return _enter_fresh_chat(page, base_url)


@pytest.fixture
def admin_page(page: Page, base_url: str) -> Page:
    login_role(page, "admin", base_url)
    return page

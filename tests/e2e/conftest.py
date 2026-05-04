"""Playwright fixtures for chat-first E2E tests.

Тесты бьют по running Django dev server (по умолчанию http://127.0.0.1:8003).
Запуск:
  # Терминал 1:
  python manage.py runserver 127.0.0.1:8003
  # Терминал 2:
  pytest tests/e2e/

Или через runner: bash tests/e2e/run.sh

Конвенции:
  • Все тесты используют demo-аккаунты (demo_buyer, demo_seller, demo_operator)
  • Аутентификация через GET /demo-login/?role=… (без UI)
  • После login сразу на /chat/
"""
from __future__ import annotations

import os

import pytest
from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright


BASE_URL = os.getenv("E2E_BASE_URL", "http://127.0.0.1:8003")


@pytest.fixture(scope="session")
def base_url() -> str:
    return BASE_URL


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as p:
        browser = p.chromium.launch(
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
    yield ctx
    ctx.close()


@pytest.fixture
def page(context: BrowserContext) -> Page:
    p = context.new_page()
    yield p
    p.close()


# ── Auth helpers ─────────────────────────────────────────────

def login_demo(page: Page, role: str, base_url: str) -> None:
    """Залогиниться как demo-юзер через /demo-login/?role=… ."""
    assert role in ("buyer", "seller", "operator"), f"unknown role {role}"
    page.goto(f"{base_url}/demo-login/?role={role}", wait_until="networkidle")


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
    login_demo(page, "buyer", base_url)
    return _enter_fresh_chat(page, base_url)


@pytest.fixture
def seller_page(page: Page, base_url: str) -> Page:
    login_demo(page, "seller", base_url)
    return _enter_fresh_chat(page, base_url)


@pytest.fixture
def operator_page(page: Page, base_url: str) -> Page:
    login_demo(page, "operator", base_url)
    return _enter_fresh_chat(page, base_url)

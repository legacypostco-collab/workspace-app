"""Cross-browser smoke-тест: chromium / firefox / webkit.

Минимальный набор: для каждого браузера и каждого ключевого URL —
проверяем 200 OK + что критичный DOM-элемент отрендерился + сохраняем
скриншот (для визуального ревью).

Запуск:
  bash tests/e2e/run.sh tests/e2e/test_cross_browser_smoke.py -v
  # или одним браузером:
  E2E_BROWSER=firefox pytest tests/e2e/test_cross_browser_smoke.py -v

Скриншоты пишутся в tests/e2e/screenshots/{browser}_{slug}.png — на CI
можно сравнивать через image-diff, локально просто посмотреть глазом.

Что покрывается:
  / (landing)        — заголовок + CTA «стать поставщиком»
  /chat/             — anon SPA: кнопки «Войти» / «Регистрация»
  /chat/ + login     — авторизация: уведомления и рабочие команды
  /parts/{slug}/     — карточка товара + Schema.org Product JSON-LD
"""
from __future__ import annotations

import os
import pathlib

import pytest

BASE_URL = os.getenv("E2E_BASE_URL", "http://127.0.0.1:8003")
HEADLESS = os.getenv("E2E_HEADED") != "1"
SCREENSHOT_DIR = pathlib.Path(__file__).parent / "screenshots"
SCREENSHOT_DIR.mkdir(exist_ok=True)

# E2E_BROWSER=chromium  → только один; пусто → все три
_ENV_BROWSER = os.getenv("E2E_BROWSER", "").strip().lower()
ALL_BROWSERS = ["chromium", "firefox", "webkit"]
BROWSERS = [_ENV_BROWSER] if _ENV_BROWSER in ALL_BROWSERS else ALL_BROWSERS


@pytest.fixture(scope="session", params=BROWSERS)
def xb_browser_name(request):
    """Параметризованная фикстура: один тест прогоняется N раз — по разу
    на каждый браузер из BROWSERS. Префикс xb_ чтобы не конфликтовать с
    общим conftest.py (где есть chromium-only browser/context/page)."""
    return request.param


@pytest.fixture
def xb_page(playwright_instance, xb_browser_name):
    """Свежий контекст под каждый тест × браузер.

    Webkit и Firefox требуют отдельной установки `playwright install <name>`.
    Если браузер не установлен — тест skip'нется с понятным сообщением.
    """
    launcher = getattr(playwright_instance, xb_browser_name)
    try:
        browser = launcher.launch(headless=HEADLESS)
    except Exception as e:
        pytest.skip(f"{xb_browser_name} не установлен: {e}. "
                     f"Запусти `playwright install {xb_browser_name}`.")
    ctx = browser.new_context(
        viewport={"width": 1440, "height": 900},
        locale="ru-RU",
    )
    page = ctx.new_page()
    yield page
    ctx.close()
    browser.close()


def _shot(page, browser_name: str, slug: str) -> None:
    """Сохранить скриншот в tests/e2e/screenshots/{browser}_{slug}.png."""
    path = SCREENSHOT_DIR / f"{browser_name}_{slug}.png"
    page.screenshot(path=str(path), full_page=True)


# ──────────────────────────────────────────────────────────────
# Тесты
# ──────────────────────────────────────────────────────────────

def test_landing_loads(xb_page, xb_browser_name):
    """/ → 200, есть нав «стать поставщиком», без JS-ошибок в консоли."""
    errors: list[str] = []
    xb_page.on("pageerror", lambda exc: errors.append(str(exc)))

    response = xb_page.goto(BASE_URL + "/", wait_until="domcontentloaded")
    assert response and response.ok, f"landing → HTTP {response.status if response else '?'}"

    # CTA на /register/?role=seller должен присутствовать
    cta = xb_page.locator('a[href*="role=seller"]')
    assert cta.count() > 0, "ссылка «стать поставщиком» не найдена на лендинге"

    _shot(xb_page, xb_browser_name, "landing")
    assert not errors, f"JS-ошибки на /: {errors}"


def test_chat_anonymous(xb_page, xb_browser_name):
    """/chat/ как гость → 200, видны кнопки start_login / start_registration."""
    errors: list[str] = []
    xb_page.on("pageerror", lambda exc: errors.append(str(exc)))

    response = xb_page.goto(BASE_URL + "/chat/", wait_until="domcontentloaded")
    assert response and response.ok, f"/chat/ → HTTP {response.status if response else '?'}"

    # Anonymous: кнопки «Войти» и «Регистрация» в топбаре
    login_btn = xb_page.locator('button:has-text("Войти")').first
    reg_btn = xb_page.locator('button:has-text("Регистрация")').first
    assert login_btn.is_visible(), "кнопка «Войти» не видна на /chat/ для anonymous"
    assert reg_btn.is_visible(), "кнопка «Регистрация» не видна на /chat/ для anonymous"

    _shot(xb_page, xb_browser_name, "chat_anon")
    assert not errors, f"JS-ошибки на /chat/ (anon): {errors}"


def test_chat_authenticated(xb_page, xb_browser_name, login_as):
    """Логин через штатное действие → авторизованный topbar без ошибок."""
    errors: list[str] = []
    xb_page.on("pageerror", lambda exc: errors.append(str(exc)))

    login_as(xb_page, "buyer")

    assert xb_page.evaluate("window.IS_AUTHENTICATED === true")
    assert xb_page.locator("#topBell").is_visible(timeout=5000)
    assert xb_page.locator('button:has-text("Войти")').count() == 0
    assert xb_page.locator('button:has-text("Регистрация")').count() == 0

    _shot(xb_page, xb_browser_name, "chat_auth")
    assert not errors, f"JS-ошибки на /chat/ (auth): {errors}"


def test_part_detail_with_schema(xb_page, xb_browser_name, login_as):
    """/parts/{slug}/ → JSON-LD Product присутствует в DOM."""
    login_as(xb_page, "buyer")

    # Достанем любой slug через DB? Нет — лучше через каталог.
    # Берём первый part из каталога:
    xb_page.goto(BASE_URL + "/catalog/", wait_until="domcontentloaded")
    first_part_link = xb_page.locator('a[href*="/parts/"]').first
    if first_part_link.count() == 0:
        pytest.skip("в каталоге нет ни одной part — пропускаем")
    href = first_part_link.get_attribute("href")
    assert href and "/parts/" in href

    response = xb_page.goto(BASE_URL + href, wait_until="domcontentloaded")
    assert response and response.ok, f"part_detail → HTTP {response.status if response else '?'}"

    # Schema.org Product JSON-LD должен присутствовать
    jsonld = xb_page.locator('script[type="application/ld+json"]')
    assert jsonld.count() > 0, "JSON-LD не найден на /parts/{slug}/"
    content = jsonld.first.inner_text()
    assert '"@type": "Product"' in content, f"JSON-LD без @type Product: {content[:200]}"

    _shot(xb_page, xb_browser_name, "part_detail")


def test_no_horizontal_scroll_375px(xb_page, xb_browser_name):
    """На 375px viewport не должно быть горизонтального скролла."""
    xb_page.set_viewport_size({"width": 375, "height": 667})
    xb_page.goto(BASE_URL + "/chat/", wait_until="domcontentloaded")

    # documentElement.scrollWidth не должен превышать clientWidth
    overflow = xb_page.evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    assert overflow <= 0, (
        f"горизонтальный overflow на 375px: scrollWidth-clientWidth={overflow}px"
    )

    _shot(xb_page, xb_browser_name, "chat_375")


def test_seo_files(xb_page, xb_browser_name):
    """/sitemap.xml + /robots.txt → 200 + содержат ожидаемый контент."""
    r = xb_page.request.get(BASE_URL + "/robots.txt")
    assert r.ok, f"/robots.txt → HTTP {r.status}"
    body = r.text()
    assert "User-agent:" in body and "Sitemap:" in body

    r = xb_page.request.get(BASE_URL + "/sitemap.xml")
    assert r.ok, f"/sitemap.xml → HTTP {r.status}"
    assert "<urlset" in r.text() and "<loc>" in r.text()

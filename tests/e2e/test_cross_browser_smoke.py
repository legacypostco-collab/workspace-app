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
  старые каталог/товар — переводят в единое рабочее пространство
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
    visible_text = xb_page.locator("body").inner_text()
    assert not any(term in visible_text for term in ("AUTO", "SEMI", "MANUAL")), (
        "в интерфейсе видны внутренние названия режима заявки"
    )

    _shot(xb_page, xb_browser_name, "chat_auth")
    assert not errors, f"JS-ошибки на /chat/ (auth): {errors}"


@pytest.mark.parametrize("legacy_path", ["/catalog/", "/parts/legacy-test-part/"])
def test_legacy_catalog_routes_to_workspace(xb_page, legacy_path):
    """Старые ссылки сохраняют поисковое действие и не открывают прежний интерфейс."""
    response = xb_page.goto(BASE_URL + legacy_path, wait_until="domcontentloaded")

    assert response and response.ok, (
        f"{legacy_path} → HTTP {response.status if response else '?'}"
    )
    assert xb_page.url.split("?", 1)[0].endswith("/chat/"), (
        f"{legacy_path} не перевёл пользователя в /chat/: {xb_page.url}"
    )
    assert "run=search_parts" in xb_page.url
    assert xb_page.locator(".marketplace-layout").count() == 0


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


@pytest.mark.parametrize("width,height", [(320, 568), (375, 667)])
def test_mobile_header_controls_fit_viewport(xb_page, width, height):
    """Гостевые действия помещаются в шапку и не сталкиваются с логотипом."""
    xb_page.set_viewport_size({"width": width, "height": height})
    xb_page.goto(BASE_URL + "/chat/", wait_until="domcontentloaded")

    metrics = xb_page.evaluate(
        """() => {
          const box = (selector) => {
            const rect = document.querySelector(selector).getBoundingClientRect();
            return {left: rect.left, right: rect.right};
          };
          return {
            brand: box(".top-brand"),
            actions: box(".top-right"),
            registration: box("button[data-ui-name='start_registration']"),
            welcomeLogoDisplay: getComputedStyle(
              document.querySelector(".welcome-logo")
            ).display,
          };
        }"""
    )

    assert metrics["brand"]["right"] <= metrics["actions"]["left"]
    assert metrics["registration"]["left"] >= 0
    assert metrics["registration"]["right"] <= width
    assert metrics["welcomeLogoDisplay"] == "none"


def test_mobile_message_uses_full_available_width(xb_page):
    """Скрытый мобильный аватар не должен оставлять пустую колонку."""
    xb_page.set_viewport_size({"width": 375, "height": 667})
    xb_page.goto(BASE_URL + "/chat/", wait_until="domcontentloaded")

    metrics = xb_page.evaluate(
        """() => {
          const row = document.createElement("div");
          row.className = "msg";
          row.style.width = "347px";
          row.innerHTML = `
            <div class="msg-avatar"></div>
            <div class="msg-body"><div class="msg-content">Проверка</div></div>
          `;
          document.body.appendChild(row);
          const body = row.querySelector(".msg-body");
          const avatar = row.querySelector(".msg-avatar");
          const result = {
            rowWidth: row.getBoundingClientRect().width,
            bodyWidth: body.getBoundingClientRect().width,
            bodyOffset: body.getBoundingClientRect().left
              - row.getBoundingClientRect().left,
            avatarDisplay: getComputedStyle(avatar).display,
          };
          row.remove();
          return result;
        }"""
    )

    assert metrics["avatarDisplay"] == "none"
    assert metrics["bodyOffset"] == 0
    assert metrics["bodyWidth"] == metrics["rowWidth"]


def test_seo_files(xb_page, xb_browser_name):
    """/sitemap.xml + /robots.txt → 200 + содержат ожидаемый контент."""
    r = xb_page.request.get(BASE_URL + "/robots.txt")
    assert r.ok, f"/robots.txt → HTTP {r.status}"
    body = r.text()
    assert "User-agent:" in body and "Sitemap:" in body

    r = xb_page.request.get(BASE_URL + "/sitemap.xml")
    assert r.ok, f"/sitemap.xml → HTTP {r.status}"
    assert "<urlset" in r.text() and "<loc>" in r.text()

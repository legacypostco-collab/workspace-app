"""axe-playwright accessibility scan.

Прогоняет axe-core по ключевым публичным страницам. Фейлит при наличии
violations с impact=critical (WCAG AA / Section 508 нарушения).
Serious/moderate/minor — собираются в отчёт но не блокируют.

Зависимости (только для CI):
    pip install axe-playwright-python

Запуск:
    E2E_BASE_URL=http://127.0.0.1:8003 pytest tests/e2e/test_axe_a11y.py -v

JSON-отчёты пишутся в tests/e2e/axe-results/ — артефакт в GitHub Actions.
"""
from __future__ import annotations

import json
import os
import pathlib

import pytest
from playwright.sync_api import sync_playwright

BASE_URL = os.getenv("E2E_BASE_URL", "http://127.0.0.1:8003")
RESULTS_DIR = pathlib.Path(__file__).parent / "axe-results"
RESULTS_DIR.mkdir(exist_ok=True)

# Страницы для скана: (url, slug_для_отчёта, требует_login?)
PAGES = [
    ("/",      "landing", False),
    ("/chat/", "chat",    False),
    # Авторизованные — нужны demo-аккаунты (создаются в CI до запуска)
    ("/chat/", "chat_buyer",    "demo_buyer"),
    ("/chat/", "chat_operator", "demo_operator"),
]


@pytest.fixture(scope="session")
def axe_runner():
    """Возвращает (Page, run_axe_fn). axe-playwright-python инжектит
    axe-core script и возвращает violations."""
    try:
        from axe_playwright_python.sync_playwright import Axe  # type: ignore
    except ImportError:
        pytest.skip("axe-playwright-python не установлен (`pip install axe-playwright-python`)")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            viewport={"width": 1440, "height": 900}, locale="ru-RU",
        )
        page = ctx.new_page()
        axe = Axe()
        yield page, axe
        ctx.close()
        browser.close()


def _login_demo(page, role: str) -> None:
    page.goto(f"{BASE_URL}/demo-login/?role={role.split('_')[1]}",
               wait_until="domcontentloaded")


@pytest.mark.parametrize("url,slug,login_as", PAGES,
                          ids=[f"{slug}" for _, slug, _ in PAGES])
def test_axe_no_critical(axe_runner, url, slug, login_as):
    """Страница не должна иметь impact=critical нарушений."""
    page, axe = axe_runner
    if login_as:
        _login_demo(page, login_as)
    page.goto(f"{BASE_URL}{url}", wait_until="networkidle")
    # Даём шанс ленивым картинкам/шрифтам загрузиться
    page.wait_for_timeout(500)

    results = axe.run(page)
    # axe_playwright_python возвращает AxeResults с .violations list
    violations = results.response.get("violations", []) if hasattr(results, "response") else results.get("violations", [])

    # Сохраняем полный отчёт для артефакта
    out = RESULTS_DIR / f"{slug}.json"
    out.write_text(json.dumps({
        "url": url, "login_as": login_as,
        "violations": violations,
        "passes": len(results.response.get("passes", [])) if hasattr(results, "response") else 0,
    }, indent=2, ensure_ascii=False))

    critical = [v for v in violations if v.get("impact") == "critical"]
    serious = [v for v in violations if v.get("impact") == "serious"]
    print(f"\n  [{slug}] {len(critical)} critical · {len(serious)} serious · "
          f"{len(violations)} total")
    for v in critical[:5]:
        print(f"    🔴 {v.get('id')}: {v.get('help','')[:80]}")
        for n in v.get("nodes", [])[:2]:
            print(f"        → {n.get('target', ['?'])}")

    assert not critical, (
        f"axe нашёл {len(critical)} critical violations на {slug}. "
        f"Отчёт: {out}. Топ: " + ", ".join(v.get("id", "?") for v in critical)
    )

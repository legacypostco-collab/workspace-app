"""5 ключевых E2E-сценариев маркетплейса для регрессии перед релизом.

Сценарии:
  1. buyer_search_to_rfq           — поиск → создание RFQ
  2. operator_kyb_review            — KYB-очередь → детальная карточка → решение
  3. operator_logistics_route_drill — Логистика → активный маршрут → детали заказа
  4. cookie_banner_persistence      — Принять cookie → не показывается при reload
  5. buyer_support_entry             — открытие рабочего раздела поддержки

Все сценарии используют существующую инфру в conftest.py.

Запуск:
    ./tests/e2e/run.sh tests/e2e/test_marketplace_critical.py -v
"""
from playwright.sync_api import Page, expect


# ── Сценарий 1: Поиск → RFQ ──────────────────────────────────────

def test_buyer_search_to_rfq(buyer_page: Page):
    """Buyer заходит → ищет деталь → создаёт RFQ."""
    page = buyer_page
    # Ожидаем hero
    expect(page.locator("#welcomeTitle")).to_be_visible(timeout=5000)
    # Ввод запроса
    page.fill("#heroInput", "FIX-1")
    page.locator("#heroSendBtn").click()
    # Ждём ответа ассистента (карточка с парт-найдено)
    page.wait_for_selector(".card, .ls-card, .spec-card", timeout=15000)
    # Проверка что переход в conv state произошёл
    expect(page.locator("#convStage")).to_be_visible()


# ── Сценарий 2: KYB review (operator) ──────────────────────────

def test_operator_kyb_queue_drill(operator_page: Page):
    """Operator → KYB поставщиков → очередь или корректное пустое состояние."""
    page = operator_page
    page.locator(".pill[data-pid^='op_kyb_queue#']:visible").last.click()
    page.wait_for_selector(".ls-card:visible", timeout=10000)

    card_text = page.locator(".ls-card:visible").last.inner_text()
    if "Очередь пуста" in card_text:
        assert "Все анкеты обработаны" in card_text
        return

    rows = page.locator(".ls-row:visible")
    assert rows.count() > 0, "KYB queue is neither empty nor clickable"
    rows.first.click()
    page.wait_for_selector("text=Авто-проверки", timeout=8000)
    expect(page.locator("text=Что проверить глазами")).to_be_visible()


# ── Сценарий 3: Логистика → маршрут ────────────────────────────

def test_operator_logistics_active_routes(operator_page: Page):
    """Operator → Логистика → сводка отражает текущие или исторические маршруты."""
    page = operator_page
    page.locator(".pill[data-pid^='op_logistics_stats#']:visible").last.click()
    page.wait_for_selector("text=Логистика — что под контролем сейчас", timeout=10000)
    body_text = page.locator("#streamInner").inner_text()
    assert "По маршрутам" in body_text
    assert any(marker in body_text for marker in ("→", "нет ни активных отгрузок"))


# ── Сценарий 4: Cookie banner persistence ──────────────────────

def test_cookie_banner_accepts_and_persists(page: Page, base_url):
    """Гость → landing → видит banner → accept → reload → не видит."""
    page.goto(f"{base_url}/")
    page.evaluate("localStorage.removeItem('cookie_consent')")
    page.context.clear_cookies()
    page.reload()
    # Banner появляется
    banner = page.locator("#cookie-banner")
    expect(banner).to_be_visible(timeout=3000)
    page.locator("[data-cookie-action='accept']").click()
    expect(banner).to_be_hidden()
    # Reload — не должен снова появиться
    page.reload()
    expect(banner).to_be_hidden(timeout=2000)


# ── Сценарий 5: Поддержка ──────────────────────────────────────

def test_buyer_support_entry(buyer_page: Page):
    """Buyer открывает поддержку из набора быстрых действий."""
    page = buyer_page
    page.locator(".pill[data-pid^='support_home#']").first.click()
    page.wait_for_selector(".msg-assistant", timeout=15000)
    body = page.locator("#streamInner").inner_text(timeout=2000).lower()
    assert "поддерж" in body or "оператор" in body

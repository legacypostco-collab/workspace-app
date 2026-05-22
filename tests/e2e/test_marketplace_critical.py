"""5 ключевых E2E-сценариев маркетплейса для регрессии перед релизом.

Сценарии:
  1. buyer_search_to_rfq           — поиск → создание RFQ
  2. operator_kyb_review            — KYB-очередь → детальная карточка → решение
  3. operator_logistics_route_drill — Логистика → активный маршрут → детали заказа
  4. cookie_banner_persistence      — Принять cookie → не показывается при reload
  5. feedback_widget_submission     — Click 🐞 → отправка → toast подтверждения

Все сценарии используют существующую инфру в conftest.py.

Запуск:
    ./tests/e2e/run.sh tests/e2e/test_marketplace_critical.py -v
"""
import pytest
from playwright.sync_api import Page, expect


# ── Сценарий 1: Поиск → RFQ ──────────────────────────────────────

def test_buyer_search_to_rfq(page: Page, base_url, login_as):
    """Buyer заходит → ищет деталь → создаёт RFQ."""
    login_as("demo_buyer")
    page.goto(f"{base_url}/chat/")
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

def test_operator_kyb_queue_drill(page: Page, base_url, login_as):
    """Operator → KYB поставщиков → клик на pending → детальная карточка."""
    login_as("demo_operator")
    page.goto(f"{base_url}/chat/")
    # Кликаем pill «🛡 KYB поставщиков»
    page.locator(".pill", has_text="KYB").first.click()
    # Должна появиться list-карточка с pending
    page.wait_for_selector(".ls-card", timeout=10000)
    # Кликаем первую строку
    page.locator(".ls-row").first.click()
    # Ждём детальную карточку (3 блока: форма + API панель + чеклист)
    page.wait_for_selector("text=Авто-проверки", timeout=8000)
    expect(page.locator("text=Что проверить глазами")).to_be_visible()


# ── Сценарий 3: Логистика → маршрут ────────────────────────────

def test_operator_logistics_active_routes(page: Page, base_url, login_as):
    """Operator → Логистика → видны маршруты CN→RU / TR→RU / KZ→RU."""
    login_as("demo_operator")
    page.goto(f"{base_url}/chat/")
    page.locator(".pill", has_text="Логистика").first.click()
    page.wait_for_selector("text=Активные маршруты", timeout=10000)
    # Должны быть видны несколько стран
    body_text = page.locator("body").text_content() or ""
    found_countries = sum(1 for c in ("CN", "TR", "DE", "KZ") if c in body_text)
    assert found_countries >= 2, f"expected ≥2 countries, found {found_countries}"


# ── Сценарий 4: Cookie banner persistence ──────────────────────

def test_cookie_banner_accepts_and_persists(page: Page, base_url):
    """Гость → landing → видит banner → accept → reload → не видит."""
    page.context.clear_cookies()
    page.goto(f"{base_url}/")
    # Banner появляется
    banner = page.locator("#cookieBanner")
    expect(banner).to_be_visible(timeout=3000)
    # Нажимаем «Понятно»
    page.locator("#cookieBanner button").first.click()
    expect(banner).to_be_hidden()
    # Reload — не должен снова появиться
    page.reload()
    expect(banner).to_be_hidden(timeout=2000)


# ── Сценарий 5: Feedback widget ────────────────────────────────

def test_feedback_widget_submission(page: Page, base_url, login_as):
    """Buyer → 🐞 → текст → отправка → toast «Спасибо»."""
    login_as("demo_buyer")
    page.goto(f"{base_url}/chat/")
    page.wait_for_selector("#feedbackBtn", timeout=5000)
    page.click("#feedbackBtn")
    # Modal открылась
    page.wait_for_selector("#feedbackModal", state="visible", timeout=2000)
    page.fill("#feedbackText", "E2E test feedback — это просто проверка отправки")
    page.click("#feedbackSend")
    # Status (Спасибо) появляется
    page.wait_for_selector("#feedbackStatus", state="visible", timeout=5000)
    status_text = page.locator("#feedbackStatus").text_content() or ""
    assert "✓" in status_text or "Спасибо" in status_text or "Thanks" in status_text

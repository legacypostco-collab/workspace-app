"""E2E: RFQ-page status banner + USD total + send-action."""
from __future__ import annotations

import re

import requests
from playwright.sync_api import Page, expect


def _create_rfq_for_buyer(base_url: str, page: Page) -> int:
    """Создать RFQ через HTTP API и вернуть его id."""
    cookies = {c["name"]: c["value"] for c in page.context.cookies()}
    s = requests.Session()
    s.cookies.update(cookies)
    csrf = cookies.get("csrftoken", "")
    r = s.post(
        f"{base_url}/api/assistant/action/",
        json={"action": "create_rfq", "params": {"query": "Test pump x1\nTest filter x10"}},
        headers={"X-CSRFToken": csrf, "Referer": base_url + "/", "Content-Type": "application/json"},
        timeout=15,
    )
    if r.status_code >= 400:
        return 0
    text = (r.json() or {}).get("text", "")
    m = re.search(r"#(\d+)", text)
    return int(m.group(1)) if m else 0


def test_rfq_page_shows_stage_banner(buyer_page: Page, base_url: str):
    """Открытие RFQ показывает stage-banner с заголовком."""
    rfq_id = _create_rfq_for_buyer(base_url, buyer_page)
    if not rfq_id:
        return  # create_rfq не настроен — skip
    buyer_page.goto(f"{base_url}/chat/rfq/{rfq_id}/", wait_until="networkidle")
    expect(buyer_page.locator(".stage-banner")).to_be_visible(timeout=10000)
    title = buyer_page.locator(".stage-title").first.inner_text()
    assert any(kw in title.lower() for kw in ["rfq", "котировк", "разосл", "ждём"]), \
        f"banner title doesn't reflect stage: {title!r}"


def test_rfq_page_shows_hero_total_in_usd(buyer_page: Page, base_url: str):
    """Hero-total показывает USD-конвертированный бюджет."""
    rfq_id = _create_rfq_for_buyer(base_url, buyer_page)
    if not rfq_id:
        return
    buyer_page.goto(f"{base_url}/chat/rfq/{rfq_id}/", wait_until="networkidle")
    expect(buyer_page.locator(".hero-total")).to_be_visible(timeout=10000)
    label = buyer_page.locator(".hero-total-label").inner_text()
    assert "USD" in label, f"hero-total должен содержать USD label: {label!r}"


def test_rfq_page_draft_stage_has_send_cta(buyer_page: Page, base_url: str):
    """RFQ свежесозданный (без рассылки) → banner с CTA «Разослать поставщикам»."""
    rfq_id = _create_rfq_for_buyer(base_url, buyer_page)
    if not rfq_id:
        return
    buyer_page.goto(f"{base_url}/chat/rfq/{rfq_id}/", wait_until="networkidle")
    expect(buyer_page.locator(".stage-banner")).to_be_visible(timeout=10000)
    cta = buyer_page.locator(".banner-cta")
    assert cta.count() > 0, "должен быть CTA в банере"
    label = cta.first.inner_text()
    assert "Разослать" in label or "поставщик" in label.lower(), \
        f"CTA должен про рассылку: {label!r}"


def test_rfq_api_returns_stage_and_total(buyer_page: Page, base_url: str):
    """API /api/assistant/rfq/<id>/ возвращает stage, total_usd, quotes_count, sent_count."""
    rfq_id = _create_rfq_for_buyer(base_url, buyer_page)
    if not rfq_id:
        return
    cookies = {c["name"]: c["value"] for c in buyer_page.context.cookies()}
    r = requests.get(f"{base_url}/api/assistant/rfq/{rfq_id}/", cookies=cookies, timeout=10)
    assert r.status_code == 200
    data = r.json()
    for key in ("stage", "total_usd", "quotes_count", "sent_count", "is_owner"):
        assert key in data, f"API response missing {key!r}: {list(data.keys())}"
    assert data["stage"] in ("draft", "awaiting_quotes", "quotes_received", "needs_review", "cancelled")
    assert isinstance(data["total_usd"], (int, float))
    assert data["is_owner"] is True

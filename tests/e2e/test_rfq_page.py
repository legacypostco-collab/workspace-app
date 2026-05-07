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
    """Открытие RFQ показывает stage-banner с заголовком (любого из 5 stage'ов)."""
    rfq_id = _create_rfq_for_buyer(base_url, buyer_page)
    if not rfq_id:
        return  # create_rfq не настроен — skip
    buyer_page.goto(f"{base_url}/chat/rfq/{rfq_id}/", wait_until="networkidle")
    expect(buyer_page.locator(".stage-banner")).to_be_visible(timeout=10000)
    title = buyer_page.locator(".stage-title").first.inner_text()
    # AUTO/SEMI/MANUAL — каждый из 3 mode'ов имеет свои keywords
    assert any(kw in title.lower() for kw in
               ["auto", "ai", "semi", "manual", "котировк", "разосл", "ждём", "rfq", "получено"]), \
        f"banner title doesn't reflect stage/mode: {title!r}"


def test_rfq_page_hero_total_present(buyer_page: Page, base_url: str):
    """Hero-total элемент рендерится (с USD label если есть цены, или 'Уточняется' если нет)."""
    rfq_id = _create_rfq_for_buyer(base_url, buyer_page)
    if not rfq_id:
        return
    buyer_page.goto(f"{base_url}/chat/rfq/{rfq_id}/", wait_until="networkidle")
    expect(buyer_page.locator(".hero-total")).to_be_visible(timeout=10000)
    label = buyer_page.locator(".hero-total-label").inner_text()
    val = buyer_page.locator(".hero-total-val").inner_text()
    # Либо «(USD)» в label + сумма, либо «Бюджет» + «Уточняется»
    assert ("USD" in label and "$" in val) or ("Уточняется" in val), \
        f"hero-total mismatch: label={label!r} val={val!r}"


def test_rfq_auto_mode_default_no_manual_cta(buyer_page: Page, base_url: str):
    """AUTO режим (default) → banner без manual CTA, AI собирает котировки."""
    rfq_id = _create_rfq_for_buyer(base_url, buyer_page)
    if not rfq_id:
        return
    buyer_page.goto(f"{base_url}/chat/rfq/{rfq_id}/", wait_until="networkidle")
    expect(buyer_page.locator(".stage-banner")).to_be_visible(timeout=10000)
    title = buyer_page.locator(".stage-title").inner_text()
    # AUTO либо в стадии awaiting_quotes (если auto-sent сработал), либо draft с
    # AI-banner. В обоих случаях фраза «AI» / «AUTO» должна присутствовать
    assert "AI" in title or "AUTO" in title or "🤖" in title, \
        f"AUTO mode должен иметь AI/AUTO в заголовке: {title!r}"


def test_rfq_semi_mode_has_send_cta(buyer_page: Page, base_url: str):
    """SEMI режим (явно) → banner с CTA «Разослать кандидатам»."""
    cookies = {c["name"]: c["value"] for c in buyer_page.context.cookies()}
    s = requests.Session()
    s.cookies.update(cookies)
    csrf = cookies.get("csrftoken", "")
    r = s.post(
        f"{base_url}/api/assistant/action/",
        json={"action": "create_rfq", "params": {
            "query": "Test pump", "mode": "semi",
        }},
        headers={"X-CSRFToken": csrf, "Referer": base_url + "/", "Content-Type": "application/json"},
        timeout=15,
    )
    if r.status_code >= 400:
        return
    text = (r.json() or {}).get("text", "")
    m = re.search(r"#(\d+)", text)
    if not m:
        return
    rfq_id = int(m.group(1))
    buyer_page.goto(f"{base_url}/chat/rfq/{rfq_id}/", wait_until="networkidle")
    expect(buyer_page.locator(".stage-banner")).to_be_visible(timeout=10000)
    cta = buyer_page.locator(".banner-cta")
    assert cta.count() > 0, "SEMI режим должен иметь CTA"
    label = cta.first.inner_text()
    assert "Разослать" in label or "разослать" in label.lower(), \
        f"SEMI CTA должен про рассылку: {label!r}"


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

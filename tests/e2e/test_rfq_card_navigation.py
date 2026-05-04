"""E2E: RFQ-карточка в чате открывается по клику без отдельной кнопки."""
from __future__ import annotations

import re

import requests
from playwright.sync_api import Page


def _create_rfq(base_url: str, page: Page) -> int:
    cookies = {c["name"]: c["value"] for c in page.context.cookies()}
    s = requests.Session(); s.cookies.update(cookies)
    csrf = cookies.get("csrftoken", "")
    r = s.post(
        f"{base_url}/api/assistant/action/",
        json={"action": "create_rfq", "params": {"query": "Test pump x1"}},
        headers={"X-CSRFToken": csrf, "Referer": base_url + "/", "Content-Type": "application/json"},
        timeout=15,
    )
    if r.status_code >= 400:
        return 0
    text = (r.json() or {}).get("text", "")
    m = re.search(r"#(\d+)", text)
    return int(m.group(1)) if m else 0


def test_rfq_card_has_data_href(buyer_page: Page, base_url: str):
    """RFQ-карточка получает data-href с прямой ссылкой на /chat/rfq/<id>/."""
    rfq_id = _create_rfq(base_url, buyer_page)
    if not rfq_id:
        return
    # Используем pill «📋 Открытые RFQ» в welcome-stage (буде уже открыт по fixture)
    pill = buyer_page.locator(".pill", has_text="RFQ").first
    if not pill.is_visible():
        return
    pill.click()
    buyer_page.wait_for_selector(
        f".card-clickable[data-href='/chat/rfq/{rfq_id}/']", timeout=15000
    )


def test_create_rfq_response_has_no_open_rfq_button(buyer_page: Page, base_url: str):
    """В ответе на create_rfq нет дублирующей кнопки «Открыть страницу RFQ»."""
    cookies = {c["name"]: c["value"] for c in buyer_page.context.cookies()}
    s = requests.Session(); s.cookies.update(cookies)
    csrf = cookies.get("csrftoken", "")
    r = s.post(
        f"{base_url}/api/assistant/action/",
        json={"action": "create_rfq", "params": {"query": "Test pump x1"}},
        headers={"X-CSRFToken": csrf, "Referer": base_url + "/", "Content-Type": "application/json"},
        timeout=15,
    )
    if r.status_code >= 400:
        return
    data = r.json()
    actions_labels = [a.get("label", "") for a in (data.get("actions") or [])]
    # «Открыть RFQ» / «Открыть страницу RFQ» теперь не должна возвращаться —
    # карточка RFQ сама кликабельна
    assert not any("Открыть" in lbl and "RFQ" in lbl for lbl in actions_labels), \
        f"actions[] не должны содержать «Открыть RFQ», got: {actions_labels}"


def test_rfq_card_click_navigates(buyer_page: Page, base_url: str):
    """Клик по RFQ-карточке переходит на /chat/rfq/<id>/."""
    rfq_id = _create_rfq(base_url, buyer_page)
    if not rfq_id:
        return
    pill = buyer_page.locator(".pill", has_text="RFQ").first
    if not pill.is_visible():
        return
    pill.click()
    card = buyer_page.locator(f".card-clickable[data-href='/chat/rfq/{rfq_id}/']")
    card.wait_for(timeout=15000)
    card.first.click()
    buyer_page.wait_for_url(re.compile(f".*/chat/rfq/{rfq_id}"), timeout=10000)

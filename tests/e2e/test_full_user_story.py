"""Release-gated end-to-end marketplace user stories.

These tests intentionally use dedicated disposable accounts and real staging
data. They are opt-in because they create catalog rows, RFQs, quotes and orders.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import struct
import time
from collections import deque
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import Browser, Page

pytestmark = pytest.mark.skipif(
    os.getenv("E2E_USER_STORY") != "1",
    reason="set E2E_USER_STORY=1 for stateful marketplace acceptance tests",
)

PASSWORD = os.getenv("E2E_PASSWORD", "")
BUYER_A = os.getenv("E2E_STORY_BUYER_A", "itu_us_buyer_a")
BUYER_B = os.getenv("E2E_STORY_BUYER_B", "itu_us_buyer_b")
SELLER_A = os.getenv("E2E_STORY_SELLER_A", "itu_us_seller_a")
SELLER_B = os.getenv("E2E_STORY_SELLER_B", "itu_us_seller_b")
MULTI = os.getenv("E2E_STORY_MULTI", "itu_us_multi")
OPERATOR = os.getenv("E2E_STORY_OPERATOR", "itu_us_operator")
LOGIST = os.getenv("E2E_STORY_LOGIST", "itu_us_logist")
STORY_OEM = os.getenv("E2E_STORY_OEM", "7760-23-9880")
BUYER_OTP_SECRET = os.getenv("E2E_BUYER_OTP_SECRET", "")
BUYER_BACKUP_CODES = deque(
    code.strip()
    for code in os.getenv("E2E_BUYER_BACKUP_CODES", "").split(",")
    if code.strip()
)

DATA_DIR = Path(__file__).resolve().parents[2] / "Тестовые данные"
PRICE_A = DATA_DIR / "Прайс_поставщика_сводный.xlsx"
PRICE_B = DATA_DIR / "Прайс_поставщика_сводный_1.xlsx"


def _csrf(page: Page) -> str:
    for cookie in page.context.cookies():
        if cookie["name"] == "csrftoken":
            return cookie["value"]
    # Anonymous login/registration is accepted before SessionAuthentication
    # has an authenticated identity to protect. Django sets the cookie when
    # login rotates the token; every later mutating call must then carry it.
    return ""


def _json_fetch(
    page: Page,
    url: str,
    *,
    method: str = "GET",
    body: dict | None = None,
) -> dict:
    return page.evaluate(
        """async ({url, method, body, csrf}) => {
          const response = await fetch(url, {
            method,
            credentials: "same-origin",
            headers: body ? {
              "Content-Type": "application/json",
              "X-CSRFToken": csrf,
            } : {},
            body: body ? JSON.stringify(body) : undefined,
          });
          let data;
          try { data = await response.json(); }
          catch (_) { data = {raw: await response.text()}; }
          return {status: response.status, ok: response.ok, ...data};
        }""",
        {"url": url, "method": method, "body": body, "csrf": _csrf(page)},
    )


def _login(browser: Browser, base_url: str, username: str, role: str) -> Page:
    if not PASSWORD:
        raise AssertionError("E2E_PASSWORD is required")
    page = browser.new_page()
    page.goto(f"{base_url}/chat/", wait_until="domcontentloaded")
    result = _json_fetch(
        page,
        "/api/assistant/action/",
        method="POST",
        body={
            "action": "start_login",
            "params": {
                "confirmed": True,
                "username": username,
                "password": PASSWORD,
                "role": role,
            },
        },
    )
    if (
        result.get("_post_action") != "reload"
        and username == BUYER_A
        and (BUYER_BACKUP_CODES or BUYER_OTP_SECRET)
    ):
        otp_code = (
            BUYER_BACKUP_CODES.popleft()
            if BUYER_BACKUP_CODES
            else _totp(BUYER_OTP_SECRET)
        )
        result = _json_fetch(
            page,
            "/api/assistant/action/",
            method="POST",
            body={
                "action": "start_login",
                "params": {
                    "confirmed": True,
                    "two_factor": True,
                    "role": role,
                    "otp_code": otp_code,
                },
            },
        )
    assert result["status"] == 200, result
    assert result.get("_post_action") == "reload", result
    page.goto(f"{base_url}/chat/?workspace=1", wait_until="networkidle")
    config = _json_fetch(page, "/api/assistant/widget-config/")
    assert config["status"] == 200 and not config.get("anonymous"), config
    assert config["role"] == role, config
    return page


def _switch_role(page: Page, role: str) -> None:
    result = _json_fetch(
        page,
        "/api/assistant/role/",
        method="POST",
        body={"role": role},
    )
    assert result["status"] == 200, result
    page.reload(wait_until="networkidle")
    config = _json_fetch(page, "/api/assistant/widget-config/")
    assert config["role"] == role, config


def _action(
    page: Page,
    action: str,
    params: dict | None = None,
    conversation_id: str | None = None,
) -> dict:
    result = _json_fetch(
        page,
        "/api/assistant/action/",
        method="POST",
        body={
            "conversation_id": conversation_id,
            "action": action,
            "params": params or {},
        },
    )
    assert result["status"] == 200, result
    assert "error" not in result, result
    return result


def _card_rows(result: dict) -> list[dict]:
    rows = []
    for card in result.get("cards") or []:
        rows.extend((card.get("data") or {}).get("rows") or [])
    return rows


def _find_action_row(
    result: dict,
    action: str,
    *text_fragments: str,
) -> dict:
    for row in _card_rows(result):
        text = f"{row.get('title', '')} {row.get('subtitle', '')}".lower()
        if row.get("action") == action and all(
            fragment.lower() in text for fragment in text_fragments
        ):
            return row
    raise AssertionError(
        f"row action={action!r} fragments={text_fragments!r} not found: {result}"
    )


def _confirm_invoice_payment(
    page: Page,
    invoice_id: int,
    bank_reference: str,
) -> dict:
    preview = _action(
        page,
        "settlement_confirm_payment",
        {"invoice_id": invoice_id},
    )
    form = next(
        card for card in preview.get("cards") or []
        if card.get("type") == "form"
    )
    fields = (form.get("data") or {}).get("fields") or []
    amount = next(
        field.get("value")
        for field in fields
        if field.get("name") == "amount"
    )
    confirmed = _action(
        page,
        "settlement_confirm_payment",
        {
            "invoice_id": invoice_id,
            "amount": amount,
            "bank_reference": bank_reference,
            "confirmed": True,
        },
    )
    assert confirmed.get("action_succeeded") is True, confirmed
    return confirmed


def _assert_pdf_available(page: Page, url: str) -> None:
    origin = f"{urlsplit(page.url).scheme}://{urlsplit(page.url).netloc}"
    response = page.context.request.get(f"{origin}{url}")
    assert response.status == 200, (url, response.status)
    assert response.body().startswith(b"%PDF"), url


def _totp(secret: str, counter_offset: int = 0) -> str:
    normalized = secret.strip().replace(" ", "").upper()
    padding = "=" * ((8 - len(normalized) % 8) % 8)
    key = base64.b32decode(normalized + padding)
    counter = int(time.time()) // 30 + counter_offset
    digest = hmac.new(
        key,
        struct.pack(">Q", counter),
        hashlib.sha1,
    ).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{value % 1_000_000:06d}"


def _upload_order_evidence(
    page: Page,
    order_id: int,
    status: str,
    trigger_id: str,
) -> dict:
    origin = f"{urlsplit(page.url).scheme}://{urlsplit(page.url).netloc}"
    response = page.context.request.post(
        f"{origin}/api/assistant/orders/{order_id}/documents/",
        headers={
            "X-CSRFToken": _csrf(page),
            "Referer": f"{origin}/chat/",
        },
        multipart={
            "status": status,
            "trigger_id": trigger_id,
            "file": {
                "name": f"{trigger_id}.pdf",
                "mimeType": "application/pdf",
                "buffer": b"%PDF-1.4 acceptance evidence",
            },
        },
    )
    payload = response.json()
    assert response.status == 201, payload
    assert payload.get("ok"), payload
    return payload


def _scan_order_qr(
    page: Page,
    order_id: int,
    action: str = "inspected",
    payload: str | None = None,
) -> dict:
    if payload is None:
        qr = _action(page, "generate_qr", {"order_id": order_id})
        qr_card = next(
            card for card in qr.get("cards") or []
            if card.get("type") == "qr"
        )
        payload = (qr_card.get("data") or {}).get("payload")
        assert payload, qr_card
    response = page.context.request.post(
        payload,
        headers={
            "X-CSRFToken": _csrf(page),
            "Referer": page.url,
        },
        form={"action": action},
    )
    result = response.json()
    assert response.status == 200, result
    assert result.get("ok"), result
    result["_payload"] = payload
    return result


def _upload_pricelist(page: Page, path: Path, brand: str) -> dict:
    assert path.exists(), path
    origin = f"{urlsplit(page.url).scheme}://{urlsplit(page.url).netloc}"
    upload_response = page.context.request.post(
        f"{origin}/api/assistant/upload-pricelist/",
        headers={
            "X-CSRFToken": _csrf(page),
            "Referer": f"{origin}/chat/",
        },
        multipart={
            "file": {
                "name": path.name,
                "mimeType": (
                    "application/vnd.openxmlformats-officedocument."
                    "spreadsheetml.sheet"
                ),
                "buffer": path.read_bytes(),
            }
        },
    )
    pending = upload_response.json()
    assert upload_response.status == 200, pending
    pending = {
        "import_id": pending["import_id"],
        "mapping": pending.get("suggested_mapping") or {},
        "transform_rules": pending.get("transform_rules") or {},
        "constants": pending.get("constants") or {},
    }
    for field in ("title", "price_exw"):
        assert pending["mapping"].get(field), (field, pending)
    headers = upload_response.json().get("headers", [])
    # The automatic mapper must distinguish weight from lead time. Overriding
    # this in the test used to hide a real import defect.
    if "Вес, кг" in headers:
        assert pending["mapping"].get("weight_kg") == "Вес, кг", pending
    if "Срок, дн." in headers:
        assert pending["mapping"].get("lead_time_days") == "Срок, дн.", pending

    constants = {
        **pending["constants"],
        "brand": brand,
        "currency": "USD",
        "condition": "new",
        "availability": "in_stock",
        "warehouse_address": "JAFZA Test Warehouse, Dubai, UAE",
        "sea_port": "AEJEA · Jebel Ali",
        "air_port": "AEDXB · Dubai International Airport",
    }
    result = _json_fetch(
        page,
        f"/api/assistant/upload-pricelist/{pending['import_id']}/commit/",
        method="POST",
        body={
            "mapping": pending["mapping"],
            "transform_rules": pending["transform_rules"],
            "constants": constants,
        },
    )
    assert result["status"] in (200, 202), result
    if result["status"] == 202:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            result = _json_fetch(
                page,
                f"/api/assistant/upload-pricelist/{pending['import_id']}/import-progress/",
            )
            if result.get("done"):
                result = result.get("result") or result
                break
            page.wait_for_timeout(500)
        else:
            raise AssertionError("pricelist import timed out")
    assert result.get("ok") is True, result
    assert result.get("created", 0) + result.get("updated", 0) > 0, result
    return result


def _first_oem_from_search(page: Page, query: str) -> str:
    result = _action(page, "search_parts", {"query": query})
    cards = result.get("cards") or []
    assert cards, result
    for card in cards:
        data = card.get("data") or {}
        for item in data.get("items") or []:
            oem = item.get("oem_number") or item.get("article") or item.get("id")
            if oem:
                return str(oem)
    raise AssertionError(f"no searchable OEM in response: {result}")


def test_01_multiple_sellers_upload_real_pricelists(browser: Browser, base_url: str):
    seller_a = _login(browser, base_url, SELLER_A, "seller")
    imported_a = _upload_pricelist(seller_a, PRICE_A, "ITU STORY A")
    seller_a.close()

    seller_b = _login(browser, base_url, SELLER_B, "seller")
    imported_b = _upload_pricelist(seller_b, PRICE_B, "ITU STORY B")
    seller_b.close()

    assert imported_a.get("failed", 0) < (
        imported_a.get("created", 0) + imported_a.get("updated", 0)
    )
    assert imported_b.get("failed", 0) < (
        imported_b.get("created", 0) + imported_b.get("updated", 0)
    )


def test_02_extra_seller_role_can_manage_catalog(browser: Browser, base_url: str):
    multi = _login(browser, base_url, MULTI, "buyer")
    _switch_role(multi, "seller")
    imported = _upload_pricelist(multi, PRICE_B, "ITU STORY MULTI")
    assert imported.get("created", 0) + imported.get("updated", 0) > 0
    multi.close()


def test_03_buyer_search_to_competing_quotes_and_order(
    browser: Browser,
    base_url: str,
):
    buyer = _login(browser, base_url, BUYER_A, "buyer")
    oem = _first_oem_from_search(buyer, STORY_OEM)

    created = _action(
        buyer,
        "create_rfq",
        {"query": oem, "quantity": 10, "mode": "auto"},
    )
    rfq_cards = [c for c in created.get("cards") or [] if c.get("type") == "rfq"]
    assert rfq_cards, created
    rfq_id = int(rfq_cards[0]["data"]["id"])

    quotes = _action(buyer, "view_rfq_quotes", {"rfq_id": rfq_id})
    quote_cards = quotes.get("cards") or []
    assert quote_cards, quotes

    quote_ids = []
    for card in quote_cards:
        for item in (card.get("data") or {}).get("items") or []:
            if item.get("id"):
                quote_ids.append(int(item["id"]))
    if not quote_ids:
        for action in quotes.get("actions") or []:
            if action.get("params", {}).get("quote_id"):
                quote_ids.append(int(action["params"]["quote_id"]))
    assert len(set(quote_ids)) >= 3, quotes

    selected_quote = min(set(quote_ids))
    preview = _action(buyer, "accept_quote", {"quote_id": selected_quote})
    assert any(
        (card.get("data") or {}).get("confirm_action") == "accept_quote"
        for card in preview.get("cards") or []
    ), preview
    accepted = _action(
        buyer,
        "accept_quote",
        {"quote_id": selected_quote, "confirmed": True},
    )
    order_actions = accepted.get("actions") or accepted.get("contextual_actions") or []
    order_ids = [
        int(a["params"]["order_id"])
        for a in order_actions
        if a.get("params", {}).get("order_id")
    ]
    assert order_ids, accepted
    order_id = order_ids[0]

    invoice_card = next(
        card for card in accepted.get("cards") or []
        if card.get("type") == "invoice"
    )
    invoice_url = (invoice_card.get("data") or {}).get("pdf_url")
    contract_url = next(
        action["params"]["_url"]
        for action in accepted.get("actions") or []
        if action.get("action") == "open_url"
        and "договор" in (action.get("label") or "").lower()
    )
    assert invoice_url and contract_url, accepted
    _assert_pdf_available(buyer, contract_url)
    _assert_pdf_available(buyer, invoice_url)

    documents = _action(
        buyer,
        "settlement_my_documents",
        {"order_id": order_id},
    )
    buyer_signature = next(
        action for action in documents.get("actions") or []
        if action.get("action") == "sign_document"
    )
    signed = _action(buyer, "sign_document", buyer_signature["params"])
    assert "подпис" in (signed.get("text") or "").lower(), signed

    payment_notice = next(
        action for action in accepted.get("actions") or []
        if action.get("action") == "settlement_report_paid"
    )
    reported = _action(
        buyer,
        "settlement_report_paid",
        payment_notice["params"],
    )
    assert reported.get("action_succeeded") is True, reported
    assert "сверк" in (reported.get("text") or "").lower(), reported
    buyer.close()


def test_04_second_buyer_cannot_read_first_buyers_data(
    browser: Browser,
    base_url: str,
):
    first = _login(browser, base_url, BUYER_A, "buyer")
    own = _action(first, "get_orders", {})
    order_cards = [c for c in own.get("cards") or [] if c.get("type") == "order"]
    assert order_cards, own
    order_id = int(order_cards[0]["data"]["id"])
    first.close()

    second = _login(browser, base_url, BUYER_B, "buyer")
    denied = _action(second, "get_order_detail", {"order_id": order_id})
    assert "доступ" in (denied.get("text") or "").lower(), denied
    second.close()


def test_05_paid_order_completes_logistics_and_invoice_cycle(
    browser: Browser,
    base_url: str,
):
    buyer = _login(browser, base_url, BUYER_A, "buyer")
    orders = _action(buyer, "get_orders", {})
    order_ids = [
        int(card["data"]["id"])
        for card in orders.get("cards") or []
        if card.get("type") == "order" and (card.get("data") or {}).get("id")
    ]
    assert order_ids, orders
    order_id = max(order_ids)
    buyer.close()

    seller_pages = []
    owner = None
    non_owner = None
    for username in (SELLER_A, SELLER_B, MULTI):
        page = _login(browser, base_url, username, "seller")
        seller_pages.append(page)
        detail = _action(page, "get_order_detail", {"order_id": order_id})
        if "нет доступа" in (detail.get("text") or "").lower():
            non_owner = non_owner or page
        else:
            owner = page

    assert owner is not None, "none of the quoted sellers owns the accepted order"
    assert non_owner is not None, "all acceptance sellers unexpectedly own one order"
    denied = _action(non_owner, "advance_order", {"order_id": order_id})
    assert "не содержит ваших товаров" in (denied.get("text") or "").lower(), denied

    payment_gate = _action(owner, "advance_order", {"order_id": order_id})
    assert any(
        marker in (payment_gate.get("text") or "").lower()
        for marker in ("первого банковского платежа", "подтверждения оплаты")
    ), payment_gate
    for page in seller_pages:
        if page is not owner:
            page.close()

    seller_documents = _action(
        owner,
        "settlement_seller_documents",
        {"order_id": order_id},
    )
    seller_signature = next(
        action for action in seller_documents.get("actions") or []
        if action.get("action") == "sign_document"
    )
    signed_by_seller = _action(owner, "sign_document", seller_signature["params"])
    assert "подпис" in (signed_by_seller.get("text") or "").lower(), signed_by_seller

    operator = _login(browser, base_url, OPERATOR, "operator_payment")
    finance_queue = _action(
        operator,
        "settlement_finance_queue",
        {"order_id": order_id},
    )
    platform_signatures = [
        row for row in _card_rows(finance_queue)
        if row.get("action") == "sign_document"
    ]
    assert len(platform_signatures) >= 2, finance_queue
    for row in platform_signatures:
        signed_by_platform = _action(operator, "sign_document", row["params"])
        assert "подпис" in (signed_by_platform.get("text") or "").lower(), signed_by_platform

    finance_queue = _action(
        operator,
        "settlement_finance_queue",
        {"order_id": order_id},
    )
    reserve_receivable = _find_action_row(
        finance_queue,
        "settlement_confirm_payment",
        "покупатель должен платформе",
        "первый платёж",
    )
    _confirm_invoice_payment(
        operator,
        int(reserve_receivable["params"]["invoice_id"]),
        f"ITU-IN-RESERVE-{order_id}",
    )

    advanced = _action(owner, "advance_order", {"order_id": order_id})
    assert "подтвержд" in (advanced.get("text") or "").lower(), advanced
    production = _action(owner, "advance_order", {"order_id": order_id})
    assert "производств" in (production.get("text") or "").lower(), production
    ready = _action(owner, "advance_order", {"order_id": order_id})
    assert "готов" in (ready.get("text") or "").lower(), ready

    buyer = _login(browser, base_url, BUYER_A, "buyer")
    buyer_documents = _action(
        buyer,
        "settlement_my_documents",
        {"order_id": order_id},
    )
    final_notice = next(
        action for action in buyer_documents.get("actions") or []
        if action.get("action") == "settlement_report_paid"
    )
    final_reported = _action(
        buyer,
        "settlement_report_paid",
        final_notice["params"],
    )
    assert final_reported.get("action_succeeded") is True, final_reported
    buyer.close()

    finance_queue = _action(
        operator,
        "settlement_finance_queue",
        {"order_id": order_id},
    )
    final_receivable = _find_action_row(
        finance_queue,
        "settlement_confirm_payment",
        "покупатель должен платформе",
        "окончательный платёж",
    )
    _confirm_invoice_payment(
        operator,
        int(final_receivable["params"]["invoice_id"]),
        f"ITU-IN-FINAL-{order_id}",
    )

    finance_queue = _action(
        operator,
        "settlement_finance_queue",
        {"order_id": order_id},
    )
    seller_invoices = [
        row for row in _card_rows(finance_queue)
        if row.get("action") == "settlement_confirm_payment"
        and "платформа должна продавцу" in (
            f"{row.get('title', '')} {row.get('subtitle', '')}".lower()
        )
    ]
    assert len(seller_invoices) == 2, finance_queue
    for index, row in enumerate(seller_invoices, start=1):
        _confirm_invoice_payment(
            operator,
            int(row["params"]["invoice_id"]),
            f"ITU-OUT-{order_id}-{index}",
        )
    operator.close()

    shipping = {
        "order_id": order_id,
        "tracking_number": f"ITU-STORY-{order_id}",
        "carrier": "ITU Acceptance Carrier",
        "carrier_phone": "+971500000000",
        "carrier_email": "dispatch@example.test",
    }
    blocked_shipping = _action(owner, "ship_order", shipping)
    assert "чек-лист" in (blocked_shipping.get("text") or "").lower(), blocked_shipping
    for trigger_id in ("invoice", "packing_list"):
        _upload_order_evidence(
            owner,
            order_id,
            "ready_to_ship",
            trigger_id,
        )
    ready_scan = _scan_order_qr(owner, order_id)
    assert ready_scan.get("trigger_id") == "fob_handoff_qr", ready_scan
    package_qr_payload = ready_scan["_payload"]
    shipped = _action(owner, "ship_order", shipping)
    assert "отгружен" in (shipped.get("text") or "").lower(), shipped
    owner.close()

    logist = _login(browser, base_url, LOGIST, "operator_logist")
    customs = _action(logist, "advance_order", {"order_id": order_id})
    assert "тамож" in (customs.get("text") or "").lower(), customs

    customs_blocked = _action(logist, "advance_order", {"order_id": order_id})
    assert "декларац" in (customs_blocked.get("text") or "").lower(), customs_blocked
    declaration = _upload_order_evidence(
        logist,
        order_id,
        "customs",
        "declaration",
    )
    assert declaration.get("trigger_id") == "declaration", declaration
    transit_rf = _action(logist, "advance_order", {"order_id": order_id})
    assert "транзит" in (transit_rf.get("text") or "").lower(), transit_rf

    rf_scan = _scan_order_qr(logist, order_id, payload=package_qr_payload)
    assert rf_scan.get("trigger_id") == "qr_rf", rf_scan
    _upload_order_evidence(logist, order_id, "transit_rf", "ttn_rf")
    issuing = _action(logist, "advance_order", {"order_id": order_id})
    assert "выдач" in (issuing.get("text") or "").lower(), issuing

    issuing_trigger = _scan_order_qr(
        logist,
        order_id,
        payload=package_qr_payload,
    )
    assert issuing_trigger.get("trigger_id") == "qr_issuing", issuing_trigger
    delivered = _action(logist, "advance_order", {"order_id": order_id})
    assert "доставлен" in (delivered.get("text") or "").lower(), delivered
    logist.close()

    buyer = _login(browser, base_url, BUYER_A, "buyer")
    denied_buyer_qr = _action(buyer, "generate_qr", {"order_id": order_id})
    assert denied_buyer_qr.get("action_succeeded") is False, denied_buyer_qr
    assert not (denied_buyer_qr.get("cards") or []), denied_buyer_qr
    received_scan = _scan_order_qr(
        buyer,
        order_id,
        action="received",
        payload=package_qr_payload,
    )
    assert received_scan.get("trigger_id") == "qr_received", received_scan
    _upload_order_evidence(buyer, order_id, "delivered", "signed_docs")
    delivery_preview = _action(buyer, "confirm_delivery", {"order_id": order_id})
    assert any(
        action.get("action") == "confirm_delivery"
        for action in delivery_preview.get("actions") or []
    ), delivery_preview
    settled = _action(
        buyer,
        "confirm_delivery",
        {"order_id": order_id, "confirmed": True},
    )
    assert "закрыт" in (settled.get("text") or "").lower(), settled
    detail = _action(buyer, "get_orders", {})
    completed_cards = [
        card
        for card in detail.get("cards") or []
        if (
            card.get("type") == "order"
            and int((card.get("data") or {}).get("id") or 0) == order_id
            and (card.get("data") or {}).get("status_code") == "completed"
        )
    ]
    assert completed_cards, detail
    buyer.close()


def test_06_rfq_notification_arrives_without_page_reload(
    browser: Browser,
    base_url: str,
):
    buyer = _login(browser, base_url, BUYER_A, "buyer")
    cleared = _json_fetch(
        buyer,
        "/api/assistant/notifications/read-all/",
        method="POST",
        body={},
    )
    assert cleared["status"] == 200, cleared
    badge = buyer.locator("#bellBadge")
    clear_deadline = time.monotonic() + 5
    while time.monotonic() < clear_deadline:
        if not badge.is_visible() or not (badge.text_content() or "").strip():
            break
        buyer.wait_for_timeout(100)
    else:
        raise AssertionError("notification badge did not clear")
    buyer.evaluate("window.__acceptanceRealtimeMarker = Date.now()")

    created = _action(
        buyer,
        "create_rfq",
        {"query": STORY_OEM, "quantity": 1, "mode": "auto"},
    )
    assert any(c.get("type") == "rfq" for c in created.get("cards") or []), created

    push_deadline = time.monotonic() + 10
    while time.monotonic() < push_deadline:
        marker_survived = bool(
            buyer.evaluate("window.__acceptanceRealtimeMarker || 0")
        )
        badge_text = (badge.text_content() or "").strip()
        if (
            marker_survived
            and badge.is_visible()
            and badge_text.isdigit()
            and int(badge_text) > 0
            and buyer.title().startswith("(")
        ):
            break
        buyer.wait_for_timeout(100)
    else:
        raise AssertionError("RFQ notification did not arrive in the open tab")
    assert buyer.locator("#notifToastHost > div").count() > 0
    buyer.close()

"""Tests for /payments/callback/ endpoint (SECURITY P0-2).

Матрица:
  - No secret + DEBUG=True   → 503 (fail-closed)
  - No secret + DEBUG=False  → 503 (прод без секрета — отказ)
  - Wrong secret + prod      → 403
  - Correct secret + prod    → 200 + меняет статус заказа
  - Секрет в query-param     → отклоняется
  - order_not_found          → 404
"""
import json

import pytest
from django.contrib.auth import get_user_model
from django.test import Client, override_settings

User = get_user_model()

CALLBACK_URL = "/payments/callback/"


@pytest.fixture
def client():
    return Client(HTTP_HOST="testserver")


@pytest.fixture
def buyer(db):
    return User.objects.create_user(
        username="cb_buyer", password="pass", email="cb@test.local"
    )


@pytest.fixture
def order(db, buyer):
    from marketplace.models import Order
    return Order.objects.create(
        buyer=buyer,
        status="pending",
        payment_status="pending",
        total_amount=100,
        reserve_amount=10,
        invoice_number="INV-CALLBACK-1",
    )


# ── 1. Без секрета в DEBUG ─────────────────────────────────────────────────


@pytest.mark.django_db
@override_settings(PAYMENT_CALLBACK_SECRET="", DEBUG=True)
def test_callback_no_secret_debug_503(client, order):
    """Даже в режиме DEBUG callback без секрета не меняет оплату."""
    resp = client.post(
        CALLBACK_URL,
        data=json.dumps({"order_id": order.id, "status": "reserve_paid"}),
        content_type="application/json",
    )
    assert resp.status_code == 503
    assert resp.json().get("ok") is False
    order.refresh_from_db()
    assert order.payment_status == "pending"


@pytest.mark.django_db
@override_settings(PAYMENT_CALLBACK_SECRET="correct-secret", DEBUG=True)
def test_callback_debug_still_requires_provider_identifiers(client, order):
    resp = client.post(
        CALLBACK_URL,
        data=json.dumps({
            "order_id": order.id,
            "status": "reserve_paid",
            "amount": "10.00",
            "currency": "USD",
        }),
        content_type="application/json",
        HTTP_X_PAYMENT_SECRET="correct-secret",
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "transaction_id_required"
    order.refresh_from_db()
    assert order.payment_status == "pending"


# ── 2. Без секрета в PRODUCTION ────────────────────────────────────────────


@pytest.mark.django_db
@override_settings(PAYMENT_CALLBACK_SECRET="", DEBUG=False)
def test_callback_no_secret_prod_503(client, order):
    """В production без секрета → 503, заказ не тронут."""
    resp = client.post(
        CALLBACK_URL,
        data=json.dumps({"order_id": order.id, "status": "reserve_paid"}),
        content_type="application/json",
    )
    assert resp.status_code == 503
    data = resp.json()
    assert data.get("ok") is False
    assert "PAYMENT_CALLBACK_SECRET" in data.get("error", "")

    order.refresh_from_db()
    assert order.payment_status == "pending"


# ── 3. Неверный секрет ─────────────────────────────────────────────────────


@pytest.mark.django_db
@override_settings(PAYMENT_CALLBACK_SECRET="correct-secret", DEBUG=False)
def test_callback_wrong_secret_403(client, order):
    """Неверный секрет → 403, заказ не тронут."""
    resp = client.post(
        CALLBACK_URL,
        data=json.dumps({"order_id": order.id, "status": "reserve_paid"}),
        content_type="application/json",
        HTTP_X_PAYMENT_SECRET="wrong-secret",
    )
    assert resp.status_code == 403
    data = resp.json()
    assert data.get("ok") is False
    assert data.get("error") == "invalid_secret"

    order.refresh_from_db()
    assert order.payment_status == "pending"


# ── 4. Верный секрет через заголовок ─────────────────────────────────────


@pytest.mark.django_db
@override_settings(PAYMENT_CALLBACK_SECRET="correct-secret", DEBUG=False)
def test_callback_correct_secret_processes(client, order):
    """Верный секрет → 200, payment_status обновлён."""
    resp = client.post(
        CALLBACK_URL,
        data=json.dumps({
            "order_id": order.id,
            "invoice_number": order.invoice_number,
            "status": "reserve_paid",
            "transaction_id": "provider-tx-correct",
            "amount": "10.00",
            "currency": "USD",
        }),
        content_type="application/json",
        HTTP_X_PAYMENT_SECRET="correct-secret",
    )
    assert resp.status_code == 200
    assert resp.json().get("ok") is True

    order.refresh_from_db()
    assert order.payment_status == "reserve_paid"


@pytest.mark.django_db
@override_settings(PAYMENT_CALLBACK_SECRET="correct-secret", DEBUG=False)
def test_callback_is_idempotent_by_transaction_id(client, order):
    payload = {
        "order_id": order.id,
        "invoice_number": order.invoice_number,
        "status": "reserve_paid",
        "transaction_id": "provider-tx-1",
        "amount": "10.00",
        "currency": "USD",
    }
    first = client.post(
        CALLBACK_URL,
        data=json.dumps(payload),
        content_type="application/json",
        HTTP_X_PAYMENT_SECRET="correct-secret",
    )
    second = client.post(
        CALLBACK_URL,
        data=json.dumps(payload),
        content_type="application/json",
        HTTP_X_PAYMENT_SECRET="correct-secret",
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["idempotent_replay"] is True


@pytest.mark.django_db
@override_settings(PAYMENT_CALLBACK_SECRET="correct-secret", DEBUG=False)
def test_callback_rejects_skipping_reserve(client, order):
    response = client.post(
        CALLBACK_URL,
        data=json.dumps({
            "order_id": order.id,
            "invoice_number": order.invoice_number,
            "status": "paid",
            "transaction_id": "provider-tx-skip",
            "amount": "90.00",
            "currency": "USD",
        }),
        content_type="application/json",
        HTTP_X_PAYMENT_SECRET="correct-secret",
    )

    assert response.status_code == 409
    order.refresh_from_db()
    assert order.payment_status == "pending"


@pytest.mark.django_db
@override_settings(PAYMENT_CALLBACK_SECRET="correct-secret", DEBUG=False)
def test_callback_rejects_amount_mismatch(client, order):
    response = client.post(
        CALLBACK_URL,
        data=json.dumps({
            "order_id": order.id,
            "invoice_number": order.invoice_number,
            "status": "reserve_paid",
            "transaction_id": "provider-tx-wrong-amount",
            "amount": "100.00",
            "currency": "USD",
        }),
        content_type="application/json",
        HTTP_X_PAYMENT_SECRET="correct-secret",
    )

    assert response.status_code == 400
    assert response.json()["error"] == "amount_mismatch"
    order.refresh_from_db()
    assert order.payment_status == "pending"


@pytest.mark.django_db
@override_settings(PAYMENT_CALLBACK_SECRET="correct-secret", DEBUG=False)
def test_callback_rejects_transaction_reuse_for_another_order(
    client,
    order,
    buyer,
):
    from marketplace.models import Order

    shared_transaction = "provider-tx-shared"
    first = client.post(
        CALLBACK_URL,
        data=json.dumps({
            "order_id": order.id,
            "invoice_number": order.invoice_number,
            "status": "reserve_paid",
            "transaction_id": shared_transaction,
            "amount": "10.00",
            "currency": "USD",
        }),
        content_type="application/json",
        HTTP_X_PAYMENT_SECRET="correct-secret",
    )
    assert first.status_code == 200

    another = Order.objects.create(
        buyer=buyer,
        status="pending",
        payment_status="pending",
        total_amount=200,
        reserve_amount=20,
        invoice_number="INV-CALLBACK-2",
    )
    replay = client.post(
        CALLBACK_URL,
        data=json.dumps({
            "order_id": another.id,
            "invoice_number": another.invoice_number,
            "status": "reserve_paid",
            "transaction_id": shared_transaction,
            "amount": "20.00",
            "currency": "USD",
        }),
        content_type="application/json",
        HTTP_X_PAYMENT_SECRET="correct-secret",
    )

    assert replay.status_code == 409
    assert replay.json()["error"] == "transaction_id_reused"
    another.refresh_from_db()
    assert another.payment_status == "pending"


@pytest.mark.django_db
@override_settings(PAYMENT_CALLBACK_SECRET="correct-secret", DEBUG=False)
def test_callback_rejects_non_object_json(client):
    response = client.post(
        CALLBACK_URL,
        data="[]",
        content_type="application/json",
        HTTP_X_PAYMENT_SECRET="correct-secret",
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_payload"


# ── 5. Секрет через query-param ────────────────────────────────────────────


@pytest.mark.django_db
@override_settings(PAYMENT_CALLBACK_SECRET="correct-secret", DEBUG=False)
def test_callback_secret_via_query_param_rejected(client, order):
    """Секрет через ?secret= больше не принимается: он утекает в журналы."""
    resp = client.post(
        CALLBACK_URL + "?secret=correct-secret",
        data=json.dumps({"order_id": order.id, "status": "paid"}),
        content_type="application/json",
    )
    assert resp.status_code == 403
    assert resp.json().get("error") == "invalid_secret"


# ── 6. Несуществующий заказ ────────────────────────────────────────────────


@pytest.mark.django_db
@override_settings(PAYMENT_CALLBACK_SECRET="correct-secret", DEBUG=True)
def test_callback_order_not_found(client):
    """Несуществующий order_id → 404."""
    resp = client.post(
        CALLBACK_URL,
        data=json.dumps({"order_id": 999999, "status": "reserve_paid"}),
        content_type="application/json",
        HTTP_X_PAYMENT_SECRET="correct-secret",
    )
    assert resp.status_code == 404
    assert resp.json().get("ok") is False

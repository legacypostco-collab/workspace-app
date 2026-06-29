"""Tests for /payments/callback/ endpoint (SECURITY P0-2).

Матрица:
  - No secret + DEBUG=True   → 200 (dev-режим, разрешено)
  - No secret + DEBUG=False  → 503 (прод без секрета — отказ)
  - Wrong secret + prod      → 403
  - Correct secret + prod    → 200 + меняет статус заказа
  - Секрет в query-param     → принимается
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
    )


# ── 1. Без секрета в DEBUG ─────────────────────────────────────────────────


@pytest.mark.django_db
@override_settings(PAYMENT_CALLBACK_SECRET="", DEBUG=True)
def test_callback_no_secret_debug_ok(client, order):
    """В режиме DEBUG без секрета endpoint проходим."""
    resp = client.post(
        CALLBACK_URL,
        data=json.dumps({"order_id": order.id, "status": "reserve_paid"}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    assert resp.json().get("ok") is True


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
        data=json.dumps({"order_id": order.id, "status": "reserve_paid"}),
        content_type="application/json",
        HTTP_X_PAYMENT_SECRET="correct-secret",
    )
    assert resp.status_code == 200
    assert resp.json().get("ok") is True

    order.refresh_from_db()
    assert order.payment_status == "reserve_paid"


# ── 5. Секрет через query-param ────────────────────────────────────────────


@pytest.mark.django_db
@override_settings(PAYMENT_CALLBACK_SECRET="correct-secret", DEBUG=False)
def test_callback_secret_via_query_param(client, order):
    """Секрет через ?secret= тоже принимается."""
    resp = client.post(
        CALLBACK_URL + "?secret=correct-secret",
        data=json.dumps({"order_id": order.id, "status": "paid"}),
        content_type="application/json",
    )
    assert resp.status_code == 200
    assert resp.json().get("ok") is True


# ── 6. Несуществующий заказ ────────────────────────────────────────────────


@pytest.mark.django_db
@override_settings(PAYMENT_CALLBACK_SECRET="", DEBUG=True)
def test_callback_order_not_found(client):
    """Несуществующий order_id → 404."""
    resp = client.post(
        CALLBACK_URL,
        data=json.dumps({"order_id": 999999, "status": "reserve_paid"}),
        content_type="application/json",
    )
    assert resp.status_code == 404
    assert resp.json().get("ok") is False

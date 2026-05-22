"""Тесты op_assign_carrier — операторский action назначения перевозчика."""
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model

from assistant.operator_actions import op_assign_carrier
from marketplace.models import Order, OrderEvent, UserProfile

U = get_user_model()


@pytest.fixture
def buyer(db):
    u = U.objects.create_user(username="acb_buyer", password="x", email="b@x.local")
    UserProfile.objects.create(user=u, role="buyer", language="ru")
    return u


@pytest.fixture
def operator(db):
    u = U.objects.create_user(username="acb_op", password="x", is_staff=True)
    UserProfile.objects.create(user=u, role="operator", language="ru")
    return u


@pytest.fixture
def order(db, buyer):
    return Order.objects.create(
        buyer=buyer, customer_name="Test", customer_email="b@x.local",
        customer_phone="+7", delivery_address="addr",
        status="transit_abroad", payment_status="paid",
        total_amount=Decimal("12000"), reserve_amount=Decimal("1200"),
    )


# ── 1. Permission ────────────────────────────────────────────

def test_buyer_cannot_assign_carrier(buyer, order):
    r = op_assign_carrier({"order_id": order.id}, buyer, "buyer")
    assert "только оператор" in (r.text or "").lower() or \
           "оператор" in (r.text or "").lower()


def test_seller_cannot_assign_carrier(buyer, order):
    seller = U.objects.create_user(username="acb_sel", password="x")
    UserProfile.objects.create(user=seller, role="seller", language="ru")
    r = op_assign_carrier({"order_id": order.id}, seller, "seller")
    assert "оператор" in (r.text or "").lower()


def test_operator_phase1_returns_form(operator, order):
    r = op_assign_carrier({"order_id": order.id}, operator, "operator")
    assert r.cards
    form = r.cards[0]["data"]
    assert form["submit_action"] == "op_assign_carrier"
    names = [f["name"] for f in form["fields"]]
    assert {"carrier_name", "tracking_number", "tracking_url",
            "carrier_phone", "carrier_email"}.issubset(set(names))


# ── 2. Validation ────────────────────────────────────────────

def test_missing_carrier_name_rejected(operator, order):
    r = op_assign_carrier({
        "order_id": order.id, "confirmed": True,
        "tracking_number": "MAEU1234",
    }, operator, "operator")
    assert "обязател" in (r.text or "").lower()


def test_missing_tracking_number_rejected(operator, order):
    r = op_assign_carrier({
        "order_id": order.id, "confirmed": True,
        "carrier_name": "DHL",
    }, operator, "operator")
    assert "обязател" in (r.text or "").lower()


def test_invalid_order_id(operator):
    r = op_assign_carrier({"order_id": 9999999, "confirmed": True,
                           "carrier_name": "X", "tracking_number": "Y"},
                          operator, "operator")
    assert "не найден" in (r.text or "").lower()


# ── 3. Happy path: assign + persist + audit ──────────────────

def test_assign_persists_to_db(operator, order):
    r = op_assign_carrier({
        "order_id": order.id, "confirmed": True,
        "carrier_name": "Maersk Line",
        "tracking_number": "MAEU1234567",
        "tracking_url": "https://www.maersk.com/track/MAEU1234567",
        "carrier_phone": "+45 33 63 33 63",
        "carrier_email": "track@maersk.com",
    }, operator, "operator")
    order.refresh_from_db()
    assert order.carrier_name == "Maersk Line"
    assert order.tracking_number == "MAEU1234567"
    assert order.tracking_url == "https://www.maersk.com/track/MAEU1234567"
    assert order.carrier_phone == "+45 33 63 33 63"
    assert "✅" in r.text or "назначен" in r.text.lower()


def test_assign_creates_audit_event(operator, order):
    op_assign_carrier({
        "order_id": order.id, "confirmed": True,
        "carrier_name": "DHL", "tracking_number": "DHL999",
    }, operator, "operator")
    ev = OrderEvent.objects.filter(order=order, event_type="carrier_assigned").first()
    assert ev is not None
    assert ev.meta["carrier_name"] == "DHL"
    assert ev.meta["tracking_number"] == "DHL999"
    assert ev.meta["by"] == operator.username


def test_assign_replaces_previous_carrier(operator, order):
    # Первое назначение
    op_assign_carrier({
        "order_id": order.id, "confirmed": True,
        "carrier_name": "Maersk", "tracking_number": "M-1",
    }, operator, "operator")
    # Замена
    op_assign_carrier({
        "order_id": order.id, "confirmed": True,
        "carrier_name": "DHL", "tracking_number": "D-2",
    }, operator, "operator")
    order.refresh_from_db()
    assert order.carrier_name == "DHL"
    # Аудит должен зафиксировать prev_carrier="Maersk"
    last = OrderEvent.objects.filter(
        order=order, event_type="carrier_assigned",
    ).order_by("-created_at").first()
    assert last.meta["prev_carrier"] == "Maersk"
    assert last.meta["prev_tracking"] == "M-1"


# ── 4. Field-length truncation (защита от слишком длинных значений) ──

def test_long_carrier_name_truncated(operator, order):
    long_name = "X" * 500
    op_assign_carrier({
        "order_id": order.id, "confirmed": True,
        "carrier_name": long_name, "tracking_number": "T1",
    }, operator, "operator")
    order.refresh_from_db()
    # CharField(max_length=120) — мы режем в коде до 120
    assert len(order.carrier_name) <= 120

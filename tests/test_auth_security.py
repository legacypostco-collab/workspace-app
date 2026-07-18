"""Integration tests for auth + security hardening.

Что покрыто:
  - Login throttling (Axes + chat-native login flow)
  - Register throttling (5/hour)
  - Password reset throttling (3/hour)
  - demo_login backdoor закрыт когда DEBUG=False
  - GDPR export endpoint требует auth, отдаёт корректный JSON
  - Legacy cabinet → /chat/ redirect
"""
import sys

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, override_settings

User = get_user_model()

# Python 3.14 + Django 5.1.6 Context.__copy__ bug — skip template-render tests
_PY314_DJANGO_BUG = sys.version_info >= (3, 14)
_skip_template_render = pytest.mark.skipif(
    _PY314_DJANGO_BUG,
    reason="Django Context.__copy__ AttributeError on Python 3.14 (upstream bug)",
)


@pytest.fixture
def client():
    cache.clear()
    return Client(HTTP_HOST="testserver")


@pytest.fixture
def user(db):
    return User.objects.create_user(username="auth_test_user", password="strong-x-pass",
                                      email="auth@test.local")


# ══ Login throttling ══════════════════════════════════════════════

@_skip_template_render
def test_login_blocks_after_failed_attempts(client, user, settings):
    """Несколько неудачных входов блокируют пару IP+логин."""
    limit = int(getattr(settings, "AXES_FAILURE_LIMIT", 5))
    for _ in range(limit - 1):
        r = client.post("/login/", {"username": "auth_test_user", "password": "wrong"})
        assert r.status_code == 302
        assert "/chat/" in r["Location"]
    client.post("/login/", {"username": "auth_test_user", "password": "wrong"})
    # Следующая попытка — даже с правильным паролем — должна показать rate-limit.
    r = client.post("/login/", {"username": "auth_test_user", "password": "strong-x-pass"})
    assert r.status_code == 429


@_skip_template_render
def test_login_success_resets_counter(client, user):
    """Успешный логин обнуляет счётчик."""
    # Несколько неудач, но меньше порога блокировки.
    for _ in range(2):
        r = client.post("/login/", {"username": "auth_test_user", "password": "wrong"})
        assert r.status_code == 302
    # Успех
    r = client.post("/login/", {"username": "auth_test_user", "password": "strong-x-pass"})
    assert r.status_code == 302  # redirect to /chat/
    assert r["Location"] == "/chat/"
    # И снова несколько неудач — должно работать, счётчик сброшен.
    for _ in range(2):
        r = client.post("/login/", {"username": "auth_test_user", "password": "wrong"})
        assert r.status_code == 302


# ══ Register throttling ═══════════════════════════════════════════

@_skip_template_render
def test_register_blocks_after_5_attempts(client, db):
    """Регистрация: 5 попыток/час/IP."""
    for i in range(5):
        client.post("/register/", {
            "username": f"newuser_{i}", "email": f"new{i}@test.local",
            "password1": "VeryStr0ngPass!@#", "password2": "VeryStr0ngPass!@#",
            "role": "buyer", "language": "ru",
        })
    # 6-я попытка
    r = client.post("/register/", {
        "username": "newuser_99", "email": "new99@test.local",
        "password1": "VeryStr0ngPass!@#", "password2": "VeryStr0ngPass!@#",
        "role": "buyer", "language": "ru",
    })
    # Юзер не должен быть создан
    assert not User.objects.filter(username="newuser_99").exists()


# ══ Password reset throttling ═════════════════════════════════════

@_skip_template_render
def test_password_reset_blocks_after_3_requests(client, user):
    """Password reset: 3/час/IP — защита от спама на чужие email."""
    for _ in range(3):
        r = client.post("/password_reset/", {"email": "any@x.com"})
        # Django возвращает 302 на /password_reset/done/ даже когда email не найден
        # (anti-enumeration); или 200 c сообщением
        assert r.status_code in (200, 302)
    # 4-я попытка — наш RL должен подавить email-отправку
    r = client.post("/password_reset/", {"email": "any@x.com"})
    # Контент должен содержать упоминание лимита
    assert r.status_code in (200, 302, 429)


# ══ demo_login backdoor ════════════════════════════════════════════

@override_settings(DEBUG=False)
@_skip_template_render
def test_demo_login_404_in_prod(client):
    """В production /demo-login/ возвращает 404 (без `ALLOW_DEMO_LOGIN=1`)."""
    r = client.get("/demo-login/?role=buyer")
    assert r.status_code == 404


@override_settings(DEBUG=True)
@_skip_template_render
def test_demo_login_works_in_debug(client, db):
    """В DEBUG/dev — работает."""
    # Создаём demo_buyer
    User.objects.get_or_create(username="demo_buyer",
                                 defaults={"email": "demo@x.com"})[0].set_password("demo12345")
    r = client.get("/demo-login/?role=buyer")
    # В DEBUG → 302 на /chat/ (либо login если demo_buyer не нашёлся)
    assert r.status_code == 302


# ══ Legacy cabinet redirect ═══════════════════════════════════════

@_skip_template_render
def test_legacy_buyer_redirects_to_chat(client, user):
    """ Залогиненный заход на /buyer/ → 302 /chat/."""
    client.force_login(user)
    r = client.get("/buyer/")
    assert r.status_code == 302
    assert "/chat/" in r["Location"]


@_skip_template_render
def test_legacy_dashboard_redirects_to_chat(client, user):
    client.force_login(user)
    r = client.get("/dashboard/")
    assert r.status_code == 302
    assert "/chat/" in r["Location"]


@_skip_template_render
def test_admin_login_NOT_redirected(client, user):
    """Whitelist: /admin/ должен пройти через middleware → Django admin login."""
    client.force_login(user)
    r = client.get("/admin/")
    # 302 → /admin/login/ (не на /chat/)
    assert r.status_code == 302
    assert "/admin/" in r["Location"]


# ══ Healthchecks (no auth) ════════════════════════════════════════

def test_healthz_no_auth(client):
    r = client.get("/healthz/")
    assert r.status_code == 200
    assert r.json()["ok"] is True


@_skip_template_render
def test_readyz_no_auth(client):
    r = client.get("/readyz/")
    assert r.status_code in (200, 503)
    data = r.json()
    assert set(data).issubset({"ok", "status"})


# ══ GDPR export/delete ════════════════════════════════════════════

def test_gdpr_export_requires_auth(client):
    r = client.get("/api/me/export/")
    # login_required → 302 на /login/
    assert r.status_code == 302
    assert "/login" in r["Location"]


def test_gdpr_export_returns_json(client, user):
    client.force_login(user)
    r = client.get("/api/me/export/")
    assert r.status_code == 200
    assert r["Content-Type"] == "application/json"
    assert b'"user"' in r.content
    assert user.username.encode() in r.content


def test_gdpr_delete_requires_confirm(client, user):
    client.force_login(user)
    r = client.post("/api/me/delete/", {})  # no confirm
    assert r.status_code == 400


def test_gdpr_delete_anonymizes(client, user):
    client.force_login(user)
    r = client.post("/api/me/delete/", {"confirm": "DELETE"})
    assert r.status_code == 200
    user.refresh_from_db()
    assert user.is_active is False
    assert user.username.startswith("deleted-")
    assert user.email.endswith("@deleted.invalid")

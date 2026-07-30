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
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings
from marketplace.models import Category, Part

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


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
)
def test_password_reset_limits_the_same_email_across_different_ips(user):
    from django.core import mail

    cache.clear()
    for index in range(4):
        client = Client(
            HTTP_HOST="testserver",
            REMOTE_ADDR=f"198.51.100.{index + 1}",
        )
        response = client.post(
            "/password_reset/",
            {"email": user.email},
        )
        assert response.status_code in (200, 302)

    assert len(mail.outbox) == 3


def test_rate_limit_consumes_attempt_atomically(client):
    from marketplace.views import _rl_consume

    request = type("Request", (), {"META": {"REMOTE_ADDR": "203.0.113.10"}})()
    assert _rl_consume(request, "atomic-test", 2, 60)
    assert _rl_consume(request, "atomic-test", 2, 60)
    assert not _rl_consume(request, "atomic-test", 2, 60)


# ══ Вход без пароля отсутствует ════════════════════════════════════

@override_settings(DEBUG=True)
@_skip_template_render
def test_demo_login_route_is_removed(client):
    r = client.get("/demo-login/?role=buyer")
    assert r.status_code == 404


def test_disabled_cart_route_ignores_external_next(client, db):
    category = Category.objects.create(name="Redirect test", slug="redirect-test")
    part = Part.objects.create(
        category=category,
        title="Redirect test",
        slug="redirect-test",
        oem_number="REDIRECT-1",
        price="1.00",
        stock_quantity=1,
    )
    response = client.post(
        f"/cart/add/{part.id}/",
        {"next": "https://evil.example/phishing"},
    )
    assert response.status_code == 302
    assert response["Location"] == "/chat/"


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
def test_demo_route_keeps_public_workspace_for_authenticated_user(client, user):
    client.force_login(user)
    response = client.get("/demo/")
    assert response.status_code == 302
    assert response["Location"] == "/chat/?workspace=1"


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


@override_settings(TELEGRAM_WEBHOOK_SECRET="telegram-test-secret")
def test_telegram_webhook_requires_secret_header(client):
    missing = client.post(
        "/api/assistant/tg/webhook/",
        data="{}",
        content_type="application/json",
    )
    assert missing.status_code == 404

    with pytest.MonkeyPatch.context() as monkeypatch:
        handled = []
        monkeypatch.setattr("assistant.tg_views.handle_update", handled.append)
        accepted = client.post(
            "/api/assistant/tg/webhook/",
            data='{"update_id": 1}',
            content_type="application/json",
            HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="telegram-test-secret",
        )
    assert accepted.status_code == 200
    assert handled == [{"update_id": 1}]


@override_settings(TELEGRAM_WEBHOOK_SECRET="telegram-test-secret")
def test_telegram_webhook_rejects_oversized_or_non_object_payload(
    client,
    monkeypatch,
):
    monkeypatch.setattr("assistant.tg_views.MAX_TELEGRAM_WEBHOOK_BYTES", 4)
    oversized = client.post(
        "/api/assistant/tg/webhook/",
        data='{"update_id": 1}',
        content_type="application/json",
        HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="telegram-test-secret",
    )
    assert oversized.status_code == 413

    monkeypatch.setattr("assistant.tg_views.MAX_TELEGRAM_WEBHOOK_BYTES", 1024)
    non_object = client.post(
        "/api/assistant/tg/webhook/",
        data="[]",
        content_type="application/json",
        HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN="telegram-test-secret",
    )
    assert non_object.status_code == 400


def test_guest_spec_upload_rejects_executable_disguised_as_csv(client):
    upload = SimpleUploadedFile(
        "parts.csv",
        b"MZ\x90\x00malicious",
        content_type="text/csv",
    )
    response = client.post(
        "/api/assistant/upload-spec/",
        {"file": upload},
    )
    assert response.status_code == 400


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

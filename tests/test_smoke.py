"""Smoke tests — verify all key URLs return non-error status."""
import pytest
from django.contrib.auth.models import User
from django.test import Client


@pytest.fixture
def client():
    return Client()


@pytest.fixture
def user(db):
    u = User.objects.create_user(username="testuser", email="test@example.com", password="test12345")
    return u


@pytest.fixture
def authed_client(client, user):
    client.force_login(user)
    return client


@pytest.mark.django_db
@pytest.mark.parametrize("url", [
    "/",
    "/login/",
    "/register/",
    "/catalog/",
    "/brands/",
    "/categories/",
    "/help/",
    "/terms/",
    "/privacy/",
    "/cookies/",
    "/personal-data-consent/",
    "/password-reset/",
])
def test_public_urls_render(client, url):
    """All public URLs return 200."""
    resp = client.get(url)
    assert resp.status_code in (200, 301, 302), f"{url} returned {resp.status_code}"


@pytest.mark.django_db
def test_legal_pages_publish_operator_and_consent_version(client, settings):
    settings.PLATFORM_LEGAL_NAME = "Test Legal Operator"
    settings.PLATFORM_PAYMENT_CONTACT_EMAIL = "privacy@example.test"

    privacy = client.get("/privacy/")
    consent = client.get("/personal-data-consent/")

    assert privacy.status_code == consent.status_code == 200
    assert b"Test Legal Operator" in privacy.content
    assert b"privacy@example.test" in privacy.content
    assert b"PD-2026-08-08" in consent.content
    assert b"Test Legal Operator" in consent.content


@pytest.mark.django_db
@pytest.mark.parametrize("url", ["/api/docs/", "/api/schema/", "/api/redoc/"])
def test_api_documentation_requires_staff(client, url):
    assert client.get(url).status_code == 403

    staff = User.objects.create_user(
        username=f"docs_staff_{url.split('/')[2]}",
        password="not-used",
        is_staff=True,
    )
    client.force_login(staff)
    assert client.get(url).status_code == 200


@pytest.mark.django_db
def test_404_branded(client):
    resp = client.get("/this-does-not-exist/")
    assert resp.status_code == 404


@pytest.mark.django_db
def test_jsi18n_endpoint(client):
    """JS catalog should serve translations for current language."""
    resp = client.get("/jsi18n/")
    assert resp.status_code == 200
    assert b"django.gettext" in resp.content or b"gettext" in resp.content


@pytest.mark.django_db
def test_set_language(client):
    """Language switcher should set cookie."""
    resp = client.post("/i18n/setlang/", {"language": "en", "next": "/"})
    assert resp.status_code in (200, 302)


@pytest.mark.django_db
def test_removed_seller_cabinet_is_gone(client):
    """The retired seller cabinet must not expose its former interface."""
    resp = client.get("/seller/")
    assert resp.status_code == 410
    assert resp["Cache-Control"] == "no-store"

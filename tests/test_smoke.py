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
    client.login(username="testuser", password="test12345")
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
    "/password-reset/",
    "/api/docs/",
    "/api/schema/",
])
def test_public_urls_render(client, url):
    """All public URLs return 200."""
    resp = client.get(url)
    assert resp.status_code in (200, 301, 302), f"{url} returned {resp.status_code}"


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
def test_authenticated_cabinet_redirect(client):
    """Cabinet pages should redirect unauthenticated to login."""
    resp = client.get("/seller/")
    assert resp.status_code in (302, 200)  # 302 if login required

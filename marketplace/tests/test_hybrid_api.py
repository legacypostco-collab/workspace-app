import os
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import RequestFactory, TestCase, override_settings

from marketplace.models import Category, NewsletterSubscriber, Part, UserProfile
from marketplace.export_security import safe_spreadsheet_cell


class HybridApiTests(TestCase):
    def test_spreadsheet_export_neutralizes_formula_prefixes(self):
        for value in ("=1+1", "+cmd", "-2+3", "@SUM(A1)", "\t=1", "\r=1"):
            self.assertEqual(safe_spreadsheet_cell(value), f"'{value}")
        self.assertEqual(safe_spreadsheet_cell("OEM-123"), "OEM-123")
        self.assertEqual(safe_spreadsheet_cell(123), 123)

    def test_health_endpoint(self):
        response = self.client.get("/api/v1/health/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json().get("ok"))

    def test_readiness_endpoint(self):
        response = self.client.get("/api/v1/readiness/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json().get("ok"))

    @override_settings(TRUSTED_PROXY_NETWORKS=["127.0.0.1/32"])
    def test_client_ip_trusts_forwarding_only_from_known_proxy(self):
        from assistant.security import client_ip

        factory = RequestFactory()
        proxied = factory.get(
            "/",
            REMOTE_ADDR="127.0.0.1",
            HTTP_X_FORWARDED_FOR="198.51.100.70, 203.0.113.25",
        )
        self.assertEqual(client_ip(proxied), "203.0.113.25")

        direct = factory.get(
            "/",
            REMOTE_ADDR="198.51.100.90",
            HTTP_X_FORWARDED_FOR="203.0.113.99",
        )
        self.assertEqual(client_ip(direct), "198.51.100.90")

    def test_hybrid_analytics_requires_auth(self):
        response = self.client.get("/api/v1/analytics/hybrid/")
        self.assertIn(response.status_code, {401, 403})

    def test_demo_login_route_is_removed(self):
        self.assertEqual(self.client.get("/demo-login/").status_code, 404)

    def test_old_demo_center_uses_public_workspace_without_database_data(self):
        response = self.client.get("/demo-center/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/chat/?workspace=1")

    @override_settings(DEBUG=False)
    def test_stub_payment_adapter_is_disabled_outside_debug(self):
        from marketplace.payments import get_payment_adapter

        with self.assertRaises(RuntimeError):
            get_payment_adapter("stub")

    def test_newsletter_subscription_is_persisted_and_deduplicated(self):
        first = self.client.post(
            "/api/v1/newsletter/subscribe/",
            {"email": " Reader@Example.com "},
            content_type="application/json",
        )
        self.assertEqual(first.status_code, 202)
        self.assertEqual(first.json(), {"ok": True})
        self.assertEqual(
            NewsletterSubscriber.objects.filter(email="reader@example.com").count(),
            1,
        )

        repeated = self.client.post(
            "/api/v1/newsletter/subscribe/",
            {"email": "reader@example.com"},
            content_type="application/json",
        )
        self.assertEqual(repeated.status_code, 202)
        self.assertEqual(repeated.json(), {"ok": True})
        self.assertEqual(NewsletterSubscriber.objects.count(), 1)

    def test_admin_panel_requires_superuser_not_staff_flag(self):
        staff_user = User.objects.create_user(
            username="staff_without_admin_role",
            is_staff=True,
        )
        self.client.force_login(staff_user)
        denied = self.client.get("/admin-panel/")
        self.assertEqual(denied.status_code, 403)

        admin = User.objects.create_superuser(
            username="actual_platform_admin",
            email="admin@example.com",
            password="strong-admin-password",
        )
        self.client.force_login(admin)
        allowed = self.client.get("/admin-panel/")
        self.assertEqual(allowed.status_code, 200)

    def test_create_admin_does_not_create_account_without_password(self):
        env = {
            "DJANGO_ADMIN_USER": "admin_without_password",
            "DJANGO_ADMIN_PASSWORD": "",
        }
        with patch.dict(os.environ, env, clear=False):
            with self.assertRaises(CommandError):
                call_command("create_admin")
        self.assertFalse(User.objects.filter(username=env["DJANGO_ADMIN_USER"]).exists())

    def test_create_admin_does_not_promote_user_without_password(self):
        username = "ordinary_admin_name"
        User.objects.create_user(username=username)
        env = {
            "DJANGO_ADMIN_USER": username,
            "DJANGO_ADMIN_PASSWORD": "",
        }
        with patch.dict(os.environ, env, clear=False):
            with self.assertRaises(CommandError):
                call_command("create_admin")
        self.assertFalse(User.objects.get(username=username).is_superuser)

    def test_hybrid_analytics_authenticated(self):
        user = User.objects.create_user(username="buyer1", password="pass123")
        self.client.force_login(user)
        response = self.client.get("/api/v1/analytics/hybrid/?days=7")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["window_days"], 7)
        self.assertIn("orders_total", body)
        self.assertIn("rfq_total", body)

    def test_hybrid_funnel_authenticated(self):
        user = User.objects.create_user(username="buyer2", password="pass123")
        self.client.force_login(user)
        response = self.client.get("/api/v1/analytics/funnel/?days=14")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["window_days"], 14)
        self.assertIn("funnel", body)
        self.assertIn("conversion", body)

    def test_public_part_api_hides_supplier_identity_and_inactive_parts(self):
        seller = User.objects.create_user(username="private_supplier")
        UserProfile.objects.create(user=seller, role="seller")
        category = Category.objects.create(name="Public API", slug="public-api")
        part = Part.objects.create(
            seller=seller,
            category=category,
            title="Eligible part",
            slug="eligible-public-part",
            oem_number="PUBLIC-001",
            price=Decimal("100.00"),
            currency="USD",
            incoterm="EXW",
            moq=1,
            gross_weight_kg=Decimal("1.00"),
            length_cm=Decimal("10.00"),
            width_cm=Decimal("10.00"),
            height_cm=Decimal("10.00"),
            country_of_origin="CN",
            stock_quantity=1,
            is_active=True,
        )

        response = self.client.get(f"/api/v1/parts/{part.id}/")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("seller_username", response.json())

        part.is_active = False
        part.save(update_fields=["is_active"])
        hidden = self.client.get(f"/api/v1/parts/{part.id}/")
        self.assertEqual(hidden.status_code, 404)

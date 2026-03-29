from django.contrib.auth.models import User
from django.test import TestCase


class HybridApiTests(TestCase):
    def test_health_endpoint(self):
        response = self.client.get("/api/v1/health/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json().get("ok"))

    def test_readiness_endpoint(self):
        response = self.client.get("/api/v1/readiness/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json().get("ok"))

    def test_hybrid_analytics_requires_auth(self):
        response = self.client.get("/api/v1/analytics/hybrid/")
        self.assertIn(response.status_code, {401, 403})

    def test_hybrid_analytics_authenticated(self):
        user = User.objects.create_user(username="buyer1", password="pass123")
        self.client.login(username="buyer1", password="pass123")
        response = self.client.get("/api/v1/analytics/hybrid/?days=7")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["window_days"], 7)
        self.assertIn("orders_total", body)
        self.assertIn("rfq_total", body)

    def test_hybrid_funnel_authenticated(self):
        user = User.objects.create_user(username="buyer2", password="pass123")
        self.client.login(username="buyer2", password="pass123")
        response = self.client.get("/api/v1/analytics/funnel/?days=14")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["window_days"], 14)
        self.assertIn("funnel", body)
        self.assertIn("conversion", body)

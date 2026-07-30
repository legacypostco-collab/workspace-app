from django.test import TestCase, override_settings

from marketplace.models import NewsletterSubscriber


class PublicApiSecurityTests(TestCase):
    def test_newsletter_response_does_not_reveal_existing_address(self):
        first = self.client.post(
            "/api/v1/newsletter/subscribe/",
            {"email": "subscriber@example.com"},
            content_type="application/json",
        )
        second = self.client.post(
            "/api/v1/newsletter/subscribe/",
            {"email": "subscriber@example.com"},
            content_type="application/json",
        )

        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 202)
        self.assertEqual(first.json(), {"ok": True})
        self.assertEqual(second.json(), {"ok": True})
        self.assertEqual(
            NewsletterSubscriber.objects.filter(
                email="subscriber@example.com"
            ).count(),
            1,
        )

    @override_settings(HEALTHCHECK_TOKEN="health-secret")
    def test_readiness_details_require_the_exact_token(self):
        generic = self.client.get(
            "/api/v1/readiness/",
            HTTP_X_HEALTHCHECK_TOKEN="health-secrex",
        )
        detailed = self.client.get(
            "/api/v1/readiness/",
            HTTP_X_HEALTHCHECK_TOKEN="health-secret",
        )

        self.assertEqual(generic.status_code, 200)
        self.assertNotIn("checks", generic.json())
        self.assertEqual(detailed.status_code, 200)
        self.assertEqual(detailed.json()["checks"], {"database": True})

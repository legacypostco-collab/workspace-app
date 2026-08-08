from django.test import TestCase, override_settings


class CookieConsentTests(TestCase):
    @override_settings(
        ANALYTICS_ENABLED=False,
        COOKIE_CONSENT_VERSION="COOKIE-TEST-1",
    )
    def test_banner_does_not_request_unconfigured_analytics(self):
        response = self.client.get("/cookies/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-consent-version="COOKIE-TEST-1"')
        self.assertContains(response, 'data-analytics-enabled="false"')
        self.assertNotContains(response, 'data-cookie-action="accept"')
        self.assertNotContains(response, 'id="cookie-analytics"')

from io import StringIO

from django.core.management import call_command
from django.test import SimpleTestCase, override_settings


class DeployReadinessSecurityTests(SimpleTestCase):
    @override_settings(
        DEBUG=False,
        ALLOWED_HOSTS=["*"],
        CSRF_TRUSTED_ORIGINS=["http://example.com"],
        SESSION_COOKIE_SECURE=True,
        CSRF_COOKIE_SECURE=True,
        SECURE_HSTS_SECONDS=0,
        ENABLE_VIRUS_SCAN=False,
        VIRUS_SCAN_REQUIRED=True,
        EMAIL_HOST="smtp.example.com",
        EMAIL_USE_TLS=False,
        EMAIL_USE_SSL=False,
        MAX_IMPORT_ROWS=10001,
        MAX_QUOTE_ITEMS=501,
        SITE_URL="http://example.com",
        CACHES={
            "default": {
                "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            },
        },
    )
    def test_insecure_runtime_settings_are_blocking_errors(self):
        output = StringIO()

        with self.assertRaises(SystemExit):
            call_command("check_deploy_readiness", stdout=output)

        report = output.getvalue()
        self.assertIn("ALLOWED_HOSTS must not contain a wildcard", report)
        self.assertIn(
            "CSRF_TRUSTED_ORIGINS must contain only HTTPS origins",
            report,
        )
        self.assertIn("SECURE_HSTS_SECONDS must be greater than zero", report)
        self.assertIn("ENABLE_VIRUS_SCAN must be enabled", report)
        self.assertIn("SMTP transport encryption must be enabled", report)
        self.assertIn("MAX_IMPORT_ROWS must not exceed 10000", report)
        self.assertIn("MAX_QUOTE_ITEMS must not exceed 500", report)
        self.assertIn("SITE_URL must use HTTPS", report)
        self.assertIn("default cache must use Redis", report)

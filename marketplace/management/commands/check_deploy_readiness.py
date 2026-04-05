from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Validate deployment readiness for production-like environments."

    def add_arguments(self, parser):
        parser.add_argument(
            "--allow-no-tls",
            action="store_true",
            help="Allow insecure HTTP mode (for internal/staging environments).",
        )

    def handle(self, *args, **options):
        allow_no_tls = bool(options.get("allow_no_tls"))
        errors: list[str] = []
        warnings: list[str] = []

        if settings.DEBUG:
            errors.append("DEBUG must be disabled in production.")

        secret = str(getattr(settings, "SECRET_KEY", "") or "")
        if not secret or "dev-secret" in secret or "django-insecure" in secret:
            errors.append("SECRET_KEY must be set to a strong non-default value.")

        allowed_hosts = list(getattr(settings, "ALLOWED_HOSTS", []) or [])
        if not allowed_hosts:
            errors.append("ALLOWED_HOSTS is empty.")

        if not allow_no_tls:
            if not bool(getattr(settings, "SESSION_COOKIE_SECURE", False)):
                errors.append("SESSION_COOKIE_SECURE must be enabled.")
            if not bool(getattr(settings, "CSRF_COOKIE_SECURE", False)):
                errors.append("CSRF_COOKIE_SECURE must be enabled.")
            # SECURE_SSL_REDIRECT should be False when behind nginx proxy (nginx does redirect)
            behind_proxy = bool(getattr(settings, "SECURE_PROXY_SSL_HEADER", None))
            if not behind_proxy and not bool(getattr(settings, "SECURE_SSL_REDIRECT", False)):
                warnings.append("SECURE_SSL_REDIRECT is False and BEHIND_PROXY is not set — ensure TLS termination happens elsewhere.")
            if int(getattr(settings, "SECURE_HSTS_SECONDS", 0) or 0) <= 0:
                warnings.append("SECURE_HSTS_SECONDS is 0 — consider enabling HSTS once TLS is confirmed stable.")
        else:
            warnings.append("TLS checks skipped due to --allow-no-tls.")

        # Database engine check
        db_engine = str(settings.DATABASES.get("default", {}).get("ENGINE", ""))
        if "sqlite" in db_engine:
            warnings.append("Using SQLite — switch to PostgreSQL for production (set DB_ENGINE).")

        # Email configuration
        email_host = str(getattr(settings, "EMAIL_HOST", "") or "")
        if not email_host:
            warnings.append("EMAIL_HOST is not set — email verification and notifications are disabled.")

        # Admin password via env
        import os
        if not os.getenv("DJANGO_ADMIN_PASSWORD"):
            warnings.append("DJANGO_ADMIN_PASSWORD not set — admin account has no password until set manually.")

        webhook_secret = str(getattr(settings, "WEBHOOK_SECRET", "") or "")
        if not webhook_secret:
            warnings.append("WEBHOOK_SECRET is empty; webhook authenticity checks are weaker.")

        payment_url = str(getattr(settings, "PAYMENT_PROVIDER_URL", "") or "")
        if not payment_url:
            warnings.append("PAYMENT_PROVIDER_URL not set — payment gateway is not configured.")

        if int(getattr(settings, "MAX_IMPORT_ROWS", 0) or 0) > 10000:
            warnings.append("MAX_IMPORT_ROWS is high; consider <= 10000 for safer defaults.")
        if int(getattr(settings, "MAX_QUOTE_ITEMS", 0) or 0) > 500:
            warnings.append("MAX_QUOTE_ITEMS is high; consider <= 500.")

        if errors:
            self.stdout.write(self.style.ERROR("Deploy readiness: FAILED"))
            for item in errors:
                self.stdout.write(self.style.ERROR(f" - {item}"))
            for item in warnings:
                self.stdout.write(self.style.WARNING(f" - {item}"))
            raise SystemExit(1)

        self.stdout.write(self.style.SUCCESS("Deploy readiness: OK"))
        for item in warnings:
            self.stdout.write(self.style.WARNING(f" - {item}"))

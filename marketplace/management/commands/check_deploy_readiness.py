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
            if not bool(getattr(settings, "SECURE_SSL_REDIRECT", False)):
                errors.append("SECURE_SSL_REDIRECT should be enabled.")
            if int(getattr(settings, "SECURE_HSTS_SECONDS", 0) or 0) <= 0:
                errors.append("SECURE_HSTS_SECONDS should be > 0.")
        else:
            warnings.append("TLS checks skipped due to --allow-no-tls.")

        webhook_secret = str(getattr(settings, "WEBHOOK_SECRET", "") or "")
        if not webhook_secret:
            warnings.append("WEBHOOK_SECRET is empty; webhook authenticity checks are weaker.")

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

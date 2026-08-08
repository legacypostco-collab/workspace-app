from __future__ import annotations

import os
from urllib.parse import urlparse

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
        if (
            len(secret) < 50
            or "dev-secret" in secret
            or "django-insecure" in secret
            or "CHANGE_ME" in secret
        ):
            errors.append("SECRET_KEY must be set to a strong non-default value.")
        qr_secret = str(getattr(settings, "QR_SECRET", "") or "")
        if len(qr_secret) < 32 or "CHANGE_ME" in qr_secret or "dev-only" in qr_secret:
            errors.append("QR_SECRET must be set to a strong non-default value.")
        payment_callback_secret = str(
            getattr(settings, "PAYMENT_CALLBACK_SECRET", "") or ""
        )
        if len(payment_callback_secret) < 32 or "CHANGE_ME" in payment_callback_secret:
            errors.append(
                "PAYMENT_CALLBACK_SECRET must be set to a strong non-default value."
            )

        allowed_hosts = list(getattr(settings, "ALLOWED_HOSTS", []) or [])
        if not allowed_hosts:
            errors.append("ALLOWED_HOSTS is empty.")
        elif "*" in allowed_hosts:
            errors.append("ALLOWED_HOSTS must not contain a wildcard.")
        csrf_origins = list(
            getattr(settings, "CSRF_TRUSTED_ORIGINS", []) or []
        )
        if not csrf_origins:
            errors.append("CSRF_TRUSTED_ORIGINS is empty.")
        elif not allow_no_tls and any(
            not str(origin).startswith("https://")
            for origin in csrf_origins
        ):
            errors.append(
                "CSRF_TRUSTED_ORIGINS must contain only HTTPS origins."
            )

        site_url = str(getattr(settings, "SITE_URL", "") or "").strip()
        parsed_site = urlparse(site_url)
        if not site_url:
            errors.append("SITE_URL is required for email and invitation links.")
        elif (
            not parsed_site.scheme
            or not parsed_site.hostname
            or parsed_site.username
            or parsed_site.password
            or parsed_site.query
            or parsed_site.fragment
            or parsed_site.path not in {"", "/"}
        ):
            errors.append("SITE_URL must be a clean absolute origin.")
        elif not allow_no_tls and parsed_site.scheme != "https":
            errors.append("SITE_URL must use HTTPS.")

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
                errors.append("SECURE_HSTS_SECONDS must be greater than zero.")
        else:
            warnings.append("TLS checks skipped due to --allow-no-tls.")

        # Database engine check
        db_engine = str(settings.DATABASES.get("default", {}).get("ENGINE", ""))
        if "sqlite" in db_engine:
            errors.append("Using SQLite is not allowed for production.")

        cache_backend = str(
            settings.CACHES.get("default", {}).get("BACKEND", "")
        ).lower()
        if "redis" not in cache_backend:
            errors.append(
                "The default cache must use Redis so rate limits and locks are "
                "shared by every application process."
            )

        # Email configuration
        email_host = str(getattr(settings, "EMAIL_HOST", "") or "")
        if not email_host and bool(
            getattr(settings, "EMAIL_VERIFICATION_REQUIRED", False)
        ):
            errors.append(
                "EMAIL_HOST is required while email verification is enabled."
            )
        elif not email_host:
            warnings.append(
                "EMAIL_HOST is not set; email verification and notifications are disabled."
            )
        elif not (
            bool(getattr(settings, "EMAIL_USE_TLS", False))
            or bool(getattr(settings, "EMAIL_USE_SSL", False))
        ):
            errors.append(
                "SMTP transport encryption must be enabled with EMAIL_USE_TLS "
                "or EMAIL_USE_SSL."
            )
        elif (
            bool(getattr(settings, "EMAIL_USE_TLS", False))
            and bool(getattr(settings, "EMAIL_USE_SSL", False))
        ):
            errors.append(
                "EMAIL_USE_TLS and EMAIL_USE_SSL cannot both be enabled."
            )

        # Admin password via env
        if not (
            os.getenv("DJANGO_ADMIN_PASSWORD")
            or os.getenv("DJANGO_ADMIN_PASSWORD_FILE")
        ):
            warnings.append(
                "DJANGO_ADMIN_PASSWORD not set; automatic administrator provisioning "
                "is unavailable."
            )

        if not getattr(settings, "ANTHROPIC_API_KEY", ""):
            message = (
                "ANTHROPIC_API_KEY is not set; free-form chat uses the limited "
                "deterministic fallback."
            )
            (errors if getattr(settings, "LLM_REQUIRED", False) else warnings).append(message)

        if not getattr(settings, "KONTUR_FOCUS_API_KEY", ""):
            message = (
                "KONTUR_FOCUS_API_KEY is not set; company checks require manual review."
            )
            (errors if getattr(settings, "KYB_EXTERNAL_REQUIRED", False) else warnings).append(message)

        monitoring_values = {
            "SENTRY_DSN": getattr(settings, "SENTRY_DSN", ""),
            "UPTIME_HEARTBEAT_URL": getattr(settings, "UPTIME_HEARTBEAT_URL", ""),
        }
        has_alert_channel = bool(
            getattr(settings, "MONITOR_WEBHOOK_URL", "")
            or (
                getattr(settings, "MONITOR_TELEGRAM_BOT_TOKEN", "")
                and getattr(settings, "MONITOR_TELEGRAM_CHAT_ID", "")
            )
        )
        missing_monitoring = [name for name, value in monitoring_values.items() if not value]
        if not has_alert_channel:
            missing_monitoring.append("MONITOR_ALERT_CHANNEL")
        if not getattr(settings, "MONITOR_CONTROLLER_ENABLED", False):
            missing_monitoring.append("MONITOR_CONTROLLER_ENABLED")
        if missing_monitoring:
            message = "Monitoring is incomplete: " + ", ".join(missing_monitoring) + "."
            (errors if getattr(settings, "MONITORING_REQUIRED", False) else warnings).append(message)
        monitoring_urls = {
            **monitoring_values,
            "UPTIME_HEARTBEAT_FAIL_URL": getattr(
                settings, "UPTIME_HEARTBEAT_FAIL_URL", ""
            ),
            "MONITOR_WEBHOOK_URL": getattr(settings, "MONITOR_WEBHOOK_URL", ""),
        }
        for name, value in monitoring_urls.items():
            parsed = urlparse(value) if value else None
            if value and (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
            ):
                errors.append(f"{name} must be an HTTPS URL without credentials.")

        webhook_secret = str(getattr(settings, "WEBHOOK_SECRET", "") or "")
        if getattr(settings, "WEBHOOK_ENDPOINTS", "") and len(webhook_secret) < 32:
            errors.append(
                "WEBHOOK_SECRET must contain at least 32 characters when outgoing webhooks are enabled."
            )
        if (
            getattr(settings, "WEBHOOK_ENDPOINTS", "")
            and not getattr(settings, "WEBHOOK_ALLOWED_HOSTS", "")
        ):
            errors.append(
                "WEBHOOK_ALLOWED_HOSTS is required when outgoing webhooks are enabled."
            )

        if not bool(getattr(settings, "ENABLE_VIRUS_SCAN", False)):
            errors.append("ENABLE_VIRUS_SCAN must be enabled in production.")
        if not bool(getattr(settings, "VIRUS_SCAN_REQUIRED", False)):
            errors.append("VIRUS_SCAN_REQUIRED must be enabled in production.")

        if os.getenv("PAYMENT_ENGINE", "wallet").strip().lower() == "stripe":
            errors.append(
                "PAYMENT_ENGINE=stripe is not production-ready: refunds and payment "
                "reconciliation are incomplete."
            )

        settlement_mode = str(
            getattr(settings, "SETTLEMENT_MODE", "invoice_contract") or ""
        ).strip().lower()
        if settlement_mode != "invoice_contract":
            errors.append("SETTLEMENT_MODE must be invoice_contract in production.")
        if bool(getattr(settings, "LEGACY_WALLET_UI_ENABLED", False)):
            errors.append("LEGACY_WALLET_UI_ENABLED must be disabled in production.")
        settlement_required = bool(
            getattr(settings, "SETTLEMENT_REQUIRED", False)
        )
        if not settlement_required:
            errors.append("SETTLEMENT_REQUIRED must be enabled in production.")
        else:
            from assistant.settlements import platform_snapshot

            platform = platform_snapshot()
            required_settlement_values = {
                "PLATFORM_LEGAL_NAME": platform["legal_name"],
                "PLATFORM_LEGAL_ADDRESS": platform["address"],
                "PLATFORM_TAX_ID": platform["tax_id"],
                "PLATFORM_REGISTRATION_NO": platform["registration_no"],
                "PLATFORM_BANK_NAME": platform["bank_name"],
                "PLATFORM_BANK_ACCOUNT": platform["bank_account"],
                "PLATFORM_BANK_SWIFT": platform["bank_swift"],
                "PLATFORM_SIGNATORY": platform["signatory"],
            }
            missing = [
                name for name, value in required_settlement_values.items()
                if not str(value or "").strip()
                or str(value).strip().upper() == "__CHANGE_ME__"
            ]
            if missing:
                errors.append(
                    "Settlement legal and bank details are incomplete: "
                    + ", ".join(missing)
                )

        payment_url = str(getattr(settings, "PAYMENT_PROVIDER_URL", "") or "")
        if settlement_mode != "invoice_contract" and not payment_url:
            warnings.append("PAYMENT_PROVIDER_URL not set — payment gateway is not configured.")

        if int(getattr(settings, "MAX_IMPORT_ROWS", 0) or 0) > 10000:
            errors.append("MAX_IMPORT_ROWS must not exceed 10000.")
        if int(getattr(settings, "MAX_QUOTE_ITEMS", 0) or 0) > 500:
            errors.append("MAX_QUOTE_ITEMS must not exceed 500.")

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

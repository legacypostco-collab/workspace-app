"""Send daily email digest of unread notifications.

Usage:
  python manage.py send_email_digest [--window-hours=24] [--dry-run]

Prod: запускается из cron / Celery beat ежедневно в 8:00 локального
времени получателя (или единое UTC-время).

Что делает:
  1. Берёт всех User'ов с включённым notif_email_enabled и непустым email
  2. Для каждого ищет непрочитанные Notification за последние N часов
  3. Если есть — шлёт сводку через assistant.channels.send_digest
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.utils import timezone

from assistant.channels import send_digest


class Command(BaseCommand):
    help = "Send daily email digest of unread notifications to opted-in users"

    def add_arguments(self, parser):
        parser.add_argument("--window-hours", type=int, default=24,
                            help="Lookback window for unread notifications (hrs)")
        parser.add_argument("--dry-run", action="store_true",
                            help="Don't send, just count")

    def handle(self, *args, **options):
        import os
        os.environ["DIGEST_WINDOW_HOURS"] = str(options["window_hours"])

        User = get_user_model()
        from marketplace.models import UserProfile

        eligible = User.objects.filter(
            is_active=True, email__isnull=False,
        ).exclude(email="").select_related("profile")

        sent = 0
        skipped = 0
        for user in eligible:
            profile = getattr(user, "profile", None)
            if profile and not profile.notif_email_enabled:
                skipped += 1
                continue
            if options["dry_run"]:
                self.stdout.write(f"  would send to {user.username} ({user.email})")
                sent += 1
                continue
            ok = send_digest(user)
            if ok:
                sent += 1
                self.stdout.write(self.style.SUCCESS(f"  → {user.username} ({user.email})"))
            else:
                skipped += 1

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            f"Digest run · sent {sent}, skipped {skipped}"
            + (" (DRY RUN)" if options["dry_run"] else "")
        ))

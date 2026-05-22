"""ТЗ §8: записать `no_response` rating events для продавцов, которые не ответили
на RFQ-уведомление в нормативный срок (по умолчанию 24 часа).

Запуск:
  python manage.py detect_no_response [--threshold-hours=24] [--dry-run]

Cron / Celery beat: раз в час.

Идемпотентность: повторный запуск не дублирует events — отслеживаем через
SupplierRatingEvent.meta.notification_id (уникально на нотификацию).
"""
from __future__ import annotations

import re
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone


def _extract_rfq_id(url: str) -> int | None:
    """Достать RFQ id из url notification: /chat/rfq/<id>/?source=invite."""
    if not url:
        return None
    m = re.search(r"/chat/rfq/(\d+)/?", url)
    return int(m.group(1)) if m else None


class Command(BaseCommand):
    help = "Detect sellers who didn't respond to RFQ notifications in time, record rating events"

    def add_arguments(self, parser):
        parser.add_argument("--threshold-hours", type=int, default=24,
                            help="Считать «не ответил» если прошло >X часов с момента нотификации")
        parser.add_argument("--dry-run", action="store_true",
                            help="Только показать кандидатов, не писать events")

    def handle(self, *args, **options):
        from assistant.rating import record_rating_event
        from marketplace.models import Notification, Quote, SupplierRatingEvent

        threshold = options["threshold_hours"]
        cutoff = timezone.now() - timedelta(hours=threshold)
        dry = options["dry_run"]

        # RFQ-нотификации старше threshold
        rfq_notifs = Notification.objects.filter(
            kind="rfq", created_at__lte=cutoff,
        ).select_related("user")

        # Уже записанные no_response events (по notification_id) — для idempotency
        already_recorded = set(
            SupplierRatingEvent.objects.filter(event_type="no_response")
            .exclude(meta__notification_id__isnull=True)
            .values_list("meta__notification_id", flat=True)
        )

        recorded = 0
        skipped_already = 0
        skipped_responded = 0

        for n in rfq_notifs:
            if n.id in already_recorded:
                skipped_already += 1
                continue
            rfq_id = _extract_rfq_id(n.url)
            if not rfq_id:
                continue
            # Если seller отправил Quote по этому RFQ — он ответил
            responded = Quote.objects.filter(rfq_id=rfq_id, seller=n.user).exists()
            if responded:
                skipped_responded += 1
                continue

            # Записываем no_response event
            if not dry:
                record_rating_event(
                    n.user, event_type="no_response",
                    meta={
                        "rfq_id": rfq_id,
                        "notification_id": n.id,
                        "hours_passed": int((timezone.now() - n.created_at).total_seconds() / 3600),
                    },
                )
            recorded += 1
            self.stdout.write(
                self.style.WARNING(
                    f"  → {n.user.username} · RFQ #{rfq_id} · "
                    f"{int((timezone.now() - n.created_at).total_seconds() / 3600)}h без ответа"
                )
            )

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(
            f"Detect no_response · recorded={recorded} · "
            f"skipped already={skipped_already} responded={skipped_responded}"
            + (" (DRY RUN)" if dry else "")
        ))

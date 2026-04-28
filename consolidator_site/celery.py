"""Celery application bootstrap.

Run worker:    celery -A consolidator_site worker -l info
Run beat:      celery -A consolidator_site beat -l info
Run flower:    celery -A consolidator_site flower
"""
from __future__ import annotations

import os

from celery import Celery
from celery.schedules import crontab

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "consolidator_site.settings")

app = Celery("consolidator_site")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

# Periodic tasks (override in admin via django-celery-beat for runtime changes)
app.conf.beat_schedule = {
    "check-sla-breaches-every-15min": {
        "task": "marketplace.tasks.check_sla_breaches",
        "schedule": crontab(minute="*/15"),
    },
    "send-pending-notifications-every-minute": {
        "task": "marketplace.tasks.send_pending_email_notifications",
        "schedule": crontab(minute="*"),
    },
    "cleanup-expired-tokens-daily": {
        "task": "marketplace.tasks.cleanup_expired_tokens",
        "schedule": crontab(hour=3, minute=0),
    },
}


@app.task(bind=True)
def debug_task(self):
    print(f"Request: {self.request!r}")

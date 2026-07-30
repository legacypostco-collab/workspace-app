"""Celery application bootstrap.

Run worker:    celery -A consolidator_site worker -l info
Run beat:      celery -A consolidator_site beat -l info
Run flower:    celery -A consolidator_site flower
"""
from __future__ import annotations

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "consolidator_site.settings")

app = Celery("consolidator_site")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

@app.task(bind=True)
def debug_task(self):
    print(f"Request: {self.request!r}")

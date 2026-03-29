from __future__ import annotations

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "consolidator_site.settings")

app = Celery("consolidator_site")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()

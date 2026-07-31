from __future__ import annotations

from datetime import timedelta

from django.core.cache import cache
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.utils import timezone


class Command(BaseCommand):
    help = "Check database, cache, Celery workers, and recent background-task failures."

    def add_arguments(self, parser):
        parser.add_argument("--require-worker", action="store_true")
        parser.add_argument("--max-failed-tasks", type=int, default=20)

    def handle(self, *args, **options):
        failures = []
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            if cursor.fetchone() != (1,):
                failures.append("database roundtrip failed")

        marker = timezone.now().isoformat()
        cache.set("__operations_check__", marker, 30)
        if cache.get("__operations_check__") != marker:
            failures.append("cache roundtrip failed")

        worker_count = 0
        try:
            from consolidator_site.celery import app

            replies = app.control.inspect(timeout=3).ping() or {}
            worker_count = len(replies)
        except Exception as exc:
            if options["require_worker"]:
                failures.append(f"Celery inspection failed: {exc.__class__.__name__}")
        if options["require_worker"] and worker_count == 0:
            failures.append("no Celery worker answered ping")

        try:
            from django_celery_results.models import TaskResult

            recent_failures = TaskResult.objects.filter(
                status="FAILURE",
                date_done__gte=timezone.now() - timedelta(hours=24),
            ).count()
        except Exception as exc:
            failures.append(f"task result check failed: {exc.__class__.__name__}")
            recent_failures = -1

        maximum = max(0, options["max_failed_tasks"])
        if recent_failures > maximum:
            failures.append(
                f"{recent_failures} failed background tasks in 24h; limit is {maximum}"
            )

        if failures:
            raise CommandError("; ".join(failures))
        self.stdout.write(self.style.SUCCESS(
            f"Operations check OK: workers={worker_count}, failed_tasks_24h={recent_failures}"
        ))

from __future__ import annotations

import json
from datetime import timedelta

from django.core.cache import cache
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.utils import timezone


class Command(BaseCommand):
    help = "Check database, cache, Celery workers, and recent background-task failures."

    def add_arguments(self, parser):
        parser.add_argument("--require-worker", action="store_true")
        parser.add_argument("--require-heartbeat", action="store_true")
        parser.add_argument("--heartbeat-max-age", type=int, default=180)
        parser.add_argument("--max-failed-tasks", type=int, default=20)
        parser.add_argument("--http-window-minutes", type=int, default=5)
        parser.add_argument("--min-http-requests", type=int, default=20)
        parser.add_argument("--max-http-5xx", type=int, default=10)
        parser.add_argument("--max-http-5xx-rate", type=float, default=0.05)
        parser.add_argument("--max-queue-depth", type=int, default=100)
        parser.add_argument("--max-db-connection-percent", type=float, default=85.0)
        parser.add_argument("--json", action="store_true", dest="as_json")

    def handle(self, *args, **options):
        failures: list[str] = []
        metrics: dict[str, int | float] = {}
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            if cursor.fetchone() != (1,):
                failures.append("database roundtrip failed")

        if connection.vendor == "postgresql":
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT count(*), current_setting('max_connections')::int "
                        "FROM pg_stat_activity"
                    )
                    used_connections, max_connections = cursor.fetchone()
                metrics["db_connections"] = int(used_connections)
                metrics["db_max_connections"] = int(max_connections)
                connection_percent = (
                    float(used_connections) * 100.0 / float(max_connections)
                    if max_connections else 100.0
                )
                metrics["db_connection_percent"] = round(connection_percent, 2)
                if connection_percent > max(
                    1.0, float(options["max_db_connection_percent"])
                ):
                    failures.append(
                        f"database connections at {connection_percent:.1f}%"
                    )
            except Exception as exc:
                failures.append(
                    f"database connection utilization check failed: {exc.__class__.__name__}"
                )

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
        metrics["workers"] = worker_count

        queue_depth = -1
        try:
            import redis
            from django.conf import settings

            broker_url = str(getattr(settings, "CELERY_BROKER_URL", "") or "")
            if broker_url.startswith(("redis://", "rediss://")):
                broker = redis.Redis.from_url(
                    broker_url,
                    socket_connect_timeout=2,
                    socket_timeout=2,
                )
                queue_depth = int(broker.llen("celery"))
        except Exception as exc:
            if options["require_worker"]:
                failures.append(f"queue depth check failed: {exc.__class__.__name__}")
        metrics["queue_depth"] = queue_depth
        if queue_depth > max(0, int(options["max_queue_depth"])):
            failures.append(
                f"Celery queue depth is {queue_depth}; limit is {options['max_queue_depth']}"
            )

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
        metrics["failed_tasks_24h"] = recent_failures

        heartbeat_age = -1
        heartbeat = cache.get("operations:celery_heartbeat")
        if heartbeat:
            try:
                heartbeat_age = max(
                    0,
                    int(timezone.now().timestamp() - float(heartbeat)),
                )
            except (TypeError, ValueError):
                heartbeat_age = -1
        metrics["heartbeat_age_seconds"] = heartbeat_age
        if options["require_heartbeat"] and (
            heartbeat_age < 0
            or heartbeat_age > max(30, int(options["heartbeat_max_age"]))
        ):
            failures.append(
                "Celery beat heartbeat is missing or stale"
                if heartbeat_age < 0
                else f"Celery beat heartbeat is {heartbeat_age}s old"
            )

        from marketplace.services.observability import http_window

        request_window = http_window(options["http_window_minutes"])
        metrics.update({f"http_{key}": value for key, value in request_window.items()})
        request_count = int(request_window["total"])
        if request_count >= max(1, int(options["min_http_requests"])):
            error_count = int(request_window["status_5xx"])
            error_rate = float(request_window["error_rate"])
            if error_count > max(0, int(options["max_http_5xx"])):
                failures.append(
                    f"HTTP 5xx count is {error_count} in the monitoring window"
                )
            if error_rate > max(0.0, float(options["max_http_5xx_rate"])):
                failures.append(
                    f"HTTP 5xx rate is {error_rate:.1%} in the monitoring window"
                )

        payload = {"ok": not failures, "metrics": metrics, "failures": failures}
        if options["as_json"]:
            self.stdout.write(json.dumps(payload, ensure_ascii=True, sort_keys=True))
        if failures:
            raise CommandError("; ".join(failures))
        if not options["as_json"]:
            self.stdout.write(self.style.SUCCESS(
                "Operations check OK: "
                f"workers={worker_count}, queue_depth={queue_depth}, "
                f"heartbeat_age={heartbeat_age}s, "
                f"failed_tasks_24h={recent_failures}, "
                f"http_5xx={request_window['status_5xx']}/{request_count}"
            ))

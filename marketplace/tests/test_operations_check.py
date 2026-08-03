from io import StringIO
from unittest.mock import Mock, patch

from django.core.cache import cache
from django.core.management import CommandError, call_command
from django.test import TestCase
from django.utils import timezone


class OperationsCheckTests(TestCase):
    def setUp(self):
        cache.clear()

    @patch("consolidator_site.celery.app.control.inspect")
    @patch("redis.Redis.from_url")
    def test_database_cache_worker_and_failures_are_checked(self, redis_from_url, inspect):
        inspector = Mock()
        inspector.ping.return_value = {"celery@test": {"ok": "pong"}}
        inspect.return_value = inspector
        redis_from_url.return_value.llen.return_value = 0
        output = StringIO()

        call_command(
            "check_operations",
            require_worker=True,
            max_failed_tasks=0,
            stdout=output,
        )

        self.assertIn("workers=1", output.getvalue())
        self.assertIn("failed_tasks_24h=0", output.getvalue())

    @patch("consolidator_site.celery.app.control.inspect")
    @patch("redis.Redis.from_url")
    def test_missing_required_worker_fails_the_check(self, redis_from_url, inspect):
        inspector = Mock()
        inspector.ping.return_value = {}
        inspect.return_value = inspector
        redis_from_url.return_value.llen.return_value = 0

        with self.assertRaisesRegex(CommandError, "no Celery worker"):
            call_command("check_operations", require_worker=True)

    @patch("consolidator_site.celery.app.control.inspect")
    @patch("redis.Redis.from_url")
    def test_required_stale_heartbeat_fails(self, redis_from_url, inspect):
        inspect.return_value.ping.return_value = {"celery@test": {"ok": "pong"}}
        redis_from_url.return_value.llen.return_value = 0
        cache.set(
            "operations:celery_heartbeat",
            timezone.now().timestamp() - 600,
            timeout=60,
        )

        with self.assertRaisesRegex(CommandError, "heartbeat is 600s old"):
            call_command(
                "check_operations",
                require_worker=True,
                require_heartbeat=True,
                heartbeat_max_age=180,
            )

    @patch("consolidator_site.celery.app.control.inspect")
    @patch("redis.Redis.from_url")
    def test_http_error_threshold_is_checked(self, redis_from_url, inspect):
        from marketplace.services.observability import record_http_request

        inspect.return_value.ping.return_value = {"celery@test": {"ok": "pong"}}
        redis_from_url.return_value.llen.return_value = 0
        for _ in range(20):
            record_http_request(500, 10)

        with self.assertRaisesRegex(CommandError, "HTTP 5xx"):
            call_command(
                "check_operations",
                min_http_requests=20,
                max_http_5xx=10,
                max_http_5xx_rate=0.05,
            )

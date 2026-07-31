from io import StringIO
from unittest.mock import Mock, patch

from django.core.management import CommandError, call_command
from django.test import TestCase


class OperationsCheckTests(TestCase):
    @patch("consolidator_site.celery.app.control.inspect")
    def test_database_cache_worker_and_failures_are_checked(self, inspect):
        inspector = Mock()
        inspector.ping.return_value = {"celery@test": {"ok": "pong"}}
        inspect.return_value = inspector
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
    def test_missing_required_worker_fails_the_check(self, inspect):
        inspector = Mock()
        inspector.ping.return_value = {}
        inspect.return_value = inspector

        with self.assertRaisesRegex(CommandError, "no Celery worker"):
            call_command("check_operations", require_worker=True)

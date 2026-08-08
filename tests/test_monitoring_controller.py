import json
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock, patch

from deploy.monitoring_controller import CheckResult, Controller


class MonitoringControllerTests(TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.controller = Controller(
            app_dir=root,
            state_path=root / "status.json",
        )
        self.controller.alert_after = 2
        self.controller.autoheal = False

    def _result(self, ok=False):
        return CheckResult(
            "internal_readiness",
            ok,
            "ready" if ok else "unavailable",
            component="web",
        )

    @patch.object(Controller, "_notify_local")
    @patch.object(Controller, "_heartbeat")
    @patch.object(Controller, "notify")
    def test_alert_requires_two_failures(self, notify, heartbeat, notify_local):
        with patch.object(self.controller, "checks", return_value=[self._result()]):
            first, first_code = self.controller.run_once()
            second, second_code = self.controller.run_once()

        self.assertEqual(first_code, 1)
        self.assertEqual(second_code, 1)
        self.assertEqual(first["check_state"]["internal_readiness"]["consecutive_failures"], 1)
        self.assertEqual(second["check_state"]["internal_readiness"]["consecutive_failures"], 2)
        notify.assert_called_once()
        self.assertEqual(notify.call_args.args[0], "critical")

    @patch.object(Controller, "_notify_local")
    @patch.object(Controller, "_heartbeat")
    @patch.object(Controller, "notify")
    def test_recovery_is_reported(self, notify, heartbeat, notify_local):
        failed = self._result()
        ready = self._result(ok=True)
        with patch.object(self.controller, "checks", return_value=[failed]):
            self.controller.run_once()
            self.controller.run_once()
        notify.reset_mock()

        with patch.object(self.controller, "checks", return_value=[ready]):
            state, code = self.controller.run_once()

        self.assertEqual(code, 0)
        self.assertTrue(state["ok"])
        notify.assert_called_once()
        self.assertEqual(notify.call_args.args[0], "recovery")
        self.assertEqual(
            state["check_state"]["internal_readiness"]["last_alert_epoch"],
            0.0,
        )

    def test_autoheal_is_limited_to_application_services(self):
        self.controller.autoheal = True
        self.controller.autoheal_after = 3
        self.controller._run = Mock(return_value=Mock(returncode=0))
        entry = {"consecutive_failures": 3}

        message = self.controller._autoheal(self._result(), entry)
        database_message = self.controller._autoheal(
            CheckResult("database_backup", False, "missing", component="backup"),
            {"consecutive_failures": 3},
        )

        self.assertEqual(message, "restarted web")
        self.assertIsNone(database_message)
        self.controller._run.assert_called_once_with(
            ["docker", "compose", "restart", "web"], timeout=90
        )

    @patch.dict("os.environ", {"MONITOR_DOCKER_SERVICES": "web"})
    def test_docker_restart_counter_is_checked(self):
        self.controller._run = Mock(
            side_effect=[
                Mock(
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "Service": "web",
                            "ID": "container-id",
                            "State": "running",
                            "Health": "healthy",
                        }
                    ),
                ),
                Mock(returncode=0, stdout="/consolidator-web 4\n"),
            ]
        )

        result = self.controller.check_docker()

        self.assertFalse(result.ok)
        self.assertIn("restarted-4-times", result.summary)
        self.assertEqual(result.value, 4)

    @patch.dict("os.environ", {}, clear=True)
    def test_docker_requires_antivirus_by_default(self):
        self.controller._run = Mock(
            return_value=Mock(
                returncode=0,
                stdout=json.dumps([
                    {"Service": name, "State": "running", "Health": "healthy"}
                    for name in ("db", "redis", "web", "worker", "beat")
                ]),
            )
        )

        result = self.controller.check_docker()

        self.assertFalse(result.ok)
        self.assertIn("clamav:missing", result.summary)

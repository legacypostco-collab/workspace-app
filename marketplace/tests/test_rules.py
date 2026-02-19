from django.test import SimpleTestCase

from marketplace.rules import AutoModeInputs, can_be_executor, decide_auto_mode


class AutoModeDecisionTests(SimpleTestCase):
    def test_auto_enabled_when_all_conditions_met(self):
        decision = decide_auto_mode(
            AutoModeInputs(
                part_found=True,
                confidence=88.0,
                trusted_count=2,
                sandbox_count=1,
                fresh_data=True,
            )
        )
        self.assertTrue(decision.eligible_auto)
        self.assertEqual(decision.next_state, "auto_matched")

    def test_auto_disabled_without_trusted(self):
        decision = decide_auto_mode(
            AutoModeInputs(
                part_found=True,
                confidence=90.0,
                trusted_count=0,
                sandbox_count=3,
                fresh_data=True,
            )
        )
        self.assertFalse(decision.eligible_auto)
        self.assertEqual(decision.next_state, "needs_review")
        self.assertIn("no_trusted_supplier", decision.reason)

    def test_auto_disabled_when_offers_less_than_3(self):
        decision = decide_auto_mode(
            AutoModeInputs(
                part_found=True,
                confidence=90.0,
                trusted_count=1,
                sandbox_count=1,
                fresh_data=True,
            )
        )
        self.assertFalse(decision.eligible_auto)
        self.assertIn("offers_less_than_3", decision.reason)

    def test_auto_disabled_when_confidence_below_threshold(self):
        decision = decide_auto_mode(
            AutoModeInputs(
                part_found=True,
                confidence=65.0,
                trusted_count=2,
                sandbox_count=2,
                fresh_data=True,
            )
        )
        self.assertFalse(decision.eligible_auto)
        self.assertIn("confidence_below_threshold", decision.reason)


class ExecutorMatrixTests(SimpleTestCase):
    def test_auto_mode_only_trusted_executor(self):
        self.assertTrue(can_be_executor("auto", "trusted", has_trusted_for_position=True))
        self.assertFalse(can_be_executor("auto", "sandbox", has_trusted_for_position=True))
        self.assertFalse(can_be_executor("auto", "risky", has_trusted_for_position=True))

    def test_semi_mode_sandbox_requires_operator_confirmation(self):
        self.assertFalse(
            can_be_executor(
                "semi",
                "sandbox",
                has_trusted_for_position=False,
                operator_confirmed=False,
            )
        )
        self.assertTrue(
            can_be_executor(
                "semi",
                "sandbox",
                has_trusted_for_position=False,
                operator_confirmed=True,
            )
        )

    def test_semi_mode_risky_requires_double_confirmation(self):
        self.assertFalse(
            can_be_executor(
                "semi",
                "risky",
                has_trusted_for_position=False,
                operator_confirmed=True,
                risky_double_confirmed=False,
            )
        )
        self.assertTrue(
            can_be_executor(
                "semi",
                "risky",
                has_trusted_for_position=False,
                operator_confirmed=True,
                risky_double_confirmed=True,
            )
        )

    def test_rejected_is_always_blocked(self):
        self.assertFalse(can_be_executor("auto", "rejected", has_trusted_for_position=False))
        self.assertFalse(can_be_executor("semi", "rejected", has_trusted_for_position=False, operator_confirmed=True))
        self.assertFalse(can_be_executor("manual_oem", "rejected", has_trusted_for_position=False, operator_confirmed=True))

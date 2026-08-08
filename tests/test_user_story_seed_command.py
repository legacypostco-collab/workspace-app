from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings

from assistant.management.commands.seed_user_story_accounts import ACCOUNT_ROLES


@override_settings(DEBUG=True)
class UserStorySeedCommandTests(TestCase):
    def test_cleanup_only_removes_accounts_without_recreating_them(self):
        output = StringIO()
        call_command(
            "seed_user_story_accounts",
            reset=True,
            password="AcceptancePass2026!",
            stdout=output,
        )
        users = get_user_model().objects.filter(username__in=ACCOUNT_ROLES)
        self.assertEqual(users.count(), len(ACCOUNT_ROLES))

        call_command(
            "seed_user_story_accounts",
            cleanup_only=True,
            password="AcceptancePass2026!",
            stdout=output,
        )

        self.assertFalse(users.exists())
        self.assertIn("related data removed", output.getvalue())

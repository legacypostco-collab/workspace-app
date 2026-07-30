import importlib
import json
import uuid

from django.apps import apps
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase

from assistant.models import ActionExecution, Conversation, Message
from marketplace.models import Order, OrderEvent, TwoFactorAuth


class SecretCleanupMigrationTests(TestCase):
    def test_existing_chat_and_idempotency_secrets_are_redacted(self):
        user = get_user_model().objects.create_user(
            username="secret-cleanup",
            password="test-password",
        )
        conversation = Conversation.objects.create(user=user, role="admin")
        raw_api_token = "ck_live_" + ("A" * 32)
        raw_invite_token = "invite-" + ("B" * 24)
        assistant_message = Message.objects.create(
            conversation=conversation,
            role=Message.Role.ASSISTANT,
            content=(
                f"Generated: {raw_api_token}\n"
                f"https://example.com/chat/?join_team={raw_invite_token}"
            ),
            cards=[
                {
                    "type": "qr",
                    "data": {
                        "payload": "otpauth://totp/test?secret=ABC",
                        "manual_entry": "ABC",
                    },
                },
                {
                    "type": "list",
                    "data": {
                        "title": "Backup-коды",
                        "items": [{"title": "deadbeef"}],
                    },
                },
            ],
        )
        action_message = Message.objects.create(
            conversation=conversation,
            role=Message.Role.ACTION,
            content="verify",
            actions=[
                {
                    "action": "verify_2fa",
                    "params": {"code": "123456", "confirmed": True},
                }
            ],
        )
        execution = ActionExecution.objects.create(
            operation_id=uuid.uuid4(),
            user=user,
            action="create_api_token",
            response={
                "text": raw_api_token,
                "cards": [{"data": {"manual_entry": "ABC"}}],
            },
        )

        migration = importlib.import_module(
            "marketplace.migrations.0102_remove_auth_secrets_from_chat_history"
        )
        migration.remove_auth_secrets(apps, None)

        assistant_message.refresh_from_db()
        action_message.refresh_from_db()
        execution.refresh_from_db()
        serialized = json.dumps(
            {
                "content": assistant_message.content,
                "cards": assistant_message.cards,
                "actions": action_message.actions,
                "response": execution.response,
            }
        )
        self.assertNotIn(raw_api_token, serialized)
        self.assertNotIn(raw_invite_token, serialized)
        self.assertNotIn("otpauth://", serialized)
        self.assertNotIn("deadbeef", serialized)
        self.assertNotIn("123456", serialized)
        self.assertNotIn('"ABC"', serialized)

    def test_unused_qr_tokens_are_removed_from_orders_and_events(self):
        user = get_user_model().objects.create_user(
            username="qr-cleanup",
            password="test-password",
        )
        order = Order.objects.create(
            customer_name="QR cleanup",
            customer_email="qr-cleanup@example.com",
            customer_phone="+70000000000",
            delivery_address="Address",
            buyer=user,
            logistics_meta={"qr_token": "old-secret", "route": "air"},
        )
        event = OrderEvent.objects.create(
            order=order,
            event_type="document_uploaded",
            actor=user,
            source="buyer",
            meta={"kind": "qr", "token": "old-secret", "note": "created"},
        )

        migration = importlib.import_module(
            "marketplace.migrations.0103_remove_unused_qr_tokens"
        )
        migration.remove_unused_qr_tokens(apps, None)

        order.refresh_from_db()
        event.refresh_from_db()
        self.assertEqual(order.logistics_meta, {"route": "air"})
        self.assertEqual(event.meta, {"kind": "qr", "note": "created"})

    def test_legacy_plaintext_totp_secret_is_encrypted(self):
        user = get_user_model().objects.create_user(
            username="totp-cleanup",
            password="test-password",
        )
        two_factor = TwoFactorAuth.objects.create(
            user=user,
            secret="INITIALVALUE",
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE marketplace_twofactorauth SET secret = %s WHERE id = %s",
                ["LEGACYPLAINTEXT", two_factor.id],
            )

        migration = importlib.import_module(
            "marketplace.migrations.0104_encrypt_two_factor_secrets"
        )
        migration.encrypt_existing_secrets(apps, None)

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT secret FROM marketplace_twofactorauth WHERE id = %s",
                [two_factor.id],
            )
            stored = cursor.fetchone()[0]
        self.assertTrue(stored.startswith("enc:v1:"))
        self.assertNotEqual(stored, "LEGACYPLAINTEXT")
        two_factor.refresh_from_db()
        self.assertEqual(two_factor.secret, "LEGACYPLAINTEXT")

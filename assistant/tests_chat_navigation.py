from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework.test import APIClient

from marketplace.models import RFQ, UserProfile, UserRole

from .commands import commands_for_role
from .models import Conversation, Message
from .serializers import MessageSerializer


class ConversationNavigationTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user("chat_nav", password="x")
        UserProfile.objects.create(user=self.user, role="buyer")
        UserRole.objects.create(user=self.user, role="seller", is_enabled=True)
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_explicit_new_conversations_have_unique_ids(self):
        first = self.client.post("/api/assistant/conversations/", {}, format="json")
        second = self.client.post("/api/assistant/conversations/", {}, format="json")

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)
        self.assertNotEqual(first.data["id"], second.data["id"])

    def test_conversation_list_is_scoped_to_active_role(self):
        buyer = Conversation.objects.create(user=self.user, role="buyer", title="Buyer")
        seller = Conversation.objects.create(user=self.user, role="seller", title="Seller")

        response = self.client.get("/api/assistant/conversations/")

        self.assertEqual(response.status_code, 200)
        ids = {str(item["id"]) for item in response.data}
        self.assertIn(str(buyer.id), ids)
        self.assertNotIn(str(seller.id), ids)

    def test_conversation_from_other_role_cannot_be_opened(self):
        seller = Conversation.objects.create(user=self.user, role="seller")

        response = self.client.get(f"/api/assistant/conversations/{seller.id}/")

        self.assertEqual(response.status_code, 404)

    def test_action_without_conversation_id_does_not_reuse_old_chat(self):
        first = self.client.post(
            "/api/assistant/action/",
            {"action": "get_orders", "params": {"_label": "Мои заказы"}},
            format="json",
        )
        second = self.client.post(
            "/api/assistant/action/",
            {"action": "get_orders", "params": {"_label": "Мои заказы"}},
            format="json",
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertNotEqual(first.data["conversation_id"], second.data["conversation_id"])


class ChatCommandTests(TestCase):
    def test_buyer_has_direct_rfq_command(self):
        commands = commands_for_role("buyer")
        actions = [item["action"] for item in commands]

        self.assertIn("get_rfq_status", actions)
        self.assertEqual(
            next(item["label"] for item in commands if item["action"] == "get_rfq_status"),
            "Заявки",
        )

    def test_admin_has_dedicated_commands(self):
        actions = [item["action"] for item in commands_for_role("admin")]

        self.assertIn("admin_dashboard", actions)
        self.assertIn("admin_users", actions)
        self.assertNotIn("op_dashboard", actions)

    def test_message_serializer_restores_contextual_controls(self):
        user = get_user_model().objects.create_user("message_history", password="x")
        conversation = Conversation.objects.create(user=user, role="buyer")
        message = Message.objects.create(
            conversation=conversation,
            role=Message.Role.ASSISTANT,
            content="Готово",
            contextual_actions=[{"action": "track_order", "label": "Открыть заказ"}],
            suggestions=[{"action": "get_orders", "label": "Мои заказы"}],
        )

        data = MessageSerializer(message).data

        self.assertEqual(data["contextual_actions"][0]["action"], "track_order")
        self.assertEqual(data["suggestions"][0]["action"], "get_orders")

    def test_widget_returns_admin_commands_for_staff(self):
        admin = get_user_model().objects.create_user(
            "chat_admin",
            password="x",
            is_staff=True,
        )
        client = APIClient()
        client.force_authenticate(admin)

        response = client.get("/api/assistant/widget-config/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["role"], "admin")
        actions = [item["action"] for item in response.data["commands"]]
        self.assertIn("admin_dashboard", actions)
        self.assertNotIn("op_dashboard", actions)

    def test_guest_cannot_create_persistent_conversation(self):
        response = APIClient().post("/api/assistant/conversations/", {}, format="json")

        self.assertEqual(response.status_code, 403)


class GuestWorkspaceTests(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()

    def test_guest_commands_only_contain_public_workspace_actions(self):
        response = self.client.get("/api/assistant/widget-config/")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["anonymous"])
        actions = [item["action"] for item in response.data["commands"]]
        self.assertEqual(
            actions,
            ["search_parts", "compare_suppliers", "kb_search", "create_rfq"],
        )
        self.assertNotIn("get_orders", actions)
        self.assertNotIn("get_rfq_status", actions)

    def test_public_workspace_hides_account_only_navigation(self):
        response = self.client.get("/chat/?workspace=1")
        html = response.content.decode()

        self.assertEqual(response.status_code, 200)
        self.assertIn("guest-side-action", html)
        self.assertNotIn('data-role="seller"', html)
        self.assertNotIn('data-role="operator"', html)
        self.assertNotIn("window.quickAction('seller_team'", html)
        self.assertNotIn("/api/me/export/", html)
        self.assertNotIn('id="topBell"', html)
        self.assertIn("Публичный просмотр", html)

    def test_landing_search_opens_public_workspace(self):
        response = self.client.get("/landing/")

        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("'/chat/?workspace=1&q='", html)
        self.assertIn("'/chat/?workspace=1'", html)

    def test_public_login_form_has_no_demo_credentials(self):
        response = self.client.post(
            "/api/assistant/action/",
            {"action": "start_login", "params": {}},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        serialized = str(response.data)
        self.assertNotIn("demo_buyer", serialized)
        self.assertNotIn("demo-аккаунт", serialized)

    def test_guest_rfq_is_deferred_until_authentication(self):
        before = RFQ.objects.count()
        response = self.client.post(
            "/api/assistant/action/",
            {
                "action": "create_rfq",
                "params": {"query": "RE48786", "quantity": 2},
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(RFQ.objects.count(), before)
        self.assertEqual(
            self.client.session["pending_action"]["action"],
            "create_rfq",
        )
        self.assertIn("аккаунт", response.data["text"].lower())

    def test_guest_can_parse_spec_without_creating_conversation(self):
        upload = SimpleUploadedFile(
            "parts.csv",
            b"part_number,quantity\nRE48786,2\n",
            content_type="text/csv",
        )

        response = self.client.post(
            "/api/assistant/upload-spec/",
            {"file": upload},
            format="multipart",
        )

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.data["conversation_id"])
        self.assertGreaterEqual(response.data["articles_found"], 1)
        self.assertEqual(Conversation.objects.count(), 0)


class ChatAuthFlowTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            "chat_login",
            password="strong-test-password",
        )
        UserProfile.objects.create(user=self.user, role="buyer")
        self.client = APIClient()

    def test_role_endpoint_does_not_offer_demo_account(self):
        self.client.force_authenticate(self.user)

        response = self.client.post(
            "/api/assistant/role/",
            {"role": "seller"},
            format="json",
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["action"], "add_account_role")
        self.assertNotIn("target_username", response.data)

    def test_initial_login_form_does_not_show_2fa_field(self):
        response = self.client.post(
            "/api/assistant/action/",
            {
                "action": "start_login",
                "params": {"role": "buyer"},
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        fields = response.data["cards"][0]["data"]["fields"]
        self.assertEqual(
            [field["name"] for field in fields],
            ["username", "password"],
        )

    def test_invalid_password_keeps_login_form_visible(self):
        response = self.client.post(
            "/api/assistant/action/",
            {
                "action": "start_login",
                "params": {
                    "confirmed": True,
                    "role": "buyer",
                    "username": self.user.username,
                    "password": "wrong-password",
                },
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        form = response.data["cards"][0]["data"]
        fields = {field["name"]: field for field in form["fields"]}
        self.assertEqual(fields["username"]["value"], self.user.username)
        self.assertTrue(fields["password"]["error"])
        self.assertEqual(form["submit_action"], "start_login")

    def test_successful_login_persists_session_for_next_request(self):
        response = self.client.post(
            "/api/assistant/action/",
            {
                "action": "start_login",
                "params": {
                    "confirmed": True,
                    "role": "buyer",
                    "username": self.user.username,
                    "password": "strong-test-password",
                },
            },
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["_post_action"], "reload")
        self.assertEqual(
            self.client.session.get("_auth_user_id"),
            str(self.user.pk),
        )
        config = self.client.get("/api/assistant/widget-config/")
        self.assertEqual(config.status_code, 200)
        self.assertEqual(config.data["role"], "buyer")

    def test_enabled_2fa_is_requested_only_after_password(self):
        import pyotp
        from marketplace.models import TwoFactorAuth

        secret = pyotp.random_base32()
        TwoFactorAuth.objects.create(
            user=self.user,
            secret=secret,
            enabled=True,
        )
        password_response = self.client.post(
            "/api/assistant/action/",
            {
                "action": "start_login",
                "params": {
                    "confirmed": True,
                    "role": "buyer",
                    "username": self.user.username,
                    "password": "strong-test-password",
                },
            },
            format="json",
        )

        self.assertEqual(password_response.status_code, 200)
        form = password_response.data["cards"][0]["data"]
        self.assertEqual(
            [field["name"] for field in form["fields"]],
            ["otp_code"],
        )
        self.assertTrue(form["fixed_params"]["two_factor"])
        self.assertNotIn("_auth_user_id", self.client.session)

        code_response = self.client.post(
            "/api/assistant/action/",
            {
                "action": "start_login",
                "params": {
                    "confirmed": True,
                    "two_factor": True,
                    "role": "buyer",
                    "otp_code": pyotp.TOTP(secret).now(),
                },
            },
            format="json",
        )

        self.assertEqual(code_response.status_code, 200)
        self.assertEqual(code_response.data["_post_action"], "reload")
        self.assertEqual(
            self.client.session.get("_auth_user_id"),
            str(self.user.pk),
        )

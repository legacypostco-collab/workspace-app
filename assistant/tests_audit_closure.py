from io import BytesIO
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from assistant.actions import (
    _REGISTRY,
    _split_order_by_operator,
    _user_can_access_order,
    can_execute,
    execute,
    register,
)
from assistant.conversation_access import accessible_conversations
from assistant.models import ActionExecution, Conversation, ConversationParticipant, Message
from assistant.order_events import _shipment_conv
from assistant.permissions import detect_user_role, user_allowed_roles
from assistant.serializers import MessageSerializer
from assistant.support_threads import create_support_conversation, post_support_message
from marketplace.models import (
    Brand,
    Category,
    CompanyVerification,
    Order,
    OrderEvent,
    OrderItem,
    Notification,
    Part,
    PlatformRevenueLine,
    Quote,
    RFQItem,
    Shipment,
    TeamMember,
    UserProfile,
    UserRole,
)


User = get_user_model()


class WebSocketInputSecurityTests(TestCase):
    def test_binary_and_oversized_frames_are_closed(self):
        from assistant.consumers import AssistantConsumer, MAX_WS_FRAME_CHARS

        consumer = AssistantConsumer()
        consumer.close = AsyncMock()
        async_to_sync(consumer.receive)(bytes_data=b"binary")
        consumer.close.assert_awaited_once_with(code=1003)

        consumer.close.reset_mock()
        async_to_sync(consumer.receive)(text_data="x" * (MAX_WS_FRAME_CHARS + 1))
        consumer.close.assert_awaited_once_with(code=1009)

    def test_non_string_and_oversized_messages_are_rejected(self):
        import json

        from assistant.consumers import AssistantConsumer, MAX_WS_MESSAGE_CHARS

        consumer = AssistantConsumer()
        consumer.send_json = AsyncMock()

        async_to_sync(consumer.receive)(
            text_data=json.dumps({"type": "message", "content": []}),
        )
        self.assertEqual(
            consumer.send_json.await_args.args[0]["message"],
            "Invalid message",
        )

        consumer.send_json.reset_mock()
        async_to_sync(consumer.receive)(
            text_data=json.dumps({
                "type": "message",
                "content": "x" * (MAX_WS_MESSAGE_CHARS + 1),
            }),
        )
        self.assertIn(
            "слишком длинное",
            consumer.send_json.await_args.args[0]["message"],
        )


class ConfirmationParsingSecurityTests(TestCase):
    def test_confirmation_parser_rejects_false_like_and_arbitrary_values(self):
        from assistant.security import confirmation_is_true

        for value in (False, None, 0, "0", "false", "no", "off", "", [], {}):
            with self.subTest(value=value):
                self.assertFalse(confirmation_is_true(value))

        for value in (True, 1, "1", "true", "TRUE", "yes", "on", "да"):
            with self.subTest(value=value):
                self.assertTrue(confirmation_is_true(value))

    def test_false_string_does_not_apply_notification_change(self):
        from assistant.notif_settings import notif_set_email

        user = User.objects.create_user("confirmation_buyer")
        profile = UserProfile.objects.create(
            user=user,
            role="buyer",
            notif_email_enabled=True,
        )

        result = notif_set_email(
            {"enabled": "0", "confirmed": "false"},
            user,
            "buyer",
        )

        profile.refresh_from_db()
        self.assertTrue(profile.notif_email_enabled)
        self.assertTrue(result.cards)


class PricelistOverrideSecurityTests(TestCase):
    def test_ai_measurement_overrides_accept_only_finite_bounded_values(self):
        from assistant.pricelist import _apply_ai_measurement_overrides

        result = _apply_ai_measurement_overrides(
            {},
            {
                "GOOD-1": {"weight_kg": "12.5", "length_cm": "100"},
                "NAN-1": {"weight_kg": "NaN"},
                "INF-1": {"weight_kg": "Infinity"},
                "NEG-1": {"weight_kg": "-1"},
                "HUGE-1": {"weight_kg": "1000001"},
            },
        )

        self.assertEqual(result["GOOD-1"]["weight_kg"], 12.5)
        self.assertEqual(result["GOOD-1"]["confidence"], 1.0)
        self.assertNotIn("NAN-1", result)
        self.assertNotIn("INF-1", result)
        self.assertNotIn("NEG-1", result)
        self.assertNotIn("HUGE-1", result)


class RoleBoundaryTests(TestCase):
    @override_settings(DEBUG=False)
    def test_username_prefix_cannot_grant_operator_role(self):
        account = User.objects.create_user("demo_operator")

        self.assertEqual(detect_user_role(account), "buyer")
        self.assertEqual(user_allowed_roles(account), ["buyer"])

    def test_staff_flag_does_not_grant_application_admin(self):
        staff_buyer = User.objects.create_user("staff_buyer", is_staff=True)
        UserProfile.objects.create(user=staff_buyer, role="buyer")

        self.assertEqual(detect_user_role(staff_buyer), "buyer")
        self.assertEqual(user_allowed_roles(staff_buyer), ["buyer"])

    def test_operator_tab_resolves_to_granted_subrole(self):
        logist = User.objects.create_user("audit_logist", is_staff=True)
        UserProfile.objects.create(
            user=logist, role="operator", operator_role="logist",
        )

        self.assertEqual(detect_user_role(logist), "operator_logist")
        self.assertEqual(
            detect_user_role(logist, override="operator"),
            "operator_logist",
        )
        self.assertFalse(can_execute("op_confirm_topup", "operator_logist"))
        self.assertFalse(can_execute("op_customs_release", "operator_logist"))
        self.assertTrue(can_execute("op_assign_carrier", "operator_logist"))

    def test_staff_buyer_cannot_read_foreign_order(self):
        staff_buyer = User.objects.create_user("staff_order_buyer", is_staff=True)
        UserProfile.objects.create(user=staff_buyer, role="buyer")
        owner = User.objects.create_user("order_owner")
        UserProfile.objects.create(user=owner, role="buyer")
        order = Order.objects.create(
            customer_name="Owner", customer_email="owner@example.com",
            customer_phone="+70000000000", delivery_address="Address",
            buyer=owner,
        )

        self.assertFalse(_user_can_access_order(order, staff_buyer, "buyer"))

    def test_duplicate_action_registration_fails_fast(self):
        existing = _REGISTRY["search_parts"]
        with self.assertRaises(RuntimeError):
            register("search_parts")(lambda params, user, role: None)
        self.assertIs(_REGISTRY["search_parts"], existing)

    def test_seller_team_roles_are_enforced_by_executor(self):
        owner = User.objects.create_user("team_owner")
        UserProfile.objects.create(user=owner, role="seller")
        viewer = User.objects.create_user("team_viewer")
        UserProfile.objects.create(user=viewer, role="seller")
        TeamMember.objects.create(
            owner=owner,
            user=viewer,
            invited_email="viewer@example.com",
            role="viewer",
            status="active",
        )

        denied = execute("edit_product", {"product_id": 1}, viewer, "seller")
        allowed = execute("seller_catalog", {}, viewer, "seller")

        self.assertIn("не разрешает", denied.text)
        self.assertNotIn("не разрешает", allowed.text)

    def test_finance_employee_cannot_manage_company_team(self):
        owner = User.objects.create_user("finance_owner")
        UserProfile.objects.create(user=owner, role="seller")
        finance = User.objects.create_user("finance_member")
        UserProfile.objects.create(user=finance, role="seller")
        TeamMember.objects.create(
            owner=owner,
            user=finance,
            invited_email="finance@example.com",
            role="finance",
            status="active",
        )

        denied = execute(
            "team_set_role",
            {"member_id": 999, "role": "admin"},
            finance,
            "seller",
        )

        self.assertIn("не разрешает", denied.text)


class MultiSellerOrderIsolationTests(TestCase):
    def setUp(self):
        self.buyer = User.objects.create_user("multi_order_buyer")
        UserProfile.objects.create(user=self.buyer, role="buyer")
        self.seller = User.objects.create_user("multi_order_seller")
        UserProfile.objects.create(user=self.seller, role="seller")
        self.other_seller = User.objects.create_user("multi_order_other_seller")
        UserProfile.objects.create(user=self.other_seller, role="seller")
        category = Category.objects.create(
            name="Multi seller category",
            slug="multi-seller-category",
        )
        self.part = Part.objects.create(
            seller=self.seller,
            category=category,
            title="Visible seller item",
            slug="multi-visible-item",
            oem_number="VISIBLE-OEM-001",
            price=Decimal("210.00"),
        )
        self.other_part = Part.objects.create(
            seller=self.other_seller,
            category=category,
            title="Other seller confidential item",
            slug="multi-hidden-item",
            oem_number="HIDDEN-OEM-999",
            price=Decimal("999.00"),
        )
        self.order = Order.objects.create(
            buyer=self.buyer,
            customer_name="Multi buyer",
            customer_email="multi@example.com",
            customer_phone="+70000000000",
            delivery_address="Test address",
            status="confirmed",
            payment_status="reserve_paid",
            total_amount=Decimal("1419.00"),
            reserve_amount=Decimal("141.90"),
        )
        self.item = OrderItem.objects.create(
            order=self.order,
            part=self.part,
            quantity=2,
            unit_price=Decimal("210.00"),
            status="confirmed",
        )
        self.other_item = OrderItem.objects.create(
            order=self.order,
            part=self.other_part,
            quantity=1,
            unit_price=Decimal("999.00"),
            status="ready_to_ship",
        )
        self.shipment = Shipment.objects.create(
            order=self.order,
            kind="consolidated",
            status="formed",
        )
        self.shipment.items.add(self.item, self.other_item)
        OrderEvent.objects.create(
            order=self.order,
            event_type="status_changed",
            source="seller",
            actor=self.other_seller,
            meta={"to": "ready_to_ship", "secret": "hidden seller event"},
        )

    def test_chat_order_views_hide_other_seller_data(self):
        list_result = execute("get_orders", {}, self.seller, "seller")
        list_card = list_result.cards[0]["data"]
        self.assertEqual(list_card["total"], 420.0)

        detail = execute(
            "get_order_detail",
            {"order_id": self.order.id},
            self.seller,
            "seller",
        )
        spec_card = next(
            card for card in detail.cards
            if card["type"] == "spec_results"
        )
        self.assertEqual(spec_card["data"]["total"], 420)
        self.assertEqual(
            [item["id"] for item in spec_card["data"]["items"]],
            ["VISIBLE-OEM-001"],
        )
        self.assertNotIn("HIDDEN-OEM-999", str(detail.cards))
        self.assertFalse(
            any(
                action["action"] == "advance_order"
                for action in detail.actions
            ),
        )

        tracking = execute(
            "track_order",
            {"order_id": self.order.id},
            self.seller,
            "seller",
        )
        tracking_data = next(
            card["data"]
            for card in tracking.cards
            if card["type"] == "tracking"
        )
        self.assertEqual(tracking_data["total"], 420.0)
        self.assertEqual(len(tracking_data["parts"]), 1)
        self.assertEqual(tracking_data["parts"][0]["amount"], 420.0)
        self.assertNotIn("multi_order_other_seller", str(tracking_data))
        self.assertNotIn("hidden seller event", str(tracking_data))
        self.assertIsNone(tracking_data["tracking_number"])
        self.assertIsNone(tracking_data["carrier"])

    def test_multi_seller_global_actions_are_rejected(self):
        from assistant.actions import advance_order, ship_order
        from assistant.documents import generate_invoice_pdf

        advance = advance_order(
            {"order_id": self.order.id},
            self.seller,
            "seller",
        )
        shipment = ship_order(
            {"order_id": self.order.id},
            self.seller,
            "seller",
        )
        invoice = generate_invoice_pdf(
            {"order_id": self.order.id},
            self.seller,
            "seller",
        )

        self.assertIn("несколько поставщиков", advance.text)
        self.assertIn("несколько поставщиков", shipment.text)
        self.assertIn("несколько поставщиков", invoice.text)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "confirmed")

    def test_hybrid_buyer_seller_does_not_see_competitor_identity(self):
        UserRole.objects.create(user=self.buyer, role="seller", is_enabled=True)
        hybrid_part = Part.objects.create(
            seller=self.buyer,
            category=self.part.category,
            title="Hybrid own item",
            slug="hybrid-own-item",
            oem_number="HYBRID-OWN-001",
            price=Decimal("75.00"),
        )
        hybrid_item = OrderItem.objects.create(
            order=self.order,
            part=hybrid_part,
            quantity=1,
            unit_price=Decimal("75.00"),
            status="confirmed",
        )
        self.shipment.items.add(hybrid_item)

        detail = execute(
            "get_order_detail",
            {"order_id": self.order.id},
            self.buyer,
            "seller",
        )
        tracking = execute(
            "track_order",
            {"order_id": self.order.id},
            self.buyer,
            "seller",
        )
        competitor_batch = execute(
            "order_batch_items",
            {"order_id": self.order.id, "seller_id": self.other_seller.id},
            self.buyer,
            "seller",
        )

        serialized = str(detail.cards) + str(tracking.cards) + str(competitor_batch.cards)
        self.assertNotIn(self.seller.username, serialized)
        self.assertNotIn(self.other_seller.username, serialized)
        self.assertIn("Партнёр CP · ", serialized)


class SharedSupportConversationTests(TestCase):
    def setUp(self):
        self.buyer = User.objects.create_user("support_buyer")
        UserProfile.objects.create(user=self.buyer, role="buyer")
        self.operator = User.objects.create_user("support_manager", is_staff=True)
        UserProfile.objects.create(
            user=self.operator, role="operator", operator_role="manager",
        )
        self.stranger = User.objects.create_user("support_stranger")
        UserProfile.objects.create(user=self.stranger, role="buyer")

    @patch("assistant.consumers.push_notification_to_user")
    def test_both_participants_share_one_conversation(self, push):
        with self.captureOnCommitCallbacks(execute=True):
            conv = create_support_conversation(
                requester=self.buyer,
                requester_role="buyer",
                context="заказ #42",
                operator=self.operator,
            )

        self.assertEqual(conv.participant_links.count(), 2)
        self.assertTrue(
            accessible_conversations(self.buyer, "buyer").filter(id=conv.id).exists()
        )
        self.assertTrue(
            accessible_conversations(
                self.operator, "operator_manager",
            ).filter(id=conv.id).exists()
        )
        self.assertFalse(
            accessible_conversations(self.stranger, "buyer").filter(id=conv.id).exists()
        )

        with self.captureOnCommitCallbacks(execute=True):
            buyer_message = post_support_message(
                conv, self.buyer, "buyer", "Нужна помощь",
            )
            operator_message = post_support_message(
                conv, self.operator, "operator_manager", "Уточняю статус",
            )
        buyer_request = type("Request", (), {"user": self.buyer})()
        operator_request = type("Request", (), {"user": self.operator})()

        self.assertEqual(
            MessageSerializer(buyer_message, context={"request": buyer_request}).data["role"],
            "user",
        )
        self.assertEqual(
            MessageSerializer(buyer_message, context={"request": operator_request}).data["role"],
            "assistant",
        )
        self.assertEqual(
            MessageSerializer(operator_message, context={"request": buyer_request}).data["role"],
            "assistant",
        )
        self.assertEqual(conv.messages.filter(sender__isnull=False).count(), 2)
        self.assertGreaterEqual(push.call_count, 3)

    @patch("assistant.consumers.push_notification_to_user")
    def test_http_chat_posts_to_human_without_ai(self, _push):
        conv = create_support_conversation(
            requester=self.buyer,
            requester_role="buyer",
            context="общий вопрос",
            operator=self.operator,
        )
        client = Client()
        client.force_login(self.buyer)
        with patch("assistant.views.process_query_sync") as ai:
            response = client.post(
                "/api/assistant/chat/",
                data={"conversation_id": str(conv.id), "message": "Здравствуйте"},
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["human_support"])
        ai.assert_not_called()

    @patch("assistant.consumers.push_notification_to_user")
    def test_closed_support_thread_never_falls_back_to_ai(self, _push):
        conv = create_support_conversation(
            requester=self.buyer,
            requester_role="buyer",
            context="закрытый вопрос",
            operator=self.operator,
        )
        conv.support_status = "closed"
        conv.save(update_fields=["support_status"])
        client = Client()
        client.force_login(self.buyer)

        with patch("assistant.views.process_query_sync") as ai:
            response = client.post(
                "/api/assistant/chat/",
                data={"conversation_id": str(conv.id), "message": "Еще вопрос"},
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 403)
        ai.assert_not_called()
        self.assertEqual(conv.messages.filter(sender=self.buyer).count(), 0)

    @patch("assistant.consumers.push_notification_to_user")
    def test_complaint_has_explicit_kind_and_appears_in_operator_inbox(self, _push):
        result = execute(
            "open_complaint",
            {"confirmed": True, "against": "platform", "text": "Не работает оплата по заказу"},
            self.buyer,
            "buyer",
        )
        conv = Conversation.objects.get(id=result.navigate_conversation_id)

        self.assertEqual(conv.support_kind, "complaint")
        inbox = execute("support_home", {}, self.operator, "operator_manager")
        complaint_lists = [
            card for card in inbox.cards
            if card.get("type") == "list" and "Жалобы" in card.get("data", {}).get("title", "")
        ]
        self.assertEqual(len(complaint_lists), 1)
        self.assertEqual(
            complaint_lists[0]["data"]["items"][0]["params"]["conversation_id"],
            str(conv.id),
        )

    @patch("assistant.consumers.push_notification_to_user")
    def test_operator_joins_existing_ticket_instead_of_creating_duplicate(self, _push):
        conv = create_support_conversation(
            requester=self.buyer,
            requester_role="buyer",
            context="заказ #77",
            operator=self.operator,
        )
        lead = User.objects.create_user("support_lead", is_staff=True)
        UserProfile.objects.create(user=lead, role="operator")
        before = Conversation.objects.count()

        result = execute(
            "join_support_ticket",
            {"conversation_id": str(conv.id)},
            lead,
            "operator",
        )

        self.assertEqual(result.navigate_conversation_id, str(conv.id))
        self.assertEqual(Conversation.objects.count(), before)
        self.assertTrue(
            ConversationParticipant.objects.filter(
                conversation=conv, user=lead, role="operator",
            ).exists()
        )

    @patch("assistant.consumers.push_notification_to_user")
    def test_shared_thread_service_fields_cannot_be_changed_through_api(self, _push):
        conv = create_support_conversation(
            requester=self.buyer,
            requester_role="buyer",
            context="заказ #91",
            operator=self.operator,
        )
        client = Client()
        client.force_login(self.buyer)

        response = client.patch(
            f"/api/assistant/conversations/{conv.id}/",
            data={"category": "general", "title": "Обычный чат"},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 403)
        conv.refresh_from_db()
        self.assertEqual(conv.category, "support")

    @patch("assistant.consumers.push_notification_to_user")
    def test_action_from_shared_thread_is_written_to_a_separate_conversation(self, _push):
        conv = create_support_conversation(
            requester=self.buyer,
            requester_role="buyer",
            context="заказ #92",
            operator=self.operator,
        )
        client = Client()
        client.force_login(self.buyer)

        response = client.post(
            "/api/assistant/action/",
            data={
                "conversation_id": str(conv.id),
                "action": "get_orders",
                "params": {},
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(response.json()["conversation_id"], str(conv.id))
        conv.refresh_from_db()
        self.assertEqual(conv.category, "support")


class PaymentAndViewAsTests(TestCase):
    def setUp(self):
        self.buyer = User.objects.create_user("legacy_payment_buyer")
        UserProfile.objects.create(user=self.buyer, role="buyer")
        self.order = Order.objects.create(
            customer_name="Buyer", customer_email="buyer@example.com",
            customer_phone="+70000000001", delivery_address="Address",
            buyer=self.buyer, total_amount=Decimal("10000.00"),
        )

    def test_legacy_payment_and_quality_posts_are_gone(self):
        client = Client()
        client.force_login(self.buyer)
        paths = [
            f"/orders/{self.order.id}/reserve-paid/",
            f"/orders/{self.order.id}/final-paid/",
            f"/orders/{self.order.id}/mid-paid/",
            f"/orders/{self.order.id}/customs-paid/",
            f"/orders/{self.order.id}/confirm-quality/",
        ]
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(client.post(path).status_code, 410)
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, "awaiting_reserve")
        self.assertEqual(self.order.status, "pending")

    @override_settings(DEBUG=False)
    def test_demo_username_does_not_receive_production_balance(self):
        from assistant.models import Wallet, WalletTx

        account = User.objects.create_user("demo_balance")
        wallet = Wallet.for_user(account)

        self.assertEqual(wallet.balance, Decimal("0"))
        self.assertFalse(WalletTx.objects.filter(wallet=wallet).exists())

    def test_view_as_blocks_mutation_and_allows_operator_exit(self):
        operator = User.objects.create_user("view_operator", is_staff=True)
        UserProfile.objects.create(user=operator, role="operator")
        seller = User.objects.create_user("view_seller")
        UserProfile.objects.create(user=seller, role="seller")
        client = Client()
        client.force_login(operator)
        session = client.session
        session["op_view_as_id"] = seller.id
        session["op_view_as_originator_id"] = operator.id
        session.save()

        blocked = client.post(
            "/api/assistant/action/",
            data={"action": "upload_pricelist", "params": {}},
            content_type="application/json",
        )
        self.assertEqual(blocked.status_code, 403)
        self.assertEqual(Conversation.objects.count(), 0)

        exited = client.post(
            "/api/assistant/action/",
            data={"action": "op_exit_view_as", "params": {}},
            content_type="application/json",
        )
        self.assertEqual(exited.status_code, 200)
        self.assertEqual(exited.json()["_post_action"], "reload")
        self.assertNotIn("op_view_as_id", client.session)

    def test_order_cancellation_keeps_valid_payment_state(self):
        result = execute(
            "cancel_order",
            {"order_id": self.order.id},
            self.buyer,
            "buyer",
        )

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "cancelled")
        self.assertEqual(self.order.payment_status, "awaiting_reserve")
        self.assertIn("отменён", result.text)

    @override_settings(AI_REQUEST_PRICE_USD=float("nan"))
    def test_invalid_ai_request_price_cannot_change_wallet_or_credits(self):
        from assistant.models import Wallet, WalletTx

        wallet = Wallet.for_user(self.buyer)
        wallet.balance = Decimal("100.00")
        wallet.save(update_fields=["balance"])
        before_credits = self.buyer.profile.ai_credits

        result = execute(
            "buy_ai_requests",
            {"count": 100},
            self.buyer,
            "buyer",
        )

        wallet.refresh_from_db()
        self.buyer.profile.refresh_from_db()
        self.assertIn("временно недоступна", result.text.lower())
        self.assertEqual(wallet.balance, Decimal("100.00"))
        self.assertEqual(self.buyer.profile.ai_credits, before_credits)
        self.assertFalse(WalletTx.objects.filter(wallet=wallet).exists())

    def test_staged_order_cannot_be_charged_by_simple_final_payment(self):
        self.order.payment_scheme = "staged"
        self.order.payment_status = "reserve_paid"
        self.order.status = "ready_to_ship"
        self.order.save(update_fields=["payment_scheme", "payment_status", "status"])

        result = execute(
            "pay_final",
            {"order_id": self.order.id, "confirmed": True},
            self.buyer,
            "buyer",
        )

        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, "reserve_paid")
        self.assertIn("Автоматическое списание заблокировано", result.text)

    @override_settings(
        DEBUG=False,
        PLATFORM_LEGAL_NAME="",
        PLATFORM_LEGAL_ADDRESS="",
        PLATFORM_TAX_ID="",
        PLATFORM_REGISTRATION_NO="",
        PLATFORM_BANK_NAME="",
        PLATFORM_BANK_ACCOUNT_TITLE="",
        PLATFORM_BANK_IBAN="",
        PLATFORM_BANK_ACCOUNT="",
        PLATFORM_BANK_SWIFT="",
        PLATFORM_BANK_BRANCH="",
        PLATFORM_BANK_BRANCH_CODE="",
        PLATFORM_BANK_CURRENCY="",
        TOPUP_BANK_BENEFICIARY="",
        TOPUP_BANK_BENEFICIARY_ADDR="",
        TOPUP_BANK_TRADE_LICENSE="",
        TOPUP_BANK_TAX_NO="",
        TOPUP_BANK_NAME="",
        TOPUP_BANK_BRANCH_CODE="",
        TOPUP_BANK_SWIFT="",
        TOPUP_BANK_IBAN="",
        TOPUP_BANK_ACCOUNT="",
        TOPUP_BANK_CURRENCY="",
        TOPUP_USDT_ADDRESS="",
    )
    @patch.dict("os.environ", {"WALLET_DEMO_MODE": "1"})
    def test_demo_topup_cannot_credit_wallet_in_production(self):
        from assistant.actions import topup_wallet
        from assistant.models import Wallet, WalletTx

        result = topup_wallet({"amount": 10000}, self.buyer, "buyer")

        self.assertIn("оператор", result.text.lower())
        self.assertEqual(Wallet.for_user(self.buyer).balance, Decimal("0"))
        self.assertFalse(WalletTx.objects.filter(wallet__user=self.buyer).exists())

    @override_settings(
        TOPUP_BANK_BENEFICIARY="",
        TOPUP_BANK_NAME="",
        TOPUP_BANK_SWIFT="",
        TOPUP_BANK_IBAN="",
        TOPUP_BANK_ACCOUNT="",
        TOPUP_USDT_ADDRESS="",
    )
    def test_unconfigured_card_topup_does_not_create_request(self):
        from assistant.actions import submit_topup
        from assistant.models import WalletTopupRequest

        result = submit_topup(
            {"amount": "1000", "method": "card"},
            self.buyer,
            "buyer",
        )

        self.assertIn("Неизвестный способ оплаты", result.text)
        self.assertFalse(WalletTopupRequest.objects.exists())

    @override_settings(
        TOPUP_BANK_BENEFICIARY="Test Company",
        TOPUP_BANK_NAME="Test Bank",
        TOPUP_BANK_SWIFT="TESTUS00",
        TOPUP_BANK_IBAN="TEST-IBAN",
        TOPUP_BANK_ACCOUNT="TEST-ACCOUNT",
    )
    def test_topup_rejects_non_finite_amount(self):
        from assistant.actions import submit_topup
        from assistant.models import WalletTopupRequest

        for amount in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(amount=amount):
                result = submit_topup(
                    {"amount": amount, "method": "bank_wire"},
                    self.buyer,
                    "buyer",
                )
                self.assertIn("Некорректная сумма", result.text)

        self.assertFalse(WalletTopupRequest.objects.exists())

    def test_action_operation_id_replays_completed_response(self):
        import uuid

        client = Client()
        client.force_login(self.buyer)
        operation_id = str(uuid.uuid4())
        payload = {
            "action": "cancel_order",
            "params": {"order_id": self.order.id},
            "operation_id": operation_id,
        }

        first = client.post(
            "/api/assistant/action/", data=payload, content_type="application/json",
        )
        second = client.post(
            "/api/assistant/action/", data=payload, content_type="application/json",
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json(), first.json())
        self.assertEqual(
            ActionExecution.objects.filter(
                user=self.buyer, operation_id=operation_id,
            ).count(),
            1,
        )

    def test_action_operation_is_removed_when_conversation_is_inaccessible(self):
        import uuid

        stranger = User.objects.create_user("operation_stranger")
        UserProfile.objects.create(user=stranger, role="buyer")
        stranger_conversation = Conversation.objects.create(
            user=stranger, role="buyer",
        )
        client = Client()
        client.force_login(self.buyer)
        operation_id = str(uuid.uuid4())

        response = client.post(
            "/api/assistant/action/",
            data={
                "conversation_id": str(stranger_conversation.id),
                "action": "get_orders",
                "params": {},
                "operation_id": operation_id,
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 404)
        self.assertFalse(
            ActionExecution.objects.filter(
                user=self.buyer, operation_id=operation_id,
            ).exists()
        )


class PartNormalizationTests(TestCase):
    def setUp(self):
        self.category = Category.objects.create(
            name="OEM normalization", slug="oem-normalization",
        )

    def test_regular_and_bulk_writes_keep_oem_clean_current(self):
        regular = Part.objects.create(
            title="Regular", slug="regular-oem", oem_number=" ab-12.3 ",
            category=self.category, price=Decimal("10.00"),
        )
        bulk = Part(
            title="Bulk", slug="bulk-oem", oem_number=" km/45_6 ",
            category=self.category, price=Decimal("20.00"),
        )
        Part.objects.bulk_create([bulk])

        regular.refresh_from_db()
        bulk.refresh_from_db()
        self.assertEqual(regular.oem_clean, "AB123")
        self.assertEqual(bulk.oem_clean, "KM456")

        bulk.oem_number = " zz-99 "
        Part.objects.bulk_update([bulk], ["oem_number"])
        bulk.refresh_from_db()
        self.assertEqual(bulk.oem_clean, "ZZ99")


class SellerCatalogMutationTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("catalog_company_owner")
        UserProfile.objects.create(user=self.owner, role="seller")
        self.employee = User.objects.create_user("catalog_company_employee")
        UserProfile.objects.create(user=self.employee, role="seller")
        CompanyVerification.objects.create(
            user=self.owner,
            legal_name="Catalog Company",
            status="verified",
        )
        TeamMember.objects.create(
            owner=self.owner,
            user=self.employee,
            invited_email="catalog-employee@example.com",
            role="manager",
            status="active",
        )
        self.category = Category.objects.create(
            name="Catalog mutation",
            slug="catalog-mutation",
        )

    def test_team_member_adds_product_to_company_catalog(self):
        result = execute(
            "add_product",
            {
                "article": "TEAM-001",
                "title": "Team product",
                "price": "125.50",
                "stock_qty": "10",
                "brand": "Team Brand",
            },
            self.employee,
            "seller",
        )

        self.assertIn("добавлен", result.text)
        part = Part.objects.get(oem_number="TEAM-001")
        self.assertEqual(part.seller, self.owner)
        self.assertTrue(part.slug)
        self.assertTrue(part.brand.slug)

    def test_add_product_rejects_invalid_money_and_stock(self):
        for price, stock in (("NaN", "1"), ("100", "-1"), ("100", "NaN")):
            with self.subTest(price=price, stock=stock):
                result = execute(
                    "add_product",
                    {
                        "article": f"BAD-{price}-{stock}",
                        "title": "Invalid product",
                        "price": price,
                        "stock_qty": stock,
                    },
                    self.owner,
                    "seller",
                )
                self.assertIn("Некорр", result.text)
        self.assertFalse(Part.objects.filter(title="Invalid product").exists())

    def test_string_false_deactivates_product(self):
        part = Part.objects.create(
            title="Toggle product",
            slug="toggle-product",
            oem_number="TOGGLE-1",
            category=self.category,
            seller=self.owner,
            price=Decimal("10.00"),
            is_active=True,
        )

        execute(
            "toggle_product",
            {"part_id": part.id, "active": "false"},
            self.employee,
            "seller",
        )

        part.refresh_from_db()
        self.assertFalse(part.is_active)


class OrderSplitAndAssignmentTests(TestCase):
    def setUp(self):
        self.buyer = User.objects.create_user("split_buyer")
        UserProfile.objects.create(user=self.buyer, role="buyer")
        self.lead = User.objects.create_user("split_lead", is_staff=True)
        UserProfile.objects.create(user=self.lead, role="operator")
        self.logist = User.objects.create_user("split_logist", is_staff=True)
        UserProfile.objects.create(
            user=self.logist, role="operator", operator_role="logist",
        )
        self.customs = User.objects.create_user("split_customs", is_staff=True)
        UserProfile.objects.create(
            user=self.customs, role="operator", operator_role="customs",
        )
        self.seller_a = User.objects.create_user("split_seller_a")
        self.seller_b = User.objects.create_user("split_seller_b")
        UserProfile.objects.create(
            user=self.seller_a, role="seller", assigned_operator=self.logist,
        )
        UserProfile.objects.create(
            user=self.seller_b, role="seller", assigned_operator=self.customs,
        )
        CompanyVerification.objects.create(
            user=self.seller_a,
            status="verified",
            legal_name="Verified Split Seller",
        )
        brand = Brand.objects.create(name="Split Brand")
        category = Category.objects.create(name="Split Category", slug="split-category")
        self.part_a = Part.objects.create(
            brand=brand, category=category, seller=self.seller_a,
            oem_number="SPLIT-A", title="Part A", slug="split-a",
            price=Decimal("100.00"),
        )
        self.part_b = Part.objects.create(
            brand=brand, category=category, seller=self.seller_b,
            oem_number="SPLIT-B", title="Part B", slug="split-b",
            price=Decimal("250.00"),
        )
        self.order = Order.objects.create(
            customer_name="Split", customer_email="split@example.com",
            customer_phone="+70000000002", delivery_address="Address",
            buyer=self.buyer, total_amount=Decimal("700.00"),
        )
        OrderItem.objects.create(
            order=self.order, part=self.part_a, quantity=2,
            unit_price=Decimal("100.00"), status="confirmed",
        )
        OrderItem.objects.create(
            order=self.order, part=self.part_b, quantity=2,
            unit_price=Decimal("250.00"), status="in_production",
        )

    def test_split_orders_contain_their_items_and_totals(self):
        children = _split_order_by_operator(self.order)

        self.assertEqual(len(children), 2)
        by_operator = {child.assigned_operator_id: child for child in children}
        self.assertEqual(by_operator[self.logist.id].items.count(), 1)
        self.assertEqual(by_operator[self.logist.id].total_amount, Decimal("200.00"))
        self.assertEqual(by_operator[self.customs.id].items.count(), 1)
        self.assertEqual(by_operator[self.customs.id].total_amount, Decimal("500.00"))
        self.assertTrue(all(child.is_sub_order for child in children))

    @patch("assistant.operator_actions._notify")
    def test_assignment_persists_selected_specialist(self, notify):
        result = execute(
            "op_assign",
            {"order_id": self.order.id, "to_role": "logist", "confirmed": True},
            self.lead,
            "operator",
        )

        self.order.refresh_from_db()
        self.assertEqual(self.order.assigned_operator, self.logist)
        self.assertIn(self.logist.username, result.text)
        notify.assert_called_once()

    def test_shipment_conversations_are_separated_by_role(self):
        buyer_conv = _shipment_conv(self.buyer, self.order, role="buyer")
        seller_conv = _shipment_conv(self.buyer, self.order, role="seller")

        self.assertNotEqual(buyer_conv.id, seller_conv.id)
        self.assertEqual(
            Conversation.objects.filter(
                user=self.buyer, category="shipment",
            ).count(),
            2,
        )

    def test_seller_cannot_advance_logistics_after_shipping_handoff(self):
        self.order.status = "transit_abroad"
        self.order.save(update_fields=["status"])

        result = execute(
            "advance_order",
            {"order_id": self.order.id},
            self.seller_a,
            "seller",
        )

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "transit_abroad")
        self.assertIn("дальнейшие этапы ведёт оператор", result.text)

    @patch("assistant.payments.release_to_seller", side_effect=RuntimeError("gateway failed"))
    @patch("assistant.payments.split_by_seller")
    def test_delivery_settlement_failure_rolls_back_status_and_revenue(self, split, _release):
        self.order.status = "delivered"
        self.order.save(update_fields=["status"])
        split.return_value = [{
            "seller": self.seller_a,
            "amount": Decimal("200.00"),
            "share": Decimal("1.00"),
        }]

        with patch(
            "assistant.actions._verified_trigger_ids",
            return_value={"qr_received", "signed_docs"},
        ):
            result = execute(
                "confirm_delivery",
                {"order_id": self.order.id, "confirmed": True},
                self.buyer,
                "buyer",
            )

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "delivered")
        self.assertEqual(
            PlatformRevenueLine.objects.filter(order=self.order).count(),
            0,
        )
        self.assertIn("Статус и деньги не изменены", result.text)


class LazyConversationTests(TestCase):
    def test_widget_config_does_not_create_empty_conversation(self):
        user = User.objects.create_user("lazy_chat_user")
        UserProfile.objects.create(user=user, role="buyer")
        client = Client()
        client.force_login(user)

        response = client.get("/api/assistant/widget-config/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Conversation.objects.filter(user=user).count(), 0)


class BackgroundTaskTests(TestCase):
    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_email_digest_does_not_resend_same_notification(self):
        from marketplace.tasks import send_pending_email_notifications

        user = User.objects.create_user("digest_user", email="digest@example.com")
        UserProfile.objects.create(
            user=user,
            role="buyer",
            notif_email_enabled=True,
            notif_kinds="order",
        )
        notification = Notification.objects.create(
            user=user,
            kind="order",
            title="Статус заказа изменен",
        )

        with patch("marketplace.tasks.send_mail", return_value=1) as send:
            send_pending_email_notifications()
            send_pending_email_notifications()

        notification.refresh_from_db()
        self.assertEqual(send.call_count, 1)
        self.assertIsNotNone(notification.email_sent_at)
        self.assertEqual(notification.email_attempts, 1)

    @patch("assistant.actions._notify")
    def test_sla_task_resolves_recipients_through_order_items(self, notify):
        from marketplace.tasks import check_sla_breaches

        buyer = User.objects.create_user("sla_buyer")
        seller = User.objects.create_user("sla_seller")
        UserProfile.objects.create(user=buyer, role="buyer")
        UserProfile.objects.create(user=seller, role="seller")
        brand = Brand.objects.create(name="SLA Brand")
        category = Category.objects.create(name="SLA Category", slug="sla-category")
        part = Part.objects.create(
            brand=brand,
            category=category,
            seller=seller,
            oem_number="SLA-1",
            title="SLA Part",
            slug="sla-part",
            price=Decimal("10.00"),
        )
        order = Order.objects.create(
            customer_name="SLA",
            customer_email="sla@example.com",
            customer_phone="+70000000009",
            delivery_address="Address",
            buyer=buyer,
            status="in_production",
            ship_deadline=timezone.now() - timezone.timedelta(hours=1),
        )
        OrderItem.objects.create(
            order=order,
            part=part,
            quantity=1,
            unit_price=Decimal("10.00"),
        )

        result = check_sla_breaches()

        order.refresh_from_db()
        notified_user_ids = {call.args[0].id for call in notify.call_args_list}
        self.assertEqual(notified_user_ids, {buyer.id, seller.id})
        self.assertEqual(order.sla_status, "breached")
        self.assertEqual(order.sla_breaches_count, 1)
        self.assertIn("2", result)


class LocalQrGenerationTests(TestCase):
    @override_settings(
        QR_SECRET="test-only-qr-secret",
        SITE_URL="https://service.example.com",
    )
    def test_order_qr_never_sends_signed_payload_to_third_party(self):
        buyer = User.objects.create_user("qr_buyer")
        seller = User.objects.create_user("qr_seller")
        UserProfile.objects.create(user=buyer, role="buyer")
        UserProfile.objects.create(user=seller, role="seller")
        brand = Brand.objects.create(name="QR Brand")
        category = Category.objects.create(name="QR Category", slug="qr-category")
        part = Part.objects.create(
            brand=brand,
            category=category,
            seller=seller,
            oem_number="QR-1",
            title="QR Part",
            slug="qr-part",
            price=Decimal("10.00"),
        )
        order = Order.objects.create(
            customer_name="QR Buyer",
            customer_email="qr@example.com",
            customer_phone="+70000000010",
            delivery_address="Address",
            buyer=buyer,
        )
        OrderItem.objects.create(
            order=order,
            part=part,
            quantity=1,
            unit_price=Decimal("10.00"),
        )

        result = execute("generate_qr", {"order_id": order.id}, seller, "seller")

        image_url = result.cards[0]["data"]["image_url"]
        self.assertTrue(image_url.startswith("data:image/png;base64,"))
        self.assertNotIn("qrserver", image_url)
        order.refresh_from_db()
        self.assertNotIn("qr_token", order.logistics_meta)
        self.assertFalse(
            OrderEvent.objects.filter(
                order=order,
                meta__has_key="token",
            ).exists()
        )


class ExternalFallbackBoundaryTests(TestCase):
    @override_settings(DEBUG=False)
    @patch("marketplace.fx._fetch_rates", return_value=None)
    def test_production_fx_does_not_use_fixed_demo_rates(self, _fetch):
        from django.core.cache import cache
        from marketplace.fx import get_rates

        cache.clear()
        with self.assertRaises(RuntimeError):
            get_rates()

    @override_settings(
        DEBUG=False,
        LOGISTICS_STRICT_MODE=False,
        TEUSTAT_STRICT_MODE=False,
        TEUSTAT_API_URL="",
    )
    def test_production_logistics_does_not_return_formula_as_quote(self):
        from marketplace.services.logistics import _request_external

        result = _request_external("teustat", {"weight_kg": "100"})

        self.assertFalse(result["ok"])
        self.assertIn("not configured", result["error"])

    @override_settings(
        DEBUG=False,
        LOGISTICS_STRICT_MODE=False,
        TEUSTAT_STRICT_MODE=False,
        LOGISTICS_PROVIDER="internal",
    )
    def test_production_rejects_internal_logistics_formula(self):
        from marketplace.services.logistics import logistics_estimate

        result = logistics_estimate({"weight_kg": "100"})

        self.assertFalse(result["ok"])
        self.assertEqual(result["provider"], "internal")

    @override_settings(
        DEBUG=False,
        LOGISTICS_STRICT_MODE=True,
        TEUSTAT_API_URL="https://logistics.example/quote",
        TEUSTAT_API_KEY="secret",
    )
    @patch(
        "assistant.security.safe_outbound_url",
        return_value=(True, ""),
    )
    @patch(
        "assistant.security.urlopen_no_redirect",
        return_value=BytesIO(
            b'{"cost":"125.50","currency":"USD",'
            b'"internal_token":"must-not-leak"}'
        ),
    )
    def test_logistics_response_does_not_expose_provider_payload(
        self,
        _open,
        _safe_url,
    ):
        from marketplace.services.logistics import _request_external

        result = _request_external("teustat", {"weight_kg": "100"})

        self.assertTrue(result["ok"])
        self.assertEqual(result["cost"], "125.50")
        self.assertNotIn("internal_token", str(result))

    @override_settings(DEBUG=False, EMBEDDING_PROVIDER="auto")
    @patch("assistant.embeddings._openai_embedding", side_effect=RuntimeError)
    @patch("assistant.embeddings._voyage_embedding", side_effect=RuntimeError)
    def test_production_embeddings_fail_closed_without_provider(
        self,
        _voyage,
        _openai,
    ):
        from assistant.embeddings import get_embedding

        with self.assertRaises(RuntimeError):
            get_embedding("sensitive production query")

    @override_settings(DEBUG=False, LOGISTICS_STRICT_MODE=False)
    @patch(
        "marketplace.views.logistics_estimate",
        return_value={"ok": False, "error": "provider unavailable"},
    )
    def test_order_is_not_created_with_zero_logistics_after_provider_failure(
        self,
        _estimate,
    ):
        from marketplace.views import _create_order_from_rows

        category = Category.objects.create(
            name="Logistics boundary",
            slug="logistics-boundary",
        )
        part = Part.objects.create(
            category=category,
            title="Logistics part",
            slug="logistics-part",
            oem_number="LOG-FAIL-1",
            price=Decimal("100.00"),
            stock_quantity=5,
            gross_weight_kg=Decimal("10.00"),
        )

        with self.assertRaisesMessage(ValueError, "provider unavailable"):
            _create_order_from_rows(
                rows=[{"part": part, "quantity": 1}],
                total=Decimal("100.00"),
                customer_name="Audit Buyer",
                customer_email="audit@example.com",
                customer_phone="+79990000002",
                delivery_address="Audit address",
                buyer=None,
                source="audit",
            )

        self.assertFalse(Order.objects.filter(customer_email="audit@example.com").exists())


class RegistrationSecurityTests(TestCase):
    def setUp(self):
        from django.core.cache import cache

        cache.clear()

    def tearDown(self):
        from django.core.cache import cache

        cache.clear()

    @override_settings(EMAIL_VERIFICATION_REQUIRED=True)
    @patch("marketplace.views._send_verification_email", return_value=True)
    @patch("assistant.buyer_registration._check_email", return_value=(True, ""))
    def test_chat_buyer_registration_requires_email_activation(
        self,
        _check_email,
        send_email,
    ):
        response = self.client.post(
            "/api/assistant/action/",
            data={
                "action": "start_registration",
                "params": {
                    "confirmed": True,
                    "role": "buyer",
                    "country": "RU",
                    "tax_id": "7708123456",
                    "contact_name": "Audit Buyer",
                    "position": "buyer",
                    "email": "audit-buyer@example.com",
                    "phone_e164": "+79990000001",
                    "messenger_kind": "telegram",
                    "messenger_handle": "@audit_buyer",
                    "equipment_fleet": "Test fleet",
                    "username": "audit_chat_buyer",
                    "password1": "VeryStr0ngPass!42",
                    "password2": "VeryStr0ngPass!42",
                    "accept_terms": True,
                    "personal_data_consent": True,
                },
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        user = User.objects.get(username="audit_chat_buyer")
        self.assertFalse(user.is_active)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertIn("ссылку подтверждения", response.json()["text"])
        self.assertEqual(user.consent_records.count(), 2)
        send_email.assert_called_once()

    @override_settings(EMAIL_VERIFICATION_REQUIRED=True)
    @patch("marketplace.views._send_verification_email", return_value=True)
    def test_chat_seller_registration_requires_email_activation(self, send_email):
        response = self.client.post(
            "/api/assistant/action/",
            data={
                "action": "start_registration",
                "params": {
                    "confirmed": True,
                    "role": "seller",
                    "email": "audit-seller@example.com",
                    "username": "audit_chat_seller",
                    "password1": "VeryStr0ngPass!42",
                    "password2": "VeryStr0ngPass!42",
                    "accept_terms": True,
                    "personal_data_consent": True,
                },
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        user = User.objects.get(username="audit_chat_seller")
        self.assertFalse(user.is_active)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertIn("ссылку подтверждения", response.json()["text"])
        self.assertEqual(user.consent_records.count(), 2)
        send_email.assert_called_once()

    @patch("assistant.buyer_registration._check_email", return_value=(True, ""))
    def test_chat_registration_rejects_missing_separate_consents(self, _check_email):
        response = self.client.post(
            "/api/assistant/action/",
            data={
                "action": "start_registration",
                "params": {
                    "confirmed": True,
                    "role": "buyer",
                    "country": "RU",
                    "tax_id": "7708123456",
                    "contact_name": "No Consent Buyer",
                    "position": "buyer",
                    "email": "no-consent@example.com",
                    "phone_e164": "+79990000001",
                    "messenger_kind": "telegram",
                    "messenger_handle": "@no_consent_buyer",
                    "equipment_fleet": "Test fleet",
                    "username": "no_consent_buyer",
                    "password1": "VeryStr0ngPass!42",
                    "password2": "VeryStr0ngPass!42",
                },
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username="no_consent_buyer").exists())
        payload = response.json()
        self.assertIn("отдельным флажком", payload["text"])
        fields = payload["cards"][0]["data"]["fields"]
        consent_fields = {
            field["name"]: field
            for field in fields
            if field.get("type") == "checkbox"
        }
        self.assertEqual(
            set(consent_fields),
            {"accept_terms", "personal_data_consent"},
        )
        self.assertTrue(consent_fields["personal_data_consent"]["error"])

    def test_registration_rejects_duplicate_email_case_insensitively(self):
        from marketplace.forms import RegisterForm

        User.objects.create_user(
            "existing_email_owner",
            email="Owner@Example.com",
        )
        form = RegisterForm({
            "username": "second_email_owner",
            "email": "owner@example.com",
            "password1": "VeryStr0ngPass!42",
            "password2": "VeryStr0ngPass!42",
            "role": "buyer",
            "language": "ru",
            "first_name": "",
            "last_name": "",
            "company_name": "",
        })

        self.assertFalse(form.is_valid())
        self.assertIn("email", form.errors)


@override_settings(SITE_URL="https://service.example.com")
class InviteSecurityTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            "invite_owner",
            email="owner@example.com",
        )
        UserProfile.objects.create(
            user=self.owner,
            role="seller",
            company_name="Owner Company",
        )
        self.invited = User.objects.create_user(
            "invite_buyer",
            email="invited@example.com",
        )
        UserProfile.objects.create(user=self.invited, role="buyer")

    def test_team_invite_is_hashed_one_time_and_adds_role(self):
        import re

        from assistant.seller_actions import (
            accept_team_invite,
            invite_team_member,
        )

        created = invite_team_member(
            {"email": self.invited.email, "role": "manager"},
            self.owner,
            "seller",
        )
        raw_token = re.search(r"join_team=([A-Za-z0-9_-]+)", created.text).group(1)
        member = TeamMember.objects.get(owner=self.owner)
        self.assertNotEqual(member.invite_token, raw_token)
        self.assertEqual(len(member.invite_token), 64)

        accepted = accept_team_invite(
            {"token": raw_token},
            self.invited,
            "buyer",
        )
        self.assertIn("присоединились", accepted.text)
        member.refresh_from_db()
        self.assertEqual(member.status, "active")
        self.assertEqual(member.invite_token, "")
        self.assertEqual(self.invited.profile.role, "buyer")
        self.assertTrue(
            UserRole.objects.filter(
                user=self.invited,
                role="seller",
                is_enabled=True,
            ).exists()
        )

        reused = accept_team_invite(
            {"token": raw_token},
            self.invited,
            "seller",
        )
        self.assertIn("не найдено", reused.text)

    def test_team_invite_rejects_a_different_email(self):
        import re

        from assistant.seller_actions import (
            accept_team_invite,
            invite_team_member,
        )

        created = invite_team_member(
            {"email": self.invited.email, "role": "viewer"},
            self.owner,
            "seller",
        )
        raw_token = re.search(r"join_team=([A-Za-z0-9_-]+)", created.text).group(1)
        stranger = User.objects.create_user(
            "invite_stranger",
            email="stranger@example.com",
        )
        UserProfile.objects.create(user=stranger, role="buyer")

        denied = accept_team_invite(
            {"token": raw_token},
            stranger,
            "buyer",
        )

        self.assertIn("адресом", denied.text)
        self.assertEqual(
            TeamMember.objects.get(owner=self.owner).status,
            "invited",
        )

    @override_settings(DEBUG=False, SITE_URL="")
    def test_team_invite_is_not_created_with_a_cross_environment_url(self):
        from assistant.seller_actions import invite_team_member

        result = invite_team_member(
            {"email": self.invited.email, "role": "viewer"},
            self.owner,
            "seller",
        )

        self.assertIn("адрес сервиса не настроен", result.text)
        self.assertFalse(TeamMember.objects.filter(owner=self.owner).exists())

    def test_team_invite_link_is_not_saved_in_chat_history(self):
        import json
        import re

        from assistant.rag import execute_action

        conversation = Conversation.objects.create(
            user=self.owner,
            role="seller",
        )
        result = execute_action(
            conversation,
            "invite_team_member",
            {"email": self.invited.email, "role": "viewer"},
            self.owner,
            role="seller",
        )
        raw_token = re.search(
            r"join_team=([A-Za-z0-9_-]+)",
            result["text"],
        ).group(1)
        stored = Message.objects.filter(
            conversation=conversation,
            role=Message.Role.ASSISTANT,
        ).latest("created_at")
        self.assertNotIn(raw_token, stored.content)
        self.assertNotIn(raw_token, json.dumps(stored.cards))


class ChatActionSecurityTests(TestCase):
    def test_buyer_cannot_execute_seller_analytics(self):
        self.assertFalse(can_execute("seller_analytics_hub", "buyer"))
        self.assertFalse(can_execute("seller_executive_report", "buyer"))
        self.assertTrue(can_execute("seller_analytics_hub", "seller"))

    def test_action_exception_details_are_not_returned_to_client(self):
        def failing_action(**_kwargs):
            raise RuntimeError("database-password=do-not-return")

        with patch.dict(
            _REGISTRY,
            {"security_failure_probe": failing_action},
        ):
            result = execute(
                "security_failure_probe",
                {},
                User.objects.create_superuser("security_admin"),
                "admin",
            )

        self.assertNotIn("database-password", result.text)
        self.assertIn("Не удалось выполнить", result.text)

    def test_public_article_list_is_bounded(self):
        from assistant.actions import MAX_SPEC_ARTICLES, _bounded_articles

        articles = [f"OEM-{index:04d}" for index in range(250)]
        bounded = _bounded_articles(articles)

        self.assertEqual(len(bounded), MAX_SPEC_ARTICLES)
        self.assertEqual(bounded[-1], "OEM-0199")


class RfqNotificationAccessTests(TestCase):
    def setUp(self):
        from marketplace.models import RFQ

        self.buyer = User.objects.create_user("rfq_prefix_buyer")
        UserProfile.objects.create(user=self.buyer, role="buyer")
        self.seller = User.objects.create_user("rfq_prefix_seller")
        UserProfile.objects.create(user=self.seller, role="seller")
        self.target = RFQ.objects.create(
            id=12,
            created_by=self.buyer,
            customer_name="Target buyer",
            customer_email="target@example.com",
        )
        self.decoy = RFQ.objects.create(
            id=123,
            created_by=self.buyer,
            customer_name="Decoy buyer",
            customer_email="decoy@example.com",
        )
        self.client.force_login(self.seller)

    def test_notification_for_prefixed_id_does_not_grant_access(self):
        Notification.objects.create(
            user=self.seller,
            kind="rfq",
            title="RFQ 123",
            url=f"/chat/?rfq={self.decoy.id}",
        )

        response = self.client.get(
            f"/api/assistant/rfq/{self.target.id}/"
        )

        self.assertEqual(response.status_code, 404)

    def test_exact_query_parameter_grants_access(self):
        Notification.objects.create(
            user=self.seller,
            kind="rfq",
            title="RFQ 12",
            url=f"/chat/?source=invite&rfq={self.target.id}",
        )

        response = self.client.get(
            f"/api/assistant/rfq/{self.target.id}/"
        )

        self.assertEqual(response.status_code, 200)

    def test_recipient_sees_only_own_items_and_quote_statistics(self):
        other_seller = User.objects.create_user("rfq_competing_seller")
        UserProfile.objects.create(user=other_seller, role="seller")
        category = Category.objects.create(
            name="RFQ private category",
            slug="rfq-private-category",
        )
        own_part = Part.objects.create(
            seller=self.seller,
            category=category,
            title="Own visible part",
            slug="own-visible-part",
            oem_number="OWN-RFQ-001",
            price="100.00",
        )
        competing_part = Part.objects.create(
            seller=other_seller,
            category=category,
            title="Competing secret part",
            slug="competing-secret-part",
            oem_number="SECRET-RFQ-002",
            price="50.00",
        )
        RFQItem.objects.create(
            rfq=self.target,
            query="OWN-RFQ-001 buyer-contact@example.com",
            quantity=1,
            matched_part=own_part,
        )
        RFQItem.objects.create(
            rfq=self.target,
            query="SECRET-RFQ-002",
            quantity=1,
            matched_part=competing_part,
        )
        Quote.objects.create(rfq=self.target, seller=self.seller, total_amount="100.00")
        Quote.objects.create(rfq=self.target, seller=other_seller, total_amount="50.00")
        Notification.objects.create(
            user=self.seller,
            kind="rfq",
            title="RFQ 12",
            url=f"/chat/?rfq={self.target.id}",
        )

        response = self.client.get(f"/api/assistant/rfq/{self.target.id}/")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload["items"]), 1)
        self.assertEqual(payload["quotes_count"], 1)
        self.assertEqual(payload["sent_count"], 1)
        self.assertIn("[контакт скрыт]", payload["items"][0]["article"])
        serialized = str(payload)
        self.assertNotIn("buyer-contact@example.com", serialized)
        self.assertNotIn("SECRET-RFQ-002", serialized)
        self.assertNotIn(other_seller.username, serialized)

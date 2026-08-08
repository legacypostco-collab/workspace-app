from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from assistant.models import Conversation, ConversationParticipant, Message
from marketplace.models import (
    RFQ,
    Category,
    CompanyVerification,
    Notification,
    Order,
    OrderItem,
    Part,
    RFQItem,
    SettlementContract,
    SettlementInvoice,
    SettlementPayment,
    UserProfile,
)


class ControlAccessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser(
            username="control_admin",
            email="admin@example.com",
            password="test-password-123",
        )
        cls.buyer = User.objects.create_user(username="control_buyer", password="test-password-123")
        UserProfile.objects.update_or_create(
            user=cls.buyer, defaults={"role": "buyer", "company_name": "Buyer Ltd"}
        )
        cls.operator = User.objects.create_user(
            username="control_operator", password="test-password-123"
        )
        UserProfile.objects.update_or_create(
            user=cls.operator,
            defaults={"role": "operator", "operator_role": ""},
        )
        cls.finance_operator = User.objects.create_user(
            username="control_finance", password="test-password-123"
        )
        UserProfile.objects.update_or_create(
            user=cls.finance_operator,
            defaults={"role": "operator", "operator_role": "payment"},
        )

    def test_anonymous_is_sent_to_login(self):
        response = self.client.get("/control/")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith("/login/"))

    def test_buyer_cannot_open_control(self):
        self.client.force_login(self.buyer)
        self.assertEqual(self.client.get("/control/").status_code, 403)

    def test_admin_can_render_every_control_section(self):
        self.client.force_login(self.admin)
        for url in (
            "/control/",
            "/control/search/",
            "/control/notifications/",
            "/control/finance/",
            "/control/orders/",
            "/control/users/",
            "/control/moderation/",
            "/control/catalog/",
            "/control/support/",
            "/control/audit/",
            "/control/settings/",
        ):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "операционный центр")

    def test_finance_section_is_limited_to_finance_and_admin(self):
        self.client.force_login(self.operator)
        self.assertEqual(self.client.get("/control/finance/").status_code, 403)

        self.client.force_login(self.finance_operator)
        response = self.client.get("/control/finance/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Финансы")

    def test_view_as_session_does_not_replace_control_actor(self):
        seller = User.objects.create_user(username="viewed_seller")
        UserProfile.objects.update_or_create(user=seller, defaults={"role": "seller"})
        self.client.force_login(self.operator)
        session = self.client.session
        session["op_view_as_id"] = seller.id
        session["op_view_as_originator_id"] = self.operator.id
        session.save()

        response = self.client.get("/control/orders/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "control_operator")


class ControlActionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser(
            username="action_admin",
            email="admin@example.com",
            password="test-password-123",
        )
        cls.buyer = User.objects.create_user(username="action_buyer", password="test-password-123")
        UserProfile.objects.update_or_create(
            user=cls.buyer, defaults={"role": "buyer", "company_name": "Action Buyer"}
        )
        cls.seller = User.objects.create_user(
            username="action_seller", password="test-password-123"
        )
        UserProfile.objects.update_or_create(
            user=cls.seller, defaults={"role": "seller", "company_name": "Action Seller"}
        )
        category = Category.objects.create(name="Control tests", slug="control-tests")
        cls.part = Part.objects.create(
            title="Hydraulic pump",
            slug="control-hydraulic-pump",
            oem_number="CP-CONTROL-001",
            price=Decimal("1000.00"),
            seller=cls.seller,
            category=category,
        )
        cls.order = Order.objects.create(
            customer_name="Action Buyer",
            customer_email="buyer@example.com",
            customer_phone="+70000000000",
            delivery_address="Test address",
            buyer=cls.buyer,
            total_amount=Decimal("1000.00"),
            reserve_amount=Decimal("100.00"),
        )
        OrderItem.objects.create(
            order=cls.order,
            part=cls.part,
            quantity=1,
            unit_price=Decimal("1000.00"),
        )
        contract = SettlementContract.objects.create(
            order=cls.order,
            kind="buyer_sale",
            number="CP-SALE-CONTROL-001",
            status="issued",
            amount=Decimal("1000.00"),
            currency="USD",
            platform_snapshot={},
            counterparty_snapshot={},
            terms_snapshot={},
            created_by=cls.admin,
        )
        cls.invoice = SettlementInvoice.objects.create(
            order=cls.order,
            contract=contract,
            direction="receivable",
            stage="reserve",
            number="CP-INV-CONTROL-001",
            reference_code="PAY-CONTROL-001",
            status="awaiting_confirmation",
            amount=Decimal("100.00"),
            currency="USD",
            due_date=timezone.localdate() + timedelta(days=7),
            created_by=cls.admin,
        )

    def test_admin_confirms_payment_from_control(self):
        self.client.force_login(self.admin)
        response = self.client.post(
            f"/control/finance/{self.invoice.id}/",
            {
                "action": "confirm",
                "amount": "100.00",
                "bank_reference": "BANK-CONTROL-001",
                "paid_at": timezone.localtime().strftime("%Y-%m-%dT%H:%M"),
                "note": "Проверено по выписке",
            },
        )

        self.assertRedirects(
            response,
            f"/control/finance/{self.invoice.id}/",
            fetch_redirect_response=False,
        )
        self.invoice.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(self.invoice.status, "paid")
        self.assertEqual(self.invoice.paid_amount, Decimal("100.00"))
        self.assertEqual(self.order.payment_status, "reserve_paid")
        self.assertTrue(
            SettlementPayment.objects.filter(bank_reference="BANK-CONTROL-001").exists()
        )

    def test_admin_can_block_and_unblock_user(self):
        self.client.force_login(self.admin)
        url = f"/control/users/{self.buyer.id}/"

        self.client.post(url, {"action": "block"})
        self.buyer.refresh_from_db()
        self.assertFalse(self.buyer.is_active)

        self.client.post(url, {"action": "unblock"})
        self.buyer.refresh_from_db()
        self.assertTrue(self.buyer.is_active)

    def test_finance_totals_show_only_outstanding_amount(self):
        self.invoice.status = "partially_paid"
        self.invoice.paid_amount = Decimal("25.00")
        self.invoice.save(update_fields=["status", "paid_amount"])
        self.client.force_login(self.admin)

        response = self.client.get("/control/finance/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["incoming_waiting"], Decimal("75.00"))

    def test_finance_totals_are_separated_by_currency(self):
        SettlementInvoice.objects.create(
            order=self.order,
            contract=self.invoice.contract,
            direction="receivable",
            stage="final",
            number="CP-INV-CONTROL-RUB",
            reference_code="PAY-CONTROL-RUB",
            status="issued",
            amount=Decimal("5000.00"),
            currency="RUB",
            due_date=timezone.localdate() + timedelta(days=10),
            created_by=self.admin,
        )
        self.client.force_login(self.admin)

        response = self.client.get("/control/finance/")

        self.assertEqual(
            response.context["incoming_waiting_totals"],
            [
                {"currency": "RUB", "amount": Decimal("5000.00")},
                {"currency": "USD", "amount": Decimal("100.00")},
            ],
        )

    def test_only_admin_can_change_catalog_publication(self):
        operator = User.objects.create_user(username="catalog_operator")
        UserProfile.objects.update_or_create(
            user=operator,
            defaults={"role": "operator", "operator_role": ""},
        )
        self.client.force_login(operator)
        self.client.post(
            "/control/catalog/",
            {"part_id": self.part.id, "action": "hide"},
        )
        self.part.refresh_from_db()
        self.assertTrue(self.part.is_active)

        self.client.force_login(self.admin)
        self.client.post(
            "/control/catalog/",
            {"part_id": self.part.id, "action": "hide"},
        )
        self.part.refresh_from_db()
        self.assertFalse(self.part.is_active)


class ControlInternalNavigationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser(
            username="navigation_admin",
            email="navigation-admin@example.com",
            password="test-password-123",
        )
        cls.buyer = User.objects.create_user(
            username="navigation_buyer",
            email="navigation-buyer@example.com",
            password="test-password-123",
        )
        UserProfile.objects.update_or_create(
            user=cls.buyer,
            defaults={"role": "buyer", "company_name": "Навигация Покупатель"},
        )
        cls.seller = User.objects.create_user(
            username="navigation_seller",
            email="navigation-seller@example.com",
            password="test-password-123",
        )
        UserProfile.objects.update_or_create(
            user=cls.seller,
            defaults={"role": "seller", "company_name": "Навигация Поставщик"},
        )
        category = Category.objects.create(name="Navigation", slug="navigation")
        part = Part.objects.create(
            title="Navigation part",
            slug="navigation-part",
            oem_number="NAV-001",
            price=Decimal("150.00"),
            seller=cls.seller,
            category=category,
        )
        cls.rfq = RFQ.objects.create(
            created_by=cls.buyer,
            customer_name="Навигация Покупатель",
            customer_email=cls.buyer.email,
            company_name="Навигация Покупатель",
        )
        RFQItem.objects.create(
            rfq=cls.rfq,
            query="NAV-001",
            quantity=2,
            matched_part=part,
            state="auto_matched",
        )
        cls.verification = CompanyVerification.objects.create(
            user=cls.seller,
            status="pending",
            legal_name="Навигация Поставщик",
            inn="7700000000",
            country="RU",
            submitted_at=timezone.now(),
        )
        cls.conversation = Conversation.objects.create(
            user=cls.buyer,
            role="buyer",
            category="support",
            title="Проверка внутренней навигации",
            support_status="waiting_operator",
            support_kind="request",
        )
        ConversationParticipant.objects.create(
            conversation=cls.conversation,
            user=cls.buyer,
            role="buyer",
        )
        Message.objects.create(
            conversation=cls.conversation,
            sender=cls.buyer,
            role=Message.Role.USER,
            content="Нужна помощь с заказом.",
        )

    def setUp(self):
        self.client.force_login(self.admin)

    def test_entity_lists_link_to_control_pages(self):
        response = self.client.get("/control/search/", {"q": "Навигация"})
        self.assertContains(response, f"/control/requests/{self.rfq.id}/")
        self.assertNotContains(response, "run=get_rfq_status")

        response = self.client.get("/control/moderation/")
        self.assertContains(
            response,
            f"/control/moderation/companies/{self.seller.id}/",
        )
        self.assertNotContains(response, "run=op_kyb")

        response = self.client.get("/control/support/")
        self.assertContains(response, f"/control/support/{self.conversation.id}/")
        self.assertNotContains(response, f"/chat/?conversation={self.conversation.id}")

    def test_internal_detail_pages_render(self):
        for url, expected in (
            (f"/control/requests/{self.rfq.id}/", "Позиции заявки"),
            (
                f"/control/moderation/companies/{self.seller.id}/",
                "Лист проверки",
            ),
            (f"/control/support/{self.conversation.id}/", "История обращения"),
        ):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, expected)

    def test_legacy_notification_opens_internal_entity(self):
        notification = Notification.objects.create(
            user=self.admin,
            kind="rfq",
            title="Новая заявка",
            url=f"/chat/?new=1&run=get_rfq_status&rfq_id={self.rfq.id}",
        )

        response = self.client.get(f"/control/notifications/{notification.id}/open/")

        self.assertRedirects(
            response,
            f"/control/requests/{self.rfq.id}/",
            fetch_redirect_response=False,
        )
        notification.refresh_from_db()
        self.assertTrue(notification.is_read)

    def test_operator_can_reply_from_support_page(self):
        response = self.client.post(
            f"/control/support/{self.conversation.id}/",
            {"action": "reply", "content": "Проверили данные, возвращаемся с ответом."},
        )

        self.assertRedirects(
            response,
            f"/control/support/{self.conversation.id}/",
            fetch_redirect_response=False,
        )
        self.conversation.refresh_from_db()
        self.assertEqual(self.conversation.assigned_operator, self.admin)
        self.assertEqual(self.conversation.support_status, "waiting_user")
        self.assertTrue(
            self.conversation.messages.filter(
                sender=self.admin,
                content="Проверили данные, возвращаемся с ответом.",
            ).exists()
        )

    def test_company_can_be_approved_from_control_after_checklist(self):
        checklist = (
            "streetview_ok",
            "reviews_ok",
            "site_ok",
            "bank_ok",
            "certs_ok",
            "messenger_test_ok",
        )
        url = f"/control/moderation/companies/{self.seller.id}/"
        for item in checklist:
            self.client.post(url, {"action": "toggle_check", "item": item})

        response = self.client.post(url, {"action": "approve"})

        self.assertRedirects(response, url, fetch_redirect_response=False)
        self.verification.refresh_from_db()
        self.assertEqual(self.verification.status, "verified")
        self.assertEqual(self.verification.reviewed_by, self.admin)

from decimal import Decimal

from django.contrib.auth.models import User
from django.test import Client, RequestFactory, TestCase

from marketplace.models import (
    Brand,
    Category,
    CompanyVerification,
    Part,
    Quote,
    RFQ,
    RFQItem,
    UserProfile,
    UserRole,
)

from .permissions import _override_allowed, detect_user_role, user_allowed_role_tabs


class MultiRolePermissionsTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.user = User.objects.create_user("multi_seller", password="x")
        UserProfile.objects.create(user=self.user, role="seller")

    def test_primary_role_is_available(self):
        self.assertEqual(detect_user_role(self.user), "seller")
        self.assertEqual(user_allowed_role_tabs(self.user), [{"role": "seller", "label": "Поставщик"}])

    def test_extra_buyer_role_can_be_selected(self):
        UserRole.objects.create(user=self.user, role="buyer")
        request = self.factory.get("/chat/")
        request.user = self.user
        request.session = {"assistant_role_override": "buyer"}

        self.assertTrue(_override_allowed(self.user, "buyer"))
        self.assertEqual(detect_user_role(self.user, request=request), "buyer")
        self.assertEqual(
            user_allowed_role_tabs(self.user),
            [{"role": "seller", "label": "Поставщик"}, {"role": "buyer", "label": "Покупатель"}],
        )

    def test_missing_role_is_rejected(self):
        request = self.factory.get("/chat/")
        request.user = self.user
        request.session = {"assistant_role_override": "operator"}

        self.assertFalse(_override_allowed(self.user, "operator"))
        self.assertEqual(detect_user_role(self.user, request=request), "seller")

    def test_login_activates_an_enabled_extra_role(self):
        buyer = User.objects.create_user("login_multi_buyer", password="secret")
        UserProfile.objects.create(user=buyer, role="buyer")
        UserRole.objects.create(
            user=buyer,
            role="seller",
            is_enabled=True,
        )
        client = Client()

        response = client.post(
            "/api/assistant/action/",
            {
                "action": "start_login",
                "params": {
                    "confirmed": True,
                    "username": buyer.username,
                    "password": "secret",
                    "role": "seller",
                },
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json().get("_post_action"), "reload")
        self.assertEqual(client.session.get("assistant_role_override"), "seller")

    def test_login_does_not_activate_a_disabled_extra_role(self):
        buyer = User.objects.create_user("login_pending_seller", password="secret")
        UserProfile.objects.create(user=buyer, role="buyer")
        UserRole.objects.create(
            user=buyer,
            role="seller",
            is_enabled=False,
        )
        client = Client()

        response = client.post(
            "/api/assistant/action/",
            {
                "action": "start_login",
                "params": {
                    "confirmed": True,
                    "username": buyer.username,
                    "password": "secret",
                    "role": "seller",
                },
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json().get("_post_action"), "reload")
        self.assertIsNone(client.session.get("assistant_role_override"))

    def test_can_extend_current_account_with_buyer_role(self):
        client = Client()
        client.force_login(self.user)

        response = client.post(
            "/api/assistant/action/",
            {
                "action": "add_account_role",
                "params": {
                    "role": "buyer",
                    "confirmed": True,
                    "company_name": "Test Buyer",
                    "contact_name": "Nikita",
                    "country": "RU",
                },
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(UserRole.objects.filter(user=self.user, role="buyer", is_enabled=True).exists())
        self.assertEqual(client.session.get("assistant_role_override"), "buyer")

    def test_seller_extension_requires_operator_approval(self):
        buyer = User.objects.create_user("multi_buyer", password="x")
        UserProfile.objects.create(user=buyer, role="buyer")
        client = Client()
        client.force_login(buyer)

        response = client.post(
            "/api/assistant/action/",
            {
                "action": "add_account_role",
                "params": {
                    "role": "seller",
                    "confirmed": True,
                    "legal_name": "Test Seller LLC",
                    "inn": "7700000000",
                    "contact_name": "Nikita",
                },
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(UserRole.objects.filter(user=buyer, role="seller", is_enabled=False).exists())
        self.assertFalse(any(tab["role"] == "seller" for tab in user_allowed_role_tabs(buyer)))
        self.assertEqual(CompanyVerification.objects.get(user=buyer).status, "pending")

    def test_kyb_approval_enables_pending_seller_role(self):
        from .onboarding import op_kyb_approve

        buyer = User.objects.create_user("buyer_pending_seller", password="x")
        UserProfile.objects.create(user=buyer, role="buyer")
        UserRole.objects.create(user=buyer, role="seller", is_enabled=False)
        CompanyVerification.objects.create(
            user=buyer,
            status="pending",
            legal_name="Pending Seller LLC",
            inn="7700000001",
            operator_checklist={
                "streetview_ok": True,
                "reviews_ok": True,
                "site_ok": True,
                "bank_ok": True,
                "certs_ok": True,
                "messenger_test_ok": True,
            },
        )
        operator = User.objects.create_user("operator_1", password="x", is_staff=True)
        UserProfile.objects.create(user=operator, role="operator")

        result = op_kyb_approve({"user_id": buyer.id}, operator, "operator")

        self.assertIn("KYB одобрен", str(result.text))
        self.assertTrue(UserRole.objects.get(user=buyer, role="seller").is_enabled)
        self.assertTrue(any(tab["role"] == "seller" for tab in user_allowed_role_tabs(buyer)))

    def test_enabled_extra_seller_role_receives_auto_rfq(self):
        from .negotiation import send_rfq_to_suppliers

        buyer = User.objects.create_user("dispatch_buyer", password="x")
        UserProfile.objects.create(user=buyer, role="buyer")
        extra_seller = User.objects.create_user("dispatch_extra_seller", password="x")
        UserProfile.objects.create(user=extra_seller, role="buyer")
        UserRole.objects.create(
            user=extra_seller,
            role="seller",
            is_enabled=True,
        )
        UserProfile.objects.filter(user=extra_seller).update(
            supplier_status="trusted",
        )

        category = Category.objects.create(
            name="Multi-role dispatch",
            slug="multi-role-dispatch",
        )
        brand = Brand.objects.create(
            name="Multi-role brand",
            slug="multi-role-brand",
        )
        primary_part = Part.objects.create(
            title="Primary offer",
            slug="multi-role-primary-offer",
            oem_number="MULTI-RFQ-1",
            category=category,
            brand=brand,
            price=Decimal("8000.00"),
            seller=self.user,
        )
        Part.objects.create(
            title="Extra-role offer",
            slug="multi-role-extra-offer",
            oem_number="MULTI-RFQ-1",
            category=category,
            brand=brand,
            price=Decimal("7900.00"),
            seller=extra_seller,
        )
        rfq = RFQ.objects.create(
            created_by=buyer,
            customer_name=buyer.username,
            customer_email="buyer@example.test",
            mode="auto",
        )
        RFQItem.objects.create(
            rfq=rfq,
            query="MULTI-RFQ-1",
            quantity=1,
            matched_part=primary_part,
            confidence=100,
        )

        result = send_rfq_to_suppliers(
            {"rfq_id": rfq.id, "confirmed": True},
            buyer,
            "buyer",
        )

        self.assertIn("разослан", str(result.text))
        self.assertTrue(
            Quote.objects.filter(
                rfq=rfq,
                seller=extra_seller,
                direction="seller_to_buyer",
            ).exists()
        )

    def test_catalog_match_is_not_dropped_by_sandbox_limit(self):
        from .negotiation import send_rfq_to_suppliers

        buyer = User.objects.create_user("routing_buyer", password="x")
        UserProfile.objects.create(user=buyer, role="buyer")
        for index in range(3):
            trusted = User.objects.create_user(f"routing_trusted_{index}")
            UserProfile.objects.create(
                user=trusted,
                role="seller",
                supplier_status="trusted",
            )
        for index in range(4):
            unrelated = User.objects.create_user(f"routing_sandbox_{index}")
            UserProfile.objects.create(
                user=unrelated,
                role="seller",
                supplier_status="sandbox",
            )

        exact = User.objects.create_user("routing_exact_sandbox")
        UserProfile.objects.create(
            user=exact,
            role="seller",
            supplier_status="sandbox",
        )
        category = Category.objects.create(
            name="Routing category",
            slug="routing-category",
        )
        brand = Brand.objects.create(
            name="Routing brand",
            slug="routing-brand",
        )
        part = Part.objects.create(
            title="Exact sandbox offer",
            slug="routing-exact-sandbox",
            oem_number="ROUTING-EXACT-1",
            category=category,
            brand=brand,
            price=Decimal("125.00"),
            seller=exact,
        )
        rfq = RFQ.objects.create(
            created_by=buyer,
            customer_name=buyer.username,
            customer_email="routing@example.test",
            mode="auto",
        )
        RFQItem.objects.create(
            rfq=rfq,
            query="ROUTING-EXACT-1",
            quantity=2,
            matched_part=part,
            confidence=100,
        )

        send_rfq_to_suppliers(
            {"rfq_id": rfq.id, "confirmed": True},
            buyer,
            "buyer",
        )

        self.assertTrue(
            Quote.objects.filter(
                rfq=rfq,
                seller=exact,
                direction="seller_to_buyer",
            ).exists()
        )

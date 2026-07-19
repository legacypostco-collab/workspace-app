from django.contrib.auth.models import User
from django.test import Client, RequestFactory, TestCase

from marketplace.models import CompanyVerification, UserProfile, UserRole

from .permissions import _override_allowed, detect_user_role, user_allowed_role_tabs


class MultiRolePermissionsTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.user = User.objects.create_user("multi_seller", password="x")
        UserProfile.objects.create(user=self.user, role="seller")

    def test_primary_role_is_available(self):
        self.assertEqual(detect_user_role(self.user), "seller")
        self.assertEqual(user_allowed_role_tabs(self.user), [{"role": "seller", "label": "Продавец"}])

    def test_extra_buyer_role_can_be_selected(self):
        UserRole.objects.create(user=self.user, role="buyer")
        request = self.factory.get("/chat/")
        request.user = self.user
        request.session = {"assistant_role_override": "buyer"}

        self.assertTrue(_override_allowed(self.user, "buyer"))
        self.assertEqual(detect_user_role(self.user, request=request), "buyer")
        self.assertEqual(
            user_allowed_role_tabs(self.user),
            [{"role": "seller", "label": "Продавец"}, {"role": "buyer", "label": "Покупатель"}],
        )

    def test_missing_role_is_rejected(self):
        request = self.factory.get("/chat/")
        request.user = self.user
        request.session = {"assistant_role_override": "operator"}

        self.assertFalse(_override_allowed(self.user, "operator"))
        self.assertEqual(detect_user_role(self.user, request=request), "seller")

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

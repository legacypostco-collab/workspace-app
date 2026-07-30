from django.contrib.auth.models import User
from django.test import TestCase

from marketplace.models import Order, OrderClaim, UserProfile


class ClaimStatusSecurityTests(TestCase):
    def setUp(self):
        self.buyer = User.objects.create_user("claim_status_buyer")
        UserProfile.objects.create(user=self.buyer, role="buyer")
        self.operator = User.objects.create_user("claim_status_operator")
        UserProfile.objects.create(user=self.operator, role="operator")
        self.order = Order.objects.create(
            buyer=self.buyer,
            customer_name="Buyer",
            customer_email="buyer@example.com",
            customer_phone="",
            delivery_address="Address",
            status="delivered",
            total_amount="1000.00",
        )
        self.claim = OrderClaim.objects.create(
            order=self.order,
            opened_by=self.buyer,
            title="Damaged part",
            description="Damage found on delivery",
            status="open",
        )

    def test_removed_claim_mutation_rejects_buyer(self):
        self.client.force_login(self.buyer)

        response = self.client.post(
            f"/claims/{self.claim.id}/status/",
            {"status": "approved"},
        )

        self.claim.refresh_from_db()
        self.assertEqual(response.status_code, 410)
        self.assertEqual(self.claim.status, "open")
        self.assertIsNone(self.claim.resolved_by)

    def test_removed_claim_mutation_rejects_operator(self):
        self.client.force_login(self.operator)

        for status in ("in_review", "approved"):
            with self.subTest(status=status):
                response = self.client.post(
                    f"/claims/{self.claim.id}/status/",
                    {"status": status},
                )
                self.assertEqual(response.status_code, 410)

        self.claim.refresh_from_db()
        self.assertEqual(self.claim.status, "open")
        self.assertIsNone(self.claim.reviewed_by)
        self.assertIsNone(self.claim.resolved_by)

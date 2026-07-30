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

    def test_buyer_cannot_approve_own_claim(self):
        self.client.force_login(self.buyer)

        response = self.client.post(
            f"/claims/{self.claim.id}/status/",
            {"status": "approved"},
        )

        self.claim.refresh_from_db()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.claim.status, "open")
        self.assertIsNone(self.claim.resolved_by)

    def test_operator_can_follow_claim_transition_but_cannot_skip_stage(self):
        self.client.force_login(self.operator)

        skipped = self.client.post(
            f"/claims/{self.claim.id}/status/",
            {"status": "approved"},
        )
        self.claim.refresh_from_db()
        self.assertEqual(skipped.status_code, 302)
        self.assertEqual(self.claim.status, "open")

        reviewed = self.client.post(
            f"/claims/{self.claim.id}/status/",
            {"status": "in_review"},
        )
        approved = self.client.post(
            f"/claims/{self.claim.id}/status/",
            {"status": "approved"},
        )

        self.claim.refresh_from_db()
        self.assertEqual(reviewed.status_code, 302)
        self.assertEqual(approved.status_code, 302)
        self.assertEqual(self.claim.status, "approved")
        self.assertEqual(self.claim.reviewed_by_id, self.operator.id)
        self.assertEqual(self.claim.resolved_by_id, self.operator.id)

from django.contrib.auth.models import User
from django.test import TestCase

from marketplace.models import Category, Part, RFQ, RFQItem, UserProfile


class SellerRequestsApiTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(username="seller_requests_api", password="pass12345")
        UserProfile.objects.create(
            user=self.seller,
            role="seller",
            company_name="Requests API Supplier",
            can_manage_orders=True,
        )
        self.client.login(username="seller_requests_api", password="pass12345")
        self.category = Category.objects.create(name="Requests API Category", slug="requests-api-category")
        self.part = Part.objects.create(
            seller=self.seller,
            title="Request API Part",
            slug="request-api-part",
            oem_number="REQ-API-001",
            description="RFQ matched part",
            price="120.00",
            stock_quantity=5,
            category=self.category,
        )
        self.rfq = RFQ.objects.create(
            customer_name="Requests Buyer",
            customer_email="requests_buyer@example.com",
            company_name="Requests Co",
            status="new",
        )
        self.rfq_item = RFQItem.objects.create(
            rfq=self.rfq,
            query="REQ-API-001",
            quantity=3,
            matched_part=self.part,
            state="auto_matched",
        )

    def test_seller_requests_list_endpoint(self):
        response = self.client.get("/api/v1/seller/requests/")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body["items"]), 1)
        self.assertEqual(body["items"][0]["id"], self.rfq.id)
        self.assertEqual(body["items"][0]["seller_items_count"], 1)

    def test_seller_request_detail_endpoint(self):
        response = self.client.get(f"/api/v1/seller/requests/{self.rfq.id}/")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["id"], self.rfq.id)
        self.assertEqual(len(body["items"]), 1)
        self.assertEqual(body["items"][0]["query"], "REQ-API-001")

    def test_seller_request_quote_endpoint(self):
        response = self.client.post(
            f"/api/v1/seller/requests/{self.rfq.id}/quote/",
            data={"comment": "ready to quote"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.rfq.refresh_from_db()
        self.rfq_item.refresh_from_db()
        self.assertEqual(self.rfq.status, "quoted")
        self.assertIn("seller_quote", self.rfq_item.decision_reason)

    def test_seller_request_decline_endpoint(self):
        response = self.client.post(
            f"/api/v1/seller/requests/{self.rfq.id}/decline/",
            data={"reason": "not available"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.rfq.refresh_from_db()
        self.rfq_item.refresh_from_db()
        self.assertEqual(self.rfq.status, "cancelled")
        self.assertIn("seller_decline", self.rfq_item.decision_reason)

    def test_seller_request_renegotiate_endpoint(self):
        response = self.client.post(
            f"/api/v1/seller/requests/{self.rfq.id}/renegotiate/",
            data={"comment": "need operator review"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.rfq.refresh_from_db()
        self.rfq_item.refresh_from_db()
        self.assertEqual(self.rfq.status, "needs_review")
        self.assertIn("seller_renegotiate", self.rfq_item.decision_reason)

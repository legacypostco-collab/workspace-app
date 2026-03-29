from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from marketplace.models import Category, Order, OrderClaim, OrderEvent, OrderItem, Part, UserProfile


class SellerOrdersApiTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(username="seller_orders_api", password="pass12345")
        UserProfile.objects.create(
            user=self.seller,
            role="seller",
            company_name="Orders API Supplier",
            can_manage_orders=True,
        )
        self.client.login(username="seller_orders_api", password="pass12345")
        self.category = Category.objects.create(name="Orders API Category", slug="orders-api-category")
        self.part = Part.objects.create(
            seller=self.seller,
            title="Orders API Part",
            slug="orders-api-part",
            oem_number="ORD-API-001",
            description="Order matched part",
            price="210.00",
            stock_quantity=4,
            category=self.category,
        )
        self.order = Order.objects.create(
            customer_name="Orders Buyer",
            customer_email="orders_buyer@example.com",
            customer_phone="+1000000002",
            delivery_address="Riyadh",
            status="confirmed",
            payment_status="reserve_paid",
            total_amount="420.00",
            reserve_amount="42.00",
            supplier_confirm_deadline=timezone.now() + timedelta(hours=2),
        )
        self.order_item = OrderItem.objects.create(order=self.order, part=self.part, quantity=2, unit_price="210.00")
        self.event = OrderEvent.objects.create(
            order=self.order,
            event_type="order_created",
            source="system",
        )
        self.claim = OrderClaim.objects.create(
            order=self.order,
            title="Damaged package",
            description="Package has visible damage",
            status="open",
            opened_by=self.seller,
        )

    def test_seller_orders_list_endpoint(self):
        response = self.client.get("/api/v1/seller/orders/")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body["items"]), 1)
        self.assertEqual(body["items"][0]["id"], self.order.id)
        self.assertEqual(body["items"][0]["items_count"], 1)

    def test_seller_order_detail_and_timeline_endpoints(self):
        detail_response = self.client.get(f"/api/v1/seller/orders/{self.order.id}/")
        self.assertEqual(detail_response.status_code, 200)
        detail = detail_response.json()
        self.assertEqual(detail["id"], self.order.id)
        self.assertEqual(len(detail["seller_items"]), 1)
        self.assertEqual(len(detail["claims"]), 1)

        timeline_response = self.client.get(f"/api/v1/seller/orders/{self.order.id}/timeline/")
        self.assertEqual(timeline_response.status_code, 200)
        timeline = timeline_response.json()
        self.assertEqual(timeline["order_id"], self.order.id)
        self.assertTrue(len(timeline["items"]) >= 1)

    def test_seller_order_action_endpoint(self):
        response = self.client.post(
            f"/api/v1/seller/orders/{self.order.id}/action/",
            data={"status": "in_production"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "in_production")

    def test_seller_claims_and_claim_respond_endpoints(self):
        claims_response = self.client.get("/api/v1/seller/orders/claims/")
        self.assertEqual(claims_response.status_code, 200)
        claims = claims_response.json()
        self.assertEqual(len(claims["items"]), 1)
        self.assertEqual(claims["items"][0]["id"], self.claim.id)

        respond_response = self.client.post(
            f"/api/v1/seller/orders/claims/{self.claim.id}/respond/",
            data={"status": "in_review", "comment": "checking with warehouse"},
            content_type="application/json",
        )
        self.assertEqual(respond_response.status_code, 200)
        self.claim.refresh_from_db()
        self.assertEqual(self.claim.status, "in_review")
        self.assertIn("checking with warehouse", self.claim.description)


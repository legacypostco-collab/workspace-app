from django.contrib.auth.models import User
from django.test import TestCase

from marketplace.models import Category, Order, OrderItem, Part, RFQ, RFQItem, UserProfile


class SellerProductApiTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(username="seller_api", password="pass12345")
        UserProfile.objects.create(
            user=self.seller,
            role="seller",
            company_name="API Supplier",
            can_manage_assortment=True,
            can_manage_pricing=True,
        )
        self.client.login(username="seller_api", password="pass12345")
        self.category = Category.objects.create(name="API Category", slug="api-category")
        self.part = Part.objects.create(
            seller=self.seller,
            title="API Part",
            slug="api-part",
            oem_number="API-001",
            description="API detail",
            price="110.00",
            stock_quantity=7,
            category=self.category,
            availability_status="active",
        )

    def test_seller_products_list_endpoint(self):
        response = self.client.get("/api/v1/seller/products/")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body["items"]), 1)
        self.assertEqual(body["items"][0]["title"], "API Part")
        self.assertIn("stale", body["items"][0])
        self.assertIn("demand", body["items"][0])

    def test_seller_product_detail_endpoint(self):
        response = self.client.get(f"/api/v1/seller/products/{self.part.id}/")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["id"], self.part.id)
        self.assertEqual(body["title"], "API Part")
        self.assertIn("stale", body)
        self.assertIn("demand", body)

    def test_seller_product_bulk_action_endpoint(self):
        response = self.client.post(
            "/api/v1/seller/products/bulk-action/",
            data={"action": "status", "part_ids": [self.part.id], "availability_status": "limited"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.part.refresh_from_db()
        self.assertEqual(self.part.availability_status, "limited")

    def test_seller_product_price_history_and_demand_endpoints(self):
        rfq = RFQ.objects.create(customer_name="API Buyer", customer_email="api_buyer@example.com")
        RFQItem.objects.create(rfq=rfq, query="API-001", quantity=2, matched_part=self.part, state="auto_matched")
        order = Order.objects.create(
            customer_name="Order Buyer",
            customer_email="order_buyer@example.com",
            customer_phone="+1000000001",
            delivery_address="Riyadh",
            status="completed",
            payment_status="paid",
            total_amount="220.00",
        )
        OrderItem.objects.create(order=order, part=self.part, quantity=2, unit_price="110.00")

        history_response = self.client.get(f"/api/v1/seller/products/{self.part.id}/price-history/")
        self.assertEqual(history_response.status_code, 200)
        history = history_response.json()
        self.assertEqual(history["part_id"], self.part.id)
        self.assertTrue(len(history["items"]) >= 1)

        demand_response = self.client.get(f"/api/v1/seller/products/{self.part.id}/demand/")
        self.assertEqual(demand_response.status_code, 200)
        demand = demand_response.json()
        self.assertEqual(demand["rfq_count"], 1)
        self.assertEqual(demand["orders_count"], 1)

    def test_seller_product_export_endpoint(self):
        response = self.client.get("/api/v1/seller/products/export/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response["Content-Type"])
        self.assertIn("API Part", response.content.decode("utf-8"))

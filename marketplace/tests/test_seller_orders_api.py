from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from marketplace.models import (
    Category,
    Order,
    OrderClaim,
    OrderDocument,
    OrderEvent,
    OrderItem,
    Part,
    TeamMember,
    UserProfile,
)


class SellerOrdersApiTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(username="seller_orders_api", password="pass12345")
        UserProfile.objects.create(
            user=self.seller,
            role="seller",
            company_name="Orders API Supplier",
            can_manage_orders=True,
        )
        self.client.force_login(self.seller)
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

    def _add_second_seller(self):
        second_seller = User.objects.create_user(
            username="seller_orders_api_other",
            password="pass12345",
        )
        UserProfile.objects.create(
            user=second_seller,
            role="seller",
            company_name="Other Supplier",
            can_manage_orders=True,
        )
        second_part = Part.objects.create(
            seller=second_seller,
            title="Other Seller Secret Part",
            slug="orders-api-other-part",
            oem_number="OTHER-SECRET-OEM",
            description="Must never be exposed to another seller",
            price="999.00",
            stock_quantity=2,
            category=self.category,
        )
        second_item = OrderItem.objects.create(
            order=self.order,
            part=second_part,
            quantity=1,
            unit_price="999.00",
        )
        self.order.total_amount = "1419.00"
        self.order.reserve_amount = "141.90"
        self.order.save(update_fields=["total_amount", "reserve_amount"])
        return second_seller, second_item

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

    def test_repeating_current_status_does_not_duplicate_event(self):
        before = self.order.events.count()

        response = self.client.post(
            f"/api/v1/seller/orders/{self.order.id}/action/",
            data={"status": "confirmed"},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["no_change"])
        self.assertEqual(self.order.events.count(), before)

    def test_seller_cannot_mark_delivery_or_cancel_through_api(self):
        for target in ("delivered", "cancelled", "shipped"):
            with self.subTest(target=target):
                response = self.client.post(
                    f"/api/v1/seller/orders/{self.order.id}/action/",
                    data={"status": target},
                    content_type="application/json",
                )
                self.assertEqual(response.status_code, 400)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, "confirmed")

    def test_legacy_seller_status_route_is_removed(self):
        response = self.client.get(f"/seller/orders/{self.order.id}/status/")
        self.assertEqual(response.status_code, 410)
        self.assertEqual(response["Cache-Control"], "no-store")

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

    def test_seller_cannot_resolve_or_close_claim(self):
        for status in ("approved", "rejected", "closed"):
            with self.subTest(status=status):
                response = self.client.post(
                    f"/api/v1/seller/orders/claims/{self.claim.id}/respond/",
                    data={"status": status, "comment": "self resolution"},
                    content_type="application/json",
                )
                self.assertEqual(response.status_code, 403)

        self.claim.refresh_from_db()
        self.assertEqual(self.claim.status, "open")
        self.assertNotIn("self resolution", self.claim.description)

    def test_multi_seller_order_is_scoped_to_current_seller(self):
        second_seller, _second_item = self._add_second_seller()
        own_event = OrderEvent.objects.create(
            order=self.order,
            event_type="status_changed",
            source="seller",
            actor=self.seller,
            meta={"to": "in_production"},
        )
        hidden_event = OrderEvent.objects.create(
            order=self.order,
            event_type="status_changed",
            source="seller",
            actor=second_seller,
            meta={"to": "ready_to_ship", "secret": "other seller event"},
        )
        OrderDocument.objects.create(
            order=self.order,
            doc_type="invoice",
            title="Current seller invoice",
            uploaded_by=self.seller,
        )
        hidden_document = OrderDocument.objects.create(
            order=self.order,
            doc_type="invoice",
            title="Other seller secret invoice",
            uploaded_by=second_seller,
        )
        hidden_claim = OrderClaim.objects.create(
            order=self.order,
            title="Buyer claim for operator routing",
            description="Not assigned to a seller",
            status="open",
        )

        list_response = self.client.get("/api/v1/seller/orders/")
        self.assertEqual(list_response.status_code, 200)
        listed = list_response.json()["items"][0]
        self.assertEqual(listed["total_amount"], "420.00")
        self.assertEqual(listed["reserve_amount"], "42.00")
        self.assertEqual(listed["items_count"], 1)
        self.assertEqual(
            [item["part_oem"] for item in listed["seller_items"]],
            ["ORD-API-001"],
        )

        detail_response = self.client.get(
            f"/api/v1/seller/orders/{self.order.id}/",
        )
        self.assertEqual(detail_response.status_code, 200)
        detail = detail_response.json()
        serialized = str(detail)
        self.assertNotIn("OTHER-SECRET-OEM", serialized)
        self.assertNotIn(hidden_document.title, serialized)
        self.assertNotIn(hidden_claim.title, serialized)
        self.assertNotIn("other seller event", serialized)
        self.assertIn(own_event.id, [event["id"] for event in detail["events"]])
        self.assertNotIn(hidden_event.id, [event["id"] for event in detail["events"]])

        timeline_response = self.client.get(
            f"/api/v1/seller/orders/{self.order.id}/timeline/",
        )
        timeline_ids = [
            event["id"]
            for event in timeline_response.json()["items"]
        ]
        self.assertIn(own_event.id, timeline_ids)
        self.assertNotIn(hidden_event.id, timeline_ids)

        action_response = self.client.post(
            f"/api/v1/seller/orders/{self.order.id}/action/",
            data={"status": "in_production"},
            content_type="application/json",
        )
        self.assertEqual(action_response.status_code, 409)

    def test_active_team_member_uses_owner_seller_scope(self):
        member = User.objects.create_user(
            username="seller_orders_team_member",
            password="pass12345",
        )
        UserProfile.objects.create(
            user=member,
            role="seller",
            can_manage_orders=True,
        )
        TeamMember.objects.create(
            owner=self.seller,
            user=member,
            invited_email="seller-team@example.com",
            role="manager",
            status="active",
        )
        self.client.force_login(member)

        response = self.client.get("/api/v1/seller/orders/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"][0]["id"], self.order.id)

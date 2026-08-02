from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from assistant.embeddings import knowledge_chunks_for_user
from assistant.actions import get_claims
from assistant.claims import claim_detail
from assistant.indexer import index_order
from assistant.seller_actions import (
    _autolink_orders,
    customer_detail,
    kam_deals,
    link_order_to_customer,
)
from assistant.models import KnowledgeChunk
from marketplace.models import (
    Category,
    CompanyVerification,
    Customer,
    Order,
    OrderClaim,
    OrderItem,
    Part,
    UserProfile,
    participant_public_code,
)
from marketplace.participant_identity import customer_label, partner_label


@override_settings(DEBUG=True, EMBEDDING_PROVIDER="stub")
class ParticipantAnonymityTests(TestCase):
    def setUp(self):
        self.buyer = User.objects.create_user(
            username="private_buyer",
            email="private-buyer@example.com",
            first_name="Buyer",
            last_name="Secret",
        )
        self.other_buyer = User.objects.create_user(username="other_buyer")
        self.seller = User.objects.create_user(
            username="private_seller",
            email="private-seller@example.com",
        )
        for user, role in (
            (self.buyer, "buyer"),
            (self.other_buyer, "buyer"),
            (self.seller, "seller"),
        ):
            UserProfile.objects.create(user=user, role=role)

        category = Category.objects.create(name="Private category", slug="private-category")
        part = Part.objects.create(
            seller=self.seller,
            title="Private part",
            slug="private-part",
            oem_number="PRIVATE-001",
            price="100.00",
            category=category,
        )
        self.order = Order.objects.create(
            buyer=self.buyer,
            customer_name="Buyer Secret",
            customer_email="private-buyer@example.com",
            total_amount="100.00",
        )
        OrderItem.objects.create(
            order=self.order,
            part=part,
            quantity=1,
            unit_price="100.00",
        )

    def test_public_codes_are_stable_and_role_specific(self):
        partner = partner_label(self.seller)
        customer = customer_label(self.seller)

        self.seller.profile.save()
        self.seller.refresh_from_db()

        self.assertEqual(partner_label(self.seller), partner)
        self.assertEqual(customer_label(self.seller), customer)
        self.assertRegex(partner, r"^Партнёр CP · \d{4}$")
        self.assertRegex(customer, r"^Заказчик CP · \d{4}$")
        self.assertNotEqual(partner.rsplit(" ", 1)[-1], customer.rsplit(" ", 1)[-1])

    def test_public_codes_do_not_repeat_after_four_digit_range(self):
        for role in ("buyer", "seller"):
            self.assertNotEqual(
                participant_public_code(1, role),
                participant_public_code(9001, role),
            )
            self.assertNotEqual(
                participant_public_code(9001, role),
                participant_public_code(999001, role),
            )

    def test_order_index_is_anonymous_and_scoped_to_participants(self):
        chunk = index_order(self.order)

        self.assertNotIn(self.buyer.username, chunk.content)
        self.assertNotIn(self.buyer.email, chunk.content)
        self.assertNotIn(self.seller.username, chunk.content)
        self.assertIn(customer_label(self.buyer), chunk.content)
        self.assertIn(partner_label(self.seller), chunk.content)

        buyer_ids = set(knowledge_chunks_for_user(
            KnowledgeChunk.objects.all(), role="buyer", user=self.buyer,
        ).values_list("id", flat=True))
        other_buyer_ids = set(knowledge_chunks_for_user(
            KnowledgeChunk.objects.all(), role="buyer", user=self.other_buyer,
        ).values_list("id", flat=True))
        seller_ids = set(knowledge_chunks_for_user(
            KnowledgeChunk.objects.all(), role="seller", user=self.seller,
        ).values_list("id", flat=True))

        self.assertIn(chunk.id, buyer_ids)
        self.assertNotIn(chunk.id, other_buyer_ids)
        self.assertIn(chunk.id, seller_ids)

    def test_seller_claim_views_hide_buyer_identity_and_contacts(self):
        claim = OrderClaim.objects.create(
            order=self.order,
            kind="defect",
            title="Позвоните +7 999 123-45-67",
            description="private-buyer@example.com",
            opened_by=self.buyer,
            status="open",
        )

        listing = get_claims({}, self.seller, "seller")
        detail = claim_detail({"claim_id": claim.id}, self.seller, "seller")
        serialized = str(listing.cards) + str(detail.cards)

        self.assertIn(customer_label(self.buyer), serialized)
        self.assertIn("[контакт скрыт]", serialized)
        self.assertNotIn(self.buyer.username, serialized)
        self.assertNotIn(self.buyer.email, serialized)
        self.assertNotIn("+7 999 123-45-67", serialized)

    def test_unrelated_role_cannot_list_or_open_claims(self):
        claim = OrderClaim.objects.create(
            order=self.order,
            kind="defect",
            title="Private claim",
            opened_by=self.buyer,
            status="open",
        )

        listing = get_claims({}, self.other_buyer, "guest")
        detail = claim_detail({"claim_id": claim.id}, self.other_buyer, "buyer")

        self.assertIn("ограничен", listing.text)
        self.assertIn("ограничен", detail.text)

    def test_customer_order_link_requires_accepted_customer_account(self):
        customer = Customer.objects.create(
            owner=self.seller,
            inn="7701234567",
            name="Buyer Secret",
        )
        CompanyVerification.objects.create(
            user=self.buyer,
            inn=customer.inn,
            legal_name=customer.name,
            status="verified",
        )

        self.assertEqual(_autolink_orders(customer), 0)
        denied = link_order_to_customer(
            {"id": customer.id, "order_id": self.order.id},
            self.seller,
            "seller",
        )
        self.order.refresh_from_db()
        self.assertIn("принять приглашение", denied.text)
        self.assertIsNone(self.order.customer_ref_id)

        customer.user = self.buyer
        customer.save(update_fields=["user"])
        link_order_to_customer(
            {"id": customer.id, "order_id": self.order.id},
            self.seller,
            "seller",
        )
        self.order.refresh_from_db()
        self.assertEqual(self.order.customer_ref_id, customer.id)
        self.assertEqual(self.order.assigned_kam_id, self.seller.id)

    def test_customer_cannot_claim_unrelated_order_by_id(self):
        customer = Customer.objects.create(
            owner=self.seller,
            user=self.buyer,
            inn="7707654321",
            name="Accepted customer",
        )
        unrelated = Order.objects.create(
            buyer=self.other_buyer,
            customer_name="Other private buyer",
            customer_email="other-private@example.com",
            total_amount="200.00",
        )

        result = link_order_to_customer(
            {"id": customer.id, "order_id": unrelated.id},
            self.seller,
            "seller",
        )
        unrelated.refresh_from_db()

        self.assertIn("не относится", result.text)
        self.assertIsNone(unrelated.customer_ref_id)
        self.assertIsNone(unrelated.assigned_kam_id)

    def test_unaccepted_legacy_customer_link_does_not_expose_order(self):
        customer = Customer.objects.create(
            owner=self.seller,
            inn="7700000001",
            name="Legacy unaccepted customer",
        )
        self.order.customer_ref = customer
        self.order.assigned_kam = self.seller
        self.order.save(update_fields=["customer_ref", "assigned_kam"])

        detail = customer_detail(
            {"id": customer.id},
            self.seller,
            "seller",
        )
        deals = kam_deals({}, self.seller, "seller")
        serialized = str(detail.cards) + str(deals.cards)

        self.assertNotIn(f"Заказ #{self.order.id}", serialized)
        self.assertNotIn(f"ORD-{self.order.id}", serialized)

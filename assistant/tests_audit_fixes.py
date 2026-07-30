"""Регресс-тесты для багов из пред-тестового аудита (2026-06-16, коммит 59449811).

Запуск:
    python manage.py test assistant.tests_audit_fixes -v 2

Закрывают: IDOR (get_rfq_status/generate_proposal/view_rfq_quotes для чужого/анон),
приём протухшей котировки, Order из 0 позиций, двойной клик confirm_kp_and_reserve.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.test import TestCase
from django.utils import timezone

from assistant.actions import execute
from assistant.models import Wallet
from marketplace.models import (
    Brand, Category, Order, Part, Quote, QuoteItem, RFQ, RFQItem, UserProfile,
)

User = get_user_model()


class AuditFixesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.buyerA = User.objects.create_user("aud_buyerA", password="x", email="a@x.com")
        UserProfile.objects.create(user=cls.buyerA, role="buyer")
        cls.buyerB = User.objects.create_user("aud_buyerB", password="x", email="b@x.com")
        UserProfile.objects.create(user=cls.buyerB, role="buyer")
        cls.seller = User.objects.create_user("aud_seller", password="x")
        UserProfile.objects.create(user=cls.seller, role="seller")

        cls.brand = Brand.objects.create(name="AudBrand")
        cls.cat = Category.objects.create(name="AudCat", slug="audcat")
        cls.part = Part.objects.create(
            brand=cls.brand, category=cls.cat, oem_number="AUD-1",
            title="Aud part", slug="aud-1", seller=cls.seller, price=Decimal("1000"),
        )
        w = Wallet.for_user(cls.buyerA)
        w.balance = Decimal("1000000"); w.save(update_fields=["balance"])

        # RFQ покупателя A + сматченная позиция + котировка продавца
        cls.rfqA = RFQ.objects.create(
            created_by=cls.buyerA, customer_name="A", customer_email="a@x.com",
            mode="auto", status="quoted",
        )
        cls.itemA = RFQItem.objects.create(rfq=cls.rfqA, query="Aud part", matched_part=cls.part, quantity=2)
        # Сумма выше платформенного минимума заказа ($7000), чтобы тест проверял
        # именно целевые гейты (срок/0 позиций/double-click), а не min_order.
        cls.quoteA = Quote.objects.create(
            rfq=cls.rfqA, seller=cls.seller, status="submitted",
            total_amount=Decimal("8000"), valid_until=timezone.now() + timedelta(days=5),
        )
        QuoteItem.objects.create(quote=cls.quoteA, part=cls.part, rfq_item=cls.itemA,
                                 quantity=2, unit_price=Decimal("4000"))

    # ── IDOR: чужой RFQ ───────────────────────────────────────────
    def test_idor_get_rfq_status_foreign(self):
        r = execute("get_rfq_status", {"rfq_id": self.rfqA.id}, self.buyerB, "buyer")
        self.assertIn("не найден", r.text.lower())

    def test_idor_generate_proposal_foreign(self):
        r = execute("generate_proposal", {"rfq_id": self.rfqA.id}, self.buyerB, "buyer")
        self.assertIn("не найден", r.text.lower())

    def test_generate_proposal_uses_real_quote_and_private_pdf(self):
        r = execute(
            "generate_proposal",
            {"rfq_id": self.rfqA.id},
            self.buyerA,
            "buyer",
        )

        self.assertIn(f"PRO-{self.rfqA.id}/{self.quoteA.id}", r.text)
        self.assertTrue(r.cards)
        self.assertIn(
            f"/api/assistant/rfq/{self.rfqA.id}/quotes/{self.quoteA.id}/proforma.pdf",
            str(r.actions),
        )
        self.assertNotIn("/chat/proposal/", str(r.actions))
        self.assertNotIn("/proposal/pdf/", str(r.actions))

    def test_legacy_rfq_checkout_and_proposal_routes_cannot_mutate(self):
        self.client.force_login(self.buyerA)
        before = Order.objects.filter(buyer=self.buyerA).count()
        expected = (
            f"/chat/?new=1&run=get_rfq_status&rfq_id={self.rfqA.id}"
        )

        for suffix in (
            "",
            "proposal/",
            "proposal/pdf/",
            "proposal/logistics/",
            "checkout/",
        ):
            url = f"/rfq/{self.rfqA.id}/{suffix}"
            with self.subTest(url=url):
                response = self.client.post(url, {"customer_name": "Bypass"})
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response["Location"], expected)

        response = self.client.get(f"/chat/proposal/{self.rfqA.id}/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], expected)
        self.assertEqual(Order.objects.filter(buyer=self.buyerA).count(), before)

    def test_owner_get_rfq_status_ok(self):
        # Контроль: сам владелец видит свой RFQ
        r = execute("get_rfq_status", {"rfq_id": self.rfqA.id}, self.buyerA, "buyer")
        self.assertNotIn("не найден", r.text.lower())

    # ── Аноним не видит чужие котировки ───────────────────────────
    def test_anon_view_rfq_quotes_blocked(self):
        r = execute("view_rfq_quotes", {"rfq_id": self.rfqA.id}, AnonymousUser(), "buyer")
        self.assertTrue("войдите" in r.text.lower() or "аккаунт" in r.text.lower())

    # ── Протухшая котировка ───────────────────────────────────────
    def test_accept_quote_expired_blocked(self):
        self.quoteA.valid_until = timezone.now() - timedelta(hours=1)
        self.quoteA.save(update_fields=["valid_until"])
        r = execute("accept_quote", {"quote_id": self.quoteA.id, "confirmed": False}, self.buyerA, "buyer")
        self.assertIn("истекл", r.text.lower())

    # ── Order из 0 позиций (несматченное КП) ──────────────────────
    def test_accept_quote_zero_items_blocked(self):
        rfq = RFQ.objects.create(created_by=self.buyerA, customer_name="A",
                                 customer_email="a@x.com", mode="auto", status="quoted")
        q = Quote.objects.create(rfq=rfq, seller=self.seller, status="submitted",
                                 total_amount=Decimal("8000"),
                                 valid_until=timezone.now() + timedelta(days=5))
        QuoteItem.objects.create(quote=q, part=None, quantity=1, unit_price=Decimal("8000"),
                                 title_snapshot="свободная позиция без матча")
        before = Order.objects.filter(buyer=self.buyerA).count()
        r = execute("accept_quote", {"quote_id": q.id, "confirmed": True}, self.buyerA, "buyer")
        self.assertIn("некорректные", r.text.lower())
        self.assertEqual(Order.objects.filter(buyer=self.buyerA).count(), before)

    def test_accept_quote_rejects_tampered_total_and_unmatched_item(self):
        before = Order.objects.filter(buyer=self.buyerA).count()
        self.quoteA.total_amount = Decimal("9000")
        self.quoteA.save(update_fields=["total_amount"])

        bad_total = execute(
            "accept_quote",
            {"quote_id": self.quoteA.id, "confirmed": True},
            self.buyerA,
            "buyer",
        )

        self.quoteA.total_amount = Decimal("8000")
        self.quoteA.save(update_fields=["total_amount"])
        quote_item = self.quoteA.items.get()
        quote_item.part = None
        quote_item.save(update_fields=["part"])
        unmatched = execute(
            "accept_quote",
            {"quote_id": self.quoteA.id, "confirmed": True},
            self.buyerA,
            "buyer",
        )

        self.assertIn("некорректные", bad_total.text.lower())
        self.assertIn("некорректные", unmatched.text.lower())
        self.assertEqual(Order.objects.filter(buyer=self.buyerA).count(), before)

    def test_accept_quote_rejects_buyer_counter_direction(self):
        self.quoteA.direction = "buyer_to_seller"
        self.quoteA.save(update_fields=["direction"])

        result = execute(
            "accept_quote",
            {"quote_id": self.quoteA.id, "confirmed": True},
            self.buyerA,
            "buyer",
        )

        self.assertIn("только котировку поставщика", result.text.lower())
        self.assertFalse(Order.objects.filter(buyer=self.buyerA).exists())

    # ── Двойной клик confirm_kp_and_reserve ───────────────────────
    def test_confirm_kp_double_click_one_order(self):
        params = {"rfq_id": self.rfqA.id, "quote_id": self.quoteA.id}
        r1 = execute("confirm_kp_and_reserve", dict(params), self.buyerA, "buyer")
        r2 = execute("confirm_kp_and_reserve", dict(params), self.buyerA, "buyer")
        # ГЛАВНОЕ: второй клик НЕ создаёт второй заказ и не списывает резерв дважды.
        n_orders = Order.objects.filter(buyer=self.buyerA).count()
        self.assertEqual(n_orders, 1, f"Должен быть ровно 1 заказ, а не {n_orders}")
        # Второй вызов заблокирован: последовательный — внешним статус-чеком
        # («статус: принята»), параллельная гонка — select_for_update («уже создан»).
        t = r2.text.lower()
        self.assertTrue("уже создан" in t or "нельзя принять" in t or "принята" in t,
                        f"Второй клик должен быть заблокирован, а получили: {r2.text}")

    def test_confirm_kp_ignores_tampered_negative_logistics_cost(self):
        params = {
            "rfq_id": self.rfqA.id,
            "quote_id": self.quoteA.id,
            "logistics_cost": "-7999.99",
        }

        result = execute("confirm_kp_and_reserve", params, self.buyerA, "buyer")

        self.assertIn("сделка перешла", result.text.lower())
        order = Order.objects.get(buyer=self.buyerA)
        self.assertGreaterEqual(order.total_amount, self.quoteA.total_amount)
        self.assertGreaterEqual(order.logistics_cost, Decimal("0"))
        self.assertEqual(
            order.reserve_amount,
            (order.total_amount * Decimal("0.10")).quantize(Decimal("0.01")),
        )

    def test_kp_rejects_quote_total_that_does_not_match_items(self):
        self.quoteA.total_amount = Decimal("9000")
        self.quoteA.save(update_fields=["total_amount"])

        shown = execute(
            "present_kp_to_buyer",
            {"rfq_id": self.rfqA.id},
            self.buyerA,
            "buyer",
        )
        confirmed = execute(
            "confirm_kp_and_reserve",
            {"rfq_id": self.rfqA.id, "quote_id": self.quoteA.id},
            self.buyerA,
            "buyer",
        )

        self.assertIn("некорректные", shown.text.lower())
        self.assertIn("некорректные", confirmed.text.lower())
        self.assertFalse(Order.objects.filter(buyer=self.buyerA).exists())

    def test_cancelled_rfq_blocks_all_operator_kp_mutations(self):
        from assistant.kp_workflow import (
            op_approve_kp,
            op_compose_kp,
            op_dispatch_manual_rfq,
        )

        self.rfqA.mode = "semi"
        self.rfqA.status = "cancelled"
        self.rfqA.notes = "before"
        self.rfqA.save(update_fields=["mode", "status", "notes"])

        approved = op_approve_kp(
            {"rfq_id": self.rfqA.id, "confirmed": True},
            self.buyerA,
            "admin",
        )
        composed = op_compose_kp(
            {"rfq_id": self.rfqA.id, "quote_id": self.quoteA.id},
            self.buyerA,
            "admin",
        )
        self.rfqA.mode = "manual"
        self.rfqA.save(update_fields=["mode"])
        dispatched = op_dispatch_manual_rfq(
            {"rfq_id": self.rfqA.id, "confirmed": True},
            self.buyerA,
            "admin",
        )

        self.rfqA.refresh_from_db()
        self.assertIn("отмен", approved.text.lower())
        self.assertIn("отмен", composed.text.lower())
        self.assertIn("отмен", dispatched.text.lower())
        self.assertEqual(self.rfqA.notes, "before")

    def test_confirm_kp_rejects_cancelled_rfq(self):
        self.rfqA.status = "cancelled"
        self.rfqA.save(update_fields=["status"])

        result = execute(
            "confirm_kp_and_reserve",
            {"rfq_id": self.rfqA.id, "quote_id": self.quoteA.id},
            self.buyerA,
            "buyer",
        )

        self.assertIn("отмен", result.text.lower())
        self.assertFalse(Order.objects.filter(buyer=self.buyerA).exists())

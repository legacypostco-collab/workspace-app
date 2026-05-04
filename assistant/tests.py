"""Unit tests for the chat-first assistant data modules.

Запуск:
  python manage.py test assistant
"""
from decimal import Decimal

from django.test import TestCase

from .customs_data import (
    duty_rate_for, vat_rate_for, fees_for, required_certs_for,
    find_hs_codes, sanctions_check, DUTY_DEFAULT, VAT_DEFAULT,
)


class CustomsDataTests(TestCase):
    """Чистые юнит-тесты справочников customs_data — без БД."""

    # ── HS-codes / поиск ───────────────────────────────────────
    def test_find_hs_codes_filter_match(self):
        hits = find_hs_codes("масляный фильтр", limit=5)
        self.assertTrue(any(h["code"].startswith("8421") for h in hits),
                        f"expected 8421.* in hits, got {hits}")

    def test_find_hs_codes_pump_match(self):
        hits = find_hs_codes("hydraulic pump", limit=3)
        codes = [h["code"] for h in hits]
        self.assertIn("8413.50", codes)

    def test_find_hs_codes_empty(self):
        self.assertEqual(find_hs_codes("", limit=5), [])
        # Слишком короткие слова игнорируются (>=3)
        self.assertEqual(find_hs_codes("a b", limit=5), [])

    def test_find_hs_codes_no_match(self):
        self.assertEqual(find_hs_codes("xyzzy unicorn", limit=5), [])

    def test_find_hs_codes_limit(self):
        hits = find_hs_codes("part запчасть", limit=2)
        self.assertLessEqual(len(hits), 2)

    # ── Пошлины ────────────────────────────────────────────────
    def test_duty_rate_known_prefix(self):
        self.assertEqual(duty_rate_for("8413.50"), Decimal("5.0"))
        self.assertEqual(duty_rate_for("4011.20"), Decimal("10.0"))
        self.assertEqual(duty_rate_for("8431.49"), Decimal("0.0"))  # преференция

    def test_duty_rate_default_for_unknown(self):
        self.assertEqual(duty_rate_for("9999.99"), DUTY_DEFAULT)

    def test_duty_rate_no_dot(self):
        # 4-знач код без точки — берём первые 4 символа
        self.assertEqual(duty_rate_for("8413"), Decimal("5.0"))

    def test_duty_rate_empty(self):
        self.assertEqual(duty_rate_for(""), DUTY_DEFAULT)
        self.assertEqual(duty_rate_for(None), DUTY_DEFAULT)

    # ── НДС / сборы ────────────────────────────────────────────
    def test_vat_rate_known_country(self):
        self.assertEqual(vat_rate_for("RU"), Decimal("20.0"))
        self.assertEqual(vat_rate_for("KZ"), Decimal("12.0"))

    def test_vat_rate_lowercase(self):
        self.assertEqual(vat_rate_for("ru"), Decimal("20.0"))

    def test_vat_rate_unknown(self):
        self.assertEqual(vat_rate_for("XX"), VAT_DEFAULT)
        # Пустая строка → дефолт RU=20
        self.assertEqual(vat_rate_for(""), VAT_DEFAULT)

    def test_country_fees(self):
        ru = fees_for("RU")
        self.assertIn("broker", ru)
        self.assertIn("terminal", ru)
        self.assertGreater(ru["broker"], 0)

    def test_country_fees_unknown(self):
        # неизвестная страна → дефолт-словарь
        f = fees_for("XX")
        self.assertIn("broker", f)
        self.assertIn("terminal", f)

    # ── Сертификаты ────────────────────────────────────────────
    def test_required_certs_pumps(self):
        certs = required_certs_for("8413.50")
        self.assertIn("EAC", certs)
        self.assertTrue(any("ТР ТС" in c for c in certs))

    def test_required_certs_unknown_falls_back_to_eac(self):
        self.assertEqual(required_certs_for("9999.99"), ["EAC"])
        self.assertEqual(required_certs_for(""), ["EAC"])

    # ── Санкции ────────────────────────────────────────────────
    def test_sanctions_high_risk_country(self):
        res = sanctions_check(country="IR")
        self.assertEqual(res["level"], "high")
        self.assertTrue(any("OFAC" in r for r in res["reasons"]))

    def test_sanctions_clean(self):
        res = sanctions_check(country="RU")
        self.assertEqual(res["level"], "none")
        self.assertEqual(res["reasons"], [])

    def test_sanctions_takes_max_severity(self):
        # entity high + category medium → итог high
        res = sanctions_check(entity="rostec", category="dual_use_chip")
        self.assertEqual(res["level"], "high")
        self.assertEqual(len(res["reasons"]), 2)

    def test_sanctions_medium_only(self):
        res = sanctions_check(category="dual_use_chip")
        self.assertEqual(res["level"], "medium")

    def test_sanctions_empty_args(self):
        res = sanctions_check()
        self.assertEqual(res["level"], "none")


class PaymentsModuleSmokeTests(TestCase):
    """Лёгкие smoke-тесты — без сети, без реальных пользователей.

    Проверяет: create_payment_intent возвращает ожидаемые поля,
    escrow_summary не падает на пустой БД.
    """

    def test_create_intent_shape(self):
        from django.contrib.auth import get_user_model
        from . import payments
        User = get_user_model()
        u = User.objects.create_user(username="t_buyer", password="x")
        intent = payments.create_payment_intent(100, order_id=1, payer=u, kind="reserve")
        self.assertEqual(intent["amount"], 100.0)
        self.assertEqual(intent["status"], "requires_confirmation")
        self.assertTrue(intent["id"].startswith("pi_"))
        self.assertEqual(intent["kind"], "reserve")

    def test_escrow_summary_empty(self):
        from . import payments
        s = payments.escrow_summary()
        self.assertIn("outstanding_balance", s)
        self.assertIn("open_holds", s)
        self.assertIsInstance(s["open_holds"], dict)

    def test_dispatch_webhook_unknown_event(self):
        from .payments import dispatch_webhook
        r = dispatch_webhook({"type": "totally.unknown", "data": {}})
        self.assertTrue(r["received"])
        self.assertFalse(r["handled"])
        self.assertIn("unknown event", r["reason"])

    def test_dispatch_webhook_known_event(self):
        from .payments import dispatch_webhook
        r = dispatch_webhook({"type": "payment_intent.succeeded",
                              "data": {"id": "pi_x", "status": "succeeded"}})
        self.assertTrue(r["received"])
        self.assertTrue(r["handled"])


class EscrowTransferTests(TestCase):
    """Реальные эскроу-движения через WalletEngine (без сети)."""

    def setUp(self):
        from django.contrib.auth import get_user_model
        from marketplace.models import Order, OrderItem, Part, Category, Brand
        from .models import Wallet
        from decimal import Decimal as D
        U = get_user_model()
        self.buyer = U.objects.create_user(username="t_buyer", password="x")
        self.seller_a = U.objects.create_user(username="t_seller_a", password="x")
        self.seller_b = U.objects.create_user(username="t_seller_b", password="x")
        # Wallet'ы покупателя и продавцов
        wb = Wallet.for_user(self.buyer, demo_seed_amount=0)
        wb.balance = D("10000"); wb.save(update_fields=["balance"])
        Wallet.for_user(self.seller_a, demo_seed_amount=0)
        Wallet.for_user(self.seller_b, demo_seed_amount=0)
        # Order + items
        import uuid
        u = uuid.uuid4().hex[:6]
        cat = Category.objects.create(name=f"Cat-{u}", slug=f"cat-{u}")
        brand = Brand.objects.create(name=f"Brand-{u}", slug=f"brand-{u}")
        self.part_a = Part.objects.create(
            title=f"A-{u}", oem_number=f"A1-{u}", slug=f"a-{u}",
            category=cat, brand=brand,
            price=D("300"), seller=self.seller_a, is_active=True,
        )
        self.part_b = Part.objects.create(
            title=f"B-{u}", oem_number=f"B1-{u}", slug=f"b-{u}",
            category=cat, brand=brand,
            price=D("100"), seller=self.seller_b, is_active=True,
        )
        self.order = Order.objects.create(
            customer_name="t", customer_email="t@x.t", customer_phone="",
            delivery_address="-", buyer=self.buyer, total_amount=D("400"),
        )
        OrderItem.objects.create(order=self.order, part=self.part_a, quantity=1, unit_price=D("300"))
        OrderItem.objects.create(order=self.order, part=self.part_b, quantity=1, unit_price=D("100"))

    def _balances(self):
        from .models import Wallet
        from . import payments as p
        return {
            "buyer": Wallet.for_user(self.buyer).balance,
            "a": Wallet.for_user(self.seller_a).balance,
            "b": Wallet.for_user(self.seller_b).balance,
            "platform": p.get_platform_wallet().balance,
        }

    def test_full_escrow_cycle_multi_seller(self):
        """buyer → escrow → 2 sellers (split 75/25 по позициям 300/100)."""
        from . import payments as p
        from decimal import Decimal as D

        intent = p.create_payment_intent(D("400"), order_id=self.order.id, payer=self.buyer)
        intent = p.confirm_payment_intent(intent, self.buyer)
        self.assertEqual(intent["status"], "succeeded")

        b1 = self._balances()
        self.assertEqual(b1["buyer"], D("9600"))
        self.assertEqual(b1["platform"], D("400"))

        # Эскроу-баланс по конкретному заказу
        self.assertEqual(p.escrow_balance_for_order(self.order.id), D("400"))

        # Multi-seller split
        splits = p.split_by_seller(self.order)
        self.assertEqual(len(splits), 2)
        amt_a = next(s["amount"] for s in splits if s["seller"].id == self.seller_a.id)
        amt_b = next(s["amount"] for s in splits if s["seller"].id == self.seller_b.id)
        self.assertEqual(amt_a + amt_b, D("400"))

        # Release всем
        for s in splits:
            r = p.release_to_seller(order=self.order, seller=s["seller"], amount=s["amount"])
            self.assertTrue(r["ok"])

        b2 = self._balances()
        self.assertEqual(b2["a"] + b2["b"], D("400"))
        self.assertEqual(b2["platform"], D("0"))
        self.assertEqual(p.escrow_balance_for_order(self.order.id), D("0"))

    def test_refund_to_buyer(self):
        from . import payments as p
        from decimal import Decimal as D

        intent = p.confirm_payment_intent(
            p.create_payment_intent(D("400"), order_id=self.order.id, payer=self.buyer),
            self.buyer,
        )
        self.assertEqual(self._balances()["platform"], D("400"))

        r = p.refund_to_buyer(order=self.order, buyer=self.buyer, amount=D("400"))
        self.assertTrue(r["ok"])
        self.assertEqual(self._balances()["buyer"], D("10000"))
        self.assertEqual(self._balances()["platform"], D("0"))

    def test_partial_release_then_refund_remainder(self):
        from . import payments as p
        from decimal import Decimal as D

        p.confirm_payment_intent(
            p.create_payment_intent(D("400"), order_id=self.order.id, payer=self.buyer),
            self.buyer,
        )
        # Частичная выплата seller_a (его доля)
        p.release_to_seller(order=self.order, seller=self.seller_a, amount=D("300"))
        self.assertEqual(p.escrow_balance_for_order(self.order.id), D("100"))
        # Возврат остатка покупателю
        p.refund_to_buyer(order=self.order, buyer=self.buyer, amount=D("100"))
        self.assertEqual(p.escrow_balance_for_order(self.order.id), D("0"))
        b = self._balances()
        self.assertEqual(b["a"], D("300"))
        self.assertEqual(b["buyer"], D("9700"))
        self.assertEqual(b["platform"], D("0"))

    def test_split_by_seller_proportional(self):
        from . import payments as p
        from decimal import Decimal as D

        p.confirm_payment_intent(
            p.create_payment_intent(D("400"), order_id=self.order.id, payer=self.buyer),
            self.buyer,
        )
        splits = p.split_by_seller(self.order)
        # Σ amount == escrow
        self.assertEqual(sum((s["amount"] for s in splits), D("0")), D("400"))
        # Доли пропорциональны line_total
        amt_a = next(s["amount"] for s in splits if s["seller"].id == self.seller_a.id)
        amt_b = next(s["amount"] for s in splits if s["seller"].id == self.seller_b.id)
        self.assertEqual(amt_a, D("300.00"))
        self.assertEqual(amt_b, D("100.00"))

    def test_release_more_than_escrow_raises(self):
        from . import payments as p
        from .payments import InsufficientEscrow
        from decimal import Decimal as D

        # эскроу пуст
        with self.assertRaises(InsufficientEscrow):
            p._wallet_release_to_seller(order=self.order, seller=self.seller_a, amount=D("1"))

    def test_confirm_intent_insufficient_funds(self):
        from . import payments as p
        from .payments import InsufficientFunds
        from decimal import Decimal as D
        from .models import Wallet

        wb = Wallet.for_user(self.buyer)
        wb.balance = D("50"); wb.save(update_fields=["balance"])

        with self.assertRaises(InsufficientFunds):
            p.confirm_payment_intent(
                p.create_payment_intent(D("100"), order_id=self.order.id, payer=self.buyer),
                self.buyer,
            )


class WebhookSignatureTests(TestCase):
    """HMAC-SHA256 подпись Stripe-style webhook."""

    def _sign(self, body: bytes, secret: str, ts: int) -> str:
        import hmac, hashlib
        return hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()

    def test_demo_mode_passes_when_no_secret(self):
        import os
        from .payments_engines import verify_webhook_signature
        os.environ.pop("STRIPE_WEBHOOK_SECRET", None)
        self.assertTrue(verify_webhook_signature(b"{}", "anything"))

    def test_valid_signature(self):
        import os, time
        from .payments_engines import verify_webhook_signature
        secret = "whsec_test_unit"
        os.environ["STRIPE_WEBHOOK_SECRET"] = secret
        try:
            body = b'{"type":"x"}'
            ts = int(time.time())
            sig = self._sign(body, secret, ts)
            self.assertTrue(verify_webhook_signature(body, f"t={ts},v1={sig}"))
        finally:
            os.environ.pop("STRIPE_WEBHOOK_SECRET", None)

    def test_invalid_signature(self):
        import os
        from .payments_engines import verify_webhook_signature
        os.environ["STRIPE_WEBHOOK_SECRET"] = "whsec_test_unit"
        try:
            self.assertFalse(verify_webhook_signature(b"{}", "t=1,v1=baadf00d"))
            self.assertFalse(verify_webhook_signature(b"{}", ""))
        finally:
            os.environ.pop("STRIPE_WEBHOOK_SECRET", None)


class EngineSelectorTests(TestCase):
    """Engine selector: PAYMENT_ENGINE env routing."""

    def setUp(self):
        # Сбрасываем singleton
        import assistant.payments_engines as pe
        pe._ENGINE_INSTANCE = None

    def tearDown(self):
        import assistant.payments_engines as pe
        pe._ENGINE_INSTANCE = None

    def test_default_is_wallet(self):
        import os
        os.environ.pop("PAYMENT_ENGINE", None)
        from .payments_engines import get_engine, WalletEngine
        e = get_engine()
        self.assertIsInstance(e, WalletEngine)
        self.assertEqual(e.name, "wallet")

    def test_explicit_wallet(self):
        import os
        os.environ["PAYMENT_ENGINE"] = "wallet"
        try:
            from .payments_engines import get_engine, WalletEngine
            self.assertIsInstance(get_engine(), WalletEngine)
        finally:
            os.environ.pop("PAYMENT_ENGINE", None)

    def test_stripe_without_keys_falls_back_to_wallet(self):
        import os
        os.environ["PAYMENT_ENGINE"] = "stripe"
        os.environ.pop("STRIPE_SECRET_KEY", None)
        try:
            from .payments_engines import get_engine, WalletEngine
            # No STRIPE_SECRET_KEY → init raises → fallback to WalletEngine
            self.assertIsInstance(get_engine(), WalletEngine)
        finally:
            os.environ.pop("PAYMENT_ENGINE", None)


class OperatorActionsTests(TestCase):
    """Smoke-тесты operator actions: dashboard / queue / sla."""

    def setUp(self):
        from django.contrib.auth import get_user_model
        U = get_user_model()
        self.op = U.objects.create_user(username="t_op", password="x")

    def test_dashboard_for_operator(self):
        from .operator_actions import op_dashboard
        r = op_dashboard({}, self.op, "operator")
        self.assertIn("Сводка оператора", r.text)
        kpi = next((c for c in r.cards if c["type"] == "kpi_grid"), None)
        self.assertIsNotNone(kpi)
        self.assertGreaterEqual(len(kpi["data"]["items"]), 5)

    def test_dashboard_blocks_non_operator(self):
        from .operator_actions import op_dashboard
        r = op_dashboard({}, self.op, "buyer")
        self.assertIn("только оператору", r.text)

    def test_queue_filter_default_all(self):
        from .operator_actions import op_queue
        r = op_queue({"filter": "all"}, self.op, "operator")
        self.assertIn("«all»", r.text)

    def test_sla_breach_no_data(self):
        from .operator_actions import op_sla_breach
        r = op_sla_breach({}, self.op, "operator")
        # пусто но не падает
        self.assertIsNotNone(r.cards)

    def test_op_assign_returns_form_on_step1(self):
        from marketplace.models import Order
        from .operator_actions import op_assign
        from decimal import Decimal as D
        order = Order.objects.create(
            customer_name="t", customer_email="t@x.t", customer_phone="",
            delivery_address="-", buyer=self.op, total_amount=D("100"),
        )
        r = op_assign({"order_id": order.id}, self.op, "operator")
        self.assertTrue(any(c["type"] == "form" for c in r.cards))

    def test_op_assign_writes_event_on_step2(self):
        from marketplace.models import Order, OrderEvent
        from .operator_actions import op_assign
        from decimal import Decimal as D
        order = Order.objects.create(
            customer_name="t", customer_email="t@x.t", customer_phone="",
            delivery_address="-", buyer=self.op, total_amount=D("100"),
        )
        r = op_assign({
            "order_id": order.id, "to_role": "logist",
            "comment": "x", "confirmed": True,
        }, self.op, "operator")
        self.assertIn("✓", r.text)
        ev = OrderEvent.objects.filter(order=order, event_type="operator_action").first()
        self.assertIsNotNone(ev)
        self.assertEqual(ev.meta.get("kind"), "assigned")
        self.assertEqual(ev.meta.get("to_role"), "logist")


class CustomsActionsTests(TestCase):
    """Customs flow: hs_assign → calc_duty → certs_check → release."""

    def setUp(self):
        from django.contrib.auth import get_user_model
        from marketplace.models import Order
        from decimal import Decimal as D
        U = get_user_model()
        self.op = U.objects.create_user(username="t_op_cs", password="x")
        self.buyer = U.objects.create_user(username="t_buyer_cs", password="x")
        self.order = Order.objects.create(
            customer_name="t", customer_email="t@x.t", customer_phone="",
            delivery_address="-", buyer=self.buyer, status="customs",
            total_amount=D("1000"),
        )

    def test_hs_lookup_finds_filter(self):
        from .operator_actions import op_hs_lookup
        r = op_hs_lookup({"query": "масляный фильтр"}, self.op, "operator_customs")
        self.assertIn("Найдено", r.text)

    def test_hs_assign_then_calc_duty(self):
        from .operator_actions import op_hs_assign, op_calc_duty
        r1 = op_hs_assign({
            "order_id": self.order.id, "hs_code": "8421.23",
            "country": "RU", "confirmed": True,
        }, self.op, "operator_customs")
        self.assertIn("✓", r1.text)
        r2 = op_calc_duty({"order_id": self.order.id}, self.op, "operator_customs")
        self.assertIn("ИТОГО", r2.text)
        # 1000 * 5% (8421) = 50 пошлина; 1050 * 20% (RU) = 210 НДС;
        # 250 broker + 180 terminal → ИТОГО 690
        self.assertIn("$690.00", r2.text)

    def test_release_blocks_without_certs(self):
        from .operator_actions import op_hs_assign, op_customs_release
        op_hs_assign({"order_id": self.order.id, "hs_code": "8413.50",
                      "country": "RU", "confirmed": True},
                     self.op, "operator_customs")
        r = op_customs_release({"order_id": self.order.id, "confirmed": True},
                                self.op, "operator_customs")
        self.assertIn("Нельзя выпустить", r.text)
        self.assertIn("EAC", r.text)

    def test_sanctions_high_blocks_country(self):
        from .operator_actions import op_sanctions_check
        r = op_sanctions_check({"country": "IR"}, self.op, "operator_customs")
        self.assertIn("Запрещено", r.text)

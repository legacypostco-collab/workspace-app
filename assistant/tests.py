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


class RFQModeClassifierTests(TestCase):
    """RFQ mode classifier — 6 правил из ТЗ §7.1, §7.2."""

    def setUp(self):
        from django.contrib.auth import get_user_model
        from marketplace.models import (
            Brand, Category, Part, UserProfile, CompanyVerification,
        )
        from decimal import Decimal as D
        import uuid
        u = uuid.uuid4().hex[:6]
        U = get_user_model()
        self.buyer = U.objects.create_user(username=f"t_cls_b_{u}", password="x")
        self.seller_trusted = U.objects.create_user(username=f"t_cls_st_{u}", password="x")
        self.seller_sandbox = U.objects.create_user(username=f"t_cls_ss_{u}", password="x")
        # Setup profiles
        UserProfile.objects.create(user=self.buyer, role="buyer")
        UserProfile.objects.create(user=self.seller_trusted, role="seller")
        UserProfile.objects.create(user=self.seller_sandbox, role="seller")
        # supplier_status — editable=False, обходим через .update()
        UserProfile.objects.filter(user=self.seller_trusted).update(supplier_status="trusted")
        UserProfile.objects.filter(user=self.seller_sandbox).update(supplier_status="sandbox")
        # Buyer KYB-verified by default
        CompanyVerification.objects.create(
            user=self.buyer, legal_name="Buyer Co", inn="1234567890", status="verified",
        )
        # Catalog parts
        cat = Category.objects.create(name=f"c-{u}", slug=f"c-{u}")
        brand = Brand.objects.create(name=f"b-{u}", slug=f"b-{u}")
        self.part_trusted = Part.objects.create(
            title=f"P1-{u}", oem_number=f"P1-{u}", slug=f"p1-{u}",
            category=cat, brand=brand, price=D("100"),
            seller=self.seller_trusted, is_active=True,
        )
        self.part_sandbox = Part.objects.create(
            title=f"P2-{u}", oem_number=f"P2-{u}", slug=f"p2-{u}",
            category=cat, brand=brand, price=D("200"),
            seller=self.seller_sandbox, is_active=True,
        )

    def _items(self, *parts_or_none):
        """Builds items_to_add tuples like create_rfq does (4-tuple with confidence)."""
        return [
            (p.oem_number if p else "unknown", 1, p, 100 if p else 0)
            for p in parts_or_none
        ]

    def test_auto_when_all_matched_trusted_and_verified(self):
        """ТЗ §4.1: AUTO требует ≥3 предложений; для теста ослабляем до 1."""
        from .actions import _classify_rfq_mode
        items = self._items(self.part_trusted, self.part_trusted)
        mode, reason = _classify_rfq_mode(items, self.buyer, {"min_offers_for_auto": 1})
        self.assertEqual(mode, "auto", f"reason={reason}")
        self.assertIn("auto", reason.lower())

    def test_semi_when_partial_matched(self):
        from .actions import _classify_rfq_mode
        items = self._items(self.part_trusted, None)  # 1/2 matched
        mode, reason = _classify_rfq_mode(items, self.buyer, {})
        self.assertEqual(mode, "semi")
        self.assertIn("partial", reason.lower())

    def test_manual_oem_when_zero_matched(self):
        from .actions import _classify_rfq_mode
        items = self._items(None, None)
        mode, reason = _classify_rfq_mode(items, self.buyer, {})
        self.assertEqual(mode, "manual_oem")
        self.assertIn("0/", reason)

    def test_manual_oem_when_articles_param_passed(self):
        from .actions import _classify_rfq_mode
        items = self._items(self.part_trusted)
        mode, reason = _classify_rfq_mode(items, self.buyer, {"articles": ["X-123", "Y-456"]})
        self.assertEqual(mode, "manual_oem")
        self.assertIn("oem", reason.lower())

    def test_semi_when_buyer_not_verified(self):
        from .actions import _classify_rfq_mode
        from marketplace.models import CompanyVerification
        kyb = CompanyVerification.objects.get(user=self.buyer)
        kyb.status = "rejected"; kyb.save()
        items = self._items(self.part_trusted)
        mode, reason = _classify_rfq_mode(items, self.buyer, {"min_offers_for_auto": 1})
        self.assertEqual(mode, "semi", f"reason={reason}")
        self.assertIn("kyb", reason.lower())

    def test_semi_when_urgency_critical(self):
        from .actions import _classify_rfq_mode
        items = self._items(self.part_trusted)
        mode, reason = _classify_rfq_mode(items, self.buyer,
                                          {"urgency": "critical", "min_offers_for_auto": 1})
        self.assertEqual(mode, "semi")
        self.assertIn("critical", reason.lower())

    def test_semi_when_seller_is_sandbox(self):
        """ТЗ §6.2: исполнитель НЕ trusted → SEMI, нужен оператор."""
        from .actions import _classify_rfq_mode
        items = self._items(self.part_sandbox)
        mode, reason = _classify_rfq_mode(items, self.buyer, {"min_offers_for_auto": 1})
        self.assertEqual(mode, "semi", f"reason={reason}")
        # Reason может быть либо «нет надёжных» (§5.1), либо «исполнитель не trusted»
        # — обе валидны; главное mode=semi
        assert "sandbox" in reason.lower() or "надёжных" in reason.lower(), reason

    def test_explicit_mode_param_overrides_classifier(self):
        from .actions import _classify_rfq_mode
        items = self._items(None)
        mode, reason = _classify_rfq_mode(items, self.buyer, {"mode": "auto"})
        self.assertEqual(mode, "auto")
        self.assertIn("явно", reason.lower())

    def test_semi_when_insufficient_offers(self):
        """ТЗ §5.2: <3 предложений → SEMI."""
        from .actions import _classify_rfq_mode
        # part_trusted имеет только одного продавца — недостаточно для AUTO
        items = self._items(self.part_trusted, self.part_trusted)
        mode, reason = _classify_rfq_mode(items, self.buyer, {})  # default min=3
        self.assertEqual(mode, "semi", f"reason={reason}")
        self.assertTrue("недостаточно" in reason.lower() or "<3" in reason
                        or "надёжных" in reason.lower(), reason)

    def test_semi_when_no_trusted_supplier(self):
        """ТЗ §5.1: нет надёжного → SEMI с пометкой."""
        from .actions import _classify_rfq_mode
        # part_sandbox — единственный продавец в sandbox, нет trusted
        items = self._items(self.part_sandbox)
        mode, reason = _classify_rfq_mode(items, self.buyer, {"min_offers_for_auto": 1})
        self.assertEqual(mode, "semi", f"reason={reason}")
        self.assertIn("надёжных", reason.lower())

    def test_semi_when_low_confidence(self):
        """ТЗ §5.3: confidence < threshold → SEMI."""
        from .actions import _classify_rfq_mode
        # Передаём 4-tuple с низкой confidence (50 < default threshold 70)
        items = [(self.part_trusted.oem_number, 1, self.part_trusted, 50)]
        mode, reason = _classify_rfq_mode(items, self.buyer, {
            "min_offers_for_auto": 1,
        })
        self.assertEqual(mode, "semi", f"reason={reason}")
        self.assertIn("confidence", reason.lower())

    def test_match_confidence_helper(self):
        """_match_confidence: 100 exact, 80 substring, 60 fuzzy, 0 None."""
        from .actions import _match_confidence
        # exact
        self.assertEqual(_match_confidence(self.part_trusted.oem_number, self.part_trusted), 100)
        # substring
        oem = self.part_trusted.oem_number
        self.assertEqual(_match_confidence(oem[:4], self.part_trusted), 80)
        # No matched_part
        self.assertEqual(_match_confidence("XYZ-999", None), 0)


class SupplierRatingEngineTests(TestCase):
    """ТЗ §1, §8: события подбора → behavioral_score → status."""

    def setUp(self):
        from django.contrib.auth import get_user_model
        from marketplace.models import UserProfile
        U = get_user_model()
        self.seller = U.objects.create_user(username="t_rate_s", password="x")
        UserProfile.objects.create(user=self.seller, role="seller")

    def _profile(self):
        from marketplace.models import UserProfile
        return UserProfile.objects.get(user=self.seller)

    def test_record_event_creates_row_and_recalcs(self):
        from .rating import record_rating_event
        from marketplace.models import SupplierRatingEvent
        ev = record_rating_event(self.seller, event_type="rfq_response")
        self.assertIsNotNone(ev)
        self.assertEqual(SupplierRatingEvent.objects.filter(supplier=self.seller).count(), 1)
        # rfq_response default impact = +1, baseline 60 → behavioral = 61
        self._profile().behavioral_score
        self.assertEqual(self._profile().behavioral_score, Decimal("61.00"))

    def test_negative_event_lowers_behavioral_score(self):
        from .rating import record_rating_event
        # claim_confirmed = -7, baseline 60 → 53
        record_rating_event(self.seller, event_type="claim_confirmed")
        self.assertEqual(self._profile().behavioral_score, Decimal("53.00"))

    def test_status_changes_when_score_crosses_threshold(self):
        """80+ → trusted, 60-79 → sandbox, 0-59 → risky."""
        from .rating import record_rating_event
        # Default external=60, behavioral=60 → rating=60 → sandbox
        self.assertEqual(self._profile().supplier_status, "sandbox")

        # 20 раз +1 = +20 baseline=60, behavioral=80 → rating=0.6*60+0.4*80=68 → sandbox
        for _ in range(20):
            record_rating_event(self.seller, event_type="rfq_response")
        # 60*0.6 + 80*0.4 = 36 + 32 = 68 → sandbox (need 80 для trusted)
        self.assertEqual(self._profile().supplier_status, "sandbox")

        # Нужно повысить external_score до 90 чтобы перешагнуть 80:
        p = self._profile()
        p.external_score = Decimal("90.00")
        p.save()
        # 90*0.6 + 80*0.4 = 54 + 32 = 86 → trusted
        self.assertEqual(self._profile().supplier_status, "trusted")

    def test_excluded_status_when_bankruptcy_flag(self):
        """ТЗ §3: bankruptcy/liquidation → 'rejected' (excluded)."""
        p = self._profile()
        p.bankruptcy_flag = True
        p.save()
        self.assertEqual(self._profile().supplier_status, "rejected")
        self.assertEqual(self._profile().rating_score, Decimal("0.00"))

    def test_rating_window_limits_old_events(self):
        """Старые события (>90 дней) не учитываются."""
        from datetime import timedelta
        from django.utils import timezone
        from marketplace.models import SupplierRatingEvent
        from .rating import recalc_behavioral_score

        # Старое событие: -50, должно НЕ попасть в окно
        old_ev = SupplierRatingEvent.objects.create(
            supplier=self.seller, event_type="claim_confirmed",
            impact_score=Decimal("-50"),
        )
        # Принудительно сместим created_at на 100 дней назад
        SupplierRatingEvent.objects.filter(id=old_ev.id).update(
            created_at=timezone.now() - timedelta(days=100)
        )
        recalc_behavioral_score(self.seller)
        # 60 (baseline) + 0 (events в окне 90д) = 60
        self.assertEqual(self._profile().behavioral_score, Decimal("60.00"))

    def test_score_clamped_to_0_100(self):
        """Сумма импактов выходящая за [0,100] клампится."""
        from .rating import record_rating_event
        # 50 раз claim_confirmed (-7) = -350, baseline 60 → должно быть 0
        for _ in range(50):
            record_rating_event(self.seller, event_type="claim_confirmed")
        self.assertEqual(self._profile().behavioral_score, Decimal("0.00"))
        # rating = 0.6*60 + 0.4*0 = 36 → risky
        self.assertEqual(self._profile().supplier_status, "risky")

    def test_quote_submission_records_rating_event(self):
        """submit_quote должен создавать SupplierRatingEvent с типом rfq_response."""
        from marketplace.models import (
            Brand, Category, Part, RFQ, RFQItem, CompanyVerification,
            UserProfile, SupplierRatingEvent,
        )
        from .negotiation import submit_quote
        from decimal import Decimal as D
        import uuid
        u = uuid.uuid4().hex[:6]

        # Verify seller (для KYB-gate)
        CompanyVerification.objects.create(
            user=self.seller, legal_name="Test", inn="1234567890", status="verified",
        )
        # Создаём buyer + RFQ
        from django.contrib.auth import get_user_model
        U = get_user_model()
        buyer = U.objects.create_user(username=f"t_buyer_{u}", password="x")
        cat = Category.objects.create(name=f"c-{u}", slug=f"c-{u}")
        brand = Brand.objects.create(name=f"b-{u}", slug=f"b-{u}")
        part = Part.objects.create(
            title=f"P-{u}", oem_number=f"P-{u}", slug=f"p-{u}",
            category=cat, brand=brand, price=D("100"),
            seller=self.seller, is_active=True,
        )
        rfq = RFQ.objects.create(created_by=buyer, customer_name="b", customer_email="b@x.t")
        item = RFQItem.objects.create(rfq=rfq, query="P", quantity=1, matched_part=part)

        # Сабмит котировки
        submit_quote({
            "rfq_id": rfq.id, f"price_{item.id}": "90", "confirmed": True,
        }, self.seller, "seller")

        # Должно появиться rfq_response событие
        events = SupplierRatingEvent.objects.filter(
            supplier=self.seller, event_type="rfq_response",
        )
        self.assertEqual(events.count(), 1)
        self.assertEqual(events.first().impact_score, Decimal("1.00"))


class ClaimWorkflowTests(TestCase):
    """ТЗ §5.4: claim flow с 6 статусами + переходы."""

    def setUp(self):
        from django.contrib.auth import get_user_model
        from marketplace.models import Order, UserProfile
        from decimal import Decimal as D
        U = get_user_model()
        self.buyer = U.objects.create_user(username="t_cl_b", password="x", email="b@x.t")
        self.op = U.objects.create_user(username="t_cl_op", password="x")
        UserProfile.objects.create(user=self.buyer, role="buyer")
        self.order = Order.objects.create(
            customer_name="b", customer_email="b@x.t", customer_phone="",
            delivery_address="-", buyer=self.buyer, total_amount=D("1000"),
            status="completed", payment_status="paid",
        )

    def test_open_claim_form_then_create(self):
        from .claims import open_claim
        from marketplace.models import OrderClaim
        # Step 1 (form)
        r = open_claim({"order_id": self.order.id}, self.buyer, "buyer")
        self.assertTrue(any(c["type"] == "form" for c in r.cards))
        # Step 2 (confirmed)
        r2 = open_claim({
            "order_id": self.order.id, "kind": "defect", "title": "Брак насос",
            "description": "Не работает", "confirmed": True,
        }, self.buyer, "buyer")
        self.assertIn("✓", r2.text)
        claim = OrderClaim.objects.get(order=self.order)
        self.assertEqual(claim.status, "open")
        self.assertEqual(claim.kind, "defect")

    def test_full_flow_open_review_approve_corrective_close(self):
        from .claims import (open_claim, start_claim_review, approve_claim,
                             apply_corrective, close_claim)
        from marketplace.models import OrderClaim
        # 1. open
        open_claim({
            "order_id": self.order.id, "kind": "defect", "title": "x",
            "description": "y", "confirmed": True,
        }, self.buyer, "buyer")
        claim = OrderClaim.objects.get(order=self.order)
        # 2. in_review
        start_claim_review({"claim_id": claim.id}, self.op, "operator")
        claim.refresh_from_db()
        self.assertEqual(claim.status, "in_review")
        # 3. approve
        approve_claim({"claim_id": claim.id, "confirmed": True}, self.op, "operator")
        claim.refresh_from_db()
        self.assertEqual(claim.status, "approved")
        # 4. corrective_actions (resolution=repair)
        apply_corrective({
            "claim_id": claim.id, "resolution_kind": "repair", "confirmed": True,
        }, self.op, "operator")
        claim.refresh_from_db()
        self.assertEqual(claim.status, "corrective_actions")
        self.assertEqual(claim.resolution_kind, "repair")
        # 5. close
        close_claim({"claim_id": claim.id}, self.op, "operator")
        claim.refresh_from_db()
        self.assertEqual(claim.status, "closed")
        self.assertIsNotNone(claim.closed_at)

    def test_reject_path(self):
        from .claims import open_claim, reject_claim
        from marketplace.models import OrderClaim
        open_claim({
            "order_id": self.order.id, "kind": "other", "title": "x",
            "description": "y", "confirmed": True,
        }, self.buyer, "buyer")
        claim = OrderClaim.objects.get(order=self.order)
        # reject_claim → автоматически closes
        reject_claim({
            "claim_id": claim.id, "reason": "не подтверждается",
            "confirmed": True,
        }, self.op, "operator")
        claim.refresh_from_db()
        # rejected → closed (auto)
        self.assertEqual(claim.status, "closed")
        self.assertEqual(claim.rejection_reason, "не подтверждается")

    def test_settlement_path_with_full_refund(self):
        from .claims import (open_claim, start_claim_review, approve_claim,
                             apply_settlement)
        from marketplace.models import OrderClaim
        open_claim({
            "order_id": self.order.id, "kind": "defect", "title": "x",
            "description": "y", "confirmed": True,
        }, self.buyer, "buyer")
        claim = OrderClaim.objects.get(order=self.order)
        start_claim_review({"claim_id": claim.id}, self.op, "operator")
        approve_claim({"claim_id": claim.id, "confirmed": True}, self.op, "operator")
        apply_settlement({
            "claim_id": claim.id, "resolution_kind": "full_refund",
            "confirmed": True,
        }, self.op, "operator")
        claim.refresh_from_db()
        self.assertEqual(claim.status, "financial_settlement")
        self.assertEqual(claim.refund_amount, Decimal("1000.00"))

    def test_approve_records_rating_event(self):
        from .claims import open_claim, approve_claim
        from marketplace.models import OrderClaim, OrderItem, Part, Brand, Category, SupplierRatingEvent
        from django.contrib.auth import get_user_model
        from decimal import Decimal as D
        import uuid
        u = uuid.uuid4().hex[:6]
        U = get_user_model()
        seller = U.objects.create_user(username=f"t_sel_{u}", password="x")
        cat = Category.objects.create(name=f"c-{u}", slug=f"c-{u}")
        brand = Brand.objects.create(name=f"b-{u}", slug=f"b-{u}")
        part = Part.objects.create(
            title=f"p-{u}", oem_number=f"P-{u}", slug=f"p-{u}",
            category=cat, brand=brand, price=D("100"), seller=seller, is_active=True,
        )
        OrderItem.objects.create(order=self.order, part=part, quantity=1, unit_price=D("100"))
        # open + approve
        open_claim({"order_id": self.order.id, "kind": "defect", "title": "x",
                    "description": "y", "confirmed": True}, self.buyer, "buyer")
        claim = OrderClaim.objects.get(order=self.order)
        approve_claim({"claim_id": claim.id, "confirmed": True}, self.op, "operator")
        # Должен быть claim_confirmed event для seller'а
        events = SupplierRatingEvent.objects.filter(supplier=seller, event_type="claim_confirmed")
        self.assertEqual(events.count(), 1)


class BuyerVolumeDiscountTests(TestCase):
    """ТЗ §4.1: auto-discount по годовому обороту."""

    def setUp(self):
        from django.contrib.auth import get_user_model
        U = get_user_model()
        self.buyer = U.objects.create_user(username="t_vol_b", password="x")

    def _create_paid_order(self, amount):
        from marketplace.models import Order
        from decimal import Decimal as D
        return Order.objects.create(
            customer_name="x", customer_email="x@x.x", customer_phone="",
            delivery_address="-", buyer=self.buyer,
            total_amount=D(str(amount)), payment_status="paid",
            status="completed",
        )

    def test_level_0_when_no_orders(self):
        from .discounts import recalc_buyer_volume
        bvy = recalc_buyer_volume(self.buyer)
        self.assertEqual(bvy.level, 0)
        self.assertEqual(bvy.discount_pct, Decimal("0.00"))
        self.assertEqual(bvy.volume_usd, Decimal("0.00"))

    def test_level_1_at_1_1m(self):
        from .discounts import recalc_buyer_volume
        self._create_paid_order(1_200_000)
        bvy = recalc_buyer_volume(self.buyer)
        self.assertEqual(bvy.level, 1)
        self.assertEqual(bvy.discount_pct, Decimal("1.00"))

    def test_level_2_at_5_5m(self):
        from .discounts import recalc_buyer_volume
        self._create_paid_order(6_000_000)
        bvy = recalc_buyer_volume(self.buyer)
        self.assertEqual(bvy.level, 2)
        self.assertEqual(bvy.discount_pct, Decimal("1.50"))

    def test_level_3_at_11m(self):
        from .discounts import recalc_buyer_volume
        self._create_paid_order(12_000_000)
        bvy = recalc_buyer_volume(self.buyer)
        self.assertEqual(bvy.level, 3)
        self.assertEqual(bvy.discount_pct, Decimal("3.00"))

    def test_apply_volume_discount_calculates_total(self):
        from .discounts import apply_volume_discount, recalc_buyer_volume
        # Создадим заказы на 6M → level 2 → 1.5%
        self._create_paid_order(6_000_000)
        recalc_buyer_volume(self.buyer)
        result = apply_volume_discount(Decimal("100000"), self.buyer)
        self.assertEqual(result["level"], 2)
        self.assertEqual(result["discount_pct"], Decimal("1.50"))
        self.assertEqual(result["discount_amount"], Decimal("1500.00"))
        self.assertEqual(result["total"], Decimal("98500.00"))

    def test_get_buyer_discount_action_returns_kpi(self):
        from .actions import get_buyer_discount
        self._create_paid_order(2_000_000)
        r = get_buyer_discount({}, self.buyer, "buyer")
        self.assertIn("Уровень 1", r.text)
        kpi = r.cards[0]["data"]["items"]
        self.assertEqual(next(i for i in kpi if i["label"] == "Уровень")["value"], "Уровень 1")


class ConversationCategorizationTests(TestCase):
    """Не плодить новые conv'ы на каждый клик пилюли — reuse по категории."""

    def setUp(self):
        from django.contrib.auth import get_user_model
        U = get_user_model()
        self.user = U.objects.create_user(username="t_cat", password="x")

    def test_category_for_action_admin(self):
        from .conv_category import category_for_action
        self.assertEqual(category_for_action("start_onboarding"), "admin")
        self.assertEqual(category_for_action("seller_team"), "admin")
        self.assertEqual(category_for_action("sync_1c"), "admin")
        self.assertEqual(category_for_action("create_api_token"), "admin")

    def test_category_for_action_purchase(self):
        from .conv_category import category_for_action
        self.assertEqual(category_for_action("create_rfq"), "purchase")
        self.assertEqual(category_for_action("quick_order"), "purchase")
        self.assertEqual(category_for_action("track_order"), "purchase")
        self.assertEqual(category_for_action("pay_reserve"), "purchase")

    def test_category_for_action_support(self):
        from .conv_category import category_for_action
        self.assertEqual(category_for_action("create_claim"), "support")
        self.assertEqual(category_for_action("op_resolve_dispute"), "support")

    def test_category_for_action_general_fallback(self):
        from .conv_category import category_for_action
        self.assertEqual(category_for_action("unknown_action"), "general")
        self.assertEqual(category_for_action(""), "general")

    def test_title_includes_category_prefix(self):
        from .conv_category import title_for_action
        self.assertEqual(
            title_for_action("submit_company_info", "📋 Реквизиты компании"),
            "Управление · 📋 Реквизиты компании",
        )
        self.assertEqual(
            title_for_action("track_order", "ORD-151"),
            "Покупки · ORD-151",
        )

    def test_find_or_create_reuses_existing_conv_by_category(self):
        from .conv_category import find_or_create_conv
        from .models import Conversation
        # 1-й вызов create
        conv1 = find_or_create_conv(
            self.user, action_name="start_onboarding", role="seller",
            action_label="Шаг 1",
        )
        self.assertEqual(conv1.category, "admin")
        self.assertEqual(Conversation.objects.filter(user=self.user, category="admin").count(), 1)
        # 2-й вызов другого admin-action — REUSE того же conv'а
        conv2 = find_or_create_conv(
            self.user, action_name="seller_team", role="seller",
            action_label="Команда",
        )
        self.assertEqual(conv1.id, conv2.id)
        # Title обновился
        conv2.refresh_from_db()
        self.assertIn("Команда", conv2.title)

    def test_purchase_creates_separate_conv_from_admin(self):
        from .conv_category import find_or_create_conv
        admin_conv = find_or_create_conv(
            self.user, action_name="start_onboarding", role="seller", action_label="KYB",
        )
        purchase_conv = find_or_create_conv(
            self.user, action_name="quick_order", role="buyer", action_label="Заказ",
        )
        self.assertNotEqual(admin_conv.id, purchase_conv.id)
        self.assertEqual(admin_conv.category, "admin")
        self.assertEqual(purchase_conv.category, "purchase")

    def test_action_view_reuses_admin_conv_across_clicks(self):
        """E2E: 2 разных admin pill'a → 1 conv в БД."""
        from rest_framework.test import APIClient
        from .models import Conversation
        client = APIClient()
        client.force_authenticate(self.user)
        # Клик «Верификация»
        client.post("/api/assistant/action/", {
            "action": "start_onboarding", "params": {"_label": "🛡 Верификация"},
        }, format="json")
        # Клик «Команда»
        client.post("/api/assistant/action/", {
            "action": "seller_team", "params": {"_label": "👥 Команда"},
        }, format="json")
        admin_convs = Conversation.objects.filter(user=self.user, category="admin")
        self.assertEqual(admin_convs.count(), 1, "Должен быть ровно один admin conv")


class ExternalRatingTests(TestCase):
    """ТЗ §1: внешняя оценка из Kontur/СПАРК."""

    def setUp(self):
        from django.contrib.auth import get_user_model
        from marketplace.models import UserProfile, CompanyVerification
        U = get_user_model()
        self.seller = U.objects.create_user(username="t_ext_s", password="x")
        UserProfile.objects.create(user=self.seller, role="seller")
        CompanyVerification.objects.create(
            user=self.seller, legal_name="Test Co", inn="7707083893", status="verified",
        )

    def test_demo_fetch_returns_score_in_range(self):
        from .external_rating import fetch_external_rating
        data = fetch_external_rating("7707083893")
        self.assertIn("score", data)
        self.assertIn("source", data)
        self.assertEqual(data["source"], "demo")
        self.assertGreaterEqual(data["score"], 40)
        self.assertLessEqual(data["score"], 100)
        self.assertFalse(data["bankruptcy"])
        self.assertFalse(data["liquidation"])

    def test_demo_inn_starting_00_flags_bankruptcy(self):
        from .external_rating import fetch_external_rating
        data = fetch_external_rating("0012345678")
        self.assertTrue(data["bankruptcy"])
        self.assertEqual(data["score"], 0.0)

    def test_demo_inn_starting_99_flags_liquidation(self):
        from .external_rating import fetch_external_rating
        data = fetch_external_rating("9912345678")
        self.assertTrue(data["liquidation"])

    def test_refresh_external_rating_applies_to_profile(self):
        from .external_rating import refresh_external_rating
        from marketplace.models import UserProfile
        data = refresh_external_rating(self.seller)
        self.assertIsNotNone(data)
        self.assertGreaterEqual(data["score"], 40)
        # Profile обновился
        p = UserProfile.objects.get(user=self.seller)
        self.assertEqual(float(p.external_score), float(data["score"]))

    def test_refresh_with_no_inn_returns_skip(self):
        from django.contrib.auth import get_user_model
        from marketplace.models import UserProfile
        from .external_rating import refresh_external_rating
        u = get_user_model().objects.create_user(username="t_no_inn", password="x")
        UserProfile.objects.create(user=u, role="seller")
        # Нет CompanyVerification → нет INN
        data = refresh_external_rating(u)
        self.assertEqual(data["source"], "skip")

    def test_bankruptcy_inn_flips_status_to_rejected(self):
        from marketplace.models import UserProfile, CompanyVerification
        from .external_rating import refresh_external_rating
        # Заменяем INN на «банкротный»
        kyb = CompanyVerification.objects.get(user=self.seller)
        kyb.inn = "0012345678"; kyb.save()
        refresh_external_rating(self.seller)
        p = UserProfile.objects.get(user=self.seller)
        self.assertTrue(p.bankruptcy_flag)
        self.assertEqual(p.supplier_status, "rejected")


class PriorityRoutingTests(TestCase):
    """ТЗ §7.1: рассылка RFQ — trusted приоритет, sandbox fallback, risky вручную."""

    def setUp(self):
        from django.contrib.auth import get_user_model
        from marketplace.models import RFQ, RFQItem, UserProfile
        import uuid
        u = uuid.uuid4().hex[:6]
        U = get_user_model()
        self.buyer = U.objects.create_user(username=f"t_pr_b_{u}", password="x")
        UserProfile.objects.create(user=self.buyer, role="buyer")
        # 2 trusted, 2 sandbox, 2 risky
        self.trusted_a = U.objects.create_user(username=f"t_pr_ta_{u}", password="x")
        self.trusted_b = U.objects.create_user(username=f"t_pr_tb_{u}", password="x")
        self.sandbox_a = U.objects.create_user(username=f"t_pr_sa_{u}", password="x")
        self.sandbox_b = U.objects.create_user(username=f"t_pr_sb_{u}", password="x")
        self.risky_a = U.objects.create_user(username=f"t_pr_ra_{u}", password="x")
        self.risky_b = U.objects.create_user(username=f"t_pr_rb_{u}", password="x")
        for s, st in [
            (self.trusted_a, "trusted"), (self.trusted_b, "trusted"),
            (self.sandbox_a, "sandbox"), (self.sandbox_b, "sandbox"),
            (self.risky_a, "risky"), (self.risky_b, "risky"),
        ]:
            UserProfile.objects.create(user=s, role="seller")
            UserProfile.objects.filter(user=s).update(supplier_status=st)
        self.rfq = RFQ.objects.create(
            created_by=self.buyer, customer_name="b", customer_email="b@x.t",
        )
        RFQItem.objects.create(rfq=self.rfq, query="X", quantity=1)

    def test_default_routing_includes_trusted_and_sandbox_skips_risky(self):
        from .negotiation import send_rfq_to_suppliers
        from marketplace.models import Notification
        send_rfq_to_suppliers({"rfq_id": self.rfq.id, "confirmed": True}, self.buyer, "buyer")
        recipients = set(Notification.objects.filter(kind="rfq").values_list("user_id", flat=True))
        # Trusted всегда
        self.assertIn(self.trusted_a.id, recipients)
        self.assertIn(self.trusted_b.id, recipients)
        # Sandbox обычно тоже (для альтернативных предложений по ТЗ §3.1)
        # Risky — нет
        self.assertNotIn(self.risky_a.id, recipients)
        self.assertNotIn(self.risky_b.id, recipients)

    def test_include_risky_flag_dispatches_to_risky(self):
        from .negotiation import send_rfq_to_suppliers
        from marketplace.models import Notification
        send_rfq_to_suppliers(
            {"rfq_id": self.rfq.id, "confirmed": True, "include_risky": True},
            self.buyer, "buyer",
        )
        recipients = set(Notification.objects.filter(kind="rfq").values_list("user_id", flat=True))
        self.assertIn(self.risky_a.id, recipients)
        self.assertIn(self.risky_b.id, recipients)


class NoResponseDetectionTests(TestCase):
    """ТЗ §8: detect_no_response cron — продавец без ответа в норматив → −5."""

    def setUp(self):
        from django.contrib.auth import get_user_model
        from marketplace.models import RFQ, RFQItem, UserProfile
        U = get_user_model()
        self.buyer = U.objects.create_user(username="t_nr_b", password="x", email="b@x.t")
        self.seller = U.objects.create_user(username="t_nr_s", password="x")
        UserProfile.objects.create(user=self.buyer, role="buyer")
        UserProfile.objects.create(user=self.seller, role="seller")
        self.rfq = RFQ.objects.create(
            created_by=self.buyer, customer_name="b", customer_email="b@x.t",
        )
        RFQItem.objects.create(rfq=self.rfq, query="X", quantity=1)

    def test_old_unanswered_notification_creates_no_response_event(self):
        from datetime import timedelta
        from django.core.management import call_command
        from django.utils import timezone
        from marketplace.models import Notification, SupplierRatingEvent

        # Notification 25 часов назад
        n = Notification.objects.create(
            user=self.seller, kind="rfq",
            title="RFQ", body="X", url=f"/chat/rfq/{self.rfq.id}/?source=invite",
        )
        Notification.objects.filter(id=n.id).update(
            created_at=timezone.now() - timedelta(hours=25),
        )

        call_command("detect_no_response", "--threshold-hours=24")

        events = SupplierRatingEvent.objects.filter(supplier=self.seller, event_type="no_response")
        self.assertEqual(events.count(), 1)
        self.assertEqual(events.first().meta["notification_id"], n.id)
        self.assertEqual(events.first().meta["rfq_id"], self.rfq.id)

    def test_responded_seller_skipped(self):
        from datetime import timedelta
        from django.core.management import call_command
        from django.utils import timezone
        from marketplace.models import Notification, Quote, SupplierRatingEvent

        n = Notification.objects.create(
            user=self.seller, kind="rfq",
            title="RFQ", body="", url=f"/chat/rfq/{self.rfq.id}/?source=invite",
        )
        Notification.objects.filter(id=n.id).update(
            created_at=timezone.now() - timedelta(hours=25),
        )
        # Seller ответил Quote'ом
        Quote.objects.create(
            rfq=self.rfq, seller=self.seller, direction="seller_to_buyer",
            round_number=1, status="submitted", total_amount=100,
        )
        call_command("detect_no_response", "--threshold-hours=24")
        # no_response event НЕ создан
        events = SupplierRatingEvent.objects.filter(supplier=self.seller, event_type="no_response")
        self.assertEqual(events.count(), 0)

    def test_idempotent(self):
        """Повторный запуск не дублирует events."""
        from datetime import timedelta
        from django.core.management import call_command
        from django.utils import timezone
        from marketplace.models import Notification, SupplierRatingEvent

        n = Notification.objects.create(
            user=self.seller, kind="rfq",
            title="RFQ", body="", url=f"/chat/rfq/{self.rfq.id}/?source=invite",
        )
        Notification.objects.filter(id=n.id).update(
            created_at=timezone.now() - timedelta(hours=25),
        )
        call_command("detect_no_response", "--threshold-hours=24")
        call_command("detect_no_response", "--threshold-hours=24")  # Re-run
        events = SupplierRatingEvent.objects.filter(supplier=self.seller, event_type="no_response")
        self.assertEqual(events.count(), 1)  # Не дублируется


class OnboardingKybTests(TestCase):
    """KYB wizard: 5 шагов + operator review/approve/reject + gating."""

    def setUp(self):
        from django.contrib.auth import get_user_model
        U = get_user_model()
        self.seller = U.objects.create_user(username="t_kyb_seller", password="x")
        self.operator = U.objects.create_user(username="t_kyb_op", password="x")

    # --- wizard ---
    def test_start_onboarding_new_user_returns_step1_action(self):
        from .onboarding import start_onboarding
        r = start_onboarding({}, self.seller, "seller")
        self.assertIn("Onboarding", r.cards[0]["data"]["title"])
        # actions должны указывать на submit_company_info
        action_names = [a["action"] for a in r.actions]
        self.assertIn("submit_company_info", action_names)

    def test_step1_validation_inn(self):
        from .onboarding import submit_company_info
        # bad INN
        r = submit_company_info({
            "legal_name": "ООО Тест", "inn": "123",
            "confirmed": True,
        }, self.seller, "seller")
        self.assertIn("ИНН", r.text)
        self.assertIn("Проверьте", r.text)

    def test_step1_step2_step3_step4(self):
        from .onboarding import (submit_company_info, submit_legal_address,
                                  submit_bank, submit_director)
        r1 = submit_company_info({
            "legal_name": "ООО Тест", "inn": "1234567890",
            "kpp": "123456789", "ogrn": "1234567890123",
            "confirmed": True,
        }, self.seller, "seller")
        self.assertIn("✓", r1.text)

        r2 = submit_legal_address({
            "legal_address": "г. Москва, ул. Тестовая 1",
            "confirmed": True,
        }, self.seller, "seller")
        self.assertIn("✓", r2.text)

        r3 = submit_bank({
            "bank_name": "ПАО Тестбанк", "bik": "044525225",
            "bank_account": "40702810400000000001",
            "confirmed": True,
        }, self.seller, "seller")
        self.assertIn("✓", r3.text)

        r4 = submit_director({
            "director_name": "Иванов Иван Иванович",
            "confirmed": True,
        }, self.seller, "seller")
        self.assertIn("✓", r4.text)

    def test_step5_submit_for_review_flips_status_pending(self):
        from .onboarding import (submit_company_info, submit_legal_address,
                                  submit_bank, submit_director, submit_for_review)
        from marketplace.models import CompanyVerification
        for fn, p in [
            (submit_company_info, {"legal_name":"ООО","inn":"1234567890","confirmed":True}),
            (submit_legal_address, {"legal_address":"Москва","confirmed":True}),
            (submit_bank, {"bank_name":"Б","bik":"044525225","bank_account":"40702810400000000001","confirmed":True}),
            (submit_director, {"director_name":"И.","confirmed":True}),
        ]:
            fn(p, self.seller, "seller")
        # step1: preview
        r1 = submit_for_review({}, self.seller, "seller")
        self.assertTrue(any(c["type"] == "draft" for c in r1.cards))
        # step2: confirm
        r2 = submit_for_review({"confirmed": True}, self.seller, "seller")
        self.assertIn("отправлена", r2.text.lower())
        kyb = CompanyVerification.objects.get(user=self.seller)
        self.assertEqual(kyb.status, "pending")
        self.assertIsNotNone(kyb.submitted_at)

    def test_submit_for_review_blocks_incomplete(self):
        from .onboarding import submit_for_review
        # ничего не заполнено
        r = submit_for_review({"confirmed": True}, self.seller, "seller")
        self.assertIn("не готова", r.text.lower())

    # --- operator review ---
    def test_op_kyb_queue_lists_pending(self):
        from .onboarding import op_kyb_queue
        from marketplace.models import CompanyVerification
        from django.utils import timezone
        CompanyVerification.objects.create(
            user=self.seller, legal_name="ООО Pending",
            inn="1234567890", status="pending", submitted_at=timezone.now(),
        )
        r = op_kyb_queue({}, self.operator, "operator")
        self.assertIn("KYB", r.text)
        items = r.cards[0]["data"]["items"]
        self.assertTrue(any("Pending" in it["title"] for it in items))

    def test_op_kyb_approve_flips_status_verified(self):
        from .onboarding import op_kyb_approve
        from marketplace.models import CompanyVerification, Notification
        from django.utils import timezone
        kyb = CompanyVerification.objects.create(
            user=self.seller, legal_name="ООО Test",
            inn="1234567890", status="pending", submitted_at=timezone.now(),
        )
        # step1: preview
        r1 = op_kyb_approve({"user_id": self.seller.id}, self.operator, "operator")
        self.assertTrue(any(c["type"] == "draft" for c in r1.cards))
        # step2: confirm
        r2 = op_kyb_approve({"user_id": self.seller.id, "confirmed": True},
                            self.operator, "operator")
        self.assertIn("одобрен", r2.text.lower())
        kyb.refresh_from_db()
        self.assertEqual(kyb.status, "verified")
        self.assertEqual(kyb.reviewed_by, self.operator)
        # Нотификация ушла seller'у
        self.assertTrue(Notification.objects.filter(user=self.seller, kind="system").exists())

    def test_op_kyb_reject_writes_reason(self):
        from .onboarding import op_kyb_reject
        from marketplace.models import CompanyVerification
        from django.utils import timezone
        CompanyVerification.objects.create(
            user=self.seller, legal_name="X", inn="1234567890",
            status="pending", submitted_at=timezone.now(),
        )
        r = op_kyb_reject({
            "user_id": self.seller.id, "reason": "Поддельный ИНН",
            "confirmed": True,
        }, self.operator, "operator")
        self.assertIn("отклонён", r.text.lower())
        kyb = CompanyVerification.objects.get(user=self.seller)
        self.assertEqual(kyb.status, "rejected")
        self.assertEqual(kyb.rejection_reason, "Поддельный ИНН")

    def test_op_kyb_actions_blocked_for_buyer(self):
        from .onboarding import op_kyb_queue
        r = op_kyb_queue({}, self.seller, "buyer")
        self.assertIn("оператор", r.text.lower())

    # --- gating ---
    def test_kyb_required_for_unverified_seller(self):
        from .onboarding import kyb_required_for_seller
        # пустой KYB → требуется
        self.assertTrue(kyb_required_for_seller(self.seller))
        # demo-аккаунт всегда пропускаем
        from django.contrib.auth import get_user_model
        demo = get_user_model().objects.create_user(username="demo_x", password="x")
        self.assertFalse(kyb_required_for_seller(demo))

    def test_gate_blocks_respond_rfq_for_unverified(self):
        from .actions import execute, kyb_gate
        # gate сам по себе
        reason = kyb_gate("respond_rfq", "seller", self.seller)
        self.assertIsNotNone(reason)
        self.assertIn("KYB", reason)
        # execute() возвращает ошибку с ссылкой на onboarding
        res = execute("respond_rfq", {}, self.seller, "seller")
        self.assertIn("🛡", res.text)
        action_names = [a["action"] for a in res.actions]
        self.assertIn("start_onboarding", action_names)

    def test_gate_passes_for_verified_seller(self):
        from .actions import kyb_gate
        from marketplace.models import CompanyVerification
        CompanyVerification.objects.create(
            user=self.seller, legal_name="X", inn="1234567890", status="verified",
        )
        self.assertIsNone(kyb_gate("respond_rfq", "seller", self.seller))

    def test_gate_does_not_apply_to_buyer_actions(self):
        from .actions import kyb_gate
        # quick_order не в списке gated → не блокируется
        self.assertIsNone(kyb_gate("quick_order", "buyer", self.seller))

    def test_gate_only_for_seller_role(self):
        from .actions import kyb_gate
        # seller-action в роли operator не блокируется (operator не нуждается в KYB)
        self.assertIsNone(kyb_gate("ship_order", "operator", self.seller))


class NegotiationFlowTests(TestCase):
    """RFQ → Quote → counter → accept end-to-end."""

    def setUp(self):
        from django.contrib.auth import get_user_model
        from marketplace.models import (
            Brand, Category, Part, RFQ, RFQItem, CompanyVerification,
        )
        from decimal import Decimal as D
        import uuid
        u = uuid.uuid4().hex[:6]
        U = get_user_model()
        self.buyer = U.objects.create_user(username=f"t_neg_b_{u}", password="x")
        self.seller_a = U.objects.create_user(username=f"t_neg_sa_{u}", password="x")
        self.seller_b = U.objects.create_user(username=f"t_neg_sb_{u}", password="x")
        # Verify оба продавца чтобы KYB-gate их не блокировал
        for s in (self.seller_a, self.seller_b):
            CompanyVerification.objects.create(
                user=s, legal_name=f"Co {s.id}", inn="1234567890", status="verified",
            )
        cat = Category.objects.create(name=f"c-{u}", slug=f"c-{u}")
        brand = Brand.objects.create(name=f"b-{u}", slug=f"b-{u}")
        self.part1 = Part.objects.create(
            title=f"Pump-{u}", oem_number=f"P-{u}", slug=f"pump-{u}",
            category=cat, brand=brand, price=D("1000"),
            seller=self.seller_a, is_active=True,
        )
        self.part2 = Part.objects.create(
            title=f"Filter-{u}", oem_number=f"F-{u}", slug=f"filter-{u}",
            category=cat, brand=brand, price=D("100"),
            seller=self.seller_a, is_active=True,
        )
        self.rfq = RFQ.objects.create(
            created_by=self.buyer, customer_name="Buyer Co",
            customer_email="b@x.t",
        )
        self.rfq_item1 = RFQItem.objects.create(
            rfq=self.rfq, query="Pump", quantity=2, matched_part=self.part1,
        )
        self.rfq_item2 = RFQItem.objects.create(
            rfq=self.rfq, query="Filter", quantity=10, matched_part=self.part2,
        )

    def test_submit_quote_creates_quote_with_items(self):
        from .negotiation import submit_quote
        from marketplace.models import Quote, QuoteItem
        r = submit_quote({
            "rfq_id": self.rfq.id,
            f"price_{self.rfq_item1.id}": "950",
            f"price_{self.rfq_item2.id}": "90",
            "delivery_days": 10, "valid_days": 7,
            "confirmed": True,
        }, self.seller_a, "seller")
        self.assertIn("✓", r.text)
        q = Quote.objects.filter(rfq=self.rfq, seller=self.seller_a).first()
        self.assertIsNotNone(q)
        # 2 × 950 + 10 × 90 = 1900 + 900 = 2800
        self.assertEqual(q.total_amount, Decimal("2800.00"))
        self.assertEqual(q.round_number, 1)
        self.assertEqual(q.delivery_days, 10)
        self.assertEqual(q.items.count(), 2)

    def test_submit_quote_form_step_returns_form(self):
        from .negotiation import submit_quote
        r = submit_quote({"rfq_id": self.rfq.id}, self.seller_a, "seller")
        self.assertTrue(any(c["type"] == "form" for c in r.cards))

    def test_submit_quote_blocks_unverified_seller(self):
        from .negotiation import submit_quote
        from marketplace.models import CompanyVerification
        # сбросить verified на rejected
        kyb = CompanyVerification.objects.get(user=self.seller_a)
        kyb.status = "rejected"; kyb.save()
        r = submit_quote({"rfq_id": self.rfq.id, "confirmed": True,
                          f"price_{self.rfq_item1.id}": "100"},
                         self.seller_a, "seller")
        self.assertIn("верифицированным", r.text)
        # vs verified
        kyb.status = "verified"; kyb.save()
        r2 = submit_quote({"rfq_id": self.rfq.id, "confirmed": True,
                           f"price_{self.rfq_item1.id}": "100"},
                          self.seller_a, "seller")
        self.assertIn("✓", r2.text)

    def test_view_rfq_quotes_orders_by_total_asc(self):
        from .negotiation import submit_quote, view_rfq_quotes
        # seller_a: 1100*2 + 100*10 = 3200
        submit_quote({
            "rfq_id": self.rfq.id, f"price_{self.rfq_item1.id}": "1100",
            f"price_{self.rfq_item2.id}": "100", "confirmed": True,
        }, self.seller_a, "seller")
        # seller_b: 900*2 + 80*10 = 2600 (cheaper)
        submit_quote({
            "rfq_id": self.rfq.id, f"price_{self.rfq_item1.id}": "900",
            f"price_{self.rfq_item2.id}": "80", "confirmed": True,
        }, self.seller_b, "seller")
        r = view_rfq_quotes({"rfq_id": self.rfq.id}, self.buyer, "buyer")
        items = r.cards[0]["data"]["items"]
        self.assertEqual(len(items), 2)
        # Самый дешёвый первый — но имя скрыто для buyer'а («Поставщик №1»)
        self.assertIn("Поставщик №1", items[0]["title"])
        self.assertIn("$2,600", items[0]["title"])
        # Реального username seller_b не должно быть в выдаче buyer'у
        self.assertNotIn(self.seller_b.username, items[0]["title"])
        self.assertNotIn(self.seller_a.username, items[1]["title"])

    def test_view_rfq_quotes_blocks_non_owner(self):
        from .negotiation import view_rfq_quotes
        r = view_rfq_quotes({"rfq_id": self.rfq.id}, self.seller_a, "seller")
        self.assertIn("только заказчик", r.text)

    def test_accept_quote_creates_order(self):
        from .negotiation import submit_quote, accept_quote
        from marketplace.models import Quote, Order
        from decimal import Decimal as D
        submit_quote({
            "rfq_id": self.rfq.id, f"price_{self.rfq_item1.id}": "1000",
            f"price_{self.rfq_item2.id}": "100", "confirmed": True,
        }, self.seller_a, "seller")
        q = Quote.objects.filter(rfq=self.rfq).first()
        # step 1
        r1 = accept_quote({"quote_id": q.id}, self.buyer, "buyer")
        self.assertTrue(any(c["type"] == "draft" for c in r1.cards))
        # step 2
        r2 = accept_quote({"quote_id": q.id, "confirmed": True}, self.buyer, "buyer")
        self.assertIn("создан заказ", r2.text)
        order = Order.objects.filter(buyer=self.buyer).order_by("-id").first()
        self.assertIsNotNone(order)
        # 2 × 1000 + 10 × 100 = 3000
        self.assertEqual(order.total_amount, D("3000.00"))
        self.assertEqual(order.items.count(), 2)
        q.refresh_from_db()
        self.assertEqual(q.status, "accepted")

    def test_accept_quote_auto_declines_others(self):
        from .negotiation import submit_quote, accept_quote
        from marketplace.models import Quote
        submit_quote({
            "rfq_id": self.rfq.id, f"price_{self.rfq_item1.id}": "1000",
            f"price_{self.rfq_item2.id}": "100", "confirmed": True,
        }, self.seller_a, "seller")
        submit_quote({
            "rfq_id": self.rfq.id, f"price_{self.rfq_item1.id}": "950",
            f"price_{self.rfq_item2.id}": "95", "confirmed": True,
        }, self.seller_b, "seller")
        winner = Quote.objects.filter(rfq=self.rfq, seller=self.seller_b).first()
        accept_quote({"quote_id": winner.id, "confirmed": True}, self.buyer, "buyer")
        loser = Quote.objects.filter(rfq=self.rfq, seller=self.seller_a).first()
        loser.refresh_from_db()
        self.assertEqual(loser.status, "declined")

    def test_counter_offer_creates_round_2_buyer_to_seller(self):
        from .negotiation import submit_quote, counter_offer
        from marketplace.models import Quote
        submit_quote({
            "rfq_id": self.rfq.id, f"price_{self.rfq_item1.id}": "1000",
            f"price_{self.rfq_item2.id}": "100", "confirmed": True,
        }, self.seller_a, "seller")
        q = Quote.objects.filter(rfq=self.rfq).first()
        qi1 = q.items.filter(rfq_item=self.rfq_item1).first()
        qi2 = q.items.filter(rfq_item=self.rfq_item2).first()
        r = counter_offer({
            "quote_id": q.id, "confirmed": True,
            f"price_{qi1.id}": "850", f"price_{qi2.id}": "85",
            "message": "Можем дешевле?",
        }, self.buyer, "buyer")
        self.assertIn("Контр-оффер", r.text)
        # Original → countered
        q.refresh_from_db()
        self.assertEqual(q.status, "countered")
        # New round_2 quote с direction=buyer_to_seller
        new = Quote.objects.filter(rfq=self.rfq, round_number=2).first()
        self.assertIsNotNone(new)
        self.assertEqual(new.direction, "buyer_to_seller")
        self.assertEqual(new.parent_quote_id, q.id)
        # 2 × 850 + 10 × 85 = 2550
        self.assertEqual(new.total_amount, Decimal("2550.00"))

    def test_counter_offer_blocked_for_finalized(self):
        from .negotiation import submit_quote, counter_offer, mark_quote_final
        from marketplace.models import Quote
        submit_quote({
            "rfq_id": self.rfq.id, f"price_{self.rfq_item1.id}": "1000",
            f"price_{self.rfq_item2.id}": "100", "confirmed": True,
        }, self.seller_a, "seller")
        q = Quote.objects.filter(rfq=self.rfq).first()
        mark_quote_final({"quote_id": q.id}, self.seller_a, "seller")
        r = counter_offer({"quote_id": q.id}, self.buyer, "buyer")
        self.assertIn("финальная", r.text)

    def test_decline_quote_marks_status(self):
        from .negotiation import submit_quote, decline_quote
        from marketplace.models import Quote
        submit_quote({
            "rfq_id": self.rfq.id, f"price_{self.rfq_item1.id}": "1000",
            f"price_{self.rfq_item2.id}": "100", "confirmed": True,
        }, self.seller_a, "seller")
        q = Quote.objects.filter(rfq=self.rfq).first()
        r = decline_quote({"quote_id": q.id}, self.buyer, "buyer")
        self.assertIn("✓", r.text)
        q.refresh_from_db()
        self.assertEqual(q.status, "declined")

    def test_buyer_anonymity_in_top_suppliers(self):
        """top_suppliers скрывает имена для buyer и показывает для seller/operator."""
        from .actions import top_suppliers
        # Buyer
        r_buyer = top_suppliers({}, self.buyer, "buyer")
        suppliers = r_buyer.cards[0]["data"]["suppliers"]
        names = [s["name"] for s in suppliers]
        assert all(n.startswith("Поставщик №") for n in names), \
            f"buyer should see anonymized names, got {names}"
        # Реальные имена не утекают
        assert "Caterpillar" not in str(suppliers)
        assert "Уралмаш" not in str(suppliers)
        # Seller — видит реальные имена
        r_seller = top_suppliers({}, self.seller_a, "seller")
        seller_names = [s["name"] for s in r_seller.cards[0]["data"]["suppliers"]]
        assert any("Caterpillar" in n for n in seller_names), \
            f"seller should see real names, got {seller_names}"

    def test_buyer_anonymity_in_view_quote(self):
        """view_quote показывает «Поставщик №N» для buyer и username для seller."""
        from .negotiation import submit_quote, view_quote
        from marketplace.models import Quote
        submit_quote({
            "rfq_id": self.rfq.id, f"price_{self.rfq_item1.id}": "900",
            "confirmed": True,
        }, self.seller_a, "seller")
        q = Quote.objects.filter(rfq=self.rfq).first()
        # Buyer видит «Поставщик №…»
        r_buyer = view_quote({"quote_id": q.id}, self.buyer, "buyer")
        rows = r_buyer.cards[0]["data"]["rows"]
        seller_row = next(r for r in rows if r["label"] == "Продавец")
        assert seller_row["value"].startswith("Поставщик №"), \
            f"buyer should see anon, got {seller_row['value']!r}"
        # Seller (автор) видит свой username
        r_seller = view_quote({"quote_id": q.id}, self.seller_a, "seller")
        seller_row2 = next(r for r in r_seller.cards[0]["data"]["rows"] if r["label"] == "Продавец")
        assert seller_row2["value"] == self.seller_a.username

    def test_buyer_anonymity_revealed_after_accept(self):
        """После accept_quote buyer видит реального продавца — заказ оформлен."""
        from .negotiation import submit_quote, accept_quote, view_quote
        from marketplace.models import Quote
        submit_quote({
            "rfq_id": self.rfq.id, f"price_{self.rfq_item1.id}": "900",
            "confirmed": True,
        }, self.seller_a, "seller")
        q = Quote.objects.filter(rfq=self.rfq).first()
        accept_quote({"quote_id": q.id, "confirmed": True}, self.buyer, "buyer")
        # После accepted — имя раскрыто
        r = view_quote({"quote_id": q.id}, self.buyer, "buyer")
        rows = r.cards[0]["data"]["rows"]
        seller_row = next(r for r in rows if r["label"] == "Продавец")
        assert seller_row["value"] == self.seller_a.username, \
            f"after accept buyer should see real username, got {seller_row['value']!r}"

    def test_mark_quote_final_only_by_seller(self):
        from .negotiation import submit_quote, mark_quote_final
        from marketplace.models import Quote
        submit_quote({
            "rfq_id": self.rfq.id, f"price_{self.rfq_item1.id}": "1000",
            "confirmed": True,
        }, self.seller_a, "seller")
        q = Quote.objects.filter(rfq=self.rfq).first()
        r = mark_quote_final({"quote_id": q.id}, self.buyer, "buyer")
        self.assertIn("автор", r.text)
        # Author может
        r2 = mark_quote_final({"quote_id": q.id}, self.seller_a, "seller")
        self.assertIn("🔒", r2.text)
        q.refresh_from_db()
        self.assertTrue(q.is_final)
        self.assertEqual(q.status, "finalized")


class DurableChannelsTests(TestCase):
    """Email + Telegram fanout + per-user preferences."""

    def setUp(self):
        from django.contrib.auth import get_user_model
        from marketplace.models import UserProfile
        U = get_user_model()
        self.user = U.objects.create_user(
            username="t_chan", password="x", email="user@example.com",
        )
        self.profile = UserProfile.objects.create(user=self.user, role="buyer")

    def test_email_sent_when_enabled_and_kind_match(self):
        from django.core import mail
        from .channels import send_email
        self.profile.notif_email_enabled = True
        self.profile.notif_kinds = "order,payment"
        self.profile.save()
        ok = send_email(self.user, kind="order", title="T", body="B", url="/x")
        self.assertTrue(ok)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("T", mail.outbox[0].subject)
        self.assertIn("user@example.com", mail.outbox[0].to)

    def test_email_skipped_when_kind_not_in_prefs(self):
        from django.core import mail
        from .channels import send_email
        self.profile.notif_email_enabled = True
        self.profile.notif_kinds = "payment"  # only payment
        self.profile.save()
        ok = send_email(self.user, kind="rfq", title="T", body="B")
        self.assertFalse(ok)
        self.assertEqual(len(mail.outbox), 0)

    def test_email_skipped_when_disabled(self):
        from django.core import mail
        from .channels import send_email
        self.profile.notif_email_enabled = False
        self.profile.save()
        ok = send_email(self.user, kind="order", title="T")
        self.assertFalse(ok)
        self.assertEqual(len(mail.outbox), 0)

    def test_email_skipped_when_no_email(self):
        from .channels import send_email
        self.user.email = ""
        self.user.save()
        ok = send_email(self.user, kind="order", title="T")
        self.assertFalse(ok)

    def test_telegram_skipped_when_no_token(self):
        import os
        from .channels import send_telegram
        os.environ.pop("TELEGRAM_BOT_TOKEN", None)
        self.profile.notif_telegram_enabled = True
        self.profile.notif_telegram_chat_id = "12345"
        self.profile.notif_kinds = "order"
        self.profile.save()
        ok = send_telegram(self.user, kind="order", title="T")
        self.assertFalse(ok)

    def test_telegram_skipped_when_no_chat_id(self):
        import os
        from .channels import send_telegram
        os.environ["TELEGRAM_BOT_TOKEN"] = "fake"
        try:
            self.profile.notif_telegram_enabled = True
            self.profile.notif_telegram_chat_id = ""
            self.profile.save()
            ok = send_telegram(self.user, kind="order", title="T")
            self.assertFalse(ok)
        finally:
            os.environ.pop("TELEGRAM_BOT_TOKEN", None)

    def test_fanout_to_durable_returns_status_dict(self):
        from .channels import fanout_to_durable
        self.profile.notif_email_enabled = True
        self.profile.notif_kinds = "order"
        self.profile.save()
        r = fanout_to_durable(self.user, kind="order", title="T", body="B", url="/x")
        self.assertIn("email", r)
        self.assertIn("telegram", r)
        self.assertTrue(r["email"])
        self.assertFalse(r["telegram"])

    def test_notify_creates_db_row_and_sends_email(self):
        from django.core import mail
        from .actions import _notify
        from marketplace.models import Notification
        self.profile.notif_email_enabled = True
        self.profile.notif_kinds = "order"
        self.profile.save()
        _notify(self.user, kind="order", title="New order #42",
                body="Total $100", url="/chat/?order=42")
        # 1. DB row создан
        self.assertEqual(Notification.objects.filter(user=self.user).count(), 1)
        # 2. Email отправлен
        self.assertEqual(len(mail.outbox), 1)

    def test_send_digest_returns_false_when_no_unread(self):
        from .channels import send_digest
        ok = send_digest(self.user)
        self.assertFalse(ok)

    def test_send_digest_sends_when_unread_exists(self):
        from django.core import mail
        from .channels import send_digest
        from marketplace.models import Notification
        Notification.objects.create(user=self.user, kind="order", title="A", body="x")
        Notification.objects.create(user=self.user, kind="payment", title="B", body="y")
        self.profile.notif_email_enabled = True
        self.profile.save()
        ok = send_digest(self.user)
        self.assertTrue(ok)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("2 новых", mail.outbox[0].subject)


class NotifSettingsActionsTests(TestCase):
    """User-facing notification preferences actions."""

    def setUp(self):
        from django.contrib.auth import get_user_model
        from marketplace.models import UserProfile
        U = get_user_model()
        self.user = U.objects.create_user(username="t_pref", password="x")
        UserProfile.objects.create(user=self.user, role="buyer")

    def test_notif_prefs_returns_kpi_grid(self):
        from .notif_settings import notif_prefs
        r = notif_prefs({}, self.user, "buyer")
        self.assertEqual(r.cards[0]["type"], "kpi_grid")
        labels = [it["label"] for it in r.cards[0]["data"]["items"]]
        self.assertIn("Email-канал", labels)
        self.assertIn("Telegram", labels)

    def test_notif_set_email_toggle_off(self):
        from .notif_settings import notif_set_email
        r = notif_set_email({"enabled": "0", "confirmed": True}, self.user, "buyer")
        self.assertIn("выключен", r.text)
        self.user.profile.refresh_from_db()
        self.assertFalse(self.user.profile.notif_email_enabled)

    def test_notif_set_kinds_validates(self):
        from .notif_settings import notif_set_kinds
        # bad
        r = notif_set_kinds({"kinds": "garbage,nonsense", "confirmed": True},
                             self.user, "buyer")
        self.assertIn("⚠️", r.text)
        # good
        r2 = notif_set_kinds({"kinds": "order, sla, claim", "confirmed": True},
                              self.user, "buyer")
        self.assertIn("✓", r2.text)
        self.user.profile.refresh_from_db()
        self.assertEqual(self.user.profile.notif_kinds, "order,sla,claim")

    def test_notif_link_telegram_writes_chat_id(self):
        from .notif_settings import notif_link_telegram
        r = notif_link_telegram({"chat_id": "12345678", "confirmed": True},
                                 self.user, "buyer")
        self.assertIn("✓", r.text)
        self.user.profile.refresh_from_db()
        self.assertEqual(self.user.profile.notif_telegram_chat_id, "12345678")
        self.assertTrue(self.user.profile.notif_telegram_enabled)

    def test_notif_link_telegram_rejects_non_numeric(self):
        from .notif_settings import notif_link_telegram
        r = notif_link_telegram({"chat_id": "abc", "confirmed": True},
                                 self.user, "buyer")
        self.assertIn("числом", r.text)


class AdminActionsTests(TestCase):
    """Platform admin actions: dashboards, user management, moderation."""

    def setUp(self):
        from django.contrib.auth import get_user_model
        from marketplace.models import UserProfile
        U = get_user_model()
        self.admin = U.objects.create_user(username="t_admin", password="x",
                                            is_superuser=True, is_staff=True)
        self.buyer = U.objects.create_user(username="t_buy", password="x",
                                            email="b@x.t")
        UserProfile.objects.create(user=self.buyer, role="buyer")

    # --- gating ---
    def test_dashboard_blocks_non_admin(self):
        from .admin_actions import admin_dashboard
        r = admin_dashboard({}, self.buyer, "buyer")
        self.assertIn("администратор", r.text.lower())

    def test_dashboard_returns_kpi_grid(self):
        from .admin_actions import admin_dashboard
        r = admin_dashboard({}, self.admin, "admin")
        self.assertEqual(r.cards[0]["type"], "kpi_grid")
        labels = [it["label"] for it in r.cards[0]["data"]["items"]]
        self.assertIn("Активных юзеров", labels)
        self.assertIn("GMV 7d", labels)

    # --- read actions ---
    def test_admin_users_default_lists_all(self):
        from .admin_actions import admin_users
        r = admin_users({"filter": "all"}, self.admin, "admin")
        items = r.cards[0]["data"]["items"]
        # platform_escrow юзер исключён
        usernames = " ".join(it.get("title", "") for it in items)
        self.assertNotIn("__platform_escrow__", usernames)
        self.assertIn("t_buy", usernames)

    def test_admin_users_filter_banned(self):
        from .admin_actions import admin_users
        self.buyer.is_active = False
        self.buyer.save()
        r = admin_users({"filter": "banned"}, self.admin, "admin")
        usernames = " ".join(it.get("title", "") for it in r.cards[0]["data"]["items"])
        self.assertIn("t_buy", usernames)

    def test_admin_user_detail_shows_status(self):
        from .admin_actions import admin_user_detail
        r = admin_user_detail({"user_id": self.buyer.id}, self.admin, "admin")
        # Статус row с "Активен"
        rows = r.cards[0]["data"]["rows"]
        labels_values = {row["label"]: row["value"] for row in rows}
        self.assertIn("Username", labels_values)
        self.assertEqual(labels_values["Username"], "t_buy")

    # --- write actions ---
    def test_ban_user_two_step(self):
        from .admin_actions import admin_ban_user
        r1 = admin_ban_user({"user_id": self.buyer.id}, self.admin, "admin")
        self.assertTrue(any(c["type"] == "form" for c in r1.cards))
        r2 = admin_ban_user({"user_id": self.buyer.id, "reason": "Спам",
                              "confirmed": True}, self.admin, "admin")
        self.assertIn("🚫", r2.text)
        self.buyer.refresh_from_db()
        self.assertFalse(self.buyer.is_active)

    def test_ban_admin_blocked(self):
        from .admin_actions import admin_ban_user
        from django.contrib.auth import get_user_model
        U = get_user_model()
        other_admin = U.objects.create_user(username="t_admin2", password="x",
                                              is_superuser=True)
        r = admin_ban_user({"user_id": other_admin.id, "reason": "x",
                             "confirmed": True}, self.admin, "admin")
        self.assertIn("админа нельзя", r.text)

    def test_ban_self_blocked(self):
        from .admin_actions import admin_ban_user
        r = admin_ban_user({"user_id": self.admin.id, "reason": "x",
                             "confirmed": True}, self.admin, "admin")
        self.assertIn("самого себя", r.text)

    def test_unban_user(self):
        from .admin_actions import admin_unban_user
        self.buyer.is_active = False
        self.buyer.save()
        r = admin_unban_user({"user_id": self.buyer.id, "confirmed": True},
                              self.admin, "admin")
        self.assertIn("✓", r.text)
        self.buyer.refresh_from_db()
        self.assertTrue(self.buyer.is_active)

    def test_change_role_buyer_to_seller(self):
        from .admin_actions import admin_change_role
        r = admin_change_role({"user_id": self.buyer.id, "new_role": "seller",
                                "confirmed": True}, self.admin, "admin")
        self.assertIn("buyer → seller", r.text)
        self.buyer.profile.refresh_from_db()
        self.assertEqual(self.buyer.profile.role, "seller")

    def test_change_role_invalid(self):
        from .admin_actions import admin_change_role
        # Step 1: form
        r = admin_change_role({"user_id": self.buyer.id, "new_role": "bogus",
                                "confirmed": True}, self.admin, "admin")
        # invalid роль → возвращает форму, не пишет
        self.assertTrue(any(c["type"] == "form" for c in r.cards))
        self.buyer.profile.refresh_from_db()
        self.assertEqual(self.buyer.profile.role, "buyer")

    # --- moderation queue ---
    def test_moderation_queue_returns_list(self):
        from .admin_actions import admin_moderation_queue
        r = admin_moderation_queue({}, self.admin, "admin")
        self.assertEqual(r.cards[0]["type"], "list")

    def test_catalog_review_returns_three_lists(self):
        from .admin_actions import admin_catalog_review
        r = admin_catalog_review({}, self.admin, "admin")
        # 3 списка: $0 цена, без seller'а, последние
        self.assertEqual(len(r.cards), 3)

    def test_platform_settings_kpi(self):
        from .admin_actions import admin_platform_settings
        r = admin_platform_settings({}, self.admin, "admin")
        items = r.cards[0]["data"]["items"]
        labels = [it["label"] for it in items]
        self.assertIn("Payment engine", labels)
        self.assertIn("ANTHROPIC_API_KEY", labels)


class AuthFlowTests(TestCase):
    """Magic-link, TOTP 2FA, API tokens — full auth surface."""

    def setUp(self):
        from django.contrib.auth import get_user_model
        U = get_user_model()
        self.user = U.objects.create_user(
            username="t_auth", password="x", email="auth@example.com",
        )

    # --- magic-link ---
    def test_magic_link_request_creates_token_and_sends_email(self):
        from django.core import mail
        from django.test import Client
        from marketplace.models import MagicLinkToken
        c = Client()
        resp = c.post("/api/assistant/auth/magic-link/",
                      data='{"email":"auth@example.com"}',
                      content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        # Token создан
        ml = MagicLinkToken.objects.filter(user=self.user).first()
        self.assertIsNotNone(ml)
        self.assertTrue(ml.is_active)
        # Email отправлен
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("ссылка", mail.outbox[0].subject.lower())

    def test_magic_link_request_unknown_email_returns_200(self):
        from django.test import Client
        from marketplace.models import MagicLinkToken
        c = Client()
        resp = c.post("/api/assistant/auth/magic-link/",
                      data='{"email":"unknown@x.x"}',
                      content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        # Токен НЕ создан, чтобы не утекала инфа существует ли email
        self.assertEqual(MagicLinkToken.objects.count(), 0)

    def test_magic_link_confirm_logs_in_and_redirects(self):
        from datetime import timedelta
        from django.test import Client
        from django.utils import timezone
        from marketplace.models import MagicLinkToken
        ml = MagicLinkToken.objects.create(
            token="xyz123", user=self.user,
            expires_at=timezone.now() + timedelta(minutes=15),
        )
        c = Client()
        resp = c.get(f"/api/assistant/auth/magic-link/{ml.token}/")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.url, "/chat/")
        ml.refresh_from_db()
        self.assertIsNotNone(ml.used_at)
        # logged-in
        self.assertEqual(int(c.session["_auth_user_id"]), self.user.id)

    def test_magic_link_confirm_invalid_token_410(self):
        from django.test import Client
        c = Client()
        resp = c.get("/api/assistant/auth/magic-link/no-such-token/")
        self.assertEqual(resp.status_code, 410)

    def test_magic_link_confirm_expired_410(self):
        from datetime import timedelta
        from django.test import Client
        from django.utils import timezone
        from marketplace.models import MagicLinkToken
        ml = MagicLinkToken.objects.create(
            token="exp123", user=self.user,
            expires_at=timezone.now() - timedelta(minutes=1),
        )
        c = Client()
        resp = c.get(f"/api/assistant/auth/magic-link/{ml.token}/")
        self.assertEqual(resp.status_code, 410)

    # --- 2FA ---
    def test_setup_2fa_generates_secret_and_qr(self):
        from .auth_actions import setup_2fa
        from marketplace.models import TwoFactorAuth
        r = setup_2fa({}, self.user, "buyer")
        self.assertEqual(r.cards[0]["type"], "qr")
        self.assertIn("qr_url", r.cards[0]["data"])
        twofa = TwoFactorAuth.objects.get(user=self.user)
        self.assertNotEqual(twofa.secret, "")
        self.assertEqual(len(twofa.backup_codes.split(",")), 8)
        self.assertFalse(twofa.enabled)  # ещё не активирован — нужен verify

    def test_verify_2fa_with_valid_code_enables(self):
        import pyotp
        from .auth_actions import setup_2fa, verify_2fa
        from marketplace.models import TwoFactorAuth
        setup_2fa({}, self.user, "buyer")
        twofa = TwoFactorAuth.objects.get(user=self.user)
        valid_code = pyotp.TOTP(twofa.secret).now()
        r = verify_2fa({"code": valid_code, "confirmed": True}, self.user, "buyer")
        self.assertIn("активирован", r.text)
        twofa.refresh_from_db()
        self.assertTrue(twofa.enabled)

    def test_verify_2fa_with_bad_code_fails(self):
        from .auth_actions import setup_2fa, verify_2fa
        setup_2fa({}, self.user, "buyer")
        r = verify_2fa({"code": "000000", "confirmed": True}, self.user, "buyer")
        self.assertIn("неверн", r.text)

    def test_disable_2fa_requires_valid_code(self):
        import pyotp
        from .auth_actions import setup_2fa, verify_2fa, disable_2fa
        from marketplace.models import TwoFactorAuth
        setup_2fa({}, self.user, "buyer")
        twofa = TwoFactorAuth.objects.get(user=self.user)
        verify_2fa({"code": pyotp.TOTP(twofa.secret).now(), "confirmed": True},
                    self.user, "buyer")
        # Bad code
        r = disable_2fa({"code": "111111", "confirmed": True}, self.user, "buyer")
        self.assertIn("неверн", r.text)
        twofa.refresh_from_db()
        self.assertTrue(twofa.enabled)
        # Good code
        r2 = disable_2fa({"code": pyotp.TOTP(twofa.secret).now(), "confirmed": True},
                          self.user, "buyer")
        self.assertIn("✓", r2.text)
        twofa.refresh_from_db()
        self.assertFalse(twofa.enabled)

    # --- API tokens ---
    def test_create_api_token_returns_full_once(self):
        from .auth_actions import create_api_token
        from marketplace.models import ApiToken
        r = create_api_token({"label": "CI", "permissions": "read,write",
                                "confirmed": True}, self.user, "buyer")
        # Полный токен в card.draft.rows
        rows = r.cards[0]["data"]["rows"]
        full_row = next((row for row in rows if row["label"] == "Полный токен"), None)
        self.assertIsNotNone(full_row)
        self.assertTrue(full_row["value"].startswith("ck_live_"))
        # В БД хранится только хэш
        token_obj = ApiToken.objects.get(user=self.user, label="CI")
        self.assertEqual(token_obj.permissions, "read,write")
        self.assertTrue(token_obj.is_active)

    def test_list_api_tokens_shows_prefix_only(self):
        from .auth_actions import create_api_token, list_api_tokens
        create_api_token({"label": "Ext", "permissions": "read",
                            "confirmed": True}, self.user, "buyer")
        r = list_api_tokens({}, self.user, "buyer")
        items = r.cards[0]["data"]["items"]
        self.assertEqual(len(items), 1)
        self.assertIn("Ext", items[0]["title"])
        # Префикс показан, полный токен — нет
        self.assertIn("ck_live_", items[0]["title"])

    def test_revoke_api_token_marks_inactive(self):
        from .auth_actions import create_api_token, revoke_api_token
        from marketplace.models import ApiToken
        create_api_token({"label": "T", "permissions": "read",
                            "confirmed": True}, self.user, "buyer")
        token = ApiToken.objects.get(user=self.user, label="T")
        r = revoke_api_token({"token_id": token.id, "confirmed": True},
                              self.user, "buyer")
        self.assertIn("отозван", r.text)
        token.refresh_from_db()
        self.assertFalse(token.is_active)
        self.assertIsNotNone(token.revoked_at)

    # --- OAuth scaffolding ---
    def test_oauth_login_returns_503_when_not_configured(self):
        import os
        from django.test import Client
        os.environ.pop("GOOGLE_CLIENT_ID", None)
        c = Client()
        resp = c.get("/api/assistant/auth/oauth/google/")
        self.assertEqual(resp.status_code, 503)
        body = resp.json()
        self.assertIn("GOOGLE_CLIENT_ID", body["error"])

    def test_oauth_login_unknown_provider_400(self):
        from django.test import Client
        c = Client()
        resp = c.get("/api/assistant/auth/oauth/bogus/")
        self.assertEqual(resp.status_code, 400)

    def test_oauth_callback_state_mismatch_400(self):
        from django.test import Client
        c = Client()
        resp = c.get("/api/assistant/auth/oauth/callback/google/?code=abc&state=xxx")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("state mismatch", resp.json()["error"])

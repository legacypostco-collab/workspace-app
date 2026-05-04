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
        # Самый дешёвый первый
        self.assertIn(self.seller_b.username, items[0]["title"])
        self.assertIn("$2,600", items[0]["title"])

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

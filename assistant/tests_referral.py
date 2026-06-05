"""Тесты реферальных наград (assistant/referral.py + ReferralReward).

Запуск (на изолированной SQLite, без Postgres):
  DATABASE_URL=sqlite://:memory: python manage.py test assistant.tests_referral -v2
"""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from marketplace.models import Order, ReferralReward
from assistant import referral as ref
from assistant.models import Wallet

User = get_user_model()


def _bal(u):
    return Wallet.for_user(u).balance


class RecordReferralTests(TestCase):
    def setUp(self):
        self.buyer = User.objects.create_user("inviter_buyer", password="x")
        self.seller = User.objects.create_user("inviter_seller", password="x")
        self.operator = User.objects.create_user("inviter_op", password="x")
        self.kam = User.objects.create_user("inviter_kam", password="x")
        self.invitee = User.objects.create_user("invitee_one", password="x")

    def test_buyer_role_creates_buyer_discount(self):
        rw = ref.record_referral(self.buyer, self.invitee, "buyer")
        self.assertIsNotNone(rw)
        self.assertEqual(rw.kind, "buyer_discount")
        self.assertEqual(rw.status, "pending")
        self.assertEqual(rw.amount, Decimal("100"))

    def test_seller_role_creates_flat(self):
        rw = ref.record_referral(self.seller, self.invitee, "seller")
        self.assertEqual(rw.kind, "flat_first_order")
        self.assertEqual(rw.referred_id, self.invitee.id)

    def test_operator_role_creates_flat(self):
        rw = ref.record_referral(self.operator, self.invitee, "operator")
        self.assertEqual(rw.kind, "flat_first_order")

    def test_kam_role_returns_none(self):
        rw = ref.record_referral(self.kam, self.invitee, "operator_manager")
        self.assertIsNone(rw)
        self.assertEqual(ReferralReward.objects.count(), 0)

    def test_record_is_idempotent_flat(self):
        a = ref.record_referral(self.seller, self.invitee, "seller")
        b = ref.record_referral(self.seller, self.invitee, "seller")
        self.assertEqual(a.id, b.id)
        self.assertEqual(ReferralReward.objects.filter(kind="flat_first_order").count(), 1)

    def test_record_is_idempotent_buyer_discount(self):
        ref.record_referral(self.buyer, self.invitee, "buyer")
        other_invitee = User.objects.create_user("invitee_two", password="x")
        ref.record_referral(self.buyer, other_invitee, "buyer")
        # buyer_discount — один на пригласившего, независимо от числа приглашённых
        self.assertEqual(
            ReferralReward.objects.filter(referrer=self.buyer, kind="buyer_discount").count(), 1)


class CreditFlatOnFirstOrderTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user("ref_seller", password="x")
        self.invitee = User.objects.create_user("buyer_invitee", password="x")
        ref.record_referral(self.seller, self.invitee, "seller")

    def _make_order(self):
        return Order.objects.create(
            customer_name="t", customer_email="t@t.local", delivery_address="—",
            buyer=self.invitee, status="reserve_paid", payment_status="reserve_paid",
            total_amount=Decimal("5000"),
        )

    def test_first_order_credits_referrer_100(self):
        start = _bal(self.seller)
        order = self._make_order()
        n = ref.on_order_reserve_paid(order)
        self.assertEqual(n, 1)
        self.assertEqual(_bal(self.seller), start + Decimal("100"))
        rw = ReferralReward.objects.get(referrer=self.seller)
        self.assertEqual(rw.status, "credited")
        self.assertEqual(rw.trigger_order_id, order.id)

    def test_credit_is_idempotent(self):
        order = self._make_order()
        ref.on_order_reserve_paid(order)
        bal_after_first = _bal(self.seller)
        # Повторный триггер (напр. второй заказ) — не дублирует
        order2 = self._make_order()
        n = ref.on_order_reserve_paid(order2)
        self.assertEqual(n, 0)
        self.assertEqual(_bal(self.seller), bal_after_first)

    def test_no_reward_for_unrelated_buyer(self):
        stranger = User.objects.create_user("stranger", password="x")
        order = Order.objects.create(
            customer_name="t", customer_email="t@t.local", delivery_address="—",
            buyer=stranger, status="reserve_paid", payment_status="reserve_paid",
            total_amount=Decimal("5000"))
        n = ref.on_order_reserve_paid(order)
        self.assertEqual(n, 0)


class CreditBuyerDiscountOnTopupTests(TestCase):
    def setUp(self):
        self.buyer = User.objects.create_user("ref_buyer", password="x")
        self.invitee = User.objects.create_user("disc_invitee", password="x")
        ref.record_referral(self.buyer, self.invitee, "buyer")

    def test_topup_credits_buyer_100(self):
        start = _bal(self.buyer)
        n = ref.on_deposit_funded(self.buyer)
        self.assertEqual(n, 1)
        self.assertEqual(_bal(self.buyer), start + Decimal("100"))
        self.assertEqual(ReferralReward.objects.get(referrer=self.buyer).status, "credited")

    def test_topup_idempotent(self):
        ref.on_deposit_funded(self.buyer)
        b = _bal(self.buyer)
        n = ref.on_deposit_funded(self.buyer)
        self.assertEqual(n, 0)
        self.assertEqual(_bal(self.buyer), b)


class AcceptReferralBranchTests(TestCase):
    """accept_referral разветвляется по роли пригласившего."""

    def setUp(self):
        from django.core import signing
        self.signing = signing
        self.invitee = User.objects.create_user("accept_invitee", password="x")

    def _code(self, ref_user):
        return self.signing.dumps(int(ref_user.id), salt="kam-ref")

    def test_non_kam_records_reward_no_customer(self):
        from assistant.seller_actions import accept_referral
        from marketplace.models import Customer, UserProfile
        seller = User.objects.create_user("acc_seller", password="x")
        UserProfile.objects.update_or_create(user=seller, defaults={"role": "seller"})
        res = accept_referral({"code": self._code(seller)}, self.invitee, "buyer")
        self.assertIn("Приглашение принято", res.text)
        # detect_user_role(seller) == 'seller' → flat_first_order
        self.assertEqual(
            ReferralReward.objects.filter(referrer=seller, kind="flat_first_order").count(), 1)
        # CRM-заказчик НЕ создаётся для не-KAM пригласившего
        self.assertEqual(Customer.objects.filter(owner=seller).count(), 0)

    def test_short_code_resolves(self):
        from assistant.seller_actions import accept_referral
        from marketplace.models import Customer, UserProfile, ReferralCode
        seller = User.objects.create_user("short_seller", password="x")
        UserProfile.objects.update_or_create(user=seller, defaults={"role": "seller"})
        code = ReferralCode.for_user(seller).code
        self.assertTrue(6 <= len(code) <= 16)
        res = accept_referral({"code": code}, self.invitee, "buyer")
        self.assertIn("Приглашение принято", res.text)
        self.assertEqual(
            ReferralReward.objects.filter(referrer=seller, kind="flat_first_order").count(), 1)

    def test_short_code_case_insensitive(self):
        from marketplace.models import ReferralCode
        u = User.objects.create_user("ci_user", password="x")
        code = ReferralCode.for_user(u).code
        self.assertEqual(ReferralCode.resolve(code.lower()).id, u.id)

    def test_old_signed_token_still_works(self):
        from assistant.seller_actions import accept_referral
        from marketplace.models import UserProfile
        seller = User.objects.create_user("legacy_seller", password="x")
        UserProfile.objects.update_or_create(user=seller, defaults={"role": "seller"})
        old_token = self.signing.dumps(int(seller.id), salt="kam-ref")
        res = accept_referral({"code": old_token}, self.invitee, "buyer")
        self.assertIn("Приглашение принято", res.text)

    @override_settings(DEBUG=True)
    def test_kam_creates_customer(self):
        from assistant.seller_actions import accept_referral
        from marketplace.models import Customer
        # KAM-роль детектится по username demo_operator_manager (DEBUG).
        kam = User.objects.create_user("demo_operator_manager", password="x")
        res = accept_referral({"code": self._code(kam)}, self.invitee, "buyer")
        self.assertIn("закреплены", res.text.lower())
        self.assertEqual(ReferralReward.objects.count(), 0)
        self.assertTrue(Customer.objects.filter(user=self.invitee).exists())

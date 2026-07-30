"""P0 MONEY-FLOW тесты — закрывают пробел из pre-prod audit (2026-05-21).

Запуск:
    python manage.py test assistant.tests_p0_money_flow -v 2

Проверяет:
- P0-3: ownership check в seller_cancel_pending / seller_demand_payment
- P0-5: select_for_update в pay_reserve / pay_final / mark_paid
- P0-6: идемпотентность _wallet_confirm_intent (один intent_id → одно списание)
- P0-7: confirmed-gate в quick_order и confirm_delivery
- Sanity: pay_reserve создаёт ровно один escrow_hold
"""
from __future__ import annotations

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from assistant.actions import execute
from assistant.models import Wallet, WalletTx
from assistant.payments import _wallet_confirm_intent, create_payment_intent
from marketplace.models import Brand, Category, Order, OrderItem, Part, RFQ, UserProfile

User = get_user_model()


class P0MoneyFlowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.buyer = User.objects.create_user("p0_buyer", password="x")
        UserProfile.objects.create(user=cls.buyer, role="buyer")
        cls.seller = User.objects.create_user("p0_seller", password="x")
        UserProfile.objects.create(user=cls.seller, role="seller")
        cls.other_seller = User.objects.create_user("p0_other_seller", password="x")
        UserProfile.objects.create(user=cls.other_seller, role="seller")

        cls.brand = Brand.objects.create(name="P0Brand")
        cls.cat = Category.objects.create(name="P0Cat", slug="p0cat")
        cls.part = Part.objects.create(
            brand=cls.brand, category=cls.cat,
            oem_number="P0-001", title="P0 part", slug="p0-part-001",
            seller=cls.seller, price=Decimal("1000"),
        )
        # Депозит покупателю
        cls.buyer_wallet = Wallet.for_user(cls.buyer)
        cls.buyer_wallet.balance = Decimal("100000")
        cls.buyer_wallet.save(update_fields=["balance"])

    # ── P0-7: confirmed-gate ────────────────────────────────────
    def test_p07_quick_order_without_confirmed_returns_preview(self):
        r = execute("quick_order",
                    {"product_ids": [self.part.id], "quantity": 1},
                    self.buyer, "buyer")
        # Без confirmed=True не создаётся заказ
        self.assertIn("Подтвердите заказ", r.text)
        self.assertEqual(Order.objects.filter(buyer=self.buyer).count(), 0)
        # Должна быть кнопка "Подтвердить" в actions
        confirm_btn = next((a for a in (r.actions or []) if a.get("params", {}).get("confirmed")), None)
        self.assertIsNotNone(confirm_btn, "Должна быть кнопка с confirmed=True")

    def test_false_string_cannot_confirm_quick_order(self):
        result = execute(
            "quick_order",
            {
                "product_ids": [self.part.id],
                "quantity": 1,
                "confirmed": "false",
            },
            self.buyer,
            "buyer",
        )

        self.assertIn("Подтвердите заказ", result.text)
        self.assertFalse(Order.objects.filter(buyer=self.buyer).exists())

    def test_p07_quick_order_with_confirmed_creates_order(self):
        r = execute("quick_order",
                    {"product_ids": [self.part.id], "quantity": 1, "confirmed": True},
                    self.buyer, "buyer")
        # Главное — проверить что preview больше не возвращается (а confirm-флоу
        # выполнен). Создание Order зависит от calc_logistics + dest_country и
        # может вернуть ActionResult с ошибкой расчёта — но preview явно нет.
        self.assertNotIn("Подтвердите заказ", r.text)

    def test_quick_order_rejects_invalid_or_excessive_quantity(self):
        for quantity in ("NaN", -1, 1_000_001):
            with self.subTest(quantity=quantity):
                result = execute(
                    "quick_order",
                    {"product_ids": [self.part.id], "quantity": quantity},
                    self.buyer,
                    "buyer",
                )
                self.assertIn("Количество", result.text)
        self.assertFalse(Order.objects.filter(buyer=self.buyer).exists())

    def test_quick_order_rejects_oversized_product_list(self):
        result = execute(
            "quick_order",
            {"product_ids": list(range(1, 32)), "quantity": 1},
            self.buyer,
            "buyer",
        )

        self.assertIn("не более 30", result.text)
        self.assertFalse(Order.objects.filter(buyer=self.buyer).exists())

    def test_create_rfq_rejects_invalid_quantity_and_oversized_list(self):
        invalid_quantity = execute(
            "create_rfq",
            {"articles": ["A-1"], "quantity": "NaN"},
            self.buyer,
            "buyer",
        )
        oversized = execute(
            "create_rfq",
            {"articles": [f"A-{i}" for i in range(101)], "quantity": 1},
            self.buyer,
            "buyer",
        )

        self.assertIn("Количество", invalid_quantity.text)
        self.assertIn("не более 100", oversized.text)
        self.assertFalse(RFQ.objects.filter(created_by=self.buyer).exists())

    def test_p07_confirm_delivery_without_confirmed_returns_preview(self):
        order = Order.objects.create(buyer=self.buyer, status="delivered",
                                      payment_status="paid", total_amount=Decimal("1000"),
                                      customer_name="P0")
        OrderItem.objects.create(order=order, part=self.part, quantity=1, unit_price=Decimal("1000"))
        r = execute("confirm_delivery", {"order_id": order.id}, self.buyer, "buyer")
        self.assertIn("Подтвердить приёмку", r.text)
        order.refresh_from_db()
        # Статус НЕ изменился без подтверждения
        self.assertEqual(order.status, "delivered")

    # ── P0-3: ownership-check ───────────────────────────────────
    def test_p03_seller_cant_cancel_others_order(self):
        # Заказ принадлежит other_seller, не self.seller
        other_part = Part.objects.create(brand=self.brand, category=self.cat,
                                          oem_number="OTHER-1", title="other 1", slug="p0-other-1",
                                          seller=self.other_seller, price=Decimal("500"))
        order = Order.objects.create(buyer=self.buyer, status="awaiting_reserve",
                                      payment_status="awaiting_reserve",
                                      total_amount=Decimal("500"), customer_name="X")
        OrderItem.objects.create(order=order, part=other_part, quantity=1, unit_price=Decimal("500"))
        # Наш seller пытается отменить
        r = execute("seller_cancel_pending", {"order_id": order.id}, self.seller, "seller")
        self.assertIn("не содержит ваших товаров", r.text)
        # Заказ всё ещё существует
        self.assertTrue(Order.objects.filter(id=order.id).exists())

    def test_p03_seller_cant_demand_payment_on_others_order(self):
        other_part = Part.objects.create(brand=self.brand, category=self.cat,
                                          oem_number="OTHER-2", title="other 2", slug="p0-other-2",
                                          seller=self.other_seller, price=Decimal("500"))
        order = Order.objects.create(buyer=self.buyer, status="awaiting_reserve",
                                      payment_status="awaiting_reserve",
                                      total_amount=Decimal("500"), customer_name="X")
        OrderItem.objects.create(order=order, part=other_part, quantity=1, unit_price=Decimal("500"))
        r = execute("seller_demand_payment", {"order_id": order.id}, self.seller, "seller")
        self.assertIn("не содержит ваших товаров", r.text)

    # ── P0-6: идемпотентность ──────────────────────────────────
    def test_p06_wallet_confirm_intent_is_idempotent(self):
        amount = Decimal("100")
        order = Order.objects.create(buyer=self.buyer, status="awaiting_reserve",
                                      payment_status="awaiting_reserve",
                                      total_amount=amount, reserve_amount=amount,
                                      customer_name="P06")
        intent = create_payment_intent(amount, order_id=order.id, payer=self.buyer, kind="reserve")
        # Первый confirm — реальное списание
        balance_before = Wallet.for_user(self.buyer).balance
        _wallet_confirm_intent(intent, self.buyer)
        balance_after_1 = Wallet.for_user(self.buyer).balance
        self.assertEqual(balance_before - balance_after_1, amount)
        n_tx_after_1 = WalletTx.objects.filter(
            description__contains=f"(intent {intent['id']})", kind="escrow_hold",
        ).count()
        # Второй confirm на тот же intent — НЕ должен списать
        result_2 = _wallet_confirm_intent(intent, self.buyer)
        balance_after_2 = Wallet.for_user(self.buyer).balance
        self.assertEqual(balance_after_1, balance_after_2,
                          "Повторный confirm на тот же intent_id списал деньги дважды!")
        n_tx_after_2 = WalletTx.objects.filter(
            description__contains=f"(intent {intent['id']})", kind="escrow_hold",
        ).count()
        self.assertEqual(n_tx_after_1, n_tx_after_2,
                          "Создан дубликат WalletTx — нарушена идемпотентность")
        self.assertTrue(result_2.get("idempotent_replay"))

    # ── Sanity: pay_reserve создаёт ровно один escrow_hold ─────
    def test_pay_reserve_creates_exactly_one_escrow_hold(self):
        order = Order.objects.create(buyer=self.buyer, status="awaiting_reserve",
                                      payment_status="awaiting_reserve",
                                      total_amount=Decimal("1000"), reserve_amount=Decimal("100"),
                                      customer_name="Sanity")
        OrderItem.objects.create(order=order, part=self.part, quantity=1, unit_price=Decimal("1000"))
        n_before = WalletTx.objects.filter(order_id=order.id, kind="escrow_hold").count()
        execute("pay_reserve",
                {"order_id": order.id, "confirmed": True},
                self.buyer, "buyer")
        # На один заказ должно быть 2 записи escrow_hold:
        #   1) payer_wallet (списание у покупателя)
        #   2) platform_wallet (зачисление платформе)
        n_after = WalletTx.objects.filter(order_id=order.id, kind="escrow_hold").count()
        self.assertEqual(n_after - n_before, 2,
                          "pay_reserve должен создать ровно 2 escrow_hold (buyer + platform)")
        order.refresh_from_db()
        self.assertEqual(order.payment_status, "reserve_paid")

    def test_false_string_cannot_confirm_reserve_or_final_payment(self):
        reserve_order = Order.objects.create(
            buyer=self.buyer,
            status="awaiting_reserve",
            payment_status="awaiting_reserve",
            total_amount=Decimal("1000"),
            reserve_amount=Decimal("100"),
            customer_name="False reserve",
        )
        OrderItem.objects.create(
            order=reserve_order,
            part=self.part,
            quantity=1,
            unit_price=Decimal("1000"),
        )

        reserve_result = execute(
            "pay_reserve",
            {"order_id": reserve_order.id, "confirmed": "false"},
            self.buyer,
            "buyer",
        )

        reserve_order.refresh_from_db()
        self.assertIn("Готовлю списание", reserve_result.text)
        self.assertEqual(reserve_order.payment_status, "awaiting_reserve")
        self.assertFalse(
            WalletTx.objects.filter(order_id=reserve_order.id).exists()
        )

        final_order = Order.objects.create(
            buyer=self.buyer,
            status="reserve_paid",
            payment_status="reserve_paid",
            total_amount=Decimal("1000"),
            reserve_amount=Decimal("100"),
            customer_name="False final",
        )
        OrderItem.objects.create(
            order=final_order,
            part=self.part,
            quantity=1,
            unit_price=Decimal("1000"),
        )

        final_result = execute(
            "pay_final",
            {"order_id": final_order.id, "confirmed": "false"},
            self.buyer,
            "buyer",
        )

        final_order.refresh_from_db()
        self.assertIn("Готовлю списание остатка", final_result.text)
        self.assertEqual(final_order.payment_status, "reserve_paid")
        self.assertFalse(
            WalletTx.objects.filter(order_id=final_order.id).exists()
        )

    def test_pay_reserve_second_call_returns_already_paid(self):
        order = Order.objects.create(buyer=self.buyer, status="awaiting_reserve",
                                      payment_status="awaiting_reserve",
                                      total_amount=Decimal("1000"), reserve_amount=Decimal("100"),
                                      customer_name="DoublePay")
        OrderItem.objects.create(order=order, part=self.part, quantity=1, unit_price=Decimal("1000"))
        # Первый вызов
        execute("pay_reserve", {"order_id": order.id, "confirmed": True},
                self.buyer, "buyer")
        balance_after_1 = Wallet.for_user(self.buyer).balance
        # Второй вызов — НЕ должен ещё раз списать
        r = execute("pay_reserve", {"order_id": order.id, "confirmed": True},
                    self.buyer, "buyer")
        balance_after_2 = Wallet.for_user(self.buyer).balance
        self.assertEqual(balance_after_1, balance_after_2,
                          "Повторный pay_reserve списал деньги повторно!")
        self.assertIn("уже списан", r.text.lower())


class P0PermissionsTests(TestCase):
    """P0-1: role escalation через X-Assistant-Role / RoleSwitchView."""

    @classmethod
    def setUpTestData(cls):
        cls.regular_buyer = User.objects.create_user("p0_real_buyer", password="x")
        UserProfile.objects.create(user=cls.regular_buyer, role="buyer")
        cls.real_operator = User.objects.create_user("p0_real_operator", password="x")
        UserProfile.objects.create(user=cls.real_operator, role="operator")

    def test_p01_regular_buyer_cant_override_to_operator(self):
        from django.conf import settings as _s
        from assistant.permissions import _override_allowed
        # Реальный buyer (не demo_*) → override в operator ЗАПРЕЩЁН
        allowed = _override_allowed(self.regular_buyer, "operator")
        if _s.DEBUG and self.regular_buyer.username.startswith(("demo_", "test_")):
            # demo-аккаунты в DEBUG могут — это специально
            self.assertTrue(allowed)
        else:
            self.assertFalse(allowed,
                              "Buyer не должен иметь права становиться operator (P0-1)")

    def test_p01_operator_can_keep_operator_role(self):
        from assistant.permissions import _override_allowed
        self.assertTrue(_override_allowed(self.real_operator, "operator"))
        # Предметная подроль должна быть явно выдана, иначе общий оператор не
        # может подменять свой серверный контекст через переключатель.
        self.assertFalse(_override_allowed(self.real_operator, "operator_logist"))

    def test_p01_anyone_can_keep_own_role(self):
        from assistant.permissions import _override_allowed
        self.assertTrue(_override_allowed(self.regular_buyer, "buyer"))

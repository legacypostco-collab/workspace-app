"""End-to-end smoke-тест полного цикла buyer ↔ seller ↔ operator.

Покрывает три «опоры» системы:
1. Полный pipeline сделки (10 стадий: reserve_paid → ... → completed)
2. Симметричный broadcast в shipment-чаты trёх ролей
3. Operator-эскалация (SLA breach, claim_opened) в support-conv

Запуск:  .venv/bin/python manage.py test assistant.tests_deal_flow_e2e -v 2
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from assistant.models import Conversation, Message
from assistant.order_events import notify_operator_alert, notify_order_event
from marketplace.models import Category, Order, OrderItem, Part, UserProfile

User = get_user_model()


@override_settings(STORAGES={
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}, CHANNEL_LAYERS={"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}})
class DealFlowE2ESmokeTests(TestCase):
    """Smoke полного жизненного цикла заказа."""

    @classmethod
    def setUpTestData(cls):
        # Buyer
        cls.buyer = User.objects.create_user(username="e2e_buyer", password="x")
        UserProfile.objects.create(user=cls.buyer, role="buyer")
        # Seller (поставщик товара в OrderItem)
        cls.seller = User.objects.create_user(username="e2e_seller", password="x")
        UserProfile.objects.create(user=cls.seller, role="seller")
        # Operator (is_staff)
        cls.operator = User.objects.create_user(
            username="e2e_op", password="x", is_staff=True
        )
        UserProfile.objects.create(user=cls.operator, role="operator")
        # Каталог
        cls.cat = Category.objects.create(name="Engine", slug="engine-e2e")
        cls.part = Part.objects.create(
            seller=cls.seller, category=cls.cat,
            title="Main Switch", slug="main-switch-e2e",
            oem_number="RE-E2E-1",
            price=Decimal("295.00"),
            stock_quantity=10,
            condition="oem",
            is_active=True,
        )

    def setUp(self):
        # Пустой order, ровно как create при оплате 10%-резерва.
        self.order = Order.objects.create(
            buyer=self.buyer,
            status="reserve_paid",
            payment_status="reserve_paid",
            total_amount=Decimal("885.00"),
        )
        OrderItem.objects.create(
            order=self.order, part=self.part,
            quantity=3, unit_price=Decimal("295.00"),
        )

    # ── Helpers ──────────────────────────────────────────────

    def _shipment_conv(self, user):
        return Conversation.objects.filter(
            user=user, category="shipment",
            title__startswith=f"Сделка ORD-{self.order.id}",
        ).first()

    def _support_conv(self, user):
        return Conversation.objects.filter(
            user=user, category="support",
            title__startswith="Алерты",
        ).first()

    # ── Tests ────────────────────────────────────────────────

    def test_shipped_event_broadcasts_to_buyer_and_seller(self):
        """Самый базовый: ship_order → buyer и seller получают conv + timeline."""
        notify_order_event(self.order, "shipped", actor=self.seller)

        bconv = self._shipment_conv(self.buyer)
        sconv = self._shipment_conv(self.seller)
        self.assertIsNotNone(bconv, "buyer должен получить shipment-conv")
        self.assertIsNotNone(sconv, "seller должен получить shipment-conv")
        self.assertNotEqual(bconv.id, sconv.id, "у каждой роли свой conv")

        bmsg = bconv.messages.last()
        smsg = sconv.messages.last()
        self.assertEqual(bmsg.role, Message.Role.SYSTEM)
        self.assertTrue(bmsg.cards, "должна быть order_timeline-card")
        # У buyer и seller — РАЗНЫЕ тексты (роле-специфичные).
        self.assertNotEqual(bmsg.content, smsg.content)

    def test_full_pipeline_appends_messages_in_one_conv(self):
        """Несколько событий по одному заказу → один conv, много сообщений."""
        events = [
            "shipped", "transit_abroad", "customs",
            "transit_rf", "issuing", "delivered", "completed",
        ]
        for ev in events:
            notify_order_event(self.order, ev, actor=self.seller)

        bconv = self._shipment_conv(self.buyer)
        # Один conv на (buyer, ORD) — invariant из _shipment_conv
        bconvs = Conversation.objects.filter(
            user=self.buyer, category="shipment",
            title__startswith=f"Сделка ORD-{self.order.id}",
        )
        self.assertEqual(bconvs.count(), 1, "не должно плодить дубликаты")
        self.assertEqual(bconv.messages.count(), len(events))

    def test_operator_target_creates_support_conv_via_alert(self):
        """notify_operator_alert(order, event=sla_breach) → support-conv."""
        # SLA breach — наиболее частый сценарий
        notify_operator_alert(order=self.order, event="sla_breach")

        opconv = self._support_conv(self.operator)
        self.assertIsNotNone(opconv, "operator должен получить алерт-conv")
        self.assertEqual(opconv.role, "operator")
        msg = opconv.messages.last()
        self.assertIn("ORD-", msg.content, "сообщение должно содержать ORD-id")
        # Card timeline тоже прилетает
        self.assertTrue(msg.cards)

    def test_claim_alert_groups_into_same_support_conv(self):
        """Несколько разных алертов одному оператору → один support-conv."""
        notify_operator_alert(order=self.order, event="sla_breach")
        notify_operator_alert(order=self.order, event="claim_opened")

        convs = Conversation.objects.filter(
            user=self.operator, category="support",
            title__startswith="Алерты",
        )
        self.assertEqual(convs.count(), 1, "все алерты группируются в один conv")
        self.assertEqual(convs.first().messages.count(), 2)

    def test_targets_filter_excludes_role(self):
        """targets=('buyer',) — seller НЕ получает уведомление."""
        notify_order_event(self.order, "shipped", actor=self.seller,
                           targets=("buyer",))
        self.assertIsNotNone(self._shipment_conv(self.buyer))
        self.assertIsNone(self._shipment_conv(self.seller),
                          "seller с targets=('buyer',) не должен получить conv")

    def test_websocket_group_send_does_not_crash(self):
        """Smoke: WS broadcast не падает даже без активных подписчиков.

        Проверяет только что вызов notify_order_event не выкидывает исключение
        при наличии in-memory channel layer.
        """
        try:
            notify_order_event(self.order, "shipped", actor=self.seller)
            notify_operator_alert(order=self.order, event="sla_breach")
        except Exception as e:
            self.fail(f"notify_* should not raise: {e}")

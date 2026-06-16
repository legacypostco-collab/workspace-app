"""Тесты ленты важных событий админа (ActivityEvent + admin_activity_feed).

Запуск:
    python manage.py test assistant.tests_activity_feed -v 2
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase

from assistant.actions import _log_activity, execute
from marketplace.models import ActivityEvent, UserProfile

User = get_user_model()


class ActivityFeedTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.buyer = User.objects.create_user("af_buyer", password="x", email="b@x.com")
        UserProfile.objects.create(user=cls.buyer, role="buyer")
        cls.admin = User.objects.create_user("af_admin", password="x", is_superuser=True)
        UserProfile.objects.create(user=cls.admin, role="admin")

    # ── _log_activity пишет событие с IP/кабинетом/позициями ──────
    def test_log_activity_persists_ip_and_meta(self):
        _log_activity("order", actor=self.buyer, ip="91.92.40.204",
                      title="Заказ #1 · 2 поз · $14,500",
                      meta={"order_id": 1, "n_items": 2,
                            "items": [{"name": "Ковш"}, {"name": "Шланг"}]})
        ev = ActivityEvent.objects.get(kind="order")
        self.assertEqual(ev.ip, "91.92.40.204")
        self.assertEqual(ev.actor_id, self.buyer.id)
        self.assertEqual(ev.actor_role, "buyer")
        self.assertEqual(ev.meta["n_items"], 2)

    def test_log_activity_anon_actor_none(self):
        # Гость без аккаунта → actor=None, но IP сохраняется
        from django.contrib.auth.models import AnonymousUser
        _log_activity("rfq", actor=AnonymousUser(), ip="8.8.8.8", title="RFQ #5")
        ev = ActivityEvent.objects.get(kind="rfq")
        self.assertIsNone(ev.actor_id)
        self.assertEqual(ev.ip, "8.8.8.8")

    # ── Лента: админ видит события с IP/кабинетом ─────────────────
    def test_feed_admin_sees_events(self):
        _log_activity("order", actor=self.buyer, ip="91.92.40.204",
                      title="Заказ #1", meta={"order_id": 1,
                                              "items": [{"name": "Ковш"}]})
        r = execute("admin_activity_feed", {}, self.admin, "admin")
        items = r.cards[0]["data"]["items"]
        self.assertEqual(len(items), 1)
        sub = items[0]["subtitle"]
        self.assertIn("91.92.40.204", sub)       # IP
        self.assertIn("af_buyer", sub)            # кабинет
        self.assertIn("Ковш", sub)               # позиции

    def test_feed_kind_filter(self):
        _log_activity("order", actor=self.buyer, ip="1.1.1.1", title="Заказ")
        _log_activity("pricelist", actor=self.buyer, ip="2.2.2.2", title="Прайс")
        r = execute("admin_activity_feed", {"kind": "pricelist"}, self.admin, "admin")
        items = r.cards[0]["data"]["items"]
        self.assertEqual(len(items), 1)
        self.assertIn("2.2.2.2", items[0]["subtitle"])

    # ── Гейт: не-админ не видит ленту ─────────────────────────────
    def test_feed_non_admin_blocked(self):
        # Блокируется на уровне execute() (admin_activity_feed в _ADMIN_ONLY) —
        # buyer не получает данные. Defense-in-depth: внутри ещё _ensure_admin.
        r = execute("admin_activity_feed", {}, self.buyer, "buyer")
        self.assertIn("нет прав", r.text.lower())
        self.assertNotIn("subtitle", str(getattr(r, "cards", "")))

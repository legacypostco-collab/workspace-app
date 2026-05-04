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

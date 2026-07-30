"""Tests:
  • KnowledgeBaseEntry (model + search)
  • kb_faq: чтение из БД + fallback на хардкод
  • escalate_stale_claims management command
"""
from datetime import timedelta
from decimal import Decimal
from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.utils import timezone

from assistant.support_hub import kb_faq
from marketplace.models import (
    KnowledgeBaseEntry,
    Order,
    OrderClaim,
    UserProfile,
)

U = get_user_model()


# ── 1. KnowledgeBaseEntry.search ───────────────────────────────

@pytest.mark.django_db
class TestKbSearch:
    def setup_method(self):
        KnowledgeBaseEntry.objects.all().delete()
        for i, (cat, q, a) in enumerate([
            ("delivery", "Сколько дней идёт груз из Китая?",
             "Морем 30-45, авиа 5-10, авто 15-25."),
            ("payment",  "Как оплатить остаток?",
             "Через депозит платформы — авто-списание при ready_to_ship."),
            ("claims",   "Как открыть рекламацию?",
             "В карточке заказа кнопка «🧾 Открыть рекламацию»."),
            ("kyb",      "Сколько занимает KYB?",
             "Авто-проверки мгновенно, оператор — до 24 ч."),
        ]):
            KnowledgeBaseEntry.objects.create(
                category=cat, question=q, answer=a, sort_order=i * 10,
            )

    def test_empty_query_returns_all_active(self):
        out = list(KnowledgeBaseEntry.search(""))
        assert len(out) == 4

    def test_query_filters_by_question(self):
        out = list(KnowledgeBaseEntry.search("рекламацию"))
        assert any("рекламац" in e.question.lower() for e in out)

    def test_query_filters_by_answer(self):
        # SQLite icontains должен найти по слову в ответе
        out = list(KnowledgeBaseEntry.search("оператор"))
        assert any("оператор" in e.answer.lower() for e in out)

    def test_inactive_entries_hidden(self):
        e = KnowledgeBaseEntry.objects.first()
        e.is_active = False
        e.save()
        out = list(KnowledgeBaseEntry.search(""))
        assert e not in out


# ── 2. kb_faq: DB-first, fallback ──────────────────────────────

@pytest.mark.django_db
class TestKbFaqIntegration:
    def setup_method(self):
        KnowledgeBaseEntry.objects.all().delete()
        self.user = U.objects.create_user(username="kb_u", password="x")
        UserProfile.objects.create(user=self.user, role="buyer", language="ru")

    def test_db_has_entries(self):
        KnowledgeBaseEntry.objects.create(
            category="payment", question="Кастомный вопрос от оператора",
            answer="Кастомный ответ.", sort_order=10,
        )
        r = kb_faq({}, self.user, "buyer")
        assert "из админки" in r.text

    def test_fallback_to_hardcoded_when_db_empty(self):
        # Таблица пуста — используется встроенный набор FAQ.
        r = kb_faq({}, self.user, "buyer")
        assert "встроенная" in r.text
        assert r.cards
        assert r.cards[0]["type"] == "faq"
        assert r.cards[0]["data"]["items"]

    def test_search_in_db(self):
        KnowledgeBaseEntry.objects.create(
            category="payment", question="Уникальный запрос для теста ZYX",
            answer="Уникальный ответ для теста ZYX.", sort_order=10,
        )
        r = kb_faq({"query": "ZYX"}, self.user, "buyer")
        assert "ZYX" in r.text

    def test_views_incremented_on_query(self):
        e = KnowledgeBaseEntry.objects.create(
            category="payment", question="Тест счётчика просмотров FAQ",
            answer="ответ", sort_order=10,
        )
        assert e.views == 0
        kb_faq({"query": "счётчика"}, self.user, "buyer")
        e.refresh_from_db()
        assert e.views == 1


# ── 3. seed_kb_faq management command ──────────────────────────

@pytest.mark.django_db
class TestSeedKbFaq:
    def test_seeds_from_hardcoded(self):
        KnowledgeBaseEntry.objects.all().delete()
        out = StringIO()
        call_command("seed_kb_faq", stdout=out)
        assert KnowledgeBaseEntry.objects.count() >= 10
        assert "Создано" in out.getvalue()

    def test_idempotent(self):
        KnowledgeBaseEntry.objects.all().delete()
        call_command("seed_kb_faq", stdout=StringIO())
        n1 = KnowledgeBaseEntry.objects.count()
        # Повторный запуск — не должен задвоить
        call_command("seed_kb_faq", stdout=StringIO())
        assert KnowledgeBaseEntry.objects.count() == n1


# ── 4. escalate_stale_claims ───────────────────────────────────

@pytest.mark.django_db
class TestEscalateClaims:
    def setup_method(self):
        self.buyer = U.objects.create_user(username="esc_b", password="x")
        self.order = Order.objects.create(
            buyer=self.buyer, customer_name="X",
            customer_email="b@x.local", customer_phone="+7",
            delivery_address="addr", status="delivered",
            payment_status="paid",
            total_amount=Decimal("10000"), reserve_amount=Decimal("1000"),
        )
        # Нужен хотя бы один operator для notify_operator_alert
        self.op = U.objects.create_user(username="esc_op", password="x",
                                          is_staff=True)
        UserProfile.objects.create(user=self.op, role="operator", language="ru")

    def _make_claim(self, status="open", age_days=10):
        c = OrderClaim.objects.create(
            order=self.order, opened_by=self.buyer,
            kind="defect", title="Test stale", description="long ago",
            status=status,
        )
        # Прокручиваем created_at назад
        OrderClaim.objects.filter(id=c.id).update(
            created_at=timezone.now() - timedelta(days=age_days),
        )
        c.refresh_from_db()
        return c

    def test_stale_claim_escalated(self):
        claim = self._make_claim(status="open", age_days=10)
        out = StringIO()
        call_command("escalate_stale_claims", "--days=7", stdout=out)
        claim.refresh_from_db()
        assert claim.escalated_at is not None

    def test_fresh_claim_not_escalated(self):
        claim = self._make_claim(status="open", age_days=2)  # 2 < 7
        call_command("escalate_stale_claims", "--days=7", stdout=StringIO())
        claim.refresh_from_db()
        assert claim.escalated_at is None

    def test_closed_claim_not_escalated(self):
        claim = self._make_claim(status="closed", age_days=30)
        call_command("escalate_stale_claims", "--days=7", stdout=StringIO())
        claim.refresh_from_db()
        assert claim.escalated_at is None

    def test_idempotency(self):
        """Уже эскалированный claim не эскалируется второй раз."""
        claim = self._make_claim(status="open", age_days=10)
        call_command("escalate_stale_claims", "--days=7", stdout=StringIO())
        claim.refresh_from_db()
        first_escalated_at = claim.escalated_at
        assert first_escalated_at is not None
        # Повторный запуск — escalated_at не должен поменяться
        call_command("escalate_stale_claims", "--days=7", stdout=StringIO())
        claim.refresh_from_db()
        assert claim.escalated_at == first_escalated_at

    def test_dry_run_does_not_persist(self):
        claim = self._make_claim(status="open", age_days=10)
        call_command("escalate_stale_claims", "--days=7", "--dry-run",
                     stdout=StringIO())
        claim.refresh_from_db()
        assert claim.escalated_at is None  # dry-run не записал

    def test_custom_threshold(self):
        """--days=3 → claim 5-дневный должен эскалироваться."""
        claim = self._make_claim(status="open", age_days=5)
        call_command("escalate_stale_claims", "--days=3", stdout=StringIO())
        claim.refresh_from_db()
        assert claim.escalated_at is not None

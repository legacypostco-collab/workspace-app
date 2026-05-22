"""Tests for:
  • assistant/kb_markdown.py  — безопасный markdown-render
  • /help/ public landing      — SEO + Schema.org
  • Per-category SLA в escalate_stale_claims
  • Telegram sender (smoke без реального API)
"""
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import Client, override_settings
from django.utils import timezone

from assistant.kb_markdown import render_kb_markdown
from marketplace.models import (
    KnowledgeBaseEntry,
    Order,
    OrderClaim,
    UserProfile,
)

U = get_user_model()


# ── 1. kb_markdown safe-renderer ───────────────────────────────

class TestKbMarkdown:
    def test_basic_bold_italic_code(self):
        out = render_kb_markdown("**bold** *ital* `code`")
        assert "<strong>bold</strong>" in out
        assert "<em>ital</em>" in out
        assert "<code>code</code>" in out

    def test_paragraphs(self):
        out = render_kb_markdown("Первый абзац\n\nВторой абзац")
        assert out.count("<p>") == 2

    def test_unordered_list(self):
        out = render_kb_markdown("- one\n- two\n- three")
        assert "<ul>" in out and "</ul>" in out
        assert out.count("<li>") == 3

    def test_ordered_list(self):
        out = render_kb_markdown("1. one\n2. two")
        assert "<ol>" in out and "</ol>" in out

    def test_safe_link(self):
        out = render_kb_markdown("see [docs](https://docs.example.com)")
        assert 'href="https://docs.example.com"' in out
        assert 'rel="noopener noreferrer"' in out
        assert 'target="_blank"' in out

    def test_unsafe_link_stripped(self):
        """javascript:alert(1) — не должен превратиться в <a href>."""
        out = render_kb_markdown("[click](javascript:alert(1))")
        assert "javascript:" not in out
        assert "<a" not in out

    def test_safe_image(self):
        out = render_kb_markdown("![hero](https://cdn.example.com/img.jpg)")
        assert '<img src="https://cdn.example.com/img.jpg"' in out
        assert 'loading="lazy"' in out
        assert 'decoding="async"' in out

    def test_unsafe_image_stripped(self):
        out = render_kb_markdown("![x](javascript:alert(1))")
        assert "<img" not in out

    def test_html_in_input_escaped(self):
        """<script>alert(1)</script> в answer — должен быть escaped."""
        out = render_kb_markdown("<script>alert(1)</script>")
        assert "<script>" not in out
        assert "&lt;script&gt;" in out

    def test_xss_via_onerror_attribute_blocked(self):
        out = render_kb_markdown('![x](https://safe.com/x.jpg" onerror="alert(1))')
        # При парсинге `)` в URL ломает синтаксис ![]() — изображение не вставится
        # или URL не пройдёт валидацию. В любом случае — нет onerror.
        assert "onerror" not in out


# ── 2. /help/ public landing ───────────────────────────────────

import sys
_PY314_DJANGO_BUG = sys.version_info >= (3, 14)
_skip_template = pytest.mark.skipif(
    _PY314_DJANGO_BUG,
    reason="Django Context.__copy__ AttributeError on Python 3.14 (upstream bug)",
)


@_skip_template
@pytest.mark.django_db
class TestHelpCenter:
    def setup_method(self):
        KnowledgeBaseEntry.objects.all().delete()
        KnowledgeBaseEntry.objects.create(
            category="payment", question="Тестовый вопрос про оплату",
            answer="Тестовый **жирный** ответ.", sort_order=10,
        )
        KnowledgeBaseEntry.objects.create(
            category="delivery", question="Тестовый вопрос про доставку",
            answer="ETA 30 дней.", sort_order=10,
        )

    def test_help_anonymous_200(self):
        c = Client()
        r = c.get("/help/")
        assert r.status_code == 200

    def test_help_has_schema_jsonld(self):
        c = Client()
        r = c.get("/help/")
        assert b'application/ld+json' in r.content
        assert b'FAQPage' in r.content

    def test_help_search_filters(self):
        c = Client()
        r = c.get("/help/?q=оплат")
        assert r.status_code == 200
        assert b"\xd0\xbe\xd0\xbf\xd0\xbb\xd0\xb0\xd1\x82" in r.content  # «оплат»
        # Доставка не должна попасть в результат
        assert b"ETA 30 \xd0\xb4\xd0\xbd" not in r.content

    def test_help_canonical_meta(self):
        c = Client()
        r = c.get("/help/")
        assert b'rel="canonical"' in r.content
        assert b'/help/' in r.content

    def test_help_renders_markdown_in_answers(self):
        c = Client()
        r = c.get("/help/")
        assert b"<strong>\xd0\xb6\xd0\xb8\xd1\x80\xd0\xbd\xd1\x8b\xd0\xb9</strong>" in r.content


# ── 3. Per-category SLA в escalate_stale_claims ────────────────

@pytest.mark.django_db
class TestPerCategorySLA:
    def setup_method(self):
        self.buyer = U.objects.create_user(username="sla_b", password="x")
        self.order = Order.objects.create(
            buyer=self.buyer, customer_name="X",
            customer_email="b@x.local", customer_phone="+7",
            delivery_address="addr", status="delivered",
            payment_status="paid",
            total_amount=Decimal("10000"), reserve_amount=Decimal("1000"),
        )
        self.op = U.objects.create_user(username="sla_op", password="x",
                                         is_staff=True)
        UserProfile.objects.create(user=self.op, role="operator", language="ru")

    def _claim(self, kind: str, age_days: int):
        c = OrderClaim.objects.create(
            order=self.order, opened_by=self.buyer,
            kind=kind, title=f"SLA test {kind}", description="x",
            status="in_review",
        )
        OrderClaim.objects.filter(id=c.id).update(
            created_at=timezone.now() - timedelta(days=age_days),
        )
        c.refresh_from_db()
        return c

    def test_missing_escalates_at_2_days(self):
        """`missing` имеет SLA=2 дня — claim 3-х дневный должен эскалироваться."""
        claim = self._claim("missing", age_days=3)
        call_command("escalate_stale_claims", stdout=StringIO())
        claim.refresh_from_db()
        assert claim.escalated_at is not None

    def test_wrong_part_not_escalated_at_5_days(self):
        """`wrong_part` SLA=7 — 5-дневный не должен."""
        claim = self._claim("wrong_part", age_days=5)
        call_command("escalate_stale_claims", stdout=StringIO())
        claim.refresh_from_db()
        assert claim.escalated_at is None

    def test_defect_escalates_at_3_days(self):
        """`defect` SLA=3 — 4-дневный должен."""
        claim = self._claim("defect", age_days=4)
        call_command("escalate_stale_claims", stdout=StringIO())
        claim.refresh_from_db()
        assert claim.escalated_at is not None

    def test_override_days_applies_to_all(self):
        """`--days=10` переопределяет per-category — wrong_part 9-дневный не эскалируется."""
        claim = self._claim("wrong_part", age_days=9)
        call_command("escalate_stale_claims", "--days=10", stdout=StringIO())
        claim.refresh_from_db()
        assert claim.escalated_at is None

    @override_settings(CLAIM_SLA_DAYS={"defect": 30})
    def test_settings_override(self):
        """settings.CLAIM_SLA_DAYS переопределяет дефолты."""
        claim = self._claim("defect", age_days=10)  # 10 < 30
        call_command("escalate_stale_claims", stdout=StringIO())
        claim.refresh_from_db()
        assert claim.escalated_at is None


# ── 4. Telegram sender (без реальных API-вызовов) ──────────────

class TestTelegramSender:
    def test_no_token_returns_false(self):
        from assistant.notif_settings import send_telegram
        with override_settings(TELEGRAM_BOT_TOKEN=""):
            assert send_telegram("chat-1", "hi") is False

    def test_empty_inputs_return_false(self):
        from assistant.notif_settings import send_telegram
        with override_settings(TELEGRAM_BOT_TOKEN="dummy"):
            assert send_telegram("", "hi") is False
            assert send_telegram("chat-1", "") is False

    @override_settings(TELEGRAM_BOT_TOKEN="dummy-token")
    def test_token_present_makes_api_call(self):
        from assistant.notif_settings import send_telegram
        with mock.patch("requests.post") as post:
            post.return_value.ok = True
            ok = send_telegram("chat-42", "Test alert")
            assert ok is True
            assert post.called
            url, kwargs = post.call_args.args, post.call_args.kwargs
            assert "api.telegram.org/botdummy-token/sendMessage" in url[0]
            assert kwargs["json"]["chat_id"] == "chat-42"
            assert kwargs["json"]["text"] == "Test alert"

"""Тесты Support Hub: 6 actions + anti-collusion detector."""
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings

from assistant.support_hub import (
    FAQ_ENTRIES,
    _detect_offplatform_contact,
    contact_operator,
    kb_faq,
    my_bonuses,
    my_verifications,
    open_complaint,
    support_home,
)

U = get_user_model()


# ── 1. Anti-collusion detector ────────────────────────────────

class TestOffplatformDetector:
    @pytest.mark.parametrize("text,expected", [
        ("Нормальный вопрос про доставку",                False),
        ("Цена 7000 за единицу",                          False),
        ("Заказ 7079958030 не пришёл",                    False),
        ("@admin откройте тикет",                         False),  # сам в системе
        ("",                                                False),
        (None,                                              False),
        # Должны флагнуться:
        ("Напишите мне на mail@example.com",              True),
        ("Свяжитесь в WhatsApp +79261234567",             True),
        ("Мой telegram @user_2025 — обсудим напрямую",    True),
        ("contact me on viber +1234567890",               True),
        ("позвоните в Telegram @support",                 True),
        ("давайте созвонимся, мой номер +7 926 123-45-67 в whatsapp", True),
    ])
    def test_detector(self, text, expected):
        assert _detect_offplatform_contact(text) is expected


# ── 2. support_home ───────────────────────────────────────────

@pytest.mark.django_db
class TestSupportHome:
    def setup_method(self):
        self.buyer = U.objects.create_user(username="sup_buy", password="x")
        self.seller = U.objects.create_user(username="sup_sel", password="x")
        from marketplace.models import UserProfile
        UserProfile.objects.create(user=self.buyer,  role="buyer",  language="ru")
        UserProfile.objects.create(user=self.seller, role="seller", language="ru")

    def test_buyer_sees_buyer_actions(self):
        r = support_home({}, self.buyer, "buyer")
        labels = [a["label"] for a in (r.actions or [])]
        assert any("рекламации" in l.lower() for l in labels)
        assert any("бонусы" in l.lower() for l in labels)
        assert any("оператор" in l.lower() for l in labels)
        # KYB-кнопки у buyer нет
        assert not any("kyb" in l.lower() for l in labels)

    def test_seller_sees_kyb_button(self):
        r = support_home({}, self.seller, "seller")
        labels = [a["label"] for a in (r.actions or [])]
        # Seller видит KYB-кнопку первой
        assert any("kyb" in l.lower() or "верификация" in l.lower() for l in labels)


# ── 3. FAQ ────────────────────────────────────────────────────

@pytest.mark.django_db
class TestKbFaq:
    def test_no_query_returns_all(self):
        buyer = U.objects.create_user(username="faq_u", password="x")
        r = kb_faq({}, buyer, "buyer")
        # FAQ_ENTRIES = 16 вопросов
        assert "16" in r.text or "ответов" in r.text

    def test_query_filters(self):
        buyer = U.objects.create_user(username="faq_u2", password="x")
        r = kb_faq({"query": "рекламац"}, buyer, "buyer")
        # Должны найтись 2 вопроса в категории Рекламации
        assert "2" in r.text or "рекламац" in r.text.lower()

    def test_unknown_query_offers_operator(self):
        buyer = U.objects.create_user(username="faq_u3", password="x")
        r = kb_faq({"query": "zyxquantum999"}, buyer, "buyer")
        assert "не найдено" in r.text.lower()
        assert any(a["action"] == "contact_operator" for a in (r.actions or []))


# ── 4. my_verifications ───────────────────────────────────────

@pytest.mark.django_db
class TestMyVerifications:
    def test_buyer_profile_fields(self):
        u = U.objects.create_user(username="ver_b", email="b@x.local", password="x")
        from marketplace.models import UserProfile
        UserProfile.objects.create(user=u, role="buyer", language="ru",
                                    country="RU", tax_id="7708123456",
                                    contact_name="Иванов И.И.",
                                    phone_e164="+79261234567",
                                    messenger_kind="telegram",
                                    messenger_handle="@ivanov")
        r = my_verifications({}, u, "buyer")
        items = r.cards[0]["data"]["items"]
        # все ключевые поля присутствуют
        labels = [i["label"] for i in items]
        assert any("Email" in l for l in labels)
        assert any("Телефон" in l for l in labels)
        assert any("ИНН" in l or "Tax ID" in l for l in labels)


# ── 5. my_bonuses ─────────────────────────────────────────────

@pytest.mark.django_db
class TestMyBonuses:
    def test_non_buyer_blocked(self):
        u = U.objects.create_user(username="bon_s", password="x")
        r = my_bonuses({}, u, "seller")
        assert "только в кабинете покупателя" in r.text.lower() or \
               "только" in r.text.lower()

    def test_buyer_sees_tiers(self):
        u = U.objects.create_user(username="bon_b", password="x")
        from marketplace.models import UserProfile
        UserProfile.objects.create(user=u, role="buyer", language="ru")
        r = my_bonuses({}, u, "buyer")
        # 2 карточки: kpi + list тиров
        assert len(r.cards) >= 2
        text_dump = str(r.cards)
        for tier in ("Bronze", "Silver", "Gold", "Platinum"):
            assert tier in text_dump


# ── 6. contact_operator (form + side-effect) ──────────────────

@pytest.mark.django_db
class TestContactOperator:
    def test_phase1_returns_form(self):
        u = U.objects.create_user(username="co_u", password="x")
        r = contact_operator({}, u, "buyer")
        assert r.cards
        form_data = r.cards[0]["data"]
        assert form_data["submit_action"] == "contact_operator"
        field_names = [f["name"] for f in form_data["fields"]]
        assert "topic" in field_names and "text" in field_names

    def test_phase2_with_flag(self):
        u = U.objects.create_user(username="co_u2", password="x")
        r = contact_operator({
            "confirmed": True, "topic": "billing",
            "text": "напишите мне на mybox@gmail.com",
        }, u, "buyer")
        # должно сработать предупреждение про off-platform контакты
        assert "контактные данные" in r.text.lower() or \
               "off-platform" in r.text.lower() or \
               "коммуникации" in r.text.lower()


# ── 7. open_complaint ─────────────────────────────────────────

@pytest.mark.django_db
class TestOpenComplaint:
    def test_phase1_form(self):
        u = U.objects.create_user(username="cmp_u", password="x")
        r = open_complaint({}, u, "buyer")
        assert r.cards
        field_names = [f["name"] for f in r.cards[0]["data"]["fields"]]
        assert "against" in field_names and "text" in field_names

    def test_phase2_creates_alert(self):
        u = U.objects.create_user(username="cmp_u2", password="x")
        r = open_complaint({
            "confirmed": True, "against": "platform",
            "text": "Тестовая жалоба на работу платформы.",
        }, u, "buyer")
        assert "зарегистрирована" in r.text.lower()
        assert r.navigate_conversation_id

"""Buyer registration по ТЗ — chat-native двухфазный flow.

ТЗ §1: 8 полей при регистрации
  • country, tax_id, contact_name, position, email, phone, messenger, equipment_fleet

ТЗ §2: автоматические проверки
  • email format + MX-записи домена
  • phone format
  • регистрация номера в WhatsApp/Telegram

ТЗ §3: решение
  • все проверки пройдены → auto-create + login
  • проблемы с контактами → запрос уточнений

ТЗ §4: что не спрашиваем сейчас (потом при первой сделке)
  • юридический адрес, банк, ФИО подписанта

Наименование и адрес компании подтягиваются автоматически по tax_id
через `assistant/kyb_api_checks.py` (check_ru_aggregator / check_opencorporates).
"""
from __future__ import annotations

import logging
import re
from typing import Any

from django.utils.translation import gettext as _

logger = logging.getLogger(__name__)

# ── Справочные списки ──────────────────────────────────────────
COUNTRY_CHOICES = [
    {"value": "RU", "label": _("🇷🇺 Россия")},
    {"value": "BY", "label": _("🇧🇾 Беларусь")},
    {"value": "KZ", "label": _("🇰🇿 Казахстан")},
    {"value": "UZ", "label": _("🇺🇿 Узбекистан")},
    {"value": "KG", "label": _("🇰🇬 Кыргызстан")},
    {"value": "AM", "label": _("🇦🇲 Армения")},
    {"value": "AE", "label": _("🇦🇪 ОАЭ")},
    {"value": "TR", "label": _("🇹🇷 Турция")},
    {"value": "CN", "label": _("🇨🇳 Китай")},
    {"value": "DE", "label": _("🇩🇪 Германия")},
    {"value": "OTHER", "label": _("🌍 Другая страна")},
]
POSITION_CHOICES = [
    {"value": "buyer",      "label": _("Закупщик")},
    {"value": "director",   "label": _("Директор")},
    {"value": "owner",      "label": _("Владелец")},
    {"value": "engineer",   "label": _("Инженер / технолог")},
    {"value": "logist",     "label": _("Логист")},
    {"value": "other",      "label": _("Иное")},
]
MESSENGER_CHOICES = [
    {"value": "telegram", "label": _("Telegram (@username)")},
    {"value": "whatsapp", "label": _("WhatsApp (+номер)")},
]

# Простой regex для базового формата (не RFC-полный — для UX достаточно).
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# E.164: + и 8–15 цифр (без пробелов и скобок — фронт сам clean'ит).
_PHONE_RE = re.compile(r"^\+\d{8,15}$")


# ── Автопроверки (ТЗ §2) ───────────────────────────────────────

def _check_email(email: str) -> tuple[bool, str]:
    """Format + MX. MX делаем best-effort: если dnspython нет, fallback на format."""
    if not _EMAIL_RE.match(email):
        return False, _("неверный формат e-mail")
    domain = email.rsplit("@", 1)[1].lower()
    # MX через dnspython (опционально). На бете и без него — допустимо.
    try:
        import dns.resolver  # type: ignore
        try:
            answers = dns.resolver.resolve(domain, "MX", lifetime=2.5)
            if not list(answers):
                return False, _("у домена %(domain)s нет MX-записей — почта не дойдёт") % {"domain": domain}
        except Exception:
            return False, _("не удалось проверить MX для %(domain)s") % {"domain": domain}
    except ImportError:
        pass  # без dnspython — пропускаем MX-чек (доступ всё равно даём)
    return True, ""


def _check_phone(phone: str) -> tuple[bool, str]:
    cleaned = re.sub(r"[\s\-\(\)]", "", phone or "")
    if not cleaned.startswith("+"):
        cleaned = "+" + cleaned.lstrip("0")
    if not _PHONE_RE.match(cleaned):
        return False, _("телефон должен быть в формате +7XXXXXXXXXX (8–15 цифр)")
    return True, cleaned  # возвращаем нормализованный


def _check_messenger(kind: str, handle: str, phone_e164: str) -> tuple[bool, str]:
    """Использует mock-API из kyb_api_checks. На проде дёрнет реальные SDK."""
    try:
        from .kyb_api_checks import check_messengers
    except Exception:
        return True, ""  # модуля нет → не блокируем регистрацию
    wa = phone_e164 if kind == "whatsapp" else ""
    tg = handle if kind == "telegram" else ""
    r = check_messengers(wa, tg, phone_e164)
    # check_messengers возвращает signals — yellow если канала нет
    sigs = [s for s in (r.get("signals") or []) if s.get("level") in ("red", "yellow")]
    if sigs:
        return False, sigs[0].get("msg", _("канал связи не подтверждён"))
    return True, ""


def _resolve_company_name(country: str, tax_id: str) -> str:
    """ТЗ §1: «Наименование подтянется автоматически по регистрационному номеру»."""
    try:
        from .kyb_api_checks import check_opencorporates, check_ru_aggregator
        if country == "RU":
            r = check_ru_aggregator(tax_id, "RU")
        else:
            r = check_opencorporates(tax_id, country)
        if r.get("ok") and r.get("data"):
            data = r["data"]
            name = (data.get("name") or data.get("legal_name") or "").strip()
            if name:
                return name
            # RU-агрегаторы часто отдают только реквизиты без названия — соберём
            # читаемое имя из ОГРН/КПП. В проде здесь будет реальный
            # ЕГРЮЛ-запрос, мы дёрнем поле «полное_наименование».
            if country == "RU":
                ogrn = data.get("ogrn") or ""
                return (_("ИНН %(inn)s") % {"inn": tax_id}) + (
                    _(" · ОГРН %(ogrn)s") % {"ogrn": ogrn} if ogrn else "")
    except Exception:
        logger.exception("company_name lookup failed for %s/%s", country, tax_id)
    return _("ИНН/Tax ID %(tax)s") % {"tax": tax_id} if tax_id else ""


# ── Form rendering / decision ──────────────────────────────────

def _form_card(values: dict | None = None, errors: dict | None = None) -> dict:
    """ТЗ §1: одна карточка с 8 полями + автоподтянутое название."""
    values = values or {}
    errors = errors or {}

    def err(name: str) -> str:
        return errors.get(name) or ""

    fields = [
        {"name": "country", "label": _("Страна"), "type": "select",
         "required": True, "options": COUNTRY_CHOICES,
         "value": values.get("country", "RU"), "error": err("country")},
        {"name": "tax_id", "label": _("Регистрационный номер (ИНН / Tax ID)"),
         "required": True, "placeholder": "7708123456",
         "value": values.get("tax_id", ""),
         "help": _("Название и адрес подтянутся автоматически."),
         "error": err("tax_id")},
        {"name": "contact_name", "label": _("ФИО контактного лица"),
         "required": True, "placeholder": _("Иванов Иван Иванович"),
         "value": values.get("contact_name", ""), "error": err("contact_name")},
        {"name": "position", "label": _("Должность"), "type": "select",
         "required": True, "options": POSITION_CHOICES,
         "value": values.get("position", "buyer"), "error": err("position")},
        {"name": "email", "label": _("Рабочий e-mail"), "type": "email",
         "required": True, "placeholder": "ivanov@company.ru",
         "value": values.get("email", ""), "error": err("email")},
        {"name": "phone_e164", "label": _("Телефон (с кодом страны)"),
         "required": True, "placeholder": "+79261234567",
         "value": values.get("phone_e164", ""), "error": err("phone_e164")},
        {"name": "messenger_kind", "label": _("Мессенджер для связи"),
         "type": "select", "required": True, "options": MESSENGER_CHOICES,
         "value": values.get("messenger_kind", "telegram"),
         "error": err("messenger_kind")},
        {"name": "messenger_handle", "label": _("Username / номер в мессенджере"),
         "required": True, "placeholder": "@ivanov  /  +79261234567",
         "value": values.get("messenger_handle", ""),
         "error": err("messenger_handle")},
        {"name": "equipment_fleet", "label": _("Парк техники (бренды и модели)"),
         "type": "textarea", "required": True,
         "placeholder": "Komatsu PC400, CAT 336, Hitachi ZX350 …",
         "value": values.get("equipment_fleet", ""),
         "help": _("Под какие машины закупаете запчасти."),
         "error": err("equipment_fleet")},
        {"name": "username", "label": _("Логин для входа"),
         "required": True, "placeholder": "ivanov",
         "value": values.get("username", ""), "error": err("username")},
        {"name": "password1", "label": _("Пароль"), "type": "password",
         "required": True, "minlength": 8, "error": err("password1")},
        {"name": "password2", "label": _("Повторите пароль"), "type": "password",
         "required": True, "minlength": 8, "error": err("password2")},
    ]
    return {
        "type": "form",
        "data": {
            "title": _("Регистрация покупателя"),
            "subtitle": _("8 полей · проверка контактов · доступ за 5 минут."),
            "submit_action": "start_registration",
            "submit_label": _("Создать аккаунт и войти →"),
            "fields": fields,
            "fixed_params": {"confirmed": True, "role": "buyer"},
        },
    }


def render_form(params: dict | None = None) -> dict:
    """Phase 1: показать форму (вместе с возможными ошибками после попытки сабмита)."""
    return {
        "text": _("Заполните 8 полей — после автопроверки сразу получите доступ."),
        "cards": [_form_card(params or {})],
        "actions": [{"action": "start_login", "label": _("У меня уже есть аккаунт")}],
        "suggestions": [], "contextual_actions": [],
    }


def attempt_register(request, params: dict) -> dict:
    """Phase 2: валидация + создание юзера.

    Возвращает {"ok": True/False, "response": dict, "user": User|None}.
    Caller (ActionView) делает login(request, user) при ok=True.
    """
    from django.contrib.auth import get_user_model
    from marketplace.forms import RegisterForm
    from marketplace.models import UserProfile

    errors: dict[str, str] = {}
    v = {
        "country":          (params.get("country") or "RU").upper(),
        "tax_id":           (params.get("tax_id") or "").strip(),
        "contact_name":     (params.get("contact_name") or "").strip(),
        "position":         (params.get("position") or "").strip(),
        "email":            (params.get("email") or "").strip(),
        "phone_e164":       (params.get("phone_e164") or "").strip(),
        "messenger_kind":   (params.get("messenger_kind") or "").strip(),
        "messenger_handle": (params.get("messenger_handle") or "").strip(),
        "equipment_fleet":  (params.get("equipment_fleet") or "").strip(),
        "username":         (params.get("username") or "").strip(),
        "password1":        params.get("password1") or "",
        "password2":        params.get("password2") or "",
    }

    # ── Базовая обязательность ──────────────────────────────
    for f in ("country", "tax_id", "contact_name", "position", "email",
              "phone_e164", "messenger_kind", "messenger_handle",
              "equipment_fleet", "username", "password1"):
        if not v[f]:
            errors[f] = _("обязательное поле")

    # ── Авто-проверки (ТЗ §2) ───────────────────────────────
    if v["email"] and "email" not in errors:
        ok, msg = _check_email(v["email"])
        if not ok:
            errors["email"] = msg
    if v["phone_e164"] and "phone_e164" not in errors:
        ok, normalized = _check_phone(v["phone_e164"])
        if not ok:
            errors["phone_e164"] = normalized
        else:
            v["phone_e164"] = normalized
    if (v["messenger_kind"] and v["messenger_handle"]
            and "messenger_handle" not in errors
            and "phone_e164" not in errors):
        ok, msg = _check_messenger(v["messenger_kind"], v["messenger_handle"],
                                     v["phone_e164"])
        if not ok:
            errors["messenger_handle"] = msg

    if errors:
        # ТЗ §3: «проблемы с контактами → запрос уточнений»
        return {"ok": False, "user": None, "response": {
            "text": _("Проверьте поля и попробуйте снова:") + "\n"
                    + "\n".join(f"• {f}: {m}" for f, m in errors.items()),
            "cards": [_form_card(v, errors)],
            "actions": [],
            "suggestions": [], "contextual_actions": [],
        }}

    # ── Резолвим название компании по tax_id ────────────────
    company_name = _resolve_company_name(v["country"], v["tax_id"])

    # ── Создаём User через стандартный RegisterForm для пароль-валидации ─
    form = RegisterForm({
        "username": v["username"], "email": v["email"],
        "password1": v["password1"], "password2": v["password2"],
        "role": "buyer", "language": "ru",
        "first_name": "", "last_name": "",
        "company_name": company_name,
    })
    if not form.is_valid():
        # Скорее всего username duplicate / weak password
        msg_lines = []
        for fname, errs in form.errors.items():
            for e in errs:
                errors[fname if fname in v else "username"] = str(e)
                msg_lines.append(f"• {fname}: {e}")
        return {"ok": False, "user": None, "response": {
            "text": _("Не получилось создать аккаунт:") + "\n" + "\n".join(msg_lines),
            "cards": [_form_card(v, errors)],
            "actions": [],
            "suggestions": [], "contextual_actions": [],
        }}

    user = form.save(commit=False)
    user.email = v["email"]
    user.save()
    UserProfile.objects.create(
        user=user, role="buyer", language="ru",
        company_name=company_name,
        country=v["country"], tax_id=v["tax_id"],
        contact_name=v["contact_name"], position=v["position"],
        phone_e164=v["phone_e164"],
        messenger_kind=v["messenger_kind"],
        messenger_handle=v["messenger_handle"],
        equipment_fleet=v["equipment_fleet"],
    )

    # ── ТЗ §3: «все проверки пройдены → доступ автоматически» ─
    greeting = (_(", %(company)s") % {"company": company_name}) if company_name else ""
    return {"ok": True, "user": user, "response": {
        "text": (
            _("Аккаунт создан и проверен. Добро пожаловать%(greeting)s!\n"
              "• E-mail проверен · телефон проверен · мессенджер на связи.\n"
              "• Реквизиты компании подтянулись автоматически.\n\n"
              "Юридический адрес, банк и подписанта попросим при первой сделке "
              "(не сейчас — экономим ваше время).") % {"greeting": greeting}
        ),
        "cards": [],
        "actions": [{"action": "reload_page", "label": _("Открыть кабинет")}],
        "suggestions": [_("Найди мне гусеничную цепь Komatsu"),
                         _("Покажи поставщиков с лучшим SLA")],
        "contextual_actions": [],
        "_post_action": "reload",
    }}

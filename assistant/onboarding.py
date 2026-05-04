"""Onboarding + KYB wizard actions.

Цепочка для нового продавца:
  start_onboarding → submit_company_info → submit_legal_address →
  submit_bank → submit_director → submit_for_review →
  [operator: op_kyb_review → op_kyb_approve|op_kyb_reject] →
  ✓ trader status

Используем существующую модель `marketplace.CompanyVerification` — без
новых миграций. Все шаги — DraftCard preview → confirm.
"""
from __future__ import annotations

import logging
import re

from django.utils import timezone

from .actions import ActionResult, _log_event, _notify, register

logger = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────

INN_RE = re.compile(r"^\d{10}(\d{2})?$")    # 10 (юр. лицо) или 12 (ИП)
KPP_RE = re.compile(r"^\d{9}$")
OGRN_RE = re.compile(r"^\d{13}(\d{2})?$")    # 13 (юр) или 15 (ИП)
BIK_RE = re.compile(r"^\d{9}$")
ACCOUNT_RE = re.compile(r"^\d{20}$")


def _kyb(user):
    """Get-or-create CompanyVerification для пользователя."""
    from marketplace.models import CompanyVerification
    kyb, _created = CompanyVerification.objects.get_or_create(user=user)
    return kyb


def _kyb_step(kyb) -> str:
    """Какой шаг текущий — на основании заполненности полей."""
    if kyb.status == "verified":
        return "verified"
    if kyb.status == "pending":
        return "pending"
    if kyb.status == "rejected":
        return "rejected"
    if not (kyb.legal_name and kyb.inn):
        return "company_info"
    if not kyb.legal_address:
        return "legal_address"
    if not (kyb.bank_name and kyb.bik and kyb.bank_account):
        return "bank"
    if not kyb.director_name:
        return "director"
    return "ready_for_review"


def _step_progress(step: str) -> tuple[int, int]:
    order = ["company_info", "legal_address", "bank", "director", "ready_for_review", "pending", "verified"]
    if step in order:
        return order.index(step) + 1, 5  # 5 заполняемых шагов до verify
    return 0, 5


# ══════════════════════════════════════════════════════════
# 0. Точка входа — start_onboarding
# ══════════════════════════════════════════════════════════

@register("start_onboarding")
def start_onboarding(params, user, role):
    """Показать текущий шаг onboarding'а или приветственный экран."""
    kyb = _kyb(user)
    step = _kyb_step(kyb)

    if step == "verified":
        return ActionResult(
            text=f"✓ Компания «{kyb.legal_name}» верифицирована. Все возможности платформы доступны.",
            cards=[{"type": "kpi_grid", "data": {"title": "🛡 Статус KYB", "items": [
                {"label": "Статус", "value": "Верифицирована", "tone": "ok"},
                {"label": "ИНН", "value": kyb.inn or "—"},
                {"label": "Проверено", "value": kyb.reviewed_at.strftime("%d.%m.%Y") if kyb.reviewed_at else "—"},
            ]}}],
        )
    if step == "pending":
        return ActionResult(
            text=(
                f"⏳ Анкета отправлена на проверку оператору ({kyb.submitted_at:%d.%m.%Y %H:%M}).\n"
                f"Обычно проверка занимает до 24 часов. Дождитесь решения — мы пришлём "
                f"нотификацию."
            ),
            cards=[{"type": "kpi_grid", "data": {"title": "🛡 Статус KYB", "items": [
                {"label": "Статус", "value": "На проверке", "tone": "info"},
                {"label": "Компания", "value": kyb.legal_name or "—"},
                {"label": "ИНН", "value": kyb.inn or "—"},
            ]}}],
        )
    if step == "rejected":
        return ActionResult(
            text=(
                f"❌ Анкета отклонена оператором.\nПричина: {kyb.rejection_reason or '—'}\n\n"
                f"Исправьте данные и отправьте повторно."
            ),
            contextual_actions=[
                {"action": "submit_company_info", "label": "🔄 Начать заново"},
            ],
        )

    cur, total = _step_progress(step)
    next_action = {
        "company_info":     ("submit_company_info",  "Реквизиты компании"),
        "legal_address":    ("submit_legal_address", "Юридический адрес"),
        "bank":             ("submit_bank",          "Банковские реквизиты"),
        "director":         ("submit_director",      "Директор"),
        "ready_for_review": ("submit_for_review",    "Отправить на проверку"),
    }[step]

    return ActionResult(
        text=(
            f"👋 Добро пожаловать! Чтобы заключать сделки на платформе, "
            f"пройдите верификацию компании (KYB).\n"
            f"Шаг {cur}/{total} · {next_action[1]}"
        ),
        cards=[{"type": "kpi_grid", "data": {"title": "🚀 Onboarding", "items": [
            {"label": "Шаг", "value": f"{cur}/{total}", "tone": "info"},
            {"label": "Текущий", "value": next_action[1]},
            {"label": "Статус", "value": "Черновик"},
        ]}}],
        actions=[
            {"action": next_action[0], "label": f"➡ {next_action[1]}"},
        ],
        suggestions=[
            "Сколько времени занимает верификация?",
            "Какие документы нужны?",
        ],
    )


# ══════════════════════════════════════════════════════════
# 1. Реквизиты компании (legal_name + ИНН + КПП + ОГРН)
# ══════════════════════════════════════════════════════════

@register("submit_company_info")
def submit_company_info(params, user, role):
    kyb = _kyb(user)
    legal_name = (params.get("legal_name") or "").strip()
    inn = (params.get("inn") or "").strip()
    kpp = (params.get("kpp") or "").strip()
    ogrn = (params.get("ogrn") or "").strip()
    confirmed = bool(params.get("confirmed"))

    if not confirmed or not (legal_name and inn):
        return ActionResult(
            text="📋 Шаг 1/5 · Реквизиты компании",
            cards=[{"type": "form", "data": {
                "title": "📋 Шаг 1/5 · Реквизиты компании",
                "submit_action": "submit_company_info",
                "fields": [
                    {"name": "legal_name", "label": "Полное наименование (как в ЕГРЮЛ)",
                     "required": True, "value": kyb.legal_name},
                    {"name": "inn", "label": "ИНН (10 цифр для юр.лица, 12 для ИП)",
                     "required": True, "value": kyb.inn},
                    {"name": "kpp", "label": "КПП (для юр.лица, 9 цифр)", "value": kyb.kpp},
                    {"name": "ogrn", "label": "ОГРН (13 или 15 цифр)", "value": kyb.ogrn},
                ],
                "fixed_params": {"confirmed": True},
            }}],
        )

    # Валидация
    errors = []
    if not INN_RE.match(inn):
        errors.append("ИНН должен быть 10 или 12 цифр")
    if kpp and not KPP_RE.match(kpp):
        errors.append("КПП должен быть 9 цифр")
    if ogrn and not OGRN_RE.match(ogrn):
        errors.append("ОГРН должен быть 13 или 15 цифр")
    if errors:
        return ActionResult(text="⚠️ Проверьте данные:\n• " + "\n• ".join(errors))

    kyb.legal_name = legal_name
    kyb.inn = inn
    kyb.kpp = kpp
    kyb.ogrn = ogrn
    kyb.save(update_fields=["legal_name", "inn", "kpp", "ogrn"])

    return ActionResult(
        text=f"✓ Шаг 1/5 готов · ИНН {inn} принят.",
        actions=[
            {"action": "submit_legal_address", "label": "➡ Шаг 2/5 · Юридический адрес"},
        ],
    )


# ══════════════════════════════════════════════════════════
# 2. Юридический адрес
# ══════════════════════════════════════════════════════════

@register("submit_legal_address")
def submit_legal_address(params, user, role):
    kyb = _kyb(user)
    address = (params.get("legal_address") or "").strip()
    confirmed = bool(params.get("confirmed"))

    if not confirmed or not address:
        return ActionResult(
            text="📍 Шаг 2/5 · Юридический адрес",
            cards=[{"type": "form", "data": {
                "title": "📍 Шаг 2/5 · Юридический адрес",
                "submit_action": "submit_legal_address",
                "fields": [
                    {"name": "legal_address", "label": "Адрес как в ЕГРЮЛ",
                     "type": "textarea", "required": True, "value": kyb.legal_address},
                ],
                "fixed_params": {"confirmed": True},
            }}],
        )

    kyb.legal_address = address
    kyb.save(update_fields=["legal_address"])
    return ActionResult(
        text="✓ Шаг 2/5 готов.",
        actions=[
            {"action": "submit_bank", "label": "➡ Шаг 3/5 · Банковские реквизиты"},
        ],
    )


# ══════════════════════════════════════════════════════════
# 3. Банковские реквизиты
# ══════════════════════════════════════════════════════════

@register("submit_bank")
def submit_bank(params, user, role):
    kyb = _kyb(user)
    bank_name = (params.get("bank_name") or "").strip()
    bik = (params.get("bik") or "").strip()
    account = (params.get("bank_account") or "").strip()
    confirmed = bool(params.get("confirmed"))

    if not confirmed or not (bank_name and bik and account):
        return ActionResult(
            text="🏦 Шаг 3/5 · Банковские реквизиты",
            cards=[{"type": "form", "data": {
                "title": "🏦 Шаг 3/5 · Банковские реквизиты",
                "submit_action": "submit_bank",
                "fields": [
                    {"name": "bank_name", "label": "Наименование банка", "required": True,
                     "value": kyb.bank_name},
                    {"name": "bik", "label": "БИК (9 цифр)", "required": True, "value": kyb.bik},
                    {"name": "bank_account", "label": "Расчётный счёт (20 цифр)",
                     "required": True, "value": kyb.bank_account},
                ],
                "fixed_params": {"confirmed": True},
            }}],
        )

    errors = []
    if not BIK_RE.match(bik):
        errors.append("БИК должен быть 9 цифр")
    if not ACCOUNT_RE.match(account):
        errors.append("Расчётный счёт должен быть 20 цифр")
    if errors:
        return ActionResult(text="⚠️ Проверьте данные:\n• " + "\n• ".join(errors))

    kyb.bank_name = bank_name
    kyb.bik = bik
    kyb.bank_account = account
    kyb.save(update_fields=["bank_name", "bik", "bank_account"])

    return ActionResult(
        text="✓ Шаг 3/5 готов.",
        actions=[
            {"action": "submit_director", "label": "➡ Шаг 4/5 · Директор"},
        ],
    )


# ══════════════════════════════════════════════════════════
# 4. Директор
# ══════════════════════════════════════════════════════════

@register("submit_director")
def submit_director(params, user, role):
    kyb = _kyb(user)
    name = (params.get("director_name") or "").strip()
    confirmed = bool(params.get("confirmed"))

    if not confirmed or not name:
        return ActionResult(
            text="👤 Шаг 4/5 · Директор / уполномоченное лицо",
            cards=[{"type": "form", "data": {
                "title": "👤 Шаг 4/5 · Директор",
                "submit_action": "submit_director",
                "fields": [
                    {"name": "director_name", "label": "ФИО директора (как в паспорте)",
                     "required": True, "value": kyb.director_name},
                ],
                "fixed_params": {"confirmed": True},
            }}],
        )

    kyb.director_name = name
    kyb.save(update_fields=["director_name"])
    return ActionResult(
        text="✓ Шаг 4/5 готов.",
        actions=[
            {"action": "submit_for_review", "label": "➡ Шаг 5/5 · Отправить на проверку"},
        ],
    )


# ══════════════════════════════════════════════════════════
# 5. Финал — submit_for_review
# ══════════════════════════════════════════════════════════

@register("submit_for_review")
def submit_for_review(params, user, role):
    kyb = _kyb(user)
    step = _kyb_step(kyb)
    if step != "ready_for_review":
        return ActionResult(
            text=f"Анкета не готова к отправке (текущий шаг: {step}). Заполните все поля.",
            actions=[{"action": "start_onboarding", "label": "Продолжить заполнение"}],
        )

    confirmed = bool(params.get("confirmed"))
    if not confirmed:
        return ActionResult(
            text="📨 Шаг 5/5 · Отправка на проверку",
            cards=[{"type": "draft", "data": {
                "title": "Подтвердите отправку анкеты",
                "rows": [
                    {"label": "Компания", "value": kyb.legal_name, "primary": True},
                    {"label": "ИНН / ОГРН", "value": f"{kyb.inn} / {kyb.ogrn or '—'}"},
                    {"label": "Адрес", "value": kyb.legal_address[:80]},
                    {"label": "Банк", "value": f"{kyb.bank_name} (БИК {kyb.bik})"},
                    {"label": "Счёт", "value": kyb.bank_account},
                    {"label": "Директор", "value": kyb.director_name},
                ],
                "warnings": [
                    "После отправки данные нельзя редактировать до решения оператора.",
                    "Проверка обычно занимает до 24 часов.",
                ],
                "confirm_action": "submit_for_review",
                "confirm_label": "📨 Отправить на проверку",
                "confirm_params": {"confirmed": True},
                "cancel_label": "Отмена",
            }}],
        )

    kyb.status = "pending"
    kyb.submitted_at = timezone.now()
    kyb.rejection_reason = ""
    kyb.save(update_fields=["status", "submitted_at", "rejection_reason"])

    # Уведомляем всех операторов
    try:
        from django.contrib.auth import get_user_model
        for op in get_user_model().objects.filter(username__icontains="operator")[:5]:
            _notify(
                op, kind="system",
                title=f"Новая KYB-анкета: {kyb.legal_name}",
                body=f"ИНН {kyb.inn} · от {user.username}. Проверьте и одобрите/отклоните.",
                url="/chat/",
            )
    except Exception:
        logger.exception("notify operators on KYB submit failed")

    return ActionResult(
        text=(
            f"✓ Анкета «{kyb.legal_name}» отправлена на проверку.\n"
            f"Мы пришлём нотификацию когда оператор примет решение (обычно в течение 24 часов)."
        ),
        contextual_actions=[
            {"action": "start_onboarding", "label": "Статус анкеты"},
        ],
    )


@register("kyb_status")
def kyb_status(params, user, role):
    """Read-only: статус KYB пользователя."""
    return start_onboarding(params, user, role)


# ══════════════════════════════════════════════════════════
# Operator — модерация KYB
# ══════════════════════════════════════════════════════════

def _is_operator(role: str) -> bool:
    return bool(role) and (role == "operator" or role.startswith("operator_"))


@register("op_kyb_queue")
def op_kyb_queue(params, user, role):
    if not _is_operator(role):
        return ActionResult(text="Доступно только оператору.")
    from marketplace.models import CompanyVerification
    pending = list(CompanyVerification.objects.filter(status="pending").order_by("submitted_at")[:20])
    rows = [
        {
            "title": f"{kyb.legal_name or '—'} (ИНН {kyb.inn})",
            "subtitle": f"От {kyb.user.username} · {kyb.submitted_at:%d.%m %H:%M}",
        }
        for kyb in pending
    ]
    items_for_actions = [
        {"action": "op_kyb_review", "label": f"#{kyb.user.id} {kyb.legal_name[:30]}",
         "params": {"user_id": kyb.user.id}}
        for kyb in pending[:5]
    ]
    return ActionResult(
        text=f"📋 Очередь KYB · {len(pending)} анкет ждут проверки.",
        cards=[{"type": "list", "data": {
            "title": "🛡 KYB на модерации",
            "items": rows or [{"title": "Очередь пуста", "subtitle": "Все анкеты обработаны"}],
        }}],
        contextual_actions=items_for_actions,
    )


@register("op_kyb_review")
def op_kyb_review(params, user, role):
    if not _is_operator(role):
        return ActionResult(text="Доступно только оператору.")
    from marketplace.models import CompanyVerification
    try:
        kyb = CompanyVerification.objects.get(user_id=int(params.get("user_id") or 0))
    except (CompanyVerification.DoesNotExist, ValueError, TypeError):
        return ActionResult(text="Анкета не найдена.")

    rows = [
        {"label": "Компания", "value": kyb.legal_name or "—", "primary": True},
        {"label": "ИНН / КПП", "value": f"{kyb.inn or '—'} / {kyb.kpp or '—'}"},
        {"label": "ОГРН", "value": kyb.ogrn or "—"},
        {"label": "Адрес", "value": kyb.legal_address[:120] or "—"},
        {"label": "Банк / БИК", "value": f"{kyb.bank_name or '—'} ({kyb.bik or '—'})"},
        {"label": "Счёт", "value": kyb.bank_account or "—"},
        {"label": "Директор", "value": kyb.director_name or "—"},
        {"label": "Статус", "value": kyb.get_status_display()},
    ]
    if kyb.submitted_at:
        rows.append({"label": "Подана", "value": kyb.submitted_at.strftime("%d.%m.%Y %H:%M")})

    actions = []
    if kyb.status == "pending":
        actions = [
            {"action": "op_kyb_approve", "label": "✓ Одобрить", "params": {"user_id": kyb.user_id}},
            {"action": "op_kyb_reject", "label": "✗ Отклонить", "params": {"user_id": kyb.user_id}},
        ]

    return ActionResult(
        text=f"🛡 KYB · {kyb.legal_name or '—'}",
        cards=[{"type": "draft", "data": {
            "title": f"Анкета KYB · {kyb.user.username}",
            "rows": rows,
            "confirm_label": "—",
        }}],
        actions=actions,
        contextual_actions=[
            {"action": "op_kyb_queue", "label": "← Очередь"},
        ],
    )


@register("op_kyb_approve")
def op_kyb_approve(params, user, role):
    if not _is_operator(role):
        return ActionResult(text="Доступно только оператору.")
    from marketplace.models import CompanyVerification
    confirmed = bool(params.get("confirmed"))
    try:
        kyb = CompanyVerification.objects.get(user_id=int(params.get("user_id") or 0))
    except (CompanyVerification.DoesNotExist, ValueError, TypeError):
        return ActionResult(text="Анкета не найдена.")

    if kyb.status != "pending":
        return ActionResult(text=f"Анкета не в статусе pending (сейчас: {kyb.get_status_display()}).")

    if not confirmed:
        return ActionResult(
            text=f"Одобрить анкету {kyb.legal_name}?",
            cards=[{"type": "draft", "data": {
                "title": f"✓ Одобрение KYB · {kyb.legal_name}",
                "rows": [
                    {"label": "Компания", "value": kyb.legal_name, "primary": True},
                    {"label": "ИНН", "value": kyb.inn},
                    {"label": "Пользователь", "value": kyb.user.username},
                ],
                "confirm_action": "op_kyb_approve",
                "confirm_label": "✓ Одобрить",
                "confirm_params": {"user_id": kyb.user_id, "confirmed": True},
                "cancel_label": "Отмена",
            }}],
        )

    kyb.status = "verified"
    kyb.reviewed_at = timezone.now()
    kyb.reviewed_by = user
    kyb.rejection_reason = ""
    kyb.save(update_fields=["status", "reviewed_at", "reviewed_by", "rejection_reason"])

    _notify(
        kyb.user, kind="system",
        title=f"✓ KYB одобрен · {kyb.legal_name}",
        body="Все возможности платформы теперь доступны: можно отвечать на RFQ, оформлять заказы, управлять каталогом.",
        url="/chat/",
    )

    return ActionResult(
        text=f"✓ KYB одобрен · «{kyb.legal_name}» (ИНН {kyb.inn}). Уведомление отправлено.",
        contextual_actions=[
            {"action": "op_kyb_queue", "label": "← Очередь"},
        ],
    )


@register("op_kyb_reject")
def op_kyb_reject(params, user, role):
    if not _is_operator(role):
        return ActionResult(text="Доступно только оператору.")
    from marketplace.models import CompanyVerification
    try:
        kyb = CompanyVerification.objects.get(user_id=int(params.get("user_id") or 0))
    except (CompanyVerification.DoesNotExist, ValueError, TypeError):
        return ActionResult(text="Анкета не найдена.")
    if kyb.status != "pending":
        return ActionResult(text=f"Анкета не в статусе pending (сейчас: {kyb.get_status_display()}).")

    reason = (params.get("reason") or "").strip()
    confirmed = bool(params.get("confirmed"))
    if not confirmed or not reason:
        return ActionResult(
            text="Укажите причину отклонения",
            cards=[{"type": "form", "data": {
                "title": f"✗ Отклонить KYB · {kyb.legal_name}",
                "submit_action": "op_kyb_reject",
                "fields": [
                    {"name": "reason", "label": "Причина (видна заявителю)",
                     "type": "textarea", "required": True},
                ],
                "fixed_params": {"user_id": kyb.user_id, "confirmed": True},
            }}],
        )

    kyb.status = "rejected"
    kyb.rejection_reason = reason
    kyb.reviewed_at = timezone.now()
    kyb.reviewed_by = user
    kyb.save(update_fields=["status", "rejection_reason", "reviewed_at", "reviewed_by"])

    _notify(
        kyb.user, kind="system",
        title=f"✗ KYB отклонён · {kyb.legal_name}",
        body=f"Причина: {reason[:160]}. Исправьте данные и отправьте повторно.",
        url="/chat/",
    )

    return ActionResult(
        text=f"✗ KYB отклонён · «{kyb.legal_name}». Причина передана заявителю.",
        contextual_actions=[
            {"action": "op_kyb_queue", "label": "← Очередь"},
        ],
    )


# ══════════════════════════════════════════════════════════
# Gating helper — exposed для actions.py / can_execute()
# ══════════════════════════════════════════════════════════

def kyb_required_for_seller(user) -> bool:
    """True если у пользователя KYB не verified (нужно блокировать seller-actions).

    Demo-аккаунты (demo_*) пропускаются — у них статусы могут быть пустыми, но они
    должны работать «из коробки» для презентаций.
    """
    if not user or not user.is_authenticated:
        return False
    if (user.username or "").startswith("demo_"):
        return False
    try:
        from marketplace.models import CompanyVerification
        kyb = CompanyVerification.objects.filter(user=user).first()
        if not kyb:
            return True
        return kyb.status != "verified"
    except Exception:
        return False

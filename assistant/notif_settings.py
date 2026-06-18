"""User-facing actions for managing durable notification preferences.

  notif_prefs       — показать текущие настройки
  notif_set_email   — включить/выключить email-канал
  notif_set_kinds   — какие kinds доставлять в durable
  notif_link_telegram — связать Telegram chat_id (после /start у бота)
"""
from __future__ import annotations

import logging

from django.utils.translation import gettext as _

from .actions import ActionResult, register

logger = logging.getLogger(__name__)


VALID_KINDS = {"order", "payment", "rfq", "sla", "claim", "system", "info"}


def _get_or_create_profile(user):
    """UserProfile с автоматическим созданием если нет."""
    from marketplace.models import UserProfile
    profile = getattr(user, "profile", None) or getattr(user, "userprofile", None)
    if profile:
        return profile
    profile, _ = UserProfile.objects.get_or_create(user=user)
    return profile


@register("notif_prefs")
def notif_prefs(params, user, role):
    """Показать текущие настройки durable-каналов."""
    p = _get_or_create_profile(user)
    items = [
        {"label": _("Email-канал"), "value": _("✓ Вкл") if p.notif_email_enabled else _("✗ Выкл"),
         "tone": "ok" if p.notif_email_enabled else "warn"},
        {"label": "Email", "value": user.email or "—"},
        {"label": "Telegram", "value":
            (_("✓ Вкл") if p.notif_telegram_enabled and p.notif_telegram_chat_id else _("✗ Не подключён")),
         "tone": "ok" if (p.notif_telegram_enabled and p.notif_telegram_chat_id) else "warn"},
        {"label": "Telegram chat_id", "value": p.notif_telegram_chat_id or "—"},
        {"label": _("Типы событий"), "value": p.notif_kinds},
    ]
    return ActionResult(
        text=(
            _("🔔 Настройки уведомлений · email %(email)s, telegram %(tg)s.") % {
                "email": _("вкл") if p.notif_email_enabled else _("выкл"),
                "tg": _("вкл") if p.notif_telegram_enabled and p.notif_telegram_chat_id else _("выкл"),
            }
        ),
        cards=[{"type": "kpi_grid", "data": {"title": _("🔔 Каналы доставки"), "items": items}}],
        contextual_actions=[
            {"action": "notif_set_email", "label": _("📧 Email вкл/выкл")},
            {"action": "notif_set_kinds", "label": _("🏷 Какие события")},
            {"action": "notif_link_telegram", "label": _("✈️ Подключить Telegram")},
        ],
    )


@register("notif_set_email")
def notif_set_email(params, user, role):
    p = _get_or_create_profile(user)
    enabled = params.get("enabled")
    confirmed = bool(params.get("confirmed"))

    if not confirmed or enabled is None:
        return ActionResult(
            text=_("Включить или выключить email-канал?"),
            cards=[{"type": "form", "data": {
                "title": _("📧 Email-уведомления"),
                "submit_action": "notif_set_email",
                "fields": [{
                    "name": "enabled", "label": _("Включить"),
                    "type": "select",
                    "options": [
                        {"value": "1", "label": _("✓ Включить")},
                        {"value": "0", "label": _("✗ Выключить")},
                    ],
                    "value": "1" if p.notif_email_enabled else "0",
                }],
                "fixed_params": {"confirmed": True},
            }}],
        )

    new_val = str(enabled).strip() in ("1", "true", "yes", "on")
    p.notif_email_enabled = new_val
    p.save(update_fields=["notif_email_enabled"])
    return ActionResult(
        text=_("✓ Email-канал %(state)s.") % {"state": _("включён") if new_val else _("выключен")},
        contextual_actions=[
            {"action": "notif_prefs", "label": _("← Все настройки")},
        ],
    )


@register("notif_set_kinds")
def notif_set_kinds(params, user, role):
    p = _get_or_create_profile(user)
    raw = (params.get("kinds") or "").strip()
    confirmed = bool(params.get("confirmed"))

    if not confirmed or not raw:
        return ActionResult(
            text=_("Какие типы событий доставлять в email/telegram?"),
            cards=[{"type": "form", "data": {
                "title": _("🏷 Типы уведомлений"),
                "submit_action": "notif_set_kinds",
                "fields": [{
                    "name": "kinds",
                    "label": _("Через запятую: order, payment, rfq, sla, claim, system, info"),
                    "value": p.notif_kinds,
                    "required": True,
                }],
                "fixed_params": {"confirmed": True},
            }}],
        )

    requested = [k.strip().lower() for k in raw.split(",") if k.strip()]
    valid = [k for k in requested if k in VALID_KINDS]
    if not valid:
        return ActionResult(
            text=_("⚠️ Не распознано ни одного типа. Допустимые: %(kinds)s") % {
                "kinds": ", ".join(sorted(VALID_KINDS))},
        )
    csv = ",".join(valid)
    p.notif_kinds = csv
    p.save(update_fields=["notif_kinds"])
    return ActionResult(
        text=_("✓ Будут приходить: %(kinds)s.") % {"kinds": csv},
        contextual_actions=[
            {"action": "notif_prefs", "label": _("← Все настройки")},
        ],
    )


# ── Telegram sender (для эскалаций и critical-alerts) ─────────

def send_telegram(chat_id: str, text: str) -> bool:
    """Отправляет сообщение в Telegram через Bot API.

    Тихо возвращает False если:
      • TELEGRAM_BOT_TOKEN не задан в env
      • requests не установлен
      • API вернул ошибку

    Не падает — не блокирует основной flow при сбоях TG.
    """
    from django.conf import settings
    token = getattr(settings, "TELEGRAM_BOT_TOKEN", "")
    if not (token and chat_id and text):
        return False
    try:
        import requests  # type: ignore
    except ImportError:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id, "text": text[:4000],
                "disable_web_page_preview": True,
            },
            timeout=5,
        )
        return r.ok
    except Exception:
        return False


def send_telegram_to_operators(text: str) -> int:
    """Шлёт push всем операторам у которых notif_telegram_enabled+chat_id.

    Возвращает количество успешных отправок. Используется в:
      • escalate_stale_claims management command
      • любой будущий critical-alert (KYB fraud, SLA breach)
    """
    from django.contrib.auth import get_user_model
    from django.db.models import Q
    U = get_user_model()

    ops = (U.objects.filter(is_active=True)
           .filter(Q(is_staff=True) | Q(profile__role__startswith="operator"))
           .filter(profile__notif_telegram_enabled=True)
           .exclude(profile__notif_telegram_chat_id="")
           .select_related("profile")
           .distinct()[:20])
    sent = 0
    for op in ops:
        chat_id = op.profile.notif_telegram_chat_id
        if send_telegram(chat_id, text):
            sent += 1
    logger.info("send_telegram_to_operators: %d / %d delivered", sent, ops.count())
    return sent


@register("notif_link_telegram")
def notif_link_telegram(params, user, role):
    """Привязать Telegram chat_id.

    Ожидаемый flow в проде:
      1. Пользователь пишет нашему боту /start → бот отвечает chat_id
      2. Пользователь вставляет chat_id сюда

    В demo (без реального бота) — просто принимаем числовой chat_id и
    активируем канал.
    """
    p = _get_or_create_profile(user)
    chat_id = (params.get("chat_id") or "").strip()
    confirmed = bool(params.get("confirmed"))

    if not confirmed or not chat_id:
        bot_username = "your_bot"  # в проде взять из env TELEGRAM_BOT_USERNAME
        return ActionResult(
            text=(
                _(
                    "✈️ Подключение Telegram\n\n"
                    "1. Откройте бота: @%(bot)s\n"
                    "2. Отправьте /start\n"
                    "3. Бот ответит вашим chat_id — вставьте сюда"
                ) % {"bot": bot_username}
            ),
            cards=[{"type": "form", "data": {
                "title": _("✈️ Telegram chat_id"),
                "submit_action": "notif_link_telegram",
                "fields": [{
                    "name": "chat_id",
                    "label": _("Числовой chat_id из ответа бота"),
                    "required": True,
                    "value": p.notif_telegram_chat_id,
                }],
                "fixed_params": {"confirmed": True},
            }}],
        )

    if not chat_id.lstrip("-").isdigit():
        return ActionResult(text=_("⚠️ chat_id должен быть числом (может быть отрицательным)."))

    p.notif_telegram_chat_id = chat_id
    p.notif_telegram_enabled = True
    p.save(update_fields=["notif_telegram_chat_id", "notif_telegram_enabled"])
    return ActionResult(
        text=_("✓ Telegram подключён (chat_id=%(chat_id)s).") % {"chat_id": chat_id},
        contextual_actions=[
            {"action": "notif_prefs", "label": _("← Все настройки")},
        ],
    )

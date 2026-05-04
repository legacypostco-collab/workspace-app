"""User-facing actions for managing durable notification preferences.

  notif_prefs       — показать текущие настройки
  notif_set_email   — включить/выключить email-канал
  notif_set_kinds   — какие kinds доставлять в durable
  notif_link_telegram — связать Telegram chat_id (после /start у бота)
"""
from __future__ import annotations

import logging

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
        {"label": "Email-канал", "value": "✓ Вкл" if p.notif_email_enabled else "✗ Выкл",
         "tone": "ok" if p.notif_email_enabled else "warn"},
        {"label": "Email", "value": user.email or "—"},
        {"label": "Telegram", "value":
            ("✓ Вкл" if p.notif_telegram_enabled and p.notif_telegram_chat_id else "✗ Не подключён"),
         "tone": "ok" if (p.notif_telegram_enabled and p.notif_telegram_chat_id) else "warn"},
        {"label": "Telegram chat_id", "value": p.notif_telegram_chat_id or "—"},
        {"label": "Типы событий", "value": p.notif_kinds},
    ]
    return ActionResult(
        text=(
            f"🔔 Настройки уведомлений · email "
            f"{'вкл' if p.notif_email_enabled else 'выкл'}, telegram "
            f"{'вкл' if p.notif_telegram_enabled and p.notif_telegram_chat_id else 'выкл'}."
        ),
        cards=[{"type": "kpi_grid", "data": {"title": "🔔 Каналы доставки", "items": items}}],
        contextual_actions=[
            {"action": "notif_set_email", "label": "📧 Email вкл/выкл"},
            {"action": "notif_set_kinds", "label": "🏷 Какие события"},
            {"action": "notif_link_telegram", "label": "✈️ Подключить Telegram"},
        ],
    )


@register("notif_set_email")
def notif_set_email(params, user, role):
    p = _get_or_create_profile(user)
    enabled = params.get("enabled")
    confirmed = bool(params.get("confirmed"))

    if not confirmed or enabled is None:
        return ActionResult(
            text="Включить или выключить email-канал?",
            cards=[{"type": "form", "data": {
                "title": "📧 Email-уведомления",
                "submit_action": "notif_set_email",
                "fields": [{
                    "name": "enabled", "label": "Включить",
                    "type": "select",
                    "options": [
                        {"value": "1", "label": "✓ Включить"},
                        {"value": "0", "label": "✗ Выключить"},
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
        text=f"✓ Email-канал {'включён' if new_val else 'выключен'}.",
        contextual_actions=[
            {"action": "notif_prefs", "label": "← Все настройки"},
        ],
    )


@register("notif_set_kinds")
def notif_set_kinds(params, user, role):
    p = _get_or_create_profile(user)
    raw = (params.get("kinds") or "").strip()
    confirmed = bool(params.get("confirmed"))

    if not confirmed or not raw:
        return ActionResult(
            text="Какие типы событий доставлять в email/telegram?",
            cards=[{"type": "form", "data": {
                "title": "🏷 Типы уведомлений",
                "submit_action": "notif_set_kinds",
                "fields": [{
                    "name": "kinds",
                    "label": "Через запятую: order, payment, rfq, sla, claim, system, info",
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
            text=f"⚠️ Не распознано ни одного типа. Допустимые: {', '.join(sorted(VALID_KINDS))}",
        )
    csv = ",".join(valid)
    p.notif_kinds = csv
    p.save(update_fields=["notif_kinds"])
    return ActionResult(
        text=f"✓ Будут приходить: {csv}.",
        contextual_actions=[
            {"action": "notif_prefs", "label": "← Все настройки"},
        ],
    )


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
                f"✈️ Подключение Telegram\n\n"
                f"1. Откройте бота: @{bot_username}\n"
                f"2. Отправьте /start\n"
                f"3. Бот ответит вашим chat_id — вставьте сюда"
            ),
            cards=[{"type": "form", "data": {
                "title": "✈️ Telegram chat_id",
                "submit_action": "notif_link_telegram",
                "fields": [{
                    "name": "chat_id",
                    "label": "Числовой chat_id из ответа бота",
                    "required": True,
                    "value": p.notif_telegram_chat_id,
                }],
                "fixed_params": {"confirmed": True},
            }}],
        )

    if not chat_id.lstrip("-").isdigit():
        return ActionResult(text="⚠️ chat_id должен быть числом (может быть отрицательным).")

    p.notif_telegram_chat_id = chat_id
    p.notif_telegram_enabled = True
    p.save(update_fields=["notif_telegram_chat_id", "notif_telegram_enabled"])
    return ActionResult(
        text=f"✓ Telegram подключён (chat_id={chat_id}).",
        contextual_actions=[
            {"action": "notif_prefs", "label": "← Все настройки"},
        ],
    )

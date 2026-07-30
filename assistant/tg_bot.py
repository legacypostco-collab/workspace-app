"""Telegram-бот: обработка команд от пользователей.

Два режима работы:

  • PROD: webhook /api/assistant/tg/webhook/ с секретным заголовком
    Установка: см. README.deploy (нужен публичный HTTPS URL).

  • DEV: management command `python manage.py tg_poll` — long-polling,
    дёргает getUpdates каждую секунду. Не нужен публичный URL.

Команды:
  /start             — привязать chat_id к аккаунту (по first-time use)
  /status            — последние 5 заказов + их этапы
  /claims            — открытые рекламации
  /help              — список команд + ссылка на /help/

Security:
  • Команды действуют от имени user'а, у которого notif_telegram_chat_id
    совпадает с from.id. Если не привязан — отвечаем «привяжитесь сначала».
  • Никаких mutating-команд (нельзя оплатить/отменить заказ из TG).
    Чтение only — это снижает риски от похищенного TG-аккаунта.
"""
from __future__ import annotations

import logging
from typing import Optional

from django.conf import settings
from django.contrib.auth import get_user_model

logger = logging.getLogger(__name__)


def _find_user_by_chat_id(chat_id: int | str) -> Optional[object]:
    """Кто из юзеров привязал этот chat_id?"""
    U = get_user_model()
    matches = list(U.objects.filter(
        profile__notif_telegram_chat_id=str(chat_id),
        is_active=True,
    ).select_related("profile")[:2])
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        logger.error("duplicate telegram chat binding detected")
    return None


def _send(chat_id: int | str, text: str) -> bool:
    """Шорткат для send_telegram c plain-text форматом."""
    from .notif_settings import send_telegram
    return send_telegram(str(chat_id), text)


# ── Обработчики команд ─────────────────────────────────────────

def _site_link(path: str = "/chat/") -> str:
    site = str(getattr(settings, "SITE_URL", "") or "").rstrip("/")
    return f"{site}{path}" if site else path


def cmd_start(
    chat_id: int | str,
    _user,
    argument: str = "",
    chat_type: str = "private",
) -> None:
    """/start — подтверждает одноразовую привязку или показывает гайд."""
    if argument:
        if chat_type != "private":
            _send(
                chat_id,
                "Подключение аккаунта доступно только в личном диалоге с ботом.",
            )
            return
        from .tg_linking import consume_link_token

        linked_user, result = consume_link_token(argument, chat_id)
        if linked_user:
            _send(
                chat_id,
                (
                    f"Telegram подключён к аккаунту {linked_user.username}.\n\n"
                    "Теперь сюда будут приходить выбранные уведомления. "
                    "Доступные команды: /status, /claims, /help."
                ),
            )
            return
        message = {
            "expired": "Ссылка истекла или уже использована. Создайте новую в настройках уведомлений.",
            "already_linked": "Этот Telegram уже подключён к другому аккаунту.",
            "busy": "Привязка уже обрабатывается. Повторите через несколько секунд.",
            "profile_missing": "Профиль аккаунта не найден. Обратитесь в поддержку.",
        }.get(result, "Ссылка подключения недействительна.")
        _send(chat_id, message)
        return

    u = _find_user_by_chat_id(chat_id)
    if u:
        _send(chat_id, (
            f"👋 Привет, {u.username}!\n\n"
            f"Telegram уже привязан к твоему аккаунту в Consolidator Parts.\n\n"
            f"Доступные команды:\n"
            f"  /status   — последние 5 заказов\n"
            f"  /claims   — открытые рекламации\n"
            f"  /help     — все команды\n\n"
            f"Я буду присылать сюда важные события: статус заказа,\n"
            f"оплаты, рекламации, эскалации (для операторов)."
        ))
        return
    _send(chat_id, (
        "👋 Привет! Это бот Consolidator Parts.\n\n"
        f"Чтобы подключить аккаунт, открой {_site_link()} и выбери "
        "«Подключить Telegram» в настройках уведомлений. "
        "Сайт выдаст одноразовую ссылку для подтверждения."
    ))


def cmd_status(
    chat_id: int | str,
    user,
    _argument: str = "",
    _chat_type: str = "private",
) -> None:
    """/status — 5 последних заказов юзера."""
    if not user:
        _send(chat_id, "⚠️ Сначала привяжи TG к аккаунту: /start")
        return
    from marketplace.models import Order
    qs = Order.objects.filter(buyer=user).order_by("-created_at")[:5]
    if not qs:
        _send(chat_id, "📦 У тебя пока нет заказов.")
        return
    lines = ["📦 Последние 5 заказов:"]
    for o in qs:
        status_emoji = {
            "pending": "⏳", "reserve_paid": "💰", "confirmed": "✅",
            "in_production": "🏭", "ready_to_ship": "📦",
            "transit_abroad": "🛫", "customs": "🛃",
            "transit_rf": "🚛", "issuing": "📬",
            "delivered": "✅", "completed": "✅",
            "cancelled": "❌",
        }.get(o.status, "•")
        lines.append(
            f"{status_emoji} ORD-{o.id} · {o.get_status_display()} · ${o.total_amount:,.0f}"
        )
    lines.append(f"\n🔗 Полный список: {_site_link()}")
    _send(chat_id, "\n".join(lines))


def cmd_claims(
    chat_id: int | str,
    user,
    _argument: str = "",
    _chat_type: str = "private",
) -> None:
    """/claims — открытые рекламации."""
    if not user:
        _send(chat_id, "⚠️ Сначала привяжи TG к аккаунту: /start")
        return
    from marketplace.models import OrderClaim
    qs = (OrderClaim.objects
          .filter(order__buyer=user,
                  status__in=("open", "in_review", "approved",
                              "corrective_actions", "financial_settlement"))
          .select_related("order").order_by("-created_at")[:5])
    if not qs:
        _send(chat_id, "🧾 Открытых рекламаций нет.")
        return
    lines = ["🧾 Открытые рекламации:"]
    for c in qs:
        days = (c.created_at and (c.created_at - c.created_at).days) or 0
        from django.utils import timezone
        age_d = (timezone.now() - c.created_at).days
        lines.append(
            f"• #{c.id} · {c.get_kind_display()} · ORD-{c.order.id} · "
            f"{c.get_status_display()} · {age_d}д"
        )
    lines.append(f"\n🔗 Подробнее: {_site_link()}")
    _send(chat_id, "\n".join(lines))


def cmd_help(
    chat_id: int | str,
    _user,
    _argument: str = "",
    _chat_type: str = "private",
) -> None:
    _send(chat_id, (
        "📚 Команды бота:\n"
        "  /start    — привязать TG к аккаунту\n"
        "  /status   — последние 5 заказов\n"
        "  /claims   — открытые рекламации\n"
        "  /help     — это сообщение\n\n"
        f"📖 База знаний: {_site_link('/help/')}\n"
        f"💬 Чат с оператором: {_site_link()}"
    ))


# ── Диспетчер ──────────────────────────────────────────────────

COMMANDS = {
    "/start":   cmd_start,
    "/status":  cmd_status,
    "/claims":  cmd_claims,
    "/help":    cmd_help,
}


def handle_update(update: dict) -> None:
    """Принимает один update от Telegram (через webhook или polling).

    Извлекает текст команды, находит юзера, диспатчит.
    """
    msg = update.get("message") or update.get("edited_message") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    text = (msg.get("text") or "").strip()
    if not chat_id or not text:
        return

    # Поддерживаем команды с @bot_username (мобильный TG так делает в группах)
    parts = text.split(maxsplit=1)
    cmd = parts[0].split("@", 1)[0].lower()
    argument = parts[1].strip() if len(parts) > 1 else ""
    handler = COMMANDS.get(cmd)
    user = _find_user_by_chat_id(chat_id)

    if handler:
        try:
            handler(chat_id, user, argument, str(chat.get("type") or ""))
        except Exception:
            logger.exception("tg_bot: handler %s failed", cmd)
            _send(chat_id, "⚠️ Ошибка обработки команды. Оператор уже знает.")
    else:
        _send(chat_id, (
            f"❓ Команда «{cmd}» не распознана.\n"
            f"Используй /help — список команд."
        ))

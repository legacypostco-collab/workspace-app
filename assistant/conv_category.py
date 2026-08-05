"""Категоризация actions → category для group-by-category conv reuse.

Вместо плодить отдельный Conversation на каждый клик пилюли (Верификация,
Команда, Интеграции, …), reuse один долгий thread на категорию:

  admin     — всё административное: KYB, команда, интеграции, настройки
  purchase  — покупка/трекинг/оплата заказов: RFQ, quick_order, pay_*
  support   — рекламации, споры, обращения к оператору
  general   — поиск/общение/всё что не подпадает под выше (default)

При обращении в ActionView без conv_id:
  1. Определяем category по action_name
  2. Ищем существующий Conversation(user, category) — берём самый свежий
  3. Если нет — создаём новый с этой category
  4. Обновляем conv.title из текущего label/step

Заголовок ведёт сама ситуация — пользователь видит «Верификация · Шаг 2/5»,
позже тот же чат будет «Команда · 4 человека», и т.д.
"""
from __future__ import annotations
import re

from django.utils.translation import gettext as _

# Mapping: action_name → category
_CATEGORY_MAP: dict[str, str] = {
    # admin: KYB, profile, team, integrations, settings, auth, notifications
    "start_onboarding": "admin",
    "kyb_status": "admin",
    "submit_company_info": "admin",
    "submit_legal_address": "admin",
    "submit_bank": "admin",
    "submit_director": "admin",
    "submit_for_review": "admin",
    "seller_team": "admin",
    "invite_team_member": "admin",
    "accept_team_invite": "admin",
    "team_member": "admin",
    "team_disable": "admin",
    "team_enable": "admin",
    "team_set_role": "admin",
    "seller_integrations": "admin",
    "sync_1c": "admin",
    "seller_qr": "admin",
    "generate_qr": "admin",
    "seller_drawings": "admin",
    "upload_drawing": "admin",
    "referral_program": "admin",
    "notif_prefs": "admin",
    "notif_set_email": "admin",
    "notif_set_kinds": "admin",
    "notif_link_telegram": "admin",
    "setup_2fa": "admin",
    "verify_2fa": "admin",
    "disable_2fa": "admin",
    "create_api_token": "admin",
    "list_api_tokens": "admin",
    "revoke_api_token": "admin",
    # purchase: RFQ → quote → order → tracking → payment
    "search_parts": "purchase",
    "create_rfq": "purchase",
    "get_rfq_status": "purchase",
    "rfq_detail": "purchase",
    "view_rfq_quotes": "purchase",
    "view_quote": "purchase",
    "accept_quote": "purchase",
    "counter_offer": "purchase",
    "decline_quote": "purchase",
    "send_rfq_to_suppliers": "purchase",
    "submit_quote": "purchase",
    "respond_to_counter": "purchase",
    "mark_quote_final": "purchase",
    "quick_order": "purchase",
    "pay_reserve": "purchase",
    "pay_final": "purchase",
    "track_order": "purchase",
    "track_shipment": "purchase",
    "advance_order": "purchase",
    "ship_order": "purchase",
    "confirm_delivery": "purchase",
    "get_orders": "purchase",
    "get_order_detail": "purchase",
    "compare_products": "purchase",
    "compare_suppliers": "purchase",
    "top_suppliers": "purchase",
    "upload_parts_list": "purchase",
    "analyze_spec": "purchase",
    "price_quote": "purchase",
    "get_balance": "purchase",
    "settlement_prepare": "purchase",
    "settlement_my_documents": "purchase",
    "settlement_report_paid": "purchase",
    "settlement_seller_documents": "purchase",
    "settlement_finance_queue": "admin",
    "settlement_confirm_payment": "admin",
    "settlement_issue_invoice": "admin",
    "settlement_reverse_payment": "admin",
    "settlement_report": "admin",
    "withdraw_wallet": "purchase",
    "submit_withdrawal": "purchase",
    "list_withdrawals": "purchase",
    "cancel_withdrawal": "purchase",
    "transfer_wallet": "purchase",
    "submit_wallet_transfer": "purchase",
    "confirm_wallet_transfer": "purchase",
    "cancel_wallet_transfer": "purchase",
    "list_wallet_transfers": "purchase",
    "op_withdrawal_queue": "admin",
    "op_approve_withdrawal": "admin",
    "op_complete_withdrawal": "admin",
    "op_reject_withdrawal": "admin",
    # support: claims, disputes
    "get_claims": "support",
    "create_claim": "support",
    "op_resolve_dispute": "support",
    # general (default): пусто — всё что не выше falls back here
}


def category_for_action(action_name: str) -> str:
    return _CATEGORY_MAP.get(action_name or "", "general")


# Заголовки по категориям — основа, потом дополняется через action_label
_CATEGORY_TITLES: dict[str, str] = {
    "admin":    _("Управление"),
    "purchase": _("Покупки"),
    "support":  _("Поддержка"),
    "general":  "",  # default — берётся из контента
}


# Bug-E fix: маппинг технических action_name → человекочитаемые заголовки.
# Без этой таблицы в sidebar истории показывались строки вида
# «seller_demand_payment», «get_claims», что одновременно UX-проблема
# и утечка названий API actions.
_ACTION_TITLES: dict[str, str] = {
    # Seller-side
    "seller_dashboard":        _("Сводка продавца"),
    "seller_inbox":            _("Входящие заявки"),
    "seller_orders":           _("Мои заказы"),
    "seller_catalog":          _("Мои товары"),
    "seller_drawings":         _("Чертежи"),
    "seller_team":             _("Команда"),
    "seller_integrations":     _("Интеграции"),
    "seller_analytics":        _("Аналитика продавца"),
    "seller_quotes":           _("Мои котировки"),
    "seller_demand_payment":   _("Запрос оплаты"),
    "seller_cancel_pending":   _("Отмена ожидающего заказа"),
    "send_quote":              _("Отправить котировку"),
    # Buyer-side
    "get_orders":              _("Мои заказы"),
    "get_my_deals":            _("Мои сделки"),
    "get_order_detail":        _("Детали заказа"),
    "track_order":             _("Отслеживание заказа"),
    "track_shipment":          _("Отслеживание доставки"),
    "get_rfq_status":          _("Заявки"),
    "rfq_detail":              _("Детали заявки"),
    "create_rfq":              _("Создать заявку"),
    "search_parts":            _("Поиск запчастей"),
    "compare_products":        _("Сравнение товаров"),
    "compare_suppliers":       _("Сравнение поставщиков"),
    "top_suppliers":           _("Топ-поставщиков"),
    "get_budget":              _("Бюджет"),
    "get_analytics":           _("Аналитика"),
    "get_buyer_discount":      _("Скидка покупателя"),
    "get_savings":             _("Экономия"),
    "quick_order":             _("Быстрая покупка"),
    "pay_reserve":             _("Оплата резерва"),
    "pay_final":                _("Финальная оплата"),
    "confirm_delivery":        _("Подтверждение доставки"),
    "get_balance":             _("Внутренний счёт"),
    "withdraw_wallet":         _("Вывод средств"),
    "submit_withdrawal":       _("Заявка на вывод"),
    "list_withdrawals":        _("Заявки на вывод"),
    "transfer_wallet":         _("Внутренний перевод"),
    "list_wallet_transfers":   _("История переводов"),
    # Claims / Support
    "get_claims":              _("Рекламации"),
    "create_claim":            _("Создать рекламацию"),
    "open_claim":              _("Открыть рекламацию"),
    "claim_detail":            _("Детали рекламации"),
    "support_home":            _("Поддержка"),
    # Auth / Onboarding
    "start_login":             _("Вход"),
    "start_registration":      _("Регистрация"),
    "start_onboarding":        _("Верификация · Начало"),
    "submit_company_info":     _("Верификация · Компания"),
    "submit_legal_address":    _("Верификация · Адрес"),
    "submit_bank":             _("Верификация · Реквизиты"),
    "submit_director":         _("Верификация · Директор"),
    "submit_for_review":       _("Верификация · Отправка"),
    "kyb_status":              _("Статус верификации"),
    # Settings
    "account_settings":        _("Настройки"),
    "notif_settings":          _("Уведомления"),
    "notif_prefs":             _("Настройки уведомлений"),
    "setup_2fa":               _("Двухфакторная аутентификация"),
    # Operator
    "op_dashboard":            _("Дашборд оператора"),
    "settlement_prepare":      _("Договор и счёт"),
    "settlement_my_documents": _("Мои расчёты"),
    "settlement_report_paid":  _("Сообщить об оплате"),
    "settlement_seller_documents": _("Расчёты с платформой"),
    "settlement_finance_queue": _("Очередь платежей"),
    "settlement_confirm_payment": _("Подтверждение платежа"),
    "settlement_issue_invoice": _("Выставление счёта"),
    "settlement_reverse_payment": _("Отмена проводки"),
    "settlement_report":       _("Отчёт по расчётам"),
    "op_topup_queue":          _("Очередь пополнений"),
    "op_withdrawal_queue":     _("Очередь выплат"),
    "op_rfq_queue":            _("Очередь RFQ"),
    "op_analytics_hub":        _("Аналитика оператора"),
    "get_sla_report":          _("Отчёт по SLA"),
    # Generic
    "kb_search":               _("База знаний"),
    "audit_log":               _("Журнал действий"),
    "recent_activity":         _("История действий"),
    "notifications":           _("Уведомления"),
    "generate_qr":             _("QR-код"),
    "price_quote":             _("Калькулятор цены"),
    "admin_dashboard":         _("Сводка платформы"),
    "admin_users":             _("Пользователи"),
    "admin_moderation":        _("Модерация"),
    "admin_events":            _("Лента событий"),
}


def _humanize_action(action_name: str) -> str:
    """Не отдавать пользователю внутреннее имя действия."""
    if not action_name:
        return ""
    return _("Раздел")


_LEADING_DECORATION_RE = re.compile(
    r"^[\s\u200d\u2600-\u27bf\ufe0f\U0001f300-\U0001faff]+"
)


def humanize_action_title(action_name: str) -> str:
    """Человекочитаемое название действия без внутренних идентификаторов."""
    return str(_ACTION_TITLES.get(action_name) or _humanize_action(action_name))


def clean_action_label(value: str | None) -> str:
    """Убрать декоративный знак и отсеять переданный вместо подписи action."""
    label = _LEADING_DECORATION_RE.sub("", (value or "").strip()).strip()
    if re.fullmatch(r"[a-z][a-z0-9_]*", label, flags=re.IGNORECASE):
        return ""
    return label


def title_for_action(action_name: str, action_label: str | None = None) -> str:
    """Заголовок для conv'а на основании текущего action.

    Например: «Верификация · Реквизиты» / «Заказы · ORD-151» / «Команда».
    """
    cat = category_for_action(action_name)
    base = _CATEGORY_TITLES.get(cat, "")
    label = clean_action_label(action_label)
    if not label:
        nice = humanize_action_title(action_name)
        return base or nice
    if base:
        return f"{base} · {label}"
    return label


def find_or_create_conv(user, *, action_name: str, role: str, action_label: str | None = None):
    """Найти долгий conv пользователя по категории action'а или создать.

    Инвариант: per (user, role, category) держим РОВНО ОДИН активный conv. Если
    у пользователя уже накопились дубликаты (например, до того как
    группировка по категории была включена) — оставляем самый свежий, а
    остальные той же категории архивируем (is_active=False), чтобы они
    не маячили в сайдбаре.

    Возвращает Conversation. Заголовок обновляется на текущий label если он
    более информативен.
    """
    from .models import Conversation
    cat = category_for_action(action_name)
    qs = (
        Conversation.objects.filter(
            user=user,
            role=role,
            category=cat,
            is_active=True,
        )
        .order_by("-updated_at")
    )
    matches = list(qs[:20])
    if matches:
        existing = matches[0]
        # Архивируем дубликаты той же категории (если есть)
        dup_ids = [c.id for c in matches[1:]]
        if dup_ids:
            Conversation.objects.filter(id__in=dup_ids).update(is_active=False)
        # Обновляем title под текущий шаг
        new_title = title_for_action(action_name, action_label)
        if new_title and new_title != existing.title:
            existing.title = new_title[:200]
            existing.save(update_fields=["title", "updated_at"])
        return existing
    return Conversation.objects.create(
        user=user, role=role, category=cat,
        title=title_for_action(action_name, action_label)[:200],
    )

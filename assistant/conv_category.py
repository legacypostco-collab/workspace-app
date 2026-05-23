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
    "seller_integrations": "admin",
    "sync_1c": "admin",
    "seller_qr": "admin",
    "generate_qr": "admin",
    "seller_drawings": "admin",
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
    "admin":    "Управление",
    "purchase": "Покупки",
    "support":  "Поддержка",
    "general":  "",  # default — берётся из контента
}


# Bug-E fix: маппинг технических action_name → человекочитаемые заголовки.
# Без этой таблицы в sidebar истории показывались строки вида
# «seller_demand_payment», «get_claims», что одновременно UX-проблема
# и утечка названий API actions.
_ACTION_TITLES: dict[str, str] = {
    # Seller-side
    "seller_dashboard":        "Сводка продавца",
    "seller_orders":           "Мои заказы",
    "seller_catalog":          "Мои товары",
    "seller_drawings":         "Чертежи",
    "seller_team":             "Команда",
    "seller_integrations":     "Интеграции",
    "seller_analytics":        "Аналитика продавца",
    "seller_quotes":           "Мои котировки",
    "seller_demand_payment":   "Запрос оплаты",
    "seller_cancel_pending":   "Отмена ожидающего заказа",
    "send_quote":              "Отправить котировку",
    # Buyer-side
    "get_orders":              "Мои заказы",
    "get_order_detail":        "Детали заказа",
    "track_order":             "Отслеживание заказа",
    "track_shipment":          "Отслеживание доставки",
    "get_rfq_status":          "Статусы RFQ",
    "rfq_detail":              "Детали RFQ",
    "create_rfq":              "Создать RFQ",
    "search_parts":            "Поиск запчастей",
    "compare_products":        "Сравнение товаров",
    "compare_suppliers":       "Сравнение поставщиков",
    "top_suppliers":           "Топ-поставщиков",
    "get_budget":              "Бюджет",
    "get_analytics":           "Аналитика",
    "get_buyer_discount":      "Скидка покупателя",
    "get_savings":             "Экономия",
    "quick_order":             "Быстрая покупка",
    "pay_reserve":             "Оплата резерва",
    "pay_final":                "Финальная оплата",
    "confirm_delivery":        "Подтверждение доставки",
    # Claims / Support
    "get_claims":              "Рекламации",
    "create_claim":            "Создать рекламацию",
    "open_claim":              "Открыть рекламацию",
    "claim_detail":            "Детали рекламации",
    "support_home":            "Поддержка",
    # Auth / Onboarding
    "start_login":             "Вход",
    "start_registration":      "Регистрация",
    "start_onboarding":        "Верификация · Начало",
    "submit_company_info":     "Верификация · Компания",
    "submit_legal_address":    "Верификация · Адрес",
    "submit_bank":             "Верификация · Реквизиты",
    "submit_director":         "Верификация · Директор",
    "submit_for_review":       "Верификация · Отправка",
    "kyb_status":              "Статус верификации",
    # Settings
    "account_settings":        "Настройки",
    "notif_settings":          "Уведомления",
    "notif_prefs":             "Настройки уведомлений",
    "setup_2fa":               "Двухфакторная аутентификация",
    # Operator
    "op_dashboard":            "Дашборд оператора",
    "op_topup_queue":          "Очередь пополнений",
    "op_rfq_queue":            "Очередь RFQ",
    "op_analytics_hub":        "Аналитика оператора",
    "get_sla_report":          "Отчёт по SLA",
    # Generic
    "kb_search":               "База знаний",
    "audit_log":               "Журнал действий",
    "recent_activity":         "История действий",
    "notifications":           "Уведомления",
    "generate_qr":             "QR-код",
    "price_quote":             "Калькулятор цены",
}


def _humanize_action(action_name: str) -> str:
    """Fallback humanizer: get_orders → 'Get orders'. Лучше чем raw имя."""
    if not action_name:
        return ""
    return action_name.replace("_", " ").strip().capitalize()


def title_for_action(action_name: str, action_label: str | None = None) -> str:
    """Заголовок для conv'а на основании текущего action.

    Например: «Верификация · Реквизиты» / «Заказы · ORD-151» / «Команда».
    """
    cat = category_for_action(action_name)
    base = _CATEGORY_TITLES.get(cat, "")
    label = (action_label or "").strip()
    if not label:
        # Bug-E fix: не показывать сырое имя action. Сначала пробуем
        # справочник человекочитаемых заголовков, потом fallback-гуманизация.
        nice = _ACTION_TITLES.get(action_name) or _humanize_action(action_name)
        return base or nice
    if base:
        return f"{base} · {label}"
    return label


def find_or_create_conv(user, *, action_name: str, role: str, action_label: str | None = None):
    """Найти долгий conv пользователя по категории action'а или создать.

    Инвариант: per (user, category) держим РОВНО ОДИН активный conv. Если
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
        Conversation.objects.filter(user=user, category=cat, is_active=True)
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

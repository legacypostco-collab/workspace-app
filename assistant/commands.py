"""Canonical command menu for the chat-first interface."""

from django.utils.translation import gettext as _


_ROLE_COMMANDS = {
    "guest": [
        ("🔍", "Найти запчасть", "search_parts", {}),
        ("🏭", "Сравнить поставщиков", "compare_suppliers", {}),
        ("📚", "База знаний", "kb_search", {}),
        ("📋", "Создать заявку", "create_rfq", {}),
    ],
    "buyer": [
        ("📦", "Мои сделки", "get_my_deals", {}),
        ("📋", "Заявки", "get_rfq_status", {}),
        ("👤", "Мой менеджер", "my_kam", {}),
        ("📐", "Чертежи", "seller_drawings", {}),
        ("🧾", "Расчёты", "settlement_my_documents", {}),
        ("🎯", "Автоскидка", "get_buyer_discount", {}),
        ("🎧", "Поддержка", "support_home", {}),
    ],
    "seller": [
        ("📋", "Мои сделки", "get_my_deals", {}),
        ("📥", "Входящие заявки", "seller_inbox", {}),
        ("📤", "Загрузить прайс", "upload_pricelist", {}),
        ("📦", "Мои товары", "seller_warehouses", {}),
        ("📐", "Чертежи", "seller_drawings", {}),
        ("🧾", "Расчёты", "settlement_seller_documents", {}),
        ("🛡", "Верификация", "start_onboarding", {}),
        ("📊", "Аналитика", "seller_analytics_hub", {}),
        ("🎧", "Поддержка", "support_home", {}),
    ],
    "operator": [
        ("🎛", "Сводка", "op_dashboard", {}),
        ("📋", "Очередь заказов", "op_queue", {}),
        ("⏱", "Нарушения сроков", "op_sla_breach", {}),
        ("🛂", "Таможня", "op_customs_dashboard", {}),
        ("🚚", "Логистика", "op_logistics_stats", {}),
        ("🏭", "Мои поставщики", "op_my_suppliers", {}),
        ("🛡", "Проверка поставщиков", "op_kyb_queue", {}),
        ("🧾", "Рекламации", "get_claims", {}),
        ("📂", "Мои диалоги", "op_my_user_chats", {}),
        ("📐", "Чертежи", "op_drawings_by_part", {}),
        ("📊", "Аналитика", "op_analytics_hub", {}),
    ],
    "operator_manager": [
        ("👥", "Заказчики", "seller_customers", {}),
        ("📋", "Мои сделки", "kam_deals", {}),
        ("💰", "Начисления", "my_accruals", {}),
        ("📨", "Пригласить", "invite_customer", {}),
        ("📊", "Аналитика", "op_analytics_hub", {}),
        ("📂", "Мои диалоги", "op_my_user_chats", {}),
        ("🎧", "Поддержка", "support_home", {}),
    ],
    "operator_logist": [
        ("🚚", "Логистика", "op_logistics_stats", {}),
        ("🎛", "Сводка", "op_dashboard", {}),
        ("📋", "Открытая очередь", "op_queue", {"filter": "open"}),
        ("⏱", "Нарушения сроков", "op_sla_breach", {}),
    ],
    "operator_customs": [
        ("🛂", "Сводка таможни", "op_customs_dashboard", {}),
        ("🔎", "ТН ВЭД", "op_hs_lookup", {}),
        ("🚫", "Санкции", "op_sanctions_check", {}),
        ("📋", "На таможне", "op_queue", {"filter": "open"}),
    ],
    "operator_payment": [
        ("🧾", "Очередь платежей", "settlement_finance_queue", {}),
        ("📊", "Отчёт по расчётам", "settlement_report", {}),
        ("⏳", "Ожидают первый платёж", "op_queue", {"filter": "awaiting_reserve"}),
        ("↩", "Возвраты", "op_queue", {"filter": "refund"}),
    ],
    "admin": [
        ("🛡", "Сводка платформы", "admin_dashboard", {}),
        ("🧾", "Счета и платежи", "settlement_finance_queue", {}),
        ("🌐", "Слепок рынка", "admin_market_twin", {}),
        ("🛂", "Таможенные данные", "admin_customs", {}),
        ("📈", "Оборот", "admin_gmv", {}),
        ("👥", "Пользователи", "admin_users", {}),
        ("⚠", "Модерация", "admin_moderation_queue", {}),
        ("📦", "Проверка каталога", "admin_catalog_review", {}),
        ("⚙", "Настройки платформы", "admin_platform_settings", {}),
    ],
}

_ROLE_SUGGESTIONS = {
    "buyer": [
        "Покажи мои активные RFQ",
        "Какие гусеничные цепи есть для Komatsu?",
        "Статус моих заказов за последний месяц",
        "Сравни поставщиков по SLA",
    ],
    "seller": [
        "Новые RFQ за сегодня",
        "Какие запчасти ищут чаще всего?",
        "Мои просроченные заказы",
        "KPI за этот месяц",
    ],
    "operator_logist": [
        "Какие отгрузки сейчас в пути?",
        "Есть ли нарушения SLA?",
        "Контейнеры на таможне",
    ],
    "operator_customs": [
        "Грузы ожидающие растаможки",
        "Документы для контейнера",
        "Просроченные декларации",
    ],
    "operator_payment": [
        "Неоплаченные инвойсы",
        "Просроченные платежи",
        "Счета и договоры по заказам",
    ],
    "operator_manager": [
        "Конверсия RFQ → заказ за месяц",
        "Топ покупатели по выручке",
        "Неактивные клиенты",
    ],
    "operator": [
        "Открытые заказы с нарушением сроков",
        "Поставщики на проверке",
        "Новые обращения пользователей",
    ],
    "admin": [
        "Метрики платформы за неделю",
        "Поставщики на верификации",
        "Просроченные SLA",
    ],
}


def _base_role(role: str) -> str:
    if role in _ROLE_COMMANDS:
        return role
    if role and role.startswith("operator_"):
        return role if role in _ROLE_COMMANDS else "operator"
    return "buyer"


def commands_for_role(role: str, *, anonymous: bool = False) -> list[dict]:
    key = "guest" if anonymous else _base_role(role)
    return [
        {
            "icon": icon,
            "label": _(label),
            "action": action,
            "params": params.copy(),
        }
        for icon, label, action, params in _ROLE_COMMANDS[key]
    ]


def suggestions_for_role(role: str, *, anonymous: bool = False) -> list[str]:
    if anonymous:
        return []
    key = _base_role(role)
    source = _ROLE_SUGGESTIONS.get(key, _ROLE_SUGGESTIONS["buyer"])
    return [_(text) for text in source]


def commands_for_all_roles() -> dict[str, list[dict]]:
    return {
        role: commands_for_role(role, anonymous=(role == "guest"))
        for role in _ROLE_COMMANDS
    }

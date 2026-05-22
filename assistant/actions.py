"""Chat-First Action Executor.

When AI determines the user wants to perform an action (search, create RFQ,
track shipment, etc.), it calls one of these handlers. Each handler returns
an ActionResult with text + cards + new actions + suggestions.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import timedelta
from collections.abc import Callable
from dataclasses import dataclass, field

from django.db.models import Q
from django.utils.translation import gettext as _

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# Status label translation helper.
# Many chat cards historically use hardcoded Russian status_label strings.
# To avoid touching dozens of call sites we translate them at emit time
# via gettext lookup. If a translation isn't found, the original Russian
# is returned (graceful fallback).
# ──────────────────────────────────────────────────────────────────────
_STATUS_LABEL_KEYS = {
    "Ожидание оплаты",
    "Резерв оплачен",
    "Готов к отгрузке",
    "В производстве",
    "Подтверждён",
    "На таможне",
    "Доставлен",
    "Завершён",
    "Отменён",
    "Транзит",
}


def tr_label(text: str) -> str:
    """Translate a status_label-ish Russian string via gettext.
    Returns original text if no translation registered."""
    if not text:
        return text
    try:
        translated = _(text)
        return translated
    except Exception:
        return text


@dataclass
class ActionResult:
    """Standard return type for any action.

    По ТЗ кнопки делятся на два уровня:
    • actions — обязательные, диктуются state machine. Без них логика не
      двигается (например, «Принять КП» / «Отклонить» / «Запросить переторжку»
      на карточке RFQ). Primary-стиль, без AI-маркера.
    • contextual_actions — контекстные, по правилам кода для текущей ситуации
      (просрочка → «История SLA», новый поставщик → «Профиль», цена выросла →
      «Сравнить с прошлым»). Secondary-стиль, маркер 💡.

    suggestions — текстовые подсказки-чипы для следующего шага (просто
    подставляют текст в input).
    """
    text: str = ""
    cards: list = field(default_factory=list)
    actions: list = field(default_factory=list)              # уровень 1
    contextual_actions: list = field(default_factory=list)   # уровень 2
    suggestions: list = field(default_factory=list)

    def to_dict(self):
        return {
            "text": self.text,
            "cards": self.cards,
            "actions": self.actions,
            "contextual_actions": self.contextual_actions,
            "suggestions": self.suggestions,
        }


# ── Permission matrix ──────────────────────────────────────
# Buyer-actions: покупка, оплата, приёмка. Доступны и buyer, и seller
# (продавец тоже может докупать товар или докомплектовывать свой заказ
# как обычный покупатель).
_BUYER_ACTIONS = [
    "search_parts", "create_rfq", "get_rfq_status",
    "get_orders", "get_order_detail", "track_order", "track_shipment",
    "get_budget", "get_analytics", "get_supply_report", "get_sla_report",
    "get_buyer_discount", "get_savings", "recent_activity",
    "seller_analytics_hub", "seller_executive_report",
    "compare_products", "compare_suppliers", "top_suppliers",
    "buyer_best_offers", "buyer_offer_compare", "calc_part_logistics",
    "upload_parts_list", "analyze_spec",
    "get_claims", "create_claim", "open_claim", "claim_detail",
    "open_url", "generate_proposal",
    # покупка и депозит
    "quick_order", "pay_reserve", "pay_final",
    "shipping_choose", "shipping_apply",
    "get_balance", "topup_wallet",
    # Production deposit top-up flow
    "start_topup", "submit_topup", "confirm_topup_paid", "cancel_topup", "list_topups",
    # приёмка собственного заказа после доставки
    "confirm_delivery",
    # база знаний, конфигуратор цены, аудит, QR, уведомления
    "kb_search", "price_quote", "audit_log", "generate_qr", "notifications",
    # Support Hub — общий для всех ролей
    "support_home", "kb_faq", "my_verifications", "my_bonuses",
    "contact_operator", "open_complaint",
    # Onboarding / KYB wizard (всем доступно)
    "start_onboarding", "submit_company_info", "submit_legal_address",
    "submit_bank", "submit_director", "submit_for_review", "kyb_status",
    "update_kyb_contacts",
    # Negotiation (buyer side)
    "view_rfq_quotes", "view_quote", "accept_quote", "counter_offer", "decline_quote",
    "send_rfq_to_suppliers", "auto_accept_and_pay_reserve",
    # KP workflow (buyer side): present инвойс + confirm reserve
    "present_kp_to_buyer", "confirm_kp_and_reserve",
    # Competitor offers (§5.2): buyer загружает чужой оффер для триггера переторжки
    "upload_competitor_offer",
    # PDF documents (§12.2): invoice/packing/QC — все доступны buyer'у
    "generate_invoice_pdf", "generate_packing_list_pdf",
    "generate_qc_report_pdf", "list_order_documents",
    # Notification preferences (durable channels)
    "notif_prefs", "notif_set_email", "notif_set_kinds", "notif_link_telegram",
    # Auth — 2FA + API tokens (всем доступно)
    "setup_2fa", "verify_2fa", "disable_2fa",
    "create_api_token", "list_api_tokens", "revoke_api_token",
]

# Seller-only: эксклюзивные действия продавца — отвечать на RFQ, грузить
# прайс, двигать заказ по pipeline (production → ready → shipped → ...).
# Внутри advance_order ещё проверяется, что в заказе есть товары seller'а.
_SELLER_ONLY = [
    "respond_rfq", "upload_pricelist",
    # Pricelist через AI-маппинг (история, ошибки) + Google Sheets sync
    "pricelist_show_errors", "pricelist_history", "connect_gsheet",
    # Negotiation (seller side)
    "submit_quote", "respond_to_counter", "mark_quote_final",
    # Competitor offer response (§5.2): seller дает скидку или отказывается
    "respond_to_competitor_offer",
    "get_demand_report", "get_sla_report",
    "advance_order", "complete_trigger",
    "seller_demand_payment", "seller_cancel_pending",
    "seller_pipeline", "ship_order",
    "seller_dashboard", "seller_finance", "seller_rating",
    "seller_inbox",
    "seller_catalog", "seller_warehouses", "toggle_product", "add_product", "edit_product",
    "product_detail", "import_pricelist_preview",
    "rfq_detail", "respond_rfq_form",
    "seller_drawings", "seller_team", "invite_team_member",
    "seller_integrations", "seller_reports",
    "seller_qr", "seller_logistics", "seller_negotiations",
    "price_quote", "audit_log", "recent_activity", "generate_qr", "notifications",
    "support_home", "kb_faq", "my_verifications",
    "contact_operator", "open_complaint",
    "view_support_ticket", "color_legend",
    "sync_1c",
]

_OPERATOR_CORE = [
    # Read-only browse + диспетчерские action'ы
    "search_parts", "get_orders", "get_order_detail", "get_rfq_status",
    "track_order", "track_shipment", "advance_order", "complete_trigger",
    "get_analytics", "get_supply_report", "get_demand_report", "get_sla_report", "get_budget",
    "compare_suppliers", "compare_products", "top_suppliers",
    "get_claims", "open_url", "generate_proposal",
    "audit_log", "recent_activity", "kb_search", "notifications",
    "view_support_ticket", "color_legend",
    "support_home", "kb_faq", "my_verifications",
    "contact_operator", "open_complaint",
    # Operator-only: dashboard, очередь, назначение, спор, заметка
    "op_dashboard", "op_queue", "op_rfq_queue", "op_sla_breach",
    "op_order_detail", "op_assign", "op_assign_carrier", "op_add_note", "op_resolve_dispute",
    # Customs / Compliance
    "op_hs_lookup", "op_hs_assign", "op_calc_duty",
    "op_certs_check", "op_cert_upload", "op_sanctions_check",
    "op_customs_dashboard", "op_customs_release",
    # Payments / Escrow dashboard
    "op_payments_dashboard",
    # Аналитический хаб + отдельные отчёты
    "op_analytics_hub",
    # Deposit top-up confirmation (финансы)
    "op_topup_queue", "op_confirm_topup", "op_reject_topup",
    # Operator analytics
    "op_logistics_stats", "op_payments_stats",
    # KYB moderation
    "op_kyb_queue", "op_kyb_review", "op_kyb_approve", "op_kyb_reject",
    "op_kyb_check", "op_kyb_clarify",
    # External rating refresh
    "op_refresh_external_rating",
    # Claim workflow (operator side)
    "start_claim_review", "approve_claim", "reject_claim",
    "apply_corrective", "apply_settlement", "close_claim", "claim_detail",
    # KP workflow: SEMI approve + MANUAL dispatch/compose
    "op_approve_kp", "op_dispatch_manual_rfq", "op_compose_kp",
    # Document generators (operator может создавать любые)
    "generate_invoice_pdf", "generate_packing_list_pdf",
    "generate_qc_report_pdf", "list_order_documents",
]

ROLE_ACTIONS = {
    "buyer":  _BUYER_ACTIONS,
    "seller": _BUYER_ACTIONS + _SELLER_ONLY,
    "operator_logist": _OPERATOR_CORE,
    "operator_customs": _OPERATOR_CORE,
    "operator_payment": _OPERATOR_CORE,
    "operator_manager": _OPERATOR_CORE,
    "operator": _OPERATOR_CORE,
    "admin": ["*"],  # admin sees everything (wildcard — все actions доступны)
}

# Подмножество actions, специфичных только для admin (вне operator/seller/buyer)
_ADMIN_ONLY = [
    "admin_dashboard", "admin_gmv", "admin_users", "admin_user_detail",
    "admin_ban_user", "admin_unban_user", "admin_change_role",
    "admin_moderation_queue", "admin_catalog_review", "admin_platform_settings",
    "admin_revenue_breakdown",
]


# Действия продавца, которые требуют верификации KYB
_KYB_GATED_SELLER = {
    "respond_rfq", "respond_rfq_form",
    "submit_quote", "respond_to_counter", "mark_quote_final",
    "ship_order", "advance_order",
    "add_product", "edit_product", "toggle_product",
    "upload_pricelist", "import_pricelist_preview",
}


def can_execute(action_name: str, role: str) -> bool:
    allowed = ROLE_ACTIONS.get(role, [])
    return "*" in allowed or action_name in allowed


def kyb_gate(action_name: str, role: str, user) -> str | None:
    """Возвращает строку-причину если seller-action заблокирован из-за KYB; иначе None.

    Логика:
      • Если действие не из _KYB_GATED_SELLER — не блокируем
      • Если роль не seller — не блокируем
      • Если у пользователя KYB verified — пропускаем
      • Demo-аккаунты пропускаются (для презентаций)
    """
    if action_name not in _KYB_GATED_SELLER:
        return None
    if role != "seller":
        return None
    try:
        from .onboarding import kyb_required_for_seller
        if kyb_required_for_seller(user):
            return ("Это действие доступно только верифицированным продавцам. "
                    "Пройдите KYB-верификацию: «Начать верификацию».")
    except Exception:
        logger.exception("kyb_gate check failed")
    return None


# ── Registry ───────────────────────────────────────────────
_REGISTRY: dict[str, Callable] = {}


def register(name: str):
    def decorator(func):
        _REGISTRY[name] = func
        return func
    return decorator


def execute(action_name: str, params: dict, user, role: str) -> ActionResult:
    """Run an action. Returns ActionResult."""
    if not can_execute(action_name, role):
        # Дружелюбное сообщение: подсказываем какая роль нужна и предлагаем
        # переключиться, вместо холодного «нет прав».
        SELLER_ONLY_HINTS = {
            "seller_pipeline":     "очередь продавца",
            "seller_dashboard":    "дашборд продавца",
            "seller_inbox":        "входящие RFQ",
            "seller_catalog":      "каталог продавца",
            "seller_finance":      "финансы продавца",
            "seller_rating":       "рейтинг продавца",
            "seller_negotiations": "переговоры продавца",
            "submit_quote":        "ответ на RFQ",
            "ship_order":          "отгрузка заказа",
            "advance_order":       "движение заказа по этапам",
            "upload_pricelist":    "загрузка прайс-листа",
            "respond_rfq":         "ответ на RFQ",
        }
        hint = SELLER_ONLY_HINTS.get(action_name)
        if hint and role == "buyer":
            return ActionResult(
                text=(
                    f"🔁 «{hint}» — это раздел продавца, а вы сейчас в роли «Покупатель».\n"
                    f"Переключите роль в шапке (Покупатель ↔ Продавец) или нажмите кнопку ниже."
                ),
                actions=[
                    {"action": "_switch_role", "label": "🔁 Переключиться на «Продавец»",
                     "params": {"role": "seller"}},
                    {"action": "go_home", "label": "🏠 Главная"},
                ],
            )
        return ActionResult(text=f"⚠️ Нет прав на действие '{action_name}' для роли {role}")
    # KYB gate: продавцы без верификации не могут писать-action'ы
    gate_reason = kyb_gate(action_name, role, user)
    if gate_reason:
        return ActionResult(
            text=f"🛡 {gate_reason}",
            actions=[
                {"action": "start_onboarding", "label": "🚀 Начать верификацию"},
            ],
        )
    handler = _REGISTRY.get(action_name)
    if not handler:
        return ActionResult(text=f"⚠️ Действие '{action_name}' не зарегистрировано")
    try:
        return handler(params=params or {}, user=user, role=role)
    except Exception as e:
        logger.exception(f"Action {action_name} failed")
        return ActionResult(text=f"⚠️ Ошибка выполнения: {e}")


def list_actions(role: str) -> list[str]:
    allowed = ROLE_ACTIONS.get(role, [])
    if "*" in allowed:
        return list(_REGISTRY.keys())
    return [a for a in _REGISTRY.keys() if a in allowed]


# ══════════════════════════════════════════════════════════
# Tool schemas (Claude tool-use format)
# ══════════════════════════════════════════════════════════
# These describe each action so Claude can call them as tools instead of
# being instructed to emit :::block JSON. Action handlers stay the same;
# only the entrypoint differs.

_STR = {"type": "string"}
_INT = {"type": "integer"}
_NUM = {"type": "number"}
_BOOL = {"type": "boolean"}
_LIST_STR = {"type": "array", "items": {"type": "string"}}

TOOL_SCHEMAS = {
    "search_parts": {
        "description": (
            "Поиск запчастей по каталогу. Поддерживает свободный текст и "
            "список OEM-артикулов (через query как многострочную строку или "
            "через articles[]). При >=2 артикулах возвращает spec_results "
            "карточку (KPI + таблица), иначе — карточки product."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {**_STR, "description": "Свободный текст или несколько артикулов через перевод строки/запятую"},
                "articles": {**_LIST_STR, "description": "Список OEM-артикулов для точного поиска"},
                "brand": {**_STR, "description": "Фильтр по бренду"},
                "category": {**_STR, "description": "Фильтр по категории"},
                "limit": {**_INT, "description": "Макс. кол-во результатов (default 20, max 50)"},
            },
        },
    },
    "analyze_spec": {
        "description": (
            "Многострочный разбор спецификации/BoM. Считает best mix, "
            "находит OEM/аналоги, помечает недоступные. Используй когда "
            "пользователь говорит «посчитай по парку», «обработай спеку», "
            "«сколько будет стоить», «лучший микс»."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "condition": {"type": "string", "enum": ["oem", "analogue"], "description": "Фильтр: только OEM или только аналоги"},
                "lead_max_days": {**_INT, "description": "Макс. лидтайм в днях (фильтр)"},
            },
        },
    },
    "top_suppliers": {
        "description": (
            "Возвращает ранжированный топ-N поставщиков под текущую спеку. "
            "Используй когда пользователь просит «топ-3 поставщиков», "
            "«сравни поставщиков», «лучшие предложения»."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {**_INT, "description": "Сколько поставщиков (default 3)"},
                "condition": {"type": "string", "enum": ["oem", "analogue"]},
            },
        },
    },
    "create_rfq": {
        "description": (
            "Создаёт RFQ (запрос котировок). Принимает product_ids (UUID из "
            "каталога) ИЛИ articles (OEM-номера) ИЛИ свободный query. "
            "Поставщики получат уведомление."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "product_ids": {**_LIST_STR, "description": "UUIDs товаров из каталога"},
                "articles": {**_LIST_STR, "description": "Список OEM-артикулов"},
                "query": {**_STR, "description": "Свободный текст запроса"},
                "quantity": {**_INT, "description": "Кол-во по каждой позиции (default 1)"},
            },
        },
    },
    "get_orders": {
        "description": "Список заказов пользователя.",
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {**_STR, "description": "Фильтр по статусу"},
                "limit": {**_INT},
            },
        },
    },
    "get_order_detail": {
        "description": "Детали конкретного заказа.",
        "input_schema": {"type": "object", "properties": {"order_id": _STR}, "required": ["order_id"]},
    },
    "get_rfq_status": {
        "description": "Список или статус RFQ. Без params — все RFQ пользователя.",
        "input_schema": {
            "type": "object",
            "properties": {"rfq_id": _INT, "status": _STR},
        },
    },
    "track_shipment": {
        "description": "Трекинг отгрузки по order_id.",
        "input_schema": {"type": "object", "properties": {"order_id": _STR}},
    },
    "get_buyer_discount": {
        "description": "ТЗ §4.1: текущий уровень auto-discount buyer'а по годовому обороту (0/1/2/3).",
        "input_schema": {"type": "object", "properties": {}},
    },
    "get_budget": {
        "description": "Бюджет/расходы пользователя за период.",
        "input_schema": {
            "type": "object",
            "properties": {"period": {"type": "string", "enum": ["week", "month", "quarter", "year"]}},
        },
    },
    "get_analytics": {
        "description": "Аналитика для роли (дашборд-метрики).",
        "input_schema": {"type": "object", "properties": {}},
    },
    "compare_suppliers": {
        "description": "Сравнение поставщиков по метрикам.",
        "input_schema": {
            "type": "object",
            "properties": {"supplier_ids": _LIST_STR},
        },
    },
    "compare_products": {
        "description": "Сравнение товаров side-by-side.",
        "input_schema": {
            "type": "object",
            "properties": {"product_ids": _LIST_STR},
            "required": ["product_ids"],
        },
    },
    "get_claims": {
        "description": "Список рекламаций пользователя.",
        "input_schema": {"type": "object", "properties": {}},
    },
    "get_sla_report": {
        "description": "SLA-отчёт по нарушениям.",
        "input_schema": {"type": "object", "properties": {}},
    },
    "get_demand_report": {
        "description": "Отчёт по спросу для поставщика.",
        "input_schema": {"type": "object", "properties": {}},
    },
    "generate_proposal": {
        "description": (
            "Формирует коммерческое предложение (КП) по существующему RFQ. "
            "Используй когда пользователь просит «сформируй КП», «сделай "
            "коммерческое предложение», «выгрузи КП», «нужно КП по RFQ X». "
            "Возвращает ссылку на страницу КП с возможностью скачать PDF."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "rfq_id": {**_INT, "description": "ID RFQ. Если не указан — последний созданный RFQ пользователя."},
            },
        },
    },
    # ── Operator-cabinet actions ────────────────────────────
    "op_dashboard": {
        "description": "Операторская сводка: KPI заказов в работе, SLA, оборот, приоритетная очередь.",
        "input_schema": {"type": "object", "properties": {}},
    },
    "op_queue": {
        "description": "Очередь заказов, требующих внимания оператора. filter: all|breached|at_risk|refund|awaiting_reserve|open.",
        "input_schema": {
            "type": "object",
            "properties": {"filter": {**_STR, "description": "all|breached|at_risk|refund|awaiting_reserve|open"}},
        },
    },
    "op_sla_breach": {
        "description": "Список заказов с нарушенным или под угрозой SLA + время до/после дедлайна.",
        "input_schema": {"type": "object", "properties": {}},
    },
    "op_order_detail": {
        "description": "Расширенный operator-view заказа: статусы, текущее назначение оператора, аудит-лог.",
        "input_schema": {
            "type": "object",
            "properties": {"order_id": {**_INT, "description": "ID заказа"}},
            "required": ["order_id"],
        },
    },
    "op_assign": {
        "description": "Назначить суб-роль оператора (manager/logist/customs/payments) на заказ. Шаг 1 без to_role/confirmed → форма; шаг 2 с confirmed=true и to_role → запись.",
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": _INT,
                "to_role": {**_STR, "description": "manager|logist|customs|payments"},
                "comment": _STR,
                "confirmed": _BOOL,
            },
            "required": ["order_id"],
        },
    },
    "op_add_note": {
        "description": "Добавить операторскую заметку к заказу (audit-log). Шаг 1 без text/confirmed → форма; шаг 2 → запись.",
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": _INT,
                "text": _STR,
                "confirmed": _BOOL,
            },
            "required": ["order_id"],
        },
    },
    "op_resolve_dispute": {
        "description": "Закрыть спор по заказу. resolution: refund|partial_refund|release|no_action. Шаг 1 — форма; шаг 2 с confirmed=true → запись + side-effects на payment_status.",
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": _INT,
                "resolution": {**_STR, "description": "refund|partial_refund|release|no_action"},
                "refund_amount": _NUM,
                "reason": _STR,
                "confirmed": _BOOL,
            },
            "required": ["order_id"],
        },
    },
    # ── Customs / Compliance ───────────────────────────────
    "op_hs_lookup": {
        "description": "Поиск ТН ВЭД (HS-code) по описанию детали или артикулу.",
        "input_schema": {"type": "object", "properties": {"query": _STR}},
    },
    "op_hs_assign": {
        "description": "Присвоить ТН ВЭД заказу. Шаг 1 без hs_code/confirmed — форма; шаг 2 с confirmed=true → запись.",
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": _INT,
                "hs_code": {**_STR, "description": "ТН ВЭД, например 8413.50"},
                "country": {**_STR, "description": "Страна импорта ISO-2 (RU/BY/KZ/AM/KG)"},
                "confirmed": _BOOL,
            },
            "required": ["order_id"],
        },
    },
    "op_calc_duty": {
        "description": "Расчёт таможенной пошлины + НДС + сборов по заказу. Использует HS-code и страну из заказа (или из параметров).",
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": _INT,
                "hs_code": _STR,
                "country": _STR,
            },
            "required": ["order_id"],
        },
    },
    "op_certs_check": {
        "description": "Проверка обязательных сертификатов для заказа (по ТН ВЭД).",
        "input_schema": {
            "type": "object",
            "properties": {"order_id": _INT},
            "required": ["order_id"],
        },
    },
    "op_cert_upload": {
        "description": "Зафиксировать загрузку сертификата на заказ. Шаг 1 — форма; шаг 2 с confirmed=true.",
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": _INT,
                "cert": {**_STR, "description": "Тип сертификата (EAC, ТР ТС 010/2011...)"},
                "number": _STR,
                "confirmed": _BOOL,
            },
            "required": ["order_id"],
        },
    },
    "op_sanctions_check": {
        "description": "Санкционный скрининг по стране / контрагенту / категории. Возвращает уровень риска (high/medium/low/none) и причины.",
        "input_schema": {
            "type": "object",
            "properties": {
                "country": _STR,
                "entity": _STR,
                "category": _STR,
            },
        },
    },
    "op_customs_dashboard": {
        "description": "Сводка по таможне: грузы на оформлении, готовы к выпуску, ждут документы, в транзите.",
        "input_schema": {"type": "object", "properties": {}},
    },
    "op_customs_release": {
        "description": "Выпустить груз с таможни (status customs → transit_rf). Жёстко проверяет ТН ВЭД и сертификаты. Шаг 1 — форма; шаг 2 с confirmed=true → запись + WS-нотификация покупателю.",
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": _INT,
                "comment": _STR,
                "confirmed": _BOOL,
            },
            "required": ["order_id"],
        },
    },
    "op_payments_dashboard": {
        "description": "Эскроу-сводка платформы: текущий holding, выплачено продавцам, возвращено покупателям, открытые холды по заказам.",
        "input_schema": {"type": "object", "properties": {}},
    },
    "op_logistics_stats": {
        "description": "Логистическая аналитика: KPI по статусам, средний срок доставки, разбивка по перевозчикам.",
        "input_schema": {"type": "object", "properties": {}},
    },
    "op_payments_stats": {
        "description": "Платежная аналитика: разбивка по payment_status, средний чек, refund rate.",
        "input_schema": {"type": "object", "properties": {}},
    },
    # ── Onboarding / KYB wizard ─────────────────────────────
    "start_onboarding": {
        "description": "Точка входа в onboarding/KYB-процесс. Показывает текущий шаг или welcome-экран.",
        "input_schema": {"type": "object", "properties": {}},
    },
    "kyb_status": {
        "description": "Текущий статус KYB-верификации компании пользователя.",
        "input_schema": {"type": "object", "properties": {}},
    },
    "submit_company_info": {
        "description": "Шаг 1/5 onboarding'а — наименование, ИНН, КПП, ОГРН.",
        "input_schema": {
            "type": "object",
            "properties": {
                "legal_name": _STR, "inn": _STR, "kpp": _STR, "ogrn": _STR,
                "confirmed": _BOOL,
            },
        },
    },
    "submit_legal_address": {
        "description": "Шаг 2/5 — юридический адрес.",
        "input_schema": {
            "type": "object",
            "properties": {"legal_address": _STR, "confirmed": _BOOL},
        },
    },
    "submit_bank": {
        "description": "Шаг 3/5 — банковские реквизиты (банк, БИК, расч. счёт).",
        "input_schema": {
            "type": "object",
            "properties": {"bank_name": _STR, "bik": _STR, "bank_account": _STR, "confirmed": _BOOL},
        },
    },
    "submit_director": {
        "description": "Шаг 4/5 — ФИО директора / уполномоченного лица.",
        "input_schema": {
            "type": "object",
            "properties": {"director_name": _STR, "confirmed": _BOOL},
        },
    },
    "submit_for_review": {
        "description": "Шаг 5/5 — отправить заполненную анкету оператору на проверку.",
        "input_schema": {"type": "object", "properties": {"confirmed": _BOOL}},
    },
    "op_kyb_queue": {
        "description": "Очередь KYB-анкет на модерации (operator-only).",
        "input_schema": {"type": "object", "properties": {}},
    },
    "op_kyb_review": {
        "description": "Просмотр KYB-анкеты пользователя.",
        "input_schema": {
            "type": "object",
            "properties": {"user_id": _INT},
            "required": ["user_id"],
        },
    },
    "op_kyb_approve": {
        "description": "Одобрить KYB-анкету. Шаг 1 — preview; шаг 2 с confirmed=true — запись + WS-нотификация заявителю.",
        "input_schema": {
            "type": "object",
            "properties": {"user_id": _INT, "confirmed": _BOOL},
            "required": ["user_id"],
        },
    },
    "op_kyb_reject": {
        "description": "Отклонить KYB с причиной. Шаг 1 — форма; шаг 2 с confirmed=true и reason — запись + нотификация.",
        "input_schema": {
            "type": "object",
            "properties": {"user_id": _INT, "reason": _STR, "confirmed": _BOOL},
            "required": ["user_id"],
        },
    },
    # ── Negotiation (Quote multi-round) ─────────────────────
    "submit_quote": {
        "description": "Продавец создаёт котировку на RFQ. Шаг 1 без confirmed — форма (цены per-line + срок + комментарий); шаг 2 с confirmed=true → запись Quote+QuoteItem.",
        "input_schema": {
            "type": "object",
            "properties": {
                "rfq_id": _INT,
                "delivery_days": _INT,
                "valid_days": _INT,
                "message": _STR,
                "parent_quote_id": _INT,
                "direction": _STR,
                "confirmed": _BOOL,
            },
            "required": ["rfq_id"],
        },
    },
    "view_rfq_quotes": {
        "description": "Покупатель видит все котировки по своему RFQ — sorted by total. Доступно владельцу RFQ или оператору.",
        "input_schema": {
            "type": "object",
            "properties": {"rfq_id": _INT},
            "required": ["rfq_id"],
        },
    },
    "view_quote": {
        "description": "Детальная карточка котировки — позиции, статус, доступные actions.",
        "input_schema": {
            "type": "object",
            "properties": {"quote_id": _INT},
            "required": ["quote_id"],
        },
    },
    "accept_quote": {
        "description": "Покупатель принимает котировку → создаётся Order. Шаг 1 — preview, шаг 2 с confirmed=true.",
        "input_schema": {
            "type": "object",
            "properties": {"quote_id": _INT, "confirmed": _BOOL},
            "required": ["quote_id"],
        },
    },
    "counter_offer": {
        "description": "Покупатель предлагает свою цену. Шаг 1 — форма со всеми позициями, шаг 2 с confirmed=true → новая Quote (direction=buyer_to_seller, round_number+1).",
        "input_schema": {
            "type": "object",
            "properties": {"quote_id": _INT, "confirmed": _BOOL, "message": _STR},
            "required": ["quote_id"],
        },
    },
    "respond_to_counter": {
        "description": "Продавец отвечает на контр-оффер — открывает форму submit_quote с parent_quote_id.",
        "input_schema": {
            "type": "object",
            "properties": {"quote_id": _INT},
            "required": ["quote_id"],
        },
    },
    "mark_quote_final": {
        "description": "Продавец фиксирует свою котировку как финальную (is_final=True) — переторжка невозможна, покупатель только принимает или отклоняет.",
        "input_schema": {
            "type": "object",
            "properties": {"quote_id": _INT},
            "required": ["quote_id"],
        },
    },
    "decline_quote": {
        "description": "Покупатель отклоняет котировку. Уведомляет продавца.",
        "input_schema": {
            "type": "object",
            "properties": {"quote_id": _INT},
            "required": ["quote_id"],
        },
    },
    "send_rfq_to_suppliers": {
        "description": "Разослать RFQ кандидатам-поставщикам (верифицированные KYB приоритетно). DraftCard preview → confirm.",
        "input_schema": {
            "type": "object",
            "properties": {"rfq_id": _INT, "confirmed": _BOOL},
            "required": ["rfq_id"],
        },
    },
    # ── Durable notification preferences ────────────────────
    "notif_prefs": {
        "description": "Текущие настройки durable-каналов (email, telegram, kinds).",
        "input_schema": {"type": "object", "properties": {}},
    },
    "notif_set_email": {
        "description": "Включить/выключить email-уведомления. Шаг 1 — форма; шаг 2 с confirmed=true.",
        "input_schema": {
            "type": "object",
            "properties": {"enabled": _STR, "confirmed": _BOOL},
        },
    },
    "notif_set_kinds": {
        "description": "Какие типы событий доставлять в email/telegram (CSV из order/payment/rfq/sla/claim/system/info).",
        "input_schema": {
            "type": "object",
            "properties": {"kinds": _STR, "confirmed": _BOOL},
        },
    },
    "notif_link_telegram": {
        "description": "Привязать Telegram chat_id для durable-доставки. Демо: ввести числовой chat_id вручную.",
        "input_schema": {
            "type": "object",
            "properties": {"chat_id": _STR, "confirmed": _BOOL},
        },
    },
    # ── Admin (platform-level) actions ──────────────────────
    "admin_dashboard": {
        "description": "Платформенная сводка для админа: GMV 7d, юзеры, заказы, KYB, SLA.",
        "input_schema": {"type": "object", "properties": {}},
    },
    "admin_gmv": {
        "description": "Платформенный GMV по периодам (24h/7d/30d/90d) + топ категорий.",
        "input_schema": {"type": "object", "properties": {}},
    },
    "admin_users": {
        "description": "Список пользователей с фильтрами: all|active|banned|buyers|sellers|kyb_pending.",
        "input_schema": {
            "type": "object",
            "properties": {"filter": _STR},
        },
    },
    "admin_user_detail": {
        "description": "Детальный профиль пользователя для админа: статусы, KYB, wallet, заказы.",
        "input_schema": {
            "type": "object",
            "properties": {"user_id": _INT},
            "required": ["user_id"],
        },
    },
    "admin_ban_user": {
        "description": "Заблокировать пользователя (User.is_active=False). Шаг 1 — форма с reason; шаг 2 c confirmed=true → запись + WS-нотификация.",
        "input_schema": {
            "type": "object",
            "properties": {"user_id": _INT, "reason": _STR, "confirmed": _BOOL},
            "required": ["user_id"],
        },
    },
    "admin_unban_user": {
        "description": "Разблокировать пользователя. DraftCard preview → confirm.",
        "input_schema": {
            "type": "object",
            "properties": {"user_id": _INT, "confirmed": _BOOL},
            "required": ["user_id"],
        },
    },
    "admin_change_role": {
        "description": "Сменить роль пользователя (buyer ↔ seller). Шаг 1 — select; шаг 2 — запись.",
        "input_schema": {
            "type": "object",
            "properties": {"user_id": _INT, "new_role": _STR, "confirmed": _BOOL},
            "required": ["user_id"],
        },
    },
    "admin_moderation_queue": {
        "description": "Единая очередь модерации платформы: KYB pending, refunds, SLA breach, контр-офферы.",
        "input_schema": {"type": "object", "properties": {}},
    },
    "admin_catalog_review": {
        "description": "Каталог-модерация: товары с price=$0, без seller'а, последние добавленные.",
        "input_schema": {"type": "object", "properties": {}},
    },
    "admin_platform_settings": {
        "description": "Read-only снэпшот платформенной конфигурации (engine, env vars).",
        "input_schema": {"type": "object", "properties": {}},
    },
    # ── Auth — TOTP 2FA + API tokens ──────────────────────
    "setup_2fa": {
        "description": "Сгенерировать TOTP secret и показать QR-код для сканирования в authenticator-приложении.",
        "input_schema": {"type": "object", "properties": {}},
    },
    "verify_2fa": {
        "description": "Подтвердить 6-значный OTP код и активировать 2FA.",
        "input_schema": {
            "type": "object",
            "properties": {"code": _STR, "confirmed": _BOOL},
        },
    },
    "disable_2fa": {
        "description": "Выключить 2FA (требует ввода OTP кода для подтверждения).",
        "input_schema": {
            "type": "object",
            "properties": {"code": _STR, "confirmed": _BOOL},
        },
    },
    "create_api_token": {
        "description": "Сгенерировать API-токен для интеграций. Полный токен виден ОДИН раз.",
        "input_schema": {
            "type": "object",
            "properties": {
                "label": _STR,
                "permissions": {**_STR, "description": "read | read,write | read,write,admin"},
                "confirmed": _BOOL,
            },
        },
    },
    "list_api_tokens": {
        "description": "Список API-токенов пользователя (активных и отозванных).",
        "input_schema": {"type": "object", "properties": {}},
    },
    "revoke_api_token": {
        "description": "Отозвать API-токен. DraftCard preview → confirm.",
        "input_schema": {
            "type": "object",
            "properties": {"token_id": _INT, "confirmed": _BOOL},
            "required": ["token_id"],
        },
    },
}


def get_tool_definitions(role: str) -> list[dict]:
    """Return Claude tool-use definitions filtered by role permissions."""
    available = list_actions(role)
    out = []
    for name in available:
        schema = TOOL_SCHEMAS.get(name)
        if not schema:
            continue
        out.append({
            "name": name,
            "description": schema["description"],
            "input_schema": schema["input_schema"],
        })
    return out


# ══════════════════════════════════════════════════════════
# Action handlers
# ══════════════════════════════════════════════════════════

_OEM_RE = __import__("re").compile(r"^[A-Za-z0-9][A-Za-z0-9\-/.]{3,18}$")


@register("open_url")
def open_url(params, user, role):
    """Stub: navigation handled client-side via params._url. If we get here,
    the frontend didn't intercept and we just confirm the link."""
    url = params.get("_url") or "/"
    return ActionResult(text=f"Открываю: {url}")


@register("generate_proposal")
def generate_proposal(params, user, role):
    """Generate commercial proposal (КП) for an RFQ. Returns link to proposal page."""
    from marketplace.models import RFQ
    rfq_id = params.get("rfq_id")
    if not rfq_id:
        # Default to user's most recent RFQ
        rfq = RFQ.objects.filter(created_by=user).order_by("-created_at").first()
        if not rfq:
            return ActionResult(text="⚠️ У вас пока нет ни одного RFQ для формирования КП.")
        rfq_id = rfq.id
    else:
        try:
            rfq = RFQ.objects.get(id=rfq_id)
        except RFQ.DoesNotExist:
            return ActionResult(text=f"⚠️ RFQ #{rfq_id} не найден")

    items_count = rfq.items.count()
    total = sum(
        float(it.matched_part.price) * it.quantity
        for it in rfq.items.select_related("matched_part").all()
        if it.matched_part and it.matched_part.price
    )

    return ActionResult(
        text=f"КП по RFQ #{rfq.id} готово — {items_count} позиций на сумму ${total:,.0f}",
        cards=[{
            "type": "rfq",
            "data": {
                "id": str(rfq.id),
                "number": rfq.id,
                "status": rfq.status,
                "description": f"Коммерческое предложение · {items_count} позиций · ${total:,.0f}",
                "customer": rfq.customer_name,
                "created_at": rfq.created_at.strftime("%d.%m.%Y"),
            },
        }],
        actions=[
            {"label": "Открыть КП", "action": "open_url",
             "params": {"_url": f"/chat/proposal/{rfq.id}/"}},
            {"label": "Скачать PDF", "action": "open_url",
             "params": {"_url": f"/rfq/{rfq.id}/proposal/pdf/"}},
            {"label": "Открыть RFQ", "action": "open_url",
             "params": {"_url": f"/chat/rfq/{rfq.id}/"}},
        ],
    )


def _extract_articles(text: str) -> list[str]:
    """Extract OEM-like article numbers from a multi-line message."""
    if not text:
        return []
    out = []
    # Split on common separators: newlines, commas, semicolons
    for chunk in __import__("re").split(r"[\n,;]+", text):
        token = chunk.strip().strip(".").strip()
        if token and _OEM_RE.match(token) and any(ch.isdigit() for ch in token):
            out.append(token)
    return out


@register("search_parts")
def search_parts(params, user, role):
    """Search catalog. params: {query, articles?, brand?, category?, limit?}

    If query contains multiple article-like tokens (newline/comma separated),
    auto-extracts and searches each individually — returns one product card per
    matched part. Renders as a spec_results-style card if 5+ articles supplied.
    """
    from marketplace.models import Part
    query = (params.get("query") or "").strip()
    limit = min(int(params.get("limit") or 20), 50)

    # 1) Multi-article list (paste of OEM numbers) ------------------
    articles = params.get("articles") or _extract_articles(query)
    if len(articles) >= 2:
        return _search_articles_list(
            articles, params.get("quantities") or {},
            dest_country=params.get("dest_country") or "",
            delivery_address=params.get("delivery_address") or "",
            arrival_port=params.get("arrival_port") or "",
            filter_origin=params.get("filter_origin") or "",
        )

    # 2) Free-text query --------------------------------------------
    qs = Part.objects.select_related("brand", "category").filter(is_active=True)
    if query:
        # OEM-нормализатор: «707-99-58030» матчит «7079958030», «CAT-265-0235»
        # матчит «265-0235». Lookup идёт двусторонне: и query→БД, и БД→query
        # через SQL `replace()` annotation (убираем разделители из stored OEM,
        # сравниваем с нормализованной формой query).
        from django.db.models import Value as V
        from django.db.models.functions import Replace, Upper

        from .oem_normalizer import _strip_separators, expand_query_for_db
        candidates = expand_query_for_db(query, params.get("brand"))
        clean_candidates = list({
            _strip_separators(c).upper() for c in candidates if c
        })
        qs = qs.annotate(
            oem_clean=Upper(Replace(Replace(Replace(Replace(
                "oem_number",
                V("-"), V("")), V("."), V("")), V(" "), V("")), V("/"), V(""))),
        )
        oem_q = Q()
        for c in candidates:
            oem_q |= Q(oem_number__iexact=c)
        if clean_candidates:
            oem_q |= Q(oem_clean__in=clean_candidates)
        # Fallback: icontains (для частичных «707-99» + title/description-поиск)
        qs = qs.filter(
            oem_q
            | Q(oem_number__icontains=query)
            | Q(title__icontains=query)
            | Q(description__icontains=query)
        )
    if params.get("brand"):
        qs = qs.filter(brand__name__icontains=params["brand"])
    if params.get("category"):
        qs = qs.filter(category__name__icontains=params["category"])

    parts = list(qs[:limit])
    cards = [{
        "type": "product",
        "data": {
            "id": str(p.id),
            "article": p.oem_number,
            "brand": p.brand.name if p.brand else "—",
            "name": p.title,
            "price": float(p.price) if p.price else None,
            "currency": "USD",
            "in_stock": getattr(p, "stock_qty", 0) > 0,
            "category": p.category.name if p.category else None,
        },
    } for p in parts]

    if not cards:
        return ActionResult(
            text=f"По запросу «{query}» в каталоге ничего не найдено.",
            actions=[
                {"label": "Создать RFQ", "action": "create_rfq",
                 "params": {"query": query, "quantity": 1}},
            ],
            suggestions=["Найти аналог", "Загрузить список артикулов"],
        )

    return ActionResult(
        text=f"Найдено {len(cards)} позиций по запросу «{query}»:",
        cards=cards,
        actions=[
            {"label": "Создать RFQ на все", "action": "create_rfq",
             "params": {"product_ids": [c["data"]["id"] for c in cards]}},
            {"label": "Сравнить", "action": "compare_products",
             "params": {"product_ids": [c["data"]["id"] for c in cards]}},
        ],
        suggestions=["Показать ещё", "Фильтр по бренду", "История цен"],
    )


def _search_articles_list(articles: list[str], quantities: dict | None = None,
                            dest_country: str = "", delivery_address: str = "",
                            arrival_port: str = "", filter_origin: str = ""):
    """Look up each article in the catalog → spec_results-style card.

    quantities: {oem: qty} — параметр от fast-path парсера «OEM qty».
    """
    from marketplace.models import Part
    qmap = quantities or {}

    items = []
    matched_ids: list[str] = []
    matched_qty_pairs: list[tuple[str, int]] = []
    found_n = 0
    not_found_n = 0
    total = 0
    ship_total = 0
    origins_count: dict[str, int] = {}  # 'CN'/'TR' → кол-во позиций
    # (Part, qty, price, cargo_line) — для второго прохода (агрегат фрахта).
    resolved_parts: list = []

    from decimal import Decimal

    from assistant.logistics import (
        _country_from_port,
        _volumetric_kg,
        calc_incoterm_breakdown,
    )
    from marketplace.models import LogisticsTariff
    delivery_address = (delivery_address or "").strip()
    arrival_port = (arrival_port or "").strip()
    # Страна назначения выводится из префикса порта прибытия (RUMOW → RU).
    # FOB не требует данных — клиент сам забирает в порту поставщика.
    # CIP нужен arrival_port. DDP — arrival_port + delivery_address.
    dest = (dest_country or "").upper()[:2]
    if not dest and arrival_port:
        dest = _country_from_port(arrival_port)
    cip_available = bool(arrival_port)
    ddp_available = bool(arrival_port and delivery_address)
    # «needs_delivery_info» = нет данных даже для CIP. Форма всё равно
    # покажется над матрицей, FOB будет доступен сразу.
    needs_delivery_info = not cip_available
    # Матрица 3 mode × 3 incoterm + детальный breakdown.
    matrix_ships = {(m, i): Decimal("0") for m in ("sea","air","auto") for i in ("FOB","CIP","DDP")}
    matrix_breakdown = {(m, i): {"freight": Decimal("0"), "insurance": Decimal("0"),
                                   "carriage_ext": Decimal("0"), "duty": Decimal("0"),
                                   "vat": Decimal("0"), "last_mile": Decimal("0")}
                          for m in ("sea","air","auto") for i in ("FOB","CIP","DDP")}
    matrix_days  = {("sea","FOB"): 0, ("air","FOB"): 0, ("auto","FOB"): 0}
    matrix_avail = {"sea": False, "air": False, "auto": False}
    for art in articles:
        qty = int(qmap.get(art, 1) or 1)
        # Ищем ВСЕ Part с таким OEM (не только первый): нужно ранжировать
        # по цене + рейтингу поставщика и отфильтровать «Исключён».
        # ТЗ §3: rejected/«Исключён» не участвует ни в каком режиме.
        # OEM-нормализация: артикул в БД и query могут различаться раздели-
        # телями/leading zero. Annotation чистит stored oem_number и матчит
        # против нормализованной формы query. Двусторонняя проверка.
        from django.db.models import Value as V
        from django.db.models.functions import Replace, Upper

        from .oem_normalizer import _strip_separators, normalize_oem
        oem_candidates = normalize_oem(art)
        clean_candidates = list({
            _strip_separators(c).upper() for c in oem_candidates if c
        })
        candidates = list(
            Part.objects
            .select_related("brand", "seller", "seller__profile")
            .annotate(oem_clean=Upper(Replace(Replace(Replace(Replace(
                "oem_number",
                V("-"), V("")), V("."), V("")), V(" "), V("")), V("/"), V(""))))
            .filter(is_active=True)
            .filter(
                Q(oem_number__in=oem_candidates) | Q(oem_clean__in=clean_candidates)
            )
        )
        if not candidates:
            candidates = list(
                Part.objects
                .select_related("brand", "seller", "seller__profile")
                .filter(is_active=True, oem_number__icontains=art)[:20]
            )
        # Фильтруем исключённых поставщиков
        candidates = [c for c in candidates
                      if _seller_rating(c.seller).get("status") != "rejected"]
        # Ранжируем по цене + рейтингу (50/50)
        offer_pool = []
        for c in candidates:
            r = _seller_rating(c.seller)
            offer_pool.append({
                "part": c, "price": float(c.price) if c.price else None,
                "rating": r["rating"], "status": r["status"],
            })
        ranked = _rank_offers(offer_pool)
        p = ranked[0]["part"] if ranked else None
        best_status = ranked[0]["status"] if ranked else "sandbox"
        best_rating = ranked[0]["rating"] if ranked else 60.0
        alt_offers_count = max(0, len(ranked) - 1)
        if p:
            origin_cc = _country_from_port(p.sea_port or p.air_port or "")
            # Фильтр по стране отправления — "купить только из Турции".
            if filter_origin and origin_cc and origin_cc.upper() != filter_origin.upper():
                items.append({"status": "skipped", "id": p.oem_number,
                              "name": p.title, "qty": qty,
                              "reason": f"origin {origin_cc}"})
                continue
            price = float(p.price) if p.price else 0
            cargo_line = Decimal(str(price)) * Decimal(qty)
            if origin_cc:
                origins_count[origin_cc] = origins_count.get(origin_cc, 0) + 1
            # FOB не зависит от mode/dest — заводим $0 ship per line.
            for m in ("sea", "air", "auto"):
                bd_fob = calc_incoterm_breakdown(Decimal("0"), cargo_line, "FOB")
                matrix_ships[(m, "FOB")] += bd_fob["total"]
                for k in ("freight", "insurance", "carriage_ext", "duty", "vat", "last_mile"):
                    matrix_breakdown[(m, "FOB")][k] += bd_fob[k]
            # Запоминаем параметры для второго прохода (агрегация по shipment).
            resolved_parts.append((p, qty, price, cargo_line))
            items.append({
                "status": "in_stock",
                "id": p.oem_number,
                "name": p.title,
                "brand": p.brand.name if p.brand else "—",
                "condition": "oem",
                "price": price,
                "qty": qty,
                "weight": f"{p.gross_weight_kg} кг" if p.gross_weight_kg else "—",
                "ship_cost": None,
                "ship_mode": None,
                "ship_days": None,
                "currency": "USD",
                # Поставщик: статус + рейтинг (§3 ТЗ)
                "supplier_status": best_status,
                "supplier_status_badge": _status_badge(best_status),
                "supplier_rating": round(best_rating, 1),
                "alt_offers": alt_offers_count,  # сколько ещё поставщиков по этой позиции
                # Полный ранжированный список поставщиков для inline-раскрытия.
                # Анонимизирован: только псевдоним #S{id%1000:03d} + бейдж.
                "alt_suppliers": [
                    {
                        "label": f"Поставщик #S{(o['part'].seller_id or 0) % 1000:03d}",
                        "price": o["price"],
                        "currency": "USD",
                        "rating": round(o["rating"], 1),
                        "status": o["status"],
                        "status_badge": _status_badge(o["status"]),
                        "score": round(o.get("score", 0) * 100, 1) if o.get("score") else None,
                        "warehouse": (o["part"].warehouse_address or "")[:40],
                        "condition": o["part"].condition or "oem",
                        "stock": getattr(o["part"], "stock_quantity", 0) or 0,
                        "is_primary": (o["part"].id == p.id),
                    }
                    for o in ranked
                ],
            })
            matched_ids.append(str(p.id))
            matched_qty_pairs.append((str(p.id), qty))
            found_n += 1
            total += price * qty
        else:
            items.append({
                "status": "not_found",
                "id": art,
                "name": "",
                "qty": qty,
            })
            not_found_n += 1

    # ── Pass 2: агрегируем фрахт по группам (origin_port, mode). ─────────
    # min_charge тарифа — это минимум одного коносамента/AWB, а не per-item.
    # Поэтому собираем все позиции одной отправки вместе, считаем
    # rate × Σchargeable_kg, и лишь итог клампим к min_charge.
    cargo_total = Decimal(str(total))
    from collections import defaultdict
    origin_groups_info: dict = defaultdict(lambda: {
        "count": 0, "weight": Decimal("0"), "cargo": Decimal("0"),
        "freight": {}, "days": {}, "items": [],
    })
    # Базовая разбивка origin_groups_info — собираем даже когда dest нет.
    # Это нужно чтобы кнопка «Состав» и таблица origin_breakdown были
    # доступны до того как пользователь укажет порт прибытия.
    if resolved_parts:
        for p, qty, price, cargo_line in resolved_parts:
            origin = (p.sea_port or p.air_port or "").strip()
            origin_code = origin.split()[0] if origin else ""
            if not origin_code:
                continue
            info = origin_groups_info[origin_code]
            ch = max(
                Decimal(p.gross_weight_kg or 0),
                _volumetric_kg(p.length_cm, p.width_cm, p.height_cm, "sea"),
            ) * Decimal(qty)
            info["count"] += 1
            info["weight"] += ch
            info["cargo"] += cargo_line
            info["items"].append({
                "oem": p.oem_number,
                "title": p.title[:60] if p.title else "",
                "weight_kg": float(p.gross_weight_kg or 0),
                "cargo": float(cargo_line),
            })
    if dest and resolved_parts:
        # cache тарифов чтобы не дёргать БД 9× на каждую группу
        tariff_cache: dict = {}
        def _lookup_tariff(origin_code: str, m: str):
            key = (origin_code, m)
            if key in tariff_cache:
                return tariff_cache[key]
            cc = _country_from_port(origin_code) or origin_code
            t = LogisticsTariff.objects.filter(
                origin_port__iexact=origin_code, dest_country=dest,
                mode=m, is_active=True,
            ).first()
            if not t and cc and cc != origin_code:
                t = LogisticsTariff.objects.filter(
                    origin_port__iexact=cc, dest_country=dest,
                    mode=m, is_active=True,
                ).first()
            tariff_cache[key] = t
            return t

        per_mode_freight: dict = {}  # mode → (freight_total, transit_days)
        per_line_ship: dict = {}     # mode → {part_id: ship_cost}
        for m in ("sea", "air", "auto"):
            # Группируем позиции по origin_port
            groups = defaultdict(list)
            for p, qty, price, cargo_line in resolved_parts:
                port_field = "sea_port" if m == "sea" else "air_port" if m == "air" else "sea_port"
                origin = ((getattr(p, port_field, "") or "").strip())
                origin_code = origin.split()[0] if origin else ""
                if not origin_code:
                    continue
                ch = max(
                    Decimal(p.gross_weight_kg or 0),
                    _volumetric_kg(p.length_cm, p.width_cm, p.height_cm, m),
                ) * Decimal(qty)
                if ch <= 0:
                    continue
                groups[origin_code].append((p, ch, cargo_line))
            mode_freight = Decimal("0")
            mode_days = 0
            line_ship: dict = {}
            for origin_code, lines in groups.items():
                t = _lookup_tariff(origin_code, m)
                if not t:
                    continue
                ch_sum = sum((l[1] for l in lines), Decimal("0"))
                group_freight = ch_sum * t.rate_per_kg
                if t.min_charge and group_freight < t.min_charge:
                    group_freight = Decimal(t.min_charge)
                mode_freight += group_freight
                if t.transit_days and t.transit_days > mode_days:
                    mode_days = t.transit_days
                # count/weight/cargo/items уже заполнены в первом проходе
                info = origin_groups_info[origin_code]
                info["freight"][m] = group_freight
                info["days"][m] = t.transit_days or 0
                # Распределяем freight группы по позициям пропорционально весу
                for p, ch, _cargo in lines:
                    share = (group_freight * ch / ch_sum).quantize(Decimal("0.01")) if ch_sum > 0 else Decimal("0")
                    line_ship[p.id] = float(share)
            if mode_freight > 0:
                matrix_avail[m] = True
                per_mode_freight[m] = (mode_freight, mode_days)
                per_line_ship[m] = line_ship
                matrix_days[(m, "FOB")] = mode_days
                for inc in ("CIP", "DDP"):
                    bd = calc_incoterm_breakdown(mode_freight, cargo_total, inc)
                    matrix_ships[(m, inc)] = bd["total"]
                    for k in ("freight", "insurance", "carriage_ext", "duty", "vat", "last_mile"):
                        matrix_breakdown[(m, inc)][k] = bd[k]

        # Заполняем per-line ship_cost (используем самый дешёвый режим)
        if per_mode_freight:
            best_mode = min(per_mode_freight.items(), key=lambda x: x[1][0])[0]
            line_map = per_line_ship.get(best_mode, {})
            for it in items:
                if it["status"] != "in_stock":
                    continue
                p_match = next((p for p, q, _, _ in resolved_parts if p.oem_number == it["id"]), None)
                if p_match and p_match.id in line_map:
                    s = line_map[p_match.id]
                    it["ship_cost"] = s
                    it["ship_mode"] = best_mode
                    it["ship_days"] = per_mode_freight[best_mode][1]
                    ship_total += s

    landed_total = total + ship_total
    if found_n and needs_delivery_info:
        intro = (
            f"Проверил {len(articles)} артикулов: {found_n} найдено, "
            f"{not_found_n} нет в каталоге. Сумма EXW — ${total:,.0f}. "
            f"FOB-самовывоз из порта поставщика доступен сразу. "
            f"Чтобы рассчитать CIP (до вашего порта) или DDP (до двери) — "
            f"укажите порт прибытия (и адрес для DDP) ниже."
        )
    elif found_n and not ddp_available:
        intro = (
            f"Проверил {len(articles)} артикулов: {found_n} найдено, "
            f"{not_found_n} нет в каталоге. Сумма EXW — ${total:,.0f}. "
            f"FOB и CIP до {dest or 'порта'} рассчитаны. "
            f"Для DDP добавьте адрес доставки."
        )
    else:
        intro = (
            f"Проверил {len(articles)} артикулов: {found_n} найдено, "
            f"{not_found_n} нет в каталоге. "
            + (f"Сумма по найденным — ${total:,.0f}. Выберите способ и базис ниже."
               if found_n else "Можно создать RFQ — поставщики поищут аналоги.")
        )

    # Полный набор действий — то, что умел маркетплейс, но прямо в чате.
    # Порядок: primary (RFQ) → создание ценности (КП, заказ) → сравнение/анализ → утилиты.
    actions = []
    if matched_ids:
        # quantities: dict id→qty чтобы quick_order/create_rfq могли учесть
        qty_param = {pid: q for pid, q in matched_qty_pairs} if any(q != 1 for _, q in matched_qty_pairs) else None
        qo_params = {"product_ids": matched_ids}
        rfq_params = {"product_ids": matched_ids}
        if qty_param:
            qo_params["product_quantities"] = qty_param
            rfq_params["product_quantities"] = qty_param
        # Кнопка как fallback — без явного выбора базиса (дефолт: sea/FOB)
        actions.append({"label": f"⚡ Купить сейчас ${total:,.0f}",
                        "action": "quick_order", "params": qo_params})
        actions.append({"label": "Создать RFQ на найденные", "action": "create_rfq",
                        "params": rfq_params})
    if not_found_n:
        actions.append({"label": f"RFQ на {not_found_n} ненайденных",
                        "action": "create_rfq",
                        "params": {"query": ", ".join(it["id"] for it in items if it["status"] == "not_found")}})
    actions.append({"label": "Создать RFQ на все", "action": "create_rfq",
                    "params": {"query": ", ".join(articles)}})
    if matched_ids:
        actions.append({"label": "Сравнить поставщиков", "action": "top_suppliers",
                        "params": {"limit": 3}})
        actions.append({"label": "Только OEM", "action": "analyze_spec",
                        "params": {"condition": "oem"}})
        actions.append({"label": "Найти дешевле (аналоги)", "action": "analyze_spec",
                        "params": {"condition": "analogue"}})

    # Сводим origin_breakdown по странам (а не по портам) — клиенту важна
    # страна для решения «забрать только из Турции». При нескольких origin
    # добавляем кнопки фильтрации.
    origin_breakdown = []
    cc_flags = {"CN":"🇨🇳","TR":"🇹🇷","AE":"🇦🇪","NL":"🇳🇱","KZ":"🇰🇿","RU":"🇷🇺","DE":"🇩🇪","US":"🇺🇸","PK":"🇵🇰","ES":"🇪🇸"}
    cc_names = {"CN":"Китай","TR":"Турция","AE":"ОАЭ","NL":"Нидерланды","KZ":"Казахстан","RU":"Россия","DE":"Германия","US":"США","PK":"Пакистан","ES":"Испания"}
    if origin_groups_info:
        from collections import defaultdict as _dd
        by_country: dict = _dd(lambda: {
            "ports": set(), "count": 0, "weight": Decimal("0"), "cargo": Decimal("0"),
            "freight": {"sea": Decimal("0"), "air": Decimal("0"), "auto": Decimal("0")},
            "days": {"sea": 0, "air": 0, "auto": 0},
            "items": [],
        })
        for origin_code, info in origin_groups_info.items():
            cc = _country_from_port(origin_code) or origin_code[:2].upper()
            b = by_country[cc]
            b["ports"].add(origin_code)
            b["count"] += info["count"]
            b["weight"] += info["weight"]
            b["cargo"] += info["cargo"]
            b["items"].extend(info.get("items", []))
            for m in ("sea", "air", "auto"):
                if m in info["freight"]:
                    b["freight"][m] += info["freight"][m]
                    if info["days"].get(m, 0) > b["days"][m]:
                        b["days"][m] = info["days"][m]
        for cc, b in sorted(by_country.items(), key=lambda x: -x[1]["cargo"]):
            # Скипаем артефакты: origin без позиций (бывает когда air_port
            # отличается от sea_port — для air-mode появляется лишняя группа).
            if b["count"] == 0:
                continue
            origin_breakdown.append({
                "country_code": cc,
                "flag": cc_flags.get(cc, "🌍"),
                "name": cc_names.get(cc, cc),
                "ports": sorted(b["ports"]),
                "count": b["count"],
                "weight_kg": float(b["weight"]),
                "cargo": float(b["cargo"]),
                "freight_sea": float(b["freight"]["sea"]),
                "freight_air": float(b["freight"]["air"]),
                "freight_auto": float(b["freight"]["auto"]),
                "days_sea": b["days"]["sea"],
                "days_air": b["days"]["air"],
                "days_auto": b["days"]["auto"],
                "items": b["items"],
            })

    card = {
        "type": "spec_results",
        "data": {
            "title": f"Подбор по списку — {len(articles)} артикулов",
            "found": found_n,
            "analogue": 0,
            "not_found": not_found_n,
            "items": items,
            "more_count": 0,
            "offers_count": found_n,
            "sellers_count": found_n,  # 1 supplier per match in stub
            "best_mix": int(total) if total else None,
            "total": int(total) if total else None,
            "shipping_total": int(ship_total) if ship_total else None,
            "landed_total": int(landed_total) if landed_total else None,
            "dest_country": dest,
            "currency": "USD",
            "foot_info": f"{found_n} из {len(articles)} priced" +
                          (f" · доставка ${ship_total:,.0f}" if ship_total else ""),
            # Матрица 3 mode × 3 incoterm — для виджета выбора базиса.
            # Матрица всегда видна: FOB-колонка работает без dest/адреса
            # (клиент сам забирает в порту отгрузки). CIP/DDP — гейтятся
            # наличием arrival_port / delivery_address.
            "shipping_matrix": [
                {
                    "mode": m,
                    "mode_label": {"sea":"🚢 Морем","air":"✈️ Авиа","auto":"🚚 Авто"}[m],
                    "days": matrix_days.get((m, "FOB"), 0),
                    "available": True,
                    "options": [
                        {
                            "incoterm": inc,
                            "available": (
                                True if inc == "FOB"
                                else (cip_available and matrix_avail.get(m, False)) if inc == "CIP"
                                else (ddp_available and matrix_avail.get(m, False))
                            ),
                            "ship": float(matrix_ships[(m, inc)]),
                            "landed": float(Decimal(str(total)) + matrix_ships[(m, inc)]),
                            "breakdown": {
                                "freight": float(matrix_breakdown[(m, inc)]["freight"]),
                                "insurance": float(matrix_breakdown[(m, inc)]["insurance"]),
                                "carriage_ext": float(matrix_breakdown[(m, inc)]["carriage_ext"]),
                                "duty": float(matrix_breakdown[(m, inc)]["duty"]),
                                "vat": float(matrix_breakdown[(m, inc)]["vat"]),
                                "last_mile": float(matrix_breakdown[(m, inc)]["last_mile"]),
                            },
                        }
                        for inc in ("FOB", "CIP", "DDP")
                    ],
                }
                for m in ("sea", "air", "auto")
            ],
            "incoterm_descs": {
                "FOB": "самовывоз из порта поставщика — без доплат к EXW",
                "CIP": "port-to-port фрахт + страховка груза (1.5%). Таможня — покупателя",
                "DDP": "all-in до двери: фрахт + страховка + пошлина (~10%) + НДС 20% + last-mile (~5%)",
            },
            "cip_available": cip_available,
            "origin_breakdown": origin_breakdown,
            "filter_origin": filter_origin,
            "needs_delivery_info": needs_delivery_info,
            "ddp_available": ddp_available,
            "delivery_address": delivery_address,
            "arrival_port": arrival_port,
            "dest_country": dest,
            "orig_articles": list(articles),  # для повторного вызова с адресом
            # Откуда едет груз — для понимания базиса FOB
            "origins": [
                {"country_code": cc, "count": n,
                 "flag": {"CN":"🇨🇳","TR":"🇹🇷","AE":"🇦🇪","NL":"🇳🇱","KZ":"🇰🇿","RU":"🇷🇺","DE":"🇩🇪","US":"🇺🇸"}.get(cc, "🌍"),
                 "name": {"CN":"Китай","TR":"Турция","AE":"ОАЭ","NL":"Нидерланды","KZ":"Казахстан","RU":"Россия","DE":"Германия","US":"США"}.get(cc, cc)}
                for cc, n in sorted(origins_count.items(), key=lambda x: -x[1])
            ],
            "product_ids": matched_ids,
            "product_quantities": ({pid: q for pid, q in matched_qty_pairs}
                                    if any(q != 1 for _, q in matched_qty_pairs) else None),
        },
    }

    return ActionResult(
        text=intro,
        cards=[card],
        actions=actions,
        suggestions=[
            "Найти аналоги для ненайденных",
            "Сравни цены по бренду",
            "Сформировать КП",
            "Топ-3 поставщика",
            "Скачать спеку CSV",
        ],
    )


def _classify_rfq_mode(items_to_add, user, params) -> tuple[str, str]:
    """Определяет режим обработки RFQ согласно детальному ТЗ §3-§5.

    Возвращает (mode, reason) где:
      mode  — 'auto' | 'semi' | 'manual'
      reason — человекочитаемое объяснение (для notes / UI / аудита)

    Правила (приоритет сверху вниз):
      0. params.mode передан явно → override (ops / тесты / форс)

    Гард-условия для SEMI:
      1. Buyer не верифицирован KYB → SEMI (защита от спама)
      2. urgency=critical → SEMI (нужен человек в loop'е)
      3. confidence < 70% (если матчер передал) → SEMI (§5.3)

    Triggers для MANUAL_OEM (§7):
      4. params.articles[] (явный OEM-ввод) → MANUAL_OEM
      5. 0% сматчено → MANUAL_OEM (нет в каталоге)

    Условия чистого AUTO (§4.1) — ВСЕ должны выполниться:
      6a. Все позиции имеют matched_part
      6b. Для каждой позиции ≥3 актуальных предложений (trusted + sandbox)
      6c. Для каждой позиции есть ≥1 «надёжный» поставщик
      6d. Все matched_part от trusted-поставщиков (исполнитель)
    Иначе → SEMI с пояснением (§5.1, §5.2).
    """
    # 0. Явный override
    explicit = (params.get("mode") or "").strip().lower()
    if explicit in ("auto", "semi", "manual"):
        return explicit, f"mode={explicit} (явно передан в params)"

    total = len(items_to_add)
    matched = [t for t in items_to_add if t[2] is not None]
    matched_count = len(matched)

    # 4. Buyer вручную ввёл OEM-номера → MANUAL_OEM
    if params.get("articles"):
        return "manual", (
            f"manual · buyer ввёл {len(params['articles'])} OEM-номеров вручную"
        )

    # 5. Ни одного совпадения с каталогом → MANUAL_OEM
    if matched_count == 0:
        return "manual", f"manual · 0/{total} позиций сматчены с каталогом"

    # 1. Buyer не верифицирован KYB → SEMI
    try:
        from .onboarding import kyb_required_for_seller as _kyb_required
        kyb_unverified = _kyb_required(user)
    except Exception:
        kyb_unverified = False
    if kyb_unverified:
        return "semi", (
            f"semi · buyer не верифицирован KYB · {matched_count}/{total} matched"
        )

    # 2. Срочность critical → SEMI
    urgency = (params.get("urgency") or "").strip().lower()
    if urgency == "critical":
        return "semi", f"semi · urgency=critical · {matched_count}/{total} matched"

    # 3. confidence ниже порога → SEMI (§5.3)
    # items_to_add tuples могут быть (query, qty, mp) [legacy] или
    # (query, qty, mp, confidence) [новый формат]. Читаем conf если есть.
    confidence_threshold = float(params.get("confidence_threshold") or 70)
    low_confidence = []
    for t in matched:
        query = t[0]
        # confidence: либо из 4-tuple, либо из params (старый API)
        c = t[3] if len(t) >= 4 else None
        if c is None:
            c = params.get(f"confidence_{query}") or params.get("min_confidence")
        if c is not None and float(c) < confidence_threshold:
            low_confidence.append(query)
    if low_confidence:
        return "semi", (
            f"semi · confidence <{confidence_threshold}% по {len(low_confidence)} позиции"
        )

    # 6. AUTO условия (§4.1) — проверяем все
    if matched_count < total:
        return "semi", (
            f"semi · partial match {matched_count}/{total} · "
            f"{total - matched_count} требуют уточнения (§5.2)"
        )

    # Считаем предложения per-position: для каждого matched_part ищем все Part'ы
    # с тем же oem_number и считаем поставщиков по supplier_status.
    from marketplace.models import Part, UserProfile
    insufficient_offers = []
    no_trusted = []
    untrusted_executor = []
    min_offers = int(params.get("min_offers_for_auto") or 3)

    # Собираем все oem_numbers items_to_add (matched это tuples длины 3 или 4)
    oem_set = list({t[2].oem_number for t in matched if t[2] and t[2].oem_number})
    if oem_set:
        # Один SQL: все Part'ы с этими OEM + их seller-profiles
        candidates = (
            Part.objects.filter(oem_number__in=oem_set, is_active=True, price__gt=0)
            .select_related("seller")
        )
        # Build map oem → list of (seller_id, status)
        offers_by_oem = {}
        seller_status_cache = {}
        for c in candidates:
            if not c.seller_id:
                continue
            if c.seller_id not in seller_status_cache:
                prof = UserProfile.objects.filter(user_id=c.seller_id).only("supplier_status").first()
                seller_status_cache[c.seller_id] = prof.supplier_status if prof else "sandbox"
            status = seller_status_cache[c.seller_id]
            # ТЗ §3: «Исключён» (status='rejected' в модели) — полностью отключён
            if status == "rejected":
                continue
            offers_by_oem.setdefault(c.oem_number, []).append((c.seller_id, status))

        for t in matched:
            mp = t[2]
            offers = offers_by_oem.get(mp.oem_number, [])
            unique_sellers = {sid: st for sid, st in offers}
            n_offers = sum(1 for st in unique_sellers.values() if st in ("trusted", "sandbox"))
            n_trusted = sum(1 for st in unique_sellers.values() if st == "trusted")

            # 6b. <3 предложений (trusted + sandbox)
            if n_offers < min_offers:
                insufficient_offers.append((mp.oem_number, n_offers))
            # 6c. нет ни одного trusted
            if n_trusted == 0:
                no_trusted.append(mp.oem_number)

        # 6d. Исполнитель (matched_part.seller) обязан быть trusted
        for t in matched:
            mp = t[2]
            executor_status = seller_status_cache.get(
                mp.seller_id,
                (UserProfile.objects.filter(user_id=mp.seller_id)
                 .only("supplier_status").first().supplier_status
                 if mp.seller_id else "sandbox"),
            ) if mp.seller_id else "sandbox"
            if executor_status != "trusted":
                untrusted_executor.append((mp.seller_id, executor_status))

    # Применяем правила в порядке тяжести
    if no_trusted:
        return "semi", (
            f"semi · нет «надёжных» поставщиков по {len(no_trusted)} позициям (§5.1)"
        )
    if insufficient_offers:
        return "semi", (
            f"semi · недостаточно предложений (<{min_offers}) "
            f"по {len(insufficient_offers)} позициям (§5.2)"
        )
    if untrusted_executor:
        kinds = ",".join(sorted(set(s for _, s in untrusted_executor)))
        return "semi", (
            f"semi · исполнитель не trusted ({kinds}) · "
            f"требуется подтверждение оператора (§6.2)"
        )

    return "auto", (
        f"auto · {matched_count}/{total} matched · ≥{min_offers} предложений · "
        f"trusted-исполнитель · buyer verified"
    )


def _match_confidence(query: str, matched_part) -> int:
    """Confidence score 0-100 для соответствия query→matched_part.

    100 — exact OEM match (case-insensitive)
     80 — substring (либо query в OEM, либо OEM в query)
     60 — fuzzy (нет точного совпадения, но что-то нашлось)
      0 — нет matched_part
    """
    if not matched_part:
        return 0
    q = (query or "").strip().lower()
    oem = (getattr(matched_part, "oem_number", "") or "").strip().lower()
    if not q or not oem:
        return 60
    if q == oem:
        return 100
    if q in oem or oem in q:
        return 80
    return 60


@register("create_rfq")
def create_rfq(params, user, role):
    """Create a new RFQ + RFQItem rows. params: {product_ids?, articles?, quantity, query?}"""
    from marketplace.models import RFQ, Part, RFQItem

    quantity = int(params.get("quantity") or 1)

    # Resolve items: explicit product_ids first, then articles, then split query.
    # Format: (query, qty, matched_part, confidence)
    items_to_add = []

    if params.get("product_ids"):
        for pid in params["product_ids"]:
            p = Part.objects.filter(id=pid).select_related("brand").first()
            if p:
                items_to_add.append((p.oem_number, quantity, p, 100))
            else:
                items_to_add.append((str(pid), quantity, None, 0))

    elif params.get("articles"):
        for art in params["articles"]:
            p = (
                Part.objects.select_related("brand")
                .filter(is_active=True)
                .filter(Q(oem_number__iexact=art) | Q(oem_number__icontains=art))
                .first()
            )
            items_to_add.append((art, quantity, p, _match_confidence(art, p)))

    elif params.get("query"):
        q = params["query"]
        articles = _extract_articles(q)
        if articles:
            for art in articles:
                p = (
                    Part.objects.select_related("brand")
                    .filter(is_active=True)
                    .filter(Q(oem_number__iexact=art) | Q(oem_number__icontains=art))
                    .first()
                )
                items_to_add.append((art, quantity, p, _match_confidence(art, p)))
        else:
            items_to_add.append((q[:255], quantity, None, 0))

    if not items_to_add:
        items_to_add = [("RFQ из чата", quantity, None, 0)]

    # Mode определяется классификатором согласно ТЗ §7.1/§7.2.
    # Критерии: matched_count, supplier_status (trusted/sandbox/risky),
    # KYB-верификация buyer'а, urgency, явный articles[] / params.mode.
    mode, classifier_reason = _classify_rfq_mode(items_to_add, user, params)

    # Build a short notes summary
    notes_parts = []
    if params.get("query") and len(items_to_add) == 1:
        notes_parts.append(f"Запрос: {params['query'][:300]}")
    notes_parts.append(f"Создано из чата · {len(items_to_add)} позиций")
    notes_parts.append(f"Mode: {classifier_reason}")

    try:
        rfq = RFQ.objects.create(
            created_by=user,
            customer_name=user.get_full_name() or user.username,
            customer_email=user.email or f"{user.username}@chat.local",
            company_name="",
            mode=mode,
            urgency="standard",
            status="new",
            notes=" | ".join(notes_parts)[:5000],
        )
        for query_str, qty, matched_part, confidence in items_to_add:
            RFQItem.objects.create(
                rfq=rfq,
                query=str(query_str)[:255],
                quantity=qty,
                matched_part=matched_part,
                state=("matched" if matched_part and confidence >= 80
                       else "needs_review" if matched_part else "new"),
                confidence=confidence,
            )
    except Exception as e:
        logger.exception("create_rfq failed")
        return ActionResult(text=f"⚠️ Не удалось создать RFQ: {e}")

    # AUTO/SEMI: сразу автоматически рассылаем поставщикам и собираем КП.
    # MANUAL: ждём оператора (op_dispatch_manual_rfq).
    auto_sent_count = 0
    if mode in ("auto", "semi"):
        try:
            from .negotiation import send_rfq_to_suppliers
            r = send_rfq_to_suppliers(
                {"rfq_id": rfq.id, "confirmed": True}, user, role,
            )
            # send_rfq_to_suppliers вернёт текст «✓ RFQ #N разослан K поставщикам»
            import re as _re
            m = _re.search(r"разослан (\d+)", r.text or "")
            if m:
                auto_sent_count = int(m.group(1))
        except Exception:
            logger.exception("auto-send on create_rfq failed")

    # SEMI: уведомляем оператора, что нужен approve в 15 минут
    if mode == "semi":
        try:
            from django.contrib.auth import get_user_model
            User = get_user_model()
            ops = User.objects.filter(is_staff=True, is_active=True)[:5]
            for op in ops:
                _notify(
                    op, kind="rfq",
                    title=f"⏱ SEMI RFQ #{rfq.id} — нужен approve (15 мин)",
                    body=f"Buyer {user.username} создал SEMI-RFQ. "
                         f"Проверь КП и одобри/отклони.",
                    url=f"/chat/rfq/{rfq.id}/?source=semi-approve",
                )
        except Exception:
            logger.exception("SEMI operator notify failed")

    # MANUAL: уведомляем оператора, что нужен ручной dispatch (48h)
    if mode == "manual":
        try:
            from django.contrib.auth import get_user_model
            User = get_user_model()
            ops = User.objects.filter(is_staff=True, is_active=True)[:5]
            for op in ops:
                _notify(
                    op, kind="rfq",
                    title=f"📋 MANUAL RFQ #{rfq.id} — нужна ручная рассылка",
                    body=f"Buyer {user.username}: {len(items_to_add)} позиций. "
                         f"Срок сбора КП — 48 часов.",
                    url=f"/chat/rfq/{rfq.id}/?source=manual-dispatch",
                )
        except Exception:
            logger.exception("MANUAL operator notify failed")

    matched_count = sum(1 for t in items_to_add if t[2] is not None)
    summary = f"{matched_count} из {len(items_to_add)} позиций сматчены с каталогом"

    if mode == "auto":
        text = (
            f"✓ RFQ #{rfq.id} создан · {len(items_to_add)} позиций. {summary}.\n"
            f"🤖 AUTO: запрос автоматически разослан {auto_sent_count} поставщикам.\n"
            f"📋 КП готовится — откройте, чтобы подтвердить и зарезервировать 10%."
        )
    elif mode == "semi":
        text = (
            f"✓ RFQ #{rfq.id} создан в SEMI режиме · {len(items_to_add)} позиций.\n"
            f"⏱ Расчёт готов, оператор подтвердит КП в течение 15 минут.\n"
            f"После approve вы получите инвойс с кнопкой резервирования."
        )
    else:  # manual
        text = (
            f"✓ RFQ #{rfq.id} создан в MANUAL режиме · {len(items_to_add)} позиций.\n"
            f"📋 Оператор вручную разошлёт запрос поставщикам и сформирует КП.\n"
            f"⏱ Срок сбора предложений — 48 часов."
        )

    # AUTO: сразу показываем КП-инвойс buyer'у с кнопкой
    # «Подтвердить и зарезервировать 10%».
    actions = []
    if mode == "auto":
        from marketplace.models import Quote as _Q
        if _Q.objects.filter(rfq=rfq, direction="seller_to_buyer", status="submitted").exists():
            actions.append({
                "action": "present_kp_to_buyer",
                "label": "📋 Открыть КП и подтвердить",
                "params": {"rfq_id": rfq.id},
            })

    return ActionResult(
        text=text,
        cards=[{
            "type": "rfq",
            "data": {
                "id": str(rfq.id),
                "number": rfq.id,
                "status": "new",
                "description": " · ".join(str(t[0]) for t in items_to_add[:5])[:200],
                "quantity": sum(int(t[1] or 1) for t in items_to_add),
                "created_at": rfq.created_at.strftime("%d.%m.%Y %H:%M"),
            },
        }],
        actions=actions,
        suggestions=["Мои активные RFQ", "Создать ещё RFQ"],
    )


@register("get_orders")
def get_orders(params, user, role):
    """List user's orders. params: {status?, limit?}"""
    from marketplace.models import Order
    limit = min(int(params.get("limit") or 5), 20)
    qs = Order.objects.select_related("buyer").order_by("-created_at")

    # Scope by role
    if role == "buyer":
        qs = qs.filter(buyer=user)
    elif role == "seller":
        # Seller sees orders containing their parts
        from marketplace.models import OrderItem
        seller_part_ids = list(user.parts.values_list("id", flat=True)) if hasattr(user, "parts") else []
        order_ids = OrderItem.objects.filter(part_id__in=seller_part_ids).values_list("order_id", flat=True).distinct()
        qs = qs.filter(id__in=order_ids)
    # Operators see all

    if params.get("status"):
        qs = qs.filter(status=params["status"])

    orders = list(qs[:limit])
    cards = [{
        "type": "order",
        "data": {
            "id": str(o.id),
            "number": f"ORD-{o.id}",
            "status": o.get_status_display() if hasattr(o, "get_status_display") else o.status,
            "status_code": o.status,
            "payment_status": o.payment_status,
            "total": float(o.total_amount or 0),
            "currency": "USD",
            "customer": o.customer_name or (o.buyer.get_full_name() if o.buyer else "—"),
            "created_at": o.created_at.strftime("%d.%m.%Y"),
            # Можно отменить если резерв ещё не списан и заказ свежий
            "can_cancel": (role == "buyer" and o.payment_status == "awaiting_reserve"),
        },
    } for o in orders]

    if not cards:
        return ActionResult(
            text="У вас пока нет заказов.",
            suggestions=["Найти запчасть", "Создать RFQ"],
        )

    return ActionResult(
        text=(
            f"Все недавние {len(cards)} заказов на платформе:" if role and role.startswith("operator")
            else (f"Заказы по вашим товарам · {len(cards)}:" if role == "seller"
                  else f"Ваши последние {len(cards)} заказа:")
        ),
        cards=cards,
        actions=[
            {"label": "Только в работе", "action": "get_orders",
             "params": {"status": "in_production"}},
            {"label": "Только оплаченные", "action": "get_orders",
             "params": {"status": "paid"}},
            {"label": "📦 Трекинг отгрузки", "action": "track_shipment", "params": {}},
            {"label": "💰 Бюджет за месяц", "action": "get_budget", "params": {}},
        ],
        suggestions=[],
    )


@register("get_order_detail")
def get_order_detail(params, user, role):
    """Полная карточка заказа: позиции, документы, доступные действия по
    статусу и роли (buyer/seller/operator).
    """
    from marketplace.models import Order
    oid = params.get("order_id") or params.get("id")
    if not oid:
        return ActionResult(text="⚠️ Не указан ID заказа")
    try:
        o = (Order.objects.select_related("buyer")
             .prefetch_related("items__part__brand", "documents").get(id=oid))
    except Order.DoesNotExist:
        return ActionResult(text=f"⚠️ Заказ #{oid} не найден")

    # Доступ
    is_seller = (role == "seller" and any(
        it.part and it.part.seller_id == user.id for it in o.items.all()
    ))
    is_buyer = (o.buyer_id == user.id)
    is_op = role.startswith("operator") or user.is_staff
    if not (is_buyer or is_seller or is_op):
        return ActionResult(text="Нет доступа к этому заказу.")

    # Позиции
    items_rows = []
    for it in o.items.all():
        if is_seller and (not it.part or it.part.seller_id != user.id):
            continue  # seller видит только свои позиции
        items_rows.append({
            "label": f"{it.part.oem_number if it.part else '—'} · {(it.part.title if it.part else '—')[:40]}",
            "value": f"× {it.quantity} = ${it.unit_price * it.quantity:,.2f}",
        })

    # Документы
    docs = list(o.documents.all().order_by("-created_at")[:10])
    doc_rows = [{
        "label": f"📄 {d.title}",
        "value": d.get_doc_type_display(),
    } for d in docs]

    rows = [
        {"label": "Заказ",            "value": f"ORD-{o.id}",                                "primary": True},
        {"label": "Статус",           "value": o.get_status_display()},
        {"label": "Оплата",           "value": o.get_payment_status_display()},
        {"label": "Сумма",            "value": f"${(o.total_amount or 0):,.2f}",            "primary": True},
        {"label": "Создан",           "value": o.created_at.strftime("%d.%m.%Y %H:%M") if o.created_at else "—"},
        {"label": "Покупатель",       "value": o.customer_name or "—"},
    ]
    if o.logistics_cost:
        rows.append({"label": "Логистика", "value": f"${o.logistics_cost:,.2f}"})
    if o.reserve_amount:
        rows.append({"label": "Резерв 10%", "value": f"${o.reserve_amount:,.2f}"})
    if items_rows:
        rows.append({"label": "─── Позиции ───", "value": ""})
        rows.extend(items_rows)
    if doc_rows:
        rows.append({"label": "─── Документы ───", "value": ""})
        rows.extend(doc_rows)

    # Действия зависят от роли + статуса
    actions = []
    # Документы — всем доступны (buyer/seller/operator)
    actions.append({"label": "📄 Все документы",
                     "action": "list_order_documents",
                     "params": {"order_id": o.id}})
    actions.append({"label": "🧾 Создать invoice",
                     "action": "generate_invoice_pdf",
                     "params": {"order_id": o.id}})
    # Seller-кнопки: pipeline
    if is_seller:
        if o.status == "reserve_paid":
            actions.append({"label": "▶️ Подтвердить и в производство",
                             "action": "advance_order",
                             "params": {"order_id": o.id}})
        elif o.status == "confirmed":
            actions.append({"label": "▶️ Запустить производство",
                             "action": "advance_order",
                             "params": {"order_id": o.id}})
        elif o.status == "in_production":
            actions.append({"label": "▶️ Готов к отгрузке",
                             "action": "advance_order",
                             "params": {"order_id": o.id}})
        elif o.status == "ready_to_ship":
            actions.append({"label": "🚚 Отгрузить",
                             "action": "ship_order",
                             "params": {"order_id": o.id}})
            actions.append({"label": "📦 Создать packing list",
                             "action": "generate_packing_list_pdf",
                             "params": {"order_id": o.id}})
            actions.append({"label": "✅ QC report",
                             "action": "generate_qc_report_pdf",
                             "params": {"order_id": o.id}})
        elif o.status in ("transit_abroad", "customs", "transit_rf", "issuing"):
            actions.append({"label": "▶️ Следующий этап",
                             "action": "advance_order",
                             "params": {"order_id": o.id}})
    # Buyer-кнопки
    if is_buyer:
        actions.append({"label": "📦 Трекинг",
                         "action": "track_shipment",
                         "params": {"order_id": o.id}})
        if o.payment_status == "reserve_paid" and o.status in ("ready_to_ship", "transit_abroad", "customs", "transit_rf", "issuing", "delivered"):
            actions.append({"label": "💳 Оплатить остаток 90%",
                             "action": "pay_final",
                             "params": {"order_id": o.id}})
        if o.status == "delivered":
            actions.append({"label": "✓ Подтвердить приёмку",
                             "action": "confirm_delivery",
                             "params": {"order_id": o.id}})

    return ActionResult(
        text=f"📋 Заказ ORD-{o.id} · {o.get_status_display()} · ${o.total_amount:,.2f}",
        cards=[{
            "type": "draft",
            "data": {
                "title": f"Заказ ORD-{o.id}",
                "rows": rows,
            },
        }],
        actions=actions,
    )


@register("track_shipment")
def track_shipment(params, user, role):
    from marketplace.models import Order
    oid = params.get("order_id")
    if not oid:
        # Show all in-transit orders
        return get_orders({"status": "transit_abroad", "limit": 5}, user, role)
    try:
        o = Order.objects.get(id=oid)
    except Order.DoesNotExist:
        return ActionResult(text=f"⚠️ Заказ #{oid} не найден")

    return ActionResult(
        text=f"Трекинг заказа ORD-{o.id} — статус: {o.get_status_display()}",
        cards=[{
            "type": "shipment",
            "data": {
                "order_id": str(o.id),
                "status": o.status,
                "status_label": o.get_status_display(),
                "stages": [
                    {"label": "Резерв оплачен", "done": o.status not in ("pending",)},
                    {"label": "В производстве", "done": o.status in ("in_production", "ready_to_ship", "transit_abroad", "customs", "transit_rf", "issuing", "shipped", "delivered", "completed")},
                    {"label": "Транзит", "done": o.status in ("customs", "transit_rf", "issuing", "shipped", "delivered", "completed")},
                    {"label": "Таможня", "done": o.status in ("transit_rf", "issuing", "shipped", "delivered", "completed")},
                    {"label": "Доставлен", "done": o.status in ("delivered", "completed")},
                ],
            },
        }],
        suggestions=["Все заказы в пути", "Открыть карту"],
    )


@register("get_rfq_status")
def get_rfq_status(params, user, role):
    from marketplace.models import RFQ
    rfq_id = params.get("rfq_id")
    if rfq_id:
        try:
            rfq = RFQ.objects.get(id=rfq_id)
        except RFQ.DoesNotExist:
            return ActionResult(text=f"⚠️ RFQ #{rfq_id} не найден")
        return ActionResult(
            text=f"RFQ #{rfq.id} — {rfq.get_status_display() if hasattr(rfq,'get_status_display') else rfq.status}",
            cards=[{
                "type": "rfq",
                "data": {
                    "id": str(rfq.id),
                    "number": rfq.id,
                    "status": rfq.status,
                    "description": (rfq.notes or "")[:200],
                    "customer": rfq.customer_name,
                    "created_at": rfq.created_at.strftime("%d.%m.%Y"),
                },
            }],
            actions=[
                {"label": "Открыть страницу RFQ", "action": "open_url",
                 "params": {"_url": f"/chat/rfq/{rfq.id}/"}},
            ],
        )
    # List active RFQs
    qs = RFQ.objects.order_by("-created_at")
    if role == "buyer":
        qs = qs.filter(buyer=user) if hasattr(RFQ, "buyer") else qs.filter(customer_email=user.email)
    rfqs = list(qs[:5])
    cards = [{
        "type": "rfq",
        "data": {
            "id": str(r.id),
            "number": r.id,
            "status": r.status,
            "description": (r.notes or "")[:120],
            "created_at": r.created_at.strftime("%d.%m.%Y"),
        },
    } for r in rfqs]
    return ActionResult(
        text=f"Найдено {len(cards)} RFQ:" if cards else "У вас нет активных RFQ.",
        cards=cards,
    )


@register("get_budget")
def get_budget(params, user, role):
    from marketplace.models import Order
    qs = Order.objects.filter(buyer=user) if role == "buyer" else Order.objects.all()
    total_paid = sum(float(o.total_amount or 0) for o in qs.filter(status__in=["paid", "completed", "delivered"]))
    total_pending = sum(float(o.total_amount or 0) for o in qs.exclude(status__in=["paid", "completed", "delivered", "cancelled"]))
    return ActionResult(
        text=f"Бюджет: оплачено ${total_paid:,.0f}, в работе ${total_pending:,.0f}",
        cards=[{
            "type": "chart",
            "data": {
                "title": "Расходы",
                "items": [
                    {"label": "Оплачено", "value": total_paid, "color": "#22c55e"},
                    {"label": "В работе", "value": total_pending, "color": "#6366f1"},
                ],
            },
        }],
        suggestions=["Отчёт за месяц", "Топ поставщики"],
    )


@register("get_analytics")
def get_analytics(params, user, role):
    """Расширенная аналитика покупателя/площадки с графиками-барами."""
    from collections import defaultdict
    from datetime import timedelta
    from django.utils import timezone

    from marketplace.models import RFQ, Order

    from .seller_actions import _effective_seller
    now = timezone.now()
    if role and role.startswith("operator"):
        qs = Order.objects.all()
    elif role == "seller":
        # Для seller — заказы где есть его позиции (items.part.seller).
        eff = _effective_seller(user)
        qs = Order.objects.filter(items__part__seller=eff).distinct()
    else:
        qs = Order.objects.filter(buyer=user)

    total_orders = qs.count()
    in_flight = qs.exclude(status__in=("delivered", "completed", "cancelled")).count()
    delivered = qs.filter(status__in=("delivered", "completed")).count()
    cancelled = qs.filter(status="cancelled").count()
    total_gmv = float(sum((o.total_amount or 0) for o in qs))
    avg_check = (total_gmv / total_orders) if total_orders else 0

    # Распределение по статусам
    by_status = defaultdict(int)
    for o in qs:
        by_status[o.get_status_display() if o.status else "—"] += 1
    status_items = sorted(by_status.items(), key=lambda x: -x[1])[:6]
    max_val = max((v for _, v in status_items), default=1)

    # Динамика по месяцам (последние 6)
    months = []
    for i in range(5, -1, -1):
        m_start = (now - timedelta(days=30 * (i + 1)))
        m_end = (now - timedelta(days=30 * i))
        cnt = qs.filter(created_at__gte=m_start, created_at__lt=m_end).count()
        months.append({"label": m_end.strftime("%b"), "value": cnt})

    # ── Аналитические метрики ──
    # Тренд GMV: последние 30д vs предыдущие 30д
    last_30 = qs.filter(created_at__gte=now - timedelta(days=30))
    prev_30 = qs.filter(created_at__gte=now - timedelta(days=60),
                         created_at__lt=now - timedelta(days=30))
    gmv_last = float(sum((o.total_amount or 0) for o in last_30))
    gmv_prev = float(sum((o.total_amount or 0) for o in prev_30))
    n_last = last_30.count()
    if gmv_prev:
        gmv_delta_pct = int((gmv_last - gmv_prev) * 100 / gmv_prev)
    elif gmv_last:
        gmv_delta_pct = 100
    else:
        gmv_delta_pct = 0
    arrow = "↑" if gmv_delta_pct > 0 else ("↓" if gmv_delta_pct < 0 else "→")
    gmv_tone = "ok" if gmv_delta_pct > 5 else ("bad" if gmv_delta_pct < -10 else "info")

    # Conversion: delivered / total
    delivery_pct = int(delivered * 100 / total_orders) if total_orders else 0
    deliv_tone = "ok" if delivery_pct >= 70 else ("warn" if delivery_pct >= 40 else "bad")
    # Cancel rate
    cancel_pct = int(cancelled * 100 / total_orders) if total_orders else 0
    cancel_tone = "bad" if cancel_pct > 15 else ("warn" if cancel_pct > 5 else "ok")
    # Топ-статус «зависания»
    top_status = status_items[0] if status_items else None

    scope_label = "по платформе" if role and role.startswith("operator") else "по вашим заказам"

    # Текст-инсайт: приоритет — тренд, провал доставки, отмены, норма
    text_parts = []
    if gmv_delta_pct <= -20 and gmv_prev:
        text_parts.append(f"⚠️ GMV просел: {arrow}{abs(gmv_delta_pct)}% к прошлым 30д (${gmv_last:,.0f} vs ${gmv_prev:,.0f}).")
    elif delivery_pct < 40 and total_orders >= 5:
        text_parts.append(f"⚠️ Низкая доходимость: только {delivery_pct}% заказов закрываются доставкой.")
    elif cancel_pct > 15:
        text_parts.append(f"⚠️ Высокий % отмен: {cancel_pct}% ({cancelled} из {total_orders}).")
    elif gmv_delta_pct >= 15:
        text_parts.append(f"📈 GMV растёт: {arrow}{gmv_delta_pct}% к прошлым 30д (${gmv_last:,.0f}).")
    else:
        text_parts.append(f"📊 Аналитика {scope_label}: GMV ${total_gmv:,.0f}, средний чек ${avg_check:,.0f}.")
    if top_status and in_flight > 3:
        text_parts.append(f"🔎 Основная масса активных — «{top_status[0]}» ({top_status[1]}).")

    return ActionResult(
        text="\n".join(text_parts),
        cards=[
            {"type": "kpi_grid", "data": {
                "title": f"📊 Ключевые цифры · {scope_label}",
                "items": [
                    {"label": "GMV всего",        "value": f"${total_gmv:,.0f}",
                     "sub":   "оборот за весь период", "tone": "info"},
                    {"label": "Средний чек",      "value": f"${avg_check:,.0f}",
                     "sub":   f"по {total_orders} заказам"},
                    {"label": "GMV тренд 30д",    "value": f"{arrow} {abs(gmv_delta_pct)}%",
                     "sub":   f"${gmv_last:,.0f} vs ${gmv_prev:,.0f}", "tone": gmv_tone},
                    {"label": "Доходимость",      "value": f"{delivery_pct}%",
                     "sub":   f"{delivered} доставлено", "tone": deliv_tone},
                    {"label": "% отмен",          "value": f"{cancel_pct}%",
                     "sub":   f"{cancelled} отменено", "tone": cancel_tone},
                    {"label": "Новых 30д",        "value": str(n_last),
                     "sub":   "темп заказов", "tone": "info"},
                ],
            }},
            {"type": "bar_chart", "data": {
                "title": "📈 Заказы по месяцам (6 мес)",
                "items": months,
                "color": "#64B5F6",
            }},
            {"type": "bar_chart", "data": {
                "title": "📋 Распределение по статусам",
                "items": [{"label": k, "value": v} for k, v in status_items],
                "color": "#81C784",
            }},
        ],
        contextual_actions=[
            {"action": "get_orders",        "label": "📦 Все заказы"},
            {"action": "get_supply_report", "label": "🚚 Отчёт по поставкам"},
            {"action": "get_sla_report",    "label": "⏱ SLA-отчёт"},
        ],
    )


@register("get_supply_report")
def get_supply_report(params, user, role):
    """Отчёт по поставкам: что в пути, ETA, по странам отправления, риски SLA.

    Закрывает жалобу: «не формирует отчёты по поставкам». Pure-DB, без LLM.
    """
    from collections import defaultdict
    from datetime import timedelta
    from django.utils import timezone

    from marketplace.models import Order, OrderItem
    now = timezone.now()

    # Только заказы в активной доставке
    in_transit_statuses = ("ready_to_ship", "transit_abroad", "customs",
                            "transit_rf", "issuing")
    qs = Order.objects.filter(status__in=in_transit_statuses)
    if role and role.startswith("operator"):
        pass  # all orders
    elif role == "seller":
        # Для seller — заказы где есть его позиции (через part.seller)
        from .seller_actions import _effective_seller
        eff = _effective_seller(user)
        qs = qs.filter(items__part__seller=eff).distinct()
    else:
        qs = qs.filter(buyer=user)
    orders = list(qs.select_related("buyer").order_by("status", "-created_at"))

    if not orders:
        return ActionResult(
            text="🚚 Нет заказов в активной поставке. Все доставлены или ещё не запущены.",
            contextual_actions=[
                {"action": "get_orders", "label": "📦 Все заказы"},
            ],
        )

    # Группировка по статусу
    by_status = defaultdict(list)
    for o in orders:
        by_status[o.status].append(o)
    status_label = {
        "ready_to_ship":  "📦 Готов к отгрузке",
        "transit_abroad": "🛫 Транзит за рубеж",
        "customs":        "🛃 На таможне",
        "transit_rf":     "🚛 Транзит по РФ",
        "issuing":        "📬 Выдача",
    }
    status_eta = {  # типичный остаток дней
        "ready_to_ship": 5, "transit_abroad": 14, "customs": 5,
        "transit_rf": 7, "issuing": 2,
    }

    # KPI
    total = len(orders)
    total_value = sum(float(o.total_amount or 0) for o in orders)
    breached = sum(1 for o in orders if o.sla_status == "breached")
    at_risk = sum(1 for o in orders if o.sla_status == "at_risk")

    # ── Аналитика ──
    # Среднее время в текущем этапе
    avg_age_days = (sum((now - o.created_at).days for o in orders) / total) if total else 0
    # Средний чек в поставке
    avg_shipment = (total_value / total) if total else 0
    # Доля бутылочного горлышка: где больше всего застряло
    biggest = max(by_status.items(), key=lambda x: len(x[1])) if by_status else None
    biggest_share = int(len(biggest[1]) * 100 / total) if (biggest and total) else 0
    # Money at risk: сумма заказов с at_risk/breached SLA
    at_risk_value = sum(float(o.total_amount or 0) for o in orders
                         if o.sla_status in ("breached", "at_risk"))
    # SLA здоровье
    sla_healthy_pct = int((total - breached - at_risk) * 100 / total) if total else 100

    # Bar-chart по статусам
    chart_items = []
    for st, lbl in status_label.items():
        if st in by_status:
            chart_items.append({"label": lbl, "value": len(by_status[st])})

    # Топ-10 заказов с ETA
    rows = []
    for o in orders[:10]:
        days_left = status_eta.get(o.status, 0)
        eta = (now + timedelta(days=days_left)).strftime("%d.%m")
        tone = "bad" if o.sla_status == "breached" else (
            "warn" if o.sla_status == "at_risk" else "ok")
        items_n = OrderItem.objects.filter(order=o).count()
        rows.append({
            "title": f"ORD-{o.id} · {o.customer_name or o.buyer.username}",
            "subtitle": (
                f"{status_label.get(o.status, o.status)} · "
                f"{items_n} поз · ${float(o.total_amount or 0):,.0f} · "
                f"ETA ~{eta} ({days_left}д)"
            ),
            "tone": tone,
            "action": "get_order_detail",
            "params": {"order_id": o.id},
        })

    # Текст-инсайт по приоритету
    text_parts = []
    if breached:
        text_parts.append(f"🔴 Срочно: {breached} заказов с SLA-нарушением на ${at_risk_value:,.0f}.")
    elif at_risk >= 3:
        text_parts.append(f"⚠️ {at_risk} заказов под угрозой SLA — проверьте этапы.")
    elif biggest and biggest_share > 50:
        text_parts.append(f"🔎 Бутылочное горлышко: {biggest_share}% в этапе «{status_label.get(biggest[0], biggest[0])}».")
    else:
        text_parts.append(f"🚚 В поставке {total} заказов на ${total_value:,.0f}, SLA здоров на {sla_healthy_pct}%.")

    return ActionResult(
        text="\n".join(text_parts),
        cards=[
            {"type": "kpi_grid", "data": {
                "title": "🚚 Аналитика поставок",
                "items": [
                    {"label": "Сумма в пути",    "value": f"${total_value:,.0f}", "tone": "info"},
                    {"label": "Средний чек",     "value": f"${avg_shipment:,.0f}",
                     "sub": f"по {total} заказам"},
                    {"label": "Деньги под риском","value": f"${at_risk_value:,.0f}",
                     "tone": "bad" if breached else ("warn" if at_risk else "ok"),
                     "sub": f"{breached + at_risk} заказов"},
                    {"label": "SLA здоровье",    "value": f"{sla_healthy_pct}%",
                     "tone": "ok" if sla_healthy_pct >= 80 else ("warn" if sla_healthy_pct >= 60 else "bad")},
                    {"label": "Средн. возраст",  "value": f"{avg_age_days:.0f} дн",
                     "sub": "в текущем этапе",
                     "tone": "warn" if avg_age_days > 14 else "info"},
                    {"label": "Узкое место",     "value": (status_label.get(biggest[0], biggest[0]).split(" ", 1)[-1] if biggest else "—"),
                     "sub": f"{biggest_share}% объёма" if biggest else "",
                     "tone": "warn" if biggest_share > 50 else "info"},
                ],
            }},
            {"type": "bar_chart", "data": {
                "title": "📊 Распределение по этапам",
                "items": chart_items,
                "color": "#FFB74D",
            }},
            {"type": "list", "data": {
                "title": f"📋 Заказы в поставке (топ {len(rows)})",
                "items": rows,
            }},
        ],
        contextual_actions=[
            {"action": "get_orders",     "label": "📦 Все заказы"},
            {"action": "get_analytics",  "label": "📊 Общая аналитика"},
            {"action": "get_sla_report", "label": "⏱ SLA-отчёт"},
        ],
    )


@register("compare_products")
def compare_products(params, user, role):
    from marketplace.models import Part
    ids = params.get("product_ids") or []
    parts = list(Part.objects.filter(id__in=ids).select_related("brand", "category"))
    if len(parts) < 2:
        return ActionResult(text="Для сравнения нужно минимум 2 товара.")
    return ActionResult(
        text=f"Сравнение {len(parts)} товаров:",
        cards=[{
            "type": "comparison",
            "data": {
                "headers": ["Артикул", "Бренд", "Цена", "В наличии"],
                "rows": [
                    [p.oem_number, p.brand.name if p.brand else "—",
                     f"${p.price}" if p.price else "—",
                     "✓" if getattr(p, "stock_qty", 0) > 0 else "—"]
                    for p in parts
                ],
            },
        }],
    )


# ══════════════════════════════════════════════════════════
# Buyer-anonymity: имена поставщиков скрыты до акцепта котировки
# ══════════════════════════════════════════════════════════

def _is_buyer_view(role: str) -> bool:
    """Buyer не должен видеть реальные имена поставщиков, чтобы не обходить
    платформу. Имена раскрываются только в Quote после accept_quote.
    Operator/admin/seller видят настоящие.
    """
    return role == "buyer"


def _anonymize_supplier(s: dict, idx: int) -> dict:
    """Скрывает name/email/identifying fields, оставляя метрики и рейтинг."""
    safe = dict(s)
    safe["name"] = f"Поставщик №{idx + 1}"
    # Удаляем потенциально идентифицирующие поля
    for k in ("email", "phone", "company_name", "username", "legal_name", "inn"):
        safe.pop(k, None)
    safe["anonymous"] = True
    return safe


def _maybe_anonymize_suppliers(suppliers: list[dict], role: str) -> list[dict]:
    if not _is_buyer_view(role):
        return suppliers
    return [_anonymize_supplier(s, i) for i, s in enumerate(suppliers)]


@register("compare_suppliers")
def compare_suppliers(params, user, role):
    from django.contrib.auth.models import User
    # related_name = 'profile' (см. marketplace.UserProfile), а не 'userprofile'
    sellers = list(User.objects.filter(profile__role="seller")[:5])
    if _is_buyer_view(role):
        # Для buyer — анонимизируем: только rank + рейтинг, без имени и email
        rows = [
            [f"Поставщик №{i + 1}", "—"]
            for i, _ in enumerate(sellers)
        ]
    else:
        rows = [[s.get_full_name() or s.username, s.email or "—"] for s in sellers]
    return ActionResult(
        text=f"Топ поставщиков ({len(sellers)}):" + (
            "\n💡 Имена скрыты — раскрываются после принятия котировки." if _is_buyer_view(role) else ""
        ),
        cards=[{
            "type": "comparison",
            "data": {
                "headers": ["Поставщик", "Email"],
                "rows": rows,
            },
        }],
    )


def _seller_rating(seller) -> dict:
    """Достаёт rating/status поставщика. UserProfile может отсутствовать —
    возвращаем дефолты sandbox/60."""
    try:
        prof = seller.profile
        return {
            "rating": float(prof.rating_score or 60),
            "status": prof.supplier_status or "sandbox",
            "external": float(prof.external_score or 60),
            "behavioral": float(prof.behavioral_score or 60),
        }
    except Exception:
        return {"rating": 60.0, "status": "sandbox",
                "external": 60.0, "behavioral": 60.0}


def _status_badge(status: str) -> str:
    return {
        "trusted":  "🟢 Надёжный",
        "sandbox":  "🟡 Песочница",
        "risky":    "🟠 Рисковый",
        "rejected": "🔴 Исключён",
    }.get(status, status)


def _rank_offers(offers: list[dict],
                  price_weight: float = 0.5,
                  rating_weight: float = 0.5) -> list[dict]:
    """Считает score: дешевле + выше рейтинг = выше score.

    score = (1 - norm_price) * w_price + (rating/100) * w_rating
    norm_price = (price - min) / (max - min) — линейная нормализация.
    Для единственного оффера score = rating/100.
    """
    if not offers:
        return []
    prices = [o["price"] for o in offers if o.get("price") and o["price"] > 0]
    if not prices:
        return offers
    pmin, pmax = min(prices), max(prices)
    span = max(pmax - pmin, 1e-9)
    for o in offers:
        price = o.get("price") or pmax
        norm = (price - pmin) / span if pmax > pmin else 0.0
        rating = (o.get("rating") or 60.0) / 100.0
        o["score"] = round(
            (1 - norm) * price_weight + rating * rating_weight, 4
        )
    return sorted(offers, key=lambda x: x["score"], reverse=True)


@register("buyer_best_offers")
def buyer_best_offers(params, user, role):
    """Поиск лучших предложений по OEM/названию среди ВСЕХ продавцов.

    params: {query: str, limit?: int (default 8)}

    Группирует Part по oem_number, оставляет лучший оффер от каждого
    продавца (минимальная цена среди их позиций), ранжирует по score
    (50% цена + 50% rating). Возвращает топ-N с анонимизацией для buyer.
    """
    from marketplace.models import Part
    query = (params.get("query") or "").strip()
    limit = min(int(params.get("limit") or 8), 30)
    if not query:
        return ActionResult(
            text="Укажите OEM-номер или название детали для поиска.",
            suggestions=["6I-2502", "Engine oil filter", "Гидравлический насос"],
        )

    qs = (Part.objects
          .filter(is_active=True)
          .filter(Q(oem_number__icontains=query)
                  | Q(title__icontains=query)
                  | Q(cross_numbers__icontains=query))
          .select_related("seller", "seller__profile", "brand"))
    parts = list(qs[:200])
    if not parts:
        return ActionResult(
            text=f"По запросу «{query}» предложений не найдено.",
            actions=[{"label": "📝 Создать RFQ", "action": "create_rfq",
                       "params": {"query": query, "quantity": 1}}],
        )

    # Группируем: для каждой пары (oem_number, seller) — минимальная цена
    by_key: dict[tuple, dict] = {}
    for p in parts:
        key = ((p.oem_number or "").upper(), p.seller_id)
        price = float(p.price) if p.price else None
        existing = by_key.get(key)
        if existing and existing["price"] is not None and price is not None:
            if price >= existing["price"]:
                continue
        rating = _seller_rating(p.seller)
        by_key[key] = {
            "part_id": p.id,
            "oem_number": p.oem_number,
            "title": p.title,
            "brand": p.brand.name if p.brand else "—",
            "price": price,
            "currency": p.currency or "USD",
            "price_fob_sea": float(p.price_fob_sea) if p.price_fob_sea else None,
            "price_fob_air": float(p.price_fob_air) if p.price_fob_air else None,
            "sea_port": p.sea_port or "",
            "air_port": p.air_port or "",
            "warehouse": p.warehouse_address or "",
            "condition": p.condition or "",
            "availability": getattr(p, "availability", "") or "",
            "stock": getattr(p, "stock_quantity", 0) or 0,
            "seller_id": p.seller_id,
            "rating": rating["rating"],
            "status": rating["status"],
        }

    offers = _rank_offers(list(by_key.values()))[:limit]

    # Анонимизация для buyer — стабильный псевдоним по seller_id
    anon = _is_buyer_view(role)
    for i, o in enumerate(offers, 1):
        if anon:
            o["supplier_label"] = f"Поставщик #S{o['seller_id'] % 1000:03d}"
        else:
            try:
                u = next(p.seller for p in parts if p.seller_id == o["seller_id"])
                o["supplier_label"] = u.get_full_name() or u.username
            except StopIteration:
                o["supplier_label"] = f"Seller {o['seller_id']}"
        o["status_badge"] = _status_badge(o["status"])

    # Уникальные OEM для drill-down кнопок
    oems = sorted({o["oem_number"] for o in offers})

    actions = []
    if len(oems) == 1:
        actions.append({
            "label": "🔍 Все поставщики этой позиции",
            "action": "buyer_offer_compare",
            "params": {"oem_number": oems[0]},
        })
    actions.append({"label": "📝 Создать RFQ", "action": "create_rfq",
                     "params": {"query": query}})

    intro = (f"🛒 Топ {len(offers)} предложений по «{query}» "
             f"(ранжировано по цене + рейтингу поставщика)")
    if anon:
        intro += "\n💡 Имена скрыты до принятия котировки — виден только рейтинг."

    return ActionResult(
        text=intro,
        cards=[{
            "type": "best_offers",
            "data": {
                "title": "Лучшие предложения",
                "query": query,
                "rows": offers,
                "anonymous": anon,
            },
        }],
        actions=actions,
        suggestions=["Сравнить всех поставщиков", "Создать RFQ"],
    )


@register("buyer_offer_compare")
def buyer_offer_compare(params, user, role):
    """Drill-down: все продавцы, у кого есть конкретный OEM.

    params: {oem_number: str, dest_country?: str (default 'RU')}

    Показывает полную сравнительную таблицу с ценой EXW, FOB SEA/AIR,
    портами, складом, condition, наличием, рейтингом и статусом
    поставщика. Также рассчитывает доставку по весу/габаритам через
    `assistant.logistics.calc_logistics` для каждого оффера → буйер
    видит итоговую landed cost, а не только FOB.
    """
    from assistant.logistics import calc_logistics
    from marketplace.models import Part
    oem = (params.get("oem_number") or "").strip()
    dest_country = (params.get("dest_country") or "RU").upper()[:2]
    if not oem:
        return ActionResult(text="Не указан OEM-номер для сравнения.")

    qs = (Part.objects
          .filter(is_active=True, oem_number__iexact=oem)
          .select_related("seller", "seller__profile", "brand"))
    parts = list(qs)
    if not parts:
        return ActionResult(
            text=f"По OEM «{oem}» поставщиков нет.",
            actions=[{"label": "📝 Создать RFQ", "action": "create_rfq",
                       "params": {"query": oem, "quantity": 1}}],
        )

    offers = []
    for p in parts:
        rating = _seller_rating(p.seller)
        # Расчёт доставки sea+air → выбираем дешевле для итоговой landed
        ship_sea = calc_logistics(p, dest_country, "sea")
        ship_air = calc_logistics(p, dest_country, "air")
        best_ship = None
        for s in (ship_sea, ship_air):
            if s.get("cost") is not None:
                if best_ship is None or s["cost"] < best_ship["cost"]:
                    best_ship = s
        offers.append({
            "part_id": p.id,
            "seller_id": p.seller_id,
            "title": p.title,
            "brand": p.brand.name if p.brand else "—",
            "price": float(p.price) if p.price else None,
            "currency": p.currency or "USD",
            "price_fob_sea": float(p.price_fob_sea) if p.price_fob_sea else None,
            "price_fob_air": float(p.price_fob_air) if p.price_fob_air else None,
            "sea_port": p.sea_port or "",
            "air_port": p.air_port or "",
            "warehouse": p.warehouse_address or "",
            "condition": p.condition or "",
            "availability": getattr(p, "availability", "") or "",
            "stock": getattr(p, "stock_quantity", 0) or 0,
            "rating": rating["rating"],
            "status": rating["status"],
            # Логистика
            "ship_sea_cost": float(ship_sea["cost"]) if ship_sea.get("cost") else None,
            "ship_air_cost": float(ship_air["cost"]) if ship_air.get("cost") else None,
            "ship_sea_days": ship_sea.get("transit_days"),
            "ship_air_days": ship_air.get("transit_days"),
            "ship_best_mode": best_ship.get("mode") if best_ship else None,
            "ship_best_cost": float(best_ship["cost"]) if best_ship and best_ship.get("cost") else None,
            "chargeable_kg": float(ship_sea.get("chargeable_kg") or 0) or None,
            # Итоговый landed cost = EXW + best shipping
            "landed_cost": (
                float((p.price or 0) + best_ship["cost"]) if (best_ship and best_ship.get("cost") and p.price) else None
            ),
        })

    offers = _rank_offers(offers)

    anon = _is_buyer_view(role)
    for o in offers:
        if anon:
            o["supplier_label"] = f"Поставщик #S{o['seller_id'] % 1000:03d}"
        else:
            try:
                u = next(p.seller for p in parts if p.seller_id == o["seller_id"])
                o["supplier_label"] = u.get_full_name() or u.username
            except StopIteration:
                o["supplier_label"] = f"Seller {o['seller_id']}"
        o["status_badge"] = _status_badge(o["status"])

    # Подсказки: дешевле/рисковее vs. дороже/надёжнее
    cheapest = min((o for o in offers if o["price"]),
                    key=lambda o: o["price"], default=None)
    safest = max(offers, key=lambda o: o["rating"], default=None)

    insight_lines = []
    if cheapest:
        insight_lines.append(
            f"💰 Самый дешёвый: {cheapest['supplier_label']} · "
            f"{cheapest['price']:.2f} {cheapest['currency']} · "
            f"{cheapest['status_badge']} (рейтинг {cheapest['rating']:.0f})"
        )
    if safest and safest != cheapest:
        insight_lines.append(
            f"🛡️ Самый надёжный: {safest['supplier_label']} · "
            f"{safest['price']:.2f} {safest['currency'] if safest['price'] else ''} · "
            f"{safest['status_badge']} (рейтинг {safest['rating']:.0f})"
        )

    intro = (f"🔍 Сравнение {len(offers)} поставщиков по OEM «{oem}»\n"
             + "\n".join(insight_lines))
    if anon:
        intro += "\n💡 Имена скрыты — виден только рейтинг и статус."

    return ActionResult(
        text=intro,
        cards=[{
            "type": "offer_compare",
            "data": {
                "title": f"OEM {oem} — все поставщики",
                "oem_number": oem,
                "rows": offers,
                "anonymous": anon,
            },
        }],
        actions=[
            {"label": "📝 Создать RFQ", "action": "create_rfq",
             "params": {"query": oem, "quantity": 1}},
            {"label": "↩️ Назад к поиску", "action": "buyer_best_offers",
             "params": {"query": oem}},
        ],
        suggestions=["Создать RFQ", "Показать больше"],
    )


@register("calc_part_logistics")
def calc_part_logistics(params, user, role):
    """Калькулятор доставки одной позиции по весу/габаритам.

    params: {part_id: int, dest_country?: str (default 'RU'), mode?: 'sea'|'air'}
    Без mode — считаем оба и показываем сравнение.
    """
    from assistant.logistics import calc_logistics
    from marketplace.models import Part
    try:
        pid = int(params.get("part_id") or 0)
    except (ValueError, TypeError):
        return ActionResult(text="Неверный part_id.")
    if not pid:
        return ActionResult(text="Не указан part_id.")
    try:
        p = Part.objects.select_related("brand").get(id=pid, is_active=True)
    except Part.DoesNotExist:
        return ActionResult(text="Позиция не найдена.")

    dest = (params.get("dest_country") or "RU").upper()[:2]
    mode = params.get("mode")
    modes = [mode] if mode in ("sea", "air") else ["sea", "air"]
    results = {m: calc_logistics(p, dest, m) for m in modes}

    err_map = {
        "no_origin_port": "Не указан порт отправления",
        "no_dest_country": "Не указана страна назначения",
        "no_weight_or_dims": "Нет данных о весе/габаритах позиции",
        "no_tariff": f"Тариф для маршрута → {dest} не настроен",
    }
    lines = [f"🚚 Расчёт доставки **{p.oem_number}** ({p.title[:40]}) → {dest}"]
    for m, r in results.items():
        m_label = "🚢 Море" if m == "sea" else "✈️ Авиа"
        if r["cost"] is None:
            lines.append(f"{m_label}: — ({err_map.get(r['error'], r['error'])})")
        else:
            lines.append(
                f"{m_label}: **${r['cost']}** · "
                f"{r['chargeable_kg']:.2f} кг ({r['actual_kg']:.1f} факт / "
                f"{r['volumetric_kg']:.1f} объём) · "
                f"~{r['transit_days']} дн."
            )
    return ActionResult(
        text="\n".join(lines),
        suggestions=["Создать RFQ", "Сравнить поставщиков"],
    )


@register("get_demand_report")
def get_demand_report(params, user, role):
    """Дашборд «Спрос на рынке» — что покупатели запрашивают через RFQ,
    где у поставщика дыры в каталоге, какие бренды/OEM в топе.

    Для seller: персонализирован (выделяет позиции, которых нет в каталоге).
    Для operator/admin: глобальная сводка по рынку.
    """
    from collections import Counter
    from datetime import timedelta
    from django.db.models import Count, Sum
    from django.utils import timezone

    from marketplace.models import RFQ, RFQItem, Part

    from .seller_actions import _effective_seller
    seller = _effective_seller(user) if role == "seller" else None

    now = timezone.now()
    last_30 = now - timedelta(days=30)
    last_7  = now - timedelta(days=7)

    # ── Базовые метрики ─────────────────────────────────────
    active_rfq_qs = RFQ.objects.exclude(status__in=("closed", "cancelled"))
    open_rfq      = active_rfq_qs.count()
    new_7d        = RFQ.objects.filter(created_at__gte=last_7).count()
    new_30d       = RFQ.objects.filter(created_at__gte=last_30).count()
    items_30d_qs  = RFQItem.objects.filter(rfq__created_at__gte=last_30)
    total_items   = items_30d_qs.count()
    unique_oem    = items_30d_qs.values("query").distinct().count()
    avg_qty       = round((items_30d_qs.aggregate(s=Sum("quantity"))["s"] or 0)
                          / max(total_items, 1), 1)

    # ── Топ-бренды по объёму запросов ───────────────────────
    brand_counter = Counter()
    for it in items_30d_qs.select_related("matched_part", "matched_part__brand")[:1000]:
        brand = (it.matched_part.brand.name
                  if it.matched_part_id and it.matched_part.brand_id else "Без бренда")
        brand_counter[brand] += it.quantity or 1
    top_brands = brand_counter.most_common(8)
    max_brand_val = max((v for _, v in top_brands), default=1)

    # ── Топ-OEM (артикулы) ──────────────────────────────────
    oem_counter = Counter()
    for it in items_30d_qs[:1000]:
        oem_counter[it.query] = oem_counter.get(it.query, 0) + (it.quantity or 1)
    top_oem = oem_counter.most_common(10)

    # ── Категории/бренды где у поставщика нет позиций ───────
    coverage_lines = []
    if seller:
        my_brands = set(
            Part.objects.filter(seller=seller, is_active=True, brand__isnull=False)
            .values_list("brand__name", flat=True).distinct()
        )
        missing_demand = [(b, v) for b, v in top_brands if b not in my_brands and b != "Без бренда"]
        for b, v in missing_demand[:5]:
            coverage_lines.append({
                "title":    f"📈 {b} — {v} запросов / 30 дней",
                "subtitle": "В вашем каталоге нет этого бренда — упускаете спрос",
                "action":   "upload_pricelist", "params": {},
            })

    # ── Динамика по неделям (4 недели) ──────────────────────
    weeks = []
    for i in range(3, -1, -1):
        w_start = now - timedelta(days=7 * (i + 1))
        w_end   = now - timedelta(days=7 * i)
        cnt = RFQ.objects.filter(created_at__gte=w_start, created_at__lt=w_end).count()
        label = w_end.strftime("%d.%m")
        weeks.append({"label": label, "value": cnt})
    max_week = max((w["value"] for w in weeks), default=1)

    # ── Аналитические метрики ──
    # Тренд: 7д vs средний по 30д
    expected_weekly = new_30d / 4 if new_30d else 0
    if expected_weekly:
        week_delta = int((new_7d - expected_weekly) * 100 / expected_weekly)
    else:
        week_delta = 0
    arrow_w = "↑" if week_delta > 0 else ("↓" if week_delta < 0 else "→")
    week_tone = "ok" if week_delta > 10 else ("bad" if week_delta < -20 else "info")
    # Концентрация спроса: какая доля у топ-3 брендов
    top3_share = 0
    if top_brands:
        total_brand_v = sum(v for _, v in top_brands) or 1
        top3_share = int(sum(v for _, v in top_brands[:3]) * 100 / total_brand_v)
    # Доля повторных OEM (повторяемость) — индикатор регулярного спроса
    repeat_oem = sum(1 for _, c in oem_counter.items() if c > 1)
    repeat_pct = int(repeat_oem * 100 / max(unique_oem, 1))
    # Coverage gap (для seller)
    gap_n = len(coverage_lines) if seller else 0

    hero_kpis = [
        {"label": "Темп 7д vs средний",  "value": f"{arrow_w} {abs(week_delta)}%",
         "sub": f"{new_7d} / норма {expected_weekly:.0f}", "tone": week_tone},
        {"label": "Концентрация (топ-3)", "value": f"{top3_share}%",
         "sub": "доля у 3 брендов-лидеров",
         "tone": "warn" if top3_share > 70 else "info"},
        {"label": "Повторяемость OEM",   "value": f"{repeat_pct}%",
         "sub": f"{repeat_oem} артикулов запросили >1",
         "tone": "ok" if repeat_pct > 30 else "info"},
        {"label": "Средн. позиций/RFQ",  "value": str(avg_qty)},
    ]
    if seller and gap_n:
        hero_kpis.append({"label": "Дыр в каталоге", "value": str(gap_n),
                           "sub": "топ-брендов не закрыты", "tone": "bad"})

    cards = [
        {"type": "kpi_grid", "data": {
            "title": "📈 Спрос на рынке — ключевые метрики (30 дней)",
            "items": hero_kpis,
        }},
    ]

    # Bar chart — динамика RFQ по неделям
    if max_week > 0:
        bar_rows = [{
            "label": w["label"],
            "value": w["value"],
            "pct":   round(w["value"] / max_week * 100) if max_week else 0,
        } for w in weeks]
        cards.append({"type": "bar_chart", "data": {
            "title":  "📊 RFQ в неделю (4 последних)",
            "rows":   bar_rows,
            "unit":   "RFQ",
        }})

    # Top-brands список
    if top_brands:
        brand_items = [{
            "title":    f"{b} · {v} позиций",
            "subtitle": f"доля {round(v/sum(c for _,c in top_brands)*100)}% от спроса",
            "badge":    {"label": str(v), "tone": "info"},
        } for b, v in top_brands]
        cards.append({"type": "list", "data": {
            "title": "🏷 Топ-бренды по объёму запросов",
            "items": brand_items,
        }})

    # Top-OEM
    if top_oem:
        oem_items = [{
            "title":    f"{oem} · {v} шт",
            "subtitle": "запросов за 30 дней",
        } for oem, v in top_oem]
        cards.append({"type": "list", "data": {
            "title": "🔢 Топ-OEM номера (за 30 дней)",
            "items": oem_items,
        }})

    # Coverage gaps — для seller
    if seller and coverage_lines:
        cards.append({"type": "list", "data": {
            "title": "🎯 Где вы упускаете спрос (нет в каталоге)",
            "items": coverage_lines,
        }})

    # ── Текст-инсайт по приоритету ─────────────────────────
    text_parts = []
    if seller and gap_n >= 3:
        text_parts.append(f"🎯 Срочно: {gap_n} топ-брендов есть в спросе, но нет в вашем каталоге — упускаете выручку.")
    elif week_delta <= -25 and new_30d >= 10:
        text_parts.append(f"📉 Спрос проседает: {arrow_w}{abs(week_delta)}% к норме (за неделю {new_7d}, норма {expected_weekly:.0f}).")
    elif week_delta >= 25:
        text_parts.append(f"📈 Спрос растёт: {arrow_w}{week_delta}% к норме (за неделю {new_7d}).")
    elif top3_share > 70 and top_brands:
        text_parts.append(f"🏷 Спрос концентрирован: топ-3 бренда дают {top3_share}% — лидер {top_brands[0][0]}.")
    else:
        text_parts.append(f"📈 Спрос стабилен: {new_30d} RFQ за 30 дней, {unique_oem} уникальных OEM.")

    text = "\n".join(text_parts)

    return ActionResult(
        text=text,
        cards=cards,
        actions=[
            {"label": "📤 Загрузить недостающие позиции",
             "action": "upload_pricelist", "params": {}},
            {"label": "📋 Открытые RFQ",
             "action": "get_rfq_status", "params": {}},
            {"label": "🔥 Срочное",
             "action": "seller_inbox", "params": {}},
        ],
        suggestions=["Топ запрашиваемых категорий"],
    )


@register("get_sla_report")
def get_sla_report(params, user, role):
    """SLA по заказам + среднее время на каждом этапе pipeline.
    Buyer видит свои заказы, seller — где он поставщик, operator/admin — глобально.
    """
    from collections import defaultdict
    from datetime import timedelta

    from django.utils import timezone

    from marketplace.models import Order, OrderEvent

    qs = Order.objects.all()
    scope_label = "по платформе"
    if role == "buyer":
        qs = qs.filter(buyer=user)
        scope_label = "по вашим заказам"
    elif role == "seller":
        from .seller_actions import _effective_seller
        eff = _effective_seller(user)
        qs = qs.filter(items__part__seller=eff).distinct()
        scope_label = "по вашим поставкам"

    breached = qs.filter(sla_status="breached").count()
    on_track = qs.filter(sla_status="on_track").count()
    at_risk  = qs.filter(sla_status="at_risk").count() if hasattr(Order, "sla_status") else 0
    total    = breached + on_track + at_risk
    on_track_pct = (on_track / total * 100) if total else None

    # Money at risk (для заказов в риске/нарушенных)
    risk_orders = list(qs.filter(sla_status__in=("at_risk", "breached"))[:500])
    money_at_risk = sum(float(o.total_amount or 0) for o in risk_orders)
    # Breach rate
    breach_pct = int(breached * 100 / total) if total else None
    # Health score — при отсутствии данных показываем «—», а не пугающий 0%.
    if on_track_pct is None:
        health_label, health_tone, health_sub = "—", "info", "нет данных"
    else:
        health_tone = "ok" if on_track_pct >= 80 else ("warn" if on_track_pct >= 60 else "bad")
        health_label = f"{on_track_pct:.0f}%"
        health_sub = f"{on_track}/{total} on-track"
    if breach_pct is None:
        breach_label, breach_tone, breach_sub = "—", "info", "нет заказов"
    else:
        breach_label = f"{breach_pct}%"
        breach_tone = "bad" if breach_pct > 10 else ("warn" if breach_pct > 0 else "ok")
        breach_sub = f"{breached} нарушено"
    items = [
        {"label": "SLA здоровье",  "value": health_label,
         "tone": health_tone, "sub": health_sub},
        {"label": "% нарушений",   "value": breach_label,
         "tone": breach_tone, "sub": breach_sub},
        {"label": "Деньги под риском", "value": f"${money_at_risk:,.0f}",
         "tone": "bad" if money_at_risk and breached else ("warn" if money_at_risk else "ok"),
         "sub": f"{breached + at_risk} заказов"},
    ]

    # ── Среднее время на каждом этапе pipeline ──────────────
    # Для каждого OrderEvent(status_changed): время от предыдущего status_changed
    # того же заказа = время проведённое в "from"-статусе. Усредняем по всем
    # заказам в текущем scope (qs).
    order_ids = list(qs.values_list("id", flat=True)[:500])  # лимит для прод
    events = list(
        OrderEvent.objects.filter(
            order_id__in=order_ids, event_type="status_changed"
        ).order_by("order_id", "created_at")
    )

    STAGE_LABELS = {
        "awaiting_reserve": "⏳ Ожидание резерва",
        "reserve_paid":     "💰 Резерв оплачен",
        "confirmed":        "✅ Подтверждено",
        "in_production":    "🏭 В производстве",
        "ready_to_ship":    "📦 Готов к отгрузке",
        "transit_abroad":   "🚢 Транзит за рубеж",
        "customs":          "🛃 Таможня",
        "transit_rf":       "🚛 Транзит по РФ",
        "issuing":          "📍 Выдача",
    }
    # SLA-нормативы по этапам (рабочие дни) — для сравнения
    STAGE_SLA_DAYS = {
        "awaiting_reserve": 2,
        "reserve_paid":     2,
        "confirmed":        1,
        "in_production":    7,
        "ready_to_ship":    2,
        "transit_abroad":   14,
        "customs":          5,
        "transit_rf":       7,
        "issuing":          3,
    }

    # dwell[stage] = list[seconds spent at stage]
    dwell = defaultdict(list)
    prev_ev = {}  # order_id → last event
    for ev in events:
        meta = ev.meta or {}
        from_status = meta.get("from") or (
            prev_ev.get(ev.order_id).meta.get("to")
            if prev_ev.get(ev.order_id) and prev_ev[ev.order_id].meta else None
        )
        prev = prev_ev.get(ev.order_id)
        if prev and from_status:
            delta = (ev.created_at - prev.created_at).total_seconds()
            if 0 < delta < 86400 * 90:  # игнорим аномальные >90 дней
                dwell[from_status].append(delta)
        prev_ev[ev.order_id] = ev

    stage_rows = []
    for status, label in STAGE_LABELS.items():
        durations = dwell.get(status, [])
        n_now = qs.filter(status=status).count()
        if not durations and n_now == 0:
            continue
        avg_sec = sum(durations) / len(durations) if durations else 0
        avg_days = avg_sec / 86400
        sla_days = STAGE_SLA_DAYS.get(status, 0)
        # tone: ok если в норме, warn если близко к лимиту, bad если превышено
        if not durations:
            tone = "info"
            avg_label = "—"
        else:
            avg_label = (f"{avg_days:.1f} дн" if avg_days >= 1 else
                          f"{avg_sec/3600:.1f} ч" if avg_sec >= 3600 else
                          f"{avg_sec/60:.0f} мин")
            tone = ("ok"   if avg_days <= sla_days * 0.8 else
                    "warn" if avg_days <= sla_days       else "bad") if sla_days else "info"
        stage_rows.append({
            "title": f"{label}",
            "subtitle": (
                f"среднее: {avg_label}" +
                (f" / SLA {sla_days} дн" if sla_days else "") +
                (f" · сейчас в этом этапе: {n_now} заказ" if n_now else "") +
                (f" · {len(durations)} переходов" if durations else "")
            ),
            "badge": {"label": avg_label, "tone": tone},
        })

    cards = [{"type": "kpi_grid",
              "data": {"title": f"⏱ SLA {scope_label}", "items": items}}]
    if stage_rows:
        cards.append({"type": "list", "data": {
            "title": "⏱ Среднее время на каждом этапе pipeline",
            "items": stage_rows,
        }})

    # ── Самые «застрявшие» заказы (на текущем этапе дольше SLA) ─
    now_dt = timezone.now()
    stuck = []
    for o in qs.exclude(status__in=("delivered", "completed", "cancelled")):
        sla = STAGE_SLA_DAYS.get(o.status, 0)
        if not sla:
            continue
        age = (now_dt - o.created_at).days
        if age > sla * 2:  # вдвое дольше норматива
            stuck.append((o, age, sla))
    stuck.sort(key=lambda x: -x[1])
    if stuck:
        items_stuck = [{
            "title":    f"Заказ #{o.id} · {o.customer_name[:30]}",
            "subtitle": (f"в статусе «{STAGE_LABELS.get(o.status, o.status)}» уже {age} дн "
                         f"(норматив {sla} дн)"),
            "badge":    {"label": f"+{age - sla}д", "tone": "bad"},
            "action":   "track_order",
            "params":   {"order_id": o.id},
        } for o, age, sla in stuck[:8]]
        cards.append({"type": "list", "data": {
            "title": f"🔴 Застрявшие заказы — {len(stuck)} превысили норматив",
            "items": items_stuck,
        }})

    # Текст-сводка
    text_parts = [f"⏱ SLA {scope_label}: на дорожке {on_track}, риск {at_risk}, нарушено {breached}."]
    if stage_rows:
        # самый медленный этап
        slow = max(((s["title"], s["badge"]["label"], s["badge"]["tone"])
                     for s in stage_rows if s["badge"]["tone"] != "info"),
                    key=lambda x: x[1] if x[2] == "bad" else "", default=None)
        if slow:
            text_parts.append(f"⚠️ Самый проблемный этап: {slow[0]} (среднее {slow[1]}).")
    if stuck:
        text_parts.append(f"🔴 {len(stuck)} заказов застряли — превысили норматив этапа более чем вдвое.")

    return ActionResult(
        text="\n".join(text_parts),
        cards=cards,
        actions=[
            {"label": "📊 Аналитика заказов", "action": "get_analytics",     "params": {}},
            ({"label": "💸 Экономия",         "action": "get_savings",       "params": {}}
              if role == "buyer" else
             {"label": "📦 Поставки",         "action": "get_supply_report", "params": {}}),
        ],
        contextual_actions=[{"action": "seller_analytics_hub" if role == "seller" else "support_home",
                              "label": "← Аналитика" if role == "seller" else "← Поддержка"}],
    )


@register("get_claims")
def get_claims(params, user, role):
    """Рекламации — информативная сводка для роли.

    Buyer  → свои рекламации
    Seller → рекламации по заказам, где есть его OrderItem
    Operator → все рекламации (модерация)
    """
    from decimal import Decimal

    from django.utils import timezone

    from marketplace.models import OrderClaim, OrderItem

    qs = OrderClaim.objects.select_related("order", "order__buyer").order_by("-created_at")
    if role == "buyer":
        qs = qs.filter(order__buyer=user)
    elif role == "seller":
        from .seller_actions import _effective_seller
        s = _effective_seller(user)
        order_ids = OrderItem.objects.filter(part__seller=s).values_list("order_id", flat=True).distinct()
        qs = qs.filter(order_id__in=list(order_ids))
    # operator → без фильтра

    all_claims = list(qs[:200])
    now = timezone.now()

    # Подсчёты
    by_status = {}
    for c in all_claims:
        by_status[c.status] = by_status.get(c.status, 0) + 1
    open_n     = by_status.get("open", 0)
    review_n   = by_status.get("in_review", 0)
    approved_n = by_status.get("approved", 0)
    corr_n     = by_status.get("corrective_actions", 0)
    fin_n      = by_status.get("financial_settlement", 0)
    closed_n   = by_status.get("closed", 0)
    rejected_n = by_status.get("rejected", 0)
    in_work    = open_n + review_n + approved_n + corr_n + fin_n

    # SLA: open > 2д, in_review > 5д
    overdue = []
    for c in all_claims:
        age_d = (now - c.created_at).days
        if c.status == "open" and age_d > 2:
            overdue.append((c, age_d))
        elif c.status == "in_review" and age_d > 5:
            overdue.append((c, age_d))

    total_refund = sum(
        (c.refund_amount or Decimal("0"))
        for c in all_claims
        if c.status in ("approved", "corrective_actions", "financial_settlement", "closed")
    )

    # Деньги в работе (потенциальный возврат, активные)
    pending_refund = sum(
        (c.refund_amount or Decimal("0"))
        for c in all_claims
        if c.status in ("open", "in_review", "approved")
    )

    # ── Аналитические метрики (не дублируют счётчики секций ниже) ──
    # 1. Средний срок решения по закрытым (в днях)
    closed_with_time = [
        (c.closed_at - c.created_at).days
        for c in all_claims
        if c.status in ("closed",) and c.closed_at
    ]
    avg_resolution = (sum(closed_with_time) / len(closed_with_time)) if closed_with_time else None

    # 2. Approval rate среди рассмотренных
    reviewed = [c for c in all_claims if c.status in (
        "approved", "rejected", "corrective_actions", "financial_settlement", "closed",
    )]
    approved_total = sum(1 for c in reviewed if c.status != "rejected")
    approval_rate = (approved_total * 100 // len(reviewed)) if reviewed else None

    # 3. Топ-причина (на каком типе проблем теряем больше всего)
    kind_cnt = {}
    for c in all_claims:
        kind_cnt[c.kind] = kind_cnt.get(c.kind, 0) + 1
    top_kind = max(kind_cnt.items(), key=lambda x: x[1]) if kind_cnt else None
    KIND_LBL_LOCAL = {k: str(v) for k, v in OrderClaim.KIND_CHOICES}

    # 4. Тренд: рекламации за 30 дней vs предыдущие 30
    cutoff_30 = now - timezone.timedelta(days=30)
    cutoff_60 = now - timezone.timedelta(days=60)
    last_30 = sum(1 for c in all_claims if c.created_at >= cutoff_30)
    prev_30 = sum(1 for c in all_claims if cutoff_60 <= c.created_at < cutoff_30)
    if prev_30:
        delta_pct = (last_30 - prev_30) * 100 // prev_30
    elif last_30:
        delta_pct = 100
    else:
        delta_pct = 0

    # 5. Повторные нарушители (заказы с >1 рекламацией за всё время)
    order_cnt = {}
    for c in all_claims:
        order_cnt[c.order_id] = order_cnt.get(c.order_id, 0) + 1
    repeat_orders = sum(1 for n in order_cnt.values() if n > 1)

    # 6. Средний возврат по закрытым с deny=False
    paid_refunds = [c.refund_amount for c in all_claims
                     if c.status == "closed" and c.refund_amount]
    avg_refund = (sum(paid_refunds) / len(paid_refunds)) if paid_refunds else None

    kpi_items = []
    if avg_resolution is not None:
        # Норматив: 5 дней. Хуже — bad, лучше — ok.
        tone = "bad" if avg_resolution > 7 else ("warn" if avg_resolution > 5 else "ok")
        kpi_items.append({"label": "Средний срок решения",
                          "value": f"{avg_resolution:.0f} дн", "tone": tone})
    if approval_rate is not None:
        # Высокий approval = жалобы оправданы → проблема с качеством/логистикой
        tone = "bad" if approval_rate >= 70 else ("warn" if approval_rate >= 40 else "ok")
        kpi_items.append({"label": "% одобренных",
                          "value": f"{approval_rate}%", "tone": tone})
    if pending_refund:
        kpi_items.append({"label": "Сумма под риском",
                          "value": f"${int(pending_refund):,}".replace(",", " "),
                          "tone": "warn"})
    if top_kind:
        share = top_kind[1] * 100 // len(all_claims)
        kpi_items.append({"label": "Топ-причина",
                          "value": f"{KIND_LBL_LOCAL.get(top_kind[0], top_kind[0])} ({share}%)",
                          "tone": "info"})
    if last_30 or prev_30:
        arrow = "↑" if delta_pct > 0 else ("↓" if delta_pct < 0 else "→")
        tone = "bad" if delta_pct > 20 else ("ok" if delta_pct < -10 else "info")
        kpi_items.append({"label": "Тренд 30д vs 30д",
                          "value": f"{arrow} {abs(delta_pct)}% ({last_30} шт)",
                          "tone": tone})
    if repeat_orders:
        kpi_items.append({"label": "Заказы с повторами",
                          "value": str(repeat_orders), "tone": "warn"})
    if avg_refund:
        kpi_items.append({"label": "Средний возврат",
                          "value": f"${int(avg_refund):,}".replace(",", " "),
                          "tone": "info"})
    if total_refund:
        kpi_items.append({"label": "Выплачено всего",
                          "value": f"${int(total_refund):,}".replace(",", " "),
                          "tone": "info"})

    cards = []
    if kpi_items:
        cards.append({"type": "kpi_grid", "data": {
            "title": "📊 Аналитика рекламаций",
            "items": kpi_items,
        }})

    STATUS_TONE = {
        "open":                 "bad",
        "in_review":            "warn",
        "approved":             "info",
        "corrective_actions":   "info",
        "financial_settlement": "info",
        "closed":               "ok",
        "rejected":             "ok",
    }
    KIND_LABEL = {k: str(v) for k, v in OrderClaim.KIND_CHOICES}
    STATUS_LABEL = {k: str(v) for k, v in OrderClaim.STATUS_CHOICES}

    def _row(c, age_d, *, sla_bad=False):
        order = c.order
        order_tag = f"#{order.id}"
        who = (order.buyer.username if order.buyer_id else (order.customer_name or "—"))[:24]
        money = f" · возврат ${int(c.refund_amount):,}".replace(",", " ") if c.refund_amount else ""
        return {
            "title": c.title[:60] or KIND_LABEL.get(c.kind, c.kind),
            "subtitle": (
                f"{KIND_LABEL.get(c.kind, c.kind)} · заказ {order_tag} · {who} · "
                f"{age_d} дн назад{money}"
            ),
            "badge": {"label": STATUS_LABEL.get(c.status, c.status),
                       "tone": "bad" if sla_bad else STATUS_TONE.get(c.status, "info")},
            "tone": "bad" if sla_bad else STATUS_TONE.get(c.status, "info"),
            "action": "claim_detail",
            "params": {"claim_id": c.id},
        }

    # 1. Просроченные — наверх (для оператора/продавца особенно важно)
    if overdue:
        overdue.sort(key=lambda x: -x[1])
        cards.append({"type": "list", "data": {
            "title": f"🔴 Просрочены SLA — требуют немедленной реакции ({len(overdue)})",
            "items": [_row(c, age_d, sla_bad=True) for c, age_d in overdue[:10]],
        }})

    # 2. Активные (не просроченные)
    overdue_ids = {c.id for c, _ in overdue}
    active_rows = []
    for c in all_claims:
        if c.id in overdue_ids:
            continue
        if c.status in ("closed", "rejected"):
            continue
        age_d = (now - c.created_at).days
        active_rows.append(_row(c, age_d))
    if active_rows:
        cards.append({"type": "list", "data": {
            "title": f"📋 Активные рекламации ({len(active_rows)})",
            "items": active_rows[:15],
        }})

    # 3. Недавно закрытые (для контекста)
    recent_closed = [c for c in all_claims if c.status in ("closed", "rejected")][:5]
    if recent_closed:
        cards.append({"type": "list", "data": {
            "title": f"✅ Недавно закрытые ({len(recent_closed)})",
            "items": [_row(c, (now - c.created_at).days) for c in recent_closed],
        }})

    # Текст-сводка — инсайт «что делать», а не пересказ счётчиков.
    text_parts = []
    if not all_claims:
        text_parts.append("Нет рекламаций.")
    elif overdue:
        text_parts.append(
            f"Самое срочное: {len(overdue)} рекламаций нарушили SLA — начните с верхнего блока."
        )
    elif top_kind and top_kind[1] >= 3:
        text_parts.append(
            f"Главная причина потерь: {KIND_LBL_LOCAL.get(top_kind[0], top_kind[0])} "
            f"({top_kind[1]} из {len(all_claims)}). Стоит проработать корневую причину."
        )
    elif delta_pct > 20:
        text_parts.append(
            f"За 30 дней рост рекламаций на {delta_pct}% — проверьте качество поставок."
        )
    elif in_work:
        text_parts.append(f"В работе {in_work} рекламаций, SLA в норме.")
    else:
        text_parts.append("Все рекламации закрыты.")

    # Быстрые действия по роли
    quick = []
    if role == "buyer":
        quick.append({"label": "Создать рекламацию", "action": "create_claim", "params": {}})
    elif role == "operator" and open_n:
        quick.append({"label": f"Взять в работу ({open_n})", "action": "get_claims",
                      "params": {"filter": "open"}})
    quick.append({"label": "Заказы", "action": "get_orders", "params": {}})

    back = ({"action": "op_analytics_hub", "label": "← Аналитика"} if role == "operator"
            else {"action": "support_home", "label": "← Поддержка"})
    return ActionResult(
        text=" ".join(text_parts),
        cards=cards,
        actions=quick,
        contextual_actions=[back],
    )


@register("create_claim")
def create_claim(params, user, role):
    """Создать рекламацию по заказу (chat-native, без редиректа на старую форму).

    Двухфазный flow:
      Phase 1 (no order_id|no kind|no description|not confirmed) → форма:
        выбор заказа (если order_id не передан) + вид + заголовок + описание.
      Phase 2 (confirmed=True + все поля) → создание OrderClaim, уведомление
        оператора + продавца, возврат подтверждения.
    """
    from marketplace.models import Order, OrderClaim

    from .order_events import notify_operator_alert
    confirmed = bool(params.get("confirmed"))
    order_id = params.get("order_id")
    kind = (params.get("kind") or "").strip()
    title = (params.get("title") or "").strip()
    description = (params.get("description") or "").strip()
    refund_str = (params.get("refund_amount") or "0").strip()

    # ── AuthZ: рекламацию открывает только тот, кто имеет отношение к заказу.
    # Без ограничения по роли — раньше любой не-buyer попадал в else-ветку и
    # мог открыть claim против чужого заказа.
    # Buyer  → только свои заказы (по полю buyer)
    # Seller → только заказы, где есть его OrderItem (через part.seller=user)
    # Operator → любой заказ (модерация)
    # Остальное → запрещено
    CLAIM_OK_STATUSES = ("ready_to_ship", "transit_abroad", "customs",
                         "transit_rf", "issuing", "delivered", "completed")
    if role not in ("buyer", "seller", "operator"):
        return ActionResult(text="Создание рекламации недоступно для вашей роли.")

    order = None
    if order_id:
        try:
            order_id_int = int(order_id)
        except (ValueError, TypeError):
            return ActionResult(text="Неверный ID заказа.")
        qs = Order.objects.filter(id=order_id_int)
        if role == "buyer":
            qs = qs.filter(buyer=user)
        elif role == "seller":
            from marketplace.models import OrderItem

            from .seller_actions import _effective_seller
            user = _effective_seller(user)
            if not OrderItem.objects.filter(
                order_id=order_id_int, part__seller=user,
            ).exists():
                return ActionResult(
                    text=f"Заказ #{order_id} не содержит ваших товаров.",
                )
        # operator → no filter, может на любой заказ
        order = qs.first()
        if not order:
            return ActionResult(text=f"Заказ #{order_id} не найден или не принадлежит вам.")

    # ── Phase 1: форма ─────────────────────────────────────────
    if not confirmed or not kind or not title or not description:
        # Если заказ не выбран — показываем select со списком подходящих
        order_options = []
        if not order:
            qs = Order.objects.filter(status__in=CLAIM_OK_STATUSES)
            if role == "buyer":
                qs = qs.filter(buyer=user)
            elif role == "seller":
                qs = qs.filter(items__part__seller=user).distinct()
            # operator → видит все
            qs = qs.order_by("-id")[:20]
            order_options = [{"value": str(o.id),
                              "label": f"ORD-{o.id} · {o.customer_name or ''} · "
                                       f"{o.get_status_display()} · "
                                       f"${float(o.total_amount or 0):,.0f}"}
                             for o in qs]
            if not order_options:
                return ActionResult(
                    text="🧾 У вас нет заказов, по которым можно открыть рекламацию.\n"
                         "Рекламация открывается на доставленные / в пути / готовые к отгрузке заказы.",
                    contextual_actions=[{"action": "go_home", "label": "🏠 Главная"}],
                )

        kind_choices = [
            {"value": "defect",     "label": "🔧 Брак"},
            {"value": "wrong_part", "label": "🔁 Не та деталь"},
            {"value": "missing",    "label": "📭 Не пришла"},
            {"value": "damage",     "label": "📦 Повреждение при доставке"},
            {"value": "late",       "label": "⏰ Просрочка поставки"},
            {"value": "other",      "label": "❓ Другое"},
        ]
        fields = []
        if order:
            fields.append({"name": "_order_label",
                            "label": "Заказ",
                            "value": f"ORD-{order.id} · {order.customer_name or ''}",
                            "readonly": True})
        else:
            fields.append({"name": "order_id", "label": "Заказ",
                            "type": "select", "required": True,
                            "options": order_options})
        fields.extend([
            {"name": "kind", "label": "Что произошло", "type": "select",
             "required": True, "options": kind_choices, "value": kind or "defect"},
            {"name": "title", "label": "Краткий заголовок",
             "required": True, "value": title,
             "placeholder": "Например: «Гидроцилиндр течёт по штоку»"},
            {"name": "description", "label": "Подробное описание",
             "type": "textarea", "required": True, "value": description,
             "placeholder": "Что именно не так, как обнаружили, какое решение хотите"},
            {"name": "refund_amount", "label": "Желаемая компенсация ($, опц.)",
             "type": "number", "value": refund_str if refund_str != "0" else ""},
        ])
        fixed = {"confirmed": True}
        if order:
            fixed["order_id"] = order.id
        return ActionResult(
            text="🧾 Открытие рекламации",
            cards=[{"type": "form", "data": {
                "title": "🧾 Новая рекламация" + (f" по ORD-{order.id}" if order else ""),
                "submit_action": "create_claim",
                "submit_label": "📨 Отправить рекламацию",
                "fields": fields,
                "fixed_params": fixed,
            }}],
            contextual_actions=[{"action": "go_home", "label": "🏠 Главная"}],
        )

    # ── Phase 2: сохранение ───────────────────────────────────
    if not order:
        return ActionResult(text="Не указан заказ.")
    try:
        from decimal import Decimal
        refund = Decimal(refund_str or "0")
    except Exception:
        refund = Decimal("0")
    resolution_kind = "partial_refund" if refund > 0 else "none"

    claim = OrderClaim.objects.create(
        order=order, kind=kind, title=title, description=description,
        status="open", resolution_kind=resolution_kind,
        refund_amount=refund, opened_by=user,
    )
    # Уведомляем оператора
    try:
        notify_operator_alert(order=order, event="claim_opened",
                                text=f"Открыта рекламация по ORD-{order.id}: {title}")
    except Exception:
        logger.exception("notify_operator_alert failed for claim")

    return ActionResult(
        text=(
            f"✓ Рекламация #{claim.id} открыта по ORD-{order.id}.\n"
            f"Тип: {claim.get_kind_display()}. Оператор уведомлён, "
            f"свяжется с продавцом в течение 24 часов."
        ),
        contextual_actions=[
            {"action": "get_claims", "label": "🧾 Мои рекламации"},
            {"action": "track_order", "label": "📦 Заказ", "params": {"order_id": order.id}},
            {"action": "go_home", "label": "🏠 Главная"},
        ],
    )


@register("upload_parts_list")
def upload_parts_list(params, user, role):
    """Buyer: вставить список артикулов (текстом или CSV) → распарсить → поиск.

    Phase 1 (no text): форма с textarea.
    Phase 2 (text provided): парсим строки `OEM<tab|comma|space>QTY`, дальше
    показываем как обычный search_parts c найденными позициями.
    """
    raw_text = (params.get("text") or params.get("articles_text") or "").strip()
    confirmed = bool(params.get("confirmed")) or bool(raw_text)
    if not confirmed:
        return ActionResult(
            text="📋 Вставьте список артикулов одним блоком — найду совпадения в каталоге.",
            cards=[{"type": "form", "data": {
                "title": "📋 Список артикулов",
                "submit_action": "upload_parts_list",
                "submit_label": "🔎 Найти в каталоге",
                "fields": [{
                    "name": "text", "type": "textarea", "rows": 8,
                    "label": "OEM-номера (по одному в строке, можно с количеством)",
                    "placeholder": "Примеры:\n2W1223  1\n1R0750  2\n14Y-22-37470 5",
                    "required": True,
                }],
                "fixed_params": {"confirmed": True},
            }}],
            contextual_actions=[{"action": "go_home", "label": "🏠 Главная"}],
        )

    # Phase 2: parse → search_parts
    articles = []
    quantities = {}
    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.replace(",", " ").replace("\t", " ").split()
        if not parts:
            continue
        art = parts[0].strip()
        if not art:
            continue
        articles.append(art)
        qty = 1
        if len(parts) >= 2:
            try:
                qty = max(1, int(parts[1]))
            except ValueError:
                pass
        quantities[art] = qty

    if not articles:
        return ActionResult(text="Не удалось распознать ни одного артикула.")

    # Дальше — обычный search_parts с распознанными артикулами
    return search_parts({"articles": articles, "_quantities": quantities},
                       user, role)


@register("upload_pricelist")
def upload_pricelist(params, user, role):
    """Seller: вставить прайс CSV → распарсить → bulk-create Part'ов.

    Формат строки: OEM,Название,Бренд,Цена,Количество,Состояние(oem|aftermarket)
    Минимум: OEM + Цена. Остальное опционально.
    Phase 1: форма с textarea. Phase 2: парсим и создаём.
    """
    import re as _re
    from decimal import Decimal as _D

    from marketplace.models import Brand, Category, Part

    if role != "seller":
        return ActionResult(text="Загрузка прайса доступна только продавцам.")

    raw_csv = (params.get("csv") or params.get("text") or "").strip()
    confirmed = bool(params.get("confirmed")) or bool(raw_csv)

    if not confirmed:
        return ActionResult(
            text="📤 Вставьте прайс-лист (CSV) — добавлю позиции в ваш каталог.",
            cards=[{"type": "form", "data": {
                "title": "📤 Загрузка прайс-листа",
                "submit_action": "upload_pricelist",
                "submit_label": "✓ Импортировать",
                "fields": [{
                    "name": "csv", "type": "textarea", "rows": 10,
                    "label": "CSV-строки (минимум: OEM, цена)",
                    "placeholder": "Формат:\nOEM,Название,Бренд,Цена,Кол-во,Состояние\n"
                                   "2W1223,Уплотнение гидроцилиндра,Caterpillar,180,15,oem\n"
                                   "1R0750,Фильтр масла,Caterpillar,42,40,oem",
                    "required": True,
                }],
                "fixed_params": {"confirmed": True},
            }}],
            contextual_actions=[{"action": "go_home", "label": "🏠 Главная"}],
        )

    # Phase 2: parse
    created, updated, errors = 0, 0, []
    default_category = Category.objects.first()
    if not default_category:
        default_category = Category.objects.create(name="Запчасти")

    for line_num, line in enumerate(raw_csv.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Допускаем разделители , ; \t
        cols = [c.strip() for c in _re.split(r"[,;\t]", line)]
        if len(cols) < 2:
            errors.append(f"стр.{line_num}: мало колонок ({line[:40]})")
            continue
        oem = cols[0].strip()
        # Skip header rows
        if oem.lower() in ("oem", "артикул", "sku", "номер"):
            continue
        # Find price column (look for first numeric)
        price = None
        title = oem
        brand_name = ""
        qty = 1
        condition = "oem"
        for i, col in enumerate(cols[1:], 1):
            if price is None:
                try:
                    candidate = _D(col.replace(" ", "").replace("\xa0", ""))
                    if candidate > 0:
                        price = candidate
                        continue
                except Exception:
                    pass
            if i == 1 and title == oem:
                title = col
            elif not brand_name and not col.replace(".", "").isdigit():
                brand_name = col
        if price is None:
            errors.append(f"стр.{line_num}: не найдена цена ({line[:40]})")
            continue
        # Try parse qty (last numeric col) and condition (oem/aftermarket)
        for col in cols[::-1]:
            cl = col.strip().lower()
            if cl in ("oem", "aftermarket", "an", "а/н", "ан"):
                condition = "oem" if cl == "oem" else "aftermarket"
            elif cl.isdigit() and int(cl) > 0 and int(cl) < 100000:
                qty = int(cl)

        brand_obj = None
        if brand_name:
            brand_obj, _ = Brand.objects.get_or_create(name=brand_name[:140])

        slug = _re.sub(r"[^a-z0-9]+", "-", (oem + "-" + str(user.id)).lower()).strip("-")[:280]
        existing = Part.objects.filter(seller=user, oem_number=oem).first()
        if existing:
            existing.price = price
            existing.title = title[:255] or existing.title
            existing.stock_quantity = qty
            existing.condition = condition
            if brand_obj:
                existing.brand = brand_obj
            existing.availability_status = "active"
            existing.save(update_fields=["price", "title", "stock_quantity",
                                          "condition", "brand", "availability_status"])
            updated += 1
        else:
            Part.objects.create(
                seller=user, oem_number=oem, slug=slug,
                title=title[:255], brand=brand_obj, category=default_category,
                price=price, stock_quantity=qty, condition=condition,
                availability_status="active",
            )
            created += 1

    text_parts = ["📥 Импорт завершён."]
    if created:  text_parts.append(f"Создано позиций: **{created}**")
    if updated:  text_parts.append(f"Обновлено: **{updated}**")
    if errors:   text_parts.append(f"Ошибок: {len(errors)} (первые 3 ниже)")
    if errors:
        text_parts.extend(f"• {e}" for e in errors[:3])

    return ActionResult(
        text="\n".join(text_parts),
        contextual_actions=[
            {"action": "seller_warehouses", "label": "📦 Мои товары"},
            {"action": "go_home", "label": "🏠 Главная"},
        ],
    )


@register("respond_rfq")
def respond_rfq(params, user, role):
    rfq_id = params.get("rfq_id")
    return ActionResult(
        text=f"Ответ на RFQ #{rfq_id}: используйте форму на /seller/requests/{rfq_id}/",
    )


# ══════════════════════════════════════════════════════════
# Spec analysis (multi-line BoM → priced mix)
# ══════════════════════════════════════════════════════════

# Demo data — realistic-looking spec for the Spec Q2 2026 reference screenshot.
# In production this comes from parsing user-uploaded XLSX + matching against
# the catalog + querying suppliers. Right now we hand-craft for the demo so the
# response renders exactly like the design reference.
_DEMO_SPEC_ITEMS = [
    {"status": "in_stock", "id": "3047531", "name": "Filter, hydraulic — return line",
     "brand": "CAT", "condition": "oem", "price": 176, "qty": 12, "weight": "4 lbs"},
    {"status": "in_stock", "id": "9X-2073", "name": "Seal kit, cylinder rod",
     "brand": "CAT", "condition": "oem", "price": 148, "qty": 16, "weight": "1 lb",
     "tag": "приоритет ТО"},
    {"status": "backorder", "id": "7Y-1947", "name": "Bushing, pin — bucket linkage",
     "brand": "CAT", "condition": "oem", "price": 56.20, "qty": 24, "weight": "2 lbs"},
    {"status": "in_stock", "id": "8E-9885", "name": "Cutting edge — Komatsu PC400",
     "brand": "KOMATSU", "condition": "analogue", "price": 412, "qty": 6, "weight": "18 lbs"},
    {"status": "backorder", "id": "386-9999", "name": "Track shoe assembly — D8T",
     "brand": "CAT", "condition": "analogue", "price": 3720, "qty": 2, "weight": "220 lbs"},
    {"status": "not_found", "id": "XB-77421", "name": "", "qty": 3},
]


@register("analyze_spec")
def analyze_spec(params, user, role):
    """Analyze a multi-line spec — returns spec_results card with KPIs + table.

    params: {file_id?, query?, lead_max_days?, condition?}
      condition='oem' filters out analogues
    """
    cond = (params.get("condition") or "").lower()
    lead_max = params.get("lead_max_days")
    items = _DEMO_SPEC_ITEMS

    if cond == "oem":
        items = [it for it in items if it.get("condition") == "oem" or it["status"] == "not_found"]

    # Static aggregated stats (47 lines total in spec; visible items are a 6-row preview)
    found = 32 if cond != "oem" else 28
    analogue = 11 if cond != "oem" else 0
    not_found = 4
    total = 48420 if cond != "oem" else 47890

    refs = [
        "fleet_nordisk_2026.xlsx", "service_intervals.xlsx", "cat_988h_assembly.pdf",
    ]

    intro = (
        f"Обработал спеку: {found} Found · {analogue} Analogue · {not_found} Not found. "
        f"Собрал 198 предложений от 23 поставщиков. "
        f"Лучший микс — ${total:,.0f} у 12 поставщиков, средний лидтайм 11 дней."
    )
    if cond == "oem" and lead_max:
        intro = (
            f"Сузил выборку: {found} OEM-предложений у 8 поставщиков, "
            f"лидтайм 4–{lead_max} дней. Топ-3 по сумме при заказе всей спеки:"
        )
    elif cond == "oem":
        intro = f"Только OEM: {found} позиций у 8 поставщиков, средняя сумма ${total:,.0f}."

    card = {
        "type": "spec_results",
        "data": {
            "title": "Spec Q2 2026 — Результаты",
            "found": found,
            "analogue": analogue,
            "not_found": not_found,
            "items": items,
            "more_count": max(0, 47 - len(items)),
            "offers_count": 198,
            "sellers_count": 23,
            "best_mix": int(total),
            "total": int(total),
            "currency": "USD",
            "foot_info": f"Estimated total · {len(items) - not_found} из 47 priced · средний лидтайм 11 дней",
        },
    }

    actions_list = [
        {"label": "Открыть в Explorer", "action": "search_parts", "params": {"query": "spec_q2"}},
        {"label": "Создать RFQ", "action": "create_rfq", "params": {"query": "Spec Q2 2026"}},
        {"label": "Только OEM", "action": "analyze_spec", "params": {"condition": "oem"}},
        {"label": "Найти аналоги для 4", "action": "analyze_spec", "params": {"condition": "analogue"}},
        {"label": "Экспорт в .xlsx", "action": "analyze_spec", "params": {"export": "xlsx"}},
    ]

    return ActionResult(
        text=intro,
        cards=[card],
        actions=actions_list,
        suggestions=[
            "Только OEM, лидтайм до 14 дней",
            "Покажи топ-3 поставщиков",
            "Сравни цены по бренду",
        ],
    )


@register("top_suppliers")
def top_suppliers(params, user, role):
    """Top-N suppliers ranked by price/coverage/lead time on the current spec.

    Для buyer'а имена анонимизированы (Поставщик №1/2/3) до момента
    принятия котировки. Это защищает платформу от обхода — buyer не может
    напрямую связаться с поставщиком в обход маркетплейса.
    """
    suppliers = [
        {"name": "Caterpillar Eurasia", "rating": "4.9", "total": 47890,
         "coverage": "32 из 39 позиций", "lead_time": "9 дней", "currency": "USD"},
        {"name": "Heavy Equipment Spares", "rating": "4.7", "total": 48720,
         "coverage": "35 из 39", "lead_time": "10 дней", "currency": "USD"},
        {"name": "Уралмаш-Маркет", "rating": "4.8", "total": 48410,
         "coverage": "38 из 39", "lead_time": "11 дней", "note": "включая аналоги",
         "currency": "USD"},
    ]
    visible = _maybe_anonymize_suppliers(suppliers, role)

    if _is_buyer_view(role):
        intro = (
            "Топ-3 поставщика по вашей спеке. Имена скрыты — раскрываются "
            "после принятия котировки. Создать RFQ всем?"
        )
        # Используем индексы вместо имён в action params
        compare_ids = [f"supplier_{i + 1}" for i, _ in enumerate(suppliers)]
    else:
        intro = (
            "Рекомендую разослать всем трём — Caterpillar Eurasia может не покрыть 7 позиций, "
            "остальные дадут конкуренцию по цене. Создать RFQ?"
        )
        compare_ids = [s["name"] for s in suppliers]

    return ActionResult(
        text=intro,
        cards=[{"type": "supplier_top", "data": {"suppliers": visible}}],
        actions=[
            {"label": "Создать RFQ для топ-3", "action": "create_rfq",
             "params": {"query": "Spec Q2 2026 — top 3 suppliers"}},
            {"label": "Добавить ещё поставщиков", "action": "top_suppliers",
             "params": {"limit": 5}},
            {"label": "Сравнить детально", "action": "compare_suppliers",
             "params": {"supplier_ids": compare_ids}},
        ],
        suggestions=["Только OEM-сертифицированные", "Сравни по SLA"],
    )


# ══════════════════════════════════════════════════════════
# Quick path: spec → order → payment (без RFQ-цикла)
# ══════════════════════════════════════════════════════════

@register("quick_order")
def quick_order(params, user, role):
    """Создать заказ из найденных артикулов сразу, минуя RFQ.

    params: {product_ids: [int, ...], quantity?: int}
    """
    from decimal import Decimal

    from marketplace.models import Order, OrderItem, Part

    from .models import Wallet

    product_ids = params.get("product_ids") or []
    quantity = int(params.get("quantity") or 1)
    # SECURITY: количество должно быть положительным целым
    if quantity <= 0:
        return ActionResult(text="Количество должно быть больше 0.")
    if not product_ids:
        return ActionResult(
            text="Нет позиций для заказа. Загрузите спеку или добавьте артикулы в сообщение.",
        )

    parts = list(
        Part.objects.select_related("brand")
        .filter(id__in=product_ids, is_active=True)
    )
    if not parts:
        return ActionResult(text="Запчасти не найдены — возможно, удалены из каталога.")

    # SECURITY P0-7: confirmed-gate. Без подтверждения — показываем preview,
    # не создаём заказ. AI или фронт-кнопка из spec_results не должны
    # создавать Order на реальную сумму без явного клика «Подтвердить».
    if not bool(params.get("confirmed")):
        preview_total = sum((Decimal(str(p.price or 0)) * quantity) for p in parts)
        return ActionResult(
            text=(f"📦 Подтвердите заказ:\n"
                   f"Позиций: {len(parts)} · кол-во каждой: {quantity}\n"
                   f"Сумма товара: ${float(preview_total):,.0f}"),
            cards=[{"type": "list", "data": {
                "title": "Позиции в заказе",
                "items": [{
                    "title": ((p.brand.name + ' · ') if p.brand_id else '')
                              + (getattr(p, 'article', None) or getattr(p, 'name', None) or f'#{p.id}'),
                    "subtitle": f"{quantity} × ${float(p.price or 0):,.0f}",
                } for p in parts[:20]],
            }}],
            actions=[
                {"label": f"✓ Подтвердить заказ", "action": "quick_order",
                 "params": {**params, "confirmed": True}},
                {"label": "Отмена", "action": "search_parts", "params": {}},
            ],
        )

    total = Decimal("0")
    for p in parts:
        if p.price:
            total += Decimal(str(p.price)) * quantity

    # Рассчитываем доставку с учётом выбранных mode + incoterm.
    from assistant.logistics import (
        INCOTERM_RULES,
        _country_from_port,
        _volumetric_kg,
        calc_incoterm_breakdown,
        calc_logistics,
    )
    from marketplace.models import LogisticsTariff
    dest_country = (params.get("dest_country") or "RU").upper()[:2]
    chosen_mode = params.get("mode") or ""
    chosen_inc = params.get("incoterm") or "FOB"
    if chosen_inc not in INCOTERM_RULES:
        chosen_inc = "FOB"
    ship_total = Decimal("0")
    ship_breakdown = []
    ship_components = {"freight": Decimal("0"), "insurance": Decimal("0"),
                        "carriage_ext": Decimal("0"), "duty": Decimal("0"),
                        "vat": Decimal("0"), "last_mile": Decimal("0")}
    ship_missing = 0
    for p in parts:
        best = None
        modes_to_try = [chosen_mode] if chosen_mode else ("sea", "air", "auto")
        for m in modes_to_try:
            r = calc_logistics(p, dest_country, m)
            if r.get("cost") is None:
                continue
            if best is None or r["cost"] < best["cost"]:
                best = r
        if best:
            cargo_line = Decimal(str(p.price or 0)) * Decimal(quantity)
            freight_line = best["cost"] * Decimal(quantity)
            bd = calc_incoterm_breakdown(freight_line, cargo_line, chosen_inc)
            ship_total += bd["total"]
            for k in ship_components:
                ship_components[k] += bd[k]
            ship_breakdown.append((p.oem_number, best["mode"], bd["total"], best["transit_days"]))
        else:
            ship_missing += 1
    landed_total = (total + ship_total).quantize(Decimal("0.01"))

    # Origin breakdown — состав отправки по странам для выбранного mode.
    # Считается ОТ chosen_mode тарифа: rate × Σchargeable, min_charge один раз.
    from collections import defaultdict as _dd
    origin_breakdown = []
    eff_mode = chosen_mode or "sea"
    port_field = "sea_port" if eff_mode == "sea" else "air_port" if eff_mode == "air" else "sea_port"
    by_cc: dict = _dd(lambda: {"ports": set(), "count": 0,
                                "weight": Decimal("0"), "cargo": Decimal("0"),
                                "freight": Decimal("0"), "days": 0})
    parts_by_origin: dict = _dd(list)
    for p in parts:
        origin = ((getattr(p, port_field, "") or "").strip())
        origin_code = origin.split()[0] if origin else ""
        if not origin_code:
            continue
        cc = _country_from_port(origin_code) or origin_code[:2].upper()
        ch = max(
            Decimal(p.gross_weight_kg or 0),
            _volumetric_kg(p.length_cm, p.width_cm, p.height_cm, eff_mode),
        ) * Decimal(quantity)
        cargo_line = Decimal(str(p.price or 0)) * Decimal(quantity)
        b = by_cc[cc]
        b["ports"].add(origin_code)
        b["count"] += 1
        b["weight"] += ch
        b["cargo"] += cargo_line
        parts_by_origin[(cc, origin_code)].append(ch)
    # Считаем freight per origin_port (как в _search_articles_list), потом
    # схлопываем по стране для UI.
    for (cc, origin_code), chargeables in parts_by_origin.items():
        t = LogisticsTariff.objects.filter(
            origin_port__iexact=origin_code, dest_country=dest_country,
            mode=eff_mode, is_active=True,
        ).first()
        if not t:
            t = LogisticsTariff.objects.filter(
                origin_port__iexact=cc, dest_country=dest_country,
                mode=eff_mode, is_active=True,
            ).first()
        if not t:
            continue
        ch_sum = sum(chargeables, Decimal("0"))
        gf = ch_sum * t.rate_per_kg
        if t.min_charge and gf < t.min_charge:
            gf = Decimal(t.min_charge)
        by_cc[cc]["freight"] += gf
        if (t.transit_days or 0) > by_cc[cc]["days"]:
            by_cc[cc]["days"] = t.transit_days or 0

    cc_flags = {"CN":"🇨🇳","TR":"🇹🇷","AE":"🇦🇪","NL":"🇳🇱","KZ":"🇰🇿","RU":"🇷🇺","DE":"🇩🇪","US":"🇺🇸","PK":"🇵🇰","ES":"🇪🇸"}
    cc_names = {"CN":"Китай","TR":"Турция","AE":"ОАЭ","NL":"Нидерланды","KZ":"Казахстан","RU":"Россия","DE":"Германия","US":"США","PK":"Пакистан","ES":"Испания"}
    for cc, b in sorted(by_cc.items(), key=lambda x: -x[1]["cargo"]):
        origin_breakdown.append({
            "country_code": cc, "flag": cc_flags.get(cc, "🌍"),
            "name": cc_names.get(cc, cc), "ports": sorted(b["ports"]),
            "count": b["count"], "weight_kg": float(b["weight"]),
            "cargo": float(b["cargo"]), "freight": float(b["freight"]),
            "days": b["days"],
        })

    # Бизнес-правило: не работаем с заказами меньше MIN_ORDER_USD.
    # Проверяем по landed_total (запчасти + логистика), т.к. именно это
    # юзер реально оплачивает.
    from .order_limits import check_min_order
    block = check_min_order(landed_total)
    if block:
        return ActionResult(**block)

    reserve_pct = Decimal("10.00")
    reserve_amount = (landed_total * reserve_pct / Decimal("100")).quantize(Decimal("0.01"))
    wallet = Wallet.for_user(user)

    order = Order.objects.create(
        customer_name=user.get_full_name() or user.username,
        customer_email=user.email or f"{user.username}@chat.local",
        customer_phone="",
        delivery_address="—",
        buyer=user,
        status="pending",
        payment_status="awaiting_reserve",
        payment_scheme="simple",
        reserve_percent=reserve_pct,
        reserve_amount=reserve_amount,
        total_amount=landed_total,  # включаем доставку в total
    )
    for p in parts:
        OrderItem.objects.create(
            order=order,
            part=p,
            quantity=quantity,
            unit_price=p.price or Decimal("0"),
        )
    _log_event(order, "order_created", actor=user, source="buyer",
               meta={"items": len(parts), "total": float(total)})
    # Уведомляем продавцов о новом заказе
    _notify_seller_of_order(
        order, kind="order",
        title=f"Новый заказ #{order.id}",
        body=f"Покупатель {user.username} оформил заказ на ${total:,.0f} ({len(parts)} поз.).",
    )

    enough = wallet.balance >= reserve_amount

    # Сохраняем дефолтные shipping_mode + incoterm на ордер.
    # Покупатель сможет переключить через "shipping_choose" — пересчитаем.
    default_mode = None
    if ship_breakdown:
        # Самый частый mode среди позиций
        from collections import Counter
        default_mode = Counter(m for _, m, _, _ in ship_breakdown).most_common(1)[0][0]
    order.shipping_mode = chosen_mode or default_mode or "sea"
    order.incoterm = chosen_inc
    order.logistics_cost = ship_total
    order.save(update_fields=["shipping_mode", "incoterm", "logistics_cost"])

    # Сводка с разложением доставки по компонентам Incoterms-базиса
    mode_counts = {}
    for _, m, _, _ in ship_breakdown:
        mode_counts[m] = mode_counts.get(m, 0) + 1
    mode_emoji = {"sea": "🚢", "air": "✈️", "auto": "🚚"}
    mode_label = " ".join(f"{n}×{mode_emoji.get(m, m)}" for m, n in mode_counts.items())
    text_lines = [
        f"✓ Заказ #{order.id} создан · {len(parts)} позиций · базис **{chosen_inc}** {mode_label}",
        f"  Товары (EXW):           ${total:,.2f}",
    ]
    # Детальный breakdown — только ненулевые строки
    bd_labels = [
        ("freight",      "Фрахт",           "📦"),
        ("carriage_ext", "Carriage до места", "→"),
        ("insurance",    "Страховка груза",  "🛡"),
        ("duty",         "Импортная пошлина","🛂"),
        ("vat",          "НДС 20%",          "📑"),
        ("last_mile",    "Доставка до двери","🏠"),
    ]
    for key, label, icon in bd_labels:
        v = ship_components.get(key, Decimal("0"))
        if v > 0:
            text_lines.append(f"  {icon} {label:22s} ${v:,.2f}")
    if ship_total > 0:
        text_lines.append("  ─────────────────────")
        text_lines.append(f"  Итого доставка:         ${ship_total:,.2f}")
    if ship_missing:
        text_lines.append(f"  ⚠ Доставка не рассчитана для {ship_missing} поз.")
    text_lines.append(f"  **🎯 Landed total:       ${landed_total:,.2f}**")
    text_lines.append("")
    text_lines.append(f"Резерв 10%: ${reserve_amount:,.2f} · на счёте: ${wallet.balance:,.2f} {wallet.currency}")
    if not enough:
        text_lines.append(f"⚠️ Недостаточно — пополните на ${reserve_amount - wallet.balance:,.0f}.")

    return ActionResult(
        text="\n".join(text_lines),
        cards=[{
            "type": "order",
            "data": {
                "id": str(order.id),
                "number": order.id,
                "status": "pending",
                "status_label": "Ожидание оплаты",
                "items_count": len(parts),
                "total": float(landed_total),
                "items_subtotal": float(total),
                "shipping_total": float(ship_total),
                "shipping_missing": ship_missing,
                "dest_country": dest_country,
                "currency": "USD",
                "reserve_amount": float(reserve_amount),
                "payment_status": "awaiting_reserve",
                "payment_status_label": "Ожидает резерва 10%",
                "wallet_balance": float(wallet.balance),
                "shipping_mode": order.shipping_mode,
                "incoterm": chosen_inc,
                "origin_breakdown": origin_breakdown,
                "can_cancel": True,
            },
        }],
        actions=(
            [{"label": f"💳 Списать ${reserve_amount:,.0f} из депозита",
              "action": "pay_reserve", "params": {"order_id": order.id}}]
            if enough else
            [{"label": "Пополнить депозит (демо)", "action": "topup_wallet",
              "params": {"amount": float(max(reserve_amount * 5, Decimal("10000"))),
                          "pending_order_id": order.id}}]
        ) + [
            {"label": "🚚 Изменить доставку/базис",
             "action": "shipping_choose", "params": {"order_id": order.id}},
            {"label": "Детали заказа", "action": "get_order_detail",
             "params": {"order_id": order.id}},
        ],
        suggestions=["Баланс депозита", "Статус заказа", "Изменить адрес доставки"],
    )


@register("shipping_choose")
def shipping_choose(params, user, role):
    """Показать покупателю варианты доставки (sea/air) и базиса (FOB/CIF/DDP)
    с пересчётом landed cost для каждого варианта."""
    from decimal import Decimal

    from assistant.logistics import calc_logistics
    from marketplace.models import Order
    try:
        oid = int(params.get("order_id") or 0)
    except (ValueError, TypeError):
        return ActionResult(text="Неверный order_id.")
    try:
        order = Order.objects.get(id=oid, buyer=user)
    except Order.DoesNotExist:
        return ActionResult(text=f"Заказ #{oid} не найден.")
    if order.payment_status != "awaiting_reserve":
        return ActionResult(text=f"По заказу #{oid} уже выбран базис — резерв оплачен.")

    items = list(order.items.select_related("part"))
    # Destination читаем из профиля покупателя; параметр params.dest перекрывает.
    # Fallback: первые 2 символа delivery_address (если ISO-страна явно прописана) → "RU".
    dest = (params.get("dest") or "").strip().upper()
    if not dest:
        try:
            prof_country = getattr(user.profile, "country", "") or ""
            dest = (prof_country or "").strip().upper()
        except Exception:
            dest = ""
    if not dest and order.delivery_address:
        head = (order.delivery_address or "").strip().split()[:1]
        if head and len(head[0]) == 2 and head[0].isalpha():
            dest = head[0].upper()
    if not dest:
        dest = "RU"
    # Считаем суммарный shipping для каждого mode
    sums = {"sea": Decimal("0"), "air": Decimal("0")}
    days_max = {"sea": 0, "air": 0}
    for mode in ("sea", "air"):
        for it in items:
            r = calc_logistics(it.part, dest, mode)
            if r.get("cost"):
                sums[mode] += r["cost"] * Decimal(it.quantity)
                if r.get("transit_days"):
                    days_max[mode] = max(days_max[mode], r["transit_days"])
    # Markup для CIF и DDP относительно базового FOB shipping
    # FOB = только shipping. CIF = + insurance/freight 5%. DDP = + customs/local 18%.
    base_items_total = sum(
        Decimal(str(it.unit_price or 0)) * Decimal(it.quantity) for it in items
    )
    incoterm_markup = {"FOB": Decimal("1.00"), "CIF": Decimal("1.05"), "DDP": Decimal("1.18")}

    rows = []
    for mode in ("sea", "air"):
        if sums[mode] <= 0:
            continue
        for inc in ("FOB", "CIF", "DDP"):
            ship = (sums[mode] * incoterm_markup[inc]).quantize(Decimal("0.01"))
            landed = (base_items_total + ship).quantize(Decimal("0.01"))
            rows.append({
                "mode": mode, "mode_label": "🚢 Морем" if mode == "sea" else "✈️ Авиа",
                "incoterm": inc,
                "incoterm_desc": {
                    "FOB": "до порта отгрузки (вы организуете дальше)",
                    "CIF": "до порта назначения, фрахт+страховка включены",
                    "DDP": "до двери, всё включено (фрахт, страховка, таможня)",
                }[inc],
                "shipping": float(ship),
                "landed": float(landed),
                "days": days_max[mode],
                "selected": (mode == order.shipping_mode and inc == order.incoterm),
            })

    text = (f"🚚 Выберите способ доставки и базис для заказа #{order.id}\n"
            f"Базовая стоимость товаров: ${base_items_total:,.2f}")
    return ActionResult(
        text=text,
        cards=[{
            "type": "shipping_options",
            "data": {
                "title": "Варианты доставки",
                "order_id": order.id,
                "rows": rows,
                "currency": "USD",
            },
        }],
        suggestions=["Объяснить разницу FOB/CIF/DDP", "Изменить страну доставки"],
    )


@register("shipping_apply")
def shipping_apply(params, user, role):
    """Применить выбор mode + incoterm к ордеру и пересчитать landed."""
    from decimal import Decimal

    from assistant.logistics import calc_logistics
    from marketplace.models import Order
    try:
        oid = int(params.get("order_id") or 0)
    except (ValueError, TypeError):
        return ActionResult(text="Неверный order_id.")
    mode = params.get("mode")
    inc = params.get("incoterm")
    if mode not in ("sea", "air"):
        return ActionResult(text="Способ доставки должен быть sea или air.")
    if inc not in ("FOB", "CIF", "DDP"):
        return ActionResult(text="Базис должен быть FOB, CIF или DDP.")
    try:
        order = Order.objects.get(id=oid, buyer=user)
    except Order.DoesNotExist:
        return ActionResult(text=f"Заказ #{oid} не найден.")
    if order.payment_status != "awaiting_reserve":
        return ActionResult(text="Резерв уже оплачен — нельзя менять базис.")

    items = list(order.items.select_related("part"))
    dest = "RU"
    ship_total = Decimal("0")
    for it in items:
        r = calc_logistics(it.part, dest, mode)
        if r.get("cost"):
            ship_total += r["cost"] * Decimal(it.quantity)
    markup = {"FOB": Decimal("1.00"), "CIF": Decimal("1.05"), "DDP": Decimal("1.18")}[inc]
    ship_total = (ship_total * markup).quantize(Decimal("0.01"))
    items_total = sum(
        Decimal(str(it.unit_price or 0)) * Decimal(it.quantity) for it in items
    )
    landed = (items_total + ship_total).quantize(Decimal("0.01"))
    reserve = (landed * Decimal("0.10")).quantize(Decimal("0.01"))

    order.shipping_mode = mode
    order.incoterm = inc
    order.logistics_cost = ship_total
    order.total_amount = landed
    order.reserve_amount = reserve
    order.save(update_fields=[
        "shipping_mode", "incoterm", "logistics_cost",
        "total_amount", "reserve_amount",
    ])

    from .models import Wallet
    wallet = Wallet.for_user(user)
    enough = wallet.balance >= reserve

    mode_label = "🚢 Морем" if mode == "sea" else "✈️ Авиа"
    return ActionResult(
        text=(
            f"✓ Базис заказа #{order.id} обновлён: **{mode_label} · {inc}**\n"
            f"Товары: ${items_total:,.2f} · Доставка ({inc}): ${ship_total:,.2f}\n"
            f"**Итого landed: ${landed:,.2f}** · резерв 10%: ${reserve:,.2f}"
            + ("" if enough else
               f"\n⚠️ Депозит ${wallet.balance:,.0f} — не хватает ${reserve - wallet.balance:,.0f}")
        ),
        actions=(
            [{"label": f"💳 Списать резерв ${reserve:,.0f}",
              "action": "pay_reserve", "params": {"order_id": order.id}}]
            if enough else
            [{"label": "Пополнить депозит",
              "action": "topup_wallet",
              "params": {"amount": float(max((reserve - wallet.balance) * 2, 5000)),
                          "pending_order_id": order.id}}]
        ) + [
            {"label": "🚚 Изменить вариант доставки",
             "action": "shipping_choose", "params": {"order_id": order.id}},
        ],
    )


@register("pay_reserve")
def pay_reserve(params, user, role):
    """Списывает резерв с депозита (Wallet) и переводит заказ в производство.

    Двухступенчатая схема (по ТЗ): без `confirmed=true` возвращает черновик
    (DraftCard) с предупреждениями. Только после явного подтверждения —
    реальное списание.
    """
    from django.db import transaction
    from django.utils import timezone

    from marketplace.models import Order

    from .models import Wallet

    order_id = params.get("order_id")
    if not order_id:
        return ActionResult(text="Не указан заказ.")
    try:
        order = Order.objects.get(id=order_id, buyer=user)
    except Order.DoesNotExist:
        return ActionResult(text=f"Заказ #{order_id} не найден.")

    if order.payment_status != "awaiting_reserve":
        return ActionResult(
            text=f"По заказу #{order.id} резерв уже списан ({order.get_payment_status_display()}).",
        )

    wallet = Wallet.for_user(user)
    amount = order.reserve_amount

    if wallet.balance < amount:
        shortage = amount - wallet.balance
        return ActionResult(
            text=(
                f"❌ Недостаточно средств для списания резерва.\n"
                f"Нужно: ${amount:,.2f} · на счёте: ${wallet.balance:,.2f} · "
                f"не хватает: ${shortage:,.2f}."
            ),
            actions=[
                {"label": f"Пополнить депозит на ${max(shortage * 2, 10000):,.0f}",
                 "action": "topup_wallet",
                 "params": {"amount": float(max(shortage * 2, 10000)),
                             "pending_order_id": order.id}},
                {"label": "Баланс депозита", "action": "get_balance", "params": {}},
            ],
        )

    # ── ШАГ 1: показ черновика, если ещё не подтверждено ──
    if not params.get("confirmed"):
        balance_after = wallet.balance - amount
        warnings = []
        if balance_after < amount:
            warnings.append(
                f"После списания остаток будет ${balance_after:,.0f} — этого "
                f"может не хватить на следующий платёж."
            )
        return ActionResult(
            text=(
                f"Готовлю списание резерва по заказу #{order.id}. "
                f"Деньги уйдут с депозита в эскроу платформы и удерживаются "
                f"до подтверждения готовности к отгрузке."
            ),
            cards=[{
                "type": "draft",
                "data": {
                    "title": f"Подтвердите списание резерва по заказу #{order.id}",
                    "rows": [
                        {"label": "Заказ", "value": f"#{order.id} · {order.customer_name or '—'}"},
                        {"label": "Сумма заказа", "value": f"${order.total_amount:,.2f}"},
                        {"label": "Резерв 10%", "value": f"${amount:,.2f}", "primary": True},
                        {"label": "Депозит сейчас", "value": f"${wallet.balance:,.2f}"},
                        {"label": "После списания", "value": f"${balance_after:,.2f}"},
                    ],
                    "warnings": warnings,
                    "confirm_action": "pay_reserve",
                    "confirm_label": f"💳 Списать ${amount:,.0f}",
                    "confirm_params": {"order_id": order.id, "confirmed": True},
                    "cancel_label": "Отмена",
                },
            }],
            suggestions=["Изменить заказ", "Какой остаток после?"],
        )

    # SECURITY P0-5: double-spend защита через select_for_update + re-check
    # под блокировкой. Без этого два одновременных клика «Оплатить резерв»
    # (двойной клик, две вкладки, ретрай на медленном WS) проходят check
    # одновременно и списывают деньги дважды.
    from . import payments as _pay
    with transaction.atomic():
        order = (Order.objects.select_for_update()
                 .select_related().get(id=order.id, buyer=user))
        # Re-check под блокировкой — статус мог поменяться
        if order.payment_status != "awaiting_reserve":
            return ActionResult(
                text=f"Резерв по заказу #{order.id} уже списан.",
            )
        wallet = (Wallet.objects.select_for_update()
                  .get(pk=wallet.pk))
        if wallet.balance < amount:
            return ActionResult(text="Недостаточно средств (перепроверка).")
        intent = _pay.create_payment_intent(amount, order_id=order.id, payer=user, kind="reserve")
        intent = _pay.confirm_payment_intent(intent, user)
        order.payment_status = "reserve_paid"
        order.status = "reserve_paid"
        order.reserve_paid_at = timezone.now()
        # Авто-триггер «Предоплата поступила» — фиксируется автоматически
        # при переходе в reserve_paid (ТЗ §3: тип = Автомат).
        _meta = order.logistics_meta or {}
        _tg = _meta.get("triggers") or {}
        _tg.setdefault("reserve_paid", {})["payment_received"] = (
            timezone.now().isoformat() + "|auto"
        )
        _meta["triggers"] = _tg
        order.logistics_meta = _meta
        order.save(update_fields=["payment_status", "status", "reserve_paid_at", "logistics_meta"])
    wallet.refresh_from_db(fields=["balance"])
    _log_event(order, "reserve_paid", actor=user, source="buyer",
               meta={"amount": float(amount), "balance_after": float(wallet.balance),
                     "intent_id": intent["id"]})
    _notify_seller_of_order(
        order, kind="payment",
        title=f"Резерв оплачен по заказу #{order.id}",
        body=f"Покупатель оплатил резерв ${amount:,.0f}. Можно подтверждать и запускать в производство.",
    )

    return ActionResult(
        text=(
            f"✓ Списано ${amount:,.2f} с депозита по заказу #{order.id}.\n"
            f"Остаток на счёте: ${wallet.balance:,.2f} {wallet.currency}.\n"
            f"Заказ передан поставщику в производство. Следующий платёж — "
            f"после готовности к отгрузке."
        ),
        cards=[{
            "type": "order",
            "data": {
                "id": str(order.id),
                "number": order.id,
                "status": "reserve_paid",
                "status_label": "Резерв оплачен",
                "total": float(order.total_amount),
                "currency": "USD",
                "payment_status": "reserve_paid",
                "payment_status_label": f"Списано ${amount:,.0f} (10%) · остаток ${wallet.balance:,.0f}",
                "wallet_balance": float(wallet.balance),
            },
        }],
        actions=[
            {"label": "📦 Трекинг", "action": "track_order",
             "params": {"order_id": order.id}},
            {"label": "Баланс депозита", "action": "get_balance", "params": {}},
            {"label": "Все мои заказы", "action": "get_orders", "params": {}},
        ],
        suggestions=["Где заказ?", "История списаний", "Когда готовность?"],
    )


# ── Tracking helpers ──────────────────────────────────────

# Pipeline stages в нужном порядке: какие статусы заказа идут друг за другом.
# (status_code, label, eta_days_from_created) — сколько дней с момента создания
# обычно занимает прохождение этого этапа в нашей логистике.
TRACKING_STAGES = [
    ("pending",        "Создан · ожидает оплаты резерва",  0),
    ("reserve_paid",   "Резерв оплачен",                    1),
    ("confirmed",      "Подтверждён поставщиком",           2),
    ("in_production",  "В производстве",                    7),
    ("ready_to_ship",  "Готов к отгрузке",                  10),
    ("transit_abroad", "Транзит (зарубеж)",                 18),
    ("customs",        "Таможня",                           22),
    ("transit_rf",     "Транзит (РФ)",                      26),
    ("issuing",        "Выдача",                            28),
    ("delivered",      "Доставлен",                         29),
    ("completed",      "Завершён",                          30),
]
TRACKING_INDEX = {code: i for i, (code, _, _) in enumerate(TRACKING_STAGES)}


def _log_event(order, event_type: str, actor=None, source="system", meta=None):
    from marketplace.models import OrderEvent
    try:
        OrderEvent.objects.create(
            order=order, event_type=event_type, source=source,
            actor=actor, meta=meta or {},
        )
    except Exception:
        logger.exception("OrderEvent create failed")


def _notify(user, *, kind: str, title: str, body: str = "", url: str = ""):
    """Создаёт Notification + пушит её через WS + дублирует в durable каналы.

    Цепочка:
      1. DB row (всегда — основа inbox)
      2. WebSocket push (если открыта вкладка → toast + badge)
      3. Email + Telegram fanout (если оффлайн — durable каналы)

    Все шаги best-effort: ошибка в одном не ломает остальные.
    """
    if not user:
        return
    notif_id = None
    try:
        from marketplace.models import Notification
        n = Notification.objects.create(
            user=user, kind=kind, title=title[:200], body=body, url=url[:400],
        )
        notif_id = n.id
    except Exception:
        logger.exception("Notification create failed")
    # Realtime push (best-effort)
    try:
        from .consumers import push_notification_to_user
        push_notification_to_user(user.id, {
            "id": notif_id,
            "kind": kind,
            "title": title[:200],
            "body": body,
            "url": url[:400],
        })
    except Exception:
        logger.exception("WS notify push failed")
    # Durable fanout (Email + Telegram)
    try:
        from .channels import fanout_to_durable
        fanout_to_durable(user, kind=kind, title=title[:200], body=body, url=url[:400])
    except Exception:
        logger.exception("durable fanout failed")


def _notify_seller_of_order(order, kind="order", title="", body=""):
    """Уведомить всех продавцов, чьи товары есть в заказе."""
    if not order:
        return
    try:
        from marketplace.models import OrderItem
        seller_ids = set(
            OrderItem.objects.filter(order=order).values_list("part__seller_id", flat=True)
        )
        for sid in seller_ids:
            if not sid:
                continue
            from django.contrib.auth import get_user_model
            try:
                seller = get_user_model().objects.get(id=sid)
                _notify(seller, kind=kind, title=title or f"Событие по заказу #{order.id}",
                        body=body or "", url=f"/chat/?order={order.id}")
            except Exception:
                pass
    except Exception:
        logger.exception("notify_seller failed")


def _build_contextual_actions(order, role: str, user) -> list:
    """Контекстные действия (Уровень 2) — по правилам кода для текущей ситуации.

    Не дублирует обязательные кнопки. Добавляется к ActionResult.contextual_actions.
    Примеры из ТЗ: просрочка → «История SLA», цена выросла → «Сравнить с прошлым»,
    новый поставщик → «Профиль», срочный заказ → «Запросить ускорение».
    """
    items = []
    # Просрочка SLA → история SLA по этому заказу
    if getattr(order, "sla_status", None) == "breached":
        items.append({"label": "📊 История SLA",
                      "action": "get_sla_report", "params": {}})
    # Заказ задержался > 14 дней в текущем статусе → запросить ускорение
    from datetime import timedelta

    from django.utils import timezone
    if order.created_at and (timezone.now() - order.created_at) > timedelta(days=14):
        if order.status not in ("completed", "delivered", "cancelled"):
            items.append({"label": "⚡ Запросить ускорение",
                          "action": "create_claim",
                          "params": {"order_id": order.id, "kind": "delay"}})
    # Buyer на этапе delivered → отзыв о поставщике
    if role == "buyer" and order.status == "delivered":
        items.append({"label": "⭐ Оценить поставщика",
                      "action": "create_claim",
                      "params": {"order_id": order.id, "kind": "feedback"}})
    # Seller на этапе ready_to_ship — документы для отгрузки
    if role == "seller" and order.status == "ready_to_ship":
        items.append({"label": "📄 Документы для отгрузки",
                      "action": "open_url",
                      "params": {"_url": f"/seller/orders/{order.id}/"}})
    return items


def _order_stage_meta(status_code: str, incoterm: str = "FOB") -> dict:
    """Per-order trigger/actor/SLA для продавца.

    Принцип: продавец ВСЕГДА грузит FOB (доезд от своего склада до порта
    отгрузки). Что клиент купил — FOB, CIP или DDP — определяет что будет
    делаться с грузом ПОСЛЕ передачи в порт: зарубежные логисты подхватывают
    и довозят до точки по контракту. Зона ответственности продавца — EXW→FOB.
    """
    if status_code == "ready_to_ship":
        nxt = {
            "FOB": "Покупатель сам забирает груз в порту",
            "CIP": "Зарубежный логист (морем/авто/авиа) → порт прибытия",
            "DDP": "Зарубежный логист + таможня + РФ-логист → дверь покупателя",
        }.get(incoterm, "")
        return {
            "trigger": "Сдача груза в порт отгрузки (EXW → FOB)",
            "actor": "Поставщик",
            "sla": "1-2 рабочих дня — доезд от склада до порта",
            "next_actor": f"Что дальше ({incoterm}): {nxt}" if nxt else "",
        }
    return {}


def _stage_checklist(status_code: str, incoterm: str = "FOB") -> list:
    """Чек-лист продавца. Продавец всегда грузит FOB (доезд до порта).
    Дальше CIP/DDP — задача логистов маркетплейса, не продавца.
    """
    if status_code == "ready_to_ship":
        # Поставщик одинаково грузит FOB независимо от того что купил клиент.
        # Минимум документов: Инвойс + Упаковочный лист + QR-передача в порту.
        return [
            {"id": "invoice",         "label": "Инвойс",                            "type": "upload"},
            {"id": "packing_list",    "label": "Упаковочный лист",                   "type": "upload"},
            {"id": "fob_handoff_qr",  "label": "QR-передача груза в порту (FOB)",    "type": "qr"},
        ]
    return _STAGE_CHECKLISTS.get(status_code, [])


_STAGE_CHECKLISTS = {
    "reserve_paid":   [{"id": "payment_received", "label": "Предоплата 10% зачислена",          "type": "auto"},
                        {"id": "confirm_composition", "label": "Подтвердить состав заказа",      "type": "button"}],
    "confirmed":      [{"id": "production_started", "label": "Запустить производство / комплектование", "type": "button"}],
    "in_production":  [{"id": "packed", "label": "Груз упакован",                                "type": "button"},
                        {"id": "ready_marked", "label": "Отметить готовность к отгрузке",        "type": "button"}],
    "ready_to_ship":  [{"id": "qr_scan_all", "label": "QR-скан всех мест",                       "type": "qr"},
                        {"id": "transport_invoice", "label": "Транспортная накладная",            "type": "upload"},
                        {"id": "packing_list", "label": "Упаковочный лист",                       "type": "upload"},
                        {"id": "certificates", "label": "Сертификаты",                            "type": "upload"},
                        {"id": "invoice", "label": "Инвойс",                                      "type": "upload"}],
    "transit_abroad": [{"id": "arrived_customs", "label": "Груз прибыл на таможню",              "type": "button"}],
    "customs":        [{"id": "declaration", "label": "Декларация загружена",                    "type": "upload"},
                        {"id": "cleared", "label": "Груз растаможен",                             "type": "button"}],
    "transit_rf":     [{"id": "qr_rf", "label": "QR-скан передачи в РФ",                          "type": "qr"},
                        {"id": "ttn_rf", "label": "ТТН / счёт-фактура",                           "type": "upload"}],
    "issuing":        [{"id": "qr_issuing", "label": "QR-скан выдачи",                            "type": "qr"}],
    "delivered":      [{"id": "qr_received", "label": "QR-скан приёмки",                          "type": "qr"},
                        {"id": "signed_docs", "label": "Подписанные накладные",                   "type": "upload"}],
}


@register("complete_trigger")
def complete_trigger(params, user, role):
    """Помечает триггер этапа как выполненный (QR-скан, загрузка документа и т.п.).

    params: {order_id, status, trigger_id}
    Сохраняет timestamp в Order.logistics_meta['triggers'][status][trigger_id].
    """
    from django.utils import timezone

    from marketplace.models import Order, OrderItem
    order_id = params.get("order_id")
    status = params.get("status") or ""
    trigger_id = params.get("trigger_id") or ""
    if not (order_id and status and trigger_id):
        return ActionResult(text="Не указаны order_id / status / trigger_id.")
    try:
        order = Order.objects.get(id=order_id)
    except Order.DoesNotExist:
        return ActionResult(text=f"Заказ #{order_id} не найден.")
    # Только продавец/оператор по заказу с его позициями
    if role == "buyer":
        return ActionResult(text="Триггеры закрывает поставщик / оператор.")
    if role == "seller":
        from .seller_actions import _effective_seller
        user = _effective_seller(user)
        if not OrderItem.objects.filter(order_id=order_id, part__seller=user).exists():
            return ActionResult(text=f"Заказ #{order_id} не содержит ваших товаров.")
    if order.status != status:
        return ActionResult(
            text=f"⚠️ Заказ #{order.id} уже не в статусе «{status}» (текущий: {order.status}).",
        )
    meta = order.logistics_meta or {}
    triggers = meta.get("triggers") or {}
    stage_triggers = triggers.get(status) or {}
    if stage_triggers.get(trigger_id):
        return ActionResult(text=f"Триггер «{trigger_id}» уже отмечен ранее.")
    stage_triggers[trigger_id] = timezone.now().isoformat()
    triggers[status] = stage_triggers
    meta["triggers"] = triggers
    order.logistics_meta = meta
    order.save(update_fields=["logistics_meta"])
    _log_event(order, "trigger_completed", actor=user, source=role,
               meta={"status": status, "trigger_id": trigger_id})
    # Сколько ещё осталось
    required = _stage_checklist(status)
    done_ids = set(stage_triggers.keys())
    remaining = [t for t in required if t["id"] not in done_ids]
    if remaining:
        return ActionResult(
            text=(f"✓ «{trigger_id}» отмечен.\n"
                   f"Осталось {len(remaining)}: " + ", ".join(t["label"] for t in remaining[:5])),
            actions=[{"label": "📦 Очередь продавца", "action": "seller_pipeline", "params": {}}],
        )
    return ActionResult(
        text=(f"✅ Все триггеры этапа «{status}» выполнены — можно нажать кнопку перехода."),
        actions=[{"label": "📦 Очередь продавца", "action": "seller_pipeline", "params": {}}],
    )


@register("seller_demand_payment")
def seller_demand_payment(params, user, role):
    """Продавец даёт покупателю 24ч на оплату резерва.

    Заказ остаётся в статусе awaiting_reserve, но фиксируется deadline.
    Если резерв не пришёл к deadline → заказ можно автоматически отменить
    (отдельная очистка). Покупателю отправляется уведомление.
    """
    from datetime import timedelta

    from django.utils import timezone

    from marketplace.models import Order, OrderItem
    from .seller_actions import _effective_seller
    order_id = params.get("order_id")
    if not order_id:
        return ActionResult(text="Не указан ID заказа.")
    try:
        order = Order.objects.get(id=order_id)
    except Order.DoesNotExist:
        # SECURITY: одинаковый текст для not-found и not-yours — защита от enumeration leak.
        return ActionResult(text=f"Заказ #{order_id} не содержит ваших товаров.")
    # SECURITY P0-3: ownership-check — продавец не может трогать чужой заказ.
    seller_user = _effective_seller(user)
    if not OrderItem.objects.filter(order=order, part__seller=seller_user).exists():
        return ActionResult(text=f"Заказ #{order_id} не содержит ваших товаров.")
    if order.payment_status != "awaiting_reserve":
        return ActionResult(
            text=f"❌ Заказ #{order.id} не в статусе ожидания оплаты ({order.get_payment_status_display()}).",
        )
    deadline = timezone.now() + timedelta(hours=24)
    meta = order.logistics_meta or {}
    meta["payment_deadline"] = deadline.isoformat()
    meta["payment_demanded_by"] = user.username
    meta["payment_demanded_at"] = timezone.now().isoformat()
    order.logistics_meta = meta
    order.save(update_fields=["logistics_meta"])
    _log_event(order, "payment_deadline_set", actor=user, source="seller",
               meta={"deadline": deadline.isoformat(), "hours": 24})
    # Уведомление покупателю
    if order.buyer:
        from .notifications import notify_user
        try:
            notify_user(
                order.buyer,
                title=f"⏰ Дедлайн оплаты по заказу #{order.id}",
                body=(f"Продавец установил дедлайн 24 часа на оплату резерва "
                      f"${order.reserve_amount:,.0f}. После {deadline.strftime('%d.%m %H:%M')} "
                      f"заказ может быть отменён."),
                kind="payment",
            )
        except Exception:
            pass
    return ActionResult(
        text=(
            f"⏰ Установлен дедлайн оплаты по заказу #{order.id}: "
            f"{deadline.strftime('%d.%m.%Y %H:%M')} (24 часа).\n"
            f"Покупатель уведомлён. Если резерв не придёт — отмените вручную."
        ),
        actions=[
            {"label": "📦 Очередь продавца", "action": "seller_pipeline", "params": {}},
        ],
    )


@register("seller_cancel_pending")
def seller_cancel_pending(params, user, role):
    """Продавец отменяет неоплаченный заказ (резерв ещё не списан)."""
    from marketplace.models import Order, OrderItem
    from .seller_actions import _effective_seller
    order_id = params.get("order_id")
    if not order_id:
        return ActionResult(text="Не указан ID заказа.")
    try:
        order = Order.objects.get(id=order_id)
    except Order.DoesNotExist:
        # SECURITY: одинаковый ответ для not-found и not-yours (enum-leak protection).
        return ActionResult(text=f"Заказ #{order_id} не содержит ваших товаров.")
    # SECURITY P0-3: ownership-check — продавец удаляет только свои заказы.
    seller_user = _effective_seller(user)
    if not OrderItem.objects.filter(order=order, part__seller=seller_user).exists():
        return ActionResult(text=f"Заказ #{order_id} не содержит ваших товаров.")
    if order.payment_status != "awaiting_reserve":
        return ActionResult(
            text=f"❌ Заказ #{order.id} уже оплачен — отмена через спор.",
        )
    total = order.total_amount or 0
    _log_event(order, "order_cancelled_by_seller", actor=user, source="seller",
               meta={"total": float(total), "reason": "unpaid_reserve"})
    # Уведомление покупателю
    if order.buyer:
        from .notifications import notify_user
        try:
            notify_user(
                order.buyer,
                title=f"❌ Заказ #{order.id} отменён продавцом",
                body=f"Заказ на ${total:,.0f} отменён продавцом — резерв не был оплачен в срок.",
                kind="order",
            )
        except Exception:
            pass
    order_num = f"ORD-{order.id}"
    order.delete()
    return ActionResult(
        text=f"✓ Заказ {order_num} (${total:,.0f}) отменён.",
        actions=[
            {"label": "📦 Очередь продавца", "action": "seller_pipeline", "params": {}},
        ],
    )


@register("cancel_order")
def cancel_order(params, user, role):
    """Отменить заказ, если резерв ещё не списан.

    Доступно только покупателю и только пока `payment_status == "awaiting_reserve"`.
    Удаляет Order + OrderItem (запись не понадобится — заказ был черновиком).
    После оплаты резерва отмена через эту функцию запрещена (тогда —
    через спор/возврат).
    """
    from marketplace.models import Order
    order_id = params.get("order_id")
    if not order_id:
        return ActionResult(text="Не указан ID заказа.")
    try:
        order = Order.objects.get(id=order_id, buyer=user)
    except Order.DoesNotExist:
        return ActionResult(text=f"Заказ #{order_id} не найден или не принадлежит вам.")
    if role != "buyer":
        return ActionResult(
            text="❌ Отмена заказа доступна только покупателю.",
        )
    if order.payment_status != "awaiting_reserve":
        return ActionResult(
            text=(
                f"❌ Заказ #{order.id} нельзя отменить — резерв уже списан "
                f"({order.get_payment_status_display()}).\n"
                f"Для возврата — создайте спор или рекламацию."
            ),
        )
    total = order.total_amount or 0
    _log_event(order, "order_cancelled_by_buyer", actor=user, source="buyer",
               meta={"total": float(total)})
    order_num = f"ORD-{order.id}"
    order.delete()  # OrderItem'ы каскадно удалятся
    return ActionResult(
        text=f"✓ Заказ {order_num} (${total:,.0f}) отменён.",
        actions=[
            {"label": "📦 Мои заказы", "action": "get_orders", "params": {}},
            {"label": "🔍 Новый поиск", "action": "open_url", "params": {"_url": "/chat/"}},
        ],
    )


@register("seller_dashboard")
def seller_dashboard(params, user, role):
    """Главная сводка продавца: KPI, новые RFQ, активные заказы, рейтинг.

    Аналог /seller/dashboard/, но в чате — пять KPI-блоков и кнопки на
    самые частые действия.
    """
    from datetime import timedelta
    from decimal import Decimal

    from django.db.models import Sum
    from django.utils import timezone

    from marketplace.models import RFQ, Order, OrderItem, Part

    from .seller_actions import _effective_seller
    user = _effective_seller(user)
    now = timezone.now()
    week_ago = now - timedelta(days=7)
    month_ago = now - timedelta(days=30)

    # Активные заказы с моими товарами
    my_orders_qs = (
        Order.objects.filter(items__part__seller=user)
        .exclude(status__in=["cancelled", "completed"])
        .distinct()
    )
    active_orders = my_orders_qs.count()
    in_production = my_orders_qs.filter(status="in_production").count()
    ready_to_ship = my_orders_qs.filter(status="ready_to_ship", payment_status="paid").count()
    in_transit = my_orders_qs.filter(status__in=["transit_abroad", "customs", "transit_rf", "issuing"]).count()

    # Выручка (по моим OrderItem за период)
    revenue_month = OrderItem.objects.filter(
        part__seller=user, order__created_at__gte=month_ago,
        order__status__in=["completed", "delivered", "issuing", "transit_rf", "customs", "transit_abroad", "ready_to_ship"],
    ).aggregate(s=Sum("unit_price"))["s"] or Decimal("0")

    # Новые RFQ за неделю (всего открытых в системе — для seller'a это входящие)
    new_rfqs = RFQ.objects.filter(
        status__in=["new", "processing"], created_at__gte=week_ago,
    ).count()
    open_rfqs = RFQ.objects.filter(status__in=["new", "processing"]).count()

    # Каталог
    catalog_size = Part.objects.filter(seller=user, is_active=True).count()

    # SLA / рейтинг — упрощённо: доля заказов on_track
    total_with_sla = my_orders_qs.exclude(sla_status="").count() or 1
    on_track = my_orders_qs.filter(sla_status="on_track").count()
    sla_pct = round(on_track / total_with_sla * 100)

    rating = "—"
    profile = getattr(user, "profile", None) or getattr(user, "userprofile", None)
    if profile and getattr(profile, "rating_score", None) is not None:
        rating = f"{profile.rating_score:.1f}"

    text = (
        f"📊 Сводка продавца за неделю\n\n"
        f"• Активных заказов: {active_orders} "
        f"(в производстве: {in_production}, готовы к отгрузке: {ready_to_ship}, в пути: {in_transit})\n"
        f"• Новых RFQ за неделю: {new_rfqs}, всего открытых: {open_rfqs}\n"
        f"• Выручка за 30 дней: ${revenue_month:,.0f}\n"
        f"• Каталог: {catalog_size} позиций · SLA: {sla_pct}% on-track · Рейтинг: {rating}"
    )

    # Дашборд — хаб для всех разделов кабинета продавца
    next_actions = [
        {"label": "🔥 Срочное",      "action": "seller_inbox",        "params": {}},
        {"label": "🚚 К отгрузке",   "action": "seller_pipeline",     "params": {}},
        {"label": "📋 RFQ inbox",    "action": "get_rfq_status",      "params": {}},
        {"label": "💬 Переговоры",   "action": "seller_negotiations", "params": {}},
        {"label": "📦 Каталог",      "action": "seller_catalog",      "params": {}},
        {"label": "💰 Финансы",      "action": "seller_finance",      "params": {}},
        {"label": "📈 Спрос",        "action": "get_demand_report",   "params": {}},
        {"label": "⭐ Рейтинг",      "action": "seller_rating",       "params": {}},
        {"label": "🚛 Логистика",    "action": "seller_logistics",    "params": {}},
        {"label": "🔍 QR-контроль",  "action": "seller_qr",           "params": {}},
        {"label": "👥 Команда",      "action": "seller_team",         "params": {}},
        {"label": "📐 Чертежи",      "action": "seller_drawings",     "params": {}},
        {"label": "🔌 Интеграции",   "action": "seller_integrations", "params": {}},
        {"label": "📑 Отчёты",       "action": "seller_reports",      "params": {}},
    ]
    return ActionResult(
        text=text,
        cards=[{
            "type": "kpi_grid",
            "data": {
                "title": "Сводка продавца",
                "kpis": [
                    {"label": "Активные заказы", "value": active_orders,
                     "sub": f"{in_production} в произв. · {ready_to_ship} к отгр. · {in_transit} в пути"},
                    {"label": "Выручка 30д", "value": f"${revenue_month:,.0f}",
                     "sub": "по проданным позициям"},
                    {"label": "Открытые RFQ", "value": open_rfqs,
                     "sub": f"+{new_rfqs} за неделю"},
                    {"label": "Каталог", "value": catalog_size,
                     "sub": "активных карточек"},
                    {"label": "SLA on-track", "value": f"{sla_pct}%",
                     "sub": f"{on_track} из {total_with_sla}"},
                    {"label": "Рейтинг", "value": rating,
                     "sub": "профиль продавца"},
                ],
            },
        }],
        actions=next_actions,
        suggestions=[
            "Что отгрузить сегодня?",
            "Какие RFQ ждут ответа?",
            "Спрос за неделю",
            "Финансовая сводка",
        ],
    )


@register("seller_finance")
def seller_finance(params, user, role):
    """Финансы продавца: выручка, ожидающие выплаты, депозит."""
    from datetime import timedelta
    from decimal import Decimal

    from django.db.models import Sum
    from django.utils import timezone

    from marketplace.models import OrderItem

    from .models import Wallet
    from .seller_actions import _effective_seller
    user = _effective_seller(user)

    now = timezone.now()
    month_ago = now - timedelta(days=30)
    week_ago = now - timedelta(days=7)

    # Выручка по этапам
    completed_rev = OrderItem.objects.filter(
        part__seller=user, order__status__in=["completed", "delivered"]
    ).aggregate(s=Sum("unit_price"))["s"] or Decimal("0")
    pending_rev = OrderItem.objects.filter(
        part__seller=user, order__status__in=["ready_to_ship", "transit_abroad",
                                                "customs", "transit_rf", "issuing"]
    ).aggregate(s=Sum("unit_price"))["s"] or Decimal("0")
    in_production_rev = OrderItem.objects.filter(
        part__seller=user, order__status__in=["confirmed", "in_production", "reserve_paid"]
    ).aggregate(s=Sum("unit_price"))["s"] or Decimal("0")

    rev_month = OrderItem.objects.filter(
        part__seller=user, order__created_at__gte=month_ago,
        order__status__in=["completed", "delivered", "issuing", "transit_rf",
                           "customs", "transit_abroad", "ready_to_ship"],
    ).aggregate(s=Sum("unit_price"))["s"] or Decimal("0")

    rev_week = OrderItem.objects.filter(
        part__seller=user, order__created_at__gte=week_ago,
        order__status__in=["completed", "delivered", "issuing", "transit_rf",
                           "customs", "transit_abroad", "ready_to_ship"],
    ).aggregate(s=Sum("unit_price"))["s"] or Decimal("0")

    wallet = Wallet.for_user(user)

    text = (
        f"💰 Финансы\n\n"
        f"• Выручка за 7 дней: ${rev_week:,.0f}\n"
        f"• Выручка за 30 дней: ${rev_month:,.0f}\n"
        f"• К получению (в пути / готов к отгрузке): ${pending_rev:,.0f}\n"
        f"• В производстве (ещё не отгружено): ${in_production_rev:,.0f}\n"
        f"• Завершённые продажи (доставленные): ${completed_rev:,.0f}\n"
        f"• Депозит на счёте: ${wallet.balance:,.2f} {wallet.currency}"
    )

    return ActionResult(
        text=text,
        cards=[{
            "type": "kpi_grid",
            "data": {
                "title": "Финансы продавца",
                "kpis": [
                    {"label": "Выручка 7д",   "value": f"${rev_week:,.0f}"},
                    {"label": "Выручка 30д",  "value": f"${rev_month:,.0f}"},
                    {"label": "К получению",  "value": f"${pending_rev:,.0f}",
                     "sub": "в транзите / готовы"},
                    {"label": "В работе",     "value": f"${in_production_rev:,.0f}",
                     "sub": "в производстве"},
                    {"label": "Завершено",    "value": f"${completed_rev:,.0f}",
                     "sub": "доставленные"},
                    {"label": "Депозит",      "value": f"${wallet.balance:,.0f}"},
                ],
            },
        }],
        actions=[
            {"label": "История депозита", "action": "get_balance", "params": {}},
            {"label": "🚚 К отгрузке",   "action": "seller_pipeline", "params": {}},
            {"label": "📊 Дашборд",      "action": "seller_dashboard", "params": {}},
        ],
        suggestions=["Когда выплата?", "Депозит", "Выручка по месяцам"],
    )


@register("seller_rating")
def seller_rating(params, user, role):
    """Рейтинг продавца + последние отзывы (упрощённая версия /seller/rating/)."""

    from marketplace.models import Order, OrderClaim

    from .seller_actions import _effective_seller
    user = _effective_seller(user)

    profile = getattr(user, "profile", None) or getattr(user, "userprofile", None)
    rating = float(profile.rating_score) if profile and getattr(profile, "rating_score", None) else None
    external = float(profile.external_score) if profile and getattr(profile, "external_score", None) else None
    behavioral = float(profile.behavioral_score) if profile and getattr(profile, "behavioral_score", None) else None
    supplier_status = (profile.get_supplier_status_display() if profile else "—") or "—"

    # Жалобы и SLA-нарушения по моим заказам
    my_orders_ids = list(
        Order.objects.filter(items__part__seller=user).values_list("id", flat=True).distinct()
    )
    claims_n = OrderClaim.objects.filter(order_id__in=my_orders_ids).count() if hasattr(OrderClaim, "order_id") else 0
    breaches = Order.objects.filter(id__in=my_orders_ids, sla_status="breached").count()

    text = (
        f"⭐ Рейтинг продавца\n\n"
        f"• Сводный балл: {f'{rating:.1f}' if rating is not None else '—'}\n"
        f"• Статус: {supplier_status}\n"
        f"• Внешний скоринг: {f'{external:.1f}' if external is not None else '—'}\n"
        f"• Поведенческий: {f'{behavioral:.1f}' if behavioral is not None else '—'}\n"
        f"• Жалоб всего: {claims_n} · SLA-нарушений: {breaches}"
    )
    return ActionResult(
        text=text,
        cards=[{
            "type": "kpi_grid",
            "data": {
                "title": "Рейтинг продавца",
                "kpis": [
                    {"label": "Сводный балл", "value": f"{rating:.1f}" if rating else "—"},
                    {"label": "Статус",       "value": supplier_status},
                    {"label": "Внешний",      "value": f"{external:.1f}" if external else "—"},
                    {"label": "Поведение",    "value": f"{behavioral:.1f}" if behavioral else "—"},
                    {"label": "Жалоб",        "value": claims_n},
                    {"label": "SLA-нарушений","value": breaches},
                ],
            },
        }],
        actions=[
            {"label": "Жалобы по моим заказам", "action": "get_claims", "params": {}},
            {"label": "Просрочки SLA",          "action": "get_sla_report", "params": {}},
            {"label": "📊 Дашборд",             "action": "seller_dashboard", "params": {}},
        ],
        suggestions=["Какие жалобы открыты?", "Что с SLA?"],
    )


@register("seller_pipeline")
def seller_pipeline(params, user, role):
    """Очередь продавца: какие его товары и в каких заказах ждут действий.

    Группирует только OrderItem'ы где `part.seller == user` (то есть это
    ИХ товар), показывает по этапам pipeline и сумму. Безопасно для
    больших заказов с миксом продавцов — каждый видит только свою часть.
    """
    from collections import defaultdict
    from decimal import Decimal

    from marketplace.models import OrderItem

    from .seller_actions import _effective_seller
    user = _effective_seller(user)

    items_qs = (
        OrderItem.objects
        .select_related("order", "part", "part__brand")
        .filter(part__seller=user)
        .exclude(order__status__in=["cancelled", "completed"])
        .order_by("-order__created_at")
    )

    # Группируем по статусу заказа: сколько позиций / на сумму
    groups = defaultdict(lambda: {"orders": {}, "items_count": 0, "amount": Decimal("0")})
    total_orders = set()
    for it in items_qs[:200]:
        order = it.order
        st = order.status
        g = groups[st]
        oid = order.id
        if oid not in g["orders"]:
            meta = order.logistics_meta or {}
            triggers_done = dict((meta.get("triggers") or {}).get(order.status) or {})
            # Backfill автоматических триггеров для уже оплаченных заказов
            # (которые попали в reserve_paid до того как мы добавили эту логику).
            if order.status == "reserve_paid" and "payment_received" not in triggers_done:
                triggers_done["payment_received"] = "backfill|auto"
            g["orders"][oid] = {
                "id": oid,
                "buyer": order.customer_name or (order.buyer.username if order.buyer else "—"),
                "items": [],
                "subtotal": Decimal("0"),
                "payment_status": order.payment_status,
                "payment_deadline": meta.get("payment_deadline"),
                "triggers_done": list(triggers_done.keys()),
                "incoterm": order.incoterm or "FOB",
                "status": order.status,
            }
        sub = (Decimal(str(it.unit_price)) * it.quantity).quantize(Decimal("0.01"))
        g["orders"][oid]["items"].append({
            "article": it.part.oem_number,
            "name": it.part.title,
            "brand": it.part.brand.name if it.part.brand else "—",
            "condition": it.part.condition or "oem",
            "qty": it.quantity,
            "unit_price": float(it.unit_price),
            "subtotal": float(sub),
            "weight": f"{it.part.gross_weight_kg} кг" if it.part.gross_weight_kg else "—",
            "stock": getattr(it.part, "stock_quantity", 0) or 0,
            "warehouse": (it.part.warehouse_address or "")[:40],
        })
        g["orders"][oid]["subtotal"] = g["orders"][oid]["subtotal"] + sub
        g["items_count"] += 1
        g["amount"] = g["amount"] + sub
        total_orders.add(oid)

    if not total_orders:
        return ActionResult(
            text="🟢 Очередь пуста — нет открытых заказов с вашими товарами.",
            actions=[
                {"label": "Загрузить прайс-лист", "action": "upload_pricelist", "params": {}},
                {"label": "Спрос на маркетплейсе", "action": "get_demand_report", "params": {}},
            ],
            suggestions=["Что чаще всего ищут?", "Какие RFQ открыты?"],
        )

    # (status, label, btn_label, btn_action, short_chip_label, meta)
    # meta: {trigger, checklist, sla, actor} — описание этапа из ТЗ.
    # checklist: список триггеров — должны быть все выполнены прежде чем
    # можно нажать кнопку перехода на следующий статус.
    STATUS_ORDER = [
        ("reserve_paid",  "💰 Резерв оплачен — подтвердить и в производство", "▶️ Подтвердить",       None,         "Резерв оплачен", {
            "trigger": "Предоплата 10% поступила на счёт платформы",
            "checklist": [
                {"id": "payment_received", "label": "Предоплата 10% зачислена", "type": "auto"},
                {"id": "confirm_composition", "label": "Подтвердить состав заказа", "type": "button"},
            ],
            "actor": "Поставщик",
            "sla": "≤ 2 рабочих дня",
        }),
        ("confirmed",     "✅ Подтверждены — запустить производство",          "▶️ В производство",    None,         "Подтверждён", {
            "trigger": "Поставщик подтвердил наличие и состав",
            "checklist": [
                {"id": "production_started", "label": "Запустить производство / комплектование", "type": "button"},
            ],
            "actor": "Поставщик",
            "sla": "Срок комплектования",
        }),
        ("in_production", "🏭 В производстве — отметить готовность",          "▶️ Готов к отгрузке",  None,         "В производстве", {
            "trigger": "Груз скомплектован, упакован",
            "checklist": [
                {"id": "packed", "label": "Груз упакован", "type": "button"},
                {"id": "ready_marked", "label": "Отметить готовность к отгрузке", "type": "button"},
            ],
            "actor": "Поставщик / склад",
            "sla": "≤ срок производства",
        }),
        ("ready_to_ship", "📦 Готов к отгрузке — оплачено, можно грузить",    "🚚 Отгрузить",         "ship_order", "Готов к отгрузке", {
            "trigger": "FOB: сдача в порт отгрузки (продавец) · CIP/DDP: передача зарубежному перевозчику",
            "checklist": [],  # per-order, см. _stage_checklist()
            "actor": "FOB: продавец · CIP/DDP: зарубежный логист",
            "sla": "FOB: 1-2 дня (доезд до порта) · CIP/DDP: согласно фрахту",
        }),
        # Стадии ниже — после FOB, это зона логистов маркетплейса, не продавца.
        # Оставлены для совместимости, но _SELLER_HIDDEN_STATUSES скрывает их
        # в seller_pipeline (см. фильтр выше).
        ("transit_abroad","🛫 В транзите за рубеж",                            "▶️ На таможню",        None,         "В транзите", {
            "trigger": "Груз прибыл на таможенный пост РФ",
            "checklist": [
                {"id": "arrived_customs", "label": "Груз прибыл на таможню", "type": "button"},
            ],
            "actor": "Зарубежный логист (под контролем оператора)",
            "sla": "По графику перевозки",
        }),
        ("customs",       "🛃 На таможне",                                     "▶️ Транзит по РФ",     None,         "На таможне", {
            "trigger": "Таможня завершена — груз растаможен",
            "checklist": [
                {"id": "declaration", "label": "Декларация загружена", "type": "upload"},
                {"id": "cleared", "label": "Груз растаможен", "type": "button"},
            ],
            "actor": "Таможенный брокер (под контролем оператора)",
            "sla": "≤ 3 рабочих дня",
        }),
        ("transit_rf",    "🚛 Транзит по РФ",                                  "▶️ К выдаче",          None,         "Транзит РФ", {
            "trigger": "Груз передан в логистику РФ",
            "checklist": [
                {"id": "qr_rf", "label": "QR-скан передачи в РФ", "type": "qr"},
                {"id": "ttn_rf", "label": "ТТН / счёт-фактура", "type": "upload"},
            ],
            "actor": "РФ-логист (под контролем оператора)",
            "sla": "≤ 1 рабочий день",
        }),
        ("issuing",       "📬 На выдаче",                                      "▶️ Доставлен",         None,         "На выдаче", {
            "trigger": "Груз готов к выдаче в пункте самовывоза / у курьера",
            "checklist": [
                {"id": "qr_issuing", "label": "QR-скан выдачи", "type": "qr"},
            ],
            "actor": "Пункт выдачи / РФ-логист",
            "sla": "≤ 1 рабочий день",
        }),
        ("delivered",     "🏁 Доставлен — ждём приёмки покупателя",           None,                    None,         "Доставлен", {
            "trigger": "Фактическая приёмка груза покупателем",
            "checklist": [
                {"id": "qr_received", "label": "QR-скан приёмки (покупатель)", "type": "qr"},
                {"id": "signed_docs", "label": "Подписанные накладные", "type": "upload"},
            ],
            "actor": "Покупатель (рекламации → оператор)",
            "sla": "Автозакрытие через 1 час после приёмки",
        }),
        ("pending",       "⏳ Ожидает оплаты резерва (на покупателе)",         "📩 Дать 24ч",           "seller_demand_payment", "Ждёт оплаты", {
            "trigger": "Счёт сформирован — ожидаем 10% резерв от покупателя",
            "checklist": [],
            "actor": "Покупатель / система",
            "sla": "15 мин (авто) / 48 ч (ручной) — иначе авто-отмена",
        }),
    ]

    # Зоны ответственности продавца:
    #   1. Активная работа: до FOB-порта (pending → ready_to_ship)
    #   2. Архив отгруженных: после FOB-порта (transit_abroad → delivered).
    #      Продавец не двигает, но видит где находится — для гарантии/claims.
    _SELLER_ACTIVE = {"pending", "reserve_paid", "confirmed", "in_production", "ready_to_ship"}
    _SELLER_ARCHIVE = {"transit_abroad", "customs", "transit_rf", "issuing", "delivered"}
    sections = []
    for code, label, btn, btn_action, short_label, meta in STATUS_ORDER:
        if code in _SELLER_ARCHIVE:
            # Архивные стадии — без button'ов, рендерим отдельным блоком ниже
            continue
        g = groups.get(code)
        if not g or not g["orders"]:
            continue
        orders_list = []
        for o in list(g["orders"].values())[:8]:
            # Per-order checklist — зависит от incoterm заказа (FOB/CIP/DDP).
            o_inc = o.get("incoterm") or "FOB"
            o_status = o.get("status") or code
            order_checklist = _stage_checklist(o_status, o_inc)
            # Stage meta зависит от incoterm — для FOB ready_to_ship это
            # передача в порт продавцом, а не ожидание зарубежного логиста.
            o_stage = _order_stage_meta(o_status, o_inc)
            orders_list.append({
                "id": o["id"],
                "buyer": o["buyer"],
                "items": o["items"],
                "subtotal": float(o["subtotal"]),
                "payment_status": o["payment_status"],
                "payment_deadline": o.get("payment_deadline"),
                "triggers_done": o.get("triggers_done", []),
                "incoterm": o_inc,
                "checklist": order_checklist,
                "stage_meta": o_stage,
                # Клик по карточке заказа → открыть деталь
                "open_action": {
                    "action": "get_order_detail",
                    "params": {"order_id": o["id"]},
                },
            })
        sections.append({
            "status": code,
            "label": label,
            "short_label": short_label,
            "btn": btn,
            "btn_action": btn_action or "advance_order",
            "orders_count": len(g["orders"]),
            "items_count": g["items_count"],
            "amount": float(g["amount"]),
            "actionable": btn is not None,
            "orders": orders_list,
            # Триггеры/документы/SLA — ТЗ «Этапы ЛК»
            "trigger": meta.get("trigger", ""),
            "checklist": meta.get("checklist", []),
            "docs": meta.get("docs", []),
            "actor": meta.get("actor", ""),
            "sla": meta.get("sla", ""),
        })

    # Архивные секции — что вы уже отгрузили (без действий, только статус)
    archive_sections = []
    # Актёры по цепочке. Оператор маркетплейса не «на выдаче» — он
    # КООРДИНИРУЕТ всю цепочку (логистов, брокеров, платежи, рекламации).
    # На самой выдаче — РФ-логист / пункт самовывоза.
    _NEXT_ACTOR_LABEL = {
        "transit_abroad": "Зарубежный логист везёт до порта прибытия (контролирует оператор)",
        "customs":        "Таможенный брокер оформляет растаможку (контролирует оператор)",
        "transit_rf":     "РФ-логист везёт до пункта выдачи (контролирует оператор)",
        "issuing":        "Пункт выдачи / РФ-логист передаёт покупателю",
        "delivered":      "Покупатель принял груз — гарантия активна (рекламации → оператор)",
    }
    for code, label, _btn, _ba, short_label, meta in STATUS_ORDER:
        if code not in _SELLER_ARCHIVE:
            continue
        g = groups.get(code)
        if not g or not g["orders"]:
            continue
        archive_orders = []
        for o in list(g["orders"].values())[:8]:
            archive_orders.append({
                "id": o["id"],
                "buyer": o["buyer"],
                "items": o["items"],
                "subtotal": float(o["subtotal"]),
                "payment_status": o["payment_status"],
                "incoterm": o.get("incoterm", "FOB"),
                "current_actor": _NEXT_ACTOR_LABEL.get(code, ""),
                "open_action": {
                    "action": "get_order_detail",
                    "params": {"order_id": o["id"]},
                },
            })
        archive_sections.append({
            "status": code,
            "label": label,
            "short_label": short_label,
            "orders_count": len(g["orders"]),
            "items_count": g["items_count"],
            "amount": float(g["amount"]),
            "orders": archive_orders,
        })

    text = (
        f"🔧 В вашей очереди — {len(total_orders)} заказа(ов).\n"
        f"Ваша зона: довезти от склада до FOB-порта + передать пакет документов. "
        f"Дальше всю цепочку (зарубежный логист → таможня → РФ-логист → пункт выдачи) "
        f"координирует оператор маркетплейса. После приёмки покупателем — вы отвечаете "
        f"только за качество, комплектность и гарантию. Рекламации идут через оператора."
    )

    # Кнопки next-action для самого срочного этапа
    next_actions = []
    for sec in sections:
        if sec["actionable"]:
            first_oid = sec["orders"][0]["id"]
            next_actions.append({
                "label": f"{sec['btn']} (#{first_oid})",
                "action": sec["btn_action"],
                "params": {"order_id": first_oid},
            })
            break
    next_actions.append({"label": "📤 Загрузить прайс", "action": "upload_pricelist", "params": {}})
    next_actions.append({"label": "📊 Спрос", "action": "get_demand_report", "params": {}})

    return ActionResult(
        text=text,
        cards=[{
            "type": "seller_queue",
            "data": {
                "title": "Ваш кусок: до FOB-порта",
                "total_orders": len(total_orders),
                "sections": sections,
                "archive_sections": archive_sections,
                "archive_title": "📤 Отгружено — оператор маркетплейса ведёт до клиента (ваша гарантия активна)",
            },
        }],
        actions=next_actions,
        suggestions=["Двинь #" + str(next(iter(total_orders))), "Спрос на рынке", "Что ещё в очереди?"],
    )


@register("ship_order")
def ship_order(params, user, role):
    """Отгрузка заказа поставщиком.

    Двухфазный action:
      1. Без tracking_number → показывает inline-форму ввода
      2. С tracking_number → проводит отгрузку, пишет в logistics_meta,
         двигает статус ready_to_ship → transit_abroad, OrderEvent.

    Только для seller'а: проверяется наличие его товаров в заказе.
    """
    from django.utils import timezone

    from marketplace.models import Order, OrderItem

    from .seller_actions import _effective_seller
    # Маппим тест-юзеров на demo_seller — иначе клик в seller_inbox даст
    # «не содержит ваших товаров», т.к. items принадлежат demo_seller.
    user = _effective_seller(user)

    order_id = params.get("order_id")
    if not order_id:
        return ActionResult(text="Не указан заказ.")
    try:
        order_id = int(order_id)
    except (TypeError, ValueError):
        return ActionResult(text="Некорректный ID заказа.")

    # Проверка прав: в заказе должны быть товары seller'a
    if role == "seller":
        if not OrderItem.objects.filter(order_id=order_id, part__seller=user).exists():
            return ActionResult(
                text=f"Заказ #{order_id} не содержит ваших товаров — отгружать его не можете."
            )

    try:
        order = Order.objects.get(id=order_id)
    except Order.DoesNotExist:
        return ActionResult(text=f"Заказ #{order_id} не найден.")

    # Проверка статуса
    if order.status != "ready_to_ship":
        return ActionResult(
            text=(
                f"Отгрузить заказ #{order.id} нельзя — он в статусе "
                f"«{order.get_status_display()}». Отгрузка возможна только "
                f"со статуса «Готов к отгрузке»."
            ),
            actions=[{"label": "📦 Трекинг", "action": "track_order",
                      "params": {"order_id": order.id}}],
        )
    if order.payment_status != "paid":
        return ActionResult(
            text=(
                f"Заказ #{order.id} не может быть отгружен: остаток 90% "
                f"ещё не оплачен покупателем."
            ),
        )

    tracking      = (params.get("tracking_number") or "").strip()
    carrier       = (params.get("carrier") or "").strip() or "Self"
    carrier_phone = (params.get("carrier_phone") or "").strip()
    carrier_email = (params.get("carrier_email") or "").strip()
    carrier_site  = (params.get("carrier_site") or "").strip()
    tracking_url  = (params.get("tracking_url") or "").strip()

    # Шаг 1: запрашиваем tracking, если не передан
    if not tracking:
        return ActionResult(
            text=(
                f"Отгрузка заказа #{order.id} ({order.customer_name}) "
                f"на сумму ${order.total_amount:,.0f}.\n"
                f"Заполните данные перевозчика — они уйдут оператору платформы "
                f"и сохранятся в audit-логе заказа. Прямые контакты юзеру "
                f"не раскрываются (анти-сговор) — связь только через оператора."
            ),
            cards=[{
                "type": "form",
                "data": {
                    "title": f"🚚 Отгрузка заказа #{order.id}",
                    "intent": "Контакты перевозчика нужны оператору, чтобы оперативно решать вопросы по доставке (задержки, повреждения, таможня).",
                    "submit_action": "ship_order",
                    "submit_label": "📨 Отправить",
                    "fields": [
                        {"name": "tracking_number", "label": "Tracking-номер",
                         "placeholder": "например, RA123456789CN",
                         "required": True,
                         "hint": "Номер накладной перевозчика — по нему отслеживается груз"},
                        {"name": "carrier", "label": "Перевозчик (название компании)",
                         "placeholder": "DHL / China Post / EMS / Self",
                         "value": "Self", "required": True},
                        {"name": "tracking_url", "label": "Ссылка на трекинг (URL)",
                         "type": "url",
                         "placeholder": "https://www.dhl.com/ru-en/home/tracking.html",
                         "hint": "Опционально — прямая ссылка где видно текущее местоположение груза"},
                        {"name": "carrier_phone", "label": "Телефон перевозчика",
                         "required": True,
                         "placeholder": "+86 138 0000 1234",
                         "hint": "Контакт диспетчера / линии поддержки — для оператора платформы"},
                        {"name": "carrier_email", "label": "Email перевозчика",
                         "type": "email", "required": True,
                         "placeholder": "support@dhl.com",
                         "hint": "Куда писать по проблемам с грузом"},
                        {"name": "carrier_site", "label": "Сайт перевозчика",
                         "type": "url",
                         "placeholder": "https://www.dhl.com",
                         "hint": "Опционально — для справки в audit-логе"},
                    ],
                    "fixed_params": {"order_id": order.id},
                },
            }],
            actions=[
                {"label": "Отмена", "action": "track_order",
                 "params": {"order_id": order.id}},
            ],
            suggestions=["Какой перевозчик быстрее?", "Сколько идёт DHL?"],
        )

    # Серверная валидация required-полей
    missing = []
    if not carrier_phone: missing.append("телефон перевозчика")
    if not carrier_email: missing.append("email перевозчика")
    if missing:
        return ActionResult(
            text=(
                f"⚠️ Заполните обязательные поля: {', '.join(missing)}. "
                f"Оператор не сможет связаться с перевозчиком если что-то пойдёт не так."
            ),
            actions=[{"label": "← Назад к форме", "action": "ship_order",
                       "params": {"order_id": order.id}}],
        )

    # Шаг 2: реально отгружаем
    meta = dict(order.logistics_meta or {})
    meta.update({
        "tracking_number": tracking,
        "tracking_url":    tracking_url,
        "carrier":         carrier,
        "carrier_phone":   carrier_phone,
        "carrier_email":   carrier_email,
        "carrier_site":    carrier_site,
        "shipped_at":      timezone.now().isoformat(),
        "shipped_by":      user.username,
    })
    update_fields = ["status", "logistics_meta", "logistics_provider"]
    order.status = "transit_abroad"
    order.logistics_meta = meta
    order.logistics_provider = carrier or order.logistics_provider
    # Order.* поля (только если есть в модели — гарды на случай если поля
    # ещё не мигрированы или удалены).
    if hasattr(order, "tracking_number"):
        order.tracking_number = tracking; update_fields.append("tracking_number")
    if hasattr(order, "tracking_url") and tracking_url:
        order.tracking_url = tracking_url; update_fields.append("tracking_url")
    if hasattr(order, "carrier_name"):
        order.carrier_name = carrier; update_fields.append("carrier_name")
    if hasattr(order, "carrier_phone") and carrier_phone:
        order.carrier_phone = carrier_phone; update_fields.append("carrier_phone")
    if hasattr(order, "carrier_email") and carrier_email:
        order.carrier_email = carrier_email; update_fields.append("carrier_email")
    order.save(update_fields=update_fields)
    _log_event(order, "status_changed", actor=user, source="seller",
               meta={"from": "ready_to_ship", "to": "transit_abroad",
                     "tracking_number": tracking,
                     "tracking_url":    tracking_url,
                     "carrier":         carrier,
                     "carrier_phone":   carrier_phone,
                     "carrier_email":   carrier_email,
                     "carrier_site":    carrier_site})
    # Уведомить покупателя об отгрузке
    if order.buyer_id:
        _notify(order.buyer, kind="order",
                title=f"Заказ #{order.id} отгружен",
                body=f"Tracking {tracking} · перевозчик {carrier}. В транзите за рубеж.")
    # + системное сообщение в shipment-чат с обновлённым timeline
    try:
        from .order_events import notify_operator_alert, notify_order_event
        notify_order_event(order, "shipped", actor=user,
            text=(f"🚚 Заказ ORD-{order.id} отгружен!\n"
                  f"Tracking: {tracking} · Перевозчик: {carrier}.\n"
                  f"В транзите за рубеж."))
        # Алерт оператору — со ВСЕМИ контактами перевозчика чтобы он мог
        # оперативно решать вопросы (анти-сговор: эти контакты buyer/seller
        # сами не видят, только оператор).
        op_lines = [
            f"🚚 Отгружен заказ ORD-{order.id} на ${order.total_amount:,.0f}",
            f"Перевозчик: {carrier}",
            f"Tracking: {tracking}",
        ]
        if tracking_url:  op_lines.append(f"URL: {tracking_url}")
        if carrier_phone: op_lines.append(f"Tel: {carrier_phone}")
        if carrier_email: op_lines.append(f"Email: {carrier_email}")
        if carrier_site:  op_lines.append(f"Site: {carrier_site}")
        notify_operator_alert(order=order, event="shipment_started",
                              text="\n".join(op_lines))
    except Exception:
        logger.exception("notify_order_event in ship_order failed")

    return ActionResult(
        text=(
            f"🚚 Заказ #{order.id} отгружен.\n"
            f"Tracking: {tracking} · Перевозчик: {carrier}.\n"
            f"Покупатель уведомлён, статус — «Транзит (зарубеж)»."
        ),
        cards=[{
            "type": "order",
            "data": {
                "id": str(order.id), "number": order.id,
                "status": "transit_abroad",
                "status_label": f"Транзит · {carrier} · {tracking}",
                "total": float(order.total_amount), "currency": "USD",
                "payment_status_label": order.get_payment_status_display(),
            },
        }],
        actions=[
            {"label": "📦 Трекинг", "action": "track_order",
             "params": {"order_id": order.id}},
            {"label": "🚚 К отгрузке", "action": "seller_pipeline", "params": {}},
        ],
        suggestions=["Что отгружать дальше?", "Очередь продавца"],
    )


@register("track_order")
def track_order(params, user, role):
    """Полная карточка отслеживания заказа: progress bar + timeline + ETA."""
    from datetime import timedelta

    from django.utils import timezone

    from marketplace.models import Order, OrderEvent

    order_id = params.get("order_id") or params.get("id")
    if not order_id:
        return ActionResult(text="Не указан ID заказа.")
    # Buyer видит только свой заказ; seller — заказы с его товарами; operator — все
    qs = Order.objects.select_related("buyer")
    if role == "buyer":
        qs = qs.filter(id=order_id, buyer=user)
    elif role == "seller":
        from marketplace.models import OrderItem

        from .seller_actions import _effective_seller
        user = _effective_seller(user)
        if not OrderItem.objects.filter(order_id=order_id, part__seller=user).exists():
            return ActionResult(text=f"Заказ #{order_id} не содержит ваших товаров.")
        qs = qs.filter(id=order_id)
    else:
        qs = qs.filter(id=order_id)
    try:
        order = qs.get()
    except Order.DoesNotExist:
        return ActionResult(text=f"Заказ #{order_id} не найден.")

    current_idx = TRACKING_INDEX.get(order.status, 0)
    created = order.created_at

    stages = []
    for i, (code, label, eta_days) in enumerate(TRACKING_STAGES):
        if i < current_idx:
            state = "done"
        elif i == current_idx:
            state = "current"
        else:
            state = "pending"
        eta = (created + timedelta(days=eta_days)) if eta_days else created
        stages.append({
            "code": code,
            "label": label,
            "state": state,
            "eta": eta.strftime("%d.%m.%Y") if eta else None,
        })

    # Timeline: последние 12 событий
    events = OrderEvent.objects.filter(order=order).order_by("created_at")[:24]
    EVENT_LABELS = {
        "order_created":         "🆕 Заказ создан",
        "status_changed":        "🔁 Статус изменён",
        "sla_status_changed":    "⏱ SLA",
        "invoice_opened":        "🧾 Инвойс открыт",
        "reserve_paid":          "💳 Резерв 10% оплачен",
        "mid_payment_paid":      "💳 Промежуточный платёж",
        "customs_payment_paid":  "💳 Таможенный платёж",
        "final_payment_paid":    "💳 Остаток 90% оплачен",
        "quality_confirmed":     "✅ Качество подтверждено",
        "document_uploaded":     "📄 Документ загружен",
        "claim_opened":          "⚠️ Открыта рекламация",
    }
    timeline = []
    for ev in events:
        when = timezone.localtime(ev.created_at)
        meta = ev.meta or {}
        text = EVENT_LABELS.get(ev.event_type, ev.event_type)
        if ev.event_type == "status_changed" and meta.get("to"):
            text = f"🔁 → {meta['to']}"
        timeline.append({
            "when": when.strftime("%d.%m %H:%M"),
            "text": text,
        })

    progress_pct = int(round(current_idx / max(1, len(TRACKING_STAGES) - 1) * 100))

    eta_total_days = TRACKING_STAGES[-2][2]  # до delivered
    eta_delivery = (created + timedelta(days=eta_total_days)).strftime("%d.%m.%Y")
    days_left = max(0, (created + timedelta(days=eta_total_days) - timezone.now()).days)

    current_label = TRACKING_STAGES[current_idx][1] if current_idx < len(TRACKING_STAGES) else order.get_status_display()

    text = (
        f"📦 Заказ #{order.id} · {current_label}\n"
        f"Сумма: ${order.total_amount:,.0f} · оплата: {order.get_payment_status_display()}\n"
        f"Ожидаемая доставка: {eta_delivery} ({days_left} дн.)"
    )
    # Подсказка для seller: ждём оплату от покупателя
    if role == "seller" and order.status == "ready_to_ship" and order.payment_status != "paid":
        from decimal import Decimal as _D
        rem = (_D(str(order.total_amount)) - _D(str(order.reserve_amount or 0))).quantize(_D("0.01"))
        text += f"\n⏳ Ожидаем от покупателя оплату остатка ${rem:,.0f} (90%) — отгрузка после поступления денег в эскроу."

    # ── Карточка «🚚 Перевозчик» — реальные данные логиста ──────
    # Раньше AI выдумывал «напишите в DHL/UPS» — теперь показываем настоящие
    # контакты + tracking_number + ссылку на статус. Если поля пустые
    # (заказ ещё не отгружен / оператор не заполнил) — пишем явно.
    #
    # Анти-сговор: реальный оператор = staff/admin И не является buyer/seller
    # этого заказа. Покупатель, переключивший UI-toggle в "operator", им НЕ
    # становится — модели в БД источник истины, не клиент. (Без этой защиты
    # buyer мог бы увидеть телефон перевозчика → договориться напрямую.)
    _is_owner_buyer  = (order.buyer_id == user.id)
    _is_owner_seller = order.items.filter(part__seller=user).exists()
    is_real_operator = (
        bool(getattr(user, "is_staff", False) or getattr(user, "is_superuser", False))
        and not _is_owner_buyer
        and not _is_owner_seller
    )
    carrier_items = []
    is_in_transit = order.status in (
        "ready_to_ship", "transit_abroad", "customs", "transit_rf", "issuing",
    )
    if order.carrier_name or order.tracking_number or is_in_transit:
        carrier_items.append({
            "label": "Перевозчик",
            "value": order.carrier_name or "—",
            "tone": "info" if order.carrier_name else "warn",
        })
        if order.tracking_number:
            carrier_items.append({
                "label": "Трек-номер",
                "value": order.tracking_number,
                "sub": ("📋 Скопируйте и проверьте на сайте перевозчика"
                        if not order.tracking_url else ""),
            })
        # Контакты перевозчика (телефон/email) — анти-сговор: показываем
        # ТОЛЬКО реальному оператору (staff/admin, не buyer/seller заказа).
        if is_real_operator:
            if order.carrier_phone:
                carrier_items.append({
                    "label": "Телефон перевозчика",
                    "value": order.carrier_phone,
                })
            if order.carrier_email:
                carrier_items.append({
                    "label": "Email перевозчика",
                    "value": order.carrier_email,
                })
        elif order.carrier_phone or order.carrier_email:
            carrier_items.append({
                "label": "Связь с перевозчиком",
                "value": "Через оператора платформы",
                "sub": "Прямые контакты доступны только оператору.",
            })
        if not (order.carrier_name or order.tracking_number):
            carrier_items.append({
                "label": "Статус",
                "value": "Перевозчик ещё не назначен оператором",
                "tone": "warn",
                "sub": "Связаться с оператором — поможет ускорить.",
            })

    # Контекстные кнопки — разные для buyer и seller.
    # ВАЖНО: ориентируемся не только на UI-toggle role, но и на фактическое
    # отношение к заказу. Если user — buyer этого заказа, показываем
    # buyer-кнопки даже если он переключился в seller-режим (а двигать
    # заказ может только seller, владелец позиций).
    is_owner_buyer = (order.buyer_id == user.id)
    is_owner_seller = order.items.filter(part__seller=user).exists()
    if is_owner_buyer and not is_owner_seller:
        effective_role = "buyer"
    elif is_owner_seller and not is_owner_buyer:
        effective_role = "seller"
    else:
        effective_role = role  # admin/operator или гибридный случай
    actions_list = []
    if effective_role == "buyer":
        if order.payment_status == "awaiting_reserve":
            actions_list.append({
                "label": f"💳 Оплатить резерв ${order.reserve_amount:,.0f}",
                "action": "pay_reserve", "params": {"order_id": order.id},
            })
        elif order.status == "ready_to_ship" and order.payment_status != "paid":
            from decimal import Decimal
            rem = (Decimal(str(order.total_amount)) - Decimal(str(order.reserve_amount or 0))).quantize(Decimal("0.01"))
            actions_list.append({
                "label": f"💳 Оплатить остаток ${rem:,.0f}",
                "action": "pay_final", "params": {"order_id": order.id},
            })
        elif order.status == "delivered":
            actions_list.append({
                "label": "✅ Подтвердить приёмку",
                "action": "confirm_delivery", "params": {"order_id": order.id},
            })
        actions_list.append({"label": "Баланс депозита", "action": "get_balance", "params": {}})
    elif effective_role == "seller":
        # Продавец двигает заказ по pipeline (производство → отгрузка → таможня)
        if order.status in ("reserve_paid", "confirmed", "in_production"):
            actions_list.append({"label": "▶️ Двинуть дальше", "action": "advance_order",
                                 "params": {"order_id": order.id}})
        elif order.status == "ready_to_ship" and order.payment_status == "paid":
            actions_list.append({"label": "🚚 Отгрузить", "action": "ship_order",
                                 "params": {"order_id": order.id}})
        elif order.status == "ready_to_ship" and order.payment_status != "paid":
            # Ждём оплаты от покупателя — действий у seller'a нет
            pass
        elif order.status in ("transit_abroad", "customs", "transit_rf", "issuing"):
            actions_list.append({"label": "▶️ Следующий этап", "action": "advance_order",
                                 "params": {"order_id": order.id}})

    actions_list.append({"label": "Все мои заказы", "action": "get_orders", "params": {}})

    # ── Что должно произойти дальше: явный «next trigger» ──
    from decimal import Decimal as _D
    rem = (_D(str(order.total_amount)) - _D(str(order.reserve_amount or 0))).quantize(_D("0.01"))
    next_actor, next_event = "—", "—"
    if order.payment_status == "awaiting_reserve":
        next_actor = "Покупатель"
        next_event = f"оплачивает резерв 10% (${order.reserve_amount:,.0f})"
    elif order.status == "reserve_paid":
        next_actor = "Поставщик"
        next_event = "подтверждает заказ и принимает в работу"
    elif order.status == "confirmed":
        next_actor = "Поставщик"
        next_event = "запускает производство"
    elif order.status == "in_production":
        next_actor = "Поставщик"
        next_event = "сообщает о готовности к отгрузке"
    elif order.status == "ready_to_ship" and order.payment_status != "paid":
        next_actor = "Покупатель"
        next_event = f"оплачивает остаток 90% (${rem:,.0f})"
    elif order.status == "ready_to_ship":
        next_actor = "Поставщик"
        next_event = "оформляет отгрузку и передаёт перевозчику"
    elif order.status == "transit_abroad":
        next_actor = "Перевозчик"
        next_event = "доставляет груз до границы РФ"
    elif order.status == "customs":
        next_actor = "Таможенный брокер"
        next_event = "проводит таможенное оформление"
    elif order.status == "transit_rf":
        next_actor = "Перевозчик"
        next_event = "везёт груз по России до пункта выдачи"
    elif order.status == "issuing":
        next_actor = "Перевозчик / получатель"
        next_event = "забирает груз с пункта выдачи"
    elif order.status == "delivered":
        next_actor = "Покупатель"
        next_event = "подтверждает приёмку — после этого эскроу выплачивает поставщику"
    elif order.status == "completed":
        next_actor = "—"
        next_event = "Заказ закрыт"

    # Контекстные кнопки: только rule-based. AI proactive отключён в hot-path —
    # round-trip к Anthropic Haiku добавлял 1–2с задержку на каждом открытии
    # карточки заказа. Включить можно через env ENABLE_PROACTIVE_AI=1, но
    # тогда надо обернуть в async/cache.
    ctx_actions = _build_contextual_actions(order, role, user)
    if os.getenv("ENABLE_PROACTIVE_AI", "") == "1":
        try:
            from .proactive import proactive_actions_for
            ai_extra = proactive_actions_for(
                intent=f"track_order:{order.id}",
                context={
                    "order_id": order.id, "status": order.status,
                    "sla_status": order.sla_status,
                    "payment_status": order.payment_status,
                    "total": float(order.total_amount),
                    "tracking_number": (order.logistics_meta or {}).get("tracking_number"),
                    "days_in_progress": days_left,
                },
                max_items=2,
            )
            seen = {(a["action"], json.dumps(a.get("params", {}), sort_keys=True)) for a in ctx_actions}
            for a in ai_extra:
                key = (a["action"], json.dumps(a.get("params", {}), sort_keys=True))
                if key not in seen:
                    ctx_actions.append(a)
        except Exception:
            pass

    # ── Карточка «🚚 Перевозчик / Логист» — ТОЛЬКО для реального оператора ──
    # Анти-сговор: покупатель/продавец не видят ни перевозчика, ни трек-номер,
    # ни контакты. Статус перевозки они и так видят в основной timeline-карточке.
    extra_cards = []
    if carrier_items and is_real_operator:
        extra_cards.append({
            "type": "kpi_grid",
            "data": {
                "title": "🚚 Перевозчик / Логист",
                "items": carrier_items,
            },
        })
    # Прямая кнопка «Открыть трекинг на сайте перевозчика» — только реальному
    # оператору (staff/admin, не buyer/seller). Покупатель не должен знать
    # перевозчика и иметь URL для прямого контакта.
    if order.tracking_url and is_real_operator:
        actions_list.insert(0, {
            "label": f"🔗 Открыть трекинг {order.carrier_name or ''}".strip(),
            "action": "open_url",
            "params": {"url": order.tracking_url},
        })
    elif is_in_transit and not is_real_operator:
        # Покупателю/продавцу — единый CTA «Уточнить у оператора» вне зависимости
        # от того, назначен перевозчик или нет (платформа = единая точка контакта).
        actions_list.insert(0, {
            "label": "💬 Уточнить статус у оператора",
            "action": "contact_operator",
            "params": {
                "topic": "order",
                "_label": f"Уточнить статус ORD-{order.id}",
            },
        })

    return ActionResult(
        text=text,
        cards=[{
            "type": "tracking",
            "data": {
                "order_id": order.id,
                "title": f"Заказ #{order.id}",
                "current_label": current_label,
                "current_idx": current_idx,
                "total_stages": len(TRACKING_STAGES),
                "progress_pct": progress_pct,
                "stages": stages,
                "timeline": timeline,
                "total": float(order.total_amount),
                "currency": "USD",
                "eta_delivery": eta_delivery,
                "days_left": days_left,
                "payment_status_label": order.get_payment_status_display(),
                "tracking_number": order.tracking_number
                                    or (order.logistics_meta or {}).get("tracking_number"),
                "carrier": order.carrier_name
                            or (order.logistics_meta or {}).get("carrier"),
                "next_actor": next_actor,
                "next_event": next_event,
            },
        }] + extra_cards,
        actions=actions_list,
        contextual_actions=ctx_actions,
        # Action-chip вместо plain-text. Контекст однозначен (этот заказ),
        # ответ детерминирован — нет смысла гонять через /chat/ + Claude.
        suggestions=[
            {"label": "📍 Где заказ?",      "action": "track_order",
             "params": {"order_id": order.id}},
            {"label": "📅 Когда доставят?", "action": "track_order",
             "params": {"order_id": order.id}},
            {"label": "📜 История по заказу", "action": "audit_log",
             "params": {"order_id": order.id}},
        ],
    )


@register("pay_final")
def pay_final(params, user, role):
    """Оплачивает остаток (всё что не покрыто резервом) и переводит заказ в paid → ready_to_ship."""
    from decimal import Decimal

    from django.db import transaction
    from django.utils import timezone

    from marketplace.models import Order

    from .models import Wallet

    order_id = params.get("order_id")
    if not order_id:
        return ActionResult(text="Не указан заказ.")
    try:
        order = Order.objects.get(id=order_id, buyer=user)
    except Order.DoesNotExist:
        return ActionResult(text=f"Заказ #{order_id} не найден.")

    if order.payment_status == "paid":
        return ActionResult(
            text=f"Заказ #{order.id} уже полностью оплачен.",
            actions=[{"label": "Детали заказа", "action": "get_order_detail",
                      "params": {"order_id": order.id}}],
        )

    if order.payment_status == "awaiting_reserve":
        return ActionResult(
            text=(
                f"Сначала нужно оплатить резерв 10% по заказу #{order.id} — "
                f"только потом можно закрывать остаток."
            ),
            actions=[{"label": f"💳 Списать резерв ${order.reserve_amount:,.0f}",
                      "action": "pay_reserve", "params": {"order_id": order.id}}],
        )

    final_amount = (Decimal(str(order.total_amount)) - Decimal(str(order.reserve_amount or 0))).quantize(Decimal("0.01"))
    if final_amount <= 0:
        return ActionResult(text="По заказу нет остатка к оплате.")

    wallet = Wallet.for_user(user)
    if wallet.balance < final_amount:
        shortage = final_amount - wallet.balance
        return ActionResult(
            text=(
                f"❌ Недостаточно средств для оплаты остатка по заказу #{order.id}.\n"
                f"Нужно: ${final_amount:,.2f} · на счёте: ${wallet.balance:,.2f} · "
                f"не хватает: ${shortage:,.2f}."
            ),
            actions=[
                {"label": f"Пополнить депозит на ${max(shortage * Decimal('1.2'), Decimal('1000')):,.0f}",
                 "action": "topup_wallet",
                 "params": {"amount": float(max(shortage * Decimal("1.2"), Decimal("1000")))}},
                {"label": "Баланс депозита", "action": "get_balance", "params": {}},
            ],
        )

    # ── ШАГ 1: черновик до подтверждения ──
    if not params.get("confirmed"):
        balance_after = wallet.balance - final_amount
        warnings = []
        if balance_after < final_amount * Decimal("0.5"):
            warnings.append(
                f"После списания на счёте останется ${balance_after:,.0f} — "
                f"меньше половины этой суммы. Рекомендую заранее пополнить депозит."
            )
        return ActionResult(
            text=(
                f"Готовлю списание остатка по заказу #{order.id}. После оплаты "
                f"поставщик начнёт отгрузку. Деньги остаются в эскроу до "
                f"вашего подтверждения приёмки."
            ),
            cards=[{
                "type": "draft",
                "data": {
                    "title": f"Подтвердите оплату остатка по заказу #{order.id}",
                    "rows": [
                        {"label": "Заказ", "value": f"#{order.id} · {order.customer_name or '—'}"},
                        {"label": "Сумма заказа", "value": f"${order.total_amount:,.2f}"},
                        {"label": "Уже оплачено (резерв)", "value": f"${order.reserve_amount:,.2f}"},
                        {"label": "К оплате (90%)", "value": f"${final_amount:,.2f}", "primary": True},
                        {"label": "Депозит сейчас", "value": f"${wallet.balance:,.2f}"},
                        {"label": "После списания", "value": f"${balance_after:,.2f}"},
                    ],
                    "warnings": warnings,
                    "confirm_action": "pay_final",
                    "confirm_label": f"💳 Оплатить ${final_amount:,.0f}",
                    "confirm_params": {"order_id": order.id, "confirmed": True},
                    "cancel_label": "Отмена",
                },
            }],
            suggestions=["Сколько с депозита уйдёт всего?", "Когда выплата поставщику?"],
        )

    # ── 2FA: для платежей >= $5,000 требуется код подтверждения ──
    if final_amount >= Decimal("5000"):
        otp_required = str(params.get("otp") or "").strip()
        # В demo-режиме фиксированный код; в проде интегрируется с TwoFactorAuth
        expected = "1234"
        if otp_required != expected:
            return ActionResult(
                text=(
                    f"Платёж >${final_amount:,.0f} требует двухфакторной защиты. "
                    f"Введите 4-значный код подтверждения. Demo-код: 1234 "
                    f"(в проде — отправляется в Telegram-бот / email)."
                ),
                cards=[{
                    "type": "form",
                    "data": {
                        "title": f"🔐 2FA · Подтвердите оплату ${final_amount:,.0f}",
                        "submit_action": "pay_final",
                        "submit_label": "Подтвердить",
                        "fields": [
                            {"name": "otp", "label": "Код из 4 цифр",
                             "required": True, "placeholder": "1234"},
                        ],
                        "fixed_params": {
                            "order_id": order.id,
                            "confirmed": True,
                        },
                    },
                }],
                suggestions=["Куда придёт код?", "Отменить"],
            )

    # SECURITY P0-5: select_for_update + re-check для защиты от double-spend.
    from . import payments as _pay
    with transaction.atomic():
        order = (Order.objects.select_for_update()
                 .get(id=order.id, buyer=user))
        if order.payment_status == "paid":
            return ActionResult(text=f"Заказ #{order.id} уже оплачен (перепроверка).")
        wallet = (Wallet.objects.select_for_update().get(pk=wallet.pk))
        if wallet.balance < final_amount:
            return ActionResult(text="Недостаточно средств (перепроверка).")
        intent = _pay.create_payment_intent(final_amount, order_id=order.id, payer=user, kind="final")
        intent = _pay.confirm_payment_intent(intent, user)
        order.payment_status = "paid"
        order.status = "ready_to_ship"
        order.final_paid_at = timezone.now()
        order.save(update_fields=["payment_status", "status", "final_paid_at"])
    wallet.refresh_from_db(fields=["balance"])
    _log_event(order, "final_payment_paid", actor=user, source="buyer",
               meta={"amount": float(final_amount), "balance_after": float(wallet.balance),
                     "intent_id": intent["id"]})

    # ТЗ §4.1: пересчитать annual volume buyer'а — может перейти на новый
    # discount level (1/2/3) после этого закрытого заказа.
    try:
        from .discounts import recalc_buyer_volume
        recalc_buyer_volume(user)
    except Exception:
        logger.exception("recalc_buyer_volume on pay_final failed")

    # Broadcast в shipment-чат buyer'а
    try:
        from .order_events import notify_order_event
        notify_order_event(order, "pay_final", actor=user)
    except Exception:
        logger.exception("notify_order_event in pay_final failed")

    return ActionResult(
        text=(
            f"✓ Списано ${final_amount:,.2f} с депозита — остаток по заказу #{order.id} оплачен.\n"
            f"Депозит: ${wallet.balance:,.2f} {wallet.currency}.\n"
            f"Заказ переведён в статус «готов к отгрузке»."
        ),
        cards=[{
            "type": "order",
            "data": {
                "id": str(order.id),
                "number": order.id,
                "status": "ready_to_ship",
                "status_label": "Готов к отгрузке",
                "total": float(order.total_amount),
                "currency": "USD",
                "payment_status": "paid",
                "payment_status_label": f"Оплачено полностью · депозит ${wallet.balance:,.0f}",
                "wallet_balance": float(wallet.balance),
            },
        }],
        actions=[
            {"label": "Отгрузить заказ", "action": "advance_order",
             "params": {"order_id": order.id}},
            {"label": "Баланс депозита", "action": "get_balance", "params": {}},
        ],
        suggestions=["Когда отгрузка?", "Отслеживание", "История списаний"],
    )


@register("advance_order")
def advance_order(params, user, role):
    """Двигает заказ на следующий статус по pipeline (production → ready → shipped → delivered).

    Сам не делает финансовых операций — для платежей есть pay_reserve / pay_final.
    """
    from django.utils import timezone

    from marketplace.models import Order

    order_id = params.get("order_id")
    if not order_id:
        return ActionResult(text="Не указан заказ.")

    # Buyer не может двигать заказ — это делает продавец. Жёсткий чек
    # по UI-роли даже если пользователь технически владеет товарами тоже.
    if role == "buyer":
        return ActionResult(
            text=("Покупатель не может двигать заказ по пайплайну. "
                  "Это делает поставщик после оплаты резерва. "
                  "Переключитесь в режим «Продавец» если вы владеете товарами в заказе."),
        )
    # Seller — только свои заказы (где есть его позиции). Operator — любой.
    # Тест-юзеры маппятся на demo_seller через _effective_seller.
    from marketplace.models import OrderItem

    from .seller_actions import _effective_seller
    if role == "seller":
        user = _effective_seller(user)
        is_my_order = OrderItem.objects.filter(
            order_id=order_id, part__seller=user,
        ).exists()
        if not is_my_order:
            return ActionResult(
                text=f"Заказ #{order_id} не содержит ваших товаров — двигать его не могу.",
            )
    try:
        order = Order.objects.get(id=order_id)
    except Order.DoesNotExist:
        return ActionResult(text=f"Заказ #{order_id} не найден.")

    transitions = {
        "reserve_paid":   ("confirmed",      "Подтверждён поставщиком"),
        "confirmed":      ("in_production",  "В производстве"),
        "in_production":  ("ready_to_ship",  "Готов к отгрузке"),
        "ready_to_ship":  ("transit_abroad", "Транзит (зарубеж)"),
        "transit_abroad": ("customs",        "Таможня"),
        "customs":        ("transit_rf",     "Транзит (РФ)"),
        "transit_rf":     ("issuing",        "Выдача"),
        "issuing":        ("delivered",      "Доставлен"),
        "delivered":      ("completed",      "Завершён"),
    }

    if order.status not in transitions:
        return ActionResult(
            text=f"Заказ #{order.id} в статусе «{order.get_status_display()}» — двигать дальше некуда.",
        )

    # ТЗ «Этапы ЛК» — для перехода на следующий статус должны быть выполнены
    # ВСЕ триггеры текущего этапа (QR-скан, документы, кнопочные подтверждения).
    # Однако button-триггеры (где actor == текущий пользователь) считаем
    # подтверждёнными самим кликом «двинуть дальше» — двойного подтверждения
    # не требуем. QR-скан/документы остаются обязательными отдельно.
    required_triggers = _stage_checklist(order.status, order.incoterm or "FOB")
    if required_triggers:
        meta = order.logistics_meta or {}
        done = (meta.get("triggers") or {}).get(order.status) or {}
        # Auto-backfill: «Предоплата» — авто-триггер от системы.
        if order.status == "reserve_paid" and "payment_received" not in done:
            done["payment_received"] = timezone.now().isoformat() + "|backfill"
        # Auto-complete button-триггеры — пользователь нажал переход, значит
        # подтвердил выполнение всех кнопочных шагов своей роли.
        for t in required_triggers:
            if t.get("type") == "button" and t["id"] not in done:
                done[t["id"]] = timezone.now().isoformat() + "|advance"
        meta.setdefault("triggers", {})[order.status] = done
        order.logistics_meta = meta
        order.save(update_fields=["logistics_meta"])
        # Проверяем что осталось — только qr/upload триггеры могут блокировать
        missing = [t for t in required_triggers
                   if t["id"] not in done and t.get("type") in ("qr", "upload")]
        if missing:
            return ActionResult(
                text=(
                    f"⚠️ Нельзя двинуть заказ #{order.id} дальше — не выполнены триггеры этапа:\n"
                    + "\n".join(f"  • {t['label']}" for t in missing)
                    + "\n\nОтметьте их в чек-листе этапа в очереди продавца."
                ),
                actions=[
                    {"label": "📦 Очередь продавца", "action": "seller_pipeline", "params": {}},
                ],
            )

    # Не пускаем за ready_to_ship без полной оплаты — кнопка «Оплатить»
    # показывается только покупателю; продавец видит ожидание.
    if order.status == "ready_to_ship" and order.payment_status != "paid":
        from decimal import Decimal
        rem = (Decimal(str(order.total_amount)) - Decimal(str(order.reserve_amount or 0))).quantize(Decimal("0.01"))
        if role == "buyer":
            return ActionResult(
                text=(
                    f"Заказ #{order.id} готов к отгрузке. До отправки нужно "
                    f"оплатить остаток ${rem:,.0f} (90%) — деньги списываются "
                    f"с депозита и держатся в эскроу до подтверждения доставки."
                ),
                actions=[
                    {"label": f"💳 Оплатить остаток ${rem:,.0f}",
                     "action": "pay_final", "params": {"order_id": order.id}},
                    {"label": "Баланс депозита", "action": "get_balance", "params": {}},
                ],
                suggestions=["Оплатить остаток", "Состояние депозита"],
            )
        # seller / operator
        return ActionResult(
            text=(
                f"Заказ #{order.id} готов к отгрузке. Ожидаем от покупателя "
                f"остаток ${rem:,.0f} (90%) — после оплаты сможете отгрузить."
            ),
            actions=[
                {"label": "📦 Трекинг", "action": "track_order",
                 "params": {"order_id": order.id}},
                {"label": "🚚 К отгрузке", "action": "seller_pipeline", "params": {}},
            ],
            suggestions=["Что отгрузить?", "Очередь продавца"],
        )

    old_status = order.status
    new_status, label = transitions[order.status]
    order.status = new_status
    order.save(update_fields=["status"])
    _log_event(order, "status_changed", actor=user, source="buyer",
               meta={"from": old_status, "to": new_status})

    # Broadcast в shipment-чат buyer'а с обновлённым timeline
    try:
        from .order_events import notify_order_event
        notify_order_event(order, new_status, actor=user)
    except Exception:
        logger.exception("notify_order_event failed in advance_order")

    next_actions = []
    suggestions = []
    next_text = ""

    # Контекстные подсказки + следующая кнопка
    NEXT_LABELS = {
        "confirmed":      "▶️ В производство",
        "in_production":  "▶️ Готовность",
        "ready_to_ship":  "💳 Оплатить остаток (90%)",
        "transit_abroad": "▶️ На таможню",
        "customs":        "▶️ Транзит по РФ",
        "transit_rf":     "▶️ Передать на выдачу",
        "issuing":        "▶️ Подтвердить доставку",
        "delivered":      "▶️ Закрыть заказ",
    }
    if new_status == "ready_to_ship" and order.payment_status != "paid":
        from decimal import Decimal
        final_amount = (Decimal(str(order.total_amount)) - Decimal(str(order.reserve_amount or 0))).quantize(Decimal("0.01"))
        if role == "buyer":
            next_text = (
                f"\nЧтобы запустить отгрузку, оплатите остаток "
                f"${final_amount:,.0f} (90%) — деньги уйдут с депозита в эскроу."
            )
            next_actions.append({
                "label": f"💳 Оплатить остаток ${final_amount:,.0f}",
                "action": "pay_final", "params": {"order_id": order.id},
            })
            suggestions = ["Оплатить остаток", "Состояние депозита"]
        else:
            # seller / operator: ждём покупателя
            next_text = (
                f"\nОжидаем от покупателя остаток ${final_amount:,.0f} (90%). "
                f"Как только эскроу пополнится — сможете отгружать."
            )
            next_actions.append({
                "label": "🚚 К отгрузке", "action": "seller_pipeline", "params": {},
            })
            suggestions = ["Что ещё в очереди?", "Какие RFQ открыты?"]
    elif new_status in NEXT_LABELS:
        next_actions.append({"label": NEXT_LABELS[new_status], "action": "advance_order",
                             "params": {"order_id": order.id}})
        suggestions = [
            {"label": "📍 Где заказ?",      "action": "track_order",
             "params": {"order_id": order.id}},
            {"label": "📅 Когда доставят?", "action": "track_order",
             "params": {"order_id": order.id}},
            {"label": "📦 Трекинг",         "action": "track_order",
             "params": {"order_id": order.id}},
        ]

    next_actions.append({"label": "📦 Трекинг", "action": "track_order",
                         "params": {"order_id": order.id}})

    return ActionResult(
        text=f"✓ Заказ #{order.id} → «{label}».{next_text}",
        cards=[{
            "type": "order",
            "data": {
                "id": str(order.id),
                "number": order.id,
                "status": new_status,
                "status_label": label,
                "total": float(order.total_amount),
                "currency": "USD",
                "payment_status_label": order.get_payment_status_display(),
            },
        }],
        actions=next_actions,
        suggestions=suggestions,
    )


@register("confirm_delivery")
def confirm_delivery(params, user, role):
    """Покупатель подтверждает приёмку: delivered → completed.

    Доступно только покупателю и только когда продавец уже довёл заказ
    до статуса `delivered`.
    """
    from decimal import Decimal

    from marketplace.models import Order

    order_id = params.get("order_id")
    if not order_id:
        return ActionResult(text="Не указан заказ.")
    try:
        order = Order.objects.get(id=order_id, buyer=user)
    except Order.DoesNotExist:
        return ActionResult(text=f"Заказ #{order_id} не найден.")

    if order.status == "completed":
        return ActionResult(text=f"Заказ #{order.id} уже закрыт.")
    if order.status != "delivered":
        return ActionResult(
            text=(
                f"Закрыть заказ #{order.id} можно только после статуса «Доставлен». "
                f"Сейчас — «{order.get_status_display()}». Отгрузку и доставку "
                f"подтверждает поставщик."
            ),
            actions=[{"label": "📦 Трекинг", "action": "track_order",
                      "params": {"order_id": order.id}}],
        )

    # SECURITY P0-7: confirmed-gate. confirm_delivery высвобождает эскроу
    # продавцу, генерирует revenue_lines и обновляет рейтинг — это
    # необратимое финансовое действие. Требуем явный клик «Подтвердить».
    if not bool(params.get("confirmed")):
        return ActionResult(
            text=(f"📦 Подтвердить приёмку заказа #{order.id}?\n\n"
                   f"После подтверждения: эскроу-холд перейдёт продавцу, "
                   f"платформа выставит revenue-lines, рейтинг продавца "
                   f"обновится. Действие необратимо."),
            cards=[{"type": "kpi_grid", "data": {
                "title": f"Заказ #{order.id}",
                "items": [
                    {"label": "Сумма", "value": f"${float(order.total_amount or 0):,.0f}"},
                    {"label": "Покупатель", "value": (order.customer_name or "—")[:24]},
                ],
            }}],
            actions=[
                {"label": "✓ Подтверждаю приёмку",
                 "action": "confirm_delivery",
                 "params": {"order_id": order.id, "confirmed": True}},
                {"label": "Открыть рекламацию",
                 "action": "open_claim", "params": {"order_id": order.id}},
            ],
        )

    order.status = "completed"
    order.save(update_fields=["status"])
    _log_event(order, "status_changed", actor=user, source="buyer",
               meta={"from": "delivered", "to": "completed", "kind": "buyer_accepted"})

    # ТЗ §15: генерация revenue lines по этому заказу (basis_fee, logistics,
    # success_fee, rf_agent, customs_fee, volume_discount).
    try:
        from .revenue import generate_revenue_lines
        # Базис берём из meta или DDP по умолчанию (typical для РФ-импорта)
        meta = order.logistics_meta or {}
        basis = (meta.get("customs", {}) or {}).get("basis") or "DDP"
        we_clear = bool((meta.get("customs", {}) or {}).get("hs_code"))  # если HS присвоен — мы оформляем
        generate_revenue_lines(
            order, basis=basis, payment_currency="USD",
            we_clear_customs=we_clear,
        )
    except Exception:
        logger.exception("generate_revenue_lines on confirm_delivery failed")

    # Эскроу → продавцам. Multi-seller split: разносим сумму по
    # OrderItem.part.seller пропорционально стоимости их позиций.
    release_summary = ""
    try:
        from . import payments as _pay
        from .rating import record_rating_event
        splits = _pay.split_by_seller(order)
        released_total = Decimal("0")
        for s in splits:
            seller = s["seller"]
            res = _pay.release_to_seller(order=order, seller=seller, amount=s["amount"])
            if res.get("ok"):
                released_total += Decimal(str(res["amount"]))
                _log_event(order, "operator_action", actor=user, source="system",
                           meta={"kind": "escrow_released", "to": res["to"],
                                 "amount": res["amount"], "share": s["share"]})
                _notify(seller, kind="payment",
                        title=f"Поступление по заказу #{order.id}",
                        body=f"Покупатель подтвердил приёмку — на счёт зачислено ${res['amount']:,.2f}.",
                        url=f"/chat/?order={order.id}")
                # Rating event: +2 за on-time-delivery (buyer accepted без рекламации)
                record_rating_event(
                    seller, event_type="delivery_on_time",
                    meta={"order_id": order.id, "amount": float(s["amount"])},
                )
        if released_total > 0:
            n = len(splits)
            release_summary = (
                f"\nПлатформа выплатила ${released_total:,.2f} из эскроу"
                + (f" (распределено между {n} продавцами)." if n > 1 else " продавцу.")
            )
    except Exception:
        logger.exception("escrow release on confirm_delivery failed")

    # Broadcast в shipment-чат buyer'а — финальный таймлайн «completed»
    try:
        from .order_events import notify_order_event
        notify_order_event(order, "completed", actor=user)
    except Exception:
        logger.exception("notify_order_event in confirm_delivery failed")

    return ActionResult(
        text=f"✓ Заказ #{order.id} закрыт. Спасибо за приёмку!" + release_summary,
        cards=[{
            "type": "order",
            "data": {
                "id": str(order.id), "number": order.id,
                "status": "completed", "status_label": "Завершён",
                "total": float(order.total_amount), "currency": "USD",
                "payment_status_label": order.get_payment_status_display(),
            },
        }],
        actions=[
            {"label": "Все мои заказы", "action": "get_orders", "params": {}},
            {"label": "Оставить отзыв", "action": "create_claim",
             "params": {"order_id": order.id, "kind": "feedback"}},
        ],
        suggestions=["Открыть отзыв", "Что заказать ещё?"],
    )


@register("get_buyer_discount")
def get_buyer_discount(params, user, role):
    """ТЗ §4.1: показать текущий уровень auto-discount по годовому обороту."""
    from django.utils import timezone

    from .discounts import LEVEL_THRESHOLDS, recalc_buyer_volume

    bvy = recalc_buyer_volume(user, year=timezone.now().year)
    if not bvy:
        return ActionResult(text="Не удалось рассчитать ваш объём закупок.")

    LEVEL_NAMES = {0: "Без скидки", 1: "Уровень 1", 2: "Уровень 2", 3: "Уровень 3"}

    def _fmt_short(amount):
        a = float(amount)
        if a >= 1_000_000:
            return f"${a/1_000_000:.1f}M".rstrip("0").rstrip(".") + "M" if False else f"${a/1_000_000:,.1f}M"
        if a >= 1_000:
            return f"${a/1_000:,.0f}K"
        return f"${a:,.0f}"

    # Лестница тиров (LEVEL_THRESHOLDS уже включает level=0 «без скидки»).
    # Сортируем по возрастанию уровня — снизу вверх лестница.
    tiers = sorted(
        [{"level": lvl, "threshold": float(threshold),
          "discount": (f"{int(disc)}%" if disc == int(disc) else f"{float(disc):g}%"),
          "label": LEVEL_NAMES.get(lvl, f"Уровень {lvl}")}
         for lvl, threshold, disc in LEVEL_THRESHOLDS],
        key=lambda t: t["level"],
    )

    tier_items = []
    for t in tiers:
        if t["level"] < bvy.level:
            state = "done"
        elif t["level"] == bvy.level:
            state = "current"
        else:
            state = "future"
        tier_items.append({
            "label":          t["label"],
            "discount_pct":   t["discount"],
            "threshold_text": _fmt_short(t["threshold"]) if t["threshold"] > 0 else "—",
            "state":          state,
        })

    # Прогресс к следующему тиру
    progress = None
    next_label_text = None
    if bvy.level < 3:
        next_level = bvy.level + 1
        for lvl, threshold, _disc in LEVEL_THRESHOLDS:
            if lvl == next_level:
                # Текущий тир = пол для текущего уровня
                if bvy.level == 0:
                    floor_val = 0
                else:
                    for lvl2, thr2, _ in LEVEL_THRESHOLDS:
                        if lvl2 == bvy.level:
                            floor_val = float(thr2)
                            break
                    else:
                        floor_val = 0
                span = float(threshold) - floor_val
                done = max(0, float(bvy.volume_usd) - floor_val)
                pct = (done / span * 100) if span > 0 else 0
                gap = max(0, float(threshold) - float(bvy.volume_usd))
                progress = {
                    "pct":          round(min(100, max(0, pct)), 1),
                    "current_text": f"${bvy.volume_usd:,.0f}",
                    "target_text":  _fmt_short(float(threshold)),
                    "gap_text":     f"Ещё {_fmt_short(gap)}" if gap > 0 else "Цель достигнута",
                    "next_label":   f"Уровню {next_level}",
                }
                next_label_text = f"Уровню {next_level}"
                break

    text_lines = [
        f"💰 Ваш годовой объём в {bvy.year}: ${bvy.volume_usd:,.0f}",
        f"Текущий уровень: {LEVEL_NAMES[bvy.level]} · скидка {bvy.discount_pct}%",
    ]
    if progress and progress.get("gap_text"):
        text_lines.append(f"До {next_label_text}: {progress['gap_text'].lower()} оборота.")

    return ActionResult(
        text="\n".join(text_lines),
        cards=[{
            "type": "tier_progress",
            "data": {
                "title":   f"Auto-discount · {bvy.year}",
                "current": {
                    "discount_pct":  f"{bvy.discount_pct}%",
                    "label":         LEVEL_NAMES[bvy.level],
                    "turnover_text": f"${bvy.volume_usd:,.0f}",
                },
                "progress": progress,
                "tiers":    tier_items,
                "footer_text": (
                    "Скидка применяется автоматически на следующих заказах "
                    "после достижения порога оборота за календарный год."
                ),
            },
        }],
        actions=[
            {"label": "💸 Экономия",           "action": "get_savings",       "params": {}},
            {"label": "📊 Аналитика заказов",   "action": "get_analytics",    "params": {}},
            {"label": "📦 Отчёт по поставкам", "action": "get_supply_report", "params": {}},
            {"label": "⏱ SLA по заказам",      "action": "get_sla_report",   "params": {}},
        ],
        contextual_actions=[
            {"action": "my_bonuses",     "label": "← Бонусы"},
            {"action": "support_home",   "label": "← Поддержка"},
        ],
    )


@register("get_savings")
def get_savings(params, user, role):
    """Экономия покупателя за год: сколько денег сэкономил auto-discount
    + средний % скидки + сравнение с предыдущим годом."""
    from decimal import Decimal

    from django.utils import timezone

    from marketplace.models import Order

    if role != "buyer":
        return ActionResult(
            text="💸 Отчёт «Экономия» доступен только в кабинете покупателя.",
            contextual_actions=[{"action": "support_home", "label": "← Назад"}],
        )

    now = timezone.now()
    this_year = now.year
    prev_year = this_year - 1

    # Заказы за год — берём только реально оплаченные/прошедшие
    paid_statuses = ("reserve_paid", "paid", "in_production", "ready_to_ship",
                     "transit_abroad", "customs", "transit_rf", "issuing",
                     "delivered", "completed")

    def _stats_for(year):
        qs = Order.objects.filter(
            buyer=user,
            created_at__year=year,
            status__in=paid_statuses,
        )
        gross = Decimal("0")
        saved = Decimal("0")
        orders_with_discount = 0
        for o in qs:
            total = Decimal(str(o.total_amount or 0))
            # discount_pct лежит в meta; если поле явно есть — используем,
            # иначе считаем из tier'а на момент заказа (упрощённо — 0).
            disc_pct = Decimal("0")
            try:
                meta = o.meta or {}
                if isinstance(meta, dict):
                    raw = meta.get("auto_discount_pct") or meta.get("discount_pct")
                    if raw not in (None, ""):
                        disc_pct = Decimal(str(raw))
            except Exception:
                pass
            if disc_pct > 0:
                orders_with_discount += 1
                # total — уже после скидки, экономия = total * disc / (100 - disc)
                if disc_pct < 100:
                    saved += (total * disc_pct / (Decimal("100") - disc_pct))\
                                .quantize(Decimal("0.01"))
            gross += total
        return {
            "orders":               qs.count(),
            "gross":                gross,
            "saved":                saved,
            "orders_with_discount": orders_with_discount,
            "avg_disc_pct": (saved / (gross + saved) * 100) if (gross + saved) > 0 else Decimal("0"),
        }

    cur  = _stats_for(this_year)
    prev = _stats_for(prev_year)

    delta_saved = cur["saved"] - prev["saved"]
    delta_sign = "+" if delta_saved >= 0 else "−"
    delta_abs  = abs(delta_saved)

    # Доля заказов со скидкой и YoY %
    coverage_pct = int(cur["orders_with_discount"] * 100 / cur["orders"]) if cur["orders"] else 0
    if prev["saved"] > 0:
        yoy_pct = int((cur["saved"] - prev["saved"]) * 100 / prev["saved"])
    elif cur["saved"] > 0:
        yoy_pct = 100
    else:
        yoy_pct = 0
    yoy_arrow = "↑" if yoy_pct > 0 else ("↓" if yoy_pct < 0 else "→")
    items = [
        {"label": f"Сэкономлено за {this_year}",
         "value": f"${float(cur['saved']):,.0f}",
         "tone":  "ok" if cur["saved"] > 0 else "info"},
        {"label": "Средняя скидка",
         "value": f"{float(cur['avg_disc_pct']):.2f}%",
         "tone":  "ok" if float(cur['avg_disc_pct']) >= 3 else "info",
         "sub":   "взвешенная по обороту"},
        {"label": "Покрытие скидкой",
         "value": f"{coverage_pct}%",
         "tone":  "ok" if coverage_pct >= 50 else ("warn" if coverage_pct >= 20 else "bad"),
         "sub":   f"{cur['orders_with_discount']}/{cur['orders']} заказов"},
        {"label": "Оборот (gross)",
         "value": f"${float(cur['gross'] + cur['saved']):,.0f}",
         "tone":  "info"},
        {"label": f"YoY экономия",
         "value": f"{yoy_arrow} {abs(yoy_pct)}%",
         "tone":  "ok" if yoy_pct >= 0 else "bad",
         "sub":   f"{this_year}: ${float(cur['saved']):,.0f} · {prev_year}: ${float(prev['saved']):,.0f}"},
    ]

    # Текст-инсайт по приоритету
    if cur["orders_with_discount"] == 0:
        text_lines = ["⚠️ Auto-discount не активен: ни один заказ не прошёл со скидкой. "
                       "Достигните Уровня 1 ($1.1M оборота за год) для авто-3%."]
    elif yoy_pct <= -25 and prev["saved"] > 0:
        text_lines = [f"📉 Экономия упала: {yoy_arrow}{abs(yoy_pct)}% к {prev_year} (${float(cur['saved']):,.0f} vs ${float(prev['saved']):,.0f})."]
    elif yoy_pct >= 25:
        text_lines = [f"📈 Экономия выросла: {yoy_arrow}{yoy_pct}% к {prev_year} — продолжайте набирать оборот."]
    elif coverage_pct < 30 and cur["orders"] >= 5:
        text_lines = [f"💡 Только {coverage_pct}% заказов со скидкой — большой потенциал роста экономии."]
    else:
        text_lines = [f"💸 Экономия за {this_year}: ${float(cur['saved']):,.0f} (средняя скидка {float(cur['avg_disc_pct']):.2f}%)."]

    return ActionResult(
        text="\n".join(text_lines),
        cards=[{
            "type": "kpi_grid",
            "data": {"title": f"💸 Экономия · {this_year}", "items": items},
        }],
        actions=[
            {"label": "🎯 Лестница тиров",   "action": "get_buyer_discount", "params": {}},
            {"label": "📊 Аналитика заказов","action": "get_analytics",      "params": {}},
        ],
        contextual_actions=[
            {"action": "my_bonuses",     "label": "← Бонусы"},
            {"action": "support_home",   "label": "← Поддержка"},
        ],
    )


@register("get_balance")
def get_balance(params, user, role):
    """Показать баланс депозита и последние транзакции.
    Для seller-роли тест-юзеры маппятся на demo_seller (`_effective_seller`)."""
    from .models import Wallet
    if role == "seller":
        from .seller_actions import _effective_seller
        user = _effective_seller(user)
    wallet = Wallet.for_user(user)
    txs = list(wallet.transactions.all()[:10])

    if not txs:
        body = "Движений пока не было."
    else:
        lines = []
        for tx in txs:
            sign = "+" if tx.kind in ("topup", "refund") else "−"
            lines.append(
                f"{tx.created_at.strftime('%d.%m %H:%M')} · {sign}${tx.amount:,.0f} · "
                f"{tx.description or tx.get_kind_display()}"
            )
        body = "\n".join(lines)

    return ActionResult(
        text=(
            f"💰 Депозит: ${wallet.balance:,.2f} {wallet.currency}.\n\n"
            f"Последние операции:\n{body}"
        ),
        actions=[
            {"label": "Пополнить на $10,000", "action": "topup_wallet",
             "params": {"amount": 10000}},
            {"label": "Все мои заказы", "action": "get_orders", "params": {}},
        ],
        suggestions=["История списаний", "Пополнить депозит"],
    )


@register("topup_wallet")
def topup_wallet(params, user, role):
    """Точка входа в пополнение депозита.

    В production (по умолчанию) показывает форму выбора суммы + способа оплаты
    → submit_topup создаёт заявку → confirm_topup_paid юзер подтверждает оплату
    → op_confirm_topup финансист зачисляет в реальный кошелёк.

    В dev (WALLET_DEMO_MODE=1) сохраняется старое демо-поведение: моментальное
    зачисление без реального платежа. Это нужно для e2e-тестов и демо-показов.
    """
    if os.getenv("WALLET_DEMO_MODE", "") == "1":
        return _topup_wallet_demo(params, user, role)
    # Production flow — показываем форму
    return start_topup_form(params, user, role)


def _topup_wallet_demo(params, user, role):
    """DEMO-only: моментальное пополнение без оплаты (для dev/e2e)."""
    from decimal import Decimal

    from marketplace.models import Order

    from .models import Wallet, WalletTx
    try:
        amount = Decimal(str(params.get("amount") or 10000)).quantize(Decimal("0.01"))
    except Exception:
        return ActionResult(text="Некорректная сумма.")
    if amount <= 0:
        return ActionResult(text="Сумма должна быть больше нуля.")

    wallet = Wallet.for_user(user)
    wallet.balance = wallet.balance + amount
    wallet.save(update_fields=["balance", "updated_at"])
    WalletTx.objects.create(
        wallet=wallet, kind="topup", amount=amount,
        description="Пополнение депозита (DEMO MODE)",
        balance_after=wallet.balance,
    )

    actions = []
    text = (
        f"✓ [DEMO] Депозит пополнен на ${amount:,.2f}.\n"
        f"Текущий остаток: ${wallet.balance:,.2f} {wallet.currency}."
    )
    pending_order_id = params.get("pending_order_id")
    if pending_order_id:
        try:
            order = Order.objects.get(id=pending_order_id, buyer=user)
            if order.payment_status == "awaiting_reserve":
                reserve = order.reserve_amount
                if wallet.balance >= reserve:
                    text += (
                        f"\n\n💼 Заказ #{order.id} ждёт оплату резерва "
                        f"${reserve:,.2f} — теперь хватает, можно продолжить покупку."
                    )
                    actions.append({
                        "label": f"💳 Завершить покупку (списать ${reserve:,.0f})",
                        "action": "pay_reserve",
                        "params": {"order_id": order.id},
                    })
        except Order.DoesNotExist:
            pass
    actions.append({"label": "Баланс депозита", "action": "get_balance", "params": {}})

    return ActionResult(text=text, actions=actions)


# ───────────────────────────────────────────────────────────────
# Production deposit top-up flow:
#   1. start_topup        → форма (amount + method)
#   2. submit_topup       → создаёт WalletTopupRequest, выдаёт реквизиты
#   3. confirm_topup_paid → юзер кликнул «Я оплатил» → status=awaiting_confirmation
#   4. op_confirm_topup   → финансист подтверждает → mark_paid() → wallet кредитуется
# ───────────────────────────────────────────────────────────────

# Реквизиты компании — в production хранить в settings/env, не в коде.
# Это safe defaults для dev; реальные данные подгружаются из settings.TOPUP_BANK_*.
def _bank_wire_details(amount, currency, ref_code):
    """Реквизиты для wire-перевода. Все значения из settings (env-overridable).
    Сейчас бенефициар — наша дубайская компания INNOVATION IDEA FZ LLC.
    Счёт в AED — банк автоматически конвертирует USD/EUR-переводы."""
    from django.conf import settings
    return {
        "beneficiary":    settings.TOPUP_BANK_BENEFICIARY,
        "beneficiary_address": settings.TOPUP_BANK_BENEFICIARY_ADDR,
        "trade_license":  settings.TOPUP_BANK_TRADE_LICENSE,
        "tax_no":         settings.TOPUP_BANK_TAX_NO,
        "bank_name":      settings.TOPUP_BANK_NAME,
        "branch_code":    settings.TOPUP_BANK_BRANCH_CODE,
        "swift":          settings.TOPUP_BANK_SWIFT,
        "iban":           settings.TOPUP_BANK_IBAN,
        "account":        settings.TOPUP_BANK_ACCOUNT,
        "account_currency": settings.TOPUP_BANK_CURRENCY,
        "contact_name":   settings.TOPUP_BANK_CONTACT_NAME,
        "contact_phone":  settings.TOPUP_BANK_CONTACT_PHONE,
        "contact_email":  settings.TOPUP_BANK_CONTACT_EMAIL,
        "reference_code": ref_code,
        "amount":         f"{amount:,.2f} {currency}",
        "purpose":        f"Deposit top-up {ref_code} for platform Consolidator Parts",
    }


@register("start_topup")
def start_topup_form(params, user, role):
    """Форма пополнения депозита: сумма + способ оплаты."""
    pending_order_id = params.get("pending_order_id")
    suggested = params.get("amount") or 10000
    return ActionResult(
        text="💰 Пополнение депозита",
        cards=[{
            "type": "form",
            "data": {
                "title":   "Пополнение депозита",
                "intent":  "Введите сумму и выберите способ оплаты. Зачисление обычно занимает 1–2 рабочих дня после получения средств.",
                "submit_action": "submit_topup",
                "submit_label":  "Создать заявку",
                "submit_params": {"pending_order_id": pending_order_id} if pending_order_id else {},
                "fields": [
                    {"name": "amount", "label": "Сумма (USD)", "type": "number",
                     "value": str(suggested), "min": 100, "max": 1_000_000,
                     "required": True,
                     "hint": "Минимум $100. Большие суммы — без ограничений."},
                    {"name": "method", "label": "Способ оплаты", "type": "select",
                     "value": "bank_wire", "required": True,
                     "options": [
                        {"value": "bank_wire", "label": "🏦 Банковский перевод — UAE счёт (1–2 дня)"},
                        {"value": "usdt",      "label": "₮ USDT TRC-20 — быстро (10–30 мин)"},
                        {"value": "card",      "label": "💳 Карта — интеграция в работе"},
                     ]},
                ],
            },
        }],
        contextual_actions=[
            {"action": "get_balance", "label": "← Назад к балансу"},
        ],
    )


@register("submit_topup")
def submit_topup(params, user, role):
    """Создаёт заявку на пополнение и выдаёт реквизиты под выбранный метод."""
    from decimal import Decimal, InvalidOperation

    from .models import WalletTopupRequest

    try:
        amount = Decimal(str(params.get("amount") or 0)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError):
        return ActionResult(text="⚠️ Некорректная сумма — введите число.",
                             actions=[{"label": "Заполнить ещё раз",
                                       "action": "start_topup", "params": {}}])
    if amount < 100:
        return ActionResult(text="⚠️ Минимальная сумма пополнения — $100.",
                             actions=[{"label": "Заполнить ещё раз",
                                       "action": "start_topup", "params": {}}])
    if amount > 1_000_000:
        return ActionResult(text="⚠️ Свыше $1,000,000 — обратитесь к менеджеру лично.",
                             actions=[{"label": "💬 Связаться с менеджером",
                                       "action": "contact_operator",
                                       "params": {"topic": "large_topup"}}])

    method = (params.get("method") or "bank_wire").strip()
    if method not in {"bank_wire", "card", "usdt"}:
        return ActionResult(text=f"⚠️ Неизвестный способ оплаты: {method}.")

    pending_order_id = params.get("pending_order_id") or None

    ref = WalletTopupRequest.make_ref()
    details: dict = {}
    if method == "bank_wire":
        details = _bank_wire_details(amount, "USD", ref)
    elif method == "card":
        details = {
            "checkout_url": f"https://pay.example/checkout?ref={ref}&amount={amount}",
            "provider":     "stub",  # TODO: интеграция со Stripe/Yookassa
            "expires_in":   "20 минут",
        }
    elif method == "usdt":
        from django.conf import settings
        details = {
            "network":       "TRC-20",
            "wallet_address": getattr(settings, "TOPUP_USDT_ADDRESS", "TX_DEMO_WALLET_NOT_FOR_USE"),
            "amount_usdt":   f"{amount:,.2f}",
            "reference_code": ref,
        }

    req = WalletTopupRequest.objects.create(
        user=user, amount=amount, currency="USD",
        method=method, status="pending",
        reference_code=ref, payment_details=details,
        note=(f"pending_order={pending_order_id}" if pending_order_id else ""),
    )

    # Карточка с реквизитами зависит от метода
    from django.utils import timezone as _tz
    now = _tz.now()
    issuer = {
        "name": "Consolidator Parts",
        "subtitle": "B2B-маркетплейс запчастей для тяжёлой техники",
    }
    common_meta = [
        {"label": "Invoice №", "value": f"INV-{req.id:06d}"},
        {"label": "Дата",      "value": now.strftime("%d.%m.%Y")},
        {"label": "Действителен до", "value": (now + timedelta(days=7)).strftime("%d.%m.%Y")},
    ]

    if method == "bank_wire":
        invoice_data = {
            "doc_type":      "INVOICE",
            "issuer":        issuer,
            "meta":          common_meta,
            "expires_text":  "Срок оплаты: 7 дней",
            "amount_text":   f"${amount:,.2f} USD",
            "ref":           ref,
            "ref_warning":   "Этот код ОБЯЗАТЕЛЬНО указать в назначении платежа. Без него деньги невозможно сопоставить с вашим аккаунтом и зачисление задержится.",
            "sections": [
                {
                    "title": "Получатель (Beneficiary)",
                    "rows": [
                        {"label": "Компания",   "value": details["beneficiary"], "copy": True},
                        {"label": "Адрес",      "value": details["beneficiary_address"]},
                        {"label": "Trade License", "value": details["trade_license"], "mono": True,
                         "hint": "RAKEZ — Ras Al Khaimah Economic Zone (UAE)"},
                        {"label": "Tax Reg No.", "value": details["tax_no"], "mono": True},
                    ],
                },
                {
                    "title": "Банковские реквизиты",
                    "rows": [
                        {"label": "Банк",         "value": details["bank_name"]},
                        {"label": "SWIFT / BIC",  "value": details["swift"], "copy": True, "mono": True},
                        {"label": "IBAN",         "value": details["iban"], "copy": True, "mono": True},
                        {"label": "Account No.",  "value": details["account"], "copy": True, "mono": True},
                        {"label": "Branch Code",  "value": details["branch_code"], "mono": True},
                        {"label": "Валюта счёта", "value": details["account_currency"], "mono": True,
                         "hint": "Счёт в AED. Банк автоматически конвертирует USD/EUR по курсу дня."},
                    ],
                },
                {
                    "title": "Назначение платежа",
                    "rows": [
                        {"label": "Payment Purpose", "value": details["purpose"], "copy": True,
                         "warn": True,
                         "hint": "Скопируйте полностью и вставьте в поле «Назначение платежа» в вашем банке."},
                    ],
                },
                {
                    "title": "Контакт по платежу",
                    "rows": [
                        {"label": "Ответственный", "value": details["contact_name"]},
                        {"label": "Телефон",       "value": details["contact_phone"], "copy": True, "mono": True},
                        {"label": "Email",         "value": details["contact_email"], "copy": True},
                    ],
                },
            ],
            "notes": [
                "Бенефициар — наша дубайская компания (UAE, юрисдикция RAKEZ). Принимаем переводы в USD / EUR / AED.",
                "После оплаты нажмите кнопку «Я оплатил». Финансовый отдел сверит поступление и зачислит депозит обычно за 1–2 рабочих дня.",
                "Реквизиты выданы для конкретной заявки. Не пересылайте третьим лицам — оплата по чужому payment reference не будет зачислена на ваш аккаунт.",
            ],
        }
        followup_text = f"Счёт INV-{req.id:06d} на ${amount:,.2f} USD сформирован."
        card = {"type": "invoice", "data": invoice_data}

    elif method == "usdt":
        invoice_data = {
            "doc_type":      "USDT INVOICE",
            "issuer":        issuer,
            "meta":          common_meta,
            "expires_text":  "Срок оплаты: 7 дней",
            "amount_text":   f"{details['amount_usdt']} USDT",
            "ref":           ref,
            "ref_warning":   "Укажите код в memo, если ваш кошелёк или биржа поддерживают memo. Если нет — после отправки нажмите «Я оплатил» с этим кодом.",
            "sections": [
                {
                    "title": "Реквизиты USDT",
                    "rows": [
                        {"label": "Сеть",        "value": details["network"], "mono": True, "warn": True,
                         "hint": "ВНИМАНИЕ: Только TRC-20! Отправка в других сетях (ERC-20, BEP-20, и т.д.) приведёт к потере средств — транзакции в блокчейне необратимы."},
                        {"label": "Wallet Address", "value": details["wallet_address"], "copy": True, "mono": True},
                        {"label": "Сумма",       "value": f"{details['amount_usdt']} USDT", "copy": True, "mono": True},
                    ],
                },
            ],
            "notes": [
                "USDT TRC-20 — самый быстрый способ пополнения. Зачисление обычно 10–30 минут после 12+ подтверждений в сети TRON.",
                "Перед отправкой ПЕРЕПРОВЕРЬТЕ адрес и сеть. Транзакции в блокчейне необратимы.",
                "После отправки нажмите «Я оплатил» — финансовый отдел проверит транзакцию в TRON Explorer по адресу и сумме.",
            ],
        }
        followup_text = f"Счёт USDT INV-{req.id:06d} на {details['amount_usdt']} USDT сформирован."
        card = {"type": "invoice", "data": invoice_data}

    else:  # card
        invoice_data = {
            "doc_type":      "INVOICE (CARD)",
            "issuer":        issuer,
            "meta":          common_meta,
            "amount_text":   f"${amount:,.2f} USD",
            "ref":           ref,
            "sections": [
                {
                    "title": "Статус интеграции",
                    "rows": [
                        {"label": "Provider", "value": "stub", "mono": True,
                         "hint": "Интеграция Stripe / Yookassa / CloudPayments в работе. Пока выберите банковский перевод или USDT."},
                        {"label": "Checkout URL", "value": details.get("checkout_url", "—"), "mono": True},
                    ],
                },
            ],
            "notes": [
                "Card-checkout временно недоступен. Рекомендуем использовать USDT (10–30 минут) или wire-перевод (1–2 дня).",
            ],
        }
        followup_text = f"Заявка #{req.id}. Card-checkout пока в режиме интеграции."
        card = {"type": "invoice", "data": invoice_data}

    return ActionResult(
        text=followup_text,
        cards=[card],
        actions=[
            {"label": "✅ Я оплатил",
             "action": "confirm_topup_paid",
             "params": {"topup_id": req.id}},
            {"label": "✖ Отменить заявку",
             "action": "cancel_topup",
             "params": {"topup_id": req.id}},
        ],
        contextual_actions=[
            {"action": "list_topups", "label": "📋 Мои заявки на пополнение"},
        ],
    )


@register("confirm_topup_paid")
def confirm_topup_paid(params, user, role):
    """Юзер заявляет, что оплата произведена. Статус → awaiting_confirmation.
    Деньги НЕ зачисляются — это делает оператор финансы после фактической
    сверки (см. op_confirm_topup)."""
    from django.utils import timezone

    from .models import WalletTopupRequest

    topup_id = params.get("topup_id")
    try:
        req = WalletTopupRequest.objects.get(id=int(topup_id), user=user)
    except (WalletTopupRequest.DoesNotExist, ValueError, TypeError):
        return ActionResult(text="Заявка не найдена.")
    if req.status not in ("pending", "awaiting_confirmation"):
        return ActionResult(
            text=f"Заявка {req.reference_code} в статусе «{req.get_status_display()}» — "
                 f"подтверждение уже невозможно.",
        )
    if req.status == "pending":
        req.status = "awaiting_confirmation"
        req.user_claim_at = timezone.now()
        req.save(update_fields=["status", "user_claim_at", "updated_at"])

    return ActionResult(
        text=(
            f"✓ Спасибо! Заявка {req.reference_code} помечена как «оплачена».\n\n"
            f"Финансовый отдел проверит поступление средств и зачислит депозит. "
            f"Обычно это занимает 1–2 рабочих дня для банковского перевода и "
            f"10–30 минут для USDT.\n\n"
            f"Вы получите уведомление, когда депозит будет пополнен."
        ),
        actions=[
            {"label": "📋 Все мои заявки", "action": "list_topups", "params": {}},
            {"label": "💰 Баланс депозита", "action": "get_balance", "params": {}},
        ],
    )


@register("cancel_topup")
def cancel_topup(params, user, role):
    """Юзер отменяет свою заявку (пока не оплачено)."""
    from django.utils import timezone

    from .models import WalletTopupRequest

    topup_id = params.get("topup_id")
    try:
        req = WalletTopupRequest.objects.get(id=int(topup_id), user=user)
    except (WalletTopupRequest.DoesNotExist, ValueError, TypeError):
        return ActionResult(text="Заявка не найдена.")
    if req.status not in ("pending", "awaiting_confirmation"):
        return ActionResult(
            text=f"Заявка {req.reference_code} уже в статусе «{req.get_status_display()}» — "
                 f"отмена невозможна.",
        )
    req.status = "cancelled"
    req.cancelled_at = timezone.now()
    req.save(update_fields=["status", "cancelled_at", "updated_at"])
    return ActionResult(
        text=f"✓ Заявка {req.reference_code} отменена.",
        actions=[
            {"label": "💰 Новое пополнение", "action": "start_topup", "params": {}},
            {"label": "Баланс депозита", "action": "get_balance", "params": {}},
        ],
    )


@register("list_topups")
def list_topups(params, user, role):
    """Список заявок юзера на пополнение."""
    from .models import WalletTopupRequest

    reqs = list(WalletTopupRequest.objects.filter(user=user).order_by("-created_at")[:30])
    if not reqs:
        return ActionResult(
            text="У вас пока нет заявок на пополнение депозита.",
            actions=[{"label": "💰 Пополнить депозит",
                      "action": "start_topup", "params": {}}],
        )

    _STATUS_EMOJI = {
        "pending": "⏳", "awaiting_confirmation": "🔎",
        "paid": "✅", "cancelled": "✖", "failed": "⚠️", "expired": "⌛",
    }
    rows = [{
        "title": f"{_STATUS_EMOJI.get(r.status, '·')} {r.reference_code} · ${r.amount:,.2f}",
        "subtitle": (
            f"{r.get_method_display()} · {r.get_status_display()} · "
            f"создана {r.created_at.strftime('%d.%m %H:%M')}"
        ),
        # ACTION-CHIP: открыть детали — пока не реализовано как отдельный экран,
        # повторно создаём «реквизиты» через submit_topup pull-flow.
    } for r in reqs]

    return ActionResult(
        text=f"📋 Заявки на пополнение · {len(reqs)}",
        cards=[{
            "type": "list",
            "data": {"title": "Мои заявки на пополнение", "items": rows},
        }],
        actions=[{"label": "💰 Новое пополнение",
                  "action": "start_topup", "params": {}}],
    )

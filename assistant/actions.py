"""Chat-First Action Executor.

When AI determines the user wants to perform an action (search, create RFQ,
track shipment, etc.), it calls one of these handlers. Each handler returns
an ActionResult with text + cards + new actions + suggestions.
"""
from __future__ import annotations

import json
import logging
import os
import re
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
    # True → фронт НЕ подставляет дефолтные подсказки (ensureSuggestions).
    # Для экранов, где рекомендации не к месту (например, отчёт об ошибках
    # импорта — там не нужно звать «Создать RFQ / Аналитика»).
    no_suggestions: bool = False

    def to_dict(self):
        return {
            "text": self.text,
            "cards": self.cards,
            "actions": self.actions,
            "contextual_actions": self.contextual_actions,
            "suggestions": self.suggestions,
            "no_suggestions": self.no_suggestions,
        }


# ── Permission matrix ──────────────────────────────────────
# Buyer-actions: покупка, оплата, приёмка. Доступны и buyer, и seller
# (продавец тоже может докупать товар или докомплектовывать свой заказ
# как обычный покупатель).
_BUYER_ACTIONS = [
    "search_parts", "create_rfq", "get_rfq_status", "get_my_deals",
    "get_orders", "get_order_detail", "track_order", "track_shipment",
    "cancel_order",
    "invite_customer", "accept_customer_invite", "accept_referral",  # инвайт/реферал (для всех ролей)
    "my_referrals",  # мои реферальные награды ($100 за приведённого)
    "my_kam", "change_manager",  # клиент: его менеджер + право сменить (удержание)
    "kam_message", "kam_reattach", "kam_request",  # клиент: написать / вернуть / подобрать KAM
    # Решение по консолидации vs split shipment
    "consolidate_wait", "split_shipment", "set_supplier_decision",
    "get_budget", "get_analytics", "get_supply_report", "get_sla_report",
    "get_buyer_discount", "get_savings", "recent_activity",
    "open_project", "list_projects",
    "seller_analytics_hub", "seller_executive_report",
    "compare_products", "compare_suppliers", "top_suppliers",
    "buyer_best_offers", "buyer_offer_compare", "calc_part_logistics",
    "upload_parts_list", "analyze_spec",
    # Чертежи (приватные, owner-based): и покупатель, и продавец грузят/смотрят
    # СВОИ. Покупатель видит «что нужно», продавец «что предлагает»; друг другу
    # не показываются — сверяет только оператор при согласовании сделки.
    "seller_drawings", "upload_drawing",
    # Папки чертежей (создать / открыть / разложить / переместить / удалить)
    "drawing_folder", "create_drawing_folder", "add_to_folder",
    "move_drawing", "delete_drawing_folder",
    # Привязка чертежа к позиции каталога через умный поиск
    "link_drawing", "bind_drawing",
    # go_home обычно перехватывается фронтом, но допускаем и на бэке (stale JS)
    "go_home",
    "get_claims", "create_claim", "open_claim", "claim_detail",
    # Communication with operator (support/escalation)
    "ask_operator", "ask_about_rfq",
    "cancel_rfq",
    "open_url", "generate_proposal",
    # покупка и депозит
    "quick_order", "pay_reserve", "pay_final",
    "shipping_choose", "shipping_apply",
    "get_balance", "topup_wallet", "buy_ai_requests", "link_card", "withdraw_wallet", "transfer_wallet",
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
    "update_kyb_contacts", "upload_kyb_doc",
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
    "request_payout",
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
    # CRM-аккаунт (заказчики, проекты, начисления) — НЕ у продавца: это KAM.
    # Продавцу/покупателю остаётся только виральный инвайт/реферал.
    "invite_customer", "accept_customer_invite", "accept_referral",
    "seller_catalog", "seller_warehouses", "toggle_product", "add_product", "edit_product",
    "product_detail", "import_pricelist_preview",
    "rfq_detail", "respond_rfq_form",
    "seller_team", "invite_team_member",
    "accept_team_invite", "team_member", "team_disable", "team_enable", "team_set_role",
    "seller_integrations", "seller_reports",
    "seller_qr", "seller_logistics", "seller_negotiations",
    "price_quote", "audit_log", "recent_activity", "generate_qr", "notifications",
    "support_home", "kb_faq", "my_verifications",
    "contact_operator", "open_complaint",
    "view_support_ticket", "color_legend",
    "sync_1c",
    # View-as: чтобы оператор в режиме просмотра мог выйти обратно + дёргать помощь
    "op_exit_view_as", "op_help_supplier", "op_help_send_reminder", "op_help_escalate",
]

_OPERATOR_CORE = [
    "open_project", "list_projects", "go_home",
    # Read-only browse + диспетчерские action'ы
    "search_parts", "get_orders", "get_order_detail", "get_rfq_status",
    "track_order", "track_shipment", "advance_order", "complete_trigger",
    "get_balance", "request_payout", "op_my_bonuses",  # бонусы оператора
    "op_my_suppliers",  # моя база поставщиков (PIVOT 2026-05-27)
    "get_analytics", "get_supply_report", "get_demand_report", "get_sla_report", "get_budget",
    "compare_suppliers", "compare_products", "top_suppliers",
    "get_claims", "open_url", "generate_proposal",
    "audit_log", "recent_activity", "kb_search", "notifications",
    "view_support_ticket", "color_legend",
    "support_home", "kb_faq", "my_verifications",
    "contact_operator", "open_complaint",
    # Operator-only: dashboard, очередь, назначение, спор, заметка
    "op_dashboard", "op_queue", "op_rfq_queue", "op_sla_breach",
    "op_drawings_by_part",  # мастер-вид: чертежи по артикулу (сверка need/offer)
    "op_escalate_to_kam",  # Оператор → KAM: эскалация исключения
    "invite_customer", "accept_referral", "accept_customer_invite",  # реферал ($100 за приведённого)
    "my_referrals",  # мои реферальные награды
    # CRM заказчиков — НЕ в общем операторском наборе: это эксклюзив KAM
    # (см. _KAM_ONLY ниже). Так оператор не пересекается с KAM по аккаунтам.
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
    # RFQ → suppliers (оператор может рассылать любые RFQ и видеть КП)
    "view_rfq_quotes", "view_quote", "send_rfq_to_suppliers", "compare_quotes",
    "rfq_detail", "create_rfq", "cancel_rfq", "ask_about_rfq",
    "contact_supplier", "ask_operator",
    # Кросс-юзер диалоги оператора (зеркальные conv в его сайдбаре)
    "op_my_user_chats", "open_conversation",
    # View-as: оператор переключается в кабинет поставщика для просмотра
    "op_view_as_supplier", "op_exit_view_as",
    # Помощь поставщику (доступна и в нормальном, и в view-as режиме)
    "op_help_supplier", "op_help_send_reminder", "op_help_escalate",
    # Document generators (operator может создавать любые)
    "generate_invoice_pdf", "generate_packing_list_pdf",
    "generate_qc_report_pdf", "list_order_documents",
]

# KAM (Key Account Manager) — коммерческий/аккаунт-набор. Эксклюзив роли:
# заказчики по ИНН, проекты, привязка отгрузок, инвайты/рефералы, начисления.
# Никакая другая операторская подроль этого не получает → нет конфликта.
_KAM_ONLY = [
    "seller_customers", "add_customer", "customer_detail",
    "create_project_for_customer", "link_order_to_customer",
    "invite_customer", "accept_customer_invite", "accept_referral",
    "customer_bonuses", "my_accruals", "kam_deals",
]

# Исполнительные writes — это зона ОПЕРАТОРА, не KAM. KAM их не делает
# (видимость дашбордов остаётся, а сами действия-исполнения — нет).
_KAM_EXCLUDED = {
    "op_customs_release", "op_hs_assign", "op_cert_upload", "op_sanctions_check",
    "op_topup_queue", "op_confirm_topup", "op_reject_topup",
    "op_kyb_queue", "op_kyb_review", "op_kyb_approve", "op_kyb_reject",
    "op_kyb_check", "op_kyb_clarify",
    "op_assign_carrier", "op_resolve_dispute",
    "start_claim_review", "approve_claim", "reject_claim",
    "apply_corrective", "apply_settlement", "close_claim",
    "op_escalate_to_kam",  # эскалирует оператор, не KAM
    # Сделку ведёт ОПЕРАТОР — KAM её не драйвит (ни КП, ни рассылку RFQ,
    # ни назначения, ни передачу на исполнение). KAM = привлечение/онбординг.
    "op_compose_kp", "op_approve_kp", "op_dispatch_manual_rfq",
    "send_rfq_to_suppliers", "op_assign", "op_add_note",
    "kam_handoff_to_operator",
}

# KAM = операторская база (для видимости статусов/RFQ/КП) МИНУС исполнение
# ПЛЮС коммерческий аккаунт-набор.
_KAM_ACTIONS = [a for a in _OPERATOR_CORE if a not in _KAM_EXCLUDED] + _KAM_ONLY

ROLE_ACTIONS = {
    "buyer":  _BUYER_ACTIONS,
    "seller": _BUYER_ACTIONS + _SELLER_ONLY,
    "operator_logist": _OPERATOR_CORE,
    "operator_customs": _OPERATOR_CORE,
    "operator_payment": _OPERATOR_CORE,
    "operator_manager": _KAM_ACTIONS,   # KAM — коммерция, без исполнения
    "operator": _OPERATOR_CORE,         # оператор — исполнение, без CRM
    "admin": ["*"],  # admin sees everything (wildcard — все actions доступны)
}

# Подмножество actions, специфичных только для admin (вне operator/seller/buyer)
_ADMIN_ONLY = [
    "admin_dashboard", "admin_gmv", "admin_users", "admin_user_detail",
    "admin_ban_user", "admin_unban_user",
    "admin_moderation_queue", "admin_catalog_review", "admin_platform_settings",
    "admin_revenue_breakdown", "admin_activity_feed",
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


_OP_ROLES_ALL = {"operator", "operator_logist", "operator_customs",
                 "operator_payment", "operator_manager", "admin"}


def _user_can_access_order(o, user, role) -> bool:
    """Право видеть/менять конкретный заказ. Защита от IDOR: действие получает
    order_id из params (его шлёт фронт/AI), поэтому КАЖДЫЙ обработчик, достающий
    Order по id, обязан проверить владельца.

      • оператор/админ (внутренние) — все заказы;
      • продавец — только если в заказе есть ЕГО товары;
      • покупатель (и все прочие) — только свой заказ.
    """
    try:
        if role in _OP_ROLES_ALL or getattr(user, "is_staff", False) or getattr(user, "is_superuser", False):
            return True
        if role == "seller":
            from .seller_actions import _effective_seller
            from marketplace.models import OrderItem
            eff = _effective_seller(user)
            return OrderItem.objects.filter(order_id=o.id, part__seller=eff).exists()
        return getattr(o, "buyer_id", None) == getattr(user, "id", None)
    except Exception:
        logger.exception("order access check failed")
        return False


# ── Registry ───────────────────────────────────────────────
_REGISTRY: dict[str, Callable] = {}


def register(name: str):
    def decorator(func):
        _REGISTRY[name] = func
        return func
    return decorator


try:  # типы запроса для защиты от утечки в JSON
    from django.http import HttpRequest as _HttpRequest
    from rest_framework.request import Request as _DRFRequest
    _REQUEST_TYPES = (_HttpRequest, _DRFRequest)
except Exception:  # pragma: no cover
    _REQUEST_TYPES = ()


def _scrub_internal(obj, _depth: int = 0):
    """Рекурсивно вырезает несериализуемые внутренние данные из payload'а
    ответа (actions/cards/...), прежде всего инжектированный `_request`
    (HttpRequest/DRF Request).

    Зачем: ActionView кладёт `_request` в params для handler'ов (login/session).
    Любой handler, копирующий `{params, ...}` в возвращаемые actions/cards
    (например confirmed-gate в quick_order), иначе утащил бы Request в JSON-
    ответ и в Message.actions (JSONField) → `Object of type Request is not JSON
    serializable` → 500, который на фронте маскируется под «Соединение
    прервалось». Чистим в единой точке диспетчера, чтобы покрыть все handler'ы.
    """
    if _depth > 10:
        return obj
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k == "_request":
                continue
            if _REQUEST_TYPES and isinstance(v, _REQUEST_TYPES):
                continue
            out[k] = _scrub_internal(v, _depth + 1)
        return out
    if isinstance(obj, (list, tuple)):
        return [_scrub_internal(v, _depth + 1) for v in obj]
    return obj


_INFO_ORDER_ACTIONS = {"get_order_detail", "track_shipment", "track_order"}


def _bump_session_seen(params, key):
    """Счётчик инфо-обращений в рамках сессии. Возвращает, сколько раз (включая
    текущее) пользователь обращался к ключу. Для «страховки»: на 2-м+ обращении к
    тому же заказу/отчёту предлагаем человека (с первого раза мог найти ответ сам)."""
    req = (params or {}).get("_request")
    sess = getattr(req, "session", None)
    if sess is None:
        return 1
    try:
        seen = dict(sess.get("_info_seen") or {})
        n = int(seen.get(key, 0)) + 1
        seen[key] = n
        sess["_info_seen"] = seen
        sess.modified = True
        return n
    except Exception:
        return 1


def _apply_safety_contact(action_name, params, role, result):
    """Пост-обработка вторичных действий ПОКУПАТЕЛЯ на карточках заказа:
    (1) «Оценить поставщика» переносим из кнопок (contextual) в чипы (suggestions)
        как action-chip (действие create_claim сохраняется) — связь с человеком
        важнее, поэтому «поменяли местами» по UX;
    (2) «страховка»: на 2-м+ обращении к заказу (детали/трекинг/отчёт) или
        повторном поиске добавляем КНОПКОЙ связь с человеком (менеджер, если у
        заказа есть KAM, иначе оператор) — первой среди contextual. На первом
        обращении не навязываемся. Оператору/продавцу — ничего не трогаем."""
    try:
        if role != "buyer":
            return result
        order_info = action_name in _INFO_ORDER_ACTIONS
        oid = (((params or {}).get("order_id") or (params or {}).get("id"))
               if order_info else None)

        # (1) «Оценить поставщика»: кнопка → чип (action-chip {label,action,params}).
        if order_info:
            ctx = list(result.contextual_actions or [])
            rate = [a for a in ctx if isinstance(a, dict)
                    and "Оценить поставщика" in str(a.get("label", ""))]
            if rate:
                result.contextual_actions = [a for a in ctx if a not in rate]
                sugg = list(result.suggestions or [])
                for a in rate:
                    if not any(isinstance(s, dict) and s.get("action") == a.get("action")
                               for s in sugg):
                        sugg.append(a)
                result.suggestions = sugg

        # (2) «Страховка» связью с человеком — КНОПКОЙ, только на повторе.
        if order_info and oid:
            if _bump_session_seen(params, f"order:{oid}") < 2:
                return result
            from marketplace.models import Order
            has_kam = (Order.objects.filter(id=oid)
                       .values_list("assigned_kam_id", flat=True).first())
            btn = ({"action": "kam_message", "label": "Связаться с менеджером",
                    "params": {"order_id": oid}} if has_kam
                   else {"action": "contact_operator", "label": "Связаться с оператором",
                         "params": {"order_id": oid}})
        elif action_name == "search_parts":
            if _bump_session_seen(params, "search") < 2:
                return result
            btn = {"action": "contact_operator",
                   "label": "Связаться с оператором", "params": {}}
        else:
            return result
        ctx = list(result.contextual_actions or [])
        if not any(isinstance(a, dict)
                   and a.get("action") in ("kam_message", "contact_operator")
                   for a in ctx):
            ctx.insert(0, btn)   # самое заметное вторичное действие — первым
            result.contextual_actions = ctx
    except Exception:
        logger.exception("safety-contact post-process failed")
    return result


def _strip_seller_rfq_create(result, role):
    """Продавец RFQ не создаёт (он отвечает котировками). Убираем «Создать RFQ»
    из подсказок и действий любого ответа продавцу — чтобы нигде не предлагалось."""
    if role != "seller":
        return

    def _is_rfq_create(item):
        if isinstance(item, dict):
            return item.get("action") == "create_rfq"
        if isinstance(item, str):
            return _is_meta_rfq_query(item)
        return False

    try:
        if result.suggestions:
            result.suggestions = [s for s in result.suggestions if not _is_rfq_create(s)]
        if result.actions:
            result.actions = [a for a in result.actions if not _is_rfq_create(a)]
        if result.contextual_actions:
            result.contextual_actions = [
                a for a in result.contextual_actions if not _is_rfq_create(a)
            ]
    except Exception:
        logger.exception("strip seller rfq create failed")


def _is_anon(user) -> bool:
    """True если пользователь не залогинен (AnonymousUser или None)."""
    return not (user and getattr(user, "is_authenticated", False))


def _anon_register_result() -> "ActionResult":
    """Карточка «зарегистрируйтесь» — та же копия, что в
    views._registration_required_response(). Возвращается, когда аноним
    дёргает действие, требующее аккаунта (user-specific запрос к БД)."""
    return ActionResult(
        text=("🔒 Чтобы продолжить — зарегистрируйтесь прямо здесь, в чате.\n"
              "Это займёт 20 секунд."),
        actions=[
            {"action": "start_registration", "label": "🚀 Зарегистрироваться"},
            {"action": "start_login",        "label": "У меня есть аккаунт"},
        ],
    )


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
        result = handler(params=params or {}, user=user, role=role)
    except Exception as e:
        logger.exception(f"Action {action_name} failed")
        # Аноним дёрнул действие, которому нужен аккаунт: user-specific запрос
        # упал на AnonymousUser (нет числового id). Вместо сырого repr-объекта
        # (некрасиво + утечка внутренностей) — мягко ведём на регистрацию.
        if _is_anon(user) and ("AnonymousUser" in str(e) or "expected a number" in str(e)):
            return _anon_register_result()
        return ActionResult(text=f"⚠️ Ошибка выполнения: {e}")
    # JSON-safety: убираем инжектированный `_request` (и любые Request-объекты),
    # которые handler мог скопировать в actions/cards через `{**params, ...}`.
    # Без этого Response/JSONField падают 500-кой → «Соединение прервалось».
    result.actions = _scrub_internal(result.actions or [])
    result.cards = _scrub_internal(result.cards or [])
    result.contextual_actions = _scrub_internal(result.contextual_actions or [])
    result.suggestions = _scrub_internal(result.suggestions or [])
    # Продавец RFQ не создаёт (он отвечает котировками) → убираем «Создать RFQ»
    # из подсказок/действий в любом ответе.
    _strip_seller_rfq_create(result, role)
    # «Страховка»: на 2-м+ обращении покупателя к инфо о заказе/повторном поиске —
    # вторичной подсказкой предложить связаться с человеком (не на первом).
    _apply_safety_contact(action_name, params, role, result)
    return result


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
    "admin_activity_feed": {
        "description": "Лента важных событий: новые сделки/RFQ/загрузки прайса с кабинетом, IP и позициями. Фильтр kind=all|order|rfq|pricelist.",
        "input_schema": {"type": "object", "properties": {"kind": _STR}},
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
        # AuthZ (IDOR): только создатель, операторы и админы могут формировать
        # КП по конкретному RFQ. Та же проверка, что в get_rfq_status.
        is_op_or_admin = bool(role and (role.startswith("operator") or role == "admin"))
        if not is_op_or_admin:
            if _is_anon(user):
                # Аноним — только анонимный RFQ (created_by=None).
                if rfq.created_by_id is not None:
                    return ActionResult(text=f"⚠️ RFQ #{rfq_id} не найден")
            elif rfq.created_by_id and rfq.created_by_id != user.id:
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
            # Кнопка «Открыть RFQ» убрана — /chat/rfq/<id>/ упразднена,
            # RFQ показывается inline-карточкой через get_rfq_status.
        ],
    )


_META_RFQ_QUERIES = {
    "создать rfq", "создать новый rfq", "создание нового rfq", "создание rfq",
    "новый rfq", "rfq", "rfq из чата", "новый запрос котировок",
    "создать запрос", "создать запрос котировок", "оформить rfq", "сделать rfq",
    "запрос котировок", "create rfq", "new rfq", "make rfq",
}


def _is_meta_rfq_query(q: str) -> bool:
    """True если query — это мета-команда («Создать RFQ», «Создание нового RFQ»),
    а не описание реальной детали. Тогда RFQ не создаём — просим уточнить, что
    нужно (иначе плодим пустые RFQ с фейковой позицией и спамим поставщиков)."""
    s = (q or "").strip().lower().strip(".!?…").strip()
    return (not s) or (s in _META_RFQ_QUERIES)


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

    # 1) Article-list (paste of OEM numbers) — даже 1 артикул должен идти
    # через rich spec_results-карточку (то же UX что и для списка).
    # БАЗОВАЯ ФИЧА платформы: вставил код → получил цену из БД мгновенно.
    articles = params.get("articles") or _extract_articles(query)
    if len(articles) >= 1:
        return _search_articles_list(
            articles, params.get("quantities") or {},
            dest_country=params.get("dest_country") or "",
            delivery_address=params.get("delivery_address") or "",
            arrival_port=params.get("arrival_port") or "",
            filter_origin=params.get("filter_origin") or "",
            role=role,
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
        # Fallback: icontains (для частичных «707-99» + поиск по тексту).
        # title_ru — русское имя из словаря (покупатель ищет «коронка»);
        # cross_numbers — кросс-ссылки/резьба (напр. «3 1/2 REG») у товаров без OEM.
        qs = qs.filter(
            oem_q
            | Q(oem_number__icontains=query)
            | Q(title__icontains=query)
            | Q(title_ru__icontains=query)
            | Q(cross_numbers__icontains=query)
            | Q(description__icontains=query)
        )
    if params.get("brand"):
        qs = qs.filter(brand__name__icontains=params["brand"])
    if params.get("category"):
        qs = qs.filter(category__name__icontains=params["category"])

    # ── Фильтр по диаметру: чипы стандартных размеров из названий ──
    # Диаметр у спец-товаров (буровые коронки и т.п.) зашит в названии
    # («… 152.4 mm …»). Собираем доступные размеры из выборки → отдаём чипами;
    # клик по чипу пере-ищет с diameter=value (комбинируется с названием-запросом).
    import re as _re_dia
    def _dia_mm(t):
        m = _re_dia.search(r"(\d+(?:[.,]\d+)?)\s*mm", (t or ""), _re_dia.I)
        return float(m.group(1).replace(",", ".")) if m else None
    _dia_avail = set()
    for _t in qs.values_list("title", flat=True)[:300]:
        _d = _dia_mm(_t)
        if _d is not None:
            _dia_avail.add(_d)
    _dia_chips = sorted(_dia_avail)[:12]
    _dia_sel = (params.get("diameter") or "").strip() if isinstance(params.get("diameter"), str) else params.get("diameter")
    if _dia_sel:
        qs = qs.filter(title__icontains=f"{_dia_sel} mm")

    parts = list(qs[:limit])
    from marketplace.fx import to_usd_float  # покупатель ВСЕГДА видит USD по бирж. курсу
    cards = [{
        "type": "product",
        "data": {
            "id": str(p.id),
            "article": p.oem_number,
            "brand": p.brand.name if p.brand else "—",
            "name": p.title,
            "price": to_usd_float(p.price, getattr(p, "currency", "USD")) if p.price else None,
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

    # Чипы фильтра по диаметру — показываем когда в выдаче несколько размеров.
    _dia_ctx = []
    if len(_dia_chips) > 1:
        if _dia_sel:
            _dia_ctx.append({"action": "search_parts", "label": "⌀ все размеры",
                             "params": {"query": query}})
        for _d in _dia_chips:
            _p = {"query": query, "diameter": f"{_d:g}"}
            if params.get("brand"):
                _p["brand"] = params["brand"]
            _mark = " ✓" if (_dia_sel and f"{_d:g}" == str(_dia_sel)) else ""
            _dia_ctx.append({"action": "search_parts",
                             "label": f"⌀ {_d:g} мм{_mark}", "params": _p})
    _sel_suffix = f" · ⌀ {_dia_sel} мм" if _dia_sel else ""
    return ActionResult(
        text=f"Найдено {len(cards)} позиций по запросу «{query}»{_sel_suffix}:",
        cards=cards,
        actions=[
            {"label": "Создать RFQ на все", "action": "create_rfq",
             "params": {"product_ids": [c["data"]["id"] for c in cards]}},
            {"label": "Сравнить", "action": "compare_products",
             "params": {"product_ids": [c["data"]["id"] for c in cards]}},
        ],
        contextual_actions=_dia_ctx,
        suggestions=["Показать ещё", "Фильтр по бренду", "История цен"],
    )


def _search_articles_list(articles: list[str], quantities: dict | None = None,
                            dest_country: str = "", delivery_address: str = "",
                            arrival_port: str = "", filter_origin: str = "",
                            role: str = "buyer"):
    """Look up each article in the catalog → spec_results-style card.

    quantities: {oem: qty} — параметр от fast-path парсера «OEM qty».
    role: «buyer» (по умолчанию) — НЕ показываем поставщиков, рейтинги,
          альт-офферы. Это коммерческая инфа платформы, открывается только
          оператору после оплаты резерва. Для operator/admin — полный payload.
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
        clearance_fee,
        fallback_origin_country,
    )
    from marketplace.models import LogisticsTariff
    delivery_address = (delivery_address or "").strip()
    arrival_port = (arrival_port or "").strip()
    # Страна назначения выводится из префикса порта прибытия (RUMOW → RU).
    # FOB не требует данных — клиент сам забирает в порту поставщика.
    # CIP и DDP нужен только arrival_port (страна+город): ЦЕНА DDP =
    # фрахт + пошлина + НДС + last-mile (~5%), точный дом для расчёта цены
    # не нужен — он нужен лишь для самой доставки до двери (берётся при выборе
    # DDP). Это позволяет показать ВСЕ три базиса с ценой сразу после ввода
    # направления, а полный адрес спрашивать только если выбран DDP.
    dest = (dest_country or "").upper()[:2]
    if not dest and arrival_port:
        dest = _country_from_port(arrival_port)
    cip_available = bool(arrival_port)
    ddp_available = bool(arrival_port)
    # «needs_delivery_info» = нет данных даже для CIP. Форма всё равно
    # покажется над матрицей, FOB будет доступен сразу.
    needs_delivery_info = not cip_available
    # Матрица 3 mode × 3 incoterm + детальный breakdown.
    matrix_ships = {(m, i): Decimal("0") for m in ("sea","air","auto") for i in ("FOB","CIP","DDP")}
    matrix_breakdown = {(m, i): {"freight": Decimal("0"), "insurance": Decimal("0"),
                                   "carriage_ext": Decimal("0"), "duty": Decimal("0"),
                                   "vat": Decimal("0"), "last_mile": Decimal("0"),
                                   "clearance": Decimal("0")}
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
        from .oem_normalizer import _strip_separators, normalize_oem
        oem_candidates = normalize_oem(art)
        # ── Fast path: точный матч по oem_number (попадает в индекс) ──
        # Раньше делали 4×Replace-annotation на всю таблицу 1.3М строк —
        # на каждый OEM по ~500мс. Сейчас сначала пробуем индекс-lookup;
        # клин-lookup только если точного матча нет (редкий случай).
        candidates = list(
            Part.objects
            .select_related("brand", "seller", "seller__profile")
            .filter(is_active=True, oem_number__in=oem_candidates)
        )
        if not candidates:
            # Fallback 1: clean-нормализация (разделители вычищены)
            from django.db.models import Value as V
            from django.db.models.functions import Replace, Upper
            clean_candidates = list({
                _strip_separators(c).upper() for c in oem_candidates if c
            })
            if clean_candidates:
                candidates = list(
                    Part.objects
                    .select_related("brand", "seller", "seller__profile")
                    .annotate(oem_clean=Upper(Replace(Replace(Replace(Replace(
                        "oem_number",
                        V("-"), V("")), V("."), V("")), V(" "), V("")), V("/"), V(""))))
                    .filter(is_active=True, oem_clean__in=clean_candidates)
                )
        if not candidates:
            # Fallback 2: частичный icontains (limit 20)
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
        from marketplace.fx import to_usd_float  # цены поставщиков → USD (и для сравнения, и для показа покупателю)
        for c in candidates:
            r = _seller_rating(c.seller)
            offer_pool.append({
                "part": c,
                "price": to_usd_float(c.price, getattr(c, "currency", "USD")) if c.price else None,
                "rating": r["rating"], "status": r["status"],
            })
        ranked = _rank_offers(offer_pool)
        p = ranked[0]["part"] if ranked else None
        best_status = ranked[0]["status"] if ranked else "sandbox"
        best_rating = ranked[0]["rating"] if ranked else 60.0
        alt_offers_count = max(0, len(ranked) - 1)
        if p:
            # Нет порта → предполагаемая страна-источник (country_of_origin/бренд/CN),
            # чтобы карточка показывала «Откуда» и работал фильтр «только из страны X».
            origin_cc = _country_from_port(p.sea_port or p.air_port or "") or fallback_origin_country(p)
            sea_code = (p.sea_port or "").split()[0] if p.sea_port else ""
            air_code = (p.air_port or "").split()[0] if p.air_port else ""
            # Фильтр по origin: 2 chars = country code (AE), 3+ = port code (AEFJR).
            # Поддерживает оба формата: «купить только из Турции» (TR) и «купить
            # только с этого терминала» (AEFJR).
            if filter_origin:
                fo = filter_origin.upper()
                country_match = origin_cc and origin_cc.upper() == fo
                port_match = (sea_code.upper() == fo or air_code.upper() == fo)
                if not (country_match or port_match):
                    items.append({"status": "skipped", "id": p.oem_number,
                                  "name": p.title, "qty": qty,
                                  "reason": f"origin {origin_cc}"})
                    continue
            # Цена ранжированного оффера уже в USD (offer_pool → to_usd_float).
            # Покупатель ВСЕГДА видит USD по биржевому курсу.
            price = ranked[0]["price"] if (ranked and ranked[0].get("price") is not None) else 0
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

            # ── Режим подбора per-item (ТЗ §4) ──────────────────────
            # AUTO: позиция в каталоге + есть Надёжный + данные свежие (≤30д)
            # SEMI: позиция в каталоге, но нет Надёжного ИЛИ данные устарели
            # MANUAL: позиции нет в каталоге (обрабатывается в else-ветке)
            from datetime import timedelta as _td_mode
            from django.utils import timezone as _tz_mode
            _PRICE_FRESHNESS_DAYS = 30
            _is_fresh = bool(
                p.data_updated_at and
                p.data_updated_at >= _tz_mode.now() - _td_mode(days=_PRICE_FRESHNESS_DAYS)
            )
            _has_reliable = any(o["status"] == "trusted" for o in ranked)
            if _has_reliable and _is_fresh:
                _item_mode = "auto"
            else:
                _item_mode = "semi"
            _freshness_days = None
            if p.data_updated_at:
                _freshness_days = int((_tz_mode.now() - p.data_updated_at).total_seconds() // 86400)

            items.append({
                "status": "in_stock",
                "id": p.oem_number,
                "part_id": str(p.id),
                # _clean_title: убирает CJK-иероглифы (中文/日本/한국),
                # чтобы UI не пугал пользователя смешанными скриптами.
                "name": _clean_title(p.title) or "—",
                # Русское имя из словаря (фронт покажет его только при ru-локали,
                # оригинал — мелким снизу). Пусто = перевода нет.
                "name_ru": p.title_ru or "",
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
                # ── Режим подбора и свежесть данных (ТЗ §4) ──
                "item_mode": _item_mode,
                "is_fresh": _is_fresh,
                "has_reliable": _has_reliable,
                "freshness_days": _freshness_days,
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
                # MANUAL OEM-поиск — позиции нет в каталоге, требуется
                # рассылка реальным поставщикам (ТЗ §7).
                "item_mode": "manual",
                "is_fresh": False,
                "has_reliable": False,
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
        # port_types — какие порты этого origin: {'sea'}, {'air'} или {'sea','air'}
        "port_types": set(),
        # country_code: явно указываем чтобы агрегация by_country была верной.
        # _country_from_port работает только для UN/LOCODE (sea), но IATA-коды
        # аэропортов (RKT, PKX) не следуют префиксу страны.
        "country_code": "",
    })

    # Хелпер: парсим country_code из строки порта вида
    # «AEFJR · Port of Fujairah · Фуджейра · 🇦🇪 ОАЭ» → "AE".
    # Сначала пытаемся через emoji-флаг (есть в seed-данных), иначе fallback на
    # _country_from_port (работает для морских UN/LOCODE).
    _FLAG_TO_CC = {
        "🇦🇪": "AE", "🇨🇳": "CN", "🇹🇷": "TR", "🇳🇱": "NL", "🇰🇿": "KZ",
        "🇷🇺": "RU", "🇩🇪": "DE", "🇺🇸": "US", "🇵🇰": "PK", "🇪🇸": "ES",
        "🇯🇵": "JP", "🇰🇷": "KR", "🇮🇳": "IN", "🇹🇭": "TH", "🇲🇾": "MY",
    }
    def _country_from_port_str(port_str: str, port_code: str) -> str:
        if not port_str:
            # Fallback-группа (беспортовая позиция): строки порта нет, но
            # port_code — это уже ISO-код страны-источника (CN/JP/…).
            return _country_from_port(port_code) or port_code[:2].upper()
        for flag, cc in _FLAG_TO_CC.items():
            if flag in port_str:
                return cc
        return _country_from_port(port_code) or port_code[:2].upper()
    # Базовая разбивка origin_groups_info — собираем даже когда dest нет.
    # Это нужно чтобы кнопка «Состав» и таблица origin_breakdown были
    # доступны до того как пользователь укажет порт прибытия.
    if resolved_parts:
        for p, qty, price, cargo_line in resolved_parts:
            # Регистрируем ОБА порта (sea + air) — юзеру нужно видеть варианты
            # доставки сразу: и контейнером, и авиа. Раньше брали первый, теперь
            # каждая позиция вносится дважды (если у партии оба порта указаны).
            sea_code = (p.sea_port or "").split()[0] if p.sea_port else ""
            air_code = (p.air_port or "").split()[0] if p.air_port else ""
            if not sea_code and not air_code:
                # Беспортовая позиция → группируем по fallback-стране (та же,
                # что во фрахт-цикле), чтобы «Состав» origin_breakdown совпадал
                # с посчитанным фрахтом, а не висел отдельной пустой группой.
                sea_code = air_code = fallback_origin_country(p)
            ch = max(
                Decimal(p.gross_weight_kg or 0),
                _volumetric_kg(p.length_cm, p.width_cm, p.height_cm, "sea"),
            ) * Decimal(qty)
            registered = False
            for port_code, port_type, port_str in [
                (sea_code, "sea", p.sea_port or ""),
                (air_code, "air", p.air_port or ""),
            ]:
                if not port_code:
                    continue
                info = origin_groups_info[port_code]
                info["port_types"].add(port_type)
                if not info["country_code"]:
                    info["country_code"] = _country_from_port_str(port_str, port_code)
                # Считаем позицию только один раз (по первому порту), чтобы
                # не дублировать count/weight/cargo при наличии и sea и air.
                if not registered:
                    info["count"] += 1
                    info["weight"] += ch
                    info["cargo"] += cargo_line
                    info["items"].append({
                        "oem": p.oem_number,
                        "title": p.title[:60] if p.title else "",
                        "weight_kg": float(p.gross_weight_kg or 0),
                        "cargo": float(cargo_line),
                    })
                    registered = True
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
                    # Порт не указан → fallback на страну-источник, чтобы фрахт
                    # (и CIP/DDP) считался по тарифу страна→страна.
                    origin_code = fallback_origin_country(p)
                elif not _lookup_tariff(origin_code, m):
                    # Порт указан, но тарифа (ни по порту, ни по его стране) нет —
                    # напр. IATA-код аэропорта ONQ→«ON». Наследуем страну детали
                    # (морской порт/coo/бренд), если по ней тариф есть.
                    fb = fallback_origin_country(p)
                    if _lookup_tariff(fb, m):
                        origin_code = fb
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
                    # Таможенное оформление — фикс-сбор ОДИН раз на отправку (не
                    # на позицию), только DDP. matrix считает колонку целиком, так
                    # что добавляем один раз здесь.
                    cl = clearance_fee(dest, inc)
                    matrix_breakdown[(m, inc)]["clearance"] = cl
                    matrix_ships[(m, inc)] += cl

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

    # ── Кнопки по группам режимов (ТЗ §4–7) ──────────────────────
    # Вместо одной «Купить всё» — разделяем по auto/semi/manual,
    # чтобы юзер видел: что можно сразу, что требует подтверждения,
    # что нужно рассылать поставщикам.
    actions = []
    auto_ids = [str(it.get("part_id") or "") for it in items if it.get("item_mode") == "auto" and it.get("part_id")]
    auto_total = sum(float(it["price"]) * (it.get("qty") or 1)
                      for it in items if it.get("item_mode") == "auto" and it.get("price"))
    semi_ids = [str(it.get("part_id") or "") for it in items if it.get("item_mode") == "semi" and it.get("part_id")]
    manual_arts = [it["id"] for it in items if it.get("item_mode") == "manual" and it.get("id")]
    qty_param = {pid: q for pid, q in matched_qty_pairs} if any(q != 1 for _, q in matched_qty_pairs) else None
    # Pre-считаем счётчики для labels (агрегация card_mode идёт позже,
    # но нам нужно знать «есть ли другие режимы» уже здесь).
    _auto_n = len(auto_ids)
    _semi_n = len(semi_ids)
    _manual_n = len(manual_arts)

    # 1) AUTO — главная primary-кнопка «Купить сейчас» только на надёжных + свежих
    if auto_ids:
        qo_params = {"product_ids": auto_ids}
        if qty_param:
            qo_params["product_quantities"] = {k: v for k, v in qty_param.items() if k in auto_ids}
        actions.append({
            "label": f"⚡ Купить {len(auto_ids)} ИЗ AUTO · ${auto_total:,.0f}" if _semi_n or _manual_n
                     else f"⚡ Купить сейчас ${auto_total:,.0f}",
            "action": "quick_order",
            "params": qo_params,
            "style": "primary",
        })

    # 2) SEMI — позиции от Песочницы / устаревшие → отправить на подтверждение оператора
    if semi_ids:
        actions.append({
            "label": f"✓ {len(semi_ids)} на подтверждение оператора",
            "action": "create_rfq",
            "params": {"product_ids": semi_ids, "mode": "semi"},
            "style": "warn",
        })

    # 3) MANUAL flow удалён (PIVOT 2026-05-28): не предлагаем создавать RFQ
    # для unmatched-артикулов. Просто оставляем их в spec_results как
    # «not_found» — пусть оператор подберёт аналог при следующем заходе,
    # или пользователь уточнит запрос.

    # Вторичные действия (не primary, поэтому в конце)
    if matched_ids:
        actions.append({"label": "Сравнить поставщиков", "action": "top_suppliers",
                        "params": {"limit": 3}})

    # Сводим origin_breakdown по странам (а не по портам) — клиенту важна
    # страна для решения «забрать только из Турции». При нескольких origin
    # добавляем кнопки фильтрации.
    origin_breakdown = []
    cc_flags = {"CN":"🇨🇳","TR":"🇹🇷","AE":"🇦🇪","NL":"🇳🇱","KZ":"🇰🇿","RU":"🇷🇺","DE":"🇩🇪","US":"🇺🇸","PK":"🇵🇰","ES":"🇪🇸"}
    cc_names = {"CN":"Китай","TR":"Турция","AE":"ОАЭ","NL":"Нидерланды","KZ":"Казахстан","RU":"Россия","DE":"Германия","US":"США","PK":"Пакистан","ES":"Испания"}
    if origin_groups_info:
        from collections import defaultdict as _dd
        by_country: dict = _dd(lambda: {
            "ports": {},  # {code: 'sea'|'air'|'both'}
            "count": 0, "weight": Decimal("0"), "cargo": Decimal("0"),
            "freight": {"sea": Decimal("0"), "air": Decimal("0"), "auto": Decimal("0")},
            "days": {"sea": 0, "air": 0, "auto": 0},
            "items": [],
        })
        for origin_code, info in origin_groups_info.items():
            # Используем явно проставленный country_code (он работает и для IATA
            # аэропортов типа RKT/PKX, где _country_from_port даёт мусор).
            cc = info.get("country_code") or _country_from_port(origin_code) or origin_code[:2].upper()
            b = by_country[cc]
            # Сохраняем тип порта (sea/air/both) для каждого кода
            types = info.get("port_types") or set()
            if len(types) >= 2:
                b["ports"][origin_code] = "both"
            elif "air" in types:
                b["ports"][origin_code] = "air"
            else:
                b["ports"][origin_code] = "sea"
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
            # Превращаем {code: type} в массив [{code, type}] — фронт
            # покажет 🚢 / ✈️ / 🚢✈️ иконку рядом с каждым портом.
            ports_list = [{"code": code, "type": b["ports"][code]}
                          for code in sorted(b["ports"].keys())]
            origin_breakdown.append({
                "country_code": cc,
                "flag": cc_flags.get(cc, "🌍"),
                "name": cc_names.get(cc, cc),
                "ports": ports_list,
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

    # ── Агрегация режимов по карточке (ТЗ §4) ─────────────────────
    # _auto_n / _semi_n / _manual_n уже посчитаны выше для action-кнопок.
    # Определяем card_mode:
    #   AUTO — все позиции auto
    #   SEMI — есть semi, нет manual
    #   MANUAL — есть not_found (нужен ручной OEM-поиск)
    #   MIXED — комбинация типов (>= 2 разных режима)
    _modes_set = {m for m in [_auto_n and "auto", _semi_n and "semi", _manual_n and "manual"] if m}
    if len(_modes_set) >= 2:
        _card_mode = "mixed"
    elif _manual_n:
        _card_mode = "manual"
    elif _semi_n:
        _card_mode = "semi"
    else:
        _card_mode = "auto"

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
            # ── Режимы (ТЗ §4) ──
            "card_mode": _card_mode,           # auto | semi | manual | mixed
            "auto_count": _auto_n,
            "semi_count": _semi_n,
            "manual_count": _manual_n,
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
                                "clearance": float(matrix_breakdown[(m, inc)]["clearance"]),
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
                "DDP": "all-in до двери: фрахт + страховка + пошлина (~5%) + НДС 22% + last-mile (~5%) + таможенное оформление",
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

    # ── Для buyer чистим коммерческую инфу поставщиков ─────────────
    # Имена/коды/рейтинги/счётчики альт-офферов — это интеллектуальная
    # собственность платформы. Юзер их видеть НЕ должен до 10% депозита,
    # иначе будет фильтровать (Надёжный/Песочница) и скачивать инсайты
    # маркетплейса бесплатно. После оплаты резерва оператор получает full.
    #
    # Также скрываем внутренний роутинг AUTO/SEMI/MANUAL — юзер видит
    # просто «в наличии» / «нет в каталоге» + единую зелёную кнопку
    # «Купить всё». Платформа сама внутри решает кого по какому режиму
    # вести (Надёжный → авто, Песочница → к оператору и т.п.).
    if (role or "buyer") == "buyer":
        for it in card["data"]["items"]:
            for k in ("supplier_status", "supplier_status_badge",
                       "supplier_rating", "alt_offers", "alt_suppliers",
                       "item_mode", "is_fresh", "has_reliable", "freshness_days"):
                it.pop(k, None)
        # Card-level режим тоже не нужен юзеру
        for k in ("card_mode", "auto_count", "semi_count", "manual_count"):
            card["data"].pop(k, None)
        # Кнопки переформируем: одна зелёная «Купить всё» (объединяем
        # auto+semi на бэке — внутренний роутинг прозрачен для юзера)
        # + soft-кнопка «Запросить ненайденные» (MANUAL OEM-поиск).
        all_purchaseable_ids = auto_ids + semi_ids
        all_total = sum(float(it["price"]) * (it.get("qty") or 1)
                         for it in items if it.get("status") == "in_stock" and it.get("price"))
        actions = []
        if all_purchaseable_ids:
            buy_params = {"product_ids": all_purchaseable_ids}
            if qty_param:
                buy_params["product_quantities"] = {
                    k: v for k, v in qty_param.items() if k in all_purchaseable_ids
                }
            actions.append({
                "label": f"⚡ Купить {len(all_purchaseable_ids)} · ${all_total:,.0f}",
                "action": "quick_order",
                "params": buy_params,
                "style": "primary",
            })
        # MANUAL CTA удалён (PIVOT 2026-05-28) — см. комментарий выше в primary actions.

    return ActionResult(
        text=intro,
        cards=[card],
        actions=actions,
        suggestions=[
            "Найти аналоги для ненайденных",
            "Сравни цены по бренду",
            "Сформировать КП",
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

    # ── PIVOT 2026-05-26: MANUAL режим убран как класс. ────────────
    # Мы НЕ делаем ручную рассылку поставщикам — только каталожный матчинг.
    # Если позиции нет в каталоге → это сигнал для отдела каталога:
    # фиксируем в аналитику «спрос без предложения» и предлагаем покупателю
    # либо аналог (через AI), либо подписку на появление, либо ничего.
    # → Раньше тут было: articles[] → "manual", 0% matched → "manual"
    # → Теперь: всё что не AUTO → SEMI (оператор уточняет аналоги).
    if matched_count == 0:
        # Не нашли вообще — оператор должен предложить аналог или сообщить
        # покупателю «этого нет в каталоге». Анализ потом → ищем поставщика.
        return "semi", (
            f"semi · 0/{total} позиций в каталоге · нужно предложить аналог "
            f"или сообщить отсутствие. Не-найденные позиции пишутся в аналитику "
            f"спроса для пополнения каталога."
        )
    if params.get("articles"):
        # Buyer вручную ввёл OEM — мы их сматчили частично. SEMI чтобы
        # оператор подтвердил/уточнил неточные совпадения.
        return "semi", (
            f"semi · buyer ввёл {len(params['articles'])} OEM-номеров, "
            f"{matched_count}/{total} сматчены — нужно подтверждение"
        )

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

    elif params.get("query") and not _is_meta_rfq_query(params["query"]):
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
        # Нет ни артикула, ни названия — пустой RFQ бессмысленен (поставщикам
        # нечего котировать). Не создаём его и не спамим поставщиков, а просим
        # уточнить, что нужно. Как только пользователь напишет деталь —
        # create_rfq вызовется уже с query и сделает реальный запрос.
        return ActionResult(
            text=(
                "Что запросить у поставщиков? Напишите артикул или название детали "
                "— можно с брендом и количеством.\n"
                "Например: «фильтр масляный Komatsu 600-211-1340, 10 шт» или "
                "«тормозные колодки на CAT 320»."
            ),
            suggestions=["Список поставщиков", "Покажи мои заказы"],
        )

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

    # Anonymous-buyer support: created_by=None, placeholder email/name.
    # При регистрации /api/assistant/action/start_registration RFQ
    # перепривяжется к новому user (см. _resume в pending_action).
    is_anon = not (user and getattr(user, "is_authenticated", False))
    if is_anon:
        rfq_creator = None
        rfq_customer_name = "Гость"
        # Email-плейсхолдер с session-id для трассировки → потом replace на user.email
        try:
            from django.contrib.sessions.models import Session  # noqa: F401
            rfq_customer_email = "anon@chat.local"
        except Exception:
            rfq_customer_email = "anon@chat.local"
    else:
        rfq_creator = user
        rfq_customer_name = user.get_full_name() or user.username
        rfq_customer_email = user.email or f"{user.username}@chat.local"

    try:
        rfq = RFQ.objects.create(
            created_by=rfq_creator,
            customer_name=rfq_customer_name,
            customer_email=rfq_customer_email,
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
        # Лента важных событий админа: новый RFQ + IP/кабинет/позиции.
        _log_activity("rfq", actor=rfq_creator, ip=params.get("_client_ip", ""),
                      title=f"RFQ #{rfq.id} · {len(items_to_add)} поз · {mode}",
                      meta={"rfq_id": rfq.id, "n_items": len(items_to_add), "mode": mode,
                            "items": [{"query": str(q)[:80], "qty": qn,
                                       "oem": (mp.oem_number if mp else "")}
                                      for (q, qn, mp, cf) in items_to_add[:20]]})
        # Аноним: запоминаем id RFQ в сессии, чтобы безопасно перепривязать его
        # к user при регистрации (а не угадывать чужой id). Ключ — "anon_rfq_ids".
        if is_anon:
            req = params.get("_request")
            if req is not None and hasattr(req, "session"):
                ids = list(req.session.get("anon_rfq_ids") or [])
                ids.append(rfq.id)
                req.session["anon_rfq_ids"] = ids
                req.session.modified = True
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
                    body=f"Buyer {rfq_customer_name} создал SEMI-RFQ. "
                         f"Проверь КП и одобри/отклони.",
                    url=f"/chat/rfq/{rfq.id}/?source=semi-approve",
                )
        except Exception:
            logger.exception("SEMI operator notify failed")

    # MANUAL режим отключён (PIVOT 2026-05-26). Если в legacy данных где-то
    # mode=="manual" — лечим в SEMI на лету, оператор увидит как «нужно
    # уточнение / предложить аналог».
    if mode == "manual":
        mode = "semi"

    matched_count = sum(1 for t in items_to_add if t[2] is not None)
    not_matched = [t[0] for t in items_to_add if t[2] is None]
    summary = f"{matched_count} из {len(items_to_add)} позиций сматчены с каталогом"

    # Фиксируем «спрос без предложения» для аналитики (что чаще запрашивают
    # но нет в каталоге → отдел развития каталога ищет поставщиков с прайсом)
    if not_matched and not is_anon:
        try:
            from .demand_tracker import track_missing_demand
            track_missing_demand(user, not_matched, rfq_id=rfq.id)
        except Exception:
            logger.exception("track_missing_demand failed")

    if mode == "auto":
        text = (
            f"RFQ #{rfq.id} создан · {len(items_to_add)} позиций. {summary}.\n"
            f"Котировка от {auto_sent_count} поставщиков готовится автоматически — "
            f"откройте чтобы подтвердить и зарезервировать 10%."
        )
    else:  # semi
        if matched_count == 0:
            text = (
                f"RFQ #{rfq.id} создан · {len(items_to_add)} позиций.\n"
                f"Ни одна позиция не найдена в каталоге. Оператор предложит аналоги "
                f"в течение 15 минут или сообщит об отсутствии. "
                f"Запрошенные OEM записаны в аналитику для развития каталога."
            )
        else:
            text = (
                f"RFQ #{rfq.id} создан · {len(items_to_add)} позиций. {summary}.\n"
                f"По {len(items_to_add) - matched_count} позициям оператор подтвердит "
                f"аналог или цену в течение 15 минут."
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

    # ── Богатый data shape для rfq-карточки (chat-first.js::rfq) ──
    # Без этого карточка выглядит пустой (0/0, бюджет —, лучшая —).
    # Считаем budget estimate из matched parts, готовим items_preview.
    from marketplace.fx import to_usd_float  # живой биржевой курс → USD
    budget_est_usd = 0.0
    items_preview = []
    for query_str, qty, matched_part, _conf in items_to_add[:8]:
        est_usd = None
        match_name = ""
        brand_name = ""
        if matched_part and matched_part.price is not None:
            ccy = (matched_part.currency or "USD").upper()
            est_usd = to_usd_float(matched_part.price, ccy)
            budget_est_usd += (est_usd or 0.0) * (qty or 1)
            match_name = _clean_title(matched_part.title or "")
            brand_name = matched_part.brand.name if matched_part.brand else ""
        items_preview.append({
            "article": str(query_str),
            "match": match_name,
            "brand": brand_name,
            "qty": qty or 1,
            "matched": bool(matched_part),
            "est_usd": int(est_usd) if est_usd else None,
        })

    items_count_total = len(items_to_add)
    sourcing_pct = int(round(matched_count * 100 / items_count_total)) if items_count_total else 0
    quoting_pct = 0  # на момент create_rfq котировок ещё нет

    # Status label по режиму — что именно сейчас происходит
    if mode == "auto":
        status_label = f"🤖 Разослано {auto_sent_count} поставщ. · ждём КП"
    elif mode == "semi":
        status_label = "⏱ Оператор подтвердит КП в течение 15 мин"
    else:
        status_label = "📋 Оператор разошлёт запрос (срок 48ч)"

    return ActionResult(
        text=text,
        cards=[{
            "type": "rfq",
            "data": {
                "id": str(rfq.id),
                "number": rfq.id,
                "status": "new",
                "status_label": status_label,
                "mode": mode.upper(),
                "description": " · ".join(str(t[0]) for t in items_to_add[:5])[:200],
                "items_count": items_count_total,
                "matched_count": matched_count,
                "sourcing_pct": sourcing_pct,
                "quoting_pct": quoting_pct,
                "quotes_count": 0,
                "sent_count": auto_sent_count,
                "budget_usd": int(budget_est_usd) if budget_est_usd else None,
                "best_quote_usd": None,
                "age_label": "только что",
                "items_preview": items_preview,
                "created_at": rfq.created_at.strftime("%d.%m.%Y %H:%M"),
            },
        }],
        actions=actions,
        suggestions=["Мои активные RFQ", "Создать ещё RFQ"],
    )


@register("get_orders")
def get_orders(params, user, role):
    """Список «Мои заказы» — КОРОТКИЕ карточки-сводки (type "order": номер ·
    сумма · статус · дата). Полная спецификация заказа разворачивается ТОЛЬКО по
    клику (карточка кликабельна → get_order_detail). params: {status?, limit?}

    Компактный список быстрее (не рендерим спецификацию каждого заказа) и читабельнее.
    """
    from marketplace.models import Order
    limit = min(int(params.get("limit") or 6), 10)
    qs = Order.objects.select_related("buyer").order_by("-created_at")

    # Scope by role
    if role == "buyer":
        qs = qs.filter(buyer=user)
    elif role == "seller":
        # Seller sees orders containing their parts (subquery — без лимита параметров).
        qs = qs.filter(items__part__seller=user).distinct()
    # Operators see all

    if params.get("status") == "awaiting_reserve":
        qs = qs.filter(payment_status="awaiting_reserve")
    elif params.get("status") == "paid":
        # «Только оплаченные» — это про статус ОПЛАТЫ, а не заказа: заказа со
        # status="paid" не существует, поэтому фильтруем по payment_status (внесён
        # резерв и далее). Иначе фильтр всегда возвращал пусто.
        qs = qs.filter(payment_status__in=["reserve_paid", "mid_paid",
                                           "customs_paid", "paid"])
    elif params.get("status"):
        qs = qs.filter(status=params["status"])

    total = qs.count()
    orders = list(qs[:limit])
    if not orders:
        return ActionResult(
            text="У вас пока нет заказов.",
            suggestions=["Найти запчасть", "Создать RFQ"],
        )

    # Короткие карточки-сводки (type "order"): номер · сумма · статус · дата.
    # Кликабельны (рендерер ставит data-action="get_order_detail") → полная
    # спецификация заказа разворачивается ТОЛЬКО по клику. Не дёргаем
    # get_order_detail на каждый заказ — список лёгкий и быстрый.
    cards = []
    for o in orders:
        cards.append({
            "type": "order",
            "data": {
                "id": str(o.id), "number": f"ORD-{o.id}",
                "status": o.get_status_display(), "status_code": o.status,
                "payment_status": o.payment_status,
                "total": float(o.total_amount or 0), "currency": "USD",
                "customer": "Покупатель" if role == "seller" else (o.customer_name or "—"),
                "created_at": o.created_at.strftime("%d.%m.%Y"),
                "can_cancel": (role == "buyer" and o.payment_status == "awaiting_reserve"),
            },
        })

    head = (
        f"Заказы на платформе · показано {len(cards)} из {total}:" if role and role.startswith("operator")
        else (f"Заказы по вашим товарам · показано {len(cards)} из {total}:" if role == "seller"
              else f"Ваши заказы · показано {len(cards)} из {total}:")
    )
    if total > len(cards):
        head += f"\nОстальные {total - len(cards)} — уточните статус фильтрами ниже."

    return ActionResult(
        text=head,
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


def _full_order_cards(order, user, role, fallback=None):
    """Единый вид заказа: полная карточка spec_results с таблицей всех позиций.

    Любой хендлер, который раньше отдавал минимальную order-карточку (только
    номер · сумма · статус), теперь должен возвращать через этот хелпер — чтобы
    ВЕЗДЕ заказ открывался одинаково (требование «абсолютно все в едином виде»).
    `fallback` — минимальная карточка на случай, если детальная пуста.
    """
    try:
        d = get_order_detail({"order_id": order.id}, user, role)
        if d.cards:
            return d.cards
    except Exception:
        logger.exception("full order detail render failed for order %s",
                         getattr(order, "id", "?"))
    return [fallback] if fallback else []


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
        # Заказ не найден — обычно случается с устаревшими ссылками
        # из истории кошелька. Даём осмысленный fallback с навигацией.
        return ActionResult(
            text=(f"⚠️ Заказ ORD-{oid} не найден или был удалён.\n"
                  f"Возможно, ссылка устарела (старая запись кошелька)."),
            actions=[
                {"action": "get_orders", "label": "📦 Мои заказы",
                 "params": {}},
                {"action": "get_my_deals", "label": "📋 Все сделки",
                 "params": {}},
            ],
            contextual_actions=[{"action": "go_home", "label": "🏠 Главная"}],
        )

    # Доступ
    is_seller = (role == "seller" and any(
        it.part and it.part.seller_id == user.id for it in o.items.all()
    ))
    is_buyer = (o.buyer_id == user.id)
    # operator/admin/staff/superuser видят любой заказ (контроль платформы).
    is_op = (role.startswith("operator") or role == "admin"
             or getattr(user, "is_staff", False) or getattr(user, "is_superuser", False))
    if not (is_buyer or is_seller or is_op):
        return ActionResult(text="Нет доступа к этому заказу.")

    # ── Spec_results таблица (единый вид как в RFQ) ──
    spec_items = []
    _STATUS_BADGE_RU = {"trusted": "Надёжный", "sandbox": "Песочница",
                         "risky": "Рисковый", "rejected": "Исключён"}
    # Map статус-кода Order → русский label (для per-item статусов).
    # str(...) обязательно — choices содержат gettext_lazy proxy-объекты,
    # которые JSON-сериализатор не понимает.
    _ORDER_STATUS_LABEL_RU = ({k: str(v) for k, v in Order._meta.get_field("status").choices}
                              if hasattr(Order, "_meta") else {})
    found_n = 0
    total_spec_usd = 0.0
    # Доставка: распределяем logistics_cost по позициям пропорционально весу/qty
    incoterm = (getattr(o, "incoterm", "") or "").upper() or None
    ship_mode = (getattr(o, "shipping_mode", "") or "").lower() or None
    total_logi = float(getattr(o, "logistics_cost", 0) or 0)
    # Базовая оценка дней доставки по basis
    _SHIP_DAYS = {"FOB": 3, "CIF": 28, "CIP": 30, "DDP": 35, "EXW": 1}
    ship_days_est = _SHIP_DAYS.get(incoterm or "", None)
    # Распределяем фрахт по числу позиций (грубо, для отображения)
    items_count = o.items.count() or 1
    ship_per_item = total_logi / items_count if total_logi else 0
    for it in o.items.all():
        if is_seller and (not it.part or it.part.seller_id != user.id):
            continue
        mp = it.part
        qty = it.quantity or 1
        price = float(it.unit_price or 0)
        if mp:
            sup_status = "trusted"
            sup_rating = 91.6
            sup_username = ""
            sup_id = mp.seller_id or 0
            # Fallback: имя из User напрямую (если нет UserProfile)
            try:
                if mp.seller:
                    sup_username = mp.seller.username
            except Exception:
                pass
            try:
                from marketplace.models import UserProfile as _UP
                _p = _UP.objects.filter(user_id=sup_id).only(
                    "supplier_status", "rating", "user").first()
                if _p:
                    sup_status = _p.supplier_status or "trusted"
                    sup_rating = float(_p.rating or 90.0)
                    try:
                        if _p.user and _p.user.username:
                            sup_username = _p.user.username
                    except Exception:
                        pass
            except Exception:
                pass
            badge_label = _STATUS_BADGE_RU.get(sup_status, "Надёжный")
            if is_op and sup_username:
                badge_text = f"{badge_label} · {sup_username}"
            else:
                badge_text = badge_label
            # Per-item статус (если задан) или общий статус заказа
            _item_eff = (it.status or o.status) if hasattr(it, "status") else o.status
            spec_items.append({
                "status": "in_stock",
                "id": mp.oem_number or "—",
                "name": _clean_title(mp.title or "")[:80] or "—",
                "brand": mp.brand.name if mp.brand else "—",
                "condition": (mp.condition or "oem"),
                "price": price,
                "qty": qty,
                "weight": f"{mp.gross_weight_kg} кг" if mp.gross_weight_kg else "—",
                "currency": "USD",
                "supplier_status": sup_status,
                "supplier_status_badge": badge_text,
                "supplier_rating": round(sup_rating, 1),
                "alt_offers": 0,
                # Для оператора — клик-action чтобы связаться с поставщиком
                "supplier_id": sup_id if is_op else None,
                "supplier_username": sup_username if is_op else None,
                # Доставка (отображается если выбран Incoterm)
                "ship_cost": ship_per_item if ship_per_item > 0 else None,
                "ship_mode": ship_mode,
                "ship_days": ship_days_est,
                "order_id": o.id,  # клик по ячейке → track_shipment(order_id)
                # Per-item статус — поставщики двигают позиции независимо
                "item_status": _item_eff,
                "item_status_label": _ORDER_STATUS_LABEL_RU.get(_item_eff, _item_eff),
                # для группировки на фронте + анонимизация для покупателя
                "_seller_id": sup_id,
            })
            found_n += 1
            total_spec_usd += price * qty
        else:
            spec_items.append({
                "status": "not_found",
                "id": "—", "name": "", "qty": qty,
            })
    # Meta-блок: всё что раньше было в draft-карточке (статус/оплата/сумма/etc)
    meta = [
        {"label": "Заказ",      "value": f"ORD-{o.id}"},
        {"label": "Статус",     "value": o.get_status_display()},
        {"label": "Оплата",     "value": o.get_payment_status_display()},
        {"label": "Сумма",      "value": f"${float(o.total_amount or 0):,.2f}"},
        {"label": "Покупатель", "value": (o.customer_name or "—") if role != "seller" else "Покупатель"},
        {"label": "Создан",     "value": o.created_at.strftime("%d.%m.%Y %H:%M") if o.created_at else "—"},
    ]
    # PIVOT 2026-05-27: sub-order контекст
    if getattr(o, "is_sub_order", False) and o.parent_order_id:
        # Сколько ещё операторов ведут эту общую сделку
        siblings = o.parent_order.sub_orders.exclude(id=o.id).count() if o.parent_order else 0
        meta.append({
            "label": "Общий заказ",
            "value": f"ORD-{o.parent_order_id} · ещё {siblings} операторов" if siblings else f"ORD-{o.parent_order_id}",
        })
    elif o.sub_orders.exists():
        # Это parent-order — показываем сколько частей
        n_subs = o.sub_orders.count()
        meta.append({
            "label": "Частей у операторов",
            "value": f"{n_subs} sub-orders",
        })
    if o.reserve_amount:
        meta.append({"label": "Резерв 10%", "value": f"${float(o.reserve_amount):,.2f}"})
    if o.logistics_cost:
        meta.append({"label": "Логистика", "value": f"${float(o.logistics_cost):,.2f}"})
    if incoterm:
        _MODE_RU = {"sea": "морем", "air": "авиа", "auto": "авто"}
        mode_ru = _MODE_RU.get(ship_mode, "") if ship_mode else ""
        v = incoterm + (f" · {mode_ru}" if mode_ru else "")
        if ship_days_est:
            v += f" · ~{ship_days_est}д"
        meta.append({"label": "Базис / доставка", "value": v})
    # «Куда» (назначение) показываем ВСЕГДА — груз всегда идёт в конкретное место.
    # Детализация зависит от базиса Incoterms:
    #   • EXW — самовывоз покупателем со склада продавца (точки назначения нет);
    #   • FOB/FAS/CFR/CIF — порт-термы: до порта назначения (страна + порт);
    #   • CPT/CIP/DAP/DPU/DDP — до места/двери: адрес назначения.
    # Продавцу точный адрес покупателя не раскрываем (анти-сговор): он грузит
    # до порта назначения, последнюю милю до двери ведёт оператор.
    _inc = incoterm or "FOB"
    _port_kind = {"sea": "морской порт", "air": "аэропорт",
                  "auto": "авто-терминал"}.get(ship_mode, "порт")
    _addr = (o.delivery_address or "").strip()
    _addr = _addr[:80] if (_addr and _addr != "—") else ""
    if _inc == "EXW":
        _kuda = "Самовывоз покупателем со склада продавца"
    elif role == "seller" or _inc in {"FOB", "FAS", "CFR", "CIF"}:
        _kuda = f"🇷🇺 Россия · {_port_kind} назначения"
    else:
        _kuda = _addr or "🇷🇺 Россия"
    meta.append({"label": "Куда", "value": _kuda})
    # Документы
    docs_count = o.documents.count() if hasattr(o, "documents") else 0
    if docs_count:
        meta.append({"label": "Документы", "value": f"{docs_count} файлов"})
    # Трек-номер если есть
    if getattr(o, "tracking_number", None):
        meta.append({"label": "Трек-номер", "value": str(o.tracking_number)})

    spec_card = {
        "type": "spec_results",
        "data": {
            "title": f"Заказ ORD-{o.id}",
            "meta": meta,
            "found": found_n,
            "analogue": 0,
            "not_found": sum(1 for s in spec_items if s.get("status") == "not_found"),
            "items": spec_items,
            "more_count": 0,
            "offers_count": found_n,
            "sellers_count": len({(s.get('supplier_status_badge') or '') for s in spec_items if s.get('supplier_status_badge')}),
            "best_mix": int(total_spec_usd) if total_spec_usd else None,
            "total": int(total_spec_usd) if total_spec_usd else None,
            "currency": "USD",
            "foot_info": f"{found_n} позиций · оплата: {o.get_payment_status_display()}",
        },
    }

    # ── По поставщикам: разбивка с per-supplier статусом ──────────
    # Поставщики двигают свои позиции независимо — общий Order.status
    # ничего не говорит о реальном положении дел по каждой части.
    from collections import defaultdict as _dd
    by_seller = _dd(lambda: {"items": [], "amount": 0.0, "qty": 0, "statuses": set(),
                              "name": "", "id": 0})
    for it in o.items.all():
        if is_seller and (not it.part or it.part.seller_id != user.id):
            continue
        sid = it.part.seller_id if it.part else 0
        g = by_seller[sid]
        g["id"] = sid
        if not g["name"]:
            g["name"] = (it.part.seller.username if (it.part and it.part.seller_id) else "—")
        g["items"].append(it)
        g["amount"] += float(it.unit_price or 0) * (it.quantity or 0)
        g["qty"] += (it.quantity or 0)
        g["statuses"].add((it.status if hasattr(it, "status") and it.status else None) or o.status)
    supplier_rows = []
    if len(by_seller) > 1:  # показываем только при мульти-поставщике
        # Какие item.id попали в какой-то shipment — для inline-трекинга
        _item_to_shipment = {}
        try:
            for sh in o.shipments.prefetch_related("items").all():
                for it in sh.items.all():
                    _item_to_shipment[it.id] = sh
        except Exception:
            pass
        for idx, (sid, g) in enumerate(sorted(by_seller.items(), key=lambda kv: -kv[1]["amount"])):
            display_name = g["name"] if (is_op or is_seller) else f"Поставщик {chr(ord('A') + idx)}"
            sts = sorted(g["statuses"])
            if len(sts) == 1:
                stage_lbl = _ORDER_STATUS_LABEL_RU.get(sts[0], sts[0])
            else:
                stage_lbl = "смешанный"
            # Тон бейджа по «слабейшему звену»
            _ORDER = ["awaiting_reserve","reserve_paid","confirmed","in_production","ready_to_ship",
                      "transit_abroad","customs","transit_rf","issuing","delivered","completed"]
            worst = min(sts, key=lambda s: _ORDER.index(s) if s in _ORDER else 99)
            tone = "warn" if worst in ("awaiting_reserve","reserve_paid","confirmed","in_production") else "ok"
            # Inline-трекинг: ищем shipment, в котором лежит хотя бы одна позиция группы
            sh = None
            for it in g["items"]:
                if it.id in _item_to_shipment:
                    sh = _item_to_shipment[it.id]
                    break
            sub_parts = [f"{len(g['items'])} поз · {g['qty']} шт · ${g['amount']:,.0f}"]
            if sh:
                kind_lbl = sh.get_kind_display()
                sub_parts.append(f"📦 {kind_lbl} #{sh.id}")
                if sh.eta_delivery:
                    sub_parts.append(f"ETA {sh.eta_delivery.strftime('%d.%m.%Y')}")
                if is_op and sh.tracking_number:
                    sub_parts.append(f"трек {sh.tracking_number}")
                if is_op and sh.carrier:
                    sub_parts.append(f"перевозчик {sh.carrier}")
            else:
                # Нет партии — нужно понимать почему: ждёт консолидации или
                # пока не дошёл до ready_to_ship
                if worst in ("ready_to_ship",):
                    sub_parts.append("⏸ на складе платформы, ждёт консолидации")
                elif worst in ("transit_abroad","customs","transit_rf","issuing","delivered","completed"):
                    sub_parts.append("⚠ ушёл в путь без оформленной партии")
                else:
                    sub_parts.append("ещё у поставщика — партия не нужна")
            _STAGE_ORDER_S = ["awaiting_reserve","reserve_paid","confirmed","in_production",
                              "ready_to_ship","transit_abroad","customs","transit_rf",
                              "issuing","delivered","completed"]
            _worst_idx = _STAGE_ORDER_S.index(worst) if worst in _STAGE_ORDER_S else 0
            _pct = int(round(_worst_idx / max(1, len(_STAGE_ORDER_S) - 1) * 100))
            # «Дальше: actor event» по слабейшему звену группы
            _actor_map = {
                "awaiting_reserve": ("Покупатель",        "оплачивает резерв 10%"),
                "reserve_paid":     ("Поставщик",         "подтверждает заказ"),
                "confirmed":        ("Поставщик",         "запускает производство"),
                "in_production":    ("Поставщик",         "сообщает о готовности"),
                "ready_to_ship":    ("Поставщик",         "оформляет отгрузку"),
                "transit_abroad":   ("Перевозчик",        "везёт груз до границы РФ"),
                "customs":          ("Таможенный брокер", "проводит оформление"),
                "transit_rf":       ("Перевозчик",        "везёт груз по РФ"),
                "issuing":          ("Перевозчик",        "выдаёт груз получателю"),
                "delivered":        ("Покупатель",        "подтверждает приёмку"),
                "completed":        ("—",                 "Партия закрыта"),
            }
            _next_a, _next_e = _actor_map.get(worst, ("—", "—"))
            # SLA-светофор + дни в стадии (по последнему status_changed для заказа)
            from django.utils import timezone as _tz_g
            from marketplace.models import OrderEvent as _OE_g
            _STAGE_SLA_G = {"awaiting_reserve":2,"reserve_paid":2,"confirmed":1,"in_production":7,
                            "ready_to_ship":2,"transit_abroad":14,"customs":5,"transit_rf":7,"issuing":3}
            _last_ev_g = (_OE_g.objects.filter(order_id=o.id, event_type="status_changed")
                           .order_by("-created_at").first())
            _entered_g = _last_ev_g.created_at if _last_ev_g else o.created_at
            _days_here_g = max(0, int((_tz_g.now() - _entered_g).total_seconds() / 86400)) if _entered_g else 0
            _sla_g = _STAGE_SLA_G.get(worst, 0)
            if _sla_g:
                if _days_here_g > _sla_g:
                    _sla_lbl_g = f"просрочка · +{_days_here_g - _sla_g} дн"
                elif _days_here_g >= _sla_g * 0.8:
                    _sla_lbl_g = f"скоро дедлайн · {max(0,_sla_g-_days_here_g)} дн"
                else:
                    _sla_lbl_g = f"в срок · ещё {_sla_g - _days_here_g} дн"
            else:
                _sla_lbl_g = "—"
            # Оплата по поставщику (proportional split от общей оплаты заказа)
            _total_ord = float(o.total_amount or 0) or 1.0
            _share = float(g['amount']) / _total_ord
            _reserve_supplier = float(o.reserve_amount or 0) * _share
            _final_supplier = max(0.0, float(g['amount']) - _reserve_supplier)
            if o.payment_status == "paid":
                _pay_lbl_g = f"оплачен полностью (${g['amount']:,.0f})"
            elif o.payment_status == "reserve_paid":
                _pay_lbl_g = f"резерв оплачен ${_reserve_supplier:,.0f} · остаток ${_final_supplier:,.0f}"
            elif o.payment_status == "awaiting_reserve":
                _pay_lbl_g = f"ждём резерв ${_reserve_supplier:,.0f}"
            else:
                _pay_lbl_g = o.get_payment_status_display()
            # Meta-блок (как в shipment-карточке)
            _meta = [
                {"lbl": "Сумма",            "val": f"${g['amount']:,.0f}"},
                {"lbl": "Оплата",           "val": _pay_lbl_g},
                {"lbl": "Состав",           "val": f"{len(g['items'])} поз · {g['qty']} шт"},
                {"lbl": "В текущей стадии", "val": f"{_days_here_g} дн"},
                {"lbl": "SLA",              "val": _sla_lbl_g},
            ]
            if sh:
                if sh.eta_delivery:
                    _meta.append({"lbl": "ETA", "val": sh.eta_delivery.strftime("%d.%m.%Y")})
                _meta.append({"lbl": "Партия", "val": f"{sh.get_kind_display()} #{sh.id}"})
                if is_op and sh.tracking_number:
                    _meta.append({"lbl": "Трек", "val": sh.tracking_number})
                if is_op and sh.carrier:
                    _meta.append({"lbl": "Перевозчик", "val": sh.carrier})
            else:
                if worst in ("ready_to_ship",):
                    _meta.append({"lbl": "Партия", "val": "⏸ на складе, ждёт консолидации"})
                elif worst in ("transit_abroad","customs","transit_rf","issuing","delivered","completed"):
                    _meta.append({"lbl": "Партия", "val": "⚠ ушёл без оформления"})
                else:
                    _meta.append({"lbl": "Партия", "val": "ещё у поставщика"})
            # Stages-пилюли — ПО БАЗИСУ поставки (FOB/CIP/DDP), а не общий DDP-цикл.
            _stages_pills = []
            for _pl_lbl, _pl_pd, _pl_dcode, _pl_fs, _pl_fe in shipment_flow(getattr(o, "incoterm", "") or "DDP"):
                if _pl_dcode == "pay":
                    # Источник истины — payment_status заказа (как в shipment-карточке),
                    # а не агрегат статусов позиций: у отменённого заказа worst=
                    # 'cancelled' ложно давал «резерв оплачен».
                    _pl_done = o.payment_status not in (
                        "awaiting_reserve", "pending", "", "cancelled", "refunded")
                else:
                    _pl_done = TRACKING_INDEX.get(worst, 0) >= TRACKING_INDEX.get(_pl_dcode, 99)
                _stages_pills.append({"label": _pl_lbl, "done": _pl_done})
            # Per-supplier decision: «ждать всех» vs «отправлять отдельно».
            # Показываем только когда выбор ещё имеет смысл — поставщик не уехал.
            # Сохранённое предпочтение читаем из logistics_meta.per_supplier.
            _per = (o.logistics_meta or {}).get("per_supplier") or {}
            _cur_choice = _per.get(str(sid))  # "consolidate" / "split" / None
            _shipped_already = bool(sh and (sh.kind == "split" or
                worst in ("transit_abroad","customs","transit_rf","issuing","delivered","completed")))
            _row_decision = None
            if not _shipped_already and worst not in ("delivered","completed","cancelled"):
                _row_decision = {
                    "current": _cur_choice,
                    "buttons": [
                        {"key":"consolidate","label":"Ждать всех","icon":"📦",
                         "active": _cur_choice == "consolidate",
                         "action":"set_supplier_decision",
                         "params":{"order_id": o.id, "seller_id": sid, "choice": "consolidate"}},
                        {"key":"split","label":"Отправлять отдельно","icon":"🚚",
                         "active": _cur_choice == "split",
                         "action":"set_supplier_decision",
                         "params":{"order_id": o.id, "seller_id": sid, "choice": "split"}},
                    ],
                }
            supplier_rows.append({
                "supplier": display_name,
                "stage_label": stage_lbl,
                "stage_tone": tone,
                "progress_pct": _pct,
                "next_actor": _next_a,
                "next_event": _next_e,
                "meta": _meta,
                "stages": _stages_pills,
                "decision": _row_decision,
            })

    # Позиции (legacy compact rows для draft-сводки) — с именем поставщика
    items_rows = []
    for it in o.items.all():
        if is_seller and (not it.part or it.part.seller_id != user.id):
            continue
        sup_name = ""
        if is_op and it.part and it.part.seller_id:
            try:
                sup_name = it.part.seller.username
            except Exception:
                sup_name = f"#S{it.part.seller_id}"
        label = (
            f"{it.part.oem_number if it.part else '—'} · "
            f"{(it.part.title if it.part else '—')[:40]}"
            + (f" · {sup_name}" if sup_name else "")
        )
        items_rows.append({
            "label": label,
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
        {"label": "Покупатель",       "value": (o.customer_name or "—") if role != "seller" else "Покупатель"},
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
    # Трекинг — компактная shipment-карточка (внутри неё «📊 Отчёт по поставке»).
    # Доступен всем ролям: покупателю, продавцу и оператору.
    if o.status != "cancelled":
        actions.append({"label": "📦 Трекинг",
                         "action": "track_shipment",
                         "params": {"order_id": o.id}})
    # Счёт на оплату — это артефакт продавца/оператора, и только пока оплата не закрыта.
    # Покупателю «создать счёт самому себе» не нужно; после full_paid тоже бессмысленно.
    if (is_seller or is_op) and o.payment_status in ("awaiting_reserve", "reserve_paid", "awaiting_final"):
        actions.append({"label": "Создать счёт на оплату",
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
            actions.append({"label": "Создать упаковочный лист",
                             "action": "generate_packing_list_pdf",
                             "params": {"order_id": o.id}})
            actions.append({"label": "Создать акт качества",
                             "action": "generate_qc_report_pdf",
                             "params": {"order_id": o.id}})
        elif o.status in ("transit_abroad", "customs", "transit_rf", "issuing"):
            actions.append({"label": "▶️ Следующий этап",
                             "action": "advance_order",
                             "params": {"order_id": o.id}})
    # Buyer-кнопки
    if is_buyer:
        if o.payment_status == "reserve_paid" and o.status in ("ready_to_ship", "transit_abroad", "customs", "transit_rf", "issuing", "delivered"):
            actions.append({"label": "💳 Оплатить остаток 90%",
                             "action": "pay_final",
                             "params": {"order_id": o.id}})
        if o.status == "delivered":
            actions.append({"label": "✓ Подтвердить приёмку",
                             "action": "confirm_delivery",
                             "params": {"order_id": o.id}})

    # ── Что дальше: подсказка по текущей фазе заказа ──────────────
    # Buyer и operator видят разные «next steps» в зависимости от роли + статуса.
    next_step_hint = ""
    suggestions = []
    if o.status == "cancelled" or o.payment_status == "refunded":
        next_step_hint = "Заказ отменён. Если нужно повторить — создайте новый RFQ."
        suggestions = ["Создать RFQ", "Все мои заказы"]
    elif o.payment_status == "awaiting_reserve":
        next_step_hint = (
            f"⏳ Дальше: оплатите резерв 10% (${float(o.reserve_amount or 0):,.0f}) — "
            f"после этого продавец подтвердит и запустит производство. "
            f"Срок без оплаты: 7 дней, потом авто-отмена."
        )
        suggestions = ["Оплатить резерв", "Отменить заказ"]
    elif o.status == "reserve_paid":
        next_step_hint = (
            "✓ Резерв оплачен. Дальше: продавец подтверждает заказ и запускает "
            "производство (SLA 24 ч). Следите за статусом — придёт уведомление."
        )
        suggestions = ["Трекинг отгрузки", "Все мои заказы"]
    elif o.status == "in_production":
        next_step_hint = (
            "🏭 В производстве. Дальше: готовность к отгрузке (срок по контракту "
            "с поставщиком). Оплата 90% — перед выходом груза."
        )
        suggestions = ["Когда отгрузка?", "Трекинг", "Документы"]
    elif o.status == "ready_to_ship":
        next_step_hint = (
            "📦 Готов к отгрузке. Дальше: оплата остатка 90% → выход груза с базиса."
        )
        suggestions = ["Оплатить остаток 90%", "Документы"]
    elif o.status in ("transit_abroad", "customs", "transit_rf", "issuing"):
        next_step_hint = (
            f"🚚 В пути ({o.get_status_display()}). ETA можно посмотреть в трекинге. "
            f"Документы (BL, инвойс, упаковочный лист) уже сформированы."
        )
        suggestions = ["Трекинг отгрузки", "Все документы"]
    elif o.status == "delivered":
        next_step_hint = (
            "✓ Доставлен. Дальше: подтвердите приёмку — после этого деньги уйдут "
            "продавцу из эскроу и заказ закроется."
        )
        suggestions = ["Подтвердить приёмку", "Открыть рекламацию", "Документы"]
    elif o.status == "completed":
        next_step_hint = "✓ Заказ закрыт. Эскроу освобождён."
        suggestions = ["Создать новый RFQ", "Все мои заказы", "Аналитика"]

    # Оператор/админ — это сам оператор: подсказки «Спросить/Связаться с оператором»
    # для него бессмысленны. Чистим self-referential пункты.
    if role in ("operator", "admin"):
        suggestions = [s for s in suggestions
                       if "оператор" not in s.lower() and "менеджер" not in s.lower()]

    text = f"Заказ ORD-{o.id} · {o.get_status_display()} · ${o.total_amount:,.2f}"
    if next_step_hint:
        text += "\n\n" + next_step_hint

    _cards = [spec_card]
    if supplier_rows:
        _cards.append({"type": "supplier_tracks", "data": {
            "title": f"📦 По поставщикам — {len(supplier_rows)} участников",
            "items": supplier_rows,
        }})
    return ActionResult(
        text=text,
        cards=_cards,
        actions=actions,
        suggestions=suggestions,
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
    if not _user_can_access_order(o, user, role):
        # Не подтверждаем существование чужого заказа — то же сообщение.
        return ActionResult(text=f"⚠️ Заказ #{oid} не найден")

    # ── Важные поля для шапки: ETA, кто держит мяч, перевозчик ──
    _actor_by_stage = {
        "awaiting_reserve": ("Покупатель",          "оплачивает резерв 10%"),
        "reserve_paid":     ("Поставщик",           "подтверждает заказ"),
        "confirmed":        ("Поставщик",           "запускает производство"),
        "in_production":    ("Поставщик",           "сообщает о готовности к отгрузке"),
        "ready_to_ship":    ("Поставщик",           "оформляет отгрузку"),
        "transit_abroad":   ("Перевозчик",          "везёт груз до границы РФ"),
        "customs":          ("Таможенный брокер",   "проводит оформление"),
        "transit_rf":       ("Перевозчик",          "везёт груз по РФ"),
        "issuing":          ("Перевозчик",          "выдаёт груз получателю"),
        "delivered":        ("Покупатель",          "подтверждает приёмку"),
        "completed":        ("—",                   "Заказ закрыт"),
    }
    _next_actor, _next_event = _actor_by_stage.get(o.status, ("—", "—"))
    _lm = o.logistics_meta or {}
    _eta = _lm.get("eta_delivery") or _lm.get("eta")  # строка дд.мм.гггг
    _tracking_number = _lm.get("tracking_number")
    _carrier = _lm.get("carrier")
    _is_real_op = role in ("operator", "admin") and getattr(user, "is_staff", False)

    # Дополнительные поля для шапки
    from django.utils import timezone as _tz
    from datetime import timedelta as _td
    _items_qs = list(o.items.all())
    _positions = len(_items_qs)
    _qty_total = sum(int(it.quantity or 0) for it in _items_qs)
    _weight = sum(float((it.part.weight if it.part and getattr(it.part, "weight", None) else 0) or 0) * (it.quantity or 0)
                  for it in _items_qs)
    # Сколько уже в текущей стадии (по последнему status_changed)
    from marketplace.models import OrderEvent as _OE2
    _last_ev = (_OE2.objects.filter(order_id=o.id, event_type="status_changed")
                .order_by("-created_at").first())
    _entered = _last_ev.created_at if _last_ev else o.created_at
    _days_here = max(0, int((_tz.now() - _entered).total_seconds() / 86400))
    _STAGE_SLA = {"awaiting_reserve":2,"reserve_paid":2,"confirmed":1,"in_production":7,
                  "ready_to_ship":2,"transit_abroad":14,"customs":5,"transit_rf":7,"issuing":3}
    _sla = _STAGE_SLA.get(o.status, 0)
    if _sla:
        if _days_here > _sla:
            _sla_label, _sla_tone = f"просрочка · +{_days_here - _sla} дн", "bad"
        elif _days_here >= _sla * 0.8:
            _sla_label, _sla_tone = f"скоро дедлайн · {_sla - _days_here} дн", "warn"
        else:
            _sla_label, _sla_tone = f"в срок · ещё {_sla - _days_here} дн", "ok"
    else:
        _sla_label, _sla_tone = "", "info"
    # Прогресс оплаты
    from decimal import Decimal as _D
    _total_d = _D(str(o.total_amount or 0))
    _reserve_d = _D(str(o.reserve_amount or 0))
    if o.payment_status == "paid":
        _pay_label = f"оплачено полностью (${_total_d:,.0f})"
    elif o.payment_status == "reserve_paid":
        _pay_label = f"резерв оплачен ${_reserve_d:,.0f} · остаток ${(_total_d - _reserve_d):,.0f}"
    elif o.payment_status == "awaiting_reserve":
        _pay_label = f"ждём резерв ${_reserve_d:,.0f}"
    else:
        _pay_label = o.get_payment_status_display()

    # ── Per-shipment / per-supplier разбивка (как в track_order) ──
    _sh_parts = []
    _total_ord = float(o.total_amount or 0) or 1.0
    # Русские labels для статусов (без gettext_lazy proxy)
    _STAGE_LABEL_RU = ({k: str(v) for k, v in Order._meta.get_field("status").choices}
                        if hasattr(Order, "_meta") else {})
    _shipments = list(o.shipments.prefetch_related("items__part__seller").all()) \
                  if hasattr(o, "shipments") else []
    if _shipments:
        _in_sh_ids = {it.id for sh in _shipments for it in sh.items.all()}
        for idx, sh in enumerate(sorted(_shipments, key=lambda s: -float(s.total_amount or 0))):
            its = list(sh.items.all())
            names = sorted({(it.part.seller.username if (it.part and it.part.seller_id) else "—") for it in its})
            disp = ", ".join(names) if _is_real_op else f"Партия {idx + 1}"
            amt = float(sh.total_amount or 0)
            _sh_parts.append({
                "supplier":     f"{disp} · {sh.get_kind_display()}",
                "amount":       amt,
                "amount_pct":   int(round(amt / _total_ord * 100)) if _total_ord else 0,
                "items_count":  len(its),
                "stage_label":  sh.get_status_display(),
                "stage_code":   sh.status,
                "shipment_id":  sh.id,
            })
        _leftover = [it for it in o.items.select_related("part__seller") if it.id not in _in_sh_ids]
        if _leftover:
            from collections import defaultdict as _dd
            _lg = _dd(lambda: {"items":[],"amount":0.0,"name":"","statuses":set()})
            for it in _leftover:
                sid = it.part.seller_id if it.part else 0
                g = _lg[sid]
                if not g["name"]:
                    g["name"] = it.part.seller.username if (it.part and it.part.seller_id) else "—"
                g["items"].append(it)
                g["amount"] += float(it.unit_price or 0) * (it.quantity or 0)
                g["statuses"].add((it.status if hasattr(it,"status") and it.status else None) or o.status)
            base = len(_sh_parts)
            for j, (sid, g) in enumerate(sorted(_lg.items(), key=lambda kv: -kv[1]["amount"])):
                disp = g["name"] if _is_real_op else f"Поставщик {chr(ord('A') + base + j)}"
                if len(g["statuses"]) == 1:
                    only = next(iter(g["statuses"]))
                    lbl = _STAGE_LABEL_RU.get(only, only)
                else:
                    lbl = "смешанный"
                _sh_parts.append({
                    "supplier":    f"{disp} · ждёт партию",
                    "amount":      g["amount"],
                    "amount_pct":  int(round(g["amount"]/_total_ord*100)) if _total_ord else 0,
                    "items_count": len(g["items"]),
                    "stage_label": lbl,
                    "stage_code":  next(iter(g["statuses"])) if len(g["statuses"]) == 1 else "",
                })
    elif o.items.count() > 0:
        # Нет Shipment'ов — группируем по поставщику (когда их >1)
        from collections import defaultdict as _dd
        _g_by_sup = _dd(lambda: {"items":[],"amount":0.0,"name":"","statuses":set()})
        for it in o.items.select_related("part__seller"):
            sid = it.part.seller_id if it.part else 0
            g = _g_by_sup[sid]
            if not g["name"]:
                g["name"] = it.part.seller.username if (it.part and it.part.seller_id) else "—"
            g["items"].append(it)
            g["amount"] += float(it.unit_price or 0) * (it.quantity or 0)
            g["statuses"].add((it.status if hasattr(it,"status") and it.status else None) or o.status)
        if len(_g_by_sup) > 1:
            for idx, (sid, g) in enumerate(sorted(_g_by_sup.items(), key=lambda kv: -kv[1]["amount"])):
                disp = g["name"] if _is_real_op else f"Поставщик {chr(ord('A') + idx)}"
                if len(g["statuses"]) == 1:
                    only = next(iter(g["statuses"]))
                    lbl = _STAGE_LABEL_RU.get(only, only)
                else:
                    lbl = "смешанный"
                _sh_parts.append({
                    "supplier":    disp,
                    "amount":      g["amount"],
                    "amount_pct":  int(round(g["amount"]/_total_ord*100)) if _total_ord else 0,
                    "items_count": len(g["items"]),
                    "stage_label": lbl,
                    "stage_code":  next(iter(g["statuses"])) if len(g["statuses"]) == 1 else "",
                })

    # ── Агрегатный статус заказа: по слабейшему звену ─────────────
    # Когда parts на разных стадиях — общий статус НЕ может быть «Транзит»,
    # потому что заказ покупателю не закрыт пока самый медленный не дойдёт.
    # Берём «min(stage_idx)» среди всех групп/партий — это честное состояние.
    _agg_status = o.status
    _agg_label = o.get_status_display()
    _ORDER_CODES_SH = ["awaiting_reserve","reserve_paid","confirmed","in_production",
                       "ready_to_ship","transit_abroad","customs","transit_rf",
                       "issuing","delivered","completed"]
    if _sh_parts:
        _idxs = []
        for p in _sh_parts:
            code = p.get("stage_code", "")
            if code and code in _ORDER_CODES_SH:
                _idxs.append(_ORDER_CODES_SH.index(code))
        if _idxs:
            _min_i, _max_i = min(_idxs), max(_idxs)
            _agg_status = _ORDER_CODES_SH[_min_i]
            if _min_i != _max_i:
                _from = _STAGE_LABEL_RU.get(_ORDER_CODES_SH[_min_i], _ORDER_CODES_SH[_min_i])
                _to   = _STAGE_LABEL_RU.get(_ORDER_CODES_SH[_max_i], _ORDER_CODES_SH[_max_i])
                _agg_label = f"Частично · от «{_from}» до «{_to}»"
            else:
                _agg_label = _STAGE_LABEL_RU.get(_agg_status, _agg_status)

    # Этапы отгрузки — ПО БАЗИСУ поставки (FOB/CIP/DDP): показываем только то,
    # что реально ведёт платформа. FOB — до передачи в порту, CIP — до прибытия
    # в порт назначения, DDP — полный цикл до двери.
    from datetime import timedelta as _td

    from django.utils import timezone as _tz
    _LABEL2CODE = {
        "Подтверждён поставщиком": "confirmed", "В производстве": "in_production",
        "Готов к отгрузке": "ready_to_ship", "Транзит (зарубеж)": "transit_abroad",
        "Таможня": "customs", "Транзит (РФ)": "transit_rf", "Выдача": "issuing",
        "Доставлен": "delivered", "Завершён": "completed",
    }
    _entry = {"pending": o.created_at}
    for _ev in o.events.order_by("created_at"):
        if _ev.event_type == "reserve_paid":
            _entry.setdefault("reserve_paid", _ev.created_at)
        elif _ev.event_type == "status_changed":
            _code = _LABEL2CODE.get((_ev.meta or {}).get("to", ""))
            if _code:
                _entry.setdefault(_code, _ev.created_at)
    _now = _tz.now()
    _flow = shipment_flow(getattr(o, "incoterm", "") or "DDP")
    _stages_out = []
    _plan_total = 0
    for _label, _pd, _dcode, _fs, _fe in _flow:
        _plan_total += _pd
        if _dcode == "pay":
            _done = o.payment_status not in ("awaiting_reserve", "pending", "")
        else:
            _done = TRACKING_INDEX.get(_agg_status, 0) >= TRACKING_INDEX.get(_dcode, 99)
        _st, _en = _entry.get(_fs), _entry.get(_fe)
        if _st and _en:
            _actual, _state = max(0, (_en - _st).days), "done"
        elif _st and o.status != "cancelled":
            _actual, _state = max(0, (_now - _st).days), "current"
        else:
            _actual, _state = None, "future"
        _stages_out.append({"label": _label, "days": _pd, "actual": _actual,
                            "state": _state, "done": _done})
    _deadline = (o.created_at + _td(days=_plan_total)).strftime("%d.%m.%Y")

    return ActionResult(
        text=f"Трекинг заказа ORD-{o.id} — статус: {_agg_label}",
        cards=[{
            "type": "shipment",
            "data": {
                "order_id": str(o.id),
                "status": _agg_status,
                "status_label": _agg_label,
                "total": float(o.total_amount or 0),
                "currency": "USD",
                "payment_status_label": _pay_label,
                "eta": _eta,
                "next_actor": _next_actor,
                "next_event": _next_event,
                "positions": _positions,
                "qty_total": _qty_total,
                "weight_kg": round(_weight, 1) if _weight else None,
                "days_in_stage": _days_here,
                "sla_label": _sla_label,
                "sla_tone": _sla_tone,
                # Per-shipment / per-supplier разбивка (показывается в рендерере
                # отдельным блоком если есть >1 партия/поставщик).
                "parts": _sh_parts,
                # tracking_number/carrier — только реальному оператору (анти-сговор)
                "tracking_number": _tracking_number if _is_real_op else None,
                "carrier":         _carrier         if _is_real_op else None,
                # Stages рисуем по агрегату (_agg_status), а не по o.status —
                # иначе при «частичный» полоска врала бы (Транзит зелёный когда
                # C ещё в производстве).
                "total_planned_days": _plan_total,
                "deadline": _deadline,
                "stages": _stages_out,
            },
        }],
        actions=_build_track_shipment_actions(o, role, user),
        suggestions=["Все заказы в пути", "Открыть карту"],
    )


def _build_track_shipment_actions(o, role, user):
    """Кнопки под shipment-карточкой — те же, что покупатель видит на get_order_detail.
    SLA-отчёт, оплата остатка, подтверждение приёмки + базовые навигационные."""
    acts = [
        {"label": "📄 Все документы", "action": "list_order_documents",
         "params": {"order_id": o.id}},
        {"label": "📊 Отчёт по поставке", "action": "track_order",
         "params": {"order_id": o.id}},
        {"label": "Детали заказа", "action": "get_order_detail",
         "params": {"order_id": o.id}},
    ]
    is_buyer = role == "buyer"
    is_seller = role == "seller"
    is_op = role in ("operator", "admin")
    # Buyer-специфичное
    if is_buyer:
        if o.payment_status == "reserve_paid" and o.status in (
                "ready_to_ship", "transit_abroad", "customs", "transit_rf",
                "issuing", "delivered"):
            acts.append({"label": "💳 Оплатить остаток 90%", "action": "pay_final",
                          "params": {"order_id": o.id}})
        if o.status == "delivered":
            acts.append({"label": "✓ Подтвердить приёмку",
                          "action": "confirm_delivery",
                          "params": {"order_id": o.id}})
    # Seller/Operator — счёт пока оплата не закрыта
    if (is_seller or is_op) and o.payment_status in (
            "awaiting_reserve", "reserve_paid", "awaiting_final"):
        acts.append({"label": "Создать счёт на оплату",
                      "action": "generate_invoice_pdf",
                      "params": {"order_id": o.id}})
    return acts


def _plural_ru(n, one, few, many):
    """Русская плюрализация: 1 заказ / 2 заказа / 5 заказов."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not (12 <= n % 100 <= 14):
        return few
    return many


def _seller_deals(user, params):
    """«Мои сделки» для продавца — секции по этапам жизненного цикла в стиле
    «Срочных задач» (inbox): иконка + счётчик + кнопка действия на каждой строке.
    Имя покупателя продавцу не показываем (только «Покупатель»).

    Фильтры (params.filter):
      • action  — только то, где нужно действие продавца (подтвердить/отгрузить)
      • transit — в пути / на таможне / выдача
      • done    — завершённые (архив)
      • active / all (по умолчанию) — все активные этапы (без архива)
    """
    from collections import defaultdict
    from marketplace.models import RFQ, Order

    # Тот же резолв «эффективного продавца», что и в seller_inbox (demo/test
    # юзеры без своего каталога видят demo_seller) — чтобы объединённый экран
    # был консистентен независимо от точки входа.
    from .seller_actions import _effective_seller
    user = _effective_seller(user)

    flt = (params or {}).get("filter", "active")
    if flt == "all":
        flt = "active"

    # status → (иконка, заголовок этапа, кнопка действия, action-обработчик)
    # Порядок = приоритет внимания: сначала то, где ждут продавца.
    STAGE_META = [
        ("reserve_paid",   "✅", "Новые заказы — подтвердить и в производство",
         "▶️ Подтвердить", "advance_order"),
        ("ready_to_ship",  "🚚", "Готовы к отгрузке",
         "🚚 Отгрузить", "ship_order"),
        ("confirmed",      "🏭", "Подтверждены — формирование заказа",
         "📦 Открыть", "get_order_detail"),
        ("in_production",  "⚙️", "В производстве",
         "📦 Открыть", "get_order_detail"),
        ("transit_abroad", "🚢", "Транзит за рубеж",
         "📦 Открыть", "get_order_detail"),
        ("shipped_abroad", "🚢", "Отгружены за рубеж",
         "📦 Открыть", "get_order_detail"),
        ("shipped",        "📦", "Отгружены",
         "📦 Открыть", "get_order_detail"),
        ("customs",        "🛃", "На таможне РФ",
         "📦 Открыть", "get_order_detail"),
        ("transit_rf",     "🚛", "Транзит по РФ",
         "📦 Открыть", "get_order_detail"),
        ("issuing",        "📬", "Выдача / приёмка",
         "📦 Открыть", "get_order_detail"),
        ("pending",        "⏳", "Ждут оплату резерва покупателем",
         "📦 Открыть", "get_order_detail"),
        ("delivered",      "🏁", "Доставлены — оплата из эскроу",
         "📦 Открыть", "get_order_detail"),
        ("completed",      "🏁", "Завершённые",
         "📦 Открыть", "get_order_detail"),
    ]
    ATTENTION = {"reserve_paid", "ready_to_ship"}
    TRANSIT   = {"transit_abroad", "shipped_abroad", "shipped", "customs", "transit_rf", "issuing"}
    DONE      = {"delivered", "completed"}
    PAID_OK   = {"paid", "final_paid", "customs_paid"}

    if flt == "action":
        allowed = ATTENTION
    elif flt == "transit":
        allowed = TRANSIT
    elif flt == "done":
        allowed = DONE
    else:  # active — всё, кроме архива
        allowed = None

    PAY_LABEL = {
        "awaiting_reserve": "ждёт резерв 10%",
        "reserve_paid":     "резерв 10% оплачен",
        "mid_paid":         "50% оплачено",
        "customs_paid":     "таможня оплачена",
        "paid":             "оплачен полностью",
        "final_paid":       "оплачен полностью",
        "refund_pending":   "возврат в обработке",
        "refunded":         "возвращён",
    }

    qs = (Order.objects.filter(items__part__seller=user).distinct()
          .order_by("-created_at"))
    if allowed is not None:
        qs = qs.filter(status__in=list(allowed))
    elif flt == "active":
        qs = qs.exclude(status__in=list(DONE) + ["cancelled"])
    qs = list(qs[:300])

    groups = defaultdict(list)
    for o in qs:
        groups[o.status].append(o)

    def _sub(o, is_done):
        amt = f"${float(o.total_amount or 0):,.0f}"
        inc = (o.incoterm or "").upper()
        bits = [amt]
        pl = PAY_LABEL.get(o.payment_status or "")
        if pl:
            bits.append(pl)
        if inc:
            bits.append(f"базис {inc}")
        sub = " · ".join(bits)
        # SLA-предупреждение спереди (только для активных — на архиве это история).
        if not is_done:
            if o.sla_status == "breached":
                sub = "Просрочка SLA · " + sub
            elif o.sla_status == "at_risk":
                sub = "SLA под угрозой · " + sub
        return sub

    sections = []
    n_action = 0
    n_breached_total = 0

    for status_code, icon, stage_title, btn_label, btn_action in STAGE_META:
        orders = groups.get(status_code) or []
        if not orders:
            continue
        is_done = status_code in DONE
        if status_code in ATTENTION:
            n_action += len(orders)
        if not is_done:
            n_breached_total += sum(1 for o in orders if o.sla_status == "breached")
        # SLA-нарушения наверх внутри этапа.
        orders.sort(key=lambda o: (
            0 if o.sla_status == "breached" else 1 if o.sla_status == "at_risk" else 2,
            -o.id,
        ))
        rows = []
        for o in orders[:12]:
            # «Отгрузить» доступно только когда заказ уже оплачен; иначе — открыть.
            if status_code == "ready_to_ship" and (o.payment_status or "") not in PAID_OK:
                lbl, act = "📦 Открыть", "get_order_detail"
            else:
                lbl, act = btn_label, btn_action
            rows.append({
                "title":    f"Заказ #{o.id} · Покупатель",
                "subtitle": _sub(o, is_done),
                "action":   {"label": lbl, "action": act, "params": {"order_id": o.id}},
            })
        n_total = len(orders)
        if n_total > 12:
            rows.append({
                "title":    f"… ещё {n_total - 12} {_plural_ru(n_total - 12, 'заказ', 'заказа', 'заказов')} на этом этапе",
                "subtitle": "—",
            })
        sections.append({"icon": icon, "title": stage_title, "rows": rows})

    # ── RFQ-фазы (только в active) ──
    n_leads = 0
    if flt == "active":
        # (а) Мои КП — уже отправленные котировки, ждут решения покупателя.
        quoted_rfqs = list(
            RFQ.objects.filter(quotes__seller=user, status="quoted")
                       .distinct().order_by("-created_at")[:12]
        )
        if quoted_rfqs:
            rrows = [{
                "title":    f"RFQ #{r.id} · Покупатель",
                "subtitle": f"{r.items.count()} {_plural_ru(r.items.count(), 'позиция', 'позиции', 'позиций')} · КП отправлен, ждём ответа",
                "action":   {"label": "💬 Открыть КП", "action": "respond_rfq_form",
                             "params": {"rfq_id": r.id}},
            } for r in quoted_rfqs]
            sections.insert(0, {"icon": "📋", "title": "Мои КП — ждут решения покупателя",
                                "rows": rrows})

        # (б) Новые входящие RFQ (лиды рынка) — ответить ценой. Это то, что
        # раньше жило в «🔥 Срочных задачах»; объединяем сюда. Исключаем те,
        # по которым продавец уже дал КП (они выше, в «Мои КП»).
        from datetime import timedelta
        from django.utils import timezone as _tz
        from .rfq_mode_badge import mode_badge_with_sla
        two_weeks = _tz.now() - timedelta(days=14)
        # РЕЛЕВАНТНОСТЬ: показываем RFQ продавцу ТОЛЬКО если у него есть ≥1 позиция
        # из запроса — в каталоге по OEM ИЛИ как аналог (кросс-номер). Раньше слали
        # ВСЕМ (broadcast) → продавец видел RFQ, где у него нет ни одной позиции (спам).
        from marketplace.models import Part as _Part
        from .negotiation import _split_query_oem_name as _split_oem
        _candidates = list(
            RFQ.objects.filter(status__in=("new", "processing"),
                               created_at__gte=two_weeks)
                       .exclude(quotes__seller=user)
                       .prefetch_related("items__matched_part")
                       .order_by("-created_at")[:60]
        )
        _rfq_oems = {}
        _all_oems = set()
        for _r in _candidates:
            _s = set()
            for _it in _r.items.all():
                _oem = (_it.matched_part.oem_number if _it.matched_part_id
                        else (_split_oem(_it.query)[0] or "")) or ""
                _oem = _oem.strip()
                if _oem:
                    _s.add(_oem)
            _rfq_oems[_r.id] = _s
            _all_oems |= _s
        # Точный матч по OEM (индекс part_seller_oem_idx → быстро даже на каталоге
        # 900К+). Кросс-номера/аналоги для маршрутизации НЕ используем: icontains без
        # индекса = полный скан, на большом каталоге это дорого. Аналог продавец всё
        # равно предложит уже в форме котировки (для RFQ, куда его привёл OEM-матч).
        # БЕЗ is_active: позиция, что была в каталоге, но сейчас скрыта — тоже «его»
        # (по той же логике — продавец её знает и может вернуть/поставить).
        _present = set()
        if _all_oems:
            _present = set(
                _Part.objects.filter(seller=user, oem_number__in=list(_all_oems))
                .values_list("oem_number", flat=True)
            )
        lead_rfqs = [_r for _r in _candidates if _rfq_oems[_r.id] & _present][:12]
        if lead_rfqs:
            n_leads = len(lead_rfqs)
            lrows = []
            for r in lead_rfqs:
                badge = mode_badge_with_sla(r.mode)
                bp = f"{badge} · " if badge else ""
                lrows.append({
                    "title":    f"RFQ #{r.id} · Покупатель",
                    "subtitle": (f"{bp}{r.items.count()} "
                                 f"{_plural_ru(r.items.count(), 'позиция', 'позиции', 'позиций')} · "
                                 f"создан {r.created_at.strftime('%d.%m.%Y')}"),
                    "action":   {"label": "💬 Ответить", "action": "respond_rfq_form",
                                 "params": {"rfq_id": r.id}},
                })
            # Лиды — в самый верх: быстрее ответишь — выше шанс выиграть.
            sections.insert(0, {"icon": "📨", "title": "Новые RFQ — ответить ценой",
                                "rows": lrows})

    if not sections:
        filter_hint = {
            "action":  "Сейчас нет заказов, требующих вашего действия.",
            "transit": "Нет заказов в пути.",
            "done":    "Завершённых сделок пока нет.",
        }.get(flt, "У вас пока нет активных сделок.")
        return ActionResult(
            text=f"📋 Мои сделки — {filter_hint}",
            actions=([{"label": "🔄 Активные", "action": "get_my_deals", "params": {}}]
                     if flt != "active" else
                     [{"label": "✅ Завершённые", "action": "get_my_deals", "params": {"filter": "done"}}]),
        )

    # Заголовок-резюме
    if flt == "active":
        bits = []
        if n_leads:          bits.append(f"{n_leads} {_plural_ru(n_leads, 'новый лид', 'новых лида', 'новых лидов')}")
        if n_action:         bits.append(f"{n_action} {_plural_ru(n_action, 'требует', 'требуют', 'требуют')} действия")
        if n_breached_total: bits.append(f"{n_breached_total} с просрочкой SLA")
        summary = " · ".join(bits) if bits else "всё под контролем"
    else:
        label = {"action": "требуют действия", "transit": "в пути", "done": "завершённые"}.get(flt, flt)
        summary = label

    total = sum(len(s["rows"]) for s in sections)
    return ActionResult(
        text=f"📋 Мои сделки — {summary}.",
        cards=[{"type": "inbox", "data": {
            "title": "Мои сделки",
            "sections": sections,
        }}],
        actions=[
            {"label": "🔴 Требуют действия", "action": "get_my_deals", "params": {"filter": "action"}},
            {"label": "🚚 В пути",           "action": "get_my_deals", "params": {"filter": "transit"}},
            {"label": "✅ Завершённые",       "action": "get_my_deals", "params": {"filter": "done"}},
            {"label": "🔄 Активные",          "action": "get_my_deals", "params": {"filter": "active"}},
        ],
    )


def _buyer_deals(user, params):
    """«Мои сделки» для покупателя — секции по этапам (inbox), как у продавца:
    что оплатить/принять — наверху, дальше «в работе» и «в пути».
    Кнопка строки открывает карточку — там оплата/приёмка с подтверждением.
    """
    from collections import defaultdict
    from marketplace.models import RFQ, Order

    flt = (params or {}).get("filter", "active")
    if flt == "all":
        flt = "active"

    # bucket → (иконка, заголовок, кнопка, тип-действия, фаза)
    #   тип-действия: order=get_order_detail · track=track_order ·
    #                 quotes=view_rfq_quotes · rfq=get_rfq_status
    SECTIONS = [
        ("pay_reserve", "💳", "Оплатить резерв 10%",            "💳 Оплатить резерв →", "order",  "decide"),
        ("pay_final",   "💳", "Оплатить остаток 90%",           "💳 Оплатить остаток →", "order", "decide"),
        ("confirm",     "📦", "Доставлены — подтвердите приёмку", "✅ Принять заказ →",   "order",  "decide"),
        ("kp_ready",    "📋", "КП готовы — выбрать и оплатить",   "📋 Открыть КП →",      "quotes", "decide"),
        ("rfq_wait",    "⏳", "В подборе / у оператора",          "📦 Открыть",           "rfq",    "active"),
        ("production",  "⚙️", "В работе у поставщика",            "📦 Открыть",           "order",  "active"),
        ("transit",     "🚢", "В пути / на таможне",             "📍 Трекинг",           "track",  "active"),
        ("done",        "🏁", "Завершённые",                     "📦 Открыть",           "order",  "done"),
    ]
    DECIDE = {"pay_reserve", "pay_final", "confirm", "kp_ready"}
    _ACT = {"order": "get_order_detail", "track": "track_order",
            "quotes": "view_rfq_quotes", "rfq": "get_rfq_status"}

    allowed = None
    if flt == "action":
        allowed = DECIDE
    elif flt == "done":
        allowed = {"done"}
    elif flt == "transit":
        allowed = {"transit"}

    PAID = {"paid", "customs_paid", "final_paid", "mid_paid"}
    by_bucket = defaultdict(list)

    for o in Order.objects.filter(buyer=user).order_by("-created_at")[:120]:
        ps = o.payment_status or ""
        st = o.status or ""
        if st == "cancelled" or ps == "refunded":
            bucket = "done"
        elif st == "completed":
            bucket = "done"
        elif st == "delivered":
            bucket = "confirm"
        elif ps == "awaiting_reserve":
            bucket = "pay_reserve"
        elif st == "ready_to_ship" and ps not in PAID:
            bucket = "pay_final"
        elif st in ("reserve_paid", "confirmed", "in_production"):
            bucket = "production"
        elif st in ("transit_abroad", "shipped_abroad", "shipped", "customs",
                    "transit_rf", "issuing", "ready_to_ship"):
            bucket = "transit"
        else:
            bucket = "production"
        by_bucket[bucket].append({
            "kind": "order", "id": o.id,
            "title": f"Заказ #{o.id}",
            "subtitle": (f"{o.created_at.strftime('%d.%m.%Y') if o.created_at else ''} · "
                         f"{o.get_status_display()} · ${float(o.total_amount or 0):,.0f}"),
        })

    rfq_qs = (RFQ.objects.filter(created_by=user) if hasattr(RFQ, "created_by")
              else RFQ.objects.filter(customer_email=user.email))
    for r in rfq_qs.exclude(status__in=("closed", "cancelled", "declined")).order_by("-created_at")[:50]:
        bucket = "kp_ready" if r.status in ("quoted", "accepted") else "rfq_wait"
        n_pos = r.items.count() if hasattr(r, "items") else 0
        by_bucket[bucket].append({
            "kind": "rfq", "id": r.id,
            "title": f"RFQ #{r.id}",
            "subtitle": (f"{r.created_at.strftime('%d.%m.%Y') if r.created_at else ''} · "
                         f"{n_pos} {_plural_ru(n_pos, 'позиция', 'позиции', 'позиций')}"),
        })

    sections = []
    n_action = 0
    for bucket, icon, title, btn_label, act_type, phase in SECTIONS:
        if allowed is not None and bucket not in allowed:
            continue
        if allowed is None and bucket == "done":
            continue  # «активные» по умолчанию — без архива
        rws = by_bucket.get(bucket) or []
        if not rws:
            continue
        if bucket in DECIDE:
            n_action += len(rws)
        rows = []
        for rw in rws[:12]:
            p = {"order_id": rw["id"]} if rw["kind"] == "order" else {"rfq_id": rw["id"]}
            rows.append({
                "title": rw["title"], "subtitle": rw["subtitle"],
                "action": {"label": btn_label, "action": _ACT[act_type], "params": p},
            })
        if len(rws) > 12:
            rows.append({"title": f"… ещё {len(rws) - 12}", "subtitle": "—"})
        sections.append({"icon": icon, "title": title, "rows": rows})

    if not sections:
        hint = {
            "action":  "Сейчас нет сделок, требующих оплаты или приёмки.",
            "transit": "Нет заказов в пути.",
            "done":    "Завершённых сделок пока нет.",
        }.get(flt, "У вас пока нет активных сделок.")
        return ActionResult(
            text=f"📋 Мои сделки — {hint}",
            actions=[{"label": "🛒 Найти запчасть", "action": "search_parts", "params": {}},
                     {"label": "📝 Создать запрос", "action": "create_rfq", "params": {}}],
        )

    if flt == "active":
        summary = f"{n_action} {_plural_ru(n_action, 'требует', 'требуют', 'требуют')} действия" if n_action else "всё под контролем"
    else:
        summary = {"action": "требуют действия", "transit": "в пути", "done": "завершённые"}.get(flt, flt)

    return ActionResult(
        text=f"📋 Мои сделки — {summary}.",
        cards=[{"type": "inbox", "data": {"title": "Мои сделки", "sections": sections}}],
        actions=[
            {"label": "🔴 Требуют действия", "action": "get_my_deals", "params": {"filter": "action"}},
            {"label": "🚢 В пути",           "action": "get_my_deals", "params": {"filter": "transit"}},
            {"label": "✅ Завершённые",       "action": "get_my_deals", "params": {"filter": "done"}},
            {"label": "🔄 Активные",          "action": "get_my_deals", "params": {"filter": "active"}},
        ],
    )


@register("get_my_deals")
def get_my_deals(params, user, role):
    """Единая лента «Мои сделки» — RFQ + Order одним списком.

    Продавцу и покупателю — чистый inbox по этапам (_seller_deals/_buyer_deals);
    оператор/остальные — старый сводный список.
    """
    # Продавец: отдельный рендер по этапам (его «основной хлеб»).
    if role == "seller":
        return _seller_deals(user, params)
    # Покупатель: такой же чистый inbox по этапам.
    if role == "buyer":
        return _buyer_deals(user, params)

    from marketplace.models import RFQ, Order
    flt = (params or {}).get("filter", "all")  # all|decide|active|done
    rows = []

    # ── RFQ-фаза ───────────────────────────────────────────────
    rfq_qs = RFQ.objects.order_by("-created_at")
    if role == "buyer":
        rfq_qs = rfq_qs.filter(created_by=user) if hasattr(RFQ, "created_by") \
                 else rfq_qs.filter(customer_email=user.email)
    elif role == "seller":
        # Продавец видит в «Моих сделках» только те RFQ, по которым он сам
        # давал котировку — чужие запросы покупателей сюда не попадают
        # (это лиды, они живут в «🔥 Срочном» / inbox).
        rfq_qs = rfq_qs.filter(quotes__seller=user).distinct()
    rfq_qs = rfq_qs.exclude(status__in=("closed",))[:50]

    # Очищаем RFQ.notes от технического хвоста ("Mode: manual · ...")
    import re as _re_n
    def _clean_rfq_title(notes: str, fallback: str) -> str:
        if not notes:
            return fallback
        # Удаляем technical-prefixes
        s = notes
        s = _re_n.sub(r"Создано из чата\s*·?\s*\d+\s*позиц[а-я]*", "", s)
        s = _re_n.sub(r"\|\s*Mode:[^|]*", "", s)
        s = _re_n.sub(r"\|\s*(MANUAL|AUTO|SEMI)[^|]*", "", s)
        s = _re_n.sub(r"Запрос:\s*", "", s)
        s = s.strip(" ·|")
        return s[:80] or fallback

    for r in rfq_qs:
        is_cancelled = r.status in ("cancelled", "declined")
        is_quoted = r.status in ("quoted", "accepted")
        if role == "seller":
            # Перспектива продавца: это его котировки в работе, а не «оплатить резерв».
            if is_cancelled:
                phase, phase_label, action_label = "done", "Отклонён покупателем", "Открыть"
            elif r.status == "accepted":
                phase, phase_label, action_label = "active", "КП принят · оформляется заказ", "Открыть"
            elif r.status == "quoted":
                phase, phase_label, action_label = "active", "КП отправлен · ждёт покупателя", "Открыть"
            else:
                phase, phase_label, action_label = "active", "В переговорах", "Открыть"
        elif is_cancelled:
            phase, phase_label, action_label = "done", "Отменён", "Открыть"
        elif r.mode == "semi" and r.status in ("new", "processing", "matched"):
            phase, phase_label, action_label = "decide", "Ждёт оператора", "Открыть"
        elif is_quoted:
            phase, phase_label, action_label = "decide", "КП готов · оплатить резерв", "Оплатить 10%"
        else:
            phase, phase_label, action_label = "decide", "Подбор позиций", "Открыть"
        if flt != "all" and flt != phase:
            continue
        items_count = r.items.count() if hasattr(r, "items") else 0
        rows.append({
            "id":           r.id,
            "number":       f"RFQ-{r.id}",
            "phase":        phase,
            "title":        _clean_rfq_title(r.notes, f"RFQ #{r.id}"),
            "stage":        phase_label,
            # amber = нужно действие, good = всё ок и работает, wait = архив
            "status":       "amber" if phase == "decide" else ("good" if phase == "active" else "wait"),
            "status_label": phase_label,
            "action_label": action_label,
            "items_count":  items_count,
            "quotes_count": 0,
            "kind":         "rfq",
            # Имя клиента нужно только seller/operator (buyer видит «себя»).
            "customer_name": ((r.customer_name if hasattr(r, "customer_name") else "") or "") if role not in ("buyer", "seller") else "",
            "date_str":     r.created_at.strftime("%d.%m.%Y") if r.created_at else "",
            "amount":       float(getattr(r, "estimated_total", 0) or 0) or None,
            "_sort":        -int(r.created_at.timestamp() if r.created_at else 0),
        })

    # ── Order-фаза ──────────────────────────────────────────────
    order_qs = Order.objects.order_by("-created_at")
    if role == "buyer":
        order_qs = order_qs.filter(buyer=user)
    elif role == "seller":
        # FIX: subquery вместо materialized list — см. get_orders выше.
        order_qs = order_qs.filter(items__part__seller=user).distinct()
    order_qs = order_qs[:50]

    from django.utils import timezone as _tz
    now = _tz.now()
    for o in order_qs:
        ps = o.payment_status or ""
        st = o.status or ""
        # Фаза
        if st in ("cancelled",) or ps == "refunded":
            phase, phase_label, action_label = "done", "Отменён", "Открыть"
        elif st in ("delivered", "completed"):
            phase, phase_label, action_label = "done", "Доставлен", "Открыть"
        elif ps == "awaiting_reserve":
            age = (now - o.created_at).days if o.created_at else 0
            if role == "seller":
                # Продавец резерв не платит — он ждёт оплату от покупателя.
                phase = "active"
                phase_label = f"Ждёт оплаты резерва покупателем · {age}д" if age >= 3 \
                              else "Ждёт оплаты резерва покупателем"
                action_label = "Открыть"
            else:
                phase = "decide"
                phase_label = f"Оплатить резерв · {age}д висит" if age >= 3 else "Оплатить резерв"
                action_label = f"Оплатить ${float(o.reserve_amount or 0):,.0f}"
        else:
            # reserve_paid / mid_paid / paid + in_production / shipped / customs / transit
            phase = "active"
            phase_label = o.get_status_display() if hasattr(o, "get_status_display") else st
            action_label = "Открыть"
        if flt != "all" and flt != phase:
            continue
        rows.append({
            "id":           o.id,
            "number":       f"ORD-{o.id}",
            "phase":        phase,
            "title":        f"Заказ · ${float(o.total_amount or 0):,.0f}",
            "stage":        phase_label,
            "status":       "amber" if phase == "decide" else ("good" if phase == "active" else "wait"),
            "status_label": phase_label,
            "action_label": action_label,
            "items_count":  o.items.count() if hasattr(o, "items") else 0,
            "quotes_count": 0,
            "kind":         "order_pending" if ps == "awaiting_reserve" else "order",
            "customer_name": (o.customer_name or "") if role not in ("buyer", "seller") else "",
            "date_str":     o.created_at.strftime("%d.%m.%Y") if o.created_at else "",
            "amount":       float(o.total_amount or 0),
            "_sort":        -int(o.created_at.timestamp() if o.created_at else 0),
        })

    # Сортируем: сначала decide, потом active, потом done; внутри по дате
    PHASE_PRI = {"decide": 0, "active": 1, "done": 2}
    rows.sort(key=lambda r: (PHASE_PRI.get(r["phase"], 9), r["_sort"]))
    for r in rows:
        r.pop("_sort", None)

    decide_rows = [r for r in rows if r["phase"] == "decide"]
    active_rows = [r for r in rows if r["phase"] == "active"]
    done_rows   = [r for r in rows if r["phase"] == "done"]

    # Группируем по стадиям в отдельные карточки (каждая со своим title)
    cards = []
    if decide_rows:
        cards.append({"type": "rfq_list", "data": {
            "title": f"Ждут моего решения · {len(decide_rows)}",
            "rows":  decide_rows,
        }})
    if active_rows:
        cards.append({"type": "rfq_list", "data": {
            "title": f"В работе · {len(active_rows)}",
            "rows":  active_rows,
        }})
    if done_rows:
        # Завершённые показываем максимум 10 — не утопаем в архиве
        cards.append({"type": "rfq_list", "data": {
            "title": f"Завершены · {len(done_rows)}",
            "rows":  done_rows[:10],
        }})
    if not cards:
        cards.append({"type": "rfq_list", "data": {
            "title": "Мои сделки",
            "rows":  [],
        }})

    if flt == "all":
        hint_bits = []
        if decide_rows: hint_bits.append(f"{len(decide_rows)} ждут решения")
        if active_rows: hint_bits.append(f"в работе: {len(active_rows)}")
        if done_rows:   hint_bits.append(f"завершены: {len(done_rows)}")
        hint = " · ".join(hint_bits) if hint_bits else "Пока нет сделок"
    else:
        hint = f"Показано: {len(rows)} · фильтр «{flt}»"

    return ActionResult(
        text=hint,
        cards=cards,
        actions=[
            {"label": "Ждут решения",   "action": "get_my_deals", "params": {"filter": "decide"}},
            {"label": "В работе",        "action": "get_my_deals", "params": {"filter": "active"}},
            {"label": "Завершены",       "action": "get_my_deals", "params": {"filter": "done"}},
            {"label": "Все",             "action": "get_my_deals", "params": {"filter": "all"}},
        ],
        suggestions=["Создать RFQ", "Найти запчасть"],
    )


@register("cancel_rfq")
def cancel_rfq(params, user, role):
    """Отменить (мягко удалить) RFQ покупателя.
    Меняет статус на 'cancelled' → запрос пропадает из списка «Мои RFQ».
    Если уже есть котировки — статус 'declined' и предупреждение, чтобы
    оператор знал что юзер передумал.
    """
    from marketplace.models import RFQ
    try:
        rfq_id = int(params.get("rfq_id") or 0)
    except (ValueError, TypeError):
        return ActionResult(text="⚠️ Неверный rfq_id.")
    if not rfq_id:
        return ActionResult(text="⚠️ Не указан rfq_id.")
    try:
        rfq = RFQ.objects.get(id=rfq_id, created_by=user)
    except RFQ.DoesNotExist:
        return ActionResult(text=f"⚠️ RFQ #{rfq_id} не найден или нет прав.")
    if rfq.status in ("cancelled", "closed"):
        return ActionResult(text=f"RFQ #{rfq_id} уже закрыт.")
    had_quotes = rfq.quotes.exists() if hasattr(rfq, "quotes") else False
    new_status = "cancelled"
    rfq.status = new_status
    rfq.save(update_fields=["status"])
    msg = f"✓ RFQ #{rfq_id} удалён из списка."
    if had_quotes:
        msg += " ⚠️ По нему были котировки — оператор получит уведомление."
    # Возвращаем обновлённый список RFQ (rfq_list card) сразу после удаления
    refreshed = get_rfq_status({}, user, role)
    refreshed.text = msg
    return refreshed


@register("get_rfq_status")
def get_rfq_status(params, user, role):
    from marketplace.models import RFQ
    rfq_id = params.get("rfq_id")
    if rfq_id:
        try:
            rfq = RFQ.objects.get(id=rfq_id)
        except RFQ.DoesNotExist:
            return ActionResult(text=f"⚠️ RFQ #{rfq_id} не найден")
        # AuthZ: только создатель, операторы и админы могут видеть конкретный RFQ.
        # Бага CRITICAL: раньше любой buyer мог открыть чужой RFQ по ID.
        is_op_or_admin = bool(role and (role.startswith("operator") or role == "admin"))
        if not is_op_or_admin:
            if _is_anon(user):
                # Аноним может смотреть ТОЛЬКО анонимный RFQ (created_by=None,
                # созданный в гостевом флоу). Чужой RFQ зарегистрированного
                # пользователя по угаданному id — закрыто (IDOR).
                if rfq.created_by_id is not None:
                    return ActionResult(text=f"⚠️ RFQ #{rfq_id} не найден")
            elif rfq.created_by_id and rfq.created_by_id != user.id:
                return ActionResult(text=f"⚠️ RFQ #{rfq_id} не найден")
        # ── RFQ инлайн — переиспользуем готовую spec_results карточку ─────
        # Это та же визуалка что у analyze_spec / upload_parts_list:
        # KPI «Found / Analogue / Not found» + полная таблица позиций со
        # статусом, OEM, name, brand, price, qty, weight, supplier rating.
        # Не плодим свой rfq-card — данные те же.
        from marketplace.models import Quote as _Quote
        from marketplace.fx import to_usd_float  # живой биржевой курс
        items_qs = list(rfq.items.select_related("matched_part__brand").all()) if hasattr(rfq, "items") else []
        spec_items = []
        found_n = 0
        not_found_n = 0
        total_usd = 0.0
        for it in items_qs:
            mp = getattr(it, "matched_part", None)
            qty = it.quantity or 1
            if mp and mp.price is not None:
                ccy = (mp.currency or "USD").upper()
                price_usd = to_usd_float(mp.price, ccy) or 0.0
                total_usd += price_usd * qty
                is_insider = bool(role and (role.startswith("operator") or role == "admin"))
                # Тянем реальный статус/рейтинг продавца из профиля
                _STATUS_BADGE = {"trusted": "Надёжный", "sandbox": "Песочница",
                                 "risky": "Рисковый", "rejected": "Исключён"}
                sup_status_code = "trusted"
                sup_rating = 91.6
                sup_username = ""
                if mp.seller_id:
                    try:
                        from marketplace.models import UserProfile as _UP
                        _p = _UP.objects.filter(user_id=mp.seller_id).only(
                            "supplier_status", "rating", "user").first()
                        if _p:
                            sup_status_code = _p.supplier_status or "trusted"
                            sup_rating = float(_p.rating or 90.0)
                            try:
                                sup_username = _p.user.username
                            except Exception:
                                pass
                    except Exception:
                        pass
                sup_status_label = _STATUS_BADGE.get(sup_status_code, "Надёжный")
                if is_insider:
                    # Оператор видит реальное имя + статус + рейтинг
                    badge_text = (f"{sup_status_label} · {sup_username}"
                                   if sup_username else sup_status_label)
                else:
                    # Покупатель видит только статус + анонимный код
                    badge_text = sup_status_label
                spec_items.append({
                    "status": "in_stock",
                    "id": mp.oem_number or "—",
                    "name": (_clean_title(mp.title or "")[:80]) or "—",
                    "brand": (mp.brand.name if mp.brand else "—"),
                    "condition": (mp.condition or "oem"),
                    # Покупатель ВСЕГДА видит USD (бирж. курс); оператор — исходную валюту продавца.
                    "price": (float(mp.price) if is_insider else price_usd),
                    "qty": qty,
                    "weight": f"{mp.gross_weight_kg} кг" if mp.gross_weight_kg else "—",
                    "currency": (ccy if is_insider else "USD"),
                    "supplier_status": sup_status_code,           # CSS-класс
                    "supplier_status_badge": badge_text,           # лейбл
                    "supplier_rating": round(sup_rating, 1),
                    "alt_offers": 0,
                })
                found_n += 1
            else:
                spec_items.append({
                    "status": "not_found",
                    "id": getattr(it, "query", "") or "—",
                    "name": "",
                    "qty": qty,
                })
                not_found_n += 1
        # Кнопки внизу — действия зависят от статуса RFQ
        quotes_count = _Quote.objects.filter(rfq=rfq).count()
        rfq_actions = []
        # Поиск связанного Order (если RFQ дошёл до оплаты) — через customer_email
        # (Order.notes поля нет; жёсткой FK rfq→order тоже нет)
        from marketplace.models import Order as _Ord
        linked_order = None
        try:
            if rfq.created_by_id:
                linked_order = (_Ord.objects.filter(buyer_id=rfq.created_by_id)
                                  .exclude(status__in=("cancelled",))
                                  .order_by("-id").first())
        except Exception:
            linked_order = None

        if rfq.status in ("cancelled", "declined", "closed"):
            # Отменённый RFQ — повтор + связи
            rfq_actions.append({
                "label": "Создать новый RFQ",
                "action": "create_rfq", "params": {},
            })
            rfq_actions.append({
                "label": "Найти аналоги",
                "action": "search_parts",
                "params": {"query": (rfq.notes or "")[:80]},
            })
            rfq_actions.append({
                "label": "История изменений",
                "action": "audit_log",
                "params": {"rfq_id": rfq.id},
            })
            rfq_actions.append({
                "label": "Связаться с оператором",
                "action": "ask_about_rfq", "params": {"rfq_id": rfq.id},
            })
            rfq_actions.append({
                "label": "Все мои сделки",
                "action": "get_my_deals", "params": {},
            })
        else:
            # Активный RFQ — стандартный набор + расширенная навигация
            if quotes_count > 0:
                rfq_actions.append({
                    "label": f"Сравнить котировки ({quotes_count})",
                    "action": "compare_quotes", "params": {"rfq_id": rfq.id},
                })
                rfq_actions.append({
                    "label": "Оплатить резерв 10%",
                    "action": "auto_accept_and_pay_reserve",
                    "params": {"rfq_id": rfq.id},
                })
                rfq_actions.append({
                    "label": "КП в PDF",
                    "action": "generate_proposal", "params": {"rfq_id": rfq.id},
                })
            else:
                rfq_actions.append({
                    "label": "Спросить оператора",
                    "action": "ask_about_rfq", "params": {"rfq_id": rfq.id},
                })
            # ── Логистика по RFQ — расчёт фрахта/портов/ETA ──
            rfq_actions.append({
                "label": "Логистика и доставка",
                "action": "shipping_choose", "params": {"rfq_id": rfq.id},
            })
            # ── SLA / прогресс / документы — общие навигационные опции ──
            rfq_actions.append({
                "label": "История изменений",
                "action": "audit_log", "params": {"rfq_id": rfq.id},
            })
            if linked_order:
                rfq_actions.append({
                    "label": "Трекинг отгрузки",
                    "action": "track_shipment", "params": {"order_id": linked_order.id},
                })
                rfq_actions.append({
                    "label": "Документы заказа",
                    "action": "list_order_documents", "params": {"order_id": linked_order.id},
                })
            rfq_actions.append({
                "label": "Отменить запрос",
                "action": "cancel_rfq", "params": {"rfq_id": rfq.id},
            })
        urgency = getattr(rfq, "urgency", "standard") or "standard"
        URGENCY_LABEL = {"critical": "СРОЧНО", "urgent": "ВАЖНО", "standard": ""}
        title_bits = [f"RFQ #{rfq.id}"]
        if URGENCY_LABEL.get(urgency):
            title_bits.append(f"· {URGENCY_LABEL[urgency]}")
        if (rfq.mode or "").upper():
            title_bits.append(f"· {rfq.mode.upper()}")
        title = " ".join(title_bits)
        foot = f"{found_n} из {len(items_qs)} priced · {quotes_count} котир."
        if quotes_count == 0 and len(items_qs) > 0:
            foot += " · ждём ответы поставщиков"

        # ── Что дальше: подсказка по статусу RFQ ──
        rfq_hint = ""
        rfq_suggestions = []
        if rfq.status in ("cancelled", "declined"):
            rfq_hint = "Запрос отменён. Если нужно повторить — создайте новый RFQ или обратитесь к оператору."
            rfq_suggestions = ["Создать RFQ", "Связаться с оператором", "Все мои сделки"]
        elif rfq.status == "closed":
            rfq_hint = "Запрос закрыт. Можно использовать как шаблон для повтора."
            rfq_suggestions = ["Создать RFQ", "Все мои сделки"]
        elif quotes_count > 0:
            rfq_hint = (
                f"📊 {quotes_count} котировок получено. Дальше: выберите поставщика, "
                f"оформите заказ и оплатите резерв 10%."
            )
            rfq_suggestions = ["Сравнить котировки", "Лучшая котировка", "Спросить оператора"]
        elif rfq.mode == "semi":
            rfq_hint = (
                "⏳ Оператор подтверждает аналоги (SLA 15 мин). Дальше: после "
                "подтверждения вы получите КП и сможете оплатить резерв 10%."
            )
            rfq_suggestions = ["Спросить оператора", "Все мои сделки"]
        elif found_n == 0:
            rfq_hint = (
                "Ни одна позиция не найдена в каталоге. Оператор предложит аналоги "
                "или сообщит об отсутствии. Запрос записан в аналитику спроса."
            )
            rfq_suggestions = ["Предложить аналог", "Связаться с оператором"]
        else:
            rfq_hint = (
                f"✓ {found_n} позиций сматчены в каталоге. Дальше: оператор подтвердит "
                f"и сформирует КП для оплаты резерва 10%."
            )
            rfq_suggestions = ["Спросить оператора", "Создать ещё RFQ"]

        # Оператор/админ — это сам оператор: «Спросить оператора» и «Оплатить резерв»
        # (это действия покупателя) для него бессмысленны. Убираем buyer-only пункты.
        if role in ("operator", "admin"):
            _op_drop = ("ask_about_rfq", "contact_operator", "auto_accept_and_pay_reserve")
            rfq_actions = [a for a in rfq_actions if a.get("action") not in _op_drop]
            rfq_suggestions = [s for s in rfq_suggestions if "оператор" not in s.lower()]

        return ActionResult(
            text=f"RFQ #{rfq.id} — {rfq.get_status_display() if hasattr(rfq,'get_status_display') else rfq.status}\n\n{rfq_hint}",
            cards=[{
                "type": "spec_results",
                "data": {
                    "title": title,
                    "found": found_n,
                    "analogue": 0,
                    "not_found": not_found_n,
                    "items": spec_items,
                    "more_count": 0,
                    "offers_count": found_n,
                    "sellers_count": found_n,
                    "best_mix": int(total_usd) if total_usd else None,
                    "total": int(total_usd) if total_usd else None,
                    "currency": "USD",
                    "foot_info": foot,
                },
            }],
            actions=rfq_actions,
            suggestions=rfq_suggestions,
        )
    # List active RFQs — компактные строки (rfq_list card).
    # Раньше возвращали N полных карточек на каждый RFQ — занимало пол-экрана,
    # 0/0/— на каждой бесполезно. Теперь одна таблица-список: id · название ·
    # статус · позиций · котировок. Клик по строке → разворачивает в полный
    # spec_results той же ручкой get_rfq_status(rfq_id).
    # Аноним: «списка моих RFQ» нет — RFQ создаются с created_by=None и общим
    # placeholder-email, разделить запросы разных гостей нельзя. Ведём на
    # регистрацию (после неё анонимные RFQ привяжутся к аккаунту). Без этого
    # qs.filter(created_by=AnonymousUser) падает «expected a number».
    if _is_anon(user):
        return _anon_register_result()
    from marketplace.models import Quote as _Quote2
    # Скрываем терминальные статусы — юзер не должен утопать в архиве.
    qs = RFQ.objects.exclude(status__in=("cancelled", "closed", "declined"))\
                     .order_by("-created_at").prefetch_related("items", "quotes")
    if role == "buyer":
        qs = qs.filter(created_by=user) if hasattr(RFQ, "created_by") else qs.filter(customer_email=user.email)
    rfqs = list(qs[:30])
    # Pending Orders (без оплаты резерва) больше не мерджим в этот список —
    # они путают: «Мои RFQ» = только реальные запросы котировок. Неоплаченные
    # заказы видны в «Мои заказы» / track_order.
    pending_orders = []

    # ── Action-oriented статусы (что юзеру делать) ────────────────
    # Не показываем сырые «new / matched / declined» — переводим в:
    #  • «Выбрать поставщика» (есть котировки → нужно решение)
    #  • «Ждём ответы» (разослано, ответов нет)
    #  • «Новый» (только создан, ещё разосланётся)
    # Группа определяет визуальный приоритет и цвет бейджа.
    ACTION_STATUS = {
        # status → (group, label, ui_status_class)
        "matched":      ("decide", "Выбрать поставщика",  "decide"),
        "quoted":       ("decide", "Сравнить котировки",  "decide"),
        "needs_review": ("review", "Нужна правка",        "warn"),
        "processing":   ("wait",   "Ждём ответы",         "wait"),
        "new":          ("new",    "Новый — рассылается", "new"),
    }
    rows = []
    for r in rfqs:
        items_count = r.items.count()
        quotes_count = _Quote2.objects.filter(rfq=r).count()
        urgency = getattr(r, "urgency", "standard") or "standard"
        URGENCY_LABEL = {"critical": "СРОЧНО", "urgent": "ВАЖНО", "standard": ""}
        # Если уже есть котировки — статус всегда «Выбрать поставщика»,
        # независимо от поля RFQ.status (приоритет действия).
        if quotes_count > 0:
            group, action_label, ui_cls = ACTION_STATUS["matched"]
        else:
            group, action_label, ui_cls = ACTION_STATUS.get(r.status, ("wait", "В работе", "wait"))
        # Описание текущего этапа («что сейчас происходит с запросом»)
        if quotes_count > 0:
            stage = f"📊 {quotes_count} котировок получено · нужно выбрать"
        elif r.status == "new":
            stage = "📤 Рассылается поставщикам"
        elif r.status == "processing":
            stage = "⏳ Ждём ответы от поставщиков"
        elif r.status == "needs_review":
            stage = "✏️ Оператор уточняет позиции"
        else:
            stage = f"Статус: {r.status}"
        # Сортировочный приоритет: 0 = надо что-то делать (decide), 1 = review,
        # 2 = ждём, 3 = новый. Внутри группы — по дате.
        group_order = {"decide": 0, "review": 1, "wait": 2, "new": 3}.get(group, 4)
        rows.append({
            "id": r.id,
            "number": f"RFQ-{r.id}",
            "title": ((r.notes or "")[:80] or f"RFQ #{r.id}"),
            "status": ui_cls,            # CSS-класс
            "status_label": action_label,  # «Что делать»
            "stage": stage,                # «Что сейчас происходит»
            "group": group,
            "_sort_key": (group_order, -int(r.created_at.timestamp())),
            "urgency": urgency,
            "urgency_label": URGENCY_LABEL.get(urgency, ""),
            "items_count": items_count,
            "quotes_count": quotes_count,
            "created_at": r.created_at.strftime("%d.%m.%Y"),
        })
    # Сортируем: «решения сверху», далее по свежести
    rows.sort(key=lambda r: r["_sort_key"])
    for r in rows:
        r.pop("_sort_key", None)
        r.pop("group", None)

    decide_rfq = sum(1 for r in rows if r["status"] == "decide")
    if decide_rfq:
        hint = f"📊 {decide_rfq} RFQ ждут вашего решения · остальные в работе"
    else:
        hint = "Все в работе — ждём ответы поставщиков."

    # Если RFQ нет — проверяем активные заказы (включая pending), чтобы
    # дать юзеру указатель куда смотреть. Юзер часто путает «не оплачено» = RFQ.
    if not rows:
        from marketplace.models import Order
        pending_orders = 0
        active_orders = 0
        if role == "buyer":
            buyer_orders = Order.objects.filter(buyer=user)
            pending_orders = buyer_orders.filter(payment_status="awaiting_reserve").count()
            active_orders = buyer_orders.exclude(
                status__in=("delivered", "completed", "cancelled")
            ).count()
        empty_text = (
            "У вас нет открытых RFQ.\n\n"
            "💡 RFQ — это «жду котировок от поставщиков» (создаётся когда "
            "цены ещё нет: запрос на ненайденные позиции, торг)."
        )
        empty_actions = []
        if pending_orders:
            empty_text += (
                f"\n\n📦 Зато есть {pending_orders} "
                f"{'заказ' if pending_orders == 1 else ('заказа' if pending_orders < 5 else 'заказов')} "
                f"без оплаты резерва — смотрите в «Мои заказы», нужно подтвердить."
            )
            empty_actions.append({
                "label": f"📦 Открыть заказы без оплаты ({pending_orders})",
                "action": "get_orders",
                "params": {"status": "awaiting_reserve"},
                "style": "primary",
            })
        elif active_orders:
            empty_text += f"\n\n📦 У вас {active_orders} активных заказов в работе."
            empty_actions.append({
                "label": f"📦 Открыть заказы ({active_orders})",
                "action": "get_orders",
                "params": {},
            })
        return ActionResult(text=empty_text, actions=empty_actions)

    return ActionResult(
        text=hint,
        cards=[{
            "type": "rfq_list",
            "data": {
                "title": "Мои RFQ · жду котировок от поставщиков",
                "rows": rows,
            },
        }],
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

    # Группировка по статусу — отдельный list-card на каждый этап.
    # Порядок этапов = воронка: Готов к отгрузке → Транзит за рубеж →
    # Таможня → Транзит РФ → Выдача.
    _STATUS_ORDER = ["ready_to_ship", "transit_abroad", "customs", "transit_rf", "issuing"]
    status_cards = []
    for st in _STATUS_ORDER:
        orders_in_st = by_status.get(st, [])
        if not orders_in_st:
            continue
        st_rows = []
        for o in orders_in_st[:10]:  # лимит 10 на блок
            days_left = status_eta.get(o.status, 0)
            eta = (now + timedelta(days=days_left)).strftime("%d.%m")
            tone = "bad" if o.sla_status == "breached" else (
                "warn" if o.sla_status == "at_risk" else "ok")
            items_n = OrderItem.objects.filter(order=o).count()
            st_rows.append({
                "title": f"ORD-{o.id} · {'Покупатель' if role == 'seller' else (o.customer_name or o.buyer.username)}",
                "subtitle": (
                    f"{items_n} поз · ${float(o.total_amount or 0):,.0f} · "
                    f"ETA ~{eta} ({days_left}д)"
                ),
                "tone": tone,
                "action": "get_order_detail",
                "params": {"order_id": o.id},
            })
        # Бейдж этапа: подсветка SLA-проблем
        breached_in_st = sum(1 for o in orders_in_st if o.sla_status == "breached")
        n_total = len(orders_in_st)
        st_label = status_label.get(st, st)
        if breached_in_st:
            block_title = f"{st_label} · {n_total} · 🔴 {breached_in_st} срыв SLA"
        else:
            block_title = f"{st_label} · {n_total}"
        more_note = f"  (+ ещё {n_total - 10} заказов)" if n_total > 10 else ""
        status_cards.append({
            "type": "list",
            "data": {
                "title": block_title + more_note,
                "items": st_rows,
                # Свёрнуто по умолчанию для этапов где >5 заказов (чтобы не залипал scroll)
                "collapsible": n_total > 5,
                "collapsed": n_total > 5,
            },
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
                    {"label": "Доля заказов в срок", "value": f"{sla_healthy_pct}%",
                     "tone": "ok" if sla_healthy_pct >= 80 else ("warn" if sla_healthy_pct >= 60 else "bad")},
                    {"label": "Средн. возраст",  "value": f"{avg_age_days:.0f} дн",
                     "sub": "в текущем этапе",
                     "tone": "warn" if avg_age_days > 14 else "info"},
                    {"label": "Самый медленный этап",     "value": (status_label.get(biggest[0], biggest[0]).split(" ", 1)[-1] if biggest else "—"),
                     "sub": f"{biggest_share}% объёма" if biggest else "",
                     "tone": "warn" if biggest_share > 50 else "info"},
                ],
            }},
            {"type": "bar_chart", "data": {
                "title": "📊 Распределение по этапам",
                "items": chart_items,
                "color": "#FFB74D",
            }},
            *status_cards,
        ],
        contextual_actions=[
            {"action": "get_orders",     "label": "📦 Все заказы"},
            {"action": "get_analytics",  "label": "📊 Общая аналитика"},
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


# Регексп для CJK-иероглифов (CN/JP/KR) — мешают читать названия типа
# «Pipe子», «PC200普通斗0.9m³带吊钩». В нашем UI язык RU/EN, поэтому стрипаем.
_CJK_RE = re.compile(r"[　-〿぀-ヿ㐀-䶿一-鿿豈-﫿＀-￯]+")


def _clean_title(title: str) -> str:
    """Чистит название запчасти от CJK-иероглифов и схлопывает пробелы.
    Pipe子 → Pipe, PC200普通斗0.9m³带吊钩 → PC200 0.9m³.
    Если после чистки осталось <3 латинских/кириллических символа — возвращает пустую строку
    (фронт покажет «—» или fallback на OEM-номер).
    """
    if not title:
        return ""
    cleaned = _CJK_RE.sub(" ", title)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .,;:-—")
    if not cleaned:
        return ""
    # Если все читаемые символы — спецсимволы/цифры, не оставляем
    if not any(ch.isalpha() for ch in cleaned):
        return ""
    return cleaned


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
    from marketplace.fx import to_usd_float  # покупатель ВСЕГДА видит USD по бирж. курсу
    for p in parts:
        key = ((p.oem_number or "").upper(), p.seller_id)
        price = to_usd_float(p.price, getattr(p, "currency", "USD"))
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
            "currency": "USD",
            "price_fob_sea": to_usd_float(p.price_fob_sea, getattr(p, "currency", "USD")),
            "price_fob_air": to_usd_float(p.price_fob_air, getattr(p, "currency", "USD")),
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
    lines = [f"🚚 Расчёт доставки {p.oem_number} ({p.title[:40]}) → {dest}"]
    for m, r in results.items():
        m_label = "🚢 Море" if m == "sea" else "✈️ Авиа"
        if r["cost"] is None:
            lines.append(f"{m_label}: — ({err_map.get(r['error'], r['error'])})")
        else:
            lines.append(
                f"{m_label}: ${r['cost']} · "
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

    # Если отчёт открыт из карточки конкретного заказа — сужаем область.
    _single_oid = (params or {}).get("order_id")
    if _single_oid:
        qs = qs.filter(id=_single_oid)
        scope_label = f"по заказу ORD-{_single_oid}"

    # Реальный пересчёт SLA на лету: дни в текущей стадии vs норматив.
    # Поле Order.sla_status не доверяем — оно ниоткуда автоматически
    # не обновляется и часто разъезжается с фактом.
    _STAGE_SLA_DAYS_KPI = {
        "awaiting_reserve": 2, "reserve_paid": 2, "confirmed": 1,
        "in_production": 7, "ready_to_ship": 2, "transit_abroad": 14,
        "customs": 5, "transit_rf": 7, "issuing": 3,
    }
    from marketplace.models import OrderEvent as _OE
    _active_for_kpi = list(qs.exclude(status__in=("completed", "cancelled")))
    _last_change_kpi = {}
    if _active_for_kpi:
        _evs = _OE.objects.filter(
            order_id__in=[o.id for o in _active_for_kpi],
            event_type="status_changed",
        ).order_by("created_at")
        for _e in _evs:
            _last_change_kpi[_e.order_id] = _e.created_at
    _now_kpi = timezone.now()
    breached = on_track = at_risk = 0
    money_at_risk = 0.0
    for _o in _active_for_kpi:
        _sla = _STAGE_SLA_DAYS_KPI.get(_o.status, 0)
        if not _sla:
            on_track += 1; continue
        _entered = _last_change_kpi.get(_o.id) or _o.created_at
        _days = max(0, int((_now_kpi - _entered).total_seconds() / 86400))
        if _days > _sla:
            breached += 1
            money_at_risk += float(_o.total_amount or 0)
        elif _days >= _sla * 0.8:
            at_risk += 1
            money_at_risk += float(_o.total_amount or 0)
        else:
            on_track += 1
    total = breached + on_track + at_risk
    on_track_pct = (on_track / total * 100) if total else None
    # Breach rate
    breach_pct = int(breached * 100 / total) if total else None
    # Health score — при отсутствии данных показываем «—», а не пугающий 0%.
    if on_track_pct is None:
        health_label, health_tone, health_sub = "—", "info", "нет данных"
    else:
        health_tone = "ok" if on_track_pct >= 80 else ("warn" if on_track_pct >= 60 else "bad")
        health_label = f"{on_track_pct:.0f}%"
        health_sub = f"{on_track}/{total} в норме"
    if breach_pct is None:
        breach_label, breach_tone, breach_sub = "—", "info", "нет заказов"
    else:
        breach_label = f"{breach_pct}%"
        breach_tone = "bad" if breach_pct > 10 else ("warn" if breach_pct > 0 else "ok")
        breach_sub = f"{breached} нарушено"
    items = [
        {"label": "Доля заказов в срок",  "value": health_label,
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
        # subtitle: «среднее: —» бессмысленно — не показываем строку среднего
        # пока нет ни одного перехода. Вместо этого даём норматив и кол-во now.
        parts_sub = []
        if durations:
            parts_sub.append(f"среднее: {avg_label}")
        if sla_days:
            parts_sub.append(f"норматив SLA: {sla_days} дн")
        if n_now:
            parts_sub.append(f"сейчас здесь: {n_now}")
        if durations:
            parts_sub.append(f"{len(durations)} переходов")
        # badge: при отсутствии данных — показываем норматив, не «—»
        badge_lbl = avg_label if durations else (f"SLA {sla_days}д" if sla_days else "—")
        stage_rows.append({
            "title": f"{label}",
            "subtitle": " · ".join(parts_sub),
            "badge": {"label": badge_lbl, "tone": tone},
        })

    cards = [{"type": "kpi_grid",
              "data": {"title": f"⏱ SLA {scope_label}", "items": items}}]

    # ── По заказам: полная tracking-карточка на каждый активный заказ ──
    active_orders = list(qs.exclude(status__in=("cancelled",)).order_by("-created_at")[:50])
    if role in ("buyer", "seller") and active_orders:
        for o in active_orders[:8]:
            try:
                tr = track_order({"order_id": o.id}, user, role)
                for c in (tr.cards or []):
                    if c.get("type") == "tracking":
                        cards.append(c)
                        break
            except Exception:
                pass

    # Когда заказ вошёл в текущую стадию = время последнего status_changed.
    last_change = {}
    for ev in events:
        last_change[ev.order_id] = ev.created_at
    now_dt2 = timezone.now()
    per_order_rows = []
    for o in active_orders:
        stage_label = STAGE_LABELS.get(o.status, o.get_status_display())
        sla = STAGE_SLA_DAYS.get(o.status, 0)
        entered = last_change.get(o.id) or o.created_at
        days_here = max(0, int((now_dt2 - entered).total_seconds() / 86400))
        # tone: сравнение с нормативом текущего этапа
        if not sla:
            tone, badge_lbl = "info", "—"
        elif days_here > sla:
            tone, badge_lbl = "bad",  f"+{days_here - sla}д просрочка"
        elif days_here >= sla * 0.8:
            tone, badge_lbl = "warn", f"осталось {max(0, sla - days_here)}д"
        else:
            tone, badge_lbl = "ok",   f"{sla - days_here}д до лимита"
        # «Ответственный» — по фазе
        actor_by_stage = {
            "awaiting_reserve": "Покупатель",
            "reserve_paid":     "Поставщик",
            "confirmed":        "Поставщик",
            "in_production":    "Поставщик",
            "ready_to_ship":    "Поставщик",
            "transit_abroad":   "Перевозчик",
            "customs":          "Таможенный брокер",
            "transit_rf":       "Перевозчик",
            "issuing":          "Перевозчик",
            "delivered":        "Покупатель (приёмка)",
            "completed":        "—",
        }
        actor = actor_by_stage.get(o.status, "—")
        amount = f"${float(o.total_amount or 0):,.0f}"
        per_order_rows.append({
            "title":    f"Заказ #{o.id} · {amount}",
            "subtitle": (f"{stage_label} · здесь {days_here} дн"
                         + (f" / норматив {sla}д" if sla else "")
                         + f" · ответственный: {actor}"),
            "badge":    {"label": badge_lbl, "tone": tone},
            "action":   "get_order_detail",
            "params":   {"order_id": o.id},
        })
    # Компактный список — только для operator (у buyer/seller выше
    # уже полные tracking-карточки, дублировать нет смысла).
    if per_order_rows and role not in ("buyer", "seller"):
        cards.append({"type": "list", "data": {
            "title": f"⏱ По заказам — {len(per_order_rows)} активных",
            "items": per_order_rows,
        }})

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
            "title":    f"Заказ #{o.id} · {'Покупатель' if role == 'seller' else (o.customer_name or '')[:30]}",
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
    text_parts = [f"⏱ SLA {scope_label}: в норме {on_track}, под риском {at_risk}, нарушено {breached}."]
    if stage_rows:
        # самый медленный этап
        slow = max(((s["title"], s["badge"]["label"], s["badge"]["tone"])
                     for s in stage_rows if s["badge"]["tone"] != "info"),
                    key=lambda x: x[1] if x[2] == "bad" else "", default=None)
        if slow:
            text_parts.append(f"⚠️ Самый проблемный этап: {slow[0]} (среднее {slow[1]}).")
    if stuck:
        text_parts.append(f"🔴 {len(stuck)} заказов застряли — превысили норматив этапа более чем вдвое.")

    # ── Скорость ответа на запросы — реальная метрика (только продавцу) ──
    # Та же цифра, что в форме котировки: медиана RFQ.created→Quote.created + перцентиль.
    if role == "seller":
        try:
            from .seller_speed import seller_speed_standing
            sp = seller_speed_standing(user)
            if sp.get("median_min") is not None:
                line = f"⚡ Скорость ответа на запросы: медиана {sp['median_label']}"
                if sp.get("faster_than_pct") is not None:
                    line += f" · быстрее {sp['faster_than_pct']}% продавцов"
                text_parts.insert(0, line + f" (по {sp['count']} котировкам).")
                rows_sp = [{"title": "Медиана времени ответа", "subtitle": sp["median_label"]}]
                if sp.get("faster_than_pct") is not None:
                    rows_sp.append({"title": "Среди продавцов платформы",
                                    "subtitle": f"быстрее {sp['faster_than_pct']}% поставщиков"})
                rows_sp.append({"title": "Выборка", "subtitle": f"{sp['count']} последних котировок"})
                cards.insert(0, {"type": "list", "data": {
                    "title": "⚡ Ваша скорость ответа на запросы", "items": rows_sp}})
            else:
                text_parts.insert(0, "⚡ Скорость ответа на запросы: пока мало данных "
                                     "(посчитаем с 3-й котировки).")
        except Exception:
            pass

    return ActionResult(
        text="\n".join(text_parts),
        cards=cards,
        actions=(
            # Отчёт по одному заказу → даём кнопку «Трекинг» (shipment-карточка
            # с meta: сумма, оплата, состав, дни в стадии, SLA, ETA). Это
            # компактнее timeline-карточки выше и показывает другую инфу.
            ([{"label": "📦 Трекинг", "action": "track_shipment",
                "params": {"order_id": _single_oid}}] if _single_oid else [])
            + [
                {"label": "📊 Аналитика заказов", "action": "get_analytics",     "params": {}},
                ({"label": "💸 Экономия",         "action": "get_savings",       "params": {}}
                 if role == "buyer" else
                 {"label": "📦 Поставки",         "action": "get_supply_report", "params": {}}),
            ]
        ),
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

    # Параметры-фильтры (drill-down с KPI-ячеек):
    #   status=<X>       — конкретный статус (approved/rejected/closed/…)
    #   kind=<X>         — тип жалобы (defect/wrong_part/…)
    #   recent=30        — только за последние N дней
    #   with_refund=1    — только с компенсацией
    #   repeats=1        — только заказы с >1 жалобой
    filter_status = (params or {}).get("status")
    filter_kind = (params or {}).get("kind")
    filter_recent = (params or {}).get("recent")
    filter_refund = (params or {}).get("with_refund")
    filter_repeats = (params or {}).get("repeats")
    if filter_status:
        qs = qs.filter(status=filter_status)
    if filter_kind:
        qs = qs.filter(kind=filter_kind)
    if filter_recent:
        try:
            cutoff = timezone.now() - timezone.timedelta(days=int(filter_recent))
            qs = qs.filter(created_at__gte=cutoff)
        except (TypeError, ValueError):
            pass
    if filter_refund:
        qs = qs.exclude(refund_amount__isnull=True).exclude(refund_amount=0)
    if filter_repeats:
        # Заказы с >1 жалобой — отбираем order_id с count > 1
        from django.db.models import Count
        repeat_ids = (OrderClaim.objects.values("order_id")
                       .annotate(n=Count("id")).filter(n__gt=1)
                       .values_list("order_id", flat=True))
        qs = qs.filter(order_id__in=list(repeat_ids))

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

    # Каждая ячейка — реальное число + sub + drill-down action на отфильтрованный список.
    kpi_items = []
    if avg_resolution is not None:
        tone = "bad" if avg_resolution > 7 else ("warn" if avg_resolution > 5 else "ok")
        kpi_items.append({
            "label": "Срок разбора жалобы",
            "value": f"{avg_resolution:.0f} дн",
            "sub": "от подачи до закрытия, среднее",
            "tone": tone,
            "action": "get_claims",
            "params": {"status": "closed"},
        })
    if approval_rate is not None and reviewed:
        tone = "bad" if approval_rate >= 70 else ("warn" if approval_rate >= 40 else "ok")
        kpi_items.append({
            "label": "Жалобы признаны обоснованными",
            "value": f"{approval_rate}%",
            "sub": f"{approved_total} из {len(reviewed)} рассмотренных",
            "tone": tone,
            "action": "get_claims",
            "params": {"status": "approved"},
        })
    if pending_refund:
        kpi_items.append({
            "label": "Деньги под риском",
            "value": f"${int(pending_refund):,}".replace(",", " "),
            "sub": "по жалобам ещё в разборе",
            "tone": "warn",
            "action": "get_claims",
            "params": {"status": "in_review"},
        })
    if top_kind:
        share = top_kind[1] * 100 // len(all_claims)
        kpi_items.append({
            "label": "Чаще всего жалуются на",
            "value": KIND_LBL_LOCAL.get(top_kind[0], top_kind[0]),
            "sub": f"{share}% жалоб · {top_kind[1]} шт за всё время",
            "tone": "warn" if share >= 50 else "info",
            "action": "get_claims",
            "params": {"kind": top_kind[0]},
        })
    diff = last_30 - prev_30
    if abs(diff) >= 1:
        if diff < 0:
            kpi_items.append({
                "label": "Жалоб стало меньше",
                "value": f"на {abs(diff)} меньше",
                "sub": f"было {prev_30} → стало {last_30} за месяц",
                "tone": "ok",
                "action": "get_claims",
                "params": {"recent": 30},
            })
        else:
            kpi_items.append({
                "label": "Жалоб стало больше",
                "value": f"на {diff} больше",
                "sub": f"было {prev_30} → стало {last_30} за месяц",
                "tone": "bad" if diff >= 3 else "warn",
                "action": "get_claims",
                "params": {"recent": 30},
            })
    if repeat_orders:
        kpi_items.append({
            "label": "Заказы с повторными жалобами",
            "value": str(repeat_orders),
            "sub": "более одной претензии по одному заказу",
            "tone": "warn",
            "action": "get_claims",
            "params": {"repeats": 1},
        })
    if avg_refund:
        kpi_items.append({
            "label": "Средний возврат",
            "value": f"${int(avg_refund):,}".replace(",", " "),
            "sub": "по закрытым с компенсацией",
            "tone": "info",
            "action": "get_claims",
            "params": {"with_refund": 1, "status": "closed"},
        })
    if total_refund:
        kpi_items.append({
            "label": "Возвращено покупателям",
            "value": f"${int(total_refund):,}".replace(",", " "),
            "sub": "всего за всё время",
            "tone": "info",
            "action": "get_claims",
            "params": {"with_refund": 1},
        })

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
        who = "Покупатель" if role == "seller" else (order.buyer.username if order.buyer_id else (order.customer_name or "—"))[:24]
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


@register("contact_supplier")
def contact_supplier(params, user, role):
    """Карточка поставщика для оператора: контакты + склады + каталог + активные
    заказы + производительность + рекламации. Не «связаться», а полный обзор для
    контроля отгрузок.
    """
    if not (role and (role.startswith("operator") or role == "admin")):
        return ActionResult(text="Доступно только оператору.")
    seller_id = params.get("seller_id")
    if not seller_id:
        return ActionResult(text="Не указан ID поставщика.")
    from django.contrib.auth import get_user_model
    from datetime import timedelta
    from django.db.models import Count, Sum, Avg, Q
    from django.utils import timezone
    User = get_user_model()
    try:
        seller = User.objects.select_related("profile").get(id=int(seller_id))
    except (User.DoesNotExist, ValueError, TypeError):
        return ActionResult(text="Поставщик не найден.")

    now = timezone.now()
    cutoff_90 = now - timedelta(days=90)

    # ── 1) Контакты ──────────────────────────────────────────
    contact_rows = [
        {"label": "Username", "value": seller.username, "primary": True},
    ]
    kyb = None
    try:
        from marketplace.models import CompanyVerification
        kyb = CompanyVerification.objects.filter(user=seller).first()
    except Exception:
        kyb = None
    if kyb and kyb.legal_name:
        contact_rows.append({"label": "Компания", "value": kyb.legal_name, "wide": True})
    if kyb and kyb.country:
        contact_rows.append({"label": "Страна", "value": kyb.country})
    if kyb and kyb.director_name:
        contact_rows.append({"label": "Директор", "value": kyb.director_name})
    if kyb and kyb.phone:
        contact_rows.append({"label": "Телефон", "value": kyb.phone})
    if kyb and (kyb.whatsapp or kyb.telegram):
        msgs = []
        if kyb.whatsapp: msgs.append(f"WA {kyb.whatsapp}")
        if kyb.telegram: msgs.append(f"TG {kyb.telegram}")
        contact_rows.append({"label": "Мессенджеры", "value": " · ".join(msgs)})
    contact_rows.append({"label": "Email", "value": (kyb.contact_email if kyb and kyb.contact_email else (seller.email or "—"))})

    # Статус и рейтинг — отдельной строкой
    prof = getattr(seller, "profile", None)
    _STATUS_RU = {"trusted": "Надёжный", "sandbox": "Песочница",
                  "risky": "Рисковый", "rejected": "Исключён"}
    if prof:
        contact_rows.append({"label": "Статус", "value": _STATUS_RU.get(prof.supplier_status, prof.supplier_status or "—")})
        if getattr(prof, "rating_score", None):
            contact_rows.append({"label": "Рейтинг", "value": f"{float(prof.rating_score):.1f} / 5"})

    # ── 2) Склады ────────────────────────────────────────────
    warehouse_rows = []
    try:
        from marketplace.models import SellerWarehouse
        for w in SellerWarehouse.objects.filter(seller=seller, is_active=True)[:5]:
            sub_parts = []
            if w.country_code: sub_parts.append(w.country_code)
            if w.sea_port: sub_parts.append(f"порт {w.sea_port}")
            if w.air_port: sub_parts.append(f"аэропорт {w.air_port}")
            if w.address: sub_parts.append(w.address[:60])
            warehouse_rows.append({
                "title": w.name or "Склад",
                "subtitle": " · ".join(sub_parts) or "—",
                "tone": "info",
            })
    except Exception:
        pass

    # ── 3) Каталог: сколько SKU, диапазон цен ────────────────
    catalog_summary = None
    try:
        from marketplace.models import Part
        parts_qs = Part.objects.filter(seller=seller)
        sku_count = parts_qs.count()
        if sku_count:
            price_stats = parts_qs.aggregate(min_p=Avg("price"), n=Count("id"),
                                              total=Sum("price"))
            catalog_summary = {
                "title": "Каталог поставщика",
                "subtitle": f"{sku_count} SKU · средняя цена ${float(price_stats['min_p'] or 0):,.0f}",
                "tone": "info",
                "action": "search_parts",
                "params": {"seller_username": seller.username},
            }
    except Exception:
        pass

    # ── 4) Активные заказы для контроля ──────────────────────
    active_rows = []
    try:
        from marketplace.models import Order
        active_orders = (Order.objects
                          .filter(items__part__seller=seller)
                          .exclude(status__in=("completed", "cancelled", "delivered"))
                          .distinct()
                          .order_by("ship_deadline", "-created_at")[:10])
        STAGE_RU = {"awaiting_reserve": "Ждёт резерв 10%",
                    "reserve_paid":     "Резерв оплачен",
                    "confirmed":        "Подтверждён",
                    "in_production":    "В производстве",
                    "ready_to_ship":    "К отгрузке",
                    "transit_abroad":   "Транзит",
                    "customs":          "Таможня",
                    "transit_rf":       "Транзит по РФ",
                    "issuing":          "На выдаче"}
        for o in active_orders:
            stage = STAGE_RU.get(o.status, o.get_status_display())
            sla_tone = ("bad" if o.sla_status == "breached"
                         else "warn" if o.sla_status == "at_risk" else "info")
            sub_parts = [stage, f"${float(o.total_amount or 0):,.0f}"]
            if o.ship_deadline:
                days_left = int((o.ship_deadline - now).total_seconds() / 86400)
                if days_left < 0:
                    sub_parts.append(f"просрочен на {abs(days_left)} дн")
                else:
                    sub_parts.append(f"дедлайн через {days_left} дн")
            active_rows.append({
                "title": f"ORD-{o.id} · {o.customer_name or 'покупатель'}",
                "subtitle": " · ".join(sub_parts),
                "tone": sla_tone,
                "action": "op_order_detail",
                "params": {"order_id": o.id},
            })
    except Exception:
        pass

    # ── 5) Производительность за 90д ─────────────────────────
    perf_items = []
    try:
        from marketplace.models import Order, OrderEvent
        delivered_qs = Order.objects.filter(items__part__seller=seller,
                                             status__in=("delivered", "completed"),
                                             created_at__gte=cutoff_90).distinct()
        delivered_n = delivered_qs.count()
        if delivered_n:
            # On-time %: сколько без SLA-нарушений
            on_time = delivered_qs.filter(sla_breaches_count=0).count()
            on_time_pct = on_time * 100 // delivered_n
            # Средний срок отгрузки (created → ready_to_ship)
            durations = []
            for o in delivered_qs[:100]:
                ev = OrderEvent.objects.filter(order=o, event_type="status_changed",
                                                meta__to="ready_to_ship").order_by("-created_at").first()
                if ev and o.created_at:
                    durations.append((ev.created_at - o.created_at).total_seconds() / 86400)
            avg_ship_days = sum(durations) / len(durations) if durations else None

            perf_items.extend([
                {"label": "Доставлено за 90д", "value": str(delivered_n),
                 "tone": "info"},
                {"label": "Без срывов SLA", "value": f"{on_time_pct}%",
                 "sub": f"{on_time} из {delivered_n}",
                 "tone": "ok" if on_time_pct >= 90 else ("warn" if on_time_pct >= 70 else "bad")},
            ])
            if avg_ship_days is not None:
                perf_items.append({
                    "label": "Срок отгрузки",
                    "value": f"{avg_ship_days:.0f} дн",
                    "sub": "от заказа до готовности",
                    "tone": "ok" if avg_ship_days <= 14 else ("warn" if avg_ship_days <= 30 else "bad"),
                })

        # Активные заказы — всегда
        active_n = (Order.objects.filter(items__part__seller=seller)
                      .exclude(status__in=("completed", "cancelled", "delivered"))
                      .distinct().count())
        perf_items.append({
            "label": "Активных заказов",
            "value": str(active_n),
            "sub": ("требуют контроля" if active_n else "ничего в работе"),
            "tone": "warn" if active_n > 5 else "info",
        })
    except Exception:
        pass

    # ── 6) Открытые рекламации ───────────────────────────────
    claim_warning = None
    try:
        from marketplace.models import Claim
        open_claims = Claim.objects.filter(
            Q(order__items__part__seller=seller),
            status__in=("open", "in_review", "escalated"),
        ).distinct().count()
        if open_claims:
            claim_warning = {
                "title": "Открытые рекламации",
                "subtitle": f"{open_claims} активных — требуют разбора",
                "tone": "bad" if open_claims >= 3 else "warn",
                "action": "get_claims",
                "params": {"seller_id": seller.id},
            }
    except Exception:
        pass

    # ── Собираем карточки ──
    cards = [
        {"type": "draft", "data": {
            "title": f"📇 Контакты · {seller.username}",
            "rows": contact_rows, "confirm_label": "—",
        }},
    ]
    if perf_items:
        cards.append({"type": "kpi_grid", "data": {
            "title": "📊 Как работает (90 дней)", "items": perf_items,
        }})
    if active_rows:
        cards.append({"type": "list", "data": {
            "title": f"🔥 Активные заказы — {len(active_rows)} в работе",
            "items": active_rows,
        }})
    if warehouse_rows:
        cards.append({"type": "list", "data": {
            "title": f"📦 Склады · {len(warehouse_rows)}",
            "items": warehouse_rows,
        }})
    if catalog_summary:
        cards.append({"type": "list", "data": {
            "title": "🛒 Каталог", "items": [catalog_summary],
        }})
    if claim_warning:
        cards.append({"type": "list", "data": {
            "title": "⚠️ Внимание", "items": [claim_warning],
        }})

    # Intro text — что оператору важно сразу
    intro_parts = []
    if active_rows:
        intro_parts.append(f"{len(active_rows)} заказов в работе")
    if perf_items:
        on_time_item = next((x for x in perf_items if x["label"] == "Без срывов SLA"), None)
        if on_time_item: intro_parts.append(f"SLA {on_time_item['value']}")
    if claim_warning:
        intro_parts.append("есть открытые рекламации")
    intro = (f"Поставщик {seller.username}: " + " · ".join(intro_parts) + "."
             if intro_parts else f"Поставщик {seller.username} — нет активной работы.")

    return ActionResult(
        text=intro,
        cards=cards,
        actions=[
            {"label": "🤝 Чем помочь",
             "action": "op_help_supplier", "params": {"seller_id": seller.id}},
            {"label": "👁 Войти как поставщик (просмотр)",
             "action": "op_view_as_supplier", "params": {"seller_id": seller.id}},
            {"label": "📋 KYB-анкета", "action": "op_kyb_review",
             "params": {"user_id": seller.id}},
            {"label": "💬 Написать", "action": "ask_operator",
             "params": {"to_user_id": seller.id,
                         "context": f"Связь с поставщиком #{seller.id} ({seller.username})"}},
        ],
        contextual_actions=[
            {"action": "op_my_suppliers", "label": "← Мои поставщики"},
        ],
    )


@register("op_view_as_supplier")
def op_view_as_supplier(params, user, role):
    """Переключить сессию оператора в режим «view-as» — он видит чат и кабинет
    как поставщик, но без права изменений. Возврат через op_exit_view_as.
    """
    # ВАЖНО: если уже в view-as, оригинального оператора возьмём из сессии.
    # Иначе role будет 'seller' (текущий target), и мы откажем себе в действии.
    from django.contrib.auth import get_user_model
    User = get_user_model()
    request = (params or {}).get("_request")
    sess = getattr(request, "session", None) if request else None

    # Если уже view-as — оригинальный оператор хранится в session
    original_user = user
    if sess and sess.get("op_view_as_originator_id"):
        try:
            original_user = User.objects.get(id=sess["op_view_as_originator_id"])
        except User.DoesNotExist:
            pass

    if not (original_user.is_authenticated and (original_user.is_staff or original_user.is_superuser)):
        return ActionResult(text="Доступно только оператору / админу.")

    seller_id = (params or {}).get("seller_id")
    if not seller_id:
        return ActionResult(text="Не указан ID поставщика.")
    try:
        seller = User.objects.select_related("profile").get(id=int(seller_id))
    except (User.DoesNotExist, ValueError, TypeError):
        return ActionResult(text="Поставщик не найден.")

    # FIX (CRITICAL): нельзя view-as staff/superuser — иначе оператор получает
    # полный доступ через имперсонацию админа.
    if seller.is_staff or seller.is_superuser:
        return ActionResult(text="Нельзя войти как служебный аккаунт.")
    target_role = getattr(getattr(seller, "profile", None), "role", "")
    if target_role and target_role != "seller":
        return ActionResult(text="View-as доступно только для поставщиков.")

    # Записываем в сессию
    if sess is None:
        return ActionResult(text="Сессия недоступна — обновите страницу.")
    sess["op_view_as_id"] = seller.id
    sess["op_view_as_originator_id"] = original_user.id
    sess.modified = True

    return ActionResult(
        text=(f"🔍 Просмотр кабинета поставщика {seller.username} "
              f"(только чтение). Все ваши действия будут заблокированы — "
              f"чтобы вернуться в свой аккаунт, нажмите «Выйти из просмотра» "
              f"или перейдите на главную."),
        actions=[
            {"label": "🚪 Выйти из просмотра", "action": "op_exit_view_as", "params": {}},
            {"label": "🏠 Кабинет поставщика", "action": "go_home", "params": {}},
        ],
    )


@register("op_help_supplier")
def op_help_supplier(params, user, role):
    """Панель помощи поставщику для оператора (доступна в view-as режиме).

    Собирает список того, что нужно помочь сделать, и для каждого пункта
    даёт конкретную кнопку-действие: напомнить, заполнить, эскалировать.
    """
    from django.contrib.auth import get_user_model
    from django.db.models import Q
    from datetime import timedelta
    from django.utils import timezone

    User = get_user_model()
    request = (params or {}).get("_request")
    sess = getattr(request, "session", None) if request else None

    # Определяем target: либо мы в view-as (берём из сессии), либо передали seller_id
    seller_id = (params or {}).get("seller_id")
    if not seller_id and sess and sess.get("op_view_as_id"):
        seller_id = sess["op_view_as_id"]
    if not seller_id:
        return ActionResult(text="Не указан поставщик. Откройте «Мои поставщики» и выберите.")

    try:
        seller = User.objects.select_related("profile").get(id=int(seller_id))
    except (User.DoesNotExist, ValueError, TypeError):
        return ActionResult(text="Поставщик не найден.")

    # Оригинальный оператор (если в view-as)
    original_user = user
    if sess and sess.get("op_view_as_originator_id"):
        try:
            original_user = User.objects.get(id=sess["op_view_as_originator_id"])
        except User.DoesNotExist:
            pass
    if not (original_user.is_authenticated and (original_user.is_staff or original_user.is_superuser)):
        return ActionResult(text="Доступно только оператору.")

    now = timezone.now()
    soon = now + timedelta(days=2)

    # ── Собираем «болевые точки» поставщика, каждая = строка с action ──
    help_rows = []

    # 1) Заказы с просроченным или близким SLA — напомнить продавцу
    from marketplace.models import Order
    overdue_orders = (Order.objects
                       .filter(items__part__seller=seller)
                       .filter(Q(sla_status="breached") | Q(sla_status="at_risk")
                                | (Q(ship_deadline__isnull=False) & Q(ship_deadline__lt=soon)))
                       .exclude(status__in=("delivered", "completed", "cancelled"))
                       .distinct()[:5])
    for o in overdue_orders:
        if o.sla_status == "breached" or (o.ship_deadline and o.ship_deadline < now):
            tone = "bad"
            urgency = "просрочено"
        else:
            tone = "warn"
            urgency = "скоро дедлайн"
        help_rows.append({
            "title": f"ORD-{o.id} · {urgency}",
            "subtitle": (f"Этап: {o.get_status_display()} · "
                          f"${float(o.total_amount or 0):,.0f} · "
                          f"Помочь: напомнить поставщику / эскалировать"),
            "tone": tone,
            "action": "op_help_send_reminder",
            "params": {"order_id": o.id, "seller_id": seller.id},
        })

    # 2) KYB-анкета с пробелами
    from marketplace.models import CompanyVerification
    kyb = CompanyVerification.objects.filter(user=seller).first()
    if kyb:
        missing = []
        if not kyb.legal_name: missing.append("название")
        if not kyb.inn: missing.append("ИНН")
        if not kyb.bank_name: missing.append("банк")
        if not kyb.director_name: missing.append("директор")
        if not kyb.warehouse_address: missing.append("адрес склада")
        if not kyb.phone: missing.append("телефон")
        if missing:
            help_rows.append({
                "title": f"KYB-анкета: пропуски ({len(missing)})",
                "subtitle": ("Нет данных: " + ", ".join(missing[:4])
                              + " · Помочь: заполнить с поставщиком"),
                "tone": "warn",
                "action": "op_kyb_review",
                "params": {"user_id": seller.id},
            })

    # 3) Нет склада → нечем грузить
    from marketplace.models import SellerWarehouse
    if not SellerWarehouse.objects.filter(seller=seller, is_active=True).exists():
        help_rows.append({
            "title": "Нет активных складов",
            "subtitle": "Поставщик не указал склад — невозможно начать отгрузку. Помочь: добавить.",
            "tone": "bad",
            "action": "seller_warehouses",  # перейдёт в кабинет складов в view-as режиме
            "params": {},
        })

    # 4) Каталог пуст / маленький
    from marketplace.models import Part
    sku_count = Part.objects.filter(seller=seller).count()
    if sku_count < 5:
        help_rows.append({
            "title": ("Каталог пуст" if not sku_count else f"Каталог маленький ({sku_count} SKU)"),
            "subtitle": "Помочь импортировать прайс-лист или заполнить вручную",
            "tone": "warn" if sku_count else "bad",
            "action": "upload_pricelist",
            "params": {},
        })

    # 5) Открытые рекламации
    try:
        from marketplace.models import Claim
        open_claims = Claim.objects.filter(
            Q(order__items__part__seller=seller),
            status__in=("open", "in_review"),
        ).distinct()[:3]
        for c in open_claims:
            help_rows.append({
                "title": f"Рекламация #{c.id}",
                "subtitle": f"Тип: {c.get_kind_display() if hasattr(c, 'get_kind_display') else c.kind} · Помочь: разобрать с покупателем",
                "tone": "bad",
                "action": "claim_detail",
                "params": {"claim_id": c.id},
            })
    except Exception:
        pass

    # ── Сборка карточек ──
    cards = []
    if help_rows:
        cards.append({"type": "list", "data": {
            "title": f"🤝 Чем помочь · {seller.username}",
            "items": help_rows,
        }})
    else:
        cards.append({"type": "list", "data": {
            "title": "🤝 Чем помочь",
            "items": [{"title": "✅ Всё в порядке",
                        "subtitle": "Нет просроченных заказов, KYB заполнен, склады есть."}],
        }})

    # Шаблонные действия независимо от состояния
    template_actions = [
        {"label": "💬 Связаться от себя",
         "action": "ask_operator",
         "params": {"to_user_id": seller.id,
                     "context": f"Помощь поставщику {seller.username}"}},
        {"label": "📋 KYB-анкета", "action": "op_kyb_review",
         "params": {"user_id": seller.id}},
        {"label": "🚨 Эскалировать менеджеру",
         "action": "op_help_escalate", "params": {"seller_id": seller.id}},
    ]

    intro = (f"Обнаружено {len(help_rows)} пунктов помощи поставщику {seller.username}."
             if help_rows else f"Поставщик {seller.username} — без болевых точек.")

    return ActionResult(
        text=intro,
        cards=cards,
        actions=template_actions,
        contextual_actions=[
            {"action": "contact_supplier", "label": "← Карточка поставщика",
             "params": {"seller_id": seller.id}},
        ],
    )


@register("op_help_send_reminder")
def op_help_send_reminder(params, user, role):
    """Отправить поставщику напоминание о заказе с просрочкой / близким SLA.

    Создаёт нотификацию + опционально системное сообщение в чат.
    """
    from django.contrib.auth import get_user_model
    from marketplace.models import Order
    User = get_user_model()
    request = (params or {}).get("_request")
    sess = getattr(request, "session", None) if request else None

    # В view-as: оригинальный оператор — из сессии
    original_user = user
    if sess and sess.get("op_view_as_originator_id"):
        try:
            original_user = User.objects.get(id=sess["op_view_as_originator_id"])
        except User.DoesNotExist:
            pass
    if not (original_user.is_authenticated and (original_user.is_staff or original_user.is_superuser)):
        return ActionResult(text="Доступно только оператору.")

    order_id = (params or {}).get("order_id")
    seller_id = (params or {}).get("seller_id")
    if not (order_id and seller_id):
        return ActionResult(text="Не указан заказ или поставщик.")
    try:
        order = Order.objects.get(id=int(order_id))
        seller = User.objects.get(id=int(seller_id))
    except (Order.DoesNotExist, User.DoesNotExist, ValueError, TypeError):
        return ActionResult(text="Заказ или поставщик не найден.")

    # Создаём notification поставщику
    try:
        from marketplace.models import Notification
        Notification.objects.create(
            user=seller,
            kind="sla_reminder",
            title=f"Напоминание оператора по ORD-{order.id}",
            body=(f"Оператор {original_user.username} просит обратить внимание "
                   f"на заказ ORD-{order.id} (статус: {order.get_status_display()}). "
                   f"Свяжитесь для уточнения сроков."),
            url=f"/chat/?action=op_order_detail&order_id={order.id}",
        )
    except Exception as e:
        return ActionResult(text=f"Не удалось отправить напоминание: {e}")

    return ActionResult(
        text=(f"✅ Напоминание по ORD-{order.id} отправлено поставщику {seller.username}. "
              f"Он получит нотификацию и email."),
        actions=[
            {"label": "← Чем ещё помочь", "action": "op_help_supplier",
             "params": {"seller_id": seller.id}},
        ],
    )


@register("op_help_escalate")
def op_help_escalate(params, user, role):
    """Эскалация ситуации с поставщиком: создаёт тикет / сообщение оператору-менеджеру."""
    from django.contrib.auth import get_user_model
    User = get_user_model()
    request = (params or {}).get("_request")
    sess = getattr(request, "session", None) if request else None

    original_user = user
    if sess and sess.get("op_view_as_originator_id"):
        try:
            original_user = User.objects.get(id=sess["op_view_as_originator_id"])
        except User.DoesNotExist:
            pass
    if not (original_user.is_authenticated and (original_user.is_staff or original_user.is_superuser)):
        return ActionResult(text="Доступно только оператору.")

    seller_id = (params or {}).get("seller_id")
    if not seller_id:
        return ActionResult(text="Не указан поставщик.")
    try:
        seller = User.objects.get(id=int(seller_id))
    except (User.DoesNotExist, ValueError, TypeError):
        return ActionResult(text="Поставщик не найден.")

    # Логируем эскалацию в audit-log (простая запись, без отдельного ticket-модели)
    try:
        from marketplace.models import Notification
        # Уведомляем всех manager'ов
        for mgr in User.objects.filter(is_staff=True, username__icontains="manager")[:5]:
            Notification.objects.create(
                user=mgr,
                kind="escalation",
                title=f"Эскалация: поставщик {seller.username}",
                body=(f"Оператор {original_user.username} эскалировал ситуацию "
                       f"по поставщику {seller.username}. Откройте его карточку для разбора."),
                url=f"/chat/?action=contact_supplier&seller_id={seller.id}",
            )
    except Exception as e:
        return ActionResult(text=f"Не удалось эскалировать: {e}")

    return ActionResult(
        text=(f"🚨 Эскалация по {seller.username} отправлена операторам-менеджерам. "
              f"Они получат уведомление и подключатся к разбору."),
        actions=[
            {"label": "← Чем ещё помочь", "action": "op_help_supplier",
             "params": {"seller_id": seller.id}},
        ],
    )


@register("op_exit_view_as")
def op_exit_view_as(params, user, role):
    """Выход из view-as: очистка сессии, возврат к оригинальному оператору."""
    request = (params or {}).get("_request")
    sess = getattr(request, "session", None) if request else None
    if not sess:
        return ActionResult(text="Сессия недоступна.")
    if not sess.get("op_view_as_id"):
        return ActionResult(text="Вы не в режиме просмотра.")
    target_id = sess.pop("op_view_as_id", None)
    original_id = sess.pop("op_view_as_originator_id", None)
    sess.modified = True
    return ActionResult(
        text=f"Вернулись в свой аккаунт оператора. Просмотр поставщика #{target_id} завершён.",
        actions=[
            {"label": "🏠 На главный экран", "action": "go_home", "params": {}},
            {"label": "📋 Мои поставщики", "action": "op_my_suppliers", "params": {}},
        ],
    )


@register("ask_operator")
def ask_operator(params, user, role):
    """Открыть диалог с оператором с контекстом по конкретному заказу/RFQ.
    Юзер жмёт «💬 Написать оператору» → получает приглашение задать вопрос,
    AI/оператор отвечает в этом же чате. Контекст (order_id/rfq_id) пришивается
    к conversation чтобы оператор сразу видел о чём речь.
    """
    order_id = params.get("order_id")
    rfq_id = params.get("rfq_id")
    to_user_id = params.get("to_user_id")
    ctx_label = ""
    suggestions = []

    # Оператор связывается с другим пользователем (поставщиком/покупателем).
    # Контекст автоматически доставляется: создаётся системное сообщение
    # в чате собеседника + notification — он сразу видит о чём речь, без
    # необходимости оператору это пересказывать.
    if to_user_id and role and (role.startswith("operator") or role == "admin"):
        from django.contrib.auth import get_user_model
        from .models import Conversation, Message
        User = get_user_model()
        try:
            target = User.objects.get(id=int(to_user_id))
        except (User.DoesNotExist, ValueError, TypeError):
            return ActionResult(text="Получатель не найден.")
        ctx = params.get("context") or f"диалог с {target.username}"

        # FIX (HIGH): только staff/manager могут инициировать кросс-юзер диалог.
        # Раньше любой оператор-логист мог отправить любому юзеру «системное»
        # сообщение от имени assistant — потенциал для фишинга.
        if not (user.is_staff or role == "operator_manager" or role == "admin"):
            return ActionResult(text=(
                "Связь с пользователем доступна только operator_manager/admin. "
                "Эскалируйте задачу старшему оператору."
            ))
        # 1) Создаём/находим support-conversation у получателя c этим контекстом
        target_conv, _ = Conversation.objects.get_or_create(
            user=target, category="support",
            title=f"💬 {ctx}"[:200],
            defaults={"role": "buyer", "is_active": True},
        )
        # 2) Кладём системное сообщение с контекстом — с пометкой что это
        # инициировал конкретный оператор (не «system»), чтобы юзер видел
        # источник и мог фильтровать спам.
        Message.objects.create(
            conversation=target_conv,
            role=Message.Role.ASSISTANT,
            content=(
                f"📋 Оператор {user.username} ({role}) открыл с вами диалог.\n\n"
                f"Контекст: {ctx}\n\n"
                f"Дождитесь сообщения оператора — он сейчас напишет. "
                f"Если хотите ответить заранее — пишите ниже. "
                f"Если это спам или ошибка — нажмите «Пожаловаться»."
            ),
        )
        # 3) Notification + email (если включён) — чтоб получатель сразу узнал
        try:
            from marketplace.models import Notification
            Notification.objects.create(
                user=target, kind="claim",
                title=f"Оператор {user.username} открыл диалог",
                body=ctx,
                url=f"/chat/?conv={target_conv.id}",
            )
        except Exception:
            pass

        # 4) FIX: создаём ЗЕРКАЛЬНУЮ conversation у самого оператора, чтобы он
        # потом мог вернуться к этому диалогу через свой сайдбар. Без этого
        # оператор «теряет» чат после отправки контекста.
        op_conv_title = f"💬 → {target.username} · {ctx[:120]}"
        op_conv, op_created = Conversation.objects.get_or_create(
            user=user, category="support",
            title=op_conv_title[:200],
            defaults={"role": role or "operator", "is_active": True},
        )
        if op_created:
            Message.objects.create(
                conversation=op_conv,
                role=Message.Role.ASSISTANT,
                content=(
                    f"📂 Диалог с {target.username}\n\n"
                    f"Контекст: {ctx}\n\n"
                    f"Сообщения, которые вы здесь напишете, увидит {target.username} "
                    f"в их support-чате. Их ответы появятся у них же — оператор "
                    f"видит их в этом разделе.\n\n"
                    f"Все ваши диалоги с пользователями можно увидеть в действии "
                    f"«op_my_user_chats»."
                ),
            )

        return ActionResult(
            text=(
                f"✅ Контекст доставлен {target.username}: "
                f"у него в чате появилось системное сообщение со ссылкой "
                f"и notification на email/Telegram (если подключены).\n\n"
                f"Этот диалог сохранён в вашем сайдбаре как "
                f"«💬 → {target.username}» — открыть позже через раздел "
                f"«Мои диалоги с пользователями».\n\n"
                f"Пишите ваше сообщение ниже — оно отправится в этот же поток."
            ),
            actions=[
                {"label": "📂 Мои диалоги с пользователями",
                 "action": "op_my_user_chats", "params": {}},
            ],
            suggestions=[
                "Здравствуйте! Уточните пожалуйста детали по этому случаю",
                "Прислали ли вы фото / документы?",
                "Когда удобно созвониться?",
            ],
        )

    if order_id:
        ctx_label = f"заказу #{order_id}"
        suggestions = [
            f"Где сейчас заказ #{order_id}?",
            f"Когда придёт #{order_id}?",
            f"Можно ли ускорить #{order_id}?",
            "Изменить адрес доставки",
        ]
    elif rfq_id:
        ctx_label = f"RFQ #{rfq_id}"
        suggestions = [
            f"Когда ждать котировок по RFQ #{rfq_id}?",
            f"Найти аналоги дешевле для RFQ #{rfq_id}",
            f"Продлить срок RFQ #{rfq_id}",
        ]
    else:
        ctx_label = "общий вопрос"
        suggestions = [
            "Помогите подобрать запчасть",
            "Сравнить поставщиков",
            "Статус всех моих заказов",
        ]
    return ActionResult(
        text=(
            f"💬 Связь с оператором ({ctx_label})\n\n"
            f"Напишите ваш вопрос в чате ниже — оператор Consolidator Parts ответит "
            f"в течение рабочего времени (обычно 15–30 минут). "
            f"AI пока поможет ответить на типовые вопросы быстрее.\n\n"
            f"Если вопрос срочный — упомяните «срочно» в сообщении."
        ),
        suggestions=suggestions,
    )


@register("op_my_user_chats")
def op_my_user_chats(params, user, role):
    """Список диалогов оператора с пользователями (поставщики/покупатели).

    Это зеркальные conversation'ы, которые создаются при вызове ask_operator
    оператором. Каждая запись = один пользователь + контекст. Клик → откроется
    эта conversation в основной области, можно продолжить общение.
    """
    if not ((role or "").startswith("operator") or role == "admin"):
        return ActionResult(text="Доступно только оператору.")
    from .models import Conversation, Message
    convs = list(Conversation.objects.filter(
        user=user, category="support",
        title__startswith="💬 → ",
        is_active=True,
    ).order_by("-updated_at")[:30])
    if not convs:
        return ActionResult(
            text=(
                "У вас нет открытых диалогов с пользователями.\n\n"
                "Диалог с покупателем или поставщиком открывается через карточку "
                "поставщика / рекламацию / тикет поддержки — кнопкой «💬 Чат с …». "
                "После открытия он появится здесь."
            ),
            actions=[
                {"label": "📋 Мои поставщики", "action": "op_my_suppliers", "params": {}},
                {"label": "📥 Inbox поддержки", "action": "support_home", "params": {}},
            ],
        )
    rows = []
    for c in convs:
        # Last activity
        last_msg = Message.objects.filter(conversation=c).order_by("-created_at").first()
        last_at = last_msg.created_at.strftime("%d.%m %H:%M") if last_msg else "—"
        # Title уже содержит «💬 → username · context»
        title = c.title.replace("💬 → ", "")  # cleanup display
        rows.append({
            "title": title[:80],
            "subtitle": f"Последнее: {last_at}",
            "tone": "info",
            "action": "open_conversation",
            "params": {"conversation_id": str(c.id)},
        })
    return ActionResult(
        text=f"📂 Ваши диалоги с пользователями · {len(convs)}",
        cards=[{"type": "list", "data": {
            "title": "📂 Мои диалоги с пользователями",
            "items": rows,
        }}],
        actions=[
            {"label": "📥 Inbox поддержки", "action": "support_home", "params": {}},
        ],
    )


@register("open_conversation")
def open_conversation(params, user, role):
    """Переключиться на конкретную conversation (используется как drill-down
    из списков). Делает frontend-redirect через _navigate.
    """
    conv_id = (params or {}).get("conversation_id")
    if not conv_id:
        return ActionResult(text="Не указана conversation.")
    from .models import Conversation
    try:
        conv = Conversation.objects.get(id=conv_id, user=user)
    except (Conversation.DoesNotExist, ValueError):
        return ActionResult(text="Conversation не найден или не ваш.")
    return ActionResult(
        text=f"Открываю «{conv.title or 'диалог'}»…",
        actions=[
            {"label": f"📂 Открыть {conv.title[:60] if conv.title else 'диалог'}",
             "action": "open_url",
             "params": {"_url": f"/chat/?conv={conv.id}"}},
        ],
    )


@register("ask_about_rfq")
def ask_about_rfq(params, user, role):
    """Alias к ask_operator с RFQ-контекстом."""
    return ask_operator({"rfq_id": params.get("rfq_id")}, user, role)


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
    if created:  text_parts.append(f"Создано позиций: {created}")
    if updated:  text_parts.append(f"Обновлено: {updated}")
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
    """Alias на chat-first форму ответа на RFQ. Раньше вёл на /seller/requests/<id>/
    (старый кабинет удалён) — теперь делегирует в respond_rfq_form."""
    from .negotiation import respond_rfq_form
    return respond_rfq_form(params, user, role)


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

def _split_order_by_operator(order):
    """PIVOT 2026-05-27: разбиение заказа на sub-orders по операторам.

    Группирует OrderItem'ы по part.seller.profile.assigned_operator.
    Если все позиции у одного оператора (или ни у кого) — sub-orders не
    создаются, заказ ведёт единственный owner.

    Если у >1 операторов — создаются N sub-orders (по 1 на оператора),
    каждый с parent_order=original, is_sub_order=True, assigned_operator=op.
    Items НЕ дублируются — sub-order это просто пойнтер для фильтрации.

    Возвращает list[Order] (созданные sub-orders) или [] если нет split.
    """
    from marketplace.models import Order, UserProfile
    from collections import defaultdict
    # Группируем items по operator-owner
    groups = defaultdict(list)  # operator_id -> [items]
    for it in order.items.select_related("part__seller").all():
        if not it.part or not it.part.seller_id:
            continue
        # Найдём assigned_operator поставщика
        prof = UserProfile.objects.filter(user_id=it.part.seller_id).only(
            "assigned_operator_id").first()
        op_id = prof.assigned_operator_id if prof else None
        groups[op_id].append(it)

    # Убираем None-группу из split (это позиции без operator-owner, ведёт лид)
    op_groups = {k: v for k, v in groups.items() if k is not None}

    # 1 оператор или 0 → split не нужен. Просто проставим assigned_operator
    if len(op_groups) <= 1:
        if op_groups:
            only_op_id = next(iter(op_groups.keys()))
            order.assigned_operator_id = only_op_id
            order.save(update_fields=["assigned_operator"])
        return []

    # >=2 операторов → создаём sub-orders для каждого
    sub_orders = []
    from decimal import Decimal as _D
    for op_id, items in op_groups.items():
        sub_total = sum((_D(str(it.unit_price or 0)) * (it.quantity or 1) for it in items), _D("0"))
        sub = Order.objects.create(
            customer_name=order.customer_name,
            customer_email=order.customer_email,
            customer_phone=order.customer_phone,
            delivery_address=order.delivery_address,
            buyer=order.buyer,
            status="pending",
            payment_status=order.payment_status,
            payment_scheme=order.payment_scheme,
            reserve_percent=order.reserve_percent,
            reserve_amount=(sub_total * (order.reserve_percent or _D("10")) / _D("100")).quantize(_D("0.01")),
            total_amount=sub_total,
            assigned_operator_id=op_id,
            parent_order=order,
            is_sub_order=True,
            shipping_mode=order.shipping_mode,
            incoterm=order.incoterm,
        )
        sub_orders.append(sub)
    return sub_orders


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
        # Финальная спецификация перед списанием с депозита.
        # Используем тот же spec_results-рендер с таблицей колонок:
        # # | OEM | Название | Бренд | Цена | Кол-во | Вес | Сумма | ETA | Origin
        _FLAG_FROM_PORT = {
            "🇦🇪":"🇦🇪","🇨🇳":"🇨🇳","🇹🇷":"🇹🇷","🇳🇱":"🇳🇱","🇰🇿":"🇰🇿",
            "🇷🇺":"🇷🇺","🇩🇪":"🇩🇪","🇺🇸":"🇺🇸","🇵🇰":"🇵🇰","🇪🇸":"🇪🇸",
            "🇯🇵":"🇯🇵","🇰🇷":"🇰🇷","🇮🇳":"🇮🇳","🇹🇭":"🇹🇭","🇲🇾":"🇲🇾",
        }
        def _flag(port_str):
            if not port_str: return "🌍"
            for f in _FLAG_FROM_PORT:
                if f in port_str:
                    return f
            return "🌍"
        preview_total = Decimal("0")
        total_weight = Decimal("0")
        max_eta = 0
        spec_items = []
        for p in parts[:30]:
            line_total = Decimal(str(p.price or 0)) * quantity
            preview_total += line_total
            line_weight = Decimal(str(p.gross_weight_kg or 0)) * quantity
            total_weight += line_weight
            eta_days = (getattr(p, "production_lead_days", 0) or 0) \
                     + (getattr(p, "prep_to_ship_days", 0) or 0) \
                     + (getattr(p, "shipping_lead_days", 0) or 0)
            if eta_days > max_eta:
                max_eta = eta_days
            origin_str = p.sea_port or p.air_port or ""
            flag = _flag(origin_str)
            spec_items.append({
                "status": "in_stock",
                "id": p.oem_number or f"#{p.id}",
                "name": _clean_title(p.title or "") or "—",
                "name_ru": p.title_ru or "",
                "brand": (p.brand.name if p.brand_id else "—"),
                "condition": p.condition or "oem",
                "price": float(p.price or 0),
                "qty": quantity,
                "weight": f"{float(line_weight):.2f} кг" if line_weight else "—",
                "currency": "USD",
                # Дополнительные поля для рендера (см. изменения в chat-first.js)
                "line_total": float(line_total),
                "origin_flag": flag,
                "eta_days": eta_days,
            })
        reserve = preview_total * Decimal("0.1")
        text_lines = [
            "📦 Подтвердите заказ — это финальная спецификация перед списанием с депозита.",
            "",
            f"После клика «Подтвердить»: спишется резерв 10% (${float(reserve):,.0f}), " +
            "оператор подберёт маршрут доставки, статус заказа → «формируется».",
        ]
        return ActionResult(
            text="\n".join(text_lines),
            cards=[{
                "type": "spec_results",
                "data": {
                    "title": f"Спецификация заказа · {len(parts)} позиций",
                    "found": len(parts),
                    "analogue": 0,
                    "not_found": 0,
                    "items": spec_items,
                    "offers_count": len(parts),
                    "sellers_count": len(parts),
                    "best_mix": int(preview_total),
                    "total": int(preview_total),
                    "currency": "USD",
                    "foot_info": (
                        f"Сумма товара (EXW): ${float(preview_total):,.0f}"
                        + (f" · вес ~{float(total_weight):.1f} кг" if total_weight else "")
                        + (f" · срок ~{max_eta} дн" if max_eta else "")
                    ),
                },
            }],
            actions=[
                {"label": f"✓ Подтвердить и зарезервировать 10% (${float(reserve):,.0f})",
                 "action": "quick_order",
                 "params": {**params, "confirmed": True},
                 "style": "primary"},
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
        clearance_fee,
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
                        "vat": Decimal("0"), "last_mile": Decimal("0"),
                        "clearance": Decimal("0")}
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
    # Таможенное оформление — ОДИН фикс-сбор на отправку (брокер+терминал),
    # только DDP и только если есть что отправлять.
    if ship_total > 0:
        cl = clearance_fee(dest_country, chosen_inc)
        if cl > 0:
            ship_components["clearance"] += cl
            ship_total += cl
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
    # Парсим country code из emoji-флага в строке порта (IATA-коды RKT/PKX
    # не следуют UN/LOCODE-префиксу страны, поэтому нельзя полагаться на
    # _country_from_port). Тот же фикс что в _search_articles_list.
    _FLAG_TO_CC_QO = {
        "🇦🇪":"AE","🇨🇳":"CN","🇹🇷":"TR","🇳🇱":"NL","🇰🇿":"KZ","🇷🇺":"RU",
        "🇩🇪":"DE","🇺🇸":"US","🇵🇰":"PK","🇪🇸":"ES","🇯🇵":"JP","🇰🇷":"KR",
        "🇮🇳":"IN","🇹🇭":"TH","🇲🇾":"MY",
    }
    def _cc_from_port_str(s, code):
        if s:
            for f, c in _FLAG_TO_CC_QO.items():
                if f in s:
                    return c
        return _country_from_port(code) or code[:2].upper()

    for p in parts:
        origin = ((getattr(p, port_field, "") or "").strip())
        origin_code = origin.split()[0] if origin else ""
        if not origin_code:
            continue
        cc = _cc_from_port_str(origin, origin_code)
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
    # схлопываем по стране для UI. Tariff lookup: сначала по port code, потом
    # по country code как fallback.
    for (cc, origin_code), chargeables in parts_by_origin.items():
        t = LogisticsTariff.objects.filter(
            origin_port__iexact=origin_code, dest_country=dest_country,
            mode=eff_mode, is_active=True,
        ).first()
        if not t and cc:
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
    # Лента важных событий админа: новая сделка + IP/кабинет/позиции.
    _log_activity("order", actor=user, ip=params.get("_client_ip", ""),
                  title=f"Заказ #{order.id} · {len(parts)} поз · ${float(landed_total):,.0f}",
                  meta={"order_id": order.id, "n_items": len(parts),
                        "amount": float(landed_total), "currency": "USD",
                        "items": [{"oem": p.oem_number,
                                   "name": _clean_title(p.title) or p.oem_number,
                                   "qty": quantity, "price": float(p.price or 0)}
                                  for p in parts[:20]]})

    # PIVOT 2026-05-27: split на sub-orders по operator-ownership.
    # Parent сохраняет ВСЕ items (видим покупателю как один заказ).
    # Sub-orders создаются для каждого уникального оператора → каждый
    # оператор видит только свою часть через фильтр items по своему ownership.
    try:
        _split_order_by_operator(order)
    except Exception:
        logger.exception("split_order_by_operator failed for order %s", order.id)
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

    # ── Чистое сообщение для buyer: только 3 ключевых вопроса ────────
    # 1. Деньги: сейчас списано / на депозите / к доплате
    # 2. Статус: текущий шаг / следующие шаги / срок
    # 3. Действия: написать оператору / детали / отменить
    # Технический «состав отправки» (порты, коносаменты, фрахт по статьям)
    # передаётся в card.data.advanced — для оператора и для разворачиваемой
    # секции «подробности» (если юзер захочет копнуть).
    mode_counts = {}
    for _, m, _, _ in ship_breakdown:
        mode_counts[m] = mode_counts.get(m, 0) + 1
    mode_label_map = {"sea": "морем", "air": "авиа", "auto": "авто"}
    primary_mode = max(mode_counts, key=mode_counts.get) if mode_counts else (chosen_mode or "sea")
    mode_word = mode_label_map.get(primary_mode, "морем")
    remaining_amount = landed_total - reserve_amount  # 90% к доплате после отгрузки
    # ETA зависит от базиса Incoterm:
    # FOB — ответственность платформы заканчивается на порту отгрузки
    #       (передаём груз там → дальше покупатель/его форвардер сам).
    #       Регламент платформы — 3 рабочих дня от резерва до готовности в порту.
    # CIP — платформа доставляет до порта прибытия покупателя
    #       (фрахт port-to-port + страховка).
    # DDP — all-in до двери (фрахт + страховка + таможня + last mile).
    if chosen_inc == "FOB":
        eta_min, eta_max = 3, 5  # «3 дня по регламенту» + люфт
        eta_label = "до готовности в порту отгрузки"
    elif chosen_inc == "CIP":
        if primary_mode == "air":
            eta_min, eta_max = 7, 14
        elif primary_mode == "auto":
            eta_min, eta_max = 10, 20
        else:
            eta_min, eta_max = 25, 40
        eta_label = "до вашего порта прибытия"
    else:  # DDP
        if primary_mode == "air":
            eta_min, eta_max = 14, 25
        elif primary_mode == "auto":
            eta_min, eta_max = 18, 30
        else:
            eta_min, eta_max = 35, 60
        eta_label = "до двери (all-in)"

    text_lines = [
        f"✓ Заказ #{order.id} принят",
        f"{len(parts)} позиций · доставка {mode_word} · базис {chosen_inc}",
    ]
    if not enough:
        text_lines.append(f"⚠️ Недостаточно на депозите — пополните на ${reserve_amount - wallet.balance:,.0f}.")

    return ActionResult(
        text="\n".join(text_lines),
        cards=[{
            "type": "order_confirm",
            "data": {
                "id": str(order.id),
                "number": order.id,
                "status": "pending",
                "items_count": len(parts),
                "total": float(landed_total),
                "currency": "USD",
                "shipping_mode": primary_mode,
                "shipping_mode_label": mode_word,
                "incoterm": chosen_inc,
                # МОНЕЙ-блок
                "money": {
                    "reserve_now": float(reserve_amount),
                    "reserve_pct": 10,
                    "wallet_balance": float(wallet.balance),
                    "wallet_enough": enough,
                    "remaining_to_pay": float(remaining_amount),
                    "remaining_when": "после отгрузки от поставщика",
                },
                # СТАТУС-блок (timeline) — зависит от базиса Incoterm.
                # FOB: ответственность платформы → порт отгрузки (3 дня).
                # CIP: + транзит до порта прибытия покупателя.
                # DDP: + таможня + last mile до двери (all-in).
                "status_steps": (
                    [
                        {"label": "Резерв 10%",       "state": "current" if enough else "pending"},
                        {"label": "Оператор связывается с поставщиком", "state": "next"},
                        {"label": "Подтверждение поставщика", "state": "next"},
                        {"label": "Подготовка и доставка в порт отгрузки", "state": "next"},
                        {"label": "Готов к передаче в порту (ваш форвардер забирает)", "state": "next"},
                    ] if chosen_inc == "FOB" else
                    [
                        {"label": "Резерв 10%",       "state": "current" if enough else "pending"},
                        {"label": "Оператор связывается с поставщиком", "state": "next"},
                        {"label": "Подтверждение поставщика", "state": "next"},
                        {"label": "Подготовка и отгрузка",  "state": "next"},
                        {"label": "Транзит до вашего порта", "state": "next"},
                        {"label": "Прибытие в порт назначения", "state": "next"},
                    ] if chosen_inc == "CIP" else
                    [
                        {"label": "Резерв 10%",       "state": "current" if enough else "pending"},
                        {"label": "Оператор связывается с поставщиком", "state": "next"},
                        {"label": "Подтверждение поставщика", "state": "next"},
                        {"label": "Подготовка и отгрузка",  "state": "next"},
                        {"label": "Транзит",                 "state": "next"},
                        {"label": "Таможенное оформление",   "state": "next"},
                        {"label": "Доставка до двери",        "state": "next"},
                    ]
                ),
                "eta_min_days": eta_min,
                "eta_max_days": eta_max,
                "eta_label": eta_label,
                # Сворачиваемый advanced-блок: тех-детали для любопытных
                "advanced": {
                    "items_subtotal": float(total),
                    "shipping_total": float(ship_total),
                    "shipping_missing": ship_missing,
                    "origin_breakdown": origin_breakdown,
                    "components": {
                        "freight":      float(ship_components.get("freight",      Decimal("0"))),
                        "carriage_ext": float(ship_components.get("carriage_ext", Decimal("0"))),
                        "insurance":    float(ship_components.get("insurance",    Decimal("0"))),
                        "duty":         float(ship_components.get("duty",         Decimal("0"))),
                        "vat":          float(ship_components.get("vat",          Decimal("0"))),
                        "last_mile":    float(ship_components.get("last_mile",    Decimal("0"))),
                        "clearance":    float(ship_components.get("clearance",    Decimal("0"))),
                    },
                },
            },
        }],
        actions=(
            # confirmed=True пропускает повторный draft-экран в pay_reserve.
            # Юзер уже видел order_confirm со всей инфой о деньгах — второе
            # подтверждение «Списать $X · депозит сейчас / после» дублирует
            # тот же контекст и раздражает.
            [{"label": f"💳 Списать ${reserve_amount:,.0f} из депозита",
              "action": "pay_reserve",
              "params": {"order_id": order.id, "confirmed": True},
              "style": "primary"}]
            if enough else
            [{"label": "Пополнить депозит",
              "action": "topup_wallet",
              "params": {"amount": float(max(reserve_amount * 5, Decimal("10000"))),
                          "pending_order_id": order.id},
              "style": "primary"}]
        ) + [
            {"label": "💬 Написать оператору", "action": "ask_operator",
             "params": {"order_id": order.id}},
            {"label": "Отменить заказ", "action": "cancel_order",
             "params": {"order_id": order.id}, "style": "danger"},
        ],
        suggestions=["Статус заказа", "Баланс депозита", "Все мои заказы"],
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
            f"✓ Базис заказа #{order.id} обновлён: {mode_label} · {inc}\n"
            f"Товары: ${items_total:,.2f} · Доставка ({inc}): ${ship_total:,.2f}\n"
            f"Итого landed: ${landed:,.2f} · резерв 10%: ${reserve:,.2f}"
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
            f"✓ Базис заказа #{order.id} обновлён: {mode_label} · {inc}\n"
            f"Товары: ${items_total:,.2f} · Доставка ({inc}): ${ship_total:,.2f}\n"
            f"Итого landed: ${landed:,.2f} · резерв 10%: ${reserve:,.2f}"
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

    # Реферал: первый оплаченный резерв приглашённого → $100 пригласившему.
    try:
        from . import referral as _ref
        _ref.on_order_reserve_paid(order)
    except Exception:
        pass

    # AI-кредиты: покупка (оплаченный резерв) обновляет лимит бесплатных
    # AI-запросов покупателя — «совершил покупку → лимит пополнился».
    try:
        from . import ai_credits as _aic
        _aic.grant_on_purchase(user)
    except Exception:
        pass

    return ActionResult(
        text=(
            f"✓ Списано ${amount:,.2f} с депозита по заказу #{order.id}.\n"
            f"Остаток на счёте: ${wallet.balance:,.2f} {wallet.currency}.\n"
            f"Заказ передан поставщику в производство. Следующий платёж — "
            f"после готовности к отгрузке."
        ),
        cards=_full_order_cards(order, user, role, fallback={
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
        }),
        actions=[
            {"label": "📦 Трекинг", "action": "track_shipment",
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


def shipment_flow(incoterm: str):
    """Этапы отгрузки, которые РЕАЛЬНО ведёт платформа по базису поставки.

    FOB — до передачи в порту отгрузки (дальше транзит/таможню/доставку
    организует сам покупатель); CIP — до прибытия в порт назначения (таможня
    и последняя миля — покупатель); DDP — полный цикл до двери.

    Каждый этап: (label, plan_days, done_code, fact_start, fact_end).
    done_code == "pay" → готов по оплате резерва; иначе этап done, когда индекс
    статуса заказа >= индекса done_code в TRACKING_STAGES.
    """
    off = {c: d for c, _, d in TRACKING_STAGES}
    inc = (incoterm or "DDP").upper()
    if inc == "FOB":
        return [
            ("Резерв оплачен",  off["reserve_paid"],                        "pay",            "pending",       "reserve_paid"),
            ("В производстве",  off["ready_to_ship"] - off["reserve_paid"], "in_production",  "reserve_paid",  "ready_to_ship"),
            ("Передан в порту", 2,                                          "transit_abroad", "ready_to_ship", "transit_abroad"),
        ]
    if inc == "CIP":
        return [
            ("Резерв оплачен",  off["reserve_paid"],                          "pay",            "pending",        "reserve_paid"),
            ("В производстве",  off["ready_to_ship"] - off["reserve_paid"],   "in_production",  "reserve_paid",   "ready_to_ship"),
            ("Транзит",         off["transit_abroad"] - off["ready_to_ship"], "transit_abroad", "ready_to_ship",  "transit_abroad"),
            ("Прибыл в порт",   off["customs"] - off["transit_abroad"],       "customs",        "transit_abroad", "customs"),
        ]
    # DDP (и дефолт) — полный цикл до двери
    return [
        ("Резерв оплачен", off["reserve_paid"],                        "pay",          "pending",       "reserve_paid"),
        ("В производстве", off["ready_to_ship"] - off["reserve_paid"], "in_production","reserve_paid", "ready_to_ship"),
        ("Транзит",        off["customs"] - off["ready_to_ship"],      "customs",      "ready_to_ship", "customs"),
        ("Таможня",        off["transit_rf"] - off["customs"],         "transit_rf",   "customs",       "transit_rf"),
        ("Доставлен",      off["delivered"] - off["transit_rf"],       "delivered",    "transit_rf",    "delivered"),
    ]


def _log_event(order, event_type: str, actor=None, source="system", meta=None):
    from marketplace.models import OrderEvent
    try:
        OrderEvent.objects.create(
            order=order, event_type=event_type, source=source,
            actor=actor, meta=meta or {},
        )
    except Exception:
        logger.exception("OrderEvent create failed")


def _log_activity(kind: str, *, actor=None, ip: str = "", title: str = "", meta=None):
    """Лента важных событий админа (контроль/безопасность): пишет ActivityEvent
    + лёгкий realtime WS-пуш онлайн-админам.

    НЕ шлёт email/telegram (это поток событий, а не алерт). Best-effort: ошибка
    логирования НЕ ломает основную операцию (заказ/RFQ/импорт).
    """
    from marketplace.models import ActivityEvent
    role = ""
    try:
        role = getattr(getattr(actor, "userprofile", None)
                       or getattr(actor, "profile", None), "role", "") or ""
    except Exception:
        role = ""
    act = actor if (actor is not None and getattr(actor, "is_authenticated", False)) else None
    try:
        ActivityEvent.objects.create(
            kind=kind, actor=act, actor_role=role,
            ip=(ip or "")[:64], title=(title or "")[:255], meta=meta or {},
        )
    except Exception:
        logger.exception("ActivityEvent create failed")
        return
    # Realtime: лёгкий WS-бейдж онлайн-админам (= суперюзеры). Без email/telegram.
    try:
        from django.contrib.auth import get_user_model
        from .consumers import push_notification_to_user
        admin_ids = list(get_user_model().objects
                         .filter(is_superuser=True, is_active=True)
                         .values_list("id", flat=True)[:20])
        for aid in admin_ids:
            push_notification_to_user(aid, {
                "kind": "activity",
                "title": ("🆕 " + (title or "Новое событие"))[:120],
                "body": "", "url": "",
            })
    except Exception:
        logger.exception("activity admin push failed")


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
    # FIX (CRITICAL): не удаляем заказ — переводим в cancelled, чтобы сохранить
    # audit trail, возможность отслеживать возвраты и финансовую сверку.
    order.status = "cancelled"
    order.payment_status = "cancelled" if hasattr(order, "payment_status") else order.payment_status
    order.save(update_fields=["status"] + (["payment_status"] if hasattr(order, "payment_status") else []))
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
    # FIX (CRITICAL): сохраняем заказ с status='cancelled' — нужен audit-trail
    # и возможность отслеживать возвраты. Раньше order.delete() терял историю.
    order.status = "cancelled"
    if hasattr(order, "payment_status") and order.payment_status == "awaiting_reserve":
        order.payment_status = "cancelled"
    order.save(update_fields=["status"] + (["payment_status"] if hasattr(order, "payment_status") else []))
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
                # Продавец не видит покупателя (анти-сговор) — обезличено.
                "buyer": "Покупатель",
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
            actions=[{"label": "📦 Трекинг", "action": "track_shipment",
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
                f"Отгрузка заказа #{order.id} "
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

    # Полная карточка заказа (таблица позиций) вместо минимальной.
    _detail = get_order_detail({"order_id": order.id}, user, role)
    _cards = _detail.cards or [{
        "type": "order",
        "data": {
            "id": str(order.id), "number": order.id,
            "status": "transit_abroad",
            "status_label": f"Транзит · {carrier} · {tracking}",
            "total": float(order.total_amount), "currency": "USD",
            "payment_status_label": order.get_payment_status_display(),
        },
    }]
    return ActionResult(
        text=(
            f"🚚 Заказ #{order.id} отгружен.\n"
            f"Tracking: {tracking} · Перевозчик: {carrier}.\n"
            f"Покупатель уведомлён, статус — «Транзит (зарубеж)»."
        ),
        cards=_cards,
        actions=[
            {"label": "📦 Трекинг", "action": "track_shipment",
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
    # Нечисловой order_id → не валим int()-кастом в seller-ветке (OrderItem.filter
    # вне try). Покрываем buyer/seller/operator единой проверкой до ветвления.
    try:
        order_id = int(order_id)
    except (ValueError, TypeError):
        return ActionResult(text=f"Заказ #{order_id} не найден.")
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

    # ── Per-supplier / per-shipment breakdown ───────────────────
    # Если по заказу есть физические партии (Shipment) — рисуем их.
    # Если нет — группируем по поставщику (виртуальные мини-таймлайны).
    parts_data = []
    total_amt = float(order.total_amount or 0) or 1.0
    is_real_op = role in ("operator", "admin") and getattr(user, "is_staff", False)
    _ORDER_CODES = [c for c, _, _ in TRACKING_STAGES]

    shipments = list(order.shipments.prefetch_related("items__part__seller").all()) \
                if hasattr(order, "shipments") else []
    if shipments:
        # Какие item.id уже в каком-то shipment'е
        in_shipment_ids = set()
        for sh in shipments:
            for it in sh.items.all():
                in_shipment_ids.add(it.id)
        # Партии (реальные Shipment'ы)
        for idx, sh in enumerate(sorted(shipments, key=lambda s: -float(s.total_amount or 0))):
            its = list(sh.items.all())
            sup_ids = {(it.part.seller_id if it.part else 0) for it in its}
            sup_names = []
            for it in its:
                if it.part and it.part.seller_id:
                    try:
                        sup_names.append(it.part.seller.username)
                    except Exception:
                        sup_names.append(f"#S{it.part.seller_id}")
            sup_name_for_display = (", ".join(sorted(set(sup_names))) if sup_names else "—") \
                                    if (is_real_op or role == "seller") \
                                    else f"Партия {idx + 1}"
            kind_lbl = sh.get_kind_display()
            amt = float(sh.total_amount or 0)
            st = sh.status
            st_idx = TRACKING_INDEX.get(st, 0)
            st_lbl = TRACKING_STAGES[st_idx][1] if st_idx < len(TRACKING_STAGES) else st
            parts_data.append({
                "supplier": f"{sup_name_for_display} · {kind_lbl}",
                "amount": amt,
                "amount_pct": int(round(amt / total_amt * 100)) if total_amt else 0,
                "items_count": len(its),
                "current_idx": st_idx,
                "current_label": st_lbl,
                "total_stages": len(TRACKING_STAGES),
                "progress_pct": int(round(st_idx / max(1, len(TRACKING_STAGES) - 1) * 100)),
                "mixed": False,
                "shipment_id": sh.id,
                "shipment_kind": sh.kind,
                "tracking_number": sh.tracking_number if (sh.tracking_number and is_real_op) else None,
                "carrier": sh.carrier if (sh.carrier and is_real_op) else None,
            })
        # Остаток: позиции БЕЗ shipment'а (ещё не оформлены в партию)
        leftover = [it for it in order.items.select_related("part__seller")
                    if it.id not in in_shipment_ids]
        if leftover:
            # Группируем остаток по поставщику для наглядности
            from collections import defaultdict as _dd
            left_groups = _dd(lambda: {"items": [], "amount": 0.0, "statuses": set(), "name": ""})
            for it in leftover:
                sid = it.part.seller_id if it.part else 0
                g = left_groups[sid]
                if not g["name"]:
                    g["name"] = (it.part.seller.username if (it.part and it.part.seller_id) else "—")
                g["items"].append(it)
                g["amount"] += float(it.unit_price or 0) * (it.quantity or 0)
                g["statuses"].add((it.status if hasattr(it, "status") and it.status else None) or order.status)
            base_idx = len(parts_data)
            for j, (sid, g) in enumerate(sorted(left_groups.items(), key=lambda kv: -kv[1]["amount"])):
                sts = sorted(g["statuses"])
                worst = min(sts, key=lambda s: _ORDER_CODES.index(s) if s in _ORDER_CODES else 99)
                worst_idx = TRACKING_INDEX.get(worst, 0)
                worst_lbl = TRACKING_STAGES[worst_idx][1] if worst_idx < len(TRACKING_STAGES) else worst
                disp = g["name"] if (is_real_op or role == "seller") else f"Поставщик {chr(ord('A') + base_idx + j)}"
                parts_data.append({
                    "supplier": f"{disp} · ждёт партию",
                    "amount": g["amount"],
                    "amount_pct": int(round(g["amount"] / total_amt * 100)) if total_amt else 0,
                    "items_count": len(g["items"]),
                    "current_idx": worst_idx,
                    "current_label": worst_lbl,
                    "total_stages": len(TRACKING_STAGES),
                    "progress_pct": int(round(worst_idx / max(1, len(TRACKING_STAGES) - 1) * 100)),
                    "mixed": len(sts) > 1,
                })
    else:
        # Нет Shipment'ов → группируем по поставщику (виртуальный режим).
        from collections import defaultdict as _dd
        _sup_groups = _dd(lambda: {"items": [], "amount": 0.0, "statuses": set(), "name": ""})
        for it in order.items.select_related("part", "part__seller"):
            sid = it.part.seller_id if it.part else 0
            g = _sup_groups[sid]
            if not g["name"]:
                g["name"] = (it.part.seller.username if (it.part and it.part.seller_id) else "—")
            g["items"].append(it)
            g["amount"] += float(it.unit_price or 0) * (it.quantity or 0)
            g["statuses"].add((it.status if hasattr(it, "status") and it.status else None) or order.status)
        if len(_sup_groups) > 1:
            for idx, (sid, g) in enumerate(sorted(_sup_groups.items(), key=lambda kv: -kv[1]["amount"])):
                sts = sorted(g["statuses"])
                worst = min(sts, key=lambda s: _ORDER_CODES.index(s) if s in _ORDER_CODES else 99)
                worst_idx = TRACKING_INDEX.get(worst, 0)
                worst_lbl = TRACKING_STAGES[worst_idx][1] if worst_idx < len(TRACKING_STAGES) else worst
                display_name = g["name"] if (is_real_op or role == "seller") else f"Поставщик {chr(ord('A') + idx)}"
                parts_data.append({
                    "supplier": display_name,
                    "amount": g["amount"],
                    "amount_pct": int(round(g["amount"] / total_amt * 100)),
                    "items_count": len(g["items"]),
                    "current_idx": worst_idx,
                    "current_label": worst_lbl,
                    "total_stages": len(TRACKING_STAGES),
                    "progress_pct": int(round(worst_idx / max(1, len(TRACKING_STAGES) - 1) * 100)),
                    "mixed": len(sts) > 1,
                })

    # ── Алерт о расхождении: кто-то уехал, кто-то ещё в производстве ──
    # Не показываем если split уже принят (split-shipment'ы существуют
    # → расхождение это уже норма для этого заказа).
    divergence_alert = None
    _split_already = any(getattr(p, "get", lambda *_: None)("shipment_kind") == "split"
                         for p in parts_data)
    if len(parts_data) >= 2 and not _split_already:
        READY_IDX = TRACKING_INDEX.get("ready_to_ship", 4)
        ahead  = [p for p in parts_data if p["current_idx"] >  READY_IDX]
        behind = [p for p in parts_data if p["current_idx"] <  READY_IDX]
        if ahead and behind:
            ahead_names  = ", ".join(p["supplier"] for p in ahead)
            behind_names = ", ".join(p["supplier"] for p in behind)
            slowest = min(behind, key=lambda p: p["current_idx"])
            divergence_alert = {
                "level": "warn",
                "title": "⚠️ Расхождение по поставщикам — консолидация под угрозой",
                "body": (f"{ahead_names} уже {ahead[0]['current_label'].lower()}, "
                         f"а {behind_names} ещё на стадии «{slowest['current_label']}». "
                         f"Платформа по умолчанию ждёт всех для единой отправки "
                         f"(один коносамент, одна таможня). Если ждать не хотите — "
                         f"можно отгрузить готовых отдельной партией (дороже логистика, но быстрее)."),
                "cta_consolidate": {
                    "label": f"📦 Ждать {behind_names} (консолидация)",
                    "action": "consolidate_wait",
                    "params": {"order_id": order.id},
                },
                "cta_split": {
                    "label": f"✈️ Отгрузить {ahead_names} сейчас (split)",
                    "action": "split_shipment",
                    "params": {"order_id": order.id},
                },
            }

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
                # Per-supplier разбивка — поставщики двигают позиции независимо.
                # Передаём массив, фронт рисует N мини-таймлайнов внутри карточки.
                "parts": parts_data,
                # Алерт когда консолидация ломается
                "divergence_alert": divergence_alert,
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
        cards=_full_order_cards(order, user, role, fallback={
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
        }),
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
                {"label": "📦 Трекинг", "action": "track_shipment",
                 "params": {"order_id": order.id}},
                {"label": "🚚 К отгрузке", "action": "seller_pipeline", "params": {}},
            ],
            suggestions=["Что отгрузить?", "Очередь продавца"],
        )

    old_status = order.status
    new_status, label = transitions[order.status]
    order.status = new_status
    order.save(update_fields=["status"])
    # FIX (HIGH): source=role а не hardcoded "buyer" — advance_order вызывается
    # buyer/seller/operator, audit-trail должен показывать настоящего actor'a.
    _log_event(order, "status_changed", actor=user, source=role or "buyer",
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
            {"label": "📦 Трекинг",         "action": "track_shipment",
             "params": {"order_id": order.id}},
        ]

    next_actions.append({"label": "📦 Трекинг", "action": "track_shipment",
                         "params": {"order_id": order.id}})

    # Возвращаем ПОЛНУЮ карточку заказа (таблица всех позиций), а не минимальную:
    # продавец/оператор должны видеть состав заказа целиком, а не только номер и сумму.
    _detail = get_order_detail({"order_id": order.id}, user, role)
    _cards = _detail.cards or [{
        "type": "order",
        "data": {
            "id": str(order.id), "number": order.id, "status": new_status,
            "status_label": label, "total": float(order.total_amount),
            "currency": "USD",
            "payment_status_label": order.get_payment_status_display(),
        },
    }]
    return ActionResult(
        text=f"✓ Заказ #{order.id} → «{label}».{next_text}",
        cards=_cards,
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
            actions=[{"label": "📦 Трекинг", "action": "track_shipment",
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
                # AI-кредиты: продавцу +50 запросов за завершённую продажу
                try:
                    from . import ai_credits as _aic
                    _aic.grant_on_sale(seller)
                except Exception:
                    logger.exception("ai_credits grant_on_sale failed")
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
        cards=_full_order_cards(order, user, role, fallback={
            "type": "order",
            "data": {
                "id": str(order.id), "number": order.id,
                "status": "completed", "status_label": "Завершён",
                "total": float(order.total_amount), "currency": "USD",
                "payment_status_label": order.get_payment_status_display(),
            },
        }),
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
                    "next_label":   f"Уровня {next_level}",
                }
                next_label_text = f"Уровня {next_level}"
                break

    text_lines = [
        f"💰 Ваш годовой объём: ${bvy.volume_usd:,.0f}",
        f"Текущий уровень: {LEVEL_NAMES[bvy.level]} · скидка {bvy.discount_pct}%",
    ]
    if progress and progress.get("gap_text"):
        text_lines.append(f"До {next_label_text}: {progress['gap_text'].lower()} оборота.")

    return ActionResult(
        text="\n".join(text_lines),
        cards=[{
            "type": "tier_progress",
            "data": {
                "title":   "Авто-скидка",
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


def _operator_bonus_view(params, user, role):
    """Экран бонусов оператора: на счёте + в холде + life-time."""
    from .models import Wallet
    from marketplace.models import OperatorBonusLine
    from datetime import timedelta
    from django.utils import timezone as _tz
    wallet = Wallet.for_user(user)
    lines = list(OperatorBonusLine.objects.filter(operator=user).select_related("order")[:20])
    now = _tz.now()
    cutoff_30 = now - timedelta(days=30)

    bonus_pending  = sum(float(l.amount) for l in lines if l.status == "pending")
    bonus_30d      = sum(float(l.amount) for l in OperatorBonusLine.objects.filter(
                          operator=user, status="released", released_at__gte=cutoff_30))
    bonus_lifetime = sum(float(l.amount) for l in OperatorBonusLine.objects.filter(
                          operator=user, status="released"))

    # Сделок закрыто за 30д
    closed_30 = OperatorBonusLine.objects.filter(
        operator=user, created_at__gte=cutoff_30,
    ).count()

    # Строки последних начислений в формате wal-row
    rows = []
    for l in lines[:10]:
        status_lbl = {
            "pending":  "в холде",
            "released": "зачислено",
            "withheld": "удержано",
            "reduced":  "−50% за рекламацию",
        }.get(l.status, l.status)
        rows.append({
            "left":   f"#{l.order_id}",
            "title":  f"{l.basis} {l.rate_pct}% · {status_lbl}",
            "amount": f"+${float(l.amount):,.0f}",
            "action": "op_order_detail",
            "params": {"order_id": l.order_id},
        })

    return ActionResult(
        text=f"💰 На счёте ${wallet.balance:,.2f} · в холде ${bonus_pending:,.0f}",
        cards=[{
            "type": "ops_dashboard",
            "data": {
                "hero_label": "На счёте",
                "hero_value": float(wallet.balance or 0),
                "currency":   wallet.currency,
                "stats": [
                    {"label": "В холде (14 дней)",
                     "value": f"${bonus_pending:,.0f}",
                     "sub":   "ждут release"},
                    {"label": "Зачислено за 30 дней",
                     "value": f"${bonus_30d:,.0f}",
                     "sub":   f"{closed_30} сделок закрыто",
                     "tone":  "ok"},
                    {"label": "Заработано всего",
                     "value": f"${bonus_lifetime:,.0f}",
                     "sub":   "life-time"},
                ],
                "rows_title": "Последние начисления",
                "rows":       rows,
            },
        }],
        actions=[
            {"label": "📤 Запрос выплаты",  "action": "request_payout", "params": {}},
            {"label": "📦 Мои сделки",       "action": "get_orders",     "params": {}},
        ],
        suggestions=["Как считается бонус?", "Когда придёт зарплата?"],
    )


def _seller_revenue_view(params, user, role):
    """Экран выручки продавца: что зачислено, что в работе, что ожидается."""
    from .models import Wallet
    from .seller_actions import _effective_seller
    from marketplace.models import OrderItem, Order
    user = _effective_seller(user)
    wallet = Wallet.for_user(user)

    # Все order items с part от этого продавца
    items = OrderItem.objects.filter(part__seller=user).select_related("order")
    # Группируем суммы по payment_status заказа
    revenue_paid     = 0.0   # уже зачислено (paid/completed/delivered)
    revenue_in_work  = 0.0   # в производстве/транзите (резерв оплачен, ждём 90%)
    revenue_pending  = 0.0   # ждём оплату резерва
    open_orders = []
    for it in items:
        o = it.order
        if not o: continue
        sub = float(it.quantity or 1) * float(it.unit_price or 0)
        ps = o.payment_status or ""
        st = o.status or ""
        if ps == "paid" or st in ("completed", "delivered"):
            revenue_paid += sub
        elif ps in ("reserve_paid", "mid_paid", "customs_paid"):
            revenue_in_work += sub
            open_orders.append((o, it, sub, "в работе"))
        elif ps == "awaiting_reserve":
            revenue_pending += sub
            open_orders.append((o, it, sub, "ждём оплату"))

    # Последние поступления — последние 8 транзакций
    txs = list(wallet.transactions.all()[:8])
    import re as _re
    def _clean(s):
        s = s or ""
        s = _re.sub(r"\s*\(intent[^)]*\)", "", s)
        s = _re.sub(r"\s*\[chat-demo\]", "", s)
        return s.strip()
    tx_rows = []
    for tx in txs:
        kind = tx.kind or ""
        is_in = kind in ("topup", "refund", "payout")
        order_ref = tx.order_id
        if not order_ref:
            m = _re.search(r"#(\d+)", tx.description or "")
            order_ref = int(m.group(1)) if m else None
        desc = _clean(tx.description) or tx.get_kind_display()
        if order_ref:
            desc = _re.sub(r"\s*#\d+\s*", " ", desc).strip()
        tx_rows.append({
            "date": tx.created_at.strftime("%d.%m"),
            "time": tx.created_at.strftime("%H:%M"),
            "amount": float(tx.amount or 0),
            "is_in": is_in,
            "label": desc[:80],
            "order_ref": order_ref,
        })

    return ActionResult(
        text=f"💰 Ваша выручка · на счёте ${wallet.balance:,.2f}",
        cards=[{
            "type": "seller_revenue",
            "data": {
                "title": "Выручка продавца",
                "balance":         float(wallet.balance or 0),
                "currency":        wallet.currency,
                "revenue_paid":    revenue_paid,
                "revenue_in_work": revenue_in_work,
                "revenue_pending": revenue_pending,
                "open_count":      len(open_orders),
                "transactions":    tx_rows,
            },
        }],
        actions=[
            {"label": "📤 Запрос выплаты", "action": "request_payout", "params": {}},
            {"label": "📦 Мои заказы",      "action": "get_orders", "params": {}},
            {"label": "📊 Аналитика",       "action": "get_analytics", "params": {}},
        ],
        suggestions=["История поступлений", "Какая комиссия платформы?"],
    )


@register("get_balance")
def get_balance(params, user, role):
    """Показать баланс депозита и последние транзакции.
    - buyer  → Wallet депозит (предоплата заказов)
    - seller → выручка: зачислено + ожидается + в работе
    - operator/admin → доход платформы (комиссия)
    """
    # Operator → единый финансовый дашборд (платформа + личный бонус в одной карточке)
    if role and role.startswith("operator"):
        from .operator_actions import op_payments_dashboard
        return op_payments_dashboard(params, user, role)
    # Admin → отчёт по комиссии платформы
    if role == "admin":
        from .admin_actions import admin_revenue_breakdown
        return admin_revenue_breakdown(params, user, role)

    # Seller → собственный экран выручки
    if role == "seller":
        return _seller_revenue_view(params, user, role)

    from .models import Wallet
    if role == "seller":
        from .seller_actions import _effective_seller
        user = _effective_seller(user)
    wallet = Wallet.for_user(user)
    txs = list(wallet.transactions.all()[:12])

    # 30-дневная статистика
    from datetime import timedelta
    from django.utils import timezone as _tz_now
    cutoff = _tz_now.now() - timedelta(days=30)
    txs_30d = [t for t in txs if t.created_at >= cutoff]
    spent_30d = sum(float(t.amount) for t in txs_30d if t.kind not in ("topup", "refund"))
    topup_30d = sum(float(t.amount) for t in txs_30d if t.kind in ("topup", "refund"))

    # Чистим описание от технических хвостов
    import re as _re
    def _clean_desc(s: str) -> str:
        s = s or ""
        s = _re.sub(r"\s*\(intent[^)]*\)", "", s)
        s = _re.sub(r"\s*\[chat-demo\]", "", s)
        return s.strip()

    tx_rows = []
    for tx in txs:
        kind = tx.kind or ""
        is_in = kind in ("topup", "refund")
        # Прямое поле order_id (источник истины); regex на description — fallback
        # для старых записей без order_id.
        order_ref = tx.order_id
        if not order_ref:
            m = _re.search(r"#(\d+)", tx.description or "")
            order_ref = int(m.group(1)) if m else None
        desc_clean = _clean_desc(tx.description) or tx.get_kind_display()
        if order_ref:
            desc_clean = _re.sub(r"\s*#\d+\s*", " ", desc_clean).strip()
        tx_rows.append({
            "date":   tx.created_at.strftime("%d.%m"),
            "time":   tx.created_at.strftime("%H:%M"),
            "amount": float(tx.amount or 0),
            "is_in":  is_in,
            "label":  desc_clean[:80],
            "order_ref": order_ref,
        })

    return ActionResult(
        text=f"💰 Ваш депозит: ${wallet.balance:,.2f} {wallet.currency}",
        cards=[{
            "type": "wallet",
            "data": {
                "balance":      float(wallet.balance or 0),
                "currency":     wallet.currency,
                "spent_30d":    spent_30d,
                "topup_30d":    topup_30d,
                "transactions": tx_rows,
            },
        }],
        actions=[
            {"label": "💵 Пополнить $10,000", "action": "topup_wallet",
             "params": {"amount": 10000}},
            {"label": "📦 Мои заказы", "action": "get_orders", "params": {}},
        ],
        suggestions=["Пополнить депозит", "История списаний"],
    )


@register("request_payout")
def request_payout(params, user, role):
    """Запрос на выплату накопленной выручки продавцу."""
    from .models import Wallet
    from .seller_actions import _effective_seller
    seller_user = _effective_seller(user) if role == "seller" else user
    wallet = Wallet.for_user(seller_user)
    if (wallet.balance or 0) <= 0:
        return ActionResult(text="💸 На счёте нет средств для вывода.",
                            suggestions=["Мои заказы", "История поступлений"])
    return ActionResult(
        text=(
            f"📤 Запрос выплаты (доступно: ${wallet.balance:,.2f})\n\n"
            "Финансист платформы зачислит средства на ваш банковский счёт в течение "
            "1-2 рабочих дней. Минимальная сумма — $500.\n\n"
            "Напишите оператору сумму и реквизиты — оформим выплату вручную "
            "(скоро добавим автоматический вывод через банк)."
        ),
        actions=[
            {"label": "💬 Написать оператору", "action": "ask_operator", "params": {}},
            {"label": "💰 Мой баланс",          "action": "get_balance", "params": {}},
        ],
    )


@register("link_card")
def link_card(params, user, role):
    """Привязать банковскую карту к депозиту.
    Заглушка: показывает форму ввода номера/CVV/expiry. Реальной интеграции
    с эквайером пока нет — пишем «функция в разработке, оператор подтвердит
    привязку вручную».
    """
    return ActionResult(
        text=(
            "💳 Привязка карты\n\n"
            "Скоро добавим автоматическую привязку через Stripe / Тинькофф Эквайринг.\n"
            "Пока — напишите оператору, и он привяжет карту вручную (1-2 часа)."
        ),
        actions=[
            {"label": "💬 Написать оператору", "action": "ask_operator", "params": {}},
            {"label": "💵 Пополнить без карты", "action": "topup_wallet", "params": {"amount": 10000}},
        ],
        suggestions=["Мой баланс", "История операций"],
    )


@register("withdraw_wallet")
def withdraw_wallet(params, user, role):
    """Вывод средств с депозита обратно на карту/счёт."""
    from .models import Wallet
    wallet = Wallet.for_user(user)
    if (wallet.balance or 0) <= 0:
        return ActionResult(text="💸 На балансе нет средств для вывода.",
                            suggestions=["Пополнить депозит"])
    return ActionResult(
        text=(
            f"💸 Вывод средств (доступно: ${wallet.balance:,.2f})\n\n"
            "Доступно для вывода: средства не зарезервированные под открытые заказы.\n"
            "Напишите оператору сумму и реквизиты — выводим в течение 1 рабочего дня."
        ),
        actions=[
            {"label": "💬 Написать оператору", "action": "ask_operator", "params": {}},
            {"label": "📊 Мой баланс", "action": "get_balance", "params": {}},
        ],
    )


@register("transfer_wallet")
def transfer_wallet(params, user, role):
    """Перевод между внутренними счетами / контрагентам."""
    return ActionResult(
        text=(
            "↗ Перевод средств\n\n"
            "Внутренние переводы между счетами Consolidator пока недоступны.\n"
            "Скоро добавим перевод бонусов и кредит-нот между связанными аккаунтами."
        ),
        actions=[
            {"label": "💬 Написать оператору", "action": "ask_operator", "params": {}},
        ],
    )


_AI_PACKS = (50, 100)  # пакеты AI-запросов для покупки с депозита


@register("buy_ai_requests")
def buy_ai_requests(params, user, role):
    """Покупка AI-запросов с депозита (кошелька). Без count → меню пакетов;
    с count (50/100) → атомарно списываем стоимость и начисляем запросы.
    Цена — settings.AI_REQUEST_PRICE_USD (≈ себестоимость). Для покупателя и
    продавца (у обоих есть Wallet); операторам недоступно (нет депозита)."""
    from decimal import Decimal

    from django.conf import settings
    from django.db import transaction
    from django.db.models import F

    from marketplace.models import UserProfile

    from .models import Wallet, WalletTx

    price = Decimal(str(getattr(settings, "AI_REQUEST_PRICE_USD", 0.04)))

    try:
        Wallet.for_user(user)
        balance0 = Wallet.objects.get(user=user).balance
    except Exception:
        balance0 = Decimal("0")

    def _menu():
        acts = []
        for n in _AI_PACKS:
            c = (price * n).quantize(Decimal("0.01"))
            acts.append({"label": f"Купить {n} (${c:,.2f})",
                         "action": "buy_ai_requests", "params": {"count": n}})
        acts.append({"label": "💰 Пополнить депозит", "action": "topup_wallet", "params": {}})
        return ActionResult(
            text=("💳 Покупка AI-запросов с депозита.\n"
                  f"Цена — ${price:.2f} за запрос (по себестоимости). "
                  f"Остаток депозита: ${balance0:,.2f}."),
            actions=acts,
        )

    count = params.get("count")
    if not count:
        return _menu()
    try:
        count = int(count)
    except (TypeError, ValueError):
        return _menu()
    if count not in _AI_PACKS:
        return _menu()

    cost = (price * count).quantize(Decimal("0.01"))
    p = getattr(user, "profile", None)
    if p is None:
        return ActionResult(text="Профиль не найден.")

    try:
        with transaction.atomic():
            Wallet.for_user(user)
            wallet = Wallet.objects.select_for_update().get(user=user)
            if wallet.balance < cost:
                return ActionResult(
                    text=(f"На депозите ${wallet.balance:,.2f}, а нужно ${cost:,.2f} "
                          f"за {count} запросов. Пополните депозит."),
                    actions=[{"label": "💰 Пополнить депозит",
                              "action": "topup_wallet", "params": {}}],
                )
            wallet.balance = wallet.balance - cost
            wallet.save(update_fields=["balance", "updated_at"])
            WalletTx.objects.create(
                wallet=wallet, kind="debit", amount=cost,
                description=f"Покупка {count} AI-запросов",
                balance_after=wallet.balance,
            )
            UserProfile.objects.filter(pk=p.pk).update(ai_credits=F("ai_credits") + count)
    except Exception:
        logger.exception("buy_ai_requests failed")
        return ActionResult(text="⚠️ Не удалось купить запросы. Попробуйте позже.")

    p.refresh_from_db(fields=["ai_credits"])
    wallet.refresh_from_db(fields=["balance"])
    return ActionResult(
        text=(f"✓ Куплено {count} AI-запросов за ${cost:,.2f}.\n"
              f"AI-баланс: {p.ai_credits} запросов · остаток депозита: ${wallet.balance:,.2f}."),
        actions=[{"label": "💬 Продолжить", "action": "go_home", "params": {}}],
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
    from django.db import transaction
    try:
        amount = Decimal(str(params.get("amount") or 10000)).quantize(Decimal("0.01"))
    except Exception:
        return ActionResult(text="Некорректная сумма.")
    if amount <= 0:
        return ActionResult(text="Сумма должна быть больше нуля.")

    # FIX (CRITICAL): защищаем read-modify-write от race condition через
    # select_for_update в transaction (как в pay_reserve).
    with transaction.atomic():
        Wallet.for_user(user)  # ensure exists
        wallet = Wallet.objects.select_for_update().get(user=user)
        wallet.balance = wallet.balance + amount
        wallet.save(update_fields=["balance", "updated_at"])
        WalletTx.objects.create(
            wallet=wallet, kind="topup", amount=amount,
            description="Пополнение депозита (DEMO MODE)",
            balance_after=wallet.balance,
        )

    # Реферал: покупатель-пригласивший пополнил депозит → его buyer_discount −$100.
    try:
        from . import referral as _ref
        _ref.on_deposit_funded(user)
        wallet.refresh_from_db(fields=["balance"])
    except Exception:
        pass

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
            "pdf_url":       f"/api/assistant/topup/{ref}/invoice.pdf",
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


@register("consolidate_wait")
def consolidate_wait(params, user, role):
    """Покупатель/оператор подтверждает: ждём готовности всех поставщиков
    для единой консолидированной отгрузки. Никаких изменений в данных пока
    нет — это решение записывается в logistics_meta для оператора и аудита."""
    from marketplace.models import Order
    oid = params.get("order_id")
    if not oid:
        return ActionResult(text="Не указан заказ.")
    try:
        o = Order.objects.get(id=oid)
    except Order.DoesNotExist:
        return ActionResult(text=f"Заказ #{oid} не найден.")
    if not _user_can_access_order(o, user, role):
        return ActionResult(text=f"Заказ #{oid} не найден.")
    lm = dict(o.logistics_meta or {})
    lm["shipment_decision"] = "consolidate"
    lm["shipment_decision_by"] = user.username
    o.logistics_meta = lm
    o.save(update_fields=["logistics_meta"])
    return ActionResult(
        text=(f"✓ Решение по ORD-{o.id}: ждём готовности всех поставщиков для "
              f"единой отправки. Готовые позиции остаются на складе платформы. "
              f"ETA = по самому медленному."),
        actions=[{"label": "📦 Открыть трекинг", "action": "track_order",
                  "params": {"order_id": o.id}}],
    )


@register("split_shipment")
def split_shipment(params, user, role):
    """Split: готовые позиции отгружаются отдельной партией не дожидаясь.
    Создаём реальный Shipment с теми OrderItem'ами, которые уже ушли
    вперёд (status > ready_to_ship). Их статус становится статусом партии."""
    from marketplace.models import Order, Shipment
    oid = params.get("order_id")
    if not oid:
        return ActionResult(text="Не указан заказ.")
    try:
        o = Order.objects.get(id=oid)
    except Order.DoesNotExist:
        return ActionResult(text=f"Заказ #{oid} не найден.")
    if not _user_can_access_order(o, user, role):
        return ActionResult(text=f"Заказ #{oid} не найден.")
    STAGE_ORDER = ["awaiting_reserve", "reserve_paid", "confirmed", "in_production",
                   "ready_to_ship", "transit_abroad", "customs", "transit_rf",
                   "issuing", "delivered", "completed"]
    READY_IDX = STAGE_ORDER.index("ready_to_ship")
    ahead_items = []
    for it in o.items.select_related("part__seller"):
        st = it.status or o.status
        idx = STAGE_ORDER.index(st) if st in STAGE_ORDER else 0
        if idx > READY_IDX:
            ahead_items.append((it, st))
    if not ahead_items:
        return ActionResult(text=(
            f"По ORD-{o.id} никто не ушёл вперёд — split не нужен. "
            f"Все позиции ≤ ready_to_ship, ждут консолидации."))
    # Уникальные комбинации (поставщик, статус) = отдельные партии.
    # Логично: разные стадии в одной партии не бывает; разные поставщики
    # дают разные коносаменты.
    from collections import defaultdict as _dd
    groups = _dd(list)
    for it, st in ahead_items:
        sid = it.part.seller_id if it.part else 0
        groups[(sid, st)].append(it)
    created = []
    for (sid, st), its in groups.items():
        # Если у этого Shipment уже есть запись — не плодим дубли
        existing = o.shipments.filter(kind="split", status=st,
                                      items__in=its).distinct().first()
        if existing:
            continue
        sh = Shipment.objects.create(
            order=o, kind="split", status=st,
            shipping_mode=getattr(o, "shipping_mode", "") or "",
            notes=f"split-by {user.username}",
        )
        sh.items.set(its)
        created.append(sh)
    lm = dict(o.logistics_meta or {})
    lm["shipment_decision"] = "split"
    lm["shipment_decision_by"] = user.username
    lm["shipments_split_count"] = (lm.get("shipments_split_count", 0) + len(created))
    o.logistics_meta = lm
    o.save(update_fields=["logistics_meta"])
    summary = "\n".join(
        f"  • Shipment #{sh.id}: {sh.items.count()} поз · "
        f"${float(sh.total_amount):,.0f} · {sh.get_status_display()}"
        for sh in created
    ) or "  (новых партий не создано — уже были)"
    return ActionResult(
        text=(f"✓ Split shipment по ORD-{o.id}: создано {len(created)} партий.\n"
              f"{summary}\n\nКаждая теперь — отдельный коносамент, ETA, таможня."),
        actions=[{"label": "📦 Открыть трекинг", "action": "track_order",
                  "params": {"order_id": o.id}}],
    )


@register("set_supplier_decision")
def set_supplier_decision(params, user, role):
    """Per-supplier выбор «ждать всех» vs «отправлять отдельно».
    Записывает предпочтение в logistics_meta.per_supplier[seller_id] = choice.
    Когда поставщик дойдёт до ready_to_ship — оператор/система создаст Shipment
    по этой настройке (consolidated или split). Если поставщик УЖЕ ready и
    выбран split — сразу создаём split-партию для его позиций."""
    from marketplace.models import Order, Shipment
    oid = params.get("order_id")
    sid = params.get("seller_id")
    choice = (params.get("choice") or "").strip()
    if not oid or choice not in ("consolidate", "split"):
        return ActionResult(text="Не указаны параметры решения.")
    try:
        o = Order.objects.get(id=oid)
    except Order.DoesNotExist:
        return ActionResult(text=f"Заказ #{oid} не найден.")
    if not _user_can_access_order(o, user, role):
        return ActionResult(text=f"Заказ #{oid} не найден.")
    lm = dict(o.logistics_meta or {})
    per = dict(lm.get("per_supplier") or {})
    per[str(sid)] = choice
    lm["per_supplier"] = per
    lm["shipment_decision_by"] = getattr(user, "username", "—")
    o.logistics_meta = lm
    o.save(update_fields=["logistics_meta"])
    # Имя поставщика для текста
    _sup_name = "поставщик"
    try:
        from marketplace.models import User as _U
        _u = _U.objects.filter(id=sid).only("username").first()
        if _u:
            _sup_name = _u.username
    except Exception:
        pass
    # Если split И поставщик уже ready_to_ship — создаём split-партию сразу
    created_now = None
    if choice == "split":
        ready_items = [it for it in o.items.select_related("part__seller")
                       if it.part and it.part.seller_id == int(sid)
                       and (it.status or o.status) == "ready_to_ship"
                       and not o.shipments.filter(items=it).exists()]
        if ready_items:
            sh = Shipment.objects.create(
                order=o, kind="split", status="ready_to_ship",
                shipping_mode=getattr(o, "shipping_mode", "") or "",
                notes=f"per-supplier split by {getattr(user,'username','—')}",
            )
            sh.items.set(ready_items)
            created_now = sh
    if choice == "consolidate":
        msg = (f"✓ {_sup_name}: ждать общую партию. Готовые позиции "
               f"останутся на складе платформы до готовности остальных.")
    else:
        msg = f"✓ {_sup_name}: отправлять отдельной партией."
        if created_now:
            msg += f" Создана партия #{created_now.id}."
    return ActionResult(
        text=msg,
        actions=[{"label": "📦 Открыть трекинг", "action": "track_order",
                  "params": {"order_id": o.id}}],
    )

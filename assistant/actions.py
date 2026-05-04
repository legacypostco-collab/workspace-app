"""Chat-First Action Executor.

When AI determines the user wants to perform an action (search, create RFQ,
track shipment, etc.), it calls one of these handlers. Each handler returns
an ActionResult with text + cards + new actions + suggestions.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Callable

from django.db.models import Q

logger = logging.getLogger(__name__)


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
    "get_budget", "get_analytics",
    "compare_products", "compare_suppliers", "top_suppliers",
    "upload_parts_list", "analyze_spec",
    "get_claims", "create_claim",
    "open_url", "generate_proposal",
    # покупка и депозит
    "quick_order", "pay_reserve", "pay_final",
    "get_balance", "topup_wallet",
    # приёмка собственного заказа после доставки
    "confirm_delivery",
    # база знаний, конфигуратор цены, аудит, QR, уведомления
    "kb_search", "price_quote", "audit_log", "generate_qr", "notifications",
]

# Seller-only: эксклюзивные действия продавца — отвечать на RFQ, грузить
# прайс, двигать заказ по pipeline (production → ready → shipped → ...).
# Внутри advance_order ещё проверяется, что в заказе есть товары seller'а.
_SELLER_ONLY = [
    "respond_rfq", "upload_pricelist",
    "get_demand_report", "get_sla_report",
    "advance_order",
    "seller_pipeline", "ship_order",
    "seller_dashboard", "seller_finance", "seller_rating",
    "seller_inbox",
    "seller_catalog", "toggle_product", "add_product", "edit_product",
    "product_detail", "import_pricelist_preview",
    "rfq_detail", "respond_rfq_form",
    "seller_drawings", "seller_team", "invite_team_member",
    "seller_integrations", "seller_reports",
    "seller_qr", "seller_logistics", "seller_negotiations",
    "price_quote", "audit_log", "generate_qr", "notifications",
    "sync_1c",
]

_OPERATOR_CORE = [
    # Read-only browse + диспетчерские action'ы
    "search_parts", "get_orders", "get_order_detail", "get_rfq_status",
    "track_order", "track_shipment", "advance_order",
    "get_analytics", "get_demand_report", "get_sla_report", "get_budget",
    "compare_suppliers", "compare_products", "top_suppliers",
    "get_claims", "open_url", "generate_proposal",
    "audit_log", "kb_search", "notifications",
    # Operator-only: dashboard, очередь, назначение, спор, заметка
    "op_dashboard", "op_queue", "op_sla_breach",
    "op_order_detail", "op_assign", "op_add_note", "op_resolve_dispute",
    # Customs / Compliance
    "op_hs_lookup", "op_hs_assign", "op_calc_duty",
    "op_certs_check", "op_cert_upload", "op_sanctions_check",
    "op_customs_dashboard", "op_customs_release",
    # Payments / Escrow dashboard
    "op_payments_dashboard",
    # Operator analytics
    "op_logistics_stats", "op_payments_stats",
]

ROLE_ACTIONS = {
    "buyer":  _BUYER_ACTIONS,
    "seller": _BUYER_ACTIONS + _SELLER_ONLY,
    "operator_logist": _OPERATOR_CORE,
    "operator_customs": _OPERATOR_CORE,
    "operator_payment": _OPERATOR_CORE,
    "operator_manager": _OPERATOR_CORE,
    "operator": _OPERATOR_CORE,
    "admin": ["*"],  # admin sees everything
}


def can_execute(action_name: str, role: str) -> bool:
    allowed = ROLE_ACTIONS.get(role, [])
    return "*" in allowed or action_name in allowed


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
        return ActionResult(text=f"⚠️ Нет прав на действие '{action_name}' для роли {role}")
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
        return _search_articles_list(articles)

    # 2) Free-text query --------------------------------------------
    qs = Part.objects.select_related("brand", "category").filter(is_active=True)
    if query:
        qs = qs.filter(
            Q(oem_number__icontains=query)
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


def _search_articles_list(articles: list[str]):
    """Look up each article in the catalog → spec_results-style card."""
    from marketplace.models import Part

    items = []
    matched_ids = []
    found_n = 0
    not_found_n = 0
    total = 0

    for art in articles:
        p = (
            Part.objects
            .select_related("brand")
            .filter(is_active=True, oem_number__iexact=art)
            .first()
        )
        if p is None:
            # Try fuzzy contains
            p = (
                Part.objects
                .select_related("brand")
                .filter(is_active=True, oem_number__icontains=art)
                .first()
            )
        if p:
            qty = 1
            price = float(p.price) if p.price else 0
            items.append({
                "status": "in_stock",
                "id": p.oem_number,
                "name": p.title,
                "brand": p.brand.name if p.brand else "—",
                "condition": "oem",
                "price": price,
                "qty": qty,
                "weight": "—",
                "currency": "USD",
            })
            matched_ids.append(str(p.id))
            found_n += 1
            total += price * qty
        else:
            items.append({
                "status": "not_found",
                "id": art,
                "name": "",
                "qty": 1,
            })
            not_found_n += 1

    intro = (
        f"Проверил {len(articles)} артикулов: {found_n} найдено, "
        f"{not_found_n} нет в каталоге. "
        + (f"Сумма по найденным — ${total:,.0f}." if found_n else
           "Можно создать RFQ — поставщики поищут аналоги.")
    )

    # Полный набор действий — то, что умел маркетплейс, но прямо в чате.
    # Порядок: primary (RFQ) → создание ценности (КП, заказ) → сравнение/анализ → утилиты.
    actions = []
    if matched_ids:
        actions.append({"label": f"⚡ Купить сейчас (${total:,.0f})", "action": "quick_order",
                        "params": {"product_ids": matched_ids}})
        actions.append({"label": "Создать RFQ на найденные", "action": "create_rfq",
                        "params": {"product_ids": matched_ids}})
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
            "currency": "USD",
            "foot_info": f"{found_n} из {len(articles)} priced",
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


@register("create_rfq")
def create_rfq(params, user, role):
    """Create a new RFQ + RFQItem rows. params: {product_ids?, articles?, quantity, query?}"""
    from marketplace.models import RFQ, RFQItem, Part

    quantity = int(params.get("quantity") or 1)

    # Resolve items: explicit product_ids first, then articles, then split query
    items_to_add = []  # list of (query, qty, matched_part)

    if params.get("product_ids"):
        for pid in params["product_ids"]:
            p = Part.objects.filter(id=pid).select_related("brand").first()
            if p:
                items_to_add.append((p.oem_number, quantity, p))
            else:
                items_to_add.append((str(pid), quantity, None))

    elif params.get("articles"):
        for art in params["articles"]:
            p = (
                Part.objects.select_related("brand")
                .filter(is_active=True)
                .filter(Q(oem_number__iexact=art) | Q(oem_number__icontains=art))
                .first()
            )
            items_to_add.append((art, quantity, p))

    elif params.get("query"):
        # Try to extract article-like tokens from the query string
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
                items_to_add.append((art, quantity, p))
        else:
            items_to_add.append((q[:255], quantity, None))

    if not items_to_add:
        items_to_add = [("RFQ из чата", quantity, None)]

    # Build a short notes summary
    notes_parts = []
    if params.get("query") and len(items_to_add) == 1:
        notes_parts.append(f"Запрос: {params['query'][:300]}")
    notes_parts.append(f"Создано из чата · {len(items_to_add)} позиций")

    try:
        rfq = RFQ.objects.create(
            created_by=user,
            customer_name=user.get_full_name() or user.username,
            customer_email=user.email or f"{user.username}@chat.local",
            company_name="",
            mode="semi",
            urgency="standard",
            status="new",
            notes=" | ".join(notes_parts)[:5000],
        )
        for query_str, qty, matched_part in items_to_add:
            RFQItem.objects.create(
                rfq=rfq,
                query=str(query_str)[:255],
                quantity=qty,
                matched_part=matched_part,
                state="matched" if matched_part else "new",
            )
    except Exception as e:
        logger.exception("create_rfq failed")
        return ActionResult(text=f"⚠️ Не удалось создать RFQ: {e}")

    matched_count = sum(1 for _, _, p in items_to_add if p is not None)
    summary = f"{matched_count} из {len(items_to_add)} позиций сматчены с каталогом"

    return ActionResult(
        text=(
            f"✓ RFQ #{rfq.id} создан · {len(items_to_add)} позиций. "
            f"{summary}. Поставщики получат уведомление и ответят с ценами."
        ),
        cards=[{
            "type": "rfq",
            "data": {
                "id": str(rfq.id),
                "number": rfq.id,
                "status": "new",
                "description": " · ".join(q for q, _, _ in items_to_add[:5])[:200],
                "quantity": sum(q for _, q, _ in items_to_add),
                "created_at": rfq.created_at.strftime("%d.%m.%Y %H:%M"),
            },
        }],
        actions=[
            {"label": "Открыть RFQ", "action": "open_url",
             "params": {"_url": f"/chat/rfq/{rfq.id}/"}},
            {"label": "Сформировать КП", "action": "generate_proposal",
             "params": {"rfq_id": rfq.id}},
            {"label": "Статус и ответы", "action": "get_rfq_status",
             "params": {"rfq_id": rfq.id}},
        ],
        suggestions=["Мои активные RFQ", "Сформировать КП", "Создать ещё RFQ"],
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
            "total": float(o.total_amount or 0),
            "currency": "USD",
            "customer": o.customer_name or (o.buyer.get_full_name() if o.buyer else "—"),
            "created_at": o.created_at.strftime("%d.%m.%Y"),
        },
    } for o in orders]

    if not cards:
        return ActionResult(
            text="У вас пока нет заказов.",
            suggestions=["Найти запчасть", "Создать RFQ"],
        )

    return ActionResult(
        text=f"Ваши последние {len(cards)} заказа:",
        cards=cards,
        actions=[
            {"label": "Только в работе", "action": "get_orders",
             "params": {"status": "in_production"}},
            {"label": "Только оплаченные", "action": "get_orders",
             "params": {"status": "paid"}},
        ],
        suggestions=["Трекинг отгрузки", "Бюджет за месяц"],
    )


@register("get_order_detail")
def get_order_detail(params, user, role):
    from marketplace.models import Order
    oid = params.get("order_id") or params.get("id")
    if not oid:
        return ActionResult(text="⚠️ Не указан ID заказа")
    try:
        o = Order.objects.select_related("buyer").get(id=oid)
    except Order.DoesNotExist:
        return ActionResult(text=f"⚠️ Заказ #{oid} не найден")

    return ActionResult(
        text=f"Заказ #{o.id} — {o.get_status_display()}",
        cards=[{
            "type": "order",
            "data": {
                "id": str(o.id),
                "number": f"ORD-{o.id}",
                "status": o.get_status_display(),
                "total": float(o.total_amount or 0),
                "customer": o.customer_name or "",
                "supplier": "—",
                "created_at": o.created_at.strftime("%d.%m.%Y %H:%M"),
            },
        }],
        actions=[
            {"label": "Трекинг", "action": "track_shipment", "params": {"order_id": o.id}},
        ],
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
    from marketplace.models import Order, RFQ
    return ActionResult(
        text="Краткая сводка платформы:",
        cards=[{
            "type": "chart",
            "data": {
                "title": "Метрики",
                "items": [
                    {"label": "Заказов всего", "value": Order.objects.count()},
                    {"label": "Активных RFQ", "value": RFQ.objects.exclude(status="closed").count()},
                ],
            },
        }],
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


@register("compare_suppliers")
def compare_suppliers(params, user, role):
    from django.contrib.auth.models import User
    sellers = User.objects.filter(userprofile__role="seller")[:5]
    return ActionResult(
        text=f"Топ поставщиков ({len(sellers)}):",
        cards=[{
            "type": "comparison",
            "data": {
                "headers": ["Поставщик", "Email"],
                "rows": [[s.get_full_name() or s.username, s.email] for s in sellers],
            },
        }],
    )


@register("get_demand_report")
def get_demand_report(params, user, role):
    from marketplace.models import RFQ
    return ActionResult(
        text=f"Активных RFQ в системе: {RFQ.objects.exclude(status='closed').count()}",
        suggestions=["Топ запрашиваемых категорий"],
    )


@register("get_sla_report")
def get_sla_report(params, user, role):
    from marketplace.models import Order
    breached = Order.objects.filter(sla_status="breached").count()
    on_track = Order.objects.filter(sla_status="on_track").count()
    return ActionResult(
        text=f"SLA: на дорожке {on_track}, нарушений {breached}",
    )


@register("get_claims")
def get_claims(params, user, role):
    from marketplace.models import OrderClaim
    qs = OrderClaim.objects.order_by("-created_at")
    if role == "buyer":
        qs = qs.filter(order__buyer=user)
    return ActionResult(
        text=f"Активных рекламаций: {qs.count()}",
        suggestions=["Создать рекламацию"],
    )


@register("create_claim")
def create_claim(params, user, role):
    return ActionResult(text="Создание рекламации — заполните форму на /buyer/claims/")


@register("upload_parts_list")
def upload_parts_list(params, user, role):
    return ActionResult(
        text="Загрузите Excel файл со списком артикулов через значок 📎",
        actions=[{"label": "Открыть прайс-лист", "action": "search_parts", "params": {"query": ""}}],
    )


@register("upload_pricelist")
def upload_pricelist(params, user, role):
    return ActionResult(
        text="Загрузите CSV/Excel прайс-лист через /seller/products/upload/",
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
    """Top-N suppliers ranked by price/coverage/lead time on the current spec."""
    suppliers = [
        {"name": "Caterpillar Eurasia", "rating": "4.9", "total": 47890,
         "coverage": "32 из 39 позиций", "lead_time": "9 дней", "currency": "USD"},
        {"name": "Heavy Equipment Spares", "rating": "4.7", "total": 48720,
         "coverage": "35 из 39", "lead_time": "10 дней", "currency": "USD"},
        {"name": "Уралмаш-Маркет", "rating": "4.8", "total": 48410,
         "coverage": "38 из 39", "lead_time": "11 дней", "note": "включая аналоги",
         "currency": "USD"},
    ]
    return ActionResult(
        text=(
            "Рекомендую разослать всем трём — Caterpillar Eurasia может не покрыть 7 позиций, "
            "остальные дадут конкуренцию по цене. Создать RFQ?"
        ),
        cards=[{"type": "supplier_top", "data": {"suppliers": suppliers}}],
        actions=[
            {"label": "Создать RFQ для топ-3", "action": "create_rfq",
             "params": {"query": "Spec Q2 2026 — top 3 suppliers"}},
            {"label": "Добавить ещё поставщиков", "action": "top_suppliers",
             "params": {"limit": 5}},
            {"label": "Сравнить детально", "action": "compare_suppliers",
             "params": {"supplier_ids": [s["name"] for s in suppliers]}},
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

    total = Decimal("0")
    for p in parts:
        if p.price:
            total += Decimal(str(p.price)) * quantity

    reserve_pct = Decimal("10.00")
    reserve_amount = (total * reserve_pct / Decimal("100")).quantize(Decimal("0.01"))
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
        total_amount=total,
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

    return ActionResult(
        text=(
            f"✓ Заказ #{order.id} создан · {len(parts)} позиций · ${total:,.0f}.\n"
            f"Резерв 10%: ${reserve_amount:,.0f} · "
            f"на счёте: ${wallet.balance:,.0f} {wallet.currency}."
            + ("" if enough else
               f"\n⚠️ Недостаточно средств — пополните депозит на ${reserve_amount - wallet.balance:,.0f}.")
        ),
        cards=[{
            "type": "order",
            "data": {
                "id": str(order.id),
                "number": order.id,
                "status": "pending",
                "status_label": "Ожидание оплаты",
                "items_count": len(parts),
                "total": float(total),
                "currency": "USD",
                "reserve_amount": float(reserve_amount),
                "payment_status": "awaiting_reserve",
                "payment_status_label": "Ожидает резерва 10%",
                "wallet_balance": float(wallet.balance),
            },
        }],
        actions=(
            [{"label": f"💳 Списать ${reserve_amount:,.0f} из депозита",
              "action": "pay_reserve", "params": {"order_id": order.id}}]
            if enough else
            [{"label": "Пополнить депозит (демо)", "action": "topup_wallet",
              "params": {"amount": float(max(reserve_amount * 5, Decimal("10000")))}}]
        ) + [
            {"label": "Детали заказа", "action": "get_order_detail",
             "params": {"order_id": order.id}},
        ],
        suggestions=["Баланс депозита", "Статус заказа", "Изменить адрес доставки"],
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
    from .models import Wallet, WalletTx

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
                 "params": {"amount": float(max(shortage * 2, 10000))}},
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

    # Эскроу-платёж: create + confirm intent → деньги уходят на платформу,
    # а не «в никуда». При confirm_delivery платформа высвобождает средства
    # продавцу; при споре — refund'ит покупателю.
    from . import payments as _pay
    intent = _pay.create_payment_intent(amount, order_id=order.id, payer=user, kind="reserve")
    with transaction.atomic():
        intent = _pay.confirm_payment_intent(intent, user)
        order.payment_status = "reserve_paid"
        order.status = "reserve_paid"
        order.reserve_paid_at = timezone.now()
        order.save(update_fields=["payment_status", "status", "reserve_paid_at"])
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
    """Создаёт Notification + пушит её через WebSocket. Безопасный — не падает."""
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


@register("seller_dashboard")
def seller_dashboard(params, user, role):
    """Главная сводка продавца: KPI, новые RFQ, активные заказы, рейтинг.

    Аналог /seller/dashboard/, но в чате — пять KPI-блоков и кнопки на
    самые частые действия.
    """
    from datetime import timedelta
    from decimal import Decimal
    from django.db.models import Count, Sum, Avg
    from django.utils import timezone
    from marketplace.models import Order, OrderItem, RFQ, Part

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
    from django.db.models import Count, Avg
    from marketplace.models import OrderClaim, Order
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
            g["orders"][oid] = {
                "id": oid,
                "buyer": order.customer_name or (order.buyer.username if order.buyer else "—"),
                "items": [],
                "subtotal": Decimal("0"),
                "payment_status": order.payment_status,
            }
        sub = (Decimal(str(it.unit_price)) * it.quantity).quantize(Decimal("0.01"))
        g["orders"][oid]["items"].append({
            "article": it.part.oem_number,
            "name": it.part.title,
            "brand": it.part.brand.name if it.part.brand else "—",
            "qty": it.quantity,
            "unit_price": float(it.unit_price),
            "subtotal": float(sub),
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

    # (status, label, btn_label, btn_action) — btn_action=None → advance_order
    STATUS_ORDER = [
        ("reserve_paid",  "💰 Резерв оплачен — подтвердить и в производство", "▶️ Подтвердить",       None),
        ("confirmed",     "✅ Подтверждены — запустить производство",          "▶️ В производство",    None),
        ("in_production", "🏭 В производстве — отметить готовность",          "▶️ Готов к отгрузке",  None),
        ("ready_to_ship", "📦 Готов к отгрузке — оплачено, можно грузить",    "🚚 Отгрузить",         "ship_order"),
        ("transit_abroad","🛫 В транзите за рубеж",                            "▶️ На таможню",        None),
        ("customs",       "🛃 На таможне",                                     "▶️ Транзит по РФ",     None),
        ("transit_rf",    "🚛 Транзит по РФ",                                  "▶️ К выдаче",          None),
        ("issuing",       "📬 На выдаче",                                      "▶️ Доставлен",         None),
        ("delivered",     "🏁 Доставлен — ждём приёмки покупателя",           None,                    None),
        ("pending",       "⏳ Ожидает оплаты резерва (на покупателе)",         None,                    None),
    ]

    sections = []
    for code, label, btn, btn_action in STATUS_ORDER:
        g = groups.get(code)
        if not g or not g["orders"]:
            continue
        orders_list = []
        for o in list(g["orders"].values())[:8]:
            orders_list.append({
                "id": o["id"],
                "buyer": o["buyer"],
                "items": o["items"],
                "subtotal": float(o["subtotal"]),
                "payment_status": o["payment_status"],
            })
        sections.append({
            "status": code,
            "label": label,
            "btn": btn,
            "btn_action": btn_action or "advance_order",
            "orders_count": len(g["orders"]),
            "items_count": g["items_count"],
            "amount": float(g["amount"]),
            "actionable": btn is not None,
            "orders": orders_list,
        })

    text = (
        f"🔧 В вашей очереди — {len(total_orders)} заказа(ов) с вашими товарами.\n"
        f"Сгруппировано по этапам ниже. Жмите кнопку рядом с этапом, чтобы продвинуть все его заказы."
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
    next_actions.append({"label": "Спрос", "action": "get_demand_report", "params": {}})
    next_actions.append({"label": "Прайс-лист", "action": "upload_pricelist", "params": {}})

    return ActionResult(
        text=text,
        cards=[{
            "type": "seller_queue",
            "data": {
                "title": "К отгрузке",
                "total_orders": len(total_orders),
                "sections": sections,
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

    tracking = (params.get("tracking_number") or "").strip()
    carrier = (params.get("carrier") or "").strip() or "Self"

    # Шаг 1: запрашиваем tracking, если не передан
    if not tracking:
        return ActionResult(
            text=(
                f"Отгрузка заказа #{order.id} ({order.customer_name}) "
                f"на сумму ${order.total_amount:,.0f}.\n"
                f"Введите номер накладной / tracking-номер перевозчика."
            ),
            cards=[{
                "type": "form",
                "data": {
                    "title": f"🚚 Отгрузка заказа #{order.id}",
                    "submit_action": "ship_order",
                    "submit_label": "Отправить",
                    "fields": [
                        {"name": "tracking_number", "label": "Tracking-номер",
                         "placeholder": "например, RA123456789CN", "required": True},
                        {"name": "carrier", "label": "Перевозчик",
                         "placeholder": "DHL / China Post / EMS / Self",
                         "default": "Self"},
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

    # Шаг 2: реально отгружаем
    meta = dict(order.logistics_meta or {})
    meta.update({
        "tracking_number": tracking,
        "carrier": carrier,
        "shipped_at": timezone.now().isoformat(),
        "shipped_by": user.username,
    })
    order.status = "transit_abroad"
    order.logistics_meta = meta
    order.logistics_provider = carrier or order.logistics_provider
    order.save(update_fields=["status", "logistics_meta", "logistics_provider"])
    _log_event(order, "status_changed", actor=user, source="seller",
               meta={"from": "ready_to_ship", "to": "transit_abroad",
                     "tracking_number": tracking, "carrier": carrier})
    # Уведомить покупателя об отгрузке
    if order.buyer_id:
        _notify(order.buyer, kind="order",
                title=f"Заказ #{order.id} отгружен",
                body=f"Tracking {tracking} · перевозчик {carrier}. В транзите за рубеж.")

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

    # Контекстные кнопки — разные для buyer и seller
    actions_list = []
    if role == "buyer":
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
    elif role == "seller":
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

    # Контекстные кнопки: правила (всегда) + AI proactive (опционально)
    ctx_actions = _build_contextual_actions(order, role, user)
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
        # Не дублируем уже добавленные правилами
        seen = {(a["action"], json.dumps(a.get("params", {}), sort_keys=True)) for a in ctx_actions}
        for a in ai_extra:
            key = (a["action"], json.dumps(a.get("params", {}), sort_keys=True))
            if key not in seen:
                ctx_actions.append(a)
    except Exception:
        pass

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
                "tracking_number": (order.logistics_meta or {}).get("tracking_number"),
                "carrier": (order.logistics_meta or {}).get("carrier"),
                "next_actor": next_actor,
                "next_event": next_event,
            },
        }],
        actions=actions_list,
        contextual_actions=ctx_actions,
        suggestions=["Где заказ?", "Когда доставят?", "История по заказу"],
    )


@register("pay_final")
def pay_final(params, user, role):
    """Оплачивает остаток (всё что не покрыто резервом) и переводит заказ в paid → ready_to_ship."""
    from decimal import Decimal
    from django.db import transaction
    from django.utils import timezone
    from marketplace.models import Order
    from .models import Wallet, WalletTx

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

    from . import payments as _pay
    intent = _pay.create_payment_intent(final_amount, order_id=order.id, payer=user, kind="final")
    with transaction.atomic():
        intent = _pay.confirm_payment_intent(intent, user)
        order.payment_status = "paid"
        order.status = "ready_to_ship"
        order.final_paid_at = timezone.now()
        order.save(update_fields=["payment_status", "status", "final_paid_at"])
    wallet.refresh_from_db(fields=["balance"])
    _log_event(order, "final_payment_paid", actor=user, source="buyer",
               meta={"amount": float(final_amount), "balance_after": float(wallet.balance),
                     "intent_id": intent["id"]})

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

    # Seller двигает только свои заказы — те, в OrderItem которых есть его товар.
    from marketplace.models import OrderItem
    if role == "seller":
        is_my_order = OrderItem.objects.filter(
            order_id=order_id, part__seller=user
        ).exists()
        if not is_my_order:
            return ActionResult(
                text=f"Заказ #{order_id} не содержит ваших товаров — двигать его не могу."
            )
        try:
            order = Order.objects.get(id=order_id)
        except Order.DoesNotExist:
            return ActionResult(text=f"Заказ #{order_id} не найден.")
    else:
        try:
            order = Order.objects.get(id=order_id, buyer=user)
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
        suggestions = ["Где заказ?", "Когда доставят?", "Трекинг"]

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
    from django.utils import timezone
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

    order.status = "completed"
    order.save(update_fields=["status"])
    _log_event(order, "status_changed", actor=user, source="buyer",
               meta={"from": "delivered", "to": "completed", "kind": "buyer_accepted"})

    # Эскроу → продавцу. Берём seller из первой OrderItem (для одно-продавцовых
    # заказов; multi-seller потребует разбиение по позициям — TODO).
    release_summary = ""
    try:
        from . import payments as _pay
        from marketplace.models import OrderItem
        seller = (
            OrderItem.objects.filter(order=order)
            .select_related("part__seller").first().part.seller
        )
        if seller:
            res = _pay.release_to_seller(order=order, seller=seller)
            if res.get("ok"):
                release_summary = f"\nПлатформа выплатила продавцу ${res['amount']:,.2f} из эскроу."
                _log_event(order, "operator_action", actor=user, source="system",
                           meta={"kind": "escrow_released",
                                 "amount": res["amount"], "to": res["to"]})
                _notify(seller, kind="payment",
                        title=f"Поступление по заказу #{order.id}",
                        body=f"Покупатель подтвердил приёмку — на счёт зачислено ${res['amount']:,.2f}.",
                        url=f"/chat/?order={order.id}")
    except Exception:
        logger.exception("escrow release on confirm_delivery failed")

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


@register("get_balance")
def get_balance(params, user, role):
    """Показать баланс депозита и последние транзакции."""
    from .models import Wallet
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
    """Демо-пополнение депозита (без реальной оплаты)."""
    from decimal import Decimal
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
        description="Пополнение депозита (демо)",
        balance_after=wallet.balance,
    )
    return ActionResult(
        text=(
            f"✓ Депозит пополнен на ${amount:,.2f}.\n"
            f"Текущий остаток: ${wallet.balance:,.2f} {wallet.currency}."
        ),
        actions=[
            {"label": "Баланс депозита", "action": "get_balance", "params": {}},
        ],
    )

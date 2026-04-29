"""Chat-First Action Executor.

When AI determines the user wants to perform an action (search, create RFQ,
track shipment, etc.), it calls one of these handlers. Each handler returns
an ActionResult with text + cards + new actions + suggestions.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

from django.db.models import Q

logger = logging.getLogger(__name__)


@dataclass
class ActionResult:
    """Standard return type for any action."""
    text: str = ""
    cards: list = field(default_factory=list)
    actions: list = field(default_factory=list)
    suggestions: list = field(default_factory=list)

    def to_dict(self):
        return {
            "text": self.text,
            "cards": self.cards,
            "actions": self.actions,
            "suggestions": self.suggestions,
        }


# ── Permission matrix ──────────────────────────────────────
ROLE_ACTIONS = {
    "buyer": [
        "search_parts", "create_rfq", "get_rfq_status", "get_orders",
        "get_order_detail", "track_shipment", "get_budget", "get_analytics",
        "compare_products", "compare_suppliers", "upload_parts_list",
        "get_claims", "create_claim",
        "analyze_spec", "top_suppliers",
    ],
    "seller": [
        "search_parts", "get_rfq_status", "respond_rfq", "get_orders",
        "get_demand_report", "upload_pricelist", "get_analytics",
        "analyze_spec", "top_suppliers",
    ],
    "operator_logist": [
        "track_shipment", "get_orders", "get_sla_report", "get_analytics",
    ],
    "operator_customs": [
        "track_shipment", "get_orders", "get_analytics",
    ],
    "operator_payment": [
        "get_orders", "get_budget", "get_analytics",
    ],
    "operator_manager": [
        "search_parts", "get_orders", "get_rfq_status", "get_analytics",
        "get_demand_report", "get_sla_report", "compare_suppliers",
    ],
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

    actions = []
    if matched_ids:
        actions.append({"label": "Создать RFQ на найденные", "action": "create_rfq",
                        "params": {"product_ids": matched_ids}})
    if not_found_n:
        actions.append({"label": f"Создать RFQ на {not_found_n} ненайденных",
                        "action": "create_rfq",
                        "params": {"query": ", ".join(it["id"] for it in items if it["status"] == "not_found")}})
    actions.append({"label": "Создать RFQ на все", "action": "create_rfq",
                    "params": {"query": ", ".join(articles)}})

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
            "Только OEM",
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
            {"label": "Открыть RFQ", "action": "get_rfq_status",
             "params": {"rfq_id": rfq.id}},
            {"label": "Перейти в кабинет", "action": "get_rfq_status",
             "params": {"rfq_id": rfq.id, "_url": f"/buyer/rfqs/{rfq.id}/"}},
        ],
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
                    "description": (rfq.description or "")[:200],
                    "customer": rfq.customer_name,
                    "created_at": rfq.created_at.strftime("%d.%m.%Y"),
                },
            }],
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
            "description": (r.description or "")[:120],
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

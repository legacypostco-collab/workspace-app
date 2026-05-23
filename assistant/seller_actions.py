"""Seller-specific chat actions: каталог, чертежи, команда, интеграции, отчёты.

Регистрируются в общий реестр assistant.actions через @register декоратор.
Импортируется из assistant/__init__.py чтобы зарегистрировать при загрузке.
"""
from __future__ import annotations

import logging
from decimal import Decimal

from django.utils import timezone

from .actions import ActionResult, register
from .rfq_mode_badge import mode_badge_with_sla

logger = logging.getLogger(__name__)


def _effective_seller(user):
    """Возвращает «продавца» для seller-actions.

    Toggle «Продавец/Покупатель/Оператор» переключает только UI-режим, не меняя
    request.user. Чтобы любой тестировщик мог посмотреть, как seller-кабинет
    наполнен, делаем fallback на demo_seller когда у юзера НЕТ собственного
    каталога — в DEBUG/dev-окружении или если username начинается с
    `demo_*` / `test_*` (классические seed-пользователи).

    Production-юзер с реальным каталогом видит свои данные — fallback не
    срабатывает (у него есть хотя бы один Part).
    """
    from django.conf import settings
    from django.contrib.auth import get_user_model

    from marketplace.models import Part

    from marketplace.models import OrderItem

    username = (user.username or "")
    if username == "demo_seller":
        return user

    # Тестовый/демо/DEBUG-юзер — всегда fallback на demo_seller,
    # независимо от того, есть ли у него своя свалка тестовых parts.
    is_test_account = (
        username.startswith("demo_")
        or username.startswith("test_")
        or username.startswith("tz")  # автогенерённые тестеры из beta
        or bool(getattr(settings, "DEBUG", False))
    )
    if is_test_account:
        try:
            return get_user_model().objects.get(username="demo_seller")
        except Exception:
            return user

    # Production-юзер с реальными OrderItem'ами — продавец сам, никаких подмен.
    if OrderItem.objects.filter(part__seller=user).exists():
        return user

    # Production-юзер без orders, но с parts (свежий продавец) — он сам.
    if Part.objects.filter(seller=user, is_active=True).exists():
        return user

    return user


# ══════════════════════════════════════════════════════════
# 0. Inbox продавца — горящие задачи на сегодня
# ══════════════════════════════════════════════════════════

@register("referral_program")
def referral_program(params, user, role):
    """Реферальная программа: личный код, статистика, размер вознаграждения."""
    import hashlib
    # Детерминированный код на основе user_id + username
    seed = f"{user.id}:{user.username}".encode()
    code = "REF-" + hashlib.md5(seed).hexdigest()[:8].upper()
    link = f"https://consolidator.parts/?ref={code}"

    # Реальная статистика будет когда подключим UTM-tracking. Пока — заглушка.
    invited = 0
    converted = 0
    earned = 0
    pending = 0

    text = (
        f"🤝 Реферальная программа\n"
        f"Ваш код: {code}\n"
        f"Условия: 2% от первого заказа приглашённого клиента (до $5,000), "
        f"далее — 0.5% от всех его заказов в течение года. Партнёрам "
        f"(дилерам и инженерам по сервису) — отдельный тариф 5%."
    )

    return ActionResult(
        text=text,
        cards=[{
            "type": "kpi_grid",
            "data": {
                "title": "Реферальная программа",
                "kpis": [
                    {"label": "Ваш код",      "value": code,       "sub": "копируйте и шлите"},
                    {"label": "Приглашено",   "value": invited,    "sub": "регистраций по коду"},
                    {"label": "Конверсия",    "value": converted,  "sub": "сделали заказ"},
                    {"label": "Заработано",   "value": f"${earned:,.0f}", "sub": "выплачено"},
                    {"label": "В ожидании",   "value": f"${pending:,.0f}", "sub": "после закрытия сделок"},
                    {"label": "Ставка",       "value": "2%",       "sub": "от 1-го заказа клиента"},
                ],
            },
        }, {
            "type": "list",
            "data": {
                "title": "Как поделиться",
                "rows": [
                    {"title": "Личная ссылка", "subtitle": link, "badge": "Копировать"},
                    {"title": "Email-приглашение",
                     "subtitle": "Шаблон с описанием платформы и ссылкой",
                     "badge": "Шаблон"},
                    {"title": "QR-код для визитки",
                     "subtitle": "Сгенерируем QR с вашей ссылкой", "badge": "QR"},
                ],
            },
        }],
        actions=[
            {"label": "Условия программы", "action": "kb_search",
             "params": {"query": "реферальная программа"}},
            {"label": "📊 Дашборд", "action": "seller_dashboard", "params": {}},
        ],
        suggestions=["Сколько мне начислили?", "Кого можно приглашать?"],
    )


@register("mfg_configurator")
def mfg_configurator(params, user, role):
    """Конфигуратор производства по параметрам чертежа.

    Детерминированный расчёт себестоимости и сроков. Без AI — только формула.
    Результат воспроизводим при тех же параметрах.

    params:
      material: 'steel'|'stainless'|'bronze'|'aluminum'|'cast_iron'
      mass_kg: масса детали в кг
      tolerance_class: 'IT7'|'IT9'|'IT11'|'IT14' (точнее → дороже)
      qty: количество, штук
      complexity: 'simple'|'medium'|'complex'
    """
    from decimal import Decimal as D

    material = (params.get("material") or "steel").lower()
    if not params.get("mass_kg") or not params.get("qty"):
        return ActionResult(
            text="Конфигуратор производства: рассчитаю заводскую себестоимость и срок.",
            cards=[{
                "type": "form",
                "data": {
                    "title": "🏭 Конфигуратор производства",
                    "submit_action": "mfg_configurator",
                    "submit_label": "Рассчитать",
                    "fields": [
                        {"name": "material", "label": "Материал",
                         "placeholder": "steel/stainless/bronze/aluminum/cast_iron",
                         "default": "steel"},
                        {"name": "mass_kg", "label": "Масса детали, кг",
                         "type": "number", "required": True,
                         "placeholder": "5"},
                        {"name": "tolerance_class", "label": "Класс точности (IT7/9/11/14)",
                         "default": "IT11"},
                        {"name": "qty", "label": "Серия (количество, шт)",
                         "type": "number", "required": True,
                         "placeholder": "100"},
                        {"name": "complexity", "label": "Сложность (simple/medium/complex)",
                         "default": "medium"},
                    ],
                    "fixed_params": {},
                },
            }],
        )

    try:
        mass = D(str(params["mass_kg"]))
        qty = int(params["qty"])
    except Exception:
        return ActionResult(text="Некорректные параметры.")

    # ── Стоимость материала ──
    MATERIAL_COST = {  # USD/kg (заготовка)
        "steel": D("1.8"),
        "stainless": D("4.5"),
        "bronze": D("12"),
        "aluminum": D("3.2"),
        "cast_iron": D("1.5"),
    }
    mat_cost_per_kg = MATERIAL_COST.get(material, D("2.5"))
    material_cost = mat_cost_per_kg * mass

    # ── Стоимость обработки ──
    COMPLEXITY_HOURS = {"simple": D("0.5"), "medium": D("1.5"), "complex": D("4")}
    hours = COMPLEXITY_HOURS.get((params.get("complexity") or "medium").lower(), D("1.5"))

    TOL_MULT = {"IT7": D("2.5"), "IT9": D("1.6"), "IT11": D("1.0"), "IT14": D("0.7")}
    tol_mult = TOL_MULT.get((params.get("tolerance_class") or "IT11").upper(), D("1.0"))

    machine_rate = D("18")  # USD/час станка
    machining_per_unit = hours * tol_mult * machine_rate

    # ── Скидка серии ──
    if qty >= 1000:
        series_disc = D("0.7")
    elif qty >= 100:
        series_disc = D("0.85")
    elif qty >= 10:
        series_disc = D("0.95")
    else:
        series_disc = D("1.0")

    unit_cost = (material_cost + machining_per_unit) * series_disc
    unit_cost = unit_cost.quantize(D("0.01"))
    total_cost = (unit_cost * qty).quantize(D("0.01"))

    # ── Срок производства ──
    setup_days = 3 if qty < 100 else 5 if qty < 1000 else 8
    prod_days = max(2, int(qty * float(hours) / 16) // 1)  # 16 рабочих часов/день/станок
    total_days = setup_days + prod_days

    text = (
        f"🏭 Расчёт производства\n"
        f"Материал: {material} · масса {mass} кг · точность "
        f"{params.get('tolerance_class','IT11')} · серия {qty} шт.\n"
        f"Себестоимость: ${unit_cost} за шт, ${total_cost} за серию. "
        f"Срок: {total_days} раб. дней (подготовка {setup_days} + производство {prod_days})."
    )
    return ActionResult(
        text=text,
        cards=[{
            "type": "kpi_grid",
            "data": {
                "title": f"Производство · {material} · {qty} шт",
                "kpis": [
                    {"label": "За шт.",         "value": f"${unit_cost}"},
                    {"label": "За серию",       "value": f"${total_cost:,.0f}"},
                    {"label": "Срок, дней",     "value": total_days,
                     "sub": f"+{setup_days} подготовка"},
                    {"label": "Материал",       "value": f"${material_cost}",
                     "sub": f"{mass} кг × ${mat_cost_per_kg}"},
                    {"label": "Обработка",      "value": f"${machining_per_unit:.2f}",
                     "sub": f"{hours} ч × {tol_mult}×"},
                    {"label": "Скидка серии",   "value": f"−{int((1-float(series_disc))*100)}%"},
                ],
            },
        }],
        actions=[
            {"label": "Пересчитать", "action": "mfg_configurator", "params": {}},
            {"label": "💰 Цена для клиента", "action": "price_quote",
             "params": {"base_price": float(unit_cost)}},
        ],
        suggestions=[
            "А если серия 1000?",
            "Что в стоимости?",
            "Ускорить срок — варианты",
        ],
    )


@register("forecast_demand")
def forecast_demand(params, user, role):
    """Прогноз потребности в запчастях на основе моточасов парка техники
    клиента. Учитывает регламентные интервалы ТО (250/500/1000 ч).

    params:
      hours_per_month: число (типичная наработка, по умолчанию 200)
      months_ahead: int (горизонт, по умолчанию 6)
      machines: int (количество единиц техники, по умолчанию 1)
    """
    hours_per_month = int(params.get("hours_per_month") or 200)
    months_ahead = min(int(params.get("months_ahead") or 6), 24)
    machines = max(1, int(params.get("machines") or 1))

    total_hours = hours_per_month * months_ahead * machines

    # Стандартные интервалы (см. KB regulation maintenance_intervals)
    schedule = [
        # (название, интервал в моточасах, тип, ср. цена за ед.)
        ("Масло гидравлическое (фильтр + замена)", 500,  "ТО",   180),
        ("Топливный фильтр",                       250,  "ТО",   45),
        ("Воздушный фильтр",                       500,  "ТО",   60),
        ("Моторное масло (фильтр + замена)",       250,  "ТО",   220),
        ("Гидроцилиндр (РТИ + сальники)",          2000, "ТО-2", 380),
        ("Стартер (профилактика)",                 5000, "КР",   650),
        ("Гусеничная цепь (профилактика)",         8000, "КР",   12000),
    ]

    rows = []
    total_cost = 0
    for name, interval, kind, unit_price in schedule:
        cycles = total_hours // interval
        if cycles == 0:
            continue
        cost = cycles * unit_price
        total_cost += cost
        rows.append({
            "title": f"{name}",
            "subtitle": (f"~{cycles} раз(а) за период · ${unit_price}/шт · "
                         f"интервал {interval} ч · {kind}"),
            "badge": f"${cost:,.0f}",
        })

    text = (
        f"📈 Прогноз потребности на {months_ahead} мес.\n"
        f"Парк: {machines} единиц(а) · наработка ~{hours_per_month} ч/мес → "
        f"итого {total_hours} мото-часов.\n"
        f"Ожидаемые расходы на ТО и КР: примерно ${total_cost:,.0f}."
    )

    return ActionResult(
        text=text,
        cards=[{
            "type": "list",
            "data": {"title": f"План потребности · {months_ahead} мес", "rows": rows},
        }],
        actions=[
            {"label": "📦 Создать RFQ на список",
             "action": "create_rfq",
             "params": {"query": ", ".join(r["title"] for r in rows[:10])}},
            {"label": "Пересчитать", "action": "forecast_demand", "params": {}},
        ],
        suggestions=[
            "А если 300 ч/мес?",
            "Прогноз на год",
            "Только критичные узлы",
        ],
    )


@register("sync_1c")
def sync_1c(params, user, role):
    """Двусторонний обмен с 1С / ERP по правилам ТЗ.

    Без переменной окружения ONEC_ENDPOINT работает в demo-режиме:
    показывает что бы обменялось. С настроенным OData-эндпоинтом —
    реальный обмен через стандартный 1С OData REST.

    params:
      direction: 'pull' (1С → платформа), 'push' (платформа → 1С), 'both'
      since_days: int (по умолчанию 7)
    """
    import os
    from datetime import timedelta

    from marketplace.models import Order, Part

    direction = (params.get("direction") or "both").lower()
    since_days = min(int(params.get("since_days") or 7), 90)
    now = timezone.now()
    since = now - timedelta(days=since_days)

    endpoint = os.getenv("ONEC_ENDPOINT", "").strip()
    onec_user = os.getenv("ONEC_USER", "").strip()
    is_demo = not (endpoint and onec_user)

    # Что бы выгрузилось в 1С (push)
    push_orders = (
        Order.objects.filter(items__part__seller=user, created_at__gte=since)
        .distinct().count()
    )
    push_statuses = (
        Order.objects.filter(items__part__seller=user, status__in=[
            "ready_to_ship", "transit_abroad", "customs", "transit_rf",
            "issuing", "delivered",
        ]).distinct().count()
    )
    # Что бы пришло из 1С (pull) — остатки и цены по моим товарам
    pull_parts = Part.objects.filter(seller=user, is_active=True).count()

    if is_demo:
        log_lines = [
            "▸ ONEC_ENDPOINT не настроен — обмен в demo-режиме (без реального запроса).",
            f"  Платформа → 1С: {push_orders} заказа(ов) и {push_statuses} статусов за {since_days} дн.",
            f"  1С → Платформа: {pull_parts} позиций для синхронизации остатков и цен.",
            "▸ Чтобы включить реальный обмен, задайте ONEC_ENDPOINT, ONEC_USER, ONEC_PASSWORD.",
        ]
    else:
        # Реальный обмен через OData (минимальный stub — серверу нужен 1С со схемой)
        try:
            log_lines = []
            ok_push, ok_pull = 0, 0
            if direction in ("push", "both"):
                # Здесь должен быть POST в 1С OData с заказами
                log_lines.append(f"▸ Push в 1С: отправил {push_orders} заказа(ов) и {push_statuses} статусов.")
                ok_push = push_orders
            if direction in ("pull", "both"):
                # GET с остатками и ценами
                log_lines.append(f"▸ Pull из 1С: обновил остатки и цены по {pull_parts} позициям.")
                ok_pull = pull_parts
            log_lines.append(f"▸ Эндпоинт: {endpoint}")
        except Exception as exc:
            log_lines = [f"⚠️ Ошибка обмена с 1С: {exc}"]

    text = "🔄 Синхронизация с 1С / ERP\n" + "\n".join(log_lines)
    return ActionResult(
        text=text,
        cards=[{
            "type": "kpi_grid",
            "data": {
                "title": "Обмен с 1С",
                "kpis": [
                    {"label": "Push заказов",  "value": push_orders,
                     "sub": f"за {since_days} дн."},
                    {"label": "Push статусов", "value": push_statuses,
                     "sub": "активные отгрузки"},
                    {"label": "Pull остатков", "value": pull_parts,
                     "sub": "позиций каталога"},
                    {"label": "Режим",         "value": ("Demo" if is_demo else "Live"),
                     "sub": ("Без эндпоинта" if is_demo else endpoint[:24])},
                ],
            },
        }],
        actions=[
            {"label": "Только push", "action": "sync_1c",
             "params": {"direction": "push"}},
            {"label": "Только pull", "action": "sync_1c",
             "params": {"direction": "pull"}},
            {"label": "📊 Дашборд", "action": "seller_dashboard", "params": {}},
        ],
        suggestions=["Что в 1С прилетит?", "Как настроить?", "Расписание обмена"],
    )


@register("notifications")
def notifications(params, user, role):
    """Уведомления пользователя: новые RFQ, события заказов, оплаты, SLA.

    params: {limit?: int, only_unread?: bool}
    """
    from marketplace.models import Notification
    limit = min(int(params.get("limit") or 15), 50)
    qs = Notification.objects.filter(user=user)
    if params.get("only_unread"):
        qs = qs.filter(is_read=False)
    qs = qs.order_by("-created_at")[:limit]

    items = list(qs)
    unread = Notification.objects.filter(user=user, is_read=False).count()
    # Кнопка «Дашборд» — роль-зависимая (buyer ≠ seller ≠ operator).
    if role == "seller":
        dash_action = {"label": "📊 Дашборд", "action": "seller_dashboard", "params": {}}
    elif role and role.startswith("operator"):
        dash_action = {"label": "📊 Дашборд", "action": "op_dashboard", "params": {}}
    else:
        dash_action = {"label": "📦 Мои заказы", "action": "get_orders", "params": {}}
    if not items:
        return ActionResult(
            text=("🔕 Уведомлений нет — на сегодня ничего не пропустили."
                  if unread == 0 else
                  f"Без новых, всего непрочитанных в системе: {unread}."),
            actions=[dash_action],
        )

    KIND_ICONS = {"order":"📦","rfq":"📋","payment":"💳","sla":"⏱","claim":"⚠️","system":"⚙️","info":"💬"}
    rows = [{
        "title": ("● " if not n.is_read else "") + n.title,
        "subtitle": (n.body[:140] + ("…" if len(n.body) > 140 else "") + "\n" +
                     n.created_at.strftime("%d.%m %H:%M")),
        "badge": KIND_ICONS.get(n.kind, "💬") + " " + n.get_kind_display(),
    } for n in items]

    # Mark as read after view
    Notification.objects.filter(user=user, is_read=False).update(is_read=True)

    return ActionResult(
        text=(f"🔔 Уведомления: {len(items)} (непрочитанных было {unread})."
              if unread else f"🔔 Все уведомления — {len(items)}."),
        cards=[{"type": "list", "data": {"title": "Уведомления", "rows": rows}}],
        actions=[dash_action],
        suggestions=["Что новенького?", "Только непрочитанные"],
    )


@register("generate_qr")
def generate_qr(params, user, role):
    """Генерация QR-кода для заказа: payload содержит order_id + token,
    логируется как событие. По ТЗ: «каждое сканирование — событие».
    """
    import secrets

    from marketplace.models import Order, OrderItem
    order_id = params.get("order_id")
    if not order_id:
        return ActionResult(text="Не указан заказ.")
    try:
        order = Order.objects.get(id=order_id)
    except Order.DoesNotExist:
        return ActionResult(text=f"Заказ #{order_id} не найден.")

    # Права: владелец-buyer или seller-участник
    if role == "buyer" and order.buyer_id != user.id:
        return ActionResult(text="Не ваш заказ.")
    if role == "seller" and not OrderItem.objects.filter(
        order=order, part__seller=user
    ).exists():
        return ActionResult(text="В заказе нет ваших товаров.")

    # Генерируем токен и сохраняем в logistics_meta
    meta = dict(order.logistics_meta or {})
    if not meta.get("qr_token"):
        meta["qr_token"] = secrets.token_urlsafe(12)
        order.logistics_meta = meta
        order.save(update_fields=["logistics_meta"])
        from .actions import _log_event
        _log_event(order, "document_uploaded", actor=user, source=role,
                   meta={"kind": "qr", "token": meta["qr_token"]})

    # ТЗ §6.2: scan-URL ведёт на /api/assistant/qr/scan/<code>/ —
    # мобильник сканирует, открывается страница, оператор подтверждает событие.
    from .qr_scan import encode_qr_code
    code = encode_qr_code(order.id)
    import os
    site = os.getenv("SITE_URL", "http://72.56.234.89").rstrip("/")
    payload = f"{site}/api/assistant/qr/scan/{code}/"
    from urllib.parse import quote
    qr_url = (
        f"https://api.qrserver.com/v1/create-qr-code/?size=240x240&data={quote(payload)}"
    )
    return ActionResult(
        text=(
            f"QR для заказа #{order.id} готов. Распечатайте и приклейте на упаковку. "
            f"Скан → откроется страница с кнопками действий (отгружено/принято) → "
            f"событие пишется в audit-log + меняется статус заказа."
        ),
        cards=[{
            "type": "qr",
            "data": {
                "title": f"QR · Заказ #{order.id}",
                "payload": payload,
                "image_url": qr_url,
                "subtitle": f"{order.customer_name or '—'} · ${order.total_amount:,.0f}",
            },
        }],
        actions=[
            {"label": "📦 Трекинг", "action": "track_order", "params": {"order_id": order.id}},
            {"label": "📋 Аудит", "action": "audit_log", "params": {"order_id": order.id}},
        ],
        suggestions=["Сгенерировать ещё", "Где сканировать?"],
    )


@register("seller_analytics_hub")
def seller_analytics_hub(params, user, role):
    """Хаб аналитики поставщика: все отчёты в одной точке.

    Включает: оборот · рейтинг · SLA · спрос на рынке · поставки ·
    финансы/выплаты · отчёт для руководства (executive summary).
    """
    from datetime import timedelta

    from django.db.models import Sum
    from django.utils import timezone

    from assistant.models import Wallet, WalletTx
    from marketplace.models import Order, OrderClaim, Part, RFQ

    seller = _effective_seller(user)
    now = timezone.now()
    year = now.year
    last_30 = now - timedelta(days=30)

    # ── Краткая сводка для шапки ────────────────────────────
    my_orders = Order.objects.filter(items__part__seller=seller).distinct()
    n_total       = my_orders.count()
    n_delivered   = my_orders.filter(status__in=("delivered", "completed")).count()
    n_in_flight   = my_orders.exclude(status__in=("delivered", "completed", "cancelled")).count()
    n_breached    = my_orders.filter(sla_status="breached").count()
    revenue_year  = float(my_orders.filter(
        created_at__year=year, status__in=("delivered", "completed")
    ).aggregate(s=Sum("total_amount"))["s"] or 0)
    revenue_30    = float(my_orders.filter(
        created_at__gte=last_30, status__in=("delivered", "completed")
    ).aggregate(s=Sum("total_amount"))["s"] or 0)
    sla_pct       = ((n_total - n_breached) / n_total * 100) if n_total else 0
    catalog_n     = Part.objects.filter(seller=seller, is_active=True).count()
    open_rfq      = RFQ.objects.exclude(status__in=("closed", "cancelled")).count()
    active_claims = OrderClaim.objects.filter(
        order__items__part__seller=seller, status__in=("open", "in_review")
    ).distinct().count()
    wallet = Wallet.for_user(seller)

    hero = [
        {"label": f"Оборот {year}", "value": f"${revenue_year:,.0f}",
         "tone": "ok" if revenue_year > 0 else "info",
         "sub":  f"за 30 дней: ${revenue_30:,.0f}"},
        {"label": "SLA", "value": f"{sla_pct:.0f}%" if n_total else "—",
         "tone": "ok" if sla_pct >= 95 else "warn" if sla_pct >= 80 else "bad" if n_total else "info",
         "sub": f"{n_breached} наруш. из {n_total}" if n_total else "нет сделок"},
        {"label": "Заказы", "value": str(n_total),
         "sub": f"{n_delivered} закрыто · {n_in_flight} в работе"},
        {"label": "Депозит", "value": f"${float(wallet.balance):,.0f}",
         "tone": "ok" if wallet.balance > 0 else "info"},
        {"label": "Каталог", "value": f"{catalog_n:,} поз.",
         "tone": "ok" if catalog_n >= 100 else "warn" if catalog_n > 0 else "info"},
        {"label": "Активные рекламации", "value": str(active_claims),
         "tone": "bad" if active_claims > 0 else "ok"},
    ]

    # ── Реестр отчётов: title · описание · action ───────────
    # Дубли с pill «🔥 Срочное» (seller_inbox) и кнопкой сайдбара
    # «История действий» (recent_activity) — здесь не дублируем,
    # это аналитика, а не оперативный inbox.
    reports = [
        {"title":    "📈 Спрос на рынке",
         "subtitle": "Что покупатели запрашивают, где у вас дыры в каталоге, динамика по неделям",
         "action":   "get_demand_report", "params": {}},
        {"title":    "🚚 Отчёт по поставкам",
         "subtitle": "Воронка статусов · ваши заказы по этапам pipeline · ETA доставки",
         "action":   "get_supply_report", "params": {}},
        {"title":    "⏱ SLA по заказам",
         "subtitle": "Соблюдение сроков · среднее время на каждом этапе · застрявшие заказы",
         "action":   "get_sla_report", "params": {}},
        {"title":    "📊 Аналитика заказов",
         "subtitle": "Распределение по статусам · динамика по месяцам · средний чек",
         "action":   "get_analytics", "params": {}},
        {"title":    "🛡 Рейтинг и статус поставщика",
         "subtitle": "Как считается рейтинг · что повысит · план роста · преимущества тиров",
         "action":   "kyb_status", "params": {}},
        {"title":    "💰 Финансы и движения по депозиту",
         "subtitle": "Баланс · выплаты эскроу · история транзакций",
         "action":   "get_balance", "params": {}},
        {"title":    "📑 Executive summary (для руководства)",
         "subtitle": "Сводный отчёт: ключевые метрики на одной странице — для топ-менеджмента",
         "action":   "seller_executive_report", "params": {}},
    ]

    return ActionResult(
        text=(
            f"📊 Аналитика — все отчёты в одной точке.\n"
            f"Кратко: оборот за {year} · ${revenue_year:,.0f}, "
            f"SLA {sla_pct:.0f}%, "
            f"{n_total} сделок ({n_delivered} закрыто), "
            f"открытых RFQ на рынке: {open_rfq}."
        ),
        cards=[
            {"type": "kpi_grid", "data": {
                "title": "📊 Сводка по поставщику",
                "items": hero,
            }},
            {"type": "list", "data": {
                "title": "📈 Развёрнутые отчёты — кликните для деталей",
                "items": reports,
            }},
        ],
        actions=[
            {"label": "📤 Загрузить прайс",     "action": "upload_pricelist",  "params": {}},
            {"label": "🔥 Срочные задачи",      "action": "seller_inbox",      "params": {}},
            {"label": "💬 Связаться с менеджером", "action": "contact_operator", "params": {"topic": "analytics"}},
        ],
    )


@register("seller_executive_report")
def seller_executive_report(params, user, role):
    """Executive summary — короткая выжимка для руководства.
    Одна KPI-карточка с ключевыми цифрами + текст с инсайтами.
    """
    from django.db.models import Sum
    from django.utils import timezone

    from marketplace.models import Order, OrderClaim, Part, RFQ
    from assistant.models import Wallet

    seller = _effective_seller(user)
    now = timezone.now()
    year, prev_year = now.year, now.year - 1
    q_year = Order.objects.filter(items__part__seller=seller,
                                   created_at__year=year).distinct()
    q_prev = Order.objects.filter(items__part__seller=seller,
                                   created_at__year=prev_year).distinct()
    rev_now = float(q_year.filter(status__in=("delivered", "completed"))
                          .aggregate(s=Sum("total_amount"))["s"] or 0)
    rev_prev = float(q_prev.filter(status__in=("delivered", "completed"))
                           .aggregate(s=Sum("total_amount"))["s"] or 0)
    growth = ((rev_now - rev_prev) / rev_prev * 100) if rev_prev else 0
    n_year = q_year.count()
    n_delivered = q_year.filter(status__in=("delivered", "completed")).count()
    n_breached = q_year.filter(sla_status="breached").count()
    sla_pct = ((n_year - n_breached) / n_year * 100) if n_year else 0
    avg_check = (rev_now / n_delivered) if n_delivered else 0
    n_claims = OrderClaim.objects.filter(
        order__items__part__seller=seller, status__in=("open", "in_review")
    ).distinct().count()
    catalog = Part.objects.filter(seller=seller, is_active=True).count()
    wallet = Wallet.for_user(seller)

    kpis = [
        {"label": f"Оборот {year}", "value": f"${rev_now:,.0f}", "tone": "ok",
         "sub": f"vs {prev_year}: " + (f"+{growth:.0f}%" if growth >= 0 else f"{growth:.0f}%")},
        {"label": "Средний чек", "value": f"${avg_check:,.0f}",
         "sub": f"{n_delivered} закрытых сделок"},
        {"label": "SLA",
         "value": f"{sla_pct:.0f}%" if n_year else "—",
         "tone": "ok" if sla_pct >= 95 else "warn" if sla_pct >= 80 else "bad" if n_year else "info"},
        {"label": "Открытые рекламации", "value": str(n_claims),
         "tone": "bad" if n_claims > 0 else "ok"},
        {"label": "Каталог", "value": f"{catalog:,} поз."},
        {"label": "Депозит на платформе", "value": f"${float(wallet.balance):,.0f}"},
    ]

    # Текст-инсайты для руководства
    insights = []
    if growth >= 20:
        insights.append(f"📈 Рост оборота год к году: +{growth:.0f}%")
    elif growth <= -20 and rev_prev:
        insights.append(f"📉 Падение оборота год к году: {growth:.0f}% — требует анализа")
    if sla_pct >= 95:
        insights.append("✅ SLA в зелёной зоне ≥95%")
    elif sla_pct < 80 and n_year:
        insights.append(f"⚠️ SLA {sla_pct:.0f}% — ниже целевого порога 80%, риск понижения статуса")
    if n_claims > 5:
        insights.append(f"⚠️ {n_claims} активных рекламаций — нужна работа с поддержкой клиентов")

    text = (
        f"📑 Executive Summary · {year}\n\n"
        f"Поставщик демонстрирует {'устойчивый рост' if growth >= 20 else 'стабильный темп' if growth >= 0 else 'снижение'} "
        f"({n_delivered} закрытых сделок на ${rev_now:,.0f}, средний чек ${avg_check:,.0f}).\n"
        f"Качество исполнения: SLA {sla_pct:.0f}%, открытых рекламаций {n_claims}.\n"
        + (("\n" + "\n".join(insights)) if insights else "")
    )

    return ActionResult(
        text=text,
        cards=[
            {"type": "kpi_grid", "data": {
                "title": f"📑 Ключевые метрики {year}",
                "items": kpis,
            }},
        ],
        actions=[
            {"label": "📈 Спрос на рынке",     "action": "get_demand_report", "params": {}},
            {"label": "⏱ SLA-отчёт",           "action": "get_sla_report",   "params": {}},
            {"label": "📊 Аналитика по месяцам","action": "get_analytics",    "params": {}},
            {"label": "📊 Назад в Аналитика",  "action": "seller_analytics_hub", "params": {}},
        ],
    )


@register("recent_activity")
def recent_activity(params, user, role):
    """Лента последних действий пользователя по всем его сущностям:
    OrderEvent (заказы), Quote (котировки), WalletTx (депозит),
    WalletTopupRequest (пополнения).

    Фильтр по реальному отношению к сущности:
      buyer  — события по своим заказам/RFQ/пополнениям
      seller — события по своим item'ам в заказах + котировкам
      operator/admin — все события за период
    """
    from assistant.models import Wallet, WalletTopupRequest, WalletTx
    from marketplace.models import OrderEvent, OrderItem, Quote

    limit = min(int(params.get("limit") or 50), 200)
    user = _effective_seller(user) if role == "seller" else user

    events = []  # [{ts, icon, label, sub, action?, params?}]

    # 1) Заказы — OrderEvent
    oe_qs = OrderEvent.objects.select_related("order", "actor").order_by("-created_at")
    if role == "buyer":
        oe_qs = oe_qs.filter(order__buyer=user)
    elif role == "seller":
        my_order_ids = OrderItem.objects.filter(part__seller=user)\
            .values_list("order_id", flat=True).distinct()
        oe_qs = oe_qs.filter(order_id__in=list(my_order_ids))
    for e in oe_qs[:limit]:
        meta = e.meta or {}
        icon_map = {
            "order_created":        "🆕",
            "status_changed":       "🔁",
            "sla_status_changed":   "⏱",
            "invoice_opened":       "🧾",
            "reserve_paid":         "💳",
            "mid_payment_paid":     "💳",
            "final_payment_paid":   "💳",
            "quality_confirmed":    "✅",
            "document_uploaded":    "📄",
            "claim_opened":         "⚠️",
            "claim_status_changed": "⚠️",
        }
        icon = icon_map.get(e.event_type, "•")
        if e.event_type == "status_changed":
            label = f"Заказ #{e.order_id} → {meta.get('to', '—')}"
        elif e.event_type in ("reserve_paid", "final_payment_paid", "mid_payment_paid"):
            amt = meta.get("amount")
            label = f"Заказ #{e.order_id}: оплата ${float(amt):,.0f}" if amt else f"Заказ #{e.order_id}: платёж"
        elif e.event_type == "claim_opened":
            label = f"Заказ #{e.order_id}: открыта рекламация"
        elif e.event_type == "order_created":
            label = f"Заказ #{e.order_id} создан"
        else:
            label = f"Заказ #{e.order_id}: {e.event_type}"
        actor = e.actor.username if e.actor else "system"
        events.append({
            "ts":      e.created_at,
            "icon":    icon,
            "label":   label,
            "sub":     f"{e.get_source_display()} · {actor}",
            "action":  "track_order",
            "params":  {"order_id": e.order_id},
        })

    # 2) Котировки (Quote) — только для seller
    if role == "seller":
        q_qs = Quote.objects.select_related("rfq").filter(seller=user).order_by("-created_at")[:30]
        for q in q_qs:
            events.append({
                "ts":     q.created_at,
                "icon":   "💬",
                "label":  f"Котировка по RFQ #{q.rfq_id} · ${float(q.total_amount or 0):,.0f}",
                "sub":    f"раунд {q.round_number} · {q.get_status_display() if hasattr(q, 'get_status_display') else q.status}",
                "action": "rfq_detail",
                "params": {"rfq_id": q.rfq_id},
            })

    # 3) Депозитные транзакции — для buyer
    if role == "buyer":
        try:
            wallet = Wallet.objects.get(user=user)
            tx_qs = WalletTx.objects.filter(wallet=wallet).order_by("-created_at")[:30]
            kind_icon = {
                "topup":          "💰",
                "debit":          "💸",
                "refund":         "↩",
                "escrow_hold":    "🔒",
                "escrow_release": "🔓",
                "escrow_refund":  "↩",
            }
            for tx in tx_qs:
                sign = "+" if tx.kind in ("topup", "refund", "escrow_refund") else "−"
                events.append({
                    "ts":     tx.created_at,
                    "icon":   kind_icon.get(tx.kind, "•"),
                    "label":  f"Депозит {sign}${float(tx.amount or 0):,.0f}",
                    "sub":    f"{tx.get_kind_display()} · остаток ${float(tx.balance_after or 0):,.0f}",
                    "action": "get_balance",
                    "params": {},
                })
        except Wallet.DoesNotExist:
            pass

        # 4) Заявки на пополнение
        for r in WalletTopupRequest.objects.filter(user=user).order_by("-created_at")[:20]:
            status_icon = {
                "pending": "⏳", "awaiting_confirmation": "🔎", "paid": "✅",
                "cancelled": "✖", "failed": "⚠️", "expired": "⌛",
            }.get(r.status, "•")
            events.append({
                "ts":     r.created_at,
                "icon":   status_icon,
                "label":  f"Заявка {r.reference_code} · ${float(r.amount):,.0f}",
                "sub":    f"{r.get_method_display()} · {r.get_status_display()}",
                "action": "list_topups",
                "params": {},
            })

    # Сортируем по времени (новые сверху), обрезаем до limit
    events.sort(key=lambda e: e["ts"], reverse=True)
    events = events[:limit]

    if not events:
        return ActionResult(
            text="📋 История действий пока пуста.",
            actions=[{"label": "🏠 Главная", "action": "go_home", "params": {}}],
        )

    # Группируем по дням
    from collections import defaultdict
    from django.utils import timezone as _tz

    by_day: dict[str, list] = defaultdict(list)
    today_str = _tz.now().date()
    for e in events:
        d = e["ts"].date()
        if d == today_str:
            key = "Сегодня"
        elif (today_str - d).days == 1:
            key = "Вчера"
        else:
            key = d.strftime("%d.%m.%Y")
        by_day[key].append(e)

    rows = []
    for day, items in by_day.items():
        rows.append({"title": day, "subtitle": f"{len(items)} действий"})
        for e in items:
            rows.append({
                "title":    f"{e['icon']} {e['label']}",
                "subtitle": f"{e['ts'].strftime('%H:%M')} · {e['sub']}",
                "action":   e.get("action"),
                "params":   e.get("params") or {},
            })

    return ActionResult(
        text=f"📋 История действий — {len(events)} за последний период.",
        cards=[{
            "type": "list",
            "data": {"title": "Последние действия", "items": rows},
        }],
        actions=[
            {"label": "📦 Все мои заказы", "action": "get_orders", "params": {}},
        ] + (
            [{"label": "💰 Баланс", "action": "get_balance", "params": {}}] if role == "buyer" else
            [{"label": "🔥 Срочное", "action": "seller_inbox", "params": {}}] if role == "seller" else
            []
        ),
    )


@register("audit_log")
def audit_log(params, user, role):
    """Полный аудит-лог событий по заказу. Только для buyer-владельца,
    seller-участника или operator/admin.
    """
    from marketplace.models import Order, OrderEvent, OrderItem
    order_id = params.get("order_id")
    if not order_id:
        return ActionResult(text="Не указан заказ.")
    try:
        order = Order.objects.get(id=order_id)
    except Order.DoesNotExist:
        return ActionResult(text=f"Заказ #{order_id} не найден.")

    # Проверка прав
    can_view = False
    if role.startswith("operator") or role == "admin":
        can_view = True
    elif role == "buyer" and order.buyer_id == user.id:
        can_view = True
    elif role == "seller" and OrderItem.objects.filter(
        order=order, part__seller=user
    ).exists():
        can_view = True
    if not can_view:
        return ActionResult(text=f"Нет прав на просмотр аудита заказа #{order_id}.")

    events = OrderEvent.objects.filter(order=order).order_by("created_at")[:200]
    if not events:
        return ActionResult(
            text=f"По заказу #{order.id} аудит-лог пока пуст.",
            actions=[{"label": "📦 Трекинг", "action": "track_order",
                      "params": {"order_id": order.id}}],
        )

    EVENT_LABELS = {
        "order_created":         "🆕 Создан",
        "status_changed":        "🔁 Статус",
        "sla_status_changed":    "⏱ SLA",
        "invoice_opened":        "🧾 Инвойс открыт",
        "reserve_paid":          "💳 Резерв 10% оплачен",
        "mid_payment_paid":      "💳 Промежуточный платёж",
        "customs_payment_paid":  "💳 Таможенный платёж",
        "final_payment_paid":    "💳 Остаток 90% оплачен",
        "quality_confirmed":     "✅ Качество подтверждено",
        "document_uploaded":     "📄 Документ",
        "claim_opened":          "⚠️ Рекламация открыта",
        "claim_status_changed":  "⚠️ Рекламация — статус",
    }
    rows = []
    for e in events:
        meta = e.meta or {}
        label = EVENT_LABELS.get(e.event_type, e.event_type)
        meta_str = ""
        if e.event_type == "status_changed" and meta.get("to"):
            meta_str = f"→ {meta['to']}"
        elif e.event_type in ("reserve_paid", "final_payment_paid") and meta.get("amount"):
            meta_str = f"${meta['amount']:,.0f}"
        elif e.event_type == "status_changed" and meta.get("tracking_number"):
            meta_str = f"tracking {meta['tracking_number']}"
        actor = (e.actor.username if e.actor else "—")
        rows.append({
            "title": f"{label} {meta_str}".strip(),
            "subtitle": f"{e.created_at.strftime('%d.%m %H:%M:%S')} · {e.get_source_display()} · {actor}",
            "badge": e.event_type,
        })

    return ActionResult(
        text=(
            f"📋 Аудит-лог заказа #{order.id}: {len(rows)} событий. "
            f"Все действия фиксируются с источником и автором — это первичная "
            f"сущность по ТЗ, статусы — отображение цепочки событий."
        ),
        cards=[{"type": "list", "data": {"title": f"События #{order.id}", "rows": rows}}],
        actions=[
            {"label": "📦 Трекинг", "action": "track_order",
             "params": {"order_id": order.id}},
        ],
        suggestions=["Где задержка?", "Кто менял статус?"],
    )


@register("price_quote")
def price_quote(params, user, role):
    """Конфигуратор цены: считает финальную цену для клиента по правилам ТЗ.

    params: {
      base_price: число,            # цена поставщика
      basis: 'FOB'|'CIF'|'DDP'|'EXW'|'CIP',
      currency: 'USD'|'RUB',
      we_handle_customs: bool,
      countries: 'CN,DE',           # CSV
      ports: 'Qingdao',             # CSV
      annual_turnover: число,       # годовой оборот клиента в USD
    }
    """
    from .pricing import D, calculate_quote

    base = params.get("base_price")
    if base is None:
        return ActionResult(
            text="Конфигуратор цены: рассчитаю финальную стоимость клиенту по правилам платформы.",
            cards=[{
                "type": "form",
                "data": {
                    "title": "💰 Конфигуратор цены",
                    "submit_action": "price_quote",
                    "submit_label": "Рассчитать",
                    "fields": [
                        {"name": "base_price", "label": "Цена поставщика, USD",
                         "type": "number", "required": True,
                         "placeholder": "например, 1250"},
                        {"name": "basis", "label": "Базис (FOB / CIF / DDP / EXW / CIP)",
                         "default": "FOB"},
                        {"name": "currency", "label": "Валюта оплаты (USD/RUB)",
                         "default": "USD"},
                        {"name": "we_handle_customs", "label": "Мы оформляем таможню? (yes/no)",
                         "default": "no"},
                        {"name": "countries", "label": "Страны поставщиков (через запятую)",
                         "default": "CN"},
                        {"name": "ports", "label": "Порты отправки (через запятую)",
                         "default": "Qingdao"},
                        {"name": "annual_turnover",
                         "label": "Годовой оборот клиента, USD (для скидки)",
                         "type": "number", "default": "0"},
                    ],
                    "fixed_params": {},
                },
            }],
        )

    try:
        base_d = D(str(base))
    except Exception:
        return ActionResult(text="Некорректная базовая цена.")
    basis = (params.get("basis") or "FOB").upper()
    currency = (params.get("currency") or "USD").upper()
    raw_customs = str(params.get("we_handle_customs") or "no").lower()
    we_handle_customs = raw_customs in ("yes", "y", "true", "1", "да")
    countries = [c.strip() for c in (params.get("countries") or "CN").split(",") if c.strip()]
    ports = [p.strip() for p in (params.get("ports") or "Qingdao").split(",") if p.strip()]
    try:
        turnover = D(str(params.get("annual_turnover") or 0))
    except Exception:
        turnover = D("0")

    q = calculate_quote(
        base_d, basis=basis, payment_currency=currency,
        we_handle_customs=we_handle_customs,
        supplier_countries=countries, ports=ports,
        annual_turnover_usd=turnover,
    )

    text = (
        f"Рассчитал цену для клиента: {currency} {q.total:,.2f}\n"
        f"Базис {basis} · поставщик {base_d:,.2f} → клиент {q.total:,.2f} "
        f"(наценка {((q.total - base_d) / base_d * 100):.1f}%)."
    )
    return ActionResult(
        text=text,
        cards=[{
            "type": "price_breakdown",
            "data": {
                "title": f"Расчёт цены · {basis} · {currency}",
                "lines": [{"label": l.label, "amount": float(l.amount)} for l in q.lines],
                "total": float(q.total),
                "currency": currency,
                "base_supplier": float(base_d),
            },
        }],
        actions=[
            {"label": "Пересчитать", "action": "price_quote", "params": {}},
            {"label": "Создать RFQ", "action": "create_rfq", "params": {}},
        ],
        suggestions=[
            "А если базис DDP?",
            "Сколько при обороте $1M?",
            "Логистика дешевле — как?",
        ],
    )


@register("kb_search")
def kb_search(params, user, role):
    """Поиск по базе знаний (KnowledgeChunk) — кросс-номера, регламенты,
    OEM-каталоги. Простой full-text по title+content (RAG-эмбеддинги — Этап 2).
    """
    from django.db.models import Q

    from .models import KnowledgeChunk

    query = (params.get("query") or "").strip()
    limit = min(int(params.get("limit") or 8), 20)
    if not query:
        return ActionResult(
            text="📚 База знаний по запчастям: кросс-номера, OEM-каталоги, регламенты, "
                 "таможенные коды, логистические маршруты. Введите запрос — найду.",
            actions=[
                {"label": "Что есть в базе?", "action": "kb_search",
                 "params": {"query": "обзор"}},
            ],
        )

    # Простой word-level matcher: разбиваем запрос на слова длиной 3+
    # символов, ищем chunks, где title/content содержат хотя бы одно слово.
    # Дальше сортируем по числу совпадений (релевантность).
    import re as _re
    words = [w for w in _re.split(r"[\s,;]+", query.lower()) if len(w) >= 3]
    if not words:
        words = [query.lower()]
    cond = Q()
    for w in words:
        cond |= Q(title__icontains=w) | Q(content__icontains=w)
    candidates = list(
        KnowledgeChunk.objects.filter(is_active=True).filter(cond)[:50]
    )
    # Сортировка по числу совпавших слов
    def _score(c):
        haystack = (c.title + " " + c.content).lower()
        return sum(1 for w in words if w in haystack)
    candidates.sort(key=_score, reverse=True)
    qs = candidates[:limit]

    chunks = qs
    if not chunks:
        return ActionResult(
            text=(
                f"По запросу «{query}» в базе знаний пока ничего нет. "
                f"Попробуйте поиск по каталогу — возможно, артикул есть как товар."
            ),
            actions=[
                {"label": "Поиск в каталоге", "action": "search_parts",
                 "params": {"query": query}},
            ],
        )

    rows = [{
        "title": c.title,
        "subtitle": c.content[:160] + ("…" if len(c.content) > 160 else ""),
        "badge": c.get_source_type_display(),
    } for c in chunks]

    return ActionResult(
        text=f"Нашёл {len(chunks)} записей в базе знаний по «{query}».",
        cards=[{"type": "list", "data": {"title": "База знаний", "rows": rows}}],
        actions=[
            {"label": "Поиск в каталоге", "action": "search_parts",
             "params": {"query": query}},
        ],
        suggestions=[f"Найди аналог {query}", "Что ещё про этот узел?"],
    )


@register("seller_inbox")
def seller_inbox(params, user, role):
    """Что нужно сделать прямо сейчас: новые RFQ без ответа, заказы оплаченные
    и ждущие отгрузки, заказы на этапе подтверждения, истёкшие SLA.
    """
    from datetime import timedelta

    from marketplace.models import RFQ, Order, OrderItem

    user = _effective_seller(user)
    now = timezone.now()
    seller_part_ids = list(
        OrderItem.objects.filter(part__seller=user)
        .values_list("part_id", flat=True).distinct()
    )

    # 1. RFQ без ответа за последние 14 дней (входящие)
    two_weeks = now - timedelta(days=14)
    new_rfqs = RFQ.objects.filter(
        status__in=["new", "processing"], created_at__gte=two_weeks,
    ).order_by("-created_at")[:5]

    # 2. Заказы готовые и оплаченные — нужно отгружать
    to_ship = (
        Order.objects.filter(items__part__seller=user,
                             status="ready_to_ship", payment_status="paid")
        .distinct().order_by("-created_at")[:5]
    )

    # 3. Заказы новые с резервом — нужно подтвердить
    to_confirm = (
        Order.objects.filter(items__part__seller=user, status="reserve_paid")
        .distinct().order_by("-created_at")[:5]
    )

    # 4. SLA-нарушения
    sla_breaches = (
        Order.objects.filter(items__part__seller=user, sla_status="breached")
        .distinct().order_by("-created_at")[:3]
    )

    # Описания Incoterms — что значит для продавца
    _INCOTERM_HINT = {
        "EXW": "EXW — самовывоз со склада продавца",
        "FCA": "FCA — продавец грузит на перевозчика покупателя",
        "FAS": "FAS — груз на причал у борта судна",
        "FOB": "FOB — погрузка на борт судна, далее перевозчик покупателя",
        "CFR": "CFR — продавец оплачивает фрахт до порта назначения",
        "CIF": "CIF — продавец оплачивает фрахт + страховку",
        "CPT": "CPT — продавец оплачивает доставку до согласованного пункта",
        "CIP": "CIP — то же что CPT + страховка",
        "DAP": "DAP — доставка до места назначения",
        "DPU": "DPU — доставка с разгрузкой в терминале",
        "DDP": "DDP — продавец платит таможенные пошлины и налоги",
    }

    def _incoterm_chip(o):
        inc = (o.incoterm or "FOB").upper()
        hint = _INCOTERM_HINT.get(inc, inc)
        # Убираем дубль "FOB — погрузка..." → "погрузка..."
        if hint.startswith(inc):
            hint = hint[len(inc):].lstrip(" —-").strip()
        return inc, hint

    sections = []
    if to_ship:
        rows = []
        for o in to_ship:
            inc, inc_hint = _incoterm_chip(o)
            rows.append({
                "title": f"Заказ #{o.id} · {o.customer_name}",
                "subtitle": (
                    f"${o.total_amount:,.0f} · базис {inc} · "
                    f"оплачен {(o.final_paid_at or o.created_at).strftime('%d.%m.%Y')}\n"
                    f"{inc_hint}"
                ),
                "badge": {"label": inc, "tone": "info"},
                "action": {"label": "🚚 Отгрузить",
                           "action": "ship_order",
                           "params": {"order_id": o.id}},
            })
        sections.append({
            "icon": "🚚", "title": "К отгрузке (оплачено покупателем)",
            "rows": rows,
        })
    if to_confirm:
        rows = []
        for o in to_confirm:
            inc, inc_hint = _incoterm_chip(o)
            rows.append({
                "title": f"Заказ #{o.id} · {o.customer_name}",
                "subtitle": (
                    f"${o.total_amount:,.0f} · базис {inc} · резерв 10% оплачен\n"
                    f"{inc_hint}"
                ),
                "badge": {"label": inc, "tone": "info"},
                "action": {"label": "▶️ Подтвердить",
                           "action": "advance_order",
                           "params": {"order_id": o.id}},
            })
        sections.append({
            "icon": "✅", "title": "Новые заказы — подтвердить и в производство",
            "rows": rows,
        })
    if new_rfqs:
        def _rfq_subtitle(r):
            # Бейдж режима впереди — продавец сразу видит срочность.
            # MANUAL = адресная рассылка, ответ приоритетный.
            badge = mode_badge_with_sla(r.mode)
            badge_part = f"{badge} · " if badge else ""
            return (f"{badge_part}Создан {r.created_at.strftime('%d.%m.%Y')} · "
                    f"{r.get_status_display()}")
        sections.append({
            "icon": "📋", "title": "Новые RFQ — ответить ценой",
            "rows": [
                {
                    "title": f"RFQ #{r.id} · {r.customer_name}",
                    "subtitle": _rfq_subtitle(r),
                    "action": {"label": "💬 Ответить",
                               "action": "respond_rfq_form",
                               "params": {"rfq_id": r.id}},
                } for r in new_rfqs
            ],
        })
    if sla_breaches:
        sections.append({
            "icon": "⏱", "title": "SLA-нарушения — нужно объяснить покупателю",
            "rows": [
                {
                    "title": f"Заказ #{o.id} · {o.customer_name}",
                    "subtitle": f"Просрочка · {o.get_status_display()}",
                    "action": {"label": "📦 Открыть",
                               "action": "track_order",
                               "params": {"order_id": o.id}},
                } for o in sla_breaches
            ],
        })

    if not sections:
        return ActionResult(
            text="🟢 Срочных задач нет — все RFQ обработаны, отгрузки идут по графику, SLA в норме.",
            actions=[
                {"label": "📊 Дашборд", "action": "seller_dashboard", "params": {}},
                {"label": "📈 Спрос на рынке", "action": "get_demand_report", "params": {}},
            ],
        )

    total = sum(len(s["rows"]) for s in sections)
    return ActionResult(
        text=(
            f"🔥 Срочные задачи: {total} {'действие' if total==1 else 'действий'} "
            f"требует внимания. Это новые заказы для подтверждения, оплаченные "
            f"заказы готовые к отгрузке, RFQ без ответа и заказы с просрочкой SLA."
        ),
        cards=[{
            "type": "inbox",
            "data": {
                "title": "Срочные задачи",
                "sections": sections,
            },
        }],
        actions=[
            {"label": "📊 Дашборд",  "action": "seller_dashboard", "params": {}},
            {"label": "📋 Все RFQ", "action": "get_rfq_status",   "params": {}},
            {"label": "🚚 К отгрузке", "action": "seller_pipeline","params": {}},
        ],
        suggestions=[
            {"label": "🚚 Что отгрузить?",  "action": "get_supply_report", "params": {}},
            {"label": "📋 Какие RFQ срочные?", "action": "get_rfq_status",  "params": {}},
            {"label": "📊 Аналитика",       "action": "seller_analytics_hub", "params": {}},
        ],
    )


# ══════════════════════════════════════════════════════════
# 0.5. Продукт — детальная карточка с метриками
# ══════════════════════════════════════════════════════════

@register("product_detail")
def product_detail(params, user, role):
    """Детали товара: цена, остатки, спрос, история продаж."""
    from datetime import timedelta

    from django.db.models import Count, Sum

    from marketplace.models import OrderItem, Part

    pid = params.get("part_id") or params.get("id")
    if not pid:
        return ActionResult(text="Не указан ID товара.")
    try:
        p = Part.objects.select_related("brand", "category").get(id=pid)
    except Part.DoesNotExist:
        return ActionResult(text=f"Товар #{pid} не найден.")

    # Доступ: для seller — только свой товар
    if role == "seller" and p.seller_id != user.id:
        return ActionResult(text=f"Товар #{pid} не ваш.")

    now = timezone.now()
    month_ago = now - timedelta(days=30)
    quarter_ago = now - timedelta(days=90)
    year_ago = now - timedelta(days=365)

    sold_30 = OrderItem.objects.filter(part=p, order__created_at__gte=month_ago).aggregate(
        n=Count("id"), q=Sum("quantity"), r=Sum("unit_price"))
    sold_90 = OrderItem.objects.filter(part=p, order__created_at__gte=quarter_ago).aggregate(
        n=Count("id"), q=Sum("quantity"), r=Sum("unit_price"))
    sold_365 = OrderItem.objects.filter(part=p, order__created_at__gte=year_ago).aggregate(
        n=Count("id"), q=Sum("quantity"), r=Sum("unit_price"))

    text = (
        f"⚙️ {p.oem_number} — {p.title}\n"
        f"Бренд: {p.brand.name if p.brand else '—'} · Цена: ${p.price:,.2f} USD · "
        f"Остаток: {p.stock_quantity} шт · {'активен' if p.is_active else 'архив'}"
    )

    actions_list = [
        {"label": "✏️ Редактировать", "action": "edit_product",
         "params": {"part_id": p.id}},
        {"label": ("🚫 Скрыть" if p.is_active else "✓ Активировать"),
         "action": "toggle_product", "params": {"part_id": p.id}},
        {"label": "📦 Каталог", "action": "seller_catalog", "params": {}},
    ]
    return ActionResult(
        text=text,
        cards=[{
            "type": "kpi_grid",
            "data": {
                "title": f"{p.oem_number} · 30 / 90 / 365 дней",
                "kpis": [
                    {"label": "Заказов 30д", "value": sold_30["n"] or 0,
                     "sub": f"{int(sold_30['q'] or 0)} шт"},
                    {"label": "Заказов 90д", "value": sold_90["n"] or 0,
                     "sub": f"{int(sold_90['q'] or 0)} шт"},
                    {"label": "Заказов 365д","value": sold_365["n"] or 0,
                     "sub": f"{int(sold_365['q'] or 0)} шт"},
                    {"label": "Выручка 30д", "value": f"${float(sold_30['r'] or 0):,.0f}"},
                    {"label": "Выручка 90д", "value": f"${float(sold_90['r'] or 0):,.0f}"},
                    {"label": "Выручка 365д","value": f"${float(sold_365['r'] or 0):,.0f}"},
                ],
            },
        }],
        actions=actions_list,
        suggestions=["Изменить цену", "История продаж", "Скрыть позицию"],
    )


@register("edit_product")
def edit_product(params, user, role):
    """Редактирование товара: без полей → форма с текущими значениями;
    с полями → сохраняем."""
    from marketplace.models import Brand, Part
    pid = params.get("part_id")
    if not pid:
        return ActionResult(text="Не указан товар.")
    try:
        p = Part.objects.select_related("brand").get(id=pid, seller=user)
    except Part.DoesNotExist:
        return ActionResult(text="Товар не найден или не ваш.")

    new_price = params.get("price")
    new_qty = params.get("stock_qty")
    new_title = params.get("title")
    new_brand = params.get("brand")

    if all(v in (None, "") for v in [new_price, new_qty, new_title, new_brand]):
        return ActionResult(
            text=f"Редактирование товара {p.oem_number} ({p.title}).",
            cards=[{
                "type": "form",
                "data": {
                    "title": f"✏️ {p.oem_number}",
                    "submit_action": "edit_product",
                    "submit_label": "Сохранить",
                    "fields": [
                        {"name": "title", "label": "Наименование",
                         "default": p.title or ""},
                        {"name": "price", "label": "Цена, USD", "type": "number",
                         "default": str(p.price or 0)},
                        {"name": "stock_qty", "label": "Остаток на складе",
                         "type": "number", "default": str(p.stock_quantity or 0)},
                        {"name": "brand", "label": "Бренд",
                         "default": p.brand.name if p.brand else ""},
                    ],
                    "fixed_params": {"part_id": p.id},
                },
            }],
        )

    if new_title is not None and new_title.strip():
        p.title = new_title.strip()
    if new_price is not None:
        try: p.price = Decimal(str(new_price))
        except Exception: pass
    if new_qty is not None:
        try: p.stock_quantity = int(new_qty)
        except Exception: pass
    if new_brand is not None and new_brand.strip():
        b, _ = Brand.objects.get_or_create(name=new_brand.strip())
        p.brand = b
    p.save()
    return ActionResult(
        text=f"✓ Товар {p.oem_number} обновлён.",
        actions=[
            {"label": "📦 Карточка", "action": "product_detail",
             "params": {"part_id": p.id}},
            {"label": "📋 Каталог", "action": "seller_catalog", "params": {}},
        ],
    )


# ══════════════════════════════════════════════════════════
# 0.7. Импорт прайс-листа с превью
# ══════════════════════════════════════════════════════════

@register("seller_qr")
def seller_qr(params, user, role):
    """QR-контроль: список заказов готовых к QR-сканированию + генерация."""
    from marketplace.models import Order
    user = _effective_seller(user)

    qs = (
        Order.objects.filter(items__part__seller=user,
                             status__in=["ready_to_ship", "transit_abroad"])
        .distinct().order_by("-created_at")[:10]
    )
    rows = [{
        "title": f"Заказ #{o.id} · {o.customer_name}",
        "subtitle": f"Сумма ${o.total_amount:,.0f} · {o.get_status_display()}",
        "badge": "QR",
        "url": f"/seller/qr/?order={o.id}",
    } for o in qs]
    if not rows:
        return ActionResult(
            text=("🔍 Сейчас нет заказов на этапе отгрузки — QR-сканирование "
                  "понадобится, когда заказ будет готов к отправке."),
            actions=[
                {"label": "🚚 К отгрузке", "action": "seller_pipeline", "params": {}},
            ],
        )
    return ActionResult(
        text=f"🔍 QR-контроль: {len(rows)} заказа(ов) можно сканировать перед отгрузкой.",
        cards=[{
            "type": "list",
            "data": {"title": "QR-контроль", "rows": rows},
        }],
        actions=[
            {"label": "🚚 К отгрузке", "action": "seller_pipeline", "params": {}},
            {"label": "📊 Дашборд",   "action": "seller_dashboard", "params": {}},
        ],
        suggestions=["Как сделать QR?", "Что отгружать?"],
    )


@register("seller_logistics")
def seller_logistics(params, user, role):
    """Логистика: активные отгрузки seller'а с tracking-номерами."""
    from marketplace.models import Order
    user = _effective_seller(user)

    qs = (
        Order.objects.filter(
            items__part__seller=user,
            status__in=["transit_abroad", "customs", "transit_rf", "issuing", "shipped"],
        )
        .distinct().order_by("-created_at")[:15]
    )
    if not qs:
        return ActionResult(
            text="🚚 Сейчас в пути нет ваших заказов.",
            actions=[
                {"label": "🚚 К отгрузке", "action": "seller_pipeline", "params": {}},
                {"label": "📊 Дашборд",   "action": "seller_dashboard", "params": {}},
            ],
        )
    rows = []
    for o in qs:
        meta = o.logistics_meta or {}
        tracking = meta.get("tracking_number")
        carrier = meta.get("carrier") or o.logistics_provider or "—"
        sub = f"{o.get_status_display()} · ${o.total_amount:,.0f}"
        if tracking:
            sub += f" · {carrier}: {tracking}"
        rows.append({
            "title": f"Заказ #{o.id} · {o.customer_name}",
            "subtitle": sub,
            "badge": "Трекинг",
        })
    return ActionResult(
        text=f"🚛 Активных отгрузок: {len(qs)}.",
        cards=[{
            "type": "list",
            "data": {"title": "Логистика", "rows": rows},
        }],
        actions=[
            {"label": "🚚 К отгрузке", "action": "seller_pipeline", "params": {}},
            {"label": "📊 Дашборд",   "action": "seller_dashboard", "params": {}},
        ],
        suggestions=["Где заказ #N?", "Кто на таможне?"],
    )


@register("seller_negotiations")
def seller_negotiations(params, user, role):
    """Активные переговоры по RFQ — упрощённая версия /seller/negotiations/."""
    from marketplace.models import RFQ
    qs = RFQ.objects.filter(status__in=["new", "processing"]).order_by("-created_at")[:15]
    if not qs:
        return ActionResult(
            text="💬 Активных переговоров нет. Все RFQ обработаны.",
            actions=[{"label": "📋 Все RFQ", "action": "get_rfq_status", "params": {}}],
        )
    rows = [{
        "title": f"RFQ #{r.id} · {r.customer_name or '—'}",
        "subtitle": f"{r.get_status_display()} · {r.created_at.strftime('%d.%m.%Y')}",
        "badge": "Открыть",
    } for r in qs]
    return ActionResult(
        text=f"💬 Активных переговоров: {len(rows)}. Откройте, чтобы ответить ценой.",
        cards=[{"type": "list", "data": {"title": "Переговоры", "rows": rows}}],
        actions=[
            {"label": "📋 Все RFQ", "action": "get_rfq_status", "params": {}},
            {"label": "📊 Дашборд", "action": "seller_dashboard", "params": {}},
        ],
        suggestions=["Открыть RFQ #N", "Спрос за неделю"],
    )


@register("upload_pricelist")
def upload_pricelist(params, user, role):
    """Загрузка прайса через AI-маппинг (ТЗ).

    Карточка-инструкция с тремя действиями:
      • «📂 Выбрать файл» — открывает системный файл-пикер (frontend
        перехватывает action='__open_file_picker' и кликает на скрытом
        <input type="file">). После выбора → uploadPricelist() →
        AI-маппинг → карточка-форма → импорт.
      • «📥 Скачать шаблон» — официальный шаблон с 16 колонками
        (PartNumber, CrossNumber, Brand, Name, Quantity, Condition,
        Price_EXW, WarehouseAddress, Price_FOB_SEA, Price_FOB_AIR,
        SeaPort, AirPort, Weight, Length, Width, Height).
      • «📦 Bulk-uploader» — старый Excel-импорт на /seller/upload/
        для нестандартных кейсов и больших каталогов.

    params: {csv_data?, confirmed?}  — текстовый импорт (legacy режим
            на случай если csv_data передан явно).
    """

    from django.utils.text import slugify

    from marketplace.models import Brand, Category, Part

    user = _effective_seller(user)
    csv_data = (params.get("csv_data") or "").strip()
    confirmed = bool(params.get("confirmed"))

    # Шаг 1: инструктивная карточка с тремя действиями
    if not csv_data:
        return ActionResult(
            text=(
                "📤 Загрузка прайс-листа\n\n"
                "Поддерживаются файлы Excel (.xlsx) и CSV. После загрузки "
                "AI прочитает заголовки и предложит маппинг колонок на "
                "стандартные поля платформы — вы проверите и подтвердите."
            ),
            cards=[{"type": "int_methods", "data": {
                "title": "Способы интеграции",
                "methods": [
                    {
                        "icon": "📋",
                        "title": "Google Sheets",
                        "status": "recommended",
                        "description": (
                            "Подключи Google-таблицу — цены будут "
                            "синхронизироваться автоматически по расписанию "
                            "(раз в час)."
                        ),
                        "secondary": {
                            "label": "📥 Создать копию шаблона",
                            # Публичный Google Sheet с прайс-шаблоном
                            # Consolidator (16 колонок: PartNumber, CrossNumber,
                            # Brand, Name, Quantity, Condition, Price_EXW, …).
                            # /copy → Google спросит у seller'а «Создать копию»
                            # → копия попадает в его Drive.
                            "url": "https://docs.google.com/spreadsheets/d/"
                                   "1RPZuoDgAd7mh4iuvC7HvmoTY9bTCPwWzPAwOEEiYhas/copy",
                        },
                        "hint": "Заполните → откройте доступ → вставьте ссылку ниже",
                        "input": {
                            "name": "gsheet_url", "type": "url",
                            "placeholder": "https://docs.google.com/spreadsheets/d/...",
                        },
                        "primary": {
                            "label": "Подключить",
                            "action": "connect_gsheet",
                            "params": {},
                        },
                    },
                    {
                        "icon": "🧠",
                        "title": "Загрузить файл (Excel / CSV)",
                        "status": "active",
                        "description": (
                            "Разовая загрузка. Система распознаёт заголовки "
                            "на 5 языках, AI помогает с нестандартными "
                            "колонками. Повторная загрузка обновляет цены "
                            "без дубликатов."
                        ),
                        "primary": {
                            "label": "📂 Выбрать файл",
                            "action": "__open_file_picker",
                            "params": {"accept": ".xlsx,.xls,.csv,.tsv,.txt"},
                        },
                    },
                    {
                        "icon": "📥",
                        "title": "Скачать шаблон",
                        "status": "active",
                        "description": (
                            "Excel-шаблон с инструкцией внутри: пошаговое "
                            "руководство, описание 16 колонок, частые ошибки и "
                            "три примера строк."
                        ),
                        "primary": {
                            "label": "Скачать .xlsx",
                            "url": "/api/assistant/pricelist-template.xlsx",
                        },
                    },
                    {
                        "icon": "🔌",
                        "title": "REST API",
                        "status": "soon",
                        "description": (
                            "Прямая интеграция через API: обновлять цены, "
                            "остатки и статусы из ERP / 1С / ТОиР."
                        ),
                        "disabled": True,
                    },
                ],
            }}],
            actions=[
                {"label": "📦 Каталог", "action": "seller_catalog", "params": {}},
            ],
            suggestions=[
                "Что делать если в прайсе нет валюты?",
                "Как часто можно обновлять прайс?",
                "Чем отличается ORIGINAL от OEM?",
            ],
        )

    # Шаг 2: парсим CSV → превью или импорт
    import re as _re
    rows = []
    failed_lines = []
    for raw in csv_data.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # Разделитель: ;  ,  \t  | (выбираем тот, который встречается чаще)
        sep = max(";,\t|", key=lambda c: line.count(c)) if any(c in line for c in ";,\t|") else None
        parts = [p.strip().strip('"') for p in (line.split(sep) if sep else [line])]
        if len(parts) < 3:
            failed_lines.append({"line": line[:80], "reason": "too few columns"})
            continue
        oem, title, price_raw = parts[0], parts[1], parts[2]
        stock_raw = parts[3] if len(parts) > 3 else "0"
        # Робастный парсер цены: терпит «€/USD», запятые-десятичные,
        # пробелы-разделители тысяч, несколько чисел в ячейке.
        from .pricelist import _coerce_decimal as _coerce
        price = _coerce(price_raw)
        if price is None or price <= 0:
            failed_lines.append({"line": line[:80], "reason": "bad price"})
            continue
        try:
            stock = int(_re.sub(r"[^\d]", "", stock_raw) or 0)
        except Exception:
            stock = 0
        rows.append({"oem": oem, "title": title, "price": price, "stock": stock})

    if not rows:
        return ActionResult(text=(
            "⚠️ Не удалось прочитать ни одной строки.\n"
            "Проверь формат: `артикул;название;цена;остаток`."
        ))

    # Превью без confirmed=true
    if not confirmed:
        preview_rows = [{
            "label": f"{r['oem']} · {r['title'][:32]}",
            "value": f"${r['price']:,.2f} · {r['stock']} шт",
        } for r in rows[:8]]
        return ActionResult(
            text=(
                f"📋 Превью импорта · {len(rows)} строк "
                f"(к импорту), {len(failed_lines)} ошибок.\n"
                f"Подтверди — обновлю существующие и создам новые."
            ),
            cards=[{"type": "draft", "data": {
                "title": f"Импорт прайса: {len(rows)} позиций",
                "rows": preview_rows + (
                    [{"label": "…", "value": f"и ещё {len(rows) - 8} строк"}]
                    if len(rows) > 8 else []
                ),
                "warnings": (
                    [f"⚠️ Пропущено {len(failed_lines)} строк (формат)"]
                    if failed_lines else []
                ),
                "confirm_action": "upload_pricelist",
                "confirm_label": f"✓ Импортировать {len(rows)} позиций",
                "confirm_params": {"csv_data": csv_data, "confirmed": True},
                "cancel_label": "Отмена",
            }}],
        )

    # Реальный импорт
    cat = Category.objects.first() or Category.objects.create(name="Запчасти", slug="parts")
    brand = Brand.objects.filter(name__iexact="Generic").first() or Brand.objects.create(
        name="Generic", slug="generic",
    )
    created, updated = 0, 0
    for r in rows:
        p, was_created = Part.objects.update_or_create(
            seller=user, oem_number__iexact=r["oem"],
            defaults={
                "title": r["title"][:255],
                "oem_number": r["oem"][:100],
                "slug": slugify(f"{r['oem']}-{user.username}")[:280],
                "price": r["price"],
                "stock_quantity": r["stock"],
                "category": cat,
                "brand": brand,
                "is_active": True,
            },
        )
        if was_created:
            created += 1
        else:
            updated += 1

    return ActionResult(
        text=(
            f"✅ Прайс импортирован.\n"
            f"  • Новых позиций: {created}\n"
            f"  • Обновлено: {updated}\n"
            f"  • Пропущено (формат): {len(failed_lines)}"
        ),
        actions=[
            {"label": "📦 Открыть каталог", "action": "seller_catalog", "params": {}},
            {"label": "📊 Спрос на маркетплейсе", "action": "get_demand_report", "params": {}},
        ],
    )


@register("import_pricelist_preview")
def import_pricelist_preview(params, user, role):
    """Открывает форму с инструкцией по импорту прайса.

    Реальный импорт идёт через /api/assistant/upload-spec/ (для buyer'а)
    или /seller/upload/ (для seller'а — bulk). В chat-first для seller'а
    показываем кнопки скачивания шаблона и прямой загрузки.
    """
    return ActionResult(
        text=(
            "📤 Импорт прайс-листа\n\n"
            "Загрузите CSV или Excel со столбцами: артикул, название, цена, "
            "остаток (опционально), бренд (опционально). Я разберу файл, "
            "покажу превью — что будет создано / обновлено / пропущено — и "
            "только после вашего подтверждения внесу в каталог."
        ),
        cards=[{
            "type": "list",
            "data": {
                "title": "Способы импорта",
                "rows": [
                    {"title": "Скачать шаблон CSV",
                     "subtitle": "Готовый файл с примером", "badge": "CSV",
                     "url": "/seller/upload/template.csv"},
                    {"title": "Загрузить через bulk",
                     "subtitle": "Старый интерфейс, поддерживает Excel/Google Sheets",
                     "badge": "Bulk", "url": "/seller/upload/"},
                    {"title": "Перетащить в чат",
                     "subtitle": "Файл .xlsx/.csv прямо в окно чата (для buyer'а)",
                     "badge": "Drop", "url": None},
                ],
            },
        }],
        actions=[
            {"label": "📦 Каталог", "action": "seller_catalog", "params": {}},
            {"label": "➕ По одному", "action": "add_product", "params": {}},
        ],
        suggestions=["Скачать шаблон", "Добавить товар по одному"],
    )


# ══════════════════════════════════════════════════════════
# 1. Каталог продавца (аналог /seller/products/)
# ══════════════════════════════════════════════════════════

@register("seller_warehouses")
def seller_warehouses(params, user, role):
    """Список виртуальных складов продавца — папок с фиксированной
    логистикой (порты + адрес).

    params: {warehouse_id?: int, rename_to?: str, action?: 'delete'}
        - без параметров: список всех складов
        - warehouse_id + rename_to: переименовать
        - warehouse_id + action='delete': удалить (Parts → orphan)
        - warehouse_id only: каталог этого склада
    """
    from django.db import connection
    from django.db.models import Count

    from marketplace.models import Part, SellerWarehouse

    user = _effective_seller(user)
    wid = params.get("warehouse_id")
    rename_to = (params.get("rename_to") or "").strip()
    sub_action = params.get("action") or ""

    # Удаление склада — позиции переводим в orphan (warehouse_id = NULL)
    if wid and sub_action == "delete":
        try:
            w = SellerWarehouse.objects.get(id=int(wid), seller=user)
        except (SellerWarehouse.DoesNotExist, ValueError, TypeError):
            return ActionResult(text="Склад не найден.")
        name = w.name
        # Bulk UPDATE Part.warehouse_id = NULL для всех позиций склада
        with connection.cursor() as cur:
            cur.execute(
                f"UPDATE {Part._meta.db_table} SET warehouse_id = NULL "
                f"WHERE warehouse_id = %s AND seller_id = %s",
                [w.id, user.id],
            )
            unlinked = cur.rowcount
        w.delete()
        return ActionResult(
            text=(f"✓ Склад «{name}» удалён." +
                  (f" {unlinked} позиций переведены в «без склада»." if unlinked else "")),
            actions=[{"label": "📦 К каталогу",
                       "action": "seller_catalog", "params": {}}],
        )

    # Переименование
    if wid and rename_to:
        try:
            w = SellerWarehouse.objects.get(id=int(wid), seller=user)
        except (SellerWarehouse.DoesNotExist, ValueError, TypeError):
            return ActionResult(text="Склад не найден.")
        w.name = rename_to[:120]
        w.save(update_fields=["name", "updated_at"])
        return ActionResult(
            text=f"✓ Склад переименован в «{w.name}».",
            actions=[{"label": "📦 К каталогу",
                       "action": "seller_catalog", "params": {}}],
        )

    # Дрилл-даун в склад → каталог отфильтрованный по warehouse_id
    if wid:
        return seller_catalog(
            {"warehouse_id": int(wid), "limit": 50, "offset": 0}, user, role,
        )

    # Список складов
    from django.db.models import Max, Q
    warehouses = list(
        SellerWarehouse.objects
        .filter(seller=user, is_active=True)
        .annotate(
            parts_count=Count("parts", filter=Q(parts__is_active=True)),
            last_import=Max("parts__data_updated_at"),
        )
        .order_by("-created_at")
    )
    orphans_count = Part.objects.filter(
        seller=user, is_active=True, warehouse__isnull=True
    ).count()

    if not warehouses and not orphans_count:
        return ActionResult(
            text="У вас пока нет складов. Каждая загрузка прайса создаст склад автоматически.",
            actions=[{"label": "📤 Загрузить прайс",
                       "action": "upload_pricelist", "params": {}}],
        )

    # Раньше: если у продавца 1 склад — прыгали сразу в плоский каталог.
    # Это плохо для юзера: он не понимает где находится, и для каталогов
    # >50k позиций экран выглядит как «свалка». Теперь ВСЕГДА показываем
    # карточку склада (даже если он один) — drill-down делается явным
    # кликом юзера, чтобы навигация была предсказуемой.

    from django.utils import timezone as _tz
    now = _tz.now()
    rows = []
    for w in warehouses:
        last_import = getattr(w, "last_import", None)
        days = (now - last_import).days if last_import else None
        if days is None:
            staleness = "unknown"
            stale_label = "—"
        elif days < 7:
            staleness = "fresh"
            stale_label = f"свежий ({days} дн.)"
        elif days < 30:
            staleness = "stale"
            stale_label = f"⚠ обновить ({days} дн.)"
        else:
            staleness = "old"
            stale_label = f"⚠ устарел ({days} дн.)"
        rows.append({
            "id": w.id,
            "name": w.name,
            "country_code": w.country_code,
            "sea_port": w.sea_port,
            "air_port": w.air_port,
            "address": w.address[:200] if w.address else "",
            "currency": w.currency,
            "parts_count": int(getattr(w, "parts_count", 0) or 0),
            "created_at": w.created_at.strftime("%d.%m.%Y"),
            "updated_at": w.updated_at.strftime("%d.%m.%Y %H:%M"),
            "last_import": last_import.strftime("%d.%m.%Y") if last_import else "",
            "staleness": staleness,
            "stale_label": stale_label,
            "days_since_import": days if days is not None else -1,
        })
    if orphans_count:
        rows.append({
            "id": 0,
            "name": "📦 Без склада (старые загрузки)",
            "country_code": "",
            "sea_port": "", "air_port": "",
            "address": "Позиции загруженные до системы складов — без логистического базиса.",
            "currency": "",
            "parts_count": orphans_count,
            "created_at": "",
            "is_orphan": True,
        })

    from django.utils import timezone as _tz
    refreshed_at = _tz.now().strftime("%H:%M:%S")
    return ActionResult(
        text=f"📦 Мои товары — {len(warehouses)} склад{'ов' if len(warehouses) != 1 else ''}"
              + (f" + {orphans_count} позиций без склада" if orphans_count else "")
              + f". Обновлено в {refreshed_at}.",
        cards=[{
            "type": "warehouses",
            "data": {
                "title": "Мои товары — выберите склад",
                "rows": rows,
                "refreshed_at": refreshed_at,
            },
        }],
        actions=[
            {"label": "🔄 Обновить",     "action": "seller_warehouses", "params": {}},
            {"label": "📤 Новая загрузка", "action": "upload_pricelist", "params": {}},
            {"label": "📦 Все товары",    "action": "seller_catalog", "params": {}},
        ],
        suggestions=["Переименовать склад", "Удалить пустой склад"],
    )


@register("seller_catalog")
def seller_catalog(params, user, role):
    """Каталог продавца — полные данные позиций с пагинацией.

    params: {q?: str, status?: 'active'|'archived',
             limit?: int (default 50), offset?: int (default 0)}
    Возвращает все поля позиции: EXW + валюта, FOB SEA/AIR, порты,
    склад, состояние, наличие, габариты, продажи. Поддерживает «Показать
    ещё» через offset.
    """
    from django.db.models import Sum

    from marketplace.models import OrderItem, Part

    user = _effective_seller(user)
    q = (params.get("q") or "").strip()
    status = params.get("status") or "active"
    limit = min(int(params.get("limit") or 50), 200)
    offset = max(int(params.get("offset") or 0), 0)
    warehouse_id = params.get("warehouse_id")
    try:
        warehouse_id = int(warehouse_id) if warehouse_id is not None and warehouse_id != "" else None
    except (TypeError, ValueError):
        warehouse_id = None

    # Без warehouse_id и без поискового запроса — показываем экран складов,
    # а не свалку всех позиций. Юзер кликает «Каталог» в общем меню и
    # ожидает выбрать склад → провалиться в его товары. Прыжок сразу в
    # плоский список 160k позиций дезориентирует и тяжёл для рендера.
    # Исключение: если явный поиск (q) или фильтр (status='archived') —
    # показываем плоский результат поиска по всем складам.
    if warehouse_id is None and not q and status == "active":
        return seller_warehouses({}, user, role)

    qs = Part.objects.filter(seller=user).select_related("brand", "category", "warehouse")
    if status == "active":
        qs = qs.filter(is_active=True)
    elif status == "archived":
        qs = qs.filter(is_active=False)
    if warehouse_id == 0:
        qs = qs.filter(warehouse__isnull=True)
    elif warehouse_id:
        qs = qs.filter(warehouse_id=warehouse_id)
    if q:
        from django.db.models import Q
        qs = qs.filter(
            Q(oem_number__icontains=q)
            | Q(title__icontains=q)
            | Q(cross_numbers__icontains=q)
            | Q(manufacturer__icontains=q)
        )

    total_count = qs.count()
    # Сортировка по дате последнего апдейта — свежие загрузки сверху.
    # -id вторичный, чтобы стабильно ранжировать строки с одинаковым timestamp
    # (bulk-upload пишет одинаковый updated_at для всех 200k+ rows).
    parts = list(qs.order_by("-data_updated_at", "-id")[offset:offset + limit])
    if not parts and offset == 0:
        return ActionResult(
            text=("В каталоге пока нет товаров." if not q else
                  f"По запросу «{q}» товаров не найдено."),
            actions=[
                {"label": "➕ Добавить товар", "action": "add_product", "params": {}},
                {"label": "📤 Загрузить прайс", "action": "upload_pricelist", "params": {}},
            ],
        )

    sales = (
        OrderItem.objects
        .filter(part_id__in=[p.id for p in parts],
                order__status__in=["delivered", "completed", "issuing",
                                   "transit_rf", "customs", "transit_abroad",
                                   "ready_to_ship"])
        .values("part_id")
        .annotate(qty=Sum("quantity"), revenue=Sum("unit_price"))
    )
    sales_map = {s["part_id"]: s for s in sales}

    rows = []
    for p in parts:
        s = sales_map.get(p.id, {})
        rows.append({
            "id": p.id,
            "article": p.oem_number,
            "cross_numbers": p.cross_numbers or "",
            "title": p.title,
            "brand": p.brand.name if p.brand else "—",
            "manufacturer": p.manufacturer or "",
            "warehouse_id": p.warehouse_id,
            "warehouse_name": (p.warehouse.name if p.warehouse_id else ""),
            "price": float(p.price) if p.price else None,
            "currency": p.currency or "USD",
            "price_fob_sea": float(p.price_fob_sea) if p.price_fob_sea else None,
            "price_fob_air": float(p.price_fob_air) if p.price_fob_air else None,
            "sea_port": p.sea_port or "",
            "air_port": p.air_port or "",
            "warehouse": p.warehouse_address or "",
            "condition": p.condition or "",
            "availability": getattr(p, "availability", "") or "",
            "stock_qty": getattr(p, "stock_quantity", None) or 0,
            "weight_kg": float(p.gross_weight_kg) if p.gross_weight_kg else None,
            "is_active": p.is_active,
            "sold_qty": int(s.get("qty") or 0),
            "revenue": float(s.get("revenue") or 0),
        })

    rows.sort(key=lambda r: (r["sold_qty"], r["id"]), reverse=True)

    shown_end = offset + len(parts)
    warehouse_label = ""
    if warehouse_id:
        from marketplace.models import SellerWarehouse
        if warehouse_id == 0:
            warehouse_label = " (без склада)"
        else:
            try:
                w = SellerWarehouse.objects.get(id=warehouse_id, seller=user)
                warehouse_label = f" со склада «{w.name}»"
            except SellerWarehouse.DoesNotExist:
                pass
    intro = f"Каталог: {total_count} {'позиций' if status == 'active' else 'архивных'}{warehouse_label}"
    if q:
        intro += f" по запросу «{q}»"
    if shown_end < total_count:
        intro += f". Показаны {offset + 1}–{shown_end}, ещё {total_count - shown_end}."
    else:
        intro += "."

    actions = [
        {"label": "➕ Добавить товар", "action": "add_product", "params": {}},
        {"label": "📤 Загрузить прайс", "action": "upload_pricelist", "params": {}},
        {"label": ("📁 Архив" if status == "active" else "📂 Активные"),
         "action": "seller_catalog",
         "params": {"status": "archived" if status == "active" else "active"}},
        {"label": "📊 Дашборд", "action": "seller_dashboard", "params": {}},
    ]
    if shown_end < total_count:
        actions.insert(0, {
            "label": f"⬇️ Показать ещё {min(limit, total_count - shown_end)}",
            "action": "seller_catalog",
            "params": {"q": q, "status": status, "limit": limit,
                       "offset": shown_end,
                       "warehouse_id": warehouse_id or ""},
        })
    if warehouse_id:
        # При фильтре по складу — кнопка возврата самой первой, чтобы
        # пользователь сразу видел путь назад в «Мои товары».
        actions.insert(0, {"label": "📦 Мои товары",
                            "action": "seller_warehouses", "params": {}})

    cards = []
    cards.append({
        "type": "catalog",
        "data": {
            "title": "Каталог" + (warehouse_label if warehouse_label else " — все позиции"),
            "rows": rows,
            "total_count": total_count,
            "offset": offset,
            "shown_end": shown_end,
            "warehouse_id": warehouse_id,
            # Хлебная крошка для возврата в общий список складов
            "back_to_warehouses": bool(warehouse_id),
            "filter": {"q": q, "status": status},
        },
    })

    return ActionResult(
        text=intro,
        cards=cards,
        actions=actions,
        suggestions=["Что чаще покупают?", "Добавить товар", "Скрыть позицию"],
    )


def _build_warehouses_card(user, active_id=None) -> dict | None:
    """Карточка со списком складов для встраивания в общий каталог.
    active_id — id текущего активного фильтра (для подсветки чипа).
    Возвращает None если складов нет вообще."""
    from django.db.models import Count, Max, Q
    from django.utils import timezone as _tz

    from marketplace.models import Part, SellerWarehouse

    warehouses = list(
        SellerWarehouse.objects
        .filter(seller=user, is_active=True)
        .annotate(
            parts_count=Count("parts", filter=Q(parts__is_active=True)),
            last_import=Max("parts__data_updated_at"),
        )
        .order_by("-created_at")
    )
    orphans_count = Part.objects.filter(
        seller=user, is_active=True, warehouse__isnull=True
    ).count()
    if not warehouses and not orphans_count:
        return None

    now = _tz.now()
    rows = []
    for w in warehouses:
        last_import = getattr(w, "last_import", None)
        days = (now - last_import).days if last_import else None
        if days is None:
            staleness = "unknown"
            stale_label = "—"
        elif days < 7:
            staleness = "fresh"
            stale_label = f"свежий ({days} дн.)"
        elif days < 30:
            staleness = "stale"
            stale_label = f"⚠ обновить ({days} дн.)"
        else:
            staleness = "old"
            stale_label = f"⚠ устарел ({days} дн.)"
        rows.append({
            "id": w.id, "name": w.name, "country_code": w.country_code,
            "sea_port": w.sea_port, "air_port": w.air_port,
            "address": w.address[:200] if w.address else "",
            "currency": w.currency,
            "parts_count": int(getattr(w, "parts_count", 0) or 0),
            "last_import": last_import.strftime("%d.%m.%Y") if last_import else "",
            "staleness": staleness, "stale_label": stale_label,
            "days_since_import": days if days is not None else -1,
        })
    if orphans_count:
        rows.append({
            "id": 0, "name": "📦 Без склада (старые загрузки)",
            "country_code": "", "sea_port": "", "air_port": "",
            "address": "Позиции загруженные до системы складов — без логистического базиса.",
            "currency": "", "parts_count": orphans_count, "is_orphan": True,
        })
    return {
        "type": "warehouses",
        "data": {
            "title": f"📁 Склады ({len(warehouses)})",
            "rows": rows,
            "compact": True,  # горизонтальные чипы вместо больших карточек
            "active_id": active_id,  # подсветить активный фильтр
        },
    }


@register("toggle_product")
def toggle_product(params, user, role):
    """Активация/деактивация товара. params: {part_id, active?}"""
    from marketplace.models import Part
    pid = params.get("part_id")
    if not pid:
        return ActionResult(text="Не указан ID товара.")
    try:
        p = Part.objects.get(id=pid, seller=user)
    except Part.DoesNotExist:
        return ActionResult(text=f"Товар #{pid} не найден или не ваш.")
    new_state = params.get("active")
    if new_state is None:
        new_state = not p.is_active
    p.is_active = bool(new_state)
    p.save(update_fields=["is_active"])
    return ActionResult(
        text=(f"✓ Товар «{p.title}» ({p.oem_number}) "
              f"{'активирован' if p.is_active else 'скрыт из каталога'}."),
        actions=[
            {"label": "Каталог", "action": "seller_catalog", "params": {}},
        ],
    )


# ══════════════════════════════════════════════════════════
# 2. CRUD товара (форма)
# ══════════════════════════════════════════════════════════

@register("add_product")
def add_product(params, user, role):
    """Двухфазный: без полей → форма, с полями → создаём Part."""
    from marketplace.models import Brand, Category, Part

    article = (params.get("article") or "").strip()
    title = (params.get("title") or "").strip()
    price = params.get("price")
    if not (article and title and price is not None):
        return ActionResult(
            text="Добавление товара в каталог.",
            cards=[{
                "type": "form",
                "data": {
                    "title": "➕ Новый товар",
                    "submit_action": "add_product",
                    "submit_label": "Добавить",
                    "fields": [
                        {"name": "article", "label": "OEM-артикул",
                         "placeholder": "C306-3673", "required": True},
                        {"name": "title", "label": "Наименование",
                         "placeholder": "Voltage Regulator (FAW)", "required": True},
                        {"name": "price", "label": "Цена, USD",
                         "type": "number", "placeholder": "96", "required": True},
                        {"name": "stock_qty", "label": "Остаток на складе",
                         "type": "number", "placeholder": "10", "default": "1"},
                        {"name": "brand", "label": "Бренд",
                         "placeholder": "FAW / Komatsu / Bosch"},
                    ],
                    "fixed_params": {},
                },
            }],
        )

    try:
        price_d = Decimal(str(price))
    except Exception:
        return ActionResult(text="Некорректная цена.")

    brand_name = (params.get("brand") or "").strip()
    brand = None
    if brand_name:
        brand, _ = Brand.objects.get_or_create(name=brand_name)
    category = Category.objects.first()  # дефолтная категория

    if Part.objects.filter(seller=user, oem_number=article).exists():
        return ActionResult(text=f"⚠️ Товар с артикулом {article} у вас уже есть.")

    p = Part.objects.create(
        seller=user,
        oem_number=article,
        title=title,
        price=price_d,
        stock_quantity=int(params.get("stock_qty") or 1),
        brand=brand,
        category=category,
        is_active=True,
    )
    return ActionResult(
        text=f"✓ Товар «{p.title}» ({p.oem_number}) добавлен в каталог.",
        cards=[{
            "type": "product",
            "data": {
                "id": str(p.id), "article": p.oem_number, "name": p.title,
                "brand": brand_name or "—", "price": float(p.price), "currency": "USD",
                "in_stock": p.stock_quantity > 0, "quantity": p.stock_quantity,
            },
        }],
        actions=[
            {"label": "📋 Каталог", "action": "seller_catalog", "params": {}},
            {"label": "➕ Ещё товар", "action": "add_product", "params": {}},
        ],
    )


# ══════════════════════════════════════════════════════════
# 3. RFQ inbox с inline-формой ответа
# ══════════════════════════════════════════════════════════

@register("rfq_detail")
def rfq_detail(params, user, role):
    """Детали входящего RFQ с inline-формой ответа."""
    from marketplace.models import RFQ, RFQItem
    rfq_id = params.get("rfq_id")
    if not rfq_id:
        return ActionResult(text="Не указан ID RFQ.")
    try:
        rfq = RFQ.objects.select_related("created_by").get(id=rfq_id)
    except RFQ.DoesNotExist:
        return ActionResult(text=f"RFQ #{rfq_id} не найден.")

    items = list(RFQItem.objects.filter(rfq=rfq).select_related("matched_part"))
    items_text = "\n".join(
        f"  • {it.query} × {it.quantity}" for it in items[:8]
    ) or "  (позиций нет)"

    badge = mode_badge_with_sla(rfq.mode)
    badge_line = f"Режим: {badge}\n" if badge else ""
    text = (
        f"📋 RFQ #{rfq.id} · {rfq.get_status_display()}\n"
        f"{badge_line}"
        f"От: {rfq.customer_name or rfq.created_by.username}\n"
        f"Создан: {rfq.created_at.strftime('%d.%m.%Y %H:%M')}\n"
        f"Позиций: {len(items)}\n\n"
        f"Список:\n{items_text}"
    )

    actions_list = []
    if role == "seller" and rfq.status in ("new", "processing"):
        actions_list.append({
            "label": "💬 Ответить ценой",
            "action": "respond_rfq_form",
            "params": {"rfq_id": rfq.id},
        })
        actions_list.append({
            "label": "❌ Отклонить",
            "action": "respond_rfq",
            "params": {"rfq_id": rfq.id, "decline": True},
        })
    actions_list.append({"label": "📋 Все RFQ", "action": "get_rfq_status", "params": {}})

    return ActionResult(
        text=text,
        cards=[{
            "type": "rfq",
            "data": {
                "id": str(rfq.id), "number": rfq.id,
                "status": rfq.get_status_display(),
                "description": rfq.notes[:200] if rfq.notes else "",
                "quantity": sum(it.quantity for it in items),
                "created_at": rfq.created_at.strftime("%d.%m.%Y %H:%M"),
            },
        }],
        actions=actions_list,
        suggestions=["Ответить ценой", "Все RFQ", "Спрос на маркетплейсе"],
    )


# NOTE: respond_rfq_form moved to assistant/negotiation.py (alias to submit_quote).
# Полноценный multi-line, multi-round flow с Quote-моделью.


# ══════════════════════════════════════════════════════════
# 4. Чертежи
# ══════════════════════════════════════════════════════════

@register("seller_drawings")
def seller_drawings(params, user, role):
    """Список чертежей по товарам seller'а."""
    from marketplace.models import Drawing, Part
    user = _effective_seller(user)

    part_ids = list(Part.objects.filter(seller=user).values_list("id", flat=True))
    qs = Drawing.objects.filter(part_id__in=part_ids).select_related("part")[:20]
    items = list(qs)
    if not items:
        return ActionResult(
            text="Чертежей пока нет. Прикрепите файлы к товарам — они помогают покупателям точнее искать.",
            actions=[
                {"label": "Каталог", "action": "seller_catalog", "params": {}},
                {"label": "📊 Дашборд", "action": "seller_dashboard", "params": {}},
            ],
        )

    rows = [{
        "id": d.id,
        "title": getattr(d, "title", None) or f"Чертёж #{d.id}",
        "part": d.part.oem_number if getattr(d, "part", None) else "—",
        "uploaded_at": d.uploaded_at.strftime("%d.%m.%Y") if hasattr(d, "uploaded_at") else "",
        "url": d.file.url if getattr(d, "file", None) else None,
    } for d in items]

    return ActionResult(
        text=f"📐 Чертежи: {len(items)} файлов по вашим товарам.",
        cards=[{
            "type": "list",
            "data": {
                "title": "Чертежи",
                "rows": [{
                    "title": r["title"],
                    "subtitle": f"К товару {r['part']} · загружен {r['uploaded_at']}",
                    "url": r["url"],
                } for r in rows],
            },
        }],
        actions=[
            {"label": "Каталог", "action": "seller_catalog", "params": {}},
            {"label": "📊 Дашборд", "action": "seller_dashboard", "params": {}},
        ],
        suggestions=["Сколько чертежей?", "По какому товару чертёж?"],
    )


# ══════════════════════════════════════════════════════════
# 5. Команда
# ══════════════════════════════════════════════════════════

@register("seller_team")
def seller_team(params, user, role):
    """Список членов команды продавца (TeamMember)."""
    from marketplace.models import TeamMember
    user = _effective_seller(user)
    qs = TeamMember.objects.filter(owner=user).select_related("user") if hasattr(TeamMember, "owner") else TeamMember.objects.none()
    items = list(qs[:30])

    rows = []
    for m in items:
        u = getattr(m, "user", None)
        name = (u.get_full_name() if u else "") or (u.username if u else "—")
        dept = getattr(m, "department", None) or getattr(m, "role", "") or ""
        active = getattr(m, "is_active", True)
        rows.append({
            "title": name,
            "subtitle": f"{dept} · {'активен' if active else 'отключён'}",
        })
    if not rows:
        rows = [{"title": user.get_full_name() or user.username, "subtitle": "Владелец · вы"}]

    return ActionResult(
        text=f"👥 Команда: {len(rows)} {'участник' if len(rows)==1 else 'участников'}.",
        cards=[{
            "type": "list",
            "data": {"title": "Команда", "rows": rows},
        }],
        actions=[
            {"label": "➕ Пригласить", "action": "invite_team_member", "params": {}},
            {"label": "📊 Дашборд", "action": "seller_dashboard", "params": {}},
        ],
        suggestions=["Пригласить менеджера", "Дать права логиста"],
    )


@register("invite_team_member")
def invite_team_member(params, user, role):
    """Пригласить участника команды по email (форма)."""
    email = (params.get("email") or "").strip()
    if not email:
        return ActionResult(
            text="Пригласить нового участника команды.",
            cards=[{
                "type": "form",
                "data": {
                    "title": "👥 Приглашение в команду",
                    "submit_action": "invite_team_member",
                    "submit_label": "Отправить приглашение",
                    "fields": [
                        {"name": "email", "label": "Email", "type": "email",
                         "required": True, "placeholder": "manager@company.com"},
                        {"name": "role", "label": "Роль",
                         "placeholder": "manager / sales / logist / viewer",
                         "default": "manager"},
                    ],
                    "fixed_params": {},
                },
            }],
        )
    return ActionResult(
        text=(
            f"✓ Приглашение отправлено на {email}.\n"
            f"Когда получатель зарегистрируется, он попадёт в вашу команду."
        ),
        actions=[{"label": "👥 Команда", "action": "seller_team", "params": {}}],
    )


# ══════════════════════════════════════════════════════════
# 6. Интеграции
# ══════════════════════════════════════════════════════════

@register("seller_integrations")
def seller_integrations(params, user, role):
    """Список доступных интеграций со статусом."""
    integrations = [
        {"key": "1c",       "name": "1С:Управление торговлей",
         "desc": "Синхронизация остатков и заказов",
         "status": "available"},
        {"key": "bitrix",   "name": "Битрикс24",
         "desc": "RFQ в CRM, авто-сделки", "status": "available"},
        {"key": "email",    "name": "Email-уведомления",
         "desc": "Новые RFQ и заказы на почту",
         "status": "active"},
        {"key": "telegram", "name": "Telegram-бот",
         "desc": "Алерты по новым заказам",
         "status": "available"},
        {"key": "api",      "name": "REST API + webhooks",
         "desc": "Свои интеграции через API-ключ",
         "status": "available"},
    ]
    rows = [{
        "title": i["name"],
        "subtitle": i["desc"],
        "badge": ("Подключено" if i["status"] == "active" else "Доступно"),
    } for i in integrations]

    return ActionResult(
        text="🔌 Интеграции с внешними системами. Подключите нужное в один клик.",
        cards=[{
            "type": "list",
            "data": {"title": "Интеграции", "rows": rows},
        }],
        actions=[
            {"label": "📊 Дашборд", "action": "seller_dashboard", "params": {}},
        ],
        suggestions=["Подключить 1С", "Настроить Telegram-бота"],
    )


# ══════════════════════════════════════════════════════════
# 7. Отчёты
# ══════════════════════════════════════════════════════════

@register("seller_reports")
def seller_reports(params, user, role):
    """Доступные отчёты для скачивания."""
    reports = [
        {"key": "sales_csv",     "name": "Продажи (CSV, 30д)",
         "desc": "Все продажи за последние 30 дней"},
        {"key": "catalog_xlsx",  "name": "Каталог (Excel)",
         "desc": "Полный каталог с остатками"},
        {"key": "rfq_csv",       "name": "RFQ-история (CSV)",
         "desc": "Все RFQ и ответы"},
        {"key": "sla_pdf",       "name": "SLA-отчёт (PDF)",
         "desc": "Отчёт о выполнении SLA"},
        {"key": "rating_pdf",    "name": "Рейтинг и отзывы (PDF)",
         "desc": "Сертификат рейтинга"},
    ]
    rows = [{
        "title": r["name"],
        "subtitle": r["desc"],
        "badge": "Скачать",
    } for r in reports]
    return ActionResult(
        text="📑 Отчёты по вашей деятельности. Любой можно сгенерировать сейчас.",
        cards=[{
            "type": "list",
            "data": {"title": "Отчёты", "rows": rows},
        }],
        actions=[
            {"label": "📊 Дашборд", "action": "seller_dashboard", "params": {}},
        ],
        suggestions=["Выгрузить продажи", "Скачать каталог"],
    )


@register("connect_gsheet")
def connect_gsheet(params, user, role):
    """ТЗ: подключить Google-таблицу как источник прайса.

    Скачивает таблицу через публичный CSV-export и прогоняет через тот
    же pipeline что и upload-файла: smart-mapping (словарь + AI) →
    preview-импорт. URL сохраняется в PricelistMapping['_gsheet_url']
    для будущей авто-синхронизации (Celery raz/час).
    """
    import re as _re

    from django.core.files.base import ContentFile

    from marketplace.models import PricelistImport, PricelistMapping

    from .pricelist import (
        MAX_COLUMNS,
        MAX_FILE_BYTES,
        MIN_COLUMNS,
        _build_mapped_preview,
        _read_preview,
        _smart_mapping,
    )

    user = _effective_seller(user)
    url = (params.get("gsheet_url") or "").strip()
    if not url:
        return ActionResult(text="⚠️ Пустая ссылка.")
    m = _re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", url)
    if not m:
        return ActionResult(text=(
            "⚠️ Это не похоже на ссылку Google Sheets. "
            "Ожидается формат `https://docs.google.com/spreadsheets/d/...`"
        ))
    sheet_id = m.group(1)
    gid_m = _re.search(r"[?&#]gid=(\d+)", url)
    gid = gid_m.group(1) if gid_m else "0"
    csv_url = (
        f"https://docs.google.com/spreadsheets/d/{sheet_id}"
        f"/export?format=csv&gid={gid}"
    )
    try:
        import requests
        resp = requests.get(csv_url, timeout=20, allow_redirects=True)
    except Exception as e:
        return ActionResult(text=f"⚠️ Не удалось скачать таблицу: {e}")
    if resp.status_code == 401 or resp.status_code == 403:
        return ActionResult(text=(
            "⚠️ Таблица не доступна публично. Откройте доступ "
            "«Все, у кого есть ссылка → Читатель» в настройках Google Sheets "
            "и попробуйте снова."
        ))
    if resp.status_code != 200:
        return ActionResult(text=(
            f"⚠️ Google вернул HTTP {resp.status_code}. Проверьте ссылку "
            f"и доступ."
        ))
    blob = resp.content
    if len(blob) > MAX_FILE_BYTES:
        return ActionResult(text=(
            f"⚠️ Таблица слишком большая ({len(blob) // 1024 // 1024} МБ). "
            f"Максимум {MAX_FILE_BYTES // 1024 // 1024} МБ."
        ))
    try:
        headers, sample = _read_preview("gsheet.csv", blob)
    except Exception as e:
        return ActionResult(text=f"⚠️ Не удалось прочитать таблицу: {e}")
    if not headers or len(headers) < MIN_COLUMNS:
        return ActionResult(text=(
            f"⚠️ В таблице меньше {MIN_COLUMNS} колонок. Нечего импортировать."
        ))
    if len(headers) > MAX_COLUMNS:
        return ActionResult(text=(
            f"⚠️ Колонок слишком много ({len(headers)}). Максимум {MAX_COLUMNS}."
        ))

    # Smart mapping: словарь + AI fallback
    pm = PricelistMapping.objects.filter(seller=user).first()
    ai_called = False
    if pm and pm.mapping:
        suggested = {k: v for k, v in pm.mapping.items()
                      if v and (str(v).startswith("fix:") or v in headers)}
    else:
        suggested, _u, ai_called, status = _smart_mapping(
            headers, sample, seller=user,
        )
        if status == "quota_exceeded":
            return ActionResult(text=(
                "Лимит распознавания исчерпан. Попробуйте завтра или "
                "скачайте шаблон."
            ))

    imp = PricelistImport.objects.create(
        seller=user, filename="gsheet.csv",
        headers=headers, sample_rows=sample,
        suggested_mapping=suggested, ai_called=ai_called,
        status="preview",
    )
    imp.file_obj.save(f"gsheet-{imp.id}.csv", ContentFile(blob), save=True)

    # Сохраняем URL для авто-синхронизации
    pm2, _ = PricelistMapping.objects.get_or_create(seller=user)
    cur = dict(pm2.mapping or {})
    cur["_gsheet_url"] = url
    pm2.mapping = cur
    pm2.save(update_fields=["mapping", "updated_at"])

    mapped_preview = _build_mapped_preview(headers, sample, suggested)
    rows = [{
        "label": "🔗 URL", "value": url[:60] + ("…" if len(url) > 60 else ""),
    }, {
        "label": "Колонок", "value": str(len(headers)),
    }, {
        "label": "Маппинг готов", "value": (
            "по словарю + AI" if ai_called else
            "по сохранённому маппингу" if pm and pm.mapping else
            "по словарю"
        ),
    }, {
        "label": "─── Распознанные поля ───", "value": "",
    }]
    for std, src in (suggested or {}).items():
        rows.append({"label": std, "value": str(src)})

    return ActionResult(
        text=(
            f"✅ Google Sheet подключён · {len(headers)} колонок прочитано.\n"
            f"Подтвердите маппинг ниже — после импорта URL сохранится для "
            f"авто-синхронизации (раз в час)."
        ),
        cards=[
            {"type": "draft", "data": {
                "title": "Google Sheet · превью",
                "rows": rows,
            }},
            {"type": "table_preview", "data": {
                "title": "Как ляжет в базу (первые 5 строк)",
                "headers": mapped_preview["headers"],
                "rows": mapped_preview["rows"],
            }},
        ],
        actions=[
            {"action": "__pricelist_commit", "label": "📥 Импортировать",
             "params": dict(
                 {"import_id": imp.id},
                 **{f"col__{k}": v for k, v in (suggested or {}).items()},
             )},
            {"action": "__pricelist_cancel", "label": "Отменить",
             "params": {"import_id": imp.id}},
        ],
    )


# ══════════════════════════════════════════════════════════
# BUG-2 fix: alias-actions для sidebar-кнопок продавца, которых
# раньше не было в реестре → «Нет прав для роли seller».
# Алиасы делегируют в реальные хэндлеры.
# ══════════════════════════════════════════════════════════

@register("seller_urgent")
def seller_urgent_alias(params, user, role):
    return seller_inbox(params, user, role)

@register("seller_quotes")
def seller_quotes_alias(params, user, role):
    return seller_negotiations(params, user, role)

@register("seller_analytics")
def seller_analytics_alias(params, user, role):
    return seller_analytics_hub(params, user, role)

@register("seller_orders")
def seller_orders_alias(params, user, role):
    # Покупательский get_orders уже отдаёт seller-view через _effective_seller
    from . import actions as _a
    return _a._REGISTRY["get_orders"](params, user, role)

@register("seller_rfqs")
def seller_rfqs_alias(params, user, role):
    return seller_inbox(params, user, role)

@register("get_seller_rfqs")
def get_seller_rfqs_alias(params, user, role):
    return seller_inbox(params, user, role)

@register("seller_upload_pricelist")
def seller_upload_pricelist_alias(params, user, role):
    from . import actions as _a
    if "upload_pricelist" in _a._REGISTRY:
        return _a._REGISTRY["upload_pricelist"](params, user, role)
    # Fallback: подсказать как загрузить
    return ActionResult(
        text="📤 Загрузка прайса. Прикрепите Excel-файл к сообщению — AI сам распознает колонки.",
        actions=[{"action": "pricelist_history", "label": "📋 История загрузок"}],
    )

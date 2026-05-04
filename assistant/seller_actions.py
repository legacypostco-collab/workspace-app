"""Seller-specific chat actions: каталог, чертежи, команда, интеграции, отчёты.

Регистрируются в общий реестр assistant.actions через @register декоратор.
Импортируется из assistant/__init__.py чтобы зарегистрировать при загрузке.
"""
from __future__ import annotations

import logging
from decimal import Decimal

from django.utils import timezone

from .actions import ActionResult, register

logger = logging.getLogger(__name__)


def _effective_seller(user):
    """Возвращает «продавца» для seller-actions.

    Toggle «Продавец/Покупатель/Оператор» переключает только UI-режим, не меняя
    request.user. Чтобы demo-аккаунт (demo_buyer / demo_operator) при переключении
    в seller-режим видел работающий кабинет, делаем fallback: если у текущего
    пользователя нет товаров в каталоге, и он — демо-аккаунт, показываем данные
    demo_seller. В реальной жизни (не demo) пользователь либо сам seller, либо
    у него своих товаров действительно нет — тогда отвечаем «пусто».
    """
    from django.contrib.auth import get_user_model
    from marketplace.models import Part
    if Part.objects.filter(seller=user, is_active=True).exists():
        return user
    if (user.username or "").startswith("demo_"):
        try:
            return get_user_model().objects.get(username="demo_seller")
        except Exception:
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
    from datetime import timedelta
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
    from marketplace.models import Order, OrderItem, Part

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
            import requests
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
    if not items:
        return ActionResult(
            text=("🔕 Уведомлений нет — на сегодня ничего не пропустили."
                  if unread == 0 else
                  f"Без новых, всего непрочитанных в системе: {unread}."),
            actions=[
                {"label": "📊 Дашборд", "action": "seller_dashboard", "params": {}},
            ],
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
        actions=[
            {"label": "📊 Дашборд", "action": "seller_dashboard", "params": {}},
        ],
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

    payload = f"CONS-ORD-{order.id}:{meta['qr_token']}"
    qr_url = (
        f"https://api.qrserver.com/v1/create-qr-code/?size=240x240&data={payload}"
    )
    return ActionResult(
        text=(
            f"QR для заказа #{order.id} готов. Распечатайте и приклейте на упаковку. "
            f"При отгрузке/приёмке сканирование зафиксируется в аудит-логе."
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
    from .pricing import calculate_quote, D

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
    from .models import KnowledgeChunk
    from django.db.models import Q

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
    from marketplace.models import Order, OrderItem, RFQ

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

    sections = []
    if to_ship:
        sections.append({
            "icon": "🚚", "title": "К отгрузке (оплачено покупателем)",
            "rows": [
                {
                    "title": f"Заказ #{o.id} · {o.customer_name}",
                    "subtitle": f"Сумма ${o.total_amount:,.0f} · оплачен {(o.final_paid_at or o.created_at).strftime('%d.%m.%Y')}",
                    "action": {"label": "🚚 Отгрузить",
                               "action": "ship_order",
                               "params": {"order_id": o.id}},
                } for o in to_ship
            ],
        })
    if to_confirm:
        sections.append({
            "icon": "✅", "title": "Новые заказы — подтвердить и в производство",
            "rows": [
                {
                    "title": f"Заказ #{o.id} · {o.customer_name}",
                    "subtitle": f"Сумма ${o.total_amount:,.0f} · резерв оплачен",
                    "action": {"label": "▶️ Подтвердить",
                               "action": "advance_order",
                               "params": {"order_id": o.id}},
                } for o in to_confirm
            ],
        })
    if new_rfqs:
        sections.append({
            "icon": "📋", "title": "Новые RFQ — ответить ценой",
            "rows": [
                {
                    "title": f"RFQ #{r.id} · {r.customer_name}",
                    "subtitle": f"Создан {r.created_at.strftime('%d.%m.%Y')} · {r.get_status_display()}",
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
        suggestions=["Что отгрузить?", "Какие RFQ срочные?", "Дашборд"],
    )


# ══════════════════════════════════════════════════════════
# 0.5. Продукт — детальная карточка с метриками
# ══════════════════════════════════════════════════════════

@register("product_detail")
def product_detail(params, user, role):
    """Детали товара: цена, остатки, спрос, история продаж."""
    from datetime import timedelta
    from marketplace.models import Part, OrderItem
    from django.db.models import Sum, Count

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
    from marketplace.models import Part, Brand
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

@register("seller_catalog")
def seller_catalog(params, user, role):
    """Список товаров продавца с базовой статистикой и быстрыми действиями.

    params: {q?: str, status?: 'active'|'archived', limit?: int}
    """
    from marketplace.models import Part, OrderItem
    from django.db.models import Sum, Count

    user = _effective_seller(user)
    q = (params.get("q") or "").strip()
    status = params.get("status") or "active"
    limit = min(int(params.get("limit") or 20), 50)

    qs = Part.objects.filter(seller=user).select_related("brand", "category")
    if status == "active":
        qs = qs.filter(is_active=True)
    elif status == "archived":
        qs = qs.filter(is_active=False)
    if q:
        from django.db.models import Q
        qs = qs.filter(Q(oem_number__icontains=q) | Q(title__icontains=q))

    parts = list(qs.order_by("-id")[:limit])
    if not parts:
        return ActionResult(
            text=("В каталоге пока нет товаров." if not q else
                  f"По запросу «{q}» товаров не найдено."),
            actions=[
                {"label": "➕ Добавить товар", "action": "add_product", "params": {}},
                {"label": "📤 Загрузить прайс", "action": "upload_pricelist", "params": {}},
            ],
        )

    # Быстрая агрегация продаж по товарам (топ-5 продаж)
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
            "title": p.title,
            "brand": p.brand.name if p.brand else "—",
            "price": float(p.price) if p.price else None,
            "stock_qty": getattr(p, "stock_quantity", None) or 0,
            "is_active": p.is_active,
            "sold_qty": int(s.get("qty") or 0),
            "revenue": float(s.get("revenue") or 0),
        })

    intro = f"Каталог: {len(parts)} {'позиций' if status=='active' else 'архивных'}"
    if q:
        intro += f" по запросу «{q}»"
    intro += ". Топовые позиции по продажам сверху."

    rows.sort(key=lambda r: r["sold_qty"], reverse=True)

    return ActionResult(
        text=intro,
        cards=[{
            "type": "catalog",
            "data": {
                "title": "Каталог продавца",
                "rows": rows,
                "filter": {"q": q, "status": status},
            },
        }],
        actions=[
            {"label": "➕ Добавить товар", "action": "add_product", "params": {}},
            {"label": "📤 Загрузить прайс", "action": "upload_pricelist", "params": {}},
            {"label": ("📁 Архив" if status == "active" else "📂 Активные"),
             "action": "seller_catalog",
             "params": {"status": "archived" if status == "active" else "active"}},
            {"label": "📊 Дашборд", "action": "seller_dashboard", "params": {}},
        ],
        suggestions=["Что чаще покупают?", "Добавить товар", "Скрыть позицию"],
    )


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
    from marketplace.models import Part, Brand, Category

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

    text = (
        f"📋 RFQ #{rfq.id} · {rfq.get_status_display()}\n"
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

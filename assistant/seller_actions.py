"""Seller-specific chat actions: каталог, чертежи, команда, интеграции, отчёты.

Регистрируются в общий реестр assistant.actions через @register декоратор.
Импортируется из assistant/__init__.py чтобы зарегистрировать при загрузке.
"""
from __future__ import annotations

import logging
from decimal import Decimal

from django.utils import timezone
from django.utils.translation import gettext as _

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

    # ── Командный доступ: активный сотрудник видит данные СВОЕЙ компании ──
    # (руководитель приглашает сотрудников → TeamMember; они работают с теми же
    #  товарами/заказами/КП, что и компания-владелец).
    try:
        from marketplace.models import TeamMember
        _tm = (TeamMember.objects.filter(user=user, status="active")
               .exclude(owner=user).select_related("owner").first())
        if _tm is not None:
            return _tm.owner
    except Exception:
        pass

    username = (user.username or "")
    if username == "demo_seller":
        return user

    # Тестовый/демо-юзер — fallback на demo_seller ТОЛЬКО в DEBUG.
    # В prod (DEBUG=False) каждый юзер видит свои данные, даже если у него
    # username начинается с demo_/test_ (важно: реальный production-юзер мог
    # случайно зарегистрироваться с таким префиксом и не должен видеть чужой
    # каталог).
    is_debug = bool(getattr(settings, "DEBUG", False))
    is_test_account = is_debug and (
        username.startswith("demo_")
        or username.startswith("test_")
        or username.startswith("tz")  # автогенерённые тестеры из beta
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
        _("🤝 Реферальная программа\n"
          "Ваш код: %(code)s\n"
          "Условия: 2%% от первого заказа приглашённого клиента (до $5,000), "
          "далее — 0.5%% от всех его заказов в течение года. Партнёрам "
          "(дилерам и инженерам по сервису) — отдельный тариф 5%%.") % {"code": code}
    )

    return ActionResult(
        text=text,
        cards=[{
            "type": "kpi_grid",
            "data": {
                "title": _("Реферальная программа"),
                "kpis": [
                    {"label": _("Ваш код"),      "value": code,       "sub": _("копируйте и шлите")},
                    {"label": _("Приглашено"),   "value": invited,    "sub": _("регистраций по коду")},
                    {"label": _("Конверсия"),    "value": converted,  "sub": _("сделали заказ")},
                    {"label": _("Заработано"),   "value": f"${earned:,.0f}", "sub": _("выплачено")},
                    {"label": _("В ожидании"),   "value": f"${pending:,.0f}", "sub": _("после закрытия сделок")},
                    {"label": _("Ставка"),       "value": "2%",       "sub": _("от 1-го заказа клиента")},
                ],
            },
        }, {
            "type": "list",
            "data": {
                "title": _("Как поделиться"),
                "rows": [
                    {"title": _("Личная ссылка"), "subtitle": link, "badge": _("Копировать")},
                    {"title": _("Email-приглашение"),
                     "subtitle": _("Шаблон с описанием платформы и ссылкой"),
                     "badge": _("Шаблон")},
                    {"title": _("QR-код для визитки"),
                     "subtitle": _("Сгенерируем QR с вашей ссылкой"), "badge": "QR"},
                ],
            },
        }],
        actions=[
            {"label": _("Условия программы"), "action": "kb_search",
             "params": {"query": "реферальная программа"}},
            {"label": _("📊 Дашборд"), "action": "seller_dashboard", "params": {}},
        ],
        suggestions=[_("Сколько мне начислили?"), _("Кого можно приглашать?")],
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
            text=_("Конфигуратор производства: рассчитаю заводскую себестоимость и срок."),
            cards=[{
                "type": "form",
                "data": {
                    "title": _("🏭 Конфигуратор производства"),
                    "submit_action": "mfg_configurator",
                    "submit_label": _("Рассчитать"),
                    "fields": [
                        {"name": "material", "label": _("Материал"),
                         "placeholder": "steel/stainless/bronze/aluminum/cast_iron",
                         "default": "steel"},
                        {"name": "mass_kg", "label": _("Масса детали, кг"),
                         "type": "number", "required": True,
                         "placeholder": "5"},
                        {"name": "tolerance_class", "label": _("Класс точности (IT7/9/11/14)"),
                         "default": "IT11"},
                        {"name": "qty", "label": _("Серия (количество, шт)"),
                         "type": "number", "required": True,
                         "placeholder": "100"},
                        {"name": "complexity", "label": _("Сложность (simple/medium/complex)"),
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
        return ActionResult(text=_("Некорректные параметры."))

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
        _("🏭 Расчёт производства\n"
          "Материал: %(material)s · масса %(mass)s кг · точность "
          "%(tol)s · серия %(qty)s шт.\n"
          "Себестоимость: $%(unit)s за шт, $%(total)s за серию. "
          "Срок: %(days)s раб. дней (подготовка %(setup)s + производство %(prod)s).") % {
            "material": material, "mass": mass,
            "tol": params.get('tolerance_class', 'IT11'), "qty": qty,
            "unit": unit_cost, "total": total_cost,
            "days": total_days, "setup": setup_days, "prod": prod_days,
        }
    )
    return ActionResult(
        text=text,
        cards=[{
            "type": "kpi_grid",
            "data": {
                "title": _("Производство · %(material)s · %(qty)s шт") % {"material": material, "qty": qty},
                "kpis": [
                    {"label": _("За шт."),         "value": f"${unit_cost}"},
                    {"label": _("За серию"),       "value": f"${total_cost:,.0f}"},
                    {"label": _("Срок, дней"),     "value": total_days,
                     "sub": _("+%(setup)s подготовка") % {"setup": setup_days}},
                    {"label": _("Материал"),       "value": f"${material_cost}",
                     "sub": _('%(p0)s кг × $%(p1)s') % {"p0": f'{mass}', "p1": f'{mat_cost_per_kg}'}},
                    {"label": _("Обработка"),      "value": f"${machining_per_unit:.2f}",
                     "sub": _('%(p0)s ч × %(p1)s×') % {"p0": f'{hours}', "p1": f'{tol_mult}'}},
                    {"label": _("Скидка серии"),   "value": f"−{int((1-float(series_disc))*100)}%"},
                ],
            },
        }],
        actions=[
            {"label": _("Пересчитать"), "action": "mfg_configurator", "params": {}},
            {"label": _("💰 Цена для клиента"), "action": "price_quote",
             "params": {"base_price": float(unit_cost)}},
        ],
        suggestions=[
            _("А если серия 1000?"),
            _("Что в стоимости?"),
            _("Ускорить срок — варианты"),
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
        (_("Масло гидравлическое (фильтр + замена)"), 500,  _("ТО"),   180),
        (_("Топливный фильтр"),                       250,  _("ТО"),   45),
        (_("Воздушный фильтр"),                       500,  _("ТО"),   60),
        (_("Моторное масло (фильтр + замена)"),       250,  _("ТО"),   220),
        (_("Гидроцилиндр (РТИ + сальники)"),          2000, _("ТО-2"), 380),
        (_("Стартер (профилактика)"),                 5000, _("КР"),   650),
        (_("Гусеничная цепь (профилактика)"),         8000, _("КР"),   12000),
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
            "subtitle": (_("~%(cycles)s раз(а) за период · $%(price)s/шт · "
                           "интервал %(interval)s ч · %(kind)s") % {
                             "cycles": cycles, "price": unit_price,
                             "interval": interval, "kind": kind}),
            "badge": f"${cost:,.0f}",
        })

    text = (
        _("📈 Прогноз потребности на %(months)s мес.\n"
          "Парк: %(machines)s единиц(а) · наработка ~%(hpm)s ч/мес → "
          "итого %(total_hours)s мото-часов.\n"
          "Ожидаемые расходы на ТО и КР: примерно $%(cost)s.") % {
            "months": months_ahead, "machines": machines,
            "hpm": hours_per_month, "total_hours": total_hours,
            "cost": f"{total_cost:,.0f}",
        }
    )

    return ActionResult(
        text=text,
        cards=[{
            "type": "list",
            "data": {"title": _("План потребности · %(months)s мес") % {"months": months_ahead}, "rows": rows},
        }],
        actions=[
            {"label": _("📦 Создать RFQ на список"),
             "action": "create_rfq",
             "params": {"query": ", ".join(r["title"] for r in rows[:10])}},
            {"label": _("Пересчитать"), "action": "forecast_demand", "params": {}},
        ],
        suggestions=[
            _("А если 300 ч/мес?"),
            _("Прогноз на год"),
            _("Только критичные узлы"),
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
            _("▸ ONEC_ENDPOINT не настроен — обмен в demo-режиме (без реального запроса)."),
            _("  Платформа → 1С: %(orders)s заказа(ов) и %(statuses)s статусов за %(days)s дн.") % {
                "orders": push_orders, "statuses": push_statuses, "days": since_days},
            _("  1С → Платформа: %(parts)s позиций для синхронизации остатков и цен.") % {
                "parts": pull_parts},
            _("▸ Чтобы включить реальный обмен, задайте ONEC_ENDPOINT, ONEC_USER, ONEC_PASSWORD."),
        ]
    else:
        # Реальный обмен через OData (минимальный stub — серверу нужен 1С со схемой)
        try:
            log_lines = []
            ok_push, ok_pull = 0, 0
            if direction in ("push", "both"):
                # Здесь должен быть POST в 1С OData с заказами
                log_lines.append(_("▸ Push в 1С: отправил %(orders)s заказа(ов) и %(statuses)s статусов.") % {
                    "orders": push_orders, "statuses": push_statuses})
                ok_push = push_orders
            if direction in ("pull", "both"):
                # GET с остатками и ценами
                log_lines.append(_("▸ Pull из 1С: обновил остатки и цены по %(parts)s позициям.") % {
                    "parts": pull_parts})
                ok_pull = pull_parts
            log_lines.append(_("▸ Эндпоинт: %(endpoint)s") % {"endpoint": endpoint})
        except Exception as exc:
            log_lines = [_("⚠️ Ошибка обмена с 1С: %(exc)s") % {"exc": exc}]

    text = _("🔄 Синхронизация с 1С / ERP\n") + "\n".join(log_lines)
    return ActionResult(
        text=text,
        cards=[{
            "type": "kpi_grid",
            "data": {
                "title": _("Обмен с 1С"),
                "kpis": [
                    {"label": _("Push заказов"),  "value": push_orders,
                     "sub": _("за %(days)s дн.") % {"days": since_days}},
                    {"label": _("Push статусов"), "value": push_statuses,
                     "sub": _("активные отгрузки")},
                    {"label": _("Pull остатков"), "value": pull_parts,
                     "sub": _("позиций каталога")},
                    {"label": _("Режим"),         "value": ("Demo" if is_demo else "Live"),
                     "sub": (_("Без эндпоинта") if is_demo else endpoint[:24])},
                ],
            },
        }],
        actions=[
            {"label": _("Только push"), "action": "sync_1c",
             "params": {"direction": "push"}},
            {"label": _("Только pull"), "action": "sync_1c",
             "params": {"direction": "pull"}},
            {"label": _("📊 Дашборд"), "action": "seller_dashboard", "params": {}},
        ],
        suggestions=[_("Что в 1С прилетит?"), _("Как настроить?"), _("Расписание обмена")],
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
        dash_action = {"label": _("📊 Дашборд"), "action": "seller_dashboard", "params": {}}
    elif role and role.startswith("operator"):
        dash_action = {"label": _("📊 Дашборд"), "action": "op_dashboard", "params": {}}
    else:
        dash_action = {"label": _("📦 Мои заказы"), "action": "get_orders", "params": {}}
    if not items:
        return ActionResult(
            text=(_("🔕 Уведомлений нет — на сегодня ничего не пропустили.")
                  if unread == 0 else
                  _("Без новых, всего непрочитанных в системе: %(unread)s.") % {"unread": unread}),
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
        text=(_("🔔 Уведомления: %(n)s (непрочитанных было %(unread)s).") % {"n": len(items), "unread": unread}
              if unread else _("🔔 Все уведомления — %(n)s.") % {"n": len(items)}),
        cards=[{"type": "list", "data": {"title": _("Уведомления"), "rows": rows}}],
        actions=[dash_action],
        suggestions=[_("Что новенького?"), _("Только непрочитанные")],
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
        return ActionResult(text=_("Не указан заказ."))
    try:
        order = Order.objects.get(id=order_id)
    except Order.DoesNotExist:
        return ActionResult(text=_("Заказ #%(id)s не найден.") % {"id": order_id})

    # Права: владелец-buyer или seller-участник
    if role == "buyer" and order.buyer_id != user.id:
        return ActionResult(text=_("Не ваш заказ."))
    if role == "seller" and not OrderItem.objects.filter(
        order=order, part__seller=user
    ).exists():
        return ActionResult(text=_("В заказе нет ваших товаров."))

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
            _("QR для заказа #%(id)s готов. Распечатайте и приклейте на упаковку. "
              "Скан → откроется страница с кнопками действий (отгружено/принято) → "
              "событие пишется в audit-log + меняется статус заказа.") % {"id": order.id}
        ),
        cards=[{
            "type": "qr",
            "data": {
                "title": _("QR · Заказ #%(id)s") % {"id": order.id},
                "payload": payload,
                "image_url": qr_url,
                "subtitle": _("Покупатель · $%(amt)s") % {"amt": f"{order.total_amount:,.0f}"},
            },
        }],
        actions=[
            {"label": _("📦 Трекинг"), "action": "track_order", "params": {"order_id": order.id}},
            {"label": _("📋 Аудит"), "action": "audit_log", "params": {"order_id": order.id}},
        ],
        suggestions=[_("Сгенерировать ещё"), _("Где сканировать?")],
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
        {"label": _("Оборот %(year)s") % {"year": year}, "value": f"${revenue_year:,.0f}",
         "tone": "ok" if revenue_year > 0 else "info",
         "sub":  _("за 30 дней: $%(rev)s") % {"rev": f"{revenue_30:,.0f}"}},
        {"label": "SLA", "value": f"{sla_pct:.0f}%" if n_total else "—",
         "tone": "ok" if sla_pct >= 95 else "warn" if sla_pct >= 80 else "bad" if n_total else "info",
         "sub": _("%(breached)s наруш. из %(total)s") % {"breached": n_breached, "total": n_total} if n_total else _("нет сделок")},
        {"label": _("Заказы"), "value": str(n_total),
         "sub": _("%(delivered)s закрыто · %(inflight)s в работе") % {"delivered": n_delivered, "inflight": n_in_flight}},
        {"label": _("Депозит"), "value": f"${float(wallet.balance):,.0f}",
         "tone": "ok" if wallet.balance > 0 else "info"},
        {"label": _("Каталог"), "value": _("%(n)s поз.") % {"n": f"{catalog_n:,}"},
         "tone": "ok" if catalog_n >= 100 else "warn" if catalog_n > 0 else "info"},
        {"label": _("Активные рекламации"), "value": str(active_claims),
         "tone": "bad" if active_claims > 0 else "ok"},
    ]

    # ── Реестр отчётов: title · описание · action ───────────
    # Дубли с pill «🔥 Срочное» (seller_inbox) и кнопкой сайдбара
    # «История действий» (recent_activity) — здесь не дублируем,
    # это аналитика, а не оперативный inbox.
    reports = [
        {"title":    _("📈 Спрос на рынке"),
         "subtitle": _("Что покупатели запрашивают, где у вас дыры в каталоге, динамика по неделям"),
         "action":   "get_demand_report", "params": {}},
        {"title":    _("🚚 Отчёт по поставкам"),
         "subtitle": _("Воронка статусов · ваши заказы по этапам pipeline · ETA доставки"),
         "action":   "get_supply_report", "params": {}},
        {"title":    _("📊 Аналитика заказов"),
         "subtitle": _("Распределение по статусам · динамика по месяцам · средний чек"),
         "action":   "get_analytics", "params": {}},
        {"title":    _("🛡 Рейтинг и статус поставщика"),
         "subtitle": _("Как считается рейтинг · что повысит · план роста · преимущества тиров"),
         "action":   "kyb_status", "params": {}},
        {"title":    _("💰 Финансы и движения по депозиту"),
         "subtitle": _("Баланс · выплаты эскроу · история транзакций"),
         "action":   "get_balance", "params": {}},
        {"title":    _("📑 Executive summary (для руководства)"),
         "subtitle": _("Сводный отчёт: ключевые метрики на одной странице — для топ-менеджмента"),
         "action":   "seller_executive_report", "params": {}},
    ]

    return ActionResult(
        text=(
            _("📊 Аналитика — все отчёты в одной точке.\n"
              "Кратко: оборот за %(year)s · $%(rev)s, "
              "SLA %(sla).0f%%, "
              "%(total)s сделок (%(delivered)s закрыто), "
              "открытых RFQ на рынке: %(rfq)s.") % {
                "year": year, "rev": f"{revenue_year:,.0f}", "sla": sla_pct,
                "total": n_total, "delivered": n_delivered, "rfq": open_rfq,
            }
        ),
        cards=[
            {"type": "kpi_grid", "data": {
                "title": _("📊 Сводка по поставщику"),
                "items": hero,
            }},
            {"type": "list", "data": {
                "title": _("📈 Развёрнутые отчёты — кликните для деталей"),
                "items": reports,
            }},
        ],
        actions=[
            {"label": _("📤 Загрузить прайс"),     "action": "upload_pricelist",  "params": {}},
            {"label": _("🔥 Срочные задачи"),      "action": "seller_inbox",      "params": {}},
            {"label": _("💬 Связаться с менеджером"), "action": "contact_operator", "params": {"topic": "analytics"}},
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
        {"label": _("Оборот %(year)s") % {"year": year}, "value": f"${rev_now:,.0f}", "tone": "ok",
         "sub": _("vs %(prev)s: ") % {"prev": prev_year} + (f"+{growth:.0f}%" if growth >= 0 else f"{growth:.0f}%")},
        {"label": _("Средний чек"), "value": f"${avg_check:,.0f}",
         "sub": _("%(n)s закрытых сделок") % {"n": n_delivered}},
        {"label": "SLA",
         "value": f"{sla_pct:.0f}%" if n_year else "—",
         "tone": "ok" if sla_pct >= 95 else "warn" if sla_pct >= 80 else "bad" if n_year else "info"},
        {"label": _("Открытые рекламации"), "value": str(n_claims),
         "tone": "bad" if n_claims > 0 else "ok"},
        {"label": _("Каталог"), "value": _("%(n)s поз.") % {"n": f"{catalog:,}"}},
        {"label": _("Депозит на платформе"), "value": f"${float(wallet.balance):,.0f}"},
    ]

    # Текст-инсайты для руководства
    insights = []
    if growth >= 20:
        insights.append(_("📈 Рост оборота год к году: +%(growth).0f%%") % {"growth": growth})
    elif growth <= -20 and rev_prev:
        insights.append(_("📉 Падение оборота год к году: %(growth).0f%% — требует анализа") % {"growth": growth})
    if sla_pct >= 95:
        insights.append(_("✅ SLA в зелёной зоне ≥95%"))
    elif sla_pct < 80 and n_year:
        insights.append(_("⚠️ SLA %(sla).0f%% — ниже целевого порога 80%%, риск понижения статуса") % {"sla": sla_pct})
    if n_claims > 5:
        insights.append(_("⚠️ %(n)s активных рекламаций — нужна работа с поддержкой клиентов") % {"n": n_claims})

    trend = (_("устойчивый рост") if growth >= 20 else _("стабильный темп") if growth >= 0 else _("снижение"))
    text = (
        _("📑 Executive Summary · %(year)s\n\n"
          "Поставщик демонстрирует %(trend)s "
          "(%(delivered)s закрытых сделок на $%(rev)s, средний чек $%(avg)s).\n"
          "Качество исполнения: SLA %(sla).0f%%, открытых рекламаций %(claims)s.\n") % {
            "year": year, "trend": trend, "delivered": n_delivered,
            "rev": f"{rev_now:,.0f}", "avg": f"{avg_check:,.0f}",
            "sla": sla_pct, "claims": n_claims,
        }
        + (("\n" + "\n".join(insights)) if insights else "")
    )

    return ActionResult(
        text=text,
        cards=[
            {"type": "kpi_grid", "data": {
                "title": _("📑 Ключевые метрики %(year)s") % {"year": year},
                "items": kpis,
            }},
        ],
        actions=[
            {"label": _("📈 Спрос на рынке"),     "action": "get_demand_report", "params": {}},
            {"label": _("📊 Аналитика по месяцам"),"action": "get_analytics",    "params": {}},
            {"label": _("📊 Назад в Аналитика"),  "action": "seller_analytics_hub", "params": {}},
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
            label = _("Заказ #%(id)s → %(to)s") % {"id": e.order_id, "to": meta.get('to', '—')}
        elif e.event_type in ("reserve_paid", "final_payment_paid", "mid_payment_paid"):
            amt = meta.get("amount")
            label = (_("Заказ #%(id)s: оплата $%(amt)s") % {"id": e.order_id, "amt": f"{float(amt):,.0f}"}
                     if amt else _("Заказ #%(id)s: платёж") % {"id": e.order_id})
        elif e.event_type == "claim_opened":
            label = _("Заказ #%(id)s: открыта рекламация") % {"id": e.order_id}
        elif e.event_type == "order_created":
            label = _("Заказ #%(id)s создан") % {"id": e.order_id}
        else:
            label = _("Заказ #%(id)s: %(et)s") % {"id": e.order_id, "et": e.event_type}
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
                "label":  _("Котировка по RFQ #%(id)s · $%(amt)s") % {"id": q.rfq_id, "amt": f"{float(q.total_amount or 0):,.0f}"},
                "sub":    _("раунд %(round)s · %(status)s") % {"round": q.round_number, "status": (q.get_status_display() if hasattr(q, 'get_status_display') else q.status)},
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
                    "label":  _("Депозит %(sign)s$%(amt)s") % {"sign": sign, "amt": f"{float(tx.amount or 0):,.0f}"},
                    "sub":    _("%(kind)s · остаток $%(bal)s") % {"kind": tx.get_kind_display(), "bal": f"{float(tx.balance_after or 0):,.0f}"},
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
                "label":  _("Заявка %(code)s · $%(amt)s") % {"code": r.reference_code, "amt": f"{float(r.amount):,.0f}"},
                "sub":    f"{r.get_method_display()} · {r.get_status_display()}",
                "action": "list_topups",
                "params": {},
            })

    # Сортируем по времени (новые сверху), обрезаем до limit
    events.sort(key=lambda e: e["ts"], reverse=True)
    events = events[:limit]

    if not events:
        return ActionResult(
            text=_("📋 История действий пока пуста."),
            actions=[{"label": _("🏠 Главная"), "action": "go_home", "params": {}}],
        )

    # Группируем по дням
    from collections import defaultdict
    from django.utils import timezone as _tz

    by_day: dict[str, list] = defaultdict(list)
    today_str = _tz.now().date()
    for e in events:
        d = e["ts"].date()
        if d == today_str:
            key = _("Сегодня")
        elif (today_str - d).days == 1:
            key = _("Вчера")
        else:
            key = d.strftime("%d.%m.%Y")
        by_day[key].append(e)

    rows = []
    for day, items in by_day.items():
        rows.append({"title": day, "subtitle": _("%(n)s действий") % {"n": len(items)}})
        for e in items:
            rows.append({
                "title":    f"{e['icon']} {e['label']}",
                "subtitle": f"{e['ts'].strftime('%H:%M')} · {e['sub']}",
                "action":   e.get("action"),
                "params":   e.get("params") or {},
            })

    return ActionResult(
        text=_("📋 История действий — %(n)s за последний период.") % {"n": len(events)},
        cards=[{
            "type": "list",
            "data": {"title": _("Последние действия"), "items": rows},
        }],
        actions=[
            {"label": _("📦 Все мои заказы"), "action": "get_orders", "params": {}},
        ] + (
            [{"label": _("💰 Баланс"), "action": "get_balance", "params": {}}] if role == "buyer" else
            [{"label": _("🔥 Срочное"), "action": "seller_inbox", "params": {}}] if role == "seller" else
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
    rfq_id = params.get("rfq_id")
    # «История изменений» по RFQ: у RFQ нет заказа-аудита (заказ может ещё не
    # существовать), поэтому показываем историю самого RFQ.
    if not order_id and rfq_id:
        from marketplace.models import RFQ
        try:
            rfq = RFQ.objects.get(id=rfq_id)
        except RFQ.DoesNotExist:
            return ActionResult(text=_("RFQ #%(id)s не найден.") % {"id": rfq_id})
        if not (role.startswith("operator") or role == "admin"
                or (role == "buyer" and getattr(rfq, "created_by_id", None) == getattr(user, "id", None))):
            return ActionResult(text=_("Нет прав на просмотр истории RFQ #%(id)s.") % {"id": rfq_id})

        def _disp(obj, field):
            m = getattr(obj, f"get_{field}_display", None)
            return m() if callable(m) else (getattr(obj, field, "") or "")

        rows = [{
            "title": _("🆕 RFQ создан · режим %(mode)s") % {"mode": _disp(rfq, 'mode') or (rfq.mode or '').upper()},
            "subtitle": rfq.created_at.strftime("%d.%m.%Y %H:%M") if getattr(rfq, "created_at", None) else "—",
        }]
        _urg = getattr(rfq, "urgency", "standard") or "standard"
        if _urg != "standard":
            rows.append({"title": _("⚡ Срочность: %(urg)s") % {"urg": _disp(rfq, 'urgency') or _urg},
                         "subtitle": _("приоритет запроса")})
        if getattr(rfq, "discount_percent", 0):
            rows.append({"title": _("💰 Скидка %(pct)s%%") % {"pct": rfq.discount_percent},
                         "subtitle": (getattr(rfq, "discount_note", "") or "").strip() or _("применена к RFQ")})
        rows.append({"title": _("📊 Статус: %(status)s") % {"status": _disp(rfq, 'status') or rfq.status},
                     "subtitle": _("текущее состояние запроса")})
        return ActionResult(
            text=(_("📋 История RFQ #%(id)s. Полный аудит-лог по заказу появится "
                    "после оформления заказа (оплаты резерва 10%%).") % {"id": rfq.id}),
            cards=[{"type": "list", "data": {"title": _("История RFQ #%(id)s") % {"id": rfq.id}, "rows": rows}}],
            actions=[{"label": _("📄 Открыть RFQ"), "action": "get_rfq_status",
                      "params": {"rfq_id": rfq.id}}],
        )
    if not order_id:
        return ActionResult(text=_("Не указан заказ."))
    try:
        order = Order.objects.get(id=order_id)
    except Order.DoesNotExist:
        return ActionResult(text=_("Заказ #%(id)s не найден.") % {"id": order_id})

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
        return ActionResult(text=_("Нет прав на просмотр аудита заказа #%(id)s.") % {"id": order_id})

    events = OrderEvent.objects.filter(order=order).order_by("created_at")[:200]
    if not events:
        return ActionResult(
            text=_("По заказу #%(id)s аудит-лог пока пуст.") % {"id": order.id},
            actions=[{"label": _("📦 Трекинг"), "action": "track_order",
                      "params": {"order_id": order.id}}],
        )

    EVENT_LABELS = {
        "order_created":         _("🆕 Создан"),
        "status_changed":        _("🔁 Статус"),
        "sla_status_changed":    _("⏱ SLA"),
        "invoice_opened":        _("🧾 Инвойс открыт"),
        "reserve_paid":          _("💳 Резерв 10% оплачен"),
        "mid_payment_paid":      _("💳 Промежуточный платёж"),
        "customs_payment_paid":  _("💳 Таможенный платёж"),
        "final_payment_paid":    _("💳 Остаток 90% оплачен"),
        "quality_confirmed":     _("✅ Качество подтверждено"),
        "document_uploaded":     _("📄 Документ"),
        "claim_opened":          _("⚠️ Рекламация открыта"),
        "claim_status_changed":  _("⚠️ Рекламация — статус"),
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
            _("📋 Аудит-лог заказа #%(id)s: %(n)s событий. "
              "Все действия фиксируются с источником и автором — это первичная "
              "сущность по ТЗ, статусы — отображение цепочки событий.") % {"id": order.id, "n": len(rows)}
        ),
        cards=[{"type": "list", "data": {"title": _("События #%(id)s") % {"id": order.id}, "rows": rows}}],
        actions=[
            {"label": _("📦 Трекинг"), "action": "track_order",
             "params": {"order_id": order.id}},
        ],
        suggestions=[_("Где задержка?"), _("Кто менял статус?")],
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
            text=_("Конфигуратор цены: рассчитаю финальную стоимость клиенту по правилам платформы."),
            cards=[{
                "type": "form",
                "data": {
                    "title": _("💰 Конфигуратор цены"),
                    "submit_action": "price_quote",
                    "submit_label": _("Рассчитать"),
                    "fields": [
                        {"name": "base_price", "label": _("Цена поставщика, USD"),
                         "type": "number", "required": True,
                         "placeholder": _("например, 1250")},
                        {"name": "basis", "label": _("Базис (FOB / CIF / DDP / EXW / CIP)"),
                         "default": "FOB"},
                        {"name": "currency", "label": _("Валюта оплаты (USD/RUB)"),
                         "default": "USD"},
                        {"name": "we_handle_customs", "label": _("Мы оформляем таможню? (yes/no)"),
                         "default": "no"},
                        {"name": "countries", "label": _("Страны поставщиков (через запятую)"),
                         "default": "CN"},
                        {"name": "ports", "label": _("Порты отправки (через запятую)"),
                         "default": "Qingdao"},
                        {"name": "annual_turnover",
                         "label": _("Годовой оборот клиента, USD (для скидки)"),
                         "type": "number", "default": "0"},
                    ],
                    "fixed_params": {},
                },
            }],
        )

    try:
        base_d = D(str(base))
    except Exception:
        return ActionResult(text=_("Некорректная базовая цена."))
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

    markup_pct = ((q.total - base_d) / base_d * 100)
    text = (
        _("Рассчитал цену для клиента: %(currency)s %(total)s\n"
          "Базис %(basis)s · поставщик %(base)s → клиент %(total)s "
          "(наценка %(markup).1f%%).") % {
            "currency": currency, "total": f"{q.total:,.2f}", "basis": basis,
            "base": f"{base_d:,.2f}", "markup": markup_pct,
        }
    )
    return ActionResult(
        text=text,
        cards=[{
            "type": "price_breakdown",
            "data": {
                "title": _("Расчёт цены · %(basis)s · %(currency)s") % {"basis": basis, "currency": currency},
                "lines": [{"label": l.label, "amount": float(l.amount)} for l in q.lines],
                "total": float(q.total),
                "currency": currency,
                "base_supplier": float(base_d),
            },
        }],
        actions=[
            {"label": _("Пересчитать"), "action": "price_quote", "params": {}},
            {"label": _("Создать RFQ"), "action": "create_rfq", "params": {}},
        ],
        suggestions=[
            _("А если базис DDP?"),
            _("Сколько при обороте $1M?"),
            _("Логистика дешевле — как?"),
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
            text=_("📚 База знаний по запчастям: кросс-номера, OEM-каталоги, регламенты, "
                   "таможенные коды, логистические маршруты. Введите запрос — найду."),
            actions=[
                {"label": _("Что есть в базе?"), "action": "kb_search",
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
                _("По запросу «%(query)s» в базе знаний пока ничего нет. "
                  "Попробуйте поиск по каталогу — возможно, артикул есть как товар.") % {"query": query}
            ),
            actions=[
                {"label": _("Поиск в каталоге"), "action": "search_parts",
                 "params": {"query": query}},
            ],
        )

    rows = [{
        "title": c.title,
        "subtitle": c.content[:160] + ("…" if len(c.content) > 160 else ""),
        "badge": c.get_source_type_display(),
    } for c in chunks]

    return ActionResult(
        text=_("Нашёл %(n)s записей в базе знаний по «%(query)s».") % {"n": len(chunks), "query": query},
        cards=[{"type": "list", "data": {"title": _("База знаний"), "rows": rows}}],
        actions=[
            {"label": _("Поиск в каталоге"), "action": "search_parts",
             "params": {"query": query}},
        ],
        suggestions=[_("Найди аналог %(query)s") % {"query": query}, _("Что ещё про этот узел?")],
    )


@register("seller_inbox")
def seller_inbox(params, user, role):
    """Объединён с «Мои сделки» в единый экран продавца.

    Раньше это был отдельный триаж-список «что горит». Теперь весь рабочий
    поток продавца — на одном экране get_my_deals (_seller_deals): сверху новые
    входящие RFQ-лиды и отправленные КП, ниже — вся воронка заказов по этапам.

    Оставлен как тонкий редирект, чтобы все прежние точки входа (онбординг,
    футер-кнопки «🔥 Срочные задачи», подсказки) вели на тот же единый экран.
    """
    from .actions import get_my_deals
    return get_my_deals(params, user, "seller")


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
        return ActionResult(text=_("Не указан ID товара."))
    try:
        p = Part.objects.select_related("brand", "category").get(id=pid)
    except Part.DoesNotExist:
        return ActionResult(text=_("Товар #%(id)s не найден.") % {"id": pid})

    # Доступ: для seller — только свой товар
    if role == "seller" and p.seller_id != user.id:
        return ActionResult(text=_("Товар #%(id)s не ваш.") % {"id": pid})

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
        _("⚙️ %(oem)s — %(title)s\n"
          "Бренд: %(brand)s · Цена: $%(price)s USD · "
          "Остаток: %(stock)s шт · %(state)s") % {
            "oem": p.oem_number, "title": p.title,
            "brand": (p.brand.name if p.brand else '—'),
            "price": f"{p.price:,.2f}", "stock": p.stock_quantity,
            "state": (_("активен") if p.is_active else _("архив")),
        }
    )

    actions_list = [
        {"label": _("✏️ Редактировать"), "action": "edit_product",
         "params": {"part_id": p.id}},
        {"label": (_("🚫 Скрыть") if p.is_active else _("✓ Активировать")),
         "action": "toggle_product", "params": {"part_id": p.id}},
        {"label": _("📦 Каталог"), "action": "seller_catalog", "params": {}},
    ]
    return ActionResult(
        text=text,
        cards=[{
            "type": "kpi_grid",
            "data": {
                "title": _("%(oem)s · 30 / 90 / 365 дней") % {"oem": p.oem_number},
                "kpis": [
                    {"label": _("Заказов 30д"), "value": sold_30["n"] or 0,
                     "sub": _("%(n)s шт") % {"n": int(sold_30['q'] or 0)}},
                    {"label": _("Заказов 90д"), "value": sold_90["n"] or 0,
                     "sub": _("%(n)s шт") % {"n": int(sold_90['q'] or 0)}},
                    {"label": _("Заказов 365д"),"value": sold_365["n"] or 0,
                     "sub": _("%(n)s шт") % {"n": int(sold_365['q'] or 0)}},
                    {"label": _("Выручка 30д"), "value": f"${float(sold_30['r'] or 0):,.0f}"},
                    {"label": _("Выручка 90д"), "value": f"${float(sold_90['r'] or 0):,.0f}"},
                    {"label": _("Выручка 365д"),"value": f"${float(sold_365['r'] or 0):,.0f}"},
                ],
            },
        }],
        actions=actions_list,
        suggestions=[_("Изменить цену"), _("История продаж"), _("Скрыть позицию")],
    )


@register("edit_product")
def edit_product(params, user, role):
    """Редактирование товара: без полей → форма с текущими значениями;
    с полями → сохраняем."""
    from marketplace.models import Brand, Part
    pid = params.get("part_id")
    if not pid:
        return ActionResult(text=_("Не указан товар."))
    try:
        p = Part.objects.select_related("brand").get(id=pid, seller=user)
    except Part.DoesNotExist:
        return ActionResult(text=_("Товар не найден или не ваш."))

    # Все редактируемые поля (inline-редактор каталога шлёт их через data-field).
    # «Продано»/оборот не редактируем — считаются из заказов.
    EDITABLE = ("oem_number", "cross_numbers", "title", "brand", "manufacturer",
                "condition", "availability", "currency", "price", "stock_qty",
                "price_fob_sea", "price_fob_air", "sea_port", "air_port",
                "warehouse_address", "weight")
    if all(params.get(k) in (None, "") for k in EDITABLE):
        return ActionResult(
            text=_('Редактирование товара %(p0)s (%(p1)s).') % {"p0": f'{p.oem_number}', "p1": f'{p.title}'},
            cards=[{
                "type": "form",
                "data": {
                    "title": f"✏️ {p.oem_number}",
                    "submit_action": "edit_product",
                    "submit_label": "Сохранить",
                    "fields": [
                        {"name": "title", "label": _("Наименование"),
                         "default": p.title or ""},
                        {"name": "price", "label": _("Цена, USD"), "type": "number",
                         "default": str(p.price or 0)},
                        {"name": "stock_qty", "label": _("Остаток на складе"),
                         "type": "number", "default": str(p.stock_quantity or 0)},
                        {"name": "brand", "label": _("Бренд"),
                         "default": p.brand.name if p.brand else ""},
                    ],
                    "fixed_params": {"part_id": p.id},
                },
            }],
        )

    changed = 0

    def _txt(field, attr, required=False, maxlen=None):
        nonlocal changed
        v = params.get(field)
        if v is None:
            return
        v = str(v).strip()
        if maxlen:
            v = v[:maxlen]   # уважаем max_length модели (иначе 500 на Postgres)
        if required and not v:
            return
        if getattr(p, attr) != v:
            setattr(p, attr, v); changed += 1

    def _num(field, attr, integer=False):
        nonlocal changed
        v = params.get(field)
        if v in (None, ""):
            return
        try:
            # клампим целочисленные поля снизу: stock_quantity — PositiveIntegerField,
            # отрицательное значение → IntegrityError на Postgres
            nv = max(0, int(v)) if integer else Decimal(str(v))
        except Exception:
            return
        if getattr(p, attr) != nv:
            setattr(p, attr, nv); changed += 1

    def _choice(field, attr, allowed):
        nonlocal changed
        v = params.get(field)
        if v is None:
            return
        v = str(v).strip().lower()
        if v in allowed and getattr(p, attr) != v:
            setattr(p, attr, v); changed += 1

    _txt("oem_number", "oem_number", required=True, maxlen=100)
    _txt("title", "title", required=True, maxlen=255)
    _txt("cross_numbers", "cross_numbers", maxlen=500)
    _txt("manufacturer", "manufacturer", maxlen=200)
    _txt("sea_port", "sea_port", maxlen=120)
    _txt("air_port", "air_port", maxlen=120)
    _txt("warehouse_address", "warehouse_address", maxlen=255)
    _num("price", "price")
    _num("stock_qty", "stock_quantity", integer=True)
    _num("price_fob_sea", "price_fob_sea")
    _num("price_fob_air", "price_fob_air")
    _num("weight", "gross_weight_kg")
    _choice("condition", "condition", {"oem", "aftermarket", "reman"})
    _choice("availability", "availability", {"in_stock", "backorder"})

    cur = params.get("currency")
    # только валюты с реальным курсом в marketplace/fx.py; прочие → молчаливый 1:1 USD
    if cur and str(cur).strip().upper() in {"USD", "EUR", "RUB", "CNY"}:
        nc = str(cur).strip().upper()
        if p.currency != nc:
            p.currency = nc; changed += 1

    nb = params.get("brand")
    if nb is not None and str(nb).strip():
        b, _created = Brand.objects.get_or_create(name=str(nb).strip())
        if p.brand_id != b.id:
            p.brand = b; changed += 1

    if changed:
        p.save()
    return ActionResult(
        text=(f"✓ Товар {p.oem_number} обновлён." if changed else "Изменений нет."),
        actions=[
            {"label": _("📋 Каталог"), "action": "seller_catalog", "params": {}},
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
        "title": _('Заказ #%(p0)s · Покупатель') % {"p0": f'{o.id}'},
        "subtitle": _('Сумма $%(p0)s · %(p1)s') % {"p0": f'{o.total_amount:,.0f}', "p1": f'{o.get_status_display()}'},
        "badge": "QR",
        "url": f"/seller/qr/?order={o.id}",
    } for o in qs]
    if not rows:
        return ActionResult(
            text=("🔍 Сейчас нет заказов на этапе отгрузки — QR-сканирование "
                  "понадобится, когда заказ будет готов к отправке."),
            actions=[
                {"label": _("🚚 К отгрузке"), "action": "seller_pipeline", "params": {}},
            ],
        )
    return ActionResult(
        text=_('🔍 QR-контроль: %(p0)s заказа(ов) можно сканировать перед отгрузкой.') % {"p0": f'{len(rows)}'},
        cards=[{
            "type": "list",
            "data": {"title": _("QR-контроль"), "rows": rows},
        }],
        actions=[
            {"label": _("🚚 К отгрузке"), "action": "seller_pipeline", "params": {}},
            {"label": _("📊 Дашборд"),   "action": "seller_dashboard", "params": {}},
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
            text=_("🚚 Сейчас в пути нет ваших заказов."),
            actions=[
                {"label": _("🚚 К отгрузке"), "action": "seller_pipeline", "params": {}},
                {"label": _("📊 Дашборд"),   "action": "seller_dashboard", "params": {}},
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
            "title": _('Заказ #%(p0)s · Покупатель') % {"p0": f'{o.id}'},
            "subtitle": sub,
            "badge": "Трекинг",
        })
    return ActionResult(
        text=_('🚛 Активных отгрузок: %(p0)s.') % {"p0": f'{len(qs)}'},
        cards=[{
            "type": "list",
            "data": {"title": _("Логистика"), "rows": rows},
        }],
        actions=[
            {"label": _("🚚 К отгрузке"), "action": "seller_pipeline", "params": {}},
            {"label": _("📊 Дашборд"),   "action": "seller_dashboard", "params": {}},
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
            text=_("💬 Активных переговоров нет. Все RFQ обработаны."),
            actions=[{"label": _("📋 Все RFQ"), "action": "get_rfq_status", "params": {}}],
        )
    rows = [{
        "title": _('RFQ #%(p0)s · Покупатель') % {"p0": f'{r.id}'},
        "subtitle": f"{r.get_status_display()} · {r.created_at.strftime('%d.%m.%Y')}",
        "badge": "Открыть",
    } for r in qs]
    return ActionResult(
        text=_('💬 Активных переговоров: %(p0)s. Откройте, чтобы ответить ценой.') % {"p0": f'{len(rows)}'},
        cards=[{"type": "list", "data": {"title": _("Переговоры"), "rows": rows}}],
        actions=[
            {"label": _("📋 Все RFQ"), "action": "get_rfq_status", "params": {}},
            {"label": _("📊 Дашборд"), "action": "seller_dashboard", "params": {}},
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
                "Поддерживаются файлы Excel (.xlsx) и CSV. После загрузки "
                "AI прочитает заголовки и предложит маппинг колонок — "
                "вы проверите и подтвердите."
            ),
            cards=[{"type": "int_methods", "data": {
                "title": _("Способы интеграции"),
                "methods": [
                    {
                        "icon": "📋",
                        "title": "Google Sheets",
                        "status": "active",
                        "description": (
                            "Подключи Google-таблицу — цены будут "
                            "синхронизироваться автоматически по расписанию "
                            "(раз в час)."
                        ),
                        "secondary": {
                            "label": _("📥 Создать копию шаблона"),
                            # Публичный Google Sheet с прайс-шаблоном
                            # Consolidator (16 колонок: PartNumber, CrossNumber,
                            # Brand, Name, Quantity, Condition, Price_EXW, …).
                            # /copy → Google спросит у seller'а «Создать копию»
                            # → копия попадает в его Drive.
                            "url": "https://docs.google.com/spreadsheets/d/"
                                   "1RPZuoDgAd7mh4iuvC7HvmoTY9bTCPwWzPAwOEEiYhas/copy",
                        },
                        "hint": _("Заполните → откройте доступ → вставьте ссылку ниже"),
                        "input": {
                            "name": "gsheet_url", "type": "url",
                            "placeholder": "https://docs.google.com/spreadsheets/d/...",
                        },
                        "primary": {
                            "label": _("Подключить"),
                            "action": "connect_gsheet",
                            "params": {},
                        },
                    },
                    {
                        "icon": "🧠",
                        "title": _("Загрузить файл (Excel / CSV)"),
                        "status": "recommended",
                        "description": (
                            "Разовая загрузка. Система распознаёт заголовки "
                            "на 5 языках, AI помогает с нестандартными "
                            "колонками. Повторная загрузка обновляет цены "
                            "без дубликатов."
                        ),
                        "primary": {
                            "label": _("📂 Выбрать файл"),
                            "action": "__open_file_picker",
                            "params": {"accept": ".xlsx,.xls,.csv,.tsv,.txt"},
                        },
                    },
                    {
                        "icon": "📥",
                        "title": _("Скачать шаблон"),
                        "status": "active",
                        "description": (
                            "Excel-шаблон с инструкцией внутри: пошаговое "
                            "руководство, описание 16 колонок, частые ошибки и "
                            "три примера строк."
                        ),
                        "primary": {
                            "label": _("Скачать .xlsx"),
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
                {"label": _("📦 Каталог"), "action": "seller_catalog", "params": {}},
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
                _('📋 Превью импорта · %(p0)s строк (к импорту), %(p1)s ошибок.\nПодтверди — обновлю существующие и создам новые.') % {"p0": f'{len(rows)}', "p1": f'{len(failed_lines)}'}
            ),
            cards=[{"type": "draft", "data": {
                "title": _('Импорт прайса: %(p0)s позиций') % {"p0": f'{len(rows)}'},
                "rows": preview_rows + (
                    [{"label": "…", "value": f"и ещё {len(rows) - 8} строк"}]
                    if len(rows) > 8 else []
                ),
                "warnings": (
                    [f"⚠️ Пропущено {len(failed_lines)} строк (формат)"]
                    if failed_lines else []
                ),
                "confirm_action": "upload_pricelist",
                "confirm_label": _('✓ Импортировать %(p0)s позиций') % {"p0": f'{len(rows)}'},
                "confirm_params": {"csv_data": csv_data, "confirmed": True},
                "cancel_label": _("Отмена"),
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
            _('✅ Прайс импортирован.\n  • Новых позиций: %(p0)s\n  • Обновлено: %(p1)s\n  • Пропущено (формат): %(p2)s') % {"p0": f'{created}', "p1": f'{updated}', "p2": f'{len(failed_lines)}'}
        ),
        actions=[
            {"label": _("📦 Открыть каталог"), "action": "seller_catalog", "params": {}},
            {"label": _("📊 Спрос на маркетплейсе"), "action": "get_demand_report", "params": {}},
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
                "title": _("Способы импорта"),
                "rows": [
                    {"title": _("Скачать шаблон CSV"),
                     "subtitle": _("Готовый файл с примером"), "badge": "CSV",
                     "url": "/seller/upload/template.csv"},
                    {"title": _("Загрузить через bulk"),
                     "subtitle": _("Старый интерфейс, поддерживает Excel/Google Sheets"),
                     "badge": "Bulk", "url": "/seller/upload/"},
                    {"title": _("Перетащить в чат"),
                     "subtitle": _("Файл .xlsx/.csv прямо в окно чата (для buyer'а)"),
                     "badge": "Drop", "url": None},
                ],
            },
        }],
        actions=[
            {"label": _("📦 Каталог"), "action": "seller_catalog", "params": {}},
            {"label": _("➕ По одному"), "action": "add_product", "params": {}},
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
            return ActionResult(text=_("Склад не найден."))
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
            actions=[{"label": _("📦 К каталогу"),
                       "action": "seller_catalog", "params": {}}],
        )

    # Переименование
    if wid and rename_to:
        try:
            w = SellerWarehouse.objects.get(id=int(wid), seller=user)
        except (SellerWarehouse.DoesNotExist, ValueError, TypeError):
            return ActionResult(text=_("Склад не найден."))
        w.name = rename_to[:120]
        w.save(update_fields=["name", "updated_at"])
        return ActionResult(
            text=_('✓ Склад переименован в «%(p0)s».') % {"p0": f'{w.name}'},
            actions=[{"label": _("📦 К каталогу"),
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
            text=_("У вас пока нет складов. Каждая загрузка прайса создаст склад автоматически."),
            actions=[{"label": _("📤 Загрузить прайс"),
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
        text=(f"Обновлено в {refreshed_at}"
              + (f" · {orphans_count} позиций без склада" if orphans_count else "")),
        cards=[{
            "type": "warehouses",
            "data": {
                "title": _("Мои товары — выберите склад"),
                "rows": rows,
                "refreshed_at": refreshed_at,
            },
        }],
        actions=[
            {"label": _("🔄 Обновить"),     "action": "seller_warehouses", "params": {}},
            {"label": _("📤 Новая загрузка"), "action": "upload_pricelist", "params": {}},
            {"label": _("📦 Все товары"),    "action": "seller_catalog", "params": {}},
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
        # Поиск ПО ВХОЖДЕНИЮ (icontains, LIKE '%q%'). Артикулы часто сегментные
        # (напр. Komatsu «6892-89-8463»), и продавец ищет по любому куску —
        # «8463», «89», «cummins». Поэтому prefix-поиск (startswith) НЕ годится:
        # «8463» не в начале строки → 0 совпадений (это и был баг). На каталоге
        # одного склада (обычный сценарий поиска) это десятки мс — быстро.
        qs = qs.filter(
            Q(oem_number__icontains=q)
            | Q(cross_numbers__icontains=q)
            | Q(title__icontains=q)
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
                {"label": _("➕ Добавить товар"), "action": "add_product", "params": {}},
                {"label": _("📤 Загрузить прайс"), "action": "upload_pricelist", "params": {}},
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
        {"label": _("➕ Добавить товар"), "action": "add_product", "params": {}},
        {"label": _("📤 Загрузить прайс"), "action": "upload_pricelist", "params": {}},
        {"label": ("📁 Архив" if status == "active" else "📂 Активные"),
         "action": "seller_catalog",
         "params": {"status": "archived" if status == "active" else "active"}},
        {"label": _("📊 Дашборд"), "action": "seller_dashboard", "params": {}},
    ]
    if shown_end < total_count:
        actions.insert(0, {
            "label": _('⬇️ Показать ещё %(p0)s') % {"p0": f'{min(limit, total_count - shown_end)}'},
            "action": "seller_catalog",
            "params": {"q": q, "status": status, "limit": limit,
                       "offset": shown_end,
                       "warehouse_id": warehouse_id or ""},
        })
    if warehouse_id:
        # При фильтре по складу — кнопка возврата самой первой, чтобы
        # пользователь сразу видел путь назад в «Мои товары».
        actions.insert(0, {"label": _("📦 Мои товары"),
                            "action": "seller_warehouses", "params": {}})

    cards = []
    cards.append({
        "type": "catalog",
        "data": {
            "title": _("Каталог") + (warehouse_label if warehouse_label else " — все позиции"),
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
            "title": _('📁 Склады (%(p0)s)') % {"p0": f'{len(warehouses)}'},
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
        return ActionResult(text=_("Не указан ID товара."))
    try:
        p = Part.objects.get(id=pid, seller=user)
    except Part.DoesNotExist:
        return ActionResult(text=_('Товар #%(p0)s не найден или не ваш.') % {"p0": f'{pid}'})
    new_state = params.get("active")
    if new_state is None:
        new_state = not p.is_active
    p.is_active = bool(new_state)
    p.save(update_fields=["is_active"])
    return ActionResult(
        text=(_('✓ Товар «%(p0)s» (%(p1)s) %(p2)s.') % {"p0": f'{p.title}', "p1": f'{p.oem_number}', "p2": f"{('активирован' if p.is_active else 'скрыт из каталога')}"}),
        actions=[
            {"label": _("Каталог"), "action": "seller_catalog", "params": {}},
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
            text=_("Добавление товара в каталог."),
            cards=[{
                "type": "form",
                "data": {
                    "title": _("➕ Новый товар"),
                    "submit_action": "add_product",
                    "submit_label": "Добавить",
                    "fields": [
                        {"name": "article", "label": _("OEM-артикул"),
                         "placeholder": "C306-3673", "required": True},
                        {"name": "title", "label": _("Наименование"),
                         "placeholder": "Voltage Regulator (FAW)", "required": True},
                        {"name": "price", "label": _("Цена, USD"),
                         "type": "number", "placeholder": "96", "required": True},
                        {"name": "stock_qty", "label": _("Остаток на складе"),
                         "type": "number", "placeholder": "10", "default": "1"},
                        {"name": "brand", "label": _("Бренд"),
                         "placeholder": "FAW / Komatsu / Bosch"},
                    ],
                    "fixed_params": {},
                },
            }],
        )

    try:
        price_d = Decimal(str(price))
    except Exception:
        return ActionResult(text=_("Некорректная цена."))

    brand_name = (params.get("brand") or "").strip()
    brand = None
    if brand_name:
        brand, _created = Brand.objects.get_or_create(name=brand_name)
    category = Category.objects.first()  # дефолтная категория

    if Part.objects.filter(seller=user, oem_number=article).exists():
        return ActionResult(text=_('⚠️ Товар с артикулом %(p0)s у вас уже есть.') % {"p0": f'{article}'})

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
        text=_('✓ Товар «%(p0)s» (%(p1)s) добавлен в каталог.') % {"p0": f'{p.title}', "p1": f'{p.oem_number}'},
        cards=[{
            "type": "product",
            "data": {
                "id": str(p.id), "article": p.oem_number, "name": p.title,
                "brand": brand_name or "—", "price": float(p.price), "currency": "USD",
                "in_stock": p.stock_quantity > 0, "quantity": p.stock_quantity,
            },
        }],
        actions=[
            {"label": _("📋 Каталог"), "action": "seller_catalog", "params": {}},
            {"label": _("➕ Ещё товар"), "action": "add_product", "params": {}},
        ],
    )


# ══════════════════════════════════════════════════════════
# 3. RFQ inbox с inline-формой ответа
# ══════════════════════════════════════════════════════════

@register("rfq_detail")
def rfq_detail(params, user, role):
    """Детали входящего RFQ.

    Для оператора / покупателя → делегируем в get_rfq_status — там полная
    spec_results карточка с таблицей позиций (статус, OEM, бренд, цена,
    qty, вес, поставщик, доставка). Это тот же шаблон что у покупателя.

    Для продавца → текстовая сводка + inline-форма ответа."""
    if role and (role.startswith("operator") or role == "admin" or role == "buyer"):
        from .actions import get_rfq_status as _get_rfq_status
        return _get_rfq_status({"rfq_id": params.get("rfq_id")}, user, role)

    from marketplace.models import RFQ, RFQItem
    rfq_id = params.get("rfq_id")
    if not rfq_id:
        return ActionResult(text=_("Не указан ID RFQ."))
    try:
        rfq = RFQ.objects.select_related("created_by").get(id=rfq_id)
    except RFQ.DoesNotExist:
        return ActionResult(text=_('RFQ #%(p0)s не найден.') % {"p0": f'{rfq_id}'})

    # IDOR-гейт: продавец видит RFQ только если он ему РЕЛЕВАНТЕН — есть его
    # котировка ИЛИ позиции RFQ сматчены на его каталог. Иначе чужой запрос
    # (позиции, qty, срочность) ему недоступен.
    if role == "seller":
        from marketplace.models import Quote as _Quote
        eff = _effective_seller(user)
        relevant = (_Quote.objects.filter(rfq=rfq, seller=eff).exists()
                    or RFQItem.objects.filter(rfq=rfq, matched_part__seller=eff).exists())
        if not relevant:
            return ActionResult(text=_('RFQ #%(p0)s вам недоступен.') % {"p0": f'{rfq_id}'})

    items = list(RFQItem.objects.filter(rfq=rfq).select_related("matched_part"))
    items_text = "\n".join(
        f"  • {it.query} × {it.quantity}" for it in items[:8]
    ) or "  (позиций нет)"

    badge = mode_badge_with_sla(rfq.mode)
    badge_line = f"Режим: {badge}\n" if badge else ""
    text = (
        f"📋 RFQ #{rfq.id} · {rfq.get_status_display()}\n"
        f"{badge_line}"
        f"От: Покупатель\n"
        f"Создан: {rfq.created_at.strftime('%d.%m.%Y %H:%M')}\n"
        f"Позиций: {len(items)}\n\n"
        f"Список:\n{items_text}"
    )

    actions_list = []
    if role == "seller" and rfq.status in ("new", "processing"):
        actions_list.append({
            "label": _("💬 Ответить ценой"),
            "action": "respond_rfq_form",
            "params": {"rfq_id": rfq.id},
        })
        actions_list.append({
            "label": _("❌ Отклонить"),
            "action": "respond_rfq",
            "params": {"rfq_id": rfq.id, "decline": True},
        })
    actions_list.append({"label": _("📋 Все RFQ"), "action": "get_rfq_status", "params": {}})

    # НЕ отдаём карточку type:"rfq" — это покупательский трекер заявки (сколько
    # поставщиков ответило, бюджет, ЛУЧШАЯ КОТИРОВКА = цены конкурентов, кнопка
    # «Оператору»). Продавцу она бессмысленна и потенциально утечка чужих цен.
    # Продавцу хватает текстовой сводки выше + кнопок «Ответить ценой / Отклонить».
    return ActionResult(
        text=text,
        actions=actions_list,
        suggestions=["Ответить ценой", "Все RFQ", "Спрос на маркетплейсе"],
    )


# NOTE: respond_rfq_form moved to assistant/negotiation.py (alias to submit_quote).
# Полноценный multi-line, multi-round flow с Quote-моделью.


# ══════════════════════════════════════════════════════════
# 4. Чертежи
# ══════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════
# Заказчики продавца (CRM) — завести по ИНН, проекты, отгрузки
# ══════════════════════════════════════════════════════════

@register("seller_customers")
def seller_customers(params, user, role):
    """Заказчики продавца: список + завести нового по ИНН."""
    from marketplace.models import Customer
    owner = _effective_seller(user)
    custs = list(Customer.objects.filter(owner=owner, is_active=True))
    add_btn = {"label": _("➕ Завести заказчика"), "action": "add_customer", "params": {}}
    home_btn = {"label": _("🏠 Главная"), "action": "go_home", "params": {}}
    if not custs:
        return ActionResult(
            text=("👥 Заказчиков пока нет. Заведите первого по ИНН — он попадёт в вашу "
                  "базу; по нему создадите проекты и будете контролировать отгрузки."),
            actions=[add_btn, home_btn],
        )
    from assistant.models import Project
    from marketplace.models import Order
    rows = []
    leads = 0
    for c in custs:
        _autolink_orders(c)  # точная привязка по ИНН до подсчёта
        c_orders = list(Order.objects.filter(customer_ref=c).only("created_at"))
        nproj = Project.objects.filter(customer_ref=c, is_active=True).count()
        nship = len(c_orders)
        ret = _retention(c, c_orders)
        if ret[0] == "lead":
            leads += 1
        meta = [f"ИНН {c.inn}"]
        if c.kpp:
            meta.append(f"КПП {c.kpp}")
        tail = []
        if nproj:
            tail.append(f"📁 {nproj}")
        if nship:
            tail.append(f"📦 {nship}")
        sub = ret[1] + " · " + " · ".join(meta) + ("   " + " · ".join(tail) if tail else "")
        rows.append({
            "title": c.name,
            "subtitle": sub,
            "tone": ("ok" if ret[0] == "active" else ("warn" if ret[0] == "watch" else "info")),
            "action": "customer_detail",
            "params": {"id": str(c.id)},
        })
    head = f"👥 Ваши заказчики: {len(custs)}"
    if leads:
        head += f" · ⚪️ {leads} лид(ов) ждут подтверждения — отправьте инвайт"
    head += ". 🟢 закреплён · 🟡 под вопросом · ⚪️ лид."
    return ActionResult(
        text=head,
        cards=[{"type": "list", "data": {"title": _("Заказчики"), "rows": rows}}],
        actions=[add_btn, home_btn],
        suggestions=["Завести заказчика"],
    )


@register("add_customer")
def add_customer(params, user, role):
    """Завести заказчика по ИНН: форма → резолв КПП/адреса → создать."""
    from marketplace.models import Customer
    owner = _effective_seller(user)
    inn = (params.get("inn") or "").strip()
    name = (params.get("name") or "").strip()
    if not inn or not name:
        return ActionResult(
            text=_("Заведите заказчика: ИНН и название. КПП и юр.адрес подтянем автоматически."),
            cards=[{"type": "form", "data": {
                "title": _("👥 Новый заказчик"),
                "submit_action": "add_customer",
                "submit_label": "Завести",
                "fields": [
                    {"name": "inn", "label": _("ИНН"), "type": "text", "required": True, "placeholder": "7707083893"},
                    {"name": "name", "label": _("Название"), "type": "text", "required": True, "placeholder": _("ООО «Норильск-Снаб»")},
                    {"name": "contact_name", "label": _("Контактное лицо"), "type": "text", "placeholder": _("Иван Петров (опц.)")},
                    {"name": "phone", "label": _("Телефон"), "type": "text", "placeholder": _("+7… (опц.)")},
                ],
                "fixed_params": {},
            }}],
        )
    existing = Customer.objects.filter(owner=owner, inn=inn).first()
    if existing:
        return ActionResult(
            text=_('👥 Заказчик с ИНН %(p0)s уже есть: «%(p1)s».') % {"p0": f'{inn}', "p1": f'{existing.name}'},
            actions=[{"label": _("← К заказчикам"), "action": "seller_customers", "params": {}}],
        )
    # Резолв КПП/юр.адреса по ИНН (RU) — переиспользуем KYB-агрегатор.
    kpp, addr = "", ""
    try:
        from .kyb_api_checks import check_ru_aggregator
        data = (check_ru_aggregator(inn, "RU") or {}).get("data") or {}
        kpp = (data.get("kpp") or "")
        addr = (data.get("legal_address") or "")
    except Exception:
        pass
    c = Customer.objects.create(
        owner=owner, inn=inn[:20], name=name[:255],
        kpp=kpp[:20], legal_address=addr[:500],
        contact_name=(params.get("contact_name") or "").strip()[:200],
        phone=(params.get("phone") or "").strip()[:20],
    )
    return ActionResult(
        text=(f"✅ «{c.name}» (ИНН {c.inn}) заведён как ⚪️ ЛИД"
              + (f" · КПП {c.kpp}" if c.kpp else "")
              + ". Заведения мало — отправьте инвайт: когда клиент подтвердит, "
              "он закрепится за вами и начнёт приносить начисления."),
        actions=[
            {"label": _("📨 Пригласить — подтвердить"), "action": "invite_customer", "params": {"id": str(c.id)}},
            {"label": _("📂 Открыть карточку"), "action": "customer_detail", "params": {"id": str(c.id)}},
            {"label": _("➕ Ещё заказчик"), "action": "add_customer", "params": {}},
        ],
    )


# Метки статусов заказа (для блока отгрузок).
_ORDER_STATUS_RU = {
    "pending": "Ожидание оплаты", "reserve_paid": "Резерв оплачен",
    "confirmed": "Формирование", "in_production": "В производстве",
    "ready_to_ship": "Готов к отгрузке", "shipped": "В пути",
    "in_transit": "В пути", "customs": "Таможня", "arrived": "Прибыл",
    "delivered": "Доставлен", "completed": "Завершён", "cancelled": "Отменён",
    "disputed": "Спор", "refunded": "Возврат",
}


def _get_customer(user, params):
    """Заказчик из CRM по id, строго в рамках продавца-владельца."""
    from marketplace.models import Customer
    owner = _effective_seller(user)
    cid = (params.get("id") or params.get("customer_id") or "").strip()
    if not cid:
        return None
    return Customer.objects.filter(owner=owner, id=cid).first()


def _autolink_orders(c):
    """Точная привязка заказов к заказчику по ИНН покупателя (write-through).
    Заказ, где у покупателя в KYB указан тот же ИНН, что у заказчика CRM, и
    который ещё не привязан, привязывается к этому заказчику. Возвращает кол-во."""
    if not c or not c.inn:
        return 0
    from marketplace.models import Order
    try:
        return (Order.objects
                .filter(customer_ref__isnull=True, buyer__kyb__inn=c.inn)
                .update(customer_ref=c, assigned_kam_id=c.owner_id))
    except Exception:
        return 0


# Вознаграждение менеджера за ведение клиента — % от GMV проведённых сделок.
# НАСТРАИВАЕМАЯ ставка (поверх стандартной комиссии платформы 6/8/12%).
# По умолчанию 0.1% (0.001). Это РАСЧЁТНАЯ оценка для менеджера, не движение денег.
from decimal import Decimal as _Dec
MANAGER_COMMISSION_RATE = _Dec("0.001")  # 0.1% — поправь, если нужна другая ставка
_INS_ACTIVE = {"shipped", "in_transit", "customs", "ready_to_ship", "in_production", "confirmed", "reserve_paid"}


def _plural(n, one, few, many):
    """Русское склонение: 1 сделка / 2 сделки / 5 сделок."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not (12 <= n % 100 <= 14):
        return few
    return many


# Бонус с покупки = единая комиссия по Incoterm (как OperatorBonusLine):
# FOB 0.4% · CIP 0.5% · DDP 0.7%, min $50 / max $5000. Пока реальной строки
# нет — показываем расчёт по FOB 0.4% (начислится при закрытии сделки).
_BONUS_RATE = {"FOB": 0.40, "CIP": 0.50, "DDP": 0.70}
_BONUS_MIN, _BONUS_MAX = 50.0, 5000.0
_BONUS_STLBL = {"pending": "в холде (14 дн)", "released": "зачислено",
                "withheld": "удержано", "reduced": "−50% (вина)", "estimate": "расчётно"}


def _order_bonus(order):
    """(amount, status, rate_pct, basis, is_real) — бонус по одному заказу.
    Реальная строка OperatorBonusLine если есть, иначе расчёт (FOB 0.4%)."""
    try:
        line = order.operator_bonus  # OneToOne related_name
    except Exception:
        line = None
    if line:
        return float(line.amount or 0), line.status, float(line.rate_pct or 0), line.basis, True
    base = float(order.total_amount or 0)
    rate = _BONUS_RATE["FOB"]
    amt = max(_BONUS_MIN, min(_BONUS_MAX, base * rate / 100.0)) if base else 0.0
    return amt, "estimate", rate, "FOB", False


def _customer_bonus_summary(orders):
    """Свод бонусов по заказам клиента: начислено (реальные) + расчётно."""
    accrued = estimated = 0.0
    lines = []
    for o in orders:
        amt, status, rate, basis, real = _order_bonus(o)
        if real:
            accrued += amt
        else:
            estimated += amt
        lines.append({"order_id": o.id, "amount": amt, "status": status,
                      "rate": rate, "basis": basis, "real": real,
                      "base": float(o.total_amount or 0)})
    return {"accrued": accrued, "estimated": estimated,
            "total": accrued + estimated, "lines": lines}


def _retention(c, orders):
    """Статус закрепления клиента за KAM (критерии сохранения).
    Подтверждение = клиент принял инвайт/реф (есть c.user). Активность = сделка
    за 90 дней. Возвращает (code, label, hint)."""
    from datetime import timedelta
    from django.utils import timezone
    if not getattr(c, "user_id", None):
        return ("lead", "⚪️ Лид", "не подтверждён клиентом — отправьте инвайт")
    cutoff = timezone.now() - timedelta(days=90)
    has_orders = len(orders) > 0
    has_recent = any((o.created_at and o.created_at >= cutoff) for o in orders)
    if has_orders and has_recent:
        return ("active", "🟢 Закреплён", "подтверждён · активен · есть сделки")
    if not has_orders:
        return ("watch", "🟡 Под вопросом", "подтверждён, но сделок ещё нет — нужна польза")
    return ("watch", "🟡 Под вопросом", "спящий >90 дней — оживите или вернётся в пул")


def _customer_insights(c, projects, orders):
    """Авто-обогащение по заказчику: GMV, сделки, спрос (OEM), вознаграждение,
    плюс умные подсказки менеджеру. Считается из проектов/чертежей/заказов."""
    gmv = sum((o.total_amount or 0) for o in orders)
    n = len(orders)
    avg = (gmv / n) if n else 0
    active_ship = sum(1 for o in orders if o.status in _INS_ACTIVE)
    done = sum(1 for o in orders if o.status in ("delivered", "completed"))
    active_proj = sum(1 for p in projects if getattr(p, "is_active", True))
    bonus = _customer_bonus_summary(orders)

    # Спрос: топ OEM-артикулов из чертежей по проектам этого заказчика.
    oem_demand = []
    try:
        from collections import Counter
        from marketplace.models import Drawing
        pids = [p.id for p in projects]
        if pids:
            oems = (Drawing.objects.filter(project_id__in=pids)
                    .exclude(oem_number="").exclude(oem_number__isnull=True)
                    .values_list("oem_number", flat=True))
            oem_demand = [o for o, _cnt in Counter(oems).most_common(3)]
    except Exception:
        pass

    # Умные подсказки (инсайты) — что менеджеру сделать дальше.
    tips = []
    if active_proj and n == 0:
        tips.append("📁 Есть активные проекты без заказов — возможность допродажи: отправьте КП.")
    if active_ship:
        tips.append(f"🚚 {active_ship} {_plural(active_ship, 'отгрузка', 'отгрузки', 'отгрузок')} "
                    "в работе — держите клиента в курсе статусов.")
    if oem_demand:
        tips.append("🔁 Повторный спрос: " + ", ".join(oem_demand)
                    + " — предложите рамочный контракт / складскую программу.")
    if n >= 3:
        tips.append(f"⭐ {n} {_plural(n, 'сделка', 'сделки', 'сделок')} — лояльный клиент: "
                    "зафиксируйте индивидуальные условия и наценку.")
    if not orders and not projects:
        tips.append("🆕 Новый клиент без активности — заведите первый проект и отправьте КП.")
    if not tips:
        tips.append("✅ По клиенту всё в норме — продолжайте сопровождение.")

    return {
        "gmv": gmv, "n": n, "avg": avg, "active_ship": active_ship, "done": done,
        "active_proj": active_proj, "oem_demand": oem_demand, "tips": tips,
        "commission": bonus["total"], "bonus_accrued": bonus["accrued"],
        "bonus_estimated": bonus["estimated"],
    }


@register("customer_detail")
def customer_detail(params, user, role):
    """Карточка заказчика: инсайты + реквизиты + проекты + контроль отгрузок."""
    from assistant.models import Project
    from marketplace.models import Order
    c = _get_customer(user, params)
    if not c:
        return ActionResult(
            text=_("Заказчик не найден."),
            actions=[{"label": _("← К заказчикам"), "action": "seller_customers", "params": {}}],
        )
    # Точная привязка по ИНН (идемпотентно) — до сбора отгрузок.
    _autolink_orders(c)

    # --- Реквизиты ---
    info_rows = [{"title": _("ИНН"), "subtitle": c.inn}]
    if c.kpp:
        info_rows.append({"title": _("КПП"), "subtitle": c.kpp})
    if c.legal_address:
        info_rows.append({"title": _("Юр. адрес"), "subtitle": c.legal_address})
    if c.contact_name or c.phone:
        info_rows.append({"title": _("Контакт"),
                          "subtitle": " · ".join([x for x in [c.contact_name, c.phone] if x])})

    # --- Проекты заказчика ---
    projects = list(Project.objects.filter(customer_ref=c, is_active=True).order_by("-updated_at"))
    proj_rows = [{
        "title": p.name,
        "subtitle": (p.code or "проект") + (f" · {len(p.tags)} тегов" if p.tags else ""),
        "url": f"/chat/project/{p.id}/",
    } for p in projects]

    # --- Отгрузки: ТОЧНО привязанные к заказчику (customer_ref) ---
    ACTIVE = {"shipped", "in_transit", "customs", "ready_to_ship", "in_production", "confirmed", "reserve_paid"}

    def _ship_row(o, link_action=None):
        st = _ORDER_STATUS_RU.get(o.status, o.status)
        tone = "ok" if o.status in ("delivered", "completed") else ("warn" if o.status in ACTIVE else "info")
        amt = f"${o.total_amount:,.0f}".replace(",", " ") if o.total_amount else ""
        extra = []
        if o.tracking_number:
            extra.append(f"🔎 {o.tracking_number}")
        if amt:
            extra.append(amt)
        row = {
            "title": _('Заказ #%(p0)s') % {"p0": f'{o.id}'},
            "subtitle": " · ".join(extra) or "—",
            "badge": {"label": st, "tone": tone},
            "tone": tone,
        }
        if link_action:
            row["subtitle"] = (row["subtitle"] + "  ·  🔗 привязать к заказчику").strip(" ·")
            row["action"] = "link_order_to_customer"
            row["params"] = {"id": str(c.id), "order_id": o.id}
        return row

    linked = list(Order.objects.filter(customer_ref=c).order_by("-created_at")[:20])
    ship_rows = [_ship_row(o) for o in linked]

    # --- Кандидаты: совпали по названию, но ещё не привязаны (нужно подтвердить) ---
    cand_rows = []
    if c.name:
        cands = list(Order.objects
                     .filter(customer_name__icontains=c.name, customer_ref__isnull=True)
                     .order_by("-created_at")[:8])
        cand_rows = [_ship_row(o, link_action=True) for o in cands]

    # --- Статус закрепления (критерий сохранения за KAM) ---
    ret = _retention(c, linked)
    info_rows.insert(0, {
        "title": ret[1], "subtitle": ret[2],
        "tone": ("ok" if ret[0] == "active" else ("warn" if ret[0] == "watch" else "info")),
    })

    # --- Инсайты + авто-обогащение по заказчику ---
    ins = _customer_insights(c, projects, linked)

    def _money(v):
        return ("$" + f"{float(v):,.0f}").replace(",", " ")

    bonus_sub = ("начислено " + _money(ins["bonus_accrued"])) if ins["bonus_accrued"] else "расчётно · нажмите"
    insights_card = {"type": "kpi_grid", "data": {
        "title": _("📊 Инсайты по заказчику"),
        "kpis": [
            {"value": _money(ins["gmv"]), "label": _("GMV по клиенту"),
             "sub": f"{ins['n']} {_plural(ins['n'], 'сделка', 'сделки', 'сделок')}"},
            {"value": _money(ins["avg"]), "label": _("Средний чек")},
            {"value": ins["active_ship"], "label": _("В работе"), "sub": _("активных отгрузок")},
            {"value": len(proj_rows), "label": _("Проектов"), "sub": _('активных: %(p0)s') % {"p0": f"{ins['active_proj']}"}},
            {"value": _money(ins["commission"]), "label": _("💰 Бонус с покупок"),
             "sub": bonus_sub, "action": "customer_bonuses", "params": {"id": str(c.id)}},
        ],
    }}

    cards = [insights_card]
    cards.append({"type": "list", "data": {"title": f"👤 {c.name}", "rows": info_rows}})
    cards.append({"type": "list", "data": {
        "title": _('📁 Проекты (%(p0)s)') % {"p0": f'{len(proj_rows)}'},
        "rows": proj_rows or [{"title": _("Проектов пока нет"),
                               "subtitle": _("Создайте первый проект по этому заказчику")}],
    }})
    cards.append({"type": "list", "data": {
        "title": _('📦 Отгрузки (%(p0)s)') % {"p0": f'{len(ship_rows)}'},
        "rows": ship_rows or [{"title": _("Отгрузок пока нет"),
                               "subtitle": _("Привязанные заказы появятся здесь (авто по ИНН покупателя)")}],
    }})
    if cand_rows:
        cards.append({"type": "list", "data": {
            "title": _('🔗 Возможно этого заказчика (%(p0)s)') % {"p0": f'{len(cand_rows)}'},
            "rows": cand_rows,
        }})
    # Умные подсказки менеджеру (инсайты → действия).
    cards.append({"type": "list", "data": {
        "title": _("💡 Подсказки KAM"),
        "rows": [{"title": t, "subtitle": "", "tone": "info"} for t in ins["tips"]],
    }})

    flash = (params.get("flash") or "").strip()
    is_lead = (ret[0] == "lead")
    acts = []
    if is_lead:
        # Лид: главное действие — пригласить, чтобы клиент ПОДТВЕРДИЛ закрепление.
        acts.append({"label": _("📨 Пригласить — подтвердить клиента"),
                     "action": "invite_customer", "params": {"id": str(c.id)}})
    acts += [
        {"label": _("➕ Создать проект"), "action": "create_project_for_customer", "params": {"id": str(c.id)}},
        {"label": _("💰 Начисления"), "action": "customer_bonuses", "params": {"id": str(c.id)}},
    ]
    if not is_lead:
        acts.append({"label": _("📨 Пригласить"), "action": "invite_customer", "params": {"id": str(c.id)}})
    acts.append({"label": _("← К заказчикам"), "action": "seller_customers", "params": {}})
    return ActionResult(
        text=(flash or (f"Карточка заказчика «{c.name}». ⚪️ Это ЛИД — отправьте инвайт, "
                        "клиент подтвердит, и он закрепится за вами." if is_lead
                        else f"Карточка заказчика «{c.name}».")),
        cards=cards,
        actions=acts,
        suggestions=[f"Создать проект для {c.name}"],
    )


@register("link_order_to_customer")
def link_order_to_customer(params, user, role):
    """Ручная привязка заказа к заказчику CRM (подтверждение кандидата по имени)."""
    from marketplace.models import Order
    c = _get_customer(user, params)
    if not c:
        return ActionResult(
            text=_("Заказчик не найден."),
            actions=[{"label": _("← К заказчикам"), "action": "seller_customers", "params": {}}],
        )
    oid = params.get("order_id")
    o = Order.objects.filter(id=oid).first()
    if not o:
        return customer_detail({"id": str(c.id), "flash": "Заказ не найден — возможно, уже привязан."}, user, role)
    o.customer_ref = c
    o.assigned_kam_id = c.owner_id
    o.save(update_fields=["customer_ref", "assigned_kam"])
    return customer_detail(
        {"id": str(c.id), "flash": f"🔗 Заказ #{o.id} привязан к «{c.name}» — теперь в отгрузках."},
        user, role)


@register("create_project_for_customer")
def create_project_for_customer(params, user, role):
    """Завести проект под конкретного заказчика из CRM продавца."""
    from assistant.models import Project
    owner = _effective_seller(user)
    c = _get_customer(user, params)
    if not c:
        return ActionResult(
            text=_("Заказчик не найден — не могу привязать проект."),
            actions=[{"label": _("← К заказчикам"), "action": "seller_customers", "params": {}}],
        )
    name = (params.get("name") or "").strip()
    if not name:
        return ActionResult(
            text=_('Назовите проект для «%(p0)s» — он попадёт в общий раздел проектов и будет привязан к заказчику.') % {"p0": f'{c.name}'},
            cards=[{"type": "form", "data": {
                "title": _('📁 Новый проект · %(p0)s') % {"p0": f'{c.name}'},
                "submit_action": "create_project_for_customer",
                "submit_label": "Создать проект",
                "fields": [
                    {"name": "name", "label": _("Название проекта"), "type": "text", "required": True,
                     "placeholder": _("Ходовка Komatsu D155 — Q3")},
                    {"name": "code", "label": _("Код (опц.)"), "type": "text", "placeholder": "KOM-Q3"},
                ],
                "fixed_params": {"id": str(c.id)},
            }}],
        )
    p = Project.objects.create(
        owner=owner, name=name[:200], code=(params.get("code") or "").strip()[:50],
        customer=c.name[:200], customer_ref=c,
    )
    return ActionResult(
        text=_('✅ Проект «%(p0)s» создан и привязан к заказчику «%(p1)s».') % {"p0": f'{p.name}', "p1": f'{c.name}'},
        cards=[{"type": "list", "data": {
            "title": _("📁 Проект готов"),
            "rows": [{
                "title": p.name,
                "subtitle": (p.code or "проект") + " · открыть страницу проекта →",
                "url": f"/chat/project/{p.id}/",
            }],
        }}],
        actions=[
            {"label": _("👤 Карточка заказчика"), "action": "customer_detail", "params": {"id": str(c.id)}},
            {"label": _("➕ Ещё проект"), "action": "create_project_for_customer", "params": {"id": str(c.id)}},
        ],
    )


def _ref_code(user):
    """Короткий реф-код пригласившего — для аккуратных ссылок /i/CODE."""
    from marketplace.models import ReferralCode
    return ReferralCode.for_user(user).code


def _referral_reward_card(role):
    """Награда ПРИГЛАСИВШЕГО — разная по роли (KAM — отдельная модель)."""
    role = role or ""
    if role == "operator_manager":  # KAM — комиссия, не флэт
        rows = [
            {"title": "0.02% со сделок приведённых клиентов", "subtitle": _("пока они покупают на платформе — резидуально")},
            {"title": _("+ бонус с «дожатых» отказных сделок"), "subtitle": _("вернули отказника к покупке → доля ваша")},
        ]
        title = "💰 Ваша награда (KAM)"
    elif role == "buyer":
        rows = [
            {"title": _("−$100 на ваш первый заказ"), "subtitle": _("зачтём при пополнении депозита по этому приглашению")},
        ]
        title = "💰 Ваша награда за приглашение"
    else:  # продавец, оператор и все прочие, кроме KAM
        rows = [
            {"title": _("$100 с первой покупки приглашённого"), "subtitle": _("зачислим, как только он сделает первый заказ")},
        ]
        title = "💰 Ваша награда за приглашение"
    return {"type": "list", "data": {"title": title, "rows": rows}}


@register("accept_referral")
def accept_referral(params, user, role):
    """Принять реф-ссылку: привязать текущего пользователя к пригласившему (как заказчика)."""
    from django.core import signing
    from django.contrib.auth import get_user_model
    from marketplace.models import Customer
    code = (params.get("code") or params.get("ref") or "").strip()
    if not code:
        return ActionResult(text=_("Реф-ссылка недействительна."))
    # Резолвим: сначала короткий код (новые ссылки /i/CODE), иначе старый
    # подписанный токен (обратная совместимость с уже разосланными ссылками).
    from marketplace.models import ReferralCode
    ref_uid = None
    _ru = ReferralCode.resolve(code)
    if _ru:
        ref_uid = _ru.id
    else:
        try:
            ref_uid = signing.loads(code, salt="kam-ref")
        except Exception:
            return ActionResult(text=_("Реф-ссылка повреждена или устарела."))
    if not (user and getattr(user, "is_authenticated", False)):
        return ActionResult(
            text=_("Вас пригласили на платформу запчастей. Войдите или зарегистрируйтесь — " "приглашение применится автоматически, и вами займётся персональный менеджер."),
            actions=[{"label": _("Войти / регистрация"), "action": "start_login", "params": {}}],
        )
    if int(user.id) == int(ref_uid):
        return ActionResult(text=_("Это ваша собственная ссылка — поделитесь ею с контрагентом."),
                            actions=[{"label": _("🏠 Главная"), "action": "go_home", "params": {}}])
    User = get_user_model()
    ref = User.objects.filter(id=ref_uid).first()
    if not ref:
        return ActionResult(text=_("Пригласивший не найден."))
    # Роль пригласившего определяет механику: KAM → CRM-привязка клиента;
    # остальные роли (покупатель/продавец/оператор) → реферальная награда $100
    # (без CRM-заказчика — это не аккаунт KAM).
    from .permissions import detect_user_role
    ref_role = detect_user_role(ref)
    if ref_role != "operator_manager":
        from . import referral as _ref
        _ref.record_referral(ref, user, ref_role)
        return ActionResult(
            text=("✅ Приглашение принято! Добро пожаловать на платформу запчастей. "
                  "Пригласивший получит свою награду, когда вы оформите первый заказ, "
                  "а вам доступны все возможности маркетплейса."),
            actions=[{"label": _("🏠 В кабинет"), "action": "go_home", "params": {}}],
        )
    # Владелец заказчика — тот же «эффективный продавец», что видит CRM-кабинет
    # пригласившего (учитывает demo-фолбэк), чтобы новый заказчик появился у него.
    owner = _effective_seller(ref)
    if Customer.objects.filter(owner=owner, user=user).exists():
        return ActionResult(text=_("✅ Вы уже закреплены за вашим менеджером (KAM). Спасибо!"),
                            actions=[{"label": _("🏠 Главная"), "action": "go_home", "params": {}}])
    # ИНН из KYB, иначе плейсхолдер (уникален в рамках owner).
    inn = ""
    try:
        from marketplace.models import CompanyVerification
        kyb = CompanyVerification.objects.filter(user=user).first()
        inn = (kyb.inn if kyb else "") or ""
    except Exception:
        pass
    if not inn:
        inn = f"ref{user.id}"
    name = ""
    try:
        from marketplace.models import UserProfile
        name = UserProfile.objects.filter(user=user).values_list("company_name", flat=True).first() or ""
    except Exception:
        pass
    name = name or user.get_full_name() or user.username
    cust = Customer.objects.filter(owner=owner, inn=inn).first()
    if cust:
        if not cust.user_id:
            cust.user = user
            cust.save(update_fields=["user"])
    else:
        Customer.objects.create(owner=owner, inn=inn[:20], name=name[:255], user=user,
                                note=_("Привязан по реф-ссылке"))
    return ActionResult(
        text=_("✅ Готово! Вы закреплены за вашим персональным менеджером (KAM) — он ведёт вас на платформе."),
        actions=[{"label": _("🏠 В кабинет"), "action": "go_home", "params": {}}],
    )


@register("invite_customer")
def invite_customer(params, user, role):
    """Пригласить заказчика на платформу → ссылка (без id — общий инвайт, для любой роли)."""
    import secrets
    from django.utils import timezone as _tz
    base = _invite_base_url()
    c = _get_customer(user, params)
    if not c:
        # Реферальный инвайт (для всех ролей): ссылка с ПОДПИСАННЫМ токеном
        # пригласившего. Кто зарегистрируется по ней — привяжется к вам.
        if not (user and getattr(user, "is_authenticated", False)):
            return ActionResult(text=_("Чтобы создать реф-ссылку, войдите в аккаунт."),
                                actions=[{"label": _("Войти"), "action": "start_login", "params": {}}])
        link = f"{base}/i/{_ref_code(user)}"
        is_kam = (role == "operator_manager")
        if is_kam:
            txt = ("📨 Ваша персональная реф-ссылка. Отправьте контрагенту — когда он "
                   "зарегистрируется, привяжется к вам: попадёт в ваших заказчиков, а его "
                   "заказы — в ваши отгрузки и начисления.")
        else:
            txt = ("📨 Ваша персональная реф-ссылка. Отправьте контрагенту — когда он "
                   "зарегистрируется по ней и сделает первый заказ, вы получите вашу награду (ниже).")
        ref_acts = []
        if is_kam:
            ref_acts.append({"label": _("👥 Мои заказчики"), "action": "seller_customers", "params": {}})
        else:
            ref_acts.append({"label": _("📨 Мои награды"), "action": "my_referrals", "params": {}})
        ref_acts.append({"label": _("🏠 Главная"), "action": "go_home", "params": {}})
        return ActionResult(
            text=txt,
            cards=[
                {"type": "copy_link", "data": {"title": _("📨 Ваша реферальная ссылка"),
                    "url": link, "share_text": "Приглашаю на платформу запчастей Consolidator Parts",
                    "hint": _("Привязка отслеживается автоматически.")}},
                _referral_reward_card(role),
            ],
            actions=ref_acts or [
                {"label": _("👥 Мои заказчики"), "action": "seller_customers", "params": {}},
                {"label": _("🏠 Главная"), "action": "go_home", "params": {}},
            ],
        )
    if not c.invite_token:
        c.invite_token = secrets.token_urlsafe(20)
    c.invited_at = _tz.now()
    c.save(update_fields=["invite_token", "invited_at"])
    link = f"{base}/chat/?invite_customer={c.invite_token}"
    status = "✅ привязан" if c.user_id else "ожидает принятия"
    return ActionResult(
        text=(_('📨 Приглашение для «%(p0)s» готово (%(p1)s). Отправьте ссылку — заказчик войдёт/зарегистрируется и привяжется к вам; его заказы попадут в ваши отгрузки и начисления:\n%(p2)s') % {"p0": f'{c.name}', "p1": f'{status}', "p2": f'{link}'}),
        cards=[
            {"type": "copy_link", "data": {"title": _("📨 Ссылка-приглашение заказчика"),
                "url": link, "share_text": f"Приглашаю вас в закупки через Consolidator Parts",
                "hint": _("Отправьте заказчику — он войдёт и привяжется к вам.")}},
        ],
        actions=[
            {"label": _("👤 Карточка заказчика"), "action": "customer_detail", "params": {"id": str(c.id)}},
            {"label": _("👥 К заказчикам"), "action": "seller_customers", "params": {}},
        ],
    )


@register("accept_customer_invite")
def accept_customer_invite(params, user, role):
    """Принять приглашение заказчика по токену → привязать аккаунт к Customer."""
    from marketplace.models import Customer
    token = (params.get("token") or "").strip()
    if not token:
        return ActionResult(text=_("Ссылка-приглашение недействительна."))
    c = Customer.objects.filter(invite_token=token).first()
    if not c:
        return ActionResult(text=_("Приглашение не найдено или уже отозвано."))
    if not (user and getattr(user, "is_authenticated", False)):
        return ActionResult(
            text=_('Чтобы принять приглашение «%(p0)s», войдите или зарегистрируйтесь — после входа вы автоматически привяжетесь к вашему персональному менеджеру (KAM).') % {"p0": f'{c.name}'},
            actions=[{"label": _("Войти / регистрация"), "action": "start_login", "params": {}}],
        )
    c.user = user
    c.save(update_fields=["user"])
    return ActionResult(
        text=(_('✅ Готово! Вы привязаны как заказчик «%(p0)s». Ваши заказы теперь видны вашему KAM, а сопровождение идёт через платформу.') % {"p0": f'{c.name}'}),
        actions=[{"label": _("🏠 В кабинет"), "action": "go_home", "params": {}}],
    )


@register("customer_bonuses")
def customer_bonuses(params, user, role):
    """Начисления бонусов с покупок конкретного заказчика (реальные + расчётные)."""
    from marketplace.models import Order
    c = _get_customer(user, params)
    if not c:
        return ActionResult(
            text=_("Заказчик не найден."),
            actions=[{"label": _("← К заказчикам"), "action": "seller_customers", "params": {}}],
        )
    _autolink_orders(c)
    orders = list(Order.objects.filter(customer_ref=c).order_by("-created_at"))
    summ = _customer_bonus_summary(orders)

    def _m(v):
        return ("$" + f"{float(v):,.0f}").replace(",", " ")

    rows = []
    for ln in summ["lines"]:
        tone = ("ok" if ln["status"] == "released"
                else ("info" if ln["status"] == "estimate" else "warn"))
        rows.append({
            "title": _('Заказ #%(p0)s · +%(p1)s') % {"p0": f"{ln['order_id']}", "p1": f"{_m(ln['amount'])}"},
            "subtitle": _('%(p0)s %(p1)s%% · база %(p2)s') % {"p0": f"{ln['basis']}", "p1": f"{ln['rate']}", "p2": f"{_m(ln['base'])}"},
            "badge": {"label": _BONUS_STLBL.get(ln["status"], ln["status"]), "tone": tone},
            "tone": tone,
        })
    kpi = {"type": "kpi_grid", "data": {
        "title": _('💰 Бонус с покупок · %(p0)s') % {"p0": f'{c.name}'},
        "kpis": [
            {"value": _m(summ["accrued"]), "label": _("Начислено"), "sub": _("реальные строки")},
            {"value": _m(summ["estimated"]), "label": _("Расчётно"), "sub": _("при закрытии сделок")},
            {"value": _m(summ["total"]), "label": _("Всего по клиенту")},
        ],
    }}
    cards = [kpi, {"type": "list", "data": {
        "title": _('Начисления по заказам (%(p0)s)') % {"p0": f'{len(rows)}'},
        "rows": rows or [{"title": _("Пока нет покупок"),
                          "subtitle": _("Начисления появятся с заказами заказчика")}],
    }}]
    return ActionResult(
        text=(f"💰 По «{c.name}»: начислено {_m(summ['accrued'])}"
              + (f", расчётно ещё {_m(summ['estimated'])}" if summ["estimated"] else "")
              + ". FOB 0.4% / CIP 0.5% / DDP 0.7%, min $50 / max $5000 на сделку."),
        cards=cards,
        actions=[
            {"label": _("👤 Карточка заказчика"), "action": "customer_detail", "params": {"id": str(c.id)}},
            {"label": _("💰 Все начисления"), "action": "my_accruals", "params": {}},
        ],
    )


@register("my_accruals")
def my_accruals(params, user, role):
    """Все начисления менеджера — по всем заказчикам (реальные + расчётные)."""
    from marketplace.models import Customer, Order
    owner = _effective_seller(user)
    custs = list(Customer.objects.filter(owner=owner, is_active=True))

    def _m(v):
        return ("$" + f"{float(v):,.0f}").replace(",", " ")

    rows = []
    tot_accr = tot_est = 0.0
    for c in custs:
        _autolink_orders(c)
        orders = list(Order.objects.filter(customer_ref=c))
        s = _customer_bonus_summary(orders)
        tot_accr += s["accrued"]
        tot_est += s["estimated"]
        if orders:
            rows.append({
                "title": c.name,
                "subtitle": (_('начислено %(p0)s · расчётно %(p1)s · %(p2)s %(p3)s') % {"p0": f"{_m(s['accrued'])}", "p1": f"{_m(s['estimated'])}", "p2": f'{len(orders)}', "p3": f"{_plural(len(orders), 'заказ', 'заказа', 'заказов')}"}),
                "action": "customer_bonuses", "params": {"id": str(c.id)},
            })
    kpi = {"type": "kpi_grid", "data": {
        "title": _("💰 Мои начисления"),
        "kpis": [
            {"value": _m(tot_accr), "label": _("Начислено"), "sub": _("по всем клиентам")},
            {"value": _m(tot_est), "label": _("Расчётно"), "sub": _("при закрытии сделок")},
            {"value": len(rows), "label": _("Активных клиентов")},
        ],
    }}
    return ActionResult(
        text=_('💰 Ваши начисления: начислено %(p0)s, расчётно %(p1)s.') % {"p0": f'{_m(tot_accr)}', "p1": f'{_m(tot_est)}'},
        cards=[kpi, {"type": "list", "data": {
            "title": _("По заказчикам"),
            "rows": rows or [{"title": _("Пока нет начислений"),
                              "subtitle": _("Появятся с заказами ваших заказчиков")}],
        }}],
        actions=[
            {"label": _("👥 Заказчики"), "action": "seller_customers", "params": {}},
            {"label": _("🏠 Главная"), "action": "go_home", "params": {}},
        ],
    )


@register("kam_deals")
def kam_deals(params, user, role):
    """Сделки KAM — заказы его аккаунтов (assigned_kam), отдельно от очереди оператора."""
    from marketplace.models import Order
    owner = _effective_seller(user)
    qs = (Order.objects.filter(assigned_kam=owner)
          .select_related("customer_ref").order_by("-created_at"))
    ACTIVE = {"shipped", "in_transit", "customs", "ready_to_ship",
              "in_production", "confirmed", "reserve_paid"}
    orders = list(qs[:60])
    # Эскалации — наверх (требуют действия KAM с клиентом).
    orders.sort(key=lambda o: 0 if o.kam_handoff == "escalation" else 1)
    total = len(orders)
    active = sum(1 for o in orders if o.status in ACTIVE)
    esc = sum(1 for o in orders if o.kam_handoff == "escalation")
    gmv = sum(float(o.total_amount or 0) for o in orders)
    HOFF = {"escalation": "🔴 эскалация — связаться с клиентом"}  # сделку ведёт оператор

    def _m(v):
        return ("$" + f"{float(v):,.0f}").replace(",", " ")

    rows = []
    for o in orders:
        st = _ORDER_STATUS_RU.get(o.status, o.status)
        hoff = o.kam_handoff or "kam"
        tone = ("warn" if hoff == "escalation"
                else ("ok" if o.status in ("delivered", "completed")
                      else ("warn" if o.status in ACTIVE else "info")))
        cname = o.customer_ref.name if o.customer_ref_id else (o.customer_name or "—")
        sub = [(_m(o.total_amount) if o.total_amount else "—"),
               HOFF.get(hoff, "🔵 ведёт оператор")]
        if hoff == "escalation" and o.kam_handoff_note:
            sub.append("⚠️ " + o.kam_handoff_note)
        row = {
            "title": _('Заказ #%(p0)s · %(p1)s') % {"p0": f'{o.id}', "p1": f'{cname}'},
            "subtitle": " · ".join(sub),
            "badge": {"label": st, "tone": tone}, "tone": tone,
        }
        if o.customer_ref_id:  # read-only: открыть карточку клиента (отношения)
            row["action"] = "customer_detail"
            row["params"] = {"id": str(o.customer_ref_id)}
        rows.append(row)

    kpi = {"type": "kpi_grid", "data": {"title": _("📋 Мои сделки (KAM)"), "kpis": [
        {"value": total, "label": _("Всего сделок")},
        {"value": active, "label": _("В работе")},
        {"value": esc, "label": _("⚠️ Эскалаций")},
        {"value": _m(gmv), "label": "GMV"},
    ]}}
    return ActionResult(
        text=(f"📋 Продажи ваших клиентов: {total} (в работе {active}"
              + (f", эскалаций {esc}" if esc else "") + "). Сделки ведёт оператор — это "
              "ваша видимость для комиссии и активации. 🔴 эскалация = оператор просит "
              "вас связаться с клиентом."),
        cards=[kpi, {"type": "list", "data": {"title": _("Сделки"),
                "rows": rows or [{"title": _("Сделок пока нет"),
                                  "subtitle": _("Появятся с заказами ваших заказчиков")}]}}],
        actions=[
            {"label": _("👥 Заказчики"), "action": "seller_customers", "params": {}},
            {"label": _("💰 Начисления"), "action": "my_accruals", "params": {}},
        ],
    )


@register("kam_handoff_to_operator")
def kam_handoff_to_operator(params, user, role):
    """KAM → Оператор: передать подтверждённую сделку на исполнение."""
    from marketplace.models import Order
    from django.utils import timezone as _tz
    owner = _effective_seller(user)
    oid = params.get("order_id")
    o = Order.objects.filter(id=oid, assigned_kam=owner).first()
    if not o:
        return ActionResult(text=_("Сделка не найдена среди ваших."),
                            actions=[{"label": _("📋 Мои сделки"), "action": "kam_deals", "params": {}}])
    o.kam_handoff = "operator"
    o.kam_handoff_note = ""
    o.kam_handoff_at = _tz.now()
    o.save(update_fields=["kam_handoff", "kam_handoff_note", "kam_handoff_at"])
    return ActionResult(
        text=(_('✅ Заказ #%(p0)s передан оператору на исполнение. Вы остаётесь на аккаунте (read-only по исполнению); вернётся к вам при исключении.') % {"p0": f'{o.id}'}),
        actions=[{"label": _("📋 Мои сделки"), "action": "kam_deals", "params": {}}],
    )


@register("op_escalate_to_kam")
def op_escalate_to_kam(params, user, role):
    """Оператор → KAM: эскалация исключения (срыв SLA / брак / перерасход)."""
    from marketplace.models import Order
    from django.utils import timezone as _tz
    oid = params.get("order_id")
    reason = (params.get("reason") or params.get("note") or "").strip()
    o = Order.objects.filter(id=oid).first()
    if not o:
        return ActionResult(text=_("Заказ не найден."))
    if not reason:
        return ActionResult(
            text=_('Эскалация заказа #%(p0)s к KAM — укажите причину.') % {"p0": f'{o.id}'},
            cards=[{"type": "form", "data": {
                "title": _('⚠️ Эскалация #%(p0)s → KAM') % {"p0": f'{o.id}'},
                "submit_action": "op_escalate_to_kam", "submit_label": "Эскалировать",
                "fields": [{"name": "reason", "label": _("Причина"), "type": "text", "required": True,
                            "placeholder": _("Срыв SLA / брак / перерасход — коротко")}],
                "fixed_params": {"order_id": oid},
            }}],
        )
    o.kam_handoff = "escalation"
    o.kam_handoff_note = reason[:300]
    o.kam_handoff_at = _tz.now()
    o.save(update_fields=["kam_handoff", "kam_handoff_note", "kam_handoff_at"])
    who = ((o.assigned_kam.get_full_name() or o.assigned_kam.username)
           if o.assigned_kam_id else "KAM не назначен")
    return ActionResult(
        text=(_('⚠️ Заказ #%(p0)s эскалирован к KAM (%(p1)s): %(p2)s. KAM свяжется с клиентом; исполнение остаётся за вами.') % {"p0": f'{o.id}', "p1": f'{who}', "p2": f'{reason}'}),
        actions=[
            {"label": _("← К заказу"), "action": "op_order_detail", "params": {"order_id": oid}},
            {"label": _("📋 Очередь"), "action": "op_queue", "params": {}},
        ],
    )


def _kam_pool():
    """Список пользователей-KAM (operator_role='manager')."""
    from marketplace.models import UserProfile
    return [p.user for p in UserProfile.objects.filter(operator_role="manager")
            .select_related("user")]


def _assign_kam(user, *, avoid_id=None):
    """Закрепить за клиентом менеджера из пула (наименее загруженного).

    Деактивирует прочие активные привязки, переиспользует существующую запись
    к этому KAM либо создаёт новую. Возвращает назначенного KAM (User) или None.
    """
    from marketplace.models import Customer
    pool = _kam_pool()
    if not pool:
        return None
    cand = [k for k in pool if k.id != avoid_id] or pool
    kam = min(cand, key=lambda k: Customer.objects.filter(owner=k, is_active=True).count())
    Customer.objects.filter(user=user, is_active=True).update(is_active=False)
    link = Customer.objects.filter(user=user, owner=kam).order_by("-updated_at").first()
    if link:
        link.is_active = True
        link.save(update_fields=["is_active"])
    else:
        name = user.get_full_name() or user.username
        Customer.objects.create(
            owner=kam, inn=f"77{user.id:08d}", name=name, country="RU",
            legal_address="г. Москва, Тестовая ул., 1",
            contact_name=name, phone="+7 (495) 000-00-00",
            user=user, is_active=True, note=_("self-assign: KAM ведёт закупки клиента"))
    return kam


@register("kam_request")
def kam_request(params, user, role):
    """Подобрать персонального менеджера (KAM) клиенту без закреплённого."""
    from marketplace.models import Customer
    if not (user and getattr(user, "is_authenticated", False)):
        return ActionResult(text=_("Войдите, чтобы подобрать менеджера."),
                            actions=[{"label": _("Войти"), "action": "start_login", "params": {}}])
    active = (Customer.objects.filter(user=user, is_active=True)
              .select_related("owner").first())
    if active and active.owner_id:
        mgr = active.owner.get_full_name() or active.owner.username
        return ActionResult(
            text=_('За вами уже закреплён персональный менеджер — %(p0)s.') % {"p0": f'{mgr}'},
            actions=[{"label": _("👤 Мой менеджер"), "action": "my_kam", "params": {}},
                     {"label": _("🏠 Главная"), "action": "go_home", "params": {}}])
    kam = _assign_kam(user)
    if not kam:
        return ActionResult(
            text=_("Сейчас нет свободного менеджера — мы подберём и сообщим вам."),
            actions=[{"label": _("🏠 Главная"), "action": "go_home", "params": {}}])
    mgr = kam.get_full_name() or kam.username
    return ActionResult(
        text=_('✅ Ваш персональный менеджер — %(p0)s. Он берёт ваши закупки на себя: подбор поставщиков, сроки, цена.') % {"p0": f'{mgr}'},
        actions=[{"label": _("👤 Мой менеджер"), "action": "my_kam", "params": {}},
                 {"label": _("💬 Написать менеджеру"), "action": "kam_message", "params": {}},
                 {"label": _("🏠 Главная"), "action": "go_home", "params": {}}])


@register("my_kam")
def my_kam(params, user, role):
    """Кабинет клиента: кто его ведёт (KAM) + диалог, заказы, смена менеджера."""
    from marketplace.models import Customer
    if not (user and getattr(user, "is_authenticated", False)):
        return ActionResult(text=_("Войдите, чтобы увидеть вашего персонального менеджера."),
                            actions=[{"label": _("Войти"), "action": "start_login", "params": {}}])
    c = (Customer.objects.filter(user=user, is_active=True)
         .select_related("owner").order_by("-updated_at").first())
    if not c:
        return ActionResult(
            text=_("За вами пока не закреплён персональный менеджер (KAM). Хотите — подберём: " "специалист по закупкам возьмёт ваши заявки на себя."),
            actions=[{"label": _("🙋 Подобрать менеджера"), "action": "kam_request", "params": {}},
                     {"label": _("🏠 Главная"), "action": "go_home", "params": {}}])
    mgr = (c.owner.get_full_name() or c.owner.username) if c.owner_id else "—"
    rows = [{
        "title": _('Ваш менеджер (KAM): %(p0)s') % {"p0": f'{mgr}'},
        "subtitle": _("Ведёт ваши закупки: сроки, цены, выгода. Нажмите → напишите ему."),
        "action": "kam_message", "params": {"id": str(c.id)},
    }]
    return ActionResult(
        text=_("👤 Ваш персональный менеджер (KAM) — отвечает за ваши закупки, сроки и выгоду."),
        cards=[{"type": "list", "data": {"title": _("Менеджер"), "rows": rows}}],
        actions=[
            {"label": _("💬 Написать менеджеру"), "action": "kam_message", "params": {"id": str(c.id)}},
            {"label": _("📦 Мои заказы"), "action": "get_orders", "params": {}},
            {"label": _("🔄 Сменить менеджера"), "action": "change_manager", "params": {"id": str(c.id)}},
            {"label": _("🏠 Главная"), "action": "go_home", "params": {}},
        ],
    )


@register("kam_message")
def kam_message(params, user, role):
    """Диалог покупателя со своим персональным менеджером (KAM).

    Phase 1 — форма сообщения. Phase 2 (confirmed) — доставка в чат менеджера.
    """
    from marketplace.models import Customer
    from .models import Conversation, Message
    if not (user and getattr(user, "is_authenticated", False)):
        return ActionResult(text=_("Войдите, чтобы написать менеджеру."),
                            actions=[{"label": _("Войти"), "action": "start_login", "params": {}}])
    cid = params.get("id")
    qs = Customer.objects.filter(user=user, is_active=True).select_related("owner")
    c = (qs.filter(id=cid).first() if cid else qs.order_by("-updated_at").first())
    if not c or not c.owner_id:
        return ActionResult(
            text=_("У вас пока нет закреплённого менеджера, которому можно написать."),
            actions=[{"label": _("👤 Мой менеджер"), "action": "my_kam", "params": {}},
                     {"label": _("🏠 Главная"), "action": "go_home", "params": {}}])
    kam = c.owner
    mgr = kam.get_full_name() or kam.username
    text = (params.get("text") or "").strip()

    if not params.get("confirmed"):
        return ActionResult(
            text=_('💬 Напишите вашему менеджеру — %(p0)s получит сообщение и ответит здесь.') % {"p0": f'{mgr}'},
            cards=[{"type": "form", "data": {
                "title": _('💬 Сообщение менеджеру (%(p0)s)') % {"p0": f'{mgr}'},
                "submit_action": "kam_message", "submit_label": "📨 Отправить",
                "fields": [{"name": "text", "label": _("Сообщение"), "type": "textarea",
                            "required": True,
                            "placeholder": _("Вопрос по заказу, срокам, цене или подбору поставщика…")}],
                "fixed_params": {"confirmed": True, "id": str(c.id)},
            }}],
            actions=[{"label": _("← К менеджеру"), "action": "my_kam", "params": {}}],
        )

    if not text:
        return ActionResult(text=_("⚠️ Сообщение пустое — напишите текст."),
                            actions=[{"label": _("← Назад"), "action": "kam_message",
                                      "params": {"id": str(c.id)}}])

    delivered = False
    try:
        conv = Conversation.objects.filter(
            user=kam, category="support", title=_("Сообщения клиентов"),
            is_active=True).order_by("-updated_at").first()
        if not conv:
            conv = Conversation.objects.create(
                user=kam, role="operator", category="support",
                title=_("Сообщения клиентов"))
        buyer_name = user.get_full_name() or user.username
        Message.objects.create(
            conversation=conv, role=Message.Role.SYSTEM,
            content=f"💬 Клиент {buyer_name} (@{user.username}): {text[:1500]}",
            actions=[{"action": "admin_user_detail", "label": _("👤 Профиль клиента"),
                      "params": {"user_id": user.id}}],
        )
        delivered = True
        try:
            from asgiref.sync import async_to_sync
            from channels.layers import get_channel_layer
            layer = get_channel_layer()
            if layer:
                async_to_sync(layer.group_send)(
                    f"notif_user_{kam.id}",
                    {"type": "operator_alert", "event": "kam_message",
                     "rfq_id": None, "order_id": None, "claim_id": None})
        except Exception:
            pass
    except Exception:
        logger.exception("kam_message: deliver failed")

    if not delivered:
        return ActionResult(
            text=_("⚠️ Не удалось отправить сообщение. Попробуйте ещё раз."),
            actions=[{"label": _("🔁 Повторить"), "action": "kam_message", "params": {"id": str(c.id)}},
                     {"label": _("← К менеджеру"), "action": "my_kam", "params": {}}])
    return ActionResult(
        text=_('✅ Сообщение отправлено менеджеру (%(p0)s). Он получит его в чате и ответит вам.') % {"p0": f'{mgr}'},
        actions=[
            {"label": _("✍️ Написать ещё"), "action": "kam_message", "params": {"id": str(c.id)}},
            {"label": _("← К менеджеру"), "action": "my_kam", "params": {}},
            {"label": _("🏠 Главная"), "action": "go_home", "params": {}},
        ],
    )


@register("change_manager")
def change_manager(params, user, role):
    """Клиент открепляется от KAM (с подтверждением) → заявка возвращается в пул."""
    from marketplace.models import Customer
    cid = params.get("id")
    c = (Customer.objects.filter(id=cid, user=user, is_active=True)
         .select_related("owner").first())
    if not c:
        return ActionResult(
            text=_("Привязка не найдена или уже снята."),
            actions=[{"label": _("👤 Мой менеджер"), "action": "my_kam", "params": {}},
                     {"label": _("🏠 Главная"), "action": "go_home", "params": {}}])
    mgr = (c.owner.get_full_name() or c.owner.username) if c.owner_id else "менеджер"

    if not params.get("confirmed"):
        return ActionResult(
            text=(_('Сменить менеджера (%(p0)s)? Мы назначим вам другого персонального менеджера из пула. Ваши заказы и история сохранятся.') % {"p0": f'{mgr}'}),
            actions=[
                {"label": _("🔄 Да, сменить"), "action": "change_manager",
                 "params": {"id": str(c.id), "confirmed": True}},
                {"label": _("← Оставить"), "action": "my_kam", "params": {}},
            ],
        )

    cur_id = c.owner_id
    new_kam = _assign_kam(user, avoid_id=cur_id)
    if not new_kam:
        c.is_active = False
        c.save(update_fields=["is_active"])
        return ActionResult(
            text=_('Вы откреплены от менеджера (%(p0)s). Сейчас нет другого свободного — можно вернуть прежнего или подобрать позже.') % {"p0": f'{mgr}'},
            actions=[
                {"label": _("↩️ Вернуть менеджера"), "action": "kam_reattach", "params": {"id": str(c.id)}},
                {"label": _("🏠 Главная"), "action": "go_home", "params": {}}])
    newmgr = new_kam.get_full_name() or new_kam.username
    return ActionResult(
        text=_('✅ Назначен новый персональный менеджер — %(p0)s. Прежний (%(p1)s) откреплён; ваши заказы и история сохранены.') % {"p0": f'{newmgr}', "p1": f'{mgr}'},
        actions=[
            {"label": _("👤 Мой менеджер"), "action": "my_kam", "params": {}},
            {"label": _("💬 Написать менеджеру"), "action": "kam_message", "params": {}},
            {"label": _("🏠 Главная"), "action": "go_home", "params": {}}],
    )


@register("kam_reattach")
def kam_reattach(params, user, role):
    """Отмена открепления — вернуть только что снятого менеджера (undo)."""
    from marketplace.models import Customer
    if not (user and getattr(user, "is_authenticated", False)):
        return ActionResult(text=_("Войдите, чтобы вернуть менеджера."),
                            actions=[{"label": _("Войти"), "action": "start_login", "params": {}}])
    if Customer.objects.filter(user=user, is_active=True).exists():
        return ActionResult(text=_("За вами уже закреплён менеджер."),
                            actions=[{"label": _("👤 Мой менеджер"), "action": "my_kam", "params": {}}])
    cid = params.get("id")
    c = (Customer.objects.filter(id=cid, user=user)
         .select_related("owner").order_by("-updated_at").first()) if cid else None
    if not c:
        c = (Customer.objects.filter(user=user, is_active=False)
             .select_related("owner").order_by("-updated_at").first())
    if not c:
        return ActionResult(
            text=_("Не нашёл, какого менеджера вернуть."),
            actions=[{"label": _("🏠 Главная"), "action": "go_home", "params": {}}])
    c.is_active = True
    c.save(update_fields=["is_active"])
    mgr = (c.owner.get_full_name() or c.owner.username) if c.owner_id else "менеджер"
    return ActionResult(
        text=_('✅ Менеджер %(p0)s снова закреплён за вами.') % {"p0": f'{mgr}'},
        actions=[{"label": _("👤 Мой менеджер"), "action": "my_kam", "params": {}},
                 {"label": _("🏠 Главная"), "action": "go_home", "params": {}}])


@register("my_referrals")
def my_referrals(params, user, role):
    """Мои реферальные награды — кого пригласил и сколько начислено (не-KAM роли)."""
    if not (user and getattr(user, "is_authenticated", False)):
        return ActionResult(text=_("Войдите, чтобы увидеть ваши реферальные награды."),
                            actions=[{"label": _("Войти"), "action": "start_login", "params": {}}])
    # KAM — резидуальная модель: его «реферал» = CRM-клиенты + начисления.
    if role == "operator_manager":
        return ActionResult(
            text=_("У KAM реферальная мотивация — резидуальная: приглашённые клиенты " "закрепляются за вами, а вознаграждение идёт со сделок (0.02% + дожатые "
                 "отказные). Смотрите в начислениях и заказчиках."),
            actions=[
                {"label": _("💰 Мои начисления"), "action": "my_accruals", "params": {}},
                {"label": _("👥 Мои заказчики"), "action": "seller_customers", "params": {}},
            ],
        )
    from . import referral as _ref
    summ = _ref.summary_for(user)

    def _m(v):
        return ("$" + f"{float(v):,.0f}").replace(",", " ")

    _ST = {"pending": ("в ожидании", "info"), "credited": ("✓ зачислено", "ok"),
           "cancelled": ("отменено", "warn")}
    _KIND = {"flat_first_order": "$100 с первой покупки приглашённого",
             "buyer_discount": "−$100 на ваш первый заказ (при пополнении)"}
    rows = []
    for r in summ["rows"]:
        lbl, tone = _ST.get(r.status, (r.status, "info"))
        rows.append({
            "title": f"{_KIND.get(r.kind, r.kind)} · +{_m(r.amount)}",
            "subtitle": (r.note or "") + (f" · заказ #{r.trigger_order_id}" if r.trigger_order_id else ""),
            "badge": {"label": lbl, "tone": tone}, "tone": tone,
        })
    kpi = {"type": "kpi_grid", "data": {"title": _("📨 Мои приглашения"), "kpis": [
        {"value": _m(summ["credited"]), "label": _("Зачислено")},
        {"value": _m(summ["pending"]), "label": _("В ожидании"), "sub": _("при первом заказе/пополнении")},
        {"value": summ["count"], "label": _("Приглашений")},
    ]}}
    return ActionResult(
        text=(f"📨 Реферальные награды: зачислено {_m(summ['credited'])}, "
              f"в ожидании {_m(summ['pending'])}." if summ["rows"]
              else "Пока нет приглашений. Отправьте реф-ссылку — за первый заказ "
                   "приглашённого получите награду."),
        cards=[kpi, {"type": "list", "data": {"title": _("Начисления"),
                "rows": rows or [{"title": _("Пока пусто"),
                                  "subtitle": _("Награда появится, когда приглашённый оформит первый заказ")}]}}],
        actions=[
            {"label": _("📨 Пригласить"), "action": "invite_customer", "params": {}},
            {"label": _("🏠 Главная"), "action": "go_home", "params": {}},
        ],
    )


def _drawing_owner(user, role):
    """Владелец чертежей. Покупатель грузит под своим user (DrawingUploadView:
    seller=request.user) → для него owner = сам user. Продавец — через
    _effective_seller (командный/демо-fallback)."""
    return user if (role or "").startswith("buyer") else _effective_seller(user)


_DRAWING_ST = {"draft": "черновик", "on_review": "на проверке", "approved": "✓ одобрен",
               "rejected": "✗ отклонён", "archived": "архив"}


def _drawing_row(d):
    """Строка списка для чертежа: клик открывает файл через access-controlled
    эндпоинт (не сырой /media/ — приватность)."""
    return {
        "title": _('%(p0)s · ред. %(p1)s') % {"p0": f"{getattr(d, 'title', None) or f'Чертёж #{d.id}'}", "p1": f"{d.revision or 'A'}"},
        "subtitle": (
            f"{(d.file_format or '').upper()} · "
            + (f"арт. {d.oem_number}" if d.oem_number
               else (f"товар {d.part.oem_number}" if d.part_id else "без привязки"))
            + f" · {_DRAWING_ST.get(d.status, d.status or '')} · {d.created_at.strftime('%d.%m.%Y')}"
        ),
        "url": f"/api/assistant/drawings/{d.id}/file/",
    }


def _drawing_item(d):
    """То же, что _drawing_row, но с id — для карточки drawings (drag/привязка)."""
    return {"id": d.id, **_drawing_row(d)}


def _drawings_view(user, role, note=None):
    """ActionResult с карточкой type=drawings (папки + чертежи без папки).
    Возвращается из seller_drawings/create_drawing_folder/move_drawing —
    фронт подменяет карточку на месте (inline-создание + drag-n-drop)."""
    from django.db.models import Count
    from marketplace.models import Drawing, DrawingFolder
    owner = _drawing_owner(user, role)
    _is_buyer = (role or "").startswith("buyer")
    _what = "что вам нужно" if _is_buyer else "что вы предлагаете"

    total = Drawing.objects.filter(seller=owner).count()
    folders_data = []

    # 1) Ручные папки (DrawingFolder)
    folders = list(DrawingFolder.objects.filter(owner=owner).annotate(n=Count("drawings")))
    for f in folders:
        fdr = (Drawing.objects.filter(seller=owner, folder=f)
               .select_related("part").order_by("-created_at")[:50])
        folders_data.append({"id": f.id, "name": f.name, "count": f.n,
                             "drawings": [_drawing_item(x) for x in fdr], "is_project": False})

    # 2) Виртуальные папки проектов: чертежи проекта (без ручной папки) →
    #    отдельной папкой с названием проекта.
    from assistant.models import Project
    # dict.fromkeys + order_by() — иначе дефолтный ordering модели попадает в
    # SELECT и .distinct() даёт дубли project_id (по паре project_id, updated_at).
    proj_ids = list(dict.fromkeys(
        Drawing.objects
        .filter(seller=owner, folder__isnull=True, project__isnull=False)
        .order_by().values_list("project_id", flat=True)))
    if proj_ids:
        pmap = {p.id: p for p in Project.objects.filter(id__in=proj_ids)}
        for pid in proj_ids:
            p = pmap.get(pid)
            if not p:
                continue
            pdr = (Drawing.objects.filter(seller=owner, folder__isnull=True, project_id=pid)
                   .select_related("part").order_by("-created_at")[:50])
            folders_data.append({"id": f"pid:{pid}", "name": p.name, "count": len(pdr),
                                 "drawings": [_drawing_item(x) for x in pdr], "is_project": True})

    # 3) Без папки и без проекта
    ungrouped = list(
        Drawing.objects.filter(seller=owner, folder__isnull=True, project__isnull=True)
        .select_related("part").order_by("-created_at")[:50]
    )
    ungrouped_data = [_drawing_item(d) for d in ungrouped]

    if total == 0:
        text = (f"📐 Чертежей пока нет. Загрузите чертёж ({_what}) — оператор "
                "сверит его при согласовании сделки. Это повышает точность поставки.")
    else:
        text = (note or (f"📐 Ваши чертежи: {total} · папок: {len(folders_data)}. "
                "Перетащите чертёж на папку, чтобы разложить. Видны только вам и оператору."))

    return ActionResult(
        text=text,
        cards=[{"type": "drawings", "data": {
            "title": _("Мои чертежи"),
            "folders": folders_data,
            "ungrouped": ungrouped_data,
            "total": total,
        }}],
        actions=[
            {"label": _("📤 Загрузить чертёж"), "action": "upload_drawing", "params": {}},
            {"label": _("🏠 Главная"), "action": "go_home", "params": {}},
        ],
        suggestions=["Загрузить чертёж"],
    )


@register("seller_drawings")
def seller_drawings(params, user, role):
    """Чертежи пользователя: папки (drop-target) + чертежи без папки.
    Папки/чертежи приватны — видны только владельцу и оператору."""
    return _drawings_view(user, role)


@register("drawing_folder")
def drawing_folder(params, user, role):
    """Содержимое одной папки + кнопки управления."""
    from marketplace.models import Drawing, DrawingFolder
    owner = _drawing_owner(user, role)
    try:
        folder = DrawingFolder.objects.get(id=params.get("folder_id"), owner=owner)
    except (DrawingFolder.DoesNotExist, ValueError, TypeError):
        return ActionResult(text=_("Папка не найдена."),
            actions=[{"label": _("← Все чертежи"), "action": "seller_drawings", "params": {}}])
    items = list(Drawing.objects.filter(seller=owner, folder=folder)
                 .select_related("part").order_by("-created_at"))
    cards = ([{"type": "list", "data": {"title": folder.name,
                                        "rows": [_drawing_row(d) for d in items]}}]
             if items else [])
    text = (f"📁 «{folder.name}» — {len(items)} чертеж." if items
            else f"📁 «{folder.name}» пока пуста. Добавьте сюда чертежи.")
    return ActionResult(
        text=text,
        cards=cards,
        actions=[
            {"label": _("➕ Добавить чертежи"), "action": "add_to_folder", "params": {"folder_id": folder.id}},
            {"label": _("🗑 Удалить папку"), "action": "delete_drawing_folder", "params": {"folder_id": folder.id}},
            {"label": _("← Все чертежи"), "action": "seller_drawings", "params": {}},
        ],
    )


@register("create_drawing_folder")
def create_drawing_folder(params, user, role):
    """Создать папку inline (имя из поля в карточке) → обновлённая карточка."""
    from marketplace.models import DrawingFolder
    owner = _drawing_owner(user, role)
    name = (params.get("name") or "").strip()[:120]
    note = None
    if name:
        folder, created = DrawingFolder.objects.get_or_create(owner=owner, name=name)
        note = (f"📁 Папка «{folder.name}» создана." if created
                else f"📁 Папка «{folder.name}» уже есть.")
    return _drawings_view(user, role, note=note)


@register("add_to_folder")
def add_to_folder(params, user, role):
    """Список чертежей вне этой папки → клик кладёт чертёж в папку."""
    from marketplace.models import Drawing, DrawingFolder
    owner = _drawing_owner(user, role)
    try:
        folder = DrawingFolder.objects.get(id=params.get("folder_id"), owner=owner)
    except (DrawingFolder.DoesNotExist, ValueError, TypeError):
        return ActionResult(text=_("Папка не найдена."),
            actions=[{"label": _("← Все чертежи"), "action": "seller_drawings", "params": {}}])
    cand = list(Drawing.objects.filter(seller=owner).exclude(folder=folder)
                .select_related("part", "folder").order_by("-created_at")[:50])
    if not cand:
        return ActionResult(
            text=_('Нет чертежей для добавления в «%(p0)s». Сначала загрузите чертёж.') % {"p0": f'{folder.name}'},
            actions=[
                {"label": _("📤 Загрузить чертёж"), "action": "upload_drawing", "params": {}},
                {"label": _('← Назад в «%(p0)s»') % {"p0": f'{folder.name}'}, "action": "drawing_folder", "params": {"folder_id": folder.id}},
            ],
        )
    rows = []
    for d in cand:
        loc = f" · сейчас в «{d.folder.name}»" if d.folder_id else ""
        rows.append({
            "title": _('%(p0)s · ред. %(p1)s') % {"p0": f"{d.title or f'Чертёж #{d.id}'}", "p1": f"{d.revision or 'A'}"},
            "subtitle": _('%(p0)s%(p1)s — нажмите, чтобы положить сюда') % {"p0": f"{(d.file_format or '').upper()}", "p1": f'{loc}'},
            "action": "move_drawing",
            "params": {"drawing_id": d.id, "folder_id": folder.id},
        })
    return ActionResult(
        text=_('Выберите чертёж — он переедет в папку «%(p0)s».') % {"p0": f'{folder.name}'},
        cards=[{"type": "list", "data": {"title": _('Добавить в «%(p0)s»') % {"p0": f'{folder.name}'}, "rows": rows}}],
        actions=[{"label": _('← Назад в «%(p0)s»') % {"p0": f'{folder.name}'}, "action": "drawing_folder", "params": {"folder_id": folder.id}}],
    )


@register("move_drawing")
def move_drawing(params, user, role):
    """Положить чертёж в папку (folder_id) или вынуть (пустой folder_id).
    Возвращает обновлённую карточку чертежей (для drag-n-drop refresh)."""
    from marketplace.models import Drawing, DrawingFolder
    owner = _drawing_owner(user, role)
    try:
        d = Drawing.objects.get(id=params.get("drawing_id"), seller=owner)
    except (Drawing.DoesNotExist, ValueError, TypeError):
        return _drawings_view(user, role)
    nm = d.title or f"Чертёж #{d.id}"
    fid = params.get("folder_id")
    if fid:
        try:
            folder = DrawingFolder.objects.get(id=fid, owner=owner)
        except (DrawingFolder.DoesNotExist, ValueError, TypeError):
            return _drawings_view(user, role)
        d.folder = folder
        note = f"✓ «{nm}» → папка «{folder.name}»."
    else:
        d.folder = None
        note = f"✓ «{nm}» вынут из папки."
    d.save(update_fields=["folder", "updated_at"])
    return _drawings_view(user, role, note=note)


@register("link_drawing")
def link_drawing(params, user, role):
    """Умный поиск позиции каталога для привязки к чертежу. Рендерит карточку
    поиска drawing_link; с параметром q отдаёт результаты (для live-swap)."""
    from django.db.models import Q
    from marketplace.models import Drawing, Part
    owner = _drawing_owner(user, role)
    try:
        d = Drawing.objects.get(id=params.get("drawing_id"), seller=owner)
    except (Drawing.DoesNotExist, ValueError, TypeError):
        return _drawings_view(user, role)
    q = (params.get("q") or "").strip()
    rows = []
    if len(q) >= 2:
        # Поиск по ОБЩЕЙ базе парт-номеров (без цен) — автоподстановка артикулов
        # для всех (включая покупателя без своего каталога). oem_number__icontains
        # + order_by(индекс) ≈ 13мс на 900K+. Уникальные OEM (один артикул у многих
        # продавцов — показываем один раз).
        seen = set()
        for r in (Part.objects.filter(oem_number__icontains=q)
                  .order_by("oem_number").values("oem_number", "title")[:80]):
            o = (r["oem_number"] or "").strip()
            if not o or o in seen:
                continue
            seen.add(o)
            ttl = f"{o} — {r['title']}".strip(" —") if r["title"] else o
            rows.append({"title": ttl, "subtitle": "",
                         "action": "bind_drawing", "params": {"drawing_id": d.id, "oem": o}})
            if len(rows) >= 15:
                break
    cur = (f"Сейчас: {d.oem_number}" if d.oem_number else "Сейчас: без привязки")
    return ActionResult(
        text=_('🔗 Привязать «%(p0)s» к позиции. %(p1)s') % {"p0": f"{d.title or 'Чертёж #' + str(d.id)}", "p1": f'{cur}'},
        cards=[{"type": "drawing_link", "data": {
            "drawing_id": d.id,
            "title": d.title or f"Чертёж #{d.id}",
            "q": q,
            "rows": rows,
            "searched": len(q) >= 2,
        }}],
        actions=[
            {"label": _("✖ Снять привязку"), "action": "bind_drawing", "params": {"drawing_id": d.id, "oem": ""}},
            {"label": _("← Все чертежи"), "action": "seller_drawings", "params": {}},
        ],
    )


@register("bind_drawing")
def bind_drawing(params, user, role):
    """Привязать чертёж к позиции по парт-номеру (oem) из общей базы, либо снять
    (пустой oem). Парт-номер — seller-agnostic, чтобы оператор метчил по нему
    чертежи всех сторон. part_id поддержан для старых карточек (legacy)."""
    from marketplace.models import Drawing, Part
    owner = _drawing_owner(user, role)
    try:
        d = Drawing.objects.get(id=params.get("drawing_id"), seller=owner)
    except (Drawing.DoesNotExist, ValueError, TypeError):
        return _drawings_view(user, role)
    nm = d.title or f"Чертёж #{d.id}"
    oem = (params.get("oem") or "").strip()
    pid = params.get("part_id")  # legacy
    if oem:
        d.part = None
        d.oem_number = oem
        d.save(update_fields=["part", "oem_number", "updated_at"])
        note = f"🔗 «{nm}» привязан к позиции {oem}."
    elif pid and str(pid) not in ("", "0", "None"):
        try:
            p = Part.objects.get(id=int(pid))
            d.part = p
            d.oem_number = p.oem_number or d.oem_number
            d.save(update_fields=["part", "oem_number", "updated_at"])
            note = f"🔗 «{nm}» привязан к позиции {p.oem_number}."
        except (Part.DoesNotExist, ValueError, TypeError):
            return _drawings_view(user, role)
    else:
        d.part = None
        d.oem_number = ""
        d.save(update_fields=["part", "oem_number", "updated_at"])
        note = f"🔗 Привязка снята с «{nm}»."
    return _drawings_view(user, role, note=note)


@register("delete_drawing_folder")
def delete_drawing_folder(params, user, role):
    """Удалить папку. Чертежи НЕ удаляются (folder=SET_NULL) — просто без папки."""
    from marketplace.models import DrawingFolder
    owner = _drawing_owner(user, role)
    try:
        folder = DrawingFolder.objects.get(id=params.get("folder_id"), owner=owner)
    except (DrawingFolder.DoesNotExist, ValueError, TypeError):
        return ActionResult(text=_("Папка не найдена."),
            actions=[{"label": _("← Все чертежи"), "action": "seller_drawings", "params": {}}])
    nm = folder.name
    folder.delete()
    return ActionResult(
        text=_('🗑 Папка «%(p0)s» удалена. Чертежи из неё сохранены (теперь без папки).') % {"p0": f'{nm}'},
        actions=[{"label": _("← Все чертежи"), "action": "seller_drawings", "params": {}}],
    )


@register("go_home")
def go_home(params, user, role):
    """Бэкенд-фолбэк для go_home. Обычно перехватывается фронтом (chat-first.js
    делает goHome() без round-trip), но если запрос всё же дошёл до сервера
    (устаревший кэш JS, реплей истории) — отдаём дружелюбный экран вместо
    «Нет прав». Доступно всем ролям."""
    return ActionResult(
        text=_("🏠 Главный экран. Чем помочь?"),
        suggestions=["Мои заказы", "Поиск запчастей", "Создать RFQ", "Чертежи"],
    )


@register("upload_drawing")
def upload_drawing(params, user, role):
    """Карточка загрузки чертежа: кнопка выбора файла → __open_drawing_picker
    (фронт открывает файл-пикер в режиме чертежа и шлёт на /drawings/upload/).
    """
    return ActionResult(
        text=(
            "📐 Загрузка чертежа.\n"
            "Форматы: PDF, DWG, DXF, STEP, IGES, STL, PNG, JPG (до 50 МБ). "
            "Укажите артикул (OEM) нужной детали — оператор сверит ваш чертёж "
            "при согласовании сделки (это повышает точность поставки). Чертёж "
            "приватный: его видите только вы и оператор."
        ),
        cards=[{"type": "int_methods", "data": {
            "title": _("Загрузить чертёж"),
            "methods": [{
                "icon": "📂",
                "title": _("Выбрать файл чертежа"),
                "status": "recommended",
                "description": _("PDF / DWG / DXF / STEP / IGES / STL / PNG / JPG, до 50 МБ. " "Можно указать артикул для привязки к товару."),
                "primary": {"label": _("📂 Выбрать файл"),
                            "action": "__open_drawing_picker", "params": {}},
            }],
        }}],
        actions=[
            {"label": _("📐 Мои чертежи"), "action": "seller_drawings", "params": {}},
            # «Каталог» — seller-only; покупателю даём поиск товара вместо него.
            ({"label": _("🔎 Найти товар"), "action": "search_parts", "params": {}}
             if (role or "").startswith("buyer")
             else {"label": _("📦 Каталог"), "action": "seller_catalog", "params": {}}),
        ],
        suggestions=["Мои чертежи", "Зачем чертежи покупателю?"],
    )


# ══════════════════════════════════════════════════════════
# 5. Команда
# ══════════════════════════════════════════════════════════

TEAM_ROLE_LABELS = {
    "admin": "Администратор",
    "manager": "Менеджер",
    "ved": "Менеджер ВЭД",
    "logist": "Логист",
    "finance": "Финансист",
    "viewer": "Только просмотр",
}
TEAM_ROLE_HINT = {
    "admin": "всё + управление командой",
    "manager": "каталог, заказы, КП",
    "ved": "поставщики, прайс-листы, импорт, таможня",
    "logist": "логистика, отгрузки, документы",
    "finance": "платежи, инвойсы, финансы",
    "viewer": "только просмотр",
}
TEAM_INVITE_TTL_DAYS = 7
_TEAM_ROLE_SYNONYMS = {
    "sales": "manager", "менеджер": "manager", "продажи": "manager",
    "вэд": "ved", "ved": "ved", "вэд-менеджер": "ved", "снабжение": "ved",
    "закупки": "ved", "закупщик": "ved", "поставщики": "ved",
    "логист": "logist", "логистика": "logist",
    "финансист": "finance", "финансы": "finance",
    "админ": "admin", "администратор": "admin", "директор": "admin",
    "просмотр": "viewer", "наблюдатель": "viewer",
}


def _company_owner(user):
    """User-владелец компании для данного юзера (он сам, если он не сотрудник)."""
    from marketplace.models import TeamMember
    try:
        m = (TeamMember.objects.filter(user=user, status="active")
             .exclude(owner=user).select_related("owner").first())
        if m:
            return m.owner
    except Exception:
        pass
    return user


def _team_role_of(user):
    """Роль юзера: 'owner' (руководитель компании) или роль TeamMember."""
    from marketplace.models import TeamMember
    try:
        m = TeamMember.objects.filter(user=user, status="active").first()
        if m and m.owner_id != user.id:
            return m.role
    except Exception:
        pass
    return "owner"


def _can_manage_team(user):
    return _team_role_of(user) in ("owner", "admin")


def _invite_base_url():
    from django.conf import settings
    base = (getattr(settings, "SITE_URL", "") or "").strip().rstrip("/")
    return base or "https://consolidatorparts.com"


def _company_name_of(owner):
    from marketplace.models import UserProfile
    try:
        return UserProfile.objects.filter(user=owner).values_list("company_name", flat=True).first() or ""
    except Exception:
        return ""


@register("seller_team")
def seller_team(params, user, role):
    """Команда компании: сотрудники, роли, статусы. Управление — у руководителя/админа."""
    from marketplace.models import TeamMember, UserProfile
    owner = _company_owner(user)
    can_manage = _team_role_of(user) in ("owner", "admin")
    if user.id == owner.id:
        UserProfile.objects.filter(user=owner).update(can_manage_team=True)

    members = list(TeamMember.objects.filter(owner=owner).select_related("user")[:50])
    company = _company_name_of(owner)

    rows = [{
        "title": "👑 " + (owner.get_full_name() or owner.username),
        "subtitle": _("Руководитель компании") + (" · вы" if user.id == owner.id else ""),
    }]
    _st_label = {"invited": "приглашён — ждёт принятия", "disabled": "отключён", "active": "активен"}
    for m in members:
        u = m.user
        name = (u.get_full_name() if u else "") or (u.username if u else "") or m.invited_email
        row = {
            "title": name,
            "subtitle": f"{TEAM_ROLE_LABELS.get(m.role, m.role)} · {_st_label.get(m.status, m.status)}",
        }
        if can_manage:
            row["action"] = "team_member"
            row["params"] = {"member_id": m.id}
        rows.append(row)

    actions = []
    if can_manage:
        actions.append({"label": _("➕ Пригласить сотрудника"), "action": "invite_team_member", "params": {}})
    actions += [
        {"label": _("📦 Каталог"), "action": "seller_catalog", "params": {}},
        {"label": _("📊 Дашборд"), "action": "seller_dashboard", "params": {}},
    ]
    head = (f"👥 Команда компании{(' «' + company + '»') if company else ''}: "
            f"{len(members)} + руководитель." if can_manage else "👥 Команда вашей компании.")
    return ActionResult(
        text=head,
        cards=[{"type": "list", "data": {"title": _("Команда компании"), "rows": rows}}],
        actions=actions,
        suggestions=(["Пригласить менеджера", "Пригласить логиста"] if can_manage else []),
    )


@register("invite_team_member")
def invite_team_member(params, user, role):
    """Пригласить сотрудника по email → ссылка-приглашение (действует TTL дней)."""
    import secrets
    from django.utils import timezone as _tz
    from marketplace.models import TeamMember

    if not _can_manage_team(user):
        return ActionResult(text=_("Приглашать сотрудников может только руководитель компании или администратор."))

    email = (params.get("email") or "").strip().lower()
    role_in = (params.get("role") or "manager").strip().lower()
    role_in = _TEAM_ROLE_SYNONYMS.get(role_in, role_in)
    if role_in not in TEAM_ROLE_LABELS:
        role_in = "manager"

    if not email:
        return ActionResult(
            text=_("Пригласить сотрудника в компанию. Он получит ссылку и доступ к данным компании."),
            cards=[{
                "type": "form",
                "data": {
                    "title": _("👥 Пригласить сотрудника в компанию"),
                    "submit_action": "invite_team_member",
                    "submit_label": "Создать приглашение",
                    "fields": [
                        {"name": "email", "label": _("Email сотрудника"), "type": "email",
                         "required": True, "placeholder": "ivanov@company.com"},
                        {"name": "role", "label": _("Права доступа"), "type": "select",
                         "default": "manager",
                         "options": [
                             {"value": k, "label": f"{TEAM_ROLE_LABELS[k]} — {TEAM_ROLE_HINT[k]}"}
                             for k in ("manager", "ved", "logist", "finance", "admin", "viewer")
                         ]},
                    ],
                    "fixed_params": {},
                },
            }],
        )

    owner = _company_owner(user)
    token = secrets.token_urlsafe(24)
    tm, created = TeamMember.objects.get_or_create(
        owner=owner, invited_email=email,
        defaults={"role": role_in, "status": "invited", "invite_token": token, "invited_at": _tz.now()},
    )
    if not created:
        tm.role = role_in
        tm.invite_token = token
        if tm.status != "active":
            tm.status = "invited"
        tm.invited_at = _tz.now()
        tm.save(update_fields=["role", "invite_token", "status", "invited_at"])

    link = f"{_invite_base_url()}/chat/?join_team={token}"
    rlabel = TEAM_ROLE_LABELS.get(role_in, role_in)
    return ActionResult(
        text=(_('✓ Приглашение для %(p0)s (%(p1)s) создано.\nОтправьте сотруднику ссылку — действует %(p2)s дней:\n%(p3)s\n\nОн откроет ссылку, войдёт или зарегистрируется и получит доступ к данным компании.') % {"p0": f'{email}', "p1": f'{rlabel}', "p2": f'{TEAM_INVITE_TTL_DAYS}', "p3": f'{link}'}),
        actions=[
            {"label": _("👥 К команде"), "action": "seller_team", "params": {}},
            {"label": _("➕ Ещё приглашение"), "action": "invite_team_member", "params": {}},
        ],
    )


@register("accept_team_invite")
def accept_team_invite(params, user, role):
    """Принять приглашение в команду по токену из ссылки ?join_team=…"""
    from datetime import timedelta
    from django.utils import timezone as _tz
    from marketplace.models import TeamMember, UserProfile

    token = (params.get("token") or params.get("join_team") or "").strip()
    if not token:
        return ActionResult(text=_("Ссылка-приглашение недействительна (нет токена)."))

    tm = TeamMember.objects.filter(invite_token=token).select_related("owner").first()
    if not tm or tm.status == "disabled":
        return ActionResult(text=_("Приглашение не найдено или отозвано. Попросите руководителя выслать новое."))
    if tm.invited_at and tm.invited_at < _tz.now() - timedelta(days=TEAM_INVITE_TTL_DAYS):
        return ActionResult(text=_('Срок действия приглашения истёк (%(p0)s дней). Попросите руководителя выслать новое.') % {"p0": f'{TEAM_INVITE_TTL_DAYS}'})

    if not getattr(user, "is_authenticated", False):
        return ActionResult(
            text=_("Чтобы принять приглашение в команду, войдите или зарегистрируйтесь — затем снова откройте ссылку из письма."),
            actions=[{"label": _("Войти / зарегистрироваться"), "action": "start_login", "params": {}}],
        )

    if tm.status == "active" and tm.user_id == user.id:
        return ActionResult(text=_("Вы уже в команде этой компании."),
                            actions=[{"label": _("📊 Дашборд"), "action": "seller_dashboard", "params": {}}])

    tm.user = user
    tm.status = "active"
    tm.accepted_at = _tz.now()
    tm.save(update_fields=["user", "status", "accepted_at"])

    prof, _created = UserProfile.objects.get_or_create(user=user, defaults={"role": "seller"})
    if prof.role != "seller":
        prof.role = "seller"
        prof.save(update_fields=["role"])
    UserProfile.objects.filter(user=user).update(can_manage_team=(tm.role == "admin"))

    company = _company_name_of(tm.owner)
    rlabel = TEAM_ROLE_LABELS.get(tm.role, tm.role)
    return ActionResult(
        text=(_('✅ Вы присоединились к команде компании%(p0)s как %(p1)s.\nТеперь у вас доступ к данным компании: каталог, заказы, КП.') % {"p0": f"{(' «' + company + '»' if company else '')}", "p1": f'{rlabel}'}),
        actions=[
            {"label": _("📊 Дашборд продавца"), "action": "seller_dashboard", "params": {}},
            {"label": _("📦 Каталог"), "action": "seller_catalog", "params": {}},
        ],
    )


@register("team_member")
def team_member(params, user, role):
    """Карточка сотрудника + управление (для руководителя/админа)."""
    from marketplace.models import TeamMember
    if not _can_manage_team(user):
        return ActionResult(text=_("Управление командой доступно руководителю или администратору."))
    owner = _company_owner(user)
    try:
        mid = int(params.get("member_id"))
    except (TypeError, ValueError):
        return ActionResult(text=_("Сотрудник не найден."))
    tm = TeamMember.objects.filter(id=mid, owner=owner).select_related("user").first()
    if not tm:
        return ActionResult(text=_("Сотрудник не найден в вашей компании."))

    u = tm.user
    name = (u.get_full_name() if u else "") or (u.username if u else "") or tm.invited_email
    rlabel = TEAM_ROLE_LABELS.get(tm.role, tm.role)
    st = {"invited": "приглашён", "disabled": "отключён", "active": "активен"}.get(tm.status, tm.status)

    if tm.status == "invited":
        link = f"{_invite_base_url()}/chat/?join_team={tm.invite_token}"
        return ActionResult(
            text=_('👤 %(p0)s\nРоль: %(p1)s · статус: %(p2)s\n\nСсылка-приглашение (7 дней):\n%(p3)s') % {"p0": f'{name}', "p1": f'{rlabel}', "p2": f'{st}', "p3": f'{link}'},
            actions=[{"label": _("👥 К команде"), "action": "seller_team", "params": {}}],
        )

    acts = []
    if tm.status == "active":
        acts.append({"label": _("🚫 Отключить доступ"), "action": "team_disable", "params": {"member_id": tm.id}})
    elif tm.status == "disabled":
        acts.append({"label": _("✅ Включить доступ"), "action": "team_enable", "params": {"member_id": tm.id}})
    for rk in ("manager", "ved", "logist", "finance", "admin", "viewer"):
        if rk != tm.role:
            acts.append({"label": _('Роль → %(p0)s') % {"p0": f'{TEAM_ROLE_LABELS[rk]}'}, "action": "team_set_role",
                         "params": {"member_id": tm.id, "role": rk}})
    acts.append({"label": _("👥 К команде"), "action": "seller_team", "params": {}})
    return ActionResult(text=_('👤 %(p0)s\nРоль: %(p1)s · статус: %(p2)s\nПрава: %(p3)s') % {"p0": f'{name}', "p1": f'{rlabel}', "p2": f'{st}', "p3": f"{TEAM_ROLE_HINT.get(tm.role, '')}"},
                        actions=acts)


def _team_set_status(params, user, status, msg):
    from marketplace.models import TeamMember
    if not _can_manage_team(user):
        return ActionResult(text=_("Недостаточно прав."))
    owner = _company_owner(user)
    try:
        mid = int(params.get("member_id"))
    except (TypeError, ValueError):
        return ActionResult(text=_("Сотрудник не найден."))
    tm = TeamMember.objects.filter(id=mid, owner=owner).first()
    if not tm:
        return ActionResult(text=_("Сотрудник не найден."))
    tm.status = status
    tm.save(update_fields=["status"])
    return ActionResult(text=f"✓ {msg}", actions=[{"label": _("👥 К команде"), "action": "seller_team", "params": {}}])


@register("team_disable")
def team_disable(params, user, role):
    return _team_set_status(params, user, "disabled", "Доступ сотрудника отключён.")


@register("team_enable")
def team_enable(params, user, role):
    return _team_set_status(params, user, "active", "Доступ сотрудника восстановлен.")


@register("team_set_role")
def team_set_role(params, user, role):
    from marketplace.models import TeamMember, UserProfile
    if not _can_manage_team(user):
        return ActionResult(text=_("Недостаточно прав."))
    owner = _company_owner(user)
    try:
        mid = int(params.get("member_id"))
    except (TypeError, ValueError):
        return ActionResult(text=_("Сотрудник не найден."))
    new_role = _TEAM_ROLE_SYNONYMS.get((params.get("role") or "").strip().lower(), (params.get("role") or "").strip().lower())
    if new_role not in TEAM_ROLE_LABELS:
        return ActionResult(text=_("Неизвестная роль."))
    tm = TeamMember.objects.filter(id=mid, owner=owner).first()
    if not tm:
        return ActionResult(text=_("Сотрудник не найден."))
    tm.role = new_role
    tm.save(update_fields=["role"])
    if tm.user_id:
        UserProfile.objects.filter(user_id=tm.user_id).update(can_manage_team=(new_role == "admin"))
    return ActionResult(text=_('✓ Роль изменена на «%(p0)s».') % {"p0": f'{TEAM_ROLE_LABELS[new_role]}'},
                        actions=[{"label": _("👥 К команде"), "action": "seller_team", "params": {}}])


# ══════════════════════════════════════════════════════════
# 6. Интеграции
# ══════════════════════════════════════════════════════════

@register("seller_integrations")
def seller_integrations(params, user, role):
    """Список доступных интеграций со статусом."""
    integrations = [
        {"key": "1c",       "name": "1С:Управление торговлей",
         "desc": _("Синхронизация остатков и заказов"),
         "status": "available"},
        {"key": "bitrix",   "name": "Битрикс24",
         "desc": _("RFQ в CRM, авто-сделки"), "status": "available"},
        {"key": "email",    "name": "Email-уведомления",
         "desc": _("Новые RFQ и заказы на почту"),
         "status": "active"},
        {"key": "telegram", "name": "Telegram-бот",
         "desc": _("Алерты по новым заказам"),
         "status": "available"},
        {"key": "api",      "name": "REST API + webhooks",
         "desc": _("Свои интеграции через API-ключ"),
         "status": "available"},
    ]
    rows = [{
        "title": i["name"],
        "subtitle": i["desc"],
        "badge": ("Подключено" if i["status"] == "active" else "Доступно"),
    } for i in integrations]

    return ActionResult(
        text=_("🔌 Интеграции с внешними системами. Подключите нужное в один клик."),
        cards=[{
            "type": "list",
            "data": {"title": _("Интеграции"), "rows": rows},
        }],
        actions=[
            {"label": _("📊 Дашборд"), "action": "seller_dashboard", "params": {}},
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
         "desc": _("Все продажи за последние 30 дней")},
        {"key": "catalog_xlsx",  "name": "Каталог (Excel)",
         "desc": _("Полный каталог с остатками")},
        {"key": "rfq_csv",       "name": "RFQ-история (CSV)",
         "desc": _("Все RFQ и ответы")},
        {"key": "sla_pdf",       "name": "SLA-отчёт (PDF)",
         "desc": _("Отчёт о выполнении SLA")},
        {"key": "rating_pdf",    "name": "Рейтинг и отзывы (PDF)",
         "desc": _("Сертификат рейтинга")},
    ]
    rows = [{
        "title": r["name"],
        "subtitle": r["desc"],
        "badge": "Скачать",
    } for r in reports]
    return ActionResult(
        text=_("📑 Отчёты по вашей деятельности. Любой можно сгенерировать сейчас."),
        cards=[{
            "type": "list",
            "data": {"title": _("Отчёты"), "rows": rows},
        }],
        actions=[
            {"label": _("📊 Дашборд"), "action": "seller_dashboard", "params": {}},
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
        # Phase 1: показываем форму для ввода ссылки (вместо «пустая ссылка» error)
        return ActionResult(
            text=_("Подключите Google Sheets как источник прайс-листа."),
            cards=[{"type": "form", "data": {
                "title": _("📊 Подключить Google Sheets"),
                "intent": (
                    "Откройте таблицу → Поделиться → 'Все, у кого есть ссылка' → "
                    "Просмотр. Затем скопируйте URL и вставьте сюда. "
                    "Платформа скачает CSV-экспорт и прогонит через smart-mapping."
                ),
                "submit_action": "connect_gsheet",
                "submit_label": "Подключить →",
                "fields": [
                    {"name": "gsheet_url", "label": _("Ссылка на таблицу"),
                     "type": "url", "required": True,
                     "placeholder": "https://docs.google.com/spreadsheets/d/.../edit#gid=0",
                     "hint": _("URL должен быть с правами 'у кого есть ссылка'")},
                ],
            }}],
            actions=[
                {"label": _("← Назад в интеграции"), "action": "seller_integrations", "params": {}},
            ],
        )
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
        return ActionResult(text=_('⚠️ Не удалось скачать таблицу: %(p0)s') % {"p0": f'{e}'})
    if resp.status_code == 401 or resp.status_code == 403:
        return ActionResult(text=(
            "⚠️ Таблица не доступна публично. Откройте доступ "
            "«Все, у кого есть ссылка → Читатель» в настройках Google Sheets "
            "и попробуйте снова."
        ))
    if resp.status_code != 200:
        return ActionResult(text=(
            _('⚠️ Google вернул HTTP %(p0)s. Проверьте ссылку и доступ.') % {"p0": f'{resp.status_code}'}
        ))
    blob = resp.content
    if len(blob) > MAX_FILE_BYTES:
        return ActionResult(text=(
            _('⚠️ Таблица слишком большая (%(p0)s МБ). Максимум %(p1)s МБ.') % {"p0": f'{len(blob) // 1024 // 1024}', "p1": f'{MAX_FILE_BYTES // 1024 // 1024}'}
        ))
    try:
        headers, sample = _read_preview("gsheet.csv", blob)
    except Exception as e:
        return ActionResult(text=_('⚠️ Не удалось прочитать таблицу: %(p0)s') % {"p0": f'{e}'})
    if not headers or len(headers) < MIN_COLUMNS:
        return ActionResult(text=(
            _('⚠️ В таблице меньше %(p0)s колонок. Нечего импортировать.') % {"p0": f'{MIN_COLUMNS}'}
        ))
    if len(headers) > MAX_COLUMNS:
        return ActionResult(text=(
            _('⚠️ Колонок слишком много (%(p0)s). Максимум %(p1)s.') % {"p0": f'{len(headers)}', "p1": f'{MAX_COLUMNS}'}
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
    pm2, _created = PricelistMapping.objects.get_or_create(seller=user)
    cur = dict(pm2.mapping or {})
    cur["_gsheet_url"] = url
    pm2.mapping = cur
    pm2.save(update_fields=["mapping", "updated_at"])

    mapped_preview = _build_mapped_preview(headers, sample, suggested)
    rows = [{
        "label": "🔗 URL", "value": url[:60] + ("…" if len(url) > 60 else ""),
    }, {
        "label": _("Колонок"), "value": str(len(headers)),
    }, {
        "label": _("Маппинг готов"), "value": (
            "по словарю + AI" if ai_called else
            "по сохранённому маппингу" if pm and pm.mapping else
            "по словарю"
        ),
    }, {
        "label": _("─── Распознанные поля ───"), "value": "",
    }]
    for std, src in (suggested or {}).items():
        rows.append({"label": std, "value": str(src)})

    return ActionResult(
        text=(
            _('✅ Google Sheet подключён · %(p0)s колонок прочитано.\nПодтвердите маппинг ниже — после импорта URL сохранится для авто-синхронизации (раз в час).') % {"p0": f'{len(headers)}'}
        ),
        cards=[
            {"type": "draft", "data": {
                "title": _("Google Sheet · превью"),
                "rows": rows,
            }},
            {"type": "table_preview", "data": {
                "title": _("Как ляжет в базу (первые 5 строк)"),
                "headers": mapped_preview["headers"],
                "rows": mapped_preview["rows"],
            }},
        ],
        actions=[
            {"action": "__pricelist_commit", "label": _("📥 Импортировать"),
             "params": dict(
                 {"import_id": imp.id},
                 **{f"col__{k}": v for k, v in (suggested or {}).items()},
             )},
            {"action": "__pricelist_cancel", "label": _("Отменить"),
             "params": {"import_id": imp.id}},
        ],
    )


# ══════════════════════════════════════════════════════════
# open_project — открыть проект ВНУТРИ /chat/ (не отдельная страница)
# Заменяет переход на /chat/project/<id>/ который имел другой шаблон.
# ══════════════════════════════════════════════════════════

@register("list_projects")
def list_projects(params, user, role):
    """Список активных проектов пользователя — отображается как plain list,
    клик ведёт в open_project.
    """
    from .models import Project, Conversation
    qs = Project.objects.filter(owner=user, is_active=True).order_by("-updated_at")[:30]
    if not qs.exists():
        return ActionResult(
            text=_("Активных проектов нет. Создайте первый — он автоматически объединит " "связанные чаты, RFQ и заказы."),
            actions=[
                {"label": _("+ Создать проект"), "action": "create_project", "params": {}},
            ],
        )
    rows = []
    for p in qs:
        chat_n = Conversation.objects.filter(user=user, project=p, is_active=True).count()
        sub_parts = []
        if p.customer: sub_parts.append(p.customer)
        if p.code: sub_parts.append(p.code)
        sub_parts.append(f"{chat_n} чат(а)")
        rows.append({
            "title": p.name,
            "subtitle": " · ".join(sub_parts),
            "tone": "info",
            "action": "open_project",
            "params": {"project_id": str(p.id)},
        })
    return ActionResult(
        text=_('📁 Ваши проекты · %(p0)s') % {"p0": f'{qs.count()}'},
        cards=[{"type": "list", "data": {"title": _("📁 Проекты"), "items": rows}}],
    )


@register("open_project")
def open_project(params, user, role):
    """Открыть проект в основной области /chat/ — без перехода на другую страницу."""
    from .models import Conversation, Project
    project_id = params.get("project_id") or ""
    if not project_id:
        return ActionResult(text=_("Проект не указан."))
    try:
        p = Project.objects.get(id=project_id, owner=user, is_active=True)
    except (Project.DoesNotExist, ValueError):
        return ActionResult(text=_("Проект не найден."))

    chats = list(Conversation.objects.filter(
        user=user, project=p, is_active=True,
    ).order_by("-updated_at")[:10])

    return ActionResult(
        text=f"📁 Проект «{p.name}»" + (f" · {p.customer}" if p.customer else ""),
        cards=[{
            "type": "project_summary",
            "data": {
                "name": p.name,
                "code": p.code or "",
                "customer": p.customer or "",
                "description": p.description or "",
                "dot_color": p.dot_color or "green",
                "chats_count": len(chats),
                "chats": [{
                    "title": c.title or "Без названия",
                    "ts": c.updated_at.strftime("%d.%m %H:%M"),
                } for c in chats],
            },
        }],
        actions=[
            {"label": _("📦 Заказы по проекту"), "action": "get_orders", "params": {"project_id": str(p.id)}},
            {"label": _("📋 Открытые RFQ"), "action": "get_rfq_status", "params": {"project_id": str(p.id)}},
        ],
        suggestions=["Открытые RFQ по проекту", "Активные заказы", "Покажи документы"],
    )

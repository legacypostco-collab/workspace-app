"""Operator-specific chat actions: dashboard, queue, assignment, dispute, notes.

Operator-кабинет в chat-first парадигме: оператор не ведёт свой каталог и не
оплачивает заказы — он надзирает за процессом, перераспределяет задачи между
суб-ролями (logist / customs / payments / manager) и закрывает спорные кейсы.

Все writing-действия идут через DraftCard preview → confirm.
Все события ложатся в OrderEvent с meta={"kind": ...} для аудита и для
подсчётов в `op_dashboard`.
"""
from __future__ import annotations

import logging
from decimal import Decimal

from django.utils import timezone

from .actions import ActionResult, _log_event, _notify, register

logger = logging.getLogger(__name__)


# ── Канонические значения ─────────────────────────────────────
OP_SUBROLES = ("manager", "logist", "customs", "payments")
DISPUTE_RESOLUTIONS = ("refund", "release", "no_action", "partial_refund")

# Open-statuses — те, что считаем «в работе»
OPEN_STATUSES = (
    "pending", "reserve_paid", "confirmed", "in_production",
    "ready_to_ship", "transit_abroad", "customs", "transit_rf", "issuing",
)


def _is_operator(role: str) -> bool:
    return bool(role) and (role == "operator" or role.startswith("operator_"))


def _ensure_operator(role: str):
    if not _is_operator(role):
        return ActionResult(text="Это действие доступно только оператору.")
    return None


# ══════════════════════════════════════════════════════════
# 1. Dashboard — операторская сводка
# ══════════════════════════════════════════════════════════

@register("op_dashboard")
def op_dashboard(params, user, role):
    err = _ensure_operator(role)
    if err:
        return err
    from datetime import timedelta

    from django.utils import timezone

    from marketplace.models import RFQ, Order, OrderClaim

    in_flight = Order.objects.filter(status__in=OPEN_STATUSES).count()
    at_risk = Order.objects.filter(sla_status="at_risk").count()
    breached = Order.objects.filter(sla_status="breached").count()
    refund_pending = Order.objects.filter(payment_status="refund_pending").count()
    awaiting_reserve = Order.objects.filter(payment_status="awaiting_reserve").count()

    # ТЗ KP-flow: SEMI требуют approve в 15 минут, MANUAL — собрать КП за 48ч
    now = timezone.now()
    semi_pending = RFQ.objects.filter(
        mode="semi", status="new", created_at__gte=now - timedelta(hours=24),
    )
    semi_overdue = semi_pending.filter(
        created_at__lt=now - timedelta(minutes=15),
    ).count()
    semi_active = semi_pending.count()

    manual_pending = RFQ.objects.filter(
        mode__in=("manual", "manual_oem"), status="new",
    )
    manual_overdue = manual_pending.filter(
        created_at__lt=now - timedelta(hours=48),
    ).count()
    manual_active = manual_pending.count()

    # Открытые claim'ы / disputes
    open_claims = OrderClaim.objects.filter(
        status__in=("open", "in_review", "corrective_actions", "financial_settlement"),
    ).count()

    # Total $ в обороте — сумма по открытым
    total_in_flight = Order.objects.filter(status__in=OPEN_STATUSES).values_list("total_amount", flat=True)
    total_usd = sum((Decimal(x) for x in total_in_flight), Decimal("0"))

    # Recent priority — топ 5 «горячих»
    hot = (
        Order.objects.filter(sla_status__in=("at_risk", "breached"))
        .order_by("sla_status", "-created_at")[:5]
    )
    rows = [
        {
            "id": o.id,
            "status": o.get_status_display(),
            "sla": o.sla_status,
            "buyer": o.customer_name,
            "total": float(o.total_amount or 0),
            "url": f"/chat/?order={o.id}",
        }
        for o in hot
    ]

    # Требует действия — собираем горячую очередь по приоритету.
    # Subtitle показывает обратный отсчёт SLA (осталось X мин), а не «N мин назад» —
    # оператору не надо считать в голове, и сразу видна срочность.
    from .rfq_mode_badge import sla_countdown_for_operator
    todo_rows: list[dict] = []
    # Без эмодзи в title — режим (SEMI/MANUAL) и тип (ORD) написаны словом.
    # Tone карточки сам подсветит цвет, эмодзи не нужны.
    for rfq in semi_pending.order_by("created_at")[:5]:
        sub, tone = sla_countdown_for_operator("semi", rfq.created_at)
        todo_rows.append({
            "title": f"SEMI RFQ #{rfq.id} · {rfq.customer_name}",
            "subtitle": f"approve КП · {sub}",
            "tone": tone,
            "action": "op_approve_kp",
            "params": {"rfq_id": rfq.id},
        })
    for rfq in manual_pending.order_by("created_at")[:5]:
        sub, tone = sla_countdown_for_operator("manual", rfq.created_at)
        todo_rows.append({
            "title": f"MANUAL RFQ #{rfq.id} · {rfq.customer_name}",
            "subtitle": f"собрать КП · {sub}",
            "tone": tone,
            "action": "op_dispatch_manual_rfq",
            "params": {"rfq_id": rfq.id},
        })
    # Hot orders (SLA at_risk/breached)
    for o in hot:
        todo_rows.append({
            "title": f"ORD-{o.id} · {o.customer_name}",
            "subtitle": f"{o.get_status_display()} · SLA {o.sla_status} · ${float(o.total_amount or 0):,.0f}",
            "action": "get_order_detail",
            "params": {"order_id": o.id},
        })
    if not todo_rows:
        todo_rows = [{"title": "✅ Очередь пуста",
                       "subtitle": "Все SEMI/MANUAL обработаны, SLA в норме"}]

    # Группировки заказов по логистическим стадиям — оператор контролирует
    # каждую (зарубежная логистика, таможня, РФ-логистика).
    in_foreign = Order.objects.filter(status="transit_abroad").count()
    in_customs = Order.objects.filter(status="customs").count()
    in_rf = Order.objects.filter(status__in=("transit_rf", "issuing")).count()
    paid_in_escrow = Order.objects.filter(payment_status__in=("reserve_paid", "mid_paid", "customs_paid")).count()

    # ── Подсветка только важных метрик ──────────────────────
    # Принцип: tone='warn'/'bad' ставим ТОЛЬКО там, где требуется внимание
    # (просрочки, нарушения, активные рекламации). Остальные — нейтральные
    # (без `tone`), чтобы взгляд не разбегался.
    auto_active   = RFQ.objects.filter(mode="auto", status="new").count()

    return ActionResult(
        text=(
            f"Оператор — дирижёр платформы.\n"
            f"В работе: {in_flight} заказов на ${total_usd:,.0f}."
        ),
        cards=[
            # 1. Платежи
            {"type": "kpi_grid", "data": {
                "title": "💰 Платежи / Эскроу",
                "items": [
                    {"label": "Ждут резерв 10%", "value": str(awaiting_reserve),
                     "tone": ("warn" if awaiting_reserve else None),
                     "action": "op_queue", "params": {"filter": "awaiting_reserve"}},
                    {"label": "В эскроу", "value": str(paid_in_escrow),
                     "action": "op_payments_dashboard"},
                    {"label": "Возвраты", "value": str(refund_pending),
                     "tone": ("warn" if refund_pending else None),
                     "action": "op_queue", "params": {"filter": "refund"}},
                ],
            }},
            # 2. Логистика
            {"type": "kpi_grid", "data": {
                "title": "🚚 Логистика",
                "items": [
                    {"label": "Зарубежная", "value": str(in_foreign),
                     "sub": "морем / авто / авиа",
                     "action": "op_queue", "params": {"filter": "transit_abroad"}},
                    {"label": "Таможня", "value": str(in_customs),
                     "sub": "растаможка",
                     "action": "op_customs_dashboard"},
                    {"label": "По РФ + выдача", "value": str(in_rf),
                     "sub": "до получателя",
                     "action": "op_queue", "params": {"filter": "transit_rf"}},
                ],
            }},
            # 3. RFQ
            {"type": "kpi_grid", "data": {
                "title": "📋 RFQ — формирование КП",
                "items": [
                    {"label": "AUTO", "value": str(auto_active),
                     "sub": "без оператора",
                     "action": "op_rfq_queue", "params": {"mode": "auto"}},
                    {"label": "SEMI · approve", "value": str(semi_active),
                     "tone": ("bad" if semi_overdue else ("warn" if semi_active else None)),
                     "sub": (
                         f"{semi_overdue} просрочено · SLA 15 мин"
                         if semi_overdue else
                         f"SLA 15 мин" if semi_active else "пусто"
                     ),
                     "action": "op_rfq_queue", "params": {"mode": "semi"}},
                    {"label": "MANUAL", "value": str(manual_active),
                     "tone": ("bad" if manual_overdue else ("warn" if manual_active else None)),
                     "sub": (
                         f"{manual_overdue} > 48ч · SLA 48 ч"
                         if manual_overdue else
                         f"SLA 48 ч" if manual_active else "пусто"
                     ),
                     "action": "op_rfq_queue", "params": {"mode": "manual"}},
                ],
            }},
            # 4. Только проблемы — SLA + рекламации в одной строке
            {"type": "kpi_grid", "data": {
                "title": "⚠️ Требуют внимания",
                "items": [
                    {"label": "SLA под угрозой", "value": str(at_risk),
                     "tone": ("warn" if at_risk else None),
                     "action": "op_sla_breach", "params": {"filter": "at_risk"}},
                    {"label": "SLA нарушено", "value": str(breached),
                     "tone": ("bad" if breached else None),
                     "action": "op_sla_breach", "params": {"filter": "breached"}},
                    {"label": "Открытые рекламации", "value": str(open_claims),
                     "tone": ("warn" if open_claims else None),
                     "action": "get_claims"},
                ],
            }},
            {"type": "list", "data": {
                "title": "🔥 Требует действия",
                "rows": todo_rows,
            }},
        ],
        contextual_actions=[
            {"action": "op_queue", "label": "Все заказы в работе", "params": {"filter": "all"}},
            {"action": "op_sla_breach", "label": "Нарушения SLA"},
            {"action": "get_claims", "label": "Рекламации"},
            {"action": "get_analytics", "label": "📈 Аналитика"},
        ],
        suggestions=[
            "Покажи очередь нарушений SLA",
            "Какие SEMI просрочены",
            "Открытые claim'ы",
        ],
    )


# ══════════════════════════════════════════════════════════
# 2. Queue — очередь требующих внимания
# ══════════════════════════════════════════════════════════

@register("op_queue")
def op_queue(params, user, role):
    err = _ensure_operator(role)
    if err:
        return err
    from collections import defaultdict

    from marketplace.models import Order

    flt = (params.get("filter") or "all").strip().lower()
    qs = Order.objects.all()
    # PIVOT 2026-05-27: оператор видит только заказы/sub-orders своих
    # поставщиков. Лиды (is_staff=True) — всё. Параметр `all_ops=1` СНИМАЕТ
    # фильтр ТОЛЬКО ДЛЯ STAFF — обычный оператор не может им bypass'нуть PIVOT.
    show_all = user.is_staff  # обычный operator (is_staff=False) ВСЕГДА ограничен своими
    if not show_all:
        # Sub-orders с assigned_operator=user + non-sub orders без split (single-op)
        # где assigned_operator=user
        qs = qs.filter(assigned_operator=user)
    # Скрываем parent-orders (для оператора они избыточны: их sub-orders уже в выдаче)
    qs = qs.filter(sub_orders__isnull=True) if not show_all else qs
    if flt == "breached":
        qs = qs.filter(sla_status="breached")
    elif flt == "at_risk":
        qs = qs.filter(sla_status="at_risk")
    elif flt == "refund":
        qs = qs.filter(payment_status="refund_pending")
    elif flt == "awaiting_reserve":
        qs = qs.filter(payment_status="awaiting_reserve")
    elif flt == "open":
        qs = qs.filter(status__in=OPEN_STATUSES)
    # Логистические фильтры — drill-down с KPI-карточек op_logistics_stats.
    elif flt == "live":
        qs = qs.filter(status__in=("transit_abroad", "customs", "transit_rf", "issuing"))
    elif flt == "customs":
        qs = qs.filter(status="customs")
    elif flt == "transit":
        qs = qs.filter(status__in=("transit_abroad", "transit_rf", "issuing"))
    if flt == "delivered":
        # Доставленные за 90 дней — единственный фильтр, который НЕ исключает
        # completed (это и есть его суть). Показываем как есть.
        from datetime import timedelta

        from django.utils import timezone as _tz
        cutoff = _tz.now() - timedelta(days=90)
        qs = qs.filter(status__in=("delivered", "completed"),
                       created_at__gte=cutoff).order_by("-created_at")[:200]
    else:
        qs = qs.exclude(status__in=("completed", "cancelled"))\
               .order_by("sla_status", "-created_at")[:200]

    # ── Группировка по этапу + что делать оператору на каждом ──
    # (порядок · title · действия оператора)
    STAGE_META = {
        "awaiting_reserve": ("Ждут оплату резерва 10%",
                              "Мониторить — при таймауте отменить заказ или связаться с покупателем"),
        "reserve_paid":     ("Резерв оплачен — ждём подтверждение продавца",
                              "Дождаться подтверждения seller'а. Если затягивает — связаться"),
        "confirmed":        ("Подтверждено — в производстве",
                              "Контроль состава заказа, готовности к отгрузке"),
        "in_production":    ("В производстве",
                              "Если SLA рискует — связаться с продавцом, ускорить"),
        "ready_to_ship":    ("Готовы к отгрузке (ждём 90% или старт)",
                              "Подтвердить выход на отгрузку. Если 90% не оплачены — напомнить покупателю"),
        "transit_abroad":   ("Транзит за рубеж",
                              "Координировать логиста: трек-номер, ETA до РФ"),
        "customs":          ("На таможне РФ",
                              "Брокер: документы, оплатить пошлины, релиз груза"),
        "transit_rf":       ("Транзит по РФ",
                              "Мониторить ETA, договориться с получателем"),
        "issuing":          ("Выдача / приёмка",
                              "Проверить приёмку покупателем, разблокировать эскроу"),
        "delivered":        ("Доставлен — финал",
                              "Закрыть заказ, выплатить продавцу из эскроу"),
    }

    # Группируем
    groups: dict[str, list[Order]] = defaultdict(list)
    for o in qs:
        groups[o.status].append(o)

    PAY_TONE = {
        "awaiting_reserve": ("warn", "Ждёт резерв"),
        "reserve_paid":     ("ok",   "Резерв оплачен"),
        "mid_paid":         ("ok",   "Подтверждение"),
        "customs_paid":     ("ok",   "Таможня оплачена"),
        "paid":             ("info", "Оплачен"),
        "refund_pending":   ("bad",  "Возврат"),
        "refunded":         ("bad",  "Возвращён"),
    }

    cards = []
    total_count = 0

    # ── ФАЗА 0: RFQ ждут подтверждения оператора (SEMI) ──
    # Перед оплатой резерва идёт стадия SEMI: каталог сматчил позиции,
    # оператор должен проверить и подтвердить КП клиенту.
    if flt in ("all", "open"):
        from marketplace.models import RFQ
        from datetime import timedelta as _td
        from django.utils import timezone as _tz
        now = _tz.now()
        pending_rfqs = list(
            RFQ.objects.filter(mode="semi", status__in=("new", "processing", "matched"))
                       .order_by("created_at")[:50]
        )
        if pending_rfqs:
            rfq_items = [{
                "title":    "Этап: Ждут подтверждения оператора (SEMI)",
                "subtitle": "Действие оператора: проверить позиции, утвердить КП клиенту или запросить уточнение (SLA 15 мин)",
            }]
            n_breach = 0
            for r in pending_rfqs[:10]:
                age_min = int((now - r.created_at).total_seconds() // 60) if r.created_at else 0
                left = 15 - age_min
                if left < 0:
                    sla_str = f"SLA нарушен {abs(left)} мин назад"
                    tone = "bad"
                    n_breach += 1
                elif left < 5:
                    sla_str = f"осталось {left} мин"
                    tone = "warn"
                else:
                    sla_str = f"осталось {left} мин из 15"
                    tone = "ok"
                rfq_items.append({
                    "title":    f"RFQ #{r.id} · {r.customer_name or '—'}",
                    "subtitle": f"{r.items.count()} позиций · {sla_str}",
                    "tone":     tone,
                    "badge":    {"label": "Ждёт подтверждения", "tone": "warn"},
                    "action":   "op_approve_kp",
                    "params":   {"rfq_id": r.id},
                })
            n_total_rfq = len(pending_rfqs)
            total_count += n_total_rfq
            meta_bits_rfq = [f"{n_total_rfq} RFQ"]
            if n_breach:
                meta_bits_rfq.append(f"{n_breach} SLA нарушен")
            cards.append({"type": "list", "data": {
                "title": f"Ждут подтверждения оператора · {' · '.join(meta_bits_rfq)}",
                "items": rfq_items
                          + ([{"title": f"… ещё {n_total_rfq - 10} RFQ", "subtitle": "—"}]
                              if n_total_rfq > 10 else []),
            }})

    for status_code, (stage_title, op_action) in STAGE_META.items():
        orders = groups.get(status_code) or []
        if not orders:
            continue
        # SLA-нарушения на верх
        orders.sort(key=lambda o: (
            0 if o.sla_status == "breached" else 1 if o.sla_status == "at_risk" else 2,
            -o.id,
        ))
        items = []
        for o in orders[:10]:
            tone, badge_label = PAY_TONE.get(o.payment_status, (None, None))
            items.append({
                "title": f"#{o.id} · {o.customer_name}",
                "subtitle": f"SLA {o.sla_status} · ${(o.total_amount or 0):,.0f}",
                "tone": tone,
                "badge": ({"label": badge_label, "tone": tone} if badge_label else None),
                "action": "get_order_detail",
                "params": {"order_id": o.id},
            })
        n_total = len(orders)
        n_breached = sum(1 for o in orders if o.sla_status == "breached")
        n_at_risk  = sum(1 for o in orders if o.sla_status == "at_risk")
        total_count += n_total
        meta_bits = [f"{n_total} заказ"]
        if n_breached: meta_bits.append(f"{n_breached} SLA нарушен")
        if n_at_risk:  meta_bits.append(f"{n_at_risk} под угрозой")
        # Header-item стилизованный — это первая строка с подзаголовком
        header = {
            "title":    f"Этап: {stage_title}",
            "subtitle": "Действие оператора: " + op_action,
        }
        cards.append({"type": "list", "data": {
            "title":   f"{stage_title} · {' · '.join(meta_bits)}",
            "items":   [header] + items
                        + ([{"title": f"… ещё {n_total - 10} заказов", "subtitle": "—"}]
                           if n_total > 10 else []),
        }})

    if not cards:
        return ActionResult(
            text=f"Очередь · фильтр «{flt}» · пусто.",
            cards=[{"type": "list", "data": {
                "title": "📋 Очередь",
                "items": [{"title": "Пусто", "subtitle": "Под этот фильтр ничего не попадает"}],
            }}],
        )

    filter_label = {
        "all": "все этапы", "breached": "только SLA нарушено",
        "at_risk": "только под угрозой", "refund": "только возвраты",
        "awaiting_reserve": "только ждут резерв",
    }.get(flt, flt)

    return ActionResult(
        text=(f"📋 Очередь заказов · {filter_label} · {total_count} заказов на "
              f"{len(cards)} этапах. Под каждым — что делать оператору."),
        cards=cards,
        contextual_actions=[
            {"action": "op_queue", "label": "Все этапы",        "params": {"filter": "all"}},
            {"action": "op_queue", "label": "SLA нарушено",     "params": {"filter": "breached"}},
            {"action": "op_queue", "label": "Под угрозой",      "params": {"filter": "at_risk"}},
            {"action": "op_queue", "label": "Возвраты",         "params": {"filter": "refund"}},
            {"action": "op_dashboard", "label": "← Дашборд"},
        ],
    )


# ══════════════════════════════════════════════════════════
# 2b. RFQ-очередь по режиму — auto/semi/manual
# ══════════════════════════════════════════════════════════

@register("op_rfq_queue")
def op_rfq_queue(params, user, role):
    """Очередь RFQ — единый экран с разбивкой по 3 режимам.

    Без параметра mode → показывает все 3 секции (AUTO/SEMI/MANUAL) в одной
    карточке. С mode=auto|semi|manual → фильтрованный полный список одного режима.
    """
    err = _ensure_operator(role)
    if err:
        return err
    from marketplace.models import RFQ

    from .rfq_mode_badge import mode_badge, sla_countdown_for_operator

    mode_raw = (params.get("mode") or "").strip().lower()

    def _build_items(mode_key, limit=10):
        if mode_key == "manual":
            qs = RFQ.objects.filter(status="new", mode__in=("manual", "manual_oem"))
        else:
            qs = RFQ.objects.filter(status="new", mode=mode_key)
        rfqs = list(qs.order_by("created_at")[:limit])
        total = qs.count()
        next_action = {
            "auto":   "rfq_detail",
            "semi":   "op_approve_kp",
            "manual": "op_dispatch_manual_rfq",
        }[mode_key]
        items = []
        for r in rfqs:
            sub, tone = sla_countdown_for_operator(r.mode, r.created_at)
            if not sub:
                sub = f"Создан {r.created_at.strftime('%d.%m %H:%M')}"
            items.append({
                "title":    f"RFQ #{r.id} · {r.customer_name}",
                "subtitle": sub,
                "tone":     tone,
                "action":   next_action,
                "params":   {"rfq_id": r.id},
            })
        return items, total

    # ── Фильтрованный режим (одна секция, полный список) ────
    if mode_raw in ("auto", "semi", "manual", "manual_oem"):
        mk = "manual" if mode_raw.startswith("manual") else mode_raw
        items, total = _build_items(mk, limit=30)
        badge = mode_badge(mk)
        sla_meta = {
            "auto":   "Без оператора · ≈1 сек",
            "semi":   "Оператор подтверждает · SLA 15 мин",
            "manual": "Адресный поиск · SLA 48 ч",
        }[mk]
        hint_meta = {
            "auto":   "Контроль не нужен — авто-подбор уже отдал результат. Открывайте для аудита.",
            "semi":   "approve КП → RFQ уходит покупателю.",
            "manual": "Собрать КП: рассылка по поставщикам, дождаться ответов, выбрать исполнителя.",
        }[mk]
        header = [{"title": f"📍 Режим: {badge}", "subtitle": f"{sla_meta} · {hint_meta}"}]
        cards = [{"type": "list", "data": {
            "title": f"{badge} · {total} RFQ",
            "items": header + (items if items else [
                {"title": "✅ Очередь пуста", "subtitle": "Нет открытых RFQ в этом режиме"},
            ]),
        }}]
        return ActionResult(
            text=f"{badge} очередь · {total} RFQ в работе.",
            cards=cards,
            contextual_actions=[
                {"action": "op_rfq_queue", "label": "Все режимы"},
                {"action": "op_dashboard", "label": "← Дашборд"},
            ],
        )

    # ── Все 3 режима в одном экране ─────────────────────────
    cards = []
    totals = {}
    # PIVOT 2026-05-26: убран MANUAL — только AUTO + SEMI
    for mk, label_prefix, sla_hint in [
        ("semi",   "Оператор подтверждает", "SLA 15 мин"),
        ("auto",   "Без оператора",          "≈1 сек"),
    ]:
        items, total = _build_items(mk, limit=8)
        totals[mk] = total
        badge = mode_badge(mk)
        action_hint = {
            "auto":   "Авто-подбор. Открывайте только для аудита, контроль не нужен.",
            "semi":   "Действие: approve КП → RFQ уходит покупателю.",
            "manual": "Действие: собрать КП — рассылка по поставщикам, выбрать исполнителя.",
        }[mk]
        # Заголовок секции (badge + total + SLA-намёк) — в data.title,
        # подсказка про действие — в data.subtitle. Не дублируем в rows.
        rows = items if items else [{"title": "✅ Очередь пуста", "subtitle": "—"}]
        cards.append({"type": "list", "data": {
            "title": f"{badge} · {total} RFQ",
            "subtitle": f"{label_prefix} · {sla_hint} · {action_hint}",
            "items": rows
                      + ([{"title": f"… ещё {total - 8} RFQ",
                            "subtitle": "Открыть полный список",
                            "action": "op_rfq_queue", "params": {"mode": mk}}]
                          if total > 8 else []),
        }})

    # PIVOT 2026-05-26: Ручная рассылка (MANUAL) удалена как класс.
    # Показываем только Автоподбор и SEMI (подтверждения оператора).
    return ActionResult(
        text=(f"Очередь RFQ · {totals.get('semi',0)+totals.get('auto',0)} в работе.\n"
              f"Нужно подтвердить: {totals.get('semi',0)} (15 мин) · "
              f"Автоподбор: {totals.get('auto',0)}."),
        cards=[c for c in cards if not (c.get('data',{}).get('title','').startswith('Ручная'))],
        contextual_actions=[
            {"action": "op_analytics_hub", "label": "← Аналитика"},
            {"action": "op_rfq_queue", "label": "Нужно подтвердить",  "params": {"mode": "semi"}},
            {"action": "op_rfq_queue", "label": "Автоподбор",         "params": {"mode": "auto"}},
            {"action": "op_dashboard", "label": "← Дашборд"},
        ],
    )


# ══════════════════════════════════════════════════════════
# 3. SLA breaches — список + причины
# ══════════════════════════════════════════════════════════

@register("op_sla_breach")
def op_sla_breach(params, user, role):
    err = _ensure_operator(role)
    if err:
        return err
    from collections import Counter

    from django.utils import timezone as tz

    from marketplace.models import Order

    now = tz.now()
    qs = Order.objects.filter(sla_status__in=("at_risk", "breached")).order_by("sla_status", "ship_deadline")[:30]

    # Кто отвечает за этап + название этапа (без emoji — одно эмодзи на карточку)
    STAGE_OWNER = {
        "pending":        ("Покупатель",          "Оплата резерва"),
        "reserve_paid":   ("Поставщик",           "Подтвердить состав"),
        "confirmed":      ("Поставщик",           "Запустить производство"),
        "in_production":  ("Поставщик / склад",   "Готовность к отгрузке"),
        "ready_to_ship":  ("Поставщик",           "Сдача в FOB-порт"),
        "transit_abroad": ("Зарубежный логист",   "Транзит за рубеж"),
        "customs":        ("Таможенный брокер",   "Растаможка"),
        "transit_rf":     ("РФ-логист",           "Транзит по РФ"),
        "issuing":        ("Пункт выдачи",        "Выдача покупателю"),
        "shipped":        ("Зарубежный логист",   "Транзит"),
        "delivered":      ("Покупатель",          "Приёмка"),
    }

    breached_rows: list[dict] = []
    at_risk_rows:  list[dict] = []
    stuck_at = Counter()
    for o in qs:
        dl = o.ship_deadline
        delta_label = "дедлайн не задан"
        if dl:
            delta_hours = int((now - dl).total_seconds()) // 3600
            if delta_hours > 0:
                if delta_hours >= 24:
                    delta_label = f"+{delta_hours // 24}д {delta_hours % 24}ч просрочки"
                else:
                    delta_label = f"+{delta_hours}ч просрочки"
            else:
                delta_label = f"{-delta_hours}ч до дедлайна"
        owner, stage_label = STAGE_OWNER.get(o.status, ("—", o.get_status_display()))
        stuck_at[o.status] += 1
        is_breached = (o.sla_status == "breached")

        row = {
            "title": f"#{o.id} · {o.customer_name} · ${(o.total_amount or 0):,.0f}",
            "subtitle": (
                f"Этап: {stage_label} · "
                f"Ответственный: {owner} · "
                f"{delta_label}"
            ),
            "tone":   "bad" if is_breached else "warn",
            "action": "op_order_detail",
            "params": {"order_id": o.id},
        }
        (breached_rows if is_breached else at_risk_rows).append(row)

    # ── Аналитика SLA (не дублирует счётчики секций) ──
    from datetime import timedelta

    total_active = Order.objects.exclude(
        status__in=("delivered", "completed", "cancelled"),
    ).count()
    sla_health = 100 - (len(qs) * 100 // total_active) if total_active else 100

    # Средняя просрочка по нарушенным (в часах)
    overdue_hours_list = []
    money_at_risk = 0
    for o in qs:
        if o.sla_status == "breached" and o.ship_deadline:
            overdue_hours_list.append(int((now - o.ship_deadline).total_seconds()) // 3600)
            money_at_risk += float(o.total_amount or 0)
    avg_overdue = sum(overdue_hours_list) / len(overdue_hours_list) if overdue_hours_list else None

    # Топ-узкое место (этап с большинством нарушений)
    if stuck_at:
        top_stage_code, top_stage_n = stuck_at.most_common(1)[0]
        top_stage_label = STAGE_OWNER.get(top_stage_code, (top_stage_code, top_stage_code))[1]
        top_owner = STAGE_OWNER.get(top_stage_code, ("—", "—"))[0]
    else:
        top_stage_code = top_stage_label = top_owner = None

    # Тренд: количество SLA-нарушений за последние 30д vs предыдущие 30д
    from marketplace.models import OrderEvent
    cutoff_30 = now - timedelta(days=30)
    cutoff_60 = now - timedelta(days=60)
    breach_30 = OrderEvent.objects.filter(
        event_type="sla_status_changed", meta__to="breached",
        created_at__gte=cutoff_30,
    ).count()
    breach_prev = OrderEvent.objects.filter(
        event_type="sla_status_changed", meta__to="breached",
        created_at__gte=cutoff_60, created_at__lt=cutoff_30,
    ).count()
    if breach_prev:
        breach_trend = int((breach_30 - breach_prev) * 100 / breach_prev)
    elif breach_30:
        breach_trend = 100
    else:
        breach_trend = 0
    arrow = "↑" if breach_trend > 0 else ("↓" if breach_trend < 0 else "→")

    # Распределение активных заказов по 3 SLA-статусам — головные плитки.
    _active_base = Order.objects.exclude(
        status__in=("delivered", "completed", "cancelled"))
    _n_breached = _active_base.filter(sla_status="breached").count()
    _n_at_risk = _active_base.filter(sla_status="at_risk").count()
    _n_on_time = max(0, total_active - _n_breached - _n_at_risk)

    # Средняя просрочка — форматируем (часы/дни), «—» если нарушений нет.
    if avg_overdue is None:
        _avg_val = "—"
    elif avg_overdue < 48:
        _avg_val = f"{avg_overdue:.0f} ч"
    else:
        _avg_val = f"{avg_overdue / 24:.1f} дн"

    # Этап с наибольшей задержкой: короткое слово в значение (чтобы плитка не
    # переносилась на 2 строки), число задержек — в подпись.
    _SHORT_STAGE = {
        "pending": "Оплата", "reserve_paid": "Подтверждение",
        "confirmed": "Производство", "in_production": "Производство",
        "ready_to_ship": "Отгрузка", "transit_abroad": "Транзит",
        "customs": "Таможня", "transit_rf": "Транзит РФ",
        "issuing": "Выдача", "shipped": "Транзит", "delivered": "Приёмка",
    }
    if top_stage_label:
        _stage_val = _SHORT_STAGE.get(top_stage_code, top_stage_label)
        _stage_sub = f"задержек: {top_stage_n}"
    else:
        _stage_val = "—"
        _stage_sub = None

    # 6 плиток — сетка 2×3.
    # Плитки кликабельны → очередь заказов с фильтром. Делаем кликабельной
    # ТОЛЬКО когда есть что показать (иначе клик ведёт в пустоту = «не работает»).
    def _q(flt):
        return {"action": "op_queue", "params": {"filter": flt}}

    kpi_items = [
        {"label": "В срок", "value": str(_n_on_time),
         "sub": f"{sla_health}% заказов", "tone": "ok",
         **(_q("open") if _n_on_time else {})},
        {"label": "Под угрозой", "value": str(_n_at_risk),
         "tone": "warn" if _n_at_risk else "ok",
         **(_q("at_risk") if _n_at_risk else {})},
        {"label": "В просрочке", "value": str(_n_breached),
         "tone": "bad" if _n_breached else "ok",
         **(_q("breached") if _n_breached else {})},
        {"label": "Средняя просрочка", "value": _avg_val,
         "tone": "bad" if (avg_overdue and avg_overdue > 48) else ("warn" if avg_overdue else "ok"),
         **(_q("breached") if _n_breached else {})},
        {"label": "Деньги под риском", "value": f"${money_at_risk:,.0f}",
         "tone": "warn" if money_at_risk else "ok",
         **(_q("breached") if money_at_risk else {})},
        {"label": "Где больше всего задержек",
         "value": _stage_val, "sub": _stage_sub,
         "tone": "bad" if top_stage_label else "ok",
         **(_q("breached") if top_stage_label else {})},
    ]

    # Actionable инсайт
    if len(breached_rows) and top_stage_label:
        intro = (f"Самый медленный этап: {top_stage_label} ({top_stage_n} нарушений). "
                 f"Начните с эскалации {top_owner}.")
    elif sla_health < 70:
        intro = f"В срок укладываются всего {sla_health}% заказов — критическая зона. Нужны срочные действия."
    elif breach_trend > 20:
        intro = (f"За 30 дней рост SLA-нарушений на {breach_trend}% — "
                 f"проверьте, что изменилось в процессе.")
    elif len(qs) == 0:
        intro = "Все заказы укладываются в SLA."
    else:
        intro = f"Под контролем {len(qs)} заказов, узких мест нет."

    cards = []
    if kpi_items:
        cards.append({"type": "kpi_grid", "data": {
            "title": "📊 Аналитика SLA",
            "items": kpi_items,
            # Монохромная линия-прогресс (% заказов в срок). Цвета — из CSS (ЧБ).
            "progress": {"value": sla_health, "center": f"{sla_health}%",
                         "label": "заказов в срок"},
        }})

    if breached_rows:
        cards.append({"type": "list", "data": {
            "title": f"Нарушено · {len(breached_rows)}",
            "items": breached_rows,
        }})
    if at_risk_rows:
        cards.append({"type": "list", "data": {
            "title": f"Под угрозой · {len(at_risk_rows)}",
            "items": at_risk_rows,
        }})
    if not cards:
        cards.append({"type": "list", "data": {
            "title": "SLA · всё в норме",
            "items": [{"title": "✅ Нет нарушений", "subtitle": "Все заказы укладываются в дедлайны"}],
        }})

    contextual = [
        {"action": "op_analytics_hub", "label": "← Аналитика"},
        {"action": "op_queue", "label": "Очередь заказов", "params": {"filter": "all"}},
        {"action": "op_logistics_stats", "label": "Сводка логистики", "params": {}},
        {"action": "op_payments_dashboard", "label": "Платежи", "params": {}},
        {"action": "get_claims", "label": "Рекламации", "params": {}},
    ]

    return ActionResult(
        text=intro,
        cards=cards,
        contextual_actions=contextual,
        suggestions=["Кто отвечает за #1?", "Эскалировать #1", "Сводка логистики"],
    )


# ══════════════════════════════════════════════════════════
# 4. Order detail (operator-view) — расширенный
# ══════════════════════════════════════════════════════════

def _latest_assignment(order):
    """Последний `op_assigned` event на этом заказе."""
    from marketplace.models import OrderEvent
    e = OrderEvent.objects.filter(
        order=order, event_type="operator_action", meta__kind="assigned"
    ).order_by("-created_at").first()
    if not e:
        return None
    return {
        "to_role": e.meta.get("to_role"),
        "to_user_id": e.meta.get("to_user_id"),
        "by": e.actor.username if e.actor else "—",
        "at": e.created_at.isoformat(),
    }


@register("op_order_detail")
def op_order_detail(params, user, role):
    err = _ensure_operator(role)
    if err:
        return err
    from marketplace.models import Order, OrderEvent
    try:
        order = Order.objects.get(id=int(params.get("order_id") or 0))
    except (Order.DoesNotExist, ValueError, TypeError):
        return ActionResult(text="Заказ не найден.")

    events = OrderEvent.objects.filter(order=order).order_by("-created_at")[:15]

    # Человекочитаемые названия событий + расшифровка статусов
    STATUS_RU = {
        "pending":        "Ожидание оплаты",
        "reserve_paid":   "Резерв оплачен",
        "confirmed":      "Подтверждён",
        "in_production":  "В производстве",
        "ready_to_ship":  "Готов к отгрузке",
        "transit_abroad": "Транзит за рубеж",
        "customs":        "На таможне",
        "transit_rf":     "Транзит по РФ",
        "issuing":        "На выдаче",
        "shipped":        "Отгружен",
        "delivered":      "Доставлен",
        "completed":      "Завершён",
        "cancelled":      "Отменён",
    }
    SLA_RU = {
        "on_track": "✓ в норме", "at_risk": "🟠 под угрозой", "breached": "🔴 нарушен",
    }
    # Без префикс-emoji в названиях — иконка уже идёт отдельной колонкой
    # слева (EVENT_ICON), чтобы не было «двойных стрелочек».
    EVENT_RU = {
        "order_created":               "Заказ создан",
        "order_cancelled_by_buyer":    "Покупатель отменил заказ",
        "order_cancelled_by_seller":   "Продавец отменил заказ",
        "reserve_paid":                "Резерв 10% оплачен",
        "final_payment_paid":          "Финальная оплата 90%",
        "status_changed":              "Статус заказа",
        "sla_status_changed":          "SLA",
        "trigger_completed":           "Триггер выполнен",
        "payment_deadline_set":        "Дедлайн оплаты установлен",
        "operator_action":             "Действие оператора",
        "shipment_created":            "Отгрузка создана",
        "claim_opened":                "Открыта рекламация",
        "claim_resolved":              "Рекламация закрыта",
        "invoice_opened":              "Счёт открыт покупателю",
        "invoice_paid":                "Счёт оплачен",
        "tracking_updated":            "Обновление трекинга",
        "document_uploaded":           "Документ загружен",
    }
    SOURCE_RU = {
        "buyer": "покупатель", "seller": "продавец", "operator": "оператор",
        "system": "система", "logist": "логист",
    }

    audit_rows = []
    for e in events:
        evt_label = EVENT_RU.get(e.event_type, e.event_type)
        # Из meta достаём детали — например from/to для status_changed
        m = e.meta or {}
        detail = ""
        if e.event_type == "status_changed":
            fr = STATUS_RU.get(m.get("from", ""), m.get("from", ""))
            to = STATUS_RU.get(m.get("to", ""), m.get("to", ""))
            if fr and to:
                detail = f": {fr} → {to}"
        elif e.event_type == "sla_status_changed":
            fr = SLA_RU.get(m.get("from", ""), m.get("from", ""))
            to = SLA_RU.get(m.get("to", ""), m.get("to", ""))
            if fr and to:
                detail = f": {fr} → {to}"
        elif e.event_type == "trigger_completed":
            tid = m.get("trigger_id") or ""
            if tid:
                detail = f": {tid}"
        elif e.event_type == "reserve_paid" or e.event_type == "final_payment_paid":
            amt = m.get("amount")
            if amt:
                detail = f": ${float(amt):,.0f}"
        actor = e.actor.username if e.actor else "система"
        source = SOURCE_RU.get(e.source, e.source or "—")
        audit_rows.append({
            "title": f"{evt_label}{detail}",
            "subtitle": f"{source} · {actor} · {e.created_at:%d.%m.%Y %H:%M}",
        })

    # Превращаем в timeline-формат (для отдельного renderer'а audit_timeline):
    # цвет/иконка по типу события, color-code по актёру (buyer/seller/operator/system).
    EVENT_ICON = {
        "status_changed": "▶", "sla_status_changed": "⏱", "trigger_completed": "✅",
        "reserve_paid": "💰", "final_payment_paid": "💰",
        "invoice_opened": "📄", "invoice_paid": "💰",
        "tracking_updated": "📍", "document_uploaded": "📎",
        "order_created": "🆕", "order_cancelled_by_buyer": "🗑", "order_cancelled_by_seller": "🗑",
        "payment_deadline_set": "📩", "operator_action": "🛡",
        "shipment_created": "🚚", "claim_opened": "🧾", "claim_resolved": "✅",
    }
    EVENT_TONE = {
        "status_changed": "blue", "sla_status_changed": "red", "trigger_completed": "green",
        "reserve_paid": "green", "final_payment_paid": "green",
        "order_created": "blue", "order_cancelled_by_buyer": "gray",
        "order_cancelled_by_seller": "gray", "payment_deadline_set": "orange",
        "claim_opened": "red", "claim_resolved": "green",
    }
    ACTOR_COLOR = {
        "buyer": "blue", "seller": "purple", "operator": "orange",
        "system": "gray", "logist": "teal",
    }

    timeline_items = []
    for e in events:
        m = e.meta or {}
        evt_title = EVENT_RU.get(e.event_type, e.event_type)
        before = after = None
        delta_text = ""
        if e.event_type == "status_changed":
            before = STATUS_RU.get(m.get("from", ""), m.get("from", ""))
            after = STATUS_RU.get(m.get("to", ""), m.get("to", ""))
        elif e.event_type == "sla_status_changed":
            before = SLA_RU.get(m.get("from", ""), m.get("from", ""))
            after = SLA_RU.get(m.get("to", ""), m.get("to", ""))
        elif e.event_type == "trigger_completed":
            delta_text = m.get("trigger_id") or ""
        elif e.event_type in ("reserve_paid", "final_payment_paid"):
            if m.get("amount"):
                delta_text = f"${float(m['amount']):,.0f}"
        timeline_items.append({
            "icon": EVENT_ICON.get(e.event_type, "•"),
            "tone": EVENT_TONE.get(e.event_type, "gray"),
            "title": evt_title,
            "before": before,
            "after": after,
            "delta": delta_text,
            "actor": e.actor.username if e.actor else "система",
            "actor_role": e.source or "system",
            "actor_color": ACTOR_COLOR.get(e.source, "gray"),
            "actor_role_label": SOURCE_RU.get(e.source, e.source or "—"),
            "when": e.created_at.isoformat(),
            "when_short": f"{e.created_at:%d.%m %H:%M}",
        })

    # Payment milestones (по финансовой модели платформы).
    # Базис покупки определяет процент сервисного сбора: FOB 10% / CIP 14% / DDP 18%.
    from decimal import Decimal as _D
    total = _D(str(order.total_amount or 0))
    inc = (order.incoterm or "FOB").upper()
    # Базовая ставка сервисного сбора
    service_pct = {"FOB": _D("0.10"), "CIP": _D("0.14"), "DDP": _D("0.18")}.get(inc, _D("0.10"))
    # FOB cost = условный «товар» (вытащим из items если есть, иначе total / (1+ставка))
    fob_value = (total / (_D("1") + service_pct)).quantize(_D("0.01"))
    service_fee = (fob_value * service_pct).quantize(_D("0.01"))
    deposit_10 = (fob_value * _D("0.10")).quantize(_D("0.01"))
    deposit_return_5 = (fob_value * _D("0.05")).quantize(_D("0.01"))
    platform_keep_5 = deposit_return_5
    seller_at_shipment = (fob_value - deposit_10).quantize(_D("0.01"))

    ps = order.payment_status
    st = order.status
    # 4 финансовых этапа по новой модели
    client_paid = ps in ("reserve_paid", "mid_paid", "customs_paid", "paid")
    shipped = st in ("transit_abroad", "customs", "transit_rf", "issuing",
                      "delivered", "completed")
    delivered = st in ("delivered", "completed")
    # Возврат депозита через 14 дней — упрощённо, считаем по статусу completed
    deposit_settled = st == "completed"

    def _mark(done, current):
        if done: return "✅"
        if current: return "⏳"
        return "○"
    milestones = [
        {"label": f"Клиент оплачивает · ${total:,.0f}",
         "value": _mark(client_paid, st == "pending"),
         "sub": f"FOB ${fob_value:,.0f} + сервис {int(service_pct*100)}% (${service_fee:,.0f})"},
        {"label": f"Выплата поставщику · ${seller_at_shipment:,.0f}",
         "value": _mark(shipped, client_paid and not shipped),
         "sub": f"FOB-цена минус депозит 10% (${deposit_10:,.0f})"},
        {"label": f"Депозит у платформы · ${deposit_10:,.0f}",
         "value": _mark(shipped, False),
         "sub": "Удерживается до приёмки + 14 дней"},
        {"label": "Через 14 дней после приёмки",
         "value": _mark(deposit_settled, delivered and not deposit_settled),
         "sub": f"+5% поставщику (${deposit_return_5:,.0f}), 5% платформе (SLA-сбор)"},
    ]

    # ── Состав заказа (OrderItem'ы) — что именно везём ──
    from marketplace.models import OrderItem
    items_qs = OrderItem.objects.filter(order=order).select_related("part", "part__brand")
    composition_rows = []
    for it in items_qs:
        p = it.part
        if not p:
            composition_rows.append({
                "title": f"× {it.quantity}",
                "subtitle": f"${float(it.unit_price):,.2f} за шт · ${float(it.total_price):,.2f}",
            })
            continue
        brand = (p.brand.name if p.brand_id else "")
        composition_rows.append({
            "title": f"{p.oem_number} · {p.title[:60]}",
            "subtitle": (f"{brand + ' · ' if brand else ''}"
                         f"кол-во {it.quantity} × ${float(it.unit_price):,.2f} = "
                         f"${float(it.total_price):,.2f}"),
            "badge": {"label": (p.condition or "OEM").upper(),
                      "tone": "ok" if (p.condition or "oem") == "oem" else "info"},
        })
    # Маршрут (если уже в логистике)
    meta = order.logistics_meta if isinstance(order.logistics_meta, dict) else {}
    origin = meta.get("origin") or meta.get("origin_country") or "—"
    customs_meta = meta.get("customs") or {}
    cm_country = customs_meta.get("country") or "RU"
    tracking = meta.get("tracking_number") or "—"
    carrier = order.logistics_provider or "—"
    mode = {"sea": "🚢 Море", "air": "✈️ Авиа", "auto": "🚚 Авто", "rail": "🚂 ЖД"} \
        .get(order.shipping_mode, order.shipping_mode or "—")
    route_rows = [
        {"title": f"Маршрут: {origin} → {cm_country}",
         "subtitle": f"{mode} · {carrier}"},
        {"title": f"Трекинг: {tracking}",
         "subtitle": f"Адрес доставки: {(order.delivery_address or '—')[:80]}"},
    ]

    # ── Документы по заказу (✅ есть / ⬜ нет) ──
    # Сводим воедино: то что загружено как OrderDocument + customs-данные
    # (ТН ВЭД, сертификаты, декларация) + триггеры этапа (ТТН, packing list).
    from marketplace.models import OrderDocument

    from .customs_data import required_certs_for as _req_certs

    # 1. Загруженные документы — OrderDocument с группировкой по типу
    uploaded = {dt: [] for dt, _ in OrderDocument.DOC_TYPE_CHOICES}
    for d in OrderDocument.objects.filter(order=order):
        uploaded.setdefault(d.doc_type, []).append(d)

    # 2. Триггеры этапов (декларация, ТТН) — из logistics_meta.triggers
    triggers_meta = meta.get("triggers") or {}
    declaration_done = bool((triggers_meta.get("customs") or {}).get("declaration"))
    ttn_done = bool((triggers_meta.get("transit_rf") or {}).get("ttn_rf"))

    hs_code = customs_meta.get("hs_code") or ""
    required_certs_list = _req_certs(hs_code) if hs_code else []
    have_certs_codes = set(customs_meta.get("certs") or [])

    # Универсальный builder строки документа: title, есть/нет, краткая инфо
    def _doc_row(title: str, present: bool, extra: str = "", required: bool = True):
        if present:
            return {
                "title": f"✅ {title}",
                "subtitle": extra or "Загружен / подтверждён",
                "tone": "ok",
                "badge": {"label": "ЕСТЬ", "tone": "ok"},
            }
        if not required:
            return {
                "title": f"⬜ {title}",
                "subtitle": extra or "Не обязателен",
                "tone": "info",
                "badge": {"label": "—", "tone": "info"},
            }
        return {
            "title": f"⬜ {title}",
            "subtitle": extra or "Не загружен — требуется",
            "tone": "warn",
            "badge": {"label": "НЕТ", "tone": "warn"},
        }

    docs_rows = [
        _doc_row(
            "Коммерческий инвойс",
            bool(uploaded.get("invoice")),
            f"{len(uploaded.get('invoice', []))} файл(ов)" if uploaded.get("invoice") else "",
        ),
        _doc_row(
            "Упаковочный лист (Packing List)",
            bool(uploaded.get("packing_list")),
            f"{len(uploaded.get('packing_list', []))} файл(ов)" if uploaded.get("packing_list") else "",
        ),
        _doc_row(
            "ТН ВЭД код",
            bool(hs_code),
            f"{hs_code} (страна {cm_country})" if hs_code else "Не присвоен — нужен для растаможки",
        ),
        _doc_row(
            "Таможенная декларация",
            declaration_done,
            "Триггер закрыт оператором" if declaration_done
            else "Ожидается от брокера",
            required=(order.status in ("customs", "transit_rf", "issuing", "delivered", "completed")),
        ),
    ]
    # Каждый требуемый сертификат отдельной строкой
    if required_certs_list:
        for cert_code in required_certs_list:
            docs_rows.append(_doc_row(
                f"Сертификат {cert_code}",
                cert_code in have_certs_codes,
                f"По ТН ВЭД {hs_code}",
                required=True,
            ))
    else:
        docs_rows.append(_doc_row(
            "Сертификаты соответствия",
            bool(uploaded.get("certificate")),
            "Перечень определится после присвоения ТН ВЭД",
            required=False,
        ))
    docs_rows.extend([
        _doc_row(
            "ТТН (товарно-транспортная)",
            ttn_done,
            "Загружена при передаче РФ-логисту" if ttn_done
            else "Нужна при транзите по РФ",
            required=(order.status in ("transit_rf", "issuing", "delivered", "completed")),
        ),
        _doc_row(
            "Отчёт о качестве / QC",
            bool(uploaded.get("quality_report")),
            "Опционально, но желателен для рекламаций",
            required=False,
        ),
    ])

    docs_present = sum(1 for r in docs_rows if r["badge"]["label"] == "ЕСТЬ")
    docs_missing = sum(1 for r in docs_rows if r["badge"]["label"] == "НЕТ")

    return ActionResult(
        text=(
            f"Заказ #{order.id} · {order.customer_name}\n"
            f"{order.get_status_display()} · {order.get_payment_status_display()} · SLA {SLA_RU.get(order.sla_status, order.sla_status)}\n"
            f"${(order.total_amount or 0):,.0f} · {len(composition_rows)} поз."
        ),
        cards=[
            {"type": "kpi_grid", "data": {
                "title": "💰 Поток платежей по заказу",
                "items": milestones,
            }},
            # ── Состав заказа — что внутри ──
            {"type": "list", "data": {
                "title": f"📦 Состав заказа · {len(composition_rows)} поз. · ${(order.total_amount or 0):,.0f}",
                "items": composition_rows or [{"title": "Нет позиций"}],
            }},
            # ── Маршрут и логистика ──
            {"type": "list", "data": {
                "title": "🚚 Маршрут и логистика",
                "items": route_rows,
            }},
            # ── Документы по заказу: что есть / чего нет ──
            {"type": "list", "data": {
                "title": (f"📑 Документы · ✅ {docs_present} есть · "
                          f"⬜ {docs_missing} не хватает"),
                "items": docs_rows,
            }},
            {"type": "audit_timeline", "data": {
                "title": f"📜 История заказа #{order.id}",
                "events": timeline_items,
            }},
        ],
        contextual_actions=[
            {"action": "op_add_note", "label": "📝 Заметка", "params": {"order_id": order.id}},
            # Назначение/смена перевозчика — приоритетная кнопка если заказ в transit
            {"action": "op_assign_carrier",
             "label": ("🚚 " + ("Сменить" if order.carrier_name else "Назначить")
                        + " перевозчика"),
             "params": {"order_id": order.id}},
            {"action": "track_order", "label": "📦 Трекинг", "params": {"order_id": order.id}},
            {"action": "op_resolve_dispute", "label": "⚖️ Закрыть спор", "params": {"order_id": order.id}},
            {"action": "get_claims", "label": "🧾 Рекламации", "params": {}},
            {"action": "go_home", "label": "🏠 Главная"},
        ],
    )


# ══════════════════════════════════════════════════════════
# 5. Assign — назначить оператора (writing → DraftCard)
# ══════════════════════════════════════════════════════════

@register("op_assign")
def op_assign(params, user, role):
    err = _ensure_operator(role)
    if err:
        return err
    from marketplace.models import Order

    try:
        order = Order.objects.get(id=int(params.get("order_id") or 0))
    except (Order.DoesNotExist, ValueError, TypeError):
        return ActionResult(text="Заказ не найден.")

    to_role = (params.get("to_role") or "").strip().lower()
    confirmed = bool(params.get("confirmed"))

    # Шаг 1: предпросмотр (DraftCard с формой)
    if not confirmed or to_role not in OP_SUBROLES:
        current = _latest_assignment(order)
        current_line = (
            f"📌 Сейчас назначен: **{current['to_role']}** (by {current['by']}, "
            f"{current['at'][:10]})"
            if current else "📌 Сейчас никто не отвечает — заказ в общей очереди"
        )
        return ActionResult(
            text=(
                f"**Кому назначить заказ #{order.id}?**\n\n"
                f"Назначение передаёт ответственность за этот заказ конкретному "
                f"специалисту-оператору. Он получит уведомление, увидит заказ "
                f"в своей персональной очереди и будет следить за SLA. Дирижирует "
                f"процессом дальше — он, но за итог отвечает старший оператор.\n\n"
                f"{current_line}\n\n"
                f"**Когда назначать:**\n"
                f"• 🚚 **Логист** — груз застрял в транзите / нужно дозвонится до перевозчика\n"
                f"• 🛂 **Таможня** — проблемы с растаможкой, документами, кодами ТН ВЭД\n"
                f"• 💰 **Платежи** — задержка оплаты, возврат, эскроу-вопрос\n"
                f"• 👤 **Менеджер** — общая координация / VIP-клиент / эскалация"
            ),
            cards=[{
                "type": "form",
                "data": {
                    "title": f"👥 Назначить заказ #{order.id} специалисту",
                    "submit_action": "op_assign",
                    "fields": [
                        {
                            "name": "to_role", "label": "Кому передать ответственность",
                            "required": True, "type": "select",
                            "options": [
                                {"value": "manager",  "label": "👤 Менеджер — общая координация"},
                                {"value": "logist",   "label": "🚚 Логист — транзит и доставка"},
                                {"value": "customs",  "label": "🛂 Таможня — растаможка, документы"},
                                {"value": "payments", "label": "💰 Платежи — оплаты, эскроу, возвраты"},
                            ],
                        },
                        {"name": "comment", "label": "Причина / контекст (видно назначенному)"},
                    ],
                    "fixed_params": {"order_id": order.id, "confirmed": True},
                },
            }],
        )

    # Шаг 2: запись
    comment = (params.get("comment") or "").strip()
    _log_event(
        order, "operator_action", actor=user, source="operator",
        meta={"kind": "assigned", "to_role": to_role, "by": user.username, "comment": comment},
    )
    # Уведомим самого оператора (что он теперь owner) — best-effort: ищем
    # любого пользователя с username demo_operator или с operator_role==to_role.
    try:
        from django.contrib.auth import get_user_model
        target = (
            get_user_model().objects.filter(username="demo_operator").first()
            or get_user_model().objects.filter(is_staff=True).first()
        )
        if target:
            _notify(
                target, kind="order",
                title=f"Вам назначен заказ #{order.id}",
                body=f"{user.username} назначил роль «{to_role}» по заказу {order.customer_name}.",
                url=f"/chat/?order={order.id}",
            )
    except Exception:
        logger.exception("op_assign notify failed")

    return ActionResult(
        text=f"✓ Заказ #{order.id} назначен на «{to_role}».",
        contextual_actions=[
            {"action": "op_order_detail", "label": "Открыть заказ", "params": {"order_id": order.id}},
            {"action": "op_dashboard", "label": "← Сводка"},
        ],
    )


# ══════════════════════════════════════════════════════════
# 6. Add note — операторская заметка (writing → DraftCard)
# ══════════════════════════════════════════════════════════

@register("op_add_note")
def op_add_note(params, user, role):
    err = _ensure_operator(role)
    if err:
        return err
    from marketplace.models import Order

    try:
        order = Order.objects.get(id=int(params.get("order_id") or 0))
    except (Order.DoesNotExist, ValueError, TypeError):
        return ActionResult(text="Заказ не найден.")

    text = (params.get("text") or "").strip()
    confirmed = bool(params.get("confirmed"))

    if not confirmed or not text:
        return ActionResult(
            text=f"Заметка к заказу #{order.id}",
            cards=[{
                "type": "form",
                "data": {
                    "title": f"📝 Заметка · #{order.id}",
                    "submit_action": "op_add_note",
                    "fields": [
                        {"name": "text", "label": "Текст", "type": "textarea", "required": True},
                    ],
                    "fixed_params": {"order_id": order.id, "confirmed": True},
                },
            }],
        )

    _log_event(
        order, "operator_action", actor=user, source="operator",
        meta={"kind": "note", "text": text, "by": user.username},
    )
    return ActionResult(
        text=f"✓ Заметка к #{order.id} сохранена.",
        contextual_actions=[
            {"action": "op_order_detail", "label": "Открыть заказ", "params": {"order_id": order.id}},
        ],
    )


# ══════════════════════════════════════════════════════════
# 6b. Assign carrier — назначить перевозчика на заказ
# ══════════════════════════════════════════════════════════

@register("op_assign_carrier")
def op_assign_carrier(params, user, role):
    """Оператор назначает перевозчика и трек-номер.

    Заполняет Order.carrier_* + tracking_*. Юзер видит карточку в
    `track_order` без обращения к LLM. Логируется в OrderEvent для audit.
    """
    err = _ensure_operator(role)
    if err:
        return err
    from marketplace.models import Order

    try:
        order = Order.objects.get(id=int(params.get("order_id") or 0))
    except (Order.DoesNotExist, ValueError, TypeError):
        return ActionResult(text="Заказ не найден.")

    confirmed = bool(params.get("confirmed"))
    carrier_name    = (params.get("carrier_name") or "").strip()[:120]
    tracking_number = (params.get("tracking_number") or "").strip()[:80]
    tracking_url    = (params.get("tracking_url") or "").strip()[:500]
    carrier_phone   = (params.get("carrier_phone") or "").strip()[:40]
    carrier_email   = (params.get("carrier_email") or "").strip()[:120]

    if not confirmed:
        # Phase 1: форма с предзаполнением (если уже что-то было)
        return ActionResult(
            text=(
                f"🚚 Назначение перевозчика для ORD-{order.id} "
                f"(текущий статус: {order.get_status_display()}).\n"
                f"После сохранения покупатель сразу увидит карточку логиста "
                f"и получит уведомление."
            ),
            cards=[{
                "type": "form",
                "data": {
                    "title": f"🚚 Перевозчик · ORD-{order.id}",
                    "submit_action": "op_assign_carrier",
                    "submit_label": "✓ Назначить и уведомить покупателя",
                    "fields": [
                        {"name": "carrier_name", "label": "Перевозчик",
                         "required": True, "value": order.carrier_name or "",
                         "placeholder": "Maersk · DHL · СДЭК · Деловые Линии"},
                        {"name": "tracking_number", "label": "Трек-номер",
                         "required": True, "value": order.tracking_number or "",
                         "placeholder": "MAEU1234567"},
                        {"name": "tracking_url", "label": "URL трекинга (опц.)",
                         "value": order.tracking_url or "",
                         "placeholder": "https://www.maersk.com/tracking/MAEU1234567"},
                        {"name": "carrier_phone", "label": "Телефон (опц.)",
                         "value": order.carrier_phone or "",
                         "placeholder": "+7 800 100-25-15"},
                        {"name": "carrier_email", "label": "Email (опц.)",
                         "type": "email", "value": order.carrier_email or "",
                         "placeholder": "support@carrier.com"},
                    ],
                    "fixed_params": {"order_id": order.id, "confirmed": True},
                },
            }],
            contextual_actions=[
                {"action": "op_order_detail", "label": "← К заказу",
                 "params": {"order_id": order.id}},
            ],
        )

    if not carrier_name or not tracking_number:
        return ActionResult(text="⚠️ Перевозчик и трек-номер обязательны.")

    # Сохраняем
    prev_carrier = order.carrier_name
    prev_track   = order.tracking_number
    order.carrier_name    = carrier_name
    order.tracking_number = tracking_number
    order.tracking_url    = tracking_url
    order.carrier_phone   = carrier_phone
    order.carrier_email   = carrier_email
    order.save(update_fields=[
        "carrier_name", "tracking_number", "tracking_url",
        "carrier_phone", "carrier_email",
    ])

    # Audit-log
    _log_event(
        order, "carrier_assigned", actor=user, source="operator",
        meta={
            "carrier_name": carrier_name,
            "tracking_number": tracking_number,
            "tracking_url": tracking_url,
            "carrier_phone": carrier_phone,
            "carrier_email": carrier_email,
            "prev_carrier": prev_carrier,
            "prev_tracking": prev_track,
            "by": user.username,
        },
    )

    # Уведомляем buyer + seller в их shipment-чатах: «оператор назначил перевозчика»
    try:
        from .order_events import notify_order_event
        notify_order_event(
            order, "carrier_assigned",
            actor=user,
            text=(
                f"🚚 Назначен перевозчик: **{carrier_name}** · "
                f"трек-номер `{tracking_number}`."
                + (f"\nURL: {tracking_url}" if tracking_url else "")
                + (f"\nТелефон: {carrier_phone}" if carrier_phone else "")
            ),
            extra_actions=[
                {"action": "track_order", "label": "📦 Открыть трекинг",
                 "params": {"order_id": order.id}},
            ] + ([
                {"action": "open_url", "label": f"🔗 {carrier_name}",
                 "params": {"url": tracking_url}},
            ] if tracking_url else []),
            targets=("buyer", "seller"),
        )
    except Exception:
        logger.exception("notify_order_event(carrier_assigned) failed")

    return ActionResult(
        text=(
            f"✅ Перевозчик для ORD-{order.id} назначен.\n"
            f"• Перевозчик: {carrier_name}\n"
            f"• Трек: {tracking_number}\n"
            f"Покупатель и поставщик получили уведомление в свои чаты."
        ),
        contextual_actions=[
            {"action": "op_order_detail", "label": "← К заказу",
             "params": {"order_id": order.id}},
            {"action": "track_order", "label": "📦 Открыть трекинг",
             "params": {"order_id": order.id}},
        ],
    )


# ══════════════════════════════════════════════════════════
# 7. Resolve dispute — закрыть спор (writing → DraftCard)
# ══════════════════════════════════════════════════════════

@register("op_resolve_dispute")
def op_resolve_dispute(params, user, role):
    err = _ensure_operator(role)
    if err:
        return err
    from marketplace.models import Order

    try:
        order = Order.objects.get(id=int(params.get("order_id") or 0))
    except (Order.DoesNotExist, ValueError, TypeError):
        return ActionResult(text="Заказ не найден.")

    # FIX (HIGH): обычный оператор может resolve dispute только по своим
    # назначенным заказам (PIVOT 2026-05-27). Только staff (lead-operator)
    # может закрыть любой спор.
    if not user.is_staff and order.assigned_operator_id and order.assigned_operator_id != user.id:
        return ActionResult(text="Этот спор не назначен на вас — обратитесь к старшему оператору.")

    resolution = (params.get("resolution") or "").strip().lower()
    refund_amount_raw = params.get("refund_amount") or "0"
    confirmed = bool(params.get("confirmed"))

    if not confirmed or resolution not in DISPUTE_RESOLUTIONS:
        return ActionResult(
            text=f"Резолюция по спору · #{order.id}",
            cards=[{
                "type": "form",
                "data": {
                    "title": f"⚖️ Закрыть спор · #{order.id}",
                    "submit_action": "op_resolve_dispute",
                    "fields": [
                        {
                            "name": "resolution", "label": "Решение", "required": True,
                            "type": "select",
                            "options": [
                                {"value": "refund", "label": "Полный возврат"},
                                {"value": "partial_refund", "label": "Частичный возврат"},
                                {"value": "release", "label": "Выпустить платёж продавцу"},
                                {"value": "no_action", "label": "Закрыть без действий"},
                            ],
                        },
                        {"name": "refund_amount", "label": "Сумма возврата ($)", "type": "number"},
                        {"name": "reason", "label": "Обоснование", "type": "textarea", "required": True},
                    ],
                    "fixed_params": {"order_id": order.id, "confirmed": True},
                },
            }],
        )

    try:
        refund_amount = Decimal(str(refund_amount_raw)) if refund_amount_raw else Decimal("0")
    except Exception:
        refund_amount = Decimal("0")
    reason = (params.get("reason") or "").strip()

    # Side-effects: статус оплаты + реальное движение эскроу (multi-seller split)
    from decimal import Decimal as _D

    from marketplace.models import OrderItem as _OI

    from . import payments as _pay
    from .rating import record_rating_event

    money_moved = ""
    new_payment_status = order.payment_status

    # Поставщики этого заказа (для rating events)
    sellers_in_order = list({
        oi.part.seller for oi in
        _OI.objects.filter(order=order).select_related("part__seller")
        if oi.part and oi.part.seller
    })

    try:
        if resolution == "refund":
            res = _pay.refund_to_buyer(order=order, buyer=order.buyer)
            if res.get("ok"):
                money_moved = f" · возврат ${res['amount']:,.2f} → покупатель"
            new_payment_status = "refunded"
            # Rating: refund → claim_confirmed (-7) для всех продавцов заказа
            for s in sellers_in_order:
                record_rating_event(s, event_type="claim_confirmed",
                                    meta={"order_id": order.id, "resolution": "refund",
                                          "reason": reason[:200]})
        elif resolution == "partial_refund":
            if refund_amount > 0 and order.buyer:
                res = _pay.refund_to_buyer(order=order, buyer=order.buyer, amount=refund_amount)
                if res.get("ok"):
                    money_moved = f" · возврат ${res['amount']:,.2f} → покупатель"
            new_payment_status = "refund_pending"
            # Rating: partial_refund → return (-5) для всех продавцов заказа
            for s in sellers_in_order:
                record_rating_event(s, event_type="return",
                                    meta={"order_id": order.id, "resolution": "partial_refund",
                                          "amount": float(refund_amount)})
        elif resolution == "release":
            splits = _pay.split_by_seller(order)
            released_total = _D("0")
            for s in splits:
                res = _pay.release_to_seller(order=order, seller=s["seller"], amount=s["amount"])
                if res.get("ok"):
                    released_total += _D(str(res["amount"]))
            if released_total > 0:
                n = len(splits)
                money_moved = (
                    f" · выплата ${released_total:,.2f} → продавц"
                    + ("ам" if n > 1 else "у")
                )
            new_payment_status = "paid"
            # release → нейтрально для рейтинга (споp закрыт в пользу продавца)
    except Exception:
        logger.exception("escrow move on dispute resolution failed")

    if new_payment_status != order.payment_status:
        order.payment_status = new_payment_status
        order.save(update_fields=["payment_status"])

    _log_event(
        order, "operator_action", actor=user, source="operator",
        meta={
            "kind": "dispute_resolved",
            "resolution": resolution,
            "refund_amount": float(refund_amount),
            "reason": reason,
            "by": user.username,
            "money_moved": money_moved,
        },
    )

    # Уведомим обе стороны
    if order.buyer:
        _notify(
            order.buyer, kind="payment",
            title=f"Спор по заказу #{order.id} закрыт",
            body=f"Решение: {resolution}.{money_moved} {reason[:120]}",
            url=f"/chat/?order={order.id}",
        )

    return ActionResult(
        text=(
            f"✓ Спор по #{order.id} закрыт · решение «{resolution}»"
            + (f" · возврат ${refund_amount:,.0f}" if refund_amount else "")
            + "."
        ),
        contextual_actions=[
            {"action": "op_order_detail", "label": "Открыть заказ", "params": {"order_id": order.id}},
            {"action": "op_dashboard", "label": "← Сводка"},
        ],
    )


# ══════════════════════════════════════════════════════════
# 8. Customs / Compliance flow
# ══════════════════════════════════════════════════════════

def _customs_meta(order) -> dict:
    """Read customs section from logistics_meta JSON; default empty."""
    meta = order.logistics_meta or {}
    return dict(meta.get("customs") or {})


def _save_customs_meta(order, customs: dict):
    """Persist customs dict back into logistics_meta."""
    meta = dict(order.logistics_meta or {})
    meta["customs"] = customs
    order.logistics_meta = meta
    order.save(update_fields=["logistics_meta"])


@register("op_hs_lookup")
def op_hs_lookup(params, user, role):
    """Поиск ТН ВЭД по описанию или артикулу."""
    err = _ensure_operator(role)
    if err:
        return err
    from .customs_data import find_hs_codes

    query = (params.get("query") or "").strip()
    if not query:
        return ActionResult(
            text="Введите описание детали или артикул для поиска ТН ВЭД.",
            cards=[{
                "type": "form",
                "data": {
                    "title": "🔎 Поиск ТН ВЭД",
                    "submit_action": "op_hs_lookup",
                    "fields": [{"name": "query", "label": "Описание / артикул", "required": True}],
                },
            }],
        )
    hits = find_hs_codes(query, limit=5)
    if not hits:
        return ActionResult(
            text=f"Не нашёл ТН ВЭД для «{query}». Попробуйте другие ключевые слова.",
            suggestions=["насос", "фильтр", "подшипник", "гидроцилиндр", "шестерня"],
        )
    return ActionResult(
        text=f"Найдено {len(hits)} ТН ВЭД-кодов по запросу «{query}».",
        cards=[{
            "type": "list",
            "data": {
                "title": "🔎 Кандидаты ТН ВЭД",
                "items": [
                    {"title": f"{h['code']} · {h['title']}",
                     "subtitle": "Ключевые слова: " + ", ".join(h["keywords"][:4])}
                    for h in hits
                ],
            },
        }],
        suggestions=[f"присвой {hits[0]['code']} заказу" if hits else "уточни запрос"],
    )


@register("op_hs_assign")
def op_hs_assign(params, user, role):
    """Присвоить HS-код заказу. Two-step DraftCard."""
    err = _ensure_operator(role)
    if err:
        return err
    from marketplace.models import Order
    try:
        order = Order.objects.get(id=int(params.get("order_id") or 0))
    except (Order.DoesNotExist, ValueError, TypeError):
        return ActionResult(text="Заказ не найден.")

    hs_code = (params.get("hs_code") or "").strip()
    country = (params.get("country") or "RU").strip().upper()
    confirmed = bool(params.get("confirmed"))

    if not confirmed or not hs_code:
        cur = _customs_meta(order)
        return ActionResult(
            text=f"Присвоить ТН ВЭД заказу #{order.id}",
            cards=[{
                "type": "form",
                "data": {
                    "title": f"📋 ТН ВЭД · #{order.id}",
                    "submit_action": "op_hs_assign",
                    "fields": [
                        {"name": "hs_code", "label": "Код ТН ВЭД (например 8413.50)",
                         "required": True, "value": cur.get("hs_code", "")},
                        {"name": "country", "label": "Страна импорта (ISO-2, RU/BY/KZ/AM/KG)",
                         "value": cur.get("country", "RU")},
                    ],
                    "fixed_params": {"order_id": order.id, "confirmed": True},
                },
            }],
        )

    customs = _customs_meta(order)
    customs.update({"hs_code": hs_code, "country": country})
    _save_customs_meta(order, customs)
    _log_event(
        order, "operator_action", actor=user, source="operator",
        meta={"kind": "customs_hs_assigned", "hs_code": hs_code, "country": country},
    )
    return ActionResult(
        text=f"✓ Заказу #{order.id} присвоен ТН ВЭД {hs_code} (страна {country}).",
        contextual_actions=[
            {"action": "op_calc_duty", "label": "💰 Рассчитать пошлину", "params": {"order_id": order.id}},
            {"action": "op_certs_check", "label": "📑 Проверить сертификаты", "params": {"order_id": order.id}},
        ],
    )


@register("op_calc_duty")
def op_calc_duty(params, user, role):
    """Расчёт таможенной пошлины + НДС + сборов по заказу."""
    err = _ensure_operator(role)
    if err:
        return err
    from marketplace.models import Order

    from .customs_data import duty_rate_for, fees_for, vat_rate_for

    try:
        order = Order.objects.get(id=int(params.get("order_id") or 0))
    except (Order.DoesNotExist, ValueError, TypeError):
        return ActionResult(text="Заказ не найден.")

    customs = _customs_meta(order)
    hs_code = (params.get("hs_code") or customs.get("hs_code") or "").strip()
    country = (params.get("country") or customs.get("country") or "RU").upper()
    if not hs_code:
        return ActionResult(
            text=f"Сначала присвойте ТН ВЭД заказу #{order.id}.",
            contextual_actions=[
                {"action": "op_hs_assign", "label": "📋 Присвоить ТН ВЭД", "params": {"order_id": order.id}},
            ],
        )

    base = Decimal(str(order.total_amount or 0))
    duty_pct = duty_rate_for(hs_code)
    vat_pct = vat_rate_for(country)
    fees = fees_for(country)

    duty = (base * duty_pct / Decimal("100")).quantize(Decimal("0.01"))
    vat_base = base + duty
    vat = (vat_base * vat_pct / Decimal("100")).quantize(Decimal("0.01"))
    broker = fees["broker"]
    terminal = fees["terminal"]
    total = (duty + vat + broker + terminal).quantize(Decimal("0.01"))

    # Сохраним расчёт
    customs.update({
        "hs_code": hs_code, "country": country,
        "duty_pct": float(duty_pct), "duty": float(duty),
        "vat_pct": float(vat_pct), "vat": float(vat),
        "broker": float(broker), "terminal": float(terminal),
        "duty_total": float(total),
    })
    _save_customs_meta(order, customs)
    _log_event(order, "operator_action", actor=user, source="operator",
               meta={"kind": "customs_duty_calculated", "duty_total": float(total)})

    return ActionResult(
        text=(
            f"Пошлина по заказу #{order.id} ({hs_code} → {country})\n"
            f"База: ${base:,.2f} · пошлина {duty_pct}% = ${duty:,.2f} · "
            f"НДС {vat_pct}% = ${vat:,.2f}\n"
            f"Брокер ${broker} · терминал ${terminal} · ИТОГО ${total:,.2f}"
        ),
        cards=[{
            "type": "kpi_grid",
            "data": {
                "title": f"💰 Расчёт пошлины · #{order.id}",
                "items": [
                    {"label": "База", "value": f"${base:,.2f}"},
                    {"label": f"Пошлина {duty_pct}%", "value": f"${duty:,.2f}"},
                    {"label": f"НДС {vat_pct}%", "value": f"${vat:,.2f}"},
                    {"label": "Брокер", "value": f"${broker}"},
                    {"label": "Терминал", "value": f"${terminal}"},
                    {"label": "ИТОГО", "value": f"${total:,.2f}", "tone": "info"},
                ],
            },
        }],
        contextual_actions=[
            {"action": "op_certs_check", "label": "📑 Сертификаты", "params": {"order_id": order.id}},
            {"action": "op_customs_release", "label": "🚚 Выпустить с таможни", "params": {"order_id": order.id}},
        ],
    )


@register("op_certs_check")
def op_certs_check(params, user, role):
    """Проверка наличия сертификатов для заказа."""
    err = _ensure_operator(role)
    if err:
        return err
    from marketplace.models import Order

    from .customs_data import required_certs_for

    try:
        order = Order.objects.get(id=int(params.get("order_id") or 0))
    except (Order.DoesNotExist, ValueError, TypeError):
        return ActionResult(text="Заказ не найден.")

    customs = _customs_meta(order)
    hs_code = customs.get("hs_code", "")
    required = required_certs_for(hs_code)
    have = list(customs.get("certs") or [])
    missing = [c for c in required if c not in have]

    text = (
        f"Сертификаты по заказу #{order.id} (ТН ВЭД {hs_code or '—'})\n"
        f"Требуется: {', '.join(required) or '—'}\n"
        f"В наличии: {', '.join(have) or 'нет'}\n"
        + (f"❗ Не хватает: {', '.join(missing)}" if missing else "✓ Все сертификаты на месте")
    )
    items = [{"label": c, "value": "✓" if c in have else "✗",
              "tone": "ok" if c in have else "warn"} for c in required]

    return ActionResult(
        text=text,
        cards=[{"type": "kpi_grid", "data": {"title": f"📑 Сертификаты #{order.id}", "items": items}}],
        contextual_actions=(
            [{"action": "op_cert_upload", "label": f"⬆ Загрузить {missing[0]}",
              "params": {"order_id": order.id, "cert": missing[0]}}] if missing else
            [{"action": "op_customs_release", "label": "🚚 Выпустить с таможни",
              "params": {"order_id": order.id}}]
        ),
    )


@register("op_cert_upload")
def op_cert_upload(params, user, role):
    """Зафиксировать загрузку сертификата (writing → DraftCard)."""
    err = _ensure_operator(role)
    if err:
        return err
    from marketplace.models import Order

    try:
        order = Order.objects.get(id=int(params.get("order_id") or 0))
    except (Order.DoesNotExist, ValueError, TypeError):
        return ActionResult(text="Заказ не найден.")

    cert = (params.get("cert") or "").strip()
    number = (params.get("number") or "").strip()
    confirmed = bool(params.get("confirmed"))

    if not confirmed or not cert:
        return ActionResult(
            text=f"Загрузка сертификата · #{order.id}",
            cards=[{
                "type": "form",
                "data": {
                    "title": f"⬆ Сертификат · #{order.id}",
                    "submit_action": "op_cert_upload",
                    "fields": [
                        {"name": "cert", "label": "Тип (EAC, ТР ТС 010/2011, ...)", "required": True,
                         "value": cert},
                        {"name": "number", "label": "Номер документа"},
                    ],
                    "fixed_params": {"order_id": order.id, "confirmed": True},
                },
            }],
        )

    customs = _customs_meta(order)
    have = list(customs.get("certs") or [])
    if cert not in have:
        have.append(cert)
    customs["certs"] = have
    customs.setdefault("cert_numbers", {})[cert] = number
    _save_customs_meta(order, customs)
    _log_event(order, "operator_action", actor=user, source="operator",
               meta={"kind": "customs_cert_uploaded", "cert": cert, "number": number})

    return ActionResult(
        text=f"✓ Сертификат «{cert}» добавлен к заказу #{order.id}.",
        contextual_actions=[
            {"action": "op_certs_check", "label": "📑 Проверить", "params": {"order_id": order.id}},
        ],
    )


@register("op_sanctions_check")
def op_sanctions_check(params, user, role):
    """Проверить контрагента/страну/категорию на санкции."""
    err = _ensure_operator(role)
    if err:
        return err
    from .customs_data import sanctions_check

    country = (params.get("country") or "").strip()
    entity = (params.get("entity") or "").strip()
    category = (params.get("category") or "").strip()
    if not (country or entity or category):
        return ActionResult(
            text="Что проверить на санкции?",
            cards=[{
                "type": "form",
                "data": {
                    "title": "🚫 Санкционная проверка",
                    "submit_action": "op_sanctions_check",
                    "fields": [
                        {"name": "country", "label": "Страна (ISO-2, например IR)"},
                        {"name": "entity", "label": "Контрагент / организация"},
                        {"name": "category", "label": "Категория (dual_use_chip)"},
                    ],
                },
            }],
        )
    res = sanctions_check(country=country, entity=entity, category=category)
    level = res["level"]
    icon = {"high": "🚫", "medium": "⚠️", "low": "ℹ️", "none": "✓"}[level]
    label = {"high": "Запрещено", "medium": "Под контролем", "low": "Серая зона", "none": "Чисто"}[level]
    items = [
        {"label": "Уровень", "value": label,
         "tone": {"high": "bad", "medium": "warn", "low": "warn", "none": "ok"}[level]},
    ]
    if country:  items.append({"label": "Страна", "value": country.upper()})
    if entity:   items.append({"label": "Контрагент", "value": entity})
    if category: items.append({"label": "Категория", "value": category})

    text_lines = [f"{icon} Санкционная проверка: {label}."]
    text_lines.extend("• " + r for r in (res["reasons"] or ["Нет совпадений в списках."]))

    return ActionResult(
        text="\n".join(text_lines),
        cards=[{"type": "kpi_grid", "data": {"title": "🚫 Санкции", "items": items}}],
    )


@register("op_customs_dashboard")
def op_customs_dashboard(params, user, role):
    """Сводка по таможне: на оформлении, документы готовы, недостающие, средний срок."""
    err = _ensure_operator(role)
    if err:
        return err
    from marketplace.models import Order

    from datetime import timedelta

    from django.db.models import Sum
    from django.utils import timezone

    from marketplace.models import OrderEvent

    now = timezone.now()
    on_customs = list(Order.objects.filter(status="customs"))
    awaiting_docs = 0
    ready_to_release = 0
    overdue_3d = 0
    total = 0
    money_stuck = 0.0
    stuck_days = []
    for o in on_customs:
        total += 1
        money_stuck += float(o.total_amount or 0)
        cm = _customs_meta(o)
        if not cm.get("hs_code") or not cm.get("certs"):
            awaiting_docs += 1
        else:
            ready_to_release += 1
        ev = OrderEvent.objects.filter(
            order=o, event_type="status_changed", meta__to="customs",
        ).order_by("-created_at").first()
        if ev:
            days = (now - ev.created_at).days
            stuck_days.append(days)
            if days >= 3:
                overdue_3d += 1

    avg_stuck = (sum(stuck_days) / len(stuck_days)) if stuck_days else 0
    docs_completeness = (ready_to_release * 100 // total) if total else 100

    # Тренд: грузов прошло таможню за 30д vs 30д
    cutoff_30 = now - timedelta(days=30)
    cutoff_60 = now - timedelta(days=60)
    released_30 = OrderEvent.objects.filter(
        event_type="status_changed", meta__from="customs", meta__to="transit_rf",
        created_at__gte=cutoff_30,
    ).count()
    released_prev = OrderEvent.objects.filter(
        event_type="status_changed", meta__from="customs", meta__to="transit_rf",
        created_at__gte=cutoff_60, created_at__lt=cutoff_30,
    ).count()
    if released_prev:
        flow_delta = int((released_30 - released_prev) * 100 / released_prev)
    elif released_30:
        flow_delta = 100
    else:
        flow_delta = 0
    arrow = "↑" if flow_delta > 0 else ("↓" if flow_delta < 0 else "→")

    # Среднее время на таможне (по выпущенным за 30д)
    released_qs = OrderEvent.objects.filter(
        event_type="status_changed", meta__from="customs", meta__to="transit_rf",
        created_at__gte=cutoff_30,
    )[:200]
    release_times = []
    for ev in released_qs:
        entered = OrderEvent.objects.filter(
            order_id=ev.order_id, event_type="status_changed", meta__to="customs",
            created_at__lte=ev.created_at,
        ).order_by("-created_at").first()
        if entered:
            release_times.append((ev.created_at - entered.created_at).total_seconds() / 86400)
    avg_release_days = (sum(release_times) / len(release_times)) if release_times else None

    rows = []
    for o in on_customs[:10]:
        cm = _customs_meta(o)
        hs = cm.get("hs_code") or "—"
        certs = cm.get("certs") or []
        has_docs = bool(hs and hs != "—" and certs)
        tone = "ok" if has_docs else "warn"
        badge_label = "К выпуску" if has_docs else "Нет доков"
        rows.append({
            "title": f"#{o.id} · {o.customer_name}",
            "subtitle": f"{o.get_status_display()} · ТН ВЭД {hs} · "
                        f"${float(o.total_amount or 0):,.0f}",
            "tone": tone,
            "badge": {"label": badge_label, "tone": tone},
            "action": "op_order_detail",
            "params": {"order_id": o.id},
        })

    # Плитки кликабельны → очередь с фильтром (только когда есть что показать).
    def _qc(flt):
        return {"action": "op_queue", "params": {"filter": flt}}

    kpi_items = [
        {"label": "Доля готовых к выпуску",
         "value": f"{docs_completeness}%",
         "tone": "ok" if docs_completeness >= 80 else ("warn" if docs_completeness >= 50 else "bad"),
         **(_qc("customs") if total else {})},
        {"label": "Средний срок на таможне",
         "value": f"{avg_stuck:.1f} дн",
         "tone": "bad" if avg_stuck > 5 else ("warn" if avg_stuck > 3 else "ok"),
         **(_qc("customs") if total else {})},
        {"label": "Просрочены >3 дн",
         "value": str(overdue_3d),
         "tone": "bad" if overdue_3d else "ok",
         **(_qc("customs") if overdue_3d else {})},
        {"label": "Сумма на таможне",
         "value": f"${money_stuck:,.0f}", "tone": "info",
         **(_qc("customs") if total else {})},
    ]
    if avg_release_days is not None:
        kpi_items.append({
            "label": "Ср. срок выпуска (30д)",
            "value": f"{avg_release_days:.1f} дн",
            "tone": "ok" if avg_release_days <= 3 else "warn",
            **_qc("transit"),
        })
    if released_30 or released_prev:
        kpi_items.append({
            "label": "Поток через таможню 30д",
            "value": f"{arrow} {abs(flow_delta)}% ({released_30} шт)",
            "tone": "ok" if flow_delta >= 0 else "warn",
            **_qc("transit"),
        })

    # Actionable инсайт
    if overdue_3d:
        cust_insight = (f"{overdue_3d} грузов застряли на таможне >3 дн — "
                         f"проверьте состояние оформления.")
    elif docs_completeness < 50 and total:
        cust_insight = (f"Только {docs_completeness}% грузов готовы к выпуску — "
                         f"загрузите ТН ВЭД и сертификаты в красные карточки.")
    elif flow_delta < -20 and released_prev:
        cust_insight = (f"Поток через таможню упал на {abs(flow_delta)}% — "
                         f"возможны проблемы у брокера.")
    elif total == 0:
        cust_insight = "Нет грузов на таможне."
    else:
        cust_insight = f"На таможне {total} грузов, поток в норме."

    return ActionResult(
        text=cust_insight,
        cards=[
            {"type": "kpi_grid", "data": {"title": "🛂 Скорость и качество прохождения", "items": kpi_items}},
            {"type": "list", "data": {"title": f"На таможне сейчас ({total})",
                "items": rows or [{"title": "Нет грузов на таможне"}]}},
        ],
        contextual_actions=[
            {"action": "op_analytics_hub", "label": "← Аналитика"},
            {"action": "op_queue", "label": "📋 Очередь оператора", "params": {"filter": "open"}},
            {"action": "go_home", "label": "🏠 Главная"},
        ],
    )


@register("op_customs_release")
def op_customs_release(params, user, role):
    """Выпустить груз с таможни — переводит status `customs` → `transit_rf`."""
    err = _ensure_operator(role)
    if err:
        return err
    from marketplace.models import Order

    from .customs_data import required_certs_for

    try:
        order = Order.objects.get(id=int(params.get("order_id") or 0))
    except (Order.DoesNotExist, ValueError, TypeError):
        return ActionResult(text="Заказ не найден.")

    confirmed = bool(params.get("confirmed"))
    customs = _customs_meta(order)
    hs_code = customs.get("hs_code", "")
    have = list(customs.get("certs") or [])
    required = required_certs_for(hs_code)
    missing = [c for c in required if c not in have]

    if not confirmed:
        warn = ""
        if not hs_code:
            warn = "❗ ТН ВЭД не присвоен."
        elif missing:
            warn = f"❗ Нет сертификатов: {', '.join(missing)}"
        elif not customs.get("duty_total"):
            warn = "ℹ️ Пошлина не рассчитана."
        return ActionResult(
            text=f"Выпустить груз #{order.id} с таможни?\n{warn}",
            cards=[{
                "type": "form",
                "data": {
                    "title": f"🚚 Выпуск с таможни · #{order.id}",
                    "submit_action": "op_customs_release",
                    "fields": [
                        {"name": "comment", "label": "Комментарий (необязательно)"},
                    ],
                    "fixed_params": {"order_id": order.id, "confirmed": True},
                },
            }],
            contextual_actions=(
                [{"action": "op_hs_assign", "label": "📋 Присвоить ТН ВЭД",
                  "params": {"order_id": order.id}}] if not hs_code else
                [{"action": "op_certs_check", "label": "📑 Проверить сертификаты",
                  "params": {"order_id": order.id}}] if missing else
                [{"action": "op_calc_duty", "label": "💰 Рассчитать пошлину",
                  "params": {"order_id": order.id}}] if not customs.get("duty_total") else []
            ),
        )

    # Жёсткая проверка перед записью
    blockers = []
    if not hs_code:
        blockers.append("нет ТН ВЭД")
    if missing:
        blockers.append("не хватает сертификатов: " + ", ".join(missing))
    if blockers:
        return ActionResult(
            text="⚠️ Нельзя выпустить: " + "; ".join(blockers) + ".",
            contextual_actions=[
                {"action": "op_customs_release", "label": "Открыть форму выпуска",
                 "params": {"order_id": order.id}},
            ],
        )

    if order.status == "customs":
        order.status = "transit_rf"
        order.save(update_fields=["status"])
        _log_event(order, "status_changed", actor=user, source="operator",
                   meta={"from": "customs", "to": "transit_rf", "by": "op_customs_release"})

    comment = (params.get("comment") or "").strip()
    _log_event(order, "operator_action", actor=user, source="operator",
               meta={"kind": "customs_released", "comment": comment})

    if order.buyer:
        _notify(order.buyer, kind="order",
                title=f"Груз #{order.id} выпущен с таможни",
                body="Едет к вам · следите за трекингом.",
                url=f"/chat/?order={order.id}")

    return ActionResult(
        text=f"✓ Заказ #{order.id} выпущен с таможни → транзит РФ.",
        contextual_actions=[
            {"action": "op_customs_dashboard", "label": "← Сводка таможни"},
            {"action": "op_order_detail", "label": "Открыть заказ", "params": {"order_id": order.id}},
        ],
    )


# ══════════════════════════════════════════════════════════
# 9. Payments dashboard — эскроу + outstanding holds
# ══════════════════════════════════════════════════════════

@register("op_logistics_stats")
def op_logistics_stats(params, user, role):
    """Логистическая аналитика для оператора:
       — KPI сводка (завершено / в транзите / на таможне / средний срок)
       — Активные маршруты по заказам (что сейчас нужно контролировать)
       — Разбивка по перевозчикам
    """
    err = _ensure_operator(role)
    if err:
        return err
    from datetime import timedelta

    from django.db.models import Count, Sum

    from marketplace.models import Order, OrderEvent

    from marketplace.models import Order, OrderEvent

    completed = Order.objects.filter(status="completed").count()
    delivered = Order.objects.filter(status="delivered").count()
    in_transit = Order.objects.filter(status__in=("transit_abroad", "transit_rf", "issuing")).count()
    on_customs = Order.objects.filter(status="customs").count()

    # Средний срок «создан → доставлен» по уже завершённым (за последние 90 дней)
    cutoff = timezone.now() - timedelta(days=90)
    delivered_orders = Order.objects.filter(status__in=("delivered", "completed"),
                                             created_at__gte=cutoff)
    avg_days = None
    if delivered_orders.exists():
        deltas = []
        for o in delivered_orders[:200]:
            ev = OrderEvent.objects.filter(order=o, event_type="status_changed",
                                            meta__to="delivered").order_by("-created_at").first()
            if ev and o.created_at:
                deltas.append((ev.created_at - o.created_at).total_seconds() / 86400)
        if deltas:
            avg_days = sum(deltas) / len(deltas)

    # Активные маршруты — заказы, которые сейчас в пути / на таможне / на выдаче.
    # Это самое важное для оператора: за этими подрядчиками нужно следить.
    LIVE_STATUSES = ("transit_abroad", "customs", "transit_rf", "issuing")
    STAGE_META = {
        "transit_abroad": ("Зарубежная логистика", "info"),
        "customs":        ("Таможенный брокер",   "warn"),
        "transit_rf":     ("РФ-логист",             "info"),
        "issuing":        ("Пункт выдачи",          "ok"),
    }
    MODE_LABEL = {"sea": "Море", "air": "Авиа", "auto": "Авто", "rail": "ЖД"}

    # Локализация технических carrier-значений.
    # Реальные бренды (MAERSK, DHL, COSCO …) — proper nouns, не переводятся.
    # Технические маркеры (SELF, INTERNAL_FALLBACK) — переводим в человеко-читаемое.
    def _carrier_label(raw: str) -> str:
        from django.utils.translation import get_language
        lang = (get_language() or "ru").split("-")[0]
        key = (raw or "").strip().upper()
        TRANS = {
            "ru": {
                "SELF":              "Своими силами",
                "INTERNAL_FALLBACK": "Внутр. перевозка",
                "PLATFORM":          "Платформа",
                "UNKNOWN":           "Не указан",
                "":                  "—",
                "—":                 "—",
            },
            "en": {
                "SELF":              "Self-pickup",
                "INTERNAL_FALLBACK": "Internal carrier",
                "PLATFORM":          "Platform",
                "UNKNOWN":           "Unknown",
                "":                  "—",
                "—":                 "—",
            },
        }
        table = TRANS.get(lang) or TRANS["en"]
        return table.get(key, raw or "—")
    now = timezone.now()
    live_qs = Order.objects.filter(status__in=LIVE_STATUSES).select_related("buyer").order_by(
        "ship_deadline" if "ship_deadline" in [f.name for f in Order._meta.fields] else "-created_at"
    )[:30]

    route_rows = []
    for o in live_qs:
        actor, tone = STAGE_META.get(o.status, (o.get_status_display(), "info"))
        meta = (o.logistics_meta or {}) if isinstance(o.logistics_meta, dict) else {}
        tracking = meta.get("tracking_number") or meta.get("tracking") or ""
        carrier = o.logistics_provider or "—"
        mode = MODE_LABEL.get(o.shipping_mode, o.shipping_mode or "—")
        # Маршрут: origin (из meta.customs.country или meta.origin) → destination
        origin = meta.get("origin") or meta.get("origin_country") or "CN"
        customs = meta.get("customs") or {}
        dest = customs.get("country") or "RU"
        # Сколько дней на текущем стейдже — берём последний status_changed.to == cur status
        days_in_stage = None
        ev = OrderEvent.objects.filter(
            order=o, event_type="status_changed", meta__to=o.status,
        ).order_by("-created_at").first()
        if ev:
            days_in_stage = round((now - ev.created_at).total_seconds() / 86400, 1)

        # SLA-маркер: если SLA breached → tone=bad, at_risk → tone=warn
        if o.sla_status == "breached":
            tone = "bad"
        elif o.sla_status == "at_risk" and tone != "bad":
            tone = "warn"

        subtitle_parts = [
            actor,
            mode,
            f"{origin}→{dest}",
        ]
        if tracking:
            subtitle_parts.append(tracking)
        if days_in_stage is not None:
            subtitle_parts.append(f"{days_in_stage}д на этапе")
        subtitle_parts.append(f"${float(o.total_amount or 0):,.0f}")

        route_rows.append({
            "title": f"ORD-{o.id} · {o.customer_name or o.buyer.username if o.buyer else 'N/A'}",
            "subtitle": " · ".join(subtitle_parts),
            "tone": tone,
            "badge": {"label": _carrier_label(carrier)[:24], "tone": tone},
            "action": "op_order_detail",
            "params": {"order_id": o.id},
        })

    # Группируем маршруты по этапу — по одной list-карточке на этап.
    # Так оператору сразу видно «12 на таможне», «6 на выдаче» и т.д.
    from collections import defaultdict as _dd
    routes_by_stage: dict = _dd(list)
    stage_order = ["transit_abroad", "customs", "transit_rf", "issuing"]
    stage_titles = {
        "transit_abroad": "Транзит за рубеж",
        "customs":        "На таможне",
        "transit_rf":     "Транзит по РФ",
        "issuing":        "Выдача / приёмка",
    }
    # Передадим route_rows + status в каждой строке для группировки.
    # Перегенерируем чтобы захватить статус.
    routes_by_stage = _dd(list)
    for o in live_qs:
        actor, tone = STAGE_META.get(o.status, (o.get_status_display(), "info"))
        meta = (o.logistics_meta or {}) if isinstance(o.logistics_meta, dict) else {}
        tracking = meta.get("tracking_number") or meta.get("tracking") or ""
        carrier = o.logistics_provider or "—"
        mode = MODE_LABEL.get(o.shipping_mode, o.shipping_mode or "—")
        origin = meta.get("origin") or meta.get("origin_country") or "CN"
        customs = meta.get("customs") or {}
        dest = customs.get("country") or "RU"
        days_in_stage = None
        ev = OrderEvent.objects.filter(
            order=o, event_type="status_changed", meta__to=o.status,
        ).order_by("-created_at").first()
        if ev:
            days_in_stage = round((now - ev.created_at).total_seconds() / 86400, 1)
        if o.sla_status == "breached":
            tone = "bad"
        elif o.sla_status == "at_risk" and tone != "bad":
            tone = "warn"
        sub_parts = [mode, f"{origin}→{dest}"]
        if tracking: sub_parts.append(tracking)
        if days_in_stage is not None: sub_parts.append(f"{days_in_stage}д на этапе")
        sub_parts.append(f"${float(o.total_amount or 0):,.0f}")
        routes_by_stage[o.status].append({
            "title": f"ORD-{o.id} · {o.customer_name or (o.buyer.username if o.buyer else 'N/A')}",
            "subtitle": " · ".join(sub_parts),
            "tone": tone,
            "badge": {"label": _carrier_label(carrier)[:24], "tone": tone},
            "action": "op_order_detail",
            "params": {"order_id": o.id},
        })

    # Перезаписываем route_rows для пустого случая.
    route_rows = [r for rs in routes_by_stage.values() for r in rs]
    if not route_rows:
        routes_by_stage = {}

    # Разбивка по перевозчикам (logistics_provider) — топ 5 для активных
    by_provider = (
        Order.objects.filter(status__in=LIVE_STATUSES)
        .values("logistics_provider")
        .annotate(n=Count("id")).order_by("-n")[:5]
    )

    # ── Аналитика: где узкие места и скорость по этапам ──
    # Среднее время в каждом этапе (по последним переходам)
    stage_avg_days = {}
    for st in ("transit_abroad", "customs", "transit_rf", "issuing"):
        evs = list(OrderEvent.objects.filter(
            event_type="status_changed", meta__from=st, created_at__gte=cutoff,
        ).order_by("-created_at")[:200])
        durations = []
        for ev in evs:
            entered = OrderEvent.objects.filter(
                order_id=ev.order_id, event_type="status_changed", meta__to=st,
                created_at__lte=ev.created_at,
            ).order_by("-created_at").first()
            if entered:
                durations.append((ev.created_at - entered.created_at).total_seconds() / 86400)
        if durations:
            stage_avg_days[st] = sum(durations) / len(durations)

    # Самый медленный этап
    stage_lbl = {"transit_abroad": "Транзит", "customs": "Таможня",
                  "transit_rf": "По РФ", "issuing": "Выдача"}
    if stage_avg_days:
        slowest = max(stage_avg_days.items(), key=lambda x: x[1])
        slowest_label = f"{stage_lbl[slowest[0]]} ({slowest[1]:.1f}д)"
    else:
        slowest, slowest_label = None, "—"

    # SLA-нарушения сейчас (только активные маршруты)
    live_active = Order.objects.filter(status__in=LIVE_STATUSES)
    sla_breached_n = live_active.filter(sla_status="breached").count()
    sla_atrisk_n = live_active.filter(sla_status="at_risk").count()
    live_total = live_active.count()
    sla_health = 100 - ((sla_breached_n + sla_atrisk_n) * 100 // live_total) if live_total else 100

    # Тренд скорости 30д vs 30д (по delivered)
    prev_cut = now - timedelta(days=60)
    def _avg_lead(start, end):
        qs = Order.objects.filter(status__in=("delivered", "completed"),
                                   created_at__gte=start, created_at__lt=end)[:300]
        deltas = []
        for o in qs:
            ev = OrderEvent.objects.filter(order=o, event_type="status_changed",
                                            meta__to="delivered").order_by("-created_at").first()
            if ev and o.created_at:
                deltas.append((ev.created_at - o.created_at).total_seconds() / 86400)
        return sum(deltas) / len(deltas) if deltas else None
    lead_30 = _avg_lead(now - timedelta(days=30), now)
    lead_prev = _avg_lead(prev_cut, now - timedelta(days=30))
    # Динамика срока — считаем в ДНЯХ (а не в %), это конкретнее.
    if lead_30 and lead_prev:
        days_delta = lead_30 - lead_prev  # >0 = стало медленнее, <0 = быстрее
    else:
        days_delta = 0

    # ── Конструктивная сводка для оператора ──────────────────
    # Принцип: каждая ячейка — реальное число + action-формулировка в sub.
    # Метрик-плейсхолдеров «—» не показываем, если данных нет — карточки нет.
    live_value = float(live_active.aggregate(s=Sum("total_amount"))["s"] or 0)
    delivered_n_90d = delivered_orders.count() if delivered_orders.exists() else 0
    items = []

    # Drill-down карты: каждая KPI-ячейка — кнопка, открывает соответствующий
    # отфильтрованный список заказов.
    # 1) ЧТО СЕЙЧАС НА КОНТРОЛЕ ── главная ячейка
    items.append({
        "label": "Активных отгрузок",
        "value": f"{live_total}",
        "sub": (f"на ${live_value:,.0f}" if live_value else "ни одной в работе"),
        "tone": "info",
        "action": "op_queue" if live_total else None,
        "params": {"filter": "live"} if live_total else None,
    })

    # 2) В зоне риска — показываем только когда есть проблема
    if sla_breached_n or sla_atrisk_n:
        risk_total = sla_breached_n + sla_atrisk_n
        sub_parts = []
        if sla_breached_n:
            sub_parts.append(f"{sla_breached_n} просрочено")
        if sla_atrisk_n:
            sub_parts.append(f"{sla_atrisk_n} под угрозой")
        items.append({
            "label": "Требуют внимания",
            "value": f"{risk_total}",
            "sub": " · ".join(sub_parts),
            "tone": "bad" if sla_breached_n else "warn",
            "action": "op_sla_breach",
            "params": {},
        })

    # 3) На таможне — отдельно, т.к. это самый частый источник задержек
    if on_customs:
        customs_avg = stage_avg_days.get("customs")
        items.append({
            "label": "Сейчас на таможне",
            "value": f"{on_customs}",
            "sub": (f"в среднем {customs_avg:.0f} дн на этапе"
                    if customs_avg else "контроль брокера"),
            "tone": "warn" if on_customs > 3 else "info",
            "action": "op_queue",
            "params": {"filter": "customs"},
        })

    # 4) В транзите
    if in_transit:
        items.append({
            "label": "В пути",
            "value": f"{in_transit}",
            "sub": "транзит + приёмка",
            "tone": "info",
            "action": "op_queue",
            "params": {"filter": "transit"},
        })

    # 5) Производительность — только если есть delivered за 90д
    if avg_days is not None and delivered_n_90d:
        items.append({
            "label": "Доставлено за 90 дней",
            "value": f"{delivered_n_90d}",
            "sub": f"средний срок {avg_days:.0f} дн",
            "tone": "ok" if avg_days <= 30 else ("warn" if avg_days <= 45 else "bad"),
            "action": "op_queue",
            "params": {"filter": "delivered"},
        })

    # 6) Самый медленный этап — клик ведёт в очередь по этому статусу
    if slowest:
        stage_filter_map = {
            "transit_abroad": "transit",
            "transit_rf":     "transit",
            "issuing":        "transit",
            "customs":        "customs",
        }
        items.append({
            "label": "Самый медленный этап",
            "value": stage_lbl[slowest[0]],
            "sub": f"в среднем {slowest[1]:.1f} дн на нём",
            "tone": "warn" if slowest[1] >= 10 else "info",
            "action": "op_queue",
            "params": {"filter": stage_filter_map.get(slowest[0], "live")},
        })

    # 7) Динамика срока к прошлому месяцу — только при значимом изменении
    if abs(days_delta) >= 1:
        if days_delta < 0:
            items.append({
                "label": "Стали возить быстрее",
                "value": f"на {abs(days_delta):.0f} дн",
                "sub": f"было {lead_prev:.0f} → стало {lead_30:.0f} дн",
                "tone": "ok",
            })
        else:
            items.append({
                "label": "Стали возить медленнее",
                "value": f"на {days_delta:.0f} дн",
                "sub": f"было {lead_prev:.0f} → стало {lead_30:.0f} дн",
                "tone": "bad" if days_delta >= 5 else "warn",
            })

    # 8) Если совсем пусто (нет ни активных, ни данных) — единственная карточка
    if not items or (len(items) == 1 and live_total == 0):
        items = [{
            "label": "Логистика простаивает",
            "value": "0",
            "sub": "нет ни активных отгрузок, ни доставленных за 90 дней",
            "tone": "info",
        }]

    # ── Аналитика по модам доставки (Море / Авиа / Авто / ЖД) ──
    # По доставленным за 90 дней: количество, средний lead-time, средняя
    # стоимость логистики, средняя стоимость на $1000 груза.
    mode_stats = {}
    delivered_qs = Order.objects.filter(status__in=("delivered", "completed"),
                                          created_at__gte=cutoff).exclude(shipping_mode="")
    for o in delivered_qs[:500]:
        m = o.shipping_mode or "—"
        s = mode_stats.setdefault(m, {"n": 0, "leads": [], "costs": [], "ratios": []})
        s["n"] += 1
        ev = OrderEvent.objects.filter(order=o, event_type="status_changed",
                                         meta__to="delivered").order_by("-created_at").first()
        if ev and o.created_at:
            s["leads"].append((ev.created_at - o.created_at).total_seconds() / 86400)
        if o.logistics_cost:
            cost = float(o.logistics_cost)
            s["costs"].append(cost)
            if o.total_amount:
                s["ratios"].append(cost / float(o.total_amount) * 100)

    # Добавим активные маршруты по модам (для текущей нагрузки)
    active_by_mode = {b["shipping_mode"]: b["n"] for b in
                       Order.objects.filter(status__in=LIVE_STATUSES)
                       .exclude(shipping_mode="")
                       .values("shipping_mode").annotate(n=Count("id"))}

    mode_rows = []
    for mcode in ("sea", "air", "auto", "rail"):
        if mcode not in mode_stats and mcode not in active_by_mode:
            continue
        s = mode_stats.get(mcode, {"n": 0, "leads": [], "costs": [], "ratios": []})
        avg_lead = (sum(s["leads"]) / len(s["leads"])) if s["leads"] else None
        avg_cost = (sum(s["costs"]) / len(s["costs"])) if s["costs"] else None
        avg_ratio = (sum(s["ratios"]) / len(s["ratios"])) if s["ratios"] else None
        live_n = active_by_mode.get(mcode, 0)

        parts = []
        if avg_lead is not None:
            parts.append(f"ср. срок {avg_lead:.1f} дн")
        if avg_cost is not None:
            parts.append(f"ср. стоимость ${avg_cost:,.0f}")
        if avg_ratio is not None:
            parts.append(f"{avg_ratio:.1f}% от груза")
        if not parts:
            parts = ["нет завершённых отгрузок за 90д"]

        # Тон по скорости: bad если медленнее эталона
        norm = {"sea": 35, "air": 7, "auto": 14, "rail": 20}.get(mcode, 30)
        tone = "info"
        if avg_lead is not None:
            tone = ("ok" if avg_lead <= norm else
                    ("warn" if avg_lead <= norm * 1.3 else "bad"))

        mode_rows.append({
            "title": MODE_LABEL.get(mcode, mcode),
            "subtitle": (f"{s['n']} доставлено за 90д · {live_n} активно сейчас · "
                          + " · ".join(parts)),
            "tone": tone,
            "badge": {"label": f"{live_n} в пути", "tone": tone},
        })

    # ── Аналитика по маршрутам (origin → dest) ──
    # Берём все заказы (не только delivered) с заполненной meta — стоимость и срок.
    route_stats = {}
    route_src_qs = Order.objects.filter(created_at__gte=cutoff)[:500]
    for o in route_src_qs:
        meta = (o.logistics_meta or {}) if isinstance(o.logistics_meta, dict) else {}
        origin = (meta.get("origin") or meta.get("origin_country") or "CN")[:8]
        dest = ((meta.get("customs") or {}).get("country") or "RU")[:8]
        key = f"{origin}→{dest}"
        s = route_stats.setdefault(key, {"n": 0, "leads": [], "costs": [], "modes": {}})
        s["n"] += 1
        m = o.shipping_mode or "—"
        s["modes"][m] = s["modes"].get(m, 0) + 1
        if o.status in ("delivered", "completed"):
            ev = OrderEvent.objects.filter(order=o, event_type="status_changed",
                                             meta__to="delivered").order_by("-created_at").first()
            if ev and o.created_at:
                s["leads"].append((ev.created_at - o.created_at).total_seconds() / 86400)
        if o.logistics_cost:
            s["costs"].append(float(o.logistics_cost))

    # Топ-маршрутов по объёму (≥2 заказа)
    route_rows_analytics = []
    for key, s in sorted(route_stats.items(), key=lambda x: -x[1]["n"])[:10]:
        if s["n"] < 2:
            continue
        avg_lead = (sum(s["leads"]) / len(s["leads"])) if s["leads"] else None
        avg_cost = (sum(s["costs"]) / len(s["costs"])) if s["costs"] else None
        top_mode = max(s["modes"].items(), key=lambda x: x[1])[0] if s["modes"] else "—"
        parts = []
        if avg_lead is not None:
            parts.append(f"ср. срок {avg_lead:.1f} дн")
        if avg_cost is not None:
            parts.append(f"ср. стоимость ${avg_cost:,.0f}")
        parts.append(f"чаще всего {MODE_LABEL.get(top_mode, top_mode)}")
        route_rows_analytics.append({
            "title": key,
            "subtitle": f"{s['n']} заказов за 90д · " + " · ".join(parts),
            "tone": "info",
        })

    # ── Аналитика по перевозчикам: ср.срок, % SLA-нарушений, средняя стоимость ──
    # (а не просто «N в работе» — это уже видно в op_queue)
    provider_stats = {}
    delivered_for_carriers = Order.objects.filter(
        status__in=("delivered", "completed"), created_at__gte=cutoff,
    ).exclude(logistics_provider="")[:500]
    for o in delivered_for_carriers:
        p = o.logistics_provider or "—"
        s = provider_stats.setdefault(p, {"n": 0, "leads": [], "costs": [], "breaches": 0})
        s["n"] += 1
        ev = OrderEvent.objects.filter(order=o, event_type="status_changed",
                                         meta__to="delivered").order_by("-created_at").first()
        if ev and o.created_at:
            s["leads"].append((ev.created_at - o.created_at).total_seconds() / 86400)
        if o.logistics_cost:
            s["costs"].append(float(o.logistics_cost))
        if (o.sla_breaches_count or 0) > 0:
            s["breaches"] += 1
    # Текущая нагрузка для контекста
    active_by_carrier = {b["logistics_provider"]: b["n"] for b in by_provider}
    for p, n in active_by_carrier.items():
        provider_stats.setdefault(p or "—", {"n": 0, "leads": [], "costs": [], "breaches": 0})

    provider_rows = []
    for p, s in sorted(provider_stats.items(), key=lambda x: -(x[1]["n"] + active_by_carrier.get(x[0], 0)))[:8]:
        avg_lead = (sum(s["leads"]) / len(s["leads"])) if s["leads"] else None
        avg_cost = (sum(s["costs"]) / len(s["costs"])) if s["costs"] else None
        breach_pct = (s["breaches"] * 100 // s["n"]) if s["n"] else 0
        live_n = active_by_carrier.get(p, 0)

        parts = []
        if avg_lead is not None:
            parts.append(f"ср. срок {avg_lead:.1f} дн")
        if avg_cost is not None:
            parts.append(f"ср. стоимость ${avg_cost:,.0f}")
        if s["n"]:
            parts.append(f"SLA-нарушений {breach_pct}%")
        if not parts:
            parts = ["нет завершённых отгрузок"]

        tone = ("bad" if breach_pct >= 30 else
                ("warn" if breach_pct >= 10 else
                 ("ok" if s["n"] else "info")))
        provider_rows.append({
            "title": _carrier_label(p),
            "subtitle": (f"{s['n']} доставлено за 90д · {live_n} активно · " + " · ".join(parts)),
            "tone": tone,
            "badge": ({"label": f"{breach_pct}% SLA", "tone": tone} if s["n"] else
                      {"label": f"{live_n} в пути", "tone": "info"}),
            "action": "op_queue",
            "params": {"filter": "all", "provider": p or ""},
        })

    # ── Аналитика по этапам: средние сроки и где застревают ──
    stage_rows_analytics = []
    for st in ("transit_abroad", "customs", "transit_rf", "issuing"):
        live_in_stage = live_active.filter(status=st)
        n_live = live_in_stage.count()
        breached_in_stage = live_in_stage.filter(sla_status="breached").count()
        avg_stage = stage_avg_days.get(st)

        # Самый старый заказ на этапе (для подсказки оператору)
        oldest_days = None
        for o in live_in_stage[:50]:
            ev = OrderEvent.objects.filter(order=o, event_type="status_changed",
                                             meta__to=st).order_by("-created_at").first()
            if ev:
                d = (now - ev.created_at).days
                if oldest_days is None or d > oldest_days:
                    oldest_days = d

        parts = []
        if avg_stage is not None:
            parts.append(f"ср. срок {avg_stage:.1f} дн")
        if oldest_days is not None:
            parts.append(f"самый давний {oldest_days} дн")
        if breached_in_stage:
            parts.append(f"{breached_in_stage} SLA")
        tone = ("bad" if breached_in_stage else
                ("warn" if (avg_stage or 0) > 10 else "info"))

        stage_rows_analytics.append({
            "title": stage_lbl.get(st, st),
            "subtitle": f"{n_live} активно · " + (" · ".join(parts) if parts else "—"),
            "tone": tone,
            "badge": {"label": f"{n_live} шт", "tone": tone},
            "action": "op_queue",
            "params": {"filter": st},
        })

    # Actionable инсайт
    if sla_breached_n:
        log_insight = (f"{sla_breached_n} маршрутов уже нарушили SLA — "
                       f"откройте SLA-отчёт и переподнимите подрядчиков.")
    elif slowest and slowest[1] > 14 and slowest[0] == "customs":
        log_insight = (f"Таможня — узкое место: средние {slowest[1]:.1f} дн. "
                       f"Проверьте сертификаты и HS-коды.")
    elif slowest and slowest[1] > 20:
        log_insight = f"Самый медленный этап: {slowest_label}. Есть смысл сменить подрядчика."
    elif days_delta >= 5:
        log_insight = (f"Стали возить на {days_delta:.0f} дн дольше "
                       f"(было {lead_prev:.0f} → стало {lead_30:.0f}). Проверьте причину.")
    elif days_delta <= -3:
        log_insight = (f"Доставка ускорилась на {abs(days_delta):.0f} дн "
                       f"(было {lead_prev:.0f} → стало {lead_30:.0f}) — хорошая динамика.")
    else:
        log_insight = "Логистика в норме."

    # В отчёт идёт ТОЛЬКО аналитика. Индивидуальные заказы доступны через
    # «📋 Очередь» и карточки заказа — здесь дублировать незачем.
    cards_out = [
        {"type": "kpi_grid", "data": {"title": "🚚 Логистика — что под контролем сейчас", "items": items}},
    ]
    if mode_rows:
        cards_out.append({"type": "list", "data": {
            "title": "🚢 По модам доставки (90д)",
            "items": mode_rows,
        }})
    if route_rows_analytics:
        cards_out.append({"type": "list", "data": {
            "title": f"🗺 По маршрутам — топ {len(route_rows_analytics)} (90д)",
            "items": route_rows_analytics,
        }})
    if stage_rows_analytics:
        cards_out.append({"type": "list", "data": {
            "title": "⏱ По этапам — где задерживаются",
            "items": stage_rows_analytics,
        }})
    cards_out.append({"type": "list", "data": {
        "title": "🚛 По перевозчикам — качество и стоимость (90д)",
        "items": provider_rows or [{"title": "Нет данных"}],
    }})

    return ActionResult(
        text=log_insight,
        cards=cards_out,
        contextual_actions=[
            {"action": "op_analytics_hub", "label": "← Аналитика"},
            {"action": "op_sla_breach", "label": "⏱ SLA-нарушения"},
            {"action": "op_customs_dashboard", "label": "🛂 Таможня"},
            {"action": "op_queue", "label": "📋 Очередь", "params": {"filter": "open"}},
        ],
    )


@register("op_payments_stats")
def op_payments_stats(params, user, role):
    """Платежная аналитика: разбивка по статусам, средний чек, конверсия."""
    err = _ensure_operator(role)
    if err:
        return err
    from django.db.models import Count, Sum

    from marketplace.models import Order

    from datetime import timedelta

    from django.utils import timezone

    now = timezone.now()
    cutoff_30 = now - timedelta(days=30)
    cutoff_60 = now - timedelta(days=60)

    by_status = list(Order.objects.values("payment_status").annotate(
        n=Count("id"), total=Sum("total_amount")).order_by("-n"))
    total_orders = sum(b["n"] for b in by_status)
    paid_orders = sum(b["n"] for b in by_status if b["payment_status"] in ("paid", "refunded"))
    refunded = next((b for b in by_status if b["payment_status"] == "refunded"), {"n": 0, "total": 0})
    pending = sum(b["n"] for b in by_status if b["payment_status"] in ("pending", "partial"))

    paid_total = sum(b["total"] or 0 for b in by_status if b["payment_status"] == "paid")
    refunded_total = refunded["total"] or 0
    avg_check = (paid_total / paid_orders) if paid_orders else 0
    refund_rate = (refunded["n"] / total_orders * 100) if total_orders else 0
    payment_conv = (paid_orders * 100 // total_orders) if total_orders else 0

    # Конверсия резерв → финал за 30д
    orders_30 = Order.objects.filter(created_at__gte=cutoff_30)
    reserved_30 = orders_30.filter(payment_status__in=("partial", "paid")).count()
    paid_30 = orders_30.filter(payment_status="paid").count()
    final_conv = (paid_30 * 100 // reserved_30) if reserved_30 else 0

    # Средний срок оплаты (paid_at - created_at) — по последним 30д оплаченным
    paid_orders_30 = list(Order.objects.filter(
        payment_status="paid", final_paid_at__isnull=False, created_at__gte=cutoff_30,
    ).values_list("created_at", "final_paid_at"))
    if paid_orders_30:
        avg_pay_hours = sum((p - c).total_seconds() for c, p in paid_orders_30) / len(paid_orders_30) / 3600
    else:
        avg_pay_hours = None

    # Тренд оплат 30д vs 30д
    paid_total_30 = float(orders_30.filter(payment_status="paid")
                          .aggregate(s=Sum("total_amount"))["s"] or 0)
    paid_total_prev = float(Order.objects.filter(
        payment_status="paid", created_at__gte=cutoff_60, created_at__lt=cutoff_30,
    ).aggregate(s=Sum("total_amount"))["s"] or 0)
    if paid_total_prev:
        trend_pct = int((paid_total_30 - paid_total_prev) * 100 / paid_total_prev)
    elif paid_total_30:
        trend_pct = 100
    else:
        trend_pct = 0
    arrow = "↑" if trend_pct > 0 else ("↓" if trend_pct < 0 else "→")

    items = [
        {"label": "Конверсия в оплату",
         "value": f"{payment_conv}%",
         "tone": "ok" if payment_conv >= 80 else ("warn" if payment_conv >= 60 else "bad")},
        {"label": "Резерв → финал (30д)",
         "value": f"{final_conv}%",
         "tone": "ok" if final_conv >= 70 else "warn"},
        {"label": "Средний чек",
         "value": f"${avg_check:,.0f}", "tone": "info"},
        {"label": "Refund rate",
         "value": f"{refund_rate:.1f}%",
         "tone": "bad" if refund_rate > 5 else ("warn" if refund_rate > 2 else "ok")},
        {"label": "Тренд выручки 30д",
         "value": f"{arrow} {abs(trend_pct)}%",
         "tone": "bad" if trend_pct < -15 else ("ok" if trend_pct > 0 else "warn")},
    ]
    if avg_pay_hours is not None:
        items.append({
            "label": "Средний срок оплаты",
            "value": (f"{avg_pay_hours:.0f} ч" if avg_pay_hours < 48 else f"{avg_pay_hours/24:.1f} дн"),
            "tone": "ok" if avg_pay_hours <= 24 else ("warn" if avg_pay_hours <= 72 else "bad"),
        })
    if pending:
        items.append({
            "label": "Сумма к получению",
            "value": f"${sum(b['total'] or 0 for b in by_status if b['payment_status'] in ('pending','partial')):,.0f}",
            "tone": "warn",
        })

    payment_labels = {k: str(v) for k, v in Order.PAYMENT_STATUS_CHOICES}
    rows = [
        {"title": payment_labels.get(b["payment_status"], b["payment_status"]),
         "subtitle": f"{b['n']} заказов · ${(b['total'] or 0):,.0f}"}
        for b in by_status
    ]

    # Текст — actionable инсайт
    if refund_rate > 5:
        insight = f"Высокий refund rate {refund_rate:.1f}% — проверьте качество и поставщиков."
    elif final_conv < 50 and reserved_30:
        insight = (f"Только {final_conv}% резервов доходят до финальной оплаты — "
                   f"много залипших на partial. Свяжитесь с покупателями.")
    elif avg_pay_hours and avg_pay_hours > 72:
        insight = (f"Средний срок оплаты {avg_pay_hours/24:.1f} дн — выше нормы 3д. "
                   f"Стоит подключить напоминания.")
    elif trend_pct < -15:
        insight = f"Выручка за 30д упала на {abs(trend_pct)}% — посмотрите воронку RFQ→Order."
    else:
        insight = "Платёжная воронка в норме."

    return ActionResult(
        text=insight,
        cards=[
            {"type": "kpi_grid", "data": {"title": "💳 Качество платёжной воронки", "items": items}},
            {"type": "list", "data": {"title": "По статусам оплаты", "items": rows}},
        ],
        contextual_actions=[
            {"action": "op_analytics_hub", "label": "← Аналитика"},
            {"action": "op_payments_dashboard", "label": "💰 Эскроу"},
            {"action": "op_queue", "label": "💸 Возвраты", "params": {"filter": "refund"}},
        ],
    )


@register("op_analytics_hub")
def op_analytics_hub(params, user, role):
    """Хаб аналитических отчётов оператора — 10 ключевых разделов.
    Каждый клик ведёт в отдельный pure-DB отчёт.
    """
    err = _ensure_operator(role)
    if err:
        return err
    from datetime import timedelta

    from django.db.models import Avg, Sum
    from django.utils import timezone

    from marketplace.models import Order, OrderClaim, RFQ

    now = timezone.now()
    cutoff_30 = now - timedelta(days=30)
    cutoff_60 = now - timedelta(days=60)

    n_orders = Order.objects.count()
    gmv      = float(Order.objects.aggregate(s=Sum("total_amount"))["s"] or 0)
    active   = Order.objects.exclude(status__in=("delivered", "completed", "cancelled")).count()
    rfq_open = RFQ.objects.exclude(status__in=("closed", "cancelled")).count()
    claims   = OrderClaim.objects.filter(status__in=("open", "in_review")).count()

    # ── Аналитика: уникальные показатели, не дублирующие список отчётов ──
    gmv_30 = float(Order.objects.filter(created_at__gte=cutoff_30)
                   .aggregate(s=Sum("total_amount"))["s"] or 0)
    gmv_prev = float(Order.objects.filter(created_at__gte=cutoff_60,
                                             created_at__lt=cutoff_30)
                     .aggregate(s=Sum("total_amount"))["s"] or 0)
    if gmv_prev:
        gmv_trend_pct = int((gmv_30 - gmv_prev) * 100 / gmv_prev)
    elif gmv_30:
        gmv_trend_pct = 100
    else:
        gmv_trend_pct = 0
    avg_check = (gmv / n_orders) if n_orders else 0
    orders_30 = Order.objects.filter(created_at__gte=cutoff_30).count()
    delivered_30 = Order.objects.filter(status__in=("delivered", "completed"),
                                          created_at__gte=cutoff_30).count()
    conversion = (delivered_30 * 100 // orders_30) if orders_30 else 0
    # Доля заказов в работе от общего — здоровье pipeline
    in_work_share = (active * 100 // n_orders) if n_orders else 0
    # claim rate — рекламации к доставленным за 30д (требуем ≥5 доставок для статистики)
    claims_30 = OrderClaim.objects.filter(created_at__gte=cutoff_30).count()
    claim_rate = (claims_30 * 100 // delivered_30) if delivered_30 >= 5 else None

    reports = [
        {"title": "📊 Заказы и GMV", "action": "get_analytics", "params": {},
         "subtitle": "Динамика по месяцам · средний чек · распределение по статусам"},
        {"title": "💰 Финансы и эскроу", "action": "op_payments_dashboard", "params": {},
         "subtitle": "Деньги на платформе · поток за всё время · удержания и выплаты"},
        {"title": "⏱ SLA и узкие места", "action": "op_sla_breach", "params": {},
         "subtitle": "Нарушения · риски · где застревают заказы"},
        {"title": "🚚 Логистика", "action": "op_logistics_stats", "params": {},
         "subtitle": "Активные маршруты · перевозчики · среднее время доставки"},
        {"title": "📈 Спрос на рынке", "action": "get_demand_report", "params": {},
         "subtitle": "Топ-бренды · топ-OEM · динамика RFQ по неделям"},
        {"title": "📋 Очередь RFQ по режимам", "action": "op_rfq_queue", "params": {},
         "subtitle": "AUTO · SEMI (15 мин) · MANUAL (48 ч) — конверсия и SLA"},
        {"title": "🧾 Рекламации", "action": "get_claims", "params": {},
         "subtitle": "Активные · по причинам · среднее время разбора"},
        {"title": "🛡 KYB очередь", "action": "op_kyb_queue", "params": {},
         "subtitle": "Кандидаты на верификацию · риск-индикаторы · решения"},
        {"title": "🛂 Таможня", "action": "op_customs_dashboard", "params": {},
         "subtitle": "Грузы на оформлении · HS-коды · пошлины · просрочки"},
        {"title": "📑 Платёжная статистика", "action": "op_payments_stats", "params": {},
         "subtitle": "Конверсия резерв → финал · средние сроки оплат"},
    ]

    # Текст — конкретные приоритеты для оператора (не CEO-уровень).

    # ── Только то, на что оператор может прямо сейчас повлиять ──
    # Executive-метрики (GMV, рост) — в отдельный «Заказы и GMV» отчёт ниже.
    # Здесь только operational signals: что в работе, что просрочено, что
    # требует разбора.
    sla_breached = Order.objects.filter(
        sla_status="breached",
    ).exclude(status__in=("delivered", "completed", "cancelled")).count()
    sla_at_risk = Order.objects.filter(
        sla_status="at_risk",
    ).exclude(status__in=("delivered", "completed", "cancelled")).count()
    on_customs = Order.objects.filter(status="customs").count()
    open_claims = OrderClaim.objects.filter(status__in=("open", "in_review")).count()
    # KYB pending — если модель доступна
    kyb_pending = 0
    try:
        from marketplace.models import CompanyVerification
        kyb_pending = CompanyVerification.objects.filter(status="pending").count()
    except Exception:
        pass
    new_7d = Order.objects.filter(created_at__gte=now - timedelta(days=7)).count()

    health_items = []
    # 1) Срочно: SLA — самая важная ячейка, операторская работа = не допустить срыв
    if sla_breached or sla_at_risk:
        risk_total = sla_breached + sla_at_risk
        sub_parts = []
        if sla_breached: sub_parts.append(f"{sla_breached} просрочено")
        if sla_at_risk:  sub_parts.append(f"{sla_at_risk} под угрозой")
        health_items.append({
            "label": "Срочно: SLA",
            "value": str(risk_total),
            "sub": " · ".join(sub_parts),
            "tone": "bad" if sla_breached else "warn",
            "action": "op_sla_breach", "params": {}})

    # 2) Активная нагрузка — всегда показываем
    health_items.append({
        "label": "В работе сейчас",
        "value": str(active),
        "sub": f"{new_7d} новых за последние 7 дней",
        "tone": "info",
        "action": "op_queue", "params": {"filter": "open"}})

    # 3) Таможня — главное узкое место в трансгран логистике
    if on_customs:
        health_items.append({
            "label": "На таможне",
            "value": str(on_customs),
            "sub": "требуют контроля брокера",
            "tone": "warn" if on_customs >= 3 else "info",
            "action": "op_queue", "params": {"filter": "customs"}})

    # 4) Рекламации в работе — операторский фронт
    if open_claims:
        health_items.append({
            "label": "Рекламации в разборе",
            "value": str(open_claims),
            "sub": "open + in_review",
            "tone": "warn" if open_claims >= 5 else "info",
            "action": "get_claims", "params": {"status": "in_review"}})

    # 5) KYB-очередь — оператор одобряет новых поставщиков
    if kyb_pending:
        health_items.append({
            "label": "KYB на проверке",
            "value": str(kyb_pending),
            "sub": "анкеты поставщиков ждут решения",
            "tone": "warn" if kyb_pending >= 5 else "info",
            "action": "op_kyb_queue", "params": {}})

    # Если ничего срочного — лаконичная зелёная ячейка
    if not health_items or (len(health_items) == 1 and active == 0):
        health_items = [{
            "label": "Платформа в покое",
            "value": "0",
            "sub": "нет активных заказов, SLA-нарушений и открытых рекламаций",
            "tone": "ok",
        }]

    # Operational insight — приоритизируем для оператора (не CEO).
    if sla_breached:
        insight = (f"{sla_breached} заказов уже нарушили SLA. Это первая задача — "
                   f"откройте «Срочно: SLA» и эскалируйте.")
    elif sla_at_risk >= 3:
        insight = (f"{sla_at_risk} заказов под угрозой SLA. Свяжитесь с подрядчиками "
                   f"чтобы успели — пока не дошло до нарушения.")
    elif open_claims >= 5:
        insight = (f"{open_claims} рекламаций в разборе. Разгребите очередь, чтобы "
                   f"не копились.")
    elif kyb_pending >= 5:
        insight = (f"{kyb_pending} KYB-анкет ждут решения. Откройте очередь — "
                   f"поставщики простаивают без верификации.")
    elif on_customs >= 5:
        insight = (f"{on_customs} грузов на таможне — проверьте, не застряли ли "
                   f"какие-то дольше 3 дней.")
    elif active == 0:
        insight = "Платформа в покое — активной работы нет."
    else:
        insight = f"{active} заказов в работе. Срочных проблем нет."

    return ActionResult(
        text=insight,
        cards=[
            {"type": "kpi_grid", "data": {
                "title": "📊 Что требует внимания сейчас",
                "items": health_items,
            }},
            {"type": "list", "data": {
                "title": "📈 Доступные отчёты — кликните для деталей",
                "items": reports,
            }},
        ],
        contextual_actions=[
            {"action": "op_dashboard", "label": "← Главный дашборд"},
        ],
    )


@register("op_my_suppliers")
def op_my_suppliers(params, user, role):
    """PIVOT 2026-05-27: список поставщиков закреплённых за этим оператором.

    Capacity-индикатор N/25, статусы (trusted/sandbox/risky), активные сделки
    по каждому.
    """
    err = _ensure_operator(role)
    if err:
        return err
    from marketplace.models import UserProfile, Order
    MAX = 25
    profiles = list(UserProfile.objects.filter(assigned_operator=user)
                     .select_related("user").order_by("supplier_status", "user__username"))
    rows = []
    _STATUS_RU = {"trusted": "Надёжный", "sandbox": "Песочница",
                  "risky": "Рисковый", "rejected": "Исключён"}
    for p in profiles:
        # активные sub-orders по этому поставщику
        active_n = Order.objects.filter(
            assigned_operator=user,
            items__part__seller=p.user,
        ).exclude(status__in=("delivered", "completed", "cancelled")).distinct().count()
        rows.append({
            "title": f"{p.user.username}",
            "subtitle": (f"{_STATUS_RU.get(p.supplier_status, p.supplier_status or '—')} · "
                          f"рейтинг {float(p.rating_score or 0):.1f} · {active_n} активных сделок"),
            "tone": {"trusted": "ok", "sandbox": "warn",
                     "risky": "bad", "rejected": "bad"}.get(p.supplier_status),
            "action": "contact_supplier",
            "params": {"seller_id": p.user_id, "seller_username": p.user.username},
        })
    cap = len(profiles)
    capacity_label = f"{cap} / {MAX}"
    title = f"Мои поставщики · {capacity_label}"
    if cap >= MAX:
        title += " · ЛИМИТ"
    return ActionResult(
        text=(
            f"Закреплено {cap} из {MAX} поставщиков. "
            + ("Свободных слотов нет — освободите место перед KYB-approve нового." if cap >= MAX
                else f"Свободно {MAX - cap} слотов.")
        ),
        cards=[{"type": "list", "data": {
            "title": title,
            "items": rows or [{"title": "Поставщиков нет",
                                "subtitle": "Одобрите KYB-анкеты — они закрепятся за вами автоматически"}],
        }}],
        actions=[
            {"label": "Очередь KYB", "action": "op_kyb_queue", "params": {}},
        ],
        contextual_actions=[
            {"action": "op_dashboard", "label": "← Дашборд"},
        ],
    )


@register("op_my_bonuses")
def op_my_bonuses(params, user, role):
    """Список бонусов оператора с фильтрами: pending / released_30d / all."""
    err = _ensure_operator(role)
    if err:
        return err
    from datetime import timedelta
    from django.utils import timezone
    from marketplace.models import OperatorBonusLine
    flt = (params or {}).get("filter", "all")
    qs = OperatorBonusLine.objects.filter(operator=user).select_related("order")
    if flt == "pending":
        qs = qs.filter(status="pending")
        title = f"💼 Бонусы в холде (14 дней)"
    elif flt == "released_30d":
        cutoff = timezone.now() - timedelta(days=30)
        qs = qs.filter(status="released", released_at__gte=cutoff)
        title = f"💼 Зачислено за 30 дней"
    else:
        title = f"💼 Все мои бонусы (life-time)"
    rows = []
    total = 0.0
    for l in qs.order_by("-created_at")[:50]:
        total += float(l.amount or 0)
        status_lbl = {"pending": "в холде", "released": "зачислено",
                       "withheld": "удержано", "reduced": "−50% вина"}.get(l.status, l.status)
        rows.append({
            "title": f"#{l.order_id} · {l.basis} {l.rate_pct}% · {status_lbl}",
            "subtitle": f"+${float(l.amount):,.2f} · база ${float(l.base_amount):,.0f} · {l.created_at:%d.%m.%Y}",
            "action": "op_order_detail",
            "params": {"order_id": l.order_id},
        })
    return ActionResult(
        text=f"{title} · итого ${total:,.2f}",
        cards=[{"type": "list", "data": {
            "title": title,
            "items": rows or [{"title": "Бонусов нет",
                                "subtitle": "По выбранному фильтру пока пусто"}],
        }}],
        contextual_actions=[
            {"action": "op_payments_dashboard", "label": "← Финансы"},
        ],
    )


@register("op_payments_dashboard")
def op_payments_dashboard(params, user, role):
    err = _ensure_operator(role)
    if err:
        return err
    from marketplace.models import Order

    from . import payments as _pay

    from datetime import timedelta

    from django.db.models import Sum
    from django.utils import timezone

    s = _pay.escrow_summary()
    holds = s.get("open_holds", {})

    # ── Реально активные эскроу: заказы которые ещё «в работе» (не paid/cancelled)
    # Считаем из Order — более правдивая картина чем из WalletTx (там может
    # остаться мусор при desync release_escrow).
    now = timezone.now()
    _orders_q = Order.objects.filter(
        id__in=list(holds.keys()),
    ).exclude(
        payment_status__in=("paid", "refunded"),
    ).exclude(status__in=("delivered", "completed", "cancelled"))
    # PIVOT 2026-05-27: фильтр по ownership (не для лидов)
    if not user.is_staff:
        _orders_q = _orders_q.filter(assigned_operator=user)
    active_orders = list(_orders_q)
    # Реальный outstanding = сумма total_amount активных заказов
    real_outstanding = sum(float(o.total_amount or 0) for o in active_orders)
    # Если активных нет, fall back на accounting-сумму из WalletTx
    if not active_orders:
        real_outstanding = s["outstanding_balance"]
    s["outstanding_balance"] = real_outstanding

    # ── Аналитика эскроу ──
    avg_hold = (real_outstanding / len(active_orders)) if active_orders else 0
    ages = [(now - o.created_at).days for o in active_orders if o.created_at]
    avg_hold_age = (sum(ages) / len(ages)) if ages else 0

    # Refund ratio: возвраты / принято всего
    refund_share = (s["total_refunded_ever"] / s["total_held_ever"] * 100) if s["total_held_ever"] else 0
    # Payout efficiency: выплачено / (принято − возвращено − на платформе)
    if s["total_held_ever"]:
        cycle_done = s["total_held_ever"] - s["outstanding_balance"]
        payout_pct = (s["total_released_ever"] / cycle_done * 100) if cycle_done else 0
    else:
        payout_pct = 0

    # 30-day flow
    cutoff_30 = now - timedelta(days=30)
    inflow_30 = float(Order.objects.filter(
        final_paid_at__gte=cutoff_30, payment_status="paid",
    ).aggregate(s=Sum("total_amount"))["s"] or 0)

    # ── Action-oriented метрики: что требует внимания оператора ──
    # 1. Старые холды (>30 дней без движения) — могут «съесть» оборотку
    stuck_count = sum(1 for o in active_orders if o.created_at and (now - o.created_at).days > 30)
    stuck_amount = sum(holds.get(o.id, 0) for o in active_orders
                        if o.created_at and (now - o.created_at).days > 30)
    # 2. Готовы к выплате продавцу (delivered/completed но не released)
    ready_payouts_qs = Order.objects.filter(
        status__in=("delivered", "completed"),
        payment_status="paid",
    ).exclude(id__in=list(holds.keys()))[:0]  # placeholder — для будущей логики
    # 3. Возвраты в обработке
    pending_refunds_qs = Order.objects.filter(payment_status="refund_pending")
    pending_refunds_count = pending_refunds_qs.count()
    pending_refunds_amount = float(pending_refunds_qs.aggregate(
        s=Sum("total_amount"))["s"] or 0)
    # 4. Рекламации за 30 дней
    from marketplace.models import OrderClaim as _OC
    claims_30 = _OC.objects.filter(created_at__gte=cutoff_30).count()

    rows = []
    # Защита от data inconsistency: исключаем заказы, по которым деньги уже
    # ушли продавцу (payment_status="paid" / delivered) — там escrow должен
    # быть закрыт. Если WalletTx не синхронизирован — это бухгалтерская
    # ошибка, но в списке «активных» им не место.
    for oid, amt in sorted(holds.items(), key=lambda x: -x[1])[:30]:
        try:
            o = Order.objects.get(id=oid)
            if o.payment_status in ("paid", "refunded") or o.status in ("delivered", "completed", "cancelled"):
                continue  # деньги уже не в работе — скрываем из списка активных
            rows.append({
                "title": f"#{oid} · {o.customer_name}",
                "subtitle": f"💰 ${amt:,.2f} лежит на платформе · {o.get_status_display()} · {o.get_payment_status_display()}",
                "action": "op_order_detail",
                "params": {"order_id": oid},
            })
            if len(rows) >= 10:
                break
        except Order.DoesNotExist:
            continue

    # Финансовая модель — полные условия по клиенту, оплатам и поставщику.
    # Без markdown в bubble — всё в list-карточках.

    # ── Что платит клиент ──
    pricing_items = [
        {"title": "FOB",
         "subtitle": "Товар + 10% сервисный сбор платформы. Фрахт + страховка + таможня — за счёт клиента."},
        {"title": "CIP",
         "subtitle": "Товар + 14% сервис + фактический фрахт по тарифу платформы. Платформа везёт до согласованного порта/терминала, страхует груз."},
        {"title": "DDP",
         "subtitle": "Товар + 18% сервис + фрахт + $300 фикс на таможенное оформление. Под ключ до двери получателя, все пошлины внутри цены."},
        {"title": "+ 2% агент РФ",
         "subtitle": "Дополнительно если оплата в рублях (валютный контроль, агентский договор). Внутри логистической строки скрыто ~25% маржи группы — это инструмент для рукоятки операционных расходов."},
    ]

    # ── Условия оплаты от клиента ──
    payment_terms_items = [
        {"title": "10% — резерв (предоплата)",
         "subtitle": "Клиент вносит после подтверждения КП. Запускает производство/закупку. При отказе клиента — невозвратный."},
        {"title": "80% — основной платёж",
         "subtitle": "Перед отгрузкой со склада поставщика. Без оплаты груз не выходит в путь."},
        {"title": "10% — финальный платёж",
         "subtitle": "При приёмке клиентом. После закрытия — деньги уходят на эскроу-распределение."},
        {"title": "Эскроу-холд",
         "subtitle": "Все 100% удерживаются на платформе до факта приёмки. Только потом распределяются: поставщику / в депозит / возврат."},
    ]

    # ── Что получает поставщик и как удерживается депозит ──
    seller_payout = [
        {"title": "При отгрузке: FOB-цена − 10% депозита",
         "subtitle": "Платформа выплачивает 90% FOB-стоимости товара. 10% удерживается как гарантия за SLA, качество и приёмку."},
        {"title": "Через 14 дней после приёмки клиентом: +5% возврата",
         "subtitle": "Половина депозита возвращается поставщику. Условие — нет открытых рекламаций и претензий по качеству."},
        {"title": "Финальное удержание: 5% остаётся платформе",
         "subtitle": "Гарантийный сбор за контроль SLA, качества и поддержку. Не возвращается."},
        {"title": "Полный возврат депозита (10%) — если",
         "subtitle": "Поставщик весь год работал без рекламаций и SLA-нарушений. Платформа возвращает накопленный депозит по итогам года."},
        {"title": "Удержание депозита (списание 5–10%)",
         "subtitle": "При подтверждённой рекламации по вине поставщика — депозит закрывает ущерб клиенту."},
    ]

    tier_items = [
        {"title": "Standard",  "subtitle": "оборот до 100M ₽ · стандартный тариф"},
        {"title": "Silver",    "subtitle": "100–200M ₽ · −1 п.п. от сервисного сбора"},
        {"title": "Gold",      "subtitle": "200–400M ₽ · −2 п.п."},
        {"title": "Platinum",  "subtitle": "400M+ ₽ · индивидуальные условия + персональный менеджер"},
    ]

    # ── Раздел правил — разворачивающийся блок (faq-card) со структурными rows.
    # Каждый раздел — нативный <details>, внутри — title/subtitle строки с
    # левой акцентной линией (см. .faq-row).
    rules_faq = {
        "type": "faq",
        "data": {
            "title": "📋 Правила и тарифы платформы (раскрыть)",
            "items": [
                {"q":    "Что платит клиент (по базису поставки)",
                 "rows": pricing_items},
                {"q":    "Условия оплаты клиентом (10 / 80 / 10)",
                 "rows": payment_terms_items},
                {"q":    "Что получает поставщик и как работает депозит",
                 "rows": seller_payout},
                {"q":    "🏆 Тарифные уровни (скидка по обороту)",
                 "rows": tier_items},
            ],
        },
    }

    # Actionable инсайт
    if refund_share > 5:
        esc_insight = (f"Возвраты съели {refund_share:.1f}% от принятого — "
                       f"высокий уровень. Проверьте качество и SLA.")
    elif avg_hold_age > 30:
        esc_insight = (f"Средний возраст активного холда {avg_hold_age:.0f} дн — "
                       f"много залипших заказов. Откройте топ-холды.")
    elif s['platform_balance'] != s['outstanding_balance']:
        diff = s['platform_balance'] - s['outstanding_balance']
        esc_insight = (f"⚠️ Расхождение баланс vs холды: ${diff:,.0f}. "
                       f"Нужна сверка.")
    elif payout_pct < 80 and s['total_held_ever']:
        esc_insight = (f"Только {payout_pct:.0f}% завершённого цикла дошло до продавцов — "
                       f"проверьте задержки выплат.")
    else:
        esc_insight = "Эскроу-поток в норме."

    # Сверка — показываем тайл ТОЛЬКО при расхождении (когда есть проблема).
    # «✓ всё сходится» как отдельный тайл не несёт пользы и выглядит колхозно.
    reconciliation_ok = s['platform_balance'] == s['outstanding_balance']
    reconciliation_diff = abs(s['platform_balance'] - s['outstanding_balance'])
    # Если есть расхождение — добавим в текст-инсайт сверху приоритетно
    if not reconciliation_ok:
        esc_insight = (f"⚠️ Расхождение с банковским балансом: ${reconciliation_diff:,.0f}. "
                       "Откройте бухгалтерскую сверку.")

    # ── Тайлы «Требует действия» (action-oriented, главная информация) ──
    action_tiles = []
    if not reconciliation_ok:
        action_tiles.append({
            "label": "Расхождение с банком",
            "value": f"⚠ ${reconciliation_diff:,.0f}",
            "sub": "срочная сверка",
            "tone": "bad",
            "action": "op_payments_reconciliation",
            "params": {},
        })
    if stuck_count > 0:
        action_tiles.append({
            "label": "Заказы застряли >30 дней",
            "value": str(stuck_count),
            "sub": f"${stuck_amount:,.0f} зависло · нужно разобрать",
            "tone": "bad" if stuck_count >= 5 else "warn",
            "action": "op_sla_breach",
            "params": {},
        })
    if pending_refunds_count > 0:
        action_tiles.append({
            "label": "Возвраты в обработке",
            "value": str(pending_refunds_count),
            "sub": f"${pending_refunds_amount:,.0f} к возврату",
            "tone": "warn",
            "action": "op_payments_refunds",
            "params": {},
        })
    if claims_30 > 0:
        action_tiles.append({
            "label": "Рекламации за 30 дней",
            "value": str(claims_30),
            "sub": "к разбору",
            "tone": "warn" if claims_30 < 5 else "bad",
            "action": "get_claims",
            "params": {},
        })

    # ── Health-метрики (для понимания общей картины) ──
    main_tiles = [
        {"label": "Удерживается до выплаты продавцам",
         "value": f"${s['outstanding_balance']:,.0f}",
         "sub": f"{len(active_orders)} {'заказ' if len(active_orders)==1 else 'заказов' if len(active_orders) >= 5 else 'заказа'} в эскроу"},
        {"label": "Средняя сумма заказа",
         "value": f"${avg_hold:,.0f}",
         "sub": "по активным эскроу",
         "tone": "info"},
        {"label": "Средний возраст",
         "value": f"{avg_hold_age:.0f} дн",
         "sub": ("ок" if avg_hold_age <= 30 else "застревают" if avg_hold_age <= 45 else "много старых"),
         "tone": "bad" if avg_hold_age > 45 else ("warn" if avg_hold_age > 30 else "ok")},
    ]

    # Выплачено / возвраты за 30 дней — нужны для hero и для нижней карточки
    payouts_30 = float(Order.objects.filter(
        final_paid_at__gte=cutoff_30, payment_status="paid",
        status__in=("delivered", "completed"),
    ).aggregate(s=Sum("total_amount"))["s"] or 0)
    refunded_30 = float(Order.objects.filter(
        payment_status="refunded",
        created_at__gte=cutoff_30,
    ).aggregate(s=Sum("total_amount"))["s"] or 0)

    # ── Hero «спидометр»: общий статус платформы одной плашкой ──
    # CRITICAL = есть рассогласование с банком, или ≥5 застрявших, или ≥10 рекламаций
    # WARNING  = есть любой action-tile
    # OK       = всё чисто
    is_critical = (not reconciliation_ok) or stuck_count >= 5 or claims_30 >= 10
    has_warnings = bool(action_tiles)
    if is_critical:
        status_kind, status_emoji, status_label = "critical", "🔴", "Критично"
    elif has_warnings:
        status_kind, status_emoji, status_label = "warning", "🟡", "Внимание"
    else:
        status_kind, status_emoji, status_label = "ok", "🟢", "Норма"
    # Краткое summary что именно — для одной строки под индикатором
    summary_bits = []
    if stuck_count:           summary_bits.append(f"{stuck_count} застряли")
    if pending_refunds_count: summary_bits.append(f"{pending_refunds_count} возвратов")
    if claims_30:             summary_bits.append(f"{claims_30} рекламаций")
    if not reconciliation_ok: summary_bits.append(f"Δ ${reconciliation_diff:,.0f} с банком")
    status_summary = " · ".join(summary_bits) if summary_bits else "Все процессы в норме"

    # ── Минималистичный дашборд: только важное для оператора ──
    # Считаем дополнительные метрики для информативных тайлов
    orders_30 = Order.objects.filter(created_at__gte=cutoff_30).count()
    paid_orders_30 = Order.objects.filter(
        final_paid_at__gte=cutoff_30, payment_status="paid",
        status__in=("delivered", "completed"),
    ).count()
    # Тренд оборота: 30д vs предыдущие 30д
    cutoff_60 = now - timedelta(days=60)
    inflow_prev_30 = float(Order.objects.filter(
        final_paid_at__gte=cutoff_60, final_paid_at__lt=cutoff_30,
        payment_status="paid",
    ).aggregate(s=Sum("total_amount"))["s"] or 0)
    # Тренд показываем только если есть осмысленная база (≥$10K) и не слишком
    # большой — иначе вместо «нет данных» получаем «↑ 2 207 552%» от копейки.
    if inflow_prev_30 >= 10_000 and inflow_30 >= 1_000:
        trend_pct = int(round((inflow_30 - inflow_prev_30) * 100 / inflow_prev_30))
        # Клемпуем до ±999% — больше уже информационный шум
        if abs(trend_pct) > 999:
            trend_pct = 999 if trend_pct > 0 else -999
        trend_arrow = "↑" if trend_pct >= 0 else "↓"
        trend_str = f"{trend_arrow} {abs(trend_pct)}% к прошлому"
    else:
        trend_str = f"{orders_30} {'заказ' if orders_30 == 1 else 'заказов'}"
    # Доля выплат от оборота (cash conversion)
    payout_share = int(round(payouts_30 * 100 / inflow_30)) if inflow_30 > 0 else 0
    # Самый старый застрявший заказ
    oldest_age = max(
        ((now - o.created_at).days for o in active_orders
         if o.created_at and (now - o.created_at).days > 30),
        default=0,
    )

    op_stats = [
        {"label": "Оборот за 30 дней",
         "value": f"${inflow_30:,.0f}",
         "sub":   trend_str,
         "action": "get_orders", "params": {"status": "paid"}},
        {"label": "Выплачено продавцам",
         "value": f"${payouts_30:,.0f}",
         "sub":   f"{payout_share}% от оборота · {paid_orders_30} заказов",
         "tone":  "ok",
         "action": "get_orders", "params": {"status": "delivered"}},
    ]
    # Третий тайл: проблема (если есть) или нейтральная статистика
    if stuck_count > 0:
        op_stats.append({
            "label": "Застряли >30 дней",
            "value": str(stuck_count),
            "sub":   f"${stuck_amount:,.0f} · самый старый {oldest_age}д",
            "tone":  "bad",
            "action": "op_sla_breach", "params": {},
        })
    elif refund_share > 0:
        op_stats.append({
            "label": "Возвраты",
            "value": f"${refunded_30:,.0f}",
            "sub":   f"{refund_share:.1f}% от оборота",
            "tone":  "bad" if refund_share > 5 else ("warn" if refund_share > 2 else None),
            "action": "op_queue", "params": {"filter": "refund"},
        })
    else:
        op_stats.append({
            "label": "Возвраты",
            "value": "$0",
            "sub":   "нет за 30 дней"})
    # Четвёртый тайл — рекламации (если есть за 30д)
    if claims_30 > 0:
        op_stats.append({
            "label": "Рекламации",
            "value": str(claims_30),
            "sub":   f"открыто за 30 дней",
            "tone":  "warn" if claims_30 < 5 else "bad",
            "action": "get_claims", "params": {},
        })

    # Формируем строки активных заказов в простом формате (left/title/amount)
    simple_rows = []
    for r in rows:
        # r.title уже как "#18 · Demo Seller"
        parts = (r.get("title") or "").split(" · ", 1)
        oid_part = parts[0]
        name_part = parts[1] if len(parts) > 1 else ""
        # Берём сумму из subtitle "💰 $X лежит ..."
        import re as _re
        m = _re.search(r"\$([\d,\.]+)", r.get("subtitle") or "")
        amount = f"${m.group(1)}" if m else ""
        # Краткий статус из subtitle (после второго · если есть)
        sub_parts = (r.get("subtitle") or "").split("·")
        stage = sub_parts[-1].strip() if len(sub_parts) >= 2 else ""
        simple_rows.append({
            "left":  oid_part,
            "title": f"{name_part}  ·  {stage}".strip(" ·"),
            "amount": amount,
            "action": r.get("action"),
            "params": r.get("params"),
        })

    # ── Личный бонус оператора (если есть данные) ──
    from marketplace.models import OperatorBonusLine as _OBL
    from .models import Wallet as _Wallet
    my_wallet = _Wallet.for_user(user)
    my_pending = float(_OBL.objects.filter(
        operator=user, status="pending",
    ).aggregate(s=Sum("amount"))["s"] or 0)
    my_30d = float(_OBL.objects.filter(
        operator=user, status="released",
        released_at__gte=cutoff_30,
    ).aggregate(s=Sum("amount"))["s"] or 0)
    my_lifetime = float(_OBL.objects.filter(
        operator=user, status="released",
    ).aggregate(s=Sum("amount"))["s"] or 0)
    my_closed_30 = _OBL.objects.filter(
        operator=user, created_at__gte=cutoff_30,
    ).count()

    # Готовим details_rows для inline-раскрытия каждого тайла
    def _bonus_rows(qs):
        out = []
        for l in qs.select_related("order").order_by("-created_at")[:30]:
            status_lbl = {"pending": "в холде", "released": "✓ зачислено",
                           "withheld": "удержано", "reduced": "−50% вина"}.get(l.status, l.status)
            out.append({
                "left":   f"#{l.order_id}",
                "title":  f"{l.basis} {l.rate_pct}% · {status_lbl}",
                "amount": f"+${float(l.amount):,.0f}",
                "action": "op_order_detail",
                "params": {"order_id": l.order_id},
            })
        return out

    pending_rows  = _bonus_rows(_OBL.objects.filter(operator=user, status="pending"))
    released_rows = _bonus_rows(_OBL.objects.filter(
        operator=user, status="released", released_at__gte=cutoff_30,
    ))
    all_rows      = _bonus_rows(_OBL.objects.filter(operator=user))

    cards = [
        # ── Карточка 1: МОЙ БОНУС (что заработал лично) ──
        {"type": "ops_dashboard", "data": {
            "hero_label": "Мой бонус · на счёте",
            "hero_value": float(my_wallet.balance or 0),
            "currency":   my_wallet.currency,
            "stats": [
                {"label": "В холде (14 дней)",
                 "value": f"${my_pending:,.0f}",
                 "sub":   "ждут release",
                 "details_rows": pending_rows,
                 "details_empty": "Нет бонусов в холде"},
                {"label": "За 30 дней",
                 "value": f"${my_30d:,.0f}",
                 "sub":   f"{my_closed_30} сделок",
                 "tone":  "ok",
                 "details_rows": released_rows,
                 "details_empty": "За 30 дней нет зачислений"},
                {"label": "Заработано всего",
                 "value": f"${my_lifetime:,.0f}",
                 "sub":   "life-time",
                 "details_rows": all_rows,
                 "details_empty": "Бонусов пока не было"},
            ],
        }},
        # ── Карточка 2: ПЛАТФОРМА (контекст и проблемы) ──
        {"type": "ops_dashboard", "data": {
            "hero_label": "Платформа · деньги в эскроу",
            "hero_value": s['outstanding_balance'],
            "currency":   "USD",
            "stats":      op_stats,
            "rows_title": f"Активные заказы · топ-{len(simple_rows)}",
            "rows":       simple_rows,
        }},
    ]
    # FAQ — отдельной справочной карточкой ниже (это не часть оперативного дашборда)
    cards.append(rules_faq)

    # Финансовая модель — полные условия по клиенту, оплатам и поставщику.
    # Без markdown в bubble — всё в list-карточках.

    # ── Что платит клиент ──
    pricing_items = [
        {"title": "FOB",
         "subtitle": "Товар + 10% сервисный сбор платформы. Фрахт + страховка + таможня — за счёт клиента."},
        {"title": "CIP",
         "subtitle": "Товар + 14% сервис + фактический фрахт по тарифу платформы. Платформа везёт до согласованного порта/терминала, страхует груз."},
        {"title": "DDP",
         "subtitle": "Товар + 18% сервис + фрахт + $300 фикс на таможенное оформление. Под ключ до двери получателя, все пошлины внутри цены."},
        {"title": "+ 2% агент РФ",
         "subtitle": "Дополнительно если оплата в рублях (валютный контроль, агентский договор). Внутри логистической строки скрыто ~25% маржи группы — это инструмент для рукоятки операционных расходов."},
    ]

    # ── Условия оплаты от клиента ──
    payment_terms_items = [
        {"title": "10% — резерв (предоплата)",
         "subtitle": "Клиент вносит после подтверждения КП. Запускает производство/закупку. При отказе клиента — невозвратный."},
        {"title": "80% — основной платёж",
         "subtitle": "Перед отгрузкой со склада поставщика. Без оплаты груз не выходит в путь."},
        {"title": "10% — финальный платёж",
         "subtitle": "При приёмке клиентом. После закрытия — деньги уходят на эскроу-распределение."},
        {"title": "Эскроу-холд",
         "subtitle": "Все 100% удерживаются на платформе до факта приёмки. Только потом распределяются: поставщику / в депозит / возврат."},
    ]

    # ── Что получает поставщик и как удерживается депозит ──
    seller_payout = [
        {"title": "При отгрузке: FOB-цена − 10% депозита",
         "subtitle": "Платформа выплачивает 90% FOB-стоимости товара. 10% удерживается как гарантия за SLA, качество и приёмку."},
        {"title": "Через 14 дней после приёмки клиентом: +5% возврата",
         "subtitle": "Половина депозита возвращается поставщику. Условие — нет открытых рекламаций и претензий по качеству."},
        {"title": "Финальное удержание: 5% остаётся платформе",
         "subtitle": "Гарантийный сбор за контроль SLA, качества и поддержку. Не возвращается."},
        {"title": "Полный возврат депозита (10%) — если",
         "subtitle": "Поставщик весь год работал без рекламаций и SLA-нарушений. Платформа возвращает накопленный депозит по итогам года."},
        {"title": "Удержание депозита (списание 5–10%)",
         "subtitle": "При подтверждённой рекламации по вине поставщика — депозит закрывает ущерб клиенту."},
    ]

    tier_items = [
        {"title": "Standard",  "subtitle": "оборот до 100M ₽ · стандартный тариф"},
        {"title": "Silver",    "subtitle": "100–200M ₽ · −1 п.п. от сервисного сбора"},
        {"title": "Gold",      "subtitle": "200–400M ₽ · −2 п.п."},
        {"title": "Platinum",  "subtitle": "400M+ ₽ · индивидуальные условия + персональный менеджер"},
    ]

    # ── Раздел правил — разворачивающийся блок (faq-card) со структурными rows.
    # Каждый раздел — нативный <details>, внутри — title/subtitle строки с
    # левой акцентной линией (см. .faq-row).
    rules_faq = {
        "type": "faq",
        "data": {
            "title": "📋 Правила и тарифы платформы (раскрыть)",
            "items": [
                {"q":    "Что платит клиент (по базису поставки)",
                 "rows": pricing_items},
                {"q":    "Условия оплаты клиентом (10 / 80 / 10)",
                 "rows": payment_terms_items},
                {"q":    "Что получает поставщик и как работает депозит",
                 "rows": seller_payout},
                {"q":    "🏆 Тарифные уровни (скидка по обороту)",
                 "rows": tier_items},
            ],
        },
    }

    # Actionable инсайт
    if refund_share > 5:
        esc_insight = (f"Возвраты съели {refund_share:.1f}% от принятого — "
                       f"высокий уровень. Проверьте качество и SLA.")
    elif avg_hold_age > 30:
        esc_insight = (f"Средний возраст активного холда {avg_hold_age:.0f} дн — "
                       f"много залипших заказов. Откройте топ-холды.")
    elif s['platform_balance'] != s['outstanding_balance']:
        diff = s['platform_balance'] - s['outstanding_balance']
        esc_insight = (f"⚠️ Расхождение баланс vs холды: ${diff:,.0f}. "
                       f"Нужна сверка.")
    elif payout_pct < 80 and s['total_held_ever']:
        esc_insight = (f"Только {payout_pct:.0f}% завершённого цикла дошло до продавцов — "
                       f"проверьте задержки выплат.")
    else:
        esc_insight = "Эскроу-поток в норме."

    # Основной дашборд — живые метрики + аналитика + холды + правила.
    return ActionResult(
        text=esc_insight,
        cards=cards,
        actions=[
            {"label": "Возвраты в обработке",
             "action": "op_queue", "params": {"filter": "refund"}},
            {"label": "Ждут резерв 10%",
             "action": "op_queue", "params": {"filter": "awaiting_reserve"}},
            {"label": "Платёжная статистика",
             "action": "op_payments_stats", "params": {}},
        ],
        contextual_actions=[
            {"action": "op_analytics_hub", "label": "← Аналитика"},
        ],
    )


# ══════════════════════════════════════════════════════════
# 10. External rating refresh (Контур/СПАРК)
# ══════════════════════════════════════════════════════════

@register("op_refresh_external_rating")
def op_refresh_external_rating(params, user, role):
    """Принудительно обновить external_score продавца из Kontur/СПАРК (ТЗ §1)."""
    if not _is_operator(role) and role != "admin":
        return ActionResult(text="Доступно только оператору / админу.")
    from django.contrib.auth import get_user_model

    from .external_rating import refresh_external_rating

    U = get_user_model()
    try:
        seller = U.objects.get(id=int(params.get("user_id") or 0))
    except (U.DoesNotExist, ValueError, TypeError):
        return ActionResult(text="Продавец не найден.")

    data = refresh_external_rating(seller)
    if not data:
        return ActionResult(text="⚠️ Не удалось получить external rating.")
    if data.get("source") == "skip":
        return ActionResult(
            text=f"⚠️ {data.get('reason', 'нет данных')} · сначала пройдите KYB.",
        )

    from marketplace.models import UserProfile
    p = UserProfile.objects.filter(user=seller).first()
    return ActionResult(
        text=(
            f"✓ External rating обновлён для {seller.username} · "
            f"score={data['score']:.0f} · {data['source']}"
        ),
        cards=[{"type": "kpi_grid", "data": {
            "title": f"📊 External rating · {seller.username}",
            "items": [
                {"label": "External score", "value": f"{float(data['score']):.1f}",
                 "tone": ("ok" if data['score'] >= 80 else
                          "warn" if data['score'] >= 60 else "bad")},
                {"label": "Источник", "value": data['source']},
                {"label": "Причина", "value": data['reason'][:80]},
                {"label": "Bankruptcy", "value": "🚫" if data['bankruptcy'] else "✓",
                 "tone": "bad" if data['bankruptcy'] else "ok"},
                {"label": "Liquidation", "value": "🚫" if data['liquidation'] else "✓",
                 "tone": "bad" if data['liquidation'] else "ok"},
                {"label": "Behavioral score",
                 "value": f"{float(p.behavioral_score):.1f}" if p else "—"},
                {"label": "Итоговый рейтинг",
                 "value": f"{float(p.rating_score):.1f}" if p else "—",
                 "tone": "info"},
                {"label": "Статус", "value": p.get_supplier_status_display() if p else "—",
                 "tone": ("ok" if (p and p.supplier_status == "trusted") else
                          "warn" if (p and p.supplier_status == "sandbox") else "bad")},
            ],
        }}],
        contextual_actions=[
            {"action": "admin_user_detail", "label": "← Профиль",
             "params": {"user_id": seller.id}},
        ],
    )


# ═══════════════════════════════════════════════════════════════
# Финансист — подтверждение пополнения депозита
# ═══════════════════════════════════════════════════════════════

@register("op_topup_queue")
def op_topup_queue(params, user, role):
    """Очередь заявок на пополнение, ожидающих подтверждения финансами."""
    from assistant.models import WalletTopupRequest

    qs = (WalletTopupRequest.objects
          .filter(status__in=("pending", "awaiting_confirmation"))
          .select_related("user")
          .order_by("status", "-created_at")[:100])
    if not qs:
        return ActionResult(
            text="✓ Очередь пополнений пуста — все заявки обработаны.",
            actions=[
                {"label": "📥 Поддержка",  "action": "support_home", "params": {}},
                {"label": "💰 Эскроу",     "action": "op_payments_dashboard", "params": {}},
            ],
            contextual_actions=[{"action": "op_analytics_hub", "label": "← Аналитика"}],
        )

    rows = []
    for r in qs:
        rows.append({
            "title": f"{r.reference_code} · ${r.amount:,.2f} · {r.user.username}",
            "subtitle": (
                f"{r.get_method_display()} · {r.get_status_display()} · "
                f"создана {r.created_at.strftime('%d.%m %H:%M')}"
                + (f" · юзер сообщил {r.user_claim_at.strftime('%d.%m %H:%M')}"
                   if r.user_claim_at else "")
            ),
            "action": "op_confirm_topup",
            "params": {"topup_id": r.id},
            "badge": {"label": r.method, "tone": "info"},
            "tone": "warn" if r.status == "awaiting_confirmation" else "info",
        })

    return ActionResult(
        text=f"💰 Заявки на пополнение в работе · {len(rows)}",
        cards=[{
            "type": "list",
            "data": {"title": "Pending top-up requests", "items": rows},
        }],
        contextual_actions=[{"action": "op_analytics_hub", "label": "← Аналитика"}],
    )


@register("op_confirm_topup")
def op_confirm_topup(params, user, role):
    """Финансист подтверждает, что средства поступили → wallet кредитуется
    атомарно (см. WalletTopupRequest.mark_paid)."""
    from assistant.models import WalletTopupRequest

    if not (user.is_staff or user.is_superuser):
        return ActionResult(text="⚠️ Действие доступно только сотрудникам.")

    topup_id = params.get("topup_id")
    try:
        req = WalletTopupRequest.objects.get(id=int(topup_id))
    except (WalletTopupRequest.DoesNotExist, ValueError, TypeError):
        return ActionResult(text="Заявка не найдена.")

    if req.status == "paid":
        return ActionResult(
            text=f"Заявка {req.reference_code} уже подтверждена ранее "
                 f"({req.confirmed_at.strftime('%d.%m %H:%M')}).",
        )
    if req.status in ("cancelled", "failed", "expired"):
        return ActionResult(
            text=f"Заявка {req.reference_code} в статусе "
                 f"«{req.get_status_display()}» — зачисление невозможно.",
        )

    tx = req.mark_paid(by_user=user)
    _log_event(
        user, "topup_confirmed",
        {"topup_id": req.id, "amount": str(req.amount),
         "ref": req.reference_code, "tx_id": tx.id if tx else None,
         "buyer_id": req.user_id},
    )
    # Уведомление покупателю — через стандартный нотификатор.
    try:
        _notify(req.user, "topup_paid",
                f"💰 Депозит пополнен на ${req.amount:,.2f} "
                f"(заявка {req.reference_code}).")
    except Exception:
        pass

    return ActionResult(
        text=(
            f"✓ Заявка {req.reference_code} подтверждена. "
            f"Депозит пополнен на ${req.amount:,.2f} для {req.user.username}."
        ),
        actions=[
            {"label": "📋 Назад в очередь", "action": "op_topup_queue", "params": {}},
        ],
    )


@register("op_reject_topup")
def op_reject_topup(params, user, role):
    """Финансист отклоняет заявку (например, деньги не пришли в срок)."""
    from django.utils import timezone

    from assistant.models import WalletTopupRequest

    if not (user.is_staff or user.is_superuser):
        return ActionResult(text="⚠️ Действие доступно только сотрудникам.")

    topup_id = params.get("topup_id")
    reason = (params.get("reason") or "").strip()[:400]
    try:
        req = WalletTopupRequest.objects.get(id=int(topup_id))
    except (WalletTopupRequest.DoesNotExist, ValueError, TypeError):
        return ActionResult(text="Заявка не найдена.")

    if req.status in ("paid", "cancelled", "failed", "expired"):
        return ActionResult(
            text=f"Заявка в статусе «{req.get_status_display()}» — изменить нельзя.",
        )

    req.status = "failed"
    req.cancelled_at = timezone.now()
    if reason:
        req.note = (req.note + " | " if req.note else "") + f"rejected: {reason}"
    req.save(update_fields=["status", "cancelled_at", "note", "updated_at"])
    _log_event(user, "topup_rejected",
               {"topup_id": req.id, "ref": req.reference_code, "reason": reason})
    try:
        _notify(req.user, "topup_failed",
                f"⚠️ Заявка {req.reference_code} отклонена: "
                f"{reason or 'свяжитесь с финансовым отделом'}.")
    except Exception:
        pass
    return ActionResult(text=f"✓ Заявка {req.reference_code} отклонена.")

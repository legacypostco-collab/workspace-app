"""Admin (platform-level) actions: GMV-аналитика, user management, модерация.

Доступно только пользователям с is_superuser=True (роль 'admin').
Ортогонально operator-actions: оператор работает с конкретными заказами,
admin — с платформой в целом.

Структура:
  admin_dashboard       — KPI grid + последние события
  admin_gmv             — платформенный GMV (день / неделя / месяц)
  admin_users           — список пользователей с фильтрами
  admin_user_detail     — детали юзера (заказы, KYB, wallet, статус)
  admin_ban_user        — заблокировать (DraftCard → User.is_active=False)
  admin_unban_user      — разблокировать
  admin_moderation_queue — единая очередь требующих внимания
  admin_catalog_review  — каталог: новые товары, подозрительные записи
  admin_platform_settings — read-only снэпшот env / config
"""
from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from .actions import ActionResult, _notify, register

logger = logging.getLogger(__name__)


def _is_admin(role: str) -> bool:
    return role == "admin"


def _ensure_admin(role: str):
    if not _is_admin(role):
        return ActionResult(
            text="🔒 Только администратор платформы может выполнять это действие.",
        )
    return None


# ══════════════════════════════════════════════════════════
# 1. Dashboard — top-level KPI
# ══════════════════════════════════════════════════════════

@register("admin_dashboard")
def admin_dashboard(params, user, role):
    err = _ensure_admin(role)
    if err: return err
    from django.contrib.auth import get_user_model

    from marketplace.models import RFQ, CompanyVerification, Order

    U = get_user_model()
    now = timezone.now()
    cutoff_24h = now - timedelta(hours=24)
    cutoff_7d = now - timedelta(days=7)

    users_total = U.objects.filter(is_active=True).count()
    users_new_7d = U.objects.filter(date_joined__gte=cutoff_7d).count()

    orders_total = Order.objects.count()
    orders_24h = Order.objects.filter(created_at__gte=cutoff_24h).count()
    open_orders = Order.objects.exclude(status__in=("completed", "cancelled")).count()

    rfq_total = RFQ.objects.count()
    rfq_24h = RFQ.objects.filter(created_at__gte=cutoff_24h).count()

    kyb_pending = CompanyVerification.objects.filter(status="pending").count()
    kyb_verified = CompanyVerification.objects.filter(status="verified").count()

    # GMV: сумма total_amount по статусу 'paid' или 'completed' за 7 дней
    paid_orders_7d = Order.objects.filter(
        created_at__gte=cutoff_7d,
        payment_status__in=("paid", "refunded"),
    ).values_list("total_amount", flat=True)
    gmv_7d = sum((Decimal(x or 0) for x in paid_orders_7d), Decimal("0"))

    # SLA breaches за 7 дней
    sla_breached_7d = Order.objects.filter(
        sla_status="breached", created_at__gte=cutoff_7d,
    ).count()

    # Загрузки прайс-листов продавцами (каталог) за 7 дней + импортировано позиций.
    from django.db.models import Sum

    from marketplace.models import PricelistImport
    pl_uploads_7d = PricelistImport.objects.filter(created_at__gte=cutoff_7d).count()
    pl_positions_7d = (PricelistImport.objects
                       .filter(status="imported", created_at__gte=cutoff_7d)
                       .aggregate(n=Sum("imported_rows"))["n"] or 0)

    return ActionResult(
        text=(
            f"🛡 Платформа · {users_total} активных юзеров (+{users_new_7d} за неделю) · "
            f"{orders_total} заказов всего · GMV за 7 дней ${gmv_7d:,.0f}."
        ),
        cards=[
            {"type": "kpi_grid", "data": {"title": "🛡 Admin · Сводка", "items": [
                {"label": "Активных юзеров", "value": str(users_total), "tone": "info",
                 "action": "admin_users", "params": {}},
                {"label": "Новых за 7 дней", "value": f"+{users_new_7d}",
                 "tone": "ok" if users_new_7d else "warn",
                 "action": "admin_users", "params": {"filter": "new"}},
                {"label": "Заказов всего", "value": str(orders_total),
                 "action": "admin_gmv", "params": {}},
                {"label": "За 24 часа", "value": str(orders_24h),
                 "action": "admin_gmv", "params": {}},
                {"label": "В работе", "value": str(open_orders), "tone": "info",
                 "action": "admin_gmv", "params": {}},
                {"label": "GMV 7d", "value": f"${gmv_7d:,.0f}", "tone": "ok",
                 "action": "admin_revenue_breakdown", "params": {}},
                {"label": "SLA breach 7d", "value": str(sla_breached_7d),
                 "tone": "bad" if sla_breached_7d > 0 else "ok",
                 "action": "admin_moderation_queue", "params": {}},
                {"label": "RFQ за 24ч", "value": str(rfq_24h),
                 "action": "admin_market_twin", "params": {}},
                {"label": "KYB pending", "value": str(kyb_pending),
                 "tone": "warn" if kyb_pending else "ok",
                 "action": "admin_moderation_queue", "params": {}},
                {"label": "KYB verified", "value": str(kyb_verified),
                 "action": "admin_users", "params": {"filter": "verified"}},
                {"label": "Загрузки прайса 7д", "value": str(pl_uploads_7d),
                 "tone": "info", "action": "admin_activity_feed",
                 "params": {"kind": "pricelist"}},
                {"label": "Позиций 7д", "value": f"{pl_positions_7d:,}",
                 "action": "admin_activity_feed", "params": {"kind": "pricelist"}},
            ]}},
        ],
        contextual_actions=[
            {"action": "admin_activity_feed", "label": "🛰 Лента событий"},
            {"action": "admin_gmv", "label": "📈 GMV-разбивка"},
            {"action": "admin_moderation_queue", "label": "🚨 Модерация"},
            {"action": "admin_users", "label": "👥 Пользователи"},
        ],
    )


# ══════════════════════════════════════════════════════════
# 2. GMV — платформенный оборот
# ══════════════════════════════════════════════════════════

@register("admin_gmv")
def admin_gmv(params, user, role):
    err = _ensure_admin(role)
    if err: return err
    from django.db.models import Count, Sum

    from marketplace.models import Order

    now = timezone.now()
    windows = [
        ("24 часа", timedelta(hours=24)),
        ("7 дней", timedelta(days=7)),
        ("30 дней", timedelta(days=30)),
        ("90 дней", timedelta(days=90)),
    ]
    items = []
    for label, td in windows:
        cutoff = now - td
        agg = Order.objects.filter(
            created_at__gte=cutoff,
            payment_status__in=("paid", "refunded"),
        ).aggregate(gmv=Sum("total_amount"), n=Count("id"))
        gmv = agg["gmv"] or Decimal("0")
        n = agg["n"] or 0
        items.append({"label": label, "value": f"${gmv:,.0f} · {n} заказ.",
                       "tone": "ok" if gmv > 0 else "warn",
                       "action": "admin_revenue_breakdown", "params": {}})

    # Top categories
    top_cat = list(
        Order.objects.filter(payment_status__in=("paid", "refunded"))
        .values("items__part__category__name")
        .annotate(gmv=Sum("total_amount"))
        .order_by("-gmv")[:5]
    )
    cat_rows = [
        {"title": c["items__part__category__name"] or "—",
         "subtitle": f"${(c['gmv'] or 0):,.0f}",
         "action": "admin_catalog_review", "params": {}}
        for c in top_cat if c["gmv"]
    ]

    return ActionResult(
        text=(
            "📈 Платформенный GMV (только paid/refunded, без отменённых)."
        ),
        cards=[
            {"type": "kpi_grid", "data": {"title": "💰 GMV по периодам", "items": items}},
            {"type": "list", "data": {"title": "🏆 Топ категорий по GMV",
                                       "items": cat_rows or [{"title": "Нет данных"}]}},
        ],
        contextual_actions=[
            {"action": "admin_dashboard", "label": "← Сводка"},
            {"action": "op_payments_dashboard", "label": "💰 Эскроу"},
        ],
    )


# ══════════════════════════════════════════════════════════
# 3. Users — список с фильтрами
# ══════════════════════════════════════════════════════════

@register("admin_users")
def admin_users(params, user, role):
    err = _ensure_admin(role)
    if err: return err
    from django.contrib.auth import get_user_model
    U = get_user_model()

    flt = (params.get("filter") or "all").strip().lower()
    qs = U.objects.select_related("profile", "kyb")
    if flt == "active":
        qs = qs.filter(is_active=True)
    elif flt == "banned":
        qs = qs.filter(is_active=False)
    elif flt == "buyers":
        qs = qs.filter(profile__role="buyer")
    elif flt == "sellers":
        qs = qs.filter(profile__role="seller")
    elif flt == "operators":
        qs = qs.filter(profile__role__startswith="operator")
    elif flt == "kyb_pending":
        qs = qs.filter(kyb__status="pending")
    elif flt == "verified":
        qs = qs.filter(kyb__status="verified")
    elif flt == "new":
        qs = qs.filter(date_joined__gte=timezone.now() - timedelta(days=7))
    qs = qs.exclude(username="__platform_escrow__").order_by("-date_joined")[:300]

    def _group_of(u):
        if u.is_superuser:
            return "admin"
        prof = getattr(u, "profile", None)
        r = ((prof.role if prof else "") or "").lower()
        if r.startswith("operator"):
            return "operator"
        if r == "seller":
            return "seller"
        if getattr(u, "is_staff", False):
            return "operator"
        return "buyer"

    def _row(u):
        prof = getattr(u, "profile", None)
        kyb_obj = getattr(u, "kyb", None)
        kyb_label = kyb_obj.get_status_display() if kyb_obj else "—"
        role_label = (prof.role if prof else "—") or "—"
        flags = []
        if u.is_superuser: flags.append("⚡admin")
        if not u.is_active: flags.append("🚫 ban")
        if kyb_obj and kyb_obj.status == "verified": flags.append("✓ KYB")
        return {
            "title": f"{u.username} · {role_label}",
            "subtitle": (f"{u.email or '—'} · KYB: {kyb_label}"
                         + (" · " + " ".join(flags) if flags else "")),
            "action": "admin_user_detail", "params": {"user_id": u.id},
        }

    # Делим по сущностям (ролям) — отдельная секция на каждую.
    GROUPS = [("buyer", "👤 Покупатели"), ("seller", "🏭 Продавцы"),
              ("operator", "🛠 Операторы"), ("admin", "⚡ Админы")]
    PER = 40
    buckets = {k: [] for k, _ in GROUPS}
    for u in qs:
        buckets[_group_of(u)].append(u)

    cards = []
    total = 0
    for key, title in GROUPS:
        bucket = buckets[key]
        if not bucket:
            continue
        total += len(bucket)
        items = [_row(u) for u in bucket[:PER]]
        if len(bucket) > PER:
            items.append({"title": f"… ещё {len(bucket) - PER} (показаны первые {PER})"})
        cards.append({"type": "list", "data": {
            "title": f"{title} · {len(bucket)}", "items": items,
            "collapsible": True}})   # изначально свёрнуто, раскрытие по клику
    if not cards:
        cards = [{"type": "list", "data": {"title": "👥 Пользователи",
                                            "items": [{"title": "Пусто"}]}}]

    return ActionResult(
        text=f"👥 Пользователи · фильтр «{flt}» · {total} в {len(cards)} группах.",
        cards=cards,
        contextual_actions=[
            {"action": "admin_users", "label": "Все",       "params": {"filter": "all"}},
            {"action": "admin_users", "label": "Активные",  "params": {"filter": "active"}},
            {"action": "admin_users", "label": "Заблокир.", "params": {"filter": "banned"}},
            {"action": "admin_users", "label": "👤 Покупатели","params": {"filter": "buyers"}},
            {"action": "admin_users", "label": "🏭 Продавцы",  "params": {"filter": "sellers"}},
            {"action": "admin_users", "label": "🛠 Операторы", "params": {"filter": "operators"}},
            {"action": "admin_users", "label": "KYB pending","params": {"filter": "kyb_pending"}},
        ],
    )


# ══════════════════════════════════════════════════════════
# 3b. Лента важных событий — контроль/безопасность
# ══════════════════════════════════════════════════════════

_ACTIVITY_EMOJI = {"order": "🛒", "rfq": "📋", "pricelist": "📦"}
_ACTIVITY_LABEL = {"order": "Заказ", "rfq": "RFQ", "pricelist": "Загрузка прайса"}


@register("admin_activity_feed")
def admin_activity_feed(params, user, role):
    """Сквозная лента важных событий: новая сделка / RFQ / загрузка прайса —
    с кабинетом (кто), IP (откуда) и позициями (что). Не дублирует разделы —
    это аудит-поток для контроля."""
    err = _ensure_admin(role)
    if err: return err
    from marketplace.models import ActivityEvent

    flt = (params.get("kind") or "all").strip().lower()
    qs = ActivityEvent.objects.select_related("actor").all()
    if flt in ("order", "rfq", "pricelist"):
        qs = qs.filter(kind=flt)
    qs = qs[:40]

    rows = []
    for ev in qs:
        m = ev.meta or {}
        actor_name = ev.actor.username if ev.actor else "гость / аноним"
        role_lbl = ev.actor_role or ("—" if ev.actor else "anon")
        when = timezone.localtime(ev.created_at).strftime("%d.%m %H:%M")
        ip = ev.ip or "—"
        # Превью позиций (что именно в событии)
        items = m.get("items") or []
        names = []
        for it in items[:4]:
            names.append(it.get("name") or it.get("query") or it.get("oem") or "")
        pos_preview = ", ".join(n for n in names if n)
        if len(items) > 4:
            pos_preview += f" … (+{len(items) - 4})"
        title = f"{_ACTIVITY_EMOJI.get(ev.kind, '•')} {ev.title or _ACTIVITY_LABEL.get(ev.kind, ev.kind)}"
        subtitle = f"{when} · 👤 {actor_name} ({role_lbl}) · 🌐 {ip}"
        if pos_preview:
            subtitle += f" · {pos_preview}"
        # Drill-in: заказ → детали заказа, RFQ → статус, загрузка → кабинет продавца
        if ev.kind == "order" and m.get("order_id"):
            click = {"action": "get_order_detail", "params": {"order_id": m["order_id"]}}
        elif ev.kind == "rfq" and m.get("rfq_id"):
            click = {"action": "get_rfq_status", "params": {"rfq_id": m["rfq_id"]}}
        elif ev.actor_id:
            click = {"action": "admin_user_detail", "params": {"user_id": ev.actor_id}}
        else:
            click = {}
        rows.append({"title": title, "subtitle": subtitle, **click})

    return ActionResult(
        text=f"🛰 Лента событий · фильтр «{flt}» · {len(rows)} последних.",
        cards=[{"type": "list", "data": {
            "title": "🛰 Важные события (сделки · RFQ · загрузки)",
            "items": rows or [{"title": "Пока событий нет"}],
        }}],
        contextual_actions=[
            {"action": "admin_activity_feed", "label": "🔄 Обновить", "params": {"kind": flt}},
            {"action": "admin_activity_feed", "label": "Все",       "params": {"kind": "all"}},
            {"action": "admin_activity_feed", "label": "🛒 Заказы",  "params": {"kind": "order"}},
            {"action": "admin_activity_feed", "label": "📋 RFQ",      "params": {"kind": "rfq"}},
            {"action": "admin_activity_feed", "label": "📦 Прайсы",   "params": {"kind": "pricelist"}},
        ],
    )


# ══════════════════════════════════════════════════════════
# 4. User detail — детальный обзор
# ══════════════════════════════════════════════════════════

@register("admin_user_detail")
def admin_user_detail(params, user, role):
    err = _ensure_admin(role)
    if err: return err
    from django.contrib.auth import get_user_model

    from marketplace.models import Order

    from .models import Wallet
    U = get_user_model()
    try:
        u = U.objects.get(id=int(params.get("user_id") or 0))
    except (U.DoesNotExist, ValueError, TypeError):
        return ActionResult(text="Пользователь не найден.")

    prof = getattr(u, "profile", None)
    kyb = getattr(u, "kyb", None)
    wallet = Wallet.objects.filter(user=u).first()

    from django.db.models import Sum

    from marketplace.models import RFQ, PricelistImport, OrderClaim, ActivityEvent
    from .models import WalletTopupRequest

    def _dt(d, fmt="%Y-%m-%d %H:%M"):
        return d.strftime(fmt) if d else "—"

    # Заказы / заявки
    orders_n = Order.objects.filter(buyer=u).count()
    orders_paid = Order.objects.filter(buyer=u, payment_status="paid").count()
    paid_sum = (Order.objects.filter(buyer=u, payment_status="paid")
                .aggregate(s=Sum("total_amount"))["s"] or 0)
    sales_orders = Order.objects.filter(items__part__seller=u).distinct().count()
    rfq_n = RFQ.objects.filter(created_by=u).count()

    # Финансы
    topups = WalletTopupRequest.objects.filter(user=u).order_by("-created_at")
    topups_n = topups.count()
    last_topup = topups.first()

    # Продавец
    pl_qs = PricelistImport.objects.filter(seller=u)
    pl_n = pl_qs.count()
    pl_positions = (pl_qs.filter(status="imported")
                    .aggregate(s=Sum("imported_rows"))["s"] or 0)

    # Активность / безопасность
    claims_n = OrderClaim.objects.filter(opened_by=u).count()
    ev_n = ActivityEvent.objects.filter(actor=u).count()
    uniq_ips = []
    for ip in (ActivityEvent.objects.filter(actor=u).exclude(ip="")
               .order_by("-created_at").values_list("ip", flat=True)[:30]):
        if ip not in uniq_ips:
            uniq_ips.append(ip)

    role_val = (prof.role if prof else "—") or "—"
    if prof and getattr(prof, "operator_role", ""):
        role_val += f" ({prof.operator_role})"
    priv = "⚡ admin" if u.is_superuser else ("staff" if u.is_staff else "user")

    groups = []
    groups.append({"title": "👤 Аккаунт", "rows": [
        {"label": "Username", "value": u.username, "primary": True},
        {"label": "Email", "value": u.email or "—"},
        {"label": "Роль", "value": role_val},
        {"label": "Статус", "value": "🚫 Заблокирован" if not u.is_active else "✓ Активен",
         "primary": not u.is_active},
        {"label": "Привилегии", "value": priv},
        {"label": "Зарегистрирован", "value": _dt(u.date_joined, "%Y-%m-%d")},
        {"label": "Последний вход", "value": _dt(u.last_login)},
        {"label": "Язык", "value": (getattr(prof, "language", "") or "—") if prof else "—"},
        {"label": "AI-кредиты", "value": str(getattr(prof, "ai_credits", "—")) if prof else "—"},
    ]})
    if prof:
        groups.append({"title": "📇 Контакты", "rows": [
            {"label": "Контактное лицо", "value": prof.contact_name or "—"},
            {"label": "Должность", "value": prof.position or "—"},
            {"label": "Телефон", "value": prof.phone_e164 or "—"},
            {"label": "Компания", "value": prof.company_name or "—"},
            {"label": "Мессенджер", "value": (f"{prof.messenger_kind}: {prof.messenger_handle}"
                                              if prof.messenger_handle else "—")},
            {"label": "Уведомления", "value": "email " + ("✓" if prof.notif_email_enabled else "✗")
                + " · TG " + ("✓" if prof.notif_telegram_enabled else "✗")},
        ]})
    if kyb:
        docs = " · ".join(filter(None, [
            "Устав" if kyb.doc_charter else "", "ЕГРЮЛ" if kyb.doc_egrul else "",
            "Паспорт" if kyb.doc_passport else ""])) or "нет"
        groups.append({"title": "🏢 Компания / KYB", "rows": [
            {"label": "Статус KYB", "value": kyb.get_status_display()},
            {"label": "Юр. название", "value": kyb.legal_name or "—"},
            {"label": "Страна", "value": kyb.country or "—"},
            {"label": "ИНН", "value": kyb.inn or "—"},
            {"label": "КПП", "value": kyb.kpp or "—"},
            {"label": "ОГРН", "value": kyb.ogrn or "—"},
            {"label": "VAT", "value": kyb.vat_number or "—"},
            {"label": "Директор", "value": kyb.director_name or "—"},
            {"label": "Банк", "value": kyb.bank_name or "—"},
            {"label": "Документы", "value": docs},
        ]})
    else:
        groups.append({"title": "🏢 Компания / KYB",
                       "rows": [{"label": "KYB", "value": "не подавалась"}]})
    fin_rows = [
        {"label": "Баланс кошелька",
         "value": f"${wallet.balance:,.2f} {wallet.currency}" if wallet else "—"},
        {"label": "Оплачено заказов", "value": f"${float(paid_sum):,.2f}"},
        {"label": "Пополнений", "value": str(topups_n)},
    ]
    if last_topup:
        fin_rows.append({"label": "Последнее пополнение",
                         "value": f"${last_topup.amount:,.0f} · {last_topup.get_status_display()} · "
                                  f"{_dt(last_topup.created_at, '%Y-%m-%d')}"})
    groups.append({"title": "💰 Финансы", "rows": fin_rows})
    groups.append({"title": "📦 Заказы и заявки", "rows": [
        {"label": "Как покупатель", "value": f"{orders_paid} оплачено / {orders_n} всего"},
        {"label": "Как продавец (продажи)", "value": str(sales_orders)},
        {"label": "RFQ создано", "value": str(rfq_n)},
    ]})
    if prof and (role_val.startswith("seller") or pl_n or sales_orders):
        flags = []
        if getattr(prof, "bankruptcy_flag", False): flags.append("⚠ банкротство")
        if getattr(prof, "liquidation_flag", False): flags.append("⚠ ликвидация")
        groups.append({"title": "🏭 Продавец-метрики", "rows": [
            {"label": "Статус поставщика", "value": prof.supplier_status or "—"},
            {"label": "Рейтинг", "value": f"{float(prof.rating_score):.1f}"
                if prof.rating_score is not None else "—"},
            {"label": "Внешний / поведенческий",
             "value": f"{float(prof.external_score or 0):.0f} / {float(prof.behavioral_score or 0):.0f}"},
            {"label": "Флаги", "value": " · ".join(flags) if flags else "—"},
            {"label": "Загрузок прайса", "value": str(pl_n)},
            {"label": "Позиций импортировано", "value": f"{pl_positions:,}"},
        ]})
    groups.append({"title": "🛰 Активность / безопасность", "rows": [
        {"label": "IP (последние)", "value": ", ".join(uniq_ips[:3]) or "—"},
        {"label": "Событий в ленте", "value": str(ev_n)},
        {"label": "Претензий открыто", "value": str(claims_n)},
        {"label": "Admin-заметка", "value": (getattr(prof, "admin_note", "") or "—") if prof else "—"},
    ]})

    actions = []
    if u.is_active and not u.is_superuser:
        actions.append({"action": "admin_ban_user", "label": "🚫 Заблокировать",
                        "params": {"user_id": u.id}})
    elif not u.is_active:
        actions.append({"action": "admin_unban_user", "label": "✓ Разблокировать",
                        "params": {"user_id": u.id}})

    # Кликабельные ссылки на сами заявки пользователя (заказы + RFQ), чтобы
    # админ мог открыть и проверить позиции — а не только видеть счётчик.
    from marketplace.models import RFQ
    open_btns = []
    for oid in (Order.objects.filter(buyer=u).order_by("-created_at")
                .values_list("id", flat=True)[:6]):
        open_btns.append({"action": "get_order_detail",
                          "label": f"📦 Заказ ORD-{oid}",
                          "params": {"order_id": oid}})
    for rid in (RFQ.objects.filter(created_by=u).order_by("-created_at")
                .values_list("id", flat=True)[:6]):
        open_btns.append({"action": "get_rfq_status",
                          "label": f"📋 RFQ #{rid}",
                          "params": {"rfq_id": rid}})

    return ActionResult(
        text=f"👤 {u.username} · {u.email or 'нет email'}",
        cards=[{"type": "draft", "data": {"title": f"Профиль · {u.username}",
                                           "groups": groups, "confirm_label": "—"}}],
        actions=actions,
        contextual_actions=open_btns + [
            {"action": "admin_users", "label": "← К списку"},
        ],
    )


# ══════════════════════════════════════════════════════════
# 5. Ban / Unban
# ══════════════════════════════════════════════════════════

@register("admin_ban_user")
def admin_ban_user(params, user, role):
    err = _ensure_admin(role)
    if err: return err
    from django.contrib.auth import get_user_model
    U = get_user_model()
    try:
        target = U.objects.get(id=int(params.get("user_id") or 0))
    except (U.DoesNotExist, ValueError, TypeError):
        return ActionResult(text="Пользователь не найден.")

    if target.id == user.id:
        return ActionResult(text="⚠️ Нельзя заблокировать самого себя.")
    if target.is_superuser:
        return ActionResult(text="⚠️ Заблокировать админа нельзя.")
    if not target.is_active:
        return ActionResult(text=f"Пользователь {target.username} уже заблокирован.")

    reason = (params.get("reason") or "").strip()
    confirmed = bool(params.get("confirmed"))
    if not confirmed or not reason:
        return ActionResult(
            text=f"Заблокировать {target.username}?",
            cards=[{"type": "form", "data": {
                "title": f"🚫 Заблокировать · {target.username}",
                "submit_action": "admin_ban_user",
                "fields": [
                    {"name": "reason", "label": "Причина (для аудита и нотификации)",
                     "type": "textarea", "required": True},
                ],
                "fixed_params": {"user_id": target.id, "confirmed": True},
            }}],
        )

    target.is_active = False
    target.save(update_fields=["is_active"])
    _notify(target, kind="system",
            title="Аккаунт заблокирован",
            body=f"Платформа заблокировала ваш аккаунт. Причина: {reason[:200]}",
            url="")
    return ActionResult(
        text=f"🚫 {target.username} заблокирован. Причина: {reason[:120]}",
        contextual_actions=[
            {"action": "admin_user_detail", "label": "← Профиль",
             "params": {"user_id": target.id}},
            {"action": "admin_users", "label": "Все юзеры"},
        ],
    )


@register("admin_unban_user")
def admin_unban_user(params, user, role):
    err = _ensure_admin(role)
    if err: return err
    from django.contrib.auth import get_user_model
    U = get_user_model()
    try:
        target = U.objects.get(id=int(params.get("user_id") or 0))
    except (U.DoesNotExist, ValueError, TypeError):
        return ActionResult(text="Пользователь не найден.")
    if target.is_active:
        return ActionResult(text=f"{target.username} не заблокирован.")

    if not bool(params.get("confirmed")):
        return ActionResult(
            text=f"Разблокировать {target.username}?",
            cards=[{"type": "draft", "data": {
                "title": f"✓ Разблокировать · {target.username}",
                "rows": [{"label": "Юзер", "value": target.username, "primary": True}],
                "confirm_action": "admin_unban_user",
                "confirm_label": "✓ Разблокировать",
                "confirm_params": {"user_id": target.id, "confirmed": True},
                "cancel_label": "Отмена",
            }}],
        )

    target.is_active = True
    target.save(update_fields=["is_active"])
    _notify(target, kind="system",
            title="Аккаунт разблокирован",
            body="Платформа восстановила доступ к вашему аккаунту.",
            url="")
    return ActionResult(
        text=f"✓ {target.username} разблокирован.",
        contextual_actions=[
            {"action": "admin_user_detail", "label": "← Профиль",
             "params": {"user_id": target.id}},
        ],
    )


# ══════════════════════════════════════════════════════════
# 6. Moderation queue — единая
# ══════════════════════════════════════════════════════════

@register("admin_moderation_queue")
def admin_moderation_queue(params, user, role):
    err = _ensure_admin(role)
    if err: return err
    from marketplace.models import CompanyVerification, Order, Quote

    kyb_pending = CompanyVerification.objects.filter(status="pending").count()
    refunds = Order.objects.filter(payment_status="refund_pending").count()
    sla_breached = Order.objects.filter(sla_status="breached").count()
    quotes_countered = Quote.objects.filter(status="countered").count()

    items = [
        {"title": f"KYB на проверке · {kyb_pending}",
         "subtitle": "Анкеты ждут верификации — нажмите", "tone": "warn",
         "action": "op_kyb_queue", "params": {}} if kyb_pending else None,
        {"title": f"Возвраты в обработке · {refunds}",
         "subtitle": "Заказы на возврат — нажмите", "tone": "warn",
         "action": "op_queue", "params": {"filter": "refund"}} if refunds else None,
        {"title": f"SLA нарушены · {sla_breached}",
         "subtitle": "Просрочки по этапам — нажмите", "tone": "bad",
         "action": "op_sla_breach", "params": {}} if sla_breached else None,
        {"title": f"Контр-офферы ждут ответа · {quotes_countered}",
         "subtitle": "Переторжка по КП — нажмите", "tone": "info",
         "action": "op_queue", "params": {}} if quotes_countered else None,
    ]
    items = [x for x in items if x]
    if not items:
        items = [{"title": "✓ Очередь пуста", "subtitle": "Всё под контролем"}]

    return ActionResult(
        text="🚨 Платформенная очередь модерации",
        cards=[{"type": "list", "data": {"title": "🚨 Модерация платформы",
                                          "items": items}}],
        contextual_actions=[
            {"action": "op_kyb_queue", "label": "🛡 KYB"},
            {"action": "op_queue", "label": "📋 Заказы (operator)"},
            {"action": "admin_dashboard", "label": "← Сводка"},
        ],
    )


# ══════════════════════════════════════════════════════════
# 8. Catalog review — новые товары
# ══════════════════════════════════════════════════════════

@register("admin_catalog_review")
def admin_catalog_review(params, user, role):
    err = _ensure_admin(role)
    if err: return err
    from marketplace.models import Part

    # Подозрительные: цена = 0, нет seller, нет brand, нет category
    suspicious = Part.objects.filter(is_active=True).filter(
        price=0,
    )[:20]
    no_seller = Part.objects.filter(is_active=True, seller__isnull=True)[:10]
    recent = Part.objects.filter(is_active=True).order_by("-id")[:10]

    susp_rows = [
        {"title": f"#{p.id} {p.title[:50]}",
         "subtitle": f"price=$0 · brand={p.brand.name if p.brand else '—'}"}
        for p in suspicious
    ]
    no_seller_rows = [
        {"title": f"#{p.id} {p.title[:50]}",
         "subtitle": "нет seller'а — orphan record"}
        for p in no_seller
    ]
    recent_rows = [
        {"title": f"#{p.id} {p.title[:50]}",
         "subtitle": f"${p.price or 0} · {p.seller.username if p.seller else '—'}"}
        for p in recent
    ]

    return ActionResult(
        text=(
            f"📦 Каталог · {len(susp_rows)} с ценой $0 · {len(no_seller_rows)} без продавца "
            f"· {len(recent_rows)} новых."
        ),
        cards=[
            {"type": "list", "data": {"title": "⚠️ Цена = $0",
                "items": susp_rows or [{"title": "Чисто"}]}},
            {"type": "list", "data": {"title": "⚠️ Без продавца",
                "items": no_seller_rows or [{"title": "Чисто"}]}},
            {"type": "list", "data": {"title": "🆕 Последние добавленные",
                "items": recent_rows or [{"title": "—"}]}},
        ],
        contextual_actions=[
            {"action": "admin_dashboard", "label": "← Сводка"},
        ],
    )


# ══════════════════════════════════════════════════════════
# 9. Platform settings — read-only снэпшот
# ══════════════════════════════════════════════════════════

@register("admin_revenue_breakdown")
def admin_revenue_breakdown(params, user, role):
    """ТЗ §15: декомпозиция дохода группы — по компонентам, по периодам.

    Показывает сумму basis_fee / logistics_margin / success_fee / rf_agent /
    customs_fee по 4 окнам (24h / 7d / 30d / 90d) + total.
    """
    err = _ensure_admin(role)
    if err: return err
    from datetime import timedelta

    from django.db.models import Sum
    from django.utils import timezone

    from marketplace.models import PlatformRevenueLine

    now = timezone.now()
    windows = [("24 часа", 1), ("7 дней", 7), ("30 дней", 30), ("90 дней", 90)]
    KIND_LABELS = dict(PlatformRevenueLine.KIND_CHOICES)

    results = []
    for label, days in windows:
        cutoff = now - timedelta(days=days)
        qs = PlatformRevenueLine.objects.filter(created_at__gte=cutoff)
        agg = qs.values("kind").annotate(total=Sum("amount")).order_by("-total")
        by_kind = {row["kind"]: row["total"] or 0 for row in agg}
        total = sum(by_kind.values(), Decimal("0"))
        results.append({"label": label, "by_kind": by_kind, "total": total,
                         "n": qs.count()})

    main_window = results[1]  # 7d
    items = [
        {"label": "За 7 дней TOTAL", "value": f"${main_window['total']:,.0f}",
         "tone": "info", "action": "admin_gmv", "params": {}},
    ]
    for kind, lbl in PlatformRevenueLine.KIND_CHOICES:
        if main_window["by_kind"].get(kind, 0):
            items.append({"label": lbl, "value": f"${main_window['by_kind'][kind]:,.0f}",
                          "action": "admin_gmv", "params": {}})

    period_rows = []
    for r in results:
        period_rows.append({
            "title": f"{r['label']}",
            "subtitle": f"${r['total']:,.0f} · {r['n']} строк",
            "action": "admin_gmv", "params": {},
        })

    return ActionResult(
        text=(
            f"💰 Декомпозиция дохода группы (ТЗ §15)\n"
            f"За 7 дней · ${main_window['total']:,.0f}"
        ),
        cards=[
            {"type": "kpi_grid", "data": {"title": "💰 Структура дохода (7d)",
                                            "items": items}},
            {"type": "list", "data": {"title": "📊 По периодам",
                                        "items": period_rows}},
        ],
        contextual_actions=[
            {"action": "admin_dashboard", "label": "← Сводка"},
            {"action": "admin_gmv", "label": "📈 GMV"},
        ],
    )


@register("admin_platform_settings")
def admin_platform_settings(params, user, role):
    err = _ensure_admin(role)
    if err: return err
    import os

    from .payments_engines import get_engine

    items = [
        {"label": "Payment engine", "value": get_engine().name,
         "tone": "info"},
        {"label": "STRIPE_SECRET_KEY",
         "value": "set" if os.getenv("STRIPE_SECRET_KEY") else "not set",
         "tone": "ok" if os.getenv("STRIPE_SECRET_KEY") else "warn"},
        {"label": "STRIPE_WEBHOOK_SECRET",
         "value": "set" if os.getenv("STRIPE_WEBHOOK_SECRET") else "not set"},
        {"label": "TELEGRAM_BOT_TOKEN",
         "value": "set" if os.getenv("TELEGRAM_BOT_TOKEN") else "not set"},
        {"label": "ANTHROPIC_API_KEY",
         "value": "set" if os.getenv("ANTHROPIC_API_KEY") else "not set"},
        {"label": "EMAIL backend",
         "value": "configured" if os.getenv("EMAIL_HOST") else "console (dev)"},
        {"label": "SITE_URL", "value": os.getenv("SITE_URL") or "(not set)"},
        {"label": "Channels layer",
         "value": "in-memory" if os.getenv("CHANNELS_INMEMORY") else "redis"},
    ]
    return ActionResult(
        text="🛠 Платформенные настройки (read-only).",
        cards=[{"type": "kpi_grid", "data": {"title": "🛠 Settings", "items": items}}],
        contextual_actions=[
            {"action": "admin_dashboard", "label": "← Сводка"},
        ],
    )


# ══════════════════════════════════════════════════════════
#  Цифровой слепок рынка — главный актив платформы (данные)
# ══════════════════════════════════════════════════════════

@register("admin_market_twin")
def admin_market_twin(params, user, role):
    """Цифровой слепок рынка: какие данные копятся, из каких источников,
    с каким покрытием. Это конечный продукт платформы для владельца."""
    err = _ensure_admin(role)
    if err:
        return err
    from django.contrib.auth import get_user_model
    from django.db.models import Count, Sum
    from marketplace.models import Part, Drawing, Order, Customer, Brand, Category
    from assistant.models import Project
    U = get_user_model()

    parts = Part.objects.count()
    try:
        from marketplace.models import PartRef
        partrefs = PartRef.objects.count()
    except Exception:
        partrefs = 0
    draw = Drawing.objects.count()
    orders = Order.objects.count()
    gmv = float(Order.objects.aggregate(s=Sum("total_amount"))["s"] or 0)
    custs = Customer.objects.count()
    projs = Project.objects.count()
    users = U.objects.count()
    brands = Brand.objects.count()
    cats = Category.objects.count()
    try:
        from marketplace.models import CustomsRecord
        customs = CustomsRecord.objects.count()
        customs_val = float(CustomsRecord.objects.aggregate(s=Sum("customs_value_usd"))["s"] or 0)
    except Exception:
        customs, customs_val = 0, 0

    def _n(v):
        return f"{int(v):,}".replace(",", " ")

    def _m(v):
        return ("$" + f"{float(v):,.0f}").replace(",", " ")

    def cov(n, rich):
        return "🟢" if n >= rich else ("🟡" if n > 0 else "⚪️")

    kpi = {"type": "kpi_grid", "data": {"title": "🌐 Цифровой слепок рынка", "kpis": [
        {"value": _n(parts), "label": "Артикулы (OEM)", "action": "admin_catalog_review", "params": {}},
        {"value": _n(partrefs), "label": "Кросс-ссылки", "sub": "граф аналогов", "action": "admin_catalog_review", "params": {}},
        {"value": _n(draw), "label": "Чертежи", "action": "op_drawings_by_part", "params": {}},
        {"value": _n(orders), "label": "Сделки", "sub": _m(gmv) + " GMV", "action": "admin_gmv", "params": {}},
        {"value": _n(custs), "label": "Заказчики", "action": "admin_customers", "params": {}},
        {"value": _n(projs), "label": "Парки техники", "action": "admin_fleets", "params": {}},
    ]}}

    # Активы рынка: что копится, источник, покрытие. Каждый → в свой раздел.
    assets = {"type": "list", "data": {"title": "📦 Активы данных (что копится) — нажмите", "rows": [
        {"title": f"{cov(parts, 100000)} Артикулы / OEM · {_n(parts)}",
         "subtitle": "спрос+предложение, цены, наличие · источник: продавцы (каталоги) + покупатели (поиск/RFQ)",
         "action": "admin_catalog_review", "params": {}},
        {"title": f"{cov(partrefs, 10000)} Кросс-ссылки аналогов · {_n(partrefs)}",
         "subtitle": "граф «оригинал↔аналог» · источник: импорт + привязки операторов/KAM",
         "action": "admin_catalog_review", "params": {}},
        {"title": f"{cov(draw, 1000)} Чертежи · {_n(draw)}",
         "subtitle": "тех. идентификация детали → точность поставки · источник: покупатели + продавцы + админ",
         "action": "op_drawings_by_part", "params": {}},
        {"title": f"{cov(orders, 1000)} Цены / маршруты / сроки · {_n(orders)} сделок",
         "subtitle": "реальные цены, логистика, таможня · источник: транзакции (самые ценные данные)",
         "action": "admin_gmv", "params": {}},
        {"title": f"{cov(custs, 100)} Заказчики + контакты · {_n(custs)}",
         "subtitle": "кто покупает, контакты ЛПР · источник: KAM (привлечение) + регистрации",
         "action": "admin_customers", "params": {}},
        {"title": f"{cov(projs, 100)} Парки техники + периодичность · {_n(projs)}",
         "subtitle": "что у клиента в эксплуатации + как часто закупает · источник: проекты/RFQ покупателей",
         "action": "admin_fleets", "params": {}},
        {"title": f"{cov(customs, 1000)} Таможенная аналитика · {_n(customs)} ({_m(customs_val)})",
         "subtitle": "реальные цены/объёмы ввоза по HS/странам · источник: ваш ручной засев",
         "action": "admin_customs", "params": {}},
    ]}}

    sources = {"type": "list", "data": {"title": "🔌 Источники обогащения — нажмите", "rows": [
        {"title": "👤 Покупатели", "subtitle": "RFQ, поиск, заказы, чертежи, парки техники — основной поток спроса",
         "action": "admin_users", "params": {"filter": "buyers"}},
        {"title": "🏭 Продавцы", "subtitle": "каталоги, цены, наличие, чертежи — основной поток предложения",
         "action": "admin_users", "params": {"filter": "sellers"}},
        {"title": "🛡 Операторы / KAM", "subtitle": "KYB, HS-коды, привязки аналогов, контакты, верификация",
         "action": "admin_moderation_queue", "params": {}},
        {"title": "🗂 Админ (вы)", "subtitle": "таможенная аналитика, чертежи, парт-номера — ручной засев",
         "action": "admin_customs", "params": {}},
    ]}}

    geo = list(Customer.objects.values("country").annotate(c=Count("id")).order_by("-c")[:10])
    geo_rows = [{"title": (g["country"] or "—"), "subtitle": f"{g['c']} заказчиков",
                 "action": "admin_customers", "params": {}} for g in geo] or \
               [{"title": "Пока нет данных по странам", "subtitle": "наполнится с заказчиками"}]
    geography = {"type": "list", "data": {"title": "🗺 География (по странам)", "rows": geo_rows}}

    roadmap = {"type": "list", "data": {"title": "🧭 Заложить под наполнение (архитектура)", "rows": [
        {"title": "Таможенная аналитика (импорт)", "subtitle": "HS-коды, объёмы, цены ввоза — отдельная модель + загрузчик", "tone": "info"},
        {"title": "История цен по артикулу", "subtitle": "цена/время/поставщик/регион → тренды и бенчмарк", "tone": "info"},
        {"title": "Структурный парк техники", "subtitle": "модель/серийник/наработка → предиктивный спрос", "tone": "info"},
        {"title": "Контакты ключевых поставщиков", "subtitle": "рейтинг надёжности, сроки, ниши", "tone": "info"},
    ]}}

    return ActionResult(
        text=(f"🌐 Цифровой слепок рынка. Артикулы {_n(parts)}, кросс-ссылки {_n(partrefs)}, "
              f"чертежи {_n(draw)}, сделки {_n(orders)} ({_m(gmv)} GMV). Это и есть конечный "
              "продукт: рынок копится из действий пользователей; вы засеваете таможню/чертежи/парт-номера."),
        cards=[kpi, assets, sources, geography, roadmap],
        contextual_actions=[
            {"action": "admin_dashboard", "label": "← Сводка"},
            {"action": "admin_catalog_review", "label": "📦 Каталог"},
        ],
    )


# ══════════════════════════════════════════════════════════
#  Таможенная аналитика — ручной засев администратора
# ══════════════════════════════════════════════════════════

@register("admin_customs")
def admin_customs(params, user, role):
    """Обзор таможенной аналитики: объём данных, топ HS/страны, последние записи."""
    err = _ensure_admin(role)
    if err:
        return err
    from django.db.models import Count, Sum
    from marketplace.models import CustomsRecord
    qs = CustomsRecord.objects.all()
    total = qs.count()
    value = float(qs.aggregate(s=Sum("customs_value_usd"))["s"] or 0)
    weight = float(qs.aggregate(s=Sum("net_weight_kg"))["s"] or 0)
    linked = qs.exclude(oem_number="").count()

    def _n(v):
        return f"{int(v):,}".replace(",", " ")

    def _m(v):
        return ("$" + f"{float(v):,.0f}").replace(",", " ")

    add_btn = {"action": "admin_customs_add", "label": "➕ Добавить запись", "params": {}}
    if not total:
        return ActionResult(
            text="🛂 Таможенная аналитика пуста. Засейте первую запись — это уникальный пласт "
                 "данных (реальные цены/объёмы ввоза), которого нет у пользователей.",
            contextual_actions=[add_btn, {"action": "admin_market_twin", "label": "← Слепок рынка"}],
        )
    top_hs = list(qs.values("hs_code").annotate(c=Count("id"), v=Sum("customs_value_usd")).order_by("-v")[:8])
    top_co = list(qs.values("origin_country").annotate(c=Count("id"), v=Sum("customs_value_usd")).order_by("-v")[:8])
    recent = list(qs[:15])

    kpi = {"type": "kpi_grid", "data": {"title": "🛂 Таможенная аналитика", "kpis": [
        {"value": _n(total), "label": "Записей"},
        {"value": _m(value), "label": "Стоимость ввоза"},
        {"value": _n(weight) + " кг", "label": "Вес"},
        {"value": _n(linked), "label": "С парт-номером", "sub": "связь с графом"},
    ]}}
    hs_rows = [{"title": f"HS {h['hs_code']}", "subtitle": f"{h['c']} записей · {_m(h['v'])}"} for h in top_hs]
    co_rows = [{"title": (c["origin_country"] or "—"), "subtitle": f"{c['c']} записей · {_m(c['v'])}"} for c in top_co]
    rec_rows = [{
        "title": f"HS {r.hs_code} · {r.origin_country or '—'}→{r.dest_country}",
        "subtitle": (f"{r.commodity or ''} · {_m(r.customs_value_usd)}"
                     + (f" · OEM {r.oem_number}" if r.oem_number else "")),
    } for r in recent]
    return ActionResult(
        text=f"🛂 Таможенная аналитика: {_n(total)} записей, {_m(value)} ввоза, {_n(linked)} с парт-номером.",
        cards=[
            kpi,
            {"type": "list", "data": {"title": "📊 Топ HS-кодов по стоимости", "rows": hs_rows}},
            {"type": "list", "data": {"title": "🗺 Топ стран происхождения", "rows": co_rows}},
            {"type": "list", "data": {"title": "🕒 Последние записи", "rows": rec_rows}},
        ],
        contextual_actions=[add_btn, {"action": "admin_market_twin", "label": "← Слепок рынка"}],
    )


@register("admin_customs_add")
def admin_customs_add(params, user, role):
    """Добавить запись таможенной аналитики (ручной засев)."""
    err = _ensure_admin(role)
    if err:
        return err
    from marketplace.models import CustomsRecord
    hs = (params.get("hs_code") or "").strip()
    if not hs:
        return ActionResult(
            text="Добавьте запись таможенной аналитики. Привяжите парт-номер (OEM) — "
                 "тогда реальная цена ввоза попадёт в граф рынка по этому артикулу.",
            cards=[{"type": "form", "data": {
                "title": "🛂 Новая запись таможни",
                "submit_action": "admin_customs_add",
                "submit_label": "Добавить",
                "fields": [
                    {"name": "hs_code", "label": "ТН ВЭД / HS", "type": "text", "required": True, "placeholder": "8431499900"},
                    {"name": "commodity", "label": "Товар", "type": "text", "placeholder": "Башмак гусеницы CAT"},
                    {"name": "oem_number", "label": "Парт-номер (OEM)", "type": "text", "placeholder": "1R-0750 (опц., связь с графом)"},
                    {"name": "origin_country", "label": "Страна происхождения (2 буквы)", "type": "text", "placeholder": "CN"},
                    {"name": "supplier", "label": "Поставщик/отправитель", "type": "text", "placeholder": "опц."},
                    {"name": "importer", "label": "Импортёр", "type": "text", "placeholder": "опц."},
                    {"name": "qty", "label": "Кол-во", "type": "text", "placeholder": "0"},
                    {"name": "net_weight_kg", "label": "Вес, кг", "type": "text", "placeholder": "0"},
                    {"name": "customs_value_usd", "label": "Стоимость ввоза, USD", "type": "text", "placeholder": "0"},
                ],
                "fixed_params": {},
            }}],
        )

    def _num(k):
        try:
            return float((params.get(k) or "0").replace(" ", "").replace(",", "."))
        except Exception:
            return 0

    rec = CustomsRecord.objects.create(
        hs_code=hs[:14],
        commodity=(params.get("commodity") or "").strip()[:300],
        oem_number=(params.get("oem_number") or "").strip()[:100],
        origin_country=(params.get("origin_country") or "").strip().upper()[:2],
        supplier=(params.get("supplier") or "").strip()[:255],
        importer=(params.get("importer") or "").strip()[:255],
        qty=_num("qty"), net_weight_kg=_num("net_weight_kg"),
        customs_value_usd=_num("customs_value_usd"),
        source="manual", created_by=user,
    )
    msg = f"✅ Запись добавлена: HS {rec.hs_code}"
    if rec.oem_number:
        msg += f" · привязана к OEM {rec.oem_number} (обогатила граф)"
    return ActionResult(
        text=msg + ".",
        contextual_actions=[
            {"action": "admin_customs_add", "label": "➕ Ещё запись", "params": {}},
            {"action": "admin_customs", "label": "🛂 К таможне", "params": {}},
        ],
    )


@register("admin_customers")
def admin_customers(params, user, role):
    """Все заказчики платформы (контакты, привязка к KAM, статус)."""
    err = _ensure_admin(role)
    if err:
        return err
    from marketplace.models import Customer
    total = Customer.objects.count()
    qs = Customer.objects.select_related("owner", "user").order_by("-created_at")[:50]
    rows = []
    for c in qs:
        kam = (c.owner.get_full_name() or c.owner.username) if c.owner_id else "—"
        confirmed = "✅ подтв." if c.user_id else "⚪️ лид"
        contact = " · ".join([x for x in [c.contact_name, c.phone] if x]) or "контактов нет"
        rows.append({"title": f"{c.name} · ИНН {c.inn}",
                     "subtitle": f"{confirmed} · KAM {kam} · {contact}",
                     "action": "customer_detail", "params": {"id": str(c.id)}})
    return ActionResult(
        text=f"👥 Заказчики платформы: {total}. Контакты ЛПР, привязка к KAM, статус закрепления.",
        cards=[{"type": "list", "data": {"title": f"Заказчики ({total})",
                "rows": rows or [{"title": "Пока нет заказчиков", "subtitle": "появятся с привлечением KAM"}]}}],
        contextual_actions=[{"action": "admin_market_twin", "label": "← Слепок рынка"}],
    )


@register("admin_fleets")
def admin_fleets(params, user, role):
    """Парки техники / проекты клиентов — что в эксплуатации (предиктивный спрос)."""
    err = _ensure_admin(role)
    if err:
        return err
    from assistant.models import Project
    total = Project.objects.count()
    qs = Project.objects.select_related("owner").order_by("-updated_at")[:50]
    rows = []
    for p in qs:
        owner = (p.owner.get_full_name() or p.owner.username) if p.owner_id else "—"
        tags = ", ".join((p.tags or [])[:6]) if p.tags else "—"
        rows.append({"title": p.name, "subtitle": f"парк/теги: {tags} · владелец {owner}",
                     "url": f"/chat/project/{p.id}/"})
    return ActionResult(
        text=f"🚜 Парки техники / проекты: {total}. Что у клиентов в эксплуатации → предиктивный спрос.",
        cards=[{"type": "list", "data": {"title": f"Парки / проекты ({total})",
                "rows": rows or [{"title": "Пока нет", "subtitle": "появятся из проектов покупателей"}]}}],
        contextual_actions=[{"action": "admin_market_twin", "label": "← Слепок рынка"}],
    )

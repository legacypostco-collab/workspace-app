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
from django.utils.translation import gettext as _

from .actions import ActionResult, _notify, register
from .security import confirmation_is_true

logger = logging.getLogger(__name__)


def _is_admin(role: str) -> bool:
    return role == "admin"


def _ensure_admin(role: str):
    if not _is_admin(role):
        return ActionResult(
            text=_("🔒 Только администратор платформы может выполнять это действие."),
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

    gmv_7d_fmt = f"{gmv_7d:,.0f}"
    return ActionResult(
        text=(
            _("🛡 Платформа · %(users)s активных юзеров (+%(new)s за неделю) · "
              "%(orders)s заказов всего · GMV за 7 дней $%(gmv)s.")
            % {"users": users_total, "new": users_new_7d,
               "orders": orders_total, "gmv": gmv_7d_fmt}
        ),
        cards=[
            {"type": "kpi_grid", "data": {"title": _("🛡 Admin · Сводка"), "items": [
                {"label": _("Активных юзеров"), "value": str(users_total), "tone": "info",
                 "action": "admin_users", "params": {}},
                {"label": _("Новых за 7 дней"), "value": f"+{users_new_7d}",
                 "tone": "ok" if users_new_7d else "warn",
                 "action": "admin_users", "params": {"filter": "new"}},
                {"label": _("Заказов всего"), "value": str(orders_total),
                 "action": "admin_gmv", "params": {}},
                {"label": _("За 24 часа"), "value": str(orders_24h),
                 "action": "admin_gmv", "params": {}},
                {"label": _("В работе"), "value": str(open_orders), "tone": "info",
                 "action": "admin_gmv", "params": {}},
                {"label": "GMV 7d", "value": f"${gmv_7d:,.0f}", "tone": "ok",
                 "action": "admin_revenue_breakdown", "params": {}},
                {"label": "SLA breach 7d", "value": str(sla_breached_7d),
                 "tone": "bad" if sla_breached_7d > 0 else "ok",
                 "action": "admin_moderation_queue", "params": {}},
                {"label": _("RFQ за 24ч"), "value": str(rfq_24h),
                 "action": "admin_market_twin", "params": {}},
                {"label": "KYB pending", "value": str(kyb_pending),
                 "tone": "warn" if kyb_pending else "ok",
                 "action": "admin_moderation_queue", "params": {}},
                {"label": "KYB verified", "value": str(kyb_verified),
                 "action": "admin_users", "params": {"filter": "verified"}},
                {"label": _("Загрузки прайса 7д"), "value": str(pl_uploads_7d),
                 "tone": "info", "action": "admin_activity_feed",
                 "params": {"kind": "pricelist"}},
                {"label": _("Позиций 7д"), "value": f"{pl_positions_7d:,}",
                 "action": "admin_activity_feed", "params": {"kind": "pricelist"}},
            ]}},
        ],
        contextual_actions=[
            {"action": "admin_activity_feed", "label": _("🛰 Лента событий")},
            {"action": "admin_gmv", "label": _("📈 GMV-разбивка")},
            {"action": "admin_moderation_queue", "label": _("🚨 Модерация")},
            {"action": "admin_users", "label": _("👥 Пользователи")},
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
        (_("24 часа"), timedelta(hours=24)),
        (_("7 дней"), timedelta(days=7)),
        (_("30 дней"), timedelta(days=30)),
        (_("90 дней"), timedelta(days=90)),
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
        gmv_fmt = f"{gmv:,.0f}"
        items.append({"label": label,
                       "value": _("$%(gmv)s · %(n)s заказ.") % {"gmv": gmv_fmt, "n": n},
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
            _("📈 Платформенный GMV (только paid/refunded, без отменённых).")
        ),
        cards=[
            {"type": "kpi_grid", "data": {"title": _("💰 GMV по периодам"), "items": items}},
            {"type": "list", "data": {"title": _("🏆 Топ категорий по GMV"),
                                       "items": cat_rows or [{"title": _("Нет данных")}]}},
        ],
        contextual_actions=[
            {"action": "admin_dashboard", "label": _("← Сводка")},
            {"action": "op_payments_dashboard", "label": _("💰 Эскроу")},
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
    GROUPS = [("buyer", _("👤 Покупатели")), ("seller", _("🏭 Продавцы")),
              ("operator", _("🛠 Операторы")), ("admin", _("⚡ Админы"))]
    PER = 40
    buckets = {k: [] for k, _label in GROUPS}
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
            items.append({"title": _("… ещё %(rest)s (показаны первые %(per)s)")
                          % {"rest": len(bucket) - PER, "per": PER}})
        cards.append({"type": "list", "data": {
            "title": f"{title} · {len(bucket)}", "items": items,
            "collapsible": True}})   # изначально свёрнуто, раскрытие по клику
    if not cards:
        cards = [{"type": "list", "data": {"title": _("👥 Пользователи"),
                                            "items": [{"title": _("Пусто")}]}}]

    return ActionResult(
        text=_("👥 Пользователи · фильтр «%(flt)s» · %(total)s в %(groups)s группах.")
             % {"flt": flt, "total": total, "groups": len(cards)},
        cards=cards,
        contextual_actions=[
            {"action": "admin_users", "label": _("Все"),       "params": {"filter": "all"}},
            {"action": "admin_users", "label": _("Активные"),  "params": {"filter": "active"}},
            {"action": "admin_users", "label": _("Заблокир."), "params": {"filter": "banned"}},
            {"action": "admin_users", "label": _("👤 Покупатели"),"params": {"filter": "buyers"}},
            {"action": "admin_users", "label": _("🏭 Продавцы"),  "params": {"filter": "sellers"}},
            {"action": "admin_users", "label": _("🛠 Операторы"), "params": {"filter": "operators"}},
            {"action": "admin_users", "label": "KYB pending","params": {"filter": "kyb_pending"}},
        ],
    )


# ══════════════════════════════════════════════════════════
# 3b. Лента важных событий — контроль/безопасность
# ══════════════════════════════════════════════════════════

_ACTIVITY_EMOJI = {"order": "🛒", "rfq": "📋", "pricelist": "📦"}
_ACTIVITY_LABEL = {"order": _("Заказ"), "rfq": "RFQ", "pricelist": _("Загрузка прайса")}


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
        actor_name = ev.actor.username if ev.actor else _("гость / аноним")
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
        text=_("🛰 Лента событий · фильтр «%(flt)s» · %(n)s последних.")
             % {"flt": flt, "n": len(rows)},
        cards=[{"type": "list", "data": {
            "title": _("🛰 Важные события (сделки · RFQ · загрузки)"),
            "items": rows or [{"title": _("Пока событий нет")}],
        }}],
        contextual_actions=[
            {"action": "admin_activity_feed", "label": _("🔄 Обновить"), "params": {"kind": flt}},
            {"action": "admin_activity_feed", "label": _("Все"),       "params": {"kind": "all"}},
            {"action": "admin_activity_feed", "label": _("🛒 Заказы"),  "params": {"kind": "order"}},
            {"action": "admin_activity_feed", "label": "📋 RFQ",      "params": {"kind": "rfq"}},
            {"action": "admin_activity_feed", "label": _("📦 Прайсы"),   "params": {"kind": "pricelist"}},
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
        return ActionResult(text=_("Пользователь не найден."))

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
    groups.append({"title": _("👤 Аккаунт"), "rows": [
        {"label": "Username", "value": u.username, "primary": True},
        {"label": "Email", "value": u.email or "—"},
        {"label": _("Роль"), "value": role_val},
        {"label": _("Статус"), "value": _("🚫 Заблокирован") if not u.is_active else _("✓ Активен"),
         "primary": not u.is_active},
        {"label": _("Привилегии"), "value": priv},
        {"label": _("Зарегистрирован"), "value": _dt(u.date_joined, "%Y-%m-%d")},
        {"label": _("Последний вход"), "value": _dt(u.last_login)},
        {"label": _("Язык"), "value": (getattr(prof, "language", "") or "—") if prof else "—"},
        {"label": _("AI-кредиты"), "value": str(getattr(prof, "ai_credits", "—")) if prof else "—"},
    ]})
    if prof:
        groups.append({"title": _("📇 Контакты"), "rows": [
            {"label": _("Контактное лицо"), "value": prof.contact_name or "—"},
            {"label": _("Должность"), "value": prof.position or "—"},
            {"label": _("Телефон"), "value": prof.phone_e164 or "—"},
            {"label": _("Компания"), "value": prof.company_name or "—"},
            {"label": _("Мессенджер"), "value": (f"{prof.messenger_kind}: {prof.messenger_handle}"
                                              if prof.messenger_handle else "—")},
            {"label": _("Уведомления"), "value": "email " + ("✓" if prof.notif_email_enabled else "✗")
                + " · TG " + ("✓" if prof.notif_telegram_enabled else "✗")},
        ]})
    if kyb:
        docs = " · ".join(filter(None, [
            _("Устав") if kyb.doc_charter else "", _("ЕГРЮЛ") if kyb.doc_egrul else "",
            _("Паспорт") if kyb.doc_passport else ""])) or _("нет")
        groups.append({"title": _("🏢 Компания / KYB"), "rows": [
            {"label": _("Статус KYB"), "value": kyb.get_status_display()},
            {"label": _("Юр. название"), "value": kyb.legal_name or "—"},
            {"label": _("Страна"), "value": kyb.country or "—"},
            {"label": _("ИНН"), "value": kyb.inn or "—"},
            {"label": _("КПП"), "value": kyb.kpp or "—"},
            {"label": _("ОГРН"), "value": kyb.ogrn or "—"},
            {"label": "VAT", "value": kyb.vat_number or "—"},
            {"label": _("Директор"), "value": kyb.director_name or "—"},
            {"label": _("Банк"), "value": kyb.bank_name or "—"},
            {"label": _("Документы"), "value": docs},
        ]})
    else:
        groups.append({"title": _("🏢 Компания / KYB"),
                       "rows": [{"label": "KYB", "value": _("не подавалась")}]})
    fin_rows = [
        {"label": _("Баланс кошелька"),
         "value": f"${wallet.balance:,.2f} {wallet.currency}" if wallet else "—"},
        {"label": _("Оплачено заказов"), "value": f"${float(paid_sum):,.2f}"},
        {"label": _("Пополнений"), "value": str(topups_n)},
    ]
    if last_topup:
        topup_amount = f"{last_topup.amount:,.0f}"
        fin_rows.append({"label": _("Последнее пополнение"),
                         "value": _("$%(amount)s · %(status)s · %(date)s")
                                  % {"amount": topup_amount,
                                     "status": last_topup.get_status_display(),
                                     "date": _dt(last_topup.created_at, '%Y-%m-%d')}})
    groups.append({"title": _("💰 Финансы"), "rows": fin_rows})
    groups.append({"title": _("📦 Заказы и заявки"), "rows": [
        {"label": _("Как покупатель"),
         "value": _("%(paid)s оплачено / %(total)s всего")
                  % {"paid": orders_paid, "total": orders_n}},
        {"label": _("Как продавец (продажи)"), "value": str(sales_orders)},
        {"label": _("RFQ создано"), "value": str(rfq_n)},
    ]})
    if prof and (role_val.startswith("seller") or pl_n or sales_orders):
        flags = []
        if getattr(prof, "bankruptcy_flag", False): flags.append(_("⚠ банкротство"))
        if getattr(prof, "liquidation_flag", False): flags.append(_("⚠ ликвидация"))
        groups.append({"title": _("🏭 Продавец-метрики"), "rows": [
            {"label": _("Статус поставщика"), "value": prof.supplier_status or "—"},
            {"label": _("Рейтинг"), "value": f"{float(prof.rating_score):.1f}"
                if prof.rating_score is not None else "—"},
            {"label": _("Внешний / поведенческий"),
             "value": f"{float(prof.external_score or 0):.0f} / {float(prof.behavioral_score or 0):.0f}"},
            {"label": _("Флаги"), "value": " · ".join(flags) if flags else "—"},
            {"label": _("Загрузок прайса"), "value": str(pl_n)},
            {"label": _("Позиций импортировано"), "value": f"{pl_positions:,}"},
        ]})
    groups.append({"title": _("🛰 Активность / безопасность"), "rows": [
        {"label": _("IP (последние)"), "value": ", ".join(uniq_ips[:3]) or "—"},
        {"label": _("Событий в ленте"), "value": str(ev_n)},
        {"label": _("Претензий открыто"), "value": str(claims_n)},
        {"label": _("Admin-заметка"), "value": (getattr(prof, "admin_note", "") or "—") if prof else "—"},
    ]})

    actions = []
    if u.is_active and not u.is_superuser:
        actions.append({"action": "admin_ban_user", "label": _("🚫 Заблокировать"),
                        "params": {"user_id": u.id}})
    elif not u.is_active:
        actions.append({"action": "admin_unban_user", "label": _("✓ Разблокировать"),
                        "params": {"user_id": u.id}})

    # Кликабельные ссылки на сами заявки пользователя (заказы + RFQ), чтобы
    # админ мог открыть и проверить позиции — а не только видеть счётчик.
    from marketplace.models import RFQ
    open_btns = []
    for oid in (Order.objects.filter(buyer=u).order_by("-created_at")
                .values_list("id", flat=True)[:6]):
        open_btns.append({"action": "get_order_detail",
                          "label": _("📦 Заказ ORD-%(id)s") % {"id": oid},
                          "params": {"order_id": oid}})
    for rid in (RFQ.objects.filter(created_by=u).order_by("-created_at")
                .values_list("id", flat=True)[:6]):
        open_btns.append({"action": "get_rfq_status",
                          "label": f"📋 RFQ #{rid}",
                          "params": {"rfq_id": rid}})

    return ActionResult(
        text=_("👤 %(username)s · %(email)s")
             % {"username": u.username, "email": u.email or _("нет email")},
        cards=[{"type": "draft", "data": {"title": _("Профиль · %(username)s") % {"username": u.username},
                                           "groups": groups, "confirm_label": "—"}}],
        actions=actions,
        contextual_actions=open_btns + [
            {"action": "admin_users", "label": _("← К списку")},
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
        return ActionResult(text=_("Пользователь не найден."))

    if target.id == user.id:
        return ActionResult(text=_("⚠️ Нельзя заблокировать самого себя."))
    if target.is_superuser:
        return ActionResult(text=_("⚠️ Заблокировать админа нельзя."))
    if not target.is_active:
        return ActionResult(text=_("Пользователь %(username)s уже заблокирован.")
                                 % {"username": target.username})

    reason = (params.get("reason") or "").strip()
    confirmed = confirmation_is_true(params.get("confirmed"))
    if not confirmed or not reason:
        return ActionResult(
            text=_("Заблокировать %(username)s?") % {"username": target.username},
            cards=[{"type": "form", "data": {
                "title": _("🚫 Заблокировать · %(username)s") % {"username": target.username},
                "submit_action": "admin_ban_user",
                "fields": [
                    {"name": "reason", "label": _("Причина (для аудита и нотификации)"),
                     "type": "textarea", "required": True},
                ],
                "fixed_params": {"user_id": target.id, "confirmed": True},
            }}],
        )

    target.is_active = False
    target.save(update_fields=["is_active"])
    _notify(target, kind="system",
            title=_("Аккаунт заблокирован"),
            body=_("Платформа заблокировала ваш аккаунт. Причина: %(reason)s")
                 % {"reason": reason[:200]},
            url="")
    return ActionResult(
        text=_("🚫 %(username)s заблокирован. Причина: %(reason)s")
             % {"username": target.username, "reason": reason[:120]},
        contextual_actions=[
            {"action": "admin_user_detail", "label": _("← Профиль"),
             "params": {"user_id": target.id}},
            {"action": "admin_users", "label": _("Все юзеры")},
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
        return ActionResult(text=_("Пользователь не найден."))
    if target.is_active:
        return ActionResult(text=_("%(username)s не заблокирован.")
                                 % {"username": target.username})

    if not confirmation_is_true(params.get("confirmed")):
        return ActionResult(
            text=_("Разблокировать %(username)s?") % {"username": target.username},
            cards=[{"type": "draft", "data": {
                "title": _("✓ Разблокировать · %(username)s") % {"username": target.username},
                "rows": [{"label": _("Юзер"), "value": target.username, "primary": True}],
                "confirm_action": "admin_unban_user",
                "confirm_label": _("✓ Разблокировать"),
                "confirm_params": {"user_id": target.id, "confirmed": True},
                "cancel_label": _("Отмена"),
            }}],
        )

    target.is_active = True
    target.save(update_fields=["is_active"])
    _notify(target, kind="system",
            title=_("Аккаунт разблокирован"),
            body=_("Платформа восстановила доступ к вашему аккаунту."),
            url="")
    return ActionResult(
        text=_("✓ %(username)s разблокирован.") % {"username": target.username},
        contextual_actions=[
            {"action": "admin_user_detail", "label": _("← Профиль"),
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
        {"title": _("KYB на проверке · %(n)s") % {"n": kyb_pending},
         "subtitle": _("Анкеты ждут верификации — нажмите"), "tone": "warn",
         "action": "op_kyb_queue", "params": {}} if kyb_pending else None,
        {"title": _("Возвраты в обработке · %(n)s") % {"n": refunds},
         "subtitle": _("Заказы на возврат — нажмите"), "tone": "warn",
         "action": "op_queue", "params": {"filter": "refund"}} if refunds else None,
        {"title": _("SLA нарушены · %(n)s") % {"n": sla_breached},
         "subtitle": _("Просрочки по этапам — нажмите"), "tone": "bad",
         "action": "op_sla_breach", "params": {}} if sla_breached else None,
        {"title": _("Контр-офферы ждут ответа · %(n)s") % {"n": quotes_countered},
         "subtitle": _("Переторжка по КП — нажмите"), "tone": "info",
         "action": "op_queue", "params": {}} if quotes_countered else None,
    ]
    items = [x for x in items if x]
    if not items:
        items = [{"title": _("✓ Очередь пуста"), "subtitle": _("Всё под контролем")}]

    return ActionResult(
        text=_("🚨 Платформенная очередь модерации"),
        cards=[{"type": "list", "data": {"title": _("🚨 Модерация платформы"),
                                          "items": items}}],
        contextual_actions=[
            {"action": "op_kyb_queue", "label": "🛡 KYB"},
            {"action": "op_queue", "label": _("📋 Заказы (operator)")},
            {"action": "admin_dashboard", "label": _("← Сводка")},
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
         "subtitle": _("нет seller'а — orphan record")}
        for p in no_seller
    ]
    recent_rows = [
        {"title": f"#{p.id} {p.title[:50]}",
         "subtitle": f"${p.price or 0} · {p.seller.username if p.seller else '—'}"}
        for p in recent
    ]

    return ActionResult(
        text=(
            _("📦 Каталог · %(susp)s с ценой $0 · %(noseller)s без продавца "
              "· %(recent)s новых.")
            % {"susp": len(susp_rows), "noseller": len(no_seller_rows),
               "recent": len(recent_rows)}
        ),
        cards=[
            {"type": "list", "data": {"title": _("⚠️ Цена = $0"),
                "items": susp_rows or [{"title": _("Чисто")}]}},
            {"type": "list", "data": {"title": _("⚠️ Без продавца"),
                "items": no_seller_rows or [{"title": _("Чисто")}]}},
            {"type": "list", "data": {"title": _("🆕 Последние добавленные"),
                "items": recent_rows or [{"title": "—"}]}},
        ],
        contextual_actions=[
            {"action": "admin_dashboard", "label": _("← Сводка")},
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
    windows = [(_("24 часа"), 1), (_("7 дней"), 7), (_("30 дней"), 30), (_("90 дней"), 90)]
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
        {"label": _("За 7 дней TOTAL"), "value": f"${main_window['total']:,.0f}",
         "tone": "info", "action": "admin_gmv", "params": {}},
    ]
    for kind, lbl in PlatformRevenueLine.KIND_CHOICES:
        if main_window["by_kind"].get(kind, 0):
            items.append({"label": lbl, "value": f"${main_window['by_kind'][kind]:,.0f}",
                          "action": "admin_gmv", "params": {}})

    period_rows = []
    for r in results:
        period_total = f"{r['total']:,.0f}"
        period_rows.append({
            "title": f"{r['label']}",
            "subtitle": _("$%(total)s · %(n)s строк") % {"total": period_total, "n": r['n']},
            "action": "admin_gmv", "params": {},
        })

    main_total_fmt = f"{main_window['total']:,.0f}"
    return ActionResult(
        text=(
            _("💰 Декомпозиция дохода группы (ТЗ §15)\n"
              "За 7 дней · $%(total)s") % {"total": main_total_fmt}
        ),
        cards=[
            {"type": "kpi_grid", "data": {"title": _("💰 Структура дохода (7d)"),
                                            "items": items}},
            {"type": "list", "data": {"title": _("📊 По периодам"),
                                        "items": period_rows}},
        ],
        contextual_actions=[
            {"action": "admin_dashboard", "label": _("← Сводка")},
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
        text=_("🛠 Платформенные настройки (read-only)."),
        cards=[{"type": "kpi_grid", "data": {"title": "🛠 Settings", "items": items}}],
        contextual_actions=[
            {"action": "admin_dashboard", "label": _("← Сводка")},
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

    kpi = {"type": "kpi_grid", "data": {"title": _("🌐 Цифровой слепок рынка"), "kpis": [
        {"value": _n(parts), "label": _("Артикулы (OEM)"), "action": "admin_catalog_review", "params": {}},
        {"value": _n(partrefs), "label": _("Кросс-ссылки"), "sub": _("граф аналогов"), "action": "admin_catalog_review", "params": {}},
        {"value": _n(draw), "label": _("Чертежи"), "action": "op_drawings_by_part", "params": {}},
        {"value": _n(orders), "label": _("Сделки"), "sub": _m(gmv) + " GMV", "action": "admin_gmv", "params": {}},
        {"value": _n(custs), "label": _("Заказчики"), "action": "admin_customers", "params": {}},
        {"value": _n(projs), "label": _("Парки техники"), "action": "admin_fleets", "params": {}},
    ]}}

    # Активы рынка: что копится, источник, покрытие. Каждый → в свой раздел.
    assets = {"type": "list", "data": {"title": _("📦 Активы данных (что копится) — нажмите"), "rows": [
        {"title": _("%(cov)s Артикулы / OEM · %(n)s") % {"cov": cov(parts, 100000), "n": _n(parts)},
         "subtitle": _("спрос+предложение, цены, наличие · источник: продавцы (каталоги) + покупатели (поиск/RFQ)"),
         "action": "admin_catalog_review", "params": {}},
        {"title": _("%(cov)s Кросс-ссылки аналогов · %(n)s") % {"cov": cov(partrefs, 10000), "n": _n(partrefs)},
         "subtitle": _("граф «оригинал↔аналог» · источник: импорт + привязки операторов/KAM"),
         "action": "admin_catalog_review", "params": {}},
        {"title": _("%(cov)s Чертежи · %(n)s") % {"cov": cov(draw, 1000), "n": _n(draw)},
         "subtitle": _("тех. идентификация детали → точность поставки · источник: покупатели + продавцы + админ"),
         "action": "op_drawings_by_part", "params": {}},
        {"title": _("%(cov)s Цены / маршруты / сроки · %(n)s сделок") % {"cov": cov(orders, 1000), "n": _n(orders)},
         "subtitle": _("реальные цены, логистика, таможня · источник: транзакции (самые ценные данные)"),
         "action": "admin_gmv", "params": {}},
        {"title": _("%(cov)s Заказчики + контакты · %(n)s") % {"cov": cov(custs, 100), "n": _n(custs)},
         "subtitle": _("кто покупает, контакты ЛПР · источник: KAM (привлечение) + регистрации"),
         "action": "admin_customers", "params": {}},
        {"title": _("%(cov)s Парки техники + периодичность · %(n)s") % {"cov": cov(projs, 100), "n": _n(projs)},
         "subtitle": _("что у клиента в эксплуатации + как часто закупает · источник: проекты/RFQ покупателей"),
         "action": "admin_fleets", "params": {}},
        {"title": _("%(cov)s Таможенная аналитика · %(n)s (%(val)s)") % {"cov": cov(customs, 1000), "n": _n(customs), "val": _m(customs_val)},
         "subtitle": _("реальные цены/объёмы ввоза по HS/странам · источник: ваш ручной засев"),
         "action": "admin_customs", "params": {}},
    ]}}

    sources = {"type": "list", "data": {"title": _("🔌 Источники обогащения — нажмите"), "rows": [
        {"title": _("👤 Покупатели"), "subtitle": _("RFQ, поиск, заказы, чертежи, парки техники — основной поток спроса"),
         "action": "admin_users", "params": {"filter": "buyers"}},
        {"title": _("🏭 Продавцы"), "subtitle": _("каталоги, цены, наличие, чертежи — основной поток предложения"),
         "action": "admin_users", "params": {"filter": "sellers"}},
        {"title": _("🛡 Операторы / KAM"), "subtitle": _("KYB, HS-коды, привязки аналогов, контакты, верификация"),
         "action": "admin_moderation_queue", "params": {}},
        {"title": _("🗂 Админ (вы)"), "subtitle": _("таможенная аналитика, чертежи, парт-номера — ручной засев"),
         "action": "admin_customs", "params": {}},
    ]}}

    geo = list(Customer.objects.values("country").annotate(c=Count("id")).order_by("-c")[:10])
    geo_rows = [{"title": (g["country"] or "—"), "subtitle": _("%(c)s заказчиков") % {"c": g['c']},
                 "action": "admin_customers", "params": {}} for g in geo] or \
               [{"title": _("Пока нет данных по странам"), "subtitle": _("наполнится с заказчиками")}]
    geography = {"type": "list", "data": {"title": _("🗺 География (по странам)"), "rows": geo_rows}}

    roadmap = {"type": "list", "data": {"title": _("🧭 Заложить под наполнение (архитектура)"), "rows": [
        {"title": _("Таможенная аналитика (импорт)"), "subtitle": _("HS-коды, объёмы, цены ввоза — отдельная модель + загрузчик"), "tone": "info"},
        {"title": _("История цен по артикулу"), "subtitle": _("цена/время/поставщик/регион → тренды и бенчмарк"), "tone": "info"},
        {"title": _("Структурный парк техники"), "subtitle": _("модель/серийник/наработка → предиктивный спрос"), "tone": "info"},
        {"title": _("Контакты ключевых поставщиков"), "subtitle": _("рейтинг надёжности, сроки, ниши"), "tone": "info"},
    ]}}

    return ActionResult(
        text=(_("🌐 Цифровой слепок рынка. Артикулы %(parts)s, кросс-ссылки %(refs)s, "
                "чертежи %(draw)s, сделки %(orders)s (%(gmv)s GMV). Это и есть конечный "
                "продукт: рынок копится из действий пользователей; вы засеваете таможню/чертежи/парт-номера.")
              % {"parts": _n(parts), "refs": _n(partrefs), "draw": _n(draw),
                 "orders": _n(orders), "gmv": _m(gmv)}),
        cards=[kpi, assets, sources, geography, roadmap],
        contextual_actions=[
            {"action": "admin_dashboard", "label": _("← Сводка")},
            {"action": "admin_catalog_review", "label": _("📦 Каталог")},
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

    add_btn = {"action": "admin_customs_add", "label": _("➕ Добавить запись"), "params": {}}
    if not total:
        return ActionResult(
            text=_("🛂 Таможенная аналитика пуста. Засейте первую запись — это уникальный пласт "
                   "данных (реальные цены/объёмы ввоза), которого нет у пользователей."),
            contextual_actions=[add_btn, {"action": "admin_market_twin", "label": _("← Слепок рынка")}],
        )
    top_hs = list(qs.values("hs_code").annotate(c=Count("id"), v=Sum("customs_value_usd")).order_by("-v")[:8])
    top_co = list(qs.values("origin_country").annotate(c=Count("id"), v=Sum("customs_value_usd")).order_by("-v")[:8])
    recent = list(qs[:15])

    kpi = {"type": "kpi_grid", "data": {"title": _("🛂 Таможенная аналитика"), "kpis": [
        {"value": _n(total), "label": _("Записей")},
        {"value": _m(value), "label": _("Стоимость ввоза")},
        {"value": _n(weight) + " кг", "label": _("Вес")},
        {"value": _n(linked), "label": _("С парт-номером"), "sub": _("связь с графом")},
    ]}}
    hs_rows = [{"title": f"HS {h['hs_code']}",
                "subtitle": _("%(c)s записей · %(v)s") % {"c": h['c'], "v": _m(h['v'])}} for h in top_hs]
    co_rows = [{"title": (c["origin_country"] or "—"),
                "subtitle": _("%(c)s записей · %(v)s") % {"c": c['c'], "v": _m(c['v'])}} for c in top_co]
    rec_rows = [{
        "title": f"HS {r.hs_code} · {r.origin_country or '—'}→{r.dest_country}",
        "subtitle": (f"{r.commodity or ''} · {_m(r.customs_value_usd)}"
                     + (f" · OEM {r.oem_number}" if r.oem_number else "")),
    } for r in recent]
    return ActionResult(
        text=_("🛂 Таможенная аналитика: %(total)s записей, %(value)s ввоза, %(linked)s с парт-номером.")
             % {"total": _n(total), "value": _m(value), "linked": _n(linked)},
        cards=[
            kpi,
            {"type": "list", "data": {"title": _("📊 Топ HS-кодов по стоимости"), "rows": hs_rows}},
            {"type": "list", "data": {"title": _("🗺 Топ стран происхождения"), "rows": co_rows}},
            {"type": "list", "data": {"title": _("🕒 Последние записи"), "rows": rec_rows}},
        ],
        contextual_actions=[add_btn, {"action": "admin_market_twin", "label": _("← Слепок рынка")}],
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
            text=_("Добавьте запись таможенной аналитики. Привяжите парт-номер (OEM) — "
                   "тогда реальная цена ввоза попадёт в граф рынка по этому артикулу."),
            cards=[{"type": "form", "data": {
                "title": _("🛂 Новая запись таможни"),
                "submit_action": "admin_customs_add",
                "submit_label": _("Добавить"),
                "fields": [
                    {"name": "hs_code", "label": _("ТН ВЭД / HS"), "type": "text", "required": True, "placeholder": "8431499900"},
                    {"name": "commodity", "label": _("Товар"), "type": "text", "placeholder": _("Башмак гусеницы CAT")},
                    {"name": "oem_number", "label": _("Парт-номер (OEM)"), "type": "text", "placeholder": _("1R-0750 (опц., связь с графом)")},
                    {"name": "origin_country", "label": _("Страна происхождения (2 буквы)"), "type": "text", "placeholder": "CN"},
                    {"name": "supplier", "label": _("Поставщик/отправитель"), "type": "text", "placeholder": _("опц.")},
                    {"name": "importer", "label": _("Импортёр"), "type": "text", "placeholder": _("опц.")},
                    {"name": "qty", "label": _("Кол-во"), "type": "text", "placeholder": "0"},
                    {"name": "net_weight_kg", "label": _("Вес, кг"), "type": "text", "placeholder": "0"},
                    {"name": "customs_value_usd", "label": _("Стоимость ввоза, USD"), "type": "text", "placeholder": "0"},
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
    msg = _("✅ Запись добавлена: HS %(hs)s") % {"hs": rec.hs_code}
    if rec.oem_number:
        msg += _(" · привязана к OEM %(oem)s (обогатила граф)") % {"oem": rec.oem_number}
    return ActionResult(
        text=msg + ".",
        contextual_actions=[
            {"action": "admin_customs_add", "label": _("➕ Ещё запись"), "params": {}},
            {"action": "admin_customs", "label": _("🛂 К таможне"), "params": {}},
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
        confirmed = _("✅ подтв.") if c.user_id else _("⚪️ лид")
        contact = " · ".join([x for x in [c.contact_name, c.phone] if x]) or _("контактов нет")
        rows.append({"title": _("%(name)s · ИНН %(inn)s") % {"name": c.name, "inn": c.inn},
                     "subtitle": f"{confirmed} · KAM {kam} · {contact}",
                     "action": "customer_detail", "params": {"id": str(c.id)}})
    return ActionResult(
        text=_("👥 Заказчики платформы: %(total)s. Контакты ЛПР, привязка к KAM, статус закрепления.")
             % {"total": total},
        cards=[{"type": "list", "data": {"title": _("Заказчики (%(total)s)") % {"total": total},
                "rows": rows or [{"title": _("Пока нет заказчиков"), "subtitle": _("появятся с привлечением KAM")}]}}],
        contextual_actions=[{"action": "admin_market_twin", "label": _("← Слепок рынка")}],
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
        rows.append({"title": p.name,
                     "subtitle": _("парк/теги: %(tags)s · владелец %(owner)s")
                                 % {"tags": tags, "owner": owner},
                     "url": f"/chat/project/{p.id}/"})
    return ActionResult(
        text=_("🚜 Парки техники / проекты: %(total)s. Что у клиентов в эксплуатации → предиктивный спрос.")
             % {"total": total},
        cards=[{"type": "list", "data": {"title": _("Парки / проекты (%(total)s)") % {"total": total},
                "rows": rows or [{"title": _("Пока нет"), "subtitle": _("появятся из проектов покупателей")}]}}],
        contextual_actions=[{"action": "admin_market_twin", "label": _("← Слепок рынка")}],
    )

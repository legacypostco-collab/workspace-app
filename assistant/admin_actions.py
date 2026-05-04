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
  admin_change_role     — buyer ↔ seller
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
    from marketplace.models import Order, RFQ, CompanyVerification, Notification

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

    return ActionResult(
        text=(
            f"🛡 Платформа · {users_total} активных юзеров (+{users_new_7d} за неделю) · "
            f"{orders_total} заказов всего · GMV за 7 дней ${gmv_7d:,.0f}."
        ),
        cards=[
            {"type": "kpi_grid", "data": {"title": "🛡 Admin · Сводка", "items": [
                {"label": "Активных юзеров", "value": str(users_total), "tone": "info"},
                {"label": "Новых за 7 дней", "value": f"+{users_new_7d}",
                 "tone": "ok" if users_new_7d else "warn"},
                {"label": "Заказов всего", "value": str(orders_total)},
                {"label": "За 24 часа", "value": str(orders_24h)},
                {"label": "В работе", "value": str(open_orders), "tone": "info"},
                {"label": "GMV 7d", "value": f"${gmv_7d:,.0f}", "tone": "ok"},
                {"label": "SLA breach 7d", "value": str(sla_breached_7d),
                 "tone": "bad" if sla_breached_7d > 0 else "ok"},
                {"label": "RFQ за 24ч", "value": str(rfq_24h)},
                {"label": "KYB pending", "value": str(kyb_pending),
                 "tone": "warn" if kyb_pending else "ok"},
                {"label": "KYB verified", "value": str(kyb_verified)},
            ]}},
        ],
        contextual_actions=[
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
    from marketplace.models import Order
    from django.db.models import Sum, Count

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
                       "tone": "ok" if gmv > 0 else "warn"})

    # Top categories
    top_cat = list(
        Order.objects.filter(payment_status__in=("paid", "refunded"))
        .values("items__part__category__name")
        .annotate(gmv=Sum("total_amount"))
        .order_by("-gmv")[:5]
    )
    cat_rows = [
        {"title": c["items__part__category__name"] or "—",
         "subtitle": f"${(c['gmv'] or 0):,.0f}"}
        for c in top_cat if c["gmv"]
    ]

    return ActionResult(
        text=(
            f"📈 Платформенный GMV (только paid/refunded, без отменённых)."
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
    qs = U.objects.all()
    if flt == "active":
        qs = qs.filter(is_active=True)
    elif flt == "banned":
        qs = qs.filter(is_active=False)
    elif flt == "buyers":
        qs = qs.filter(profile__role="buyer")
    elif flt == "sellers":
        qs = qs.filter(profile__role="seller")
    elif flt == "kyb_pending":
        qs = qs.filter(kyb__status="pending")
    qs = qs.exclude(username="__platform_escrow__").order_by("-date_joined")[:30]

    rows = []
    for u in qs:
        prof = getattr(u, "profile", None)
        kyb_obj = getattr(u, "kyb", None)
        kyb_label = kyb_obj.get_status_display() if kyb_obj else "—"
        role_label = (prof.role if prof else "—") or "—"
        flags = []
        if u.is_superuser: flags.append("⚡admin")
        if not u.is_active: flags.append("🚫 ban")
        if kyb_obj and kyb_obj.status == "verified": flags.append("✓ KYB")
        rows.append({
            "title": f"{u.username} · {role_label}",
            "subtitle": (
                f"{u.email or '—'} · KYB: {kyb_label}"
                + (" · " + " ".join(flags) if flags else "")
            ),
        })

    return ActionResult(
        text=f"👥 Пользователи · фильтр «{flt}» · {len(rows)} найдено.",
        cards=[{"type": "list", "data": {"title": f"👥 Пользователи · {flt}",
                                          "items": rows or [{"title": "Пусто"}]}}],
        contextual_actions=[
            {"action": "admin_users", "label": "Все",       "params": {"filter": "all"}},
            {"action": "admin_users", "label": "Активные",  "params": {"filter": "active"}},
            {"action": "admin_users", "label": "Заблокир.", "params": {"filter": "banned"}},
            {"action": "admin_users", "label": "Покупатели","params": {"filter": "buyers"}},
            {"action": "admin_users", "label": "Продавцы",  "params": {"filter": "sellers"}},
            {"action": "admin_users", "label": "KYB pending","params": {"filter": "kyb_pending"}},
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

    orders_n = Order.objects.filter(buyer=u).count()
    orders_paid = Order.objects.filter(buyer=u, payment_status="paid").count()
    sales_orders = Order.objects.filter(items__part__seller=u).distinct().count()

    rows = [
        {"label": "Username", "value": u.username, "primary": True},
        {"label": "Email", "value": u.email or "—"},
        {"label": "Роль", "value": (prof.role if prof else "—") or "—"},
        {"label": "Статус", "value": "🚫 Заблокирован" if not u.is_active else "✓ Активен",
         "primary": not u.is_active},
        {"label": "Зарегистрирован", "value": u.date_joined.strftime("%Y-%m-%d")},
        {"label": "Последний вход", "value":
            u.last_login.strftime("%Y-%m-%d %H:%M") if u.last_login else "—"},
        {"label": "KYB", "value": (kyb.get_status_display() if kyb else "не подавалась")},
        {"label": "Wallet", "value": f"${wallet.balance:,.2f} {wallet.currency}" if wallet else "—"},
        {"label": "Заказов как buyer", "value": f"{orders_paid} оплачено / {orders_n} всего"},
        {"label": "Заказов как seller", "value": str(sales_orders)},
        {"label": "Привилегии", "value":
            ("⚡ admin" if u.is_superuser else ("staff" if u.is_staff else "user"))},
    ]

    actions = []
    if u.is_active and not u.is_superuser:
        actions.append({"action": "admin_ban_user", "label": "🚫 Заблокировать",
                        "params": {"user_id": u.id}})
    elif not u.is_active:
        actions.append({"action": "admin_unban_user", "label": "✓ Разблокировать",
                        "params": {"user_id": u.id}})
    if not u.is_superuser:
        actions.append({"action": "admin_change_role", "label": "🔄 Сменить роль",
                        "params": {"user_id": u.id}})

    return ActionResult(
        text=f"👤 {u.username} · {u.email or 'нет email'}",
        cards=[{"type": "draft", "data": {"title": f"Профиль · {u.username}",
                                           "rows": rows, "confirm_label": "—"}}],
        actions=actions,
        contextual_actions=[
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
# 6. Change role — buyer ↔ seller
# ══════════════════════════════════════════════════════════

@register("admin_change_role")
def admin_change_role(params, user, role):
    err = _ensure_admin(role)
    if err: return err
    from django.contrib.auth import get_user_model
    from marketplace.models import UserProfile
    U = get_user_model()
    try:
        target = U.objects.get(id=int(params.get("user_id") or 0))
    except (U.DoesNotExist, ValueError, TypeError):
        return ActionResult(text="Пользователь не найден.")
    if target.is_superuser:
        return ActionResult(text="⚠️ Менять роль админу нельзя.")

    new_role = (params.get("new_role") or "").strip().lower()
    confirmed = bool(params.get("confirmed"))

    if new_role not in ("buyer", "seller") or not confirmed:
        prof = getattr(target, "profile", None)
        cur = (prof.role if prof else "buyer") or "buyer"
        return ActionResult(
            text=f"Сменить роль для {target.username} (сейчас: {cur})?",
            cards=[{"type": "form", "data": {
                "title": f"🔄 Роль · {target.username}",
                "submit_action": "admin_change_role",
                "fields": [{
                    "name": "new_role", "label": "Новая роль",
                    "type": "select", "required": True,
                    "options": [
                        {"value": "buyer",  "label": "buyer (покупатель)"},
                        {"value": "seller", "label": "seller (продавец)"},
                    ],
                    "value": cur,
                }],
                "fixed_params": {"user_id": target.id, "confirmed": True},
            }}],
        )

    profile, _ = UserProfile.objects.get_or_create(user=target)
    old_role = profile.role
    profile.role = new_role
    profile.save(update_fields=["role"])
    _notify(target, kind="system",
            title=f"Роль изменена: {old_role} → {new_role}",
            body="Администратор платформы изменил вашу роль.",
            url="/chat/")
    return ActionResult(
        text=f"✓ {target.username}: {old_role} → {new_role}.",
        contextual_actions=[
            {"action": "admin_user_detail", "label": "← Профиль",
             "params": {"user_id": target.id}},
        ],
    )


# ══════════════════════════════════════════════════════════
# 7. Moderation queue — единая
# ══════════════════════════════════════════════════════════

@register("admin_moderation_queue")
def admin_moderation_queue(params, user, role):
    err = _ensure_admin(role)
    if err: return err
    from marketplace.models import Order, CompanyVerification, Quote

    kyb_pending = CompanyVerification.objects.filter(status="pending").count()
    refunds = Order.objects.filter(payment_status="refund_pending").count()
    sla_breached = Order.objects.filter(sla_status="breached").count()
    quotes_countered = Quote.objects.filter(status="countered").count()

    items = [
        {"title": f"KYB на проверке · {kyb_pending}",
         "subtitle": "→ op_kyb_queue"} if kyb_pending else None,
        {"title": f"Возвраты в обработке · {refunds}",
         "subtitle": "→ op_queue?filter=refund"} if refunds else None,
        {"title": f"SLA нарушены · {sla_breached}",
         "subtitle": "→ op_sla_breach"} if sla_breached else None,
        {"title": f"Контр-офферы ждут ответа · {quotes_countered}",
         "subtitle": "семинары переторжки"} if quotes_countered else None,
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

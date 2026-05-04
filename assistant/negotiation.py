"""RFQ → Quote multi-round negotiation actions.

Полный flow:

  buyer.create_rfq → seller.respond_rfq_form → submit_quote
    → buyer.view_rfq_quotes (compares all sellers)
      → buyer.accept_quote → Order
      OR buyer.counter_offer → seller.respond_to_counter
         → buyer.accept_quote / decline_quote
      OR seller.mark_quote_final → buyer.accept_quote / decline_quote

Каждый раунд — отдельный Quote с parent_quote ссылкой и round_number+1.
Цены хранятся в QuoteItem (per-line). Итоги — в Quote.total_amount.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from .actions import ActionResult, _log_event, _notify, register

logger = logging.getLogger(__name__)


# ── Helpers ────────────────────────────────────────────────────

def _calc_quote_total(items: list[dict]) -> Decimal:
    """Sum of unit_price * quantity для списка items."""
    total = Decimal("0")
    for it in items:
        try:
            total += Decimal(str(it["unit_price"])) * int(it["quantity"])
        except (KeyError, ValueError):
            pass
    return total.quantize(Decimal("0.01"))


def _next_round(rfq, seller_id: int) -> int:
    """Какой round_number должен быть у новой котировки от этого продавца."""
    from marketplace.models import Quote
    last = Quote.objects.filter(rfq=rfq, seller_id=seller_id).order_by("-round_number").first()
    return (last.round_number + 1) if last else 1


# ══════════════════════════════════════════════════════════
# 0. send_rfq_to_suppliers — buyer рассылает RFQ продавцам
# ══════════════════════════════════════════════════════════

@register("send_rfq_to_suppliers")
def send_rfq_to_suppliers(params, user, role):
    """Buyer разослает RFQ кандидатам-поставщикам.

    Кандидаты: верифицированные KYB-продавцы (status=verified). В demo
    fallback — всех с role=seller. Каждому создаём Notification с
    kind='rfq' (durable channels потом добавляют email/telegram).
    """
    from django.contrib.auth import get_user_model
    from marketplace.models import RFQ, CompanyVerification

    confirmed = bool(params.get("confirmed"))
    try:
        rfq = RFQ.objects.get(id=int(params.get("rfq_id") or 0))
    except (RFQ.DoesNotExist, ValueError, TypeError):
        return ActionResult(text="RFQ не найден.")

    if rfq.created_by_id != user.id and role != "admin":
        return ActionResult(text="Разослать RFQ может только его автор.")
    if rfq.status == "cancelled":
        return ActionResult(text=f"RFQ #{rfq.id} отменён — нельзя рассылать.")

    User = get_user_model()
    # Верифицированные → приоритет; демо-fallback на всех seller-профилей
    verified_seller_ids = set(
        CompanyVerification.objects.filter(status="verified")
        .values_list("user_id", flat=True)
    )
    candidates = list(
        User.objects.filter(profile__role="seller", is_active=True)
        .exclude(username="__platform_escrow__")[:20]
    )
    if not candidates:
        return ActionResult(text="⚠️ В системе пока нет активных поставщиков.")

    n_verified = sum(1 for s in candidates if s.id in verified_seller_ids)

    # Шаг 1: preview
    if not confirmed:
        items_count = rfq.items.count()
        return ActionResult(
            text=f"📨 Разослать RFQ #{rfq.id} поставщикам?",
            cards=[{"type": "draft", "data": {
                "title": f"📨 Рассылка RFQ #{rfq.id}",
                "rows": [
                    {"label": "Позиций", "value": str(items_count), "primary": True},
                    {"label": "Получателей", "value": f"{len(candidates)} поставщиков"},
                    {"label": "Из них верифицированных",
                     "value": f"{n_verified} ({n_verified*100//max(len(candidates),1)}%)",
                     "primary": True},
                ],
                "warnings": [
                    "Каждый получит уведомление (in-app + email/telegram если включены).",
                    "Котировки будут приходить — следите за вкладкой RFQ.",
                ],
                "confirm_action": "send_rfq_to_suppliers",
                "confirm_label": f"📨 Разослать {len(candidates)} поставщикам",
                "confirm_params": {"rfq_id": rfq.id, "confirmed": True},
                "cancel_label": "Отмена",
            }}],
        )

    # Шаг 2: рассылка
    sent = 0
    for seller in candidates:
        try:
            _notify(
                seller, kind="rfq",
                title=f"Новый RFQ #{rfq.id} от {rfq.customer_name or rfq.created_by.username}",
                body=f"{rfq.items.count()} позиций · {rfq.urgency or 'standard'}. Откройте чтобы ответить котировкой.",
                url=f"/chat/rfq/{rfq.id}/?source=invite",
            )
            sent += 1
        except Exception:
            logger.exception("send_rfq notify failed for seller %s", seller.id)

    return ActionResult(
        text=(
            f"✓ RFQ #{rfq.id} разослан {sent} поставщикам ({n_verified} верифицированных).\n"
            f"Котировки будут приходить — следите за уведомлениями."
        ),
        contextual_actions=[
            {"action": "view_rfq_quotes", "label": "📊 Открытые котировки",
             "params": {"rfq_id": rfq.id}},
            {"action": "open_url", "label": "← Назад к RFQ",
             "params": {"_url": f"/chat/rfq/{rfq.id}/"}},
        ],
    )


# ══════════════════════════════════════════════════════════
# 1. submit_quote — продавец создаёт котировку
# ══════════════════════════════════════════════════════════

@register("submit_quote")
def submit_quote(params, user, role):
    """Создать котировку (Quote) на RFQ.

    params:
      rfq_id              — обязательно
      delivery_days       — срок доставки (default 14)
      message             — комментарий
      items               — список [{rfq_item_id, unit_price, quantity?}]
      valid_days          — срок действия (default 7)
      parent_quote_id     — если это ответ на counter; round_number+1
      direction           — 'seller_to_buyer' (default) или 'buyer_to_seller' для counter
      confirmed           — bool
    """
    from marketplace.models import RFQ, RFQItem, Quote, QuoteItem
    from .onboarding import kyb_required_for_seller

    try:
        rfq = RFQ.objects.get(id=int(params.get("rfq_id") or 0))
    except (RFQ.DoesNotExist, ValueError, TypeError):
        return ActionResult(text="RFQ не найден.")

    # KYB-gate (selling-action)
    if role == "seller" and kyb_required_for_seller(user):
        return ActionResult(
            text="🛡 Котировки доступны только верифицированным продавцам.",
            actions=[{"action": "start_onboarding", "label": "🚀 Начать верификацию"}],
        )

    delivery_days = int(params.get("delivery_days") or 14)
    valid_days = int(params.get("valid_days") or 7)
    message = (params.get("message") or "").strip()
    parent_quote_id = params.get("parent_quote_id")
    direction = (params.get("direction") or "seller_to_buyer").strip()
    confirmed = bool(params.get("confirmed"))

    rfq_items = list(RFQItem.objects.filter(rfq=rfq))

    # Шаг 1: форма (preview prices for each line)
    if not confirmed:
        # Подгружаем предлагаемые цены: либо из params.items, либо рассчитываем по части
        items_input = params.get("items") or []
        # Map by rfq_item_id для ассоциации
        suggested = {int(it.get("rfq_item_id") or 0): it for it in items_input if it.get("rfq_item_id")}

        fields = []
        for it in rfq_items:
            base = suggested.get(it.id, {})
            default_price = base.get("unit_price")
            if default_price is None and it.matched_part:
                default_price = float(it.matched_part.price or 0)
            fields.append({
                "name": f"price_{it.id}",
                "label": f"{it.query[:60]} × {it.quantity}",
                "type": "number",
                "required": True,
                "value": str(default_price) if default_price else "",
            })

        fields.extend([
            {"name": "delivery_days", "label": "Срок поставки (дн)",
             "type": "number", "value": str(delivery_days)},
            {"name": "valid_days", "label": "Котировка действует (дн)",
             "type": "number", "value": str(valid_days)},
            {"name": "message", "label": "Комментарий", "type": "textarea"},
        ])

        title_extra = ""
        round_number = _next_round(rfq, user.id)
        if parent_quote_id:
            title_extra = f" · ответ на counter (раунд {round_number})"

        return ActionResult(
            text=f"💬 Котировка по RFQ #{rfq.id}{title_extra}",
            cards=[{"type": "form", "data": {
                "title": f"💬 Котировка · RFQ #{rfq.id}",
                "submit_action": "submit_quote",
                "submit_label": "Отправить котировку",
                "fields": fields,
                "fixed_params": {
                    "rfq_id": rfq.id,
                    "confirmed": True,
                    "parent_quote_id": parent_quote_id,
                    "direction": direction,
                },
            }}],
        )

    # Шаг 2: парсим цены из form-полей price_<rfq_item_id>
    item_data = []
    for it in rfq_items:
        raw = params.get(f"price_{it.id}")
        if raw in (None, ""):
            continue
        try:
            price = Decimal(str(raw))
        except Exception:
            continue
        item_data.append({
            "rfq_item": it,
            "unit_price": price,
            "quantity": it.quantity,
        })

    if not item_data:
        return ActionResult(text="⚠️ Не указано ни одной цены — котировка не создана.")

    # parent linking
    parent = None
    if parent_quote_id:
        parent = Quote.objects.filter(id=int(parent_quote_id)).first()

    round_number = _next_round(rfq, user.id)
    total = _calc_quote_total([{"unit_price": d["unit_price"], "quantity": d["quantity"]} for d in item_data])
    valid_until = timezone.now() + timedelta(days=valid_days)

    quote = Quote.objects.create(
        rfq=rfq,
        seller=user if direction == "seller_to_buyer" else (parent.seller if parent else user),
        direction=direction,
        parent_quote=parent,
        round_number=round_number,
        status="submitted",
        delivery_days=delivery_days,
        valid_until=valid_until,
        total_amount=total,
        message=message,
    )
    for d in item_data:
        QuoteItem.objects.create(
            quote=quote,
            rfq_item=d["rfq_item"],
            part=d["rfq_item"].matched_part,
            title_snapshot=d["rfq_item"].query[:300],
            quantity=d["quantity"],
            unit_price=d["unit_price"],
        )

    # Состояние RFQ
    if rfq.status == "new":
        rfq.status = "quoted"
        rfq.save(update_fields=["status"])

    # Уведомляем покупателя
    if rfq.created_by:
        _notify(
            rfq.created_by, kind="rfq",
            title=f"Котировка по RFQ #{rfq.id}",
            body=f"{user.username}: ${total:,.0f} · доставка {delivery_days} дн.",
            url=f"/chat/?rfq={rfq.id}",
        )

    # Если это counter-respond — пометить parent как countered → submitted (он ответил)
    if parent and parent.direction == "buyer_to_seller":
        parent.status = "submitted"
        parent.save(update_fields=["status"])

    return ActionResult(
        text=(
            f"✓ Котировка #{quote.id} отправлена · ${total:,.2f} · доставка {delivery_days} дн."
            + (f" (раунд {round_number})" if round_number > 1 else "")
        ),
        contextual_actions=[
            {"action": "view_rfq_quotes", "label": "📊 Все котировки", "params": {"rfq_id": rfq.id}},
        ],
    )


# Обратная совместимость — старый stub respond_rfq_form
@register("respond_rfq_form")
def respond_rfq_form(params, user, role):
    """Алиас на submit_quote — единый wizard для всех ответов на RFQ."""
    return submit_quote(params, user, role)


# ══════════════════════════════════════════════════════════
# 2. view_rfq_quotes — buyer видит все котировки по RFQ
# ══════════════════════════════════════════════════════════

@register("view_rfq_quotes")
def view_rfq_quotes(params, user, role):
    from marketplace.models import RFQ, Quote
    try:
        rfq = RFQ.objects.get(id=int(params.get("rfq_id") or 0))
    except (RFQ.DoesNotExist, ValueError, TypeError):
        return ActionResult(text="RFQ не найден.")

    # Только владелец RFQ или оператор/admin
    if rfq.created_by_id != user.id and not (role and role.startswith("operator")) and role != "admin":
        return ActionResult(text="Просматривать котировки может только заказчик RFQ или оператор.")

    # Берём ПОСЛЕДНЮЮ котировку каждого продавца (макс. round_number)
    seller_ids = (
        Quote.objects.filter(rfq=rfq, direction="seller_to_buyer")
        .values_list("seller_id", flat=True).distinct()
    )
    latest = []
    for sid in seller_ids:
        q = (Quote.objects.filter(rfq=rfq, seller_id=sid, direction="seller_to_buyer")
             .order_by("-round_number").first())
        if q:
            latest.append(q)
    latest.sort(key=lambda q: q.total_amount)

    if not latest:
        return ActionResult(
            text=f"По RFQ #{rfq.id} пока нет котировок.",
            contextual_actions=[
                {"action": "get_rfq_status", "label": "📋 Все RFQ"},
            ],
        )

    # Anonymize seller имена для buyer'а (раскрываются после accept_quote)
    is_buyer_view = (rfq.created_by_id == user.id) and role == "buyer"

    items = []
    for idx, q in enumerate(latest):
        if is_buyer_view:
            seller_name = f"Поставщик №{idx + 1}"
        else:
            seller_name = q.seller.username if q.seller else "—"
        flags = []
        if q.is_final: flags.append("🔒 финальная")
        if q.round_number > 1: flags.append(f"раунд {q.round_number}")
        flag_str = " · " + " · ".join(flags) if flags else ""
        items.append({
            "title": f"{seller_name} — ${q.total_amount:,.2f}",
            "subtitle": f"Доставка {q.delivery_days} дн{flag_str}",
            "id": q.id,
        })

    # Suggest accept на самую дешёвую (если она финальная — не предлагаем counter)
    cheapest = latest[0]
    cheapest_label = "Поставщик №1" if is_buyer_view else (
        cheapest.seller.username if cheapest.seller else "?"
    )
    actions = []
    if cheapest.is_final:
        actions.append({"action": "accept_quote", "label": f"✓ Принять самую дешёвую (${cheapest.total_amount:,.0f})",
                        "params": {"quote_id": cheapest.id}})
    else:
        actions.extend([
            {"action": "accept_quote", "label": f"✓ Принять — {cheapest_label}",
             "params": {"quote_id": cheapest.id}},
            {"action": "counter_offer", "label": "↩ Контр-оффер",
             "params": {"quote_id": cheapest.id}},
        ])

    return ActionResult(
        text=(
            f"📊 По RFQ #{rfq.id} — {len(latest)} котировок · "
            f"диапазон ${latest[0].total_amount:,.0f}–${latest[-1].total_amount:,.0f}."
        ),
        cards=[{"type": "list", "data": {"title": f"💬 Котировки · RFQ #{rfq.id}",
                                          "items": items}}],
        actions=actions,
        contextual_actions=[
            {"action": "view_quote", "label": "Открыть лучшую",
             "params": {"quote_id": cheapest.id}},
        ],
    )


# ══════════════════════════════════════════════════════════
# 3. view_quote — детальная карточка котировки
# ══════════════════════════════════════════════════════════

@register("view_quote")
def view_quote(params, user, role):
    from marketplace.models import Quote
    try:
        q = Quote.objects.select_related("rfq", "seller").get(id=int(params.get("quote_id") or 0))
    except (Quote.DoesNotExist, ValueError, TypeError):
        return ActionResult(text="Котировка не найдена.")

    is_buyer = (q.rfq.created_by_id == user.id)
    is_seller = (q.seller_id == user.id)
    if not (is_buyer or is_seller or (role and role.startswith("operator")) or role == "admin"):
        return ActionResult(text="Доступ к котировке ограничен.")

    # Buyer видит «Поставщик №N» (по порядку round_number в данном RFQ);
    # accepted-котировки раскрывают имя — buyer уже выбрал и теперь нужны контакты.
    if is_buyer and role == "buyer" and q.status != "accepted":
        # Определяем порядковый номер среди всех котировок этого RFQ от seller_to_buyer
        from marketplace.models import Quote as _Q
        ranked = list(_Q.objects.filter(rfq_id=q.rfq_id, direction="seller_to_buyer")
                      .order_by("total_amount").values_list("seller_id", flat=True))
        try:
            rank = ranked.index(q.seller_id) + 1 if q.seller_id else None
            seller_label = f"Поставщик №{rank}" if rank else "Поставщик"
        except ValueError:
            seller_label = "Поставщик"
    else:
        seller_label = q.seller.username if q.seller else "—"

    rows = [
        {"label": "RFQ", "value": f"#{q.rfq_id}"},
        {"label": "Продавец", "value": seller_label},
        {"label": "Раунд", "value": str(q.round_number)},
        {"label": "Сумма", "value": f"${q.total_amount:,.2f} {q.currency}", "primary": True},
        {"label": "Доставка (дн)", "value": str(q.delivery_days)},
        {"label": "Статус", "value": q.get_status_display()},
        {"label": "Действует до", "value": q.valid_until.strftime("%d.%m.%Y %H:%M") if q.valid_until else "—"},
    ]
    if q.message:
        rows.append({"label": "Комментарий", "value": q.message[:200]})

    line_items = [
        {"title": (qi.title_snapshot or (qi.part.title if qi.part else "—"))[:60],
         "subtitle": f"{qi.quantity} × ${qi.unit_price:,.2f} = ${qi.line_total:,.2f}"}
        for qi in q.items.all()
    ]

    actions = []
    if is_buyer and q.status in ("submitted",) and q.direction == "seller_to_buyer":
        if not q.is_final:
            actions.extend([
                {"action": "accept_quote",  "label": "✓ Принять",         "params": {"quote_id": q.id}},
                {"action": "counter_offer", "label": "↩ Контр-оффер",     "params": {"quote_id": q.id}},
                {"action": "decline_quote", "label": "✗ Отклонить",       "params": {"quote_id": q.id}},
            ])
        else:
            actions.extend([
                {"action": "accept_quote",  "label": "✓ Принять",         "params": {"quote_id": q.id}},
                {"action": "decline_quote", "label": "✗ Отклонить",       "params": {"quote_id": q.id}},
            ])
    if is_seller and q.status == "submitted" and q.direction == "seller_to_buyer" and not q.is_final:
        actions.append({"action": "mark_quote_final", "label": "🔒 Зафиксировать как финальную",
                        "params": {"quote_id": q.id}})
    if is_seller and q.direction == "buyer_to_seller" and q.status == "submitted":
        # Это buyer-counter — продавец должен ответить новой котировкой
        actions.append({"action": "respond_to_counter", "label": "💬 Ответить на counter",
                        "params": {"quote_id": q.id}})

    return ActionResult(
        text=f"💬 Котировка #{q.id} · ${q.total_amount:,.2f} · {q.delivery_days} дн.",
        cards=[
            {"type": "draft", "data": {"title": f"Котировка #{q.id}", "rows": rows,
                                       "confirm_label": "—"}},
            {"type": "list", "data": {"title": "Позиции", "items": line_items or [{"title": "—"}]}},
        ],
        actions=actions,
    )


# ══════════════════════════════════════════════════════════
# 4. accept_quote — buyer принимает → Order
# ══════════════════════════════════════════════════════════

@register("accept_quote")
def accept_quote(params, user, role):
    from marketplace.models import Quote, RFQ, Order, OrderItem
    confirmed = bool(params.get("confirmed"))
    try:
        q = Quote.objects.select_related("rfq", "seller").get(id=int(params.get("quote_id") or 0))
    except (Quote.DoesNotExist, ValueError, TypeError):
        return ActionResult(text="Котировка не найдена.")

    if q.rfq.created_by_id != user.id:
        return ActionResult(text="Принять котировку может только заказчик RFQ.")
    if q.status not in ("submitted", "finalized"):
        return ActionResult(text=f"Эту котировку нельзя принять (статус: {q.get_status_display()}).")

    # Шаг 1: preview (buyer ещё не должен видеть имя — раскрываем после confirm)
    if not confirmed:
        if role == "buyer":
            from marketplace.models import Quote as _Q
            ranked = list(_Q.objects.filter(rfq_id=q.rfq_id, direction="seller_to_buyer")
                          .order_by("total_amount").values_list("seller_id", flat=True))
            try:
                rank = ranked.index(q.seller_id) + 1 if q.seller_id else None
            except ValueError:
                rank = None
            seller_label = f"Поставщик №{rank}" if rank else "Поставщик"
            extra_warn = "После принятия имя поставщика и контакты будут раскрыты для оформления заказа."
        else:
            seller_label = q.seller.username if q.seller else "—"
            extra_warn = ""
        warnings = [
            "После принятия будет создан заказ. Остальные котировки автоматически отклонятся.",
        ]
        if extra_warn:
            warnings.append(extra_warn)
        return ActionResult(
            text=f"Принять котировку #{q.id} от {seller_label}?",
            cards=[{"type": "draft", "data": {
                "title": f"✓ Принять котировку #{q.id}",
                "rows": [
                    {"label": "Продавец", "value": seller_label},
                    {"label": "Сумма", "value": f"${q.total_amount:,.2f}", "primary": True},
                    {"label": "Доставка", "value": f"{q.delivery_days} дней"},
                    {"label": "Резерв 10%", "value": f"${(q.total_amount * Decimal('0.10')):,.2f}"},
                ],
                "warnings": warnings,
                "confirm_action": "accept_quote",
                "confirm_label": "✓ Принять и создать заказ",
                "confirm_params": {"quote_id": q.id, "confirmed": True},
                "cancel_label": "Отмена",
            }}],
        )

    # Шаг 2: создаём Order
    reserve_pct = Decimal("10.00")
    reserve_amount = (q.total_amount * reserve_pct / Decimal("100")).quantize(Decimal("0.01"))
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
        total_amount=q.total_amount,
    )
    items_count = 0
    for qi in q.items.all():
        if not qi.part:
            continue
        OrderItem.objects.create(
            order=order, part=qi.part,
            quantity=qi.quantity, unit_price=qi.unit_price,
        )
        items_count += 1

    q.status = "accepted"
    q.save(update_fields=["status"])
    # Все остальные котировки этого RFQ от других продавцов → declined (auto)
    Quote.objects.filter(rfq=q.rfq).exclude(id=q.id).filter(
        status__in=("submitted", "finalized", "countered"),
    ).update(status="declined")
    q.rfq.status = "quoted"
    q.rfq.save(update_fields=["status"])

    _log_event(order, "order_created", actor=user, source="buyer",
               meta={"items": items_count, "total": float(q.total_amount),
                     "from_quote": q.id, "rfq_id": q.rfq_id})
    if q.seller:
        _notify(q.seller, kind="order",
                title=f"Котировка #{q.id} принята — заказ #{order.id}",
                body=f"Покупатель оформил заказ на ${q.total_amount:,.2f}. Можно начинать.",
                url=f"/chat/?order={order.id}")

    return ActionResult(
        text=(
            f"✓ Котировка #{q.id} принята · создан заказ #{order.id} на ${q.total_amount:,.2f}.\n"
            f"Следующий шаг — оплатить резерв 10% (${reserve_amount:,.0f})."
        ),
        actions=[
            {"action": "pay_reserve", "label": f"💳 Оплатить резерв ${reserve_amount:,.0f}",
             "params": {"order_id": order.id}},
            {"action": "track_order", "label": "📦 Трекинг", "params": {"order_id": order.id}},
        ],
    )


# ══════════════════════════════════════════════════════════
# 5. counter_offer — buyer предлагает свою цену
# ══════════════════════════════════════════════════════════

@register("counter_offer")
def counter_offer(params, user, role):
    from marketplace.models import Quote, QuoteItem
    confirmed = bool(params.get("confirmed"))
    try:
        q = Quote.objects.select_related("rfq", "seller").get(id=int(params.get("quote_id") or 0))
    except (Quote.DoesNotExist, ValueError, TypeError):
        return ActionResult(text="Котировка не найдена.")

    if q.rfq.created_by_id != user.id:
        return ActionResult(text="Контр-оффер может предлагать только заказчик RFQ.")
    if q.is_final:
        return ActionResult(text=(
            f"Котировка #{q.id} помечена как финальная — переторжка невозможна. "
            f"Только принять или отклонить."
        ))
    if q.status not in ("submitted",):
        return ActionResult(text=f"Контр-оффер невозможен (статус: {q.get_status_display()}).")

    # Шаг 1: форма с текущими ценами + полем для каждой
    if not confirmed:
        fields = []
        for qi in q.items.all():
            fields.append({
                "name": f"price_{qi.id}",
                "label": f"{qi.title_snapshot[:60]} (текущая ${qi.unit_price:,.2f})",
                "type": "number", "required": True,
                "value": str(qi.unit_price),
            })
        fields.append({"name": "message", "label": "Сообщение продавцу", "type": "textarea"})
        return ActionResult(
            text=f"↩ Контр-оффер на котировку #{q.id}",
            cards=[{"type": "form", "data": {
                "title": f"↩ Контр-оффер · #{q.id}",
                "submit_action": "counter_offer",
                "submit_label": "Отправить контр-оффер",
                "fields": fields,
                "fixed_params": {"quote_id": q.id, "confirmed": True},
            }}],
        )

    # Шаг 2: создаём новый Quote (direction=buyer_to_seller, status=submitted)
    new_round = q.round_number + 1
    items_data = []
    for qi in q.items.all():
        raw = params.get(f"price_{qi.id}")
        if raw in (None, ""):
            continue
        try:
            new_price = Decimal(str(raw))
        except Exception:
            continue
        items_data.append({"qi": qi, "unit_price": new_price})

    if not items_data:
        return ActionResult(text="⚠️ Не указано ни одной новой цены.")

    new_total = sum(
        (d["unit_price"] * d["qi"].quantity for d in items_data), Decimal("0")
    ).quantize(Decimal("0.01"))

    counter_q = Quote.objects.create(
        rfq=q.rfq,
        seller=q.seller,  # сохраняем привязку — это контр-оффер ИХ котировке
        direction="buyer_to_seller",
        parent_quote=q,
        round_number=new_round,
        status="submitted",
        delivery_days=q.delivery_days,
        valid_until=timezone.now() + timedelta(days=3),
        total_amount=new_total,
        message=(params.get("message") or "").strip(),
    )
    for d in items_data:
        QuoteItem.objects.create(
            quote=counter_q,
            rfq_item=d["qi"].rfq_item,
            part=d["qi"].part,
            title_snapshot=d["qi"].title_snapshot,
            quantity=d["qi"].quantity,
            unit_price=d["unit_price"],
        )

    # Помечаем оригинал как countered
    q.status = "countered"
    q.save(update_fields=["status"])

    if q.seller:
        _notify(q.seller, kind="rfq",
                title=f"Контр-оффер по RFQ #{q.rfq_id}",
                body=f"Покупатель предлагает ${new_total:,.0f} (было ${q.total_amount:,.0f}).",
                url=f"/chat/?rfq={q.rfq_id}")

    return ActionResult(
        text=(
            f"✓ Контр-оффер #{counter_q.id} отправлен продавцу · ${new_total:,.2f} "
            f"(раунд {new_round})."
        ),
        contextual_actions=[
            {"action": "view_rfq_quotes", "label": "📊 Все котировки", "params": {"rfq_id": q.rfq_id}},
        ],
    )


# ══════════════════════════════════════════════════════════
# 6. respond_to_counter — продавец отвечает на counter
# ══════════════════════════════════════════════════════════

@register("respond_to_counter")
def respond_to_counter(params, user, role):
    """Удобный shortcut — открывает submit_quote с parent_quote_id."""
    from marketplace.models import Quote
    try:
        q = Quote.objects.select_related("rfq").get(id=int(params.get("quote_id") or 0))
    except (Quote.DoesNotExist, ValueError, TypeError):
        return ActionResult(text="Котировка не найдена.")
    if q.direction != "buyer_to_seller":
        return ActionResult(text="Это не контр-оффер.")

    # Делегируем в submit_quote с parent_quote_id
    return submit_quote({
        "rfq_id": q.rfq_id,
        "parent_quote_id": q.id,
        "direction": "seller_to_buyer",
    }, user, role)


# ══════════════════════════════════════════════════════════
# 7. mark_quote_final — продавец фиксирует финальный оффер
# ══════════════════════════════════════════════════════════

@register("mark_quote_final")
def mark_quote_final(params, user, role):
    from marketplace.models import Quote
    try:
        q = Quote.objects.get(id=int(params.get("quote_id") or 0))
    except (Quote.DoesNotExist, ValueError, TypeError):
        return ActionResult(text="Котировка не найдена.")
    if q.seller_id != user.id:
        return ActionResult(text="Зафиксировать может только автор котировки.")
    if q.status != "submitted" or q.direction != "seller_to_buyer":
        return ActionResult(text="Эту котировку нельзя зафиксировать в текущем состоянии.")
    q.is_final = True
    q.status = "finalized"
    q.save(update_fields=["is_final", "status"])
    if q.rfq.created_by:
        _notify(q.rfq.created_by, kind="rfq",
                title=f"🔒 Финальная котировка по RFQ #{q.rfq_id}",
                body=f"{user.username}: ${q.total_amount:,.0f}. Переторжка невозможна — принять или отклонить.",
                url=f"/chat/?rfq={q.rfq_id}")
    return ActionResult(
        text=f"🔒 Котировка #{q.id} зафиксирована как финальная. Покупатель может только принять или отклонить.",
    )


# ══════════════════════════════════════════════════════════
# 8. decline_quote — buyer отклоняет
# ══════════════════════════════════════════════════════════

@register("decline_quote")
def decline_quote(params, user, role):
    from marketplace.models import Quote
    try:
        q = Quote.objects.get(id=int(params.get("quote_id") or 0))
    except (Quote.DoesNotExist, ValueError, TypeError):
        return ActionResult(text="Котировка не найдена.")
    if q.rfq.created_by_id != user.id:
        return ActionResult(text="Отклонить может только заказчик RFQ.")
    if q.status not in ("submitted", "finalized", "countered"):
        return ActionResult(text=f"Котировка уже не активна (статус: {q.get_status_display()}).")
    q.status = "declined"
    q.save(update_fields=["status"])
    if q.seller:
        _notify(q.seller, kind="rfq",
                title=f"Котировка #{q.id} отклонена",
                body=f"Покупатель не выбрал ваш вариант по RFQ #{q.rfq_id}.",
                url=f"/chat/?rfq={q.rfq_id}")
    return ActionResult(text=f"✓ Котировка #{q.id} отклонена.")

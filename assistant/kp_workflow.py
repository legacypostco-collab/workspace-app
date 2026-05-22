"""Три режима выставления КП и переход calc → shipment (ТЗ §4 расширенное).

  AUTO    — система мгновенно считает цену + логистику, выставляет КП с
            кнопкой «Подтвердить и зарезервировать 10%». После клика:
            резерв списан, чат переходит calc → shipment, статус
            «Заказ оформлен».

  SEMI    — расчёт готов, но КП уходит buyer'у только после подтверждения
            оператора. Оператору приходит уведомление, у него 15 минут на
            approve. После approve — то же что в AUTO.

  MANUAL  — оператор вручную рассылает запросы поставщикам. Срок сбора
            предложений — 48 часов. После получения ответов оператор
            формирует КП → дальше то же что в AUTO.

Во всех режимах: инвойс на 100%, 10% резервируется только после явного
подтверждения клиентом, после резерва — переход calc → shipment.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from .actions import ActionResult, _notify, register

logger = logging.getLogger(__name__)


# ── Параметры SLA ────────────────────────────────────────────────
SLA_OPERATOR_APPROVE_MINUTES = 15
SLA_MANUAL_QUOTE_COLLECTION_HOURS = 48


# ── Helpers ──────────────────────────────────────────────────────

def _conv_for_rfq(user, rfq):
    """Найти/создать conv 'calc' для этого user×rfq.

    На каждый RFQ — отдельный calc-conv (в отличие от admin/general
    группировки, тут чат привязан к сделке).
    """
    from .models import Conversation
    title = f"Расчёт RFQ #{rfq.id}"
    conv = Conversation.objects.filter(
        user=user, category="calc", title__startswith=f"Расчёт RFQ #{rfq.id}",
        is_active=True,
    ).order_by("-updated_at").first()
    if conv:
        return conv
    return Conversation.objects.create(
        user=user, role="buyer", category="calc", title=title[:200],
    )


def _switch_conv_to_shipment(conv, order):
    """calc → shipment + системное сообщение «КП подтверждено — сделка перешла в работу»."""
    from .models import Message
    if not conv:
        return
    conv.category = "shipment"
    conv.title = f"Сделка ORD-{order.id}"
    conv.save(update_fields=["category", "title", "updated_at"])
    Message.objects.create(
        conversation=conv,
        role=Message.Role.SYSTEM,
        content=(
            f"✅ КП подтверждено — сделка перешла в работу.\n"
            f"Заказ ORD-{order.id} · ${order.total_amount:,.2f} · "
            f"резерв 10% (${order.reserve_amount:,.2f}) удержан в эскроу."
        ),
        cards=[{
            "type": "order",
            "data": {
                "id": str(order.id),
                "number": order.id,
                "status": order.status,
                "total": float(order.total_amount),
                "currency": "USD",
                "reserve_amount": float(order.reserve_amount),
            },
        }],
    )


def _calc_logistics(items: list[tuple]) -> dict:
    """Грубая оценка логистики по весу позиций. Возвращает dict со
    стоимостью, способом доставки и сроком.

    items: [(rfq_item, part, qty, unit_price), …]
    """
    total_weight_kg = Decimal("0")
    for _, p, qty, _ in items:
        w = Decimal(str(getattr(p, "gross_weight_kg", None) or "1.0"))
        total_weight_kg += w * qty
    # Тариф: $4/kg base + $50 fixed (в реальности — отдельный сервис)
    rate_per_kg = Decimal("4.00")
    fixed = Decimal("50.00")
    cost = (total_weight_kg * rate_per_kg + fixed).quantize(Decimal("0.01"))
    # Способ: > 100kg → авто, иначе авиа
    method = "Авто (Россия)" if total_weight_kg > Decimal("100") else "Авиа (экспресс)"
    return {
        "weight_kg": float(total_weight_kg),
        "cost": float(cost),
        "method": method,
    }


# ══════════════════════════════════════════════════════════
# AUTO — кнопка «Подтвердить и зарезервировать 10%»
# ══════════════════════════════════════════════════════════

@register("present_kp_to_buyer")
def present_kp_to_buyer(params, user, role):
    """Показывает buyer'у инвойс на 100% по самому дешёвому КП с кнопкой
    «✓ Подтвердить и зарезервировать 10%».

    Используется во всех трёх режимах после того, как КП готово к
    показу клиенту: AUTO — сразу после auto-quote; SEMI — после
    op_approve_kp; MANUAL — после op_compose_kp.

    params: {rfq_id}
    """
    from marketplace.models import RFQ, Quote
    try:
        rfq = RFQ.objects.get(id=int(params.get("rfq_id") or 0))
    except (RFQ.DoesNotExist, ValueError, TypeError):
        return ActionResult(text="RFQ не найден.")

    if rfq.created_by_id != user.id:
        return ActionResult(text="Просматривать КП может только заказчик.")

    best = (Quote.objects.filter(
        rfq=rfq, direction="seller_to_buyer",
        status__in=("submitted", "finalized"),
    ).select_related("seller").order_by("total_amount").first())
    if not best:
        return ActionResult(text=(
            "По RFQ ещё нет готовых котировок. Дождитесь сбора КП."
        ))

    # Логистика — суммируем по items
    items_for_logi = []
    for qi in best.items.all():
        if not qi.part:
            continue
        items_for_logi.append((qi.rfq_item, qi.part, qi.quantity, qi.unit_price))
    logi = _calc_logistics(items_for_logi)

    parts_total = best.total_amount
    logi_cost_d = Decimal(str(logi["cost"]))
    full_invoice = (parts_total + logi_cost_d).quantize(Decimal("0.01"))

    # Early-warning: если сумма КП меньше минимума, сразу показываем
    # blocker — не строим Pro-forma, не плодим UI.
    from .order_limits import check_min_order
    block = check_min_order(full_invoice)
    if block:
        return ActionResult(**block)

    reserve = (full_invoice * Decimal("0.10")).quantize(Decimal("0.01"))

    # ── Генерируем настоящий PDF Pro-forma Invoice ──────────────
    proforma_url = ""
    try:
        from .documents import _build_proforma_invoice_pdf, _save_proforma_pdf
        buf = _build_proforma_invoice_pdf(rfq, best, logi_cost_d, user)
        proforma_url = _save_proforma_pdf(rfq, best, buf)
    except Exception:
        logger.exception("proforma invoice generation failed")

    # «Шапка» официального инвойса для UI-карточки
    rows = [
        {"label": "Документ",   "value": f"PRO-{rfq.id}/{best.id} · Pro-forma Invoice"},
        {"label": "Покупатель", "value": user.get_full_name() or user.username},
        {"label": "Поставщик",  "value": "Поставщик №1 (имя раскрывается после подтверждения)"},
        {"label": "Режим",      "value": {"auto": "AUTO", "semi": "SEMI",
                                            "manual": "MANUAL",
                                            "manual_oem": "MANUAL"}.get(rfq.mode, rfq.mode)},
        {"label": "Позиций",    "value": str(rfq.items.count())},
        {"label": "Запчасти",   "value": f"${parts_total:,.2f}"},
        {"label": f"Логистика ({logi['method']}, {logi['weight_kg']:.1f} кг)",
         "value": f"${logi['cost']:,.2f}"},
        {"label": "ИНВОЙС 100%", "value": f"${full_invoice:,.2f}", "primary": True},
        {"label": "Срок поставки",  "value": f"{best.delivery_days} дней"},
        {"label": "Условия оплаты", "value": "10% резерв сейчас · 90% перед отгрузкой"},
        {"label": "Резерв 10%",     "value": f"${reserve:,.2f}", "primary": True},
        {"label": "К оплате после готовности", "value": f"${full_invoice - reserve:,.2f}"},
    ]
    actions = []
    if proforma_url:
        actions.append({
            "action": "open_url",
            "label": "📄 Открыть Pro-forma Invoice (PDF)",
            "params": {"_url": proforma_url},
        })
    actions.append({
        "action": "view_rfq_quotes", "label": "📊 Сравнить все КП",
        "params": {"rfq_id": rfq.id},
    })

    return ActionResult(
        text=(
            f"📋 Pro-forma Invoice PRO-{rfq.id}/{best.id} готов.\n"
            f"Сумма: ${full_invoice:,.2f} (запчасти ${parts_total:,.0f} + "
            f"логистика ${logi['cost']:,.0f}).\n"
            f"Нажмите «Подтвердить» — заблокируем 10% (${reserve:,.0f}) "
            f"с депозита, после готовности — остаток 90%."
        ),
        cards=[{"type": "draft", "data": {
            "title": f"📋 PRO-{rfq.id}/{best.id} · ${full_invoice:,.2f}",
            "rows": rows,
            "doc_url": proforma_url,
            "warnings": [
                "После подтверждения чат переключится в режим сделки (shipment).",
                "Остальные котировки по этому RFQ автоматически отклоняются.",
            ],
            "confirm_action": "confirm_kp_and_reserve",
            "confirm_label": f"✓ Подтвердить и зарезервировать ${reserve:,.0f}",
            "confirm_params": {
                "rfq_id": rfq.id, "quote_id": best.id,
                "logistics_cost": float(logi["cost"]),
            },
            "cancel_label": "Сравнить все КП",
        }}],
        actions=actions,
    )


@register("confirm_kp_and_reserve")
def confirm_kp_and_reserve(params, user, role):
    """Финальная кнопка во всех трёх режимах: подтверждение клиента →
    резерв 10% → calc-чат становится shipment-чатом.
    """
    from django.db import transaction

    from marketplace.models import RFQ, Order, OrderItem, Quote

    from . import payments as _pay
    from .actions import _log_event
    from .models import Wallet

    try:
        rfq = RFQ.objects.get(id=int(params.get("rfq_id") or 0))
        q = Quote.objects.select_related("seller").get(id=int(params.get("quote_id") or 0))
    except (RFQ.DoesNotExist, Quote.DoesNotExist, ValueError, TypeError):
        return ActionResult(text="RFQ или котировка не найдены.")

    if rfq.created_by_id != user.id:
        return ActionResult(text="Подтвердить КП может только заказчик.")
    if q.rfq_id != rfq.id:
        return ActionResult(text="Котировка не относится к этому RFQ.")
    if q.status not in ("submitted", "finalized"):
        return ActionResult(text=f"Эту котировку нельзя принять (статус: {q.get_status_display()}).")

    # Полная сумма = запчасти + логистика
    parts_total = q.total_amount
    logi_cost = Decimal(str(params.get("logistics_cost") or 0))
    full_invoice = (parts_total + logi_cost).quantize(Decimal("0.01"))

    # Бизнес-правило: минимальная сумма заказа (см. assistant/order_limits.py).
    from .order_limits import check_min_order
    block = check_min_order(full_invoice)
    if block:
        return ActionResult(**block)

    reserve = (full_invoice * Decimal("0.10")).quantize(Decimal("0.01"))

    wallet = Wallet.for_user(user)
    if wallet.balance < reserve:
        shortage = reserve - wallet.balance
        return ActionResult(text=(
            f"❌ Недостаточно средств для резерва.\n"
            f"Нужно: ${reserve:,.2f} · на счёте: ${wallet.balance:,.2f} · "
            f"не хватает: ${shortage:,.2f}."
        ), actions=[
            {"label": f"Пополнить депозит на ${max(shortage * 2, 10000):,.0f}",
             "action": "topup_wallet",
             "params": {"amount": float(max(shortage * 2, 10000))}},
        ])

    with transaction.atomic():
        # 1. Order
        order = Order.objects.create(
            customer_name=user.get_full_name() or user.username,
            customer_email=user.email or f"{user.username}@chat.local",
            customer_phone="",
            delivery_address="—",
            buyer=user,
            status="reserve_paid",
            payment_status="reserve_paid",
            payment_scheme="simple",
            reserve_percent=Decimal("10.00"),
            reserve_amount=reserve,
            total_amount=full_invoice,
            reserve_paid_at=timezone.now(),
            logistics_cost=logi_cost,
        )
        for qi in q.items.all():
            if not qi.part:
                continue
            OrderItem.objects.create(
                order=order, part=qi.part,
                quantity=qi.quantity, unit_price=qi.unit_price,
            )
        # 2. Принять Quote, отклонить остальные
        q.status = "accepted"
        q.save(update_fields=["status"])
        Quote.objects.filter(rfq=rfq).exclude(id=q.id).filter(
            status__in=("submitted", "finalized", "countered"),
        ).update(status="declined")
        rfq.status = "quoted"
        rfq.save(update_fields=["status"])
        # 3. Эскроу
        intent = _pay.create_payment_intent(reserve, order_id=order.id, payer=user, kind="reserve")
        _pay.confirm_payment_intent(intent, user)

    _log_event(order, "kp_confirmed", actor=user, source="buyer",
               meta={"quote_id": q.id, "rfq_id": rfq.id,
                     "parts": float(parts_total), "logistics": float(logi_cost),
                     "reserve": float(reserve), "mode": rfq.mode})

    if q.seller:
        _notify(q.seller, kind="order",
                title=f"✅ КП #{q.id} принято — ORD-{order.id}",
                body=f"Резерв ${reserve:,.0f} удержан. Можно запускать в производство.",
                url=f"/chat/?order={order.id}")

    # 4. Переключаем чат calc → shipment + системное сообщение
    conv = _conv_for_rfq(user, rfq)
    _switch_conv_to_shipment(conv, order)

    # 5. Сразу генерируем официальный Commercial Invoice PDF на Order
    invoice_url = ""
    try:
        from .documents import _build_invoice_pdf, _doc_url, _save_pdf
        buf = _build_invoice_pdf(order)
        doc = _save_pdf(order, "invoice", f"Commercial Invoice ORD-{order.id}",
                         buf, user)
        invoice_url = _doc_url(doc)
    except Exception:
        logger.exception("commercial invoice generation failed")

    wallet.refresh_from_db()
    actions = [
        {"action": "track_order", "label": "📦 Трекинг",
         "params": {"order_id": order.id}},
        {"action": "pay_final",
         "label": f"💳 Оплатить остаток ${full_invoice - reserve:,.0f}",
         "params": {"order_id": order.id}},
    ]
    if invoice_url:
        actions.insert(0, {
            "action": "open_url",
            "label": "📄 Скачать Commercial Invoice (PDF)",
            "params": {"_url": invoice_url},
        })
    return ActionResult(
        text=(
            f"✅ КП подтверждено — сделка перешла в работу.\n"
            f"Заказ ORD-{order.id} · инвойс ${full_invoice:,.2f}\n"
            f"Резерв 10% (${reserve:,.2f}) списан · остаток депозита ${wallet.balance:,.2f}\n"
            f"Чат теперь — сделка. Commercial Invoice выставлен."
        ),
        cards=[{"type": "order", "data": {
            "id": str(order.id),
            "number": order.id,
            "status": "reserve_paid",
            "status_label": "Заказ оформлен",
            "total": float(full_invoice),
            "currency": "USD",
            "payment_status": "reserve_paid",
            "payment_status_label": f"Резерв ${reserve:,.0f} удержан",
            "invoice_url": invoice_url,
        }}],
        actions=actions,
    )


# ══════════════════════════════════════════════════════════
# SEMI — operator approval с SLA 15 минут
# ══════════════════════════════════════════════════════════

@register("op_approve_kp")
def op_approve_kp(params, user, role):
    """Operator approves a SEMI-mode RFQ's auto-generated КП.

    После approve КП показывается buyer'у через present_kp_to_buyer.
    SLA: 15 минут с момента создания RFQ — иначе эскалация.

    params: {rfq_id, confirmed?}
    """
    from marketplace.models import RFQ, Quote
    if not (role or "").startswith("operator") and not user.is_staff:
        return ActionResult(text="Только оператор может подтверждать КП в SEMI-режиме.")
    try:
        rfq = RFQ.objects.get(id=int(params.get("rfq_id") or 0))
    except (RFQ.DoesNotExist, ValueError, TypeError):
        return ActionResult(text="RFQ не найден.")

    if rfq.mode != "semi":
        return ActionResult(text=f"RFQ #{rfq.id} не в SEMI-режиме (mode={rfq.mode}).")

    quotes = Quote.objects.filter(rfq=rfq, direction="seller_to_buyer", status="submitted")
    if not quotes.exists():
        return ActionResult(text=(
            "По этому RFQ ещё нет КП от продавцов. "
            "Сначала запустите рассылку (send_rfq_to_suppliers)."
        ))

    if not params.get("confirmed"):
        elapsed = timezone.now() - rfq.created_at
        sla_left = timedelta(minutes=SLA_OPERATOR_APPROVE_MINUTES) - elapsed
        sla_status = (
            f"⏱ SLA: {int(sla_left.total_seconds() // 60)} мин"
            if sla_left.total_seconds() > 0 else "⚠️ SLA нарушен"
        )
        best = quotes.order_by("total_amount").first()
        return ActionResult(
            text=(
                f"📋 SEMI: одобрить КП по RFQ #{rfq.id}?\n"
                f"Лучшее предложение #{best.id} от {best.seller.username if best.seller else '—'} — "
                f"${best.total_amount:,.0f}. {sla_status}."
            ),
            cards=[{"type": "draft", "data": {
                "title": f"Подтвердить КП по RFQ #{rfq.id}",
                "rows": [
                    {"label": "Заказчик", "value": rfq.customer_name},
                    {"label": "Позиций", "value": str(rfq.items.count())},
                    {"label": "КП от продавцов", "value": str(quotes.count())},
                    {"label": "Лучшее", "value": f"${best.total_amount:,.0f}", "primary": True},
                    {"label": "SLA", "value": sla_status},
                ],
                "confirm_action": "op_approve_kp",
                "confirm_label": "✓ Одобрить и отправить клиенту",
                "confirm_params": {"rfq_id": rfq.id, "confirmed": True},
                "cancel_label": "Отклонить",
            }}],
        )

    # Маркер approve в notes
    approve_line = (
        f" | KP_APPROVED: by {user.username} at "
        f"{timezone.now().strftime('%Y-%m-%d %H:%M')}"
    )
    rfq.notes = (rfq.notes or "")[:4500] + approve_line
    rfq.save(update_fields=["notes"])

    # Уведомляем buyer'а — КП готово
    _notify(
        rfq.created_by, kind="rfq",
        title=f"📋 КП по RFQ #{rfq.id} готово к рассмотрению",
        body="Оператор одобрил расчёт. Откройте, чтобы подтвердить и зарезервировать 10%.",
        url=f"/chat/rfq/{rfq.id}/?source=kp-ready",
    )

    return ActionResult(
        text=(
            f"✓ КП по RFQ #{rfq.id} одобрено и отправлено клиенту.\n"
            f"Клиент видит инвойс с кнопкой «Подтвердить и зарезервировать 10%»."
        ),
    )


# ══════════════════════════════════════════════════════════
# MANUAL — operator dispatches manually, 48h collection
# ══════════════════════════════════════════════════════════

@register("op_dispatch_manual_rfq")
def op_dispatch_manual_rfq(params, user, role):
    """Оператор вручную отправляет MANUAL-RFQ выбранным поставщикам.

    Срок сбора предложений — 48 часов. После — оператор может вызвать
    op_compose_kp для формирования финального КП buyer'у.

    params: {rfq_id, seller_ids?: [int], confirmed?}
    """
    from marketplace.models import RFQ
    if not (role or "").startswith("operator") and not user.is_staff:
        return ActionResult(text="Только оператор может работать с MANUAL-RFQ.")
    try:
        rfq = RFQ.objects.get(id=int(params.get("rfq_id") or 0))
    except (RFQ.DoesNotExist, ValueError, TypeError):
        return ActionResult(text="RFQ не найден.")

    if rfq.mode not in ("manual", "manual_oem"):
        return ActionResult(text=f"RFQ #{rfq.id} не в MANUAL-режиме (mode={rfq.mode}).")

    # Используем стандартный send_rfq_to_suppliers — но фиксируем deadline
    from .negotiation import send_rfq_to_suppliers
    res = send_rfq_to_suppliers(
        {"rfq_id": rfq.id, "confirmed": True}, user, "operator",
    )

    deadline = timezone.now() + timedelta(hours=SLA_MANUAL_QUOTE_COLLECTION_HOURS)
    deadline_line = (
        f" | MANUAL_DEADLINE: {deadline.strftime('%Y-%m-%d %H:%M')} "
        f"(48h от {timezone.now().strftime('%Y-%m-%d %H:%M')})"
    )
    rfq.notes = (rfq.notes or "")[:4500] + deadline_line
    rfq.save(update_fields=["notes"])

    # Buyer'а уведомляем что в работе
    _notify(
        rfq.created_by, kind="rfq",
        title=f"🔍 RFQ #{rfq.id} — оператор собирает предложения",
        body=f"Срок сбора КП: 48ч до {deadline.strftime('%d.%m %H:%M')}. Вы получите готовое КП.",
        url=f"/chat/rfq/{rfq.id}/?source=manual-collecting",
    )

    return ActionResult(
        text=(
            f"✓ MANUAL-RFQ #{rfq.id} разослан.\n"
            f"⏱ Дедлайн сбора КП: {deadline.strftime('%d.%m %H:%M')} (48 часов).\n"
            f"После сбора — op_compose_kp чтобы сформировать инвойс клиенту.\n\n"
            + (res.text or "")
        ),
        actions=[
            {"action": "op_compose_kp", "label": "📋 Сформировать КП клиенту",
             "params": {"rfq_id": rfq.id}},
            {"action": "view_rfq_quotes", "label": "📊 Полученные КП",
             "params": {"rfq_id": rfq.id}},
        ],
    )


@register("op_compose_kp")
def op_compose_kp(params, user, role):
    """Оператор формирует финальное КП клиенту по итогам ручного сбора (MANUAL).

    Работает так же, как auto-end-of-flow: выбирает лучшее КП → buyer
    видит карточку с кнопкой «Подтвердить и зарезервировать 10%».

    params: {rfq_id, quote_id?}  — если quote_id передан, оператор сам выбрал
    """
    from marketplace.models import RFQ, Quote
    if not (role or "").startswith("operator") and not user.is_staff:
        return ActionResult(text="Только оператор формирует MANUAL-КП.")
    try:
        rfq = RFQ.objects.get(id=int(params.get("rfq_id") or 0))
    except (RFQ.DoesNotExist, ValueError, TypeError):
        return ActionResult(text="RFQ не найден.")

    qs = Quote.objects.filter(rfq=rfq, direction="seller_to_buyer", status="submitted")
    if params.get("quote_id"):
        try:
            chosen = qs.get(id=int(params["quote_id"]))
        except Quote.DoesNotExist:
            return ActionResult(text="Выбранная котировка не найдена.")
    else:
        chosen = qs.order_by("total_amount").first()
    if not chosen:
        return ActionResult(text="По этому RFQ нет КП от продавцов.")

    # Маркер «оператор сформировал КП» в notes
    line = (
        f" | KP_COMPOSED: quote #{chosen.id} by op {user.username} at "
        f"{timezone.now().strftime('%Y-%m-%d %H:%M')}"
    )
    rfq.notes = (rfq.notes or "")[:4500] + line
    rfq.save(update_fields=["notes"])

    _notify(
        rfq.created_by, kind="rfq",
        title=f"📋 КП по RFQ #{rfq.id} сформировано",
        body=f"Оператор сформировал инвойс на ${chosen.total_amount:,.0f}. "
             f"Откройте, чтобы подтвердить и зарезервировать 10%.",
        url=f"/chat/rfq/{rfq.id}/?source=kp-ready",
    )

    return ActionResult(
        text=(
            f"✓ КП #{chosen.id} (${chosen.total_amount:,.0f}) "
            f"отправлено клиенту по RFQ #{rfq.id}."
        ),
    )

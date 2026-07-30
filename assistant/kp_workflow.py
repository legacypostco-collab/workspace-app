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
from django.utils.translation import gettext as _

from .actions import ActionResult, _full_order_cards, _notify, register
from .security import confirmation_is_true

logger = logging.getLogger(__name__)


# ── Параметры SLA ────────────────────────────────────────────────
SLA_OPERATOR_APPROVE_MINUTES = 15
SLA_MANUAL_QUOTE_COLLECTION_HOURS = 48
MAX_KP_TOTAL = Decimal("999999999999.99")
MAX_KP_ITEM_QUANTITY = 1_000_000


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
            _('✅ КП подтверждено — сделка перешла в работу.\nЗаказ ORD-%(p0)s · $%(p1)s · резерв 10%% ($%(p2)s) удержан в эскроу.') % {"p0": f'{order.id}', "p1": f'{order.total_amount:,.2f}', "p2": f'{order.reserve_amount:,.2f}'}
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


def _quote_logistics_cost(quote) -> Decimal:
    items = [
        (qi.rfq_item, qi.part, qi.quantity, qi.unit_price)
        for qi in quote.items.select_related("rfq_item", "part").all()
        if qi.part_id
    ]
    return Decimal(str(_calc_logistics(items)["cost"])).quantize(
        Decimal("0.01")
    )


def _quote_financials(quote):
    """Validate stored quote rows before using them in an invoice or payment."""
    try:
        parts_total = Decimal(str(quote.total_amount))
    except Exception:
        return None
    if (
        not parts_total.is_finite()
        or parts_total <= 0
        or parts_total > MAX_KP_TOTAL
    ):
        return None

    quote_items = list(quote.items.select_related("part", "rfq_item").all())
    if not quote_items or any(item.part_id is None for item in quote_items):
        return None
    calculated_total = Decimal("0")
    for item in quote_items:
        try:
            quantity = int(item.quantity)
            unit_price = Decimal(str(item.unit_price))
        except Exception:
            return None
        if (
            quantity < 1
            or quantity > MAX_KP_ITEM_QUANTITY
            or not unit_price.is_finite()
            or unit_price <= 0
            or unit_price > MAX_KP_TOTAL
        ):
            return None
        calculated_total += unit_price * quantity
    if (
        not calculated_total.is_finite()
        or abs(calculated_total - parts_total) > Decimal("0.01")
    ):
        return None

    try:
        logistics_cost = _quote_logistics_cost(quote)
        full_invoice = (parts_total + logistics_cost).quantize(Decimal("0.01"))
    except Exception:
        return None
    if (
        not logistics_cost.is_finite()
        or logistics_cost < 0
        or not full_invoice.is_finite()
        or full_invoice <= 0
        or full_invoice > MAX_KP_TOTAL
    ):
        return None
    return parts_total, logistics_cost, full_invoice


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
        return ActionResult(text=_("RFQ не найден."))

    if rfq.created_by_id != user.id:
        return ActionResult(text=_("Просматривать КП может только заказчик."))
    if rfq.status == "cancelled":
        return ActionResult(text=_("RFQ отменён — коммерческое предложение закрыто."))

    best = (Quote.objects.filter(
        rfq=rfq, direction="seller_to_buyer",
        status__in=("submitted", "finalized"),
    ).select_related("seller").order_by("total_amount").first())
    if not best:
        return ActionResult(text=(
            _("По RFQ ещё нет готовых котировок. Дождитесь сбора КП.")
        ))

    financials = _quote_financials(best)
    if financials is None:
        return ActionResult(
            text=_("Котировка содержит некорректные позиции или сумму и требует проверки оператора.")
        )
    parts_total, logi_cost_d, full_invoice = financials
    logi = _calc_logistics([
        (qi.rfq_item, qi.part, qi.quantity, qi.unit_price)
        for qi in best.items.select_related("rfq_item", "part").all()
    ])

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
        {"label": _("Документ"),   "value": f"PRO-{rfq.id}/{best.id} · Pro-forma Invoice"},
        {"label": _("Покупатель"), "value": user.get_full_name() or user.username},
        {"label": _("Поставщик"),  "value": _("Поставщик №1 (имя раскрывается после подтверждения)")},
        {"label": _("Режим"),      "value": {"auto": "AUTO", "semi": "SEMI",
                                            "manual": "MANUAL",
                                            "manual_oem": "MANUAL"}.get(rfq.mode, rfq.mode)},
        {"label": _("Позиций"),    "value": str(rfq.items.count())},
        {"label": _("Запчасти"),   "value": f"${parts_total:,.2f}"},
        {"label": _('Логистика (%(p0)s, %(p1)s кг)') % {"p0": f"{logi['method']}", "p1": f"{logi['weight_kg']:.1f}"},
         "value": f"${logi['cost']:,.2f}"},
        {"label": _("ИНВОЙС 100%"), "value": f"${full_invoice:,.2f}", "primary": True},
        {"label": _("Срок поставки"),  "value": f"{best.delivery_days} дней"},
        {"label": _("Условия оплаты"), "value": _("10% резерв сейчас · 90% перед отгрузкой")},
        {"label": _("Резерв 10%"),     "value": f"${reserve:,.2f}", "primary": True},
        {"label": _("К оплате после готовности"), "value": f"${full_invoice - reserve:,.2f}"},
    ]
    actions = []
    if proforma_url:
        actions.append({
            "action": "open_url",
            "label": _("📄 Открыть Pro-forma Invoice (PDF)"),
            "params": {"_url": proforma_url},
        })
    actions.append({
        "action": "view_rfq_quotes", "label": _("📊 Сравнить все КП"),
        "params": {"rfq_id": rfq.id},
    })

    return ActionResult(
        text=(
            _('📋 Pro-forma Invoice PRO-%(p0)s/%(p1)s готов.\nСумма: $%(p2)s (запчасти $%(p3)s + логистика $%(p4)s).\nНажмите «Подтвердить» — заблокируем 10%% ($%(p5)s) с депозита, после готовности — остаток 90%%.') % {"p0": f'{rfq.id}', "p1": f'{best.id}', "p2": f'{full_invoice:,.2f}', "p3": f'{parts_total:,.0f}', "p4": f"{logi['cost']:,.0f}", "p5": f'{reserve:,.0f}'}
        ),
        cards=[{"type": "draft", "data": {
            "title": f"📋 PRO-{rfq.id}/{best.id} · ${full_invoice:,.2f}",
            "rows": rows,
            "doc_url": proforma_url,
            "warnings": [
                _("После подтверждения чат переключится в режим сделки (shipment)."),
                _("Остальные котировки по этому RFQ автоматически отклоняются."),
            ],
            "confirm_action": "confirm_kp_and_reserve",
            "confirm_label": _('✓ Подтвердить и зарезервировать $%(p0)s') % {"p0": f'{reserve:,.0f}'},
            "confirm_params": {
                "rfq_id": rfq.id, "quote_id": best.id,
                "logistics_cost": float(logi["cost"]),
            },
            "cancel_label": _("Сравнить все КП"),
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
        return ActionResult(text=_("RFQ или котировка не найдены."))

    if rfq.created_by_id != user.id:
        return ActionResult(text=_("Подтвердить КП может только заказчик."))
    if rfq.status == "cancelled":
        return ActionResult(text=_("RFQ отменён — подтвердить КП нельзя."))
    if q.rfq_id != rfq.id:
        return ActionResult(text=_("Котировка не относится к этому RFQ."))
    if q.status not in ("submitted", "finalized"):
        return ActionResult(text=_('Эту котировку нельзя принять (статус: %(p0)s).') % {"p0": f'{q.get_status_display()}'})

    # FIX (HIGH): проверка срока действия котировки. Раньше можно было принять
    # просроченное КП (valid_until заносится при auto_generate, но не проверялось).
    from django.utils import timezone as _tz
    if q.valid_until and _tz.now() > q.valid_until:
        return ActionResult(text=(
            _('Котировка истекла %(p0)s. Запросите у поставщика новое КП.') % {"p0": f"{q.valid_until.strftime('%d.%m.%Y %H:%M')}"}
        ))

    # Полная сумма = запчасти + серверный расчёт логистики. Значение из
    # параметров кнопки не является доверенным и намеренно игнорируется.
    financials = _quote_financials(q)
    if financials is None:
        return ActionResult(
            text=_("Котировка содержит некорректные позиции или сумму и не может быть принята.")
        )
    parts_total, logi_cost, full_invoice = financials

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
            _('❌ Недостаточно средств для резерва.\nНужно: $%(p0)s · на счёте: $%(p1)s · не хватает: $%(p2)s.') % {"p0": f'{reserve:,.2f}', "p1": f'{wallet.balance:,.2f}', "p2": f'{shortage:,.2f}'}
        ), actions=[
            {"label": _('Пополнить депозит на $%(p0)s') % {"p0": f'{max(shortage * 2, 10000):,.0f}'},
             "action": "topup_wallet",
             "params": {"amount": float(max(shortage * 2, 10000))}},
        ])

    with transaction.atomic():
        # FIX (БАГ 1): double-click/гонка — re-check под блокировкой.
        # Проверка статуса выше (q.status not in ...) идёт вне транзакции, поэтому
        # два одновременных клика проходят её одновременно и создают два Order +
        # два резерва (idempotency платёжного слоя не ловит — create_payment_intent
        # генерит новый uuid каждый раз). Зеркалим pay_reserve (actions.py).
        rfq = RFQ.objects.select_for_update().get(id=rfq.id)
        if rfq.status == "cancelled":
            return ActionResult(text=_("RFQ отменён — подтвердить КП нельзя."))
        q = Quote.objects.select_for_update().get(id=q.id)
        if q.rfq_id != rfq.id or q.status not in ("submitted", "finalized"):
            return ActionResult(text=_("Заказ по этой котировке уже создан или она недоступна."))
        if q.valid_until and timezone.now() > q.valid_until:
            return ActionResult(text=_("Срок действия котировки истёк."))
        financials = _quote_financials(q)
        if financials is None:
            return ActionResult(
                text=_("Котировка содержит некорректные позиции или сумму и не может быть принята.")
            )
        parts_total, logi_cost, full_invoice = financials
        block = check_min_order(full_invoice)
        if block:
            return ActionResult(**block)
        reserve = (full_invoice * Decimal("0.10")).quantize(Decimal("0.01"))
        # Кошелёк тоже под блокировкой + перепроверка баланса.
        wallet = Wallet.objects.select_for_update().get(pk=wallet.pk)
        if wallet.balance < reserve:
            return ActionResult(text=(
                _('❌ Недостаточно средств для резерва (перепроверка).\nНужно: $%(p0)s · на счёте: $%(p1)s.') % {"p0": f'{reserve:,.2f}', "p1": f'{wallet.balance:,.2f}'}
            ))
        # FIX (БАГ 2): не создаём Order из 0 позиций — ниже цикл OrderItem
        # пропускает qi с part is None, и при пустом КП получался Order без
        # позиций, но с реальным списанием резерва. Проверяем заранее.
        if not any(qi.part_id for qi in q.items.all()):
            return ActionResult(text=(
                _("В котировке нет позиций с привязанной запчастью — "
                "заказ не создан, резерв не списан.")
            ))
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

    # Реферал: первый оплаченный резерв приглашённого → $100 пригласившему.
    try:
        from . import referral as _ref
        _ref.on_order_reserve_paid(order)
    except Exception:
        pass

    if q.seller:
        _notify(q.seller, kind="order",
                title=_('✅ КП #%(p0)s принято — ORD-%(p1)s') % {"p0": f'{q.id}', "p1": f'{order.id}'},
                body=_('Резерв $%(p0)s удержан. Можно запускать в производство.') % {"p0": f'{reserve:,.0f}'},
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
        {"action": "track_order", "label": _("📦 Трекинг"),
         "params": {"order_id": order.id}},
        {"action": "pay_final",
         "label": _('💳 Оплатить остаток $%(p0)s') % {"p0": f'{full_invoice - reserve:,.0f}'},
         "params": {"order_id": order.id}},
    ]
    if invoice_url:
        actions.insert(0, {
            "action": "open_url",
            "label": _("📄 Скачать Commercial Invoice (PDF)"),
            "params": {"_url": invoice_url},
        })
    return ActionResult(
        text=(
            _('✅ КП подтверждено — сделка перешла в работу.\nЗаказ ORD-%(p0)s · инвойс $%(p1)s\nРезерв 10%% ($%(p2)s) списан · остаток депозита $%(p3)s\nЧат теперь — сделка. Commercial Invoice выставлен.') % {"p0": f'{order.id}', "p1": f'{full_invoice:,.2f}', "p2": f'{reserve:,.2f}', "p3": f'{wallet.balance:,.2f}'}
        ),
        cards=_full_order_cards(order, user, role, fallback={"type": "order", "data": {
            "id": str(order.id),
            "number": order.id,
            "status": "reserve_paid",
            "status_label": _("Заказ оформлен"),
            "total": float(full_invoice),
            "currency": "USD",
            "payment_status": "reserve_paid",
            "payment_status_label": _('Резерв $%(p0)s удержан') % {"p0": f'{reserve:,.0f}'},
            "invoice_url": invoice_url,
        }}),
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
    if not ((role or "").startswith("operator") or role == "admin"):
        return ActionResult(text=_("Только оператор может подтверждать КП в SEMI-режиме."))
    try:
        rfq = RFQ.objects.get(id=int(params.get("rfq_id") or 0))
    except (RFQ.DoesNotExist, ValueError, TypeError):
        return ActionResult(text=_("RFQ не найден."))

    if rfq.mode != "semi":
        return ActionResult(text=_('RFQ #%(p0)s не в SEMI-режиме (mode=%(p1)s).') % {"p0": f'{rfq.id}', "p1": f'{rfq.mode}'})
    if rfq.status == "cancelled":
        return ActionResult(text=_("RFQ отменён — подтверждать КП нельзя."))

    quotes = Quote.objects.filter(rfq=rfq, direction="seller_to_buyer", status="submitted")
    if not quotes.exists():
        # SEMI-режим: KP формируется автоматически из каталога. Подгружаем
        # spec-таблицу прямо сюда — оператор сразу видит что подтверждать.
        from .actions import get_rfq_status as _grs
        spec_result = _grs({"rfq_id": rfq.id}, user, role)
        return ActionResult(
            text=(
                _('RFQ #%(p0)s · подтвердите позиции и зафиксируйте КП. Спорные строки можно заменить аналогом или пометить «нет в каталоге».') % {"p0": f'{rfq.id}'}
            ),
            cards=spec_result.cards,
            actions=[
                {"label": _("Подтвердить КП"), "action": "op_compose_kp",
                 "params": {"rfq_id": rfq.id}},
                {"label": _("Спросить у клиента"), "action": "ask_about_rfq",
                 "params": {"rfq_id": rfq.id}},
            ],
        )

    if not confirmation_is_true(params.get("confirmed")):
        elapsed = timezone.now() - rfq.created_at
        sla_left = timedelta(minutes=SLA_OPERATOR_APPROVE_MINUTES) - elapsed
        sla_status = (
            _('⏱ SLA: %(p0)s мин') % {"p0": int(sla_left.total_seconds() // 60)}
            if sla_left.total_seconds() > 0 else _("⚠️ SLA нарушен")
        )
        best = quotes.order_by("total_amount").first()
        return ActionResult(
            text=(
                _('📋 SEMI: одобрить КП по RFQ #%(p0)s?\nЛучшее предложение #%(p1)s от %(p2)s — $%(p3)s. %(p4)s.') % {"p0": f'{rfq.id}', "p1": f'{best.id}', "p2": f"{(best.seller.username if best.seller else '—')}", "p3": f'{best.total_amount:,.0f}', "p4": f'{sla_status}'}
            ),
            cards=[{"type": "draft", "data": {
                "title": _('Подтвердить КП по RFQ #%(p0)s') % {"p0": f'{rfq.id}'},
                "rows": [
                    {"label": _("Заказчик"), "value": rfq.customer_name},
                    {"label": _("Позиций"), "value": str(rfq.items.count())},
                    {"label": _("КП от продавцов"), "value": str(quotes.count())},
                    {"label": _("Лучшее"), "value": f"${best.total_amount:,.0f}", "primary": True},
                    {"label": "SLA", "value": sla_status},
                ],
                "confirm_action": "op_approve_kp",
                "confirm_label": _("✓ Одобрить и отправить клиенту"),
                "confirm_params": {"rfq_id": rfq.id, "confirmed": True},
                "cancel_label": _("Отклонить"),
            }}],
        )

    from django.db import transaction

    with transaction.atomic():
        rfq = RFQ.objects.select_for_update().get(pk=rfq.pk)
        if rfq.status == "cancelled" or rfq.mode != "semi":
            return ActionResult(text=_("RFQ отменён или больше не доступен для подтверждения."))
        approve_line = (
            f" | KP_APPROVED: by {user.username} at "
            f"{timezone.now().strftime('%Y-%m-%d %H:%M')}"
        )
        rfq.notes = (rfq.notes or "")[:4500] + approve_line
        rfq.save(update_fields=["notes"])

    # Уведомляем buyer'а — КП готово
    _notify(
        rfq.created_by, kind="rfq",
        title=_('📋 КП по RFQ #%(p0)s готово к рассмотрению') % {"p0": f'{rfq.id}'},
        body=_("Оператор одобрил расчёт. Откройте, чтобы подтвердить и зарезервировать 10%."),
        url=f"/chat/rfq/{rfq.id}/?source=kp-ready",
    )

    return ActionResult(
        text=(
            _('✓ КП по RFQ #%(p0)s одобрено и отправлено клиенту.\nКлиент видит инвойс с кнопкой «Подтвердить и зарезервировать 10%%».') % {"p0": f'{rfq.id}'}
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
    if not ((role or "").startswith("operator") or role == "admin"):
        return ActionResult(text=_("Только оператор может работать с MANUAL-RFQ."))
    try:
        rfq = RFQ.objects.get(id=int(params.get("rfq_id") or 0))
    except (RFQ.DoesNotExist, ValueError, TypeError):
        return ActionResult(text=_("RFQ не найден."))

    if rfq.mode not in ("manual", "manual_oem"):
        return ActionResult(text=_('RFQ #%(p0)s не в MANUAL-режиме (mode=%(p1)s).') % {"p0": f'{rfq.id}', "p1": f'{rfq.mode}'})
    if rfq.status == "cancelled":
        return ActionResult(text=_("RFQ отменён — повторная рассылка запрещена."))

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
    from django.db import transaction

    with transaction.atomic():
        rfq = RFQ.objects.select_for_update().get(pk=rfq.pk)
        if rfq.status == "cancelled":
            return ActionResult(text=_("RFQ отменён — повторная рассылка запрещена."))
        rfq.notes = (rfq.notes or "")[:4500] + deadline_line
        rfq.save(update_fields=["notes"])

    # Buyer'а уведомляем что в работе
    _notify(
        rfq.created_by, kind="rfq",
        title=_('🔍 RFQ #%(p0)s — оператор собирает предложения') % {"p0": f'{rfq.id}'},
        body=_('Срок сбора КП: 48ч до %(p0)s. Вы получите готовое КП.') % {"p0": f"{deadline.strftime('%d.%m %H:%M')}"},
        url=f"/chat/rfq/{rfq.id}/?source=manual-collecting",
    )

    return ActionResult(
        text=(
            _('✓ MANUAL-RFQ #%(p0)s разослан.\n⏱ Дедлайн сбора КП: %(p1)s (48 часов).\nПосле сбора — op_compose_kp чтобы сформировать инвойс клиенту.\n\n') % {"p0": f'{rfq.id}', "p1": f"{deadline.strftime('%d.%m %H:%M')}"}
            + (res.text or "")
        ),
        actions=[
            {"action": "op_compose_kp", "label": _("📋 Сформировать КП клиенту"),
             "params": {"rfq_id": rfq.id}},
            {"action": "view_rfq_quotes", "label": _("📊 Полученные КП"),
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
    if not ((role or "").startswith("operator") or role == "admin"):
        return ActionResult(text=_("Только оператор формирует MANUAL-КП."))
    try:
        rfq = RFQ.objects.get(id=int(params.get("rfq_id") or 0))
    except (RFQ.DoesNotExist, ValueError, TypeError):
        return ActionResult(text=_("RFQ не найден."))

    if rfq.status == "cancelled":
        return ActionResult(text=_("RFQ отменён — формировать КП нельзя."))

    qs = Quote.objects.filter(rfq=rfq, direction="seller_to_buyer", status="submitted")
    if params.get("quote_id"):
        try:
            chosen = qs.get(id=int(params["quote_id"]))
        except (Quote.DoesNotExist, ValueError, TypeError):
            return ActionResult(text=_("Выбранная котировка не найдена."))
    else:
        chosen = qs.order_by("total_amount").first()
        if not chosen:
            # Котировок поставщиков ещё нет — формируем КП прямо из каталога
            # (тот же механизм, что в AUTO). Так «Подтвердить КП» реально
            # работает, а не упирается в тупик «подтвердите аналоги вручную».
            try:
                from django.contrib.auth import get_user_model

                from marketplace.models import Part

                from .negotiation import auto_generate_quotes_from_catalog
                _oems = [it.query for it in rfq.items.all() if it.query]
                _sids = set(Part.objects.filter(
                    is_active=True, price__gt=0, oem_number__in=_oems,
                ).values_list("seller_id", flat=True))
                _recipients = list(get_user_model().objects.filter(id__in=_sids))
                if _recipients:
                    auto_generate_quotes_from_catalog(rfq, _recipients)
                    chosen = qs.order_by("total_amount").first()
            except Exception:
                logger.exception("op_compose_kp: catalog auto-quote failed RFQ #%s", rfq.id)
    if not chosen:
        return ActionResult(
            text=(
                _('По RFQ #%(p0)s не получилось собрать КП автоматически: ни один поставщик не покрывает все позиции одним предложением (или нет актуальных цен в каталоге). Нужны котировки поставщиков или разбор по позициям.') % {"p0": f'{rfq.id}'}
            ),
            actions=[
                {"label": _("Открыть RFQ"), "action": "rfq_detail",
                 "params": {"rfq_id": rfq.id}},
                {"label": _("Спросить у клиента"), "action": "ask_about_rfq",
                 "params": {"rfq_id": rfq.id}},
            ],
        )

    if _quote_financials(chosen) is None:
        return ActionResult(
            text=_("Выбранная котировка содержит некорректные позиции или сумму.")
        )

    from django.db import transaction

    with transaction.atomic():
        rfq = RFQ.objects.select_for_update().get(pk=rfq.pk)
        chosen = Quote.objects.select_for_update(of=("self",)).get(pk=chosen.pk)
        if (
            rfq.status == "cancelled"
            or chosen.rfq_id != rfq.id
            or chosen.status != "submitted"
            or _quote_financials(chosen) is None
        ):
            return ActionResult(
                text=_("RFQ или выбранная котировка больше не доступны.")
            )
        line = (
            f" | KP_COMPOSED: quote #{chosen.id} by op {user.username} at "
            f"{timezone.now().strftime('%Y-%m-%d %H:%M')}"
        )
        rfq.notes = (rfq.notes or "")[:4500] + line
        rfq.save(update_fields=["notes"])

    _notify(
        rfq.created_by, kind="rfq",
        title=_('📋 КП по RFQ #%(p0)s сформировано') % {"p0": f'{rfq.id}'},
        body=_('Оператор сформировал инвойс на $%(p0)s. Откройте, чтобы подтвердить и зарезервировать 10%%.') % {"p0": f'{chosen.total_amount:,.0f}'},
        url=f"/chat/rfq/{rfq.id}/?source=kp-ready",
    )

    return ActionResult(
        text=(
            _('✓ КП #%(p0)s ($%(p1)s) отправлено клиенту по RFQ #%(p2)s.') % {"p0": f'{chosen.id}', "p1": f'{chosen.total_amount:,.0f}', "p2": f'{rfq.id}'}
        ),
    )

"""ТЗ §5.2: загрузка конкурентного предложения buyer'ом → manual discount.

Flow:
  1. buyer.upload_competitor_offer(quote_id, competitor, price, note)
     → CompetitorOffer(status='uploaded')
  2. seller получает Notification → respond_to_competitor_offer
     → counter-quote с новой ценой → CompetitorOffer.status='matched' или
       'declined' (если уже ниже)
  3. Buyer видит обновлённый Quote с новой ценой

Actions:
  upload_competitor_offer  — buyer-only
  view_competitor_offer    — buyer/seller/operator
  respond_to_competitor_offer — seller-only (creates counter-quote)
"""
from __future__ import annotations

import logging
from decimal import Decimal

from django.utils import timezone
from django.utils.translation import gettext as _

from .actions import ActionResult, _notify, register
from .security import confirmation_is_true

logger = logging.getLogger(__name__)
MAX_COMPETITOR_PRICE = Decimal("999999999999.99")
MAX_COMPETITOR_DELIVERY_DAYS = 365
MAX_COMPETITOR_NOTE_LENGTH = 2000


def _finite_decimal(value, *, minimum=None, maximum=None):
    try:
        parsed = Decimal(str(value))
    except Exception:
        return None
    if not parsed.is_finite():
        return None
    if minimum is not None and parsed < minimum:
        return None
    if maximum is not None and parsed > maximum:
        return None
    return parsed


def _seller_principal(user):
    from .seller_actions import _effective_seller

    return _effective_seller(user)


@register("upload_competitor_offer")
def upload_competitor_offer(params, user, role):
    """Buyer загружает конкурентное предложение для триггера переторжки."""
    from marketplace.models import CompetitorOffer, Quote

    if role not in ("buyer", "seller"):
        return ActionResult(text=_("Загрузить предложение может только покупатель."))
    confirmed = confirmation_is_true(params.get("confirmed"))
    try:
        quote = Quote.objects.select_related("rfq", "seller").get(
            id=int(params.get("quote_id") or 0),
        )
    except (Quote.DoesNotExist, ValueError, TypeError):
        return ActionResult(text=_("Котировка не найдена."))

    if (
        quote.rfq.created_by_id != user.id
        or quote.direction != "seller_to_buyer"
        or quote.status not in ("submitted", "finalized")
        or quote.rfq.status == "cancelled"
    ):
        return ActionResult(text=_("Загрузить конкурентное предложение может только автор заявки."))

    competitor_name = (params.get("competitor_name") or "").strip()
    quoted_price_raw = params.get("quoted_price") or ""
    note = (params.get("note") or "").strip()

    if not confirmed or not competitor_name or not quoted_price_raw:
        return ActionResult(
            text=_("📄 Загрузить конкурентное предложение по котировке #%(id)s") % {"id": quote.id},
            cards=[{"type": "form", "data": {
                "title": _("📄 Конкурентное предложение"),
                "submit_action": "upload_competitor_offer",
                "fields": [
                    {"name": "competitor_name", "label": _("Название поставщика-конкурента"),
                     "required": True},
                    {"name": "quoted_price", "label": _("Их цена за весь заказ ($)"),
                     "type": "number", "required": True},
                    {"name": "delivery_days", "label": _("Срок поставки (дн)"),
                     "type": "number", "value": "14"},
                    {"name": "note", "label": _("Комментарий"), "type": "textarea"},
                ],
                "fixed_params": {"quote_id": quote.id, "confirmed": True},
            }}],
        )

    quoted_price = _finite_decimal(
        quoted_price_raw,
        minimum=Decimal("0.01"),
        maximum=MAX_COMPETITOR_PRICE,
    )
    if quoted_price is None:
        return ActionResult(text=_("⚠️ Цена должна быть числом."))
    try:
        delivery_days = int(params.get("delivery_days") or 14)
    except (TypeError, ValueError, OverflowError):
        return ActionResult(text=_("⚠️ Срок поставки указан неверно."))
    if not 1 <= delivery_days <= MAX_COMPETITOR_DELIVERY_DAYS:
        return ActionResult(text=_("⚠️ Срок поставки должен быть от 1 до 365 дней."))
    if len(note) > MAX_COMPETITOR_NOTE_LENGTH:
        return ActionResult(text=_("⚠️ Комментарий слишком длинный."))

    offer = CompetitorOffer.objects.create(
        rfq=quote.rfq, quote=quote, uploaded_by=user,
        competitor_name=competitor_name[:200],
        quoted_price=quoted_price,
        delivery_days=delivery_days,
        note=note,
        status="uploaded",
    )

    # Уведомить seller'а
    if quote.seller:
        gap = quote.total_amount - quoted_price
        gap_pct = (gap / quote.total_amount * 100).quantize(Decimal("0.1")) if quote.total_amount else 0
        _their = f"{quoted_price:,.0f}"
        _yours = f"{quote.total_amount:,.0f}"
        _notify(quote.seller, kind="rfq",
                title=_("📄 Конкурентное предложение по RFQ #%(rfq)s") % {"rfq": quote.rfq_id},
                body=(
                    _("%(comp)s: $%(their)s (вы: $%(yours)s, разница %(gap)s%%). Можете снизить?")
                    % {"comp": competitor_name, "their": _their, "yours": _yours, "gap": gap_pct}
                ),
                url=f"/chat/?rfq={quote.rfq_id}")

    _their = f"{quoted_price:,.0f}"
    return ActionResult(
        text=(
            _("✓ Конкурентное предложение #%(id)s загружено.\n"
              "%(comp)s: $%(their)s · %(days)s дн.\n"
              "Поставщик уведомлён — посмотрит и сможет ответить.")
            % {"id": offer.id, "comp": competitor_name, "their": _their, "days": delivery_days}
        ),
        contextual_actions=[
            {"action": "view_rfq_quotes", "label": _("📊 Все котировки"),
             "params": {"rfq_id": quote.rfq_id}},
        ],
    )


@register("respond_to_competitor_offer")
def respond_to_competitor_offer(params, user, role):
    """Seller отвечает на competitor offer — даёт ручную скидку."""
    from marketplace.models import CompetitorOffer, Quote, QuoteItem

    if role != "seller":
        return ActionResult(text=_("Отвечать может только продавец."))
    seller_user = _seller_principal(user)
    confirmed = confirmation_is_true(params.get("confirmed"))
    try:
        offer = CompetitorOffer.objects.select_related("quote", "rfq").get(
            id=int(params.get("offer_id") or 0),
        )
    except (CompetitorOffer.DoesNotExist, ValueError, TypeError):
        return ActionResult(text=_("Конкурентное предложение не найдено."))

    if offer.quote.seller_id != seller_user.id:
        return ActionResult(text=_("Отвечать может только продавец, чью котировку оспаривают."))
    if offer.status != "uploaded" or offer.rfq.status == "cancelled":
        return ActionResult(text=_("Уже обработано (статус: %(st)s).") % {"st": offer.get_status_display()})

    discount_pct_raw = params.get("discount_pct") or ""
    seller_comment = (params.get("seller_comment") or "").strip()
    decline_raw = params.get("decline")
    decline = (
        decline_raw is True
        or decline_raw == 1
        or str(decline_raw).strip().lower() in {"1", "true", "yes", "да"}
    )
    if len(seller_comment) > MAX_COMPETITOR_NOTE_LENGTH:
        return ActionResult(text=_("Комментарий слишком длинный."))

    if not confirmed:
        gap = offer.quote.total_amount - offer.quoted_price
        gap_pct = (gap / offer.quote.total_amount * 100).quantize(Decimal("0.1")) if offer.quote.total_amount else 0
        _their = f"{offer.quoted_price:,.0f}"
        _yours = f"{offer.quote.total_amount:,.0f}"
        _comment = (_("Комментарий покупателя: ") + offer.note) if offer.note else ""
        return ActionResult(
            text=(
                _("📄 Конкурентное предложение по RFQ #%(rfq)s\n"
                  "Конкурент: %(comp)s — $%(their)s\n"
                  "Ваша цена: $%(yours)s (разница %(gap)s%%)\n"
                  "%(comment)s")
                % {"rfq": offer.rfq_id, "comp": offer.competitor_name, "their": _their,
                   "yours": _yours, "gap": gap_pct, "comment": _comment}
            ),
            cards=[{"type": "form", "data": {
                "title": _("💬 Ответ на конкурентное предложение #%(id)s") % {"id": offer.id},
                "submit_action": "respond_to_competitor_offer",
                "fields": [
                    {"name": "discount_pct", "label": _("Ваша скидка (%)"),
                     "type": "number", "value": "0"},
                    {"name": "decline", "label": _("Отказаться (наша цена остаётся)"),
                     "type": "select",
                     "options": [
                         {"value": "0", "label": _("Дать скидку")},
                         {"value": "1", "label": _("Отказаться")},
                     ],
                     "value": "0"},
                    {"name": "seller_comment", "label": _("Комментарий"), "type": "textarea"},
                ],
                "fixed_params": {"offer_id": offer.id, "confirmed": True},
            }}],
        )

    if decline:
        from django.db import transaction

        with transaction.atomic():
            locked_offer = (
                CompetitorOffer.objects.select_for_update(of=("self",))
                .select_related("quote", "rfq")
                .get(pk=offer.pk)
            )
            if (
                locked_offer.status != "uploaded"
                or locked_offer.quote.seller_id != seller_user.id
                or locked_offer.rfq.status == "cancelled"
            ):
                return ActionResult(text=_("Предложение уже обработано или недоступно."))
            locked_offer.status = "declined"
            locked_offer.seller_comment = seller_comment
            locked_offer.reviewed_at = timezone.now()
            locked_offer.save(
                update_fields=["status", "seller_comment", "reviewed_at"],
            )
            offer = locked_offer
        if offer.uploaded_by:
            _notify(offer.uploaded_by, kind="rfq",
                    title=_("Поставщик отклонил ваше конкурентное предложение"),
                    body=_("Комментарий: %(c)s") % {"c": seller_comment[:200]},
                    url=f"/chat/?rfq={offer.rfq_id}")
        return ActionResult(
            text=_("✓ Competitor #%(id)s отклонён.") % {"id": offer.id},
        )

    pct = _finite_decimal(
        discount_pct_raw,
        minimum=Decimal("0.01"),
        maximum=Decimal("99.99"),
    )
    if pct is None:
        return ActionResult(text=_("⚠️ Укажите положительный % скидки или нажмите 'Отказаться'."))

    from django.db import transaction

    with transaction.atomic():
        offer = (
            CompetitorOffer.objects.select_for_update(of=("self",))
            .select_related("quote", "rfq")
            .get(pk=offer.pk)
        )
        if (
            offer.status != "uploaded"
            or offer.quote.seller_id != seller_user.id
            or offer.rfq.status == "cancelled"
        ):
            return ActionResult(text=_("Предложение уже обработано или недоступно."))
        quote = Quote.objects.select_for_update(of=("self",)).get(pk=offer.quote_id)
        quote_total = _finite_decimal(
            quote.total_amount,
            minimum=Decimal("0.01"),
            maximum=MAX_COMPETITOR_PRICE,
        )
        if quote_total is None or quote.status not in ("submitted", "finalized"):
            return ActionResult(text=_("Исходная котировка уже недоступна для скидки."))
        multiplier = (Decimal("100") - pct) / Decimal("100")
        new_total = (quote_total * multiplier).quantize(Decimal("0.01"))
        if new_total <= 0:
            return ActionResult(text=_("После скидки цена должна оставаться положительной."))

        source_items = list(quote.items.all())
        new_items = []
        for qi in source_items:
            new_unit = (qi.unit_price * multiplier).quantize(Decimal("0.01"))
            if not new_unit.is_finite() or new_unit <= 0:
                return ActionResult(
                    text=_("После скидки цена каждой позиции должна оставаться положительной.")
                )
            new_items.append((qi, new_unit))

        new_quote = Quote.objects.create(
            rfq=offer.rfq, seller=seller_user,
            direction="seller_to_buyer",
            parent_quote=quote,
            round_number=quote.round_number + 1,
            status="submitted",
            delivery_days=quote.delivery_days,
            total_amount=new_total,
            message=(
                _("Скидка %(pct)s%% в ответ на конкурентное предложение от %(comp)s. %(comment)s")
                % {"pct": pct, "comp": offer.competitor_name, "comment": seller_comment}
            )[:500],
        )
        QuoteItem.objects.bulk_create([
            QuoteItem(
                quote=new_quote, rfq_item=qi.rfq_item, part=qi.part,
                title_snapshot=qi.title_snapshot, quantity=qi.quantity,
                unit_price=new_unit,
            )
            for qi, new_unit in new_items
        ])

        offer.status = "matched"
        offer.seller_response_pct = pct
        offer.seller_comment = seller_comment
        offer.reviewed_at = timezone.now()
        offer.save(update_fields=["status", "seller_response_pct", "seller_comment",
                                    "reviewed_at"])

        quote.status = "countered"
        quote.save(update_fields=["status"])

    if offer.uploaded_by:
        _new = f"{new_total:,.0f}"
        _was = f"{offer.quote.total_amount:,.0f}"
        _notify(offer.uploaded_by, kind="rfq",
                title=_("Скидка %(pct)s%% в ответ на конкурентное предложение") % {"pct": pct},
                body=_("Новая цена: $%(new)s (было $%(was)s)") % {"new": _new, "was": _was},
                url=f"/chat/?rfq={offer.rfq_id}")

    _new = f"{new_total:,.0f}"
    return ActionResult(
        text=(
            _("✓ Скидка %(pct)s%% применена. Новая цена $%(new)s\n"
              "Создана новая котировка #%(qid)s (раунд %(rnd)s).")
            % {"pct": pct, "new": _new, "qid": new_quote.id, "rnd": new_quote.round_number}
        ),
        contextual_actions=[
            {"action": "view_quote", "label": _("Открыть новую котировку"),
             "params": {"quote_id": new_quote.id}},
        ],
    )

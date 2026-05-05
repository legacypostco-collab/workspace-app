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

from .actions import ActionResult, _notify, register

logger = logging.getLogger(__name__)


@register("upload_competitor_offer")
def upload_competitor_offer(params, user, role):
    """Buyer загружает конкурентное предложение для триггера переторжки."""
    from marketplace.models import Quote, CompetitorOffer

    confirmed = bool(params.get("confirmed"))
    try:
        quote = Quote.objects.select_related("rfq", "seller").get(
            id=int(params.get("quote_id") or 0),
        )
    except (Quote.DoesNotExist, ValueError, TypeError):
        return ActionResult(text="Котировка не найдена.")

    if quote.rfq.created_by_id != user.id:
        return ActionResult(text="Загрузить competitor-оффер может только заказчик RFQ.")

    competitor_name = (params.get("competitor_name") or "").strip()
    quoted_price_raw = params.get("quoted_price") or ""
    delivery_days = int(params.get("delivery_days") or 14)
    note = (params.get("note") or "").strip()

    if not confirmed or not competitor_name or not quoted_price_raw:
        return ActionResult(
            text=f"📄 Загрузить конкурентное предложение по котировке #{quote.id}",
            cards=[{"type": "form", "data": {
                "title": f"📄 Конкурентное предложение",
                "submit_action": "upload_competitor_offer",
                "fields": [
                    {"name": "competitor_name", "label": "Название поставщика-конкурента",
                     "required": True},
                    {"name": "quoted_price", "label": "Их цена за весь заказ ($)",
                     "type": "number", "required": True},
                    {"name": "delivery_days", "label": "Срок поставки (дн)",
                     "type": "number", "value": "14"},
                    {"name": "note", "label": "Комментарий", "type": "textarea"},
                ],
                "fixed_params": {"quote_id": quote.id, "confirmed": True},
            }}],
        )

    try:
        quoted_price = Decimal(str(quoted_price_raw))
    except Exception:
        return ActionResult(text="⚠️ Цена должна быть числом.")

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
        _notify(quote.seller, kind="rfq",
                title=f"📄 Конкурентное предложение по RFQ #{quote.rfq_id}",
                body=(
                    f"{competitor_name}: ${quoted_price:,.0f} (вы: ${quote.total_amount:,.0f}, "
                    f"разница {gap_pct}%). Можете снизить?"
                ),
                url=f"/chat/?rfq={quote.rfq_id}")

    return ActionResult(
        text=(
            f"✓ Конкурентное предложение #{offer.id} загружено.\n"
            f"{competitor_name}: ${quoted_price:,.0f} · {delivery_days} дн.\n"
            f"Поставщик уведомлён — посмотрит и сможет ответить."
        ),
        contextual_actions=[
            {"action": "view_rfq_quotes", "label": "📊 Все котировки",
             "params": {"rfq_id": quote.rfq_id}},
        ],
    )


@register("respond_to_competitor_offer")
def respond_to_competitor_offer(params, user, role):
    """Seller отвечает на competitor offer — даёт ручную скидку."""
    from marketplace.models import CompetitorOffer, Quote, QuoteItem

    confirmed = bool(params.get("confirmed"))
    try:
        offer = CompetitorOffer.objects.select_related("quote", "rfq").get(
            id=int(params.get("offer_id") or 0),
        )
    except (CompetitorOffer.DoesNotExist, ValueError, TypeError):
        return ActionResult(text="Конкурентное предложение не найдено.")

    if offer.quote.seller_id != user.id:
        return ActionResult(text="Отвечать может только продавец, чью котировку оспаривают.")
    if offer.status != "uploaded":
        return ActionResult(text=f"Уже обработано (статус: {offer.get_status_display()}).")

    discount_pct_raw = params.get("discount_pct") or ""
    seller_comment = (params.get("seller_comment") or "").strip()
    decline = bool(params.get("decline"))

    if not confirmed:
        gap = offer.quote.total_amount - offer.quoted_price
        gap_pct = (gap / offer.quote.total_amount * 100).quantize(Decimal("0.1")) if offer.quote.total_amount else 0
        return ActionResult(
            text=(
                f"📄 Конкурентное предложение по RFQ #{offer.rfq_id}\n"
                f"Конкурент: {offer.competitor_name} — ${offer.quoted_price:,.0f}\n"
                f"Ваша цена: ${offer.quote.total_amount:,.0f} (разница {gap_pct}%)\n"
                f"{('Комментарий buyer: ' + offer.note) if offer.note else ''}"
            ),
            cards=[{"type": "form", "data": {
                "title": f"💬 Ответ на competitor #{offer.id}",
                "submit_action": "respond_to_competitor_offer",
                "fields": [
                    {"name": "discount_pct", "label": "Ваша скидка (%)",
                     "type": "number", "value": "0"},
                    {"name": "decline", "label": "Отказаться (наша цена остаётся)",
                     "type": "select",
                     "options": [
                         {"value": "0", "label": "Дать скидку"},
                         {"value": "1", "label": "Отказаться"},
                     ],
                     "value": "0"},
                    {"name": "seller_comment", "label": "Комментарий", "type": "textarea"},
                ],
                "fixed_params": {"offer_id": offer.id, "confirmed": True},
            }}],
        )

    if decline or str(decline) == "1":
        offer.status = "declined"
        offer.seller_comment = seller_comment
        offer.reviewed_at = timezone.now()
        offer.save(update_fields=["status", "seller_comment", "reviewed_at"])
        if offer.uploaded_by:
            _notify(offer.uploaded_by, kind="rfq",
                    title=f"Поставщик отклонил вашу competitor-оффер",
                    body=f"Комментарий: {seller_comment[:200]}",
                    url=f"/chat/?rfq={offer.rfq_id}")
        return ActionResult(
            text=f"✓ Competitor #{offer.id} отклонён.",
        )

    try:
        pct = Decimal(str(discount_pct_raw))
    except Exception:
        pct = Decimal("0")

    if pct <= 0:
        return ActionResult(text="⚠️ Укажите положительный % скидки или нажмите 'Отказаться'.")

    # Создаём новую counter-quote с обновлёнными ценами
    new_total = (offer.quote.total_amount * (Decimal("100") - pct) / Decimal("100")).quantize(Decimal("0.01"))
    new_quote = Quote.objects.create(
        rfq=offer.rfq, seller=user,
        direction="seller_to_buyer",
        parent_quote=offer.quote,
        round_number=offer.quote.round_number + 1,
        status="submitted",
        delivery_days=offer.quote.delivery_days,
        total_amount=new_total,
        message=(
            f"Скидка {pct}% в ответ на конкурентное предложение от "
            f"{offer.competitor_name}. {seller_comment}"
        )[:500],
    )
    # Копируем items с обновлёнными ценами
    for qi in offer.quote.items.all():
        new_unit = (qi.unit_price * (Decimal("100") - pct) / Decimal("100")).quantize(Decimal("0.01"))
        QuoteItem.objects.create(
            quote=new_quote, rfq_item=qi.rfq_item, part=qi.part,
            title_snapshot=qi.title_snapshot, quantity=qi.quantity,
            unit_price=new_unit,
        )

    offer.status = "matched"
    offer.seller_response_pct = pct
    offer.seller_comment = seller_comment
    offer.reviewed_at = timezone.now()
    offer.save(update_fields=["status", "seller_response_pct", "seller_comment",
                                "reviewed_at"])

    # Старый quote → countered (буде обновлён)
    offer.quote.status = "countered"
    offer.quote.save(update_fields=["status"])

    if offer.uploaded_by:
        _notify(offer.uploaded_by, kind="rfq",
                title=f"Скидка {pct}% по competitor-оффер",
                body=f"Новая цена: ${new_total:,.0f} (было ${offer.quote.total_amount:,.0f})",
                url=f"/chat/?rfq={offer.rfq_id}")

    return ActionResult(
        text=(
            f"✓ Скидка {pct}% применена. Новая цена ${new_total:,.0f}\n"
            f"Создана новая котировка #{new_quote.id} (раунд {new_quote.round_number})."
        ),
        contextual_actions=[
            {"action": "view_quote", "label": "Открыть новую котировку",
             "params": {"quote_id": new_quote.id}},
        ],
    )

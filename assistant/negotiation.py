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
from django.utils.translation import gettext as _

from .actions import (
    ActionResult, _anon_register_result, _is_anon, _log_event, _notify, register,
)
from .realtime import push_rfq_update as _push_rfq_update

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


def auto_generate_quotes_from_catalog(rfq, recipients) -> int:
    """ТЗ §4.1 AUTO mode: для каждого trusted-продавца, у которого в каталоге
    есть Part с матчем по oem_number из RFQItem, мгновенно создаём Quote
    с его прайсом — без ожидания ручного submit_quote.

    Возвращает кол-во созданных Quote'ов (по 1 на продавца).
    """
    from marketplace.models import Part, Quote, QuoteItem

    rfq_items = list(rfq.items.all())
    oems = [it.query for it in rfq_items if it.query]
    if not oems:
        return 0

    # Все актуальные Part'ы recipients по этим OEM
    seller_ids = [s.id for s in recipients]
    catalog = (
        Part.objects.filter(seller_id__in=seller_ids,
                            is_active=True, price__gt=0,
                            oem_number__in=oems)
        .select_related("brand")
    )
    # Map: seller_id → {oem: Part}
    by_seller: dict[int, dict[str, Part]] = {}
    for p in catalog:
        by_seller.setdefault(p.seller_id, {})[p.oem_number] = p

    created = 0
    for seller in recipients:
        seller_catalog = by_seller.get(seller.id) or {}
        if not seller_catalog:
            continue
        # Для AUTO нужен полный матч: продавец покрывает ВСЕ позиции RFQ
        if not all(it.query in seller_catalog for it in rfq_items):
            continue
        # Уже есть quote от этого продавца? — пропускаем
        if Quote.objects.filter(rfq=rfq, seller=seller,
                                direction="seller_to_buyer").exists():
            continue

        items_data = []
        total = Decimal("0")
        for it in rfq_items:
            p = seller_catalog[it.query]
            qty = int(it.quantity or 1)
            unit = Decimal(p.price)
            items_data.append((it, p, qty, unit))
            total += unit * qty

        # Самый медленный prep+ship определяет общий срок
        delivery_days = max(
            (int(p.production_lead_days or 0) + int(p.prep_to_ship_days or 0)
             + int(p.shipping_lead_days or 0))
            for _u1, p, _u2, _u3 in items_data
        ) or 14

        try:
            quote = Quote.objects.create(
                rfq=rfq, seller=seller,
                direction="seller_to_buyer",
                round_number=_next_round(rfq, seller.id),
                status="submitted",
                delivery_days=delivery_days,
                total_amount=total.quantize(Decimal("0.01")),
                message=_("Автоматическая котировка из каталога (AUTO mode)"),
                valid_until=timezone.now() + timedelta(days=7),
            )
            for it, p, qty, unit in items_data:
                QuoteItem.objects.create(
                    quote=quote, rfq_item=it, part=p,
                    title_snapshot=p.title[:255],
                    quantity=qty, unit_price=unit,
                )
            created += 1
            logger.info(
                f"AUTO quote: RFQ #{rfq.id} ← seller {seller.username} · "
                f"${total} · {delivery_days}d"
            )
        except Exception:
            logger.exception(f"auto-quote create failed for seller {seller.id}")

    return created


# ══════════════════════════════════════════════════════════
# 0. send_rfq_to_suppliers — buyer рассылает RFQ продавцам
# ══════════════════════════════════════════════════════════

@register("send_rfq_to_suppliers")
def send_rfq_to_suppliers(params, user, role):
    """Buyer разослает RFQ кандидатам-поставщикам с priority routing (ТЗ §7.1).

    Кандидаты сортируются по supplier_status:
      1) trusted    — отправка всегда
      2) sandbox    — отправка если trusted < min_trusted (по умолчанию 3)
      3) risky      — только если params.include_risky=True (фиксируется в журнал)
      4) rejected   — НИКОГДА (ТЗ §3 «Исключён»)

    KYB не verified фильтруются для AUTO-рассылки, но включаются для SEMI/MANUAL
    в как fallback (если ничего лучше нет).
    """
    from django.contrib.auth import get_user_model

    from marketplace.models import RFQ, CompanyVerification, UserProfile

    confirmed = bool(params.get("confirmed"))
    try:
        rfq = RFQ.objects.get(id=int(params.get("rfq_id") or 0))
    except (RFQ.DoesNotExist, ValueError, TypeError):
        return ActionResult(text=_("RFQ не найден."))

    # Авторы RFQ + операторы/админы могут рассылать
    is_operator = bool(role and (role.startswith("operator") or role == "admin"))
    if rfq.created_by_id != user.id and not is_operator:
        return ActionResult(text=_("Разослать RFQ может только его автор или оператор."))
    if rfq.status == "cancelled":
        return ActionResult(text=_("RFQ #%(id)s отменён — нельзя рассылать.") % {"id": rfq.id})

    User = get_user_model()
    min_trusted = int(params.get("min_trusted") or 3)
    include_risky = bool(params.get("include_risky"))

    # Все активные продавцы (исключая sentinel-эскроу)
    all_sellers = list(
        User.objects.filter(profile__role="seller", is_active=True)
        .exclude(username="__platform_escrow__")[:50]
    )
    if not all_sellers:
        return ActionResult(text=_("⚠️ В системе пока нет активных поставщиков."))

    # Группировка по supplier_status (свежее чтение)
    status_map = {}
    for p in UserProfile.objects.filter(user__in=all_sellers).only("user_id", "supplier_status"):
        status_map[p.user_id] = p.supplier_status

    trusted = [s for s in all_sellers if status_map.get(s.id) == "trusted"]
    sandbox = [s for s in all_sellers if status_map.get(s.id) == "sandbox"]
    risky = [s for s in all_sellers if status_map.get(s.id) == "risky"]
    # rejected — никогда (ТЗ §3)

    # Priority routing: trusted всегда + sandbox если мало trusted + risky по флагу
    recipients = list(trusted)
    if len(recipients) < min_trusted:
        needed = min_trusted - len(recipients)
        recipients.extend(sandbox[:max(needed, 1)])
    elif sandbox:
        # Если trusted ≥ min — всё равно даём sandbox шанс показать цены (но не как
        # primary). Бизнес-правило ТЗ §3.1: «sandbox может быть показан как
        # альтернативный вариант».
        recipients.extend(sandbox[:max(0, min_trusted)])
    if include_risky:
        recipients.extend(risky)

    # Дедуп
    seen = set()
    recipients = [r for r in recipients if r.id not in seen and not seen.add(r.id)]

    n_trusted_in = sum(1 for r in recipients if status_map.get(r.id) == "trusted")
    n_sandbox_in = sum(1 for r in recipients if status_map.get(r.id) == "sandbox")
    n_risky_in = sum(1 for r in recipients if status_map.get(r.id) == "risky")

    if not recipients:
        return ActionResult(text=(
            _("⚠️ Нет поставщиков для рассылки. "
              "Trusted=0, sandbox=0, risky=%(risky)s.")
            % {"risky": (_("включены") if include_risky else _("выключены"))}
        ))

    verified_seller_ids = set(
        CompanyVerification.objects.filter(status="verified")
        .values_list("user_id", flat=True)
    )
    n_verified = sum(1 for s in recipients if s.id in verified_seller_ids)

    # Шаг 1: preview
    if not confirmed:
        items_count = rfq.items.count()
        warnings = [
            _("Каждый получит уведомление (in-app + email/telegram если включены)."),
            _("Котировки будут приходить — следите за вкладкой RFQ."),
        ]
        if n_risky_in > 0:
            warnings.insert(0,
                _("⚠️ В рассылке %(n)s «рисковых» поставщиков (ТЗ §3.1) — "
                  "включены явно по решению оператора. Решение зафиксируется в журнале.")
                % {"n": n_risky_in}
            )
        return ActionResult(
            text=_("📨 Разослать RFQ #%(id)s поставщикам?") % {"id": rfq.id},
            cards=[{"type": "draft", "data": {
                "title": _("📨 Рассылка RFQ #%(id)s") % {"id": rfq.id},
                "rows": [
                    {"label": _("Позиций"), "value": str(items_count), "primary": True},
                    {"label": _("Trusted (надёжных)"), "value": str(n_trusted_in), "primary": True},
                    {"label": _("Sandbox (песочница)"), "value": str(n_sandbox_in)},
                    {"label": _("Risky (рисковых)"), "value": str(n_risky_in)},
                    {"label": _("KYB verified"), "value": _("%(n)s из %(total)s") % {"n": n_verified, "total": len(recipients)}},
                    {"label": _("Всего получателей"), "value": _("%(n)s поставщиков") % {"n": len(recipients)}},
                ],
                "warnings": warnings,
                "confirm_action": "send_rfq_to_suppliers",
                "confirm_label": _("📨 Разослать %(n)s поставщикам") % {"n": len(recipients)},
                "confirm_params": {
                    "rfq_id": rfq.id, "confirmed": True,
                    "include_risky": include_risky, "min_trusted": min_trusted,
                },
                "cancel_label": _("Отмена"),
            }}],
        )

    # Шаг 2: рассылка + аудит
    sent = 0
    for seller in recipients:
        try:
            _notify(
                seller, kind="rfq",
                title=_("Новый RFQ #%(id)s от %(who)s") % {"id": rfq.id, "who": rfq.customer_name or rfq.created_by.username},
                body=_("%(n)s позиций · %(urg)s. Откройте чтобы ответить котировкой.") % {"n": rfq.items.count(), "urg": rfq.urgency or 'standard'},
                url=f"/chat/rfq/{rfq.id}/?source=invite",
            )
            sent += 1
        except Exception:
            logger.exception("send_rfq notify failed for seller %s", seller.id)

    # AUTO mode (§4.1): автогенерация Quote'ов из каталога для тех продавцов,
    # у кого все позиции есть в каталоге с актуальной ценой. Без ожидания
    # ручного submit_quote.
    auto_quotes = 0
    # ТЗ: AUTO — автогенерация + сразу buyer'у. SEMI — автогенерация
    # из каталога, но КП уходит только после оператор-approve.
    if rfq.mode in ("auto", "semi"):
        try:
            auto_quotes = auto_generate_quotes_from_catalog(rfq, recipients)
            if auto_quotes > 0:
                _notify(
                    rfq.created_by, kind="rfq",
                    title=_("🤖 %(n)s котировок по RFQ #%(id)s") % {"n": auto_quotes, "id": rfq.id},
                    body=_("AUTO mode: продавцы автоматически выставили КП из каталога."),
                    url=f"/chat/rfq/{rfq.id}/?source=auto-quote",
                )
        except Exception:
            logger.exception("auto_generate_quotes_from_catalog failed")

    # Аудит: если risky включены вручную — логируем (ТЗ §7.1.1)
    # Запись в RFQ.notes (OrderEvent требует order, для RFQ-event'а не подходит)
    if n_risky_in > 0:
        try:
            audit_line = (
                f" | RISKY-DISPATCH: {n_risky_in} рисковых получателей "
                f"включены вручную {user.username} {timezone.now().strftime('%Y-%m-%d %H:%M')}"
            )
            rfq.notes = (rfq.notes or "")[:4500] + audit_line
            rfq.save(update_fields=["notes"])
        except Exception:
            logger.exception("audit risky dispatch failed")

    # AUTO-mode: достаём cheapest для primary CTA
    best_quote = None
    reserve_amount = None
    if rfq.mode == "auto" and auto_quotes > 0:
        from marketplace.models import Quote as _Q
        best_quote = (_Q.objects.filter(
            rfq=rfq, direction="seller_to_buyer", status="submitted",
        ).order_by("total_amount").first())
        if best_quote:
            reserve_amount = (best_quote.total_amount * Decimal("0.10")).quantize(Decimal("0.01"))

    auto_q_line = (
        _("\n🤖 AUTO: %(n)s продавцов сразу выставили КП из каталога.") % {"n": auto_quotes}
        + (_("\n🎯 Лучшее: $%(amt)s · резерв $%(res)s")
           % {"amt": f"{best_quote.total_amount:,.0f}", "res": f"{reserve_amount:,.0f}"}
           if best_quote else "")
        if rfq.mode == "auto" and auto_quotes > 0 else ""
    )

    actions = []
    if best_quote:
        actions.append({
            "action": "auto_accept_and_pay_reserve",
            "label": _("✓ Принять лучшее и оплатить резерв $%(res)s") % {"res": f"{reserve_amount:,.0f}"},
            "params": {"rfq_id": rfq.id},
        })
    actions.append({
        "action": "view_rfq_quotes", "label": _("📊 Все котировки"),
        "params": {"rfq_id": rfq.id},
    })

    _push_rfq_update(rfq, event="rfq_sent")

    return ActionResult(
        text=(
            _("✓ RFQ #%(id)s разослан %(sent)s поставщикам · "
              "trusted %(tr)s · sandbox %(sb)s")
            % {"id": rfq.id, "sent": sent, "tr": n_trusted_in, "sb": n_sandbox_in}
            + (_(" · risky %(n)s") % {"n": n_risky_in} if n_risky_in else "")
            + _(".\nКотировки будут приходить — следите за уведомлениями.")
            + auto_q_line
        ),
        actions=actions,
        contextual_actions=[
            # «Назад к RFQ» → inline-карточка через get_rfq_status
            # (отдельная страница /chat/rfq/<id>/ упразднена).
            {"action": "get_rfq_status", "label": _("← Назад к RFQ"),
             "params": {"rfq_id": rfq.id}},
        ],
    )


# ══════════════════════════════════════════════════════════
# Разбор свободного запроса покупателя на OEM-артикул + название
# ══════════════════════════════════════════════════════════
def _split_query_oem_name(query):
    """Из строки запроса («Датчик давления 320D») вытащить OEM-токен
    («320D») и человекочитаемое название («Датчик давления»).

    Возвращает (oem, name). Если OEM-токен не найден — ("", query).
    Если строка состоит только из кода — (query, "")."""
    import re

    q = (query or "").strip()
    if not q:
        return "", ""
    tokens = q.split()
    # OEM-подобный токен: латиница/цифры/разделители, есть цифра, длина ≥ 4.
    oem_re = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-/.]{3,}$")
    oem_token = ""
    for tok in tokens:
        t = tok.strip(".,;:()[]")
        if oem_re.match(t) and any(c.isdigit() for c in t):
            oem_token = t  # берём последний подходящий (OEM обычно в конце)
    if oem_token:
        name = " ".join(
            tok for tok in tokens if tok.strip(".,;:()[]") != oem_token
        ).strip()
        return oem_token, name
    # Один токен и в нём есть цифра — это, скорее всего, чистый код без названия.
    if len(tokens) == 1 and any(c.isdigit() for c in q):
        return q, ""
    return "", q


def _seller_catalog_match(seller_parts_qs, rfq_item, oem=None, name=None):
    """Найти позицию из каталога ПРОДАВЦА, подходящую под строку RFQ.

    Нужно, чтобы продавец прямо в карточке котировки видел: есть ли товар у
    него, по какой цене и сколько на складе — и мог принять решение сразу.
    Приоритет: его же matched_part → точный OEM → вхождение названия.
    Возвращает Part или None.
    """
    mp = rfq_item.matched_part
    if mp is not None and getattr(mp, "seller_id", None) and \
            seller_parts_qs.filter(id=mp.id).exists():
        return mp
    if oem is None or name is None:
        oem, name = _split_query_oem_name(rfq_item.query)
    if oem:
        p = (seller_parts_qs.filter(oem_number__iexact=oem)
             .select_related("brand", "category").first())
        if p is not None:
            return p
    if name and len(name) >= 4:
        p = (seller_parts_qs.filter(title__icontains=name[:40])
             .select_related("brand", "category").first())
        if p is not None:
            return p
    return None


def _market_price_usd(oem):
    """Ориентир рыночной цены по OEM из общего каталога → (median, lo, hi) в USD
    (целые). Помогает продавцу не продешевить и не вылететь из конкурса.

    Точный матч по oem_number (использует индекс — быстро на 900К+ позиций).
    Кап 300 строк. Fail-soft: любая проблема → (None, None, None), форма не падает.
    """
    if not oem:
        return (None, None, None)
    try:
        from statistics import median
        from marketplace.models import Part
        from marketplace.fx import to_usd_float
        rows = list(Part.objects.filter(
            oem_number=oem, is_active=True, price__gt=0,
        ).values_list("price", "currency")[:300])
        usd = []
        for price, ccy in rows:
            v = to_usd_float(price, ccy or "USD")
            if v and v > 0:
                usd.append(v)
        if not usd:
            return (None, None, None)
        return (int(round(median(usd))), int(round(min(usd))), int(round(max(usd))))
    except Exception:
        return (None, None, None)


def _seller_analog(seller_parts_qs, oem):
    """Аналог ИЗ КАТАЛОГА ПРОДАВЦА для запрошенного OEM: его позиция, у которой
    этот OEM указан в кросс-номерах, но сам oem_number другой (т.е. это аналог,
    а не тот же оригинал). → Part | None. Fail-soft.

    Ищем только по каталогу самого продавца (seller_parts_qs уже отфильтрован по
    seller) — это и быстро (малый набор), и корректно: продавец может предложить
    лишь то, что у него реально есть.
    """
    if not oem or seller_parts_qs is None:
        return None
    try:
        return (seller_parts_qs
                .filter(cross_numbers__icontains=oem, is_active=True)
                .exclude(oem_number__iexact=oem)
                .select_related("brand").first())
    except Exception:
        return None


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
    from marketplace.models import RFQ, Quote, QuoteItem, RFQItem

    from .onboarding import kyb_required_for_seller

    try:
        rfq = RFQ.objects.get(id=int(params.get("rfq_id") or 0))
    except (RFQ.DoesNotExist, ValueError, TypeError):
        return ActionResult(text=_("RFQ не найден."))

    # KYB-gate (selling-action)
    if role == "seller" and kyb_required_for_seller(user):
        return ActionResult(
            text=_("🛡 Котировки доступны только верифицированным продавцам."),
            actions=[{"action": "start_onboarding", "label": _("🚀 Начать верификацию")}],
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

        # Собираем строки для табличного quote_form-renderer.
        # Каждая строка: artikul + title + brand + qty + suggested unit_price.
        # Сумма и live-итого считаются на фронте при редактировании.
        from .rfq_mode_badge import mode_badge, mode_hint_for_seller
        items_for_card = []
        suggested_total = Decimal("0")
        can_fulfill = 0
        has_analog = False  # есть ли у продавца аналог хоть на одну позицию

        # Каталог самого продавца — чтобы прямо в карточке подставить его цену,
        # остаток и отметку «есть/нет в каталоге» (решение принимается на месте).
        seller_parts_qs = None
        if role == "seller":
            from marketplace.models import Part
            seller_parts_qs = Part.objects.filter(seller=user)

        _COND_RU = {"oem": "OEM", "aftermarket": _("Аналог"), "used": _("Б/У"), "new": _("Новый")}
        for it in rfq_items:
            base = suggested.get(it.id, {})
            # Артикул/название: при матче — канонический OEM + title детали;
            # без матча — разбор свободного запроса на OEM-токен + название.
            _oem, _name = _split_query_oem_name(it.query)
            article = _oem
            part_title = _name
            brand_name = ""
            currency = "USD"
            condition_lbl = ""
            weight_kg = 0.0
            category_name = ""
            origin = ""
            in_catalog = False
            stock_qty = 0
            avail_label = ""

            # Источник данных позиции: приоритет — каталог самого продавца
            # (он отвечает СВОИМ товаром), затем общий matched_part.
            cat_part = None
            if seller_parts_qs is not None:
                cat_part = _seller_catalog_match(seller_parts_qs, it, _oem, _name)
                if cat_part is not None:
                    in_catalog = True
                    can_fulfill += 1
            src = cat_part or it.matched_part

            if src is not None:
                part_title = (src.title or "")[:80] or _name
                brand_name = src.brand.name if src.brand_id else ""
                currency = src.currency or "USD"
                condition_lbl = _COND_RU.get((src.condition or "").lower(), src.condition or "")
                weight_kg = float(src.gross_weight_kg or 0)
                category_name = src.category.name if src.category_id else ""
                origin = src.country_of_origin or ""
                article = src.oem_number or _oem
                if in_catalog:
                    stock_qty = int(src.stock_quantity or 0)
                    avail_label = src.get_availability_display() if hasattr(src, "get_availability_display") else ""

            # Цена-подсказка: params.items → каталог продавца → matched_part.
            default_price = base.get("unit_price")
            if default_price is None and in_catalog:
                default_price = float(src.price or 0)
            if default_price is None and it.matched_part:
                default_price = float(it.matched_part.price or 0)
            try:
                price_d = Decimal(str(default_price)) if default_price else Decimal("0")
            except Exception:
                price_d = Decimal("0")
            suggested_total += (price_d * Decimal(str(it.quantity or 0)))

            # ── Подсказки продавцу в момент котировки ──
            # A) рыночный ориентир по OEM, B) своя цена из каталога (для кнопки
            # автозаполнения), C) аналог из своего каталога (если оригинала нет).
            from marketplace.fx import to_usd_float as _to_usd
            mkt_med, mkt_lo, mkt_hi = _market_price_usd(article)
            catalog_usd = None
            if in_catalog and src is not None:
                catalog_usd = _to_usd(src.price, src.currency or "USD")
            analog = None
            if not in_catalog and seller_parts_qs is not None:
                _ap = _seller_analog(seller_parts_qs, article)
                if _ap is not None:
                    has_analog = True
                    analog = {
                        "article":   _ap.oem_number or "",
                        "title":     (_ap.title or "")[:60],
                        "price_usd": _to_usd(_ap.price, _ap.currency or "USD"),
                        "stock":     int(_ap.stock_quantity or 0),
                    }

            items_for_card.append({
                "rfq_item_id":  it.id,
                "article":      article,
                "title":        part_title,
                "brand":        brand_name,
                "quantity":     int(it.quantity or 0),
                "unit_price":   float(price_d),
                "currency":     currency,
                "condition":    condition_lbl,
                "weight":       weight_kg,
                "category":     category_name,
                "origin":       origin,
                "in_catalog":   in_catalog,
                "stock":        stock_qty,
                "availability": avail_label,
                # Новые подсказки (фронт: рынок под ценой / «Из каталога» / чип аналога)
                "market_usd":   mkt_med,
                "market_lo":    mkt_lo,
                "market_hi":    mkt_hi,
                "catalog_usd":  float(catalog_usd) if catalog_usd is not None else None,
                "analog":       analog,
            })

        # Релевантность: нет смысла показывать продавцу форму котировки по RFQ,
        # где у него НИ ОДНОЙ позиции — ни в каталоге, ни как аналог. Инбокс
        # лидов (_seller_deals) это уже фильтрует по OEM-матчу, но форму можно
        # открыть и в обход списка (старый чат / прямая ссылка / seed-данные) —
        # закрываем ту же дыру и здесь. Оператор/админ не ограничиваются.
        if role == "seller" and can_fulfill == 0 and not has_analog:
            return ActionResult(
                text=(_("🧩 RFQ #%(id)s — не по вашему каталогу. Ни одной из "
                        "%(n)s позиций нет у вас ни в наличии, ни как "
                        "аналог — котировать нечего.\nЗагрузите подходящие позиции "
                        "или смотрите релевантные лиды (там только запросы по "
                        "вашему каталогу).") % {"id": rfq.id, "n": len(rfq_items)}),
                actions=[
                    {"label": _("🔥 Срочные задачи"), "action": "seller_inbox", "params": {}},
                ],
                contextual_actions=[{"action": "go_home", "label": _("🏠 Главная")}],
            )

        # Бейдж надёжности самого продавца — в колонку «Поставщик», как
        # «Надёжный · 91.6» в карточке заказа (а не просто «Вы»).
        _BADGE_RU = {"trusted": _("Надёжный"), "sandbox": _("Проверка"),
                     "risky": _("Рисковый"), "rejected": _("Исключён")}
        seller_status = "trusted"
        seller_rating = 90.0
        try:
            from marketplace.models import UserProfile
            _prof = (UserProfile.objects.filter(user=user)
                     .only("supplier_status", "rating").first())
            if _prof:
                seller_status = _prof.supplier_status or "trusted"
                seller_rating = float(_prof.rating or 90.0)
        except Exception:
            pass
        seller_badge = f"{_BADGE_RU.get(seller_status, _('Надёжный'))} · {round(seller_rating, 1)}"

        badge = mode_badge(rfq.mode)
        hint  = mode_hint_for_seller(rfq.mode) if role == "seller" else ""
        urgency_tone = (
            "bad"  if rfq.urgency == "critical" else
            "warn" if rfq.urgency == "urgent"   else "info"
        )

        # Код покупателя: продавцу показываем присвоенный анонимный код (узнаёт
        # повторных клиентов), но НЕ имя. Стабилен на одного покупателя.
        import hashlib as _hl
        _seed = str(getattr(rfq, "created_by_id", None) or rfq.customer_email or rfq.id)
        buyer_code = "B-" + _hl.sha256(_seed.encode()).hexdigest()[:5].upper()
        # Способ доставки (тест: морем по умолчанию; реальный выбирается при
        # оформлении заказа) + место назначения.
        ship_mode = "sea"
        ship_mode_ru = {"sea": _("морем"), "air": _("авиа"), "auto": _("авто")}.get(ship_mode, "")
        dest_port = {"sea": _("морской порт"), "air": _("аэропорт"), "auto": _("терминал")}.get(ship_mode, _("порт"))

        # Скорость ответа продавца — РЕАЛЬНАЯ метрика (медиана + перцентиль) и
        # возраст RFQ. Честный посыл «отвечай, пока запрос активен». Только продавцу.
        seller_speed = None
        rfq_age = ""
        if role == "seller":
            try:
                from .seller_speed import rfq_age_label, seller_speed_standing
                seller_speed = seller_speed_standing(user)
                rfq_age = rfq_age_label(rfq)
            except Exception:
                seller_speed = None

        return ActionResult(
            text=(_("💬 Котировка по RFQ #%(id)s") % {"id": rfq.id}
                  + (_(" · ответ на counter (раунд %(r)s)") % {"r": _next_round(rfq, user.id)} if parent_quote_id else "")
                  + (f"\n{hint}" if hint else "")),
            cards=[{
                "type": "quote_form",
                "data": {
                    "rfq_id":          rfq.id,
                    "mode_badge":      badge or "",
                    "urgency_label":   rfq.get_urgency_display(),
                    "urgency_tone":    urgency_tone,
                    # ПРИВАТНОСТЬ: продавец видит присвоенный КОД покупателя,
                    # но НИКОГДА имя/компанию. Оператор/админ — реальное имя.
                    "customer_name":   (_("Покупатель %(code)s") % {"code": buyer_code} if role == "seller"
                                        else (rfq.customer_name or "—")),
                    "company_name":    ("" if role == "seller" else (rfq.company_name or "")),
                    "buyer_code":      buyer_code,
                    "request_text":    (rfq.notes or "")[:200],
                    # «Куда» = место назначения (страна · порт) + способ доставки.
                    "destination":     _("🇷🇺 Россия (импорт) · %(port)s · %(mode)s") % {"port": dest_port, "mode": ship_mode_ru},
                    "shipping_mode":   ship_mode,
                    "items":           items_for_card,
                    # Доп. инфо для шапки/таблицы как в карточке заказа.
                    "created_at":      rfq.created_at.strftime("%d.%m.%Y %H:%M") if rfq.created_at else "",
                    "seller_status":   seller_status,
                    "seller_badge":    seller_badge,
                    # Сводка для решения «прямо в карточке» (только для продавца).
                    "seller_view":     role == "seller",
                    "can_fulfill":     can_fulfill,
                    "total_items":     len(items_for_card),
                    "delivery_days":   int(delivery_days),
                    "valid_days":      int(valid_days),
                    "message":         message,
                    "parent_quote_id": parent_quote_id or "",
                    "direction":       direction,
                    # Скорость ответа (честная метрика) + возраст запроса.
                    "seller_speed":    seller_speed,
                    "rfq_age":         rfq_age,
                },
            }],
            # «Открыть RFQ #N» убрана: вела на текстовый пересказ RFQ, который
            # полностью дублирует форму котировки выше (позиции, код покупателя,
            # режим, «куда»). Продавцу это ничего не давало — лишний клик.
            actions=[],
            contextual_actions=[
                {"action": "seller_inbox", "label": _("← Срочные задачи")},
            ],
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
        # Per-позиционные: срок поставки (lead_<id>) и тип OEM/Аналог (cond_<id>)
        lead_raw = params.get(f"lead_{it.id}")
        try:
            lead = int(lead_raw) if lead_raw not in (None, "") else None
        except (TypeError, ValueError):
            lead = None
        cond = params.get(f"cond_{it.id}")
        if cond not in ("oem", "analog"):
            cond = "oem"
        item_data.append({
            "rfq_item": it,
            "unit_price": price,
            "quantity": it.quantity,
            "delivery_days": lead,
            "condition": cond,
        })

    if not item_data:
        return ActionResult(text=_("⚠️ Не указано ни одной цены — котировка не создана."))

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
            delivery_days=d.get("delivery_days"),
            condition=d.get("condition", "oem"),
        )

    # Состояние RFQ — под блокировкой, только если ещё new (идемпотентно).
    from django.db import transaction as _txn
    with _txn.atomic():
        from marketplace.models import RFQ as _RFQ
        _r = _RFQ.objects.select_for_update().get(id=rfq.id)
        if _r.status == "new":
            _r.status = "quoted"
            _r.save(update_fields=["status"])

    # Anti-collusion: если seller написал в message email/phone/messenger
    # с offplatform-намёком — флаг оператору (audit_log + admin-chat alert).
    try:
        from .support_hub import _detect_offplatform_contact
        if message and _detect_offplatform_contact(message):
            from .order_events import notify_operator_alert
            notify_operator_alert(
                user_obj=user, event="user_registered",
                text=(
                    _("⚠️ ANTI-COLLUSION FLAG · Quote от @%(user)s\n"
                      "RFQ #%(rfq)s · Quote #%(quote)s · "
                      "в message обнаружены off-platform контакты:\n"
                      "«%(msg)s»")
                    % {"user": user.username, "rfq": rfq.id, "quote": quote.id, "msg": message[:200]}
                ),
                extra={"role": "seller"},
            )
            logger.warning(
                "anti-collusion: quote #%d by seller @%s contains offplatform contact",
                quote.id, user.username,
            )
    except Exception:
        logger.exception("anti-collusion check on submit_quote failed (non-fatal)")

    # Уведомляем покупателя
    if rfq.created_by:
        _notify(
            rfq.created_by, kind="rfq",
            title=_("Котировка по RFQ #%(id)s") % {"id": rfq.id},
            body=_("%(user)s: $%(amt)s · доставка %(d)s дн.") % {"user": user.username, "amt": f"{total:,.0f}", "d": delivery_days},
            url=f"/chat/?rfq={rfq.id}",
        )

    # Rating event: скорость ПЕРВОГО ответа реально влияет на рейтинг
    # (быстрый +1 / поздний −2 / средний — нейтрально), а рейтинг — на ранжирование
    # предложений в _rank_offers, т.е. на продажи (ТЗ §8). Только round 1:
    # counter-раунды — это переторжка, а не «скорость ответа».
    if round_number == 1:
        try:
            from .rating import record_rating_event
            from .seller_speed import response_event_for
            ev_type, resp_min = response_event_for(rfq, quote)
            if ev_type:
                record_rating_event(
                    user, event_type=ev_type,
                    meta={"rfq_id": rfq.id, "quote_id": quote.id,
                          "response_minutes": resp_min},
                )
        except Exception:
            logger.exception("rating event on submit_quote failed")

    # Ухудшение условий на переторжке (round>1): поднял цену / удлинил срок поставки
    # / сократил срок действия КП → мягкий −1 к рейтингу (не перегибаем). Хорошие
    # изменения (снизил цену) НЕ штрафуются. Сравниваем с прошлой котировкой продавца.
    elif round_number > 1:
        try:
            prev = (Quote.objects.filter(rfq=rfq, seller=user, direction="seller_to_buyer")
                    .exclude(id=quote.id).order_by("-round_number", "-created_at").first())
            if prev is not None:
                worse = {}
                if (quote.total_amount is not None and prev.total_amount is not None
                        and quote.total_amount > prev.total_amount):
                    worse["price_up"] = f"{prev.total_amount}->{quote.total_amount}"
                if (quote.delivery_days or 0) > (prev.delivery_days or 0):
                    worse["delivery_up"] = f"{prev.delivery_days}->{quote.delivery_days}"
                if prev.valid_until and quote.valid_until and quote.valid_until < prev.valid_until:
                    worse["validity_down"] = True
                if worse:
                    from .rating import record_rating_event
                    record_rating_event(
                        user, event_type="terms_worsened",
                        meta={"rfq_id": rfq.id, "quote_id": quote.id, **worse},
                    )
        except Exception:
            logger.exception("terms_worsened rating event failed")

    # Если это counter-respond — пометить parent как countered → submitted (он ответил)
    if parent and parent.direction == "buyer_to_seller":
        parent.status = "submitted"
        parent.save(update_fields=["status"])

    _push_rfq_update(rfq, event="quote_submitted", quote_id=quote.id)

    return ActionResult(
        text=(
            _("✓ Котировка #%(id)s отправлена · $%(amt)s · доставка %(d)s дн.")
            % {"id": quote.id, "amt": f"{total:,.2f}", "d": delivery_days}
            + (_(" (раунд %(r)s)") % {"r": round_number} if round_number > 1 else "")
        ),
        contextual_actions=[
            {"action": "view_rfq_quotes", "label": _("📊 Все котировки"), "params": {"rfq_id": rfq.id}},
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

@register("compare_quotes")
@register("view_rfq_quotes")
def view_rfq_quotes(params, user, role):
    """Список котировок по RFQ. `compare_quotes` — alias на тот же handler
    (кнопка «📊 Сравнить котировки (N)» из RFQ-card зовёт compare_quotes,
    функционал идентичный — отображение всех Quote для buyer)."""
    from marketplace.models import RFQ, Quote
    try:
        rfq = RFQ.objects.get(id=int(params.get("rfq_id") or 0))
    except (RFQ.DoesNotExist, ValueError, TypeError):
        return ActionResult(text=_("RFQ не найден."))

    # IDOR-гейт: аноним не должен видеть чужие анон-RFQ (None==None обходил owner-check)
    if _is_anon(user):
        return _anon_register_result()

    # Только владелец RFQ или оператор/admin
    if rfq.created_by_id != user.id and not (role and role.startswith("operator")) and role != "admin":
        return ActionResult(text=_("Просматривать котировки может только заказчик RFQ или оператор."))

    # Берём ПОСЛЕДНЮЮ котировку каждого продавца (макс. round_number).
    # PERF: один запрос + dedup в Python вместо N+1 (запрос на каждого продавца).
    latest_map = {}
    for q in (Quote.objects.filter(rfq=rfq, direction="seller_to_buyer")
              .select_related("seller").order_by("seller_id", "-round_number")):
        if q.seller_id not in latest_map:
            latest_map[q.seller_id] = q
    latest = sorted(latest_map.values(), key=lambda q: q.total_amount)

    if not latest:
        # PIVOT 2026-05-26: не предлагаем «разослать поставщикам».
        # Цена либо есть в каталоге (SEMI подтверждает), либо нет
        # (фиксируется в аналитику спроса).
        return ActionResult(
            text=(
                _("По RFQ #%(id)s КП ещё не сформирован.\n"
                  "Откройте RFQ и проверьте/подтвердите позиции из каталога.")
                % {"id": rfq.id}
            ),
            actions=[
                {"label": _("Открыть RFQ"), "action": "rfq_detail",
                 "params": {"rfq_id": rfq.id}},
            ],
            contextual_actions=[
                {"action": "get_rfq_status", "label": _("Все RFQ")},
            ],
        )

    # Один КП → сразу показываем составом (spec_results, как состав заказа),
    # а не тонким списком «поставщик — сумма». Делегируем в view_quote.
    if len(latest) == 1:
        return view_quote({"quote_id": latest[0].id}, user, role)

    # Anonymize seller имена для buyer'а (раскрываются после accept_quote)
    is_buyer_view = (rfq.created_by_id == user.id) and role == "buyer"

    items = []
    for idx, q in enumerate(latest):
        if is_buyer_view:
            seller_name = _("Поставщик №%(n)s") % {"n": idx + 1}
        else:
            seller_name = q.seller.username if q.seller else "—"
        flags = []
        if q.is_final: flags.append(_("🔒 финальная"))
        if q.round_number > 1: flags.append(_("раунд %(r)s") % {"r": q.round_number})
        flag_str = " · " + " · ".join(flags) if flags else ""
        items.append({
            "title": f"{seller_name} — ${q.total_amount:,.2f}",
            "subtitle": _("Доставка %(d)s дн%(flags)s") % {"d": q.delivery_days, "flags": flag_str},
            "id": q.id,
        })

    # Suggest accept на самую дешёвую (если она финальная — не предлагаем counter)
    cheapest = latest[0]
    cheapest_label = _("Поставщик №1") if is_buyer_view else (
        cheapest.seller.username if cheapest.seller else "?"
    )
    actions = []
    if cheapest.is_final:
        actions.append({"action": "accept_quote",
                        "label": _("✓ Принять самую дешёвую ($%(amt)s)") % {"amt": f"{cheapest.total_amount:,.0f}"},
                        "params": {"quote_id": cheapest.id}})
    else:
        actions.extend([
            {"action": "accept_quote", "label": _("✓ Принять — %(label)s") % {"label": cheapest_label},
             "params": {"quote_id": cheapest.id}},
            {"action": "counter_offer", "label": _("↩ Контр-оффер"),
             "params": {"quote_id": cheapest.id}},
        ])

    return ActionResult(
        text=(
            _("📊 По RFQ #%(id)s — %(n)s котировок · "
              "диапазон $%(lo)s–$%(hi)s.")
            % {"id": rfq.id, "n": len(latest),
               "lo": f"{latest[0].total_amount:,.0f}", "hi": f"{latest[-1].total_amount:,.0f}"}
        ),
        cards=[{"type": "list", "data": {"title": _("💬 Котировки · RFQ #%(id)s") % {"id": rfq.id},
                                          "items": items}}],
        actions=actions,
        contextual_actions=[
            {"action": "view_quote", "label": _("Открыть лучшую"),
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
        return ActionResult(text=_("Котировка не найдена."))

    # IDOR-гейт: аноним не должен видеть чужие котировки (None==None обходил owner-check)
    if _is_anon(user):
        return _anon_register_result()

    is_buyer = (q.rfq.created_by_id == user.id)
    is_seller = (q.seller_id == user.id)
    if not (is_buyer or is_seller or (role and role.startswith("operator")) or role == "admin"):
        return ActionResult(text=_("Доступ к котировке ограничен."))

    # Buyer видит «Поставщик №N» (по порядку round_number в данном RFQ);
    # accepted-котировки раскрывают имя — buyer уже выбрал и теперь нужны контакты.
    if is_buyer and role == "buyer" and q.status != "accepted":
        # Определяем порядковый номер среди всех котировок этого RFQ от seller_to_buyer
        from marketplace.models import Quote as _Q
        ranked = list(_Q.objects.filter(rfq_id=q.rfq_id, direction="seller_to_buyer")
                      .order_by("total_amount").values_list("seller_id", flat=True))
        try:
            rank = ranked.index(q.seller_id) + 1 if q.seller_id else None
            seller_label = _("Поставщик №%(n)s") % {"n": rank} if rank else _("Поставщик")
        except ValueError:
            seller_label = _("Поставщик")
    else:
        seller_label = q.seller.username if q.seller else "—"

    rows = [
        {"label": _("RFQ"), "value": f"#{q.rfq_id}"},
        {"label": _("Продавец"), "value": seller_label},
        {"label": _("Раунд"), "value": str(q.round_number)},
        {"label": _("Сумма"), "value": f"${q.total_amount:,.2f} {q.currency}", "primary": True},
        {"label": _("Доставка (дн)"), "value": str(q.delivery_days)},
        {"label": _("Статус"), "value": q.get_status_display()},
        {"label": _("Действует до"), "value": q.valid_until.strftime("%d.%m.%Y %H:%M") if q.valid_until else "—"},
    ]
    if q.message:
        rows.append({"label": _("Комментарий"), "value": q.message[:200]})

    # КП показываем В ТОМ ЖЕ ВИДЕ, что и состав заказа — таблица spec_results
    # (позиции + цена поставщика построчно), а не тонкий list.
    _BADGE_RU = {"trusted": _("Надёжный"), "sandbox": _("Проверка"),
                 "risky": _("Рисковый"), "rejected": _("Исключён")}
    _sup_status, _sup_rating = "trusted", 90.0
    try:
        from marketplace.models import UserProfile as _UP
        _p = _UP.objects.filter(user_id=q.seller_id).only("supplier_status", "rating").first()
        if _p:
            _sup_status = _p.supplier_status or "trusted"
            _sup_rating = float(_p.rating or 90.0)
    except Exception:
        pass
    # Бейдж = ТОЛЬКО статус. Рейтинг идёт отдельным полем supplier_rating —
    # фронт склеивает «Надёжный · 90.0». Иначе рейтинг дублировался (× 90.0).
    _badge = _BADGE_RU.get(_sup_status, _("Надёжный"))
    spec_items = []
    _qsum = 0.0
    for qi in q.items.all():
        p = qi.part
        price = float(qi.unit_price or 0)
        qty = qi.quantity or 1
        _qsum += price * qty
        spec_items.append({
            "status": "in_stock",
            "id": (p.oem_number if p else "") or "—",
            "name": ((qi.title_snapshot or (p.title if p else "")) or "—")[:80],
            "brand": (p.brand.name if (p and p.brand_id) else "—"),
            "price": price,
            "qty": qty,
            "supplier_status": _sup_status,
            "supplier_status_badge": _badge,
            "supplier_rating": round(_sup_rating, 1),
        })
    _valid = (q.valid_until.strftime("%d.%m.%Y") if q.valid_until else "—")
    spec_card = {"type": "spec_results", "data": {
        "title": _("КП · RFQ #%(id)s · %(seller)s") % {"id": q.rfq_id, "seller": seller_label},
        "meta": rows,
        "found": len(spec_items), "analogue": 0, "not_found": 0,
        "items": spec_items,
        "offers_count": len(spec_items), "sellers_count": 1,
        "total": int(_qsum) if _qsum else int(q.total_amount or 0),
        "best_mix": int(_qsum) if _qsum else int(q.total_amount or 0),
        "currency": q.currency or "USD",
        "foot_info": (_("%(n)s позиций · доставка %(d)s дн · "
                        "действует до %(valid)s · %(st)s")
                      % {"n": len(spec_items), "d": q.delivery_days,
                         "valid": _valid, "st": q.get_status_display()}),
    }}

    actions = []
    if is_buyer and q.status in ("submitted",) and q.direction == "seller_to_buyer":
        if not q.is_final:
            actions.extend([
                {"action": "accept_quote",  "label": _("✓ Принять"),         "params": {"quote_id": q.id}},
                {"action": "counter_offer", "label": _("↩ Контр-оффер"),     "params": {"quote_id": q.id}},
                {"action": "decline_quote", "label": _("✗ Отклонить"),       "params": {"quote_id": q.id}},
            ])
        else:
            actions.extend([
                {"action": "accept_quote",  "label": _("✓ Принять"),         "params": {"quote_id": q.id}},
                {"action": "decline_quote", "label": _("✗ Отклонить"),       "params": {"quote_id": q.id}},
            ])
    if is_seller and q.status == "submitted" and q.direction == "seller_to_buyer" and not q.is_final:
        actions.append({"action": "mark_quote_final", "label": _("🔒 Зафиксировать как финальную"),
                        "params": {"quote_id": q.id}})
    if is_seller and q.direction == "buyer_to_seller" and q.status == "submitted":
        # Это buyer-counter — продавец должен ответить новой котировкой
        actions.append({"action": "respond_to_counter", "label": _("💬 Ответить на counter"),
                        "params": {"quote_id": q.id}})

    return ActionResult(
        text=_("💬 Котировка #%(id)s · $%(amt)s · %(d)s дн.")
             % {"id": q.id, "amt": f"{q.total_amount:,.2f}", "d": q.delivery_days},
        cards=[spec_card],
        actions=actions,
    )


# ══════════════════════════════════════════════════════════
# 4. accept_quote — buyer принимает → Order
# ══════════════════════════════════════════════════════════

@register("accept_quote")
def accept_quote(params, user, role):
    from marketplace.models import Order, OrderItem, Quote
    confirmed = bool(params.get("confirmed"))
    try:
        q = Quote.objects.select_related("rfq", "seller").get(id=int(params.get("quote_id") or 0))
    except (Quote.DoesNotExist, ValueError, TypeError):
        return ActionResult(text=_("Котировка не найдена."))

    # IDOR-гейт: аноним не должен принимать чужие котировки (None==None обходил owner-check)
    if _is_anon(user):
        return _anon_register_result()

    if q.rfq.created_by_id != user.id:
        return ActionResult(text=_("Принять котировку может только заказчик RFQ."))
    if q.status not in ("submitted", "finalized"):
        return ActionResult(text=_("Эту котировку нельзя принять (статус: %(st)s).") % {"st": q.get_status_display()})

    # Срок действия КП: протухшую котировку принимать нельзя (valid_until nullable)
    from django.utils import timezone as _tz
    if q.valid_until and _tz.now() > q.valid_until:
        return ActionResult(text=(
            _("Котировка истекла %(when)s. Запросите у поставщика новое КП.")
            % {"when": q.valid_until.strftime("%d.%m.%Y %H:%M")}
        ))

    # Бизнес-правило: минимальная сумма заказа. Блокируем И на preview,
    # И на confirm — buyer должен сразу понять что котировку нельзя принять.
    from .order_limits import check_min_order
    block = check_min_order(q.total_amount)
    if block:
        return ActionResult(**block)

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
            seller_label = _("Поставщик №%(n)s") % {"n": rank} if rank else _("Поставщик")
            extra_warn = _("После принятия имя поставщика и контакты будут раскрыты для оформления заказа.")
        else:
            seller_label = q.seller.username if q.seller else "—"
            extra_warn = ""
        warnings = [
            _("После принятия будет создан заказ. Остальные котировки автоматически отклонятся."),
        ]
        if extra_warn:
            warnings.append(extra_warn)
        return ActionResult(
            text=_("Принять котировку #%(id)s от %(seller)s?") % {"id": q.id, "seller": seller_label},
            cards=[{"type": "draft", "data": {
                "title": _("✓ Принять котировку #%(id)s") % {"id": q.id},
                "rows": [
                    {"label": _("Продавец"), "value": seller_label},
                    {"label": _("Сумма"), "value": f"${q.total_amount:,.2f}", "primary": True},
                    {"label": _("Доставка"), "value": _("%(d)s дней") % {"d": q.delivery_days}},
                    {"label": _("Резерв 10%"), "value": f"${(q.total_amount * Decimal('0.10')):,.2f}"},
                ],
                "warnings": warnings,
                "confirm_action": "accept_quote",
                "confirm_label": _("✓ Принять и создать заказ"),
                "confirm_params": {"quote_id": q.id, "confirmed": True},
                "cancel_label": _("Отмена"),
            }}],
        )

    # Защита от «пустого» заказа: позиции без part пропускаются в цикле ниже,
    # поэтому если матча по каталогу нет НИ У ОДНОЙ — Order вышел бы из 0 позиций,
    # а резерв списался бы с полной суммы. Не создаём Order, отдаём на матч/оператора.
    if not q.items.filter(part__isnull=False).exists():
        return ActionResult(text=(
            _("По этой котировке нет позиций для оформления — нужен матч по каталогу или оператор.")
        ))

    # Шаг 2: создаём Order — ПОД БЛОКИРОВКОЙ котировки + re-check статуса, чтобы
    # два параллельных accept_quote не создали два заказа с двойным резервом.
    from django.db import transaction as _txn
    reserve_pct = Decimal("10.00")
    reserve_amount = (q.total_amount * reserve_pct / Decimal("100")).quantize(Decimal("0.01"))
    with _txn.atomic():
        q = (Quote.objects.select_for_update(of=("self",))
             .select_related("rfq", "seller").get(id=q.id))
        if q.status not in ("submitted", "finalized"):
            return ActionResult(
                text=_("Котировка уже обработана (статус: %(st)s).") % {"st": q.get_status_display()},
            )
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
                title=_("Котировка #%(qid)s принята — заказ #%(oid)s") % {"qid": q.id, "oid": order.id},
                body=_("Покупатель оформил заказ на $%(amt)s. Можно начинать.") % {"amt": f"{q.total_amount:,.2f}"},
                url=f"/chat/?order={order.id}")

    _push_rfq_update(q.rfq, event="quote_accepted", quote_id=q.id)

    return ActionResult(
        text=(
            _("✓ Котировка #%(qid)s принята · создан заказ #%(oid)s на $%(amt)s.\n"
              "Следующий шаг — оплатить резерв 10%% ($%(res)s).")
            % {"qid": q.id, "oid": order.id, "amt": f"{q.total_amount:,.2f}", "res": f"{reserve_amount:,.0f}"}
        ),
        actions=[
            {"action": "pay_reserve", "label": _("💳 Оплатить резерв $%(res)s") % {"res": f"{reserve_amount:,.0f}"},
             "params": {"order_id": order.id}},
            {"action": "track_order", "label": _("📦 Трекинг"), "params": {"order_id": order.id}},
        ],
    )


# ══════════════════════════════════════════════════════════
# 4b. auto_accept_and_pay_reserve — one-click AUTO-mode finisher (§4.1)
# ══════════════════════════════════════════════════════════

@register("auto_accept_and_pay_reserve")
def auto_accept_and_pay_reserve(params, user, role):
    """ТЗ §4.1: AUTO-mode завершение в один клик.

    Выбирает САМОЕ ДЕШЁВОЕ КП по RFQ, создаёт Order и сразу списывает
    резерв 10% с депозита. Order заходит в воронку (status=reserve_paid).

    params: {rfq_id, confirmed?}
    """
    from marketplace.models import RFQ, Quote

    from .actions import pay_reserve as _pay_reserve

    confirmed = bool(params.get("confirmed"))
    try:
        rfq = RFQ.objects.get(id=int(params.get("rfq_id") or 0))
    except (RFQ.DoesNotExist, ValueError, TypeError):
        return ActionResult(text=_("RFQ не найден."))

    if rfq.created_by_id != user.id:
        return ActionResult(text=_("Принять КП может только заказчик RFQ."))

    # Выбираем самое дешёвое submitted/finalized КП
    best = (Quote.objects.filter(rfq=rfq, direction="seller_to_buyer",
                                  status__in=("submitted", "finalized"))
            .order_by("total_amount").select_related("seller").first())
    if not best:
        return ActionResult(text=(
            _("По этому RFQ ещё нет котировок. Подождите рассылки или измените режим.")
        ))

    reserve_amount = (best.total_amount * Decimal("0.10")).quantize(Decimal("0.01"))

    # Шаг 1: preview
    if not confirmed:
        return ActionResult(
            text=(
                _("🎯 Лучшее КП по RFQ #%(id)s\n"
                  "Принимаем котировку #%(qid)s и сразу списываем резерв 10%%.")
                % {"id": rfq.id, "qid": best.id}
            ),
            cards=[{"type": "draft", "data": {
                "title": _("🎯 AUTO: принять #%(qid)s и оплатить резерв") % {"qid": best.id},
                "rows": [
                    {"label": _("RFQ"), "value": _("#%(id)s · %(n)s позиций") % {"id": rfq.id, "n": rfq.items.count()}},
                    {"label": _("Поставщик"), "value": _("🥇 Лучший по цене (имя раскроется)")},
                    {"label": _("Сумма заказа"), "value": f"${best.total_amount:,.2f}", "primary": True},
                    {"label": _("Срок поставки"), "value": _("%(d)s дней") % {"d": best.delivery_days}},
                    {"label": _("Резерв 10%"), "value": f"${reserve_amount:,.2f}", "primary": True},
                ],
                "warnings": [
                    _("После клика будет создан Order, остальные котировки автоматически отклоняются."),
                    _("С депозита спишется $%(res)s, удерживается в эскроу платформы.") % {"res": f"{reserve_amount:,.2f}"},
                ],
                "confirm_action": "auto_accept_and_pay_reserve",
                "confirm_label": _("✓ Принять и списать $%(res)s") % {"res": f"{reserve_amount:,.0f}"},
                "confirm_params": {"rfq_id": rfq.id, "confirmed": True},
                "cancel_label": _("Сравнить все КП"),
            }}],
            actions=[
                {"action": "view_rfq_quotes", "label": _("📊 Все котировки"),
                 "params": {"rfq_id": rfq.id}},
            ],
        )

    # Шаг 2: accept_quote inline
    res_accept = accept_quote(
        {"quote_id": best.id, "confirmed": True}, user, role,
    )
    if res_accept.text and "создан заказ" not in res_accept.text:
        # accept_quote вернул ошибку — пробрасываем как есть
        return res_accept

    # Берём order_id напрямую из action="pay_reserve" в ответе accept_quote
    # (надёжнее эвристики «последний заказ buyer'а» при гонках/нескольких заказах).
    order_id = None
    for act in (res_accept.actions or []):
        if act.get("action") == "pay_reserve":
            order_id = (act.get("params") or {}).get("order_id")
            break
    if not order_id:
        # accept_quote не дал заказ (например, защитный гейт) — отдаём его ответ
        return res_accept

    # Шаг 3: pay_reserve inline (если хватает денег на депозите)
    res_pay = _pay_reserve(
        {"order_id": order_id, "confirmed": True}, user, role,
    )
    # res_pay сам вернёт error-текст с кнопкой topup_wallet если денег нет
    return res_pay

@register("counter_offer")
def counter_offer(params, user, role):
    from marketplace.models import Quote, QuoteItem
    confirmed = bool(params.get("confirmed"))
    try:
        q = Quote.objects.select_related("rfq", "seller").get(id=int(params.get("quote_id") or 0))
    except (Quote.DoesNotExist, ValueError, TypeError):
        return ActionResult(text=_("Котировка не найдена."))

    if q.rfq.created_by_id != user.id:
        return ActionResult(text=_("Контр-оффер может предлагать только заказчик RFQ."))
    if q.is_final:
        return ActionResult(text=(
            _("Котировка #%(id)s помечена как финальная — переторжка невозможна. "
              "Только принять или отклонить.") % {"id": q.id}
        ))
    if q.status not in ("submitted",):
        return ActionResult(text=_("Контр-оффер невозможен (статус: %(st)s).") % {"st": q.get_status_display()})

    # Шаг 1: форма с текущими ценами + полем для каждой
    if not confirmed:
        fields = []
        for qi in q.items.all():
            fields.append({
                "name": f"price_{qi.id}",
                "label": _("%(title)s (текущая $%(price)s)") % {"title": qi.title_snapshot[:60], "price": f"{qi.unit_price:,.2f}"},
                "type": "number", "required": True,
                "value": str(qi.unit_price),
            })
        fields.append({"name": "message", "label": _("Сообщение продавцу"), "type": "textarea"})
        return ActionResult(
            text=_("↩ Контр-оффер на котировку #%(id)s") % {"id": q.id},
            cards=[{"type": "form", "data": {
                "title": _("↩ Контр-оффер · #%(id)s") % {"id": q.id},
                "submit_action": "counter_offer",
                "submit_label": _("Отправить контр-оффер"),
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
        return ActionResult(text=_("⚠️ Не указано ни одной новой цены."))

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
                title=_("Контр-оффер по RFQ #%(id)s") % {"id": q.rfq_id},
                body=_("Покупатель предлагает $%(new)s (было $%(old)s).") % {"new": f"{new_total:,.0f}", "old": f"{q.total_amount:,.0f}"},
                url=f"/chat/?rfq={q.rfq_id}")

    _push_rfq_update(q.rfq, event="counter_offer", quote_id=counter_q.id)

    return ActionResult(
        text=(
            _("✓ Контр-оффер #%(id)s отправлен продавцу · $%(amt)s "
              "(раунд %(r)s).")
            % {"id": counter_q.id, "amt": f"{new_total:,.2f}", "r": new_round}
        ),
        contextual_actions=[
            {"action": "view_rfq_quotes", "label": _("📊 Все котировки"), "params": {"rfq_id": q.rfq_id}},
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
        return ActionResult(text=_("Котировка не найдена."))
    if q.direction != "buyer_to_seller":
        return ActionResult(text=_("Это не контр-оффер."))

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
        return ActionResult(text=_("Котировка не найдена."))
    if q.seller_id != user.id:
        return ActionResult(text=_("Зафиксировать может только автор котировки."))
    if q.status != "submitted" or q.direction != "seller_to_buyer":
        return ActionResult(text=_("Эту котировку нельзя зафиксировать в текущем состоянии."))
    q.is_final = True
    q.status = "finalized"
    q.save(update_fields=["is_final", "status"])
    if q.rfq.created_by:
        _notify(q.rfq.created_by, kind="rfq",
                title=_("🔒 Финальная котировка по RFQ #%(id)s") % {"id": q.rfq_id},
                body=_("%(user)s: $%(amt)s. Переторжка невозможна — принять или отклонить.") % {"user": user.username, "amt": f"{q.total_amount:,.0f}"},
                url=f"/chat/?rfq={q.rfq_id}")
    _push_rfq_update(q.rfq, event="quote_finalized", quote_id=q.id)
    return ActionResult(
        text=_("🔒 Котировка #%(id)s зафиксирована как финальная. Покупатель может только принять или отклонить.") % {"id": q.id},
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
        return ActionResult(text=_("Котировка не найдена."))
    if q.rfq.created_by_id != user.id:
        return ActionResult(text=_("Отклонить может только заказчик RFQ."))
    if q.status not in ("submitted", "finalized", "countered"):
        return ActionResult(text=_("Котировка уже не активна (статус: %(st)s).") % {"st": q.get_status_display()})
    q.status = "declined"
    q.save(update_fields=["status"])
    if q.seller:
        _notify(q.seller, kind="rfq",
                title=_("Котировка #%(id)s отклонена") % {"id": q.id},
                body=_("Покупатель не выбрал ваш вариант по RFQ #%(id)s.") % {"id": q.rfq_id},
                url=f"/chat/?rfq={q.rfq_id}")
    _push_rfq_update(q.rfq, event="quote_declined", quote_id=q.id)
    return ActionResult(text=_("✓ Котировка #%(id)s отклонена.") % {"id": q.id})

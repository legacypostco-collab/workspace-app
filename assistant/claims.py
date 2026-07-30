"""ТЗ §5.4: claim workflow с 6 статусами.

Цепочка переходов:

  open → in_review → approved → corrective_actions → closed
                              → financial_settlement → closed (refund)
                  → rejected → closed

Actions:
  open_claim        — buyer открывает после delivered/quality_confirmed
  start_claim_review — operator переводит в in_review
  approve_claim     — operator подтверждает
  reject_claim      — operator отклоняет с reason
  apply_corrective  — выбор пути «Замена/Повторно произвести»
  apply_settlement  — финансовое урегулирование (рефанд)
  close_claim       — финальное закрытие

Hooks:
  approved → rating: claim_confirmed (-7) для seller'а
  apply_settlement → payments.refund_to_buyer
"""
from __future__ import annotations

import logging
from decimal import Decimal

from django.utils import timezone
from django.utils.translation import gettext as _

from .actions import ActionResult, _log_event, _notify, register
from .security import confirmation_is_true

logger = logging.getLogger(__name__)


# ── Permissions ───────────────────────────────────────────────

def _is_operator(role: str) -> bool:
    return bool(role) and (role == "operator" or role.startswith("operator_"))


# ── 1. open_claim — buyer ────────────────────────────────────

@register("open_claim")
def open_claim(params, user, role):
    """Открыть рекламацию по заказу (buyer-action).

    Доступно только если заказ в статусе delivered или completed.
    Two-step DraftCard.
    """
    from marketplace.models import Order, OrderClaim

    confirmed = confirmation_is_true(params.get("confirmed"))
    try:
        order = Order.objects.get(id=int(params.get("order_id") or 0), buyer=user)
    except (Order.DoesNotExist, ValueError, TypeError):
        return ActionResult(text=_("Заказ не найден."))

    if order.status not in ("delivered", "completed"):
        return ActionResult(text=(
            _("Открыть рекламацию можно только после доставки. "
              "Текущий статус: %(status)s")
            % {"status": order.get_status_display()}
        ))

    kind = (params.get("kind") or "").strip()
    title = (params.get("title") or "").strip()
    description = (params.get("description") or "").strip()

    if not confirmed or not kind or not title:
        return ActionResult(
            text=_("⚠️ Открыть рекламацию по заказу #%(id)s") % {"id": order.id},
            cards=[{"type": "form", "data": {
                "title": _("⚠️ Рекламация · #%(id)s") % {"id": order.id},
                "submit_action": "open_claim",
                "fields": [
                    {"name": "kind", "label": _("Тип проблемы"), "required": True,
                     "type": "select",
                     "options": [
                         {"value": "defect",      "label": _("Брак")},
                         {"value": "wrong_part",  "label": _("Не та деталь")},
                         {"value": "missing",     "label": _("Не пришла")},
                         {"value": "damage",      "label": _("Повреждение при доставке")},
                         {"value": "late",        "label": _("Просрочка поставки")},
                         {"value": "other",       "label": _("Другое")},
                     ]},
                    {"name": "title", "label": _("Краткое описание"), "required": True},
                    {"name": "description", "label": _("Подробности"), "type": "textarea",
                     "required": True},
                ],
                "fixed_params": {"order_id": order.id, "confirmed": True},
            }}],
        )

    claim = OrderClaim.objects.create(
        order=order, kind=kind, title=title[:255], description=description,
        opened_by=user, status="open",
    )
    _log_event(order, "claim_opened", actor=user, source="buyer",
               meta={"claim_id": claim.id, "kind": kind, "title": title[:120]})
    # Уведомить операторов
    try:
        from django.contrib.auth import get_user_model
        for op in get_user_model().objects.filter(username__icontains="operator")[:5]:
            _notify(op, kind="claim",
                    title=_("Новая рекламация по #%(id)s") % {"id": order.id},
                    body=_("%(kind)s · %(title)s") % {"kind": kind, "title": title[:120]},
                    url="/chat/")
    except Exception:
        logger.exception("notify operator on open_claim failed")

    return ActionResult(
        text=_("✓ Рекламация #%(id)s открыта · %(kind)s.\nОператор скоро возьмёт в работу.")
             % {"id": claim.id, "kind": claim.get_kind_display()},
        contextual_actions=[
            {"action": "track_order", "label": _("📦 Трекинг"), "params": {"order_id": order.id}},
        ],
    )


# ── 2. start_claim_review — operator ─────────────────────────

@register("start_claim_review")
def start_claim_review(params, user, role):
    if not _is_operator(role) and role != "admin":
        return ActionResult(text=_("Доступно только оператору."))
    from marketplace.models import Order, OrderClaim
    from django.db import transaction as _txn
    try:
        _cid = int(params.get("claim_id") or 0)
    except (ValueError, TypeError):
        return ActionResult(text=_("Рекламация не найдена."))
    # Гонка: lock + re-check статуса, чтобы два оператора не взяли одну в работу.
    with _txn.atomic():
        try:
            claim = OrderClaim.objects.select_for_update().get(id=_cid)
        except OrderClaim.DoesNotExist:
            return ActionResult(text=_("Рекламация не найдена."))
        if claim.status != "open":
            return ActionResult(text=_("Нельзя взять в работу — текущий статус: %(status)s.")
                                     % {"status": claim.get_status_display()})
        claim.status = "in_review"
        claim.reviewed_by = user
        claim.save(update_fields=["status", "reviewed_by", "updated_at"])
    _log_event(claim.order, "claim_status_changed", actor=user, source="operator",
               meta={"claim_id": claim.id, "from": "open", "to": "in_review"})
    if claim.opened_by:
        _notify(claim.opened_by, kind="claim",
                title=_("Рекламация #%(id)s взята в работу") % {"id": claim.id},
                body=_("Оператор %(user)s рассматривает.") % {"user": user.username},
                url=f"/chat/?order={claim.order_id}")
    ctx_actions = [
        {"action": "approve_claim", "label": _("✓ Подтвердить"), "params": {"claim_id": claim.id}},
        {"action": "reject_claim",  "label": _("✗ Отклонить"),  "params": {"claim_id": claim.id}},
    ]
    if claim.opened_by:
        ctx_actions.append({
            "action": "ask_operator",
            "label": _("💬 Чат с покупателем (%(user)s)") % {"user": claim.opened_by.username},
            "params": {"to_user_id": claim.opened_by.id,
                        "context": _("Рекламация #%(id)s по заказу #%(oid)s")
                                   % {"id": claim.id, "oid": claim.order_id}},
        })
    return ActionResult(
        text=_("✓ Рекламация #%(id)s → в работу.") % {"id": claim.id},
        contextual_actions=ctx_actions,
    )


# ── 3. approve_claim — operator ──────────────────────────────

@register("approve_claim")
def approve_claim(params, user, role):
    if not _is_operator(role) and role != "admin":
        return ActionResult(text=_("Доступно только оператору."))
    from marketplace.models import OrderClaim, OrderItem
    confirmed = confirmation_is_true(params.get("confirmed"))
    try:
        claim = OrderClaim.objects.get(id=int(params.get("claim_id") or 0))
    except (OrderClaim.DoesNotExist, ValueError, TypeError):
        return ActionResult(text=_("Рекламация не найдена."))
    if claim.status not in ("open", "in_review"):
        return ActionResult(text=_("Нельзя подтвердить — статус %(status)s.")
                                 % {"status": claim.get_status_display()})

    if not confirmed:
        # contextual_actions: чат с покупателем (опционально), чат с продавцом
        ctx_actions = []
        if claim.opened_by:
            ctx_actions.append({
                "action": "ask_operator",
                "label": _("💬 Чат с покупателем (%(user)s)") % {"user": claim.opened_by.username},
                "params": {"to_user_id": claim.opened_by.id,
                            "context": _("Рекламация #%(id)s по заказу #%(oid)s")
                                       % {"id": claim.id, "oid": claim.order_id}},
            })
        # Чат с продавцом по этому заказу
        try:
            from marketplace.models import OrderItem
            sellers = list({oi.part.seller for oi in
                              OrderItem.objects.filter(order=claim.order)
                              .select_related("part__seller")
                              if oi.part and oi.part.seller})
            for s in sellers[:1]:  # один основной seller
                ctx_actions.append({
                    "action": "ask_operator",
                    "label": _("💬 Чат с продавцом (%(user)s)") % {"user": s.username},
                    "params": {"to_user_id": s.id,
                                "context": _("Рекламация #%(id)s по заказу #%(oid)s")
                                           % {"id": claim.id, "oid": claim.order_id}},
                })
        except Exception:
            pass
        return ActionResult(
            text=_("Подтвердить рекламацию #%(id)s?") % {"id": claim.id},
            cards=[{"type": "draft", "data": {
                "title": _("✓ Подтвердить рекламацию #%(id)s") % {"id": claim.id},
                "rows": [
                    {"label": _("Заказ"), "value": f"#{claim.order_id}"},
                    {"label": _("Тип"), "value": claim.get_kind_display()},
                    {"label": _("Описание"), "value": claim.title[:120], "primary": True},
                ],
                "warnings": [
                    _("Дальше выберите путь: корректирующие действия или финансовое урегулирование."),
                    _("Рейтинг продавца получит штраф (-7) за подтверждённую рекламацию."),
                ],
                "confirm_action": "approve_claim",
                "confirm_label": _("✓ Подтвердить"),
                "confirm_params": {"claim_id": claim.id, "confirmed": True},
                "cancel_label": _("Отмена"),
            }}],
            contextual_actions=ctx_actions,
        )

    # Гонка: lock + re-check, иначе два параллельных approve дважды ставят
    # approved и дважды штрафуют рейтинг продавца (-7).
    from django.db import transaction as _txn
    with _txn.atomic():
        claim = OrderClaim.objects.select_for_update().select_related("order").get(id=claim.id)
        if claim.status not in ("open", "in_review"):
            return ActionResult(
                text=_("Рекламация #%(id)s уже обработана (%(status)s).")
                     % {"id": claim.id, "status": claim.get_status_display()})
        claim.status = "approved"
        claim.reviewed_by = user
        claim.save(update_fields=["status", "reviewed_by", "updated_at"])
        _log_event(claim.order, "claim_status_changed", actor=user, source="operator",
                   meta={"claim_id": claim.id, "from": "in_review", "to": "approved"})

        # Rating: claim_confirmed (-7) для всех продавцов заказа
        try:
            from .rating import record_rating_event
            sellers = list({oi.part.seller for oi in
                            OrderItem.objects.filter(order=claim.order).select_related("part__seller")
                            if oi.part and oi.part.seller})
            for s in sellers:
                record_rating_event(s, event_type="claim_confirmed",
                                    meta={"claim_id": claim.id, "order_id": claim.order_id})
        except Exception:
            logger.exception("rating on claim approve failed")

    if claim.opened_by:
        _notify(claim.opened_by, kind="claim",
                title=_("✓ Рекламация #%(id)s подтверждена") % {"id": claim.id},
                body=_("Дальше: %(kind)s → выберите способ урегулирования.")
                     % {"kind": claim.get_kind_display()},
                url=f"/chat/?order={claim.order_id}")

    # Дополнительно — кнопка чата с покупателем (часто нужен для уточнений
    # перед выбором пути урегулирования).
    post_actions = [
        {"action": "apply_corrective",
         "label": _("🔧 Корректирующие действия"),
         "params": {"claim_id": claim.id}},
        {"action": "apply_settlement",
         "label": _("💸 Финансовое урегулирование"),
         "params": {"claim_id": claim.id}},
    ]
    if claim.opened_by:
        post_actions.append({
            "action": "ask_operator",
            "label": _("💬 Чат с покупателем (%(user)s)") % {"user": claim.opened_by.username},
            "params": {"to_user_id": claim.opened_by.id,
                        "context": _("Рекламация #%(id)s по заказу #%(oid)s")
                                   % {"id": claim.id, "oid": claim.order_id}},
        })
    return ActionResult(
        text=_("✓ Рекламация #%(id)s подтверждена. Выберите путь:") % {"id": claim.id},
        contextual_actions=post_actions,
    )


# ── 4. reject_claim ──────────────────────────────────────────

@register("reject_claim")
def reject_claim(params, user, role):
    if not _is_operator(role) and role != "admin":
        return ActionResult(text=_("Доступно только оператору."))
    from marketplace.models import OrderClaim
    try:
        claim = OrderClaim.objects.get(id=int(params.get("claim_id") or 0))
    except (OrderClaim.DoesNotExist, ValueError, TypeError):
        return ActionResult(text=_("Рекламация не найдена."))
    if claim.status not in ("open", "in_review"):
        return ActionResult(text=_("Нельзя отклонить — статус %(status)s.")
                                 % {"status": claim.get_status_display()})

    reason = (params.get("reason") or "").strip()
    confirmed = confirmation_is_true(params.get("confirmed"))
    if not confirmed or not reason:
        return ActionResult(
            text=_("Отклонить рекламацию #%(id)s?") % {"id": claim.id},
            cards=[{"type": "form", "data": {
                "title": _("✗ Отклонить рекламацию #%(id)s") % {"id": claim.id},
                "submit_action": "reject_claim",
                "fields": [
                    {"name": "reason", "label": _("Причина (видна заявителю)"),
                     "type": "textarea", "required": True},
                ],
                "fixed_params": {"claim_id": claim.id, "confirmed": True},
            }}],
        )

    from django.db import transaction as _txn
    with _txn.atomic():
        claim = OrderClaim.objects.select_for_update().select_related("order").get(id=claim.id)
        if claim.status not in ("open", "in_review"):
            return ActionResult(
                text=_("Рекламация #%(id)s уже обработана (%(status)s).")
                     % {"id": claim.id, "status": claim.get_status_display()})
        claim.status = "rejected"
        claim.reviewed_by = user
        claim.rejection_reason = reason
        claim.save(update_fields=["status", "reviewed_by", "rejection_reason", "updated_at"])
        _log_event(claim.order, "claim_status_changed", actor=user, source="operator",
                   meta={"claim_id": claim.id, "from": "in_review", "to": "rejected", "reason": reason[:200]})
    if claim.opened_by:
        _notify(claim.opened_by, kind="claim",
                title=_("✗ Рекламация #%(id)s отклонена") % {"id": claim.id},
                body=_("Причина: %(reason)s") % {"reason": reason[:160]},
                url=f"/chat/?order={claim.order_id}")
    # Rejected → автоматически closed
    return close_claim({"claim_id": claim.id, "confirmed": True}, user, role)


# ── 5. apply_corrective ──────────────────────────────────────

@register("apply_corrective")
def apply_corrective(params, user, role):
    if not _is_operator(role) and role != "admin":
        return ActionResult(text=_("Доступно только оператору."))
    from marketplace.models import OrderClaim
    try:
        claim = OrderClaim.objects.get(id=int(params.get("claim_id") or 0))
    except (OrderClaim.DoesNotExist, ValueError, TypeError):
        return ActionResult(text=_("Рекламация не найдена."))
    if claim.status != "approved":
        return ActionResult(text=_("Нельзя — статус %(status)s.")
                                 % {"status": claim.get_status_display()})

    resolution = (params.get("resolution_kind") or "").strip()
    confirmed = confirmation_is_true(params.get("confirmed"))
    if not confirmed or resolution not in ("repair", "reproduce"):
        return ActionResult(
            text=_("🔧 Корректирующие действия по #%(id)s") % {"id": claim.id},
            cards=[{"type": "form", "data": {
                "title": _("🔧 Корректирующие действия · #%(id)s") % {"id": claim.id},
                "submit_action": "apply_corrective",
                "fields": [{
                    "name": "resolution_kind", "label": _("Способ"), "required": True,
                    "type": "select",
                    "options": [
                        {"value": "repair",    "label": _("Замена/ремонт")},
                        {"value": "reproduce", "label": _("Повторно произвести")},
                    ],
                }],
                "fixed_params": {"claim_id": claim.id, "confirmed": True},
            }}],
        )

    from django.db import transaction as _txn
    with _txn.atomic():
        claim = OrderClaim.objects.select_for_update().select_related("order").get(id=claim.id)
        if claim.status != "approved":
            return ActionResult(
                text=_("Рекламация #%(id)s уже обработана (%(status)s).")
                     % {"id": claim.id, "status": claim.get_status_display()})
        claim.status = "corrective_actions"
        claim.resolution_kind = resolution
        claim.save(update_fields=["status", "resolution_kind", "updated_at"])
        _log_event(claim.order, "claim_status_changed", actor=user, source="operator",
                   meta={"claim_id": claim.id, "from": "approved", "to": "corrective_actions",
                         "resolution": resolution})
    if claim.opened_by:
        _notify(claim.opened_by, kind="claim",
                title=_("🔧 Рекламация #%(id)s → корректирующие действия") % {"id": claim.id},
                body=_("Решение: %(res)s.") % {"res": claim.get_resolution_kind_display()},
                url=f"/chat/?order={claim.order_id}")

    return ActionResult(
        text=_("✓ Корректирующие действия запущены (%(res)s). После выполнения закройте рекламацию.")
             % {"res": claim.get_resolution_kind_display()},
        contextual_actions=[
            {"action": "close_claim", "label": _("🔒 Закрыть рекламацию"),
             "params": {"claim_id": claim.id}},
        ],
    )


# ── 6. apply_settlement ──────────────────────────────────────

@register("apply_settlement")
def apply_settlement(params, user, role):
    if not _is_operator(role) and role != "admin":
        return ActionResult(text=_("Доступно только оператору."))
    from marketplace.models import Order, OrderClaim

    from . import payments as _pay
    try:
        claim = OrderClaim.objects.get(id=int(params.get("claim_id") or 0))
    except (OrderClaim.DoesNotExist, ValueError, TypeError):
        return ActionResult(text=_("Рекламация не найдена."))
    if claim.status != "approved":
        return ActionResult(text=_("Нельзя — статус %(status)s.")
                                 % {"status": claim.get_status_display()})

    resolution = (params.get("resolution_kind") or "").strip()
    refund_raw = params.get("refund_amount") or "0"
    confirmed = confirmation_is_true(params.get("confirmed"))

    if not confirmed or resolution not in ("partial_refund", "full_refund"):
        return ActionResult(
            text=_("💸 Финансовое урегулирование #%(id)s") % {"id": claim.id},
            cards=[{"type": "form", "data": {
                "title": _("💸 Финансовое урегулирование · #%(id)s") % {"id": claim.id},
                "submit_action": "apply_settlement",
                "fields": [
                    {"name": "resolution_kind", "label": _("Тип возврата"), "required": True,
                     "type": "select",
                     "options": [
                         {"value": "full_refund",    "label": _("Полный возврат")},
                         {"value": "partial_refund", "label": _("Частичный возврат")},
                     ]},
                    {"name": "refund_amount", "label": _("Сумма возврата ($)"),
                     "type": "number"},
                ],
                "fixed_params": {"claim_id": claim.id, "confirmed": True},
            }}],
        )

    try:
        refund_amount = Decimal(str(refund_raw))
    except Exception:
        refund_amount = Decimal("0")
    if not refund_amount.is_finite():
        refund_amount = Decimal("0")

    # P0 (гонка-деньги): claim под select_for_update + re-check статуса ВНУТРИ
    # транзакции. Без этого два параллельных apply_settlement оба проходят guard
    # `status != approved` и дважды зовут refund_to_buyer → двойной возврат.
    from django.db import transaction as _txn
    with _txn.atomic():
        claim = OrderClaim.objects.select_for_update(of=("self",)).get(id=claim.id)
        if claim.status != "approved":
            return ActionResult(text=_("Уже обработано — статус %(status)s.")
                                     % {"status": claim.get_status_display()})
        order = Order.objects.select_for_update(of=("self",)).get(pk=claim.order_id)
        claim.order = order

        if resolution == "full_refund":
            refund_amount = Decimal(str(order.total_amount or 0))
        elif refund_amount <= 0:
            return ActionResult(text=_("Сумма частичного возврата должна быть больше нуля."))
        if refund_amount <= 0:
            return ActionResult(text=_("Для заказа не определена сумма возврата."))
        order_total = Decimal(str(order.total_amount or 0))
        if resolution == "partial_refund" and order_total > 0 and refund_amount > order_total:
            return ActionResult(
                text=_("Частичный возврат не может превышать сумму заказа.")
            )

        escrow_available = max(
            Decimal("0"),
            _pay.escrow_balance_for_order(order.id),
        )
        escrow_attempt = min(refund_amount, escrow_available)
        refunded_from_escrow = Decimal("0")
        if order.buyer_id and escrow_attempt > 0:
            try:
                res = _pay.refund_to_buyer(
                    order=order,
                    buyer=order.buyer,
                    amount=escrow_attempt,
                )
            except _pay.InsufficientEscrow:
                res = {"ok": False}
                logger.info(
                    "apply_settlement: escrow changed for order #%s",
                    claim.order_id,
                )
            if res.get("ok"):
                refunded_from_escrow = Decimal(str(res.get("amount") or 0))
                if (
                    not refunded_from_escrow.is_finite()
                    or refunded_from_escrow < 0
                    or refunded_from_escrow > escrow_attempt
                ):
                    raise ValueError("payment engine returned an invalid refund amount")
        else:
            res = {"ok": False}
        outstanding_refund = max(
            Decimal("0"),
            refund_amount - refunded_from_escrow,
        )

        claim.status = "financial_settlement"
        claim.resolution_kind = resolution
        # refund_amount хранит всё обязательство по решению, а фактическое
        # движение и остаток фиксируются в OrderEvent ниже.
        claim.refund_amount = refund_amount
        claim.save(update_fields=["status", "resolution_kind", "refund_amount", "updated_at"])
        order.payment_status = (
            "refunded"
            if resolution == "full_refund" and outstanding_refund == 0
            else "refund_pending"
        )
        order.save(update_fields=["payment_status"])
        _log_event(
            order,
            "claim_status_changed",
            actor=user,
            source="operator",
            meta={
                "claim_id": claim.id,
                "from": "approved",
                "to": "financial_settlement",
                "resolution": resolution,
                "amount": float(refund_amount),
                "escrow_refunded": float(refunded_from_escrow),
                "outstanding_refund": float(outstanding_refund),
            },
        )

    _amount_fmt = f"{refund_amount:,.2f}"
    _refunded_fmt = f"{refunded_from_escrow:,.2f}"
    _outstanding_fmt = f"{outstanding_refund:,.2f}"
    if outstanding_refund > 0:
        result_text = _(
            "Финансовое урегулирование зафиксировано: возврат $%(amount)s. "
            "Из эскроу перечислено $%(refunded)s; остаток $%(outstanding)s "
            "ожидает возврата внешним способом."
        ) % {
            "amount": _amount_fmt,
            "refunded": _refunded_fmt,
            "outstanding": _outstanding_fmt,
        }
        notification_body = _(
            "%(resolution)s: из эскроу возвращено $%(refunded)s, "
            "остаток $%(outstanding)s находится в обработке."
        ) % {
            "resolution": claim.get_resolution_kind_display(),
            "refunded": _refunded_fmt,
            "outstanding": _outstanding_fmt,
        }
    else:
        result_text = _(
            "✓ Финансовое урегулирование выполнено: %(resolution)s "
            "$%(amount)s возвращено покупателю."
        ) % {
            "resolution": claim.get_resolution_kind_display(),
            "amount": _amount_fmt,
        }
        notification_body = _(
            "%(resolution)s выполнен, средства возвращены."
        ) % {"resolution": claim.get_resolution_kind_display()}

    if claim.opened_by:
        _notify(claim.opened_by, kind="claim",
                title=_("💸 Рекламация #%(id)s — возврат $%(amount)s")
                      % {"id": claim.id, "amount": _amount_fmt},
                body=notification_body,
                url=f"/chat/?order={claim.order_id}")

    return ActionResult(
        text=result_text,
        contextual_actions=[
            {"action": "close_claim", "label": _("🔒 Закрыть рекламацию"),
             "params": {"claim_id": claim.id}},
        ],
    )


# ── 7. close_claim ───────────────────────────────────────────

@register("close_claim")
def close_claim(params, user, role):
    from marketplace.models import OrderClaim
    from django.db import transaction as _txn
    try:
        _cid = int(params.get("claim_id") or 0)
    except (ValueError, TypeError):
        return ActionResult(text=_("Рекламация не найдена."))
    with _txn.atomic():
        try:
            claim = OrderClaim.objects.select_for_update().select_related("order").get(id=_cid)
        except OrderClaim.DoesNotExist:
            return ActionResult(text=_("Рекламация не найдена."))
        if claim.status == "closed":
            return ActionResult(text=_("Рекламация #%(id)s уже закрыта.") % {"id": claim.id})
        # Любой статус (включая rejected) → closed
        prev = claim.status
        claim.status = "closed"
        claim.resolved_by = user
        claim.closed_at = timezone.now()
        claim.save(update_fields=["status", "resolved_by", "closed_at", "updated_at"])
        _log_event(claim.order, "claim_status_changed", actor=user, source="operator",
                   meta={"claim_id": claim.id, "from": prev, "to": "closed"})
    return ActionResult(
        text=_("🔒 Рекламация #%(id)s закрыта (предыдущий статус: %(prev)s).")
             % {"id": claim.id, "prev": prev},
    )


# ── 8. claim_detail (read) ───────────────────────────────────

@register("claim_detail")
def claim_detail(params, user, role):
    from marketplace.models import OrderClaim
    try:
        claim = OrderClaim.objects.select_related("order", "opened_by", "reviewed_by", "resolved_by").get(
            id=int(params.get("claim_id") or 0),
        )
    except (OrderClaim.DoesNotExist, ValueError, TypeError):
        return ActionResult(text=_("Рекламация не найдена."))

    is_owner = claim.opened_by_id == user.id
    is_op = _is_operator(role) or role == "admin"
    if not (is_owner or is_op):
        return ActionResult(text=_("Доступ к рекламации ограничен."))

    rows = [
        {"label": _("Заказ"), "value": f"#{claim.order_id}"},
        {"label": _("Тип"), "value": claim.get_kind_display(), "primary": True},
        {"label": _("Статус"), "value": claim.get_status_display()},
        {"label": _("Описание"), "value": claim.title[:120]},
    ]
    if claim.resolution_kind != "none":
        rows.append({"label": _("Решение"), "value": claim.get_resolution_kind_display()})
    if claim.refund_amount and claim.refund_amount > 0:
        rows.append({"label": _("Возврат"), "value": f"${claim.refund_amount:,.2f}"})
    if claim.rejection_reason:
        rows.append({"label": _("Причина отклонения"), "value": claim.rejection_reason[:200]})
    rows.append({"label": _("Открыта"), "value": claim.created_at.strftime("%d.%m.%Y %H:%M")})

    actions = []
    if is_op:
        if claim.status == "open":
            actions.append({"action": "start_claim_review", "label": _("→ В работу"),
                            "params": {"claim_id": claim.id}})
        elif claim.status == "in_review":
            actions.extend([
                {"action": "approve_claim", "label": _("✓ Подтвердить"), "params": {"claim_id": claim.id}},
                {"action": "reject_claim",  "label": _("✗ Отклонить"),  "params": {"claim_id": claim.id}},
            ])
        elif claim.status == "approved":
            actions.extend([
                {"action": "apply_corrective", "label": _("🔧 Корректирующие"),
                 "params": {"claim_id": claim.id}},
                {"action": "apply_settlement", "label": _("💸 Финансовое урегулирование"),
                 "params": {"claim_id": claim.id}},
            ])
        elif claim.status in ("corrective_actions", "financial_settlement"):
            actions.append({"action": "close_claim", "label": _("🔒 Закрыть"),
                            "params": {"claim_id": claim.id}})

    # Чаты с участниками — всегда доступны (для оператора с покупателем И продавцом,
    # для покупателя — только с оператором поддержки).
    if is_op and claim.opened_by:
        actions.append({
            "action": "ask_operator",
            "label": _("💬 Чат с покупателем (%(user)s)") % {"user": claim.opened_by.username},
            "params": {"to_user_id": claim.opened_by.id,
                        "context": _("Рекламация #%(id)s по заказу #%(oid)s")
                                   % {"id": claim.id, "oid": claim.order_id}},
        })
        try:
            from marketplace.models import OrderItem
            sellers = list({oi.part.seller for oi in
                              OrderItem.objects.filter(order=claim.order)
                              .select_related("part__seller")
                              if oi.part and oi.part.seller})
            for s in sellers[:1]:
                actions.append({
                    "action": "ask_operator",
                    "label": _("💬 Чат с продавцом (%(user)s)") % {"user": s.username},
                    "params": {"to_user_id": s.id,
                                "context": _("Рекламация #%(id)s по заказу #%(oid)s")
                                           % {"id": claim.id, "oid": claim.order_id}},
                })
        except Exception:
            pass

    # ── Контекст рекламации: состав заказа + стандартный «Отчёт по поставке» ──
    # Оператору/владельцу важно видеть, ЧТО в заказе и как прошла поставка,
    # не уходя из рекламации. Берём КАНОНИЧЕСКИЕ карточки:
    #   • spec_results (состав) — из детального вида заказа;
    #   • tracking      (Отчёт по поставке / трекинг) — из track_order.
    # tracking читает ЖИВЫЕ данные о партиях/shipments — поэтому если по итогу
    # рекламации оформят замену (новая партия), отчёт обновится сам, без
    # отдельной заморозки истории.
    context_cards = []
    try:
        from .actions import get_order_detail as _detail, track_order as _track
        _od = _detail({"order_id": claim.order_id}, user, role)
        for c in (_od.cards or []):
            if c.get("type") == "spec_results":
                context_cards.append(c)
                break
        _tr = _track({"order_id": claim.order_id}, user, role)
        for c in (_tr.cards or []):
            if c.get("type") == "tracking":
                context_cards.append(c)
                break
    except Exception:
        logger.exception("claim_detail: контекст заказа не построился")
        context_cards = []

    return ActionResult(
        text=_("📋 Рекламация #%(id)s · %(status)s")
             % {"id": claim.id, "status": claim.get_status_display()},
        cards=[{"type": "draft", "data": {"title": _("Рекламация #%(id)s") % {"id": claim.id},
                                           "rows": rows, "confirm_label": "—"}}]
              + context_cards,
        actions=actions,
    )

from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from marketplace.models import (
    CompanyVerification,
    Order,
    OrderEvent,
    SettlementContract,
    SettlementInvoice,
    SettlementPayment,
)

MONEY = Decimal("0.01")


class SettlementError(ValueError):
    pass


def money(value) -> Decimal:
    try:
        result = Decimal(str(value or 0)).quantize(MONEY)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise SettlementError("Некорректная сумма") from exc
    if not result.is_finite():
        raise SettlementError("Некорректная сумма")
    return result


def settlement_enabled() -> bool:
    return getattr(settings, "SETTLEMENT_MODE", "invoice_contract") == "invoice_contract"


def _is_finance_actor(actor) -> bool:
    if not actor or not getattr(actor, "is_authenticated", False):
        return False
    if getattr(actor, "is_superuser", False):
        return True
    profile = getattr(actor, "profile", None)
    if not profile:
        return False
    return profile.role == "admin" or (
        profile.role == "operator" and profile.operator_role == "payment"
    )


def platform_snapshot() -> dict:
    return {
        "legal_name": getattr(settings, "PLATFORM_LEGAL_NAME", "Consolidator Parts"),
        "address": getattr(settings, "PLATFORM_LEGAL_ADDRESS", ""),
        "tax_id": getattr(settings, "PLATFORM_TAX_ID", ""),
        "registration_no": getattr(settings, "PLATFORM_REGISTRATION_NO", ""),
        "bank_name": getattr(settings, "PLATFORM_BANK_NAME", ""),
        "bank_account": getattr(settings, "PLATFORM_BANK_ACCOUNT", ""),
        "bank_swift": getattr(settings, "PLATFORM_BANK_SWIFT", ""),
        "signatory": getattr(settings, "PLATFORM_SIGNATORY", ""),
        "signatory_title": getattr(settings, "PLATFORM_SIGNATORY_TITLE", "Director"),
    }


def counterparty_snapshot(user, *, order=None) -> dict:
    profile = getattr(user, "profile", None)
    kyb = CompanyVerification.objects.filter(user=user).first()
    legal_name = (
        (kyb.legal_name if kyb else "")
        or (profile.company_name if profile else "")
        or (order.customer_name if order and order.buyer_id == user.id else "")
        or user.get_full_name()
        or user.username
    )
    return {
        "legal_name": legal_name,
        "address": (kyb.legal_address if kyb else ""),
        "tax_id": (kyb.inn if kyb else "") or (profile.tax_id if profile else ""),
        "registration_no": (kyb.ogrn if kyb else ""),
        "bank_name": (kyb.bank_name if kyb else ""),
        "bank_account": (kyb.bank_account if kyb else ""),
        "bank_swift": (kyb.bik if kyb else ""),
        "signatory": (kyb.director_name if kyb else "") or user.get_full_name(),
        "signatory_title": (profile.position if profile else ""),
        "contact": (profile.contact_name if profile else "") or user.get_full_name(),
        "email": user.email,
        "phone": (profile.phone_e164 if profile else ""),
        "country": (kyb.country if kyb else "") or (profile.country if profile else ""),
    }


def validate_party_snapshot(
    snapshot: dict, *, platform=False, require_bank=False
) -> list[str]:
    required = ["legal_name", "address", "tax_id"]
    if platform or require_bank:
        required.extend(["bank_name", "bank_account", "bank_swift", "signatory"])
    return [field for field in required if not str(snapshot.get(field) or "").strip()]


def _terms(order: Order) -> dict:
    return {
        "reserve_percent": str(order.reserve_percent or Decimal("10.00")),
        "incoterm": order.incoterm,
        "shipping_mode": order.shipping_mode,
        "delivery_address": order.delivery_address,
        "order_id": order.id,
    }


def _contract_number(order: Order, kind: str, seller_id: int | None = None) -> str:
    if kind == "buyer_sale":
        return f"CP-SALE-{order.id:06d}"
    return f"CP-BUY-{order.id:06d}-{int(seller_id or 0):06d}"


def _invoice_identity(
    order: Order,
    direction: str,
    stage: str,
    seller_id: int | None = None,
) -> tuple[str, str]:
    stage_code = "R" if stage == "reserve" else "F"
    if direction == "receivable":
        number = f"CP-INV-{order.id:06d}-{stage_code}"
    else:
        number = f"CP-PAY-{order.id:06d}-{int(seller_id or 0):06d}-{stage_code}"
    digest = hashlib.sha256(number.encode("ascii")).hexdigest()[:10].upper()
    return number, f"PAY-{order.id:06d}-{stage_code}-{digest}"


def _seller_totals(order: Order) -> dict[int, Decimal]:
    totals: dict[int, Decimal] = defaultdict(lambda: Decimal("0.00"))
    for item in order.items.select_related("part").all():
        if item.part and item.part.seller_id:
            totals[int(item.part.seller_id)] += money(item.unit_price) * item.quantity
    return {seller_id: money(total) for seller_id, total in totals.items() if total > 0}


def _invoice_due_date():
    days = max(1, min(int(getattr(settings, "SETTLEMENT_INVOICE_DUE_DAYS", 7)), 90))
    return timezone.localdate() + timedelta(days=days)


def _invoice_amount(total: Decimal, reserve_percent: Decimal, stage: str) -> Decimal:
    reserve = money(total * reserve_percent / Decimal("100"))
    return reserve if stage == "reserve" else money(total - reserve)


def _get_or_create_contract(
    *, order, kind, amount, actor, seller=None, status="issued"
) -> SettlementContract:
    now = timezone.now()
    platform = platform_snapshot()
    counterparty = counterparty_snapshot(
        seller if seller is not None else order.buyer,
        order=order,
    )
    if getattr(settings, "SETTLEMENT_REQUIRED", False):
        missing_platform = validate_party_snapshot(platform, platform=True)
        missing_counterparty = validate_party_snapshot(
            counterparty,
            require_bank=seller is not None,
        )
        if missing_platform:
            raise SettlementError(
                "Не заполнены реквизиты компании платформы: "
                + ", ".join(missing_platform)
            )
        if missing_counterparty:
            party = "продавца" if seller is not None else "покупателя"
            raise SettlementError(
                f"Не заполнены реквизиты {party}: "
                + ", ".join(missing_counterparty)
            )
    defaults = {
        "number": _contract_number(order, kind, getattr(seller, "id", None)),
        "status": status,
        "amount": money(amount),
        "currency": getattr(settings, "PAYMENT_CURRENCY", "USD"),
        "platform_snapshot": platform,
        "counterparty_snapshot": counterparty,
        "terms_snapshot": _terms(order),
        "created_by": actor,
        "issued_at": now if status != "draft" else None,
    }
    contract, created = SettlementContract.objects.get_or_create(
        order=order,
        kind=kind,
        seller=seller,
        defaults=defaults,
    )
    if not created:
        changed = []
        for field in (
            "amount", "currency", "platform_snapshot", "counterparty_snapshot", "terms_snapshot"
        ):
            value = defaults[field]
            if getattr(contract, field) != value and contract.status in {"draft", "issued"}:
                setattr(contract, field, value)
                changed.append(field)
        if status == "issued" and contract.status == "draft":
            contract.status = "issued"
            contract.issued_at = now
            changed.extend(["status", "issued_at"])
        if changed:
            contract.save(update_fields=[*changed, "updated_at"])
    return contract


def _get_or_create_invoice(
    *, order, contract, direction, stage, amount, actor, seller=None, status="draft"
) -> SettlementInvoice:
    number, reference = _invoice_identity(
        order, direction, stage, getattr(seller, "id", None)
    )
    now = timezone.now()
    invoice, created = SettlementInvoice.objects.get_or_create(
        order=order,
        direction=direction,
        stage=stage,
        seller=seller,
        defaults={
            "contract": contract,
            "number": number,
            "reference_code": reference,
            "status": status,
            "amount": money(amount),
            "currency": contract.currency,
            "due_date": _invoice_due_date(),
            "created_by": actor,
            "issued_at": now if status == "issued" else None,
        },
    )
    if not created and invoice.status == "draft":
        changed = []
        expected = money(amount)
        if invoice.amount != expected:
            invoice.amount = expected
            changed.append("amount")
        if status == "issued":
            invoice.status = "issued"
            invoice.issued_at = now
            invoice.due_date = _invoice_due_date()
            changed.extend(["status", "issued_at", "due_date"])
        if changed:
            invoice.save(update_fields=[*changed, "updated_at"])
    return invoice


@transaction.atomic
def prepare_settlement_package(order: Order, actor=None) -> dict:
    if not settlement_enabled():
        raise SettlementError("Документарные взаиморасчёты отключены")
    order = (
        Order.objects.select_for_update()
        .select_related("buyer")
        .prefetch_related("items__part__seller")
        .get(pk=order.pk)
    )
    if not order.buyer_id:
        raise SettlementError("У заказа отсутствует покупатель")
    total = money(order.total_amount)
    if total <= 0:
        raise SettlementError("Сумма заказа должна быть больше нуля")
    if not order.items.exists() or order.items.filter(part__seller__isnull=True).exists():
        raise SettlementError(
            "Для каждой позиции заказа должен быть назначен продавец до формирования договоров"
        )
    reserve_percent = money(order.reserve_percent or Decimal("10.00"))
    if reserve_percent <= 0 or reserve_percent >= 100:
        raise SettlementError("Процент первого платежа должен быть от 0 до 100")
    if order.reserve_amount != _invoice_amount(total, reserve_percent, "reserve"):
        order.reserve_amount = _invoice_amount(total, reserve_percent, "reserve")
        order.save(update_fields=["reserve_amount"])

    buyer_contract = _get_or_create_contract(
        order=order,
        kind="buyer_sale",
        amount=total,
        actor=actor,
        status="issued",
    )
    buyer_reserve = _get_or_create_invoice(
        order=order,
        contract=buyer_contract,
        direction="receivable",
        stage="reserve",
        amount=_invoice_amount(total, reserve_percent, "reserve"),
        actor=actor,
        status="issued",
    )
    buyer_final = _get_or_create_invoice(
        order=order,
        contract=buyer_contract,
        direction="receivable",
        stage="final",
        amount=_invoice_amount(total, reserve_percent, "final"),
        actor=actor,
        status="draft",
    )

    seller_contracts = []
    seller_invoices = []
    from django.contrib.auth import get_user_model

    seller_totals = _seller_totals(order)
    users = get_user_model().objects.in_bulk(seller_totals.keys())
    for seller_id, seller_total in seller_totals.items():
        seller = users.get(seller_id)
        if not seller:
            continue
        contract = _get_or_create_contract(
            order=order,
            kind="seller_purchase",
            seller=seller,
            amount=seller_total,
            actor=actor,
            status="issued",
        )
        seller_contracts.append(contract)
        for stage in ("reserve", "final"):
            seller_invoices.append(_get_or_create_invoice(
                order=order,
                contract=contract,
                direction="payable",
                stage=stage,
                seller=seller,
                amount=_invoice_amount(seller_total, reserve_percent, stage),
                actor=actor,
                status="draft",
            ))

    from .settlement_documents import save_contract_document, save_invoice_document

    save_contract_document(buyer_contract, actor)
    save_invoice_document(buyer_reserve, actor)
    for contract in seller_contracts:
        save_contract_document(contract, actor)

    if not OrderEvent.objects.filter(
        order=order,
        event_type="document_uploaded",
        meta__settlement_package=True,
    ).exists():
        OrderEvent.objects.create(
            order=order,
            event_type="document_uploaded",
            source="operator" if actor else "system",
            actor=actor,
            meta={
                "settlement_package": True,
                "buyer_contract_id": buyer_contract.id,
                "buyer_invoice_id": buyer_reserve.id,
                "seller_contract_count": len(seller_contracts),
            },
        )
    return {
        "buyer_contract": buyer_contract,
        "buyer_reserve_invoice": buyer_reserve,
        "buyer_final_invoice": buyer_final,
        "seller_contracts": seller_contracts,
        "seller_invoices": seller_invoices,
    }


@transaction.atomic
def issue_invoice(invoice: SettlementInvoice, actor=None) -> SettlementInvoice:
    invoice = SettlementInvoice.objects.select_for_update().select_related(
        "order", "contract", "seller"
    ).get(pk=invoice.pk)
    if invoice.status in {"paid", "partially_paid", "cancelled"}:
        raise SettlementError("Счёт нельзя повторно выставить в текущем статусе")
    if invoice.direction == "receivable" and invoice.stage == "final":
        if invoice.order.status not in {
            "ready_to_ship", "transit_abroad", "customs", "transit_rf",
            "issuing", "shipped", "delivered", "completed",
        }:
            raise SettlementError(
                "Окончательный счёт покупателю выставляется после готовности к отгрузке"
            )
    if invoice.direction == "payable":
        if invoice.contract.status != "active":
            raise SettlementError(
                "Выплата продавцу заблокирована до подписания закупочного договора обеими сторонами"
            )
        incoming_paid = SettlementInvoice.objects.filter(
            order=invoice.order,
            direction="receivable",
            stage=invoice.stage,
            status="paid",
        ).exists()
        if not incoming_paid:
            raise SettlementError(
                "Счёт продавца нельзя выставить до подтверждения соответствующего платежа покупателя"
            )
    if invoice.status in {"draft", "overdue"}:
        invoice.status = "issued"
        invoice.issued_at = timezone.now()
        invoice.due_date = _invoice_due_date()
        invoice.save(update_fields=["status", "issued_at", "due_date", "updated_at"])
    from .settlement_documents import save_invoice_document

    save_invoice_document(invoice, actor)
    return invoice


@transaction.atomic
def cancel_settlement_package(
    order: Order, actor=None, *, reason="", source="system"
) -> None:
    """Cancel unpaid documents while preserving them for the audit trail."""
    order = Order.objects.select_for_update().get(pk=order.pk)
    if SettlementPayment.objects.filter(
        invoice__order=order,
        status="confirmed",
    ).exists():
        raise SettlementError(
            "По заказу уже есть подтверждённые банковские операции; отмену проводит финансовый оператор"
        )
    if SettlementInvoice.objects.filter(
        order=order,
        status__in={"partially_paid", "paid"},
    ).exists():
        raise SettlementError(
            "По заказу есть оплаченный счёт; автоматическая отмена документов запрещена"
        )
    if SettlementContract.objects.filter(order=order, status="active").exists():
        raise SettlementError(
            "Договор уже подписан обеими сторонами; отмену оформляет оператор"
        )
    now = timezone.now()
    SettlementInvoice.objects.filter(order=order).exclude(status="paid").update(
        status="cancelled",
        cancelled_at=now,
        updated_at=now,
    )
    SettlementContract.objects.filter(order=order).exclude(status="active").update(
        status="cancelled",
        updated_at=now,
    )
    OrderEvent.objects.create(
        order=order,
        event_type="status_changed",
        source=source if source in {"system", "buyer", "seller", "operator"} else "system",
        actor=actor,
        meta={
            "kind": "settlement_package_cancelled",
            "reason": str(reason or "")[:400],
        },
    )


@transaction.atomic
def report_invoice_paid(invoice: SettlementInvoice, payer) -> SettlementInvoice:
    invoice = SettlementInvoice.objects.select_for_update().get(pk=invoice.pk)
    if invoice.direction != "receivable" or invoice.order.buyer_id != payer.id:
        raise SettlementError("Нет доступа к подтверждению этого счёта")
    if invoice.status == "paid":
        return invoice
    if invoice.status == "awaiting_confirmation":
        return invoice
    if invoice.status not in {
        "issued", "overdue", "awaiting_confirmation", "partially_paid"
    }:
        raise SettlementError("Счёт ещё не выставлен")
    invoice.status = "awaiting_confirmation"
    invoice.payer_reported_at = timezone.now()
    invoice.save(update_fields=["status", "payer_reported_at", "updated_at"])
    OrderEvent.objects.create(
        order=invoice.order,
        event_type="payment_reported",
        source="buyer",
        actor=payer,
        meta={
            "settlement_invoice_id": invoice.id,
            "invoice_number": invoice.number,
            "stage": invoice.stage,
        },
    )
    return invoice


def activate_payables_for_contract(contract, actor) -> list[SettlementInvoice]:
    """Issue seller invoices whose matching buyer payment is already confirmed."""
    if contract.kind != "seller_purchase" or contract.status != "active":
        return []
    paid_stages = set(
        SettlementInvoice.objects.filter(
            order=contract.order,
            direction="receivable",
            status="paid",
        ).values_list("stage", flat=True)
    )
    if not paid_stages:
        return []
    issued = []
    payables = SettlementInvoice.objects.filter(
        contract=contract,
        direction="payable",
        stage__in=paid_stages,
        status="draft",
    ).select_related("contract", "seller", "order")
    for payable in payables:
        issued.append(issue_invoice(payable, actor))
    return issued


def _activate_related_payables(invoice, actor) -> None:
    payables = SettlementInvoice.objects.filter(
        order=invoice.order,
        direction="payable",
        stage=invoice.stage,
        status="draft",
        contract__status="active",
    ).select_related("contract", "seller", "order")
    for payable in payables:
        issue_invoice(payable, actor)


def _apply_incoming_order_status(invoice, actor) -> None:
    order = Order.objects.select_for_update().get(pk=invoice.order_id)
    now = timezone.now()
    changed = []
    if invoice.stage == "reserve":
        if order.payment_status in {"awaiting_reserve", "pending"}:
            order.payment_status = "reserve_paid"
            order.reserve_paid_at = now
            changed.extend(["payment_status", "reserve_paid_at"])
            if order.status == "pending":
                order.status = "reserve_paid"
                changed.append("status")
    elif invoice.stage == "final":
        if order.payment_status != "paid":
            order.payment_status = "paid"
            order.final_paid_at = now
            changed.extend(["payment_status", "final_paid_at"])
            if order.status not in {
                "transit_abroad", "customs", "transit_rf", "issuing",
                "delivered", "completed", "cancelled",
            }:
                order.status = "ready_to_ship"
                changed.append("status")
    if changed:
        order.save(update_fields=changed)
        OrderEvent.objects.create(
            order=order,
            event_type="reserve_paid" if invoice.stage == "reserve" else "final_payment_paid",
            source="operator",
            actor=actor,
            meta={
                "settlement_invoice_id": invoice.id,
                "amount": str(invoice.amount),
                "currency": invoice.currency,
            },
        )


@transaction.atomic
def confirm_bank_payment(
    *,
    invoice: SettlementInvoice,
    actor,
    amount,
    bank_reference: str,
    paid_at=None,
    note: str = "",
) -> SettlementPayment:
    if not _is_finance_actor(actor):
        raise SettlementError(
            "Подтверждать банковские операции может только финансовый оператор"
        )
    invoice = SettlementInvoice.objects.select_for_update().select_related(
        "order", "contract", "seller"
    ).get(pk=invoice.pk)
    if invoice.status == "cancelled":
        raise SettlementError("Отменённый счёт нельзя оплатить")
    if invoice.status not in {
        "issued", "awaiting_confirmation", "partially_paid", "overdue"
    }:
        raise SettlementError("Сначала счёт должен быть выставлен")
    if invoice.direction == "payable":
        if invoice.contract.status != "active":
            raise SettlementError(
                "Выплата продавцу заблокирована до подписания закупочного договора"
            )
        incoming_paid = SettlementInvoice.objects.filter(
            order=invoice.order,
            direction="receivable",
            stage=invoice.stage,
            status="paid",
        ).exists()
        if not incoming_paid:
            raise SettlementError(
                "Выплата продавцу заблокирована до подтверждения платежа покупателя"
            )
    amount = money(amount)
    if amount <= 0:
        raise SettlementError("Сумма должна быть больше нуля")
    outstanding = invoice.outstanding_amount
    if amount > outstanding:
        raise SettlementError(
            f"Сумма превышает остаток по счёту ({outstanding} {invoice.currency})"
        )
    reference = " ".join(str(bank_reference or "").split()).upper()[:160]
    if len(reference) < 4:
        raise SettlementError("Укажите номер банковской операции")
    direction = "incoming" if invoice.direction == "receivable" else "outgoing"
    try:
        payment = SettlementPayment.objects.create(
            invoice=invoice,
            direction=direction,
            amount=amount,
            currency=invoice.currency,
            bank_reference=reference,
            paid_at=paid_at or timezone.now(),
            note=str(note or "")[:400],
            confirmed_by=actor,
        )
    except IntegrityError as exc:
        raise SettlementError("Эта банковская операция уже зарегистрирована") from exc
    invoice.paid_amount = money(invoice.paid_amount + amount)
    if invoice.paid_amount == invoice.amount:
        invoice.status = "paid"
        invoice.paid_at = payment.paid_at
    else:
        invoice.status = "partially_paid"
    invoice.save(update_fields=["paid_amount", "status", "paid_at", "updated_at"])
    OrderEvent.objects.create(
        order=invoice.order,
        event_type="payment_confirmed",
        source="operator",
        actor=actor,
        meta={
            "settlement_payment_id": payment.id,
            "settlement_invoice_id": invoice.id,
            "invoice_number": invoice.number,
            "direction": direction,
            "stage": invoice.stage,
            "amount": str(payment.amount),
            "currency": payment.currency,
            "bank_reference": payment.bank_reference,
            "invoice_status": invoice.status,
        },
    )

    if invoice.status == "paid" and invoice.direction == "receivable":
        _apply_incoming_order_status(invoice, actor)
        _activate_related_payables(invoice, actor)
    return payment


@transaction.atomic
def reverse_bank_payment(
    *, payment: SettlementPayment, actor, reason: str
) -> SettlementPayment:
    if not _is_finance_actor(actor):
        raise SettlementError(
            "Отменять банковские операции может только финансовый оператор"
        )
    payment = SettlementPayment.objects.select_for_update().select_related(
        "invoice__order"
    ).get(pk=payment.pk)
    invoice = SettlementInvoice.objects.select_for_update().get(
        pk=payment.invoice_id
    )
    if payment.status == "reversed":
        raise SettlementError("Банковская операция уже отменена")
    reason = str(reason or "").strip()[:400]
    if len(reason) < 5:
        raise SettlementError("Укажите причину отмены банковской операции")
    if invoice.direction == "receivable":
        outgoing_exists = SettlementPayment.objects.filter(
            invoice__order=invoice.order,
            invoice__direction="payable",
            invoice__stage=invoice.stage,
            status="confirmed",
        ).exists()
        if outgoing_exists:
            raise SettlementError(
                "Отменить поступление нельзя: по этому этапу уже есть выплата продавцу"
            )
        order = Order.objects.select_for_update().get(pk=invoice.order_id)
        if invoice.stage == "reserve" and order.status not in {"pending", "reserve_paid"}:
            raise SettlementError(
                "Отменить первый платёж нельзя: заказ уже перешёл к исполнению"
            )
        if invoice.stage == "final" and order.status != "ready_to_ship":
            raise SettlementError(
                "Отменить окончательный платёж нельзя после начала отгрузки"
            )
    new_paid = money(invoice.paid_amount - payment.amount)
    if new_paid < 0:
        raise SettlementError("Сумма отмены превышает подтверждённую оплату")
    invoice.paid_amount = new_paid
    invoice.status = "partially_paid" if new_paid > 0 else "issued"
    invoice.paid_at = None
    invoice.save(update_fields=["paid_amount", "status", "paid_at", "updated_at"])
    payment.status = "reversed"
    payment.reversed_by = actor
    payment.reversed_at = timezone.now()
    payment.reversal_reason = reason
    payment.save(update_fields=[
        "status", "reversed_by", "reversed_at", "reversal_reason"
    ])
    if invoice.direction == "receivable":
        order = Order.objects.select_for_update().get(pk=invoice.order_id)
        if invoice.stage == "reserve":
            order.payment_status = "awaiting_reserve"
            order.reserve_paid_at = None
            if order.status == "reserve_paid":
                order.status = "pending"
            order.save(update_fields=["payment_status", "reserve_paid_at", "status"])
        else:
            order.payment_status = "reserve_paid"
            order.final_paid_at = None
            order.save(update_fields=["payment_status", "final_paid_at"])
    OrderEvent.objects.create(
        order_id=invoice.order_id,
        event_type="payment_reversed",
        source="operator",
        actor=actor,
        meta={
            "settlement_payment_id": payment.id,
            "settlement_invoice_id": invoice.id,
            "invoice_number": invoice.number,
            "direction": payment.direction,
            "stage": invoice.stage,
            "amount": str(payment.amount),
            "currency": payment.currency,
            "bank_reference": payment.bank_reference,
            "reason": reason,
        },
    )
    return payment


def invoice_for_user(invoice_id, user, role):
    query = SettlementInvoice.objects.select_related(
        "order__buyer", "seller", "contract", "document"
    )
    try:
        invoice = query.get(pk=int(invoice_id))
    except (SettlementInvoice.DoesNotExist, TypeError, ValueError):
        return None
    if role == "buyer" and invoice.direction == "receivable" and invoice.order.buyer_id == user.id:
        return invoice
    if role == "seller" and invoice.direction == "payable":
        from marketplace.order_access import seller_principal

        if invoice.seller_id == getattr(seller_principal(user), "id", None):
            return invoice
    if role == "admin" or str(role).startswith("operator"):
        return invoice
    return None

from __future__ import annotations

import logging
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db.models import Sum
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.translation import gettext as _

from marketplace.models import (
    Order,
    SettlementContract,
    SettlementInvoice,
    SettlementPayment,
    UserProfile,
)

from .actions import ActionResult, _notify, register
from .documents import _doc_url
from .security import confirmation_is_true
from .settlements import (
    SettlementError,
    confirm_bank_payment,
    invoice_for_user,
    issue_invoice,
    prepare_settlement_package,
    report_invoice_paid,
    reverse_bank_payment,
)

logger = logging.getLogger(__name__)


def _operator_role(role: str) -> bool:
    return role == "admin" or str(role or "").startswith("operator")


def _finance_role(role: str) -> bool:
    return role in {"admin", "operator_payment"}


def _amount(value, currency="USD") -> str:
    return f"{Decimal(value or 0):,.2f} {currency}"


def _status_tone(status: str) -> str:
    if status == "paid":
        return "ok"
    if status in {"overdue", "cancelled"}:
        return "bad"
    if status in {"awaiting_confirmation", "partially_paid"}:
        return "warn"
    return "info"


def _invoice_row(invoice: SettlementInvoice, *, operator=False) -> dict:
    subtitle = (
        f"{invoice.get_direction_display()} · {invoice.get_stage_display()} · "
        f"оплачено {_amount(invoice.paid_amount, invoice.currency)}"
    )
    row = {
        "title": f"{invoice.number} · {_amount(invoice.amount, invoice.currency)}",
        "subtitle": subtitle,
        "badge": {
            "label": invoice.get_status_display(),
            "tone": _status_tone(invoice.status),
        },
        "tone": _status_tone(invoice.status),
    }
    if operator:
        if invoice.status == "draft":
            can_issue = True
            if invoice.direction == "payable":
                can_issue = (
                    invoice.order.payment_status == "paid"
                    if invoice.stage == "final"
                    else invoice.order.payment_status in {"reserve_paid", "paid"}
                )
                can_issue = can_issue and invoice.contract.status == "active"
            elif invoice.stage == "final":
                can_issue = invoice.order.status in {
                    "ready_to_ship", "transit_abroad", "customs", "transit_rf",
                    "issuing", "shipped", "delivered", "completed",
                }
            if can_issue:
                row.update(
                    action="settlement_issue_invoice",
                    params={"invoice_id": invoice.id},
                )
        elif invoice.status not in {"paid", "cancelled"}:
            if (
                invoice.direction == "payable"
                and invoice.contract.status != "active"
                and invoice.contract.document_id
            ):
                row.update(
                    action="sign_document",
                    params={"document_id": invoice.contract.document_id},
                )
            else:
                row.update(
                    action="settlement_confirm_payment",
                    params={"invoice_id": invoice.id},
                )
    return row


def _invoice_card(invoice: SettlementInvoice) -> dict:
    contract = invoice.contract
    incoming = invoice.direction == "receivable"
    issuer = (
        contract.platform_snapshot if incoming else contract.counterparty_snapshot
    ) or {}
    if invoice.status == "paid":
        expires_text = (
            f"Оплачен {timezone.localtime(invoice.paid_at):%d.%m.%Y}"
            if invoice.paid_at else "Оплачен"
        )
    elif invoice.status == "cancelled":
        expires_text = "Счёт отменён"
    elif invoice.status == "partially_paid":
        expires_text = f"Остаток к оплате до {invoice.due_date:%d.%m.%Y}"
    else:
        expires_text = f"Оплатить до {invoice.due_date:%d.%m.%Y}"
    return {
        "type": "invoice",
        "data": {
            "doc_type": "СЧЁТ НА ОПЛАТУ",
            "amount_text": _amount(invoice.outstanding_amount, invoice.currency),
            "expires_text": expires_text,
            "ref": invoice.reference_code,
            "pdf_url": _doc_url(invoice.document) if invoice.document_id else "",
            "issuer": {
                "name": issuer.get("legal_name") or "Consolidator Parts",
                "subtitle": issuer.get("tax_id") or "",
            },
            "meta": [
                {"label": "Номер", "value": invoice.number},
                {"label": "Договор", "value": contract.number},
                {"label": "Статус", "value": invoice.get_status_display()},
            ],
            "sections": [{
                "title": "Расчёт",
                "rows": [
                    {"label": "Заказ", "value": f"ORD-{invoice.order_id}"},
                    {"label": "Этап", "value": invoice.get_stage_display()},
                    {
                        "label": "Оплачено",
                        "value": _amount(invoice.paid_amount, invoice.currency),
                    },
                    {
                        "label": "Остаток",
                        "value": _amount(invoice.outstanding_amount, invoice.currency),
                    },
                ],
            }],
            "notes": [
                "Укажите код платежа в назначении банковского перевода.",
                "Оплата считается подтверждённой после сверки финансовым оператором.",
            ],
            "stamp_meta": contract.number,
        },
    }


def _payment_row(payment: SettlementPayment, *, operator=False) -> dict:
    direction = (
        _("Поступление от покупателя")
        if payment.direction == "incoming"
        else _("Выплата продавцу")
    )
    row = {
        "title": f"{payment.bank_reference} · {_amount(payment.amount, payment.currency)}",
        "subtitle": (
            f"{direction} · {payment.invoice.number} · "
            f"{timezone.localtime(payment.paid_at):%d.%m.%Y %H:%M}"
        ),
        "badge": {
            "label": payment.get_status_display(),
            "tone": "ok" if payment.status == "confirmed" else "bad",
        },
    }
    if operator:
        row.update(
            action="settlement_payment_detail",
            params={"payment_id": payment.id},
        )
    return row


def _notify_finance(title: str, body: str, order_id: int) -> None:
    ids = set(
        UserProfile.objects.filter(
            role="operator", operator_role="payment"
        ).values_list("user_id", flat=True)
    )
    ids.update(
        get_user_model().objects.filter(
            is_active=True, is_superuser=True
        ).values_list("id", flat=True)
    )
    for recipient in get_user_model().objects.filter(id__in=ids):
        _notify(
            recipient,
            kind="payment",
            title=title,
            body=body,
            url=f"/chat/?order={order_id}",
        )


@register("settlement_prepare")
def settlement_prepare(params, user, role):
    try:
        order = Order.objects.select_related("buyer").get(
            id=int(params.get("order_id") or 0)
        )
    except (Order.DoesNotExist, TypeError, ValueError):
        return ActionResult(text=_("Заказ не найден."))
    if not _operator_role(role) and order.buyer_id != getattr(user, "id", None):
        return ActionResult(text=_("Нет доступа к расчётам этого заказа."))
    package_already_existed = order.settlement_contracts.exists()
    try:
        package = prepare_settlement_package(order, user)
    except SettlementError as exc:
        return ActionResult(text=str(exc), action_succeeded=False)
    invoice = package["buyer_reserve_invoice"]
    if not package_already_existed:
        for contract in package["seller_contracts"]:
            try:
                _notify(
                    contract.seller,
                    kind="order",
                    title=_("Сформирован закупочный договор"),
                    body=_("По заказу #%(id)s доступен договор %(number)s. Подпишите его в разделе расчётов.") % {
                        "id": order.id,
                        "number": contract.number,
                    },
                    url=f"/chat/?order={order.id}",
                )
            except Exception:
                logger.exception(
                    "seller contract notification failed contract_id=%s",
                    contract.id,
                )
    return ActionResult(
        text=_(
            "Договор и первый счёт сформированы. Продавцы получили отдельные "
            "закупочные договоры без реквизитов покупателя."
        ),
        cards=[_invoice_card(invoice)],
        actions=[
            {
                "action": "open_url",
                "label": _("Открыть договор"),
                "params": {"_url": _doc_url(package["buyer_contract"].document)},
            },
            {
                "action": "open_url",
                "label": _("Открыть счёт"),
                "params": {"_url": _doc_url(invoice.document)},
            },
            {
                "action": "settlement_report_paid",
                "label": _("Сообщить об оплате"),
                "params": {"invoice_id": invoice.id},
            },
        ],
        action_succeeded=True,
    )


@register("settlement_my_documents")
def settlement_my_documents(params, user, role):
    order_id = params.get("order_id")
    invoices = SettlementInvoice.objects.select_related(
        "contract", "order", "document"
    ).filter(direction="receivable", order__buyer=user)
    contracts = SettlementContract.objects.select_related("document").filter(
        kind="buyer_sale", order__buyer=user
    )
    if order_id:
        try:
            order_id = int(order_id)
        except (TypeError, ValueError):
            return ActionResult(text=_("Некорректный номер заказа."))
        invoices = invoices.filter(order_id=order_id)
        contracts = contracts.filter(order_id=order_id)
    invoices = list(invoices.order_by("-created_at")[:50])
    contracts = list(contracts.order_by("-created_at")[:25])
    if not invoices and order_id:
        return ActionResult(
            text=_("По заказу расчётные документы ещё не сформированы."),
            actions=[{
                "action": "settlement_prepare",
                "label": _("Сформировать договор и счёт"),
                "params": {"order_id": order_id},
            }],
        )
    rows = [_invoice_row(invoice) for invoice in invoices]
    actions = []
    for contract in contracts:
        if contract.document_id:
            actions.append({
                "action": "open_url",
                "label": f"Договор {contract.number}",
                "params": {"_url": _doc_url(contract.document)},
            })
            if (
                contract.status not in {"active", "cancelled"}
                and not contract.document.signatures.filter(signer=user, method="ep").exists()
            ):
                actions.append({
                    "action": "sign_document",
                    "label": f"Подписать договор {contract.number}",
                    "params": {"document_id": contract.document_id},
                })
    for invoice in invoices:
        if invoice.document_id:
            actions.append({
                "action": "open_url",
                "label": f"Счёт {invoice.number}",
                "params": {"_url": _doc_url(invoice.document)},
            })
        if invoice.status in {"issued", "overdue", "partially_paid"}:
            actions.append({
                "action": "settlement_report_paid",
                "label": f"Сообщить об оплате {invoice.number}",
                "params": {"invoice_id": invoice.id},
            })
    return ActionResult(
        text=_("Расчёты выполняются по счетам и договорам для каждого заказа."),
        cards=[{"type": "list", "data": {"title": _("Мои счета"), "rows": rows}}]
        if rows else [],
        actions=actions[:20],
    )


@register("settlement_seller_documents")
def settlement_seller_documents(params, user, role):
    from marketplace.order_access import seller_principal

    seller = seller_principal(user)
    invoices = SettlementInvoice.objects.select_related(
        "contract", "order", "document"
    ).filter(direction="payable", seller=seller)
    contracts = SettlementContract.objects.select_related("document").filter(
        kind="seller_purchase", seller=seller
    )
    if params.get("order_id"):
        try:
            order_id = int(params["order_id"])
        except (TypeError, ValueError):
            return ActionResult(text=_("Некорректный номер заказа."))
        invoices = invoices.filter(order_id=order_id)
        contracts = contracts.filter(order_id=order_id)
    invoices = list(invoices.order_by("-created_at")[:50])
    contracts = list(contracts.order_by("-created_at")[:25])
    rows = [_invoice_row(invoice) for invoice in invoices]
    actions = [
        {
            "action": "open_url",
            "label": f"Договор {contract.number}",
            "params": {"_url": _doc_url(contract.document)},
        }
        for contract in contracts
        if contract.document_id
    ]
    actions.extend(
        {
            "action": "sign_document",
            "label": f"Подписать договор {contract.number}",
            "params": {"document_id": contract.document_id},
        }
        for contract in contracts
        if (
            contract.document_id
            and contract.status not in {"active", "cancelled"}
            and not contract.document.signatures.filter(signer=user, method="ep").exists()
        )
    )
    actions.extend(
        {
            "action": "open_url",
            "label": f"Счёт {invoice.number}",
            "params": {"_url": _doc_url(invoice.document)},
        }
        for invoice in invoices
        if invoice.document_id
    )
    return ActionResult(
        text=_(
            "Здесь показаны только договоры и выплаты вашей компании. "
            "Данные покупателя и других продавцов скрыты."
        ),
        cards=[{"type": "list", "data": {"title": _("Расчёты с платформой"), "rows": rows}}]
        if rows else [],
        actions=actions[:20],
    )


@register("settlement_report_paid")
def settlement_report_paid_action(params, user, role):
    invoice = invoice_for_user(params.get("invoice_id"), user, role)
    if not invoice or role != "buyer":
        return ActionResult(text=_("Счёт не найден или недоступен."))
    try:
        report_invoice_paid(invoice, user)
    except SettlementError as exc:
        return ActionResult(text=str(exc), action_succeeded=False)
    _notify_finance(
        _("Покупатель сообщил об оплате"),
        f"Счёт {invoice.number}, {_amount(invoice.outstanding_amount, invoice.currency)}. "
        "Нужно сверить банковское поступление.",
        invoice.order_id,
    )
    return ActionResult(
        text=_(
            "Сообщение принято. Статус оплаты изменится только после сверки "
            "банковского поступления финансовым оператором."
        ),
        action_succeeded=True,
    )


@register("settlement_issue_invoice")
def settlement_issue_invoice(params, user, role):
    if not _finance_role(role):
        return ActionResult(text=_("Действие доступно финансовому оператору."))
    invoice = invoice_for_user(params.get("invoice_id"), user, role)
    if not invoice:
        return ActionResult(text=_("Счёт не найден."))
    try:
        invoice = issue_invoice(invoice, user)
    except SettlementError as exc:
        return ActionResult(text=str(exc), action_succeeded=False)
    recipient = invoice.order.buyer if invoice.direction == "receivable" else invoice.seller
    _notify(
        recipient,
        kind="payment",
        title=_("Выставлен счёт"),
        body=f"{invoice.number} · {_amount(invoice.amount, invoice.currency)}",
        url=f"/chat/?order={invoice.order_id}",
    )
    return ActionResult(
        text=f"Счёт {invoice.number} выставлен.",
        cards=[_invoice_card(invoice)],
        action_succeeded=True,
    )


@register("settlement_finance_queue")
def settlement_finance_queue(params, user, role):
    if not _finance_role(role):
        return ActionResult(text=_("Действие доступно финансовому оператору."))
    statuses = [
        "draft", "issued", "awaiting_confirmation", "partially_paid", "overdue"
    ]
    invoice_query = SettlementInvoice.objects.select_related(
        "order", "contract", "seller"
    ).filter(status__in=statuses)
    contract_query = SettlementContract.objects.select_related(
        "document", "order", "seller"
    ).filter(status__in={"issued", "signed"})
    report_params = {}
    if params.get("order_id"):
        try:
            order_id = int(params["order_id"])
        except (TypeError, ValueError):
            return ActionResult(text=_("Некорректный номер заказа."))
        invoice_query = invoice_query.filter(order_id=order_id)
        contract_query = contract_query.filter(order_id=order_id)
        report_params = {"order_id": order_id}
    invoices = list(invoice_query.order_by("due_date", "created_at")[:100])
    incoming = [_invoice_row(item, operator=True) for item in invoices if item.direction == "receivable"]
    outgoing = [_invoice_row(item, operator=True) for item in invoices if item.direction == "payable"]
    cards = []
    if incoming:
        cards.append({"type": "list", "data": {"title": _("Входящие платежи"), "rows": incoming}})
    if outgoing:
        cards.append({"type": "list", "data": {"title": _("Выплаты продавцам"), "rows": outgoing}})
    contracts = list(contract_query.order_by("created_at")[:100])
    if contracts:
        cards.append({
            "type": "list",
            "data": {
                "title": _("Договоры, ожидающие подписи"),
                "rows": [{
                    "title": contract.number,
                    "subtitle": (
                        f"ORD-{contract.order_id} · {contract.get_kind_display()} · "
                        f"{contract.get_status_display()}"
                    ),
                    "badge": {"label": contract.get_status_display(), "tone": "warn"},
                    **(
                        {
                            "action": "sign_document",
                            "params": {"document_id": contract.document_id},
                        }
                        if (
                            contract.document_id
                            and not contract.document.signatures.filter(
                                signer=user, method="ep"
                            ).exists()
                        ) else {}
                    ),
                } for contract in contracts],
            },
        })
    return ActionResult(
        text=_("Очередь неоплаченных и ожидающих подтверждения счетов."),
        cards=cards,
        actions=[{
            "action": "settlement_report",
            "label": _("Сводный отчёт"),
            "params": report_params,
        }],
    )


@register("settlement_confirm_payment")
def settlement_confirm_payment(params, user, role):
    if not _finance_role(role):
        return ActionResult(text=_("Действие доступно финансовому оператору."))
    invoice = invoice_for_user(params.get("invoice_id"), user, role)
    if not invoice:
        return ActionResult(text=_("Счёт не найден."))
    if not confirmation_is_true(params.get("confirmed")):
        direction_label = (
            _("Поступление от покупателя")
            if invoice.direction == "receivable"
            else _("Выплата продавцу")
        )
        return ActionResult(
            text=_("Подтвердите банковскую операцию по фактической выписке."),
            cards=[{"type": "form", "data": {
                "title": f"{direction_label} · {invoice.number}",
                "submit_action": "settlement_confirm_payment",
                "submit_label": _("Подтвердить платёж"),
                "fields": [
                    {
                        "name": "amount",
                        "label": _("Сумма"),
                        "type": "number",
                        "required": True,
                        "step": "0.01",
                        "min": "0.01",
                        "max": str(invoice.outstanding_amount),
                        "value": str(invoice.outstanding_amount),
                    },
                    {
                        "name": "bank_reference",
                        "label": _("Номер банковской операции"),
                        "required": True,
                        "placeholder": _("Номер платёжного поручения или операции"),
                    },
                    {
                        "name": "paid_at",
                        "label": _("Дата и время платежа"),
                        "type": "datetime-local",
                        "value": timezone.localtime().strftime("%Y-%m-%dT%H:%M"),
                    },
                    {
                        "name": "note",
                        "label": _("Комментарий"),
                        "type": "textarea",
                        "rows": 2,
                    },
                ],
                "fixed_params": {"invoice_id": invoice.id, "confirmed": True},
            }}],
        )
    paid_at = parse_datetime(str(params.get("paid_at") or ""))
    if paid_at and timezone.is_naive(paid_at):
        paid_at = timezone.make_aware(paid_at, timezone.get_current_timezone())
    try:
        payment = confirm_bank_payment(
            invoice=invoice,
            actor=user,
            amount=params.get("amount"),
            bank_reference=params.get("bank_reference"),
            paid_at=paid_at,
            note=params.get("note") or "",
        )
    except SettlementError as exc:
        return ActionResult(text=str(exc), action_succeeded=False)
    invoice.refresh_from_db()
    recipient = invoice.order.buyer if invoice.direction == "receivable" else invoice.seller
    _notify(
        recipient,
        kind="payment",
        title=_("Платёж подтверждён"),
        body=(
            f"Счёт {invoice.number}: {_amount(payment.amount, payment.currency)}. "
            f"Статус: {invoice.get_status_display()}."
        ),
        url=f"/chat/?order={invoice.order_id}",
    )
    return ActionResult(
        text=(
            f"Платёж {_amount(payment.amount, payment.currency)} подтверждён. "
            f"Счёт {invoice.number}: {invoice.get_status_display().lower()}."
        ),
        cards=[{
            "type": "payment_proof_upload",
            "data": {
                "payment_id": payment.id,
                "title": _("Подтверждение банковской операции"),
                "label": _("Приложить платёжное поручение или выписку"),
            },
        }],
        actions=[{
            "action": "settlement_payment_detail",
            "label": _("Открыть операцию"),
            "params": {"payment_id": payment.id},
        }],
        action_succeeded=True,
    )


@register("settlement_payment_detail")
def settlement_payment_detail(params, user, role):
    if not _finance_role(role):
        return ActionResult(text=_("Действие доступно финансовому оператору."))
    try:
        payment = SettlementPayment.objects.select_related(
            "invoice__order", "confirmed_by", "reversed_by"
        ).get(id=int(params.get("payment_id") or 0))
    except (SettlementPayment.DoesNotExist, TypeError, ValueError):
        return ActionResult(text=_("Банковская операция не найдена."))
    actions = []
    cards = [{
        "type": "list",
        "data": {
            "title": _("Банковская операция"),
            "rows": [_payment_row(payment)],
        },
    }]
    proof_url = f"/api/assistant/settlements/payments/{payment.id}/proof/"
    if payment.proof_file:
        actions.append({
            "action": "open_url",
            "label": _("Открыть подтверждающий файл"),
            "params": {"_url": proof_url},
        })
    else:
        cards.append({
            "type": "payment_proof_upload",
            "data": {
                "payment_id": payment.id,
                "title": _("Подтверждение банковской операции"),
                "label": _("Приложить платёжное поручение или выписку"),
            },
        })
    if payment.status == "confirmed":
        actions.append({
            "action": "settlement_reverse_payment",
            "label": _("Отменить ошибочную проводку"),
            "params": {"payment_id": payment.id},
            "style": "danger",
        })
    actions.append({
        "action": "settlement_report",
        "label": _("Вернуться к отчёту"),
        "params": {"order_id": payment.invoice.order_id},
    })
    return ActionResult(
        text=(
            f"{payment.get_direction_display()} · {payment.bank_reference} · "
            f"{_amount(payment.amount, payment.currency)}."
        ),
        cards=cards,
        actions=actions,
    )


@register("settlement_reverse_payment")
def settlement_reverse_payment(params, user, role):
    if not _finance_role(role):
        return ActionResult(text=_("Действие доступно финансовому оператору."))
    try:
        payment = SettlementPayment.objects.select_related("invoice").get(
            id=int(params.get("payment_id") or 0)
        )
    except (SettlementPayment.DoesNotExist, TypeError, ValueError):
        return ActionResult(text=_("Банковская операция не найдена."))
    if not confirmation_is_true(params.get("confirmed")):
        return ActionResult(
            text=_("Отмена создаёт обратную проводку в журнале и требует причины."),
            cards=[{"type": "form", "data": {
                "title": f"Отменить операцию {payment.bank_reference}",
                "submit_action": "settlement_reverse_payment",
                "submit_label": _("Отменить проводку"),
                "fields": [{
                    "name": "reason",
                    "label": _("Причина"),
                    "type": "textarea",
                    "required": True,
                    "rows": 3,
                }],
                "fixed_params": {"payment_id": payment.id, "confirmed": True},
            }}],
        )
    try:
        reverse_bank_payment(
            payment=payment,
            actor=user,
            reason=params.get("reason") or "",
        )
    except SettlementError as exc:
        return ActionResult(text=str(exc), action_succeeded=False)
    invoice = payment.invoice
    recipient = invoice.order.buyer if invoice.direction == "receivable" else invoice.seller
    _notify(
        recipient,
        kind="payment",
        title=_("Исправлен статус платежа"),
        body=f"Операция по счёту {invoice.number} отменена финансовым оператором.",
        url=f"/chat/?order={invoice.order_id}",
    )
    return ActionResult(
        text=_("Банковская операция отменена обратной проводкой."),
        action_succeeded=True,
    )


@register("settlement_report")
def settlement_report(params, user, role):
    if not _finance_role(role):
        return ActionResult(text=_("Действие доступно финансовому оператору."))
    invoices = SettlementInvoice.objects.exclude(status="cancelled")
    payments = SettlementPayment.objects.filter(status="confirmed")
    order_id = None
    if params.get("order_id"):
        try:
            order_id = int(params["order_id"])
        except (TypeError, ValueError):
            return ActionResult(text=_("Некорректный номер заказа."))
        invoices = invoices.filter(order_id=order_id)
        payments = payments.filter(invoice__order_id=order_id)
    rows = []
    for direction, title in (
        ("receivable", _("К получению от покупателей")),
        ("payable", _("К выплате продавцам")),
    ):
        summary = invoices.filter(direction=direction).aggregate(
            amount=Sum("amount"), paid=Sum("paid_amount")
        )
        amount = summary["amount"] or Decimal("0.00")
        paid = summary["paid"] or Decimal("0.00")
        rows.append({
            "title": title,
            "subtitle": f"Оплачено {_amount(paid)} · остаток {_amount(amount - paid)}",
            "badge": _amount(amount),
        })
    incoming = payments.filter(direction="incoming").aggregate(value=Sum("amount"))["value"] or 0
    outgoing = payments.filter(direction="outgoing").aggregate(value=Sum("amount"))["value"] or 0
    rows.append({
        "title": _("Денежный поток по подтверждённым операциям"),
        "subtitle": f"Поступило {_amount(incoming)} · выплачено {_amount(outgoing)}",
        "badge": _amount(Decimal(incoming) - Decimal(outgoing)),
    })
    recent_payments = list(
        SettlementPayment.objects.select_related("invoice")
        .filter(**({"invoice__order_id": order_id} if order_id else {}))
        .order_by("-paid_at", "-id")[:30]
    )
    context_params = {"order_id": order_id} if order_id else {}
    report_url = "/api/assistant/settlements/report.csv"
    if order_id:
        report_url += f"?order_id={order_id}"
    cards = [{"type": "list", "data": {"title": _("Расчётный отчёт"), "rows": rows}}]
    if recent_payments:
        cards.append({
            "type": "list",
            "data": {
                "title": _("Последние банковские операции"),
                "rows": [_payment_row(payment, operator=True) for payment in recent_payments],
            },
        })
    return ActionResult(
        text=_("Сводка строится только по подтверждённым счетам и банковским операциям."),
        cards=cards,
        actions=[
            {
                "action": "settlement_finance_queue",
                "label": _("Открыть очередь"),
                "params": context_params,
            },
            {
                "action": "open_url",
                "label": _("Скачать реестр"),
                "params": {"_url": report_url},
            },
        ],
    )

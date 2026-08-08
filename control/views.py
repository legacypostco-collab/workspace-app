from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Count, Q, Sum
from django.http import HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from assistant.models import Conversation, ConversationParticipant
from assistant.permissions import user_allowed_roles
from assistant.settlements import (
    SettlementError,
    confirm_bank_payment,
    platform_snapshot,
    reverse_bank_payment,
)
from marketplace.models import (
    RFQ,
    ActivityEvent,
    CompanyVerification,
    Notification,
    Order,
    OrderClaim,
    OrderEvent,
    Part,
    SettlementInvoice,
    SettlementPayment,
)

from .access import can_access, control_required, control_role, role_label

KYB_CHECKLIST = (
    ("streetview_ok", "Склад существует и соответствует заявленному адресу"),
    ("reviews_ok", "Отзывы и упоминания компании проверены"),
    ("site_ok", "Сайт и сведения о деятельности проверены"),
    ("bank_ok", "Банковские реквизиты и страна счёта сверены"),
    ("certs_ok", "Сертификаты и дилерские полномочия проверены"),
    ("messenger_test_ok", "Контакт в мессенджере подтверждён"),
)

NAVIGATION = (
    ("dashboard", "Обзор", "dashboard", "grid"),
    ("finance", "Финансы", "finance", "wallet"),
    ("orders", "Заказы", "orders", "package"),
    ("users", "Пользователи", "users", "users"),
    ("moderation", "Модерация", "moderation", "shield"),
    ("catalog", "Каталог", "catalog", "layers"),
    ("support", "Поддержка", "support", "message"),
    ("audit", "Журнал действий", "audit", "activity"),
    ("settings", "Настройки", "settings", "settings"),
)


def _page(request, active: str, title: str, **extra):
    nav = []
    for key, label, route, icon in NAVIGATION:
        if can_access(request.user, key):
            nav.append(
                {
                    "key": key,
                    "label": label,
                    "url": reverse(f"control:{route}"),
                    "icon": icon,
                }
            )
    return {
        "active_key": active,
        "page_title": title,
        "control_navigation": nav,
        "control_role": control_role(request.user),
        "control_role_label": role_label(request.user),
        "unread_notifications": request.user.notifications.filter(is_read=False).count(),
        **extra,
    }


def _paginate(request, queryset, per_page=30):
    return Paginator(queryset, per_page).get_page(request.GET.get("page") or 1)


def _money_total(queryset, field="amount") -> Decimal:
    return queryset.aggregate(value=Sum(field))["value"] or Decimal("0.00")


def _outstanding_total(queryset) -> Decimal:
    totals = queryset.aggregate(amount=Sum("amount"), paid=Sum("paid_amount"))
    return max(
        Decimal("0.00"),
        (totals["amount"] or Decimal("0.00")) - (totals["paid"] or Decimal("0.00")),
    )


def _money_totals_by_currency(queryset, field="amount"):
    return [
        {"currency": row["currency"] or "—", "amount": row["value"] or Decimal("0.00")}
        for row in queryset.values("currency").annotate(value=Sum(field)).order_by("currency")
    ]


def _outstanding_totals_by_currency(queryset):
    totals = []
    rows = queryset.values("currency").annotate(amount=Sum("amount"), paid=Sum("paid_amount"))
    for row in rows.order_by("currency"):
        totals.append(
            {
                "currency": row["currency"] or "—",
                "amount": max(
                    Decimal("0.00"),
                    (row["amount"] or Decimal("0.00")) - (row["paid"] or Decimal("0.00")),
                ),
            }
        )
    return totals


def _record(request, action: str, title: str, **meta):
    ActivityEvent.objects.create(
        kind="admin_action",
        actor=request.user,
        actor_role=control_role(request.user)[:20],
        ip=(request.META.get("REMOTE_ADDR") or "")[:64],
        title=title[:255],
        meta={"action": action, **meta},
    )


def _query_value(params, *names):
    for name in names:
        value = (params.get(name) or [""])[0]
        if value:
            return value
    return ""


def _notification_target(user, notification):
    """Translate legacy chat notification targets to internal control pages."""
    parsed = urlparse(notification.url or "")
    params = parse_qs(parsed.query)

    order_id = _query_value(params, "order_id")
    if order_id.isdigit() and Order.objects.filter(pk=order_id).exists():
        return reverse("control:order_detail", args=[order_id])

    rfq_id = _query_value(params, "rfq_id")
    if rfq_id.isdigit() and RFQ.objects.filter(pk=rfq_id).exists():
        return reverse("control:rfq_detail", args=[rfq_id])

    conversation_id = _query_value(params, "conversation", "conversation_id", "conv")
    if conversation_id and can_access(user, "support"):
        try:
            conversation = (
                Conversation.objects.filter(
                    pk=conversation_id,
                )
                .exclude(support_status="")
                .first()
            )
        except (TypeError, ValueError, ValidationError):
            conversation = None
        if conversation:
            return reverse("control:support_detail", args=[conversation.id])

    return reverse("control:notifications")


@control_required("dashboard")
def dashboard(request):
    can_view_finance = can_access(request.user, "finance")
    can_view_moderation = can_access(request.user, "moderation")
    can_view_support = can_access(request.user, "support")
    open_orders = Order.objects.exclude(status__in={"completed", "cancelled"})
    breached_orders = open_orders.filter(sla_status="breached")
    waiting_invoices = (
        SettlementInvoice.objects.filter(
            status__in={"awaiting_confirmation", "overdue"}
        )
        if can_view_finance
        else SettlementInvoice.objects.none()
    )
    open_claims = OrderClaim.objects.exclude(status__in={"closed", "rejected"})
    pending_kyb = (
        CompanyVerification.objects.filter(status="pending")
        if can_view_moderation
        else CompanyVerification.objects.none()
    )
    support_open = (
        Conversation.objects.exclude(support_status="").exclude(
            support_status="closed"
        )
        if can_view_support
        else Conversation.objects.none()
    )
    open_receivables = (
        SettlementInvoice.objects.filter(
            direction="receivable",
            status__in={"issued", "awaiting_confirmation", "partially_paid", "overdue"},
        )
        if can_view_finance
        else SettlementInvoice.objects.none()
    )
    open_payables = (
        SettlementInvoice.objects.filter(
            direction="payable",
            status__in={"issued", "partially_paid", "overdue"},
        )
        if can_view_finance
        else SettlementInvoice.objects.none()
    )

    cards = [
        {
            "label": "Заказы в работе",
            "value": open_orders.count(),
            "hint": f"{breached_orders.count()} с нарушенным сроком",
            "url": reverse("control:orders"),
            "tone": "dark",
            "icon": "package",
        },
        {
            "label": "Нарушения сроков",
            "value": breached_orders.count(),
            "hint": "требуют приоритетной обработки",
            "url": reverse("control:orders") + "?sla=breached",
            "tone": "light",
            "icon": "clock",
        },
    ]
    if can_view_finance:
        cards.append({
            "label": "Платежи к проверке",
            "value": waiting_invoices.count(),
            "hint": "сообщения об оплате и просрочки",
            "url": reverse("control:finance"),
            "tone": "accent",
            "icon": "wallet",
        })
    if can_view_moderation:
        cards.append({
            "label": "Требуют решения",
            "value": open_claims.count() + pending_kyb.count(),
            "hint": f"{pending_kyb.count()} компаний на проверке",
            "url": reverse("control:moderation"),
            "tone": "light",
            "icon": "shield",
        })
    if can_view_support:
        cards.append({
            "label": "Открытые обращения",
            "value": support_open.count(),
            "hint": "ожидают ответа команды",
            "url": reverse("control:support"),
            "tone": "light",
            "icon": "message",
        })

    context = _page(
        request,
        "dashboard",
        "Рабочая сводка",
        cards=cards,
        attention_orders=open_orders.select_related("buyer", "assigned_operator").order_by(
            "-sla_breaches_count", "-created_at"
        )[:7],
        payment_queue=waiting_invoices.select_related("order", "seller").order_by(
            "due_date", "-payer_reported_at"
        )[:6],
        recent_events=OrderEvent.objects.select_related("actor", "order")[:8],
        incoming_total=_outstanding_total(open_receivables),
        outgoing_total=_outstanding_total(open_payables),
        incoming_totals=_outstanding_totals_by_currency(open_receivables),
        outgoing_totals=_outstanding_totals_by_currency(open_payables),
        can_view_finance=can_view_finance,
    )
    return render(request, "control/dashboard.html", context)


@control_required("dashboard")
@require_http_methods(["GET", "POST"])
def notifications(request):
    if request.method == "POST":
        if request.POST.get("action") == "mark_all_read":
            updated = request.user.notifications.filter(is_read=False).update(is_read=True)
            messages.success(request, f"Отмечено прочитанными: {updated}.")
        return redirect("control:notifications")

    page = _paginate(request, request.user.notifications.all(), per_page=40)
    for item in page.object_list:
        item.control_url = reverse("control:notification_open", args=[item.id])
    return render(
        request,
        "control/notifications.html",
        _page(request, "notifications", "Уведомления", notifications=page),
    )


@control_required("dashboard")
def notification_open(request, notification_id):
    notification = get_object_or_404(Notification, pk=notification_id, user=request.user)
    if not notification.is_read:
        notification.is_read = True
        notification.save(update_fields=["is_read"])
    return redirect(_notification_target(request.user, notification))


@control_required("search")
def search(request):
    query = (request.GET.get("q") or "").strip()
    result = {"orders": [], "users": [], "invoices": [], "parts": [], "rfqs": []}
    if query:
        result["orders"] = Order.objects.filter(
            Q(id__icontains=query)
            | Q(customer_name__icontains=query)
            | Q(customer_email__icontains=query)
            | Q(invoice_number__icontains=query)
        )[:10]
        if can_access(request.user, "users"):
            result["users"] = User.objects.filter(
                Q(username__icontains=query)
                | Q(email__icontains=query)
                | Q(first_name__icontains=query)
                | Q(last_name__icontains=query)
                | Q(profile__company_name__icontains=query)
            ).select_related("profile")[:10]
        if can_access(request.user, "finance"):
            result["invoices"] = SettlementInvoice.objects.filter(
                Q(number__icontains=query)
                | Q(reference_code__icontains=query)
                | Q(order__id__icontains=query)
            ).select_related("order")[:10]
        if can_access(request.user, "catalog"):
            result["parts"] = Part.objects.filter(
                Q(title__icontains=query) | Q(oem_number__icontains=query)
            ).select_related("brand", "seller")[:10]
        result["rfqs"] = RFQ.objects.filter(
            Q(id__icontains=query)
            | Q(customer_name__icontains=query)
            | Q(company_name__icontains=query)
        )[:10]
    return render(
        request,
        "control/search.html",
        _page(request, "search", "Поиск", query=query, results=result),
    )


@control_required("orders")
def rfq_detail(request, rfq_id):
    rfq = get_object_or_404(RFQ.objects.select_related("created_by__profile"), pk=rfq_id)
    return render(
        request,
        "control/rfq_detail.html",
        _page(
            request,
            "orders",
            f"Заявка №{rfq.id}",
            rfq=rfq,
            rfq_items=rfq.items.select_related(
                "matched_part__brand", "matched_part__seller__profile"
            ),
            quotes=rfq.quotes.select_related("seller__profile").prefetch_related("items"),
            rfq_events=ActivityEvent.objects.filter(kind="rfq", meta__rfq_id=rfq.id)[:20],
        ),
    )


@control_required("finance")
def finance(request):
    invoices = SettlementInvoice.objects.select_related("order", "contract", "seller").annotate(
        payments_count=Count("payments")
    )
    status = (request.GET.get("status") or "attention").strip()
    direction = (request.GET.get("direction") or "").strip()
    query = (request.GET.get("q") or "").strip()
    if status == "attention":
        invoices = invoices.filter(
            status__in={"awaiting_confirmation", "overdue", "partially_paid"}
        )
    elif status:
        invoices = invoices.filter(status=status)
    if direction:
        invoices = invoices.filter(direction=direction)
    if query:
        invoices = invoices.filter(
            Q(number__icontains=query)
            | Q(reference_code__icontains=query)
            | Q(order__id__icontains=query)
            | Q(order__customer_name__icontains=query)
        )
    payments = SettlementPayment.objects.select_related("invoice", "confirmed_by")[:8]
    open_receivables = SettlementInvoice.objects.filter(
        direction="receivable",
        status__in={"issued", "awaiting_confirmation", "partially_paid", "overdue"},
    )
    open_payables = SettlementInvoice.objects.filter(
        direction="payable",
        status__in={"issued", "partially_paid", "overdue"},
    )
    confirmed_incoming = SettlementPayment.objects.filter(
        status="confirmed",
        paid_at__gte=timezone.now() - timedelta(days=30),
        direction="incoming",
    )
    context = _page(
        request,
        "finance",
        "Финансы",
        invoices=_paginate(request, invoices.order_by("due_date", "-created_at")),
        recent_payments=payments,
        status_filter=status,
        direction_filter=direction,
        query=query,
        incoming_waiting=_outstanding_total(open_receivables),
        outgoing_waiting=_outstanding_total(open_payables),
        confirmed_month=_money_total(confirmed_incoming),
        incoming_waiting_totals=_outstanding_totals_by_currency(open_receivables),
        outgoing_waiting_totals=_outstanding_totals_by_currency(open_payables),
        confirmed_month_totals=_money_totals_by_currency(confirmed_incoming),
    )
    return render(request, "control/finance.html", context)


@control_required("finance")
@require_http_methods(["GET", "POST"])
def invoice_detail(request, invoice_id):
    invoice = get_object_or_404(
        SettlementInvoice.objects.select_related("order__buyer", "contract", "seller", "document"),
        pk=invoice_id,
    )
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "confirm":
                paid_at_value = (request.POST.get("paid_at") or "").strip()
                paid_at = None
                if paid_at_value:
                    paid_at = datetime.fromisoformat(paid_at_value)
                    if timezone.is_naive(paid_at):
                        paid_at = timezone.make_aware(paid_at)
                payment = confirm_bank_payment(
                    invoice=invoice,
                    actor=request.user,
                    amount=request.POST.get("amount"),
                    bank_reference=request.POST.get("bank_reference"),
                    paid_at=paid_at,
                    note=request.POST.get("note", ""),
                )
                _record(
                    request,
                    "payment_confirmed",
                    f"Подтверждён платёж {payment.bank_reference}",
                    invoice_id=invoice.id,
                    payment_id=payment.id,
                    amount=str(payment.amount),
                )
                messages.success(request, "Платёж подтверждён и отражён в расчётах.")
            elif action == "reverse":
                payment = get_object_or_404(
                    SettlementPayment, pk=request.POST.get("payment_id"), invoice=invoice
                )
                reverse_bank_payment(
                    payment=payment,
                    actor=request.user,
                    reason=request.POST.get("reason", ""),
                )
                _record(
                    request,
                    "payment_reversed",
                    f"Отменена проводка {payment.bank_reference}",
                    invoice_id=invoice.id,
                    payment_id=payment.id,
                )
                messages.warning(request, "Проводка отменена. Изменение записано в журнал.")
            else:
                return HttpResponseNotAllowed(["POST"])
        except (SettlementError, ValueError) as exc:
            messages.error(request, str(exc))
        return redirect("control:invoice_detail", invoice_id=invoice.id)

    return render(
        request,
        "control/invoice_detail.html",
        _page(
            request,
            "finance",
            invoice.number,
            invoice=invoice,
            payments=invoice.payments.select_related("confirmed_by", "reversed_by").all(),
            now_local=timezone.localtime().strftime("%Y-%m-%dT%H:%M"),
        ),
    )


@control_required("orders")
def orders(request):
    queryset = Order.objects.select_related("buyer", "assigned_operator", "assigned_kam").annotate(
        items_count=Count("items")
    )
    status = (request.GET.get("status") or "active").strip()
    sla = (request.GET.get("sla") or "").strip()
    query = (request.GET.get("q") or "").strip()
    if status == "active":
        queryset = queryset.exclude(status__in={"completed", "cancelled"})
    elif status:
        queryset = queryset.filter(status=status)
    if sla:
        queryset = queryset.filter(sla_status=sla)
    if query:
        queryset = queryset.filter(
            Q(id__icontains=query)
            | Q(customer_name__icontains=query)
            | Q(customer_email__icontains=query)
            | Q(tracking_number__icontains=query)
        )
    return render(
        request,
        "control/orders.html",
        _page(
            request,
            "orders",
            "Заказы",
            orders=_paginate(request, queryset.order_by("-created_at")),
            status_filter=status,
            sla_filter=sla,
            query=query,
            status_choices=Order.STATUS_CHOICES,
        ),
    )


@control_required("orders")
def order_detail(request, order_id):
    order = get_object_or_404(
        Order.objects.select_related("buyer", "assigned_operator", "assigned_kam", "customer_ref"),
        pk=order_id,
    )
    return render(
        request,
        "control/order_detail.html",
        _page(
            request,
            "orders",
            f"Заказ №{order.id}",
            order=order,
            order_items=order.items.select_related("part__brand", "part__seller"),
            order_events=order.events.select_related("actor")[:30],
            documents=order.documents.select_related("uploaded_by"),
            invoices=order.settlement_invoices.select_related("seller"),
            claims=order.claims.select_related("opened_by", "reviewed_by"),
        ),
    )


@control_required("users")
def users(request):
    queryset = User.objects.select_related("profile").annotate(
        orders_count=Count("orders", distinct=True)
    )
    role = (request.GET.get("role") or "").strip()
    state = (request.GET.get("state") or "active").strip()
    query = (request.GET.get("q") or "").strip()
    if role in {"buyer", "seller", "operator"}:
        queryset = queryset.filter(profile__role=role)
    elif role == "admin":
        queryset = queryset.filter(is_superuser=True)
    if state == "active":
        queryset = queryset.filter(is_active=True)
    elif state == "blocked":
        queryset = queryset.filter(is_active=False)
    if query:
        queryset = queryset.filter(
            Q(username__icontains=query)
            | Q(email__icontains=query)
            | Q(first_name__icontains=query)
            | Q(last_name__icontains=query)
            | Q(profile__company_name__icontains=query)
            | Q(profile__partner_public_code__icontains=query)
            | Q(profile__customer_public_code__icontains=query)
        )
    return render(
        request,
        "control/users.html",
        _page(
            request,
            "users",
            "Пользователи",
            users=_paginate(request, queryset.order_by("-date_joined")),
            role_filter=role,
            state_filter=state,
            query=query,
        ),
    )


@control_required("users")
@require_http_methods(["GET", "POST"])
def user_detail(request, user_id):
    target = get_object_or_404(User.objects.select_related("profile"), pk=user_id)
    if request.method == "POST":
        if not request.user.is_superuser:
            messages.error(request, "Блокировать пользователей может только администратор.")
            return redirect("control:user_detail", user_id=target.id)
        if target.is_superuser:
            messages.error(request, "Учётную запись администратора нельзя изменить здесь.")
            return redirect("control:user_detail", user_id=target.id)
        action = request.POST.get("action")
        if action == "block" and target.is_active:
            target.is_active = False
            target.save(update_fields=["is_active"])
            _record(
                request,
                "user_blocked",
                f"Заблокирован пользователь {target.username}",
                user_id=target.id,
            )
            messages.warning(request, "Доступ пользователя заблокирован.")
        elif action == "unblock" and not target.is_active:
            target.is_active = True
            target.save(update_fields=["is_active"])
            _record(
                request,
                "user_unblocked",
                f"Разблокирован пользователь {target.username}",
                user_id=target.id,
            )
            messages.success(request, "Доступ пользователя восстановлен.")
        return redirect("control:user_detail", user_id=target.id)
    return render(
        request,
        "control/user_detail.html",
        _page(
            request,
            "users",
            target.get_full_name() or target.username,
            target=target,
            allowed_roles=user_allowed_roles(target),
            target_orders=target.orders.order_by("-created_at")[:8],
            target_rfqs=target.rfqs.order_by("-created_at")[:8],
            target_notifications=target.notifications.order_by("-created_at")[:8],
        ),
    )


@control_required("moderation")
def moderation(request):
    return render(
        request,
        "control/moderation.html",
        _page(
            request,
            "moderation",
            "Модерация",
            verifications=CompanyVerification.objects.select_related("user", "reviewed_by")
            .filter(status__in={"pending", "rejected"})
            .order_by("-submitted_at")[:50],
            claims=OrderClaim.objects.select_related("order", "opened_by").exclude(status="closed")[
                :50
            ],
            inactive_parts=Part.objects.select_related("brand", "seller")
            .filter(Q(is_active=False) | Q(availability_status="blocked"))
            .order_by("-id")[:50],
        ),
    )


@control_required("moderation")
@require_http_methods(["GET", "POST"])
def verification_detail(request, user_id):
    verification = get_object_or_404(
        CompanyVerification.objects.select_related("user__profile", "reviewed_by"),
        user_id=user_id,
    )
    if request.method == "POST":
        from assistant.onboarding import (
            op_kyb_approve,
            op_kyb_check,
            op_kyb_clarify,
            op_kyb_reject,
        )

        action = request.POST.get("action")
        role = control_role(request.user)
        if action == "toggle_check":
            item = (request.POST.get("item") or "").strip()
            if item not in dict(KYB_CHECKLIST):
                messages.error(request, "Неизвестный пункт проверки.")
            else:
                op_kyb_check({"user_id": user_id, "item": item}, request.user, role)
        elif action == "approve":
            missing = [
                label
                for key, label in KYB_CHECKLIST
                if not (verification.operator_checklist or {}).get(key)
            ]
            if missing:
                messages.error(request, "Перед одобрением завершите весь лист проверки.")
            else:
                result = op_kyb_approve({"user_id": user_id}, request.user, role)
                verification.refresh_from_db()
                if verification.status == "verified":
                    _record(
                        request,
                        "company_verified",
                        f"Одобрена компания {verification.legal_name or verification.user.username}",
                        user_id=user_id,
                    )
                    messages.success(
                        request, "Компания одобрена. Заявителю отправлено уведомление."
                    )
                else:
                    messages.error(request, result.text)
        elif action == "reject":
            reason = (request.POST.get("reason") or "").strip()
            if not reason:
                messages.error(request, "Укажите причину отклонения.")
            else:
                result = op_kyb_reject(
                    {"user_id": user_id, "reason": reason, "confirmed": True},
                    request.user,
                    role,
                )
                verification.refresh_from_db()
                if verification.status == "rejected":
                    _record(
                        request,
                        "company_rejected",
                        f"Отклонена компания {verification.legal_name or verification.user.username}",
                        user_id=user_id,
                    )
                    messages.warning(request, "Проверка отклонена. Причина передана заявителю.")
                else:
                    messages.error(request, result.text)
        elif action == "clarify":
            note = (request.POST.get("note") or "").strip()
            if not note:
                messages.error(request, "Укажите, какие сведения необходимо уточнить.")
            else:
                op_kyb_clarify(
                    {"user_id": user_id, "note": note, "confirmed": True},
                    request.user,
                    role,
                )
                messages.success(request, "Запрос на уточнение отправлен заявителю.")
        return redirect("control:verification_detail", user_id=user_id)

    checklist_state = verification.operator_checklist or {}
    checklist = [
        {"key": key, "label": label, "checked": bool(checklist_state.get(key))}
        for key, label in KYB_CHECKLIST
    ]
    documents = [
        {"kind": "charter", "label": "Устав", "present": bool(verification.doc_charter)},
        {"kind": "egrul", "label": "Выписка из реестра", "present": bool(verification.doc_egrul)},
        {
            "kind": "passport",
            "label": "Документ руководителя",
            "present": bool(verification.doc_passport),
        },
        {
            "kind": "dealership",
            "label": "Дилерские полномочия",
            "present": bool(verification.doc_dealership),
        },
        {"kind": "bank", "label": "Банковские реквизиты", "present": bool(verification.doc_bank)},
    ]
    return render(
        request,
        "control/verification_detail.html",
        _page(
            request,
            "moderation",
            verification.legal_name or verification.user.username,
            verification=verification,
            checklist=checklist,
            checklist_complete=all(item["checked"] for item in checklist),
            documents=documents,
            api_checks=(verification.api_results or {}).items(),
        ),
    )


@control_required("catalog")
@require_http_methods(["GET", "POST"])
def catalog(request):
    if request.method == "POST":
        if not request.user.is_superuser:
            messages.error(request, "Публикацию каталога может менять только администратор.")
            return redirect("control:catalog")
        part = get_object_or_404(Part, pk=request.POST.get("part_id"))
        action = request.POST.get("action")
        if action in {"publish", "hide"}:
            part.is_active = action == "publish"
            part.save(update_fields=["is_active"])
            verb = "Опубликована" if part.is_active else "Скрыта"
            _record(request, f"part_{action}", f"{verb} позиция {part.oem_number}", part_id=part.id)
            messages.success(request, f"{verb} позиция {part.oem_number}.")
        return redirect(request.get_full_path())

    queryset = Part.objects.select_related("brand", "seller", "category")
    state = (request.GET.get("state") or "attention").strip()
    query = (request.GET.get("q") or "").strip()
    if state == "attention":
        queryset = queryset.filter(
            Q(is_active=False) | Q(mapping_status="needs_review") | Q(availability_status="blocked")
        )
    elif state == "active":
        queryset = queryset.filter(is_active=True)
    elif state == "hidden":
        queryset = queryset.filter(is_active=False)
    if query:
        queryset = queryset.filter(
            Q(title__icontains=query)
            | Q(oem_number__icontains=query)
            | Q(brand__name__icontains=query)
        )
    return render(
        request,
        "control/catalog.html",
        _page(
            request,
            "catalog",
            "Каталог",
            parts=_paginate(request, queryset.order_by("-id")),
            state_filter=state,
            query=query,
            parts_total=Part.objects.count(),
            parts_active=Part.objects.filter(is_active=True).count(),
            parts_attention=Part.objects.filter(
                Q(is_active=False)
                | Q(mapping_status="needs_review")
                | Q(availability_status="blocked")
            ).count(),
        ),
    )


@control_required("support")
def support(request):
    status = (request.GET.get("status") or "open").strip()
    conversations = Conversation.objects.select_related("user", "assigned_operator").exclude(
        support_status=""
    )
    if status == "open":
        conversations = conversations.exclude(support_status="closed")
    elif status:
        conversations = conversations.filter(support_status=status)
    return render(
        request,
        "control/support.html",
        _page(
            request,
            "support",
            "Поддержка",
            conversations=_paginate(request, conversations.order_by("-updated_at")),
            status_filter=status,
        ),
    )


@control_required("support")
@require_http_methods(["GET", "POST"])
def support_detail(request, conversation_id):
    conversation = get_object_or_404(
        Conversation.objects.select_related("user__profile", "assigned_operator"),
        pk=conversation_id,
    )
    if not conversation.support_status:
        messages.error(request, "Этот диалог не является обращением в поддержку.")
        return redirect("control:support")

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "assign_me":
            ConversationParticipant.objects.get_or_create(
                conversation=conversation,
                user=request.user,
                role=control_role(request.user),
            )
            conversation.assigned_operator = request.user
            conversation.save(update_fields=["assigned_operator", "updated_at"])
            _record(
                request,
                "support_assigned",
                f"Назначен ответственный по обращению {conversation.id}",
                conversation_id=str(conversation.id),
            )
            messages.success(request, "Обращение назначено вам.")
        elif action == "reply":
            content = (request.POST.get("content") or "").strip()
            if not content:
                messages.error(request, "Введите ответ.")
            elif conversation.support_status == "closed":
                messages.error(request, "Закрытое обращение нельзя дополнять.")
            else:
                from assistant.support_threads import post_support_message

                ConversationParticipant.objects.get_or_create(
                    conversation=conversation,
                    user=request.user,
                    role=control_role(request.user),
                )
                if not conversation.assigned_operator_id:
                    conversation.assigned_operator = request.user
                    conversation.save(update_fields=["assigned_operator", "updated_at"])
                post_support_message(
                    conversation,
                    request.user,
                    control_role(request.user),
                    content[:10000],
                )
                messages.success(request, "Ответ отправлен пользователю.")
        elif action == "set_status":
            status = (request.POST.get("status") or "").strip()
            allowed = {
                value for value, _label in Conversation._meta.get_field("support_status").choices
            }
            if status not in allowed or not status:
                messages.error(request, "Недопустимое состояние обращения.")
            else:
                conversation.support_status = status
                conversation.save(update_fields=["support_status", "updated_at"])
                _record(
                    request,
                    "support_status_changed",
                    f"Изменено состояние обращения {conversation.id}",
                    conversation_id=str(conversation.id),
                    status=status,
                )
                messages.success(request, "Состояние обращения обновлено.")
        return redirect("control:support_detail", conversation_id=conversation.id)

    thread = list(conversation.messages.select_related("sender").order_by("-created_at")[:100])
    thread.reverse()
    return render(
        request,
        "control/support_detail.html",
        _page(
            request,
            "support",
            conversation.title or "Обращение",
            conversation=conversation,
            thread=thread,
            participants=conversation.participant_links.select_related("user"),
        ),
    )


@control_required("audit")
def audit(request):
    action = (request.GET.get("action") or "").strip()
    events = ActivityEvent.objects.select_related("actor")
    if action:
        events = events.filter(meta__action=action)
    return render(
        request,
        "control/audit.html",
        _page(
            request,
            "audit",
            "Журнал действий",
            events=_paginate(request, events.order_by("-created_at"), 40),
            action_filter=action,
        ),
    )


@control_required("settings")
def platform_settings(request):
    company = platform_snapshot()
    payment_currency = getattr(settings, "PAYMENT_CURRENCY", "USD")
    bank_currency = company.get("bank_currency") or ""
    checks = [
        (
            "Договоры и счета",
            getattr(settings, "SETTLEMENT_MODE", "invoice_contract") == "invoice_contract",
            "Рабочий расчётный контур",
        ),
        (
            "Реквизиты платформы",
            bool(
                company.get("legal_name") and company.get("tax_id") and company.get("bank_account")
            ),
            "Юридические и банковские данные",
        ),
        (
            "Почтовые уведомления",
            bool(getattr(settings, "EMAIL_HOST", "")),
            "Подключение к почтовому серверу",
        ),
        (
            "Языковая модель",
            bool(getattr(settings, "OPENAI_API_KEY", "")),
            "Свободный диалог и интеллектуальный поиск",
        ),
        (
            "Фоновая очередь",
            bool(getattr(settings, "CELERY_BROKER_URL", "")),
            "Задачи, письма и периодические проверки",
        ),
        (
            "Защищённое соединение",
            bool(getattr(settings, "USE_HTTPS", False)),
            "Защищённые cookies и перенаправление",
        ),
    ]
    return render(
        request,
        "control/settings.html",
        _page(
            request,
            "settings",
            "Состояние платформы",
            checks=checks,
            company=company,
            payment_currency=payment_currency,
            bank_currency=bank_currency or "Не указана",
            bank_currency_missing=not bank_currency,
            currency_mismatch=bool(
                bank_currency and payment_currency.upper() != bank_currency.upper()
            ),
        ),
    )

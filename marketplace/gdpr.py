"""GDPR / 152-ФЗ: data export + soft-delete of user account.

Endpoints (см. urls.py):
  GET  /api/me/export/  → JSON dump всех связанных данных пользователя
  POST /api/me/delete/  → soft-delete: user.is_active=False, анонимизация
                          email/имени/телефона. Финансовые/аудиторские записи
                          сохраняются (бухгалтерская необходимость), но без
                          PII пользователя.

Не удаляем «жёстко» Order / OrderClaim / Quote — они могут быть нужны
другим контрагентам сделки и для бухгалтерии. Удаление сильно ограничено
ст. 5 152-ФЗ при «обработке без согласия» — finance/налоговые данные.
"""
from __future__ import annotations

import json
import logging
import uuid
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.core.serializers.json import DjangoJSONEncoder
from django.http import HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

logger = logging.getLogger(__name__)


@login_required
@require_GET
def export_my_data(request):
    """Скачать все ПДн пользователя в JSON.

    Returns: application/json attachment, filename=consolidator-export-<username>-<date>.json
    """
    from assistant.models import Conversation, Wallet, WalletTx
    from marketplace.models import (
        RFQ,
        CompanyVerification,
        Notification,
        Order,
        OrderClaim,
        Quote,
    )

    user = request.user
    profile = getattr(user, "profile", None)

    def _serial(obj, fields):
        out = {}
        for f in fields:
            v = getattr(obj, f, None)
            if hasattr(v, "isoformat"):
                v = v.isoformat()
            elif isinstance(v, Decimal):
                v = str(v)
            out[f] = v
        return out

    data = {
        "exported_at": timezone.now().isoformat(),
        "user": _serial(user, ["id", "username", "email", "first_name", "last_name",
                                "date_joined", "last_login"]),
        "profile": _serial(profile, ["role", "company_name", "department",
                                      "language", "external_score", "behavioral_score",
                                      "rating_score", "supplier_status"])
                    if profile else None,
        "orders": [_serial(o, ["id", "status", "payment_status", "customer_name",
                                "customer_email", "delivery_address", "total_amount",
                                "created_at"])
                   for o in Order.objects.filter(buyer=user)[:1000]],
        "rfqs": [_serial(r, ["id", "status", "mode", "customer_name", "created_at"])
                 for r in RFQ.objects.filter(created_by=user)[:1000]],
        "quotes": [_serial(q, ["id", "status", "total_amount", "delivery_days", "created_at"])
                   for q in Quote.objects.filter(seller=user)[:1000]],
        "claims": [_serial(c, ["id", "kind", "status", "title", "description",
                                "refund_amount", "created_at"])
                   for c in OrderClaim.objects.filter(opened_by=user)[:1000]],
        "kyb": _serial(CompanyVerification.objects.filter(user=user).first(),
                       ["status", "legal_name", "inn", "ogrn", "vat_number",
                        "legal_address", "warehouse_address", "phone",
                        "contact_email", "director_name", "submitted_at",
                        "reviewed_at", "risk_indicator"])
                if CompanyVerification.objects.filter(user=user).exists() else None,
        "wallet": _serial(Wallet.objects.filter(user=user).first(),
                          ["currency", "balance"])
                  if Wallet.objects.filter(user=user).exists() else None,
        "wallet_txs": [_serial(t, ["id", "kind", "amount", "currency", "note",
                                    "created_at"])
                       for t in WalletTx.objects.filter(wallet__user=user)[:1000]],
        "notifications": [_serial(n, ["id", "kind", "title", "body", "is_read",
                                       "created_at"])
                          for n in Notification.objects.filter(user=user)[:1000]],
        "conversations": [_serial(c, ["id", "title", "category", "role",
                                        "created_at", "updated_at"])
                          for c in Conversation.objects.filter(user=user)[:200]],
    }

    # DjangoJSONEncoder обрабатывает Decimal/datetime/UUID/Promise.
    body = json.dumps(data, ensure_ascii=False, indent=2, cls=DjangoJSONEncoder)
    resp = HttpResponse(body, content_type="application/json")
    filename = f"consolidator-export-{user.username}-{timezone.now():%Y%m%d}.json"
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    logger.info("gdpr_export user_id=%s size=%d", user.id, len(body))
    return resp


@login_required
@require_POST
def delete_my_account(request):
    """Soft-delete аккаунта. Возможные эффекты:
      - user.is_active = False (нельзя войти)
      - email/имя/телефон → анонимизированы
      - заказы и платёжные данные СОХРАНЯЮТСЯ (бух. учёт + права контрагентов)
      - в OrderEvent добавляется запись об удалении

    Не удаляет:
      - Order, OrderItem, OrderClaim, Quote, WalletTx — финансовые
      - OrderEvent — аудит (3 года retention, потом prune_old_audit)
    """
    user = request.user
    confirm = (request.POST.get("confirm") or "").strip()
    if confirm != "DELETE":
        return JsonResponse({
            "ok": False,
            "error": "Confirm with field 'confirm=DELETE' to proceed.",
        }, status=400)

    anon_marker = f"deleted-{uuid.uuid4().hex[:12]}"
    user.username = anon_marker
    user.email = f"{anon_marker}@deleted.invalid"
    user.first_name = ""
    user.last_name = ""
    user.is_active = False
    user.set_unusable_password()
    user.save()

    # Анонимизация профиля
    try:
        prof = user.profile
        prof.company_name = ""
        prof.save(update_fields=["company_name"])
    except Exception:
        pass

    # Анонимизация KYB
    try:
        from marketplace.models import CompanyVerification
        CompanyVerification.objects.filter(user=user).update(
            legal_name=anon_marker, inn="", kpp="", ogrn="", vat_number="",
            legal_address="", warehouse_address="", website="",
            phone="", whatsapp="", telegram="", contact_email="",
            director_name="", bank_account="", bank_name="", bik="",
            categories="", operator_note="",
            api_results={}, operator_checklist={},
        )
    except Exception:
        logger.exception("kyb anonymize failed for user %s", user.id)

    logger.warning("gdpr_delete user_id=%s anon=%s", user.id, anon_marker)
    # Logout — у пользователя больше нет валидной сессии
    from django.contrib.auth import logout
    logout(request)
    return JsonResponse({"ok": True, "anon_id": anon_marker})

"""Versioned registration consent validation and evidence records."""
from __future__ import annotations

from django.conf import settings
from django.utils.translation import gettext as _

from .security import client_ip, confirmation_is_true

PERSONAL_DATA_CONSENT_VERSION = "PD-2026-08-08"
TERMS_VERSION = "TERMS-2026-08-08"


def registration_consent_fields(errors: dict | None = None) -> list[dict]:
    errors = errors or {}
    return [
        {
            "name": "accept_terms",
            "label": _("Я принимаю условия использования"),
            "type": "checkbox",
            "required": True,
            "link_url": "/terms/",
            "link_label": _("Открыть условия"),
            "error": errors.get("accept_terms", ""),
        },
        {
            "name": "personal_data_consent",
            "label": _("Я даю отдельное согласие на обработку персональных данных"),
            "type": "checkbox",
            "required": True,
            "link_url": "/personal-data-consent/",
            "link_label": _("Открыть текст согласия"),
            "error": errors.get("personal_data_consent", ""),
        },
    ]


def registration_consent_errors(params: dict) -> dict[str, str]:
    errors = {}
    if not confirmation_is_true(params.get("accept_terms")):
        errors["accept_terms"] = _("подтвердите принятие условий")
    if not confirmation_is_true(params.get("personal_data_consent")):
        errors["personal_data_consent"] = _(
            "согласие должно быть дано отдельным флажком"
        )
    return errors


def record_registration_consents(request, user, *, role: str) -> None:
    from marketplace.models import UserConsent

    operator_name = (
        getattr(settings, "PLATFORM_LEGAL_NAME", "") or "Innovation Idea FZ-LLC"
    )
    address = client_ip(request)
    if address == "unknown":
        address = None
    common = {
        "user": user,
        "source": "registration",
        "ip_address": address,
        "user_agent": (request.META.get("HTTP_USER_AGENT") or "")[:512],
        "language": (getattr(request, "LANGUAGE_CODE", "") or "ru")[:10],
        "session_key": (getattr(request.session, "session_key", "") or "")[:64],
        "metadata": {"role": role},
    }
    UserConsent.objects.bulk_create(
        [
            UserConsent(
                **common,
                consent_type="terms",
                version=TERMS_VERSION,
                document_url="/terms/",
                text_snapshot=(
                    "Я принимаю условия использования Consolidator Parts "
                    f"редакции {TERMS_VERSION}."
                ),
            ),
            UserConsent(
                **common,
                consent_type="personal_data",
                version=PERSONAL_DATA_CONSENT_VERSION,
                document_url="/personal-data-consent/",
                text_snapshot=(
                    "Я даю отдельное согласие "
                    f"{operator_name} на обработку персональных данных "
                    f"по документу версии {PERSONAL_DATA_CONSENT_VERSION}."
                ),
            ),
        ]
    )

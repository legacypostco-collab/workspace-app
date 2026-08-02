"""Public, stable and non-identifying labels for marketplace participants."""

import re

from django.utils.translation import gettext as _

from .models import participant_public_code


_EMAIL_RE = re.compile(r"\b[\w.%+-]+@[\w.-]+\.[A-Za-z]{2,}\b")
_URL_RE = re.compile(r"\b(?:https?://|www\.)\S+", re.IGNORECASE)
_HANDLE_RE = re.compile(r"(?<!\w)@[A-Za-z][A-Za-z0-9_]{3,30}\b")
_PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\s().-]*){7,15}(?!\w)")


def _profile(user):
    if not user or not getattr(user, "id", None):
        return None
    return getattr(user, "profile", None) or getattr(user, "userprofile", None)


def public_party_code(user, role: str, *, fallback_id: int | None = None) -> str:
    """Return the stored role-specific code, with a deterministic legacy fallback."""
    normalized_role = "seller" if role == "seller" else "buyer"
    profile = _profile(user)
    field = "partner_public_code" if normalized_role == "seller" else "customer_public_code"
    code = getattr(profile, field, None) if profile else None
    if code:
        return str(code)

    source_id = getattr(user, "id", None) or fallback_id
    if source_id:
        return participant_public_code(int(source_id), normalized_role)
    return "----"


def public_party_label(user, role: str, *, fallback_id: int | None = None) -> str:
    code = public_party_code(user, role, fallback_id=fallback_id)
    if role == "seller":
        return _("Партнёр CP · %(code)s") % {"code": code}
    return _("Заказчик CP · %(code)s") % {"code": code}


def partner_label(user, *, fallback_id: int | None = None) -> str:
    return public_party_label(user, "seller", fallback_id=fallback_id)


def customer_label(user, *, fallback_id: int | None = None) -> str:
    return public_party_label(user, "buyer", fallback_id=fallback_id)


def redact_party_contacts(value: str | None) -> str:
    """Remove direct contact channels from text shown to the opposite party."""
    text = str(value or "")
    for pattern in (_EMAIL_RE, _URL_RE, _HANDLE_RE, _PHONE_RE):
        text = pattern.sub(_("[контакт скрыт]"), text)
    return text


def redact_party_payload(value):
    """Recursively redact contact channels in JSON-compatible payloads."""
    if isinstance(value, dict):
        return {key: redact_party_payload(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_party_payload(item) for item in value]
    if isinstance(value, str):
        return redact_party_contacts(value)
    return value

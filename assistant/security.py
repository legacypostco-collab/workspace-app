from __future__ import annotations

import hmac
import ipaddress
import socket
from urllib.parse import urlparse

from django.conf import settings


def user_has_enabled_2fa(user) -> bool:
    if not user or not getattr(user, "is_authenticated", False):
        return False
    try:
        from marketplace.models import TwoFactorAuth

        return TwoFactorAuth.objects.filter(
            user=user,
            enabled=True,
            secret__gt="",
        ).exists()
    except Exception:
        return False


def verify_user_2fa(user, code: str) -> bool:
    code = (code or "").strip().replace(" ", "")
    if not code:
        return False
    try:
        import pyotp
        from marketplace.models import TwoFactorAuth

        twofa = TwoFactorAuth.objects.filter(
            user=user,
            enabled=True,
            secret__gt="",
        ).first()
        if not twofa:
            return True
        if pyotp.TOTP(twofa.secret).verify(code, valid_window=1):
            return True

        backup = [x.strip() for x in (twofa.backup_codes or "").split(",") if x.strip()]
        for item in backup:
            if hmac.compare_digest(item, code):
                backup.remove(item)
                twofa.backup_codes = ",".join(backup)
                twofa.save(update_fields=["backup_codes"])
                return True
    except Exception:
        return False
    return False


def token_has_permission(token, required: str) -> bool:
    if required == "read":
        return True
    permissions = {
        p.strip().lower()
        for p in (getattr(token, "permissions", "") or "").split(",")
        if p.strip()
    }
    if "admin" in permissions:
        return True
    if required == "write":
        return "write" in permissions
    return required in permissions


def safe_outbound_url(
    url: str,
    *,
    allowed_hosts_setting: str = "WEBHOOK_ALLOWED_HOSTS",
    allow_private_setting: str = "WEBHOOK_ALLOW_PRIVATE_IPS",
    allow_insecure_setting: str = "WEBHOOK_ALLOW_INSECURE_HTTP",
) -> tuple[bool, str]:
    """Reject SSRF-prone outbound HTTP targets."""
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in ("https", "http"):
        return False, "unsupported scheme"
    if parsed.scheme != "https" and not (
        settings.DEBUG and getattr(settings, allow_insecure_setting, False)
    ):
        return False, "https required"
    if not parsed.hostname:
        return False, "missing host"

    host = parsed.hostname.strip().lower().rstrip(".")
    if host in {"localhost", "localhost.localdomain"}:
        return False, "local host is not allowed"

    allowed_hosts = {
        h.strip().lower()
        for h in (getattr(settings, allowed_hosts_setting, "") or "").split(",")
        if h.strip()
    }
    if allowed_hosts and host not in allowed_hosts:
        return False, "host is not in allowlist"

    allow_private = bool(getattr(settings, allow_private_setting, False) and settings.DEBUG)
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False, "host does not resolve"

    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not allow_private and (
            ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_multicast or ip.is_reserved or ip.is_unspecified
        ):
            return False, "private or local address is not allowed"
    return True, ""

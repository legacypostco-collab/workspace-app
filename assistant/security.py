from __future__ import annotations

import hmac
import hashlib
import ipaddress
import socket
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, build_opener

from django.conf import settings
from django.db import transaction


_BACKUP_CODE_PREFIX = "hmac_sha256$"


def confirmation_is_true(value) -> bool:
    """Parse an untrusted confirmation flag without Python truthiness traps."""
    if value is True or value == 1:
        return True
    if value is False or value is None or value == 0:
        return False
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "да"}
    return False


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_NO_REDIRECT_OPENER = build_opener(_NoRedirectHandler)


def urlopen_no_redirect(request, *, timeout: float):
    """Open a prevalidated outbound request without following redirects."""
    return _NO_REDIRECT_OPENER.open(request, timeout=timeout)  # nosec B310


def client_ip(request) -> str:
    """Return a validated client IP, trusting forwarded values only from proxies."""
    raw_remote = (request.META.get("REMOTE_ADDR") or "").strip()
    try:
        remote = ipaddress.ip_address(raw_remote)
    except ValueError:
        return "unknown"

    trusted_networks = []
    for value in getattr(settings, "TRUSTED_PROXY_NETWORKS", []):
        try:
            trusted_networks.append(ipaddress.ip_network(value, strict=False))
        except ValueError:
            continue

    def is_trusted(address) -> bool:
        return any(address in network for network in trusted_networks)

    if not is_trusted(remote):
        return remote.compressed

    forwarded = (request.META.get("HTTP_X_FORWARDED_FOR") or "").split(",")
    chain = []
    for raw_address in forwarded:
        try:
            chain.append(ipaddress.ip_address(raw_address.strip()))
        except ValueError:
            continue
    chain.append(remote)

    for address in reversed(chain):
        if not is_trusted(address):
            return address.compressed
    return remote.compressed


def _backup_code_digest(user_id: int, code: str) -> str:
    payload = f"{user_id}:{(code or '').strip()}".encode("utf-8")
    digest = hmac.new(
        settings.SECRET_KEY.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()
    return _BACKUP_CODE_PREFIX + digest


def encode_backup_codes(user, codes) -> str:
    return ",".join(_backup_code_digest(user.pk, code) for code in codes)


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

        with transaction.atomic():
            locked = TwoFactorAuth.objects.select_for_update().get(pk=twofa.pk)
            backup = [
                x.strip()
                for x in (locked.backup_codes or "").split(",")
                if x.strip()
            ]
            normalized_backup = [
                item
                if item.startswith(_BACKUP_CODE_PREFIX)
                else _backup_code_digest(user.pk, item)
                for item in backup
            ]
            expected = _backup_code_digest(user.pk, code)
            for item in normalized_backup:
                if hmac.compare_digest(item, expected):
                    normalized_backup.remove(item)
                    locked.backup_codes = ",".join(normalized_backup)
                    locked.save(update_fields=["backup_codes"])
                    return True
            if normalized_backup != backup:
                locked.backup_codes = ",".join(normalized_backup)
                locked.save(update_fields=["backup_codes"])
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
    allow_query: bool = True,
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
    if parsed.username is not None or parsed.password is not None:
        return False, "credentials in URL are not allowed"
    if not allow_query and (parsed.query or parsed.fragment):
        return False, "query and fragment are not allowed"

    host = parsed.hostname.strip().lower().rstrip(".")
    if host in {"localhost", "localhost.localdomain"}:
        return False, "local host is not allowed"

    allowed_hosts = {
        h.strip().lower()
        for h in (getattr(settings, allowed_hosts_setting, "") or "").split(",")
        if h.strip()
    }
    if not allowed_hosts:
        return False, "outbound host allowlist is not configured"
    def host_matches(pattern: str) -> bool:
        if pattern.startswith("*.") and pattern.count(".") >= 2:
            suffix = pattern[1:]
            return host.endswith(suffix) and host != pattern[2:]
        return host == pattern

    if allowed_hosts and not any(host_matches(pattern) for pattern in allowed_hosts):
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

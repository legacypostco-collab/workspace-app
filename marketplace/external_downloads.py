from __future__ import annotations

from urllib.parse import urljoin
from urllib.request import Request

from django.conf import settings

from assistant.security import safe_outbound_url, urlopen_no_redirect


class ExternalDownloadError(ValueError):
    pass


_REDIRECT_STATUSES = {301, 302, 303, 307, 308}


def download_get_with_checked_redirects(
    url: str,
    *,
    allowed_hosts_setting: str,
    allow_private_setting: str,
    allow_insecure_setting: str,
    max_bytes: int,
    timeout: float = 20,
    max_redirects: int = 5,
) -> tuple[int, bytes, str]:
    """Download a GET response while validating every redirect target."""
    current_url = (url or "").strip()
    for redirect_count in range(max_redirects + 1):
        ok, reason = safe_outbound_url(
            current_url,
            allowed_hosts_setting=allowed_hosts_setting,
            allow_private_setting=allow_private_setting,
            allow_insecure_setting=allow_insecure_setting,
        )
        if not ok:
            raise ExternalDownloadError(f"blocked outbound URL: {reason}")

        request = Request(
            current_url,
            headers={"User-Agent": "ConsolidatorParts/1.0"},
        )
        try:
            response = urlopen_no_redirect(
                request,
                timeout=timeout,
                allow_private=bool(getattr(settings, allow_private_setting, False)),
            )
        except (OSError, ValueError) as exc:
            raise ExternalDownloadError("request failed") from exc
        try:
            if response.status in _REDIRECT_STATUSES:
                if redirect_count >= max_redirects:
                    raise ExternalDownloadError("too many redirects")
                location = (response.headers.get("Location") or "").strip()
                if not location:
                    raise ExternalDownloadError("redirect without location")
                current_url = urljoin(current_url, location)
                continue

            if response.status != 200:
                return response.status, b"", current_url

            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    declared_size = int(content_length)
                except ValueError as exc:
                    raise ExternalDownloadError("invalid content length") from exc
                if declared_size > max_bytes:
                    raise ExternalDownloadError("response is too large")

            body = bytearray()
            try:
                while True:
                    chunk = response.read(min(64 * 1024, max_bytes + 1 - len(body)))
                    if not chunk:
                        break
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        raise ExternalDownloadError("response is too large")
            except OSError as exc:
                raise ExternalDownloadError("response read failed") from exc
            return response.status, bytes(body), current_url
        finally:
            response.close()

    raise ExternalDownloadError("too many redirects")

from __future__ import annotations

from urllib.parse import urljoin

import requests

from assistant.security import safe_outbound_url


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

        try:
            response = requests.get(
                current_url,
                headers={"User-Agent": "ConsolidatorParts/1.0"},
                timeout=timeout,
                allow_redirects=False,
                stream=True,
            )
        except requests.RequestException as exc:
            raise ExternalDownloadError("request failed") from exc
        try:
            if response.status_code in _REDIRECT_STATUSES:
                if redirect_count >= max_redirects:
                    raise ExternalDownloadError("too many redirects")
                location = (response.headers.get("Location") or "").strip()
                if not location:
                    raise ExternalDownloadError("redirect without location")
                current_url = urljoin(current_url, location)
                continue

            if response.status_code != 200:
                return response.status_code, b"", current_url

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
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        raise ExternalDownloadError("response is too large")
            except requests.RequestException as exc:
                raise ExternalDownloadError("response read failed") from exc
            return response.status_code, bytes(body), current_url
        finally:
            response.close()

    raise ExternalDownloadError("too many redirects")

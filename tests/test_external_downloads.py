from unittest.mock import Mock, patch

import pytest
from django.test import override_settings

from assistant.security import safe_outbound_url
from marketplace.external_downloads import (
    ExternalDownloadError,
    download_get_with_checked_redirects,
)


@override_settings(DEBUG=True, WEBHOOK_ALLOWED_HOSTS="")
def test_debug_mode_still_requires_outbound_allowlist():
    allowed, reason = safe_outbound_url("https://example.com/webhook")

    assert allowed is False
    assert reason == "outbound host allowlist is not configured"


@override_settings(
    DEBUG=False,
    WEBHOOK_ALLOWED_HOSTS="hooks.example.com",
)
@patch(
    "assistant.security.socket.getaddrinfo",
    return_value=[(2, 1, 6, "", ("93.184.216.34", 443))],
)
def test_webhook_url_rejects_query_secrets(_dns):
    allowed, reason = safe_outbound_url(
        "https://hooks.example.com/events?secret=leak",
        allow_query=False,
    )

    assert allowed is False
    assert reason == "query and fragment are not allowed"


def test_webhook_log_display_hides_path_credentials_and_query():
    from marketplace.models import WebhookDeliveryLog

    log = WebhookDeliveryLog(
        endpoint=(
            "https://user:password@hooks.example.com:8443/"
            "private/token?signature=secret"
        )
    )

    assert log.safe_endpoint == "https://hooks.example.com:8443"
    assert "password" not in str(log)
    assert "private" not in str(log)
    assert "signature" not in str(log)


def _response(status, *, location="", body=b"", content_length=None):
    response = Mock()
    response.status_code = status
    response.headers = {}
    if location:
        response.headers["Location"] = location
    if content_length is not None:
        response.headers["Content-Length"] = str(content_length)
    response.iter_content.return_value = [body] if body else []
    return response


@override_settings(
    DEBUG=False,
    GOOGLE_SHEETS_ALLOWED_HOSTS="docs.google.com,*.googleusercontent.com",
)
@patch(
    "assistant.security.socket.getaddrinfo",
    return_value=[(2, 1, 6, "", ("93.184.216.34", 443))],
)
def test_outbound_allowlist_supports_only_explicit_subdomain_wildcards(_dns):
    allowed, _ = safe_outbound_url(
        "https://download.googleusercontent.com/file.csv",
        allowed_hosts_setting="GOOGLE_SHEETS_ALLOWED_HOSTS",
    )
    base_domain, _ = safe_outbound_url(
        "https://googleusercontent.com/file.csv",
        allowed_hosts_setting="GOOGLE_SHEETS_ALLOWED_HOSTS",
    )
    suffix_trick, _ = safe_outbound_url(
        "https://googleusercontent.com.evil.example/file.csv",
        allowed_hosts_setting="GOOGLE_SHEETS_ALLOWED_HOSTS",
    )

    assert allowed is True
    assert base_domain is False
    assert suffix_trick is False


@patch("marketplace.external_downloads.requests.get")
@patch("marketplace.external_downloads.safe_outbound_url")
def test_redirect_to_blocked_internal_address_is_never_requested(safe_url, get):
    safe_url.side_effect = [(True, ""), (False, "private address")]
    get.return_value = _response(302, location="http://127.0.0.1/admin")

    with pytest.raises(ExternalDownloadError, match="blocked outbound URL"):
        download_get_with_checked_redirects(
            "https://docs.google.com/spreadsheets/d/test/export",
            allowed_hosts_setting="GOOGLE_SHEETS_ALLOWED_HOSTS",
            allow_private_setting="GOOGLE_SHEETS_ALLOW_PRIVATE_IPS",
            allow_insecure_setting="GOOGLE_SHEETS_ALLOW_INSECURE_HTTP",
            max_bytes=1024,
        )

    get.assert_called_once()


@patch("marketplace.external_downloads.requests.get")
@patch("marketplace.external_downloads.safe_outbound_url", return_value=(True, ""))
def test_allowed_redirect_is_downloaded_with_size_limit(_safe_url, get):
    get.side_effect = [
        _response(
            302,
            location="https://download.googleusercontent.com/table.csv",
        ),
        _response(200, body=b"OEM,price\nA1,10\n"),
    ]

    status, body, final_url = download_get_with_checked_redirects(
        "https://docs.google.com/spreadsheets/d/test/export",
        allowed_hosts_setting="GOOGLE_SHEETS_ALLOWED_HOSTS",
        allow_private_setting="GOOGLE_SHEETS_ALLOW_PRIVATE_IPS",
        allow_insecure_setting="GOOGLE_SHEETS_ALLOW_INSECURE_HTTP",
        max_bytes=1024,
    )

    assert status == 200
    assert body == b"OEM,price\nA1,10\n"
    assert final_url == "https://download.googleusercontent.com/table.csv"
    assert get.call_count == 2
    assert all(call.kwargs["allow_redirects"] is False for call in get.call_args_list)


@patch("marketplace.external_downloads.requests.get")
@patch("marketplace.external_downloads.safe_outbound_url", return_value=(True, ""))
def test_declared_oversized_response_is_rejected(_safe_url, get):
    get.return_value = _response(200, content_length=2048)

    with pytest.raises(ExternalDownloadError, match="too large"):
        download_get_with_checked_redirects(
            "https://docs.google.com/spreadsheets/d/test/export",
            allowed_hosts_setting="GOOGLE_SHEETS_ALLOWED_HOSTS",
            allow_private_setting="GOOGLE_SHEETS_ALLOW_PRIVATE_IPS",
            allow_insecure_setting="GOOGLE_SHEETS_ALLOW_INSECURE_HTTP",
            max_bytes=1024,
        )

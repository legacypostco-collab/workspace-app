from unittest.mock import Mock, patch

import pytest
from django.test import override_settings

from assistant.security import safe_outbound_url, urlopen_no_redirect
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
    response.status = status
    response.headers = {}
    if location:
        response.headers["Location"] = location
    if content_length is not None:
        response.headers["Content-Length"] = str(content_length)
    response.read.side_effect = [body, b""] if body else [b""]
    return response


@patch("assistant.security._PinnedHTTPSConnection")
@patch(
    "assistant.security.socket.getaddrinfo",
    return_value=[(2, 1, 6, "", ("93.184.216.34", 443))],
)
def test_outbound_connection_uses_the_validated_ip(_dns, connection_class):
    connection = Mock()
    connection.getresponse.return_value = _response(200, body=b"ok")
    connection_class.return_value = connection

    response = urlopen_no_redirect(
        "https://example.com/resource",
        timeout=5,
    )

    connection_class.assert_called_once_with(
        "example.com",
        443,
        "93.184.216.34",
        timeout=5,
    )
    connection.request.assert_called_once_with(
        "GET",
        "/resource",
        body=None,
        headers={"Host": "example.com"},
    )
    response.close()


@patch(
    "assistant.security.socket.getaddrinfo",
    return_value=[(2, 1, 6, "", ("127.0.0.1", 443))],
)
def test_pinned_transport_rejects_private_dns_result(_dns):
    with pytest.raises(ValueError, match="private or local"):
        urlopen_no_redirect("https://example.com/resource", timeout=5)


@patch(
    "assistant.security.socket.getaddrinfo",
    return_value=[(2, 1, 6, "", ("100.64.0.1", 443))],
)
def test_pinned_transport_rejects_shared_carrier_network(_dns):
    with pytest.raises(ValueError, match="private or local"):
        urlopen_no_redirect("https://example.com/resource", timeout=5)


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


@patch("marketplace.external_downloads.urlopen_no_redirect")
@patch("marketplace.external_downloads.safe_outbound_url")
def test_redirect_to_blocked_internal_address_is_never_requested(safe_url, open_url):
    safe_url.side_effect = [(True, ""), (False, "private address")]
    open_url.return_value = _response(302, location="http://127.0.0.1/admin")

    with pytest.raises(ExternalDownloadError, match="blocked outbound URL"):
        download_get_with_checked_redirects(
            "https://docs.google.com/spreadsheets/d/test/export",
            allowed_hosts_setting="GOOGLE_SHEETS_ALLOWED_HOSTS",
            allow_private_setting="GOOGLE_SHEETS_ALLOW_PRIVATE_IPS",
            allow_insecure_setting="GOOGLE_SHEETS_ALLOW_INSECURE_HTTP",
            max_bytes=1024,
        )

    open_url.assert_called_once()


@patch("marketplace.external_downloads.urlopen_no_redirect")
@patch("marketplace.external_downloads.safe_outbound_url", return_value=(True, ""))
def test_allowed_redirect_is_downloaded_with_size_limit(_safe_url, open_url):
    open_url.side_effect = [
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
    assert open_url.call_count == 2


@patch("marketplace.external_downloads.urlopen_no_redirect")
@patch("marketplace.external_downloads.safe_outbound_url", return_value=(True, ""))
def test_declared_oversized_response_is_rejected(_safe_url, open_url):
    open_url.return_value = _response(200, content_length=2048)

    with pytest.raises(ExternalDownloadError, match="too large"):
        download_get_with_checked_redirects(
            "https://docs.google.com/spreadsheets/d/test/export",
            allowed_hosts_setting="GOOGLE_SHEETS_ALLOWED_HOSTS",
            allow_private_setting="GOOGLE_SHEETS_ALLOW_PRIVATE_IPS",
            allow_insecure_setting="GOOGLE_SHEETS_ALLOW_INSECURE_HTTP",
            max_bytes=1024,
        )

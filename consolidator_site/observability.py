from __future__ import annotations

import re
from collections.abc import Mapping


_SENSITIVE_KEY = re.compile(
    r"(authorization|cookie|password|passwd|secret|token|api[_-]?key|session|otp|totp)",
    re.IGNORECASE,
)
_REDACTED = "[Filtered]"


def _scrub(value, *, key: str = "", depth: int = 0):
    if _SENSITIVE_KEY.search(key):
        return _REDACTED
    if depth >= 8:
        return value
    if isinstance(value, Mapping):
        return {
            str(child_key): _scrub(
                child_value,
                key=str(child_key),
                depth=depth + 1,
            )
            for child_key, child_value in value.items()
        }
    if isinstance(value, list):
        return [_scrub(item, depth=depth + 1) for item in value]
    if isinstance(value, tuple):
        return tuple(_scrub(item, depth=depth + 1) for item in value)
    return value


def scrub_sentry_event(event, hint):
    """Remove authentication and secret values before an event leaves the host."""
    scrubbed = _scrub(event)
    if not isinstance(scrubbed, dict):
        return scrubbed
    scrubbed.pop("user", None)
    request = scrubbed.get("request")
    if isinstance(request, dict):
        for field in ("cookies", "data", "env", "query_string"):
            if field in request:
                request[field] = _REDACTED
    return scrubbed

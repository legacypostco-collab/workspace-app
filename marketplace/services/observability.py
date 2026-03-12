from __future__ import annotations

import logging
from time import monotonic

from django.core.cache import cache

logger = logging.getLogger("marketplace")


def metric_inc(name: str, value: int = 1) -> int:
    key = f"metric:{name}"
    try:
        cache.add(key, 0, timeout=None)
        return cache.incr(key, value)
    except Exception:
        current = int(cache.get(key, 0) or 0) + value
        cache.set(key, current, timeout=None)
        return current


def metric_get(name: str) -> int:
    return int(cache.get(f"metric:{name}", 0) or 0)


def log_api_error(endpoint: str, status: int, code: str, extra: dict | None = None) -> None:
    logger.warning(
        "api_error",
        extra={
            "endpoint": endpoint,
            "status": status,
            "error_code": code,
            **(extra or {}),
        },
    )
    metric_inc("api_errors_total")


class Timer:
    def __init__(self):
        self._started_at = monotonic()

    def elapsed_ms(self) -> int:
        return int((monotonic() - self._started_at) * 1000)

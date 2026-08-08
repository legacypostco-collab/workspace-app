from __future__ import annotations

import logging
import threading
import time
from time import monotonic

from django.core.cache import cache

logger = logging.getLogger("marketplace")

HTTP_BUCKET_TTL_SECONDS = 15 * 60
HTTP_SLOW_REQUEST_MS = 3_000
METRICS_WARNING_INTERVAL_SECONDS = 60

_warning_lock = threading.Lock()
_last_warning_at = 0.0


def _warn_cache_unavailable(operation: str) -> None:
    """Log at most once a minute: telemetry must not flood application logs."""
    global _last_warning_at
    now = monotonic()
    with _warning_lock:
        if now - _last_warning_at < METRICS_WARNING_INTERVAL_SECONDS:
            return
        _last_warning_at = now
    logger.warning(
        "observability_cache_unavailable",
        extra={"operation": operation},
        exc_info=True,
    )


def metric_inc(name: str, value: int = 1) -> int:
    key = f"metric:{name}"
    try:
        cache.add(key, 0, timeout=None)
        return cache.incr(key, value)
    except Exception:
        try:
            current = int(cache.get(key, 0) or 0) + value
            cache.set(key, current, timeout=None)
            return current
        except Exception:
            _warn_cache_unavailable("increment")
            return value


def metric_get(name: str) -> int:
    try:
        return int(cache.get(f"metric:{name}", 0) or 0)
    except Exception:
        _warn_cache_unavailable("read")
        return 0


def _http_bucket(minute: int | None = None) -> int:
    return int(minute if minute is not None else time.time() // 60)


def _bucket_inc(bucket: int, name: str, value: int = 1) -> None:
    key = f"operations:http:{bucket}:{name}"
    try:
        cache.add(key, 0, timeout=HTTP_BUCKET_TTL_SECONDS)
        cache.incr(key, value)
        cache.touch(key, HTTP_BUCKET_TTL_SECONDS)
    except Exception:
        try:
            current = int(cache.get(key, 0) or 0) + value
            cache.set(key, current, timeout=HTTP_BUCKET_TTL_SECONDS)
        except Exception:
            _warn_cache_unavailable("bucket_increment")


def record_http_request(status: int, elapsed_ms: int) -> None:
    """Record aggregate request health without URLs, users, or request bodies."""
    bucket = _http_bucket()
    metric_inc("http_requests_total")
    _bucket_inc(bucket, "total")

    status_class = max(0, min(9, int(status) // 100))
    metric_inc(f"http_responses_{status_class}xx_total")
    _bucket_inc(bucket, f"status_{status_class}xx")
    if int(elapsed_ms) >= HTTP_SLOW_REQUEST_MS:
        metric_inc("http_slow_requests_total")
        _bucket_inc(bucket, "slow")


def http_window(minutes: int = 5, *, now_minute: int | None = None) -> dict[str, int | float]:
    window = max(1, min(int(minutes), 15))
    current = _http_bucket(now_minute)
    totals = {"total": 0, "status_4xx": 0, "status_5xx": 0, "slow": 0}
    try:
        for bucket in range(current - window + 1, current + 1):
            for name in totals:
                totals[name] += int(
                    cache.get(f"operations:http:{bucket}:{name}", 0) or 0
                )
    except Exception:
        _warn_cache_unavailable("window_read")
        totals = {"total": 0, "status_4xx": 0, "status_5xx": 0, "slow": 0}
    total = totals["total"]
    totals["error_rate"] = totals["status_5xx"] / total if total else 0.0
    totals["slow_rate"] = totals["slow"] / total if total else 0.0
    return totals


class OperationsMetricsMiddleware:
    """Low-cardinality request metrics suitable for Redis and health checks."""

    _EXCLUDED_PATHS = {"/healthz/", "/readyz/", "/metrics/"}

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        started_at = monotonic()
        response = self.get_response(request)
        if request.path not in self._EXCLUDED_PATHS:
            elapsed_ms = int((monotonic() - started_at) * 1_000)
            try:
                record_http_request(getattr(response, "status_code", 500), elapsed_ms)
            except Exception:
                _warn_cache_unavailable("middleware")
        return response


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

"""Healthcheck endpoint for load balancers / orchestrators (k8s, Yandex MK,
nginx upstream health). Lightweight probes — should respond <50ms.

Endpoints:
  GET /healthz/      → 200 если приложение запустилось (liveness)
  GET /readyz/       → 200 только если DB + Redis работают (readiness)

Не требует аутентификации. Не логирует (slim 200).
"""
from django.db import connection
from django.http import HttpResponse, JsonResponse
from django.conf import settings
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET


@never_cache
@require_GET
def liveness(request):
    """Liveness — приложение в принципе живо (не нужны внешние зависимости).
    Используется для k8s livenessProbe."""
    return JsonResponse({"ok": True, "status": "alive"})


@never_cache
@require_GET
def readiness(request):
    """Readiness — готовы принимать трафик (DB + cache доступны).
    Используется для k8s readinessProbe + nginx upstream."""
    checks = {}
    # 1. DB roundtrip
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        checks["db"] = True
    except Exception as e:
        checks["db"] = False
        checks["db_error"] = str(e)[:100]
    # 2. Cache roundtrip
    try:
        from django.core.cache import cache
        cache.set("__healthz__", "1", 5)
        checks["cache"] = cache.get("__healthz__") == "1"
    except Exception as e:
        checks["cache"] = False
        checks["cache_error"] = str(e)[:100]
    ok = bool(checks.get("db")) and bool(checks.get("cache", True))
    status = 200 if ok else 503
    token = (getattr(settings, "HEALTHCHECK_TOKEN", "") or "").strip()
    provided = (request.headers.get("X-Healthcheck-Token") or "").strip()
    if token and provided == token:
        return JsonResponse({"ok": ok, **checks}, status=status)
    return JsonResponse({"ok": ok, "status": "ready" if ok else "unavailable"}, status=status)


def _metric_line(name: str, value, labels: dict[str, str] | None = None) -> str:
    suffix = ""
    if labels:
        rendered = ",".join(
            f'{key}="{str(label).replace(chr(34), chr(92) + chr(34))}"'
            for key, label in sorted(labels.items())
        )
        suffix = "{" + rendered + "}"
    return f"{name}{suffix} {value}"


@never_cache
@require_GET
def metrics(request):
    """Small Prometheus-compatible endpoint protected by HEALTHCHECK_TOKEN."""
    token = (getattr(settings, "HEALTHCHECK_TOKEN", "") or "").strip()
    provided = (request.headers.get("X-Healthcheck-Token") or "").strip()
    if not token or provided != token:
        return HttpResponse(status=404)

    from django.core.cache import cache
    from django.utils import timezone
    from marketplace.services.observability import http_window, metric_get

    window = http_window(5)
    heartbeat = cache.get("operations:celery_heartbeat")
    heartbeat_age = -1
    if heartbeat:
        heartbeat_age = max(0, int(timezone.now().timestamp() - float(heartbeat)))

    failed_tasks = 0
    try:
        from django_celery_results.models import TaskResult

        failed_tasks = TaskResult.objects.filter(
            status="FAILURE",
            date_done__gte=timezone.now() - timezone.timedelta(hours=24),
        ).count()
    except Exception:
        failed_tasks = -1

    lines = [
        "# TYPE consolidator_up gauge",
        _metric_line("consolidator_up", 1),
        "# TYPE consolidator_http_requests_total counter",
        _metric_line(
            "consolidator_http_requests_total",
            metric_get("http_requests_total"),
        ),
        "# TYPE consolidator_http_responses_total counter",
        _metric_line(
            "consolidator_http_responses_total",
            metric_get("http_responses_4xx_total"),
            {"class": "4xx"},
        ),
        _metric_line(
            "consolidator_http_responses_total",
            metric_get("http_responses_5xx_total"),
            {"class": "5xx"},
        ),
        "# TYPE consolidator_http_window_requests gauge",
        _metric_line("consolidator_http_window_requests", window["total"]),
        _metric_line(
            "consolidator_http_window_requests",
            window["status_5xx"],
            {"class": "5xx"},
        ),
        "# TYPE consolidator_http_window_error_ratio gauge",
        _metric_line(
            "consolidator_http_window_error_ratio",
            f'{window["error_rate"]:.6f}',
        ),
        "# TYPE consolidator_celery_heartbeat_age_seconds gauge",
        _metric_line("consolidator_celery_heartbeat_age_seconds", heartbeat_age),
        "# TYPE consolidator_celery_failed_tasks_24h gauge",
        _metric_line("consolidator_celery_failed_tasks_24h", failed_tasks),
    ]
    return HttpResponse(
        "\n".join(lines) + "\n",
        content_type="text/plain; version=0.0.4; charset=utf-8",
    )

"""Healthcheck endpoint for load balancers / orchestrators (k8s, Yandex MK,
nginx upstream health). Lightweight probes — should respond <50ms.

Endpoints:
  GET /healthz/      → 200 если приложение запустилось (liveness)
  GET /readyz/       → 200 только если DB + Redis работают (readiness)

Не требует аутентификации. Не логирует (slim 200).
"""
from django.db import connection
from django.http import JsonResponse
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
    return JsonResponse({"ok": ok, **checks}, status=status)

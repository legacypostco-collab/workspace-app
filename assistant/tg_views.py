"""Webhook endpoint для Telegram bot.

Production-flow:
  1. Установить webhook у Telegram:
     curl -F "url=https://<host>/api/tg/webhook/<SECRET>/" \\
          https://api.telegram.org/bot<TOKEN>/setWebhook
  2. Telegram POSTит сюда каждый update
  3. Мы валидируем secret (защита от подделки) и диспатчим в handle_update

SECRET = settings.TELEGRAM_WEBHOOK_SECRET — длинная random-строка из env.
Без неё endpoint возвращает 404 (защита от случайного скана URL'ов).
"""
import json
import logging

from django.conf import settings
from django.http import HttpResponse, HttpResponseNotFound, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .tg_bot import handle_update

logger = logging.getLogger(__name__)


@csrf_exempt
@require_POST
def telegram_webhook(request, secret: str):
    """POST /api/tg/webhook/<secret>/  — приём update'ов от Telegram."""
    expected = getattr(settings, "TELEGRAM_WEBHOOK_SECRET", "").strip()
    if not expected or secret != expected:
        logger.warning("tg_webhook: bad secret prefix=%s",
                        (secret or "")[:8])
        return HttpResponseNotFound()
    try:
        update = json.loads(request.body.decode("utf-8"))
    except Exception:
        return HttpResponse(status=400)
    try:
        handle_update(update)
    except Exception:
        logger.exception("tg_webhook: handler raised")
    return JsonResponse({"ok": True})

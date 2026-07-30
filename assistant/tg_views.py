"""Webhook endpoint для Telegram bot.

Production-flow:
  1. Установить webhook у Telegram:
     curl -F "url=https://<host>/api/assistant/tg/webhook/" \\
          -F "secret_token=<SECRET>" \\
          https://api.telegram.org/bot<TOKEN>/setWebhook
  2. Telegram POSTит сюда каждый update
  3. Мы валидируем secret (защита от подделки) и диспатчим в handle_update

SECRET = settings.TELEGRAM_WEBHOOK_SECRET — длинная random-строка из env,
передаваемая Telegram в заголовке X-Telegram-Bot-Api-Secret-Token.
Без неё endpoint возвращает 404 (защита от случайного скана URL'ов).
"""
import hmac
import json
import logging

from django.conf import settings
from django.core.exceptions import RequestDataTooBig
from django.http import HttpResponse, HttpResponseNotFound, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .tg_bot import handle_update

logger = logging.getLogger(__name__)
MAX_TELEGRAM_WEBHOOK_BYTES = 512 * 1024


@csrf_exempt
@require_POST
def telegram_webhook(request):
    """POST /api/assistant/tg/webhook/ — приём подтверждённых обновлений."""
    expected = getattr(settings, "TELEGRAM_WEBHOOK_SECRET", "").strip()
    provided = (
        request.headers.get("X-Telegram-Bot-Api-Secret-Token") or ""
    ).strip()
    if not expected or not hmac.compare_digest(provided, expected):
        logger.warning("tg_webhook: rejected request")
        return HttpResponseNotFound()
    try:
        content_length = int(request.META.get("CONTENT_LENGTH") or 0)
    except (TypeError, ValueError):
        content_length = 0
    if content_length > MAX_TELEGRAM_WEBHOOK_BYTES:
        return HttpResponse(status=413)
    try:
        raw_body = request.body
    except RequestDataTooBig:
        return HttpResponse(status=413)
    if len(raw_body) > MAX_TELEGRAM_WEBHOOK_BYTES:
        return HttpResponse(status=413)
    try:
        update = json.loads(raw_body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return HttpResponse(status=400)
    if not isinstance(update, dict):
        return HttpResponse(status=400)
    try:
        handle_update(update)
    except Exception:
        logger.exception("tg_webhook: handler raised")
    return JsonResponse({"ok": True})

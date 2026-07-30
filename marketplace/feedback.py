"""Beta-test feedback endpoint.

POST /api/feedback/ {"text": "...", "url": "...", "screenshot_b64": "..." (опц)}
  → создаёт BugReport, нотифицирует операторов через _notify (kind=system).
"""
from __future__ import annotations

import json
import logging

from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_POST

logger = logging.getLogger(__name__)

MAX_TEXT_LEN = 2000
MAX_URL_LEN = 500


@login_required
@require_POST
def submit_feedback(request):
    """Принимает фидбэк от beta-тестера. Без лимита (по 1 на пользователя достаточно)."""
    try:
        payload = json.loads(request.body or "{}")
    except Exception:
        return JsonResponse({"ok": False, "error": "invalid json"}, status=400)

    text = (payload.get("text") or "").strip()[:MAX_TEXT_LEN]
    page_url = (payload.get("url") or "").strip()[:MAX_URL_LEN]
    user_agent = request.META.get("HTTP_USER_AGENT", "")[:300]
    if not text:
        return JsonResponse({"ok": False, "error": "text is required"}, status=400)

    user = request.user
    rate_key = f"feedback:user:{user.pk}"
    if cache.add(rate_key, 1, 24 * 60 * 60):
        feedback_count = 1
    else:
        try:
            feedback_count = cache.incr(rate_key)
        except ValueError:
            cache.set(rate_key, 1, 24 * 60 * 60)
            feedback_count = 1
    if feedback_count > 20:
        return JsonResponse(
            {"ok": False, "error": "rate_limit_exceeded"},
            status=429,
        )

    logger.info(
        "beta_feedback user_id=%s text_length=%s",
        user.pk,
        len(text),
    )

    # Сохраняем как Conversation+Message — оператор увидит в чате
    # вместе со всеми остальными разговорами.
    try:
        from assistant.models import Conversation, Message
        from assistant.order_events import _notify, _operator_users

        conv = Conversation.objects.create(
            user=user,
            role="buyer",  # неважно — это feedback от любой роли
            category="general",
            title=f"🐞 Бета-фидбэк от {user.username}",
            is_active=True,
        )
        body = (
            f"**🐞 Фидбэк от beta-тестера**\n\n"
            f"**Тестер:** {user.username} ({getattr(user.profile, 'role', '—')})\n"
            f"**Страница:** {page_url or '—'}\n"
            f"**UA:** {user_agent[:120]}\n"
            f"**Время:** {timezone.now():%Y-%m-%d %H:%M:%S}\n\n"
            f"───\n\n{text}"
        )
        Message.objects.create(
            conversation=conv,
            role=Message.Role.USER,
            content=body,
        )

        # Notify operators
        try:
            for op in _operator_users():
                _notify(op, kind="system",
                        title=f"🐞 Beta фидбэк от {user.username}",
                        body=text[:200],
                        url=f"/chat/?conv={conv.id}")
        except Exception:
            logger.exception("notify operators failed (non-fatal)")

        return JsonResponse({"ok": True, "id": str(conv.id),
                              "thanks": "Спасибо! Мы получили фидбэк."})
    except Exception:
        logger.exception("save feedback failed")
        return JsonResponse({"ok": False, "error": "save_failed"}, status=500)

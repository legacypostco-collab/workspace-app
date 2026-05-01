"""AI-чат endpoint: принимает историю сообщений и возвращает SSE-поток."""
from __future__ import annotations

import json

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse, StreamingHttpResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .services import ai_assistant
from .views import _role_for


def _normalize_messages(raw: object) -> list[dict]:
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw[-20:]:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")
        if role not in {"user", "assistant"} or not isinstance(content, str):
            continue
        content = content.strip()
        if not content:
            continue
        out.append({"role": role, "content": content[:4000]})
    if out and out[0]["role"] != "user":
        out = out[1:]
    return out


@login_required
@require_POST
@csrf_exempt
def ai_chat(request):
    """POST { messages: [{role, content}, ...] } → SSE stream of text chunks.

    csrf_exempt — внутри запрос идёт с заголовком X-Requested-With и cookies,
    но для упрощения интеграции с fetch без CSRF-токена в JS отключаем проверку.
    Эндпойнт всё равно требует авторизации.
    """
    try:
        body = json.loads(request.body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "invalid json"}, status=400)

    messages = _normalize_messages(body.get("messages"))
    if not messages:
        return JsonResponse({"error": "messages required"}, status=400)

    role = _role_for(request.user) or "default"
    if role not in ai_assistant.SYSTEM_PROMPTS:
        role = "default"

    def event_stream():
        try:
            for chunk in ai_assistant.chat_stream(messages, role=role):
                if not chunk:
                    continue
                payload = json.dumps({"delta": chunk}, ensure_ascii=False)
                yield f"data: {payload}\n\n"
            yield "data: {\"done\": true}\n\n"
        except Exception as exc:  # pragma: no cover
            payload = json.dumps({"error": str(exc)}, ensure_ascii=False)
            yield f"data: {payload}\n\n"

    response = StreamingHttpResponse(event_stream(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response


@login_required
def ai_status(request):
    """Проверить, настроен ли AI."""
    return JsonResponse(
        {
            "configured": ai_assistant.is_configured(),
            "role": _role_for(request.user) or "default",
            "model": ai_assistant.DEFAULT_MODEL,
        }
    )

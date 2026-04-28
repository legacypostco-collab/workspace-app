"""RAG pipeline — main logic for AI assistant queries.

Two flavors:
- process_query_sync: blocking, returns (text, refs) — for REST API
- process_query_stream: generator yielding tokens — for WebSocket streaming
"""
from __future__ import annotations

import logging
import os

from django.conf import settings

from .embeddings import get_embedding, search_similar_chunks
from .models import Conversation, Message
from .prompts import get_system_prompt

logger = logging.getLogger(__name__)

MAX_HISTORY_MESSAGES = 20
MAX_CONTEXT_CHUNKS = 5
MIN_SIMILARITY_SCORE = 0.6
MAX_RESPONSE_TOKENS = 2048
DEFAULT_MODEL = "claude-sonnet-4-20250514"


def _detect_language(text: str) -> str:
    """Simple language detection by character ranges."""
    if not text:
        return "ru"
    if any("一" <= c <= "鿿" for c in text):
        return "zh"
    if any("Ѐ" <= c <= "ӿ" for c in text):
        return "ru"
    return "en"


def _get_history(conversation: Conversation) -> list[dict]:
    """Last N user/assistant messages in Claude API format."""
    msgs = list(
        conversation.messages
        .filter(role__in=["user", "assistant"])
        .order_by("-created_at")[:MAX_HISTORY_MESSAGES]
        .values("role", "content")
    )
    msgs.reverse()
    return msgs


def _search_context(query: str, role: str, language: str = None):
    """Embed query + hybrid vector+keyword search."""
    try:
        embedding = get_embedding(query)
    except Exception as e:
        logger.warning(f"Embedding failed: {e}")
        embedding = [0.0] * 1536
    return search_similar_chunks(
        embedding=embedding,
        role=role,
        language=language,
        limit=MAX_CONTEXT_CHUNKS,
        min_score=MIN_SIMILARITY_SCORE,
        query_text=query,
    )


def _build_context_refs(chunks):
    return [{
        "type": c.source_type,
        "id": str(c.source_id),
        "title": c.title,
        "score": getattr(c, "similarity_score", None),
    } for c in chunks]


def _get_anthropic_client():
    api_key = getattr(settings, "ANTHROPIC_API_KEY", "") or os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None
    try:
        import anthropic
        return anthropic.Anthropic(api_key=api_key)
    except ImportError:
        return None


def _stub_response(query: str, chunks) -> str:
    """Fallback when no Anthropic API key configured. Lists relevant chunks."""
    if not chunks:
        return (
            "ℹ️ AI ассистент недоступен (ANTHROPIC_API_KEY не настроен).\n\n"
            f"По вашему вопросу «{query}» — релевантного контекста не найдено."
        )
    parts = [
        f"ℹ️ AI ассистент работает в режиме без LLM (ANTHROPIC_API_KEY не настроен).",
        f"Найдено {len(chunks)} релевантных источников по запросу «{query}»:\n",
    ]
    for i, c in enumerate(chunks, 1):
        parts.append(f"{i}. **{c.title}** ({c.get_source_type_display()})")
        if c.content:
            parts.append(f"   {c.content[:200]}")
    return "\n".join(parts)


def process_query_sync(conversation: Conversation, user_message: str) -> tuple[str, list[dict]]:
    """Sync RAG pipeline. Returns (response_text, context_refs).

    Saves user + assistant messages to the conversation.
    """
    # 1. Save user message
    Message.objects.create(
        conversation=conversation,
        role=Message.Role.USER,
        content=user_message,
    )

    # 2. Search context + history
    language = _detect_language(user_message)
    context_chunks = _search_context(user_message, conversation.role, language)
    context_refs = _build_context_refs(context_chunks)
    system_prompt = get_system_prompt(conversation.role, context_chunks)
    history = _get_history(conversation)
    # history already includes the user message we just saved
    if history and history[-1]["role"] == "user" and history[-1]["content"] == user_message:
        history.pop()

    messages = history + [{"role": "user", "content": user_message}]

    # 3. Call Claude (or fallback to stub)
    client = _get_anthropic_client()
    full_response = ""
    tokens_used = 0

    if client:
        try:
            resp = client.messages.create(
                model=getattr(settings, "ANTHROPIC_MODEL", DEFAULT_MODEL),
                max_tokens=MAX_RESPONSE_TOKENS,
                system=system_prompt,
                messages=messages,
            )
            full_response = "".join(
                getattr(block, "text", "") for block in resp.content if hasattr(block, "text")
            )
            tokens_used = (resp.usage.input_tokens + resp.usage.output_tokens) if hasattr(resp, "usage") else 0
        except Exception as e:
            logger.error(f"Anthropic API error: {e}")
            full_response = f"⚠️ Ошибка API: {e}"
    else:
        full_response = _stub_response(user_message, context_chunks)

    # 4. Save assistant message
    Message.objects.create(
        conversation=conversation,
        role=Message.Role.ASSISTANT,
        content=full_response,
        context_refs=context_refs,
        tokens_used=tokens_used,
    )

    # 5. Update conversation title from first user message
    if not conversation.title:
        conversation.title = user_message[:100]
        conversation.save(update_fields=["title", "updated_at"])

    return full_response, context_refs


def process_query_stream(conversation: Conversation, user_message: str):
    """Streaming RAG pipeline. Yields {"type": "...", "data": ...} dicts.

    Events:
      {"type":"thinking"} — search started
      {"type":"context", "refs": [...]} — context found
      {"type":"token", "text":"..."} — incremental response
      {"type":"done", "tokens": N} — completion
      {"type":"error", "message":"..."}
    """
    # Save user message
    Message.objects.create(
        conversation=conversation,
        role=Message.Role.USER,
        content=user_message,
    )

    yield {"type": "thinking"}

    language = _detect_language(user_message)
    context_chunks = _search_context(user_message, conversation.role, language)
    context_refs = _build_context_refs(context_chunks)
    yield {"type": "context", "refs": context_refs}

    system_prompt = get_system_prompt(conversation.role, context_chunks)
    history = _get_history(conversation)
    if history and history[-1]["role"] == "user" and history[-1]["content"] == user_message:
        history.pop()
    messages = history + [{"role": "user", "content": user_message}]

    client = _get_anthropic_client()
    full_response = ""
    tokens_used = 0

    if client:
        try:
            with client.messages.stream(
                model=getattr(settings, "ANTHROPIC_MODEL", DEFAULT_MODEL),
                max_tokens=MAX_RESPONSE_TOKENS,
                system=system_prompt,
                messages=messages,
            ) as stream:
                for text in stream.text_stream:
                    full_response += text
                    yield {"type": "token", "text": text}
                final = stream.get_final_message()
                if final and getattr(final, "usage", None):
                    tokens_used = final.usage.input_tokens + final.usage.output_tokens
        except Exception as e:
            logger.error(f"Anthropic streaming error: {e}")
            err = f"⚠️ Ошибка API: {e}"
            yield {"type": "token", "text": err}
            full_response = err
    else:
        # Stub mode — emit response as one chunk
        full_response = _stub_response(user_message, context_chunks)
        yield {"type": "token", "text": full_response}

    # Save assistant message
    Message.objects.create(
        conversation=conversation,
        role=Message.Role.ASSISTANT,
        content=full_response,
        context_refs=context_refs,
        tokens_used=tokens_used,
    )

    if not conversation.title:
        conversation.title = user_message[:100]
        conversation.save(update_fields=["title", "updated_at"])

    yield {"type": "done", "tokens": tokens_used, "refs": context_refs}

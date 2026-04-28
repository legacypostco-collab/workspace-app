"""RAG pipeline — main logic for AI assistant queries.

Two flavors:
- process_query_sync: blocking, returns (text, refs) — for REST API
- process_query_stream: generator yielding tokens — for WebSocket streaming
"""
from __future__ import annotations

import logging
import os

from django.conf import settings

from . import actions as action_executor
from .card_renderer import parse_cards_from_text
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


def process_query_sync(conversation: Conversation, user_message: str, user=None):
    """Sync RAG pipeline. Returns dict with text/cards/actions/refs.

    Saves user + assistant messages to the conversation.
    """
    user = user or conversation.user

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
    available = action_executor.list_actions(conversation.role)
    system_prompt = get_system_prompt(conversation.role, context_chunks, available)
    history = _get_history(conversation)
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
        full_response = _stub_with_action(user_message, context_chunks, conversation.role, user)

    # 4. Parse cards/actions from AI text
    clean_text, cards, actions = parse_cards_from_text(full_response)

    # 5. Save assistant message
    Message.objects.create(
        conversation=conversation,
        role=Message.Role.ASSISTANT,
        content=clean_text or full_response,
        cards=cards,
        actions=actions,
        context_refs=context_refs,
        tokens_used=tokens_used,
    )

    # 6. Update conversation title
    if not conversation.title:
        conversation.title = user_message[:100]
        conversation.save(update_fields=["title", "updated_at"])

    return {
        "text": clean_text or full_response,
        "cards": cards,
        "actions": actions,
        "context_refs": context_refs,
        "tokens_used": tokens_used,
    }


def execute_action(conversation: Conversation, action_name: str, params: dict, user=None):
    """Execute a chat action (e.g. user clicked a button).

    Saves an "action" message + an assistant message with the result cards.
    Returns dict with text/cards/actions.
    """
    user = user or conversation.user

    # Save user-action message (for history)
    label = params.get("_label") or action_name
    Message.objects.create(
        conversation=conversation,
        role=Message.Role.ACTION,
        content=f"▸ {label}",
        actions=[{"action": action_name, "params": params}],
    )

    # Execute action
    result = action_executor.execute(action_name, params, user, conversation.role)

    # Save assistant message with result
    Message.objects.create(
        conversation=conversation,
        role=Message.Role.ASSISTANT,
        content=result.text,
        cards=result.cards,
        actions=result.actions,
    )

    if not conversation.title:
        conversation.title = label[:100]
        conversation.save(update_fields=["title", "updated_at"])

    return {
        "text": result.text,
        "cards": result.cards,
        "actions": result.actions,
        "suggestions": result.suggestions,
    }


def _stub_with_action(user_message: str, chunks, role: str, user) -> str:
    """Heuristic: detect intent and call appropriate action when ANTHROPIC_API_KEY missing."""
    import json as _json
    msg_lower = user_message.lower()
    intent_actions = [
        (["заказ", "order", "订单"], "get_orders"),
        (["rfq", "котировк"], "get_rfq_status"),
        (["трекинг", "track", "shipment", "доставк"], "track_shipment"),
        (["бюджет", "budget"], "get_budget"),
        (["аналитик", "analytics"], "get_analytics"),
        (["рекламац", "claim"], "get_claims"),
        (["sla"], "get_sla_report"),
    ]
    matched_action = None
    for keywords, action in intent_actions:
        if any(k in msg_lower for k in keywords):
            matched_action = action
            break
    # Default fallback to search
    if not matched_action and len(user_message) > 3:
        matched_action = "search_parts"

    if matched_action and action_executor.can_execute(matched_action, role):
        result = action_executor.execute(matched_action, {"query": user_message}, user, role)
        # Format cards back into :::blocks for parser
        text = result.text or ""
        for c in result.cards:
            text += f"\n\n:::{c['type']}\n{_json.dumps(c['data'], ensure_ascii=False)}\n:::"
        if result.actions:
            text += f"\n\n:::actions\n{_json.dumps(result.actions, ensure_ascii=False)}\n:::"
        return text or _stub_response(user_message, chunks)
    return _stub_response(user_message, chunks)


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
        # Stub mode — try heuristic action call, emit cards in one chunk
        full_response = _stub_with_action(user_message, context_chunks, conversation.role, conversation.user)
        yield {"type": "token", "text": full_response}

    # Parse cards/actions from final text
    clean_text, cards, actions = parse_cards_from_text(full_response)

    # Save assistant message
    Message.objects.create(
        conversation=conversation,
        role=Message.Role.ASSISTANT,
        content=clean_text or full_response,
        cards=cards,
        actions=actions,
        context_refs=context_refs,
        tokens_used=tokens_used,
    )

    if not conversation.title:
        conversation.title = user_message[:100]
        conversation.save(update_fields=["title", "updated_at"])

    yield {"type": "cards", "cards": cards, "actions": actions}
    yield {"type": "done", "tokens": tokens_used, "refs": context_refs}

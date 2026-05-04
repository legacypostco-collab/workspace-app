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
MAX_TOOL_TURNS = 6
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


_ANTHROPIC_CLIENT_CACHE = {"client": None, "checked": False}


def _get_anthropic_client():
    """Return cached Anthropic client, or None if no API key / SDK missing.

    Logs once on first call so it's obvious in dev whether the smart mode is on.
    """
    if _ANTHROPIC_CLIENT_CACHE["checked"]:
        return _ANTHROPIC_CLIENT_CACHE["client"]
    _ANTHROPIC_CLIENT_CACHE["checked"] = True

    api_key = getattr(settings, "ANTHROPIC_API_KEY", "") or os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        logger.warning(
            "AI Assistant: ANTHROPIC_API_KEY is not set — falling back to STUB mode "
            "(keyword heuristics, no real LLM). Set ANTHROPIC_API_KEY in .env to enable smart agent mode."
        )
        return None
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        model = getattr(settings, "ANTHROPIC_MODEL", DEFAULT_MODEL)
        logger.info(f"AI Assistant: Anthropic client ready (model={model}, tool-use enabled)")
        _ANTHROPIC_CLIENT_CACHE["client"] = client
        return client
    except ImportError:
        logger.warning("AI Assistant: 'anthropic' package not installed — falling back to STUB mode")
        return None


def _run_claude_with_tools(client, system_prompt, messages, role, user) -> tuple[str, int, list, list]:
    """Agentic loop: Claude calls tools (= our actions) until it produces a final answer.

    Returns: (final_text, tokens_used, accumulated_cards, accumulated_actions)
    """
    import json as _json
    from . import actions as action_executor

    tools = action_executor.get_tool_definitions(role)
    model = getattr(settings, "ANTHROPIC_MODEL", DEFAULT_MODEL)

    # Mutable working copy — we'll append assistant turns and tool_result turns to it
    msgs = [dict(m) for m in messages]

    accumulated_cards: list = []
    accumulated_actions: list = []
    tokens_total = 0
    final_text_parts: list[str] = []

    for turn in range(MAX_TOOL_TURNS):
        kwargs = {
            "model": model,
            "max_tokens": MAX_RESPONSE_TOKENS,
            "system": system_prompt,
            "messages": msgs,
        }
        if tools:
            kwargs["tools"] = tools

        resp = client.messages.create(**kwargs)
        if hasattr(resp, "usage"):
            tokens_total += resp.usage.input_tokens + resp.usage.output_tokens

        # Extract text + tool_use blocks
        text_chunks = []
        tool_uses = []
        for block in resp.content:
            btype = getattr(block, "type", None)
            if btype == "text":
                text_chunks.append(block.text)
            elif btype == "tool_use":
                tool_uses.append(block)

        if text_chunks:
            final_text_parts.append("".join(text_chunks))

        # No tool calls → final answer
        if resp.stop_reason != "tool_use" or not tool_uses:
            break

        # Append assistant message with text+tool_use as the canonical content blocks
        msgs.append({
            "role": "assistant",
            "content": [
                {"type": "text", "text": b.text} if getattr(b, "type", None) == "text"
                else {"type": "tool_use", "id": b.id, "name": b.name, "input": b.input}
                for b in resp.content
                if getattr(b, "type", None) in ("text", "tool_use")
            ],
        })

        # Execute each tool, build tool_result blocks
        tool_results = []
        for tu in tool_uses:
            result = action_executor.execute(tu.name, tu.input or {}, user, role)
            accumulated_cards.extend(result.cards or [])
            accumulated_actions.extend(result.actions or [])

            # Send only text + a slim card summary back to Claude — no need to dump
            # the full card JSON; Claude just needs to know what happened.
            summary_lines = [result.text or ""]
            if result.cards:
                summary_lines.append(
                    f"[Получено {len(result.cards)} карточек: " +
                    ", ".join(c.get("type", "?") for c in result.cards) + "]"
                )
                # Include compact data preview so Claude can reason about it
                for c in result.cards[:3]:
                    summary_lines.append(_json.dumps(c.get("data", {}), ensure_ascii=False)[:600])
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": "\n".join(s for s in summary_lines if s).strip() or "OK",
            })

        msgs.append({"role": "user", "content": tool_results})
        # Continue loop — Claude will see tool results and either call more tools or finalize.

    final_text = "\n\n".join(t for t in final_text_parts if t).strip()
    return final_text, tokens_total, accumulated_cards, accumulated_actions


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

    Hybrid execution:
      1. Fast-path: deterministic intent → run action directly, skip LLM
         (multi-article paste, "show my orders", "make proposal", etc.)
      2. Slow-path: Claude tool-use for ambiguous queries
      3. Stub: keyword fallback if no API key

    Saves user + assistant messages to the conversation.
    """
    from . import fast_path

    user = user or conversation.user

    # 1. Save user message
    Message.objects.create(
        conversation=conversation,
        role=Message.Role.USER,
        content=user_message,
    )

    # 2. Try fast-path first — deterministic, free, instant
    fp_match = fast_path.match(user_message, conversation.role)
    if fp_match:
        action_name, params, rule_name = fp_match
        if action_executor.can_execute(action_name, conversation.role):
            logger.info(f"AI fast-path: {rule_name} → {action_name}({params})")
            result = action_executor.execute(action_name, params, user, conversation.role)
            clean_text = result.text or ""
            cards = result.cards or []
            actions = result.actions or []

            assistant_msg = Message.objects.create(
                conversation=conversation,
                role=Message.Role.ASSISTANT,
                content=clean_text,
                cards=cards,
                actions=actions,
                context_refs=[],
                tokens_used=0,
            )
            if not conversation.title:
                conversation.title = user_message[:100]
                conversation.save(update_fields=["title", "updated_at"])
            return {
                "text": clean_text, "cards": cards, "actions": actions,
                "context_refs": [],
                "contextual_actions": list(getattr(result, "contextual_actions", []) or []),
                "suggestions": list(getattr(result, "suggestions", []) or []),
                "message_id": str(assistant_msg.id),
            }

    # 3. Slow-path: Claude tool-use for everything else
    language = _detect_language(user_message)
    context_chunks = _search_context(user_message, conversation.role, language)
    context_refs = _build_context_refs(context_chunks)
    available = action_executor.list_actions(conversation.role)
    system_prompt = get_system_prompt(conversation.role, context_chunks, available)
    history = _get_history(conversation)
    if history and history[-1]["role"] == "user" and history[-1]["content"] == user_message:
        history.pop()

    messages = history + [{"role": "user", "content": user_message}]

    client = _get_anthropic_client()
    full_response = ""
    tokens_used = 0

    extra_cards: list = []
    extra_actions: list = []

    if client:
        try:
            full_response, tokens_used, extra_cards, extra_actions = _run_claude_with_tools(
                client=client,
                system_prompt=system_prompt,
                messages=messages,
                role=conversation.role,
                user=user,
            )
        except Exception as e:
            logger.exception("Anthropic API error")
            full_response = f"⚠️ Ошибка API: {e}"
    else:
        full_response = _stub_with_action(user_message, context_chunks, conversation.role, user)

    # 4. Parse cards/actions from AI text
    clean_text, cards, actions = parse_cards_from_text(full_response)
    # Tool-use cards/actions take precedence (real DB data) over Claude's :::blocks
    if extra_cards:
        cards = extra_cards + cards
    if extra_actions:
        actions = extra_actions + actions

    # Strip internal [card:type] placeholders from user-facing text
    import re as _re
    clean_text = _re.sub(r"\[card:\w+\]\s*", "", clean_text or "").strip() or full_response

    # 5. Save assistant message
    assistant_msg = Message.objects.create(
        conversation=conversation,
        role=Message.Role.ASSISTANT,
        content=clean_text,
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
        "text": clean_text,
        "cards": cards,
        "actions": actions,
        "context_refs": context_refs,
        "contextual_actions": [],
        "suggestions": [],
        "tokens_used": tokens_used,
        "message_id": str(assistant_msg.id),
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
    assistant_msg = Message.objects.create(
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
        "contextual_actions": list(getattr(result, "contextual_actions", []) or []),
        "suggestions": result.suggestions,
        "message_id": str(assistant_msg.id),
    }


def _format_action_result(result) -> str:
    """Serialize an ActionResult back into AI-style :::block text for the parser."""
    import json as _json
    text = result.text or ""
    for c in result.cards:
        text += f"\n\n:::{c['type']}\n{_json.dumps(c['data'], ensure_ascii=False)}\n:::"
    if result.actions:
        text += f"\n\n:::actions\n{_json.dumps(result.actions, ensure_ascii=False)}\n:::"
    return text


def _stub_with_action(user_message: str, chunks, role: str, user) -> str:
    """Heuristic: detect intent and call appropriate action when ANTHROPIC_API_KEY missing."""
    import json as _json
    msg_lower = user_message.lower()

    # Top-suppliers intent (must come BEFORE analyze_spec — "топ поставщиков" should win
    # over "посчитай" / "spec" keywords in the same sentence)
    top_kw = ("топ", "top-3", "top 3", "ранжируй", "сравни поставщиков", "сравни цены")
    if any(k in msg_lower for k in top_kw) and (
        "поставщик" in msg_lower or "supplier" in msg_lower or "oem" in msg_lower
    ):
        params = {}
        if "oem" in msg_lower or "только oem" in msg_lower:
            params["condition"] = "oem"
        if action_executor.can_execute("top_suppliers", role):
            result = action_executor.execute("top_suppliers", params, user, role)
            return _format_action_result(result)

    # Spec-analysis intent — "посчитай по парку", "обработай спеку", "сколько будет стоить"
    spec_kw = ("спек", "посчитай по", "посчитай парк", "по нашему парку", "по парку",
               "сколько будет стоить", "обработай", "разбери список", "по списку",
               "best mix", "best price", "лучший микс", "проанализируй спек")
    only_oem_kw = ("только oem", "лидтайм до", "максимум 14 дней", "не больше 14")
    if any(k in msg_lower for k in spec_kw) or any(k in msg_lower for k in only_oem_kw):
        params = {}
        if "только oem" in msg_lower or "только oem" in msg_lower or " oem" in msg_lower:
            params["condition"] = "oem"
        # parse "лидтайм до 14 дней" → 14
        import re as _re
        m = _re.search(r"лидтайм\s+до\s+(\d+)", msg_lower)
        if m:
            params["lead_max_days"] = int(m.group(1))
        if action_executor.can_execute("analyze_spec", role):
            result = action_executor.execute("analyze_spec", params, user, role)
            return _format_action_result(result)

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
    from . import fast_path

    # Save user message
    Message.objects.create(
        conversation=conversation,
        role=Message.Role.USER,
        content=user_message,
    )

    yield {"type": "thinking"}

    # Fast-path: skip context search + Claude entirely for known intents.
    fp_match = fast_path.match(user_message, conversation.role)
    if fp_match:
        action_name, params, rule_name = fp_match
        if action_executor.can_execute(action_name, conversation.role):
            logger.info(f"AI fast-path (stream): {rule_name} → {action_name}({params})")
            result = action_executor.execute(action_name, params, user=conversation.user, role=conversation.role)
            text = result.text or ""
            cards = result.cards or []
            actions = result.actions or []

            ctx_actions = list(getattr(result, "contextual_actions", []) or [])
            suggestions = list(getattr(result, "suggestions", []) or [])
            yield {"type": "token", "text": text}
            yield {
                "type": "cards", "cards": cards, "actions": actions, "text": text,
                "contextual_actions": ctx_actions, "suggestions": suggestions,
            }
            Message.objects.create(
                conversation=conversation,
                role=Message.Role.ASSISTANT,
                content=text,
                cards=cards,
                actions=actions,
                context_refs=[],
                tokens_used=0,
            )
            if not conversation.title:
                conversation.title = user_message[:100]
                conversation.save(update_fields=["title", "updated_at"])
            yield {"type": "done", "tokens": 0, "refs": []}
            return

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
    extra_cards: list = []
    extra_actions: list = []

    if client:
        try:
            # Tool-use loop runs synchronously (multiple round-trips with Claude). We
            # don't stream tokens during tool calls — instead we emit a "thinking" tick
            # so the UI shows progress, then send the final composed text in one shot.
            yield {"type": "token", "text": ""}  # ensures bubble appears
            full_response, tokens_used, extra_cards, extra_actions = _run_claude_with_tools(
                client=client,
                system_prompt=system_prompt,
                messages=messages,
                role=conversation.role,
                user=conversation.user,
            )
            yield {"type": "token", "text": full_response}
        except Exception as e:
            logger.exception("Anthropic streaming error")
            err = f"⚠️ Ошибка API: {e}"
            yield {"type": "token", "text": err}
            full_response = err
    else:
        # Stub mode — heuristic action call. Stream ONLY the clean text (no :::blocks),
        # then deliver cards/actions through the structured event below.
        full_response = _stub_with_action(user_message, context_chunks, conversation.role, conversation.user)
        clean_for_stream, _, _ = parse_cards_from_text(full_response)
        # Strip [card:type] markers from the streamed text (they were placeholders)
        import re as _re
        clean_for_stream = _re.sub(r"\[card:\w+\]\s*", "", clean_for_stream).strip()
        yield {"type": "token", "text": clean_for_stream}

    # Parse cards/actions from final text
    clean_text, cards, actions = parse_cards_from_text(full_response)
    if extra_cards:
        cards = extra_cards + cards
    if extra_actions:
        actions = extra_actions + actions

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

    # Strip [card:type] markers — they're internal placeholders, not for the user
    import re as _re2
    clean_final = _re2.sub(r"\[card:\w+\]\s*", "", clean_text or full_response).strip()

    yield {"type": "cards", "cards": cards, "actions": actions, "text": clean_final}
    yield {"type": "done", "tokens": tokens_used, "refs": context_refs}

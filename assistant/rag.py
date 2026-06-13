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
DEFAULT_MODEL = "claude-sonnet-4-6"  # sonnet-4-20250514 выводится 2026-06-15 → мигрировали
FAST_MODEL    = "claude-haiku-4-5-20251001"  # 12× дешевле для простых запросов

# Какие роли получают Haiku (простой intent → action). Sonnet остаётся для
# seller/operator — там сложная reasoning (smart price mapping, KP analysis,
# multi-step KYB review). Buyer-chat в 95% случаев — это «найди/закажи/трек».
FAST_MODE_ROLES = {"buyer"}


def _pick_model(role: str | None) -> str:
    """Возвращает имя модели исходя из роли и feature-flag.

    ANTHROPIC_FAST_MODE=1 (env) — принудительно Haiku везде (R&D, dev).
    Иначе buyer → Haiku, остальные → Sonnet (или ANTHROPIC_MODEL override).
    """
    if getattr(settings, "ANTHROPIC_FAST_MODE", False):
        return getattr(settings, "ANTHROPIC_FAST_MODEL", FAST_MODEL)
    if role in FAST_MODE_ROLES:
        return getattr(settings, "ANTHROPIC_FAST_MODEL", FAST_MODEL)
    return getattr(settings, "ANTHROPIC_MODEL", DEFAULT_MODEL)


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
        # timeout: запрос через релей (Москва→Амстердам→Anthropic) иногда
        # подвисает — без таймаута SDK ждёт ~10 мин и чат «висит». 60с/вызов
        # + 1 ретрай на транзиентную сетевую ошибку.
        client = anthropic.Anthropic(api_key=api_key, timeout=60.0, max_retries=1)
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
    model = _pick_model(role)

    # Mutable working copy — we'll append assistant turns and tool_result turns to it
    msgs = [dict(m) for m in messages]

    accumulated_cards: list = []
    accumulated_actions: list = []
    tokens_total = 0
    final_text_parts: list[str] = []

    # ── Prompt caching (Anthropic ephemeral cache, 5 min TTL) ──
    # system-prompt (~3-5K токенов) и tools-schema (~2K) не меняются между
    # запросами одного user-role. Помечаем их cache_control: при втором+
    # запросе в течение 5 мин Anthropic тарифицирует cached input как 10%
    # стоимости (вместо $3/M → $0.30/M). Экономия на повторных:
    #   - 1-й запрос: cache_creation_input_tokens учитываются за полную цену
    #   - 2+: cache_read_input_tokens учитываются за 10%
    # Структура: system как list[block], последний tool с cache_control.
    system_blocks = [{
        "type": "text",
        "text": system_prompt,
        "cache_control": {"type": "ephemeral"},
    }]
    tools_with_cache = None
    if tools:
        # Anthropic кеширует ВСЁ что идёт ДО последнего cache_control,
        # ставим его на самый последний tool — закроется и system, и все tools.
        tools_with_cache = [dict(t) for t in tools]
        tools_with_cache[-1]["cache_control"] = {"type": "ephemeral"}

    for turn in range(MAX_TOOL_TURNS):
        kwargs = {
            "model": model,
            "max_tokens": MAX_RESPONSE_TOKENS,
            "system": system_blocks,
            "messages": msgs,
        }
        if tools_with_cache:
            kwargs["tools"] = tools_with_cache

        # ── Cost-cap: проверка перед запросом, учёт после ──
        from .ai_budget import BudgetExceeded, check_budget_or_raise, record_usage
        try:
            check_budget_or_raise(user)
        except BudgetExceeded as e:
            logger.warning("AI budget exceeded for user %s: $%.4f >= $%.2f",
                            e.user_id, e.spent_usd, e.limit_usd)
            final_text_parts.append(
                f"⚠️ Дневной AI-лимит исчерпан "
                f"(${e.spent_usd:.2f} из ${e.limit_usd:.2f}). "
                f"Возобновится завтра или попробуйте без AI-режима."
            )
            break
        resp = client.messages.create(**kwargs)
        if hasattr(resp, "usage"):
            u = resp.usage
            # cache_read_input_tokens — переиспользованный кэш (10% цены)
            # cache_creation_input_tokens — создание кэша (125% цены, потом окупается)
            # input_tokens — обычный input (не cached)
            cached_read = getattr(u, "cache_read_input_tokens", 0) or 0
            cached_write = getattr(u, "cache_creation_input_tokens", 0) or 0
            regular_input = u.input_tokens
            tokens_total += regular_input + cached_read + cached_write + u.output_tokens
            try:
                # ai_budget учитывает по эффективной цене:
                # cached_read = 0.1× от input_tokens,
                # cached_write = 1.25× от input_tokens.
                effective_input = (
                    regular_input
                    + int(cached_read * 0.1)
                    + int(cached_write * 1.25)
                )
                record_usage(user,
                              input_tokens=effective_input,
                              output_tokens=u.output_tokens)
                if cached_read:
                    logger.info(
                        "AI cache hit: %d cached_read · %d regular_in · saved ~%d tokens",
                        cached_read, regular_input, int(cached_read * 0.9))
            except Exception:
                logger.exception("record_usage failed (non-fatal)")

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

    # Витки исчерпаны, но Claude всё время звал инструменты и не дал текст →
    # final_text был бы ПУСТЫМ, и юзер видит «нет ответа» / вечный спиннер.
    # Добиваем одним вызовом с tool_choice=none — Claude обязан ответить
    # текстом по уже собранным результатам.
    if not any((t or "").strip() for t in final_text_parts):
        try:
            fin_kwargs = {
                "model": model,
                "max_tokens": MAX_RESPONSE_TOKENS,
                "system": system_blocks,
                "messages": msgs,
            }
            if tools_with_cache:
                fin_kwargs["tools"] = tools_with_cache
                fin_kwargs["tool_choice"] = {"type": "none"}
            finalize = client.messages.create(**fin_kwargs)
            ft = "".join(
                b.text for b in finalize.content
                if getattr(b, "type", None) == "text"
            )
            if ft.strip():
                final_text_parts.append(ft)
        except Exception:
            logger.exception("finalize call (tool_choice=none) failed")
        if not any((t or "").strip() for t in final_text_parts):
            final_text_parts.append(
                "Собрал данные по запросу — смотри карточки выше. "
                "Уточни, что показать подробнее?"
            )

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
        "ℹ️ AI ассистент работает в режиме без LLM (ANTHROPIC_API_KEY не настроен).",
        f"Найдено {len(chunks)} релевантных источников по запросу «{query}»:\n",
    ]
    for i, c in enumerate(chunks, 1):
        parts.append(f"{i}. {c.title} ({c.get_source_type_display()})")
        if c.content:
            parts.append(f"   {c.content[:200]}")
    return "\n".join(parts)


def _answer_cache_key(user_id, role, message: str) -> str:
    """Ключ для answer-cache: одна и та же фраза от того же юзера-роли."""
    import hashlib
    h = hashlib.md5(message.strip().lower().encode("utf-8")).hexdigest()
    return f"ai_answer:{user_id}:{role}:{h}"


def process_query_sync(conversation: Conversation, user_message: str, user=None):
    """Sync RAG pipeline. Returns dict with text/cards/actions/refs.

    Hybrid execution:
      0. Answer-cache: тот же вопрос в течение 5 мин → отдаём prior response
         (Redis TTL=300с, ключ хеширует user+role+message-lower)
      1. Fast-path: deterministic intent → run action directly, skip LLM
         (multi-article paste, "show my orders", "make proposal", etc.)
      2. Slow-path: Claude tool-use for ambiguous queries
      3. Stub: keyword fallback if no API key

    Saves user + assistant messages to the conversation.
    """
    from . import fast_path

    user = user or conversation.user

    # 0. Answer cache — повторяющиеся вопросы в течение 5 мин не зовут LLM.
    # Только для slow-path (LLM-heavy) actions — fast-path и так instant.
    # Кэш создаётся ПОСЛЕ обработки в slow-path (см. ниже).
    from django.core.cache import cache
    cache_key = _answer_cache_key(user.id if user else 0, conversation.role, user_message)
    cached = cache.get(cache_key) if user_message.strip() else None
    if cached:
        # Сохраняем user-message и копию ассистентского ответа в БД
        # (для истории), но БЕЗ повторного LLM-вызова.
        Message.objects.create(
            conversation=conversation, role=Message.Role.USER,
            content=user_message,
        )
        assistant_msg = Message.objects.create(
            conversation=conversation, role=Message.Role.ASSISTANT,
            content=cached.get("text") or "",
            cards=cached.get("cards") or [],
            actions=cached.get("actions") or [],
            context_refs=cached.get("context_refs") or [],
            tokens_used=0,  # из кэша = $0
        )
        logger.info("AI answer-cache HIT (saved ~%s tokens)",
                    cached.get("_orig_tokens", "?"))
        return {**cached, "message_id": str(assistant_msg.id), "_from_cache": True}

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

    # 3. Slow-path: Claude tool-use — это ПЛАТНЫЙ вызов. Списываем AI-кредит
    # покупателя (операторы/продавцы — без лимита). Лимит исчерпан → не зовём
    # Claude, показываем «оформите заказ». fast-path/кэш выше — бесплатны.
    from . import ai_credits as _aic
    _ok, _left = _aic.try_consume(user, conversation.role)
    if not _ok:
        _m = _aic.limit_message()
        assistant_msg = Message.objects.create(
            conversation=conversation, role=Message.Role.ASSISTANT,
            content=_m["text"], cards=[], actions=_m["actions"],
            context_refs=[], tokens_used=0,
        )
        return {
            "text": _m["text"], "cards": [], "actions": _m["actions"],
            "context_refs": [], "contextual_actions": [],
            "suggestions": _m["suggestions"], "message_id": str(assistant_msg.id),
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

    response_dict = {
        "text": clean_text,
        "cards": cards,
        "actions": actions,
        "context_refs": context_refs,
        "contextual_actions": [],
        "suggestions": [],
        "tokens_used": tokens_used,
        "message_id": str(assistant_msg.id),
    }

    # 7. Cache LLM-response на 5 минут — повторный идентичный вопрос отдаст
    # тот же ответ без LLM-вызова. Кэш только для slow-path (LLM-heavy),
    # fast-path и так бесплатный. Не кэшируем mutating actions (создание RFQ/Order)
    # — у них есть `actions` с side-effects в follow-up клике.
    has_side_effects = any(
        a.get("action", "").startswith(("create_", "pay_", "quick_order",
                                          "accept_quote", "confirm_"))
        for a in (actions or [])
    )
    if user_message.strip() and not has_side_effects:
        cache.set(cache_key, {
            **response_dict, "_orig_tokens": tokens_used,
        }, 300)  # 5 минут

    return response_dict


def execute_action(conversation: Conversation | None, action_name: str, params: dict,
                    user=None, role: str | None = None):
    """Execute a chat action (e.g. user clicked a button).

    Saves an "action" message + an assistant message with the result cards.
    Returns dict with text/cards/actions.

    `role` — текущая UI-роль (от тоггла в шапке). Если None, fallback на
    сохранённую в conversation. Это позволяет покупателю переключиться
    в режим «Продавец» в любой conversation, не плодя новые.

    Если conversation=None — stateless flow (анонимный юзер кликнул
    кнопку из ANON_ALLOWED_ACTIONS, например start_registration). Не пишем
    Message в БД (нечем привязаться), просто выполняем action и отдаём ответ.
    """
    # Stateless anon-flow: нет conversation, нет user.
    if conversation is None:
        label = params.get("_label") or action_name
        effective_role = role or "buyer"
        result = action_executor.execute(action_name, params, user, effective_role)
        return {
            "text": result.text,
            "cards": result.cards,
            "actions": result.actions,
            "contextual_actions": list(getattr(result, "contextual_actions", []) or []),
            "suggestions": result.suggestions,
            "message_id": None,
        }

    user = user or conversation.user

    # Save user-action message (for history) — но без _request (HttpRequest не сериализуем).
    label = params.get("_label") or action_name
    saved_params = {k: v for k, v in (params or {}).items() if k != "_request"}

    # FIX: защита от дублей при двойном клике / быстром ретрае. Если за
    # последние 3 секунды ровно та же пара (action, params) уже записывалась
    # в эту conversation — возвращаем последний ASSISTANT-ответ вместо
    # повторного выполнения и нового Message.create. Юзер всё равно увидит
    # тот же результат; в истории не плодим дубли.
    from datetime import timedelta
    from django.utils import timezone as _tz
    debounce_cutoff = _tz.now() - timedelta(seconds=3)
    recent_dup = (Message.objects
        .filter(conversation=conversation, role=Message.Role.ACTION,
                created_at__gte=debounce_cutoff)
        .order_by("-created_at").first())
    if recent_dup:
        try:
            prev_action = (recent_dup.actions or [{}])[0]
            if prev_action.get("action") == action_name \
               and (prev_action.get("params") or {}) == saved_params:
                # Берём последний ASSISTANT после этого action
                prev_assistant = (Message.objects
                    .filter(conversation=conversation, role=Message.Role.ASSISTANT,
                            created_at__gte=recent_dup.created_at)
                    .order_by("created_at").first())
                if prev_assistant:
                    return {
                        "text": prev_assistant.content,
                        "cards": prev_assistant.cards or [],
                        "actions": prev_assistant.actions or [],
                        "contextual_actions": [],
                        "suggestions": [],
                        "message_id": str(prev_assistant.id),
                        "_debounced": True,
                    }
        except Exception:
            pass

    Message.objects.create(
        conversation=conversation,
        role=Message.Role.ACTION,
        content=f"▸ {label}",
        actions=[{"action": action_name, "params": saved_params}],
    )

    # Execute action — current request's role over conversation's stored role
    effective_role = role or conversation.role
    result = action_executor.execute(action_name, params, user, effective_role)

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

    # Slow-path = ПЛАТНЫЙ Claude. Лимит AI-кредитов покупателя (см. sync-путь).
    from . import ai_credits as _aic
    _ok, _left = _aic.try_consume(conversation.user, conversation.role)
    if not _ok:
        _m = _aic.limit_message()
        yield {"type": "token", "text": _m["text"]}
        yield {"type": "cards", "cards": [], "actions": _m["actions"],
               "text": _m["text"], "contextual_actions": [],
               "suggestions": _m["suggestions"]}
        Message.objects.create(
            conversation=conversation, role=Message.Role.ASSISTANT,
            content=_m["text"], cards=[], actions=_m["actions"],
            context_refs=[], tokens_used=0,
        )
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

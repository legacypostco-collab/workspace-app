"""AI-ассистент для кабинетов оператора, продавца и покупателя.

Использует Anthropic Claude API. Поддерживает streaming через SSE.
Системный промпт зависит от роли пользователя.
"""
from __future__ import annotations

import os
from typing import Iterator, Optional

try:
    from anthropic import Anthropic
except ImportError:  # pragma: no cover
    Anthropic = None  # type: ignore


DEFAULT_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
MAX_TOKENS = 1024


SYSTEM_PROMPTS = {
    "operator": (
        "Ты — AI-ассистент оператора маркетплейса Consolidator. "
        "Помогаешь менеджерам, логистам, таможенникам и платёжным операторам в работе. "
        "Отвечаешь кратко, по делу, на русском языке. "
        "Знаешь предметную область: логистика грузов, таможенное оформление, эскроу-платежи, "
        "сверка документов, переговоры с поставщиками. "
        "Если вопрос требует данных из системы — поясни, какие данные нужно посмотреть "
        "и в каком разделе кабинета их найти."
    ),
    "seller": (
        "Ты — AI-ассистент продавца на маркетплейсе Consolidator. "
        "Помогаешь продавцу с управлением каталогом запчастей, обработкой запросов (RFQ), "
        "ценообразованием, заказами и претензиями. "
        "Отвечаешь кратко, на русском языке. "
        "Подсказывай, как лучше настроить карточки товара, реагировать на падение спроса, "
        "оформлять предложения по запросам, работать с командой и интеграциями."
    ),
    "buyer": (
        "Ты — AI-ассистент покупателя на маркетплейсе Consolidator. "
        "Помогаешь покупателю искать запчасти, формировать заказы, вести переговоры "
        "с поставщиками, отслеживать поставки, оформлять рекламации. "
        "Отвечаешь кратко, на русском языке, с акцентом на практические шаги."
    ),
    "default": (
        "Ты — AI-ассистент маркетплейса Consolidator. Отвечаешь кратко на русском языке."
    ),
}


def _client() -> Optional["Anthropic"]:
    if Anthropic is None:
        return None
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return None
    return Anthropic(api_key=api_key)


def get_system_prompt(role: str) -> str:
    return SYSTEM_PROMPTS.get(role, SYSTEM_PROMPTS["default"])


def is_configured() -> bool:
    return Anthropic is not None and bool(os.getenv("ANTHROPIC_API_KEY", "").strip())


def chat(messages: list[dict], role: str = "default") -> str:
    """Синхронный вызов: возвращает полный текст ответа."""
    client = _client()
    if client is None:
        return _fallback_reply(messages, role)

    try:
        resp = client.messages.create(
            model=DEFAULT_MODEL,
            max_tokens=MAX_TOKENS,
            system=[
                {
                    "type": "text",
                    "text": get_system_prompt(role),
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=messages,
        )
        parts = [block.text for block in resp.content if getattr(block, "type", "") == "text"]
        return "".join(parts).strip() or "(пустой ответ)"
    except Exception as exc:  # pragma: no cover
        return f"Ошибка обращения к AI: {exc}"


def chat_stream(messages: list[dict], role: str = "default") -> Iterator[str]:
    """Стриминг: возвращает кусочки текста по мере генерации."""
    client = _client()
    if client is None:
        yield _fallback_reply(messages, role)
        return

    try:
        with client.messages.stream(
            model=DEFAULT_MODEL,
            max_tokens=MAX_TOKENS,
            system=[
                {
                    "type": "text",
                    "text": get_system_prompt(role),
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=messages,
        ) as stream:
            for text in stream.text_stream:
                yield text
    except Exception as exc:  # pragma: no cover
        yield f"\n\n[Ошибка AI: {exc}]"


def _fallback_reply(messages: list[dict], role: str) -> str:
    """Заглушка, когда ANTHROPIC_API_KEY не задан — чтобы UI не казался сломанным."""
    last_user = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            last_user = (m.get("content") or "").strip()
            break
    return (
        "AI-ассистент не настроен — задайте переменную окружения ANTHROPIC_API_KEY "
        "в файле .env, затем перезапустите сервер.\n\n"
        f"(Получено сообщение: «{last_user[:120]}»)"
    )

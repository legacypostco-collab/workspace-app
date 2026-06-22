"""ТЗ: защита от runaway tool loop в Claude-агенте (rag._run_claude_with_tools).

Мокаем Anthropic-клиент, который БЕСКОНЕЧНО зовёт инструмент (имитация runaway),
и проверяем что:
  §1 — tool_result обрезается до MAX_TOOL_RESULT_CHARS + суффикс;
  §3 — max_tokens выставлен по типу запроса;
  §4 — цикл не превышает MAX_TOOL_TURNS, затем один finalize.
"""
from unittest.mock import MagicMock, patch

import pytest


class _Block:
    def __init__(self, type, text=None, id=None, name=None, input=None):
        self.type = type
        self.text = text
        self.id = id
        self.name = name
        self.input = input


class _Usage:
    input_tokens = 100
    output_tokens = 50
    cache_read_input_tokens = 0
    cache_creation_input_tokens = 0


class _Resp:
    def __init__(self, content, stop_reason):
        self.content = content
        self.stop_reason = stop_reason
        self.usage = _Usage()


class _Messages:
    def __init__(self, outer):
        self.outer = outer

    def create(self, **kwargs):
        self.outer.calls.append(kwargs)
        # finalize-вызов (tool_choice=none) → отдаём текст, чтобы цикл закрылся
        if (kwargs.get("tool_choice") or {}).get("type") == "none":
            return _Resp([_Block("text", text="итоговый ответ по данным")], "end_turn")
        # иначе ВСЕГДА зовём инструмент → имитируем runaway
        n = len(self.outer.calls)
        return _Resp([_Block("tool_use", id=f"t{n}", name="get_orders", input={})], "tool_use")


class _Client:
    def __init__(self):
        self.calls = []
        self.messages = _Messages(self)


@pytest.mark.django_db
def test_runaway_tool_loop_is_capped():
    from assistant import rag

    big = MagicMock()
    big.text = "Д" * 10_000          # огромный tool_result
    big.cards = [{"type": "order", "data": {"id": i}} for i in range(5)]
    big.actions = []

    client = _Client()
    tool_def = [{"name": "get_orders", "description": "d",
                 "input_schema": {"type": "object", "properties": {}}}]
    q = "Кто из поставщиков самый плохой"
    with patch("assistant.actions.execute", return_value=big), \
         patch("assistant.actions.get_tool_definitions", return_value=tool_def):
        text, tokens, cards, actions = rag._run_claude_with_tools(
            client, "sys", [{"role": "user", "content": q}],
            "operator", None, user_query=q)

    loop_calls = [c for c in client.calls if (c.get("tool_choice") or {}).get("type") != "none"]
    finalize_calls = [c for c in client.calls if (c.get("tool_choice") or {}).get("type") == "none"]

    # §4 — не больше MAX_TOOL_TURNS витков + ровно один finalize
    assert len(loop_calls) <= rag.MAX_TOOL_TURNS == 5
    assert len(finalize_calls) == 1
    # §3 — "самый" → TOOL-потолок (2048), не дефолтный chat и не analytics
    assert loop_calls[0]["max_tokens"] == rag.MAX_TOKENS_TOOL == 2048
    # §1 — tool_result во 2-м вызове обрезан до лимита + суффикс
    tr = client.calls[1]["messages"][-1]["content"][0]["content"]
    assert len(tr) <= rag.MAX_TOOL_RESULT_CHARS + 80
    assert "обрезаны" in tr
    # цикл всё же дал текстовый ответ
    assert text.strip()


@pytest.mark.django_db
def test_precheck_heavy_query_gates():
    from assistant import rag

    def _mk(answer):
        class _M:
            @staticmethod
            def create(**kw):
                assert kw["max_tokens"] == 256  # лёгкий pre-check
                return _Resp([_Block("text", text=answer)], "end_turn")

        class _C:
            messages = _M
        return _C

    # тяжёлый запрос + SUMMARY → директива «режим сводки»
    d = rag._precheck_heavy_query(_mk("SUMMARY"), "топ худших поставщиков", "operator", None)
    assert "СВОДКИ" in d
    # тяжёлый + DETAIL → без директивы
    assert rag._precheck_heavy_query(_mk("DETAIL"), "топ поставщиков", "operator", None) == ""
    # не тяжёлый запрос → даже не зовём модель, пустая строка
    assert rag._precheck_heavy_query(_mk("SUMMARY"), "покажи мой заказ #5", "operator", None) == ""

    # ошибка клиента → graceful '' (не валит чат)
    class _CErr:
        class messages:
            @staticmethod
            def create(**kw):
                raise RuntimeError("api down")
    assert rag._precheck_heavy_query(_CErr, "все заказы", "operator", None) == ""


@pytest.mark.django_db
def test_max_tokens_tiers():
    from assistant.rag import (_max_out_tokens, MAX_TOKENS_CHAT,
                               MAX_TOKENS_TOOL, MAX_TOKENS_ANALYTICS)
    assert _max_out_tokens("привет") == MAX_TOKENS_CHAT
    assert _max_out_tokens("покажи аналитику за месяц") == MAX_TOKENS_ANALYTICS
    assert _max_out_tokens("отчёт по продажам") == MAX_TOKENS_ANALYTICS
    assert _max_out_tokens("топ поставщиков") == MAX_TOKENS_TOOL
    assert _max_out_tokens("сравни всех") == MAX_TOKENS_TOOL

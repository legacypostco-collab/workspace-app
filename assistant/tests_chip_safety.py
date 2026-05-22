"""Test-страховка: под карточками сущностей (Order/RFQ/Shipment/Payment)
все quick-reply должны быть action-chip (объект с `action`/`params`),
а не plain-text-chip (строка).

Plain-text-chip уходит через input → /chat/ → Claude. Это дорогая дорога
для случая, где контекст однозначен (карточка конкретного заказа) и
ответ детерминирован.

Этот тест ловит регрессию: если кто-то добавит обратно `suggestions=["Где
заказ?", ...]` в ответ action'а, который возвращает карточку Order/RFQ —
тест упадёт.
"""
from __future__ import annotations

import re
from pathlib import Path

from django.test import SimpleTestCase

# Запрещённые plain-text quick-reply под карточкой заказа: контекст ясен,
# должны быть action-chip с прямым data-action.
FORBIDDEN_TEXT_SUGGESTIONS = (
    "Где заказ?",
    "Когда доставят?",
    "История по заказу",
)

# Имена action'ов, в чьих ответах эти строки точно не должны встречаться
# (это action'ы, возвращающие карточку конкретной сущности — контекст однозначен).
ENTITY_ACTIONS = (
    "track_order",       # карточка заказа
    "get_order_detail",  # карточка заказа
    "advance_order",     # переход статуса заказа
    "rfq_detail",        # карточка RFQ
    "track_shipment",    # карточка отгрузки
)


def _read(rel: str) -> str:
    base = Path(__file__).resolve().parent
    return (base / rel).read_text(encoding="utf-8")


class EntityCardChipSafetyTests(SimpleTestCase):
    """Static-scan тест на наличие forbidden text suggestions в action-файлах."""

    SOURCE_FILES = ("actions.py", "seller_actions.py", "operator_actions.py")

    def test_no_forbidden_text_suggestions_in_entity_action_responses(self):
        """В ответе action'а из ENTITY_ACTIONS не должно быть plain-text
        suggestion'ов из FORBIDDEN_TEXT_SUGGESTIONS."""
        offenders: list[tuple[str, int, str, str]] = []

        # Регэксп ловит блок `@register("<action>")` ... до следующего @register
        # или EOF. Этого хватает для нашей цели — найти все «зоны влияния»
        # entity-action'а и проверить их.
        for fname in self.SOURCE_FILES:
            src = _read(fname)
            for action_name in ENTITY_ACTIONS:
                pattern = re.compile(
                    rf'@register\(["\']{action_name}["\']\)(.*?)(?=@register\(|\Z)',
                    re.DOTALL,
                )
                for m in pattern.finditer(src):
                    block = m.group(1)
                    block_start_line = src[: m.start()].count("\n") + 1
                    for forbidden in FORBIDDEN_TEXT_SUGGESTIONS:
                        if f'"{forbidden}"' in block or f"'{forbidden}'" in block:
                            # Нужно убедиться, что это именно в suggestions=,
                            # а не где-то в комментарии / тексте сообщения.
                            sug_block = re.search(
                                r"suggestions\s*=\s*\[(.*?)\]",
                                block, re.DOTALL,
                            )
                            if sug_block and forbidden in sug_block.group(1):
                                offenders.append(
                                    (fname, block_start_line, action_name, forbidden)
                                )

        if offenders:
            msg = "\n".join(
                f"  {f}:~{ln}  @register('{a}')  → plain-text chip '{s}'"
                for f, ln, a, s in offenders
            )
            self.fail(
                "Под карточками сущностей найдены plain-text quick-reply, "
                "которые уйдут через /chat/ в Claude вместо детерминированного "
                "endpoint'а. Замените на action-chip "
                "{'label': '…', 'action': '…', 'params': {…}}:\n" + msg
            )

    def test_action_chip_format_recognised(self):
        """Sanity: убедиться, что action-chip формат поддержан хотя бы где-то
        (защита от того, чтоб разработчик не «починил» падающий тест выше
        просто удалением suggestions вовсе)."""
        src = _read("actions.py")
        # Ищем хотя бы один кейс suggestions=[{...action...}] — это и есть
        # action-chip формат, к которому мы хотим перейти.
        self.assertTrue(
            re.search(r"suggestions\s*=\s*\[\s*\{[^}]*['\"]action['\"]", src,
                       re.DOTALL),
            "Не нашёл ни одного action-chip suggestion в actions.py. "
            "Action-chip = {'label', 'action', 'params'} вместо строки.",
        )

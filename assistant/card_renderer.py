"""Parse :::type ... ::: card blocks from AI text into structured cards.

Supported block types: product, rfq, order, shipment, supplier, comparison,
chart, actions, file, table.

AI is instructed (via prompts.py) to output JSON inside :::type ... ::: fences.
This parser extracts them and returns (clean_text, cards, actions).
"""
from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)

CARD_TYPES = {"product", "rfq", "order", "shipment", "supplier",
              "comparison", "chart", "file", "table"}
ACTION_TYPE = "actions"

_BLOCK_RE = re.compile(
    r":::(" + "|".join(sorted(CARD_TYPES | {ACTION_TYPE})) + r")\s*\n([\s\S]*?)\n:::",
    re.MULTILINE,
)


def parse_cards_from_text(text: str) -> tuple[str, list, list]:
    """Extract card/action blocks from AI text.

    Returns (clean_text, cards, actions):
      clean_text — original text with card blocks replaced by [card:type] markers
      cards      — list of {type, data} dicts
      actions    — list of action button dicts {label, action, params}
    """
    if not text:
        return "", [], []

    cards = []
    actions = []

    def replace(m):
        block_type = m.group(1)
        body = m.group(2).strip()
        try:
            payload = json.loads(body)
        except Exception as e:
            logger.warning(f"Failed to parse :::{block_type} block: {e}")
            return m.group(0)  # keep raw on parse failure

        if block_type == ACTION_TYPE:
            # actions block contains a list of {label, action, params}
            if isinstance(payload, list):
                actions.extend(payload)
            return ""  # drop block from text
        else:
            # card block — single object or list of objects
            items = payload if isinstance(payload, list) else [payload]
            for item in items:
                cards.append({"type": block_type, "data": item})
            return f"[card:{block_type}]"

    clean = _BLOCK_RE.sub(replace, text)
    # Collapse extra blank lines
    clean = re.sub(r"\n{3,}", "\n\n", clean).strip()
    return clean, cards, actions


def merge_action_results(results: list) -> tuple[str, list, list, list]:
    """Combine multiple ActionResult objects into one text+cards+actions+suggestions."""
    text_parts = []
    cards = []
    actions = []
    suggestions = []
    for r in results:
        if r.text:
            text_parts.append(r.text)
        cards.extend(r.cards)
        actions.extend(r.actions)
        suggestions.extend(r.suggestions)
    return "\n\n".join(text_parts), cards, actions, suggestions

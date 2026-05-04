"""Проактивные AI-предложения — Уровень 2.5 в кнопочной системе.

По ТЗ: «правила покрывают 90% контекстных кнопок (срочный заказ → ускорить,
просрочка SLA → история, новый поставщик → профиль). AI добавляет 1–3 кнопки
только если в данных есть НЕСТАНДАРТНЫЙ нюанс — то, чего нет в правилах».

Этот модуль вызывает Claude API c узким промптом и whitelist'ом действий.
Если API недоступен (нет ключа, ошибка) — возвращает [], не падая.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# Тулзы из actions.py, которые AI имеет право предлагать как proactive button.
# Это whitelist — иначе модель может «галлюцинировать» несуществующее действие.
PROACTIVE_WHITELIST = {
    "kb_search", "compare_products", "compare_suppliers", "top_suppliers",
    "get_demand_report", "get_sla_report", "get_analytics",
    "track_order", "audit_log", "price_quote",
    "generate_proposal", "create_claim",
}


def _client():
    try:
        import anthropic
    except ImportError:
        return None
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return None
    return anthropic.Anthropic(api_key=api_key)


def proactive_actions_for(*, intent: str, context: dict, max_items: int = 3) -> list[dict]:
    """Опционально предложить 0–3 нестандартных контекстных кнопки.

    intent — что сделал пользователь (например, "track_order:142").
    context — dict с фактами (статус, поставщик, история).

    Без ключа API возвращает [].
    """
    cli = _client()
    if cli is None:
        return []

    try:
        prompt = _build_prompt(intent, context, max_items)
        resp = cli.messages.create(
            model=os.getenv("ANTHROPIC_PROACTIVE_MODEL", "claude-haiku-4-5-20251001"),
            max_tokens=400,
            system=(
                "Ты — AI-снабженец. Получаешь контекст ситуации и предлагаешь "
                "0–3 ДОПОЛНИТЕЛЬНЫХ кнопок-действий, которых НЕТ в обычных "
                "правилах системы. Если ситуация стандартная — верни пустой "
                "массив. Используй только actions из whitelist. Отвечай ТОЛЬКО "
                "валидным JSON-массивом."
            ),
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
        # Cleanup: модели любят оборачивать в ```json
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text
            text = text.rsplit("```", 1)[0] if text.endswith("```") else text
        items = json.loads(text)
        if not isinstance(items, list):
            return []
        # Фильтр по whitelist
        out = []
        for it in items[:max_items]:
            if not isinstance(it, dict):
                continue
            action = it.get("action")
            label = it.get("label")
            if action not in PROACTIVE_WHITELIST or not label:
                continue
            out.append({
                "label": str(label)[:60],
                "action": action,
                "params": it.get("params") or {},
            })
        return out
    except Exception as exc:
        logger.warning("proactive_actions failed: %s", exc)
        return []


def _build_prompt(intent: str, context: dict, max_items: int) -> str:
    return (
        f"Контекст: пользователь выполнил «{intent}».\n"
        f"Факты:\n{json.dumps(context, ensure_ascii=False, indent=2)}\n\n"
        f"Whitelist действий: {sorted(PROACTIVE_WHITELIST)}\n\n"
        f"Задача: если в данных есть НЕСТАНДАРТНЫЙ нюанс (необычно высокая "
        f"цена, редкий бренд, нетипичный маршрут, особый статус поставщика), "
        f"предложи до {max_items} кнопок. Формат:\n"
        f'[{{"label":"...","action":"...","params":{{}}}}]\n'
        f"Если ничего нестандартного нет — верни []."
    )

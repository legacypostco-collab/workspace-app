"""Бизнес-правила лимитов заказа.

Сейчас одно правило: минимальная сумма заказа $7,000 USD.

Не работаем со сделками меньше — экономика консолидации не сходится
(фиксированные затраты на логистику, таможню, операторов).

Точки применения:
  • quick_order        (assistant/actions.py)
  • accept_quote       (assistant/negotiation.py)
  • kp_workflow.confirm_buy (assistant/kp_workflow.py)
  • present_kp_to_buyer — early warning при показе КП

Можно переопределить через env var MIN_ORDER_USD (для тестов / R&D).
"""
from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.utils.translation import gettext as _


def min_order_usd() -> Decimal:
    """Минимальная сумма заказа (USD). Можно переопределить через env."""
    raw = getattr(settings, "MIN_ORDER_USD", None) or "7000"
    try:
        return Decimal(str(raw))
    except Exception:
        return Decimal("7000")


def check_min_order(total_usd: Decimal | float | int):
    """Возвращает None если сумма >= лимита, иначе dict-ответ с card-warning.

    Используется в action-handler'ах:

        from .order_limits import check_min_order
        block = check_min_order(landed_total)
        if block:
            return ActionResult(block)
        # ...создаём Order
    """
    try:
        total = Decimal(str(total_usd))
    except Exception:
        return None  # неизвестная сумма — не блокируем (защита от false-positive)
    limit = min_order_usd()
    if total >= limit:
        return None
    shortage = limit - total
    limit_str = f"{limit:,.0f}"
    total_str = f"{total:,.2f}"
    shortage_str = f"{shortage:,.2f}"
    return {
        "text": (
            _("⚠️ Минимальная сумма заказа на платформе — $%(limit)s.\n"
              "Сейчас в корзине $%(total)s · не хватает $%(shortage)s.\n\n"
              "Добавьте позиции в спецификацию — экономика консолидации "
              "работает на партиях от $%(limit)s: фиксированные расходы "
              "на логистику, таможню и операторов перекрывают маржу на меньших объёмах.")
            % {"limit": limit_str, "total": total_str, "shortage": shortage_str}
        ),
        "cards": [{
            "type": "kpi_grid",
            "data": {
                "title": _("💰 Сумма заказа"),
                "items": [
                    {"label": _("Сейчас"),  "value": f"${total:,.0f}",  "tone": "warn"},
                    {"label": _("Минимум"), "value": f"${limit:,.0f}",  "tone": "info"},
                    {"label": _("Добрать"), "value": f"${shortage:,.0f}", "tone": "bad"},
                ],
            },
        }],
        "actions": [
            {"action": "search_parts", "label": _("🔍 Добавить позиции")},
            {"action": "upload_parts_list", "label": _("📎 Загрузить спецификацию")},
        ],
        "suggestions": [],
        "contextual_actions": [],
    }

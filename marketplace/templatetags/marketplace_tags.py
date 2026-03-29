from decimal import Decimal, InvalidOperation

from django import template

register = template.Library()


@register.filter
def money_usd(value):
    if value in (None, ""):
        return "-"
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        return value
    return f"${amount:,.2f}"

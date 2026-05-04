from django import template
from django.utils.translation import pgettext

register = template.Library()


@register.filter
def hours_to_dh(value):
    """Convert float hours to localized 'Xd Yh' / 'Yh' / 'Zm' (RU/EN/ZH aware)."""
    if value is None:
        return "—"
    try:
        total_minutes = round(float(value) * 60)
    except (TypeError, ValueError):
        return "—"
    # Translatable short units: pgettext with context "duration_short"
    d = pgettext("duration_short", "d")
    h = pgettext("duration_short", "h")
    m = pgettext("duration_short", "m")
    if total_minutes <= 0:
        return f"< 1{h}"
    days = total_minutes // (24 * 60)
    remaining_minutes = total_minutes % (24 * 60)
    hours = remaining_minutes // 60
    minutes = remaining_minutes % 60
    if days > 0:
        return f"{days}{d} {hours}{h}" if hours else f"{days}{d}"
    if hours > 0:
        return f"{hours}{h} {minutes}{m}" if minutes else f"{hours}{h}"
    return f"{minutes}{m}"

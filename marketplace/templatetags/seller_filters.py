from django import template

register = template.Library()


@register.filter
def hours_to_dh(value):
    """Convert float hours to human-readable 'Xд Yч' or 'Yч' or 'Zм'."""
    if value is None:
        return "—"
    try:
        total_minutes = round(float(value) * 60)
    except (TypeError, ValueError):
        return "—"
    if total_minutes <= 0:
        return "< 1ч"
    days = total_minutes // (24 * 60)
    remaining_minutes = total_minutes % (24 * 60)
    hours = remaining_minutes // 60
    minutes = remaining_minutes % 60
    if days > 0:
        return f"{days}д {hours}ч" if hours else f"{days}д"
    if hours > 0:
        return f"{hours}ч {minutes}м" if minutes else f"{hours}ч"
    return f"{minutes}м"

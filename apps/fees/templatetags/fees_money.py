from django import template

from apps.core.money import naira_display

register = template.Library()


@register.filter
def naira(kobo: int | None) -> str:
    """Integer kobo as ``₦1,234.56``."""
    return "" if kobo is None else naira_display(int(kobo))

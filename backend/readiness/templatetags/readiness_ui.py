"""Plain words for readiness codes on the readiness screens (01D J4-3)."""

from django import template

from readiness.views import equipment_label as _equipment_label

register = template.Library()


@register.filter
def equipment_label(name: str) -> str:
    """``restraint_kit`` -> ``Restraint kit``."""
    return _equipment_label(str(name))

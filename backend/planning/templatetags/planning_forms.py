"""``{% form_token %}``: a fresh one-time token for the form being drawn (see planning.plain)."""

from typing import Any

from django import template
from django.utils.html import format_html
from django.utils.safestring import SafeString

from planning.plain import issue_token

register = template.Library()


@register.simple_tag(takes_context=True)
def form_token(context: Any) -> SafeString:
    return format_html('<input type="hidden" name="form_token" value="{}">', issue_token(context["request"]))

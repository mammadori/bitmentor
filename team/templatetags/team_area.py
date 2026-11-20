"""For the pages of the volunteers' area (team/area.py)."""

from django import template

register = template.Library()


@register.filter
def short_addresses(text):
    """Every address in the text shortened as the notices write it (`ma…@provider`): the area never shows one whole.
    Plain text in, plain text out: the template escapes it."""
    from team.area import shortened

    return shortened(str(text or ""))

from django import template
from django.urls import reverse
from django.utils.safestring import mark_safe

from .. import board as boards, conf

register = template.Library()


# The texts of a meetup that come from its organisers (staff or trusted Nostr authors)
OUTSIDE_TEXTS = ("title", "city", "place", "zone")


def inert(entry):
    """A meetup for a community page: the page goes through Markdown after the template (website/views.py), so its
    organisers' texts must not become links or images there. They get the same treatment as a shop's texts
    (website.views.inert_text): readable, no longer syntax. On the board's own page they show the same."""
    from website.views import inert_text  # the views import the tags' app: not at load time

    return {**entry, **{name: inert_text(str(entry[name])) for name in OUTSIDE_TEXTS if entry.get(name)}}


def static_copy(context):
    request = context.get("request")
    return bool(request and request.headers.get("X-Static-Copy") == "1")


@register.inclusion_tag("meetups/board.html", takes_context=True)
def meetup_board(context, upcoming=None, past=None, level=2, full=False):
    """The meetups for a page of the community: `{% meetup_board %}` is the next ones and the last ones of the window;
    `upcoming=3 past=3` cuts them, `level=3` is the level of its headings (under a heading of the page's own, h2).
    `full=True` is the whole board for a page of its own: it says when nothing is announced and adds the link to the
    calendar and the note on the dates, and not the link to the board's page. Draws nothing without the `meetups` block,
    and nothing in the static copy of the site, whose dates would go stale."""
    if not conf.enabled() or static_copy(context):
        return {"empty": True}
    level = level if level in (2, 3, 4) else 2
    found = boards.board(upcoming, past)
    # The board is cached as it is: the copies are made inert here, for this page
    found = {**found, "upcoming": [inert(entry) for entry in found["upcoming"]],
             "past": [inert(entry) for entry in found["past"]]}
    return {"empty": False, "board": found, "open": mark_safe(f"<h{level}>"), "close": mark_safe(f"</h{level}>"),
            "page": reverse("meetups"), "full": full, "feed": reverse("meetups_feed")}


@register.inclusion_tag("meetups/next.html", takes_context=True)
def meetup_next(context):
    """One line: the next meetup that is on, with the link to the board. Nothing if there is none, or without the block."""
    if not conf.enabled() or static_copy(context):
        return {"next": None}
    found = boards.next_one()
    return {"next": inert(found) if found else None, "page": reverse("meetups")}

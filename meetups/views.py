from django.http import Http404, HttpResponse
from django.shortcuts import render
from django.utils.translation import get_language, gettext as _
from django.views.decorators.http import require_safe

from config import ratelimit
from config.community import page_path
from website.views import page as community_page

from . import conf, ical

FEED_PER_HOUR = 120


def page(request):
    """The public board: the next meetups and the last ones of the window. Without the `meetups` block there is no such
    page. It reads the database, at most once in a few minutes (board.CACHE_SECONDS); the static copy of the site (`X-Static-Copy`) gets the
    page without the list, because a copy is a photograph and the dates change."""
    # A community page of the same name takes the address, with the block or without it: it writes the board where it wants
    # with {% meetup_board %}
    if page_path(get_language(), "meetups").exists():
        return community_page(request, "meetups")
    if not conf.enabled():
        raise Http404
    static_copy = request.headers.get("X-Static-Copy") == "1"
    return render(request, "meetups/page.html", {
        "static_copy": static_copy, "page_title": _("Meetups"), "slug": "meetups",
        "page_description": _("Where and when the community meets: the next meetups and the last ones, with their group.")})


@require_safe
def feed(request):
    """The same meetups as an iCalendar file, for whoever wants them in their own calendar. Public, read only, cached for
    fifteen minutes, and limited per connection like the other public data of the site."""
    if not conf.enabled():
        raise Http404
    if ratelimit.over_limit(f"meetups-ics:{ratelimit.client_ip(request)}", FEED_PER_HOUR, 3600):
        return HttpResponse(status=429)
    response = HttpResponse(ical.calendar(), content_type="text/calendar; charset=utf-8")
    response["Cache-Control"] = f"public, max-age={ical.FEED_SECONDS}"
    response["Content-Disposition"] = 'inline; filename="meetups.ics"'
    response["X-Robots-Tag"] = "noindex"
    return response

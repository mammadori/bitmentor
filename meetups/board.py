"""What the board shows: the visible meetups of the window, the copies of one evening taken out, split into the next ones
and the last ones, and each one as the plain texts a page or a calendar needs."""
import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db import models
from django.utils import dateformat, formats, timezone
from django.utils.translation import get_language, gettext as _
from django.utils.translation import pgettext

from . import conf
from .models import Meetup

# A meeting that began less than this ago is still "next": it is on tonight
ONGOING = timedelta(hours=3)
# Two aggregators publish the same evening with times that differ by hours: within this, in the same city, it is one
SAME_EVENING = timedelta(hours=3)
MONTH = timedelta(days=30)
# A page never draws more than this for each list, nor the feed more events: the site keeps thousands, a screen does not
PAGE_LIMIT = 100
FEED_LIMIT = 1000
# More than is shown is read, for the copies of an evening that two authors published and collapse keeps once
MARGIN = 20
# The board is worked out once for a few minutes for everybody (a page of the site reads it, and so does the feed, and the
# table can hold thousands of rows); whatever changes the meetups makes the next request work it out again (forget)
CACHE_SECONDS = 300
STAMP_KEY = "meetups:stamp"


def stamp():
    """The mark that goes into every cache key of the board: `forget` changes it, and what was kept under the old one is
    never read again (and expires by itself)."""
    found = cache.get(STAMP_KEY)
    if found is None:
        cache.add(STAMP_KEY, secrets.token_hex(6), None)
        found = cache.get(STAMP_KEY)
    return found or ""


def forget():
    """The meetups changed: the board and the feed are worked out again at the next request."""
    cache.set(STAMP_KEY, secrets.token_hex(6), None)


def forget_on_change(sender, **kwargs):
    forget()


def cache_key(kind, *parts):
    return f"meetups:{kind}:" + hashlib.sha256("\x1f".join(map(str, (stamp(), *parts))).encode()).hexdigest()


def curated(meetup):
    """Written or corrected by the staff: it is never taken out as a copy of another."""
    return meetup.source == Meetup.Source.STAFF or meetup.staff_edited


def identity(meetup):
    return "staff" if meetup.source == Meetup.Source.STAFF else meetup.nostr_author


def collapse(meetups):
    """One evening published twice by two authors shows once. In the same city, within SAME_EVENING, the staff's own wins,
    then the one the staff corrected, then the newest publication; the other authors' copies go. Two meetups of the same
    author, or two the staff wrote or corrected, are never taken for copies: they were meant to be two."""
    # Without a city (a community with no `cities`) nobody knows where a meetup is: it is never taken for a copy
    kept, cluster = [m for m in meetups if not m.city.strip()], []
    meetups = [m for m in meetups if m.city.strip()]

    def close():
        if not cluster:
            return
        best = max(cluster, key=lambda m: (curated(m), m.source == Meetup.Source.STAFF, m.nostr_created_at or 0))
        kept.extend(m for m in cluster if curated(m) or identity(m) == identity(best))

    for meetup in sorted(meetups, key=lambda m: (m.city.casefold(), m.starts_at)):
        if cluster and (meetup.city.casefold() != cluster[0].city.casefold()
                        or meetup.starts_at - cluster[0].starts_at > SAME_EVENING):
            close()
            cluster = []
        cluster.append(meetup)
    close()
    return sorted(kept, key=lambda m: m.starts_at)


def status_text(meetup):
    if meetup.status == Meetup.Status.CANCELLED:
        return pgettext("meetup state", "Cancelled")
    if meetup.status == Meetup.Status.POSTPONED:
        if meetup.moved_to:
            return _("Postponed to %(date)s") % {"date": dateformat.format(meetup.moved_to, _("j F Y"))}
        return pgettext("meetup state", "Postponed")
    if meetup.status == Meetup.Status.INFORMAL:
        return pgettext("meetup state", "Informal get-together")
    return ""


def row(meetup, now, fallback_link):
    """A meetup as the texts to show, in the active language and in the zone of the meetup."""
    start = meetup.local_start
    home = timezone.get_default_timezone_name()
    return {
        "id": str(meetup.id), "start": start, "all_day": meetup.all_day,
        "iso": start.date().isoformat() if meetup.all_day else start.isoformat(timespec="minutes"),
        "weekday": dateformat.format(start, "l"), "date": dateformat.format(start, _("j F Y")),
        "day": start.day, "month": dateformat.format(start, "M"),
        "time": "" if meetup.all_day else formats.time_format(start, "TIME_FORMAT"),
        # The zone is said only when it is not the community's: a visitor reads the hour as local time
        "zone": "" if meetup.all_day or meetup.tzid == home else start.tzname() or meetup.tzid,
        "city": meetup.city, "place": meetup.place, "title": " ".join(filter(None, (conf.name(), meetup.city.strip()))) if conf.name() else meetup.title,
        "status": meetup.status, "status_text": status_text(meetup), "off": meetup.status in (Meetup.Status.CANCELLED, Meetup.Status.POSTPONED),
        "url": conf.safe_link(meetup.url) or fallback_link, "past": is_past(meetup, now),
    }


def is_past(meetup, now):
    if meetup.all_day:
        return meetup.starts_at < now.astimezone(timezone.get_default_timezone()).replace(hour=0, minute=0, second=0, microsecond=0)
    return meetup.starts_at < now - ONGOING


def board(upcoming=None, past=None, now=None):
    """{"upcoming": [...], "past": [...], "updated": datetime or None}. The next ones from the soonest, the last ones from
    the most recent; `upcoming` and `past` cut how many (None: up to PAGE_LIMIT). Kept for CACHE_SECONDS, by language and
    cut, unless `now` is given (the tests)."""
    if now is not None:
        return build(upcoming, past, now)
    language = get_language() or settings.LANGUAGE_CODE
    key = cache_key("board", language, upcoming, past, conf.past_months(), conf.default_link(language))
    found = cache.get(key)
    if found is None:
        found = build(upcoming, past, timezone.now())
        cache.set(key, found, CACHE_SECONDS)
    return found


def sides(now):
    """Two querysets over the visible meetups of the window: the ones still to come (the soonest first) and the past ones
    (the most recent first). The same cut as is_past, made by the database."""
    today = now.astimezone(timezone.get_default_timezone()).replace(hour=0, minute=0, second=0, microsecond=0)
    still = models.Q(all_day=False, starts_at__gte=now - ONGOING) | models.Q(all_day=True, starts_at__gte=today)
    visible = Meetup.objects.filter(visible=True, starts_at__gte=now - conf.past_months() * MONTH)
    return visible.filter(still).order_by("starts_at"), visible.exclude(still).order_by("-starts_at")


def build(upcoming, past, now):
    """The board as it is now. Only as many meetups are read from the table, and made into rows, as are shown (with a margin
    for the copies that collapse takes out): the table can hold thousands."""
    language = get_language() or settings.LANGUAGE_CODE
    wanted_ahead = PAGE_LIMIT if upcoming is None else upcoming
    wanted_behind = PAGE_LIMIT if past is None else past
    coming, gone = sides(now)
    ahead = collapse(list(coming[:wanted_ahead * 2 + MARGIN]))[:wanted_ahead] if wanted_ahead else []
    behind = collapse(list(gone[:wanted_behind * 2 + MARGIN]))[::-1][:wanted_behind] if wanted_behind else []
    link = conf.default_link(language)
    return {"upcoming": [row(meetup, now, link) for meetup in ahead], "past": [row(meetup, now, link) for meetup in behind],
            "updated": Meetup.objects.filter(visible=True, starts_at__gte=now - conf.past_months() * MONTH)
            .aggregate(latest=models.Max("updated"))["latest"],
            "more_upcoming": max(0, coming.count() - len(ahead)), "more_past": max(0, gone.count() - len(behind))}


def next_one(now=None):
    """The first meetup still to come that is happening, or None: the one line for a home page."""
    for entry in board(now=now)["upcoming"]:
        if not entry["off"]:
            return entry
    return None

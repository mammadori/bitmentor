"""The meetups as an iCalendar feed (RFC 5545), for whoever wants them in their own calendar. Plain data: when, where,
what and whether it is on. No person, no organiser, no description from outside, no counts."""
import re
from datetime import timezone as dt_timezone

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone
from django.utils.translation import gettext as _, override

from config.community import community, localized

from . import board, conf
from .models import Meetup

PRODID = "-//bitmentor//meetups//EN"
STAMP = "%Y%m%dT%H%M%SZ"
FEED_SECONDS = 900


def escape(text):
    """A TEXT value (RFC 5545 3.3.11): backslash, semicolon, comma and line breaks. The texts are already one line."""
    return re.sub(r"([\\;,])", r"\\\1", str(text)).replace("\r", " ").replace("\n", " ")


def fold(line):
    """Lines of at most 75 octets, the rest on the next line after a space; never in the middle of a character."""
    data = line.encode()
    if len(data) <= 75:
        return line
    out, current, size = [], "", 0
    for char in line:
        width = len(char.encode())
        if size + width > (75 if not out else 74):
            out.append(current)
            current, size = "", 0
        current += char
        size += width
    out.append(current)
    return "\r\n ".join(out)


def summary(meetup, row):
    base = meetup.title or (_("Meetup in %(city)s") % {"city": meetup.city} if meetup.city else _("Meetup"))
    return f"{row['status_text']}: {base}" if meetup.status in (Meetup.Status.CANCELLED, Meetup.Status.POSTPONED) else base


def event(meetup, row, host):
    lines = ["BEGIN:VEVENT", f"UID:{meetup.id}@{host}", f"DTSTAMP:{meetup.updated.astimezone(dt_timezone.utc).strftime(STAMP)}",
             f"LAST-MODIFIED:{meetup.updated.astimezone(dt_timezone.utc).strftime(STAMP)}"]
    if meetup.all_day:
        lines.append(f"DTSTART;VALUE=DATE:{meetup.local_start:%Y%m%d}")
    else:
        lines.append(f"DTSTART:{meetup.starts_at.astimezone(dt_timezone.utc).strftime(STAMP)}")
    lines.append(f"SUMMARY:{escape(summary(meetup, row))}")
    where = ", ".join(part for part in (meetup.place, meetup.city) if part)
    if where:
        lines.append(f"LOCATION:{escape(where)}")
    if row["url"]:
        lines.append(f"URL:{row['url']}")
    # A cancelled meeting, and the old day of a postponed one, are not on: calendars show them as such
    lines.append("STATUS:" + ("CANCELLED" if meetup.status in (Meetup.Status.CANCELLED, Meetup.Status.POSTPONED)
                              else "CONFIRMED"))
    if row["status_text"]:
        lines.append(f"DESCRIPTION:{escape(row['status_text'])}")
    lines.append("END:VEVENT")
    return lines


def calendar(now=None):
    """The whole feed as text, with CRLF line ends, in the community's first language. Kept for FEED_SECONDS like the board
    (unless `now` is given, the tests), and worked out again after any change of the meetups."""
    if now is not None:
        return build(now)
    key = board.cache_key("feed", settings.LANGUAGE_CODE, conf.past_months(), conf.default_link(settings.LANGUAGE_CODE))
    found = cache.get(key)
    if found is None:
        found = build(timezone.now())
        cache.set(key, found, FEED_SECONDS)
    return found


def chosen(now):
    """What goes into the feed: every meetup still to come (the soonest, if they are more than FEED_LIMIT by themselves) and,
    with the room that is left under that ceiling, the most recent past ones. Read from the table no further than that."""
    coming, gone = board.sides(now)
    ahead = board.collapse(list(coming[:board.FEED_LIMIT + board.MARGIN]))[:board.FEED_LIMIT]
    room = board.FEED_LIMIT - len(ahead)
    behind = board.collapse(list(gone[:room + board.MARGIN]))[-room:] if room > 0 else []
    return behind + ahead


def build(now):
    with override(settings.LANGUAGE_CODE):
        meetups = chosen(now)
        link = conf.default_link(settings.LANGUAGE_CODE)
        name = localized(community().get("name", ""), settings.LANGUAGE_CODE)
        lines = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:{PRODID}", "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
                 f"X-WR-CALNAME:{escape(_('Meetups of %(community)s') % {'community': name})}"]
        for meetup in meetups:
            lines += event(meetup, board.row(meetup, now, link), settings.SITE_HOST)
        lines.append("END:VCALENDAR")
    return "\r\n".join(fold(line) for line in lines) + "\r\n"

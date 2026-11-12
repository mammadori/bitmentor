"""Calendar events (NIP-52) from Nostr, turned into meetups.

The reader (`manage.py read_meetups`) asks the relays for the events of the certified organisers; what comes back is
data from outside and is trusted for nothing until it passes this module. Every event is checked before it is read:

- the shape and the size (a few kilobytes, a few hundred short tags), then whether the author is on the list of the
  trusted ones (`trusted_authors()`), and only then the signature (NIP-01: id recomputed, Schnorr signature);
- the kind: 31922 (a day) and 31923 (a moment) become meetups, 5 (NIP-09) cancels them, anything else is ignored;
- the dates: a start that is not a number or a day, or is before 2009 or years ahead, rejects the event;
- the text: title and place become plain text (tags and entities removed, no control or direction characters, cut to
  the length of the field), never HTML; the images and the description are ignored; a link is kept only if the
  community asked for it (`event_links`) and it is an https address.

An event is addressable (NIP-33): the key is (kind, author, `d`) and the newest version replaces the older. A meetup the
staff corrected by hand is never overwritten, and a deletion cancels a meetup that has not happened yet (a deletion
after the date is a clean-up of the relay, not a cancellation). Nothing here connects to a relay: meetups/relays.py does."""
import html
import math
import re
import unicodedata
import zoneinfo
from collections import Counter
from datetime import date, datetime, time, timedelta
from datetime import timezone as dt_timezone

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from config import limits
from shops.nostr import decode_geohash, verified

from . import board, conf
from .models import Meetup, TrustedAuthor

DATE_KIND, TIME_KIND, DELETION_KIND = 31922, 31923, 5
CALENDAR_KINDS = (DATE_KIND, TIME_KIND)
MAX_EVENT_CHARS = 32 * 1024
MAX_TAGS = 200
MAX_TAG_ITEMS = 12
MAX_TAG_TEXT = 2000
MAX_CONTENT = 20000
MAX_EVENTS = 3000
MAX_NEW_PER_RUN = 200
FUTURE_SLACK = 24 * 3600
EARLIEST = datetime(2009, 1, 3, tzinfo=dt_timezone.utc)
YEARS_AHEAD = 3
YEARS_BACK = 3
HEX64 = re.compile(r"[0-9a-f]{64}")
HEX128 = re.compile(r"[0-9a-f]{128}")
ZONE_NAME = re.compile(r"[A-Za-z0-9_+\-]+(?:/[A-Za-z0-9_+\-]+){0,2}")
# The words with which an organiser writes in the title that a meeting is off (the aggregators do, by hand)
CANCELLED_WORDS = re.compile(r"(?<!\w)(?:annullat[oae]|cancell?ed|abgesagt|cancelad[oa]|annul[ée]e?)(?!\w)", re.I)
MARKUP = re.compile(r"<[^<>]*>")
REASONS = {
    "malformed": "not an event as NIP-01 writes it", "too_big": "too big", "future": "created in the future",
    "untrusted": "author not on the list", "unsigned": "wrong id or signature", "no_d": "no usable `d` tag",
    "bad_start": "start that is not a date", "absurd_date": "start before 2009 or years away", "too_old": "too long ago",
    "outside": "not in the cities of the community", "limit": "over the limit of one reading",
}


class Rejected(Exception):
    """The event is not used; `reason` is a key of REASONS."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def plain_text(value, limit):
    """Text from outside as one line of plain text: entities decoded once, tags dropped, control, format and private
    characters (the ones that hide or reverse text) dropped, blanks collapsed, cut to `limit`. What is left is shown by
    the templates, which escape it anyway."""
    if not isinstance(value, str):
        return ""
    text = MARKUP.sub(" ", html.unescape(value))
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)  # a tag between a word and its comma left a blank before it
    text = "".join(char for char in text if char.isspace() or unicodedata.category(char) not in ("Cc", "Cf", "Cs", "Co", "Cn"))
    return re.sub(r"\s+", " ", text).strip()[:limit].strip()


def trusted_authors():
    """{key: note} of the authors the reader trusts: the ones in `meetups.trusted` and the active ones the staff added."""
    found = {author["pubkey"]: author["note"] for author in conf.trusted()}
    for author in TrustedAuthor.objects.filter(active=True):
        found.setdefault(author.pubkey, author.note)
    return found


KEYS = ("id", "pubkey", "created_at", "kind", "tags", "content", "sig")


def shape(event, now):
    """A copy of the event with the seven fields NIP-01 has, all of a sane type and size; Rejected otherwise. Whatever else
    the event carries is dropped here, never looked at: it cannot be an id, a signature or a way to make the parser
    work."""
    if not isinstance(event, dict):
        raise Rejected("malformed")
    for key, pattern in (("id", HEX64), ("pubkey", HEX64), ("sig", HEX128)):
        if not isinstance(event.get(key), str) or not pattern.fullmatch(event[key]):
            raise Rejected("malformed")
    created_at, kind, tags, content = event.get("created_at"), event.get("kind"), event.get("tags"), event.get("content")
    if any(isinstance(value, bool) or not isinstance(value, int) for value in (created_at, kind)) or created_at <= 0 \
            or not isinstance(content, str) or not isinstance(tags, list):
        raise Rejected("malformed")
    if len(content) > MAX_CONTENT or len(tags) > MAX_TAGS:
        raise Rejected("too_big")
    size = len(content)
    for tag in tags:
        if not isinstance(tag, list) or not 1 <= len(tag) <= MAX_TAG_ITEMS or any(not isinstance(item, str) for item in tag):
            raise Rejected("malformed")
        if any(len(item) > MAX_TAG_TEXT for item in tag):
            raise Rejected("too_big")
        size += sum(len(item) for item in tag)
    if size > MAX_EVENT_CHARS:
        raise Rejected("too_big")
    if created_at > now.timestamp() + FUTURE_SLACK:
        raise Rejected("future")
    return {key: event[key] for key in KEYS}


def candidate(event, trusted, now):
    """A clean copy of an event that is well formed, of a kind we read and from a trusted author; Rejected otherwise. Not
    yet its signature: that costs the most, and `ingest` checks it only for the version of each event it will use."""
    event = shape(event, now)
    if event["kind"] not in (*CALENDAR_KINDS, DELETION_KIND):
        raise Rejected("malformed")
    if event["pubkey"] not in trusted:
        raise Rejected("untrusted")
    return event


def check(event, trusted, now):
    """The event if it is a candidate and signed by its author; Rejected otherwise."""
    event = candidate(event, trusted, now)
    if not verified(event):
        raise Rejected("unsigned")
    return event


def tag_values(tags, name):
    return [tag[1] for tag in tags if len(tag) > 1 and tag[0] == name]


def position_of(tags):
    """(latitude, longitude) from the most precise `g` tag (a geohash), or from `lat` and `lon`; None without either."""
    best = None
    for value in tag_values(tags, "g"):
        found = decode_geohash(value.lower())
        if found and (best is None or len(value) > best[0]):
            best = (len(value), found)
    if best:
        return best[1]
    try:
        latitude, longitude = float(tag_values(tags, "lat")[0]), float(tag_values(tags, "lon")[0])
    except (IndexError, ValueError):
        return None
    return (latitude, longitude) if math.isfinite(latitude) and math.isfinite(longitude) \
        and abs(latitude) <= 90 and abs(longitude) <= 180 else None


def kilometres(one, other):
    (lat1, lon1), (lat2, lon2) = ((math.radians(a), math.radians(b)) for a, b in (one, other))
    half = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 12742 * math.asin(math.sqrt(half))


def city_of(places, position, text):
    """The community's city the event is in: the nearest one within its radius from the position, or, when the event has no
    position, the one the text names. None if there is none."""
    if position:
        near = [(kilometres(position, (place["latitude"], place["longitude"])), place) for place in places]
        near = [(distance, place) for distance, place in near if distance <= place["radius_km"]]
        return min(near, key=lambda pair: pair[0])[1] if near else None
    for place in places:
        if re.search(rf"(?<!\w){re.escape(place['name'])}(?!\w)", text, re.I):
            return place
    return None


def zone_of(name, default):
    """The zone an event names with `start_tzid`; the community's own for a name that is not one."""
    if isinstance(name, str) and len(name) <= 64 and ZONE_NAME.fullmatch(name):
        try:
            return zoneinfo.ZoneInfo(name)
        except (zoneinfo.ZoneInfoNotFoundError, ValueError, OSError):
            pass
    return default


def parse(event, now, places, links):
    """The fields of a meetup from a checked calendar event; Rejected if it cannot be one."""
    tags = event["tags"]
    kind = event["kind"]
    d = next(iter(tag_values(tags, "d")), "")
    if not d or len(d) > 200 or not d.isprintable():
        raise Rejected("no_d")
    start = next(iter(tag_values(tags, "start")), "")
    default_zone = zoneinfo.ZoneInfo(settings.TIME_ZONE)
    if kind == TIME_KIND:
        if not re.fullmatch(r"[0-9]{1,11}", start):
            raise Rejected("bad_start")
        moment = datetime.fromtimestamp(int(start), dt_timezone.utc)
        zone, all_day = zone_of(next(iter(tag_values(tags, "start_tzid")), None), default_zone), False
    else:
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", start):
            raise Rejected("bad_start")
        try:
            day = date.fromisoformat(start)
        except ValueError:
            raise Rejected("bad_start") from None
        zone, all_day = default_zone, True
        moment = datetime.combine(day, time(0), tzinfo=zone)
    if moment < EARLIEST or moment > now + timedelta(days=365 * YEARS_AHEAD):
        raise Rejected("absurd_date")
    if moment < now - timedelta(days=365 * YEARS_BACK):
        raise Rejected("too_old")
    location = plain_text(next(iter(tag_values(tags, "location")), ""), 200)
    title = plain_text(next(iter(tag_values(tags, "title") or tag_values(tags, "name")), ""), 200)
    city, place = "", location
    if places:
        found = city_of(places, position_of(tags), f"{location} {title}")
        if found is None:
            raise Rejected("outside")
        city = found["name"]
        # "Place, Street 7, City": the city is the board's own column, so it is not said twice
        place = re.sub(rf"[,;\s-]*(?<!\w){re.escape(city)}(?!\w)\s*$", "", location, flags=re.I).strip(" ,;-") or location
    link = ""
    if links:
        link = next((safe for safe in map(conf.safe_link, tag_values(tags, "r")) if safe), "")
    return {"address": f"{kind}:{event['pubkey']}:{d}", "starts_at": moment.astimezone(dt_timezone.utc), "all_day": all_day,
            "tzid": zone.key, "city": city[:100], "place": place[:200], "title": title, "url": link,
            "status": Meetup.Status.CANCELLED if CANCELLED_WORDS.search(title) else Meetup.Status.CONFIRMED}


def deletion_targets(event):
    """What a NIP-09 deletion points at: the addresses of calendar events of its own author and the ids of events."""
    addresses, ids = set(), set()
    for tag in event["tags"]:
        if len(tag) < 2:
            continue
        if tag[0] == "a":
            parts = tag[1].split(":", 2)
            if len(parts) == 3 and parts[0] in map(str, CALENDAR_KINDS) and parts[1] == event["pubkey"]:
                addresses.add(tag[1])
        elif tag[0] == "e" and HEX64.fullmatch(tag[1]):
            ids.add(tag[1])
    return addresses, ids


class Report:
    """What a reading did, in numbers and in the reasons for what was left out."""

    def __init__(self):
        self.seen = self.created = self.updated = self.cancelled = self.kept = self.older = 0
        self.rejected = Counter()

    @property
    def rejected_total(self):
        return sum(self.rejected.values())

    def detail(self):
        lines = [f"{REASONS[reason]}: {count}" for reason, count in sorted(self.rejected.items())]
        if self.kept:
            lines.append(f"left alone because the staff corrected them: {self.kept}")
        return "\n".join(lines)


def address_key(event):
    return (event["kind"], event["pubkey"], next(iter(tag_values(event["tags"], "d")), ""))


def verified_newest(versions, report):
    """The newest version of an event whose signature holds, or None. A relay that sends a newer version nobody signed does
    not push out the one the author did: each unsigned one is counted and the next is tried."""
    for event in sorted(versions, key=lambda event: (-event["created_at"], event["id"])):
        if verified(event):
            return event
        report.rejected["unsigned"] += 1
    return None


def ingest(events, now=None):
    """Applies the events from the relays to the meetups and tells what came of it (a Report). Nothing from outside is
    believed before it is checked; one event that is wrong never stops the others. The copies that several relays send of
    the same event count once, and only after that does the ceiling of one reading (MAX_EVENTS events, the newest) apply."""
    now = now or timezone.now()
    report = Report()
    trusted = trusted_authors()
    places, links = conf.cities(), conf.event_links()
    calendar, deletions = {}, {}
    for event in events:
        report.seen += 1
        try:
            event = candidate(event, trusted, now)
        except Rejected as rejected:
            report.rejected[rejected.reason] += 1
            continue
        if event["kind"] == DELETION_KIND:
            deletions.setdefault(event["id"], []).append(event)
        else:
            calendar.setdefault(address_key(event), []).append(event)
    chosen = []
    for groups in (calendar, deletions):
        ordered = sorted(groups.values(), key=lambda versions: max(event["created_at"] for event in versions), reverse=True)
        if len(ordered) > MAX_EVENTS:
            report.rejected["limit"] += len(ordered) - MAX_EVENTS
        chosen.append([found for found in (verified_newest(versions, report) for versions in ordered[:MAX_EVENTS]) if found])
    stored = Meetup.objects.filter(source=Meetup.Source.NOSTR).count()
    # limits.nostr_meetups (0: no limit); at the limit nothing new is kept and nothing is deleted
    room = max(0, limits.get("nostr_meetups") - stored) if limits.get("nostr_meetups") else MAX_NEW_PER_RUN
    budget = min(MAX_NEW_PER_RUN, room)
    for event in sorted(chosen[0], key=lambda event: event["created_at"]):
        try:
            fields = parse(event, now, places, links)
        except Rejected as rejected:
            report.rejected[rejected.reason] += 1
            continue
        budget -= store(event, fields, now, report, budget)
    for event in sorted(chosen[1], key=lambda event: event["created_at"]):
        report.cancelled += withdraw(event, now)
    if report.created or report.updated or report.cancelled:
        board.forget()
    return report


def store(event, fields, now, report, budget):
    """Creates or updates the meetup of an event; returns 1 if a new one was made (for the budget of the reading)."""
    held = Meetup.objects.filter(nostr_address=fields["address"]).first()
    values = {key: value for key, value in fields.items() if key != "address"}
    values.update(nostr_event_id=event["id"], nostr_created_at=event["created_at"])
    if held is None:
        if budget <= 0:
            report.rejected["limit"] += 1
            return 0
        try:
            with transaction.atomic():
                Meetup.objects.create(source=Meetup.Source.NOSTR, nostr_address=fields["address"],
                                      nostr_author=event["pubkey"], **values)
        except IntegrityError:  # another reading made it between the question and now
            return 0
        report.created += 1
        return 1
    if held.staff_edited:
        report.kept += 1
    elif held.nostr_created_at is not None and event["created_at"] <= held.nostr_created_at:
        report.older += 1
    # The staff may have corrected it since it was read: the update only reaches it while it is still the reader's
    elif Meetup.objects.filter(pk=held.pk, staff_edited=False).update(updated=now, **values):
        report.updated += 1
    else:
        report.kept += 1
    return 0


def withdraw(event, now):
    """A deletion by a trusted author cancels their meetups that have not happened yet, if the staff did not correct them
    and no newer version was published after the deletion. Returns how many it cancelled."""
    addresses, ids = deletion_targets(event)
    if not addresses and not ids:
        return 0
    target = Q(nostr_address__in=addresses) | Q(nostr_event_id__in=ids)
    return Meetup.objects.filter(target, source=Meetup.Source.NOSTR, nostr_author=event["pubkey"], staff_edited=False,
                                 starts_at__gt=now, nostr_created_at__lte=event["created_at"]) \
        .exclude(status=Meetup.Status.CANCELLED).update(status=Meetup.Status.CANCELLED, updated=now)

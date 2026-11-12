"""The meetup board turns on with the `meetups` block in community.yml: without it, or empty, the pages answer 404 and the
tags in the pages draw nothing.

The format is next to the block in examples/community/community.yml and in docs/features/meetups.md. A value that cannot
be used is left out, the reason goes to `warn` and the board stays up."""
import math
import re
from urllib.parse import urlsplit

from django.conf import settings

from config import weburl
from config.community import community, localized

from .keys import to_hex

DEFAULT_PAST_MONTHS = 12
PAST_MONTHS_LIMITS = (1, 36)
MAX_LINK = 500
MAX_RELAYS = 8
MAX_TRUSTED = 100
MAX_CITIES = 50
DEFAULT_RADIUS_KM = 15
RADIUS_LIMITS = (1, 200)
RELAY_URL = re.compile(r"wss?://[A-Za-z0-9.\-\[\]:]+(/[A-Za-z0-9._~\-/%]*)?")


def options():
    data = community().get("meetups")
    return data if isinstance(data, dict) else {}


def enabled():
    return bool(options())


def safe_link(value):
    """An address a visitor can follow: https, a server, no user name or password, no spaces or control characters and
    nothing outside ASCII (an address with other letters is written percent-encoded). Anything else is not a link."""
    if not isinstance(value, str) or not value or len(value) > MAX_LINK or not value.isascii() or not value.isprintable() \
            or any(char in value for char in " \\\"'<>`"):
        return ""
    return value if weburl.parts(value, "https") else ""


def past_months(warn=None):
    value = options().get("past_months", DEFAULT_PAST_MONTHS)
    low, high = PAST_MONTHS_LIMITS
    if isinstance(value, int) and not isinstance(value, bool) and low <= value <= high:
        return value
    if warn:
        warn(f"`meetups.past_months` must be a whole number from {low} to {high}: {value!r}; {DEFAULT_PAST_MONTHS} counts instead")
    return DEFAULT_PAST_MONTHS


def default_link(language, warn=None):
    """The group or page that stands in for a meetup's own link: `meetups.link`, one address or one per language."""
    value = options().get("link")
    if value is None:
        return ""
    link = safe_link(localized(value, language))
    if not link and warn:
        warn(f"`meetups.link` must be an https:// address (one, or one per language), without spaces: {value!r}")
    return link


def event_links():
    """Whether the links inside Nostr events (the `r` tags) are shown. Off unless the block says `event_links: true`: the
    link is whatever the publisher chose, an app or a shop, and the community decides if it wants it on its page."""
    return options().get("event_links") is True


def name():
    """The name the community gives its meetups (`name: Satoshi Spritz`). When set, the board shows it with the city instead
    of the titles the organisers write in their events, which differ from one to the next."""
    value = options().get("name")
    return value.strip() if isinstance(value, str) else ""


def own_relays():
    """The relays of the site's own configuration (`NOSTR_RELAYS`, where `publish_nostr` writes): trusted as they are,
    plain `ws://` and internal addresses included, because the operator wrote them."""
    return list(settings.NOSTR_RELAYS)


def relays(warn=None):
    """Where the reader looks: `meetups.relays`, or without it the site's own relays. A relay that is not the site's own
    must be `wss://` and cannot be written with an address (see meetups/relays.py, which also checks where the name
    leads)."""
    data = options().get("relays")
    if data is None:
        return own_relays()
    listed = data if isinstance(data, list) else [data]
    found = []
    for relay in listed:
        if relay in own_relays():
            found.append(relay)
            continue
        if not isinstance(relay, str) or not RELAY_URL.fullmatch(relay) or not relay.startswith("wss://"):
            if warn:
                warn(f"`meetups.relays` takes wss:// addresses of relays, not {relay!r}")
            continue
        host = urlsplit(relay).hostname or ""
        if re.fullmatch(r"[0-9.]+|.*:.*", host):
            if warn:
                warn(f"`meetups.relays`: a relay is written with its name, not its address: {relay!r}")
            continue
        if relay not in found:
            found.append(relay)
    if len(found) > MAX_RELAYS:
        if warn:
            warn(f"`meetups.relays` reads at most {MAX_RELAYS} relays: the others are left out")
        found = found[:MAX_RELAYS]
    return found


def trusted(warn=None):
    """The authors named in the block: `[{npub: npub1…, note: who certified them}]`. The staff adds more in the
    administration (`TrustedAuthor`); both count."""
    data = options().get("trusted")
    if data is None:
        return []
    found = {}
    for entry in data if isinstance(data, list) else [data]:
        try:
            if not isinstance(entry, dict) or not isinstance(entry.get("npub"), str):
                raise ValueError("write it as `- npub: npub1…` with a `note`")
            note = entry.get("note")
            if not isinstance(note, str) or not note.strip():
                raise ValueError("a `note` says who certified this author, and is needed")
            found.setdefault(to_hex(entry["npub"]), note.strip()[:300])
        except ValueError as error:
            if warn:
                warn(f"`meetups.trusted`: {entry!r} is left out: {error}")
    if len(found) > MAX_TRUSTED:
        if warn:
            warn(f"`meetups.trusted` has at most {MAX_TRUSTED} authors: the others are left out")
        found = dict(list(found.items())[:MAX_TRUSTED])
    return [{"pubkey": key, "note": note} for key, note in found.items()]


def number(entry, name, low, high):
    value = entry.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or math.isnan(value) or not low <= value <= high:
        raise ValueError(f"`{name}` must be a number from {low} to {high}")
    return float(value)


def cities(warn=None):
    """The places the board is about: `[{name, latitude, longitude, radius_km}]`. An event from Nostr is kept only if it is
    within `radius_km` of one of them (or, without a position, names one); without any city every event from a trusted
    author is kept."""
    data = options().get("cities")
    if data is None:
        return []
    found = []
    for entry in data if isinstance(data, list) else [data]:
        try:
            if not isinstance(entry, dict) or not isinstance(entry.get("name"), str) or not entry["name"].strip() \
                    or len(entry["name"]) > 100:
                raise ValueError("write it as `- name: …` with `latitude` and `longitude`")
            radius = entry.get("radius_km", DEFAULT_RADIUS_KM)
            found.append({"name": entry["name"].strip(), "latitude": number(entry, "latitude", -90, 90),
                          "longitude": number(entry, "longitude", -180, 180),
                          "radius_km": number({"radius_km": radius}, "radius_km", *RADIUS_LIMITS)})
        except ValueError as error:
            if warn:
                warn(f"`meetups.cities`: {entry!r} is left out: {error}")
    if len(found) > MAX_CITIES:
        if warn:
            warn(f"`meetups.cities` has at most {MAX_CITIES} places: the others are left out")
        found = found[:MAX_CITIES]
    return found


def problems():
    """Everything in the block that is left out, for the checks. Asked in every language for the link."""
    found = []
    if not enabled():
        return found
    past_months(found.append)
    event_links()
    relays(found.append)
    trusted(found.append)
    cities(found.append)
    for code, _name in settings.LANGUAGES:
        link_problems = []
        default_link(code, link_problems.append)
        found += [problem for problem in link_problems if problem not in found]
    if "meetups" in community() and not isinstance(community().get("meetups"), (dict, type(None))):
        found.append("`meetups` is a list of fields, not a value")
    return found

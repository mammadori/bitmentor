"""OpenStreetMap in both directions: the shops of the community and their entries on the map everybody can edit.

`manage.py osm_sync` (once a month, bitmentor-osm@<project>.timer) READS OpenStreetMap through Overpass and:
- PROPOSES a link between a shop and its element (`Shop.osm_proposed`) when the name is alike and the place is close, and
  nothing else is: the association confirms it in /admin/ (`Shop.osm_ref`). A doubtful match is never proposed: it goes in
  the report. Anybody can put a place next to a shop on OpenStreetMap, so nothing is read from an element, or written for
  it, before the confirmation;
- for a confirmed link, fills the address, the city and the position (inside the area) a shop has EMPTY, never a field
  somebody wrote, and OFFERS the website and the phone for the association to accept; everything it writes is in the
  report and remembered (`Shop.osm_notes`), so that nothing is filled twice;
- lists the elements that accept bitcoin and are not ours (candidates, never created by themselves);
- works out which tags OpenStreetMap should have from what this site knows (accepts bitcoin or not, Lightning and
  on-chain, the community's discount tag, the day the shop's Lightning address last worked, the operator), and what differs: the
  PROPOSALS. Between this site and OpenStreetMap the dates decide: a more recent check there is never overwritten.

Nothing here writes to OpenStreetMap. That is `manage.py osm_upload`, run by hand by a person with their own token
(docs/features/openstreetmap.md, "Automatic edits"). The reading, the matching and the proposals are pure functions on
what Overpass said, so they are tested with made-up answers; the requests are in shops/osm_net.py.

Everything Overpass says is written by strangers: the answer is read field by field (`parse`), a value is used only
after it passed the rules of the field it would fill, and a name that reaches a message goes through `alert_text`."""
import ipaddress
import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from functools import lru_cache
from urllib.parse import urlsplit

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.translation import gettext as _
from django.utils.translation import ngettext
from urllib3.util import parse_url

from config import features
from config.community import community, localized

from . import certified, details
from .directory import metres_between, waits_for_wallet
from .forms import PRIVATE_NAMES
from .models import OSM_REF, OsmRun, Shop, is_demo, real

# The tags that tell OpenStreetMap a place takes bitcoin: any element with one of them is read
BITCOIN_KEYS = ("currency:XBT", "payment:bitcoin", "payment:lightning", "payment:onchain")
ACCEPTS = "currency:XBT"
LIGHTNING = "payment:lightning"
ONCHAIN = "payment:onchain"
LEGACY = "payment:bitcoin"
DATE = "check_date:currency:XBT"

DEFAULT_OVERPASS = "https://overpass-api.de/api/interpreter"
DEFAULT_API = "https://api.openstreetmap.org/api/0.6"
DEFAULT_SIMILARITY = 0.85
DEFAULT_METRES = 100
DEFAULT_SECONDS = 60
DEFAULT_BYTES = 8 * 1024 * 1024
LARGEST_BYTES = 32 * 1024 * 1024
LONGEST_SECONDS = 180
# The area is a few cities or a region, not a country: Overpass would refuse it, and a typo would ask for the world
LARGEST_SPAN = 20
DEFAULT_COMMENT = "Update bitcoin payment tags"

# What an answer can hold before it is not an answer: a region has hundreds of such places, a typo in the area thousands
MAX_ELEMENTS = 20000
MAX_TAGS = 300
MAX_TEXT = 255
MAX_NODES = 2000
MAX_MEMBERS = 5000
MAX_ID = 10**15
# Doubtful pairs and candidates listed in a report: the rest is counted
LISTED = 60
CANDIDATES_KEPT = 500
# A name that is only part of another one ("Centrale" in "Bar Centrale") counts as this much alike, if it is a word
CONTAINED = 0.9
# A pair this far (in times the distance that makes a safe match) is still listed, for a person to look at
FAR = 5
KEEP_RUNS = 12
# Readings that failed kept apart from the ones that worked
KEEP_FAILED = 5
# A reading with fewer than half the elements of the last one, when that had at least this many, is not trusted
SMALLEST_FALL = 10
# Older than this many days without a reading that worked: the dashboard says so
LATE_DAYS = 45

ADD, UPDATE, REVIEW = "add", "update", "review"
TAG_KEY = re.compile(r"^[A-Za-z0-9_:.-]{1,100}$")
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f  ]")
REF = re.compile(OSM_REF)
PHONE = re.compile(r"^\+?[0-9][0-9 ()./-]{2,38}$")
PERCENT = re.compile(r"^\s*([0-9]{1,3}(?:[.,][0-9]+)?)\s*%?\s*$")


class OsmError(Exception):
    """OpenStreetMap, or the answer, cannot be used: the message is one line, safe to print and to keep (no address, no
    token, no part of the answer except a short remark)."""


# --- Settings ---------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Settings:
    bbox: tuple
    overpass_url: str = DEFAULT_OVERPASS
    api_url: str = DEFAULT_API
    discount_tag: str = ""
    operator_tag: str = ""
    operator_value: str = ""
    similarity: float = DEFAULT_SIMILARITY
    metres: int = DEFAULT_METRES
    auto_link: bool = True
    comment: object = DEFAULT_COMMENT
    seconds: int = DEFAULT_SECONDS
    max_bytes: int = DEFAULT_BYTES

    @property
    def tags(self):
        """The tags a change can touch: those listed here and no other, whatever a proposal says (osm_upload checks)."""
        found = {ACCEPTS, LIGHTNING, ONCHAIN, LEGACY, DATE}
        found |= {tag for tag in (self.discount_tag, self.operator_tag) if tag}
        return found


def number(value, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if low <= value <= high else None


# The servers the token of the account that signs the edits may go to: the editing API of OpenStreetMap and its server for
# tests, by exact name. Not "anything under openstreetmap.org": a name is read in different ways by different parsers
API_HOSTS = ("api.openstreetmap.org", "master.apis.dev.openstreetmap.org")
HOST_CHARS = re.compile(r"[a-z0-9.-]+\Z")
UNPLAIN = re.compile(r"[\\%\s\x00-\x1f\x7f-\x9f\u2028\u2029]")


def plain_host(url):
    """The host `requests` would connect to for `url`, or "" when the address is not plain enough to be sure of it. It is read
    with urllib3's parser, the one `requests` uses, and with urlsplit, and the two must agree: `https://evil.example\\.openstreetmap.org`
    is a host under openstreetmap.org for one and `evil.example` for the other. A backslash, a percent sign, a space or a
    control character anywhere in the address, or a host with anything but letters, digits, dots and hyphens, is not plain."""
    if not isinstance(url, str):
        return ""
    # Only the part before the first slash, question mark or hash: a query may hold a percent sign (Overpass's does)
    authority = re.match(r"[A-Za-z][A-Za-z0-9+.-]*://([^/?#]*)", url)
    if authority is None or UNPLAIN.search(authority[1]):
        return ""
    try:
        theirs = (parse_url(url).host or "").lower()
        ours = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    return theirs if theirs == ours and HOST_CHARS.match(theirs) else ""


def api_server(url):
    """The address is the editing API of OpenStreetMap (or its test server): the only place the token is sent."""
    return plain_host(url) in API_HOSTS


def https_url(value, name, say):
    """A server of OpenStreetMap's, as `community.yml` writes it: https, a plain name, the default port, nothing private.
    The requests check again what the address leads to (shops/osm_net.py)."""
    if not isinstance(value, str):
        say(f"`osm.{name}` must be an https:// address")
        return None
    parts = urlsplit(value.strip())
    host = plain_host(value.strip())
    try:
        port = parts.port
    except ValueError:
        port = -1
    try:
        literal = not ipaddress.ip_address(host).is_global
    except ValueError:
        literal = False
    if (parts.scheme != "https" or not host or port not in (None, 443) or parts.username or parts.password
            or parts.query or parts.fragment or host.endswith(PRIVATE_NAMES) or host == "localhost" or literal
            or CONTROL.search(value) or UNPLAIN.search(value)):
        say(f"`osm.{name}` must be an https:// address on a public server, with a plain name, no port, query or password: {value!r}")
        return None
    return value.strip().rstrip("/") if name == "api_url" else value.strip()


def tag_name(value, name, say):
    if value is None:
        return ""
    if not isinstance(value, str) or not TAG_KEY.match(value):
        say(f"`osm.{name}` must be a tag name like currency:XBT:discount: {value!r}")
        return ""
    return value


def settings_from(data, warn=None):
    """The `osm` section of `community.yml` as Settings, or None when there is none (or it says `false`): the whole
    feature is off then. A section without a usable area is off too, and says why. Any other field that cannot be
    used is left at its default and told to `warn`. The format is in docs/features/openstreetmap.md."""
    say = warn or (lambda problem: None)
    if data is None or data is False:
        return None
    if not isinstance(data, dict):
        say("`osm` is not a list of fields")
        return None
    box = data.get("bbox")
    if (not isinstance(box, (list, tuple)) or len(box) != 4
            or any(number(value, -180, 180) is None for value in box)):
        say("`osm.bbox` must be four numbers: south, west, north, east (degrees)")
        return None
    south, west, north, east = (float(value) for value in box)
    if not (-90 <= south < north <= 90 and -180 <= west < east <= 180) or north - south > LARGEST_SPAN or east - west > LARGEST_SPAN:
        say(f"`osm.bbox` must be south < north and west < east, within {LARGEST_SPAN} degrees each way: {box!r}")
        return None
    found = {"bbox": (south, west, north, east)}
    for key, default in (("overpass_url", DEFAULT_OVERPASS), ("api_url", DEFAULT_API)):
        if data.get(key) is not None:
            found[key] = https_url(data[key], key, say) or default
            if key == "api_url" and not api_server(found[key]):
                say(f"`osm.api_url` must be {' or '.join(API_HOSTS)}: the token of the account that signs the edits goes there: {data[key]!r}")
                found[key] = default
    found["discount_tag"] = tag_name(data.get("discount_tag"), "discount_tag", say)
    operator = data.get("operator")
    if operator is not None:
        tag, value = (operator.get("tag"), operator.get("value")) if isinstance(operator, dict) else (None, None)
        if (not isinstance(tag, str) or not TAG_KEY.match(tag) or not isinstance(value, str) or not value.strip()
                or len(value) > MAX_TEXT or CONTROL.search(value) or ";" in value):
            say("`osm.operator` must be `{tag: payment:bitcoin:operator, value: the-operator.example}`: "
                "one value, no `;`")
        else:
            found["operator_tag"], found["operator_value"] = tag, value.strip()
    for key, default, low, high in (("name_similarity", DEFAULT_SIMILARITY, 0.5, 1), ("distance_metres", DEFAULT_METRES, 5, 2000)):
        if data.get(key) is not None:
            value = number(data[key], low, high)
            if value is None:
                say(f"`osm.{key}` must be a number between {low} and {high}: {data[key]!r}")
            found["similarity" if key == "name_similarity" else "metres"] = default if value is None else value
    if data.get("auto_link") is not None:
        if isinstance(data["auto_link"], bool):
            found["auto_link"] = data["auto_link"]
        else:
            say("`osm.auto_link` must be true or false")
    comment = data.get("comment")
    if comment is not None:
        texts = comment.values() if isinstance(comment, dict) else [comment]
        if not texts or not all(isinstance(text, str) and text.strip() and len(text) <= COMMENT_OWN and not CONTROL.search(text) for text in texts):
            say(f"`osm.comment` must be a line of text ({COMMENT_OWN} characters at most: the site adds where the data come from), or one per language")
        else:
            found["comment"] = comment
    for key, name, low, high in (("timeout", "seconds", 5, LONGEST_SECONDS), ("max_megabytes", "max_bytes", 1, LARGEST_BYTES // 2**20)):
        if data.get(key) is not None:
            value = number(data[key], low, high)
            if value is None:
                say(f"`osm.{key}` must be a number between {low} and {high}: {data[key]!r}")
            else:
                found[name] = int(value) * (2**20 if key == "max_megabytes" else 1)
    return Settings(**found)


def settings(warn=None):
    """The community's settings for OpenStreetMap, or None while the section is missing: the commands, the dashboard and
    the merchant's page do nothing then."""
    return settings_from(community().get("osm"), warn)


COMMENT_LIMIT = 255
COMMENT_OWN = 150


def changeset_comment(config, language=None):
    """The changeset's comment: the community's line, and a sentence that says where the data come from, which is from the
    Lightning addresses this site checks, not from somebody who went there (OpenStreetMap's limit for a comment is 255)."""
    from django.conf import settings as django_settings

    base = str(localized(config.comment, language or django_settings.LANGUAGE_CODE)).strip().rstrip(".")
    sentence = f". Data from the Lightning addresses checked by {django_settings.SITE_HOST}."
    return base[:COMMENT_LIMIT - len(sentence)] + sentence


# --- Reading what Overpass said ---------------------------------------------------------------------------------------

@dataclass
class Element:
    ref: str
    tags: dict
    lat: float | None = None
    lon: float | None = None
    # The coordinates as the server wrote them: osmChange gives a node back exactly as it was
    text_lat: str = ""
    text_lon: str = ""
    version: int | None = None
    nodes: list | None = None
    members: list | None = None
    # Something in it was not what OpenStreetMap writes (a tag too long or with a line break, a coordinate out of its bounds,
    # no version): what was wrong is left out and the rest is used for reading, but the element is never given back to the
    # server, which would make the edit take away what was left out, and it is never linked or matched
    unreliable: bool = False

    @property
    def position(self):
        return None if self.lat is None or self.lon is None else (self.lat, self.lon)

    @property
    def names(self):
        found = []
        for key in ("name", "official_name", "brand", "alt_name"):
            found += [part.strip() for part in self.tags.get(key, "").split(";") if part.strip()]
        return found


def reject_constant(text):
    raise ValueError(text)


def load(body):
    """The JSON of an answer, or OsmError. NaN and Infinity are not JSON; decimals stay decimals (a coordinate keeps the
    digits it came with); a document nested past Python's limit, or a number the arithmetic cannot hold, is an answer that
    is not right, not a crash."""
    try:
        return json.loads(body, parse_float=Decimal, parse_constant=reject_constant)
    except (ValueError, RecursionError, MemoryError, ArithmeticError):
        raise OsmError("the answer is not JSON") from None


def clean_text(value):
    return isinstance(value, str) and len(value) <= MAX_TEXT and not CONTROL.search(value)


# A coordinate has at most seven decimals in OpenStreetMap: nine are kept, and a number smaller than this is zero
COORDINATE_STEP = Decimal("1e-9")
COORDINATE_DECIMALS = -9
COORDINATE_ZERO = -12


def coordinate(value, high):
    """A latitude or longitude as (float, exact text), or None. The text has nine decimals at most: `1e-999999999` is a valid
    JSON number, and written out in full it would take a gigabyte."""
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    try:
        exact = Decimal(repr(value)) if isinstance(value, float) else Decimal(value)
        if not exact.is_finite() or abs(exact) > high:
            return None
        if exact and exact.adjusted() < COORDINATE_ZERO:
            exact = Decimal(0).quantize(COORDINATE_STEP)
        elif exact.as_tuple().exponent < COORDINATE_DECIMALS:
            exact = exact.quantize(COORDINATE_STEP)
        return float(exact), format(exact, "f")
    except (InvalidOperation, ArithmeticError, MemoryError):
        return None


def parse(data, meta=False):
    """The elements of an Overpass answer as (list of Element, number left out). OsmError when the answer cannot be the
    whole truth: not an object, too many elements, or a `remark` (Overpass answers 200 with a remark and part of the
    data when it ran out of time or memory, and a partial list would say that shops left OpenStreetMap). An element with
    no usable type or id is left out and counted. One with a bad part (a tag, a coordinate) is kept with that part left out
    and marked `unreliable`: it exists, so a shop linked to it is not "not found". With `meta` an element also needs what
    osmChange asks: its version, and the nodes of a way or the members of a relation."""
    if not isinstance(data, dict):
        raise OsmError("the answer is not an object")
    remark = data.get("remark")
    if remark is not None:
        text = CONTROL.sub("?", str(remark))[:80]
        raise OsmError(f"Overpass sent a remark instead of the whole answer: {text}")
    listed = data.get("elements")
    if not isinstance(listed, list):
        raise OsmError("the answer has no list of elements")
    if len(listed) > MAX_ELEMENTS:
        raise OsmError(f"more than {MAX_ELEMENTS} elements: the area is too big")
    found, skipped, seen = [], 0, set()
    try:
        for raw in listed:
            element = one_element(raw, meta)
            if element is None or element.ref in seen:
                skipped += 1
                continue
            seen.add(element.ref)
            found.append(element)
    except (MemoryError, ArithmeticError):
        raise OsmError("a number in the answer is out of range") from None
    return found, skipped


def one_element(raw, meta):
    if not isinstance(raw, dict) or raw.get("type") not in ("node", "way", "relation"):
        return None
    number_id = raw.get("id")
    if isinstance(number_id, bool) or not isinstance(number_id, int) or not 0 < number_id < MAX_ID:
        return None
    element = Element(f"{raw['type']}/{number_id}", {})
    tags = raw.get("tags")
    if isinstance(tags, dict):
        for key, value in tags.items():
            if not key or not clean_text(key) or not clean_text(value) or len(element.tags) >= MAX_TAGS:
                element.unreliable = True
            else:
                element.tags[key] = value
    else:
        element.unreliable = True
    if not element.tags:
        element.unreliable = True
    where = raw if raw["type"] == "node" else raw.get("center")
    if isinstance(where, dict) and "lat" in where and "lon" in where:
        lat, lon = coordinate(where["lat"], 90), coordinate(where["lon"], 180)
        if lat is None or lon is None:
            element.unreliable = True
        else:
            (element.lat, element.text_lat), (element.lon, element.text_lon) = lat, lon
    elif raw["type"] == "node":
        element.unreliable = True
    if meta:
        version = raw.get("version")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            element.unreliable = True
        else:
            element.version = version
        if raw["type"] == "way":
            nodes = raw.get("nodes")
            if (not isinstance(nodes, list) or not 2 <= len(nodes) <= MAX_NODES
                    or not all(isinstance(node, int) and not isinstance(node, bool) and 0 < node < MAX_ID for node in nodes)):
                element.unreliable = True
            else:
                element.nodes = list(nodes)
        elif raw["type"] == "relation":
            members = raw.get("members")
            kept = []
            for member in members if isinstance(members, list) and 1 <= len(members) <= MAX_MEMBERS else []:
                if (not isinstance(member, dict) or member.get("type") not in ("node", "way", "relation")
                        or isinstance(member.get("ref"), bool) or not isinstance(member.get("ref"), int)
                        or not 0 < member["ref"] < MAX_ID or not clean_text(member.get("role", ""))):
                    kept = []
                    break
                kept.append({"type": member["type"], "ref": member["ref"], "role": member.get("role", "")})
            if kept:
                element.members = kept
            else:
                element.unreliable = True
    return element


# --- Names, places, dates ---------------------------------------------------------------------------------------------

@lru_cache(maxsize=65536)
def plain_name(text):
    """A name without capitals, accents or punctuation, words one space apart: what two spellings of a name share."""
    text = unicodedata.normalize("NFKD", str(text).casefold())
    text = "".join(char for char in text if not unicodedata.combining(char))
    return " ".join(re.sub(r"[\W_]+", " ", text).split())


def similarity(left, right):
    """How alike two names are, from 0 to 1: letters in common, or a whole word of one inside the other."""
    left, right = plain_name(left), plain_name(right)
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    matcher = SequenceMatcher(None, left, right, autojunk=False)
    ratio = matcher.ratio() if matcher.real_quick_ratio() >= 0.3 and matcher.quick_ratio() >= 0.3 else 0.0
    words_left, words_right = set(left.split()), set(right.split())
    short = left if len(words_left) <= len(words_right) else right
    if (words_left < words_right or words_right < words_left) and len(short) >= 4:
        ratio = max(ratio, CONTAINED)
    return ratio


def best_similarity(shop, element):
    return max((similarity(shop.name, name) for name in element.names), default=0.0)


def distance(shop, element):
    """Metres between the shop and the element, or None when either has no position."""
    if shop.latitude is None or shop.longitude is None or element.position is None:
        return None
    return metres_between((shop.latitude, shop.longitude), element.position)


def parse_day(text):
    """The day an OpenStreetMap date means: 2025-06-27, or a month or a year alone (its first day). None for the rest."""
    match = re.fullmatch(r"\s*([0-9]{4})(?:-([0-9]{2})(?:-([0-9]{2}))?)?\s*", text or "")
    if not match:
        return None
    try:
        return date(int(match[1]), int(match[2] or 1), int(match[3] or 1))
    except ValueError:
        return None


# Where OpenStreetMap keeps the day somebody looked: the one for bitcoin, the one for each payment method, and the two that
# say it for the whole place. Any of them is a check, and the most recent is the day OpenStreetMap vouches for
DATE_KEYS = (DATE, "check_date", "survey:date")


def osm_day(tags):
    """The most recent day OpenStreetMap says somebody checked this place: the latest of check_date:currency:XBT,
    check_date:payment:*, check_date and survey:date, or None."""
    days = [parse_day(value) for key, value in tags.items() if key in DATE_KEYS or key.startswith("check_date:payment:")]
    return max((day for day in days if day), default=None)


def percent(text):
    """The discount a tag says, as a number, or None: '10%', '10 %', '10'."""
    match = PERCENT.match(text or "")
    if not match:
        return None
    value = Decimal(match[1].replace(",", "."))
    return int(value) if value == value.to_integral_value() else value


def says(tags, key):
    """What a yes/no tag says: "yes", "no", or "" when it is missing; anything else is "other"."""
    value = tags.get(key, "").strip().lower()
    return value if value in ("", "yes", "no") else "other"


def accepts(element):
    """OpenStreetMap says this place takes bitcoin."""
    return says(element.tags, ACCEPTS) == "yes" or any(says(element.tags, key) == "yes" for key in (LIGHTNING, ONCHAIN, LEGACY))


def refuses(element):
    """OpenStreetMap says this place takes no bitcoin: `currency:XBT=no`, or, with no `currency:XBT` at all, a payment method
    that says no and none that says yes (every method at no, or a single `payment:lightning=no`)."""
    tags = element.tags
    if says(tags, ACCEPTS) == "no":
        return True
    return says(tags, ACCEPTS) == "" and not accepts(element) and any(says(tags, key) == "no" for key in (LIGHTNING, ONCHAIN, LEGACY))


# --- Matching a shop and an element -----------------------------------------------------------------------------------

@dataclass
class Matches:
    safe: list = field(default_factory=list)
    ambiguous: list = field(default_factory=list)
    unsure: list = field(default_factory=list)


def match(shops, elements, config):
    """Which unlinked shop is which unlinked element. SAFE: a shop and an element with a name this alike (`similarity`)
    and this close (`metres`), and no other element that alike and close to the shop, no other shop to the element.
    AMBIGUOUS: more than one of those, listed and never linked. UNSURE: a name alike but the place far, or the shop
    without a position: for a person to look at, never linked. Pairs are (shop, element, similarity, metres)."""
    strong, weak = {}, {}
    for shop in shops:
        if not shop.name.strip():
            continue
        for element in elements:
            if element.unreliable:
                continue
            metres = distance(shop, element)
            # Far enough that nothing would be listed: the names are not even compared (a region has thousands of elements)
            if metres is not None and metres > config.metres * FAR:
                continue
            alike = best_similarity(shop, element)
            if alike < config.similarity:
                continue
            if metres is not None and metres <= config.metres:
                strong.setdefault(shop.pk, []).append((shop, element, alike, metres))
            elif metres is None or metres <= config.metres * FAR:
                weak.setdefault(shop.pk, []).append((shop, element, alike, metres))
    by_element = {}
    for pairs in strong.values():
        for pair in pairs:
            by_element.setdefault(pair[1].ref, []).append(pair)
    found = Matches()
    for pairs in strong.values():
        if len(pairs) == 1 and len(by_element[pairs[0][1].ref]) == 1:
            found.safe.append(pairs[0])
        else:
            found.ambiguous.append(sorted(pairs, key=lambda pair: (-pair[2], pair[3])))
    for pk, pairs in weak.items():
        if pk not in strong:
            found.unsure.append(min(pairs, key=lambda pair: (-pair[2], pair[3] if pair[3] is not None else 10**9)))
    found.safe.sort(key=lambda pair: pair[0].name.lower())
    found.ambiguous.sort(key=lambda pairs: pairs[0][0].name.lower())
    found.unsure.sort(key=lambda pair: pair[0].name.lower())
    return found


# --- Filling our empty fields -----------------------------------------------------------------------------------------

URL_RULES = URLValidator(schemes=["http", "https"])
# What the reading fills by itself, for a link the association confirmed: the address, the town and the position (inside the
# area). What it only OFFERS, for the association to accept: the website and the phone, which a stranger can write to send
# customers somewhere else.
AUTO_FIELDS = ("address", "city", "position")
CONTACT_FIELDS = ("website", "phone")
# What became of a value: written by the reading, offered and waiting, accepted by the association, or declined
FILLED, OFFERED, ACCEPTED, DECLINED = "filled", "offered", "accepted", "declined"


def website_of(tags):
    for key in ("website", "contact:website", "url"):
        for part in tags.get(key, "").split(";")[:1]:
            part = part.strip()
            if part and len(part) <= Shop._meta.get_field("website").max_length:
                try:
                    URL_RULES(part)
                except ValidationError:
                    continue
                return part
    return ""


def phone_of(tags):
    for key in ("phone", "contact:phone", "mobile", "contact:mobile"):
        for part in tags.get(key, "").split(";")[:1]:
            part = part.strip()
            if PHONE.match(part):
                return part
    return ""


def address_of(tags):
    street = (tags.get("addr:street") or tags.get("addr:place") or "").strip()
    if not street:
        return ""
    text = f"{street} {tags.get('addr:housenumber', '').strip()}".strip()
    return text if len(text) <= Shop._meta.get_field("address").max_length else ""


def inside(bbox, position):
    """A position within the community's area (south, west, north, east)."""
    south, west, north, east = bbox
    return position is not None and south <= position[0] <= north and west <= position[1] <= east


def notes_of(shop):
    """What the reading remembers of a shop, as {"rejected": [refs], "fields": {name: {"value", "state"}}}."""
    notes = shop.osm_notes if isinstance(shop.osm_notes, dict) else {}
    rejected = notes.get("rejected")
    fields = notes.get("fields")
    decisions = notes.get("decisions")
    proposal = notes.get("proposal")
    return {"rejected": list(rejected) if isinstance(rejected, list) else [],
            "fields": {name: dict(entry) for name, entry in (fields.items() if isinstance(fields, dict) else []) if isinstance(entry, dict)},
            "decisions": [dict(item) for item in decisions if isinstance(item, dict)] if isinstance(decisions, list) else [],
            "proposal": dict(proposal) if isinstance(proposal, dict) else {}}


DECISIONS_KEPT = 50


def decided(notes, what, by, now=None, **more):
    """Writes down who decided what and when (the association's confirmations, refusals, acceptances): the shop's notes keep
    the last DECISIONS_KEPT, for whoever asks later why a field says what it says."""
    notes["decisions"].append({"what": what, "by": str(by)[:150], "at": (now or timezone.now()).isoformat(), **more})
    notes["decisions"] = notes["decisions"][-DECISIONS_KEPT:]


def offers_of(shop):
    """The values OpenStreetMap offered and nobody accepted or declined yet: {field: value}."""
    return {name: entry["value"] for name, entry in notes_of(shop)["fields"].items()
            if entry.get("state") == OFFERED and name in CONTACT_FIELDS and isinstance(entry.get("value"), str)}


def fillable(shop, element, config):
    """What the element could give to the fields this shop has empty: {field: value}, `position` as (latitude, longitude)
    and only if it falls inside the area. Nothing for a field with a value, whoever wrote it, and nothing for a field the
    reading already filled or the association already took or declined (see `notes_of`): an owner who emptied a field on
    purpose does not see it filled again. Contacts come with the same rule, but are offered, not written."""
    found = {}
    tags = element.tags
    if element.unreliable:
        return found
    if not shop.address.strip() and (address := address_of(tags)):
        found["address"] = address
    city = tags.get("addr:city", "").strip()
    if not shop.city.strip() and city and len(city) <= Shop._meta.get_field("city").max_length:
        found["city"] = city
    if not shop.website.strip() and (website := website_of(tags)):
        found["website"] = website
    if not shop.phone.strip() and (phone := phone_of(tags)):
        found["phone"] = phone
    if shop.latitude is None and shop.longitude is None and inside(config.bbox, element.position):
        found["position"] = (round(element.lat, 7), round(element.lon, 7))
    memory = notes_of(shop)["fields"]
    kept = {}
    for name, value in found.items():
        entry = memory.get(name)
        if entry and (entry.get("state") in (FILLED, ACCEPTED) or entry.get("value") == (list(value) if name == "position" else value)
                      and entry.get("state") in (OFFERED, DECLINED)):
            continue
        kept[name] = value
    return kept


def remember(notes, name, value, state):
    notes["fields"][name] = {"value": list(value) if isinstance(value, tuple) else value, "state": state}


def fill(shop, element, config):
    """Writes the address, the town and the position the element gives to the fields this shop has empty, a field at a time
    and only if it is still empty when the write happens (the owner may be saving the same shop), and notes the value
    written. The website and the phone are not written: they are offered, and wait for the association. Returns
    (written, offered), each {field: value}, the values as they went in. A conditional UPDATE, not a save: nothing else of the
    shop is rewritten."""
    found = fillable(shop, element, config)
    notes = notes_of(shop)
    written, offered = {}, {}
    for name in ("address", "city"):
        if name in found:
            remember(notes, name, found[name], FILLED)
            if Shop.objects.filter(pk=shop.pk, **{name: ""}).update(**{name: found[name], "osm_notes": notes}):
                written[name] = found[name]
            else:
                notes["fields"].pop(name, None)
    if "position" in found:
        lat, lon = found["position"]
        remember(notes, "position", found["position"], FILLED)
        if Shop.objects.filter(pk=shop.pk, latitude=None, longitude=None).update(latitude=lat, longitude=lon, osm_notes=notes):
            written["position"] = [lat, lon]
        else:
            notes["fields"].pop("position", None)
    for name in CONTACT_FIELDS:
        if name in found:
            remember(notes, name, found[name], OFFERED)
            offered[name] = found[name]
    if written or offered:
        Shop.objects.filter(pk=shop.pk).update(osm_notes=notes)
        shop.osm_notes = notes
    return written, offered


def accept_contacts(shop, by=""):
    """The association accepts what OpenStreetMap offered (the website, the phone): written to the fields that are still
    empty, and remembered, with who accepted and when, so that it is not offered again. A field that has a value by now
    declines the offer."""
    notes = notes_of(shop)
    written = {}
    for name, value in offers_of(shop).items():
        if Shop.objects.filter(pk=shop.pk, **{name: ""}).update(**{name: value}):
            remember(notes, name, value, ACCEPTED)
            written[name] = value
        else:
            remember(notes, name, value, DECLINED)
    if written:
        decided(notes, "accepted contacts", by, fields=written)
    Shop.objects.filter(pk=shop.pk).update(osm_notes=notes)
    shop.osm_notes = notes
    return written


def decline_contacts(shop, by=""):
    notes = notes_of(shop)
    declined = offers_of(shop)
    for name, value in declined.items():
        remember(notes, name, value, DECLINED)
    if declined:
        decided(notes, "declined contacts", by, fields=declined)
    Shop.objects.filter(pk=shop.pk).update(osm_notes=notes)
    shop.osm_notes = notes
    return declined


def propose_link(shop, element, alike=None, metres=None):
    """Puts the element next to the shop as a link waiting for the association (`Shop.osm_proposed`), if the shop has neither
    a link nor a proposal and the association never refused this element, and keeps what the association needs to look at it
    (the element's name, how alike it is, how far). False when somebody got there first."""
    notes = notes_of(shop)
    if element.ref in notes["rejected"]:
        return False
    notes["proposal"] = {"ref": element.ref, "name": (element.names or [""])[0][:120],
                         "similarity": None if alike is None else round(alike, 2), "metres": None if metres is None else round(metres)}
    done = bool(Shop.objects.filter(pk=shop.pk, osm_ref="", osm_proposed="").update(osm_proposed=element.ref, osm_notes=notes))
    if done:
        shop.osm_proposed, shop.osm_notes = element.ref, notes
    return done


def confirm_link(shop, by="", now=None):
    """The association confirms the proposed link: it becomes the shop's link, with who confirmed it and when written in the
    notes. False when there is nothing proposed, the shop has a link already, or another shop got the element."""
    if not shop.osm_proposed or shop.osm_ref:
        return False
    notes = notes_of(shop)
    proposal = notes.pop("proposal")
    decided(notes, "confirmed link", by, now, ref=shop.osm_proposed, **{key: value for key, value in proposal.items() if key != "ref"})
    try:
        with transaction.atomic():
            done = bool(Shop.objects.filter(pk=shop.pk, osm_ref="", osm_proposed=shop.osm_proposed).update(
                osm_ref=shop.osm_proposed, osm_proposed="", osm_seen=None, osm_notes=notes))
    except IntegrityError:
        return False
    if done:
        shop.osm_ref, shop.osm_proposed, shop.osm_notes = shop.osm_proposed, "", notes
    return done


def reject_link(shop, by=""):
    """The association refuses the proposed link: it is cleared and never proposed again for this shop."""
    if not shop.osm_proposed:
        return False
    notes = notes_of(shop)
    if shop.osm_proposed not in notes["rejected"]:
        notes["rejected"].append(shop.osm_proposed)
    notes.pop("proposal", None)
    decided(notes, "refused link", by, ref=shop.osm_proposed)
    done = bool(Shop.objects.filter(pk=shop.pk, osm_proposed=shop.osm_proposed).update(osm_proposed="", osm_notes=notes))
    if done:
        shop.osm_proposed, shop.osm_notes = "", notes
    return done


# --- What OpenStreetMap should say ------------------------------------------------------------------------------------

def standing(shop, today):
    """What this site knows about the shop, for OpenStreetMap: ("yes", the day its Lightning address last worked or None), ("no",
    the day it closed), or None when it says nothing (a made-up shop, suspended, not listed, waiting for its wallet:
    nothing we know is proof of anything there). A closing date still to come is a shop that still takes bitcoin."""
    if is_demo(shop) or shop.suspended or not shop.listed or waits_for_wallet(shop):
        return None
    if shop.closed_on is not None and shop.closed_on < today:
        return "no", shop.closed_on
    last = shop.lightning_worked
    return "yes", certified.local_day(last) if last else None


def methods(shop):
    """The payment tags a shop of ours says yes to: Lightning with a register, on-chain when its owner says so (and
    on-chain at the register, an experimental feature, is on: off, the site says nothing of on-chain)."""
    return [*([LIGHTNING] if shop.has_register else []),
            *([ONCHAIN] if shop.onchain and features.enabled(features.ONCHAIN) else [])]


def operator_values(text):
    """The operators a tag lists: OpenStreetMap writes several with `;`. Empty ones and spaces left out."""
    return [part.strip() for part in text.split(";") if part.strip()]


def changes_for(shop, element, config, today):
    """The tags to change on `element` so that it says what this site knows of `shop`: a list of (tag, old, new, kind),
    `new` None for a tag to take away. KIND: ADD (the tag is missing), UPDATE (a value to refresh or correct, or an operator
    to list next to the others), REVIEW (a person decides: it turns a "no" into a "yes", or touches a discount somebody
    else wrote).

    Who wins when the two differ is a matter of dates. This site's date is the day the shop's Lightning address last
    worked (or the day the shop closed); OpenStreetMap's is the most recent of its checks (`osm_day`). A check there more recent than ours is never
    overwritten, and after a "no" like that nothing at all is proposed for the shop: tags that go with a place that does not
    take bitcoin any more would be a lie. A tag that is missing is added whatever the dates say, as long as nothing says no."""
    stand = standing(shop, today)
    if stand is None or element.unreliable:
        return []
    state, ours = stand
    tags = element.tags
    theirs = osm_day(tags)
    wins = ours is not None and (theirs is None or ours > theirs)
    found = []

    def add(tag, new, kind=None):
        old = tags.get(tag)
        if old == new:
            return
        found.append((tag, old, new, kind or (ADD if old is None else UPDATE)))

    if state == "no":
        if accepts(element) and wins:
            add(ACCEPTS, "no", UPDATE)
            for key in (LIGHTNING, ONCHAIN, LEGACY):
                if says(tags, key) == "yes":
                    add(key, "no", UPDATE)
            add(DATE, ours.isoformat())
            if config.discount_tag and config.discount_tag in tags:
                add(config.discount_tag, None, UPDATE)
        return found

    said_no = refuses(element)
    if said_no and not wins:
        # They checked after our address last worked (or it never did) and say it takes no bitcoin: it is theirs to say
        return []
    flipping = said_no
    wanted = [(ACCEPTS, "yes"), *((key, "yes") for key in methods(shop))]
    for tag, value in wanted:
        now = says(tags, tag)
        if now == "yes":
            continue
        if now == "":
            add(tag, value)
        elif wins:
            # "no" (or something else) there, and our address worked after their check: a person decides
            flipping = True
            add(tag, value, REVIEW)
    if config.discount_tag:
        mine = shop.discount
        theirs_discount = tags.get(config.discount_tag)
        if mine > 0:
            if theirs_discount is None or percent(theirs_discount) != mine:
                add(config.discount_tag, f"{mine}%")
        elif theirs_discount is not None:
            add(config.discount_tag, None, REVIEW)
    if config.operator_tag and config.operator_value:
        current = tags.get(config.operator_tag)
        values = operator_values(current or "")
        if config.operator_value.lower() not in (value.lower() for value in values):
            # Ours next to the others, theirs kept: operators come and go, and a place may have several
            add(config.operator_tag, ";".join([*values, config.operator_value]))
    if ours is not None and (theirs is None or ours > theirs):
        add(DATE, ours.isoformat())
    if flipping:
        # A shop turned from "no" to "yes" is for a person to read as a whole: no tag that goes with it goes alone
        found = [(tag, old, new, REVIEW) for tag, old, new, _kind in found]
    return found


def wanted_tags(shop, config, today):
    """Every tag this site would write for a shop that is not on OpenStreetMap yet, or whose entry is not read: {tag: value},
    in the order the merchant's page shows them. Empty for a shop that says nothing. The shop's name and what describes
    the place (website, phone, the town) are there for whoever adds it; the category (a bakery, a bar) the site does not
    know, and the merchant chooses it in the editor."""
    stand = standing(shop, today)
    if stand is None or stand[0] != "yes":
        return {}
    found = {}
    if shop.name.strip():
        found["name"] = shop.name
    found[ACCEPTS] = "yes"
    for key in methods(shop):
        found[key] = "yes"
    if config.discount_tag and shop.discount > 0:
        found[config.discount_tag] = f"{shop.discount}%"
    if stand[1] is not None:
        found[DATE] = stand[1].isoformat()
    if config.operator_tag and config.operator_value:
        found[config.operator_tag] = config.operator_value
    # Only what the owner shows (shops/details.py): a phone or a website kept hidden on the site is not written on a public
    # map either. The town is not a detail the owner hides.
    shown = details.shown(shop)
    for tag, value in (("website", shown.get("website", "")), ("phone", shown.get("phone", "")), ("addr:city", shop.city)):
        if value.strip():
            found[tag] = value.strip()
    return found


# --- One reading ------------------------------------------------------------------------------------------------------

def view(shop, element, **more):
    return {"shop": str(shop.pk), "name": shop.name[:120], "ref": element.ref, **more}


def synchronize(config, elements, skipped=0, now=None, apply=True, previous=None):
    """The whole reading, as a report (a dict of plain values for OsmRun.report). With `apply` it proposes the links that
    are beyond doubt (if `auto_link`: the association confirms them in /admin/), and for the shops whose link is
    confirmed it fills the address, the town and the position they have empty, offers the website and the phone, and
    stamps `osm_seen`; without `apply` it only says what it would do. `previous` is the report of the last reading that
    was told, to say which candidates are new."""
    now = now or timezone.now()
    today = certified.local_day(now)
    shops = list(real(Shop.objects.all()).order_by("name", "pk"))
    by_ref = {element.ref: element for element in elements}
    report = {"version": 2, "created": now.isoformat(), "area": list(config.bbox), "elements": len(elements), "skipped": skipped,
              "unreliable_elements": sum(1 for element in elements if element.unreliable), "link_proposals": [], "suggested": [],
              "waiting_links": 0, "filled": [], "offers": [], "missing": [], "unreliable": [], "ambiguous": [], "unsure": [],
              "candidates": [], "new_candidates": [], "osm_says_no": [], "newer_on_osm": [], "proposals": [], "not_on_osm": 0}

    pairs = {}
    free = []
    waiting = set()
    for shop in shops:
        if shop.osm_ref:
            if shop.osm_ref in by_ref:
                pairs[shop.pk] = (shop, by_ref[shop.osm_ref])
            else:
                report["missing"].append({"shop": str(shop.pk), "name": shop.name[:120], "ref": shop.osm_ref})
            continue
        if shop.osm_proposed:
            proposed = by_ref.get(shop.osm_proposed)
            if proposed is not None and not proposed.unreliable:
                waiting.add(shop.osm_proposed)
                report["waiting_links"] += 1
                continue
            if apply:
                # The element is gone from the reading: the proposal with it
                Shop.objects.filter(pk=shop.pk, osm_proposed=shop.osm_proposed).update(osm_proposed="")
        free.append(shop)
    taken = {shop.osm_ref for shop in shops if shop.osm_ref} | waiting
    matches = match(free, [element for element in elements if element.ref not in taken], config)

    for shop, element, alike, metres in matches.safe:
        if element.ref in notes_of(shop)["rejected"]:
            continue
        item = view(shop, element, osm_name=element.names[0][:120] if element.names else "", similarity=round(alike, 2),
                    metres=round(metres))
        if apply and config.auto_link and propose_link(shop, element, alike, metres):
            report["link_proposals"].append(item)
        elif not (apply and config.auto_link):
            report["suggested"].append(item)
    for options in matches.ambiguous[:LISTED]:
        report["ambiguous"].append({"shop": str(options[0][0].pk), "name": options[0][0].name[:120],
                                    "options": [{"ref": element.ref, "name": (element.names or [""])[0][:120],
                                                 "metres": round(metres)} for _shop, element, _alike, metres in options[:5]]})
    for shop, element, alike, metres in matches.unsure[:LISTED]:
        report["unsure"].append(view(shop, element, osm_name=(element.names or [""])[0][:120], similarity=round(alike, 2),
                                     metres=None if metres is None else round(metres),
                                     why="no_position" if metres is None else "far"))

    seen = []
    for shop, element in pairs.values():
        seen.append(shop.pk)
        if element.unreliable:
            # Not given back to the server, nothing proposed on a half-read element: for a person to look at
            report["unreliable"].append(view(shop, element))
            continue
        if apply:
            written, offered = fill(shop, element, config)
        else:
            found = fillable(shop, element, config)
            written = {name: value for name, value in found.items() if name in AUTO_FIELDS}
            offered = {name: value for name, value in found.items() if name in CONTACT_FIELDS}
        if written:
            report["filled"].append(view(shop, element, values=written))
        if offered:
            report["offers"].append(view(shop, element, values=offered))
        found = changes_for(shop, element, config, today)
        stand = standing(shop, today)
        if found:
            report["proposals"].append(view(shop, element, changes=[
                {"tag": tag, "old": old, "new": new, "kind": kind} for tag, old, new, kind in found]))
        if stand is not None:
            ours, theirs = stand[1], osm_day(element.tags)
            item = {"state": stand[0], "ours": ours.isoformat() if ours else "", "theirs": theirs.isoformat() if theirs else ""}
            if stand[0] == "yes" and refuses(element):
                report["osm_says_no"].append(view(shop, element, **item))
            # Checked there after our address last worked (or it never did) and still saying yes: it takes bitcoin without
            # our registers. For a shop we closed: still yes after the day we closed it
            if accepts(element) and theirs is not None and (ours is None or theirs > ours):
                report["newer_on_osm"].append(view(shop, element, **item))
    if apply and seen:
        Shop.objects.filter(pk__in=seen).update(osm_seen=now)
    report["not_on_osm"] = sum(1 for shop in shops if shop.pk not in pairs and (standing(shop, today) or ("",))[0] == "yes")

    mine = {element.ref for _shop, element in pairs.values()} | taken | {item["ref"] for item in report["link_proposals"] + report["suggested"]}
    doubtful = {option["ref"] for item in report["ambiguous"] for option in item["options"]}
    # An informative list: nothing is proposed for a place that is not one of our shops, nor for one closed on OpenStreetMap
    candidates = sorted((element for element in elements if says(element.tags, ACCEPTS) == "yes"
                         and element.ref not in mine and element.ref not in doubtful),
                        key=lambda element: ((element.names or [""])[0].lower(), element.ref))
    old = {item["ref"] for item in (previous or {}).get("candidates", [])}
    for element in candidates[:CANDIDATES_KEPT]:
        report["candidates"].append({"ref": element.ref, "name": (element.names or [""])[0][:120], "city": element.tags.get("addr:city", "")[:60],
                                     "date": element.tags.get(DATE, "")[:10]})
        if previous is not None and element.ref not in old:
            report["new_candidates"].append(element.ref)
    report["candidates_total"] = len(candidates)
    return report


# --- Keeping the report -----------------------------------------------------------------------------------------------

def last_run(ok=True):
    runs = OsmRun.objects.all()
    return runs.filter(ok=True).first() if ok else runs.first()


def plausible(count, previous):
    """An answer with far fewer elements than the last reading is not trusted: a server that answers an empty list with
    no remark would make every linked shop look deleted there. OsmError, unless the reading before had few elements."""
    before = (previous or {}).get("elements", 0)
    if before >= SMALLEST_FALL and count * 2 < before:
        raise OsmError(f"only {count} elements against {before} last time: not trusted (--accept-fewer if it is true)")


def previous_run():
    """The report of the last reading that worked and that the association was told about (or that needed no telling): what
    the next reading compares with, to tell what is new. Not a reading whose summary did not go out, and not one made by
    hand without --send, which told nobody."""
    for run in OsmRun.objects.filter(ok=True)[:KEEP_RUNS]:
        if not run.report.get("undelivered") and not run.report.get("silent"):
            return run
    return None


def save_run(report=None, error=""):
    """Keeps a reading, or the failure of one, and forgets the old ones: the last KEEP_RUNS that worked and the last
    KEEP_FAILED that did not, each kept apart, so that a week of failures never pushes out the last good reading, which
    the comparison of the next one, the merchants' page and the proposals stand on. A failure just like the one before it is
    not written again."""
    if error:
        last = OsmRun.objects.first()
        if last is not None and not last.ok and last.error == error[:200]:
            return last
    # The moment the reading began, so that the shops it stamped (osm_seen) are not older than the report that tells it
    began = parse_datetime((report or {}).get("created", "")) or timezone.now()
    run = OsmRun.objects.create(ok=not error, error=error[:200], report=report or {}, created=began)
    for good, keep in ((True, KEEP_RUNS), (False, KEEP_FAILED)):
        for old in OsmRun.objects.filter(ok=good).order_by("-created", "-pk").values_list("pk", flat=True)[keep:]:
            OsmRun.objects.filter(pk=old).delete()
    return run


def waiting(run):
    """The proposals of a reading not yet sent, shop by shop: [{"shop", "name", "ref", "changes": [...]}]."""
    return list((run.report or {}).get("proposals", [])) if run and run.ok else []


ONE_SHOT = ("link_proposals", "filled", "offers")


def carry(report, previous):
    """What a reading does once (a link proposed, a field written, a contact offered) is not found again by the next one: the
    shop has it already. So that the summary that finally goes out tells it, the items of the readings since the last one that
    was told (`previous`, the run or None) are added to this one's, without repeating any. Without this a summary that did not
    go out, or a reading made by hand, would take its news with it."""
    since = OsmRun.objects.filter(ok=True)
    if previous is not None:
        since = since.filter(created__gt=previous.created)
    merged = {**report}
    for name in ONE_SHOT:
        seen, items = set(), []
        for source in [run.report for run in since.order_by("created", "pk")[:KEEP_RUNS]] + [report]:
            for item in source.get(name, []):
                key = (item["shop"], item["ref"], json.dumps(item.get("values", ""), sort_keys=True))
                if key not in seen:
                    seen.add(key)
                    items.append(item)
        merged[name] = items
    return merged


def counts(report):
    """The numbers a summary says: how many of each, from a report."""
    proposals = report.get("proposals", [])
    return {"proposals": sum(len(item["changes"]) for item in proposals), "shops": len(proposals),
            "link_proposals": len(report.get("link_proposals", [])), "filled": len(report.get("filled", [])),
            "offers": len(report.get("offers", [])),
            "candidates": len(report.get("new_candidates", [])), "total_candidates": report.get("candidates_total", 0),
            "says_no": len(report.get("osm_says_no", [])), "newer": len(report.get("newer_on_osm", [])),
            "ambiguous": len(report.get("ambiguous", [])) + len(report.get("unsure", [])),
            "missing": len(report.get("missing", [])), "suggested": len(report.get("suggested", [])),
            "unreliable": len(report.get("unreliable", []))}


def identities(report):
    """What each item of a report is, for telling the new from the old: {list name: {key, ...}}. Proposals one by one (a tag
    and the value proposed for it), the shops OpenStreetMap says no for or checked after us with the day, the others
    by shop and element."""
    found = {}
    for name in ("link_proposals", "suggested", "filled", "offers", "missing", "unreliable", "osm_says_no", "unsure"):
        found[name] = {(item["shop"], item["ref"], json.dumps(item.get("values", ""), sort_keys=True)) for item in report.get(name, [])}
    found["proposals"] = {(item["shop"], change["tag"], change["new"]) for item in report.get("proposals", []) for change in item["changes"]}
    found["newer_on_osm"] = {(item["shop"], item["ref"], item["theirs"]) for item in report.get("newer_on_osm", [])}
    found["ambiguous"] = {(item["shop"], tuple(sorted(option["ref"] for option in item["options"]))) for item in report.get("ambiguous", [])}
    return found


def fresh(report, previous):
    """The report with only what the previous one (that was told) did not have: new proposals, new places, shops newly said
    to take no bitcoin... `previous` None: everything is new. A link proposed, a field filled or offered is always new:
    it happens once."""
    if previous is None:
        return {**report, "new_candidates": [item["ref"] for item in report.get("candidates", [])]}
    old = identities(previous)
    mine = {**report}
    for name in ("link_proposals", "suggested", "filled", "offers", "missing", "unreliable", "osm_says_no", "unsure"):
        mine[name] = [item for item in report.get(name, [])
                      if (item["shop"], item["ref"], json.dumps(item.get("values", ""), sort_keys=True)) not in old[name]]
    mine["newer_on_osm"] = [item for item in report.get("newer_on_osm", []) if (item["shop"], item["ref"], item["theirs"]) not in old["newer_on_osm"]]
    mine["ambiguous"] = [item for item in report.get("ambiguous", [])
                         if (item["shop"], tuple(sorted(option["ref"] for option in item["options"]))) not in old["ambiguous"]]
    proposals = []
    for item in report.get("proposals", []):
        changes = [change for change in item["changes"] if (item["shop"], change["tag"], change["new"]) not in old["proposals"]]
        if changes:
            proposals.append({**item, "changes": changes})
    mine["proposals"] = proposals
    return mine


def worth_telling(report, previous=None):
    """Something new for a person to look at since the last summary: the monthly message goes out only then. The same
    proposals month after month are not news: they wait on /admin/today/."""
    found = counts(fresh(report, previous))
    return any(found[key] for key in ("proposals", "link_proposals", "filled", "offers", "candidates", "says_no", "newer",
                                      "ambiguous", "missing", "suggested", "unreliable"))


# Telegram refuses a message of more than 4096 characters; a little less leaves room for what it adds
MESSAGE_LIMIT = 3900


def fit(lines):
    """The lines as one text of at most MESSAGE_LIMIT characters, cut between two lines, with the last one saying so."""
    text = "\n".join(lines)
    if len(text) <= MESSAGE_LIMIT:
        return text
    kept, size = [], 0
    for line in lines[:-1]:
        if size + len(line) + 1 > MESSAGE_LIMIT - 200:
            break
        kept.append(line)
        size += len(line) + 1
    kept += ["…", lines[-1]]
    return "\n".join(kept)[:MESSAGE_LIMIT]


def summary(report, previous=None, link_to=""):
    """The monthly message for the moderators, in the site's language: (subject, text). The full count of what waits, and by
    name only what is new since the last summary. Names that OpenStreetMap holds go through alert_text (no address, no
    mention, no command); ours are the shops'. At most MESSAGE_LIMIT characters."""
    from .notifications import alert_text

    total = counts(report)
    news = fresh(report, previous)
    found = counts(news)
    subject = _("OpenStreetMap: %(count)d changes proposed") % {"count": total["proposals"]} if total["proposals"] else _(
        "OpenStreetMap: the monthly reading")
    lines = [subject]
    # Sending them is `manage.py osm_upload`, an experimental feature (config/features.py): off, the message promises none
    lines.append((ngettext("%(count)d change proposed for OpenStreetMap, in %(shops)d shops: nothing is sent until a person runs the upload.",
                           "%(count)d changes proposed for OpenStreetMap, in %(shops)d shops: nothing is sent until a person runs the upload.",
                           total["proposals"]) if features.enabled(features.OSM_UPLOAD) else
                  ngettext("%(count)d change proposed for OpenStreetMap, in %(shops)d shops: the site sends nothing, a person can make it by hand on openstreetmap.org.",
                           "%(count)d changes proposed for OpenStreetMap, in %(shops)d shops: the site sends nothing, a person can make them by hand on openstreetmap.org.",
                           total["proposals"])) % {"count": total["proposals"], "shops": total["shops"]}
                 if total["proposals"] else _("No change to propose to OpenStreetMap."))
    if found["link_proposals"]:
        lines.append(ngettext("%(count)d shop has a link to its OpenStreetMap entry waiting for you to confirm it in the administration:",
                              "%(count)d shops have a link to their OpenStreetMap entries waiting for you to confirm them in the administration:",
                              found["link_proposals"]) % {"count": found["link_proposals"]})
        lines += [f"· {alert_text(item['name'])} ({item['ref']})" for item in news["link_proposals"][:8]]
    if found["filled"]:
        lines.append(ngettext("%(count)d shop got empty details from OpenStreetMap (address, town, position), listed in the report.",
                              "%(count)d shops got empty details from OpenStreetMap (address, town, position), listed in the report.",
                              found["filled"]) % {"count": found["filled"]})
        lines += [f"· {alert_text(item['name'])}: {', '.join(item['values'])}" for item in news["filled"][:8]]
    if found["offers"]:
        lines.append(ngettext("%(count)d shop has a website or a phone offered by OpenStreetMap, to accept in the administration:",
                              "%(count)d shops have a website or a phone offered by OpenStreetMap, to accept in the administration:",
                              found["offers"]) % {"count": found["offers"]})
        lines += [f"· {alert_text(item['name'])}: {', '.join(item['values'])}" for item in news["offers"][:8]]
    if found["candidates"]:
        lines.append(ngettext("%(count)d new place on OpenStreetMap takes bitcoin and is not among our shops (only to know):",
                              "%(count)d new places on OpenStreetMap take bitcoin and are not among our shops (only to know):",
                              found["candidates"]) % {"count": found["candidates"]})
        new = set(news.get("new_candidates", []))
        lines += [f"· {alert_text(item['name'] or item['ref'])} ({item['ref']})" for item in report["candidates"] if item["ref"] in new][:8]
    if found["says_no"]:
        lines.append(ngettext("%(count)d of our shops is marked as no longer taking bitcoin on OpenStreetMap:",
                              "%(count)d of our shops are marked as no longer taking bitcoin on OpenStreetMap:",
                              found["says_no"]) % {"count": found["says_no"]})
        lines += [f"· {alert_text(item['name'])}" for item in news["osm_says_no"][:8]]
    if found["newer"]:
        lines.append(ngettext("%(count)d shop was checked on OpenStreetMap after our last check or closing date:",
                              "%(count)d shops were checked on OpenStreetMap after our last check or closing date:",
                              found["newer"]) % {"count": found["newer"]})
        lines += [f"· {alert_text(item['name'])}" for item in news["newer_on_osm"][:8]]
    if found["ambiguous"] or found["suggested"]:
        count = found["ambiguous"] + found["suggested"]
        lines.append(ngettext("%(count)d shop may be the same place as an OpenStreetMap element, but it is not certain: not linked.",
                              "%(count)d shops may be the same place as an OpenStreetMap element, but it is not certain: not linked.",
                              count) % {"count": count})
    if found["missing"]:
        lines.append(ngettext("%(count)d linked shop was not found on OpenStreetMap: deleted there, or no longer tagged for bitcoin.",
                              "%(count)d linked shops were not found on OpenStreetMap: deleted there, or no longer tagged for bitcoin.",
                              found["missing"]) % {"count": found["missing"]})
    if found["unreliable"]:
        lines.append(ngettext("%(count)d linked shop has an entry on OpenStreetMap that could not be read whole: nothing is proposed for it.",
                              "%(count)d linked shops have an entry on OpenStreetMap that could not be read whole: nothing is proposed for them.",
                              found["unreliable"]) % {"count": found["unreliable"]})
    if link_to:
        lines.append(_("The full report and the proposals: {link}").format(link=link_to))
    return subject, fit(lines)


def report_lines(report):
    """A report as lines of plain text, for /admin/ (escaped there): what each list holds, a line for each."""
    found = counts(report)
    lines = [f"elements: {report.get('elements', 0)} ({report.get('skipped', 0)} left out, {report.get('unreliable_elements', 0)} "
             f"not read whole); candidates: {found['total_candidates']}; links waiting for confirmation: {report.get('waiting_links', 0)}"]
    for title, key in (("Links proposed now, to confirm in /admin/", "link_proposals"), ("Suggested, not proposed (auto_link is off)", "suggested"),
                       ("Fields written (the values, to undo them)", "filled"), ("Contacts offered, to accept in /admin/", "offers"),
                       ("Linked, not found on OpenStreetMap", "missing"), ("Linked, but the entry could not be read whole", "unreliable"),
                       ("Marked as no longer taking bitcoin there", "osm_says_no"),
                       ("Checked there after our last check or closing day", "newer_on_osm")):
        if report.get(key):
            lines += ["", f"{title}:"]
            for item in report[key]:
                if "values" in item:
                    extra = ", ".join(f"{name}={value}" for name, value in item["values"].items())
                else:
                    extra = " ".join(f"{name}={item[name]}" for name in ("similarity", "metres", "ours", "theirs") if item.get(name) not in (None, ""))
                lines.append(f"  {item['name']}  {item['ref']}  {extra}".rstrip())
    if report.get("ambiguous"):
        lines += ["", "Doubtful (more than one fits), never linked:"]
        for item in report["ambiguous"]:
            lines.append(f"  {item['name']}: " + ", ".join(f"{option['ref']} ({option['name']}, {option['metres']} m)" for option in item["options"]))
    if report.get("unsure"):
        lines += ["", "Possible, but far or without a position, never linked:"]
        for item in report["unsure"]:
            lines.append(f"  {item['name']}  {item['ref']} ({item['osm_name']})  {item['why']}")
    if report.get("proposals"):
        lines += ["", "Changes proposed for OpenStreetMap (osm_upload sends them, by hand):"
                  if features.enabled(features.OSM_UPLOAD) else
                  "Changes proposed for OpenStreetMap (the site sends nothing: for a person to make by hand on openstreetmap.org):"]
        for item in report["proposals"]:
            lines.append(f"  {item['name']}  {item['ref']}")
            for change in item["changes"]:
                lines.append(f"    {change['tag']}: {change['old'] if change['old'] is not None else '(missing)'} -> "
                             f"{change['new'] if change['new'] is not None else '(remove)'}" + ("   [needs judgement]" if change["kind"] == REVIEW else ""))
    if report.get("candidates"):
        lines += ["", "Places on OpenStreetMap that take bitcoin and are not among our shops (to know, nothing is proposed for them):"]
        lines += [f"  {item['name'] or '(no name)'}  {item['ref']}  {item['city']}  {item['date']}" for item in report["candidates"]]
    return lines


# --- The merchant's page ----------------------------------------------------------------------------------------------

def element_url(ref):
    return f"https://www.openstreetmap.org/{ref}" if REF.match(ref or "") else ""


def editor_url(shop, ref=""):
    """The editor (iD) opened on the shop's element, or on its position for a shop that is not there yet, or on the
    centre of the community's area for one with no position."""
    base = "https://www.openstreetmap.org/edit?editor=id"
    if ref and REF.match(ref):
        kind, _slash, number_id = ref.partition("/")
        return f"{base}&{kind}={number_id}"
    if shop.latitude is not None and shop.longitude is not None:
        return f"{base}#map=19/{shop.latitude:.6f}/{shop.longitude:.6f}"
    return base


def request_summary(lines):
    """The lines to write, one tag a line, as the request keeps them for whoever does it."""
    return "\n".join(f"{tag} = {value}" for tag, value in lines.items())


def page(shop, now=None):
    """What the merchant's page about OpenStreetMap says, from the database only (no server is asked while it loads):
    None when the community does not use it. `linked`: the shop has an element and what differs from this site's data
    as of the last reading; otherwise the tags a new entry would carry. A change that needs a person's judgement is not
    shown to the merchant: the association looks at it, and the page says so instead of saying that all is well. It never
    says that all is well on a reading older than LATE_DAYS, nor while OpenStreetMap says the shop takes no bitcoin."""
    config = settings()
    if config is None or not shop.name:
        return None
    now = now or timezone.now()
    today = certified.local_day(now)
    run = last_run()
    wanted = wanted_tags(shop, config, today)
    state = {"wanted": wanted, "ref": shop.osm_ref, "url": element_url(shop.osm_ref), "editor": editor_url(shop, shop.osm_ref),
             "run": run, "read": run.created if run else None, "changes": None, "not_found": False, "up_to_date": False,
             "review": False, "says_no": False, "unreliable": False, "stale": False,
             "can_ask": bool(wanted) or bool(shop.osm_ref and standing(shop, today))}
    if shop.osm_ref and run is not None:
        mine = next((item for item in waiting(run) if item["shop"] == str(shop.pk) and item["ref"] == shop.osm_ref), None)
        every = (mine or {}).get("changes", [])
        found = [change for change in every if change["kind"] != REVIEW]
        here = str(shop.pk)
        state["changes"] = found
        state["review"] = len(found) < len(every)
        state["not_found"] = any(item["shop"] == here for item in run.report.get("missing", []))
        state["says_no"] = any(item["shop"] == here for item in run.report.get("osm_says_no", []))
        state["unreliable"] = any(item["shop"] == here for item in run.report.get("unreliable", []))
        state["stale"] = now - run.created > timedelta(days=LATE_DAYS)
        state["up_to_date"] = (not found and not state["review"] and not state["not_found"] and not state["says_no"]
                               and not state["unreliable"] and not state["stale"] and shop.osm_seen is not None
                               and shop.osm_seen >= run.created and bool(standing(shop, today)))
    # The lines to write: what differs there, or, for a shop not read yet or not there, every line a new entry would carry
    if state["changes"]:
        lines = [f"{change['tag']}={change['new']}" for change in state["changes"] if change["new"] is not None]
    elif state["up_to_date"] or state["not_found"] or state["says_no"] or state["review"] or state["unreliable"]:
        lines = []
    else:
        lines = [f"{tag}={value}" for tag, value in wanted.items()]
    state["lines"] = "\n".join(lines)
    # Two more than the lines: on a phone a long line wraps, and the last lines must not hide below the box
    state["rows"] = min(max(len(lines) + 2, 4), 16)
    return state

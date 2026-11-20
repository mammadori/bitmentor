"""Hints for whoever approves a new shop (shops/approval.py): what makes a waiting shop look like a copy of a shop already
on the site. Computed when needed from the shops in the database, stored nowhere, no network.

A shop that waits is compared with every other shop of another owner (approved, waiting, suspended, closed; a shop with
no owner is another owner's): the same name in the same area, the same website, phone or email, how near, whether the
other one is paid at another Lightning address, and an owner's account opened just before the shop. Another shop of
the same owner is a branch, never a signal.

They are hints, not a defence: whoever changes the name, the website and the street passes. Their job is to make a
person look before a likely copy goes on the map; `asks` signals hold the approval until that person says they checked
(approval.approve(confirmed=True)), and the history of the shop records which signals were there. A false alarm costs
more than it seems: people who tick a box every day stop reading it. So only what is likely a copy asks: the same name
in the same area, a name alike next door, the same contact. The rest is a quiet note."""

import logging
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlsplit

from django.contrib.auth import get_user_model
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.translation import gettext as _

from config import features

from .directory import metres_between
from .models import Profile, Shop
from .osm import DEFAULT_SIMILARITY, plain_name, similarity

logger = logging.getLogger(__name__)

# The same threshold the OpenStreetMap reading uses by default to call two names the same place (osm.DEFAULT_SIMILARITY):
# a letter or two apart, or every word of a name of 4 letters or more inside the other (osm.similarity)
NAME_SIMILARITY = DEFAULT_SIMILARITY
# The same area: this close when both have a position, else the same town
AREA_METRES = 2000
# A name only alike (not the same) asks for a check this close, or with a contact in common: in a big town «Bar Mario»
# and «Bar Maria» a kilometre apart are two bars
ALIKE_METRES = 300
# Near enough to be told in metres: the next door, a few house numbers away
NEAR_METRES = 150
# An account opened this long before the shop was written is "new"
NEW_ACCOUNT = timedelta(hours=24)
PHONE_DIGITS = 7
# The other shops named for one waiting shop: the list stays readable
MOST_OTHERS = 3
# Hosts that hold the pages of many businesses: the address is compared with its path, never the host alone
SHARED_HOSTS = (
    "facebook.com",
    "fb.com",
    "fb.me",
    "instagram.com",
    "linktr.ee",
    "linktree.com",
    "tiktok.com",
    "twitter.com",
    "x.com",
    "youtube.com",
    "youtu.be",
    "t.me",
    "telegram.me",
    "wa.me",
    "whatsapp.com",
    "g.page",
    "g.co",
    "maps.apple.com",
    "booking.com",
    "ubereats.com",
    "glovoapp.com",
    "paginegialle.it",
    "linkedin.com",
    "pinterest.com",
    "threads.net",
    "bsky.app",
    "github.com",
    "gitlab.com",
    "beacons.ai",
    "bio.link",
    "carrd.co",
    "about.me",
    "taplink.cc",
)
# The same, for every country of a brand: google.it, maps.google.co.uk, tripadvisor.de, thefork.fr…
SHARED_BRANDS = ("google", "tripadvisor", "thefork", "deliveroo", "justeat", "yelp")
# Short links: host and path, the path as written (its letters are the link)
SHORTENERS = ("bit.ly", "tinyurl.com", "amzn.to", "t.co", "is.gd", "rb.gy", "cutt.ly", "goo.gl")
# Builders that put each site on a subdomain of their own: the subdomain is the site; on the bare host, the path
SITE_HOSTS = (
    "wordpress.com",
    "blogspot.com",
    "myshopify.com",
    "jimdofree.com",
    "jimdosite.com",
    "altervista.org",
    "weebly.com",
    "squarespace.com",
    "business.site",
    "wixsite.com",
    "canva.site",
    "github.io",
    "netlify.app",
    "vercel.app",
    "pages.dev",
)
SITE_BRANDS = ("webnode",)
# What the shop's owner changes on the panel that a signal reads: an edit of anything else computes nothing
WATCHED = ("name", "website", "phone", "email", "city", "latitude", "longitude")
FIELDS = (
    "id",
    "owner_id",
    "name",
    "city",
    "latitude",
    "longitude",
    "website",
    "phone",
    "email",
    "lightning_address",
    "approved_on",
    "closed_on",
    "created",
    "claim_hash",
)

NAMESAKE = "namesake"
NAMESAKE_NOTE = "namesake_note"
SAME_CONTACT = "same_contact"
NEAR = "near"
OTHER_LIGHTNING = "other_lightning"
NEW_OWNER = "new_owner"
EQUAL, ALIKE = "equal", "alike"


@dataclass(frozen=True)
class Signal:
    code: str
    text: str
    # True: the approval waits for a person to say they checked (approval.approve(confirmed=True))
    asks: bool
    # The other shop (id, name) the signal is about, for a link to it in /admin/
    other_id: object = None
    other_name: str = ""
    # The first signal about that other shop: the page links the shop there, once; the last says what to check (`advice`)
    linked: bool = False
    advice: str = ""


@dataclass
class Card:
    """What the comparison reads of a shop, worked out once per shop and per page."""

    pk: object
    owner_id: object
    name: str
    plain: str
    words: frozenset
    letters: frozenset
    city: str
    position: tuple | None
    website: str
    phone: tuple
    email: str
    lightning: str
    closed_on: object
    claimable: bool
    approved_on: object
    created: object


def enabled():
    return features.enabled(features.APPROVAL)


def idna(host):
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return host


def brand(labels, brands):
    """Where a brand is among the labels of a host, followed only by a country's suffix (it, de, co.uk…), or None."""
    for index, label in enumerate(labels[:-1]):
        if label in brands and all(len(rest) <= 3 for rest in labels[index + 1 :]):
            return index
    return None


def under(host, domains):
    """The domain of the list the host is or is under, or None."""
    return next((domain for domain in domains if host == domain or host.endswith(f".{domain}")), None)


def page(host, parts, lower=True):
    """Host, path and query: the page of one business on a host of many. Without a path, nothing."""
    path = parts.path.rstrip("/")
    if not path and not parts.query:
        return ""
    key = f"{host}{path}{'?' + parts.query if parts.query else ''}"
    return key.lower() if lower else key


def website_key(url):
    """What two spellings of a website share: the host, in small letters and in its ASCII form, without www. or m. (any
    other subdomain is another site); not the path, so that /index.html or /Home is the same site. On a host of many
    businesses (SHARED_HOSTS, SHARED_BRANDS in any country) the path is the business, in small letters with its query;
    on a short link (SHORTENERS) the path as written; on a site builder (SITE_HOSTS, SITE_BRANDS) the subdomain is the
    site, or the path on the bare host. Without what tells the business, nothing. An address that cannot be read has
    no key."""
    url = (url or "").strip()
    if not url:
        return ""
    try:
        parts = urlsplit(url if "//" in url else f"//{url}")
        host = idna((parts.hostname or "").rstrip("."))
    except ValueError:
        return ""
    for prefix in ("www.", "m.", "mobile."):
        host = host.removeprefix(prefix)
    if not host:
        return ""
    labels = host.split(".")
    builder = under(host, SITE_HOSTS)
    if builder:
        return host if host != builder else page(host, parts)
    index = brand(labels, SITE_BRANDS)
    if index is not None:
        return host if index > 0 else page(host, parts)
    if under(host, SHORTENERS):
        return page(host, parts, lower=False)
    if under(host, SHARED_HOSTS) or brand(labels, SHARED_BRANDS) is not None:
        return page(host, parts)
    return host


def phone_keys(phone):
    """(international, national): the digits of a number written with its country (+ or 00 in front), or else None, and
    the shapes the same number may have without the country (1 to 3 digits) or with the leading 0 of the trunk."""
    raw = (phone or "").strip()
    digits = "".join(char for char in raw if char.isdigit())
    if raw.startswith("00"):
        digits = digits[2:]
    if len(digits) < PHONE_DIGITS:
        return None, frozenset()
    if raw.startswith(("+", "00")):
        nationals = {digits[cut:] for cut in (1, 2, 3)}
        nationals |= {"0" + number for number in nationals}
        return digits, frozenset(number for number in nationals if len(number) >= PHONE_DIGITS)
    return None, frozenset(number for number in (digits, digits.removeprefix("0")) if len(number) >= PHONE_DIGITS)


def phones_match(left, right):
    """Two numbers are the same when their digits are, or when one is written with its country and the other without.
    Two numbers both written with their country are compared whole: +39 and +33 are two countries."""
    (left_full, left_national), (right_full, right_national) = left, right
    if left_full and right_full:
        return left_full == right_full
    return bool(left_national & right_national)


def same_phone(left, right):
    return phones_match(phone_keys(left), phone_keys(right))


def provider(address):
    return address.rpartition("@")[2].strip().lower()


def letters(text):
    """Each letter with its count so far ("a1", "a2"…): two of these sets share as many items as the two texts share
    letters, counted with repeats."""
    seen = {}
    found = set()
    for char in text:
        seen[char] = seen.get(char, 0) + 1
        found.add((char, seen[char]))
    return frozenset(found)


def card(shop):
    plain = plain_name(shop.name or "")
    has_position = shop.latitude is not None and shop.longitude is not None
    return Card(
        pk=shop.pk,
        owner_id=shop.owner_id,
        name=shop.name or "",
        plain=plain,
        words=frozenset(plain.split()),
        letters=letters(plain),
        city=(shop.city or "").strip().casefold(),
        position=(shop.latitude, shop.longitude) if has_position else None,
        website=website_key(shop.website),
        phone=phone_keys(shop.phone),
        email=(shop.email or "").strip().casefold(),
        lightning=(shop.lightning_address or "").strip().lower(),
        closed_on=shop.closed_on,
        claimable=bool(shop.claim_hash),
        approved_on=shop.approved_on,
        created=shop.created,
    )


def others():
    """Every shop, with only what the comparison reads: one query, whatever the number of shops that wait."""
    return [card(shop) for shop in Shop.objects.only(*FIELDS)]


def owners(shops):
    """{user id: (when the account was opened, has a recovery email)}, in two queries."""
    ids = {shop.owner_id for shop in shops if shop.owner_id}
    if not ids:
        return {}
    emails = set(Profile.objects.filter(user_id__in=ids).exclude(email="").values_list("user_id", flat=True))
    return {
        pk: (joined, pk in emails)
        for pk, joined in get_user_model().objects.filter(pk__in=ids).values_list("pk", "date_joined")
    }


def names(left, right):
    """EQUAL, ALIKE (osm.similarity at its threshold) or None. Two lengths too far apart for the threshold, and no whole
    name inside the other, are not even compared letter by letter."""
    if not left.plain or not right.plain:
        return None
    if left.plain == right.plain:
        return EQUAL
    # The letters in common (each counted as many times as both have it) bound the ratio from above, as difflib's
    # quick_ratio does: below the threshold, the names are not compared letter by letter. Unless every word of one name
    # is inside the other (osm.similarity's other way to the threshold), the shorter of 4 letters or more
    if 2 * len(left.letters & right.letters) / (len(left.plain) + len(right.plain)) < NAME_SIMILARITY:
        fewer = left.plain if len(left.words) <= len(right.words) else right.plain
        if not ((left.words < right.words or right.words < left.words) and len(fewer) >= 4):
            return None
    return ALIKE if similarity(left.name, right.name) >= NAME_SIMILARITY else None


def contacts(shop, other):
    """The kinds of contact two Cards share: never the values, which a page or a message would show."""
    kinds = []
    if shop.website and shop.website == other.website:
        kinds.append(_("website"))
    if phones_match(shop.phone, other.phone):
        kinds.append(_("phone"))
    if shop.email and shop.email == other.email:
        kinds.append(_("email"))
    return kinds


def signals_of(shops, everything=None, accounts=None, name=str, waiting_only=True):
    """{shop pk: [Signal]} for the given shops that wait (any shop with `waiting_only` false); [] for the others and with
    the approval turned off. `name` writes the other shop's name (notifications.alert_text for a message)."""
    shops = list(shops)
    found = {shop.pk: [] for shop in shops}
    waiting = [shop for shop in shops if shop.approved_on is None or not waiting_only]
    if not enabled() or not waiting:
        return found
    everything = others() if everything is None else everything
    accounts = owners(waiting) if accounts is None else accounts
    for shop in waiting:
        found[shop.pk] = signals_for(shop if isinstance(shop, Card) else card(shop), everything, accounts, name)
    return found


def signals_for(shop, everything, accounts, name=str, most=MOST_OTHERS):
    """The signals of one shop (a Card) against every other one (Cards), about `most` other shops at most (None: all)."""
    strong, notes = [], []
    for other in everything:
        if other.pk == shop.pk or (shop.owner_id and other.owner_id == shop.owner_id):
            continue
        alike = names(shop, other)
        kinds = contacts(shop, other)
        if not alike and not kinds:
            continue
        metres = metres_between(shop.position, other.position) if shop.position and other.position else None
        same_city = bool(shop.city) and shop.city == other.city
        area = metres <= AREA_METRES if metres is not None else same_city
        # The same name counts the town too: the owner places the pin, and a copy would place it 2.1 km away
        area = area or (alike == EQUAL and same_city)
        # The same name in the same area asks; a name only alike asks next door, or with a contact in common
        asks = (
            bool(kinds)
            or (alike == EQUAL and area)
            or (alike == ALIKE and metres is not None and metres < ALIKE_METRES)
        )
        if asks:
            strong.append(
                (
                    metres if metres is not None else float("inf"),
                    other.name.casefold(),
                    other,
                    alike if area else None,
                    metres,
                    kinds,
                )
            )
        else:
            notes.append((0 if area else 1, other.name.casefold(), other, area, metres))
    found = []
    for _metres, _name, other, alike, metres, kinds in sorted(strong, key=lambda row: row[:2])[:most]:
        found += about(shop, other, alike, metres, kinds, name)
    if found:
        joined, has_email = accounts.get(shop.owner_id, (None, True))
        alone = not any(other.owner_id == shop.owner_id for other in everything if other.pk != shop.pk)
        if joined and shop.created and shop.created - joined < NEW_ACCOUNT and not has_email and alone:
            found.append(
                Signal(
                    NEW_OWNER,
                    _("The owner's account was opened just before the shop, with no recovery email and no other shop."),
                    False,
                )
            )
        return found
    for _area, _name, other, area, metres in sorted(notes, key=lambda row: row[:2])[:MOST_OTHERS]:
        values = {"other": name(other.name)}
        if area and metres is not None:
            text = _("A shop of another owner %(km)s km away has a name like this one: «%(other)s».") % {
                **values,
                "km": f"{metres / 1000:.1f}",
            }
        elif area:
            text = _("A shop of another owner in the same town has a name like this one: «%(other)s».") % values
        else:
            text = (
                _("A shop of another owner elsewhere has a name like this one: «%(other)s» (a chain, perhaps).")
                % values
            )
        found.append(Signal(NAMESAKE_NOTE, text, False, other.pk, other.name, True))
    return found


def advice(other, name):
    """What the person who approves can do about the other shop, which depends on what it is."""
    values = {"other": name(other.name)}
    if other.owner_id is None:
        # A merchant cannot take it alone: a moved shop waits for its claim link (shops/moving.py), an imported one for
        # the staff to give it an owner
        if other.claimable:
            return (
                _(
                    "«%(other)s» is on the site without an owner and waits for its claim link: if it is the same business, "
                    "send that link to this merchant instead of approving a second shop."
                )
                % values
            )
        return (
            _(
                "«%(other)s» is on the site without an owner: if it is the same business, give that shop to this owner "
                "in the administration instead of approving a second one."
            )
            % values
        )
    if other.closed_on:
        values["date"] = date_format(other.closed_on, "SHORT_DATE_FORMAT")
        # As Shop.historic_note reads the date: up to and including that day the shop still "closes"
        if other.closed_on >= timezone.localdate():
            return (
                _("«%(other)s» closes on %(date)s: this new shop may be its successor, or a move. Ask its owner.")
                % values
            )
        return _("«%(other)s» closed on %(date)s: check whether this new shop is its reopening.") % values
    return _("Ask the owner of «%(other)s» whether this new shop is theirs, or genuine.") % values


def about(shop, other, alike, metres, kinds, name):
    """The signals about one other shop: its name in the same area and how near, its contacts, its Lightning address."""
    values = {"other": name(other.name)}
    found = []
    if alike == EQUAL:
        found.append(
            (NAMESAKE, _("The same name as «%(other)s», a shop of another owner in the same area.") % values, True)
        )
    elif alike:
        found.append(
            (
                NAMESAKE,
                _("A name very like «%(other)s», a shop of another owner in the same area.") % values,
                bool(kinds) or (metres is not None and metres < ALIKE_METRES),
            )
        )
    if alike and metres is not None and metres < NEAR_METRES:
        found.append((NEAR, _("The two are only %(metres)d m apart.") % {"metres": round(metres)}, False))
    if kinds:
        found.append(
            (
                SAME_CONTACT,
                _("The same contacts as «%(other)s», a shop of another owner: %(kinds)s.")
                % {**values, "kinds": ", ".join(kinds)},
                True,
            )
        )
    # Never the addresses themselves: whether they differ, and whether at another provider
    if shop.lightning and other.lightning and shop.lightning != other.lightning:
        text = (
            _("«%(other)s» is paid at another Lightning address, at another provider.")
            if provider(shop.lightning) != provider(other.lightning)
            else _("«%(other)s» is paid at another Lightning address.")
        ) % values
        found.append((OTHER_LIGHTNING, text, False))
    tip = advice(other, name)
    return [
        Signal(code, text, asks, other.pk, other.name, index == 0, tip if index == len(found) - 1 else "")
        for index, (code, text, asks) in enumerate(found)
    ]


def signals(shop, name=str, waiting_only=True):
    """The signals of one shop: [] when it does not wait (unless `waiting_only` is false)."""
    return signals_of([shop], name=name, waiting_only=waiting_only)[shop.pk]


def asks(found):
    """Whether a list of signals holds the approval until a person says they checked."""
    return any(signal.asks for signal in found)


def codes(found):
    return list(dict.fromkeys(signal.code for signal in found))

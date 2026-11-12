"""Nostr events signed by the community: the list of shops outlives the site."""
import hashlib
import json
import re
import time

import coincurve

from . import details, vacation

GEOHASH = "0123456789bcdefghjkmnpqrstuvwxyz"


def geohash(latitude, longitude, precision=9):
    """The position as Nostr `g` tags want it (NIP-52): 9 characters are a few metres."""
    bounds = {True: [-180.0, 180.0], False: [-90.0, 90.0]}
    text, value, bits, is_longitude = "", 0, 0, True
    while len(text) < precision:
        low_high, coordinate = bounds[is_longitude], longitude if is_longitude else latitude
        middle = (low_high[0] + low_high[1]) / 2
        upper = coordinate >= middle
        value = value * 2 + upper
        low_high[0 if upper else 1] = middle
        is_longitude = not is_longitude
        bits += 1
        if bits == 5:
            text += GEOHASH[value]
            value = bits = 0
    return text


def decode_geohash(text):
    """The middle of the box a geohash names, as (latitude, longitude); None if the text is not a geohash (the reverse of
    geohash(), for the `g` tags of the events we read)."""
    if not isinstance(text, str) or not 0 < len(text) <= 12 or any(char not in GEOHASH for char in text):
        return None
    bounds = {True: [-180.0, 180.0], False: [-90.0, 90.0]}
    is_longitude = True
    for char in text:
        value = GEOHASH.index(char)
        for bit in (16, 8, 4, 2, 1):
            low_high = bounds[is_longitude]
            middle = (low_high[0] + low_high[1]) / 2
            low_high[0 if value & bit else 1] = middle
            is_longitude = not is_longitude
    return sum(bounds[False]) / 2, sum(bounds[True]) / 2


def public_key(secret):
    return coincurve.PublicKeyXOnly.from_secret(bytes.fromhex(secret)).format().hex()


def event_id(pubkey, created_at, kind, tags, content):
    serialized = json.dumps([0, pubkey, created_at, kind, tags, content], separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(serialized.encode()).digest()


def verified(event):
    """An event from outside is what it claims to be: the id recomputed as in sign() and the Schnorr signature of its
    key (NIP-01). Anything odd counts as not verified."""
    try:
        expected = event_id(event["pubkey"], event["created_at"], event["kind"], event["tags"], event["content"])
        if expected.hex() != event["id"]:
            return False
        return coincurve.PublicKeyXOnly(bytes.fromhex(event["pubkey"])).verify(bytes.fromhex(event["sig"]), expected)
    except Exception:  # noqa: BLE001 - relay data: wrong types, lengths and keys are all a no
        return False


def sign(secret, kind, content, tags, created_at=None):
    pubkey = public_key(secret)
    created_at = created_at or int(time.time())
    digest = event_id(pubkey, created_at, kind, tags, content)
    signature = coincurve.PrivateKey(bytes.fromhex(secret)).sign_schnorr(digest)
    return {"id": digest.hex(), "pubkey": pubkey, "created_at": created_at, "kind": kind, "tags": tags,
            "content": content, "sig": signature.hex()}


SHOP_KIND = 30402


def deletion_event(secret, kind, identifiers, event_ids=()):
    """NIP-09: asks relays to remove the addressable events with these `d`, every version up to now (`a`), and the
    events seen with these ids (`e`): some relays, like nostr-rs-relay 0.9, delete only by id."""
    pubkey = public_key(secret)
    tags = [["e", id_] for id_ in sorted(event_ids)] + [["a", f"{kind}:{pubkey}:{d}"] for d in sorted(identifiers)]
    return sign(secret, 5, "", tags + [["k", str(kind)]])


def shop_event(secret, shop, url):
    tags = [["d", str(shop.id)], ["title", shop.name], ["t", "bitcoin"], ["r", url],
            # The street only if the owner shows the address (shops/details.py); the town otherwise. Nothing else of the
            # shop's details goes out: not the phone, the email, the business name or the VAT number, shown or not
            ["location", details.place(shop)], ["g", geohash(shop.latitude, shop.longitude)]]
    # A historic shop stays, with its date and without a discount: the new event replaces the one that had it
    if shop.closed_on:
        tags.append(["closed_on", shop.closed_on.isoformat()])
    elif shop.discount:
        tags.append(["discount", str(shop.discount)])
    # Closed for a while (shops/vacation.py): the reason from a list and the day it opens again, empty if not said. The
    # event is signed again each time it changes, so the day it ends the relays get the shop without it
    pause = vacation.api(shop)
    if pause and pause["state"] == "closed":
        tags.append(["break", pause["reason"], pause["back"] or ""])
    return sign(secret, SHOP_KIND, shop_text(shop), tags)


def shop_text(shop):
    """The shop text in the event. NIP-99 content is Markdown, signed with the community key: without web addresses and
    with the Markdown marks escaped, like the description on the site, which is plain text; and capped."""
    text = re.sub(r"(?i)\b(?:https?://|www\.)\S*", "", shop.description or shop.name)
    text = re.sub(r"([\\`*_{}\[\]()#+\-!|>~<])", r"\\\1", text).strip()
    return text[:1000] or shop.name


def file_event(secret, sha256, size, mime, summary, url=""):
    """NIP-94: a file described by its SHA-256. Without `url` it holds wherever that file ends up."""
    tags = [["x", sha256], ["m", mime], ["size", str(size)], ["alt", summary]]
    if url:
        tags.insert(0, ["url", url])
    return sign(secret, 1063, summary, tags)


def profile_event(secret, name, about, picture=""):
    """NIP-01. Without an image there is no `picture`: clients show an address that is not an image as broken."""
    profile = {"name": name, "about": about, **({"picture": picture} if picture else {})}
    return sign(secret, 0, json.dumps(profile, ensure_ascii=False), [])

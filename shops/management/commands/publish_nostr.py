"""Publishes the community profile and the visible shops to the relays. With --sha256 it signs a file instead."""
import json
import re
import time
from urllib.parse import urljoin

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.urls import reverse
from django.utils.translation import override
from websockets.exceptions import WebSocketException
from websockets.sync.client import connect

from config.community import community, localized
from shops.directory import public_shops
from shops.models import real
from shops.nostr import SHOP_KIND, deletion_event, file_event, profile_event, public_key, shop_event, verified

# The images every Nostr client shows as a profile picture: many do not draw an SVG logo
PICTURE = (".png", ".jpg", ".jpeg", ".webp", ".gif")
SHOPS_ASKED = 5000
LISTING_SECONDS = 30
# A deletion with hundreds of tags can exceed a relay's limit per event: it goes out in blocks
DELETION_BLOCK = 100


def published_shops(socket, pubkey):
    """The shops the relay still has from this key, as {d: event ids}.

    The relay is someone else's server, and what it answers ends up in a deletion signed with the community key, which
    the other relays apply by id: only a shop event whose id matches and whose signature verifies counts. A relay that
    made up an id could not make us delete anything that is not one of our shops.
    A NOTICE is skipped; EOSE, CLOSED, an answer that is not NIP-01, silence or 30 s end the list: publishing goes on,
    and only what was seen is withdrawn."""
    socket.send(json.dumps(["REQ", "shops", {"authors": [pubkey], "kinds": [SHOP_KIND], "limit": SHOPS_ASKED}]))
    found = {}
    deadline = time.monotonic() + LISTING_SECONDS
    for _ in range(SHOPS_ASKED * 2):
        left = deadline - time.monotonic()
        if left <= 0:
            break
        try:
            message = json.loads(socket.recv(timeout=min(10, left)))
        except TimeoutError:
            break
        if not isinstance(message, list) or not message or message[0] in ("EOSE", "CLOSED"):
            break
        event = message[2] if message[0] == "EVENT" and len(message) > 2 else None
        if isinstance(event, dict) and event.get("pubkey") == pubkey and event.get("kind") == SHOP_KIND and verified(event):
            d = next((tag[1] for tag in event["tags"] if isinstance(tag, list) and len(tag) > 1 and tag[0] == "d"), None)
            if isinstance(d, str) and 0 < len(d) <= 100:
                found.setdefault(d, set()).add(event["id"])
    socket.send(json.dumps(["CLOSE", "shops"]))
    return found


def deliver(socket, event):
    """Sends an event and waits for its OK. EOSE, CLOSED, NOTICE or events left over from the list are skipped, so a
    late answer is not taken for the OK of the next event. An answer that is not NIP-01 makes the relay fail."""
    socket.send(json.dumps(["EVENT", event], ensure_ascii=False))
    for _ in range(20):
        reply = json.loads(socket.recv(timeout=10))
        if reply[0] != "OK":
            if isinstance(reply, list) and isinstance(reply[0], str):
                continue
            raise ValueError("not a NIP-01 answer")
        if not isinstance(reply[2], bool):
            raise TypeError("OK without true or false")
        return reply[2]
    raise ValueError("no OK")


def send(relay, events, secret=None):
    """With `secret`, also withdraws (NIP-09) the shops the relay still has but that are no longer among those sent:
    suspended, taken off the map, deleted. A suspended shop, or one without consent, must not stay on the relays with
    its name, address and position. With no shop to send nothing is withdrawn: an empty database by mistake is more
    likely than a community without shops. One key, one site: another instance publishing with the same key would see
    these shops as stale, and the other way round."""
    accepted = withdrawn = 0
    deletions = []
    with connect(relay) as socket:
        current = {tag[1] for event in events if event["kind"] == SHOP_KIND for tag in event["tags"] if tag[0] == "d"}
        if secret and current:
            stale = {d: ids for d, ids in published_shops(socket, public_key(secret)).items() if d not in current}
            names = sorted(stale)
            for start in range(0, len(names), DELETION_BLOCK):
                block = names[start:start + DELETION_BLOCK]
                deletions.append((block, deletion_event(secret, SHOP_KIND, block, set().union(*(stale[d] for d in block)))))
        # Deletions first: an event refused halfway (a relay closing the connection) must not leave suspended shops
        # on the relays
        for block, event in deletions:
            if deliver(socket, event):
                accepted += 1
                withdrawn += len(block)
        for event in events:
            accepted += deliver(socket, event)
    return f"{accepted}/{len(events) + len(deletions)}" + (f", withdrawn: {withdrawn}" if withdrawn else "")


def picture(data, base):
    """`nostr.picture`, absolute or under the site; otherwise the theme logo, if it is an image; otherwise nothing."""
    found = str((data.get("nostr") or {}).get("picture") or "")
    logo = str(data.get("logo") or "")
    if not found and logo.lower().endswith(PICTURE):
        found = f"/theme/{logo}"
    return urljoin(base + "/", found) if found else ""


def publish(relays, events, write, secret=None):
    """Each relay on its own: one that is down does not keep the events from the others. Returns how many failed."""
    failed = 0
    for relay in relays:
        try:
            write(f"{relay}: {send(relay, events, secret)}")
        # A relay is someone else's server: an answer that is not ["OK", id, true/false] counts as a relay that failed
        except (OSError, WebSocketException, ValueError, LookupError, TypeError) as error:
            write(f"{relay}: {type(error).__name__}")
            failed += 1
    return failed


class Command(BaseCommand):
    help = "Signs the public data and publishes it to NOSTR_RELAYS"

    def add_arguments(self, parser):
        parser.add_argument("--sha256", help="sign this file (NIP-94) instead of the public data, and print the event")
        parser.add_argument("--size", type=int, default=0)
        parser.add_argument("--mime", default="application/octet-stream")
        parser.add_argument("--summary", default="")
        parser.add_argument("--url", default="")
        parser.add_argument("--sign-only", action="store_true", help="with --sha256: send nothing to the relays")

    def handle(self, *args, **options):
        secret = settings.NOSTR_SECRET_KEY
        if not secret:
            raise CommandError("NOSTR_SECRET_KEY is needed")
        if options["sha256"]:
            self.sign_file(secret, options)
            return
        if not settings.NOSTR_RELAYS:
            raise CommandError("NOSTR_SECRET_KEY and NOSTR_RELAYS are needed")
        data = community()
        base = f"https://{settings.SITE_HOST}"
        language = next(iter(data["languages"]))
        with override(language):
            events = [profile_event(secret, data["name"], localized(data.get("tagline", ""), language), picture(data, base))]
            events += [shop_event(secret, shop, base + reverse("shop_detail", args=[shop.id]))
                       for shop in real(public_shops()).prefetch_related("closures")]
        if publish(settings.NOSTR_RELAYS, events, self.stdout.write, secret):
            raise CommandError("Some relays did not receive the events")

    def sign_file(self, secret, options):
        if not re.fullmatch(r"[0-9a-f]{64}", options["sha256"]):
            raise CommandError("--sha256 wants 64 hexadecimal digits")
        event = file_event(secret, options["sha256"], options["size"], options["mime"], options["summary"], options["url"])
        failed = 0 if options["sign_only"] else publish(settings.NOSTR_RELAYS, [event], self.stderr.write)
        self.stdout.write(json.dumps(event, ensure_ascii=False))
        if failed:
            raise CommandError("Some relays did not receive the event")

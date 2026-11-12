"""Asks relays for events and nothing else: it never writes to one. A relay is someone else's server, so what it says is
data (meetups/nostr_read.py decides what to believe) and what it does is limited:

- a relay that is not in the site's own configuration (`NOSTR_RELAYS`) must be `wss://`, and the name must lead only to
  public addresses: it is resolved once, every address is checked, and the check is repeated on the address we are
  connected to, so a name that answers differently the second time reaches nothing inside;
- no proxy from the environment, a few seconds to connect, a total time per relay however slowly it sends, a ceiling on
  the size of a message and on the number of events, and no redirect (a WebSocket upgrade does not follow one);
- an answer that is not NIP-01 is skipped, and an error is one relay failing: the others are still read."""
import json
import socket
import time
from urllib.parse import urlsplit

from websockets.exceptions import WebSocketException
from websockets.sync.client import connect

from config.network import is_public, resolve

from . import conf

CONNECT_SECONDS = 8
TOTAL_SECONDS = 45
RECV_SECONDS = 10
MESSAGE_BYTES = 128 * 1024
# What the relay may send, however it nests it: a frame bigger than an event may be (nostr_read.MAX_EVENT_CHARS, with its
# envelope) is dropped before it is parsed, and a relay gets only this much of our memory
EVENT_FRAME_BYTES = 34 * 1024
TOTAL_BYTES = 8 * 1024 * 1024
MAX_EVENTS = 1500
AUTHORS_PER_FILTER = 25
CALENDAR_KINDS = [31922, 31923]
DELETION_KIND = 5


class RelayError(Exception):
    """This relay could not be read: the text says why, in a few words."""


def public_socket(host, port):
    """A connection to a name whose every address is public, checked on the address connected to."""
    try:
        found = resolve(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        raise RelayError("the name does not lead anywhere") from None
    if not found or not all(is_public(info[4][0]) for info in found):
        raise RelayError("the name leads to an address that is not public")
    for family, kind, proto, _canonical, address in found[:4]:
        sock = socket.socket(family, kind, proto)
        try:
            sock.settimeout(CONNECT_SECONDS)
            sock.connect(address)
            if not is_public(sock.getpeername()[0]):
                raise RelayError("connected to an address that is not public")
            sock.settimeout(None)
            return sock
        except RelayError:
            sock.close()
            raise
        except OSError:
            sock.close()
    raise RelayError("no connection")


def open_relay(url, own):
    options = {"proxy": None, "open_timeout": CONNECT_SECONDS, "close_timeout": 3, "ping_interval": None,
               "max_size": MESSAGE_BYTES, "max_queue": 64, "user_agent_header": "bitmentor"}
    if url in own:
        return connect(url, **options)
    parts = urlsplit(url)
    if parts.scheme != "wss" or not parts.hostname:
        raise RelayError("only wss:// addresses are read")
    sock = public_socket(parts.hostname, parts.port or 443)
    try:
        return connect(url, sock=sock, **options)
    except BaseException:
        sock.close()
        raise


def build_filters(authors, since):
    """The questions for the authors, a block of them at a time: their calendar events and their deletions."""
    questions = []
    for start in range(0, len(authors), AUTHORS_PER_FILTER):
        block = authors[start:start + AUTHORS_PER_FILTER]
        questions.append({"kinds": CALENDAR_KINDS, "authors": block, "since": since, "limit": 500})
        questions.append({"kinds": [DELETION_KIND], "authors": block, "since": since, "limit": 200})
    return questions


def read(url, questions, own=(), seconds=TOTAL_SECONDS, limit=MAX_EVENTS):
    """(events, complete) from one relay. `complete` is False when it stopped early, for time, size or a refusal; the
    events until then are good all the same. RelayError when the relay could not be read and gave nothing."""
    started = time.monotonic()
    events, complete, received = [], True, 0
    # Only the authors we asked for are kept: a relay can send anything, and the rest would only weigh on memory
    allowed = {author for question in questions for author in question.get("authors", ())}
    try:
        with open_relay(url, own) as relay:
            for number, question in enumerate(questions):
                subscription = f"bm{number}"
                relay.send(json.dumps(["REQ", subscription, question]))
                while True:
                    left = started + seconds - time.monotonic()
                    if left <= 0 or len(events) >= limit:
                        complete = False
                        break
                    try:
                        raw = relay.recv(timeout=min(RECV_SECONDS, left))
                    except TimeoutError:
                        complete = False
                        break
                    if not isinstance(raw, str) or len(raw) > EVENT_FRAME_BYTES:
                        continue  # a binary frame, or one too big to be an event: not parsed
                    received += len(raw)
                    if received > TOTAL_BYTES:
                        complete = False
                        break
                    try:
                        message = json.loads(raw)
                    except (ValueError, RecursionError):
                        continue  # text that is not JSON, or nested so deep that the parser gives up
                    if not isinstance(message, list) or len(message) < 2:
                        continue
                    if message[0] == "EOSE" and message[1] == subscription:
                        break
                    if message[0] == "CLOSED" and message[1] == subscription:
                        complete = False
                        break
                    if message[0] == "EVENT" and len(message) > 2 and message[1] == subscription and isinstance(message[2], dict) \
                            and message[2].get("pubkey") in allowed:
                        events.append(message[2])
                if left <= 0 or received > TOTAL_BYTES:
                    break
                relay.send(json.dumps(["CLOSE", subscription]))
    except RelayError:
        raise
    except (OSError, WebSocketException, ValueError) as error:
        if not events:
            raise RelayError(type(error).__name__) from None
        complete = False
    if not events and not complete:
        raise RelayError("no complete answer (silent, refused or too slow)")
    return events, complete


def read_all(urls, authors, since, write):
    """The events of every relay, one relay at a time; a relay that fails is told to `write` and the others go on.
    Returns (events, how many answered)."""
    own = conf.own_relays()
    found, answered = [], 0
    questions = build_filters(authors, since)
    for url in urls:
        try:
            events, complete = read(url, questions, own)
        except RelayError as error:
            write(f"{url}: {error}")
            continue
        except Exception as error:  # noqa: BLE001 - a relay is someone else's server: whatever it does is one relay failing
            write(f"{url}: {type(error).__name__}")
            continue
        answered += 1
        write(f"{url}: {len(events)} events" + ("" if complete else ", stopped early"))
        found += events
    return found, answered

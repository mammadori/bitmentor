"""Before the shop is created, the Lightning address is probed the way the register will use it, paying nothing.

A provider without LUD-21 gives the register no way to see a payment arrive: the merchant has to check the wallet by
hand. This has to be found out here, not in front of the customer.
"""
import hashlib
import json
import socket
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as Late
from urllib.parse import urlsplit

import requests
from django.core.cache import cache
from django.utils.translation import gettext as _
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import NewConnectionError
from urllib3.util.timeout import _DEFAULT_TIMEOUT

from config.community import community
from config.network import SlowName, is_public, resolve

from .forms import PRIVATE_NAMES, validate_lightning_address

TIMEOUT = 5
# Any server answers: a huge answer, or one trickling in, must not hold a worker
MAX_BYTES = 64 * 1024
MAX_SECONDS = 10
# A caller that reads a list (the transactions of an address) can ask for more, never beyond this
LARGEST_BYTES = 512 * 1024
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; bitmentor)"}
MIN_MSAT = 1000
OK, UNKNOWN_DOMAIN, NO_ANSWER, NO_CONFIRMATION = "ok", "unknown_domain", "no_answer", "no_confirmation"
# The live check remembers an address that works long enough to fill in the rest of the form, a problem only briefly:
# whoever fixes the wallet and pastes again wants a new answer
REMEMBER_OK = 15 * 60
REMEMBER_PROBLEM = 30
# Live checks at the same time, per process: the others answer "busy" at once. A slow server chosen by whoever writes
# holds at most these threads, never the ones that serve the pages
LIVE_AT_ONCE = 2
LIVE_SLOTS = threading.BoundedSemaphore(LIVE_AT_ONCE)
LIVE_POOL = ThreadPoolExecutor(max_workers=LIVE_AT_ONCE, thread_name_prefix="lightning-check")
BUSY = "busy"
# The check on submit of the forms (`refusal` with `seconds`): a pool of its own, so live checks do not keep a merchant
# from saving, and the other way round. Past its time a probe is LATE: the merchant is told to try again
SUBMIT_AT_ONCE = 2
SUBMIT_SLOTS = threading.BoundedSemaphore(SUBMIT_AT_ONCE)
SUBMIT_POOL = ThreadPoolExecutor(max_workers=SUBMIT_AT_ONCE, thread_name_prefix="lightning-submit")
LATE = "late"


class Watchdog(HTTPAdapter):
    """`TIMEOUT` applies to each single read: whoever sends one byte every few seconds always respects it, in TLS, in the
    headers or in the body, and would hold the thread for hours. When `seconds` runs out the connection is closed from
    outside, whatever it is reading. With `public_only` (the default, and every caller today) it talks only to public
    addresses; `public_only=False` is for a service the administrator chooses in the settings, which may be internal. With `pinned` (addresses as getaddrinfo
    gives them) every connection goes to those, already checked, and the name is not asked again: a second lookup would
    have no time limit, and could give another address."""

    def __init__(self, seconds, public_only=True, pinned=None):
        self.sockets = []
        self.fired = False
        self.public_only = public_only
        self.pinned = pinned
        self.timer = threading.Timer(seconds, self.cut)
        self.timer.daemon = True
        super().__init__(max_retries=0)
        self.timer.start()

    def init_poolmanager(self, *args, **kwargs):
        super().init_poolmanager(*args, **kwargs)
        watchdog = self

        def watched(base):
            class Connection(base):
                def _new_conn(self):
                    sock = pinned_socket(self, watchdog.pinned) if watchdog.pinned else super()._new_conn()
                    # Without `pinned` urllib3 resolves the name again: whoever controls the DNS can give the check a
                    # public address and this connection an internal one. What counts is the one we actually connected
                    # to, before sending a byte
                    if watchdog.public_only and not is_public(sock.getpeername()[0]):
                        sock.close()
                        raise OSError(f"{self.host} leads to an address that is not public")
                    # A copy of the descriptor: TLS takes the original, but shutdown applies to the socket, from any copy
                    watchdog.sockets.append(sock.dup())
                    if watchdog.fired:
                        watchdog.cut()
                    return sock

            return Connection

        class Pool(HTTPConnectionPool):
            ConnectionCls = watched(HTTPConnection)

        class SecurePool(HTTPSConnectionPool):
            ConnectionCls = watched(HTTPSConnection)

        self.poolmanager.pool_classes_by_scheme = {"http": Pool, "https": SecurePool}

    def cut(self):
        self.fired = True
        for sock in list(self.sockets):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def close(self):
        self.timer.cancel()
        self.timer.join()
        super().close()
        for sock in self.sockets:
            sock.close()


def pinned_socket(connection, addresses):
    """A connection of urllib3 to the first of `addresses` that answers, with its timeout and socket options, without
    asking the DNS: TLS and the Host header still use the name, so the certificate is checked for the name."""
    failure = None
    for family, _kind, proto, _name, address in addresses:
        sock = socket.socket(family, socket.SOCK_STREAM, proto)
        try:
            for option in connection.socket_options or ():
                sock.setsockopt(*option)
            if connection.timeout is not _DEFAULT_TIMEOUT:
                sock.settimeout(connection.timeout)
            sock.connect(address)
            return sock
        except OSError as error:
            sock.close()
            failure = error
    raise NewConnectionError(connection, f"Failed to establish a new connection: {failure}")


def public_addresses(host, seconds):
    """The addresses of `host` on port 443, asked within `seconds` (config.network.resolve), every one of them public:
    the connection goes to these. ValueError when one is not public or there is none; requests.ConnectionError, with
    the gaierror as its cause, when the name does not resolve (`missing` reads it); SlowName when the DNS does not
    answer in time, which says nothing about the address."""
    try:
        found = resolve(host, 443, seconds=seconds, proto=socket.IPPROTO_TCP)
    except SlowName:
        raise
    except socket.gaierror as error:
        raise requests.ConnectionError(f"{host} does not resolve") from error
    except (OSError, UnicodeError):
        raise ValueError(host) from None
    if not found or not all(is_public(info[4][0]) for info in found):
        raise ValueError(host)
    return found


def fetch(url, seconds=MAX_SECONDS, size=MAX_BYTES):
    """The URL comes from an external server: only HTTPS, only public hosts, no redirects. `seconds` can only shorten
    the limit of the whole request, DNS included, never lengthen it; `size` is at most LARGEST_BYTES. SlowName when the
    name gets no answer in time."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or parts.port not in (None, 443) or not host or host.endswith(PRIVATE_NAMES):
        raise ValueError(url)
    seconds = min(seconds, MAX_SECONDS)
    size = min(size, LARGEST_BYTES)
    if seconds <= 0 or size <= 0:
        raise ValueError(url)
    deadline = time.monotonic() + seconds
    addresses = public_addresses(host, seconds)
    left = deadline - time.monotonic()
    if left <= 0:
        raise SlowName(f"no time left after asking for {host}")
    body = b""
    with requests.Session() as session:
        watchdog = Watchdog(left, pinned=addresses)
        session.mount("https://", watchdog)
        with session.get(url, headers=HEADERS, timeout=min(TIMEOUT, seconds), allow_redirects=False,
                         stream=True) as response:
            response.raise_for_status()
            for chunk in response.iter_content(8192):
                body += chunk
                if len(body) > size or time.monotonic() > deadline:
                    raise ValueError(url)
        # Cut by the watchdog, an answer with no length ends as if it were complete: it is not
        if watchdog.fired:
            raise ValueError(url)
    try:
        return json.loads(body)
    except RecursionError:
        # «[[[[…» nested beyond Python's limit fits under MAX_BYTES: it is a wrong answer, not an error of the site
        raise ValueError(url) from None


def same_provider(url, domain):
    """Whether the register would talk to `url` for an address at `domain`: https on the default port, on that domain or
    a subdomain of it. The same rule as `trusted` in static/register.js, whose page opens only those hosts (its CSP): what
    the register refuses at the counter must not pass here, where the merchant can still be told."""
    if not isinstance(url, str):
        return False
    try:
        parts = urlsplit(url)
        host, port = (parts.hostname or "").lower(), parts.port
    except ValueError:
        return False
    domain = domain.lower()
    return parts.scheme == "https" and port in (None, 443) and (host == domain or host.endswith(f".{domain}"))


def probe(address, seconds=None, busy=NO_ANSWER, slow=None):
    """How the register would see `address`: its metadata, an invoice (nothing gets paid) and whether that invoice can
    say it was paid. Returns OK, or why it could not take payments there: UNKNOWN_DOMAIN, NO_ANSWER, NO_CONFIRMATION.
    Invoices and confirmations count only from the address's own domain or a subdomain (`same_provider`), as at the register.
    `seconds` caps the whole probe, for the live check of the form; without it each request has its own limit. A
    provider that answers 429 or 5xx is busy or down for a while: `busy` is returned then (the guardian of the
    Lightning addresses passes its "do not know", which does not count as a failure). A name the DNS does not answer
    for in time gives `slow`, by default `busy`: nothing is known about the address."""
    user, _at, domain = address.partition("@")
    deadline = None if seconds is None else time.monotonic() + seconds

    def get(url):
        return fetch(url) if deadline is None else fetch(url, deadline - time.monotonic())

    try:
        try:
            metadata = get(f"https://{domain}/.well-known/lnurlp/{user}")
        except requests.ConnectionError as error:
            # A slip of the keyboard after the @ is the most common mistake: say so, instead of "does not answer"
            return UNKNOWN_DOMAIN if missing(domain, error) else NO_ANSWER
        if metadata.get("tag") != "payRequest":
            return NO_ANSWER
        amount = max(int(metadata.get("minSendable", MIN_MSAT)), MIN_MSAT)
        callback = metadata["callback"]
        # Invoices from another site: the register asks nobody but the address's own provider, so it gets none
        if not same_provider(callback, domain):
            return NO_ANSWER
        invoice = get(f"{callback}{'&' if '?' in callback else '?'}amount={amount}")
        if not invoice.get("pr"):
            return NO_ANSWER
        # The same for the address that says «paid»: elsewhere, the register cannot ask it and never confirms
        if (not invoice.get("verify") or not same_provider(invoice["verify"], domain)
                or not confirms(get(invoice["verify"]), invoice["pr"])):
            return NO_CONFIRMATION
    except requests.HTTPError as error:
        status = getattr(error.response, "status_code", None) or 0
        return busy if status == 429 or status >= 500 else NO_ANSWER
    except SlowName:
        return busy if slow is None else slow
    except (requests.RequestException, ValueError, KeyError, TypeError, AttributeError, ArithmeticError):
        # ArithmeticError: a `minSendable` that is no whole number (JSON allows 1e400, read as infinity)
        return NO_ANSWER
    return OK


def confirms(answer, invoice):
    """The answer of a LUD-21 verify URL for the invoice just made, as the register will read it: `settled` a boolean,
    and false, since nobody knows that invoice to pay it (a provider that says true would show the register sales that
    never arrived). `status`, when there, says OK (an ERROR with `settled` beside it is not an answer); `pr`, when
    there, is that invoice. A provider that leaves out `status` or `pr` is still followed: the register reads only
    `settled`."""
    if not isinstance(answer, dict) or answer.get("settled") is not False:
        return False
    status = answer.get("status", "OK")
    if not isinstance(status, str) or status.upper() != "OK":
        return False
    paid = answer.get("pr")
    return paid is None or (isinstance(paid, str) and paid.lower() == str(invoice).lower())


def missing(domain, error=None):
    """True only when the DNS says that the name does not exist: a resolver that does not answer is not a typo. With
    `error`, the ConnectionError of a fetch, its own lookup decides, without asking again: a fetch always asks the
    name first, so a connection that failed without a gaierror was to a name that exists."""
    if error is not None:
        cause = error.__cause__
        if not isinstance(cause, socket.gaierror):
            return False
        return cause.errno in {socket.EAI_NONAME, getattr(socket, "EAI_NODATA", socket.EAI_NONAME)}
    try:
        resolve(domain, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as error:
        return error.errno in {socket.EAI_NONAME, getattr(socket, "EAI_NODATA", socket.EAI_NONAME)}
    except (OSError, UnicodeError):
        return False
    return False


def one_slip(typed, known):
    """`known` differs from `typed` by one slip of the keyboard: a letter more, one less, one changed, two swapped."""
    if typed == known or abs(len(typed) - len(known)) > 1:
        return False
    if len(typed) == len(known):
        diff = [i for i, (a, b) in enumerate(zip(typed, known)) if a != b]
        return len(diff) == 1 or (len(diff) == 2 and diff[1] == diff[0] + 1
                                  and typed[diff[0]] == known[diff[1]] and typed[diff[1]] == known[diff[0]])
    short, long = sorted((typed, known), key=len)
    return any(long[:i] + long[i + 1:] == short for i in range(len(long)))


def known_domains():
    """The providers that work, as this site has seen them: those of at least two active shops with a register and
    different owners, the most used first. Not what passed a check: anyone can make an address of their own pass, and a
    domain of theirs one slip away from a known provider would be offered as where the money goes. Not a shop's own
    domain either, which would tell it to others. No provider is named in the software."""
    from .models import Shop

    shops = (Shop.objects.exclude(lightning_address="").exclude(owner=None)
             .filter(suspended=False, closed_on=None))
    owners = {}
    for address, owner in shops.values_list("lightning_address", "owner")[:5000]:
        owners.setdefault(address.rpartition("@")[2], set()).add(owner)
    counted = Counter({domain: len(who) for domain, who in owners.items() if len(who) >= 2})
    return [domain for domain, _count in counted.most_common()]


def suggestion(address):
    """The same address at a provider one slip away from the one written, if this site has seen it work."""
    user, _at, domain = address.partition("@")
    near = next((known for known in known_domains() if one_slip(domain, known)), None)
    return f"{user}@{near}" if near else None


def explanation(state, address, suggested=None):
    """What the merchant reads: what is wrong and what to do now."""
    domain = address.rpartition("@")[2]
    text = {
        OK: _("It works: the register will know when a payment arrives. Provider: {provider}."),
        UNKNOWN_DOMAIN: _("I cannot find {provider}: check what you wrote after the @."),
        NO_ANSWER: _("This Lightning address does not answer: check it and try again. "
                     "Phoenix addresses, the ones with the ₿ sign, cannot be used here."),
        NO_CONFIRMATION: _("This address can receive, but it does not tell the register when a payment arrives. "
                           "Use a wallet with a different Lightning address, or write to {email}."),
    }[state].format(provider=domain, email=community()["email"])
    if suggested:
        text = f"{text} {_('Did you mean {address}?').format(address=suggested)}"
    return text


def cache_key(address, user):
    """One person's result: what another account has tried says nothing on submit."""
    return "lnaddress:" + hashlib.sha256(f"{user}:{address}".encode()).hexdigest()


def live_check(address, seconds, user):
    """The state of `address` for the live check of the form. A result stays for a short while, so that whoever pastes
    the same address again does not send the same requests. It is the exact address and the same person: another
    address, even by a letter, is probed from scratch."""
    remembered = cache.get(cache_key(address, user))
    if remembered:
        return remembered
    state = probe(address, seconds)
    cache.set(cache_key(address, user), state, REMEMBER_OK if state == OK else REMEMBER_PROBLEM)
    return state


def bounded(slots, pool, seconds, work):
    """work() in a thread of `pool`, waited for at most `seconds`: BUSY at once when every place of `slots` is taken,
    LATE past the time. A work past its time keeps its place until it really ends, so a slow server chosen by whoever
    writes holds at most the places of `slots`, never the threads that serve the pages."""
    if not slots.acquire(blocking=False):
        return BUSY
    try:
        future = pool.submit(work)
    except RuntimeError:
        slots.release()
        return BUSY
    future.add_done_callback(lambda _done: slots.release())
    try:
        return future.result(timeout=seconds)
    except Late:
        return LATE


def checked_live(address, seconds, user):
    """The rules of the form, then the probe, all within `seconds`, DNS included: they run in a thread of their own,
    and the page gets BUSY when LIVE_AT_ONCE are already running in this process. A check past its time keeps its slot
    until it really ends. The result is a state, or the ValidationError of the form, whose message is translated by
    the caller in the language of the page."""
    deadline = time.monotonic() + seconds

    def run():
        try:
            validate_lightning_address(address)
        except Exception as error:  # noqa: BLE001 - the ValidationError goes back to the view as it is
            return error
        return live_check(address, deadline - time.monotonic(), user)

    found = bounded(LIVE_SLOTS, LIVE_POOL, seconds, run)
    return NO_ANSWER if found == LATE else found


def refusal(address, user=None, seconds=None):
    """The reason the register could not take payments on this address, or None. The check on submit is the
    real one: only the same address that has just passed the live check of the same person is not probed again. The
    form (with `user`) gets "Did you mean ...?" too, which reads the shops: not the callers that probe in a thread of
    their own, like the diagnosis, where the database is not theirs to open.

    With `seconds` (the forms, in a web worker) the whole probe, DNS included, runs in the pool of SUBMIT_AT_ONCE
    places and is waited for about `seconds` (a second more, for the probe to end on its own limit and say why). When
    the places are all taken, or the provider is too slow, the answer is "try again" and nothing is decided about the
    address: the form is shown again as it was written. Saving without the check would give a register that may never
    see a payment, and refusing the address would be wrong."""
    if user is not None and cache.get(cache_key(address, user)) == OK:
        return None
    if seconds is None:
        state = probe(address)
    else:
        deadline = time.monotonic() + seconds

        def run():
            # A name the DNS does not answer for in time is "try again", as a provider too slow: nothing is decided
            state = probe(address, deadline - time.monotonic(), slow=LATE)
            # Cut by the time: what failed says how slow the provider is, not that the address is wrong
            return LATE if state != OK and time.monotonic() >= deadline else state

        # A little longer than the probe's own limit, which then ends by itself and says why
        state = bounded(SUBMIT_SLOTS, SUBMIT_POOL, seconds + 1, run)
        if state == BUSY:
            return _("Too many checks right now: try again in a minute.")
        if state == LATE:
            return _("Your wallet's provider is slow or busy right now: try again in a few minutes.")
    if state == OK:
        return None
    return explanation(state, address, suggestion(address) if user is not None and state != NO_CONFIRMATION else None)

"""The requests to OpenStreetMap's servers: Overpass, which is read, and the editing API, which `manage.py osm_upload`
writes to by hand and nothing else ever does.

Every request goes through the same protections as the other requests to servers the site does not control
(shops/lnaddress.py): https only, public hosts only, the name asked once within the request's time and the connection
made to the addresses checked (TLS still checks the certificate for the name), no redirect, a limit on the time of the whole request and on the size of the answer, read as it arrives. The answer of
Overpass is bigger than a Lightning address's, so the limits here are the community's own (`osm.timeout`,
`osm.max_megabytes`), each with a ceiling.

The token of the account that signs the edits (`OSM_TOKEN`) is read from the environment by osm_upload, handed to
`request` for one call, and sent in one header to the editing API of the community's settings. It is never in a URL, in
a log, in an error message or in a file. The tests replace `request` with a fake: no test reaches a server."""
import socket
import time
from urllib.parse import urlencode, urlsplit

import requests
from django.conf import settings

from config.network import SlowName, is_public, resolve

from .forms import PRIVATE_NAMES
from .lnaddress import Watchdog
from .osm import BITCOIN_KEYS, UNPLAIN, OsmError, api_server, plain_host

# Seconds to connect: a server that does not answer the connection is down, whatever the query takes
CONNECT_SECONDS = 10
# What the editing API says back: a changeset number, a diff, an error line
API_BYTES = 256 * 1024
API_SECONDS = 30


def user_agent():
    """Who asks, and where to write to them: both OpenStreetMap servers ask for it, and block those who do not say."""
    return f"bitmentor/1.0 (+{settings.PUBLIC_URL})"


def public(host, seconds=None):
    """The addresses of the name, asked within `seconds` (config.network.resolve), when it resolves and only to public
    addresses; otherwise an empty list. A name that does not resolve is not public: the check that a name is "not
    private" cannot pass for one nobody can say anything about (shops/forms.py takes it as public, since the probe will
    refuse it; here the request itself would be the first to try it). SlowName when the DNS does not answer in time."""
    try:
        found = resolve(host, 443, seconds=seconds, proto=socket.IPPROTO_TCP)
    except SlowName:
        raise
    except (OSError, UnicodeError):
        return []
    return found if found and all(is_public(address[4][0]) for address in found) else []


def request(method, url, *, seconds, size, data=None, headers=None):
    """One request, the answer's bytes. OsmError, with one line that names no address and no token, when the server is
    not reachable, answers with an error, takes too long or sends more than `size` bytes."""
    parts = urlsplit(url)
    # The host as the parser of `requests` reads it, and the same as ours (`plain_host`): where the bytes go is where we looked
    host = plain_host(url)
    if parts.scheme != "https" or not host or parts.port not in (None, 443) or host.endswith(PRIVATE_NAMES):
        raise OsmError("the address is not a public https server")
    if seconds <= 0 or size <= 0:
        raise OsmError("no time or room left for the answer")
    deadline = time.monotonic() + seconds
    # The connection goes to these addresses, already checked: urllib3 does not ask the DNS again (a second lookup
    # would have no time limit, and could give another address)
    try:
        addresses = public(host, seconds)
    except SlowName:
        raise OsmError("the server's name did not resolve in time") from None
    if not addresses:
        raise OsmError("the address is not a public https server")
    left = deadline - time.monotonic()
    if left <= 0:
        raise OsmError("the server's name did not resolve in time")
    # A bytearray grows in place: `body += chunk` on bytes copies everything read so far, once for each chunk
    body = bytearray()
    try:
        with requests.Session() as session:
            watchdog = Watchdog(left, pinned=addresses)
            session.mount("https://", watchdog)
            with session.request(method, url, data=data, headers={"User-Agent": user_agent(), **(headers or {})},
                                 timeout=(min(CONNECT_SECONDS, seconds), seconds), allow_redirects=False, stream=True) as response:
                if response.status_code >= 400:
                    raise OsmError(f"the server answered {response.status_code}{refusal(response)}")
                if 300 <= response.status_code < 400:
                    raise OsmError(f"the server redirected ({response.status_code}): not followed")
                for chunk in response.iter_content(8192):
                    body += chunk
                    if len(body) > size:
                        raise OsmError("the answer is bigger than the limit")
                    if time.monotonic() > deadline:
                        raise OsmError("the answer took too long")
            # Cut by the watchdog, an answer with no length looks complete: it is not
            if watchdog.fired:
                raise OsmError("the answer took too long")
    except requests.Timeout:
        raise OsmError("the server did not answer in time") from None
    except (requests.RequestException, OSError, ValueError) as error:
        raise OsmError(f"the server could not be reached ({type(error).__name__})") from None
    return bytes(body)


def refusal(response):
    """The first words of an error answer that is text (OpenStreetMap writes why: 'Version mismatch', 'Precondition
    failed'), for the line that says what went wrong. Nothing that is not plain text, and not much of it."""
    try:
        text = response.headers.get("Error") or ""
    except AttributeError:
        text = ""
    text = "".join(char if char.isprintable() else "?" for char in str(text))[:100].strip()
    return f": {text}" if text else ""


def overpass_query(config, meta=False):
    """Everything in the area with a bitcoin tag: nodes, ways and relations. `meta` adds the version of each element and the
    nodes of a way, what an edit needs and a reading does not. A way or a relation comes with its centre."""
    south, west, north, east = (f"{value:.6f}" for value in config.bbox)
    area = f"({south},{west},{north},{east})"
    union = "".join(f'nwr["{key}"]{area};' for key in BITCOIN_KEYS)
    return f"[out:json][timeout:{config.seconds}];({union});out {'meta' if meta else 'body'} center;"


def read_area(config, meta=False):
    """The Overpass answer for the community's area, as the bytes it sent."""
    url = f"{config.overpass_url}{'&' if '?' in config.overpass_url else '?'}{urlencode({'data': overpass_query(config, meta)})}"
    # The query's own timeout is the community's; the request waits a little longer for the answer to arrive
    return request("GET", url, seconds=config.seconds + 10, size=config.max_bytes)


def api(config, method, path, token, data=None):
    """One call to the editing API (the changeset, its upload, its closing). The token is the account's, in a header."""
    url = f"{config.api_url}/{path.lstrip('/')}"
    # The token goes to the editing API of OpenStreetMap, by exact name, and to nowhere else, whatever the settings say: checked
    # on the very address that is about to be asked, with the parser `requests` uses
    if UNPLAIN.search(config.api_url) or urlsplit(url).scheme != "https" or not api_server(url):
        raise OsmError("the editing API is not a server of OpenStreetMap: the token is not sent")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "text/xml; charset=utf-8"}
    return request(method, url, seconds=API_SECONDS, size=API_BYTES, data=data, headers=headers)

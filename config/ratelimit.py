"""How often a visitor may ask: counters by name and time window, in the database, and the visitor's address they count.

The counters live in the database (shops.models.RequestCount), not in the cache: with the file cache two workers could
both read 9 and both write 10. The address is the last one of X-Forwarded-For, which Caddy always writes itself
(`header_up X-Forwarded-For {client_ip}`, with what it trusts from TRUSTED_PROXIES): web is reachable only through
Caddy, so whatever a visitor sends in that header is replaced. For IPv6 the network counts, a /64, and with
`over_address_limits` its /48 too, which a tunnel or a VPS gives for free with 65,536 /64 networks inside."""
import hashlib
import ipaddress
import secrets
import time
from datetime import datetime
from datetime import timezone as dt_timezone

from django.db import IntegrityError, transaction
from django.db.models import F

# The network that counts as one visitor's when one person can have all of it for free
WIDE_PREFIX = 48


def forwarded_address(request):
    """The visitor's address as written: the last of X-Forwarded-For, or the connection's without a proxy. A string,
    not checked."""
    return request.headers.get("X-Forwarded-For", request.META.get("REMOTE_ADDR", "")).split(",")[-1].strip()


def visitor_ip(request):
    """The visitor's address as an ip_address, IPv4-mapped IPv6 as the IPv4 it is; None when it is not an address."""
    try:
        ip = ipaddress.ip_address(forwarded_address(request))
    except ValueError:
        return None
    return (ip.version == 6 and ip.ipv4_mapped) or ip


def client_ip(request, prefix=64):
    """The key of the visitor for a limit: the IPv4 address, or the IPv6 network of `prefix` bits (whoever has one
    has billions of addresses). What is not an address is kept as it was written."""
    ip = visitor_ip(request)
    if ip is None:
        return forwarded_address(request)
    if ip.version == 6:
        return str(ipaddress.ip_network(f"{ip}/{prefix}", strict=False).network_address)
    return str(ip)


def client_network(request):
    """The visitor's widest network for a limit: the /48 for IPv6, the address itself for IPv4."""
    return client_ip(request, WIDE_PREFIX)


def limit_key(name, seconds):
    """The window in which over_limit counts `name`, and the key of its row."""
    window = int(time.time() // seconds)
    return window, hashlib.sha256(f"{name}:{seconds}:{window}".encode()).hexdigest()


def give_back(name, seconds):
    """Takes off the count of `name` a request that over_limit counted in this window, for one that in the end was not
    of the kind the limit wants to count (count first, atomically, and give back after: requests at the same time do not pass)."""
    from shops.models import RequestCount

    RequestCount.objects.filter(key=limit_key(name, seconds)[1], count__gt=0).update(count=F("count") - 1)


def seconds_left(seconds):
    """How long until the end of the window of `seconds` in which over_limit is counting now: the Retry-After of a refusal."""
    return int(seconds - time.time() % seconds) + 1


def over_limit(name, limit, seconds):
    """Counts a request of `name` in the window of `seconds` and says whether it went over `limit`. The UPDATE is atomic: two
    requests at the same time count two."""
    from shops.models import RequestCount

    window, key = limit_key(name, seconds)
    if not RequestCount.objects.filter(key=key).update(count=F("count") + 1):
        try:
            with transaction.atomic():
                RequestCount.objects.create(key=key, expires=datetime.fromtimestamp((window + 1) * seconds, dt_timezone.utc))
        except IntegrityError:
            RequestCount.objects.filter(key=key).update(count=F("count") + 1)
    if secrets.randbelow(50) == 0:
        # The same clock as the window, and never the count just made: with two clocks (tests move timezone.now) the
        # row just counted could look expired, and the get below would find nothing
        RequestCount.objects.filter(expires__lt=datetime.fromtimestamp(time.time(), dt_timezone.utc)).exclude(key=key).delete()
    return RequestCount.objects.get(key=key).count > limit


def give_back_address(request, name, seconds):
    """Takes off both counts of `over_address_limits` a request it counted, for one that in the end did not do what the
    limit is about (an email that did not leave)."""
    give_back(f"{name}:{client_ip(request)}", seconds)
    give_back(f"{name}-net:{client_network(request)}", seconds)


def over_address_limits(request, name, per_address, per_network, seconds):
    """Counts a request of `name` for the visitor's address (`name:<address>`) and, if that one is not over, for its
    widest network (`name-net:<network>`); True when one of them went over."""
    return (over_limit(f"{name}:{client_ip(request)}", per_address, seconds)
            or over_limit(f"{name}-net:{client_network(request)}", per_network, seconds))

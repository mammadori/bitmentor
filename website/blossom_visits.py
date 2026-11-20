"""How many different people use the Blossom server (website/blossom.py), with no personal data.

A user is the visitor's network (the /24 of an IPv4 address, the /48 of an IPv6 one), kept only as an HMAC with a key made from SECRET_KEY and the month: next month the same user is another hash, and nothing leads
back to the address. One row a day for each user and kind of request, kept 90 days.

Not counted: the addresses of DEV_ALLOWED_IPS, private and local networks, and the User-Agents of bots. Asking only for
`/list` makes no user: a user asked for at least one file by its hash.

`users()` is the most different users of one month's key seen in the last 30 days: a user seen at both ends of a
month is not counted twice. When it reaches `blossom_alert_users` of community.yml (3 when unset; 0 never tells) for
the first time, the moderators are told, the way the morning summary goes.
"""

import datetime
import hashlib
import hmac
import ipaddress
import logging
import re
import threading

from django.conf import settings
from django.db import DatabaseError, IntegrityError, transaction
from django.db.models import Count
from django.utils import timezone

from config import ratelimit
from config.community import community

logger = logging.getLogger(__name__)

BLOB = "blob"
LIST = "list"
KEEP_DAYS = 90
MEMORY = 10000  # rows remembered in a process, so as not to write again
WINDOW_DAYS = 30
ALERT_USERS = 3
BOTS = re.compile(r"bot|crawl|spider|slurp|preview|monitor|uptime|curl/|wget/|python-requests|go-http-client", re.I)

_lock = threading.Lock()
_written = set()  # (day, month, who, kind) already in the database: no write for each request
_cleaned = {"day": None}


def reset():
    with _lock:
        _written.clear()
        _cleaned["day"] = None


def month_of(day):
    return day.strftime("%Y-%m")


def who(network, month):
    """Only the network: a User-Agent changed at every request would make a user each, and the alert a forgery."""
    key = hmac.new(settings.SECRET_KEY.encode(), f"blossom visitors {month}".encode(), hashlib.sha256).digest()
    return hmac.new(key, str(network).encode(), hashlib.sha256).hexdigest()


def network_of(ip):
    return ipaddress.ip_network(f"{ip}/{24 if ip.version == 4 else 48}", strict=False)


def counted(request):
    """The visitor's network, or None when it is not to be counted."""
    ip = ratelimit.visitor_ip(request)
    if ip is None or not ip.is_global:
        return None
    from .visibility import allowed_networks

    if any(ip in network for network in allowed_networks()):
        return None
    if BOTS.search(request.headers.get("User-Agent", "")):
        return None
    return network_of(ip)


def seen(request, kind):
    """Writes down a request for a blob or for the list. Never fails the answer."""
    try:
        network = counted(request)
        if network is None:
            return
        today = timezone.localdate()
        month = month_of(today)
        row = (today, month, who(network, month), kind)
        with _lock:
            if row in _written:
                return
            if _cleaned["day"] != today or len(_written) > MEMORY:
                _written.clear()
        record(*row)
        with _lock:
            _written.add(row)
    except DatabaseError:
        logger.exception("blossom: a visit could not be written")


def record(day, month, hashed, kind):
    from .models import BlossomVisit

    if _cleaned["day"] != day:
        BlossomVisit.objects.filter(day__lt=day - datetime.timedelta(days=KEEP_DAYS)).delete()
        _cleaned["day"] = day
    try:
        with transaction.atomic():
            BlossomVisit.objects.create(day=day, month=month, who=hashed, kind=kind)
    except IntegrityError:
        return
    if kind == BLOB:
        maybe_tell(day)


def users(day=None):
    """The different users who asked for a blob in the last 30 days, counted within one month's key."""
    from .models import BlossomVisit

    day = day or timezone.localdate()
    per_month = (
        BlossomVisit.objects.filter(kind=BLOB, day__gt=day - datetime.timedelta(days=WINDOW_DAYS))
        .values("month")
        .annotate(users=Count("who", distinct=True))
    )
    return max((row["users"] for row in per_month), default=0)


def threshold():
    value = community().get("blossom_alert_users", ALERT_USERS)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else ALERT_USERS


def maybe_tell(day):
    from .models import BlossomMilestone

    goal = threshold()
    if not goal or users(day) < goal:
        return
    try:
        with transaction.atomic():
            BlossomMilestone.objects.create(users=goal)
    except IntegrityError:
        return  # already told

    def deliver():
        from shops.notifications import deliver_to_moderators

        from . import blossom

        text = (
            f"Blossom: {goal} different users in the last {WINDOW_DAYS} days, for the first time "
            f"({blossom.server()}/list)."
        )
        where, problems = deliver_to_moderators("Blossom users", text)
        if where is None:
            logger.warning("blossom: the moderators could not be told: %s", "; ".join(problems))

    transaction.on_commit(lambda: threading.Thread(target=deliver, daemon=True).start())

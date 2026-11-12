"""Every limit on how much the site grows or how often a visitor can ask, in one place.

A site open to the public that nobody watches every day must not fill up with spam or with data nobody asked for. Each
limit here has a default that suits a small community and can be changed in `community.yml`:

    limits:
      shops: 1000
      help_requests: 0         # 0 = no limit

A value that is not a whole number of 0 or more counts as the default, and `manage.py check` says so. The keys, with
their defaults and what they count, are in LIMITS; the same list is documented in examples/community/community.yml and
in docs/configuration.md.

Two kinds:
- totals (`counters` knows how to count them): when one is reached the site refuses politely whatever would add one
  more, never deletes anything, and says so in the log, in the staff's dashboard (/admin/today/) and in `manage.py
  check` and `manage.py preflight`, with a warning from WARN_AT of the limit;
- rates (a number per address, per author, per shop): only enforced where the request comes in.

The Nostr relay is not ours: RELAY_RECOMMENDED only documents what whoever runs it should configure, nothing here
enforces it."""
import logging
from dataclasses import dataclass

from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from . import mailbudget
from .community import community

logger = logging.getLogger(__name__)
# From this share of a total limit the staff is warned
WARN_AT = 0.8
# What a rate limit of 0 becomes where a number is needed
NO_LIMIT = 10**9


@dataclass(frozen=True)
class Limit:
    key: str
    default: int
    what: str
    # True when `counters` counts what the limit is about; False for a rate
    total: bool = True


LIMITS = {limit.key: limit for limit in (
    Limit("shops", 500, gettext_lazy("shops on the site (the demo shops do not count)")),
    Limit("shops_waiting_wallet", 100, gettext_lazy("shops saved without a wallet yet (sign-ups not finished)")),
    Limit("accounts", 2000, gettext_lazy("accounts of merchants and visitors who signed in (staff left out)")),
    Limit("help_requests", 5000, gettext_lazy("requests for help kept, solved ones too (delete the old ones in /admin/)")),
    Limit("nostr_meetups", 5000, gettext_lazy("meetups copied from Nostr and kept")),
    Limit("emails_per_month", 1000, gettext_lazy("emails sent this month (the mail provider's quota; see config/mailbudget.py)")),
    Limit("emails_per_day", 100, gettext_lazy("emails sent today (by default a tenth of the month's limit)")),
    Limit("staff_registers_per_shop", 30, gettext_lazy("staff registers turned on at the same time in one shop"), total=False),
    Limit("accounts_per_address_hour", 20, gettext_lazy("new accounts from one network address in an hour"), total=False),
    Limit("shops_per_address_hour", 30, gettext_lazy("new shops or wallets connected from one network address in an hour"), total=False),
    Limit("emails_per_address_day", 5, gettext_lazy("emails a visitor can make the site send to the same address in a day"), total=False),
    Limit("emails_per_sender_day", 10, gettext_lazy("emails one network address can make the site send in a day (three times as many for its wider network)"), total=False),
    Limit("emails_staff_factor", 3, gettext_lazy("times more emails (per address and per network address) for the staff and the allowed addresses, who may be testing"), total=False),
    Limit("help_requests_per_shop_day", 3, gettext_lazy("requests for help from one shop in a day"), total=False),
)}

# What whoever runs the Nostr relay should configure (docs/features/nostr-copies.md): kept here with the other numbers
# so that they change together, never enforced by the site
RELAY_RECOMMENDED = {"event_kb": 256, "quota_per_key_mb": 2, "quota_per_key_events": 200}


def block():
    data = community().get("limits")
    return data if isinstance(data, dict) else {}


def valid(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def default(key):
    """The default: a fixed number, except the emails of a day, a tenth of the month's limit so that one day cannot use up
    the month (no limit if the month has none)."""
    if key == "emails_per_day":
        month = get("emails_per_month")
        return max(1, month // 10) if month else 0
    return LIMITS[key].default


def get(key):
    """The limit's value: the one in `community.yml`, or the default when it is missing or wrong. 0 means no limit."""
    value = block().get(key)
    return value if value is not None and valid(value) else default(key)


def rate(key):
    """For `over_limit(name, rate(key), seconds)`: the value, or a number nobody reaches when it is 0 (no limit)."""
    return get(key) or NO_LIMIT


def problems():
    """What is wrong in the `limits` block, for `manage.py check`: a wrong value counts as the default."""
    data = community().get("limits")
    if data is None:
        return []
    if not isinstance(data, dict):
        return [f"`limits` is a block with a whole number for each limit ({', '.join(LIMITS)}): {data!r}; "
                "every limit keeps its default"]
    found = []
    for key, value in data.items():
        if key not in LIMITS:
            found.append(f"`limits.{key}` is not a limit of this software (they are {', '.join(LIMITS)})")
        elif not valid(value):
            found.append(f"`limits.{key}` is a whole number, 0 or more (0 is no limit): {value!r}; "
                         f"the default {default(key)} counts")
    return found


def counters():
    """How each total is counted. Imports here, so that this module can be imported before the apps are ready."""
    from django.contrib.auth import get_user_model
    from meetups.models import Meetup
    from shops.models import DEMO_PREFIX, HelpRequest, Shop

    shops = Shop.objects.exclude(external_id__startswith=DEMO_PREFIX)
    return {
        "shops": lambda: shops.count(),
        "shops_waiting_wallet": lambda: shops.filter(awaiting_wallet=True).count(),
        "accounts": lambda: get_user_model().objects.filter(is_staff=False, is_superuser=False).count(),
        "help_requests": lambda: HelpRequest.objects.count(),
        "nostr_meetups": lambda: Meetup.objects.filter(source=Meetup.Source.NOSTR).count(),
        "emails_per_month": mailbudget.month_count,
        "emails_per_day": mailbudget.day_count,
    }


def used(key):
    return counters()[key]()


def reached(key):
    """True if the total is at its limit: whatever would add one more is refused. Existing data is never touched."""
    limit = get(key)
    return bool(limit) and used(key) >= limit


def refuse(key):
    """reached(), and when it is, a line in the log: `limits.<key>` in `community.yml` changes it."""
    if not reached(key):
        return False
    logger.warning("limit reached: %s is %s (limits.%s in community.yml); nothing is deleted, new ones are refused",
                   key, get(key), key)
    return True


def message():
    """What a visitor is told when a total is full: no number, no key."""
    return _("This site cannot take more of this for now: the association has been told. Please try again later.")


def rows():
    """The totals for the staff: key, what it counts, limit (0 = none), used, share (0 to 1, None without a limit),
    level ("full", "near" or ""), and whether the limit is the default."""
    found = []
    for key, count in counters().items():
        value, now = get(key), count()
        share = now / value if value else None
        level = "" if share is None else "full" if share >= 1 else "near" if share >= WARN_AT else ""
        found.append({"key": key, "what": LIMITS[key].what, "limit": value, "used": now, "share": share, "level": level,
                      "default": value == default(key)})
    return found


def configured():
    """Every limit, rates too: (key, value, whether it is the default), for preflight."""
    return [(key, get(key), get(key) == default(key)) for key in LIMITS]


def notes():
    """One line per total that is near or at its limit, for `manage.py check` and `preflight`."""
    return [f"{row['used']}/{row['limit']} {row['key']}: "
            + ("full, new ones are refused" if row["level"] == "full" else f"over {int(WARN_AT * 100)}%")
            + f" (limits.{row['key']} in community.yml)" for row in rows() if row["level"]]

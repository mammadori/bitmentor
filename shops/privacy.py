"""How long the site keeps what it knows about a person: the `privacy` block of community.yml.

    privacy:
      help_requests_days: 365
      email_prints_days: 62

`help_requests_days` is the number of days after a request for help was solved when `manage.py forget_old_requests` puts away
what the request kept of a person (the merchant's line, the copy of the diagnosis, the account: docs/features/help.md). It is
what the payments timer uses, and what the command uses when it is run without `--days`. A whole number from FLOOR to CEILING;
`0` or `false` is «never from the timer» (the command can still be run by hand with `--days`). A missing block, or a number
that cannot be used, counts as DEFAULTS; `manage.py check` says so (shops.W004). The floor is the same one `--days` has: the
panel shows a solved request for three days, and a request is never forgotten under a month.

`email_prints_days`: how long the email budget keeps its prints of the recipients (config/mailbudget.py); at least 32, since
it counts a month. The site applies no law of its own: these are defaults, and each community sets its own."""
from config.community import community

DEFAULTS = {"help_requests_days": 365, "email_prints_days": 62}
FLOOR, CEILING = 30, 3650
FLOORS = {"email_prints_days": 32}


def options():
    data = community().get("privacy")
    return data if isinstance(data, dict) else {}


def days(name, warn=None):
    """The days of the block, or None when the community wrote 0 or false (never). The default if it is missing or wrong,
    and why to `warn`."""
    value = options().get(name, DEFAULTS[name])
    if value is False or (isinstance(value, int) and not isinstance(value, bool) and value == 0):
        return None
    floor = FLOORS.get(name, FLOOR)
    if isinstance(value, int) and not isinstance(value, bool) and floor <= value <= CEILING:
        return value
    if warn:
        warn(f"`privacy.{name}` is a whole number of days from {floor} to {CEILING}, or 0 or false for never: {value!r}; "
             f"{DEFAULTS[name]} counts instead")
    return DEFAULTS[name]


def problems():
    found = []
    data = community().get("privacy")
    if data is not None and not isinstance(data, dict):
        found.append(f"`privacy` is a block with `help_requests_days`: {data!r}; the defaults count instead")
    for name in options():
        if name not in DEFAULTS:
            found.append(f"`privacy.{name}` is not a setting of the site (they are: {', '.join(DEFAULTS)}): left out")
    for name in DEFAULTS:
        days(name, found.append)
    return found

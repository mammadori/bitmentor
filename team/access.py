"""The switch of team access and the pass it gives (docs/features/team-access.md).

`TEAM_ACCESS=on` in the `.env`, read by Caddy too, turns the feature on; it works only on a site under construction
(`SITE_MODE=development`) and only on the site's own entrance (`X-Entrance: site`, which Caddy writes itself): never on
the onion, in maintenance or in production.

The pass is the `team_pass` cookie: a value signed with SECRET_KEY (django.core.signing, with its own salt) that names
a team key and the key's stamp. `holder` is the only place where a pass is judged: Caddy asks it through
/team/check/ before showing the site to a browser from outside, and Django asks it in `from_allowed_address`
(website/visibility.py), so the two always agree. It reads the cookie and the database at every request: a key revoked,
deleted, or whose passes were ended (a new stamp) stops there, and so does a pass older than PASS_AGE.

What a pass gives is what an address in DEV_ALLOWED_IPS gives, no more. It is not an account and gives no right in
/admin/, which keeps its password and its own rule by address (ADMIN_ALLOWED_IPS): where that rule is left open, a
browser with a pass reaches the admin's sign-in page, as an allowed address does.

`TEAM_AREA=on` adds the volunteers' area (team/area.py): wallets can also sign in while the site is in production, and a
pass of a key with the role `moderator` opens the area, in development and in production. The same pass, read by the
same code (`valid_pass`); `holder` keeps its meaning, a site under construction only."""

from datetime import datetime, timedelta
from datetime import timezone as dt_timezone

from django.conf import settings
from django.core import signing
from django.utils.crypto import constant_time_compare

PASS_COOKIE = "team_pass"
PASS_SALT = "team.pass"
# A pass lasts a week, then the wallet signs again: long enough not to be a chore on a site that changes every day,
# short enough for a browser left signed in, or one that got its pass by a trick, to drop out by itself. It is not
# renewed by use
PASS_AGE = 7 * 86400
ENTRANCE = "X-Entrance"


def on():
    """TEAM_ACCESS=on: the admin shows the keys and the invites."""
    return settings.TEAM_ACCESS


def active():
    """The feature does something only on a site under construction."""
    return settings.TEAM_ACCESS and settings.SITE_MODE == "development"


def here(request):
    """True when this request can use team access at all: the feature active, on the site's own entrance."""
    return active() and request.headers.get(ENTRANCE) == "site"


def area_on():
    """TEAM_AREA=on, which needs TEAM_ACCESS=on: alone it does nothing."""
    return settings.TEAM_ACCESS and settings.TEAM_AREA


def area_active():
    """The area exists on a site under construction and on an open one; in maintenance everything stays closed."""
    return area_on() and settings.SITE_MODE in ("development", "production")


def area_here(request):
    return area_active() and request.headers.get(ENTRANCE) == "site"


def signs_in():
    """Where wallets sign in and invites are taken: a site under construction, and with the area an open site too."""
    return active() or area_active()


def signs_here(request):
    return signs_in() and request.headers.get(ENTRANCE) == "site"


def pass_for(key):
    return signing.dumps({"key": key.pk, "stamp": key.stamp}, salt=PASS_SALT)


def valid_pass(request):
    """(the team key, when its pass was given) for the valid pass this request carries, or None. Whoever calls it has
    already decided that a pass counts here."""
    from .models import TeamKey

    value = request.COOKIES.get(PASS_COOKIE)
    if not value:
        return None
    try:
        data = signing.loads(value, salt=PASS_SALT, max_age=PASS_AGE)
    except signing.BadSignature:  # a forged or changed value, and one past its time (SignatureExpired)
        return None
    if not isinstance(data, dict) or type(data.get("key")) is not int or not isinstance(data.get("stamp"), str):
        return None
    key = TeamKey.active().filter(pk=data["key"]).first()
    if key is None or not constant_time_compare(key.stamp, data["stamp"]):
        return None
    # The value is "payload:timestamp:signature", and the signature just checked covers the timestamp
    given = datetime.fromtimestamp(signing.b62_decode(value.rsplit(":", 2)[1]), tz=dt_timezone.utc)
    return key, given


def read(request):
    if not here(request):
        return None
    found = valid_pass(request)
    return found[0] if found else None


def holder(request):
    """The team key whose valid pass this request carries, or None. Worked out once for a request."""
    if not hasattr(request, "_team_key"):
        request._team_key = read(request)
    return request._team_key


def area_pass(request):
    """(the team key, when its pass ends) for a valid pass where the area exists, whatever the key's role; None
    elsewhere. Worked out once for a request."""
    if not hasattr(request, "_team_area_pass"):
        found = valid_pass(request) if area_here(request) else None
        request._team_area_pass = (found[0], found[1] + timedelta(seconds=PASS_AGE)) if found else None
    return request._team_area_pass


def volunteer(request):
    """The team key whose pass opens the volunteers' area to this request: the area on and here, a valid pass, the role
    `moderator`. None for everybody else, staff accounts included: the area is not for them."""
    from .models import TeamKey

    found = area_pass(request)
    return found[0] if found and found[0].role == TeamKey.Role.MODERATOR else None


def give(response, key):
    """Hands the browser a pass of this key. For the whole site, because Caddy must see it on every path; only over
    HTTPS, and never to a script."""
    response.set_cookie(
        PASS_COOKIE, pass_for(key), max_age=PASS_AGE, path="/", secure=True, httponly=True, samesite="Lax"
    )

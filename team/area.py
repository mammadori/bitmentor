"""The volunteers' area (TEAM_AREA=on, docs/features/team-access.md): the day's work of a moderator, outside /admin/.

Whoever holds a team key with the role `moderator` signs in at /team/ with their wallet, as on a site under
construction, and the pass of that browser (team/access.py, `volunteer`) opens the area's pages: the day's list, the
requests for help with their two actions, the OpenStreetMap requests, the register of the changes to the owners'
accounts, who they are, and, for a key an administrator marked (`TeamKey.approves`), the new shops to approve. No
account (`request.user` is never set here), no right in /admin/, no Lightning address in full, no recovery email, no
link into /admin/.

The area follows the Moderators group (shops/moderators.py, `RIGHTS`) for the day's list, the requests for help and
the register of the account changes, and shows no more of them than the group sees in /admin/. It adds two things of
its own, which the group does not have (`BEYOND_THE_GROUP`), each on purpose: marking an OpenStreetMap request as done
(the shop's public data, nothing written by the site anywhere), and, only for the keys marked by staff who may approve
shops themselves, approving a new shop that does not look like a copy of another.

Every view of the area goes through `area_view`: 404 unless the area is on, on the site's own entrance, and the request
carries a valid pass of a moderator key, before anything else is read or counted, so that the area does not tell it
exists. The sign-in page is the one exception: it is where a pass is got. Every change is a POST with CSRF, re-reads its
object, and writes a row of TeamAction in the same transaction; one that changes nothing writes none.

Where it is reached: on a site under construction Caddy lets a browser with a valid pass through to every address of
the site, any method (Caddyfile, `team-pass-on`), so the area's pages and POSTs reach Django; without a pass /team/ is
the under-construction page, and the team signs in with the page's gesture as before. On an open site everything
reaches Django, which decides. On the onion entrance and in maintenance the area does not exist."""

import posixpath
import re
from datetime import timedelta
from functools import wraps
from urllib.parse import unquote, urlsplit

import segno
from django.core.paginator import Paginator
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.csrf import csrf_exempt, csrf_protect
from django.views.decorators.http import require_GET, require_POST

from config import ratelimit
from config.community import community
from shops import approval, details, guard, moderators, osm
from shops.guard import short_address
from shops.helpdesk import SOLVED_SHOWN
from shops.lightning_watch import when
from shops.models import AccountChange, HelpRequest, OsmRequest, Shop

from . import access
from .models import TeamAction, TeamKey
from .views import (
    CODES,
    K1_COOKIE,
    WINDOW,
    bind,
    lnurl_of,
    over,
    own_challenge,
)

# Pages read, and changes, in WINDOW, per address and per /48 network: a volunteer opens a few pages a minute
PAGES = (300, 900)
CHANGES = (60, 180)
# The register of the account changes, a page at a time; the OpenStreetMap requests closed this recently are shown
CHANGES_PER_PAGE = 50
OSM_SHOWN = timedelta(days=7)
# What the area gives that the Moderators group does not have in /admin/, on purpose (see the module's docstring):
# team/test_area_more.py pins it, so that it is never read as an oversight
BEYOND_THE_GROUP = {
    "osm_done": "mark an OpenStreetMap request as done: the shop's public data, the site writes nothing anywhere",
    "approve": "approve a new shop, only for a key marked by staff who may approve shops, never a shop that looks like a copy",
}
# What the Moderators group may do in /admin/, by the names of its rights: the area shows no more than that
RIGHTS = frozenset(f"{label}.{codename}" for label, codename, _what in moderators.PERMISSIONS)
# Who solved a request, as the admin's action writes it with a staff username: a username cannot hold brackets or
# spaces (Django's validator), so this cannot be taken for one
SOLVED_BY = "[team key {pk}] {name}"
# Anything that looks like an address (a Lightning address, an email) in a text the area shows: cut as the notices
# cut it, `ma…@provider`. And the URL forms of a Lightning address (LUD-16, LUD-17): the name after
# /.well-known/lnurlp/ is cut the same way. A courtesy for what the site itself writes and the obvious forms a person
# types, not a guarantee: a merchant can write an address in free text in a way no pattern catches
ADDRESS = re.compile(r"[^\s@<>()\[\]{};,\"'«»]+@[^\s@<>()\[\]{};,\"'«»]+")
ADDRESS_URL = re.compile(
    r"(?i)\b((?:https?|lnurlp)://[^\s/@<>()\[\]{};,\"'«»]+/\.well-known/lnurlp/)"
    r"([^\s/?#<>()\[\]{};,\"'«»]+)"
)


def shortened(text):
    """`text` with every address in it shortened (shops/guard.py, `short_address`)."""
    text = ADDRESS_URL.sub(lambda found: found[1] + (found[2][:2] + "…" if len(found[2]) > 2 else found[2]), text or "")
    return ADDRESS.sub(lambda found: short_address(found[0]), text)


# The findings of the diagnosis (shops/diagnosis.py, and shops/helpdesk.py's own) that the area's copy keeps, and those
# it drops: the owner's account and how they sign in (the wallet key, the last sign-in, the recovery email, a staff
# account). The changes to the accounts (`account_changed*`, with the staff's usernames) are kept: they are what the
# area's own page of that register shows (`account_changes`). The findings that name the registers of the merchant's
# staff, often the first names of their employees, are kept only when the community says so in community.yml
# (`team_area.staff_register_names: true`, `DIAGNOSIS_BY_CHOICE`); otherwise one neutral line takes their place. A code
# in none of the lists is dropped until somebody puts it in one: team/test_area.py pins the three against every code
# the diagnosis has
DIAGNOSIS_HIDDEN = frozenset({"owner_is_staff", "owner_recovery_email", "owner_no_recovery_email"})
DIAGNOSIS_BY_CHOICE = frozenset({"staff_code_waiting", "staff_off", "staff_ok", "staff_unopened"})
DIAGNOSIS_SHOWN = frozenset(
    {
        "account_changed",
        "account_changed_other",
        "address_broken",
        "address_works",
        "certificate_done",
        "certificate_ready",
        "certificate_revoked",
        "certificate_waiting",
        "check_failed",
        "closed_hidden",
        "closed_ignored",
        "closed_long",
        "closed_now",
        "closed_planned",
        "closed_soon",
        "customer_path_off",
        "customer_path_ok",
        "customer_wallets_empty",
        "first_day_done",
        "first_day_todo",
        "lightning_no_answer",
        "lightning_no_confirmation",
        "lightning_ok",
        "lightning_slow",
        "moved_no_owner",
        "moved_no_wallet",
        "moved_unclaimed",
        "practice_offer",
        "register_ready",
        "register_unavailable",
        "shop_awaiting_approval",
        "shop_closing",
        "shop_historic",
        "shop_no_owner",
        "shop_no_position",
        "shop_suspended",
        "shop_unlisted",
        "shop_visible",
        "shop_waiting_wallet",
        "staff_no_register",
        "time_up",
        "training_done",
        "training_locked",
        "training_missing",
    }
)
# A line of the copy (shops/helpdesk.py, `summary_lines`): "<sign> [<code>] <sentence>", its detail under it
FINDING = re.compile(r"\S+ \[([a-z0-9_]+)\] ")
DETAIL = "    "
NO_DIAGNOSIS = "(the diagnosis could not be made"
# The block of community.yml about the area, and its one key
OPTIONS = "team_area"
STAFF_NAMES = "staff_register_names"


def options(warn=None):
    """{"staff_register_names": bool} from the `team_area` block of community.yml: false unless the community writes
    `true`. What cannot be used counts as false and is told to `warn`."""
    say = warn or (lambda problem: None)
    data = community().get(OPTIONS)
    if data is None:
        return {STAFF_NAMES: False}
    if not isinstance(data, dict):
        say(f"`{OPTIONS}` is a block with the key {STAFF_NAMES}, true or false: {data!r}; it stays off")
        return {STAFF_NAMES: False}
    for name in data:
        if name != STAFF_NAMES:
            say(f"`{OPTIONS}.{name}` is not a setting of the volunteers' area (it has: {STAFF_NAMES}): it does nothing")
    value = data.get(STAFF_NAMES, False)
    if not isinstance(value, bool):
        say(f"`{OPTIONS}.{STAFF_NAMES}` must be true or false, not {value!r}: it stays off")
        value = False
    return {STAFF_NAMES: value}


def problems():
    """What is wrong in the `team_area` block, for `manage.py check` (team/apps.py)."""
    found = []
    options(found.append)
    return found


def diagnosis_for_area(summary, staff_names=None):
    """The copy of the diagnosis as the area shows it: only the findings of DIAGNOSIS_SHOWN (and of DIAGNOSIS_BY_CHOICE
    when the community turned them on, or `staff_names` says so), each with its detail, and the addresses cut. In the
    place of the first finding of DIAGNOSIS_BY_CHOICE left out, one neutral line, so that the volunteer knows to ask.
    A line that belongs to no finding is dropped, except the one that says there is no diagnosis."""
    summary = summary or ""
    if summary.startswith(NO_DIAGNOSIS) and "\n" not in summary:
        return summary
    if staff_names is None:
        staff_names = options()[STAFF_NAMES]
    shown = DIAGNOSIS_SHOWN | DIAGNOSIS_BY_CHOICE if staff_names else DIAGNOSIS_SHOWN
    kept, keep, told = [], False, False
    for line in summary.splitlines():
        found = FINDING.match(line)
        if found:
            keep = found[1] in shown
            if not keep and not told and found[1] in DIAGNOSIS_BY_CHOICE:
                kept.append(
                    "· " + _("Something about the registers of the staff: whoever has the administration sees it.")
                )
                told = True
        elif not line.startswith(DETAIL):
            keep = False
        if keep:
            kept.append(line)
    return shortened("\n".join(kept))


def solved_by(row):
    """Who solved a request, as the area may say it: the name of a team key, never a staff username ("" for a request
    solved from /admin/, which the page says in words)."""
    return row.resolved_by if row.resolved_by.startswith("[team key ") else ""


def area_view(view):
    """The gate of every page of the area: the area on and on this entrance, a valid pass of a moderator key; a 404
    otherwise, before anything is counted. Then CSRF, the limits, and the headers of a private page.

    The CSRF check runs after the gate, not before it as the middleware would: a POST without a token from whoever the
    area is not for gets the 404 of an address that does not exist, not a 403 that tells it does."""

    @csrf_protect
    def inside(request, key, *args, **kwargs):
        if over(
            request,
            "area-change" if request.method == "POST" else "area-page",
            CHANGES if request.method == "POST" else PAGES,
        ):
            response = render(request, "team/area_busy.html", {"page_title": title()}, status=429)
            response["Retry-After"] = str(window_left())
            return response
        return view(request, key, *args, **kwargs)

    @csrf_exempt
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        key = access.volunteer(request)
        if key is None:
            raise Http404
        return private(inside(request, key, *args, **kwargs))

    return wrapper


def window_left():
    return ratelimit.seconds_left(WINDOW)


def private(response):
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def where_the_area_is(view):
    """The sign-in page exists only where the area does: a 404 elsewhere, to every method, before anything else."""

    @csrf_exempt
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not access.area_here(request):
            raise Http404
        # It takes no POST (require_GET): there is no form to protect, and the middleware's 403 would come first
        return private(view(request, *args, **kwargs))

    return wrapper


@where_the_area_is
@require_GET
def sign_in(request):
    """/team/: the sign-in code of the area. A browser whose pass already opens it goes to the day's list. The code is
    the one of /team/login/ (the same limits, the same cap on codes alive, bound to this browser by `team_k1`), and the
    page asks /team/poll/, which names no code in its address."""
    if access.volunteer(request) is not None:
        return redirect("team_today")
    found = access.area_pass(request)
    context = {"page_title": title(), "other_role": found[0].get_role_display() if found else None}
    challenge = None if over(request, "code", CODES) else own_challenge(request)
    if challenge is None:
        response = render(request, "team/area_sign_in.html", {**context, "busy": True}, status=429)
        response["Retry-After"] = str(window_left())
        return response
    lnurl = lnurl_of(challenge, "login")
    response = render(
        request,
        "team/area_sign_in.html",
        {
            **context,
            "lnurl": lnurl,
            "qr": segno.make(lnurl, error="l").svg_data_uri(scale=6, border=2, light="#fff"),
            "poll": reverse("team_own_poll"),
            "script": reverse("team_script"),
            "done": reverse("team_area"),
        },
    )
    bind(response, request, challenge)
    return response


def title():
    return _("Volunteers' area")


def area_items():
    """The day's list as /admin/today/ builds it (shops/dashboard.py, `summary`, and the admin's `flag_copies`), with
    the links a moderator would see there by the rights of the group and that are not into /admin/: [(item, links)]."""
    from shops.admin import flag_copies
    from shops.dashboard import summary

    data = summary()
    flag_copies(data["items"])
    rows = [
        (
            item,
            [
                link
                for link in item.links
                if (not link.get("perm") or link["perm"] in RIGHTS) and not into_admin(link["url"])
            ],
        )
        for item in data["items"]
    ]
    return data, rows


def into_admin(url):
    """A link into /admin/, of this site or written whole: the area has none. The path as a browser and the site
    would resolve it: backslashes as slashes, percent-escapes decoded, doubled slashes, `.` and `..` folded, any
    case. A link that starts with two slashes can be read as a host or as a path: both readings are checked."""
    url = url.replace("\\", "/")
    parts = urlsplit(url)
    paths = [parts.path]
    if not parts.scheme:
        paths.append(url.split("?", 1)[0].split("#", 1)[0])
    for path in paths:
        path = posixpath.normpath(re.sub(r"/+", "/", "/" + unquote(path).replace("\\", "/"))).lower()
        if path == "/admin" or path.startswith("/admin/"):
            return True
    return False


@area_view
@require_GET
def today(request, key):
    """The day's list: the same items as /admin/today/, in the visitor's language as that page, without its links into
    the admin and without its buttons (a key that may approve does it on its own page, `new_shops`)."""
    if "shops.view_shop" not in RIGHTS:
        raise Http404
    data, rows = area_items()
    context = {
        "page_title": title(),
        "key": key,
        "rows": rows,
        "counts": data["counts"],
        "shops": data["shops"],
        "closed": data["closed"],
        "section": "today",
    }
    # The line /admin/today/ has about the register of the changes to the owners' accounts, with the area's own page
    if "shops.view_accountchange" in RIGHTS:
        context["account_changes"] = (
            AccountChange.objects.filter(created__gte=timezone.now() - guard.RECENT)
            .exclude(via=AccountChange.Via.OWNER)
            .count()
        )
    return render(request, "team/area_today.html", context)


def contact(row):
    """How the merchant asked to be called back, with what a volunteer needs to do it, and never the recovery email:
    the shop's public phone, or its Telegram chats."""
    if row.channel == HelpRequest.Channel.PHONE:
        return row.shop.phone
    if row.channel == HelpRequest.Channel.TELEGRAM:
        return row.shop.telegram_chat
    return ""


@area_view
@require_GET
def help_requests(request, key):
    """The requests for help still open, the oldest first as on the day's list, and those solved in the last days (the
    merchant still sees «Solved» in the panel), which can be marked as waiting again."""
    if "shops.view_helprequest" not in RIGHTS:
        raise Http404
    now = timezone.now()
    open_rows = HelpRequest.objects.filter(resolved__isnull=True).select_related("shop").order_by("created", "pk")
    solved = (
        HelpRequest.objects.filter(resolved__gte=now - SOLVED_SHOWN).select_related("shop").order_by("-resolved", "-pk")
    )
    shown = [
        {
            "row": row,
            "when": when(row.created),
            "note": shortened(row.note),
            "summary": diagnosis_for_area(row.summary),
            "contact": contact(row),
            "solved_when": when(row.resolved) if row.resolved else "",
            "solved_by": solved_by(row) if row.resolved else "",
        }
        for row in [*open_rows, *solved]
    ]
    return render(
        request,
        "team/area_help.html",
        {
            "page_title": title(),
            "key": key,
            "open": [entry for entry in shown if not entry["row"].resolved],
            "solved": [entry for entry in shown if entry["row"].resolved],
            "can_change": "shops.change_helprequest" in RIGHTS,
            "section": "help",
        },
    )


def target_of(row):
    return f"help request {row.pk}: {row.shop.name}"


@area_view
@require_POST
def solve(request, key, number):
    """«Mark as solved», as the admin's action: when, and who (the key's name, marked as a team key). Only an open
    request changes, and only then a row of the log is written."""
    if "shops.change_helprequest" not in RIGHTS:
        raise Http404
    row = get_object_or_404(HelpRequest.objects.select_related("shop"), pk=number)
    with transaction.atomic():
        changed = HelpRequest.objects.filter(pk=row.pk, resolved__isnull=True).update(
            resolved=timezone.now(), resolved_by=SOLVED_BY.format(pk=key.pk, name=key.name)[:150]
        )
        if changed:
            TeamAction.write(key, TeamAction.Action.SOLVED, target_of(row))
    return redirect(f"{reverse('team_help')}#request-{row.pk}")


@area_view
@require_POST
def reopen(request, key, number):
    """«Mark as waiting again», as the admin's action: back on top of the day's list."""
    if "shops.change_helprequest" not in RIGHTS:
        raise Http404
    row = get_object_or_404(HelpRequest.objects.select_related("shop"), pk=number)
    with transaction.atomic():
        changed = HelpRequest.objects.filter(pk=row.pk, resolved__isnull=False).update(resolved=None, resolved_by="")
        if changed:
            TeamAction.write(key, TeamAction.Action.WAITING, target_of(row))
    return redirect(f"{reverse('team_help')}#request-{row.pk}")


@area_view
@require_GET
def me(request, key):
    """Who this pass says the visitor is, and until when."""
    _key, ends = access.area_pass(request)
    return render(request, "team/area_me.html", {"page_title": title(), "key": key, "ends": ends, "section": "me"})


@area_view
@require_POST
def end(request, key):
    """Ends this browser's pass: the cookie goes. The other browsers of the key keep theirs (an administrator ends them
    all with «End the passes»)."""
    with transaction.atomic():
        TeamAction.write(key, TeamAction.Action.SIGNED_OUT)
    response = redirect("team_area")
    response.delete_cookie(access.PASS_COOKIE, path="/", samesite="Lax")
    response.delete_cookie(K1_COOKIE, path="/")
    return response


# The new shops (TeamKey.approves) ----------------------------------------------------------------------------------


def waiting_items():
    """The shops that wait, as /admin/today/ lists them (shops/dashboard.py, `approval_items`, and the admin's
    `flag_copies`): the same ones, in the same order, those that look like a copy of another shop first."""
    from shops.admin import flag_copies
    from shops.dashboard import approval_items

    items = approval_items()
    flag_copies(items)
    return items


def judged(shop):
    """What a person needs to judge a new shop: what its page would show the public (shops/details.py), its town and
    description, when it was written. Not the owner's account, not the Lightning address."""
    shown = details.shown(shop)
    return {
        "shop": shop,
        "contacts": [
            (details.LABELS[name], value)
            for name, value in shown.items()
            if name in ("address", "website", "phone", "email")
        ],
        "description": shortened(shop.description),
        "when": when(shop.created),
    }


@area_view
@require_GET
def new_shops(request, key):
    """The new shops that wait, for a key an administrator allowed to approve (`approves`); a 404 for the others."""
    if not key.approves:
        raise Http404
    rows = [{"item": item, **judged(item.shop)} for item in waiting_items()]
    return render(
        request, "team/area_shops.html", {"page_title": title(), "key": key, "rows": rows, "section": "shops"}
    )


@area_view
@require_POST
def approve(request, key, shop):  # BEYOND_THE_GROUP["approve"]
    """Approves one shop that waits, in the name of the key. Never one with a signal that asks for a check, nor any
    when the signals cannot be computed: `approve_or_hold` without `confirmed` holds them, whatever the form sends.
    Those stay for an administrator, on /admin/today/, and so does a shop set aside (shops/approval.py, `set_aside`)."""
    if not key.approves:
        raise Http404
    found = get_object_or_404(Shop, pk=shop)
    with transaction.atomic():
        # The flag read again, under a lock, in the transaction that approves: one removed a moment ago is honoured,
        # and so is a key revoked meanwhile
        if not TeamKey.active().select_for_update().filter(pk=key.pk, approves=True).exists():
            raise Http404
        # Only a shop that is asked about: one an administrator set aside stays for an administrator
        outcome = approval.approve_or_hold(
            approval.waiting().filter(pk=found.pk), None, by=SOLVED_BY.format(pk=key.pk, name=key.name)
        )
        if outcome.approved:
            TeamAction.write(key, TeamAction.Action.APPROVED, f"shop {found.pk}: {found.name}")
    return redirect(f"{reverse('team_shops')}#shop-{found.pk}")


# OpenStreetMap requests ------------------------------------------------------------------------------------------


@area_view
@require_GET
def osm_requests(request, key):  # BEYOND_THE_GROUP: the group has no right on these requests
    """The merchants' requests for the association to do their entry on OpenStreetMap: what to write, the entry if the
    shop has one, and «Mark as done», the admin's own action. The work itself is done by a person on openstreetmap.org
    with their own account: the site writes nothing there. The ones closed in the last days below, to read."""
    now = timezone.now()
    open_rows = OsmRequest.objects.filter(state=OsmRequest.State.OPEN).select_related("shop").order_by("created", "pk")
    closed = (
        OsmRequest.objects.exclude(state=OsmRequest.State.OPEN)
        .filter(done__gte=now - OSM_SHOWN)
        .select_related("shop")
        .order_by("-done", "-pk")
    )
    shown = [
        {
            "row": row,
            "when": when(row.created),
            "lines": shortened(row.summary),
            "entry": osm.element_url(row.shop.osm_ref) if row.shop.osm_ref else "",
            "editor": osm.editor_url(row.shop, row.shop.osm_ref or ""),
            "done_when": when(row.done) if row.done else "",
            "done_by": solved_by_name(row.done_by),
        }
        for row in [*open_rows, *closed]
    ]
    return render(
        request,
        "team/area_osm.html",
        {
            "page_title": title(),
            "key": key,
            "section": "osm",
            "open": [entry for entry in shown if entry["row"].state == OsmRequest.State.OPEN],
            "closed": [entry for entry in shown if entry["row"].state != OsmRequest.State.OPEN],
        },
    )


def solved_by_name(text):
    """The name of a team key, never a staff username ("": the page says it was done from the administration)."""
    return text if text.startswith("[team key ") else ""


@area_view
@require_POST
def osm_done(request, key, number):  # BEYOND_THE_GROUP["osm_done"]
    """«Mark as done», as the admin's action: only an open request, and only then a row of the log."""
    row = get_object_or_404(OsmRequest.objects.select_related("shop"), pk=number)
    with transaction.atomic():
        changed = OsmRequest.objects.filter(pk=row.pk, state=OsmRequest.State.OPEN).update(
            state=OsmRequest.State.DONE, done=timezone.now(), done_by=SOLVED_BY.format(pk=key.pk, name=key.name)[:150]
        )
        if changed:
            TeamAction.write(key, TeamAction.Action.OSM_DONE, f"OpenStreetMap request {row.pk}: {row.shop.name}")
    return redirect(f"{reverse('team_osm')}#osm-{row.pk}")


# The register of the changes to the owners' accounts, to read ----------------------------------------------------


@area_view
@require_GET
def account_changes(request, key):
    """The register of what whoever runs the site changed in the owners' accounts (shops/guard.py), newest first, a page
    at a time: the second pair of eyes of the Moderators group. Who did it is shown, the staff's username: that is the
    point of the register. Nothing else of an account: the register keeps the names of what changed, never the values."""
    if "shops.view_accountchange" not in RIGHTS:
        raise Http404
    page = Paginator(AccountChange.objects.order_by("-created", "-pk"), CHANGES_PER_PAGE).get_page(
        request.GET.get("page")
    )
    rows = [
        {
            "row": row,
            "when": when(row.created),
            "what": ", ".join(guard.words(row.changed.split(","))),
            "via": guard.VIA.get(row.via, row.via),
            "shops": shortened(row.shops),
            "told": ", ".join(str(guard.CHANNELS[code]) for code in row.told.split(",") if code in guard.CHANNELS),
            "not_told": ", ".join(
                str(guard.CHANNELS[code]) for code in row.not_told.split(",") if code in guard.CHANNELS
            ),
        }
        for row in page
    ]
    return render(
        request,
        "team/area_changes.html",
        {"page_title": title(), "key": key, "rows": rows, "page": page, "section": "changes"},
    )

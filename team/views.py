"""The addresses of team access, all under /team/ (docs/features/team-access.md): the sign-in a page under construction
asks for, the wallet's callback (LUD-04), the check Caddy makes of a pass, and the page of an invite. Caddy lets these
addresses through from outside one by one, GET only (Caddyfile, `team-open`): a new one goes there too.

Every one answers 404 unless the feature is active on this entrance (team/access.py). The signature is checked by the
same code as the merchants' sign-in (shops/lnurl_auth.py): compressed keys only, 32 random bytes of challenge, the first
signature wins, a challenge is used once."""
import io
import logging
from functools import wraps
from pathlib import Path

import segno
from django.conf import settings
from django.db import IntegrityError, transaction
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone, translation
from django.utils.crypto import constant_time_compare
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.http import require_GET

from config import ratelimit
from shops.lnurl_auth import K1, encode_lnurl, verify
from shops.notifications import alert_text, deliver_to_moderators

from . import access
from .models import CHALLENGE_LIFETIME, TeamChallenge, TeamInvite, TeamKey, token_hash

logger = logging.getLogger(__name__)
# The cookie that binds a challenge to the browser that asked for it: anybody in the room can see the QR code, and only
# that browser gets the answer of its poll. Only for /team/
K1_COOKIE = "team_k1"
K1_SALT = "team.k1"
K1_PATH = "/team/"
WINDOW = 600
# In WINDOW, for an address (IPv6: its /64) and for its /48 network. Sign-in codes asked: a browser makes two in ten
# minutes. Signatures: a wallet sends one. Invite pages opened: a person opens one, a few times
CODES = (30, 90)
SIGNATURES = (20, 60)
INVITES = (30, 90)
# A page asks every two seconds while its code lives
POLLS = (600, 1800)
SCRIPT = (Path(__file__).resolve().parent / "static" / "team" / "join.js").read_bytes()


def gated(view):
    """`view` where team access is active, on the site's own entrance; a 404 everywhere else, before anything is read or
    counted: with the feature off the address does not exist."""
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not access.here(request):
            raise Http404
        response = view(request, *args, **kwargs)
        response.headers.setdefault("Cache-Control", "private, no-store")
        response.headers.setdefault("X-Robots-Tag", "noindex, nofollow")
        return response
    return wrapper


def over(request, name, limits):
    return ratelimit.over_address_limits(request, f"team-{name}", *limits, WINDOW)


def too_many(wallet=False):
    """The 429 of every address that answers JSON: `error` for a page, and LUD-04's `status` and `reason` for a wallet."""
    message = _("Too many requests from your connection: try again in a few minutes.")
    body = {"status": "ERROR", "reason": message, "error": message} if wallet else {"error": message}
    return JsonResponse(body, status=429, headers={"Retry-After": str(ratelimit.seconds_left(WINDOW))})


def own_challenge(request, invite=None):
    """This browser's challenge if it can still be used (not signed, young enough to be scanned), else a new one; None
    when too many are alive."""
    k1 = own_k1(request)
    if k1:
        young = timezone.now() - CHALLENGE_LIFETIME / 2
        found = TeamChallenge.objects.filter(k1=k1, linking_key="", created__gte=young, invite=invite).first()
        if found:
            return found
    return TeamChallenge.new(invite)


def lnurl_of(challenge, action):
    """The wallet signs for the domain of the callback, always the site's own name: its prompt says that name."""
    return encode_lnurl(f"{settings.PUBLIC_URL}{reverse('team_auth')}?tag=login&action={action}&k1={challenge.k1}")


def qr_svg(text):
    """The QR code as an <svg> element with its namespace: a page can put it in as it is, or show it as an image
    (a data: address), which without the namespace draws nothing."""
    drawn = io.BytesIO()
    segno.make(text, error="l").save(drawn, kind="svg", xmldecl=False, svgns=True, nl=False, scale=6, border=2, light="#fff")
    return drawn.getvalue().decode()


def bind(response, request, challenge):
    response.set_signed_cookie(K1_COOKIE, challenge.k1, salt=K1_SALT, httponly=True, samesite="Lax", path=K1_PATH,
                               secure=request.is_secure(), max_age=int(CHALLENGE_LIFETIME.total_seconds()))


# What the team hears, in the community's language
JOINED = gettext_lazy("{name} joined the team ({role}): from now on their wallet opens the site under construction. "
                      "The team's keys:")
NEW_PASS = gettext_lazy("{name} ({role}) opened the site under construction in a new browser. If it was not them, "
                        "revoke the key or end its passes:")


class Answer(JsonResponse):
    """A JSON answer that can do one more thing once the server has sent it (`then`): whatever that is, the visitor is
    not kept waiting for it and cannot be failed by it."""
    then = None

    def close(self):
        work, self.then = self.then, None
        try:
            if work is not None:
                work()
        except Exception:  # the answer is given: whatever comes after it must not turn into an error
            logger.exception("team access: what was to be done after the answer failed")
        finally:
            super().close()


def tell_admins(response, message, key):
    """Tells the team once `response` has been sent: to the moderators' chat, or to the community's email without one
    (shops/notifications.py). The sign-in is neither delayed nor failed by it: Telegram and the mail server are waited
    for, ten seconds each at most, by the worker's thread after the answer has left, and a message that cannot be
    sent, for whatever reason, is a line in the log."""
    name, role = key.name, key.get_role_display

    def deliver():
        from shops.management.commands.dashboard import site_label

        with translation.override(settings.LANGUAGE_CODE):
            text = str(message).format(name=alert_text(name), role=role())
            link = f"{settings.PUBLIC_URL}{reverse('admin:team_teamkey_changelist')}"
            where, problems = deliver_to_moderators(str(_("Team access")), f"{site_label()}\n{text}\n{link}")
        if where is None:
            logger.warning("team access: the team was not told of a new key or pass (%s)", "; ".join(problems))

    response.then = deliver


@gated
@require_GET
def login(request):
    """A sign-in code for the page under construction: the QR as SVG, the LNURL and the address to poll."""
    if over(request, "code", CODES):
        return too_many()
    challenge = own_challenge(request)
    if challenge is None:
        return too_many()
    lnurl = lnurl_of(challenge, "login")
    response = JsonResponse({"svg": qr_svg(lnurl), "lnurl": lnurl, "poll": reverse("team_poll", args=[challenge.k1])})
    bind(response, request, challenge)
    return response


def own_k1(request):
    """The k1 of the challenge this browser was given, from its signed cookie; "" without one."""
    k1 = request.get_signed_cookie(K1_COOKIE, "", salt=K1_SALT, max_age=CHALLENGE_LIFETIME.total_seconds())
    return k1 if K1.fullmatch(k1) else ""


def state_of(challenge):
    """What became of this browser's challenge: the answer of both polls. With "ok" comes the pass, once."""
    if challenge is None:
        return JsonResponse({"state": "expired"})
    if not challenge.linking_key:
        # The code of an invite that another wallet took meanwhile, or that was deleted, has nothing left to wait for
        spent = challenge.invite_id and not TeamInvite.open().filter(pk=challenge.invite_id).exists()
        return JsonResponse({"state": "denied" if spent else "pending"})
    key = TeamKey.active().filter(linking_key=challenge.linking_key).first()
    if key is None:
        return JsonResponse({"state": "denied"})
    # Used once: of two polls at the same time only one deletes it, and only that one gives a pass
    if not TeamChallenge.objects.filter(pk=challenge.pk).delete()[0]:
        return JsonResponse({"state": "expired"})
    TeamKey.objects.filter(pk=key.pk).update(last_pass=timezone.now())
    response = Answer({"state": "ok"})
    access.give(response, key)
    response.delete_cookie(K1_COOKIE, path=K1_PATH)
    # A wallet signs whatever code it is shown: if somebody talked this person into scanning a code of theirs, the pass
    # is in their browser. The team hears of every new one, and can end it
    tell_admins(response, NEW_PASS, key)
    return response


@gated
@require_GET
def poll(request, k1):
    """What became of a sign-in code, for the browser it was made for: any other gets "expired". The k1 in the address
    is the one of the QR code, which opens nothing by itself; the code of an invite's page, which until it is signed is
    worth the invite, is never asked for by an address that names it (`own_poll`), and is not answered here."""
    if over(request, "poll", POLLS):
        return too_many()
    mine = own_k1(request)
    challenge = None
    if mine and constant_time_compare(mine, k1):
        challenge = TeamChallenge.alive().filter(k1=mine, invite__isnull=True).first()
    return state_of(challenge)


@gated
@require_GET
def own_poll(request):
    """The same answer for the challenge this browser's cookie names, with nothing in the address: what the invite's
    page asks, so that its k1 is in no address a log could write."""
    if over(request, "poll", POLLS):
        return too_many()
    mine = own_k1(request)
    return state_of(TeamChallenge.alive().filter(k1=mine).first() if mine else None)


def wallet_answer(reason=None, status=200):
    """LUD-04's answer to the wallet."""
    if reason is None:
        return JsonResponse({"status": "OK"})
    return JsonResponse({"status": "ERROR", "reason": str(reason)}, status=status)


def claim(challenge, key):
    """Writes on a challenge the key that signed it: the first signature counts, a second one does not replace it. True
    if the challenge is this key's."""
    TeamChallenge.objects.filter(pk=challenge.pk, linking_key="").update(linking_key=key)
    return TeamChallenge.objects.filter(pk=challenge.pk, linking_key=key).exists()


def join_with(challenge, key):
    """The first valid signature of an invite makes the team key and uses the invite up. Returns (team key, None) or
    (None, why not). The invite is taken with one conditional UPDATE: of two wallets signing at the same time, on the
    same code or on two pages of the same invite, one makes the key and the other gets nothing."""
    if TeamKey.objects.filter(linking_key=key).exists():
        # Nothing is used up: the invite and the code stay good for the wallet they were meant for
        return None, _("This wallet already has a team key here, or had one: ask whoever invited you.")
    invalid = _("This invite is not valid any more.")
    try:
        with transaction.atomic():
            if not claim(challenge, key):
                return None, invalid
            now = timezone.now()
            if not TeamInvite.objects.filter(pk=challenge.invite_id, used__isnull=True, expires__gt=now).update(used=now):
                transaction.set_rollback(True)
                return None, invalid
            invite = TeamInvite.objects.get(pk=challenge.invite_id)
            made = TeamKey.objects.create(linking_key=key, name=invite.name, role=invite.role)
            TeamInvite.objects.filter(pk=invite.pk).update(key=made)
    except IntegrityError:
        # The same wallet signing two invites at the same time: the other one made the key
        return None, invalid
    return made, None


@gated
@require_GET
def auth(request):
    """The wallet's callback: tag=login, k1, sig, key."""
    if over(request, "signature", SIGNATURES):
        return too_many(wallet=True)
    k1 = request.GET.get("k1", "")
    key = request.GET.get("key", "").lower()
    challenge = TeamChallenge.alive().filter(k1=k1).first() if K1.fullmatch(k1) else None
    if challenge is None or not verify(challenge.k1, request.GET.get("sig", ""), key):
        return wallet_answer(_("Invalid sign-in"))
    if challenge.linking_key:
        # Signed already: the same wallet asking again hears what it heard, another one cannot take the code
        known = challenge.linking_key == key and TeamKey.active().filter(linking_key=key).exists()
        return wallet_answer(None if known else _("Invalid sign-in"))
    if challenge.invite_id is None:
        if not claim(challenge, key):
            return wallet_answer(_("Invalid sign-in"))
        if not TeamKey.active().filter(linking_key=key).exists():
            # The page that showed the code hears "denied"; nothing else happens
            return wallet_answer(_("This wallet is not in the team of this site."))
        return wallet_answer()
    made, why_not = join_with(challenge, key)
    if made is None:
        return wallet_answer(why_not)
    response = Answer({"status": "OK"})
    tell_admins(response, JOINED, made)
    return response


@gated
@require_GET
def check(request):
    """For Caddy's forward_auth: 204 with a valid pass, 401 without. The same judgement as from_allowed_address
    (access.holder). No counter here: it is asked for every file of a page, a counter would write to the database each
    time, and a forged pass is refused by its signature before the database is read."""
    return HttpResponse(status=204 if access.holder(request) else 401)


def invite_page(request, status=200, **context):
    response = render(request, "team/join.html", context, status=status)
    # The address of this page opens something: it is told to no other site, and no cache keeps the page
    response["Referrer-Policy"] = "no-referrer"
    return response


def signed_here(request, token):
    """The code of this invite that this browser showed and a wallet signed, while the browser has not asked for its
    pass yet: a phone that loads the page again on the way back from the wallet finds the invite used, and must still
    get what its own poll would have got. Only the browser with the code's cookie, as for the poll."""
    k1 = own_k1(request)
    if not k1 or not 20 <= len(token) <= 100:
        return None
    return TeamChallenge.alive().filter(k1=k1, invite__token_hash=token_hash(token)).exclude(linking_key="").first()


@gated
@require_GET
def join(request, token):
    """The page of an invite: the code its wallet signs, once."""
    if over(request, "invite", INVITES):
        response = invite_page(request, status=429, state="busy")
        response["Retry-After"] = str(ratelimit.seconds_left(WINDOW))
        return response
    invite = TeamInvite.by_token(token)
    if invite is None:
        signed = signed_here(request, token)
        if signed is None:
            return invite_page(request, status=404, state="invalid")
        return invite_page(request, state="signed", poll=reverse("team_own_poll"), script=reverse("team_script"))
    challenge = own_challenge(request, invite)
    if challenge is None:
        response = invite_page(request, status=429, state="busy")
        response["Retry-After"] = str(ratelimit.seconds_left(WINDOW))
        return response
    lnurl = lnurl_of(challenge, "register")
    response = invite_page(request, state="waiting", invite=invite, lnurl=lnurl,
                           qr=segno.make(lnurl, error="l").svg_data_uri(scale=6, border=2, light="#fff"),
                           poll=reverse("team_own_poll"), script=reverse("team_script"))
    bind(response, request, challenge)
    return response


@gated
@require_GET
def script(request):
    """The script of the invite's page, from under /team/: on a site under construction /static/ is closed to whoever
    is outside, and the page's content security policy takes scripts only from the site."""
    return HttpResponse(SCRIPT, content_type="text/javascript; charset=utf-8", headers={"Cache-Control": "no-cache"})

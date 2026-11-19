"""Sign-in to /admin/ with a Lightning wallet (LNURL-auth, LUD-04): the hobby level, `ADMIN_WALLET_SIGN_IN`.

**Who it is for.** Whoever tries the site with a few friends, a team learning the tools: a staff account links its own
wallet, and that wallet then opens /admin/ in place of the password, on the browsers where the account already signed in
with its password. One step, not two: whoever holds the unlocked phone with that wallet, and such a browser, has /admin/.
The password keeps working.

**When.** `ADMIN_WALLET_SIGN_IN=on` (off by default: none of this exists, its addresses answer 404 and /admin/ is as it
always was), and only with `ADMIN_SECOND_FACTOR=optional`. With `required` or `passkey` a wallet never opens /admin/:
the linked wallets stay stored and unused, the page says so, `manage.py check` warns (shops.W016) and preflight notes
it. An account that has a second factor (a code from an app, a passkey) chose more than one step: its wallet does not
open its /admin/. A wallet is never a way around a second factor, and never a second factor itself.

**Only on a known browser.** A wallet signs whatever code it is shown: whoever opens the sign-in page in their own
browser and gets a staff member to scan its QR code would otherwise be in /admin/ (a relay). So a wallet signs in only
a browser that carries the account's `admin_device` cookie (config/urls.py, `known_device`): one where that account
signed in with its password before, within KNOWN_DEVICE_AGE, and whose password has not changed since (the cookie holds
the account's session auth hash, an HMAC of its password hash). After a change of password the wallets sign in nowhere
until the new password is used once on that browser; they stay linked. A wallet sign-in does not renew the cookie: the
password is used at least once in KNOWN_DEVICE_AGE on each browser.

**Linking.** On the account's own second factor's page (/admin/second-factor/), in a session opened with the password
(never one opened with a wallet, nor one opened with a code only to add a passkey): a QR code with a fresh k1 kept in
that session (WALLET_LINK_SESSION); the wallet signs; the page shows the start and the end of the key that signed and
the person confirms it with a name (a QR code anyone in the room can see is signed first by whoever scans it first: the
confirmation is the owner's). At most MAX_WALLETS per account; renamed and removed on the same page, from a session of
the password too, never another account's. **One key, one role:** a key already known as a shop owner's (an account
born from it, or a WalletKey) or as a team key (TeamKey, a revoked one too) is refused, with a reason that says nothing
of whose it is; and a key linked here is refused at the shops' sign-in, at the connection of a wallet to an owner's
account and at a team invite (`staff_key`). Each side checks again after writing; only the key's own holder, signing
both at the same instant, could slip between the two checks.

**The codes.** A k1 is made by this module and checked by its own HMAC (`new_k1`, `read_k1`): its time, its purpose
(sign-in or link), random bytes. Nothing is written until a wallet signs a k1 that is valid and alive; only then does
the callback count the signature against the limits of the wallet's address and network, and write the key that signed
on a row (AdminWalletChallenge), the first one only. The browser is bound to its k1 on the server: the k1 lives in the
browser's own session (SIGN_IN_SESSION, WALLET_LINK_SESSION), never in a cookie or an address the page asks, and the
polls look only for the k1 that session holds; a k1 signed for one browser tells nothing to another one.

**The sign-in.** /admin/login/wallet/ shows the QR code (CHALLENGE_SECONDS, used once). The poll
(/admin/login/wallet/status/) takes the signed row once, counts it against the sign-in's limits by address (as a
password), and signs in a key linked to an active staff account without a second factor, from a known browser of that
account: the session gets a new key (`login`), is the admin's (ADMIN_SESSION) and the wallet's (WALLET_SESSION: its id
and the time it was opened), and the account's history gets one line. Anything else gets one and the same answer,
«refused», and nothing is made.

**The session.** A session opened with a wallet holds while that wallet is still linked to the account, the switch is
on, the level `optional`, the account has no second factor, and ADMIN_SESSION_HOURS at most (`session_holds`, read by
config/second_factor.py); a change of password ends it as it ends every session. Removing wallet A ends the sessions
opened with A, not those opened with B.

**Where.** Only at the site's own address (PUBLIC_URL), as passkeys: the wallet signs for the callback's domain, which
is always PUBLIC_URL. On the onion or another name of the machine the pages say so and show no code.

Linking, removing, the reset and every sign-in are written in the account's history; linking, removing and the reset
are also told to the people who look after the site (config/second_factor.py, `record_change`).
"""
import hmac
import logging
import secrets
from datetime import timedelta

import segno
from django.conf import settings
from django.contrib import admin, messages
from django.contrib.admin.models import CHANGE as LOGGED_CHANGE
from django.contrib.admin.models import LogEntry
from django.contrib.auth import REDIRECT_FIELD_NAME, get_user_model
from django.contrib.auth import login as auth_login
from django.db import IntegrityError, transaction
from django.http import (
    Http404,
    HttpResponseNotAllowed,
    HttpResponseRedirect,
    JsonResponse,
)
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils import timezone
from django.utils.crypto import salted_hmac
from django.utils.http import url_has_allowed_host_and_scheme, urlencode
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.cache import never_cache

from config import ratelimit, second_factor
from config.guard import (
    ADMIN_SESSION,
    WALLET_LINK_SESSION,
    WALLET_SESSION,
)
from shops.lnurl_auth import K1, encode_lnurl, verify

logger = logging.getLogger(__name__)

# In the session of a browser on the wallet's sign-in page: {"k1", "until" (Unix time), "next"}
SIGN_IN_SESSION = "admin_wallet_sign_in"
CHALLENGE_SECONDS = 300
MAX_WALLETS = 5
# Signatures at the callback, for a valid and live k1: in WINDOW, for an address (IPv6: its /64) and for its /48
# network. A wallet sends one; every row of AdminWalletChallenge comes from one of them
WINDOW = 600
SIGNATURES = (20, 60)
# Sign-in pages shown to browsers without a session, per address and per network in WINDOW
PAGES = (30, 90)
# What a k1 says of itself (`new_k1`): sign in, or link a wallet
SIGN_IN, LINK = b"s", b"l"
K1_SALT = "config.admin_wallet.k1"
TEMPLATE = "admin/wallet_login.html"
LOGIN_TEMPLATE = "admin/login_with_wallet.html"
ACTIONS = ("wallet-link", "wallet-cancel", "wallet-rename", "wallet-remove")
# The account's history, at a sign-in with a wallet
SIGNED_IN = "Signed in to /admin/ with a linked wallet"

# What the people who look after the site are told; {name} is the staff account
SUBJECT = gettext_lazy("Wallet sign-in")
LINKED = gettext_lazy("The staff account {name} linked a wallet that signs in to /admin/.")
REMOVED = gettext_lazy("The staff account {name} removed a wallet that signed in to /admin/.")
RESET = gettext_lazy("The wallets of the staff account {name} were removed with manage.py admin_wallet reset.")
# The register's code of the change (shops/guard.py, WORDS)
CHANGE = "admin_wallet"


def on():
    """ADMIN_WALLET_SIGN_IN=on: the page links wallets, and the callback answers."""
    return settings.ADMIN_WALLET_SIGN_IN


def signs_in():
    """On, and at the level where a wallet opens /admin/: `optional`."""
    return on() and second_factor.mode() == "optional"


def here(request):
    """True at the site's own address, the only one the wallet's callback names (as passkeys: not on the onion, nor at
    another name of the machine)."""
    return second_factor.passkeys_here(request)


def staff_key(key):
    """True when `key` is linked to a staff account's sign-in to /admin/: it is then nobody's key on the shops' side or
    the team's."""
    from shops.models import AdminWallet

    return bool(key) and AdminWallet.objects.filter(linking_key=key).exists()


def known_elsewhere(key):
    """True when `key` already has another role here: it signs in to a shop owner's account (the account born from it,
    or a connected WalletKey), or it is, or was, a team key."""
    from shops.models import WalletKey
    from team.models import TeamKey

    return (WalletKey.objects.filter(key=key).exists()
            or get_user_model()._default_manager.filter(username=key).exists()
            or TeamKey.objects.filter(linking_key=key).exists())


def short_key(key):
    return f"{key[:6]}…{key[-4:]}"


def opened_with_wallet(request):
    """A session opened with a wallet: it does not manage the wallets and adds no second factor (config/second_factor.py,
    `setup_view`), so it never becomes anything more than it was opened as. Whoever holds the phone for a moment cannot
    add a way in of their own, nor lock the account's owner out with a factor only they have."""
    return WALLET_SESSION in request.session


# --- The codes -----------------------------------------------------------------------------------------------------


def mac(body):
    return salted_hmac(K1_SALT, body, algorithm="sha256").digest()[:16]


def new_k1(purpose):
    """A new k1, 32 bytes in hex: the time it was made (5 bytes), its purpose (1), random bytes (10), and the HMAC of
    those (16). Nothing is written: the callback knows a k1 of this site by its HMAC."""
    body = int(second_factor.now()).to_bytes(5, "big") + purpose + secrets.token_bytes(10)
    return (body + mac(body)).hex()


def read_k1(k1, purpose=None):
    """True for a k1 made here (and for `purpose`, if given) that is still alive: CHALLENGE_SECONDS from its time."""
    if not isinstance(k1, str) or not K1.fullmatch(k1):
        return False
    raw = bytes.fromhex(k1)
    body, signature = raw[:16], raw[16:]
    if not hmac.compare_digest(mac(body), signature):
        return False
    if purpose is not None and body[5:6] != purpose:
        return False
    return 0 <= second_factor.now() - int.from_bytes(body[:5], "big") < CHALLENGE_SECONDS


def held(request, name, purpose):
    """The k1 this session was given under `name`, while it lives; "" otherwise."""
    data = request.session.get(name)
    k1 = data.get("k1") if isinstance(data, dict) else None
    return k1 if read_k1(k1, purpose) else ""


def signed_row(k1):
    """The row a wallet's signature wrote for `k1`, or None while nobody signed it."""
    from shops.models import AdminWalletChallenge

    return AdminWalletChallenge.objects.filter(k1=k1).first() if k1 else None


def own_or_new(request, name, purpose, **extra):
    """This session's k1 under `name` if it is still young and nobody signed it (a page shown again shows the same
    code), else a new one, kept in the session. Returns the k1."""
    k1 = held(request, name, purpose)
    if k1 and int(second_factor.now()) - int(k1[:10], 16) < CHALLENGE_SECONDS / 2 and signed_row(k1) is None:
        request.session[name] = {**request.session[name], **extra}
        return k1
    k1 = new_k1(purpose)
    request.session[name] = {"k1": k1, **extra}
    return k1


def code_of(k1, action):
    """The LNURL and its QR code: the wallet signs for the callback's domain, always the site's own name."""
    lnurl = encode_lnurl(f"{settings.PUBLIC_URL}{reverse('admin_wallet_auth')}?tag=login&action={action}&k1={k1}")
    return {"lnurl": lnurl, "qr": segno.make(lnurl, error="l").svg_data_uri(scale=6, border=2, light="#fff")}


def wallet_error(reason="Invalid login", status=200):
    return JsonResponse({"status": "ERROR", "reason": reason}, status=status)


@never_cache
def auth(request):
    """What the wallet calls with its signature (LUD-04): tag=login, k1, sig, key. A k1 that is not one of this site's,
    or no longer alive, costs nothing but its HMAC and gets the generic error. For a live one the signature counts
    against the limits of the wallet's address; a right one writes the key on the k1's row, the first key only; the same
    wallet asking again hears «OK» again, another one is refused. Nothing else is decided here: the page's poll decides,
    for the session that holds the k1."""
    from shops.models import AdminWalletChallenge

    if not on():
        raise Http404
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    k1 = request.GET.get("k1", "")
    key = request.GET.get("key", "").lower()
    if not read_k1(k1):
        logger.info("admin wallet: refused, no such challenge")
        return wallet_error()
    if ratelimit.over_address_limits(request, "admin-wallet-signature", *SIGNATURES, WINDOW):
        response = wallet_error("Too many requests from your connection: try again in a few minutes.", status=429)
        response["Retry-After"] = str(ratelimit.seconds_left(WINDOW))
        return response
    if not verify(k1, request.GET.get("sig", ""), key):
        logger.info("admin wallet: refused, the signature is not of this challenge")
        return wallet_error()
    # The rows of codes that can no longer be signed go first: nothing else cleans the table
    AdminWalletChallenge.objects.filter(created__lt=timezone.now() - timedelta(seconds=CHALLENGE_SECONDS)).delete()
    try:
        with transaction.atomic():
            row, _made = AdminWalletChallenge.objects.get_or_create(k1=k1, defaults={"linking_key": key})
    except IntegrityError:
        # Another wallet's signature wrote it a moment ago
        row = AdminWalletChallenge.objects.get(k1=k1)
    if row.linking_key != key:
        logger.info("admin wallet: refused, the challenge was signed by another key")
        return wallet_error()
    logger.info("admin wallet: signed (%s)", "link" if bytes.fromhex(k1)[5:6] == LINK else "sign in")
    return JsonResponse({"status": "OK"})


# --- The sign-in ---------------------------------------------------------------------------------------------------


def login_link(request, context):
    """The login page's link to the wallet's sign-in, when there is one: the template that shows it, and its address.
    None when off, or at a level other than `optional`: the login page is then exactly as it always was."""
    if not signs_in():
        return None
    target = context.get(REDIRECT_FIELD_NAME) or request.GET.get(REDIRECT_FIELD_NAME, "")
    url = reverse("admin_wallet_login")
    return {"wallet_sign_in_url": f"{url}?{urlencode({REDIRECT_FIELD_NAME: target})}" if target else url}


@never_cache
def login_page(request):
    """«Sign in with your wallet»: the QR code to sign, for this browser's session. Only on and `optional`."""
    if not signs_in():
        raise Http404
    site = admin.site
    if site.has_permission(request):
        return HttpResponseRedirect(reverse("admin:index", current_app=site.name))
    request.current_app = site.name
    target = str(request.GET.get(REDIRECT_FIELD_NAME, ""))[:2000]
    password_url = reverse("admin:login", current_app=site.name)
    context = {**site.each_context(request), "title": _("Log in"), "subtitle": None, "here": here(request),
               "public_url": settings.PUBLIC_URL, "status_url": reverse("admin_wallet_status"),
               "password_url": f"{password_url}?{urlencode({REDIRECT_FIELD_NAME: target})}" if target else password_url}
    # A visitor whose session holds no code gets one here, and with it a row of the sessions' table (also with a made-up
    # session cookie): no more of them per address than this, or the table would grow at the cost of a GET. Past it
    # the page has no code, and says to wait
    if context["here"] and SIGN_IN_SESSION not in request.session and ratelimit.over_address_limits(
            request, "admin-wallet-page", *PAGES, WINDOW):
        context.update(here=False, busy=True)
    if context["here"]:
        context.update(code_of(own_or_new(request, SIGN_IN_SESSION, SIGN_IN, next=target), "login"))
    return TemplateResponse(request, TEMPLATE, context)


def state(name, status=200):
    return JsonResponse({"state": name}, status=status)


@never_cache
def login_status(request):
    """What became of this session's code, asked by its page every two seconds: pending, expired, refused, wait (the
    sign-in's limits), or ok with where to go, once signed in. Whatever keeps the wallet out (a key not linked, an
    account no longer staff or active, one with a second factor, a browser where the account never signed in with its
    password or not since its password changed) is «refused», all the same."""
    from shops.models import AdminWallet, AdminWalletChallenge

    if not signs_in():
        raise Http404
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    k1 = held(request, SIGN_IN_SESSION, SIGN_IN)
    if not k1:
        return state("expired")
    row = signed_row(k1)
    if row is None:
        return state("pending")
    target = str(request.session[SIGN_IN_SESSION].get("next") or "")
    # Used once: of two polls at the same moment only one goes on
    if not AdminWalletChallenge.objects.filter(pk=row.pk, used=None).update(used=timezone.now()):
        return state("expired")
    request.session.pop(SIGN_IN_SESSION, None)
    from config.urls import ADMIN_WINDOW, known_device

    # As a password: every signature that tries to sign in counts against the limits of the address and its network
    if ratelimit.over_address_limits(request, "admin", 10, 30, ADMIN_WINDOW):
        response = state("wait", status=429)
        response["Retry-After"] = str(ratelimit.seconds_left(ADMIN_WINDOW))
        return response
    wallet = AdminWallet.objects.filter(linking_key=row.linking_key).select_related("user").first()
    user = wallet.user if wallet else None
    if (user is None or not (user.is_active and user.is_staff) or not here(request)
            or second_factor.accepted_factors(user) or not known_device(request, user.get_username())):
        logger.info("admin wallet: sign-in refused")
        return state("refused")
    auth_login(request, user, backend=settings.AUTHENTICATION_BACKENDS[0])
    request.session[ADMIN_SESSION] = True
    request.session[WALLET_SESSION] = [wallet.pk, second_factor.now()]
    AdminWallet.objects.filter(pk=wallet.pk).update(last_used=timezone.now())
    LogEntry.objects.log_actions(user.pk, [user], LOGGED_CHANGE, change_message=SIGNED_IN, single_object=True)
    logger.info("admin wallet: signed in account %s", user.pk)
    if not url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        target = reverse("admin:index", current_app=admin.site.name)
    return JsonResponse({"state": "ok", "next": target})


# --- The session ---------------------------------------------------------------------------------------------------


def session_holds(request):
    """For a session opened with a wallet (WALLET_SESSION), not with a second factor since: True while that wallet is
    still linked to the account, the switch on, the level `optional`, the account without a second factor, and within
    ADMIN_SESSION_HOURS. Worked out once for a request."""
    if not hasattr(request, "_admin_wallet_holds"):
        request._admin_wallet_holds = wallet_holds(request)
    return request._admin_wallet_holds


def wallet_holds(request):
    from shops.models import AdminWallet

    try:
        key, since = request.session.get(WALLET_SESSION)
        since = float(since)
    except (TypeError, ValueError):
        return False
    if type(key) is not int or not signs_in() or second_factor.factors_of_request(request):
        return False
    if not AdminWallet.objects.filter(pk=key, user_id=request.user.pk).exists():
        return False
    return 0 <= second_factor.now() - since < settings.ADMIN_SESSION_HOURS * 3600


# --- The page of the account's own wallets -------------------------------------------------------------------------


def signed_link(request):
    """This session's code to link a wallet, once a wallet signed it and until the person decides."""
    row = signed_row(held(request, WALLET_LINK_SESSION, LINK))
    return row if row is not None and row.used is None else None


def page_context(request):
    """The wallets' part of the second factor's page: none when off. The account's wallets; whether they sign in here
    and now; and, in a session that may manage them (opened with the password), the key that signed waiting for its
    name, or a new code to scan."""
    from shops.models import AdminWallet

    if not on():
        return {}
    wallets = list(AdminWallet.objects.filter(user=request.user))
    context = {"wallet_on": True, "wallets": wallets, "wallet_limit": MAX_WALLETS, "wallet_signs_in": signs_in(),
               "wallet_mode": second_factor.mode(), "wallet_has_factor": bool(second_factor.factors_of_request(request)),
               "wallet_here": here(request), "public_url": settings.PUBLIC_URL,
               "wallet_status_url": reverse("admin_wallet_link_status")}
    if second_factor.bootstrap_session(request):
        context["wallet_locked"] = True
        return context
    if opened_with_wallet(request):
        context.update({"wallet_locked": True, "wallet_from_wallet": True})
        return context
    signed = signed_link(request)
    if signed is not None:
        context["wallet_signed"] = short_key(signed.linking_key)
    elif len(wallets) < MAX_WALLETS and context["wallet_here"]:
        k1 = own_or_new(request, WALLET_LINK_SESSION, LINK)
        context.update({f"wallet_{name}": value for name, value in code_of(k1, "link").items()})
    return context


@never_cache
def link_status(request):
    """Asked by the second factor's page while a wallet signs, behind the admin's own checks: pending, signed (the page
    then shows the key to confirm) or expired. It changes nothing."""
    if not on():
        raise Http404
    k1 = held(request, WALLET_LINK_SESSION, LINK)
    row = signed_row(k1)
    return state("expired" if not k1 or (row is not None and row.used) else "signed" if row else "pending")


def back_to_page():
    return HttpResponseRedirect(reverse("admin_second_factor"))


def action(request, name):
    """A POST of the wallets' part of the page: confirm or cancel the wallet that signed, rename or remove one of the
    account's own (any other id is not found). Nothing when off, from a session opened with a code only to add a
    passkey, or from one opened with a wallet: the wallets are managed from a session of the password."""
    from shops.models import AdminWallet, AdminWalletChallenge

    if not on() or second_factor.bootstrap_session(request):
        return back_to_page()
    if opened_with_wallet(request):
        messages.error(request, _("Sign in with the password to manage the wallets."))
        return back_to_page()
    user = request.user
    if name in ("wallet-link", "wallet-cancel"):
        signed = signed_link(request)
        request.session.pop(WALLET_LINK_SESSION, None)
        # Used once, whichever button and however many times it is pressed
        taken = signed is not None and AdminWalletChallenge.objects.filter(pk=signed.pk, used=None).update(
            used=timezone.now())
        if name == "wallet-link":
            if taken:
                link(request, signed.linking_key, second_factor.clean_name(request.POST.get("name")))
            else:
                messages.error(request, _("The wallet's signature is no longer valid: scan the new code."))
        return back_to_page()
    raw = str(request.POST.get("wallet", ""))
    wallet = None
    if raw.isascii() and raw.isdigit() and len(raw) < 19:
        wallet = AdminWallet.objects.filter(user=user, pk=int(raw)).first()
    if wallet is None:
        return back_to_page()
    if name == "wallet-rename":
        AdminWallet.objects.filter(pk=wallet.pk, user=user).update(name=second_factor.clean_name(request.POST.get("name")))
        return back_to_page()
    with transaction.atomic():
        if AdminWallet.objects.filter(pk=wallet.pk, user=user).delete()[0]:
            second_factor.record_change(user, "Wallet removed (sign-in to /admin/)", REMOVED, by=user, code=CHANGE,
                                        subject=SUBJECT)
    # A session opened with it ends at its next page (session_holds)
    messages.success(request, _("The wallet is removed: it no longer signs in to /admin/."))
    return back_to_page()


def link(request, key, name):
    """Links the wallet `key` to the signed-in account, or says why not."""
    from shops.models import AdminWallet

    user = request.user
    refused = _("This wallet cannot be linked here: it already has another use on this site. Use another wallet, or "
                "another account in your wallet.")
    if AdminWallet.objects.filter(user=user, linking_key=key).exists():
        messages.info(request, _("This wallet is already linked to your account."))
        return
    if known_elsewhere(key) or AdminWallet.objects.filter(linking_key=key).exists():
        messages.error(request, refused)
        return
    try:
        with transaction.atomic():
            # One link at a time for an account: the limit holds
            get_user_model()._default_manager.select_for_update().filter(pk=user.pk).first()
            if AdminWallet.objects.filter(user=user).count() >= MAX_WALLETS:
                messages.error(request, _("Your account has as many wallets as it may have: remove one first."))
                return
            AdminWallet.objects.create(user=user, linking_key=key, name=name)
            # Looked at again now that the row is written: a shop's account or a team key may have been made with this
            # key a moment ago
            if known_elsewhere(key):
                raise IntegrityError("the key has another role")
            second_factor.record_change(user, "Wallet linked (sign-in to /admin/)", LINKED, by=user, code=CHANGE,
                                        subject=SUBJECT)
    except IntegrityError:
        messages.error(request, refused)
        return
    messages.success(request, _("The wallet is linked to your account."))

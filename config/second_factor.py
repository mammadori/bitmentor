"""The second factor of /admin/ after the password: a code from an authenticator app (TOTP, config/totp.py), or a
passkey (WebAuthn, config/webauthn.py).

**Per account.** A staff account adds its own at `/admin/second-factor/`. The TOTP: the page shows a new secret, as a QR
code and as text, and only a correct code from the app confirms it (`AdminSecondFactor.confirmed`); until then it does
nothing, and the secret is never shown again. A passkey: the browser makes it on the phone, the computer or a security
key, and the site keeps only its public key (`AdminPasskey`); an account may have several, and names, renames and
removes them. No page shows or removes another account's second factor, a superuser's included. The account turns the
TOTP off with a current code; for a lost phone or key `manage.py admin_totp reset <username>` and `manage.py
admin_passkey reset <username>` remove them on the machine.

**`ADMIN_SECOND_FACTOR`.** `optional` (the default): nothing changes for an account until it adds a second factor, and
either a code or a passkey signs in. `required`: a staff account without one is sent, right after the password, to add
one, and reaches no other page of /admin/ until it has (`Gate`). `passkey`: the same, and only passkeys count: a TOTP
the account still has is neither asked for nor accepted (a code can be phished, a passkey signs only for this site). A
temporary password (config/temporary.py) is changed first, then the second factor.

**The sign-in.** After a right password for an account with a second factor the mode accepts, nobody is signed in:
whoever was signed in in that browser is signed out, the session gets a new key, and it only remembers for five minutes
which account passed the password (`PENDING`). The second factor's page (config/urls.py, `admin_code`) asks for a
passkey or a code; only a right one signs in, with a new session key again, and marks the session as born from the
admin's password (`ADMIN_SESSION`) and from that second factor (`FACTOR_SESSION`: its kind and key). Between the two
steps the browser is not signed in: no page of /admin/, no `next`, no `admin_view` lets it in. The pending mark names
the account by its key and is bound to its password and to its second factors: a new password, a factor added,
removed or reset ends it, and it cannot be pointed at another account. A passkey signs a challenge that lives in the
pending mark, five minutes and one try.

**A day.** A session opened with a second factor ends `ADMIN_SESSION_HOURS` (24) after it was opened, whatever the
activity: then password and second factor again. Sessions opened with the password alone are as they always were. An
account that has a second factor the mode accepts reaches /admin/ only from a session opened with one of them, and
only while that same one exists and is accepted: sessions opened before it was added (in another browser), or with the
previous release, end at their next page of /admin/, and so do the ones opened with a factor that was removed or reset
since, or that the mode no longer accepts. Removing one passkey ends the sessions opened with it, not the others.
A session opened with a linked wallet (config/admin_wallet.py, `ADMIN_WALLET_SIGN_IN`, `optional` only) is one of the
password alone, with a day too: it holds while that wallet does (`session_holds` asks config/admin_wallet.py).

**Codes.** A code is accepted within one 30-second step either side of the server's clock (keep the machine on NTP),
and never twice: the step of the last accepted code is stored, and no code of that step or an earlier one passes again.
**Passkeys.** The sign count of each is kept: when it does not grow, the sign-in is refused and the moderators are told
(a copied authenticator).

Adding, turning off, removing and the resets are written where a change of password is (`record_change`).
"""
import hashlib
import logging
import threading
import time
from datetime import timedelta

from django.conf import settings
from django.contrib import admin, messages
from django.contrib.admin.models import CHANGE, LogEntry
from django.contrib.auth import REDIRECT_FIELD_NAME, get_user_model
from django.contrib.auth import login as auth_login
from django.contrib.auth import logout as auth_logout
from django.contrib.auth.views import LoginView, redirect_to_login
from django.db import DatabaseError, IntegrityError, transaction
from django.http import HttpResponseForbidden, HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils import timezone, translation
from django.utils.crypto import constant_time_compare
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables

from config import ratelimit, totp, webauthn

from .guard import (
    ADMIN_SESSION,
    FACTOR_SESSION,
    PASSKEY_SETUP_SESSION,
    SETUP_SESSION,
    WALLET_SESSION,
    comes_from_admin,
)

# In the session between the password and the code: who passed the password, until when, and where to go after
PENDING = "admin_pending"
PENDING_SECONDS = 300
# In a session opened with a second factor (FACTOR_SESSION): [its kind, "totp" or "passkey", its key, the Unix time the
# session was opened]
TOTP, PASSKEY = "totp", "passkey"
# A session opened, in `passkey` mode, with the code of an account that has no passkey yet: it reaches only the pages
# where a passkey is added (`exempt`), and becomes a passkey's session once one is added
BOOTSTRAP = "totp-bootstrap"
# What a person may call a passkey
NAME_LENGTH = 60
logger = logging.getLogger(__name__)
# The limits of the codes typed on the set-up page, as those of the change of password (config/temporary.py)
WINDOW = 900
# The account's own budget of codes on the sign-in's code page, apart from the passwords': (name, limit, seconds). A
# guess has three chances in a million (a step either side is accepted too). The windows are fixed, not sliding: at
# most 20 guesses in a day of the counter (from midnight UTC), so up to 40 within 24 hours across a midnight
CODE_LIMITS = (("admin-code", 5, 900), ("admin-code-day", 20, 86400))
DAY = 86400
# A set-up shown again within this time keeps its secret: a second tab must not void the QR code just scanned
SETUP_REUSE = timedelta(minutes=10)
TEMPLATE = "admin/second_factor.html"
CODE_TEMPLATE = "admin/second_factor_code.html"


def now():
    """The server's clock, as a Unix time: the codes and the times of the sessions are read from it."""
    return time.time()


def mode():
    """ADMIN_SECOND_FACTOR: optional, required or passkey."""
    return settings.ADMIN_SECOND_FACTOR


def required():
    """True when every staff account must have a second factor (`required`, or `passkey`)."""
    return mode() in ("required", "passkey")


def totp_accepted():
    """False in `passkey` mode: a code from an app is then neither asked for nor accepted."""
    return mode() != "passkey"


def confirmed_factor(user):
    """The account's confirmed TOTP, or None, whatever the mode."""
    from shops.models import AdminSecondFactor

    if user is None or user.pk is None:
        return None
    return AdminSecondFactor.objects.filter(user_id=user.pk, confirmed__isnull=False).first()


def factor_of_request(request):
    """confirmed_factor of the request's account, read once per request."""
    if not hasattr(request, "_second_factor"):
        user = request.user
        request._second_factor = confirmed_factor(user) if user.is_authenticated else None
    return request._second_factor


def factor_key(factor):
    """[kind, key] of a TOTP or a passkey: how a session and the known browser's cookie name the factor."""
    from shops.models import AdminPasskey

    return [PASSKEY if isinstance(factor, AdminPasskey) else TOTP, factor.pk]


class Factors:
    """The second factors of an account that the mode accepts: its confirmed TOTP (None in `passkey` mode) and its
    passkeys. In `passkey` mode, the confirmed TOTP of an account that has no passkey yet is its `bootstrap`: asked
    after the password once, to open a session that reaches only the pages where a passkey is added (BOOTSTRAP), so
    that the password alone never adds a passkey to an account that had a second factor."""

    def __init__(self, totp_factor=None, passkeys=(), bootstrap=None):
        self.totp = totp_factor
        self.passkeys = list(passkeys)
        self.bootstrap = bootstrap

    def __bool__(self):
        return self.totp is not None or bool(self.passkeys) or self.bootstrap is not None

    @property
    def code_factor(self):
        """The TOTP whose code the sign-in asks for: the accepted one, or the bootstrap one; None if neither."""
        return self.totp or self.bootstrap

    def marks(self):
        """[[kind, key], ...]: what the pending mark, the sessions and the known browser's cookie compare."""
        found = [factor_key(factor) for factor in ([self.totp] if self.totp else []) + self.passkeys]
        return found + ([[BOOTSTRAP, self.bootstrap.pk]] if self.bootstrap else [])

    def missing(self):
        """True when the mode wants the account to add one: none at all with `required`, no passkey with `passkey`."""
        if mode() == "passkey":
            return not self.passkeys
        return required() and not self

    def without(self, factor):
        """These factors less one: what would be left after removing it."""
        return Factors(None if self.totp == factor else self.totp,
                       [passkey for passkey in self.passkeys if passkey != factor],
                       None if self.bootstrap == factor else self.bootstrap)


def accepted_factors(user):
    """The Factors of an account (none for an account not saved)."""
    from shops.models import AdminPasskey

    if user is None or user.pk is None:
        return Factors()
    passkeys = list(AdminPasskey.objects.filter(user_id=user.pk).order_by("created", "pk"))
    if totp_accepted():
        return Factors(confirmed_factor(user), passkeys)
    return Factors(None, passkeys, None if passkeys else confirmed_factor(user))


def factors_of_request(request):
    """accepted_factors of the request's account, read once per request."""
    if not hasattr(request, "_second_factors"):
        user = request.user
        request._second_factors = accepted_factors(user) if user.is_authenticated else Factors()
    return request._second_factors


@sensitive_variables()
def take_code(factor, code, confirm=False):
    """True if `code` is a code of the factor in the window around now, of a step later than the last accepted one; that
    step becomes the last one, atomically: of two requests with the same code only one passes. With `confirm`, for a
    set-up not confirmed yet, and it confirms it."""
    from shops.models import AdminSecondFactor

    step = totp.accepted_step(factor.secret, code, now(), factor.last_step)
    if step is None:
        return False
    rows = AdminSecondFactor.objects.filter(pk=factor.pk, user_id=factor.user_id, last_step__lt=step,
                                            confirmed__isnull=confirm)
    changes = {"last_step": step, **({"confirmed": timezone.now()} if confirm else {})}
    return bool(rows.update(**changes))


# --- The sign-in ---------------------------------------------------------------------------------------------------


class PasswordStep(LoginView):
    """The admin's login view, which signs in after the password only an account without a second factor the mode
    accepts."""

    def form_valid(self, form):
        user = form.get_user()
        factors = accepted_factors(user)
        if not factors:
            return super().form_valid(form)
        request = self.request
        target = self.get_success_url()
        # Nobody stays signed in in this browser, and the session gets a new key (flush): a key planted before the
        # password, or the session of another account, is not the one that waits for the code
        auth_logout(request)
        request.session[PENDING] = {"user": user.pk, "auth": user.get_session_auth_hash(), "factors": factors.marks(),
                                    "backend": getattr(user, "backend", ""), "until": now() + PENDING_SECONDS,
                                    "next": target}
        response = HttpResponseRedirect(reverse("admin_second_factor_code"))
        response.pending_user = user
        return response


def login(request, extra_context=None):
    """What `admin.site.login` does, with PasswordStep in place of LoginView (config/urls.py puts it in its place)."""
    from django.contrib.admin.forms import AdminAuthenticationForm

    site = admin.site
    if request.method == "GET" and site.has_permission(request):
        return HttpResponseRedirect(reverse("admin:index", current_app=site.name))
    context = {**site.each_context(request), "title": _("Log in"), "subtitle": None, "app_path": request.get_full_path(),
               "username": request.user.get_username()}
    if REDIRECT_FIELD_NAME not in request.GET and REDIRECT_FIELD_NAME not in request.POST:
        context[REDIRECT_FIELD_NAME] = reverse("admin:index", current_app=site.name)
    context.update(extra_context or {})
    request.current_app = site.name
    template = site.login_template or "admin/login.html"
    if not site.login_template:
        # With ADMIN_WALLET_SIGN_IN on and `optional`, a link to the wallet's sign-in; otherwise the very same page
        from config import admin_wallet

        link = admin_wallet.login_link(request, context)
        if link is not None:
            template = admin_wallet.LOGIN_TEMPLATE
            context.update(link)
    return PasswordStep.as_view(extra_context=context, authentication_form=site.login_form or AdminAuthenticationForm,
                                template_name=template)(request)


def pending(request):
    """(account, its Factors, where to go) of the account that passed the password in this session, if it still may
    give its second factor: within PENDING_SECONDS, still active and staff, with the same password and the same second
    factors the mode accepts (one added, removed or reset since ends it). Otherwise None."""
    data = request.session.get(PENDING)
    if not isinstance(data, dict):
        return None
    try:
        if not now() < float(data.get("until", 0)):
            return None
    except (TypeError, ValueError):
        return None
    User = get_user_model()
    user = User._default_manager.filter(pk=data.get("user")).first()
    if user is None or not (user.is_active and user.is_staff):
        return None
    if not constant_time_compare(str(data.get("auth", "")), user.get_session_auth_hash()):
        return None
    factors = accepted_factors(user)
    if not factors or factors.marks() != data.get("factors"):
        return None
    return user, factors, str(data.get("next") or "")


def pending_challenge(request):
    """A new challenge for a passkey, kept in the pending mark (so it ends with it), replacing the one before: one per
    page shown, valid CHALLENGE_SECONDS."""
    data = dict(request.session[PENDING])
    data["challenge"] = [webauthn.new_challenge(), now() + webauthn.CHALLENGE_SECONDS]
    request.session[PENDING] = data
    return data["challenge"][0]


def take_challenge(request, key):
    """The challenge kept under `key` (PENDING, or PASSKEY_SETUP_SESSION), if it is still valid; gone either way: one
    try each."""
    if key == PENDING:
        data = request.session.get(PENDING)
        if not isinstance(data, dict):
            return None
        data = dict(data)
        found = data.pop("challenge", None)
        request.session[PENDING] = data
    else:
        found = request.session.pop(key, None)
    try:
        value, until = found[0], found[1]
        if isinstance(value, str) and now() < float(until) and not spent_before(value):
            return value
    except (TypeError, ValueError, IndexError, KeyError):
        pass
    return None


def spent_before(challenge):
    """True if the challenge was already taken once. The session forgets a challenge when it is taken, but two requests
    of the same session at the same moment both read it before either saves: a row with the challenge's hash as its
    unique key (the request counters' table, expired rows are deleted there) lets only the first one through. When the
    database refuses, the challenge counts as spent: refused, never let through twice."""
    from shops.models import RequestCount

    key = hashlib.sha256(f"admin-passkey-challenge:{challenge}".encode()).hexdigest()
    try:
        with transaction.atomic():
            RequestCount.objects.create(key=key, expires=timezone.now() + timedelta(seconds=webauthn.CHALLENGE_SECONDS))
    except DatabaseError:
        return True
    return False


def open_session(request, user, factor, target, bootstrap=False):
    """The code or the passkey was right: the account is signed in (with a new session key, `login` cycles it), and
    the session is the admin's (ADMIN_SESSION) and that second factor's (FACTOR_SESSION; with `bootstrap`, the code of
    an account without a passkey in `passkey` mode, BOOTSTRAP). Returns the redirect to where the sign-in wanted to go,
    if it is still a safe address of this site."""
    from django.utils.http import url_has_allowed_host_and_scheme

    backend = request.session.get(PENDING, {}).get("backend") or None
    request.session.pop(PENDING, None)
    auth_login(request, user, backend=backend)
    request.session[ADMIN_SESSION] = True
    request.session[FACTOR_SESSION] = [BOOTSTRAP, factor.pk, now()] if bootstrap else [*factor_key(factor), now()]
    if bootstrap:
        # Only the page where the passkey is added: wherever the sign-in wanted to go
        return HttpResponseRedirect(reverse("admin_second_factor"))
    if not url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        target = reverse("admin:index", current_app=admin.site.name)
    return HttpResponseRedirect(target)


def end_pending(request):
    request.session.pop(PENDING, None)


# --- The sessions --------------------------------------------------------------------------------------------------


def session_holds(request):
    """For a session of the admin (ADMIN_SESSION): False when it must end. A session opened with a second factor ends
    ADMIN_SESSION_HOURS after it was opened, and when that factor is gone or no longer accepted by the mode; a session
    opened with the password alone ends when the account has a second factor the mode accepts. A BOOTSTRAP session
    holds only on the pages where a passkey is added (`exempt`). Else True."""
    marker = request.session.get(FACTOR_SESSION)
    if marker is None:
        if WALLET_SESSION in request.session:
            # Opened with a wallet (config/admin_wallet.py): while that wallet holds
            from config import admin_wallet

            return admin_wallet.session_holds(request)
        return not factors_of_request(request)
    if not marker_holds(request):
        return False
    return marker[0] != BOOTSTRAP or exempt(request)


def marker_holds(request):
    """True when the factor this session was opened with still exists, the mode accepts it (as a bootstrap, for a
    BOOTSTRAP session), and the session is within its hours; whatever the page."""
    try:
        kind, key, since = request.session.get(FACTOR_SESSION)
        since = float(since)
    except (TypeError, ValueError):
        return False
    if [kind, key] not in factors_of_request(request).marks():
        return False
    return 0 <= now() - since < settings.ADMIN_SESSION_HOURS * 3600


def bootstrap_session(request):
    """True for a session opened with the code to add a first passkey, that still holds somewhere."""
    marker = request.session.get(FACTOR_SESSION)
    return isinstance(marker, list) and marker[:1] == [BOOTSTRAP] and marker_holds(request)


def exempt(request):
    """The pages of /admin/ a staff account without a second factor reaches when the mode requires one: the set-up,
    the sign-out, and the change of a temporary password, which comes first."""
    from config import temporary

    path = request.path_info
    if path in (reverse("admin_second_factor"), reverse("admin_wallet_link_status"), reverse("admin:logout")):
        return True
    return temporary.exempt(path) and temporary.is_temporary(request.user)


class Gate:
    """After the accounts, StaffApart and TemporaryPassword. On a page of /admin/, for a session of the admin: one the
    second factor no longer holds (`session_holds`) is signed out and sent to sign in again, from the change of password
    too; when the mode requires one, a staff account without a second factor the mode accepts is sent to add one (a
    temporary password is changed first: TemporaryPassword sends there, and the change's pages are let through)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.check(request) or self.get_response(request)

    @staticmethod
    def check(request):
        # Without the session's cookie nobody is signed in: nothing is looked up (as in StaffApart)
        if settings.SESSION_COOKIE_NAME not in request.COOKIES or not comes_from_admin(request):
            return None
        # The sign-in's own pages make a new session whatever this one was, and the sign-out ends it
        if request.path_info in (reverse("admin:login"), reverse("admin_second_factor_code"), reverse("admin:logout"),
                                 reverse("admin_wallet_login"), reverse("admin_wallet_status")):
            return None
        user = request.user
        if not (user.is_authenticated and request.session.get(ADMIN_SESSION)):
            return None
        if not session_holds(request):
            if bootstrap_session(request):
                # Opened with the code only to add a passkey: that page, and nothing else
                return HttpResponseRedirect(reverse("admin_second_factor"))
            auth_logout(request)
            return redirect_to_login(request.get_full_path(), reverse("admin:login"))
        if WALLET_SESSION in request.session and request.path_info.endswith("/password/"):
            # The admin's page that sets an account's password asks for no old one: a session opened with a wallet
            # would leave with a password, and from there with a factor and the wallets (config/admin_wallet.py)
            return HttpResponseForbidden(
                _("This session was opened with a wallet: sign in with the password to set a password."))
        if user.is_active and user.is_staff and factors_of_request(request).missing() and not exempt(request):
            return HttpResponseRedirect(reverse("admin_second_factor"))
        return None


# --- The register --------------------------------------------------------------------------------------------------


def in_background(work):
    """Runs `work` in a thread of its own: the page or the command waits neither for Telegram nor for the mail server.
    The thread's own database connections (the mail budget) are closed when it ends: nobody else would close them."""
    def run():
        from django.db import connections

        try:
            work()
        finally:
            connections.close_all()

    threading.Thread(target=run, daemon=True).start()


def tell(text, wait=False, report=None, subject=None):
    """Tells the people who look after the site (shops.notifications.deliver_to_moderators), in the site's language,
    with the site's name. In a thread, unless `wait` (a command, which would end before the thread); then `report`, if
    given, gets (where, problems) as deliver_to_moderators says them. A message that cannot go is a line in the log, never
    an error for whoever caused it."""
    def deliver():
        where, problems = None, []
        from shops.management.commands.dashboard import site_label
        from shops.notifications import deliver_to_moderators

        try:
            with translation.override(settings.LANGUAGE_CODE):
                where, problems = deliver_to_moderators(str(subject or _("Second factor")), f"{site_label()}\n{text()}")
            if where is None:
                logger.warning("second factor: the moderators were not told (%s)", "; ".join(problems))
        except Exception as error:
            logger.exception("second factor: telling the moderators failed")
            problems = [*problems, f"it failed with {type(error).__name__}"]
        if report is not None:
            report(where, problems)

    if wait:
        deliver()
    else:
        in_background(deliver)


def notice(sentence, user):
    """The text of a notice, made when it is sent: the account's name as an alert shows a name."""
    def text():
        from shops.notifications import alert_text

        return str(sentence).format(name=alert_text(user.get_username()))
    return text


def record_change(user, message, sentence, by=None, wait=False, report=None, code="second_factor", subject=None):
    """Turning the second factor on or off, or its reset, is written where the change of an account's password is: the
    register of the changes to the owners' accounts (shops/guard.py), which keeps and tells it only for an account that
    owns a shop, and, for every account, the admin's own history of the account (LogEntry), as the admin's page of a
    user writes a change of its password. `by` is the account that made it from a page of the admin; from a command,
    the history names the account itself, and the message says where it came from. And, for every staff account, the
    people who look after the site are told (`sentence`), after the commit: a turn-on nobody expected means that the
    password is known to someone else. `code` is the register's name of the change (shops/guard.py, WORDS), and
    `subject` the notice's: the wallets of config/admin_wallet.py have their own."""
    from shops import guard

    shops = guard.owned(user)
    guard.record(user, [code], shops, guard.owned_chats(shops))
    LogEntry.objects.log_actions((by or user).pk, [user], CHANGE, change_message=message, single_object=True)
    text = notice(sentence, user)
    transaction.on_commit(lambda: tell(text, wait=wait, report=report, subject=subject), robust=True)


# What the people who look after the site are told; {name} is the staff account
TURNED_ON = gettext_lazy("The staff account {name} turned its second factor on.")
TURNED_OFF = gettext_lazy("The staff account {name} turned its second factor off.")
RESET = gettext_lazy("The second factor of the staff account {name} was reset with manage.py admin_totp reset.")
WRONG_CODE = gettext_lazy("A wrong second-factor code after the right password for the staff account {name}: its "
                          "password may be known to someone else. Change it.")
PASSKEY_ADDED = gettext_lazy("The staff account {name} added a passkey.")
PASSKEY_REMOVED = gettext_lazy("The staff account {name} removed a passkey.")
PASSKEY_RESET = gettext_lazy("The passkeys of the staff account {name} were removed with manage.py admin_passkey reset.")
WRONG_PASSKEY = gettext_lazy("A refused passkey after the right password for the staff account {name}: its password may "
                             "be known to someone else. Change it.")
PASSKEY_COPIED = gettext_lazy("A passkey of the staff account {name} signed with a count that did not grow: it may have "
                              "been copied. The sign-in was refused. Remove that passkey from the account, and check "
                              "who else may have it.")


# --- The codes of the sign-in --------------------------------------------------------------------------------------


def over_code_limits(user):
    """Counts a code typed for the account against its own budget (CODE_LIMITS), window after window: a code the short
    window refuses is not checked, and does not use up the day's. Returns (the seconds of the window that refuses, or 0;
    the counters, to give back if the code is right)."""
    counted = []
    for name, limit, seconds in CODE_LIMITS:
        counted.append((f"{name}:{user.pk}", limit, seconds))
        if ratelimit.over_limit(f"{name}:{user.pk}", limit, seconds):
            return seconds, counted
    return 0, counted


def wrong_code(response, user):
    """A wrong code after the right password: the password is known, maybe to someone else. The first of a day for the
    account is written in its history in the admin and told to the people who look after the site, once the page has
    gone (`response`'s closers run after it was sent). Whatever goes wrong here is a line in the log: the page is the
    same as for any wrong code."""
    try:
        # One a day: a counter of its own, in the database like the limits, so that the workers agree
        if ratelimit.over_limit(f"admin-code-told:{user.pk}", 1, DAY):
            return
        LogEntry.objects.log_actions(user.pk, [user], CHANGE, single_object=True,
                                     change_message="A wrong second-factor code after the right password")
        text = notice(WRONG_CODE, user)
        response._resource_closers.append(lambda: tell(text))
    except Exception:
        logger.exception("second factor: a wrong code could not be written down")


# --- The page of the account's own second factor -------------------------------------------------------------------


def over_limits(request):
    """The codes typed on the set-up page, as the old passwords of the change of password: counted by address and
    network, given back when right."""
    counted = [(f"admin-factor:{ratelimit.client_ip(request)}", 10), (f"admin-factor-net:{ratelimit.client_network(request)}", 30)]
    # Both are counted, whatever the first says
    spent = [ratelimit.over_limit(name, limit, WINDOW) for name, limit in counted]
    return any(spent), counted


def give_back(counted):
    """Gives back the counters of a code that was right: (name, limit) of the set-up page's window, or (name, limit,
    seconds)."""
    for name, *rest in counted:
        ratelimit.give_back(name, rest[1] if len(rest) > 1 else WINDOW)



# --- Passkeys ------------------------------------------------------------------------------------------------------


def passkeys_here(request):
    """True when this page is at the site's own origin (PUBLIC_URL), the only one a passkey of this site answers: not
    on the onion, nor at another name of the machine."""
    from config.mailbudget import from_onion

    return f"{request.scheme}://{request.get_host()}" == webauthn.origin() and not from_onion(request)


def rp_name():
    from config.community import community

    return str(community().get("name") or settings.SITE_HOST)


def handle_of(user):
    """The account's opaque id for the authenticators, or None before its first passkey."""
    from shops.models import AdminPasskeyHandle

    return AdminPasskeyHandle.objects.filter(user_id=user.pk).values_list("handle", flat=True).first()


def clean_name(value):
    """What the person called a passkey, as one line of at most NAME_LENGTH characters ("" for none)."""
    text = "".join(" " if not character.isprintable() else character for character in str(value or ""))
    return " ".join(text.split())[:NAME_LENGTH]


def take_assertion(request, user, posted, outcome=None):
    """(the account's passkey that signed this sign-in's challenge, its sign count and BS flag stored, or None; True if
    it was refused as a copy). Whatever is wrong (no challenge or an old one, a credential of another account or
    unknown, a wrong signature, a BE flag other than at registration, any malformed part), the caller answers the
    same. A count that did not grow is also written in the account's history and told. `outcome`, a dict if given,
    gets "gone": True when the passkey answered no challenge this session still has (none, one past its time, or one a
    second tab replaced): a refusal that says nothing about who knows the password."""
    from shops.models import AdminPasskey

    outcome = {} if outcome is None else outcome
    challenge = take_challenge(request, PENDING)
    if challenge is None:
        outcome["gone"] = True
        return None, False
    key = None
    try:
        assertion = webauthn.parse_assertion(posted)
        key = AdminPasskey.objects.filter(user_id=user.pk, credential_id=assertion.credential_id).first()
        if key is None:
            raise webauthn.Refused("not a passkey of this account")
        count, backed_up = webauthn.verify_assertion(assertion, challenge, key.public_key, key.algorithm,
                                                     key.sign_count, handle_of(user), key.backup_eligible)
    except webauthn.Replayed:
        copied_passkey(user)
        return None, True
    except webauthn.Refused as error:
        logger.info("passkey refused for account %s: %s", user.pk, error)
        if str(error) == "another challenge":
            outcome["gone"] = True
        return None, False
    # Atomically: of two sign-ins with the same count only one passes
    rows = AdminPasskey.objects.filter(pk=key.pk, user_id=user.pk, sign_count=key.sign_count)
    if not rows.update(sign_count=count, backed_up=backed_up, last_used=timezone.now()):
        return None, False
    return key, False


def wrong_passkey(response, user):
    """A refused passkey after the right password: the password is known, maybe to someone else. As a wrong code
    (`wrong_code`): the first of a day for the account, with a counter of its own, is written in its history and told
    once the page has gone; the page is the same as for any refused passkey."""
    try:
        if ratelimit.over_limit(f"admin-passkey-wrong-told:{user.pk}", 1, DAY):
            return
        LogEntry.objects.log_actions(user.pk, [user], CHANGE, single_object=True,
                                     change_message="A refused passkey after the right password")
        text = notice(WRONG_PASSKEY, user)
        response._resource_closers.append(lambda: tell(text))
    except Exception:
        logger.exception("second factor: a refused passkey could not be written down")


def copied_passkey(user):
    """A right signature with a count that did not grow: written in the account's history and told, once a day per
    account. The page is the same as for any refused passkey."""
    try:
        if ratelimit.over_limit(f"admin-passkey-told:{user.pk}", 1, DAY):
            return
        LogEntry.objects.log_actions(user.pk, [user], CHANGE, single_object=True,
                                     change_message="A passkey signed with a count that did not grow: refused")
        tell(notice(PASSKEY_COPIED, user))
    except Exception:
        logger.exception("second factor: a copied passkey could not be written down")


def last_factor(user, factor):
    """True when removing `factor` would leave the account without the second factor the mode requires."""
    factors = accepted_factors(user)
    return factor_key(factor) in factors.marks() and factors.without(factor).missing()


def remove_factor(user, factor, message, sentence):
    """Removes `factor` unless it is the last one the mode requires, and writes it down; False if it was not removed.
    The account's row is locked first and the check made again inside: of two removals at the same moment, of two
    factors, the second sees the first and is refused."""
    with transaction.atomic():
        get_user_model()._default_manager.select_for_update().filter(pk=user.pk).first()
        if last_factor(user, factor):
            return False
        type(factor).objects.filter(pk=factor.pk, user_id=user.pk).delete()
        record_change(user, message, sentence, by=user)
    return True


# --- The page of the account's own second factor -------------------------------------------------------------------


def passkey_context(request):
    """The account's passkeys, and, where passkeys work, the options of a new one with a fresh challenge of this
    session (PASSKEY_SETUP_SESSION: one try, CHALLENGE_SECONDS)."""
    from shops.models import AdminPasskey

    user = request.user
    passkeys = list(AdminPasskey.objects.filter(user=user).order_by("created", "pk"))
    context = {"passkeys": passkeys, "passkeys_here": passkeys_here(request), "public_url": settings.PUBLIC_URL,
               "passkey_mode": mode() == "passkey", "totp_accepted": totp_accepted()}
    if context["passkeys_here"]:
        challenge = webauthn.new_challenge()
        handle = handle_of(user) or webauthn.new_user_handle()
        request.session[PASSKEY_SETUP_SESSION] = [challenge, now() + webauthn.CHALLENGE_SECONDS, handle]
        context["passkey_options"] = webauthn.creation_options(
            challenge, handle, user.get_username(), rp_name(), [passkey.credential_id for passkey in passkeys])
    return context


def wallet_context(request):
    from config import admin_wallet

    return admin_wallet.page_context(request)


@sensitive_variables()
def page(request, context, status=200):
    site = admin.site
    response = TemplateResponse(request, TEMPLATE, {**site.each_context(request), "title": _("Second factor"),
                                                    "required": factors_of_request(request).missing(),
                                                    "bootstrap_session": bootstrap_session(request),
                                                    **passkey_context(request), **wallet_context(request), **context},
                                status=status)
    if status == 429:
        response["Retry-After"] = str(ratelimit.seconds_left(WINDOW))
    return response


def shown_setup(request):
    """The waiting set-up of the signed-in account that this session was shown, as a queryset (empty if none)."""
    from shops.models import AdminSecondFactor

    return AdminSecondFactor.objects.filter(pk=request.session.get(SETUP_SESSION), user=request.user,
                                            confirmed__isnull=True)


@sensitive_variables()
def setup_context(request, factor):
    host = settings.SITE_HOST
    return {"setting_up": True, "secret": totp.grouped(factor.secret),
            "uri": totp.uri(factor.secret, request.user.get_username(), host)}


def totp_context(request):
    """The TOTP part of the page, for a GET: the date of a confirmed one; or, where the mode accepts codes, a set-up."""
    from shops.models import AdminSecondFactor

    factor = confirmed_factor(request.user)
    if factor is not None:
        return {"on_since": factor.confirmed}
    if not totp_accepted():
        return {}
    recent = shown_setup(request).filter(created__gte=timezone.now() - SETUP_REUSE).first()
    if recent is not None:
        return setup_context(request, recent)
    with transaction.atomic():
        AdminSecondFactor.objects.filter(user=request.user, confirmed__isnull=True).delete()
        fresh = AdminSecondFactor.objects.create(user=request.user, secret=totp.new_secret())
    request.session[SETUP_SESSION] = fresh.pk
    return setup_context(request, fresh)


def opened_with(request, kind):
    marker = request.session.get(FACTOR_SESSION)
    return isinstance(marker, list) and len(marker) == 3 and marker[0] == kind


def back_to_page():
    return HttpResponseRedirect(reverse("admin_second_factor"))


def add_passkey(request):
    """A new passkey of the signed-in account, from the JSON the page posted after navigator.credentials.create, for
    the challenge this session was shown. A session opened with the password alone becomes one opened with it."""
    from shops.models import AdminPasskey, AdminPasskeyHandle

    user = request.user
    shown = request.session.pop(PASSKEY_SETUP_SESSION, None)
    try:
        challenge, until, handle = shown
        if not (isinstance(challenge, str) and isinstance(handle, str) and now() < float(until)):
            raise ValueError
    except (TypeError, ValueError):
        return page(request, {"passkey_wrong": True})
    if spent_before(challenge):
        return page(request, {"passkey_wrong": True})
    if not passkeys_here(request):
        return page(request, {"passkey_wrong": True})
    try:
        found = webauthn.verify_registration(request.POST.get("credential", ""), challenge)
    except webauthn.Refused as error:
        logger.info("passkey not added for account %s: %s", user.pk, error)
        return page(request, {"passkey_wrong": True})
    try:
        with transaction.atomic():
            mine, _made = AdminPasskeyHandle.objects.get_or_create(user=user, defaults={"handle": handle})
            # The authenticator was given this handle: one made meanwhile in another session is another account's id
            # to it, and the passkey would not be this account's
            if mine.handle != handle or AdminPasskey.objects.filter(credential_id=found.credential_id).exists():
                raise IntegrityError("another handle, or a credential already registered")
            passkey = AdminPasskey.objects.create(
                user=user, credential_id=found.credential_id, public_key=found.public_key, algorithm=found.algorithm,
                sign_count=found.sign_count, backup_eligible=found.backup_eligible, backed_up=found.backed_up,
                name=clean_name(request.POST.get("name")))
            record_change(user, "Passkey added", PASSKEY_ADDED, by=user)
    except IntegrityError:
        return page(request, {"passkey_wrong": True})
    # A session opened with the password alone, or with the code only to add a first passkey, proved this passkey: it
    # becomes one opened with it, with a new key and its day from now. One opened with another factor stays as it is,
    # with its own day
    if request.session.get(FACTOR_SESSION) is None or opened_with(request, BOOTSTRAP):
        request.session.cycle_key()
        request.session[FACTOR_SESSION] = [PASSKEY, passkey.pk, now()]
    messages.success(request, _("The passkey is added: from the next sign-in, the password and then the passkey."))
    return back_to_page()


def passkey_action(request, action):
    """Rename or remove one of the signed-in account's own passkeys (any other key is not found)."""
    from shops.models import AdminPasskey

    user = request.user
    if action == "passkey-add":
        return add_passkey(request)
    raw = str(request.POST.get("passkey", ""))
    passkey = None
    if raw.isascii() and raw.isdigit() and len(raw) < 19:
        passkey = AdminPasskey.objects.filter(user=user, pk=int(raw)).first()
    if passkey is None:
        return back_to_page()
    if action == "passkey-rename":
        AdminPasskey.objects.filter(pk=passkey.pk, user=user).update(name=clean_name(request.POST.get("name")))
        return back_to_page()
    if not remove_factor(user, passkey, "Passkey removed", PASSKEY_REMOVED):
        return page(request, {"last_factor": True})
    if opened_with(request, PASSKEY) and request.session[FACTOR_SESSION][1] == passkey.pk:
        # The session opened with it ends: whoever goes on signs in again
        request.session.pop(FACTOR_SESSION, None)
    messages.success(request, _("The passkey is removed."))
    return back_to_page()


@sensitive_post_parameters("code")
@sensitive_variables()
def setup_view(request):
    """The account's own second factor, behind `admin.site.admin_view` (the admin's session, staff).

    Passkeys: the list, add (`passkey-add`), rename and remove; never another account's. The TOTP, where the mode
    accepts codes: without a confirmed one, a set-up, confirmed by a right code. The waiting secret belongs to the
    session that was shown it (SETUP_SESSION): that session sees it again within SETUP_REUSE (a second tab); any other
    GET, from another browser or a session that signed in again, deletes it and makes a new one, so that two sessions
    never share a secret, and only the session that was shown a secret can confirm it. With a confirmed one: its date,
    and turning it off with a right code. The last factor the mode requires is not removed."""
    request.current_app = admin.site.name
    user = request.user
    if request.method != "POST":
        return page(request, totp_context(request))
    from config import admin_wallet

    action = request.POST.get("action")
    if action in ("passkey-add", "on") and admin_wallet.opened_with_wallet(request):
        # A session opened with a wallet adds no factor: it would become that factor's session, manage the wallets
        # and leave the owner, who has the password but not that factor, outside
        messages.error(request, _("Sign in with the password to add a second factor."))
        return back_to_page()
    if action in ("passkey-add", "passkey-rename", "passkey-remove"):
        return passkey_action(request, action)
    if action in admin_wallet.ACTIONS:
        return admin_wallet.action(request, action)
    factor = confirmed_factor(user)
    if factor is not None:
        if opened_with(request, BOOTSTRAP):
            # This session was opened with this code only to add a passkey: never to take the code away
            return page(request, {"on_since": factor.confirmed, "bootstrap_off": True})
        if last_factor(user, factor):
            return page(request, {"on_since": factor.confirmed, "last_factor": True})
        spent, counted = over_limits(request)
        if spent:
            return page(request, {"on_since": factor.confirmed, "wait": True}, status=429)
        if action != "off" or not take_code(factor, request.POST.get("code")):
            return page(request, {"on_since": factor.confirmed, "wrong": True})
        give_back(counted)
        if not remove_factor(user, factor, "Second factor turned off", TURNED_OFF):
            return page(request, {"on_since": factor.confirmed, "last_factor": True})
        if opened_with(request, TOTP):
            # This session goes on as one opened with the password alone, which it is again: while the account has
            # passkeys, that ends it at the next page
            request.session.pop(FACTOR_SESSION, None)
        if accepted_factors(user):
            messages.success(request, _("The code from the app is off."))
        else:
            messages.success(request, _("The second factor is off: the sign-in asks only for the password."))
        return back_to_page()
    if not totp_accepted():
        return back_to_page()
    waiting = shown_setup(request).first()
    if waiting is None:
        # A set-up this session was not shown, or that a reset or another session took away: start again
        return back_to_page()
    spent, counted = over_limits(request)
    if spent:
        return page(request, {**setup_context(request, waiting), "wait": True}, status=429)
    if action != "on" or not take_code(waiting, request.POST.get("code"), confirm=True):
        return page(request, {**setup_context(request, waiting), "wrong": True})
    give_back(counted)
    record_change(user, "Second factor turned on", TURNED_ON, by=user)
    request.session.pop(SETUP_SESSION, None)
    # A session opened with the password alone proved the code: it becomes one opened with the second factor, with a
    # new key and its day from now. One opened with a passkey stays as it is
    if request.session.get(FACTOR_SESSION) is None:
        request.session.cycle_key()
        request.session[FACTOR_SESSION] = [TOTP, waiting.pk, now()]
    messages.success(request, _("The second factor is on: from the next sign-in, the password and then a code from the app."))
    return back_to_page()

"""Whoever runs the site cannot pass for a merchant.

An account of the staff (`is_staff` or `is_superuser`) and a session born from the admin's password (`ADMIN_SESSION`)
are for /admin/ and nothing else. Everywhere else on the site the request is that of a visitor who is not signed in, so
that no page and no action of a merchant (the panel, the edit form, the recovery email, the guide of the wallet, the
registers of the employees, the sign) can be used from the administration, whoever
the account belongs to: the staff gives a merchant `is_staff` and a password, signs in at /admin/login/, and the shop's
pages answer like to a stranger.

This is the only place where it is decided. A view that reads `request.user` gets it for free, a new one too: nothing
has to be remembered. `shops/test_guard.py` walks all the URLs of the site to keep it so.

What it does not stop: an account that is not staff and was changed, such as a username replaced with a key the staff
controls, which then signs in as any merchant would. That one is seen, not stopped: see `shops/guard.py`.
"""
import contextvars

from django.conf import settings
from django.contrib.auth import logout
from django.contrib.auth.models import AnonymousUser
from django.contrib.auth.signals import user_logged_in
from django.dispatch import receiver

# The admin opens only with a session born from the password here (config/urls.py), or from a staff account's own
# linked wallet (config/admin_wallet.py): a session opened by the link in an email or by the shops' wallet sign-in is not
# enough, not even if the account becomes staff afterwards.
ADMIN_SESSION = "admin_password"
# What config/second_factor.py keeps in a session of the admin: the second factor it was opened with ([kind, key, since]),
# the TOTP set-up this session was shown, and the challenge of a passkey being added. They go with ADMIN_SESSION
FACTOR_SESSION = "admin_second_factor"
SETUP_SESSION = "admin_second_factor_setup"
PASSKEY_SETUP_SESSION = "admin_passkey_setup"
# What config/admin_wallet.py keeps: in a session opened with a wallet, [the AdminWallet's key, the Unix time it was
# opened]; and the challenge of a wallet being linked. They go with ADMIN_SESSION too
WALLET_SESSION = "admin_wallet"
WALLET_LINK_SESSION = "admin_wallet_link"
ADMIN_PATH = "/admin/"

# The request being served, for whoever has to know where a change to the database comes from (shops/guard.py):
# a page of the admin, a page of the site, or no request at all (a command, a shell)
current_request = contextvars.ContextVar("current_request", default=None)


def comes_from_admin(request):
    return request.path_info.startswith(ADMIN_PATH)


def staff_account(request):
    """The account of this request, if it is of the staff or if the session was born from the admin's password: even
    when the account is no longer staff (`is_staff` taken away after the sign-in), the session stays on the staff side.
    Whatever the second factor says: this is the account StaffApart keeps away from the merchants' pages."""
    # Without the session's cookie nobody is signed in: the user is not looked for (a visitor's page does not start
    # to depend on the cookie, nor to ask the database)
    if settings.SESSION_COOKIE_NAME not in request.COOKIES:
        return None
    user = request.user
    if not user.is_authenticated:
        return None
    if request.session.get(ADMIN_SESSION) or user.is_staff or user.is_superuser:
        return user
    return None


def staff_side(request):
    """staff_account, if the session is still a staff session: for an account with a confirmed second factor, a session
    opened with it and not past its hours (config/second_factor.py, `session_holds`). For an account without one, the same
    answer as staff_account. Read before StaffApart hides the account."""
    from config.second_factor import session_holds

    user = staff_account(request)
    if user is None or not session_holds(request):
        return None
    return user


def bootstrap_session(request):
    """True for a session opened, in `passkey` mode, with the code only to add a first passkey, whose mark still holds
    (config/second_factor.py, BOOTSTRAP): it holds only on the pages where the passkey is added, so StaffApart's
    question is False everywhere else; outside /admin/ it is a visitor, and is not signed out (the browser asks for
    /favicon.ico by itself)."""
    from config.second_factor import bootstrap_session as held

    return held(request)


@receiver(user_logged_in)
def a_new_sign_in_ends_the_admin_session(sender, request, user, **kwargs):
    """Whoever signs in again, with the wallet or the email link, is no longer in the session the admin's password made:
    `login()` keeps the data of a session when the user is the same, and the flag would stay (and keep the owner of a shop
    that lost is_staff out of their own shop). The admin's own sign-in sets the flag after this, in config/urls.py. The
    second factor's marks go with it: the one of the factor the session was opened with (set again by the code's page
    after this), the set-up this session was shown and the challenge of a passkey being added, which a session that
    signed in again does not get back. And the wallet's: the one the session was opened with (set again by the wallet's
    sign-in after this) and the challenge of a wallet being linked."""
    if request is not None and hasattr(request, "session"):
        for key in (ADMIN_SESSION, FACTOR_SESSION, SETUP_SESSION, PASSKEY_SETUP_SESSION, WALLET_SESSION,
                    WALLET_LINK_SESSION):
            request.session.pop(key, None)


class StaffApart:
    """After the accounts (AuthenticationMiddleware), before anything that looks at the user. Outside /admin/ the staff's
    request becomes that of a visitor not signed in; the account it had stays in `request.staff_user`, for the one page
    that explains why and for what the staff may do as a visitor (the pages marked `visibility: staff`, the e-mail
    limits). A session of a staff account that the second factor no longer holds (opened with the password alone before
    the account turned it on, or past its hours) is signed out here, as /admin/ does (config/second_factor.py, Gate): it
    is staff nowhere, and no merchant either. A session opened with the code only to add a
    first passkey (`bootstrap_session`) is a visitor here too, staff nowhere, and stays signed in for its page."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.staff_user = None
        if not comes_from_admin(request) and staff_account(request) is not None:
            user = staff_side(request)
            if user is None and not bootstrap_session(request):
                logout(request)
            else:
                request.staff_user = user
            visitor = AnonymousUser()
            request.user = request._cached_user = visitor

            async def auser():
                return visitor

            request.auser = auser
        token = current_request.set(request)
        try:
            return self.get_response(request)
        finally:
            current_request.reset(token)

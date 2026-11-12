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
from django.contrib.auth.models import AnonymousUser
from django.contrib.auth.signals import user_logged_in
from django.dispatch import receiver

# The admin opens only with a session born from the password here (config/urls.py): a session opened by the link in an
# email or by a wallet's signature is not enough, not even if the account becomes staff afterwards.
ADMIN_SESSION = "admin_password"
ADMIN_PATH = "/admin/"

# The request being served, for whoever has to know where a change to the database comes from (shops/guard.py):
# a page of the admin, a page of the site, or no request at all (a command, a shell)
current_request = contextvars.ContextVar("current_request", default=None)


def comes_from_admin(request):
    return request.path_info.startswith(ADMIN_PATH)


def staff_side(request):
    """The account of this request, if it is of the staff or if the session was born from the admin's password: even
    when the account is no longer staff (`is_staff` taken away after the sign-in), the session stays on the staff side."""
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


@receiver(user_logged_in)
def a_new_sign_in_ends_the_admin_session(sender, request, user, **kwargs):
    """Whoever signs in again, with the wallet or the email link, is no longer in the session the admin's password made:
    `login()` keeps the data of a session when the user is the same, and the flag would stay (and keep the owner of a shop
    that lost is_staff out of their own shop). The admin's own sign-in sets the flag after this, in config/urls.py."""
    if request is not None and hasattr(request, "session"):
        request.session.pop(ADMIN_SESSION, None)


class StaffApart:
    """After the accounts (AuthenticationMiddleware), before anything that looks at the user. Outside /admin/ the staff's
    request becomes that of a visitor not signed in; the account it had stays in `request.staff_user`, for the one page
    that explains why."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.staff_user = None
        if not comes_from_admin(request):
            user = staff_side(request)
            if user is not None:
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

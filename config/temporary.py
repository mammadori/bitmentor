"""A temporary password is changed at the first sign-in.

The association creates the accounts of the staff and hands the password over in person (docs/operations.md). Django does
not ask the person who gets it to change it. Here it does, without anything to remember.

**The mark.** `manage.py temporary_password <username>` writes in `Profile.temporary_password` the hash that
`user.password` has at that moment. While `user.password` is that same string, the password is temporary. A new password
is a new hash (Django salts every one), so the moment the person changes it, or anyone sets another one, the mark stops
holding on its own: no signal watches it and no step has to clear it. (The form below and the sign-in also tidy the old
hash away, but nothing depends on them.) Setting the same password again is a new hash too: the mark does not come back
by itself, the command is run again.

**The middleware.** `TemporaryPassword` sends whoever signed in to the admin with a temporary password to the admin's
change of password, from every page of `/admin/`. Only the change itself, its «done» page and the sign-out stay open.
Outside `/admin/` it does nothing: there the staff is a visitor already (config/guard.py), and a staff member who opens
the site from a network `ADMIN_ALLOWED_IPS` does not let into the admin would be sent to a page that answers 403. It is for
the staff account that the admin would let in (active, staff, signed in with the admin's password): an account that is
not staff has no change of password to be sent to, and a merchant must never be locked out of the shop by a mark that has
nothing to do with them.

**The change.** `password_change` is the admin's own, with a form that asks for the old password, runs the validators of
`AUTH_PASSWORD_VALIDATORS` on the new one and does not take the old one again: the same password put back would be a new
hash, and the mark would stop holding without the password having changed. The form is a way to guess the old password
for whoever holds a session, so it has the limits of the sign-in: only a wrong old password counts, and when the limit is
reached the page says so and the answer carries `Retry-After`. The page also offers to log out, for whoever cannot change
it now: the next sign-in asks again.

Nothing here reads, shows or logs a password or a hash.
"""
import unicodedata

from django.conf import settings
from django.contrib import admin, messages
from django.contrib.admin.forms import AdminPasswordChangeForm
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import check_password, identify_hasher
from django.contrib.auth.views import PasswordChangeView
from django.core.exceptions import ValidationError
from django.http import HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.debug import sensitive_variables

from config import ratelimit
from shops.models import Profile

from .guard import ADMIN_SESSION, comes_from_admin

TEMPLATE = "admin/temporary_password_change.html"
# The limits of the admin's sign-in (config/urls.py), for the wrong old passwords of the change of password
WINDOW = 900


def marked_hash(user):
    """The hash written when the password was marked, or an empty string: none, or no Profile."""
    return Profile.objects.filter(user_id=user.pk).values_list("temporary_password", flat=True).first() or ""


def is_temporary(user):
    """True while the account's password is the one that was marked: the same hash, not only the same password."""
    if not getattr(user, "is_authenticated", False) or not user.has_usable_password():
        return False
    marked = marked_hash(user)
    return bool(marked) and marked == user.password


def has_password(user):
    """True if the account has a password somebody could sign in with: not blank, not set unusable, in a format Django knows."""
    if not user.password or not user.has_usable_password():
        return False
    try:
        identify_hasher(user.password)
    except ValueError:
        return False
    return True


def mark(user):
    """Marks the password the account has now (a Profile is made if it has none). The caller checked has_password()."""
    Profile.objects.update_or_create(user=user, defaults={"temporary_password": user.password})


def clear(user):
    Profile.objects.filter(user_id=user.pk).update(temporary_password="")


def before_sign_in(username):
    """Called by the admin's sign-in before it checks the password: (pk, hash) of the account it is about if its password is
    temporary right now, or None. The sign-in itself can store the same password with a newer hash; only then does the
    mark follow (after_sign_in), and to know it was this sign-in that did it, the state before it has to be read first."""
    User = get_user_model()
    # As the sign-in form reads the name (UsernameField), and the backend looks it up
    name = unicodedata.normalize("NFKC", username.strip())
    try:
        user = User._default_manager.get_by_natural_key(name)
    except User.DoesNotExist:
        return None
    return (user.pk, user.password) if is_temporary(user) else None


@sensitive_variables("password")
def after_sign_in(user, password, before):
    """Called by the admin's sign-in once the password was right. Django re-hashes a password at the sign-in when its hasher
    or its rounds changed since it was set: the hash differs and the password does not, and the mark would be lost on the
    very sign-in it is for. So, if the password was temporary just before this sign-in (`before`), and the password typed
    also fits the marked hash, the mark follows the new hash. In any other case an old hash is dropped, and not moved: a
    password set again by someone else is not marked again by the sign-in that follows."""
    marked = marked_hash(user)
    if not marked or marked == user.password:
        return
    if before == (user.pk, marked) and check_password(password, marked):
        Profile.objects.filter(user_id=user.pk).update(temporary_password=user.password)
    else:
        clear(user)


def exempt(path):
    return path in (reverse("admin:password_change"), reverse("admin:password_change_done"), reverse("admin:logout"))


class TemporaryPassword:
    """After the accounts and StaffApart: on a page of /admin/ (the only place where the staff is the staff), a staff
    member signed in with a temporary password goes to the change of password, whatever they asked for. A plain redirect:
    no message, no address of the page they wanted, nothing about the password in it."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if self.must_change(request):
            return HttpResponseRedirect(reverse("admin:password_change"))
        return self.get_response(request)

    @staticmethod
    def must_change(request):
        # Without the session's cookie nobody is signed in: nothing is looked up (as in StaffApart)
        if settings.SESSION_COOKIE_NAME not in request.COOKIES or not comes_from_admin(request) or exempt(request.path_info):
            return False
        user = request.user
        # Who the admin lets in: if it would not let them in, there is no change of password for them to be sent to
        if not (user.is_authenticated and user.is_active and user.is_staff and request.session.get(ADMIN_SESSION)):
            return False
        return is_temporary(user)


class NewPasswordForm(AdminPasswordChangeForm):
    """The admin's form (old password, the new one twice, the validators of the settings), and not the old password again."""
    error_messages = {**AdminPasswordChangeForm.error_messages,
                      "password_reused": gettext_lazy("The new password must be different from the old one.")}

    def clean(self):
        cleaned = super().clean()
        # Only when the old password was right, so that the form cannot be used to test guesses of it
        old, new = cleaned.get("old_password"), cleaned.get("new_password1")
        if old and new and old == new:
            self.add_error("new_password1", ValidationError(self.error_messages["password_reused"], code="password_reused"))
        return cleaned

    def save(self, commit=True):
        user = super().save(commit)
        if commit:
            clear(user)
        return user


def wrong_old_password(response):
    """True if the form answered that the old password was wrong: the only answer that counts against the limit."""
    form = (getattr(response, "context_data", None) or {}).get("form")
    return bool(form) and form.has_error("old_password", "password_incorrect")


def password_change(request, extra_context=None):
    """What `admin.site.password_change` does, with NewPasswordForm (config/urls.py puts it in its place). On the page
    of someone whose password is temporary, it says why they are there and offers to log out.

    The limits count before the form looks at the password (atomic, so that requests sent together do not all pass), and a
    request that was not a wrong old password is given back. At the limit the answer is a page with a line that says so, a
    429 with `Retry-After`: this is a page the person is sent to, a bare error would leave them nowhere."""
    site = admin.site
    request.current_app = site.name
    temporary = is_temporary(request.user)
    guessing = request.method in ("POST", "PUT")
    counted = []
    if guessing:
        counted = [(f"admin-password:{ratelimit.client_ip(request)}", 10), (f"admin-password-net:{ratelimit.client_network(request)}", 30)]
        # Both are counted, whatever the first says
        if any([ratelimit.over_limit(name, limit, WINDOW) for name, limit in counted]):
            response = TemplateResponse(request, TEMPLATE, {**site.each_context(request), "title": _("Password change"),
                                                            "wait": True, "temporary": temporary}, status=429)
            response["Retry-After"] = str(ratelimit.seconds_left(WINDOW))
            return response
    options = {"form_class": NewPasswordForm,
               "template_name": TEMPLATE,
               "success_url": reverse("admin:password_change_done", current_app=site.name),
               "extra_context": {**site.each_context(request), **(extra_context or {}), "temporary": temporary}}
    response = PasswordChangeView.as_view(**options)(request)
    if guessing and not wrong_old_password(response):
        for name, _limit in counted:
            ratelimit.give_back(name, WINDOW)
    # The form is shown (a GET, or a POST that was refused): after a change the answer is a redirect
    if response.status_code == 200 and temporary:
        messages.warning(request, _("Your password is temporary. Choose a new one to go on."))
    return response

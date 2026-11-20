import unicodedata

from django.conf import settings
from django.conf.urls.i18n import i18n_patterns
from django.contrib import admin, messages
from django.contrib.auth import get_user_model
from django.core import signing
from django.http import HttpResponse, HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import include, path, reverse
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.debug import sensitive_post_parameters

from config import admin_wallet, ratelimit, second_factor, temporary, webauthn
from config.guard import ADMIN_SESSION
from meetups import views as meetups
from shops import explorer, staff
from shops import views as shops
from shops.admin import approve_places_view, approve_view, dashboard_view, set_aside_view
from website.views import robots, sitemap, switch_view, theme_file

# The admin opens only with a session born from the password, here (ADMIN_SESSION, config/guard.py), or from a staff
# account's own linked wallet at the hobby level (config/admin_wallet.py, ADMIN_WALLET_SIGN_IN). A session opened from
# the email link or from the shops' wallet sign-in is not enough, even if the account becomes staff later: becoming staff
# does not change the password, and the session would stay valid. Outside /admin/ that session does not count as a user:
# StaffApart.


# Sign-ins tried on one account in ADMIN_WINDOW, from any address: guessing spread over many addresses stops here too
ADMIN_TRIES_PER_ACCOUNT = 20
ADMIN_WINDOW = 900


def admin_account(request, name=None):
    """The name of the account tried, as the counter knows it: the same for «Boss», «boss » and «ｂｏｓｓ». The sign-in
    form (UsernameField) applies NFKC before the lookup, so every name that signs in as one account must count as that
    account here too; casefold on top merges more names, never fewer. It is never stored as written, over_limit keeps
    only a hash. `name`: the account's name when it is not the one posted (the code's page, where the account is the
    one that passed the password)."""
    name = unicodedata.normalize("NFKC", request.POST.get("username", "") if name is None else name).strip().casefold()
    return f"admin-user:{name}"


# A browser that signed in to an account here before carries a signed cookie that names the account: for it the
# per-account limit does not refuse, so whoever spends that limit from elsewhere does not lock the staff out. The
# per-address limits still hold. The cookie is bound to the account's password (session auth hash): a new password
# ends it, and it holds nothing a thief could use elsewhere. For an account with a second factor it is bound to that
# factor too: only a cookie given by a sign-in with a factor the account has now, and the mode accepts, counts (a
# browser that signed in with the password alone, before the factor was added, is a stranger to it), on the password's
# page and the second factor's.
KNOWN_DEVICE_COOKIE = "admin_device"
KNOWN_DEVICE_SALT = "admin-known-device"
KNOWN_DEVICE_AGE = 180 * 86400


def known_device(request, name=None):
    """True if this browser signed in before to the account whose name is posted (or `name`, on the code's page), and its
    password did not change since; for an account with second factors, if it signed in with one of them."""
    value = request.COOKIES.get(KNOWN_DEVICE_COOKIE)
    if not value:
        return False
    try:
        data = signing.loads(value, salt=KNOWN_DEVICE_SALT, max_age=KNOWN_DEVICE_AGE)
    except signing.BadSignature:
        return False
    User = get_user_model()
    # As the sign-in form reads the name (UsernameField), and the backend looks it up
    name = unicodedata.normalize("NFKC", (request.POST.get("username", "") if name is None else name).strip())
    try:
        user = User._default_manager.get_by_natural_key(name)
    except User.DoesNotExist:
        return False
    if not (
        isinstance(data, dict)
        and data.get("user") == user.pk
        and user.is_active
        and user.is_staff
        and signing.constant_time_compare(str(data.get("auth", "")), user.get_session_auth_hash())
    ):
        return False
    factors = second_factor.accepted_factors(user)
    return not factors or data.get("factor") in factors.marks()


def remember_device(response, user, factor=None):
    """The cookie of a known browser, after a right password (and, with `factor`, the right code or passkey of that
    factor: the cookie names it by kind and key)."""
    value = {
        "user": user.pk,
        "auth": user.get_session_auth_hash(),
        **({"factor": second_factor.factor_key(factor)} if factor else {}),
    }
    response.set_cookie(
        KNOWN_DEVICE_COOKIE,
        signing.dumps(value, salt=KNOWN_DEVICE_SALT),
        max_age=KNOWN_DEVICE_AGE,
        path="/admin/",
        secure=settings.SESSION_COOKIE_SECURE,
        httponly=True,
        samesite="Lax",
    )


@sensitive_post_parameters("password")
def admin_login(request, extra_context=None):
    # By address (IPv6: by /64), by /48 network, which a tunnel gives for free with 65,536 /64 networks inside, and by
    # account. The same empty 429 for all three, whether the account exists or not. The account's tries are counted
    # from a known browser too, but they do not refuse it
    if request.method == "POST":
        spent = ratelimit.over_address_limits(request, "admin", 10, 30, ADMIN_WINDOW)
        if not spent:
            spent = ratelimit.over_limit(
                admin_account(request), ADMIN_TRIES_PER_ACCOUNT, ADMIN_WINDOW
            ) and not known_device(request)
        if spent:
            return HttpResponse(status=429, headers={"Retry-After": str(ratelimit.seconds_left(ADMIN_WINDOW))})
    # Whether the password is temporary has to be read before the sign-in, which can store the same password with a newer hash
    before = temporary.before_sign_in(request.POST.get("username", "")) if request.method == "POST" else None
    response = second_factor.login(request, extra_context)
    # An account with a second factor is not signed in after the password: it waits for the code (config/second_factor.py).
    # The right password gives its try back as below; the codes count on their own, and the browser becomes a known one
    # only with the right code
    waiting = getattr(response, "pending_user", None)
    if request.method == "POST" and waiting is not None:
        ratelimit.give_back(admin_account(request), ADMIN_WINDOW)
        temporary.after_sign_in(waiting, request.POST.get("password", ""), before)
        return response
    # To a POST the admin answers with a redirect only when the password was right and the account is staff
    if request.method == "POST" and response.status_code == 302 and request.user.is_authenticated:
        request.session[ADMIN_SESSION] = True
        # The right password does not use up the account's tries: only the wrong ones count
        ratelimit.give_back(admin_account(request), ADMIN_WINDOW)
        # A temporary password stays so if this sign-in stored it with a newer hash (config/temporary.py)
        temporary.after_sign_in(request.user, request.POST.get("password", ""), before)
        remember_device(response, request.user)
    return response


def second_factor_page(request, factors, context, status=200):
    """The second factor's page: the passkey, with a fresh challenge, where the account has one and passkeys work
    here; the code, where the account has a TOTP the mode accepts, or, in `passkey` mode, one to add a first passkey."""
    here = second_factor.passkeys_here(request)
    context = {
        **context,
        "has_passkeys": bool(factors.passkeys),
        "passkeys_here": here,
        "has_totp": factors.code_factor is not None,
        "bootstrap": factors.bootstrap is not None,
        "public_url": settings.PUBLIC_URL,
    }
    if factors.passkeys and here:
        context["passkey_options"] = webauthn.request_options(
            second_factor.pending_challenge(request), [passkey.credential_id for passkey in factors.passkeys]
        )
    return TemplateResponse(request, second_factor.CODE_TEMPLATE, context, status=status)


@sensitive_post_parameters("code", "credential")
@csrf_protect
@never_cache
def admin_code(request):
    """The second step of the sign-in of an account with a second factor (config/second_factor.py): a passkey, or the
    code of its app. Only the session that passed the password, within five minutes. Every code typed and every passkey
    posted counts against the sign-in's limits by address and network, as a password does. A code, from a browser that
    is not a known one of the account's factors, counts against the account's own budget of codes too
    (second_factor.CODE_LIMITS: 5 in 15 minutes, 20 in a day), apart from the passwords'; a passkey cannot be guessed
    and has none. The right one gives the account's codes back, signs in and makes the browser a known one; the first
    wrong code of a day is written in the account's history and told to the people who look after the site. A passkey
    refused for any reason gets one and the same answer."""
    found = second_factor.pending(request)
    if found is None:
        if second_factor.PENDING in request.session:
            second_factor.end_pending(request)
            messages.error(request, _("The time to type the code ran out, or the account changed. Sign in again."))
        return HttpResponseRedirect(reverse("admin:login"))
    user, factors, target = found
    name = user.get_username()
    context = {**admin.site.each_context(request), "title": _("Log in"), "subtitle": None, "username": name}
    request.current_app = admin.site.name
    if request.method != "POST":
        return second_factor_page(request, factors, context)
    passkey = "credential" in request.POST
    wait = ADMIN_WINDOW if ratelimit.over_address_limits(request, "admin", 10, 30, ADMIN_WINDOW) else 0
    counted = []
    if not wait and not passkey and not known_device(request, name):
        # A known browser of these factors keeps the address limits only: somebody else's guesses do not lock it out
        wait, counted = second_factor.over_code_limits(user)
    if wait:
        if passkey:
            # The challenge of the page is used up all the same
            second_factor.take_challenge(request, second_factor.PENDING)
        response = second_factor_page(request, factors, {**context, "wait": True, "wait_passkey": passkey}, status=429)
        response["Retry-After"] = str(ratelimit.seconds_left(wait))
        return response
    if passkey:
        # Told only for a passkey refused against a live challenge: one a second tab replaced, or that ran out, says
        # nothing about the password
        outcome = {}
        key, copied = second_factor.take_assertion(request, user, request.POST.get("credential", ""), outcome)
        if key is None:
            response = second_factor_page(request, factors, {**context, "passkey_wrong": True})
            if not copied and not outcome.get("gone"):
                second_factor.wrong_passkey(response, user)
            return response
        response = second_factor.open_session(request, user, key, target)
        remember_device(response, user, key)
        return response
    # In `passkey` mode a code is checked only for an account without a passkey (its bootstrap), else it is wrong
    code_factor = factors.code_factor
    if code_factor is not None and second_factor.take_code(code_factor, request.POST.get("code")):
        second_factor.give_back(counted)
        bootstrap = factors.totp is None
        response = second_factor.open_session(request, user, code_factor, target, bootstrap=bootstrap)
        # The code that only lets a first passkey be added makes no known browser
        if not bootstrap:
            remember_device(response, user, code_factor)
        return response
    response = second_factor_page(request, factors, {**context, "wrong": True})
    if code_factor is not None:
        second_factor.wrong_code(response, user)
    return response


def admin_permission(request):
    # The last check only for a session that passed the others: it reads the account's second factor
    return (
        request.user.is_active
        and request.user.is_staff
        and request.session.get(ADMIN_SESSION, False)
        and second_factor.session_holds(request)
    )


admin.site.has_permission = admin_permission
# The sign-in that waits for the second factor's code (config/second_factor.py); /admin/login/ is admin_login below
admin.site.login = admin_login
# The change of password asks for a new one, not the old one again (config/temporary.py); it has to be here before
# admin.site.urls is read below
admin.site.password_change = temporary.password_change


urlpatterns = [
    path("theme/<path:path>", theme_file),
    path("shops.json", shops.shops_json, name="shops_json"),
    path("sitemap.xml", sitemap, name="sitemap"),
    path("robots.txt", robots, name="robots"),
    path("view/", switch_view, name="switch_view"),
    path("health/", shops.health, name="health"),
    # The fake explorer of the tests and of practice sites (shops/explorer.py): 404 unless FAKE_EXPLORER, never on mainnet
    path("dev/explorer/", explorer.pay, name="fake_explorer"),
    path("dev/explorer/api", explorer.api_root, name="fake_explorer_api"),
    path("dev/explorer/api/address/<str:address>/txs", explorer.address_txs, name="fake_explorer_txs"),
    path("admin/login/", admin_login),
    # The second factor, a passkey or a code, between the password and the admin (config/second_factor.py)
    path("admin/login/code/", admin_code, name="admin_second_factor_code"),
    # The sign-in with a linked wallet, ADMIN_WALLET_SIGN_IN with `optional` (config/admin_wallet.py): 404 otherwise
    path("admin/login/wallet/", admin_wallet.login_page, name="admin_wallet_login"),
    path("admin/login/wallet/status/", admin_wallet.login_status, name="admin_wallet_status"),
    # The account's own second factor: set it up, turn it off, behind the admin's own checks
    path("admin/second-factor/", admin.site.admin_view(second_factor.setup_view), name="admin_second_factor"),
    # What became of the code of a wallet being linked there (ADMIN_WALLET_SIGN_IN): 404 when off
    path(
        "admin/second-factor/wallet/status/",
        admin.site.admin_view(admin_wallet.link_status),
        name="admin_wallet_link_status",
    ),
    # What to do today about the registers, behind the admin's own checks (the password session, staff)
    path("admin/today/", admin.site.admin_view(dashboard_view), name="admin_dashboard"),
    path("admin/today/approve/<uuid:shop>/", admin.site.admin_view(approve_view), name="admin_approve_shop"),
    path("admin/today/set-aside/<uuid:shop>/", admin.site.admin_view(set_aside_view), name="admin_set_aside_shop"),
    path(
        "admin/today/approve-places/<uuid:shop>/",
        admin.site.admin_view(approve_places_view),
        name="admin_approve_places",
    ),
    path("admin/", admin.site.urls),
    path(".well-known/nostr.json", shops.nostr_names, name="nostr_names"),
    path("register/<uuid:shop>/manifest.webmanifest", shops.register_manifest, name="register_manifest"),
    path("lnurl-auth/", shops.lnurl_auth, name="lnurl_auth"),
    path("lnurl-auth/status/", shops.login_status, name="login_status"),
    # The wallet's callback for /admin/ (config/admin_wallet.py), outside /admin/ as the shops' one: 404 when off
    path("lnurl-auth/staff/", admin_wallet.auth, name="admin_wallet_auth"),
    # Team access to a site under construction (team/): every address is a 404 unless TEAM_ACCESS=on
    path("team/", include("team.urls")),
    path("meetups.ics", meetups.feed, name="meetups_feed"),
    # A staff register (shops/staff.py): its manifest, with the register's id instead of the shop, and its key in the query
    path("register/p/<uuid:register>/manifest.webmanifest", staff.manifest, name="staff_manifest"),
]
urlpatterns += i18n_patterns(
    path("shops/", include("shops.urls")), path("meetups/", include("meetups.urls")), path("", include("website.urls"))
)

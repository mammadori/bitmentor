import unicodedata

from django.conf.urls.i18n import i18n_patterns
from django.conf import settings
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.core import signing
from django.http import HttpResponse
from django.urls import include, path
from django.views.decorators.debug import sensitive_post_parameters

from config import ratelimit, temporary
from config.guard import ADMIN_SESSION
from meetups import views as meetups
from shops import explorer, staff
from shops.admin import dashboard_view
from shops import views as shops
from website.views import robots, sitemap, switch_view, theme_file
from website import blossom


# The admin opens only with a session born from the password, here (ADMIN_SESSION, config/guard.py). A session opened
# from the email link or from a wallet signature is not enough, even if the account becomes staff later: becoming staff does not
# change the password, and the session would stay valid. Outside /admin/ that session does not count as a user: StaffApart.


# Sign-ins tried on one account in ADMIN_WINDOW, from any address: guessing spread over many addresses stops here too
ADMIN_TRIES_PER_ACCOUNT = 20
ADMIN_WINDOW = 900


def admin_account(request):
    """The name of the account tried, as the counter knows it: the same for «Boss», «boss » and «ｂｏｓｓ». The sign-in
    form (UsernameField) applies NFKC before the lookup, so every name that signs in as one account must count as that
    account here too; casefold on top merges more names, never fewer. It is never stored as written, over_limit keeps
    only a hash."""
    name = unicodedata.normalize("NFKC", request.POST.get("username", "")).strip().casefold()
    return f"admin-user:{name}"


# A browser that signed in to an account here before carries a signed cookie that names the account: for it the
# per-account limit does not refuse, so whoever spends that limit from elsewhere does not lock the staff out. The
# per-address limits still hold. The cookie is bound to the account's password (session auth hash): a new password
# ends it, and it holds nothing a thief could use elsewhere.
KNOWN_DEVICE_COOKIE = "admin_device"
KNOWN_DEVICE_SALT = "admin-known-device"
KNOWN_DEVICE_AGE = 180 * 86400


def known_device(request):
    """True if this browser signed in before to the account whose name is posted, and its password did not change since."""
    value = request.COOKIES.get(KNOWN_DEVICE_COOKIE)
    if not value:
        return False
    try:
        data = signing.loads(value, salt=KNOWN_DEVICE_SALT, max_age=KNOWN_DEVICE_AGE)
    except signing.BadSignature:
        return False
    User = get_user_model()
    # As the sign-in form reads the name (UsernameField), and the backend looks it up
    name = unicodedata.normalize("NFKC", request.POST.get("username", "").strip())
    try:
        user = User._default_manager.get_by_natural_key(name)
    except User.DoesNotExist:
        return False
    return (isinstance(data, dict) and data.get("user") == user.pk and user.is_active and user.is_staff
            and signing.constant_time_compare(str(data.get("auth", "")), user.get_session_auth_hash()))


def remember_device(response, user):
    response.set_cookie(KNOWN_DEVICE_COOKIE, signing.dumps({"user": user.pk, "auth": user.get_session_auth_hash()},
                                                           salt=KNOWN_DEVICE_SALT),
                        max_age=KNOWN_DEVICE_AGE, path="/admin/", secure=settings.SESSION_COOKIE_SECURE, httponly=True,
                        samesite="Lax")


@sensitive_post_parameters("password")
def admin_login(request, extra_context=None):
    # By address (IPv6: by /64), by /48 network, which a tunnel gives for free with 65,536 /64 networks inside, and by
    # account. The same empty 429 for all three, whether the account exists or not. The account's tries are counted
    # from a known browser too, but they do not refuse it
    if request.method == "POST":
        spent = ratelimit.over_address_limits(request, "admin", 10, 30, ADMIN_WINDOW)
        if not spent:
            spent = ratelimit.over_limit(admin_account(request), ADMIN_TRIES_PER_ACCOUNT, ADMIN_WINDOW) and not known_device(request)
        if spent:
            return HttpResponse(status=429, headers={"Retry-After": str(ratelimit.seconds_left(ADMIN_WINDOW))})
    # Whether the password is temporary has to be read before the sign-in, which can store the same password with a newer hash
    before = temporary.before_sign_in(request.POST.get("username", "")) if request.method == "POST" else None
    response = admin.site.login(request, extra_context)
    # To a POST the admin answers with a redirect only when the password was right and the account is staff
    if request.method == "POST" and response.status_code == 302 and request.user.is_authenticated:
        request.session[ADMIN_SESSION] = True
        # The right password does not use up the account's tries: only the wrong ones count
        ratelimit.give_back(admin_account(request), ADMIN_WINDOW)
        # A temporary password stays so if this sign-in stored it with a newer hash (config/temporary.py)
        temporary.after_sign_in(request.user, request.POST.get("password", ""), before)
        remember_device(response, request.user)
    return response


def admin_permission(request):
    return request.user.is_active and request.user.is_staff and request.session.get(ADMIN_SESSION, False)


admin.site.has_permission = admin_permission
# The change of password asks for a new one, not the old one again (config/temporary.py); it has to be here before
# admin.site.urls is read below
admin.site.password_change = temporary.password_change


urlpatterns = [
    path("theme/<path:path>", theme_file),
    path("shops.json", shops.shops_json, name="shops_json"),
    # Read-only Blossom (BUD-01) for the theme files, when `blossom: true` (website/blossom.py); the list before the hashes
    path("blossom/list", blossom.listing, name="blossom_list"),
    path("blossom/<str:name>", blossom.blob, name="blossom_blob"),
    path("sitemap.xml", sitemap, name="sitemap"),
    path("robots.txt", robots, name="robots"),
    path("view/", switch_view, name="switch_view"),
    path("health/", shops.health, name="health"),
    # The fake explorer of the tests and of practice sites (shops/explorer.py): 404 unless FAKE_EXPLORER, never on mainnet
    path("dev/explorer/", explorer.pay, name="fake_explorer"),
    path("dev/explorer/api", explorer.api_root, name="fake_explorer_api"),
    path("dev/explorer/api/address/<str:address>/txs", explorer.address_txs, name="fake_explorer_txs"),
    path("admin/login/", admin_login),
    # What to do today about the registers, behind the admin's own checks (the password session, staff)
    path("admin/today/", admin.site.admin_view(dashboard_view), name="admin_dashboard"),
    path("admin/", admin.site.urls),
    path(".well-known/nostr.json", shops.nostr_names, name="nostr_names"),
    path("register/<uuid:shop>/manifest.webmanifest", shops.register_manifest, name="register_manifest"),
    path("lnurl-auth/", shops.lnurl_auth, name="lnurl_auth"),
    path("lnurl-auth/status/", shops.login_status, name="login_status"),
    # Team access to a site under construction (team/): every address is a 404 unless TEAM_ACCESS=on
    path("team/", include("team.urls")),
    path("meetups.ics", meetups.feed, name="meetups_feed"),
    # A staff register (shops/staff.py): its manifest, with the register's id instead of the shop, and its key in the query
    path("register/p/<uuid:register>/manifest.webmanifest", staff.manifest, name="staff_manifest"),
]
urlpatterns += i18n_patterns(path("shops/", include("shops.urls")),
                              path("meetups/", include("meetups.urls")),
                              path("", include("website.urls")))

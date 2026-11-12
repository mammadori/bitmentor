import logging
import mimetypes
import re
import secrets
from datetime import timedelta
from functools import wraps
from smtplib import SMTPException
from urllib.parse import urlencode

import segno
from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model, login, logout
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.cache import cache
from django.core.mail import send_mail
from django.db import DatabaseError, IntegrityError, transaction
from django.db.models import F
from django.db.models.functions import Lower
from django.http import Http404, HttpResponse, HttpResponseBadRequest, JsonResponse
from django.middleware.csrf import get_token
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import number_format
from django.utils.cache import patch_vary_headers
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.safestring import mark_safe
from django.utils.translation import get_language
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.http import require_POST

from config import features, limits, mailbudget, ratelimit
from config.community import community, page_path, private_page, shop_form_links, shop_page_links
from config.middleware import csp_with_connect

from . import address_change, certified, details, first_day, guard, journey, lightning_watch, vacation
from . import helpdesk, osm, osm_request, public_cache, training
from .directory import WAITING_FOR_WALLET, nearby_shops, public_shops, waits_for_wallet
from .forms import NewShopForm, ProfileForm, RecoveryForm, ShopDetailsForm, ShopDisplayForm, ShopForm, WalletForm, prefill, tidy_lightning_address
from .lnaddress import BUSY, NO_ANSWER, UNKNOWN_DOMAIN, checked_live, explanation, refusal, suggestion
from .lnurl_auth import K1, encode_lnurl, verify
from .models import HAS_REGISTER, REGISTER_AFTER_CLOSING, keeps_register, register_kept, LoginChallenge, Profile, Shop, claim_digest
from .nostr import public_key
from .register import connect_origins, register_data

logger = logging.getLogger(__name__)
CHALLENGE_LIFETIME = timedelta(minutes=5)
# New sign-in challenges (a row and a QR each) for browsers without one alive: a browser makes about two every five
# minutes, a school or a shop behind one address a few dozen
CHALLENGES_PER_IP = 60
CHALLENGES_PER_NETWORK = 300
CHALLENGE_WINDOW = 600
# An account costs nothing. The limit per address (limits.shops_per_address_hour) allows for an evening with many shops
SHOP_WINDOW = 3600
# One person helping an evening of shops, and a /48 with a few such evenings at once
SHOPS_PER_USER = 20
SHOPS_PER_NETWORK = 60
# The check of the Lightning address on submit, name resolution included (lnaddress.refusal): past it, "try again"
LIGHTNING_SUBMIT_SECONDS = 12
# The live check of a Lightning address sends requests to a server chosen by whoever writes: few per person, a few more
# per address for an evening with many shops behind one connection. Within this time the page always gets an answer
LIGHTNING_CHECKS_PER_USER = 12
LIGHTNING_CHECKS_PER_IP = 40
# A network /48, which a tunnel or a VPS gives for free with 65,536 /64s inside
LIGHTNING_CHECKS_PER_NETWORK = 80
LIGHTNING_CHECK_WINDOW = 600
LIGHTNING_CHECK_SECONDS = 6
DOUBLE_SUBMIT = timedelta(minutes=2)
# Longer than any address: what is beyond is not looked at
MAX_ADDRESS_TYPED = 400
GUIDE_PAGE = "merchants"
HEALTH_MEMORY = 30


def not_indexed(view):
    """A page that must not end up in search engines, in any mode: the register, the sign, the ones of people who have
    an account. The header is what takes it out of the results: that is why robots.txt lets the register and the sign be
    opened, as they are found in a link or a QR."""
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        response = view(request, *args, **kwargs)
        response["X-Robots-Tag"] = "noindex, nofollow"
        return response
    return wrapper


def no_store(view):
    """A page that carries a sign-in or someone's email: the sign-in page with its challenge, the email links. No cache
    keeps it, the browser's included. Pages of someone signed in get it from config.middleware.PrivateForAccounts."""
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        response = view(request, *args, **kwargs)
        response.headers.setdefault("Cache-Control", "private, no-store")
        return response
    return wrapper












def account_label(user, email=""):
    """Which account this panel belongs to, in a few characters: the recovery email when there is one, otherwise the
    start and the end of the wallet's key. A wallet signs in whatever browser shows it the code (LUD-04): someone could
    hand over a code of their own, and whoever then fills in this panel would be filling in theirs."""
    if email:
        return email
    name = user.get_username()
    return f"{name[:6]}…{name[-4:]}" if len(name) > 12 else name


def local_path(target):
    """Where to go back to after signing in: only a page of this site, whatever is written in the link."""
    return target if target.startswith("/") and url_has_allowed_host_and_scheme(target, allowed_hosts=None) else ""


@not_indexed
@no_store
def index(request):
    target = local_path(request.GET.get("next", ""))
    if request.user.is_authenticated:
        if target and request.method == "GET":
            return redirect(target)
        profile, _created = Profile.objects.get_or_create(user=request.user)
        form = ProfileForm(request.POST or None, initial={"email": profile.email})
        if request.method == "POST" and form.is_valid():
            save_recovery_email(request, profile, form.cleaned_data["email"].lower())
            return redirect("shops")
        shops = list(request.user.shops.order_by("-created").prefetch_related("closures"))
        training.mark_trained(shops)
        # The answer to "Try again now" goes next to its shop, the other messages on top
        notices = list(messages.get_messages(request))
        osm_on = osm.settings() is not None
        # The addresses in the QR codes never come from the Host header: whoever opens the panel from the onion must not pass the onion
        for shop in shops:
            shop.register_url = f"{settings.PUBLIC_URL}{reverse('register', args=[shop.id])}"
            shop.register_qr = segno.make(shop.register_url, error="m").svg_data_uri(scale=5, border=2)
            shop.first_day = first_day_summary(shop) if first_day.applies(shop) else None
            shop.lightning_watch = lightning_watch.panel(shop)
            # The owner's own change of the address, for a while (shops/address_change.py)
            shop.address_change = address_change.panel(shop)
            shop.lightning_notes = [note for note in notices if note.extra_tags == f"lightning lightning-{shop.id}"]
            # The path to a complete, verified shop: only what the site already knows, no other server asked
            shop.journey = journey.summary(shop, shop.first_day, shop.lightning_watch) if journey.applies(shop) else None
            shop.marks = shop.journey["card"].get("marks", []) if shop.journey else []
            shop.staff_count = shop.staff_registers.filter(retired__isnull=True).count()
            # Without a register yet: the page to connect the wallet opens (needs_wallet), and a shop saved before
            # having a wallet or moved here waits off the map for it (waits_for_wallet), which the panel says first
            shop.needs_wallet = needs_wallet(shop)
            # A historic shop keeps its register for a year after its closing day (models.REGISTER_AFTER_CLOSING)
            shop.register_kept = keeps_register(shop)
            shop.register_until = shop.closed_on + REGISTER_AFTER_CLOSING if shop.closed_on else None
            shop.waits_for_wallet = shop.needs_wallet and waits_for_wallet(shop)
            shop.waits_after_move = shop.waits_for_wallet and shop.moved_register
            # Closed for a while: the card to say so, and the welcome back after (shops/vacation.py)
            shop.break_eligible = vacation.enabled() and not (shop.closed_on or shop.waits_for_wallet)
            shop.break_state = vacation.panel(shop) if shop.break_eligible else None
            shop.welcome = vacation.welcome(shop) if shop.break_eligible else None
            # «Something wrong?»: the request still open, or the one solved in the last days (shops/helpdesk.py)
            shop.help_state = helpdesk.panel(shop)
            # The real till waits for the basic pass of the merchant's training (shops/training.py); a signet one never does
            shop.till_quick = shop.has_register and training.quick_start(shop, request.user)
            shop.till_locked = shop.has_register and not shop.till_quick and training.locked(shop)
            # «Your shop on OpenStreetMap»: only when the community uses it, and not for a shop without a place on the lists
            shop.osm_enabled = osm_on and not shop.waits_for_wallet
            shop.osm_asked = osm_request.panel(shop) if shop.osm_enabled else None
        return render(request, "shops/panel.html", {"shops": shops, "form": form, "saved": "saved" in request.GET,
                                                     "signed_in": account_label(request.user, profile.email),
                                                     "training": training.panel(request.user, shops),
                                                     "first_day_count": FIRST_DAY_COUNT, "community_email": community()["email"],
                                                     "account_notice": guard.notice(request.user),
                                                     "notices": [note for note in notices if not note.extra_tags.startswith("lightning")]})
    LoginChallenge.objects.filter(created__lt=timezone.now() - CHALLENGE_LIFETIME).delete()
    # This browser's challenge, if still alive, is reused: a visit to the panel does not write a new one in the database each time
    alive = request.get_signed_cookie("k1", default="", salt="login", max_age=CHALLENGE_LIFETIME.total_seconds())
    challenge = (LoginChallenge.objects.filter(k1=alive, linking_key="", created__gte=timezone.now() - CHALLENGE_LIFETIME / 2).first()
                 if alive else None)
    # A new one only within the limits of the address and of its /48: without the cookie every visit writes a row. Over
    # them the page has no wallet code for a while, and the email still signs in
    if challenge is None and not ratelimit.over_address_limits(request, "challenge", CHALLENGES_PER_IP, CHALLENGES_PER_NETWORK,
                                                                CHALLENGE_WINDOW):
        challenge = LoginChallenge.new()
    lnurl = qr = ""
    if challenge:
        # The wallet signs for the domain of the callback: from the onion it would also have opened another account
        callback = f"{settings.PUBLIC_URL}{reverse('lnurl_auth')}"
        lnurl = encode_lnurl(f"{callback}?tag=login&action=login&k1={challenge.k1}")
        qr = segno.make(lnurl, error="l").svg_data_uri(scale=6, border=2)
    response = render(request, "shops/login.html", {
        "lnurl": lnurl, "qr": qr, "returning": bool(target),
        # Staff signed in to the admin lands here from a shop's page (config/guard.py): the page says why
        "staff_apart": getattr(request, "staff_user", None) is not None,
        "recovery_url": f"{reverse('recovery')}?{urlencode({'next': target})}" if target else reverse("recovery")},
        status=200 if challenge else 429)
    if challenge:
        response.set_signed_cookie("k1", challenge.k1, salt="login", httponly=True, samesite="Lax",
                                   secure=request.is_secure(), max_age=CHALLENGE_LIFETIME.seconds)
    else:
        response["Retry-After"] = str(ratelimit.seconds_left(CHALLENGE_WINDOW))
    return response


def signs_in_here(user):
    """The staff comes in only from the admin, with the password: a link in the mail or a wallet key is not enough. From an
    address allowed to the admin, whoever reads the recovery email of a staff account would get into the admin too."""
    return not (user.is_staff or user.is_superuser)


def live_challenges():
    return LoginChallenge.objects.filter(created__gte=timezone.now() - CHALLENGE_LIFETIME)


def lnurl_auth(request):
    k1 = request.GET.get("k1", "")
    key = request.GET.get("key", "").lower()
    challenge = live_challenges().filter(k1=k1).first() if K1.fullmatch(k1) else None
    if not challenge or not verify(challenge.k1, request.GET.get("sig", ""), key):
        return JsonResponse({"status": "ERROR", "reason": "Invalid login"})
    # The challenge is in a QR that anyone can see: the first signature counts, a second one does not replace it
    LoginChallenge.objects.filter(pk=k1, linking_key="").update(linking_key=key)
    if not LoginChallenge.objects.filter(pk=k1, linking_key=key).exists():
        return JsonResponse({"status": "ERROR", "reason": "Invalid login"})
    return JsonResponse({"status": "OK"})



def login_status(request):
    k1 = request.get_signed_cookie("k1", "", salt="login")
    challenge = live_challenges().filter(k1=k1).exclude(linking_key="").first()
    if not challenge:
        return JsonResponse({"logged_in": False})
    user = get_user_model().objects.filter(username=challenge.linking_key).first()
    if not user:
        # Every signed key is a new account: without a cap, whoever generates keys in bursts fills the database
        if limits.refuse("accounts"):
            return JsonResponse({"logged_in": False, "error": limits.message()}, status=503)
        if ratelimit.over_limit(f"account:{ratelimit.client_ip(request)}", limits.rate("accounts_per_address_hour"), 3600):
            return JsonResponse({"logged_in": False, "error": _("Too many new accounts from here: try again later.")},
                                status=429)
        user, _created = get_user_model().objects.get_or_create(username=challenge.linking_key)
    challenge.delete()
    if not signs_in_here(user):
        return JsonResponse({"logged_in": False})
    login(request, user)
    return JsonResponse({"logged_in": True})


def shop_map(request):
    """The list under the map is the same for everybody and costs the most: it comes from a short cache
    (shops/public_cache.py). The page around it is rendered for each request, since it says who is signed in."""
    listing = public_cache.cached("map-list", map_listing)
    return render(request, "shops/map.html", {"shop_list": mark_safe(listing["html"]), "places": listing["places"],
                                               "paid_texts": listing["texts"], "shops": listing["shops"]})


# What a community's map template may read of each shop in `shops` (for example a count, or the names and towns of the
# places of a year): public fields only, the same ones /shops.json gives
MAP_SHOP_FIELDS = ("id", "external_id", "name", "city", "closed_on", "discount")


def map_shop(shop):
    found = {field: getattr(shop, field) for field in MAP_SHOP_FIELDS}
    found["id"] = str(shop.id)
    # As in /shops.json: a historic shop has no discount
    found["discount"] = 0 if shop.closed_on else shop.discount
    return found


def map_listing():
    """What shop_map caches: the list rendered as HTML, the number of places to pay and the words for map.js. Nothing
    here reads a request or an account."""
    # First the places to pay, the historic ones at the end: the counter counts only the first
    shops = list(public_shops().prefetch_related("closures").order_by(F("closed_on").asc(nulls_first=True), "-discount", "name"))
    now = timezone.now()
    training.mark_trained(shops, now)
    for shop in shops:
        shop.marks = certified.marks(shop, now)
        # A shop closed for a while stays a place to pay and on the map: it only says so (shops/vacation.py)
        shop.break_note = vacation.note(shop)
    texts = certified.texts()
    if not certified.enabled():
        # No badge, so nothing to say about it; the icons' sentences stay
        del texts["certified"], texts["rule"]
    if vacation.enabled():
        texts["break"] = vacation.texts()
    texts["trained"] = str(training.TRAINED)
    return {"html": render_to_string("shops/map_list.html", {"shops": shops}), "texts": texts,
            "places": sum(1 for shop in shops if not shop.closed_on),
            # Plain values, not the models: they go in the cache and every visitor gets them
            "shops": [map_shop(shop) for shop in shops]}


def shops_json(request):
    """The same body for everybody, from a short cache (shops/public_cache.py): browsers may keep it for half a minute.
    It changes with the language only (the links to the shops' pages)."""
    response = HttpResponse(public_cache.cached("json", shops_json_body), content_type="application/json")
    response["Cache-Control"] = f"public, max-age={public_cache.browser_seconds()}"
    # The language may come from the header or from the cookie (LocaleMiddleware)
    patch_vary_headers(response, ("Accept-Language", "Cookie"))
    return response


def shops_json_body():
    now = timezone.now()
    shops = training.mark_trained(public_shops().prefetch_related("closures").order_by("name"), now)
    # A feature the community turned off (config/features.py) has no key at all: `"certified": false` would say the shop
    # is not certified, `"break": null` that it is open, and neither is known
    certificates, breaks = features.enabled(features.CERTIFIED), features.enabled(features.VACATION)
    onchain = features.enabled(features.ONCHAIN)
    # A historic shop is there, with its date, but without a discount: whoever reads the JSON must not send anyone to pay there.
    # external_id is the shop's number in the imported list: it does not change from one installation to another, while the id
    # is new at every import. It is empty for whoever signed up alone.
    return JsonResponse({"shops": [
        {"id": str(shop.id), "external_id": shop.external_id, "name": shop.name, "description": shop.description,
         "city": shop.city,
         # The address and the website only when the owner shows them (shops/details.py): a hidden one is not there at all
         **details.api(shop),
         "discount": 0 if shop.closed_on else shop.discount, "lightning": shop.lightning,
         # What the owner says, no more: the site knows no on-chain address and no on-chain payment. Absent while
         # on-chain at the register, an experimental feature, is off
         **({"onchain": shop.onchain} if onchain else {}), "latitude": shop.latitude,
         "longitude": shop.longitude, "url": reverse("shop_detail", args=[shop.id]), "closed_on": shop.closed_on,
         # Only the flag and the bands (shops/certified.py): never the dates, counts or amounts behind them
         **({"certified": shop.certified} if certificates else {}),
         "trained": shop.trained,
         "paid": None if shop.closed_on else certified.bands(shop, now),
         # Closed for a while (holidays, works…): the reason from a list, the day it opens again, never a free text
         **({"break": vacation.api(shop)} if breaks else {})}
        for shop in shops]}).content


def shop_detail(request, shop):
    """The page stays even for a suspended shop: the QR of the sign leads here. But the association took it off the
    maps, maybe for a site or a text that are wrong: no description or contacts, and no search engines.

    Whoever scans the sign stands at the till: the page says how to pay there and has a card to show the cashier,
    in the community's main language. Neither for a historic shop nor for a suspended one. Then the places to pay
    nearby, and the community's own pages about the shop (`shop_page` in community.yml)."""
    shop = get_object_or_404(Shop, pk=shop)
    reduced = shop.suspended
    shop.trained = not reduced and training.is_trained(shop)
    language = get_language()
    extras = shop_page_links(language, shop.external_id)
    # What the owner shows (shops/details.py); a shop the association took off the map shows no contacts
    shown = details.shown_on_a_suspended_page(shop) if reduced else details.shown(shop)
    response = render(request, "shops/shop.html", {
        "shop": shop, "reduced": reduced, "facts": details.rows(shop, shown),
        "payment": None if reduced or shop.closed_on or waits_for_wallet(shop) else till_methods(shop),
        "waiting": waits_for_wallet(shop), "break_note": "" if reduced else vacation.note(shop),
        "main_language": settings.LANGUAGE_CODE, "language": language,
        "stories": [] if reduced else extras["stories"],
        "certified_rule": certified.MEANING if shop.certified and not reduced else "",
        "nearby": [{"shop": other, "distance": distance_text(other.metres)} for other in nearby_shops(shop)]})
    if shop.suspended or not shop.listed or waits_for_wallet(shop):
        response["X-Robots-Tag"] = "noindex, nofollow"
    return response


def till_methods(shop):
    """How the shop takes bitcoin at the till: "lightning", "onchain" or "both". A shop with a register takes Lightning,
    and on-chain too when its owner says so (`onchain`). A shop imported from a list has its own till: there the list's
    Lightning flag is all we know, and without it, on-chain.

    While on-chain at the register is off (an experimental feature, config/features.py) no page says on-chain: the
    owner's word is not read, and the shop with its own till and no Lightning flag is plain "bitcoin"."""
    onchain = features.enabled(features.ONCHAIN)
    if shop.has_register or shop.lightning:
        return "both" if shop.onchain and onchain else "lightning"
    return "onchain" if onchain else "bitcoin"


def distance_text(metres):
    """Metres rounded to ten under a kilometre, then kilometres with one decimal, as the page's language writes them.
    Rounded here, half up: number_format cuts the decimals, and 999 m would be 0.9 km."""
    tens = max(10, int(metres / 10 + 0.5) * 10)
    return f"{tens} m" if tens < 1000 else f"{number_format(int(metres / 100 + 0.5) / 10, 1)} km"


def shop_by_external(request, external):
    """The number in the imported list is the same on every installation, the id is not: the community pages that
    name a shop go through here, and work both on development and in production.
    Only the shops on maps and lists: the numbers are consecutive, and by trying them one would find the others too, with the id
    that opens their register."""
    # PostgreSQL refuses a text with a NUL in it: no shop has one
    shop = None if "\x00" in external else (
        Shop.objects.filter(external_id=external, suspended=False, listed=True).exclude(WAITING_FOR_WALLET)
        .order_by("pk").first())
    if shop is None:
        raise Http404
    return redirect("shop_detail", shop=shop.id)


FIRST_DAY_COUNT = gettext_lazy("The first day: {done} of {total} done")


def first_day_summary(shop):
    """The first day of a shop for the panel, with its one-line title."""
    day = first_day.summary(shop)
    day["title"] = (_("The first day: all done") if day["complete"]
                    else str(FIRST_DAY_COUNT).format(done=day["done"], total=day["total"]))
    return day


# What an owner saves from the panel, per account and per hour: far more than anyone clicks, only a script reaches it
TICKS_PER_USER = 120
EDITS_PER_USER = 120
OWNER_WINDOW = 3600


@not_indexed
@login_required(login_url="shops")
@require_POST
def first_day_tick(request, shop):
    """The merchant ticks, or unticks, a step the site cannot know."""
    shop = get_object_or_404(request.user.shops.filter(HAS_REGISTER), pk=shop)
    field = first_day.TICKED.get(request.POST.get("step", ""))
    if not field:
        return HttpResponseBadRequest()
    if ratelimit.over_limit(f"first-day-user:{request.user.pk}", TICKS_PER_USER, OWNER_WINDOW):
        messages.error(request, _("You saved many times in a short while: wait a few minutes and try again."))
        return redirect(f"{reverse('shops')}#first-day-{shop.id}")
    setattr(shop, field, timezone.now() if request.POST.get("done") == "1" else None)
    shop.save(update_fields=[field])
    return redirect(f"{reverse('shops')}#first-day-{shop.id}")


# "Try again now" in the panel: each probe holds a web worker for up to half a minute
RETRIES_PER_SHOP = 3
RETRIES_PER_USER = 10
RETRY_WINDOW = 600
# The whole probe, name resolution included: past this the merchant hears "try again in a few minutes"
RETRY_SECONDS = 15


@not_indexed
@login_required(login_url="shops")
@require_POST
def lightning_retry(request, shop):
    """The owner asks to probe the shop's Lightning address again now, after fixing it. Only reads: the address stays as
    it is. The answer goes in the panel, so no email for a change seen there; the association hears it."""
    shop = get_object_or_404(lightning_watch.watched().filter(owner=request.user), pk=shop)
    back = redirect(f"{reverse('shops')}#lightning-{shop.id}")
    tag = f"lightning lightning-{shop.id}"
    if (ratelimit.over_limit(f"lightning-retry:{shop.id}", RETRIES_PER_SHOP, RETRY_WINDOW)
            or ratelimit.over_limit(f"lightning-retry-user:{request.user.pk}", RETRIES_PER_USER, RETRY_WINDOW)):
        messages.error(request, _("You already tried a few times: wait ten minutes and try again."), extra_tags=tag)
        return back
    if not lightning_watch.PANEL_PROBES.acquire(blocking=False):
        messages.error(request, _("Too many checks right now: try again in a minute."), extra_tags=tag)
        return back
    address = shop.lightning_address
    # The place is freed when the probe really ends, even past RETRY_SECONDS
    result = lightning_watch.probe_within(address, RETRY_SECONDS, done=lightning_watch.PANEL_PROBES.release)
    checked, change, recorded = lightning_watch.record(shop.pk, address, result)
    if change:
        lightning_watch.tell_association([(checked, change)])
    if result == lightning_watch.UNSURE:
        messages.error(request, _("Your wallet's provider is slow or busy right now: try again in a few minutes."),
                       extra_tags=tag)
    elif not recorded:
        messages.error(request, _("Your shop changed while it was being checked: look at it again."), extra_tags=tag)
    elif result is None:
        messages.success(request, _("Checked again just now: it works."), extra_tags=tag)
    else:
        messages.error(request, _("Checked again just now: it still does not work."), extra_tags=tag)
    return back


@not_indexed
@login_required(login_url="shops")
def till_card(request, shop):
    """An A4 sheet for whoever stands at the till, next to the sign: how to take a payment with the register, in its
    own words, and what to do when something goes differently. With ?qr=1 also the register code, for the staff."""
    shop = get_object_or_404(request.user.shops.filter(HAS_REGISTER), pk=shop)
    with_qr = request.GET.get("qr") == "1"
    context = {
        "shop": shop, "with_qr": with_qr, "paying_to": guard.paying_to(shop),
        "guide": f"{settings.PUBLIC_URL}{guide()}" if guide() else ""}
    if with_qr:
        context["register_url"] = f"{settings.PUBLIC_URL}{reverse('register', args=[shop.id])}"
        context["register_qr"] = segno.make(context["register_url"], error="m").svg_data_uri(scale=5, border=2)
    return render(request, "shops/till_card.html", context)


@not_indexed
@login_required(login_url="shops")
def shop_sign(request, shop):
    # A shop waiting for its wallet takes no payments yet: a sign would send customers to a till that is not there
    shop = get_object_or_404(request.user.shops.exclude(WAITING_FOR_WALLET), pk=shop)
    url = f"{settings.PUBLIC_URL}{reverse('shop_detail', args=[shop.id])}"
    return render(request, "shops/sign.html", {"shop": shop, "qr": segno.make(url, error="m").svg_data_uri(scale=10, border=1)})


def register_manifest(request, shop, start=None):
    shop = get_object_or_404(Shop.objects.filter(HAS_REGISTER), pk=shop)
    data = community()
    logo = data["logo"]
    sizes = "any" if logo.endswith(".svg") else "300x300"  # a vector logo has no size
    return JsonResponse({
        "name": shop.name, "short_name": shop.name[:12], "display": "standalone", "lang": get_language(),
        "start_url": start or reverse("register", args=[shop.id]), "theme_color": data.get("theme_color", "#333333"),
        "background_color": "#ffffff",
        "icons": [{"src": f"/theme/{logo}", "sizes": sizes, "type": mimetypes.guess_type(logo)[0] or "image/png"}]},
        content_type="application/manifest+json")


@require_POST
def sign_out(request):
    logout(request)
    # Whoever passes the phone leaves the register and leaves it open
    target = request.POST.get("next", "")
    if url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return redirect(target)
    return redirect("shops")


@not_indexed
@login_required(login_url="shops")
def new_shop(request):
    proposed = prefill(request.GET) if request.method == "GET" else {}
    form = NewShopForm(request.POST or None, initial=proposed, owner=request.user)
    if request.method == "POST" and form.is_valid() and too_many_shops(request, "shop"):
        form.add_error(None, _("Too many shops in a short time: wait a few minutes."))
    elif request.method == "POST" and form.is_valid() and (
            limits.refuse("shops") or (not form.cleaned_data["lightning_address"] and limits.refuse("shops_waiting_wallet"))):
        form.add_error(None, limits.message())
    elif request.method == "POST" and form.is_valid():
        draft = form.save(commit=False)
        # The probe asks a server chosen by whoever writes: before the lock, never holding a transaction while it waits
        problem = None if draft.awaiting_wallet else refusal(draft.lightning_address, request.user.pk,
                                                             seconds=LIGHTNING_SUBMIT_SECONDS)
        # The lock lines up double submissions: the second finds the first one's shop
        with transaction.atomic():
            Profile.objects.select_for_update().get_or_create(user=request.user)
            recent = timezone.now() - DOUBLE_SUBMIT
            twin = request.user.shops.filter(name=form.cleaned_data["name"], created__gte=recent).order_by("-created").first()
            if twin and needs_wallet(twin):
                # The first submit saved the shop without a wallet: an address sent again is never lost in silence
                if form.cleaned_data["lightning_address"]:
                    messages.info(request, _("Your shop was already saved a moment ago, without a wallet. Paste the "
                                             "address here: we try it and make the register."))
                return redirect("wallet_steps", shop=twin.id)
            if twin:
                return shop_saved()
            shop = draft
            shop.owner = request.user
            if shop.awaiting_wallet:
                # No wallet yet: the shop's page is saved, without a register, and the guide takes it from here
                shop.save()
                warn_about_vat(request, form)
                return redirect(f"{reverse('wallet_steps', args=[shop.id])}?saved=1")
            if not problem:
                shop.save()
                warn_about_vat(request, form)
                return shop_saved()
        form.add_error("lightning_address", problem)
    return render(request, "shops/shop_form.html", {"form": form, "guide": guide(), "prefilled": proposed,
                                                     "wallet_page": shop_form_links(get_language())["wallet"]})


@not_indexed
@login_required(login_url="shops")
def wallet_steps(request, shop):
    """The register for a shop that has none (needs_wallet), at /wallet/<id>/ and /connect/<id>/: a shop saved before
    having a wallet, one moved from another site, one of the directory given to its owner. A guide in a few steps, from
    choosing a wallet to an address that works, and the form with the address. Saving it makes the register with the
    same checks as a new shop, and puts the shop on the map."""
    shop = get_object_or_404(request.user.shops, pk=shop)
    if not needs_wallet(shop):
        return redirect("shops")
    form = WalletForm(request.POST if request.method == "POST" else None, instance=shop)
    done = connect_wallet(request, shop, form)
    if done is not None:
        return shop_saved() if done is True else done
    links = shop_form_links(get_language())
    wallets = [{**wallet, "platform_names": ", ".join(str(PLATFORM_NAMES[name]) for name in wallet["platforms"])}
               for wallet in links["wallets"]]
    return render(request, "shops/wallet_steps.html", {
        "form": form, "shop": shop, "saved": "saved" in request.GET, "guide": guide(), "wallets": wallets,
        "wallet_page": links["wallet"], "community_email": community()["email"]})


def connect_wallet(request, shop, form):
    """The submit of the wallet forms: True when the address passed the checks and the register is made, a redirect when the
    shop changed meanwhile, None when the form must be shown again (with its errors)."""
    if request.method != "POST" or not form.is_valid():
        return None
    if too_many_shops(request, "wallet"):
        form.add_error(None, _("Too many shops in a short time: wait a few minutes."))
        return None
    address = form.cleaned_data["lightning_address"]
    problem = refusal(address, request.user.pk, seconds=LIGHTNING_SUBMIT_SECONDS)
    if problem:
        form.add_error("lightning_address", problem)
        return None
    # Written only if the shop is still as it was: something changed meanwhile (the association suspended it, made
    # it historic, a second submit connected it) and nothing is written, never a register for a shop that may not have one
    saved = Shop.objects.filter(register_kept(), pk=shop.pk, owner=request.user, lightning_address="",
                                suspended=False).update(lightning_address=address, lightning=True, awaiting_wallet=False)
    if saved:
        return True
    messages.error(request, _("Your shop changed while the wallet was being connected: reload the page and look at it again."))
    return redirect("shops")


def too_many_shops(request, kind):
    """Counts a shop (`kind` "shop") or a wallet connected ("wallet") that is about to be probed, per account, per /64
    and per /48, and says whether one of them went over. The guide connecting a wallet counts apart from the new shops: an
    evening of new shops behind one connection does not use up the wallets connected later, and the other way round. Each submit sends requests to a server chosen by whoever writes:
    a new IPv6 /64, or a new account, must not give a fresh count."""
    return (ratelimit.over_limit(f"{kind}-user:{request.user.pk}", SHOPS_PER_USER, SHOP_WINDOW)
            or ratelimit.over_address_limits(request, kind, limits.rate("shops_per_address_hour"), SHOPS_PER_NETWORK, SHOP_WINDOW))

# The platforms of a wallet in the guide (config.community.WALLET_PLATFORMS), as a merchant calls them
PLATFORM_NAMES = {"android": "Android", "ios": "iPhone", "windows": "Windows", "macos": "Mac", "linux": "Linux",
                  "web": gettext_lazy("in the browser")}


@not_indexed
@require_POST
def lightning_check(request):
    """The Lightning address of the new shop form, tried as soon as it is pasted or the field is left: the same rules
    and the same probe as on submit, which stays the real check. The page gets the tidy address, a state, our own
    message and at most a suggested address: nothing of what the provider answered."""
    if not request.user.is_authenticated:
        return JsonResponse({"state": "login"}, status=403)
    busy = JsonResponse({"state": BUSY, "message": _(
        "Too many checks in a short time. You can go on: the address is checked again when you create the shop.")},
        status=429, headers={"Cache-Control": "no-store"})
    if (ratelimit.over_limit(f"lncheck:{request.user.pk}", LIGHTNING_CHECKS_PER_USER, LIGHTNING_CHECK_WINDOW)
            or ratelimit.over_address_limits(request, "lncheck-ip", LIGHTNING_CHECKS_PER_IP, LIGHTNING_CHECKS_PER_NETWORK,
                                             LIGHTNING_CHECK_WINDOW)):
        return busy
    address = tidy_lightning_address(request.POST.get("address", "")[:MAX_ADDRESS_TYPED])
    answer = {"address": address, "provider": address.rpartition("@")[2] if "@" in address else ""}
    found = checked_live(address, LIGHTNING_CHECK_SECONDS, request.user.pk)
    if found == BUSY:
        return busy
    if isinstance(found, forms.ValidationError):
        answer.update(state=found.code or "shape", message=str(found.messages[0]))
    elif isinstance(found, Exception):
        raise found
    else:
        answer.update(state=found, message=explanation(found, address),
                      suggestion=suggestion(address) if found in (UNKNOWN_DOMAIN, NO_ANSWER) else None)
    return JsonResponse(answer, headers={"Cache-Control": "no-store"})


def shop_saved():
    return redirect(f"{reverse('shops')}?saved=1")


def needs_wallet(shop):
    """A shop of its owner with no register yet: a shop moved from another site, or a shop of the directory given to its
    owner. A historic shop takes one until REGISTER_AFTER_CLOSING after its closing day (shops/models.py); one the
    association suspended takes none. Once set, the owner changes the
    address from its own page (shops/address_change.py): this page never opens again for it."""
    return not (shop.lightning_address or shop.suspended) and keeps_register(shop)


def guide():
    return reverse("page", args=[GUIDE_PAGE]) if page_path(get_language(), GUIDE_PAGE).exists() and not private_page(get_language(), GUIDE_PAGE) else ""


@not_indexed
@login_required(login_url="shops")
def edit_shop(request, shop):
    shop = get_object_or_404(request.user.shops, pk=shop)
    form = ShopDetailsForm(request.POST or None, instance=shop)
    if request.method == "POST" and ratelimit.over_limit(f"edit-shop-user:{request.user.pk}", EDITS_PER_USER, OWNER_WINDOW):
        # Nothing saved; what was typed stays in the form
        form.add_error(None, _("You saved many times in a short while: wait a few minutes and try again."))
        return render(request, "shops/shop_edit.html", {"form": form, "shop": shop, "needs_wallet": needs_wallet(shop), "can_change_address": address_change.changeable(shop),
                                                         "waits_for_wallet": needs_wallet(shop) and waits_for_wallet(shop)},
                      status=429)
    if request.method == "POST" and form.is_valid():
        # Only the form's columns: a wallet connected meanwhile, or any field written by someone else, stays as it is
        form.save(commit=False).save(update_fields=ShopDetailsForm.Meta.fields)
        messages.success(request, _("Saved."))
        warn_about_vat(request, form)
        return redirect("shops")
    return render(request, "shops/shop_edit.html", {"form": form, "shop": shop,
                                                     "needs_wallet": needs_wallet(shop), "can_change_address": address_change.changeable(shop),
                                                     "waits_for_wallet": needs_wallet(shop) and waits_for_wallet(shop)})


def warn_about_vat(request, form):
    """A VAT number that looks wrong is saved as it was written; the owner is told, once, and the association decides. The
    number is not in the message: it is on the page the owner is going to."""
    why = form.doubt()
    if why:
        messages.warning(request, _("We saved your VAT number, but it may have a mistake. %(why)s") % {"why": why})


@not_indexed
@login_required(login_url="shops")
def shop_display(request, shop):
    """«What customers see» (shops/details.py): the owner turns on or off the website, the address, the phone, the email,
    the business name and the VAT number, and sees the shop's page as it will be. Without JavaScript the preview changes
    when the choices are saved; with it, as they are ticked (static/details.js)."""
    shop = get_object_or_404(request.user.shops, pk=shop)
    form = ShopDisplayForm(request.POST or None, instance=shop)
    if request.method == "POST" and form.is_valid():
        # Only the six ticks: a wallet connected meanwhile, or any field written by someone else, stays as it is
        form.save(commit=False).save(update_fields=ShopDisplayForm.Meta.fields)
        messages.success(request, _("Saved. This is what your customers see now."))
        return redirect("shop_display", shop=shop.id)
    rows = [{"name": name, "label": details.LABELS[name], "value": getattr(shop, name), "field": form[details.flag(name)],
             "page_only": name in details.PAGE_ONLY} for name in details.CHOICES]
    for row in rows:
        row["doubt"] = details.vat_doubt(row["value"]) if row["name"] == "vat_number" else ""
    return render(request, "shops/shop_display.html", {
        "shop": shop, "form": form, "rows": rows, "facts": details.rows(shop, details.shown(shop), preview=True),
        "missing": sum(not row["value"] for row in rows), "notices": list(messages.get_messages(request))})


CLAIMS_PER_IP = 20
CLAIM_WINDOW = 3600
CLAIM_CODE = re.compile(r"[A-Za-z0-9_-]{32}")


@not_indexed
@login_required(login_url="shops")
def claim_shop(request):
    """The one-time link of a shop moved from another site (shops/moving.py): the owner signs in here, with the
    wallet or the email, and takes the shop. The link works once, for the first account that confirms it; never for
    staff accounts, which sign in only from the admin. The code comes in the query and goes back in the form's body:
    never in the path, which the access logs write."""
    code = request.POST.get("code") or request.GET.get("code", "")
    if ratelimit.over_limit(f"claim:{ratelimit.client_ip(request)}", CLAIMS_PER_IP, CLAIM_WINDOW):
        return render(request, "shops/claim.html", {"busy": True}, status=429)
    digest = claim_digest(code) if CLAIM_CODE.fullmatch(code) else None
    shop = Shop.objects.filter(owner=None, claim_hash=digest).first() if digest else None
    if shop is None or not signs_in_here(request.user):
        return render(request, "shops/claim.html", {"gone": True}, status=404)
    if request.method == "POST":
        with transaction.atomic():
            # One Lightning address, one owner (shops/certified.py): checked under the lock, on the address as it is now
            locked = Shop.objects.select_for_update().filter(pk=shop.pk, owner=None, claim_hash=digest).first()
            if locked is not None and certified.address_taken(locked.lightning_address, request.user, locked):
                return render(request, "shops/claim.html", {"shop": shop, "code": code, "taken": ShopForm.TAKEN},
                              status=409)
            taken = Shop.objects.filter(pk=shop.pk, owner=None, claim_hash=digest).update(owner=request.user, claim_hash="")
        if not taken:
            return render(request, "shops/claim.html", {"gone": True}, status=404)
        messages.success(request, _("%(name)s is in your account now.") % {"name": shop.name})
        return redirect("shops")
    return render(request, "shops/claim.html", {"shop": shop, "code": code})


@not_indexed
@login_required(login_url="shops")
def delete_shop(request, shop):
    shop = get_object_or_404(request.user.shops, pk=shop)
    if request.method == "POST":
        shop.delete()
        messages.success(request, _("Shop deleted."))
        return redirect("shops")
    return render(request, "shops/shop_delete.html", {"shop": shop})


def spreadsheet_text(text):
    """A text that the spreadsheet does not mistake for a formula."""
    return f"'{text}" if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def money_text(value, currency):
    return f"{number_format(value, 2)} {currency}"


# How many staff registers turned off for good the owner's register page names at most (gone_tills)
GONE_TILLS = 500


@not_indexed
def register(request, shop, staff=None):
    """The register. The phone asks the shop's Lightning address for each invoice and checks the payment with the
    provider (shops/register.py, static/register.js); the site gives the page, and the sales stay on the phone. With `staff` the
    register of a person who works with the owner (shops/staff.py): its own addresses, no owner's links, and words that
    send refunds to the owner. Both say where the money goes («Paying to:»)."""
    shop = get_object_or_404(Shop.objects.filter(HAS_REGISTER), pk=shop)
    data = register_data(shop)
    # A historic shop's register ends REGISTER_AFTER_CLOSING after its closing day, when the guardian stops watching it.
    # A suspended shop keeps it: suspension is about the map and the lists, never the payments
    if data is None or not keeps_register(shop):
        raise Http404
    owner = not staff and request.user == shop.owner
    # The real till waits for the basic pass (shops/training.py), except the owner's quick start: the first sales up to a
    # small amount, counted on the phone
    quick = 0
    if training.locked(shop):
        quick = 0 if staff else training.quick_start(shop, request.user)
        if not quick:
            return render(request, "shops/register_locked.html", {"shop": shop, "owner": owner, "staff": staff}, status=403)
    if staff:
        # The key goes in the query and in a header, never in a path: the logs drop queries (shops/staff.py)
        urls = {"manifest": f"{reverse('staff_manifest', args=[staff.id])}?k={staff.key}",
                "worker": "" if staff.retired else reverse("staff_worker", args=[staff.id])}
        # Turned off, the register takes no new sale: it only shows how a sale already on its screen ends
        data = {**data, "retired": bool(staff.retired), "texts": {**data["texts"], **staff_texts()}}
        if data.get("chain_on"):
            # The shop's count of on-chain addresses (shops/chain_count.py): asked with the register's key
            data["chain_claim"] = {"url": reverse("staff_chain", args=[staff.id]), "key": staff.key}
    else:
        urls = {"manifest": reverse("register_manifest", args=[shop.id]), "worker": reverse("register_worker", args=[shop.id])}
        if owner and data.get("chain_on"):
            # The shop's count of on-chain addresses (shops/chain_count.py): only the signed-in owner asks it
            data = {**data, "chain_claim": {"url": reverse("register_chain", args=[shop.id]), "csrf": get_token(request)},
                    "explorer_save": reverse("register_explorer", args=[shop.id])}
        if owner:
            # The staff registers the owner turned off for good: the owner's phone forgets the Nostr keys it made for
            # them, and no longer takes their copies (static/register.js, docs/features/nostr-copies.md)
            from .staff import RETIRED_GRACE

            # The newest first: a phone that missed some learns those it most likely still holds; the oldest past the
            # bound were sent on earlier visits
            data = {**data, "gone_tills": [str(pk) for pk in shop.staff_registers.filter(
                retired__lte=timezone.now() - RETIRED_GRACE).order_by("-retired", "id").values_list(
                "id", flat=True)[:GONE_TILLS]]}
    if quick:
        data = {**data, "quick_start": {"cents": quick, "currency": data["currency"]},
                "texts": {**data["texts"], **quick_texts()}}
    # The practice register (shops/practice.py): the same screens, nothing real
    urls["practice"] = practice_url(shop, staff)
    response = render(request, "shops/register.html", {
        "shop": shop, "currency": community().get("currency", "EUR"), "owner": owner, "register": data, "staff": staff,
        "urls": urls, "ledger": f"{shop.id}{f':{staff.id}' if staff else ''}",
        # Whoever stands at the till sees where the money goes, on every screen of the register
        "paying_to": guard.paying_to(shop, whole=bool(staff) or request.user == shop.owner)})
    # Only the register talks to the shop's Lightning provider and to the rate sources: the other pages stay closed
    response.headers["Content-Security-Policy"] = csp_with_connect(connect_origins(data))
    # What the register's copy on the phone keeps as the register page (static/register.js, keepCopy): only this answer
    response.headers["X-Register"] = "1"
    return response


def practice_url(shop, staff=None):
    """Where the register links to its practice copy. Not for a register the owner turned off, and nowhere when the
    community has turned the practice register off (`features.practice`)."""
    if not features.enabled(features.PRACTICE):
        return ""
    if staff:
        return "" if staff.retired else f"{reverse('staff_practice', args=[staff.id])}?k={staff.key}"
    return reverse("practice", args=[shop.id])


def quick_texts():
    """The words of the quick start: the first sales of the owner's register before the training (shops/training.py)."""
    return {
        "quick_left": _("Quick start: {left} of {limit} left before the training."),
        "quick_over": _("This sale is {amount} and only {left} of the quick start is left. Type less, or finish the training to take any amount."),
        "quick_done": _("Quick start used up. Finish the training to keep taking payments."),
        "quick_currency": _("The quick start counts in {currency}: set that currency in the settings."),
    }


def staff_texts():
    """The register's words on a staff register: the wallet is the owner's, and so is where the money goes."""
    return {
        "to": "",
        "invoice": _("Asking the shop's Lightning wallet for the invoice..."),
        "no_answer": _("The shop's Lightning wallet does not answer: the register cannot charge."),
        "limits": _("The shop's Lightning wallet does not accept this amount: too high or too low."),
        "elsewhere": _("The shop's Lightning wallet answers from another address: the register cannot use it."),
        "bad_invoice": _("The shop's Lightning wallet sent a wrong invoice: do not use it."),
        "old_invoice": _("The shop's Lightning wallet sent an invoice that is old or already used for another sale: do "
                         "not use it. Try again in a moment."),
        "no_check": _("Careful: this register cannot see when this payment arrives. Hand over the goods only after "
                      "the owner sees the payment in their wallet, then confirm here."),
        "seen": _("The owner sees the payment in the wallet"),
        "confirmed": _("Marked as paid: the owner will check it"),
        "unchecked": _("Not checked: tell the owner"),
        "by_hand": _("Of these, {count} marked as paid: the owner checks them in the wallet."),
        "unchecked_count": _("Not checked: {count}. Tell the owner."),
        "late_count": _("Of these, {count} paid after they were cancelled: tell the owner."),
        "copy_now": _("Shift ended. Now download the encrypted copy and give it to the owner."),
    }


def register_worker(request, shop, keep=True, staff=False):
    """The register's service worker, served from the register's path: its scope is that, not the whole site. A register
    that cannot take payments any more (locked, turned off, an address that no longer works) must leave the phone: after
    a failure the copy would serve the old register, with the address from before. The browser checks it again at every
    opening."""
    shop = get_object_or_404(Shop.objects.filter(HAS_REGISTER), pk=shop)
    # The owner's quick start keeps the register (and its copy on the phone) as a shop with the pass does
    open_till = not training.locked(shop) or (not staff and training.quick_start(shop, request.user) > 0)
    name = ("register-sw.js" if keep and keeps_register(shop) and register_data(shop) and open_till
            else "register-sw-gone.js")
    script = (settings.BASE_DIR / "static" / name).read_text()
    return HttpResponse(script, content_type="text/javascript", headers={"Cache-Control": "no-cache"})


RECOVERY_LIFETIME = 15 * 60


def profile_for(email):
    """Exact comparison: for the database the uppercase of «ı» is «I», and `iexact` would mistake gmaıl.com for gmail.com."""
    return Profile.objects.annotate(lowered=Lower("email")).filter(lowered=email.lower()).select_related("user").first()


def login_state(email):
    """Changes at every sign-in, so that a link is worth once."""
    profile = profile_for(email)
    return str(profile.user.last_login) if profile else ""


def site_link(name, token):
    """Never from the Host header: it is sent by whoever asks for the link, and the link goes to someone else's mailbox."""
    return f"{settings.PUBLIC_URL}{reverse(name)}?{urlencode({'token': token})}"


def send_link(subject, intro, link, email):
    """True if the email left; False if the mail server did not take it or the email budget kept it back."""
    name = community()["name"]
    body = "\n\n".join([intro, link, _("If you did not ask for it, ignore this email.")])
    try:
        return bool(send_mail(f"{name}: {subject}", body, None, [email]))
    except (OSError, SMTPException):
        return False


@not_indexed
@no_store
def recovery(request):
    form = RecoveryForm(request.POST or None)
    target = local_path(request.POST.get("next") or request.GET.get("next", ""))
    sent = failed = busy = recent = budget = daily = False
    email = ""
    if request.method == "POST" and form.is_valid():
        email = form.cleaned_data["email"].lower()
        # First the IP address and its network, then the email: whoever already went over the limit counts no more.
        # Over a limit the page says so, it does not pretend to send: anyone can use up the limit of an email, and its
        # owner would wait for a mail that never comes. What it tells is only that someone asked a short time ago
        times = mailbudget.factor(request)
        if ratelimit.over_address_limits(request, "mail", 10 * times, 30 * times, RECOVERY_LIFETIME):
            busy = True
        elif ratelimit.over_limit(f"mail:{email}", 3, RECOVERY_LIFETIME):
            recent = True
        elif not mailbudget.visitor_allowed(email, times):
            budget = True
        elif mailbudget.sender_over(request, times):
            daily = True
            # Nothing goes to that mailbox: its own count of the last minutes stays as it was, or its owner would be
            # told that a link was already sent
            ratelimit.give_back(f"mail:{email}", RECOVERY_LIFETIME)
        else:
            token = signing.dumps({"email": email, "state": login_state(email), "next": target}, salt="recovery")
            failed = not send_link(_("Your link to sign in"), _("Open this link to sign in. It lasts 15 minutes and works once."),
                                   site_link("recovery_login", token), email)
            sent = not failed
            if failed:
                mailbudget.sender_give_back(request)
    # The address as written, with or without an account: whoever got it wrong notices, and nobody finds out who is signed up
    response = render(request, "shops/recovery.html", {"form": form, "sent": sent, "failed": failed, "busy": busy,
                                                         "recent": recent, "next": target, "email": email,
                                                         "budget": mailbudget.message() if budget else "",
                                                         "daily": mailbudget.sender_message() if daily else ""},
                      status=429 if busy or recent or daily else 503 if budget else 200)
    if busy or recent or daily:
        response["Retry-After"] = str(ratelimit.seconds_left(mailbudget.SENDER_WINDOW if daily else RECOVERY_LIFETIME))
    return response


@not_indexed
@no_store
def recovery_login(request):
    """The link opens a page with a button: the automatic checks of email do not use up the sign-in."""
    token = request.POST.get("token") or request.GET.get("token", "")
    try:
        data = signing.loads(token, salt="recovery", max_age=RECOVERY_LIFETIME)
    except signing.BadSignature:
        data = None
    if not data or data["state"] != login_state(data["email"]):
        return render(request, "shops/recovery.html", {"form": RecoveryForm(), "expired": True}, status=404)
    if request.method != "POST":
        # Whoever opens the link sees which account they are entering: a link forwarded by someone else would lead into theirs
        return render(request, "shops/link_confirm.html", {"token": token, "action": reverse("recovery_login"), "account": data["email"]})
    email = data["email"]
    profile = profile_for(email)
    if not profile and limits.refuse("accounts"):
        return render(request, "shops/recovery.html", {"form": RecoveryForm(), "full": limits.message()}, status=503)
    profile = profile or new_account(email)
    if not profile:
        return render(request, "shops/recovery.html", {"form": RecoveryForm(), "expired": True}, status=404)
    if not signs_in_here(profile.user):
        return render(request, "shops/recovery.html", {"form": RecoveryForm(), "staff": True}, status=403)
    login(request, profile.user, backend="django.contrib.auth.backends.ModelBackend")
    return redirect(local_path(data.get("next", "")) or "shops")


def new_account(email):
    """User and profile together or nothing. With a double click the second finds the first one's account: the email is unique."""
    try:
        with transaction.atomic():
            user = get_user_model().objects.create(username=f"email:{secrets.token_hex(16)}")
            return Profile.objects.create(user=user, email=email)
    except IntegrityError:
        return profile_for(email)


def save_recovery_email(request, profile, email):
    """A new email counts only after the confirmation from the link: removing the old one is immediate."""
    if email == profile.email:
        return
    if not email:
        profile.email = ""
        profile.save()
        return
    # Accounts are free: besides the account, the address and its network count, with the same counters as the
    # recovery form (both send mail from here), and so does the address written, which the recovery limits too
    times = mailbudget.factor(request)
    if (ratelimit.over_limit(f"mail:{request.user.pk}", 3, RECOVERY_LIFETIME)
            or ratelimit.over_address_limits(request, "mail", 10 * times, 30 * times, RECOVERY_LIFETIME)
            or ratelimit.over_limit(f"mail:{email}", 3, RECOVERY_LIFETIME)):
        messages.error(request, _("Too many emails in a short time: wait a few minutes and try again."))
        return
    if not mailbudget.visitor_allowed(email, times):
        messages.error(request, mailbudget.message())
        return
    if mailbudget.sender_over(request, times):
        ratelimit.give_back(f"mail:{email}", RECOVERY_LIFETIME)
        messages.error(request, mailbudget.sender_message())
        return
    token = signing.dumps({"user": request.user.pk, "email": email, "current": profile.email}, salt="confirm-email")
    if not send_link(_("Confirm your email"),
                     _("Open this link in the browser where you are signed in to the account that asked for it, to "
                       "confirm this email for that account. It lasts 15 minutes."),
                     site_link("confirm_email", token), email):
        mailbudget.sender_give_back(request)
        messages.error(request, _("The email could not be sent: try again in a few minutes."))
        return
    messages.success(request, _("We sent a confirmation link to {email}: the address changes when you click it.").format(email=email))


@not_indexed
@no_store
def confirm_email(request):
    token = request.POST.get("token") or request.GET.get("token", "")
    try:
        data = signing.loads(token, salt="confirm-email", max_age=RECOVERY_LIFETIME)
    except signing.BadSignature:
        data = None
    profile = Profile.objects.filter(user=data["user"]).first() if data else None
    if not profile or profile.email != data["current"]:
        return render(request, "shops/recovery.html", {"form": RecoveryForm(), "expired": True}, status=404)
    # Only the account that asked, signed in, confirms: another account could ask to confirm someone else's email, and
    # once that person pressed the button the email and every later recovery would be the other account's
    if request.user.pk != profile.user_id:
        return render(request, "shops/link_confirm.html", {"elsewhere": True}, status=403)
    if request.method != "POST":
        return render(request, "shops/link_confirm.html", {"token": token, "action": reverse("confirm_email"), "account": data["email"],
                                                            "confirming": True})
    owner = profile_for(data["email"])
    if not owner or owner.pk == profile.pk:
        profile.email = data["email"]
        profile.email_changed = timezone.now()
        try:
            # Two confirmations of the same email at the same time both pass the check: the database constraint decides
            with transaction.atomic():
                profile.save()
            return redirect("shops")
        except IntegrityError:
            pass
    return render(request, "shops/recovery.html", {"form": RecoveryForm(), "taken": True}, status=409)


def database_problems():
    """What is wrong, with stable names for monitoring: empty when the database answers."""
    try:
        Shop.objects.exists()
    except DatabaseError:
        return ["database"]
    return []


def health(request):
    """For external monitoring. The result is remembered for a short time: the page is public."""
    problems = cache.get_or_set("health", database_problems, HEALTH_MEMORY)
    return JsonResponse({"ok": not problems, "problems": problems,
                         "version": {"software": settings.BITMENTOR_VERSION, "site": settings.SITE_VERSION},
                         "mode": settings.SITE_MODE, "mode_since": settings.SITE_MODE_SINCE},
                        status=503 if problems else 200)


def nostr_names(request):
    names = dict(public_shops().exclude(nip05_name__in=("", "_")).exclude(nostr_pubkey="").values_list("nip05_name", "nostr_pubkey"))
    if settings.NOSTR_SECRET_KEY:
        names["_"] = public_key(settings.NOSTR_SECRET_KEY)
    response = JsonResponse({"names": names})
    response["Access-Control-Allow-Origin"] = "*"
    return response

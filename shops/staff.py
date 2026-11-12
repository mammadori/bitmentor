"""Registers for whoever works with the owner: each person gets their own link, with a name, turned off alone.

The owner's register (`/register/<shop>/`) is the shop's id, which is public, and it cannot be taken back. A staff register
has its own secret key, reaches a phone through a one-time code, and takes payments for the shop and nothing else. Like
the owner's register, it keeps its sales on its own phone: the site never receives them.

Turning a register off stops new payments: a sale already on its screen still shows how it ends, since turning a
register off must never hide a payment, and for RETIRED_GRACE the page still opens, for the end of the shift and the
encrypted copy for the owner. After that the old key opens only a page that says the register is gone (410). The copy
kept on the phone goes away.

The register is paired with the owner's phone: the code's QR, drawn on the owner's phone (static/staff-pair.js), carries
in the link's fragment, which never reaches the site, the Lightning address the owner saw, the key of the owner's
receive chain (…/0, or the path the owner set), who the shop is and the owner's preferences
(static/register-pairing.js). Every register of the shop takes the next index of that chain from one count on the site
(shops/chain_count.py), so its payments show in the owner's wallet. The staff register has no field to change any of it: a new code of the owner
pairs it again. The site keeps the list of registers, to turn one off.

The key and the one-time code never travel in a path, which gunicorn, Caddy and Django's error log write down: the
page and its manifest take the key in the query (`?k=`), which the logs drop, and the code arrives in the query and goes
back in the body of the form. The paths carry only the register's id, which opens nothing alone."""

import hmac
import re
from datetime import timedelta

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import Http404, HttpResponse
from django.middleware.csrf import get_token
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.http import require_POST

from config import features, limits, ratelimit

from . import training, views
from .models import HAS_REGISTER, StaffRegister
from .register import options, preferences, register_data, relays

KEY = re.compile(r"[A-Za-z0-9_-]{20,64}")
# Long enough to print the card and hand it over the next day; once used, or after this, the code opens nothing
CODE_LIFETIME = timedelta(hours=24)
REGISTER_CHANGES = 30
REGISTER_CHANGE_WINDOW = 3600
# Opening a code: whoever tries codes at random finds none (128 bits), the limit keeps the database quiet
OPENS_PER_IP = 30
OPEN_WINDOW = 600
# How long a register turned off still opens with its key: the sale on its screen, the copy of the shift for the owner
RETIRED_GRACE = timedelta(hours=24)


class StaffRegisterForm(forms.ModelForm):
    class Meta:
        model = StaffRegister
        fields = ["name"]
        labels = {"name": gettext_lazy("Who will use it")}
        help_texts = {"name": gettext_lazy("A first name or a word you recognise, for example «Anna» or «evening shift». "
                                           "It shows at the top of the register's screen.")}

    def clean_name(self):
        name = " ".join(self.cleaned_data["name"].split())
        if not name:
            raise forms.ValidationError(_("Write a name."))
        return name


def staff_register(register, key):
    """The register with this id, if `key` is its key; 404 otherwise. Turned off registers too: the callers decide."""
    found = (StaffRegister.objects.select_related("shop").filter(shop__lightning_address__gt="").filter(pk=register).first()
             if KEY.fullmatch(key or "") else None)
    if not found or not hmac.compare_digest(found.key, key):
        raise Http404
    return found


def gone(staff):
    """Turned off longer than RETIRED_GRACE ago: the key opens nothing of the shop any more."""
    return bool(staff.retired and staff.retired <= timezone.now() - RETIRED_GRACE)


def gone_page(request, staff):
    return private(render(request, "shops/staff_gone.html", {"staff": staff}, status=410))


def page_key(request):
    return request.GET.get("k", "")


def page_url(staff):
    """The address of the register page, with its key: what the phone keeps, in the bookmarks and on the home screen."""
    return f"{reverse('staff_register', args=[staff.id])}?k={staff.key}"


def private(response):
    """The key is in the address: it stays out of the Referer of other sites, the caches and the search engines. Not
    no-referrer: with it the browser sends «Origin: null» with the forms, and Django's CSRF check refuses them."""
    response["Referrer-Policy"] = "same-origin"
    response["Cache-Control"] = "no-store"
    response["X-Robots-Tag"] = "noindex, nofollow"
    return response


def new_code(register):
    register.code = StaffRegister._meta.get_field("key").default()
    register.code_until = timezone.now() + CODE_LIFETIME
    register.save(update_fields=["code", "code_until"])


def pairing_data(shop, register, url):
    """What the owner's phone needs to draw the pairing code (static/staff-pair.js): where the code leads, the shop's
    Lightning address as the site has it (the page shows it whole, for the owner to check), the
    network and what the owner's register starts from. The owner's own settings, the on-chain key among them, are read
    on the owner's phone and never come here. `chain_on` says that on-chain at the register is on (an experimental
    feature, config/features.py): without it the code carries no on-chain key or address, whatever the phone holds."""
    data = register_data(shop) or {}
    start = preferences(shop)
    onchain = features.enabled(features.ONCHAIN)
    return {"url": url, "register": str(register.id), "shop_id": str(shop.id), "lightning": data.get("address", ""),
            "network": settings.NETWORK, "sources": options()["sources"],
            "currency": data.get("currency", ""), "discount": start["discount"], "confirm_above": start["confirm_above"],
            "shop": start["shop"], **relays(), **({"chain_on": True} if onchain else {}), "texts": pairing_texts(onchain)}


def pairing_texts(onchain=True):
    return {
        "lightning": _("Lightning: {address}"),
        **({"chain_key": _("On-chain: the receive addresses of your wallet, …/{path}, counted with your other registers. "
                           "The first one is {address}."),
            "chain_fixed": _("On-chain: your fixed address {address}."),
            "lightning_only": _("On-chain: no. To add it, set it in your register's settings on this phone, then come back here."),
            "preferences": _("Discount {discount}%, currency {currency}, a confirmation above {above}.")}
           if onchain else {"preferences": _("Discount {discount}%, currency {currency}.")}),
        "nostr": _("Sales sent to you on Nostr as they happen, encrypted to {owner}."),
        "pin_changed": _("The shop's Lightning address is {after}, but the register on this phone takes payments into "
                         "{before}. Open your register first and check it: no code until they are the same."),
        "failed": _("The code cannot be made on this browser: try another browser, or open this page on the phone with "
                    "your register."),
    }


def code_alive(register):
    return bool(register.code and register.code_until and register.code_until > timezone.now() and not register.retired)


def owned_shop(request, shop):
    return get_object_or_404(request.user.shops.filter(HAS_REGISTER), pk=shop)


def summary(shop):
    """The staff registers of a shop for the owner, with whether their one-time code still opens them."""
    registers = list(shop.staff_registers.all())
    for register in registers:
        register.code_alive = code_alive(register)
    return registers


@views.not_indexed
@login_required(login_url="shops")
def manage(request, shop):
    """The owner's page of the staff registers: add one, show the code that opens it on a phone, turn it off."""
    shop = owned_shop(request, shop)
    form = StaffRegisterForm(request.POST or None)
    if request.method == "POST":
        if ratelimit.over_limit(f"staff:{request.user.pk}", REGISTER_CHANGES, REGISTER_CHANGE_WINDOW):
            form.add_error(None, _("Too many changes in a short time: wait a few minutes."))
        elif limits.get("staff_registers_per_shop") and \
                shop.staff_registers.filter(retired__isnull=True).count() >= limits.get("staff_registers_per_shop"):
            form.add_error(None, _("Too many registers turned on: turn off the ones nobody uses any more."))
        elif form.is_valid():
            register = form.save(commit=False)
            register.shop = shop
            register.save()
            new_code(register)
            # The first day's «give the register to whoever works with you», done by doing it
            if not shop.register_shared:
                shop.register_shared = timezone.now()
                shop.save(update_fields=["register_shared"])
            return redirect("staff_code", shop.id, register.id)
    registers = summary(shop)
    return render(request, "shops/staff_registers.html", {
        "shop": shop, "form": form, "active": [r for r in registers if not r.retired],
        "retired": [r for r in registers if r.retired], "owner_register": reverse("register", args=[shop.id])})


@views.not_indexed
@login_required(login_url="shops")
def code(request, shop, register):
    """The one-time code that opens a staff register on a phone: on the screen to scan, or printed on a card."""
    shop = owned_shop(request, shop)
    register = get_object_or_404(shop.staff_registers.filter(retired__isnull=True), pk=register)
    if request.method == "POST":
        if ratelimit.over_limit(f"staff:{request.user.pk}", REGISTER_CHANGES, REGISTER_CHANGE_WINDOW):
            messages.error(request, _("Too many changes in a short time: wait a few minutes."))
        else:
            new_code(register)
        return redirect("staff_code", shop.id, register.id)
    context = {"shop": shop, "register": register, "alive": code_alive(register)}
    if context["alive"]:
        # Never from the Host header: whoever opens the panel from the onion must not hand out the onion
        url = f"{settings.PUBLIC_URL}{reverse('staff_open')}?c={register.code}"
        # The QR is drawn on the owner's phone, with the pairing in the link's fragment: the site never sees that part
        pairing = pairing_data(shop, register, url)
        if pairing.get("chain_on"):
            # The shop's count of on-chain addresses, which only the owner opens (shops/chain_count.py)
            pairing["chain_open"] = {"url": reverse("register_chain", args=[shop.id]), "csrf": get_token(request)}
        context.update(url=url, pairing=pairing)
    return private(render(request, "shops/staff_code.html", context))


@views.not_indexed
@login_required(login_url="shops")
@require_POST
def retire(request, shop, register):
    """Turns a staff register off at once: no new payments from it, the other registers untouched."""
    shop = owned_shop(request, shop)
    with transaction.atomic():
        register = get_object_or_404(shop.staff_registers.select_for_update(), pk=register)
        if not register.retired:
            register.retired = timezone.now()
            register.code = ""
            register.save(update_fields=["retired", "code"])
    messages.success(request, _("«{name}» is turned off: it takes no more payments. If the person needs a register "
                                "again, add a new one.").format(name=register.name))
    return redirect("staff_registers", shop.id)


@views.not_indexed
def open_register(request):
    """Where the one-time code leads. A page with a button, not a redirect: the previews of chats and of some camera
    apps open links by themselves, and would use up the code. The button puts the register on this phone."""
    code = request.POST.get("code", "") if request.method == "POST" else request.GET.get("c", "")
    found = (StaffRegister.objects.select_related("shop").filter(shop__lightning_address__gt="").filter(
        code=code, retired__isnull=True, code_until__gt=timezone.now()).first() if KEY.fullmatch(code) else None)
    if request.method == "POST":
        if ratelimit.over_limit(f"staff-open:{ratelimit.client_ip(request)}", OPENS_PER_IP, OPEN_WINDOW):
            return private(render(request, "shops/staff_open.html", {"busy": True}, status=429))
        if found:
            with transaction.atomic():
                # Two phones pressing at once: only the first gets the register
                used = StaffRegister.objects.filter(pk=found.pk, code=code).update(code="", opened=timezone.now())
            if used:
                return private(redirect(page_url(found)))
            found = None
    # The page keeps the pairing of the link's fragment for the register page (static/staff-open.js): only for a code
    # that opens a register, and only for that register
    return private(render(request, "shops/staff_open.html", {"register": found, "code": code},
                          status=200 if found else 404))


def register(request, register):
    """The register page of a staff register. Just turned off, it still opens: a sale on its screen goes on to the end,
    and the page says why no new payment can start. After RETIRED_GRACE, gone: nothing of the shop."""
    staff = staff_register(register, page_key(request))
    if gone(staff):
        return gone_page(request, staff)
    # A register of someone new reads the three rules of the counter first (shops/training.py); one that has accepted
    # them once never stops for them again, since it could be in the middle of a sale
    if training.staff_rules_due(staff):
        return private(redirect(f"{reverse('staff_rules', args=[staff.id])}?k={staff.key}"))
    return private(views.register(request, staff.shop.id, staff=staff))


def manifest(request, register):
    staff = staff_register(register, page_key(request))
    if gone(staff):
        return private(HttpResponse(status=410))
    return private(views.register_manifest(request, staff.shop.id, start=page_url(staff)))


def worker(request, register):
    """No key: the browser asks for the script on its own. The script has nothing of the register, and for a register
    turned off it is the one that removes the copy kept on the phone."""
    staff = get_object_or_404(StaffRegister.objects.select_related("shop").filter(shop__lightning_address__gt=""), pk=register)
    return views.register_worker(request, staff.shop.id, keep=not staff.retired, staff=True)

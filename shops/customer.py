"""The customer at the till who has no wallet, or has one and does not know how to pay.

The sign's QR code opens the shop page (shops/views.py, `shop_detail`), and whoever reads it stands at the till. The
merchant knows nothing about Lightning and cannot help them. So the shop page leads to two paths, for anyone, with no
sign-in:

- "I don't have a wallet": three screens. Choose a wallet from the community's list (the same rules as for a merchant:
  open source, non-custodial and free, never a name written in the software), install it and make it, and get the
  first sats: from a person or a page of the community.
- "I have a wallet, but I'm not sure how to pay": what to scan, what to expect, and what to do when the wallet says
  error, in plain words, with generic pictures drawn by the server (shops/customer_art.py).

For the merchant, a sheet to print, "No wallet? Scan here", with a QR code that opens the first path.

Nothing here writes or asks for anything: the pages are only read, there is no form, no script and no request to any
other server, and nothing about the visitor is kept. The phone's make is looked at once, to show the wallets that run on
it first, and forgotten.
"""
from decimal import Decimal

import segno
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.cache import patch_vary_headers
from django.utils.text import slugify
from django.utils.translation import get_language
from django.utils.translation import gettext as _
from django.views.decorators.http import require_safe

from config.community import community, customer_links, shop_page_links

from . import customer_art, views
from .directory import WAITING_FOR_WALLET, waits_for_wallet
from .models import Shop

STEPS = (1, 2, 3)
# What a customer's bill is taken to be, to say in money what the discount is worth
EXAMPLE_BILL = Decimal(20)
CENT = Decimal("0.01")
# How the address of a test network starts, to tell a customer which one to copy: signet and testnet use tb1
ADDRESS_START = {"regtest": "bcrt1"}
# The phones the list can be narrowed to: the make is read from the browser once and not kept
DEVICES = ("android", "ios")


def payable(shop):
    """How the shop takes bitcoin (views.till_methods) if a customer can pay there now, else None. The same
    shops as the "How to pay here" box of the shop page: not a suspended one, not a historic one, not one that waits for
    its owner's wallet."""
    if shop.suspended or shop.closed_on or waits_for_wallet(shop):
        return None
    return views.till_methods(shop)


def device(request):
    """The phone the visitor holds, if the browser says: "android", "ios" or "" (anything else, a computer included)."""
    agent = request.headers.get("User-Agent", "")
    if "Android" in agent:
        return "android"
    if any(name in agent for name in ("iPhone", "iPad", "iPod")):
        return "ios"
    return ""


def wallet_keys(wallets):
    """A short key for each wallet, from its name, to carry the choice from one screen to the next. The same name twice
    or a name with no letters gets its position instead."""
    keys, used = [], set()
    for position, wallet in enumerate(wallets, 1):
        key = slugify(wallet["name"])[:30] or str(position)
        if key in used:
            key = f"{key}-{position}"
        used.add(key)
        keys.append(key)
    return keys


def usable(wallets, methods):
    """The wallets that can pay where the shop takes bitcoin: a shop that takes only one of the two methods needs a
    wallet that sends it. The community says what a wallet sends with `methods`; without it, both. A shop of which the
    site says only "bitcoin" (views.till_methods) gets them all."""
    needed = {"lightning", "onchain"} if methods in ("both", "bitcoin") else {methods}
    return [wallet for wallet in wallets if needed & set(wallet["methods"])]


def common(request, shop):
    """What every page of the path needs, or the response that sends the visitor to the shop page instead."""
    methods = payable(shop)
    if methods is None:
        return None, redirect("shop_detail", shop=shop.id)
    data = community()
    links = customer_links(get_language())
    wallets = usable(links["wallets"], methods)
    for wallet, key in zip(wallets, wallet_keys(wallets)):
        wallet["key"] = key
    context = {
        "shop": shop, "methods": methods, "network": settings.NETWORK, "test_network": settings.NETWORK != "mainnet",
        "address_start": ADDRESS_START.get(settings.NETWORK, "tb1"),
        "wallets": wallets, "first_bitcoin": links["first_bitcoin"], "community_email": data.get("email", ""),
        "wallet_page": shop_page_links(get_language())["wallet"], "deal": deal(shop, data.get("currency", "EUR")),
        "art": pictures()}
    return context, None


def pictures():
    """The drawings of customer_art, by name, for the templates: {{ art.store }}."""
    return {name: customer_art.picture(name) for name in customer_art.PICTURES}


def deal(shop, currency):
    """What the discount is worth on a 20 euro bill, in the community's currency: a reason to try, in money."""
    if not shop.discount:
        return None
    saving = (EXAMPLE_BILL * shop.discount / 100).quantize(CENT)
    return {"percent": shop.discount, "bill": views.money_text(EXAMPLE_BILL, currency),
            "saving": views.money_text(saving, currency)}


def answer(request, template, context, by_phone=False):
    """The page, kept out of the search engines (it is the same for every shop). Only the first screen, when the visitor
    has not chosen a phone, is told to caches to keep a copy per phone: its list starts with the wallets for the one the
    browser says it is."""
    response = render(request, template, context)
    response["X-Robots-Tag"] = "noindex, nofollow"
    if by_phone:
        patch_vary_headers(response, ["User-Agent"])
    return response


@require_safe
def hub(request, shop):
    """Where the sign's QR code leads for whoever has no wallet: the two ways in."""
    shop = get_object_or_404(Shop, pk=shop)
    context, rejected = common(request, shop)
    if rejected:
        return rejected
    return answer(request, "shops/customer.html", context)


@require_safe
def step(request, shop, number):
    """One of the three screens of the path for whoever has no wallet: choose, install and make, get the first sats."""
    if number not in STEPS:
        raise Http404
    shop = get_object_or_404(Shop, pk=shop)
    context, rejected = common(request, shop)
    if rejected:
        return rejected
    wallets = context["wallets"]
    chosen = next((wallet for wallet in wallets if wallet["key"] == request.GET.get("w")), None)
    # Screens 2 and 3 are about a wallet. With a list and no wallet chosen, back to the list; with no list at all, the
    # path goes on with "your wallet"
    if number > 1 and wallets and chosen is None:
        return redirect(reverse("customer_step", args=[shop.id, 1]))
    context.update(number=number, chosen=chosen, query=f"?w={chosen['key']}" if chosen else "",
                   progress=[{"number": item, "done": item < number, "current": item == number} for item in STEPS])
    by_phone = False
    if number == 1:
        phone = request.GET.get("on")
        if phone not in DEVICES + ("all",):
            phone, by_phone = device(request) or "all", True
        shown = [wallet for wallet in wallets if phone == "all" or not wallet["platforms"] or phone in wallet["platforms"]]
        context.update(
            phone=phone, shown=[{**wallet, "platform_names": ", ".join(str(views.PLATFORM_NAMES[name]) for name in wallet["platforms"])}
                                for wallet in shown],
            filters=[("android", views.PLATFORM_NAMES["android"]), ("ios", views.PLATFORM_NAMES["ios"]), ("all", _("All"))])
    return answer(request, "shops/customer_step.html", context, by_phone)


@require_safe
def help_page(request, shop):
    """For whoever has a wallet and does not know how to pay: the steps, what to expect, and what to do when the wallet
    says error."""
    shop = get_object_or_404(Shop, pk=shop)
    context, rejected = common(request, shop)
    if rejected:
        return rejected
    return answer(request, "shops/customer_help.html", context)


@views.not_indexed
@login_required(login_url="shops")
def card(request, shop):
    """An A4 sheet for the merchant to print and keep at the till: "No wallet? Scan here", with a QR code that opens the
    first screen of the path, in the language of the page. Only for a shop where a customer can pay."""
    shop = get_object_or_404(request.user.shops.exclude(WAITING_FOR_WALLET).filter(suspended=False, closed_on=None), pk=shop)
    url = f"{settings.PUBLIC_URL}{reverse('customer', args=[shop.id])}"
    return render(request, "shops/customer_card.html", {
        "shop": shop, "methods": views.till_methods(shop), "url": url,
        # White behind it, dark or light theme: a code on a dark page is not read, and a sheet is printed on white
        "qr": segno.make(url, error="m").svg_data_uri(scale=10, border=1, dark="#000", light="#fff"), "art": pictures(),
        "deal": deal(shop, community().get("currency", "EUR"))})

"""The practice register: the shop's own register, with the same screens and the same words, where nothing is real.

A merchant who knows nothing of Lightning should not learn the register with a customer waiting. Here a pretend
customer pays, does not pay, the connection drops or the phone switches off, and the register answers as the real one
does, saying what to do. A few exercises end with the real first day.

Nothing leaves the phone and nothing is written: the page is the real register (templates/shops/register.html, with
register.js unchanged), and static/practice.js, loaded first, gives it a pretend rate, invoice and payment check in the
browser. The page has no address to send sales to, and its CSP lets it talk to nobody. The code on the screen is a QR of
a sentence, not a payment request: no wallet can pay it. The pages ask nothing of any provider, and a shop without a
wallet can practise too.
"""
import segno
from django.http import Http404
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils.translation import gettext as _

from config import features
from config.community import community

from . import first_day
from . import staff as staff_registers
from . import views
from .models import Shop
from .register import options, texts as register_texts


def texts(staff):
    """What the panel of the pretend customer says after each move: what happened, and what the merchant does now. The
    two sentences of the on-chain exercise only while on-chain at the register, an experimental feature, is on."""
    found = all_texts(staff)
    if not features.enabled(features.ONCHAIN):
        del found["confused"], found["chain_ready"]
    return found


def all_texts(staff):
    return {
        "pay": _("The customer paid. Hand over the goods only when this screen shows the green tick and «Payment "
                 "received»: the customer's phone does not count."),
        "none": _("The customer leaves without paying. The register says «Payment expired»: nothing was charged, the "
                  "goods stay with you. In the real register it happens when the code expires."),
        "down": _("No connection, as when the shop's Wi-Fi drops. The register says so and keeps the sale: do not "
                  "cancel it and do not hand over the goods. Wait for the connection."),
        "up": _("The connection is back: the register asks at once how the sale went."),
        "offline": (_("No connection: the shop's Lightning wallet cannot be reached.") if staff else
                    _("No connection: your Lightning wallet cannot be reached.")),
        "phone": _("The phone switched off and you opened the register again: the sale comes back by itself and says "
                   "how it ended."),
        "confused": _("The customer looks at the code and does not know what to do: their wallet does not read "
                      "Lightning codes. Maybe it pays only on-chain: do not despair. Cancel this sale, type the amount "
                      "again and press «OK, on-chain»."),
        "chain_ready": _("An on-chain code: the customer's wallet pays it as an ordinary bitcoin payment. Wait for "
                         "«Payment received» here, as with Lightning."),
        "first": _("Type an amount and press OK: then the pretend customer can pay."),
        "ready": _("The customer has the code in front of them. Choose what they do."),
        "done": _("Exercise done: {name}."),
        "all": _("All the exercises are done. Now the real thing."),
    }


def context(request, shop, staff=None):
    owner = not staff and request.user.is_authenticated and request.user == shop.owner
    if staff:
        base = reverse("staff_practice", args=[staff.id])
        real = staff_registers.page_url(staff)
        back, back_text = real, _("Back to the register")
        next_url, next_text = real, _("Open the real register")
        next_note = _("The next sale is real. The first times, ask the owner to stay close.")
    else:
        base = reverse("practice", args=[shop.id])
        real = reverse("register", args=[shop.id]) if shop.has_register else ""
        back, back_text = (reverse("shops"), _("Your shops")) if owner else (real, _("Back to the register"))
        if owner and first_day.applies(shop):
            next_url, next_text = f"{reverse('shops')}#first-day-{shop.id}", _("Go to the first day")
            next_note = _("Now a real sale: open the register and pay yourself a small amount from another phone.")
        elif owner and not shop.has_register:
            next_url, next_text = reverse("connect_wallet", args=[shop.id]), _("Connect your wallet")
            next_note = _("To take real payments the shop needs its wallet: it takes a few minutes.")
        else:
            next_url, next_text = real, _("Open the real register")
            next_note = _("The next sale is real.")
    sentence = _("PRACTICE ONLY. This is not a payment: there is nothing to pay.")
    currency = community().get("currency", "EUR")
    # The practice code pays into nobody's address
    words = {**register_texts(), **(views.staff_texts() if staff else {}), "to": ""}
    return {
        "shop": shop, "currency": currency, "owner": owner, "staff": staff, "practice": True, "urls": {"practice": ""},
        # The register's own data, with no address to pay into; `chain_on` as the real register has it (shops/register.py),
        # for the exercise where the customer's wallet pays only on-chain
        "register": {"address": "", "domain": "", "currency": currency, "sources": [], "tolerance": options()["tolerance"],
                     "max_age": options()["max_age"], "texts": words,
                     **({"chain_on": True} if features.enabled(features.ONCHAIN) else {})},
        "ledger": f"practice:{base}",
        "back": back, "back_text": back_text, "next_url": next_url, "next_text": next_text, "next_note": next_note,
        "practice_data": {"qr": segno.make(sentence, error="m").svg_data_uri(scale=6, border=2), "text": sentence,
                          "texts": texts(staff)},
    }


@views.not_indexed
def practice(request, shop):
    """The owner's practice register. Open to whoever can open the real register (its link is enough), and to the
    owner of a shop with no wallet yet, still trading: the best moment to learn."""
    shop = get_object_or_404(Shop, pk=shop)
    # Without a register, only the owner of a shop still trading: a historic shop has nothing left to learn (the panel
    # shows the link on the same terms)
    if not shop.has_register and (shop.closed_on or not (request.user.is_authenticated and request.user == shop.owner)):
        raise Http404
    response = render(request, "shops/practice.html", context(request, shop))
    response["Cache-Control"] = "no-store"
    return response


def staff_practice(request, register):
    """The practice register of a staff register: its key, its words. Not for a register the owner turned off."""
    staff = staff_registers.staff_register(register, staff_registers.page_key(request))
    if staff.retired:
        raise Http404
    return staff_registers.private(render(request, "shops/practice.html", context(request, staff.shop, staff)))

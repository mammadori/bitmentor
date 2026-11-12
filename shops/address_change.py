"""The owner changes the Lightning address of a shop that already has a register, from the panel: the same shop, the same
page, the same sign (its code leads to the shop's page, /s/<id>/), no duplicate on the map.

Where the money goes is the most sensitive thing an account holds, so the change asks more than the other details:

- only the owner, from their own panel; the administration never changes it (shops/guard.py, `refuse_from_admin`);
- a sign-in of the last `FRESH` minutes in this browser (with the wallet, or with the email link, which is the email's
  confirmation): an open session left on a phone, or a stolen cookie, is not enough;
- the new address passes the same live check as the first one (`lnaddress.refusal`, LUD-16 and LUD-21) and the rule
  "one address, one owner" (`certified.address_taken`);
- at most one change every `settings.LIGHTNING_CHANGE_DAYS` days per shop: whoever took the access cannot keep moving it;
- the guardian writes it in its register (`AccountChange`) and tells the owner (email, the shop's chats) and the
  association (shops/guard.py, `destination_changed`); the panel says it for `NOTICE`;
- the guardian of the addresses (shops/lightning_watch.py) starts again from scratch on the new address, and the badge
  and the private part of the pages wait for it to see the new address work (`models.ADDRESS_PENDING`): the certificate
  and the approval themselves are kept, they belong to the owner, who did not change;
- the registers: the owner's phone remembers the address it takes payments into and stops until the owner confirms the
  new one; the staff registers stop until the owner gives them a new pairing code (static/register.js, `setUp`). The
  provider of the old address stays open to the registers for `PREVIOUS_OPEN` (`previous_domain`), so that the sales
  already shown on it are still recognised.
"""
import time
from datetime import timedelta
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.signals import user_logged_in
from django.db import transaction
from django.db.models import Q
from django.dispatch import receiver
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _

from config import ratelimit
from config.community import community

from . import lightning_watch
from .certified import address_taken, same_address
from .forms import WalletForm
from .guard import OWNER_CHANGE, short_address
from .lnaddress import refusal
from .models import Profile, Shop, is_demo, keeps_register, register_kept
from .register import lightning_parts

SESSION_KEY = "signed_in_at"
# How recent the sign-in in this browser must be
FRESH = timedelta(minutes=15)
# How long the old address's provider stays open to the registers: an invoice lasts minutes, a sale left waiting is
# looked at again for a day after its end (static/register.js, recheckCancelled)
PREVIOUS_OPEN = timedelta(days=3)
# How long the panel says it
NOTICE = timedelta(days=14)
# Tries of the form, by account: each sends a request to a server chosen by whoever writes
TRIES_PER_USER = 10
TRIES_WINDOW = 3600
SUBMIT_SECONDS = 12


@receiver(user_logged_in)
def stamp(sender, request=None, user=None, **kwargs):
    """Every sign-in, with the wallet or the email link, writes its time in the session: the change asks a recent one."""
    session = getattr(request, "session", None)
    if session is not None:
        session[SESSION_KEY] = int(time.time())


def signed_in_recently(request):
    when = request.session.get(SESSION_KEY)
    return isinstance(when, int) and 0 <= time.time() - when < FRESH.total_seconds()


def every():
    return timedelta(days=settings.LIGHTNING_CHANGE_DAYS)


def next_change(shop, now=None):
    """When the owner may change the address again, or None if now: `every()` after the last change, and after the
    last change of the account's recovery email (a stolen session could set its own email and sign in with it)."""
    times = [moment + every() for moment in (shop.lightning_changed, email_changed(shop.owner_id)) if moment]
    then = max(times, default=None)
    return then if then and then > (now or timezone.now()) else None


def email_changed(user_id):
    return Profile.objects.filter(user_id=user_id).values_list("email_changed", flat=True).first()


def changeable(shop):
    """A shop of the site with a register that still works: a suspended shop takes no new destination, nor does a historic
    one past REGISTER_AFTER_CLOSING."""
    return bool(shop.lightning_address and not shop.suspended and keeps_register(shop) and not is_demo(shop))


def apply(shop_pk, user, before, address, now=None):
    """Writes the new address if the shop is still as the owner saw it, and the pace allows it. True if written. The
    guardian sees the update (shops/models.py, ShopQuerySet) and does the rest."""
    now = now or timezone.now()
    with transaction.atomic():
        # One address, one owner, read again in the transaction that writes
        if address_taken(address, user) or next_change(Shop(owner_id=user.pk), now):
            return False
        mark = OWNER_CHANGE.set(True)
        try:
            return bool(Shop.objects.filter(register_kept(), pk=shop_pk, owner=user, lightning_address=before, suspended=False)
                    .filter(Q(lightning_changed__isnull=True) | Q(lightning_changed__lte=now - every()))
                    .update(lightning_address=address, lightning=True, awaiting_wallet=False, lightning_previous=before,
                            lightning_changed=now, **lightning_watch.BLANK))
        finally:
            OWNER_CHANGE.reset(mark)


def previous_domain(shop, now=None):
    """The domain of the address before the owner's last change, while the registers still need it (PREVIOUS_OPEN), when
    it is another one than today's: the sales already shown on the old address are checked with its provider."""
    if not shop.lightning_changed or not shop.lightning_previous:
        return ""
    if (now or timezone.now()) - shop.lightning_changed >= PREVIOUS_OPEN:
        return ""
    old, new = lightning_parts_of(shop.lightning_previous), lightning_parts(shop)
    if not old or (new and old[1] == new[1]):
        return ""
    return old[1]


def lightning_parts_of(address):
    return lightning_parts(Shop(lightning_address=address))


def panel(shop, now=None):
    """What the panel says about a change of the last NOTICE: None, or {"when", "before", "after", "pending"}."""
    if not shop.lightning_changed or (now or timezone.now()) - shop.lightning_changed >= NOTICE:
        return None
    return {"when": timezone.localtime(shop.lightning_changed), "before": short_address(shop.lightning_previous),
            "after": short_address(shop.lightning_address), "pending": shop.address_pending}


@login_required(login_url="shops")
def change(request, shop):
    """The page that changes the address: the form, after a recent sign-in; otherwise the way to sign in again."""

    shop = get_object_or_404(request.user.shops, pk=shop)
    if not shop.lightning_address:
        return redirect("wallet_steps", shop=shop.id)
    if not changeable(shop):
        messages.error(request, _("The Lightning address of a suspended shop, or of one closed more than a year ago, cannot be changed."))
        return redirect("edit_shop", shop=shop.id)
    before = shop.lightning_address
    wait = next_change(shop)
    fresh = signed_in_recently(request)
    form = WalletForm(request.POST if request.method == "POST" and fresh and not wait else None, instance=shop,
                      initial={"lightning_address": ""})
    status = 200
    if form.is_bound and ratelimit.over_limit(f"lnchange-user:{request.user.pk}", TRIES_PER_USER, TRIES_WINDOW):
        form.add_error(None, _("Too many tries in a short time: wait a few minutes."))
        status = 429
    elif form.is_bound and form.is_valid():
        address = form.cleaned_data["lightning_address"]
        if same_address(address).filter(pk=shop.pk).exists():
            form.add_error("lightning_address", _("This is already your shop's Lightning address."))
        else:
            problem = refusal(address, request.user.pk, seconds=SUBMIT_SECONDS)
            if problem:
                form.add_error("lightning_address", problem)
            elif apply(shop.pk, request.user, before, address):
                messages.success(request, _("Done: payments now go to {address}. Open each register again and confirm "
                                            "the new address.").format(address=address))
                return redirect("shops")
            else:
                messages.error(request, _("Your shop changed while you were saving: look at it again."))
                return redirect("edit_shop", shop=shop.id)
    sign_in = f"{reverse('shops')}?{urlencode({'next': reverse('change_address', args=[shop.id])})}"
    return render(request, "shops/address_change.html", {
        "form": form, "shop": shop, "fresh": fresh, "wait": wait, "days": settings.LIGHTNING_CHANGE_DAYS,
        "sign_in": sign_in, "minutes": int(FRESH.total_seconds() // 60), "community_email": community()["email"],
        "before": before}, status=status)

"""The guardian of the Lightning addresses: every shop with a register that takes Lightning has its address probed again
from time to time, the way the register will use it (`lnaddress.probe`, the same probe as when the shop was created),
so that a provider that changed, an address deleted or a wallet that stopped confirming payments is found by the
merchant and the association, not by a customer at the counter.

It only reads: it never changes where a shop's money goes and never talks to a payment server. Its findings stay on the shop
(`lightning_*` fields), a fault counts only after two failed probes in a row some time apart, and the merchant (email)
and the association (Telegram) hear about it only when the status changes. The register is never blocked by it."""
import logging
import socket
import threading
from datetime import timedelta
from smtplib import SMTPException
from time import monotonic
from urllib.parse import urlsplit

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import F, Q
from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.translation import gettext as _
from django.utils.translation import override

from config import ratelimit
from config.community import community
from config.network import resolve

from . import certified, lnaddress, public_cache, vacation
from .models import HAS_REGISTER, keeps_register, register_kept, LightningStatus, Profile, Shop, real
from .notifications import NotificationFailed, alert_name, telegram

logger = logging.getLogger(__name__)

# A working address is probed again after this long
CHECK_EVERY = timedelta(hours=6)
# After a failed probe, and while the address does not work, sooner: the fault is confirmed, or the repair seen, within
# about an hour. A second failure counts only this long after the first one, so a retry pressed twice in a minute or a
# provider restarting does not make a fault
FAILURE_GAP = timedelta(minutes=45)
FAILURES = 2
BROKEN = (LightningStatus.NO_ANSWER, LightningStatus.NO_CONFIRMATION)
# The changes worth telling someone
BROKE = "broke"
FIXED = "fixed"
# "Do not know": a provider answering 429 or 5xx, a run where most addresses failed at once, a probe from the panel past
# its time. It changes nothing on the shop: the next run tries again
UNSURE = "unsure"
# More than half of the shops of a run failing, out of at least this many, is the server's network or a big provider:
# nothing counts, and only the association hears about it
MASS_FAILURE_MIN = 4
# Against an address that goes up and down: at most this many emails to a merchant, and lines to the association, a day
ALERTS_PER_DAY = 4
# Whether this server resolves names at all (network_ok): seconds for each name, and for all of them
NAME_SECONDS = 4
NETWORK_SECONDS = 12
# The fields the probes write: saved alone, so a probe never overwrites what the merchant saved in the meantime
FIELDS = ["lightning_status", "lightning_since", "lightning_checked", "lightning_worked", "lightning_failures",
          "lightning_failing_since", "lightning_checked_address"]
BLANK = {"lightning_status": "", "lightning_since": None, "lightning_checked": None, "lightning_worked": None,
         "lightning_failures": 0, "lightning_failing_since": None, "lightning_checked_address": ""}
# Probes asked from the panel run in the web workers: a few at a time, the others hear "try again in a minute"
PANEL_PROBES = threading.BoundedSemaphore(2)
TELEGRAM_LINES = 20

assert lnaddress.NO_ANSWER == LightningStatus.NO_ANSWER and lnaddress.NO_CONFIRMATION == LightningStatus.NO_CONFIRMATION
NO_ANSWER_STATUS = LightningStatus.NO_ANSWER


def watched():
    """The shops whose Lightning address the register uses today: with a register and an address, not suspended. A
    historic shop keeps its register, and stays watched, until REGISTER_AFTER_CLOSING after its closing day."""
    return real(Shop.objects.filter(HAS_REGISTER)).filter(suspended=False).filter(register_kept())


def is_watched(shop):
    return bool(shop.has_register and not shop.suspended and keeps_register(shop))


def due(now):
    """The watched shops to probe now, the ones waiting the longest first: never probed, probed more than CHECK_EVERY
    ago, failing or broken and probed more than FAILURE_GAP ago, or with an address the fields are not about."""
    failing = Q(lightning_failures__gt=0) | Q(lightning_status__in=BROKEN)
    return (watched().filter(Q(lightning_checked__isnull=True) | Q(lightning_checked__lt=now - CHECK_EVERY)
                             | (failing & Q(lightning_checked__lt=now - FAILURE_GAP))
                             | ~Q(lightning_checked_address=F("lightning_address")))
            .order_by(F("lightning_checked").asc(nulls_first=True), "pk"))


def forget_unwatched():
    """A shop that is no longer watched (suspended, closed, without a Lightning address) keeps no status:
    /admin/ and preflight show only what holds today, and if it comes back it starts from scratch."""
    stale = real(Shop.objects.exclude(pk__in=watched().values("pk"))).exclude(
        lightning_status="", lightning_failures=0, lightning_checked=None, lightning_checked_address="")
    return stale.update(**BLANK)


def apply(shop, result, now):
    """Writes a probe's result on the shop and returns the change worth telling: BROKE, FIXED or None."""
    if shop.lightning_checked_address != shop.lightning_address:
        # Another address: what was found about the old one does not hold for this one
        for field, value in BLANK.items():
            setattr(shop, field, value)
        shop.lightning_checked_address = shop.lightning_address
    shop.lightning_checked = now
    if result is None:
        was = shop.lightning_status
        shop.lightning_worked = now
        shop.lightning_failures = 0
        shop.lightning_failing_since = None
        if was != LightningStatus.OK:
            shop.lightning_status = LightningStatus.OK
            shop.lightning_since = now
        return FIXED if was in BROKEN else None
    if shop.lightning_status in BROKEN:
        # Still broken: the latest reason, the same fault, nobody told again
        shop.lightning_status = result
        return None
    if not shop.lightning_failures:
        shop.lightning_failures = 1
        shop.lightning_failing_since = now
        return None
    if now - shop.lightning_failing_since < FAILURE_GAP:
        return None
    shop.lightning_failures += 1
    if shop.lightning_failures < FAILURES:
        return None
    shop.lightning_status = result
    shop.lightning_since = shop.lightning_failing_since
    return BROKE


def watch_probe(address):
    """lnaddress.probe as the guardian reads it: None when the register can get paid there, NO_ANSWER or
    NO_CONFIRMATION, UNSURE for a busy provider. A provider whose name no longer exists (UNKNOWN_DOMAIN, the live check
    of the form says it to whoever is typing) does not answer for the register either: the same fault, with the same
    two probes some time apart before it counts, the same texts and no new status to store."""
    result = lnaddress.probe(address, busy=UNSURE)
    if result == lnaddress.OK:
        return None
    return NO_ANSWER_STATUS if result == lnaddress.UNKNOWN_DOMAIN else result


def probe_shop(pk):
    """Probes one watched shop, without saving: (address, result, broken before), or None for a shop that is gone or not
    watched. A busy provider (429, 5xx) gives UNSURE."""
    shop = watched().filter(pk=pk).first()
    if shop is None:
        return None
    return shop.lightning_address, watch_probe(shop.lightning_address), shop.lightning_status in BROKEN


def probe_within(address, seconds, done=None):
    """The probe with a cap on all of it, name resolution included (the watchdog of lnaddress cuts a slow answer, not a
    slow DNS). Past `seconds` the answer is UNSURE, and the probe ends on its own limits in the background, unread; `done`
    is called when it really ends, to free its place."""
    box = {}

    def run():
        try:
            box["result"] = watch_probe(address)
        finally:
            if done:
                done()
    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(seconds)
    return box.get("result", UNSURE) if not worker.is_alive() else UNSURE


def same_day(before, after):
    """Whether two values of `lightning_worked` fall on the same day of the community's calendar (or are both empty)."""
    if before is None or after is None:
        return before is after
    return certified.local_day(before) == certified.local_day(after)


def record(pk, address, result):
    """Saves a probe's result on the shop, if it is still watched and still has that address: a probe of an address that
    changed meanwhile says nothing about the new one. Returns (shop, change, recorded). UNSURE records nothing."""
    if result == UNSURE:
        return watched().filter(pk=pk).first(), None, False
    with transaction.atomic():
        # Read again under the lock: another probe (the panel, a run by hand) may have saved in the meantime, and two
        # probes must not both tell the same change
        shop = watched().select_for_update().filter(pk=pk).first()
        if shop is None or shop.lightning_address != address:
            return shop, None, False
        worked = shop.lightning_worked
        change = apply(shop, result, timezone.now())
        if same_day(worked, shop.lightning_worked):
            # The map's bands count whole days: a probe that worked again today changes nothing the public list shows,
            # and the guardian's other columns are not on it (public_cache.NOT_PUBLIC)
            public_cache.same_list(shop)
        try:
            shop.save(update_fields=FIELDS)
        finally:
            # The mark is for this one save: post_save takes it, but a save that raises never gets there
            shop.__dict__.pop(public_cache.SAME_LIST, None)
    if result is None and not shop.certified_month:
        # An address that works may be the last thing the certificate waited for (shops/certified.py)
        certified.refresh(Shop.objects.filter(pk=shop.pk))
    if change and vacation.is_quiet(shop):
        # Closed for a while: the guardian keeps watching and the panel shows the state, but nobody is
        # told. The fault is said when the shop reopens (vacation.welcome_back, vacation.reopen_today)
        change = None
    return shop, change, True


def check(pk):
    """Probes one watched shop, saves what it found and tells the merchant by email when the status changes. Returns
    (shop, change); (None, None) for a shop that is gone or not watched. The association is told by the caller."""
    found = probe_shop(pk)
    if found is None:
        return None, None
    shop, change, _recorded = record(pk, found[0], found[1])
    if change:
        tell_owner(shop, change)
    return shop, change


def network_ok(shops):
    """Whether this server resolves names at all: the domains of a few watched shops, and Telegram. If none
    resolves, every address would look dead because of us, and the run is better skipped."""
    hosts = {address.rpartition("@")[2] for address in shops.values_list("lightning_address", flat=True)[:50]}
    hosts = sorted(hosts)[:5] + [urlsplit(url).hostname for url in (settings.TELEGRAM_API,) if url]
    # The domains are the shops' choice: each is asked within a bound (config.network.resolve), all of them within
    # NETWORK_SECONDS, so a name whose DNS never answers cannot hold the run
    deadline = monotonic() + NETWORK_SECONDS
    for host in filter(None, hosts):
        left = deadline - monotonic()
        if left <= 0:
            return False
        try:
            resolve(host, 443, seconds=min(left, NAME_SECONDS), proto=socket.IPPROTO_TCP)
            return True
        except (OSError, UnicodeError):
            continue
    return False


def allowed(kind, shop):
    """Counts an alert about a shop and says whether it is within ALERTS_PER_DAY."""
    return not ratelimit.over_limit(f"lightning-{kind}:{shop.pk}", ALERTS_PER_DAY, 86400)


def when(moment, now=None):
    """«today at 9:00», «yesterday at 18:30» or «on 4 November at 9:00», in the community's time zone. Written to sit in
    the middle of a sentence, after a verb: «it stopped answering today at 9:00», «checked on 4 November at 9:00»."""
    local = timezone.localtime(moment)
    # Hours and minutes, the same in every language: «9 a.m.» would end a sentence with two full stops
    time = date_format(local, "H:i")
    today = timezone.localdate(now) if now else timezone.localdate()
    if local.date() == today:
        return _("today at {time}").format(time=time)
    if local.date() == today - timedelta(days=1):
        return _("yesterday at {time}").format(time=time)
    return _("on {date} at {time}").format(date=date_format(local, "MONTH_DAY_FORMAT"), time=time)


def panel(shop):
    """What the merchant's panel says about the address: None, or {"broken", "status", "since", "checked", "customer"}."""
    if not is_watched(shop) or shop.lightning_checked_address != shop.lightning_address:
        return None
    if shop.lightning_status in BROKEN:
        # Never seen working: "it stopped" would not be true
        return {"broken": True, "status": shop.lightning_status, "since": when(shop.lightning_since),
                "checked": when(shop.lightning_checked), "customer": customer_text(shop),
                "never": shop.lightning_worked is None}
    if shop.lightning_status == LightningStatus.OK and shop.lightning_worked:
        return {"broken": False, "checked": when(shop.lightning_worked)}
    return None


def owner_email(shop):
    """The owner's email to get back in, confirmed with a link, or empty. Not the shop's public email: anyone can write
    any address there, and these emails would carry a name they chose to a stranger's inbox."""
    profile = Profile.objects.filter(user_id=shop.owner_id).first() if shop.owner_id else None
    return profile.email if profile else ""


def customer_text(shop):
    """What a customer at the counter meets, in the merchant's words."""
    if shop.lightning_status == LightningStatus.NO_CONFIRMATION:
        return _("Customers can pay and the money reaches your wallet, but the register keeps waiting and does not say "
                 "it is paid. Until this is fixed, check in your wallet that each payment arrived before the customer "
                 "leaves.")
    return _("The register cannot take Lightning payments: customers cannot pay you in bitcoin until it works again.")


def email_text(shop, change):
    """Subject and body of the email to the merchant, in the community's main language."""
    contact = community()["email"]
    values = {"shop": shop.name, "address": shop.lightning_address, "email": contact}
    if change == FIXED:
        subject = _("{shop}: Lightning payments work again").format(shop=alert_name(shop))
        body = [_("Good news: the Lightning address of {shop}, {address}, works again. Customers can pay you with "
                  "Lightning, and the register sees their payments.").format(**values),
                _("There is nothing to do.")]
    else:
        values["since"] = when(shop.lightning_since)
        if shop.lightning_status == LightningStatus.NO_CONFIRMATION:
            subject = _("{shop}: the register does not see Lightning payments arrive").format(shop=alert_name(shop))
            happened = (_("We tried the Lightning address of {shop}, {address}, twice, some time apart: it receives "
                          "payments, but it does not tell the register when one arrives.")
                        if shop.lightning_worked is None else
                        _("The Lightning address of {shop}, {address}, still receives payments, but {since} it stopped "
                          "telling the register when one arrives."))
            steps = [_("1. Open your wallet and check that your Lightning address is still exactly {address}."),
                     _("2. If your wallet changed it, or changed how it works, the register needs a Lightning address "
                       "that tells it when a payment arrives: change it from your shop's page in your panel, or write to "
                       "{email} and we help you.")]
        else:
            subject = _("{shop}: the register cannot take Lightning payments").format(shop=alert_name(shop))
            happened = (_("We tried the Lightning address of {shop}, {address}, twice, some time apart, and it did "
                          "not answer.")
                        if shop.lightning_worked is None else
                        _("The Lightning address of {shop}, {address}, stopped answering {since}. We tried it twice, "
                          "some time apart."))
            steps = [_("1. Open your wallet and find your Lightning address, usually under Receive."),
                     _("2. Check that it is still exactly {address}."),
                     _("3. If it is different, or your wallet no longer has one, change it from your shop's page in your "
                       "panel: the shop and its sign stay the same. If you need help, write to {email}.")]
        body = [_("What happened") + "\n" + happened.format(**values),
                _("What your customers see") + "\n" + customer_text(shop),
                _("What to do now") + "\n" + "\n".join(step.format(**values) for step in steps),
                _("We keep checking it, and we write to you when it works again.")]
    body.append(_("Your panel: {link}").format(link=f"{settings.PUBLIC_URL}{reverse('shops')}"))
    return f"{community()['name']}: {subject}", "\n\n".join(body)


def tell_owner(shop, change):
    email = owner_email(shop)
    if not email or not allowed("mail", shop):
        return False
    with override(settings.LANGUAGE_CODE):
        subject, body = email_text(shop, change)
    try:
        send_mail(subject, body, None, [email])
    except (OSError, SMTPException, ValueError):
        logger.error("Lightning address email not delivered for shop %s", shop.pk)
        return False
    return True


def tell_association(changes, mass=None):
    """One Telegram message to the association for the changes of a run: a provider that goes down for many shops at
    once makes one message, not one per shop. TELEGRAM_MODERATION_CHAT, the chat of the people who look after the site.
    `mass` is (failed, probed) for a run where most
    addresses failed at once and nothing was counted."""
    changes = [(shop, change) for shop, change in changes if change and allowed("telegram", shop)]
    if not (changes or mass) or not settings.TELEGRAM_MODERATION_CHAT:
        return
    with override(settings.LANGUAGE_CODE):
        lines = [_("Lightning addresses of the shops:")]
        if mass:
            lines.append(_("{failed} of {probed} addresses failed at the same time: probably the network of this server "
                           "or a big provider, not the shops. Nothing was counted and no merchant was told; the next "
                           "run tries again.").format(failed=mass[0], probed=mass[1]))
        for shop, change in changes[:TELEGRAM_LINES]:
            if change == FIXED:
                lines.append(_("{shop}: works again").format(shop=alert_name(shop)))
            elif shop.lightning_worked is None:
                lines.append((_("{shop}: does not confirm payments")
                              if shop.lightning_status == LightningStatus.NO_CONFIRMATION
                              else _("{shop}: does not answer")).format(shop=alert_name(shop)))
            elif shop.lightning_status == LightningStatus.NO_CONFIRMATION:
                lines.append(_("{shop}: stopped confirming payments {since}").format(
                    shop=alert_name(shop), since=when(shop.lightning_since)))
            else:
                lines.append(_("{shop}: stopped answering {since}").format(
                    shop=alert_name(shop), since=when(shop.lightning_since)))
        if len(changes) > TELEGRAM_LINES:
            lines.append(_("…and {count} more").format(count=len(changes) - TELEGRAM_LINES))
        link = f"{settings.PUBLIC_URL}{reverse('admin:shops_shop_changelist')}?lightning=broken"
        lines.append(_("Shops with a problem: {link}").format(link=link))
    try:
        telegram(settings.TELEGRAM_MODERATION_CHAT, "\n".join(lines))
    except NotificationFailed:
        logger.error("Lightning address alert to the association not delivered")

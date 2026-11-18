"""No shop goes on the map without an approval: `Shop.approved_on` is empty until someone of the community looks at the
shop and approves it, on /admin/today/ or with the action in /admin/. Until then the shop is off the maps, the lists,
the counts and OpenStreetMap, and its page shows as little as a suspended one's. Its register works from the first
minute: the approval is about what the public sees, never about the payments.

Approved by whoever brings them: the shops imported from a list (`import_shops`, with `external_id`, later taken by
their owner), those moved from another site (`manage.py move_shops`), those written in /admin/, the made-up ones of the
demo. A shop its owner writes on the site (/shops/new/) waits. A community can turn the wait off
(`features: {approval: false}`): new shops are then approved as they are saved, and the shops already waiting keep
waiting until someone approves them.

Some new shops look like a copy of a shop already on the site (shops/approval_signals.py): the same name nearby, the same
website or phone. Those wait for a person who says they checked: `approve` skips them unless `confirmed`, and the history
of an approval records the signals that were there."""
import logging
import threading
from typing import NamedTuple

from django.conf import settings
from django.contrib.admin.models import CHANGE, LogEntry
from django.core.cache import cache
from django.db import transaction
from django.urls import reverse
from django.utils import timezone, translation
from django.utils.translation import gettext as _

from config import features

from . import approval_signals
from .models import Shop

logger = logging.getLogger(__name__)


def enabled():
    return features.enabled(features.APPROVAL)


def waiting():
    return Shop.objects.filter(approved_on=None)


def stamp(shop, by):
    """Marks a shop being saved as approved, by `by` (a username, or how it came: "import", "move", "demo")."""
    if shop.approved_on is None:
        shop.approved_on, shop.approved_by = timezone.now(), by


def approve(shops, user, *, confirmed=False):
    """Approves the shops of a queryset that wait; each approval goes in the admin's history of the shop, with the codes of
    the signals the shop had (shops/approval_signals.py). A shop with a signal that asks for a check is skipped unless
    `confirmed`: a person said they looked (`held` tells which were skipped). Returns how many were approved."""
    return approve_or_hold(shops, user, confirmed=confirmed)[0]


class Outcome(NamedTuple):
    approved: int
    held: list
    # The signals could not be computed: `held` are all the waiting shops, not shops that look like copies
    failed: bool = False


def approve_or_hold(shops, user, *, confirmed=False):
    """`approve`, and the shops it skipped, with the signals computed once: an Outcome. Fails closed: when the signals
    cannot be computed, nothing is approved without `confirmed` (all are held, `failed`); with it, all are approved as
    before, and the history says the checks could not be run."""
    found = list(shops.filter(approved_on=None))
    failed = False
    try:
        signals = approval_signals.signals_of(found)
    except Exception:
        if not confirmed:
            logger.exception("approval: the signals could not be computed, so %d shops were not approved", len(found))
            return Outcome(0, found, True)
        logger.exception("approval: the signals could not be computed; the shops are approved as confirmed")
        signals, failed = {shop.pk: [] for shop in found}, True
    held_shops = [] if confirmed else [shop for shop in found if approval_signals.asks(signals[shop.pk])]
    if held_shops:
        # A trace for a caller that suddenly approves fewer: how many, never which
        logger.warning("approval: %d shops held, a signal asks a person to check them (shops/approval_signals.py)",
                       len(held_shops))
    count = 0
    for shop in found:
        if shop in held_shops:
            continue
        # Only if it still waits: two people approving at once write one history line
        if Shop.objects.filter(pk=shop.pk, approved_on=None).update(approved_on=timezone.now(),
                                                                       approved_by=user.get_username()):
            codes = approval_signals.codes(signals[shop.pk])
            message = f"Shop approved; signals: {', '.join(codes)}" if codes else "Shop approved"
            if failed:
                message = "Shop approved; the checks could not be run"
            LogEntry.objects.log_actions(user.pk, [shop], CHANGE, change_message=message, single_object=True)
            count += 1
    return Outcome(count, held_shops, failed)


def held(shops):
    """The shops of a queryset that wait and that `approve` skips without `confirmed`: a signal asks a person to check.
    When the signals cannot be computed, every waiting shop, as `approve` holds them all."""
    found = list(shops.filter(approved_on=None))
    try:
        signals = approval_signals.signals_of(found)
    except Exception:
        logger.exception("approval: the signals could not be computed")
        return found
    return [shop for shop in found if approval_signals.asks(signals[shop.pk])]


def lines(found):
    """The signals as lines of a message: each sentence, and after the first about a shop what to check."""
    return [line for signal in found for line in (signal.text, signal.advice) if line]


def suspicion(shop_id, name=str):
    """The signals of a waiting shop when one of them asks for a check, else []: a note alone (a namesake far away) is
    for the page, not for a message."""
    shop = Shop.objects.filter(pk=shop_id).first()
    found = approval_signals.signals(shop, name=name) if shop else []
    return found if approval_signals.asks(found) else []


def tell_moderators(shop):
    """Tells the people who look after the site that a shop waits, once the shop is saved, from a thread of its own: the
    owner's page waits neither for Telegram nor for the mail server, and a message that cannot go is a line in the log."""
    shop_id, name = shop.pk, shop.name

    def deliver(lines=()):
        from .management.commands.dashboard import site_label
        from .notifications import alert_text, deliver_to_moderators

        try:
            with translation.override(settings.LANGUAGE_CODE):
                text = _("A new shop waits for your approval: %(name)s") % {"name": alert_text(name)}
                link = f"{settings.PUBLIC_URL}{reverse('admin_dashboard')}#approve-{shop_id}"
                subject, message = str(_("New shop")), f"{site_label()}\n{text}\n{link}"
                # A shop that looks like a copy of another says so on its first line, with the shops' names only
                if lines:
                    heading = f"⚠ {_('Check before approving')}"
                    subject = f"{heading}: {subject}"
                    message = f"{heading}\n{site_label()}\n{text}\n" + "".join(f"- {line}\n" for line in lines) + link
                where, problems = deliver_to_moderators(subject, message)
            if where is None:
                logger.warning("new shop: the moderators were not told (%s)", "; ".join(problems))
        except Exception:
            logger.exception("new shop: telling the moderators failed")

    def start():
        # The signals are read here, in the request's own connection: the thread only talks to Telegram and the mail
        try:
            from .notifications import alert_text

            with translation.override(settings.LANGUAGE_CODE):
                found = lines(suspicion(shop_id, alert_text))
        except Exception:
            logger.exception("new shop: the approval signals could not be read")
            found = []
        threading.Thread(target=deliver, args=(found,), daemon=True).start()

    transaction.on_commit(start)


TOLD_SECONDS = 24 * 3600


def told_key(shop_id, code, other_id):
    """What an edit already told the moderators (watch_edit), for TOLD_SECONDS."""
    return f"approval-told:{shop_id}:{code}:{other_id}"


def watch_edit(before, shop):
    """An owner's edit of an approved shop (shops/views.py, edit_shop) that makes it look like a copy of another shop
    (a confirmation-level signal it did not have before the edit) is told to the moderators, once the edit is saved: an
    approved shop renamed and given another shop's website would otherwise pass. The shop stays on the map until a person
    acts: an honest edit never takes a shop away. The owner is told nothing. Nothing is computed when the edit changed
    none of what the signals read (approval_signals.WATCHED). `before` is a copy of the shop as it was."""
    if shop.needs_approval or not approval_signals.enabled():
        return
    if all(getattr(before, field) == getattr(shop, field) for field in approval_signals.WATCHED):
        return
    previous, shop_id = approval_signals.card(before), shop.pk

    def deliver(name, found):
        from .management.commands.dashboard import site_label
        from .notifications import alert_text, deliver_to_moderators

        try:
            with translation.override(settings.LANGUAGE_CODE):
                heading = f"⚠ {_('Check this shop')}"
                text = _("%(name)s, already on the map, was changed by its owner and now looks like a shop already on the "
                         "site.") % {"name": alert_text(name)}
                link = f"{settings.PUBLIC_URL}{reverse('admin:shops_shop_change', args=[shop_id])}"
                message = f"{heading}\n{site_label()}\n{text}\n" + "".join(f"- {line}\n" for line in found) + link
                where, problems = deliver_to_moderators(heading, message)
            if where is None:
                logger.warning("edited shop: the moderators were not told (%s)", "; ".join(problems))
        except Exception:
            logger.exception("edited shop: telling the moderators failed")

    def start():
        # Read in the request's own connection, after the save: the thread only talks to Telegram and the mail
        try:
            from .notifications import alert_text

            current = Shop.objects.filter(pk=shop_id).first()
            if current is None or current.needs_approval:
                return
            everything = approval_signals.others()
            accounts = approval_signals.owners([current])
            with translation.override(settings.LANGUAGE_CODE):
                # Every signal of before and after, not only the first shops a page shows: an edit that only puts the
                # others in another order announces nothing
                had = {(signal.code, signal.other_id) for signal in
                       approval_signals.signals_for(previous, everything, accounts, most=None) if signal.asks}
                now = approval_signals.signals_for(approval_signals.card(current), everything, accounts, alert_text,
                                                   most=None)
                gained = {(signal.code, signal.other_id) for signal in now
                          if signal.asks and (signal.code, signal.other_id) not in had}
                # Once a day for each shop, signal and other shop: an owner who goes back and forth between their own
                # name and a copied one sends one message, not one per round trip
                gained = {pair for pair in gained if cache.add(told_key(shop_id, *pair), True, TOLD_SECONDS)}
                if not gained:
                    return
                about = {other for _code, other in gained}
                found = lines([signal for signal in now if signal.other_id in about or signal.other_id is None])
        except Exception:
            logger.exception("edited shop: the approval signals could not be read")
            return
        threading.Thread(target=deliver, args=(current.name, found), daemon=True).start()

    transaction.on_commit(start)

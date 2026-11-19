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
of an approval records the signals that were there.

A waiting shop that is not for the map (a trial of the team's, a duplicate, something written by mistake) can be set
aside from /admin/today/ (`set_aside`): it stays not approved, so off the map exactly like a waiting shop, with its
register working, but it is no longer in `waiting()` and nobody is asked about it again. Its owner reads so in the panel.
It can be put back among the waiting ones (`put_back`), or approved all the same."""
import logging
import threading
from typing import NamedTuple

from django.conf import settings
from django.contrib.admin.models import CHANGE, LogEntry
from django.core.cache import cache
from django.db import connections, transaction
from django.urls import reverse
from django.utils import timezone, translation
from django.utils.translation import gettext as _

from config import features

from . import approval_signals, events
from .models import Shop, is_demo

logger = logging.getLogger(__name__)


def enabled():
    return features.enabled(features.APPROVAL)


def waiting():
    """The shops that wait for an approval and are asked about: not approved, not set aside."""
    return Shop.objects.filter(approved_on=None, set_aside_on=None)


def set_aside(shop, user):
    """Sets aside `shop`, if it still waits: not for the map, not asked about any more. It stays not approved. Only if
    nobody approved it or set it aside meanwhile, so two people clicking write one history line. Returns whether it did."""
    now, name = timezone.now(), user.get_username()[:150]
    if not Shop.objects.filter(pk=shop.pk, approved_on=None, set_aside_on=None).update(set_aside_on=now, set_aside_by=name):
        return False
    shop.set_aside_on, shop.set_aside_by = now, name
    LogEntry.objects.log_actions(user.pk, [shop], CHANGE, change_message="Shop set aside: not for the map",
                                 single_object=True)
    return True


def put_back(shop, user):
    """Puts a shop set aside back among the waiting ones: asked about again. Returns whether it did."""
    if not Shop.objects.filter(pk=shop.pk, approved_on=None, set_aside_on__isnull=False).update(set_aside_on=None,
                                                                                                 set_aside_by=""):
        return False
    shop.set_aside_on, shop.set_aside_by = None, ""
    LogEntry.objects.log_actions(user.pk, [shop], CHANGE, change_message="Shop put back among the waiting ones",
                                 single_object=True)
    return True


def stamp(shop, by):
    """Marks a shop being saved as approved, by `by` (a username, or how it came: "import", "move", "demo")."""
    if shop.approved_on is None:
        shop.approved_on, shop.approved_by = timezone.now(), by


def approve(shops, user, *, confirmed=False):
    """Approves the shops of a queryset that wait; each approval goes in the admin's history of the shop, with the codes of
    the signals the shop had (shops/approval_signals.py). A shop with a signal that asks for a check is skipped unless
    `confirmed`: a person said they looked (`held` tells which were skipped). Returns how many were approved. A shop set
    aside in the queryset is approved too, and is no longer set aside (approve_or_hold)."""
    return approve_or_hold(shops, user, confirmed=confirmed)[0]


class Outcome(NamedTuple):
    approved: int
    held: list
    # The signals could not be computed: `held` are all the waiting shops, not shops that look like copies
    failed: bool = False


def approve_or_hold(shops, user, *, confirmed=False, by=None):
    """`approve`, and the shops it skipped, with the signals computed once: an Outcome. Fails closed: when the signals
    cannot be computed, nothing is approved without `confirmed` (all are held, `failed`); with it, all are approved as
    before, and the history says the checks could not be run.

    `by` is for an approval made in the name of somebody who is no account: a team key in the volunteers' area
    (team/area.py), written `[team key N] name`. `user` is then None, `approved_by` is `by`, and nothing is written in the
    admin's history, which needs an account: the caller keeps its own record (team.TeamAction). A `by` that is empty or
    blank is refused, as is giving both or neither: every approval says who made it.

    Each shop really approved here sends `events.shop_approved(shop, by)` after the commit, `by` being what `approved_by`
    says; a held shop, or one somebody approved in the meantime, sends nothing. The shops approved as they are brought
    (`stamp`: imports, moves, the demo, the admin's add) never pass here and send no `shop_approved`.

    A shop set aside (`set_aside`) that is in `shops` is approved like any other: it is not approved, so it is there to
    approve, and the approval empties its set-aside fields. Nobody has to put it back first; a caller that wants only
    the shops asked about passes `waiting()`. Not with `by` (a team key): a shop set aside, even a moment before the
    write, is never approved in a team key's name."""
    if (user is None) == (by is None):
        raise ValueError("approve_or_hold: a user or `by`, not both")
    if by is not None and (not isinstance(by, str) or not by.strip()):
        raise ValueError("approve_or_hold: `by` must name who approves")
    name = by if user is None else user.get_username()
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
        # Only if it still waits: two people approving at once write one history line. In the name of a team key, only
        # if nobody set it aside meanwhile either: that is an administrator's decision
        now = timezone.now()
        still = Shop.objects.filter(pk=shop.pk, approved_on=None)
        if user is None:
            still = still.filter(set_aside_on=None)
        if still.update(approved_on=now, approved_by=name[:150], set_aside_on=None, set_aside_by=""):
            shop.approved_on, shop.approved_by = now, name[:150]
            shop.set_aside_on, shop.set_aside_by = None, ""
            events.send(events.shop_approved, Shop, shop=shop, by=shop.approved_by)
            # The owner hears of it after the commit, from a thread: never a receiver of `shop_approved`, which is the
            # communities' own
            tell_owner(shop)
            codes = approval_signals.codes(signals[shop.pk])
            message = f"Shop approved; signals: {', '.join(codes)}" if codes else "Shop approved"
            if failed:
                message = "Shop approved; the checks could not be run"
            if user is not None:
                LogEntry.objects.log_actions(user.pk, [shop], CHANGE, change_message=message, single_object=True)
            count += 1
    return Outcome(count, held_shops, failed)


def on_the_map(shop):
    """Whether an approved shop really is a place to pay on the map, as the public lists say (directory.payable_shops):
    listed, not suspended, with a position, not waiting for its wallet, not historic. One query."""
    from .directory import payable_shops

    return payable_shops().filter(pk=shop.pk).exists()


def sign_ready(shop):
    """Whether the printed sign can be offered: what shop_sign serves (not while the shop waits for its wallet), and
    only for a shop still trading and not suspended, whose page shows how to pay."""
    from .directory import waits_for_wallet

    return not waits_for_wallet(shop) and not shop.suspended and not shop.closed


def owner_texts(shop):
    """The notice of the approval in the active language: (subject, body) of the email and the Telegram text. Only what
    is true: «on the map» only for a shop that is there; otherwise «approved», and in one sentence what the owner can
    still do to put it there (the wallet, the position). The link to the public page and the sign only when they serve;
    the panel always. No Lightning address."""
    from config.community import community

    from .directory import waits_for_wallet
    from .notifications import alert_name, alert_text

    page = f"{settings.PUBLIC_URL}{reverse('shop_detail', args=[shop.pk])}"
    panel = f"{settings.PUBLIC_URL}{reverse('shops')}"
    shown, wallet = on_the_map(shop), waits_for_wallet(shop)
    headline = _("{shop} is on the map") if shown else _("{shop} is approved")
    said = [(_("Good news: the community approved {shop}. It is on the map now, and in the lists.") if shown
             else _("Good news: the community approved {shop}.")).format(shop=shop.name)]
    # «As soon as» only when that one thing is all that keeps the shop off the map; otherwise only «approved»
    no_position = shop.latitude is None or shop.longitude is None
    rest_fine = shop.listed and not shop.suspended and not shop.closed
    if not shown and rest_fine and wallet and not no_position:
        said.append(_("It goes on the map as soon as you add the wallet where the money arrives: your panel shows how."))
    elif not shown and rest_fine and no_position and not wallet:
        said.append(_("It goes on the map as soon as its position is in its details: you can add it from your panel."))
    lines = []
    if not wallet:
        lines.append(_("Its page, the one customers see: {link}").format(link=page))
    if sign_ready(shop):
        lines.append(_("You can print the sign now: its QR code opens that page. You find it in your panel, together "
                       "with the card for the till."))
    lines.append(_("Your panel: {link}").format(link=panel))
    subject = f"{community()['name']}: {headline.format(shop=alert_name(shop))}"
    body = "\n\n".join([" ".join(said), *lines])
    chat = "\n".join([f"{alert_text(community()['name'])}: {headline.format(shop=alert_name(shop))}", *said[1:], *lines])
    return subject, body, chat


def tell_owner(shop):
    """Tells the owner that `shop` was approved, once the approval is committed: by email to the confirmed recovery
    address (kind "merchant", counted in the email budget) and in the shop's own Telegram chats, each on its own (the
    guardian's rules: lightning_watch.owner_email, owner_chats). The approver's page waits for neither: the addresses are
    read in the request's connection, the sending happens in a thread (inline with MAIL_IN_BACKGROUND off, as in the
    tests), and whatever fails is a line in the log. An owner with no email and no chat is told nothing. Nothing at all
    while the approval is turned off, and nothing for a shop of the demo."""
    if not enabled() or not shop.owner_id or is_demo(shop):
        return
    shop_id = shop.pk

    def deliver(email, found, subject, body, chat):
        from config.mail import merchant_sender, send_mail

        from .notifications import NotificationFailed, Unreachable, telegram

        if email:
            try:
                if not send_mail(subject, body, merchant_sender(), [email], kind="merchant"):
                    logger.error("approval notice: email not sent for shop %s (kept back by the email budget, or "
                                 "taken by no backend)", shop_id)
            except Exception as error:  # noqa: BLE001 (the log, never the approver's page)
                logger.error("approval notice: email not delivered for shop %s: %s", shop_id, type(error).__name__)
        for place, where in enumerate(found, 1):
            try:
                if not telegram(where, chat):
                    logger.error("approval notice: chat %s of %s of shop %s refused it", place, len(found), shop_id)
            except NotificationFailed as error:
                logger.error("approval notice: not delivered to chat %s of %s of shop %s", place, len(found), shop_id)
                if isinstance(error, Unreachable):
                    break
            except Exception:
                logger.exception("approval notice: chat %s of shop %s", place, shop_id)

    def behind(*arguments):
        try:
            deliver(*arguments)
        finally:
            # The thread's own database connections (the email count): nobody else would close them
            connections.close_all()

    def start():
        # Read in the request's own connection, after the commit: the thread only talks to the mail server and Telegram
        try:
            from .lightning_watch import owner_chats, owner_email

            current = Shop.objects.filter(pk=shop_id).first()
            if current is None or current.needs_approval:
                return
            email, found = owner_email(current), owner_chats(current)
            if not (email or found):
                return
            with translation.override(settings.LANGUAGE_CODE):
                arguments = (email, found, *owner_texts(current))
        except Exception:
            logger.exception("approval notice: the owner's addresses could not be read")
            return
        if settings.MAIL_IN_BACKGROUND:
            threading.Thread(target=behind, args=arguments, daemon=True).start()
        else:
            deliver(*arguments)

    transaction.on_commit(start)


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

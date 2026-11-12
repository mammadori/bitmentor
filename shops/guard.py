"""The account of a shop's owner is watched: what whoever runs the site changes in it is written down, and said.

`config/guard.py` stops the staff from using the pages of a merchant. What it cannot stop is a change to an account
that is not staff: a `username` replaced with a key the staff controls (then the wallet signs in as that merchant, as
any merchant would), a shop handed to another account, the chat that receives a shop's notices pointed elsewhere. These
are seen here, and not left silent:

- the owner is told, by email (the recovery email) and in the Telegram chat of each of their shops;
- the association is told, in the moderation chat (TELEGRAM_MODERATION_CHAT);
- the change is written in `AccountChange`, a register that the admin shows and nobody edits there, and the owner's
  `Profile.account_changed` takes the date, for the panel's notice and for whoever needs it (`recently_changed`).

What is watched, for the owner of at least one shop: a User saved with another `password`, `is_staff`, `is_superuser`,
`is_active` or `username`; the groups and the permissions of the user; the user deleted; a shop that changes owner or
Telegram chat from the admin or from a command (the owner's own form is the site's way, not watched).

A sensitive action of the future can ask more care while the account is recent:

    if guard.recently_changed(request.user):   # days: RECENT; a different window: recently_changed(user, timedelta(...))
        ...ask the email link again, or wait

Today none does: the panel only says it (`notice`).

Where a shop's money goes (its Lightning address, `FIELDS`) is watched on every road: `save()`, `QuerySet.update()` and `bulk_update()` (`ShopQuerySet`), from a page of the
admin, a command or a shell. The owner's first connection of a wallet from the panel is written down and confirmed to the
owner alone ("you changed ..."). The owner's change of an address that existed (shops/address_change.py) is confirmed to
the owner the same way (email, the chats of their shops) and told to the association too; any other change is an alarm
to the owner and to the association. The notices carry the
Lightning address short, never whole. The administration cannot change these columns at all (`refuse_from_admin`).

What it does not see, honestly: a change that skips Django (SQL, `QuerySet.raw`, a dump loaded into the database), and everything done by
whoever has the shell or the database, who can also delete the register. This is a witness, not a lock: against them
only another person's eyes help (the moderation chat is read by the association, not by the one who made the change).
"""
import contextvars
import logging
import threading
import time
from datetime import timedelta
from smtplib import SMTPException

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.core.mail import send_mail
from django.db import transaction
from django.db.models.signals import m2m_changed, post_save, pre_delete, pre_save
from django.dispatch import receiver
from django.urls import reverse as reverse_url
from django.utils import timezone, translation
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from config.community import community
from config.guard import comes_from_admin, current_request

from .models import AccountChange, Profile, Shop, is_demo
from .notifications import NotificationFailed, alert_name, telegram

logger = logging.getLogger(__name__)
# Set by shops/address_change.py while it writes the owner's own change of an existing address
OWNER_CHANGE = contextvars.ContextVar("owner_change", default=False)
User = get_user_model()

WATCHED_USER = ("password", "is_staff", "is_superuser", "is_active", "username")
# The columns that decide where the money goes
FIELDS = ("lightning_address",)
WATCHED_SHOP = ("owner_id", "telegram_chat", *FIELDS)
# While the owner's panel says it, and while recently_changed() is true
RECENT = timedelta(days=14)
# A user with many shops writes to each chat once, and not to a hundred
MAX_CHATS = 5
# After this many seconds no new message is tried (see Delivery)
DELIVERY_SECONDS = 20

# What changed: the code stored, the words of the register (for whoever runs the site), the sentence for the owner
# (short, in everyday words, one thing that happened)
WORDS = {
    "password": (gettext_lazy("password"), gettext_lazy("A password was set or changed on your account.")),
    "is_staff": (gettext_lazy("access to the admin"),
                 gettext_lazy("Your account was given, or lost, access to the site's administration.")),
    "is_superuser": (gettext_lazy("all rights in the admin"),
                     gettext_lazy("Your account was given, or lost, all the rights over the site.")),
    "is_active": (gettext_lazy("switched on or off"), gettext_lazy("Your account was switched on or off.")),
    "username": (gettext_lazy("name it signs in with"),
                 gettext_lazy("The name your account signs in with was replaced by another one.")),
    "groups": (gettext_lazy("groups"), gettext_lazy("The rights of your account were changed.")),
    "permissions": (gettext_lazy("permissions"), gettext_lazy("The rights of your account were changed.")),
    "shop_owner": (gettext_lazy("owner of a shop"), gettext_lazy("A shop of yours was handed to another account.")),
    "shop_chat": (gettext_lazy("Telegram chat of a shop"),
                  gettext_lazy("The Telegram chat that receives the notices of a shop of yours was changed.")),
    "deleted": (gettext_lazy("account deleted"), gettext_lazy("Your account was deleted.")),
    "shop_lightning": (gettext_lazy("Lightning address of a shop"),
                       gettext_lazy("The Lightning address where a shop of yours gets paid was changed.")),
}
# The column of the destination and the code the register keeps for its change
DESTINATION_CODES = {"lightning_address": "shop_lightning"}
VIA = {via.value: via.label for via in AccountChange.Via}
# Who was told, in the register's words
CHANNELS = {"email": gettext_lazy("email"), "telegram": gettext_lazy("Telegram chat of the shop"),
            "association": gettext_lazy("chat of the association")}

_local = threading.local()


def words(codes, sentence=False):
    """The codes in words, in the order of WORDS and without repeating a sentence two codes share."""
    said = []
    for code, texts in WORDS.items():
        text = str(texts[1 if sentence else 0])
        if code in codes and text not in said:
            said.append(text)
    return said


def origin():
    """Where the change comes from: a page of the admin (and who is signed in there), a page of the site, or no request
    at all, as in a command or a shell."""
    request = current_request.get()
    if request is None:
        return "command", ""
    if comes_from_admin(request):
        user = request.user
        return "admin", user.get_username() if user.is_authenticated else ""
    return "site", ""


def recently_changed(user, within=RECENT):
    """True if whoever runs the site changed this account, or one of its shops' owner or chat, within `within`."""
    return bool(user and user.is_authenticated and Profile.objects.filter(
        user=user, account_changed__gte=timezone.now() - within).exists())


def advice(contact):
    """What the owner does: nothing if they know why, and otherwise tell the association at once."""
    if contact:
        return _("If you know why, you do not need to do anything. If you do not, write to {email} now.").format(email=contact)
    return _("If you know why, you do not need to do anything. If you do not, tell the association now.")


def notice(user):
    """What the panel says to an owner whose account changed in the last RECENT: the date and the sentences, or None."""
    since = timezone.now() - RECENT
    # The owner's own act (a wallet connected) is in the register, but it is not something that happened to the account
    rows = list(AccountChange.objects.filter(user=user, created__gte=since).exclude(via=AccountChange.Via.OWNER)
                .order_by("created"))
    if not rows:
        return None
    codes = {code for row in rows for code in row.changed.split(",")}
    return {"when": timezone.localtime(rows[-1].created), "what": words(codes, sentence=True),
            "advice": advice(community().get("email", ""))}


def pending():
    """The change being written in this thread's transaction, by user: a form of the admin saves the user and then the
    groups, and the owner gets one message, not two."""
    if not hasattr(_local, "pending"):
        _local.pending = {}
    return _local.pending


def record(user, codes, shops, chats, mark=True, deleting=False):
    """Writes the change down and, when the transaction commits, tells the owner and the association. `shops` are the
    shops it is about (their names go in the register), `chats` the Telegram chats of the owner's notices as they were
    before the change. Nothing for an account with no shop. The messages go after the commit and never fail the change:
    a change must not be lost because an email cannot go."""
    shops = list(shops)
    if not shops:
        return None
    codes = set(codes)
    chats = {chat for chat in chats if chat}
    entry = pending().get(user.pk)
    # The row is looked for in the database, with its time: after a rollback the entry is left behind, and the row is not
    if entry and not deleting and AccountChange.objects.filter(pk=entry["row"], created=entry["created"]).exists():
        entry["codes"] |= codes
        entry["chats"] |= chats
        AccountChange.objects.filter(pk=entry["row"]).update(changed=",".join(sorted(entry["codes"])))
        return entry["row"]
    via, actor = origin()
    names = ", ".join(sorted({shop.name for shop in shops}))
    # In a Telegram chat the shop's name is the owner's text, as everywhere in the alerts: one line, no web address
    plain = ", ".join(sorted({alert_name(shop) for shop in shops}))
    # A deleted user is not pointed to: the row would be left pointing at nobody the moment the deletion goes on
    row = AccountChange.objects.create(user=None if deleting else user, shops=names[:300], changed=",".join(sorted(codes)),
                                       actor=actor[:150], via=via)
    profile = Profile.objects.filter(user=user).first()
    if mark and not deleting:
        Profile.objects.update_or_create(user=user, defaults={"account_changed": row.created})
    # The key now: a user deleted has no pk left when the commit comes
    key = user.pk
    pending()[key] = {"row": row.pk, "created": row.created, "codes": codes, "chats": chats,
                      "email": profile.email if profile else "", "shops": names, "plain": plain}
    transaction.on_commit(lambda: deliver(key, row.pk), robust=True)
    return row.pk


class Delivery:
    """One round of messages: after DELIVERY_SECONDS no new attempt is made (each one has its own timeout, so the round
    ends a little later), and after Telegram fails to answer none is tried again: a Telegram that is down is down for
    the next chat too."""

    def __init__(self):
        self.deadline = time.monotonic() + DELIVERY_SECONDS
        self.telegram_down = False

    def may_try(self):
        return time.monotonic() < self.deadline

    def telegram(self, chat, text):
        """True only when Telegram took the message: refused (a wrong chat), no bot, no answer or no time are all False."""
        if self.telegram_down or not self.may_try():
            return False
        try:
            return telegram(chat, text) is True
        except NotificationFailed:
            self.telegram_down = True
            return False

    def email(self, address, subject, body):
        if not self.may_try():
            return False
        try:
            send_mail(subject, body, None, [address])
        except (OSError, SMTPException):
            return False
        return True


def attempt(row_pk, channel, function):
    """A channel on its own: whatever goes wrong in it (a text that cannot be made, a mail backend that raises something
    else) is the channel's failure, and the others are still tried. Only the kind of error is logged: never the text."""
    try:
        return bool(function())
    except Exception as error:
        logger.error("Account change %s: %s failed with %s", row_pk, channel, type(error).__name__)
        return False


def deliver(key, row_pk):
    """The messages, after the commit: a rolled back change says nothing. The association first (it is the witness the
    change cannot silence), then the owner by email, then the owner's chats. Each channel that got through, and each that
    did not, is written in the register."""
    entry = pending().pop(key, None)
    if not entry or entry["row"] != row_pk:
        return
    row = AccountChange.objects.filter(pk=row_pk).first()
    if row is None:
        return
    told, not_told = [], []
    delivery = Delivery()
    with translation.override(settings.LANGUAGE_CODE):
        name = community()["name"]
        sentences = words(entry["codes"], sentence=True)
        owner_line = _("Someone who runs the site changed your account on {site}:").format(site=name)
        advice_line = advice(community().get("email", ""))

        def association():
            by = _("By {actor}, from {via}.") if row.actor else _("From {via}.")
            link = f"{settings.PUBLIC_URL}{reverse_url('admin:shops_accountchange_changelist')}"
            text = " ".join([_("Account change: {shops}.").format(shops=entry["plain"]),
                             _("Changed: {what}.").format(what=", ".join(words(entry["codes"]))),
                             by.format(actor=row.actor, via=str(VIA[row.via])),
                             _("The register: {link}").format(link=link)])
            return delivery.telegram(settings.TELEGRAM_MODERATION_CHAT, text)

        def email():
            body = "\n\n".join([owner_line, "\n".join(f"- {line}" for line in sentences),
                                _("Shops on your account: {shops}.").format(shops=entry["shops"]), advice_line])
            return delivery.email(entry["email"], f"{name}: {_('your account was changed')}", body)

        def chats():
            text = "\n".join([owner_line, *(f"- {line}" for line in sentences), advice_line])
            chosen = sorted(entry["chats"])
            # Each chat is tried, and the channel counts as told only if all of them took it, the ones beyond the
            # limit (never written to) included
            reached = [delivery.telegram(chat, text) for chat in chosen[:MAX_CHATS]]
            return all(reached) and len(chosen) <= MAX_CHATS

        channels = [("association", association if settings.TELEGRAM_MODERATION_CHAT else None),
                    ("email", email if entry["email"] else None), ("telegram", chats if entry["chats"] else None)]
        for channel, function in channels:
            if function is None:
                # The association is always said: «not told» is a fact the register keeps. The owner's channels only if
                # the owner has them
                if channel == "association":
                    not_told.append(channel)
                continue
            (told if attempt(row_pk, channel, function) else not_told).append(channel)
    order = ["email", "telegram", "association"]
    AccountChange.objects.filter(pk=row_pk).update(told=",".join(sorted(told, key=order.index)),
                                                   not_told=",".join(sorted(not_told, key=order.index)))


def owned(user):
    return list(Shop.objects.filter(owner=user)) if user.pk else []


def owned_chats(shops):
    return sorted({shop.telegram_chat for shop in shops if shop.telegram_chat})


# --- Where a shop's money goes ------------------------------------------------------------------------------------------

def watch(function, *args):
    """What the guardian does about a destination change, which must never stop the change itself: the connection of a wallet
    is the merchant's, and the witness is not a lock. Whatever goes wrong in it (a database error, a text that cannot be
    made) is logged, without the values, and left in its own savepoint, so that the change and the transaction go on. The
    refusal of a write from /admin/ is not here: it is `refuse_from_admin`, called before anything is written."""
    try:
        with transaction.atomic():
            return function(*args)
    except Exception as error:
        logger.exception("The guardian could not watch a change of where a shop's money goes (%s)", type(error).__name__)
        return None


def refuse_from_admin():
    """The administration never changes where a shop's money goes, whoever signs in there and whichever module or action
    asks: the owner does, from the panel. Called before such a column is written (a save, an update)."""
    if origin()[0] == "admin":
        raise PermissionDenied("Where a shop's money goes is not changed from the administration: only its owner does, "
                               "from their panel.")


def destinations(pks):
    """{pk: {column: value}} of the destination columns of these shops, as the database has them now."""
    pks, found = list(pks), {}
    for start in range(0, len(pks), 500):
        for row in Shop.objects.filter(pk__in=pks[start:start + 500]).values("pk", *FIELDS):
            found[row["pk"]] = row
    return found


def destinations_changed(before, after):
    """The shops whose destination is not what it was: for the update() of a queryset (shops/models.py)."""
    for pk, old in before.items():
        new = after.get(pk)
        if new is not None and any(old[field] != new[field] for field in FIELDS):
            shop = Shop.objects.filter(pk=pk).first()
            if shop is not None:
                watch(destination_changed, shop, old, new)


def outside(shop):
    """A shop whose changes are nobody's business: one of the training site."""
    return is_demo(shop)


def short_address(value):
    """A Lightning address with its name cut after two letters: `an…@provider.example`. The provider stays: it says
    where the money goes, and is not a secret."""
    value = (value or "").strip()
    name, at, host = value.rpartition("@")
    if not at:
        return value[:2] + "…" if len(value) > 2 else value
    return f"{name[:2]}…@{host}" if len(name) > 2 else value


def paying_to(shop, whole=True):
    """What the till says: [{"kind", "label", "value"}] with the Lightning address, whole or, without `whole`, short
    (`short_address`): the plain link of a register opens for whoever knows the shop's number, which /shops.json gives to
    everybody. The short form only keeps the address off the screens: it is no secret from whoever opens the page, which
    carries it whole in the register's data (shops/register.py, register_data: the phone asks it for every invoice).
    Empty for a shop without one."""
    if not shop.lightning_address:
        return []
    return [{"kind": "lightning", "label": _("Lightning address"),
             "value": shop.lightning_address if whole else short_address(shop.lightning_address)}]


def destination_lines(before, after):
    """What changed, as [(code, old, new)] with the address short. The register has no value; the notices have these."""
    lines = []
    if before["lightning_address"] != after["lightning_address"]:
        lines.append(("lightning_address", short_address(before["lightning_address"]),
                      short_address(after["lightning_address"])))
    return lines


def destination_changed(shop, before, after):
    """A shop's destination was changed, by a save or an update, from wherever. Written in the register and said, after the
    commit: the owner's first connection of a wallet from the panel (`Via.OWNER`) is confirmed to the owner alone; any other
    change, a destination that existed replaced by the owner's own account included, is an alarm to the owner, in the chats
    of all their shops, and to the association. Returns the row's key, or None if nothing changed or
    the shop is not watched (`outside`)."""
    lines = destination_lines(before, after)
    if not lines or outside(shop) or shop.owner_id is None:
        return None
    codes = sorted(DESTINATION_CODES[field] for field, _old, _new in lines)
    via, actor = origin()
    request = current_request.get()
    visitor = getattr(request, "user", None) if request is not None else None
    # The owner's own act from a page of the site (the first connection of a wallet, or the change of the address from the
    # panel, shops/address_change.py) is confirmed to the owner as theirs. Only the FIRST connection stays the owner's
    # business alone: replacing a destination that existed is told to the association too, whoever's account asks, since
    # a stolen account would otherwise change it with a confirmation to a channel the thief may have changed first
    first = not before["lightning_address"]
    # A replacement counts as the owner's only from the one page made for it (address_change.apply marks it)
    own = bool(via == "site" and (first or OWNER_CHANGE.get()) and shop.owner_id and visitor is not None and visitor.is_authenticated
               and visitor.pk == shop.owner_id)
    owner = User.objects.filter(pk=shop.owner_id).first() if shop.owner_id else None
    chats = owned_chats(owned(owner)) if owner else []
    row = AccountChange.objects.create(user=owner, shops=shop.name[:300], changed=",".join(codes), actor=actor[:150],
                                       via=AccountChange.Via.OWNER if own else via)
    if owner and not own:
        Profile.objects.update_or_create(user=owner, defaults={"account_changed": row.created})
    profile = Profile.objects.filter(user=owner).first() if owner else None
    payload = {"row": row.pk, "own": own, "association": not (own and first), "lines": lines, "plain": alert_name(shop), "chats": chats,
               "email": profile.email if profile else ""}
    transaction.on_commit(lambda: deliver_destination(payload), robust=True)
    return row.pk


def destination_text(lines):
    """The lines of a notice: what changed, from what to what, in the active language."""
    names = {"lightning_address": _("Lightning address")}
    out = []
    for field, old, new in lines:
        out.append(f"{names[field]}: {old or _('nothing')} → {new or _('nothing')}")
    return out


def deliver_destination(payload):
    """The messages of a destination change, after the commit. The owner's own change goes to the owner (email, chats) as a
    confirmation; any other goes to the association first, then to the owner. Each channel is written in the register."""
    row_pk = payload["row"]
    row = AccountChange.objects.filter(pk=row_pk).first()
    if row is None:
        return
    told, not_told = [], []
    delivery = Delivery()
    with translation.override(settings.LANGUAGE_CODE):
        name = community()["name"]
        details = destination_text(payload["lines"])
        contact = community().get("email", "")
        if payload["own"]:
            owner_line = _("You changed where a shop of yours gets paid, on {site}:").format(site=name)
            closing = (_("If it was you, you do not need to do anything. If it was not, write to {email} now.")
                       .format(email=contact) if contact else
                       _("If it was you, you do not need to do anything. If it was not, tell the association now."))
            subject = f"{name}: {_('you changed where your shop gets paid')}"
        else:
            owner_line = _("Someone who runs the site changed where a shop of yours gets paid, on {site}:").format(site=name)
            closing = advice(contact)
            subject = f"{name}: {_('where your shop gets paid was changed')}"
        owner_text = "\n".join([owner_line, *(f"- {line}" for line in details), _("Shop: {shop}.").format(shop=payload["plain"]),
                                 closing])

        def association():
            by = _("By {actor}, from {via}.") if row.actor else _("From {via}.")
            link = f"{settings.PUBLIC_URL}{reverse_url('admin:shops_accountchange_changelist')}"
            text = " ".join([_("Destination change: {shop}.").format(shop=payload["plain"]),
                             _("Changed: {what}.").format(what="; ".join(details)),
                             by.format(actor=row.actor, via=str(VIA[row.via])),
                             _("The register: {link}").format(link=link)])
            return delivery.telegram(settings.TELEGRAM_MODERATION_CHAT, text)

        def email():
            return delivery.email(payload["email"], subject, owner_text)

        def chats():
            chosen = sorted(payload["chats"])
            reached = [delivery.telegram(chat, owner_text) for chat in chosen[:MAX_CHATS]]
            return all(reached) and len(chosen) <= MAX_CHATS

        channels = [("association", association if settings.TELEGRAM_MODERATION_CHAT else None),
                    ("email", email if payload["email"] else None), ("telegram", chats if payload["chats"] else None)]
        for channel, function in channels:
            if channel == "association" and not payload.get("association", not payload["own"]):
                continue  # the owner's own act is the owner's business: the register keeps it
            if function is None:
                if channel == "association":
                    not_told.append(channel)
                continue
            (told if attempt(row_pk, channel, function) else not_told).append(channel)
    order = ["email", "telegram", "association"]
    AccountChange.objects.filter(pk=row_pk).update(told=",".join(sorted(told, key=order.index)),
                                                   not_told=",".join(sorted(not_told, key=order.index)))


@receiver(pre_save, sender=User)
def user_before(sender, instance, raw=False, update_fields=None, **kwargs):
    instance._guard_before = None
    if raw or instance._state.adding or instance.pk is None:
        return
    if update_fields is not None and not set(update_fields) & set(WATCHED_USER):
        return
    instance._guard_before = User.objects.filter(pk=instance.pk).values(*WATCHED_USER).first()


@receiver(post_save, sender=User)
def user_after(sender, instance, created=False, raw=False, update_fields=None, **kwargs):
    before = instance.__dict__.pop("_guard_before", None)
    if created or raw or not before:
        return
    changed = [field for field in WATCHED_USER if before[field] != getattr(instance, field)]
    # Django itself rewrites the hash with a stronger one when someone signs in (check_password), and says so by saving
    # only `password`: the same secret, not a new one. Only when there was a usable hash: a password set where there was
    # none is always a change
    if (changed == ["password"] and update_fields is not None and set(update_fields) == {"password"}
            and before["password"] and not before["password"].startswith("!")):
        return
    if changed:
        shops = owned(instance)
        record(instance, changed, shops, owned_chats(shops))


def memberships(code, instance, action, reverse, pk_set):
    """Groups and permissions, from either side: `instance` is the user, or the group (the permission) with the users'
    keys in `pk_set`. A clear does not say who was in it: from the group's side it is read before it happens."""
    if reverse:
        if action == "pre_clear":
            users = list(instance.user_set.all())
        elif action in ("post_add", "post_remove") and pk_set:
            users = list(User.objects.filter(pk__in=pk_set))
        else:
            return
    elif (action in ("post_add", "post_remove") and pk_set) or action == "post_clear":
        users = [instance]
    else:
        return
    for user in users:
        shops = owned(user)
        record(user, [code], shops, owned_chats(shops))


@receiver(m2m_changed, sender=User.groups.through)
def groups_changed(sender, instance, action, reverse, pk_set, **kwargs):
    memberships("groups", instance, action, reverse, pk_set)


@receiver(m2m_changed, sender=User.user_permissions.through)
def permissions_changed(sender, instance, action, reverse, pk_set, **kwargs):
    memberships("permissions", instance, action, reverse, pk_set)


@receiver(pre_delete, sender=User)
def user_deleted(sender, instance, **kwargs):
    shops = owned(instance)
    record(instance, ["deleted"], shops, owned_chats(shops), mark=False, deleting=True)


@receiver(pre_save, sender=Shop)
def shop_before(sender, instance, raw=False, update_fields=None, **kwargs):
    instance._guard_before = None
    if raw:
        return
    written = set(FIELDS) if update_fields is None else set(FIELDS) & set(update_fields)
    if instance._state.adding:
        # A new shop is not a change. The administration does not give one a destination either
        if written and any(getattr(instance, field) for field in FIELDS):
            refuse_from_admin()
        return
    if update_fields is not None and not {"owner", "owner_id", "telegram_chat", *FIELDS} & set(update_fields):
        return
    instance._guard_before = Shop.objects.filter(pk=instance.pk).values(*WATCHED_SHOP).first()
    before = instance._guard_before
    if before and any(before[field] != getattr(instance, field) for field in written):
        refuse_from_admin()


@receiver(post_save, sender=Shop)
def shop_after(sender, instance, created=False, raw=False, update_fields=None, **kwargs):
    """A shop that changes owner or chat from the admin or from a command. The owner's own form (edit_shop) is a page of
    the site: not watched. A shop with no owner yet that gets one (an imported shop being claimed) is not a change of
    anybody's account. A change of where its money goes is watched on every road, the owner's own too (destination_changed)."""
    before = instance.__dict__.pop("_guard_before", None)
    if created or raw or not before:
        return
    written = set(FIELDS) if update_fields is None else set(FIELDS) & set(update_fields)
    watch(destination_changed, instance, {field: before[field] for field in FIELDS},
          {field: getattr(instance, field) if field in written else before[field] for field in FIELDS})
    via, _actor = origin()
    if via == "site":
        return
    old_owner = User.objects.filter(pk=before["owner_id"]).first() if before["owner_id"] else None
    if old_owner is None:
        return
    codes = []
    if before["owner_id"] != instance.owner_id:
        codes.append("shop_owner")
    if before["telegram_chat"] != instance.telegram_chat:
        codes.append("shop_chat")
    if codes:
        record(old_owner, codes, [instance], [before["telegram_chat"]])

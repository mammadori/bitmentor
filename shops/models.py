import hashlib
import logging
import secrets
import uuid
from datetime import timedelta

from django.conf import settings
from django.core.validators import MaxLengthValidator, MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models, transaction
from django.db.models.functions import Lower
from django.utils import dateformat, timezone
from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _


logger = logging.getLogger(__name__)


class LoginChallenge(models.Model):
    k1 = models.CharField(max_length=64, primary_key=True)
    created = models.DateTimeField(auto_now_add=True, db_index=True)
    linking_key = models.CharField(max_length=66, blank=True)

    @classmethod
    def new(cls):
        return cls.objects.create(k1=secrets.token_hex(32))


class Profile(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile")
    # Only to recover access if the wallet is lost: optional and never public
    email = models.EmailField(_("recovery email"), blank=True, db_index=True)
    # When whoever runs the site last changed this account or one of its shops (shops/guard.py): the password, the
    # staff rights, the sign-in name, the groups, the owner of a shop or the chat of its notices. Written only by the
    # watcher, never by a form. A sensitive action may ask for more care while it is recent: guard.recently_changed().
    account_changed = models.DateTimeField(null=True, blank=True, editable=False)
    # When the recovery email was last set or confirmed (shops/views.py, confirm_email): a Lightning address waits after
    # it (shops/address_change.py), since whoever holds an open session can set an email of theirs and sign in with it
    email_changed = models.DateTimeField(null=True, blank=True, editable=False)
    # The hash the account's password had when `manage.py temporary_password` marked it as temporary (a password handed
    # over in person). The password is temporary while `user.password` is this same string, and the staff member who signs in
    # with it is sent to change it (config/temporary.py). Another password is another hash: the mark stops holding on its own.
    # Never shown, never written by a form.
    # db_default: a release that comes back after the migration still inserts a Profile (it does not know the column)
    temporary_password = models.CharField(max_length=128, blank=True, default="", db_default="", editable=False)

    class Meta:
        constraints = [models.UniqueConstraint(Lower("email"), condition=~models.Q(email=""), name="unique_recovery_email")]


class AccountChange(models.Model):
    """The register of what whoever runs the site changed in the account of a shop's owner (shops/guard.py): what,
    when, by whom and who was told. Read only, also in the admin. Names of what changed, never their values: no password
    and no key is written here. It outlives the account and the shop: the names of the shops are copied."""
    class Via(models.TextChoices):
        """Where the change came from, as the words of «from {via}»: a page of /admin/, a page of the site, or no
        request at all (a command, a shell)."""
        ADMIN = "admin", _("the admin")
        SITE = "site", _("the pages of the site")
        COMMAND = "command", _("a command or the shell")
        # The owner's own act from the panel (a wallet connected): the register keeps it, and the owner is told it was them
        OWNER = "owner", _("the owner's panel")

    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                             related_name="account_changes", editable=False)
    shops = models.CharField(_("shops"), max_length=300, blank=True, editable=False)
    # What changed, as codes joined by commas: guard.WORDS says them in words
    changed = models.CharField(_("what changed"), max_length=200, editable=False)
    actor = models.CharField(_("who"), max_length=150, blank=True, editable=False)
    via = models.CharField(_("origin"), max_length=10, choices=Via.choices, editable=False)
    created = models.DateTimeField(_("when"), default=timezone.now, db_index=True, editable=False)
    # Who was told, and who could not be reached: email, telegram (the shop's chat), association (the moderation chat)
    told = models.CharField(max_length=100, blank=True, editable=False)
    not_told = models.CharField(max_length=100, blank=True, editable=False)

    class Meta:
        ordering = ["-created"]
        verbose_name = _("account change")
        verbose_name_plural = _("account changes")


DESCRIPTION_LENGTH = 1000


class RequestCount(models.Model):
    """A counter for request limits (config.ratelimit.over_limit), by name and time window. In the database: with the file cache
    two simultaneous requests read the same count, and cleaning a full cache also deleted counters at random. The key
    is a hash: the emails that go into the names are not left written."""
    key = models.CharField(max_length=64, unique=True)
    count = models.PositiveIntegerField(default=1)
    expires = models.DateTimeField(db_index=True)


# A reference to an OpenStreetMap element, as the site writes it (shops/osm.py): its type and its number
OSM_REF = r"^(node|way|relation)/[1-9][0-9]{0,15}$"


class LightningStatus(models.TextChoices):
    """What the guardian knows of a shop's Lightning address. Empty: not checked yet, or not watched."""
    OK = "ok", _("Works")
    NO_ANSWER = "no_answer", _("Does not answer")
    NO_CONFIRMATION = "no_confirmation", _("Does not confirm payments")


# The shops that have a register, as a filter: the same rule as Shop.has_register
HAS_REGISTER = ~models.Q(lightning_address="")
# A historic shop (`closed_on`) keeps its register, and may still connect a wallet or change its address, until this long
# after its closing day: the owner settles the last accounts (liquidation can take a year)
REGISTER_AFTER_CLOSING = timedelta(days=365)


def register_kept(today=None):
    """A filter for the shops whose register still works by date: not historic, or closed less than REGISTER_AFTER_CLOSING ago."""
    from django.utils import timezone

    today = today or timezone.localdate()
    return models.Q(closed_on__isnull=True) | models.Q(closed_on__gte=today - REGISTER_AFTER_CLOSING)


def keeps_register(shop, today=None):
    """register_kept for one shop already read."""
    from django.utils import timezone

    return shop.closed_on is None or shop.closed_on >= (today or timezone.localdate()) - REGISTER_AFTER_CLOSING


# The shops whose owner changed the Lightning address and whose new one the guardian has not yet seen working
# (shops/address_change.py): their certificate and the private part wait for it. The same rule as Shop.address_pending
ADDRESS_PENDING = (models.Q(lightning_changed__isnull=False)
                   & (~models.Q(lightning_checked_address=models.F("lightning_address"))
                      | models.Q(lightning_worked__isnull=True)))


class ShopQuerySet(models.QuerySet):
    """`update()` skips the signals, so the guardian (shops/guard.py) would not see a destination changed with it. This one
    reads the destination columns before and after, so that an update() or a bulk_update() written in a command or in a
    shell goes into the register like a save(), and refuses it from a page of /admin/. Like a save(), it also renews the
    public list of shops (admin actions, the Lightning guardian, the certificates). Anything else is as in Django."""

    def update(self, **kwargs):
        from . import public_cache

        if {"owner", "owner_id"} & set(kwargs):
            # The private part was approved for the owner who had the shop (shops/certified.py): never handed on
            kwargs.update(private_approved=None, private_approved_by="")

        count = self.guarded_update(**kwargs)
        if count and set(kwargs) - public_cache.NOT_PUBLIC:
            # Like a save(): the public list of shops is built again at the next request (shops/public_cache.py)
            public_cache.changed()
        return count

    def guarded_update(self, **kwargs):
        from . import guard

        if not set(kwargs) & set(guard.FIELDS):
            return super().update(**kwargs)
        guard.refuse_from_admin()
        with transaction.atomic():
            # What the guardian reads before and after never stops the update itself (guard.watch)
            pks = list(self.values_list("pk", flat=True))
            before = guard.watch(guard.destinations, pks)
            count = super().update(**kwargs)
            if before is not None:
                guard.watch(lambda: guard.destinations_changed(before, guard.destinations(pks)))
        return count


class Shop(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # Empty for imported shops, until the owner claims them
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.CASCADE,
                              related_name="shops")
    external_id = models.CharField(max_length=64, blank=True, db_index=True)
    name = models.CharField(_("name"), max_length=120)
    # A cap: the description goes into the map, /shops.json and Nostr events, and a million characters blocked them
    description = models.TextField(_("description"), blank=True, max_length=DESCRIPTION_LENGTH,
                                   validators=[MaxLengthValidator(DESCRIPTION_LENGTH)])
    address = models.CharField(_("address"), max_length=200, blank=True)
    latitude = models.FloatField(_("latitude"), null=True, blank=True,
                                 validators=[MinValueValidator(-90), MaxValueValidator(90)])
    longitude = models.FloatField(_("longitude"), null=True, blank=True,
                                  validators=[MinValueValidator(-180), MaxValueValidator(180)])
    city = models.CharField(_("city"), max_length=100, blank=True)
    website = models.URLField(_("website"), blank=True)
    email = models.EmailField(_("email"), blank=True)
    phone = models.CharField(_("phone"), max_length=40, blank=True)
    # The business behind the shop, as on its invoices. The association keeps them; customers see them only if the owner
    # turns them on (shops/details.py). Never in diagnose_shop, the logs or a Nostr event.
    # Every new column that cannot be null says its starting value in the database too (db_default, the same as the Python
    # default): the previous image, put back on a database this release migrated (deploy/release.sh), does not name them
    legal_name = models.CharField(_("business name"), max_length=200, blank=True, default="", db_default="")
    vat_number = models.CharField(_("VAT number"), max_length=32, blank=True, default="", db_default="")
    # What customers see of the details above, chosen by the owner on the page «What customers see» (shops/details.py).
    # The website and the address are shown until the owner says otherwise; the rest is hidden until the owner turns it on.
    # The name is always shown. Whoever runs the site sets them only for a shop that has no owner yet.
    show_website = models.BooleanField(_("show the website"), default=True, db_default=True)
    show_address = models.BooleanField(_("show the address"), default=True, db_default=True)
    show_phone = models.BooleanField(_("show the phone"), default=False, db_default=False)
    show_email = models.BooleanField(_("show the email"), default=False, db_default=False)
    show_legal_name = models.BooleanField(_("show the business name"), default=False, db_default=False)
    show_vat_number = models.BooleanField(_("show the VAT number"), default=False, db_default=False)
    discount = models.PositiveSmallIntegerField(_("discount for bitcoin payments (%)"), default=0,
                                                validators=[MaxValueValidator(100)])
    listed = models.BooleanField(default=True, help_text="The owner agrees to appear on maps and lists")
    lightning = models.BooleanField(default=False)
    # The owner says the till takes on-chain payments too: a sign for the map, nothing more. Where on-chain payments go
    # stays on the owner's phone (static/register.js), never here
    onchain = models.BooleanField(_("I take on-chain payments too"), default=False, db_default=False)
    # Where the money goes: public data, never a secret. A shop with an address has a register (`has_register`)
    lightning_address = models.CharField(_("Lightning address"), max_length=200, blank=True)
    nostr_pubkey = models.CharField(_("Nostr public key (hex)"), max_length=64, blank=True,
                                    validators=[RegexValidator(r"^[0-9a-f]{64}$", _("64 hexadecimal characters."))])
    nip05_name = models.SlugField(_("Nostr name"), max_length=60, blank=True)
    telegram_chat = models.CharField(_("Telegram chat for notifications"), max_length=40, blank=True, validators=[
        RegexValidator(r"^(-?[0-9]+|@[A-Za-z0-9_]{5,})$", _("A chat number or a @name."))])
    suspended = models.BooleanField(default=False, help_text="Hides from maps and lists, leaves the register alone")
    # Decided by the association, not by the owner: no form on the site shows it
    closed_on = models.DateField(null=True, blank=True, help_text=(
        "History: stays on the map with a different marker, does not count among the places to pay. "
        "A future date is fine too: it reads «closes on …». The register and the sign stay with the owner"))
    created = models.DateTimeField(auto_now_add=True)
    # Every column below that cannot be NULL has a db_default, not only a default: when a release does not answer, deploy/release.sh
    # puts the previous image back on the migrated database, and that image does not know these columns, so its INSERTs
    # leave them out (docs/releases.md, "Going back"). A new NOT NULL column needs a db_default for the same reason.
    # The first day (shops/first_day.py): the steps the merchant ticks, the first sale included, since the site sees no
    # sale. In the database, not in the browser: the shop is often set up on a computer and run from a phone, the ticks
    # follow the account, and the panel works without JavaScript.
    first_sale = models.DateTimeField(null=True, blank=True)
    sign_printed = models.DateTimeField(null=True, blank=True)
    register_shared = models.DateTimeField(null=True, blank=True)
    # The owner's own explorer (an Esplora or mempool API, https): the registers ask it before the community's. The site
    # never fetches it; the register page needs it for its content security policy (shops/register.py)
    own_explorer = models.CharField(max_length=200, blank=True)
    # A shop saved by its owner before having a wallet (the new shop form, "I do not have a wallet yet"): its page is
    # ready, but it has no register, and stays off the map and the lists until the owner adds the
    # wallet (views.wallet_steps), which makes the register like for any new shop. Never set or cleared by a form.
    awaiting_wallet = models.BooleanField(_("waiting for the owner's wallet"), default=False, db_default=False, editable=False)
    # Shops moved here from another bitmentor site (shops/moving.py, manage.py import_moved_shops): when they arrived,
    # and whether they had a register there. Such a shop has no register here until its owner connects a wallet, and
    # stays off the map until then (shops/directory.py). Written only by the import, never by a form.
    moved_in = models.DateTimeField(_("moved here on"), null=True, blank=True, editable=False)
    # The shop's id there: a second import of the same file finds it. Never reused as the id here, since the id opens
    # the register, and a register link handed out on the other site must not open this one.
    moved_from = models.CharField(_("id on the other site"), max_length=36, blank=True, editable=False, db_index=True, default="", db_default="")
    moved_register = models.BooleanField(_("had a register on the other site"), default=False, db_default=False, editable=False)
    # A moved shop waiting for its owner: the SHA-256 of the one-time link's code (claim_digest). The code itself is
    # only in the list of owners the import writes. Emptied when the owner takes the shop.
    claim_hash = models.CharField(max_length=64, blank=True, editable=False, db_index=True, default="", db_default="")

    # The guardian of the Lightning address (shops/lightning_watch.py): what the last probes found, read by the panel
    # and /admin/. Only written by the probes, never by a form: none of these changes where the money goes.
    lightning_status = models.CharField(_("Lightning address status"), max_length=20, choices=LightningStatus.choices,
                                        blank=True, editable=False, default="", db_default="")
    lightning_since = models.DateTimeField(_("Lightning status since"), null=True, blank=True, editable=False)
    lightning_checked = models.DateTimeField(_("Lightning address last checked"), null=True, blank=True, editable=False)
    lightning_worked = models.DateTimeField(_("Lightning address last worked"), null=True, blank=True, editable=False)
    # Failed probes in a row, each one at least FAILURE_GAP after the first: a hiccup of the provider is not a fault
    lightning_failures = models.PositiveSmallIntegerField(default=0, db_default=0, editable=False)
    lightning_failing_since = models.DateTimeField(null=True, blank=True, editable=False)
    # The address these fields are about: a new address starts from scratch
    lightning_checked_address = models.CharField(max_length=200, blank=True, editable=False, default="", db_default="")
    # The owner's last change of the Lightning address from the panel (shops/address_change.py): when, and the address before.
    # The date paces the changes and keeps the badge waiting for the guardian; the old address's provider stays open to
    # the registers for a while, so that the sales already shown on it are still recognised
    lightning_changed = models.DateTimeField(_("Lightning address changed by the owner"), null=True, blank=True, editable=False)
    lightning_previous = models.CharField(_("previous Lightning address"), max_length=200, blank=True, default="",
                                          db_default="", editable=False)
    # The certified shop (shops/certified.py), never written by a form: the Lightning address answers the guardian, and the
    # owner has the basic pass of the training. Outside the site only `certified` leaves, never the month.
    # The first calendar month in which all of it held: once certified, always certified (it is a fact of the past),
    # unless the association revokes it from /admin/, which also stops the site from certifying the shop again
    certified_month = models.DateField(_("certified since the month of"), null=True, blank=True, editable=False)
    certified_revoked = models.BooleanField(_("certificate revoked"), default=False, db_default=False, editable=False)
    # The private part of the pages (website/visibility.py) is opened to a certified shop's owner only by a person of the
    # association, by hand from /admin/: when and who (the username, kept even if that account goes). Empty: not approved
    private_approved = models.DateTimeField(_("private part approved on"), null=True, blank=True, editable=False)
    private_approved_by = models.CharField(_("private part approved by"), max_length=150, blank=True, default="",
                                           db_default="", editable=False)
    # The shop's entry on OpenStreetMap (shops/osm.py): "node/123", "way/123" or "relation/123". Set by the association in
    # /admin/, or by manage.py osm_sync when a shop and an element are the same place beyond doubt. Never by the owner, and
    # it has nothing to do with where the money goes. It lets the sync fill the details the shop has not written yet.
    osm_ref = models.CharField(_("OpenStreetMap element"), max_length=30, blank=True, default="", db_default="", validators=[
        RegexValidator(OSM_REF, _("Write it like node/123456, way/123456 or relation/123456."))],
        help_text=_("The shop's entry on OpenStreetMap, like node/123456 (the last number of its address on "
                    "openstreetmap.org). Empty: not linked."))
    # The last sync that found the linked element on OpenStreetMap
    osm_seen = models.DateTimeField(_("seen on OpenStreetMap"), null=True, blank=True, editable=False)
    # An element the reading found beyond doubt to be the same place, waiting for the association to confirm it in /admin/
    # (an action): nothing is read from it, nothing is written for it, until then. Anybody can put a place on OpenStreetMap
    # next to a shop with its name, so a match is only a proposal. Written only by manage.py osm_sync and the admin's actions.
    osm_proposed = models.CharField(_("OpenStreetMap element proposed"), max_length=30, blank=True, default="", db_default="", editable=False)
    # What the reading did to this shop, so that it is not done twice (shops/osm.py): the elements the association refused,
    # and for each field the value it filled or offered and what became of it. A field the owner emptied on purpose is not
    # filled again; a value the association declined is not offered again. Never shown to the owner.
    osm_notes = models.JSONField(default=dict, db_default={}, blank=True, editable=False)

    objects = ShopQuerySet.as_manager()

    class Meta:
        # The name is a Nostr identity: /.well-known/nostr.json publishes only one, even if the admin saves two
        constraints = [models.UniqueConstraint(Lower("nip05_name"), condition=~models.Q(nip05_name=""), name="unique_nip05_name",
                                               violation_error_message=_("This name is already taken.")),
                       # An element is one place: two shops on it would share the details written to the empty fields
                       models.UniqueConstraint(fields=["osm_ref"], condition=~models.Q(osm_ref=""), name="unique_osm_ref",
                                               violation_error_message=_("Another shop is already linked to this element."))]

    def __str__(self):
        return self.name

    def owner_changed(self):
        """True if this saved shop is about to get another owner than the one in the database."""
        if not self.pk or "owner" in self.get_deferred_fields():
            return False
        before = Shop.objects.filter(pk=self.pk).values_list("owner_id", flat=True).first()
        return before is not None and before != self.owner_id

    def save(self, *args, **kwargs):
        # The private part was approved for the owner who had the shop (shops/certified.py): another owner starts
        # without it. Read only for an approved shop, so the ordinary saves pay no query
        if self.private_approved is not None and "private_approved" not in self.get_deferred_fields() \
                and self.owner_changed():
            self.private_approved, self.private_approved_by = None, ""
            if kwargs.get("update_fields") is not None:
                kwargs["update_fields"] = {*kwargs["update_fields"], "private_approved", "private_approved_by"}
        # A shop with an owner waits for nobody: its one-time link stops working, also when /admin/ assigns it. A shop
        # loaded with only some fields never reads the others here: that would be one more query, maybe in a thread
        if "claim_hash" not in self.get_deferred_fields() and self.owner_id and self.claim_hash:
            self.claim_hash = ""
            if kwargs.get("update_fields") is not None:
                kwargs["update_fields"] = {*kwargs["update_fields"], "claim_hash"}
        super().save(*args, **kwargs)

    @property
    def has_register(self):
        """A shop with a Lightning address has a register: the phone asks the address for each invoice."""
        return bool(self.lightning_address)

    @property
    def public_details(self):
        """{detail: value} of what the owner shows and has filled in (shops/details.py): the one way the pages read them."""
        from .details import shown

        return shown(self)

    @property
    def public_place(self):
        """Where the shop is, as the map says it: the address if shown, otherwise the town."""
        from .details import place

        return place(self)

    @property
    def certified(self):
        """True for a certified shop, and always False when the community turned the badge off (`features.certified`,
        config/features.py): the pages, the map, /shops.json, and the private part of the pages all ask this.
        `certified_month` itself stays, so turning the badge on again brings every certificate back."""
        from config import features

        return features.enabled(features.CERTIFIED) and self.certified_month is not None and not self.address_pending

    @property
    def address_pending(self):
        """True while a Lightning address the owner changed has not been seen working by the guardian (ADDRESS_PENDING)."""
        return bool(self.lightning_changed and (self.lightning_checked_address != self.lightning_address
                                                or self.lightning_worked is None))

    @property
    def historic_note(self):
        """The phrase of a past closure in the page's language: "closes" up to and including that day, then "closed"."""
        if not self.closed_on:
            return ""
        # Translators: the closing date with the letters of Django's date filter, for example "j. F Y" in German
        day = dateformat.format(self.closed_on, gettext("j F Y"))
        if self.closed_on >= timezone.localdate():
            return gettext("Historic · closes on %(date)s") % {"date": day}
        return gettext("Historic · closed on %(date)s") % {"date": day}


def claim_digest(code):
    """The digest of a one-time link's code, as Shop.claim_hash keeps it."""
    return hashlib.sha256(code.encode()).hexdigest()


# The external id of the shops `manage.py demo_data` makes (shops/demo.py): a training site's shops have no store that
# exists and no Lightning provider that answers, so what reads the stores and the providers (the periodic commands) and
# what publishes the shops outside the site (Nostr) skips them
DEMO_PREFIX = "bitmentor-demo:"


def real(shops):
    """The shops of a queryset without the demo mark."""
    return shops.exclude(external_id__startswith=DEMO_PREFIX)


def is_demo(shop):
    return shop.external_id.startswith(DEMO_PREFIX)


def staff_key():
    return secrets.token_urlsafe(24)


class StaffRegister(models.Model):
    """A register the owner gives to one person who works with them: its own link, its own name, turned off alone.

    Whoever has the link can take payments for the shop and nothing else: the money goes where the owner's pairing code
    says (its Lightning address and the key of the shop's on-chain chain, never known here). The page says where it goes («Paying to:», to
    be seen at the till), and shows neither the settings nor the sales. The link is the key: it reaches a phone
    through a one-time code (`code`), so a photo of the code taken after it was used, or after `code_until`, opens
    nothing. Turned off (`retired`), the register takes no new payments, but a sale already on its screen still shows
    how it ends: turning a register off must never hide a payment in progress. Never deleted: the sales keep its name."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name="staff_registers")
    name = models.CharField(_("name"), max_length=40)
    key = models.CharField(max_length=64, unique=True, default=staff_key, editable=False)
    created = models.DateTimeField(auto_now_add=True)
    retired = models.DateTimeField(null=True, blank=True)
    # The one-time code that opens the register on a phone: empty once used
    code = models.CharField(max_length=64, blank=True, db_index=True, editable=False)
    code_until = models.DateTimeField(null=True, blank=True, editable=False)
    opened = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        ordering = ["retired", "-created"]

    def __str__(self):
        return self.name


class ChainCount(models.Model):
    """The next index of one on-chain chain of a shop, shared by all its registers (shops/chain_count.py). `chain` is a
    SHA-256 of the chain's extended key, never the key: the site cannot derive an address."""
    shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name="chain_counts")
    chain = models.CharField(max_length=64)
    next = models.PositiveBigIntegerField(default=0, db_default=0)
    changed = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["shop", "chain"], name="chain_count_once")]

    def __str__(self):
        return f"{self.shop} {self.chain[:8]}: {self.next}"


class DashboardDigest(models.Model):
    """The last morning summary sent by `manage.py dashboard --send`: the things to do it named (code and shop, no
    text) and when. One row: the next summary goes only when the list changes, or a week later as a reminder."""
    sent = models.DateTimeField()
    items = models.JSONField(default=list)


class Closure(models.Model):
    """A shop that closes for a while and opens again (shops/vacation.py): holidays, works, a bereavement or an illness.
    A day of the community's calendar, never a moment: `first_day` is the first closed day and `reopens_on` the first day
    open again, empty while the owner has not said (they reopen when they say so). The shop stays on the map and its
    register keeps working: nothing here touches the till or where the money goes. Past closures stay, since the icons
    of the map and the owner's path count only the days the shop was open (`vacation.open_days`)."""

    class Reason(models.TextChoices):
        HOLIDAY = "holiday", _("Holidays")
        WORKS = "works", _("Building works")
        PERSONAL = "personal", _("Personal reasons")
        OTHER = "other", _("Other")

    shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name="closures")
    first_day = models.DateField(_("closed from"))
    reopens_on = models.DateField(_("reopens on"), null=True, blank=True)
    reason = models.CharField(_("reason"), max_length=12, choices=Reason.choices, default=Reason.HOLIDAY)
    created = models.DateTimeField(auto_now_add=True)
    # The welcome back email or message went out (shops/vacation.py welcome_back), and the owner closed the card
    welcomed = models.DateTimeField(null=True, blank=True, editable=False)
    seen = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        ordering = ["first_day", "pk"]
        constraints = [models.CheckConstraint(condition=models.Q(reopens_on__isnull=True) | models.Q(reopens_on__gt=models.F("first_day")),
                                              name="closure_reopens_after_it_closes")]

    def __str__(self):
        return f"{self.shop_id}: {self.first_day} - {self.reopens_on or '…'}"

    def clean(self):
        from django.core.exceptions import ValidationError

        if self.first_day and self.reopens_on and self.reopens_on <= self.first_day:
            raise ValidationError({"reopens_on": gettext("The shop reopens after the day it closes.")})


class HelpRequest(models.Model):
    """A merchant asking the association for help from «Something wrong?» (shops/helpdesk.py): the shop, what the
    merchant chose as the symptom, a short line they wrote, how they want to be called back, and what the site knew about
    the shop at that moment (`summary`, written by the diagnosis: no secret, no key). It stays on the dashboard
    (/admin/today/) on top, until someone marks it solved in /admin/: `resolved` and `resolved_by`. Written only by the
    merchant's request and that action, never by a form of /admin/."""

    class Symptom(models.TextChoices):
        PAY = "pay", _("A customer cannot pay")
        WAITING = "waiting", _("A payment stays on «waiting»")
        OPEN = "open", _("The register does not open")
        PHONE = "phone", _("I lost my phone or changed phone")
        CHANGE = "change", _("Too much change given, or a customer paid too much")
        OTHER = "other", _("Something else")

    class Channel(models.TextChoices):
        """How the merchant wants to be called back, among what the shop already has. `page`: they wait on the panel."""
        EMAIL = "email", _("by email")
        TELEGRAM = "telegram", _("on Telegram")
        PHONE = "phone", _("by phone")
        PAGE = "page", _("they will look at their panel")

    class Delivery(models.TextChoices):
        """The message to the moderators' Telegram chat: taken, refused, not possible (no bot or no chat), or not
        delivered for now. Without «sent» the request is still on the dashboard."""
        SENT = "sent", _("sent")
        REFUSED = "refused", _("refused by Telegram")
        OFF = "off", _("no Telegram bot or chat")
        FAILED = "failed", _("not delivered")

    shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name="help_requests")
    # The owner who asked: after a change of owner the new one does not read the old one's requests
    asked_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                 related_name="help_requests", editable=False)
    symptom = models.CharField(_("symptom"), max_length=10, choices=Symptom.choices)
    # One line of plain text the merchant wrote: no control characters, no secret (shops/helpdesk.py cleans it)
    note = models.CharField(_("what the merchant wrote"), max_length=200, blank=True)
    channel = models.CharField(_("call back"), max_length=10, choices=Channel.choices, default=Channel.PAGE)
    created = models.DateTimeField(_("asked"), default=timezone.now, db_index=True)
    # The diagnosis at that moment, in the lines a helper reads (shops/diagnosis.py): empty while it is being made
    summary = models.TextField(_("what the site knew"), blank=True)
    delivery = models.CharField(_("message to the association"), max_length=10, choices=Delivery.choices, blank=True)
    resolved = models.DateTimeField(_("solved"), null=True, blank=True, db_index=True)
    resolved_by = models.CharField(_("solved by"), max_length=150, blank=True)

    class Meta:
        ordering = ["-created", "-pk"]
        verbose_name = _("help request")
        verbose_name_plural = _("help requests")

    def __str__(self):
        return f"{self.shop}: {self.get_symptom_display()}"


class OsmRun(models.Model):
    """One reading of OpenStreetMap by `manage.py osm_sync` (shops/osm.py): when, whether it worked, and what it found, as
    a report the association reads in /admin/ and the merchant's page draws on. `report` holds the links made, the
    fields filled, the new candidates, the doubtful matches, the shops OpenStreetMap says no longer accept bitcoin, the
    shops verified more recently there than here, and the changes proposed for OpenStreetMap, shop by shop. Nothing in it
    is a secret. The last twelve are kept."""
    created = models.DateTimeField(default=timezone.now, db_index=True)
    ok = models.BooleanField(default=True)
    # Why it did not work, in a line: no address, no body
    error = models.CharField(max_length=200, blank=True)
    report = models.JSONField(default=dict)
    # The last manage.py osm_upload: when and in which changeset
    uploaded = models.DateTimeField(null=True, blank=True)
    changeset = models.CharField(max_length=20, blank=True)

    class Meta:
        ordering = ["-created", "-pk"]
        verbose_name = _("OpenStreetMap reading")
        verbose_name_plural = _("OpenStreetMap readings")


class OsmRequest(models.Model):
    """A merchant asking the association to bring the shop's entry on OpenStreetMap up to date, or to put the shop there
    (shops/osm_views.py): «The association does it for me». Only a queue: the site never writes to OpenStreetMap by
    itself. One is open at a time for a shop; someone of the staff does it, by hand or with `manage.py osm_upload`, and
    marks it done in /admin/ (`done` and `done_by`). The merchant sees its state in the panel."""

    class Kind(models.TextChoices):
        ADD = "add", _("put the shop on OpenStreetMap")
        UPDATE = "update", _("update the shop's entry on OpenStreetMap")

    class State(models.TextChoices):
        OPEN = "open", _("waiting")
        DONE = "done", _("done")
        DECLINED = "declined", _("could not be done")

    shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name="osm_requests")
    # The owner who asked: after a change of owner the new one does not read the old one's requests
    asked_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                 related_name="osm_requests", editable=False)
    kind = models.CharField(_("what is asked"), max_length=10, choices=Kind.choices)
    created = models.DateTimeField(_("asked"), default=timezone.now, db_index=True)
    # The lines to write, as the site had them when the merchant asked: for the person who does it, no secret in them
    summary = models.TextField(_("what to write"), blank=True)
    state = models.CharField(_("state"), max_length=10, choices=State.choices, default=State.OPEN, db_index=True)
    # The message to the moderators' Telegram chat, as for the help requests
    delivery = models.CharField(_("message to the association"), max_length=10, choices=HelpRequest.Delivery.choices, blank=True)
    done = models.DateTimeField(_("done on"), null=True, blank=True)
    done_by = models.CharField(_("done by"), max_length=150, blank=True)
    # A line for the merchant when it could not be done: written by the staff, shown in the panel
    answer = models.CharField(_("answer to the merchant"), max_length=200, blank=True)

    class Meta:
        ordering = ["-created", "-pk"]
        constraints = [models.UniqueConstraint(fields=["shop"], condition=models.Q(state="open"), name="osm_request_one_open")]
        verbose_name = _("OpenStreetMap request")
        verbose_name_plural = _("OpenStreetMap requests")


class Trainee(models.Model):
    """What the site keeps of a merchant's training (shops/training.py): one row for each account, made when the account
    first meets the training. The modules and the declarations are in `ModuleResult` and `Declaration`; this row holds
    only the facts that are not derived from them. Written by the training pages (shops/training.py), never by a form."""
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="trainee")
    created = models.DateTimeField(default=timezone.now, editable=False)
    # The day the basic pass was earned, once: a fact of the past, kept even if the community adds a module later
    base_earned = models.DateTimeField(_("basic pass earned"), null=True, blank=True, editable=False)
    # The last short review passed (or the basic pass itself): the next one is due `review_months` later
    reviewed = models.DateTimeField(_("last review"), null=True, blank=True, editable=False)

    class Meta:
        verbose_name = _("merchant training")
        verbose_name_plural = _("merchant training")

    def __str__(self):
        return f"{self.user_id}"


class ModuleResult(models.Model):
    """The quiz of one module of the training, for one account. `passed` is set when every answer was right, once; tries
    only count, with no penalty. A module is a name written in `community.yml`, so there is no foreign key to it."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="training_results")
    module = models.CharField(max_length=40)
    passed = models.DateTimeField(null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "module"], name="module_result_once")]


class Declaration(models.Model):
    """«I know that…»: a statement the merchant (or the person at a staff register) ticked, kept as the written proof that
    they were told. Which statement (`key`), the version of its text (`version`, the hash of the text the community
    wrote, in every language), the text exactly as it was shown (`text`, in `language`), when, who and for which shop.
    No address of the connection and no other detail of the device. Append only: a changed text makes a new version, asked
    again, and the old rows stay. It outlives the shop and the account (they are emptied, the names are copied)."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                             related_name="declarations", editable=False)
    shop = models.ForeignKey(Shop, null=True, blank=True, on_delete=models.SET_NULL, related_name="declarations",
                             editable=False)
    # A staff register's «I understood» has no account: the register that made it
    register = models.ForeignKey(StaffRegister, null=True, blank=True, on_delete=models.SET_NULL,
                                 related_name="declarations", editable=False)
    # The account's name, or the register's, as they were: the row says who even when the account is gone
    who = models.CharField(_("who"), max_length=150, editable=False)
    shop_name = models.CharField(_("shop"), max_length=120, blank=True, editable=False)
    key = models.CharField(_("statement"), max_length=80, editable=False)
    version = models.CharField(_("version of the text"), max_length=64, editable=False)
    text = models.TextField(_("text shown"), editable=False)
    language = models.CharField(_("language"), max_length=10, blank=True, editable=False)
    made = models.DateTimeField(_("when"), default=timezone.now, db_index=True, editable=False)

    class Meta:
        ordering = ["-made", "-pk"]
        verbose_name = _("declaration")
        verbose_name_plural = _("declarations")
        constraints = [
            models.UniqueConstraint(fields=["user", "key", "version", "shop"], name="declaration_once_by_user",
                                    condition=models.Q(user__isnull=False, shop__isnull=False)),
            # Only the rows made without a shop (no name copied): a row whose shop was deleted keeps the name and loses
            # the link, and two of those, for two shops of one owner, are two proofs, not one twice
            models.UniqueConstraint(fields=["user", "key", "version"], name="declaration_once_by_user_no_shop",
                                    condition=models.Q(user__isnull=False, shop__isnull=True, shop_name="")),
            models.UniqueConstraint(fields=["register", "key", "version"], name="declaration_once_by_register",
                                    condition=models.Q(register__isnull=False))]

    def __str__(self):
        return f"{self.who}: {self.key}"


class EmailSent(models.Model):
    """One outgoing email handed to the mail server (config/mailbudget.py counts them): the provider's quota is per month, so
    the count must survive a restart. Only a print of the recipient is kept (to cap what one address receives in a day),
    never the address or the text. Rows older than two months are dropped as new ones are written."""
    sent = models.DateTimeField(default=timezone.now, db_index=True)
    recipient = models.CharField(max_length=64, db_index=True)

    class Meta:
        ordering = ["-sent"]

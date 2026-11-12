"""Moving shops from one bitmentor site to another: `manage.py export_shops` writes a file, `manage.py
import_moved_shops` reads it on the other site.

The file carries what the shop *is*, never how it gets paid. In it: the shop's details (name, texts, position,
discount, contacts, business name and VAT number, Nostr identity, Telegram chat for alerts), what its owner chose to show
of them (shops/details.py), what the association decided about it (listed, suspended, historic), and what is needed to
tell its owner on the other site: the recovery email, and whether they sign in with a wallet. A file without the
business details (version 1) is read too: its shops start with the phone and the email shown, for a shop that had
an owner. Never in it: the wallet's login key, which is worth nothing on another
site (LNURL-auth gives each domain its own key), the Lightning address and what the guardian found about it, invoices, sales, refunds, the first
day's ticks, the certificate and the payments it is made of (the certificate is earned with payments on one network:
on another it starts again), passwords, sessions, secrets.

A file never gives a shop to an account that already exists here: a changed file must not hand a shop, or the private
part of the site, to somebody else, nor write an email on an account (that email would get the account back). A shop
goes straight to its owner only when the owner's email has no account here yet: the new account is made for that
email, and only whoever reads it can sign in. Every other owned shop arrives without an owner and with a one-time link
(`claim_hash` keeps only its SHA-256): the owner signs in here, with the wallet or the email, and opens it.

The import is a dry run unless asked to write. Everything happens in one transaction: a dry run is the same import
rolled back, so what it reports is what --apply would do. Each shop goes through the same rules as the shop form
(ShopDetailsForm) and the model's own checks. A shop already moved (found by its id on the other site), or a shop
already configured here with the same external id, is never touched: running the import twice changes nothing.

A moved shop gets a new id here: the id opens the register, and a register link handed out on the other site must not
open this one. A shop that had a register there waits off the map until its owner connects a wallet."""
import json
import math
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import urlencode

from django import forms
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models.functions import Lower
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import override

from config import features

from . import details
from .forms import CanonicalEmailField, ShopDetailsForm
from .lnurl_auth import LINKING_KEY
from .models import Profile, Shop, claim_digest, keeps_register, real

FORMAT = "bitmentor-shops"
# 2 has the business name, the VAT number and what the owner shows; 1 has none of them, and is read too.
VERSION = 2
READABLE = (1, 2)
# A file bigger than this is not an export of a community's shops
MAX_BYTES = 20 * 1024 * 1024
MAX_SHOPS = 20000
# No field of a shop is longer than the description: anything longer is refused before reaching the form
MAX_TEXT = 5000

DETAILS = tuple(ShopDetailsForm.Meta.fields)
TEXT = ("external_id", "name", "description", "address", "city", "website", "email", "phone", "legal_name", "vat_number",
        "nostr_pubkey", "nip05_name", "telegram_chat")
NUMBERS = ("latitude", "longitude")
FLAGS = ("listed", "lightning", "suspended", "register")
# What the owner shows of the details (shops/details.py), one true or false for each
SHOWN = tuple(details.flag(name) for name in details.CHOICES)
SHOP_KEYS = frozenset({"id", *TEXT, *NUMBERS, "discount", *FLAGS, *SHOWN, "closed_on", "owner"})
# What a file of version 1 does not have
NEW_IN_2 = frozenset({"legal_name", "vat_number", *SHOWN})
OWNER_KEYS = frozenset({"email", "wallet"})
TOP_KEYS = frozenset({"format", "version", "exported", "network", "site", "shops"})

NEW, FILLED, HERE, CONFLICT, REFUSED = "new", "filled", "here", "conflict", "refused"


class FileRefused(Exception):
    """The whole file is refused: nothing is written."""


# --- Export ---------------------------------------------------------------------------------------------------------

def owner_entry(user):
    """How to find the owner on another site, or None. Staff accounts sign in only with a password, which never
    travels: their shops move without an owner, and whoever runs the site assigns them from /admin/."""
    if user is None or user.is_staff or user.is_superuser or not user.is_active:
        return None
    wallet = bool(LINKING_KEY.fullmatch(user.username))
    profile = Profile.objects.filter(user=user).first()
    email = profile.email if profile else ""
    if not wallet and not email:
        return None
    return {"email": email, "wallet": wallet}


def has_register(shop):
    """It had a register here, or it had one on the site it came from and still waits for its wallet, or its owner saved
    it before having a wallet (`awaiting_wallet`): on the other site it waits off the map for its wallet too, never a
    place to pay without a register. A historic shop takes no new register: it moves as a place of the map."""
    return (shop.has_register or shop.moved_register or shop.awaiting_wallet) and not shop.closed_on


def waiting_for(shop, today=None):
    """What a shop moved here from another site still waits for, or None. "unclaimed": its owner has not opened the
    one-time link (which exists); "no_owner": nobody to give it to; "no_wallet": the owner took it, but it has no
    register. A shop that has its register, a historic one past REGISTER_AFTER_CLOSING, or one that did not move here waits for
    nothing. The same rule as the dashboard's list of moved shops (`dashboard.moved_items`), for one shop already read:
    a test keeps the two in step."""
    if shop.moved_in is None or not keeps_register(shop, today):
        return None
    if shop.has_register:
        return None
    if shop.owner_id is None:
        return "unclaimed" if shop.claim_hash else "no_owner"
    return "no_wallet"


def shop_entry(shop):
    register = has_register(shop)
    entry = {"id": str(shop.pk), **{name: getattr(shop, name) for name in TEXT}, "latitude": shop.latitude,
             "longitude": shop.longitude, "discount": shop.discount, "listed": shop.listed,
             # For a shop with a register the flag follows its Lightning address, which does not move
             "lightning": shop.lightning and not register, "suspended": shop.suspended,
             "register": register, **{name: getattr(shop, name) for name in SHOWN},
             "closed_on": shop.closed_on.isoformat() if shop.closed_on else None, "owner": owner_entry(shop.owner)}
    return entry


def exported_shops():
    """Every shop."""
    shops = real(Shop.objects.select_related("owner"))
    return shops.order_by(Lower("name"), "external_id", "pk")


def export():
    shops = list(exported_shops())
    document = {"format": FORMAT, "version": VERSION, "exported": timezone.now().isoformat(timespec="seconds"),
                "network": settings.NETWORK, "site": settings.PUBLIC_URL, "shops": [shop_entry(shop) for shop in shops]}
    # What the person running the export should know: owners that will not be found on the other site
    lost = [shop for shop in shops if shop.owner_id and owner_entry(shop.owner) is None]
    return document, lost


# --- Reading the file -----------------------------------------------------------------------------------------------

def no_duplicate_keys(pairs):
    keys = [key for key, _value in pairs]
    if len(keys) != len(set(keys)):
        raise FileRefused("a key appears twice in the same object")
    return dict(pairs)


def finite(text):
    value = float(text)
    if not math.isfinite(value):
        raise FileRefused(f"not a finite number: {text}")
    return value


def constant(text):
    raise FileRefused(f"not a number JSON allows: {text}")


def is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def check_shop(row, number, version=VERSION):
    """The shape of one shop, exactly as export_shops writes it: a key more or less, or a value of the wrong kind,
    refuse the whole file, since it was not written by export_shops or was changed by hand."""
    where = f"shop {number}"
    if not isinstance(row, dict):
        raise FileRefused(f"{where} is not an object")
    keys = SHOP_KEYS if version >= 2 else SHOP_KEYS - NEW_IN_2
    if set(row) != keys:
        extra, missing = sorted(set(row) - keys), sorted(keys - set(row))
        raise FileRefused(f"{where}: unexpected keys {extra}, missing keys {missing}")
    try:
        if not isinstance(row["id"], str) or len(row["id"]) != 36:
            raise ValueError
        uuid.UUID(row["id"])
    except ValueError:
        raise FileRefused(f"{where}: id is not a UUID") from None
    for name in TEXT:
        if name in row and (not isinstance(row[name], str) or len(row[name]) > MAX_TEXT):
            raise FileRefused(f"{where}: {name} is not a text of at most {MAX_TEXT} characters")
    for name in NUMBERS:
        if row[name] is not None and not is_number(row[name]):
            raise FileRefused(f"{where}: {name} is not a number")
    if not isinstance(row["discount"], int) or isinstance(row["discount"], bool):
        raise FileRefused(f"{where}: discount is not a whole number")
    for name in (*FLAGS, *(name for name in SHOWN if name in row)):
        if not isinstance(row[name], bool):
            raise FileRefused(f"{where}: {name} is not true or false")
    if row["closed_on"] is not None and not isinstance(row["closed_on"], str):
        raise FileRefused(f"{where}: closed_on is not a date")
    owner = row["owner"]
    if owner is not None:
        if not isinstance(owner, dict) or set(owner) != OWNER_KEYS:
            raise FileRefused(f"{where}: owner is not an object with email and wallet")
        if not isinstance(owner["wallet"], bool):
            raise FileRefused(f"{where}: owner wallet is not true or false")
        if not isinstance(owner["email"], str) or len(owner["email"]) > 320:
            raise FileRefused(f"{where}: owner email is not a text")


def upgrade(row):
    """A shop of a version 1 file, with what version 2 adds: no business name or VAT number, and the choices of a
    shop whose phone and email were on show: they stay on show for a shop that had an owner; the rest as a new shop."""
    owned = row["owner"] is not None
    row["legal_name"] = row["vat_number"] = ""
    for name in details.CHOICES:
        row[details.flag(name)] = name in details.DEFAULT_SHOWN or (owned and name in ("phone", "email") and bool(row[name]))


def read(data):
    """The document from the bytes of the file, or FileRefused with the reason."""
    if len(data) > MAX_BYTES:
        raise FileRefused(f"the file is larger than {MAX_BYTES // 1024 // 1024} MB")
    try:
        text = data.decode("utf-8")
        document = json.loads(text, object_pairs_hook=no_duplicate_keys, parse_float=finite, parse_constant=constant)
    except UnicodeDecodeError:
        raise FileRefused("the file is not UTF-8 text") from None
    except (ValueError, RecursionError) as error:
        raise FileRefused(f"the file is not valid JSON ({type(error).__name__})") from None
    if not isinstance(document, dict) or document.get("format") != FORMAT:
        raise FileRefused(f"this is not a file written by export_shops (format {FORMAT!r})")
    version = document.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version not in READABLE:
        raise FileRefused(f"the file is version {version!r}, this site reads versions {', '.join(map(str, READABLE))}: "
                          "update the older site first")
    if set(document) != TOP_KEYS:
        raise FileRefused(f"unexpected or missing keys at the top of the file: {sorted(set(document) ^ TOP_KEYS)}")
    for name in ("exported", "network", "site"):
        if not isinstance(document[name], str) or len(document[name]) > 200:
            raise FileRefused(f"{name} is not a short text")
    shops = document["shops"]
    if not isinstance(shops, list) or len(shops) > MAX_SHOPS:
        raise FileRefused(f"shops is not a list of at most {MAX_SHOPS}")
    for number, row in enumerate(shops, 1):
        check_shop(row, number, version)
        if version < 2:
            upgrade(row)
        # One spelling of the same id: upper case, braces or no dashes would be another shop
        row["id"] = str(uuid.UUID(row["id"]))
    ids = [row["id"] for row in shops]
    if len(ids) != len(set(ids)):
        raise FileRefused("the same shop id appears twice")
    return document


# --- Import ---------------------------------------------------------------------------------------------------------

@dataclass
class Outcome:
    row: dict
    status: str = ""
    reasons: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    shop: Shop = None
    sign_in: str = ""  # "wallet" or "email": how the owner signs in here; "" when nobody will be told
    email: str = ""
    # The one-time link's code, when the shop waits for its owner (only known while the import runs)
    code: str = ""

    @property
    def name(self):
        return self.row["name"]


class RowRefused(Exception):
    pass


def configured(shop):
    """A shop someone already set up here: never overwritten by a move."""
    return bool(shop.owner_id or shop.lightning_address or shop.moved_in)


def form_data(row):
    data = {name: row[name] for name in DETAILS}
    for name in NUMBERS:
        data[name] = "" if row[name] is None else repr(row[name])
    data["discount"] = str(row["discount"])
    return data


def checked_shop(row, target, outcome):
    """The shop with the row's values, through the form's rules and the model's checks, or RowRefused. `target` is a
    new Shop, or the unclaimed directory shop of this site it replaces."""
    row = dict(row)
    replaced = target.pk is not None and Shop.objects.filter(pk=target.pk).exists()
    hidden = (target.suspended, target.listed) if replaced else (False, True)
    form = ShopDetailsForm(form_data(row), instance=target)
    if not form.is_valid() and "nip05_name" in form.errors:
        # A Nostr name taken or reserved here is not a reason to leave the shop behind: it arrives without one
        outcome.notes.append(_("Its Nostr name %(name)s cannot be used here: it arrives without one.")
                             % {"name": row["nip05_name"]})
        row["nip05_name"] = ""
        form = ShopDetailsForm(form_data(row), instance=target)
    if not form.is_valid():
        raise RowRefused("; ".join(f"{field}: {' '.join(errors)}" for field, errors in form.errors.items()))
    shop = form.save(commit=False)
    shop.external_id = row["external_id"]
    shop.lightning = row["lightning"]
    for name in SHOWN:
        setattr(shop, name, row[name])
    if form.doubt():
        # A generic sentence: the number, and the letters it starts with, are never in the report, which goes to the
        # terminal and to the logs
        outcome.notes.append(_("Its VAT number may have a mistake: look at it in the admin. It arrives as written."))
    # What this site decided stays: a directory shop suspended or kept off the map here is not switched back on
    shop.suspended = row["suspended"] or hidden[0]
    shop.listed = row["listed"] and hidden[1]
    shop.closed_on = None
    if row["closed_on"] is not None:
        try:
            shop.closed_on = date.fromisoformat(row["closed_on"])
        except ValueError:
            raise RowRefused(f"closed_on: not a YYYY-MM-DD date: {row['closed_on']!r}") from None
    try:
        shop.full_clean()
    except ValidationError as error:
        raise RowRefused("; ".join(f"{field}: {' '.join(messages)}" for field, messages in error.message_dict.items())) from None
    for name in NUMBERS:
        value = getattr(shop, name)
        if value is not None and not math.isfinite(value):
            raise RowRefused(f"{name}: not a finite number")
    return shop


def profile_with(email):
    return Profile.objects.annotate(lowered=Lower("email")).filter(lowered=email).select_related("user").first()


def issue_code(shop, outcome):
    """A new one-time link for the shop: the code goes to the owner, only its digest stays here."""
    outcome.code = secrets.token_urlsafe(24)
    shop.claim_hash = claim_digest(outcome.code)


def owner_email(entry, outcome):
    """The owner's email as this site writes it (as the sign-in form does), or "" when it is not one."""
    try:
        return (CanonicalEmailField(required=False).clean(entry["email"].strip()) or "").lower()
    except forms.ValidationError:
        outcome.notes.append(_("Its owner's email is not valid: left out."))
        return ""


def place_owner(shop, entry, outcome, made):
    """Gives the shop to a new account made for its owner's email, or leaves it waiting with a one-time link. Never
    to an account that already exists here, except one this same import made for the same email (`made`, email to
    user): an owner with two shops gets both in one account."""
    if entry is None:
        return
    email = outcome.email = owner_email(entry, outcome)
    if email and email in made:
        shop.owner, outcome.sign_in = made[email], "email"
        return
    if email and profile_with(email) is None:
        user = get_user_model().objects.create(username=f"email:{secrets.token_hex(16)}")
        Profile.objects.create(user=user, email=email)
        made[email] = user
        shop.owner, outcome.sign_in = user, "email"
        return
    if not email and not entry["wallet"]:
        outcome.notes.append(_("Its owner cannot be found here: assign it from the admin."))
        return
    if email:
        outcome.sign_in = "email"
        outcome.notes.append(_("Its owner's email already has an account here: the shop waits for a one-time link."))
    else:
        outcome.sign_in = "wallet"
        outcome.notes.append(_("Its owner signs in with a wallet only, which has another key on another site: the "
                               "shop waits for a one-time link."))
    issue_code(shop, outcome)


def import_row(row, now, made, new_codes=False):
    outcome = Outcome(row=row)
    moved = Shop.objects.filter(moved_from=row["id"])
    # Under the lock when a code may be made again: an owner taking the shop now wins, and keeps it
    here = (moved.select_for_update() if new_codes else moved).first()
    if here is not None:
        outcome.status = HERE
        outcome.shop = here
        if new_codes and here.owner_id is None and here.claim_hash and row["owner"] is not None:
            # The list of owners went lost: a new link replaces the old one, which stops working
            outcome.email = owner_email(row["owner"], outcome)
            outcome.sign_in = "email" if outcome.email else "wallet"
            issue_code(here, outcome)
            here.save(update_fields=["claim_hash"])
        return outcome
    target = None
    if row["external_id"]:
        same = Shop.objects.filter(external_id=row["external_id"]).order_by("pk")
        if len(same) > 1:
            outcome.notes.append(_("%(count)s shops here have the external id %(id)s.")
                                 % {"count": len(same), "id": row["external_id"]})
        if any(configured(shop) for shop in same):
            outcome.status = CONFLICT
            outcome.reasons.append(_("A shop with the same external id is already set up here: left as it is."))
            return outcome
        # A shop of the directory, imported here and never claimed: the moved shop takes its place and its id
        target = same.first()
    try:
        with transaction.atomic():
            shop = checked_shop(row, target or Shop(), outcome)
            shop.moved_in, shop.moved_from = now, row["id"]
            shop.moved_register = row["register"] and shop.closed_on is None
            shop.owner, shop.claim_hash = None, ""
            place_owner(shop, row["owner"], outcome, made)
            shop.save()
    except RowRefused as error:
        outcome.code, outcome.sign_in = "", ""
        outcome.status = REFUSED
        outcome.reasons.append(str(error))
        return outcome
    except IntegrityError:
        outcome.code, outcome.sign_in = "", ""
        outcome.status = REFUSED
        outcome.reasons.append(_("Another shop here has the same unique value (Nostr name or id)."))
        return outcome
    outcome.status = FILLED if target else NEW
    outcome.shop = shop
    return outcome


def import_document(document, apply=False, new_codes=False):
    """Every shop of the document, in one transaction: rolled back unless `apply`. Returns the outcomes. A dry run
    keeps no code: the links it would give do not exist."""
    now = timezone.now()
    with transaction.atomic():
        made = {}
        outcomes = [import_row(row, now, made, new_codes) for row in document["shops"]]
        if not apply:
            transaction.set_rollback(True)
            for outcome in outcomes:
                outcome.code = ""
    return outcomes


# --- What the owners are told ---------------------------------------------------------------------------------------

def claim_url(code, language):
    """The one-time link for a code, in the language of the owner's message. The code goes in the query, as for the
    sign-in links: access logs write the path, and the code must not stay there. Never from the Host header."""
    with override(language):
        return f"{settings.PUBLIC_URL}{reverse('claim_shop')}?{urlencode({'code': code})}"


def claim_link(outcome, language):
    """The one-time link of a moved shop, or "" when the shop waits for nothing."""
    return claim_url(outcome.code, language) if outcome.code else ""


def owner_message(outcome, site, language):
    """The words for the owner of a moved shop, in the language of the site: what happened and what to do now. In a
    dry run the link is not there yet, and the message says so."""
    with override(language):
        if outcome.sign_in == "wallet":
            how = _("with your wallet (scan the code on the sign-in page)")
        else:
            how = _("with your email %(email)s (we send you a link)") % {"email": outcome.email}
        if outcome.shop is not None and outcome.shop.owner_id:
            parts = [_("Your shop %(name)s has moved to %(site)s. Sign in there %(how)s: your page and its details are "
                       "already there.") % {"name": outcome.name, "site": site, "how": how}]
        else:
            link = claim_link(outcome, language) or _("[the link is made with --apply]")
            parts = [_("Your shop %(name)s has moved to %(site)s. Sign in there %(how)s, then open this link to take it "
                       "back: %(link)s. It works once, for whoever opens it first: do not pass it on.")
                     % {"name": outcome.name, "site": site, "how": how, "link": link}]
        if outcome.shop.moved_register:
            # The on-chain key only while on-chain at the register, an experimental feature, is on
            parts.append(_("To take payments again, connect your wallet from your shops' page: your Lightning address, "
                           "your on-chain key, or both, as before. Until then the shop is not on the map.")
                         if features.enabled(features.ONCHAIN) else
                         _("To take payments again, connect your wallet from your shops' page, with your Lightning "
                           "address, as before. Until then the shop is not on the map."))
            parts.append(_("Then print the new sign: the old one leads to the old site. If someone else takes payments "
                           "with you, send them the new register link."))
        else:
            parts.append(_("Nothing else to do."))
        return " ".join(parts)

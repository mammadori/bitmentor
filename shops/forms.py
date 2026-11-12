import re
import socket
from pathlib import Path

from django import forms
from django.conf import settings
from django.utils.text import format_lazy, slugify
from django.utils.translation import gettext_lazy as _

from config import features
from config.community import community
from config.network import is_public, resolve

from . import details
from .certified import address_taken
from .details import INVISIBLE_CONTROLS
from .models import Shop

PRIVATE_KEY = re.compile(r"[xyztuv]prv[1-9a-z]{20,}|nsec1[0-9a-z]{50,}", re.I)
SEED_WORDS = frozenset((Path(__file__).parent / "bip39_english.txt").read_text().split())
SEED_LENGTH = 12
LIGHTNING_ADDRESS = re.compile(r"^[a-z0-9._+-]+@((?!-)[a-z0-9-]{1,63}(?<!-)\.)+[a-z]{2,63}$", re.I)
# What comes around an address copied from a message or a page, and the prefix of a `lightning:` link
PASTED_AROUND = "\"'`«»“”„‘’‚‹›<>()[]{}.,;:!?"
LIGHTNING_PREFIX = re.compile(r"^lightning:(//)?", re.I)
LNURLP_URL = re.compile(r"(?:https|lnurlp)://(?P<host>[^/?#:@]+)/\.well-known/lnurlp/(?P<name>[^/?#@]+)/?(?:[?#].*)?", re.I)
# A BOLT11 invoice: in the wallet it sits next to the address, under Receive, and gets copied instead of it
LIGHTNING_INVOICE = re.compile(r"^ln(?:bc|tb|tbs|bcrt)[0-9]*[munp]?1[02-9ac-hj-np-z]{6,}$", re.I)
PRIVATE_NAMES = (".local", ".internal", ".intranet", ".lan", ".home", ".corp", ".localhost", ".onion")
# A link may propose only these: never where the money goes, which would come from whoever wrote the link, not from the owner
PREFILL_FIELDS = ("name", "description", "address", "city", "discount")
CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def looks_like_seed(value):
    run = 0
    for word in re.findall(r"[a-z]+", value.lower()):
        run = run + 1 if word in SEED_WORDS else 0
        if run >= SEED_LENGTH:
            return True
    return False


def secret_warning(value):
    if PRIVATE_KEY.search(value):
        return _("This is a private key: never share it. Use the public key of your wallet.")
    if looks_like_seed(value):
        return _("This looks like a seed phrase: never share it. Use the public key of your wallet.")
    return None


def tidy_lightning_address(text):
    """A Lightning address as merchants paste it, in the form the register uses: without spaces or invisible
    characters, quotes or brackets around it, a full stop after it, the `lightning:` prefix that wallets copy too; the
    address of its LNURL (`https://provider/.well-known/lnurlp/name`) becomes the address itself; and lowercase, as
    LUD-16 wants it and as the phone does not type it. static/shop_form.js does the same in the page."""
    value = INVISIBLE_CONTROLS.sub("", "".join(text.split())).strip(PASTED_AROUND)
    value = LIGHTNING_PREFIX.sub("", value).strip(PASTED_AROUND)
    url = LNURLP_URL.fullmatch(value)
    if url:
        value = f"{url['name']}@{url['host']}"
    return value.lower()


def validate_lightning_address(value):
    """The shape of the address, before asking anything to its provider. Each error has its code, which the live check
    of the form gives the page."""
    if value.startswith("₿") or value.startswith("lno1"):
        raise forms.ValidationError(format_lazy(
            _("This address uses BOLT12, like the ones from Phoenix, and this register cannot read it. "
              "Use a wallet with a different Lightning address, or write to {email}."), email=community()["email"]),
            code="bolt12")
    if LIGHTNING_INVOICE.match(value):
        raise forms.ValidationError(_("This is a request for one payment, not your Lightning address: in your wallet, "
                                      "look for the address that looks like an email, name@provider."), code="invoice")
    if not LIGHTNING_ADDRESS.match(value):
        raise forms.ValidationError(_("This is not a Lightning address: it should look like name@provider.com."),
                                    code="shape")
    domain = value.rpartition("@")[2].lower()
    if domain.endswith(PRIVATE_NAMES) or not public_host(domain):
        raise forms.ValidationError(_("This Lightning address cannot be reached from the internet."), code="private")


def public_host(domain):
    try:
        addresses = resolve(domain, 443, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError):
        # A name that does not resolve, or not in time: the probe asks it again within its own deadline, refuses it or
        # answers "try again", and connects only to the addresses it checked (lnaddress.fetch), so nothing inside is
        # reached either way
        return True
    return all(is_public(address[4][0]) for address in addresses)


def plain_input(**attrs):
    """On the phone: no initial capital and no autocorrect on what must be copied literally."""
    return forms.TextInput(attrs={"autocapitalize": "none", "autocorrect": "off", "spellcheck": "false", **attrs})


class FocusOnFirstError:
    """After a wrong submit the focus goes to the first field to fix: the browser does it with autofocus, without JS.
    The shop forms do it with shop_form.js, in page order."""

    def full_clean(self):
        super().full_clean()
        first = next((name for name in self.fields if name in self._errors), None)
        if first:
            self.fields[first].widget.attrs["autofocus"] = True


class CoordinateField(forms.FloatField):
    widget = forms.TextInput(attrs={"inputmode": "decimal"})

    def to_python(self, value):
        return super().to_python(value.replace(",", ".") if isinstance(value, str) else value)


def vat_number_field(**kwargs):
    """The VAT number as a form writes it: typed with spaces, dots or dashes as on an invoice, kept without them. More than
    a number can be is refused as too long; a number of a strange shape is not refused: see VatNumberMixin."""
    return forms.CharField(label=_("VAT number"), required=False, max_length=48, **kwargs)


class VatNumberMixin:
    """The two rules of every form that writes a VAT number, the owner's and the admin's: the number is kept tidy and its
    length is checked on the tidy number (a capital ß is two letters), and one that looks wrong is saved all the same."""

    def clean_vat_number(self):
        number = details.normalize_vat(self.cleaned_data["vat_number"])
        if len(number) > Shop._meta.get_field("vat_number").max_length:
            raise forms.ValidationError(_("This is longer than a VAT number can be."))
        return number

    def doubt(self):
        """Why the VAT number just written may be wrong, or "": for the message after saving (never a refusal)."""
        return details.vat_doubt(self.cleaned_data.get("vat_number", ""))


class ShopAdminForm(VatNumberMixin, forms.ModelForm):
    """The form of /admin/shops/shop/: the model's fields, with the VAT number kept as the owner's form keeps it."""
    vat_number = vat_number_field(widget=forms.TextInput(attrs={"class": "vTextField"}))

    class Meta:
        model = Shop
        fields = "__all__"


class ShopDetailsForm(VatNumberMixin, forms.ModelForm):
    """The shop data the owner can change: the destination of the funds is not among them."""
    # Plain text: whoever writes www.example.it must not see the form refused by the browser
    website = forms.URLField(label=_("website"), required=False, assume_scheme="https", max_length=200,
                             widget=plain_input(inputmode="url", placeholder="www.example.com"))
    vat_number = vat_number_field(widget=plain_input(autocomplete="off", autocapitalize="characters"))

    class Meta:
        model = Shop
        fields = ["name", "description", "address", "city", "latitude", "longitude", "website", "email", "phone",
                  "legal_name", "vat_number", "discount", "nostr_pubkey", "nip05_name", "telegram_chat"]
        field_classes = {"latitude": CoordinateField, "longitude": CoordinateField}
        widgets = {"description": forms.Textarea(attrs={"rows": 3}), "phone": forms.TextInput(attrs={"inputmode": "tel"}),
                   "nostr_pubkey": plain_input(), "nip05_name": plain_input(), "telegram_chat": plain_input()}
        help_texts = {
            "description": _("Public: it appears on the shop page."),
            "website": _("Customers see your website unless you hide it: see «What customers see»."),
            "email": _("Customers see your email only if you turn it on: see «What customers see»."),
            "phone": _("Customers see your phone number only if you turn it on: see «What customers see»."),
            "legal_name": _("The name of the business, as on your invoices. Customers see it only if you turn it on."),
            "vat_number": _("With the two letters of the country, like IT01234567890. Customers see it only if you turn it on."),
            "discount": _("Leave it at 0 if you do not offer one."),
            "nostr_pubkey": _("Only if you use Nostr: your public key in hex format, not the npub."),
            "nip05_name": _("The name for your Nostr address on this site."),
            "telegram_chat": _("The chat number or @name where the site writes to you: account changes, help answers, the reopening after a closure."),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if "discount" in self.fields:
            self.fields["discount"].required = False

    def clean_discount(self):
        return self.cleaned_data["discount"] or 0

    def clean_telegram_chat(self):
        """The bot writes where the owner says: not in the moderators' chat, where a shop name chosen on purpose
        would look like a message from the community."""
        chat = self.cleaned_data["telegram_chat"].strip()
        moderators = settings.TELEGRAM_MODERATION_CHAT.strip().lower()
        same_number = chat.lstrip("-").isdigit() and moderators.lstrip("-").isdigit() and int(chat) == int(moderators)
        if chat and (chat.lower() == moderators or same_number):
            raise forms.ValidationError(_("This chat cannot receive the site's messages."))
        return chat

    def clean_nip05_name(self):
        """The name is an identity: `_` is the community's, and two shops do not share one. Names that a Nostr
        client would show as the association's own (info@, admin@, the community name) are not shops."""
        name = self.cleaned_data["nip05_name"].lower()
        reserved = RESERVED_NIP05 | {slugify(community().get("name", "")), slugify(settings.SITE_HOST.split(".")[0])}
        if name in reserved or (name and Shop.objects.filter(nip05_name__iexact=name).exclude(pk=self.instance.pk).exists()):
            raise forms.ValidationError(_("This name is already taken."))
        return name

    def clean(self):
        data = super().clean()
        for field in self.fields:
            warning = secret_warning(str(self.data.get(field, "")))
            if warning:
                self.add_error(field, warning)
        # Characters that are not seen but change the surrounding text: U+202E reverses the line in the map, the title and on
        # Telegram. U+200D stays, as it holds composed emoji together
        for field in ("name", "description", "address", "city", "legal_name"):
            if isinstance(data.get(field), str):
                data[field] = INVISIBLE_CONTROLS.sub("", data[field]).strip()
        return data


class ShopDisplayForm(forms.ModelForm):
    """What customers see of the shop's details (shops/details.py): one tick for each, and nothing else. Apart from the
    details form on purpose: a form that did not carry the ticks would switch them all off when saved."""
    class Meta:
        model = Shop
        fields = [details.flag(name) for name in details.CHOICES]


class ShopForm(ShopDetailsForm):
    class Meta(ShopDetailsForm.Meta):
        # On-chain too: a sign for the map that the owner gives; not in ShopDetailsForm, which a move between sites carries
        fields = [*ShopDetailsForm.Meta.fields, "lightning_address", "onchain"]
        help_texts = {**ShopDetailsForm.Meta.help_texts, "onchain": _(
            "Only a sign for customers, on the map and on your page. Where on-chain payments go is set in your register, "
            "on your phone: the site never sees it.")}
        widgets = {**ShopDetailsForm.Meta.widgets, "lightning_address": plain_input(inputmode="email", autocomplete="off")}

    # Shops of one owner may share a Lightning address, shops of different owners never (shops/certified.py): which shop
    # has it, and whose, is not said
    TAKEN = _("This Lightning address is already used by another shop. Use the address of your own wallet.")

    def __init__(self, *args, owner=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.owner = owner or self.instance.owner
        # On-chain at the register is an experimental feature (config/features.py): off, the form does not offer its
        # sign, and what the shop had ticked stays in the database untouched
        if not features.enabled(features.ONCHAIN):
            self.fields.pop("onchain", None)
        self.fields["lightning_address"].help_text = _("It looks like an email: name@provider. You find it in your wallet, usually under Receive. "
                                                     "Phoenix addresses, the ones with the ₿ sign, do not work here.")

    def clean(self):
        data = super().clean()
        lightning = tidy_lightning_address(data.get("lightning_address", ""))
        data["lightning_address"] = lightning
        # Written back as it will be used: after a wrong submit the field shows the tidy address, not what was pasted
        if lightning != self.data.get("lightning_address", lightning):
            self.data = self.data.copy()
            self.data["lightning_address"] = lightning
        if lightning and "lightning_address" not in self.errors:
            try:
                validate_lightning_address(lightning)
            except forms.ValidationError as error:
                self.add_error("lightning_address", error)
        if lightning and "lightning_address" not in self.errors and address_taken(lightning, self.owner, self.instance):
            self.add_error("lightning_address", forms.ValidationError(self.TAKEN, code="taken"))
        if not lightning and "lightning_address" not in self.errors and not data.get("later"):
            raise forms.ValidationError(_("Add your Lightning address."))
        return data

    def save(self, commit=True):
        shop = super().save(commit=False)
        # Only NewShopForm lets a shop without an address through: no register until it has one
        shop.lightning, shop.awaiting_wallet = bool(shop.lightning_address), not shop.lightning_address
        if commit:
            shop.save()
        return shop


class NewShopForm(ShopForm):
    """The new shop form, which can also save a shop whose owner has no wallet yet: ticked, and with no address written,
    the shop is saved waiting for its wallet (Shop.awaiting_wallet). A written address always counts and is checked as
    usual, ticked or not."""
    later = forms.BooleanField(label=_("I do not have a wallet yet: save my shop, I add the wallet afterwards"),
                               required=False, widget=forms.CheckboxInput(attrs={"aria-describedby": "later-help"}))


class WalletForm(ShopForm):
    """Where the money goes, for a shop of the owner that has no register yet: one saved before having a wallet, one
    moved from another site, or one of the directory given to its owner. Only the address: the details have
    their own page, and `website` and `vat_number`, declared on ShopDetailsForm, are not among them."""
    website = None
    vat_number = None

    class Meta(ShopForm.Meta):
        fields = ["lightning_address"]


def prefill(query):
    """What a link proposes in the form: every value goes through its field's rule, otherwise it is not there."""
    values = {}
    for name in PREFILL_FIELDS:
        text = query.get(name, "").strip()
        if not text or CONTROL_CHARS.search(text) or secret_warning(text):
            continue
        try:
            values[name] = Shop._meta.get_field(name).clean(ShopForm.base_fields[name].clean(text), None)
        except forms.ValidationError:
            continue
    return {name: value for name, value in values.items() if value}


RESERVED_NIP05 = {"_", "admin", "administrator", "info", "contact", "contatti", "help", "support", "staff", "moderator",
                  "mod", "root", "webmaster", "postmaster", "abuse", "security", "noreply", "news", "press", "stampa",
                  "official", "team", "board", "association", "associazione", "verein", "community", "comunita", "bitcoin"}


class CanonicalEmailField(forms.EmailField):
    """The domain as mail writes it (IDNA 2003, like send_mail): «mario@ｇｍａｉｌ.com» and «mario@gmail.com» arrive
    in the same mailbox, and must be the same address for the limits, the fingerprints and uniqueness."""

    def clean(self, value):
        value = super().clean(value)
        if not value:
            return value
        local, _at, domain = value.rpartition("@")
        try:
            domain = domain.encode("idna").decode("ascii")
        except UnicodeError:
            raise forms.ValidationError(self.error_messages["invalid"], code="invalid") from None
        return f"{local}@{domain.lower()}"


# The owner's email: the browser suggests it by itself (WCAG 1.3.5)
OWN_EMAIL = forms.EmailInput(attrs={"autocomplete": "email"})


class ProfileForm(FocusOnFirstError, forms.Form):
    email = CanonicalEmailField(label=_("Email to get back in"), required=False, widget=OWN_EMAIL,
                             help_text=_("We send a link to confirm it. It is used to get back into your account, and to tell you if "
                                         "your register stops taking Lightning payments."))


class RecoveryForm(FocusOnFirstError, forms.Form):
    email = CanonicalEmailField(label=_("Your email"), widget=OWN_EMAIL)



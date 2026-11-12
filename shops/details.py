"""What a shop tells the public, and what its owner has chosen to keep to themselves (docs/features/shop-details.md).

The association keeps the shop's data in the database; the owner chooses which of it customers see. The name is always
shown. Of the rest the owner turns on or off the website, the address, the phone, the email, the business name and the
VAT number: `Shop.show_<detail>` holds the choice, and nothing else decides it.

Every place that shows a shop's details to someone who is not its owner reads them from here: the shop's page, the
map, `/shops.json`, the Nostr events, the owner's own preview. A detail that is hidden is not in the answer at all, not
even empty: a page added tomorrow that reads `shop.phone` directly would show it, and `shops/test_details.py` walks
the public outputs to catch it.
"""
import re
import unicodedata

from django.utils.translation import gettext_lazy as _

# The details an owner can show or hide, in the order of the page
CHOICES = ("website", "address", "phone", "email", "legal_name", "vat_number")
# Until the owner chooses: the website (if there is one) and the address, which a shop tells anyway. The rest is the
# owner's to switch on
DEFAULT_SHOWN = frozenset({"website", "address"})
# Only the shop's own page shows these. The map, its list and /shops.json carry the website and the address and
# nothing else: a list of every shop's phone, email and VAT number, ready to be downloaded, is not what a customer needs
PAGE_ONLY = ("phone", "email", "legal_name", "vat_number")
# What the owner reads next to each choice
LABELS = {
    "website": _("Website"), "address": _("Address"), "phone": _("Phone"), "email": _("Email"),
    "legal_name": _("Business name"), "vat_number": _("VAT number"),
}


def flag(name):
    """The field of Shop that holds the choice for a detail."""
    return f"show_{name}"


def chosen(shop, name):
    return bool(getattr(shop, flag(name)))


def shown(shop):
    """{detail: value} for what the owner shows and has filled in. A hidden or empty detail is not in it."""
    return {name: getattr(shop, name) for name in CHOICES if chosen(shop, name) and getattr(shop, name)}


def shown_on_a_suspended_page(shop):
    """A shop the association took off the map keeps its page for the sign's QR code, with no contacts: the address and
    nothing else."""
    return {name: value for name, value in shown(shop).items() if name == "address"}


# The order of the lines on the shop's page
PAGE_ORDER = ("address", "phone", "email", "website", "legal_name", "vat_number")


def rows(shop, facts, preview=False):
    """The lines of a shop's page that say who and where it is (shops/shop_facts.html): {name, value, hidden}. `facts` is
    what is shown. The owner's preview also carries the filled ones that are hidden, marked, so that ticking a box can
    show a line without asking the server."""
    return [{"name": name, "value": getattr(shop, name), "hidden": name not in facts}
            for name in PAGE_ORDER if name in facts or (preview and getattr(shop, name))]


def place(shop):
    """The line that says where the shop is, for the map, its list and the Nostr event: the address if it is shown,
    otherwise the town."""
    return shown(shop).get("address") or shop.city


def api(shop):
    """What /shops.json says of the shop's details: the website and the address when they are shown, even empty (they
    were always there), and nothing when they are hidden."""
    return {name: getattr(shop, name) for name in ("address", "website") if chosen(shop, name)}


# --- The VAT number --------------------------------------------------------------------------------------------------

# The prefixes of the VAT numbers of the EU (Greece writes EL, Northern Ireland XI)
EU_PREFIXES = frozenset("AT BE BG CY CZ DE DK EE EL ES FI FR HR HU IE IT LT LU LV MT NL PL PT RO SE SI SK XI".split())
SEPARATORS = re.compile(r"[\s.\-/_]")
# The marks that reverse or hide text (U+202E turns a line round in the map, the title and on Telegram). The shop form takes
# them out of the names and the texts; normalize_vat takes out these and every other control or format character
INVISIBLE_CONTROLS = re.compile("[\u200b\u200c\u200e\u200f\u202a-\u202e\u2060-\u2069\ufeff]")
ITALIAN = re.compile(r"[0-9]{11}")
OTHER_BODY = re.compile(r"[0-9A-Z]{2,12}")


def normalize_vat(text):
    """The number as it is kept: no spaces, dots, dashes or slashes, no control or invisible character (a null byte, a
    zero-width space, the marks that reverse a line: the same ones the shop form takes out of the texts), capital
    letters. «it 012.345.678-90» is «IT01234567890». Nothing else is touched. A few capitals are longer than their
    small letter («ß» is «SS»): the length is checked on what this returns."""
    kept = "".join(char for char in str(text or "") if unicodedata.category(char) not in ("Cc", "Cf"))
    return SEPARATORS.sub("", kept).upper()


def italian_check(digits):
    """The check digit of an Italian partita IVA: the Luhn sum of the eleven digits must end in zero."""
    total = 0
    for position, char in enumerate(reversed(digits)):
        digit = int(char)
        if position % 2:
            digit = digit * 2 - 9 if digit > 4 else digit * 2
        total += digit
    return total % 10 == 0


def vat_doubt(text):
    """Why a VAT number looks wrong, or "". It is a doubt, never a refusal: an unusual number is saved as written and the
    association decides. Italy: eleven digits whose last one checks the others. Another country of the EU: its two
    letters and a few digits or letters. A bare number is taken for Italian if it fits, and otherwise asks for the letters."""
    number = normalize_vat(text)
    if not number:
        return ""
    prefix, body = (number[:2], number[2:]) if number[:2].isalpha() else ("", number)
    if not prefix:
        if ITALIAN.fullmatch(body) and italian_check(body):
            return ""
        return str(_("Start with the two letters of the country, like IT for Italy or DE for Germany."))
    if prefix not in EU_PREFIXES:
        return str(_("The letters %(prefix)s are not those of a country of the EU (IT, DE, FR, ES…).") % {"prefix": prefix})
    if prefix == "IT":
        if ITALIAN.fullmatch(body) and italian_check(body):
            return ""
        return str(_("An Italian number is IT and eleven digits, the last one checks the others: one of them may be wrong."))
    if OTHER_BODY.fullmatch(body):
        return ""
    return str(_("After the two letters of the country there are a few digits or letters, with no other signs."))

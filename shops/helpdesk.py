"""«Something wrong?»: the merchant tells the symptom, the site says what it already knows about the shop and what to do
now, and, if that is not enough, one tap asks the association for help, with the diagnosis attached.

The page starts from the symptom (`KINDS`): a few words the merchant would use at the till. For each one, two lists of
the checks of shops/diagnosis.py: `local`, which only read the database and run while the page loads, and `deep`,
which ask the merchant's Lightning provider, with the time limits the diagnosis already has, and
run only when the merchant presses «Look again» or asks for help. No rule is copied here: the findings are the ones
`diagnose_shop` prints, in the same words (they already speak to the merchant), and the page adds only what the site keeps
besides (the watch on the Lightning address) and the steps to take.

A request (`HelpRequest`) is made by `create`, which keeps the limit per shop under a lock, and completed by `finish`
outside the lock: the diagnosis, then the message to the moderators' chat. Telegram that does not answer, refuses, or is
not set up never loses the request: it stays on the dashboard (shops/dashboard.py), and the merchant is told. Nothing
a merchant writes reaches a message as anything but plain text, and nothing secret is ever asked for or sent.

This module does not import shops/diagnosis.py at the top: that one imports the views, and the panel (a view) asks
`panel` for the state of a request."""
import logging
from dataclasses import dataclass
from datetime import timedelta

from django import forms
from django.conf import settings
from django.db import transaction
from django.urls import reverse
from django.utils import timezone, translation
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from config import limits

from . import lightning_watch
from .forms import secret_warning
from .models import HelpRequest, LightningStatus, Shop, is_demo
from .notifications import NotificationFailed, alert_name, alert_text, plain, telegram

logger = logging.getLogger(__name__)

# Requests per shop in a rolling day: enough for a bad day, few enough that nobody floods the moderators' chat
ASK_WINDOW = timedelta(hours=24)
# The same symptom asked again within this time is a double tap, not a second request
DOUBLE_TAP = timedelta(minutes=2)
NOTE_LENGTH = 200
# A solved request stays in the panel this long, so that the merchant sees «Solved»
SOLVED_SHOWN = timedelta(days=3)
HISTORY = 5
# Seconds for all the checks: «Look again» is the merchant waiting at the page, the request is made with the customer
# waiting. The probe keeps its own shorter limit (diagnosis.LIGHTNING_SECONDS).
LOOK_SECONDS = 20
ASK_SECONDS = 10
SUMMARY_LENGTH = 4000
SUMMARY_CODES = 8
SENT = HelpRequest.Delivery.SENT
Symptom = HelpRequest.Symptom
Channel = HelpRequest.Channel

# Findings of this module (the diagnosis' own are in shops/diagnosis.py): same shape
OK, WARNING, PROBLEM = "ok", "warning", "problem"


@dataclass(frozen=True)
class Kind:
    key: str
    hint: object
    steps: tuple
    # The names of the checks of shops/diagnosis.py, without the «check_» (resolved when asked: that module is imported late)
    local: tuple
    deep: tuple = ()
    # The watch on the Lightning address, which is not a check of the diagnosis
    watch: bool = False

    @property
    def label(self):
        return Symptom(self.key).label


KINDS = {kind.key: kind for kind in (
    Kind("pay", gettext_lazy("The code does not scan, the wallet refuses it, or the register shows no code."), (
        gettext_lazy("Start the sale again from the register: a new code appears. An old code can stop working."),
        gettext_lazy("Ask the customer to scan the code from inside their bitcoin wallet app, not with the camera of the phone, "
          "and to check that they have enough in the wallet."),
        gettext_lazy("If the register shows no code or the page does not load, look at the phone's connection and reload the "
          "page."),
        gettext_lazy("A customer with no wallet at all: show them the card «Card for customers without a wallet» (in your panel, "
          "under your shop). The phone guides them."),
        gettext_lazy("Do not keep the customer waiting: if nothing works, they can pay another way. Then ask us for help below."),
    ), local=("shop", "waiting", "register", "closure"), deep=("lightning",), watch=True),
    Kind("waiting", gettext_lazy("The customer says they paid, but the register keeps waiting."), (
        gettext_lazy("Do not ask the customer to pay again, and do not hand over the goods yet."),
        gettext_lazy("Open your wallet app: did the money arrive? If it did, the payment is good even if the register is still "
          "waiting. Then ask us for help below: we find out why the register did not notice."),
        gettext_lazy("The money is not in your wallet: a Lightning payment is instant, so ask the customer to look at their "
          "wallet and try again."),
        gettext_lazy("The list of sales at the bottom of the register, on the phone that took the sale, shows how each sale ended."),
    ), local=("shop", "waiting", "register", "closure"), deep=("lightning",), watch=True),
    Kind("open", gettext_lazy("The register page does not load, asks you to sign in, or a person of your staff cannot open theirs."), (
        gettext_lazy("Reload the page and look at the connection of the phone: wifi or data."),
        gettext_lazy("If it asks you to sign in, sign in again from your panel: with your wallet, or with the link we send to "
          "your email."),
        gettext_lazy("A register of your staff: the code on its card works once and lasts 24 hours. If it has expired, make a new "
          "one in «Registers for your staff»."),
        gettext_lazy("If the page does not load for anyone, the site may be resting for a few minutes."),
        gettext_lazy("A phone where the register was opened before keeps taking Lightning payments, even when the site "
                     "does not answer."),
    ), local=("shop", "waiting", "owner", "register", "staff", "account", "closure")),
    Kind("phone", gettext_lazy("You can no longer sign in from the phone you used, or someone else may have it."), (
        gettext_lazy("Your money is safe: it is in your wallet, not in the phone and not on this site."),
        gettext_lazy("On the new phone, sign in here with your wallet, or with the link we send to your email, if you gave us "
          "one."),
        gettext_lazy("Open your register from your panel and add it to the home screen of the new phone."),
        gettext_lazy("If somebody else may use the old phone: turn off the registers of your staff that were on it, in "
          "«Registers for your staff». Your own register only takes payments, and they go to your wallet."),
        gettext_lazy("If your wallet is new too, ask us for help below: only you can connect the new wallet, and we show you how."),
    ), local=("owner", "account", "staff", "waiting")),
    Kind("change", gettext_lazy("You gave back too much, or a customer paid more than the price."), (
        gettext_lazy("A customer paid more than the price: the extra is theirs. Give it back from your wallet."),
        gettext_lazy("You gave too much change: the register cannot take it back, and a payment already made does not change."),
        gettext_lazy("If you are not sure what happened, ask us for help below."),
    ), local=("waiting",)),
    Kind("other", gettext_lazy("Anything else: tell us in a line, and we look at everything the site knows."), (
        gettext_lazy("Write in a line what you see and when it started."),
        gettext_lazy("If a customer is waiting, let them pay another way first: we answer afterwards."),
    ), local=("shop", "waiting", "owner", "register", "first_day", "closure", "staff", "account"),
        deep=("lightning",)),
)}


def kind(key):
    return KINDS.get(key)


def checks(found, deep):
    """The functions of shops/diagnosis.py behind the names of a kind: only the local ones, or all of them."""
    from . import diagnosis

    names = set(found.local) | (set(found.deep) if deep else set())
    return {getattr(diagnosis, f"check_{name}") for name in names}


def facts(shop, found):
    """What the site keeps on the shop besides the diagnosis' checks, as findings: the guardian's last verdict on the
    Lightning address. Only the database."""
    from .diagnosis import Finding

    if not found.watch:
        return []
    out = []
    watch = lightning_watch.panel(shop)
    if watch and watch["broken"]:
        if watch["status"] == LightningStatus.NO_CONFIRMATION:
            text = _("The site watches your Lightning address: it answers, but it does not tell the register when a "
                     "payment arrives (since {since}, checked {checked}).")
        elif watch["never"]:
            text = _("The site watches your Lightning address: it has never answered (checked {checked}).")
        else:
            text = _("The site watches your Lightning address: it has not answered since {since} (checked {checked}).")
        text = text.format(since=watch["since"], checked=watch["checked"])
        out.append(Finding("address_broken", PROBLEM, f"{text} {watch['customer']}",
                           f"guardian: {shop.lightning_status} since {shop.lightning_since}"))
    elif watch:
        out.append(Finding("address_works", OK, _("The site watches your Lightning address: it worked when it last "
                                                  "checked, {checked}.").format(checked=watch["checked"]),
                           f"guardian: ok, last worked {shop.lightning_worked}"))
    return out


def steps_for(shop, found):
    """The steps of a symptom for this shop."""
    return list(found.steps)


@dataclass
class Look:
    kind: Kind
    findings: list
    deep: bool
    steps: list

    @property
    def problems(self):
        return [item for item in self.findings if item.level != OK]

    @property
    def fine(self):
        return [item for item in self.findings if item.level == OK]

    @property
    def worst(self):
        return PROBLEM if any(item.level == PROBLEM for item in self.findings) else (
            WARNING if self.problems else OK)


def look(shop, found, deep=False):
    """What the site knows about the shop for this symptom. `deep` also asks the shop's Lightning
    provider (the diagnosis' time limits, LOOK_SECONDS in all); without it nothing leaves the machine."""
    from .diagnosis import LEVELS, diagnose

    findings = diagnose(shop, LOOK_SECONDS, only=checks(found, deep), owner_view=True) + facts(shop, found)
    findings.sort(key=lambda item: LEVELS.index(item.level))
    return Look(found, findings, deep, steps_for(shop, found))


# ---------------------------------------------------------------------------------------------------------------------
# The request
# ---------------------------------------------------------------------------------------------------------------------

def channels(shop):
    """How the merchant can be called back: what the shop already has, and always «I will look at my panel»."""
    from .diagnosis import masked

    found = []
    email = lightning_watch.owner_email(shop)
    if email:
        found.append((Channel.EMAIL, _("By email, to {email}").format(email=masked(email))))
    if shop.telegram_chat:
        found.append((Channel.TELEGRAM, _("On Telegram, in the chat of your shop")))
    if shop.phone:
        found.append((Channel.PHONE, _("By phone, to {phone} (the one in your shop's details)").format(phone=plain(shop.phone))))
    found.append((Channel.PAGE, _("I will look at my panel: it says when it is solved")))
    return found


def clean_note(text):
    """One line of plain text: whitespace made single, control and direction characters turned into «?»."""
    return plain(" ".join(str(text).split()))


class AskForm(forms.Form):
    symptom = forms.ChoiceField(choices=Symptom.choices, widget=forms.HiddenInput)
    note = forms.CharField(required=False, max_length=NOTE_LENGTH, label=gettext_lazy("In a line: what do you see?"),
                           widget=forms.TextInput(attrs={"maxlength": NOTE_LENGTH, "autocomplete": "off"}),
                           error_messages={"max_length": gettext_lazy("Please keep it to a line of up to %(limit_value)d characters.")})
    channel = forms.ChoiceField(widget=forms.RadioSelect, label=gettext_lazy("How do you want us to call you back?"))

    def __init__(self, *args, shop, **kwargs):
        super().__init__(*args, **kwargs)
        self.shop = shop
        self.fields["channel"].choices = channels(shop)
        self.fields["channel"].initial = self.fields["channel"].choices[0][0]

    def clean_note(self):
        note = clean_note(self.cleaned_data["note"])
        if secret_warning(note):
            raise forms.ValidationError(_("This looks like a secret (a private key or the words of a wallet): never "
                                          "write it here. We never ask for it."))
        return note


class TooMany(Exception):
    """The shop has asked limits.help_requests_per_shop_day times in the last day."""


class Full(Exception):
    """The site keeps limits.help_requests requests already: the merchant is told to write instead."""


NEW, AGAIN, ADDED = "new", "again", "added"


def create(shop, symptom, note, channel, asked_by, now=None):
    """(request, NEW) a new one; (request, AGAIN) the same symptom asked a moment ago by the same person (a double tap),
    or (request, ADDED) when the second tap carried another line, which is added to the first request; TooMany at the
    limit. Counted under a lock on the shop, so that two requests together do not both pass on the last place."""
    now = now or timezone.now()
    with transaction.atomic():
        Shop.objects.select_for_update().filter(pk=shop.pk).first()
        recent = list(shop.help_requests.filter(created__gte=now - ASK_WINDOW))
        again = next((row for row in recent if row.symptom == symptom and not row.resolved and row.asked_by_id == asked_by.pk
                      and row.created >= now - DOUBLE_TAP), None)
        if again:
            if note and note not in again.note:
                again.note = f"{again.note} / {note}"[:NOTE_LENGTH] if again.note else note
                again.save(update_fields=["note"])
                return again, ADDED
            return again, AGAIN
        if limits.get("help_requests_per_shop_day") and len(recent) >= limits.get("help_requests_per_shop_day"):
            raise TooMany
        if limits.refuse("help_requests"):
            raise Full
        return HelpRequest.objects.create(shop=shop, symptom=symptom, note=note, channel=channel, created=now,
                                          asked_by=asked_by), NEW


SIGNS = {"problem": "✗", "warning": "!", "ok": "✓"}


def summary_lines(findings):
    """The diagnosis as the helper reads it: one line per finding, its detail under it. The details of the diagnosis have
    no secret by design (shops/diagnosis.py), and each line goes through `plain`."""
    lines = []
    for item in findings:
        lines.append(plain(f"{SIGNS[item.level]} [{item.code}] {item.say}"))
        if item.detail:
            lines.append(plain(f"    {item.detail}"))
    text = "\n".join(lines)
    return text if len(text) <= SUMMARY_LENGTH else text[:SUMMARY_LENGTH - 1] + "…"


def codes_line(findings, level):
    codes = [item.code for item in findings if item.level == level]
    shown = ", ".join(codes[:SUMMARY_CODES])
    return shown + (f" (+{len(codes) - SUMMARY_CODES})" if len(codes) > SUMMARY_CODES else "")


def message(row, findings):
    """The text for the moderators' chat, in the association's language: the shop, the symptom, the line the merchant
    wrote as plain text, the codes of the diagnosis (never its sentences or details: they name addresses) and the link."""
    with translation.override(settings.LANGUAGE_CODE):
        from .management.commands.dashboard import site_label

        shop = row.shop
        # The site first: development and production may write to the same chat
        lines = [site_label(), _("{shop} asks for help.").format(shop=alert_name(shop)),
                 _("Symptom: {symptom}").format(symptom=row.get_symptom_display()),
                 _("Call back: {channel}").format(channel=row.get_channel_display())]
        if row.note:
            lines.append(_("They wrote: «{note}»").format(note=alert_text(row.note, NOTE_LENGTH)))
        problems, warnings = codes_line(findings, "problem"), codes_line(findings, "warning")
        if problems:
            lines.append(_("Problems: {codes}").format(codes=problems))
        if warnings:
            lines.append(_("To look at: {codes}").format(codes=warnings))
        if not (problems or warnings):
            lines.append(_("The checks found nothing wrong."))
        link = f"{settings.PUBLIC_URL}{reverse('admin:shops_helprequest_change', args=[row.pk])}"
        lines.append(_("The request and its diagnosis: {link}").format(link=link))
        return "\n".join(lines)


def tell(row, findings, notify=True):
    """Tell the moderators' chat; the state of it, as HelpRequest.Delivery. A demo shop (a training site) never writes
    to the chat of the people who look after the real site. Without `notify` (the caller's limits on messages to the chat
    are spent) nothing is sent: the request is only on the dashboard, and the merchant is told so."""
    if is_demo(row.shop):
        return HelpRequest.Delivery.OFF
    if not notify:
        return HelpRequest.Delivery.FAILED
    try:
        answer = telegram(settings.TELEGRAM_MODERATION_CHAT, message(row, findings))
    except NotificationFailed:
        return HelpRequest.Delivery.FAILED
    except Exception as error:  # noqa: BLE001 - the request is saved; nothing here may lose it
        logger.error("Help request %s: the message to the moderators failed: %s", row.pk, type(error).__name__)
        return HelpRequest.Delivery.FAILED
    if answer is None:
        return HelpRequest.Delivery.OFF
    return HelpRequest.Delivery.SENT if answer else HelpRequest.Delivery.REFUSED


def finish(row, notify=True):
    """The diagnosis of the request, written on it, then the message to the moderators. Never raises: a request that
    could not be completed stays, without a summary, on the dashboard."""
    from .diagnosis import LEVELS, diagnose

    found = KINDS[row.symptom]
    findings = []
    with translation.override(settings.LANGUAGE_CODE):
        try:
            findings = diagnose(row.shop, ASK_SECONDS, only=checks(found, True)) + facts(row.shop, found)
            findings.sort(key=lambda item: LEVELS.index(item.level))
            row.summary = summary_lines(findings)
        except Exception as error:  # noqa: BLE001 - see above
            logger.error("Help request %s: the diagnosis failed: %s", row.pk, type(error).__name__)
            row.summary = f"(the diagnosis could not be made: {type(error).__name__})"
    row.delivery = tell(row, findings, notify)
    row.save(update_fields=["summary", "delivery"])
    return row


# ---------------------------------------------------------------------------------------------------------------------
# What the merchant sees afterwards
# ---------------------------------------------------------------------------------------------------------------------

def state(row):
    """A request as the panel and the help page say it."""
    return {"request": row, "open": row.resolved is None, "when": lightning_watch.when(row.created),
            "solved_when": lightning_watch.when(row.resolved) if row.resolved else "",
            "delivered": row.delivery == SENT, "symptom": row.get_symptom_display(),
            "channel": row.get_channel_display()}


def own(shop):
    """The requests the shop's owner now made: after a change of owner the new one does not read the old one's."""
    if shop.owner_id is None:
        return HelpRequest.objects.none()
    return shop.help_requests.filter(asked_by_id=shop.owner_id)


def history(shop):
    """The latest requests of the shop's owner, newest first, as `state` says them."""
    return [state(row) for row in own(shop)[:HISTORY]]


def panel(shop, now=None):
    """For the panel: None, or the request still open, or the one solved in the last SOLVED_SHOWN. One query."""
    now = now or timezone.now()
    rows = list(own(shop)[:HISTORY])
    chosen = next((row for row in rows if row.resolved is None), None) or next(
        (row for row in rows if row.resolved >= now - SOLVED_SHOWN), None)
    return state(chosen) if chosen else None


def open_request(shop):
    row = own(shop).filter(resolved__isnull=True).first()
    return state(row) if row else None

"""A shop that closes for a while: holidays, works, a bereavement, an illness. It stays on the map, says so kindly, and
nothing raises an alarm for the days it was closed.

A closure (`Closure`) is a range of days of the community's calendar, never moments: `first_day` is the first closed
day, `reopens_on` the first day open again, empty while the owner has not said (they reopen when they say so). Days,
not instants, so that the hour, the time zone of whoever looks and the change of summer time cannot move a day: "today"
is always `day_of()`, the day in the community's time zone.

What a closure changes, and what it does not:
- The map, /shops.json, the shop page and the Nostr event say "closed for holidays, back on 17 August". The shop does
  not leave the map and the counters, and its pin does not change.
- The icons of how recently the Lightning address worked (`certified.band`) count only the days the shop was open. A
  budget keeps it honest: of the last 365 days at most `MAX_CREDIT` closed ones are forgiven in all (the first ones in
  time), so that a closure "until I say so", or closures one after the other, cannot keep the icons alive for ever.
- The certificate does not move: a certified shop stays certified, it is a fact of the past.
- The dashboard, the morning summary and the guardian of the Lightning address do not raise alarms for a shop that is
  closed today (`is_quiet`). The guardian keeps probing; what it finds is said when the shop reopens (`welcome_back`,
  `reopen_today`).
- The register is never touched: a closed shop can sell, and its money goes where it always went.

The owner changes it from the panel (`shops/vacation_views.py`), the association from /admin/."""
import logging
from datetime import date, timedelta
from smtplib import SMTPException

from django import forms
from django.conf import settings
from django.db import transaction
from django.utils import dateformat, timezone
from django.utils.translation import gettext, override
from django.utils.translation import gettext_lazy as _

from config import features
from config.community import community

from .models import Closure, Shop
from .notifications import alert_name

logger = logging.getLogger(__name__)

# Closed days forgiven by the icons and the path, in all and over the last BUDGET_DAYS days, whatever the payments (a
# payment restarts the count of days since the last one, never this budget). Beyond it the shop looks quiet again, which
# it is
MAX_CREDIT = 92
BUDGET_DAYS = 365
# A closure still to come is said in public this many days before it starts
SOON_DAYS = 7
# Longest closure the form accepts
LONGEST = 366
# The welcome back card stays in the panel this many days after the shop reopened; the message goes out from this hour
WELCOME_DAYS = 14
WELCOME_HOUR = 8

WHAT = {Closure.Reason.HOLIDAY: _("Closed for holidays"), Closure.Reason.WORKS: _("Closed for works"),
        Closure.Reason.PERSONAL: _("Closed for personal reasons"), Closure.Reason.OTHER: _("Closed for a while")}
# The four sentences of the public notice. Translators: {what} is one of the sentences above, the dates are written
# with the language's own format
CLOSED_BACK = _("{what} · back on {date}")
CLOSED_OPEN = _("{what} · back soon")
SOON_BACK = _("{what} from {date} · back on {back}")
SOON_OPEN = _("{what} from {date}")


def day_of(moment=None):
    """The day of `moment` (now) in the community's calendar."""
    return timezone.localtime(moment or timezone.now(), timezone.get_default_timezone()).date()


def enabled():
    return features.enabled(features.VACATION)


def stored(shop):
    """Every closure written for the shop, from the prefetch when the caller made one. [] for a shop not saved yet (its
    id exists already, and there is nothing to ask the database)."""
    return [] if shop._state.adding else list(shop.closures.all())


def closures(shop):
    """The closures the site knows about. When the community turned the feature off (`features.vacation: false`) there are
    none: the map, the shop page, /shops.json, Nostr, the icons and the path all behave as if the shop had never closed.
    The rows stay in the database for when it is turned on again. The one thing a stored closure still does is `is_quiet`."""
    return stored(shop) if enabled() else []


def covers(closure, day):
    return closure.first_day <= day and (closure.reopens_on is None or day < closure.reopens_on)


def latest_end(closure):
    """Sort key: the closure that ends last (one with no day to open again, last of all)."""
    return closure.reopens_on or date.max, closure.first_day, closure.pk


def current(shop, day=None):
    """The closure that covers `day` (today): the one that ends last when several do (only /admin/ can make two overlap),
    so the shop never says it opens before it does."""
    day = day or day_of()
    return max((closure for closure in closures(shop) if covers(closure, day)), key=latest_end, default=None)


def upcoming(shop, day=None):
    """The next closure that starts after `day`."""
    day = day or day_of()
    return min((closure for closure in closures(shop) if closure.first_day > day), key=lambda c: (c.first_day, c.pk),
               default=None)


def holds(shop, day):
    """True if any closure written for the shop covers `day`, whether or not the community has the feature on: the shop
    has not reopened while one does."""
    return any(covers(closure, day) for closure in stored(shop))


def is_closed(shop, day=None):
    """True if the shop is closed for a while on `day` (today). A historic shop is closed for good, not for a while."""
    return not shop.closed_on and current(shop, day) is not None


def is_quiet(shop, day=None):
    """Closed for a while today: the days nobody raises an alarm for (the dashboard, the guardian). The site sees no
    sale, so it cannot tell a shop that sells while it says it is closed: the fault kept quiet is still a line for the
    association.

    With the feature turned off the site no longer says anything about closures, but a shop that is away right now (its
    owner wrote the closure while the feature was on) must not raise false alarms for the days it said it would be
    away: a closure with a day to open again still keeps the alarms quiet until that day, and not a day longer; and no
    longer than MAX_CREDIT days from its first day, so a fault never stays quiet for months. One with no end ("until I
    say so") does not: with the feature off nobody can end it, so it could silence a real fault for ever. The fault
    kept quiet is still a line for the association in the dashboard (`closed_lightning`), and is told when the closure
    ends (`welcome_back`)."""
    day = day or day_of()
    if shop.closed_on:
        return False
    if not enabled():
        found = max((closure for closure in stored(shop) if closure.reopens_on is not None and covers(closure, day)
                     and (day - closure.first_day).days <= MAX_CREDIT), key=latest_end, default=None)
    else:
        found = current(shop, day)
    return found is not None


def long_closure(shop, day=None):
    """The closure in course when it has lasted more than MAX_CREDIT days, closures one after the other counted as one:
    the day the chain began, or None. For the association to ask whether the shop is coming back."""
    day = day or day_of()
    found = None if shop.closed_on else current(shop, day)
    if found is None:
        return None
    first, others = found.first_day, closures(shop)
    while True:
        before = [c for c in others if c.first_day < first and (c.reopens_on is None or c.reopens_on >= first)]
        if not before:
            break
        first = min(c.first_day for c in before)
    return first if (day - first).days > MAX_CREDIT else None


def spans(shop, start, end):
    """The closed days of [start, end) as (first, after the last) spans in order, overlapping closures counted once."""
    clipped = sorted((max(c.first_day, start), min(c.reopens_on or end, end)) for c in closures(shop))
    merged = []
    for first, last in clipped:
        if last <= first:
            continue
        if merged and first <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], last))
        else:
            merged.append((first, last))
    return merged


def closed_days(shop, start, end):
    """How many days of [start, end) the shop was closed, overlapping closures counted once."""
    return sum((last - first).days for first, last in spans(shop, start, end))


def forgiven(shop, today):
    """The closed days the icons and the path forgive, as spans: of the last BUDGET_DAYS days (today included) the first
    MAX_CREDIT closed ones in time. Independent of the payments: a payment, one's own too, does not give a new budget."""
    left, found = MAX_CREDIT, []
    for first, last in spans(shop, today - timedelta(days=BUDGET_DAYS - 1), today + timedelta(days=1)):
        take = min((last - first).days, left)
        found.append((first, first + timedelta(days=take)))
        left -= take
        if left <= 0:
            break
    return found


def open_days(shop, since, today):
    """The whole days from `since` to `today` the shop was open: the days that passed, minus the forgiven closed ones
    among them. `since` is the day of a payment, which was a day open; the days after it, up to today included, are the
    ones that passed. Never below zero."""
    elapsed = (today - since).days
    if elapsed <= 0:
        return 0
    start, end = since + timedelta(days=1), today + timedelta(days=1)
    credit = sum(max((min(last, end) - max(first, start)).days, 0) for first, last in forgiven(shop, today))
    return max(elapsed - credit, 0)


def info(shop, day=None):
    """What anyone may know: {"state": "closed" or "soon", "reason", "from", "back"} (dates), or None. Never a free
    text: the owner picks a reason from a list, and "personal reasons" says nothing more."""
    if shop.closed_on:
        return None
    day = day or day_of()
    found = current(shop, day)
    state = "closed"
    if found is None:
        found = upcoming(shop, day)
        state = "soon"
        if found is None or (found.first_day - day).days > SOON_DAYS:
            return None
    return {"state": state, "reason": found.reason, "from": found.first_day, "back": found.reopens_on}


def api(shop, day=None):
    """`info` for /shops.json: the same, with ISO dates."""
    found = info(shop, day)
    if found is None:
        return None
    return {**found, "from": found["from"].isoformat(), "back": found["back"].isoformat() if found["back"] else None}


def long_date(day):
    # Translators: a date written with the letters of Django's date filter, for example "j. F Y" in German
    return dateformat.format(day, gettext("j F Y"))


def text(found):
    """The public notice in the page's language, from `info`. Empty for None."""
    if not found:
        return ""
    what = str(WHAT.get(found["reason"], WHAT[Closure.Reason.OTHER]))
    values = {"what": what, "date": long_date(found["from"] if found["state"] == "soon" else found["back"] or found["from"]),
              "back": long_date(found["back"]) if found["back"] else ""}
    if found["state"] == "closed":
        return (str(CLOSED_BACK) if found["back"] else str(CLOSED_OPEN)).format(**values)
    return (str(SOON_BACK) if found["back"] else str(SOON_OPEN)).format(**values)


def note(shop, day=None):
    return text(info(shop, day))


def texts():
    """The sentences for map.js, which writes the notice itself from the JSON: the dates come in the page's language
    from the browser, the words from here."""
    return {"what": {reason: str(WHAT[reason]) for reason in WHAT}, "closed_back": str(CLOSED_BACK),
            "closed_open": str(CLOSED_OPEN), "soon_back": str(SOON_BACK), "soon_open": str(SOON_OPEN)}


def panel(shop, day=None):
    """The owner's view of the shop's closure: the one that covers today, or the next one, with its dates."""
    day = day or day_of()
    found = current(shop, day)
    state = "closed"
    if found is None:
        found = upcoming(shop, day)
        state = "planned"
    if found is None:
        return None
    words = {"state": "closed" if state == "closed" else "soon", "reason": found.reason, "from": found.first_day,
             "back": found.reopens_on}
    return {"state": state, "closure": found, "first_day": found.first_day, "reopens_on": found.reopens_on,
            "reason": found.reason, "text": text(words)}


def welcome(shop, now=None):
    """The card for the owner after a closure: {"closure", "works"} while the last closure ended at most WELCOME_DAYS ago
    and the owner has not closed the card, None otherwise. `works`: the guardian found the Lightning address working
    on or after the day the shop reopened (`lightning_worked`)."""
    day = day_of(now)
    if shop.closed_on or is_closed(shop, day):
        return None
    ended = [closure for closure in closures(shop) if closure.reopens_on and closure.reopens_on <= day
             and (day - closure.reopens_on).days <= WELCOME_DAYS and closure.seen is None]
    if not ended:
        return None
    latest = max(ended, key=lambda closure: (closure.reopens_on, closure.pk))
    worked = shop.lightning_worked
    return {"closure": latest, "works": bool(worked) and day_of(worked) >= latest.reopens_on}


class BreakForm(forms.Form):
    """«I close from … until …», with a reason. For a closure already started the first day cannot change. The days are
    the community's calendar: «today» is the site's today."""
    reason = forms.ChoiceField(label=_("Why"), choices=Closure.Reason.choices)
    first_day = forms.DateField(label=_("Closed from"), required=False,
                                widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"), input_formats=["%Y-%m-%d"])
    reopens_on = forms.DateField(label=_("I open again on"), required=False, help_text=_("The first day the shop is open."),
                                 widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"), input_formats=["%Y-%m-%d"])
    until_told = forms.BooleanField(label=_("I will say when I open again"), required=False)

    def __init__(self, *args, day, editing=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.day, self.editing = day, editing
        self.started = bool(editing and editing.first_day <= day)
        if not self.is_bound:
            self.initial.setdefault("first_day", editing.first_day if editing else day)
            if editing:
                self.initial.update(reason=editing.reason, reopens_on=editing.reopens_on,
                                    until_told=editing.reopens_on is None)

    def clean(self):
        data = super().clean()
        first = self.editing.first_day if self.started else data.get("first_day")
        if first is None and not self.has_error("first_day"):
            self.add_error("first_day", _("Say the first day of the closure."))
        if first is not None and not self.started and first < self.day:
            self.add_error("first_day", _("Choose today or a day to come."))
            first = None
        data["first_day"] = first
        if data.get("until_told"):
            data["reopens_on"] = None
        elif not self.has_error("reopens_on"):
            back = data.get("reopens_on")
            if back is None:
                self.add_error("reopens_on", _("Say when you open again, or tick that you will say."))
            elif first is not None and back <= first and not (self.started and back == self.day):
                self.add_error("reopens_on", _("You open again after the day you close."))
            elif self.started and back < self.day:
                self.add_error("reopens_on", _("Choose today or a day to come."))
            elif first is not None and (back - first).days > LONGEST:
                self.add_error("reopens_on", _("That is more than a year: choose an earlier day, or tick that you will say."))
        return data


def save(shop, data, day, now=None):
    """Writes what the form said as the shop's one closure to come or in course. Under the shop's row lock, so that
    two requests do not make two. Returns the closure, or None if the shop is open from today (a closure that would
    open and close the same day is no closure). A shop that reopens today by choice is not welcomed by message: the
    owner is here, but the association hears about a fault of the Lightning address kept quiet meanwhile."""
    now = now or timezone.now()
    reopened = False
    with transaction.atomic():
        Shop.objects.select_for_update().get(pk=shop.pk)
        closure = current_for_edit(shop, day)
        if closure is None:
            closure = Closure(shop=shop)
        started = closure.pk is not None and closure.first_day <= day
        if not started:
            closure.first_day = data["first_day"]
        closure.reason = data["reason"]
        closure.reopens_on = data["reopens_on"]
        if closure.reopens_on is not None and closure.reopens_on <= closure.first_day:
            if closure.pk:
                closure.delete()
                reopened = started
            saved = None
        else:
            if closure.reopens_on is not None and closure.reopens_on <= day:
                closure.welcomed = now
                reopened = started
            closure.save()
            saved = closure
    if reopened:
        tell_association_if_broken(shop)
    return saved


def current_for_edit(shop, day):
    # Straight from the database: under the lock, a prefetch from before it could be old
    rows = list(Closure.objects.filter(shop=shop).order_by("first_day", "pk"))
    covering = [c for c in rows if covers(c, day)]
    if covering:
        return max(covering, key=latest_end)
    later = [c for c in rows if c.first_day > day]
    return later[0] if later else None


def close_now(shop, reason, day, now=None):
    """«Closed from today, I open again when I say so»: one tap in the panel."""
    return save(shop, {"first_day": day, "reopens_on": None, "reason": reason}, day, now)


def reopen_today(shop, day, now=None):
    """The owner is back: every closure in course ends today (one that began today is dropped), one still to come is
    dropped when there is none in course. Returns what happened: "reopened", "cancelled" or None when there was nothing
    to do. A shop that was closed hears nothing from the guardian meanwhile, so the association is told now about a fault
    of its Lightning address, whichever way it reopens."""
    now = now or timezone.now()
    result, was_closed = None, False
    with transaction.atomic():
        Shop.objects.select_for_update().get(pk=shop.pk)
        rows = list(Closure.objects.filter(shop=shop).order_by("first_day", "pk"))
        covering = [c for c in rows if covers(c, day)]
        for closure in covering:
            was_closed = True
            if closure.first_day >= day:
                closure.delete()
                result = result or "cancelled"
            else:
                closure.reopens_on = day
                closure.welcomed = now
                closure.save(update_fields=["reopens_on", "welcomed"])
                result = "reopened"
        later = [c for c in rows if c.first_day > day]
        if not covering and later:
            later[0].delete()
            result = "cancelled"
    if was_closed:
        tell_association_if_broken(shop)
    return result


def tell_association_if_broken(shop):
    """The guardian kept the Lightning address and said nothing during the closure: the association hears about a fault at
    the reopening, as it would have the day it started."""
    from . import lightning_watch

    shop = Shop.objects.get(pk=shop.pk)
    watch = lightning_watch.panel(shop)
    if watch and watch["broken"]:
        lightning_watch.tell_association([(shop, lightning_watch.BROKE)])


def mark_seen(shop, pk, now=None):
    return Closure.objects.filter(shop=shop, pk=pk, seen__isnull=True).update(seen=now or timezone.now())


def welcome_text(shop, broken):
    """Subject and body of the welcome back message, in the community's main language."""
    # The name as the alerts write it (one line, no web address, no mention): the same text goes to a Telegram chat the
    # owner chose, which can be anyone's, with the community's bot as the sender
    values = {"shop": alert_name(shop)}
    subject = gettext("{shop}: welcome back").format(**values)
    body = [gettext("Welcome back! Your shop {shop} is open again.").format(**values)]
    if broken:
        # The register did work while the shop was closed, but the address does not now, and may not have for long: no
        # "while you were away", and the small payment waits for it to work
        body += [gettext("One thing to look at: your Lightning address does not work now, and customers cannot pay you "
                         "with it. Your panel says what to do."),
                 gettext("When it works again, try a small payment of your own: open the register and pay it from "
                         "another phone.")]
    else:
        body.append(gettext("The register worked while you were away, so there is nothing to turn on. To be sure that "
                            "everything works, try a small payment of your own: open the register and pay it from "
                            "another phone."))
    return f"{community()['name']}: {subject}", "\n\n".join(body)


def welcome_back(now=None):
    """Tells the owner «welcome back, try a small payment» once for each closure that ended on its day, from WELCOME_HOUR
    of the community's morning on the day the shop reopens (or the next ones, up to three days late if a run was
    missed), by email and on the shop's Telegram chat, whichever the owner has. A fault of the Lightning address found
    meanwhile is said in the same message and to the association. Returns the shops told. Run by check_lightning. When the
    community turned the feature off nobody is welcomed back (the closure is marked all the same), but the association is
    still told of a fault of the address on the day the closure ends: the guardian kept quiet during it (`is_quiet`) and
    nobody else would ever say."""
    from . import lightning_watch
    from .notifications import NotificationFailed, telegram

    now = now or timezone.now()
    day = day_of(now)
    if timezone.localtime(now, timezone.get_default_timezone()).hour < WELCOME_HOUR:
        return []
    due = Closure.objects.filter(reopens_on__lte=day, reopens_on__gte=day - timedelta(days=3), welcomed__isnull=True)
    told = []
    for closure in due.select_related("shop"):
        # Taken first, by a conditional update: a second run at the same time finds it taken
        if not Closure.objects.filter(pk=closure.pk, welcomed__isnull=True).update(welcomed=now):
            continue
        shop = Shop.objects.get(pk=closure.shop_id)
        if shop.closed_on or holds(shop, day):
            continue
        if not enabled():
            tell_association_if_broken(shop)
            continue
        watch = lightning_watch.panel(shop)
        broken = bool(watch and watch["broken"])
        with override(settings.LANGUAGE_CODE):
            subject, body = welcome_text(shop, broken)
        email = lightning_watch.owner_email(shop)
        if email:
            try:
                from django.core.mail import send_mail

                send_mail(subject, body, None, [email])
            except (OSError, SMTPException, ValueError):
                logger.error("Welcome back email not delivered for shop %s", shop.pk)
        if shop.telegram_chat:
            try:
                telegram(shop.telegram_chat, body)
            except NotificationFailed:
                logger.error("Welcome back message not delivered for shop %s", shop.pk)
        if broken:
            lightning_watch.tell_association([(shop, lightning_watch.BROKE)])
        told.append(shop)
    return told

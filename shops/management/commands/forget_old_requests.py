"""Forgets the personal data of the requests that were closed long ago, and keeps the rows for the counts.

A request for help (shops/helpdesk.py) keeps, besides what counting needs (the shop, the symptom, when it was asked, when it
was solved, whether the message to the association went out), things about a person: the line the merchant wrote, a copy of
what the site knew about the shop when they asked (the diagnosis: the Lightning address, the start of the wallet key, when the
owner last signed in, the recovery email as m***@e***.org), the account that asked, how they wanted to be called back and the
volunteer who solved it. A year after the merchant was answered none of it is of any use, and it is personal data: it goes.
Each of those fields is put back to what a new row has (an empty text, no account, the call back «they will look at their
panel»), and the row stays, with its shop, symptom and dates, so that the counts do not change.

Only what is closed, and closed long ago: `closed` is the field that holds the day a row was closed (not the day it was asked).
A request still open or waiting has no such day and is left alone, whatever its age; so is one closed less than --days ago
(365 to start), and one that was reopened. A row that already forgot is not counted again: a second run does nothing and says 0.

Safe to run every day, and does: deploy/dashboard.sh runs it before the morning summary.
The days are --days or, without it, `privacy.help_requests_days` of community.yml (shops/privacy.py; 365 if it is not written, and `0` or `false` there
is «never from the timer»: the command then says so and does nothing). Under FLOOR days (a month) it refuses without --force:
the panel shows a solved request for three days, and a slip of the finger must not empty a week. And it does nothing when the
site's own clock looks wrong: if the last reading the site wrote on a shop is more than two days old, or in the future, "now" is
not to be trusted, and what is forgotten cannot be given back.

Another kind of request that keeps a person's words is one more line of RULES: its model, the field that holds the day it was
closed (empty while it is open) and the fields to forget. A field is put back to its default (`null` and no default is an
error, so that a rule cannot be written that breaks a row). Backups made before keep what they kept until they expire."""
from datetime import timedelta
from typing import NamedTuple

from django.apps import apps
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Max, Q
from django.utils import timezone

from shops import privacy
from shops.models import Shop

# The last reading may be this old before the clock is doubted; and a reading «in the future» up to this much is just two
# processes a few minutes apart
CLOCK_SLACK = timedelta(days=2)
CLOCK_SKEW = timedelta(minutes=10)


class Rule(NamedTuple):
    model: str    # "app_label.ModelName"
    closed: str   # the date field set when the request is closed: empty while it is open or waiting
    forget: tuple  # the fields put back to what a new row has


RULES = (
    Rule("shops.HelpRequest", "resolved", ("note", "summary", "channel", "asked_by", "resolved_by")),
    Rule("shops.OsmRequest", "done", ("summary", "answer", "asked_by", "done_by")),
)


def neutral(model, name):
    """What a new row has in this field: its default, or null. A field with neither cannot be forgotten."""
    field = model._meta.get_field(name)
    value = field.get_default()
    if value is None and not field.null:
        raise CommandError(f"{model._meta.label}.{name} has no default and cannot be null: it cannot be forgotten")
    return value


def kept(model, forget):
    """The rows that still hold something of the fields to forget: the ones a second run has nothing to do with are not these."""
    holding = Q()
    for name in forget:
        value = neutral(model, name)
        holding |= Q(**{f"{name}__isnull": False}) if value is None else ~Q(**{name: value})
    return holding


def last_reading():
    """The latest moment the site itself wrote on a shop when it read something (the Lightning address), or None when it
    never did: a new site, or one with no shops, has nothing to compare the clock with."""
    return Shop.objects.aggregate(last=Max("lightning_checked"))["last"]


def clock_problem(now):
    """Why the clock is not to be trusted, or an empty text."""
    last = last_reading()
    if last is None:
        return ""
    if last > now + CLOCK_SKEW:
        return f"the last reading the site wrote is {last:%Y-%m-%d %H:%M} UTC, later than now ({now:%Y-%m-%d %H:%M} UTC)"
    if now - last > CLOCK_SLACK:
        return (f"the last reading the site wrote is from {last:%Y-%m-%d %H:%M} UTC, more than two days before now "
                f"({now:%Y-%m-%d %H:%M} UTC)")
    return ""


class Command(BaseCommand):
    help = "Forgets the merchant's words and the account in the requests closed more than --days ago (365), and keeps the rows"

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=None,
                            help="days since a request was closed (privacy.help_requests_days of community.yml, 365 if not written)")
        parser.add_argument("--force", action="store_true", help=f"allow --days under {privacy.FLOOR}")
        parser.add_argument("--dry-run", action="store_true", help="say what would be forgotten, and forget nothing")

    def handle(self, *args, days, force, dry_run, **options):
        if days is None:
            days = privacy.days("help_requests_days")
            if days is None:
                self.stdout.write("privacy.help_requests_days of community.yml is 0 or false: the site does not forget by itself. "
                                  "Nothing done (--days <number> forgets by hand).")
                return
        elif days < 0:
            raise CommandError("--days is a number of days, zero or more")
        elif days < privacy.FLOOR and not force:
            raise CommandError(f"--days {days} is under {privacy.FLOOR}: a solved request is shown to the merchant for three days, "
                               "and what is forgotten cannot be given back. Add --force if that is what you mean. Nothing changed.")
        now = timezone.now()
        try:
            before = now - timedelta(days=days)
        except OverflowError:
            raise CommandError(f"--days {days} is too many days. Nothing changed.") from None
        problem = clock_problem(now)
        if problem:
            raise CommandError(f"The clock may be wrong: {problem}. Nothing forgotten. Run check_lightning and "
                               "look at the date of the machine; the timer tries again.")
        for rule in RULES:
            model = apps.get_model(rule.model)
            over = model.objects.filter(**{f"{rule.closed}__lt": before}).filter(kept(model, rule.forget))
            label = model._meta.label
            if dry_run:
                self.stdout.write(f"Dry run: {over.count()} {label} closed before {before:%Y-%m-%d} would be forgotten.")
                continue
            count = over.update(**{name: neutral(model, name) for name in rule.forget})
            self.stdout.write(f"{count} {label} closed before {before:%Y-%m-%d} forgotten.")

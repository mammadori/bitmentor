import uuid
import zoneinfo

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.utils.translation import pgettext_lazy

from . import conf
from .keys import to_npub


def valid_zone(name):
    try:
        zoneinfo.ZoneInfo(name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError, OSError):
        raise ValidationError(_("This is not a time zone name: write it like Europe/Rome."), code="zone") from None


def default_zone():
    return settings.TIME_ZONE


def https_link(value):
    if not conf.safe_link(value):
        raise ValidationError(_("A link starts with https:// and has no spaces."), code="link")


class TrustedAuthor(models.Model):
    """A Nostr key whose calendar events the board reads (`read_meetups`). The staff keeps the list in the administration,
    with a note on who certified the key and why; `meetups.trusted` in community.yml adds more."""
    pubkey = models.CharField(_("key"), max_length=64, unique=True, validators=[
        RegexValidator(r"\A[0-9a-f]{64}\Z", _("A key is an npub1… or 64 hexadecimal digits."))],
        help_text=_("The npub1… of the organiser, or the same key in 64 hexadecimal digits."))
    note = models.CharField(_("who certified it"), max_length=300,
                            help_text=_("Who vouches for this author, and since when: the next person to read the list "
                                        "has no one else to ask."))
    active = models.BooleanField(_("read"), default=True, help_text=_("Untick to stop reading it. What it published stays."))
    added_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, editable=False,
                                 related_name="+")
    created = models.DateTimeField(_("added"), auto_now_add=True)

    class Meta:
        ordering = ["created"]
        verbose_name = _("trusted author")
        verbose_name_plural = _("trusted authors")

    def __str__(self):
        return f"{to_npub(self.pubkey)[:16]}…"

    @property
    def npub(self):
        return to_npub(self.pubkey)


class Meetup(models.Model):
    """A meeting of the community: when, where, what, and whether it is happening. The staff writes them in the
    administration; the reader of Nostr (`read_meetups`) adds the ones certified organisers publish. A meeting that is
    cancelled or postponed stays on the board as such. Nothing here is about people: no names, no photos, no counts."""

    class Status(models.TextChoices):
        # With a context: "Cancelled" is already in the catalogue for a sale, and in Italian it is feminine
        CONFIRMED = "confirmed", pgettext_lazy("meetup state", "Confirmed")
        CANCELLED = "cancelled", pgettext_lazy("meetup state", "Cancelled")
        POSTPONED = "postponed", pgettext_lazy("meetup state", "Postponed")
        INFORMAL = "informal", pgettext_lazy("meetup state", "Informal get-together")

    class Source(models.TextChoices):
        STAFF = "staff", _("Staff")
        NOSTR = "nostr", _("Nostr")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    starts_at = models.DateTimeField(_("starts"), db_index=True)
    all_day = models.BooleanField(_("no time, only the day"), default=False)
    tzid = models.CharField(_("time zone"), max_length=64, default=default_zone, validators=[valid_zone],
                            help_text=_("The time is written and shown in this zone, like Europe/Rome."))
    city = models.CharField(_("city"), max_length=100, blank=True)
    place = models.CharField(_("place"), max_length=200, blank=True)
    title = models.CharField(_("title or topic"), max_length=200, blank=True)
    url = models.CharField(_("link"), max_length=conf.MAX_LINK, blank=True, validators=[https_link],
                           help_text=_("The group or the page of the event, with https://. Without it, the board links "
                                       "the community's group, if it has one."))
    status = models.CharField(_("status"), max_length=10, choices=Status.choices, default=Status.CONFIRMED, db_index=True)
    moved_to = models.DateField(_("postponed to"), null=True, blank=True,
                                help_text=_("For a postponed meetup: the day it moves to, if it is already known."))
    visible = models.BooleanField(_("shown on the site"), default=True,
                                  help_text=_("Untick to take it off the board and the calendar without losing it."))
    source = models.CharField(_("source"), max_length=5, choices=Source.choices, default=Source.STAFF, editable=False)
    nostr_address = models.CharField(max_length=300, blank=True, editable=False)
    nostr_event_id = models.CharField(max_length=64, blank=True, editable=False)
    nostr_author = models.CharField(max_length=64, blank=True, editable=False)
    nostr_created_at = models.PositiveBigIntegerField(null=True, blank=True, editable=False)
    staff_edited = models.BooleanField(_("corrected by hand"), default=False,
                                       help_text=_("The reader of Nostr never overwrites a meetup the staff corrected. "
                                                   "Untick to let it update this one again."))
    entered_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   editable=False, related_name="+")
    created = models.DateTimeField(_("added"), auto_now_add=True)
    updated = models.DateTimeField(_("changed"), auto_now=True)

    class Meta:
        ordering = ["-starts_at"]
        verbose_name = _("meetup")
        verbose_name_plural = _("meetups")
        constraints = [
            models.UniqueConstraint(fields=["nostr_address"], condition=~models.Q(nostr_address=""), name="meetup_nostr_address_unique"),
            models.CheckConstraint(condition=models.Q(source="staff") | ~models.Q(nostr_address=""), name="meetup_nostr_has_address"),
        ]

    def __str__(self):
        local = self.local_start
        return f"{local:%Y-%m-%d}" + ("" if self.all_day else f" {local:%H:%M}") + f" {self.city}".rstrip()

    @property
    def local_start(self):
        """The start in the zone of the meetup, an aware datetime."""
        try:
            return timezone.localtime(self.starts_at, zoneinfo.ZoneInfo(self.tzid))
        except (zoneinfo.ZoneInfoNotFoundError, ValueError, OSError):
            return timezone.localtime(self.starts_at)

    @property
    def from_nostr(self):
        return self.source == self.Source.NOSTR


class Reading(models.Model):
    """One run of `read_meetups`: when, how many relays answered and what came of it. The last ones stay, so the staff
    can see in the administration that the reader works."""
    when = models.DateTimeField(_("when"), auto_now_add=True)
    relays_asked = models.PositiveSmallIntegerField(_("relays asked"), default=0)
    relays_answered = models.PositiveSmallIntegerField(_("relays that answered"), default=0)
    events_seen = models.PositiveIntegerField(_("events seen"), default=0)
    created = models.PositiveIntegerField(_("new"), default=0)
    updated = models.PositiveIntegerField(_("updated"), default=0)
    cancelled = models.PositiveIntegerField(_("cancelled"), default=0)
    rejected = models.PositiveIntegerField(_("rejected"), default=0)
    detail = models.TextField(_("detail"), blank=True)

    class Meta:
        ordering = ["-when"]
        verbose_name = _("reading of Nostr")
        verbose_name_plural = _("readings of Nostr")

    def __str__(self):
        return f"{timezone.localtime(self.when):%Y-%m-%d %H:%M}"

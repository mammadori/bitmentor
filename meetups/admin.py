import zoneinfo
from datetime import datetime
from datetime import timezone as dt_timezone

from django import forms
from django.contrib import admin, messages
from django.contrib.admin.widgets import AdminDateWidget, AdminTimeWidget
from django.core.exceptions import ValidationError
from django.utils import formats
from django.utils.translation import gettext_lazy as _

from config.admin_secrets import HidesSecrets

from . import conf
from .keys import to_hex, to_npub
from .models import Meetup, Reading, TrustedAuthor


class MeetupForm(forms.ModelForm):
    """The day and the hour are written in the time zone of the meetup, not in the site's: a meetup in another zone is
    entered as its people will read the clock. The start (UTC) is worked out from them."""
    day = forms.DateField(label=_("Day"), widget=AdminDateWidget)
    hour = forms.TimeField(label=_("Hour"), widget=AdminTimeWidget, required=False,
                           help_text=_("Local time at the meetup. Leave it empty if only the day is known."))

    class Meta:
        model = Meetup
        fields = ["all_day", "tzid", "city", "place", "title", "url", "status", "moved_to", "visible", "staff_edited"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            start = self.instance.local_start
            self.initial.setdefault("day", start.date())
            if not self.instance.all_day:
                self.initial.setdefault("hour", start.time().replace(second=0, microsecond=0))

    def clean(self):
        cleaned = super().clean()
        day, hour, all_day = cleaned.get("day"), cleaned.get("hour"), cleaned.get("all_day")
        zone = None
        try:
            zone = zoneinfo.ZoneInfo(cleaned["tzid"]) if cleaned.get("tzid") else None
        except (zoneinfo.ZoneInfoNotFoundError, ValueError, OSError):
            pass  # the field itself says so
        if day and zone:
            if hour is None and not all_day:
                self.add_error("hour", _("Write the hour, or tick “no time, only the day”."))
            else:
                naive = datetime.combine(day, datetime.min.time() if all_day or hour is None else hour)
                # Through UTC and back: an hour that does not exist (the clocks go forward) becomes the one that does
                self.instance.starts_at = naive.replace(tzinfo=zone).astimezone(zoneinfo.ZoneInfo("UTC"))
        if not self.instance.from_nostr and not (cleaned.get("city") or "").strip():
            self.add_error("city", _("Write the city."))
        if cleaned.get("status") != Meetup.Status.POSTPONED:
            cleaned["moved_to"] = None  # a day to move to means something only for a postponed meetup
        return cleaned


@admin.register(Meetup)
class MeetupAdmin(HidesSecrets, admin.ModelAdmin):
    """The staff writes the meetups and corrects the ones the reader of Nostr found. One that came from Nostr is not
    deleted (the next reading would bring it back): it is taken off the site with “shown on the site”. A correction
    of it stays: the reader does not overwrite it."""
    form = MeetupForm
    list_display = ("when", "city", "place", "title", "status", "origin", "visible")
    list_display_links = ("when", "city")
    list_filter = ("status", "source", "visible")
    search_fields = ("city", "place", "title")
    ordering = ("-starts_at",)
    date_hierarchy = "starts_at"
    actions = ("mark_confirmed", "mark_cancelled", "show", "hide")
    fieldsets = (
        (None, {"fields": ("day", "hour", "all_day", "tzid")}),
        (_("Where and what"), {"fields": ("city", "place", "title", "url")}),
        (_("Is it on?"), {"fields": ("status", "moved_to", "visible")}),
        (_("Where it comes from"), {"fields": ("origin_text", "staff_edited")}),
    )
    readonly_fields = ("origin_text",)

    class Media:
        css = {"all": ["meetups-admin.css"]}

    def get_fieldsets(self, request, obj=None):
        """“Corrected by hand” is for what came from Nostr: a meetup the staff wrote has nothing to protect."""
        if obj and obj.from_nostr:
            return super().get_fieldsets(request, obj)
        return [(name, {**options, "fields": [field for field in options["fields"] if field != "staff_edited"]})
                for name, options in super().get_fieldsets(request, obj)]

    @admin.display(description=_("when"), ordering="starts_at")
    def when(self, meetup):
        start = meetup.local_start
        return formats.date_format(start, "SHORT_DATE_FORMAT") + ("" if meetup.all_day else " " + formats.time_format(start, "H:i"))

    @admin.display(description=_("source"), ordering="source")
    def origin(self, meetup):
        return meetup.get_source_display() + (f" ({_('corrected')})" if meetup.from_nostr and meetup.staff_edited else "")

    @admin.display(description=_("source"))
    def origin_text(self, meetup):
        if not meetup.pk:
            return _("Written here, by you.")
        if meetup.from_nostr:
            return _("Read from Nostr: author %(author)s, event %(event)s, published %(when)s.") % {
                "author": to_npub(meetup.nostr_author)[:20] + "…" if meetup.nostr_author else "-",
                "event": meetup.nostr_event_id[:12] + "…",
                "when": formats.date_format(datetime.fromtimestamp(meetup.nostr_created_at, dt_timezone.utc), "SHORT_DATE_FORMAT")
                if meetup.nostr_created_at else "-"}
        who = meetup.entered_by.get_username() if meetup.entered_by else "-"
        return _("Written by %(who)s on %(date)s.") % {"who": who, "date": formats.date_format(meetup.created, "SHORT_DATE_FORMAT")}

    def has_delete_permission(self, request, obj=None):
        return super().has_delete_permission(request, obj) and not (obj and obj.from_nostr)

    def save_model(self, request, obj, form, change):
        if not change:
            obj.entered_by = request.user
        # A correction of what came from Nostr is the staff's: the reader leaves it alone from now on, unless the staff
        # itself ticked or unticked “corrected by hand”
        if change and obj.from_nostr and "staff_edited" not in form.changed_data and form.has_changed():
            obj.staff_edited = True
        super().save_model(request, obj, form, change)

    def decide(self, request, queryset, **values):
        count = 0
        for meetup in queryset:
            for name, value in values.items():
                setattr(meetup, name, value)
            if meetup.from_nostr:
                meetup.staff_edited = True
            meetup.save()
            count += 1
        self.message_user(request, _("Changed: %(count)d") % {"count": count}, messages.SUCCESS)

    @admin.action(description=_("Mark as confirmed"), permissions=["change"])
    def mark_confirmed(self, request, queryset):
        self.decide(request, queryset, status=Meetup.Status.CONFIRMED, moved_to=None)

    @admin.action(description=_("Mark as cancelled (it stays on the board as cancelled)"), permissions=["change"])
    def mark_cancelled(self, request, queryset):
        self.decide(request, queryset, status=Meetup.Status.CANCELLED, moved_to=None)

    @admin.action(description=_("Show on the site"), permissions=["change"])
    def show(self, request, queryset):
        self.decide(request, queryset, visible=True)

    @admin.action(description=_("Take off the site"), permissions=["change"])
    def hide(self, request, queryset):
        self.decide(request, queryset, visible=False)


class TrustedAuthorForm(forms.ModelForm):
    class Meta:
        model = TrustedAuthor
        fields = ["pubkey", "note", "active"]

    def clean_pubkey(self):
        try:
            return to_hex(self.cleaned_data["pubkey"])
        except ValueError as error:
            raise ValidationError(str(error)) from None


@admin.register(TrustedAuthor)
class TrustedAuthorAdmin(HidesSecrets, admin.ModelAdmin):
    """The Nostr keys whose calendar events the reader trusts, each with a note on who certified it. An event from any
    other key is not read. The keys written in community.yml (`meetups.trusted`) count too, and are not here."""
    form = TrustedAuthorForm
    list_display = ("short_npub", "note", "active", "created", "added_by")
    list_filter = ("active",)
    search_fields = ("note", "pubkey")

    @admin.display(description=_("key"), ordering="pubkey")
    def short_npub(self, author):
        return author.npub[:20] + "…" + author.npub[-6:]

    def get_readonly_fields(self, request, obj=None):
        return ("pubkey_npub",) if obj else ()

    def get_fields(self, request, obj=None):
        return ["pubkey_npub", "note", "active"] if obj else ["pubkey", "note", "active"]

    @admin.display(description=_("key"))
    def pubkey_npub(self, author):
        return author.npub

    def save_model(self, request, obj, form, change):
        if not change:
            obj.added_by = request.user
        super().save_model(request, obj, form, change)

    def changelist_view(self, request, extra_context=None):
        listed = conf.trusted()
        if listed:
            self.message_user(request, _("%(count)d more authors are written in community.yml and count as well.") % {
                "count": len(listed)}, messages.INFO)
        return super().changelist_view(request, extra_context)


@admin.register(Reading)
class ReadingAdmin(HidesSecrets, admin.ModelAdmin):
    """Every run of the reader, as it was: nothing here is written by hand. If the last one is old, or no relay answered,
    look at `journalctl -u bitmentor-meetups@<project>`."""
    list_display = ("when", "relays_answered", "relays_asked", "events_seen", "created", "updated", "cancelled", "rejected")
    readonly_fields = [field.name for field in Reading._meta.fields]
    ordering = ("-when",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

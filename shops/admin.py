import csv

from django.contrib import admin, messages
from django.http import HttpResponse
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from config import features
from config.admin_secrets import HidesSecrets

from . import certified, details, guard, osm
from .forms import ShopAdminForm
from .guard import CHANNELS, RECENT, VIA, words
from .lightning_watch import BROKEN
from .models import AccountChange, Closure, Declaration, HelpRequest, LightningStatus, OsmRequest, OsmRun, Shop, Trainee


class LightningFilter(admin.SimpleListFilter):
    """The status of the Lightning address, as the guardian last found it: "Not working" puts together the two faults."""
    title = _("Lightning address")
    parameter_name = "lightning"

    def lookups(self, request, model_admin):
        return [("broken", _("Not working")), *LightningStatus.choices, ("unknown", _("Not checked yet"))]

    def queryset(self, request, queryset):
        value = self.value()
        if value == "broken":
            return queryset.filter(lightning_status__in=BROKEN)
        if value == "unknown":
            return queryset.filter(lightning_status="")
        if value in LightningStatus.values:
            return queryset.filter(lightning_status=value)
        return queryset


class CertifiedFilter(admin.SimpleListFilter):
    """Certified shops (shops/certified.py), and all the others, with or without a register."""
    title = _("certified shop")
    parameter_name = "certified"

    def lookups(self, request, model_admin):
        return [("yes", _("Certified")), ("waiting", _("Certified, private part waiting for approval")),
                ("no", _("Not yet"))]

    def queryset(self, request, queryset):
        if self.value() == "waiting":
            return certified.waiting_approval(queryset)
        if self.value() == "yes":
            return queryset.filter(certified_month__isnull=False)
        if self.value() == "no":
            return queryset.filter(certified_month__isnull=True)
        return queryset


class OsmFilter(admin.SimpleListFilter):
    """The shops linked to an entry on OpenStreetMap (shops/osm.py), and those that are not."""
    title = _("OpenStreetMap")
    parameter_name = "osm"

    def lookups(self, request, model_admin):
        return [("linked", _("Linked")), ("proposed", _("Link proposed, to confirm")), ("not_linked", _("Not linked"))]

    def queryset(self, request, queryset):
        if self.value() == "linked":
            return queryset.exclude(osm_ref="")
        if self.value() == "proposed":
            return queryset.exclude(osm_proposed="")
        if self.value() == "not_linked":
            return queryset.filter(osm_ref="")


class VatFilter(admin.SimpleListFilter):
    """The VAT numbers that look wrong (shops/details.py): saved as they were written, for the association to check."""
    title = _("VAT number")
    parameter_name = "vat"

    def lookups(self, request, model_admin):
        return [("doubtful", _("May have a mistake")), ("written", _("Written")), ("empty", _("Not written"))]

    def queryset(self, request, queryset):
        value = self.value()
        if value == "doubtful":
            written = queryset.exclude(vat_number="").values_list("pk", "vat_number")
            return queryset.filter(pk__in=[pk for pk, number in written if details.vat_doubt(number)])
        if value == "written":
            return queryset.exclude(vat_number="")
        if value == "empty":
            return queryset.filter(vat_number="")
        return queryset


class ClosureInline(admin.TabularInline):
    """Closed for a while (shops/vacation.py): the association writes it for a merchant who phones. The first day may be in
    the past; the shop says it is closed until the day it opens again, and nothing else changes: the register keeps
    working."""
    model = Closure
    extra = 0
    fields = ("first_day", "reopens_on", "reason")


@admin.register(Shop)
class ShopAdmin(HidesSecrets, admin.ModelAdmin):
    form = ShopAdminForm
    list_display = ("name", "city", "lightning_column", "certified_column", "osm_proposal_column", "discount", "listed", "suspended",
                    "closed_on", "owner")
    list_editable = ("suspended", "closed_on")
    list_filter = (LightningFilter, CertifiedFilter, OsmFilter, VatFilter, "suspended", "closed_on", "listed", "lightning",
                   "awaiting_wallet")
    search_fields = ("name", "city", "legal_name", "vat_number")
    # Where the money goes is the owner's to change, from the panel. What the guardian found is written only by its probes
    # (manage.py check_lightning), and the certificate never from here
    readonly_fields = ("lightning_address", "lightning_status", "lightning_since",
                       "lightning_checked", "lightning_worked", "certified_month", "certified_revoked", "private_approved",
                       "private_approved_by", "moved_in",
                       "moved_register", "awaiting_wallet", "osm_seen", "osm_page", "osm_proposal", "osm_offered", "osm_history")
    actions = ("approve_private_part", "revoke_private_part", "revoke_certificate", "allow_certificate", "show_contacts", "hide_contacts", "confirm_osm_link",
               "reject_osm_link", "accept_osm_contacts", "decline_osm_contacts")
    inlines = (ClosureInline,)
    # What the community's `features` block (config/features.py) takes away from the administration. The data stays in
    # the database; the page just does not show a feature that is off. The Closures table is never hidden here (a closure written before can still keep the alarms quiet until its day, and the association must be able
    # to shorten or remove it)
    CERTIFICATE_FIELDS = ("certified_month", "certified_revoked", "private_approved", "private_approved_by")
    CERTIFICATE_ACTIONS = ("revoke_certificate", "allow_certificate", "approve_private_part", "revoke_private_part")

    def get_list_display(self, request):
        columns = super().get_list_display(request)
        return columns if features.enabled(features.CERTIFIED) else [name for name in columns if name != "certified_column"]

    def get_list_filter(self, request):
        found = super().get_list_filter(request)
        return found if features.enabled(features.CERTIFIED) else [item for item in found if item is not CertifiedFilter]

    def get_actions(self, request):
        found = super().get_actions(request)
        if features.enabled(features.CERTIFIED):
            return found
        return {name: action for name, action in found.items() if name not in self.CERTIFICATE_ACTIONS}

    def get_readonly_fields(self, request, obj=None):
        """Three things take columns away from the form of a shop. The certificate's go when the badge is turned off
        (features). What customers see of a shop (shops/details.py) is the owner's choice: the page «What customers see»
        writes it, and whoever runs the site sets it only for a shop with no owner, one loaded from a list. And where the
        money goes is never written from here, by anybody (superusers too): the owner changes it from the panel. Whatever
        else a subclass or a setting does to the list, the destination stays in it; `shops/guard.py` refuses a write of
        it that comes from /admin/ all the same, from any module or action."""
        found = list(super().get_readonly_fields(request, obj))
        if obj is not None and obj.owner_id:
            found += [details.flag(name) for name in details.CHOICES]
        if not features.enabled(features.CERTIFIED):
            found = [name for name in found if name not in self.CERTIFICATE_FIELDS]
        return found + [name for name in guard.FIELDS if name not in found]

    # On-chain at the register is an experimental feature (config/features.py, `experimental`): while it is off the form
    # of a shop shows neither the owner's tick nor the shop's own explorer. Both stay in the database
    ONCHAIN_FIELDS = ("onchain", "own_explorer")

    def get_exclude(self, request, obj=None):
        found = list(super().get_exclude(request, obj) or [])
        return found if features.enabled(features.ONCHAIN) else [*found, *self.ONCHAIN_FIELDS]

    def hidden_fields(self):
        """What the form does not show is not asked for in the address of the list either (config/admin_secrets.py)."""
        found = super().hidden_fields()
        return found if features.enabled(features.ONCHAIN) else found | set(self.ONCHAIN_FIELDS)

    @admin.display(description=_("OpenStreetMap link proposed"), ordering="osm_proposed")
    def osm_proposal_column(self, shop):
        """The element proposed, its name, how far it is and how alike: what the association needs to confirm it from the list."""
        if not shop.osm_proposed:
            return "–"
        proposal = osm.notes_of(shop)["proposal"]
        if proposal.get("ref") != shop.osm_proposed:
            return shop.osm_proposed
        details = [f"{proposal['metres']} m"] if proposal.get("metres") is not None else []
        details += [f"{proposal['similarity']:.2f}"] if proposal.get("similarity") is not None else []
        return " · ".join([f"{proposal.get('name') or '?'} ({shop.osm_proposed})", *details])

    @admin.display(description=_("OpenStreetMap decisions"))
    def osm_history(self, shop):
        """Who confirmed or refused a link, who accepted or declined the contacts, and when."""
        lines = [f"{item.get('at', '')[:16].replace('T', ' ')} · {item.get('by') or '?'} · {item.get('what', '')}"
                 for item in osm.notes_of(shop)["decisions"]]
        return format_html("<br>".join(["{}"] * len(lines)), *lines) if lines else "–"

    @admin.display(description=_("Offered by OpenStreetMap"))
    def osm_offered(self, shop):
        """The website and the phone OpenStreetMap offered and nobody accepted yet: they are never written by the reading."""
        offered = osm.offers_of(shop)
        return "; ".join(f"{name}: {value}" for name, value in offered.items()) or "–"

    @admin.action(description=_("Confirm the OpenStreetMap link proposed"), permissions=["change"])
    def confirm_osm_link(self, request, queryset):
        """A proposed link (shops/osm.py) becomes the shop's link: from the next reading its empty address, town and position
        are filled, and its entry is compared. Look at the element first: it is in the shop's page."""
        count = sum(1 for shop in queryset.exclude(osm_proposed="") if osm.confirm_link(shop, request.user.get_username()))
        self.message_user(request, _("%(count)d links to OpenStreetMap confirmed.") % {"count": count})

    @admin.action(description=_("Refuse the OpenStreetMap link proposed"), permissions=["change"])
    def reject_osm_link(self, request, queryset):
        count = sum(1 for shop in queryset.exclude(osm_proposed="") if osm.reject_link(shop, request.user.get_username()))
        self.message_user(request, _("%(count)d proposed links to OpenStreetMap refused: they are not proposed again.") % {"count": count})

    @admin.action(description=_("Accept the website and phone offered by OpenStreetMap"), permissions=["change"])
    def accept_osm_contacts(self, request, queryset):
        """Written only into the fields that are still empty."""
        count = sum(1 for shop in queryset.exclude(osm_ref="") if osm.accept_contacts(shop, request.user.get_username()))
        self.message_user(request, _("%(count)d shops got the contacts OpenStreetMap offered.") % {"count": count})

    @admin.action(description=_("Decline the website and phone offered by OpenStreetMap"), permissions=["change"])
    def decline_osm_contacts(self, request, queryset):
        count = sum(1 for shop in queryset.exclude(osm_ref="") if osm.decline_contacts(shop, request.user.get_username()))
        self.message_user(request, _("%(count)d shops declined the contacts OpenStreetMap offered: they are not offered again.") % {"count": count})

    @admin.display(description=_("OpenStreetMap page"))
    def osm_page(self, shop):
        """A link to the linked element, to check that the link is the right place."""
        url = osm.element_url(shop.osm_ref)
        return format_html('<a href="{}" rel="noopener noreferrer" target="_blank">{}</a>', url, shop.osm_ref) if url else "–"

    @admin.display(description=_("Link proposed"))
    def osm_proposal(self, shop):
        """The element the reading proposes as the shop's, with a link to look at it before confirming."""
        url = osm.element_url(shop.osm_proposed)
        return format_html('<a href="{}" rel="noopener noreferrer" target="_blank">{}</a>', url, shop.osm_proposed) if url else "–"

    def save_model(self, request, obj, form, change):
        """A link to OpenStreetMap written by hand is the link: whatever the reading had proposed is gone. The form keeps
        the VAT number as the owner's form keeps it (shops/forms.py). One that looks wrong is saved all the same, with a
        warning, but only when this save wrote the number: a tick of the list does not repeat it."""
        if obj.osm_ref and obj.osm_proposed:
            obj.osm_proposed = ""
        # Another owner: Shop.save takes the approval of the private part away, written here in the shop's history
        approved = change and "owner" in form.changed_data and obj.private_approved is not None
        super().save_model(request, obj, form, change)
        if approved:
            self.log_change(request, obj, "Private part revoked: the shop changed owner")
        why = details.vat_doubt(obj.vat_number) if "vat_number" in form.changed_data else ""
        if why:
            self.message_user(request, _("The VAT number of %(shop)s may have a mistake: %(why)s It is saved as written.")
                              % {"shop": obj.name, "why": why}, messages.WARNING)

    def get_search_results(self, request, queryset, search_term):
        """The search finds a VAT number written with dots, dashes or spaces as the list keeps it: the term is tidied the way
        the number was, and what it finds is added to what the ordinary search finds, inside the same filters."""
        found, distinct = super().get_search_results(request, queryset, search_term)
        number = details.normalize_vat(search_term)
        if number and number != search_term.upper():
            found = found | queryset.filter(vat_number__icontains=number)
        return found, distinct

    @admin.display(description=_("Lightning address"), ordering="lightning_status")
    def lightning_column(self, shop):
        """The status and since when: a fault since the first failed probe, so the date says how long it has lasted."""
        if not shop.lightning_status:
            return "–"
        label = shop.get_lightning_status_display()
        if shop.lightning_status in BROKEN and shop.lightning_since:
            return f"{label} · {date_format(timezone.localtime(shop.lightning_since), 'SHORT_DATETIME_FORMAT')}"
        return label

    @admin.display(description=_("Certified"), boolean=True, ordering="certified_month")
    def certified_column(self, shop):
        return shop.certified

    @admin.action(description=_("Show the phone and the email of the selected shops with no owner"), permissions=["change"])
    def show_contacts(self, request, queryset):
        """For the shops loaded from a list: they start with the phone and the email hidden, and no owner can turn them on.
        A shop with an owner is left alone: its owner chooses (shops/details.py)."""
        count = queryset.filter(owner__isnull=True).update(show_phone=True, show_email=True)
        self.message_user(request, _("%(count)d shops now show their phone and email.") % {"count": count})

    @admin.action(description=_("Hide the phone and the email of the selected shops with no owner"), permissions=["change"])
    def hide_contacts(self, request, queryset):
        count = queryset.filter(owner__isnull=True).update(show_phone=False, show_email=False)
        self.message_user(request, _("%(count)d shops now hide their phone and email.") % {"count": count})

    def log_each(self, request, shops, message):
        """The association's actions on the certificate and the private part go in the admin's history of each shop."""
        for shop in shops:
            self.log_change(request, shop, message)

    @admin.action(description=_("Open the private part to the owner"), permissions=["change"])
    def approve_private_part(self, request, queryset):
        """The owners of these certified shops see the private part of the pages: when and who is written on the shop and
        in its history. A shop not certified, or already approved, is left as it is."""
        shops = list(certified.waiting_approval(queryset))
        Shop.objects.filter(pk__in=[shop.pk for shop in shops]).update(
            private_approved=timezone.now(), private_approved_by=request.user.get_username())
        self.log_each(request, shops, "Private part approved")
        self.message_user(request, _("Private part opened for %(count)d shops.") % {"count": len(shops)})

    @admin.action(description=_("Close the private part to the owner"), permissions=["change"])
    def revoke_private_part(self, request, queryset):
        shops = list(queryset.filter(private_approved__isnull=False))
        Shop.objects.filter(pk__in=[shop.pk for shop in shops]).update(private_approved=None, private_approved_by="")
        self.log_each(request, shops, "Private part revoked")
        self.message_user(request, _("Private part closed for %(count)d shops.") % {"count": len(shops)})

    @admin.action(description=_("Revoke the certificate"), permissions=["change"])
    def revoke_certificate(self, request, queryset):
        """No badge and no private part, and the site no longer certifies these shops. The approval of the private part
        goes too: a certificate allowed again waits for a new one."""
        shops = list(queryset)
        count = queryset.update(certified_month=None, certified_revoked=True, private_approved=None, private_approved_by="")
        self.log_each(request, shops, "Certificate revoked")
        self.message_user(request, _("Certificate revoked for %(count)d shops.") % {"count": count})

    @admin.action(description=_("Allow the certificate again"), permissions=["change"])
    def allow_certificate(self, request, queryset):
        """Undoes a revocation: a shop whose Lightning address works and whose owner has the basic pass is certified
        again at once, the others when they get there."""
        pks = list(queryset.filter(certified_revoked=True).values_list("pk", flat=True))
        count = Shop.objects.filter(pk__in=pks).update(certified_revoked=False)
        certified.refresh(Shop.objects.filter(pk__in=pks))
        self.message_user(request, _("%(count)d shops can be certified again.") % {"count": count})


@admin.register(Declaration)
class DeclarationAdmin(HidesSecrets, admin.ModelAdmin):
    """The written proof that a merchant (or the person at a staff register) was told (shops/training.py): which
    statement, the version of its text, the text as it was shown, when, who and for which shop. To look at and to export,
    never to edit: nothing is added, changed or deleted here (whoever has the shell or the database can, and the docs say
    so). No address of the connection is kept."""
    list_display = ("made", "who", "shop_name", "key", "version_short", "language")
    list_filter = ("key", "language")
    search_fields = ("who", "shop_name", "key", "version")
    date_hierarchy = "made"
    readonly_fields = [field.name for field in Declaration._meta.fields]
    actions = ["export_csv"]

    @admin.display(description=_("version of the text"), ordering="version")
    def version_short(self, declaration):
        return declaration.version[:12]

    @admin.action(description=_("Export the selected declarations as a spreadsheet (CSV)"))
    def export_csv(self, request, queryset):
        from .training import masked
        from .views import spreadsheet_text

        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="declarations.csv"'
        response["Cache-Control"] = "no-store"
        writer = csv.writer(response)
        writer.writerow(["made", "who", "shop", "shop_id", "register_id", "statement", "version", "language", "text"])
        for row in queryset.order_by("made", "pk").iterator():
            writer.writerow([timezone.localtime(row.made).isoformat(timespec="seconds"), spreadsheet_text(masked(row.who)),
                             spreadsheet_text(row.shop_name), row.shop_id or "", row.register_id or "", row.key, row.version,
                             row.language, spreadsheet_text(row.text)])
        return response

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Trainee)
class TraineeAdmin(HidesSecrets, admin.ModelAdmin):
    """Who has the basic pass of the merchant's training and since when, and the last review. To look at: the pass is
    earned on the site, not given here."""
    list_display = ("user", "base_earned", "reviewed")
    search_fields = ("user__username",)
    date_hierarchy = "base_earned"
    readonly_fields = [field.name for field in Trainee._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AccountChange)
class AccountChangeAdmin(HidesSecrets, admin.ModelAdmin):
    """What whoever runs the site changed in the account of a shop's owner (shops/guard.py), newest first. To look at,
    not to edit: nothing is added, changed or deleted here, by anyone (whoever has the shell or the database can, and the
    docs say so)."""
    list_display = ("when", "sentence", "what", "told_column", "not_told_column")
    list_filter = ("via",)
    search_fields = ("shops", "actor")
    date_hierarchy = "created"
    readonly_fields = [field.name for field in AccountChange._meta.fields]

    @admin.display(description=_("when"), ordering="created")
    def when(self, change):
        return date_format(timezone.localtime(change.created), "SHORT_DATETIME_FORMAT")

    @admin.display(description=_("account"))
    def sentence(self, change):
        if change.actor:
            return _("The account of {shops} was changed from {via} by {actor}.").format(
                shops=change.shops, via=VIA[change.via], actor=change.actor)
        return _("The account of {shops} was changed from {via}.").format(shops=change.shops, via=VIA[change.via])

    @admin.display(description=_("what changed"))
    def what(self, change):
        return ", ".join(words(change.changed.split(",")))

    @admin.display(description=_("told"))
    def told_column(self, change):
        return ", ".join(str(CHANNELS[code]) for code in change.told.split(",") if code) or "–"

    @admin.display(description=_("not told"))
    def not_told_column(self, change):
        return ", ".join(str(CHANNELS[code]) for code in change.not_told.split(",") if code) or "–"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class SolvedFilter(admin.SimpleListFilter):
    """The requests for help still waiting, or already solved."""
    title = _("state")
    parameter_name = "state"

    def lookups(self, request, model_admin):
        return [("open", _("Waiting")), ("solved", _("Solved"))]

    def queryset(self, request, queryset):
        if self.value() == "open":
            return queryset.filter(resolved__isnull=True)
        if self.value() == "solved":
            return queryset.filter(resolved__isnull=False)
        return queryset


@admin.register(HelpRequest)
class HelpRequestAdmin(HidesSecrets, admin.ModelAdmin):
    """The merchants' requests for help (shops/helpdesk.py), newest first, with the diagnosis the site made when they
    asked. To read and to mark solved (an action: who and when are kept); nothing is added, edited or deleted here. The
    open ones are on top of /admin/today/ until someone marks them solved."""
    list_display = ("when", "shop", "symptom", "call_back", "delivery", "state")
    list_select_related = ("shop",)
    list_filter = (SolvedFilter, "symptom")
    search_fields = ("shop__name", "note")
    date_hierarchy = "created"
    actions = ("solve", "reopen")
    readonly_fields = [field.name for field in HelpRequest._meta.fields] + ["call_back_to"]
    fields = ("shop", "symptom", "note", "created", "channel", "call_back_to", "delivery", "summary", "resolved", "resolved_by")

    @admin.display(description=_("asked"), ordering="created")
    def when(self, row):
        return date_format(timezone.localtime(row.created), "SHORT_DATETIME_FORMAT")

    @admin.display(description=_("call back"), ordering="channel")
    def call_back(self, row):
        return row.get_channel_display()

    @admin.display(description=_("state"), boolean=True, ordering="resolved")
    def state(self, row):
        return row.resolved is not None

    @admin.display(description=_("where to call back"))
    def call_back_to(self, row):
        """Where the merchant said to call, read now from the shop and its owner: the recovery email, the shop's Telegram
        chat or its public phone."""
        from .lightning_watch import owner_email

        found = {HelpRequest.Channel.EMAIL: owner_email(row.shop), HelpRequest.Channel.TELEGRAM: row.shop.telegram_chat,
                 HelpRequest.Channel.PHONE: row.shop.phone}.get(row.channel)
        return found or "–"

    @admin.action(description=_("Mark as solved"), permissions=["change"])
    def solve(self, request, queryset):
        count = queryset.filter(resolved__isnull=True).update(resolved=timezone.now(),
                                                              resolved_by=request.user.get_username()[:150])
        self.message_user(request, _("%(count)d requests marked as solved.") % {"count": count})

    @admin.action(description=_("Mark as waiting again"), permissions=["change"])
    def reopen(self, request, queryset):
        count = queryset.filter(resolved__isnull=False).update(resolved=None, resolved_by="")
        self.message_user(request, _("%(count)d requests marked as waiting again.") % {"count": count})

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        # The list and its actions yes; the page of one request only to read
        return obj is None and super().has_change_permission(request)

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(OsmRun)
class OsmRunAdmin(HidesSecrets, admin.ModelAdmin):
    """The monthly readings of OpenStreetMap (shops/osm.py), newest first, with what each found and the changes proposed
    for OpenStreetMap. To read, not to edit: the proposals are sent by a person with `manage.py osm_upload`, never from here."""
    list_display = ("when", "ok", "result", "uploaded")
    list_filter = ("ok",)
    readonly_fields = ("created", "ok", "error", "uploaded", "changeset", "result", "text")
    fields = readonly_fields

    @admin.display(description=_("read"), ordering="created")
    def when(self, run):
        return date_format(timezone.localtime(run.created), "SHORT_DATETIME_FORMAT")

    @admin.display(description=_("result"))
    def result(self, run):
        if not run.ok:
            return run.error
        found = osm.counts(run.report)
        return _("%(proposals)d changes proposed, %(link_proposals)d links proposed, %(candidates)d new places, %(says_no)d marked as closed there") % found

    @admin.display(description=_("report"))
    def text(self, run):
        return format_html("<pre>{}</pre>", "\n".join(osm.report_lines(run.report))) if run.ok else "–"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return obj is None and super().has_change_permission(request)

    def has_delete_permission(self, request, obj=None):
        return False


class OsmStateFilter(admin.SimpleListFilter):
    title = _("state")
    parameter_name = "state"

    def lookups(self, request, model_admin):
        return OsmRequest.State.choices

    def queryset(self, request, queryset):
        return queryset.filter(state=self.value()) if self.value() in OsmRequest.State.values else queryset


@admin.register(OsmRequest)
class OsmRequestAdmin(HidesSecrets, admin.ModelAdmin):
    """The merchants' requests for the association to bring their entry on OpenStreetMap up to date, or to put the shop
    there (shops/osm_views.py), newest first. A person of the staff does it (by hand in the editor, or with `manage.py
    osm_upload`) and marks it done here, or writes why it could not be done: the merchant sees both in the panel.
    The site never writes to OpenStreetMap by itself."""
    list_display = ("when", "shop", "kind", "state", "delivery", "done_by")
    list_select_related = ("shop",)
    list_filter = (OsmStateFilter, "kind")
    search_fields = ("shop__name",)
    actions = ("mark_done",)
    readonly_fields = ("shop", "asked_by", "kind", "created", "summary", "delivery", "done", "done_by", "page")
    fields = ("shop", "kind", "created", "summary", "page", "state", "answer", "delivery", "done", "done_by")

    @admin.display(description=_("asked"), ordering="created")
    def when(self, row):
        return date_format(timezone.localtime(row.created), "SHORT_DATETIME_FORMAT")

    @admin.display(description=_("the shop's entry"))
    def page(self, row):
        url = osm.element_url(row.shop.osm_ref)
        if url:
            return format_html('<a href="{}" rel="noopener noreferrer" target="_blank">{}</a>', url, row.shop.osm_ref)
        return _("Not linked: link it in the shop's page if it is already on OpenStreetMap.")

    def stamp(self, request, row):
        if row.state == OsmRequest.State.OPEN:
            row.done, row.done_by = None, ""
        elif row.done is None:
            row.done, row.done_by = timezone.now(), request.user.get_username()[:150]

    def save_model(self, request, obj, form, change):
        self.stamp(request, obj)
        super().save_model(request, obj, form, change)

    @admin.action(description=_("Mark as done"), permissions=["change"])
    def mark_done(self, request, queryset):
        count = queryset.filter(state=OsmRequest.State.OPEN).update(
            state=OsmRequest.State.DONE, done=timezone.now(), done_by=request.user.get_username()[:150])
        self.message_user(request, _("%(count)d requests marked as done.") % {"count": count})

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


def dashboard_view(request):
    """What to do today about the shops' registers (shops/dashboard.py): staff with the admin's own session, and the
    permission to see shops. Only reads the database."""
    from django.core.exceptions import PermissionDenied
    from django.template.response import TemplateResponse

    from .dashboard import summary

    if not request.user.has_perm("shops.view_shop"):
        raise PermissionDenied
    context = {**admin.site.each_context(request), "title": _("To do today"), **summary()}
    # What whoever runs the site changed in the accounts of shop owners lately (shops/guard.py): a line, with its register
    if request.user.has_perm("shops.view_accountchange"):
        context["account_changes"] = (AccountChange.objects.filter(created__gte=timezone.now() - RECENT)
                                       .exclude(via=AccountChange.Via.OWNER).count())
    return TemplateResponse(request, "admin/shops/dashboard.html", context)

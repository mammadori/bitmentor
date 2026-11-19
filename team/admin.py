"""Team access in /admin/: the invites the staff makes and the keys they became. With TEAM_ACCESS off nothing of it
shows: no page, no link, for anybody."""
import segno
from django.conf import settings
from django.contrib import admin, messages
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext

from config.admin_secrets import HidesSecrets

from . import access
from .models import TeamAction, TeamInvite, TeamKey


def may_give_approves(request):
    """Only staff who may approve shops themselves (`shops.change_shop`, what approving needs in /admin/) can let a team
    key approve them in the volunteers' area: the right to change keys or to make invites is not enough, or anybody
    with it could give their own key more than they have."""
    return request.user.has_perm("shops.change_shop")


class Switched:
    """An admin that exists only while TEAM_ACCESS is on: off, every permission is refused, so the index does not list
    it and its addresses answer 403, as for whoever has no right to them."""

    def has_module_permission(self, request):
        return access.on() and super().has_module_permission(request)

    def has_view_permission(self, request, obj=None):
        return access.on() and super().has_view_permission(request, obj)

    def has_add_permission(self, request):
        return access.on() and super().has_add_permission(request)

    def has_change_permission(self, request, obj=None):
        return access.on() and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return access.on() and super().has_delete_permission(request, obj)


@admin.register(TeamKey)
class TeamKeyAdmin(Switched, HidesSecrets, admin.ModelAdmin):
    """The team's keys. A key is made only by an invite; here it is renamed, revoked or deleted. Revoking keeps the
    row, so the same wallet cannot come back with another invite; deleting forgets it."""
    list_display = ("name", "role", "approves", "short_key", "created", "last_pass", "state")
    list_filter = ("role", "approves")
    search_fields = ("name",)
    fields = ("name", "role", "approves", "linking_key", "created", "last_pass", "revoked")
    readonly_fields = ("linking_key", "created", "last_pass", "revoked")
    actions = ("revoke", "end_passes")

    def get_fields(self, request, obj=None):
        # With the volunteers' area on, a key's page links to what it did there
        return (*self.fields, "area_log") if obj and access.area_on() else self.fields

    def get_readonly_fields(self, request, obj=None):
        found = (*self.readonly_fields, "area_log") if obj and access.area_on() else self.readonly_fields
        return found if may_give_approves(request) else (*found, "approves")

    def save_model(self, request, obj, form, change):
        # On the server too: a form made by hand cannot set it
        if not may_give_approves(request):
            obj.approves = TeamKey.objects.filter(pk=obj.pk).values_list("approves", flat=True).first() or False
        super().save_model(request, obj, form, change)

    @admin.display(description=_("in the volunteers' area"))
    def area_log(self, key):
        count = key.area_actions.count()
        url = f"{reverse('admin:team_teamaction_changelist')}?key__id__exact={key.pk}"
        return format_html('<a href="{}">{}</a>', url,
                           ngettext("%(count)d action", "%(count)d actions", count) % {"count": count})

    @admin.display(description=_("linking key"))
    def short_key(self, key):
        return f"{key.linking_key[:8]}…{key.linking_key[-6:]}"

    @admin.display(description=_("state"))
    def state(self, key):
        return _("revoked") if key.revoked else _("active")

    def has_add_permission(self, request):
        return False

    def each(self, request, queryset, change, done):
        """Applies `change` to the keys one by one and writes each one it changed in the admin's log, as a change made in
        the key's own page is: the history of a key says who revoked it, or ended its passes, and when. Returns how many."""
        count = 0
        for key in queryset:
            if change(TeamKey.objects.filter(pk=key.pk)):
                self.log_change(request, key, str(done))
                count += 1
        return count

    @admin.action(description=_("Revoke the selected keys: their passes stop at once"), permissions=["change"])
    def revoke(self, request, queryset):
        count = self.each(request, queryset, TeamKey.revoke, _("Revoked: the wallet signs in no more, its passes stopped."))
        self.message_user(request, ngettext("%(count)d key revoked.", "%(count)d keys revoked.", count) % {"count": count},
                          messages.SUCCESS)

    @admin.action(description=_("End the passes of the selected keys: every browser signs in again"), permissions=["change"])
    def end_passes(self, request, queryset):
        count = self.each(request, queryset, TeamKey.end_passes, _("Passes ended: every browser signs in again."))
        self.message_user(request, ngettext("The passes of %(count)d key ended.", "The passes of %(count)d keys ended.",
                                            count) % {"count": count}, messages.SUCCESS)


@admin.register(TeamInvite)
class TeamInviteAdmin(Switched, HidesSecrets, admin.ModelAdmin):
    """The invites: made here with a name and a role, shown once as a link and a QR code, then only looked at. Deleting
    one that is still open revokes it."""
    list_display = ("name", "role", "created", "expires", "state", "key")
    list_filter = ("role",)
    search_fields = ("name",)
    fields = ("name", "role", "approves", "created", "expires", "used", "key")
    readonly_fields = ("created", "expires", "used", "key")

    @admin.display(description=_("state"))
    def state(self, invite):
        if invite.used:
            return _("used")
        return _("open") if invite.expires > timezone.now() else _("expired")

    def get_fields(self, request, obj=None):
        return self.fields if obj else ("name", "role", "approves")

    def get_readonly_fields(self, request, obj=None):
        return self.readonly_fields if may_give_approves(request) else (*self.readonly_fields, "approves")

    def has_add_permission(self, request):
        # An invite does something only where wallets sign in: a site under construction, and an open one with the
        # volunteers' area on
        return access.signs_in() and super().has_add_permission(request)

    def has_change_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        # The token is kept nowhere: it goes from here to the page that shows it, in this same request
        request.team_invite_token = obj.arm()
        if not may_give_approves(request):
            obj.approves = False
        super().save_model(request, obj, form, change)

    def response_add(self, request, obj, post_url_continue=None):
        link = f"{settings.PUBLIC_URL}{reverse('team_join', args=[request.team_invite_token])}"
        response = TemplateResponse(request, "admin/team/teaminvite/created.html", {
            **self.admin_site.each_context(request), "opts": self.opts, "title": str(obj), "invite": obj, "link": link,
            "qr": segno.make(link, error="l").svg_data_uri(scale=6, border=2, light="#fff")})
        response["Cache-Control"] = "private, no-store"
        return response


@admin.register(TeamAction)
class TeamActionAdmin(HidesSecrets, admin.ModelAdmin):
    """What the volunteers did in their area (team/area.py), newest first: to read, never to add, change or delete. It
    shows only while the area is on (TEAM_AREA with TEAM_ACCESS); the rows stay in the database when it is off."""
    list_display = ("created", "name", "action", "target")
    list_filter = ("action", "key")
    fields = ("created", "name", "key", "action", "target")
    readonly_fields = fields

    def has_module_permission(self, request):
        return access.area_on() and super().has_module_permission(request)

    def has_view_permission(self, request, obj=None):
        return access.area_on() and super().has_view_permission(request, obj)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

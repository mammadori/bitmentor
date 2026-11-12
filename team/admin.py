"""Team access in /admin/: the invites the staff makes and the keys they became. With TEAM_ACCESS off nothing of it
shows: no page, no link, for anybody."""
import segno
from django.conf import settings
from django.contrib import admin, messages
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext

from config.admin_secrets import HidesSecrets

from . import access
from .models import TeamInvite, TeamKey


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
    list_display = ("name", "role", "short_key", "created", "last_pass", "state")
    list_filter = ("role",)
    search_fields = ("name",)
    fields = ("name", "role", "linking_key", "created", "last_pass", "revoked")
    readonly_fields = ("linking_key", "created", "last_pass", "revoked")
    actions = ("revoke", "end_passes")

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
    fields = ("name", "role", "created", "expires", "used", "key")
    readonly_fields = ("created", "expires", "used", "key")

    @admin.display(description=_("state"))
    def state(self, invite):
        if invite.used:
            return _("used")
        return _("open") if invite.expires > timezone.now() else _("expired")

    def get_fields(self, request, obj=None):
        return self.fields if obj else ("name", "role")

    def has_add_permission(self, request):
        # An invite does something only on a site under construction
        return access.active() and super().has_add_permission(request)

    def has_change_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change):
        # The token is kept nowhere: it goes from here to the page that shows it, in this same request
        request.team_invite_token = obj.arm()
        super().save_model(request, obj, form, change)

    def response_add(self, request, obj, post_url_continue=None):
        link = f"{settings.PUBLIC_URL}{reverse('team_join', args=[request.team_invite_token])}"
        response = TemplateResponse(request, "admin/team/teaminvite/created.html", {
            **self.admin_site.each_context(request), "opts": self.opts, "title": str(obj), "invite": obj, "link": link,
            "qr": segno.make(link, error="l").svg_data_uri(scale=6, border=2, light="#fff")})
        response["Cache-Control"] = "private, no-store"
        return response

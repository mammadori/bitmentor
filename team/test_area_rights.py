"""The volunteers' area: who may give a key the right to approve shops, and what the area gives beyond the Moderators
group, on purpose (team/area.py, `BEYOND_THE_GROUP`)."""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.urls import reverse
from django.utils import timezone

from shops import approval
from shops.models import Shop
from shops.test_guard import sign_in_as_staff_session

from . import access, area
from .models import TeamAction, TeamInvite, TeamKey
from .test_area import AreaBase

User = get_user_model()


class WhoGivesApproves(AreaBase):
    """Only staff who may approve shops themselves can let a key approve them."""

    def setUp(self):
        super().setUp()
        self.key, _browser = self.moderator("Ugo")
        staff = User.objects.create_user("keeper", is_staff=True)
        staff.user_permissions.add(
            *Permission.objects.filter(
                content_type__app_label="team",
                codename__in=("view_teamkey", "change_teamkey", "view_teaminvite", "add_teaminvite"),
            )
        )
        self.keeper = self.client_class()
        sign_in_as_staff_session(self.keeper, staff)

    def test_not_by_form_nor_by_post_without_the_right_to_approve_shops(self):
        change = reverse("admin:team_teamkey_change", args=[self.key.pk])
        page = self.keeper.get(change)
        self.assertEqual(page.status_code, 200)
        self.assertNotIn('name="approves"', page.content.decode())
        self.keeper.post(change, {"name": "Ugo", "role": "moderator", "approves": "on"})
        self.key.refresh_from_db()
        self.assertFalse(self.key.approves)
        add = reverse("admin:team_teaminvite_add")
        self.assertNotIn('name="approves"', self.keeper.get(add).content.decode())
        self.assertEqual(
            self.keeper.post(add, {"name": "Nina", "role": "moderator", "approves": "on"}).status_code, 200
        )
        self.assertFalse(TeamInvite.objects.get(name="Nina").approves)
        # A key that has it keeps it when such a person changes its name
        TeamKey.objects.filter(pk=self.key.pk).update(approves=True)
        self.keeper.post(change, {"name": "Ugo B", "role": "moderator"})
        self.key.refresh_from_db()
        self.assertEqual((self.key.name, self.key.approves), ("Ugo B", True))

    def test_a_superuser_can(self):
        boss = self.client_class()
        sign_in_as_staff_session(boss, User.objects.create_superuser("boss"))
        self.assertIn('name="approves"', boss.get(reverse("admin:team_teaminvite_add")).content.decode())
        boss.post(
            reverse("admin:team_teamkey_change", args=[self.key.pk]),
            {"name": "Ugo", "role": "moderator", "approves": "on"},
        )
        self.key.refresh_from_db()
        self.assertTrue(self.key.approves)


class TheExceptions(AreaBase):
    def test_what_the_area_gives_beyond_the_group_is_named(self):
        self.assertEqual(set(area.BEYOND_THE_GROUP), {"osm_done", "approve"})
        # The group has neither: they are the area's own, on purpose
        self.assertNotIn("shops.change_shop", area.RIGHTS)
        self.assertFalse({right for right in area.RIGHTS if "osmrequest" in right})

    def test_a_flag_taken_away_between_the_gate_and_the_write_is_honoured(self):
        waiting = Shop.objects.create(name="Forno Nuovo", approved_on=None)
        key, browser = self.moderator("Ada")
        TeamKey.objects.filter(pk=key.pk).update(approves=True)
        original = access.volunteer
        for change in ({"approves": False}, {"revoked": timezone.now()}):

            def late(request, change=change):
                found = original(request)
                if found is not None and request.method == "POST":
                    TeamKey.objects.filter(pk=found.pk).update(**change)
                return found

            with patch("team.access.volunteer", side_effect=late):
                self.assertEqual(browser.post(reverse("team_approve", args=[waiting.pk])).status_code, 404, change)
            waiting.refresh_from_db()
            self.assertIsNone(waiting.approved_on)
            self.assertFalse(TeamAction.objects.exists())
            TeamKey.objects.filter(pk=key.pk).update(approves=True, revoked=None)
        # With the flag still there it approves
        self.assertEqual(browser.post(reverse("team_approve", args=[waiting.pk])).status_code, 302)
        waiting.refresh_from_db()
        self.assertIsNotNone(waiting.approved_on)

    def test_by_must_name_somebody(self):
        waiting = Shop.objects.create(name="Forno Nuovo", approved_on=None)
        for by in ("", "   ", "\n"):
            with self.assertRaises(ValueError):
                approval.approve_or_hold(Shop.objects.filter(pk=waiting.pk), None, by=by)
        waiting.refresh_from_db()
        self.assertIsNone(waiting.approved_on)

    def test_the_you_page_speaks_for_each_key(self):
        key, browser = self.moderator()
        self.assertIn("nothing here changes a shop", browser.get("/team/me/").content.decode())
        TeamKey.objects.filter(pk=key.pk).update(approves=True)
        page = browser.get("/team/me/").content.decode()
        self.assertNotIn("nothing here changes a shop", page)
        self.assertIn("puts a new shop on the map only when you approve it", page)

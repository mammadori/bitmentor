"""The volunteers' area, stage 3 (team/area.py): approving new shops, the OpenStreetMap requests, the register of the
changes to the owners' accounts."""
import re
from datetime import timedelta
from unittest.mock import patch

import coincurve
from django.contrib.admin.models import LogEntry
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.urls import reverse
from django.utils import timezone

from shops import approval, moderators
from shops.models import AccountChange, OsmRequest, Profile, Shop
from shops.test_guard import sign_in_as_staff_session

from .models import TeamAction, TeamInvite, TeamKey
from .test_area import ADDRESS, RECOVERY, AreaBase
from .tests import k1_of, signed

User = get_user_model()
OLD = timezone.now() - timedelta(days=400)


class NewShops(AreaBase):
    """Approving new shops: only for the keys an administrator marked, never a shop that looks like a copy."""

    def setUp(self):
        super().setUp()
        baker = User.objects.create_user("baker", date_joined=OLD)
        Profile.objects.create(user=baker, email="baker.secret@mail.example")
        self.waiting = Shop.objects.create(
            name="Forno Nuovo", approved_on=None, owner=baker, city="Lucca", address="Via Roma 1",
            website="https://forno.example/", phone="+39 0583 1", show_phone=True,
            lightning_address="forno.secret@wallet.example", description=f"Bread. Pay {ADDRESS}")

    def approver(self):
        key, browser = self.moderator("Ada")
        TeamKey.objects.filter(pk=key.pk).update(approves=True)
        key.refresh_from_db()
        return key, browser

    def test_a_key_that_approves_sees_them_and_approves(self):
        key, browser = self.approver()
        page = browser.get("/team/shops/")
        self.assertEqual(page.status_code, 200)
        content = page.content.decode()
        for text in ("Forno Nuovo", "Lucca", "Via Roma 1", "https://forno.example/", "+39 0583 1", "me…@wallet.example"):
            self.assertIn(text, content)
        for text in ("forno.secret", "baker.secret", ADDRESS, "/admin/"):
            self.assertNotIn(text, content)
        self.assertIn("You may approve new shops", browser.get("/team/me/").content.decode())
        self.assertIn(reverse("team_approve", args=[self.waiting.pk]), content)
        logged = LogEntry.objects.count()
        response = browser.post(reverse("team_approve", args=[self.waiting.pk]))
        self.assertRedirects(response, f"/team/shops/#shop-{self.waiting.pk}", fetch_redirect_response=False)
        self.waiting.refresh_from_db()
        self.assertIsNotNone(self.waiting.approved_on)
        self.assertEqual(self.waiting.approved_by, f"[team key {key.pk}] Ada")
        row = TeamAction.objects.get()
        self.assertEqual((row.action, row.target), (TeamAction.Action.APPROVED, f"shop {self.waiting.pk}: Forno Nuovo"))
        # No account, so nothing in the admin's history: the TeamAction row is the record, approved_by says who
        self.assertEqual(LogEntry.objects.count(), logged)
        browser.post(reverse("team_approve", args=[self.waiting.pk]))
        self.assertEqual(TeamAction.objects.count(), 1)
        boss = self.client_class()
        sign_in_as_staff_session(boss, User.objects.create_superuser("boss"))
        self.assertContains(boss.get(reverse("admin:shops_shop_change", args=[self.waiting.pk])), f"[team key {key.pk}] Ada")

    def test_a_key_that_does_not_approve_gets_404(self):
        _key, browser = self.moderator()
        self.assertEqual(browser.get("/team/shops/").status_code, 404)
        self.assertEqual(browser.post(reverse("team_approve", args=[self.waiting.pk])).status_code, 404)
        self.waiting.refresh_from_db()
        self.assertIsNone(self.waiting.approved_on)
        # The day's list shows the shop as today, with no button and no way to approve it
        page = browser.get("/team/today/").content.decode()
        self.assertIn("Forno Nuovo", page)
        self.assertNotIn("/team/shops/", page)
        self.assertNotIn("Approve", page)
        self.assertFalse(TeamAction.objects.exists())
        self.assertIn("You do not approve new shops", browser.get("/team/me/").content.decode())

    def test_a_shop_that_looks_like_a_copy_stays_for_an_administrator(self):
        Shop.objects.create(name="Forno Nuovo", owner=User.objects.create_user("other", date_joined=OLD), city="Lucca",
                            approved_on=timezone.now())
        _key, browser = self.approver()
        page = browser.get("/team/shops/").content.decode()
        self.assertIn("This shop is not approved from here", page)
        self.assertNotIn(reverse("team_approve", args=[self.waiting.pk]), page)
        # Whatever the form sends
        for data in ({}, {"checked": "1"}, {"confirmed": "1"}):
            self.assertEqual(browser.post(reverse("team_approve", args=[self.waiting.pk]), data).status_code, 302)
        self.waiting.refresh_from_db()
        self.assertIsNone(self.waiting.approved_on)
        self.assertFalse(TeamAction.objects.exists())

    def test_when_the_signals_cannot_be_computed_nothing_is_approved(self):
        _key, browser = self.approver()
        with patch("shops.approval_signals.signals_of", side_effect=RuntimeError("down")):
            self.assertIn("The checks could not be run", browser.get("/team/shops/").content.decode())
            browser.post(reverse("team_approve", args=[self.waiting.pk]))
        self.waiting.refresh_from_db()
        self.assertIsNone(self.waiting.approved_on)
        self.assertFalse(TeamAction.objects.exists())

    def test_the_same_shops_in_the_same_order_as_the_admin(self):
        Shop.objects.create(name="Bar Due", approved_on=None)
        _key, browser = self.approver()
        account = User.objects.create_user("mod", is_staff=True)
        account.groups.add(Group.objects.get(name=moderators.GROUP))
        admin = self.client_class()
        sign_in_as_staff_session(admin, account)
        theirs = [item.shop.pk for item in admin.get("/admin/today/").context["items"] if item.code == "approve"]
        ours = [row["shop"].pk for row in browser.get("/team/shops/").context["rows"]]
        self.assertEqual(ours, theirs)
        self.assertEqual(len(ours), 2)

    def test_approve_or_hold_needs_a_user_or_a_name(self):
        with self.assertRaises(ValueError):
            approval.approve_or_hold(Shop.objects.filter(pk=self.waiting.pk), None)
        boss = User.objects.create_superuser("boss2")
        with self.assertRaises(ValueError):
            approval.approve_or_hold(Shop.objects.filter(pk=self.waiting.pk), boss, by="x")

    def test_set_on_the_invite_and_on_the_key_in_the_admin(self):
        boss = self.client_class()
        sign_in_as_staff_session(boss, User.objects.create_superuser("boss"))
        boss.post(reverse("admin:team_teaminvite_add"), {"name": "Nina", "role": "moderator", "approves": "on"})
        self.assertTrue(TeamInvite.objects.get(name="Nina").approves)
        invite, token = self.invite("Lia", "moderator")
        TeamInvite.objects.filter(pk=invite.pk).update(approves=True)
        page = self.client.get(f"/team/join/{token}/").content.decode()
        lnurl = re.search(r'href="lightning:([A-Z0-9]+)"', page)[1]
        self.client.get("/team/auth/", signed(k1_of(lnurl), coincurve.PrivateKey()))
        self.assertTrue(TeamKey.objects.get(name="Lia").approves)
        key, _browser = self.moderator("Ugo")
        boss.post(reverse("admin:team_teamkey_change", args=[key.pk]), {"name": "Ugo", "role": "moderator", "approves": "on"})
        key.refresh_from_db()
        self.assertTrue(key.approves)
        history = LogEntry.objects.filter(object_id=str(key.pk), content_type__app_label="team").latest("action_time")
        self.assertIn("approve", history.get_change_message().lower())


class OsmRequests(AreaBase):
    def setUp(self):
        super().setUp()
        self.ask = OsmRequest.objects.create(shop=self.shop, kind=OsmRequest.Kind.ADD,
                                             summary=f"name = Bar\nwebsite = https://bar.example/\ncontact = {ADDRESS}")

    def test_the_page_and_mark_as_done(self):
        key, browser = self.moderator()
        page = browser.get("/team/osm/").content.decode()
        for text in ("Bar", "website = https://bar.example/", "openstreetmap.org/edit", "me…@wallet.example"):
            self.assertIn(text, page)
        for text in (ADDRESS, RECOVERY, "/admin/"):
            self.assertNotIn(text, page)
        browser.post(reverse("team_osm_done", args=[self.ask.pk]))
        self.ask.refresh_from_db()
        self.assertEqual(self.ask.state, OsmRequest.State.DONE)
        self.assertEqual(self.ask.done_by, f"[team key {key.pk}] Mara")
        self.assertEqual(TeamAction.objects.get().action, TeamAction.Action.OSM_DONE)
        browser.post(reverse("team_osm_done", args=[self.ask.pk]))
        self.assertEqual(TeamAction.objects.count(), 1)
        self.assertIn(f"[team key {key.pk}] Mara", browser.get("/team/osm/").content.decode())
        # The day's list points to the page, not to the admin
        self.assertFalse(OsmRequest.objects.filter(state=OsmRequest.State.OPEN).exists())

    def test_closed_from_the_admin_names_nobody(self):
        OsmRequest.objects.filter(pk=self.ask.pk).update(state=OsmRequest.State.DECLINED, done=timezone.now(),
                                                          done_by="bossname", answer="Not a shop")
        _key, browser = self.moderator()
        page = browser.get("/team/osm/").content.decode()
        self.assertNotIn("bossname", page)
        self.assertIn("from the administration", page)
        self.assertEqual(browser.post(reverse("team_osm_done", args=[self.ask.pk])).status_code, 302)
        self.assertFalse(TeamAction.objects.exists())
        self.assertEqual(browser.post(reverse("team_osm_done", args=[999999])).status_code, 404)

    def test_the_days_list_links_to_the_page(self):
        _key, browser = self.moderator()
        self.assertIn("/team/osm/", browser.get("/team/today/").content.decode())


class AccountChanges(AreaBase):
    def test_the_register_with_who_did_it_and_nothing_of_the_account(self):
        for number in range(60):
            AccountChange.objects.create(user=self.owner, shops=f"Bar {number}", changed="username,shop_lightning",
                                         actor="bossname", via=AccountChange.Via.ADMIN, told="email", not_told="telegram")
        _key, browser = self.moderator()
        page = browser.get("/team/changes/")
        self.assertEqual(page.status_code, 200)
        main = page.content.decode().split("<main")[1]
        self.assertIn("Bar 59", main)
        self.assertNotIn("Bar 5<", main)
        self.assertIn("bossname", main)
        self.assertIn("?page=2", main)
        for text in (RECOVERY, ADDRESS, "/admin/", ">owner<"):
            self.assertNotIn(text, main)
        self.assertIn("Bar 0<", browser.get("/team/changes/?page=2").content.decode())
        self.assertEqual(browser.get("/team/changes/?page=x").status_code, 200)
        self.assertEqual(browser.post("/team/changes/").status_code, 405)
        # The day's list points to it
        self.assertIn("/team/changes/", browser.get("/team/today/").content.decode())

"""«Your shop on OpenStreetMap» for the merchant, and what the association sees (shops/osm_views.py, osm_request.py): who may open
the page, what it says from the database alone, the request «The association does it for me» (the tick, the CSRF token, the
limits, one open request per shop, the message to the moderators), the administration, the dashboard and the panel. Nothing
here writes to OpenStreetMap or asks any server."""
import threading
import time
from datetime import timedelta
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.db import IntegrityError, connection, connections, transaction
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone, translation

from config import features

from . import dashboard, osm, osm_request
from .models import OsmRequest, OsmRun, Profile, Shop
from .notifications import NotificationFailed
from .test_experimental import on
from .tests import admin_session, reset_limits

User = get_user_model()
CALLED_OUT = AssertionError("The page must not ask another server")
MODERATORS = "-100500"
SETTINGS = {"bbox": [41.8, 12.4, 42.0, 12.7], "discount_tag": "currency:XBT:discount",
            "operator": {"tag": "payment:bitcoin:operator", "value": "operator.example"}}
HOUR = 1_800_000_000


def in_one_hour():
    return patch("config.ratelimit.time.time", return_value=HOUR)


def community_with_osm(settings=SETTINGS):
    return patch("shops.osm.community", return_value={"osm": settings} if settings else {})


class Base(TestCase):
    def setUp(self):
        reset_limits()
        for patcher in (community_with_osm(),
                        patch("requests.sessions.Session.request", side_effect=CALLED_OUT)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.owner = User.objects.create(username="merchant")
        Profile.objects.create(user=self.owner, email="mario.rossi@example.org")
        self.shop = Shop.objects.create(
            owner=self.owner, name="Central Bar", city="Town", lightning_address="bar@ln.example.org",
            latitude=41.9, longitude=12.5, discount=10, lightning_worked=timezone.now() - timedelta(days=3))
        self.client.force_login(self.owner)

    def url(self, name, shop=None):
        with translation.override("en"):
            return reverse(name, args=[(shop or self.shop).pk])

    def page(self, **extra):
        return self.client.get(self.url("osm_home"), **extra)

    def tick(self, **extra):
        return self.client.post(self.url("osm_ask"), {"association": "on"}, **extra)


class Access(Base):
    def routes(self):
        return [("get", self.url("osm_home")), ("post", self.url("osm_ask"))]

    def test_a_stranger_is_sent_to_sign_in_and_nothing_is_made(self):
        self.client.logout()
        for verb, url in self.routes():
            response = getattr(self.client, verb)(url, {"association": "on"})
            self.assertEqual(response.status_code, 302, url)
            self.assertIn("/shops/", response["Location"])
        self.assertEqual(OsmRequest.objects.count(), 0)

    def test_another_merchant_does_not_see_the_shop_and_cannot_ask_for_it(self):
        self.client.force_login(User.objects.create(username="other"))
        for verb, url in self.routes():
            self.assertEqual(getattr(self.client, verb)(url, {"association": "on"}).status_code, 404, url)
        self.assertEqual(OsmRequest.objects.count(), 0)

    def test_the_staff_is_a_visitor_here_whoever_owns_the_shop(self):
        self.client.logout()
        admin_session(self.client)
        shop = Shop.objects.create(owner=User.objects.get(username="boss"), name="Staff shop", lightning_address="bar@ln.example.org")
        for verb, url in (("get", self.url("osm_home", shop)), ("post", self.url("osm_ask", shop))):
            response = getattr(self.client, verb)(url, {"association": "on"})
            self.assertEqual(response.status_code, 302, url)
            self.assertFalse(response.wsgi_request.user.is_authenticated)
        self.assertEqual(OsmRequest.objects.count(), 0)

    def test_not_indexed_and_posts_only(self):
        self.assertEqual(self.page()["X-Robots-Tag"], "noindex, nofollow")
        self.assertEqual(self.client.get(self.url("osm_ask")).status_code, 405)

    def test_the_form_has_the_csrf_token_and_a_post_without_it_is_refused(self):
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.owner)
        self.assertEqual(strict.post(self.url("osm_ask"), {"association": "on"}).status_code, 403)
        self.assertEqual(OsmRequest.objects.count(), 0)
        self.assertEqual(self.page().content.decode().count("csrfmiddlewaretoken"), 1)

    def test_a_community_without_openstreetmap_has_no_page_and_no_card(self):
        with community_with_osm(None):
            self.assertEqual(self.page().status_code, 404)
            self.assertEqual(self.tick().status_code, 404)
            self.assertNotContains(self.client.get(reverse("shops")), "OpenStreetMap")
        self.assertEqual(OsmRequest.objects.count(), 0)

    def test_robots_txt_keeps_the_page_out(self):
        with override_settings(SITE_MODE="production"):
            self.assertContains(self.client.get("/robots.txt"), "shops/openstreetmap/")


class Page(Base):
    def test_a_shop_not_linked_gets_the_lines_of_a_new_entry(self):
        response = self.page()
        self.assertContains(response, "Your shop is not on OpenStreetMap, or we have not found it")
        text = response.content.decode()
        for line in ("name=Central Bar", "currency:XBT=yes", "payment:lightning=yes", "currency:XBT:discount=10%",
                     "payment:bitcoin:operator=operator.example", "addr:city=Town", "check_date:currency:XBT="):
            self.assertIn(line, text)
        self.assertContains(response, "https://www.openstreetmap.org/edit?editor=id#map=19/41.900000/12.500000")
        self.assertContains(response, "Do it yourself, step by step")
        self.assertContains(response, "Press «Point»")

    def test_the_word_tag_is_explained_once(self):
        response = self.page()
        self.assertContains(response, "is described by tags: lines of information", count=1)

    def test_a_linked_shop_sees_what_differs(self):
        self.shop.osm_ref = "node/77"
        self.shop.save()
        osm.save_run({"proposals": [{"shop": str(self.shop.pk), "name": "Central Bar", "ref": "node/77", "changes": [
            {"tag": "payment:lightning", "old": None, "new": "yes", "kind": "add"},
            {"tag": "currency:XBT:discount", "old": "5%", "new": "10%", "kind": "update"},
            {"tag": "payment:bitcoin:operator", "old": "old.example", "new": "old.example;operator.example", "kind": "review"}]}]})
        response = self.page()
        self.assertContains(response, "Your shop is on OpenStreetMap")
        self.assertContains(response, "https://www.openstreetmap.org/node/77")
        self.assertContains(response, "https://www.openstreetmap.org/edit?editor=id&amp;node=77")
        self.assertContains(response, "these lines are missing or different there")
        self.assertContains(response, "payment:lightning=yes")
        self.assertContains(response, "currency:XBT:discount=10%")
        self.assertContains(response, "Click on your shop on the map")
        # What needs a person's judgement is the association's, not the merchant's
        self.assertNotContains(response, "old.example")
        self.assertNotContains(response, "payment:bitcoin:operator")

    def test_a_linked_shop_that_is_right(self):
        self.shop.osm_ref = "node/77"
        self.shop.save()
        report = {"created": timezone.now().isoformat(), "proposals": []}
        osm.save_run(report)
        Shop.objects.filter(pk=self.shop.pk).update(osm_seen=timezone.now())
        response = self.page()
        self.assertContains(response, "Nothing to do.")
        self.assertNotContains(response, "Do it yourself")

    def test_a_linked_shop_that_was_not_found(self):
        self.shop.osm_ref = "node/77"
        self.shop.save()
        osm.save_run({"proposals": [], "missing": [{"shop": str(self.shop.pk), "name": "Central Bar", "ref": "node/77"}]})
        response = self.page()
        self.assertContains(response, "The last check did not find it there")
        self.assertContains(response, "info@example.org")

    def test_a_linked_shop_not_compared_yet_shows_every_line(self):
        self.shop.osm_ref = "node/77"
        self.shop.save()
        response = self.page()
        self.assertContains(response, "We have not compared it yet")
        self.assertContains(response, "currency:XBT=yes")

    def test_a_shop_that_says_nothing_has_nothing_to_write_and_nothing_to_ask(self):
        Shop.objects.filter(pk=self.shop.pk).update(suspended=True)
        response = self.page()
        self.assertContains(response, "Nothing to write on OpenStreetMap for this shop now")
        self.assertNotContains(response, "Send the request")
        self.assertEqual(self.tick().status_code, 302)
        self.assertEqual(OsmRequest.objects.count(), 0)

    def test_the_page_asks_no_server_and_few_queries(self):
        with CaptureQueriesContext(connection) as done:
            self.assertEqual(self.page().status_code, 200)
        self.assertLessEqual(len(done), 20)

    def test_a_name_with_markup_is_escaped(self):
        Shop.objects.filter(pk=self.shop.pk).update(name='<script>alert(1)</script> "Bar"')
        text = self.page().content.decode()
        self.assertNotIn("<script>alert(1)</script>", text)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", text)

    def test_the_panel_has_the_card_with_the_state(self):
        response = self.client.get(reverse("shops"))
        self.assertContains(response, 'href="%s"' % self.url("osm_home"))
        self.assertContains(response, "Your shop on OpenStreetMap")
        self.tick()
        self.assertContains(self.client.get(reverse("shops")), "You asked the association about OpenStreetMap")
        OsmRequest.objects.update(state="done", done=timezone.now(), done_by="boss")
        self.assertContains(self.client.get(reverse("shops")), "Your shop on OpenStreetMap: done")


class Ask(Base):
    def test_the_tick_is_needed(self):
        response = self.client.post(self.url("osm_ask"), {})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Tick the box to send the request.")
        self.assertEqual(OsmRequest.objects.count(), 0)

    def test_a_request_for_a_shop_not_there_is_to_add_it(self):
        with override_settings(TELEGRAM_BOT_TOKEN="t", TELEGRAM_MODERATION_CHAT=MODERATORS), \
                patch("shops.osm_request.telegram", return_value=True) as chat:
            response = self.tick()
        self.assertRedirects(response, self.url("osm_home"), fetch_redirect_response=False)
        row = OsmRequest.objects.get()
        self.assertEqual((row.shop, row.asked_by, row.kind, row.state, row.delivery), (self.shop, self.owner, "add", "open", "sent"))
        self.assertIn("name = Central Bar", row.summary)
        self.assertIn("currency:XBT = yes", row.summary)
        self.assertEqual(chat.call_args.args[0], MODERATORS)
        text = chat.call_args.args[1]
        self.assertIn("Central Bar asks the association to put the shop on OpenStreetMap", text)
        self.assertIn(f"/admin/shops/osmrequest/{row.pk}/change/", text)
        self.assertNotIn("mario.rossi", text)
        self.assertNotIn("bar@ln.example.org", text)
        self.assertContains(self.page(), "You asked the association")

    def test_a_request_for_a_linked_shop_is_to_update_it(self):
        Shop.objects.filter(pk=self.shop.pk).update(osm_ref="node/77")
        with patch("shops.osm_request.telegram", return_value=None):
            self.tick()
        row = OsmRequest.objects.get()
        self.assertEqual((row.kind, row.delivery), ("update", "off"))

    def test_a_second_tap_is_one_request(self):
        with patch("shops.osm_request.telegram", return_value=True) as chat:
            self.tick()
            response = self.tick(follow=True)
        self.assertEqual(OsmRequest.objects.count(), 1)
        self.assertEqual(chat.call_count, 1)
        self.assertContains(response, "You asked a moment ago, and we have your request.")

    def test_the_database_allows_one_open_request_per_shop(self):
        OsmRequest.objects.create(shop=self.shop, kind="add")
        with self.assertRaises(IntegrityError), transaction.atomic():
            OsmRequest.objects.create(shop=self.shop, kind="add")
        OsmRequest.objects.update(state="done")
        OsmRequest.objects.create(shop=self.shop, kind="add")

    def test_a_request_done_lets_the_next_one_in_up_to_the_daily_limit(self):
        with patch("shops.osm_request.telegram", return_value=True):
            for number in range(osm_request.ASK_PER_DAY):
                self.assertEqual(self.tick().status_code, 302)
                OsmRequest.objects.filter(state="open").update(state="done", done=timezone.now())
            response = self.tick(follow=True)
        self.assertEqual(OsmRequest.objects.count(), osm_request.ASK_PER_DAY)
        self.assertContains(response, "You have already asked 3 times today")

    def test_the_hourly_limit_of_a_person(self):
        with in_one_hour(), patch("shops.osm_request.telegram", return_value=True):
            for number in range(11):
                self.tick()
                OsmRequest.objects.filter(state="open").update(state="done", done=timezone.now(), created=timezone.now() - timedelta(days=2))
            response = self.tick(follow=True)
        self.assertContains(response, "Too many requests in a short time")
        self.assertEqual(OsmRequest.objects.count(), 10)

    def test_a_telegram_that_does_not_answer_never_loses_the_request(self):
        for outcome, delivery in ((NotificationFailed("503"), "failed"), (False, "refused"), (RuntimeError("boom"), "failed")):
            OsmRequest.objects.all().delete()
            reset_limits()
            effect = {"side_effect": outcome} if isinstance(outcome, Exception) else {"return_value": outcome}
            with patch("shops.osm_request.telegram", **effect):
                self.assertEqual(self.tick().status_code, 302)
            self.assertEqual(OsmRequest.objects.get().delivery, delivery)

    def test_the_messages_to_the_moderators_have_a_ceiling_and_the_request_stays(self):
        with in_one_hour(), patch("shops.osm_request.telegram", return_value=True) as chat:
            for number in range(osm_request_ceiling() + 2):
                shop = Shop.objects.create(owner=self.owner, name=f"Bar {number}", lightning_address="bar@ln.example.org",
                                           latitude=41.9, longitude=12.5)
                self.client.post(self.url("osm_ask", shop), {"association": "on"})
        self.assertEqual(chat.call_count, osm_request_ceiling())
        self.assertEqual(OsmRequest.objects.count(), osm_request_ceiling() + 2)
        self.assertTrue(OsmRequest.objects.filter(delivery="failed").exists())

    def test_a_made_up_shop_never_writes_to_the_real_chat(self):
        Shop.objects.filter(pk=self.shop.pk).update(external_id="bitmentor-demo:bar")
        with patch("shops.osm_request.telegram", return_value=True) as chat:
            self.tick()
        self.assertEqual(chat.call_count, 0)

    def test_what_a_merchant_could_write_goes_nowhere(self):
        with patch("shops.osm_request.telegram", return_value=True) as chat:
            self.client.post(self.url("osm_ask"), {"association": "on", "note": "@everyone http://phish.example", "kind": "update",
                                                   "summary": "evil", "state": "done"})
        row = OsmRequest.objects.get()
        self.assertEqual((row.kind, row.state, row.summary.startswith("name = ")), ("add", "open", True))
        self.assertNotIn("phish", chat.call_args.args[1])

    def test_nothing_reaches_openstreetmap(self):
        # setUp() makes any request fail the test; this one says it out loud
        with patch("shops.osm_net.request", side_effect=CALLED_OUT), patch("shops.osm_net.api", side_effect=CALLED_OUT), \
                patch("shops.osm_request.telegram", return_value=True):
            self.page()
            self.tick()
        self.assertEqual(OsmRequest.objects.count(), 1)

    def test_after_a_change_of_owner_the_new_one_does_not_read_the_old_ones_request(self):
        OsmRequest.objects.create(shop=self.shop, kind="add", asked_by=self.owner, state="done", done=timezone.now())
        newcomer = User.objects.create(username="newcomer")
        Shop.objects.filter(pk=self.shop.pk).update(owner=newcomer)
        self.shop.refresh_from_db()
        self.assertIsNone(osm_request.panel(self.shop))


def osm_request_ceiling():
    from .osm_views import MESSAGES_PER_IP
    return MESSAGES_PER_IP


class Administration(Base):
    def setUp(self):
        super().setUp()
        self.staff = Client()
        admin_session(self.staff)

    def test_the_link_is_set_and_checked_in_the_shop_page(self):
        url = reverse("admin:shops_shop_change", args=[self.shop.pk])
        response = self.staff.get(url)
        self.assertContains(response, 'name="osm_ref"')
        self.assertNotContains(response, 'name="osm_seen"')
        for bad in ("node/0", "node/", "way 12", "https://www.openstreetmap.org/node/12", "area/5", "node/012"):
            form = self.change(url, {"osm_ref": bad})
            self.assertEqual(form.status_code, 200, bad)
            self.assertTrue(form.context["adminform"].form.errors.get("osm_ref"), bad)
        self.assertEqual(Shop.objects.get(pk=self.shop.pk).osm_ref, "")
        self.assertEqual(self.change(url, {"osm_ref": "node/12345"}).status_code, 302)
        self.assertEqual(Shop.objects.get(pk=self.shop.pk).osm_ref, "node/12345")

    def change(self, url, more):
        shop = Shop.objects.get(pk=self.shop.pk)
        data = {"name": shop.name, "description": "", "address": "", "city": shop.city, "latitude": "41.9", "longitude": "12.5", "website": "",
                "email": "", "phone": "", "discount": "10", "listed": "on", "nostr_pubkey": "", "nip05_name": "", "telegram_chat": "",
                "owner": shop.owner_id, "external_id": "", "osm_ref": "", "closures-TOTAL_FORMS": "0", "closures-INITIAL_FORMS": "0",
                "closures-MIN_NUM_FORMS": "0", "closures-MAX_NUM_FORMS": "1000", **more}
        return self.staff.post(url, data)

    def test_two_shops_cannot_share_an_element(self):
        other = Shop.objects.create(name="Other", osm_ref="node/5")
        url = reverse("admin:shops_shop_change", args=[self.shop.pk])
        self.assertContains(self.change(url, {"osm_ref": "node/5"}), "Another shop is already linked to this element.")
        self.assertEqual(Shop.objects.get(pk=other.pk).osm_ref, "node/5")

    def test_the_list_filters_by_link(self):
        Shop.objects.create(name="Linked Bar", osm_ref="node/5")
        response = self.staff.get(reverse("admin:shops_shop_changelist") + "?osm=linked")
        self.assertContains(response, "Linked Bar")
        self.assertNotContains(response, "Central Bar")
        response = self.staff.get(reverse("admin:shops_shop_changelist") + "?osm=not_linked")
        self.assertContains(response, "Central Bar")
        self.assertNotContains(response, "Linked Bar")

    def test_the_readings_are_read_only_and_escape_what_openstreetmap_wrote(self):
        run = osm.save_run({"elements": 3, "skipped": 0, "candidates_total": 1, "proposals": [], "candidates": [
            {"ref": "node/9", "name": "<img src=x onerror=alert(1)>", "city": "", "date": ""}]})
        listing = self.staff.get(reverse("admin:shops_osmrun_changelist"))
        self.assertEqual(listing.status_code, 200)
        page = self.staff.get(reverse("admin:shops_osmrun_change", args=[run.pk]))
        self.assertContains(page, "&lt;img src=x onerror=alert(1)&gt;")
        self.assertNotContains(page, "<img src=x")
        self.assertEqual(self.staff.get(reverse("admin:shops_osmrun_add")).status_code, 403)
        self.assertEqual(self.staff.post(reverse("admin:shops_osmrun_delete", args=[run.pk]), {"post": "yes"}).status_code, 403)
        self.assertEqual(OsmRun.objects.count(), 1)

    def test_a_request_is_marked_done_with_who_did_it(self):
        row = OsmRequest.objects.create(shop=self.shop, kind="update", asked_by=self.owner)
        response = self.staff.post(reverse("admin:shops_osmrequest_changelist"), {"action": "mark_done", "_selected_action": [row.pk]})
        self.assertEqual(response.status_code, 302)
        row.refresh_from_db()
        self.assertEqual((row.state, row.done_by, row.done is not None), ("done", "boss", True))
        self.assertContains(self.client.get(self.url("osm_home")), "Done")

    def test_a_request_that_could_not_be_done_says_why_to_the_merchant(self):
        row = OsmRequest.objects.create(shop=self.shop, kind="add", asked_by=self.owner)
        url = reverse("admin:shops_osmrequest_change", args=[row.pk])
        self.assertEqual(self.staff.post(url, {"state": "declined", "answer": "We could not find the place on the map."}).status_code, 302)
        row.refresh_from_db()
        self.assertEqual((row.state, row.done_by), ("declined", "boss"))
        page = self.client.get(self.url("osm_home"))
        self.assertContains(page, "The association could not do it.")
        self.assertContains(page, "We could not find the place on the map.")
        # Reopened by mistake: it waits again, and nobody is named
        self.staff.post(url, {"state": "open", "answer": ""})
        row.refresh_from_db()
        self.assertEqual((row.state, row.done_by, row.done), ("open", "", None))

    def test_only_what_the_staff_may_change_is_in_the_form(self):
        row = OsmRequest.objects.create(shop=self.shop, kind="add", asked_by=self.owner)
        url = reverse("admin:shops_osmrequest_change", args=[row.pk])
        self.staff.post(url, {"state": "done", "answer": "", "kind": "update", "shop": "", "summary": "evil", "delivery": "sent"})
        row.refresh_from_db()
        self.assertEqual((row.kind, row.shop, row.summary == "evil", row.delivery), ("add", self.shop, False, ""))
        self.assertEqual(self.staff.get(reverse("admin:shops_osmrequest_add")).status_code, 403)
        self.assertEqual(self.staff.post(reverse("admin:shops_osmrequest_delete", args=[row.pk]), {"post": "yes"}).status_code, 403)

    def test_a_member_of_the_staff_without_the_right_does_not_change_requests(self):
        helper = User.objects.create(username="helper", is_staff=True)
        helper.user_permissions.add(Permission.objects.get(codename="view_osmrequest"))
        row = OsmRequest.objects.create(shop=self.shop, kind="add")
        client = Client()
        client.force_login(helper)
        session = client.session
        from config.urls import ADMIN_SESSION
        session[ADMIN_SESSION] = True
        session.save()
        client.post(reverse("admin:shops_osmrequest_changelist"), {"action": "mark_done", "_selected_action": [row.pk]})
        row.refresh_from_db()
        self.assertEqual(row.state, "open")


class Dashboard(Base):
    def codes(self):
        return [(item.code, item.shop.name if item.shop else None) for item in dashboard.summary()["items"]]

    def test_nothing_when_the_community_does_not_use_it(self):
        with community_with_osm(None):
            self.assertEqual([code for code, _name in self.codes() if code.startswith("osm")], [])

    @on(features.OSM_UPLOAD)
    def test_an_open_request_is_a_line_with_its_shop_on_top(self):
        row = OsmRequest.objects.create(shop=self.shop, kind="update", asked_by=self.owner)
        found = [item for item in dashboard.summary()["items"] if item.code == "osm_request"]
        self.assertEqual(len(found), 1)
        item = found[0]
        self.assertEqual((item.shop, item.level, item.since), (self.shop, "warning", row.created))
        self.assertIn("osm_upload", item.command)
        self.assertEqual(item.links[0]["url"], reverse("admin:shops_osmrequest_change", args=[row.pk]))
        self.assertEqual(dashboard.summary()["items"][0].code, "osm_request")
        OsmRequest.objects.update(state="done")
        self.assertNotIn("osm_request", [code for code, _name in self.codes()])

    def test_with_the_upload_off_a_request_is_done_by_hand_and_names_no_command(self):
        """`manage.py osm_upload` is an experimental feature, off unless the community turns it on."""
        OsmRequest.objects.create(shop=self.shop, kind="update", asked_by=self.owner)
        item = next(item for item in dashboard.summary()["items"] if item.code == "osm_request")
        self.assertEqual(item.command, "")
        self.assertIn("Update the shop's entry in the editor with the lines of the request", item.do)
        self.assertIn("The site never writes to OpenStreetMap by itself.", item.do)
        self.assertNotIn("command", item.do)
        # What the merchant is told is the same: a person does it by hand
        self.assertIn("a person of the association does it by hand", item.say)

    def test_with_the_upload_off_the_proposals_are_what_openstreetmap_should_say(self):
        run = osm.save_run({"proposals": [{"shop": "x", "name": "A", "ref": "node/1", "changes": [{"tag": "a", "old": None, "new": "b", "kind": "add"}] * 3}]})
        item = next(item for item in dashboard.summary()["items"] if item.code == "osm_proposals")
        self.assertEqual(item.what, "3 tags of our shops on OpenStreetMap are not what this site knows.")
        self.assertIn("A person can change them by hand on openstreetmap.org", item.do)
        self.assertIn("The site sends nothing.", item.do)
        self.assertEqual((item.command, item.level), ("", "info"))
        self.assertEqual(item.links[0]["url"], reverse("admin:shops_osmrun_change", args=[run.pk]))
        # The page of the staff names no upload either
        OsmRequest.objects.create(shop=self.shop, kind="update", asked_by=self.owner)
        staff = Client()
        admin_session(staff)
        page = staff.get(reverse("admin_dashboard"))
        self.assertContains(page, "3 tags of our shops on OpenStreetMap are not what this site knows.")
        self.assertNotContains(page, "osm_upload")
        with on(features.OSM_UPLOAD):
            self.assertContains(staff.get(reverse("admin_dashboard")), "osm_upload")

    def test_a_request_to_add_has_no_command(self):
        OsmRequest.objects.create(shop=self.shop, kind="add")
        item = next(item for item in dashboard.summary()["items"] if item.code == "osm_request")
        self.assertEqual(item.command, "")

    @on(features.OSM_UPLOAD)
    def test_proposals_waiting_are_a_line_that_says_nobody_sends_them_alone(self):
        run = osm.save_run({"proposals": [{"shop": "x", "name": "A", "ref": "node/1", "changes": [{"tag": "a", "old": None, "new": "b", "kind": "add"}] * 3}]})
        item = next(item for item in dashboard.summary()["items"] if item.code == "osm_proposals")
        self.assertIn("3 changes proposed for OpenStreetMap", item.what)
        self.assertIn("never sends them by itself", item.do)
        self.assertEqual(item.level, "info")
        self.assertEqual(item.links[0]["url"], reverse("admin:shops_osmrun_change", args=[run.pk]))
        osm.save_run({"proposals": []})
        self.assertNotIn("osm_proposals", [code for code, _name in self.codes()])

    def test_a_reading_that_failed_or_is_late(self):
        self.assertIn("osm_late", [code for code, _name in self.codes()])
        osm.save_run(error="the server answered 504")
        self.assertIn("osm_failed", [code for code, _name in self.codes()])
        osm.save_run({"proposals": []})
        self.assertEqual([code for code, _name in self.codes() if code.startswith("osm")], [])
        OsmRun.objects.update(created=timezone.now() - timedelta(days=osm.LATE_DAYS + 1))
        self.assertIn("osm_late", [code for code, _name in self.codes()])

    def test_the_admin_page_shows_the_lines(self):
        OsmRequest.objects.create(shop=self.shop, kind="add")
        staff = Client()
        admin_session(staff)
        page = staff.get(reverse("admin_dashboard"))
        self.assertContains(page, "Asked the association to put the shop on OpenStreetMap.")
        self.assertContains(page, "never writes to OpenStreetMap by itself")


@skipUnless(connection.vendor == "postgresql", "two connections at once need PostgreSQL")
class TwoAtOnce(TransactionTestCase):
    """Two taps, or two readings, on two connections at the same moment."""

    def run_two(self, work):
        start = threading.Barrier(2)
        results = []

        def go(argument):
            try:
                start.wait()
                results.append(work(argument))
            finally:
                connections.close_all()

        threads = [threading.Thread(target=go, args=(number,)) for number in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return results

    def test_two_taps_on_the_tick_make_one_request(self):
        owner = User.objects.create(username="merchant")
        shop = Shop.objects.create(owner=owner, name="Bar", lightning_address="bar@ln.example.org", latitude=41.9, longitude=12.5)
        real = OsmRequest.objects.create

        def slow_create(**fields):
            time.sleep(0.5)
            return real(**fields)

        with community_with_osm(), patch.object(OsmRequest.objects, "create", side_effect=slow_create):
            results = self.run_two(lambda _number: osm_request.create(Shop.objects.get(pk=shop.pk), owner)[1])
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(shop.osm_requests.count(), 1)

    def test_two_shops_cannot_confirm_the_same_element_at_once(self):
        shops = [Shop.objects.create(name=f"Bar {number}", osm_proposed="node/9") for number in range(2)]
        results = self.run_two(lambda number: osm.confirm_link(Shop.objects.get(pk=shops[number].pk)))
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(Shop.objects.filter(osm_ref="node/9").count(), 1)
        # The one that lost keeps its proposal, to be refused or to be linked by hand to something else
        self.assertEqual(Shop.objects.filter(osm_proposed="node/9").count(), 1)

    def test_two_readings_propose_one_link(self):
        mine = Shop.objects.create(name="Bar")
        element = osm.Element("node/9", {"name": "Bar"})
        results = self.run_two(lambda _number: osm.propose_link(Shop.objects.get(pk=mine.pk), element))
        self.assertEqual(sorted(results), [False, True])


class Honest(Base):
    """The page never says «all is well» when it is not: a review waiting, a «no» there, an old reading."""

    def linked(self, report, age=timedelta(0)):
        Shop.objects.filter(pk=self.shop.pk).update(osm_ref="node/77")
        self.shop.refresh_from_db()
        created = timezone.now() - age
        run = osm.save_run({"created": created.isoformat(), "proposals": [], **report})
        Shop.objects.filter(pk=self.shop.pk).update(osm_seen=timezone.now() - age + timedelta(minutes=1))
        return run

    def test_all_is_well_says_when_it_was_read(self):
        run = self.linked({})
        response = self.page()
        self.assertContains(response, "Nothing to do.")
        self.assertContains(response, "As of the check of")
        self.assertEqual(osm.page(self.shop)["read"], run.created)

    def test_a_review_waiting_is_not_all_well(self):
        self.linked({"proposals": [{"shop": str(self.shop.pk), "name": "Central Bar", "ref": "node/77", "changes": [
            {"tag": "payment:bitcoin:operator", "old": "old.example", "new": "old.example;operator.example", "kind": "review"}]}]})
        response = self.page()
        self.assertNotContains(response, "Nothing to do.")
        self.assertContains(response, "a person of the association has to look at it")
        self.assertNotContains(response, "old.example")
        state = osm.page(self.shop)
        self.assertEqual((state["up_to_date"], state["review"], state["lines"]), (False, True, ""))

    def test_a_no_there_is_said_and_the_merchant_can_ask(self):
        self.linked({"osm_says_no": [{"shop": str(self.shop.pk), "name": "Central Bar", "ref": "node/77", "state": "yes", "ours": "", "theirs": ""}]})
        response = self.page()
        self.assertNotContains(response, "Nothing to do.")
        self.assertContains(response, "OpenStreetMap says that your shop does not take bitcoin any more.")
        self.assertContains(response, "Send the request")

    def test_an_entry_that_could_not_be_read_whole_is_not_all_well_either(self):
        self.linked({"unreliable": [{"shop": str(self.shop.pk), "name": "Central Bar", "ref": "node/77"}]})
        self.assertNotContains(self.page(), "Nothing to do.")

    def test_an_old_reading_is_not_all_well(self):
        self.linked({}, age=timedelta(days=osm.LATE_DAYS + 3))
        response = self.page()
        self.assertNotContains(response, "Nothing to do.")
        self.assertContains(response, "We have not compared it for a while.")
        self.assertContains(response, "currency:XBT=yes")
        self.assertTrue(osm.page(self.shop)["stale"])


class ConfirmLinks(Base):
    """The link a reading proposes becomes the shop's only when the association confirms it in /admin/."""

    def setUp(self):
        super().setUp()
        self.staff = Client()
        admin_session(self.staff)
        Shop.objects.filter(pk=self.shop.pk).update(osm_proposed="node/77")
        self.shop.refresh_from_db()

    def act(self, name):
        return self.staff.post(reverse("admin:shops_shop_changelist"), {"action": name, "_selected_action": [self.shop.pk]})

    def test_confirm(self):
        self.assertEqual(self.act("confirm_osm_link").status_code, 302)
        self.shop.refresh_from_db()
        self.assertEqual((self.shop.osm_ref, self.shop.osm_proposed), ("node/77", ""))

    def test_refuse_is_remembered(self):
        self.act("reject_osm_link")
        self.shop.refresh_from_db()
        self.assertEqual((self.shop.osm_ref, self.shop.osm_proposed, osm.notes_of(self.shop)["rejected"]), ("", "", ["node/77"]))

    def test_the_actions_need_the_right_to_change(self):
        helper = User.objects.create(username="helper", is_staff=True)
        helper.user_permissions.add(Permission.objects.get(codename="view_shop"))
        client = Client()
        client.force_login(helper)
        session = client.session
        from config.urls import ADMIN_SESSION
        session[ADMIN_SESSION] = True
        session.save()
        for name in ("confirm_osm_link", "reject_osm_link", "accept_osm_contacts", "decline_osm_contacts"):
            client.post(reverse("admin:shops_shop_changelist"), {"action": name, "_selected_action": [self.shop.pk]})
        self.shop.refresh_from_db()
        self.assertEqual((self.shop.osm_ref, self.shop.osm_proposed), ("", "node/77"))

    def test_the_shop_page_shows_the_proposal_with_a_link_and_the_offers(self):
        osm.fill(self.shop, osm.Element("node/77", {"website": "https://bar.example/"}), osm.settings_from(SETTINGS))
        page = self.staff.get(reverse("admin:shops_shop_change", args=[self.shop.pk]))
        self.assertContains(page, "https://www.openstreetmap.org/node/77")
        self.assertContains(page, "https://bar.example/")
        self.assertNotContains(page, 'name="osm_proposed"')

    def test_a_link_written_by_hand_clears_the_proposal(self):
        url = reverse("admin:shops_shop_change", args=[self.shop.pk])
        shop = self.shop
        data = {"name": shop.name, "description": "", "address": "", "city": shop.city, "latitude": "41.9", "longitude": "12.5", "website": "",
                "email": "", "phone": "", "discount": "10", "listed": "on", "nostr_pubkey": "", "nip05_name": "", "telegram_chat": "",
                "owner": shop.owner_id, "external_id": "", "osm_ref": "node/12", "closures-TOTAL_FORMS": "0", "closures-INITIAL_FORMS": "0",
                "closures-MIN_NUM_FORMS": "0", "closures-MAX_NUM_FORMS": "1000"}
        self.assertEqual(self.staff.post(url, data).status_code, 302)
        shop.refresh_from_db()
        self.assertEqual((shop.osm_ref, shop.osm_proposed), ("node/12", ""))

    def test_the_contacts_are_accepted_or_declined_in_the_admin(self):
        Shop.objects.filter(pk=self.shop.pk).update(osm_ref="node/77", osm_proposed="")
        self.shop.refresh_from_db()
        osm.fill(self.shop, osm.Element("node/77", {"website": "https://bar.example/", "phone": "+39 0461 123456"}), osm.settings_from(SETTINGS))
        self.shop.refresh_from_db()
        self.assertEqual((self.shop.website, self.shop.phone), ("", ""))
        self.act("accept_osm_contacts")
        self.shop.refresh_from_db()
        self.assertEqual((self.shop.website, self.shop.phone, osm.offers_of(self.shop)), ("https://bar.example/", "+39 0461 123456", {}))

    def test_the_dashboard_has_a_line_for_each_kind_of_waiting(self):
        Shop.objects.create(name="Other", osm_ref="node/5", osm_notes={"fields": {"phone": {"value": "+39 1", "state": "offered"}}})
        codes = [item.code for item in dashboard.summary()["items"]]
        self.assertIn("osm_links", codes)
        self.assertIn("osm_contacts", codes)
        page = self.staff.get(reverse("admin_dashboard"))
        self.assertContains(page, "1 shop has a link to OpenStreetMap proposed")
        self.assertContains(page, "1 shop has a website or a phone offered by OpenStreetMap")


class OwnerChange(Base):
    """The shop changed hands: the new owner does not see, nor is blocked by, the old owner's request."""

    def test_the_new_owner_neither_sees_nor_inherits_the_old_request(self):
        old = OsmRequest.objects.create(shop=self.shop, kind="update", asked_by=self.owner)
        newcomer = User.objects.create(username="newcomer")
        Shop.objects.filter(pk=self.shop.pk).update(owner=newcomer)
        self.client.force_login(newcomer)
        page = self.page()
        self.assertNotContains(page, "You asked the association")
        self.assertContains(page, "Send the request")
        with patch("shops.osm_request.telegram", return_value=True) as chat:
            response = self.tick(follow=True)
        self.assertNotContains(response, "You asked a moment ago")
        old.refresh_from_db()
        mine = OsmRequest.objects.exclude(pk=old.pk).get()
        self.assertEqual((old.state, mine.state, mine.asked_by), ("declined", "open", newcomer))
        self.assertIn("changed owner", old.done_by)
        self.assertEqual(chat.call_count, 1)
        self.assertEqual(OsmRequest.objects.filter(shop=self.shop, state="open").count(), 1)

    def test_the_same_owner_still_finds_their_open_request(self):
        row = OsmRequest.objects.create(shop=self.shop, kind="update", asked_by=self.owner)
        with patch("shops.osm_request.telegram", return_value=True) as chat:
            response = self.tick(follow=True)
        self.assertContains(response, "You asked a moment ago, and we have your request.")
        row.refresh_from_db()
        self.assertEqual((row.state, chat.call_count), ("open", 0))

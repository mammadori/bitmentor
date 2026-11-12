"""OpenStreetMap, where the token may go and what is kept of each decision: the token and a backslash in an address, every payment
method at "no", what a reading did once and a summary that did not go out, who decided what and when, and the changeset's
words."""
import io
import socket
from datetime import timedelta
from unittest.mock import patch

from django.conf import settings
from django.core.management import CommandError, call_command
from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from . import osm, osm_edit, osm_net
from .models import OsmRun, Shop
from .test_osm import AREA, CALLED_OUT, SETTINGS, TODAY, Guard, answer, config, day, fake_overpass, node, read, shop
from .tests import admin_session


class Hosts(SimpleTestCase):
    """The token goes where `requests` will connect, and nowhere else: a name the two parsers read differently is refused."""

    ACCEPTED = ("https://api.openstreetmap.org/api/0.6", "https://API.OPENSTREETMAP.ORG/api/0.6", "https://api.openstreetmap.org:443/api/0.6",
                "https://master.apis.dev.openstreetmap.org/api/0.6")
    REFUSED = ("https://evil.example\\.openstreetmap.org/api", "https://evil.example\\@api.openstreetmap.org/api/0.6",
               "https://evil.example\\x.openstreetmap.org/api", "https://api.openstreetmap.org\\.evil.example/api",
               "https://api.openstreetmap.org@evil.example/api/0.6", "https://evil.example#@api.openstreetmap.org/api/0.6",
               "https://evil.example/.openstreetmap.org/api", "https://api.openstreetmap.org.evil.example/api/0.6",
               "https://evilopenstreetmap.org/api/0.6", "https://api.openstreetmap.org./api/0.6", "https://xn--openstreetmap-x.org/api/0.6",
               "https://api.openstreetmap.org:8443/api/0.6", "https://api.openstreetmap.org%2f.evil.example/api",
               "https://api.openstreetmap.org\t.evil.example/api", "https://evil.example?.openstreetmap.org",
               "http://api.openstreetmap.org/api/0.6", "https://\uff11\uff12\uff17.\uff10.\uff10.\uff11/api", "https://api.openstreetmap.org /api/0.6",
               "https://evil.example%23.openstreetmap.org/api", "https://evil.example%5c.openstreetmap.org/api", "https://api.openstreetmap.org%00.evil.example/api",
               "https://openstreetmap.org/api/0.6", "https://api06.dev.openstreetmap.org/api/0.6")

    def test_the_settings_take_the_exact_servers_only(self):
        for url in self.ACCEPTED:
            self.assertEqual(osm.settings_from({"bbox": AREA, "api_url": url}).api_url, url.rstrip("/"), url)
        for url in self.REFUSED:
            told = []
            found = osm.settings_from({"bbox": AREA, "api_url": url}, told.append)
            self.assertEqual(found.api_url, osm.DEFAULT_API, url)
            self.assertTrue(told, url)

    def test_whatever_is_kept_leads_where_requests_would_connect(self):
        from urllib3.util import parse_url
        for url in (*self.ACCEPTED, *self.REFUSED):
            found = osm.settings_from({"bbox": AREA, "api_url": url}).api_url
            self.assertIn(parse_url(found + "/changeset/create").host, osm.API_HOSTS, url)

    def test_a_backslash_never_gets_the_token_out(self):
        import urllib3.util.connection as connection
        for url in self.REFUSED:
            hand_made = osm.Settings(bbox=(41.8, 12.4, 42.0, 12.7), api_url=url)
            seen = []

            def connect(address, *args, **kwargs):
                seen.append(address)
                raise OSError("no network in a test")

            with patch.object(connection, "create_connection", connect), patch.object(socket, "create_connection", connect), \
                    patch("requests.sessions.Session.request", side_effect=CALLED_OUT), self.assertRaises(osm.OsmError, msg=url):
                osm_net.api(hand_made, "PUT", "changeset/create", "SECRET-TOKEN", b"<osm/>")
            self.assertEqual(seen, [], url)

    def test_the_two_parsers_must_agree(self):
        self.assertEqual(osm.plain_host("https://api.openstreetmap.org/api/0.6"), "api.openstreetmap.org")
        self.assertEqual(osm.plain_host("https://overpass.example/api/interpreter?data=out%3Bx&a=%25"), "overpass.example")
        for url in ("https://evil.example\\.openstreetmap.org/api", "https:///x", "https://", "ftp-less", None, 5, "https://a_b.example/x",
                    "https://exa mple.org/", "https://example.org\n/"):
            self.assertEqual(osm.plain_host(url), "", url)

    def test_a_name_that_does_not_resolve_is_not_public(self):
        with patch("shops.osm_net.socket.getaddrinfo", side_effect=socket.gaierror(socket.EAI_NONAME, "no such name")):
            self.assertFalse(osm_net.public("nowhere.example"))
            with patch("requests.sessions.Session.request", side_effect=CALLED_OUT), self.assertRaisesMessage(osm.OsmError, "not a public https server"):
                osm_net.request("GET", "https://nowhere.example/x", seconds=5, size=100)
        with patch("shops.osm_net.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 443))]):
            self.assertTrue(osm_net.public("example.org"))
        with patch("shops.osm_net.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 443)), (2, 1, 6, "", ("10.0.0.1", 443))]):
            self.assertFalse(osm_net.public("mixed.example"))
        with patch("shops.osm_net.socket.getaddrinfo", return_value=[]):
            self.assertFalse(osm_net.public("empty.example"))


class EveryMethodAtNo(TestCase):
    """«No» is also every payment method at no with no currency:XBT, and a single Lightning no."""

    def changes(self, mine, element, **more):
        return sorted(osm.changes_for(mine, read(answer(element))[0], config(**more), TODAY))

    def test_nothing_is_proposed_after_a_more_recent_no_of_that_kind(self):
        mine = shop(discount=10, lightning_worked=day(days=300))
        recent = timezone.localtime(timezone.now() - timedelta(days=20)).date().isoformat()
        for tags in ({"payment:lightning": "no", "payment:onchain": "no", "check_date": recent}, {"payment:lightning": "no", "check_date": recent},
                     {"payment:onchain": "no", "check_date:payment:onchain": recent}, {"payment:bitcoin": "no", "survey:date": recent}):
            self.assertEqual(self.changes(mine, node(1, "Bar Aurora", **tags)), [], tags)

    def test_everything_waits_for_a_person_when_our_check_is_more_recent(self):
        mine = shop(discount=10, lightning_worked=day(days=3))
        old = timezone.localtime(timezone.now() - timedelta(days=200)).date().isoformat()
        for tags in ({"payment:lightning": "no", "payment:onchain": "no", "check_date": old}, {"payment:lightning": "no", "check_date": old}):
            found = self.changes(mine, node(1, "Bar Aurora", **tags))
            self.assertTrue(found, tags)
            self.assertEqual({kind for *_rest, kind in found}, {"review"}, tags)
            self.assertIn(("currency:XBT", None, "yes", "review"), found)

    def test_a_yes_anywhere_is_not_a_no(self):
        shop(lightning_worked=day(days=300))
        recent = timezone.localtime(timezone.now() - timedelta(days=20)).date().isoformat()
        element = read(answer(node(1, "Bar Aurora", **{"payment:lightning": "no", "payment:onchain": "yes", "check_date": recent})))[0]
        self.assertFalse(osm.refuses(element))
        self.assertTrue(osm.refuses(read(answer(node(2, "Bar", **{"currency:XBT": "no"})))[0]))
        self.assertTrue(osm.refuses(read(answer(node(3, "Bar", **{"payment:lightning": "no"})))[0]))
        self.assertFalse(osm.refuses(read(answer(node(4, "Bar", **{"currency:XBT": "yes", "payment:lightning": "no"})))[0]))
        self.assertFalse(osm.refuses(read(answer(node(5, "Only a name")))[0]))

    def test_the_report_lists_the_shop_as_marked_no(self):
        shop("Bar Aurora", osm_ref="node/1", lightning_worked=day(days=3))
        report = osm.synchronize(config(), read(answer(node(1, "Bar Aurora", **{"payment:lightning": "no", "payment:onchain": "no"}))))
        self.assertEqual([item["name"] for item in report["osm_says_no"]], ["Bar Aurora"])


class NotToldYet(Guard, TestCase):
    """What a reading did once is told by the first summary that goes out afterwards."""

    def run_sync(self, body, delivered, *args):
        out = io.StringIO()
        sent = ("TELEGRAM_MODERATION_CHAT", []) if delivered else (None, ["down"])
        with patch("shops.osm.community", return_value={"osm": SETTINGS}), patch("shops.osm_net.read_area", return_value=body), \
                patch("shops.management.commands.osm_sync.deliver_to_moderators", return_value=sent) as delivery:
            try:
                call_command("osm_sync", "--send", *args, stdout=out, stderr=io.StringIO())
                code = 0
            except CommandError:
                code = 1
            except SystemExit as stop:
                code = stop.code
        return code, out.getvalue(), delivery

    def test_a_link_proposed_before_a_summary_that_did_not_go_out_is_told_after(self):
        shop("Bar Aurora")
        body = fake_overpass(node(1, "Bar Aurora", lat=41.9002, **{"currency:XBT": "yes"}))
        code, _text, delivery = self.run_sync(body, delivered=False)
        self.assertEqual(code, 1)
        self.assertEqual(Shop.objects.get(name="Bar Aurora").osm_proposed, "node/1")
        code, _text, delivery = self.run_sync(body, delivered=True)
        self.assertEqual(code, 10)
        message = delivery.call_args.args[1]
        self.assertIn("Bar Aurora (node/1)", message)
        self.assertIn("waiting for you to confirm", message)
        # And once told, it is not told again
        code, text, delivery = self.run_sync(body, delivered=True)
        self.assertEqual((code, delivery.call_count), (0, 0))
        self.assertIn("no message", text)

    def test_what_a_reading_by_hand_did_is_told_by_the_next_summary(self):
        known = shop("Bar Aurora", osm_ref="node/1", address="")
        body = fake_overpass(node(1, "Bar Aurora", **{"currency:XBT": "yes", "addr:street": "Main Street", "website": "https://bar.example/"}))
        out = io.StringIO()
        with patch("shops.osm.community", return_value={"osm": SETTINGS}), patch("shops.osm_net.read_area", return_value=body):
            call_command("osm_sync", stdout=out, stderr=io.StringIO())
        self.assertEqual(Shop.objects.get(pk=known.pk).address, "Main Street")
        self.assertTrue(OsmRun.objects.get().report["silent"])
        code, _text, delivery = self.run_sync(body, delivered=True)
        self.assertEqual(code, 10)
        message = delivery.call_args.args[1]
        self.assertIn("got empty details", message)
        self.assertIn("Bar Aurora: address", message)
        self.assertIn("offered by OpenStreetMap", message)

    def test_carrying_does_not_repeat_and_does_not_reach_back_past_a_summary(self):
        first = {"shop": "1", "name": "A", "ref": "node/1", "values": {"address": "x"}}
        osm.save_run({"created": (timezone.now() - timedelta(days=60)).isoformat(), "filled": [{"shop": "0", "name": "Old", "ref": "node/9", "values": {"city": "c"}}]})
        told = osm.save_run({"created": (timezone.now() - timedelta(days=30)).isoformat(), "filled": []})
        osm.save_run({"created": (timezone.now() - timedelta(days=20)).isoformat(), "silent": True, "filled": [first]})
        merged = osm.carry({"filled": [first, {"shop": "2", "name": "B", "ref": "node/2", "values": {"city": "y"}}], "offers": []}, told)
        self.assertEqual([item["name"] for item in merged["filled"]], ["A", "B"])


class WhoDecidedWhat(TestCase):
    def setUp(self):
        self.staff = Client()
        admin_session(self.staff)
        self.mine = shop("Bar Aurora")
        self.element = read(answer(node(77, "Bar Aurora Cafe", lat=41.9004, **{"currency:XBT": "yes", "website": "https://bar.example/"})))[0]

    def propose(self):
        osm.synchronize(config(), [self.element])
        self.mine.refresh_from_db()

    def test_the_list_shows_the_element_proposed_how_far_and_how_alike(self):
        self.propose()
        self.assertEqual(osm.notes_of(self.mine)["proposal"]["ref"], "node/77")
        page = self.staff.get(reverse("admin:shops_shop_changelist"))
        self.assertContains(page, "Bar Aurora Cafe (node/77)")
        self.assertRegex(page.content.decode(), r"Bar Aurora Cafe \(node/77\) · \d+ m · 0\.\d\d")
        # Whatever the strangers wrote is escaped
        hostile = read(answer(node(78, "<script>alert(1)</script>", lat=41.9004, **{"currency:XBT": "yes"})))[0]
        other = shop("Gelato <b>Luna</b>")
        osm.propose_link(other, hostile, 0.9, 12)
        text = self.staff.get(reverse("admin:shops_shop_changelist")).content.decode()
        self.assertNotIn("<script>alert(1)</script>", text)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", text)

    def test_who_confirmed_and_when(self):
        self.propose()
        before = timezone.now()
        self.staff.post(reverse("admin:shops_shop_changelist"), {"action": "confirm_osm_link", "_selected_action": [self.mine.pk]})
        self.mine.refresh_from_db()
        self.assertEqual(self.mine.osm_ref, "node/77")
        decision = osm.notes_of(self.mine)["decisions"][-1]
        self.assertEqual((decision["what"], decision["by"], decision["ref"], decision["name"]), ("confirmed link", "boss", "node/77", "Bar Aurora Cafe"))
        self.assertGreaterEqual(decision["at"], before.isoformat())
        self.assertEqual(osm.notes_of(self.mine)["proposal"], {})
        page = self.staff.get(reverse("admin:shops_shop_change", args=[self.mine.pk]))
        self.assertContains(page, "boss · confirmed link")

    def test_who_accepted_the_contacts_and_who_refused_a_link(self):
        Shop.objects.filter(pk=self.mine.pk).update(osm_ref="node/77")
        self.mine.refresh_from_db()
        osm.fill(self.mine, self.element, config())
        self.mine.refresh_from_db()
        self.staff.post(reverse("admin:shops_shop_changelist"), {"action": "accept_osm_contacts", "_selected_action": [self.mine.pk]})
        self.mine.refresh_from_db()
        decision = osm.notes_of(self.mine)["decisions"][-1]
        self.assertEqual((decision["what"], decision["by"], decision["fields"]), ("accepted contacts", "boss", {"website": "https://bar.example/"}))
        other = shop("Other Shop")
        osm.propose_link(other, self.element, 0.9, 10)
        self.staff.post(reverse("admin:shops_shop_changelist"), {"action": "reject_osm_link", "_selected_action": [other.pk]})
        other.refresh_from_db()
        decision = osm.notes_of(other)["decisions"][-1]
        self.assertEqual((decision["what"], decision["by"], decision["ref"]), ("refused link", "boss", "node/77"))
        self.assertEqual(osm.notes_of(other)["rejected"], ["node/77"])

    def test_the_trail_is_kept_short(self):
        notes = osm.notes_of(self.mine)
        for number in range(osm.DECISIONS_KEPT + 20):
            osm.decided(notes, "declined contacts", "boss", fields={"phone": str(number)})
        self.assertEqual(len(notes["decisions"]), osm.DECISIONS_KEPT)
        self.assertEqual(notes["decisions"][-1]["fields"], {"phone": str(osm.DECISIONS_KEPT + 19)})


class ChangesetWords(TestCase):
    def test_the_source_is_the_site_and_the_comment_says_where_the_data_come_from(self):
        xml = osm_edit.changeset_xml(config(), "site.example")
        import xml.etree.ElementTree as ET
        tags = {tag.get("k"): tag.get("v") for tag in ET.fromstring(xml).findall("changeset/tag")}
        self.assertEqual(tags["source"], "site.example")
        self.assertNotIn("survey", tags["source"] + tags["comment"])
        self.assertEqual(tags["comment"], f"Update bitcoin payment tags. Data from the Lightning addresses checked by {settings.SITE_HOST}.")
        self.assertEqual(tags["created_by"], "bitmentor")
        self.assertLessEqual(len(tags["comment"]), 255)

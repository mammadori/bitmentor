"""The meetup board: settings, the administration, the board and the calendar feed. Nostr is in test_nostr.py."""
import types
from datetime import date, datetime, timedelta
from datetime import timezone as dt_timezone
from unittest import skipUnless
from unittest.mock import patch
from zoneinfo import ZoneInfo

from config.test_translations import python_ids, untranslated
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import checks
from django.core.cache import cache
from django.db import connection
from django.template import engines
from django.test import RequestFactory, override_settings
from django.test import TestCase as DjangoTestCase
from django.utils import timezone, translation

from shops.tests import admin_session, reset_limits

from . import board, conf, ical
from .keys import to_hex, to_npub
from .models import Meetup, TrustedAuthor
from .nostr_read import plain_text



class TestCase(DjangoTestCase):
    """The board and the feed are kept on the server for a few minutes (board.py), and the database is rolled back after each
    test, so each test starts with nothing kept."""

    def _pre_setup(self):
        super()._pre_setup()
        cache.clear()


KEY = "11" * 32
NPUB = to_npub(KEY)
ROME = ZoneInfo("Europe/Rome")


def block(**fields):
    """The `meetups` block as given, on top of the example community."""
    from config.community import community

    return patch("meetups.conf.community", return_value={**community(), "meetups": fields})


def on(**fields):
    return block(link="https://t.me/example", **fields)


def make(days=0, hours=19, **fields):
    """A meetup `days` from today (negative: past) at `hours` o'clock in Rome."""
    local = (timezone.localtime(timezone.now(), ROME) + timedelta(days=days)).replace(hour=hours, minute=0, second=0, microsecond=0)
    values = {"starts_at": local.astimezone(dt_timezone.utc), "tzid": "Europe/Rome", "city": "Example", "place": "The Bar",
              "title": "Spritz"}
    values.update(fields)
    return Meetup.objects.create(**values)


@skipUnless(connection.vendor == "postgresql", "the foreign keys follow the models in the database on PostgreSQL only")
class DeletedByThePreviousImage(TestCase):
    """Going back (docs/releases.md): the previous image deletes an account the way it knows, and knows none of these
    tables. The database must set what points to the account to NULL, as the models do in Python (shops/going_back.py)."""

    def test_the_foreign_keys_to_the_accounts_set_null_in_the_database(self):
        for table, column in (("meetups_meetup", "entered_by_id"), ("meetups_trustedauthor", "added_by_id")):
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT c.confdeltype FROM pg_constraint c JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey) "
                    "WHERE c.contype = 'f' AND c.conrelid = %s::regclass AND a.attname = %s", [table, column])
                self.assertEqual([row[0] for row in cursor.fetchall()], ["n"], f"{table}.{column}")

    def test_an_account_the_old_image_deletes_stays_in_the_new_tables_as_nobody(self):
        author = get_user_model().objects.create(username="staff-member")
        make(3, entered_by=author)
        TrustedAuthor.objects.create(pubkey=KEY, note="Certified", added_by=author)
        with connection.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
            cursor.execute("DELETE FROM auth_user WHERE id = %s", [author.pk])
        self.assertIsNone(Meetup.objects.get().entered_by_id)
        self.assertIsNone(TrustedAuthor.objects.get().added_by_id)


class Keys(TestCase):
    def test_an_npub_and_its_hexadecimal_are_the_same_key(self):
        self.assertEqual(to_hex(NPUB), KEY)
        self.assertEqual(to_hex(NPUB.upper()), KEY)
        self.assertEqual(to_hex(KEY.upper()), KEY)
        self.assertEqual(to_npub(to_hex(NPUB)), NPUB)

    def test_a_wrong_key_is_refused(self):
        for text in (NPUB[:-1] + ("q" if NPUB[-1] != "q" else "p"), NPUB[:-3], "npub1", KEY[:-1], KEY + "0", "zz" * 32, "",
                     "nsec1" + NPUB[5:], NPUB.replace("npub", "nsec", 1), None, 5):
            with self.subTest(text=text), self.assertRaises(ValueError):
                to_hex(text)


class PlainText(TestCase):
    def test_markup_and_entities_become_plain_text(self):
        self.assertEqual(plain_text("Caff&egrave; <b>da</b> <script>alert(1)</script> Mario &amp; Co", 100), "Caffè da alert(1) Mario & Co")

    def test_control_and_direction_characters_go(self):
        self.assertEqual(plain_text("a\x00b‮evil‬​ c\r\nd\te", 100), "abevil c d e")

    def test_it_is_cut_and_a_non_text_is_nothing(self):
        self.assertEqual(plain_text("x" * 500, 200), "x" * 200)
        self.assertEqual(plain_text(None, 10), "")
        self.assertEqual(plain_text(["a"], 10), "")


class Settings(TestCase):
    def test_off_without_the_block_or_with_an_empty_one(self):
        self.assertFalse(conf.enabled())
        with block():
            self.assertFalse(conf.enabled())
        with block(past_months=6):
            self.assertTrue(conf.enabled())

    def test_a_link_is_https_without_tricks(self):
        for good in ("https://t.me/example", "https://example.org/a?b=c#d", "https://example.org:8443/x"):
            with self.subTest(good=good):
                self.assertEqual(conf.safe_link(good), good)
        for bad in ("http://example.org", "javascript:alert(1)", "//example.org", "https://", "https://a b.org", "https://u:p@example.org/",
                    "https://example.org/è", "https://example.org/\"onmouseover=1", "https://example.org/\r\nX: 1", "ftp://x.org/",
                    "https://example.org/" + "a" * 600, "https://example.org:port/", "", None, 7, "https://x.org/<b>"):
            with self.subTest(bad=bad):
                self.assertEqual(conf.safe_link(bad), "")

    def test_past_months_has_a_default_and_limits(self):
        with block(past_months=6):
            self.assertEqual(conf.past_months(), 6)
        for bad in (0, 99, "12", True, 1.5):
            warnings = []
            with block(past_months=bad):
                self.assertEqual(conf.past_months(warnings.append), 12)
            self.assertEqual(len(warnings), 1, bad)

    def test_the_default_link_may_be_one_per_language(self):
        with block(link={"en": "https://t.me/en", "it": "https://t.me/it"}):
            self.assertEqual(conf.default_link("it"), "https://t.me/it")
            self.assertEqual(conf.default_link("de"), "https://t.me/en")
        warnings = []
        with block(link="http://insecure.example.org"):
            self.assertEqual(conf.default_link("en", warnings.append), "")
        self.assertEqual(len(warnings), 1)

    def test_relays_are_wss_with_a_name(self):
        warnings = []
        with block(relays=["wss://relay.example.org", "ws://relay.example.org", "wss://1.2.3.4", "wss://[::1]/", "https://x.org",
                           "wss://relay.example.org", 7, "wss://ok.example.org:7777/path", "wss://a b.org"]):
            self.assertEqual(conf.relays(warnings.append), ["wss://relay.example.org", "wss://ok.example.org:7777/path"])
        self.assertEqual(len(warnings), 6)

    def test_without_relays_the_sites_own_are_read_and_may_be_plain(self):
        with override_settings(NOSTR_RELAYS=["ws://relay:7777"]):
            with block(past_months=6):
                self.assertEqual(conf.relays(), ["ws://relay:7777"])
            with block(relays=["ws://relay:7777", "ws://elsewhere:7777"]):
                self.assertEqual(conf.relays(), ["ws://relay:7777"])

    def test_trusted_authors_need_a_note_and_a_real_key(self):
        warnings = []
        with block(trusted=[{"npub": NPUB, "note": "  Certified by the staff, 2026-11-05  "}, {"npub": NPUB, "note": "again"},
                            {"npub": KEY.replace("1", "2"), "note": "hex works"}, {"npub": NPUB},
                            {"npub": "npub1wrong", "note": "bad"}, "text", {"note": "no key"}]):
            found = conf.trusted(warnings.append)
        self.assertEqual(found, [{"pubkey": KEY, "note": "Certified by the staff, 2026-11-05"},
                                 {"pubkey": "22" * 32, "note": "hex works"}])
        self.assertEqual(len(warnings), 4)

    def test_cities_have_a_position(self):
        warnings = []
        with block(cities=[{"name": "Example", "latitude": 41.9, "longitude": 12.5}, {"name": "Far", "latitude": 95, "longitude": 0},
                           {"name": "Wide", "latitude": 41.0, "longitude": 12.5, "radius_km": 5000}, {"latitude": 1, "longitude": 1},
                           {"name": "Ok", "latitude": 45, "longitude": 11, "radius_km": 4}, {"name": "Str", "latitude": "45", "longitude": 1}]):
            found = conf.cities(warnings.append)
        self.assertEqual([c["name"] for c in found], ["Example", "Ok"])
        self.assertEqual((found[0]["radius_km"], found[1]["radius_km"]), (15.0, 4.0))
        self.assertEqual(len(warnings), 4)

    def test_the_check_reports_what_is_left_out(self):
        with on(past_months=0, relays=["ws://x.org"]):
            messages = [m.msg for m in checks.run_checks() if m.id == "meetups.W001"]
        self.assertEqual(len(messages), 2)
        self.assertEqual([m for m in checks.run_checks() if m.id == "meetups.W001"], [])

    def test_the_example_community_checks_clean(self):
        self.assertEqual([m for m in checks.run_checks() if m.id.startswith("meetups")], [])


class Admin(TestCase):
    def setUp(self):
        admin_session(self.client)

    def post_new(self, **fields):
        data = {"day": "2026-12-04", "hour": "19:00", "tzid": "Europe/Rome", "city": "Example", "place": "The Bar", "title": "Spritz",
                "url": "", "status": "confirmed", "moved_to": "", "visible": "on"}
        data.update(fields)
        data = {key: value for key, value in data.items() if value is not None}
        return self.client.post("/admin/meetups/meetup/add/", data)

    def test_a_meetup_is_written_in_its_own_time_zone(self):
        self.assertEqual(self.post_new().status_code, 302)
        meetup = Meetup.objects.get()
        self.assertEqual(meetup.starts_at, datetime(2026, 12, 4, 18, 0, tzinfo=dt_timezone.utc))  # winter: Rome is UTC+1
        self.assertEqual((meetup.source, meetup.entered_by.username), ("staff", "boss"))
        self.post_new(day="2026-07-10", tzid="America/New_York", title="Summer")
        summer = Meetup.objects.get(title="Summer")
        self.assertEqual(summer.starts_at, datetime(2026, 7, 10, 23, 0, tzinfo=dt_timezone.utc))
        edit = self.client.get(f"/admin/meetups/meetup/{summer.pk}/change/").content.decode()
        self.assertIn('value="19:00:00"', edit)  # shown again as the hour on the clock of New York

    def test_an_hour_that_does_not_exist_becomes_the_one_that_does(self):
        self.post_new(day="2027-03-28", hour="02:30")  # the clocks go forward at 02:00
        self.assertEqual(Meetup.objects.get().local_start.hour, 3)

    def test_only_the_day_is_enough_when_the_box_says_so(self):
        self.post_new(all_day="on", hour="")
        self.assertTrue(Meetup.objects.get().all_day)
        self.assertEqual(self.post_new(hour="", title="no").status_code, 200)
        self.assertFalse(Meetup.objects.filter(title="no").exists())

    def test_the_city_a_zone_and_a_link_are_checked(self):
        self.assertContains(self.post_new(city=""), "Write the city.")
        self.assertContains(self.post_new(tzid="Mars/Olympus"), "not a time zone")
        self.assertContains(self.post_new(url="javascript:alert(1)"), "starts with https://")
        self.assertContains(self.post_new(url="http://example.org"), "starts with https://")
        self.assertContains(self.post_new(day="31/12"), "valid")
        self.assertFalse(Meetup.objects.exists())

    def test_postponed_keeps_its_new_day_and_other_states_drop_it(self):
        self.post_new(status="postponed", moved_to="2027-01-08", title="a")
        self.post_new(status="confirmed", moved_to="2027-01-08", title="b")
        self.assertEqual(Meetup.objects.get(title="a").moved_to, date(2027, 1, 8))
        self.assertIsNone(Meetup.objects.get(title="b").moved_to)

    def test_correcting_what_came_from_nostr_protects_it_from_the_reader(self):
        nostr = make(10, source="nostr", nostr_address="31923:" + KEY + ":x", nostr_author=KEY, nostr_event_id="a" * 64, nostr_created_at=5)
        self.assertFalse(nostr.staff_edited)
        page = self.client.get(f"/admin/meetups/meetup/{nostr.pk}/change/")
        self.assertContains(page, "Read from Nostr")
        self.assertContains(page, 'name="staff_edited"')
        local = nostr.local_start
        data = {"day": local.date().isoformat(), "hour": local.strftime("%H:%M:%S"), "tzid": "Europe/Rome", "city": "Example",
                "place": "The Bar", "title": "Spritz", "url": "", "status": "cancelled", "visible": "on"}
        self.client.post(f"/admin/meetups/meetup/{nostr.pk}/change/", data)
        nostr.refresh_from_db()
        self.assertEqual((nostr.status, nostr.staff_edited), ("cancelled", True))
        # Another edit with the box left ticked: still the staff's. With the box unticked: handed back to the reader
        self.client.post(f"/admin/meetups/meetup/{nostr.pk}/change/", {**data, "staff_edited": "on", "title": "Again"})
        nostr.refresh_from_db()
        self.assertEqual((nostr.title, nostr.staff_edited), ("Again", True))
        self.client.post(f"/admin/meetups/meetup/{nostr.pk}/change/", {**data, "title": "Handed back"})
        nostr.refresh_from_db()
        self.assertEqual((nostr.title, nostr.staff_edited), ("Handed back", False))
        # And a later edit that does not touch the box is a correction again
        self.client.post(f"/admin/meetups/meetup/{nostr.pk}/change/", {**data, "title": "Corrected again"})
        nostr.refresh_from_db()
        self.assertTrue(nostr.staff_edited)
        self.assertNotContains(self.client.get(f"/admin/meetups/meetup/{make(3).pk}/change/"), 'name="staff_edited"')

    def test_what_the_staff_wrote_does_not_get_the_protection_box(self):
        meetup = make(3)
        self.client.post(f"/admin/meetups/meetup/{meetup.pk}/change/", {
            "day": meetup.local_start.date().isoformat(), "hour": "20:30", "tzid": "Europe/Rome", "city": "Example", "place": "", "title": "",
            "url": "", "status": "confirmed", "visible": "on"})
        meetup.refresh_from_db()
        self.assertEqual((meetup.local_start.hour, meetup.local_start.minute, meetup.staff_edited), (20, 30, False))

    def test_a_meetup_from_nostr_is_not_deleted_but_taken_off(self):
        nostr = make(10, source="nostr", nostr_address="31923:" + KEY + ":x", nostr_author=KEY)
        own = make(11)
        self.assertEqual(self.client.get(f"/admin/meetups/meetup/{nostr.pk}/delete/").status_code, 403)
        self.client.post("/admin/meetups/meetup/", {"action": "delete_selected", "_selected_action": [nostr.pk], "post": "yes"})
        self.assertTrue(Meetup.objects.filter(pk=nostr.pk).exists())
        self.client.post(f"/admin/meetups/meetup/{own.pk}/delete/", {"post": "yes"})
        self.assertFalse(Meetup.objects.filter(pk=own.pk).exists())
        self.client.post("/admin/meetups/meetup/", {"action": "hide", "_selected_action": [nostr.pk]})
        nostr.refresh_from_db()
        self.assertEqual((nostr.visible, nostr.staff_edited), (False, True))

    def test_cancelling_from_the_list(self):
        one, two = make(3), make(4, source="nostr", nostr_address="31923:" + KEY + ":y", nostr_author=KEY)
        self.client.post("/admin/meetups/meetup/", {"action": "mark_cancelled", "_selected_action": [one.pk, two.pk]})
        one.refresh_from_db()
        two.refresh_from_db()
        self.assertEqual((one.status, one.staff_edited, two.status, two.staff_edited), ("cancelled", False, "cancelled", True))

    def test_the_form_does_not_say_the_hour_is_the_servers(self):
        # The built image names static files with their hash (ManifestStaticFilesStorage): meetups-admin.<hash>.css
        self.assertRegex(self.client.get("/admin/meetups/meetup/add/").content.decode(), r"meetups-admin(\.[0-9a-f]+)?\.css")

    def test_the_list_opens(self):
        make(3)
        make(-3, source="nostr", nostr_address="31923:" + KEY + ":z", nostr_author=KEY, staff_edited=True)
        page = self.client.get("/admin/meetups/meetup/")
        self.assertContains(page, "Example")
        self.assertContains(page, "Nostr (corrected)")

    def test_trusted_authors_are_added_with_an_npub_and_a_note(self):
        response = self.client.post("/admin/meetups/trustedauthor/add/", {"pubkey": NPUB, "note": "Certified by the staff", "active": "on"})
        self.assertEqual(response.status_code, 302)
        author = TrustedAuthor.objects.get()
        self.assertEqual((author.pubkey, author.added_by.username), (KEY, "boss"))
        self.assertContains(self.client.get("/admin/meetups/trustedauthor/"), NPUB[:20])
        self.assertContains(self.client.get(f"/admin/meetups/trustedauthor/{author.pk}/change/"), NPUB)

    def test_a_trusted_author_without_a_note_or_with_a_wrong_key_is_refused(self):
        self.assertContains(self.client.post("/admin/meetups/trustedauthor/add/", {"pubkey": NPUB, "note": ""}), "required")
        self.assertContains(self.client.post("/admin/meetups/trustedauthor/add/", {"pubkey": "npub1wrong", "note": "x"}), "not a valid npub")
        self.assertContains(self.client.post("/admin/meetups/trustedauthor/add/", {"pubkey": "abc", "note": "x"}), "64 hexadecimal")
        self.assertFalse(TrustedAuthor.objects.exists())

    def test_the_trusted_authors_of_the_block_are_mentioned(self):
        with block(trusted=[{"npub": NPUB, "note": "n"}]):
            self.assertContains(self.client.get("/admin/meetups/trustedauthor/"), "1 more authors are written in community.yml")

    def test_readings_are_only_read(self):
        self.assertEqual(self.client.get("/admin/meetups/reading/").status_code, 200)
        self.assertEqual(self.client.get("/admin/meetups/reading/add/").status_code, 403)


class Board(TestCase):
    def test_next_and_last_ones(self):
        make(2, title="Soon")
        make(9, title="Later")
        make(-2, title="Yesterday")
        make(-20, title="Last month")
        make(-400, title="Too old")
        found = board.board()
        self.assertEqual([r["title"] for r in found["upcoming"]], ["Soon", "Later"])
        self.assertEqual([r["title"] for r in found["past"]], ["Yesterday", "Last month"])

    def test_the_window_follows_the_block(self):
        make(-20, title="Last month")
        with block(past_months=1, link="https://t.me/x"):
            self.assertEqual(board.board()["past"][0]["title"], "Last month")
        make(-50, title="Older")
        with block(past_months=1):
            self.assertEqual([r["title"] for r in board.board()["past"]], ["Last month"])
        with block(past_months=3):
            self.assertEqual([r["title"] for r in board.board()["past"]], ["Last month", "Older"])

    def test_one_that_began_an_hour_ago_is_still_next(self):
        now = timezone.now()
        Meetup.objects.create(starts_at=now - timedelta(hours=1), city="Example", title="On now")
        Meetup.objects.create(starts_at=now - timedelta(hours=5), city="Example", title="Over")
        found = board.board()
        self.assertEqual([r["title"] for r in found["upcoming"]], ["On now"])
        self.assertEqual([r["title"] for r in found["past"]], ["Over"])

    def test_a_day_without_an_hour_is_next_all_day(self):
        local = timezone.localtime(timezone.now(), ROME).replace(hour=0, minute=0, second=0, microsecond=0)
        Meetup.objects.create(starts_at=local, all_day=True, tzid="Europe/Rome", city="Example", title="Today")
        Meetup.objects.create(starts_at=local - timedelta(days=1), all_day=True, tzid="Europe/Rome", city="Example", title="Yesterday")
        found = board.board()
        self.assertEqual(([r["title"] for r in found["upcoming"]], [r["title"] for r in found["past"]]), (["Today"], ["Yesterday"]))
        self.assertEqual(found["upcoming"][0]["time"], "")

    def test_cancelled_and_postponed_stay_and_are_said(self):
        make(3, title="Off", status="cancelled")
        make(4, title="Later", status="postponed", moved_to=date(2027, 2, 12))
        make(5, title="Soon", status="postponed")
        make(6, title="Casual", status="informal")
        make(7, title="Normal")
        rows = {r["title"]: r for r in board.board()["upcoming"]}
        self.assertEqual(rows["Off"]["status_text"], "Cancelled")
        self.assertEqual(rows["Later"]["status_text"], "Postponed to 12 February 2027")
        self.assertEqual(rows["Soon"]["status_text"], "Postponed")
        self.assertEqual(rows["Casual"]["status_text"], "Informal get-together")
        self.assertEqual(rows["Normal"]["status_text"], "")
        self.assertEqual([r["off"] for r in rows.values()], [True, True, True, False, False])

    def test_what_is_taken_off_is_not_shown(self):
        make(3, title="Hidden", visible=False)
        self.assertEqual(board.board()["upcoming"], [])

    def test_the_link_is_the_meetups_or_the_groups(self):
        make(3, title="Own", url="https://example.org/event")
        make(4, title="Group")
        make(5, title="Broken", url="javascript:alert(1)")
        with block(link="https://t.me/group"):
            rows = {r["title"]: r["url"] for r in board.board()["upcoming"]}
        self.assertEqual(rows, {"Own": "https://example.org/event", "Group": "https://t.me/group", "Broken": "https://t.me/group"})
        with block(past_months=3):
            self.assertEqual({r["url"] for r in board.board()["upcoming"]} - {"https://example.org/event"}, {""})

    def test_the_hour_is_the_one_of_the_meetup_and_the_zone_is_said_when_it_differs(self):
        translation.activate("it")
        make(3, title="Here", hours=19)
        far = make(4, title="There", hours=19, tzid="America/New_York")
        rows = {r["title"]: r for r in board.board()["upcoming"]}
        self.assertEqual((rows["Here"]["time"], rows["Here"]["zone"]), ("19:00", ""))
        self.assertEqual(rows["There"]["zone"], far.local_start.tzname())
        self.assertIn(rows["There"]["time"], ("19:00", "13:00"))  # the hour written is of New York, not of Rome
        self.assertEqual(rows["There"]["start"].tzinfo.key, "America/New_York")

    def test_the_state_and_the_date_are_said_in_the_language(self):
        make(3, status="cancelled")
        make(4, status="postponed", moved_to=date(2027, 2, 12))
        make(5, status="informal")
        for language, words in (("it", ["Annullato", "Rinviato al 12 febbraio 2027", "Ritrovo informale"]),
                                ("de", ["Abgesagt", "Verschoben auf den 12. Februar 2027", "Informelles Treffen"])):
            translation.activate(language)
            rows = board.board()["upcoming"]
            self.assertEqual([r["status_text"] for r in rows], words, language)
            self.assertIn(Meetup.Status.CANCELLED.label, words[:1])
        translation.activate("en")

    def test_the_language_changes_the_day_names(self):
        make(3)
        translation.activate("it")
        self.assertIn(board.board()["upcoming"][0]["weekday"].lower(), ("lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"))
        translation.activate("en")
        self.assertIn(board.board()["upcoming"][0]["weekday"], ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"))

    def test_next_one_skips_what_is_off(self):
        make(2, title="Off", status="cancelled")
        make(3, title="Moved", status="postponed")
        make(4, title="On")
        self.assertEqual(board.next_one()["title"], "On")
        Meetup.objects.filter(title="On").delete()
        self.assertIsNone(board.next_one())

    def test_a_page_never_draws_more_than_the_ceiling(self):
        for number in range(1, 8):
            make(number)
            make(-number)
        with patch("meetups.board.PAGE_LIMIT", 3):
            found = board.board()
        self.assertEqual((len(found["upcoming"]), len(found["past"]), found["more_upcoming"], found["more_past"]), (3, 3, 4, 4))

    def test_the_limits_cut_and_say_how_many_are_left(self):
        for number in range(1, 5):
            make(number)
            make(-number)
        found = board.board(upcoming=2, past=1)
        self.assertEqual((len(found["upcoming"]), len(found["past"]), found["more_upcoming"], found["more_past"]), (2, 1, 2, 3))
        self.assertEqual(len(board.board(upcoming=0, past=0)["upcoming"]), 0)


def nostr(days, key=KEY, created=100, **fields):
    return make(days, source="nostr", nostr_address=f"31923:{key}:{days}-{fields.get('title', '')}", nostr_author=key,
                nostr_created_at=created, **fields)


class Copies(TestCase):
    """One evening published twice by two aggregators shows once."""

    def titles(self):
        return [r["title"] for r in board.board()["upcoming"]]

    def test_the_newest_of_two_authors_wins(self):
        nostr(5, "aa" * 32, 100, title="Old copy", hours=17)
        nostr(5, "bb" * 32, 200, title="New copy", hours=19)
        self.assertEqual(self.titles(), ["New copy"])

    def test_the_staffs_own_wins_over_any_copy(self):
        nostr(5, "aa" * 32, 900, title="Copy", hours=18)
        make(5, title="Own", hours=19)
        self.assertEqual(self.titles(), ["Own"])

    def test_what_the_staff_corrected_wins_and_the_other_authors_copy_goes(self):
        nostr(5, "aa" * 32, 100, title="Mine", hours=18, staff_edited=True, status="cancelled")
        nostr(5, "bb" * 32, 900, title="Copy", hours=19)
        self.assertEqual(self.titles(), ["Mine"])

    def test_the_same_author_or_the_staff_twice_are_two_meetups(self):
        nostr(5, "aa" * 32, 100, title="One", hours=17)
        nostr(5, "aa" * 32, 100, title="Two", hours=19)
        make(8, title="Three", hours=10)
        make(8, title="Four", hours=11)
        self.assertEqual(self.titles(), ["One", "Two", "Three", "Four"])

    def test_another_city_or_another_evening_is_not_a_copy(self):
        nostr(5, "aa" * 32, 100, title="Here", hours=19)
        nostr(5, "bb" * 32, 100, title="There", hours=19, city="Elsewhere")
        nostr(5, "bb" * 32, 100, title="Later", hours=23)
        self.assertEqual(sorted(self.titles()), ["Here", "Later", "There"])

    def test_without_a_city_nothing_is_taken_for_a_copy(self):
        # A community with no `cities` knows no city: the same hour of two meetups says nothing about where they are
        nostr(5, "aa" * 32, 100, title="One", city="", hours=19)
        nostr(5, "bb" * 32, 200, title="Two", city="", hours=19)
        nostr(5, "bb" * 32, 200, title="Three", city=" ", hours=18)
        self.assertEqual(sorted(self.titles()), ["One", "Three", "Two"])

    def test_the_cities_are_compared_without_case(self):
        nostr(5, "aa" * 32, 100, title="A", city="Example")
        nostr(5, "bb" * 32, 101, title="B", city="EXAMPLE")
        self.assertEqual(self.titles(), ["B"])


class Pages(TestCase):
    def setUp(self):
        reset_limits()
        cache.clear()

    def test_there_is_no_page_without_the_block(self):
        self.assertEqual(self.client.get("/en/meetups/").status_code, 404)
        self.assertEqual(self.client.get("/meetups.ics").status_code, 404)

    def test_the_page_lists_the_meetups_without_javascript_and_without_people(self):
        user = get_user_model().objects.create_user("simone")
        make(3, title="Wallet security", place="The Bar", entered_by=user)
        make(4, title="Gone", status="cancelled", url="https://example.org/e")
        make(-5, title="Last time")
        with on():
            page = self.client.get("/en/meetups/")
        self.assertEqual(page.status_code, 200)
        html = page.content.decode()
        for text in ("Next meetups", "Latest meetups", "Wallet security", "The Bar", "Example", "Cancelled", "Last time",
                     "https://t.me/example", "https://example.org/e", "/meetups.ics", "Add the meetups to your calendar"):
            self.assertIn(text, html)
        self.assertNotIn("simone", html)
        self.assertEqual(html.count("<h1"), 1)
        self.assertNotIn("<script", html.split("<main", 1)[1].split("</main>")[0])
        self.assertIn('class="meetup meetup-off"', html)

    def test_an_empty_board_says_so(self):
        with on():
            self.assertContains(self.client.get("/en/meetups/"), "No meetup is announced yet.")

    def test_the_page_is_in_every_language(self):
        make(3)
        with on():
            self.assertContains(self.client.get("/it/meetups/"), "Prossimi incontri")

    def test_the_static_copy_gets_no_dates(self):
        make(3, title="Wallet security")
        with on():
            page = self.client.get("/en/meetups/", headers={"X-Static-Copy": "1"})
        self.assertNotContains(page, "Wallet security")
        self.assertContains(page, "on the live site")

    def test_a_community_page_of_the_same_name_is_served_without_the_block_too(self):
        text = "---\ntitle: Our meetups\n---\n# Our meetups\n\nHello from the community page.\n"
        page = types.SimpleNamespace(exists=lambda: True, read_text=lambda: text)
        with patch("website.views.page_path", return_value=page), patch("meetups.views.page_path", return_value=page):
            response = self.client.get("/en/meetups/")
        self.assertContains(response, "Hello from the community page.")
        self.assertEqual(self.client.get("/en/meetups/").status_code, 404)

    def test_a_community_page_of_the_same_name_takes_the_address(self):
        from config.community import page_path

        make(3, title="Wallet security")
        text = "---\ntitle: Our meetups\n---\n# Our meetups\n\n{% meetup_board level=2 %}\n"
        with on(), patch("website.views.page_path", return_value=types.SimpleNamespace(exists=lambda: True, read_text=lambda: text)), \
                patch("meetups.views.page_path", return_value=types.SimpleNamespace(exists=lambda: True)):
            page = self.client.get("/en/meetups/")
        self.assertContains(page, "Our meetups")
        self.assertContains(page, "Wallet security")
        self.assertNotContains(page, "Add the meetups to your calendar")
        self.assertTrue(callable(page_path))

    def test_the_sitemap_lists_the_board_only_when_it_is_on(self):
        self.assertNotIn("/meetups/", self.client.get("/sitemap.xml").content.decode())
        with on():
            sitemap = self.client.get("/sitemap.xml").content.decode()
        self.assertIn(f"<loc>{settings.PUBLIC_URL}/en/meetups/</loc>", sitemap)
        self.assertIn(f'hreflang="it" href="{settings.PUBLIC_URL}/it/meetups/"', sitemap)

    def test_the_sitemap_does_not_list_it_twice_when_a_page_has_the_address(self):
        taken = types.SimpleNamespace(exists=lambda: True)
        with on(), patch("meetups.views.page_path", return_value=taken), patch("website.views.public_slugs", return_value=["meetups"]):
            sitemap = self.client.get("/sitemap.xml").content.decode()
        self.assertEqual(sitemap.count(f"<loc>{settings.PUBLIC_URL}/en/meetups/</loc>"), 1)


class Tags(TestCase):
    def render(self, template, static_copy=False):
        request = RequestFactory().get("/en/", headers={"X-Static-Copy": "1"} if static_copy else {})
        return engines["django"].from_string(template).render({}, request)

    def test_the_board_tag_draws_the_lists(self):
        make(2, title="First")
        make(5, title="Second")
        make(-3, title="Before")
        with on():
            html = self.render("{% meetup_board %}")
            self.assertIn("<h2>Next meetups</h2>", html)
            self.assertIn("<h2>Latest meetups</h2>", html)
            small = self.render("{% meetup_board upcoming=1 past=0 level=3 %}")
        self.assertIn("<h3>Next meetups</h3>", small)
        self.assertIn("First", small)
        self.assertNotIn("Second", small)
        self.assertNotIn("Before", small)
        self.assertIn('<a href="/en/meetups/">All the meetups</a>', small)

    def test_the_headings_are_h2_to_h4_only(self):
        make(2)
        with on():
            self.assertIn("<h2>", self.render("{% meetup_board level=1 %}"))
            self.assertIn("<h2>", self.render("{% meetup_board level=7 %}"))
            self.assertIn("<h4>", self.render("{% meetup_board level=4 %}"))

    def test_nothing_without_the_block_or_in_the_static_copy(self):
        make(2, title="First")
        self.assertEqual(self.render("{% meetup_board %}{% meetup_next %}").strip(), "")
        with on():
            self.assertEqual(self.render("{% meetup_board %}{% meetup_next %}", static_copy=True).strip(), "")

    def test_the_one_line(self):
        make(2, title="Off", status="cancelled")
        make(4, title="On", place="The Bar")
        with on():
            html = self.render("{% meetup_next %}")
        self.assertIn("Next meetup:", html)
        self.assertIn("Example, The Bar", html)
        self.assertNotIn("Off", html)
        self.assertIn('href="/en/meetups/"', html)

    def test_the_include_for_a_community_page_starts_under_the_pages_own_heading(self):
        make(2, title="First")
        make(-3, title="Before")
        with on():
            html = self.render('{% include "website/meetup_board.html" %}')
        self.assertIn("<h3>Next meetups</h3>", html)
        self.assertIn("<h3>Latest meetups</h3>", html)
        self.assertNotIn("<h2", html)
        self.assertNotIn("<h1", html)
        self.assertIn('href="/meetups.ics"', html)
        self.assertIn("check the group before you go", html)
        self.assertNotIn("All the meetups", html)

    def test_the_include_says_so_when_nothing_is_announced_and_is_empty_when_off(self):
        with on():
            self.assertIn("No meetup is announced yet.", self.render('{% include "website/meetup_board.html" %}'))
        self.assertEqual(self.render('{% include "website/meetup_board.html" %}').strip(), "")
        with on():
            self.assertEqual(self.render('{% include "website/meetup_board.html" %}', static_copy=True).strip(), "")

    def test_the_block_is_the_switch_of_a_page_with_a_fallback(self):
        from config.community import community

        make(2, title="First")
        page = '{% if community.meetups %}{% include "website/meetup_board.html" %}{% else %}FALLBACK{% endif %}'
        self.assertEqual(self.render(page), "FALLBACK")
        data = {**community(), "meetups": {"link": "https://t.me/example"}}
        with patch("config.community.community", return_value=data), patch("meetups.conf.community", return_value=data):
            html = self.render(page)
        self.assertIn("First", html)
        self.assertNotIn("FALLBACK", html)

    def test_no_line_when_nothing_is_coming(self):
        with on():
            self.assertEqual(self.render("{% meetup_next %}").strip(), "")

    def test_text_from_outside_is_escaped(self):
        make(2, title='<img src=x onerror=alert(1)>', place="<script>alert(2)</script>", city='Test"ville')
        with on():
            html = self.render("{% meetup_board %}")
        self.assertNotIn("<img", html)
        self.assertNotIn("<script>", html)
        # Every character outside letters, digits and spaces is an entity (website.views.inert_text)
        self.assertIn("&#60;img src&#61;x onerror&#61;alert&#40;1&#41;&#62;", html)

    def test_organisers_texts_are_no_markdown_in_a_community_page(self):
        """A community page is a template, then Markdown: the one line used inside a paragraph must not bring links or
        images from the organisers' texts."""
        from website.views import convert

        make(2, city="[Example](javascript:alert(1))", place="![x](https://evil.example/x.png)")
        with on():
            text = self.render("Come along: {% meetup_next %} and the board {% meetup_board %}")
        html = convert(text, "Back")
        self.assertNotIn("javascript:", html)
        self.assertNotIn("<img", html)
        self.assertIn("Example", html)
        self.assertEqual(html.count("<a "), html.count('href="/en/meetups/"') + html.count('href="https://t.me/example"'))


class Feed(TestCase):
    def setUp(self):
        reset_limits()
        cache.clear()

    def get(self):
        with on():
            return self.client.get("/meetups.ics")

    def test_the_calendar(self):
        make(3, title="Wallet security", place="The Bar", url="https://example.org/e", hours=19)
        response = self.get()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/calendar; charset=utf-8")
        self.assertEqual(response["Cache-Control"], "public, max-age=900")
        text = response.content.decode()
        self.assertTrue(text.startswith("BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"))
        self.assertTrue(text.endswith("END:VCALENDAR\r\n"))
        self.assertNotIn("\n", text.replace("\r\n", ""))
        for line in ("SUMMARY:Wallet security", "LOCATION:The Bar\\, Example", "URL:https://example.org/e", "STATUS:CONFIRMED"):
            self.assertIn(line + "\r\n", text)
        start = [line for line in text.split("\r\n") if line.startswith("DTSTART")][0]
        self.assertRegex(start, r"^DTSTART:\d{8}T\d{6}Z$")
        self.assertNotIn("ORGANIZER", text)
        self.assertNotIn("ATTENDEE", text)

    def test_the_hour_is_in_utc_so_no_zone_is_needed(self):
        meetup = make(3, hours=19)
        text = self.get().content.decode()
        expected = meetup.starts_at.strftime("DTSTART:%Y%m%dT%H%M%SZ")
        self.assertIn(expected, text)

    def test_a_day_alone_is_a_date(self):
        local = (timezone.localtime(timezone.now(), ROME) + timedelta(days=4)).replace(hour=0, minute=0, second=0, microsecond=0)
        Meetup.objects.create(starts_at=local, all_day=True, tzid="Europe/Rome", city="Example", title="Fair")
        self.assertIn(f"DTSTART;VALUE=DATE:{local:%Y%m%d}\r\n", self.get().content.decode())

    def test_cancelled_and_postponed_are_marked_so_calendars_drop_them(self):
        make(3, title="Off", status="cancelled")
        make(4, title="Moved", status="postponed", moved_to=date(2027, 2, 12))
        make(5, title="On")
        text = self.get().content.decode()
        self.assertEqual(text.count("STATUS:CANCELLED"), 2)
        self.assertIn("SUMMARY:Cancelled: Off\r\n", text)
        self.assertIn("SUMMARY:Postponed to 12 February 2027: Moved\r\n", text)

    def test_a_title_cannot_add_lines_or_fields(self):
        Meetup.objects.create(starts_at=timezone.now() + timedelta(days=2), city="Test;ville,", title="A\r\nATTENDEE:mailto:x@y.z",
                              place="B\\C", url="https://example.org/a\r\nX-EVIL:1")
        text = self.get().content.decode()
        self.assertNotIn("\r\nATTENDEE", text)
        self.assertNotIn("X-EVIL", text.replace("\r\n ", ""))
        self.assertIn("SUMMARY:A  ATTENDEE:mailto:x@y.z\r\n", text)
        self.assertIn("LOCATION:B\\\\C\\, Test\\;ville\\,\r\n", text)

    def test_long_lines_are_folded_at_75_octets(self):
        make(3, title="È" * 150)
        text = self.get().content.decode()
        for line in text.split("\r\n"):
            self.assertLessEqual(len(line.encode()), 75, line)
        self.assertEqual(ical.fold("x" * 200).count("\r\n "), 2)
        self.assertEqual("".join(ical.fold("È" * 100).split("\r\n ")), "È" * 100)

    def test_the_uid_is_stable_and_the_items_are_the_boards(self):
        meetup = make(3)
        make(5, visible=False)
        make(-500)
        first, second = self.get().content.decode(), self.get().content.decode()
        self.assertEqual(first.count("BEGIN:VEVENT"), 1)
        self.assertIn(f"UID:{meetup.id}@{settings.SITE_HOST}", first)
        self.assertEqual([line for line in first.split("\r\n") if line.startswith("UID")],
                         [line for line in second.split("\r\n") if line.startswith("UID")])

    def test_only_reading_and_a_limit(self):
        with on():
            self.assertEqual(self.client.head("/meetups.ics").status_code, 200)
            self.assertEqual(self.client.post("/meetups.ics").status_code, 405)
        with on(), patch("meetups.views.FEED_PER_HOUR", 2):
            statuses = [self.client.get("/meetups.ics").status_code for _ in range(4)]
        self.assertEqual(statuses[2:], [429, 429])

    def titles(self):
        return [line[8:] for line in self.get().content.decode().split("\r\n") if line.startswith("SUMMARY:")]

    def test_over_the_ceiling_the_feed_keeps_every_meetup_to_come_and_the_latest_past_ones(self):
        for number in range(1, 7):
            make(-number, title=f"past {number}")
        for number in range(1, 5):
            make(number, title=f"next {number}")
        with patch("meetups.board.FEED_LIMIT", 7):
            titles = self.titles()
        self.assertEqual(titles, ["past 3", "past 2", "past 1", "next 1", "next 2", "next 3", "next 4"])

    def test_if_those_to_come_are_more_than_the_ceiling_the_soonest_go_in(self):
        for number in range(1, 7):
            make(number, title=f"next {number}")
        make(-1, title="past")
        with patch("meetups.board.FEED_LIMIT", 4):
            self.assertEqual(self.titles(), ["next 1", "next 2", "next 3", "next 4"])

    def test_the_ceiling_with_more_rows_than_it_in_the_real_numbers(self):
        now = timezone.now()
        Meetup.objects.bulk_create([
            Meetup(starts_at=now - timedelta(days=300) + timedelta(days=number * 0.75), tzid="Europe/Rome", city=f"C{number}", title=f"T{number}")
            for number in range(1200)])
        board.forget()
        titles = self.titles()
        self.assertEqual(len(titles), board.FEED_LIMIT)
        self.assertIn("T1199", titles)  # the last one, in the future: never cut for the sake of an older one
        self.assertEqual(sum(1 for number in range(1200) if now - timedelta(days=300) + timedelta(days=number * 0.75) > now
                             and f"T{number}" in titles), sum(1 for number in range(1200) if number * 0.75 > 300))

    def test_an_empty_calendar_is_a_calendar(self):
        text = self.get().content.decode()
        self.assertIn("BEGIN:VCALENDAR", text)
        self.assertNotIn("BEGIN:VEVENT", text)


class Translations(TestCase):
    """Every string of the board's code is in every language of locale/ (the templates: config/test_translations.py)."""

    def test_every_language(self):
        ids = python_ids(path for path in (settings.BASE_DIR / "meetups").glob("*.py") if not path.name.startswith("test"))
        self.assertGreater(len(ids), 20)
        untranslated(self, ids)


class Kept(TestCase):
    """The board and the feed are worked out once for a few minutes, and again after anything changes the meetups."""

    def test_the_second_request_does_not_ask_the_database(self):
        make(3, title="First")
        with on():
            first = board.board()
            with self.assertNumQueries(0):
                self.assertEqual(board.board(), first)
                board.next_one()

    def test_it_is_worked_out_again_after_a_save_a_delete_or_a_change_of_the_reader(self):
        meetup = make(3, title="First")
        self.assertEqual([r["title"] for r in board.board()["upcoming"]], ["First"])
        make(4, title="Second")
        self.assertEqual([r["title"] for r in board.board()["upcoming"]], ["First", "Second"])
        meetup.delete()
        self.assertEqual([r["title"] for r in board.board()["upcoming"]], ["Second"])
        Meetup.objects.filter(title="Second").delete()
        self.assertEqual(board.board()["upcoming"], [])

    def test_each_language_has_its_own(self):
        make(3)
        translation.activate("it")
        weekday_it = board.board()["upcoming"][0]["weekday"]
        translation.activate("en")
        self.assertNotEqual(board.board()["upcoming"][0]["weekday"].lower(), weekday_it.lower())

    def test_a_change_of_the_cut_or_of_the_window_is_not_served_from_the_old_one(self):
        make(3, title="One")
        make(4, title="Two")
        self.assertEqual(len(board.board(upcoming=1)["upcoming"]), 1)
        self.assertEqual(len(board.board()["upcoming"]), 2)
        make(-100, title="Old")
        with block(past_months=1, link="https://t.me/x"):
            self.assertEqual(board.board()["past"], [])
        with block(past_months=6, link="https://t.me/x"):
            self.assertEqual([r["title"] for r in board.board()["past"]], ["Old"])

    def test_the_rows_are_made_only_for_what_is_shown(self):
        for number in range(1, 9):
            make(number)
            make(-number)
        with patch("meetups.board.row", wraps=board.row) as made:
            found = board.board(upcoming=2, past=1)
        self.assertEqual((made.call_count, len(found["upcoming"]), len(found["past"])), (3, 2, 1))
        self.assertEqual((found["more_upcoming"], found["more_past"]), (6, 7))

    def test_a_table_full_of_meetups_is_not_read_whole(self):
        now = timezone.now()
        Meetup.objects.bulk_create([Meetup(starts_at=now + timedelta(hours=number - 150, minutes=30), tzid="Europe/Rome", city="Example", title=f"T{number}")
                                    for number in range(300)])
        with patch("meetups.board.PAGE_LIMIT", 10), patch.object(Meetup, "from_db", wraps=Meetup.from_db) as read:
            found = board.board()
        self.assertEqual((len(found["upcoming"]), len(found["past"])), (10, 10))
        self.assertLessEqual(read.call_count, 2 * (2 * 10 + board.MARGIN))
        # 153 meetups are not older than three hours (T147 is 2.5 hours old) and 147 are: the rest is counted and not read
        self.assertEqual((found["more_upcoming"], found["more_past"]), (143, 137))
        self.assertEqual((found["upcoming"][0]["title"], found["past"][0]["title"]), ("T147", "T146"))

    def test_the_feed_is_kept_and_forgotten_too(self):
        make(3, title="First")
        with on(), patch("meetups.ical.build", wraps=ical.build) as built:
            first = self.client.get("/meetups.ics").content
            self.assertEqual(self.client.get("/meetups.ics").content, first)
            self.assertEqual(built.call_count, 1)
            make(4, title="Second")
            self.assertIn(b"SUMMARY:Second", self.client.get("/meetups.ics").content)
            self.assertEqual(built.call_count, 2)
        self.assertNotIn(b"Second", first)

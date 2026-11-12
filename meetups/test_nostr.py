"""Reading the calendar events of certified organisers from Nostr: what is believed, what is refused, and the relays.
No real relay is asked: the events are signed here and the relay is one of ours on 127.0.0.1."""
import json
import socket
import threading
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db.models.query import QuerySet
from django.test import TestCase, override_settings
from django.utils import timezone
from websockets.sync.server import serve

from shops.nostr import decode_geohash, geohash, public_key, sign

from config.community import community

from . import board, conf, relays
from .models import Meetup, Reading, TrustedAuthor
from .nostr_read import KEYS, MAX_EVENTS, Rejected, check, ingest, parse, shape, trusted_authors
from .tests import block, make

SECRET_A, SECRET_B, SECRET_C = "01" * 32, "02" * 32, "03" * 32
KEY_A, KEY_B, KEY_C = public_key(SECRET_A), public_key(SECRET_B), public_key(SECRET_C)
NOW = timezone.now().replace(microsecond=0)
# The community of the example sits in Rome; the position of the city the events are about
EXAMPLE = (41.9, 12.5)
CITIES = [{"name": "Example", "latitude": EXAMPLE[0], "longitude": EXAMPLE[1], "radius_km": 20}]


def stamp(days=0, hours=0):
    return int((NOW + timedelta(days=days, hours=hours)).timestamp())


def event(secret=SECRET_A, kind=31923, d="spritz-1", start=None, title="Wallet security", location="The Bar, Main Street 1, Example",
          extra=(), created_at=None, tzid="Europe/Rome", content="", drop=()):
    """A signed calendar event as an organiser's client publishes it."""
    if start is None:
        start = stamp(10) if kind == 31923 else timezone.localtime(NOW + timedelta(days=10)).date().isoformat()
    tags = [["d", d], ["title", title], ["start", str(start)], ["start_tzid", tzid], ["location", location], *map(list, extra)]
    tags = [tag for tag in tags if tag[0] not in drop and not (kind == 31922 and tag[0] == "start_tzid")]
    return sign(secret, kind, content, tags, created_at or stamp(-1))


def trust(*keys, note="Certified by the staff"):
    for key in keys:
        TrustedAuthor.objects.get_or_create(pubkey=key, defaults={"note": note})


def run(*events, now=NOW):
    return ingest(list(events), now=now)


class Checks(TestCase):
    """Each event is believed only after these."""

    def setUp(self):
        trust(KEY_A)

    def test_a_good_event_becomes_a_meetup(self):
        report = run(event(extra=[["g", geohash(*EXAMPLE)]]))
        self.assertEqual((report.created, report.rejected_total), (1, 0))
        meetup = Meetup.objects.get()
        self.assertEqual((meetup.source, meetup.title, meetup.status, meetup.nostr_author), ("nostr", "Wallet security", "confirmed", KEY_A))
        self.assertEqual(meetup.starts_at, NOW + timedelta(days=10))
        self.assertEqual(meetup.nostr_address, f"31923:{KEY_A}:spritz-1")
        self.assertEqual((meetup.place, meetup.tzid, meetup.all_day), ("The Bar, Main Street 1, Example", "Europe/Rome", False))

    def test_a_false_signature_is_refused(self):
        good = event()
        other = event(d="other")
        for bad in ({**good, "sig": other["sig"]}, {**good, "sig": "00" * 64}, {**good, "content": "changed"},
                    {**good, "tags": [*good["tags"], ["t", "x"]]}, {**good, "created_at": good["created_at"] + 1},
                    {**good, "id": other["id"]}, {**good, "pubkey": KEY_B}):
            trust(bad["pubkey"])
            report = run(bad)
            self.assertEqual(report.rejected["unsigned"], 1, bad)
        self.assertFalse(Meetup.objects.exists())

    def test_a_key_that_is_not_on_the_list_is_not_read(self):
        report = run(event(SECRET_B))
        self.assertEqual((report.created, report.rejected["untrusted"]), (0, 1))
        trust(KEY_B)
        self.assertEqual(run(event(SECRET_B)).created, 1)

    def test_a_key_the_staff_switched_off_is_not_read(self):
        TrustedAuthor.objects.filter(pubkey=KEY_A).update(active=False)
        self.assertEqual(run(event()).rejected["untrusted"], 1)

    def test_a_key_in_the_block_counts_too(self):
        with block(trusted=[{"npub": KEY_B, "note": "in the file"}]):
            self.assertEqual(set(trusted_authors()), {KEY_A, KEY_B})
            self.assertEqual(run(event(SECRET_B)).created, 1)

    def test_nothing_that_is_not_an_event_stops_the_others(self):
        junk = [None, 5, "text", [], {}, {"id": "x"}, {**event(), "kind": "31923"}, {**event(), "tags": "no"}, {**event(), "tags": [["d"]]},
                {**event(), "tags": [["d", 5]]}, {**event(), "tags": [[]]}, {**event(), "created_at": True}, {**event(), "created_at": 1.5},
                {**event(), "content": None}, {**event(), "id": "A" * 64}, {**event(), "sig": "00"}]
        report = run(*junk, event(d="fine"))
        # `["d"]` is a tag of one item, well formed: it is the id that no longer matches
        self.assertEqual((report.created, report.rejected["malformed"] + report.rejected["unsigned"]), (1, len(junk)))

    def test_a_kind_that_is_not_ours_is_ignored(self):
        self.assertEqual(run(sign(SECRET_A, 1, "hello", [])).rejected["malformed"], 1)
        self.assertEqual(run(sign(SECRET_A, 30402, "shop", [["d", "x"]])).rejected["malformed"], 1)

    def test_too_big_is_refused(self):
        giant = [["t", "x" * 10]] * 300
        self.assertEqual(run(event(extra=giant)).rejected["too_big"], 1)
        self.assertEqual(run(event(content="x" * 30000)).rejected["too_big"], 1)
        self.assertEqual(run(event(title="x" * 3000)).rejected["too_big"], 1)
        self.assertFalse(Meetup.objects.exists())

    def test_an_event_from_the_future_is_refused(self):
        self.assertEqual(run(event(created_at=stamp(3))).rejected["future"], 1)
        self.assertEqual(run(event(created_at=stamp(hours=-1))).created, 1)

    def test_a_lot_of_junk_is_seen_and_refused(self):
        report = run(*[{"x": number} for number in range(MAX_EVENTS + 50)])
        self.assertEqual((report.seen, report.rejected["malformed"]), (MAX_EVENTS + 50, MAX_EVENTS + 50))

    def test_the_ceiling_comes_after_the_copies_and_keeps_the_newest(self):
        events = [event(d=f"d{number}", created_at=stamp(hours=-number - 1)) for number in range(6)]
        # Four relays send the same events: the copies are one event each, and do not eat the ceiling
        with patch("meetups.nostr_read.MAX_EVENTS", 4):
            report = run(*(events * 4))
        self.assertEqual((report.seen, report.created, report.rejected["limit"]), (24, 4, 2))
        self.assertEqual(sorted(Meetup.objects.values_list("nostr_address", flat=True)),
                         sorted(f"31923:{KEY_A}:d{number}" for number in range(4)))
        self.assertIn("over the limit of one reading: 2", report.detail())
        with patch("meetups.nostr_read.MAX_EVENTS", 10):
            self.assertEqual(run(*(events * 4)).rejected["limit"], 0)

    def test_the_copies_of_one_event_from_four_relays_count_once(self):
        report = run(*([event()] * 4))
        self.assertEqual((report.seen, report.created, report.rejected_total), (4, 1, 0))

    def test_a_newer_version_nobody_signed_does_not_push_out_the_signed_one(self):
        genuine = event(title="Real", created_at=stamp(-3))
        forged = {**event(title="Forged", created_at=stamp(-1)), "sig": "00" * 64}
        report = run(forged, genuine)
        self.assertEqual((Meetup.objects.get().title, report.created, report.rejected["unsigned"]), ("Real", 1, 1))

    def test_what_an_event_carries_besides_its_seven_fields_is_dropped_and_never_looked_at(self):
        deep = []
        cursor = deep
        for _ in range(5000):
            inner = []
            cursor.append(inner)
            cursor = inner
        hostile = {**event(), "x": deep, "y": {"z": deep}}
        self.assertEqual(sorted(shape(hostile, NOW)), sorted(KEYS))
        self.assertEqual(run(hostile).created, 1)

    def test_the_size_is_counted_on_the_fields_that_count(self):
        self.assertEqual(run(event(extra=[["t", "x" * 1900]] * 20)).rejected["too_big"], 1)  # 38 kB of tags

    def test_check_keeps_the_order_cheap_first(self):
        stranger = event(SECRET_B)
        with patch("meetups.nostr_read.verified", side_effect=AssertionError("verified a stranger")):
            with self.assertRaises(Rejected) as caught:
                check(stranger, {KEY_A: "x"}, NOW)
        self.assertEqual(caught.exception.reason, "untrusted")


class Dates(TestCase):
    def setUp(self):
        trust(KEY_A)

    def test_absurd_dates_are_refused(self):
        for start in ("0", "-5", "1", "99999999999", "abc", "1e12", "12.5", "", " 1700000000", "9" * 30, "0x10", "1700000000;",
                      str(stamp(365 * 10)), "1230768000"):
            with self.subTest(start=start):
                report = run(event(start=start, d=f"d{start}"))
                self.assertEqual(report.created, 0, start)
                self.assertEqual(report.rejected["bad_start"] + report.rejected["absurd_date"] + report.rejected["too_old"], 1, start)

    def test_absurd_days_are_refused(self):
        for start in ("0000-01-01", "9999-12-31", "2026-13-01", "2026-02-30", "26-12-03", "2026-12-3", "2026/12/03", "2026-12-03T10:00",
                      "1970-01-01", "2004-02-29"):
            with self.subTest(start=start):
                self.assertEqual(run(event(kind=31922, start=start, d=f"d{start}")).created, 0, start)

    def test_a_day_event_is_a_day_without_an_hour(self):
        run(event(kind=31922, d="fair"))
        meetup = Meetup.objects.get()
        self.assertTrue(meetup.all_day)
        self.assertEqual(meetup.local_start.date(), (NOW + timedelta(days=10)).astimezone(meetup.local_start.tzinfo).date())

    def test_the_time_zone_is_the_events_when_it_is_one(self):
        run(event(tzid="America/New_York", d="ny"))
        self.assertEqual(Meetup.objects.get().tzid, "America/New_York")

    def test_a_zone_that_is_not_one_is_the_communitys(self):
        for number, zone in enumerate(("Mars/Olympus", "../../etc/passwd", "", "Europe/Rome\x00", "A" * 100, "UTC; DROP TABLE", "/etc/localtime")):
            run(event(tzid=zone, d=f"z{number}"))
        self.assertEqual({m.tzid for m in Meetup.objects.all()}, {"Europe/Rome"})
        self.assertEqual(Meetup.objects.count(), 7)

    def test_the_last_three_years_and_the_next_three_are_what_counts(self):
        self.assertEqual(run(event(start=stamp(-365 * 2), d="old")).created, 1)
        self.assertEqual(run(event(start=stamp(-365 * 4), d="older")).rejected["too_old"], 1)
        self.assertEqual(run(event(start=stamp(365 * 2), d="far")).created, 1)

    def test_a_missing_or_odd_d_is_refused(self):
        self.assertEqual(run(event(drop=("d",))).rejected["no_d"], 1)
        for number, d in enumerate(("", "x" * 300, "a\x00b", "line\nbreak", "‮bad")):
            self.assertEqual(run(event(d=d)).rejected["no_d"], 1, repr(d))
        self.assertFalse(Meetup.objects.exists())


class Text(TestCase):
    def setUp(self):
        trust(KEY_A)

    def test_html_becomes_plain_text(self):
        run(event(title="<b>Wallets</b> &amp; keys <script>alert(1)</script>", location="Caf&eacute; <i>Corner</i>, Example",
                  extra=[["image", "https://evil.example/x.png"]], content="<script>x</script>"))
        meetup = Meetup.objects.get()
        self.assertEqual(meetup.title, "Wallets & keys alert(1)")
        self.assertEqual(meetup.place, "Café Corner, Example")
        self.assertNotIn("<", meetup.title + meetup.place + meetup.city)

    def test_what_hides_or_reverses_text_is_gone(self):
        run(event(title="A‮evil‬\x00B C", location="x\x07y"))
        meetup = Meetup.objects.get()
        self.assertEqual((meetup.title, meetup.place), ("AevilB C", "xy"))

    def test_lengths_are_the_fields(self):
        run(event(title="t" * 1500, location="l" * 1500))
        meetup = Meetup.objects.get()
        self.assertEqual((len(meetup.title), len(meetup.place)), (200, 200))

    def test_the_title_may_say_it_is_off(self):
        for number, title in enumerate(("ANNULLATO - Spritz", "Spritz (cancelled)", "Abgesagt: Spritz", "Spritz annullata")):
            run(event(title=title, d=f"c{number}"))
        run(event(title="Spritz of the cancellations committee", d="fine"))
        self.assertEqual(sorted(Meetup.objects.values_list("status", flat=True)), ["cancelled"] * 4 + ["confirmed"])

    def test_the_image_the_description_and_the_participants_are_not_kept(self):
        run(event(extra=[["image", "https://evil.example/x.png"], ["p", KEY_B, "", "host"], ["summary", "words"]], content="long text"))
        meetup = Meetup.objects.get()
        shown = " ".join(str(getattr(meetup, field.name)) for field in meetup._meta.fields)
        for text in ("evil.example", KEY_B, "words", "long text"):
            self.assertNotIn(text, shown)


class Links(TestCase):
    def setUp(self):
        trust(KEY_A)

    def test_links_inside_events_are_off_unless_asked(self):
        run(event(extra=[["r", "https://app.example.org/e/1"]]))
        self.assertEqual(Meetup.objects.get().url, "")

    def test_when_asked_only_a_good_https_link_is_kept(self):
        with block(event_links=True):
            run(event(extra=[["r", "javascript:alert(1)"], ["r", "http://x.org/"], ["r", "https://u:p@x.org/"], ["r", "https://app.example.org/e/1"]]))
        self.assertEqual(Meetup.objects.get().url, "https://app.example.org/e/1")

    def test_when_asked_a_bad_link_is_no_link(self):
        with block(event_links=True):
            run(event(extra=[["r", "javascript:alert(1)"], ["r", "https://a.org/\" onclick=\"x"]]))
        self.assertEqual(Meetup.objects.get().url, "")

    def test_event_links_must_be_exactly_true(self):
        with block(event_links="yes"):
            run(event(extra=[["r", "https://app.example.org/e/1"]]))
        self.assertEqual(Meetup.objects.get().url, "")


class Cities(TestCase):
    def setUp(self):
        trust(KEY_A)

    def test_without_cities_everything_from_a_trusted_author_is_kept_with_no_city(self):
        run(event(location="Anywhere"))
        meetup = Meetup.objects.get()
        self.assertEqual((meetup.city, meetup.place), ("", "Anywhere"))

    def test_the_position_decides(self):
        with block(cities=CITIES):
            self.assertEqual(run(event(d="near", extra=[["g", geohash(41.91, 12.46)]], location="Elsewhere, 3")).created, 1)
            self.assertEqual(run(event(d="far", extra=[["g", geohash(48.0, 2.0)]], location="Example")).rejected["outside"], 1)
        meetup = Meetup.objects.get()
        self.assertEqual((meetup.city, meetup.place), ("Example", "Elsewhere, 3"))

    def test_without_a_position_the_name_decides(self):
        with block(cities=CITIES):
            self.assertEqual(run(event(d="named", location="The Bar, Main Street 1, Example")).created, 1)
            self.assertEqual(run(event(d="named2", location="The Bar", title="Spritz Example")).created, 1)
            self.assertEqual(run(event(d="other", location="The Bar, Main Street 1, Elsewhere")).rejected["outside"], 1)
            self.assertEqual(run(event(d="inside", location="Examplex")).rejected["outside"], 1)
        self.assertEqual(Meetup.objects.get(nostr_address__endswith=":named").place, "The Bar, Main Street 1")

    def test_lat_and_lon_tags_work_too(self):
        with block(cities=CITIES):
            self.assertEqual(run(event(d="a", extra=[["lat", "41.91"], ["lon", "12.46"]], location="x")).created, 1)
            self.assertEqual(run(event(d="b", extra=[["lat", "nan"], ["lon", "12.46"]], location="x")).rejected["outside"], 1)
            self.assertEqual(run(event(d="c", extra=[["lat", "500"], ["lon", "12.46"]], location="x")).rejected["outside"], 1)

    def test_a_bad_geohash_is_no_position(self):
        self.assertIsNone(decode_geohash("aaaa"))
        self.assertIsNone(decode_geohash(""))
        self.assertIsNone(decode_geohash("u" * 20))
        lat, lon = decode_geohash(geohash(*EXAMPLE))
        self.assertAlmostEqual(lat, EXAMPLE[0], places=3)
        self.assertAlmostEqual(lon, EXAMPLE[1], places=3)
        with block(cities=CITIES):
            self.assertEqual(run(event(d="x", extra=[["g", "AAAA"]], location="Example")).created, 1)  # no position: the name

    def test_the_nearest_city_is_the_one(self):
        two = [{"name": "Example", "latitude": 41.9, "longitude": 12.5, "radius_km": 50},
               {"name": "Nextville", "latitude": 42.08, "longitude": 12.58, "radius_km": 50}]
        with block(cities=two):
            run(event(d="n", extra=[["g", geohash(42.07, 12.58)]], location="x"))
        self.assertEqual(Meetup.objects.get().city, "Nextville")


class Replacing(TestCase):
    """An event is addressable: the newest version replaces the older, and the staff's correction is never replaced."""

    def setUp(self):
        trust(KEY_A, KEY_B)

    def test_the_newer_version_updates(self):
        run(event(title="First", created_at=stamp(-3)))
        run(event(title="Second", created_at=stamp(-2), start=stamp(12)))
        meetup = Meetup.objects.get()
        self.assertEqual((meetup.title, meetup.starts_at), ("Second", NOW + timedelta(days=12)))
        self.assertEqual(Meetup.objects.count(), 1)

    def test_an_older_version_does_not(self):
        run(event(title="Second", created_at=stamp(-2)))
        report = run(event(title="First", created_at=stamp(-3)))
        self.assertEqual((Meetup.objects.get().title, report.older), ("Second", 1))

    def test_the_same_version_again_changes_nothing(self):
        same = event(created_at=stamp(-2))
        run(same)
        report = run(same)
        self.assertEqual((report.created, report.updated, report.older), (0, 0, 1))

    def test_in_one_reading_only_the_newest_of_an_address_counts(self):
        report = run(event(title="First", created_at=stamp(-3)), event(title="Third", created_at=stamp(-1)), event(title="Second", created_at=stamp(-2)))
        self.assertEqual((Meetup.objects.get().title, report.created, report.updated), ("Third", 1, 0))

    def test_two_authors_with_the_same_d_are_two_meetups(self):
        run(event(SECRET_A, d="same"), event(SECRET_B, d="same"))
        self.assertEqual(Meetup.objects.count(), 2)

    def test_a_kind_is_an_address_of_its_own(self):
        run(event(kind=31923, d="same"), event(kind=31922, d="same"))
        self.assertEqual(Meetup.objects.count(), 2)

    def test_what_the_staff_corrected_stays(self):
        run(event(title="First", created_at=stamp(-3)))
        Meetup.objects.update(status="cancelled", staff_edited=True, title="Corrected")
        report = run(event(title="Second", created_at=stamp(-2), start=stamp(12)))
        meetup = Meetup.objects.get()
        self.assertEqual((meetup.title, meetup.status, meetup.starts_at, report.kept, report.updated),
                         ("Corrected", "cancelled", NOW + timedelta(days=10), 1, 0))

    def test_handing_it_back_lets_the_reader_update(self):
        run(event(title="First", created_at=stamp(-3)))
        Meetup.objects.update(staff_edited=True)
        run(event(title="Second", created_at=stamp(-2)))
        Meetup.objects.update(staff_edited=False)
        run(event(title="Second", created_at=stamp(-2)))
        self.assertEqual(Meetup.objects.get().title, "Second")

    def test_a_meetup_the_staff_wrote_is_not_touched(self):
        own = make(10, title="Mine")
        run(event(title="Theirs"))
        own.refresh_from_db()
        self.assertEqual((own.title, Meetup.objects.count()), ("Mine", 2))

    def test_a_correction_the_staff_saves_while_the_reader_works_is_not_lost(self):
        run(event(created_at=stamp(-3), title="Wallet security"))
        real_first = QuerySet.first

        def first(queryset):
            found = real_first(queryset)
            if found is not None and found.nostr_address:
                # The staff saves its correction now: after the reader looked at the meetup and before it writes
                Meetup.objects.filter(pk=found.pk).update(title="Corrected by the staff", staff_edited=True)
            return found

        with patch.object(QuerySet, "first", first):
            report = run(event(created_at=stamp(-1), title="Overwritten title"))
        meetup = Meetup.objects.get()
        self.assertEqual((meetup.title, meetup.staff_edited, report.updated, report.kept), ("Corrected by the staff", True, 0, 1))

    def test_what_the_staff_hid_stays_hidden_after_an_update(self):
        run(event(created_at=stamp(-3)))
        Meetup.objects.update(visible=False, staff_edited=True)
        run(event(title="Again", created_at=stamp(-2)))
        self.assertFalse(Meetup.objects.get().visible)

    def test_a_title_that_says_cancelled_in_a_newer_version_cancels(self):
        run(event(title="Spritz", created_at=stamp(-3)))
        run(event(title="ANNULLATO Spritz", created_at=stamp(-2)))
        self.assertEqual(Meetup.objects.get().status, "cancelled")


def deletion(secret, *tags, created_at=None):
    return sign(secret, 5, "", [list(tag) for tag in tags], created_at or stamp(hours=-1))


class Deletions(TestCase):
    def setUp(self):
        trust(KEY_A, KEY_B)
        run(event(d="spritz", created_at=stamp(-3)))
        self.address = f"31923:{KEY_A}:spritz"

    def status(self):
        return Meetup.objects.get().status

    def test_the_author_deleting_cancels_a_meetup_to_come(self):
        report = run(deletion(SECRET_A, ["a", self.address]))
        self.assertEqual((self.status(), report.cancelled), ("cancelled", 1))
        self.assertEqual(Meetup.objects.count(), 1)  # it stays on the board as cancelled

    def test_by_the_id_of_the_event_too(self):
        run(deletion(SECRET_A, ["e", Meetup.objects.get().nostr_event_id]))
        self.assertEqual(self.status(), "cancelled")

    def test_another_author_cannot_cancel_it(self):
        run(deletion(SECRET_B, ["a", self.address]))
        self.assertEqual(self.status(), "confirmed")
        run(deletion(SECRET_B, ["e", Meetup.objects.get().nostr_event_id]))
        self.assertEqual(self.status(), "confirmed")

    def test_a_stranger_cannot_either(self):
        report = run(deletion(SECRET_C, ["a", self.address]))
        self.assertEqual((self.status(), report.rejected["untrusted"]), ("confirmed", 1))

    def test_a_deletion_with_a_signature_that_is_false_is_not_read(self):
        bad = {**deletion(SECRET_A, ["a", self.address]), "sig": "00" * 64}
        self.assertEqual((run(bad).rejected["unsigned"], self.status()), (1, "confirmed"))

    def test_a_deletion_aimed_at_someone_elses_address_is_ignored(self):
        run(deletion(SECRET_A, ["a", f"31923:{KEY_B}:spritz"]))
        self.assertEqual(self.status(), "confirmed")

    def test_a_deletion_older_than_the_version_does_nothing(self):
        run(deletion(SECRET_A, ["a", self.address], created_at=stamp(-5)))
        self.assertEqual(self.status(), "confirmed")

    def test_a_deletion_after_the_date_is_a_clean_up(self):
        Meetup.objects.update(starts_at=NOW - timedelta(days=2))
        run(deletion(SECRET_A, ["a", self.address]))
        self.assertEqual(self.status(), "confirmed")

    def test_what_the_staff_corrected_is_not_cancelled_by_the_reader(self):
        Meetup.objects.update(staff_edited=True)
        run(deletion(SECRET_A, ["a", self.address]))
        self.assertEqual(self.status(), "confirmed")

    def test_odd_tags_are_ignored(self):
        run(deletion(SECRET_A, ["a"], ["a", "garbage"], ["a", "1:2"], ["a", "30023:" + KEY_A + ":spritz"], ["e", "nothex"], ["e", "A" * 64], ["x", "y"]))
        self.assertEqual(self.status(), "confirmed")

    def test_a_newer_version_after_the_deletion_brings_it_back(self):
        run(deletion(SECRET_A, ["a", self.address], created_at=stamp(-1)))
        self.assertEqual(self.status(), "cancelled")
        run(event(d="spritz", created_at=stamp(hours=-2), title="Spritz again"))
        self.assertEqual((self.status(), Meetup.objects.get().title), ("confirmed", "Spritz again"))

    def test_deleting_twice_is_the_same_as_once(self):
        gone = deletion(SECRET_A, ["a", self.address])
        run(gone)
        self.assertEqual(run(gone).cancelled, 0)


class Limits(TestCase):
    def setUp(self):
        trust(KEY_A)

    def test_one_reading_makes_only_so_many(self):
        with patch("meetups.nostr_read.MAX_NEW_PER_RUN", 3):
            report = run(*[event(d=f"d{number}") for number in range(6)])
        self.assertEqual((report.created, report.rejected["limit"], Meetup.objects.count()), (3, 3, 3))

    def test_nor_more_than_the_ceiling_in_all(self):
        for number in range(3):
            make(5 + number, source="nostr", nostr_address=f"31923:{KEY_B}:{number}", nostr_author=KEY_B, nostr_created_at=1)
        with patch.dict(community(), {"limits": {"nostr_meetups": 4}}):
            report = run(event(d="a"), event(d="b"))
        self.assertEqual((report.created, report.rejected["limit"]), (1, 1))

    def test_updates_are_not_held_back_by_the_ceiling(self):
        run(event(d="a", created_at=stamp(-3)))
        with patch.dict(community(), {"limits": {"nostr_meetups": 1}}):
            self.assertEqual(run(event(d="a", created_at=stamp(-2), title="New")).updated, 1)

    def test_the_parse_alone_says_why(self):
        with self.assertRaises(Rejected) as caught:
            parse(event(start="abc"), NOW, [], False)
        self.assertEqual(caught.exception.reason, "bad_start")


class FakeRelay:
    """A relay of ours: answers a REQ with the events of the kinds and authors asked, then EOSE. `mode` makes it misbehave."""

    def __init__(self, events=(), mode="normal"):
        self.events, self.mode, self.requests = list(events), mode, []
        self.server = serve(self.handle, "127.0.0.1", 0, max_size=2 ** 22)
        self.url = f"ws://127.0.0.1:{self.server.socket.getsockname()[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def handle(self, ws):
        try:
            for raw in ws:
                message = json.loads(raw)
                if message[0] != "REQ":
                    continue
                self.requests.append(message)
                _name, sub, question = message
                if self.mode == "silent":
                    continue
                if self.mode == "closed":
                    ws.send(json.dumps(["CLOSED", sub, "auth-required: sign in"]))
                    continue
                if self.mode == "huge":
                    ws.send("x" * (300 * 1024))
                    continue
                if self.mode == "nested":
                    ws.send("[" * 30000)  # the parser gives up on it with a RecursionError
                    ws.send('["EVENT", "' + sub + '", ' + "[" * 30000 + "]")
                if self.mode == "bigframe":
                    ws.send(json.dumps(["EVENT", sub, {**self.events[0], "content": "x" * (60 * 1024)}]))
                if self.mode == "strangers":
                    ws.send(json.dumps(["EVENT", sub, event(SECRET_C, d="stranger")]))
                if self.mode == "noise":
                    ws.send(b"\x00\x01binary")
                    ws.send("not json")
                    ws.send(json.dumps({"EVENT": 1}))
                    ws.send(json.dumps(["EVENT", "other-sub", self.events[0]]))
                for found in self.events:
                    if found["kind"] in question["kinds"] and found["pubkey"] in question["authors"]:
                        ws.send(json.dumps(["EVENT", sub, found]))
                if self.mode == "flood":
                    for number in range(relays.MAX_EVENTS + 100):
                        ws.send(json.dumps(["EVENT", sub, self.events[0]]))
                if self.mode == "drop":
                    ws.close()
                    return
                ws.send(json.dumps(["EOSE", sub]))
        except Exception:  # noqa: BLE001 - the client left
            pass

    def stop(self):
        self.server.shutdown()


class Relays(TestCase):
    def setUp(self):
        self.started = []

    def tearDown(self):
        for relay in self.started:
            relay.stop()

    def relay(self, *events, mode="normal"):
        relay = FakeRelay(events, mode)
        self.started.append(relay)
        return relay

    def read(self, relay, **kwargs):
        with override_settings(NOSTR_RELAYS=[relay.url]):
            return relays.read(relay.url, relays.build_filters([KEY_A], 0), own=conf.own_relays(), **kwargs)

    def test_it_asks_for_the_calendar_and_the_deletions_of_the_authors(self):
        relay = self.relay(event())
        events, complete = self.read(relay)
        self.assertEqual((len(events), complete), (1, True))
        kinds = [message[2]["kinds"] for message in relay.requests]
        self.assertEqual(kinds, [[31922, 31923], [5]])
        self.assertTrue(all(message[2]["authors"] == [KEY_A] for message in relay.requests))
        self.assertTrue(all(message[0] == "REQ" for message in relay.requests))

    def test_many_authors_make_many_questions(self):
        questions = relays.build_filters([f"{number:064x}" for number in range(60)], 5)
        self.assertEqual([len(q["authors"]) for q in questions], [25, 25, 25, 25, 10, 10])
        self.assertTrue(all(q["since"] == 5 for q in questions))

    def test_a_relay_that_stays_silent_is_given_up_at_the_time_limit(self):
        relay = self.relay(event(), mode="silent")
        with patch("meetups.relays.RECV_SECONDS", 0.3):
            with self.assertRaises(relays.RelayError):
                self.read(relay, seconds=1)

    def test_a_relay_that_refuses_gives_what_it_gave(self):
        relay = self.relay(event(), mode="closed")
        with self.assertRaises(relays.RelayError):
            self.read(relay)

    def test_a_message_over_the_ceiling_ends_the_reading(self):
        relay = self.relay(event(), mode="huge")
        with self.assertRaises(relays.RelayError):
            self.read(relay)

    def test_noise_is_skipped(self):
        relay = self.relay(event(), mode="noise")
        events, complete = self.read(relay)
        self.assertEqual((len(events), complete), (1, True))

    def test_json_nested_to_the_depth_of_a_bomb_is_skipped_and_the_reading_goes_on(self):
        relay = self.relay(event(), mode="nested")
        events, complete = self.read(relay)
        self.assertEqual((len(events), complete), (1, True))

    def test_a_frame_bigger_than_an_event_may_be_is_not_parsed(self):
        relay = self.relay(event(), mode="bigframe")
        with patch("meetups.relays.json.loads", wraps=json.loads) as parse_it:
            events, complete = self.read(relay)
        self.assertEqual((len(events), complete), (1, True))
        self.assertTrue(all(len(call.args[0]) <= relays.EVENT_FRAME_BYTES for call in parse_it.call_args_list))

    def test_only_the_authors_asked_for_are_kept(self):
        relay = self.relay(event(), mode="strangers")
        events, _complete = self.read(relay)
        self.assertEqual([found["pubkey"] for found in events], [KEY_A])

    def test_a_relay_gets_only_so_much_of_our_memory(self):
        relay = self.relay(event(), mode="flood")
        # A flood shorter than the real ceiling on events, still well over the bytes allowed
        with patch("meetups.relays.TOTAL_BYTES", 5000), patch("meetups.relays.MAX_EVENTS", 100):
            events, complete = self.read(relay)
        self.assertEqual(complete, False)
        self.assertLess(len(events), 12)

    def test_whatever_a_relay_does_is_one_relay_failing(self):
        good = self.relay(event())
        said = []
        with patch("meetups.relays.read", side_effect=[RecursionError("deep"), ([event()], True)]):
            found, answered = relays.read_all(["wss://one.example.org", good.url], [KEY_A], 0, said.append)
        self.assertEqual((len(found), answered), (1, 1))
        self.assertEqual(said[0], "wss://one.example.org: RecursionError")

    def test_a_flood_is_cut(self):
        relay = self.relay(event(), mode="flood")
        # A lower ceiling (the relay floods past it by 100), so that the flood is quick to send
        with patch("meetups.relays.MAX_EVENTS", 100):
            events, complete = self.read(relay, limit=100)
        self.assertEqual((len(events) <= 100, complete), (True, False))

    def test_a_relay_that_drops_halfway_still_gives_what_came(self):
        relay = self.relay(event(), mode="drop")
        events, complete = self.read(relay)
        self.assertEqual((len(events), complete), (1, False))

    def test_a_relay_that_is_not_there_is_one_relay_failing(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        url = f"ws://127.0.0.1:{port}"
        with override_settings(NOSTR_RELAYS=[url]):
            with self.assertRaises(relays.RelayError):
                relays.read(url, relays.build_filters([KEY_A], 0), own=conf.own_relays())

    def test_read_all_goes_on_after_a_relay_fails(self):
        good = self.relay(event())
        said = []
        with override_settings(NOSTR_RELAYS=[good.url, "ws://127.0.0.1:9"]):
            found, answered = relays.read_all([good.url, "ws://127.0.0.1:9"], [KEY_A], 0, said.append)
        self.assertEqual((len(found), answered, len(said)), (1, 1, 2))
        self.assertTrue(any(line.startswith("ws://127.0.0.1:9") for line in said))
        self.assertTrue(any("1 events" in line for line in said))

    # What is not ours is read only if it is wss:// and public

    def test_a_plain_ws_relay_of_someone_else_is_not_read(self):
        relay = self.relay(event())
        with override_settings(NOSTR_RELAYS=[]):
            with self.assertRaises(relays.RelayError) as caught:
                relays.read(relay.url, relays.build_filters([KEY_A], 0), own=[])
        self.assertIn("wss", str(caught.exception))
        self.assertEqual(relay.requests, [])

    def test_a_name_that_leads_inside_is_not_read(self):
        for host, address in (("relay.example.org", "127.0.0.1"), ("relay.example.org", "10.0.0.5"), ("relay.example.org", "192.168.1.9"),
                              ("relay.example.org", "169.254.169.254"), ("relay.example.org", "::1"), ("relay.example.org", "fd00::1"),
                              ("relay.example.org", "::ffff:10.0.0.1"), ("relay.example.org", "100.64.0.1"), ("relay.example.org", "0.0.0.0"),
                              ("relay.example.org", "::127.0.0.1"), ("relay.example.org", "64:ff9b::7f00:1")):
            with self.subTest(address=address), patch("meetups.relays.socket.getaddrinfo", return_value=[(2, 1, 6, "", (address, 443))]), \
                    patch("meetups.relays.connect") as connect:
                with self.assertRaises(relays.RelayError):
                    relays.read(f"wss://{host}", [], own=[])
                connect.assert_not_called()

    def test_one_private_address_among_public_ones_is_enough_to_refuse(self):
        found = [(2, 1, 6, "", ("93.184.216.34", 443)), (2, 1, 6, "", ("10.0.0.5", 443))]
        with patch("meetups.relays.socket.getaddrinfo", return_value=found), patch("meetups.relays.connect") as connect:
            with self.assertRaises(relays.RelayError):
                relays.read("wss://relay.example.org", [], own=[])
        connect.assert_not_called()

    def test_a_name_that_changes_its_mind_after_the_check_reaches_nothing(self):
        # The name looks public, the connection lands on an internal address: the check on the address connected to stops it
        fake = patch("meetups.relays.socket.socket")
        with patch("meetups.relays.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 443))]), fake as sock, \
                patch("meetups.relays.connect") as connect:
            sock.return_value.getpeername.return_value = ("10.0.0.5", 443)
            with self.assertRaises(relays.RelayError):
                relays.read("wss://relay.example.org", [], own=[])
            sock.return_value.close.assert_called()
        connect.assert_not_called()

    def test_a_public_name_is_connected_through_the_socket_we_checked(self):
        with patch("meetups.relays.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 443))]), \
                patch("meetups.relays.socket.socket") as sock, patch("meetups.relays.connect") as connect:
            sock.return_value.getpeername.return_value = ("93.184.216.34", 443)
            connect.return_value.__enter__.return_value.recv.side_effect = TimeoutError
            with self.assertRaises(relays.RelayError):  # the mock says nothing: that is a relay that did not answer
                relays.read("wss://relay.example.org", relays.build_filters([KEY_A], 0), own=[])
        self.assertIs(connect.call_args.kwargs["sock"], sock.return_value)
        self.assertIsNone(connect.call_args.kwargs["proxy"])
        self.assertEqual(connect.call_args.args[0], "wss://relay.example.org")
        self.assertLessEqual(connect.call_args.kwargs["max_size"], 256 * 1024)

    def test_a_name_that_does_not_exist_is_one_relay_failing(self):
        with patch("meetups.relays.socket.getaddrinfo", side_effect=socket.gaierror):
            with self.assertRaises(relays.RelayError):
                relays.read("wss://nowhere.example.org", [], own=[])


class ReadCommand(TestCase):
    def setUp(self):
        trust(KEY_A)

    def call(self, *args, relay_events=None, answered=1, **kwargs):
        out, err = StringIO(), StringIO()
        events = [event()] if relay_events is None else relay_events

        def fake(urls, authors, since, write):
            write("wss://relay.example.org: fake")
            return events, answered

        with block(relays=["wss://relay.example.org"], past_months=12), patch("meetups.management.commands.read_meetups.relays.read_all", fake):
            call_command("read_meetups", *args, stdout=out, stderr=err, **kwargs)
        return out.getvalue()

    def test_it_reads_and_keeps_the_reading(self):
        out = self.call()
        self.assertIn("1 events: 1 new", out)
        reading = Reading.objects.get()
        self.assertEqual((reading.relays_asked, reading.relays_answered, reading.created), (1, 1, 1))
        self.assertEqual(Meetup.objects.count(), 1)

    def test_a_dry_run_changes_nothing(self):
        out = self.call("--dry-run")
        self.assertIn("1 new", out)
        self.assertEqual((Meetup.objects.count(), Reading.objects.count()), (0, 0))

    def test_the_reasons_for_what_was_left_out_are_said(self):
        out = self.call(relay_events=[event(SECRET_B), event(start="abc", d="b")])
        self.assertIn("2 rejected", out)
        self.assertIn("author not on the list: 1", out)
        self.assertIn("start that is not a date: 1", out)
        self.assertIn("author not on the list", Reading.objects.get().detail)

    def test_no_relay_answering_fails_the_run_so_that_it_is_seen(self):
        with self.assertRaises(CommandError):
            self.call(relay_events=[], answered=0)
        self.assertEqual(Reading.objects.get().relays_answered, 0)

    def test_nothing_to_read_is_not_an_error(self):
        out = StringIO()
        with block(relays=[]):
            call_command("read_meetups", stdout=out)
        self.assertIn("no relays", out.getvalue())
        TrustedAuthor.objects.all().delete()
        with block(relays=["wss://relay.example.org"]):
            call_command("read_meetups", stdout=out)
        self.assertIn("no trusted authors", out.getvalue())

    def test_without_the_block_it_does_nothing(self):
        out = StringIO()
        call_command("read_meetups", stdout=out)
        self.assertIn("not in community.yml", out.getvalue())

    def test_only_the_last_readings_are_kept(self):
        with patch("meetups.management.commands.read_meetups.KEEP_READINGS", 3):
            for _ in range(5):
                self.call()
        self.assertEqual(Reading.objects.count(), 3)

    def test_end_to_end_through_a_relay_of_ours(self):
        relay = FakeRelay([event(), event(SECRET_B, d="stranger"), deletion(SECRET_A, ["a", f"31923:{KEY_A}:gone"])])
        try:
            out = StringIO()
            with override_settings(NOSTR_RELAYS=[relay.url]), block(past_months=12):
                call_command("read_meetups", stdout=out)
        finally:
            relay.stop()
        self.assertIn("2 events: 1 new", out.getvalue())
        self.assertEqual(Meetup.objects.get().title, "Wallet security")
        # Only the trusted author was asked for
        self.assertTrue(all(message[2]["authors"] == [KEY_A] for message in relay.requests))


class ReadingAndTheBoard(TestCase):
    """The board is kept for a few minutes (board.py): a reading that changes a meetup makes it new at once."""

    def _pre_setup(self):
        super()._pre_setup()
        cache.clear()

    def test_a_reading_that_changes_something_makes_the_board_new(self):
        trust(KEY_A)
        self.assertEqual(board.board()["upcoming"], [])
        ingest([event(title="From Nostr")])
        self.assertEqual([r["title"] for r in board.board()["upcoming"]], ["From Nostr"])
        ingest([event(title="From Nostr, changed", created_at=int(timezone.now().timestamp()))])
        self.assertEqual([r["title"] for r in board.board()["upcoming"]], ["From Nostr, changed"])

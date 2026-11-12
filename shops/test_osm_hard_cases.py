"""OpenStreetMap, the hard cases: the companion tags that follow a more recent "no",
the dates OpenStreetMap keeps besides the one for bitcoin, the forms of an operator tag, a coordinate that would take a
gigabyte, the readings that fail and the one that worked, a node put next to a shop by a stranger, what goes into an
osmChange file, an element that lost a tag to the parser, and the migrations both ways."""
import xml.etree.ElementTree as ET
from datetime import timedelta

from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from . import osm, osm_edit
from .models import OsmRun
from .test_osm import TODAY, answer, config, day, meta, node, read, shop

TAG = "payment:bitcoin:operator"


def iso(days):
    """The day `days` ago, as OpenStreetMap writes it: the dates of these tests move with the clock."""
    return (timezone.now() - timedelta(days=days)).date().isoformat()


class HardCases(TestCase):
    def changes(self, mine, element, **more):
        return sorted(osm.changes_for(mine, read(answer(element))[0], config(**more), TODAY))

    def test_nothing_goes_with_a_no_that_is_more_recent_than_our_check(self):
        mine = shop(discount=10, lightning_worked=day(days=300))
        element = node(1, "Bar Aurora", **{"currency:XBT": "no", "check_date:currency:XBT": iso(20)})
        # Not the lightning tag, the on-chain tag, the discount, the operator: none of the tags that go with a "yes"
        self.assertEqual(self.changes(mine, element), [])
        # Equal days are not "more recent" for us either; and an address of ours that never worked is nothing to say against a no
        same = shop("Bar Same", lightning_worked=day(days=5))
        paid = timezone.localtime(timezone.now() - timedelta(days=5)).date().isoformat()
        self.assertEqual(self.changes(same, node(2, "Bar Same", **{"currency:XBT": "no", "check_date:currency:XBT": paid})), [])
        never = shop("Bar Never")
        self.assertEqual(self.changes(never, node(3, "Bar Never", **{"currency:XBT": "no"})), [])

    def test_everything_waits_for_a_person_when_our_check_is_more_recent_than_the_no(self):
        mine = shop(discount=10, lightning_worked=day(days=3))
        element = node(1, "Bar Aurora", **{"currency:XBT": "no", "check_date:currency:XBT": iso(40)})
        found = self.changes(mine, element)
        self.assertTrue(found)
        self.assertEqual({kind for _tag, _old, _new, kind in found}, {"review"})
        self.assertIn(("currency:XBT", "no", "yes", "review"), found)
        self.assertIn(("payment:lightning", None, "yes", "review"), found)
        # An undated no is a no too: our working address is the only evidence, and a person reads it
        undated = shop("Bar Undated", lightning_worked=day(days=3))
        self.assertEqual({kind for *_rest, kind in self.changes(undated, node(2, "Bar Undated", **{"currency:XBT": "no"}))}, {"review"})

    def test_the_other_dates_of_openstreetmap_are_checks_too(self):
        mine = shop(lightning_worked=day(days=100))
        for key in ("check_date", "survey:date", "check_date:payment:lightning", "check_date:payment:onchain", "check_date:currency:XBT"):
            no = node(1, "Bar Aurora", **{"currency:XBT": "no", "payment:lightning": "no", key: iso(20)})
            self.assertEqual(self.changes(mine, no, operator=None), [], key)
            yes = node(2, "Bar Aurora", **{"currency:XBT": "yes", "payment:lightning": "yes", key: iso(20)})
            # Checked there after our address last worked: our date does not replace theirs
            self.assertEqual(self.changes(mine, yes, operator=None), [], key)
        self.assertEqual(osm.osm_day({"check_date": "2025-01-01", "survey:date": "2026-02-03", "check_date:payment:lightning": "2024-05",
                                      "check_date:currency:XBT": "2023-01-01", "check_date:other": "2030-01-01"}), osm.parse_day("2026-02-03"))
        self.assertIsNone(osm.osm_day({"check_date": "soon", "name": "2026-01-01"}))
        # An older generic date does not hide our check
        older = node(3, "Bar Aurora", **{"currency:XBT": "yes", "payment:lightning": "yes", "check_date": "2020-01-01"})
        self.assertEqual([tag for tag, *_rest in self.changes(mine, older, operator=None)], ["check_date:currency:XBT"])

    def test_the_dates_decide_in_the_report_too(self):
        shop("Bar Generic", osm_ref="node/1", lightning_worked=day(days=100))
        found, skipped = osm.parse(answer(node(1, "Bar Generic", **{"currency:XBT": "yes", "check_date": iso(20)})))
        report = osm.synchronize(config(), found, skipped)
        self.assertEqual([item["name"] for item in report["newer_on_osm"]], ["Bar Generic"])

    def test_the_forms_of_an_operator_tag(self):
        mine = shop(lightning_worked=day(days=3))
        base = {"currency:XBT": "yes", "payment:lightning": "yes", "check_date:currency:XBT": timezone.localtime(timezone.now() - timedelta(days=3)).date().isoformat()}
        cases = {"old.example": "old.example;operator.example", "a;": "a;operator.example", "a; ": "a;operator.example",
                 ";a": "a;operator.example", " ": "operator.example", "": "operator.example", " ; ; ": "operator.example",
                 "old.example;other.example": "old.example;other.example;operator.example",
                 "old.example ;  other.example": "old.example;other.example;operator.example"}
        for current, expected in cases.items():
            self.assertEqual(self.changes(mine, node(9, "Bar", **base, **{TAG: current})), [(TAG, current, expected, "update")], repr(current))
        # Ours is there already, whatever the capitals and the spaces
        for current in ("OPERATOR.example", "old.example; Operator.Example ", "operator.example;", ";operator.example"):
            self.assertEqual(self.changes(mine, node(9, "Bar", **base, **{TAG: current})), [], repr(current))
        self.assertEqual(self.changes(mine, node(9, "Bar", **base)), [(TAG, None, "operator.example", "add")])

    def test_a_coordinate_that_would_take_a_gigabyte(self):
        found, skipped = osm.parse(osm.load('{"elements": [{"type": "node", "id": 1, "lat": 1e-20000000, "lon": 1, "tags": {"a": "b"}}]}'))
        self.assertEqual((len(found), skipped, found[0].text_lat), (1, 0, "0.000000000"))
        found, _skipped = osm.parse(osm.load('{"elements": [{"type": "node", "id": 1, "lat": 1e-999999999, "lon": -1e-999999999, "tags": {"a": "b"}}]}'))
        self.assertEqual((found[0].text_lat, found[0].text_lon), ("0.000000000", "0.000000000"))

    def test_failures_do_not_evict_the_good_reading_and_do_not_pile_up(self):
        good = osm.save_run({"created": timezone.now().isoformat(), "elements": 80, "proposals": []})
        for _ in range(12):
            osm.save_run(error="the server answered 504")
        self.assertEqual((OsmRun.objects.count(), osm.last_run().pk, osm.previous_run().pk), (2, good.pk, good.pk))
        for number in range(12):
            osm.save_run(error=f"the server answered 50{number}")
        self.assertEqual((OsmRun.objects.filter(ok=True).count(), OsmRun.objects.filter(ok=False).count()), (1, osm.KEEP_FAILED))
        self.assertEqual(osm.previous_run().pk, good.pk)

    def test_a_node_put_next_to_a_shop_by_a_stranger_is_a_proposal_and_writes_nothing(self):
        mine = shop("Bar Aurora")
        fake = node(77, "Bar Aurora", lat=41.9002, lon=12.5, **{"currency:XBT": "yes", "website": "https://scam.example/", "phone": "+39 800 111222",
                                                                "addr:street": "Fake Street", "addr:city": "Nowhere"})
        report = osm.synchronize(config(), read(answer(fake)))
        mine.refresh_from_db()
        self.assertEqual((mine.osm_ref, mine.osm_proposed), ("", "node/77"))
        self.assertEqual((mine.website, mine.phone, mine.address, mine.city), ("", "", "", ""))
        self.assertEqual((report["filled"], report["offers"], report["proposals"]), ([], [], []))
        self.assertEqual([item["ref"] for item in report["link_proposals"]], ["node/77"])
        # Confirmed by the association: the address by itself, the contacts only offered
        self.assertTrue(osm.confirm_link(mine))
        mine.refresh_from_db()
        again = osm.synchronize(config(), read(answer(fake)))
        mine.refresh_from_db()
        self.assertEqual((mine.address, mine.website, mine.phone), ("Fake Street", "", ""))
        self.assertEqual(again["offers"][0]["values"], {"website": "https://scam.example/", "phone": "+39 800 111222"})

    def test_what_goes_into_the_file_survives_the_trip(self):
        mine = shop("Bar Aurora", osm_ref="node/1", lightning_worked=day(days=3))
        odd = node(1, "Bar \"A\" & <B> 'x'", **{"currency:XBT": "yes", TAG: 'x"y&z<w>;q', "note": "  spaced  ", "name:de": "Bär & Söhne"})
        element = read(answer(meta(odd, version=4)), meta=True)[0]
        found = osm.changes_for(mine, element, config(), TODAY)
        document = osm_edit.osm_change([osm_edit.Edit(mine, element, found)], 99)
        root = ET.fromstring(document)
        tags = {tag.get("k"): tag.get("v") for tag in root.findall("modify/node/tag")}
        self.assertEqual(tags["name"], "Bar \"A\" & <B> 'x'")
        self.assertEqual(tags["note"], "  spaced  ")
        self.assertEqual(tags["name:de"], "Bär & Söhne")
        self.assertEqual(tags[TAG], 'x"y&z<w>;q;operator.example')
        self.assertEqual(tags["payment:lightning"], "yes")
        self.assertEqual((root.find("modify/node").get("version"), root.find("modify/node").get("changeset")), ("4", "99"))
        self.assertTrue(document.startswith(b"<?xml"))

    def test_an_element_that_lost_a_tag_to_the_parser_is_not_a_deleted_one(self):
        shop("Bar Aurora", osm_ref="node/1")
        bad = node(1, "Bar Aurora", **{"currency:XBT": "yes", "note": "x\ny"})
        elements, skipped = osm.parse(answer(bad))
        report = osm.synchronize(config(), elements, skipped)
        self.assertEqual((report["missing"], skipped), ([], 0))
        self.assertEqual([item["ref"] for item in report["unreliable"]], ["node/1"])
        self.assertEqual(report["proposals"], [])


class Migrations(TransactionTestCase):
    """The shop's new fields come and go with the migrations, and the unique link holds in the database."""

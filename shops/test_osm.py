"""OpenStreetMap in both directions (shops/osm.py, osm_net.py, osm_edit.py, manage.py osm_sync and osm_upload): the settings,
the reading of an answer from strangers, which shop is which element, the empty fields filled and the full ones left alone,
the tags proposed and who wins when the dates differ, and an upload that sends nothing without a token and a typed
confirmation. Every answer is made up in the shape Overpass writes it; no test reaches a server."""
import json
import os
import socket
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import MagicMock, patch

import requests
from django.conf import settings
from django.core.management import CommandError, call_command
from django.db import IntegrityError, transaction
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from config import features

from . import osm, osm_net
from .models import OsmRequest, OsmRun, Shop
from .test_experimental import OsmUpload, on

CALLED_OUT = AssertionError("A test must not reach OpenStreetMap")
# What the name of a server resolves to in these tests: one public address (documentation range, never connected to)
PUBLIC = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443))]
AREA = [41.8, 12.4, 42.0, 12.7]
SETTINGS = {"bbox": AREA, "discount_tag": "currency:XBT:discount",
            "operator": {"tag": "payment:bitcoin:operator", "value": "operator.example"}}


def nobody_outside():
    """Any HTTP request or connection fails the test: the answers of Overpass and of the editing API are made up."""
    return (patch("requests.sessions.Session.request", side_effect=CALLED_OUT),
            patch.object(socket, "create_connection", side_effect=CALLED_OUT))


def config(**more):
    return osm.settings_from({**SETTINGS, **more})


def node(number, name=None, lat=41.9, lon=12.5, **tags):
    """An element as Overpass writes a node, with the coordinates as decimals do after parsing."""
    if name is not None:
        tags["name"] = name
    return {"type": "node", "id": number, "lat": lat, "lon": lon, "tags": tags}


def answer(*elements, **more):
    return {"version": 0.6, "generator": "Overpass API", "elements": list(elements), **more}


def read(data, meta=False):
    return osm.parse(osm.load(json.dumps(data)), meta)[0]


def meta(element, version=3):
    return {**element, "version": version}


def shop(name="Bar Aurora", **fields):
    values = {"latitude": 41.9, "longitude": 12.5, "lightning_address": "bar@example.org"}
    values.update(fields)
    return Shop.objects.create(name=name, **values)


def day(**kwargs):
    return timezone.now() - timedelta(**kwargs)


class Guard:
    """The base of the tests that run commands: no request leaves, whatever the code tries."""

    def setUp(self):
        super().setUp()
        for patcher in nobody_outside():
            patcher.start()
            self.addCleanup(patcher.stop)


class Settings(SimpleTestCase):
    def test_no_section_means_off(self):
        self.assertIsNone(osm.settings_from(None))
        self.assertIsNone(osm.settings_from(False))

    def test_the_area_is_all_it_needs(self):
        found = osm.settings_from({"bbox": AREA})
        self.assertEqual(found.bbox, (41.8, 12.4, 42.0, 12.7))
        self.assertEqual((found.similarity, found.metres, found.auto_link), (osm.DEFAULT_SIMILARITY, osm.DEFAULT_METRES, True))
        self.assertEqual((found.discount_tag, found.operator_tag), ("", ""))
        self.assertEqual(found.overpass_url, osm.DEFAULT_OVERPASS)

    def test_a_bad_area_turns_it_off_and_says_why(self):
        for bbox in (None, [1, 2, 3], [46, 11, 45, 12], [46, 12.7, 42.0, 11], ["a", 1, 2, 3], [0, 0, 60, 60], [True, 1, 2, 3], "46,11,42.0,12.7"):
            told = []
            self.assertIsNone(osm.settings_from({"bbox": bbox}, told.append), bbox)
            self.assertTrue(told and "bbox" in told[0], bbox)

    def test_the_rest_is_checked_field_by_field(self):
        told = []
        found = osm.settings_from({"bbox": AREA, "overpass_url": "http://overpass.example/api", "api_url": "https://localhost/api",
                                   "discount_tag": "not a tag!", "operator": {"tag": "x", "value": "a;b"},
                                   "name_similarity": 3, "distance_metres": "far", "auto_link": "yes",
                                   "comment": 5, "timeout": 100000}, told.append)
        self.assertEqual(len(told), 9, told)
        self.assertEqual((found.overpass_url, found.api_url), (osm.DEFAULT_OVERPASS, osm.DEFAULT_API))
        self.assertEqual((found.discount_tag, found.operator_tag, found.similarity, found.metres, found.auto_link),
                         ("", "", osm.DEFAULT_SIMILARITY, osm.DEFAULT_METRES, True))
        self.assertEqual(found.seconds, osm.DEFAULT_SECONDS)

    def test_what_is_set_is_used(self):
        found = osm.settings_from({**SETTINGS, "name_similarity": 0.9, "distance_metres": 50, "auto_link": False,
                                   "comment": {"en": "Bitcoin tags", "it": "Tag bitcoin"}, "timeout": 30, "max_megabytes": 2,
                                   "api_url": "https://master.apis.dev.openstreetmap.org/api/0.6/"})
        self.assertEqual((found.similarity, found.metres, found.auto_link, found.seconds, found.max_bytes),
                         (0.9, 50, False, 30, 2 * 2**20))
        self.assertEqual(found.api_url, "https://master.apis.dev.openstreetmap.org/api/0.6")
        # The community's line, and where the data come from: the Lightning addresses the site checks, not a visit
        self.assertEqual(osm.changeset_comment(found, "it"), f"Tag bitcoin. Data from the Lightning addresses checked by {settings.SITE_HOST}.")
        self.assertEqual(osm.changeset_comment(found, "de"), f"Bitcoin tags. Data from the Lightning addresses checked by {settings.SITE_HOST}.")
        long = osm.Settings(bbox=(41.8, 12.4, 42.0, 12.7), comment="x" * 400)
        self.assertEqual(len(osm.changeset_comment(long)), osm.COMMENT_LIMIT)
        self.assertTrue(osm.changeset_comment(long).endswith(f"checked by {settings.SITE_HOST}."))
        told = []
        self.assertEqual(osm.settings_from({"bbox": AREA, "comment": "x" * 151}, told.append).comment, osm.DEFAULT_COMMENT)
        self.assertTrue(told)
        self.assertEqual((found.operator_tag, found.operator_value), ("payment:bitcoin:operator", "operator.example"))

    def test_manage_check_says_what_cannot_be_used(self):
        from website.apps import wrong_osm_settings
        with patch("config.community.community", return_value={"osm": {"bbox": AREA, "auto_link": "yes", "discount_tag": "no tag"}}):
            found = wrong_osm_settings(None)
        self.assertEqual([check.id for check in found], ["website.W012", "website.W012"])
        with patch("config.community.community", return_value={"osm": {"bbox": "here"}}):
            self.assertEqual([check.id for check in wrong_osm_settings(None)], ["website.W012"])
        for data in ({}, {"osm": SETTINGS}, {"osm": False}):
            with patch("config.community.community", return_value=data):
                self.assertEqual(wrong_osm_settings(None), [])

    def test_the_example_community_is_off(self):
        self.assertIsNone(osm.settings())


class Reading(SimpleTestCase):
    def test_an_answer_as_overpass_writes_it(self):
        way = {"type": "way", "id": 7, "center": {"lat": 41.92, "lon": 12.52}, "tags": {"name": "Hub", "currency:XBT": "yes"}}
        found, skipped = osm.parse(osm.load(json.dumps(answer(node(1, "Bar", **{"currency:XBT": "yes"}), way))))
        self.assertEqual((skipped, [element.ref for element in found]), (0, ["node/1", "way/7"]))
        self.assertEqual(found[0].position, (41.9, 12.5))
        self.assertEqual(found[1].position, (41.92, 12.52))
        self.assertEqual(found[0].text_lat, "41.9")

    def test_coordinates_keep_the_digits_they_came_with(self):
        found = osm.parse(osm.load('{"elements": [{"type": "node", "id": 1, "lat": 41.8647594, "lon": 12.5504520, "tags": {"a": "b"}}]}'))[0]
        self.assertEqual((found[0].text_lat, found[0].text_lon), ("41.8647594", "12.5504520"))

    def test_a_remark_is_a_partial_answer_and_is_refused(self):
        with self.assertRaisesMessage(osm.OsmError, "remark"):
            osm.parse(answer(node(1, "Bar", a="b"), remark="runtime error: Query timed out in \"query\" at line 4 after 61 seconds."))

    def test_not_what_overpass_writes(self):
        for body in ("", "[1, 2]", '"text"', "null", '{"elements": 3}', '{"elements": {"a": 1}}', "{", '{"elements": [NaN]}',
                     '{"elements": [{"lat": Infinity}]}', "[" * 100000, '{"a":' * 100000):
            with self.assertRaises(osm.OsmError, msg=body[:20]):
                osm.parse(osm.load(body))

    def test_too_many_elements_is_a_wrong_area(self):
        many = {"elements": [node(number, a="b") for number in range(1, osm.MAX_ELEMENTS + 2)]}
        with self.assertRaisesMessage(osm.OsmError, "too big"):
            osm.parse(many)

    def test_an_element_with_no_type_or_id_is_left_out_and_counted(self):
        bad = [node(0, a="b"), node(-1, a="b"), node(True, a="b"), node("5", a="b"), node(10**16, a="b"),
               {"type": "area", "id": 1, "lat": 1, "lon": 1, "tags": {"a": "b"}}, "text", 5, None, [1], {"id": 7}]
        good = node(99, "Bar", a="b")
        found, skipped = osm.parse(answer(*bad, good, good))
        self.assertEqual([element.ref for element in found], ["node/99"])
        self.assertEqual(skipped, len(bad) + 1)
        self.assertFalse(found[0].unreliable)

    def test_a_bad_tag_costs_the_tag_not_the_element(self):
        elements = [node(8, "Bar", **{"currency:XBT": "yes", "note": "x" * 256}), node(9, "Bar", **{"currency:XBT": "yes", "a\nb": "x"}),
                    node(10, "Bar", **{"currency:XBT": "yes", "a": "x\u0000y"}), node(11, "Bar", **{"currency:XBT": "yes", "a": 5}),
                    node(12, "Bar", **{"currency:XBT": "yes", "note": "x\u2028y"})]
        found, skipped = osm.parse(answer(*elements))
        self.assertEqual((skipped, [element.ref for element in found]), (0, [f"node/{number}" for number in range(8, 13)]))
        for element in found:
            self.assertEqual(element.tags, {"name": "Bar", "currency:XBT": "yes"})
            self.assertTrue(element.unreliable)
        # Never given back to the server (osmChange would take the dropped tag away), never matched: but it exists
        self.assertEqual(osm.fillable(Shop(name="Bar", osm_ref="node/8"), found[0], config()), {})

    def test_other_bad_parts_keep_the_element_and_mark_it(self):
        for broken in (node(3, lat=91, a="b"), node(4, lon=-181, a="b"), node(5, lat="46", a="b"), node(6, lat=True, a="b"),
                       node(7), {"type": "node", "id": 2, "tags": {"a": "b"}}, {"type": "node", "id": 12, "lat": 1, "lon": 1, "tags": []},
                       {"type": "node", "id": 13, "lat": 1, "lon": 1, "tags": {str(n): "v" for n in range(osm.MAX_TAGS + 1)}}):
            found, skipped = osm.parse(answer(broken))
            self.assertEqual((skipped, len(found), found[0].unreliable), (0, 1, True), broken)
        self.assertEqual(len(found[0].tags), osm.MAX_TAGS)

    def test_a_way_without_a_centre_has_no_position_but_is_read(self):
        found = read(answer({"type": "way", "id": 3, "tags": {"name": "Hall", "currency:XBT": "yes"}}))
        self.assertIsNone(found[0].position)
        self.assertFalse(found[0].unreliable)
        broken = osm.parse(answer({"type": "way", "id": 3, "center": {"lat": 100, "lon": 1}, "tags": {"a": "b"}}))[0]
        self.assertEqual((len(broken), broken[0].position, broken[0].unreliable), (1, None, True))

    def test_what_an_edit_needs_is_there_or_the_element_is_marked(self):
        good_way = {"type": "way", "id": 3, "version": 2, "nodes": [1, 2], "center": {"lat": 1, "lon": 1}, "tags": {"a": "b"}}
        relation = {"type": "relation", "id": 4, "version": 1, "members": [{"type": "way", "ref": 3, "role": "outer"}],
                    "center": {"lat": 1, "lon": 1}, "tags": {"a": "b"}}
        found = osm.parse(answer(meta(node(1, a="b")), good_way, relation), meta=True)[0]
        self.assertEqual([(element.ref, element.version, element.unreliable) for element in found],
                         [("node/1", 3, False), ("way/3", 2, False), ("relation/4", 1, False)])
        self.assertEqual(found[2].members, [{"type": "way", "ref": 3, "role": "outer"}])
        for broken in (node(1, a="b"), {**good_way, "nodes": []}, {**good_way, "nodes": "1,2"}, {**good_way, "nodes": [1, "x"]},
                       {**good_way, "version": 0}, {**relation, "members": []}, {**relation, "members": [{"type": "x", "ref": 1}]},
                       {**relation, "members": [{"type": "way", "ref": True}]}):
            found = osm.parse(answer(broken), meta=True)[0]
            self.assertEqual((len(found), found[0].unreliable), (1, True), broken)

    def test_a_number_the_arithmetic_cannot_hold_is_not_a_crash(self):
        for exponent in ("1e-999999999", "1e-20000000", "-1e-400", "0.00000000000000000000000000000000000001"):
            body = '{"elements": [{"type": "node", "id": 1, "lat": %s, "lon": 1, "tags": {"a": "b"}}]}' % exponent
            found, skipped = osm.parse(osm.load(body))
            # Zero, written with nine decimals: not a gigabyte of zeros
            self.assertEqual((skipped, found[0].text_lat, found[0].lat), (0, "0.000000000", 0.0), exponent)
        found = osm.parse(osm.load('{"elements": [{"type": "node", "id": 1, "lat": 1e999999999, "lon": 1, "tags": {"a": "b"}}]}'))[0]
        self.assertEqual((found[0].unreliable, found[0].position), (True, None))
        # Many digits are rounded to nine decimals, not written out
        found = osm.parse(osm.load('{"elements": [{"type": "node", "id": 1, "lat": 1.' + "1" * 5000 + ', "lon": 1, "tags": {"a": "b"}}]}'))[0]
        self.assertEqual(found[0].text_lat, "1.111111111")
        self.assertEqual(osm.coordinate(Decimal("41.86475946"), 90)[1], "41.86475946")
        self.assertEqual(osm.coordinate(Decimal("41.8647594612"), 90)[1], "41.864759461")
        with patch("shops.osm.one_element", side_effect=MemoryError), self.assertRaises(osm.OsmError):
            osm.parse(answer(node(1, a="b")))
        with patch("shops.osm.one_element", side_effect=ArithmeticError), self.assertRaises(osm.OsmError):
            osm.parse(answer(node(1, a="b")))
        with patch("shops.osm.json.loads", side_effect=ArithmeticError), self.assertRaises(osm.OsmError):
            osm.load("{}")


class Names(SimpleTestCase):
    def test_alike_names(self):
        for left, right in (("Bar Aurora", "bar aurora"), ("Café Aurora", "CAFE AURORA"), ("Bar Aurora", "Bar Aurora."),
                            ("Central Bar", "Central"), ("Pizzeria of Mario", "Pizzeria Of Mario!"),
                            ("Gasthaus Straße", "Gasthaus Strasse")):
            self.assertGreaterEqual(osm.similarity(left, right), 0.85, (left, right))

    def test_names_that_are_not(self):
        for left, right in (("Bar Aurora", "Corner Bar"), ("Bar Aurora", "Pizzeria Aurora North"), ("Bar", "Bar Sport"), ("", "Bar"),
                            ("Gelato Luna", "Locksmith")):
            self.assertLess(osm.similarity(left, right), 0.85, (left, right))

    def test_dates_and_percents(self):
        self.assertEqual(osm.parse_day("2025-06-27"), date(2025, 6, 27))
        self.assertEqual(osm.parse_day("2025-06"), date(2025, 6, 1))
        self.assertEqual(osm.parse_day("2025"), date(2025, 1, 1))
        for text in ("", None, "yesterday", "2025-13-01", "2025-02-30", "27/06/2025"):
            self.assertIsNone(osm.parse_day(text), text)
        self.assertEqual([osm.percent(text) for text in ("10%", "10 %", "10", "7,5%")], [10, 10, 10, osm.percent("7.5")])
        self.assertIsNone(osm.percent("ten"))


class Matching(TestCase):
    def test_a_close_place_with_a_name_alike_is_safe(self):
        mine = shop("Bar Aurora")
        found = osm.match([mine], read(answer(node(1, "Bar Aurora", lat=41.9003, lon=12.5))), config())
        self.assertEqual([(pair[0], pair[1].ref) for pair in found.safe], [(mine, "node/1")])
        self.assertLess(found.safe[0][3], 100)

    def test_two_elements_that_fit_are_ambiguous_and_nothing_is_linked(self):
        shop("Bar Aurora")
        found = osm.match(list(Shop.objects.all()), read(answer(node(1, "Bar Aurora"), node(2, "Bar Aurora", lat=41.9004))), config())
        self.assertEqual((found.safe, len(found.ambiguous)), ([], 1))
        self.assertEqual({pair[1].ref for pair in found.ambiguous[0]}, {"node/1", "node/2"})

    def test_two_shops_for_one_element_are_ambiguous_too(self):
        shop("Bar Aurora"), shop("Bar Aurora ")
        found = osm.match(list(Shop.objects.all()), read(answer(node(1, "Bar Aurora"))), config())
        self.assertEqual((found.safe, len(found.ambiguous)), ([], 2))

    def test_a_name_alike_but_far_is_for_a_person(self):
        shop("Bar Aurora")
        found = osm.match(list(Shop.objects.all()), read(answer(node(1, "Bar Aurora", lat=41.9030))), config())
        self.assertEqual((found.safe, found.ambiguous, [(pair[1].ref) for pair in found.unsure]), ([], [], ["node/1"]))
        self.assertGreater(found.unsure[0][3], 100)

    def test_very_far_or_another_name_is_nothing(self):
        shop("Bar Aurora")
        elements = read(answer(node(1, "Bar Aurora", lat=41.95), node(2, "Pizzeria Corner", lat=41.9001)))
        found = osm.match(list(Shop.objects.all()), elements, config())
        self.assertEqual((found.safe, found.ambiguous, found.unsure), ([], [], []))

    def test_a_shop_without_a_position_is_never_safe(self):
        shop("Bar Aurora", latitude=None, longitude=None)
        found = osm.match(list(Shop.objects.all()), read(answer(node(1, "Bar Aurora"))), config())
        self.assertEqual((found.safe, [(pair[1].ref, pair[3]) for pair in found.unsure]), ([], [("node/1", None)]))

    def test_the_thresholds_are_the_communitys(self):
        shop("Bar Aurora")
        elements = read(answer(node(1, "Bar Aurora", lat=41.9010)))
        self.assertEqual(len(osm.match(list(Shop.objects.all()), elements, config(distance_metres=200)).safe), 1)
        self.assertEqual(len(osm.match(list(Shop.objects.all()), elements, config(distance_metres=50)).safe), 0)
        near = read(answer(node(1, "Bar Auroro")))
        self.assertEqual(len(osm.match(list(Shop.objects.all()), near, config(name_similarity=0.8)).safe), 1)
        self.assertEqual(len(osm.match(list(Shop.objects.all()), near, config(name_similarity=0.95)).safe), 0)

    def test_another_name_of_the_element_counts(self):
        shop("Bar Aurora")
        found = osm.match(list(Shop.objects.all()), read(answer(node(1, "Café del Sole", alt_name="Bar Aurora;Sole"))), config())
        self.assertEqual(len(found.safe), 1)


class Filling(TestCase):
    """What a confirmed link lets the reading write: the address, the town and the position inside the area, by itself; the
    website and the phone only as offers. The values written are kept, and nothing is filled twice."""

    def element(self, **tags):
        return read(answer(node(1, "Bar Aurora", lat=42.0, lon=12.6, **tags)))[0]

    def test_address_town_and_position_are_written_and_remembered(self):
        mine = shop(latitude=None, longitude=None)
        written, offered = osm.fill(mine, self.element(**{"addr:street": "Main Street", "addr:housenumber": "12", "addr:city": "Example",
                                                          "website": "https://bar.example/", "phone": "+39 0461 123456"}), config())
        self.assertEqual(written, {"address": "Main Street 12", "city": "Example", "position": [42.0, 12.6]})
        self.assertEqual(offered, {"website": "https://bar.example/", "phone": "+39 0461 123456"})
        mine.refresh_from_db()
        self.assertEqual((mine.address, mine.city, mine.latitude, mine.longitude), ("Main Street 12", "Example", 42.0, 12.6))
        # The website and the phone are not written, only offered and remembered
        self.assertEqual((mine.website, mine.phone), ("", ""))
        self.assertEqual(osm.offers_of(mine), {"website": "https://bar.example/", "phone": "+39 0461 123456"})
        self.assertEqual(osm.notes_of(mine)["fields"]["address"], {"value": "Main Street 12", "state": "filled"})

    def test_a_field_with_something_in_it_is_not_touched(self):
        mine = shop(address="Square 1", city="Town", website="https://mine.example/", phone="12345", latitude=41.9, longitude=12.5)
        before = Shop.objects.values().get(pk=mine.pk)
        written, offered = osm.fill(mine, self.element(**{"addr:street": "Main Street", "addr:city": "Example", "website": "https://bar.example/",
                                                          "phone": "+39 0461 123456"}), config())
        self.assertEqual((written, offered), ({}, {}))
        self.assertEqual(Shop.objects.values().get(pk=mine.pk), before)

    def test_the_position_is_whole_and_inside_the_area_or_nothing(self):
        mine = shop(latitude=41.9, longitude=None)
        self.assertEqual(osm.fill(mine, self.element(), config()), ({}, {}))
        mine.refresh_from_db()
        self.assertEqual((mine.latitude, mine.longitude), (41.9, None))
        outside = shop("Bar Outside", latitude=None, longitude=None)
        elsewhere = read(answer(node(2, "Bar Outside", lat=47.5, lon=12.6)))[0]
        self.assertEqual(osm.fill(outside, elsewhere, config()), ({}, {}))
        outside.refresh_from_db()
        self.assertEqual((outside.latitude, outside.longitude), (None, None))
        inside = read(answer(node(3, "Bar Inside", lat=41.8, lon=12.7)))[0]
        self.assertEqual(osm.fill(outside, inside, config())[0], {"position": [41.8, 12.7]})

    def test_what_somebody_writes_meanwhile_stays(self):
        stale = shop(address="")
        Shop.objects.filter(pk=stale.pk).update(address="Written by the owner a moment ago")
        self.assertEqual(osm.fill(stale, self.element(**{"addr:street": "Main Street"}), config())[0], {})
        stale.refresh_from_db()
        self.assertEqual(stale.address, "Written by the owner a moment ago")
        self.assertNotIn("address", osm.notes_of(stale)["fields"])

    def test_a_field_the_owner_emptied_on_purpose_is_not_filled_again(self):
        mine = shop(address="")
        element = self.element(**{"addr:street": "Main Street", "addr:city": "Example"})
        self.assertEqual(osm.fill(mine, element, config())[0], {"address": "Main Street", "city": "Example"})
        Shop.objects.filter(pk=mine.pk).update(address="", city="")
        mine.refresh_from_db()
        self.assertEqual(osm.fill(mine, element, config()), ({}, {}))
        mine.refresh_from_db()
        self.assertEqual((mine.address, mine.city), ("", ""))

    def test_an_offer_is_made_once_changed_when_the_value_changes_and_not_after_a_no(self):
        mine = shop()
        first = self.element(website="https://bar.example/")
        self.assertEqual(osm.fill(mine, first, config())[1], {"website": "https://bar.example/"})
        self.assertEqual(osm.fill(mine, first, config()), ({}, {}))
        second = self.element(website="https://new.example/")
        self.assertEqual(osm.fill(mine, second, config())[1], {"website": "https://new.example/"})
        self.assertEqual(osm.offers_of(mine), {"website": "https://new.example/"})
        self.assertEqual(osm.decline_contacts(mine), {"website": "https://new.example/"})
        self.assertEqual(osm.offers_of(mine), {})
        self.assertEqual(osm.fill(mine, second, config()), ({}, {}))
        # A different value after a no is offered again
        self.assertEqual(osm.fill(mine, first, config())[1], {"website": "https://bar.example/"})

    def test_an_offer_accepted_is_written_to_an_empty_field_and_never_offered_again(self):
        mine = shop()
        element = self.element(website="https://bar.example/", phone="+39 0461 123456")
        osm.fill(mine, element, config())
        mine.refresh_from_db()
        Shop.objects.filter(pk=mine.pk).update(phone="0461 999")
        mine.refresh_from_db()
        self.assertEqual(osm.accept_contacts(mine), {"website": "https://bar.example/"})
        mine.refresh_from_db()
        self.assertEqual((mine.website, mine.phone), ("https://bar.example/", "0461 999"))
        self.assertEqual(osm.offers_of(mine), {})
        Shop.objects.filter(pk=mine.pk).update(website="")
        mine.refresh_from_db()
        self.assertEqual(osm.fill(mine, element, config()), ({}, {}))

    def test_nothing_unfit_goes_in(self):
        mine = shop(latitude=None, longitude=None)
        element = self.element(**{"website": "javascript:alert(1)", "contact:website": "ftp://files.example/", "phone": "call me maybe",
                                  "contact:phone": "<script>", "addr:city": "C" * 150, "addr:street": "S" * 250})
        self.assertEqual(sorted(osm.fillable(mine, element, config())), ["position"])
        self.assertEqual(osm.website_of({"contact:website": "https://ok.example/x?a=b"}), "https://ok.example/x?a=b")
        self.assertEqual(osm.website_of({"website": "https://a.example/;https://b.example/"}), "https://a.example/")
        self.assertEqual(osm.website_of({"website": "https://" + "a" * 300 + ".example"}), "")
        self.assertEqual(osm.phone_of({"phone": "+39 0461 1;+39 333"}), "+39 0461 1")
        self.assertEqual(osm.address_of({"addr:place": "Upper Hamlet"}), "Upper Hamlet")
        self.assertEqual(osm.address_of({"addr:housenumber": "3"}), "")

    def test_an_email_is_not_taken(self):
        mine = shop()
        self.assertNotIn("email", osm.fillable(mine, self.element(email="owner@example.org", **{"contact:email": "x@example.org"}), config()))

    def test_a_link_is_proposed_then_confirmed_by_the_association(self):
        first, second = shop("Bar Aurora"), shop("Bar Aurora Two")
        element = read(answer(node(1, "Bar Aurora")))[0]
        self.assertTrue(osm.propose_link(first, element))
        self.assertFalse(osm.propose_link(first, element))
        first.refresh_from_db()
        self.assertEqual((first.osm_ref, first.osm_proposed), ("", "node/1"))
        self.assertTrue(osm.confirm_link(first))
        first.refresh_from_db()
        self.assertEqual((first.osm_ref, first.osm_proposed), ("node/1", ""))
        self.assertFalse(osm.confirm_link(first))
        # Another shop cannot confirm the same element
        Shop.objects.filter(pk=second.pk).update(osm_proposed="node/1")
        second.refresh_from_db()
        self.assertFalse(osm.confirm_link(second))
        self.assertEqual(Shop.objects.get(pk=second.pk).osm_ref, "")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Shop.objects.filter(pk=second.pk).update(osm_ref="node/1")

    def test_a_link_refused_is_not_proposed_again(self):
        mine = shop("Bar Aurora")
        element = read(answer(node(1, "Bar Aurora")))[0]
        osm.propose_link(mine, element)
        mine.refresh_from_db()
        self.assertTrue(osm.reject_link(mine))
        mine.refresh_from_db()
        self.assertEqual((mine.osm_proposed, osm.notes_of(mine)["rejected"]), ("", ["node/1"]))
        self.assertFalse(osm.propose_link(mine, element))
        self.assertFalse(osm.reject_link(mine))


TODAY = date(2026, 11, 5)


class Proposals(TestCase):
    """What OpenStreetMap should say, and who wins when the two differ."""

    def changes(self, mine, element, **more):
        return {(tag, old, new, kind) for tag, old, new, kind in osm.changes_for(mine, read(answer(element))[0], config(**more), TODAY)}

    def test_a_shop_missing_everything_gets_the_tags_added(self):
        mine = shop(discount=10, lightning_worked=day(days=3))
        paid = timezone.localtime(timezone.now() - timedelta(days=3)).date().isoformat()
        found = self.changes(mine, node(1, "Bar Aurora"))
        self.assertEqual(found, {("currency:XBT", None, "yes", "add"), ("payment:lightning", None, "yes", "add"),
                                 ("currency:XBT:discount", None, "10%", "add"),
                                 ("check_date:currency:XBT", None, paid, "add"),
                                 ("payment:bitcoin:operator", None, "operator.example", "add")})

    def test_the_way_of_getting_paid_gives_the_tags(self):
        for address, tags in (("bar@example.org", {"payment:lightning"}), ("", set())):
            mine = Shop.objects.create(name=f"Bar {address}", lightning_address=address)
            found = {tag for tag, *_ in self.changes(mine, node(1, "Bar"))}
            self.assertEqual(found & {"payment:onchain", "payment:lightning"}, tags, address)

    def test_a_shop_that_is_right_has_nothing_to_change(self):
        mine = shop(discount=10, lightning_worked=day(days=3))
        paid = timezone.localtime(timezone.now() - timedelta(days=3)).date().isoformat()
        element = node(1, "Bar Aurora", **{"currency:XBT": "yes", "payment:lightning": "yes", "currency:XBT:discount": "10 %",
                                           "check_date:currency:XBT": paid, "payment:bitcoin:operator": "other.example;operator.example"})
        self.assertEqual(self.changes(mine, element), set())

    def test_the_dates_decide_our_check_after_their_check(self):
        mine = shop(lightning_worked=day(days=3))
        paid = timezone.localtime(timezone.now() - timedelta(days=3)).date().isoformat()
        element = node(1, "Bar Aurora", **{"currency:XBT": "yes", "payment:lightning": "yes", "check_date:currency:XBT": "2024-10-12"})
        self.assertEqual(self.changes(mine, element, operator=None), {("check_date:currency:XBT", "2024-10-12", paid, "update")})

    def test_the_dates_decide_their_check_after_our_check_is_left_alone(self):
        mine = shop(lightning_worked=day(days=300))
        # The community's day of that moment, as the site counts it: the UTC one differs across a change of summer time
        newer = timezone.localtime(day(days=20)).date().isoformat()
        element = node(1, "Bar Aurora", **{"currency:XBT": "yes", "payment:lightning": "yes", "check_date:currency:XBT": newer})
        self.assertEqual(self.changes(mine, element, operator=None), set())
        # and the same day is not "newer"
        same = shop("Bar Same", lightning_worked=day(days=20))
        self.assertEqual(self.changes(same, node(2, "Bar Same", **{"currency:XBT": "yes", "payment:lightning": "yes", "check_date:currency:XBT": newer}),
                                      operator=None), set())

    def test_an_address_that_never_worked_proposes_no_date(self):
        mine = shop()
        found = {tag for tag, *_ in self.changes(mine, node(1, "Bar Aurora"))}
        self.assertNotIn("check_date:currency:XBT", found)

    def test_a_no_there_with_our_address_working_after_their_check_is_for_a_person(self):
        mine = shop(lightning_worked=day(days=3))
        element = node(1, "Bar Aurora", **{"currency:XBT": "no", "payment:lightning": "no", "check_date:currency:XBT": "2025-06-27"})
        found = self.changes(mine, element, operator=None)
        paid = timezone.localtime(timezone.now() - timedelta(days=3)).date().isoformat()
        self.assertEqual(found, {("currency:XBT", "no", "yes", "review"), ("payment:lightning", "no", "yes", "review"),
                                 ("check_date:currency:XBT", "2025-06-27", paid, "review")})

    def test_a_no_there_checked_after_our_address_last_worked_is_not_changed(self):
        mine = shop(lightning_worked=day(days=300))
        element = node(1, "Bar Aurora", **{"currency:XBT": "no", "payment:lightning": "no", "check_date:currency:XBT": "2026-09-01"})
        self.assertEqual(self.changes(mine, element, operator=None), set())

    def test_a_shop_that_closed(self):
        mine = shop(closed_on=date(2026, 3, 31), lightning_worked=day(days=300))
        element = node(1, "Bar Aurora", **{"currency:XBT": "yes", "payment:lightning": "yes", "payment:onchain": "no",
                                           "currency:XBT:discount": "10%", "check_date:currency:XBT": "2025-06-27",
                                           "payment:bitcoin:operator": "operator.example"})
        self.assertEqual(self.changes(mine, element), {
            ("currency:XBT", "yes", "no", "update"), ("payment:lightning", "yes", "no", "update"),
            ("check_date:currency:XBT", "2025-06-27", "2026-03-31", "update"), ("currency:XBT:discount", "10%", None, "update")})

    def test_a_closed_shop_is_never_given_our_operator(self):
        mine = shop(closed_on=date(2026, 3, 31))
        found = self.changes(mine, node(1, "Bar Aurora", **{"currency:XBT": "yes"}))
        self.assertNotIn("payment:bitcoin:operator", {tag for tag, *_ in found})
        self.assertEqual(self.changes(mine, node(2, "Bar Aurora", **{"currency:XBT": "no"})), set())

    def test_a_closing_date_still_to_come_is_a_shop_that_takes_bitcoin(self):
        mine = shop(closed_on=date(2026, 12, 31))
        found = self.changes(mine, node(1, "Bar Aurora"))
        self.assertIn(("currency:XBT", None, "yes", "add"), found)

    def test_closed_after_their_check_is_theirs_to_say(self):
        mine = shop(closed_on=date(2026, 3, 31))
        element = node(1, "Bar Aurora", **{"currency:XBT": "yes", "check_date:currency:XBT": "2026-08-01"})
        self.assertEqual(self.changes(mine, element), set())

    def test_the_discount(self):
        mine = shop(discount=15, lightning_worked=day(days=3))
        base = {"currency:XBT": "yes", "payment:lightning": "yes", "check_date:currency:XBT": timezone.localtime(timezone.now() - timedelta(days=3)).date().isoformat()}
        self.assertEqual(self.changes(mine, node(1, "Bar", **base, **{"currency:XBT:discount": "10%"}), operator=None),
                         {("currency:XBT:discount", "10%", "15%", "update")})
        self.assertEqual(self.changes(mine, node(2, "Bar", **base, **{"currency:XBT:discount": "lots"}), operator=None),
                         {("currency:XBT:discount", "lots", "15%", "update")})
        # No discount here and one there: could be a discount nobody told us about, or one that ended: a person decides
        none = shop("Bar None", discount=0, lightning_worked=day(days=3))
        self.assertEqual(self.changes(none, node(3, "Bar None", **base, **{"currency:XBT:discount": "10%"}), operator=None),
                         {("currency:XBT:discount", "10%", None, "review")})
        # The community without a discount tag proposes nothing about it
        self.assertEqual(self.changes(mine, node(4, "Bar", **base, **{"currency:XBT:discount": "10%"}), operator=None, discount_tag=None), set())

    def test_the_operator(self):
        mine = shop(lightning_worked=day(days=3))
        base = {"currency:XBT": "yes", "payment:lightning": "yes", "check_date:currency:XBT": timezone.localtime(timezone.now() - timedelta(days=3)).date().isoformat()}
        tag = "payment:bitcoin:operator"
        self.assertEqual(self.changes(mine, node(1, "Bar", **base)), {(tag, None, "operator.example", "add")})
        # Ours next to the others, theirs kept: the normal case, not a matter for a person
        self.assertEqual(self.changes(mine, node(2, "Bar", **base, **{tag: "old.example"})), {(tag, "old.example", "old.example;operator.example", "update")})
        self.assertEqual(self.changes(mine, node(3, "Bar", **base, **{tag: "old.example;other.example"})),
                         {(tag, "old.example;other.example", "old.example;other.example;operator.example", "update")})
        self.assertEqual(self.changes(mine, node(4, "Bar", **base, **{tag: "operator.example"})), set())
        self.assertEqual(self.changes(mine, node(5, "Bar", **base, **{tag: "old.example; operator.example"})), set())
        self.assertEqual(self.changes(mine, node(6, "Bar", **base), operator=None), set())

    def test_shops_that_say_nothing_get_no_proposal(self):
        element = node(1, "Bar Aurora")
        for fields in ({"suspended": True}, {"listed": False}, {"awaiting_wallet": True, "lightning_address": ""},
                       {"external_id": "bitmentor-demo:x"}):
            self.assertEqual(self.changes(shop(f"Bar {fields}", **fields), element), set(), fields)

    def test_the_tags_that_can_change_are_those_of_the_settings(self):
        mine = shop(discount=10, lightning_worked=day(days=3))
        for element in (node(1, "Bar"), node(2, "Bar", **{"currency:XBT": "no", "payment:lightning": "no", "payment:onchain": "no"})):
            tags = {tag for tag, *_ in osm.changes_for(mine, read(answer(element))[0], config(), TODAY)}
            self.assertLessEqual(tags, config().tags)
        self.assertEqual(config(discount_tag=None, operator=None).tags,
                         {"currency:XBT", "payment:bitcoin", "payment:lightning", "payment:onchain", "check_date:currency:XBT"})

    def test_the_tags_of_a_new_entry(self):
        mine = shop(discount=5, lightning_worked=day(days=1), website="https://bar.example/", city="Example", phone="123456")
        wanted = osm.wanted_tags(mine, config(), (timezone.now()).date())
        self.assertEqual(list(wanted)[:3], ["name", "currency:XBT", "payment:lightning"])
        self.assertEqual((wanted["currency:XBT:discount"], wanted["payment:bitcoin:operator"], wanted["addr:city"], wanted["website"]),
                         ("5%", "operator.example", "Example", "https://bar.example/"))
        self.assertEqual(osm.wanted_tags(shop("Bar Closed", closed_on=date(2020, 1, 1)), config(), TODAY), {})

    def test_a_hidden_phone_or_website_is_not_written_on_the_map(self):
        """What the owner keeps hidden on the site (shops/details.py) is not proposed for OpenStreetMap, a public map."""
        mine = shop(lightning_worked=day(days=1), website="https://bar.example/", phone="123456",
                    show_website=False, show_phone=False)
        wanted = osm.wanted_tags(mine, config(), TODAY)
        self.assertNotIn("phone", wanted)
        self.assertNotIn("website", wanted)
        Shop.objects.filter(pk=mine.pk).update(show_website=True, show_phone=True)
        mine.refresh_from_db()
        wanted = osm.wanted_tags(mine, config(), TODAY)
        self.assertEqual((wanted["phone"], wanted["website"]), ("123456", "https://bar.example/"))


def fake_overpass(*elements, **more):
    return json.dumps(answer(*elements, **more)).encode()


class Report(Guard, TestCase):
    """One whole reading, with shops in the database."""

    def run_it(self, *elements, previous=None, apply=True, **more):
        found, skipped = osm.parse(osm.load(fake_overpass(*elements)))
        return osm.synchronize(config(**more), found, skipped, apply=apply, previous=previous)

    def test_a_reading_proposes_a_link_and_fills_only_confirmed_ones(self):
        known = shop("Bar Aurora", address="", lightning_worked=day(days=2))
        linked = shop("Gelato Luna", osm_ref="node/2", website="https://mine.example/", address="", latitude=None, longitude=None)
        lost = shop("Bakery Gone", osm_ref="node/404")
        outside = shop("Pizzeria Alone")
        report = self.run_it(
            node(1, "Bar Aurora", lat=41.9002, **{"currency:XBT": "yes", "addr:street": "Main Street", "payment:lightning": "yes",
                                                  "website": "https://scam.example/", "phone": "+39 800 111222"}),
            node(2, "Gelato Luna", lat=41.95, lon=12.55, **{"currency:XBT": "no", "phone": "+39 111 2222", "website": "https://theirs.example/",
                                                          "addr:street": "New Street", "addr:housenumber": "3"}),
            node(3, "Locksmith Rossi", lat=41.99, **{"currency:XBT": "yes", "check_date:currency:XBT": "2025-01-01"}),
            node(4, "Hotel Stella", lat=41.99, **{"payment:lightning": "yes"}))
        known.refresh_from_db(), linked.refresh_from_db()
        # A match is only a proposal: nothing is written for it, and nothing is filled, until the association confirms it
        self.assertEqual((known.osm_ref, known.osm_proposed), ("", "node/1"))
        self.assertEqual((known.address, known.website, known.phone, known.osm_seen), ("", "", "", None))
        self.assertEqual([item["name"] for item in report["link_proposals"]], ["Bar Aurora"])
        self.assertEqual([item["ref"] for item in report["candidates"]], ["node/3"])
        # A confirmed link: the address and the position by themselves, written down; the contacts only offered
        self.assertEqual((linked.address, linked.latitude, linked.longitude), ("New Street 3", 41.95, 12.55))
        self.assertEqual((linked.website, linked.phone), ("https://mine.example/", ""))
        self.assertEqual(osm.offers_of(linked), {"phone": "+39 111 2222"})
        self.assertEqual({item["name"]: item["values"] for item in report["filled"]},
                         {"Gelato Luna": {"address": "New Street 3", "position": [41.95, 12.55]}})
        self.assertEqual({item["name"]: item["values"] for item in report["offers"]}, {"Gelato Luna": {"phone": "+39 111 2222"}})
        self.assertEqual([item["name"] for item in report["missing"]], ["Bakery Gone"])
        self.assertEqual(report["not_on_osm"], 3)
        self.assertEqual(Shop.objects.get(pk=outside.pk).osm_ref, "")
        self.assertEqual(Shop.objects.get(pk=lost.pk).osm_ref, "node/404")
        # The next reading: the proposal waits (it is not proposed twice, nor is the element offered to another shop)
        again = self.run_it(node(1, "Bar Aurora", lat=41.9002, **{"currency:XBT": "yes"}), node(2, "Gelato Luna", **{"currency:XBT": "no"}))
        self.assertEqual((again["link_proposals"], again["waiting_links"]), ([], 1))
        # The association confirms it: from the next reading, its element is read and compared
        osm.confirm_link(Shop.objects.get(pk=known.pk))
        third = self.run_it(node(1, "Bar Aurora", lat=41.9002, **{"currency:XBT": "yes", "addr:street": "Main Street"}))
        self.assertEqual({item["name"] for item in third["filled"]}, {"Bar Aurora"})
        proposed = {item["name"]: {change["tag"] for change in item["changes"]} for item in third["proposals"]}
        self.assertIn("check_date:currency:XBT", proposed["Bar Aurora"])

    def test_a_proposal_whose_element_is_gone_is_cleared(self):
        mine = shop("Bar Aurora")
        Shop.objects.filter(pk=mine.pk).update(osm_proposed="node/1")
        self.run_it(node(5, "Another Place", **{"currency:XBT": "yes"}))
        self.assertEqual(Shop.objects.get(pk=mine.pk).osm_proposed, "")

    def test_candidates_are_a_list_to_know_nothing_is_proposed_for_them(self):
        report = self.run_it(node(1, "Place One", **{"currency:XBT": "yes"}), node(2, "Closed One", **{"currency:XBT": "no"}))
        self.assertEqual(([item["ref"] for item in report["candidates"]], report["proposals"], report["osm_says_no"]), (["node/1"], [], []))
        self.assertEqual(Shop.objects.count(), 0)

    def test_a_linked_element_that_was_not_read_whole_is_not_deleted_nor_proposed_for(self):
        shop("Bar Aurora", osm_ref="node/1", lightning_worked=day(days=3))
        report = self.run_it(node(1, "Bar Aurora", **{"currency:XBT": "yes", "note": "x" * 300}))
        self.assertEqual((report["missing"], report["proposals"], report["unreliable_elements"]), ([], [], 1))
        self.assertEqual([item["name"] for item in report["unreliable"]], ["Bar Aurora"])

    def test_doubts_are_in_the_report_and_not_in_the_shops(self):
        shop("Bar Aurora"), shop("Pizzeria Far", latitude=41.9)
        report = self.run_it(node(1, "Bar Aurora"), node(2, "Bar Aurora", lat=41.9003), node(3, "Pizzeria Far", lat=41.9040, **{"currency:XBT": "yes"}))
        self.assertEqual(Shop.objects.exclude(osm_ref="").count(), 0)
        self.assertEqual([item["name"] for item in report["ambiguous"]], ["Bar Aurora"])
        self.assertEqual([(item["name"], item["why"]) for item in report["unsure"]], [("Pizzeria Far", "far")])
        # The elements in doubt are not offered as new places
        self.assertEqual([item["ref"] for item in report["candidates"]], ["node/3"])

    def test_without_apply_nothing_is_written(self):
        known = shop("Bar Aurora", address="")
        before = Shop.objects.values().get(pk=known.pk)
        report = self.run_it(node(1, "Bar Aurora", **{"addr:street": "Main Street", "currency:XBT": "yes"}), apply=False)
        self.assertEqual(Shop.objects.values().get(pk=known.pk), before)
        self.assertEqual([item["name"] for item in report["suggested"]], ["Bar Aurora"])
        self.assertEqual(report["link_proposals"], [])

    def test_auto_link_off_only_suggests(self):
        known = shop("Bar Aurora", address="")
        report = self.run_it(node(1, "Bar Aurora", **{"addr:street": "Main Street", "currency:XBT": "yes"}), auto_link=False)
        self.assertEqual((Shop.objects.get(pk=known.pk).osm_ref, Shop.objects.get(pk=known.pk).address), ("", ""))
        self.assertEqual(len(report["suggested"]), 1)

    def test_what_openstreetmap_says_about_the_dates(self):
        shop("Bar Old", osm_ref="node/1", lightning_worked=day(days=400))
        shop("Bar Said No", osm_ref="node/2", lightning_worked=day(days=10))
        shop("Bar Never Paid", osm_ref="node/3")
        shop("Bar Recent", osm_ref="node/4", lightning_worked=day(days=5))
        report = self.run_it(
            node(1, "Bar Old", **{"currency:XBT": "yes", "check_date:currency:XBT": "2026-09-01"}),
            node(2, "Bar Said No", **{"currency:XBT": "no", "check_date:currency:XBT": "2026-09-01"}),
            node(3, "Bar Never Paid", **{"currency:XBT": "yes", "check_date:currency:XBT": "2026-09-01"}),
            node(4, "Bar Recent", **{"currency:XBT": "yes", "check_date:currency:XBT": "2020-01-01"}))
        self.assertEqual(sorted(item["name"] for item in report["newer_on_osm"]), ["Bar Never Paid", "Bar Old"])
        self.assertEqual([item["name"] for item in report["osm_says_no"]], ["Bar Said No"])
        self.assertEqual({item["state"] for item in report["newer_on_osm"]}, {"yes"})
        # the shop whose address worked more recently gets its date refreshed
        recent = next(item for item in report["proposals"] if item["name"] == "Bar Recent")
        self.assertIn("check_date:currency:XBT", {change["tag"] for change in recent["changes"]})
        self.assertNotIn("Bar Old", [item["name"] for item in report["proposals"] if "check_date:currency:XBT" in {c["tag"] for c in item["changes"]}])

    def test_candidates_are_new_once(self):
        first = self.run_it(node(1, "Place One", **{"currency:XBT": "yes"}), node(2, "Place Two", **{"currency:XBT": "no"}))
        self.assertEqual(([item["ref"] for item in first["candidates"]], first["new_candidates"]), (["node/1"], []))
        second = self.run_it(node(1, "Place One", **{"currency:XBT": "yes"}), node(3, "Place Three", **{"currency:XBT": "yes"}), previous=first)
        self.assertEqual(second["new_candidates"], ["node/3"])
        self.assertEqual(osm.counts(second)["candidates"], 1)

    def test_made_up_shops_are_not_read(self):
        shop("Bar Aurora", external_id="bitmentor-demo:aurora")
        report = self.run_it(node(1, "Bar Aurora", **{"currency:XBT": "yes"}))
        self.assertEqual((report["link_proposals"], [item["ref"] for item in report["candidates"]]), ([], ["node/1"]))

    def test_the_good_readings_are_kept_twelve_deep(self):
        for number in range(osm.KEEP_RUNS + 3):
            osm.save_run({"proposals": [], "number": number})
        self.assertEqual(OsmRun.objects.count(), osm.KEEP_RUNS)
        self.assertEqual(osm.last_run().report["number"], osm.KEEP_RUNS + 2)
        osm.save_run(error="Overpass is down")
        self.assertEqual((osm.last_run(ok=False).ok, osm.last_run().report["number"]), (False, osm.KEEP_RUNS + 2))

    def test_failures_never_push_out_the_last_good_reading(self):
        good = osm.save_run({"created": timezone.now().isoformat(), "elements": 80, "proposals": []})
        for number in range(osm.KEEP_RUNS + 3):
            osm.save_run(error=f"the server answered 50{number}")
        self.assertEqual(osm.last_run().pk, good.pk)
        self.assertEqual(osm.previous_run().pk, good.pk)
        self.assertEqual((OsmRun.objects.filter(ok=True).count(), OsmRun.objects.filter(ok=False).count()), (1, osm.KEEP_FAILED))

    def test_a_failure_like_the_one_before_is_not_written_again(self):
        first = osm.save_run(error="the server answered 504")
        self.assertEqual(osm.save_run(error="the server answered 504").pk, first.pk)
        self.assertEqual(OsmRun.objects.count(), 1)
        osm.save_run(error="the answer is not JSON")
        osm.save_run(error="the server answered 504")
        self.assertEqual(OsmRun.objects.count(), 3)
        # After a good reading the same failure is news again
        osm.save_run({"proposals": []})
        osm.save_run(error="the server answered 504")
        self.assertEqual(OsmRun.objects.count(), 5)

    @on(features.OSM_UPLOAD)
    def test_the_summary_for_the_moderators(self):
        shop("Bar Aurora", osm_ref="node/1", lightning_worked=day(days=3), discount=10)
        report = self.run_it(node(1, "Bar Aurora", **{"currency:XBT": "yes"}), node(2, "Evil @everyone http://phish.example", **{"currency:XBT": "yes"}),
                             node(3, "Gone Bar", **{"currency:XBT": "no"}), previous={"candidates": []})
        subject, text = osm.summary(report, None, "https://site.example/admin/shops/osmrun/")
        self.assertIn("changes proposed", subject)
        self.assertIn("nothing is sent until a person runs the upload", text)
        self.assertNotIn("@everyone", text)
        self.assertNotIn("phish.example", text)
        self.assertIn("https://site.example/admin/shops/osmrun/", text)
        self.assertTrue(osm.worth_telling(report))
        self.assertFalse(osm.worth_telling({}))
        self.assertIn("(osm_upload sends them, by hand)", "\n".join(osm.report_lines(report)))

    def test_with_the_upload_off_the_summary_and_the_report_promise_none(self):
        """Writing to OpenStreetMap is an experimental feature, off unless the community turns it on: the changes are
        still listed, as what OpenStreetMap should have, for a person to make by hand."""
        shop("Bar Aurora", osm_ref="node/1", lightning_worked=day(days=3), discount=10)
        report = self.run_it(node(1, "Bar Aurora", **{"currency:XBT": "yes"}))
        subject, text = osm.summary(report, None, "https://site.example/admin/shops/osmrun/")
        self.assertIn("changes proposed", subject)
        self.assertIn("the site sends nothing, a person can make them by hand on openstreetmap.org", text)
        self.assertNotIn("upload", text)
        lines = "\n".join(osm.report_lines(report))
        self.assertIn("Changes proposed for OpenStreetMap (the site sends nothing: for a person to make by hand", lines)
        self.assertNotIn("osm_upload", lines)
        # The proposals themselves are the same, with the switch or without
        self.assertIn("payment:lightning: (missing) -> yes", lines)

    def test_the_summary_says_what_is_new_and_only_when_something_is(self):
        shop("Bar Aurora", osm_ref="node/1", lightning_worked=day(days=3), discount=10)
        shop("Bar Said No", osm_ref="node/2", lightning_worked=day(days=1))
        elements = [node(1, "Bar Aurora", **{"currency:XBT": "yes"}), node(2, "Bar Said No", **{"currency:XBT": "no", "check_date:currency:XBT": "2020-01-01"})]
        first = self.run_it(*elements)
        self.assertTrue(osm.worth_telling(first, None))
        # The same month after month is not news: it waits on the dashboard
        second = self.run_it(*elements, previous=first)
        self.assertFalse(osm.worth_telling(second, first))
        subject, text = osm.summary(second, first)
        self.assertNotIn("Bar Said No", text)
        # A new proposal, a new place, a shop newly said to take no bitcoin are
        Shop.objects.filter(name="Bar Aurora").update(discount=15)
        self.assertTrue(osm.worth_telling(self.run_it(*elements, previous=first), first))
        Shop.objects.filter(name="Bar Aurora").update(discount=10)
        extra = [*elements, node(9, "Newcomer", **{"currency:XBT": "yes"})]
        third = self.run_it(*extra, previous=first)
        self.assertTrue(osm.worth_telling(third, first))
        self.assertIn("Newcomer", osm.summary(third, first)[1])

    def test_the_summary_stays_under_what_telegram_takes(self):
        long = "x" * 100
        crowd = [{"shop": str(number), "name": f"Shop {number} {long}", "ref": f"node/{number}", "values": {"address": long, "city": long},
                  "ours": "", "theirs": "2020-01-01", "state": "yes", "osm_name": long, "why": "far"} for number in range(40)]
        report = {"proposals": [{"shop": str(number), "name": f"Shop {number}", "ref": f"node/{number}",
                                 "changes": [{"tag": "a", "old": None, "new": "b", "kind": "add"}]} for number in range(40)],
                  "link_proposals": crowd, "filled": crowd, "offers": crowd, "osm_says_no": crowd, "newer_on_osm": crowd, "missing": crowd,
                  "unreliable": crowd, "candidates": [{"ref": f"node/{1000 + number}", "name": long, "city": "", "date": ""} for number in range(40)],
                  "new_candidates": [f"node/{1000 + number}" for number in range(40)], "candidates_total": 40,
                  "ambiguous": [], "unsure": [], "suggested": []}
        text = osm.summary(report, None, "https://site.example/admin/")[1]
        self.assertLessEqual(len(text), osm.MESSAGE_LIMIT)
        self.assertLess(osm.MESSAGE_LIMIT, 4096)
        # The end, the link to the page that says the rest, is never the part that is cut
        self.assertTrue(text.endswith("https://site.example/admin/"))
        self.assertIn("…", text)
        lines = [f"line {number} " + "y" * 80 for number in range(100)] + ["the last line"]
        fitted = osm.fit(lines)
        self.assertLessEqual(len(fitted), osm.MESSAGE_LIMIT)
        self.assertTrue(fitted.endswith("the last line"))
        self.assertEqual(osm.fit(["short", "text"]), "short\ntext")


class Requests(SimpleTestCase):
    """The requests: the same protections as the other servers the site does not control."""

    def streamed(self, status=200, body=b"{}", headers=None):
        response = MagicMock()
        response.__enter__.return_value = response
        response.status_code = status
        response.headers = headers or {}
        response.iter_content.return_value = [body[index:index + 8192] for index in range(0, len(body), 8192)] or [b""]
        return response

    def go(self, url="https://overpass.example/api/interpreter?data=x", **more):
        return osm_net.request("GET", url, seconds=10, size=1000, **more)

    def test_only_public_https(self):
        for url in ("http://overpass.example/x", "ftp://overpass.example/x", "https://overpass.example:8443/x", "https://127.0.0.1/x",
                    "https://10.0.0.5/x", "https://[::1]/x", "https://printer.local/x", "https://x.onion/x", "file:///etc/passwd", "https:///x"):
            with patch("requests.sessions.Session.request", side_effect=CALLED_OUT), self.assertRaises(osm.OsmError, msg=url):
                self.go(url)

    def test_an_answer_within_the_limits(self):
        with patch("requests.sessions.Session.request", return_value=self.streamed(body=b'{"elements": []}')) as asked, \
                patch("shops.osm_net.public", return_value=PUBLIC):
            self.assertEqual(self.go(), b'{"elements": []}')
        self.assertEqual(asked.call_args.kwargs["allow_redirects"], False)
        self.assertTrue(asked.call_args.kwargs["headers"]["User-Agent"].startswith("bitmentor/"))
        self.assertIn("https://", asked.call_args.kwargs["headers"]["User-Agent"])

    def test_too_big_too_slow_redirected_refused_unreachable(self):
        cases = {"big": self.streamed(body=b"x" * 5000), "redirect": self.streamed(status=302), "busy": self.streamed(status=429),
                 "down": self.streamed(status=504), "refused": self.streamed(status=403, headers={"Error": "Not allowed\nx" * 30})}
        for name, response in cases.items():
            with patch("requests.sessions.Session.request", return_value=response), patch("shops.osm_net.public", return_value=PUBLIC), \
                    self.assertRaises(osm.OsmError, msg=name) as caught:
                self.go()
            self.assertNotIn("\n", str(caught.exception))
            self.assertLess(len(str(caught.exception)), 200)
        for error in (requests.ConnectionError("https://secret.example/?token=abc"), requests.Timeout(), OSError("boom")):
            with patch("requests.sessions.Session.request", side_effect=error), patch("shops.osm_net.public", return_value=PUBLIC), \
                    self.assertRaises(osm.OsmError) as caught:
                self.go()
            self.assertNotIn("secret", str(caught.exception))
        with patch("requests.sessions.Session.request", return_value=self.streamed(body=b"x" * 10)), \
                patch("shops.osm_net.public", return_value=PUBLIC), \
                patch("shops.osm_net.time", MagicMock(monotonic=MagicMock(side_effect=[0, 0, 99]))), \
                self.assertRaisesMessage(osm.OsmError, "too long"):
            self.go()

    def test_the_connection_goes_to_the_addresses_checked(self):
        # The name is asked once, within the request's time; urllib3 connects to those addresses and does not ask again
        with patch("requests.sessions.Session.request", return_value=self.streamed(body=b"{}")), \
                patch("shops.osm_net.public", return_value=PUBLIC) as asked, \
                patch("shops.osm_net.Watchdog", wraps=osm_net.Watchdog) as watchdog:
            self.go()
        self.assertEqual(asked.call_args.args, ("overpass.example", 10))
        self.assertEqual(watchdog.call_args.kwargs["pinned"], PUBLIC)
        self.assertLessEqual(watchdog.call_args.args[0], 10)

    def test_a_name_without_an_answer_in_time(self):
        with patch("requests.sessions.Session.request", side_effect=CALLED_OUT), \
                patch("shops.osm_net.resolve", side_effect=osm_net.SlowName("slow")), \
                self.assertRaisesMessage(osm.OsmError, "did not resolve in time"):
            self.go()

    def test_no_time_no_request(self):
        with patch("requests.sessions.Session.request", side_effect=CALLED_OUT), patch("shops.osm_net.public", return_value=PUBLIC), \
                self.assertRaises(osm.OsmError):
            osm_net.request("GET", "https://overpass.example/x", seconds=0, size=10)

    def test_the_query_is_the_area_and_the_bitcoin_tags(self):
        query = osm_net.overpass_query(config(timeout=45))
        self.assertTrue(query.startswith("[out:json][timeout:45];("))
        self.assertEqual(query.count("(41.800000,12.400000,42.000000,12.700000)"), 4)
        for key in osm.BITCOIN_KEYS:
            self.assertIn(f'nwr["{key}"]', query)
        self.assertTrue(query.endswith("out body center;"))
        self.assertTrue(osm_net.overpass_query(config(), meta=True).endswith("out meta center;"))

    def test_the_reading_asks_with_get_and_the_communitys_limits(self):
        with patch("shops.osm_net.request", return_value=b"{}") as asked:
            osm_net.read_area(config(timeout=40, max_megabytes=2, overpass_url="https://overpass.example/api/interpreter"))
        self.assertEqual(asked.call_args.args[0], "GET")
        self.assertTrue(asked.call_args.args[1].startswith("https://overpass.example/api/interpreter?data="))
        self.assertEqual((asked.call_args.kwargs["seconds"], asked.call_args.kwargs["size"]), (50, 2 * 2**20))

    def test_the_token_goes_in_a_header_and_nowhere_else(self):
        with patch("shops.osm_net.request", return_value=b"1") as asked:
            osm_net.api(config(), "PUT", "changeset/create", "secret-token", b"<osm/>")
        url = asked.call_args.args[1]
        self.assertEqual(url, "https://api.openstreetmap.org/api/0.6/changeset/create")
        self.assertNotIn("secret-token", url)
        self.assertEqual(asked.call_args.kwargs["headers"]["Authorization"], "Bearer secret-token")


class Sync(Guard, TestCase):
    """manage.py osm_sync, with Overpass answering what the test says."""

    def sync(self, body=None, *args, error=None, community=SETTINGS):
        out, err = StringIO(), StringIO()
        # The reading asks Overpass and nothing else: whatever tries to write to OpenStreetMap fails the test
        with patch("shops.osm.community", return_value={"osm": community}), \
                patch("shops.osm_net.read_area", side_effect=error, return_value=body), \
                patch("shops.osm_net.api", side_effect=CALLED_OUT):
            call_command("osm_sync", *args, stdout=out, stderr=err)
        return out.getvalue()

    def test_it_is_off_without_the_section(self):
        text = self.sync(community=None)
        self.assertIn("OpenStreetMap is off", text)
        self.assertEqual(OsmRun.objects.count(), 0)

    def test_a_run_proposes_a_link_and_keeps_the_report(self):
        known = shop("Bar Aurora", address="")
        text = self.sync(fake_overpass(node(1, "Bar Aurora", **{"currency:XBT": "yes", "addr:street": "Main Street"})))
        self.assertIn("Links proposed now: 1", text)
        known.refresh_from_db()
        self.assertEqual((known.osm_ref, known.osm_proposed, known.address), ("", "node/1", ""))
        run = OsmRun.objects.get()
        self.assertTrue(run.ok)
        self.assertEqual(run.report["link_proposals"][0]["ref"], "node/1")
        # A reading made by hand, without --send, told nobody: the next summary does not take it as the last one told
        self.assertTrue(run.report["silent"])
        self.assertIsNone(osm.previous_run())

    def test_a_dry_run_writes_nothing(self):
        known = shop("Bar Aurora", address="")
        self.sync(fake_overpass(node(1, "Bar Aurora", **{"currency:XBT": "yes", "addr:street": "Main Street"})), "--dry-run")
        known.refresh_from_db()
        self.assertEqual((known.osm_ref, known.address, OsmRun.objects.count()), ("", "", 0))

    def test_overpass_that_fails_changes_nothing_and_says_so(self):
        known = shop("Bar Aurora", osm_ref="node/1", address="")
        for kwargs in ({"error": osm.OsmError("the server answered 504")},
                       {"body": fake_overpass(node(1, "Bar Aurora", a="b"), remark="runtime error: Query timed out")},
                       {"body": b"<html>Bad gateway</html>"}, {"body": b'{"elements": [' + b"[" * 50000},
                       {"body": fake_overpass(*[node(number, a="b") for number in range(1, osm.MAX_ELEMENTS + 2)])}):
            with self.assertRaises(CommandError, msg=kwargs) as caught:
                self.sync(**kwargs)
            self.assertIn("Nothing was changed", str(caught.exception))
        self.assertEqual(Shop.objects.get(pk=known.pk).address, "")
        # Two of the five failures are the same line, "the answer is not JSON": written once
        self.assertEqual(OsmRun.objects.filter(ok=False).count(), 4)
        self.assertEqual(OsmRun.objects.filter(ok=True).count(), 0)

    def test_an_answer_with_far_fewer_elements_is_not_trusted(self):
        known = shop("Bar Aurora", osm_ref="node/1")
        many = [node(number, f"Place {number}", **{"currency:XBT": "yes"}) for number in range(1, 21)]
        self.sync(fake_overpass(*many))
        with self.assertRaisesMessage(CommandError, "not trusted"):
            self.sync(fake_overpass())
        with self.assertRaisesMessage(CommandError, "only 3 elements against 20"):
            self.sync(fake_overpass(*many[:3]))
        self.assertEqual(Shop.objects.get(pk=known.pk).osm_ref, "node/1")
        self.assertEqual(osm.last_run(ok=False).ok, False)
        self.assertEqual(osm.last_run().report["elements"], 20)
        # More than half is a normal month; and the person who knows can accept less
        self.sync(fake_overpass(*many[:11]))
        self.sync(fake_overpass(*many[:2]), "--accept-fewer")
        self.assertEqual(osm.last_run().report["elements"], 2)

    def test_a_small_area_may_come_back_empty(self):
        self.sync(fake_overpass(node(1, "Place", **{"currency:XBT": "yes"})))
        self.sync(fake_overpass())
        self.assertEqual(osm.last_run().report["elements"], 0)

    def test_the_summary_goes_to_the_moderators_only_when_there_is_something(self):
        shop("Bar Aurora", osm_ref="node/1", lightning_address="")
        quiet = fake_overpass(node(1, "Bar Aurora", **{"currency:XBT": "yes", "payment:bitcoin:operator": "operator.example"}))
        with override_settings(TELEGRAM_BOT_TOKEN="t", TELEGRAM_MODERATION_CHAT="-100"), \
                patch("shops.notifications.telegram", return_value=True) as chat:
            self.assertIn("no message", self.sync(quiet, "--send"))
            self.assertEqual(chat.call_count, 0)
        with override_settings(TELEGRAM_BOT_TOKEN="t", TELEGRAM_MODERATION_CHAT="-100"), \
                patch("shops.notifications.telegram", return_value=True) as chat:
            with self.assertRaises(SystemExit) as stop:
                self.sync(fake_overpass(node(1, "Bar Aurora", **{"currency:XBT": "yes"}), node(2, "Newcomer", **{"currency:XBT": "yes"})), "--send")
        self.assertEqual(stop.exception.code, 10)
        self.assertEqual(chat.call_args.args[0], "-100")
        self.assertIn("Newcomer", chat.call_args.args[1])
        self.assertIn("/admin/shops/osmrun/", chat.call_args.args[1])

    def test_the_same_summary_is_not_sent_twice(self):
        body = fake_overpass(node(2, "Newcomer", **{"currency:XBT": "yes"}))
        with override_settings(TELEGRAM_BOT_TOKEN="t", TELEGRAM_MODERATION_CHAT="-100"), \
                patch("shops.notifications.telegram", return_value=True) as chat:
            with self.assertRaises(SystemExit):
                self.sync(body, "--send")
            self.assertIn("no message", self.sync(body, "--send"))
            self.assertEqual(chat.call_count, 1)

    def test_a_summary_that_did_not_go_is_told_again_next_time(self):
        shop("Bar Aurora", osm_ref="node/1")
        body = fake_overpass(node(1, "Bar Aurora", **{"currency:XBT": "yes"}), node(2, "Newcomer", **{"currency:XBT": "yes"}))
        self.sync(fake_overpass(node(1, "Bar Aurora", **{"currency:XBT": "yes"})))
        with override_settings(TELEGRAM_BOT_TOKEN="", EMAIL_HOST="", TELEGRAM_MODERATION_CHAT=""), \
                patch("shops.notifications.community", create=True), patch("config.community.community", return_value={"name": "X"}):
            with self.assertRaisesMessage(CommandError, "not delivered"):
                self.sync(body, "--send")
        self.assertTrue(OsmRun.objects.first().report["undelivered"])
        with override_settings(TELEGRAM_BOT_TOKEN="t", TELEGRAM_MODERATION_CHAT="-100"), \
                patch("shops.notifications.telegram", return_value=True) as chat, self.assertRaises(SystemExit):
            self.sync(body, "--send")
        self.assertIn("Newcomer", chat.call_args.args[1])

    def test_it_falls_back_to_the_community_email(self):
        body = fake_overpass(node(2, "Newcomer", **{"currency:XBT": "yes"}))
        self.sync(fake_overpass())
        with override_settings(TELEGRAM_BOT_TOKEN="t", TELEGRAM_MODERATION_CHAT="-100"), \
                patch("shops.notifications.telegram", return_value=False), \
                patch("config.community.community", return_value={"email": "info@example.org"}), \
                patch("shops.notifications.send_mail") as mail, self.assertRaises(SystemExit):
            self.sync(body, "--send")
        self.assertEqual(mail.call_args.args[3], ["info@example.org"])


def edit_setup(case):
    case.mine = shop("Bar Aurora", osm_ref="node/1", lightning_worked=day(days=3))
    case.paid = timezone.localtime(timezone.now() - timedelta(days=3)).date().isoformat()
    case.body = fake_overpass(meta(node(1, "Bar Aurora", **{"currency:XBT": "yes", "addr:street": "Main Street", "cuisine": "bar",
                                                           "check_date:currency:XBT": "2024-01-01"}), version=7))


class UploadOff(Guard, TestCase):
    """manage.py osm_upload in a community that has not turned it on (`experimental: {osm_upload: true}`): it says so and
    ends cleanly, token or not."""

    def setUp(self):
        super().setUp()
        edit_setup(self)

    def run_it(self, *args, token=""):
        out, err = StringIO(), StringIO()
        with override_settings(OSM_TOKEN=token), patch("shops.osm.community", return_value={"osm": SETTINGS}), \
                patch("shops.osm_net.read_area", side_effect=CALLED_OUT) as read_area, \
                patch("shops.osm_net.api", side_effect=CALLED_OUT) as api, \
                patch("shops.management.commands.osm_upload.ask", side_effect=CALLED_OUT) as asked:
            # No CommandError, no SystemExit: the exit code is 0
            call_command("osm_upload", *args, stdout=out, stderr=err)
        self.assertEqual((read_area.call_count, api.call_count, asked.call_count), (0, 0, 0))
        return out.getvalue(), err.getvalue()

    def test_it_says_it_is_off_and_how_to_turn_it_on_and_sends_nothing(self):
        for token in ("", "secret-token"):
            with self.subTest(token=bool(token)):
                text, err = self.run_it(token=token)
                self.assertIn("experimental feature, and it is off", text)
                self.assertIn("`osm_upload: true` in an `experimental` block of community.yml", text)
                self.assertIn("nothing was sent", text)
                self.assertEqual(err, "")
                self.assertNotIn("secret-token", text)

    def test_it_writes_no_file_and_keeps_the_standard_output_clean(self):
        path = f"/tmp/osm-test-off-{self.mine.pk}.osc"
        self.run_it("--osc", path)
        self.assertFalse(os.path.exists(path))
        # With `--osc -` the standard output is for the file: the sentence goes to the other one
        text, err = self.run_it("--osc", "-")
        self.assertEqual(text, "")
        self.assertIn("experimental feature, and it is off", err)

    def test_it_is_off_before_anything_else_is_looked_at(self):
        # No `osm` section, a limit out of range: still the same clean end, not an error
        with patch("shops.osm.community", return_value={}):
            out = StringIO()
            call_command("osm_upload", "--limit", "5000", stdout=out, stderr=StringIO())
        self.assertIn("it is off", out.getvalue())
        self.assertEqual(OsmRun.objects.filter(uploaded__isnull=False).count(), 0)


class Upload(OsmUpload, Guard, TestCase):
    """manage.py osm_upload: nothing leaves without the token and the typed word."""

    def setUp(self):
        super().setUp()
        edit_setup(self)
        self.calls = []

    def api(self, config, method, path, token, data=None):
        self.calls.append((method, path, token, data))
        return {"changeset/create": b"4242\n"}.get(path, b"<diffResult/>")

    def upload(self, *args, token="", typed="upload", tty=True, body=None, api=None):
        out, err = StringIO(), StringIO()
        with override_settings(OSM_TOKEN=token), patch("shops.osm.community", return_value={"osm": SETTINGS}), \
                patch("shops.osm_net.read_area", return_value=body or self.body), \
                patch("shops.osm_net.api", side_effect=api or self.api), \
                patch("shops.management.commands.osm_upload.interactive", return_value=tty), \
                patch("shops.management.commands.osm_upload.ask", return_value=typed) as asked:
            try:
                call_command("osm_upload", *args, stdout=out, stderr=err)
            finally:
                self.asked = asked
        return out.getvalue(), err.getvalue()

    def test_without_a_token_it_writes_a_file_and_links_and_sends_nothing(self):
        path = f"/tmp/osm-test-{self.mine.pk}.osc"
        text, _err = self.upload("--osc", path)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.asked.call_count, 0)
        self.assertIn("No OSM_TOKEN: nothing was sent", text)
        self.assertIn("https://www.openstreetmap.org/edit?editor=id&node=1", text)
        self.assertIn("payment:lightning: (missing) -> yes", text)
        with open(path, "rb") as handle:
            document = handle.read()
        root = ET.fromstring(document)
        self.assertEqual(root.tag, "osmChange")
        node_element = root.find("modify/node")
        self.assertEqual((node_element.get("id"), node_element.get("version"), node_element.get("lat"), node_element.get("lon")),
                         ("1", "7", "41.9", "12.5"))
        self.assertIsNone(node_element.get("changeset"))
        os.remove(path)

    def test_the_file_can_go_to_standard_output(self):
        text, err = self.upload("--osc", "-")
        self.assertTrue(text.startswith("<?xml"))
        self.assertIn("osmChange", text)
        self.assertIn("changes in 1 elements", err)

    def test_only_the_bitcoin_tags_change_and_the_rest_is_given_back_whole(self):
        text, _err = self.upload("--osc", "-")
        tags = {tag.get("k"): tag.get("v") for tag in ET.fromstring(text.encode()).findall("modify/node/tag")}
        before = {"name": "Bar Aurora", "currency:XBT": "yes", "addr:street": "Main Street", "cuisine": "bar", "check_date:currency:XBT": "2024-01-01"}
        changed = {key for key in set(tags) | set(before) if tags.get(key) != before.get(key)}
        self.assertEqual(changed, {"payment:lightning", "check_date:currency:XBT", "payment:bitcoin:operator"})
        self.assertLessEqual(changed, config().tags)
        self.assertEqual((tags["name"], tags["cuisine"], tags["addr:street"]), ("Bar Aurora", "bar", "Main Street"))
        self.assertEqual(tags["check_date:currency:XBT"], self.paid)

    def test_a_way_is_given_back_with_its_nodes_and_a_relation_with_its_members(self):
        shop("Hall", osm_ref="way/9", lightning_worked=day(days=3), latitude=None, longitude=None)
        shop("Park", osm_ref="relation/8", lightning_worked=day(days=3), latitude=None, longitude=None)
        way = {"type": "way", "id": 9, "version": 2, "nodes": [11, 12, 13], "center": {"lat": 41.9, "lon": 12.5}, "tags": {"name": "Hall", "currency:XBT": "yes"}}
        relation = {"type": "relation", "id": 8, "version": 5, "members": [{"type": "way", "ref": 9, "role": "outer"}],
                    "center": {"lat": 41.9, "lon": 12.5}, "tags": {"name": "Park", "currency:XBT": "yes"}}
        text, _err = self.upload("--osc", "-", body=fake_overpass(way, relation))
        root = ET.fromstring(text.encode())
        self.assertEqual([nd.get("ref") for nd in root.findall("modify/way/nd")], ["11", "12", "13"])
        self.assertEqual([(m.get("type"), m.get("ref"), m.get("role")) for m in root.findall("modify/relation/member")], [("way", "9", "outer")])
        self.assertIsNone(root.find("modify/way").get("lat"))

    def test_with_a_token_it_asks_and_a_wrong_answer_sends_nothing(self):
        for typed in ("", "yes", "UPLOAD now", "no"):
            text, _err = self.upload(token="secret-token", typed=typed)
            self.assertIn("Nothing was sent.", text)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.asked.call_count, 1)

    def test_without_a_terminal_it_does_not_even_ask(self):
        with self.assertRaisesMessage(CommandError, "terminal"):
            self.upload(token="secret-token", tty=False)
        self.assertEqual((self.calls, self.asked.call_count), ([], 0))

    def test_the_upload_opens_a_changeset_sends_and_closes_it(self):
        OsmRequest.objects.create(shop=self.mine, kind="update")
        OsmRequest.objects.create(shop=shop("Other"), kind="add")
        text, err = self.upload(token="secret-token")
        self.assertEqual([(method, path) for method, path, _token, _data in self.calls],
                         [("PUT", "changeset/create"), ("POST", "changeset/4242/upload"), ("PUT", "changeset/4242/close")])
        self.assertEqual({token for _m, _p, token, _d in self.calls}, {"secret-token"})
        created = ET.fromstring(self.calls[0][3])
        tags = {tag.get("k"): tag.get("v") for tag in created.findall("changeset/tag")}
        self.assertEqual((tags["created_by"], tags["source"]), ("bitmentor", settings.SITE_HOST))
        self.assertEqual(tags["comment"], f"Update bitcoin payment tags. Data from the Lightning addresses checked by {settings.SITE_HOST}.")
        sent = ET.fromstring(self.calls[1][3])
        self.assertEqual(sent.find("modify/node").get("changeset"), "4242")
        self.assertIn("https://www.openstreetmap.org/changeset/4242", text)
        self.assertNotIn("secret-token", text + err)
        # The merchant who asked is told it is done; the request to be added is not an update
        self.assertEqual(OsmRequest.objects.get(shop=self.mine).state, "done")
        self.assertEqual(OsmRequest.objects.get(shop=self.mine).done_by, "osm_upload")
        self.assertEqual(OsmRequest.objects.exclude(shop=self.mine).get().state, "open")

    def test_what_was_sent_is_no_longer_proposed(self):
        self.mine.refresh_from_db()
        osm.save_run({"proposals": [{"shop": str(self.mine.pk), "name": "Bar Aurora", "ref": "node/1", "changes": [
            {"tag": "payment:lightning", "old": None, "new": "yes", "kind": "add"},
            {"tag": "payment:bitcoin:operator", "old": "old.example", "new": "old.example;operator.example", "kind": "review"}]}]})
        self.upload(token="secret-token")
        run = osm.last_run()
        self.assertEqual(run.changeset, "4242")
        self.assertIsNotNone(run.uploaded)
        self.assertEqual(osm.waiting(run), [])

    def test_a_failed_upload_closes_the_changeset_and_says_so(self):
        def api(config, method, path, token, data=None):
            self.calls.append((method, path))
            if path.endswith("/upload"):
                raise osm.OsmError("the server answered 409: Version mismatch")
            return b"4242"
        with self.assertRaisesMessage(CommandError, "closed"):
            self.upload(token="secret-token", api=api)
        self.assertEqual(self.calls[-1], ("PUT", "changeset/4242/close"))
        self.assertEqual(OsmRun.objects.count(), 0)

    def test_a_changeset_that_cannot_be_opened_sends_nothing(self):
        def api(config, method, path, token, data=None):
            self.calls.append((method, path))
            if path == "changeset/create":
                return b"<html>not a number</html>"
            raise AssertionError("nothing else may be asked")
        with self.assertRaisesMessage(CommandError, "not opened"):
            self.upload(token="secret-token", api=api)
        self.assertEqual(self.calls, [("PUT", "changeset/create")])

    def test_the_proposals_that_need_judgement_wait_for_the_flag(self):
        shop("Bar Said No", osm_ref="node/2", lightning_worked=day(days=3))
        body = fake_overpass(meta(node(2, "Bar Said No", **{"currency:XBT": "no", "check_date:currency:XBT": "2020-01-01"}), 4))
        # A "no" turned into a "yes" is read as a whole by a person: no tag that goes with it is sent alone
        _text, err = self.upload("--osc", "-", body=body)
        self.assertIn("Nothing to send", err)
        _text, err = self.upload("--osc", "-", "--with-review", body=body)
        self.assertIn("currency:XBT: no -> yes", err)
        self.assertIn("payment:lightning: (missing) -> yes", err)
        self.assertIn("[needs your judgement]", err)
        self.assertNotIn("Nothing to send", err)

    def test_a_shop_that_closed_is_sent_as_no_with_the_day_it_closed(self):
        Shop.objects.filter(pk=self.mine.pk).update(closed_on=date(2026, 3, 31), lightning_worked=None)
        text, err = self.upload("--osc", "-")
        tags = {tag.get("k"): tag.get("v") for tag in ET.fromstring(text.encode()).findall("modify/node/tag")}
        self.assertEqual((tags["currency:XBT"], tags["check_date:currency:XBT"]), ("no", "2026-03-31"))
        self.assertNotIn("payment:bitcoin:operator", tags)

    def test_a_check_there_that_is_more_recent_sends_nothing(self):
        body = fake_overpass(meta(node(1, "Bar Aurora", **{"currency:XBT": "yes", "payment:lightning": "yes", "payment:onchain": "yes",
                                                           "payment:bitcoin:operator": "operator.example",
                                                           "check_date:currency:XBT": "2099-01-01"}), 7))
        _text, err = self.upload("--osc", "-", body=body)
        self.assertIn("Nothing to send", err)

    def test_openstreetmap_that_does_not_answer_sends_nothing(self):
        for error in (osm.OsmError("the server answered 504"), ):
            with patch("shops.osm.community", return_value={"osm": SETTINGS}), patch("shops.osm_net.read_area", side_effect=error), \
                    override_settings(OSM_TOKEN="secret-token"), self.assertRaisesMessage(CommandError, "Nothing was sent"):
                call_command("osm_upload", stdout=StringIO(), stderr=StringIO())
        self.assertEqual(self.calls, [])

    def test_off_without_the_section_and_a_limit_in_range(self):
        with patch("shops.osm.community", return_value={}), self.assertRaisesMessage(CommandError, "is off"):
            call_command("osm_upload", stdout=StringIO(), stderr=StringIO())
        with patch("shops.osm.community", return_value={"osm": SETTINGS}), self.assertRaisesMessage(CommandError, "--limit"):
            call_command("osm_upload", "--limit", "5000", stdout=StringIO(), stderr=StringIO())

    def test_a_changeset_has_a_size_a_person_can_read(self):
        for number in range(2, 8):
            shop(f"Bar {number}", osm_ref=f"node/{number}", lightning_worked=day(days=3))
        body = fake_overpass(*[meta(node(number, f"Bar {number}", **{"currency:XBT": "yes"}), 1) for number in range(2, 8)])
        text, err = self.upload("--osc", "-", "--limit", "4", body=body)
        self.assertIn("2 more elements wait", err)
        self.assertEqual(len(ET.fromstring(text.encode()).findall("modify/node")), 4)

    def test_the_default_file_has_a_name_nobody_can_guess_and_is_for_its_owner_only(self):
        names = []
        for _ in range(2):
            text, _err = self.upload()
            path = text.split("The osmChange file is ", 1)[1].split(":", 1)[0]
            names.append(path)
            self.assertTrue(os.path.basename(path).startswith("osm-changes-") and path.endswith(".osc"))
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            self.assertEqual(ET.parse(path).getroot().tag, "osmChange")
            os.remove(path)
        self.assertNotEqual(names[0], names[1])
        self.assertNotIn(f"{timezone.now():%Y%m%d}.osc", names[0])

    def test_a_chosen_file_must_be_new_and_not_a_link(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            existing = os.path.join(folder, "there.osc")
            with open(existing, "w") as handle:
                handle.write("keep me")
            with self.assertRaisesMessage(CommandError, "exists already"):
                self.upload("--osc", existing)
            self.assertEqual(open(existing).read(), "keep me")
            link = os.path.join(folder, "link.osc")
            os.symlink(existing, link)
            with self.assertRaisesMessage(CommandError, "exists already"):
                self.upload("--osc", link)
            self.assertEqual(open(existing).read(), "keep me")
            dangling = os.path.join(folder, "dangling.osc")
            os.symlink(os.path.join(folder, "elsewhere"), dangling)
            with self.assertRaises(CommandError):
                self.upload("--osc", dangling)
            self.assertFalse(os.path.exists(os.path.join(folder, "elsewhere")))
            fresh = os.path.join(folder, "new.osc")
            self.upload("--osc", fresh)
            self.assertEqual(os.stat(fresh).st_mode & 0o777, 0o600)

    def test_the_changeset_is_closed_whatever_goes_wrong(self):
        for failure in (RuntimeError("boom"), KeyboardInterrupt(), osm.OsmError("the server answered 500")):
            self.calls.clear()

            def api(config, method, path, token, data=None, failure=failure):
                self.calls.append((method, path))
                if path.endswith("/upload"):
                    raise failure
                return b"4242"
            with self.assertRaises(type(failure) if not isinstance(failure, osm.OsmError) else CommandError):
                self.upload(token="secret-token", api=api)
            self.assertEqual(self.calls[-1], ("PUT", "changeset/4242/close"), failure)

    def test_the_token_goes_only_to_the_editing_api_by_exact_name(self):
        told = []
        found = osm.settings_from({"bbox": AREA, "api_url": "https://evil.example/api/0.6"}, told.append)
        self.assertEqual(found.api_url, osm.DEFAULT_API)
        self.assertTrue(told and "api.openstreetmap.org" in told[0])
        for url in ("https://api.openstreetmap.org/api/0.6", "https://master.apis.dev.openstreetmap.org/api/0.6"):
            self.assertEqual(osm.settings_from({"bbox": AREA, "api_url": url}).api_url, url)
        # Not "anything under openstreetmap.org", not the bare domain, not another test server that is not listed
        for url in ("https://openstreetmap.org.evil.example/api", "https://evilopenstreetmap.org/api", "https://openstreetmap.org@evil.example/api",
                    "https://openstreetmap.org/api/0.6", "https://api06.dev.openstreetmap.org/api/0.6", "https://www.openstreetmap.org/api/0.6"):
            told.clear()
            self.assertEqual(osm.settings_from({"bbox": AREA, "api_url": url}, told.append).api_url, osm.DEFAULT_API, url)
            self.assertTrue(told, url)
        # And whatever the settings say, the call refuses before sending the token
        bad = osm.Settings(bbox=(41.8, 12.4, 42.0, 12.7), api_url="https://evil.example/api")
        with patch("shops.osm_net.request", side_effect=CALLED_OUT), self.assertRaises(osm.OsmError):
            osm_net.api(bad, "PUT", "changeset/create", "secret-token", b"<osm/>")

    def test_an_element_not_read_whole_is_never_given_back(self):
        body = fake_overpass(meta(node(1, "Bar Aurora", **{"currency:XBT": "yes", "note": "x" * 300}), version=7))
        _text, err = self.upload("--osc", "-", body=body)
        self.assertIn("Nothing to send", err)
        body = fake_overpass(node(1, "Bar Aurora", **{"currency:XBT": "yes"}))
        _text, err = self.upload("--osc", "-", body=body)
        self.assertIn("Nothing to send", err)

"""The error paths of the requests to OpenStreetMap (shops/osm_net.py) that test_osm.py does not reach: the time that
runs out between the name and the connection, the watchdog cutting an answer, and an error line that is not text."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from . import osm, osm_net
from .test_osm import PUBLIC


class Errors(SimpleTestCase):
    def test_the_name_that_used_up_the_time_leaves_none_for_the_connection(self):
        with patch("shops.osm_net.public", return_value=PUBLIC), \
                patch("shops.osm_net.time", MagicMock(monotonic=MagicMock(side_effect=[0, 50]))), \
                self.assertRaisesMessage(osm.OsmError, "did not resolve in time"):
            osm_net.request("GET", "https://overpass.example/x", seconds=10, size=1000)

    def test_an_answer_cut_by_the_watchdog_is_never_taken_as_complete(self):
        response = MagicMock(status_code=200, headers={})
        response.__enter__.return_value = response
        response.iter_content.return_value = [b"{}"]
        with patch("requests.sessions.Session.request", return_value=response), \
                patch("shops.osm_net.public", return_value=PUBLIC), \
                patch("shops.osm_net.Watchdog", return_value=MagicMock(fired=True)), \
                self.assertRaisesMessage(osm.OsmError, "took too long"):
            osm_net.request("GET", "https://overpass.example/x", seconds=10, size=1000)

    def test_the_reason_of_a_refusal_is_only_short_plain_text(self):
        def refused(headers):
            return SimpleNamespace(headers=headers)

        self.assertEqual(osm_net.refusal(refused({"Error": "Version mismatch"})), ": Version mismatch")
        self.assertEqual(osm_net.refusal(refused({})), "")
        self.assertEqual(osm_net.refusal(refused({"Error": "a\x00b\nc"})), ": a?b?c")
        self.assertEqual(len(osm_net.refusal(refused({"Error": "x" * 500}))), len(": ") + 100)
        # Headers that cannot be read at all say nothing
        self.assertEqual(osm_net.refusal(SimpleNamespace(headers=None)), "")
        self.assertEqual(osm_net.refusal(object()), "")

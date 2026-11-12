"""The error paths of the register's community.yml readers (shops/register.py): a mistake in the file must never stop
every register, and a value that cannot be read must fall back to the default, not raise."""
from django.test import SimpleTestCase

from .register import MAX_TOLERANCE, TOLERANCE, connect_origins, explorer_url, number, read_tolerance, relay_url


class Tolerance(SimpleTestCase):
    def test_a_word_is_not_a_tolerance(self):
        self.assertEqual(read_tolerance({"tolerance": "abc"}), (TOLERANCE, False))
        self.assertEqual(read_tolerance({"tolerance": ""}), (TOLERANCE, False))

    def test_a_percentage_a_negative_number_and_a_huge_one(self):
        self.assertEqual(read_tolerance({"tolerance": "2% "}), (2.0, True))
        self.assertEqual(read_tolerance({"tolerance": -1}), (TOLERANCE, False))
        self.assertEqual(read_tolerance({"tolerance": MAX_TOLERANCE + 5}), (MAX_TOLERANCE, False))
        self.assertEqual(read_tolerance({}), (TOLERANCE, True))


class Numbers(SimpleTestCase):
    def test_a_word_a_fraction_or_a_value_out_of_bounds_is_the_default(self):
        for written in ("many", "1.5", "", [3], {"a": 1}, -1, 101):
            self.assertEqual(number({"discount": written}, "discount", 7, 0, 100), 7, written)
        self.assertEqual(number({}, "discount", 7, 0, 100), 7)

    def test_a_whole_number_a_percentage_and_the_edges(self):
        self.assertEqual(number({"discount": "5%"}, "discount", 7, 0, 100), 5)
        self.assertEqual(number({"discount": 0}, "discount", 7, 0, 100), 0)
        self.assertEqual(number({"discount": 100}, "discount", 7, 0, 100), 100)


class Addresses(SimpleTestCase):
    def test_an_address_that_cannot_even_be_split_is_refused(self):
        # An unclosed IPv6 bracket makes urlsplit raise ValueError
        self.assertIsNone(explorer_url("https://[::1/api"))
        self.assertIsNone(relay_url("wss://[::1/relay"))

    def test_a_relay_with_a_port_a_query_or_credentials_is_refused(self):
        for wrong in ("ws://relay.example.org", "wss://relay.example.org:444", "wss://u:p@relay.example.org",
                      "wss://relay.example.org/?x=1", "wss://relay.example.org/#x", "wss://127.0.0.1", 5, None):
            self.assertIsNone(relay_url(wrong), wrong)
        self.assertEqual(relay_url(" wss://Relay.Example.org/nostr/ "), "wss://relay.example.org/nostr")


class Origins(SimpleTestCase):
    def test_without_data_the_register_talks_to_nobody(self):
        self.assertEqual(connect_origins(None), [])
        self.assertEqual(connect_origins({}), [])

    def test_only_https_explorers_and_wss_relays_enter_the_list_by_host(self):
        origins = connect_origins({"domain": "ln.example.org", "sources": ["kraken"],
                                   "explorers": ["https://mempool.example.org/api", "http://plain.example.org/api"],
                                   "relays": ["wss://relay.example.org/x"], "public_relays": ["ws://plain.example.org"]})
        self.assertIn("https://mempool.example.org", origins)
        self.assertIn("wss://relay.example.org", origins)
        self.assertNotIn("https://plain.example.org", origins)
        self.assertFalse([origin for origin in origins if "plain.example.org" in origin])
        self.assertEqual(len(origins), len(set(origins)))

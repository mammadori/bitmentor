"""What `manage.py publish_nostr` sends: the profile (NIP-01) and the shops, and what it withdraws. No real relay."""
import itertools
import json
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from config.community import community

from .management.commands.publish_nostr import picture
from .models import Shop
from .nostr import profile_event, public_key, sign

SECRET = "01" * 32


class Profile(TestCase):
    def test_no_picture_means_no_picture(self):
        self.assertNotIn("picture", json.loads(profile_event(SECRET, "Community", "")["content"]))

    def test_the_picture_comes_from_the_configuration(self):
        base = "https://example.org"
        for data, expected in (
                ({"nostr": {"picture": "/theme/logo.png"}, "logo": "logo.svg"}, "https://example.org/theme/logo.png"),
                ({"nostr": {"picture": "https://img.example.net/a.jpg"}}, "https://img.example.net/a.jpg"),
                ({"logo": "logo.png"}, "https://example.org/theme/logo.png"),
                ({"logo": "logo.svg"}, ""),
                ({"nostr": None}, ""),
                ({}, "")):
            with self.subTest(data):
                self.assertEqual(picture(data, base), expected)


@override_settings(NOSTR_SECRET_KEY=SECRET, NOSTR_RELAYS=["ws://relay"], SITE_HOST="example.org")
class Command(TestCase):
    def publish(self, relay_has=None, **data):
        """`relay_has`: what the relay answers to the question about shops, before the OKs."""
        community_data = {**community(), **data}
        out, errors = StringIO(), StringIO()
        with patch("config.community.community", return_value=community_data), \
                patch("shops.management.commands.publish_nostr.community", return_value=community_data), \
                patch("shops.management.commands.publish_nostr.connect") as connect:
            socket = connect.return_value.__enter__.return_value
            answers = [message if isinstance(message, Exception) else json.dumps(message) for message in relay_has or []]
            socket.recv.side_effect = itertools.chain(answers, itertools.repeat(json.dumps(["OK", "x", True, ""])))
            call_command("publish_nostr", stdout=out, stderr=errors)
        self.out = out.getvalue()
        self.messages = [json.loads(call[0][0]) for call in socket.send.call_args_list]
        return [message[1] for message in self.messages if message[0] == "EVENT"], errors.getvalue()

    def shop_on_the_relay(self, d, pubkey=None):
        event = sign(SECRET, 30402, "Bar", [["d", d], ["title", "Bar"]])
        return ["EVENT", "shops", {**event, "pubkey": pubkey or event["pubkey"]}]

    def test_shops_no_longer_public_are_withdrawn(self):
        """NIP-09: suspended, removed from the map or deleted, the shop does not stay on the relays with name and position."""
        shown = Shop.objects.create(name="Bar", latitude=46, longitude=11)
        hidden = Shop.objects.create(name="Pub", latitude=46, longitude=11, suspended=True)
        on_the_relay = [self.shop_on_the_relay(str(shown.id)), self.shop_on_the_relay(str(hidden.id)), ["NOTICE", "slow down"],
                        self.shop_on_the_relay("deleted"), ["EOSE", "shops"]]
        sent, _errors = self.publish(relay_has=on_the_relay)
        deletion = next(event for event in sent if event["kind"] == 5)
        # By id too: nostr-rs-relay 0.9 ignores `a` tags
        self.assertEqual(sorted(tag[1] for tag in deletion["tags"] if tag[0] == "e"), sorted([on_the_relay[1][2]["id"], on_the_relay[3][2]["id"]]))
        self.assertEqual(deletion["kind"], 5)
        pubkey = public_key(SECRET)
        self.assertEqual(sorted(tag[1] for tag in deletion["tags"] if tag[0] == "a"), sorted([f"30402:{pubkey}:deleted", f"30402:{pubkey}:{hidden.id}"]))
        self.assertIn(["k", "30402"], deletion["tags"])
        self.assertEqual([event["kind"] for event in sent].count(30402), 1)
        self.assertIn("3/3, withdrawn: 2", self.out)
        self.assertEqual(self.messages[0], ["REQ", "shops", {"authors": [pubkey], "kinds": [30402], "limit": 5000}])
        self.assertIn(["CLOSE", "shops"], self.messages)

    def test_nothing_is_withdrawn_when_the_relay_has_only_the_shops_shown(self):
        shown = Shop.objects.create(name="Bar", latitude=46, longitude=11)
        sent, _errors = self.publish(relay_has=[self.shop_on_the_relay(str(shown.id)), ["EOSE", "shops"]])
        self.assertNotIn(5, [event["kind"] for event in sent])

    def test_events_of_other_keys_are_not_withdrawn(self):
        Shop.objects.create(name="Bar", latitude=46, longitude=11)
        sent, _errors = self.publish(relay_has=[self.shop_on_the_relay("somebody-elses", pubkey="ab" * 32), ["EOSE", "shops"]])
        self.assertNotIn(5, [event["kind"] for event in sent])

    def test_without_any_shop_to_send_nothing_is_asked_or_withdrawn(self):
        """A database emptied by mistake must not empty the relays."""
        sent, _errors = self.publish(relay_has=[self.shop_on_the_relay("deleted"), ["EOSE", "shops"]])
        self.assertNotIn("REQ", [message[0] for message in self.messages])
        self.assertNotIn(5, [event["kind"] for event in sent])

    @patch("shops.management.commands.publish_nostr.SHOPS_ASKED", 3)
    def test_a_relay_that_never_stops_sending_does_not_hold_the_publication(self):
        """Counted reads: the list stops, and a relay that never gives an OK counts as a relay that did not receive."""
        Shop.objects.create(name="Bar", latitude=46, longitude=11)
        with self.assertRaises(CommandError):
            self.publish(relay_has=[self.shop_on_the_relay(f"old-{n}") for n in range(50)])

    def test_ids_the_relay_makes_up_are_never_signed_for_deletion(self):
        """A hostile relay could put under our key the id of a real event (a release signature, a current shop):
        signing it in a deletion, the other relays would remove it. Only an event of shop type with the id that matches
        and a valid signature is kept."""
        Shop.objects.create(name="Bar", latitude=46, longitude=11)
        stale = self.shop_on_the_relay("real")
        release = sign(SECRET, 1063, "", [["d", "release"], ["x", "ab" * 32]])
        made_up = {**self.shop_on_the_relay("made_up")[2], "id": release["id"]}
        unsigned = {**self.shop_on_the_relay("unsigned")[2], "sig": "00" * 64}
        sent, _errors = self.publish(relay_has=[stale, ["EVENT", "shops", made_up], ["EVENT", "shops", unsigned],
                                                ["EVENT", "shops", release], ["EOSE", "shops"]])
        deletion = next(event for event in sent if event["kind"] == 5)
        self.assertEqual([tag[1] for tag in deletion["tags"] if tag[0] == "e"], [stale[2]["id"]])
        self.assertEqual([tag[1].rsplit(":", 1)[1] for tag in deletion["tags"] if tag[0] == "a"], ["real"])

    def test_malformed_events_from_the_relay_do_not_stop_the_shops(self):
        Shop.objects.create(name="Bar", latitude=46, longitude=11)
        pubkey = public_key(SECRET)
        odd = [["EVENT", "shops", {"pubkey": pubkey, "kind": 30402, "tags": None, "id": "x"}],
               ["EVENT", "shops", {"pubkey": pubkey, "kind": 30402, "tags": [["d", 5]], "id": "y"}],
               ["EVENT", "shops", sign(SECRET, 30402, "Bar", [["d", ["list"]]])], ["EVENT"], ["EOSE", "shops"]]
        sent, errors = self.publish(relay_has=odd)
        self.assertEqual([event["kind"] for event in sent], [0, 30402])
        self.assertIn("2/2", self.out)

    def test_a_relay_that_does_not_answer_the_question_still_gets_the_shops(self):
        """A relay that accepts only writes does not answer the REQ: before, it received the shops, and still does."""
        Shop.objects.create(name="Bar", latitude=46, longitude=11)
        sent, _errors = self.publish(relay_has=[TimeoutError()])
        self.assertEqual([event["kind"] for event in sent], [0, 30402])
        self.assertIn("2/2", self.out)

    def test_late_answers_to_the_question_are_not_taken_for_an_ok(self):
        Shop.objects.create(name="Bar", latitude=46, longitude=11)
        sent, _errors = self.publish(relay_has=[self.shop_on_the_relay("old"), ["EOSE", "shops"], ["CLOSED", "shops", ""],
                                                ["NOTICE", "bye"], ["OK", "x", False, "too big"], ["OK", "x", True, ""], ["OK", "x", True, ""]])
        self.assertIn("2/3", self.out)
        self.assertNotIn("withdrawn", self.out)

    @patch("shops.management.commands.publish_nostr.DELETION_BLOCK", 1)
    def test_many_shops_are_withdrawn_in_blocks(self):
        Shop.objects.create(name="Bar", latitude=46, longitude=11)
        sent, _errors = self.publish(relay_has=[self.shop_on_the_relay("uno"), self.shop_on_the_relay("due"), ["EOSE", "shops"]])
        # The deletions go first: an event rejected afterwards does not stop them
        self.assertEqual([event["kind"] for event in sent], [5, 5, 0, 30402])
        self.assertIn("4/4, withdrawn: 2", self.out)

    def test_a_strange_answer_ends_the_list_and_the_shops_still_go_out(self):
        Shop.objects.create(name="Bar", latitude=46, longitude=11)
        sent, _errors = self.publish(relay_has=[{"error": "rate limited"}])
        self.assertEqual([event["kind"] for event in sent].count(30402), 1)
        self.assertNotIn(5, [event["kind"] for event in sent])

    def test_the_profile_of_the_example_has_no_picture(self):
        # The example's logo is an SVG and there is no `nostr.picture`: before, the profile pointed to the home
        sent, _errors = self.publish()
        self.assertNotIn("picture", json.loads(sent[0]["content"]))

    def test_the_profile_picture_is_an_image_of_the_site(self):
        sent, _errors = self.publish(nostr={"picture": "/theme/logo.png"})
        self.assertEqual(json.loads(sent[0]["content"])["picture"], "https://example.org/theme/logo.png")

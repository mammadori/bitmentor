"""The edges of the guardian of the Lightning addresses (shops/lightning_watch.py): the number of failures that makes a
fault, and every sentence of the message to the association."""
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from . import lightning_watch
from .lightning_watch import BROKE, FAILURE_GAP, FIXED, apply, tell_association
from .lnaddress import NO_ANSWER
from .models import LightningStatus, Shop
from .tests import reset_limits


class Threshold(TestCase):
    def test_the_number_of_failures_that_make_a_fault_is_the_one_configured(self):
        shop = Shop(lightning_address="a@example.org")
        now = timezone.now()
        with patch("shops.lightning_watch.FAILURES", 3):
            self.assertIsNone(apply(shop, NO_ANSWER, now))
            # The second failure, far enough from the first, is still not a fault: one more is needed
            self.assertIsNone(apply(shop, NO_ANSWER, now + FAILURE_GAP))
            self.assertEqual(shop.lightning_failures, 2)
            self.assertNotIn(shop.lightning_status, lightning_watch.BROKEN)
            self.assertEqual(apply(shop, NO_ANSWER, now + 2 * FAILURE_GAP), BROKE)
        self.assertEqual(shop.lightning_since, now)
        self.assertEqual(shop.lightning_status, NO_ANSWER)


@override_settings(TELEGRAM_BOT_TOKEN="t", TELEGRAM_MODERATION_CHAT="-100")
class Message(TestCase):
    def setUp(self):
        reset_limits()
        self.now = timezone.now()

    def shop(self, name, status, worked, **more):
        return Shop.objects.create(name=name, lightning_address=f"{name.lower()}@example.org", lightning_status=status,
                                   lightning_worked=worked, lightning_since=self.now - timedelta(days=1), **more)

    def said(self, changes, mass=None):
        with patch("shops.lightning_watch.telegram") as sent:
            tell_association(changes, mass)
        sent.assert_called_once()
        self.assertEqual(sent.call_args.args[0], "-100")
        return sent.call_args.args[1]

    def test_every_kind_of_change_has_its_own_sentence(self):
        text = self.said([
            (self.shop("Never", LightningStatus.NO_ANSWER, None), BROKE),
            (self.shop("Unconfirmed", LightningStatus.NO_CONFIRMATION, None), BROKE),
            (self.shop("Stopped", LightningStatus.NO_ANSWER, self.now - timedelta(days=9)), BROKE),
            (self.shop("Mute", LightningStatus.NO_CONFIRMATION, self.now - timedelta(days=9)), BROKE),
            (self.shop("Back", LightningStatus.OK, self.now), FIXED)])
        for line in ("Never: does not answer", "Unconfirmed: does not confirm payments", "Stopped: stopped answering",
                     "Mute: stopped confirming payments", "Back: works again"):
            self.assertIn(line, text)
        self.assertIn("/admin/shops/shop/?lightning=broken", text)

    def test_a_long_run_says_how_many_lines_were_left_out(self):
        changes = [(self.shop(f"Bar{n}", LightningStatus.NO_ANSWER, None), BROKE) for n in range(5)]
        with patch("shops.lightning_watch.TELEGRAM_LINES", 2):
            text = self.said(changes)
        self.assertIn("Bar0", text)
        self.assertIn("Bar1", text)
        self.assertNotIn("Bar2", text)
        self.assertIn("…and 3 more", text)

    def test_a_run_where_most_failed_says_it_is_probably_the_network(self):
        text = self.said([], mass=(40, 50))
        self.assertIn("40 of 50 addresses failed at the same time", text)
        self.assertIn("no merchant was told", text)

    def test_nothing_to_tell_nothing_sent(self):
        with patch("shops.lightning_watch.telegram") as sent:
            tell_association([])
            tell_association([(self.shop("Quiet", LightningStatus.OK, self.now), None)])
        sent.assert_not_called()

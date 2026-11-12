"""The check of a Lightning address on submit (new shop, wallet guide): bounded in time and in threads, outside the
transaction, behind limits per account, per /64 and per /48. A free account pointing its address at a slow server must
not hold the web workers."""
import threading
import time
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase

from . import lnaddress
from .forms import public_host
from .lnaddress import refusal
from .models import Shop
from .tests import reset_limits, steady_limits

SLOW = "Your wallet's provider is slow or busy right now: try again in a few minutes."
BUSY = "Too many checks right now: try again in a minute."


def slow_probe(seconds):
    """A provider that takes `seconds`, whatever limit it is given."""
    return patch("shops.lnaddress.probe", side_effect=lambda *args, **kwargs: time.sleep(seconds) or lnaddress.OK)


def wait_for_places():
    """The probes past their time end in the background: the next test starts with every place free."""
    stop = time.monotonic() + 5
    while lnaddress.SUBMIT_SLOTS._value < lnaddress.SUBMIT_AT_ONCE and time.monotonic() < stop:
        time.sleep(0.05)


class Base(TestCase):
    def setUp(self):
        reset_limits()
        self.addCleanup(reset_limits)
        self.addCleanup(wait_for_places)
        self.user = get_user_model().objects.create(username="k")
        self.client.force_login(self.user)
        self.enterContext(patch("shops.forms.public_host", return_value=True))

    def new(self, user=None, ip="203.0.113.5", **fields):
        if user:
            self.client.force_login(user)
        return self.client.post("/en/shops/new/", {"name": "Bar", "lightning_address": "mario@example.org", **fields},
                                REMOTE_ADDR=ip)

    def waiting(self):
        return Shop.objects.create(name="Bar", owner=self.user, awaiting_wallet=True)


class Bounded(Base):
    def test_a_slow_provider_holds_the_page_only_for_the_limit(self):
        with patch("shops.views.LIGHTNING_SUBMIT_SECONDS", 0.1), slow_probe(2):
            started = time.monotonic()
            response = self.new()
            self.assertLess(time.monotonic() - started, 1.6)
        self.assertContains(response, "is slow or busy right now: try again in a few minutes.")
        self.assertFalse(Shop.objects.exists())

    def test_the_wallet_guide_too(self):
        shop = self.waiting()
        with patch("shops.views.LIGHTNING_SUBMIT_SECONDS", 0.1), slow_probe(2):
            started = time.monotonic()
            response = self.client.post(f"/en/shops/wallet/{shop.id}/", {"lightning_address": "mario@example.org"})
            self.assertLess(time.monotonic() - started, 1.6)
        self.assertContains(response, "is slow or busy right now: try again in a few minutes.")
        shop.refresh_from_db()
        self.assertEqual(shop.lightning_address, "")

    def test_the_probe_gets_what_is_left_of_the_limit(self):
        with patch("shops.lnaddress.probe", return_value=lnaddress.OK) as tried:
            self.assertEqual(self.new().status_code, 302)
        address, seconds = tried.call_args.args
        self.assertEqual(address, "mario@example.org")
        self.assertTrue(0 < seconds <= 12, seconds)
        self.assertEqual(Shop.objects.get().lightning_address, "mario@example.org")

    def test_a_probe_cut_by_its_own_limit_is_slow_not_wrong(self):
        """The fetches end on their own deadline with "no answer": that is the provider's time, not a wrong address."""
        def cut(address, seconds, **_options):
            time.sleep(seconds + 0.05)
            return lnaddress.NO_ANSWER
        with patch("shops.lnaddress.probe", side_effect=cut):
            self.assertEqual(refusal("mario@example.org", self.user.pk, seconds=0.2), SLOW)

    def test_with_every_place_taken_the_form_hears_busy_at_once(self):
        for _place in range(lnaddress.SUBMIT_AT_ONCE):
            lnaddress.SUBMIT_SLOTS.acquire()
        try:
            with patch("shops.lnaddress.probe") as tried:
                started = time.monotonic()
                response = self.new()
                self.assertLess(time.monotonic() - started, 1)
            tried.assert_not_called()
        finally:
            for _place in range(lnaddress.SUBMIT_AT_ONCE):
                lnaddress.SUBMIT_SLOTS.release()
        self.assertContains(response, BUSY)
        # The form is shown again as written: one more press when the server is free
        self.assertContains(response, 'value="mario@example.org"')
        self.assertFalse(Shop.objects.exists())

    def test_at_most_submit_at_once_probes_run_together(self):
        running, most, lock = [0], [0], threading.Lock()

        def probe(*args, **kwargs):
            with lock:
                running[0] += 1
                most[0] = max(most[0], running[0])
            time.sleep(0.3)
            with lock:
                running[0] -= 1
            return lnaddress.OK

        answers = []
        with patch("shops.lnaddress.probe", side_effect=probe):
            workers = [threading.Thread(target=lambda: answers.append(refusal("mario@example.org", seconds=2)))
                       for _n in range(6)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()
        self.assertEqual(most[0], lnaddress.SUBMIT_AT_ONCE)
        self.assertEqual(answers.count(None), lnaddress.SUBMIT_AT_ONCE)
        self.assertEqual(answers.count(BUSY), 6 - lnaddress.SUBMIT_AT_ONCE)

    def test_the_live_check_does_not_take_the_places_of_the_submit(self):
        for _place in range(lnaddress.LIVE_AT_ONCE):
            lnaddress.LIVE_SLOTS.acquire()
        try:
            with patch("shops.lnaddress.probe", return_value=lnaddress.OK):
                self.assertEqual(self.new().status_code, 302)
        finally:
            for _place in range(lnaddress.LIVE_AT_ONCE):
                lnaddress.LIVE_SLOTS.release()

    def test_an_address_that_just_passed_the_live_check_is_not_probed_again(self):
        lnaddress.cache.set(lnaddress.cache_key("mario@example.org", self.user.pk), lnaddress.OK)
        for _place in range(lnaddress.SUBMIT_AT_ONCE):
            lnaddress.SUBMIT_SLOTS.acquire()
        try:
            with patch("shops.lnaddress.probe") as tried:
                self.assertEqual(self.new().status_code, 302)
            tried.assert_not_called()
        finally:
            for _place in range(lnaddress.SUBMIT_AT_ONCE):
                lnaddress.SUBMIT_SLOTS.release()


class OutsideTheTransaction(Base):
    def test_the_probe_runs_before_the_lock(self):
        """While the probe waits on someone else's server no transaction is open and no row of the profile is locked."""
        depth = len(connection.atomic_blocks)  # the test's own transactions
        seen = []

        def probe(address, user, seconds):
            seen.append(len(connection.atomic_blocks))
            return None
        with patch("shops.views.refusal", side_effect=probe):
            self.assertEqual(self.new().status_code, 302)
        self.assertEqual(seen, [depth])


class Limits(Base):
    def setUp(self):
        super().setUp()
        self.enterContext(steady_limits())
        self.enterContext(patch("shops.views.refusal", return_value=None))

    def test_per_account_whatever_the_address(self):
        with patch("shops.views.SHOPS_PER_USER", 1):
            self.new(ip="2001:db8:1:1::1", name="Uno")
            self.assertContains(self.new(ip="198.51.100.7", name="Due"), "Too many shops in a short time")
        self.assertEqual(Shop.objects.count(), 1)

    def test_per_48_whatever_the_64_and_the_account(self):
        other = get_user_model().objects.create(username="j")
        with patch("shops.views.SHOPS_PER_NETWORK", 1):
            self.new(ip="2001:db8:1:1::1", name="Uno")
            # Another owner's wallet: one address is never on shops of different owners (shops/certified.py)
            self.assertContains(self.new(user=other, ip="2001:db8:1:2::1", name="Due", lightning_address="j@example.org"), "Too many shops in a short time")
            # Another /48 has a count of its own
            self.assertEqual(self.new(user=other, ip="2001:db8:2:1::1", name="Tre",
                                      lightning_address="j@example.org").status_code, 302)
        self.assertEqual(Shop.objects.count(), 2)

    def test_the_wallet_guide_per_account_and_per_48(self):
        first, second = self.waiting(), self.waiting()
        with patch("shops.views.SHOPS_PER_USER", 1):
            self.client.post(f"/en/shops/wallet/{first.id}/", {"lightning_address": "mario@example.org"},
                             REMOTE_ADDR="2001:db8:1:1::1")
            response = self.client.post(f"/en/shops/wallet/{second.id}/", {"lightning_address": "mario@example.org"},
                                        REMOTE_ADDR="198.51.100.7")
        self.assertContains(response, "Too many shops in a short time")
        reset_limits()
        with patch("shops.views.SHOPS_PER_NETWORK", 1):
            self.client.post(f"/en/shops/wallet/{second.id}/", {"lightning_address": "mario@example.org"},
                             REMOTE_ADDR="2001:db8:1:1::1")
            third = self.waiting()
            response = self.client.post(f"/en/shops/wallet/{third.id}/", {"lightning_address": "mario@example.org"},
                                        REMOTE_ADDR="2001:db8:1:2::1")
        self.assertContains(response, "Too many shops in a short time")


class SlowNames(TestCase):
    def test_a_slow_dns_does_not_hold_the_form(self):
        """The rules of the form ask the DNS too: past DNS_SECONDS the name counts as not known, and the probe, bounded,
        decides; its connections are checked on the address reached."""
        with patch("config.network.DNS_SECONDS", 0.1), \
                patch("socket.getaddrinfo", side_effect=lambda *args, **kwargs: time.sleep(0.5) or []):
            started = time.monotonic()
            self.assertTrue(public_host("slow.example.org"))
            self.assertLess(time.monotonic() - started, 0.45)
            time.sleep(0.5)

    def test_a_name_without_answer_is_try_again_and_is_not_asked_twice(self):
        """The probe asks the name within its own deadline; past DNS_SECONDS it answers "try again" (nothing is known
        about the address) and does not let urllib3 ask the same name again, with no limit, in the submit pool."""
        answer = threading.Event()
        self.addCleanup(answer.set)
        with patch("config.network.DNS_SECONDS", 0.2), patch("config.network.DNS_SLOTS", threading.BoundedSemaphore(8)), \
                patch("socket.getaddrinfo", side_effect=lambda *args, **kwargs: answer.wait(5) and []) as asked, \
                patch("shops.lnaddress.requests.Session.get") as fetched:
            started = time.monotonic()
            self.assertEqual(refusal("mario@example.org", seconds=5), SLOW)
            self.assertLess(time.monotonic() - started, 2)
            # The guardian hears "do not know", the live check "does not answer"
            self.assertEqual(lnaddress.probe("mario@example.org", busy="unsure"), "unsure")
            self.assertEqual(lnaddress.probe("mario@example.org", seconds=5), lnaddress.NO_ANSWER)
            answer.set()
        fetched.assert_not_called()
        self.assertEqual(asked.call_count, 3)

    def test_names_that_lead_inside_in_ipv6_spelling(self):
        for address in ("::127.0.0.1", "64:ff9b::a00:1", "::ffff:10.0.0.1"):
            with self.subTest(address), patch("socket.getaddrinfo", return_value=[(10, 1, 6, "", (address, 443, 0, 0))]):
                self.assertFalse(public_host("inside.example.org"))

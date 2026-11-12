"""The guardian of the Lightning addresses (shops/lightning_watch.py), against a fake LNURL provider that answers well,
badly, slowly or not at all. Nothing here talks to a real provider."""
import json
from datetime import date, timedelta
from io import StringIO
from unittest.mock import MagicMock, patch
from urllib.parse import urlsplit

import requests
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from . import lightning_watch
from .lightning_watch import BROKE, FAILURE_GAP, FIXED, apply, check
from .lnaddress import NO_ANSWER, NO_CONFIRMATION, OK, UNKNOWN_DOMAIN, probe
from .management.commands.preflight import lightning_notes
from .models import LightningStatus, Profile, Shop
from .tests import admin_session, public_names, steady_limits

COMMAND = "shops.management.commands.preflight"
# The settings of a site ready for production, as in test_preflight
PREFLIGHT = {"SITE_SCHEME": "https", "NETWORK": "mainnet", "DEBUG": False, "SECRET_KEY": "x" * 60, "EMAIL_HOST": "mail",
             "DEFAULT_FROM_EMAIL": "Community <noreply@community.example.net>", "SECURE_HSTS_SECONDS": 31536000}
PANEL = "/en/shops/"


class FakeProvider:
    """A Lightning provider as `requests.Session.get` sees it, for every address under any domain. `mode`:
    ok, silent (no connection), error (HTTP 404), busy (HTTP 503), slow (trickles past the time limit), no_verify (an invoice that cannot
    say it was paid), not_lnurl (a page that is not a Lightning address), no_invoice, infinite (a smallest amount that
    is no number: 1e400 in the JSON, which Python reads as infinity), callback_elsewhere and verify_elsewhere (the
    invoices, or the address that says «paid», on another site than the address's)."""

    def __init__(self, mode="ok"):
        self.mode = mode
        self.urls = []

    def reply(self, answer, chunks=None):
        response = MagicMock()
        response.__enter__.return_value = response
        response.iter_content.return_value = chunks or [json.dumps(answer).encode()]
        return response

    def __call__(self, url, **kwargs):
        self.urls.append(url)
        assert kwargs["allow_redirects"] is False
        if self.mode == "silent":
            raise requests.ConnectionError("no route")
        if self.mode in ("error", "busy"):
            response = self.reply({})
            # error: 404, the address is not there; busy: 503, the provider is down for a while
            response.status_code = 404 if self.mode == "error" else 503
            response.raise_for_status.side_effect = requests.HTTPError(str(response.status_code), response=response)
            return response
        if self.mode == "slow":
            return self.reply({}, chunks=[b" "] * 50)
        if "/.well-known/lnurlp/" in url:
            if self.mode == "not_lnurl":
                return self.reply({"status": "ERROR", "reason": "not found"})
            if self.mode == "infinite":
                return self.reply(None, chunks=[b'{"tag": "payRequest", "callback": "https://pay.example.org/cb", "minSendable": 1e400}'])
            # The provider of each address answers from a subdomain of the address's own domain, as the register asks
            host = "elsewhere.example" if self.mode == "callback_elsewhere" else urlsplit(url).hostname
            return self.reply({"tag": "payRequest", "callback": f"https://pay.{host}/cb", "minSendable": 1000})
        if "/cb?" in url:
            if self.mode == "no_invoice":
                return self.reply({"status": "ERROR"})
            invoice = {"pr": "lnbc10n1fake"}
            if self.mode != "no_verify":
                host = "pay.elsewhere.example" if self.mode == "verify_elsewhere" else urlsplit(url).hostname
                invoice["verify"] = f"https://{host}/verify/abc"
            return self.reply(invoice)
        if "/verify/" in url:
            return self.reply({"status": "OK", "settled": False, "preimage": None, "pr": "lnbc10n1fake"})
        raise AssertionError(url)


def provider(mode="ok"):
    fake = FakeProvider(mode)
    patches = [public_names(), patch("shops.lnaddress.missing", return_value=False),
               patch("shops.lnaddress.requests.Session.get", side_effect=fake)]
    if mode == "slow":
        # Each chunk takes a second: past MAX_SECONDS the answer does not count
        clock = iter(range(0, 10000))
        patches.append(patch("shops.lnaddress.time.monotonic", side_effect=lambda: next(clock)))
    return fake, patches


class Provider:
    """with Provider("silent") as fake: ... the fake provider in place for the block."""

    def __init__(self, mode="ok"):
        self.fake, self.patches = provider(mode)

    def __enter__(self):
        for item in self.patches:
            item.start()
        return self.fake

    def __exit__(self, *exc):
        for item in reversed(self.patches):
            item.stop()


class Probe(TestCase):
    def test_what_the_probe_finds(self):
        for mode, found in (("ok", OK), ("silent", NO_ANSWER), ("error", NO_ANSWER), ("slow", NO_ANSWER),
                            ("not_lnurl", NO_ANSWER), ("no_invoice", NO_ANSWER), ("no_verify", NO_CONFIRMATION),
                            ("infinite", NO_ANSWER), ("callback_elsewhere", NO_ANSWER),
                            ("verify_elsewhere", NO_CONFIRMATION)):
            with self.subTest(mode), Provider(mode):
                self.assertEqual(probe("bar@example.org"), found)

    def test_what_the_guardian_reads(self):
        """One probe for the form and the guardian: OK is None for the guardian, and a provider whose name is gone a fault
        like one that does not answer."""
        for found, read in ((OK, None), (UNKNOWN_DOMAIN, NO_ANSWER), (NO_ANSWER, NO_ANSWER),
                            (NO_CONFIRMATION, NO_CONFIRMATION), (lightning_watch.UNSURE, lightning_watch.UNSURE)):
            with self.subTest(found), patch("shops.lnaddress.probe", return_value=found) as asked:
                self.assertEqual(lightning_watch.watch_probe("bar@example.org"), read)
            asked.assert_called_once_with("bar@example.org", busy=lightning_watch.UNSURE)

    def test_only_the_address_s_own_provider_is_asked_as_at_the_register(self):
        """The register talks only to the address's domain and its subdomains: an invoice or a confirmation from another
        site would pass here and fail at the counter. Nothing is asked of the other site."""
        with Provider("callback_elsewhere") as fake:
            self.assertEqual(probe("bar@example.org"), NO_ANSWER)
        self.assertEqual(fake.urls, ["https://example.org/.well-known/lnurlp/bar"])
        with Provider("verify_elsewhere") as fake:
            self.assertEqual(probe("bar@example.org"), NO_CONFIRMATION)
        self.assertEqual(fake.urls, ["https://example.org/.well-known/lnurlp/bar", "https://pay.example.org/cb?amount=1000"])

    def test_what_counts_as_the_address_s_own_provider(self):
        from .lnaddress import same_provider
        for url in ("https://example.org/cb", "https://pay.example.org/cb", "https://a.b.example.org:443/cb", "https://EXAMPLE.org/cb"):
            self.assertTrue(same_provider(url, "example.org"), url)
        for url in ("http://example.org/cb", "https://example.org:8443/cb", "https://notexample.org/cb",
                    "https://example.org.evil.example/cb", "https://evil.example/?example.org", "https://user@evil.example/example.org",
                    "https://example.org@evil.example/cb", "//example.org/cb", "", None, 7, "https://[::1/cb"):
            self.assertFalse(same_provider(url, "example.org"), url)

    def test_the_same_requests_as_when_the_shop_is_created(self):
        """Metadata, an invoice of the smallest amount that nobody pays, its verify URL: nothing more."""
        with Provider() as fake:
            probe("bar@example.org")
        self.assertEqual(fake.urls, ["https://example.org/.well-known/lnurlp/bar", "https://pay.example.org/cb?amount=1000",
                                     "https://pay.example.org/verify/abc"])


@override_settings(TELEGRAM_BOT_TOKEN="", TELEGRAM_MODERATION_CHAT="")
class Watch(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create(username="k")
        Profile.objects.create(user=self.owner, email="mario@example.org")
        self.shop = Shop.objects.create(owner=self.owner, name="Central Bar",
                                        lightning_address="bar@example.org")
        self.now = timezone.now()

    def run_check(self, mode, after=timedelta(0), by_owner=False):
        """by_owner: as "Try again now" in the panel, which records without an email."""
        self.now += after
        with Provider(mode), patch("shops.lightning_watch.timezone.now", return_value=self.now):
            if by_owner:
                address, result, _broken = lightning_watch.probe_shop(self.shop.pk)
                shop, change, _recorded = lightning_watch.record(self.shop.pk, address, result)
            else:
                shop, change = check(self.shop.pk)
        self.shop.refresh_from_db()
        return change

    def test_a_working_address_is_noted_quietly(self):
        self.assertIsNone(self.run_check("ok"))
        self.assertEqual(self.shop.lightning_status, LightningStatus.OK)
        self.assertEqual(self.shop.lightning_worked, self.now)
        self.assertEqual(mail.outbox, [])

    def test_one_failure_is_a_hiccup(self):
        self.run_check("ok")
        self.assertIsNone(self.run_check("silent", timedelta(hours=6)))
        self.assertEqual(self.shop.lightning_status, LightningStatus.OK)
        self.assertEqual(self.shop.lightning_failures, 1)
        self.assertIsNone(self.run_check("ok", timedelta(hours=1)))
        self.assertEqual((self.shop.lightning_failures, self.shop.lightning_failing_since), (0, None))
        self.assertEqual(mail.outbox, [])

    def test_two_failures_close_together_are_not_a_fault(self):
        self.run_check("silent")
        self.assertIsNone(self.run_check("silent", FAILURE_GAP - timedelta(minutes=1)))
        self.assertNotEqual(self.shop.lightning_status, LightningStatus.NO_ANSWER)
        self.assertEqual(mail.outbox, [])

    def test_two_failures_some_time_apart_are_a_fault_told_once(self):
        self.run_check("ok")
        first = self.now + timedelta(hours=6)
        self.run_check("silent", timedelta(hours=6))
        self.assertEqual(self.run_check("silent", FAILURE_GAP), BROKE)
        self.assertEqual(self.shop.lightning_status, LightningStatus.NO_ANSWER)
        # Since the first failed probe: that is when customers started to meet it
        self.assertEqual(self.shop.lightning_since, first)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["mario@example.org"])
        self.assertIsNone(self.run_check("silent", timedelta(hours=1)))
        self.assertEqual(len(mail.outbox), 1)

    def test_back_to_work(self):
        self.run_check("no_verify")
        self.run_check("no_verify", timedelta(hours=1))
        self.assertEqual(self.shop.lightning_status, LightningStatus.NO_CONFIRMATION)
        self.assertEqual(self.run_check("ok", timedelta(hours=1)), FIXED)
        self.assertEqual(self.shop.lightning_status, LightningStatus.OK)
        self.assertEqual(len(mail.outbox), 2)
        self.assertIn("work again", mail.outbox[1].subject)
        self.assertIn("nothing to do", mail.outbox[1].body)

    def test_another_reason_for_the_same_fault_is_not_told_again(self):
        self.run_check("silent")
        self.run_check("silent", timedelta(hours=1))
        self.assertIsNone(self.run_check("no_verify", timedelta(hours=1)))
        self.assertEqual(self.shop.lightning_status, LightningStatus.NO_CONFIRMATION)
        self.assertEqual(len(mail.outbox), 1)

    def test_a_new_address_starts_from_scratch(self):
        self.run_check("silent")
        self.run_check("silent", timedelta(hours=1))
        Shop.objects.filter(pk=self.shop.pk).update(lightning_address="new@example.net")
        self.assertIsNone(self.run_check("ok", timedelta(minutes=5)))
        self.assertEqual(self.shop.lightning_status, LightningStatus.OK)
        self.assertEqual(self.shop.lightning_checked_address, "new@example.net")
        # Neither "it works again" (it is another address) nor a new fault
        self.assertEqual(len(mail.outbox), 1)

    def test_an_address_changed_during_the_probe_keeps_nothing_of_it(self):
        def changed(*args, **kwargs):
            Shop.objects.filter(pk=self.shop.pk).update(lightning_address="new@example.net")
            return NO_ANSWER
        with patch("shops.lightning_watch.lnaddress.probe", side_effect=changed):
            self.assertEqual(check(self.shop.pk)[1], None)
        self.shop.refresh_from_db()
        self.assertIsNone(self.shop.lightning_checked)

    def test_a_shop_deleted_or_not_watched_is_left_alone(self):
        self.shop.suspended = True
        self.shop.save()
        with Provider() as fake:
            self.assertEqual(check(self.shop.pk), (None, None))
            self.assertEqual(check("00000000-0000-0000-0000-000000000000"), (None, None))
        self.assertEqual(fake.urls, [])

    def test_a_fault_seen_from_the_panel_sends_no_email(self):
        self.run_check("silent")
        self.run_check("silent", timedelta(hours=1))
        self.assertEqual(self.run_check("ok", timedelta(minutes=2), by_owner=True), FIXED)
        self.assertEqual(len(mail.outbox), 1)

    def test_the_email_says_what_happened_what_customers_see_and_what_to_do(self):
        self.run_check("silent")
        self.run_check("silent", timedelta(hours=1))
        message = mail.outbox[0]
        self.assertIn("Central Bar", message.subject)
        self.assertIn("cannot take Lightning payments", message.subject)
        for text in ("bar@example.org", "did not answer", "What your customers see", "What to do now",
                     "Open your wallet", f"{settings.PUBLIC_URL}/en/shops/", "change it from your shop's page"):
            self.assertIn(text, message.body)
        for jargon in ("LNURL", "LUD", "webhook", "store"):
            self.assertNotIn(jargon, message.body)

    def test_the_email_for_payments_not_confirmed(self):
        self.run_check("no_verify")
        self.run_check("no_verify", timedelta(hours=1))
        self.assertIn("does not see Lightning payments arrive", mail.outbox[0].subject)
        self.assertIn("check in your wallet that each payment arrived", mail.outbox[0].body)

    def test_never_to_the_public_email(self):
        """Anyone can write any address as a shop's public email: these emails would reach a stranger."""
        Profile.objects.filter(user=self.owner).update(email="")
        self.shop.email = "info@example.net"
        self.shop.save()
        self.run_check("silent")
        self.assertEqual(self.run_check("silent", timedelta(hours=1)), BROKE)
        self.assertEqual(mail.outbox, [])

    def test_without_any_email_the_panel_is_enough(self):
        Profile.objects.filter(user=self.owner).update(email="")
        self.run_check("silent")
        self.assertEqual(self.run_check("silent", timedelta(hours=1)), BROKE)
        self.assertEqual(mail.outbox, [])

    def test_a_mail_server_down_does_not_stop_the_watch(self):
        self.run_check("silent")
        with patch("shops.lightning_watch.send_mail", side_effect=OSError):
            self.assertEqual(self.run_check("silent", timedelta(hours=1)), BROKE)
        self.assertEqual(self.shop.lightning_status, LightningStatus.NO_ANSWER)

    def test_a_name_on_two_lines_does_not_break_the_subject(self):
        Shop.objects.filter(pk=self.shop.pk).update(name="Bar\nCentrale")
        self.run_check("silent")
        self.run_check("silent", timedelta(hours=1))
        self.assertEqual(mail.outbox[0].subject.count("\n"), 0)


class Watched(TestCase):
    def shop(self, **fields):
        return Shop.objects.create(**{"name": "Bar", "lightning_address": "bar@example.org", **fields})

    def test_who_is_watched(self):
        today = timezone.localdate()
        watched = [self.shop(), self.shop(closed_on=today + timedelta(days=30)),
                   self.shop(closed_on=today), self.shop(closed_on=today - timedelta(days=1)),
                   self.shop(closed_on=today - timedelta(days=365))]
        self.shop(suspended=True)
        # A historic shop keeps its register for a year after its closing day (models.REGISTER_AFTER_CLOSING)
        self.shop(closed_on=today - timedelta(days=366))
        self.shop(lightning_address="")
        self.assertEqual(set(lightning_watch.watched()), set(watched))
        for shop in Shop.objects.all():
            self.assertEqual(lightning_watch.is_watched(shop), shop in watched, shop.__dict__)

    def test_who_is_due(self):
        now = timezone.now()
        never = self.shop()
        recent = self.shop(lightning_checked=now - timedelta(hours=1), lightning_checked_address="bar@example.org",
                           lightning_status="ok")
        old = self.shop(lightning_checked=now - timedelta(hours=7), lightning_checked_address="bar@example.org",
                        lightning_status="ok")
        failing = self.shop(lightning_checked=now - timedelta(hours=1), lightning_checked_address="bar@example.org",
                            lightning_status="ok", lightning_failures=1)
        broken = self.shop(lightning_checked=now - timedelta(hours=1), lightning_checked_address="bar@example.org",
                           lightning_status="no_answer")
        just_failed = self.shop(lightning_checked=now - timedelta(minutes=10), lightning_checked_address="bar@example.org",
                                lightning_status="no_answer")
        other_address = self.shop(lightning_checked=now - timedelta(minutes=10), lightning_checked_address="old@example.org",
                                  lightning_status="ok")
        due = list(lightning_watch.due(now))
        self.assertEqual(set(due), {never, old, failing, broken, other_address})
        self.assertEqual(due[0], never)
        self.assertNotIn(recent, due)
        self.assertNotIn(just_failed, due)

    def test_a_shop_no_longer_watched_forgets_its_status(self):
        shop = self.shop(suspended=True, lightning_status="no_answer", lightning_checked=timezone.now(),
                         lightning_checked_address="bar@example.org", lightning_failures=2)
        kept = self.shop(lightning_status="no_answer", lightning_checked=timezone.now(), lightning_checked_address="bar@example.org")
        self.assertEqual(lightning_watch.forget_unwatched(), 1)
        shop.refresh_from_db()
        self.assertEqual((shop.lightning_status, shop.lightning_checked, shop.lightning_failures), ("", None, 0))
        kept.refresh_from_db()
        self.assertEqual(kept.lightning_status, "no_answer")

    def test_apply_alone(self):
        shop = Shop(lightning_address="a@example.org")
        now = timezone.now()
        self.assertIsNone(apply(shop, NO_ANSWER, now))
        self.assertIsNone(apply(shop, NO_ANSWER, now + FAILURE_GAP / 2))
        self.assertEqual(apply(shop, NO_ANSWER, now + FAILURE_GAP), BROKE)
        self.assertEqual(shop.lightning_since, now)


@override_settings(TELEGRAM_BOT_TOKEN="t", TELEGRAM_MODERATION_CHAT="-100")
class Command(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create(username="k")
        self.shops = [Shop.objects.create(owner=self.owner, name=f"Bar {n}",
                                          lightning_address=f"bar{n}@example.org") for n in range(3)]

    def run_it(self, mode="ok", *args, now=None, clock=None):
        out = StringIO()
        with Provider(mode) as fake, patch("shops.management.commands.check_lightning.time") as command_time, \
                patch("shops.notifications.requests.post") as post:
            command_time.monotonic.side_effect = clock or (lambda: 0)
            sleep = command_time.sleep
            post.return_value.status_code = 200
            network = patch("shops.lightning_watch.network_ok", return_value=True)
            network.start()
            self.addCleanup(network.stop)
            if now:
                with patch("shops.lightning_watch.timezone.now", return_value=now), \
                        patch("shops.management.commands.check_lightning.timezone.now", return_value=now):
                    call_command("check_lightning", *args, stdout=out)
            else:
                call_command("check_lightning", *args, stdout=out)
        return fake, sleep, post, out.getvalue()

    def test_one_shop_at_a_time_with_a_pause(self):
        fake, sleep, post, out = self.run_it()
        self.assertEqual(len(fake.urls), 9)
        self.assertEqual(sleep.call_count, 2)
        sleep.assert_called_with(3)
        self.assertIn("3 probed, 3 working, 0 failed, 0 unsure, 0 changed", out)
        post.assert_not_called()

    def test_only_the_shops_that_are_due(self):
        self.run_it()
        fake, sleep, post, out = self.run_it()
        self.assertEqual(fake.urls, [])
        fake, sleep, post, out = self.run_it("ok", "--all")
        self.assertEqual(len(fake.urls), 9)

    def test_a_cap_per_run(self):
        fake, sleep, post, out = self.run_it("ok", "--limit", "1")
        self.assertEqual(len(fake.urls), 3)
        # The next run takes the others, the ones waiting the longest first
        fake, sleep, post, out = self.run_it("ok", "--limit", "5")
        self.assertEqual(len(fake.urls), 6)

    def test_a_time_budget_per_run(self):
        fake, sleep, post, out = self.run_it("ok", "--minutes", "1", clock=[0, 0, 61, 61])
        self.assertEqual(len(fake.urls), 3)
        self.assertIn("1 probed", out)

    def test_a_provider_down_for_everybody_makes_one_message(self):
        now = timezone.now()
        self.run_it("ok", now=now)
        self.run_it("silent", "--all", now=now + timedelta(hours=6))
        fake, sleep, post, out = self.run_it("silent", now=now + timedelta(hours=7))
        self.assertEqual(post.call_count, 1)
        text = post.call_args.kwargs["json"]["text"]
        self.assertEqual(post.call_args.kwargs["json"]["chat_id"], "-100")
        for shop in self.shops:
            self.assertIn(shop.name, text)
        self.assertIn("stopped answering", text)
        self.assertIn("/admin/shops/shop/?lightning=broken", text)
        self.assertNotIn("bar0@example.org", text)
        self.assertIn("3 changed", out)
        fake, sleep, post, out = self.run_it("ok", now=now + timedelta(hours=8))
        self.assertIn("works again", post.call_args.kwargs["json"]["text"])

    def test_without_a_moderation_chat_no_message(self):
        now = timezone.now()
        with override_settings(TELEGRAM_MODERATION_CHAT=""):
            self.run_it("silent", now=now)
            fake, sleep, post, out = self.run_it("silent", now=now + timedelta(hours=1))
        post.assert_not_called()
        self.assertIn("3 changed", out)

    def test_telegram_down_does_not_stop_the_run(self):
        now = timezone.now()
        self.run_it("silent", now=now)
        with patch("shops.lightning_watch.telegram", side_effect=lightning_watch.NotificationFailed("500")):
            fake, sleep, post, out = self.run_it("silent", now=now + timedelta(hours=1))
        self.assertEqual(Shop.objects.filter(lightning_status="no_answer").count(), 3)

    def test_a_provider_that_answers_nonsense_does_not_stop_the_run(self):
        """One merchant's provider answers a smallest amount of 1e400: that address does not answer, like any other
        fault, and the run goes on to record every shop."""
        now = timezone.now()
        self.run_it("infinite", now=now)
        fake, sleep, post, out = self.run_it("infinite", now=now + timedelta(hours=1))
        self.assertIn("3 probed", out)
        self.assertEqual(Shop.objects.filter(lightning_status="no_answer").count(), 3)

    def test_a_probe_that_raises_skips_that_shop_only(self):
        """Whatever goes wrong while one shop is probed, the others are probed and recorded, and the log says which."""
        original = lightning_watch.probe_shop
        broken = self.shops[0].pk

        def probe_shop(pk):
            if pk == broken:
                raise RuntimeError("unexpected")
            return original(pk)
        with patch("shops.lightning_watch.probe_shop", side_effect=probe_shop), \
                self.assertLogs("shops.management.commands.check_lightning", "ERROR") as logs, \
                self.assertRaisesRegex(CommandError, "1 shops raised an error"):
            self.run_it()
        self.assertIn(str(broken), logs.output[0])
        # The others were probed and recorded before the run ended as failed, which is what makes the unit's alarm ring
        self.assertEqual(Shop.objects.exclude(lightning_checked=None).count(), 2)
        self.assertIsNone(Shop.objects.get(pk=broken).lightning_checked)

    def test_an_owner_who_cannot_be_told_does_not_stop_the_run(self):
        now = timezone.now()
        self.run_it("silent", now=now)
        with patch("shops.lightning_watch.tell_owner", side_effect=RuntimeError("unexpected")), \
                self.assertLogs("shops.management.commands.check_lightning", "ERROR"), \
                self.assertRaisesRegex(CommandError, "3 shops raised an error"):
            self.run_it("silent", now=now + timedelta(hours=1))
        self.assertEqual(Shop.objects.filter(lightning_status="no_answer").count(), 3)

    def test_a_shop_deleted_during_the_run_is_skipped(self):
        original = lightning_watch.probe_shop

        def probe_shop(pk):
            # While the first shop is probed, another one is deleted
            if Shop.objects.count() == 3:
                Shop.objects.exclude(pk=pk).first().delete()
            return original(pk)
        with patch("shops.lightning_watch.probe_shop", side_effect=probe_shop):
            fake, sleep, post, out = self.run_it()
        self.assertIn("2 probed", out)
        self.assertIn("1 skipped", out)


@override_settings(TELEGRAM_BOT_TOKEN="t", TELEGRAM_MODERATION_CHAT="-100")
class Panel(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create(username="k")
        self.client.force_login(self.owner)
        now = timezone.now()
        self.shop = Shop.objects.create(owner=self.owner, name="Bar", lightning_address="bar@example.org",
                                        lightning_status="no_answer", lightning_since=now - timedelta(hours=2),
                                        lightning_checked=now - timedelta(hours=1), lightning_checked_address="bar@example.org",
                                        lightning_failures=2, lightning_worked=now - timedelta(hours=3))
        self.retry = f"/en/shops/lightning/{self.shop.id}/"

    def page(self):
        return self.client.get(PANEL)

    def test_a_fault_shows_what_to_do_and_a_button(self):
        page = self.page()
        self.assertContains(page, 'class="lightning-alert"')
        self.assertContains(page, "Lightning: your address does not answer")
        self.assertContains(page, "Open your wallet")
        html = page.content.decode()
        form = html[html.index(f'action="{self.retry}"'):]
        self.assertIn("csrfmiddlewaretoken", form[:form.index("</form>")])
        # The register stays: the guardian only warns
        self.assertContains(page, f"/en/shops/register/{self.shop.id}/")

    def test_payments_not_confirmed(self):
        Shop.objects.filter(pk=self.shop.pk).update(lightning_status="no_confirmation")
        self.assertContains(self.page(), "the register does not see payments arrive")

    def test_a_working_address_is_one_quiet_line(self):
        # Checked just now: an hour ago is yesterday between midnight and one in the community's time zone
        Shop.objects.filter(pk=self.shop.pk).update(lightning_status="ok", lightning_failures=0, lightning_checked=timezone.now(),
                                                    lightning_worked=timezone.now())
        page = self.page()
        self.assertNotContains(page, "lightning-alert")
        # Said in the merchant's path (shops/journey.py), once
        self.assertContains(page, "Your Lightning address works: checked today at", count=1)

    def test_nothing_for_a_shop_not_watched(self):
        for change in ({"suspended": True}, {"closed_on": date(2020, 1, 1)}, {"lightning_address": "other@example.org"}):
            with self.subTest(change):
                Shop.objects.filter(pk=self.shop.pk).update(**change)
                page = self.page()
                self.assertNotContains(page, "lightning-alert")
                self.assertNotContains(page, "Lightning address checked")
                Shop.objects.filter(pk=self.shop.pk).update(suspended=False, closed_on=None, lightning_address="bar@example.org")

    def test_try_again_and_it_works(self):
        with Provider("ok"), patch("shops.notifications.requests.post") as post:
            post.return_value.status_code = 200
            response = self.client.post(self.retry)
        self.assertRedirects(response, f"{PANEL}#lightning-{self.shop.id}", fetch_redirect_response=False)
        self.shop.refresh_from_db()
        self.assertEqual(self.shop.lightning_status, "ok")
        # The merchant sees it in the panel: no email; the association hears it
        self.assertEqual(mail.outbox, [])
        self.assertIn("works again", post.call_args.kwargs["json"]["text"])
        page = self.page()
        self.assertContains(page, "Checked again just now: it works.")
        self.assertNotContains(page, 'class="notice" role="status">Checked again')

    def test_try_again_and_it_still_does_not_work(self):
        with Provider("silent"), patch("shops.notifications.requests.post") as post:
            self.client.post(self.retry)
        post.assert_not_called()
        self.shop.refresh_from_db()
        self.assertEqual(self.shop.lightning_status, "no_answer")
        self.assertContains(self.page(), "Checked again just now: it still does not work.")

    def test_only_the_owner_only_post(self):
        self.assertEqual(self.client.get(self.retry).status_code, 405)
        other = get_user_model().objects.create(username="other")
        self.client.force_login(other)
        with Provider() as fake:
            self.assertEqual(self.client.post(self.retry).status_code, 404)
        self.assertEqual(fake.urls, [])
        self.client.logout()
        self.assertEqual(self.client.post(self.retry).status_code, 302)
        self.assertEqual(fake.urls, [])

    def test_csrf_is_required(self):
        client = self.client_class(enforce_csrf_checks=True)
        client.force_login(self.owner)
        with Provider() as fake:
            self.assertEqual(client.post(self.retry).status_code, 403)
        self.assertEqual(fake.urls, [])

    def test_a_shop_not_watched_cannot_be_tried(self):
        Shop.objects.filter(pk=self.shop.pk).update(suspended=True)
        with Provider() as fake:
            self.assertEqual(self.client.post(self.retry).status_code, 404)
        self.assertEqual(fake.urls, [])

    def test_a_limit_on_retries(self):
        self.enterContext(steady_limits())
        with Provider("silent") as fake:
            for _ in range(5):
                self.client.post(self.retry)
        self.assertEqual(len(fake.urls), 3)
        self.assertContains(self.page(), "wait ten minutes")

    def test_too_many_checks_at_once(self):
        with patch.object(lightning_watch.PANEL_PROBES, "acquire", return_value=False), Provider() as fake:
            self.client.post(self.retry)
        self.assertEqual(fake.urls, [])
        self.assertContains(self.page(), "try again in a minute")

    def test_other_messages_stay_on_top(self):
        self.client.post(f"/en/shops/edit/{self.shop.id}/", {"name": "Bar", "discount": 0})
        self.assertContains(self.page(), '<p class="notice" role="status">Saved.</p>', html=False)


class Admin(TestCase):
    def setUp(self):
        admin_session(self.client)
        now = timezone.now()
        self.broken = Shop.objects.create(name="Broken", lightning_address="a@example.org",
                                          lightning_status="no_answer", lightning_since=now)
        self.fine = Shop.objects.create(name="Healthy", lightning_address="b@example.org", lightning_status="ok")
        self.unknown = Shop.objects.create(name="Brand new", lightning_address="c@example.org")

    def test_a_column_and_a_filter(self):
        page = self.client.get("/admin/shops/shop/")
        self.assertContains(page, "Does not answer ·")
        self.assertContains(page, "?lightning=broken")
        page = self.client.get("/admin/shops/shop/?lightning=broken")
        self.assertContains(page, "Broken")
        self.assertNotContains(page, "Healthy")
        self.assertNotContains(page, "Brand new")
        self.assertContains(self.client.get("/admin/shops/shop/?lightning=unknown"), "Brand new")
        self.assertNotContains(self.client.get("/admin/shops/shop/?lightning=ok"), "Broken")

    def test_the_status_is_not_edited_here(self):
        page = self.client.get(f"/admin/shops/shop/{self.broken.id}/change/")
        self.assertNotContains(page, 'name="lightning_status"')
        self.assertNotContains(page, 'name="lightning_failures"')
        self.assertContains(page, "Does not answer")


class PreflightNotes(TestCase):
    def test_broken_addresses_are_counted_not_a_stop(self):
        now = timezone.now()
        Shop.objects.create(name="A", lightning_address="a@example.org", lightning_status="no_answer",
                            lightning_checked=now)
        Shop.objects.create(name="B", lightning_address="b@example.org", lightning_status="no_confirmation",
                            lightning_checked=now)
        Shop.objects.create(name="C", lightning_address="c@example.org", lightning_status="no_answer",
                            suspended=True)
        self.assertEqual(lightning_notes(), ["2 shops with a Lightning address that does not work: /admin/shops/shop/?lightning=broken"])

    def test_a_guardian_that_stopped(self):
        Shop.objects.create(name="A", lightning_address="a@example.org", lightning_status="ok",
                            lightning_checked=timezone.now() - timedelta(days=1))
        self.assertIn("timer", lightning_notes()[0])

    def test_nothing_to_say(self):
        self.assertEqual(lightning_notes(), [])

    def test_preflight_says_it_and_passes(self):
        Shop.objects.create(name="A", lightning_address="a@example.org", lightning_status="no_answer",
                            lightning_checked=timezone.now())
        out = StringIO()
        with override_settings(**PREFLIGHT), patch(f"{COMMAND}.get_connection"):
            call_command("preflight", stdout=out, stderr=StringIO())
        self.assertIn("good to know: 1 shops with a Lightning address that does not work", out.getvalue())
        self.assertIn("All ready", out.getvalue())


@override_settings(TELEGRAM_BOT_TOKEN="t", TELEGRAM_MODERATION_CHAT="-100")
class NotTheShopsFault(TestCase):
    """Failures that are not the shop's: the server's own network, a busy provider, an address that never worked, one that goes up and down."""

    def setUp(self):
        self.owner = get_user_model().objects.create(username="k")
        Profile.objects.create(user=self.owner, email="mario@example.org")
        self.shops = [Shop.objects.create(owner=self.owner, name=f"Bar {n}",
                                          lightning_address=f"bar{n}@example.org") for n in range(5)]
        self.now = timezone.now()

    def run_it(self, mode, *args, after=timedelta(0), network=True):
        self.now += after
        out = StringIO()
        with Provider(mode) as fake, patch("shops.management.commands.check_lightning.time") as command_time, \
                patch("shops.notifications.requests.post") as post, \
                patch("shops.lightning_watch.network_ok", return_value=network), \
                patch("shops.lightning_watch.timezone.now", return_value=self.now):
            command_time.monotonic.return_value = 0
            post.return_value.status_code = 200
            call_command("check_lightning", *args, stdout=out, stderr=out)
        return fake, post, out.getvalue()

    def statuses(self):
        return sorted(Shop.objects.values_list("lightning_status", "lightning_failures"))

    def test_a_busy_provider_is_do_not_know(self):
        with Provider("busy"):
            self.assertEqual(probe("bar@example.org"), NO_ANSWER)  # creating a shop: still refused
            self.assertEqual(probe("bar@example.org", busy=lightning_watch.UNSURE), lightning_watch.UNSURE)
        self.run_it("ok")
        fake, post, out = self.run_it("busy", "--all", after=timedelta(hours=6))
        self.assertIn("5 unsure", out)
        self.assertEqual(self.statuses(), [("ok", 0)] * 5)
        self.assertEqual(mail.outbox, [])

    def test_most_addresses_failing_at_once_count_nothing(self):
        self.run_it("ok")
        self.run_it("silent", "--all", after=timedelta(hours=6))
        fake, post, out = self.run_it("silent", "--all", after=timedelta(hours=1))
        self.assertEqual(self.statuses(), [("ok", 0)] * 5)
        self.assertEqual(mail.outbox, [])
        self.assertIn("5 of 5 failed at once", out)
        text = post.call_args.kwargs["json"]["text"]
        self.assertIn("5 of 5 addresses failed at the same time", text)
        self.assertIn("no merchant was told", text)

    def test_a_few_failing_among_many_still_count(self):
        self.run_it("ok")
        broken = self.shops[:2]
        # Two of five fail: not a network problem
        fake_ok, fake_bad = FakeProvider("ok"), FakeProvider("silent")
        router = lambda url, **kwargs: (fake_bad if any(f"lnurlp/{s.lightning_address.split('@')[0]}" in url
                                                         for s in broken) else fake_ok)(url, **kwargs)
        for after in (timedelta(hours=6), timedelta(hours=1)):
            self.now += after
            with public_names(), \
                    patch("shops.lnaddress.requests.Session.get", side_effect=router), \
                    patch("shops.management.commands.check_lightning.time") as command_time, \
                    patch("shops.notifications.requests.post") as post, \
                    patch("shops.lightning_watch.network_ok", return_value=True), \
                    patch("shops.lightning_watch.timezone.now", return_value=self.now):
                command_time.monotonic.return_value = 0
                post.return_value.status_code = 200
                call_command("check_lightning", "--all", stdout=StringIO())
        self.assertEqual(Shop.objects.filter(lightning_status="no_answer").count(), 2)
        self.assertEqual(len(mail.outbox), 2)

    def test_no_names_resolved_no_run(self):
        fake, post, out = self.run_it("silent", network=False)
        self.assertEqual(fake.urls, [])
        self.assertIn("resolves no names", out)
        self.assertEqual(self.statuses(), [("", 0)] * 5)

    def test_network_ok(self):
        shops = lightning_watch.watched()
        with patch("shops.lightning_watch.socket.getaddrinfo", side_effect=OSError) as lookup:
            self.assertFalse(lightning_watch.network_ok(shops))
        # The shops' domain, then Telegram
        self.assertGreaterEqual(lookup.call_count, 2)
        with patch("shops.lightning_watch.socket.getaddrinfo", side_effect=[OSError, [("x",)]]):
            self.assertTrue(lightning_watch.network_ok(shops))
        # Each name within a bound: a DNS that never answers for a shop's domain does not hold the run
        from config.network import SlowName
        with patch("shops.lightning_watch.resolve", side_effect=SlowName("slow")) as lookup:
            self.assertFalse(lightning_watch.network_ok(shops))
        self.assertTrue(lookup.call_args_list)
        self.assertTrue(all(0 < call.kwargs["seconds"] <= lightning_watch.NAME_SECONDS for call in lookup.call_args_list))
        with patch("shops.lightning_watch.resolve", side_effect=SlowName("slow")) as lookup, \
                patch("shops.lightning_watch.monotonic", side_effect=[0, 0, 100, 100]):
            self.assertFalse(lightning_watch.network_ok(shops))
        self.assertEqual(lookup.call_count, 1)

    def test_an_address_that_never_worked_did_not_stop(self):
        shop = self.shops[0]
        with Provider("silent"), patch("shops.lightning_watch.timezone.now", return_value=self.now):
            check(shop.pk)
        with Provider("silent"), patch("shops.lightning_watch.timezone.now", return_value=self.now + timedelta(hours=1)):
            self.assertEqual(check(shop.pk)[1], BROKE)
        self.assertIn("and it did not answer", mail.outbox[0].body)
        self.assertNotIn("stopped", mail.outbox[0].body)
        self.client.force_login(self.owner)
        page = self.client.get(PANEL)
        self.assertContains(page, "Your Lightning address does not answer: we tried it twice")
        self.assertNotContains(page, "stopped answering")

    def test_up_and_down_is_told_a_few_times_a_day(self):
        self.enterContext(steady_limits())
        shop = self.shops[0]
        for _ in range(4):
            for mode, after in (("ok", timedelta(hours=1)), ("silent", timedelta(hours=1)), ("silent", timedelta(hours=1))):
                with Provider(mode), patch("shops.lightning_watch.timezone.now", return_value=self.now + after):
                    self.now += after
                    check(shop.pk)
        self.assertEqual(len(mail.outbox), lightning_watch.ALERTS_PER_DAY)

    def test_preflight_survives_a_database_not_migrated(self):
        from django.db import DatabaseError

        out = StringIO()
        with override_settings(**PREFLIGHT), patch(f"{COMMAND}.lightning_notes", side_effect=DatabaseError("no such column")), \
                patch(f"{COMMAND}.get_connection"):
            call_command("preflight", stdout=out, stderr=StringIO())
        self.assertIn("is migrate missing?", out.getvalue())
        self.assertIn("All ready", out.getvalue())


@override_settings(TELEGRAM_BOT_TOKEN="", TELEGRAM_MODERATION_CHAT="")
class RetryLimits(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create(username="k")
        self.client.force_login(self.owner)
        self.shop = Shop.objects.create(owner=self.owner, name="Bar",
                                        lightning_address="bar@example.org", lightning_status="no_answer",
                                        lightning_since=timezone.now(), lightning_checked=timezone.now(),
                                        lightning_checked_address="bar@example.org", lightning_failures=2)
        self.retry = f"/en/shops/lightning/{self.shop.id}/"

    def notes(self):
        return self.client.get(PANEL).content.decode()

    def test_a_probe_past_its_time_is_do_not_know(self):
        import threading
        release = threading.Event()

        def slow(address, **kwargs):
            release.wait(5)
            return None
        with patch("shops.views.RETRY_SECONDS", 0.2), patch("shops.lightning_watch.lnaddress.probe", side_effect=slow):
            self.client.post(self.retry)
            self.shop.refresh_from_db()
            self.assertEqual(self.shop.lightning_status, "no_answer")
            # The place stays taken until the probe really ends
            self.assertEqual(lightning_watch.PANEL_PROBES._value, 1)
            release.set()
        for _ in range(50):
            if lightning_watch.PANEL_PROBES._value == 2:
                break
            threading.Event().wait(0.05)
        self.assertEqual(lightning_watch.PANEL_PROBES._value, 2)
        self.assertIn("slow or busy right now", self.notes())

    def test_an_address_changed_during_the_probe_is_not_reported(self):
        def changed(address, seconds, done=None):
            Shop.objects.filter(pk=self.shop.pk).update(lightning_address="new@example.net")
            done()
            return None
        with patch("shops.views.lightning_watch.probe_within", side_effect=changed):
            self.client.post(self.retry)
        self.shop.refresh_from_db()
        self.assertEqual(self.shop.lightning_status, "no_answer")
        self.assertIn("changed while it was being checked", self.notes())

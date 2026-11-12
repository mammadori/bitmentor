"""«Something wrong?» (shops/helpdesk.py, shops/help_views.py): who may open the pages, what the page says from the
database alone (no other server asked while it loads), «Look again» with its limits, the request to the association
(the limit, the double tap, hostile text, Telegram that refuses or is not there), what the dashboard and the panel say
about it, the administration, and the diagnosis that stays the same."""
import io
import re
import threading
import time
from contextlib import nullcontext
from datetime import timedelta
from pathlib import Path
from unittest import skipUnless
from unittest.mock import patch

import requests
from config.test_translations import untranslated
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connection, connections
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone, translation

from . import dashboard, diagnosis, helpdesk
from .management.commands import dashboard as dashboard_command
from .models import HelpRequest, LightningStatus, Profile, Shop, StaffRegister
from .notifications import NotificationFailed
from .test_diagnosis import provider
from .tests import admin_session, reset_limits

User = get_user_model()
CALLED_OUT = AssertionError("The help page must not ask another server while it loads")
MODERATORS = "-100500"
# The first second of an hour: the limits count per clock hour, and a test must not straddle two of them
HOUR = 1_800_000_000


def in_one_hour():
    return patch("config.ratelimit.time.time", return_value=HOUR)


def nobody_outside():
    """Any request to another server fails the test."""
    return (patch("requests.sessions.Session.request", side_effect=CALLED_OUT),
            patch("shops.lnaddress.fetch", side_effect=CALLED_OUT))


class Base(TestCase):
    def setUp(self):
        reset_limits()
        self.owner = User.objects.create(username="merchant")
        Profile.objects.create(user=self.owner, email="mario.rossi@example.org")
        self.shop = Shop.objects.create(
            owner=self.owner, name="Central Bar", city="Town", phone="0123 456",
            lightning_address="bar@ln.example.org", lightning=True, telegram_chat="12345", latitude=41.9, longitude=12.5, first_sale=timezone.now())
        self.client.force_login(self.owner)

    def url(self, name, *args, shop=None):
        with translation.override("en"):
            return reverse(name, args=[(shop or self.shop).pk, *args])

    def ask(self, symptom="pay", note="", channel="email", **extra):
        return self.client.post(self.url("help_ask"), {"symptom": symptom, "note": note, "channel": channel}, **extra)


# ---------------------------------------------------------------------------------------------------------------------
# Who opens the pages
# ---------------------------------------------------------------------------------------------------------------------

class Access(Base):
    def routes(self):
        return [("get", self.url("help_home")), ("get", self.url("help_symptom", "pay")),
                ("post", self.url("help_again", "pay")), ("post", self.url("help_ask"))]

    def test_a_stranger_is_sent_to_sign_in_and_nothing_is_made(self):
        self.client.logout()
        for verb, url in self.routes():
            response = getattr(self.client, verb)(url, {"symptom": "pay", "channel": "page"})
            self.assertEqual(response.status_code, 302, url)
            self.assertIn("/shops/", response["Location"])
        self.assertEqual(HelpRequest.objects.count(), 0)

    def test_another_merchant_does_not_see_the_shop_and_cannot_ask_for_it(self):
        other = User.objects.create(username="other")
        self.client.force_login(other)
        for verb, url in self.routes():
            self.assertEqual(getattr(self.client, verb)(url, {"symptom": "pay", "channel": "page"}).status_code, 404, url)
        self.assertEqual(HelpRequest.objects.count(), 0)

    def test_the_staff_is_a_visitor_here_whoever_owns_the_shop(self):
        self.owner.is_staff = True
        self.owner.save()
        self.client.logout()
        admin_session(self.client)
        boss = User.objects.get(username="boss")
        shop = Shop.objects.create(owner=boss, name="Staff shop", lightning_address="x@example.org")
        for verb, url in (("get", self.url("help_home", shop=shop)), ("get", self.url("help_symptom", "pay", shop=shop)),
                          ("post", self.url("help_again", "pay", shop=shop)), ("post", self.url("help_ask", shop=shop))):
            response = getattr(self.client, verb)(url, {"symptom": "pay", "channel": "page"})
            self.assertEqual(response.status_code, 302, url)
            self.assertFalse(response.wsgi_request.user.is_authenticated)
        self.assertEqual(HelpRequest.objects.count(), 0)

    def test_the_pages_are_not_indexed_and_the_asks_are_posts_only(self):
        response = self.client.get(self.url("help_home"))
        self.assertEqual(response["X-Robots-Tag"], "noindex, nofollow")
        self.assertEqual(self.client.get(self.url("help_ask")).status_code, 405)
        self.assertEqual(self.client.get(self.url("help_again", "pay")).status_code, 405)

    def test_an_unknown_symptom_is_not_found(self):
        self.assertEqual(self.client.get(self.url("help_symptom", "nonsense")).status_code, 404)
        self.assertEqual(self.client.post(self.url("help_again", "nonsense")).status_code, 404)
        self.assertEqual(self.ask(symptom="nonsense").status_code, 404)

    def test_the_forms_have_the_csrf_token(self):
        from django.test import Client
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.owner)
        self.assertEqual(strict.post(self.url("help_ask"), {"symptom": "pay", "channel": "page"}).status_code, 403)
        self.assertEqual(strict.post(self.url("help_again", "pay")).status_code, 403)
        self.assertEqual(HelpRequest.objects.count(), 0)
        page = self.client.get(self.url("help_symptom", "pay")).content.decode()
        self.assertEqual(page.count("csrfmiddlewaretoken"), 2)


# ---------------------------------------------------------------------------------------------------------------------
# The page: from the database, in seconds
# ---------------------------------------------------------------------------------------------------------------------

class Pages(Base):
    def test_the_home_has_a_big_button_for_every_symptom(self):
        page = self.client.get(self.url("help_home")).content.decode()
        for key in helpdesk.KINDS:
            self.assertIn(self.url("help_symptom", key), page)
        for text in ("A customer cannot pay", "A payment stays on «waiting»", "The register does not open",
                     "I lost my phone or changed phone", "Too much change given, or a customer paid too much", "Something else"):
            self.assertIn(text, page)

    def test_the_panel_has_the_button_for_every_shop(self):
        other = Shop.objects.create(owner=self.owner, name="No wallet", awaiting_wallet=True)
        page = self.client.get(reverse("shops")).content.decode()
        self.assertIn(self.url("help_home"), page)
        self.assertIn(self.url("help_home", shop=other), page)
        self.assertIn("Something wrong?", page)

    def test_the_page_never_tells_the_changes_made_to_another_shop_that_shares_the_name(self):
        from .models import AccountChange
        stranger = User.objects.create(username="other")
        AccountChange.objects.create(user=stranger, shops="Central Bar", changed="shop_owner,username", via="admin",
                                     actor="boss")
        AccountChange.objects.create(user=self.owner, shops="Central Bar", changed="username", via="admin", actor="boss")
        # The shop of the other account has the same name: its owner's own row is not this shop's owner's
        for key in ("open", "phone", "other"):
            page = self.client.get(self.url("help_symptom", key)).content.decode()
            self.assertNotIn("another account", page, key)
            self.assertNotIn("concerns this shop", page, key)
        # The diagnosis for whoever helps keeps both
        codes = {item.code for item in diagnosis.diagnose(self.shop, only={diagnosis.check_account})}
        self.assertEqual(codes, {"account_changed", "account_changed_other"})
        owner_view = {item.code for item in diagnosis.diagnose(self.shop, only={diagnosis.check_account}, owner_view=True)}
        self.assertEqual(owner_view, {"account_changed"})

    def test_a_confirmation_that_never_comes_is_not_called_a_silence(self):
        Shop.objects.filter(pk=self.shop.pk).update(
            lightning_status=LightningStatus.NO_CONFIRMATION, lightning_checked_address="bar@ln.example.org",
            lightning_since=timezone.now() - timedelta(hours=3), lightning_checked=timezone.now(),
            lightning_worked=timezone.now())
        page = self.client.get(self.url("help_symptom", "waiting")).content.decode()
        self.assertIn("it answers, but it does not tell the register when a payment arrives", page)
        self.assertNotIn("has not answered since", page)

    def test_a_register_opened_before_keeps_taking_payments(self):
        page = self.client.get(self.url("help_symptom", "open")).content.decode()
        self.assertIn("A phone where the register was opened before keeps taking Lightning payments", page)
        self.assertIn('class="help-steps"', page)

    def test_the_owner_has_the_button_on_the_register_too_and_nobody_else(self):
        with translation.override("en"):
            url = reverse("register", args=[self.shop.pk])
        link = self.url("help_home")
        self.assertIn(link, self.client.get(url).content.decode())
        # Whoever opens the register from its link (a phone of the staff, a visitor) sees no way into the owner's pages
        self.client.logout()
        self.assertNotIn(link, self.client.get(url).content.decode())

    def test_no_symptom_page_asks_another_server_while_it_loads(self):
        # A shop that would be probed (store, address) and has everything: still nothing leaves the machine
        first, second = nobody_outside()
        with first as session, second as fetch:
            for key in helpdesk.KINDS:
                response = self.client.get(self.url("help_symptom", key))
                self.assertEqual(response.status_code, 200, key)
        session.assert_not_called()
        fetch.assert_not_called()

    def test_the_page_reads_a_few_queries_whatever_the_symptom(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        for key in helpdesk.KINDS:
            with CaptureQueriesContext(connection) as queries:
                self.client.get(self.url("help_symptom", key))
            self.assertLess(len(queries), 45, key)
            self.assertEqual([q for q in queries.captured_queries if re.match(r"\s*(INSERT|UPDATE|DELETE)", q["sql"], re.I)
                              and "django_session" not in q["sql"]], [], key)

    def test_the_steps_are_there_in_order_and_in_plain_words(self):
        page = self.client.get(self.url("help_symptom", "waiting")).content.decode()
        self.assertEqual(page.count('<li>', page.index('class="help-steps"')), len(helpdesk.KINDS["waiting"].steps))
        self.assertIn("Do not ask the customer to pay again", page)
        for word in ("LUD", "LNURL", "webhook", "BTCPay"):
            self.assertNotIn(word, page)

    def test_a_lightning_address_the_guardian_found_broken_is_the_first_thing_said(self):
        Shop.objects.filter(pk=self.shop.pk).update(
            lightning_status=LightningStatus.NO_ANSWER, lightning_checked_address="bar@ln.example.org",
            lightning_since=timezone.now() - timedelta(hours=3), lightning_checked=timezone.now(),
            lightning_worked=timezone.now() - timedelta(days=1))
        page = self.client.get(self.url("help_symptom", "pay")).content.decode()
        self.assertIn("help-problem", page)
        self.assertIn("cannot pay you", page)
        self.assertLess(page.index("The site watches your Lightning address"), page.index("What to do now"))
        # A problem puts the way to ask for help right under it
        self.assertIn('href="#ask"', page)

    def test_an_address_that_works_is_said_and_no_payment(self):
        now = timezone.now()
        Shop.objects.filter(pk=self.shop.pk).update(
            lightning_status=LightningStatus.OK, lightning_checked_address="bar@ln.example.org", lightning_checked=now,
            lightning_worked=now)
        page = self.client.get(self.url("help_symptom", "waiting")).content.decode()
        self.assertIn("it worked when it last checked", page)
        # The site sees no sale: nothing about the last payment, and no «Sales» page to send the merchant to
        self.assertNotIn("The last payment the site saw", page)
        self.assertNotIn("«Sales»", page)
        # Only the database was read: it does not claim more, and says how to ask the Lightning wallet too
        self.assertIn("Nothing wrong on file.", page)
        self.assertIn("press «Look again»", page)
        self.assertNotIn("Nothing wrong from here.", page)
        self.assertNotIn('href="#ask"', page)

    def test_a_staff_register_never_opened_is_on_the_page_of_the_register_that_does_not_open(self):
        StaffRegister.objects.create(shop=self.shop, name="Anna", code="", opened=None,
                                     code_until=timezone.now() - timedelta(hours=1))
        page = self.client.get(self.url("help_symptom", "open")).content.decode()
        self.assertIn("never opened on a phone", page)
        self.assertIn("Anna", page)

    def test_no_wallet_no_register_is_said(self):
        self.shop.lightning_address = ""
        self.shop.save()
        page = self.client.get(self.url("help_symptom", "pay")).content.decode()
        # No wallet to ask: no «Look again»
        self.assertNotIn("help_again", page)
        self.assertNotIn(self.url("help_again", "pay"), page)

    def test_look_again_is_offered_only_where_there_is_something_to_ask(self):
        self.assertIn(self.url("help_again", "pay"), self.client.get(self.url("help_symptom", "pay")).content.decode())
        # Lost phone: all of it is in the database
        self.assertNotIn("help_again", self.client.get(self.url("help_symptom", "phone")).content.decode())
        self.assertNotIn("/again/", self.client.get(self.url("help_symptom", "phone")).content.decode())

    def test_a_note_with_markup_is_text_on_every_page(self):
        HelpRequest.objects.create(shop=self.shop, asked_by=self.owner, symptom="pay", note="<script>alert(1)</script> <b>x</b>",
                                   channel="page")
        for url in (self.url("help_home"), reverse("shops")):
            page = self.client.get(url).content.decode()
            self.assertNotIn("<script>alert(1)", page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", self.client.get(self.url("help_home")).content.decode())

    def test_the_page_works_without_javascript_and_with_the_security_policy(self):
        for key in helpdesk.KINDS:
            page = self.client.get(self.url("help_symptom", key)).content.decode()
            self.assertNotRegex(page, r"<script[^>]*>[^<]")  # no script in line
            self.assertNotRegex(page, r"\sstyle=")
            self.assertNotRegex(page, r"\son\w+=")
        home = self.client.get(self.url("help_home")).content.decode()
        self.assertNotRegex(home, r"\sstyle=")


# ---------------------------------------------------------------------------------------------------------------------
# Look again
# ---------------------------------------------------------------------------------------------------------------------

@override_settings(TELEGRAM_BOT_TOKEN="bot-token-secret")
class LookAgain(Base):
    def setUp(self):
        super().setUp()
        self.domains = []

    def again(self, key="pay", fetch=None):
        fetch = fetch or provider()
        with patch("shops.lnaddress.fetch", side_effect=lambda url: (self.domains.append(url.split("/")[2]), fetch(url))[1]):
            return self.client.post(self.url("help_again", key))

    def test_it_asks_the_address(self):
        response = self.again("waiting")
        self.assertEqual(response.status_code, 200)
        page = response.content.decode()
        self.assertIn("Checked just now", page)
        self.assertIn("Your Lightning address answers and tells the register when a payment arrives", page)
        self.assertEqual(set(self.domains), {"ln.example.org"})

    def test_a_wallet_that_is_down_is_told_in_plain_words(self):
        page = self.again("pay", provider(fail=requests.ConnectionError("refused"))).content.decode()
        self.assertIn("Your Lightning wallet is not answering", page)
        self.assertIn("help-problem", page)

    def test_no_secret_reaches_the_page(self):
        page = self.again("other").content.decode()
        for secret in ("bot-token-secret",):
            self.assertNotIn(secret, page)

    def test_it_does_not_run_for_a_shop_without_a_register_or_a_symptom_with_nothing_to_ask(self):
        with patch("shops.lnaddress.fetch", side_effect=CALLED_OUT):
            response = self.client.post(self.url("help_again", "phone"))
        self.assertEqual(response.status_code, 302)
        Shop.objects.filter(pk=self.shop.pk).update(lightning_address="")
        with patch("shops.lnaddress.fetch", side_effect=CALLED_OUT):
            self.assertEqual(self.client.post(self.url("help_again", "pay")).status_code, 302)

    def test_a_shop_may_look_six_times_an_hour(self):
        with in_one_hour():
            for _ in range(6):
                self.assertEqual(self.again("pay").status_code, 200)
            calls = len(self.domains)
            response = self.again("pay")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(self.domains), calls)
        page = self.client.get(response["Location"]).content.decode()
        self.assertIn("You have looked many times in a short while", page)

    def test_the_limit_is_the_shops_and_the_persons_not_one_for_everybody(self):
        other_owner = User.objects.create(username="other")
        other = Shop.objects.create(owner=other_owner, name="Other", lightning_address="x@example.org")
        with in_one_hour():
            for _ in range(6):
                self.again("pay")
        self.client.force_login(other_owner)
        with in_one_hour(), patch("shops.lnaddress.fetch", side_effect=provider()):
            self.assertEqual(self.client.post(self.url("help_again", "pay", shop=other)).status_code, 200)

    def test_a_slow_check_is_cut_by_the_time_limit(self):
        release = threading.Event()

        def slow(url):
            release.wait(5)
            raise requests.ReadTimeout("slow")
        started = time.monotonic()
        try:
            with patch("shops.lnaddress.fetch", side_effect=slow), patch("shops.helpdesk.LOOK_SECONDS", 1):
                page = self.client.post(self.url("help_again", "pay")).content.decode()
        finally:
            release.set()
        self.assertLess(time.monotonic() - started, 4)
        self.assertIn("Your Lightning wallet takes too long", page)


# ---------------------------------------------------------------------------------------------------------------------
# The request
# ---------------------------------------------------------------------------------------------------------------------

@override_settings(TELEGRAM_BOT_TOKEN="bot-token-secret", TELEGRAM_MODERATION_CHAT=MODERATORS,
                   PUBLIC_URL="https://shops.example.org")
class Asking(Base):
    def setUp(self):
        super().setUp()
        self.sent = []
        patcher = patch("shops.helpdesk.telegram", side_effect=self.telegram)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.answer = True

    def telegram(self, chat, text):
        self.sent.append((chat, text))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    def go(self, symptom="waiting", note="", channel="email", fetch=None):
        with patch("shops.lnaddress.fetch", side_effect=fetch or provider()):
            return self.ask(symptom, note, channel)

    def test_a_request_is_made_with_the_symptom_the_line_and_the_way_to_call(self):
        response = self.go("waiting", "The customer paid at 10:20 and it waits", "email")
        self.assertRedirects(response, self.url("help_home"), fetch_redirect_response=False)
        row = HelpRequest.objects.get()
        self.assertEqual((row.shop, row.symptom, row.channel, row.resolved), (self.shop, "waiting", "email", None))
        self.assertEqual(row.note, "The customer paid at 10:20 and it waits")
        self.assertEqual(row.delivery, "sent")
        self.assertIn("[lightning_ok]", row.summary)
        self.assertIn("[register_ready]", row.summary)

    def test_the_moderators_are_told_at_once_with_the_codes_and_the_link_and_nothing_private(self):
        self.go("pay", "No code on the screen", "telegram", provider(fail=requests.ConnectionError("down")))
        self.assertEqual(len(self.sent), 1)
        chat, text = self.sent[0]
        self.assertEqual(chat, MODERATORS)
        row = HelpRequest.objects.get()
        for part in ("Central Bar asks for help.", "Symptom: A customer cannot pay", "Call back: on Telegram",
                     "They wrote: «No code on the screen»", "Problems: lightning_no_answer",
                     f"https://shops.example.org/admin/shops/helprequest/{row.pk}/change/"):
            self.assertIn(part, text)
        for private in ("mario.rossi", "12345", "0123 456", "bar@ln.example.org", "bot-token-secret"):
            self.assertNotIn(private, text)

    def test_no_secret_is_kept_on_the_request_either(self):
        self.go("other", "x", "page")
        row = HelpRequest.objects.get()
        for secret in ("bot-token-secret", "mario.rossi@example.org"):
            self.assertNotIn(secret, row.summary)

    def test_the_text_of_the_merchant_cannot_become_a_mention_a_link_or_a_command(self):
        self.go("pay", "@everyone see https://evil.example/x t.me/evil www.evil.example /start@bot", "page")
        text = self.sent[0][1]
        wrote = next(line for line in text.splitlines() if line.startswith("They wrote"))
        self.assertNotIn("@", wrote)
        self.assertNotIn("evil", wrote)
        self.assertNotIn("http", wrote)
        self.assertEqual(wrote.count("«"), 1)

    def test_the_line_is_one_line_of_plain_text(self):
        self.go("pay", "first\nsecond\tthird \x1b[31m red \u202e rtl <b>bold</b>", "page")
        row = HelpRequest.objects.get()
        self.assertNotRegex(row.note, r"[\x00-\x1f\x7f\u202e]")
        self.assertIn("first second third", row.note)
        self.assertIn("<b>bold</b>", row.note)  # kept as text: escaped wherever it is shown
        self.assertNotIn("\x1b", self.sent[0][1])

    def test_a_shop_name_cannot_forge_the_message_either(self):
        Shop.objects.filter(pk=self.shop.pk).update(name="Bar\n@admin: /ban https://evil.example")
        self.go("pay", "", "page")
        first = self.sent[0][1].splitlines()[0]
        self.assertNotIn("evil", first)
        self.assertNotIn("@admin", first)

    def test_a_line_that_is_too_long_or_hides_a_secret_is_refused_and_nothing_is_made(self):
        response = self.go("pay", "a" * 201, "page")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Please keep it to a line", response.content.decode())
        seed = "abandon ability able about above absent absorb abstract absurd abuse access accident"
        response = self.go("pay", seed, "page")
        self.assertIn("never write it here", response.content.decode())
        self.assertNotIn("abandon ability", response.content.decode().replace('value="' + seed + '"', ""))
        response = self.go("pay", "xprv9s21ZrQH143K3QTDL4LXw2F7HEK3wJUD2nW2nRk4stbPy6cq3jPPqjiChkVvvNKmPGJxWUtg6LnF5kejMRNNU3TGtRBeJgk33yuGBxrMPHi", "page")
        self.assertIn("never write it here", response.content.decode())
        self.assertEqual(HelpRequest.objects.count(), 0)
        self.assertEqual(self.sent, [])

    def test_the_channel_must_be_one_the_shop_has(self):
        Shop.objects.filter(pk=self.shop.pk).update(telegram_chat="", phone="")
        for bad in ("telegram", "phone", "carrier-pigeon", ""):
            response = self.go("pay", "", bad)
            self.assertEqual(response.status_code, 200, bad)
        self.assertEqual(HelpRequest.objects.count(), 0)
        Profile.objects.filter(user=self.owner).update(email="")
        self.assertEqual(self.go("pay", "", "email").status_code, 200)
        self.assertEqual(self.go("pay", "", "page").status_code, 302)

    def test_the_form_offers_what_the_shop_has_with_the_email_masked(self):
        page = self.client.get(self.url("help_symptom", "pay")).content.decode()
        self.assertIn("By email, to m***@e***.org", page)
        self.assertNotIn("mario.rossi", page)
        self.assertIn("On Telegram, in the chat of your shop", page)
        self.assertNotIn("12345", page)
        self.assertIn("By phone, to 0123 456", page)
        self.assertIn("I will look at my panel", page)
        Shop.objects.filter(pk=self.shop.pk).update(telegram_chat="", phone="")
        Profile.objects.filter(user=self.owner).update(email="")
        page = self.client.get(self.url("help_symptom", "pay")).content.decode()
        self.assertNotIn("On Telegram", page)
        self.assertNotIn("By email", page)
        self.assertIn("I will look at my panel", page)

    # --- Telegram does not take it -------------------------------------------------------------------------------

    def test_telegram_that_refuses_keeps_the_request_and_the_merchant_knows(self):
        self.answer = False
        response = self.go("pay", "", "email")
        row = HelpRequest.objects.get()
        self.assertEqual(row.delivery, "refused")
        page = self.client.get(response["Location"]).content.decode()
        self.assertIn("it is saved", page)
        self.assertIn("did not go out", page)
        panel = self.client.get(reverse("shops")).content.decode()
        self.assertIn("You asked for help", panel)
        self.assertIn("even if the message to them did not go out", panel)
        self.assertEqual([item.code for item in dashboard.summary()["items"]][:1], ["help_request"])

    def test_telegram_that_fails_or_raises_or_is_not_there(self):
        for answer, state in ((NotificationFailed("503"), "failed"), (RuntimeError("boom"), "failed"), (None, "off")):
            HelpRequest.objects.all().delete()
            reset_limits()
            self.answer = answer
            with self.assertLogs("shops.helpdesk", "ERROR") if isinstance(answer, RuntimeError) else nullcontext():
                self.go("pay", "", "page")
            row = HelpRequest.objects.get()
            self.assertEqual(row.delivery, state)
            self.assertEqual(dashboard.summary()["items"][0].code, "help_request")

    def test_without_a_bot_or_a_chat_the_request_is_on_the_dashboard_and_says_so(self):
        self.answer = None
        response = self.go("pay", "", "page")
        self.assertEqual(HelpRequest.objects.get().delivery, "off")
        self.assertIn("did not go out", self.client.get(response["Location"]).content.decode())
        self.assertEqual(dashboard.summary()["items"][0].shop, self.shop)

    def test_a_training_shop_never_writes_to_the_moderators(self):
        Shop.objects.filter(pk=self.shop.pk).update(external_id="bitmentor-demo:1")
        self.go("pay", "", "page")
        self.assertEqual(self.sent, [])
        self.assertEqual(HelpRequest.objects.get().delivery, "off")

    def test_a_diagnosis_that_breaks_never_loses_the_request(self):
        with patch("shops.diagnosis.diagnose", side_effect=RuntimeError("boom")), self.assertLogs("shops.helpdesk", "ERROR"):
            response = self.ask("pay", "help", "page")
        self.assertEqual(response.status_code, 302)
        row = HelpRequest.objects.get()
        self.assertIn("could not be made", row.summary)
        self.assertEqual(row.delivery, "sent")

    def test_no_free_place_for_a_probe_is_not_a_problem_of_the_shop(self):
        from . import diagnosis as module
        places = threading.BoundedSemaphore(1)
        places.acquire()
        with patch.object(module, "THREADS", places), patch.object(module, "LIGHTNING_SECONDS", 0.5):
            self.go("pay", "", "page")
        row = HelpRequest.objects.get()
        self.assertNotIn("lightning_slow", row.summary)
        self.assertIn("[time_up]", row.summary)
        self.assertNotIn("lightning_slow", self.sent[0][1])
        with patch.object(module, "THREADS", places), patch.object(module, "LIGHTNING_SECONDS", 0.5), \
                patch("shops.helpdesk.LOOK_SECONDS", 3):
            page = self.client.post(self.url("help_again", "pay")).content.decode()
        self.assertNotIn("Your Lightning wallet takes too long", page)
        self.assertIn("did not finish in time", page)

    def test_a_wallet_that_is_down_still_gets_the_request_in_a_few_seconds(self):
        started = time.monotonic()
        self.go("pay", "", "page", provider(fail=requests.ConnectionError("refused")))
        self.assertLess(time.monotonic() - started, 5)
        row = HelpRequest.objects.get()
        self.assertIn("[lightning_no_answer]", row.summary)
        self.assertIn("Problems: lightning_no_answer", self.sent[0][1])

    def test_the_request_uses_its_own_time_limit(self):
        seen = []
        real = diagnosis.diagnose

        def spy(shop, seconds, only=None):
            seen.append(seconds)
            return real(shop, seconds, only=only)
        with patch("shops.diagnosis.diagnose", side_effect=spy):
            self.go("phone", "", "page")
        self.assertEqual(seen, [helpdesk.ASK_SECONDS])

    # --- The limit ------------------------------------------------------------------------------------------------

    def test_a_shop_asks_three_times_a_day_and_the_fourth_is_kept_out(self):
        for symptom in ("pay", "waiting", "open"):
            self.assertEqual(self.go(symptom, "", "page").status_code, 302)
        response = self.go("phone", "", "page")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(HelpRequest.objects.count(), 3)
        self.assertEqual(len(self.sent), 3)
        page = self.client.get(response["Location"]).content.decode()
        self.assertIn("You have already asked 3 times today", page)
        self.assertIn("write to", page)

    def test_the_day_is_a_rolling_one_not_a_calendar_one(self):
        for symptom in ("pay", "waiting", "open"):
            self.go(symptom, "", "page")
        HelpRequest.objects.filter(symptom="pay").update(created=timezone.now() - timedelta(hours=25))
        self.assertEqual(self.go("phone", "", "page").status_code, 302)
        self.assertEqual(HelpRequest.objects.count(), 4)

    def test_the_limit_is_the_shops_own(self):
        for symptom in ("pay", "waiting", "open"):
            self.go(symptom, "", "page")
        other_owner = User.objects.create(username="other")
        other = Shop.objects.create(owner=other_owner, name="Other", lightning_address="x@example.org")
        self.client.force_login(other_owner)
        self.client.post(self.url("help_ask", shop=other), {"symptom": "pay", "channel": "page"})
        self.assertEqual(other.help_requests.count(), 1)

    def test_a_double_tap_is_one_request_and_one_message(self):
        self.go("pay", "same", "page")
        response = self.go("pay", "same", "page")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(HelpRequest.objects.count(), 1)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("You asked a moment ago", self.client.get(response["Location"]).content.decode())

    def test_the_same_symptom_asked_later_is_a_new_request(self):
        self.go("pay", "", "page")
        HelpRequest.objects.update(created=timezone.now() - timedelta(minutes=10))
        self.go("pay", "", "page")
        self.assertEqual(HelpRequest.objects.count(), 2)

    def test_a_script_is_stopped_before_the_diagnosis(self):
        with in_one_hour(), patch("shops.help_views.ASKS_PER_USER", 2), patch("shops.diagnosis.diagnose") as made:
            made.return_value = []
            for _ in range(4):
                self.ask("pay", "", "page")
        self.assertLessEqual(made.call_count, 2)
        self.assertLessEqual(HelpRequest.objects.count(), 2)

    def test_the_count_is_made_under_a_lock_on_the_shop(self):
        with patch.object(Shop.objects, "select_for_update", wraps=Shop.objects.select_for_update) as lock:
            helpdesk.create(self.shop, "pay", "", "page", self.owner)
        lock.assert_called_once()

    # --- What the merchant and the association see afterwards -----------------------------------------------------

    def test_the_merchant_sees_the_time_in_the_panel_and_then_solved(self):
        self.go("pay", "", "page")
        panel = self.client.get(reverse("shops")).content.decode()
        self.assertRegex(panel, r"You asked for help today at \d\d:\d\d\.")
        self.assertIn("The association has been told.", panel)
        row = HelpRequest.objects.get()
        row.resolved = timezone.now()
        row.resolved_by = "volunteer"
        row.save()
        panel = self.client.get(reverse("shops")).content.decode()
        self.assertRegex(panel, r"Solved today at \d\d:\d\d\.")
        self.assertNotIn("You asked for help", panel)
        self.assertNotIn("volunteer", panel)
        # A few days later the panel is quiet again, the page keeps the history
        HelpRequest.objects.update(resolved=timezone.now() - timedelta(days=4))
        self.assertNotIn("Solved ", self.client.get(reverse("shops")).content.decode())
        self.assertIn("Solved", self.client.get(self.url("help_home")).content.decode())

    def test_the_messages_to_the_moderators_have_a_ceiling_for_all_the_shops_and_for_each_network(self):
        # The ceiling for all: the third request of whoever it is is saved and only on the dashboard
        with in_one_hour(), patch("shops.help_views.MESSAGES_IN_ALL", 2):
            for number in range(3):
                owner = User.objects.create(username=f"t{number}")
                shop = Shop.objects.create(owner=owner, name=f"Shop {number}", lightning_address="x@example.org")
                self.client.force_login(owner)
                response = self.client.post(self.url("help_ask", shop=shop), {"symptom": "phone", "channel": "page"},
                                            REMOTE_ADDR=f"203.0.113.{number}")
                self.assertEqual(response.status_code, 302)
        self.assertEqual(len(self.sent), 2)
        last = HelpRequest.objects.get(shop__name="Shop 2")
        self.assertEqual((last.delivery, last.resolved), ("failed", None))
        self.assertEqual(dashboard.summary()["items"][0].code, "help_request")
        self.assertIn("did not go out", self.client.get(self.url("help_home", shop=last.shop)).content.decode())
        # And for a network: the same address, any number of shops
        self.sent.clear()
        with in_one_hour(), patch("shops.help_views.MESSAGES_PER_IP", 1):
            for number in range(2):
                owner = User.objects.create(username=f"u{number}")
                shop = Shop.objects.create(owner=owner, name=f"Net {number}", lightning_address="x@example.org")
                self.client.force_login(owner)
                self.client.post(self.url("help_ask", shop=shop), {"symptom": "phone", "channel": "page"},
                                 REMOTE_ADDR="198.51.100.7", HTTP_X_FORWARDED_FOR="198.51.100.7")
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(HelpRequest.objects.get(shop__name="Net 1").delivery, "failed")

    def test_a_second_tap_with_another_line_adds_it_to_the_request(self):
        self.go("pay", "first line", "page")
        response = self.go("pay", "and the second", "page")
        row = HelpRequest.objects.get()
        self.assertEqual(row.note, "first line / and the second")
        self.assertEqual(len(self.sent), 1)
        self.assertIn("we added what you wrote", self.client.get(response["Location"]).content.decode())
        # The same line again, or none: nothing more is added
        self.go("pay", "and the second", "page")
        self.go("pay", "", "page")
        self.assertEqual(HelpRequest.objects.get().note, "first line / and the second")
        # Never beyond what a line may hold
        self.go("pay", "z" * 190, "page")
        self.assertLessEqual(len(HelpRequest.objects.get().note), helpdesk.NOTE_LENGTH)

    def test_the_message_names_the_site_first(self):
        from .management.commands.dashboard import site_label
        self.go("pay", "", "page")
        lines = self.sent[0][1].splitlines()
        self.assertEqual(lines[0], site_label())
        self.assertEqual(lines[1], "Central Bar asks for help.")

    def test_a_request_is_named_by_its_shop_and_its_symptom(self):
        self.go("pay", "", "page")
        self.assertEqual(str(HelpRequest.objects.get()), "Central Bar: A customer cannot pay")

    def test_the_symptom_page_says_a_request_is_open(self):
        self.go("pay", "", "page")
        page = self.client.get(self.url("help_symptom", "waiting")).content.decode()
        self.assertIn("You asked for help today at", page)

    def test_only_the_owners_own_requests_show(self):
        other_owner = User.objects.create(username="other")
        other = Shop.objects.create(owner=other_owner, name="Other", lightning_address="x@example.org")
        HelpRequest.objects.create(shop=other, asked_by=other_owner, symptom="pay", note="private line of another", channel="page")
        self.assertNotIn("private line of another", self.client.get(self.url("help_home")).content.decode())
        self.assertNotIn("private line of another", self.client.get(reverse("shops")).content.decode())


# ---------------------------------------------------------------------------------------------------------------------
# The association: the dashboard and the administration
# ---------------------------------------------------------------------------------------------------------------------

@override_settings(TELEGRAM_BOT_TOKEN="", TELEGRAM_MODERATION_CHAT="")
class Association(Base):
    def make(self, shop=None, minutes=0, **fields):
        values = {"symptom": "pay", "note": "", "channel": "email", "summary": "✗ [lightning_no_answer] x\n    detail",
                  "asked_by": (shop or self.shop).owner}
        values.update(fields)
        return HelpRequest.objects.create(shop=shop or self.shop, created=timezone.now() - timedelta(minutes=minutes), **values)

    def test_the_open_requests_are_on_top_of_the_dashboard_whatever_else_is_wrong(self):
        # A broken address since long ago would be first by itself
        broken = Shop.objects.create(
            owner=User.objects.create(username="b"), name="Broken", 
            lightning_address="b@example.org", latitude=1, longitude=2, first_sale=timezone.now(),
            lightning_status=LightningStatus.NO_ANSWER, lightning_since=timezone.now() - timedelta(days=30),
            lightning_checked=timezone.now(), lightning_worked=timezone.now() - timedelta(days=40),
            lightning_checked_address="b@example.org")
        first = self.make(minutes=30, note="older")
        second = self.make(shop=broken, minutes=5, symptom="waiting", note="newer")
        items = dashboard.summary()["items"]
        self.assertEqual([item.code for item in items[:2]], ["help_request", "help_request"])
        self.assertEqual([item.shop for item in items[:2]], [self.shop, broken])
        self.assertIn("lightning_no_answer", [item.code for item in items[2:]])
        self.assertEqual(items[0].level, dashboard.PROBLEM)
        self.assertEqual(items[0].what, "Asked for help: A customer cannot pay.")
        self.assertEqual(items[0].wrote, "older")
        self.assertIn("by email", items[0].do)
        self.assertTrue(items[0].command.endswith(f"diagnose_shop {self.shop.pk}"))
        self.assertEqual(items[0].links[0]["url"], reverse("admin:shops_helprequest_change", args=[first.pk]))
        self.assertEqual(dashboard.summary()["counts"]["to_look"], 2)
        self.assertEqual(second.resolved, None)

    def test_a_solved_request_leaves_the_dashboard(self):
        row = self.make()
        self.assertEqual(len(dashboard.help_items(timezone.now())), 1)
        row.resolved = timezone.now()
        row.save()
        self.assertEqual(dashboard.help_items(timezone.now()), [])

    def test_the_dashboard_page_shows_the_request_and_what_the_merchant_wrote_as_text(self):
        self.make(note="<img src=x onerror=alert(1)>")
        admin_session(self.client)
        page = self.client.get(reverse("admin_dashboard")).content.decode()
        self.assertIn("Asked for help: A customer cannot pay.", page)
        self.assertIn("The merchant wrote:", page)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", page)
        self.assertNotIn("<img src=x", page)
        self.assertIn(reverse("admin:shops_helprequest_changelist").rstrip("/"), page.replace("change/", ""))

    def test_the_text_command_and_the_morning_message_never_carry_the_line(self):
        self.make(note="secret line \x1b[31m red")
        out = io.StringIO()
        with self.assertRaises(SystemExit):
            call_command("dashboard", stdout=out)
        text = out.getvalue()
        self.assertIn("Asked for help: A customer cannot pay.", text)
        self.assertIn("secret line", text)
        self.assertNotIn("\x1b", text)
        _subject, message = dashboard_command.message(dashboard.summary()["items"], set(), 0)
        self.assertIn("Asked for help: A customer cannot pay.", message)
        self.assertNotIn("secret line", message)

    def test_the_administration_lists_reads_and_solves(self):
        row = self.make(note="line", summary="✗ [lightning_no_answer] The wallet is not answering\n    detail of it")
        admin_session(self.client)
        listing = self.client.get(reverse("admin:shops_helprequest_changelist"))
        self.assertContains(listing, "Central Bar")
        self.assertContains(self.client.get(reverse("admin:shops_helprequest_changelist") + "?state=open"), "Central Bar")
        self.assertNotContains(self.client.get(reverse("admin:shops_helprequest_changelist") + "?state=solved"), "Central Bar")
        detail = self.client.get(reverse("admin:shops_helprequest_change", args=[row.pk]))
        self.assertContains(detail, "lightning_no_answer")
        self.assertContains(detail, "detail of it")
        self.assertContains(detail, "mario.rossi@example.org")
        self.assertNotContains(detail, "whsec-very-secret")
        self.assertEqual(self.client.get(reverse("admin:shops_helprequest_add")).status_code, 403)
        self.assertEqual(self.client.post(reverse("admin:shops_helprequest_delete", args=[row.pk]), {"post": "yes"}).status_code, 403)
        # Nothing can be edited from the form of one request
        self.client.post(reverse("admin:shops_helprequest_change", args=[row.pk]), {"note": "changed", "resolved_0": "2026-01-01"})
        row.refresh_from_db()
        self.assertEqual((row.note, row.resolved), ("line", None))

    def test_solving_keeps_who_and_when_and_can_be_undone(self):
        row = self.make()
        admin_session(self.client)
        url = reverse("admin:shops_helprequest_changelist")
        before = timezone.now()
        self.client.post(url, {"action": "solve", "_selected_action": [row.pk]})
        row.refresh_from_db()
        self.assertGreaterEqual(row.resolved, before)
        self.assertEqual(row.resolved_by, "boss")
        stamp = row.resolved
        self.client.post(url, {"action": "solve", "_selected_action": [row.pk]})
        row.refresh_from_db()
        self.assertEqual(row.resolved, stamp)
        self.client.post(url, {"action": "reopen", "_selected_action": [row.pk]})
        row.refresh_from_db()
        self.assertEqual((row.resolved, row.resolved_by), (None, ""))

    def test_the_link_to_the_request_is_only_for_who_can_open_it(self):
        from django.contrib.auth.models import Permission
        from config.guard import ADMIN_SESSION
        self.make()
        moderator = User.objects.create_user("moderator", password="x", is_staff=True)
        moderator.user_permissions.add(Permission.objects.get(codename="view_shop"))
        self.client.logout()
        self.client.force_login(moderator)
        session = self.client.session
        session[ADMIN_SESSION] = True
        session.save()
        page = self.client.get(reverse("admin_dashboard")).content.decode()
        self.assertIn("Asked for help", page)
        self.assertNotIn("/admin/shops/helprequest/", page)
        moderator.user_permissions.add(Permission.objects.get(codename="view_helprequest"))
        moderator = User.objects.get(pk=moderator.pk)
        self.client.force_login(moderator)
        session = self.client.session
        session[ADMIN_SESSION] = True
        session.save()
        self.assertIn("/admin/shops/helprequest/", self.client.get(reverse("admin_dashboard")).content.decode())

    def test_the_actions_need_the_permission_to_change(self):
        from django.contrib.auth.models import Permission
        from config.guard import ADMIN_SESSION
        row = self.make()
        reader = User.objects.create_user("reader", password="x", is_staff=True)
        reader.user_permissions.add(Permission.objects.get(codename="view_helprequest"))
        self.client.logout()
        self.client.force_login(reader)
        session = self.client.session
        session[ADMIN_SESSION] = True
        session.save()
        url = reverse("admin:shops_helprequest_changelist")
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertNotContains(self.client.get(url), "Mark as solved")
        self.client.post(url, {"action": "solve", "_selected_action": [row.pk]})
        row.refresh_from_db()
        self.assertIsNone(row.resolved)

    def test_only_what_the_owner_asked_shows_after_the_shop_changes_hands(self):
        self.make(note="the old owner's line")
        new = User.objects.create(username="newcomer")
        Shop.objects.filter(pk=self.shop.pk).update(owner=new)
        self.client.force_login(new)
        for url in (self.url("help_home"), reverse("shops"), self.url("help_symptom", "pay")):
            page = self.client.get(url).content.decode()
            self.assertNotIn("the old owner", page)
            self.assertNotIn("You asked for help", page)
        # The association still sees it
        self.assertEqual(dashboard.summary()["items"][0].wrote, "the old owner's line")

    def test_a_staff_account_without_the_permission_cannot_see_them(self):
        row = self.make()
        staff = User.objects.create_user("volunteer", password="x", is_staff=True)
        self.client.logout()
        self.client.force_login(staff)
        session = self.client.session
        from config.guard import ADMIN_SESSION
        session[ADMIN_SESSION] = True
        session.save()
        self.assertEqual(self.client.get(reverse("admin:shops_helprequest_changelist")).status_code, 403)
        self.assertEqual(self.client.post(reverse("admin:shops_helprequest_changelist"),
                                          {"action": "solve", "_selected_action": [row.pk]}).status_code, 403)

    def test_a_deleted_shop_takes_its_requests(self):
        self.make()
        self.shop.delete()
        self.assertEqual(HelpRequest.objects.count(), 0)


# ---------------------------------------------------------------------------------------------------------------------
# The diagnosis stays the same
# ---------------------------------------------------------------------------------------------------------------------

@override_settings(TELEGRAM_BOT_TOKEN="bot-token-secret")
class DiagnosisUnchanged(Base):
    def test_without_only_every_check_runs_and_with_it_only_those(self):
        with patch("shops.lnaddress.fetch", side_effect=provider()):
            everything = {finding.code for finding in diagnosis.diagnose(self.shop)}
            some = {finding.code for finding in diagnosis.diagnose(self.shop, only={diagnosis.check_shop, diagnosis.check_owner})}
        self.assertIn("lightning_ok", everything)
        self.assertEqual(some, {"shop_visible", "owner_recovery_email"})

    def test_the_local_checks_of_every_symptom_never_ask_another_server(self):
        first, second = nobody_outside()
        # The diagnosis catches what a check raises: the stand-ins must also not have been called at all
        with first as requested, second as fetched:
            for found in helpdesk.KINDS.values():
                self.assertTrue(helpdesk.look(self.shop, found), found.key)
        requested.assert_not_called()
        fetched.assert_not_called()

    def test_every_check_a_symptom_names_exists(self):
        for found in helpdesk.KINDS.values():
            self.assertTrue(helpdesk.checks(found, True) >= helpdesk.checks(found, False))
            self.assertEqual(len(helpdesk.checks(found, True)), len(set(found.local) | set(found.deep)))
        self.assertEqual({found.key for found in helpdesk.KINDS.values()}, set(HelpRequest.Symptom.values))

    def test_a_symptom_that_names_a_check_that_asks_a_server_has_it_in_deep_not_local(self):
        slow = {"lightning"}
        for found in helpdesk.KINDS.values():
            self.assertFalse(slow & set(found.local), found.key)


# ---------------------------------------------------------------------------------------------------------------------
# Words in three languages
# ---------------------------------------------------------------------------------------------------------------------

class Translations(TestCase):
    """Every string of the pages and of the symptoms is translated into Italian and German: the .po files are written by
    hand, and a string added without them shows in English."""
    def python_ids(self):
        found = set()
        for name in ("helpdesk.py", "help_views.py", "dashboard.py", "admin.py", "models.py"):
            text = (Path(__file__).parent / name).read_text()
            if name in ("dashboard.py", "admin.py", "models.py"):
                text = text[text.index("class HelpRequest") if name == "models.py" else 0:]
            for match in re.finditer(r"""(?:\b_|gettext_lazy)\(\s*((?:"(?:[^"\\]|\\.)*"\s*)+)""", text):
                pieces = re.findall(r'"((?:[^"\\]|\\.)*)"', match.group(1))
                found.add("".join(pieces).replace('\\"', '"'))
        return found

    def test_italian_and_german(self):
        # The templates are in config/test_translations.py, with every other one
        ids = self.python_ids()
        self.assertGreater(len(ids), 40)
        untranslated(self, ids)

    def test_the_pages_speak_italian_and_the_strings_german(self):
        owner = User.objects.create(username="t")
        shop = Shop.objects.create(owner=owner, name="Bar", lightning_address="x@example.org")
        self.client.force_login(owner)
        page = self.client.get(f"/it/shops/help/{shop.pk}/").content.decode()
        self.assertIn("Qualcosa non va?", page)
        self.assertIn("Il cliente non riesce a pagare", page)
        self.assertNotIn("What is happening?", page)
        symptom = self.client.get(f"/it/shops/help/{shop.pk}/waiting/").content.decode()
        for english in ("What to do now", "Ask the association for help", "Do not ask the customer to pay again"):
            self.assertNotIn(english, symptom)
        self.assertIn("Non chiedere al cliente di pagare di nuovo", symptom)
        # The example community has no German site: the strings are the ones the communities that have it will show
        with translation.override("de"):
            from django.utils.translation import gettext
            self.assertEqual(gettext("Something wrong?"), "Stimmt etwas nicht?")
            self.assertEqual(str(HelpRequest.Symptom.WAITING.label), "Eine Zahlung bleibt auf „Wartet“")


@skipUnless(connection.vendor == "postgresql", "two connections at once need PostgreSQL")
class TwoAtOnce(TransactionTestCase):
    """Two taps on two connections, both for the last place of the day: one request is made, the other is refused."""

    def test_the_last_place_of_the_day(self):
        owner = User.objects.create(username="merchant")
        shop = Shop.objects.create(owner=owner, name="Bar", lightning_address="x@example.org")
        for symptom in ("pay", "waiting"):
            HelpRequest.objects.create(shop=shop, asked_by=owner, symptom=symptom, channel="page")
        start = threading.Barrier(2)
        real = HelpRequest.objects.create
        results = []

        def slow_create(**fields):
            time.sleep(0.5)
            return real(**fields)

        def ask(symptom):
            try:
                start.wait()
                results.append(helpdesk.create(Shop.objects.get(pk=shop.pk), symptom, "", "page", owner)[1])
            except helpdesk.TooMany:
                results.append("too many")
            finally:
                connections.close_all()

        with patch.object(HelpRequest.objects, "create", side_effect=slow_create):
            threads = [threading.Thread(target=ask, args=(symptom,)) for symptom in ("open", "phone")]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(sorted(map(str, results)), ["new", "too many"])
        self.assertEqual(shop.help_requests.count(), 3)


class AlertText(TestCase):
    def test_no_web_address_or_link_to_an_app_survives(self):
        from .notifications import alert_text
        for text in ("see https://evil.example/x", "tg://resolve?domain=evil", "ftp://files.example/x", "tg:resolve?domain=x",
                     "www.evil.example", "evil.example/path", "javascript://x%0Aalert(1)", "HTTP://EVIL.EXAMPLE"):
            shown = alert_text(text, 200)
            self.assertNotRegex(shown, r"(?i)://|tg:|www\.|evil", text)
        self.assertEqual(alert_text("Il cliente ha pagato alle 10:20 e resta in attesa", 200),
                         "Il cliente ha pagato alle 10:20 e resta in attesa")

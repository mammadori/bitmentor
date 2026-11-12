"""The Lightning address of the new shop form, tried while the merchant writes it: what gets pasted is tidied, the
live check says what is wrong and what to do, and the check on submit stays the real one."""
import json
import socket
import time
from contextlib import contextmanager
from datetime import date
from unittest.mock import patch

import requests
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, TestCase, override_settings

from config.community import community
from website.apps import skipped_form_links

from . import lnaddress
from .forms import ShopForm, tidy_lightning_address
from .lnaddress import confirms, fetch, missing, one_slip, probe, refusal, suggestion
from .models import RequestCount, Shop
from .tests import public_names, steady_limits, stream

METADATA = {"tag": "payRequest", "callback": "https://pay.example.org/cb", "minSendable": 2000,
            "metadata": "[[\"text/plain\", \"internal note from the provider\"]]"}
INVOICE = {"pr": "lnbc1", "verify": "https://pay.example.org/verify/a", "reason": "provider-only detail"}
SETTLED = {"status": "OK", "settled": False}


def answers(*replies):
    """The provider answers these, one per request, through the real `fetch` and its protections."""
    return patch("shops.lnaddress.requests.Session.get", side_effect=[stream(reply) for reply in replies])


@contextmanager
def public():
    """Every name points to a public address, without asking the DNS."""
    with public_names(), patch("shops.forms.public_host", return_value=True):
        yield


def shop(name, address, owner=None, **fields):
    """A shop with a register, of its own owner unless told otherwise."""
    owner = owner or get_user_model().objects.create(username=f"owner-{name}")
    return Shop.objects.create(name=name, lightning_address=address, owner=owner, **fields)


class Fresh(TestCase):
    def setUp(self):
        cache.clear()
        RequestCount.objects.all().delete()
        self.addCleanup(cache.clear)


class Tidy(TestCase):
    def test_what_gets_pasted_becomes_the_address(self):
        for pasted in ("mario@example.org", "Mario@Example.org", "lightning:mario@example.org", "LIGHTNING:Mario@example.org",
                       "lightning://mario@example.org", " mario@example.org ", "mario@example.org.", "mario@example.org,",
                       '"mario@example.org"', "'mario@example.org'", "«mario@example.org»", "“mario@example.org”.",
                       "<mario@example.org>", "(mario@example.org)", "`mario@example.org`", '"lightning:mario@example.org".',
                       "mario @ example . org", "mario @example.org", "mario​@example.org﻿", "\tmario@example.org\n",
                       "https://example.org/.well-known/lnurlp/mario", "https://Example.org/.well-known/lnurlp/Mario/",
                       "https://example.org/.well-known/lnurlp/mario?amount=1000", "lnurlp://example.org/.well-known/lnurlp/mario",
                       "lightning:https://example.org/.well-known/lnurlp/mario"):
            with self.subTest(pasted):
                self.assertEqual(tidy_lightning_address(pasted), "mario@example.org")

    def test_what_is_not_an_address_stays_what_it_is(self):
        """The rules of the form refuse it, with the reason: tidying does not make up an address."""
        self.assertEqual(tidy_lightning_address("₿mario@example.org."), "₿mario@example.org")
        self.assertEqual(tidy_lightning_address("https://example.org:8443/.well-known/lnurlp/mario"),
                         "https://example.org:8443/.well-known/lnurlp/mario")
        self.assertEqual(tidy_lightning_address("http://example.org/.well-known/lnurlp/mario"),
                         "http://example.org/.well-known/lnurlp/mario")
        self.assertEqual(tidy_lightning_address("https://example.org/pay/mario"), "https://example.org/pay/mario")
        self.assertEqual(tidy_lightning_address("..."), "")

    def test_the_page_tidies_the_same_way(self):
        """static/shop_form.js keeps its own copy of the rules: the same characters and the same prefixes."""
        script = (settings.BASE_DIR / "static/shop_form.js").read_text()
        from .forms import LNURLP_URL, PASTED_AROUND
        self.assertIn(json.dumps(PASTED_AROUND, ensure_ascii=False), script)
        self.assertIn("/^lightning:(\\/\\/)?/i", script)
        self.assertIn(LNURLP_URL.pattern.replace("(?P<host>", "(").replace("(?P<name>", "(").replace("/", "\\/"), script)


@override_settings(NETWORK="signet")
class TheForm(TestCase):
    def form(self, address, **fields):
        with public():
            form = ShopForm({"name": "Bar", "lightning_address": address, **fields})
            form.is_valid()
        return form

    def test_a_pasted_address_is_saved_tidy(self):
        form = self.form('"Lightning:Mario@Example.org".')
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save(commit=False).lightning_address, "mario@example.org")

    def test_each_mistake_has_its_reason(self):
        for address, code, words in (
                ("₿mario@example.org", "bolt12", "BOLT12"), ("lno1qgsqvgnwgcg35z6ee2h3yczraddm72xrfua9uve2rlrm9deu7xyfzr", "bolt12", "BOLT12"),
                ("lnbc10u1pjq8wq3pp5qqqsyqcyq5rqwzqfqqqsyqcyq5rqwzqfqqqsyqcyq5rqwzqfqypq", "invoice", "one payment"),
                ("lntbs1u1pjq8wq3pp5qqqsyqcyq5rqwzqfqqqsyqcyq5rqwzqfqqqsyqcyq5rqwzqfqypq", "invoice", "one payment"),
                ("mario", "shape", "name@provider.com"), ("mario@example", "shape", "name@provider.com"),
                ("mario@printer.local", "private", "cannot be reached")):
            with self.subTest(address):
                errors = self.form(address).errors.as_data()["lightning_address"]
                self.assertEqual(errors[0].code, code)
                self.assertIn(words, errors[0].messages[0])


class Probe(Fresh):
    def test_every_state(self):
        with public(), answers(METADATA, INVOICE, SETTLED):
            self.assertEqual(probe("mario@example.org"), lnaddress.OK)
        with public(), answers(METADATA, {"pr": "lnbc1"}):
            self.assertEqual(probe("mario@example.org"), lnaddress.NO_CONFIRMATION)
        with public(), answers({"status": "ERROR", "reason": "no such user"}):
            self.assertEqual(probe("mario@example.org"), lnaddress.NO_ANSWER)
        with public(), patch("shops.lnaddress.requests.Session.get", side_effect=requests.ConnectionError):
            with patch("shops.lnaddress.missing", return_value=True):
                self.assertEqual(probe("mario@exmple.org"), lnaddress.UNKNOWN_DOMAIN)
            with patch("shops.lnaddress.missing", return_value=False):
                self.assertEqual(probe("mario@example.org"), lnaddress.NO_ANSWER)
        with public(), patch("shops.lnaddress.requests.Session.get", side_effect=requests.ReadTimeout), \
                patch("shops.lnaddress.missing") as asked:
            self.assertEqual(probe("mario@example.org"), lnaddress.NO_ANSWER)
        asked.assert_not_called()

    def test_only_a_name_that_does_not_exist_is_missing(self):
        """A resolver that does not answer says nothing about the name."""
        for errno, gone in ((socket.EAI_NONAME, True), (socket.EAI_AGAIN, False), (socket.EAI_FAIL, False)):
            with self.subTest(errno), patch("socket.getaddrinfo", side_effect=socket.gaierror(errno, "x")):
                self.assertEqual(missing("exmple.org"), gone)
        with patch("socket.getaddrinfo", return_value=[]):
            self.assertFalse(missing("example.org"))

    def test_the_lookup_of_the_fetch_says_whether_the_name_exists(self):
        """The fetch asks the name first: its answer decides, without a second lookup."""
        for errno, state in ((socket.EAI_NONAME, lnaddress.UNKNOWN_DOMAIN), (socket.EAI_AGAIN, lnaddress.NO_ANSWER)):
            with self.subTest(errno), patch("socket.getaddrinfo", side_effect=socket.gaierror(errno, "x")) as asked, \
                    patch("shops.lnaddress.requests.Session.get") as fetched:
                self.assertEqual(probe("mario@exmple.org"), state)
            self.assertEqual(asked.call_count, 1)
            fetched.assert_not_called()
        # Every address must be public, or nothing is fetched
        inside = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
                  (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 443))]
        with patch("socket.getaddrinfo", return_value=inside), patch("shops.lnaddress.requests.Session.get") as fetched:
            self.assertEqual(probe("mario@example.org"), lnaddress.NO_ANSWER)
        fetched.assert_not_called()

    def test_the_live_check_has_one_limit_for_all_its_requests(self):
        with patch("shops.lnaddress.fetch", side_effect=[METADATA, INVOICE, SETTLED]) as get:
            self.assertEqual(probe("mario@example.org", seconds=12), lnaddress.OK)
        limits = [call.args[1] for call in get.call_args_list]
        self.assertTrue(all(0 < limit <= 12 for limit in limits), limits)
        self.assertEqual(limits, sorted(limits, reverse=True))
        # Time used up: what is left is not positive, and fetch asks nothing with it (test_a_shorter_limit_never_a_longer_one)
        limits = []

        def fake(url, seconds):
            limits.append(seconds)
            if seconds <= 0:
                raise ValueError(url)
            return METADATA

        with patch("shops.lnaddress.time.monotonic", side_effect=[0, 1, 20]), patch("shops.lnaddress.fetch", side_effect=fake):
            self.assertEqual(probe("mario@example.org", seconds=12), lnaddress.NO_ANSWER)
        self.assertEqual(limits, [11, -8])

    def test_a_shorter_limit_never_a_longer_one(self):
        with public(), patch("shops.lnaddress.requests.Session.get", return_value=stream({})) as get:
            fetch("https://example.org/x", 2)
            self.assertEqual(get.call_args.kwargs["timeout"], 2)
            fetch("https://example.org/x", 100)
            self.assertEqual(get.call_args.kwargs["timeout"], lnaddress.TIMEOUT)
            with self.assertRaises(ValueError):
                fetch("https://example.org/x", 0)
        self.assertEqual(get.call_count, 2)


class Suggestion(Fresh):
    def test_one_slip(self):
        for typed, known in (("exmple.org", "example.org"), ("examplle.org", "example.org"), ("exapmle.org", "example.org"),
                             ("example.or", "example.org"), ("example.orf", "example.org"), ("example.con", "example.com")):
            with self.subTest(typed):
                self.assertTrue(one_slip(typed, known))
        for typed, known in (("example.org", "example.org"), ("exmpl.org", "example.org"), ("other.org", "example.org"),
                             ("xeample.orh", "example.org")):
            with self.subTest(typed):
                self.assertFalse(one_slip(typed, known))

    def test_from_the_shops_of_the_site(self):
        shop("A", "anna@pay.example.org")
        shop("B", "bruno@pay.example.org")
        Shop.objects.create(name="C", lightning_address="carla@other.example.net")  # no owner
        shop("D", "dino@other.example.net")  # a single owner
        self.assertEqual(suggestion("mario@pay.exmple.org"), "mario@pay.example.org")
        self.assertIsNone(suggestion("mario@other.exampel.net"))
        self.assertIsNone(suggestion("mario@pay.example.org"))
        self.assertIsNone(suggestion("mario@far.example.net"))

    def test_only_providers_of_two_active_shops_of_different_owners(self):
        """A domain of one shop alone is its own business, and a shop's owner can open a second one."""
        first = shop("A", "anna@mine.example.org")
        self.assertIsNone(suggestion("mario@mine.exmple.org"))
        shop("B", "bruno@mine.example.org", owner=first.owner)
        self.assertIsNone(suggestion("mario@mine.exmple.org"))
        shop("C", "carla@mine.example.org", suspended=True)
        shop("D", "dino@mine.example.org", closed_on=date(2020, 1, 1))
        self.assertIsNone(suggestion("mario@mine.exmple.org"))
        shop("E", "elsa@mine.example.org")
        self.assertEqual(suggestion("mario@mine.exmple.org"), "mario@mine.example.org")

    def test_never_from_what_just_passed_a_check(self):
        """Anyone can make an address of their own pass: one slip away from a known provider, it would be offered as
        where the money goes."""
        metadata = {**METADATA, "callback": "https://wallet.example.net/cb"}
        with public(), answers(metadata, {**INVOICE, "verify": "https://wallet.example.net/verify/a"}, SETTLED):
            self.assertEqual(lnaddress.live_check("anna@wallet.example.net", 6, 1), lnaddress.OK)
        self.assertIsNone(suggestion("mario@wallet.exampel.net"))

    def test_on_submit_too(self):
        shop("A", "anna@pay.example.org")
        shop("B", "bruno@pay.example.org")
        with public(), patch("shops.lnaddress.requests.Session.get", side_effect=requests.ConnectionError), \
                patch("shops.lnaddress.missing", return_value=True):
            self.assertEqual(refusal("mario@pay.exmple.org", user=1),
                             "I cannot find pay.exmple.org: check what you wrote after the @. Did you mean mario@pay.example.org?")
            # Without a person (the diagnosis, in its own thread) the shops are not read
            self.assertEqual(refusal("mario@pay.exmple.org"), "I cannot find pay.exmple.org: check what you wrote after the @.")


class LiveCheck(Fresh):
    url = "/en/shops/new/lightning-address/"

    def setUp(self):
        super().setUp()
        self.user = get_user_model().objects.create(username="k")
        self.client.force_login(self.user)

    def ask(self, address, client=None):
        response = (client or self.client).post(self.url, {"address": address})
        return response, (response.json() if response["Content-Type"] == "application/json" else None)

    def test_only_who_is_logged_in(self):
        with patch("shops.lnaddress.probe") as tried:
            response, answer = self.ask("mario@example.org", Client())
        self.assertEqual(response.status_code, 403)
        self.assertEqual(answer, {"state": "login"})
        tried.assert_not_called()
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_only_from_the_page_of_the_site(self):
        guarded = Client(enforce_csrf_checks=True)
        guarded.force_login(self.user)
        with patch("shops.lnaddress.probe", return_value=lnaddress.OK) as tried:
            self.assertEqual(guarded.post(self.url, {"address": "mario@example.org"}).status_code, 403)
            tried.assert_not_called()
            guarded.get("/en/shops/new/")
            response = guarded.post(self.url, {"address": "mario@example.org"},
                                    headers={"X-CSRFToken": guarded.cookies[settings.CSRF_COOKIE_NAME].value})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["X-Robots-Tag"], "noindex, nofollow")
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_a_working_address(self):
        with public(), answers(METADATA, INVOICE, SETTLED) as get:
            response, answer = self.ask(" lightning:Mario@Example.org. ")
        self.assertEqual(answer, {"address": "mario@example.org", "provider": "example.org", "state": "ok", "suggestion": None,
                                  "message": "It works: the register will know when a payment arrives. Provider: example.org."})
        self.assertEqual(get.call_args_list[0].args[0], "https://example.org/.well-known/lnurlp/mario")

    def test_in_the_language_of_the_page(self):
        with public(), answers(METADATA, INVOICE, SETTLED):
            answer = self.client.post(self.url.replace("/en/", "/it/"), {"address": "mario@example.org"}).json()
        self.assertEqual(answer["message"], "Funziona: la cassa saprà quando arriva un pagamento. Fornitore: example.org.")

    def test_nothing_of_what_the_provider_says_reaches_the_page(self):
        with public(), answers(METADATA, INVOICE, {"settled": False, "preimage": "secret-preimage"}):
            response, answer = self.ask("mario@example.org")
        for detail in ("internal note", "provider-only", "lnbc1", "pay.example.org", "verify", "secret-preimage"):
            self.assertNotIn(detail, response.content.decode())
        self.assertEqual(set(answer), {"address", "provider", "state", "message", "suggestion"})

    def test_the_problems_with_what_to_do(self):
        cases = (
            ("mario", "shape", "should look like name@provider.com", None),
            ("₿mario@example.org", "bolt12", f"write to {community()['email']}", None),
            ("lnbc10u1pjq8wq3pp5qqqsyqcyq5rqwzqfqqqsyqcyq5rqwzqfqqqsyqcyq5rqwzqfqypq", "invoice", "looks like an email", None),
            ("mario@printer.local", "private", "cannot be reached from the internet", None),
        )
        for address, state, words, _replies in cases:
            with self.subTest(address), patch("shops.lnaddress.probe") as tried:
                response, answer = self.ask(address)
                self.assertEqual((response.status_code, answer["state"]), (200, state))
                self.assertIn(words, answer["message"])
                tried.assert_not_called()
        with public(), answers(METADATA, {"pr": "lnbc1"}):
            answer = self.ask("mario@example.org")[1]
        self.assertEqual(answer["state"], "no_confirmation")
        self.assertIn("does not tell the register when a payment arrives", answer["message"])
        with public(), answers({"tag": "withdrawRequest"}):
            answer = self.ask("luigi@example.org")[1]
        self.assertEqual(answer["state"], "no_answer")
        self.assertIn("does not answer: check it and try again", answer["message"])

    def test_a_provider_that_does_not_exist_and_the_one_meant(self):
        shop("A", "anna@pay.example.org")
        shop("B", "bruno@pay.example.org")
        with public(), patch("shops.lnaddress.requests.Session.get", side_effect=requests.ConnectionError), \
                patch("shops.lnaddress.missing", return_value=True):
            answer = self.ask("mario@pay.exmple.org")[1]
        self.assertEqual(answer["state"], "unknown_domain")
        self.assertEqual(answer["message"], "I cannot find pay.exmple.org: check what you wrote after the @.")
        self.assertEqual(answer["suggestion"], "mario@pay.example.org")

    def test_a_short_limit_for_the_whole_check(self):
        with patch("shops.lnaddress.probe", return_value=lnaddress.NO_ANSWER) as tried, public():
            self.ask("mario@example.org")
        address, seconds = tried.call_args.args
        self.assertEqual(address, "mario@example.org")
        self.assertTrue(0 < seconds <= 6, seconds)

    def test_the_names_are_asked_within_the_same_time(self):
        """A DNS that does not answer holds the check, not the page: past its time the page hears "does not answer"."""
        with patch("shops.views.LIGHTNING_CHECK_SECONDS", 0.1), \
                patch("shops.forms.public_host", side_effect=lambda host: time.sleep(0.5) or True), \
                patch("shops.lnaddress.probe") as tried:
            started = time.monotonic()
            answer = self.ask("mario@example.org")[1]
            self.assertLess(time.monotonic() - started, 0.45)
            self.assertEqual(answer["state"], "no_answer")
            # Past its time the check keeps its place until it really ends
            self.assertEqual(lnaddress.LIVE_SLOTS._value, lnaddress.LIVE_AT_ONCE - 1)
            time.sleep(0.6)
        self.assertEqual(lnaddress.LIVE_SLOTS._value, lnaddress.LIVE_AT_ONCE)
        tried.assert_called_once()
        self.assertLessEqual(tried.call_args.args[1], 0)

    def test_few_checks_at_once(self):
        """A slow server holds at most LIVE_AT_ONCE threads per process: the others answer "busy" at once."""
        for _n in range(lnaddress.LIVE_AT_ONCE):
            lnaddress.LIVE_SLOTS.acquire()
        try:
            with patch("shops.lnaddress.probe") as tried, public():
                response, answer = self.ask("mario@example.org")
        finally:
            for _n in range(lnaddress.LIVE_AT_ONCE):
                lnaddress.LIVE_SLOTS.release()
        self.assertEqual((response.status_code, answer["state"]), (429, "busy"))
        self.assertIn("checked again when you create the shop", answer["message"])
        tried.assert_not_called()

    def test_a_limit_for_the_wider_network_too(self):
        self.enterContext(steady_limits())
        with patch("shops.lnaddress.probe", return_value=lnaddress.NO_ANSWER), public(), \
                patch("shops.views.LIGHTNING_CHECKS_PER_NETWORK", 2):
            states = []
            for n in range(3):
                client = Client(REMOTE_ADDR=f"2001:db8:1:{n}::1")
                client.force_login(get_user_model().objects.create(username=f"net{n}"))
                states.append(self.ask(f"n{n}@example.org", client)[1]["state"])
        self.assertEqual(states, ["no_answer", "no_answer", "busy"])

    def test_limits_per_person_and_per_address(self):
        self.enterContext(steady_limits())
        with patch("shops.lnaddress.probe", return_value=lnaddress.NO_ANSWER) as tried, public(), \
                patch("shops.views.LIGHTNING_CHECKS_PER_USER", 3), patch("shops.views.LIGHTNING_CHECKS_PER_IP", 4):
            states = [self.ask(f"m{n}@example.org")[1]["state"] for n in range(4)]
            self.assertEqual(states, ["no_answer"] * 3 + ["busy"])
            response, answer = self.ask("m9@example.org")
            self.assertEqual(response.status_code, 429)
            self.assertIn("checked again when you create the shop", answer["message"])
            # Another person from the same address: theirs, until the address has had its share
            other = Client()
            other.force_login(get_user_model().objects.create(username="j"))
            states = [self.ask(f"o{n}@example.org", other)[1]["state"] for n in range(3)]
        self.assertEqual(states, ["no_answer", "busy", "busy"])
        self.assertEqual(tried.call_count, 4)

    def test_the_same_address_is_not_asked_twice_in_a_row(self):
        with public(), answers(METADATA, INVOICE, SETTLED) as get:
            self.ask("mario@example.org")
            self.assertEqual(self.ask("Mario@example.org")[1]["state"], "ok")
        self.assertEqual(get.call_count, 3)
        with public(), answers(METADATA, {"pr": "lnbc1"}) as get:
            self.assertEqual(self.ask("maria@example.org")[1]["state"], "no_confirmation")
        self.assertEqual(get.call_count, 2)

    def test_a_problem_is_remembered_only_briefly(self):
        with patch("shops.lnaddress.probe", return_value=lnaddress.NO_ANSWER), public(), \
                patch("shops.lnaddress.cache") as kept:
            kept.get.side_effect = lambda key, default=None: default
            self.ask("mario@example.org")
        self.assertEqual(kept.set.call_args.args[2], lnaddress.REMEMBER_PROBLEM)


@override_settings(NETWORK="signet")
class TheSubmitStaysTheRealCheck(Fresh):
    """The live check saves at most the second probe of the very same address, if it worked: never a check."""

    def setUp(self):
        super().setUp()
        self.client.force_login(get_user_model().objects.create(username="k"))

    def live(self, address, *replies):
        with public(), answers(*replies):
            return self.client.post("/en/shops/new/lightning-address/", {"address": address}).json()["state"]

    def submit(self, address, *replies, name="Bar"):
        with public(), answers(*replies) as get:
            response = self.client.post("/en/shops/new/", {"name": name, "lightning_address": address})
        return response, get.call_count

    def test_an_address_that_just_worked_is_not_probed_again(self):
        self.assertEqual(self.live("mario@example.org", METADATA, INVOICE, SETTLED), "ok")
        response, probed = self.submit("lightning:Mario@example.org")
        self.assertEqual((response.status_code, probed), (302, 0))
        self.assertEqual(Shop.objects.get().lightning_address, "mario@example.org")

    def test_what_another_person_tried_does_not_count(self):
        other = Client()
        other.force_login(get_user_model().objects.create(username="other"))
        with public(), answers(METADATA, INVOICE, SETTLED):
            self.assertEqual(other.post("/en/shops/new/lightning-address/", {"address": "mario@example.org"}).json()["state"], "ok")
        response, probed = self.submit("mario@example.org", METADATA, {"pr": "lnbc1"})
        self.assertEqual(probed, 2)
        self.assertContains(response, "does not tell the register")
        self.assertFalse(Shop.objects.exists())

    def test_another_address_is_probed_on_submit(self):
        self.assertEqual(self.live("mario@example.org", METADATA, INVOICE, SETTLED), "ok")
        response, probed = self.submit("mario@example.com", {**METADATA, "callback": "https://pay.example.com/cb"}, {"pr": "lnbc1"})
        self.assertEqual(probed, 2)
        self.assertContains(response, "does not tell the register")
        self.assertFalse(Shop.objects.exists())

    def test_a_problem_seen_live_is_probed_again(self):
        self.assertEqual(self.live("mario@example.org", METADATA, {"pr": "lnbc1"}), "no_confirmation")
        response, probed = self.submit("mario@example.org", METADATA, INVOICE, SETTLED)
        self.assertEqual((response.status_code, probed), (302, 3))

    def test_the_check_on_submit_does_not_let_the_next_one_off(self):
        response, probed = self.submit("mario@example.org", METADATA, INVOICE, SETTLED)
        self.assertEqual((response.status_code, probed), (302, 3))
        # Another shop, so that it is not taken for a second click on the same button
        response, probed = self.submit("mario@example.org", METADATA, {"pr": "lnbc1"}, name="Bar two")
        self.assertEqual(probed, 2)
        self.assertContains(response, "does not tell the register")


class ThePage(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create(username="k")
        self.client.force_login(self.user)

    def configured(self, block):
        data = {key: value for key, value in community().items() if key != "shop_form"}
        if block is not None:
            data["shop_form"] = block
        return patch("config.community.community", return_value=data)

    def test_the_live_region_and_the_help(self):
        page = self.client.get("/en/shops/new/")
        self.assertContains(page, '<p id="lightning-check" class="field-check" role="status" aria-live="polite" hidden '
                                  'data-url="/en/shops/new/lightning-address/" data-checking="Trying the address..."')
        self.assertContains(page, '<details class="field-help"><summary>Where do I find it?</summary>')
        self.assertContains(page, "look under Receive")
        self.assertNotContains(page, "The wallets that work with the register")
        self.assertContains(self.client.get("/it/shops/new/"), "<summary>Dove lo trovo?</summary>")

    def test_the_wallet_page_of_the_community(self):
        with self.configured({"wallet": "learn/wallet/"}):
            self.assertContains(self.client.get("/en/shops/new/"),
                                '<p><a href="/en/learn/wallet/">The wallets that work with the register</a></p>')
            self.assertContains(self.client.get("/it/shops/new/"),
                                '<p><a href="/it/learn/wallet/">I wallet che funzionano con la cassa</a></p>')
            self.assertEqual(skipped_form_links(None), [])
        with self.configured({"wallet": "https://wallets.example.org/"}):
            self.assertContains(self.client.get("/en/shops/new/"), 'href="https://wallets.example.org/"')

    def test_a_wrong_wallet_link_stays_out_and_the_check_says_why(self):
        for block in ({"wallet": "/learn/"}, {"wallet": "javascript:alert(1)"}, "learn/"):
            with self.subTest(block), self.configured(block):
                self.assertNotContains(self.client.get("/en/shops/new/"), "The wallets that work with the register")
                warnings = skipped_form_links(None)
                self.assertEqual(len(warnings), 1)
                self.assertEqual(warnings[0].id, "website.W006")
        with self.configured(None):
            self.assertEqual(skipped_form_links(None), [])

    def test_where_the_money_goes_is_not_in_the_edit_form(self):
        """The edit form leaves the destination out on purpose (ShopDetailsForm): nothing to try there."""
        shop = Shop.objects.create(name="Bar", owner=self.user, lightning_address="mario@example.org")
        page = self.client.get(f"/en/shops/edit/{shop.id}/")
        self.assertNotContains(page, "lightning-check")
        self.assertNotContains(page, 'name="lightning_address"')


class VerifyAnswer(TestCase):
    """LUD-21: the invoice the probe makes is never paid, so `settled` must be there, a boolean, and false."""

    def test_what_real_providers_send(self):
        for answer in ({"status": "OK", "settled": False, "preimage": None, "pr": "lnbc1"},
                       {"status": "OK", "settled": False, "pr": "LNBC1"}, {"status": "ok", "settled": False},
                       {"settled": False}, {"settled": False, "preimage": None}):
            with self.subTest(answer):
                self.assertTrue(confirms(answer, "lnbc1"))

    def test_what_the_register_could_not_trust(self):
        for answer in ({"settled": True}, {"status": "OK", "settled": True, "pr": "lnbc1"},
                       {"settled": "false"}, {"settled": 0}, {"settled": None}, {"status": "OK"},
                       {"status": "ERROR", "settled": False, "reason": "not found"}, {"status": None, "settled": False},
                       {"settled": False, "pr": "lnbc2"}, {"settled": False, "pr": 1}, [], "settled", None):
            with self.subTest(answer):
                self.assertFalse(confirms(answer, "lnbc1"))

    def test_through_the_probe(self):
        metadata = {"tag": "payRequest", "callback": "https://pay.example.org/cb"}
        invoice = {"pr": "lnbc1", "verify": "https://pay.example.org/verify/a"}
        for verify, refused in (({"status": "OK", "settled": True}, True), ({"status": "ERROR", "settled": False}, True),
                                ({"status": "OK", "settled": False, "pr": "lnbc1"}, False)):
            with self.subTest(verify), public_names(), \
                    patch("shops.lnaddress.requests.Session.get", side_effect=[stream(metadata), stream(invoice), stream(verify)]):
                problem = refusal("mario@example.org")
            if refused:
                self.assertIn("does not tell the register", problem)
            else:
                self.assertIsNone(problem)

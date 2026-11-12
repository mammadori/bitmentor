"""The register: the phone collects in Lightning by itself. Here what the server decides: who has a register, what the
page carries along, the CSP of the register alone, the service worker with its scope, and that no sale comes back."""
import json
import re
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase

from config import features
from config.community import community

from .models import Shop, StaffRegister
from .register import PRICE_SOURCES, options
from .test_experimental import on


def register_data(page):
    match = re.search(r'<script id="register-data" type="application/json">(.*?)</script>', page, re.S)
    return json.loads(match[1]) if match else None


class Register(TestCase):
    def setUp(self):
        owner = get_user_model().objects.create(username="k")
        self.shop = Shop.objects.create(owner=owner, name="Bar", lightning_address="Till@LN.example.org")
        self.url = f"/en/shops/register/{self.shop.id}/"

    def test_the_page_carries_what_the_backup_needs_and_nothing_secret(self):
        page = self.client.get(self.url).content.decode()
        data = register_data(page)
        self.assertEqual(data["address"], "till@ln.example.org")
        self.assertEqual(data["domain"], "ln.example.org")
        self.assertEqual(data["currency"], community().get("currency", "EUR"))
        self.assertEqual(data["sources"][:2], ["kraken", "coinbase"])
        self.assertEqual(data["tolerance"], 1.0)
        self.assertEqual(data["max_age"], 120)
        # Nothing of on-chain, an experimental feature that is off unless the community turns it on
        self.assertEqual(set(data), {"address", "domain", "currency", "sources", "tolerance", "max_age", "network",
                                     "relays", "public_relays", "discount", "confirm_above", "shop", "now", "texts"})
        self.assertEqual(data["network"], "signet")
        self.assertEqual(data["shop"]["name"], "Bar")
        self.assertIn(f'data-worker="/en/shops/register/{self.shop.id}/sw.js"', page)

    @on(features.ONCHAIN)
    def test_with_on_chain_on_the_page_also_carries_where_to_look(self):
        data = register_data(self.client.get(self.url).content.decode())
        self.assertEqual(set(data), {"address", "domain", "currency", "sources", "tolerance", "max_age", "network", "chain_on",
                                     "explorers", "relays", "public_relays", "discount", "confirm_above", "shop", "now", "texts"})
        # On-chain: that it is on, where to look, and the owner's starting preferences; never an address or a key
        self.assertEqual((data["chain_on"], data["explorers"]), (True, ["https://mempool.space/signet/api"]))

    def test_only_the_register_talks_to_the_lightning_provider_and_the_price_sources(self):
        policy = self.client.get(self.url).headers["Content-Security-Policy"]
        connect = policy.split("connect-src ")[1].split(";")[0].split()
        self.assertEqual(connect[:3], ["'self'", "https://ln.example.org", "https://*.ln.example.org"])
        self.assertIn("https://api.kraken.com", connect)
        self.assertIn("https://api.coinbase.com", connect)
        # The explorer of on-chain payments, by its origin only
        self.assertIn("https://mempool.space", connect)
        # The rest of the CSP is as always
        self.assertIn("script-src 'self' 'inline-speculation-rules'", policy)
        self.assertIn("default-src 'self'", policy)
        for other in ("/en/shops/", f"/en/shops/s/{self.shop.id}/", f"/register/{self.shop.id}/manifest.webmanifest"):
            self.assertNotIn("connect-src", self.client.get(other).headers["Content-Security-Policy"], other)
            # Only the register page is kept as the register's copy on the phone (static/register.js, keepCopy)
            self.assertNotIn("X-Register", self.client.get(other).headers, other)
        self.assertEqual(self.client.get(self.url).headers["X-Register"], "1")

    def test_no_address_no_register(self):
        Shop.objects.filter(pk=self.shop.pk).update(lightning_address="")
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_an_address_that_would_open_the_policy_gets_no_register(self):
        for address in ("a@ln.example.org; script-src *", "a b@ln.example.org", "a@*.org", "a@localhost", "nobody",
                        "a@ln.example.org/x", "a@[::1]"):
            Shop.objects.filter(pk=self.shop.pk).update(lightning_address=address)
            self.assertEqual(self.client.get(self.url).status_code, 404, address)

    def test_the_community_chooses_the_sources(self):
        base = community()
        with patch("shops.register.community", return_value={**base, "register": {
                "price_sources": ["bitstamp", "nowhere", "bitstamp", "mempool"], "tolerance": 2, "max_rate_age": 30}}):
            response = self.client.get(self.url)
        data = register_data(response.content.decode())
        self.assertEqual((data["sources"], data["tolerance"], data["max_age"]), (["bitstamp", "mempool"], 2.0, 30))
        self.assertNotIn("nowhere", response.headers["Content-Security-Policy"])
        self.assertNotIn("kraken", response.headers["Content-Security-Policy"])
        # With a single source the rate cannot be compared: no register
        with patch("shops.register.community", return_value={**base, "register": {"price_sources": ["kraken"]}}):
            self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_the_service_worker_belongs_to_the_register(self):
        response = self.client.get(f"{self.url}sw.js")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/javascript")
        self.assertEqual(response["Cache-Control"], "no-cache")
        # Without Service-Worker-Allowed the scope cannot go beyond the till
        self.assertNotIn("Service-Worker-Allowed", response.headers)
        script = response.content.decode()
        self.assertIn('if (request.method !== "GET" || new URL(request.url).origin !== location.origin) return;', script)
        self.assertNotIn("sw-website", script)

    def test_without_a_register_the_old_service_worker_removes_itself(self):
        """Left on the phone, the copy would serve the old register, with an address that no longer works."""
        Shop.objects.filter(pk=self.shop.pk).update(lightning_address="nobody")
        script = self.client.get(f"{self.url}sw.js").content.decode()
        self.assertIn("unregister()", script)
        self.assertIn("caches.delete", script)
        self.assertNotIn("respondWith", script)

    def test_settings_written_loosely_do_not_break_the_register(self):
        loose = {**community(), "register": {"tolerance": "2%", "max_rate_age": "due ore", "price_sources": "bitstamp"}}
        with patch("shops.register.community", return_value=loose):
            # A single source is no register, but no error either
            self.assertEqual(self.client.get(self.url).status_code, 404)
            data = options()
        self.assertEqual((data["tolerance"], data["max_age"], data["sources"]), (2.0, 120, ["bitstamp"]))
        loose["register"]["price_sources"] = ["bitstamp", "kraken"]
        with patch("shops.register.community", return_value=loose):
            self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_a_tolerance_the_page_cannot_carry_is_not_used(self):
        """inf, nan or a negative number would reach the page as Infinity or NaN: no two sources would ever agree, or
        the JSON would not parse. The default applies, too much is the most, and `manage.py check` says so."""
        from .register import MAX_TOLERANCE, TOLERANCE, number_problems

        for written, used in (("inf", TOLERANCE), ("nan", TOLERANCE), (-1, TOLERANCE), ("-0.5%", TOLERANCE),
                              (1e9, MAX_TOLERANCE), ("2%", 2.0), (0, 0.0)):
            loose = {**community(), "register": {"tolerance": written, "max_rate_age": -5}}
            with patch("shops.register.community", return_value=loose):
                self.assertEqual(options()["tolerance"], used, written)
                self.assertEqual(options()["max_age"], 120)
                problems = number_problems()
                self.assertEqual(len(problems), 1 if used == 2.0 or written == 0 else 2, problems)
                page = self.client.get(self.url).content.decode()
            self.assertNotIn("Infinity", page)
            self.assertNotIn("NaN", page)
            self.assertEqual(register_data(page)["tolerance"], used)

    def test_no_service_worker_without_a_register(self):
        Shop.objects.filter(pk=self.shop.pk).update(lightning_address="")
        self.assertEqual(self.client.get(f"{self.url}sw.js").status_code, 404)
        self.assertEqual(self.client.get("/en/shops/register/00000000-0000-0000-0000-000000000000/sw.js").status_code, 404)

    def test_the_page_and_the_script_know_the_same_sources(self):
        script = (settings.BASE_DIR / "static" / "register.js").read_text()
        block = script.split("const SOURCES = {")[1].split("\n  };")[0]
        self.assertEqual(re.findall(r"^    (\w+): \{", block, re.M), list(PRICE_SOURCES))
        for name, origin in PRICE_SOURCES.items():
            self.assertIn(f"{origin}/", block, name)



class History(TestCase):
    """The history stays on the phone: its screen, the warnings, the day closing and the copies, all without the site."""

    def setUp(self):
        owner = get_user_model().objects.create(username="k")
        self.shop = Shop.objects.create(owner=owner, name="Café Central", lightning_address="till@ln.example.org")
        self.page = self.client.get(f"/en/shops/register/{self.shop.id}/").content.decode()
        self.script = (settings.BASE_DIR / "static" / "register.js").read_text()

    def test_the_page_says_once_where_the_sales_are(self):
        sentence = ("Sales stay only on this phone and in the copies you download. The site does not see or keep them: if you "
                    "lose the phone without a copy, you lose the history (not the money, which is in your wallet).")
        # In the history; at the first opening a short line, under the keypad so that the keypad stays in view
        self.assertEqual(self.page.count(sentence), 1)
        self.assertIn("Sales stay only on this phone: download a copy now and then.", self.page)
        self.assertLess(self.page.index('class="keys"'), self.page.index('<div id="keep-first" class="notice keep-first" hidden>'))

    def test_a_staff_register_says_whose_wallet_has_the_money(self):
        register = StaffRegister.objects.create(shop=self.shop, name="Anna")
        page = self.client.get(f"/en/shops/register/p/{register.id}/?k={register.key}")
        self.assertContains(page, "(not the money, which is in the shop's wallet)", count=1)
        self.assertContains(page, 'data-till-name="Anna"')
        self.assertContains(page, "End the shift")
        self.assertIn("owner", page.context["register"]["texts"]["by_hand"])

    def test_the_history_screen_has_the_closing_and_the_copies(self):
        for element in ('id="history"', 'id="copy-state"', 'id="day"', 'id="close-day"', 'id="csv-form"', 'id="export-form"',
                        'id="import-form"', 'id="history-back"', "data-history", 'data-file="cafe-central"'):
            self.assertIn(element, self.page)
        # The reminder choices are the ones the script knows
        choices = re.findall(r'<option value="(\w+)">', self.page.split('id="copy-every"')[1].split("</select>")[0])
        self.assertEqual(choices, ["manual", "daily", "weekly", "monthly", "closing"])



class NoSalesBack(TestCase):
    """The sales stay on the phone: the site has no address to receive them, and the register sends none."""

    def test_there_is_no_address_for_the_sales(self):
        shop = Shop.objects.create(name="Bar", lightning_address="till@ln.example.org")
        sale = json.dumps({"id": "1", "cents": 100})
        self.assertEqual(self.client.post(f"/register/{shop.id}/sale/", sale, content_type="application/json").status_code, 404)

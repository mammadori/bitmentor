"""The customers' path: whoever stands at the till with no wallet, or with one and no idea how to pay (shops/customer.py).

The pages are read-only, open to anyone, ask for nothing and call no other server. The wallets they offer come from
the community and follow the same rules as the merchants' list; the pictures are drawn here and carry no text."""
import re
from datetime import date
from unittest.mock import patch

import segno
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings

from config import features
from config.community import community, customer_links
from website.apps import skipped_customer_links

from . import customer_art
from .customer import device, wallet_keys
from .models import Shop
from .test_experimental import on

ANDROID = "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 Chrome/131 Mobile Safari/537.36"
IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 Version/17.5 Mobile Safari/604.1"
DESKTOP = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131 Safari/537.36"


def wallet(name, platforms=("android", "ios"), **fields):
    return {"name": name, "link": f"https://{name.lower().replace(' ', '-')}.example.org/", "platforms": list(platforms),
            "description": {"en": f"{name} is free.", "it": f"{name} è gratis."}, "open_source": True, "self_custody": True,
            "free": True, **fields}


WALLETS = [wallet("Pocket Wallet"), wallet("Apple Only", ["ios"]), wallet("Robot Only", ["android"]),
           wallet("Anywhere Wallet", [])]


LANGUAGES = [code for code, _name in settings.LANGUAGES]


def configured(**block):
    """The community's `shop_page` block, as given; `shop_form` is left alone."""
    data = {**community(), "shop_page": block}
    return patch("config.community.community", return_value=data)


def scripts(content):
    """The scripts a page loads, by their names without the hash a built image gives them (site.3f2a9c.js)"""
    return [re.sub(r"\.\w+(?=\.js$)", "", src) for src in re.findall(r'<script[^>]*src="([^"]+)"', content)]


def page(client, shop, path="", language="en", **headers):
    return client.get(f"/{language}/shops/customer/{shop.id}/{path}", **headers)


class Customer(TestCase):
    def setUp(self):
        cache.clear()
        self.lightning = Shop.objects.create(name="Bar", lightning=True, discount=10)
        self.onchain = Shop.objects.create(name="Books")

    def step(self, shop, number, query="", **headers):
        return page(self.client, shop, f"{number}/{query}", **headers)


class Hub(Customer):
    def test_the_two_ways_in(self):
        response = page(self.client, self.lightning)
        self.assertContains(response, "<h1>Paying with bitcoin at Bar</h1>")
        self.assertContains(response, f'href="/en/shops/customer/{self.lightning.id}/1/"')
        self.assertContains(response, f'href="/en/shops/customer/{self.lightning.id}/help/"')
        self.assertContains(response, "I don't have a wallet")
        self.assertContains(response, "I have a wallet, but I'm not sure how to pay")
        self.assertContains(response, f'href="/en/shops/s/{self.lightning.id}/#shop-pay-title"')

    def test_the_discount_is_a_reason_in_money(self):
        response = page(self.client, self.lightning)
        self.assertContains(response, '<p class="deal">-10% <span>if you pay in bitcoin</span></p>')
        self.assertContains(response, "On a bill of 20.00 EUR, you save 2.00 EUR.")
        response = page(self.client, self.onchain)
        self.assertNotContains(response, 'class="deal"')
        self.assertNotContains(response, "you save")

    def test_the_discount_in_the_communitys_currency(self):
        data = {**community(), "currency": "CHF"}
        with patch("shops.customer.community", return_value=data):
            self.assertContains(page(self.client, self.lightning), "On a bill of 20.00 CHF, you save 2.00 CHF.")

    def test_in_every_language(self):
        for language, text in (("en", "I don't have a wallet"), ("it", "Non ho un wallet")):
            with self.subTest(language):
                response = page(self.client, self.lightning, language=language)
                self.assertContains(response, text)
                self.assertContains(response, f'<html lang="{language}">')

    def test_the_communitys_page_on_wallets(self):
        with configured(wallet="learn/wallet/"):
            self.assertContains(page(self.client, self.onchain), '<a href="/en/learn/wallet/">More about wallets, from our community</a>')
        with configured():
            self.assertNotContains(page(self.client, self.onchain), "More about wallets")

    def test_the_shop_page_leads_here(self):
        response = self.client.get(f"/en/shops/s/{self.lightning.id}/")
        self.assertContains(response, f"<a href=\"/en/shops/customer/{self.lightning.id}/1/\">I don't have a wallet</a>")
        self.assertContains(response, f"<a href=\"/en/shops/customer/{self.lightning.id}/help/\">I have a wallet, but I'm not sure how to pay</a>")

    def test_not_for_a_shop_where_nobody_can_pay(self):
        """The shop page says why; a printed card that outlives the shop does not lead to an error"""
        historic = Shop.objects.create(name="Old", closed_on=date(2020, 1, 1))
        suspended = Shop.objects.create(name="Off", suspended=True)
        waiting = Shop.objects.create(name="Soon", awaiting_wallet=True)
        for shop in (historic, suspended, waiting):
            for path in ("", "1/", "2/", "3/", "help/"):
                with self.subTest(shop.name, path=path):
                    response = page(self.client, shop, path)
                    self.assertRedirects(response, f"/en/shops/s/{shop.id}/", fetch_redirect_response=False)

    def test_unknown_shop_and_unknown_step(self):
        self.assertEqual(self.client.get("/en/shops/customer/00000000-0000-0000-0000-000000000000/").status_code, 404)
        self.assertEqual(self.client.get("/en/shops/customer/00000000-0000-0000-0000-000000000000/1/").status_code, 404)
        self.assertEqual(self.step(self.lightning, 0).status_code, 404)
        self.assertEqual(self.step(self.lightning, 4).status_code, 404)

    def test_read_only(self):
        for path in ("", "1/", "help/"):
            self.assertEqual(self.client.post(f"/en/shops/customer/{self.lightning.id}/{path}").status_code, 405)
            self.assertEqual(self.client.head(f"/en/shops/customer/{self.lightning.id}/{path}").status_code, 200)

    def test_not_for_the_search_engines(self):
        for path in ("", "1/", "2/", "3/", "help/"):
            self.assertEqual(page(self.client, self.lightning, path)["X-Robots-Tag"], "noindex, nofollow")

    def test_a_copy_per_phone_only_where_the_phone_picks_the_list(self):
        """The first screen, until the visitor chooses a phone: its list starts with the wallets for the browser's"""
        self.assertIn("User-Agent", self.step(self.lightning, 1)["Vary"])
        for response in (self.step(self.lightning, 1, "?on=ios"), self.step(self.lightning, 1, "?on=all"),
                         page(self.client, self.lightning), page(self.client, self.lightning, "help/"),
                         self.step(self.lightning, 2), self.step(self.lightning, 3)):
            self.assertNotIn("User-Agent", response.get("Vary", ""))
        # A phone that is not one of the two is not a choice: back to the browser's
        self.assertIn("User-Agent", self.step(self.lightning, 1, "?on=nokia")["Vary"])


class Pages(Customer):
    """Nothing to fill in, nothing to run, nothing kept, nobody called"""

    def everything(self):
        for shop in (self.lightning, self.onchain):
            for path in ("", "1/", "2/?w=pocket-wallet", "3/?w=pocket-wallet", "help/"):
                yield shop, path

    def test_no_form_no_script_no_style_no_cookie(self):
        with configured(wallets=WALLETS):
            for shop, path in self.everything():
                for language in LANGUAGES:
                    with self.subTest(shop.name, path=path, language=language):
                        response = page(self.client, shop, path or "", language=language)
                        content = response.content.decode()
                        self.assertEqual(response.status_code, 200)
                        self.assertNotIn("<form", content)
                        self.assertNotIn("<input", content)
                        self.assertNotRegex(content, r"<script(?![^>]*\bsrc=)")
                        self.assertNotIn('style="', content)
                        self.assertNotRegex(content, r"\bon[a-z]+=\"")
                        self.assertEqual(len(response.cookies), 0)
                        # Only the shared scripts of the site; nothing of the path's own
                        self.assertEqual(scripts(content), ["/static/site.js"])

    def test_no_request_to_any_server(self):
        with configured(wallets=WALLETS), patch("requests.sessions.Session.request", side_effect=AssertionError("called out")), \
                patch("socket.create_connection", side_effect=AssertionError("called out")):
            for shop, path in self.everything():
                self.assertIn(page(self.client, shop, path).status_code, (200, 302))

    def test_no_link_to_another_server_but_the_communitys_wallets(self):
        with configured(wallets=WALLETS):
            for shop, path in self.everything():
                content = page(self.client, shop, path).content.decode()
                for link in re.findall(r'<a [^>]*href="(https?://[^"]+)"', content):
                    # The link to the source code (AGPLv3) in the footer is the one other server
                    self.assertTrue(link.endswith(".example.org/") or link == settings.SOURCE_URL, link)
                # What the browser loads or may be sent to: the site's own address, the canonical one included
                for link in re.findall(r'<(?:img|link|iframe|source|script)[^>]*(?:src|href)="(https?://[^"]+)"', content):
                    self.assertTrue(link.startswith(settings.PUBLIC_URL), link)

    def test_the_progress_marks_the_step(self):
        for number in (1, 2, 3):
            response = self.step(self.lightning, number)
            self.assertContains(response, '<ol class="customer-progress"')
            self.assertEqual(len(re.findall(r'aria-current="step"', response.content.decode())), 1)
            self.assertContains(response, f'<li aria-current="step"><span>{("Choose", "Install", "Get ready")[number - 1]}</span>')
        self.assertContains(self.step(self.lightning, 3), '<li class="done"><span>Choose</span><span class="visually-hidden"> (done)</span></li>')


class Choosing(Customer):
    def listed(self, response):
        return [name for name in ("Pocket Wallet", "Apple Only", "Robot Only", "Anywhere Wallet") if f"<strong>{name}</strong>" in response.content.decode()]

    def test_the_communitys_wallets(self):
        with configured(wallets=WALLETS):
            response = self.step(self.lightning, 1)
        self.assertEqual(self.listed(response), ["Pocket Wallet", "Apple Only", "Robot Only", "Anywhere Wallet"])
        self.assertContains(response, "Pocket Wallet is free.")
        self.assertContains(response, "Android, iPhone")
        self.assertContains(response, f'href="/en/shops/customer/{self.lightning.id}/2/?w=pocket-wallet"')
        self.assertContains(response, 'Choose <span class="visually-hidden">Pocket Wallet</span>')

    def test_the_wallets_in_the_language(self):
        with configured(wallets=WALLETS):
            self.assertContains(self.step(self.lightning, 1, language="it"), "Pocket Wallet è gratis.")

    def test_the_phone_picks_the_list(self):
        with configured(wallets=WALLETS):
            android = self.step(self.lightning, 1, HTTP_USER_AGENT=ANDROID)
            iphone = self.step(self.lightning, 1, HTTP_USER_AGENT=IPHONE)
            desktop = self.step(self.lightning, 1, HTTP_USER_AGENT=DESKTOP)
        self.assertEqual(self.listed(android), ["Pocket Wallet", "Robot Only", "Anywhere Wallet"])
        self.assertEqual(self.listed(iphone), ["Pocket Wallet", "Apple Only", "Anywhere Wallet"])
        self.assertEqual(len(self.listed(desktop)), 4)
        self.assertContains(android, '<a href="?on=android" aria-current="true">Android</a>')
        self.assertContains(iphone, '<a href="?on=ios" aria-current="true">iPhone</a>')
        self.assertContains(desktop, '<a href="?on=all" aria-current="true">All</a>')

    def test_the_visitor_can_choose_another_list(self):
        with configured(wallets=WALLETS):
            self.assertEqual(self.listed(self.step(self.lightning, 1, "?on=ios", HTTP_USER_AGENT=ANDROID)),
                             ["Pocket Wallet", "Apple Only", "Anywhere Wallet"])
            self.assertEqual(len(self.listed(self.step(self.lightning, 1, "?on=all", HTTP_USER_AGENT=ANDROID))), 4)
            # Whatever else is written there is not a phone
            for junk in ("?on=<script>", "?on=", "?on=nokia", "?on=android&on=ios"):
                response = self.step(self.lightning, 1, junk, HTTP_USER_AGENT=IPHONE)
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, "<script>")

    def test_the_agent_is_read_not_kept(self):
        self.assertEqual(device(type("R", (), {"headers": {"User-Agent": ANDROID}})()), "android")
        self.assertEqual(device(type("R", (), {"headers": {"User-Agent": IPHONE}})()), "ios")
        self.assertEqual(device(type("R", (), {"headers": {"User-Agent": DESKTOP}})()), "")
        self.assertEqual(device(type("R", (), {"headers": {}})()), "")

    def test_no_wallet_for_the_phone(self):
        with configured(wallets=[wallet("Apple Only", ["ios"])]):
            response = self.step(self.lightning, 1, HTTP_USER_AGENT=ANDROID)
        self.assertContains(response, "None of the wallets on our list is for this phone.")
        self.assertNotContains(response, "<strong>Apple Only</strong>")

    @on(features.ONCHAIN)
    def test_a_shop_gets_the_wallets_that_can_pay_there(self):
        wallets = [wallet("Lightning Only", methods=["lightning"]), wallet("Chain Only", methods="onchain"), wallet("Both")]
        with configured(wallets=wallets):
            lightning = self.step(self.lightning, 1, "?on=all")
            onchain = self.step(self.onchain, 1, "?on=all")
        self.assertEqual([n for n in ("Lightning Only", "Chain Only", "Both") if f"<strong>{n}</strong>" in lightning.content.decode()],
                         ["Lightning Only", "Both"])
        self.assertEqual([n for n in ("Lightning Only", "Chain Only", "Both") if f"<strong>{n}</strong>" in onchain.content.decode()],
                         ["Chain Only", "Both"])

    def test_the_merchants_list_when_the_customers_has_none(self):
        data = {**community(), "shop_form": {"wallets": [wallet("Merchant Wallet")]}}
        with patch("config.community.community", return_value=data):
            self.assertContains(self.step(self.lightning, 1), "<strong>Merchant Wallet</strong>")
        data["shop_page"] = {"wallets": [wallet("Customer Wallet")]}
        with patch("config.community.community", return_value=data):
            response = self.step(self.lightning, 1)
        self.assertContains(response, "<strong>Customer Wallet</strong>")
        self.assertNotContains(response, "Merchant Wallet")

    def test_only_open_source_non_custodial_and_free(self):
        """MiCAR: the software cannot check it, so the community states it, and a wallet without it is not shown"""
        problems = []
        for missing in ("open_source", "self_custody", "free"):
            entry = {key: value for key, value in wallet("Custodial").items() if key != missing}
            with configured(wallets=[entry, {**wallet("Custodial 2"), missing: False}, wallet("Honest")]):
                response = self.step(self.lightning, 1, "?on=all")
                problems += [problem.msg for problem in skipped_customer_links(None)]
            self.assertContains(response, "<strong>Honest</strong>")
            self.assertNotContains(response, "Custodial")
        # Two wallets wrong in each of the three ways
        self.assertEqual(len(problems), 6)
        self.assertTrue(all("self-custody" in problem or "open source" in problem for problem in problems))
        self.assertTrue(all(problem.startswith("`shop_page.wallets`, wallet ") for problem in problems))

    def test_wrong_wallets_are_left_out_and_said(self):
        wrong = [wallet("Bad link", link="javascript:alert(1)"), wallet("Plain", link="http://plain.example.org/"),
                 wallet("Bad phone", ["nokia"]), wallet("Bad method", methods=["cheque"]), wallet("x" * 41), "Pocket Wallet"]
        problems = []
        with configured(wallets=[*wrong, wallet("Fine")]):
            found = customer_links("en", problems.append)["wallets"]
            checks = skipped_customer_links(None)
        self.assertEqual([item["name"] for item in found], ["Fine"])
        self.assertEqual(len(problems), len(wrong))
        self.assertEqual([check.id for check in checks], ["website.W008"] * len(wrong))

    def test_the_software_names_no_wallet(self):
        self.assertEqual(customer_links("en")["wallets"], [])
        response = self.step(self.lightning, 1)
        self.assertNotContains(response, "customer-wallets")
        self.assertContains(response, "Ask us which wallet to use")

    def test_the_communitys_page_stays_reachable_with_a_list_too(self):
        """The shop page no longer links it: the first screen does, whether or not the community listed wallets"""
        with configured(wallets=WALLETS, wallet="learn/wallet/"):
            self.assertContains(self.step(self.lightning, 1), '<a href="/en/learn/wallet/">More about wallets, from our community</a>')
        with configured(wallets=WALLETS):
            self.assertNotContains(self.step(self.lightning, 1), "More about wallets")
        with configured(wallet="learn/wallet/"):
            self.assertContains(self.step(self.lightning, 1), '<a class="button" href="/en/learn/wallet/">The wallets we suggest</a>')
            self.assertNotContains(self.step(self.lightning, 1), "More about wallets")

    def test_without_a_list_the_page_of_the_community(self):
        with configured(wallet="learn/wallet/"):
            response = self.step(self.lightning, 1)
        self.assertContains(response, '<a class="button" href="/en/learn/wallet/">The wallets we suggest</a>')
        self.assertContains(response, f'href="/en/shops/customer/{self.lightning.id}/2/"')

    def test_keys_for_the_choice(self):
        self.assertEqual(wallet_keys([{"name": "Pocket Wallet"}, {"name": "Pocket wallet"}, {"name": "!!"}, {"name": "ÄÖ"}]),
                         ["pocket-wallet", "pocket-wallet-2", "3", "ao"])


class Installing(Customer):
    def test_the_chosen_wallet(self):
        with configured(wallets=WALLETS):
            response = self.step(self.lightning, 2, "?w=pocket-wallet")
        self.assertContains(response, "<h1>Install Pocket Wallet and make your wallet</h1>")
        self.assertContains(response, '<a class="button button-quiet" href="https://pocket-wallet.example.org/" rel="noopener noreferrer">The page of Pocket Wallet</a>')
        self.assertContains(response, "Write the words on paper")
        self.assertContains(response, "Never type those words anywhere else")
        self.assertContains(response, f'href="/en/shops/customer/{self.lightning.id}/3/?w=pocket-wallet"')
        self.assertContains(response, f'href="/en/shops/customer/{self.lightning.id}/1/"')

    def test_a_wallet_that_is_not_on_the_list_goes_back_to_the_list(self):
        with configured(wallets=WALLETS):
            for query in ("", "?w=nobody", "?w=%3Cscript%3E", "?w=", "?w=pocket-wallet%0A"):
                with self.subTest(query):
                    self.assertRedirects(self.step(self.lightning, 2, query), f"/en/shops/customer/{self.lightning.id}/1/",
                                         fetch_redirect_response=False)
                    self.assertRedirects(self.step(self.lightning, 3, query), f"/en/shops/customer/{self.lightning.id}/1/",
                                         fetch_redirect_response=False)

    @on(features.ONCHAIN)
    def test_a_wallet_that_cannot_pay_here_is_not_chosen_here(self):
        with configured(wallets=[wallet("Lightning Only", methods=["lightning"])]):
            here = self.step(self.lightning, 2, "?w=lightning-only")
            there = self.step(self.onchain, 2, "?w=lightning-only")
        self.assertContains(here, "<h1>Install Lightning Only and make your wallet</h1>")
        # Nothing to choose from on-chain: the path goes on with "your wallet", without the name of one that cannot pay
        self.assertContains(there, "<h1>Install your wallet and make it</h1>")
        self.assertNotContains(there, "Lightning Only")

    def test_without_a_list_the_steps_go_on(self):
        response = self.step(self.lightning, 2)
        self.assertContains(response, "<h1>Install your wallet and make it</h1>")
        self.assertNotContains(response, "The page of")
        self.assertEqual(self.step(self.lightning, 3).status_code, 200)

    def test_the_picture_of_the_paper(self):
        self.assertContains(self.step(self.lightning, 2), customer_art.picture("paper"), html=False)


class FirstSats(Customer):
    @override_settings(NETWORK="signet")
    def test_on_signet_someone_to_ask(self):
        response = self.step(self.lightning, 3)
        self.assertContains(response, "<h1>Get your first sats</h1>")
        self.assertContains(response, "called signet")
        self.assertContains(response, "starts with tb1")
        self.assertContains(response, 'href="mailto:')
        self.assertNotContains(response, "ask a friend")

    def test_the_address_starts_as_the_network_says(self):
        for network, start in (("signet", "tb1"), ("regtest", "bcrt1")):
            with self.subTest(network), override_settings(NETWORK=network):
                response = self.step(self.lightning, 3)
                self.assertContains(response, f"it starts with {start}.")
                self.assertContains(response, f"called {network}")
        self.assertNotContains(self.step(self.lightning, 3), "bcrt1")

    @override_settings(NETWORK="mainnet")
    def test_on_mainnet_a_little_bitcoin(self):
        with configured(first_bitcoin="learn/first-bitcoin/"):
            response = self.step(self.lightning, 3)
        self.assertContains(response, "<h1>Put some bitcoin in it</h1>")
        self.assertContains(response, "ask a friend who has bitcoin to send you a small amount")
        # No currency: the community's may be another, and a friend sends what they can
        self.assertNotRegex(response.content.decode(), r"(?i)euro|\bEUR\b|dollar|franc")
        self.assertContains(response, "we do not sell it and we do not suggest any company")
        self.assertContains(response, '<a href="/en/learn/first-bitcoin/">What our community can tell you about it</a>')
        for text in ("faucet", "tb1", "test network", "signet"):
            self.assertNotContains(response, text)
        # No company is named, and none is linked: the only addresses are the ones of the community
        self.assertNotRegex(response.content.decode(), r"(?i)exchange|buy it at")

    @override_settings(NETWORK="mainnet")
    def test_on_mainnet_without_a_page_the_association_to_write_to(self):
        response = self.step(self.lightning, 3)
        self.assertNotContains(response, "What our community can tell you")
        self.assertContains(response, "Not sure? Write to")

    @override_settings(NETWORK="mainnet")
    def test_a_wrong_page_stays_out_and_is_said(self):
        for wrong in ("/first/", "http://example.org/", "my page/", 7, "www.example.org/x"):
            with self.subTest(wrong), configured(first_bitcoin=wrong):
                self.assertNotContains(self.step(self.lightning, 3), "What our community can tell you")
                checks = skipped_customer_links(None)
                self.assertEqual([check.id for check in checks], ["website.W008"])
                self.assertIn("`shop_page.first_bitcoin`", checks[0].msg)

    @override_settings(NETWORK="mainnet")
    def test_the_way_to_the_shop(self):
        response = self.step(self.lightning, 3)
        self.assertContains(response, '-10% <span>if you pay in bitcoin</span>')
        self.assertContains(response, f'href="/en/shops/s/{self.lightning.id}/#shop-pay-title"')
        self.assertContains(response, f'href="/en/shops/customer/{self.lightning.id}/help/"')


class Paying(Customer):
    def test_lightning(self):
        response = page(self.client, self.lightning, "help/")
        self.assertContains(response, "<h1>How to pay at Bar</h1>")
        self.assertContains(response, "Here it works with Lightning.")
        self.assertContains(response, "With Lightning it takes a few seconds.")
        self.assertNotContains(response, "ten minutes")
        self.assertContains(response, "It says the payment failed")

    @on(features.ONCHAIN)
    def test_onchain(self):
        response = page(self.client, self.onchain, "help/")
        self.assertContains(response, "Here it works on-chain: your wallet has to send on-chain, not with Lightning.")
        self.assertContains(response, "about ten minutes")
        self.assertNotContains(response, "With Lightning it takes a few seconds.")
        self.assertNotContains(response, "It says the payment failed")

    def test_what_to_do_when_it_says_error(self):
        response = page(self.client, self.lightning, "help/")
        self.assertContains(response, "<h2>If your wallet says error</h2>")
        for problem in ("It does not scan", "It says there is not enough money", "It says the code has expired",
                        "It says the code is not valid, or the network is wrong", "I paid, but the till shows nothing",
                        "I paid too little, or too much"):
            self.assertContains(response, f"<summary>{problem}</summary>")
        self.assertContains(response, "Do not pay again.")
        # The answers are open to a reader without JavaScript: <details> needs none
        self.assertEqual(response.content.decode().count("<details>"), 7)

    def test_the_network_is_told(self):
        with override_settings(NETWORK="signet"):
            self.assertContains(page(self.client, self.lightning, "help/"), "a test network, called signet")
        with override_settings(NETWORK="mainnet"):
            response = page(self.client, self.lightning, "help/")
        self.assertContains(response, "switch to real bitcoin")
        self.assertNotContains(response, "called signet")

    def test_the_discount_is_asked_for_at_the_till(self):
        """The till does not apply it: the card to show the cashier asks for it, and so does this page"""
        response = page(self.client, self.lightning, "help/")
        self.assertContains(response, "This shop takes 10% off when you pay in bitcoin: ask for it at the till. "
                                      "On a bill of 20.00 EUR, you save 2.00 EUR.")
        self.assertNotContains(response, "already in the price")
        self.assertNotContains(page(self.client, self.onchain, "help/"), "off when you pay in bitcoin")

    def test_the_pictures_are_generic(self):
        content = page(self.client, self.lightning, "help/").content.decode()
        self.assertIn(str(customer_art.picture("scan")), content)
        self.assertIn(str(customer_art.picture("done")), content)


class Pictures(TestCase):
    def test_drawn_by_the_server_with_no_text_no_link_no_script(self):
        for name in customer_art.PICTURES:
            with self.subTest(name):
                picture = str(customer_art.picture(name))
                self.assertTrue(picture.startswith('<svg class="art" viewBox="0 0 160 120" aria-hidden="true" focusable="false">'))
                self.assertTrue(picture.endswith("</svg>"))
                for forbidden in ("<text", "<image", "<script", "<style", "href", "style=", "onload", "<foreignObject", "data:"):
                    self.assertNotIn(forbidden, picture)
                self.assertEqual(picture, str(customer_art.picture(name)))

    def test_the_code_is_not_a_code(self):
        """A picture of a code that a camera reads would send a wallet somewhere: it has the look and nothing else. No
        row of the dots is full, and the squares are drawn without the quiet zone and the timing lines of a real code"""
        self.assertTrue(all("1" * 9 != row and row.count("1") < 8 for row in customer_art.CODE_DOTS))
        self.assertEqual(customer_art.code(0, 0, 4), customer_art.code(0, 0, 4))
        self.assertEqual(customer_art.code(0, 0, 4).count('class="art-ink"'), 3 * 2 + sum(
            1 for row, dots in enumerate(customer_art.CODE_DOTS) for column, dot in enumerate(dots) if dot == "1"
            and not any(0 <= column - fx < 3 and 0 <= row - fy < 3 for fx, fy in customer_art.FINDERS)))

    def test_nothing_leaves_the_picture(self):
        """Every rectangle drawn outside a rotated group stays inside the 160x120 frame, a stroke's width from the edge"""
        for name in customer_art.PICTURES:
            picture = str(customer_art.picture(name))
            flat = re.sub(r"<g transform=.*?</g>", "", picture, flags=re.S)
            for tag in re.findall(r"<rect [^>]*>", flat):
                x, y, width, height = (float(re.search(rf'\b{key}="([\d.]+)"', tag)[1]) for key in ("x", "y", "width", "height"))
                with self.subTest(name, tag=tag):
                    self.assertGreaterEqual(x, 1.5)
                    self.assertGreaterEqual(y, 1.5)
                    self.assertLessEqual(x + width, 158.5)
                    self.assertLessEqual(y + height, 118.5)

    def test_the_sheet_prints_on_an_a4_page(self):
        css = (settings.BASE_DIR / "static" / "customer.css").read_text()
        self.assertRegex(css, r"@page customer-card \{[^}]*size: A4")
        self.assertIn("page: customer-card", css)

    def test_every_picture_is_in_the_pages(self):
        self.assertEqual(set(customer_art.PICTURES), {"store", "paper", "receive", "scan", "confirm", "done", "problem"})


class Card(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = get_user_model().objects.create(username="owner")
        self.client.force_login(self.owner)
        self.shop = Shop.objects.create(owner=self.owner, name="Bar", lightning=True, discount=10,
                                        lightning_address="bar@ln.example.org")

    def card(self, shop=None, language="en"):
        return self.client.get(f"/{language}/shops/customer-card/{(shop or self.shop).id}/")

    def test_the_sheet_to_print(self):
        response = self.card()
        self.assertContains(response, "<h1>No wallet yet?</h1>")
        self.assertContains(response, "Scan this code with your phone. In a few minutes you can pay here in bitcoin.")
        self.assertContains(response, "-10% <span>if you pay in bitcoin</span>")
        self.assertContains(response, "<strong>Bar</strong>")
        self.assertContains(response, '<button id="print" class="button">Print</button>')
        self.assertEqual(response["X-Robots-Tag"], "noindex, nofollow")

    def test_the_code_opens_the_path_in_the_language_of_the_sheet(self):
        for language in LANGUAGES:
            with self.subTest(language):
                url = f"https://localhost/{language}/shops/customer/{self.shop.id}/"
                with override_settings(PUBLIC_URL="https://localhost"):
                    response = self.card(language=language)
                self.assertContains(response, segno.make(url, error="m").svg_data_uri(scale=10, border=1, dark="#000", light="#fff"))
                self.assertContains(response, f'<a href="{url}">')

    def test_the_languages_of_the_sheet(self):
        response = self.card()
        for code in LANGUAGES:
            self.assertContains(response, f'href="/{code}/shops/customer-card/{self.shop.id}/"')

    def test_for_the_owner_only(self):
        other = get_user_model().objects.create(username="other")
        self.client.force_login(other)
        self.assertEqual(self.card().status_code, 404)
        self.client.logout()
        self.assertEqual(self.card().status_code, 302)

    def test_not_for_a_shop_where_nobody_can_pay(self):
        for fields in ({"suspended": True}, {"closed_on": date(2020, 1, 1)}, {"awaiting_wallet": True}):
            shop = Shop.objects.create(owner=self.owner, name="No", **fields)
            with self.subTest(fields):
                self.assertEqual(self.card(shop).status_code, 404)

    def test_the_button_in_the_panel(self):
        waiting = Shop.objects.create(owner=self.owner, name="Soon", awaiting_wallet=True)
        suspended = Shop.objects.create(owner=self.owner, name="Off", suspended=True)
        response = self.client.get("/en/shops/")
        self.assertContains(response, f'href="/en/shops/customer-card/{self.shop.id}/">Card for customers without a wallet</a>')
        self.assertNotContains(response, f"/customer-card/{waiting.id}/")
        self.assertNotContains(response, f"/customer-card/{suspended.id}/")

    def test_the_sheet_has_no_script_but_the_printing_one(self):
        content = self.card().content.decode()
        self.assertEqual(scripts(content), ["/static/sign.js"])
        self.assertNotIn('style="', content)

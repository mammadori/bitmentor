"""The page the QR code on the sign opens: how to pay at the till, the card for the cashier, the places to pay nearby
and the community's own pages about the shop (`shop_page` in community.yml)."""
import re
from datetime import date
from unittest.mock import patch

from django.conf import settings
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils.translation import gettext, override

from config import features
from config.community import community
from website.apps import skipped_shop_links

from . import certified, osm
from .directory import nearby_shops
from .models import Shop
from .test_experimental import on
from .views import distance_text, till_methods

# Metres in a degree of latitude, with the radius the page uses
DEGREE = 111194.93


def other_language():
    return next(code for code, _name in settings.LANGUAGES if code != settings.LANGUAGE_CODE)


class Till(TestCase):
    def page(self, shop, language=None):
        return self.client.get(f"/{language or settings.LANGUAGE_CODE}/shops/s/{shop.id}/")

    def test_a_lightning_shop(self):
        shop = Shop.objects.create(name="Bar", lightning=True, discount=10)
        self.assertEqual(till_methods(shop), "lightning")
        page = self.client.get(f"/en/shops/s/{shop.id}/")
        self.assertContains(page, '<h2 id="shop-pay-title">How to pay here</h2>')
        self.assertContains(page, "<li>Ask to pay in bitcoin, with Lightning.</li>")
        self.assertContains(page, "<li>The till shows a QR code with the amount.</li>")
        self.assertContains(page, "<li>Scan it with your wallet and confirm. With Lightning it takes a few seconds.</li>")
        self.assertContains(page, 'class="button needs-js" id="show-counter"')
        self.assertContains(page, '<dialog id="counter-card" class="counter-card"')
        self.assertContains(page, "I would like to pay in bitcoin, with Lightning.")
        self.assertContains(page, "With the 10% discount for paying in bitcoin, thank you.")

    @on(features.ONCHAIN)
    def test_an_onchain_shop(self):
        shop = Shop.objects.create(name="Bar")
        self.assertEqual(till_methods(shop), "onchain")
        page = self.client.get(f"/en/shops/s/{shop.id}/")
        self.assertContains(page, "<li>Ask to pay in bitcoin. Here it is on-chain: your wallet has to send on-chain, not with Lightning.</li>")
        self.assertContains(page, "<li>Scan it with your wallet, check the amount and confirm.</li>")
        self.assertContains(page, '<span class="counter-ask">I would like to pay in bitcoin.</span>')
        self.assertNotContains(page, "I would like to pay in bitcoin, with Lightning.")
        self.assertNotContains(page, "counter-deal")
        self.assertNotContains(page, "Accepts Lightning payments")

    def test_imported_shops_follow_the_lightning_flag(self):
        self.assertEqual(till_methods(Shop(name="A", lightning=True)), "lightning")
        with on(features.ONCHAIN):
            self.assertEqual(till_methods(Shop(name="B", lightning=False)), "onchain")
        # On-chain is an experimental feature, off unless the community turns it on: then the site says only "bitcoin"
        self.assertEqual(till_methods(Shop(name="B", lightning=False)), "bitcoin")

    def test_with_a_register_the_shop_takes_lightning_whatever_the_flag(self):
        # The flag changed alone in /admin/: the register still takes Lightning
        self.assertEqual(till_methods(Shop(name="C", lightning_address="c@ln.example.org", lightning=False)), "lightning")

    @on(features.ONCHAIN)
    def test_on_chain_too_is_only_what_the_owner_says(self):
        shop = Shop.objects.create(name="Bar", lightning_address="bar@ln.example.org", lightning=True, onchain=True,
                                   latitude=1, longitude=1)
        self.assertEqual(till_methods(shop), "both")
        self.assertContains(self.page(shop), "Ask to pay in bitcoin: Lightning or on-chain, whichever your wallet uses.")
        # The map has the flag and nothing more: no on-chain address, band or payment
        found = next(item for item in self.client.get("/shops.json").json()["shops"] if item["id"] == str(shop.id))
        self.assertIs(found["onchain"], True)
        self.assertNotIn("onchain", found["paid"] or {})
        self.assertEqual([mark["kind"] for mark in certified.marks(shop)], ["onchain"])
        self.assertEqual(osm.methods(shop), [osm.LIGHTNING, osm.ONCHAIN])
        shop.onchain = False
        self.assertEqual(certified.marks(shop), [])
        self.assertEqual(osm.methods(shop), [osm.LIGHTNING])

    def test_the_owner_ticks_on_chain_in_the_shop_form(self):
        from .forms import ShopForm

        self.assertIn("onchain", ShopForm.base_fields)

    def test_the_card_puts_the_main_language_first(self):
        shop = Shop.objects.create(name="Bar", lightning=True, discount=10)
        main, other = settings.LANGUAGE_CODE, other_language()
        html = self.page(shop, other).content.decode()
        big = html.index(f'<p class="counter-say" lang="{main}">')
        small = html.index(f'<p class="counter-own" lang="{other}">')
        self.assertLess(big, small)
        with override(main):
            self.assertIn(gettext("I would like to pay in bitcoin, with Lightning."), html[big:small])
        with override(other):
            self.assertIn(gettext("I would like to pay in bitcoin, with Lightning."), html[small:])
        # In the main language one sentence is enough
        self.assertNotContains(self.page(shop, main), "counter-own")

    def test_the_card_in_italian_and_german(self):
        shop = Shop.objects.create(name="Bar", lightning=True, discount=10)
        html = self.client.get(f"/it/shops/s/{shop.id}/").content.decode()
        self.assertIn("Vorrei pagare in bitcoin, con Lightning.", html)
        self.assertIn("Con lo sconto del 10% per chi paga in bitcoin, grazie.", html)
        self.assertIn("<li>Chiedi di pagare in bitcoin, con Lightning.</li>", html)
        # German writes a space before %: a non-breaking one, or the number stays alone at the end of a line
        with override("de"):
            self.assertEqual(gettext("With the %(discount)s%% discount for paying in bitcoin, thank you.") % {"discount": 10},
                             "Mit den 10\u00a0% Rabatt für die Zahlung mit Bitcoin, danke.")

    def test_neither_a_historic_shop(self):
        for closed in (date(2020, 1, 1), date(2099, 1, 1)):
            shop = Shop.objects.create(name="Bar", lightning=True, discount=5, closed_on=closed)
            page = self.client.get(f"/en/shops/s/{shop.id}/")
            for absent in ("How to pay here", "show-counter", "counter-card", "I would like to pay", "shop.js"):
                self.assertNotContains(page, absent)

    def test_nor_a_suspended_one(self):
        shop = Shop.objects.create(name="Bar", lightning=True, discount=5, phone="123456",
                                   suspended=True, latitude=1, longitude=1)
        page = self.client.get(f"/en/shops/s/{shop.id}/")
        for absent in ("How to pay here", "show-counter", "counter-card", "I would like to pay", "123456"):
            self.assertNotContains(page, absent)
        self.assertEqual(page["X-Robots-Tag"], "noindex, nofollow")

    def test_a_shop_off_the_maps_still_says_how_to_pay(self):
        shop = Shop.objects.create(name="Bar", listed=False)
        page = self.client.get(f"/en/shops/s/{shop.id}/")
        self.assertContains(page, "How to pay here")
        self.assertEqual(page["X-Robots-Tag"], "noindex, nofollow")

    def test_only_scripts_from_files(self):
        shop = Shop.objects.create(name="Bar", latitude=1, longitude=1)
        page = self.client.get(f"/en/shops/s/{shop.id}/")
        self.assertIn("script-src 'self'", page["Content-Security-Policy"])
        scripts = re.findall(r"<script[^>]*>", page.content.decode())
        self.assertTrue(scripts)
        for script in scripts:
            self.assertRegex(script, r'src="/static/|type="speculationrules"|type="application/ld\+json"')


class Nearby(TestCase):
    def setUp(self):
        self.here = Shop.objects.create(name="Here", latitude=45.0, longitude=11.0)

    def place(self, name, north=0, east=0, **fields):
        return Shop.objects.create(name=name, latitude=45.0 + north / DEGREE,
                                   longitude=11.0 + east / (DEGREE * 0.7071067811865476), **fields)

    def test_the_closest_four_first(self):
        for name, metres in (("C", 300), ("A", 100), ("E", 500), ("B", 200), ("D", 400), ("F", 900)):
            self.place(name, north=metres)
        found = nearby_shops(self.here)
        self.assertEqual([shop.name for shop in found], ["A", "B", "C", "D"])
        self.assertAlmostEqual(found[0].metres, 100, delta=0.5)
        self.assertEqual([shop.minutes for shop in found], [2, 4, 5, 7])

    def test_same_distance_by_name(self):
        self.place("Zeta", north=200)
        self.place("Alfa", north=-200)
        self.assertEqual([shop.name for shop in nearby_shops(self.here)], ["Alfa", "Zeta"])

    def test_only_public_places_to_pay_within_a_walk(self):
        self.place("Suspended", north=50, suspended=True)
        self.place("Off the maps", north=60, listed=False)
        self.place("Closed", north=70, closed_on=date(2020, 1, 1))
        self.place("Closing", north=75, closed_on=date(2099, 1, 1))
        Shop.objects.create(name="Nowhere")
        self.place("Far north", north=1300)
        self.place("Far east", east=1300)
        self.place("East", east=1000)
        self.place("Open", north=80)
        self.assertEqual([shop.name for shop in nearby_shops(self.here)], ["Open", "East"])

    def test_a_circle_not_a_square(self):
        # 900 m north and 900 m east: inside the box the query asks for, 1273 m away
        self.place("Corner", north=900, east=900)
        self.place("Side", north=1150)
        self.assertEqual([shop.name for shop in nearby_shops(self.here)], ["Side"])

    def test_a_shop_without_a_position_has_no_neighbours(self):
        self.place("Open", north=80)
        self.assertEqual(nearby_shops(Shop.objects.create(name="Nowhere")), [])

    def test_the_distance_as_the_page_writes_it(self):
        self.assertEqual(distance_text(3), "10 m")
        self.assertEqual(distance_text(345), "350 m")
        self.assertEqual(distance_text(994), "990 m")
        with override("en"):
            for metres, text in ((995, "1.0 km"), (999, "1.0 km"), (1049, "1.0 km"), (1050, "1.1 km"), (1099, "1.1 km"),
                                 (1149, "1.1 km"), (1199, "1.2 km")):
                self.assertEqual(distance_text(metres), text, metres)
        with override("it"):
            self.assertEqual(distance_text(1199), "1,2 km")

    def test_on_the_page(self):
        self.place("Far", north=600, discount=7)
        near = self.place("Near", north=120)
        page = self.client.get(f"/en/shops/s/{self.here.id}/")
        html = page.content.decode()
        self.assertContains(page, '<h2 id="shop-nearby-title">Nearby</h2>')
        self.assertLess(html.index("<strong>Near</strong>"), html.index("<strong>Far</strong>"))
        self.assertContains(page, f'<a href="/en/shops/s/{near.id}/"><strong>Near</strong> <span>120 m, 2 min on foot</span></a>')
        self.assertContains(page, '<b class="discount">-7%</b>')
        self.assertContains(self.client.get(f"/it/shops/s/{near.id}/"), "<span>120 m, 2 min a piedi</span>")
        # A historic shop sends people to the places where they can still pay
        Shop.objects.filter(pk=self.here.pk).update(closed_on=date(2020, 1, 1))
        self.assertContains(self.client.get(f"/en/shops/s/{self.here.id}/"), "<strong>Near</strong>")

    def test_no_section_without_neighbours(self):
        self.place("Far", north=5000)
        self.assertNotContains(self.client.get(f"/en/shops/s/{self.here.id}/"), "shop-nearby-title")

    def test_more_shops_do_not_cost_more_queries(self):
        def queries():
            with CaptureQueriesContext(connection) as captured:
                self.client.get(f"/en/shops/s/{self.here.id}/")
            return len(captured)

        self.place("One", north=100)
        queries()  # the first request of a test can warm up something else
        few = queries()
        for metres in range(200, 1200, 100):
            self.place(f"Shop {metres}", north=metres)
        self.assertEqual(queries(), few)


class CommunityLinks(TestCase):
    """`shop_page` in community.yml: the page for whoever has no wallet, and the community's pages about a shop."""

    def setUp(self):
        self.shop = Shop.objects.create(name="Bar", external_id="12")

    def configured(self, block):
        data = {key: value for key, value in community().items() if key != "shop_page"}
        if block is not None:
            data["shop_page"] = block
        return patch("config.community.community", return_value=data)

    def page(self, shop=None, language="en"):
        return self.client.get(f"/{language}/shops/s/{(shop or self.shop).id}/")

    def hub(self, language="en"):
        """The first screen of the customers' path, where the community's page for whoever has no wallet is linked"""
        return self.client.get(f"/{language}/shops/customer/{self.shop.id}/")

    def warnings(self):
        return [warning.msg for warning in skipped_shop_links(None)]

    def test_nothing_without_the_block(self):
        with self.configured(None):
            page = self.page()
            self.assertEqual(self.warnings(), [])
        self.assertNotContains(page, "No wallet yet")
        self.assertNotContains(page, "In our story")

    def test_the_wallet_page(self):
        """The community's page for whoever has no wallet is a line on the first screen of the customers' path, which
        the shop page leads to (shops/test_customer.py)"""
        hub = f"/en/shops/customer/{self.shop.id}/"
        with self.configured({"wallet": "learn/wallet/"}):
            self.assertContains(self.client.get(hub), '<a href="/en/learn/wallet/">More about wallets, from our community</a>')
            self.assertContains(self.client.get(hub.replace("/en/", "/it/")), 'href="/it/learn/wallet/"')
        with self.configured({"wallet": {"en": "wallet/", "it": "portafoglio/"}}):
            self.assertContains(self.client.get(hub.replace("/en/", "/it/")), 'href="/it/portafoglio/"')
        with self.configured({"wallet": "https://wallets.example.org/start"}):
            self.assertContains(self.client.get(hub), 'href="https://wallets.example.org/start"')
        with self.configured(None):
            self.assertNotContains(self.client.get(hub), "More about wallets")

    def test_the_two_ways_in_for_whoever_has_no_wallet(self):
        page = self.page()
        self.assertContains(page, f"<a href=\"/en/shops/customer/{self.shop.id}/1/\">I don't have a wallet</a>")
        self.assertContains(page, f"<a href=\"/en/shops/customer/{self.shop.id}/help/\">I have a wallet, but I'm not sure how to pay</a>")

    def test_no_way_in_for_a_shop_where_one_cannot_pay(self):
        historic = Shop.objects.create(name="Old", closed_on=date(2020, 1, 1))
        suspended = Shop.objects.create(name="Off", suspended=True)
        waiting = Shop.objects.create(name="Soon", awaiting_wallet=True)
        with self.configured({"wallet": "learn/wallet/"}):
            for shop in (historic, suspended, waiting):
                with self.subTest(shop.name):
                    page = self.page(shop)
                    self.assertEqual(page.status_code, 200)
                    self.assertNotContains(page, "Never paid with bitcoin?")
                    self.assertNotContains(page, "not sure how to pay")
                    self.assertNotContains(page, "/shops/customer/")
            self.assertContains(self.page(), "Never paid with bitcoin?")

    def test_a_wrong_wallet_link_stays_out(self):
        for wrong in ("/wallet/", "http://wallets.example.org/", "https://", "my wallet/", "a\\b", 42, {}, "",
                      "www.example.org/wallet", "mailto:info@example.org", "javascript:alert(1)", "../wallet/"):
            with self.subTest(wrong), self.configured({"wallet": wrong}):
                page = self.page()
                self.assertEqual(page.status_code, 200)
                self.assertNotContains(page, "No wallet yet")
                self.assertEqual(len(self.warnings()), 1, wrong)

    def test_the_stories_of_a_shop(self):
        block = {"stories": {12: [{"path": "walk/#bakery", "text": {"en": "A stop on the walk", "it": "Una tappa della passeggiata"}},
                                  {"path": "https://archive.example.org/1", "text": "<b>Old</b> photos"}],
                             "13": {"path": "history/", "text": "Not this shop"}}}
        with self.configured(block):
            page = self.page()
            self.assertContains(page, '<h2 id="shop-stories-title">In our story</h2>')
            self.assertContains(page, '<li><a href="/en/walk/#bakery">A stop on the walk</a></li>')
            self.assertContains(page, '<li><a href="https://archive.example.org/1">&lt;b&gt;Old&lt;/b&gt; photos</a></li>')
            self.assertNotContains(page, "Not this shop")
            self.assertContains(self.page(language="it"), '<a href="/it/walk/#bakery">Una tappa della passeggiata</a>')
            self.assertEqual(self.warnings(), [])
            # One link alone, without a list, is fine too
            other = Shop.objects.create(name="Other", external_id="13")
            self.assertContains(self.page(other), '<a href="/en/history/">Not this shop</a>')
            # A shop that signed up on its own has no external_id, and no stories
            self.assertNotContains(self.page(Shop.objects.create(name="New")), "In our story")

    def test_stories_on_a_historic_page_but_not_on_a_suspended_one(self):
        block = {"stories": {"12": [{"path": "walk/", "text": "A stop on the walk"}]}}
        with self.configured(block):
            Shop.objects.filter(pk=self.shop.pk).update(closed_on=date(2020, 1, 1))
            self.assertContains(self.page(), "A stop on the walk")
            Shop.objects.filter(pk=self.shop.pk).update(suspended=True)
            self.assertNotContains(self.page(), "A stop on the walk")

    def test_the_check_reads_every_language(self):
        main, other = settings.LANGUAGE_CODE, other_language()
        block = {"wallet": {main: "wallet/", other: "/wallet/"},
                 "stories": {12: [{"path": "walk/", "text": {main: "A stop on the walk", other: 2015}}]}}
        with self.configured(block):
            self.assertContains(self.page(language=main), "A stop on the walk")
            self.assertContains(self.hub(main), f'href="/{main}/wallet/"')
            self.assertNotContains(self.page(language=other), "walk/")
            self.assertNotContains(self.hub(other), "/wallet/")
            problems = self.warnings()
        self.assertEqual(problems, [f"`shop_page.wallet`: a page goes without the leading slash, an address with https://: '/wallet/' (in {other})",
                                    f"`shop_page.stories` 12, link 1: `text` must be text, between quotes: 2015 (in {other})"])

    def test_wrong_stories_stay_out_and_the_check_says_why(self):
        block = {"wallet": "wallet/", "stories": {12: [
            "walk/", {"path": "walk/"}, {"text": "No path"}, {"path": "/walk/", "text": "Leading slash"},
            {"path": "walk/", "text": {}}, {"path": "walk/#good", "text": "Good"}]}}
        with self.configured(block):
            page = self.page()
            self.assertEqual(page.status_code, 200)
            self.assertContains(page, '<li><a href="/en/walk/#good">Good</a></li>')
            self.assertNotContains(page, "No path")
            self.assertNotContains(page, "Leading slash")
            self.assertContains(self.hub(), 'href="/en/wallet/"')
            problems = self.warnings()
        self.assertEqual(len(problems), 5, problems)
        self.assertTrue(all(problem.startswith("`shop_page.stories` 12, link ") for problem in problems))
        with self.configured({"stories": {12: {"path": "walk/", "text": 2015}}}):
            self.assertNotContains(self.page(), "In our story")
            self.assertEqual(self.warnings(), ["`shop_page.stories` 12, link 1: `text` must be text, between quotes: 2015"])
        for block in ("not a block", {"stories": ["walk/"]}):
            with self.subTest(block), self.configured(block):
                self.assertEqual(self.page().status_code, 200)
                self.assertNotContains(self.page(), "In our story")
                self.assertEqual(len(self.warnings()), 1)

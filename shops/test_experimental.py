"""The experimental features (config/features.py, `experimental:` in community.yml): on-chain at the register and writing to
OpenStreetMap. Off unless the community writes `true`, and off they are as quiet as a feature turned off: the pages, the
register, the addresses, the commands and the texts; and what must never be lost while they are off."""
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from datetime import timedelta
from pathlib import Path
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import checks
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from config import features
from config.community import community

from . import certified, nostr, osm
from .forms import NewShopForm, ShopForm
from .models import ChainCount, Shop, StaffRegister
from .staff import new_code
from .views import till_methods


def on(*names):
    """The community's file with these experimental features turned on (`experimental:` in community.yml). A context
    manager, or a decorator of one test."""
    return patch("config.features.community", new=lambda: {**community(), "experimental": {name: True for name in names}})


class Turned:
    """For a class of tests about an experimental feature: turned on around each whole test (its setUp too), as in a
    community that wrote it in `experimental`. First among the bases."""
    experimental = ()

    def run(self, result=None):
        with on(*self.experimental):
            return super().run(result)


class OnChain(Turned):
    """On-chain at the register: `experimental: {onchain: true}`."""
    experimental = (features.ONCHAIN,)


class OsmUpload(Turned):
    """Writing to OpenStreetMap with `manage.py osm_upload`: `experimental: {osm_upload: true}`."""
    experimental = (features.OSM_UPLOAD,)


# After the helpers above, which these modules import from here
from .test_customer import configured, wallet
from .test_explorer import with_register
from .test_practice import practice_data
from .test_register import register_data
from .test_register_chain import BIP84_ACCOUNT, BIP84_RECEIVE, REGISTER
from .test_register_flow import ADDRESS, STATIC, RegisterRun, invoice
from .test_staff import PairingDrawn, staff_page
from .tests import admin_session, reset_limits


def switched(**values):
    """The community's file with this `experimental` block, whatever it says."""
    return patch("config.features.community", new=lambda: {**community(), "experimental": values})


class TheBlock(TestCase):
    def test_a_community_that_writes_nothing_has_them_all_off(self):
        self.assertEqual(features.experimental_states(), {name: False for name in features.EXPERIMENTAL})
        self.assertEqual(features.experimental_on(), [])
        self.assertEqual(features.experimental_problems(), [])
        # They are not among the features a community turns off
        self.assertFalse(set(features.EXPERIMENTAL) & set(features.FEATURES))
        self.assertEqual(features.switched_off(), [])

    def test_true_turns_one_on_and_leaves_the_others(self):
        with on(features.ONCHAIN):
            self.assertTrue(features.enabled(features.ONCHAIN))
            self.assertEqual(features.experimental_on(), [features.ONCHAIN])
            self.assertEqual(features.experimental_problems(), [])
            # The features that are on by default are not touched
            self.assertEqual(features.switched_off(), [])
        with switched(onchain=False):
            self.assertFalse(features.enabled(features.ONCHAIN))
            self.assertEqual(features.experimental_problems(), [])

    def test_only_the_word_true_turns_one_on_and_the_check_says_what_is_wrong(self):
        for wrong in ("true", "yes", "on", 1, [True], {"on": True}, None):
            with self.subTest(wrong), switched(onchain=wrong):
                self.assertFalse(features.enabled(features.ONCHAIN))
                self.assertEqual(len(features.experimental_problems()), 1)
                self.assertIn("it stays off", features.experimental_problems()[0])
        with switched(onchain="yes", teleporter=True):
            self.assertEqual(features.experimental_on(), [])
            found = features.experimental_problems()
            self.assertEqual(len(found), 2)
            self.assertTrue(any("experimental.teleporter" in problem and "does nothing" in problem for problem in found))
            warnings = [item for item in checks.run_checks() if item.id == "shops.W011"]
            self.assertEqual(len(warnings), 2)
            # The block of the features that are on by default has its own check, and nothing to say here
            self.assertEqual([item for item in checks.run_checks() if item.id == "shops.W003"], [])

    def test_a_block_that_is_not_a_block_leaves_everything_off(self):
        for wrong in ("on", True, ["onchain"]):
            with self.subTest(wrong), patch("config.features.community", new=lambda: {**community(), "experimental": wrong}):  # noqa: B023
                self.assertEqual(features.experimental_on(), [])
                self.assertEqual(len(features.experimental_problems()), 1)
        with patch("config.features.community", new=lambda: {**community(), "experimental": None}):
            self.assertEqual(features.experimental_problems(), [])

    def test_a_name_written_in_the_other_block_does_nothing(self):
        # `features: {onchain: true}` is not how it is turned on, and the check of that block says so
        with patch("config.features.community", new=lambda: {**community(), "features": {"onchain": True}}):
            self.assertFalse(features.enabled(features.ONCHAIN))
            self.assertEqual(len(features.problems()), 1)

    def test_the_pages_know_the_switches(self):
        self.assertEqual(self.client.get("/en/shops/map/").context["experimental"],
                         {name: False for name in features.EXPERIMENTAL})
        with on(features.ONCHAIN):
            self.assertIs(self.client.get("/en/shops/map/").context["experimental"]["onchain"], True)

    def test_the_example_community_documents_every_switch_and_its_block_is_valid_once_uncommented(self):
        import yaml

        from config.community import COMMUNITY_DIR

        lines = (COMMUNITY_DIR / "community.yml").read_text().splitlines()
        start = lines.index("# experimental:")
        block = []
        for line in lines[start:]:
            if not line.startswith("#  ") and line != "# experimental:":
                break
            block.append(line[2:])
        found = yaml.safe_load("\n".join(block))
        self.assertEqual(found, {"experimental": {name: True for name in features.EXPERIMENTAL}})
        # Commented: the example community has them off
        self.assertNotIn("experimental", community())

    def test_preflight_says_what_is_on(self):
        from shops.management.commands.preflight import feature_notes

        self.assertEqual(feature_notes(), [])
        with on(features.ONCHAIN):
            notes = feature_notes()
        self.assertEqual(len(notes), 1)
        self.assertIn("experimental features turned on", notes[0])
        self.assertIn("onchain", notes[0])


class Base(TestCase):
    def setUp(self):
        cache.clear()
        reset_limits()
        self.owner = get_user_model().objects.create_user("owner")
        self.shop = Shop.objects.create(name="Bakery", owner=self.owner, lightning_address="bar@ln.example.org", lightning=True,
                                        onchain=True, latitude=41.9, longitude=12.5, city="Town", discount=10,
                                        own_explorer="https://node.example.org:4081/api")
        self.staff = StaffRegister.objects.create(shop=self.shop, name="Anna")
        self.url = reverse("register", args=[self.shop.id])

    def connect(self, response):
        return response.headers["Content-Security-Policy"].split("connect-src ")[1].split(";")[0].split()

    def staff_page(self):
        return self.client_class().get(staff_page(self.staff))


class OnChainOffAtTheRegister(Base):
    """A community that wrote nothing: the register is Lightning only, and nothing of on-chain is on its pages."""

    def test_the_owner_register_has_no_on_chain_button_setting_or_script(self):
        self.client.force_login(self.owner)
        page = self.client.get(self.url).content.decode()
        for part in ('id="chain-ok"', "OK, on-chain", 'id="chain-key"', 'id="chain-start"', 'id="chain-path"', 'id="own-explorer"',
                     'id="confirm-above"', 'id="chain-gap"', "<legend>On-chain", "How on-chain works", "register-chain",
                     "data-chain="):
            self.assertNotIn(part, page, part)
        # The rest of the settings is there, and the screens of a sale too
        for part in ('id="settings-open"', 'id="currency"', 'id="discount-percent"', 'id="colour"', 'id="sources"', 'id="pay"',
                     'id="chain-state"', "data-qrcode=", "data-nostr="):
            self.assertIn(part, page, part)
        data = register_data(page)
        self.assertFalse({"chain_on", "explorers", "own_explorer", "chain_claim", "explorer_save"} & set(data))
        with on(features.ONCHAIN):
            page = self.client.get(self.url).content.decode()
        for part in ('id="chain-ok"', 'id="chain-key"', 'id="chain-start"', 'id="own-explorer"', 'id="confirm-above"',
                     'id="chain-gap"', "data-chain="):
            self.assertIn(part, page, part)
        data = register_data(page)
        self.assertEqual((data["chain_on"], data["own_explorer"]), (True, "https://node.example.org:4081/api"))
        self.assertEqual(data["chain_claim"]["url"], reverse("register_chain", args=[self.shop.id]))
        self.assertEqual(data["explorer_save"], reverse("register_explorer", args=[self.shop.id]))

    def test_no_explorer_is_opened_in_the_csp(self):
        with with_register(explorers=["https://ours.example.org/api"], price_sources=["kraken", "coinbase"]):
            response = self.client.get(self.url)
            connect = self.connect(response)
            self.assertEqual(connect, ["'self'", "https://ln.example.org", "https://*.ln.example.org", "https://api.kraken.com",
                                       "https://api.coinbase.com"])
            with on(features.ONCHAIN):
                connect = self.connect(self.client.get(self.url))
        self.assertIn("https://ours.example.org", connect)
        self.assertIn("https://node.example.org:4081", connect)

    def test_a_staff_register_gets_no_count_to_ask(self):
        data = register_data(self.staff_page().content.decode())
        self.assertFalse({"chain_on", "explorers", "own_explorer", "chain_claim"} & set(data))
        page = self.staff_page().content.decode()
        for part in ('id="chain-ok"', "OK, on-chain", "data-chain="):
            self.assertNotIn(part, page, part)
        with on(features.ONCHAIN):
            data = register_data(self.staff_page().content.decode())
        self.assertEqual(data["chain_claim"], {"url": reverse("staff_chain", args=[self.staff.id]), "key": self.staff.key})

    def test_the_count_and_the_own_explorer_answer_404_to_everybody(self):
        body = json.dumps({"chain": "a" * 64, "at_least": 0})
        staff = json.dumps({"chain": "a" * 64, "at_least": 0, "k": self.staff.key})
        urls = [(reverse("register_chain", args=[self.shop.id]), body), (reverse("staff_chain", args=[self.staff.id]), staff),
                (reverse("register_explorer", args=[self.shop.id]), json.dumps({"explorer": "https://other.example.org/api"}))]
        for signed_in in (False, True):
            if signed_in:
                self.client.force_login(self.owner)
            for url, sent in urls:
                with self.subTest(url, signed_in=signed_in):
                    self.assertEqual(self.client.post(url, sent, content_type="application/json").status_code, 404)
                    self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(ChainCount.objects.count(), 0)
        self.shop.refresh_from_db()
        self.assertEqual(self.shop.own_explorer, "https://node.example.org:4081/api")
        with on(features.ONCHAIN):
            self.assertEqual(self.client.post(urls[0][0], body, content_type="application/json").json(), {"index": 0})

    @override_settings(FAKE_EXPLORER=True)
    def test_the_fake_explorer_is_not_there_either(self):
        from . import explorer
        from .register import explorers

        self.assertFalse(explorer.enabled())
        for url in ("/dev/explorer/", "/dev/explorer/api", "/dev/explorer/api/address/tb1qnothing0000000/txs"):
            self.assertEqual(self.client.get(url).status_code, 404, url)
        self.assertEqual(self.client.post("/dev/explorer/", {"address": "tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx",
                                                             "sats": 9000, "action": "mempool"}).status_code, 404)
        self.assertNotIn("explorers", register_data(self.client.get(self.url).content.decode()))
        with on(features.ONCHAIN):
            self.assertTrue(explorer.enabled())
            self.assertEqual(explorers(), ["/dev/explorer/api"])
            self.assertEqual(self.client.get("/dev/explorer/api/address/tb1qnothing0000000/txs").status_code, 200)

    def test_nothing_is_deleted_and_turned_on_everything_is_back(self):
        ChainCount.objects.create(shop=self.shop, chain="a" * 64, next=7)
        self.client.force_login(self.owner)
        self.client.get(self.url)
        self.staff_page()
        self.shop.refresh_from_db()
        self.assertEqual((self.shop.onchain, self.shop.own_explorer), (True, "https://node.example.org:4081/api"))
        self.assertEqual(ChainCount.objects.get(shop=self.shop).next, 7)
        with on(features.ONCHAIN):
            answer = self.client.post(reverse("register_chain", args=[self.shop.id]), json.dumps({"chain": "a" * 64, "at_least": 0}),
                                      content_type="application/json")
        self.assertEqual(answer.json(), {"index": 7})

    def test_the_pairing_of_a_staff_register_carries_nothing_of_on_chain(self):
        self.client.force_login(self.owner)
        new_code(self.staff)
        url = reverse("staff_code", args=[self.shop.id, self.staff.id])
        page = self.client.get(url)
        data = page.context["pairing"]
        self.assertFalse({"chain_on", "chain_open", "key", "onchain", "xpub", "address"} & set(data))
        self.assertFalse({"chain_key", "chain_fixed", "lightning_only"} & set(data["texts"]))
        self.assertEqual(data["texts"]["preferences"], "Discount {discount}%, currency {currency}.")
        self.assertNotIn("on-chain", page.content.decode().lower())
        self.assertContains(page, "so that your settings come along")
        # An expired code: the page says when to make a new one, without the on-chain key
        StaffRegister.objects.filter(pk=self.staff.pk).update(code_until=timezone.now() - timedelta(minutes=1))
        expired = self.client.get(url)
        self.assertContains(expired, "Changed your settings or the Lightning address?")
        self.assertNotIn("on-chain", expired.content.decode().lower())
        with on(features.ONCHAIN):
            self.assertContains(self.client.get(url), "the on-chain key or the Lightning address?")
            new_code(self.staff)
            data = self.client.get(url).context["pairing"]
        self.assertEqual(data["chain_open"]["url"], reverse("register_chain", args=[self.shop.id]))
        self.assertIn("a confirmation above", data["texts"]["preferences"])

    def test_the_practice_register_has_four_exercises_and_no_on_chain_move(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("practice", args=[self.shop.id]))
        page = response.content.decode()
        self.assertEqual(re.findall(r'data-move="(\w+)"', page), ["pay", "none", "down", "up", "phone"])
        self.assertEqual(re.findall(r'data-exercise="(\w+)"', page), ["take", "offline", "expired", "phone", "real"])
        for part in ('id="chain-ok"', "OK, on-chain", "Does not understand the code", "register-chain"):
            self.assertNotIn(part, page, part)
        texts = practice_data(response)["texts"]
        self.assertFalse({"confused", "chain_ready"} & set(texts))
        self.assertNotIn("on-chain", json.dumps(texts).lower())
        self.assertNotIn("chain_on", register_data(page))
        staff = self.client_class().get(f"{reverse('staff_practice', args=[self.staff.id])}?k={self.staff.key}").content.decode()
        self.assertEqual(re.findall(r'data-exercise="(\w+)"', staff), ["take", "offline", "expired", "phone", "real"])
        with on(features.ONCHAIN):
            page = self.client.get(reverse("practice", args=[self.shop.id])).content.decode()
        self.assertIs(register_data(page)["chain_on"], True)
        self.assertIn('data-exercise="onchain"', page)


class OnChainOffOnThePages(Base):
    """What the owner is offered and what the public reads: nobody is told that a shop takes on-chain."""

    def test_the_new_shop_form_does_not_offer_the_sign_and_leaves_what_was_ticked(self):
        self.assertNotIn("onchain", ShopForm(instance=self.shop).fields)
        self.assertNotIn("onchain", NewShopForm(owner=self.owner).fields)
        self.client.force_login(self.owner)
        self.assertNotContains(self.client.get(reverse("new_shop")), 'name="onchain"')
        self.assertNotContains(self.client.get(reverse("new_shop")), "on-chain")
        # A form posted with the tick all the same does not write it, and a form saved without it does not clear it
        other = Shop.objects.create(name="Other", owner=self.owner)
        data = {"name": "Other", "lightning_address": "other@ln.example.org", "onchain": "on", "discount": 0}
        with patch("shops.forms.validate_lightning_address"):
            form = ShopForm(data, instance=other, owner=self.owner)
            form.is_valid()
            self.assertNotIn("onchain", form.cleaned_data)
            kept = ShopForm({**data, "name": "Bakery", "lightning_address": "bar@ln.example.org"}, instance=self.shop,
                            owner=self.owner)
            kept.is_valid()
            self.assertNotIn("onchain", kept.cleaned_data)
            with on(features.ONCHAIN):
                self.assertIn("onchain", ShopForm(instance=self.shop).fields)
                self.assertContains(self.client.get(reverse("new_shop")), 'name="onchain"')
        self.shop.refresh_from_db()
        self.assertTrue(self.shop.onchain)

    def test_the_shop_page_the_map_and_the_json_do_not_say_on_chain(self):
        self.assertEqual(till_methods(self.shop), "lightning")
        page = self.client.get(reverse("shop_detail", args=[self.shop.id])).content.decode()
        self.assertIn("<li>Ask to pay in bitcoin, with Lightning.</li>", page)
        self.assertNotIn("on-chain", page.lower())
        found = self.client.get(reverse("shops_json")).json()["shops"][0]
        self.assertEqual(found["name"], "Bakery")
        # Absent, not false: nothing is known
        self.assertNotIn("onchain", found)
        self.assertIs(found["lightning"], True)
        map_page = self.client.get("/en/shops/map/")
        self.assertNotIn("on-chain", map_page.content.decode().lower())
        self.assertNotIn("onchain", map_page.context["paid_texts"])
        self.assertNotIn("onchain", certified.texts())
        self.assertNotIn("onchain", [mark["kind"] for mark in certified.marks(self.shop)])
        self.assertNotIn("on-chain", self.client.get("/en/shops/").content.decode().lower())
        cache.clear()
        with on(features.ONCHAIN):
            self.assertEqual(till_methods(self.shop), "both")
            self.assertIs(self.client.get(reverse("shops_json")).json()["shops"][0]["onchain"], True)
            self.assertIn("On-chain payments too", self.client.get("/en/shops/map/").content.decode())
            self.assertEqual([mark["kind"] for mark in certified.marks(self.shop)][-1], "onchain")

    def test_nostr_and_openstreetmap_are_not_told_either(self):
        event = nostr.shop_event("11" * 32, self.shop, "https://example.org/s/1")
        self.assertNotIn("chain", json.dumps(event).lower())
        self.assertEqual(osm.methods(self.shop), [osm.LIGHTNING])
        with on(features.ONCHAIN):
            self.assertEqual(osm.methods(self.shop), [osm.LIGHTNING, osm.ONCHAIN])
            self.assertNotIn("chain", json.dumps(nostr.shop_event("11" * 32, self.shop, "https://example.org/s/1")).lower())

    def test_a_shop_with_its_own_till_and_no_lightning_flag_is_said_to_take_bitcoin(self):
        imported = Shop.objects.create(name="Books", latitude=41.9, longitude=12.5)
        self.assertEqual(till_methods(imported), "bitcoin")
        page = self.client.get(reverse("shop_detail", args=[imported.id])).content.decode()
        self.assertIn("<li>Ask to pay in bitcoin.</li>", page)
        self.assertIn("<li>Scan it with your wallet, check the amount and confirm.</li>", page)
        self.assertIn('<span class="counter-ask">I would like to pay in bitcoin.</span>', page)
        self.assertNotIn("on-chain", page.lower())
        self.assertNotIn("Lightning", page)
        # The customer's path: every wallet of the list, and no word on the way
        with configured(wallets=[wallet("Lightning Only", methods=["lightning"]), wallet("Chain Only", methods="onchain")]):
            step = self.client.get(f"/en/shops/customer/{imported.id}/1/?on=all").content.decode()
        self.assertIn("<strong>Lightning Only</strong>", step)
        self.assertIn("<strong>Chain Only</strong>", step)
        help_page = self.client.get(reverse("customer_help", args=[imported.id])).content.decode()
        self.assertNotIn("on-chain", help_page.lower())
        self.assertNotIn("What to expect", help_page)
        self.assertIn("<h2>If your wallet says error</h2>", help_page)

    def test_the_customer_path_of_a_shop_that_ticked_on_chain_is_the_lightning_one(self):
        for path in ("", "help/", "1/", "2/", "3/"):
            with self.subTest(path):
                page = self.client.get(f"/en/shops/customer/{self.shop.id}/{path}").content.decode()
                self.assertNotIn("on-chain", page.lower())
        help_page = self.client.get(reverse("customer_help", args=[self.shop.id])).content.decode()
        self.assertIn("Here it works with Lightning.", help_page)
        self.assertIn("Your wallet holds less than the price. Add some bitcoin to the wallet, or pay the rest another way.",
                      help_page)
        self.assertNotIn("ten minutes", help_page)
        with on(features.ONCHAIN):
            help_page = self.client.get(reverse("customer_help", args=[self.shop.id])).content.decode()
        self.assertIn("Lightning or on-chain, whichever your wallet uses.", help_page)
        self.assertIn("An on-chain payment also needs a little more", help_page)

    def test_the_diagnosis_and_the_merchants_help_do_not_mention_it(self):
        from . import diagnosis

        found = diagnosis.diagnose(self.shop)
        text = json.dumps([finding.as_dict() for finding in found], default=str).lower()
        self.assertNotIn("on-chain", text)
        self.assertNotIn("onchain", text)
        self.assertIn("the shop takes lightning", text)
        self.client.force_login(self.owner)
        for url in (reverse("help_home", args=[self.shop.id]), reverse("shops")):
            self.assertNotIn("on-chain", self.client.get(url).content.decode().lower(), url)

    def test_the_administration_does_not_show_the_tick_or_the_own_explorer(self):
        staff = self.client_class()
        admin_session(staff)
        url = reverse("admin:shops_shop_change", args=[self.shop.pk])
        page = staff.get(url).content.decode()
        self.assertNotIn('name="onchain"', page)
        self.assertNotIn('name="own_explorer"', page)
        self.assertNotIn("on-chain", page.lower())
        self.assertIn('name="discount"', page)
        # What the form does not show cannot be asked in the address of the list (config/admin_secrets.py)
        listing = reverse("admin:shops_shop_changelist")
        self.assertEqual(staff.get(listing, {"own_explorer__startswith": "https"}).status_code, 400)
        self.assertEqual(staff.get(listing, {"onchain__exact": "1"}).status_code, 400)
        with on(features.ONCHAIN):
            page = staff.get(url).content.decode()
            self.assertEqual(staff.get(listing, {"own_explorer__startswith": "https"}).status_code, 200)
        self.assertIn('name="onchain"', page)
        self.assertIn('name="own_explorer"', page)
        self.shop.refresh_from_db()
        self.assertEqual((self.shop.onchain, self.shop.own_explorer), (True, "https://node.example.org:4081/api"))

    def test_a_shop_that_moved_is_told_to_connect_its_lightning_address_only(self):
        from types import SimpleNamespace

        from . import moving

        outcome = SimpleNamespace(sign_in="wallet", email="", name="Bakery", code="", shop=SimpleNamespace(
            owner_id=1, moved_register=True))
        message = moving.owner_message(outcome, "site.example", "en")
        self.assertIn("with your Lightning address, as before", message)
        self.assertNotIn("on-chain", message)
        with on(features.ONCHAIN):
            self.assertIn("your on-chain key, or both, as before", moving.owner_message(outcome, "site.example", "en"))


# The owner's register in the node tests, and the count of its chain kept on the phone
OWNER = {"ledger": "shop-1", "shop": "shop-1"}
COUNT = {"of": f"{BIP84_ACCOUNT}/0", "next": 4}


class OnChainOffInTheRegister(RegisterRun):
    """static/register.js in node, as the page of a community that has not turned on-chain on gives it (no `chain_on`), on a
    phone that holds everything of when it was on: the key, the count, the on-chain sales. Even with an explorer and a
    count to ask left in the page, the register asks neither."""

    def storage(self, **more):
        return {"register-settings:shop-1": json.dumps({"lightning": ADDRESS, "onchain": {"key": BIP84_ACCOUNT, "path": "0"},
                                                        "discount": 0, "confirmAbove": 5000, "currency": "EUR"}),
                "register-chain-next:shop-1": json.dumps(COUNT), **more}

    def sale(self, name, **fields):
        now = time.time() * 1000
        return {"id": name, "at": now - 60000, "cents": 900, "currency": "EUR", "status": "pending", "method": "onchain",
                "sats": 9000, "address": BIP84_RECEIVE[0], "path": "0", "index": 0, "before": [], "wait": False,
                "label": "9.00 EUR", "rate": 100000, "sources": [], "expires": now + 3600000, "till": "shop-1", **fields}

    def test_a_phone_that_holds_a_key_offers_no_on_chain_sale(self):
        out = self.run_register(OWNER, self.storage(), chain_on=False)
        self.assertTrue(out["chainHidden"])
        self.assertEqual((out["blocked"], out["error"], out["chainError"], out["gap"], out["gapCount"]), ("", "", "", "", ""))
        # The button pressed all the same (a page left open): no sale, no address, no question to the explorer or the site
        self.assertIsNone(out.get("sale"))
        self.assertEqual((out["to"], out["asked"], out["claims"], out["ledger"]), ("", [], [], []))
        self.assertEqual(json.loads(out["next"]), COUNT)
        # With on-chain on, the same phone takes the sale at the address its count says
        on_again = self.run_register(OWNER, self.storage(), shopCount=4)
        self.assertFalse(on_again["chainHidden"])
        self.assertEqual(on_again["sale"]["index"], 4)

    def test_lightning_goes_on_as_always(self):
        pr, _hash = invoice(10000)
        out = self.run_register(OWNER, self.storage(), chain_on=False, discount=0,
                                ln={"typed": ["1000"], "invoices": [pr],
                                    "verify": {0: {"status": "OK", "settled": True, "preimage": "00" * 32, "pr": pr}}})
        sale = out["ledger"][0]
        self.assertEqual((sale["status"], sale["method"], sale["sats"]), ("paid", "lightning", 10000))
        self.assertFalse([url for url in out["sales"][0]["ledger"] if "address" in url])

    def test_saving_the_settings_keeps_the_key_the_threshold_and_the_count(self):
        # The form has no on-chain field: whatever a stale page still held in them is not read
        for fields in ({"currency": "CHF", "discount-percent": "5"},
                       {"currency": "CHF", "discount-percent": "5", "chain-key": "", "chain-start": "7", "chain-path": "0/9",
                        "confirm-above": "1"},
                       {"currency": "CHF", "discount-percent": "5", "chain-key": "not a key", "confirm-above": "nonsense"}):
            with self.subTest(fields):
                out = self.run_register(OWNER, self.storage(), chain_on=False, settings=fields, open_settings=True)
                self.assertEqual(out["settingsSay"],
                                 "Saved on this phone. Staff registers get the new settings only with a new pairing code.")
                saved = out["settings"]
                self.assertEqual(saved["onchain"], {"key": BIP84_ACCOUNT, "path": "0"})
                self.assertEqual((saved["confirmAbove"], saved["currency"], saved["discount"]), (5000, "CHF", 5))
                self.assertEqual(json.loads(out["nextAfter"]), COUNT)
                self.assertEqual(out["asked"], [])
        # A phone that never had on-chain saves none
        plain = {"register-settings:shop-1": json.dumps({"lightning": ADDRESS})}
        out = self.run_register(OWNER, plain, chain_on=False, settings={"currency": "EUR", "discount-percent": "0"})
        self.assertNotIn("onchain", out["settings"])
        self.assertNotIn("confirmAbove", out["settings"])
        self.assertIsNone(out.get("nextAfter"))

    def test_on_chain_sales_left_waiting_are_kept_and_go_at_once_to_check_in_the_wallet(self):
        now = time.time() * 1000
        addresses = self.derived_many(BIP84_ACCOUNT, 0, range(5))
        sales = [self.sale("waiting"), self.sale("over", at=now - 2 * 86400000, expires=now - 86400000, index=1,
                                                 address=addresses[1]),
                 self.sale("paid", status="paid", txid="a" * 64, index=2, address=addresses[2]),
                 self.sale("seen", status="seen", index=3, address=addresses[3]),
                 self.sale("cancelled", status="cancelled", index=4, address=addresses[4])]
        paid = {sale["address"]: [{"txid": "f" * 64, "status": {"confirmed": True, "block_time": int(now / 1000)},
                                   "vout": [{"scriptpubkey_address": sale["address"], "value": 9000}]}] for sale in sales}
        out = self.run_register(OWNER, self.storage(**{"register-ledger:shop-1": json.dumps(sales)}), chain_on=False,
                                atStart=paid)
        # Nobody is asked, even with an explorer that would say «paid»; nothing is lost, and nothing is called paid. What
        # was waiting is not left to say «the register looks again»: nobody will, so it is to check in the wallet at once
        self.assertEqual(out["asked"], [])
        by_id = {sale["id"]: sale for sale in out["ledger"]}
        self.assertEqual(sorted(by_id), ["cancelled", "over", "paid", "seen", "waiting"])
        self.assertEqual({name: sale["status"] for name, sale in by_id.items()},
                         {"waiting": "unchecked", "over": "unchecked", "paid": "paid", "seen": "unchecked",
                          "cancelled": "cancelled"})
        self.assertEqual((by_id["paid"]["address"], by_id["paid"]["txid"]), (addresses[2], "a" * 64))
        # No new sale: the button pressed starts none
        self.assertEqual(len(out["ledger"]), 5)
        self.assertEqual(out["claims"], [])

    def test_an_on_chain_sale_that_was_on_the_screen_is_not_put_back(self):
        script = """
          await wait(300);
          return { pay: element("pay").hidden, to: element("to").textContent, qr: element("qr").src,
            error: element("error").textContent, open: kept.get("register-open:shop-1") ?? null, ledger: ledger(),
            asked: asked.filter((url) => url.includes("/address/")) };
        """
        storage = self.storage(**{"register-ledger:shop-1": json.dumps([self.sale("open")]),
                                  "register-open:shop-1": json.dumps("open")})
        out = self.run_register(OWNER, storage, chain_on=False, script=script)
        self.assertEqual((out["to"], out["qr"], out["error"], out["open"], out["asked"]), ("", "", "", None, []))
        self.assertEqual([(sale["id"], sale["status"]) for sale in out["ledger"]], [("open", "unchecked")])
        back = self.run_register(OWNER, storage, script=script)
        self.assertIn(BIP84_RECEIVE[0], back["to"])
        self.assertEqual(back["open"], json.dumps("open"))

    def test_the_history_and_the_day_closing_still_show_the_on_chain_sales(self):
        at = time.mktime((2026, 11, 9, 12, 0, 0, 0, 0, -1)) * 1000
        sales = [self.sale("chain", at=at, status="paid", txid="a" * 64),
                 {"id": "ln", "at": at + 1000, "cents": 500, "currency": "EUR", "status": "paid", "sats": 5000,
                  "label": "5.00 EUR", "till": "shop-1"}]
        out = self.run_register(OWNER, self.storage(**{"register-ledger:shop-1": json.dumps(sales)}), chain_on=False,
                                show_day="2026-11-09")
        totals = "\\n".join(out["dayTotals"])
        self.assertIn("On-chain: 1 paid", totals)
        self.assertIn("Lightning: 1 paid", totals)
        self.assertEqual(len(out["daySales"]), 2)
        self.assertEqual(len(out["ledger"]), 2)

    def test_a_staff_register_paired_with_on_chain_takes_lightning_only(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        harness = Path(folder.name) / "code.js"
        harness.write_text(r"""
const fs = require("fs");
for (const f of process.argv.slice(3)) eval(fs.readFileSync(f, "utf8"));
(async () => {
  const key = await registerDerive.childKey(process.argv[2], 0, "mainnet");
  process.stdout.write(registerPairing.encode({ register: "REGISTER", lightning: "ADDRESS", onchain: { key, path: "0" }, confirmAbove: 0,
    discount: 0, currency: "EUR", sources: ["kraken", "coinbase"], shop: { name: "Bar Anna", town: "Example" } },
    ["kraken", "coinbase", "bitstamp"]));
})();
""".replace("REGISTER", REGISTER).replace("ADDRESS", ADDRESS))
        made = subprocess.run([self.node, str(harness), BIP84_ACCOUNT, str(STATIC / "register-derive.js"),
                               str(STATIC / "register-pairing.js")], capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(made.returncode, 0, made.stderr)
        ledger = f"shop-1:{REGISTER}"
        dataset = {"ledger": ledger, "shop": "shop-1", "staff": REGISTER, "tillName": "Anna"}
        kept = {f"register-pairing:{ledger}": json.dumps({"code": made.stdout, "at": 1})}
        claim = {"url": f"/en/shops/register/p/{REGISTER}/chain/", "key": "k" * 40}
        script = """
          await wait(300);
          for (const k of "1000") key(k);
          listeners["chain-ok:click"]?.forEach((fn) => fn());
          await wait(300);
          return { blocked: element("blocked-text").textContent, hidden: element("chain-ok").hidden,
            paired: element("paired").textContent, shop: element("shop-name").textContent, ledger: ledger(), claims,
            asked: asked.filter((url) => url.includes("/address/")), kept: kept.has(`register-pairing:${given.dataset.ledger}`) };
        """
        out = self.run_register(dataset, kept, chain_on=False, chain_claim=claim, script=script)
        # Paired as before, to the same shop, and the pairing stays on the phone; of on-chain, nothing
        self.assertEqual((out["blocked"], out["hidden"], out["shop"], out["kept"]), ("", True, "Bar Anna", True))
        self.assertIn("Paired with the owner's code", out["paired"])
        self.assertNotIn("On-chain", out["paired"])
        self.assertEqual((out["ledger"], out["claims"], out["asked"]), ([], [], []))
        with_it = self.run_register(dataset, kept, chain_claim=claim, script=script)
        self.assertIn("On-chain: yes.", with_it["paired"])
        self.assertEqual(len(with_it["claims"]), 1)


class PairingDrawnWithOnChainOff(TestCase):
    """static/staff-pair.js in node, on the owner's phone that still holds an on-chain key: the code carries none."""

    def setUp(self):
        self.node = os.environ.get("NODE") or shutil.which("node")
        if not self.node:
            self.skipTest("node is missing: NODE=/path/to/node to draw the pairing code")
        owner = get_user_model().objects.create(username="owner")
        self.shop = Shop.objects.create(owner=owner, name="Bar", city="Example", lightning_address="till@ln.example.org")
        self.register = StaffRegister.objects.create(shop=self.shop, name="Anna", code="c" * 32,
                                                     code_until=timezone.now() + timedelta(hours=1))

    def test_the_code_carries_no_key_and_the_summary_no_word_of_it(self):
        for held in ({"key": BIP84_ACCOUNT}, {"key": BIP84_ACCOUNT, "path": "0/5"}, {"address": BIP84_RECEIVE[0]}):
            with self.subTest(held):
                out = PairingDrawn.draw(self, {"lightning": "till@ln.example.org", "onchain": held, "discount": 7,
                                              "currency": "CHF", "confirmAbove": 2500, "sources": ["kraken", "bitstamp"]})
                self.assertTrue(out["qr"])
                self.assertIn("#pair=1.", out["link"])
                self.assertIsNone(out["pairing"]["onchain"])
                self.assertEqual((out["pairing"]["lightning"], out["pairing"]["discount"], out["pairing"]["currency"]),
                                 ("till@ln.example.org", 7, "CHF"))
                summary = " ".join(out["summary"])
                self.assertNotIn("chain", summary.lower())
                self.assertNotIn(BIP84_ACCOUNT, out["link"])
                self.assertIn("Discount 7%, currency CHF.", summary)
                self.assertNotIn("{", summary)
        with on(features.ONCHAIN):
            out = PairingDrawn.draw(self, {"lightning": "till@ln.example.org", "onchain": {"key": BIP84_ACCOUNT}})
        self.assertEqual(out["pairing"]["onchain"]["path"], "0")


BROWSER = os.environ.get("BROWSER_BIN") or shutil.which("chromium-headless-shell")
# In the page, after its own scripts: what went wrong while it ran, then the owner opens the settings from the menu and
# saves them; what the page and the phone's memory hold at the end goes in the title, for --dump-dom
WATCH = """<script>
const wrong = [];
addEventListener("error", (event) => wrong.push(String(event.message)));
addEventListener("unhandledrejection", (event) => wrong.push(String(event.reason)));
localStorage.clear();
for (const [key, value] of Object.entries(%s)) localStorage.setItem(key, value);
</script>"""
REPORT = """<script>
const later = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
addEventListener("load", async () => {
  const shop = document.body.dataset.shop;
  await later(600);
  // The sale that was on the screen when the page closed: back on it, or not
  const pay = !document.getElementById("pay").hidden;
  // What the page loaded by itself at its opening, before any screen of the menu
  const atOpening = { derive: Boolean(window.registerDerive), chain: Boolean(window.registerChain) };
  document.getElementById("menu-open").click();
  document.getElementById("settings-open").click();
  await later(600);
  const settingsOpen = !document.getElementById("settings").hidden;
  const keyShown = document.getElementById("chain-key")?.value ?? null;
  document.getElementById("currency").value = "CHF";
  document.getElementById("settings-form").requestSubmit();
  await later(600);
  document.title = "result=" + encodeURIComponent(JSON.stringify({ wrong, settingsOpen, keyShown,
    say: document.getElementById("settings-say").textContent, button: Boolean(document.getElementById("chain-ok")),
    buttonHidden: document.getElementById("chain-ok")?.hidden ?? null, pay, atOpening,
    chainLoaded: Boolean(window.registerChain),
    error: document.getElementById("error").textContent,
    settings: JSON.parse(localStorage.getItem(`register-settings:${shop}`)),
    next: localStorage.getItem(`register-chain-next:${shop}`), open: localStorage.getItem(`register-open:${shop}`),
    ledger: JSON.parse(localStorage.getItem(`register-ledger:${shop}`) || "[]").map((sale) => [sale.id, sale.status]) }));
});
</script>"""


@skipUnless(BROWSER or os.environ.get("REQUIRE_BROWSER") == "1", "no chromium-headless-shell")
class OnChainOffInABrowser(TestCase):
    """The owner's register page as the site gives it, with its own scripts, run in a headless Chromium on a phone's
    memory that holds a key, a count and on-chain sales: the node tests have a stand-in for every element, a browser has
    only the ones the page really has. Skipped without a browser, an error with REQUIRE_BROWSER=1."""

    def setUp(self):
        reset_limits()
        self.owner = get_user_model().objects.create(username="owner")
        self.shop = Shop.objects.create(owner=self.owner, name="Bakery", lightning_address="bar@ln.example.org")
        self.client.force_login(self.owner)

    def run_page(self):
        self.assertTrue(BROWSER, "REQUIRE_BROWSER=1 but chromium-headless-shell is missing: BROWSER_BIN=/path")
        html = self.client.get(reverse("register", args=[self.shop.id])).content.decode()
        now = time.time() * 1000
        sale = {"at": now - 60000, "cents": 900, "currency": "EUR", "method": "onchain", "sats": 9000, "path": "0",
                "before": [], "wait": False, "label": "9.00 EUR", "rate": 100000, "sources": [], "till": str(self.shop.id)}
        memory = {
            f"register-settings:{self.shop.id}": json.dumps({"lightning": "bar@ln.example.org", "confirmAbove": 5000,
                                                             "onchain": {"key": BIP84_ACCOUNT}}),
            f"register-chain-next:{self.shop.id}": json.dumps({"of": f"{BIP84_ACCOUNT}/0", "next": 2}),
            f"register-open:{self.shop.id}": json.dumps("open"),
            f"register-ledger:{self.shop.id}": json.dumps([
                {**sale, "id": "paid", "status": "paid", "address": BIP84_RECEIVE[0], "index": 0, "txid": "a" * 64},
                {**sale, "id": "open", "status": "pending", "address": BIP84_RECEIVE[1], "index": 1,
                 "expires": now + 3600000}])}

        def named(address):
            return re.sub(r"\.[0-9a-f]{12}\.js$", ".js", address.split("/static/", 1)[1])

        # The page's own scripts in the page, the ones loaded at first use from the files; nothing from the network
        html = re.sub(r'<script src="([^"]*/static/[^"]+)"></script>',
                      lambda match: f"<script>{(STATIC / named(match.group(1))).read_text()}</script>", html)
        html = re.sub(r'data-(\w+)="([^"]*/static/[^"]+\.js)"',
                      lambda match: f'data-{match.group(1)}="{(STATIC / named(match.group(2))).as_uri()}"', html)
        html = re.sub(r'<link rel="(?:stylesheet|manifest)"[^>]*>', "", html)
        html = html.replace("<head>", "<head>" + WATCH % json.dumps(memory)).replace("</body>", REPORT + "</body>")
        with tempfile.TemporaryDirectory() as folder:
            page = Path(folder) / "register.html"
            page.write_text(html)
            result = subprocess.run([BROWSER, "--no-sandbox", "--disable-gpu", f"--user-data-dir={folder}/profile",
                                     "--allow-file-access-from-files", "--window-size=360,640", "--virtual-time-budget=8000",
                                     "--dump-dom", page.as_uri()], capture_output=True, text=True, timeout=90, check=False)
        found = re.search(r"<title>result=([^<]+)</title>", result.stdout)
        self.assertTrue(found, result.stderr[-2000:])
        from urllib.parse import unquote

        return json.loads(unquote(found.group(1)))

    def test_the_page_runs_and_saves_its_settings_without_the_on_chain_fields(self):
        out = self.run_page()
        self.assertEqual(out["wrong"], [])
        self.assertEqual((out["button"], out["keyShown"], out["pay"], out["error"]), (False, None, False, ""))
        # Opening the register loads neither on-chain file, whatever the history holds; register-chain.js never loads
        # (register-derive.js comes with the history and the settings: the Nostr copies and the check of an imported
        # copy use it)
        self.assertEqual(out["atOpening"], {"derive": False, "chain": False})
        self.assertFalse(out["chainLoaded"])
        self.assertTrue(out["settingsOpen"])
        self.assertIn("Saved on this phone.", out["say"])
        # What the phone held of on-chain is all there after a save, and the sale that was on the screen is in the history
        self.assertEqual(out["settings"]["onchain"], {"key": BIP84_ACCOUNT})
        self.assertEqual((out["settings"]["confirmAbove"], out["settings"]["currency"]), (5000, "CHF"))
        self.assertEqual(json.loads(out["next"]), {"of": f"{BIP84_ACCOUNT}/0", "next": 2})
        self.assertIsNone(out["open"])
        self.assertEqual(out["ledger"], [["paid", "paid"], ["open", "unchecked"]])

    def test_turned_on_the_same_phone_has_it_all_back(self):
        with on(features.ONCHAIN), with_register(explorers=["https://explorer.invalid/api"]):
            out = self.run_page()
        self.assertEqual((out["button"], out["buttonHidden"], out["keyShown"]), (True, False, BIP84_ACCOUNT))
        self.assertEqual(out["atOpening"], {"derive": True, "chain": True})
        self.assertTrue(out["settingsOpen"])
        self.assertEqual(out["settings"]["onchain"], {"key": BIP84_ACCOUNT})
        self.assertEqual(json.loads(out["next"]), {"of": f"{BIP84_ACCOUNT}/0", "next": 2})
        # The sale that was on the screen is back on it
        self.assertTrue(out["pay"])
        self.assertEqual(json.loads(out["open"]), "open")

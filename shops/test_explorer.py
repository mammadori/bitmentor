"""Where the register sees on-chain payments: the community's explorers, the register's CSP that opens only those, and the
fake explorer of the tests and of practice sites, which cannot run on mainnet."""
import os
import subprocess
import sys
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings

from config.community import community

from . import explorer
from .models import Shop
from .register import explorer_problems, explorer_url, explorers
from .test_experimental import OnChain
from .test_register import register_data

RECORDED = "bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu"


def with_register(**block):
    return patch("shops.register.community", return_value={**community(), "register": block})


class Explorers(SimpleTestCase):
    def test_only_an_https_api_of_a_domain_name(self):
        self.assertEqual(explorer_url("https://Mempool.Example.org/signet/api/"), "https://mempool.example.org/signet/api")
        for wrong in ("http://mempool.space/api", "https://mempool.space:8443/api", "https://a@mempool.space/api",
                      "https://mempool.space/api?x=1", "https://mempool.space/api#x", "https://127.0.0.1/api",
                      "https://*.space/api", "https://mempool.space/api; script-src *", "https://mempool.space/a b",
                      "javascript:alert(1)", None, 3, ["https://mempool.space/api"]):
            self.assertIsNone(explorer_url(wrong), wrong)

    def test_by_network_or_one_list_and_mempool_space_otherwise(self):
        self.assertEqual(explorers(), ["https://mempool.space/signet/api"])
        with with_register(explorers={"mainnet": ["https://x.example.org/api"], "signet": "https://ours.example.org/api"}):
            self.assertEqual(explorers(), ["https://ours.example.org/api"])
        with with_register(explorers=["https://ours.example.org/api", "http://bad.example.org", "https://ours.example.org/api/"]):
            self.assertEqual(explorers(), ["https://ours.example.org/api"])
            self.assertEqual(len(explorer_problems()), 1)
        with with_register(explorers=["nonsense"]), override_settings(NETWORK="mainnet"):
            self.assertEqual(explorers(), ["https://mempool.space/api"])
        with with_register(explorers={"signet": ["https://ours.example.org/api", 7]}):
            self.assertEqual(explorer_problems(), ["register.explorers: 7 is not an https address of an explorer's API "
                                                   "(https://host/path): it is not used"])


class RegisterPolicy(OnChain, TestCase):
    def setUp(self):
        owner = get_user_model().objects.create(username="k")
        self.shop = Shop.objects.create(owner=owner, name="Bar", city="Example", discount=0,
                                        lightning_address="till@ln.example.org")
        self.url = f"/en/shops/register/{self.shop.id}/"

    def connect(self, response):
        return response.headers["Content-Security-Policy"].split("connect-src ")[1].split(";")[0].split()

    def test_the_csp_opens_only_the_community_explorers(self):
        with with_register(explorers=["https://ours.example.org/api", "https://evil.example.org:444/api"],
                           price_sources=["kraken", "coinbase"]):
            response = self.client.get(self.url)
        connect = self.connect(response)
        self.assertIn("https://ours.example.org", connect)
        self.assertNotIn("https://mempool.space", connect)
        self.assertFalse([origin for origin in connect if "evil" in origin])
        self.assertEqual(register_data(response.content.decode())["explorers"], ["https://ours.example.org/api"])

    def test_the_owner_starts_from_the_shop_discount_or_the_community_suggestion(self):
        with with_register(discount=10, confirm_above=50):
            data = register_data(self.client.get(self.url).content.decode())
        self.assertEqual((data["discount"], data["confirm_above"]), (10, 5000))
        self.assertEqual(data["shop"], {"name": "Bar", "town": "Example", "colour": community().get("theme_color")})
        Shop.objects.filter(pk=self.shop.pk).update(discount=5)
        with with_register(discount="a lot", confirm_above=-1):
            data = register_data(self.client.get(self.url).content.decode())
        self.assertEqual((data["discount"], data["confirm_above"]), (5, 0))

    @override_settings(FAKE_EXPLORER=True)
    def test_with_the_fake_explorer_the_register_asks_the_site(self):
        with with_register(price_sources=["kraken", "coinbase"]):
            response = self.client.get(self.url)
        self.assertEqual(register_data(response.content.decode())["explorers"], ["/dev/explorer/api"])
        self.assertNotIn("mempool.space", self.connect(response))


class FakeExplorer(OnChain, TestCase):
    def setUp(self):
        cache.clear()

    def test_off_unless_asked(self):
        self.assertFalse(settings.FAKE_EXPLORER)
        for url in (f"/dev/explorer/api/address/{RECORDED}/txs", "/dev/explorer/", "/dev/explorer/api"):
            self.assertEqual(self.client.get(url).status_code, 404, url)

    @override_settings(FAKE_EXPLORER=True, NETWORK="mainnet")
    def test_never_on_mainnet_whatever_the_settings_say(self):
        self.assertEqual(self.client.get(f"/dev/explorer/api/address/{RECORDED}/txs").status_code, 404)
        self.assertEqual(self.client.post("/dev/explorer/", {"address": RECORDED, "sats": 1, "action": "mempool"}).status_code, 404)

    def test_the_settings_refuse_it_on_mainnet(self):
        env = {**os.environ, "NETWORK": "mainnet", "FAKE_EXPLORER": "1", "DEBUG": "", "SECRET_KEY": "x"}
        result = subprocess.run([sys.executable, "-c", "import django; from django.conf import settings; settings.DEBUG"],
                                env={**env, "DJANGO_SETTINGS_MODULE": "config.settings"}, cwd=settings.BASE_DIR,
                                capture_output=True, text=True, timeout=60)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("FAKE_EXPLORER cannot be turned on on mainnet", result.stderr)

    @override_settings(FAKE_EXPLORER=True)
    def test_recorded_answers_and_pretend_payments(self):
        recorded = self.client.get(f"/dev/explorer/api/address/{RECORDED}/txs").json()
        self.assertEqual([tx["txid"] for tx in recorded], ["a" * 64])
        self.assertEqual(self.client.get("/dev/explorer/api/address/tb1qnothing0000000/txs").json(), [])
        self.assertEqual(self.client.get("/dev/explorer/api/address/bad!/txs").status_code, 400)
        address = "tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx"
        self.assertEqual(self.client.post("/dev/explorer/", {"address": address, "sats": 9000, "action": "mempool"}).status_code, 302)
        txs = self.client.get(f"/dev/explorer/api/address/{address}/txs").json()
        self.assertEqual((txs[0]["vout"][0], txs[0]["status"]),
                         ({"scriptpubkey_type": "v0_p2wpkh", "scriptpubkey_address": address, "value": 9000}, {"confirmed": False}))
        self.client.post("/dev/explorer/", {"address": address, "action": "confirm"})
        self.assertTrue(self.client.get(f"/dev/explorer/api/address/{address}/txs").json()[0]["status"]["confirmed"])
        page = self.client.get(f"/dev/explorer/?a={address}")
        self.assertContains(page, "9000 sat")
        self.assertEqual(page["X-Robots-Tag"], "noindex, nofollow")
        self.assertTrue(explorer.enabled())

    @override_settings(FAKE_EXPLORER=True)
    def test_pretend_payments_are_limited(self):
        """No login on the page: a visitor makes so many pretend payments an hour, and only so many addresses are kept, the
        oldest forgotten, so the cache keeps room for the site's own counters."""
        with patch.object(explorer, "MAX_ADDRESSES", 3), patch.object(explorer, "PER_VISITOR", 5):
            addresses = [f"tb1qpretend{n:08d}" for n in range(5)]
            for address in addresses:
                self.assertEqual(self.client.post("/dev/explorer/", {"address": address, "sats": 1000,
                                                                     "action": "mempool"}).status_code, 302)
            kept = [address for address in addresses if self.client.get(f"/dev/explorer/api/address/{address}/txs").json()]
            self.assertEqual(kept, addresses[2:])
            refused = self.client.post("/dev/explorer/", {"address": addresses[0], "sats": 1000, "action": "mempool"})
            self.assertEqual(refused.status_code, 429)
            self.assertIn("Retry-After", refused)
            # Another visitor still can
            self.assertEqual(self.client.post("/dev/explorer/", {"address": addresses[0], "sats": 1000, "action": "mempool"},
                                              REMOTE_ADDR="192.0.2.7").status_code, 302)

"""The shop's own explorer: an https address of an Esplora or mempool API (the owner's node) that the owner's and the staff
registers ask before the community's. The browser asks it; the site only keeps the address, for the register's CSP."""
import json

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from .models import Shop, StaffRegister
from .register import connect_origins, own_explorer_url, register_data
from .test_experimental import OnChain
from .test_register import register_data as page_data


class Validation(SimpleTestCase):
    def test_a_well_written_address_is_normalised(self):
        self.assertEqual(own_explorer_url(" https://Node.Example.org:4081/api/ "), "https://node.example.org:4081/api")
        self.assertEqual(own_explorer_url("https://node.example.org"), "https://node.example.org")

    def test_what_a_browser_could_not_or_should_not_ask_is_refused(self):
        for wrong in ("http://node.example.org/api", "node.example.org/api", "https://user:pass@node.example.org/api",
                      "https://user@node.example.org/api", "https://node.example.org/api?x=1", "https://node.example.org/api#a",
                      "https://192.168.1.5/api", "https://localhost/api", "https://node.example.org:0/api",
                      "https://node.example.org:99999/api", "https://node.example.org/a b", "https://[::1/api", "",
                      "https://node.example.org/" + "a" * 200, None, 5, "ftp://node.example.org", "wss://node.example.org"):
            self.assertIsNone(own_explorer_url(wrong), wrong)


class Kept(OnChain, TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user("owner")
        self.shop = Shop.objects.create(name="Bar", owner=self.owner, lightning_address="bar@wallet.example")
        self.url = reverse("register_explorer", args=[self.shop.pk])

    def save(self, value):
        return self.client.post(self.url, json.dumps({"explorer": value}), content_type="application/json")

    def test_only_the_owner_saves_it(self):
        self.assertEqual(self.save("https://node.example.org/api").status_code, 403)
        self.client.force_login(get_user_model().objects.create_user("other"))
        self.assertEqual(self.save("https://node.example.org/api").status_code, 403)
        self.shop.refresh_from_db()
        self.assertEqual(self.shop.own_explorer, "")

    def test_the_owner_saves_and_clears_it(self):
        self.client.force_login(self.owner)
        answer = self.save(" https://Node.Example.org/api/ ")
        self.assertEqual((answer.status_code, answer.json()), (200, {"explorer": "https://node.example.org/api"}))
        self.shop.refresh_from_db()
        self.assertEqual(self.shop.own_explorer, "https://node.example.org/api")
        self.assertEqual(self.save("").json(), {"explorer": ""})
        self.shop.refresh_from_db()
        self.assertEqual(self.shop.own_explorer, "")

    def test_a_wrong_one_is_refused_and_the_old_stays(self):
        self.client.force_login(self.owner)
        self.save("https://node.example.org/api")
        for wrong in ("http://node.example.org/api", "https://u:p@node.example.org/api", "https://node.example.org/" + "a" * 300):
            self.assertEqual(self.save(wrong).status_code, 400, wrong)
        for body in ("nonsense", json.dumps({"explorer": 5}), json.dumps({})):
            self.assertEqual(self.client.post(self.url, body, content_type="application/json").status_code, 400)
        self.shop.refresh_from_db()
        self.assertEqual(self.shop.own_explorer, "https://node.example.org/api")

    def test_the_site_never_fetches_it(self):
        from unittest.mock import patch

        self.client.force_login(self.owner)
        with patch("urllib.request.urlopen", side_effect=AssertionError("fetched")), \
                patch("socket.create_connection", side_effect=AssertionError("fetched")):
            self.assertEqual(self.save("https://node.example.org/api").status_code, 200)

    def test_the_register_carries_it_and_opens_the_csp_to_its_origin_with_the_port(self):
        self.assertNotIn("own_explorer", register_data(self.shop))
        self.shop.own_explorer = "https://node.example.org:4081/api"
        self.shop.save()
        data = register_data(self.shop)
        self.assertEqual(data["own_explorer"], "https://node.example.org:4081/api")
        self.assertIn("https://node.example.org:4081", connect_origins(data))
        self.assertNotIn("https://node.example.org/api", connect_origins(data))
        # The community's explorers stay (the fallback)
        self.assertIn("https://mempool.space", connect_origins(data))

    def test_a_value_that_would_not_pass_today_is_not_carried(self):
        self.shop.own_explorer = "http://node.example.org/api"
        self.shop.save()
        self.assertNotIn("own_explorer", register_data(self.shop))

    def test_the_owner_page_and_the_staff_register_get_it(self):
        self.shop.own_explorer = "https://node.example.org/api"
        self.shop.save()
        self.client.force_login(self.owner)
        page = self.client.get(reverse("register", args=[self.shop.pk]))
        data = page_data(page.content.decode())
        self.assertEqual(data["own_explorer"], "https://node.example.org/api")
        self.assertEqual(data["explorer_save"], self.url)
        self.assertIn("https://node.example.org", page.headers["Content-Security-Policy"])
        self.client.logout()
        staff = StaffRegister.objects.create(shop=self.shop, name="Anna")
        staff_page = self.client.get(f"/en/shops/register/p/{staff.id}/?k={staff.key}")
        self.assertEqual(staff_page.status_code, 200)
        staff_data = page_data(staff_page.content.decode())
        self.assertEqual(staff_data["own_explorer"], "https://node.example.org/api")
        # …but cannot change it
        self.assertNotIn("explorer_save", staff_data)

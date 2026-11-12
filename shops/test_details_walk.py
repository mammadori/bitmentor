"""The walk (shops/details.py): every route of the site, as a stranger and as the owner, with a shop whose every detail is
hidden and written as a word nobody else uses. Whatever a page, a JSON or a file says, the words must not be in it, except on
the owner's own pages, which are the ones that show them. A page added tomorrow is walked without anyone remembering it."""
import re
from collections import defaultdict
from datetime import timedelta
from unittest.mock import patch

import requests
from django.test import TestCase
from django.urls import get_resolver
from django.utils import timezone, translation

from .models import Shop, StaffRegister
from .test_details import an_owner
from .test_guard import routes
from .tests import reset_limits

SENTINELS = {"address": "SENTADDR91", "phone": "SENTPHONE92", "email": "sentemail93@example.org", "legal_name": "SENTLEGAL94",
             "vat_number": "ITSENTVAT95", "website": "https://sentweb96.example.org"}
# The pages of the owner that show the owner's own details, hidden or not, to the owner only
OWNER_PAGES = {"en/shops/edit/<uuid:shop>/", "en/shops/preview/<uuid:shop>/"}


def sentinel_shop(owner, **fields):
    values = dict(name="Probe", city="Example", latitude=41.9, longitude=12.5, lightning=True,
                  lightning_address="probe@wallet.example.org", description="A shop",
                  show_website=False, show_address=False, show_phone=False, show_email=False, show_legal_name=False,
                  show_vat_number=False, **SENTINELS)
    values.update(fields)
    return Shop.objects.create(owner=owner, **values)


class Walk(TestCase):
    def setUp(self):
        reset_limits()
        # Nothing leaves the test: the wallets, the providers and the price sources do not answer
        patcher = patch("requests.sessions.Session.request", side_effect=requests.ConnectionError)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.owner = an_owner("walker")
        self.shops = {
            "plain": sentinel_shop(self.owner, name="Plain"),
            "suspended": sentinel_shop(None, name="Susp", suspended=True, external_id="7"),
            "historic": sentinel_shop(None, name="Hist", closed_on=timezone.localdate() - timedelta(days=30), external_id="8"),
            "waiting": sentinel_shop(self.owner, name="Wait", awaiting_wallet=True, lightning_address=""),
            "unlisted": sentinel_shop(None, name="Unl", listed=False, external_id="9"),
        }
        self.shop = self.shops["plain"]
        self.register = StaffRegister.objects.create(shop=self.shop, name="Anna", code="c" * 32,
                                                     code_until=timezone.now() + timedelta(hours=1))

    def url(self, route, shop):
        values = {"shop": str(shop.id), "register": str(self.register.id), "invoice": "inv1", "external": shop.external_id or "7",
                  "pk": "00000000-0000-0000-0000-000000000001", "slug": "home", "path": "x", "year": "2026", "month": "10",
                  "number": "1", "key": "k" * 32, "code": "c" * 32, "name": "x", "token": "t"}
        try:
            return "/" + re.sub(r"<(?:\w+:)?(\w+)>", lambda found: values[found.group(1)], route)
        except KeyError:
            return None

    def walk(self, who):
        """{(route, shop): [names of the sentinels found]} for every page that answered with text, JSON or an SVG."""
        with translation.override("en"):
            found = sorted(set(routes(get_resolver().url_patterns)))
        client = self.client_class(raise_request_exception=False)
        if who == "owner":
            client.force_login(self.owner)
        hits, answered = defaultdict(list), 0
        for route in found:
            for key, shop in self.shops.items():
                if "<uuid:shop>" not in route and key != "plain":
                    continue
                url = self.url(route, shop)
                if url is None:
                    continue
                response = client.get(url)
                kind = response.get("Content-Type", "")
                if response.status_code >= 500 or not any(word in kind for word in ("text", "json", "svg")):
                    continue
                answered += 1
                body = response.content.decode(errors="replace").lower()
                for name, value in SENTINELS.items():
                    if value.lower() in body:
                        hits[(route, key)].append(name)
        return hits, answered

    def test_a_stranger_finds_none_of_the_hidden_details_on_any_route(self):
        hits, answered = self.walk("stranger")
        self.assertGreater(answered, 100)
        self.assertEqual(dict(hits), {})

    def test_the_owner_finds_them_only_on_the_pages_that_are_theirs(self):
        hits, answered = self.walk("owner")
        self.assertGreater(answered, 100)
        elsewhere = {where: names for where, names in hits.items() if where[0] not in OWNER_PAGES}
        self.assertEqual(elsewhere, {})
        # Not an empty guard: the owner's two pages do show them
        self.assertEqual({route for route, _shop in hits}, OWNER_PAGES)

    def test_the_walk_notices_a_leak(self):
        """If every choice were on, the stranger's walk would find the details: the guard is not blind."""
        with patch("shops.details.chosen", return_value=True):
            hits, _answered = self.walk("stranger")
        names = {name for found in hits.values() for name in found}
        self.assertTrue({"address", "website", "phone", "email", "legal_name", "vat_number"} <= names, names)

    def test_the_walk_sees_the_pages_that_could_leak(self):
        with translation.override("en"):
            found = set(routes(get_resolver().url_patterns))
        for route in ("en/shops/s/<uuid:shop>/", "en/shops/map/", "en/shops/sign/<uuid:shop>/", "en/shops/card/<uuid:shop>/",
                      "en/shops/customer-card/<uuid:shop>/", "en/shops/preview/<uuid:shop>/", "shops.json"):
            self.assertIn(route, found)

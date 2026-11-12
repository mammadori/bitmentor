"""Certified shops and how recently their Lightning address worked (shops/certified.py): the rule, the bands, what
leaves the site, the admin."""
import json
from datetime import date, datetime, timedelta
from datetime import timezone as dt_timezone
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone


from . import certified, lightning_watch, training
from .certified import MONTH, OLDER, WEEK, YEAR, bands, owns_certified_shop
from .models import LightningStatus, Shop, Trainee
from .nostr import shop_event
from .tests import admin_session

UTC = dt_timezone.utc
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
SECRET = "1" * 64


def when(*args):
    return datetime(*args, tzinfo=UTC)


class Base(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user("merchant")
        self.shop = Shop.objects.create(name="Bar", owner=self.owner, latitude=41.9, longitude=12.5, lightning=True)


class Bands(Base):
    def make(self, worked=None, address="a@b.example"):
        return Shop(name="x", lightning_address=address, lightning_worked=worked)

    def test_the_four_bands(self):
        for days, band in ((0, WEEK), (7, WEEK), (8, MONTH), (31, MONTH), (32, YEAR), (366, YEAR), (367, OLDER)):
            with self.subTest(days=days):
                self.assertEqual(bands(self.make(NOW - timedelta(days=days)), NOW), {"lightning": band})

    def test_an_address_that_never_worked_has_no_icon(self):
        # Never worked: no icon, rather than one saying "not active"
        self.assertIsNone(bands(self.make(), NOW))

    def test_whole_days_in_the_community_calendar(self):
        # 20 November 12:00 UTC: 13 November from 00:00 in Rome is a week ago, whatever the hour
        self.assertEqual(bands(self.make(when(2026, 11, 12, 23, 0)), NOW)["lightning"], WEEK)
        self.assertEqual(bands(self.make(when(2026, 11, 12, 22, 59)), NOW)["lightning"], MONTH)
        self.assertEqual(bands(self.make(when(2026, 11, 12, 23, 0)), when(2026, 11, 20, 22, 59))["lightning"], WEEK)

    def test_only_lightning(self):
        self.assertEqual(set(bands(self.make(NOW), NOW)), {"lightning"})


class Rule(Base):
    """A certified shop: the current Lightning address works, the owner has the basic pass, and it was not revoked."""

    def setUp(self):
        super().setUp()
        Shop.objects.filter(pk=self.shop.pk).update(lightning_address="bar@b.example", lightning_status=LightningStatus.OK,
                                                     lightning_checked_address="bar@b.example", lightning_worked=NOW)

    def pass_(self):
        Trainee.objects.create(user=self.owner, base_earned=NOW)

    def month(self):
        return Shop.objects.get(pk=self.shop.pk).certified_month

    def test_all_three_certify_with_the_month_of_the_refresh(self):
        self.pass_()
        self.assertTrue(certified.meets_rule(self.shop))
        self.assertEqual(certified.refresh(now=NOW), 1)
        self.assertEqual(self.month(), date(2026, 11, 1))
        # Once certified, a later refresh keeps the first month
        self.assertEqual(certified.refresh(now=NOW + timedelta(days=40)), 0)
        self.assertEqual(self.month(), date(2026, 11, 1))

    def test_without_the_basic_pass_no_certificate(self):
        Trainee.objects.create(user=self.owner)
        self.assertFalse(certified.meets_rule(self.shop))
        self.assertEqual(certified.refresh(now=NOW), 0)
        self.assertIsNone(self.month())

    def test_without_a_working_address_no_certificate(self):
        self.pass_()
        for fields in ({"lightning_status": LightningStatus.NO_ANSWER}, {"lightning_status": ""},
                       {"lightning_checked_address": "old@b.example"}, {"lightning_address": "", "lightning_checked_address": ""}):
            with self.subTest(fields=fields):
                Shop.objects.filter(pk=self.shop.pk).update(**fields)
                self.assertEqual(certified.refresh(now=NOW), 0)
                self.assertIsNone(self.month())
                Shop.objects.filter(pk=self.shop.pk).update(
                    lightning_address="bar@b.example", lightning_status=LightningStatus.OK,
                    lightning_checked_address="bar@b.example")

    def test_revoked_is_never_certified_again(self):
        self.pass_()
        Shop.objects.filter(pk=self.shop.pk).update(certified_revoked=True)
        self.assertEqual(certified.refresh(now=NOW), 0)
        self.assertIsNone(self.month())

    def test_never_taken_away_by_the_site(self):
        self.pass_()
        certified.refresh(now=NOW)
        Shop.objects.filter(pk=self.shop.pk).update(lightning_status=LightningStatus.NO_ANSWER)
        Trainee.objects.filter(user=self.owner).update(base_earned=None)
        certified.refresh(now=NOW + timedelta(days=60))
        self.assertEqual(self.month(), date(2026, 11, 1))

    def test_a_good_probe_certifies(self):
        self.pass_()
        Shop.objects.filter(pk=self.shop.pk).update(lightning_status="", lightning_checked_address="")
        with patch("shops.lightning_watch.watch_probe", return_value=None):
            lightning_watch.check(self.shop.pk)
        self.assertIsNotNone(self.month())

    def test_a_failed_probe_does_not(self):
        self.pass_()
        Shop.objects.filter(pk=self.shop.pk).update(lightning_status="", lightning_checked_address="")
        with patch("shops.lightning_watch.watch_probe", return_value=LightningStatus.NO_ANSWER):
            lightning_watch.check(self.shop.pk)
        self.assertIsNone(self.month())

    def test_the_basic_pass_certifies(self):
        Trainee.objects.create(user=self.owner)
        with patch("shops.training.parse", return_value=object()), \
                patch("shops.training.all_modules", return_value=[]):
            self.assertTrue(training.settle(self.owner))
        self.assertIsNotNone(self.month())

    def test_check_lightning_certifies_what_is_ready(self):
        self.pass_()
        call_command("check_lightning", stdout=StringIO(), stderr=StringIO())
        self.assertIsNotNone(self.month())


class Outside(Base):
    """Only the flag and the bands leave the site: no date, count or amount."""

    def setUp(self):
        super().setUp()
        Shop.objects.filter(pk=self.shop.pk).update(
            certified_month=date(2026, 9, 1), lightning_address="bar@b.example",
            lightning_worked=timezone.now() - timedelta(days=200, hours=2))
        self.shop.refresh_from_db()
        Shop.objects.create(name="Plain", latitude=41.8, longitude=12.4)
        self.secrets = ["2026-09", "2026-9", self.shop.lightning_worked.date().isoformat(),
                        str(int(self.shop.lightning_worked.timestamp()))]

    def assertNothingSecret(self, text):
        for secret in self.secrets:
            self.assertNotIn(secret, text)
        for word in ("certified_month", "lightning_worked"):
            self.assertNotIn(word, text)

    def test_shops_json(self):
        data = self.client.get("/shops.json").json()["shops"]
        mine = next(shop for shop in data if shop["name"] == "Bar")
        plain = next(shop for shop in data if shop["name"] == "Plain")
        self.assertIs(mine["certified"], True)
        self.assertEqual(mine["paid"], {"lightning": YEAR})
        self.assertIs(plain["certified"], False)
        self.assertIsNone(plain["paid"])
        self.assertNothingSecret(json.dumps(data))

    def test_historic_shop_has_no_icons(self):
        Shop.objects.filter(pk=self.shop.pk).update(closed_on=date(2026, 1, 1))
        mine = next(shop for shop in self.client.get("/shops.json").json()["shops"] if shop["name"] == "Bar")
        self.assertIsNone(mine["paid"])
        self.assertIs(mine["certified"], True)

    def test_map_and_shop_page(self):
        page = self.client.get("/en/shops/map/").content.decode()
        self.assertIn("Certified shop", page)
        self.assertIn('aria-label="Lightning, active in the last year"', page)
        self.assertNotIn('aria-label="On-chain', page)
        self.assertNothingSecret(page)
        page = self.client.get(f"/en/shops/s/{self.shop.pk}/").content.decode()
        self.assertIn("Certified shop", page)
        self.assertNothingSecret(page)
        Shop.objects.filter(pk=self.shop.pk).update(certified_month=None)
        self.assertNotIn("Certified shop", self.client.get(f"/en/shops/s/{self.shop.pk}/").content.decode())

    def test_translated(self):
        page = self.client.get(f"/it/shops/s/{self.shop.pk}/").content.decode()
        self.assertIn("Negozio certificato", page)

    def test_suspended_shop_page_has_no_badge(self):
        Shop.objects.filter(pk=self.shop.pk).update(suspended=True)
        self.assertNotIn("Certified shop", self.client.get(f"/en/shops/s/{self.shop.pk}/").content.decode())

    def test_nostr(self):
        event = shop_event(SECRET, self.shop, "https://example.org/x")
        self.assertNothingSecret(json.dumps(event))


class Admin(Base):
    def test_read_only_with_a_filter(self):
        Shop.objects.filter(pk=self.shop.pk).update(certified_month=date(2026, 9, 1))
        admin_session(self.client)
        page = self.client.get(f"/admin/shops/shop/{self.shop.pk}/change/").content.decode()
        self.assertIn("Certified since the month of", page)
        for name in ("certified_month", "certified_revoked"):
            self.assertNotIn(f'name="{name}"', page)
        link = f"/admin/shops/shop/{self.shop.pk}/change/"
        self.assertIn(link, self.client.get("/admin/shops/shop/?certified=yes").content.decode())
        self.assertNotIn(link, self.client.get("/admin/shops/shop/?certified=no").content.decode())


class Owner(Base):
    def test_owns_certified_shop(self):
        self.assertFalse(owns_certified_shop(self.owner))
        Shop.objects.filter(pk=self.shop.pk).update(certified_month=date(2026, 9, 1))
        self.assertTrue(owns_certified_shop(self.owner))
        self.assertFalse(owns_certified_shop(AnonymousUser()))
        self.assertFalse(owns_certified_shop(None))
        self.assertFalse(owns_certified_shop(get_user_model().objects.create_user("other")))
        Shop.objects.filter(pk=self.shop.pk).update(suspended=True)
        self.assertFalse(owns_certified_shop(self.owner))


class Revoke(Base):
    def test_revoked_and_allowed_again_from_the_admin(self):
        Shop.objects.filter(pk=self.shop.pk).update(certified_month=date(2026, 9, 1), lightning_address="bar@b.example",
                                                     lightning_status=LightningStatus.OK,
                                                     lightning_checked_address="bar@b.example")
        admin_session(self.client)
        url = "/admin/shops/shop/"
        self.client.post(url, {"action": "revoke_certificate", "_selected_action": [str(self.shop.pk)]})
        shop = Shop.objects.get(pk=self.shop.pk)
        self.assertIsNone(shop.certified_month)
        self.assertTrue(shop.certified_revoked)
        self.assertFalse(owns_certified_shop(self.owner))
        # Revoked: the site does not certify it again by itself
        Trainee.objects.create(user=self.owner, base_earned=NOW)
        certified.refresh()
        self.assertIsNone(Shop.objects.get(pk=self.shop.pk).certified_month)
        # Allowed again: certified at once, since it meets the rule
        self.client.post(url, {"action": "allow_certificate", "_selected_action": [str(self.shop.pk)]})
        shop = Shop.objects.get(pk=self.shop.pk)
        self.assertFalse(shop.certified_revoked)
        self.assertIsNotNone(shop.certified_month)


    def test_historic_certified_shop_keeps_the_badge_in_the_list(self):
        Shop.objects.filter(pk=self.shop.pk).update(certified_month=date(2026, 9, 1), closed_on=date(2026, 1, 1),
                                                     lightning_worked=timezone.now())
        page = self.client.get("/en/shops/map/").content.decode()
        self.assertIn("Certified shop", page)
        self.assertNotIn("Lightning, active", page.split('id="paid-texts"')[0])


class RuleText(Base):
    """What the badge means, on the page of a certified shop and on the map: no numbers, no dates."""

    def test_on_the_page_of_a_certified_shop_and_the_map(self):
        Shop.objects.filter(pk=self.shop.pk).update(certified_month=date(2026, 10, 1), listed=True)
        url = f"/en/shops/s/{self.shop.pk}/"
        meaning = "Certified shop: its Lightning address works, and its owner has done the community&#x27;s training."
        page = self.client.get(url).content.decode()
        self.assertIn(f'<p class="muted certified-rule">{meaning}</p>', page)
        self.assertEqual(certified.texts()["rule"], meaning.replace("&#x27;", "'"))
        Shop.objects.filter(pk=self.shop.pk).update(certified_month=None)
        self.assertNotIn("certified-rule", self.client.get(url).content.decode())

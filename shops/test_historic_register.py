"""A historic shop keeps its register, and may still connect a wallet, until a year after its closing day."""
from datetime import date, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from . import address_change, dashboard, lightning_watch
from .models import Shop
from .views import needs_wallet


class Historic(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user("owner")
        self.client.force_login(self.owner)

    def shop(self, closed_on, address=""):
        return Shop.objects.create(name="Bar", owner=self.owner, closed_on=closed_on, lightning_address=address,
                                   lightning=bool(address))

    def test_closing_soon_without_wallet_can_add_it(self):
        shop = self.shop(date(2026, 11, 27))
        self.assertTrue(needs_wallet(shop))
        page = self.client.get(reverse("edit_shop", args=[shop.pk])).content.decode()
        self.assertIn("Add the wallet", page)
        self.assertNotIn("Lightning: </p>", page)
        with patch("shops.views.refusal", return_value=None):
            self.client.post(reverse("wallet_steps", args=[shop.pk]), {"lightning_address": "bar@wallet.example"})
        self.assertEqual(Shop.objects.get(pk=shop.pk).lightning_address, "bar@wallet.example")

    def test_closed_months_ago_still_can(self):
        shop = self.shop(timezone.localdate() - timedelta(days=200))
        self.assertTrue(needs_wallet(shop))

    def test_closed_over_a_year_ago_cannot(self):
        shop = self.shop(timezone.localdate() - timedelta(days=400))
        self.assertFalse(needs_wallet(shop))
        page = self.client.get(reverse("edit_shop", args=[shop.pk])).content.decode()
        self.assertNotIn("Add the wallet", page)
        self.assertIn("Historic shop: no register.", page)
        self.assertNotIn("Lightning: </p>", page)

    def test_watched_and_changeable_for_a_year(self):
        recent = self.shop(timezone.localdate() - timedelta(days=200), "bar@wallet.example")
        old = Shop.objects.create(name="Old", owner=self.owner, closed_on=timezone.localdate() - timedelta(days=400),
                                  lightning_address="old@wallet.example", lightning=True)
        watched = set(lightning_watch.watched().values_list("pk", flat=True))
        self.assertIn(recent.pk, watched)
        self.assertNotIn(old.pk, watched)
        trading = set(dashboard.trading(timezone.now()).values_list("pk", flat=True))
        self.assertIn(recent.pk, trading)
        self.assertNotIn(old.pk, trading)
        self.assertTrue(address_change.changeable(recent))
        self.assertFalse(address_change.changeable(old))

    def test_register_ends_a_year_after_closing(self):
        recent = self.shop(timezone.localdate() - timedelta(days=200), "bar@wallet.example")
        old = Shop.objects.create(name="Old", owner=self.owner, closed_on=timezone.localdate() - timedelta(days=400),
                                  lightning_address="old@wallet.example", lightning=True)
        with patch("shops.training.locked", return_value=False):
            self.assertEqual(self.client.get(reverse("register", args=[old.pk])).status_code, 404)
            self.assertEqual(self.client.get(reverse("register", args=[recent.pk])).status_code, 200)
            gone = self.client.get(reverse("register_worker", args=[old.pk])).content.decode()
            kept = self.client.get(reverse("register_worker", args=[recent.pk])).content.decode()
        self.assertNotEqual(gone, kept)
        from django.conf import settings
        self.assertEqual(gone, (settings.BASE_DIR / "static" / "register-sw-gone.js").read_text())

    def test_suspended_keeps_its_register(self):
        shop = Shop.objects.create(name="Sus", owner=self.owner, suspended=True, lightning_address="s@wallet.example",
                                   lightning=True)
        with patch("shops.training.locked", return_value=False):
            self.assertEqual(self.client.get(reverse("register", args=[shop.pk])).status_code, 200)

    def test_panel_says_until_when(self):
        Shop.objects.create(name="Bar", owner=self.owner, closed_on=timezone.localdate() - timedelta(days=200),
                            lightning_address="bar@wallet.example", lightning=True, latitude=45, longitude=11)
        old = Shop.objects.create(name="Old", owner=self.owner, closed_on=timezone.localdate() - timedelta(days=400),
                                  lightning_address="old@wallet.example", lightning=True, latitude=45, longitude=11)
        page = self.client.get(reverse("shops")).content.decode()
        self.assertIn("Register and payments keep working until", page)
        self.assertIn("The register stopped on", page)
        self.assertNotIn(f'href="{reverse("register", args=[old.pk])}"', page)

    def test_suspended_historic_without_address_says_suspended(self):
        shop = Shop.objects.create(name="Sus", owner=self.owner, suspended=True,
                                   closed_on=timezone.localdate() - timedelta(days=400))
        page = self.client.get(reverse("edit_shop", args=[shop.pk])).content.decode()
        self.assertIn("Suspended: no register can be added.", page)
        self.assertNotIn("Historic shop: no register.", page)

    def test_moved_historic_shop_within_its_year_waits_for_the_wallet(self):
        from . import moving
        shop = Shop.objects.create(name="Moved", owner=self.owner, moved_in=timezone.now(), moved_register=True,
                                   closed_on=timezone.localdate() - timedelta(days=30))
        self.assertEqual(moving.waiting_for(shop), "no_wallet")
        moved = [item.code for item in dashboard.moved_items(timezone.now()) if item.shop.pk == shop.pk]
        self.assertEqual(moved, ["moved_no_wallet"])
        Shop.objects.filter(pk=shop.pk).update(closed_on=timezone.localdate() - timedelta(days=400))
        self.assertIsNone(moving.waiting_for(Shop.objects.get(pk=shop.pk)))
        self.assertFalse([item for item in dashboard.moved_items(timezone.now()) if item.shop.pk == shop.pk])

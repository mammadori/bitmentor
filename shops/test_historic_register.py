"""A historic shop keeps its register, may connect a wallet and change its address, for as long as its owner wants: the
closing is about the map and the history, not the payments. It is no longer a place to pay, and the guardian of the
Lightning addresses leaves it alone."""
from datetime import date, timedelta
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from . import address_change, dashboard, lightning_watch
from .models import REGISTER_AFTER_CLOSING, Shop, keeps_register, register_kept
from .views import needs_wallet


class Historic(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user("owner")
        self.client.force_login(self.owner)

    def shop(self, closed_on, address="", name="Bar"):
        return Shop.objects.create(name=name, owner=self.owner, closed_on=closed_on, lightning_address=address,
                                   lightning=bool(address))

    def test_closing_soon_without_wallet_can_add_it(self):
        shop = self.shop(timezone.localdate() + timedelta(days=9))
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

    def test_closed_years_ago_still_can(self):
        shop = self.shop(timezone.localdate() - timedelta(days=4000))
        self.assertTrue(needs_wallet(shop))
        page = self.client.get(reverse("edit_shop", args=[shop.pk])).content.decode()
        self.assertIn("Add the wallet", page)
        self.assertNotIn("Historic shop: no register.", page)
        with patch("shops.views.refusal", return_value=None):
            self.client.post(reverse("wallet_steps", args=[shop.pk]), {"lightning_address": "bar@wallet.example"})
        self.assertEqual(Shop.objects.get(pk=shop.pk).lightning_address, "bar@wallet.example")

    def test_changeable_however_old_and_never_watched(self):
        recent = self.shop(timezone.localdate() - timedelta(days=1), "bar@wallet.example")
        old = self.shop(timezone.localdate() - timedelta(days=4000), "old@wallet.example", name="Old")
        closing = self.shop(timezone.localdate(), "closing@wallet.example", name="Closing")
        watched = set(lightning_watch.watched().values_list("pk", flat=True))
        trading = set(dashboard.trading(timezone.now()).values_list("pk", flat=True))
        # Up to and including its closing day a shop is an ordinary one: watched and looked after
        self.assertEqual(watched, {closing.pk})
        self.assertEqual(trading, {closing.pk})
        self.assertFalse(lightning_watch.is_watched(recent))
        self.assertTrue(lightning_watch.is_watched(closing))
        for shop in (recent, old, closing):
            self.assertTrue(address_change.changeable(shop))
        self.assertTrue(address_change.apply(old.pk, self.owner, "old@wallet.example", "new@wallet.example"))
        self.assertEqual(Shop.objects.get(pk=old.pk).lightning_address, "new@wallet.example")

    def test_the_register_has_no_end(self):
        recent = self.shop(timezone.localdate() - timedelta(days=200), "bar@wallet.example")
        old = self.shop(timezone.localdate() - timedelta(days=4000), "old@wallet.example", name="Old")
        gone = (settings.BASE_DIR / "static" / "register-sw-gone.js").read_text()
        with patch("shops.training.locked", return_value=False):
            for shop in (recent, old):
                self.assertEqual(self.client.get(reverse("register", args=[shop.pk])).status_code, 200)
                self.assertNotEqual(self.client.get(reverse("register_worker", args=[shop.pk])).content.decode(), gone)

    def test_the_old_names_stay(self):
        """Community code may import them: the limit they described is gone."""
        old = self.shop(timezone.localdate() - timedelta(days=4000))
        self.assertEqual(REGISTER_AFTER_CLOSING, timedelta(days=365))
        self.assertTrue(keeps_register(old))
        self.assertTrue(keeps_register(old, date(2099, 1, 1)))
        self.assertIn(old, Shop.objects.filter(register_kept()))
        self.assertEqual(Shop.objects.filter(register_kept(date(2099, 1, 1))).count(), Shop.objects.count())

    def test_suspended_keeps_its_register(self):
        shop = Shop.objects.create(name="Sus", owner=self.owner, suspended=True, lightning_address="s@wallet.example",
                                   lightning=True)
        with patch("shops.training.locked", return_value=False):
            self.assertEqual(self.client.get(reverse("register", args=[shop.pk])).status_code, 200)

    def test_panel_says_the_register_stays(self):
        Shop.objects.create(name="Bar", owner=self.owner, closed_on=timezone.localdate() - timedelta(days=200),
                            lightning_address="bar@wallet.example", lightning=True, latitude=45, longitude=11)
        old = Shop.objects.create(name="Old", owner=self.owner, closed_on=timezone.localdate() - timedelta(days=4000),
                                  lightning_address="old@wallet.example", lightning=True, latitude=45, longitude=11)
        page = self.client.get(reverse("shops")).content.decode()
        self.assertEqual(page.count("The register stays yours and keeps working, for as long as you want."), 2)
        self.assertNotIn("keep working until", page)
        self.assertNotIn("The register stopped on", page)
        self.assertIn(f'href="{reverse("register", args=[old.pk])}"', page)

    def test_suspended_historic_without_address_says_suspended(self):
        shop = Shop.objects.create(name="Sus", owner=self.owner, suspended=True,
                                   closed_on=timezone.localdate() - timedelta(days=400))
        page = self.client.get(reverse("edit_shop", args=[shop.pk])).content.decode()
        self.assertIn("Suspended: no register can be added.", page)
        self.assertNotIn("Historic shop: no register.", page)

    def test_a_moved_shop_waits_for_the_wallet_until_its_closing_day(self):
        """The association's list of moved shops is about places to pay: a historic one waits for nothing there, and its
        owner may still connect a wallet."""
        from . import moving
        shop = Shop.objects.create(name="Moved", owner=self.owner, moved_in=timezone.now(), moved_register=True,
                                   closed_on=timezone.localdate())
        self.assertEqual(moving.waiting_for(shop), "no_wallet")
        moved = [item.code for item in dashboard.moved_items(timezone.now()) if item.shop.pk == shop.pk]
        self.assertEqual(moved, ["moved_no_wallet"])
        for closed_on in (timezone.localdate() - timedelta(days=1), timezone.localdate() - timedelta(days=4000)):
            Shop.objects.filter(pk=shop.pk).update(closed_on=closed_on)
            self.assertIsNone(moving.waiting_for(Shop.objects.get(pk=shop.pk)))
            self.assertFalse([item for item in dashboard.moved_items(timezone.now()) if item.shop.pk == shop.pk])
            self.assertTrue(needs_wallet(Shop.objects.get(pk=shop.pk)))

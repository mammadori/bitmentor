"""The printed sign waits for the approval (shops/views.py, shop_sign): its QR code opens the shop's page, which shows how
to pay only once the shop is approved. The register and the other printables never wait."""
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from config.community import community

from . import first_day
from .models import Shop


def approval_off():
    return patch("config.features.community", new=lambda: {**community(), "features": {"approval": False}})


class SignWaits(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create(username="owner")
        self.client.force_login(self.owner)
        self.shop = Shop.objects.create(name="Bar", owner=self.owner, latitude=41.9, longitude=12.5,
                                        lightning_address="mario@wallet.example", approved_on=None)
        self.sign = f"/en/shops/sign/{self.shop.id}/"

    def test_a_waiting_shop_gets_a_page_that_says_why_not_the_sign(self):
        response = self.client.get(self.sign)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "shops/sign_waiting.html")
        self.assertTemplateNotUsed(response, "shops/sign.html")
        self.assertContains(response, 'id="sign-waits"')
        self.assertContains(response, f"/en/shops/card/{self.shop.id}/")

    def test_the_panel_says_it_in_place_of_the_button(self):
        panel = self.client.get("/en/shops/").content.decode()
        self.assertIn(f'id="sign-waits-{self.shop.id}"', panel)
        self.assertIn("The sign and the card for customers come once the community has approved the shop", panel)
        self.assertNotIn(f'href="{self.sign}"', panel)
        self.assertNotIn(f"/en/shops/customer-card/{self.shop.id}/", panel)
        # The card for the till and the register do not wait
        self.assertIn(f"/en/shops/card/{self.shop.id}/", panel)
        self.assertIn(f"/en/shops/register/{self.shop.id}/", panel)
        self.assertEqual(self.client.get(f"/en/shops/card/{self.shop.id}/").status_code, 200)
        self.assertEqual(self.client.get(f"/en/shops/register/{self.shop.id}/").status_code, 200)

    def test_the_first_day_says_to_wait(self):
        step = next(step for step in first_day.steps(self.shop) if step["key"] == "sign")
        self.assertEqual((step["state"], step["why"]), (first_day.TODO, "approval"))
        panel = self.client.get("/en/shops/").content.decode()
        self.assertIn("Wait for the community to approve the shop", panel)
        self.assertIn("print the sign once the community has approved the shop", panel)

    def test_the_new_shop_notice_says_to_wait(self):
        panel = self.client.get("/en/shops/?saved").content.decode()
        self.assertIn("The sign to print comes once the community has approved the shop.", panel)
        self.assertNotIn("Then print the sign.", panel)

    def test_approved_as_today(self):
        Shop.objects.filter(pk=self.shop.pk).update(approved_on=timezone.now())
        response = self.client.get(self.sign)
        self.assertTemplateUsed(response, "shops/sign.html")
        panel = self.client.get("/en/shops/?saved").content.decode()
        self.assertIn(f'href="{self.sign}"', panel)
        self.assertNotIn("sign-waits-", panel)
        self.assertIn("Then print the sign.", panel)
        self.assertIn("Print the sign for the door or the counter", panel)

    def test_the_card_for_customers_waits_too(self):
        response = self.client.get(f"/en/shops/customer-card/{self.shop.id}/")
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "shops/sign_waiting.html")
        self.assertTemplateNotUsed(response, "shops/customer_card.html")
        self.assertContains(response, "The card for customers of Bar")
        Shop.objects.filter(pk=self.shop.pk).update(approved_on=timezone.now())
        self.assertTemplateUsed(self.client.get(f"/en/shops/customer-card/{self.shop.id}/"), "shops/customer_card.html")
        self.assertIn(f"/en/shops/customer-card/{self.shop.id}/", self.client.get("/en/shops/").content.decode())

    def test_with_the_approval_off_a_new_shop_never_waits(self):
        with approval_off(), patch("shops.views.refusal", return_value=None):
            self.client.post("/en/shops/new/", {"name": "Fresh", "latitude": "41.9", "longitude": "12.5",
                                                "lightning_address": "fresh@wallet.example"})
            fresh = Shop.objects.get(name="Fresh")
            self.assertFalse(fresh.needs_approval)
            self.assertTemplateUsed(self.client.get(f"/en/shops/sign/{fresh.id}/"), "shops/sign.html")
            self.assertTemplateUsed(self.client.get(f"/en/shops/customer-card/{fresh.id}/"), "shops/customer_card.html")
            step = next(step for step in first_day.steps(fresh) if step["key"] == "sign")
            self.assertNotIn("why", step)

    def test_a_shop_still_waiting_when_the_approval_was_turned_off_still_waits(self):
        # Not on the public lists (they read approved_on, whatever the switch): no sign that leads to a poor page
        with approval_off():
            self.assertTemplateUsed(self.client.get(self.sign), "shops/sign_waiting.html")
            self.assertIn(f'id="sign-waits-{self.shop.id}"', self.client.get("/en/shops/").content.decode())

    def test_a_suspended_or_closed_shop_as_today(self):
        for change in ({"suspended": True}, {"closed_on": timezone.localdate() - timedelta(days=3)}):
            with self.subTest(change):
                Shop.objects.filter(pk=self.shop.pk).update(**{"approved_on": timezone.now(), "suspended": False,
                                                               "closed_on": None, **change})
                self.assertTemplateUsed(self.client.get(self.sign), "shops/sign.html")
                self.assertIn(f'href="{self.sign}"', self.client.get("/en/shops/").content.decode())

    def test_a_printed_sign_stays_done(self):
        Shop.objects.filter(pk=self.shop.pk).update(sign_printed=timezone.now())
        self.shop.refresh_from_db()
        step = next(step for step in first_day.steps(self.shop) if step["key"] == "sign")
        self.assertEqual(step["state"], first_day.DONE)
        self.assertNotIn("why", step)

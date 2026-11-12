"""The owner changes a shop's Lightning address from the panel (shops/address_change.py)."""
import time
from datetime import date, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import address_change, certified
from .models import AccountChange, LightningStatus, Profile, Shop
from .register import connect_origins, register_data

OLD, NEW = "bar@old.example", "bar@new.example"


class Change(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user("owner")
        Profile.objects.create(user=self.owner, email="owner@mail.example")
        self.shop = Shop.objects.create(name="Bar", owner=self.owner, lightning_address=OLD, lightning=True,
                                        certified_month=date(2026, 9, 1), private_approved=timezone.now(),
                                        lightning_status=LightningStatus.OK, lightning_checked_address=OLD,
                                        lightning_worked=timezone.now())
        self.client.force_login(self.owner)
        self.url = reverse("change_address", args=[self.shop.pk])
        self.probe = patch("shops.address_change.refusal", return_value=None)
        self.probe.start()
        self.addCleanup(self.probe.stop)

    def post(self, address=NEW, client=None):
        with self.captureOnCommitCallbacks(execute=True):
            return (client or self.client).post(self.url, {"lightning_address": address})

    def test_changes_same_shop_and_tells(self):
        response = self.post()
        self.assertRedirects(response, reverse("shops"), fetch_redirect_response=False)
        shop = Shop.objects.get(pk=self.shop.pk)
        self.assertEqual(shop.lightning_address, NEW)
        self.assertEqual(shop.lightning_previous, OLD)
        self.assertEqual(Shop.objects.count(), 1)
        self.assertEqual(shop.lightning_checked_address, "")
        row = AccountChange.objects.get()
        self.assertEqual(row.via, AccountChange.Via.OWNER)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("You changed", mail.outbox[0].body)
        # The badge and the private part wait for the guardian; the certificate and the approval stay
        self.assertTrue(shop.address_pending)
        self.assertFalse(shop.certified)
        self.assertIsNotNone(shop.certified_month)
        self.assertIsNotNone(shop.private_approved)
        self.assertFalse(certified.owns_certified_shop(self.owner))
        Shop.objects.filter(pk=shop.pk).update(lightning_checked_address=NEW, lightning_worked=timezone.now(),
                                               lightning_status=LightningStatus.OK)
        self.assertTrue(Shop.objects.get(pk=shop.pk).certified)
        self.assertTrue(certified.owns_certified_shop(self.owner))
        page = self.client.get(reverse("shops")).content.decode()
        self.assertIn(f"address-changed-{shop.pk}", page)

    def test_old_provider_stays_open_for_a_while(self):
        self.post()
        shop = Shop.objects.get(pk=self.shop.pk)
        data = register_data(shop)
        self.assertEqual(data["previous_domain"], "old.example")
        self.assertIn("https://old.example", connect_origins(data))
        Shop.objects.filter(pk=shop.pk).update(lightning_changed=timezone.now() - timedelta(days=4))
        self.assertNotIn("previous_domain", register_data(Shop.objects.get(pk=shop.pk)))

    def test_one_change_every_few_days(self):
        self.post()
        self.post("bar@third.example")
        self.assertEqual(Shop.objects.get(pk=self.shop.pk).lightning_address, NEW)
        Shop.objects.filter(pk=self.shop.pk).update(lightning_changed=timezone.now() - timedelta(days=8))
        self.post("bar@third.example")
        self.assertEqual(Shop.objects.get(pk=self.shop.pk).lightning_address, "bar@third.example")

    def test_needs_recent_sign_in(self):
        session = self.client.session
        session[address_change.SESSION_KEY] = int(time.time()) - 3600
        session.save()
        response = self.post()
        self.assertContains(response, "Sign in again")
        self.assertEqual(Shop.objects.get(pk=self.shop.pk).lightning_address, OLD)

    def test_address_of_another_owner_refused(self):
        other = get_user_model().objects.create_user("other")
        Shop.objects.create(name="Other", owner=other, lightning_address=NEW)
        response = self.post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Shop.objects.get(pk=self.shop.pk).lightning_address, OLD)

    def test_live_check_refusal(self):
        with patch("shops.address_change.refusal", return_value="does not answer"):
            response = self.post()
        self.assertContains(response, "does not answer")
        self.assertEqual(Shop.objects.get(pk=self.shop.pk).lightning_address, OLD)

    def test_only_owner(self):
        other = get_user_model().objects.create_user("other")
        self.client.force_login(other)
        self.assertEqual(self.post().status_code, 404)

    def test_suspended_not_changed(self):
        Shop.objects.filter(pk=self.shop.pk).update(suspended=True)
        self.post()
        self.assertEqual(Shop.objects.get(pk=self.shop.pk).lightning_address, OLD)

    @override_settings(LIGHTNING_CHANGE_DAYS=0)
    def test_same_address_refused(self):
        response = self.post(OLD)
        self.assertContains(response, "already your shop")

    def test_waits_after_a_new_recovery_email(self):
        Profile.objects.filter(user=self.owner).update(email_changed=timezone.now() - timedelta(days=1))
        response = self.post()
        self.assertContains(response, "recovery email")
        self.assertEqual(Shop.objects.get(pk=self.shop.pk).lightning_address, OLD)

    def test_only_this_page_makes_a_replacement_the_owner_s(self):
        from django.test import RequestFactory
        from config.guard import current_request
        request = RequestFactory().post("/")
        request.user = self.owner
        token = current_request.set(request)
        try:
            with self.captureOnCommitCallbacks(execute=True):
                Shop.objects.filter(pk=self.shop.pk).update(lightning_address=NEW)
        finally:
            current_request.reset(token)
        self.assertEqual(AccountChange.objects.get().via, "site")

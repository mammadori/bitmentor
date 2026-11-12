"""Branches of the views that guard money, access and the site's email that the other tests did not walk: the account cap
at the wallet sign-in, a claim lost to another account, the email budget at the recovery address, the email budget
failing open, the owner's address change refused under its lock or past its tries, and the training's tries."""
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.db import DatabaseError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from config import mailbudget

from .models import LoginChallenge, Profile, Shop, claim_digest
from .test_limits import set_limits
from .test_mailbudget import COUNTING, ask
from .tests import reset_limits

CODE = "A" * 32


class WalletSignIn(TestCase):
    def test_a_new_account_past_the_cap_is_refused_with_503(self):
        reset_limits()
        self.client.get("/en/shops/")
        LoginChallenge.objects.filter(linking_key="").update(linking_key="02" + "ab" * 32)
        with patch("shops.views.limits.refuse", return_value=True):
            response = self.client.get(reverse("login_status"))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["logged_in"], False)
        self.assertFalse(get_user_model().objects.filter(username__startswith="02").exists())


class Claim(TestCase):
    def setUp(self):
        reset_limits()
        self.user = get_user_model().objects.create(username="email:abc")
        self.client.force_login(self.user)
        self.shop = Shop.objects.create(name="Bar", lightning_address="bar@ln.example.org",
                                        claim_hash=claim_digest(CODE))

    def test_a_claim_another_account_took_meanwhile_is_gone(self):
        def taken_meanwhile(*args, **kwargs):
            Shop.objects.filter(pk=self.shop.pk).update(owner=get_user_model().objects.create(username="other"), claim_hash="")
            return False

        with patch("shops.views.certified.address_taken", side_effect=taken_meanwhile):
            response = self.client.post(reverse("claim_shop"), {"code": CODE})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(Shop.objects.get(pk=self.shop.pk).owner.username, "other")


class RecoveryAddress(TestCase):
    def setUp(self):
        reset_limits()
        self.user = get_user_model().objects.create(username="k")
        Profile.objects.create(user=self.user, email="old@example.org")
        self.client.force_login(self.user)

    def test_the_email_budget_stops_the_confirmation_link(self):
        with patch("shops.views.mailbudget.visitor_allowed", return_value=False):
            response = self.client.post("/en/shops/", {"email": "new@example.org"}, follow=True)
        self.assertContains(response, "This site cannot send more emails for now")
        self.assertEqual(mail.outbox, [])

    def test_a_link_that_cannot_be_sent_is_said(self):
        with patch("shops.views.send_link", return_value=False):
            response = self.client.post("/en/shops/", {"email": "new@example.org"}, follow=True)
        self.assertContains(response, "The email could not be sent")
        self.assertNotContains(response, "We sent a confirmation link")

    def test_an_empty_address_removes_the_email_at_once(self):
        self.client.post("/en/shops/", {"email": ""})
        self.assertEqual(Profile.objects.get(user=self.user).email, "")
        self.assertEqual(mail.outbox, [])


@COUNTING
class BudgetFailsOpen(TestCase):
    """A database that cannot count does not stop the sign-in links: one email too many is better than none."""

    def setUp(self):
        reset_limits()

    def test_a_visitor_is_allowed_when_the_count_cannot_be_read(self):
        with patch("config.mailbudget.blocked", side_effect=DatabaseError), self.assertLogs("config.mailbudget", "ERROR"):
            self.assertTrue(mailbudget.visitor_allowed("a@example.org"))

    def test_the_backend_sends_when_the_count_cannot_be_read_or_written(self):
        with patch("config.mailbudget.blocked", side_effect=DatabaseError), self.assertLogs("config.mailbudget", "ERROR"):
            self.assertEqual(ask(), 1)
        with patch("config.mailbudget.record", side_effect=DatabaseError), self.assertLogs("config.mailbudget", "ERROR"):
            self.assertEqual(ask(), 1)
        self.assertEqual(len(mail.outbox), 2)

    def test_the_backend_opens_and_closes_the_real_one(self):
        from django.core.mail import get_connection

        with get_connection() as connection:
            self.assertIsInstance(connection, mailbudget.CountingBackend)

    def test_without_a_cap_per_address_nobody_is_crowded(self):
        for _ in range(3):
            ask("owner@example.org")
        with set_limits(emails_per_address_day=0):
            self.assertEqual(mailbudget.crowded(), 0)
        with set_limits(emails_per_address_day=2):
            self.assertEqual(mailbudget.crowded(), 1)


class AddressChange(TestCase):
    OLD, NEW = "bar@old.example", "bar@new.example"

    def setUp(self):
        reset_limits()
        self.owner = get_user_model().objects.create_user("owner")
        Profile.objects.create(user=self.owner, email="owner@mail.example")
        self.shop = Shop.objects.create(name="Bar", owner=self.owner, lightning_address=self.OLD, lightning=True)
        self.client.force_login(self.owner)
        self.url = reverse("change_address", args=[self.shop.pk])
        self.enterContext(patch("shops.address_change.refusal", return_value=None))

    def test_an_address_taken_under_the_lock_changes_nothing(self):
        """Another owner's shop took the address between the form's check and the write."""
        with patch("shops.address_change.address_taken", return_value=True):
            response = self.client.post(self.url, {"lightning_address": self.NEW})
        self.assertRedirects(response, reverse("edit_shop", kwargs={"shop": self.shop.pk}), fetch_redirect_response=False)
        self.assertEqual(Shop.objects.get(pk=self.shop.pk).lightning_address, self.OLD)

    def test_too_many_tries_are_429(self):
        with patch("shops.address_change.ratelimit.over_limit", return_value=True):
            response = self.client.post(self.url, {"lightning_address": self.NEW})
        self.assertContains(response, "Too many tries in a short time", status_code=429)
        self.assertEqual(Shop.objects.get(pk=self.shop.pk).lightning_address, self.OLD)


class TrainingTries(TestCase):
    def setUp(self):
        from .test_training import EXAMPLE_WITHOUT_QUICK, configured

        reset_limits()
        self.enterContext(configured(EXAMPLE_WITHOUT_QUICK))
        self.owner = get_user_model().objects.create(username="owner")
        Shop.objects.create(owner=self.owner, name="Bar", lightning_address="s@ln.example.org")
        self.client.force_login(self.owner)
        self.enterContext(patch("shops.training_views.busy", return_value=True))

    def test_a_declaration_past_the_tries_is_not_saved(self):
        from . import training
        from .models import Declaration

        step = next(iter(training.step_keys()))
        response = self.client.post(reverse("training_step"), {"step": step, "agree": "1"}, follow=True)
        self.assertContains(response, "Too many tries in a short time")
        self.assertFalse(Declaration.objects.exists())

    def test_the_review_past_the_tries_is_not_marked(self):
        from .models import Trainee

        Trainee.objects.create(user=self.owner, base_earned=timezone.now(), reviewed=timezone.now() - timedelta(days=400))
        response = self.client.post(reverse("training_review"), {"a-wallet": 0}, follow=True)
        self.assertContains(response, "Too many tries in a short time")
        self.assertLess(Trainee.objects.get().reviewed, timezone.now() - timedelta(days=399))



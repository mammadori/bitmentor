"""The limits of growth (config/limits.py): defaults and what `community.yml` changes, what is refused at a limit and what
is kept, and where the staff sees the usage (the dashboard, `manage.py preflight`, `manage.py check`)."""
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import checks
from django.core.cache import cache
from django.core.management import CommandError, call_command
from django.test import TestCase

from config import limits
from config.community import community

from . import dashboard, helpdesk
from .apps import wrong_limits
from .models import HelpRequest, Shop
from .tests import admin_session, reset_limits


def set_limits(**values):
    return patch.dict(community(), {"limits": values})


def row(key):
    return next(found for found in limits.rows() if found["key"] == key)


class Values(TestCase):
    def test_the_defaults_are_the_documented_ones(self):
        self.assertEqual(limits.get("shops"), 500)
        self.assertEqual(limits.get("help_requests_per_shop_day"), 3)

    def test_community_yml_changes_a_default_and_zero_is_no_limit(self):
        with set_limits(shops=3, accounts=0):
            self.assertEqual(limits.get("shops"), 3)
            self.assertFalse(limits.reached("accounts"))
            self.assertEqual(limits.rate("accounts"), limits.NO_LIMIT)

    def test_a_wrong_value_is_the_default_and_the_check_says_so(self):
        with set_limits(shops="many", accounts=-1, nonsense=4, help_requests=True):
            self.assertEqual(limits.get("shops"), 500)
            self.assertEqual(limits.get("accounts"), 2000)
            self.assertEqual(limits.get("help_requests"), 5000)
            self.assertEqual([found.id for found in wrong_limits(None)], ["shops.W009"] * 4)
        self.assertEqual(limits.problems(), [])

    def test_every_limit_is_documented_in_the_example_and_in_the_docs(self):
        root = Path(__file__).resolve().parent.parent
        example = (root / "examples/community/community.yml").read_text()
        # The image leaves docs/ out (.dockerignore): there only the example is checked
        guide = root / "docs/configuration.md"
        docs = guide.read_text() if guide.exists() else None
        for key in limits.LIMITS:
            self.assertIn(f"#   {key}:", example)
            if docs is not None:
                self.assertIn(f"`{key}`", docs)
        for key in limits.counters():
            self.assertTrue(limits.LIMITS[key].total)


class Refusals(TestCase):
    def setUp(self):
        cache.clear()
        reset_limits()
        self.owner = get_user_model().objects.create_user("owner")
        self.client.force_login(self.owner)

    def test_a_full_site_refuses_a_new_shop_and_keeps_the_others(self):
        Shop.objects.create(name="Old", owner=self.owner)
        with set_limits(shops=1), patch("shops.views.refusal", return_value=None), \
                patch("shops.forms.public_host", return_value=True), self.assertLogs("config.limits", "WARNING") as logged:
            response = self.client.post("/en/shops/new/", {"name": "New", "lightning_address": "a@example.org", "discount": 0})
        self.assertContains(response, "cannot take more")
        self.assertEqual(list(Shop.objects.values_list("name", flat=True)), ["Old"])
        self.assertIn("limits.shops", logged.output[0])

    def test_sign_ups_without_a_wallet_have_their_own_limit(self):
        Shop.objects.create(name="Waiting", owner=self.owner, awaiting_wallet=True)
        with set_limits(shops_waiting_wallet=1):
            response = self.client.post("/en/shops/new/", {"name": "Second", "later": "on", "discount": 0})
        self.assertContains(response, "cannot take more")
        self.assertEqual(Shop.objects.count(), 1)

    def test_demo_shops_do_not_count(self):
        Shop.objects.create(name="Demo", owner=self.owner, external_id="bitmentor-demo:1")
        self.assertEqual(limits.used("shops"), 0)

    def test_accounts_count_without_the_staff(self):
        get_user_model().objects.create_superuser("boss", "boss@example.org", "x")
        with set_limits(accounts=1):
            self.assertTrue(limits.reached("accounts"))
        with set_limits(accounts=2):
            self.assertFalse(limits.reached("accounts"))

    def test_the_check_near_the_limits_lets_a_new_database_migrate(self):
        from django.db import OperationalError

        from .apps import limits_near

        with patch.object(limits, "notes", side_effect=OperationalError("no such table: shops_shop")):
            self.assertEqual(limits_near(None, databases=["default"]), [])

    def test_a_full_site_makes_no_account_from_an_email_link(self):
        from django.core import signing
        from django.urls import reverse

        from .views import login_state

        email = "new@example.org"
        token = signing.dumps({"email": email, "state": login_state(email)}, salt="recovery")
        before = get_user_model().objects.count()
        with set_limits(accounts=1):
            response = self.client.post(reverse("recovery_login"), {"token": token})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(get_user_model().objects.count(), before)

    def test_a_full_help_desk_refuses_and_keeps_the_requests(self):
        shop = Shop.objects.create(name="Bakery", owner=self.owner, lightning_address="b@example.org")
        HelpRequest.objects.create(shop=shop, symptom="pay")
        with set_limits(help_requests=1), self.assertRaises(helpdesk.Full):
            helpdesk.create(shop, "open", "", "page", self.owner)
        self.assertEqual(HelpRequest.objects.count(), 1)


class Reports(TestCase):
    def setUp(self):
        cache.clear()
        reset_limits()
        self.owner = get_user_model().objects.create_user("owner")
        for number in range(4):
            Shop.objects.create(name=f"Shop {number}", owner=self.owner)

    def test_rows_say_used_limit_and_level(self):
        with set_limits(shops=5):
            self.assertEqual((row("shops")["used"], row("shops")["limit"], row("shops")["level"], row("shops")["default"]),
                             (4, 5, "near", False))
        with set_limits(shops=4):
            self.assertEqual(row("shops")["level"], "full")
        self.assertEqual(row("shops")["level"], "")

    def test_the_dashboard_has_an_item_near_and_at_the_limit(self):
        def item():
            return next((item for item in dashboard.summary()["items"] if item.code == "limit_shops"), None)

        with set_limits(shops=5):
            self.assertEqual(item().level, dashboard.WARNING)
            self.assertIn("4 of 5", item().what)
        with set_limits(shops=4):
            self.assertEqual(item().level, dashboard.PROBLEM)
        self.assertIsNone(item())

    def test_the_admin_page_shows_the_usage(self):
        admin_session(self.client)
        with set_limits(shops=40):
            response = self.client.get("/admin/today/")
        self.assertContains(response, "4/40")
        self.assertContains(response, "set in community.yml")

    def test_preflight_and_check_warn_near_the_limit(self):
        with set_limits(shops=5):
            out = StringIO()
            # A test site is not ready for mainnet: preflight fails, after the notes
            with self.assertRaises(CommandError):
                call_command("preflight", stdout=out, stderr=StringIO())
            self.assertIn("good to know: limit 4/5 shops", out.getvalue())
            self.assertIn("limits changed in community.yml (limits): shops=5", out.getvalue())
            self.assertIn("shops.W010", [found.id for found in checks.run_checks(databases=["default"])])
        self.assertNotIn("shops.W010", [found.id for found in checks.run_checks(databases=["default"])])

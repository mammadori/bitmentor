"""The day's list and the merchant's training (shops/dashboard.py, `training_items`; shops/training.py, `stuck`): the
owners whose real register waits for the pass, or who stopped half-way through the basic modules, for more than
`nudge_days` days, one item each, to call them. Also in the morning summary and in the volunteers' area."""
from datetime import datetime, timedelta
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone, translation

from team.test_area import RECOVERY, AreaBase

from . import dashboard, training
from .models import ModuleResult, Shop, Trainee
from .test_dashboard import register
from .test_training import EXAMPLE, configured

User = get_user_model()
# The first day the tests count from: noon, so that no business day or time zone moves it
DAY = datetime(2026, 3, 2, 12, 0, tzinfo=timezone.get_default_timezone())


def at(days):
    return patch("django.utils.timezone.now", return_value=DAY + timedelta(days=days))


def codes(found):
    return [(item.code, item.shop.name if item.shop else None) for item in found["items"]
            if item.code.startswith("training_")]


def title(name):
    return training.shown(training.find_module(training.parse(), name).title)


@override_settings(NETWORK="mainnet", TELEGRAM_BOT_TOKEN="", LANGUAGE_CODE="en")
class Nudges(TestCase):
    block = EXAMPLE

    def setUp(self):
        cache.clear()
        patcher = configured(self.block)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.owner = User.objects.create(username="owner-login", email="owner@example.org")
        self.shop = register("Bar", owner=self.owner, created=DAY)
        translation.activate("en")
        self.addCleanup(translation.deactivate)

    def found(self, days):
        with at(days):
            return dashboard.summary()

    def start_the_path(self, when=DAY):
        """The first module done (its quiz and its two declarations): the next step is the second one."""
        ModuleResult.objects.create(user=self.owner, module="wallet", passed=when, attempts=1)
        module = training.find_module(training.parse(), "wallet")
        for statement in module.statements:
            training.record(self.owner, self.shop, statement.key, statement.version, "text", now=when)

    def test_the_register_that_waits_for_the_pass_after_three_days(self):
        self.assertEqual(codes(self.found(2)), [])
        self.assertEqual(codes(self.found(3)), [])
        found = self.found(4)
        self.assertEqual(codes(found), [("training_register_waits", "Bar")])
        item = found["items"][0]
        self.assertEqual(item.level, dashboard.WARNING)
        self.assertTrue(item.do.startswith("Call them:"))
        self.assertIn(title("wallet"), item.do)
        self.assertIn("Bar", item.what)
        self.assertEqual(item.links, [dashboard.shop_link(self.shop)])
        self.assertEqual(found["counts"]["to_look"], 1)
        self.assertNotIn("owner-login", item.what + item.do)

    def test_the_wait_counts_from_the_last_declaration(self):
        self.start_the_path(DAY + timedelta(days=2))
        self.assertEqual(codes(self.found(5)), [])
        found = self.found(6)
        self.assertEqual(codes(found), [("training_register_waits", "Bar")])
        self.assertIn(title("paid"), found["items"][0].do)
        self.assertEqual(found["items"][0].since, DAY + timedelta(days=2))

    def test_the_wait_counts_from_the_since_day(self):
        with configured({**self.block, "since": "2026-03-04"}):
            self.assertEqual(codes(self.found(4)), [])
            self.assertEqual(codes(self.found(5)), [])
            self.assertEqual(codes(self.found(6)), [("training_register_waits", "Bar")])

    @override_settings(NETWORK="signet")
    def test_stopped_half_way_needs_a_pass_that_matters(self):
        self.start_the_path()
        self.assertEqual(codes(self.found(10)), [])

    def test_stopped_half_way_where_the_register_is_not_locked(self):
        # The register opens until the community's day: the pass matters, nothing locks yet
        with configured({**self.block, "since": "2027-01-01"}):
            self.start_the_path()
            self.assertEqual(codes(self.found(2)), [])
            self.assertEqual(codes(self.found(3)), [])
            found = self.found(4)
            self.assertEqual(codes(found), [("training_stopped", "Bar")])
            item = found["items"][0]
            self.assertEqual(item.level, dashboard.WARNING)
            self.assertIn(f"«{title('paid')}»", item.do)
            self.assertTrue(item.do.startswith("Call them:"))

    def test_never_started_and_not_locked_is_nobody_to_call(self):
        with configured({**self.block, "since": "2027-01-01"}):
            self.assertEqual(codes(self.found(30)), [])

    def test_a_gate_that_only_reminds_says_nothing(self):
        self.start_the_path()
        with configured({**self.block, "gate": "reminder"}):
            self.assertEqual(codes(self.found(30)), [])

    def test_without_a_training_block_nothing(self):
        self.start_the_path()
        with configured(None):
            self.assertEqual(codes(self.found(30)), [])

    def test_nudge_days_zero_or_false_turns_it_off(self):
        for value in (0, False):
            with configured({**self.block, "nudge_days": value}):
                self.assertEqual(training.problems(), [])
                self.assertEqual(training.parse().nudge_days, 0)
                self.assertEqual(codes(self.found(30)), [])

    def test_the_number_comes_from_community_yml(self):
        with configured({**self.block, "nudge_days": 10}):
            self.assertEqual(codes(self.found(10)), [])
            self.assertEqual(codes(self.found(11)), [("training_register_waits", "Bar")])
        self.assertEqual(EXAMPLE.get("nudge_days"), 3)

    def test_a_wrong_number_counts_as_three_and_is_told(self):
        for value in (61, -1, "5", 2.5, True):
            with configured({**self.block, "nudge_days": value}):
                self.assertTrue(any("training.nudge_days" in problem for problem in training.problems()), value)
                self.assertEqual(training.parse().nudge_days, 3)
        with configured({**self.block, "nudge_days": 60}):
            self.assertEqual(training.problems(), [])

    def test_the_owner_with_the_pass_is_not_called(self):
        self.start_the_path()
        Trainee.objects.create(user=self.owner, base_earned=DAY, reviewed=DAY)
        self.assertEqual(codes(self.found(30)), [])

    def test_suspended_closed_and_demo_shops_are_left_out(self):
        # The demo shops come from a list (`external_id`): their gate is written as the others', so only being a demo counts
        with configured({**self.block, "gate_imported": "unlock"}):
            self.assertEqual(len(codes(self.found(30))), 1)
            for fields in ({"suspended": True}, {"closed_on": DAY.date()}, {"external_id": "bitmentor-demo:1"}):
                Shop.objects.filter(pk=self.shop.pk).update(**{"suspended": False, "closed_on": None, "external_id": "",
                                                               **fields})
                self.assertEqual(codes(self.found(30)), [], fields)

    def test_one_item_for_an_owner_with_two_shops(self):
        register("Cafe", owner=self.owner, created=DAY + timedelta(days=1))
        found = self.found(30)
        self.assertEqual(codes(found), [("training_register_waits", "Bar")])
        self.assertIn("Bar, Cafe", found["items"][0].what)
        self.assertEqual(found["counts"]["to_look"], 1)

    def test_two_owners_two_items_the_oldest_first(self):
        other = User.objects.create(username="other")
        register("Alpha", owner=other, created=DAY + timedelta(days=1))
        self.assertEqual(codes(self.found(30)), [("training_register_waits", "Bar"),
                                                 ("training_register_waits", "Alpha")])

    def test_few_queries_for_many_owners(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        for number in range(2):
            register(f"Shop{number}", owner=User.objects.create(username=f"u{number}"), created=DAY)
        with at(30), CaptureQueriesContext(connection) as few:
            dashboard.summary()
        for number in range(2, 12):
            register(f"Shop{number}", owner=User.objects.create(username=f"u{number}"), created=DAY)
        with at(30), CaptureQueriesContext(connection) as many:
            found = dashboard.summary()
        self.assertEqual(len(codes(found)), 13)
        self.assertEqual(len(few), len(many))

    def test_the_communitys_rule_is_asked_once_per_owner(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from .test_hooks import Hooked

        asked = []
        rule = lambda user: asked.append(user.pk) and None  # no grace for anybody
        register("Bar2", owner=self.owner, created=DAY)
        with Hooked(training_grace_until=rule):
            with at(30), CaptureQueriesContext(connection) as few:
                self.assertEqual(len(codes(dashboard.summary())), 1)
            self.assertEqual(sorted(asked), [self.owner.pk])
            for number in range(5):
                owner = User.objects.create(username=f"u{number}")
                register(f"Shop{number}a", owner=owner, created=DAY)
                register(f"Shop{number}b", owner=owner, created=DAY)
            asked.clear()
            with at(30), CaptureQueriesContext(connection) as many:
                self.assertEqual(len(codes(dashboard.summary())), 6)
        self.assertEqual(len(asked), 6)
        self.assertEqual(len(set(asked)), 6)
        self.assertEqual(len(few), len(many))

    def test_a_database_error_in_the_rule_leaves_the_owner_off_the_list(self):
        from django.db import DatabaseError

        from .test_hooks import Hooked

        def rule(user):
            raise DatabaseError("the community's table does not answer")

        with Hooked(training_grace_until=rule), self.assertLogs("shops.training", "WARNING") as logged:
            found = self.found(30)
        self.assertEqual(codes(found), [])
        self.assertEqual(len(logged.records), 1)
        # The day's list itself is still there, and so is the command's summary
        self.assertIn("counts", found)

    def test_the_wait_counts_from_the_day_after_the_grace(self):
        from .test_hooks import Hooked

        with Hooked(training_grace_until=lambda user: (DAY + timedelta(days=28)).date()):
            # Still in the grace days: the register is open, nothing to call about
            self.assertEqual(codes(self.found(28)), [])
            self.assertEqual(codes(self.found(30)), [])
            self.assertEqual(codes(self.found(32)), [])
            found = self.found(33)
        self.assertEqual(codes(found), [("training_register_waits", "Bar")])
        self.assertEqual(found["items"][0].since.date(), (DAY + timedelta(days=29)).date())

    def test_the_wait_counts_from_the_wallet_connected(self):
        from .models import AccountChange

        with at(20):
            Shop.objects.filter(pk=self.shop.pk).update(lightning_address="new@example.org")
        rows = AccountChange.objects.filter(user=self.owner, changed__contains="shop_lightning")
        self.assertEqual(rows.count(), 1)
        # The field's default is bound to the real clock: the row is dated by hand
        rows.update(created=DAY + timedelta(days=20))
        self.assertEqual(codes(self.found(20)), [])
        self.assertEqual(codes(self.found(23)), [])
        self.assertEqual(codes(self.found(24)), [("training_register_waits", "Bar")])

    def test_a_shop_set_aside_is_nobody_to_call_one_waiting_for_its_approval_is(self):
        Shop.objects.filter(pk=self.shop.pk).update(approved_on=None, set_aside_on=None)
        self.assertEqual(codes(self.found(30)), [("training_register_waits", "Bar")])
        Shop.objects.filter(pk=self.shop.pk).update(set_aside_on=DAY)
        self.assertEqual(codes(self.found(30)), [])
        # Set aside once, approved later: a shop like the others
        Shop.objects.filter(pk=self.shop.pk).update(approved_on=DAY)
        self.assertEqual(codes(self.found(30)), [("training_register_waits", "Bar")])

    @override_settings(TELEGRAM_BOT_TOKEN="t", TELEGRAM_MODERATION_CHAT="-100", SITE_HOST="shops.example.org",
                       PUBLIC_URL="https://shops.example.org")
    def test_in_the_morning_summary(self):
        out = StringIO()
        with at(30), patch("shops.notifications.requests.post") as post:
            post.return_value.status_code = 200
            with self.assertRaises(SystemExit) as end:
                call_command("dashboard", "--send", "--language", "en", stdout=out)
        self.assertEqual(end.exception.code, 10)
        text = post.call_args.kwargs["json"]["text"]
        self.assertIn("! Bar: The real register waits for the owner's basic pass of the training: Bar.", text)
        self.assertNotIn("owner-login", text)
        self.assertNotIn("owner@example.org", text)

    def test_the_page_of_the_admin_links_the_shop(self):
        from .tests import admin_session

        admin_session(self.client)
        with at(30):
            page = self.client.get("/admin/today/")
        self.assertContains(page, "Call them:")
        self.assertContains(page, f'href="/admin/shops/shop/{self.shop.pk}/change/"')

    def test_italian_and_german(self):
        with at(30), translation.override("it"):
            self.assertIn("patentino", dashboard.summary()["items"][0].what)
        with at(30), translation.override("de"):
            item = dashboard.summary()["items"][0]
            self.assertIn("Sie", item.do)
            self.assertIn("Thekenschein", item.what)


@override_settings(NETWORK="mainnet")
class InTheArea(AreaBase):
    """/team/today/ for a moderator's key: the shop and the step, nothing of the owner's account."""

    def test_the_item_with_the_shop_and_the_step(self):
        with configured(EXAMPLE):
            ModuleResult.objects.create(user=self.owner, module="wallet", passed=timezone.now() - timedelta(days=9))
            for statement in training.find_module(training.parse(), "wallet").statements:
                training.record(self.owner, self.shop, statement.key, statement.version, "text",
                                now=timezone.now() - timedelta(days=9))
            _key, browser = self.moderator()
            page = browser.get("/team/today/").content.decode()
            step = training.shown(training.find_module(training.parse(), "paid").title, "en")
        self.assertIn("training_register_waits", page)
        self.assertIn("Call them:", page)
        self.assertIn(step, page)
        for gone in (RECOVERY, f"/admin/shops/shop/{self.shop.pk}/"):
            self.assertNotIn(gone, page)

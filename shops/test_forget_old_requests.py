"""Forgetting (shops/management/commands/forget_old_requests.py): a request for help closed more than a year ago loses what it
kept of a person and stays as a row for the counts; one that is open, waiting, reopened or closed less than a year ago is not
touched; the second run does nothing; and the command runs with the payments timer."""
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.conf import settings
from django.core import checks
from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.test import TestCase
from django.utils import timezone

from config.community import community

from . import privacy
from .management.commands import forget_old_requests
from .models import Closure, HelpRequest, Profile, Shop
from .test_help import Base as HelpBase

User = get_user_model()

DIAGNOSIS = ("✓ [owner_recovery_email] You can sign in with a link sent to your email, even without the wallet.\n"
             "    signs in with: wallet key 02ab12cd…; last login 2025-01-03 18:02 UTC; recovery email m***@e***.org\n"
             "! [lightning_no_answer] Your Lightning wallet is not answering.\n"
             "    bar@ln.example.org; 4.2 s: LNURL-pay metadata or invoice not obtained")
FORGOTTEN = {"note": "", "summary": "", "channel": "page", "asked_by_id": None, "resolved_by": ""}


def run(*options):
    out = StringIO()
    call_command("forget_old_requests", *options, stdout=out)
    return out.getvalue()


class ForgetOldRequests(TestCase):
    def setUp(self):
        self.owner = User.objects.create(username="merchant")
        Profile.objects.create(user=self.owner, email="mario.rossi@example.org")
        self.shop = Shop.objects.create(owner=self.owner, name="Central Bar", city="Town")
        self.now = timezone.now()

    def ask(self, solved_days_ago=None, asked_days_ago=None, **fields):
        """A request that keeps a person's words, solved `solved_days_ago` days ago (open when None)."""
        solved = None if solved_days_ago is None else self.now - timedelta(days=solved_days_ago)
        asked = self.now - timedelta(days=asked_days_ago if asked_days_ago is not None else (solved_days_ago or 0) + 1)
        values = {"shop": self.shop, "asked_by": self.owner, "symptom": HelpRequest.Symptom.PAY, "note": "Call me after six",
                  "channel": HelpRequest.Channel.PHONE, "summary": DIAGNOSIS, "delivery": HelpRequest.Delivery.SENT,
                  "created": asked, "resolved": solved, "resolved_by": "volunteer" if solved else ""}
        return HelpRequest.objects.create(**{**values, **fields})

    def stored(self, row):
        return HelpRequest.objects.values().get(pk=row.pk)

    def kept_for_the_counts(self, row):
        """What a forgotten row still says: which shop, which symptom, when it was asked and solved, whether the message went."""
        return {name: self.stored(row)[name] for name in ("shop_id", "symptom", "created", "resolved", "delivery")}

    # -- what is forgotten -------------------------------------------------------------------------------------------

    def test_a_request_solved_over_a_year_ago_loses_what_it_kept_of_a_person_and_stays_for_the_counts(self):
        row = self.ask(solved_days_ago=400)
        counts = self.kept_for_the_counts(row)
        out = run()
        self.assertEqual({name: self.stored(row)[name] for name in FORGOTTEN}, FORGOTTEN)
        self.assertEqual(self.kept_for_the_counts(row), counts)
        self.assertIn("1 shops.HelpRequest", out)
        self.assertEqual(HelpRequest.objects.filter(shop=self.shop, symptom="pay", resolved__isnull=False).count(), 1)

    def test_nothing_of_the_diagnosis_or_the_merchants_words_is_left_in_the_row(self):
        row = self.ask(solved_days_ago=400)
        run()
        text = " ".join(str(value) for value in self.stored(row).values())
        for gone in ("Call me", "bar@ln.example.org", "02ab12cd", "m***@e***.org", "last login", "volunteer"):
            self.assertNotIn(gone, text)

    def test_the_shop_and_the_account_are_not_touched_only_the_rows_link_to_it(self):
        row = self.ask(solved_days_ago=400)
        before = (Shop.objects.values().get(pk=self.shop.pk), User.objects.values().get(pk=self.owner.pk),
                  Profile.objects.values().get(user=self.owner))
        run()
        self.assertEqual((Shop.objects.values().get(pk=self.shop.pk), User.objects.values().get(pk=self.owner.pk),
                          Profile.objects.values().get(user=self.owner)), before)
        self.assertIsNone(HelpRequest.objects.get(pk=row.pk).asked_by)

    # -- what is not -------------------------------------------------------------------------------------------------

    def test_a_request_solved_less_than_a_year_ago_is_not_touched(self):
        row = self.ask(solved_days_ago=300)
        edge = self.ask(solved_days_ago=364, symptom=HelpRequest.Symptom.OPEN)
        before = [self.stored(row), self.stored(edge)]
        out = run()
        self.assertEqual([self.stored(row), self.stored(edge)], before)
        self.assertIn("0 shops.HelpRequest", out)

    def test_the_day_that_counts_is_the_day_it_was_solved_not_the_day_it_was_asked(self):
        """Asked two years ago, solved last week: it was answered only a week ago, and the merchant may still look at it."""
        row = self.ask(solved_days_ago=7, asked_days_ago=730)
        before = self.stored(row)
        run()
        self.assertEqual(self.stored(row), before)

    def test_a_request_still_open_is_not_touched_however_old(self):
        row = self.ask(asked_days_ago=900)
        before = self.stored(row)
        run()
        run("--days", "0", "--force")
        self.assertEqual(self.stored(row), before)
        self.assertIsNone(self.stored(row)["resolved"])

    def test_a_request_reopened_after_a_year_is_waiting_again_and_is_not_touched(self):
        row = self.ask(solved_days_ago=500)
        HelpRequest.objects.filter(pk=row.pk).update(resolved=None, resolved_by="")
        before = self.stored(row)
        run()
        self.assertEqual(self.stored(row), before)

    def test_only_the_old_ones_of_many_are_forgotten(self):
        old = [self.ask(solved_days_ago=366 + n) for n in range(3)]
        recent = [self.ask(solved_days_ago=n * 100) for n in range(1, 4)]
        waiting = [self.ask(asked_days_ago=n * 200) for n in range(1, 3)]
        before = [self.stored(row) for row in [*recent, *waiting]]
        self.assertIn("3 shops.HelpRequest", run())
        self.assertEqual([self.stored(row) for row in [*recent, *waiting]], before)
        for row in old:
            self.assertEqual({name: self.stored(row)[name] for name in FORGOTTEN}, FORGOTTEN)
        self.assertEqual(HelpRequest.objects.count(), 8)

    # -- the second run, the dry run, the days -----------------------------------------------------------------------

    def test_a_second_run_does_nothing_and_says_so(self):
        row = self.ask(solved_days_ago=400)
        self.assertIn("1 shops.HelpRequest", run())
        after = self.stored(row)
        self.assertIn("0 shops.HelpRequest", run())
        self.assertEqual(self.stored(row), after)

    def test_a_dry_run_says_how_many_and_forgets_nothing(self):
        row = self.ask(solved_days_ago=400)
        self.ask(solved_days_ago=10)
        before = self.stored(row)
        out = run("--dry-run")
        self.assertEqual(self.stored(row), before)
        self.assertIn("Dry run: 1 shops.HelpRequest", out)
        self.assertIn("would be forgotten", out)

    def test_the_days_can_be_changed(self):
        young = self.ask(solved_days_ago=40)
        older = self.ask(solved_days_ago=200)
        run("--days", "30")
        self.assertEqual(self.stored(young)["note"], "")
        self.assertEqual(self.stored(older)["note"], "")
        again = self.ask(solved_days_ago=40)
        run("--days", "90")
        self.assertEqual(self.stored(again)["note"], "Call me after six")

    def test_zero_days_forgets_everything_that_is_closed_and_nothing_that_is_not(self):
        closed = self.ask(solved_days_ago=1)
        waiting = self.ask()
        run("--days", "0", "--force")
        self.assertEqual(self.stored(closed)["note"], "")
        self.assertEqual(self.stored(waiting)["note"], "Call me after six")

    def test_a_negative_number_of_days_is_refused_and_nothing_changes(self):
        row = self.ask(solved_days_ago=400)
        before = self.stored(row)
        with self.assertRaises(CommandError):
            run("--days", "-1")
        self.assertEqual(self.stored(row), before)

    # -- the days: community.yml, the floor, a number too big ---------------------------------------------------------

    def with_privacy(self, block):
        return patch("shops.privacy.community", return_value={**community(), "privacy": block})

    def test_without_days_the_command_uses_the_number_of_community_yml(self):
        row = self.ask(solved_days_ago=200)
        run()
        self.assertEqual(self.stored(row)["note"], "Call me after six")
        with self.with_privacy({"help_requests_days": 180}):
            run()
        self.assertEqual(self.stored(row)["note"], "")

    def test_without_the_block_it_is_a_year(self):
        self.assertEqual(privacy.days("help_requests_days"), 365)
        self.assertEqual(privacy.problems(), [])

    def test_days_on_the_command_line_win_over_community_yml(self):
        row = self.ask(solved_days_ago=200)
        with self.with_privacy({"help_requests_days": 3650}):
            run("--days", "100")
        self.assertEqual(self.stored(row)["note"], "")

    def test_zero_or_false_in_community_yml_is_never_from_the_timer_and_says_so(self):
        row = self.ask(solved_days_ago=900)
        before = self.stored(row)
        for never in (0, False):
            with self.with_privacy({"help_requests_days": never}):
                out = run()
            self.assertIn("does not forget by itself", out)
            self.assertEqual(self.stored(row), before)
        # By hand it still works
        with self.with_privacy({"help_requests_days": 0}):
            run("--days", "400")
        self.assertEqual(self.stored(row)["note"], "")

    def test_a_wrong_number_in_community_yml_counts_as_a_year_and_check_says_so(self):
        row = self.ask(solved_days_ago=200)
        for wrong in (True, "180", 12.5, 29, -5, 5000, None, [180]):
            with self.with_privacy({"help_requests_days": wrong}):
                self.assertEqual(privacy.days("help_requests_days"), 365, wrong)
                found = privacy.problems()
                run()
            self.assertEqual(self.stored(row)["note"], "Call me after six", wrong)
            self.assertEqual(len(found), 1, wrong)
            self.assertIn("privacy.help_requests_days", found[0])
        with self.with_privacy({"help_requests_days": 30}):
            self.assertEqual(privacy.problems(), [])

    def test_a_block_that_is_not_a_block_or_has_an_unknown_name_is_told(self):
        with self.with_privacy("yes"):
            self.assertEqual(len(privacy.problems()), 1)
            self.assertEqual(privacy.days("help_requests_days"), 365)
        with self.with_privacy({"help_request_days": 90}):
            problems = privacy.problems()
        self.assertEqual(len(problems), 1)
        self.assertIn("help_request_days", problems[0])

    def test_manage_py_check_warns_about_it(self):
        with self.with_privacy({"help_requests_days": 7}):
            found = [message for message in checks.run_checks() if message.id == "shops.W004"]
        self.assertEqual(len(found), 1)
        self.assertIn("7", found[0].msg)
        self.assertEqual([message for message in checks.run_checks() if message.id == "shops.W004"], [])

    def test_under_a_month_is_refused_without_force_and_nothing_changes(self):
        row = self.ask(solved_days_ago=10)
        before = self.stored(row)
        for days in ("0", "5", "29"):
            with self.assertRaises(CommandError) as raised:
                run("--days", days)
            self.assertIn("--force", str(raised.exception))
        self.assertEqual(self.stored(row), before)
        run("--days", "5", "--force")
        self.assertEqual(self.stored(row)["note"], "")

    def test_a_month_exactly_needs_no_force(self):
        row = self.ask(solved_days_ago=31)
        run("--days", "30")
        self.assertEqual(self.stored(row)["note"], "")

    def test_a_number_of_days_that_does_not_fit_is_refused_and_nothing_changes(self):
        row = self.ask(solved_days_ago=400)
        before = self.stored(row)
        for days in ("999999999", "99999999999999999999", "1000000"):
            with self.assertRaises(CommandError) as raised:
                run("--days", days)
            self.assertIn("too many days", str(raised.exception))
        self.assertEqual(self.stored(row), before)

    # -- the clock ---------------------------------------------------------------------------------------------------

    def read_shop(self, **fields):
        Shop.objects.filter(pk=self.shop.pk).update(**fields)

    def test_with_no_reading_at_all_there_is_nothing_to_doubt_the_clock_with(self):
        row = self.ask(solved_days_ago=400)
        run()
        self.assertEqual(self.stored(row)["note"], "")

    def test_a_recent_reading_lets_it_through(self):
        row = self.ask(solved_days_ago=400)
        self.read_shop(lightning_checked=timezone.now() - timedelta(hours=40))
        run()
        self.assertEqual(self.stored(row)["note"], "")

    def test_the_latest_of_the_readings_counts_not_the_oldest(self):
        row = self.ask(solved_days_ago=400)
        Shop.objects.create(name="Other", lightning_checked=timezone.now() - timedelta(days=9))
        self.read_shop(lightning_checked=timezone.now() - timedelta(hours=2))
        run()
        self.assertEqual(self.stored(row)["note"], "")

    def test_a_last_reading_older_than_two_days_stops_it_and_it_says_why(self):
        row = self.ask(solved_days_ago=400)
        before = self.stored(row)
        self.read_shop(lightning_checked=timezone.now() - timedelta(days=3))
        for options in ((), ("--dry-run",)):
            with self.assertRaises(CommandError) as raised:
                run(*options)
            self.assertIn("The clock may be wrong", str(raised.exception))
            self.assertIn("Nothing forgotten", str(raised.exception))
        self.assertEqual(self.stored(row), before)

    def test_a_last_reading_in_the_future_stops_it(self):
        row = self.ask(solved_days_ago=400)
        before = self.stored(row)
        self.read_shop(lightning_checked=timezone.now() + timedelta(days=1))
        with self.assertRaises(CommandError) as raised:
            run("--days", "100")
        self.assertIn("later than now", str(raised.exception))
        self.assertEqual(self.stored(row), before)

    def test_a_few_minutes_of_difference_between_two_processes_are_not_a_wrong_clock(self):
        row = self.ask(solved_days_ago=400)
        self.read_shop(lightning_checked=timezone.now() + timedelta(minutes=3))
        run()
        self.assertEqual(self.stored(row)["note"], "")

    def test_force_does_not_silence_the_clock(self):
        self.ask(solved_days_ago=400)
        self.read_shop(lightning_checked=timezone.now() - timedelta(days=30))
        with self.assertRaises(CommandError):
            run("--days", "5", "--force")

    # -- one more line for another kind of request -------------------------------------------------------------------

    def test_another_kind_of_request_is_one_more_rule(self):
        """A model, the field that holds the day it was closed, the fields to forget: here a closure, by the day of its
        welcome back, forgets its reason (back to the default) and when it was seen (back to nothing)."""
        day, seen = self.now.date(), self.now - timedelta(days=3)
        shop = Shop.objects.create(name="Long ago", city="Town")
        closed = Closure.objects.create(shop=shop, first_day=day, reason=Closure.Reason.WORKS, seen=seen,
                                        welcomed=self.now - timedelta(days=400))
        later = Shop.objects.create(name="Later", city="Town")
        recent = Closure.objects.create(shop=later, first_day=day, reason=Closure.Reason.WORKS, seen=seen,
                                        welcomed=self.now - timedelta(days=5))
        waiting = Shop.objects.create(name="Waiting", city="Town")
        open_one = Closure.objects.create(shop=waiting, first_day=day, reason=Closure.Reason.WORKS, seen=seen)
        rules = (*forget_old_requests.RULES, forget_old_requests.Rule("shops.Closure", "welcomed", ("reason", "seen")))
        with patch.object(forget_old_requests, "RULES", rules):
            out = run()
        for row in (closed, recent, open_one):
            row.refresh_from_db()
        self.assertEqual((closed.reason, closed.seen), (Closure.Reason.HOLIDAY, None))
        self.assertEqual((recent.reason, recent.seen), (Closure.Reason.WORKS, seen))
        self.assertEqual((open_one.reason, open_one.seen), (Closure.Reason.WORKS, seen))
        self.assertIn("1 shops.Closure", out)

    def test_a_field_with_no_default_and_no_null_cannot_be_forgotten(self):
        self.ask(solved_days_ago=400)
        rules = (forget_old_requests.Rule("shops.HelpRequest", "resolved", ("note", "shop")),)
        with patch.object(forget_old_requests, "RULES", rules), self.assertRaises(CommandError):
            run()
        self.assertEqual(HelpRequest.objects.get().note, "Call me after six")

    def test_the_rule_names_exist(self):
        for rule in forget_old_requests.RULES:
            from django.apps import apps

            model = apps.get_model(rule.model)
            for name in (rule.closed, *rule.forget):
                model._meta.get_field(name)
            self.assertTrue(model._meta.get_field(rule.closed).null, "a request that is open has no closing day")

    # -- the timer and the handbook ----------------------------------------------------------------------------------

    def test_it_runs_with_the_morning_timer_and_its_failure_counts(self):
        script = (settings.BASE_DIR / "deploy" / "dashboard.sh").read_text()
        line = next(line for line in script.splitlines() if "forget_old_requests" in line and line.startswith("docker exec"))
        self.assertTrue(line.endswith("|| result=$?"), line)
        self.assertIn('[ "$result" = 0 ] || exit "$result"', script)
        # Before the summary
        self.assertLess(script.index("manage.py forget_old_requests"), script.index('docker exec "$web" python manage.py dashboard'))


class WhatTheMerchantAndTheAssociationSeeAfter(HelpBase):
    def old_request(self):
        old = timezone.now() - timedelta(days=500)
        return HelpRequest.objects.create(
            shop=self.shop, asked_by=self.owner, symptom="pay", note="Please call after six", channel="phone", summary=DIAGNOSIS,
            delivery="sent", created=old - timedelta(hours=2), resolved=old, resolved_by="volunteer")

    def test_the_old_request_is_in_the_history_of_the_owner_until_it_is_forgotten_and_then_it_is_not(self):
        self.old_request()
        before = self.client.get(self.url("help_home"))
        self.assertContains(before, "Please call after six")
        run()
        after = self.client.get(self.url("help_home"))
        self.assertEqual(after.status_code, 200)
        self.assertNotContains(after, "Please call after six")
        self.assertNotContains(after, "bar@ln.example.org")

    def test_the_administration_still_opens_the_forgotten_row_and_the_list_counts_it(self):
        from .tests import admin_session

        row = self.old_request()
        run()
        self.client.logout()
        admin_session(self.client)
        page = self.client.get(f"/admin/shops/helprequest/{row.pk}/change/")
        self.assertEqual(page.status_code, 200)
        for gone in ("Please call after six", "02ab12cd", "bar@ln.example.org", "volunteer"):
            self.assertNotContains(page, gone)
        listing = self.client.get("/admin/shops/helprequest/")
        self.assertEqual(listing.status_code, 200)
        self.assertContains(listing, "Central Bar")


class Retention(TestCase):
    """The other retention settings are defaults the community changes: the site applies no law of its own."""

    def with_privacy(self, block):
        return patch("shops.privacy.community", return_value={**community(), "privacy": block})

    def test_the_defaults_and_the_floors(self):
        self.assertEqual(privacy.days("email_prints_days"), 62)
        with self.with_privacy({"email_prints_days": 31}):
            found = privacy.problems()
            self.assertEqual(privacy.days("email_prints_days"), 62)
        self.assertEqual(len(found), 1)
        with self.with_privacy({"email_prints_days": 32}):
            self.assertEqual((privacy.days("email_prints_days"), privacy.problems()), (32, []))

    def test_never_keeps_the_email_prints(self):
        from config import mailbudget

        from .models import EmailSent

        old = timezone.now() - timedelta(days=400)
        EmailSent.objects.create(sent=old, recipient="x")
        with self.with_privacy({"email_prints_days": 0}):
            mailbudget.record(["someone@example.org"])
        self.assertTrue(EmailSent.objects.filter(sent=old).exists())
        with self.with_privacy({"email_prints_days": 40}):
            mailbudget.record(["someone@example.org"])
        self.assertFalse(EmailSent.objects.filter(sent=old).exists())

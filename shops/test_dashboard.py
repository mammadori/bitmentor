"""The association's dashboard (shops/dashboard.py, /admin/today/, manage.py dashboard): each thing to do with its
level and date, the order, the numbers on top, who may open the page, and a page that only reads the database, with
the same number of queries for two shops or for forty."""
import socket
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

import requests

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.core.management import CommandError, call_command
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from config import limits

from . import dashboard
from .models import DashboardDigest, LightningStatus, Shop
from .tests import admin_session

CALLED_OUT = AssertionError("The dashboard must not ask another server")


def nobody_outside():
    """Any HTTP request or socket fails the test. The chain height of every page's footer (config.community) is left
    out: it is the site's, not the dashboard's."""
    return (patch("requests.sessions.Session.request", side_effect=CALLED_OUT),
            patch.object(socket, "create_connection", side_effect=CALLED_OUT))


def ago(**kwargs):
    return timezone.now() - timedelta(**kwargs)


def register(name="Bar", created=None, **fields):
    """A shop with a register whose Lightning address was checked a moment ago and is fine."""
    now = timezone.now()
    values = {"lightning_address": f"{name.lower()}@example.org",
              "latitude": 1, "longitude": 2, "first_sale": ago(days=60), "lightning_status": LightningStatus.OK,
              "lightning_checked": now, "lightning_worked": now, "lightning_checked_address": f"{name.lower()}@example.org"}
    values.update(fields)
    shop = Shop.objects.create(name=name, **values)
    Shop.objects.filter(pk=shop.pk).update(created=created or ago(days=90))
    shop.refresh_from_db()
    return shop


def broken(name="Bar", days=1, **fields):
    """A shop with a register whose Lightning address stopped answering `days` days ago: a problem of the dashboard."""
    return register(name, lightning_status=LightningStatus.NO_ANSWER, lightning_since=ago(days=days), **fields)


def codes(found):
    return [(item.code, item.shop.name if item.shop else None) for item in found["items"]]


@override_settings(TELEGRAM_BOT_TOKEN="")
class Items(TestCase):
    def setUp(self):
        cache.clear()

    def test_a_site_where_everything_works_has_nothing_to_do(self):
        register("Bar")
        found = dashboard.summary()
        self.assertEqual(found["items"], [])
        self.assertEqual(found["counts"], {"working": 1, "to_look": 0})

    def test_a_broken_lightning_address_is_a_problem_since_the_first_failed_probe(self):
        since = ago(days=2)
        register("Bar", lightning_status=LightningStatus.NO_ANSWER, lightning_since=since, lightning_worked=ago(days=3))
        item = dashboard.summary()["items"][0]
        self.assertEqual((item.code, item.level, item.since), ("lightning_no_answer", dashboard.PROBLEM, since))
        self.assertEqual(item.days, 2)
        self.assertIn("stopped answering", item.what)
        self.assertIn("diagnose_shop", item.command)

    def test_an_address_that_never_confirms_payments_says_so(self):
        register("Bar", lightning_status=LightningStatus.NO_CONFIRMATION, lightning_since=ago(hours=5))
        item = dashboard.summary()["items"][0]
        self.assertEqual(item.code, "lightning_no_confirmation")
        self.assertIn("check in your wallet", item.say)

    def test_a_status_about_an_old_address_does_not_count(self):
        register("Bar", lightning_status=LightningStatus.NO_ANSWER, lightning_since=ago(days=2),
                 lightning_checked_address="old@example.org")
        self.assertNotIn("lightning_no_answer", [code for code, _ in codes(dashboard.summary())])

    def test_a_suspended_shop_is_not_counted_as_working(self):
        register("Bar", suspended=True)
        found = dashboard.summary()
        self.assertEqual(found["items"], [])
        self.assertEqual(found["counts"]["working"], 0)

    def test_only_working_addresses_count_as_working(self):
        register("Bar")
        register("Pub", lightning_status=LightningStatus.NO_ANSWER, lightning_since=ago(days=1))
        register("Inn", lightning_status="")
        self.assertEqual(dashboard.summary()["counts"]["working"], 1)

    def test_a_moved_shop_with_a_register_again_is_listed_once(self):
        owner = get_user_model().objects.create(username="k")
        register("Back", moved_in=ago(days=3), owner=owner, latitude=None)
        self.assertEqual(codes(dashboard.summary()), [("no_position", "Back")])

    def test_the_advice_for_moved_shops_does_not_remake_every_link_by_default(self):
        Shop.objects.create(name="Nobody", moved_in=ago(days=4))
        item = dashboard.summary()["items"][0]
        self.assertNotIn("--new-codes", item.do)
        self.assertIn("assign the shop", item.do)

    def test_a_shop_without_position(self):
        register("Bar", latitude=None, longitude=None)
        item = dashboard.summary()["items"][0]
        self.assertEqual((item.code, item.level), ("no_position", dashboard.INFO))
        self.assertEqual(dashboard.summary()["counts"]["to_look"], 0)

    def test_historic_shops_and_shops_without_register_are_left_out(self):
        # A historic shop keeps its register for a year after its closing day (models.REGISTER_AFTER_CLOSING)
        register("Bar", closed_on=timezone.localdate() - timedelta(days=400))
        Shop.objects.create(name="Pub")
        self.assertEqual(dashboard.summary()["shops"], 0)

    def test_moved_shops_waiting_for_the_owner_or_the_wallet(self):
        owner = get_user_model().objects.create(username="k")
        Shop.objects.create(name="Unclaimed", moved_in=ago(days=5), claim_hash="a" * 64)
        Shop.objects.create(name="Nobody", moved_in=ago(days=4))
        Shop.objects.create(name="Wallet", moved_in=ago(days=3), owner=owner, moved_register=True)
        Shop.objects.create(name="Done", moved_in=ago(days=3), owner=owner, lightning_address="k@example.org")
        moved = [found for found in codes(dashboard.summary()) if found[0].startswith("moved")]
        self.assertEqual(moved, [("moved_unclaimed", "Unclaimed"), ("moved_no_owner", "Nobody"),
                                 ("moved_no_wallet", "Wallet")])
        # The command for a lost link makes a new one for that shop only
        unclaimed = next(item for item in dashboard.summary()["items"] if item.code == "moved_unclaimed")
        self.assertIn(f"import_moved_shops <file> --only {unclaimed.shop.pk} --new-codes --apply --owners", unclaimed.command)
        self.assertIn("this shop only", unclaimed.do)

    def test_the_guardian_not_running_is_said(self):
        register("Bar", lightning_checked=ago(days=2),
                 lightning_status=LightningStatus.NO_ANSWER, lightning_since=ago(days=3))
        found = codes(dashboard.summary())
        self.assertEqual(found[0], ("lightning_no_answer", "Bar"))
        self.assertIn(("guardian_late", None), found)

    def test_problems_first_then_the_oldest(self):
        register("Old", lightning_status=LightningStatus.NO_ANSWER, lightning_since=ago(days=5))
        register("New", lightning_status=LightningStatus.NO_ANSWER, lightning_since=ago(hours=5))
        register("Nowhere", latitude=None)
        self.assertEqual([name for _, name in codes(dashboard.summary())], ["Old", "New", "Nowhere"])

    def test_the_same_queries_for_two_shops_or_forty(self):
        def queries(count):
            Shop.objects.all().delete()
            for number in range(count):
                register(f"Bar{number}", latitude=None, lightning_status=LightningStatus.NO_ANSWER,
                         lightning_since=ago(days=1))
                Shop.objects.create(name=f"Moved{number}", moved_in=ago(days=1), claim_hash=f"{number:064d}")
            with CaptureQueriesContext(connection) as done:
                found = dashboard.summary()
            self.assertEqual(len({item.shop.pk for item in found["items"] if item.shop}), 2 * count)
            return len(done)
        self.assertEqual(queries(2), queries(40))
        # The shops, the money owed by the shops no longer trading, the moved shops, the requests for help, the requests
        # about OpenStreetMap (its settings, which ask nothing, are off here), and one count for each total of the limits
        self.assertLessEqual(queries(2), 6 + len(limits.counters()))

    def test_no_other_server_is_asked(self):
        register("Bar", lightning_status=LightningStatus.NO_ANSWER, lightning_since=ago(days=1), first_sale=None,
                 created=ago(days=10))
        outside, sockets = nobody_outside()
        with outside, sockets:
            self.assertTrue(dashboard.summary()["items"])


@override_settings(TELEGRAM_BOT_TOKEN="")
class Page(TestCase):
    def setUp(self):
        cache.clear()

    def test_only_the_admin_session_opens_it(self):
        page = self.client.get("/admin/today/")
        self.assertEqual(page.status_code, 302)
        self.assertIn("/admin/login/", page["Location"])
        # A staff account signed in by link, not by the admin's password, does not get in
        self.client.force_login(get_user_model().objects.create_user("staff", is_staff=True))
        self.assertEqual(self.client.get("/admin/today/").status_code, 302)

    def test_staff_without_the_permission_to_see_shops_is_refused(self):
        from config.urls import ADMIN_SESSION

        user = get_user_model().objects.create_user("staff", is_staff=True)
        self.client.force_login(user)
        session = self.client.session
        session[ADMIN_SESSION] = True
        session.save()
        self.assertEqual(self.client.get("/admin/today/").status_code, 403)
        self.assertNotContains(self.client.get("/admin/"), "/admin/today/")
        user.user_permissions.add(Permission.objects.get(codename="view_shop"))
        self.assertEqual(self.client.get("/admin/today/").status_code, 200)

    def test_the_page_lists_what_to_do_with_its_links(self):
        admin_session(self.client)
        shop = register("Bar", lightning_status=LightningStatus.NO_ANSWER, lightning_since=ago(days=2))
        outside, sockets = nobody_outside()
        with outside, sockets:
            page = self.client.get("/admin/today/")
        self.assertContains(page, "stopped answering")
        self.assertContains(page, "2 days ago")
        self.assertContains(page, f"diagnose_shop {shop.pk}")
        self.assertContains(page, f'href="/admin/shops/shop/{shop.pk}/change/"')
        self.assertRegex(page.content.decode(), r"dashboard(\.\w+)?\.css")
        self.assertNotContains(page, "<script>")
        self.assertNotContains(page, "style=")

    def test_an_empty_site_says_there_is_nothing_to_do(self):
        admin_session(self.client)
        page = self.client.get("/admin/today/")
        self.assertContains(page, "Nothing to do")
        self.assertContains(page, "What this page does not see")

    def test_the_admin_home_links_the_page(self):
        admin_session(self.client)
        self.assertContains(self.client.get("/admin/"), 'href="/admin/today/"')


@override_settings(TELEGRAM_BOT_TOKEN="")
class Command(TestCase):
    def test_control_characters_in_names_are_not_printed(self):
        register("Bar\x1b[2J\u202eNord", lightning_status=LightningStatus.NO_ANSWER, lightning_since=ago(days=2))
        text = self.run_command("--language", "en")[1]
        self.assertIn("Bar?[2J?Nord", text)
        self.assertNotIn("\x1b", text)
        self.assertEqual(dashboard.plain("Ana 👩\u200d💻"), "Ana 👩\u200d💻")

    def run_command(self, *args):
        out = StringIO()
        try:
            call_command("dashboard", *args, stdout=out)
            code = 0
        except SystemExit as exit:
            code = exit.code
        return code, out.getvalue()

    def test_nothing_to_do_ends_with_zero(self):
        register("Bar")
        code, text = self.run_command("--language", "en")
        self.assertEqual(code, 0)
        self.assertIn("1 with a Lightning address that works", text)

    def test_something_to_do_ends_with_one(self):
        shop = register("Bar", lightning_status=LightningStatus.NO_ANSWER, lightning_since=ago(days=2))
        code, text = self.run_command("--language", "en")
        self.assertEqual(code, 2)
        self.assertIn("✗ Bar: The Lightning address stopped answering", text)
        self.assertIn(f"diagnose_shop {shop.pk}", text)

    def test_things_to_know_only_with_all(self):
        register("Bar", latitude=None)
        code, text = self.run_command("--language", "en")
        self.assertEqual(code, 0)
        self.assertNotIn("no position", text)
        self.assertIn("1 more thing to know", text)
        self.assertIn("no position", self.run_command("--language", "en", "--all")[1])


@override_settings(TELEGRAM_BOT_TOKEN="t", TELEGRAM_MODERATION_CHAT="-100", LANGUAGE_CODE="en", SITE_HOST="shops.example.org",
                   PUBLIC_URL="https://shops.example.org", NETWORK="signet")
class Send(TestCase):
    """manage.py dashboard --send, the morning message of bitmentor-dashboard@: a summary to the moderators only when
    the things to do change, or a week later, with the shops' names and nothing else about them."""

    def send(self, *args, status=200):
        out, err = StringIO(), StringIO()
        with patch("shops.notifications.requests.post") as post:
            post.return_value.status_code = status
            try:
                call_command("dashboard", "--send", *args, stdout=out, stderr=err)
                code = 0
            except SystemExit as exit:
                code = exit.code
        self.err = err.getvalue()
        return code, post, out.getvalue()

    def text(self, post):
        return post.call_args.kwargs["json"]["text"]

    def test_nothing_to_do_sends_nothing(self):
        register("Bar", latitude=None)  # only a thing to know
        code, post, out = self.send()
        self.assertEqual(code, 0)
        post.assert_not_called()
        self.assertIn("no message", out)
        self.assertFalse(DashboardDigest.objects.exists())

    def test_a_summary_with_the_names_and_the_link(self):
        owner = get_user_model().objects.create(username="owner-login", email="owner@example.org")
        register("Bar www.evil.example", owner=owner, lightning_status=LightningStatus.NO_ANSWER,
                 lightning_since=ago(days=2), lightning_address="bar@wallet.example.org",
                 lightning_checked_address="bar@wallet.example.org", email="bar@example.org",
                 phone="+39 000", address="Main Street 1")
        broken("@admin /start\x1b[2J", days=1)
        code, post, out = self.send()
        self.assertEqual(code, 10)
        self.assertEqual(post.call_args.kwargs["json"]["chat_id"], "-100")
        lines = self.text(post).splitlines()
        self.assertEqual(lines[0], "Example community (shops.example.org, signet): 2 things to do about the shops' registers")
        self.assertTrue(lines[1].startswith("✗ Bar …: The Lightning address stopped answering"), lines[1])
        # No mention, no bot command, no escape sequence from a name
        self.assertRegex(lines[2], r"^✗ \uff20admin /start\?\[2J: The Lightning address stopped answering")
        self.assertEqual(lines[-1], "What to do, and the words for the merchant: https://shops.example.org/admin/today/")
        for private in ("owner", "wallet.example.org", "bar@example.org", "+39", "Main Street", "diagnose_shop", "evil", "@"):
            self.assertNotIn(private, self.text(post))
        self.assertIn("Summary sent to TELEGRAM_MODERATION_CHAT: 2 things to do, 2 new, 0 solved.", out)

    def test_only_when_the_list_changes_and_once_a_week(self):
        """The same list the next morning: nothing. Something new: the new first. Something solved: said. The same list
        a week after the last summary: a reminder. Everything solved: nothing, and a problem afterwards is new again."""
        quiet = broken("Quiet", days=3)
        self.assertEqual(self.send()[0], 10)
        code, post, out = self.send()
        self.assertEqual(code, 0)
        post.assert_not_called()
        self.assertIn("The same 1 things to do", out)
        bar = register("Bar", lightning_status=LightningStatus.NO_ANSWER, lightning_since=ago(days=1))
        code, post, out = self.send()
        self.assertEqual(code, 10)
        self.assertEqual(self.text(post).splitlines()[1:5], [
            "New:", "✗ Bar: The Lightning address stopped answering: customers cannot pay with Lightning.",
            "Still to do:", self.text(post).splitlines()[4]])
        self.assertTrue(self.text(post).splitlines()[4].startswith("✗ Quiet: The Lightning address stopped answering"))
        Shop.objects.filter(pk=quiet.pk).update(lightning_status=LightningStatus.OK, lightning_since=None)
        code, post, out = self.send()
        self.assertEqual(code, 10)
        self.assertIn("1 thing solved since the last summary.", self.text(post))
        self.assertNotIn("New:", self.text(post))
        self.assertEqual(self.send()[0], 0)
        DashboardDigest.objects.update(sent=ago(days=7, minutes=1))
        code, post, out = self.send()
        self.assertEqual(code, 10)
        self.assertIn("Nothing new since the summary of", self.text(post))
        self.assertEqual(self.send()[0], 0)
        Shop.objects.filter(pk=bar.pk).update(lightning_status=LightningStatus.OK, lightning_since=None)
        code, post, out = self.send()
        self.assertEqual((code, post.called), (0, False))
        self.assertEqual(DashboardDigest.objects.get().items, [])
        Shop.objects.filter(pk=bar.pk).update(lightning_status=LightningStatus.NO_ANSWER, lightning_since=ago(hours=1))
        code, post, out = self.send()
        self.assertEqual(code, 10)
        self.assertNotIn("New:", self.text(post))  # all new, no headings
        self.assertEqual(DashboardDigest.objects.count(), 1)

    def test_a_long_list_is_cut_the_new_first(self):
        for number in range(8):
            broken(f"Shop {number:02}", days=40 + number)
        self.send()
        for number in range(8, 12):
            broken(f"Shop {number:02}", days=40 + number)
        lines = self.text(self.send()[1]).splitlines()
        self.assertEqual(lines[1], "New:")
        self.assertEqual([line[2:9] for line in lines[2:6]], ["Shop 11", "Shop 10", "Shop 09", "Shop 08"])
        self.assertEqual(lines[6], "Still to do:")
        self.assertIn("…and 2 more", lines)
        self.assertEqual(len([line for line in lines if line.startswith("✗ ")]), 10)

    @override_settings(TELEGRAM_BOT_TOKEN="")
    def test_without_telegram_the_community_email(self):
        from django.core import mail

        broken()
        with patch("shops.management.commands.dashboard.community", return_value={"name": "Shops", "email": "info@example.org"}):
            code, post, out = self.send()
        self.assertEqual(code, 10)
        post.assert_not_called()
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["info@example.org"])
        self.assertEqual(mail.outbox[0].subject, "Shops (shops.example.org, signet): 1 thing to do about the shops' registers")
        self.assertIn("/admin/today/", mail.outbox[0].body)
        DashboardDigest.objects.all().delete()
        with patch("shops.management.commands.dashboard.community", return_value={"name": "Shops"}):
            with self.assertRaisesMessage(CommandError, "no `email` in community.yml"):
                call_command("dashboard", "--send", stdout=StringIO())

    def test_telegram_refusing_it_falls_back_on_the_email_or_fails(self):
        """A wrong chat or a bot thrown out of it (400, 403) is no delivery: the email instead, or an error, and the
        summary is not remembered as sent, so the next run tries again."""
        from django.core import mail

        broken()
        for status in (400, 401, 403):
            with self.subTest(status=status):
                mail.outbox.clear()
                DashboardDigest.objects.all().delete()
                with patch("shops.management.commands.dashboard.community",
                           return_value={"name": "Shops", "email": "info@example.org"}):
                    code, post, out = self.send(status=status)
                self.assertEqual(code, 10)
                self.assertEqual(len(mail.outbox), 1)
                self.assertIn("Telegram refused it", self.err)
                self.assertIn("Summary sent to the community's email", out)
        DashboardDigest.objects.all().delete()
        with patch("shops.management.commands.dashboard.community", return_value={"name": "Shops"}):
            with self.assertRaisesMessage(CommandError, "Telegram refused it"):
                self.send(status=403)
        self.assertFalse(DashboardDigest.objects.exists())

    def test_telegram_down_is_an_error_of_the_command(self):
        broken()
        with patch("shops.notifications.requests.post", side_effect=requests.ConnectionError("https://api/bott/x")), \
                patch("shops.management.commands.dashboard.community", return_value={"name": "Shops"}):
            with self.assertRaises(CommandError) as failed:
                call_command("dashboard", "--send", stdout=StringIO())
        self.assertNotIn("bott", str(failed.exception))
        self.assertFalse(DashboardDigest.objects.exists())

    def test_italian(self):
        broken()
        text = self.text(self.send("--language", "it")[1])
        self.assertIn("1 cosa da fare sulle casse dei negozi", text.splitlines()[0])
        self.assertIn("L'indirizzo Lightning ha smesso di rispondere", text)

    @override_settings(NETWORK="mainnet")
    def test_on_mainnet_the_host_alone(self):
        broken()
        self.assertTrue(self.text(self.send()[1]).startswith("Example community (shops.example.org): 1 thing"))

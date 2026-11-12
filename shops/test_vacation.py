"""A shop closed for a while (shops/vacation.py): the days, the edges (starts today, ends today, time zone, summer time),
the notice on the map, in the JSON, on the shop page and on Nostr, the icons that count only open days, the
certificate that does not move, no alarms from the dashboard and the guardian, the welcome back, and the owner's pages.

Every day here is built from the community's calendar (`today()`), never as `now - timedelta(days=n)`: the tests give
the same answer at any hour, and across the change of summer time."""
from datetime import date, datetime, time, timedelta
from datetime import timezone as dt_timezone
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import override

from . import certified, dashboard, lightning_watch, nostr, vacation
from .models import Closure, LightningStatus, Profile, Shop
from .tests import steady_limits

SECRET = "11" * 32


def today():
    return vacation.day_of()


def days(number):
    return timedelta(days=number)


def at(day, hour=12):
    """A moment of the community's calendar: `hour` o'clock of `day` in the community's time zone."""
    return timezone.make_aware(datetime.combine(day, time(hour)), timezone.get_default_timezone())


def make(shop, first, back=None, reason="holiday", **fields):
    return Closure.objects.create(shop=shop, first_day=first, reopens_on=back, reason=reason, **fields)


def fresh(shop):
    return Shop.objects.prefetch_related("closures").get(pk=shop.pk)


class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = get_user_model().objects.create_user("merchant")
        self.shop = Shop.objects.create(name="Bar", owner=self.owner, 
                                        lightning_address="bar@example.org", latitude=41.9, longitude=12.5,
                                        lightning=True, first_sale=timezone.now())

    def shop_now(self):
        return fresh(self.shop)


class Days(Base):
    def test_a_closure_covers_its_days_and_not_the_day_it_opens(self):
        make(self.shop, today() - days(2), today() + days(3))
        shop = self.shop_now()
        for offset, closed in ((-3, False), (-2, True), (0, True), (2, True), (3, False), (4, False)):
            self.assertIs(vacation.is_closed(shop, today() + days(offset)), closed, offset)

    def test_a_closure_that_starts_today_is_closed_today(self):
        make(self.shop, today(), today() + days(1))
        shop = self.shop_now()
        self.assertTrue(vacation.is_closed(shop))
        self.assertFalse(vacation.is_closed(shop, today() - days(1)))
        self.assertFalse(vacation.is_closed(shop, today() + days(1)))

    def test_a_closure_that_ends_today_is_open_today(self):
        make(self.shop, today() - days(5), today())
        shop = self.shop_now()
        self.assertFalse(vacation.is_closed(shop))
        self.assertTrue(vacation.is_closed(shop, today() - days(1)))

    def test_until_i_say_so_has_no_end(self):
        make(self.shop, today() - days(400), None)
        self.assertTrue(vacation.is_closed(self.shop_now(), today() + days(1000)))

    def test_a_historic_shop_is_closed_for_good_not_for_a_while(self):
        make(self.shop, today(), today() + days(3))
        Shop.objects.filter(pk=self.shop.pk).update(closed_on=today() - days(1))
        shop = self.shop_now()
        self.assertFalse(vacation.is_closed(shop))
        self.assertIsNone(vacation.info(shop))

    def test_the_day_is_the_communitys_not_utc(self):
        # 22:30 UTC on 24 October is half past midnight on 25 October in Rome (summer time), and 23:30 UTC on the 25th is
        # half past midnight on the 26th (summer time ended at 3:00)
        self.assertEqual(vacation.day_of(datetime(2026, 10, 24, 22, 30, tzinfo=dt_timezone.utc)), date(2026, 10, 25))
        self.assertEqual(vacation.day_of(datetime(2026, 10, 24, 21, 30, tzinfo=dt_timezone.utc)), date(2026, 10, 24))
        self.assertEqual(vacation.day_of(datetime(2026, 10, 25, 22, 30, tzinfo=dt_timezone.utc)), date(2026, 10, 25))
        self.assertEqual(vacation.day_of(datetime(2026, 10, 25, 23, 30, tzinfo=dt_timezone.utc)), date(2026, 10, 26))

    def test_summer_time_does_not_move_the_days_that_count(self):
        # The closure covers the night the clocks go back: 25 hours are one day, and the days are counted as dates
        make(self.shop, date(2026, 10, 24), date(2026, 10, 27))
        shop = self.shop_now()
        self.assertEqual(vacation.closed_days(shop, date(2026, 10, 20), date(2026, 11, 1)), 3)
        self.assertEqual(vacation.open_days(shop, date(2026, 10, 23), date(2026, 10, 28)), 5 - 3)
        self.assertTrue(vacation.is_closed(shop, date(2026, 10, 25)))
        self.assertFalse(vacation.is_closed(shop, date(2026, 10, 27)))

    def test_closed_days_are_counted_once(self):
        make(self.shop, date(2026, 8, 1), date(2026, 8, 11))
        make(self.shop, date(2026, 8, 6), date(2026, 8, 16))
        shop = self.shop_now()
        self.assertEqual(vacation.closed_days(shop, date(2026, 7, 1), date(2026, 9, 1)), 15)
        # Only the part inside the stretch
        self.assertEqual(vacation.closed_days(shop, date(2026, 8, 10), date(2026, 8, 13)), 3)
        make(self.shop, date(2026, 1, 1), None)
        self.assertEqual(vacation.closed_days(self.shop_now(), date(2026, 1, 1), date(2026, 11, 1)), 304)

    def test_the_forgiven_days_are_a_budget_of_the_last_365_days_the_first_in_time(self):
        make(self.shop, today() - days(500), today() - days(420))   # older than the window: no budget spent
        make(self.shop, today() - days(200), today() - days(150))   # 50 days
        make(self.shop, today() - days(100), today() - days(40))    # 60 days: only 42 are forgiven
        shop = self.shop_now()
        found = vacation.forgiven(shop, today())
        self.assertEqual(sum((last - first).days for first, last in found), vacation.MAX_CREDIT)
        self.assertEqual(found[0], (today() - days(200), today() - days(150)))
        self.assertEqual(found[1], (today() - days(100), today() - days(100) + days(42)))

    def test_overlapping_closures_say_the_latest_end_and_one_tap_ends_them_all(self):
        make(self.shop, today() - days(10), today() + days(30), "works")
        make(self.shop, today() - days(1), today() + days(2), "holiday")
        shop = self.shop_now()
        self.assertEqual(vacation.current(shop).reopens_on, today() + days(30))
        self.assertEqual(vacation.info(shop)["back"], today() + days(30))
        self.assertEqual(vacation.reopen_today(self.shop, today()), "reopened")
        self.assertFalse(vacation.is_closed(self.shop_now()))
        self.assertEqual(sorted(c.reopens_on for c in Closure.objects.all()), [today(), today()])

    def test_closures_one_after_the_other_are_one_for_the_three_months(self):
        make(self.shop, today() - days(60), today() - days(30))
        make(self.shop, today() - days(30), None)
        self.assertEqual(vacation.long_closure(self.shop_now()), None)
        Closure.objects.all().delete()
        make(self.shop, today() - days(100), today() - days(50))
        make(self.shop, today() - days(50), None)
        self.assertEqual(vacation.long_closure(self.shop_now()), today() - days(100))
        # With a day open between them they are two
        Closure.objects.all().delete()
        make(self.shop, today() - days(100), today() - days(51))
        make(self.shop, today() - days(50), None)
        self.assertIsNone(vacation.long_closure(self.shop_now()))

    def test_open_days_never_below_zero_and_a_payment_today_is_zero(self):
        make(self.shop, today() - days(3), None)
        shop = self.shop_now()
        self.assertEqual(vacation.open_days(shop, today(), today()), 0)
        self.assertEqual(vacation.open_days(shop, today() - days(3), today()), 0)
        self.assertEqual(vacation.open_days(shop, today() - days(5), today()), 1)

    def test_a_shop_not_saved_has_no_closures(self):
        self.assertEqual(vacation.closures(Shop(name="New")), [])


class Notice(Base):
    def test_closed_with_a_day_to_open(self):
        make(self.shop, today(), today() + days(15), "holiday")
        with override(language="en"):
            self.assertEqual(vacation.note(self.shop_now()),
                             f"Closed for holidays · back on {vacation.long_date(today() + days(15))}")

    def test_the_reasons(self):
        for reason, words in (("works", "Closed for works"), ("personal", "Closed for personal reasons"),
                              ("other", "Closed for a while")):
            Closure.objects.all().delete()
            make(self.shop, today(), today() + days(2), reason)
            with override(language="en"):
                self.assertTrue(vacation.note(self.shop_now()).startswith(words), reason)

    def test_until_i_say_so_says_back_soon(self):
        make(self.shop, today(), None, "other")
        with override(language="en"):
            self.assertEqual(vacation.note(self.shop_now()), "Closed for a while · back soon")

    def test_a_closure_to_come_is_said_a_week_before_not_earlier(self):
        make(self.shop, today() + days(vacation.SOON_DAYS), today() + days(20))
        info = vacation.info(self.shop_now())
        self.assertEqual(info["state"], "soon")
        with override(language="en"):
            self.assertIn("from", vacation.note(self.shop_now()))
        Closure.objects.all().delete()
        make(self.shop, today() + days(vacation.SOON_DAYS + 1), today() + days(20))
        self.assertIsNone(vacation.info(self.shop_now()))

    def test_italian_and_german_have_the_words(self):
        make(self.shop, today(), today() + days(15))
        shop = self.shop_now()
        with override(language="it"):
            self.assertTrue(vacation.note(shop).startswith("Chiuso per ferie"), vacation.note(shop))
        with override(language="de"):
            self.assertNotIn("Closed", vacation.note(shop))

    def test_the_json_the_map_and_the_shop_page_say_it(self):
        make(self.shop, today(), today() + days(15), "works")
        found = self.client.get(reverse("shops_json")).json()["shops"][0]
        self.assertEqual(found["break"], {"state": "closed", "reason": "works", "from": today().isoformat(),
                                          "back": (today() + days(15)).isoformat()})
        # The shop stays on the map, with its pin and its discount
        self.assertEqual(found["closed_on"], None)
        page = self.client.get(reverse("shop_map")).content.decode()
        self.assertIn('class="break-note"', page)
        self.assertIn('"closed_back"', page)
        shop_page = self.client.get(reverse("shop_detail", args=[self.shop.pk])).content.decode()
        self.assertIn('class="notice shop-break"', shop_page)

    def test_an_open_shop_says_nothing(self):
        found = self.client.get(reverse("shops_json")).json()["shops"][0]
        self.assertIsNone(found["break"])
        self.assertNotIn('class="break-note"', self.client.get(reverse("shop_map")).content.decode())
        self.assertNotIn("shop-break", self.client.get(reverse("shop_detail", args=[self.shop.pk])).content.decode())

    def test_the_json_never_has_a_free_text(self):
        make(self.shop, today(), None, "personal")
        found = self.client.get(reverse("shops_json")).json()["shops"][0]["break"]
        self.assertEqual(sorted(found), ["back", "from", "reason", "state"])

    def test_nostr_gets_the_reason_and_the_day(self):
        make(self.shop, today(), today() + days(3), "holiday")
        event = nostr.shop_event(SECRET, self.shop_now(), "https://example.org/s/1")
        self.assertIn(["break", "holiday", (today() + days(3)).isoformat()], event["tags"])
        Closure.objects.all().delete()
        event = nostr.shop_event(SECRET, self.shop_now(), "https://example.org/s/1")
        self.assertFalse([tag for tag in event["tags"] if tag[0] == "break"])

    def test_the_map_asks_the_same_queries_for_two_shops_or_forty(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        def queries(count):
            Shop.objects.exclude(pk=self.shop.pk).delete()
            for number in range(count):
                other = Shop.objects.create(name=f"B{number}", latitude=1, longitude=2)
                make(other, today(), today() + days(3))
            with CaptureQueriesContext(connection) as done:
                self.client.get(reverse("shops_json"))
                self.client.get(reverse("shop_map"))
            return len(done)
        self.assertEqual(queries(2), queries(12))


class Icons(Base):
    def paid(self, worked_day, **fields):
        """The Lightning address last worked on `worked_day`: the icon counts from there."""
        Shop.objects.filter(pk=self.shop.pk).update(lightning_worked=at(worked_day), **fields)
        return self.shop_now()

    def band(self, shop):
        return certified.bands(shop, at(today(), 23))["lightning"]

    def test_a_fortnight_of_holidays_does_not_fade_the_icons(self):
        shop = self.paid(today() - days(20))
        self.assertEqual(self.band(shop), certified.MONTH)
        make(self.shop, today() - days(16), today() - days(2))
        # 20 days passed, 14 of them closed: 6 open days
        self.assertEqual(self.band(self.shop_now()), certified.WEEK)

    def test_closed_right_now_the_icons_wait(self):
        self.paid(today() - days(10))
        make(self.shop, today() - days(5), None)
        # 10 days passed, 6 of them closed (today included)
        self.assertEqual(self.band(self.shop_now()), certified.WEEK)

    def test_a_closure_before_the_payment_changes_nothing(self):
        make(self.shop, today() - days(60), today() - days(40))
        self.paid(today() - days(20))
        self.assertEqual(self.band(self.shop_now()), certified.MONTH)

    def test_a_payment_does_not_give_a_new_budget(self):
        # 90 days closed, a payment, then 40 days closed: only 2 of those 40 are forgiven, the budget is of the year
        make(self.shop, today() - days(150), today() - days(60))
        self.paid(today() - days(55))
        make(self.shop, today() - days(50), today() - days(10))
        # 55 days passed since the payment, 2 of them forgiven: 53 open days
        self.assertEqual(vacation.open_days(self.shop_now(), today() - days(55), today()), 53)
        self.assertEqual(self.band(self.shop_now()), certified.YEAR)

    def test_a_closure_older_than_the_year_spends_no_budget(self):
        make(self.shop, today() - days(500), today() - days(420))
        self.paid(today() - days(20))
        make(self.shop, today() - days(16), today() - days(2))
        self.assertEqual(self.band(self.shop_now()), certified.WEEK)

    def test_a_closure_that_never_ends_does_not_keep_the_icons_for_ever(self):
        self.paid(today() - days(300))
        make(self.shop, today() - days(299), None)
        # 300 passed, at most 92 forgiven: 208 open days, a year band, not the week
        self.assertEqual(self.band(self.shop_now()), certified.YEAR)

    def test_the_bands_of_an_open_shop_are_as_before(self):
        for age, expected in ((0, certified.WEEK), (7, certified.WEEK), (8, certified.MONTH), (31, certified.MONTH),
                              (32, certified.YEAR), (366, certified.YEAR), (367, certified.OLDER)):
            self.assertEqual(self.band(self.paid(today() - days(age))), expected, age)

    def test_the_same_shop_in_memory_without_a_prefetch_gives_the_same_bands(self):
        self.paid(today() - days(20))
        make(self.shop, today() - days(16), today() - days(2))
        plain = Shop.objects.get(pk=self.shop.pk)
        self.assertEqual(self.band(plain), self.band(self.shop_now()))


class Certificate(Base):
    """The rule does not move for a closure: nobody is asked less, and nobody loses what was earned."""

    def test_a_certified_shop_stays_certified_through_a_closure(self):
        Shop.objects.filter(pk=self.shop.pk).update(certified_month=date(2026, 6, 1))
        make(self.shop, today() - days(40), today() - days(1))
        self.assertEqual(self.shop_now().certified_month, date(2026, 6, 1))
        self.assertTrue(certified.owns_certified_shop(self.owner))

class Alarms(Base):
    def setUp(self):
        super().setUp()
        now = timezone.now()
        Shop.objects.filter(pk=self.shop.pk).update(
            lightning_status=LightningStatus.NO_ANSWER, lightning_since=now - timedelta(hours=30),
            lightning_checked=now, lightning_worked=now - timedelta(days=3), lightning_checked_address="bar@example.org",
            first_sale=now - timedelta(days=100))
        Shop.objects.filter(pk=self.shop.pk).update(created=now - timedelta(days=200))

    def codes(self):
        return {item.code for item in dashboard.summary()["items"] if item.shop and item.shop.pk == self.shop.pk}

    def test_without_a_closure_the_fault_is_there(self):
        self.assertIn("lightning_no_answer", self.codes())

    def test_closed_today_no_alarm_for_the_address(self):
        make(self.shop, today(), today() + days(2))
        # Only a line to know (never in the morning message): the address is broken, and the shop is closed
        self.assertEqual(self.codes(), {"closed_lightning"})
        found = dashboard.summary()
        self.assertEqual(found["closed"], 1)
        self.assertEqual(found["counts"], {"working": 0, "to_look": 0})

    def test_a_closed_shop_with_a_broken_address_has_a_line_to_know_and_no_alarm(self):
        make(self.shop, today(), today() + days(2))
        found = [item for item in dashboard.summary()["items"] if item.shop and item.shop.pk == self.shop.pk]
        self.assertEqual([(item.code, item.level) for item in found], [("closed_lightning", dashboard.INFO)])

    def test_the_day_it_opens_again_the_fault_is_back_on_the_list(self):
        make(self.shop, today() - days(5), today())
        self.assertIn("lightning_no_answer", self.codes())

    def test_after_three_months_closed_the_association_is_asked_whether_it_is_coming_back(self):
        make(self.shop, today() - days(vacation.MAX_CREDIT), None)
        self.assertNotIn("closed_long", self.codes())
        Closure.objects.all().delete()
        make(self.shop, today() - days(vacation.MAX_CREDIT + 1), None)
        self.assertEqual(self.codes(), {"closed_long", "closed_lightning"})
        item = [i for i in dashboard.summary()["items"] if i.code == "closed_long"][0]
        self.assertEqual(item.level, dashboard.INFO)
        self.assertEqual(item.days, vacation.MAX_CREDIT + 1)

    def test_the_dashboard_asks_the_same_queries_for_two_shops_or_forty_with_closures(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        def queries(count):
            Shop.objects.exclude(pk=self.shop.pk).delete()
            for number in range(count):
                other = Shop.objects.create(name=f"B{number}", 
                                            lightning_address=f"b{number}@example.org", latitude=1, longitude=2)
                make(other, today(), today() + days(3))
            with CaptureQueriesContext(connection) as done:
                dashboard.summary()
            return len(done)
        self.assertEqual(queries(2), queries(10))


class Guardian(Base):
    def broken_once(self):
        now = timezone.now()
        Shop.objects.filter(pk=self.shop.pk).update(
            lightning_status=LightningStatus.OK, lightning_since=now - timedelta(days=2), lightning_checked=now,
            lightning_worked=now - timedelta(hours=3), lightning_failures=1,
            lightning_failing_since=now - timedelta(hours=2), lightning_checked_address="bar@example.org")

    def test_a_fault_is_told_when_the_shop_is_open(self):
        self.broken_once()
        _shop, change, recorded = lightning_watch.record(self.shop.pk, "bar@example.org", lightning_watch.NO_ANSWER_STATUS)
        self.assertEqual((change, recorded), (lightning_watch.BROKE, True))

    def test_a_fault_during_a_closure_is_kept_on_the_shop_and_not_told(self):
        self.broken_once()
        make(self.shop, today(), today() + days(5))
        shop, change, recorded = lightning_watch.record(self.shop.pk, "bar@example.org", lightning_watch.NO_ANSWER_STATUS)
        self.assertEqual((change, recorded), (None, True))
        # The guardian keeps watching: the state is on the shop, the panel shows it
        shop.refresh_from_db()
        self.assertEqual(shop.lightning_status, LightningStatus.NO_ANSWER)
        self.assertTrue(lightning_watch.panel(shop)["broken"])

    def test_the_command_tells_nobody_during_a_closure(self):
        self.broken_once()
        make(self.shop, today(), today() + days(5))
        Profile.objects.update_or_create(user=self.owner, defaults={"email": "merchant@example.org"})
        with patch.object(lightning_watch, "watch_probe", return_value=lightning_watch.NO_ANSWER_STATUS), \
                patch.object(lightning_watch, "network_ok", return_value=True), patch.object(lightning_watch, "tell_association") as told:
            call_command("check_lightning", pause=0, all=True, stdout=StringIO())
        self.assertEqual(mail.outbox, [])
        told.assert_called_once()
        self.assertEqual(told.call_args.args[0], [])


@override_settings(TELEGRAM_BOT_TOKEN="", TELEGRAM_MODERATION_CHAT="")
class Welcome(Base):
    def setUp(self):
        super().setUp()
        Profile.objects.update_or_create(user=self.owner, defaults={"email": "merchant@example.org"})

    def morning(self, day=None, hour=9):
        return at(day or today(), hour)

    def test_the_owner_is_told_the_morning_the_shop_opens_again(self):
        closure = make(self.shop, today() - days(14), today())
        told = vacation.welcome_back(self.morning())
        self.assertEqual([shop.pk for shop in told], [self.shop.pk])
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["merchant@example.org"])
        self.assertIn("small payment", mail.outbox[0].body)
        closure.refresh_from_db()
        self.assertIsNotNone(closure.welcomed)
        # Once
        self.assertEqual(vacation.welcome_back(self.morning(hour=10)), [])
        self.assertEqual(len(mail.outbox), 1)

    def test_not_before_eight_in_the_morning(self):
        make(self.shop, today() - days(14), today())
        self.assertEqual(vacation.welcome_back(self.morning(hour=7)), [])
        self.assertEqual(mail.outbox, [])
        self.assertEqual(len(vacation.welcome_back(self.morning(hour=8))), 1)

    def test_a_run_that_was_missed_is_made_up_for_a_few_days_not_for_ever(self):
        make(self.shop, today() - days(20), today() - days(2))
        self.assertEqual(len(vacation.welcome_back(self.morning())), 1)
        Closure.objects.all().delete()
        make(self.shop, today() - days(30), today() - days(10))
        self.assertEqual(vacation.welcome_back(self.morning()), [])

    def test_a_closure_still_on_or_to_come_is_not_welcomed(self):
        make(self.shop, today() - days(3), today() + days(1))
        make(self.shop, today() + days(10), today() + days(12))
        self.assertEqual(vacation.welcome_back(self.morning()), [])

    def test_closed_again_the_same_day_there_is_nothing_to_say(self):
        make(self.shop, today() - days(14), today())
        make(self.shop, today(), None)
        self.assertEqual(vacation.welcome_back(self.morning()), [])

    def test_the_fault_found_meanwhile_is_said_to_the_owner_and_the_association(self):
        now = timezone.now()
        Shop.objects.filter(pk=self.shop.pk).update(
            lightning_status=LightningStatus.NO_ANSWER, lightning_since=now - timedelta(days=3), lightning_checked=now,
            lightning_worked=now - timedelta(days=5), lightning_checked_address="bar@example.org")
        make(self.shop, today() - days(14), today())
        with patch.object(lightning_watch, "tell_association") as told:
            vacation.welcome_back(self.morning())
        body = mail.outbox[0].body
        self.assertIn("Lightning address does not work now", body)
        # The register did work, but the address did not: no "worked while you were away", no "while you were away" at all
        self.assertNotIn("while you were away", body)
        self.assertIn("When it works again, try a small payment", body)
        told.assert_called_once()
        self.assertEqual(told.call_args.args[0][0][1], lightning_watch.BROKE)

    def test_the_merchants_telegram_chat_hears_it_too_and_a_refusal_is_no_error(self):
        Shop.objects.filter(pk=self.shop.pk).update(telegram_chat="12345")
        make(self.shop, today() - days(14), today())
        with patch("shops.notifications.telegram") as sent:
            vacation.welcome_back(self.morning())
        sent.assert_called_once()
        self.assertEqual(sent.call_args.args[0], "12345")
        self.assertIn("small payment", sent.call_args.args[1])
        # The chat is the owner's choice and can be anyone's, the sender the community's bot: the name goes as the alerts
        # write it, without a web address or a mention
        Shop.objects.filter(pk=self.shop.pk).update(name="Bar www.example.net @someone")
        Closure.objects.all().delete()
        make(self.shop, today() - days(14), today())
        with patch("shops.notifications.telegram") as sent:
            vacation.welcome_back(self.morning())
        self.assertNotIn("www.example.net", sent.call_args.args[1])
        self.assertNotIn("@someone", sent.call_args.args[1])
        from .notifications import NotificationFailed

        Closure.objects.all().delete()
        make(self.shop, today() - days(14), today())
        with patch("shops.notifications.telegram", side_effect=NotificationFailed("500")):
            self.assertEqual(len(vacation.welcome_back(self.morning())), 1)

    def test_the_message_does_not_say_today_when_it_is_late(self):
        make(self.shop, today() - days(20), today() - days(2))
        vacation.welcome_back(self.morning())
        self.assertNotIn("today", mail.outbox[0].body)
        self.assertIn("is open again.", mail.outbox[0].body)

    def test_an_owner_who_reopens_by_hand_is_not_written_to(self):
        make(self.shop, today() - days(3), None)
        self.assertEqual(vacation.reopen_today(self.shop, today()), "reopened")
        self.assertEqual(vacation.welcome_back(self.morning()), [])
        self.assertEqual(mail.outbox, [])

    def test_the_card_in_the_panel_asks_for_a_small_payment_then_says_the_address_works(self):
        closure = make(self.shop, today() - days(10), today() - days(1))
        shop = self.shop_now()
        found = vacation.welcome(shop)
        self.assertEqual((found["closure"].pk, found["works"]), (closure.pk, False))
        # An address that worked on a day the shop was closed says nothing about the reopening
        Shop.objects.filter(pk=self.shop.pk).update(lightning_worked=at(today() - days(3)))
        self.assertFalse(vacation.welcome(self.shop_now())["works"])
        Shop.objects.filter(pk=self.shop.pk).update(lightning_worked=at(today() - days(1)))
        self.assertTrue(vacation.welcome(self.shop_now())["works"])
        self.client.force_login(self.owner)
        page = self.client.get(reverse("shops")).content.decode()
        self.assertIn("after you opened again: it works", page)

    def test_the_card_goes_when_the_owner_closes_it_or_after_two_weeks(self):
        closure = make(self.shop, today() - days(20), today() - days(vacation.WELCOME_DAYS))
        self.assertIsNotNone(vacation.welcome(self.shop_now()))
        Closure.objects.filter(pk=closure.pk).update(reopens_on=today() - days(vacation.WELCOME_DAYS + 1))
        self.assertIsNone(vacation.welcome(self.shop_now()))
        Closure.objects.filter(pk=closure.pk).update(reopens_on=today() - days(2))
        self.client.force_login(self.owner)
        page = self.client.get(reverse("shops")).content.decode()
        self.assertIn("Welcome back!", page)
        response = self.client.post(reverse("break_seen", args=[self.shop.pk]), {"closure": closure.pk})
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("Welcome back!", self.client.get(reverse("shops")).content.decode())

    def test_a_closure_of_someone_else_cannot_be_closed(self):
        closure = make(self.shop, today() - days(5), today() - days(1))
        other = get_user_model().objects.create_user("other")
        self.client.force_login(other)
        self.assertEqual(self.client.post(reverse("break_seen", args=[self.shop.pk]), {"closure": closure.pk}).status_code, 404)
        closure.refresh_from_db()
        self.assertIsNone(closure.seen)


class Pages(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner)
        self.url = reverse("shop_break", args=[self.shop.pk])

    def send(self, follow=False, **values):
        data = {"reason": "holiday", "first_day": today().isoformat()}
        data.update(values)
        return self.client.post(self.url, {key: value for key, value in data.items() if value is not None}, follow=follow)

    def test_the_page_opens_and_has_the_form(self):
        page = self.client.get(self.url).content.decode()
        for name in ("reason", "first_day", "reopens_on", "until_told"):
            self.assertIn(f'name="{name}"', page)
        self.assertEqual(self.client.get(self.url)["X-Robots-Tag"], "noindex, nofollow")

    def test_close_from_today_to_a_day(self):
        response = self.send(reopens_on=(today() + days(14)).isoformat(), reason="works")
        self.assertRedirects(response, f"{reverse('shops')}#break-{self.shop.pk}", fetch_redirect_response=False)
        closure = Closure.objects.get()
        self.assertEqual((closure.first_day, closure.reopens_on, closure.reason), (today(), today() + days(14), "works"))

    def test_until_i_say_so(self):
        self.send(until_told="on")
        self.assertIsNone(Closure.objects.get().reopens_on)
        # With a day too: the tick wins
        Closure.objects.all().delete()
        self.send(until_told="on", reopens_on=(today() + days(3)).isoformat())
        self.assertIsNone(Closure.objects.get().reopens_on)

    def test_the_form_refuses_what_makes_no_sense(self):
        for values in ({"first_day": (today() - days(1)).isoformat(), "reopens_on": (today() + days(3)).isoformat()},
                       {"reopens_on": today().isoformat()},
                       {"reopens_on": (today() - days(2)).isoformat()},
                       {"reopens_on": None},
                       {"reopens_on": (today() + days(vacation.LONGEST + 5)).isoformat()},
                       {"reopens_on": "tomorrow"},
                       {"reason": "vacation-at-the-beach", "reopens_on": (today() + days(3)).isoformat()},
                       {"first_day": None, "reopens_on": (today() + days(3)).isoformat()}):
            response = self.send(**values)
            self.assertEqual(response.status_code, 200, values)
            self.assertEqual(Closure.objects.count(), 0, values)

    def test_closing_to_come_can_be_shortened_lengthened_and_cancelled(self):
        start = today() + days(10)
        self.send(first_day=start.isoformat(), reopens_on=(start + days(7)).isoformat())
        closure = Closure.objects.get()
        # The form edits the same closure
        self.send(first_day=(start + days(1)).isoformat(), reopens_on=(start + days(20)).isoformat(), reason="other")
        closure = Closure.objects.get()
        self.assertEqual((closure.first_day, closure.reopens_on, closure.reason),
                         (start + days(1), start + days(20), "other"))
        response = self.client.post(reverse("break_over", args=[self.shop.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Closure.objects.count(), 0)

    def test_a_closure_in_course_keeps_its_first_day_and_can_end_early_or_late(self):
        make(self.shop, today() - days(3), today() + days(5))
        # A first day sent by hand does not move a closure that already started
        self.send(first_day=(today() + days(1)).isoformat(), reopens_on=(today() + days(10)).isoformat())
        closure = Closure.objects.get()
        self.assertEqual((closure.first_day, closure.reopens_on), (today() - days(3), today() + days(10)))
        # Opening again today from the form
        self.send(reopens_on=today().isoformat())
        closure.refresh_from_db()
        self.assertEqual(closure.reopens_on, today())
        self.assertFalse(vacation.is_closed(self.shop_now()))

    def test_i_am_back_today_ends_the_closure_and_a_closure_that_starts_today_is_dropped(self):
        make(self.shop, today() - days(3), None)
        self.client.post(reverse("break_over", args=[self.shop.pk]))
        closure = Closure.objects.get()
        self.assertEqual(closure.reopens_on, today())
        self.assertIsNotNone(closure.welcomed)
        Closure.objects.all().delete()
        make(self.shop, today(), None)
        self.client.post(reverse("break_over", args=[self.shop.pk]))
        self.assertEqual(Closure.objects.count(), 0)

    def test_closed_from_today_until_i_say_so_is_one_tap(self):
        self.client.post(reverse("break_now", args=[self.shop.pk]))
        closure = Closure.objects.get()
        self.assertEqual((closure.first_day, closure.reopens_on, closure.reason), (today(), None, "other"))
        # Twice: still one closure
        self.client.post(reverse("break_now", args=[self.shop.pk]))
        self.assertEqual(Closure.objects.count(), 1)

    def test_only_the_owner_logged_in_and_only_by_post(self):
        other = Client()
        self.assertEqual(other.get(self.url).status_code, 302)
        self.assertEqual(other.post(reverse("break_now", args=[self.shop.pk])).status_code, 302)
        stranger = get_user_model().objects.create_user("other")
        other.force_login(stranger)
        self.assertEqual(other.get(self.url).status_code, 404)
        self.assertEqual(other.post(reverse("break_now", args=[self.shop.pk])).status_code, 404)
        self.assertEqual(other.post(reverse("break_over", args=[self.shop.pk])).status_code, 404)
        self.assertEqual(Closure.objects.count(), 0)
        for name in ("break_now", "break_over", "break_seen"):
            self.assertEqual(self.client.get(reverse(name, args=[self.shop.pk])).status_code, 405, name)

    def test_the_token_is_needed(self):
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.owner)
        self.assertEqual(strict.post(reverse("break_now", args=[self.shop.pk])).status_code, 403)
        self.assertEqual(strict.post(self.url, {"reason": "holiday", "until_told": "on"}).status_code, 403)
        self.assertEqual(Closure.objects.count(), 0)

    def test_a_historic_shop_or_one_waiting_for_its_wallet_has_no_such_page(self):
        Shop.objects.filter(pk=self.shop.pk).update(closed_on=today() - days(1))
        self.assertEqual(self.client.get(self.url).status_code, 404)
        Shop.objects.filter(pk=self.shop.pk).update(closed_on=None, lightning_address="", awaiting_wallet=True)
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_too_many_changes_in_a_short_time_are_stopped(self):
        self.enterContext(steady_limits())
        with patch("shops.vacation_views.CHANGES_PER_USER", 2):
            codes = [self.client.post(reverse("break_now", args=[self.shop.pk])).status_code for _ in range(4)]
            self.assertEqual(codes, [302] * 4)
            self.assertEqual(Closure.objects.count(), 1)
            Closure.objects.all().delete()
            self.client.post(reverse("break_now", args=[self.shop.pk]))
            self.assertEqual(Closure.objects.count(), 0)

    def broken(self):
        now = timezone.now()
        Shop.objects.filter(pk=self.shop.pk).update(
            lightning_status=LightningStatus.NO_ANSWER, lightning_since=now - timedelta(hours=30), lightning_checked=now,
            lightning_worked=now - timedelta(days=3), lightning_checked_address="bar@example.org")

    def test_a_fault_kept_quiet_is_told_to_the_association_however_the_shop_reopens(self):
        self.broken()
        away = reverse("break_over", args=[self.shop.pk])
        for make_closure, act, told in (
                (lambda: make(self.shop, today() - days(3), None), lambda: self.client.post(away), 1),
                (lambda: make(self.shop, today(), None), lambda: self.client.post(away), 1),
                (lambda: make(self.shop, today() - days(3), today() + days(4)),
                 lambda: self.send(reopens_on=today().isoformat()), 1),
                (lambda: make(self.shop, today(), today() + days(4)),
                 lambda: self.send(reopens_on=today().isoformat()), 1),
                # A closure still to come never kept anything quiet
                (lambda: make(self.shop, today() + days(3), today() + days(9)), lambda: self.client.post(away), 0)):
            Closure.objects.all().delete()
            make_closure()
            with patch.object(lightning_watch, "tell_association") as heard:
                act()
            self.assertEqual(heard.call_count, told)
            self.assertFalse(vacation.is_closed(self.shop_now()))

    def test_the_message_says_welcome_back_when_the_form_opens_the_shop_today(self):
        make(self.shop, today() - days(3), today() + days(4))
        page = self.send(reopens_on=today().isoformat(), follow=True)
        self.assertContains(page, "Welcome back! The shop says it is open again.")

    def test_the_card_keeps_closing_for_a_while_next_to_the_welcome(self):
        make(self.shop, today() - days(10), today() - days(1))
        page = self.client.get(reverse("shops")).content.decode()
        self.assertIn("Welcome back!", page)
        self.assertIn("Closing for a while?", page)
        Closure.objects.all().delete()
        make(self.shop, today() - days(10), today() - days(1))
        make(self.shop, today() + days(5), today() + days(9))
        page = self.client.get(reverse("shops")).content.decode()
        self.assertIn("Welcome back!", page)
        self.assertIn("Change the dates", page)

    def test_the_panel_reads_the_closures_once_for_all_the_shops(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        for number in range(3):
            other = Shop.objects.create(name=f"B{number}", owner=self.owner, 
                                        lightning_address=f"b{number}@example.org", latitude=1, longitude=2)
            make(other, today() - days(2), today() + days(5))
        with CaptureQueriesContext(connection) as done:
            self.client.get(reverse("shops"))
        self.assertEqual(len([q for q in done if 'FROM "shops_closure"' in q["sql"]]), 1)

    def test_the_register_and_the_money_are_not_touched(self):
        before = Shop.objects.values().get(pk=self.shop.pk)
        self.send(until_told="on")
        after = Shop.objects.values().get(pk=self.shop.pk)
        self.assertEqual(before, after)
        # The register opens as always, closed or not
        self.assertEqual(self.client.get(reverse("register", args=[self.shop.pk])).status_code, 200)

    def test_the_panel_card_in_its_three_states(self):
        page = self.client.get(reverse("shops")).content.decode()
        self.assertIn("Closing for a while?", page)
        make(self.shop, today() + days(3), today() + days(9))
        page = self.client.get(reverse("shops")).content.decode()
        self.assertIn("Change the dates", page)
        self.assertNotIn("I am back today", page)
        Closure.objects.all().delete()
        make(self.shop, today(), today() + days(9))
        page = self.client.get(reverse("shops")).content.decode()
        self.assertIn("I am back today", page)
        self.assertIn("no alarm goes off", page)


class MapScript(TestCase):
    """map.js has no test runner in the image: a closure «until I say so» has no day to open again (`back` is null), and
    the date must be written only when there is one, or the whole map fails to load for everybody."""

    def test_a_date_is_written_only_when_there_is_one(self):
        from django.conf import settings

        script = (settings.BASE_DIR / "static" / "map.js").read_text()
        body = script[script.index("function breakNote("):script.index("function note(")]
        self.assertEqual(body.count("dateText("), 1, "every date goes through `when`, which tolerates null")
        self.assertIn('const when = (iso) => (iso ? dateText(iso) : "");', body)
        self.assertNotIn("dateText(found.", body)

    def test_the_pin_keeps_its_discount_in_its_name_when_the_shop_is_closed(self):
        from django.conf import settings

        script = (settings.BASE_DIR / "static" / "map.js").read_text()
        body = script[script.index("function label("):script.index("// Closed for a while")]
        self.assertLess(body.index("texts.discount"), body.index("breakNote(shop)"))
        self.assertIn("${name} · ${pause}", body)


class Migrations(TestCase):
    def test_the_models_match_the_migrations(self):
        call_command("makemigrations", "--check", "--dry-run", verbosity=0)

    def test_the_database_refuses_a_closure_that_opens_before_it_closes(self):
        from django.db import IntegrityError, transaction

        shop = Shop.objects.create(name="Bar")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Closure.objects.create(shop=shop, first_day=today(), reopens_on=today())
        with self.assertRaises(IntegrityError), transaction.atomic():
            Closure.objects.create(shop=shop, first_day=today(), reopens_on=today() - days(1))

    def test_the_model_refuses_what_the_admin_could_type(self):
        from django.core.exceptions import ValidationError

        shop = Shop.objects.create(name="Bar")
        with self.assertRaises(ValidationError) as caught:
            Closure(shop=shop, first_day=today(), reopens_on=today()).full_clean()
        self.assertIn("reopens_on", caught.exception.message_dict)
        # A first day in the past is fine here: the merchant phoned to say they closed on Monday
        Closure(shop=shop, first_day=today() - days(3), reopens_on=None).full_clean()

    def test_the_admin_can_add_a_closure_for_a_merchant_who_phoned(self):
        from .tests import admin_session

        shop = Shop.objects.create(name="Bar", latitude=1, longitude=2)
        admin_session(self.client)
        page = self.client.get(reverse("admin:shops_shop_change", args=[shop.pk]))
        self.assertContains(page, "closures-TOTAL_FORMS")

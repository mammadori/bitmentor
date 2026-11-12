"""The switches of the optional features (config/features.py): the badge of the certified shops, the shops closed for a
while and the practice register. Each one on and
off: the merchant's panel, the public pages, the URLs, the commands and the notices; and what must never be lost."""
import json
from datetime import date, timedelta
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import checks, mail
from django.core.cache import cache
from django.core.management import call_command
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from config import features
from config.community import community
from website.visibility import entitled_account

from . import certified, dashboard, diagnosis, journey, nostr, test_vacation, vacation
from .models import Closure, LightningStatus, Profile, Shop, StaffRegister
from .tests import admin_session, reset_limits

SECRET = "11" * 32


def off(*names):
    """The community's file with these features switched off (`features:` in community.yml)."""
    return patch("config.features.community", return_value={**community(), "features": {name: False for name in names}})


class Base(TestCase):
    def setUp(self):
        cache.clear()
        reset_limits()
        self.owner = get_user_model().objects.create_user("merchant")
        self.shop = Shop.objects.create(name="Bakery", owner=self.owner, lightning_address="bar@example.org",
                                        latitude=41.9, longitude=12.5, lightning=True, discount=10, city="Town")
        Shop.objects.filter(pk=self.shop.pk).update(created=timezone.now() - timedelta(days=200))
        self.shop.refresh_from_db()

    def panel(self):
        self.client.force_login(self.owner)
        response = self.client.get("/en/shops/")
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def update(self, **fields):
        Shop.objects.filter(pk=self.shop.pk).update(**fields)
        self.shop.refresh_from_db()
        return self.shop


class TheBlock(TestCase):
    def test_a_community_that_writes_nothing_has_everything_on(self):
        self.assertEqual(features.states(), {name: True for name in features.FEATURES})
        self.assertEqual(features.switched_off(), [])
        self.assertEqual(features.problems(), [])

    def test_false_switches_one_feature_off_and_leaves_the_others(self):
        with off(features.VACATION):
            self.assertFalse(features.enabled(features.VACATION))
            self.assertEqual(features.switched_off(), [features.VACATION])
            self.assertTrue(features.enabled(features.CERTIFIED))

    def test_a_name_the_software_does_not_know_is_a_mistake_not_a_switch(self):
        with self.assertRaises(KeyError):
            features.enabled("teleporter")

    def test_a_wrong_value_leaves_the_feature_on_and_the_check_says_so(self):
        block = {"certified": "no", "practice": 0, "vacation": False, "teleporter": False}
        with patch("config.features.community", return_value={**community(), "features": block}):
            self.assertTrue(features.enabled(features.CERTIFIED))
            self.assertTrue(features.enabled(features.PRACTICE))
            self.assertFalse(features.enabled(features.VACATION))
            found = features.problems()
            self.assertEqual(len(found), 3)
            self.assertTrue(any("features.teleporter" in problem and "does nothing" in problem for problem in found))
            warnings = [item for item in checks.run_checks() if item.id == "shops.W003"]
            self.assertEqual(len(warnings), 3)

    def test_a_block_that_is_not_a_block_leaves_everything_on(self):
        with patch("config.features.community", return_value={**community(), "features": "off"}):
            self.assertEqual(features.switched_off(), [])
            self.assertEqual(len(features.problems()), 1)
        with patch("config.features.community", return_value={**community(), "features": None}):
            self.assertEqual(features.problems(), [])

    def test_the_pages_know_the_switches(self):
        with off(features.PRACTICE):
            response = self.client.get("/en/shops/map/")
        self.assertEqual(response.context["features"], {**{name: True for name in features.FEATURES}, "practice": False})

    def test_the_example_community_documents_every_switch_and_its_block_is_valid_once_uncommented(self):
        import yaml
        from config.community import COMMUNITY_DIR

        lines = (COMMUNITY_DIR / "community.yml").read_text().splitlines()
        start = lines.index("# features:")
        block = []
        for line in lines[start:]:
            if not line.startswith("#  "):
                if line != "# features:":
                    break
            block.append(line[2:])
        found = yaml.safe_load("\n".join(block))
        self.assertEqual(found, {"features": {name: False for name in features.FEATURES}})

    def test_preflight_counts_the_closures_running_while_they_are_off(self):
        from shops.management.commands.preflight import feature_notes

        shop = Shop.objects.create(name="Bar")
        today = test_vacation.today()
        with off(features.VACATION):
            self.assertEqual(len(feature_notes()), 1)
            test_vacation.make(shop, today - timedelta(days=3), None)
            test_vacation.make(shop, today - timedelta(days=2), today + timedelta(days=9))
            test_vacation.make(shop, today - timedelta(days=30), today - timedelta(days=20))
            notes = feature_notes()
        self.assertEqual(len(notes), 3)
        self.assertIn("1 closures running with no day to reopen", notes[1])
        self.assertIn("1 closures running with a day to reopen", notes[2])
        self.assertIn(str(today + timedelta(days=9)), notes[2])
        with off(features.CERTIFIED):
            self.assertEqual(len(feature_notes()), 1)

    def test_preflight_says_what_is_off(self):
        from shops.management.commands.preflight import feature_notes

        self.assertEqual(feature_notes(), [])
        with off(features.CERTIFIED, features.PRACTICE):
            self.assertEqual(len(feature_notes()), 1)
            self.assertIn("certified, practice", feature_notes()[0])


class Practice(Base):
    def setUp(self):
        super().setUp()
        self.staff = StaffRegister.objects.create(shop=self.shop, name="Anna")

    def test_on_the_panel_the_register_and_the_first_day_offer_it(self):
        html = self.panel()
        self.assertIn(reverse("practice", args=[self.shop.id]), html)
        self.assertIn("Practise first, without money", html)
        self.client.logout()
        self.assertEqual(self.client.get(f"/en/shops/practice/{self.shop.id}/").status_code, 200)
        self.assertIn("Practise without money", self.client.get(f"/en/shops/register/{self.shop.id}/").content.decode())

    def test_off_there_is_no_button_no_link_and_the_urls_answer_404(self):
        with off(features.PRACTICE):
            html = self.panel()
            self.assertNotIn("Practise", html)
            self.assertNotIn("/practice/", html)
            self.assertEqual(self.client.get(f"/en/shops/practice/{self.shop.id}/").status_code, 404)
            self.assertEqual(self.client.get(f"/en/shops/register/p/{self.staff.id}/practice/?k={self.staff.key}").status_code, 404)
            # Not for a visitor either, signed in or not
            self.client.logout()
            self.assertEqual(self.client.get(f"/en/shops/practice/{self.shop.id}/").status_code, 404)
            register_page = self.client.get(f"/en/shops/register/{self.shop.id}/").content.decode()
            self.assertNotIn("Practise", register_page)
            self.assertNotIn("practice-link", register_page)
            staff_page = self.client.get(f"/en/shops/register/p/{self.staff.id}/?k={self.staff.key}").content.decode()
            self.assertNotIn("/practice/", staff_page)
        # Nothing was lost: on again, it is all there
        self.assertEqual(self.client.get(f"/en/shops/practice/{self.shop.id}/").status_code, 200)

    def test_the_diagnosis_does_not_send_the_merchant_to_a_page_that_is_not_there(self):
        def codes_and_text(shop):
            found = diagnosis.diagnose(shop, only={diagnosis.check_customer_path})
            return {item.code for item in found}

        self.assertIn("practice_offer", codes_and_text(self.shop))
        self.assertIn("Practise without money", diagnosis.connect_help())
        with off(features.PRACTICE):
            self.assertNotIn("practice_offer", codes_and_text(self.shop))
            self.assertNotIn("Practise", diagnosis.connect_help())


class Vacation(Base):
    def setUp(self):
        super().setUp()
        self.update(lightning_address="bar@example.org", first_sale=timezone.now())
        self.today = test_vacation.today()

    def fresh(self):
        return test_vacation.fresh(self.shop)

    def test_on_the_card_the_page_the_map_the_json_and_nostr_say_it(self):
        test_vacation.make(self.shop, self.today, self.today + timedelta(days=3))
        self.assertIn(f"/shops/break/{self.shop.id}/", self.panel())
        self.assertIn("Closed for holidays", self.client.get(f"/en/shops/s/{self.shop.id}/").content.decode())
        self.assertIn("Closed for holidays", self.client.get("/en/shops/map/").content.decode())
        self.assertEqual(self.client.get(reverse("shops_json")).json()["shops"][0]["break"]["reason"], "holiday")
        event = nostr.shop_event(SECRET, self.fresh(), "https://example.org/s/1")
        self.assertTrue([tag for tag in event["tags"] if tag[0] == "break"])

    def test_off_the_site_says_nothing_of_a_closure_and_the_urls_answer_404(self):
        test_vacation.make(self.shop, self.today, self.today + timedelta(days=3))
        with off(features.VACATION):
            html = self.panel()
            self.assertNotIn("/break/", html)
            self.assertNotIn("Closing for a while", html)
            self.assertNotIn("Closed for holidays", self.client.get(f"/en/shops/s/{self.shop.id}/").content.decode())
            self.assertNotIn("Closed for holidays", self.client.get("/en/shops/map/").content.decode())
            # The JSON has no `break` at all: `null` would say the shop is open, and nobody knows
            found = self.client.get(reverse("shops_json")).json()["shops"][0]
            self.assertNotIn("break", found)
            self.assertIn("paid", found)
            event = nostr.shop_event(SECRET, self.fresh(), "https://example.org/s/1")
            self.assertFalse([tag for tag in event["tags"] if tag[0] == "break"])
            for name in ("shop_break", "break_now", "break_over", "break_seen"):
                url = reverse(name, args=[self.shop.id])
                self.assertEqual(self.client.get(url).status_code, 404, name)
                self.assertEqual(self.client.post(url).status_code, 404, name)
                self.assertEqual(Client().get(url).status_code, 404, name)
        # The closure was kept, and it is all back when the switch is
        self.assertEqual(Closure.objects.count(), 1)
        self.assertIn("Closed for holidays", self.client.get(f"/en/shops/s/{self.shop.id}/").content.decode())
        self.assertIn("break", self.client.get(reverse("shops_json")).json()["shops"][0])

    def test_the_icons_and_the_path_stop_forgiving_the_closed_days(self):
        self.update(lightning_worked=timezone.now() - timedelta(days=40))
        test_vacation.make(self.shop, self.today - timedelta(days=35), self.today - timedelta(days=1))
        self.assertEqual(certified.bands(self.fresh())["lightning"], "week")
        with off(features.VACATION):
            self.assertEqual(certified.bands(self.fresh())["lightning"], "year")
            self.assertEqual(vacation.closed_days(self.fresh(), self.today - timedelta(days=40), self.today), 0)

    def test_the_administration_keeps_the_closures_so_the_association_can_shorten_or_remove_one(self):
        admin_session(self.client)
        url = reverse("admin:shops_shop_change", args=[self.shop.pk])
        self.assertIn("closures-TOTAL_FORMS", self.client.get(url).content.decode())
        with off(features.VACATION):
            self.assertIn("closures-TOTAL_FORMS", self.client.get(url).content.decode())

    def test_nobody_is_welcomed_back_while_it_is_off(self):
        Profile.objects.update_or_create(user=self.owner, defaults={"email": "merchant@example.org"})
        closure = test_vacation.make(self.shop, self.today - timedelta(days=14), self.today)
        morning = test_vacation.at(self.today, 9)
        with off(features.VACATION), patch("shops.lightning_watch.tell_association") as told:
            self.assertEqual(vacation.welcome_back(morning), [])
            self.assertEqual(vacation.welcome_back(morning), [])
        self.assertEqual(mail.outbox, [])
        # The address works: nobody has anything to hear. The closure is marked, so the next hour's run does not look again
        told.assert_not_called()
        closure.refresh_from_db()
        self.assertIsNotNone(closure.welcomed)
        self.assertEqual(vacation.welcome_back(morning), [])

    def test_the_command_of_the_guardian_runs_without_the_welcome(self):
        Profile.objects.update_or_create(user=self.owner, defaults={"email": "merchant@example.org"})
        test_vacation.make(self.shop, self.today - timedelta(days=14), self.today)
        out = StringIO()
        # No probe at all (--limit 0): only the welcome, which is the part that belongs to the closures
        with off(features.VACATION), patch("django.utils.timezone.now", return_value=test_vacation.at(self.today, 9)):
            call_command("check_lightning", "--limit", "0", stdout=out, stderr=StringIO())
        self.assertNotIn("welcomed back", out.getvalue())
        self.assertEqual(mail.outbox, [])
        with patch("django.utils.timezone.now", return_value=test_vacation.at(self.today, 9)):
            call_command("check_lightning", "--limit", "0", stdout=out, stderr=StringIO())
        # The closure was marked by the run with the feature off: nobody is welcomed afterwards either
        self.assertNotIn("welcomed back", out.getvalue())
        test_vacation.make(self.shop, self.today - timedelta(days=14), self.today + timedelta(days=1), welcomed=None)
        with patch("django.utils.timezone.now", return_value=test_vacation.at(self.today + timedelta(days=1), 9)):
            call_command("check_lightning", "--limit", "0", stdout=out, stderr=StringIO())
        self.assertIn("1 shops welcomed back", out.getvalue())


class ClosedWhenItIsTurnedOff(Base):
    """A closure in course when the community turns the feature off: no false alarm, and no notice about it."""

    def setUp(self):
        super().setUp()
        now = timezone.now()
        self.today = test_vacation.today()
        self.update(lightning_status=LightningStatus.NO_ANSWER, lightning_since=now - timedelta(hours=30), lightning_checked=now,
                    lightning_worked=now - timedelta(days=3), lightning_checked_address="bar@example.org",
                    lightning_address="bar@example.org", first_sale=now - timedelta(days=100))

    def alarms(self):
        found = dashboard.summary()
        return {item.code for item in found["items"] if item.shop and item.shop.pk == self.shop.pk}, found["closed"]

    def test_a_closure_with_a_day_to_open_again_keeps_the_alarms_quiet_until_that_day_and_says_only_the_address_is_broken(self):
        test_vacation.make(self.shop, self.today - timedelta(days=2), self.today + timedelta(days=5))
        self.assertEqual(self.alarms(), ({"closed_lightning"}, 1))
        with off(features.VACATION):
            # No alarm, no count of closed shops; the one line that stays is the fault nobody would hear about
            self.assertEqual(self.alarms(), ({"closed_lightning"}, 0))
            line = [item for item in dashboard.summary()["items"] if item.code == "closed_lightning"][0]
            self.assertEqual(line.level, dashboard.INFO)
            self.assertIn("the association hears about it the day the closure ends", line.do)
            self.assertTrue(vacation.is_quiet(test_vacation.fresh(self.shop)))
            self.assertIsNone(vacation.info(test_vacation.fresh(self.shop)))
            # And the day it said it opens again, the alarms are back, as they are with the feature on
            self.assertFalse(vacation.is_quiet(test_vacation.fresh(self.shop), self.today + timedelta(days=5)))

    def test_the_association_is_told_of_the_fault_the_day_the_closure_ends_and_the_owner_is_not_welcomed(self):
        from . import lightning_watch

        Profile.objects.update_or_create(user=self.owner, defaults={"email": "merchant@example.org"})
        test_vacation.make(self.shop, self.today - timedelta(days=2), self.today)
        with off(features.VACATION), patch.object(lightning_watch, "tell_association") as told:
            self.assertEqual(vacation.welcome_back(test_vacation.at(self.today, 9)), [])
        told.assert_called_once()
        self.assertEqual(told.call_args.args[0][0][1], lightning_watch.BROKE)
        self.assertEqual(mail.outbox, [])
        # A closure that goes on in a longer one is not an ending: nobody is told until the shop is open
        Closure.objects.all().delete()
        test_vacation.make(self.shop, self.today - timedelta(days=9), self.today)
        test_vacation.make(self.shop, self.today - timedelta(days=1), None)
        with off(features.VACATION), patch.object(lightning_watch, "tell_association") as told:
            vacation.welcome_back(test_vacation.at(self.today, 9))
        told.assert_not_called()

    def test_the_quiet_of_a_closure_that_is_off_lasts_at_most_the_budget_days_from_its_first_day(self):
        test_vacation.make(self.shop, self.today - timedelta(days=vacation.MAX_CREDIT + 5), self.today + timedelta(days=200))
        fresh = test_vacation.fresh(self.shop)
        self.assertTrue(vacation.is_quiet(fresh))
        with off(features.VACATION):
            self.assertFalse(vacation.is_quiet(fresh))
            self.assertIn("lightning_no_answer", self.alarms()[0])
        Closure.objects.all().delete()
        test_vacation.make(self.shop, self.today - timedelta(days=vacation.MAX_CREDIT), self.today + timedelta(days=200))
        with off(features.VACATION):
            self.assertTrue(vacation.is_quiet(test_vacation.fresh(self.shop)))

    def test_a_closure_nobody_can_end_does_not_silence_a_real_fault(self):
        test_vacation.make(self.shop, self.today - timedelta(days=2), None)
        self.assertEqual(self.alarms()[1], 1)
        with off(features.VACATION):
            codes_now, closed = self.alarms()
            self.assertIn("lightning_no_answer", codes_now)
            self.assertEqual(closed, 0)

    def broken_once(self):
        now = timezone.now()
        self.update(lightning_status=LightningStatus.OK, lightning_since=now - timedelta(days=2), lightning_checked=now,
                    lightning_worked=now - timedelta(hours=3), lightning_failures=1,
                    lightning_failing_since=now - timedelta(hours=2), lightning_checked_address="bar@example.org")

    def test_the_guardian_tells_nobody_about_a_fault_during_a_dated_closure_but_tells_it_during_an_undated_one(self):
        from . import lightning_watch

        for back, told in ((self.today + timedelta(days=5), None), (None, lightning_watch.BROKE)):
            Closure.objects.all().delete()
            self.broken_once()
            test_vacation.make(self.shop, self.today - timedelta(days=1), back)
            with off(features.VACATION):
                shop, change, recorded = lightning_watch.record(self.shop.pk, "bar@example.org", lightning_watch.NO_ANSWER_STATUS)
            self.assertEqual((change, recorded), (told, True))
            # Either way the guardian keeps what it found on the shop, and the panel shows it
            shop.refresh_from_db()
            self.assertTrue(lightning_watch.panel(shop)["broken"])


class Certified(Base):
    def setUp(self):
        super().setUp()
        self.update(certified_month=date(2026, 9, 1), lightning_worked=timezone.now() - timedelta(days=2),
                    first_sale=timezone.now())

    def test_on_the_page_the_map_the_json_and_the_path_have_the_badge(self):
        page = self.client.get(f"/en/shops/s/{self.shop.id}/").content.decode()
        self.assertIn('class="certified"', page)
        self.assertIn("Certified shop: its Lightning address works", page)
        self.assertIn('class="certified"', self.client.get("/en/shops/map/").content.decode())
        self.assertIs(self.client.get(reverse("shops_json")).json()["shops"][0]["certified"], True)
        self.assertTrue(certified.owns_certified_shop(self.owner))
        self.assertIn("certificate", [step["key"] for step in journey.summary(self.shop)["steps"]])

    def test_off_there_is_no_badge_no_flag_and_no_private_part_but_the_icons_stay(self):
        Shop.objects.filter(pk=self.shop.pk).update(private_approved=timezone.now(), private_approved_by="boss")
        with off(features.CERTIFIED):
            page = self.client.get(f"/en/shops/s/{self.shop.id}/").content.decode()
            self.assertNotIn('class="certified"', page)
            self.assertNotIn("Certified shop", page)
            self.assertNotIn("certified-rule", page)
            self.assertNotIn('class="certified"', self.client.get("/en/shops/map/").content.decode())
            found = self.client.get(reverse("shops_json")).json()["shops"][0]
            # Not `false`: that would say the shop is not certified, and nobody knows
            self.assertNotIn("certified", found)
            self.assertEqual(found["paid"], {"lightning": "week"})
            self.assertFalse(self.shop.certified)
            self.assertFalse(certified.owns_certified_shop(self.owner))
            self.assertFalse(entitled_account(self.owner))
            steps = [step["key"] for step in journey.summary(self.shop)["steps"]]
            self.assertNotIn("certificate", steps)
            self.assertIn("first_day", steps)
            self.assertNotIn("certificate", self.panel().lower().replace("certificate_", ""))
        # The certificate was never touched
        self.shop.refresh_from_db()
        self.assertEqual(self.shop.certified_month, date(2026, 9, 1))
        self.assertTrue(self.shop.certified)
        self.assertTrue(entitled_account(self.owner))

    def test_the_administration_has_no_column_filter_or_action_for_it(self):
        admin_session(self.client)
        url = reverse("admin:shops_shop_changelist")
        on = self.client.get(url).content.decode()
        self.assertIn("revoke_certificate", on)
        self.assertIn("column-certified_column", on)
        with off(features.CERTIFIED):
            page = self.client.get(url).content.decode()
            self.assertNotIn("revoke_certificate", page)
            self.assertNotIn("column-certified_column", page)
            self.assertNotIn("certified shop", page.lower().replace("certified shops", ""))
            self.assertNotIn("certified_month", self.client.get(reverse("admin:shops_shop_change", args=[self.shop.pk])).content.decode())

    def test_the_diagnosis_does_not_talk_about_the_certificate(self):
        self.update(certified_month=None)
        self.assertTrue({item.code for item in diagnosis.diagnose(self.shop, only={diagnosis.check_certificate})})
        with off(features.CERTIFIED):
            self.assertEqual(diagnosis.diagnose(self.shop, only={diagnosis.check_certificate}), [])


class EverythingOff(Base):
    def test_the_json_the_page_and_the_map_still_work_for_the_other_features(self):
        with off(*features.FEATURES):
            self.assertEqual(self.client.get("/en/shops/map/").status_code, 200)
            self.assertEqual(self.client.get(reverse("shops_json")).status_code, 200)
            self.assertEqual(self.client.get(f"/en/shops/s/{self.shop.id}/").status_code, 200)
            self.assertEqual(self.client.get("/en/shops/").status_code, 200)
            self.assertEqual(json.loads(self.client.get(reverse("shops_json")).content)["shops"][0]["name"], "Bakery")

    def test_every_page_a_merchant_uses_opens_with_the_three_features_off(self):
        self.update(telegram_chat="1", certified_month=date(2026, 9, 1))
        test_vacation.make(self.shop, test_vacation.today(), None)
        with off(*features.FEATURES):
            self.client.force_login(self.owner)
            for url in ("/en/shops/", "/en/shops/map/", f"/en/shops/s/{self.shop.id}/", f"/en/shops/register/{self.shop.id}/",
                        f"/en/shops/edit/{self.shop.id}/",
                        f"/en/shops/help/{self.shop.id}/", f"/en/shops/sign/{self.shop.id}/"):
                self.assertEqual(self.client.get(url).status_code, 200, url)
            admin_session(self.client)
            for url in ("/admin/shops/shop/", f"/admin/shops/shop/{self.shop.pk}/change/", "/admin/today/"):
                self.assertEqual(self.client.get(url).status_code, 200, url)
        self.assertEqual(dashboard.summary()["shops"], 1)

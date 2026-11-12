"""When a merchant calls (shops/diagnosis.py and manage.py diagnose_shop): every check with fake answers of the
Lightning provider, read-only proven on every test, no secrets in the output."""
import ast
import io
import json
import re
import threading
import time
import uuid
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import requests
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone, translation

from . import dashboard, diagnosis, moving, staff, training, vacation
from .diagnosis import OK, PROBLEM, WARNING, diagnose, find_shops, masked
from .models import AccountChange, Closure, LightningStatus, Profile, Shop, StaffRegister, Trainee

WRITES = re.compile(r"^\s*(INSERT|UPDATE|DELETE|REPLACE)\b", re.I)


def provider(verify=True, fail=None):
    """The shop's Lightning provider as lnaddress.fetch sees it: LNURL-pay, invoice, LUD-21."""
    def fetch(url):
        if fail:
            raise fail
        if "/.well-known/lnurlp/" in url:
            host = url.split("/")[2]
            return {"tag": "payRequest", "callback": f"https://{host}/cb", "minSendable": 1000, "metadata": "[]"}
        if "/cb" in url:
            host = url.split("/")[2]
            return {"pr": "lntbs10n1fake", **({"verify": f"https://{host}/verify/1"} if verify else {})}
        return {"status": "OK", "settled": False, "pr": "lntbs10n1fake"}
    return fetch


@override_settings(TELEGRAM_BOT_TOKEN="bot-token-secret")
class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = get_user_model().objects.create(username="02" + "ab" * 32, last_login=timezone.now())
        Profile.objects.create(user=self.owner, email="mario.rossi@example.org")
        self.shop = Shop.objects.create(
            owner=self.owner, name="Central Bar", city="Town",
            lightning_address="bar@ln.example.org", lightning=True, telegram_chat="12345", latitude=41.9, longitude=12.5, sign_printed=timezone.now(),
            register_shared=timezone.now(), first_sale=timezone.now())
        self.fetch = provider()
        self.domains = []
        # The customers' guide offers a wallet (the example community lists none)
        self.links = {"wallets": [{"name": "Wallet", "methods": ["lightning", "onchain"]}], "first_bitcoin": ""}
        patcher = patch("shops.diagnosis.customer_links", return_value=self.links)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_diagnosis(self, shop=None, seconds=diagnosis.TOTAL_SECONDS):
        """diagnose with the fake provider, proving on the way that nothing is written to the database."""
        fetch = self.fetch
        with patch("shops.lnaddress.fetch", side_effect=lambda url: (self.domains.append(url.split("/")[2]),
                                                                       fetch(url))[1]), \
                CaptureQueriesContext(connection) as queries:
            findings = diagnose(shop or self.shop, seconds)
        self.assertEqual([query["sql"] for query in queries.captured_queries if WRITES.match(query["sql"])], [])
        return findings

    def codes(self, findings):
        return {finding.code: finding for finding in findings}


class Healthy(Base):
    def test_a_shop_in_order_is_all_fine(self):
        findings = self.run_diagnosis()
        codes = self.codes(findings)
        self.assertEqual({finding.level for finding in findings}, {OK}, [f.as_dict() for f in findings])
        for code in ("shop_visible", "owner_recovery_email", "lightning_ok", "register_ready", "first_day_done"):
            self.assertIn(code, codes)
        self.assertIn("bar@ln.example.org", codes["register_ready"].detail)
        self.assertEqual(diagnosis.worst(findings), OK)
        # The probe asked the address the register uses, through lnaddress.fetch
        self.assertEqual(set(self.domains), {"ln.example.org"})

    def test_findings_are_data_problems_first(self):
        self.fetch = provider(fail=requests.ConnectionError("down"))
        findings = self.run_diagnosis()
        self.assertEqual(findings[0].code, "lightning_no_answer")
        levels = [diagnosis.LEVELS.index(finding.level) for finding in findings]
        self.assertEqual(levels, sorted(levels))
        self.assertEqual(set(findings[0].as_dict()), {"code", "level", "say", "detail", "do"})


class Lightning(Base):
    def test_the_wallet_does_not_answer(self):
        self.fetch = provider(fail=requests.ConnectionError("down"))
        codes = self.codes(self.run_diagnosis())
        self.assertEqual(codes["lightning_no_answer"].level, PROBLEM)
        self.assertIn("bar@ln.example.org", codes["lightning_no_answer"].detail)

    def test_the_wallet_does_not_confirm_payments(self):
        self.fetch = provider(verify=False)
        codes = self.codes(self.run_diagnosis())
        self.assertEqual(codes["lightning_no_confirmation"].level, PROBLEM)
        # The register can still charge: the merchant confirms from the wallet
        self.assertIn("register_ready", codes)

    def test_a_slow_wallet_does_not_hold_the_rest(self):
        release = threading.Event()

        def slow(url):
            release.wait(5)
            raise requests.ConnectionError("late")
        self.fetch = slow
        try:
            with patch.object(diagnosis, "LIGHTNING_SECONDS", 0.6):
                started = time.monotonic()
                codes = self.codes(self.run_diagnosis())
                self.assertLess(time.monotonic() - started, 3)
        finally:
            release.set()
        self.assertIn("lightning_slow", codes, [finding.as_dict() for finding in codes.values()])
        self.assertEqual(codes["lightning_slow"].level, PROBLEM)
        self.assertIn("register_ready", codes)

    def test_threads_left_behind_are_limited(self):
        with patch.object(diagnosis, "THREADS", threading.BoundedSemaphore(1)) as places:
            places.acquire()
            with patch.object(diagnosis, "LIGHTNING_SECONDS", 0.6):
                codes = self.codes(self.run_diagnosis())
            places.release()
        # No place for the thread: nothing was asked, so the shop is not told its wallet is slow; the checks are skipped
        self.assertNotIn("lightning_slow", codes)
        self.assertEqual(codes["time_up"].level, WARNING)
        self.assertIn("lightning", codes["time_up"].detail)
        self.assertEqual(self.domains, [])

    def test_the_probe_keeps_the_protections_of_lnaddress(self):
        """Without the fake provider: an address on a private name is refused before any connection."""
        self.shop.lightning_address = "bar@wallet.internal"
        with patch("shops.lnaddress.requests.Session.get", side_effect=AssertionError("connected")) as get:
            codes = self.codes(diagnose(self.shop))
        get.assert_not_called()
        self.assertEqual(codes["lightning_no_answer"].level, PROBLEM)


class ShopState(Base):
    def test_imported_shop_without_register_never_asks_a_wallet(self):
        shop = Shop.objects.create(name="Old Shop", external_id="12", latitude=1, longitude=1)
        codes = self.codes(self.run_diagnosis(shop))
        self.assertEqual(self.domains, [])
        self.assertEqual(codes["shop_no_owner"].level, WARNING)
        self.assertIn("external_id=12", codes["shop_no_owner"].detail)
        self.assertEqual(set(codes), {"shop_no_owner", "shop_visible"})

    def test_suspended_historic_unlisted(self):
        self.shop.suspended, self.shop.listed = True, False
        self.shop.closed_on = timezone.localdate()
        codes = self.codes(self.run_diagnosis())
        for code in ("shop_suspended", "shop_historic", "shop_unlisted"):
            self.assertEqual(codes[code].level, WARNING)
        self.assertNotIn("first_day_done", codes)

    def test_no_position(self):
        self.shop.latitude = None
        self.assertIn("shop_no_position", self.codes(self.run_diagnosis()))

    def test_owner_without_recovery_email(self):
        Profile.objects.filter(user=self.owner).update(email="")
        codes = self.codes(self.run_diagnosis())
        self.assertEqual(codes["owner_no_recovery_email"].level, WARNING)
        self.assertIn("wallet key 02ababab…", codes["owner_no_recovery_email"].detail)

    def test_owner_with_recovery_email_masked(self):
        detail = self.codes(self.run_diagnosis())["owner_recovery_email"].detail
        self.assertIn("m***@e***.org", detail)
        self.assertEqual(masked("a@b"), "a***@***")


class Limits(Base):
    def test_a_broken_check_does_not_stop_the_others(self):
        def broken(run):
            raise RuntimeError("boom")
        checks = [broken if check is diagnosis.check_register else check for check in diagnosis.CHECKS]
        with patch.object(diagnosis, "CHECKS", checks), self.assertLogs("shops.diagnosis", "ERROR") as logs:
            codes = self.codes(self.run_diagnosis())
        self.assertIn("RuntimeError", logs.output[0])
        self.assertEqual(codes["check_failed"].detail, "broken: RuntimeError")
        self.assertIn("lightning_ok", codes)

    def test_the_whole_run_has_a_time_limit(self):
        findings = self.run_diagnosis(seconds=0.2)
        codes = self.codes(findings)
        self.assertEqual(codes["time_up"].level, WARNING)
        self.assertIn("lightning", codes["time_up"].detail)


class NoSecrets(Base):
    def test_no_secret_in_any_language(self):
        Profile.objects.filter(user=self.owner).update(email="mario.rossi@example.org")
        for language in ("en", "it", "de"):
            with translation.override(language):
                text = json.dumps([finding.as_dict() for finding in self.run_diagnosis()], ensure_ascii=False)
            for secret in ("bot-token-secret", "mario.rossi", "rossi@example.org"):
                self.assertNotIn(secret, text, language)


class Search(Base):
    def test_by_id_email_name_and_number(self):
        other = Shop.objects.create(name="Bar Sport", external_id="7", email="info@sport.example.org")
        self.assertEqual(find_shops(str(self.shop.id)), [self.shop])
        self.assertEqual(find_shops("MARIO.ROSSI@example.org"), [self.shop])
        self.assertEqual(find_shops("info@sport.example.org"), [other])
        self.assertEqual(find_shops("central"), [self.shop])
        self.assertEqual(find_shops("7"), [other])
        self.assertEqual(find_shops("bar"), [other, self.shop])  # by name
        self.assertEqual(find_shops("nobody@example.org"), [])
        self.assertEqual(find_shops("  "), [])
        self.assertEqual(find_shops(str(other.id).replace("-", "")), [other])


class Command(Base):
    def call(self, *args):
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with patch("shops.lnaddress.fetch", side_effect=self.fetch):
            try:
                call_command("diagnose_shop", *args, stdout=out, stderr=err)
            except SystemExit as exit:
                code = exit.code
        return code, out.getvalue(), err.getvalue()

    def test_all_fine_exits_0(self):
        code, out, _err = self.call("central")
        self.assertEqual(code, 0)
        self.assertIn("✓ Your Lightning address answers", out)
        self.assertIn("    [lightning_ok] bar@ln.example.org;", out)

    def test_a_problem_exits_1(self):
        self.fetch = provider(fail=requests.ConnectionError("down"))
        code, out, _err = self.call(str(self.shop.id))
        self.assertEqual(code, 1)
        self.assertTrue(out.splitlines()[1].startswith("✗ "))

    def test_not_found_exits_2(self):
        code, out, err = self.call("nothing like this")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("No shop found for “nothing like this”.", err)

    def test_several_found_are_listed(self):
        Shop.objects.create(name="Bar Sport")
        code, _out, err = self.call("bar")
        self.assertEqual(code, 2)
        self.assertIn("2 shops found: run it again with the id", err)
        self.assertIn(str(self.shop.id), err)

    def test_json(self):
        code, out, _err = self.call("central", "--json")
        data = json.loads(out)
        self.assertEqual((code, data["worst"], data["shop"]["id"]), (0, OK, str(self.shop.id)))
        self.assertTrue(all(set(item) == {"code", "level", "say", "detail", "do"} for item in data["findings"]))
        code, out, _err = self.call("nothing like this", "--json")
        self.assertEqual((code, json.loads(out)), (2, {"found": []}))

    @override_settings(LANGUAGE_CODE="it")
    def test_in_the_community_first_language(self):
        _code, out, _err = self.call("central")
        self.assertIn("[lightning_ok]", out)
        self.assertNotIn("Your Lightning address answers", out)
        self.assertIn("Kein Geschäft gefunden", self.call("nothing", "--language", "de")[2])
        self.assertIn("Nessun negozio trovato per «nothing».", self.call("nothing")[2])

    def test_another_language(self):
        _code, out, _err = self.call("central", "--language", "de")
        self.assertIn("[lightning_ok]", out)
        self.assertNotIn("Your Lightning address answers", out)


class Waiting(Base):
    """A shop with no register because it waits: for its owner, for the owner's wallet."""

    def moved(self, **fields):
        data = {"name": "Moved Bar", "moved_in": timezone.now(), "moved_from": str(uuid.uuid4()), "moved_register": True,
                "latitude": 41.9, "longitude": 12.5, **fields}
        return Shop.objects.create(**data)

    def test_saved_without_a_wallet(self):
        shop = self.moved(owner=self.owner, moved_in=None, moved_from="", moved_register=False, awaiting_wallet=True)
        codes = self.codes(self.run_diagnosis(shop))
        self.assertEqual(codes["shop_waiting_wallet"].level, WARNING)
        self.assertIn("connect your wallet", codes["shop_waiting_wallet"].say)
        self.assertIn("Practise without money", codes["shop_waiting_wallet"].do)
        # Not on the map, no register to blame: one explanation, not three
        for code in ("shop_visible", "no_register", "shop_no_owner", "customer_path_ok"):
            self.assertNotIn(code, codes)

    def test_moved_and_not_yet_taken(self):
        shop = self.moved(claim_hash="a" * 64)
        codes = self.codes(self.run_diagnosis(shop))
        self.assertEqual(codes["moved_unclaimed"].level, WARNING)
        self.assertIn("open the link we sent you", codes["moved_unclaimed"].say)
        self.assertIn(f"--only {shop.pk} --new-codes", codes["moved_unclaimed"].detail)
        self.assertIn("one-time link", codes["moved_unclaimed"].do)
        self.assertNotIn("shop_no_owner", codes)
        self.assertNotIn("shop_visible", codes)
        # The digest of the code stays where it is
        self.assertNotIn("a" * 64, json.dumps([finding.as_dict() for finding in self.run_diagnosis(shop)]))

    def test_moved_with_nobody_to_give_it_to(self):
        codes = self.codes(self.run_diagnosis(self.moved()))
        self.assertEqual(codes["moved_no_owner"].level, WARNING)
        self.assertIn("assign the shop", codes["moved_no_owner"].do)

    def test_moved_and_taken_but_without_a_wallet(self):
        shop = self.moved(owner=self.owner)
        codes = self.codes(self.run_diagnosis(shop))
        self.assertIn("connect your wallet", codes["moved_no_wallet"].say)
        self.assertIn("off the map", codes["moved_no_wallet"].detail)
        self.assertNotIn("no_register", codes)
        # Moved without a register there: it was on the map, and is again
        shop = self.moved(owner=self.owner, moved_register=False, name="Other")
        self.assertIn("shop_visible", self.codes(self.run_diagnosis(shop)))

    def test_the_dashboard_and_the_diagnosis_agree(self):
        shops = [self.moved(claim_hash="b" * 64, name="A"), self.moved(name="B"), self.moved(owner=self.owner, name="C")]
        listed = {item.shop.pk: item.code for item in dashboard.moved_items(timezone.now())}
        self.assertEqual(len(listed), 3)
        for shop in shops:
            self.assertEqual(listed[shop.pk], f"moved_{moving.waiting_for(shop)}")
            self.assertIn(listed[shop.pk], self.codes(self.run_diagnosis(shop)))
        # A shop with its register, a historic one that is over, or one that did not move wait for nothing
        self.assertIsNone(moving.waiting_for(self.shop))
        self.assertIsNone(moving.waiting_for(self.moved(owner=self.owner, closed_on=date(2020, 1, 1), name="D")))
        self.assertIsNone(moving.waiting_for(Shop.objects.create(name="E")))

    def test_found_by_the_id_it_had_on_the_other_site(self):
        shop = self.moved()
        self.assertEqual(find_shops(shop.moved_from), [shop])
        self.assertEqual(find_shops(shop.moved_from.replace("-", "").upper()), [shop])
        self.assertEqual(find_shops(str(uuid.uuid4())), [])


class Closures(Base):
    def setUp(self):
        super().setUp()
        self.today = vacation.day_of()

    def close(self, start, end=None, reason="holiday"):
        return Closure.objects.create(
            shop=self.shop, first_day=self.today + timedelta(days=start), reason=reason,
            reopens_on=None if end is None else self.today + timedelta(days=end))

    def test_closed_for_holidays(self):
        self.close(-2, 5)
        codes = self.codes(self.run_diagnosis())
        self.assertEqual(codes["closed_now"].level, OK)
        self.assertIn("Closed for holidays · back on", codes["closed_now"].say)
        self.assertIn("no alarm goes off", codes["closed_now"].say)
        self.assertIn("Change the dates", codes["closed_now"].do)
        for code in ("closed_hidden", "closed_long"):
            self.assertNotIn(code, codes)

    def test_closed_until_the_owner_says(self):
        self.close(-2)
        self.assertIn("back soon", self.codes(self.run_diagnosis())["closed_now"].say)

    def test_a_closure_still_to_come(self):
        self.close(3, 10)
        codes = self.codes(self.run_diagnosis())
        self.assertIn("already say", codes["closed_soon"].say)
        Closure.objects.all().delete()
        self.close(30, 40)
        codes = self.codes(self.run_diagnosis())
        self.assertIn(f"{vacation.SOON_DAYS} days before", codes["closed_planned"].say)

    def test_a_historic_shop_ignores_its_closures(self):
        self.close(-2, 5)
        self.shop.closed_on = self.today - timedelta(days=10)
        codes = self.codes(self.run_diagnosis())
        self.assertEqual(codes["closed_ignored"].level, WARNING)
        self.assertNotIn("closed_now", codes)

    def test_a_closed_shop_off_the_map_says_so(self):
        self.close(-2, 5)
        self.shop.listed = False
        codes = self.codes(self.run_diagnosis())
        self.assertIn("nobody reads the notice", codes["closed_hidden"].say)

    def test_closed_for_months(self):
        self.close(-120)
        codes = self.codes(self.run_diagnosis())
        self.assertIn("Is your shop coming back?", codes["closed_long"].say)
        self.assertIn("120 days", codes["closed_long"].detail)
        self.assertIn(f"at most {vacation.MAX_CREDIT} closed days", codes["closed_long"].detail)

    def test_the_lines_from_the_database_come_even_without_time(self):
        self.close(-2, 5)
        codes = self.codes(self.run_diagnosis(seconds=0))
        self.assertIn("closed_now", codes)
        self.assertIn("time_up", codes)
        self.assertNotIn("lightning_ok", codes)



class StaffRegisters(Base):
    def register(self, name="Anna", **fields):
        register = StaffRegister.objects.create(shop=self.shop, name=name, **fields)
        staff.new_code(register)
        return register

    def test_a_register_waiting_for_its_code_to_be_scanned(self):
        register = self.register()
        findings = self.run_diagnosis()
        codes = self.codes(findings)
        found = codes["staff_code_waiting"]
        self.assertEqual(found.level, WARNING)
        self.assertIn("Anna", found.say)
        self.assertIn("code valid until", found.detail)
        self.assertIn("never read out on the phone", found.do)
        # The code still works: nobody is told to make another
        self.assertNotIn("staff_unopened", codes)
        self.assertNotIn("make a new one", found.say)
        # Neither the key of the register nor its code leave
        text = json.dumps([finding.as_dict() for finding in findings])
        self.assertNotIn(register.key, text)
        self.assertNotIn(register.code, text)

    def test_a_register_never_opened_whose_code_is_gone(self):
        register = self.register()
        StaffRegister.objects.filter(pk=register.pk).update(code="", code_until=None)
        codes = self.codes(self.run_diagnosis())
        found = codes["staff_unopened"]
        self.assertEqual(found.level, WARNING)
        self.assertIn("no longer valid: Anna", found.say)
        self.assertIn("make a new one", found.say)
        self.assertIn("used or expired", found.detail)
        self.assertNotIn("staff_code_waiting", codes)
        # An expired one too
        StaffRegister.objects.filter(pk=register.pk).update(code="x" * 30, code_until=timezone.now() - timedelta(hours=1))
        self.assertIn("staff_unopened", self.codes(self.run_diagnosis()))

    def test_names_come_out_as_a_terminal_shows_them(self):
        self.register(name="Mia\x1b[2Jx", opened=timezone.now())
        self.register(name="Luca\x07", retired=timezone.now())
        self.register(name="Nina\u202e")
        findings = self.run_diagnosis()
        text = "".join(finding.say + finding.detail + finding.do for finding in findings)
        for bad in ("\x1b", "\x07", "\u202e"):
            self.assertNotIn(bad, text)
        self.assertIn("Mia?[2Jx", text)
        self.assertIn("Luca?", text)

    def test_a_register_that_works(self):
        self.register(opened=timezone.now())
        found = self.codes(self.run_diagnosis())["staff_ok"]
        self.assertEqual(found.level, OK)
        self.assertIn("Anna", found.say)
        self.assertIn(f"Anna opened {timezone.now():%Y-%m-%d}", found.detail)
        self.assertNotIn("sale", found.detail)

    def test_a_register_turned_off(self):
        self.register(opened=timezone.now(), retired=timezone.now())
        codes = self.codes(self.run_diagnosis())
        self.assertIn("turned off, so they take no new payment: Anna", codes["staff_off"].say)
        self.assertNotIn("staff_ok", codes)

    def test_without_a_register_of_its_own_none_can_work(self):
        self.register(opened=timezone.now())
        shop = self.shop
        shop.lightning_address = ""
        codes = self.codes(self.run_diagnosis(shop))
        self.assertEqual(codes["staff_no_register"].level, PROBLEM)
        # They are not «working»: every link of theirs answers 404
        self.assertNotIn("staff_ok", codes)

    def test_no_staff_no_lines(self):
        codes = self.codes(self.run_diagnosis())
        self.assertFalse([code for code in codes if code.startswith("staff_")])


class Account(Base):
    def change(self, changed="username", ago=1, **fields):
        data = {"user": self.owner, "shops": self.shop.name, "changed": changed, "actor": "gino", "via": "admin",
                "told": "email", "created": timezone.now() - timedelta(days=ago), **fields}
        return AccountChange.objects.create(**data)

    def test_a_change_of_how_the_owner_signs_in(self):
        self.change("username")
        found = self.codes(self.run_diagnosis())["account_changed"]
        self.assertEqual(found.level, PROBLEM)
        self.assertIn("The name your account signs in with was replaced by another one.", found.say)
        self.assertIn("If you know why, you do not need to do anything", found.say)
        self.assertIn("by gino", found.detail)
        self.assertIn("told: email", found.detail)
        self.assertIn("Never ask for a password", found.do)
        self.assertNotIn(self.owner.username, json.dumps(found.as_dict()))

    def test_an_everyday_change_is_a_warning(self):
        self.change("groups", told="")
        self.assertEqual(self.codes(self.run_diagnosis())["account_changed"].level, WARNING)

    def test_old_changes_are_forgotten(self):
        self.change("username", ago=20)
        self.assertNotIn("account_changed", self.codes(self.run_diagnosis()))

    def test_the_new_owner_of_a_shop_handed_over_is_not_told_it_was_taken(self):
        old = get_user_model().objects.create(username="02" + "cd" * 32)
        self.change("shop_owner", user=old, shops="Pizza, Central Bar")
        self.change("shop_chat", user=None, shops="Central Bar")
        self.change("shop_owner", user=old, shops="Central Bar Due")
        codes = self.codes(self.run_diagnosis())
        found = codes["account_changed_other"]
        # A fact about the shop, in the third person, and never a problem for the owner who has it now
        self.assertEqual(found.level, WARNING)
        self.assertNotIn("account_changed", codes)
        self.assertIn("another account, not to the one signed in now", found.say)
        self.assertNotIn("Your", found.say)
        self.assertNotIn("If you know why", found.say)
        self.assertEqual(found.detail.count("shop_owner"), 1)
        self.assertEqual(found.detail.count("shop_chat"), 1)
        self.assertIn("had the shop before", found.do)

    def test_both_at_once(self):
        old = get_user_model().objects.create(username="02" + "cd" * 32)
        self.change("groups", told="")
        self.change("shop_owner", user=old, shops="Central Bar")
        codes = self.codes(self.run_diagnosis())
        self.assertEqual(codes["account_changed"].level, WARNING)
        self.assertIn("1 change(s) of the owner's account", codes["account_changed"].detail)
        self.assertNotIn("shop_owner", codes["account_changed"].detail)
        self.assertIn("shop_owner", codes["account_changed_other"].detail)

    def test_a_shop_without_an_owner_has_no_owners_account(self):
        shop = Shop.objects.create(name="Orphan")
        AccountChange.objects.create(user=None, shops="Orphan", changed="deleted", via="admin")
        codes = self.codes(self.run_diagnosis(shop))
        self.assertEqual(codes["account_changed_other"].level, WARNING)
        self.assertNotIn("account_changed", codes)

    def test_the_latest_changes_are_kept_when_there_are_many(self):
        for number in range(25):
            self.change("groups", ago=5, told="")
        self.change("username", ago=1)
        found = self.codes(self.run_diagnosis())["account_changed"]
        self.assertEqual(found.level, PROBLEM)
        self.assertEqual(found.detail.count(" via admin"), 20)
        # In order, the latest last
        self.assertTrue(found.detail.rstrip().split(" | ")[-1].split(" UTC ")[1].startswith("username"))

    def test_other_shops_are_left_alone(self):
        other = get_user_model().objects.create(username="02" + "cd" * 32)
        AccountChange.objects.create(user=other, shops="Pizza", changed="username", via="admin")
        self.assertNotIn("account_changed", self.codes(self.run_diagnosis()))


class Certificate(Base):
    def test_a_certified_shop(self):
        Shop.objects.filter(pk=self.shop.pk).update(certified_month=date(2026, 9, 1))
        self.shop.refresh_from_db()
        self.assertIn("certified, since September 2026", self.codes(self.run_diagnosis())["certificate_done"].say)

    def test_a_revoked_certificate(self):
        Shop.objects.filter(pk=self.shop.pk).update(certified_revoked=True)
        self.shop.refresh_from_db()
        self.assertEqual(self.codes(self.run_diagnosis())["certificate_revoked"].level, WARNING)

    def works(self):
        Shop.objects.filter(pk=self.shop.pk).update(lightning_status=LightningStatus.OK,
                                                    lightning_checked_address=self.shop.lightning_address)
        self.shop.refresh_from_db()

    def test_waiting_for_the_address_and_the_pass(self):
        found = self.codes(self.run_diagnosis())["certificate_waiting"]
        self.assertEqual(found.level, OK)
        self.assertIn("a Lightning address that works, the owner's basic pass of the training", found.say)
        self.assertIn("basic pass no", found.detail)
        self.assertIn("Lightning address works", found.do)

    def test_waiting_for_the_pass_only(self):
        self.works()
        found = self.codes(self.run_diagnosis())["certificate_waiting"]
        self.assertNotIn("Lightning address that works", found.say)
        self.assertIn("basic pass", found.say)

    def test_an_address_found_working_before_it_changed_does_not_count(self):
        self.works()
        Trainee.objects.create(user=self.owner, base_earned=timezone.now())
        self.shop.lightning_address = "other@ln.example.org"
        found = self.codes(self.run_diagnosis())["certificate_waiting"]
        self.assertIn("Lightning address that works", found.say)

    def test_everything_for_the_certificate(self):
        self.works()
        Trainee.objects.create(user=self.owner, base_earned=timezone.now())
        self.assertTrue(training.has_pass(self.shop))
        self.assertIn("certificate_ready", self.codes(self.run_diagnosis()))


class CustomerPath(Base):
    def test_a_guide_with_a_wallet(self):
        found = self.codes(self.run_diagnosis())["customer_path_ok"]
        self.assertEqual(found.level, OK)
        self.assertIn("Card for customers without a wallet", found.say)

    def test_no_wallet_to_suggest(self):
        self.links["wallets"] = [{"name": "Only on-chain", "methods": ["onchain"]}]
        found = self.codes(self.run_diagnosis())["customer_wallets_empty"]
        self.assertEqual(found.level, WARNING)
        self.assertIn("shop_page.wallets", found.do)

    def test_nobody_can_pay_there(self):
        self.shop.suspended = True
        self.assertEqual(self.codes(self.run_diagnosis())["customer_path_off"].level, WARNING)

    def test_the_practice_register_before_the_first_sale(self):
        Shop.objects.filter(pk=self.shop.pk).update(first_sale=None)
        self.shop.refresh_from_db()
        self.assertIn("Practise without money", self.codes(self.run_diagnosis())["practice_offer"].say)

    def test_not_offered_once_the_first_sale_is_made(self):
        self.assertNotIn("practice_offer", self.codes(self.run_diagnosis()))


class NewSectionsCommand(Base):
    call = Command.call

    def test_the_helper_reads_what_to_do_after_an_arrow(self):
        self.shop.listed = True
        Closure.objects.create(shop=self.shop, first_day=vacation.day_of() - timedelta(days=1),
                               reopens_on=vacation.day_of() + timedelta(days=4))
        _code, out, _err = self.call("central")
        lines = out.splitlines()
        index = next(number for number, line in enumerate(lines) if "[closed_now]" in line)
        self.assertTrue(lines[index - 1].startswith("    → Nothing to fix."), lines[index - 1])
        self.assertTrue(lines[index - 2].startswith("✓ On the map and on your shop page it says"))
        # A line with nothing to do has no arrow
        index = next(number for number, line in enumerate(lines) if "[lightning_ok]" in line)
        self.assertFalse(lines[index - 1].startswith("    →"))

    def test_names_are_plain_in_the_header_and_the_list(self):
        shop = Shop.objects.create(name="Bar\x1b[2J", city="To\x07wn")
        _code, out, _err = self.call(str(shop.pk))
        self.assertEqual(out.splitlines()[0], f"Bar?[2J · To?wn · {shop.pk}")
        Shop.objects.create(name="Bar\x1b[2J second")
        _code, _out, err = self.call("Bar")
        self.assertNotIn("\x1b", err)

    def test_json_carries_what_to_do(self):
        Closure.objects.create(shop=self.shop, first_day=vacation.day_of(), reopens_on=vacation.day_of() + timedelta(days=4))
        _code, out, _err = self.call("central", "--json")
        found = {item["code"]: item for item in json.loads(out)["findings"]}
        self.assertIn("Nothing to fix", found["closed_now"]["do"])
        self.assertEqual(found["lightning_ok"]["do"], "")

    def test_a_moved_shop_is_found_by_its_old_id_and_told_in_italian(self):
        shop = Shop.objects.create(name="Moved Bar", moved_in=timezone.now(), moved_from=str(uuid.uuid4()),
                                   claim_hash="c" * 64)
        code, out, _err = self.call(shop.moved_from, "--language", "it")
        self.assertEqual(code, 0)
        self.assertIn("! Il tuo negozio si è trasferito su questo sito", out)
        self.assertIn("→ ", out)


class EveryNewLine(Base):
    """Everything the database tells at once, in the three languages: no sentence breaks on its placeholders, and no
    secret of the shop, of its registers or of its owner's link comes out."""

    def test_in_english_italian_and_german(self):
        # What the diagnosis asks of the provider, before there is anything to tell: the new lines add none
        self.run_diagnosis()
        probed = len(self.domains)
        today = vacation.day_of()
        Closure.objects.create(shop=self.shop, first_day=today - timedelta(days=2), reopens_on=today + timedelta(days=9))
        Shop.objects.filter(pk=self.shop.pk).update(created=timezone.now() - timedelta(days=70))
        self.shop.refresh_from_db()
        register = StaffRegister.objects.create(shop=self.shop, name="Anna")
        staff.new_code(register)
        StaffRegister.objects.create(shop=self.shop, name="Luca", opened=timezone.now(), retired=timezone.now())
        StaffRegister.objects.create(shop=self.shop, name="Rosa", opened=timezone.now())
        AccountChange.objects.create(user=self.owner, shops=self.shop.name, changed="username,groups", actor="gino",
                                     via="admin", told="email")
        moved = Shop.objects.create(name="Moved Bar", moved_in=timezone.now(), moved_from=str(uuid.uuid4()),
                                    claim_hash="d" * 64, latitude=45, longitude=11)
        seen = set()
        for language in ("en", "it", "de"):
            before_domains = len(self.domains)
            with translation.override(language):
                findings = self.run_diagnosis()
                self.assertEqual(len(self.domains) - before_domains, probed, language)
                findings += self.run_diagnosis(moved)
            codes = {finding.code for finding in findings}
            self.assertNotIn("check_failed", codes, language)
            for finding in findings:
                self.assertNotRegex(finding.say + finding.do, r"[{}]", (language, finding.code))
            text = json.dumps([finding.as_dict() for finding in findings], ensure_ascii=False)
            for secret in (register.key, register.code, "d" * 64, self.owner.username):
                self.assertNotIn(secret, text, language)
            seen |= codes
        for code in ("closed_now", "staff_code_waiting", "staff_off", "staff_ok", "account_changed",
                     "certificate_waiting", "customer_path_ok", "moved_unclaimed"):
            self.assertIn(code, seen)


class Translations(TestCase):
    def test_every_sentence_in_italian_and_german(self):
        tree = ast.parse(Path(diagnosis.__file__).read_text())
        texts = {node.args[0].value for node in ast.walk(tree)
                 if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "_" and node.args
                 and isinstance(node.args[0], ast.Constant)}
        self.assertGreater(len(texts), 40)
        for language in ("it", "de"):
            with translation.override(language):
                missing = [text for text in texts if translation.gettext(text) == text]
            self.assertEqual(missing, [], language)
            plurals = [tuple(arg.value for arg in node.args[:2]) for node in ast.walk(tree)
                       if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "ngettext"]
            for one, many in plurals:
                with translation.override(language):
                    self.assertNotEqual(translation.ngettext(one, many, 1), one)
                    self.assertNotEqual(translation.ngettext(one, many, 2), many)
                    self.assertIn("{count}", translation.ngettext(one, many, 2))
            for text in texts:
                # The placeholders survive the translation
                with translation.override(language):
                    self.assertEqual(sorted(re.findall(r"\{\w+\}", translation.gettext(text))),
                                     sorted(re.findall(r"\{\w+\}", text)), text)

"""The second factor of /admin/ (config/second_factor.py, config/totp.py): the codes, the set-up, the sign-in in two steps,
the limits, the day's end, turning it off, the command, the registers and preflight."""
import base64
import hashlib
import os
import subprocess
import sys
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.conf import settings
from django.contrib import admin
from django.contrib.admin.models import LogEntry
from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from shops.management.commands.preflight import admin_notes
from shops.models import AccountChange, AdminSecondFactor, Profile, Shop
from shops.tests import reset_limits, steady_limits

from . import totp
from .guard import ADMIN_SESSION
from .second_factor import FACTOR_SESSION, PENDING, PENDING_SECONDS

User = get_user_model()
PASSWORD = "a long password to sign in 42"
# A moment at the start of a step: the steps around it are whole
T = 1_900_000_005
LOGIN = "/admin/login/"
CODE = "/admin/login/code/"
SETUP = "/admin/second-factor/"
EN = {"headers": {"accept-language": "en"}}


def clock(seconds):
    return patch("config.second_factor.now", return_value=seconds)


def code(factor, seconds=T, steps=0):
    return totp.code_at(factor.secret, seconds + steps * totp.STEP)


def turn_on(user, last_step=0):
    return AdminSecondFactor.objects.create(user=user, secret=totp.new_secret(), confirmed=timezone.now(),
                                            last_step=last_step)


class _Clock:
    """`time` as config.ratelimit reads it, with hands the test moves."""

    def __init__(self, now):
        self.now = now

    def time(self):
        return self.now

    def __getattr__(self, name):
        import time

        return getattr(time, name)


def signed_in(client):
    return "_auth_user_id" in client.session


class Base(TestCase):
    def setUp(self):
        reset_limits()
        self.staff = User.objects.create_user("volunteer", password=PASSWORD, is_staff=True)
        self.client = Client(**EN)
        # What the people who look after the site are told: here, at once and nowhere
        background = patch("config.second_factor.in_background", side_effect=lambda work: work())
        background.start()
        self.addCleanup(background.stop)
        told = patch("shops.notifications.deliver_to_moderators", return_value=("TELEGRAM_MODERATION_CHAT", []))
        self.told = told.start()
        self.addCleanup(told.stop)

    def told_texts(self):
        return [call.args[1] for call in self.told.call_args_list]

    def password(self, client=None, username="volunteer", password=PASSWORD, next="/admin/", **extra):
        """The sign-in's form, as the page posts it: with the hidden `next` the admin puts in it."""
        return (client or self.client).post(LOGIN, {"username": username, "password": password, "next": next}, **extra)

    def sign_in(self, factor, seconds=T, client=None, **extra):
        client = client or self.client
        with clock(seconds):
            first = self.password(client, **extra)
            self.assertRedirects(first, CODE, fetch_redirect_response=False)
            where = {"REMOTE_ADDR": extra["REMOTE_ADDR"]} if "REMOTE_ADDR" in extra else {}
            return client.post(CODE, {"code": code(factor, seconds)}, **where)


# ---------------------------------------------------------------------------------------------------------------------
# The codes
# ---------------------------------------------------------------------------------------------------------------------

class Codes(TestCase):
    # RFC 6238, appendix B: the SHA1 key and the 8-digit codes at these times
    KEY = b"12345678901234567890"
    VECTORS = ((59, "94287082"), (1111111109, "07081804"), (1111111111, "14050471"), (1234567890, "89005924"),
               (2000000000, "69279037"), (20000000000, "65353130"))

    def test_the_rfc_6238_sha1_vectors(self):
        for seconds, expected in self.VECTORS:
            self.assertEqual(totp.hotp(self.KEY, totp.step_of(seconds), digits=8, digest=hashlib.sha1), expected, seconds)
        # The same through the base32 secret, which is what the apps get
        secret = base64.b32encode(self.KEY).decode()
        self.assertEqual(totp.code_at(secret, 59, digits=8), "94287082")
        # And 6 digits are the last 6 of the same number
        self.assertEqual(totp.code_at(secret, 59), "287082")

    def test_a_new_secret_is_160_random_bits_in_base32(self):
        first, second = totp.new_secret(), totp.new_secret()
        self.assertEqual(len(first), 32)
        self.assertEqual(len(totp.secret_bytes(first)), 20)
        self.assertNotEqual(first, second)

    def test_one_step_either_side_and_no_more(self):
        secret = totp.new_secret()
        now = totp.step_of(T)
        for steps, accepted in ((-2, False), (-1, True), (0, True), (1, True), (2, False)):
            found = totp.accepted_step(secret, totp.code_at(secret, T + steps * 30), T)
            self.assertEqual(found, now + steps if accepted else None, steps)

    def test_a_step_already_accepted_or_earlier_is_refused(self):
        secret = totp.new_secret()
        now = totp.step_of(T)
        self.assertIsNone(totp.accepted_step(secret, totp.code_at(secret, T), T, last_step=now))
        self.assertIsNone(totp.accepted_step(secret, totp.code_at(secret, T - 30), T, last_step=now))
        self.assertEqual(totp.accepted_step(secret, totp.code_at(secret, T + 30), T, last_step=now), now + 1)

    def test_what_is_not_a_code(self):
        secret = totp.new_secret()
        good = totp.code_at(secret, T)
        self.assertEqual(totp.accepted_step(secret, f" {good[:3]} {good[3:]} ", T), totp.step_of(T))
        for typed in ("", None, "12345", "1234567", "abcdef", "١٢٣٤٥٦", good + "0"):
            self.assertIsNone(totp.accepted_step(secret, typed, T), typed)

    def test_the_link_for_the_apps(self):
        link = totp.uri("JBSWY3DPEHPK3PXP", "anna@example.org", "shops.example.org")
        self.assertEqual(link, "otpauth://totp/shops.example.org:anna@example.org?secret=JBSWY3DPEHPK3PXP&"
                               "issuer=shops.example.org&algorithm=SHA1&digits=6&period=30")
        self.assertIn("totp/a%3Ab:c%20d?", totp.uri("S", "c d", "a:b"))


# ---------------------------------------------------------------------------------------------------------------------
# The set-up
# ---------------------------------------------------------------------------------------------------------------------

class SetUp(Base):
    def setUp(self):
        super().setUp()
        with clock(T):
            self.assertRedirects(self.password(), "/admin/", fetch_redirect_response=False)

    def test_it_needs_a_right_code(self):
        page = self.client.get(SETUP)
        waiting = AdminSecondFactor.objects.get(user=self.staff)
        self.assertIsNone(waiting.confirmed)
        self.assertContains(page, totp.grouped(waiting.secret))
        self.assertContains(page, "otpauth://totp/")
        # Until it is confirmed it does nothing: the sign-in is the password alone
        self.assertRedirects(self.password(Client(**EN)), "/admin/", fetch_redirect_response=False)
        with clock(T):
            wrong = self.client.post(SETUP, {"action": "on", "code": code(waiting, steps=5)})
        self.assertContains(wrong, "That code is not right")
        # The same secret is shown again, to try again without scanning anew
        self.assertContains(wrong, totp.grouped(waiting.secret))
        self.assertIsNone(AdminSecondFactor.objects.get(pk=waiting.pk).confirmed)
        with clock(T):
            right = self.client.post(SETUP, {"action": "on", "code": code(waiting)})
        self.assertRedirects(right, SETUP, fetch_redirect_response=False)
        self.assertIsNotNone(AdminSecondFactor.objects.get(pk=waiting.pk).confirmed)
        # This session proved the code: it goes on, as one opened with the second factor
        self.assertEqual(self.client.session[FACTOR_SESSION][:2], ["totp", waiting.pk])
        with clock(T + 60):
            self.assertEqual(self.client.get("/admin/").status_code, 200)

    def test_a_visit_within_ten_minutes_shows_the_same_secret_and_a_later_one_a_new_one(self):
        self.client.get(SETUP)
        first = AdminSecondFactor.objects.get(user=self.staff).secret
        # A second tab, or a link from another site: the QR code just scanned still holds
        self.assertContains(self.client.get(SETUP), totp.grouped(first))
        AdminSecondFactor.objects.update(created=timezone.now() - timedelta(minutes=9))
        self.assertContains(self.client.get(SETUP), totp.grouped(first))
        AdminSecondFactor.objects.update(created=timezone.now() - timedelta(minutes=11))
        self.client.get(SETUP)
        self.assertNotEqual(AdminSecondFactor.objects.get(user=self.staff).secret, first)

    def test_a_password_cannot_plant_or_read_the_secret_of_another_session(self):
        """The attacker knows the password and keeps the set-up page fresh; the owner opens it in their own browser."""
        attacker = Client(**EN)
        with clock(T):
            self.password(attacker)
        attacker.get(SETUP)
        planted = AdminSecondFactor.objects.get(user=self.staff)
        # The owner's page shows another secret, and the attacker's one is gone
        owner_page = self.client.get(SETUP)
        self.assertNotContains(owner_page, totp.grouped(planted.secret))
        owners = AdminSecondFactor.objects.get(user=self.staff)
        self.assertNotEqual(owners.secret, planted.secret)
        # Back on the attacker's side: a new secret again, never the owner's
        self.assertNotContains(attacker.get(SETUP), totp.grouped(owners.secret))
        attackers = AdminSecondFactor.objects.get(user=self.staff)
        # The owner's confirm, with a code of the secret the owner was shown, confirms nothing: that secret is gone
        with clock(T):
            answer = self.client.post(SETUP, {"action": "on", "code": code(owners)})
        self.assertRedirects(answer, SETUP, fetch_redirect_response=False)
        self.assertFalse(AdminSecondFactor.objects.filter(confirmed__isnull=False).exists())
        # Nor can the owner's session confirm the attacker's secret, which it was never shown
        with clock(T):
            self.client.post(SETUP, {"action": "on", "code": code(attackers)})
        self.assertFalse(AdminSecondFactor.objects.filter(confirmed__isnull=False).exists())
        self.assertNotIn(FACTOR_SESSION, self.client.session)

    def test_a_get_from_another_session_replaces_it(self):
        self.client.get(SETUP)
        first = AdminSecondFactor.objects.get(user=self.staff)
        other = Client(**EN)
        with clock(T):
            self.password(other)
        other.get(SETUP)
        self.assertFalse(AdminSecondFactor.objects.filter(pk=first.pk).exists())
        with clock(T):
            self.client.post(SETUP, {"action": "on", "code": code(first)})
        self.assertFalse(AdminSecondFactor.objects.filter(confirmed__isnull=False).exists())

    def test_a_session_that_signs_in_again_gets_a_new_secret(self):
        self.client.get(SETUP)
        first = AdminSecondFactor.objects.get(user=self.staff).secret
        with clock(T):
            self.password()
        self.assertNotContains(self.client.get(SETUP), totp.grouped(first))

    def test_turning_it_on_is_told(self):
        self.client.get(SETUP)
        waiting = AdminSecondFactor.objects.get(user=self.staff)
        with clock(T), self.captureOnCommitCallbacks(execute=True):
            self.client.post(SETUP, {"action": "on", "code": code(waiting)})
        texts = self.told_texts()
        self.assertEqual(len(texts), 1)
        self.assertIn("The staff account volunteer turned its second factor on.", texts[0])
        self.assertNotIn(waiting.secret, texts[0])

    def test_no_page_shows_the_secret_after_the_set_up(self):
        self.client.get(SETUP)
        waiting = AdminSecondFactor.objects.get(user=self.staff)
        with clock(T):
            self.client.post(SETUP, {"action": "on", "code": code(waiting)})
            pages = [SETUP, "/admin/", f"/admin/auth/user/{self.staff.pk}/change/",
                     f"/admin/auth/user/{self.staff.pk}/history/", "/admin/auth/user/"]
            self.staff.is_superuser = True
            self.staff.save()
            for url in pages:
                page = self.client.get(url)
                self.assertEqual(page.status_code, 200, url)
                text = page.content.decode()
                self.assertNotIn(waiting.secret, text, url)
                self.assertNotIn(totp.grouped(waiting.secret), text, url)
                self.assertNotIn("otpauth://", text, url)
            self.assertContains(self.client.get(SETUP), "is on since")

    def test_the_secret_cannot_be_asked_for_through_the_list_of_users(self):
        self.staff.is_superuser = True
        self.staff.save()
        turn_on(User.objects.create_user("other", is_staff=True))
        secret = AdminSecondFactor.objects.get().secret
        page = self.client.get(f"/admin/auth/user/?admin_second_factor__secret__startswith={secret[:1]}")
        self.assertEqual(page.status_code, 400)

    def test_the_model_is_not_in_the_admin(self):
        self.assertNotIn(AdminSecondFactor, admin.site._registry)
        with self.assertRaises(NoReverseMatch):
            reverse("admin:shops_adminsecondfactor_changelist")

    def test_the_set_up_is_written_in_the_history_of_the_account(self):
        self.client.get(SETUP)
        waiting = AdminSecondFactor.objects.get(user=self.staff)
        with clock(T):
            self.client.post(SETUP, {"action": "on", "code": code(waiting)})
        entry = LogEntry.objects.get()
        self.assertEqual((entry.user_id, entry.object_id, entry.change_message),
                         (self.staff.pk, str(self.staff.pk), "Second factor turned on"))
        # A staff account with no shop is not in the register of the owners' accounts, as for a change of its password
        self.assertFalse(AccountChange.objects.exists())

    def test_the_page_answers_only_the_admins_session(self):
        browser = Client(**EN)
        browser.force_login(self.staff)  # a session from the email link or a wallet: not the admin's
        self.assertRedirects(browser.get(SETUP), f"{LOGIN}?next={SETUP}", fetch_redirect_response=False)
        self.assertFalse(AdminSecondFactor.objects.filter(user=self.staff).exists())


class OwnerOfAShop(Base):
    """A staff account that owns a shop is in the register of the owners' accounts: the change is written there, from
    the admin or from the command, as a change of its password is, and told as such changes are."""

    def setUp(self):
        super().setUp()
        Profile.objects.create(user=self.staff, email="volunteer@example.org")
        Shop.objects.create(owner=self.staff, name="Bar", telegram_chat="111")
        patcher = patch("shops.guard.telegram", return_value=True)
        self.telegram = patcher.start()
        self.addCleanup(patcher.stop)

    def test_turned_on_from_the_admin(self):
        with clock(T):
            self.password()
            self.client.get(SETUP)
            waiting = AdminSecondFactor.objects.get(user=self.staff)
            with self.captureOnCommitCallbacks(execute=True):
                self.client.post(SETUP, {"action": "on", "code": code(waiting)})
        row = AccountChange.objects.get()
        self.assertEqual((row.user, row.changed, row.via, row.actor, row.shops),
                         (self.staff, "second_factor", "admin", "volunteer", "Bar"))
        self.assertIn("email", row.told)

    def test_reset_with_the_command(self):
        turn_on(self.staff)
        with self.captureOnCommitCallbacks(execute=True):
            call_command("admin_totp", "reset", "volunteer", stdout=StringIO())
        row = AccountChange.objects.get()
        self.assertEqual((row.changed, row.via, row.actor), ("second_factor", "command", ""))


# ---------------------------------------------------------------------------------------------------------------------
# The sign-in
# ---------------------------------------------------------------------------------------------------------------------

def admin_urls():
    """Every page of the admin that can be named without a row: the index, each app's and each model's list and add
    page, the account's own pages, the second factor's set-up, and the association's list."""
    urls = {reverse("admin:index"), reverse("admin:password_change"), reverse("admin:password_change_done"),
            reverse("admin:jsi18n"), reverse("admin:autocomplete"), SETUP, "/admin/today/"}
    for model in admin.site._registry:
        opts = model._meta
        urls.add(reverse("admin:app_list", args=[opts.app_label]))
        urls.add(reverse(f"admin:{opts.app_label}_{opts.model_name}_changelist"))
        urls.add(reverse(f"admin:{opts.app_label}_{opts.model_name}_add"))
    return sorted(urls)


class SignIn(Base):
    def setUp(self):
        super().setUp()
        self.factor = turn_on(self.staff)

    def test_password_then_code(self):
        with clock(T):
            first = self.password()
            self.assertRedirects(first, CODE, fetch_redirect_response=False)
            self.assertFalse(signed_in(self.client))
            pending_key = self.client.session.session_key
            page = self.client.get(CODE)
            self.assertContains(page, "The password of volunteer is right")
            second = self.client.post(CODE, {"code": code(self.factor)})
            self.assertRedirects(second, "/admin/", fetch_redirect_response=False)
            self.assertTrue(signed_in(self.client))
            session = self.client.session
            self.assertTrue(session[ADMIN_SESSION])
            self.assertEqual(session[FACTOR_SESSION], ["totp", self.factor.pk, T])
            self.assertNotIn(PENDING, session)
            # A new key when the code is accepted
            self.assertNotEqual(session.session_key, pending_key)
            self.assertEqual(self.client.get("/admin/").status_code, 200)
            self.assertIn("admin_device", second.cookies)

    def test_the_password_alone_reaches_nothing(self):
        urls = admin_urls()
        self.assertGreater(len(urls), 20)
        with clock(T):
            self.password(next="/admin/shops/shop/")
            for url in urls:
                for method in ("get", "post"):
                    answer = getattr(self.client, method)(url)
                    self.assertEqual(answer.status_code, 302, f"{method} {url}")
                    self.assertTrue(answer["Location"].startswith(LOGIN), f"{method} {url}")
            # Outside /admin/ the browser is a visitor not signed in, with no staff behind it either
            self.assertFalse(signed_in(self.client))
            # The `next` is kept for after the code, and only then
            self.assertRedirects(self.client.post(CODE, {"code": code(self.factor)}), "/admin/shops/shop/",
                                 fetch_redirect_response=False)

    def test_a_next_elsewhere_ends_on_the_index(self):
        answer = self.sign_in(self.factor, next="https://evil.example/")
        # Where Django's own sign-in goes with such a `next` (LOGIN_REDIRECT_URL), never elsewhere
        self.assertEqual(answer.status_code, 302)
        self.assertEqual(answer["Location"], settings.LOGIN_REDIRECT_URL)

    def test_a_signed_in_merchant_is_signed_out_by_the_password(self):
        merchant = User.objects.create_user("merchant")
        self.client.force_login(merchant)
        with clock(T):
            self.password()
        self.assertFalse(signed_in(self.client))
        self.assertNotIn(ADMIN_SESSION, self.client.session)

    def test_a_code_is_not_accepted_twice(self):
        with clock(T):
            self.assertRedirects(self.sign_in(self.factor), "/admin/", fetch_redirect_response=False)
            again = Client(**EN)
            self.password(again)
            replay = again.post(CODE, {"code": code(self.factor)})
            self.assertContains(replay, "That code is not right, or it was already used")
            self.assertFalse(signed_in(again))
            # Nor an earlier one
            self.assertContains(again.post(CODE, {"code": code(self.factor, steps=-1)}), "not right")
        with clock(T + 30):
            self.assertRedirects(again.post(CODE, {"code": code(self.factor, T + 30)}), "/admin/",
                                 fetch_redirect_response=False)

    def test_the_step_window(self):
        for steps, accepted in ((-2, False), (-1, True), (1, True), (2, False)):
            AdminSecondFactor.objects.filter(pk=self.factor.pk).update(last_step=0)
            browser = Client(**EN)
            with clock(T):
                self.password(browser)
                browser.post(CODE, {"code": code(self.factor, steps=steps)})
            self.assertEqual(signed_in(browser), accepted, steps)
            reset_limits()

    def known_browser(self):
        """A browser that signed in with this second factor: its cookie is a known device of it."""
        browser = Client(**EN)
        self.sign_in(self.factor, T - 60, client=browser)
        browser.post("/admin/logout/")
        reset_limits()
        return browser

    def test_wrong_codes_count_against_the_addresss_limit(self):
        self.client = self.known_browser()
        with steady_limits(), clock(T):
            self.password()
            # The password was one try of the address's ten; a known browser has no budget of codes, only these
            for _ in range(9):
                self.assertEqual(self.client.post(CODE, {"code": "000000"}).status_code, 200)
            refused = self.client.post(CODE, {"code": code(self.factor)})
            self.assertEqual(refused.status_code, 429)
            self.assertIn("Retry-After", refused)
            self.assertContains(refused, "Too many wrong codes", status_code=429)
            self.assertFalse(signed_in(self.client))

    def test_wrong_codes_have_a_budget_of_their_own(self):
        with steady_limits(), clock(T):
            self.password(REMOTE_ADDR="203.0.113.200")
            # Each from another address: only the account's five codes in 15 minutes count
            for number in range(5):
                answer = self.client.post(CODE, {"code": "000000"}, REMOTE_ADDR=f"203.0.113.{number}")
                self.assertEqual(answer.status_code, 200, number)
            refused = self.client.post(CODE, {"code": code(self.factor)}, REMOTE_ADDR="203.0.113.100")
            self.assertEqual(refused.status_code, 429)
            self.assertFalse(signed_in(self.client))
            # Apart from the passwords': the password's page still lets the account try
            self.assertRedirects(self.password(Client(**EN), REMOTE_ADDR="203.0.113.101"), CODE,
                                 fetch_redirect_response=False)

    def test_the_two_windows_of_the_budget(self):
        day = 86400 * (T // 86400) + 3600
        hands = _Clock(day)
        with patch("config.ratelimit.secrets.randbelow", return_value=1), patch("config.ratelimit.time", hands), \
                clock(T):
            addresses = iter(range(200))
            for window in range(4):
                hands.now = day + window * 900
                self.password(REMOTE_ADDR=f"203.0.113.{next(addresses)}")
                for _ in range(5):
                    answer = self.client.post(CODE, {"code": "000000"}, REMOTE_ADDR=f"203.0.113.{next(addresses)}")
                    self.assertEqual(answer.status_code, 200, window)
                over = self.client.post(CODE, {"code": "000000"}, REMOTE_ADDR=f"203.0.113.{next(addresses)}")
                self.assertEqual(over.status_code, 429, window)
                self.assertLessEqual(int(over["Retry-After"]), 901)
            # 20 in the day: a fifth window of 15 minutes gives none, until the next day
            hands.now = day + 4 * 900
            self.password(REMOTE_ADDR=f"203.0.113.{next(addresses)}")
            refused = self.client.post(CODE, {"code": code(self.factor)}, REMOTE_ADDR=f"203.0.113.{next(addresses)}")
            self.assertEqual(refused.status_code, 429)
            self.assertGreater(int(refused["Retry-After"]), 901)
            hands.now = day + 86400
            self.password(REMOTE_ADDR=f"203.0.113.{next(addresses)}")
            right = self.client.post(CODE, {"code": code(self.factor)}, REMOTE_ADDR=f"203.0.113.{next(addresses)}")
            self.assertRedirects(right, "/admin/", fetch_redirect_response=False)

    def test_a_known_browser_of_the_factor_has_no_budget_of_codes(self):
        browser = self.known_browser()
        with steady_limits(), clock(T):
            self.password(browser, REMOTE_ADDR="203.0.113.200")
            for number in range(8):
                answer = browser.post(CODE, {"code": "000000"}, REMOTE_ADDR=f"203.0.113.{number}")
                self.assertEqual(answer.status_code, 200, number)
            self.assertRedirects(browser.post(CODE, {"code": code(self.factor)}, REMOTE_ADDR="203.0.113.99"), "/admin/",
                                 fetch_redirect_response=False)

    def test_a_cookie_from_before_the_factor_is_not_a_known_device(self):
        """The reviewer's case: a browser signed in with the password alone, then the account turned its factor on."""
        AdminSecondFactor.objects.all().delete()
        old = Client(**EN)
        self.assertRedirects(self.password(old), "/admin/", fetch_redirect_response=False)
        self.assertIn("admin_device", old.cookies)
        old.post("/admin/logout/")
        self.factor = turn_on(self.staff)
        reset_limits()
        with steady_limits(), clock(T):
            self.password(old, REMOTE_ADDR="203.0.113.200")
            answers = [old.post(CODE, {"code": "000000"}, REMOTE_ADDR=f"203.0.113.{number}").status_code
                       for number in range(40)]
        self.assertEqual(answers, [200] * 5 + [429] * 35)
        # Nor on the password's page: the account's twenty wrong passwords refuse that browser too
        reset_limits()
        with steady_limits():
            for number in range(20):
                old.post(LOGIN, {"username": "volunteer", "password": "wrong", "next": "/admin/"},
                         REMOTE_ADDR=f"198.51.100.{number}")
            self.assertEqual(self.password(old, REMOTE_ADDR="198.51.100.200").status_code, 429)

    def test_a_cookie_of_another_factor_is_not_a_known_device(self):
        browser = self.known_browser()
        AdminSecondFactor.objects.all().delete()
        self.factor = turn_on(self.staff)
        with steady_limits(), clock(T):
            self.password(browser, REMOTE_ADDR="203.0.113.200")
            answers = [browser.post(CODE, {"code": "000000"}, REMOTE_ADDR=f"203.0.113.{number}").status_code
                       for number in range(6)]
        self.assertEqual(answers, [200] * 5 + [429])

    def test_the_first_wrong_code_of_a_day_is_written_and_told_once(self):
        day = 86400 * (T // 86400) + 3600
        hands = _Clock(day)
        with patch("config.ratelimit.secrets.randbelow", return_value=1), patch("config.ratelimit.time", hands), \
                clock(T):
            self.password()
            first = self.client.post(CODE, {"code": "000000"})
            self.client.post(CODE, {"code": "000001"})
            # The page says nothing more than for any wrong code
            self.assertContains(first, "That code is not right")
            self.assertNotContains(first, "known")
            texts = self.told_texts()
            self.assertEqual(len(texts), 1)
            self.assertIn("A wrong second-factor code after the right password for the staff account volunteer: its "
                          "password may be known to someone else. Change it.", texts[0])
            self.assertEqual(list(LogEntry.objects.values_list("change_message", flat=True)),
                             ["A wrong second-factor code after the right password"])
            hands.now = day + 86400
            self.password()
            self.client.post(CODE, {"code": "000002"})
            self.assertEqual(len(self.told_texts()), 2)

    def test_a_message_that_cannot_go_does_not_fail_the_page(self):
        self.told.side_effect = RuntimeError("boom")
        with clock(T), self.assertLogs("config.second_factor", "ERROR"):
            self.password()
            self.assertContains(self.client.post(CODE, {"code": "000000"}), "That code is not right")

    def test_the_right_code_gives_the_accounts_try_back(self):
        with steady_limits():
            for number in range(25):
                AdminSecondFactor.objects.filter(pk=self.factor.pk).update(last_step=0)
                browser = Client(**EN)
                answer = self.sign_in(self.factor, client=browser, REMOTE_ADDR=f"203.0.113.{number}")
                self.assertRedirects(answer, "/admin/", fetch_redirect_response=False)

    def test_the_wait_for_the_code_ends(self):
        with clock(T):
            self.password()
        with clock(T + PENDING_SECONDS + 1):
            gone = self.client.post(CODE, {"code": code(self.factor, T + PENDING_SECONDS + 1)})
            self.assertRedirects(gone, LOGIN, fetch_redirect_response=False)
            self.assertFalse(signed_in(self.client))
            self.assertNotIn(PENDING, self.client.session)

    def test_a_new_password_or_a_reset_ends_the_wait(self):
        with clock(T):
            self.password()
            self.staff.set_password("another long password 77")
            self.staff.save()
            self.assertRedirects(self.client.post(CODE, {"code": code(self.factor)}), LOGIN, fetch_redirect_response=False)
            self.assertFalse(signed_in(self.client))
        self.staff.set_password(PASSWORD)
        self.staff.save()
        with clock(T):
            self.password()
        call_command("admin_totp", "reset", "volunteer", stdout=StringIO())
        with clock(T):
            self.assertRedirects(self.client.post(CODE, {"code": code(self.factor)}), LOGIN, fetch_redirect_response=False)
        self.assertFalse(signed_in(self.client))

    def test_the_code_page_without_a_password_first(self):
        self.assertRedirects(self.client.get(CODE), LOGIN, fetch_redirect_response=False)
        with clock(T):
            self.assertRedirects(self.client.post(CODE, {"code": code(self.factor)}), LOGIN, fetch_redirect_response=False)
        self.assertFalse(signed_in(self.client))

    def test_the_wait_of_one_account_does_not_open_another(self):
        other = User.objects.create_user("other", password=PASSWORD, is_staff=True)
        other_factor = turn_on(other)
        with clock(T):
            self.password()
            # Whatever is posted, the code is checked against the account that passed the password
            answer = self.client.post(CODE, {"code": code(other_factor), "username": "other"})
            self.assertContains(answer, "not right")
            self.assertFalse(signed_in(self.client))

    def test_a_session_from_the_password_alone_ends_when_the_account_has_a_second_factor(self):
        """Opened before the second factor was turned on (in another browser), or with the previous release."""
        AdminSecondFactor.objects.all().delete()
        with clock(T):
            self.password()
            self.assertEqual(self.client.get("/admin/").status_code, 200)
            turn_on(self.staff)
            self.assertRedirects(self.client.get("/admin/"), f"{LOGIN}?next=/admin/", fetch_redirect_response=False)
            self.assertFalse(signed_in(self.client))

    def test_a_moderator_signs_in_the_same_way(self):
        moderator = User.objects.create_user("moderator", password=PASSWORD)
        call_command("moderators", "add", "moderator", stdout=StringIO())
        factor = turn_on(moderator)
        with clock(T):
            self.assertRedirects(self.password(username="moderator"), CODE, fetch_redirect_response=False)
            self.assertEqual(self.client.get("/admin/today/").status_code, 302)
            self.assertRedirects(self.client.post(CODE, {"code": code(factor)}), "/admin/", fetch_redirect_response=False)
            self.assertEqual(self.client.get("/admin/today/").status_code, 200)


class OutsideTheAdmin(Base):
    """A staff session is staff outside /admin/ too (config/guard.py, staff_side): the pages marked `visibility: staff`,
    the e-mail limits, the line of the sign-in page. A session the second factor no longer holds is none of these."""

    def test_an_old_session_of_another_browser_is_not_staff_once_the_factor_is_on(self):
        browser_b = Client(**EN)
        self.assertRedirects(self.password(browser_b), "/admin/", fetch_redirect_response=False)
        self.assertContains(browser_b.get("/en/shops/"), 'id="staff-apart"')
        # Browser A turns the factor on
        self.password()
        self.client.get(SETUP)
        waiting = AdminSecondFactor.objects.get(user=self.staff)
        with clock(T):
            self.client.post(SETUP, {"action": "on", "code": code(waiting)})
            page = browser_b.get("/en/shops/")
            self.assertNotContains(page, 'id="staff-apart"')
            self.assertFalse(signed_in(browser_b))
            # Browser A, with the factor, still is
            self.assertContains(self.client.get("/en/shops/"), 'id="staff-apart"')

    def test_without_a_factor_nothing_changes(self):
        self.password()
        with clock(T + 40 * 86400):
            self.assertContains(self.client.get("/en/shops/"), 'id="staff-apart"')
        self.assertTrue(signed_in(self.client))

    def test_past_its_hours_a_session_is_not_staff(self):
        factor = turn_on(self.staff)
        self.sign_in(factor)
        with clock(T + 60):
            self.assertContains(self.client.get("/en/shops/"), 'id="staff-apart"')
        with clock(T + 24 * 3600):
            self.assertNotContains(self.client.get("/en/shops/"), 'id="staff-apart"')
        self.assertFalse(signed_in(self.client))


class Plumbing(TestCase):
    def test_a_new_sign_in_takes_the_marks_of_the_second_factor_away_with_the_admins(self):
        from django.test import RequestFactory

        from .guard import (
            PASSKEY_SETUP_SESSION,
            SETUP_SESSION,
            a_new_sign_in_ends_the_admin_session,
        )

        request = RequestFactory().get("/")
        request.session = {ADMIN_SESSION: True, FACTOR_SESSION: ["totp", 1, T], SETUP_SESSION: 2, PASSKEY_SETUP_SESSION: ["c", T, "h"], "other": 3}
        a_new_sign_in_ends_the_admin_session(None, request, None)
        self.assertEqual(request.session, {"other": 3})

    def test_the_thread_closes_its_database_connections(self):
        from .second_factor import in_background

        done = []

        class Inline:
            def __init__(self, target, daemon):
                self.target = target

            def start(self):
                self.target()

        with patch("config.second_factor.threading.Thread", Inline), \
                patch("django.db.connections.close_all") as close_all:
            in_background(lambda: done.append(1))
            self.assertEqual((done, close_all.call_count), ([1], 1))
            with self.assertRaises(RuntimeError):
                in_background(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
            self.assertEqual(close_all.call_count, 2)


class ResetSaysWhoWasTold(TransactionTestCase):
    """The command tells the moderators after its commit, synchronously, and says how it went."""

    def setUp(self):
        self.staff = User.objects.create_user("volunteer", password=PASSWORD, is_staff=True)
        turn_on(self.staff)

    def reset(self, **answer):
        out = StringIO()
        with patch("shops.notifications.deliver_to_moderators", **answer):
            call_command("admin_totp", "reset", "volunteer", stdout=out)
        return out.getvalue()

    def test_told(self):
        self.assertIn("told: TELEGRAM_MODERATION_CHAT", self.reset(return_value=("TELEGRAM_MODERATION_CHAT", [])))

    def test_not_told(self):
        text = self.reset(return_value=(None, ["Telegram refused it"]))
        self.assertIn("not told: Telegram refused it", text)


class OneDay(Base):
    def setUp(self):
        super().setUp()
        self.factor = turn_on(self.staff)

    def test_the_session_ends_a_day_after_it_was_opened(self):
        self.sign_in(self.factor)
        with clock(T + 24 * 3600 - 1):
            self.assertEqual(self.client.get("/admin/").status_code, 200)
        with clock(T + 24 * 3600):
            self.assertRedirects(self.client.get("/admin/shops/shop/"), f"{LOGIN}?next=/admin/shops/shop/",
                                 fetch_redirect_response=False)
            self.assertFalse(signed_in(self.client))
            # Password and code again
            self.assertRedirects(self.password(), CODE, fetch_redirect_response=False)

    @override_settings(ADMIN_SESSION_HOURS=1)
    def test_the_hours_are_a_setting(self):
        self.sign_in(self.factor)
        with clock(T + 3600):
            self.assertEqual(self.client.get("/admin/").status_code, 302)

    def test_a_session_from_the_password_alone_is_as_it_was(self):
        AdminSecondFactor.objects.all().delete()
        with clock(T):
            self.password()
        with clock(T + 30 * 24 * 3600):
            self.assertEqual(self.client.get("/admin/").status_code, 200)
            self.assertNotIn(FACTOR_SESSION, self.client.session)


class TurnOff(Base):
    def setUp(self):
        super().setUp()
        self.factor = turn_on(self.staff)
        self.sign_in(self.factor)

    def test_it_needs_a_right_code(self):
        with clock(T + 60):
            wrong = self.client.post(SETUP, {"action": "off", "code": "123456"})
            self.assertContains(wrong, "That code is not right")
            self.assertTrue(AdminSecondFactor.objects.filter(pk=self.factor.pk).exists())
            with self.captureOnCommitCallbacks(execute=True):
                right = self.client.post(SETUP, {"action": "off", "code": code(self.factor, T + 60)})
            self.assertRedirects(right, SETUP, fetch_redirect_response=False)
            self.assertEqual(len(self.told_texts()), 1)
            self.assertIn("The staff account volunteer turned its second factor off.", self.told_texts()[0])
            self.assertFalse(AdminSecondFactor.objects.exists())
            # The session goes on, with the password alone
            self.assertEqual(self.client.get("/admin/").status_code, 200)
            self.assertNotIn(FACTOR_SESSION, self.client.session)
        self.assertEqual(LogEntry.objects.get().change_message, "Second factor turned off")
        # The next sign-in is the password alone
        self.assertRedirects(self.password(Client(**EN)), "/admin/", fetch_redirect_response=False)

    def test_the_code_used_to_sign_in_does_not_turn_it_off(self):
        with clock(T):
            self.client.post(SETUP, {"action": "off", "code": code(self.factor)})
        self.assertTrue(AdminSecondFactor.objects.filter(pk=self.factor.pk).exists())

    def test_another_superuser_can_neither_remove_nor_read_it(self):
        boss = User.objects.create_superuser("boss", password=PASSWORD)
        browser = Client(**EN)
        with clock(T + 60):
            self.password(browser, username="boss")
            # The page is about the account signed in: the boss has none, and gets a set-up of its own
            page = browser.get(SETUP)
            self.assertNotContains(page, totp.grouped(self.factor.secret))
            browser.post(SETUP, {"action": "off", "code": code(self.factor, T + 60)})
            self.assertTrue(AdminSecondFactor.objects.filter(pk=self.factor.pk, confirmed__isnull=False).exists())
            self.assertIsNone(AdminSecondFactor.objects.get(user=boss).confirmed)
            for url in (f"/admin/auth/user/{self.staff.pk}/change/", f"/admin/auth/user/{self.staff.pk}/history/"):
                self.assertNotContains(browser.get(url), self.factor.secret)


# ---------------------------------------------------------------------------------------------------------------------
# ADMIN_SECOND_FACTOR=required
# ---------------------------------------------------------------------------------------------------------------------

@override_settings(ADMIN_SECOND_FACTOR="required")
class Required(Base):
    def test_set_up_first_and_nothing_else(self):
        with clock(T):
            self.assertRedirects(self.password(), "/admin/", fetch_redirect_response=False)
            for url in ("/admin/", "/admin/today/", "/admin/shops/shop/", "/admin/password_change/"):
                self.assertRedirects(self.client.get(url), SETUP, fetch_redirect_response=False)
            page = self.client.get(SETUP)
            self.assertContains(page, "This site asks every staff account for a second factor")
            factor = AdminSecondFactor.objects.get(user=self.staff)
            self.client.post(SETUP, {"action": "on", "code": code(factor)})
            self.assertEqual(self.client.get("/admin/").status_code, 200)

    def test_the_sign_out_stays_open(self):
        with clock(T):
            self.password()
            answer = self.client.post("/admin/logout/")
            self.assertNotEqual(answer.get("Location"), SETUP)
            self.assertFalse(signed_in(self.client))

    def test_a_temporary_password_is_changed_first(self):
        call_command("temporary_password", "volunteer", stdout=StringIO(), stderr=StringIO())
        with clock(T):
            self.password()
            self.assertRedirects(self.client.get("/admin/"), "/admin/password_change/", fetch_redirect_response=False)
            self.assertRedirects(self.client.get(SETUP), "/admin/password_change/", fetch_redirect_response=False)
            self.assertEqual(self.client.get("/admin/password_change/").status_code, 200)
            new = "a brand new secret 8421 z"
            changed = self.client.post("/admin/password_change/", {"old_password": PASSWORD, "new_password1": new,
                                                                   "new_password2": new})
            self.assertEqual(changed.status_code, 302)
            self.assertRedirects(self.client.get("/admin/"), SETUP, fetch_redirect_response=False)
            self.assertEqual(self.client.get(SETUP).status_code, 200)

    def test_with_one_the_sign_in_is_as_in_optional(self):
        factor = turn_on(self.staff)
        self.assertRedirects(self.sign_in(factor), "/admin/", fetch_redirect_response=False)
        with clock(T):
            self.assertEqual(self.client.get("/admin/").status_code, 200)


class Optional(Base):
    def test_nothing_changes_for_an_account_without_one(self):
        self.assertEqual(settings.ADMIN_SECOND_FACTOR, "optional")
        answer = self.password()
        self.assertRedirects(answer, "/admin/", fetch_redirect_response=False)
        self.assertTrue(self.client.session[ADMIN_SESSION])
        self.assertNotIn(FACTOR_SESSION, self.client.session)
        for url in ("/admin/", "/admin/password_change/", SETUP):
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_the_admin_says_nothing_about_it(self):
        """Exactly as before any second factor existed: no page in between, no banner, no message; only the quiet link
        among the user tools."""
        answer = self.client.post(LOGIN, {"username": "volunteer", "password": PASSWORD, "next": "/admin/"},
                                  follow=True)
        self.assertEqual(answer.redirect_chain, [("/admin/", 302)])
        text = answer.content.decode()
        self.assertEqual(list(answer.context["messages"]), [])
        self.assertEqual(text.count("Second factor"), 1)
        self.assertIn('<a href="/admin/second-factor/">Second factor</a>', text)
        for word in ("passkey", "Passkey", "TOTP", "authenticator", "errornote"):
            self.assertNotIn(word, text, word)
        # Nor on the pages it goes through every day
        for url in ("/admin/shops/shop/", "/admin/today/", "/admin/password_change/"):
            page = self.client.get(url).content.decode()
            self.assertNotIn("passkey", page.lower(), url)
            self.assertNotIn("This site asks every staff account", page, url)


# ---------------------------------------------------------------------------------------------------------------------
# The command, preflight, the settings
# ---------------------------------------------------------------------------------------------------------------------

class Command(Base):
    def test_list(self):
        turn_on(User.objects.create_user("anna", is_staff=True))
        User.objects.create_user("merchant")
        out = StringIO()
        call_command("admin_totp", "list", stdout=out)
        text = out.getvalue()
        self.assertIn("Staff accounts: 2, with a second factor: 1", text)
        self.assertIn("  anna  on", text)
        self.assertIn("  volunteer  off", text)
        self.assertNotIn("merchant", text)
        self.assertNotIn(AdminSecondFactor.objects.get().secret, text)

    def test_reset(self):
        factor = turn_on(self.staff)
        self.sign_in(factor)
        out = StringIO()
        with self.captureOnCommitCallbacks(execute=True):
            call_command("admin_totp", "reset", "volunteer", stdout=out)
        self.assertIn("the second factor is removed", out.getvalue())
        self.assertEqual(len(self.told_texts()), 1)
        self.assertIn("The second factor of the staff account volunteer was reset with manage.py admin_totp reset.",
                      self.told_texts()[0])
        self.assertFalse(AdminSecondFactor.objects.exists())
        entry = LogEntry.objects.get()
        self.assertEqual(entry.change_message, "Second factor removed with manage.py admin_totp reset")
        # The sessions opened with it end
        with clock(T + 60):
            self.assertEqual(self.client.get("/admin/").status_code, 302)
        self.assertFalse(signed_in(self.client))
        # And the next sign-in is the password alone
        self.assertRedirects(self.password(Client(**EN)), "/admin/", fetch_redirect_response=False)

    def test_reset_of_an_account_without_one_or_of_nobody(self):
        out = StringIO()
        call_command("admin_totp", "reset", "volunteer", stdout=out)
        self.assertIn("has no second factor", out.getvalue())
        self.assertFalse(LogEntry.objects.exists())
        with self.assertRaises(CommandError):
            call_command("admin_totp", "reset", "nobody", stdout=StringIO())


@override_settings(SITE_HOST="shops.example.org")
class Preflight(Base):
    @override_settings(ADMIN_ALLOWED_IPS="", ADMIN_SECOND_FACTOR="optional")
    def test_open_to_every_address_without_required(self):
        notes = admin_notes()
        self.assertTrue(any("open to every address" in note for note in notes), notes)
        # Calm: what the stricter levels are for
        self.assertIn("right for trying the site with the team. For a community with real shops", " ".join(notes))
        self.assertIn("1 of 1 staff accounts have no second factor: each may add one", " ".join(notes))

    @override_settings(ADMIN_ALLOWED_IPS="", ADMIN_SECOND_FACTOR="optional")
    def test_nothing_on_a_local_try(self):
        for overrides in ({"SITE_HOST": "localhost"}, {"SITE_HOST": ""}, {"DEBUG": True}):
            with self.subTest(overrides), override_settings(**overrides):
                self.assertEqual(admin_notes(), [])

    @override_settings(ADMIN_ALLOWED_IPS="", ADMIN_SECOND_FACTOR="optional")
    def test_never_a_problem_in_optional(self):
        out, err = StringIO(), StringIO()
        try:
            call_command("preflight", stdout=out, stderr=err)
        except CommandError:
            pass
        self.assertNotIn("second factor", err.getvalue().lower())
        self.assertNotIn("ADMIN_SECOND_FACTOR", err.getvalue())
        out = StringIO()
        call_command("check", stdout=out, stderr=StringIO())
        self.assertNotIn("SECOND_FACTOR", out.getvalue())

    @override_settings(ADMIN_ALLOWED_IPS="", ADMIN_SECOND_FACTOR="required")
    def test_required_says_only_who_has_none(self):
        notes = admin_notes()
        self.assertEqual(len(notes), 1)
        self.assertIn("1 of 1 staff accounts have no second factor: they are sent to set one up", notes[0])
        turn_on(self.staff)
        self.assertEqual(admin_notes(), [])

    @override_settings(ADMIN_ALLOWED_IPS="203.0.113.7", ADMIN_SECOND_FACTOR="optional")
    def test_an_address_list_and_every_account_with_one(self):
        turn_on(self.staff)
        self.assertEqual(admin_notes(), [])

    @override_settings(ADMIN_ALLOWED_IPS="", ADMIN_SECOND_FACTOR="required")
    def test_preflight_prints_them(self):
        out, err = StringIO(), StringIO()
        try:
            call_command("preflight", stdout=out, stderr=err)
        except CommandError:
            pass
        self.assertIn("good to know: 1 of 1 staff accounts have no second factor", out.getvalue())


class Settings(TestCase):
    def start(self, **values):
        env = {**os.environ, "SECRET_KEY": "x", "DJANGO_SETTINGS_MODULE": "config.settings", **values}
        return subprocess.run([sys.executable, "-c", "import django; from django.conf import settings; settings.DEBUG"],
                              env=env, cwd=settings.BASE_DIR, capture_output=True, text=True, timeout=60, check=False)

    def test_a_bad_value_stops_the_start(self):
        for name, value, words in (("ADMIN_SECOND_FACTOR", "yes",
                                    "ADMIN_SECOND_FACTOR is 'yes': it must be optional, required or passkey"),
                                   ("ADMIN_SESSION_HOURS", "0", "ADMIN_SESSION_HOURS is '0'"),
                                   ("ADMIN_SESSION_HOURS", "1.5", "ADMIN_SESSION_HOURS is '1.5'")):
            result = self.start(**{name: value})
            self.assertNotEqual(result.returncode, 0, name)
            self.assertIn(words, result.stderr)

    def test_the_defaults(self):
        result = self.start(ADMIN_SECOND_FACTOR="", ADMIN_SESSION_HOURS="")
        self.assertEqual(result.returncode, 0, result.stderr)

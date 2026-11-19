"""Sign-in to /admin/ with a Lightning wallet (config/admin_wallet.py, ADMIN_WALLET_SIGN_IN): off, the levels, linking,
the sign-in and its binding to the browser, every refusal, one key one role, the sessions, the commands, the notices,
the setting, the translations and the script."""
import json
import os
import re
import subprocess
import sys
import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import bech32
import coincurve
from django.conf import settings
from django.contrib import admin
from django.contrib.admin.models import LogEntry
from django.contrib.auth import get_user_model
from django.core import signing
from django.core.management import CommandError, call_command
from django.test import (
    Client,
    SimpleTestCase,
    TestCase,
    TransactionTestCase,
    override_settings,
)
from django.urls import reverse
from django.utils import timezone

from shops.models import (
    AdminPasskey,
    AdminSecondFactor,
    AdminWallet,
    AdminWalletChallenge,
    LoginChallenge,
    WalletKey,
)
from shops.test_scripts import node_or_skip
from shops.tests import reset_limits, steady_limits
from team.models import TeamInvite, TeamKey

from . import admin_wallet, webauthn
from .guard import ADMIN_SESSION, WALLET_LINK_SESSION, WALLET_SESSION
from .second_factor import FACTOR_SESSION
from .test_passkeys import HOST, HTTPS, browser
from .test_second_factor import (
    LOGIN,
    PASSWORD,
    SETUP,
    T,
    clock,
    code,
    signed_in,
    turn_on,
)
from .urls import KNOWN_DEVICE_COOKIE, KNOWN_DEVICE_SALT

User = get_user_model()
WALLET_LOGIN = "/admin/login/wallet/"
STATUS = "/admin/login/wallet/status/"
LINK_STATUS = "/admin/second-factor/wallet/status/"
CALLBACK = "/lnurl-auth/staff/"
ON = override_settings(SITE_HOST=HOST, PUBLIC_URL=f"https://{HOST}", ADMIN_SECOND_FACTOR="optional",
                       ADMIN_WALLET_SIGN_IN=True)
OFF = override_settings(SITE_HOST=HOST, PUBLIC_URL=f"https://{HOST}", ADMIN_SECOND_FACTOR="optional",
                        ADMIN_WALLET_SIGN_IN=False)
REFUSED_LINK = "This wallet cannot be linked here: it already has another use on this site."
CSRF = re.compile(r'name="csrfmiddlewaretoken" value="[^"]*"')


def wallet():
    return coincurve.PrivateKey()


def pub(key):
    return key.public_key.format().hex()


def k1_of(lnurl):
    _prefix, data = bech32.bech32_decode(lnurl.lower())
    return re.search(r"[?&]k1=([0-9a-f]{64})", bytes(bech32.convertbits(data, 5, 8, False)).decode())[1]


def callback_of(lnurl):
    _prefix, data = bech32.bech32_decode(lnurl.lower())
    return bytes(bech32.convertbits(data, 5, 8, False)).decode()


def signed(k1, key):
    """What a wallet sends to the callback (LUD-04): a real secp256k1 signature of the challenge and its key."""
    return {"tag": "login", "k1": k1, "sig": key.sign(bytes.fromhex(k1), hasher=None).hex(), "key": pub(key)}


def make_known(client, user):
    """The `admin_device` cookie a right password leaves in the browser (config/urls.py, remember_device)."""
    user = User.objects.get(pk=user.pk)
    client.cookies[KNOWN_DEVICE_COOKIE] = signing.dumps({"user": user.pk, "auth": user.get_session_auth_hash()},
                                                        salt=KNOWN_DEVICE_SALT)


def phone():
    """The wallet's own HTTP client: another device, with no session of the browser."""
    return Client(HTTP_HOST=HOST, **HTTPS)


class Base(TestCase):
    def setUp(self):
        reset_limits()
        self.staff = User.objects.create_user("volunteer", password=PASSWORD, is_staff=True)
        self.client = browser()
        # The server's clock, for the codes and the sessions' day: T, unless a test moves it
        frozen = clock(T)
        frozen.start()
        self.addCleanup(frozen.stop)
        background = patch("config.second_factor.in_background", side_effect=lambda work: work())
        background.start()
        self.addCleanup(background.stop)
        told = patch("shops.notifications.deliver_to_moderators", return_value=("TELEGRAM_MODERATION_CHAT", []))
        self.told = told.start()
        self.addCleanup(told.stop)

    def told_texts(self):
        return [call.args[1] for call in self.told.call_args_list]

    def password(self, client=None, username="volunteer", next="/admin/"):
        return (client or self.client).post(LOGIN, {"username": username, "password": PASSWORD, "next": next})

    def linked(self, key, user=None, name="phone"):
        """A wallet of `user` (the staff account by default), stored as the page would."""
        return AdminWallet.objects.create(user=user or self.staff, linking_key=pub(key), name=name)

    def signs(self, lnurl, key):
        """The wallet signs the code of `lnurl`: the callback's answer."""
        self.assertEqual(callback_of(lnurl).split("?")[0], f"https://{HOST}{CALLBACK}")
        return phone().get(CALLBACK, signed(k1_of(lnurl), key)).json()

    def wallet_page(self, client=None, **query):
        page = (client or self.client).get(WALLET_LOGIN, query)
        self.assertEqual(page.status_code, 200)
        return page

    def sign_in(self, key, client=None, known=None, **query):
        """A browser signs in with the wallet: the poll's answer once the wallet signed. The browser is first made a
        known one of `known` (the staff account by default; False: not at all), as a sign-in with the password does."""
        client = client or self.client
        if known is not False:
            make_known(client, known or self.staff)
        page = self.wallet_page(client, **query)
        self.assertEqual(self.signs(page.context["lnurl"], key)["status"], "OK")
        return client.get(STATUS)

    def assert_signed_in(self, answer, client=None, next="/admin/"):
        self.assertEqual(answer.json(), {"state": "ok", "next": next})
        client = client or self.client
        self.assertTrue(signed_in(client))
        self.assertEqual(client.get("/admin/").status_code, 200)

    def link(self, key, client=None, name="phone"):
        """Links a wallet from the page, in a session already signed in: the answer of the confirmation."""
        client = client or self.client
        page = client.get(SETUP)
        self.assertEqual(page.status_code, 200)
        self.assertEqual(client.get(LINK_STATUS).json(), {"state": "pending"})
        self.assertEqual(self.signs(page.context["wallet_lnurl"], key)["status"], "OK")
        self.assertEqual(client.get(LINK_STATUS).json(), {"state": "signed"})
        self.assertContains(client.get(SETUP), admin_wallet.short_key(pub(key)))
        with self.captureOnCommitCallbacks(execute=True):
            return client.post(SETUP, {"action": "wallet-link", "name": name})


def without_csrf(response):
    return CSRF.sub("", response.content.decode())


# ---------------------------------------------------------------------------------------------------------------------
# Off, and the levels where a wallet does not sign in
# ---------------------------------------------------------------------------------------------------------------------

@OFF
class Off(Base):
    def test_nothing_of_it_exists(self):
        key = wallet()
        self.linked(key)
        for url in (WALLET_LOGIN, STATUS, CALLBACK, f"{CALLBACK}?{json.dumps(signed('ab' * 32, key))}"):
            self.assertEqual(self.client.get(url).status_code, 404, url)
        self.assertEqual(phone().post(CALLBACK).status_code, 404)
        login = self.client.get(LOGIN)
        self.assertNotIn("admin/login_with_wallet.html", [template.name for template in login.templates])
        self.assertNotContains(login, "wallet")
        self.password()
        self.assertEqual(self.client.get(LINK_STATUS).status_code, 404)
        page = self.client.get(SETUP)
        self.assertNotContains(page, "Sign in with a wallet")
        self.assertNotIn("wallet_on", page.context)
        self.assertFalse(AdminWalletChallenge.objects.exists())
        # A wallet's action posted anyway does nothing
        wallet_row = AdminWallet.objects.get()
        self.client.post(SETUP, {"action": "wallet-remove", "wallet": wallet_row.pk})
        self.client.post(SETUP, {"action": "wallet-rename", "wallet": wallet_row.pk, "name": "x"})
        self.assertEqual(AdminWallet.objects.get().name, "phone")

    def test_the_login_page_is_the_same_byte_for_byte(self):
        """Off, and on at a level where a wallet does not sign in: the page of the password alone, the same bytes but
        for the CSRF token. On with `optional`: that page and one link more."""
        off = without_csrf(self.client.get(LOGIN))
        for level in ("required", "passkey"):
            with override_settings(ADMIN_WALLET_SIGN_IN=True, ADMIN_SECOND_FACTOR=level):
                self.assertEqual(without_csrf(self.client.get(LOGIN)), off, level)
        with override_settings(ADMIN_WALLET_SIGN_IN=True):
            on = without_csrf(self.client.get(LOGIN, {"next": "/admin/auth/"}))
            link = ('\n<p class="admin-wallet-sign-in"><a href="/admin/login/wallet/?next=%2Fadmin%2Fauth%2F">'
                    "Sign in with your wallet</a></p>\n")
            self.assertIn(link, on)
        with override_settings(ADMIN_WALLET_SIGN_IN=False):
            plain = without_csrf(self.client.get(LOGIN, {"next": "/admin/auth/"}))
        self.assertEqual(on.replace(link, ""), plain)

    def test_a_password_sign_in_is_as_before(self):
        self.assertRedirects(self.password(), "/admin/", fetch_redirect_response=False)
        self.assertNotIn(WALLET_SESSION, self.client.session)
        self.assertEqual(self.client.get("/admin/").status_code, 200)


@ON
class Levels(Base):
    def test_required_and_passkey_never_sign_in_with_a_wallet(self):
        key = wallet()
        self.linked(key)
        for level in ("required", "passkey"):
            with self.subTest(level), override_settings(ADMIN_SECOND_FACTOR=level):
                self.assertEqual(self.client.get(WALLET_LOGIN).status_code, 404)
                self.assertEqual(self.client.get(STATUS).status_code, 404)
                self.assertNotContains(self.client.get(LOGIN), "Sign in with your wallet")
                # The page says so; the wallets stay
                client = browser()
                self.password(client)
                page = client.get(SETUP)
                self.assertContains(page, f"This site asks for more than one step (ADMIN_SECOND_FACTOR is {level}): "
                                          "the wallets linked here do not sign in.")
                self.assertContains(page, "phone")
        self.assertTrue(AdminWallet.objects.exists())

    def test_a_challenge_made_at_optional_does_not_sign_in_after_the_level_rose(self):
        key = wallet()
        self.linked(key)
        page = self.wallet_page()
        self.signs(page.context["lnurl"], key)
        with override_settings(ADMIN_SECOND_FACTOR="required"):
            self.assertEqual(self.client.get(STATUS).status_code, 404)
        self.assertFalse(signed_in(self.client))

    def test_the_check_warns_and_does_not_stop(self):
        from shops.apps import wallet_without_effect

        self.assertEqual(wallet_without_effect(None), [])
        with override_settings(ADMIN_WALLET_SIGN_IN=False, ADMIN_SECOND_FACTOR="passkey"):
            self.assertEqual(wallet_without_effect(None), [])
        for level in ("required", "passkey"):
            with override_settings(ADMIN_SECOND_FACTOR=level):
                found = wallet_without_effect(None)
                self.assertEqual([warning.id for warning in found], ["shops.W016"])
                self.assertFalse(found[0].is_serious())
                self.assertIn(f"ADMIN_SECOND_FACTOR is {level}", found[0].msg)


# ---------------------------------------------------------------------------------------------------------------------
# The sign-in
# ---------------------------------------------------------------------------------------------------------------------

@ON
class SignIn(Base):
    def test_link_code_signature_admin(self):
        key = wallet()
        self.password()
        self.assertRedirects(self.link(key), SETUP, fetch_redirect_response=False)
        row = AdminWallet.objects.get()
        self.assertEqual((row.user, row.linking_key, row.name), (self.staff, pub(key), "phone"))
        # Another browser, where the account signs in with its password once and leaves
        other = browser()
        self.password(other)
        other.post("/admin/logout/")
        self.assertFalse(signed_in(other))
        # The link on the login page, the code, the signature
        login = other.get(LOGIN, {"next": "/admin/auth/user/"})
        self.assertContains(login, 'href="/admin/login/wallet/?next=%2Fadmin%2Fauth%2Fuser%2F">Sign in with your wallet')
        page = self.wallet_page(other, next="/admin/auth/user/")
        self.assertRegex(page.content.decode(), r"admin-wallet(\.[0-9a-f]+)?\.js")  # with or without its fingerprint
        self.assertContains(page, 'data-status="/admin/login/wallet/status/"')
        self.assertContains(page, f'href="lightning:{page.context["lnurl"]}"')
        self.assertContains(page, "The first time on a browser, after a change of password, and for an account with a "
                                  "second factor, sign in with the password.")
        self.assertEqual(other.get(STATUS).json(), {"state": "pending"})
        before = other.session.session_key
        self.assertEqual(self.signs(page.context["lnurl"], key), {"status": "OK"})
        answer = other.get(STATUS)
        self.assert_signed_in(answer, other, next="/admin/auth/user/")
        self.assertNotEqual(other.session.session_key, before)
        self.assertTrue(other.session[ADMIN_SESSION])
        self.assertEqual(other.session[WALLET_SESSION], [row.pk, T])
        self.assertNotIn(admin_wallet.SIGN_IN_SESSION, other.session)
        self.assertIsNotNone(AdminWallet.objects.get().last_used)
        # The wallet does not renew the known browser: the password does
        self.assertNotIn(KNOWN_DEVICE_COOKIE, answer.cookies)
        # Written in the account's history, with no key; nobody is told of an ordinary sign-in
        entry = LogEntry.objects.filter(change_message=admin_wallet.SIGNED_IN).get()
        self.assertEqual((entry.user_id, entry.object_id), (self.staff.pk, str(self.staff.pk)))
        self.assertEqual([text for text in self.told_texts() if "linked" not in text], [])
        # The same wallet asking again hears OK, and nothing more happens
        self.assertEqual(self.signs(page.context["lnurl"], key), {"status": "OK"})

    def test_the_relay_signs_in_nobody(self):
        """Whoever opens the wallet's page in their own browser and gets the staff member to scan its code: the right
        wallet signs, and that browser, where the account never used its password, is refused like any other."""
        key = wallet()
        self.linked(key)
        thief = browser()
        answer = self.sign_in(key, thief, known=False)
        self.assertEqual(answer.json(), {"state": "refused"})
        self.assertFalse(signed_in(thief))
        self.assertEqual(thief.get("/admin/").status_code, 302)
        self.assertFalse(LogEntry.objects.exists())
        # A forged or another site's cookie is no known browser either
        thief = browser()
        thief.cookies[KNOWN_DEVICE_COOKIE] = signing.dumps({"user": self.staff.pk, "auth": "x"}, salt=KNOWN_DEVICE_SALT)
        self.assertEqual(self.sign_in(key, thief, known=False).json(), {"state": "refused"})
        thief = browser()
        thief.cookies[KNOWN_DEVICE_COOKIE] = "not signed"
        self.assertEqual(self.sign_in(key, thief, known=False).json(), {"state": "refused"})

    def test_the_cookie_of_one_account_does_not_let_anothers_wallet_in(self):
        anna = User.objects.create_user("anna", password=PASSWORD, is_staff=True)
        hers = wallet()
        self.linked(hers, anna)
        client = browser()
        self.assertEqual(self.sign_in(hers, client, known=self.staff).json(), {"state": "refused"})
        self.assertFalse(signed_in(client))
        client = browser()
        self.assert_signed_in(self.sign_in(hers, client, known=anna), client)
        self.assertEqual(int(client.session["_auth_user_id"]), anna.pk)

    def test_a_change_of_password_stops_the_wallets_until_it_is_used_again(self):
        key = wallet()
        self.linked(key)
        known = browser()
        self.password(known)
        known.post("/admin/logout/")
        self.staff.set_password("another long password 43")
        self.staff.save()
        # Neither that browser nor any other
        self.assertEqual(self.sign_in(key, known, known=False).json(), {"state": "refused"})
        self.assertFalse(signed_in(known))
        # The new password once in that browser: the same wallet, still linked, signs in there again
        known.post(LOGIN, {"username": "volunteer", "password": "another long password 43", "next": "/admin/"})
        known.post("/admin/logout/")
        self.assert_signed_in(self.sign_in(key, known, known=False), known)
        self.assertEqual(AdminWallet.objects.count(), 1)

    def test_a_change_of_password_ends_the_sessions_of_the_wallet(self):
        key = wallet()
        self.linked(key)
        self.assert_signed_in(self.sign_in(key))
        self.staff.set_password("another long password 43")
        self.staff.save()
        self.assertEqual(self.client.get("/admin/").status_code, 302)

    def test_where_it_goes_is_checked(self):
        key = wallet()
        self.linked(key)
        for target, expected in (("https://elsewhere.example/", "/admin/"), ("//elsewhere.example/", "/admin/"),
                                 ("", "/admin/"), ("/admin/shops/", "/admin/shops/")):
            with self.subTest(target):
                client = browser()
                self.assertEqual(self.sign_in(key, client, next=target).json(), {"state": "ok", "next": expected})

    def test_a_signed_in_admin_goes_to_the_index(self):
        self.password()
        self.assertRedirects(self.client.get(WALLET_LOGIN), "/admin/", fetch_redirect_response=False)

    def test_a_k1_signed_for_another_browser_signs_in_nobody_else(self):
        key = wallet()
        self.linked(key)
        mine, theirs, stranger = browser(), browser(), browser()
        for client in (mine, theirs, stranger):
            make_known(client, self.staff)
        mine_page = self.wallet_page(mine)
        self.wallet_page(theirs)
        # The thief signs the code of another browser's page: only that browser's session holds its k1
        self.signs(mine_page.context["lnurl"], key)
        self.assertEqual(theirs.get(STATUS).json(), {"state": "pending"})
        self.assertEqual(stranger.get(STATUS).json(), {"state": "expired"})
        # Neither the k1 in the address nor a cookie tells anything to another session
        self.assertEqual(theirs.get(STATUS, {"k1": k1_of(mine_page.context["lnurl"])}).json(), {"state": "pending"})
        theirs.cookies["k1"] = k1_of(mine_page.context["lnurl"])
        self.assertEqual(theirs.get(STATUS).json(), {"state": "pending"})
        self.assertFalse(signed_in(theirs) or signed_in(stranger))
        self.assert_signed_in(mine.get(STATUS), mine)

    def test_the_k1_is_used_once(self):
        key = wallet()
        self.linked(key)
        self.assert_signed_in(self.sign_in(key))
        self.assertEqual(self.client.get(STATUS).json(), {"state": "expired"})
        self.assertEqual(AdminWalletChallenge.objects.filter(used__isnull=False).count(), 1)

    def test_two_polls_at_the_same_moment_sign_in_once(self):
        key = wallet()
        self.linked(key)
        make_known(self.client, self.staff)
        page = self.wallet_page()
        self.signs(page.context["lnurl"], key)
        # The other poll took it a moment before
        real = AdminWalletChallenge.objects.filter

        def racing(*args, **kwargs):
            found = real(*args, **kwargs)
            if "used" in kwargs:
                real(pk=kwargs["pk"]).update(used=timezone.now())
            return found

        with patch.object(AdminWalletChallenge.objects, "filter", side_effect=racing):
            self.assertEqual(self.client.get(STATUS).json(), {"state": "expired"})
        self.assertFalse(signed_in(self.client))

    def test_an_expired_k1(self):
        key = wallet()
        self.linked(key)
        make_known(self.client, self.staff)
        page = self.wallet_page()
        self.signs(page.context["lnurl"], key)
        with clock(T + admin_wallet.CHALLENGE_SECONDS):
            self.assertEqual(self.client.get(STATUS).json(), {"state": "expired"})
        # A code past its time: the wallet itself is refused, and nothing is written
        page = self.wallet_page(browser())
        with clock(T + admin_wallet.CHALLENGE_SECONDS):
            self.assertEqual(self.signs(page.context["lnurl"], key)["status"], "ERROR")
        self.assertFalse(signed_in(self.client))
        self.assertFalse(AdminWalletChallenge.objects.filter(k1=k1_of(page.context["lnurl"])).exists())

    def test_wrong_k1_and_tampered_signatures(self):
        key = wallet()
        self.linked(key)
        page = self.wallet_page()
        k1 = k1_of(page.context["lnurl"])
        good = signed(k1, key)
        tampered = good["sig"][:-2] + ("00" if good["sig"][-2:] != "00" else "01")
        forged = k1[:40] + ("0" if k1[40] != "0" else "1") + k1[41:]
        for query in ({**good, "k1": "ab" * 32}, {**good, "k1": "zz"}, {**good, "k1": k1.upper()},
                      signed(forged, key), {**good, "sig": tampered}, {**good, "sig": "zz"}, {**good, "sig": ""},
                      {**good, "key": pub(wallet())}, {**good, "key": "04" + good["key"][2:]},
                      {**good, "key": "xyz"}, {"k1": k1}, {}):
            with self.subTest(query):
                answer = phone().get(CALLBACK, query)
                self.assertEqual(answer.status_code, 200)
                self.assertEqual(answer.json(), {"status": "ERROR", "reason": "Invalid login"})
        self.assertEqual(self.client.get(STATUS).json(), {"state": "pending"})
        self.assertFalse(AdminWalletChallenge.objects.exists())
        self.assertEqual(phone().post(CALLBACK, good).status_code, 405)

    def test_a_second_key_cannot_take_a_signed_k1(self):
        mine, thief = wallet(), wallet()
        self.linked(mine)
        boss = User.objects.create_user("boss", password=PASSWORD, is_staff=True)
        self.linked(thief, boss)
        make_known(self.client, self.staff)
        page = self.wallet_page()
        self.assertEqual(self.signs(page.context["lnurl"], mine)["status"], "OK")
        self.assertEqual(self.signs(page.context["lnurl"], thief)["status"], "ERROR")
        self.assert_signed_in(self.client.get(STATUS))
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.staff.pk)

    def test_every_refusal_answers_the_same_and_makes_nothing(self):
        plain_user = User.objects.create_user("plain", password=PASSWORD)
        gone = User.objects.create_user("gone", password=PASSWORD, is_staff=True, is_active=False)
        with_code = User.objects.create_user("coded", password=PASSWORD, is_staff=True)
        with_passkey = User.objects.create_user("keyed", password=PASSWORD, is_staff=True)
        turn_on(with_code)
        AdminPasskey.objects.create(user=with_passkey, credential_id="x", public_key=b"", algorithm=webauthn.ES256)
        unknown, of_plain, of_gone, of_code, of_passkey, mine = (wallet() for _ in range(6))
        for key, user in ((of_plain, plain_user), (of_gone, gone), (of_code, with_code), (of_passkey, with_passkey),
                          (mine, self.staff)):
            self.linked(key, user)
        users, wallets = User.objects.count(), AdminWallet.objects.count()
        answers = []
        # A key nobody linked; of an account not staff, not active, with a code, with a passkey; a browser that is not
        # a known one of the account
        for key, known in ((unknown, self.staff), (of_plain, plain_user), (of_gone, gone), (of_code, with_code),
                           (of_passkey, with_passkey), (mine, False)):
            client = browser()
            answer = self.sign_in(key, client, known=known)
            answers.append((answer.status_code, answer.content))
            self.assertFalse(signed_in(client))
            self.assertEqual(client.get("/admin/").status_code, 302)
        self.assertEqual(set(answers), {(200, b'{"state": "refused"}')})
        self.assertEqual((User.objects.count(), AdminWallet.objects.count()), (users, wallets))
        self.assertFalse(User.objects.filter(username=pub(unknown)).exists())

    def test_refusals_count_against_the_address_limits_with_the_passwords(self):
        with steady_limits():
            for _ in range(10):
                self.assertEqual(self.sign_in(wallet(), browser()).json(), {"state": "refused"})
            answer = self.sign_in(wallet(), browser())
            self.assertEqual(answer.status_code, 429)
            self.assertEqual(answer.json(), {"state": "wait"})
            self.assertIn("Retry-After", answer)
            # The same counter as the passwords'
            self.assertEqual(self.password(browser()).status_code, 429)
            # A right wallet is held too, while it lasts
            key = wallet()
            self.linked(key)
            self.assertEqual(self.sign_in(key, browser()).status_code, 429)

    def test_wrong_passwords_count_against_the_wallets_too(self):
        key = wallet()
        self.linked(key)
        with steady_limits():
            for _ in range(11):
                browser().post(LOGIN, {"username": "volunteer", "password": "wrong", "next": "/admin/"})
            self.assertEqual(self.sign_in(key, browser()).json(), {"state": "wait"})

    def test_codes_write_nothing_and_only_live_signatures_count(self):
        """A page writes no row: a code is known by its own HMAC. An unknown k1 costs nothing and is not counted; the
        signatures of live codes are, by address and network."""
        with steady_limits(), patch.object(admin_wallet, "PAGES", (1000, 1000)):
            for _ in range(50):
                self.assertEqual(browser().get(WALLET_LOGIN).status_code, 200)
            self.assertFalse(AdminWalletChallenge.objects.exists())
            for _ in range(admin_wallet.SIGNATURES[0] + 5):
                self.assertEqual(phone().get(CALLBACK, {"k1": "ab" * 32}).status_code, 200)
            for _ in range(admin_wallet.SIGNATURES[0]):
                page = self.wallet_page(browser())
                self.assertEqual(self.signs(page.context["lnurl"], wallet())["status"], "OK")
            page = self.wallet_page(browser())
            self.assertEqual(phone().get(CALLBACK, signed(k1_of(page.context["lnurl"]), wallet())).status_code, 429)
            self.assertEqual(AdminWalletChallenge.objects.count(), admin_wallet.SIGNATURES[0])
            # Another network is not held by this one
            other = Client(HTTP_HOST=HOST, **HTTPS, REMOTE_ADDR="203.0.113.7")
            answer = other.get(CALLBACK, signed(k1_of(page.context["lnurl"]), wallet()))
            self.assertEqual(answer.json(), {"status": "OK"})

    def test_a_code_of_one_purpose_is_not_the_other(self):
        self.password()
        link_k1 = k1_of(self.client.get(SETUP).context["wallet_lnurl"])
        self.assertTrue(admin_wallet.read_k1(link_k1, admin_wallet.LINK))
        self.assertFalse(admin_wallet.read_k1(link_k1, admin_wallet.SIGN_IN))
        session = self.client.session
        session[admin_wallet.SIGN_IN_SESSION] = {"k1": link_k1}
        session.save()
        self.assertEqual(self.client.get(STATUS).json(), {"state": "expired"})

    def test_a_page_shown_again_keeps_its_code(self):
        first = self.wallet_page().context["lnurl"]
        self.assertEqual(self.wallet_page().context["lnurl"], first)
        with clock(T + admin_wallet.CHALLENGE_SECONDS / 2):
            self.assertNotEqual(self.wallet_page().context["lnurl"], first)
        self.assertFalse(AdminWalletChallenge.objects.exists())

    def test_a_set_up_not_confirmed_is_no_second_factor(self):
        key = wallet()
        self.linked(key)
        AdminSecondFactor.objects.create(user=self.staff, secret="A" * 32)
        client = browser()
        self.assert_signed_in(self.sign_in(key, client), client)

    def test_the_page_says_each_state(self):
        page = self.wallet_page()
        for state in ("refused", "expired", "wait"):
            self.assertContains(page, f'data-state="{state}" role="alert" hidden')
        self.assertNotContains(page, 'data-state="second-factor"')
        self.assertContains(page, 'href="/admin/login/">Sign in with the password')

    def test_the_password_still_works(self):
        self.linked(wallet())
        self.assertRedirects(self.password(), "/admin/", fetch_redirect_response=False)
        self.assertEqual(self.client.get("/admin/").status_code, 200)
        self.assertNotIn(WALLET_SESSION, self.client.session)

    def test_a_password_sign_in_after_a_wallet_one_drops_the_wallets_mark(self):
        key = wallet()
        self.linked(key)
        self.assert_signed_in(self.sign_in(key))
        self.password()
        self.assertNotIn(WALLET_SESSION, self.client.session)
        AdminWallet.objects.all().delete()
        self.assertEqual(self.client.get("/admin/").status_code, 200)

    def test_a_moderator_too(self):
        moderator = User.objects.create_user("moderator", password=PASSWORD)
        call_command("moderators", "add", "moderator", stdout=StringIO())
        moderator = User.objects.get(pk=moderator.pk)
        key = wallet()
        self.linked(key, moderator)
        self.assert_signed_in(self.sign_in(key, known=moderator))


@ON
class Onion(Base):
    def test_only_at_the_sites_own_address(self):
        key = wallet()
        self.linked(key)
        for client in (Client(HTTP_HOST=HOST, **HTTPS, headers={"x-entrance": "onion"}),
                       Client(HTTP_HOST="127.0.0.1", **HTTPS), Client(HTTP_HOST=HOST)):
            with self.subTest(client):
                page = client.get(WALLET_LOGIN)
                self.assertContains(page, "The wallet signs in only at https://localhost, not at this address.")
                self.assertNotIn("lnurl", page.context)
                self.assertEqual(client.get(STATUS).json(), {"state": "expired"})
                self.password(client)
                setup = client.get(SETUP)
                self.assertContains(setup, "Wallets are linked only at https://localhost, not at this address.")
                self.assertNotIn("wallet_lnurl", setup.context)
        self.assertFalse(AdminWalletChallenge.objects.exists())


# ---------------------------------------------------------------------------------------------------------------------
# The sessions
# ---------------------------------------------------------------------------------------------------------------------

@ON
class Sessions(Base):
    def test_removing_a_ends_the_sessions_opened_with_a_not_b(self):
        a, b = wallet(), wallet()
        row_a, row_b = self.linked(a, name="A"), self.linked(b, name="B")
        with_a, with_b = browser(), browser()
        self.assert_signed_in(self.sign_in(a, with_a), with_a)
        self.assert_signed_in(self.sign_in(b, with_b), with_b)
        # Removed from a session of the password: the wallets are not managed from a session of a wallet
        self.password()
        with self.captureOnCommitCallbacks(execute=True):
            answer = self.client.post(SETUP, {"action": "wallet-remove", "wallet": row_a.pk})
        self.assertRedirects(answer, SETUP, fetch_redirect_response=False)
        self.assertEqual(list(AdminWallet.objects.all()), [row_b])
        self.assertEqual(with_b.get("/admin/").status_code, 200)
        self.assertRedirects(with_a.get("/admin/"), f"{LOGIN}?next=/admin/", fetch_redirect_response=False)
        self.assertFalse(signed_in(with_a))
        self.assertIn("The staff account volunteer removed a wallet that signed in to /admin/.", self.told_texts()[0])
        self.assertEqual(self.told.call_args.args[0], "Wallet sign-in")
        self.assertTrue(LogEntry.objects.filter(change_message="Wallet removed (sign-in to /admin/)").exists())

    def test_a_session_of_a_wallet_does_not_manage_the_wallets(self):
        key = wallet()
        row = self.linked(key)
        self.assert_signed_in(self.sign_in(key))
        page = self.client.get(SETUP)
        self.assertContains(page, "This session was opened with a wallet: sign in with the password to manage the wallets.")
        self.assertNotIn("wallet_lnurl", page.context)
        self.assertNotContains(page, 'value="wallet-remove"')
        self.assertEqual(self.client.get(LINK_STATUS).json(), {"state": "expired"})
        for action in ("wallet-remove", "wallet-rename", "wallet-link", "wallet-cancel"):
            answer = self.client.post(SETUP, {"action": action, "wallet": row.pk, "name": "taken"})
            self.assertRedirects(answer, SETUP, fetch_redirect_response=False)
        self.assertEqual(AdminWallet.objects.get().name, "phone")
        self.assertContains(self.client.get(SETUP), "Sign in with the password to manage the wallets.")
        # A link signed in a session of a wallet, from its k1 put there by hand, is not confirmed either
        session = self.client.session
        k1 = admin_wallet.new_k1(admin_wallet.LINK)
        session[WALLET_LINK_SESSION] = {"k1": k1}
        session.save()
        phone().get(CALLBACK, signed(k1, wallet()))
        self.client.post(SETUP, {"action": "wallet-link", "name": "x"})
        self.assertEqual(AdminWallet.objects.count(), 1)
        self.assertFalse(self.told.called)

    def test_a_session_of_a_wallet_adds_no_second_factor(self):
        """Whoever holds the phone for a moment must not add a factor of their own: the session would become that
        factor's, manage the wallets, and leave the owner with the password alone outside."""
        from shops.models import AdminPasskey, AdminSecondFactor
        key = wallet()
        self.linked(key)
        self.assert_signed_in(self.sign_in(key))
        self.client.get(SETUP)
        marker = self.client.session[WALLET_SESSION]
        for action in ("passkey-add", "on"):
            answer = self.client.post(SETUP, {"action": action, "code": "000000", "credential": "{}", "name": "mine"})
            self.assertRedirects(answer, SETUP, fetch_redirect_response=False)
        self.assertContains(self.client.get(SETUP), "Sign in with the password to add a second factor.")
        self.assertFalse(AdminPasskey.objects.exists())
        self.assertFalse(AdminSecondFactor.objects.exclude(confirmed=None).exists())
        self.assertEqual(self.client.session[WALLET_SESSION], marker)
        self.assertIsNone(self.client.session.get(FACTOR_SESSION))
        self.assertTrue(admin_wallet.opened_with_wallet(self.client))

    def test_a_session_of_a_wallet_sets_no_password(self):
        """The admin's page that sets a password asks for no old one: from a wallet's session it would be the way to
        a password, a factor and the wallets."""
        key = wallet()
        self.linked(key)
        self.assert_signed_in(self.sign_in(key))
        user = get_user_model().objects.get(pk=self.client.session["_auth_user_id"])
        user.is_superuser = True
        user.save(update_fields=["is_superuser"])
        before = user.password
        url = reverse("admin:auth_user_password_change", args=[user.pk])
        self.assertEqual(self.client.get(url).status_code, 403)
        answer = self.client.post(url, {"password1": "a-new-long-password-1", "password2": "a-new-long-password-1"})
        self.assertEqual(answer.status_code, 403)
        user.refresh_from_db()
        self.assertEqual(user.password, before)
        self.assertEqual(self.client.get(reverse("admin:index")).status_code, 200)

    def test_a_made_up_session_cookie_is_counted_too(self):
        from django.contrib.sessions.models import Session
        with steady_limits(), patch.object(admin_wallet, "PAGES", (2, 2)):
            for n in range(2):
                client = browser()
                client.cookies[settings.SESSION_COOKIE_NAME] = f"made-up-{n}"
                self.assertIn("lnurl", client.get(WALLET_LOGIN).context)
            made = Session.objects.count()
            client = browser()
            client.cookies[settings.SESSION_COOKIE_NAME] = "made-up-late"
            self.assertNotIn("lnurl", client.get(WALLET_LOGIN).context)
            self.assertEqual(Session.objects.count(), made)

    def test_the_sign_in_page_makes_few_sessions_per_address(self):
        from django.contrib.sessions.models import Session
        with steady_limits(), patch.object(admin_wallet, "PAGES", (2, 2)):
            for _n in range(2):
                self.assertIn("lnurl", browser().get(WALLET_LOGIN).context)
            made = Session.objects.count()
            late = browser().get(WALLET_LOGIN)
            self.assertNotIn("lnurl", late.context)
            self.assertContains(late, "Too many tries. Wait a little and try again.")
            self.assertEqual(Session.objects.count(), made)

    def test_it_ends_when_the_switch_goes_off_or_the_level_rises(self):
        key = wallet()
        self.linked(key)
        for change in ({"ADMIN_WALLET_SIGN_IN": False}, {"ADMIN_SECOND_FACTOR": "required"},
                       {"ADMIN_SECOND_FACTOR": "passkey"}):
            with self.subTest(change):
                client = browser()
                self.assert_signed_in(self.sign_in(key, client), client)
                with override_settings(**change):
                    self.assertEqual(client.get("/admin/").status_code, 302)
                self.assertFalse(signed_in(client))

    def test_it_ends_when_the_account_adds_a_second_factor_elsewhere(self):
        key = wallet()
        self.linked(key)
        self.assert_signed_in(self.sign_in(key))
        turn_on(self.staff)
        self.assertEqual(self.client.get("/admin/").status_code, 302)
        self.assertFalse(signed_in(self.client))

    def test_it_ends_when_the_account_is_no_longer_staff(self):
        key = wallet()
        self.linked(key)
        self.assert_signed_in(self.sign_in(key))
        User.objects.filter(pk=self.staff.pk).update(is_staff=False)
        self.assertEqual(self.client.get("/admin/").status_code, 302)

    def test_the_day(self):
        key = wallet()
        self.linked(key)
        self.assert_signed_in(self.sign_in(key))
        with clock(T + 24 * 3600 - 1):
            self.assertEqual(self.client.get("/admin/").status_code, 200)
        with clock(T + 24 * 3600):
            self.assertEqual(self.client.get("/admin/").status_code, 302)
        self.assertFalse(signed_in(self.client))
        with override_settings(ADMIN_SESSION_HOURS=1):
            self.assert_signed_in(self.sign_in(key))
            with clock(T + 3600):
                self.assertEqual(self.client.get("/admin/").status_code, 302)

    def test_a_marker_of_another_shape_ends_the_session(self):
        key = wallet()
        row = self.linked(key)
        self.assert_signed_in(self.sign_in(key))
        for marker in ([str(row.pk), T], [row.pk], "x", [row.pk, "never"], [True, T]):
            with self.subTest(marker):
                session = self.client.session
                session[WALLET_SESSION] = marker
                session.save()
                self.assertEqual(self.client.get("/admin/").status_code, 302)
                self.assert_signed_in(self.sign_in(key))

    def test_a_wallet_of_another_account_does_not_hold_this_session(self):
        key, hers = wallet(), wallet()
        self.linked(key)
        anna = User.objects.create_user("anna", is_staff=True)
        other_row = self.linked(hers, anna)
        self.assert_signed_in(self.sign_in(key))
        session = self.client.session
        session[WALLET_SESSION] = [other_row.pk, T]
        session.save()
        self.assertEqual(self.client.get("/admin/").status_code, 302)

    def test_the_staff_side_outside_admin_ends_too(self):
        """StaffApart asks the same question: out of /admin/ a session the wallet no longer holds is signed out."""
        key = wallet()
        row = self.linked(key)
        self.assert_signed_in(self.sign_in(key))
        row.delete()
        self.client.get("/en/shops/")
        self.assertFalse(signed_in(self.client))


# ---------------------------------------------------------------------------------------------------------------------
# Linking, renaming, removing
# ---------------------------------------------------------------------------------------------------------------------

@ON
class Linking(Base):
    def test_linked_told_and_written(self):
        self.password()
        page = self.client.get(SETUP)
        self.assertContains(page, "Sign in with a wallet")
        self.assertContains(page, "Your account has no wallet linked.")
        self.assertContains(page, 'data-status="/admin/second-factor/wallet/status/"')
        answer = self.link(wallet(), name="  my <b>test</b>\nwallet  ")
        self.assertRedirects(answer, SETUP, fetch_redirect_response=False)
        page = self.client.get(SETUP)
        self.assertContains(page, "The wallet is linked to your account.")
        self.assertContains(page, "my &lt;b&gt;test&lt;/b&gt; wallet")
        self.assertEqual(AdminWallet.objects.get().name, "my <b>test</b> wallet")
        self.assertIn("The staff account volunteer linked a wallet that signs in to /admin/.", self.told_texts()[0])
        self.assertEqual(self.told.call_args.args[0], "Wallet sign-in")
        self.assertEqual(LogEntry.objects.get().change_message, "Wallet linked (sign-in to /admin/)")
        # The session of the password stays as it was
        self.assertNotIn(WALLET_SESSION, self.client.session)
        self.assertEqual(self.client.get("/admin/").status_code, 200)

    def test_only_from_a_session_of_the_admin_that_holds(self):
        key = wallet()
        anonymous = browser()
        self.assertEqual(anonymous.get(SETUP).status_code, 302)
        self.assertEqual(anonymous.get(LINK_STATUS).status_code, 302)
        anonymous.post(SETUP, {"action": "wallet-link", "name": "x"})
        # A session not born from the admin's password (the shops' sign-in, the email link)
        other = browser()
        other.force_login(self.staff)
        self.assertEqual(other.get(SETUP).status_code, 302)
        self.assertEqual(other.get(LINK_STATUS).status_code, 302)
        # A session that no longer holds: a second factor added elsewhere
        self.password()
        self.client.get(SETUP)
        AdminPasskey.objects.create(user=self.staff, credential_id="x", public_key=b"", algorithm=webauthn.ES256)
        self.assertEqual(self.client.get(SETUP).status_code, 302)
        self.client.post(SETUP, {"action": "wallet-link", "name": "x"})
        self.assertFalse(AdminWallet.objects.exists())
        del key

    def test_csrf(self):
        client = Client(enforce_csrf_checks=True, HTTP_HOST=HOST, **HTTPS)
        client.force_login(self.staff)
        session = client.session
        session[ADMIN_SESSION] = True
        session.save()
        row = self.linked(wallet())
        for action in ("wallet-remove", "wallet-rename", "wallet-link", "wallet-cancel"):
            self.assertEqual(client.post(SETUP, {"action": action, "wallet": row.pk, "name": "x"}).status_code, 403)
        self.assertEqual(AdminWallet.objects.get().name, "phone")

    def test_cancel_and_a_signature_used_once(self):
        key = wallet()
        self.password()
        page = self.client.get(SETUP)
        self.signs(page.context["wallet_lnurl"], key)
        confirm = self.client.get(SETUP)
        self.assertContains(confirm, "It is not mine: cancel")
        self.assertContains(confirm, "If your own wallet reported an error, press cancel: someone else's wallet signed "
                                     "first.")
        self.client.post(SETUP, {"action": "wallet-cancel"})
        self.client.post(SETUP, {"action": "wallet-link", "name": "x"})
        self.assertFalse(AdminWallet.objects.exists())
        self.assertContains(self.client.get(SETUP), "scan the new code")

    def test_the_cap(self):
        self.password()
        for number in range(admin_wallet.MAX_WALLETS - 1):
            self.linked(wallet(), name=f"w{number}")
        page = self.client.get(SETUP)
        self.signs(page.context["wallet_lnurl"], wallet())
        # Meanwhile, another page linked the fifth
        self.linked(wallet(), name="fifth")
        self.client.post(SETUP, {"action": "wallet-link", "name": "sixth"})
        self.assertEqual(AdminWallet.objects.count(), admin_wallet.MAX_WALLETS)
        page = self.client.get(SETUP)
        self.assertContains(page, "Your account has as many wallets as it may have: remove one first.")
        self.assertContains(page, "Your account has 5 wallets, the most it may have: remove one to link another.")
        self.assertNotIn("wallet_lnurl", page.context)

    def test_a_shop_owners_key_and_a_team_key_are_refused_alike(self):
        born, connected, team, revoked, other_staff = (wallet() for _ in range(5))
        owner = User.objects.create_user(pub(born))
        email_owner = User.objects.create_user("email:abc")
        WalletKey.objects.create(user=email_owner, key=pub(connected))
        TeamKey.objects.create(linking_key=pub(team), name="Anna")
        TeamKey.objects.create(linking_key=pub(revoked), name="Bob", revoked=timezone.now())
        anna = User.objects.create_user("anna", is_staff=True)
        self.linked(other_staff, anna)
        self.password()
        shown = []
        for key in (born, connected, team, revoked, other_staff):
            with self.subTest(pub(key)):
                self.link(key)
                page = self.client.get(SETUP)
                self.assertContains(page, REFUSED_LINK)
                shown.append(re.findall(r'<li class="error">([^<]*)</li>', page.content.decode()))
        self.assertEqual(len({tuple(found) for found in shown}), 1)
        self.assertEqual(AdminWallet.objects.filter(user=self.staff).count(), 0)
        self.assertFalse(self.told.called)
        del owner

    def test_a_key_that_gets_another_role_in_the_same_moment(self):
        key = wallet()
        self.password()
        real = admin_wallet.known_elsewhere
        answers = iter([False, True])
        with patch("config.admin_wallet.known_elsewhere", side_effect=lambda found: next(answers) or real(found)):
            self.link(key)
        self.assertFalse(AdminWallet.objects.exists())
        self.assertContains(self.client.get(SETUP), REFUSED_LINK)

    def test_already_linked_to_this_account(self):
        key = wallet()
        self.password()
        self.link(key)
        self.link(key)
        self.assertContains(self.client.get(SETUP), "This wallet is already linked to your account.")
        self.assertEqual(AdminWallet.objects.count(), 1)

    def test_rename_remove_and_bad_ids(self):
        self.password()
        row = self.linked(wallet())
        self.client.post(SETUP, {"action": "wallet-rename", "wallet": row.pk, "name": "x" * 100})
        self.assertEqual(AdminWallet.objects.get().name, "x" * 60)
        for value in ("", "x", "-1", "99999999999999999999999", "1.0", "١", str(row.pk + 1)):
            answer = self.client.post(SETUP, {"action": "wallet-remove", "wallet": value})
            self.assertRedirects(answer, SETUP, fetch_redirect_response=False)
        self.assertTrue(AdminWallet.objects.exists())
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(SETUP, {"action": "wallet-remove", "wallet": row.pk})
        self.assertFalse(AdminWallet.objects.exists())
        self.assertContains(self.client.get(SETUP), "The wallet is removed: it no longer signs in to /admin/.")

    def test_nobody_sees_or_touches_another_accounts_wallets(self):
        User.objects.create_superuser("boss", password=PASSWORD)
        theirs = self.linked(wallet(), name="secret-name-of-volunteer")
        boss = browser()
        self.password(boss, username="boss")
        for url in (SETUP, "/admin/", f"/admin/auth/user/{self.staff.pk}/change/",
                    f"/admin/auth/user/{self.staff.pk}/history/", "/admin/auth/user/"):
            page = boss.get(url)
            self.assertEqual(page.status_code, 200, url)
            self.assertNotIn("secret-name-of-volunteer", page.content.decode(), url)
            self.assertNotIn(theirs.linking_key, page.content.decode(), url)
        for action in ("wallet-remove", "wallet-rename"):
            boss.post(SETUP, {"action": action, "wallet": theirs.pk, "name": "taken"})
        self.assertEqual(AdminWallet.objects.get(pk=theirs.pk).name, "secret-name-of-volunteer")
        for lookup in ("admin_wallets__linking_key__startswith", "admin_wallets__name",
                       "admin_wallets__created__gte"):
            self.assertEqual(boss.get(f"/admin/auth/user/?{lookup}=a").status_code, 400, lookup)
        # The other account's signature waiting to be linked is not the boss's either
        self.password()
        page = self.client.get(SETUP)
        self.signs(page.context["wallet_lnurl"], wallet())
        boss.post(SETUP, {"action": "wallet-link", "name": "taken"})
        self.assertEqual(AdminWallet.objects.count(), 1)

    def test_the_models_are_not_in_the_admin(self):
        self.assertNotIn(AdminWallet, admin.site._registry)
        self.assertNotIn(AdminWalletChallenge, admin.site._registry)

    def test_with_a_second_factor_the_page_says_the_wallets_are_unused(self):
        self.password()
        turn_on(self.staff)
        factor = AdminSecondFactor.objects.get()
        client = browser()
        with clock(T):
            self.password(client)
            client.post("/admin/login/code/", {"code": code(factor)})
        page = client.get(SETUP)
        self.assertContains(page, "Your account has a second factor: the wallets linked here do not sign in while it is on.")
        # Linking stays possible
        self.link(wallet(), client)
        self.assertTrue(AdminWallet.objects.exists())


# ---------------------------------------------------------------------------------------------------------------------
# One key, one role: a staff key on the shops' side and the team's
# ---------------------------------------------------------------------------------------------------------------------

@ON
class OneKeyOneRole(Base):
    def shops_k1(self, client):
        client.get("/en/shops/")
        return LoginChallenge.objects.filter(link_user=None).latest("created").k1

    def test_a_staff_key_at_the_shops_sign_in(self):
        key = wallet()
        self.linked(key)
        shopper = Client()
        k1 = self.shops_k1(shopper)
        with self.assertLogs("shops.views", "INFO") as log:
            answer = shopper.get("/lnurl-auth/", signed(k1, key)).json()
        self.assertEqual(answer, {"status": "ERROR", "reason": "Invalid login"})
        self.assertIn("a staff account's wallet", log.output[0])
        self.assertFalse(shopper.get("/lnurl-auth/status/").json()["logged_in"])
        self.assertFalse(User.objects.filter(username=pub(key)).exists())
        # Another wallet still signs in there as before
        other = Client()
        k1 = self.shops_k1(other)
        self.assertEqual(other.get("/lnurl-auth/", signed(k1, wallet())).json()["status"], "OK")
        self.assertTrue(other.get("/lnurl-auth/status/").json()["logged_in"])

    def test_a_key_linked_after_it_signed_at_the_shops_opens_nothing(self):
        key = wallet()
        shopper = Client()
        k1 = self.shops_k1(shopper)
        self.assertEqual(shopper.get("/lnurl-auth/", signed(k1, key)).json()["status"], "OK")
        self.linked(key)
        self.assertFalse(shopper.get("/lnurl-auth/status/").json()["logged_in"])
        self.assertFalse(User.objects.filter(username=pub(key)).exists())
        self.assertIsNotNone(LoginChallenge.objects.get(k1=k1).used)

    def test_linked_at_the_moment_the_shops_account_is_made(self):
        key = wallet()
        shopper = Client()
        k1 = self.shops_k1(shopper)
        shopper.get("/lnurl-auth/", signed(k1, key))
        with patch("shops.views.staff_key", side_effect=[False, True]):
            self.assertFalse(shopper.get("/lnurl-auth/status/").json()["logged_in"])
        self.assertFalse(User.objects.filter(username=pub(key)).exists())

    def test_a_staff_key_is_not_connected_to_a_shop_owners_account(self):
        key = wallet()
        owner = User.objects.create_user("email:owner")
        shopper = Client()
        shopper.force_login(owner)
        shopper.get("/en/shops/wallet/")
        challenge = LoginChallenge.objects.get(link_user=owner)
        # The wallet signed before its key was linked to a staff account
        LoginChallenge.objects.filter(pk=challenge.pk).update(linking_key=pub(key))
        self.linked(key)
        staff_page = shopper.post("/en/shops/wallet/", {"confirm": "1"}, follow=True)
        self.assertFalse(WalletKey.objects.exists())
        # The same answer as for any key that cannot come here: one connected to another account
        elsewhere = wallet()
        WalletKey.objects.create(user=User.objects.create_user("email:other"), key=pub(elsewhere))
        shopper.get("/en/shops/wallet/")
        LoginChallenge.objects.filter(link_user=owner, used=None).update(linking_key=pub(elsewhere))
        other_page = shopper.post("/en/shops/wallet/", {"confirm": "1"}, follow=True)

        def errors(page):
            return [str(message) for message in page.context["messages"]]

        self.assertTrue(errors(staff_page))
        self.assertEqual(errors(staff_page), errors(other_page))
        self.assertContains(staff_page, "This wallet already has an account of its own with a shop or an email, or is "
                                        "connected to another account: it cannot be connected here.")

    def test_a_key_linked_to_a_staff_account_while_it_is_connected(self):
        key = wallet()
        owner = User.objects.create_user("email:owner")
        shopper = Client()
        shopper.force_login(owner)
        shopper.get("/en/shops/wallet/")
        LoginChallenge.objects.filter(link_user=owner).update(linking_key=pub(key))
        with patch("shops.views.staff_key", side_effect=[False, True]):
            shopper.post("/en/shops/wallet/", {"confirm": "1"})
        self.assertFalse(WalletKey.objects.exists())

    @override_settings(TEAM_ACCESS=True, SITE_MODE="development", DEV_ALLOWED_IPS="")
    def test_a_staff_key_is_not_a_team_key(self):
        key = wallet()
        self.linked(key)
        invite = TeamInvite(name="Anna", role="team")
        token = invite.arm()
        invite.save()
        member = Client(headers={"X-Entrance": "site"})
        page = member.get(f"/team/join/{token}/")
        self.assertEqual(page.status_code, 200)
        answer = member.get("/team/auth/", signed(k1_of(page.context["lnurl"]), key), headers={"X-Entrance": "site"})
        self.assertEqual(answer.json()["status"], "ERROR")
        self.assertIn("This wallet cannot join the team here: it already has another use on this site.",
                      answer.json()["reason"])
        self.assertFalse(TeamKey.objects.exists())
        self.assertIsNone(TeamInvite.objects.get().used)
        # The invite is still good for another wallet
        page = member.get(f"/team/join/{token}/")
        other = member.get("/team/auth/", signed(k1_of(page.context["lnurl"]), wallet()), headers={"X-Entrance": "site"})
        self.assertEqual(other.json()["status"], "OK")

    @override_settings(TEAM_ACCESS=True, SITE_MODE="development", DEV_ALLOWED_IPS="")
    def test_a_team_key_made_at_the_moment_it_is_linked(self):
        from team.models import TeamChallenge
        from team.views import join_with

        key = wallet()
        invite = TeamInvite(name="Anna", role="team")
        invite.arm()
        invite.save()
        challenge = TeamChallenge.new(invite)
        with patch("team.views.staff_key", side_effect=[False, True]):
            made, why_not = join_with(challenge, pub(key))
        self.assertIsNone(made)
        self.assertEqual(str(why_not), "This invite is not valid any more.")
        self.assertFalse(TeamKey.objects.exists())
        self.assertIsNone(TeamInvite.objects.get().used)


# ---------------------------------------------------------------------------------------------------------------------
# The commands, preflight, the setting, the translations
# ---------------------------------------------------------------------------------------------------------------------

@ON
class Commands(Base):
    def test_list(self):
        self.linked(wallet())
        self.linked(wallet())
        User.objects.create_user("anna", is_staff=True)
        out = StringIO()
        call_command("admin_wallet", "list", stdout=out)
        text = out.getvalue()
        self.assertIn("ADMIN_WALLET_SIGN_IN is on and ADMIN_SECOND_FACTOR is optional: a linked wallet signs in", text)
        self.assertIn("Staff accounts: 2, with a wallet: 1", text)
        self.assertIn("  volunteer  2", text)
        self.assertIn("  anna  0", text)
        self.assertNotIn(AdminWallet.objects.first().linking_key, text)
        for change, words in (({"ADMIN_SECOND_FACTOR": "passkey"},
                               "ADMIN_WALLET_SIGN_IN is on, but ADMIN_SECOND_FACTOR is passkey: no wallet signs in"),
                              ({"ADMIN_WALLET_SIGN_IN": False},
                               "ADMIN_WALLET_SIGN_IN is off (ADMIN_SECOND_FACTOR is optional): no wallet signs in")):
            out = StringIO()
            with override_settings(**change):
                call_command("admin_wallet", "list", stdout=out)
            self.assertIn(words, out.getvalue())
        User.objects.filter(pk=self.staff.pk).update(is_staff=False)
        out = StringIO()
        call_command("admin_wallet", "list", stdout=out)
        self.assertIn("Accounts no longer staff with a wallet (they do not sign in): 1", out.getvalue())

    def test_reset(self):
        key = wallet()
        self.linked(key)
        self.linked(wallet())
        self.assert_signed_in(self.sign_in(key))
        out = StringIO()
        with self.captureOnCommitCallbacks(execute=True):
            call_command("admin_wallet", "reset", "volunteer", stdout=out)
        self.assertIn("volunteer: 2 wallets removed", out.getvalue())
        self.assertFalse(AdminWallet.objects.exists())
        self.assertIn("The wallets of the staff account volunteer were removed with manage.py admin_wallet reset.",
                      self.told_texts()[0])
        self.assertTrue(LogEntry.objects.filter(change_message="Wallets removed with manage.py admin_wallet reset").exists())
        self.assertEqual(self.client.get("/admin/").status_code, 302)
        self.assertRedirects(self.password(browser()), "/admin/", fetch_redirect_response=False)

    def test_reset_of_an_account_without_or_of_nobody(self):
        out = StringIO()
        call_command("admin_wallet", "reset", "volunteer", stdout=out)
        self.assertIn("volunteer has no wallet. Nothing changed.", out.getvalue())
        self.assertFalse(LogEntry.objects.exists())
        with self.assertRaises(CommandError):
            call_command("admin_wallet", "reset", "nobody", stdout=StringIO())

    @override_settings(ADMIN_ALLOWED_IPS="10.0.0.0/8", SITE_HOST="shops.example.org")
    def test_preflight(self):
        from shops.management.commands.preflight import admin_notes

        self.assertTrue(any("ADMIN_WALLET_SIGN_IN is on: a linked wallet alone opens /admin/" in note
                            for note in admin_notes()))
        with override_settings(ADMIN_SECOND_FACTOR="required"):
            self.assertTrue(any("ADMIN_WALLET_SIGN_IN is on and ADMIN_SECOND_FACTOR is required: at this level no "
                                "wallet signs in" in note for note in admin_notes()))
        with override_settings(ADMIN_WALLET_SIGN_IN=False):
            self.assertFalse(any("ADMIN_WALLET_SIGN_IN" in note for note in admin_notes()))

    def test_in_italian_and_german(self):
        from django.utils import translation

        page = self.client.get(WALLET_LOGIN, headers={"accept-language": "it"})
        self.assertContains(page, "Accedi con la password")
        self.password()
        self.assertContains(self.client.get(SETUP, headers={"accept-language": "it"}), "Accedi con un wallet")
        with translation.override("de"):
            self.assertEqual(translation.gettext("Sign in with your wallet"), "Mit Ihrer Wallet anmelden")
            self.assertIn("Ihr", translation.gettext("Your account has no wallet linked."))


@ON
class ResetSaysWhoWasTold(TransactionTestCase):
    """The command tells the moderators after its commit, synchronously, and says how it went."""

    def test_told_and_not_told(self):
        user = User.objects.create_user("volunteer", password=PASSWORD, is_staff=True)
        for answer, words in ((("TELEGRAM_MODERATION_CHAT", []), "told: TELEGRAM_MODERATION_CHAT"),
                              ((None, ["Telegram refused it"]), "not told: Telegram refused it")):
            AdminWallet.objects.create(user=user, linking_key=pub(wallet()))
            out = StringIO()
            with patch("shops.notifications.deliver_to_moderators", return_value=answer) as told:
                call_command("admin_wallet", "reset", "volunteer", stdout=out)
            self.assertIn(words, out.getvalue())
            self.assertEqual(told.call_args.args[0], "Wallet sign-in")


class Setting(SimpleTestCase):
    def test_a_bad_value_stops_the_start(self):
        for value in ("yes", "ON", "1"):
            env = {**os.environ, "SECRET_KEY": "x", "DJANGO_SETTINGS_MODULE": "config.settings",
                   "ADMIN_WALLET_SIGN_IN": value}
            result = subprocess.run([sys.executable, "-c", "import django; from django.conf import settings; settings.DEBUG"],
                                    env=env, cwd=settings.BASE_DIR, capture_output=True, text=True, timeout=60,
                                    check=False)
            self.assertNotEqual(result.returncode, 0, value)
            self.assertIn(f"ADMIN_WALLET_SIGN_IN is {value!r}: it must be on or off", result.stderr)

    def test_off_by_default(self):
        env = {key: value for key, value in os.environ.items() if key != "ADMIN_WALLET_SIGN_IN"}
        script = "from django.conf import settings; print(settings.ADMIN_WALLET_SIGN_IN)"
        result = subprocess.run([sys.executable, "-c", script],
                                env={**env, "SECRET_KEY": "x", "DJANGO_SETTINGS_MODULE": "config.settings"},
                                cwd=settings.BASE_DIR, capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.stdout.strip(), "False", result.stderr)


# ---------------------------------------------------------------------------------------------------------------------
# The script
# ---------------------------------------------------------------------------------------------------------------------

SCRIPT = settings.BASE_DIR / "static" / "admin-wallet.js"
HARNESS = r"""
const answers = JSON.parse(process.argv[3]);
const notes = ["refused", "expired"].map((state) => ({ dataset: { state }, hidden: true }));
const seen = { fetched: [], assigned: null, reloaded: false, notes };
globalThis.document = { currentScript: { dataset: { status: "/admin/login/wallet/status/" } },
  querySelectorAll: () => notes };
globalThis.location = { href: "https://localhost/admin/login/wallet/", origin: "https://localhost",
  assign: (url) => { seen.assigned = url; }, reload: () => { seen.reloaded = true; } };
globalThis.fetch = async (url, options) => {
  seen.fetched.push([url, options.credentials]);
  const answer = answers.shift();
  if (answer === "broken") throw new Error("offline");
  return { json: async () => answer };
};
const rounds = [];
globalThis.setInterval = (work) => { rounds.push(work); return 1; };
globalThis.clearInterval = () => { seen.stopped = (seen.stopped || 0) + 1; };
require(process.argv[2]);
(async () => {
  for (let round = 0; round < 6; round += 1) await rounds[0]();
  process.stdout.write(JSON.stringify({ ...seen, notes: notes.map((note) => note.hidden) }));
})();
"""


class Script(SimpleTestCase):
    def run_with(self, answers):
        node = node_or_skip(self)
        with tempfile.TemporaryDirectory() as folder:
            harness = Path(folder) / "harness.js"
            harness.write_text(HARNESS)
            result = subprocess.run([node, str(harness), str(SCRIPT), json.dumps(answers)], capture_output=True,
                                    text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_it_goes_on_reloads_or_shows_the_note(self):
        out = self.run_with(["broken", {"state": "pending"}, {"state": "ok", "next": "/admin/shops/"}])
        self.assertEqual(out["assigned"], "https://localhost/admin/shops/")
        self.assertEqual(out["fetched"][0], ["/admin/login/wallet/status/", "same-origin"])
        self.assertEqual(out["stopped"], 1)
        # Never to another site
        self.assertEqual(self.run_with([{"state": "ok", "next": "https://elsewhere.example/x"}])["assigned"], "/admin/")
        self.assertEqual(self.run_with([{"state": "ok", "next": "//elsewhere.example/x"}])["assigned"], "/admin/")
        self.assertTrue(self.run_with([{"state": "signed"}])["reloaded"])
        out = self.run_with([{"state": "refused"}])
        self.assertEqual(out["notes"], [False, True])
        self.assertIsNone(out["assigned"])

    def test_the_templates_load_it_from_a_file(self):
        for name in ("wallet_login.html", "second_factor.html", "login_with_wallet.html"):
            text = (settings.BASE_DIR / "templates" / "admin" / name).read_text()
            self.assertNotIn("<script>", text)
            self.assertNotIn("style=", text)
        text = SCRIPT.read_text()
        self.assertEqual(text.count("fetch("), 1)
        self.assertIn("fetch(statusUrl", text)

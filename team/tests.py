"""Team access (team/, docs/features/team-access.md): the invite, the key, the pass, and everything that must not open."""
import logging
import re
import threading
from datetime import timedelta
from pathlib import Path
from unittest import skipUnless
from unittest.mock import patch

import bech32
import coincurve
import requests
import yaml
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core import mail, signing
from django.core.management import call_command
from django.db import connection, connections
from django.test import RequestFactory, TestCase, TransactionTestCase, override_settings
from django.urls import get_resolver, reverse
from django.utils import timezone

from config import mailbudget, quiet
from config.test_translations import python_ids, template_ids, untranslated
from shops.management.commands import preflight
from shops.test_guard import routes, sign_in_as_staff_session
from shops.tests import reset_limits
from website import visibility
from website.views import page_html

from . import access, views
from .models import (
    CHALLENGE_LIFETIME,
    INVITE_KEPT,
    INVITE_LIFETIME,
    TeamChallenge,
    TeamInvite,
    TeamKey,
    token_hash,
)

ROOT = Path(__file__).resolve().parent.parent
User = get_user_model()
# What Caddy writes on every request that comes in from the site's own address (Caddyfile)
SITE = {"X-Entrance": "site"}
TEAM_URLS = ("/team/login/", "/team/login/{k1}/", "/team/poll/", "/team/auth/", "/team/check/", "/team/join.js",
             "/team/join/{token}/")
METHODS = ("get", "post", "put", "patch", "delete", "head", "options", "trace")


def wallet():
    return coincurve.PrivateKey()


def signed(k1, key):
    """What a wallet sends to the callback: a real secp256k1 signature of the challenge, DER, and the compressed key."""
    return {"tag": "login", "k1": k1, "sig": key.sign(bytes.fromhex(k1), hasher=None).hex(), "key": key.public_key.format().hex()}


def lnurl_address(lnurl):
    """The address inside an LNURL (bech32)."""
    _prefix, data = bech32.bech32_decode(lnurl.lower())
    return bytes(bech32.convertbits(data, 5, 8, False)).decode()


def k1_of(lnurl):
    return re.search(r"[?&]k1=([0-9a-f]{64})", lnurl_address(lnurl))[1]


@override_settings(TEAM_ACCESS=True, SITE_MODE="development", DEV_ALLOWED_IPS="")
class Base(TestCase):
    def setUp(self):
        reset_limits()
        page_html.cache_clear()
        # No request leaves the machine (Telegram): without a bot the team is told by the community's email
        patcher = patch("requests.sessions.Session.request", side_effect=requests.ConnectionError)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = self.browser()

    def browser(self, **headers):
        """A browser that reaches Django through Caddy, on the site's entrance unless told otherwise."""
        return self.client_class(headers={**SITE, **headers})

    def invite(self, name="Anna", role="team"):
        """An invite as the admin makes one: (invite, token)."""
        invite = TeamInvite(name=name, role=role)
        token = invite.arm()
        invite.save()
        return invite, token

    def member(self, name="Anna", role="team"):
        """A team key and the wallet that holds it."""
        key = wallet()
        return TeamKey.objects.create(linking_key=key.public_key.format().hex(), name=name, role=role), key

    def sign_in(self, browser, key):
        """The whole sign-in of a browser with a wallet: the code, the signature, the poll. Returns the poll's answer."""
        code = browser.get("/team/login/").json()
        self.wallet_answer = browser.get("/team/auth/", signed(k1_of(code["lnurl"]), key)).json()
        return browser.get(code["poll"])

    def has_pass(self, browser):
        return browser.get("/team/check/").status_code == 204


class TheWholeFlow(Base):
    def test_from_the_invite_in_the_admin_to_the_site(self):
        admin = self.client_class()
        sign_in_as_staff_session(admin, User.objects.create_superuser("boss"))
        made = admin.post(reverse("admin:team_teaminvite_add"), {"name": "Anna", "role": "moderator"})
        self.assertEqual(made.status_code, 200)
        invite = TeamInvite.objects.get()
        link = re.search(r'value="(https://[^"]+)"', made.content.decode())[1]
        self.assertTrue(link.startswith(f"https://{settings.SITE_HOST}/team/join/"))
        token = link.rstrip("/").rsplit("/", 1)[1]
        # 256 bits from `secrets`, and only their hash in the database
        self.assertGreaterEqual(len(token), 43)
        self.assertEqual(invite.token_hash, token_hash(token))
        self.assertAlmostEqual(invite.expires, timezone.now() + timedelta(hours=24), delta=timedelta(seconds=10))
        self.assertContains(made, "data:image/svg+xml")
        self.assertIn("no-store", made["Cache-Control"])

        # The person opens the link from outside: a page that needs nothing closed to them
        page = self.client.get(f"/team/join/{token}/")
        self.assertContains(page, "This invitation is for Anna (moderator).")
        html = page.content.decode()
        self.assertNotIn("/static/", html)
        self.assertNotIn("/theme/", html)
        self.assertEqual(page["Referrer-Policy"], "no-referrer")
        self.assertEqual(page["Cache-Control"], "private, no-store")
        lnurl = re.search(r'href="lightning:(LNURL1[A-Z0-9]+)"', html)[1]
        callback = lnurl_address(lnurl)
        # The wallet's prompt names the site itself, whatever address the page was opened with
        self.assertTrue(callback.startswith(f"https://{settings.SITE_HOST}/team/auth/?tag=login&action=register&k1="))
        poll = re.search(r'data-poll="([^"]+)"', html)[1]
        # The code of this page is worth the invite until it is signed: it is in the QR code and nowhere else, and the
        # page polls an address without it
        self.assertEqual(poll, "/team/poll/")
        self.assertNotIn(k1_of(lnurl), html)
        self.assertEqual(self.client.get(f"/team/login/{k1_of(lnurl)}/").json(), {"state": "expired"})
        self.assertEqual(self.client.get(poll).json(), {"state": "pending"})
        self.assertFalse(self.has_pass(self.client))

        # The wallet signs, from its own connection: the first signature makes the key and uses the invite up
        key = wallet()
        phone = self.browser()
        self.assertEqual(phone.get("/team/auth/", signed(k1_of(lnurl), key)).json(), {"status": "OK"})
        team_key = TeamKey.objects.get()
        self.assertEqual((team_key.name, team_key.role, team_key.linking_key),
                         ("Anna", "moderator", key.public_key.format().hex()))
        invite.refresh_from_db()
        self.assertEqual(invite.key, team_key)
        self.assertIsNotNone(invite.used)
        self.assertFalse(get_user_model().objects.filter(username=team_key.linking_key).exists())
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Anna joined the team (moderator)", mail.outbox[0].body)
        self.assertNotIn(token, mail.outbox[0].body)

        # The page that showed the code gets the pass, once
        answer = self.client.get(poll)
        self.assertEqual(answer.json(), {"state": "ok"})
        cookie = answer.cookies["team_pass"]
        self.assertTrue(cookie["httponly"])
        self.assertTrue(cookie["secure"])
        self.assertEqual((cookie["samesite"], cookie["path"], cookie["domain"]), ("Lax", "/", ""))
        self.assertEqual(cookie["max-age"], access.PASS_AGE)
        self.assertEqual(access.PASS_AGE, 7 * 86400)
        self.assertEqual(self.client.get(poll).json(), {"state": "expired"})
        self.assertFalse(TeamChallenge.objects.exists())
        self.assertEqual(len(mail.outbox), 2)
        self.assertIn("Anna (moderator) opened the site under construction in a new browser", mail.outbox[1].body)

        # From now on this browser is an allowed address, for Caddy and for Django alike
        self.assertTrue(self.has_pass(self.client))
        private = self.client.get("/en/stories/").content.decode()
        self.assertIn("cake", private)
        self.assertIn("site mode: development", private)
        self.assertNotIn("cake", self.browser().get("/en/stories/").content.decode())
        # The link is spent
        self.assertContains(self.client.get(f"/team/join/{token}/"), "This invitation is not valid", status_code=404)

        # Another day, another browser: the hidden sign-in of the page under construction
        laptop = self.browser()
        code = laptop.get("/team/login/")
        self.assertEqual(sorted(code.json()), ["lnurl", "poll", "svg"])
        # One <svg> element with its namespace: good inline, and as an image (data:) too
        self.assertTrue(code.json()["svg"].startswith('<svg xmlns="http://www.w3.org/2000/svg" '))
        self.assertTrue(code.json()["svg"].endswith("</svg>"))
        binding = code.cookies["team_k1"]
        self.assertTrue(binding["httponly"])
        self.assertEqual((binding["samesite"], binding["path"]), ("Lax", "/team/"))
        self.assertIn("&action=login&", lnurl_address(code.json()["lnurl"]))
        k1 = k1_of(code.json()["lnurl"])
        self.assertEqual(code.json()["poll"], f"/team/login/{k1}/")
        self.assertEqual(laptop.get(code.json()["poll"]).json(), {"state": "pending"})
        self.assertEqual(phone.get("/team/auth/", signed(k1, key)).json(), {"status": "OK"})
        self.assertEqual(laptop.get(code.json()["poll"]).json(), {"state": "ok"})
        self.assertTrue(self.has_pass(laptop))
        team_key.refresh_from_db()
        self.assertIsNotNone(team_key.last_pass)

    def test_a_pass_gives_what_an_allowed_address_gives(self):
        _team_key, key = self.member()
        outside = RequestFactory().get("/en/", headers=SITE)
        self.assertFalse(visibility.from_allowed_address(outside))
        self.assertEqual(mailbudget.factor(outside), 1)
        self.assertEqual(self.sign_in(self.client, key).json(), {"state": "ok"})
        inside = RequestFactory().get("/en/", headers=SITE)
        inside.COOKIES["team_pass"] = self.client.cookies["team_pass"].value
        self.assertTrue(visibility.from_allowed_address(inside))
        # The larger email allowance of the allowed addresses
        self.assertEqual(mailbudget.factor(inside), 3)
        # Not an account, and nothing for /admin/, which keeps its password and its own rule by address
        page = self.client.get("/en/shops/")
        self.assertFalse(page.wsgi_request.user.is_authenticated)
        self.assertEqual(self.client.get("/admin/").status_code, 302)
        self.assertEqual(User.objects.count(), 0)

    def test_the_static_copy_stays_public_with_a_pass(self):
        _team_key, key = self.member()
        self.sign_in(self.client, key)
        self.assertNotIn("cake", self.client.get("/en/stories/", headers={"X-Static-Copy": "1"}).content.decode())


class Off(Base):
    """TEAM_ACCESS off, the default: nothing of the feature exists."""

    def setUp(self):
        super().setUp()
        _team_key, key = self.member()
        self.sign_in(self.client, key)
        self.assertTrue(self.has_pass(self.client))
        _invite, self.token = self.invite()

    def urls(self):
        return [url.format(k1="a" * 64, token=self.token) for url in TEAM_URLS]

    def test_every_address_is_a_404_and_the_pass_gives_nothing(self):
        with override_settings(TEAM_ACCESS=False):
            for url in self.urls():
                for verb in METHODS:
                    self.assertEqual(getattr(self.client, verb)(url).status_code, 404, f"{verb} {url}")
            self.assertNotIn("cake", self.client.get("/en/stories/").content.decode())
            request = RequestFactory().get("/en/", headers=SITE)
            request.COOKIES["team_pass"] = self.client.cookies["team_pass"].value
            self.assertFalse(visibility.from_allowed_address(request))
            self.assertIsNone(access.holder(request))
        self.assertFalse(TeamChallenge.objects.exists())

    def test_the_walk_of_every_url_knows_these(self):
        found = set(routes(get_resolver().url_patterns))
        self.assertEqual({route for route in found if route.startswith("team/")},
                         {"team/login/", "team/login/<str:k1>/", "team/poll/", "team/auth/", "team/check/", "team/join.js",
                          "team/join/<str:token>/"})

    def test_the_admin_shows_nothing_of_it(self):
        boss = self.client_class()
        sign_in_as_staff_session(boss, User.objects.create_superuser("boss"))
        with override_settings(TEAM_ACCESS=False):
            index = boss.get("/admin/").content.decode()
            self.assertNotIn("/admin/team/", index)
            for name in ("teamkey_changelist", "teaminvite_changelist", "teaminvite_add"):
                self.assertEqual(boss.get(reverse(f"admin:team_{name}")).status_code, 403, name)
            key = TeamKey.objects.get()
            self.assertEqual(boss.get(reverse("admin:team_teamkey_change", args=[key.pk])).status_code, 403)
            self.assertEqual(boss.post(reverse("admin:team_teaminvite_add"), {"name": "Eve", "role": "team"}).status_code, 403)
            self.assertEqual(boss.get("/admin/team/").status_code, 404)
        self.assertIn("/admin/team/", boss.get("/admin/").content.decode())
        self.assertEqual(TeamInvite.objects.count(), 1)

    def test_a_wrong_value_stops_the_site_as_it_stops_caddy(self):
        """Caddy imports a snippet by the variable's value, and does not start with one it does not know: Django agrees."""
        import importlib
        import os

        from django.core.exceptions import ImproperlyConfigured

        import config.settings as module

        for value, expected in (("on", True), ("off", False), ("", False)):
            with patch.dict("os.environ", {"TEAM_ACCESS": value}):
                self.assertIs(importlib.reload(module).TEAM_ACCESS, expected, value)
        # Off is the default: a .env that does not name it
        with patch.dict("os.environ"):
            os.environ.pop("TEAM_ACCESS", None)
            self.assertIs(importlib.reload(module).TEAM_ACCESS, False)
        with patch.dict("os.environ", {"TEAM_ACCESS": "yes"}), self.assertRaises(ImproperlyConfigured):
            importlib.reload(module)
        importlib.reload(module)


class OtherModes(Base):
    """In maintenance everything stays closed, as for the allowed addresses; in production the feature does nothing."""

    def test_maintenance_and_production(self):
        _team_key, key = self.member()
        self.sign_in(self.client, key)
        _invite, token = self.invite()
        cookie = self.client.cookies["team_pass"].value
        for mode in ("maintenance", "production"):
            with override_settings(SITE_MODE=mode):
                for url in TEAM_URLS:
                    url = url.format(k1="a" * 64, token=token)
                    for verb in METHODS:
                        self.assertEqual(getattr(self.client, verb)(url).status_code, 404, f"{mode} {verb} {url}")
                request = RequestFactory().get("/en/", headers=SITE)
                request.COOKIES["team_pass"] = cookie
                self.assertFalse(visibility.from_allowed_address(request), mode)
                self.assertNotIn("cake", self.client.get("/en/stories/").content.decode(), mode)
                self.assertEqual(mailbudget.factor(request), 1)
        self.assertTrue(self.has_pass(self.client))

    def test_an_address_in_the_list_still_counts_in_every_mode(self):
        """What was true before stays true: DEV_ALLOWED_IPS gives the private part in every mode, with the feature on or off."""
        for team in (True, False):
            for mode in settings.SITE_MODES:
                with override_settings(TEAM_ACCESS=team, SITE_MODE=mode, DEV_ALLOWED_IPS="127.0.0.1"):
                    self.assertTrue(visibility.from_allowed_address(RequestFactory().get("/en/", headers=SITE)))
                    self.assertFalse(visibility.from_allowed_address(RequestFactory().get("/en/")))

    def test_no_invite_is_made_outside_a_site_under_construction(self):
        boss = self.client_class()
        sign_in_as_staff_session(boss, User.objects.create_superuser("boss"))
        with override_settings(SITE_MODE="production"):
            self.assertEqual(boss.post(reverse("admin:team_teaminvite_add"), {"name": "Eve", "role": "team"}).status_code, 403)
            # The keys are still there to look at and to revoke
            self.assertEqual(boss.get(reverse("admin:team_teamkey_changelist")).status_code, 200)
        self.assertFalse(TeamInvite.objects.exists())


class Entrances(Base):
    """A pass counts only on the site's own entrance: Caddy writes X-Entrance itself, `onion` on the onion's."""

    def test_the_onion_and_a_request_with_no_entrance_get_nothing(self):
        _team_key, key = self.member()
        self.sign_in(self.client, key)
        _invite, token = self.invite()
        cookie = self.client.cookies["team_pass"].value
        for entrance in ({"X-Entrance": "onion"}, {}, {"X-Entrance": "SITE"}, {"X-Entrance": "site, onion"}):
            other = self.client_class(headers=entrance)
            other.cookies["team_pass"] = cookie
            for url in TEAM_URLS:
                url = url.format(k1="a" * 64, token=token)
                for verb in METHODS:
                    self.assertEqual(getattr(other, verb)(url).status_code, 404, f"{entrance} {verb} {url}")
            self.assertNotIn("cake", other.get("/en/stories/").content.decode(), entrance)
            request = RequestFactory().get("/en/", headers=entrance)
            request.COOKIES["team_pass"] = cookie
            self.assertFalse(visibility.from_allowed_address(request), entrance)

    def test_nothing_but_the_cookie_is_a_pass(self):
        """forward_auth sends Django the visitor's own headers: none of them can stand in for the cookie."""
        _team_key, key = self.member()
        self.sign_in(self.client, key)
        value = self.client.cookies["team_pass"].value
        for name in ("Team-Pass", "X-Team-Pass", "Authorization", "X-Forwarded-User", "Remote-User", "X-Team-Key"):
            stranger = self.browser(**{name: value, "X-Forwarded-Uri": "/team/check/", "X-Forwarded-Method": "GET"})
            self.assertEqual(stranger.get("/team/check/").status_code, 401, name)
        self.assertEqual(self.browser(Cookie=f"team_pass={value}").get("/team/check/").status_code, 204)


class Invites(Base):
    def test_an_invite_makes_one_key_whoever_signs_second(self):
        """Two people open the same link, each page with its own code, and both wallets sign: one key."""
        _invite, token = self.invite()
        first, second = self.browser(), self.browser()
        pages = [browser.get(f"/team/join/{token}/").content.decode() for browser in (first, second)]
        codes = [k1_of(re.search(r"lightning:(LNURL1[A-Z0-9]+)", page)[1]) for page in pages]
        self.assertNotEqual(*codes)
        one, two = wallet(), wallet()
        self.assertEqual(self.client.get("/team/auth/", signed(codes[0], one)).json(), {"status": "OK"})
        refused = self.client.get("/team/auth/", signed(codes[1], two)).json()
        self.assertEqual((refused["status"], refused["reason"]), ("ERROR", "This invite is not valid any more."))
        self.assertEqual([key.linking_key for key in TeamKey.objects.all()], [one.public_key.format().hex()])
        self.assertEqual(first.get("/team/poll/").json(), {"state": "ok"})
        # The second page hears that its wallet was not accepted, and its browser has no pass
        self.assertEqual(second.get("/team/poll/").json(), {"state": "denied"})
        self.assertFalse(self.has_pass(second))
        self.assertTrue(self.has_pass(first))

    def test_the_first_signature_of_a_code_wins(self):
        _invite, token = self.invite()
        page = self.client.get(f"/team/join/{token}/").content.decode()
        k1 = k1_of(re.search(r"lightning:(LNURL1[A-Z0-9]+)", page)[1])
        one, two = wallet(), wallet()
        self.assertEqual(self.client.get("/team/auth/", signed(k1, one)).json()["status"], "OK")
        self.assertEqual(self.client.get("/team/auth/", signed(k1, two)).json()["status"], "ERROR")
        # The wallet that did sign can ask again and hears the same
        self.assertEqual(self.client.get("/team/auth/", signed(k1, one)).json()["status"], "OK")
        self.assertEqual(TeamKey.objects.count(), 1)
        self.assertEqual(len(mail.outbox), 1)

    def test_a_phone_that_reloads_the_page_after_signing_still_gets_its_pass(self):
        """On a phone the wallet opens over the browser, and the page may load again on the way back: the invite is
        used by then, and the page must still lead this browser, and no other, to its pass."""
        _invite, token = self.invite()
        page = self.client.get(f"/team/join/{token}/").content.decode()
        k1 = k1_of(re.search(r"lightning:(LNURL1[A-Z0-9]+)", page)[1])
        self.assertEqual(self.client.get("/team/auth/", signed(k1, wallet())).json(), {"status": "OK"})
        again = self.client.get(f"/team/join/{token}/")
        self.assertEqual(again.status_code, 200)
        html = again.content.decode()
        self.assertIn('data-poll="/team/poll/"', html)
        self.assertNotIn(k1, html)
        self.assertNotIn("lightning:", html)
        self.assertNotIn("This invitation is for", html)
        self.assertEqual(TeamChallenge.objects.count(), 1)
        # Anybody else with the link finds it spent
        self.assertEqual(self.browser().get(f"/team/join/{token}/").status_code, 404)
        self.assertEqual(self.browser().get("/team/poll/").json(), {"state": "expired"})
        self.assertEqual(self.client.get(f"/team/login/{k1}/").json(), {"state": "expired"})
        self.assertEqual(self.client.get("/team/poll/").json(), {"state": "ok"})
        self.assertTrue(self.has_pass(self.client))
        self.assertEqual(self.client.get(f"/team/join/{token}/").status_code, 404)

    def test_an_invite_expires_after_a_day(self):
        invite, token = self.invite()
        page = self.client.get(f"/team/join/{token}/").content.decode()
        k1 = k1_of(re.search(r"lightning:(LNURL1[A-Z0-9]+)", page)[1])
        self.assertEqual(INVITE_LIFETIME, timedelta(hours=24))
        TeamInvite.objects.filter(pk=invite.pk).update(expires=timezone.now() - timedelta(seconds=1))
        self.assertContains(self.client.get(f"/team/join/{token}/"), "This invitation is not valid", status_code=404)
        # A code shown a minute before the end does not outlive the invite
        answer = self.client.get("/team/auth/", signed(k1, wallet())).json()
        self.assertEqual((answer["status"], answer["reason"]), ("ERROR", "This invite is not valid any more."))
        self.assertFalse(TeamKey.objects.exists())
        invite.refresh_from_db()
        self.assertIsNone(invite.used)
        # Nothing was written on the code either: it stays as it was
        self.assertEqual(TeamChallenge.objects.get(k1=k1).linking_key, "")

    def test_a_wrong_token_is_the_same_page_and_makes_nothing(self):
        self.invite()
        for token in ("x" * 43, "short", "y" * 500, "%2e%2e"):
            self.assertContains(self.client.get(f"/team/join/{token}/"), "This invitation is not valid", status_code=404)
        self.assertFalse(TeamChallenge.objects.exists())

    def test_a_deleted_invite_is_revoked(self):
        invite, token = self.invite()
        page = self.client.get(f"/team/join/{token}/").content.decode()
        k1 = k1_of(re.search(r"lightning:(LNURL1[A-Z0-9]+)", page)[1])
        invite.delete()
        self.assertEqual(self.client.get("/team/auth/", signed(k1, wallet())).json()["status"], "ERROR")
        self.assertEqual(self.client.get(f"/team/join/{token}/").status_code, 404)
        self.assertFalse(TeamKey.objects.exists())

    def test_a_wallet_that_has_a_key_cannot_take_an_invite(self):
        """An active key, or a revoked one coming back with a new invite: nothing is made and nothing is used up."""
        for revoked in (False, True):
            team_key, key = self.member(name=f"Old {revoked}")
            if revoked:
                TeamKey.revoke(TeamKey.objects.filter(pk=team_key.pk))
            invite, token = self.invite(name="New")
            browser = self.browser()
            page = browser.get(f"/team/join/{token}/").content.decode()
            k1 = k1_of(re.search(r"lightning:(LNURL1[A-Z0-9]+)", page)[1])
            answer = browser.get("/team/auth/", signed(k1, key)).json()
            self.assertEqual(answer["status"], "ERROR")
            self.assertIn("already has a team key", answer["reason"])
            invite.refresh_from_db()
            self.assertIsNone(invite.used)
            self.assertEqual(browser.get("/team/poll/").json(), {"state": "pending"})
            # The wallet the invite was meant for can still sign the same code
            self.assertEqual(browser.get("/team/auth/", signed(k1, wallet())).json(), {"status": "OK"})
        self.assertEqual(TeamKey.objects.filter(name="New").count(), 2)

    def test_old_invites_go_when_a_new_one_is_made(self):
        old, _token = self.invite(name="Old")
        recent, _token = self.invite(name="Recent")
        TeamInvite.objects.filter(pk=old.pk).update(expires=timezone.now() - INVITE_KEPT - timedelta(hours=1))
        TeamInvite.objects.filter(pk=recent.pk).update(expires=timezone.now() - INVITE_KEPT + timedelta(hours=1))
        self.invite(name="New")
        self.assertEqual(sorted(TeamInvite.objects.values_list("name", flat=True)), ["New", "Recent"])

    def test_the_token_is_never_in_the_database(self):
        _invite, token = self.invite()
        self.client.get(f"/team/join/{token}/")
        with connection.cursor() as cursor:
            for table in connection.introspection.table_names(cursor):
                cursor.execute(f"SELECT * FROM {connection.ops.quote_name(table)}")
                for row in cursor.fetchall():
                    self.assertNotIn(token, " ".join(str(value) for value in row), table)


class SignIn(Base):
    def test_a_key_that_is_not_of_the_team_gets_nothing(self):
        self.member()
        answer = self.sign_in(self.client, wallet())
        self.assertEqual(answer.json(), {"state": "denied"})
        self.assertNotIn("team_pass", answer.cookies)
        self.assertEqual((self.wallet_answer["status"], self.wallet_answer["reason"]),
                         ("ERROR", "This wallet is not in the team of this site."))
        self.assertFalse(self.has_pass(self.client))
        self.assertNotIn("cake", self.client.get("/en/stories/").content.decode())
        self.assertEqual(mail.outbox, [])
        self.assertEqual(TeamKey.objects.count(), 1)
        self.assertEqual(User.objects.count(), 0)

    def test_the_poll_answers_only_the_browser_the_code_was_made_for(self):
        """Anybody in the room can see the QR code and work out the address of its poll: only the browser with the
        signed cookie of that code hears what became of it, and gets the pass."""
        _team_key, key = self.member()
        code = self.client.get("/team/login/").json()
        k1 = k1_of(code["lnurl"])
        onlooker, other = self.browser(), self.browser()
        other.get("/team/login/")  # a browser with a code of its own
        forger = self.browser()
        forger.cookies["team_k1"] = signing.get_cookie_signer(salt="team_k1" + "wrong").sign(k1)
        plain = self.browser()
        plain.cookies["team_k1"] = k1
        for stage in ("before the signature", "after"):
            for browser in (onlooker, other, forger, plain):
                answer = browser.get(code["poll"])
                self.assertEqual(answer.json(), {"state": "expired"}, stage)
                self.assertNotIn("team_pass", answer.cookies)
            self.client.get("/team/auth/", signed(k1, key))
        answer = self.client.get(code["poll"])
        self.assertEqual(answer.json(), {"state": "ok"})
        self.assertIn("team_pass", answer.cookies)

    def test_a_challenge_lasts_five_minutes(self):
        _team_key, key = self.member()
        self.assertEqual(CHALLENGE_LIFETIME, timedelta(minutes=5))
        code = self.client.get("/team/login/").json()
        k1 = k1_of(code["lnurl"])
        TeamChallenge.objects.filter(k1=k1).update(created=timezone.now() - timedelta(minutes=5, seconds=1))
        self.assertEqual(self.client.get("/team/auth/", signed(k1, key)).json()["status"], "ERROR")
        self.assertEqual(self.client.get(code["poll"]).json(), {"state": "expired"})
        # The next code asked sweeps the old ones away
        self.browser().get("/team/login/")
        self.assertFalse(TeamChallenge.objects.filter(k1=k1).exists())

    def test_a_signed_code_is_used_once(self):
        _team_key, key = self.member()
        code = self.client.get("/team/login/").json()
        self.client.get("/team/auth/", signed(k1_of(code["lnurl"]), key))
        self.assertEqual(self.client.get(code["poll"]).json(), {"state": "ok"})
        self.assertEqual(self.client.get(code["poll"]).json(), {"state": "expired"})
        self.assertEqual(len(mail.outbox), 1)

    def test_the_page_keeps_its_code_while_it_is_young(self):
        first = self.client.get("/team/login/").json()
        self.assertEqual(self.client.get("/team/login/").json()["poll"], first["poll"])
        TeamChallenge.objects.update(created=timezone.now() - timedelta(minutes=3))
        self.assertNotEqual(self.client.get("/team/login/").json()["poll"], first["poll"])
        self.assertEqual(TeamChallenge.objects.count(), 2)

    def test_only_the_signature_of_the_challenge_with_a_compressed_key(self):
        """The same check as the merchants' sign-in (shops/lnurl_auth.py)."""
        team_key, key = self.member()
        k1 = k1_of(self.client.get("/team/login/").json()["lnurl"])
        good = signed(k1, key)
        long_form = key.public_key.format(compressed=False).hex()
        other_k1 = k1_of(self.browser().get("/team/login/").json()["lnurl"])
        for wrong in ({**good, "key": long_form}, {**good, "sig": signed(other_k1, key)["sig"]}, {**good, "sig": "zz"},
                      {**good, "sig": ""}, {**good, "k1": "0" * 64}, {**good, "k1": k1.upper()},
                      {**good, "key": team_key.linking_key[:2] + " " + team_key.linking_key[2:]},
                      {**good, "sig": signed(k1, wallet())["sig"]}, {"tag": "login"}):
            self.assertEqual(self.client.get("/team/auth/", wrong).json(), {"status": "ERROR", "reason": "Invalid sign-in"})
        self.assertEqual(self.client.get(f"/team/login/{k1}/").json(), {"state": "pending"})
        # Capitals are the same key
        self.assertEqual(self.client.get("/team/auth/", {**good, "key": good["key"].upper()}).json(), {"status": "OK"})
        self.assertEqual(self.client.get(f"/team/login/{k1}/").json(), {"state": "ok"})

    def test_only_get(self):
        for url in TEAM_URLS:
            url = url.format(k1="a" * 64, token="x" * 43)
            for verb in METHODS[1:]:
                self.assertEqual(getattr(self.client, verb)(url).status_code, 405, f"{verb} {url}")
        self.assertFalse(TeamChallenge.objects.exists())

    def test_the_answers_are_kept_by_no_cache(self):
        for url in ("/team/login/", f"/team/login/{'a' * 64}/", "/team/poll/", "/team/auth/", "/team/check/"):
            response = self.client.get(url)
            self.assertEqual(response["Cache-Control"], "private, no-store", url)
            self.assertEqual(response["X-Robots-Tag"], "noindex, nofollow", url)

    def test_the_pages_of_team_access_link_the_source_too(self):
        """AGPLv3, section 13 (website/tests.py, SourceLink): the invite's page is not built on base.html, in any of its
        states, and the admin's pages of the feature have the admin's footer."""
        link = '<a href="https://git.example.org/our/bitmentor" rel="noopener">Source code (AGPLv3)</a>'
        _invite, token = self.invite()
        boss = self.client_class()
        sign_in_as_staff_session(boss, User.objects.create_superuser("boss"))
        with override_settings(SOURCE_URL="https://git.example.org/our/bitmentor"):
            self.assertContains(self.client.get(f"/team/join/{token}/"), link, count=1)
            self.assertContains(self.client.get(f"/team/join/{'x' * 43}/"), link, count=1, status_code=404)
            with patch.object(views, "INVITES", (0, 0)):
                self.assertContains(self.client.get(f"/team/join/{token}/"), link, count=1, status_code=429)
            for name in ("teamkey_changelist", "teaminvite_changelist", "teaminvite_add"):
                self.assertContains(boss.get(reverse(f"admin:team_{name}")), link, count=1)
            made = boss.post(reverse("admin:team_teaminvite_add"), {"name": "Anna", "role": "team"})
            self.assertContains(made, link, count=1)

    def test_the_script_of_the_invite_page_comes_from_under_team(self):
        response = self.client.get("/team/join.js")
        self.assertEqual(response["Content-Type"], "text/javascript; charset=utf-8")
        self.assertIn(b"dataset.poll", response.content)
        _invite, token = self.invite()
        self.assertContains(self.client.get(f"/team/join/{token}/"), '<script src="/team/join.js"')


class Passes(Base):
    def setUp(self):
        super().setUp()
        self.team_key, self.key = self.member()
        self.assertEqual(self.sign_in(self.client, self.key).json(), {"state": "ok"})
        self.assertTrue(self.has_pass(self.client))

    def carrying(self, value):
        browser = self.browser()
        browser.cookies["team_pass"] = value
        return browser

    def test_a_revoked_key_stops_at_the_next_request(self):
        boss = self.client_class()
        sign_in_as_staff_session(boss, User.objects.create_superuser("boss"))
        self.assertIn("cake", self.client.get("/en/stories/").content.decode())
        done = boss.post(reverse("admin:team_teamkey_changelist"),
                         {"action": "revoke", "_selected_action": [self.team_key.pk]}, follow=True)
        self.assertContains(done, "1 key revoked.")
        self.assertFalse(self.has_pass(self.client))
        self.assertNotIn("cake", self.client.get("/en/stories/").content.decode())
        # The wallet signs in no more
        self.assertEqual(self.sign_in(self.browser(), self.key).json(), {"state": "denied"})
        self.assertEqual(self.wallet_answer["status"], "ERROR")
        # Nor does the old pass come back if the row is put back by hand
        TeamKey.objects.update(revoked=None)
        self.assertFalse(self.has_pass(self.client))

    def test_revoking_and_ending_passes_are_written_in_the_history_of_each_key(self):
        from django.contrib.admin.models import CHANGE, LogEntry

        boss_user = User.objects.create_superuser("boss")
        boss = self.client_class()
        sign_in_as_staff_session(boss, boss_user)
        other_key, _wallet = self.member(name="Bruno")
        gone, _wallet = self.member(name="Carla")
        TeamKey.revoke(TeamKey.objects.filter(pk=gone.pk))
        url = reverse("admin:team_teamkey_changelist")
        boss.post(url, {"action": "end_passes", "_selected_action": [self.team_key.pk, other_key.pk]})
        done = boss.post(url, {"action": "revoke", "_selected_action": [self.team_key.pk, gone.pk]}, follow=True)
        # Carla was revoked already: nothing was done to her key, and nothing is written
        self.assertContains(done, "1 key revoked.")
        entries = [(entry.object_id, entry.action_flag, entry.user_id, entry.get_change_message())
                   for entry in LogEntry.objects.order_by("pk")]
        self.assertEqual(entries, [
            (str(self.team_key.pk), CHANGE, boss_user.pk, "Passes ended: every browser signs in again."),
            (str(other_key.pk), CHANGE, boss_user.pk, "Passes ended: every browser signs in again."),
            (str(self.team_key.pk), CHANGE, boss_user.pk, "Revoked: the wallet signs in no more, its passes stopped.")])
        history = boss.get(reverse("admin:team_teamkey_history", args=[self.team_key.pk]))
        self.assertContains(history, "Revoked: the wallet signs in no more")
        self.assertContains(history, "boss")

    def test_ending_the_passes_keeps_the_key(self):
        boss = self.client_class()
        sign_in_as_staff_session(boss, User.objects.create_superuser("boss"))
        other_key, _wallet = self.member(name="Bruno")
        other = self.carrying(access.pass_for(other_key))
        done = boss.post(reverse("admin:team_teamkey_changelist"),
                         {"action": "end_passes", "_selected_action": [self.team_key.pk]}, follow=True)
        self.assertContains(done, "The passes of 1 key ended.")
        self.assertFalse(self.has_pass(self.client))
        self.assertTrue(self.has_pass(other))
        self.assertEqual(self.sign_in(self.client, self.key).json(), {"state": "ok"})
        self.assertTrue(self.has_pass(self.client))

    def test_a_deleted_key_stops_too(self):
        self.team_key.delete()
        self.assertFalse(self.has_pass(self.client))

    def test_a_pass_lasts_a_week(self):
        value = self.client.cookies["team_pass"].value
        with patch("django.core.signing.time.time", return_value=__import__("time").time() + access.PASS_AGE - 60):
            self.assertTrue(self.has_pass(self.carrying(value)))
        with patch("django.core.signing.time.time", return_value=__import__("time").time() + access.PASS_AGE + 60):
            self.assertFalse(self.has_pass(self.carrying(value)))
            self.assertNotIn("cake", self.carrying(value).get("/en/stories/").content.decode())

    def test_forged_passes(self):
        good = self.client.cookies["team_pass"].value
        payload, stamp, signature = good.split(":")
        other_key, _wallet = self.member(name="Bruno")
        forged = {
            "empty": "", "junk": "x" * 120, "a changed signature": f"{payload}:{stamp}:{signature[:-2]}AA",
            "a changed payload": f"{payload[:-2]}AA:{stamp}:{signature}",
            "signed for something else": signing.dumps({"key": self.team_key.pk, "stamp": self.team_key.stamp}, salt="login"),
            "signed with another secret": signing.dumps({"key": self.team_key.pk, "stamp": self.team_key.stamp},
                                                         key="another", salt=access.PASS_SALT),
            "the cookie of the code": signing.get_cookie_signer(salt="team_k1" + views.K1_SALT).sign("a" * 64),
            "another key's stamp": signing.dumps({"key": other_key.pk, "stamp": self.team_key.stamp}, salt=access.PASS_SALT),
            "no stamp": signing.dumps({"key": self.team_key.pk}, salt=access.PASS_SALT),
            "a key that is not a number": signing.dumps({"key": True, "stamp": self.team_key.stamp}, salt=access.PASS_SALT),
            "a key that does not exist": signing.dumps({"key": 10**9, "stamp": self.team_key.stamp}, salt=access.PASS_SALT),
            "not a dictionary": signing.dumps([self.team_key.pk, self.team_key.stamp], salt=access.PASS_SALT),
        }
        for what, value in forged.items():
            browser = self.carrying(value)
            self.assertEqual(browser.get("/team/check/").status_code, 401, what)
            self.assertNotIn("cake", browser.get("/en/stories/").content.decode(), what)
        self.assertTrue(self.has_pass(self.carrying(good)))

    def test_a_forged_pass_does_not_reach_the_database(self):
        browser = self.carrying("x" * 120)
        with self.assertNumQueries(0):
            self.assertEqual(browser.get("/team/check/").status_code, 401)
        with self.assertNumQueries(0):
            self.assertEqual(self.browser().get("/team/check/").status_code, 401)
        with self.assertNumQueries(1):
            self.assertEqual(self.client.get("/team/check/").status_code, 204)

    def test_the_check_and_django_are_the_same_judgement(self):
        """/team/check/ is what Caddy asks, from_allowed_address what the pages ask: one function behind both."""
        with patch("team.access.read", return_value=None) as read:
            self.assertEqual(self.client.get("/team/check/").status_code, 401)
            self.assertFalse(visibility.from_allowed_address(RequestFactory().get("/en/", headers=SITE)))
        self.assertEqual(read.call_count, 2)
        with patch("team.access.read", return_value=self.team_key):
            self.assertEqual(self.browser().get("/team/check/").status_code, 204)
            self.assertTrue(visibility.from_allowed_address(RequestFactory().get("/en/", headers=SITE)))

    def test_a_page_reads_the_pass_once(self):
        with patch("team.access.read", wraps=access.read) as read:
            self.client.get("/en/stories/")
        self.assertEqual(read.call_count, 1)


class Limits(Base):
    def test_codes_by_address(self):
        for _ in range(views.CODES[0]):
            self.assertEqual(self.client.get("/team/login/").status_code, 200)
        refused = self.client.get("/team/login/")
        self.assertEqual(refused.status_code, 429)
        self.assertEqual(list(refused.json()), ["error"])
        self.assertIn("Retry-After", refused)
        # Another address is not touched
        self.assertEqual(self.browser(**{"X-Forwarded-For": "203.0.113.9"}).get("/team/login/").status_code, 200)

    def test_signatures_by_address(self):
        _team_key, key = self.member()
        for _ in range(views.SIGNATURES[0]):
            self.assertEqual(self.client.get("/team/auth/", {"k1": "0" * 64, "sig": "00", "key": "02" + "0" * 64}).status_code, 200)
        k1 = k1_of(self.browser(**{"X-Forwarded-For": "203.0.113.9"}).get("/team/login/").json()["lnurl"])
        refused = self.client.get("/team/auth/", signed(k1, key))
        self.assertEqual(refused.status_code, 429)
        self.assertEqual(sorted(refused.json()), ["error", "reason", "status"])
        self.assertEqual(refused.json()["status"], "ERROR")
        # A refused signature wrote nothing
        self.assertEqual(TeamChallenge.objects.get(k1=k1).linking_key, "")

    def test_invite_pages_by_address(self):
        _invite, token = self.invite()
        for _ in range(views.INVITES[0]):
            self.assertEqual(self.client.get("/team/join/wrong-token-wrong-token/").status_code, 404)
        refused = self.client.get(f"/team/join/{token}/")
        self.assertContains(refused, "Too many requests from your connection", status_code=429)
        self.assertIn("Retry-After", refused)
        self.assertFalse(TeamChallenge.objects.exists())

    def test_polls_by_address(self):
        with patch.object(views, "POLLS", (3, 9)):
            for _ in range(3):
                self.assertEqual(self.client.get(f"/team/login/{'a' * 64}/").json(), {"state": "expired"})
            refused = self.client.get(f"/team/login/{'a' * 64}/")
        self.assertEqual(refused.status_code, 429)
        self.assertEqual(list(refused.json()), ["error"])

    def test_an_ipv6_network_shares_a_limit(self):
        with patch.object(views, "CODES", (2, 4)):
            for number in range(4):
                browser = self.browser(**{"X-Forwarded-For": f"2001:db8:1:{number}::1"})
                self.assertEqual(browser.get("/team/login/").status_code, 200)
            self.assertEqual(self.browser(**{"X-Forwarded-For": "2001:db8:1:9::1"}).get("/team/login/").status_code, 429)

    def test_the_table_of_challenges_cannot_grow_without_end(self):
        with patch("team.models.CHALLENGES_ALIVE", 3), self.assertLogs("team.models", "WARNING") as logs:
            for number in range(3):
                self.assertEqual(self.browser(**{"X-Forwarded-For": f"203.0.113.{number}"}).get("/team/login/").status_code, 200)
            full = self.browser(**{"X-Forwarded-For": "203.0.113.50"})
            refused = full.get("/team/login/")
            self.assertEqual(refused.status_code, 429)
            self.assertEqual(list(refused.json()), ["error"])
            # The operator finds it in the log of web
            self.assertIn("3 sign-in codes alive", logs.output[0])
            # When the old ones expire there is room again
            TeamChallenge.objects.update(created=timezone.now() - timedelta(minutes=6))
            self.assertEqual(full.get("/team/login/").status_code, 200)
            self.assertEqual(TeamChallenge.objects.count(), 1)

    def test_strangers_asking_for_sign_in_codes_cannot_keep_an_invited_person_out(self):
        """The codes of the invites' pages, which only whoever holds an open invite can make, are counted apart."""
        _invite, token = self.invite()
        with patch("team.models.CHALLENGES_ALIVE", 2), patch("team.models.INVITE_CHALLENGES_ALIVE", 2), \
                self.assertLogs("team.models", "WARNING") as logs:
            for number in range(2):
                self.assertEqual(self.browser(**{"X-Forwarded-For": f"203.0.113.{number}"}).get("/team/login/").status_code, 200)
            self.assertEqual(self.browser(**{"X-Forwarded-For": "203.0.113.9"}).get("/team/login/").status_code, 429)
            invited = self.browser(**{"X-Forwarded-For": "198.51.100.1"})
            page = invited.get(f"/team/join/{token}/")
            self.assertEqual(page.status_code, 200)
            k1 = k1_of(re.search(r"lightning:(LNURL1[A-Z0-9]+)", page.content.decode())[1])
            self.assertEqual(invited.get("/team/auth/", signed(k1, wallet())).json(), {"status": "OK"})
            self.assertEqual(invited.get("/team/poll/").json(), {"state": "ok"})
            # And the other way round: the pages of the invites have a most of their own
            _invite, other = self.invite(name="Bruno")
            for number in range(2):
                self.assertEqual(self.browser(**{"X-Forwarded-For": f"198.51.100.{number + 2}"}).get(f"/team/join/{other}/").status_code, 200)
            self.assertEqual(self.browser(**{"X-Forwarded-For": "198.51.100.9"}).get(f"/team/join/{other}/").status_code, 429)
        self.assertIn("codes of invite pages", logs.output[-1])


class Admin(Base):
    def setUp(self):
        super().setUp()
        self.boss = self.client_class()
        sign_in_as_staff_session(self.boss, User.objects.create_superuser("boss"))

    def test_the_staff_sees_the_invites_and_the_keys(self):
        team_key, _key = self.member(name="Anna")
        TeamKey.revoke(TeamKey.objects.filter(pk=self.member(name="Bruno")[0].pk))
        self.invite(name="Carla")
        used, _token = self.invite(name="Dario")
        TeamInvite.objects.filter(pk=used.pk).update(used=timezone.now(), key=team_key)
        late, _token = self.invite(name="Elena")
        TeamInvite.objects.filter(pk=late.pk).update(expires=timezone.now() - timedelta(hours=1))
        self.assertIn("/admin/team/", self.boss.get("/admin/").content.decode())
        keys = self.boss.get(reverse("admin:team_teamkey_changelist")).content.decode()
        for word in ("Anna", "Bruno", "active", "revoked", f"{team_key.linking_key[:8]}…"):
            self.assertIn(word, keys)
        self.assertNotIn(team_key.stamp, keys)
        invites = self.boss.get(reverse("admin:team_teaminvite_changelist")).content.decode()
        for word in ("Carla", "Dario", "Elena", "open", "used", "expired"):
            self.assertIn(word, invites)

    def test_a_key_is_made_only_by_an_invite(self):
        self.assertEqual(self.boss.get(reverse("admin:team_teamkey_add")).status_code, 403)
        team_key, _key = self.member()
        page = self.boss.get(reverse("admin:team_teamkey_change", args=[team_key.pk]))
        self.assertContains(page, team_key.linking_key)
        self.assertNotContains(page, team_key.stamp)
        # The name and the role can be put right; the key, its stamp and its revocation are not a form's to write
        other = "02" + "ab" * 32
        self.boss.post(reverse("admin:team_teamkey_change", args=[team_key.pk]),
                       {"name": "Anna B.", "role": "staff", "linking_key": other, "stamp": "x", "revoked_0": "2026-01-01",
                        "revoked_1": "10:00"})
        fresh = TeamKey.objects.get(pk=team_key.pk)
        self.assertEqual((fresh.name, fresh.role, fresh.linking_key, fresh.stamp, fresh.revoked),
                         ("Anna B.", "staff", team_key.linking_key, team_key.stamp, None))

    def test_an_invite_is_shown_once_and_never_changed(self):
        made = self.boss.post(reverse("admin:team_teaminvite_add"), {"name": "Anna", "role": "team"})
        token = re.search(r"/team/join/([^/\"]+)/", made.content.decode())[1]
        invite = TeamInvite.objects.get()
        for url in (reverse("admin:team_teaminvite_changelist"), reverse("admin:team_teaminvite_change", args=[invite.pk])):
            page = self.boss.get(url)
            self.assertEqual(page.status_code, 200)
            self.assertNotContains(page, token)
            self.assertNotContains(page, invite.token_hash)
        self.boss.post(reverse("admin:team_teaminvite_change", args=[invite.pk]), {"name": "Eve", "role": "staff"})
        self.assertEqual(TeamInvite.objects.get().name, "Anna")
        # Deleting an open invite revokes it
        self.boss.post(reverse("admin:team_teaminvite_delete", args=[invite.pk]), {"post": "yes"})
        self.assertFalse(TeamInvite.objects.exists())
        self.assertEqual(self.client.get(f"/team/join/{token}/").status_code, 404)

    def test_the_admins_log_does_not_keep_the_token(self):
        from django.contrib.admin.models import LogEntry

        made = self.boss.post(reverse("admin:team_teaminvite_add"), {"name": "Anna", "role": "team"})
        token = re.search(r"/team/join/([^/\"]+)/", made.content.decode())[1]
        entry = LogEntry.objects.get()
        self.assertNotIn(token, f"{entry.object_repr} {entry.change_message}")

    def test_staff_without_the_rights_see_nothing(self):
        plain = User.objects.create_user("plain", is_staff=True)
        viewer = User.objects.create_user("viewer", is_staff=True)
        viewer.user_permissions.add(*Permission.objects.filter(content_type__app_label="team", codename__startswith="view_"))
        team_key, _key = self.member()
        browser = self.client_class()
        sign_in_as_staff_session(browser, plain)
        self.assertEqual(browser.get(reverse("admin:team_teamkey_changelist")).status_code, 403)
        self.assertEqual(browser.post(reverse("admin:team_teaminvite_add"), {"name": "Eve", "role": "team"}).status_code, 403)
        sign_in_as_staff_session(browser, viewer)
        self.assertEqual(browser.get(reverse("admin:team_teamkey_changelist")).status_code, 200)
        # Whoever can only look cannot revoke
        browser.post(reverse("admin:team_teamkey_changelist"), {"action": "revoke", "_selected_action": [team_key.pk]})
        browser.post(reverse("admin:team_teamkey_changelist"), {"action": "end_passes", "_selected_action": [team_key.pk]})
        fresh = TeamKey.objects.get(pk=team_key.pk)
        self.assertEqual((fresh.revoked, fresh.stamp), (None, team_key.stamp))
        self.assertFalse(TeamInvite.objects.exists())

    def test_the_moderators_group_gets_none_of_it(self):
        from shops import moderators

        self.assertFalse([entry for entry in moderators.PERMISSIONS if entry[0] == "team"])


class Telling(Base):
    def test_telegram_first_when_the_site_has_it(self):
        _team_key, key = self.member(name="Anna @everyone http://evil.example")
        with override_settings(TELEGRAM_BOT_TOKEN="t", TELEGRAM_MODERATION_CHAT="-100"), \
                patch("shops.notifications.telegram", return_value=True) as telegram:
            self.sign_in(self.client, key)
        self.assertEqual(mail.outbox, [])
        chat, text = telegram.call_args.args
        self.assertEqual(chat, "-100")
        self.assertIn("opened the site under construction in a new browser", text)
        self.assertIn(f"https://{settings.SITE_HOST}/admin/team/teamkey/", text)
        # A name somebody else typed cannot mention or link anything in the team's chat
        self.assertNotIn("@everyone", text)
        self.assertNotIn("evil.example", text)

    def test_a_message_that_cannot_be_sent_does_not_stop_the_sign_in(self):
        _team_key, key = self.member()
        with patch("shops.notifications.send_mail", side_effect=OSError), self.assertLogs("team.views", "WARNING"):
            self.assertEqual(self.sign_in(self.client, key).json(), {"state": "ok"})
        self.assertTrue(self.has_pass(self.client))

    def test_whatever_happens_while_telling_the_sign_in_is_done(self):
        """Not only the errors a delivery is known to raise: anything."""
        _team_key, key = self.member()
        for error in (RuntimeError("boom"), KeyError("x"), ValueError(), requests.Timeout()):
            browser = self.browser()
            with patch("team.views.deliver_to_moderators", side_effect=error), self.assertLogs("team.views", "ERROR") as logs:
                answer = self.sign_in(browser, key)
            self.assertEqual((answer.status_code, answer.json()), (200, {"state": "ok"}), error)
            self.assertIn("team_pass", answer.cookies)
            self.assertTrue(self.has_pass(browser))
            self.assertIn("after the answer failed", logs.output[0])
        # The same for the wallet that takes an invite: the key is made and the wallet hears OK
        _invite, token = self.invite(name="Carla")
        page = self.client.get(f"/team/join/{token}/").content.decode()
        k1 = k1_of(re.search(r"lightning:(LNURL1[A-Z0-9]+)", page)[1])
        with patch("team.views.deliver_to_moderators", side_effect=RuntimeError("boom")), self.assertLogs("team.views", "ERROR"):
            self.assertEqual(self.client.get("/team/auth/", signed(k1, wallet())).json(), {"status": "OK"})
        self.assertTrue(TeamKey.objects.filter(name="Carla").exists())

    def test_the_team_is_told_after_the_answer_has_been_sent(self):
        """The view hands back the whole answer, the pass in it, before anything is sent to Telegram or to the mail
        server: the server sends it, and only then, when it closes the answer, the message leaves. A chat or a mail
        server that takes its ten seconds delays nobody's sign-in."""
        _team_key, key = self.member()
        code = self.client.get("/team/login/").json()
        self.client.get("/team/auth/", signed(k1_of(code["lnurl"]), key))
        request = RequestFactory().get(code["poll"], headers=SITE)
        request.COOKIES["team_k1"] = self.client.cookies["team_k1"].value
        with patch("team.views.deliver_to_moderators", return_value=("x", [])) as deliver:
            response = views.poll(request, k1_of(code["lnurl"]))
            self.assertEqual((response.status_code, response.content), (200, b'{"state": "ok"}'))
            self.assertIn("team_pass", response.cookies)
            deliver.assert_not_called()
            # The server closes the answer once it has sent it. As the test client does, without the end of the
            # request closing the test's own connection to the database
            from django.core.signals import request_finished
            from django.db import close_old_connections

            request_finished.disconnect(close_old_connections)
            try:
                response.close()
                deliver.assert_called_once()
                response.close()
                deliver.assert_called_once()
            finally:
                request_finished.connect(close_old_connections)

    def test_the_message_is_in_the_communitys_language(self):
        _team_key, key = self.member()
        self.client = self.browser(**{"Accept-Language": "de"})
        self.sign_in(self.client, key)
        self.assertIn("opened the site under construction", mail.outbox[0].body)


class Preflight(Base):
    def test_preflight_says_when_it_is_on(self):
        self.member()
        TeamKey.revoke(TeamKey.objects.filter(pk=self.member(name="Bruno")[0].pk))
        self.invite()
        with override_settings(ADMIN_ALLOWED_IPS="203.0.113.7", EMAIL_HOST="mail.example.org"):
            self.assertEqual(preflight.team_notes(),
                             ["team access is on (TEAM_ACCESS=on in the .env): 1 team keys, 1 open invites (/admin/team/)"])
            with override_settings(SITE_MODE="production"):
                self.assertIn("with SITE_MODE production it does nothing", preflight.team_notes()[0])
            self.assertIn(preflight.team_notes()[0], preflight.feature_notes())
        with override_settings(TEAM_ACCESS=False, ADMIN_ALLOWED_IPS=""):
            self.assertEqual(preflight.team_notes(), [])

    def test_preflight_says_that_a_pass_reaches_the_admins_sign_in_page(self):
        """With ADMIN_ALLOWED_IPS empty /admin/ is open to whoever sees the site, a browser with a pass included."""
        for value in ("", "  "):
            with override_settings(ADMIN_ALLOWED_IPS=value, EMAIL_HOST="mail.example.org"):
                notes = preflight.team_notes()
                self.assertEqual(len(notes), 2)
                self.assertIn("ADMIN_ALLOWED_IPS is empty", notes[1])
                self.assertIn("set ADMIN_ALLOWED_IPS", notes[1])
        with override_settings(ADMIN_ALLOWED_IPS="203.0.113.7 2001:db8::/48", EMAIL_HOST="mail.example.org"):
            self.assertEqual(len(preflight.team_notes()), 1)
        # The variable reaches web for this alone (config/settings.py, docker-compose.yml)
        web = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]["web"]["environment"]
        self.assertEqual(web["ADMIN_ALLOWED_IPS"], "${ADMIN_ALLOWED_IPS:-}")

    def test_preflight_says_when_nobody_would_be_told(self):
        on = override_settings(ADMIN_ALLOWED_IPS="203.0.113.7")
        with on, override_settings(TELEGRAM_BOT_TOKEN="", TELEGRAM_MODERATION_CHAT="", EMAIL_HOST="", NETWORK="mainnet"):
            self.assertIn("nobody will be told of new keys", preflight.team_notes()[1])
        with on, override_settings(TELEGRAM_BOT_TOKEN="t", TELEGRAM_MODERATION_CHAT="", EMAIL_HOST="mail.example.org"), \
                patch.dict(preflight.community(), {"email": ""}):
            self.assertIn("nobody will be told of new keys", preflight.team_notes()[1])
        with on, override_settings(TELEGRAM_BOT_TOKEN="", EMAIL_HOST="", NETWORK="signet"):
            self.assertIn("only written to the log of web", preflight.team_notes()[1])
        for told in ({"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_MODERATION_CHAT": "-100", "EMAIL_HOST": ""},
                     {"TELEGRAM_BOT_TOKEN": "", "EMAIL_HOST": "mail.example.org"}):
            with on, override_settings(**told):
                self.assertEqual(len(preflight.team_notes()), 1, told)


class Logs(Base):
    """The token of an invite is in a path, and paths are what logs write: every log of the stack cuts it."""
    TOKEN = "s3cr3t-Token_of-43-characters-000000000000"

    def test_django_writes_the_path_without_it(self):
        record = logging.LogRecord("django.request", logging.WARNING, __file__, 1, "%s: %s",
                                   ("Not Found", f"/team/join/{self.TOKEN}/"), None)
        self.assertTrue(quiet.QuietPaths().filter(record))
        self.assertEqual(record.getMessage(), "Not Found: /team/join/-/")
        record = logging.LogRecord("x", logging.ERROR, __file__, 1, f"Internal Server Error: /team/join/{self.TOKEN}/?a=1", None, None)
        quiet.QuietPaths().filter(record)
        self.assertEqual(record.getMessage(), "Internal Server Error: /team/join/-/?a=1")
        record = logging.LogRecord("x", logging.ERROR, __file__, 1, "%(path)s %(n)d", ({"path": f"/team/join/{self.TOKEN}", "n": 3},), None)
        quiet.QuietPaths().filter(record)
        self.assertEqual(record.getMessage(), "/team/join/- 3")

    def test_the_console_of_web_has_the_filter(self):
        self.assertEqual(settings.LOGGING["handlers"]["console"]["filters"], ["quiet_paths"])
        self.assertEqual(settings.LOGGING["filters"]["quiet_paths"]["()"], "config.quiet.QuietPaths")
        console = [handler for handler in logging.getLogger().handlers if isinstance(handler, logging.StreamHandler)]
        self.assertTrue(any(isinstance(found, quiet.QuietPaths) for handler in console for found in handler.filters))

    def test_a_404_of_an_invite_reaches_the_console_without_the_token(self):
        import io

        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.addFilter(quiet.QuietPaths())
        logger = logging.getLogger("django.request")
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)
        self.assertEqual(self.client.get(f"/team/join/{self.TOKEN}/").status_code, 404)
        with override_settings(TEAM_ACCESS=False):
            self.assertEqual(self.client.get(f"/team/join/{self.TOKEN}/").status_code, 404)
        written = stream.getvalue()
        self.assertEqual(written.count("Not Found: /team/join/-/"), 2)
        self.assertNotIn(self.TOKEN, written)

    def test_gunicorn_writes_the_path_without_it(self):
        from datetime import timedelta as delta

        source = (ROOT / "config" / "gunicorn.py").read_text()
        self.assertIn('logger_class = "config.quiet.AccessLog"', source)
        cls = quiet.AccessLog

        class Response:
            status, sent, headers = "404 Not Found", 10, []

        environ = {"REQUEST_METHOD": "GET", "RAW_URI": f"/team/join/{self.TOKEN}/?x=1", "PATH_INFO": f"/team/join/{self.TOKEN}/",
                   "QUERY_STRING": "x=1", "SERVER_PROTOCOL": "HTTP/1.1", "HTTP_REFERER": f"https://x/team/join/{self.TOKEN}/"}
        atoms = cls.atoms(cls.__new__(cls), Response(), {"referer": f"https://x/team/join/{self.TOKEN}/"}.items(), environ,
                          delta(seconds=1))
        self.assertEqual(atoms["U"], "/team/join/-/")
        self.assertEqual(atoms["m"], "GET")
        self.assertEqual([name for name, value in atoms.items() if isinstance(value, str) and self.TOKEN in value], [])
        # Any other path is written as it is
        environ["PATH_INFO"] = environ["RAW_URI"] = "/en/shops/"
        self.assertEqual(cls.atoms(cls.__new__(cls), Response(), [], environ, delta(seconds=1))["U"], "/en/shops/")

    def mangled(self, secret, word="join"):
        """The ways the reviewer found a secret in a path written whole, and a few more: (address, how it must be written)."""
        return [(f"/team/{word}/{secret}/", f"/team/{word}/-/"), (f"/team/{word}/{secret}", f"/team/{word}/-"),
                (f"/TEAM/{word.upper()}/{secret}/", f"/TEAM/{word.upper()}/-/"), (f"/Team/{word.title()}/{secret}/x/", f"/Team/{word.title()}/-/x/"),
                (f"/team//{word}/{secret}/", f"/team//{word}/-/"), (f"//team/{word}//{secret}/", f"//team/{word}//-/"),
                (f"/en/team/{word}/{secret}/", f"/en/team/{word}/-/"), (f"/it/x/team/{word}/{secret}/?a=b", f"/it/x/team/{word}/-/?a=b")]

    def test_no_way_of_writing_the_path_gets_a_token_or_a_challenge_into_the_log_of_web(self):
        k1 = "ab" * 32
        for secret, word in ((self.TOKEN, "join"), (k1, "login"), (k1, "join"), (self.TOKEN, "login")):
            for address, written in self.mangled(secret, word):
                self.assertEqual(quiet.quiet(address), written)
                self.assertEqual(quiet.quiet(f"Not Found: {address}"), f"Not Found: {written}")
        # Percent-encoded letters, which the site reads as the same path, are written decoded and cut
        for address in (f"/team/%6Aoin/{self.TOKEN}/", f"/team/join/%73{self.TOKEN[1:]}/", f"/%74eam/login/{k1}/",
                        f"/x?next=%2Fteam%2Fjoin%2F{self.TOKEN}%2F"):
            written = quiet.quiet(address)
            self.assertNotIn(self.TOKEN, written)
            self.assertNotIn(self.TOKEN[1:], written)
            self.assertNotIn(k1, written)
        # The challenge in the query of the wallet's callback, wherever a whole address is written
        self.assertEqual(quiet.quiet(f"GET /team/auth/?tag=login&k1={k1}&sig=00 HTTP/1.1"), "GET /team/auth/?tag=login&k1=-&sig=00 HTTP/1.1")
        self.assertEqual(quiet.quiet(f"/team/auth/?K1={k1}"), "/team/auth/?K1=-")
        # What is not a secret stays as it is
        for address in ("/team/login/", "/team/poll/", "/team/join.js", "/team/check/", "/en/shops/", "/en/shops/?next=%2Fen%2F",
                        "/theme/logo.svg", "/team/auth/?tag=login&sig=00&key=02ab"):
            self.assertEqual(quiet.quiet(address), address)

    def gunicorn_writes(self, method, path, query=""):
        """The line gunicorn's access log writes for a request, with the logger and the format of config/gunicorn.py,
        and every atom it could write with another format."""
        from datetime import timedelta as delta

        class Response:
            status, sent, headers = "200 OK", 10, []

        settings_of = __import__("runpy").run_path(str(ROOT / "config" / "gunicorn.py"))
        self.assertEqual(settings_of["logger_class"], "config.quiet.AccessLog")
        raw = path + ("?" + query if query else "")
        environ = {"REQUEST_METHOD": method, "RAW_URI": raw, "PATH_INFO": path, "QUERY_STRING": query,
                   "SERVER_PROTOCOL": "HTTP/1.1", "HTTP_REFERER": f"https://{settings.SITE_HOST}{raw}"}
        cls = quiet.AccessLog
        atoms = cls.atoms(cls.__new__(cls), Response(), {"referer": environ["HTTP_REFERER"]}.items(), environ, delta(seconds=1))
        return settings_of["access_log_format"] % atoms, " ".join(value for value in atoms.values() if isinstance(value, str))

    def test_a_whole_invite_and_a_whole_sign_in_leave_no_secret_in_the_log_of_web(self):
        """What the reviewer did by hand: read the log of web after an invite, find the challenge, sign it first. Every
        request of an invite and of a sign-in is made here, the two logs of web are written as the server writes them
        (gunicorn's access line with its real logger and format, Django's console with its real filter), and neither
        holds the token or a challenge."""
        import io

        console = io.StringIO()
        handler = logging.StreamHandler(console)
        handler.setLevel(logging.DEBUG)
        for found in settings.LOGGING["handlers"]["console"]["filters"]:
            self.assertEqual(settings.LOGGING["filters"][found]["()"], "config.quiet.QuietPaths")
            handler.addFilter(quiet.QuietPaths())
        watched = [logging.getLogger(name) for name in ("django", "django.request", "team", "config", "shops", "website")]
        for logger in watched:
            logger.addHandler(handler)
            self.addCleanup(logger.removeHandler, handler)
        asked = []

        def get(browser, url, data=None, method="get"):
            response = getattr(browser, method)(url, data or {})
            request = response.wsgi_request
            asked.append((request.method, request.path, request.META.get("QUERY_STRING", "")))
            return response

        _invite, token = self.invite()
        key = wallet()
        page = get(self.client, f"/team/join/{token}/").content.decode()
        invite_k1 = k1_of(re.search(r"lightning:(LNURL1[A-Z0-9]+)", page)[1])
        script, poll = re.search(r'<script src="([^"]+)" data-poll="([^"]+)"', page).groups()
        get(self.client, script)
        self.assertEqual(get(self.client, poll).json(), {"state": "pending"})
        self.assertEqual(get(self.browser(), "/team/auth/", signed(invite_k1, key)).json(), {"status": "OK"})
        self.assertEqual(get(self.client, poll).json(), {"state": "ok"})
        get(self.client, f"/team/join/{token}/")                       # the link again: spent, a 404 Django logs
        get(self.client, f"/team/join/{token}/", method="post")        # and a method it does not take
        # The ways of writing it that the review found logged whole, from an address that reaches Django
        for address, _written in self.mangled(token) + self.mangled(invite_k1, "login"):
            if not address.startswith("//"):  # the test client reads two slashes at the start as the name of a host
                get(self.client, address.split("?")[0])
        laptop = self.browser()
        code = get(laptop, "/team/login/").json()
        sign_in_k1 = k1_of(code["lnurl"])
        self.assertEqual(get(laptop, code["poll"]).json(), {"state": "pending"})
        get(self.browser(), "/team/auth/", signed(sign_in_k1, key))
        self.assertEqual(get(laptop, code["poll"]).json(), {"state": "ok"})
        get(laptop, code["poll"])
        get(laptop, "/en/")
        with override_settings(TEAM_ACCESS=False):
            get(self.client, f"/team/join/{token}/")
            get(self.client, f"/team/login/{sign_in_k1}/")

        self.assertGreater(len(asked), 25)
        secrets_ = {"the token": token, "the invite's challenge": invite_k1, "the sign-in's challenge": sign_in_k1}
        lines = []
        for method, path, query in asked:
            line, every_atom = self.gunicorn_writes(method, path, query)
            lines.append(line)
            for what, secret in secrets_.items():
                self.assertNotIn(secret, line, f"{what} in gunicorn's line for {method} {quiet.quiet(path)}")
                self.assertNotIn(secret, every_atom, f"{what} in an atom of gunicorn for {method} {quiet.quiet(path)}")
        written = console.getvalue()
        for what, secret in secrets_.items():
            self.assertNotIn(secret, written, f"{what} on Django's console")
        # Not an empty check: the requests were logged, cut
        self.assertIn(' "GET /team/join/-/" ', " ".join(lines))
        self.assertIn(' "GET /team/login/-/" ', " ".join(lines))
        self.assertIn(' "GET /team/poll/" ', " ".join(lines))
        self.assertIn("Not Found: /team/join/-/", written)
        self.assertIn("Method Not Allowed (POST): /team/join/-/", written)
        self.assertIn("Not Found: /TEAM/JOIN/-/", written)
        # And the check does see a secret that is written: without the cut, all three are there
        nothing = re.compile("(a path nobody asks for)")
        with patch.object(quiet, "SECRET_PATH", nothing), patch.object(quiet, "SECRET_QUERY", nothing):
            uncut = " ".join(self.gunicorn_writes(method, path, query)[1] for method, path, query in asked)
        for secret in secrets_.values():
            self.assertIn(secret, uncut)

    def test_caddy_cuts_them_from_both_its_logs(self):
        """A field of Caddy's logs takes one filter. The access log has one expression for the query and the two paths;
        the error log keeps its filter of the query, and the paths are cut by a second filter around the JSON, which
        Caddy 2.10 hands the fields of an object by their own name: both names are written. The same expression as
        config/quiet.py's. The real run is in the feature's page."""
        caddyfile = (ROOT / "Caddyfile").read_text()
        error_log = caddyfile.split("log default {")[1].split("\n\t}")[0]
        access_log = caddyfile.split("(access-on) {")[1].split("\n}")[0]
        inner = error_log.split("wrap filter {")[1].split("fields {")[1].split("\n\t\t\t\t}")[0]
        pattern = quiet.SECRET_PATH.pattern.replace(r"[^/?\s]+", "[^/?]+")
        self.assertIn(f"\t\t\t\t\turi regexp {pattern} ${{1}}-\n", inner + "\n")
        self.assertIn(f"\t\t\t\t\trequest>uri regexp {pattern} ${{1}}-\n", inner + "\n")
        self.assertIn("request>uri query {", error_log.split("wrap filter {")[1])
        self.assertIn(f"request>uri regexp {pattern}|([?]).* ${{1}}${{2}}\n", access_log)
        k1 = "ab" * 32
        error, access = re.compile(pattern), re.compile(f"{pattern}|([?]).*")
        for secret, word in ((self.TOKEN, "join"), (k1, "login")):
            for address, written in self.mangled(secret, word):
                self.assertEqual(error.sub(r"\g<1>-", address), written)
                in_access = access.sub(r"\g<1>\g<2>", address)
                self.assertNotIn(secret, in_access)
                self.assertEqual(in_access, written.replace("-", "").split("?")[0] + ("?" if "?" in address else ""))
        for asked, written in (("/team/join.js", "/team/join.js"), ("/team/login/", "/team/login/"), ("/team/poll/", "/team/poll/"),
                               ("/en/shops/?token=abc&k=1", "/en/shops/?"), ("/en/", "/en/"),
                               (f"/team/auth/?tag=login&k1={k1}&sig=00", "/team/auth/?"),
                               (f"/en/?next=/team/join/{self.TOKEN}/", "/en/?")):
            self.assertEqual(access.sub(r"\g<1>\g<2>", asked), written, asked)
        # The error log's own filter of the query still deletes the challenge of the wallet's callback
        self.assertIn("delete k1\n", error_log.split("request>uri query {")[1].split("}")[0])
        # The routes are still the ones the rules are written for
        self.assertEqual(reverse("team_join", args=[self.TOKEN]), f"/team/join/{self.TOKEN}/")
        self.assertEqual(reverse("team_poll", args=[k1]), f"/team/login/{k1}/")


class CaddyRules(Base):
    """What can be read of Caddy's side without running it (the run with Docker Compose is in the feature's page)."""

    def setUp(self):
        super().setUp()
        self.caddyfile = (ROOT / "Caddyfile").read_text()
        self.development = (ROOT / "caddy" / "development.caddy").read_text()

    def snippet(self, name):
        return self.caddyfile.split(f"({name}) {{\n")[1].split("\n}\n")[0]

    def test_off_imports_nothing(self):
        self.assertIn("import team-paths-{$TEAM_ACCESS:off}", self.development)
        self.assertIn("import team-pass-{$TEAM_ACCESS:off}", self.development)
        for name in ("team-paths-off", "team-pass-off", "team-wallet-off"):
            self.assertIn(f"({name}) {{\n}}\n", self.caddyfile)
        # Only a site under construction has the rules: in maintenance everything stays closed, production is open
        for mode in ("maintenance", "production"):
            self.assertNotIn("team", (ROOT / "caddy" / f"{mode}.caddy").read_text().lower())

    def test_the_pass_is_asked_before_the_page_under_construction(self):
        self.assertLess(self.development.index("import team-pass-"), self.development.index("handle @outside"))
        rules = self.snippet("team-pass-on")
        self.assertIn("forward_auth web:8000", rules)
        self.assertIn(f"uri {reverse('team_check')}\n", rules)
        # The address the limits count is the one Caddy trusts, as for every other request
        self.assertIn("header_up X-Forwarded-For {client_ip}", rules)
        self.assertNotIn("copy_headers", rules)
        # Only from outside, only on the site's entrance, and never for the addresses that are open anyway
        matcher = " ".join(rules.split("@team_pass {")[1].split("\n\t}")[0].split())
        self.assertIn("not client_ip {$DEV_ALLOWED_IPS:127.0.0.1}", matcher)
        self.assertIn("not path /health/ {$OPEN_PATHS}", matcher)
        self.assertIn("not { import team-open }", matcher)
        self.assertIn("header X-Entrance site", matcher)

    def test_the_cookie_caddy_looks_for_and_clears_is_djangos(self):
        rules = self.snippet("team-pass-on")
        shape = re.search(r'header_regexp Cookie "([^"]+)"', rules)[1]
        self.assertIn(f"{access.PASS_COOKIE}=", shape)
        team_key, key = self.member()
        self.sign_in(self.client, key)
        cookie = self.client.cookies["team_pass"]
        # Every pass Django makes has the shape Caddy lets through, whatever the key's number, alone or among other cookies
        for pk in (1, 7, 10**6, 2**62):
            team_key.pk = pk
            value = access.pass_for(team_key)
            for header in (f"team_pass={value}", f"a=1; team_pass={value}", f"team_pass={value}; b=2", f"a=1; team_pass={value}; b=2"):
                self.assertTrue(re.search(shape, header), header)
        self.assertTrue(re.search(shape, f"team_pass={cookie.value}"))
        # And what cannot be a pass asks Django nothing: no cookie, an empty or short one, one of another name
        for header in ("", "a=1", "team_pass=", "team_pass=short", f"not_team_pass={cookie.value}", f"team_pass={'x' * 500}",
                       "team_pass=<script>" + "a" * 60):
            self.assertFalse(re.search(shape, header), header)
        cleared = re.search(r'header Set-Cookie "([^"]+)"', rules)[1]
        self.assertEqual(cleared, f"{access.PASS_COOKIE}=; Path={cookie['path']}; Max-Age=0; HttpOnly; Secure; SameSite=Lax")
        # Cleared only when Django refuses the pass, not when it does not answer
        refused = rules.split("handle_response @refused {")[1].split("}")[0]
        failed = rules.split("handle_response @failed {")[1].split("}")[0]
        self.assertIn("@refused status 401", rules)
        self.assertIn("Set-Cookie", refused)
        self.assertNotIn("Set-Cookie", failed)
        for answer in (refused, failed):
            self.assertIn("import maintenance", answer)

    def open_addresses(self):
        """The expression of `team-open`: what Caddy lets through to Django from outside."""
        rules = self.snippet("team-open")
        self.assertEqual(rules.split("\n")[:2], ["\tmethod GET", "\theader X-Entrance site"])
        return re.compile(re.search(r'expression `\{http\.request\.uri\}\.matches\("([^"]+)"\)`', rules)[1])

    def test_only_the_addresses_that_exist_are_open_from_outside_and_only_with_get(self):
        self.assertEqual(" ".join(self.snippet("team-paths-on").split()), "not { import team-open }")
        expression = self.open_addresses()
        _invite, token = self.invite()
        k1 = k1_of(self.client.get("/team/login/").json()["lnurl"])
        values = {"k1": k1, "token": token}
        # Every route of team/urls.py, as Django itself writes it, and with a query; a route added tomorrow fails here
        # until the Caddyfile lets it through
        found = [route for route in routes(get_resolver().url_patterns) if route.startswith("team/")]
        self.assertEqual(len(found), len(TEAM_URLS))
        for route in found:
            url = "/" + re.sub(r"<(?:\w+:)?(\w+)>", lambda match: values[match[1]], route)
            self.assertTrue(expression.search(url), url)
            self.assertTrue(expression.search(url + "?tag=login&k1=" + k1), url)
            self.assertNotEqual(self.client.get(url).status_code, 404, url)
        # A token as `secrets.token_urlsafe` makes them, whatever its letters
        for sample in ("-" * 43, "_" * 43, "aZ09_-" * 8):
            self.assertTrue(expression.search(f"/team/join/{sample}/"))
        # Everything else under /team/, and every other way of writing those addresses, is a path like any other: the
        # under-construction page, not Django's 404 with the community's name, menu and email
        for url in ("/team/", "/team", "/team/x/", "/team/login", "/team/login/x/", f"/team/login/{k1.upper()}/",
                    f"/team/login/{k1}", f"/team/login/{k1}/x/", "/TEAM/login/", "/team/LOGIN/", "/Team/Auth/", "//team/login/",
                    "/team//login/", "/x/../team/login/", "/team/./login/", "/team/%6Cogin/", "/team/login%2F", "/en/team/login/",
                    "/team/join/", "/team/join/short/", f"/team/join/{token}", f"/team/join/{token}/x", f"/team/join/{token}//",
                    f"/TEAM/JOIN/{token}/", f"/team//join/{token}/", f"/en/team/join/{token}/", "/team/join.js/", "/team/joinXjs",
                    "/team/check", "/team/poll", "/team/auth", "/team/admin/", "http://x/team/login/", "/team/login/?a=1\n/x"):
            self.assertFalse(expression.search(url), url)

    def test_the_wallets_callback_is_not_taken_for_a_robot(self):
        """Wallets written in Go or in Python say so in their User-Agent, and the rule of the robots refuses those names:
        it lets /team/auth/ through, and only that, as it does /health/. Off, the rule is the one it was."""
        closed = self.caddyfile.split("(closed) {\n")[1].split("\n}\n")[0]
        crawlers = closed.split("@crawlers {")[1].split("\n\t}")[0]
        self.assertIn("not path /health/\n", crawlers)
        self.assertIn("import team-wallet-{$TEAM_ACCESS:off}", crawlers)
        self.assertEqual(self.snippet("team-wallet-on").strip(), f"not path {reverse('team_auth')}")
        self.assertIn("(team-wallet-off) {\n}\n", self.caddyfile)
        names = re.search(r'header_regexp User-Agent "([^"]+)"', crawlers)[1]
        for agent in ("Go-http-client/2.0", "python-requests/2.32"):
            self.assertTrue(re.search(names, agent), agent)

    def test_both_services_get_the_switch(self):
        services = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]
        for name in ("web", "caddy"):
            self.assertEqual(services[name]["environment"]["TEAM_ACCESS"], "${TEAM_ACCESS:-off}", name)
        release = yaml.safe_load((ROOT / "compose.release.yml").read_text().replace("!reset", "").replace("!override", ""))
        for name in ("web", "caddy"):
            self.assertNotIn("environment", release["services"][name], name)  # the release keeps the same environment


class Documents(Base):
    def test_the_variable_and_the_feature_are_documented(self):
        # The image leaves .env.example and docs/ out (.dockerignore): there only its own list is checked
        if (ROOT / ".env.example").is_file():
            self.assertIn("\nTEAM_ACCESS=off\n", (ROOT / ".env.example").read_text())
        if (ROOT / "docs").is_dir():
            self.assertIn("| `TEAM_ACCESS` | `off` |", (ROOT / "docs" / "configuration.md").read_text())
            self.assertIn("(team-access.md)", (ROOT / "docs" / "features" / "README.md").read_text())
            page = (ROOT / "docs" / "features" / "team-access.md").read_text()
            for word in ("TEAM_ACCESS=on", "/team/login/", "/team/check/", "/team/auth/", "/team/join/", "team_pass",
                         "team_k1", "seven days"):
                self.assertIn(word, page)
            self.assertIn("`team.TeamKey`", (ROOT / "docs" / "privacy.md").read_text())
        if (ROOT / ".dockerignore").is_file():
            # An app the image does not take in stops its build (INSTALLED_APPS names it)
            self.assertIn("\n!team\n", (ROOT / ".dockerignore").read_text())


class DefaultPage(Base):
    def test_the_default_page_under_construction_asks_only_after_the_gesture(self):
        page = (ROOT / "maintenance" / "index.html").read_text()
        script = page.split("<script>")[1].split("</script>")[0]
        self.assertNotIn("src=", page.split("<script")[1].split(">")[0])
        # One request, inside the function the gesture calls; nothing at load, no file of the site
        self.assertEqual(script.count('fetch("/team/login/"'), 1)
        self.assertLess(script.index("async function signIn()"), script.index('fetch("/team/login/"'))
        self.assertNotIn("/static/", page)
        self.assertIn('if (location.hash === "#team") signIn();', script)


class Translations(Base):
    """Every string of the feature is in every language of locale/ (its templates are not under templates/)."""

    def test_every_language(self):
        folder = ROOT / "team"
        ids = python_ids(path for path in folder.glob("*.py") if not path.name.startswith("test"))
        ids |= template_ids(sorted((folder / "templates").rglob("*.html")))
        self.assertGreater(len(ids), 40)
        untranslated(self, ids, exempt={"Home"})

    def test_the_invite_page_speaks_the_visitors_language(self):
        _invite, token = self.invite()
        page = self.browser(**{"Accept-Language": "it"}).get(f"/team/join/{token}/").content.decode()
        self.assertIn('<html lang="it">', page)
        self.assertNotIn("Scan the code", page)


@skipUnless(connection.vendor == "postgresql", "two transactions at the same time need PostgreSQL")
@override_settings(TEAM_ACCESS=True, SITE_MODE="development", DEV_ALLOWED_IPS="")
class TwoSignaturesAtOnce(TransactionTestCase):
    """Two wallets sign two codes of the same invite in the same instant, each in its own transaction: one key. And
    one wallet signing two invites in the same instant: one key, and the other invite is not used up."""

    def at_once(self, pairs):
        """Runs join_with for each (challenge, key) at the same instant, each in its own connection: the keys made."""
        barrier = threading.Barrier(len(pairs))
        outcomes = []

        def sign(challenge, key):
            try:
                barrier.wait(timeout=10)
                outcomes.append(views.join_with(challenge, key)[0])
            finally:
                connections.close_all()

        threads = [threading.Thread(target=sign, args=pair) for pair in pairs]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(len(outcomes), len(pairs))
        return [made for made in outcomes if made is not None]

    def new_invite(self, name):
        invite = TeamInvite(name=name, role="team")
        invite.arm()
        invite.save()
        return invite

    def test_one_key(self):
        for _round in range(5):
            TeamKey.objects.all().delete()
            TeamChallenge.objects.all().delete()
            invite = self.new_invite("Anna")
            pairs = [(TeamChallenge.new(invite), wallet().public_key.format().hex()) for _ in range(4)]
            self.assertEqual(len(self.at_once(pairs)), 1)
            self.assertEqual(TeamKey.objects.count(), 1)
            invite.refresh_from_db()
            self.assertEqual(invite.key, TeamKey.objects.get())
            self.assertEqual(TeamChallenge.objects.exclude(linking_key="").count(), 1)
        # The same wallet on two invites at once: one key, and the invite that lost is not used up
        TeamKey.objects.all().delete()
        TeamInvite.objects.all().delete()
        key = wallet().public_key.format().hex()
        pairs = [(TeamChallenge.new(self.new_invite(name)), key) for name in ("Anna", "Bruno")]
        self.assertEqual(len(self.at_once(pairs)), 1)
        self.assertEqual(TeamKey.objects.count(), 1)
        self.assertEqual(TeamInvite.objects.filter(used__isnull=True).count(), 1)


class Command(Base):
    def test_check_and_makemigrations_are_quiet(self):
        call_command("check")
        call_command("makemigrations", "--check", "--dry-run", verbosity=0)

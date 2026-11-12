"""Whoever runs the site cannot pass for a merchant: the staff and the sessions born from the admin's password are
visitors outside /admin/ (config/guard.py). The changes to the account of a shop's owner: test_guard_watch.py."""
import re
from collections import Counter
from datetime import timedelta
from unittest.mock import patch

import coincurve
import requests
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import mail
from django.http import HttpResponse
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import get_resolver, path
from django.urls.resolvers import URLResolver
from django.utils import timezone, translation

from config import urls as site_urls
from config.guard import ADMIN_SESSION, StaffApart, current_request

from .models import LoginChallenge, Profile, Shop, StaffRegister
from .tests import admin_session, reset_limits

User = get_user_model()


def sign_in_as_staff_session(client, user):
    """A session born from the admin's password, for any account: as /admin/login/ makes it."""
    client.force_login(user)
    session = client.session
    session[ADMIN_SESSION] = True
    session.save()


class Base(TestCase):
    def setUp(self):
        reset_limits()
        # No request leaves the machine: a page that asked would be a page that trusted the visitor to be somebody
        patcher = patch("requests.sessions.Session.request", side_effect=requests.ConnectionError)
        patcher.start()
        self.addCleanup(patcher.stop)


# ---------------------------------------------------------------------------------------------------------------------
# The staff is a visitor outside /admin/
# ---------------------------------------------------------------------------------------------------------------------

def routes(patterns, prefix=""):
    """Every route of the site, as a string with the converters in <>: the languages' prefix included."""
    for pattern in patterns:
        route = prefix + str(pattern.pattern)
        if route.startswith("admin/"):
            continue
        if isinstance(pattern, URLResolver):
            yield from routes(pattern.url_patterns, route)
        else:
            yield route


class StaffWalk(Base):
    """The test that keeps a page added tomorrow from escaping: it walks every URL of the site, GET and POST, with a
    staff account that owns a shop, signed in from the admin's password, and wants the same answer as a stranger gets."""

    def setUp(self):
        super().setUp()
        admin_session(self.client)
        self.boss = User.objects.get(username="boss")
        self.shop = Shop.objects.create(owner=self.boss, name="Bar", lightning_address="bar@ln.example.org")
        self.waiting = Shop.objects.create(owner=self.boss, name="Waiting", awaiting_wallet=True)
        self.register = StaffRegister.objects.create(shop=self.shop, name="Anna", code="c" * 32,
                                                     code_until=timezone.now() + timedelta(hours=1))
        self.client.raise_request_exception = False
        self.stranger = self.client_class(raise_request_exception=False)
        self.values = {"shop": str(self.shop.id), "register": str(self.register.id), "invoice": "inv1", "external": "x",
                       "pk": "00000000-0000-0000-0000-000000000001", "slug": "home", "path": "x",
                       "year": "2026", "secret": "s3cret", "name": "0" * 64, "month": "10", "number": "1",
                       "address": "tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx",
                       # Team access (team/): a challenge and an invite's token
                       "k1": "a" * 64, "token": "t" * 43}

    def url(self, route):
        return "/" + re.sub(r"<(?:\w+:)?(\w+)>", lambda found: self.values[found.group(1)], route)

    def all_routes(self):
        with translation.override("en"):
            return sorted(set(routes(get_resolver().url_patterns)))

    def test_the_walk_sees_the_whole_site(self):
        found = self.all_routes()
        # Not an empty walk: the pages of the merchant are in it, and nothing under /admin/
        for route in ("en/shops/edit/<uuid:shop>/", "en/shops/wallet/<uuid:shop>/", "en/shops/connect/<uuid:shop>/",
                      "en/shops/registers/<uuid:shop>/", "en/shops/preview/<uuid:shop>/",
                      "en/shops/claim/", "en/shops/sign/<uuid:shop>/", "en/shops/", "en/shops/email/confirm/",
                      "register/<uuid:shop>/manifest.webmanifest"):
            self.assertIn(route, found)
        self.assertGreater(len(found), 50)
        self.assertFalse([route for route in found if route.startswith("admin")])

    def test_every_url_gives_the_staff_the_answer_of_a_stranger(self):
        walked = 0
        answers = Counter()
        for route in self.all_routes():
            url = self.url(route)
            for verb in ("get", "post"):
                theirs = getattr(self.client, verb)(url, {})
                ours = getattr(self.stranger, verb)(url, {})
                what = f"{verb.upper()} {url}"
                self.assertFalse(theirs.wsgi_request.user.is_authenticated, what)
                self.assertEqual(theirs.status_code, ours.status_code, what)
                self.assertEqual(theirs.get("Location"), ours.get("Location"), what)
                answers[theirs.status_code] += 1
                walked += 1
        self.assertGreater(walked, 100)
        # The walk does see the merchant's pages (they send a stranger to the sign-in page), and no page fell over
        self.assertGreater(answers[302], 25, dict(answers))
        self.assertEqual(answers[500], 0)
        # Nothing was made or changed on the way
        self.assertEqual(StaffRegister.objects.count(), 1)
        self.assertEqual(Shop.objects.count(), 2)

    def test_the_merchants_pages_send_the_staff_to_the_sign_in_page_which_says_why(self):
        for route in ("edit/{s}/", "wallet/{w}/", "connect/{w}/", "registers/{s}/", "sign/{s}/", "preview/{s}/",
                      "card/{s}/", "remove/{s}/", "claim/", "first-day/{s}/", "new/"):
            url = "/en/shops/" + route.format(s=self.shop.id, w=self.waiting.id)
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302, url)
            self.assertTrue(response["Location"].startswith("/en/shops/?next="), url)
        panel = self.client.get("/en/shops/")
        self.assertContains(panel, "Staff accounts do not open the pages of a shop")
        self.assertNotContains(panel, "Your shops")
        self.assertNotContains(self.stranger.get("/en/shops/"), "Staff accounts do not open")

    def test_the_actions_of_the_merchant_do_nothing_for_the_staff(self):
        Profile.objects.create(user=self.boss, email="old@example.org")
        with patch("shops.views.refusal", return_value=None) as probe:
            self.client.post("/en/shops/", {"email": "new@example.org"})
            self.client.post(f"/en/shops/edit/{self.shop.id}/", {"name": "Mine", "telegram_chat": "123456"})
            self.client.post(f"/en/shops/wallet/{self.waiting.id}/", {"lightning_address": "me@example.org"})
            self.client.post(f"/en/shops/registers/{self.shop.id}/", {"name": "Spy"})
            self.client.post(f"/en/shops/registers/{self.shop.id}/{self.register.id}/retire/")
            self.client.post(f"/en/shops/remove/{self.shop.id}/", {"confirm": "on"})
            self.client.post(f"/en/shops/first-day/{self.shop.id}/", {"step": "sign"})
            self.client.post("/en/shops/new/", {"name": "Mine", "latitude": "41.9", "longitude": "12.5"})
        probe.assert_not_called()
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(Profile.objects.get(user=self.boss).email, "old@example.org")
        shop = Shop.objects.get(pk=self.shop.pk)
        self.assertEqual((shop.name, shop.telegram_chat), ("Bar", ""))
        self.assertEqual(Shop.objects.get(pk=self.waiting.pk).lightning_address, "")
        self.assertIsNone(StaffRegister.objects.get(pk=self.register.pk).retired)
        self.assertEqual((StaffRegister.objects.count(), Shop.objects.count()), (1, 2))

    def test_the_owners_register_and_practice_do_not_know_them_as_the_owner(self):
        for url in (f"/en/shops/register/{self.shop.id}/", f"/en/shops/practice/{self.shop.id}/"):
            page = self.client.get(url)
            if page.status_code == 200:
                self.assertEqual(page.context["owner"], False, url)

    def test_a_session_born_from_the_password_stays_apart_after_is_staff_is_taken_away(self):
        """The strategy of the story: staff for an hour, then no trace. The session keeps what it was born from."""
        self.boss.is_staff = self.boss.is_superuser = False
        self.boss.save()
        self.assertTrue(self.client.session[ADMIN_SESSION])
        response = self.client.get(f"/en/shops/edit/{self.shop.id}/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get("/admin/").status_code, 302)

    def test_a_staff_account_with_an_ordinary_session_stays_apart_too(self):
        """Opened by the link in an email or a wallet before the account became staff."""
        merchant = User.objects.create(username="merchant")
        shop = Shop.objects.create(owner=merchant, name="Pub", lightning_address="bar@ln.example.org")
        client = self.client_class()
        client.force_login(merchant)
        self.assertEqual(client.get(f"/en/shops/edit/{shop.id}/").status_code, 200)
        User.objects.filter(pk=merchant.pk).update(is_staff=True)
        self.assertEqual(client.get(f"/en/shops/edit/{shop.id}/").status_code, 302)
        User.objects.filter(pk=merchant.pk).update(is_staff=False, is_superuser=True)
        self.assertEqual(client.get(f"/en/shops/edit/{shop.id}/").status_code, 302)
        User.objects.filter(pk=merchant.pk).update(is_superuser=False)
        self.assertEqual(client.get(f"/en/shops/edit/{shop.id}/").status_code, 200)

    def test_a_merchant_who_is_not_staff_is_not_touched(self):
        merchant = User.objects.create(username="merchant")
        shop = Shop.objects.create(owner=merchant, name="Pub", lightning_address="bar@ln.example.org")
        client = self.client_class()
        client.force_login(merchant)
        self.assertContains(client.get("/en/shops/"), "Your shops")
        self.assertEqual(client.get(f"/en/shops/edit/{shop.id}/").status_code, 200)
        self.assertNotContains(client.get("/en/shops/"), "Staff accounts do not open")

    def test_the_admin_still_opens_for_the_staff(self):
        self.assertEqual(self.client.get("/admin/").status_code, 200)
        self.assertEqual(self.client.get("/admin/today/").status_code, 200)
        self.assertEqual(self.client.get("/admin/shops/shop/").status_code, 200)
        # The pages of the site, public ones too, answer a visitor who is not signed in: no menu of the shops' area
        self.assertEqual(self.client.get("/en/shops/map/").status_code, 200)

    def test_only_the_admin_lives_under_the_admins_path(self):
        """The staff keeps its account on /admin/..., so nothing of a merchant may answer there: the routes that start with
        admin/ are the admin site, its login and the association's list, and nothing else."""
        under = {str(pattern.pattern): pattern for pattern in get_resolver().url_patterns
                 if str(pattern.pattern).startswith("admin")}
        self.assertEqual(sorted(under), ["admin/", "admin/login/", "admin/today/"])
        self.assertEqual(under["admin/login/"].callback, site_urls.admin_login)
        self.assertEqual(under["admin/today/"].name, "admin_dashboard")
        self.assertEqual((under["admin/"].app_name, under["admin/"].namespace), ("admin", "admin"))
        # And a path that tries to leave /admin/ for a shop's page finds nothing
        for url in (f"/admin/../en/shops/edit/{self.shop.id}/", f"/admin/%2e%2e/en/shops/edit/{self.shop.id}/",
                    f"/admin//en/shops/edit/{self.shop.id}/"):
            self.assertEqual(self.client.get(url).status_code, 404, url)

    def test_a_page_added_tomorrow_is_apart_too(self):
        with override_settings(ROOT_URLCONF="shops.test_guard"):
            seen = self.client.get("/tomorrow/").content
            self.assertEqual(seen, b"signed in: False")


class SignInAgain(Base):
    """An account that was staff and lost it keeps the session the admin's password made, with its flag. Signing in again
    as a merchant (the wallet, the email link) ends that session: the owner is the owner again."""

    def setUp(self):
        super().setUp()
        self.owner = User.objects.create(username=coincurve.PrivateKey().public_key.format().hex())
        Profile.objects.create(user=self.owner, email="owner@example.org")
        self.shop = Shop.objects.create(owner=self.owner, name="Bar", lightning_address="bar@ln.example.org")
        self.edit = f"/en/shops/edit/{self.shop.id}/"
        sign_in_as_staff_session(self.client, self.owner)

    def test_the_session_of_the_admin_keeps_the_owner_out(self):
        self.assertEqual(self.client.get(self.edit).status_code, 302)

    def test_the_email_link_ends_it(self):
        self.client.post("/en/shops/recovery/", {"email": "owner@example.org"})
        link = mail.outbox[-1].body.split(settings.PUBLIC_URL)[1].split()[0]
        self.assertEqual(self.client.post(link).status_code, 302)
        self.assertNotIn(ADMIN_SESSION, self.client.session)
        self.assertEqual(self.client.get(self.edit).status_code, 200)

    def test_the_wallet_ends_it(self):
        key = coincurve.PrivateKey()
        User.objects.filter(pk=self.owner.pk).update(username=key.public_key.format().hex())
        self.client.get("/en/shops/")
        k1 = LoginChallenge.objects.latest("created").k1
        signature = key.sign(bytes.fromhex(k1), hasher=None).hex()
        self.client.get("/lnurl-auth/", {"k1": k1, "sig": signature, "key": key.public_key.format().hex()})
        self.assertTrue(self.client.get("/lnurl-auth/status/").json()["logged_in"])
        self.assertNotIn(ADMIN_SESSION, self.client.session)
        self.assertEqual(self.client.get(self.edit).status_code, 200)

    def test_the_admins_own_sign_in_still_sets_it(self):
        staff = User.objects.create_user("volunteer", password="a long password to sign in 42", is_staff=True)
        client = Client()
        response = client.post("/admin/login/", {"username": "volunteer", "password": "a long password to sign in 42",
                                                 "next": "/admin/"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(client.session[ADMIN_SESSION])
        self.assertEqual(client.get("/admin/").status_code, 200)
        # Again, from a session that already has it: signing in sets it again after the sign-in has cleared it
        response = client.post("/admin/login/", {"username": "volunteer", "password": "a long password to sign in 42",
                                                 "next": "/admin/"})
        self.assertTrue(client.session[ADMIN_SESSION])
        self.assertEqual(client.get("/admin/").status_code, 200)
        self.assertEqual(staff.pk, int(client.session["_auth_user_id"]))


class SignOutOfTheAdministration(TestCase):
    def test_the_staff_can_sign_out_from_the_page_that_says_why(self):
        admin_session(self.client)
        page = self.client.get("/en/shops/")
        self.assertContains(page, '<form method="post" action="/admin/logout/">')
        self.assertContains(page, "Sign out of the administration")
        # With the CSRF check on, as a browser does: the token on the page is the one that works
        browser = Client(enforce_csrf_checks=True)
        browser.cookies = self.client.cookies
        token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', browser.get("/en/shops/").content.decode())[1]
        response = browser.post("/admin/logout/", {"csrfmiddlewaretoken": token, "next": "/en/shops/"})
        self.assertRedirects(response, "/en/shops/", fetch_redirect_response=False)
        self.assertEqual(browser.get("/admin/").status_code, 302)
        self.assertNotContains(browser.get("/en/shops/"), "Sign out of the administration")


def tomorrow(request):
    return HttpResponse(f"signed in: {request.user.is_authenticated}")


# A page nobody has written yet, in a copy of the site's URLs
urlpatterns = [*site_urls.urlpatterns, path("tomorrow/", tomorrow)]


class Middleware(TestCase):
    def test_a_visitor_without_a_cookie_does_not_open_the_session(self):
        request = RequestFactory().get("/en/shops/map/")
        from django.contrib.sessions.backends.db import SessionStore
        from django.contrib.auth.middleware import AuthenticationMiddleware
        request.session = SessionStore()
        AuthenticationMiddleware(lambda r: None).process_request(request)
        seen = {}

        def view(request):
            seen["staff"] = request.staff_user
            seen["current"] = current_request.get()
            return HttpResponse()

        StaffApart(view)(request)
        self.assertIsNone(seen["staff"])
        self.assertIs(seen["current"], request)
        self.assertFalse(request.session.accessed)
        self.assertIsNone(current_request.get())

    def test_the_staff_user_is_kept_for_the_page_that_says_why(self):
        boss = User.objects.create_superuser("boss")
        request = RequestFactory().get("/en/shops/")
        request.COOKIES[settings.SESSION_COOKIE_NAME] = "x"
        request.user = boss
        request.session = {}
        StaffApart(lambda r: HttpResponse())(request)
        self.assertEqual(request.staff_user, boss)
        self.assertFalse(request.user.is_authenticated)

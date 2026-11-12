import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from contextlib import contextmanager
from datetime import date, timedelta
from io import StringIO
from unittest import skipUnless
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlencode, urlsplit

import coincurve
import requests
import segno
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.contrib.sessions.models import Session
from django.db import IntegrityError, transaction
from django.forms import modelform_factory
from django.core import mail
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import override

from config import ratelimit
from config.community import community
from config.urls import ADMIN_SESSION

from .directory import featured_shops, statistics
from .forms import ShopDetailsForm, ShopForm
from .lnaddress import fetch, refusal
from .lnurl_auth import encode_lnurl, verify
from .models import LoginChallenge, Profile, RequestCount, Shop
from .nostr import file_event, public_key, shop_event, sign
from .notifications import NotificationFailed, telegram
from .views import profile_for


SEED = " ".join(["abandon"] * 11 + ["about"])


def public_names(*inside):
    """Every name of a fetch points to a public address, without asking the DNS; the names in `inside` lead inside. The
    address given is of TEST-NET: a test that reached the network by mistake would connect nowhere."""
    def addresses(host, seconds):
        if host in inside:
            raise ValueError(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("192.0.2.1", 443))]

    return patch("shops.lnaddress.public_addresses", side_effect=addresses)


def reset_limits():
    """The request limits from scratch: the counters live in the database (RequestCount), the cache keeps the rest."""
    cache.clear()
    RequestCount.objects.all().delete()


class _StoppedClock:
    """`time` as shops.views reads it, with the hands still: every other name is the real module's."""

    def __init__(self):
        self.now = time.time()

    def time(self):
        return self.now

    def __getattr__(self, name):
        return getattr(time, name)


@contextmanager
def steady_limits():
    """For a test that depends on the request limits staying in one window. over_limit counts in windows of fixed length
    (limit_key): a test that sends the requests of a limit and expects the refusal at the next one fails when the clock
    crosses the end of a window in between, because the counter starts again (a few seconds' worth of requests at the edge of
    every quarter of an hour is enough, and under load each request can take a second). A test that counts queries sees three
    more when a counter is made in a new window, and one more when over_limit tidies the old counters, which it does on one
    request in fifty, drawn at random. Here the clock stands still and nothing is tidied; the counters still count. A test
    that needs the clock to move patches `config.ratelimit.time.time` inside this."""
    with patch("config.ratelimit.secrets.randbelow", return_value=1), patch("config.ratelimit.time", _StoppedClock()):
        yield


def admin_session(client):
    """Same as after the admin password: force_login alone is a session like the link one (config/urls.py)."""
    client.force_login(get_user_model().objects.create_superuser("boss"))
    session = client.session
    session[ADMIN_SESSION] = True
    session.save()


def http_error(status):
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(response=response)


def stream(answer):
    """A fake HTTP response, read in pieces as `lnaddress.fetch` does."""
    response = MagicMock()
    response.__enter__.return_value = response
    response.iter_content.return_value = [json.dumps(answer).encode()]
    return response


class LnurlAuth(TestCase):
    def sign(self, k1):
        key = coincurve.PrivateKey()
        return key.sign(bytes.fromhex(k1), hasher=None).hex(), key.public_key.format().hex()

    def test_encoding(self):
        self.assertTrue(encode_lnurl("https://example.org/a").startswith("LNURL1"))

    def test_signature(self):
        k1 = "ab" * 32
        signature, key = self.sign(k1)
        self.assertTrue(verify(k1, signature, key))
        self.assertFalse(verify("cd" * 32, signature, key))
        self.assertFalse(verify(k1, "zz", key))

    def k1(self):
        self.client.get("/en/shops/")
        return LoginChallenge.objects.latest("created").k1

    def test_login(self):
        k1 = self.k1()
        signature, key = self.sign(k1)
        self.assertEqual(self.client.get("/lnurl-auth/", {"k1": k1, "sig": signature, "key": key}).json()["status"], "OK")
        self.assertTrue(self.client.get("/lnurl-auth/status/").json()["logged_in"])
        self.assertFalse(LoginChallenge.objects.exists())
        self.assertTrue(get_user_model().objects.filter(username=key).exists())

    def test_forged_login(self):
        k1 = self.k1()
        signature, key = self.sign("cd" * 32)
        self.assertEqual(self.client.get("/lnurl-auth/", {"k1": k1, "sig": signature, "key": key}).json()["status"], "ERROR")
        self.assertFalse(self.client.get("/lnurl-auth/status/").json()["logged_in"])

    def test_anonymous_visits_leave_no_session(self):
        self.client.get("/en/shops/")
        self.assertFalse(Session.objects.exists())

    def test_old_challenges_are_removed(self):
        old = LoginChallenge.new()
        LoginChallenge.objects.filter(pk=old.pk).update(created=timezone.now() - timezone.timedelta(hours=2))
        self.client.get("/en/shops/")
        self.assertFalse(LoginChallenge.objects.filter(pk=old.pk).exists())


@override_settings(NETWORK="signet")
class Destinations(TestCase):
    def valid(self, **fields):
        return ShopForm({"name": "Bar", **fields}).is_valid()

    def warning(self, **fields):
        return str(ShopForm({"name": "Bar", "lightning_address": "mario@example.org", **fields}).errors)

    def test_secrets_refused_in_every_field_with_a_warning(self):
        for field, value in (("description", f"my phrase: {SEED}"), ("description", "Xprv" + "a" * 60),
                             ("lightning_address", "xprv" + "a" * 60 + "@example.org"), ("lightning_address", SEED),
                             ("description", SEED.replace(" ", ", ")), ("address", "1. " + SEED.replace(" ", " 2. "))):
            self.assertIn("never share it", self.warning(**{field: value}), (field, value))

    def test_ordinary_text_passes(self):
        self.assertEqual(self.warning(description="A bar on the square, open in the evening with a wide choice of wines"), "")

    def test_lightning_address_as_wallets_copy_it(self):
        form = ShopForm({"name": "Bar", "lightning_address": " lightning:Mario@Example.org "})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["lightning_address"], "mario@example.org")

    def test_bolt12_addresses_say_what_to_do(self):
        for address in ("₿mario@phoenixwallet.me", "lno1qgsqvgnwgcg35z6ee2h3yczraddm72xrfua9uve2rlrm9deu7xyfzrc"):
            errors = str(ShopForm({"name": "Bar", "lightning_address": address}).errors)
            self.assertIn("BOLT12", errors)
            self.assertIn(community()["email"], errors)

    def test_website_without_scheme(self):
        form = ShopForm({"name": "Bar", "lightning_address": "mario@example.org", "website": "www.example.org"})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["website"], "https://www.example.org")

    def test_refuses_internal_lightning_hosts(self):
        for address in ("x@172.25.0.13:8999", "x@172.25.0.13", "x@web", "x@bitcoind:43782", "x@host.internal", "a@b@example.org",
                        "mario@example.org;server=http://attacker.example;", "mario@[::1]", "mario@example.org/x"):
            self.assertFalse(self.valid(lightning_address=address), address)

    def test_refuses_names_that_resolve_inside(self):
        with patch("shops.forms.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("10.0.0.5", 443))]):
            self.assertFalse(self.valid(lightning_address="x@intranet.example.org"))

    def test_field_limits(self):
        for fields in ({"discount": 150}, {"latitude": 999}, {"longitude": -999}, {"nostr_pubkey": "abc"},
                       {"telegram_chat": "no idea what this is"}):
            self.assertFalse(self.valid(lightning_address="mario@example.org", **fields), fields)

    def test_comma_in_coordinates(self):
        form = ShopForm({"name": "Bar", "lightning_address": "mario@example.org", "latitude": "41,88", "longitude": "12.5"})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["latitude"], 41.88)

    def test_needs_a_way_to_get_paid(self):
        self.assertFalse(self.valid())


class LightningAddressProbe(TestCase):
    METADATA = {"tag": "payRequest", "callback": "https://pay.example.org/cb", "minSendable": 2000}
    INVOICE = {"pr": "lnbc1", "verify": "https://pay.example.org/verify/a"}

    def setUp(self):
        # An address that just worked in the live check of the form is not probed again on submit
        cache.clear()

    def probe(self, *answers, address="mario@example.org"):
        replies = [stream(answer) for answer in answers]
        with public_names(), patch("shops.lnaddress.requests.Session.get", side_effect=replies) as get:
            return refusal(address), get

    def test_a_working_address_passes(self):
        result, get = self.probe(self.METADATA, self.INVOICE, {"status": "OK", "settled": False})
        self.assertIsNone(result)
        self.assertEqual(get.call_args_list[0].args[0], "https://example.org/.well-known/lnurlp/mario")
        self.assertEqual(get.call_args_list[1].args[0], "https://pay.example.org/cb?amount=2000")

    def test_no_redirects_are_followed(self):
        result, get = self.probe(self.METADATA, self.INVOICE, {"settled": False})
        self.assertTrue(all(call.kwargs["allow_redirects"] is False for call in get.call_args_list))

    def test_without_payment_confirmation_it_is_refused(self):
        self.assertIn("does not tell the register", self.probe(self.METADATA, {"pr": "lnbc1"})[0])
        self.assertIn("does not tell the register", self.probe(self.METADATA, self.INVOICE, {"status": "OK"})[0])

    def test_blink_needs_payment_confirmation_too(self):
        """Non-custodial Blink accounts (Spark) are followed by the register only through LUD-21."""
        metadata = {**self.METADATA, "callback": "https://pay.blink.sv/cb"}
        invoice = {**self.INVOICE, "verify": "https://pay.blink.sv/verify/a"}
        result, get = self.probe(metadata, invoice, {"settled": False}, address="mario@blink.sv")
        self.assertIsNone(result)
        self.assertEqual(get.call_count, 3)
        self.assertIn("does not tell the register", self.probe(metadata, {"pr": "lnbc1"}, address="mario@blink.sv")[0])

    def test_a_provider_that_answers_from_another_site_is_refused_like_at_the_register(self):
        """The register asks only the address's own domain and its subdomains: what it would refuse at the counter is
        refused here, where the merchant can still pick another wallet."""
        result, get = self.probe(self.METADATA, address="mario@shop.example")
        self.assertIn("does not answer", result)
        self.assertEqual(get.call_count, 1)
        elsewhere = {**self.INVOICE, "verify": "https://other.example/verify/a"}
        result, get = self.probe({**self.METADATA, "callback": "https://shop.example/cb"}, elsewhere, address="mario@shop.example")
        self.assertIn("does not tell the register", result)
        self.assertEqual(get.call_count, 2)

    def test_a_silent_domain_is_refused(self):
        with public_names(), patch("shops.lnaddress.requests.Session.get", side_effect=requests.ConnectionError), \
                patch("shops.lnaddress.missing", return_value=False):
            self.assertIn("does not answer", refusal("mario@phoenixwallet.me"))

    def test_not_an_lnurl_endpoint(self):
        self.assertIn("does not answer", self.probe({"tag": "other"})[0])

    def test_answers_cannot_send_us_inside(self):
        for callback in ("http://pay.example.org/cb", "https://web/cb", "https://pay.example.org:8000/cb", "https://host.internal/cb"):
            with public_names("web"):
                reply = stream({**self.METADATA, "callback": callback})
                with patch("shops.lnaddress.requests.Session.get", return_value=reply) as get:
                    self.assertIn("does not answer", refusal("mario@example.org"), callback)
                self.assertEqual(get.call_count, 1, callback)


class NewShop(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create(username="k")
        self.client.force_login(self.user)
        self.data = {"name": "Bar", "lightning_address": "mario@example.org"}
        probe = patch("shops.views.refusal", return_value=None)
        self.refusal = probe.start()
        self.addCleanup(probe.stop)

    def test_a_refused_address_creates_nothing(self):
        self.refusal.return_value = "does not tell the register"
        self.assertContains(self.post(), "does not tell the register")
        self.assertFalse(Shop.objects.exists())

    def post(self, **fields):
        return self.client.post("/en/shops/new/", {**self.data, **fields})

    def test_the_map_speaks_the_language_of_the_page(self):
        """The Leaflet buttons are called "Zoom in" and "Zoom out": on an Italian page they must be said in Italian."""
        page = self.client.get("/it/shops/new/")
        self.assertContains(page, 'aria-describedby="pick-status" data-label="Mappa" data-zoom-in="Ingrandisci" data-zoom-out="Rimpicciolisci"')

    def test_second_submit_creates_nothing(self):
        self.assertEqual(self.post().status_code, 302)
        self.assertEqual(self.post().status_code, 302)
        self.assertEqual(Shop.objects.count(), 1)
        self.assertEqual(Shop.objects.get().lightning_address, "mario@example.org")


class ManageShop(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create(username="k")
        self.client.force_login(self.user)
        self.shop = Shop.objects.create(owner=self.user, name="Bar", lightning_address="old@example.org")

    def test_edit_changes_details_but_never_the_address(self):
        response = self.client.post(f"/en/shops/edit/{self.shop.id}/", {"name": "New", "lightning_address": "x@example.org"})
        self.assertEqual(response.status_code, 302)
        self.shop.refresh_from_db()
        self.assertEqual((self.shop.name, self.shop.lightning_address), ("New", "old@example.org"))

    def test_edit_form_has_no_payment_fields(self):
        self.assertFalse({"destination", "lightning_address"} & set(ShopDetailsForm().fields))

    def test_edit_refuses_secrets(self):
        response = self.client.post(f"/en/shops/edit/{self.shop.id}/", {"name": "Bar", "description": SEED})
        self.assertContains(response, "never share it")

    def test_other_owner_is_refused(self):
        self.client.force_login(get_user_model().objects.create(username="other"))
        for path in ("edit", "remove", "preview"):
            self.assertEqual(self.client.get(f"/en/shops/{path}/{self.shop.id}/").status_code, 404)

    def test_panel_hands_over_the_register(self):
        response = self.client.get("/en/shops/")
        self.assertContains(response, f"{settings.PUBLIC_URL}/en/shops/register/{self.shop.id}/")
        self.assertNotContains(response, "Your shop is ready")
        self.assertContains(self.client.get("/en/shops/?saved=1"), "Your shop is ready")
        self.assertContains(self.client.get("/en/shops/"), "Printable sign")

    @override_settings(ALLOWED_HOSTS=["website.onion", "testserver"], PUBLIC_URL="https://website.example.org")
    def test_codes_do_not_follow_the_host_header(self):
        """Whoever opens the panel from the onion passes the site's address to the collaborators, and the wallet signs for the site."""
        register = f"https://website.example.org/en/shops/register/{self.shop.id}/"
        with patch("shops.views.segno.make", wraps=segno.make) as make:
            response = self.client.get("/en/shops/", headers={"Host": "website.onion"})
        self.assertContains(response, register)
        self.assertEqual(make.call_args.args[0], register)
        with patch("shops.views.encode_lnurl", wraps=encode_lnurl) as encode:
            self.client_class().get("/en/shops/", headers={"Host": "website.onion"})
        self.assertTrue(encode.call_args.args[0].startswith("https://website.example.org/lnurl-auth/?"))

    @override_settings(ALLOWED_HOSTS=["website.onion", "testserver"], PUBLIC_URL="https://website.example.org")
    def test_the_sign_does_not_follow_the_host_header(self):
        with patch("shops.views.segno.make", wraps=segno.make) as make:
            self.client.get(f"/en/shops/sign/{self.shop.id}/", headers={"Host": "website.onion"})
        self.assertEqual(make.call_args.args[0], f"https://website.example.org/en/shops/s/{self.shop.id}/")

    def test_the_sign_fits_the_phone(self):
        """Without a viewport the phone draws it 980 px wide: back and Print become tiny."""
        self.assertContains(self.client.get(f"/it/shops/sign/{self.shop.id}/"), 'name="viewport" content="width=device-width')

    def test_the_way_back_from_the_register_is_for_the_owner(self):
        url = f"/en/shops/register/{self.shop.id}/"
        # In the register's menu
        self.assertContains(self.client.get(url), 'href="/en/shops/">Your shops</a>')
        self.assertNotContains(self.client_class().get(url), 'href="/en/shops/">Your shops</a>')

    def test_signing_out_keeps_the_register_open(self):
        url = f"/en/shops/register/{self.shop.id}/"
        self.assertRedirects(self.client.post("/en/shops/sign-out/", {"next": url}), url)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_signing_out_goes_nowhere_else(self):
        self.assertRedirects(self.client.post("/en/shops/sign-out/", {"next": "https://example.org/"}), "/en/shops/")

    def test_panel_explains_why_the_shop_is_not_on_the_map(self):
        self.assertContains(self.client.get("/en/shops/"), "the position is missing")
        Shop.objects.filter(pk=self.shop.pk).update(suspended=True)
        self.assertContains(self.client.get("/en/shops/"), "Register and payments keep working")


class EmailLogin(TestCase):
    def setUp(self):
        reset_limits()

    def link(self):
        return mail.outbox[-1].body.split(settings.PUBLIC_URL)[1].split()[0]

    def request_link(self, address):
        self.client.post("/en/shops/recovery/", {"email": address})
        return self.link()

    def test_the_page_of_the_link_says_which_account(self):
        """Against the link forwarded by someone else: whoever opens it sees which account they are entering"""
        self.assertContains(self.client.get(self.request_link("mario@example.org")), "You are signing in as mario@example.org.")
        self.assertContains(self.client.get(self.request_link("mario@example.org").replace("/en/", "/it/")), "Stai entrando come mario@example.org.")

    def test_unicode_spellings_of_a_domain_are_the_same_address(self):
        """"ｇｍａｉｌ.com" in full width arrives at gmail.com: for the limits it is the same mailbox"""
        self.enterContext(steady_limits())
        for address in ("mario@gmail.com", "mario@ｇｍａｉｌ.com", "mario@gmａil.com", "mario@GMAIL.com"):
            self.client.post("/en/shops/recovery/", {"email": address})
        self.assertEqual([m.to for m in mail.outbox], [["mario@gmail.com"]] * 3)

    def test_a_domain_that_is_not_one_is_refused(self):
        self.assertContains(self.client.post("/en/shops/recovery/", {"email": "mario@" + "a" * 64 + ".com"}), "valid email")
        self.assertEqual(mail.outbox, [])

    def follow(self, address):
        return self.client.post(self.request_link(address), follow=True)

    def test_a_mail_server_failure_is_told(self):
        with patch("shops.views.send_mail", side_effect=OSError):
            response = self.client.post("/en/shops/recovery/", {"email": "a@example.org"})
        self.assertContains(response, "could not be sent")
        self.assertNotContains(response, "Email sent")

    def test_the_page_says_where_the_link_went(self):
        """Whoever mistyped the address notices. With or without an account the page is the same."""
        Profile.objects.create(user=get_user_model().objects.create(username="k"), email="mario@example.org")
        pages = []
        for address in ("Mario@example.org", "luisa@example.org"):
            response = self.client_class().post("/it/shops/recovery/", {"email": address})
            self.assertContains(response, f"Email inviata a {address.lower()}. Apri la posta")
            pages.append(response.content.decode().replace(address.lower(), "chi@example.org"))
        self.assertEqual(pages[0], pages[1])

    def test_the_browser_can_fill_the_email_and_finds_the_mistake(self):
        """WCAG 1.3.5 and 3.3.1: the email is the user's, and after a wrong submission the focus is already on the field to correct."""
        field = re.compile(r'<input type="email" name="email"[^>]*>')
        self.assertIn('autocomplete="email"', field.search(self.client.get("/en/shops/recovery/").content.decode())[0])
        wrong = field.search(self.client.post("/en/shops/recovery/", {"email": "a@b"}).content.decode())[0]
        self.assertIn("autofocus", wrong)
        self.assertIn('aria-invalid="true"', wrong)
        self.assertIn('aria-describedby="id_email_error"', wrong)
        self.assertNotIn("autofocus", self.client.get("/en/shops/recovery/").content.decode())

    def test_the_account_email_too(self):
        self.client.force_login(get_user_model().objects.create(username="k"))
        field = re.compile(r'<input type="email" name="email"[^>]*>')
        self.assertIn('autocomplete="email"', field.search(self.client.get("/en/shops/").content.decode())[0])
        page = self.client.post("/en/shops/", {"email": "a@b"}).content.decode()
        self.assertIn("autofocus", field.search(page)[0])
        self.assertIn('<details class="account" open>', page)

    def test_the_email_says_what_it_is(self):
        self.request_link("a@example.org")
        self.assertIn("sign in", mail.outbox[-1].subject)
        self.assertIn("ignore this email", mail.outbox[-1].body)

    def test_existing_user_recovers_access(self):
        user = get_user_model().objects.create(username="k")
        Profile.objects.create(user=user, email="a@example.org")
        self.follow("a@example.org")
        self.assertEqual(int(self.client.session["_auth_user_id"]), user.id)

    def test_staff_does_not_sign_in_by_email(self):
        """Staff enter only from the admin with the password: whoever reads their recovery email does not get in."""
        for flags in ({"is_staff": True}, {"is_superuser": True}):
            reset_limits()
            user = get_user_model().objects.create(username=f"admin-{len(flags)}-{list(flags)[0]}", **flags)
            Profile.objects.create(user=user, email=f"{list(flags)[0]}@example.org")
            response = self.client.post(self.request_link(f"{list(flags)[0]}@example.org"))
            self.assertContains(response, "signs in from the administration", status_code=403)
            self.assertNotIn("_auth_user_id", self.client.session)

    def test_a_session_from_the_link_does_not_open_the_admin_after_becoming_staff(self):
        """Becoming staff does not change the password, and the session opened with the link would stay good for the admin too."""
        self.follow("a@example.org")
        get_user_model().objects.filter(profile__email="a@example.org").update(is_staff=True)
        self.assertRedirects(self.client.get("/admin/"), "/admin/login/?next=/admin/", fetch_redirect_response=False)
        self.assertEqual(self.client.get("/it/shops/").status_code, 200)  # the shop panel stays open

    def test_the_admin_opens_with_the_password(self):
        get_user_model().objects.create_user("boss", password="a long password", is_staff=True)
        self.assertEqual(self.client.post("/admin/login/", {"username": "boss", "password": "wrong"}).status_code, 200)
        self.assertEqual(self.client.get("/admin/").status_code, 302)
        self.client.post("/admin/login/", {"username": "boss", "password": "a long password"})
        self.assertEqual(self.client.get("/admin/").status_code, 200)

    def test_new_user_is_created(self):
        self.assertEqual(self.follow("b@example.org").status_code, 200)
        self.assertTrue(Profile.objects.filter(email="b@example.org").exists())

    def test_forged_token(self):
        self.assertEqual(self.client.get("/en/shops/recovery/sign-in/?token=abc").status_code, 404)

    def test_opening_the_link_does_not_log_in(self):
        self.assertEqual(self.client.get(self.request_link("c@example.org")).status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_link_works_once(self):
        link = self.request_link("d@example.org")
        self.client.post(link)
        self.assertEqual(self.client_class().post(link).status_code, 404)

    def test_the_button_goes_off_at_the_first_click(self):
        """The link is valid once: a second click would find it already used. A script turns the button off at the first submit;
        without script the form stays as always, with the button on."""
        page = self.client.get(self.request_link("i@example.org")).content.decode()
        form = re.search(r"<form [^>]*>.*?</form>", page, re.S)[0]
        self.assertIn("data-once", form)
        self.assertNotIn("disabled", form)
        # What the script does is tried in node (shops/test_scripts.py)
        self.assertRegex(page, r'<script src="/static/link_confirm(\.\w+)?\.js"')

    def test_old_address_after_a_change_gives_a_new_account(self):
        self.follow("e@example.org")
        Profile.objects.filter(email="e@example.org").update(email="f@example.org")
        self.client = self.client_class()
        self.assertEqual(self.follow("e@example.org").status_code, 200)
        self.assertEqual(get_user_model().objects.count(), 2)

    def test_a_double_click_makes_one_account(self):
        link = self.request_link("h@example.org")
        self.client_class().post(link)
        lookups = []

        def not_yet(email):
            # The second click checks the link and looks for the account before the first has saved it
            lookups.append(email)
            return None if len(lookups) <= 2 else profile_for(email)

        with patch("shops.views.profile_for", side_effect=not_yet):
            response = self.client.post(link)
        self.assertRedirects(response, "/en/shops/", fetch_redirect_response=False)
        profile = Profile.objects.get(email="h@example.org")
        self.assertEqual(int(self.client.session["_auth_user_id"]), profile.user_id)
        self.assertEqual(get_user_model().objects.count(), 1)

    def test_long_addresses_do_not_collide(self):
        for address in ("a" * 60 + "." + "b" * 60 + "@" + "c" * 60 + ".org", "a" * 60 + "." + "b" * 60 + "@" + "c" * 60 + ".net"):
            self.client = self.client_class()
            self.assertEqual(self.follow(address).status_code, 200)

    def test_requests_for_one_address_are_limited(self):
        self.enterContext(steady_limits())
        for _ in range(6):
            self.client.post("/en/shops/recovery/", {"email": "g@example.org"})
        self.assertEqual(len(mail.outbox), 3)

    def test_over_the_limit_of_an_address_the_page_does_not_pretend_to_send(self):
        """Anyone can use up the limit of someone else's email: its owner then reads that a link was sent a short time
        ago, not «Email sent» for a mail that never comes. The same with or without an account."""
        self.enterContext(steady_limits())
        Profile.objects.create(user=get_user_model().objects.create(username="k"), email="g@example.org")
        for n in range(3):
            self.client_class().post("/en/shops/recovery/", {"email": "g@example.org"}, HTTP_X_FORWARDED_FOR=f"192.0.2.{n}")
        pages = []
        for address in ("g@example.org", "h@example.org"):
            for n in range(3 if address.startswith("h") else 0):
                self.client_class().post("/en/shops/recovery/", {"email": address}, HTTP_X_FORWARDED_FOR=f"192.0.2.{n}")
            response = self.client.post("/en/shops/recovery/", {"email": address}, HTTP_X_FORWARDED_FOR="198.51.100.7")
            self.assertContains(response, f"A link was already sent to {address} a few minutes ago", status_code=429)
            self.assertNotContains(response, "Email sent", status_code=429)
            self.assertIn("Retry-After", response)
            page = re.sub(r'name="csrfmiddlewaretoken" value="[^"]*"', "", response.content.decode())
            pages.append(page.replace(address, "who@example.org"))
        self.assertEqual(pages[0], pages[1])
        self.assertEqual(len(mail.outbox), 6)

    def test_over_the_limit_of_a_connection_the_page_says_so(self):
        self.enterContext(steady_limits())
        for n in range(10):
            self.client.post("/en/shops/recovery/", {"email": f"n{n}@example.org"})
        response = self.client.post("/en/shops/recovery/", {"email": "last@example.org"})
        self.assertContains(response, "Too many requests from your connection", status_code=429)
        self.assertNotContains(response, "Email sent", status_code=429)
        self.assertEqual(len(mail.outbox), 10)


class RecoveryEmail(TestCase):
    def setUp(self):
        reset_limits()
        self.user = get_user_model().objects.create(username="k")
        Profile.objects.create(user=self.user, email="old@example.org")
        self.client.force_login(self.user)

    def change(self, address):
        self.client.post("/en/shops/", {"email": address})
        return mail.outbox[-1].body.split(settings.PUBLIC_URL)[1].split()[0]

    def email(self):
        return Profile.objects.get(user=self.user).email

    def test_new_address_needs_the_link(self):
        link = self.change("new@example.org")
        self.assertEqual(self.email(), "old@example.org")
        self.assertIn("signed in to the account that asked for it", mail.outbox[-1].body)
        self.assertContains(self.client.get(link), "This email will get you back into your account: new@example.org.")
        self.client.post(link)
        self.assertEqual(self.email(), "new@example.org")

    def test_only_the_account_that_asked_confirms(self):
        """Another account asks to confirm someone else's email: when that person opens the link, signed in to nothing
        or to their own account, nothing is confirmed, and the page says to open it where the asking account is signed in.
        Otherwise the asking account would get the email and every later recovery of that person."""
        link = self.change("victim@example.org")
        victim = get_user_model().objects.create(username="victim")
        signed_out, signed_in = self.client_class(), self.client_class()
        signed_in.force_login(victim)
        for client in (signed_out, signed_in):
            for method in (client.get, client.post):
                response = method(link)
                self.assertContains(response, "only that account can confirm the email", status_code=403)
                self.assertNotContains(response, "<form method=\"post\"", status_code=403)
        self.assertEqual(self.email(), "old@example.org")
        # The link is still good for the account that asked, in its own browser
        self.assertRedirects(self.client.post(link), "/en/shops/", fetch_redirect_response=False)
        self.assertEqual(self.email(), "victim@example.org")

    def test_link_works_once(self):
        link = self.change("new@example.org")
        self.client.post(link)
        self.assertEqual(self.client.post(link).status_code, 404)

    def test_address_of_another_account_is_refused(self):
        Profile.objects.create(user=get_user_model().objects.create(username="other"), email="taken@example.org")
        response = self.client.post(self.change("TAKEN@example.org"))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.email(), "old@example.org")

    def test_two_confirmations_at_once_are_not_an_error(self):
        link = self.change("new@example.org")
        Profile.objects.create(user=get_user_model().objects.create(username="other"), email="new@example.org")
        # The other account confirmed the same email while this one was running the check
        with patch("shops.views.profile_for", return_value=None):
            response = self.client.post(link)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.email(), "old@example.org")

    def test_over_the_limit_the_panel_does_not_say_sent(self):
        self.enterContext(steady_limits())
        for n in range(3):
            self.change(f"new{n}@example.org")
        response = self.client.post("/en/shops/", {"email": "last@example.org"}, follow=True)
        self.assertContains(response, "Too many emails in a short time")
        self.assertNotContains(response, "We sent a confirmation link to last@example.org")
        self.assertEqual(len(mail.outbox), 3)

    def test_many_free_accounts_cannot_mail_any_address_without_limit(self):
        """Accounts are free: the address and its /48 count too, with the recovery form's counters, and so does the
        address written, whichever account writes it."""
        self.enterContext(steady_limits())

        def ask(email, ip):
            client = self.client_class()
            client.force_login(get_user_model().objects.create(username=f"u{get_user_model().objects.count()}"))
            return client.post("/en/shops/", {"email": email}, REMOTE_ADDR=ip, follow=True)

        # One address, from many accounts and many networks: 3, as the recovery
        for n in range(3):
            self.assertContains(ask("target@example.org", f"198.51.100.{n}"), "We sent a confirmation link")
        self.assertContains(ask("target@example.org", "198.51.100.9"), "Too many emails in a short time")
        reset_limits()
        # From one address, to many: 10
        for n in range(10):
            self.assertContains(ask(f"a{n}@example.org", "203.0.113.5"), "We sent a confirmation link")
        self.assertContains(ask("b@example.org", "203.0.113.5"), "Too many emails in a short time")
        reset_limits()
        # From one /48, spread over its /64 networks: 30
        for n in range(30):
            self.assertContains(ask(f"c{n}@example.org", f"2001:db8:1:{n:x}::1"), "We sent a confirmation link")
        self.assertContains(ask("d@example.org", "2001:db8:1:ff::1"), "Too many emails in a short time")
        # The recovery form shares the counters of that network
        self.assertEqual(self.client.post("/en/shops/recovery/", {"email": "e@example.org"},
                                          REMOTE_ADDR="2001:db8:1:fe::1").status_code, 429)

    def test_clearing_needs_no_link(self):
        self.client.post("/en/shops/", {"email": ""})
        self.assertEqual(self.email(), "")


class Nostr(TestCase):
    SECRET = "01" * 32

    def test_signature_verifies(self):
        event = sign(self.SECRET, 1, "ciao", [])
        key = coincurve.PublicKeyXOnly(bytes.fromhex(event["pubkey"]))
        self.assertTrue(key.verify(bytes.fromhex(event["sig"]), bytes.fromhex(event["id"])))

    def test_two_shops_never_share_a_name_even_outside_the_form(self):
        Shop.objects.create(name="Bar", nip05_name="bar")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Shop.objects.create(name="Pub", nip05_name="BAR")
        Shop.objects.create(name="Pub")
        Shop.objects.create(name="Tavern")

    def test_the_admin_form_refuses_a_name_already_taken(self):
        """The admin uses a ModelForm: the model's constraint reaches it as a form error, not as a 500 error."""
        Shop.objects.create(name="Bar", nip05_name="bar")
        form = modelform_factory(Shop, fields=["name", "nip05_name"])({"name": "Pub", "nip05_name": "Bar"})
        self.assertFalse(form.is_valid())
        self.assertIn("This name is already taken.", str(form.errors))

    @override_settings(NOSTR_SECRET_KEY=SECRET)
    def test_nip05(self):
        Shop.objects.create(name="Bar", latitude=1, longitude=1, nip05_name="bar", nostr_pubkey="ab" * 32)
        names = self.client.get("/.well-known/nostr.json").json()["names"]
        self.assertEqual(names["bar"], "ab" * 32)
        self.assertEqual(names["_"], public_key(self.SECRET))

    def test_file_event_names_the_hash(self):
        event = file_event(self.SECRET, "ab" * 32, 12, "application/zip", "A copy")
        self.assertEqual(event["kind"], 1063)
        self.assertEqual(dict(event["tags"]), {"x": "ab" * 32, "m": "application/zip", "size": "12", "alt": "A copy"})
        self.assertEqual(dict(file_event(self.SECRET, "ab" * 32, 12, "a/b", "c", "https://x.test/f")["tags"])["url"], "https://x.test/f")

    @override_settings(NOSTR_SECRET_KEY=SECRET, NOSTR_RELAYS=["ws://relay"])
    def test_command_signs_a_file_and_prints_only_the_event(self):
        out = StringIO()
        with patch("shops.management.commands.publish_nostr.connect") as connect:
            connect.return_value.__enter__.return_value.recv.return_value = json.dumps(["OK", "x", True, ""])
            call_command("publish_nostr", sha256="ab" * 32, size=3, mime="text/plain", summary="s", stdout=out, stderr=StringIO())
        event = json.loads(out.getvalue())
        self.assertEqual(event["pubkey"], public_key(self.SECRET))
        self.assertEqual(json.loads(connect.return_value.__enter__.return_value.send.call_args[0][0])[1]["id"], event["id"])
        self.assertNotIn(self.SECRET, out.getvalue())

    @override_settings(NOSTR_SECRET_KEY=SECRET, NOSTR_RELAYS=["ws://relay"])
    def test_sign_only_touches_no_relay(self):
        with patch("shops.management.commands.publish_nostr.connect") as connect:
            call_command("publish_nostr", sha256="ab" * 32, sign_only=True, stdout=StringIO())
        connect.assert_not_called()

    def test_the_position_is_a_geohash(self):
        # NIP-52 and NIP-99: `g` is a geohash, which clients decode; "lat,lon" is not
        for latitude, longitude, expected in ((57.64911, 10.40744, "u4pruydqq"), (-25.382708, -49.265506, "6gkzwgjzn")):
            shop = Shop(name="Bar", latitude=latitude, longitude=longitude)
            tags = {tag[0]: tag[1] for tag in shop_event(self.SECRET, shop, "https://x.test/s/")["tags"]}
            self.assertEqual(tags["g"], expected)

    @override_settings(NOSTR_SECRET_KEY=SECRET, NOSTR_RELAYS=["ws://giu", "ws://su"])
    def test_a_relay_down_does_not_stop_the_others(self):
        socket = MagicMock()
        socket.recv.return_value = json.dumps(["OK", "x", True, ""])
        working = MagicMock()
        working.__enter__.return_value = socket
        errors = StringIO()
        with patch("shops.management.commands.publish_nostr.connect", side_effect=[OSError("refused"), working]):
            with self.assertRaises(CommandError):
                call_command("publish_nostr", sha256="ab" * 32, size=3, summary="s", stdout=StringIO(), stderr=errors)
        self.assertTrue(socket.send.called)
        self.assertIn("ws://giu: OSError", errors.getvalue())
        self.assertIn("ws://su: 1/1", errors.getvalue())

    @override_settings(NOSTR_SECRET_KEY=SECRET, NOSTR_RELAYS=["ws://strange", "ws://su"])
    def test_a_relay_with_a_strange_answer_does_not_stop_the_others(self):
        # A relay answers whatever it likes: an object instead of the NIP-01 list, or a "true" written as text
        for answer in ({"error": "rate limited"}, ["OK", "x", "true"], 7):
            strange, socket = MagicMock(), MagicMock()
            strange.__enter__.return_value.recv.return_value = json.dumps(answer)
            socket.recv.return_value = json.dumps(["OK", "x", True, ""])
            working = MagicMock()
            working.__enter__.return_value = socket
            errors = StringIO()
            with patch("shops.management.commands.publish_nostr.connect", side_effect=[strange, working]):
                with self.assertRaises(CommandError):
                    call_command("publish_nostr", sha256="ab" * 32, size=3, summary="s", stdout=StringIO(), stderr=errors)
            self.assertTrue(socket.send.called, answer)
            self.assertIn("ws://strange: ", errors.getvalue())
            self.assertIn("ws://su: 1/1", errors.getvalue())

    @override_settings(NOSTR_SECRET_KEY=SECRET)
    def test_a_bad_hash_is_refused(self):
        with self.assertRaises(CommandError):
            call_command("publish_nostr", sha256="xyz", sign_only=True, stdout=StringIO())


class Directory(TestCase):
    def test_suspended_shops_leave_the_map(self):
        owner = get_user_model().objects.create(username="k")
        Shop.objects.create(owner=owner, name="Open", lightning_address="a@b.example", latitude=1, longitude=1)
        Shop.objects.create(owner=owner, name="Suspended shop", lightning_address="a@b.example", latitude=1, longitude=1,
                            suspended=True)
        Shop.objects.create(owner=owner, name="No position", lightning_address="a@b.example")
        Shop.objects.create(owner=owner, name="Unlisted", lightning_address="a@b.example", latitude=1, longitude=1,
                            listed=False)
        self.assertEqual([s["name"] for s in self.client.get("/shops.json").json()["shops"]], ["Open"])

    def test_map_lists_the_shops_without_javascript(self):
        owner = get_user_model().objects.create(username="k")
        Shop.objects.create(owner=owner, name="Open", city="Example", latitude=1, longitude=1, discount=5)
        Shop.objects.create(owner=owner, name="Suspended shop", latitude=1, longitude=1, suspended=True)
        response = self.client.get("/it/shops/map/")
        self.assertContains(response, "<strong>Open</strong>")
        self.assertContains(response, "-5%")
        self.assertNotContains(response, "Suspended shop")

    def test_sign_always_finds_the_shop_page(self):
        owner = get_user_model().objects.create(username="k")
        hidden = Shop.objects.create(owner=owner, name="Unlisted", latitude=1, longitude=1, listed=False)
        suspended = Shop.objects.create(owner=owner, name="Suspended shop", latitude=1, longitude=1, suspended=True)
        shown = Shop.objects.create(owner=owner, name="Open", latitude=1, longitude=1, discount=5)
        self.assertEqual(self.client.get(f"/it/shops/s/{hidden.id}/").status_code, 200)
        self.assertEqual(self.client.get(f"/it/shops/s/{suspended.id}/").status_code, 200)
        self.assertContains(self.client.get(f"/it/shops/s/{shown.id}/"), "se paghi in bitcoin")
        self.assertEqual([s["name"] for s in self.client.get("/shops.json").json()["shops"]], ["Open"])

    def test_shop_without_position_has_no_map(self):
        shop = Shop.objects.create(name="No position")
        response = self.client.get(f"/it/shops/s/{shop.id}/")
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'id="map"')
        self.assertNotContains(response, "openstreetmap.org")

    def test_imported_shop_has_no_register(self):
        shop = Shop.objects.create(name="Imported", latitude=1, longitude=1)
        self.assertEqual(self.client.get(f"/en/shops/register/{shop.id}/").status_code, 404)


class Featured(TestCase):
    def setUp(self):
        Shop.objects.create(name="Here", city="Example", discount=5, latitude=1, longitude=1)
        Shop.objects.create(name="Elsewhere", city="Otherville", discount=9, latitude=1, longitude=1)

    def test_every_city_by_default(self):
        self.assertEqual([shop.name for shop in featured_shops()], ["Elsewhere", "Here"])

    def test_one_city(self):
        self.assertEqual([shop.name for shop in featured_shops(city="example")], ["Here"])

    def test_a_city_written_in_two_ways_counts_once(self):
        Shop.objects.create(name="There", city="example", latitude=1, longitude=1)
        self.assertEqual(statistics()["cities"], 2)

    def test_city_comes_from_the_community(self):
        data = {**community(), "map": {**community()["map"], "featured_city": "Example"}}
        with patch("website.views.community", return_value=data), patch("website.views.featured_shops", return_value=[]) as featured:
            self.client.get("/it/")
        featured.assert_called_once_with(city="Example")


class ImportedShop(TestCase):
    def test_the_owner_is_told_who_to_write_to(self):
        imported = Shop.objects.create(name="Old", external_id="1")
        owned = Shop.objects.create(name="New", owner=get_user_model().objects.create(username="o"))
        self.assertContains(self.client.get(f"/en/shops/s/{imported.id}/"), "Is this your shop?")
        self.assertNotContains(self.client.get(f"/en/shops/s/{owned.id}/"), "Is this your shop?")

    def test_the_json_has_the_stable_key_and_nothing_private(self):
        Shop.objects.create(name="Old", external_id="17", latitude=1, longitude=1, email="a@b.c", phone="123")
        Shop.objects.create(name="New", latitude=1, longitude=1, owner=get_user_model().objects.create(username="o"))
        shops = {shop["name"]: shop for shop in self.client.get("/shops.json").json()["shops"]}
        self.assertEqual((shops["Old"]["external_id"], shops["New"]["external_id"]), ("17", ""))
        self.assertFalse({"email", "phone", "owner"} & set(shops["Old"]))

    def test_the_stable_address_leads_to_the_shop_on_every_installation(self):
        imported = Shop.objects.create(name="Old", external_id="17")
        response = self.client.get("/en/shops/e/17/")
        self.assertRedirects(response, f"/en/shops/s/{imported.id}/", fetch_redirect_response=False)
        self.assertEqual(self.client.get("/en/shops/e/18/").status_code, 404)
        Shop.objects.create(name="No number")
        self.assertEqual(self.client.get("/en/shops/e//").status_code, 404)
        # PostgreSQL refuses a NUL in a text: it is no shop's number, not an error of the site
        self.assertEqual(self.client.get("/en/shops/e/%00/").status_code, 404)

    def test_the_stable_address_does_not_reveal_hidden_shops(self):
        """The numbers of the list are in a row: trying them one by one one cannot find the shops removed from maps and lists,
        nor their id, which opens the till."""
        Shop.objects.create(name="Suspended shop", external_id="20", suspended=True)
        Shop.objects.create(name="Not listed", external_id="21", listed=False)
        for number in (20, 21):
            self.assertEqual(self.client.get(f"/en/shops/e/{number}/").status_code, 404, number)
        historic = Shop.objects.create(name="Historic", external_id="22", closed_on=date(2020, 1, 1))
        self.assertRedirects(self.client.get("/en/shops/e/22/"), f"/en/shops/s/{historic.id}/", fetch_redirect_response=False)


class Map(TestCase):
    def test_coordinates_are_not_localized(self):
        self.assertContains(self.client.get("/it/shops/map/"), 'data-lat="')
        self.assertNotRegex(self.client.get("/it/shops/map/").content.decode(), r'data-lat="[0-9]+,')

    def test_leaflet_and_the_pins_speak_the_language_of_the_page(self):
        page = self.client.get("/it/shops/map/")
        for attribute in ('data-label="Mappa dei luoghi"', 'data-zoom-in="Ingrandisci"', 'data-zoom-out="Rimpicciolisci"',
                          'data-close="Chiudi"', 'data-discount="{n}% di sconto"', 'data-group="{n} luoghi: ingrandisci"'):
            self.assertContains(page, attribute)
        self.assertContains(self.client.get("/en/shops/map/"), 'data-discount="{n}% discount"')

    def test_the_map_can_be_skipped(self):
        html = self.client.get("/it/shops/map/").content.decode()
        self.assertLess(html.index('<a class="skip" href="#shop-list">Salta la mappa: vai all'), html.index('id="map"'))
        self.assertIn('<ol id="shop-list"', html)


class Historic(TestCase):
    """A former shop stays on the map as a place in history, but is no longer a place to pay."""

    def setUp(self):
        self.open = Shop.objects.create(name="Open", city="Example", latitude=1, longitude=1, discount=5, lightning=True)
        self.closing = Shop.objects.create(name="Closing", city="Otherville", latitude=1, longitude=1, discount=9, lightning=True,
                                           closed_on=date(2026, 11, 30))
        self.closed = Shop.objects.create(name="Closed", city="Otherville", latitude=1, longitude=1, closed_on=date(2026, 10, 1))
        today = patch("shops.models.timezone.localdate", return_value=date(2026, 11, 2))
        today.start()
        self.addCleanup(today.stop)

    def test_a_shop_is_not_historic_by_default(self):
        self.assertIsNone(Shop.objects.create(name="New").closed_on)
        self.assertEqual(self.open.historic_note, "")

    def test_it_closes_until_the_day_then_it_is_closed(self):
        with override("it"):
            self.assertEqual(self.closing.historic_note, "Storico · chiude il 30 novembre 2026")
            self.assertEqual(self.closed.historic_note, "Storico · chiuso il 1 ottobre 2026")
            self.closing.closed_on = date(2026, 11, 2)
            self.assertEqual(self.closing.historic_note, "Storico · chiude il 2 novembre 2026")
        with override("en"):
            self.assertEqual(self.closed.historic_note, "Historic · closed on 1 October 2026")

    def test_the_json_has_the_date_and_no_discount(self):
        shops = {shop["name"]: shop for shop in self.client.get("/shops.json").json()["shops"]}
        self.assertEqual(set(shops), {"Open", "Closing", "Closed"})
        self.assertEqual((shops["Closing"]["closed_on"], shops["Closing"]["discount"]), ("2026-11-30", 0))
        self.assertEqual((shops["Open"]["closed_on"], shops["Open"]["discount"]), (None, 5))

    def test_suspended_still_hides_it(self):
        Shop.objects.filter(pk=self.closed.pk).update(suspended=True)
        self.assertNotIn("Closed", [shop["name"] for shop in self.client.get("/shops.json").json()["shops"]])

    def test_statistics_count_only_the_places_to_pay(self):
        self.assertEqual(statistics(), {"shops": 1, "discounts": 1, "cities": 1})

    def test_it_is_never_featured(self):
        self.assertEqual([shop.name for shop in featured_shops()], ["Open"])
        self.assertEqual(list(featured_shops(city="Otherville")), [])

    def test_the_shop_page_says_it_and_does_not_invite_to_pay(self):
        page = self.client.get(f"/it/shops/s/{self.closing.id}/")
        self.assertContains(page, '<p class="notice shop-historic" role="note">Storico · chiude il 30 novembre 2026</p>')
        self.assertContains(page, 'data-historic="Storico · chiude il 30 novembre 2026"')
        self.assertNotContains(page, "se paghi in bitcoin")
        self.assertNotContains(page, "-9%")
        self.assertNotContains(page, "Accetta pagamenti Lightning")
        self.assertNotContains(page, "/register/")
        self.assertContains(self.client.get(f"/en/shops/s/{self.closed.id}/"), "Historic · closed on 1 October 2026")
        self.assertNotContains(self.client.get(f"/it/shops/s/{self.open.id}/"), "shop-historic")

    def test_the_map_counts_only_the_places_to_pay(self):
        page = self.client.get("/it/shops/map/")
        self.assertContains(page, '<span id="shop-count">1</span>')
        self.assertContains(page, "Storico · chiuso il 1 ottobre 2026")
        self.assertContains(page, "-5%")
        self.assertNotContains(page, "-9%")
        self.assertContains(page, 'data-closes="Storico · chiude il {date}"')
        self.assertContains(page, 'data-closed="Storico · chiuso il {date}"')
        html = page.content.decode()
        self.assertLess(html.index("<strong>Open</strong>"), html.index("<strong>Closing</strong>"))

    def test_on_nostr_it_has_the_date_and_no_discount(self):
        tags = dict(shop_event("01" * 32, self.closing, "https://x.test/s")["tags"])
        self.assertEqual(tags["closed_on"], "2026-11-30")
        self.assertNotIn("discount", tags)
        self.assertEqual(dict(shop_event("01" * 32, self.open, "https://x.test/s")["tags"])["discount"], "5")

    def test_the_owner_keeps_register_and_sign(self):
        owner = get_user_model().objects.create(username="o")
        Shop.objects.filter(pk=self.closing.pk).update(owner=owner, lightning_address="a@b.example")
        self.client.force_login(owner)
        panel = self.client.get("/it/shops/")
        self.assertContains(panel, "Storico · chiude il 30 novembre 2026. Sulla mappa con un segnaposto suo")
        self.assertContains(panel, f"/it/shops/register/{self.closing.id}/")
        self.assertContains(self.client.get(f"/it/shops/sign/{self.closing.id}/"), "Closing")

    def test_only_the_admin_sets_it(self):
        self.assertNotIn("closed_on", ShopForm.base_fields)
        self.assertNotIn("closed_on", ShopDetailsForm.base_fields)
        admin_session(self.client)
        listing = self.client.get("/admin/shops/shop/")
        self.assertContains(listing, 'name="form-0-closed_on"')
        self.assertContains(listing, "closed_on__isnull")
        self.assertContains(self.client.get(f"/admin/shops/shop/{self.closing.id}/change/"), 'name="closed_on"')


class ImportedHistoric(TestCase):
    def load(self, rows):
        with tempfile.NamedTemporaryFile("w", suffix=".json") as file:
            json.dump(rows, file)
            file.flush()
            call_command("import_shops", file.name, stdout=StringIO())

    def test_closed_on_makes_it_historic(self):
        self.load([{"external_id": "1", "name": "Bar", "closed_on": "2026-11-30"}, {"external_id": "2", "name": "Pub"}])
        self.assertEqual(Shop.objects.get(external_id="1").closed_on, date(2026, 11, 30))
        self.assertIsNone(Shop.objects.get(external_id="2").closed_on)

    def test_running_it_again_applies_the_date(self):
        self.load([{"external_id": "1", "name": "Bar"}])
        self.load([{"external_id": "1", "name": "Bar", "closed_on": "2026-11-30"}])
        self.assertEqual(Shop.objects.get(external_id="1").closed_on, date(2026, 11, 30))

    def test_a_claimed_shop_takes_only_the_date(self):
        self.load([{"external_id": "1", "name": "Bar"}])
        Shop.objects.filter(external_id="1").update(owner=get_user_model().objects.create(username="o"), name="The owner's")
        self.load([{"external_id": "1", "name": "Bar", "discount": 9, "closed_on": "2026-11-30"}])
        shop = Shop.objects.get(external_id="1")
        self.assertEqual((shop.name, shop.discount, shop.closed_on), ("The owner's", 0, date(2026, 11, 30)))

    def test_a_position_that_is_not_a_number_stops_the_whole_import(self):
        # Python writes NaN for a missing value: with NaN in /shops.json the map opens for nobody
        for latitude in ("NaN", "Infinity", "1e999"):
            with tempfile.NamedTemporaryFile("w", suffix=".json") as file:
                file.write(f'[{{"external_id": "1", "name": "Bar", "latitude": {latitude}, "longitude": 12.5}}]')
                file.flush()
                with self.assertRaises(CommandError, msg=latitude):
                    call_command("import_shops", file.name, stdout=StringIO())
            self.assertFalse(Shop.objects.exists())

    def test_a_wrong_date_stops_the_whole_import(self):
        with self.assertRaises(CommandError):
            self.load([{"external_id": "1", "name": "Bar"}, {"external_id": "2", "name": "Pub", "closed_on": "30/11/2026"}])
        self.assertFalse(Shop.objects.exists())

    def test_a_value_the_model_refuses_stops_the_whole_import(self):
        for bad in ({"discount": 150}, {"latitude": 200, "longitude": 12.5}, {"email": "not an email"}, {"name": ""},
                    {"name": "x" * 121}, {"discount": "many"}):
            with self.assertRaises(CommandError, msg=bad) as stop:
                self.load([{"external_id": "1", "name": "Bar"}, {"external_id": "2", "name": "Pub", **bad}])
            self.assertIn("Shop 2", str(stop.exception))
            self.assertIn(next(iter(bad)), str(stop.exception))
            self.assertFalse(Shop.objects.exists())

    def test_values_arrive_converted(self):
        self.load([{"external_id": "1", "name": "Bar", "discount": "5", "listed": False, "latitude": "41.9", "longitude": "12.5"}])
        shop = Shop.objects.get(external_id="1")
        self.assertEqual((shop.discount, shop.listed, shop.latitude), (5, False, 41.9))

    def test_a_position_written_as_text_must_be_a_number_too(self):
        for latitude in ("NaN", "nan", "inf"):
            with self.assertRaises(CommandError, msg=latitude):
                self.load([{"external_id": "1", "name": "Bar", "latitude": latitude, "longitude": 12.5}])
            self.assertFalse(Shop.objects.exists())

    def test_a_row_with_only_the_date_makes_a_claimed_shop_historic(self):
        self.load([{"external_id": "1", "name": "Bar"}])
        Shop.objects.filter(external_id="1").update(owner=get_user_model().objects.create(username="o"))
        self.load([{"external_id": "1", "closed_on": "2026-11-30"}])
        self.assertEqual(Shop.objects.get(external_id="1").closed_on, date(2026, 11, 30))

    def test_a_new_shop_needs_its_name(self):
        with self.assertRaises(CommandError):
            self.load([{"external_id": "1", "closed_on": "2026-11-30"}])
        self.assertFalse(Shop.objects.exists())

    def test_an_unclaimed_shop_is_checked_when_it_is_updated_too(self):
        self.load([{"external_id": "1", "name": "Bar"}])
        with self.assertRaises(CommandError):
            self.load([{"external_id": "1", "name": "Bar", "discount": 101}])
        self.assertEqual(Shop.objects.get(external_id="1").discount, 0)


class Register(TestCase):
    def setUp(self):
        owner = get_user_model().objects.create(username="k")
        self.shop = Shop.objects.create(owner=owner, name="Bar", lightning_address="a@b.example")

    def test_page_needs_no_login_and_shows_no_destination(self):
        response = self.client.get(f"/en/shops/register/{self.shop.id}/")
        self.assertContains(response, "Bar")

    def test_the_state_of_the_sale_has_a_voice(self):
        """The sale's status goes in a live region that is always present: inside a hidden screen it would not be heard."""
        page = self.client.get(f"/en/shops/register/{self.shop.id}/").content.decode()
        self.assertIn('<p id="state" class="visually-hidden" role="status"></p>', page)
        self.assertGreater(page.index('id="state"'), page.rindex("</section>"))

    def test_register_and_sign_have_their_main_landmark(self):
        owner = self.shop.owner
        self.client.force_login(owner)
        for url in (f"/en/shops/register/{self.shop.id}/", f"/en/shops/sign/{self.shop.id}/"):
            page = self.client.get(url).content.decode()
            self.assertEqual(page.count("<main>"), 1, url)
            self.assertIn("<h1", page.split("<main>")[1].split("</main>")[0], url)


class NotIndexed(TestCase):
    """The till, the sign and the pages of whoever has an account do not end up in search engines, in any mode:
    robots.txt lets the till and the sign be opened, and a page found in a link stays indexed without this
    header."""

    def setUp(self):
        self.owner = get_user_model().objects.create(username="k")
        self.shop = Shop.objects.create(owner=self.owner, name="Bar", lightning_address="a@b.example")

    def assert_not_indexed(self, path, status=200):
        response = self.client.get(path)
        self.assertEqual(response.status_code, status, path)
        self.assertEqual(response.headers.get("X-Robots-Tag"), "noindex, nofollow", path)

    def test_register_and_sign_in_every_mode(self):
        for mode in settings.SITE_MODES:
            with self.subTest(mode), override_settings(SITE_MODE=mode):
                self.client.logout()
                self.assert_not_indexed(f"/en/shops/register/{self.shop.id}/")
                self.assert_not_indexed(f"/en/shops/sign/{self.shop.id}/", status=302)  # to the sign-in
                self.client.force_login(self.owner)
                self.assert_not_indexed(f"/en/shops/sign/{self.shop.id}/")

    def test_the_other_private_pages(self):
        with override_settings(SITE_MODE="production"):
            for path in ("/en/shops/", "/en/shops/recovery/"):
                self.assert_not_indexed(path)
            # an expired or wrong link
            for path in ("/en/shops/recovery/sign-in/?token=x", "/en/shops/email/confirm/?token=x"):
                self.assert_not_indexed(path, status=404)
            self.client.force_login(self.owner)
            for path in ("/en/shops/", "/en/shops/new/", f"/en/shops/edit/{self.shop.id}/",
                         f"/en/shops/remove/{self.shop.id}/", f"/en/shops/registers/{self.shop.id}/"):
                self.assert_not_indexed(path)

    def test_the_public_shop_pages_stay_indexable(self):
        with override_settings(SITE_MODE="production"):
            for path in ("/en/shops/map/", f"/en/shops/s/{self.shop.id}/"):
                self.assertNotIn("X-Robots-Tag", self.client.get(path).headers, path)


class Security(TestCase):
    def test_pages_cannot_be_framed(self):
        self.assertEqual(self.client.get("/it/").headers["X-Frame-Options"], "DENY")

    def test_admin_login_is_limited(self):
        reset_limits()
        # the limit counts in 15-minute windows: with the clock stopped the test does not fall across two of them
        with patch("config.ratelimit.time.time", return_value=1_800_000_000):
            for _ in range(10):
                self.assertNotEqual(self.client.post("/admin/login/", {"username": "a", "password": "b"}).status_code, 429)
            self.assertEqual(self.client.post("/admin/login/", {"username": "a", "password": "b"}).status_code, 429)
        reset_limits()

    def test_clearing_the_cache_does_not_reset_the_limit(self):
        """With the file cache full, Django's cleanup deleted the counters at random too: now they are in the database"""
        reset_limits()
        with patch("config.ratelimit.time.time", return_value=1_800_000_000):
            for _ in range(10):
                self.client.post("/admin/login/", {"username": "a", "password": "b"})
                cache.clear()
            self.assertEqual(self.client.post("/admin/login/", {"username": "a", "password": "b"}).status_code, 429)
        self.assertFalse(RequestCount.objects.filter(key__contains="admin").exists())  # the keys are hashed

    def test_admin_login_is_limited_per_ipv6_network_too(self):
        """65,536 /64 networks in one /48: besides the per-/64 limit the /48 counts too"""
        reset_limits()
        with patch("config.ratelimit.time.time", return_value=1_800_000_000):
            # A different account each time: the per-account limit is another test
            codes = [self.client.post("/admin/login/", {"username": f"a{n}", "password": "b"},
                                      HTTP_X_FORWARDED_FOR=f"2001:db8:1:{n:x}::1").status_code for n in range(31)]
        self.assertNotIn(429, codes[:30])
        self.assertEqual(codes[30], 429)

    def test_admin_login_is_limited_per_account_from_any_address(self):
        """Guessing spread over many addresses: 20 wrong passwords for one account in 15 minutes, then 429 even with
        the right one, whatever the address and however the name is written. The answer is the same for an account that
        does not exist, and the name is not stored as written."""
        reset_limits()
        get_user_model().objects.create_user("boss", password="a long password", is_staff=True)
        with patch("config.ratelimit.time.time", return_value=1_800_000_000):
            codes = [self.client.post("/admin/login/", {"username": "boss", "password": "wrong"},
                                      HTTP_X_FORWARDED_FOR=f"198.51.100.{n}").status_code for n in range(20)]
            self.assertNotIn(429, codes)
            right = self.client.post("/admin/login/", {"username": " BOSS", "password": "a long password"},
                                     HTTP_X_FORWARDED_FOR="203.0.113.99")
            self.assertEqual(right.status_code, 429)
            self.assertIn("Retry-After", right)
            self.assertNotIn("_auth_user_id", self.client.session)
            nobody = [self.client.post("/admin/login/", {"username": "nobody", "password": "wrong"},
                                       HTTP_X_FORWARDED_FOR=f"192.0.2.{n}").status_code for n in range(21)]
            self.assertEqual(nobody[-1], 429)
            self.assertEqual(right.content, b"")
            # Another account is not touched
            self.assertNotEqual(self.client.post("/admin/login/", {"username": "other", "password": "x"},
                                                 HTTP_X_FORWARDED_FOR="203.0.113.98").status_code, 429)
        self.assertFalse(RequestCount.objects.filter(key__contains="boss").exists())
        reset_limits()

    def test_the_account_limit_counts_every_way_of_writing_the_name(self):
        """The sign-in form applies NFKC to the name: fullwidth and mathematical letters sign in as «boss», so they
        count as «boss» too, and do not start fresh counters."""
        reset_limits()
        get_user_model().objects.create_user("boss", password="a long password", is_staff=True)
        names = ["boss", "BOSS", "\uff42oss", "\U0001d41boss", " \uff22\uff2f\uff33\uff33 "]
        with patch("config.ratelimit.time.time", return_value=1_800_000_000):
            codes = [self.client.post("/admin/login/", {"username": names[n % len(names)], "password": "wrong"},
                                      HTTP_X_FORWARDED_FOR=f"198.51.100.{n}").status_code for n in range(20)]
            self.assertNotIn(429, codes)
            for name in names:
                self.assertEqual(self.client.post("/admin/login/", {"username": name, "password": "a long password"},
                                                  HTTP_X_FORWARDED_FOR="203.0.113.99").status_code, 429, name)
        reset_limits()

    def test_a_known_browser_signs_in_while_the_account_limit_is_spent(self):
        """Whoever spends the account's tries from elsewhere does not lock out the staff: a browser that signed in to
        that account before still gets in, a new one waits. The per-address limits still hold for the known browser."""
        reset_limits()
        boss = get_user_model().objects.create_user("boss", password="a long password", is_staff=True)
        known = self.client_class()
        with patch("config.ratelimit.time.time", return_value=1_799_998_000):
            first = known.post("/admin/login/", {"username": "boss", "password": "a long password"},
                               HTTP_X_FORWARDED_FOR="203.0.113.5")
        self.assertEqual(first.status_code, 302)
        cookie = first.cookies["admin_device"]
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["path"], "/admin/")
        known.cookies.pop("sessionid", None)
        with patch("config.ratelimit.time.time", return_value=1_800_000_000):
            for n in range(25):
                self.client_class().post("/admin/login/", {"username": "boss", "password": "wrong"},
                                         HTTP_X_FORWARDED_FOR=f"198.51.100.{n}")
            stranger = self.client_class().post("/admin/login/", {"username": "boss", "password": "a long password"},
                                                HTTP_X_FORWARDED_FOR="203.0.113.6")
            self.assertEqual(stranger.status_code, 429)
            # A cookie of another account, or forged, does not help
            forged = self.client_class()
            forged.cookies["admin_device"] = "forged"
            self.assertEqual(forged.post("/admin/login/", {"username": "boss", "password": "a long password"},
                                         HTTP_X_FORWARDED_FOR="203.0.113.7").status_code, 429)
            response = known.post("/admin/login/", {"username": "boss", "password": "a long password"},
                                  HTTP_X_FORWARDED_FOR="203.0.113.5")
            self.assertEqual(response.status_code, 302)
            self.assertEqual(int(known.session["_auth_user_id"]), boss.pk)
            # The same cookie with the name of another account does not pass that account's limit
            get_user_model().objects.create_user("other", password="another long password", is_staff=True)
            for n in range(25):
                self.client_class().post("/admin/login/", {"username": "other", "password": "wrong"},
                                         HTTP_X_FORWARDED_FOR=f"192.0.2.{n}")
            self.assertEqual(known.post("/admin/login/", {"username": "other", "password": "another long password"},
                                        HTTP_X_FORWARDED_FOR="203.0.113.5").status_code, 429)
            # The per-address limit still holds for the known browser
            codes = [known.post("/admin/login/", {"username": "boss", "password": "wrong"},
                                HTTP_X_FORWARDED_FOR="203.0.113.5").status_code for _ in range(10)]
            self.assertEqual(codes[-1], 429)
        reset_limits()

    def test_a_new_password_ends_the_known_browser(self):
        reset_limits()
        boss = get_user_model().objects.create_user("boss", password="a long password", is_staff=True)
        known = self.client_class()
        with patch("config.ratelimit.time.time", return_value=1_799_998_000):
            self.assertEqual(known.post("/admin/login/", {"username": "boss", "password": "a long password"},
                                        HTTP_X_FORWARDED_FOR="203.0.113.5").status_code, 302)
        known.cookies.pop("sessionid", None)
        boss.set_password("a new long password 7")
        boss.save()
        with patch("config.ratelimit.time.time", return_value=1_800_000_000):
            for n in range(25):
                self.client_class().post("/admin/login/", {"username": "boss", "password": "wrong"},
                                         HTTP_X_FORWARDED_FOR=f"198.51.100.{n}")
            self.assertEqual(known.post("/admin/login/", {"username": "boss", "password": "a new long password 7"},
                                        HTTP_X_FORWARDED_FOR="203.0.113.5").status_code, 429)
        reset_limits()

    def test_the_right_password_does_not_use_up_the_account_tries(self):
        reset_limits()
        get_user_model().objects.create_user("boss", password="a long password", is_staff=True)
        with patch("config.ratelimit.time.time", return_value=1_800_000_000):
            for n in range(25):
                client = self.client_class()
                response = client.post("/admin/login/", {"username": "boss", "password": "a long password"},
                                       HTTP_X_FORWARDED_FOR=f"198.51.100.{n}")
                self.assertEqual(response.status_code, 302, n)
        reset_limits()

    def test_expired_form_page_is_translated(self):
        client = self.client_class(enforce_csrf_checks=True)
        self.assertContains(client.post("/it/shops/recovery/", {"email": "a@example.org"}), "Pagina scaduta", status_code=403)


class PrivatePages(TestCase):
    """Pages answered to someone signed in, and the pages of the email links, are kept by no cache, the browser's
    included; public pages and files keep their own caching."""

    def setUp(self):
        reset_limits()
        self.owner = get_user_model().objects.create(username="owner")
        self.shop = Shop.objects.create(owner=self.owner, name="Bar", lightning_address="till@ln.example.org")

    def test_the_owner_pages_are_not_kept(self):
        self.client.force_login(self.owner)
        for path in ("/en/shops/", f"/en/shops/edit/{self.shop.id}/", f"/en/shops/card/{self.shop.id}/",
                     f"/en/shops/register/{self.shop.id}/", "/en/shops/recovery/", "/en/"):
            response = self.client.get(path)
            self.assertIn(response.status_code, (200, 403), path)
            self.assertIn("no-store", response.get("Cache-Control", ""), path)
            self.assertIn("private", response.get("Cache-Control", ""), path)

    def test_the_sign_in_and_the_email_links_are_not_kept(self):
        for path in ("/en/shops/", "/en/shops/recovery/", "/en/shops/recovery/sign-in/?token=x", "/en/shops/email/confirm/?token=x"):
            self.assertIn("no-store", self.client.get(path).get("Cache-Control", ""), path)
        self.assertIn("no-store", self.client.post("/en/shops/recovery/", {"email": "a@example.org"})["Cache-Control"])

    def test_public_answers_keep_their_caching(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(f"/en/shops/register/{self.shop.id}/sw.js")["Cache-Control"], "no-cache")
        self.assertNotIn("no-store", self.client.get("/theme/logo.svg").get("Cache-Control", ""))
        visitor = self.client_class()
        for path in (f"/en/shops/register/{self.shop.id}/", "/health/", "/en/"):
            self.assertNotIn("no-store", visitor.get(path).get("Cache-Control", ""), path)


class ShopPageModeration(TestCase):
    def test_a_suspended_shop_keeps_its_page_without_text_contacts_or_search_engines(self):
        shop = Shop.objects.create(name="Bar", description="Visit our site", website="https://phishing.example/",
                                   phone="123456", email="a@b.example", suspended=True)
        response = self.client.get(f"/en/shops/s/{shop.id}/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Bar")
        for hidden in ("Visit our site", "phishing.example", "123456", "a@b.example"):
            self.assertNotContains(response, hidden)
        self.assertEqual(response["X-Robots-Tag"], "noindex, nofollow")

    def test_a_shop_off_the_maps_is_not_indexed(self):
        shop = Shop.objects.create(name="Bar", description="Nice place", listed=False)
        response = self.client.get(f"/en/shops/s/{shop.id}/")
        self.assertContains(response, "Nice place")
        self.assertEqual(response["X-Robots-Tag"], "noindex, nofollow")

    def test_descriptions_have_a_limit(self):
        form = ShopDetailsForm({"name": "Bar", "description": "-" * 1001 + "x"})
        self.assertIn("description", form.errors)

    def test_invisible_controls_are_taken_out_of_the_shop_text(self):
        form = ShopDetailsForm({"name": "Bar\u202eotsiR", "description": "Local\u200b cooking 👨\u200d🍳"})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["name"], "BarotsiR")
        self.assertEqual(form.cleaned_data["description"], "Local cooking 👨\u200d🍳")

    def test_names_that_speak_for_the_association_are_reserved(self):
        for name in ("info", "Admin", "associazione"):
            self.assertIn("nip05_name", ShopDetailsForm({"name": "Bar", "nip05_name": name}).errors, name)

    def test_the_nostr_text_has_no_links_and_no_markdown(self):
        from .nostr import shop_text
        text = shop_text(Shop(name="Bar", description="[click](https://scam.example) *now* www.scam.example"))
        self.assertNotIn("scam", text)
        self.assertEqual(text, "\\[click\\]\\( \\*now\\*")
        self.assertEqual(len(shop_text(Shop(name="Bar", description="a" * 5000))), 1000)


class TelegramAlerts(TestCase):
    def test_a_shop_name_cannot_put_a_link_in_the_community_bot_messages(self):
        from .notifications import alert_name
        shop = Shop(name="A request is waiting:\nhttps://falso.example/admin/x")
        self.assertEqual(alert_name(shop), "A request is waiting: …")
        self.assertEqual(alert_name(Shop(name="Bar 2.0 di Mario")), "Bar 2.0 di Mario")

    def test_a_name_cannot_mention_command_or_rewrite_the_lines(self):
        from .notifications import alert_text
        self.assertEqual(alert_text("@admin and @all"), "\uff20admin and \uff20all")
        self.assertEqual(alert_text("/start now"), "\u2215start now")
        self.assertEqual(alert_text("Anna\x1b[2J\u202e"), "Anna?[2J?")
        self.assertEqual(alert_text("Ana 👩\u200d💻"), "Ana 👩\u200d💻")

    @override_settings(TELEGRAM_BOT_TOKEN="t")
    def test_telegram_says_how_it_went(self):
        """True delivered, False refused for good (a wrong chat, a bot thrown out), None nothing to send with; the
        errors worth trying again raise, without the URL that holds the token."""
        from .notifications import NotificationFailed, telegram
        for status, outcome in ((200, True), (400, False), (401, False), (403, False)):
            with self.subTest(status=status), patch("shops.notifications.requests.post") as post:
                post.return_value.status_code = status
                self.assertIs(telegram("-100", "x"), outcome)
        self.assertIsNone(telegram("", "x"))
        with override_settings(TELEGRAM_BOT_TOKEN=""):
            self.assertIsNone(telegram("-100", "x"))
        for status in (429, 502):
            with self.subTest(status=status), patch("shops.notifications.requests.post") as post:
                post.return_value.status_code = status
                with self.assertRaises(NotificationFailed):
                    telegram("-100", "x")

    @override_settings(TELEGRAM_MODERATION_CHAT="-100123")
    def test_the_moderators_chat_is_not_a_shop_chat(self):
        form = ShopDetailsForm({"name": "Bar", "telegram_chat": "-100123"})
        self.assertFalse(form.is_valid())
        self.assertIn("telegram_chat", form.errors)


class LoginChallenges(TestCase):
    def test_new_accounts_from_one_address_are_limited(self):
        reset_limits()
        codes = []
        with patch("config.ratelimit.time.time", return_value=1_800_000_000):
            for n in range(21):
                client = self.client_class()
                client.get("/en/shops/")
                LoginChallenge.objects.filter(linking_key="").update(linking_key=f"02{n:064x}")
                codes.append(client.get(reverse("login_status")).status_code)
        self.assertEqual(codes[:20], [200] * 20)
        self.assertEqual(codes[20], 429)
        self.assertEqual(get_user_model().objects.filter(username__startswith="02").count(), 20)


    def test_the_same_browser_reuses_its_live_challenge(self):
        for _ in range(5):
            self.client.get("/en/shops/")
        self.assertEqual(LoginChallenge.objects.count(), 1)
        self.client.cookies.clear()
        self.client.get("/en/shops/")
        self.assertEqual(LoginChallenge.objects.count(), 2)

    def test_new_challenges_from_one_address_are_limited(self):
        """Without the cookie every visit wrote a row and drew a QR: from one address 60 in ten minutes, then the page
        has no wallet code for a while, and the email still signs in. A browser with its challenge alive goes on."""
        reset_limits()
        self.enterContext(steady_limits())
        self.client.get("/en/shops/")
        for _ in range(59):
            self.assertEqual(self.client_class().get("/en/shops/").status_code, 200)
        self.assertEqual(LoginChallenge.objects.count(), 60)
        response = self.client_class().get("/en/shops/")
        self.assertContains(response, "Too many sign-in codes from your connection", status_code=429)
        self.assertContains(response, 'href="/en/shops/recovery/"', status_code=429)
        self.assertNotContains(response, "lightning:", status_code=429)
        self.assertNotIn("k1", response.cookies)
        self.assertIn("Retry-After", response)
        self.assertEqual(LoginChallenge.objects.count(), 60)
        self.assertContains(self.client.get("/en/shops/"), "lightning:")
        # Another address is not touched
        self.assertEqual(self.client_class().get("/en/shops/", HTTP_X_FORWARDED_FOR="198.51.100.9").status_code, 200)
        reset_limits()

    def test_a_signed_or_old_challenge_is_not_reused(self):
        self.client.get("/en/shops/")
        LoginChallenge.objects.update(linking_key="02" + "ab" * 32)
        self.client.get("/en/shops/")
        self.assertEqual(LoginChallenge.objects.filter(linking_key="").count(), 1)
        LoginChallenge.objects.update(created=timezone.now() - timedelta(minutes=3))
        self.client.get("/en/shops/")
        self.assertEqual(LoginChallenge.objects.filter(created__gte=timezone.now() - timedelta(minutes=1)).count(), 1)


class AccountTakeover(TestCase):
    """Letters that the database confuses must not open another person's account."""
    def setUp(self):
        reset_limits()
        self.victim = get_user_model().objects.create(username="victim")
        Profile.objects.create(user=self.victim, email="mario@gmail.com")

    def test_lookalike_address_is_a_different_account(self):
        self.client.post("/en/shops/recovery/", {"email": "mario@gma\u0131l.com"})
        link = mail.outbox[-1].body.split(settings.PUBLIC_URL)[1].split()[0]
        self.client.post(link)
        self.assertNotEqual(self.client.session.get("_auth_user_id"), str(self.victim.pk))

    def test_lookalike_address_is_not_a_taken_address(self):
        other = get_user_model().objects.create(username="other")
        Profile.objects.create(user=other, email="luigi@example.org")
        self.client.force_login(other)
        self.client.post("/en/shops/", {"email": "mario@gma\u0131l.com"})
        link = mail.outbox[-1].body.split(settings.PUBLIC_URL)[1].split()[0]
        self.assertEqual(self.client.post(link).status_code, 302)
        # Saved as the mail writes it: "gmaıl.com" with the dotless i is another domain, not gmail.com
        self.assertEqual(Profile.objects.get(user=other).email, "mario@xn--gmal-nza.com")

    def test_same_address_in_capitals_is_the_same_account(self):
        self.client.post("/en/shops/recovery/", {"email": "MARIO@GMAIL.COM"})
        link = mail.outbox[-1].body.split(settings.PUBLIC_URL)[1].split()[0]
        self.client.post(link)
        self.assertEqual(self.client.session.get("_auth_user_id"), str(self.victim.pk))

    @override_settings(ALLOWED_HOSTS=["evil.onion", "testserver"], PUBLIC_URL="https://website.example.org")
    def test_links_do_not_follow_the_host_header(self):
        self.client.post("/en/shops/recovery/", {"email": "mario@gmail.com"}, headers={"Host": "evil.onion"})
        self.assertIn("https://website.example.org/", mail.outbox[-1].body)
        self.assertNotIn("evil.onion", mail.outbox[-1].body)


class WhoIsSignedIn(TestCase):
    """A wallet signs in whatever browser shows it the code (LUD-04): someone could pass on a code of their own. The
    panel says which account it belongs to, with a way out."""
    KEY = "02" + "ab" * 31 + "cd"

    def setUp(self):
        reset_limits()
        self.user = get_user_model().objects.create(username=self.KEY)
        self.client.force_login(self.user)

    def test_the_key_in_short(self):
        page = self.client.get("/en/shops/").content.decode()
        self.assertIn(f"Signed in as {self.KEY[:6]}…{self.KEY[-4:]}.", page)
        self.assertNotIn(self.KEY, page)
        self.assertIn("Not you? Sign out", page)

    def test_the_recovery_email_when_there_is_one(self):
        Profile.objects.create(user=self.user, email="owner@example.org")
        self.assertIn("Signed in as owner@example.org.", self.client.get("/en/shops/").content.decode())

    def test_not_you_signs_out(self):
        page = self.client.get("/en/shops/").content.decode()
        form = re.search(r'<form method="post" action="([^"]+)" class="account-who', page)
        self.assertIsNotNone(form)
        self.client.post(form.group(1))
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_italian_and_german(self):
        from django.utils.translation import gettext

        self.assertIn("Non sei tu? Esci", self.client.get("/it/shops/").content.decode())
        # The example community has no German pages: the catalogue says it
        with override("de"):
            self.assertEqual(gettext("Not you? Sign out"), "Nicht Sie? Abmelden")
            self.assertEqual(gettext("Signed in as %(who)s."), "Angemeldet als %(who)s.")


class LoginChallengeRules(TestCase):
    def signed(self, k1):
        key = coincurve.PrivateKey()
        return {"k1": k1, "sig": key.sign(bytes.fromhex(k1), hasher=None).hex(), "key": key.public_key.format().hex()}

    def k1(self):
        self.client.get("/en/shops/")
        return LoginChallenge.objects.latest("created").k1

    def test_first_signature_wins(self):
        k1 = self.k1()
        first, second = self.signed(k1), self.signed(k1)
        self.assertEqual(self.client.get("/lnurl-auth/", first).json()["status"], "OK")
        self.assertEqual(self.client.get("/lnurl-auth/", second).json()["status"], "ERROR")
        self.client.get("/lnurl-auth/status/")
        self.assertTrue(get_user_model().objects.filter(username=first["key"]).exists())
        self.assertFalse(get_user_model().objects.filter(username=second["key"]).exists())

    def test_the_wallet_can_repeat_its_own_signature(self):
        k1 = self.k1()
        first = self.signed(k1)
        self.client.get("/lnurl-auth/", first)
        self.assertEqual(self.client.get("/lnurl-auth/", first).json()["status"], "OK")

    def test_one_key_has_one_form(self):
        k1 = self.k1()
        key = coincurve.PrivateKey()
        signature = key.sign(bytes.fromhex(k1), hasher=None).hex()
        long_form = key.public_key.format(compressed=False).hex()
        self.assertEqual(self.client.get("/lnurl-auth/", {"k1": k1, "sig": signature, "key": long_form}).json()["status"], "ERROR")
        spaced = key.public_key.format().hex()
        self.assertEqual(self.client.get("/lnurl-auth/", {"k1": k1, "sig": signature, "key": f"{spaced[:2]} {spaced[2:]}"}).json()["status"], "ERROR")

    def test_capitals_are_the_same_key(self):
        k1 = self.k1()
        answer = self.signed(k1)
        self.client.get("/lnurl-auth/", {**answer, "key": answer["key"].upper()})
        self.client.get("/lnurl-auth/status/")
        self.assertTrue(get_user_model().objects.filter(username=answer["key"]).exists())

    def test_a_wallet_does_not_open_a_staff_account(self):
        k1 = self.k1()
        answer = self.signed(k1)
        get_user_model().objects.create(username=answer["key"], is_staff=True)
        self.client.get("/lnurl-auth/", answer)
        self.assertFalse(self.client.get("/lnurl-auth/status/").json()["logged_in"])
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_strange_k1_is_refused_quietly(self):
        self.assertEqual(self.client.get("/lnurl-auth/", {"k1": "a\x00b", "sig": "00", "key": "00"}).json()["status"], "ERROR")

    def test_an_old_signed_challenge_no_longer_opens(self):
        k1 = self.k1()
        self.client.get("/lnurl-auth/", self.signed(k1))
        LoginChallenge.objects.filter(pk=k1).update(created=timezone.now() - timezone.timedelta(minutes=6))
        self.assertFalse(self.client.get("/lnurl-auth/status/").json()["logged_in"])


class ClientAddress(TestCase):
    def ip(self, forwarded):
        return ratelimit.client_ip(RequestFactory().get("/", headers={"X-Forwarded-For": forwarded}))

    def test_the_address_written_by_the_proxy_counts(self):
        self.assertEqual(self.ip("1.2.3.4, 5.6.7.8"), "5.6.7.8")

    def test_one_ipv6_network_is_one_client(self):
        self.assertEqual(self.ip("2001:db8:1:2::1"), self.ip("2001:db8:1:2:ffff::9"))
        self.assertNotEqual(self.ip("2001:db8:1:2::1"), self.ip("2001:db8:1:3::1"))

    def test_the_wider_ipv6_network_with_prefix_48(self):
        request = RequestFactory().get("/", headers={"X-Forwarded-For": "2001:db8:1:2::1"})
        self.assertEqual(ratelimit.client_ip(request, prefix=48), "2001:db8:1::")
        self.assertEqual(self.ip("2001:db8:1:2::1"), "2001:db8:1:2::")

    def test_an_ipv4_mapped_address_is_the_ipv4_client(self):
        """A dual-stack listener can write an IPv4 client as ::ffff:1.2.3.4: it is not an IPv6 network of its own."""
        self.assertEqual(self.ip("::ffff:203.0.113.7"), "203.0.113.7")
        self.assertEqual(self.ip("::ffff:203.0.113.7"), self.ip("203.0.113.7"))

    def test_an_address_on_the_ipv6_docker_network_is_its_own_client(self):
        """With compose.ipv6.yml two containers of the ULA /64 are one network, a visitor from outside another one."""
        self.assertEqual(self.ip("fd12:3456:789a:1::3"), "fd12:3456:789a:1::")
        self.assertNotEqual(self.ip("fd12:3456:789a:1::3"), self.ip("2001:db8:5::1"))


class Passwords(TestCase):
    def test_the_admin_cannot_have_a_weak_password(self):
        with self.assertRaises(ValidationError):
            validate_password("bitcoin12")


class Settings(TestCase):
    def load(self, **env):
        code = "from django.conf import settings; print(settings.EMAIL_REAL_BACKEND)"
        base = {"PATH": os.environ["PATH"], "COMMUNITY_DIR": os.environ.get("COMMUNITY_DIR", "examples/community"), "SECRET_KEY": "x",
                "DJANGO_SETTINGS_MODULE": "config.settings"}
        return subprocess.run([sys.executable, "-c", code], env={**base, **env}, capture_output=True, text=True)

    def test_without_a_mail_server_mainnet_does_not_print_links_in_the_log(self):
        self.assertIn("smtp", self.load(NETWORK="mainnet").stdout)
        self.assertIn("console", self.load(NETWORK="signet").stdout)

    def test_debug_never_runs_on_mainnet(self):
        self.assertIn("DEBUG", self.load(NETWORK="mainnet", DEBUG="1").stderr)

    def test_the_site_mode_is_one_of_three(self):
        self.assertIn("SITE_MODE", self.load(SITE_MODE="trial").stderr)
        for mode in ("development", "maintenance"):
            self.assertEqual(self.load(SITE_MODE=mode).returncode, 0, mode)

    def test_the_links_of_the_five_minute_try_open_on_runserver(self):
        """With DEBUG and no SITE_HOST the site is the one of the README's try: its sign-in links must not be https://localhost."""
        code = "from django.conf import settings; print(settings.PUBLIC_URL)"

        def url(**env):
            base = {"PATH": os.environ["PATH"], "COMMUNITY_DIR": os.environ.get("COMMUNITY_DIR", "examples/community"),
                    "SECRET_KEY": "x", "DJANGO_SETTINGS_MODULE": "config.settings"}
            return subprocess.run([sys.executable, "-c", code], env={**base, **env}, capture_output=True, text=True).stdout.strip()

        self.assertEqual(url(DEBUG="1"), "http://localhost:8000")
        self.assertEqual(url(DEBUG="1", SITE_HOST="shops.example.org"), "https://shops.example.org")
        self.assertEqual(url(SITE_HOST="shops.example.org"), "https://shops.example.org")
        self.assertEqual(url(), "https://localhost")

    def test_production_takes_real_payments_only(self):
        self.assertIn("mainnet", self.load(SITE_MODE="production", NETWORK="signet").stderr)
        self.assertEqual(self.load(SITE_MODE="production", NETWORK="mainnet").returncode, 0)


class TelegramToken(TestCase):
    @override_settings(TELEGRAM_BOT_TOKEN="123:secret")
    def test_the_token_never_reaches_the_log(self):
        leak = requests.ConnectionError("Max retries exceeded with url: /bot123:secret/sendMessage")
        with patch("shops.notifications.requests.post", side_effect=leak):
            with self.assertRaises(NotificationFailed) as caught:
                telegram("42", "ciao")
        self.assertNotIn("secret", "".join(traceback.format_exception(caught.exception)))

    @override_settings(TELEGRAM_BOT_TOKEN="t")
    def test_telegram_busy_is_a_failure(self):
        with patch("shops.notifications.requests.post") as post:
            post.return_value.status_code = 429
            with self.assertRaises(NotificationFailed):
                telegram("42", "ciao")


class Admin(TestCase):
    def setUp(self):
        admin_session(self.client)
        self.shop = Shop.objects.create(name="Bar", lightning_address="a@b.it")

    def test_destinations_are_not_edited_here(self):
        page = self.client.get(f"/admin/shops/shop/{self.shop.id}/change/")
        self.assertNotContains(page, 'name="lightning_address"')


class ShopWithoutStore(TestCase):
    """A shop imported and assigned by the administrator has no store to update."""
    def setUp(self):
        self.owner = get_user_model().objects.create(username="k")
        self.client.force_login(self.owner)
        self.shop = Shop.objects.create(owner=self.owner, name="Imported")

    def test_edit_and_delete(self):
        self.assertEqual(self.client.post(f"/it/shops/edit/{self.shop.id}/", {"name": "New", "discount": 0}).status_code, 302)
        self.assertEqual(self.client.post(f"/it/shops/remove/{self.shop.id}/").status_code, 302)
        self.assertFalse(Shop.objects.filter(pk=self.shop.pk).exists())

    def test_panel_offers_no_register(self):
        response = self.client.get("/it/shops/")
        self.assertNotContains(response, f"/shops/register/{self.shop.id}/")
        self.assertNotContains(response, "La cassa per chi lavora con te")
        self.assertContains(response, "Niente cassa: i pagamenti di questo negozio non sono configurati su questo sito.")

    def test_suspended_or_historic_does_not_promise_a_register(self):
        Shop.objects.filter(pk=self.shop.pk).update(suspended=True)
        response = self.client.get("/it/shops/")
        self.assertContains(response, "Sospeso: non compare in mappa né negli elenchi.")
        self.assertNotContains(response, "Cassa e incassi continuano a funzionare")
        Shop.objects.filter(pk=self.shop.pk).update(suspended=False, latitude=41.9, longitude=12.5, closed_on=date(2026, 10, 1))
        response = self.client.get("/it/shops/")
        self.assertContains(response, "Sulla mappa con un segnaposto suo, non tra i posti dove pagare.")
        self.assertNotContains(response, "Cassa e incassi continuano a funzionare")


class NostrNames(TestCase):
    def form(self, name, instance=None):
        return ShopDetailsForm({"name": "Bar", "nip05_name": name}, instance=instance)

    def test_a_name_belongs_to_one_shop(self):
        Shop.objects.create(name="First", nip05_name="bar")
        self.assertIn("nip05_name", self.form("bar").errors)
        self.assertIn("nip05_name", self.form("BAR").errors)

    def test_the_owner_keeps_the_name(self):
        shop = Shop.objects.create(name="First", nip05_name="bar")
        self.assertNotIn("nip05_name", self.form("bar", shop).errors)

    def test_the_name_of_the_community_is_reserved(self):
        self.assertIn("nip05_name", self.form("_").errors)

    @override_settings(NOSTR_SECRET_KEY="")
    def test_a_shop_cannot_be_the_community(self):
        Shop.objects.create(name="Bar", latitude=1, longitude=1, nip05_name="_", nostr_pubkey="ab" * 32)
        self.assertNotIn("_", self.client.get("/.well-known/nostr.json").json()["names"])


class ImportedWebsites(TestCase):
    def load(self, website):
        with tempfile.NamedTemporaryFile("w", suffix=".json") as file:
            json.dump([{"external_id": "1", "name": "Bar", "website": website}], file)
            file.flush()
            call_command("import_shops", file.name, stdout=StringIO())
        return Shop.objects.get(external_id="1").website

    def test_a_script_is_not_a_website(self):
        self.assertEqual(self.load("javascript:alert(1)"), "")

    def test_a_website_stays(self):
        self.assertEqual(self.load("https://example.org/"), "https://example.org/")


class ProviderAnswers(TestCase):
    def reply(self, chunks):
        response = MagicMock()
        response.__enter__.return_value = response
        response.iter_content.return_value = chunks
        return response

    def fetch(self, chunks):
        with public_names(), patch("shops.lnaddress.requests.Session.get", return_value=self.reply(chunks)) as get:
            return fetch("https://example.org/a"), get

    def test_an_answer_is_read(self):
        self.assertEqual(self.fetch([b'{"a":', b" 1}"])[0], {"a": 1})

    def test_the_answer_is_not_loaded_before_checking_it(self):
        self.assertTrue(self.fetch([b"{}"])[1].call_args.kwargs["stream"])

    def test_a_huge_answer_is_cut(self):
        with self.assertRaises(ValueError):
            self.fetch([b" " * 60000] * 3)

    def test_a_slow_answer_is_cut(self):
        # The clock: the deadline, the time left after the DNS, then the first chunk read
        with patch("shops.lnaddress.time.monotonic", side_effect=[0, 0, 1000]), self.assertRaises(ValueError):
            self.fetch([b"{", b"}"])

    def test_an_answer_nested_too_deep_is_not_an_error_of_the_site(self):
        # 60 KB of "[" are under MAX_BYTES, but json.loads ends in RecursionError: it would be a 500 for the owner
        with self.assertRaises(ValueError):
            self.fetch([b"[" * 60000])

    def test_a_name_that_changes_address_reaches_nothing_inside(self):
        """DNS rebinding: the check sees a public address, the connection gets an internal one (here the check itself
        is told the internal one is fine: the connection checks again the address it reached). The fake internal server
        must not even receive the TLS greeting."""
        listener = socket.create_server(("127.0.0.1", 0))
        listener.settimeout(5)
        received = []

        def inside():
            try:
                connection, _address = listener.accept()
                connection.settimeout(3)
                with connection:
                    received.append(connection.recv(4096))
            except OSError:
                received.append(b"")
            listener.close()

        server = threading.Thread(target=inside, daemon=True)
        server.start()
        here = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", listener.getsockname()[1]))]
        with patch("shops.lnaddress.public_addresses", return_value=here):
            with self.assertRaises((requests.RequestException, ValueError)):
                fetch("https://example.org/.well-known/lnurlp/mario")
        server.join(10)
        self.assertEqual(received, [b""])

    @skipUnless(shutil.which("openssl"), "needs openssl for the fake server's certificate")
    def test_an_answer_one_byte_at_a_time_is_cut_at_max_seconds(self):
        """One byte every 0.2 seconds: each read arrives before TIMEOUT, and without the watchdog the probe would stay
        hanging as long as the server wants. The fake server is on 127.0.0.1, with a certificate for example.org made here."""
        with tempfile.TemporaryDirectory() as folder:
            key, certificate = os.path.join(folder, "key.pem"), os.path.join(folder, "cert.pem")
            subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
                            "-keyout", key, "-out", certificate, "-days", "1", "-subj", "/CN=example.org",
                            "-addext", "subjectAltName=DNS:example.org"], check=True, capture_output=True)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(certificate, key)
            listener = socket.create_server(("127.0.0.1", 0))
            port = listener.getsockname()[1]

            def drip():
                connection, _address = listener.accept()
                with context.wrap_socket(connection, server_side=True) as tls:
                    tls.recv(4096)
                    tls.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 1000\r\n\r\n{")
                    stop = time.monotonic() + 6
                    try:
                        while time.monotonic() < stop:
                            tls.sendall(b" ")
                            time.sleep(0.2)
                    except OSError:
                        pass
                listener.close()

            server = threading.Thread(target=drip, daemon=True)
            server.start()
            here = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", port))]
            started = time.monotonic()
            # The fake server is on 127.0.0.1: here the timing is tested, not the address
            with patch("shops.lnaddress.public_addresses", return_value=here), patch("shops.lnaddress.MAX_SECONDS", 1), \
                    patch("shops.lnaddress.is_public", return_value=True), \
                    patch.dict(os.environ, {"REQUESTS_CA_BUNDLE": certificate}):
                with self.assertRaises((requests.RequestException, ValueError)):
                    fetch("https://example.org/.well-known/lnurlp/mario")
            elapsed = time.monotonic() - started
            server.join(10)
        self.assertLess(elapsed, 3)


    @skipUnless(shutil.which("openssl"), "needs openssl for the fake server's certificate")
    def test_the_connection_goes_to_the_address_checked_without_asking_again(self):
        """The name is asked once, within the deadline: the connection goes to the address checked, and TLS checks the
        certificate for the name. A second lookup, by urllib3, would have no time limit."""
        with tempfile.TemporaryDirectory() as folder:
            key, certificate = os.path.join(folder, "key.pem"), os.path.join(folder, "cert.pem")
            subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
                            "-keyout", key, "-out", certificate, "-days", "1", "-subj", "/CN=example.org",
                            "-addext", "subjectAltName=DNS:example.org"], check=True, capture_output=True)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(certificate, key)
            listener = socket.create_server(("127.0.0.1", 0))
            listener.settimeout(5)
            port = listener.getsockname()[1]
            seen = []

            def answer():
                try:
                    connection, _address = listener.accept()
                    with context.wrap_socket(connection, server_side=True) as tls:
                        seen.append(tls.recv(4096))
                        tls.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 8\r\n\r\n{\"a\": 1}")
                except OSError:
                    pass
                listener.close()

            server = threading.Thread(target=answer, daemon=True)
            server.start()
            here = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", port))]
            with patch("shops.lnaddress.resolve", return_value=here) as asked, \
                    patch("shops.lnaddress.is_public", return_value=True), \
                    patch("socket.getaddrinfo", side_effect=AssertionError("asked again")) as again, \
                    patch.dict(os.environ, {"REQUESTS_CA_BUNDLE": certificate}):
                self.assertEqual(fetch("https://example.org/.well-known/lnurlp/mario", 3), {"a": 1})
            server.join(10)
        again.assert_not_called()
        self.assertEqual(asked.call_count, 1)
        self.assertLessEqual(asked.call_args.kwargs["seconds"], 3)
        self.assertIn(b"Host: example.org", seen[0])

class ShopLimit(TestCase):
    def test_new_shops_are_limited_per_address(self):
        self.enterContext(steady_limits())
        reset_limits()
        self.client.force_login(get_user_model().objects.create(username="k"))
        with patch.dict(community(), {"limits": {"shops_per_address_hour": 1}}), patch("shops.views.refusal", return_value=None), \
                patch("shops.forms.public_host", return_value=True):
            for name in ("Uno", "Due"):
                self.client.post("/en/shops/new/", {"name": name, "lightning_address": "mario@example.org", "discount": 0})
        self.assertEqual(Shop.objects.count(), 1)
        reset_limits()


class Prefill(TestCase):
    """A link may propose the shop's data, never where the money goes, and logging in does not lose them."""

    def setUp(self):
        reset_limits()
        self.user = get_user_model().objects.create(username="merchant")
        self.url = reverse("new_shop")

    def form(self, **query):
        self.client.force_login(self.user)
        return self.client.get(self.url, query)

    def test_the_form_shows_what_the_link_says(self):
        response = self.form(name="Corner Bar", description="Bar", city="King's Example", address="Main Street 1", discount="10")
        form = response.context["form"]
        self.assertEqual(form["name"].value(), "Corner Bar")
        self.assertEqual(form["description"].value(), "Bar")
        self.assertEqual(form["city"].value(), "King's Example")
        self.assertEqual(form["address"].value(), "Main Street 1")
        self.assertEqual(form["discount"].value(), 10)
        self.assertContains(response, 'value="Corner Bar"')
        self.assertContains(response, 'class="notice" role="status"')

    def test_no_link_no_notice(self):
        self.assertNotContains(self.form(), 'class="notice" role="status"')

    def test_a_link_never_chooses_where_the_money_goes(self):
        response = self.form(name="Bar", lightning_address="thief@example.com", website="https://x.example",
                             email="a@example.com", phone="123", latitude="1", longitude="2", nostr_pubkey="ab", telegram_chat="@thief")
        form = response.context["form"]
        self.assertEqual(form["name"].value(), "Bar")
        for field in ("lightning_address", "website", "email", "phone", "latitude", "longitude", "nostr_pubkey", "telegram_chat"):
            self.assertFalse(form[field].value(), field)
        self.assertNotContains(response, "thief")

    def test_a_value_that_breaks_the_rules_is_dropped(self):
        form = self.form(name="x" * 121, city="y" * 101, description=SEED, address="Main\nStreet", discount="101")
        self.assertEqual(form.context["prefilled"], {})

    def test_a_discount_must_be_a_number(self):
        self.assertEqual(self.form(discount="a lot").context["prefilled"], {})
        self.assertEqual(self.form(discount="0").context["prefilled"], {})

    def test_markup_stays_text(self):
        response = self.form(name='"><script>alert(1)</script>')
        self.assertNotContains(response, "<script>alert(1)")

    def test_the_post_ignores_the_query(self):
        self.client.force_login(self.user)
        self.client.post(f"{self.url}?lightning_address=thief@example.com&name=Link", {"name": "Mio", "lightning_address": ""})
        self.assertFalse(Shop.objects.exists())

    def test_the_email_link_brings_you_back_to_the_form(self):
        target = f"{self.url}?{urlencode({'name': 'Corner Bar', 'city': 'Example'})}"
        login_page = self.client.get(self.url, {"name": "Corner Bar", "city": "Example"}, follow=True)
        self.assertEqual(parse_qs(urlsplit(login_page.redirect_chain[0][0]).query)["next"], [target])
        self.assertContains(login_page, urlencode({"next": target}))
        self.assertContains(self.client.get(reverse("recovery"), {"next": target}), 'name="next"')
        self.client.post(reverse("recovery"), {"email": "new@example.com", "next": target})
        link = re.search(r"https?://\S+", mail.outbox[0].body)[0]
        token = parse_qs(urlsplit(link).query)["token"][0]
        response = self.client.post(reverse("recovery_login"), {"token": token})
        self.assertRedirects(response, target)
        self.assertEqual(self.client.get(target).context["form"]["name"].value(), "Corner Bar")

    def test_the_wallet_login_comes_back_too(self):
        self.client.force_login(self.user)
        target = f"{self.url}?name=Bar"
        self.assertRedirects(self.client.get(reverse("shops"), {"next": target}), target)

    def test_no_way_out_of_the_site(self):
        for target in ("https://evil.example/", "//evil.example/", "/\\evil.example", "javascript:alert(1)"):
            self.client.logout()
            mail.outbox.clear()
            reset_limits()
            self.client.post(reverse("recovery"), {"email": "new@example.com", "next": target})
            token = parse_qs(urlsplit(re.search(r"https?://\S+", mail.outbox[0].body)[0]).query)["token"][0]
            self.assertRedirects(self.client.post(reverse("recovery_login"), {"token": token}), reverse("shops"), target_status_code=200)
            self.assertEqual(self.client.get(reverse("shops"), {"next": target}).status_code, 200)


class RequestLimits(TestCase):
    @staticmethod
    def asked(name="steady"):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext


        with CaptureQueriesContext(connection) as queries:
            ratelimit.over_limit(name, 10000, 600)
        return len(queries)

    def test_a_request_is_counted_with_the_same_queries_but_for_the_tidy_up_and_the_new_window(self):
        clock = _StoppedClock()
        with patch("config.ratelimit.time", clock):
            with patch("config.ratelimit.secrets.randbelow", return_value=1):
                self.asked()  # the first of a window makes the counter
                usual = self.asked()
                self.assertEqual({self.asked() for _ in range(100)}, {usual})
                # A new window makes its counter: three queries more (savepoint, insert, release)
                clock.now += 600
                self.assertEqual(self.asked(), usual + 3)
            # One request in fifty also deletes the counters of the windows gone: one query more
            with patch("config.ratelimit.secrets.randbelow", return_value=0):
                self.assertEqual(self.asked(), usual + 1)

    def test_steady_limits_ask_the_same_queries_on_every_request(self):
        with steady_limits():
            self.asked()  # the first of a window makes the counter
            self.assertEqual(len({self.asked() for _ in range(300)}), 1)
            # The limit still counts
            self.assertEqual(max(RequestCount.objects.values_list("count", flat=True)), 301)

    def test_the_cleanup_never_takes_the_count_just_made(self):

        later = timezone.now() + timedelta(days=30)
        with patch("config.ratelimit.secrets.randbelow", return_value=0), patch("django.utils.timezone.now", return_value=later):
            self.assertFalse(ratelimit.over_limit("cleanup", 5, 86400))
            self.assertFalse(ratelimit.over_limit("cleanup", 5, 86400))


"""The cookie that lets a browser that signed in before past the per-account limit of the admin sign-in (config/urls.py):
it must hold only for the account it was made for, and never for a stranger who can produce something that looks like it."""
import time
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import signing
from django.test import RequestFactory, TestCase

from .urls import KNOWN_DEVICE_AGE, KNOWN_DEVICE_COOKIE, KNOWN_DEVICE_SALT, known_device


class KnownDevice(TestCase):
    def setUp(self):
        self.boss = get_user_model().objects.create_user("boss", password="a long password", is_staff=True)

    def request(self, name="boss", cookie=None):
        request = RequestFactory().post("/admin/login/", {"username": name})
        if cookie is not None:
            request.COOKIES[KNOWN_DEVICE_COOKIE] = cookie
        return request

    def cookie(self, user=None, **changes):
        user = user or self.boss
        return signing.dumps({"user": user.pk, "auth": user.get_session_auth_hash(), **changes}, salt=KNOWN_DEVICE_SALT)

    def test_the_cookie_made_for_the_account_passes(self):
        self.assertTrue(known_device(self.request(cookie=self.cookie())))

    def test_no_cookie_or_one_that_is_not_ours(self):
        self.assertFalse(known_device(self.request()))
        self.assertFalse(known_device(self.request(cookie="")))
        self.assertFalse(known_device(self.request(cookie="forged")))
        self.assertFalse(known_device(self.request(cookie=signing.dumps({"user": self.boss.pk}, salt="another-salt"))))

    def test_a_name_that_is_not_an_account_passes_nothing(self):
        self.assertFalse(known_device(self.request(name="nobody", cookie=self.cookie())))
        self.assertFalse(known_device(self.request(name="", cookie=self.cookie())))

    def test_a_signed_value_that_is_not_a_record_passes_nothing(self):
        for value in ([self.boss.pk], "boss", 7, None):
            cookie = signing.dumps(value, salt=KNOWN_DEVICE_SALT)
            self.assertFalse(known_device(self.request(cookie=cookie)), value)

    def test_the_cookie_of_another_account_does_not_pass(self):
        other = get_user_model().objects.create_user("other", password="another password", is_staff=True)
        self.assertFalse(known_device(self.request(name="other", cookie=self.cookie())))
        self.assertTrue(known_device(self.request(name="other", cookie=self.cookie(other))))

    def test_a_new_password_a_closed_account_or_lost_staff_ends_it(self):
        old = self.cookie()
        self.assertTrue(known_device(self.request(cookie=old)))
        self.boss.set_password("a new long password")
        self.boss.save()
        self.assertFalse(known_device(self.request(cookie=old)))
        fresh = self.cookie()
        self.boss.is_staff = False
        self.boss.save()
        self.assertFalse(known_device(self.request(cookie=fresh)))
        self.boss.is_staff, self.boss.is_active = True, False
        self.boss.save()
        self.assertFalse(known_device(self.request(cookie=fresh)))

    def test_an_old_cookie_expires(self):
        cookie = self.cookie()
        with patch("django.core.signing.time.time", return_value=time.time() + KNOWN_DEVICE_AGE + 10):
            self.assertFalse(known_device(self.request(cookie=cookie)))

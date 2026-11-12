"""Error paths and ceilings that the feature tests do not reach: a database that fails under the certificates, the
limits of the staff registers, two phones with one code, the cache of the public list starting from nothing."""
from datetime import date
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import DatabaseError
from django.db.models import QuerySet
from django.test import TestCase
from django.utils import timezone

from . import certified, public_cache
from .models import Shop, StaffRegister
from .tests import reset_limits, steady_limits


class CertificatesWithoutDatabase(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user("owner")
        self.shop = Shop.objects.create(name="Bar", owner=self.owner, latitude=41.9, longitude=12.5, lightning=True)

    def test_the_question_whether_a_user_owns_a_certified_shop_is_never_an_error(self):
        Shop.objects.filter(pk=self.shop.pk).update(certified_month=date(2026, 9, 1))
        self.assertTrue(certified.owns_certified_shop(self.owner))
        with patch.object(QuerySet, "exists", side_effect=DatabaseError("down")):
            self.assertFalse(certified.owns_certified_shop(self.owner))

    def test_a_refresh_that_cannot_write_certifies_nobody_and_says_so(self):
        with patch.object(QuerySet, "update", side_effect=DatabaseError("down")), \
                self.assertLogs("shops.certified", "WARNING") as logged:
            self.assertEqual(certified.refresh(), 0)
        self.assertIn("DatabaseError", logged.output[0])
        # What the log says is the kind of error, never its text
        self.assertNotIn("down", logged.output[0])

    def test_a_moment_that_never_happened_is_in_the_oldest_band(self):
        self.assertEqual(certified.band(None, timezone.now()), certified.OLDER)


class StaffLimits(TestCase):
    def setUp(self):
        reset_limits()
        self.enterContext(steady_limits())
        self.owner = get_user_model().objects.create(username="owner")
        self.shop = Shop.objects.create(owner=self.owner, name="Bar", lightning_address="till@ln.example.org")
        self.client.force_login(self.owner)
        self.manage = f"/en/shops/registers/{self.shop.id}/"

    @patch("shops.staff.REGISTER_CHANGES", 2)
    def test_adding_registers_too_fast_is_refused_and_adds_nothing(self):
        for name in ("A", "B"):
            self.assertEqual(self.client.post(self.manage, {"name": name}).status_code, 302)
        response = self.client.post(self.manage, {"name": "C"})
        self.assertContains(response, "Too many changes in a short time")
        self.assertEqual(list(StaffRegister.objects.order_by("name").values_list("name", flat=True)), ["A", "B"])

    def test_a_name_of_nothing_but_spaces_adds_no_register(self):
        for name in ("", "   ", "\t\n"):
            response = self.client.post(self.manage, {"name": name})
            self.assertEqual(response.status_code, 200, repr(name))
        self.assertFalse(StaffRegister.objects.exists())

    @patch("shops.staff.REGISTER_CHANGES", 2)
    def test_a_new_code_asked_too_fast_keeps_the_old_one(self):
        register = StaffRegister.objects.create(shop=self.shop, name="Anna")
        from .staff import new_code
        new_code(register)
        register.refresh_from_db()
        old = register.code
        address = f"{self.manage}{register.id}/"
        self.client.post(address)
        self.client.post(address)
        register.refresh_from_db()
        kept = register.code
        response = self.client.post(address, follow=True)
        self.assertContains(response, "Too many changes in a short time")
        register.refresh_from_db()
        self.assertEqual(register.code, kept)
        self.assertNotEqual(kept, old)

    def test_two_phones_pressing_at_once_open_the_register_only_once(self):
        register = StaffRegister.objects.create(shop=self.shop, name="Anna")
        from .staff import new_code
        new_code(register)
        register.refresh_from_db()
        # The other phone uses the code between this one's lookup and its update (here, while the limit is counted)
        def other_phone(*args, **kwargs):
            StaffRegister.objects.filter(pk=register.pk).update(code="", opened=timezone.now())
            return False

        with patch("shops.staff.ratelimit.over_limit", side_effect=other_phone):
            response = self.client_class().post("/en/shops/register/open/", {"code": register.code})
        self.assertEqual(response.status_code, 404)
        self.assertContains(response, "This code opens nothing", status_code=404)


class PublicListGeneration(TestCase):
    def test_from_an_empty_cache_a_generation_is_made_and_kept(self):
        cache.delete(public_cache.GENERATION)
        first = public_cache.generation()
        self.assertRegex(first, r"^[0-9a-f]{8}$")
        self.assertEqual(public_cache.generation(), first)

    def test_two_requests_starting_together_agree_on_the_generation_that_won(self):
        cache.delete(public_cache.GENERATION)
        real_add = cache.add

        def lost_the_race(key, value, timeout=None):
            # The other request writes first: ours is not stored
            real_add(key, "winner0", timeout)
            return real_add(key, value, timeout)

        with patch.object(cache, "add", side_effect=lost_the_race):
            self.assertEqual(public_cache.generation(), "winner0")

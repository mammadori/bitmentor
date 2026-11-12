"""The short cache of the public list of shops (shops/public_cache.py): what is kept is the same for everybody, and a
saved shop shows at the next request."""
import re
from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.template import Context, Template
from django.db import connection, transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from . import lightning_watch, public_cache
from .models import Shop, Trainee
from .tests import admin_session


def shop_list(page):
    return re.search(r'<ol id="shop-list".*?</ol>', page.content.decode(), re.S).group(0)


@override_settings(PUBLIC_LIST_SECONDS=60)
class PublicList(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = get_user_model().objects.create_user("owner-of-the-bar", email="owner@b.example")
        self.shop = Shop.objects.create(name="Bar", owner=self.owner, latitude=41.9, longitude=12.5, lightning=True)
        Shop.objects.filter(pk=self.shop.pk).update(certified_month=date(2026, 9, 1), lightning_address="bar@b.example",
                                                    lightning_worked=timezone.now() - timedelta(days=2))
        Trainee.objects.create(user=self.owner, base_earned=timezone.now())
        self.other = get_user_model().objects.create_user("someone-else")

    def test_json_is_public_for_half_a_minute(self):
        response = self.client.get("/shops.json")
        # Browsers keep it less than the server, where a change renews it
        self.assertEqual(response["Cache-Control"], "public, max-age=30")
        self.assertIn("Cookie", response["Vary"])
        self.assertIn("Accept-Language", response["Vary"])

    def test_json_is_kept_and_a_save_renews_it(self):
        self.assertEqual(self.client.get("/shops.json").json()["shops"][0]["name"], "Bar")
        # An update that skips the signals and writes what the list does not read waits for the minute to pass
        with connection.cursor() as cursor:
            cursor.execute(f"UPDATE {Shop._meta.db_table} SET name = 'Pub'")
        with self.captureOnCommitCallbacks(execute=True):
            Shop.objects.filter(pk=self.shop.pk).update(osm_notes="seen")
        self.assertEqual(self.client.get("/shops.json").json()["shops"][0]["name"], "Bar")
        # A queryset update() of a public column renews it, as a save() does
        with self.captureOnCommitCallbacks(execute=True):
            Shop.objects.filter(pk=self.shop.pk).update(city="Rome")
        self.assertEqual(self.client.get("/shops.json").json()["shops"][0]["name"], "Pub")
        shop = Shop.objects.get(pk=self.shop.pk)
        shop.name = "Tavern"
        with self.captureOnCommitCallbacks(execute=True):
            shop.save()
        self.assertEqual(self.client.get("/shops.json").json()["shops"][0]["name"], "Tavern")

    def test_map_list_is_kept_and_a_delete_renews_it(self):
        self.assertIn("Bar", shop_list(self.client.get("/en/shops/map/")))
        with self.captureOnCommitCallbacks(execute=True):
            Shop.objects.create(name="Second", latitude=41.8, longitude=12.4).delete()
            Shop.objects.filter(pk=self.shop.pk).update(name="Pub")
        self.assertIn("Pub", shop_list(self.client.get("/en/shops/map/")))

    def test_json_is_the_same_for_owner_account_and_visitor(self):
        # The owner of the certified shop asks first: the visitor gets those very bytes, and they hold no account
        self.client.force_login(self.owner)
        owner = self.client.get("/shops.json").content
        self.client.logout()
        self.client.force_login(self.other)
        signed_in = self.client.get("/shops.json").content
        self.client.logout()
        visitor = self.client.get("/shops.json").content
        self.assertEqual(owner, visitor)
        self.assertEqual(signed_in, visitor)
        for private in (b"owner-of-the-bar", b"owner@b.example", b"someone-else", b"2026-09"):
            self.assertNotIn(private, visitor)

    def test_map_page_is_rendered_for_each_visitor(self):
        self.client.force_login(self.owner)
        owner = self.client.get("/en/shops/map/")
        self.assertIn("no-store", owner["Cache-Control"])
        self.client.logout()
        visitor = self.client.get("/en/shops/map/")
        # Only the list is shared, never the page around it
        self.assertNotIn("public", visitor.get("Cache-Control", ""))
        self.assertEqual(shop_list(owner), shop_list(visitor))
        self.assertIn("Certified shop", shop_list(visitor))
        for private in ("owner-of-the-bar", "owner@b.example"):
            self.assertNotIn(private, visitor.content.decode())

    def test_each_language_has_its_list(self):
        english = shop_list(self.client.get("/en/shops/map/"))
        italian = shop_list(self.client.get("/it/shops/map/"))
        self.assertIn(f"/en/shops/s/{self.shop.pk}/", english)
        self.assertIn(f"/it/shops/s/{self.shop.pk}/", italian)
        self.assertNotIn("/en/", italian)

    def test_map_gives_the_shops_to_the_community_templates(self):
        # A community's map template counts the shops and lists the places of a year by name and town
        Shop.objects.create(name="Old inn", city="Hilltown", external_id="77", latitude=41.7, longitude=12.3,
                            closed_on=date(2020, 5, 1))
        template = Template('{{ shops|length }}|{% for shop in shops %}{% if shop.external_id in keys or '
                            'shop.id|stringformat:"s" in keys %}<a href="{% url \'shop_detail\' shop.id %}">'
                            '{{ shop.name }}</a>, {{ shop.city }};{% endif %}{% endfor %}')
        for _round in range(2):
            # The second page comes from the cache: the same values
            shops = self.client.get("/en/shops/map/").context["shops"]
            page = template.render(Context({"shops": shops, "keys": ["77", str(self.shop.pk)]}))
            self.assertEqual(page, f'2|<a href="/en/shops/s/{self.shop.pk}/">Bar</a>, ;'
                                   f'<a href="/en/shops/s/{Shop.objects.get(external_id="77").pk}/">Old inn</a>, Hilltown;')
        # Public values only, never the owner or the Lightning address
        self.assertEqual(set(shops[0]), {"id", "external_id", "name", "city", "closed_on", "discount"})
        self.assertNotIn("bar@b.example", str(shops))

    def test_admin_actions_renew_it(self):
        self.assertTrue(self.client.get("/shops.json").json()["shops"][0]["certified"])
        admin_session(self.client)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post("/admin/shops/shop/", {"action": "revoke_certificate", "_selected_action": [str(self.shop.pk)]})
        self.client.logout()
        self.assertFalse(self.client.get("/shops.json").json()["shops"][0]["certified"])
        self.assertNotIn("Certified shop", shop_list(self.client.get("/en/shops/map/")))

    def test_the_lightning_guardian_renews_it(self):
        # A shop no longer watched loses its Lightning status with an update() (shops/lightning_watch.py)
        with connection.cursor() as cursor:
            cursor.execute(f"UPDATE {Shop._meta.db_table} SET suspended = TRUE, lightning_status = 'ok'")
        before = public_cache.generation()
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(lightning_watch.forget_unwatched(), 1)
        self.assertNotEqual(public_cache.generation(), before)

    @override_settings(PUBLIC_LIST_SECONDS=0)
    def test_off(self):
        self.client.get("/shops.json")
        Shop.objects.filter(pk=self.shop.pk).update(name="Pub")
        self.assertEqual(self.client.get("/shops.json").json()["shops"][0]["name"], "Pub")


@override_settings(PUBLIC_LIST_SECONDS=60)
class Bookkeeping(TestCase):
    """Saves of what the public list does not show keep it: the guardian probes every shop, often."""

    def setUp(self):
        cache.clear()
        self.owner = get_user_model().objects.create_user("owner-of-the-bar")
        self.shop = Shop.objects.create(name="Bar", owner=self.owner, latitude=41.9, longitude=12.5,
                                        lightning_address="bar@b.example")

    def probe(self, result):
        before = public_cache.generation()
        with self.captureOnCommitCallbacks(execute=True):
            lightning_watch.record(self.shop.pk, "bar@b.example", result)
        return public_cache.generation() != before

    def test_a_probe_renews_the_list_only_when_the_day_it_last_worked_changes(self):
        # The first time it works: the icons appear
        self.assertTrue(self.probe(None))
        # Again today, or a failure: the bands stay as they are
        self.assertFalse(self.probe(None))
        self.assertFalse(self.probe("no_answer"))
        # It last worked yesterday: today it may be another band
        Shop.objects.filter(pk=self.shop.pk).guarded_update(lightning_worked=timezone.now() - timedelta(days=1))
        self.assertTrue(self.probe(None))

    def test_a_failed_save_does_not_keep_the_mark(self):
        from unittest.mock import patch

        self.probe(None)
        marked = []
        mark = public_cache.same_list
        with patch.object(public_cache, "same_list", lambda shop: marked.append(mark(shop)) or shop), \
                patch.object(Shop, "save", side_effect=RuntimeError("database")):
            with self.assertRaises(RuntimeError):
                lightning_watch.record(self.shop.pk, "bar@b.example", None)
        [shop] = marked
        self.assertNotIn(public_cache.SAME_LIST, shop.__dict__)

    def save(self, **kwargs):
        before = public_cache.generation()
        with self.captureOnCommitCallbacks(execute=True):
            self.shop.save(**kwargs)
        return public_cache.generation() != before

    def test_a_save_of_bookkeeping_columns_keeps_it_and_of_a_public_one_renews_it(self):
        self.shop.lightning_failures = 2
        self.assertFalse(self.save(update_fields=["lightning_failures", "lightning_checked"]))
        self.shop.sign_printed = timezone.now()
        self.assertFalse(self.save(update_fields=["sign_printed"]))
        self.shop.lightning_worked = timezone.now()
        self.assertTrue(self.save(update_fields=["lightning_worked", "lightning_checked"]))
        # A save of every column renews it
        self.assertTrue(self.save())

    def test_ticks_of_the_first_day_keep_it(self):
        self.client.force_login(self.owner)
        before = public_cache.generation()
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(f"/en/shops/first-day/{self.shop.id}/", {"step": "sign", "done": "1"})
        self.assertEqual(public_cache.generation(), before)

    def test_the_bookkeeping_columns_are_not_in_the_list(self):
        # If one of them were read by /shops.json or the map, keeping the list after its save would show an old value
        self.assertNotIn("lightning_worked", public_cache.NOT_PUBLIC)
        fields = {field.name for field in Shop._meta.get_fields()}
        self.assertLessEqual(public_cache.NOT_PUBLIC, fields)


@override_settings(PUBLIC_LIST_SECONDS=60)
class AfterCommit(TestCase):
    """The list is renewed after the commit: renewed inside the transaction, a request in between would build it from
    the old rows and keep it under the new generation."""

    def setUp(self):
        cache.clear()
        self.shop = Shop.objects.create(name="Bar", latitude=41.9, longitude=12.5)

    def test_a_change_in_a_transaction_renews_it_at_the_commit(self):
        before = public_cache.generation()
        with self.captureOnCommitCallbacks() as callbacks:
            with transaction.atomic():
                self.shop.name = "Pub"
                self.shop.save()
                self.assertEqual(public_cache.generation(), before)
        self.assertEqual(public_cache.generation(), before)
        self.assertEqual(len(callbacks), 1)
        callbacks[0]()
        self.assertNotEqual(public_cache.generation(), before)

    def test_a_transaction_rolled_back_keeps_it(self):
        before = public_cache.generation()
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            try:
                with transaction.atomic():
                    self.shop.delete()
                    raise RuntimeError
            except RuntimeError:
                pass
        self.assertEqual(callbacks, [])
        self.assertEqual(public_cache.generation(), before)

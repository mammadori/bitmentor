"""New shops wait for an approval (shops/approval.py): off the map, the lists and the counts, with the register working,
until someone of the community approves them on /admin/today/ or in /admin/."""
from unittest.mock import patch

from django.contrib.admin.models import LogEntry
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.test import TestCase

from config.community import community

from . import dashboard, journey
from .directory import public_shops, statistics
from .models import RequestCount, Shop


def approval_off():
    return patch("config.features.community", new=lambda: {**community(), "features": {"approval": False}})


class NewShopWaits(TestCase):
    def setUp(self):
        cache.clear()
        RequestCount.objects.all().delete()
        self.owner = get_user_model().objects.create(username="owner")
        self.client.force_login(self.owner)
        probe = patch("shops.views.refusal", return_value=None)
        probe.start()
        self.addCleanup(probe.stop)
        told = patch("shops.approval.tell_moderators")
        self.told = told.start()
        self.addCleanup(told.stop)

    def new(self):
        self.client.post("/en/shops/new/", {"name": "Bar", "latitude": "41.9", "longitude": "12.5",
                                            "lightning_address": "mario@example.org"})
        return Shop.objects.get()

    def test_a_new_shop_waits_off_the_map_with_its_register(self):
        shop = self.new()
        self.assertTrue(shop.needs_approval)
        self.assertTrue(shop.has_register)
        self.assertFalse(public_shops().exists())
        self.assertEqual(statistics()["shops"], 0)
        self.assertNotContains(self.client.get("/shops.json"), str(shop.id))
        page = self.client.get(f"/en/shops/s/{shop.id}/")
        self.assertEqual(page["X-Robots-Tag"], "noindex, nofollow")
        self.assertTrue(page.context["reduced"])
        self.assertEqual(journey.card(shop, None), {"on_map": False, "why": "approval"})
        self.told.assert_called_once()

    def test_the_owner_reads_why(self):
        self.new()
        self.assertContains(self.client.get("/en/shops/"), "the community looks at every new shop first")

    def test_with_the_approval_off_it_goes_on_the_map_at_once(self):
        with approval_off():
            shop = self.new()
        self.assertFalse(shop.needs_approval)
        self.assertTrue(public_shops().filter(pk=shop.pk).exists())
        self.told.assert_not_called()

    def test_a_shop_nobody_approved_is_never_on_the_map(self):
        shop = Shop.objects.create(name="Old", latitude=41.9, longitude=12.5, approved_on=None)
        self.assertFalse(public_shops().filter(pk=shop.pk).exists())
        self.assertNotContains(self.client.get("/shops.json"), str(shop.id))


class Approve(TestCase):
    def setUp(self):
        self.shop = Shop.objects.create(name="Bar", latitude=41.9, longitude=12.5, approved_on=None)
        self.staff = get_user_model().objects.create(username="staff", is_staff=True)

    def grant(self, *codes):
        from config.urls import ADMIN_SESSION

        self.staff.user_permissions.add(*Permission.objects.filter(content_type__app_label="shops", codename__in=codes))
        self.client.force_login(self.staff)
        session = self.client.session
        session[ADMIN_SESSION] = True
        session.save()

    def test_today_lists_it_with_the_button(self):
        self.grant("view_shop", "change_shop")
        self.assertIn("approve", [item.code for item in dashboard.summary()["items"]])
        self.assertContains(self.client.get("/admin/today/"), f'action="/admin/today/approve/{self.shop.id}/"')

    def test_one_click_puts_it_on_the_map_and_in_the_history(self):
        self.grant("view_shop", "change_shop")
        response = self.client.post(f"/admin/today/approve/{self.shop.id}/")
        self.assertRedirects(response, "/admin/today/", fetch_redirect_response=False)
        self.shop.refresh_from_db()
        self.assertFalse(self.shop.needs_approval)
        self.assertEqual(self.shop.approved_by, "staff")
        self.assertTrue(public_shops().filter(pk=self.shop.pk).exists())
        self.assertEqual(LogEntry.objects.get().object_id, str(self.shop.pk))

    def test_only_who_may_change_shops_approves(self):
        self.grant("view_shop")
        self.assertNotContains(self.client.get("/admin/today/"), "/admin/today/approve/")
        self.assertEqual(self.client.post(f"/admin/today/approve/{self.shop.id}/").status_code, 403)
        self.shop.refresh_from_db()
        self.assertTrue(self.shop.needs_approval)

    def test_get_does_nothing(self):
        self.grant("view_shop", "change_shop")
        self.assertEqual(self.client.get(f"/admin/today/approve/{self.shop.id}/").status_code, 405)

    def test_the_admin_action(self):
        self.grant("view_shop", "change_shop")
        self.client.post("/admin/shops/shop/", {"action": "approve_shops", "_selected_action": [str(self.shop.pk)]})
        self.shop.refresh_from_db()
        self.assertFalse(self.shop.needs_approval)


class Tell(TestCase):
    def test_the_moderators_hear_of_it_after_the_save(self):
        shop = Shop.objects.create(name="Bar", approved_on=None)
        with patch("shops.notifications.deliver_to_moderators", return_value=("x", [])) as deliver, \
                patch("shops.approval.threading.Thread") as thread, self.captureOnCommitCallbacks(execute=True):
            from .approval import tell_moderators
            tell_moderators(shop)
        with patch("shops.notifications.deliver_to_moderators", return_value=("x", [])) as deliver:
            thread.call_args.kwargs["target"]()
        self.assertIn("Bar", deliver.call_args.args[1])
        self.assertIn(f"#approve-{shop.id}", deliver.call_args.args[1])


class ApprovedByWhoeverBringsThem(TestCase):
    def test_an_imported_shop_is_approved_by_the_import(self):
        import json
        import tempfile

        from django.core.management import call_command

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as file:
            json.dump([{"external_id": "7", "name": "Old", "latitude": 41.9, "longitude": 12.5}], file)
        # As in production: the model's own default (none), not the one the tests' runner gives
        field = Shop._meta.get_field("approved_on")
        with patch.object(field, "get_default", lambda: None):
            call_command("import_shops", file.name, stdout=__import__("io").StringIO())
        shop = Shop.objects.get(external_id="7")
        self.assertEqual(shop.approved_by, "import")
        self.assertTrue(public_shops().filter(pk=shop.pk).exists())

    def test_the_model_alone_approves_nothing(self):
        field = Shop._meta.get_field("approved_on")
        with patch.object(field, "get_default", lambda: None):
            self.assertIsNone(Shop(name="x").approved_on)

    def test_a_shop_written_in_the_admin_is_approved_by_its_writer(self):
        from config.urls import ADMIN_SESSION

        staff = get_user_model().objects.create_superuser("boss", password="x")
        self.client.force_login(staff)
        session = self.client.session
        session[ADMIN_SESSION] = True
        session.save()
        field = Shop._meta.get_field("approved_on")
        with patch.object(field, "get_default", lambda: None):
            response = self.client.get("/admin/shops/shop/add/")
            form = response.context["adminform"].form
            data = {name: value for name, value in form.initial.items() if value is not None and not callable(value)}
            data.update({"name": "Written", "discount": 0, "closures-TOTAL_FORMS": 0, "closures-INITIAL_FORMS": 0})
            self.client.post("/admin/shops/shop/add/", data)
        shop = Shop.objects.get(name="Written")
        self.assertEqual(shop.approved_by, "boss")

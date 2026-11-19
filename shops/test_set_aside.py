"""A shop that waits can be set aside (shops/approval.py, `set_aside`): not for the map, not asked about any more. It
stays not approved: off the map, the lists, the counts and OpenStreetMap, with its register working. It can be put back
among the waiting ones, or approved all the same."""
import re
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.admin.models import LogEntry
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.management import call_command
from django.db import connection
from django.test import TestCase
from django.utils import timezone, translation

from config.community import community

from . import approval, dashboard, journey, moderators, osm
from .directory import public_shops, statistics
from .models import Shop
from .test_guard import sign_in_as_staff_session

User = get_user_model()


def approval_off():
    return patch("config.features.community", new=lambda: {**community(), "features": {"approval": False}})


class Base(TestCase):
    def setUp(self):
        self.owner = User.objects.create(username="owner")
        self.shop = Shop.objects.create(name="Trial Bar", owner=self.owner, latitude=41.9, longitude=12.5,
                                        lightning_address="mario@wallet.example", approved_on=None)
        self.staff = User.objects.create(username="staff", is_staff=True)

    def grant(self, *codes, user=None):
        user = user or self.staff
        user.user_permissions.add(*Permission.objects.filter(content_type__app_label="shops", codename__in=codes))
        sign_in_as_staff_session(self.client, user)

    def set_aside(self):
        return self.client.post(f"/admin/today/set-aside/{self.shop.id}/")

    def history(self):
        return list(LogEntry.objects.filter(object_id=str(self.shop.pk)).order_by("pk")
                    .values_list("change_message", flat=True))


class FromTheTodayPage(Base):
    def test_the_button_is_next_to_the_approval(self):
        self.grant("view_shop", "change_shop")
        page = self.client.get("/admin/today/")
        self.assertContains(page, f'action="/admin/today/approve/{self.shop.id}/"')
        self.assertContains(page, f'action="/admin/today/set-aside/{self.shop.id}/"')
        self.assertContains(page, "Not for the map: set aside")

    def test_staff_who_may_change_shops_sets_it_aside(self):
        self.grant("view_shop", "change_shop")
        response = self.set_aside()
        self.assertRedirects(response, "/admin/today/", fetch_redirect_response=False)
        self.shop.refresh_from_db()
        self.assertIsNotNone(self.shop.set_aside_on)
        self.assertEqual(self.shop.set_aside_by, "staff")
        self.assertTrue(self.shop.needs_approval)
        self.assertIsNone(self.shop.approved_on)
        entry = LogEntry.objects.get()
        self.assertEqual((entry.object_id, entry.user_id, entry.change_message),
                         (str(self.shop.pk), self.staff.pk, "Shop set aside: not for the map"))

    def test_a_staff_without_the_right_cannot(self):
        self.grant("view_shop")
        self.assertNotContains(self.client.get("/admin/today/"), "/admin/today/set-aside/")
        self.assertEqual(self.set_aside().status_code, 403)
        self.shop.refresh_from_db()
        self.assertIsNone(self.shop.set_aside_on)

    def test_a_moderator_of_the_group_cannot(self):
        moderators.sync()
        from django.contrib.auth.models import Group

        moderator = User.objects.create(username="mod", is_staff=True)
        moderator.groups.add(Group.objects.get(name="Moderators"))
        sign_in_as_staff_session(self.client, moderator)
        self.assertEqual(self.client.get("/admin/today/").status_code, 200)
        self.assertNotContains(self.client.get("/admin/today/"), "/admin/today/set-aside/")
        self.assertEqual(self.set_aside().status_code, 403)
        self.shop.refresh_from_db()
        self.assertIsNone(self.shop.set_aside_on)

    def test_a_signed_out_visitor_cannot(self):
        response = self.set_aside()
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response["Location"])
        self.shop.refresh_from_db()
        self.assertIsNone(self.shop.set_aside_on)

    def test_get_does_nothing(self):
        self.grant("view_shop", "change_shop")
        self.assertEqual(self.client.get(f"/admin/today/set-aside/{self.shop.id}/").status_code, 405)
        self.shop.refresh_from_db()
        self.assertIsNone(self.shop.set_aside_on)

    def test_without_the_csrf_token_it_is_refused(self):
        from django.test import Client

        client = Client(enforce_csrf_checks=True)
        self.grant("view_shop", "change_shop")
        client.cookies = self.client.cookies
        self.assertEqual(client.post(f"/admin/today/set-aside/{self.shop.id}/").status_code, 403)
        self.shop.refresh_from_db()
        self.assertIsNone(self.shop.set_aside_on)

    def test_two_clicks_write_one_history_line(self):
        self.grant("view_shop", "change_shop")
        self.set_aside()
        first = Shop.objects.get(pk=self.shop.pk).set_aside_on
        self.set_aside()
        self.assertEqual(self.history(), ["Shop set aside: not for the map"])
        self.assertEqual(Shop.objects.get(pk=self.shop.pk).set_aside_on, first)
        # And the function itself, with the shop read before the other click
        stale = Shop.objects.get(pk=self.shop.pk)
        stale.set_aside_on = None
        self.assertFalse(approval.set_aside(stale, self.staff))
        self.assertEqual(len(self.history()), 1)

    def test_an_approved_shop_is_not_set_aside(self):
        Shop.objects.filter(pk=self.shop.pk).update(approved_on=timezone.now())
        self.grant("view_shop", "change_shop")
        self.set_aside()
        self.shop.refresh_from_db()
        self.assertIsNone(self.shop.set_aside_on)
        self.assertEqual(self.history(), [])

    def test_a_shop_that_looks_like_a_copy_is_set_aside_without_a_box(self):
        Shop.objects.create(name="Trial Bar", latitude=41.9, longitude=12.5, approved_on=timezone.now())
        self.assertTrue(approval.held(Shop.objects.filter(pk=self.shop.pk)))
        self.grant("view_shop", "change_shop")
        self.set_aside()
        self.shop.refresh_from_db()
        self.assertIsNotNone(self.shop.set_aside_on)
        self.assertTrue(self.shop.needs_approval)

    def test_with_the_approval_off_a_shop_left_waiting_can_still_be_set_aside(self):
        self.grant("view_shop", "change_shop")
        with approval_off():
            self.assertContains(self.client.get("/admin/today/"), f"/admin/today/set-aside/{self.shop.id}/")
            self.set_aside()
        self.shop.refresh_from_db()
        self.assertIsNotNone(self.shop.set_aside_on)


class NoLongerAsked(Base):
    def setUp(self):
        super().setUp()
        approval.set_aside(self.shop, self.staff)

    def test_out_of_waiting_the_list_of_the_day_and_its_counts(self):
        self.assertFalse(approval.waiting().filter(pk=self.shop.pk).exists())
        found = dashboard.summary()
        self.assertNotIn(self.shop.pk, [item.shop.pk for item in found["items"] if item.shop])
        self.assertEqual(found["counts"]["to_look"], 0)
        self.grant("view_shop", "change_shop")
        page = self.client.get("/admin/today/")
        self.assertNotContains(page, f"approve-{self.shop.id}")
        self.assertNotContains(page, "Trial Bar")

    def test_out_of_the_morning_summary(self):
        out = StringIO()
        try:
            call_command("dashboard", stdout=out)
        except SystemExit:
            pass
        self.assertNotIn("Trial Bar", out.getvalue())

    def test_out_of_the_volunteers_area(self):
        from team.area import waiting_items

        self.assertEqual(waiting_items(), [])

    def test_still_off_the_map_the_json_the_lists_and_openstreetmap(self):
        self.assertFalse(public_shops().filter(pk=self.shop.pk).exists())
        self.assertEqual(statistics()["shops"], 0)
        self.assertNotContains(self.client.get("/shops.json"), str(self.shop.id))
        self.assertIsNone(osm.standing(self.shop, timezone.localdate()))
        self.assertEqual(journey.card(self.shop, None), {"on_map": False, "why": "approval"})
        page = self.client.get(f"/en/shops/s/{self.shop.id}/")
        self.assertEqual(page["X-Robots-Tag"], "noindex, nofollow")
        self.assertTrue(page.context["reduced"])

    def test_the_register_still_opens(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(f"/en/shops/register/{self.shop.id}/").status_code, 200)

    def test_the_bulk_approval_of_the_waiting_ones_leaves_it(self):
        other = Shop.objects.create(name="Real Bar", approved_on=None)
        self.assertEqual(approval.approve(approval.waiting(), self.staff), 1)
        other.refresh_from_db()
        self.shop.refresh_from_db()
        self.assertFalse(other.needs_approval)
        self.assertTrue(self.shop.needs_approval)


class PutBackAndApprove(Base):
    def setUp(self):
        super().setUp()
        approval.set_aside(self.shop, self.staff)

    def test_put_back_from_its_page_it_waits_and_is_asked_again(self):
        self.grant("view_shop", "change_shop")
        page = self.client.get(f"/admin/shops/shop/{self.shop.pk}/change/")
        self.assertContains(page, f'action="/admin/shops/shop/{self.shop.pk}/put-back/"')
        self.assertContains(page, "by staff: not for the map")
        response = self.client.post(f"/admin/shops/shop/{self.shop.pk}/put-back/")
        self.assertRedirects(response, f"/admin/shops/shop/{self.shop.pk}/change/", fetch_redirect_response=False)
        self.shop.refresh_from_db()
        self.assertEqual((self.shop.set_aside_on, self.shop.set_aside_by), (None, ""))
        self.assertTrue(approval.waiting().filter(pk=self.shop.pk).exists())
        self.assertIn("approve", [item.code for item in dashboard.summary()["items"]])
        self.assertContains(self.client.get("/admin/today/"), f"/admin/today/set-aside/{self.shop.id}/")
        self.assertEqual(self.history(), ["Shop set aside: not for the map", "Shop put back among the waiting ones"])
        self.assertEqual(LogEntry.objects.latest("pk").user_id, self.staff.pk)
        # Twice: nothing more
        self.client.post(f"/admin/shops/shop/{self.shop.pk}/put-back/")
        self.assertEqual(len(self.history()), 2)

    def test_the_enter_key_of_the_shops_form_saves_and_never_puts_it_back(self):
        """The first submit control inside the shop's form is the admin's own save: Enter in a field presses it. The
        put-back is a form of its own, outside the shop's form."""
        self.grant("view_shop", "change_shop")
        page = self.client.get(f"/admin/shops/shop/{self.shop.pk}/change/").content.decode()
        start = page.index('id="shop_form"')
        form = page[start:page.index("</form>", start)]
        first = re.search(r'<(?:button|input)[^>]*type="submit"[^>]*>', form).group(0)
        self.assertIn('name="_save"', first)
        self.assertNotIn("put-back", form)
        self.assertNotIn("formaction", form)
        outside = page[:start]
        self.assertIn(f'action="/admin/shops/shop/{self.shop.pk}/put-back/"', outside)
        self.assertIn('name="csrfmiddlewaretoken"', outside[outside.index("put-back"):])

    def test_put_back_needs_the_csrf_token(self):
        from django.test import Client

        self.grant("view_shop", "change_shop")
        client = Client(enforce_csrf_checks=True)
        client.cookies = self.client.cookies
        self.assertEqual(client.post(f"/admin/shops/shop/{self.shop.pk}/put-back/").status_code, 403)
        self.shop.refresh_from_db()
        self.assertIsNotNone(self.shop.set_aside_on)

    def test_put_back_only_for_who_may_change_shops_by_post(self):
        self.grant("view_shop")
        self.assertNotContains(self.client.get(f"/admin/shops/shop/{self.shop.pk}/change/"), "put-back")
        self.assertEqual(self.client.post(f"/admin/shops/shop/{self.shop.pk}/put-back/").status_code, 403)
        self.grant("change_shop")
        self.assertEqual(self.client.get(f"/admin/shops/shop/{self.shop.pk}/put-back/").status_code, 405)
        self.shop.refresh_from_db()
        self.assertIsNotNone(self.shop.set_aside_on)
        self.client.logout()
        self.client.post(f"/admin/shops/shop/{self.shop.pk}/put-back/")
        self.shop.refresh_from_db()
        self.assertIsNotNone(self.shop.set_aside_on)

    def test_approved_after_set_aside_without_putting_it_back(self):
        with patch("shops.approval.tell_owner") as told:
            self.grant("view_shop", "change_shop")
            self.client.post(f"/admin/today/approve/{self.shop.id}/")
        self.shop.refresh_from_db()
        self.assertFalse(self.shop.needs_approval)
        self.assertEqual((self.shop.set_aside_on, self.shop.set_aside_by), (None, ""))
        self.assertTrue(public_shops().filter(pk=self.shop.pk).exists())
        told.assert_called_once()

    def test_the_admin_action_approves_it_too(self):
        self.grant("view_shop", "change_shop")
        self.client.post("/admin/shops/shop/", {"action": "approve_shops", "_selected_action": [str(self.shop.pk)]})
        self.shop.refresh_from_db()
        self.assertFalse(self.shop.needs_approval)
        self.assertIsNone(self.shop.set_aside_on)

    def test_a_team_key_does_not_approve_it(self):
        found = approval.approve_or_hold(approval.waiting().filter(pk=self.shop.pk), None, by="[team key 1] Ada")
        self.assertEqual(found.approved, 0)
        self.shop.refresh_from_db()
        self.assertTrue(self.shop.needs_approval)

    def test_a_team_key_does_not_approve_a_shop_set_aside_between_the_read_and_the_write(self):
        approval.put_back(self.shop, self.staff)

        def meanwhile(shops):
            # Somebody sets it aside after the team key's page read it as waiting
            approval.set_aside(Shop.objects.get(pk=self.shop.pk), self.staff)
            return {shop.pk: [] for shop in shops}

        with patch("shops.approval_signals.signals_of", side_effect=meanwhile):
            found = approval.approve_or_hold(approval.waiting().filter(pk=self.shop.pk), None, by="[team key 1] Ada")
        self.assertEqual(found.approved, 0)
        self.shop.refresh_from_db()
        self.assertTrue(self.shop.needs_approval)
        self.assertIsNotNone(self.shop.set_aside_on)
        # An administrator approving it explicitly still can
        self.assertEqual(approval.approve(Shop.objects.filter(pk=self.shop.pk), self.staff), 1)


class TheAdminList(Base):
    def setUp(self):
        super().setUp()
        self.other = Shop.objects.create(name="Waiting Bar", approved_on=None)
        approval.set_aside(self.shop, self.staff)
        self.grant("view_shop", "change_shop")

    def test_the_filter(self):
        page = self.client.get("/admin/shops/shop/?set_aside=yes")
        self.assertContains(page, "Trial Bar")
        self.assertNotContains(page, "Waiting Bar")
        page = self.client.get("/admin/shops/shop/?set_aside=no")
        self.assertNotContains(page, "Trial Bar")
        self.assertContains(page, "Waiting Bar")

    def test_set_aside_and_then_approved_by_the_previous_release_is_simply_approved(self):
        # docs/releases.md, "Going back": 0.1.9 approves it and does not know the fields
        Shop.objects.filter(pk=self.shop.pk).update(approved_on=timezone.now(), approved_by="old")
        self.assertNotContains(self.client.get("/admin/shops/shop/?set_aside=yes"), "Trial Bar")
        self.assertContains(self.client.get("/admin/shops/shop/?set_aside=no"), "Trial Bar")
        page = self.client.get(f"/admin/shops/shop/{self.shop.pk}/change/")
        self.assertNotContains(page, "not for the map")
        self.assertNotContains(page, "put-back")

    def test_the_fields_are_read_only_and_nothing_new_is_askable(self):
        from .admin import ShopAdmin

        fields = self.client.get(f"/admin/shops/shop/{self.shop.pk}/change/").context["adminform"].form.fields
        self.assertNotIn("set_aside_on", fields)
        self.assertNotIn("set_aside_by", fields)
        # Shown through one line of text, not as fields: the list's address cannot ask for them (config/admin_secrets.py)
        admin = ShopAdmin(Shop, None)
        self.assertIn("set_aside_on", admin.hidden_fields())
        self.assertEqual(self.client.get("/admin/shops/shop/?set_aside_by__startswith=s").status_code, 400)


class TheOwnerReadsIt(Base):
    def setUp(self):
        super().setUp()
        approval.set_aside(self.shop, self.staff)
        self.client.force_login(self.owner)

    def test_the_panel_says_it_will_not_go_on_the_map(self):
        panel = self.client.get("/en/shops/").content.decode()
        self.assertIn(f'id="set-aside-{self.shop.id}"', panel)
        self.assertNotIn(f'id="approval-waits-{self.shop.id}"', panel)
        self.assertIn("The community set this shop aside: it will not go on the map", panel)
        self.assertIn(f'href="mailto:{community()["email"]}"', panel)
        self.assertNotIn("it will be there once approved", panel)
        self.assertNotIn("come once the community has approved the shop", panel)
        self.assertNotIn("print the sign once the community has approved", panel)

    def test_the_stage_is_never_the_map(self):
        Shop.objects.filter(pk=self.shop.pk).update(first_sale=timezone.now())
        panel = self.client.get("/en/shops/").content.decode()
        self.assertNotIn('data-stage="map"', panel)
        self.assertIn('data-stage="takings"', panel)
        Shop.objects.filter(pk=self.shop.pk).update(first_sale=None)
        self.assertIn('data-stage="trial"', self.client.get("/en/shops/").content.decode())

    def test_the_sign_and_the_card_wait_with_the_honest_sentence(self):
        for url in (f"/en/shops/sign/{self.shop.id}/", f"/en/shops/customer-card/{self.shop.id}/"):
            response = self.client.get(url)
            self.assertTemplateUsed(response, "shops/sign_waiting.html")
            self.assertContains(response, "The community set this shop aside: it will not go on the map")
            self.assertNotContains(response, "once the shop is approved")
            self.assertContains(response, f"/en/shops/card/{self.shop.id}/")

    def test_the_diagnosis_of_a_request_for_help_says_it(self):
        from .diagnosis import check_shop

        lines = []
        run = SimpleNamespace(shop=self.shop, add=lambda code, level, text, detail: lines.append((code, str(text))))
        check_shop(run)
        said = "The community set your shop aside: it will not go on the map. The register still works."
        self.assertIn(("shop_awaiting_approval", said), lines)
        self.assertNotIn("yours is waiting", " ".join(text for _code, text in lines))

    def test_no_message_is_sent(self):
        with patch("shops.approval.tell_owner") as told, patch("shops.notifications.telegram") as telegram:
            approval.put_back(self.shop, self.staff)
            approval.set_aside(self.shop, self.staff)
        told.assert_not_called()
        telegram.assert_not_called()

    def test_in_italian_and_german(self):
        from django.template.loader import render_to_string

        self.assertIn("La comunità ha messo da parte questo negozio", self.client.get("/it/shops/").content.decode())
        for language, words in (("it", "La comunità ha messo da parte questo negozio"),
                                ("de", "Die Gemeinschaft hat dieses Geschäft zurückgestellt")):
            with translation.override(language):
                self.assertIn(words, render_to_string("shops/set_aside_note.html", {"community": community()}))
                self.assertNotEqual(translation.gettext("Not for the map: set aside"), "Not for the map: set aside")


class GoingBack(TestCase):
    """docs/releases.md, «Going back»: the previous image inserts shops without the new columns."""

    def test_the_new_columns_have_their_defaults_in_the_database(self):
        by = Shop._meta.get_field("set_aside_by")
        self.assertEqual(by.db_default, by.get_default())
        self.assertTrue(Shop._meta.get_field("set_aside_on").null)
        with connection.cursor() as cursor:
            columns = {column.name: column for column in connection.introspection.get_table_description(cursor, "shops_shop")}
        self.assertIsNotNone(columns["set_aside_by"].default)
        self.assertTrue(columns["set_aside_on"].null_ok)

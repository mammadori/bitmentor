"""The private part opened by hand by the association to a certified shop, and one Lightning address for one owner
(shops/certified.py, shops/admin.py, shops/forms.py, shops/dashboard.py)."""
from datetime import date, datetime
from datetime import timezone as dt_timezone
from unittest.mock import patch

from django.contrib.admin.models import LogEntry
from django.contrib.auth import get_user_model
from django.test import TestCase

from . import certified, dashboard
from .forms import NewShopForm, WalletForm
from .models import LightningStatus, Shop, Trainee
from .tests import admin_session

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=dt_timezone.utc)
URL = "/admin/shops/shop/"


def make_user(name):
    return get_user_model().objects.create_user(name)


class Approval(TestCase):
    def setUp(self):
        self.owner = make_user("owner")
        self.shop = Shop.objects.create(name="Bar", owner=self.owner, certified_month=date(2026, 9, 1),
                                        lightning_address="bar@wallet.example")
        admin_session(self.client)

    def act(self, action, shop=None):
        self.client.post(URL, {"action": action, "_selected_action": [str((shop or self.shop).pk)]})
        return Shop.objects.get(pk=(shop or self.shop).pk)

    def test_certified_is_not_enough(self):
        self.assertTrue(certified.owns_certified_shop(self.owner))
        self.assertFalse(certified.sees_private_part(self.owner))

    def test_approved_by_hand_written_down_and_revoked(self):
        shop = self.act("approve_private_part")
        self.assertIsNotNone(shop.private_approved)
        self.assertEqual(shop.private_approved_by, "boss")
        self.assertTrue(certified.sees_private_part(self.owner))
        self.assertTrue(LogEntry.objects.filter(object_id=str(shop.pk), change_message="Private part approved").exists())
        page = self.client.get(f"{URL}{shop.pk}/change/").content.decode()
        self.assertIn("Private part approved by", page)
        self.assertNotIn('name="private_approved"', page)
        shop = self.act("revoke_private_part")
        self.assertIsNone(shop.private_approved)
        self.assertEqual(shop.private_approved_by, "")
        self.assertFalse(certified.sees_private_part(self.owner))
        self.assertTrue(LogEntry.objects.filter(object_id=str(shop.pk), change_message="Private part revoked").exists())

    def test_another_owner_starts_without_it(self):
        self.act("approve_private_part")
        heir = make_user("heir")
        page = self.client.get(f"{URL}{self.shop.pk}/change/")
        data = {**page.context["adminform"].form.initial, "owner": heir.pk, "_save": "1"}
        data = {key: ("" if value is None else value) for key, value in data.items()}
        data.update({"closures-TOTAL_FORMS": "0", "closures-INITIAL_FORMS": "0"})
        response = self.client.post(f"{URL}{self.shop.pk}/change/", data)
        self.assertEqual(response.status_code, 302, getattr(response, "context", None) and
                         response.context["adminform"].form.errors)
        shop = Shop.objects.get(pk=self.shop.pk)
        self.assertEqual((shop.owner, shop.private_approved, shop.private_approved_by), (heir, None, ""))
        self.assertFalse(certified.sees_private_part(heir))
        self.assertTrue(LogEntry.objects.filter(object_id=str(shop.pk),
                                                change_message__startswith="Private part revoked: the shop changed").exists())

    def test_any_other_road_to_another_owner_too(self):
        self.act("approve_private_part")
        shop = Shop.objects.get(pk=self.shop.pk)
        shop.owner = make_user("heir")
        shop.save(update_fields=["owner"])
        self.assertIsNone(Shop.objects.get(pk=self.shop.pk).private_approved)
        self.act("approve_private_part")
        Shop.objects.filter(pk=self.shop.pk).update(owner=make_user("third"))
        self.assertIsNone(Shop.objects.get(pk=self.shop.pk).private_approved)
        # The same owner saved again keeps it
        self.act("approve_private_part")
        Shop.objects.get(pk=self.shop.pk).save()
        self.assertIsNotNone(Shop.objects.get(pk=self.shop.pk).private_approved)

    def test_an_address_shared_with_another_owner_closes_it(self):
        self.act("approve_private_part")
        self.assertTrue(certified.sees_private_part(self.owner))
        other = Shop.objects.create(name="Pub", owner=make_user("other"), closed_on=date(2025, 1, 1),
                                    lightning_address=" BAR@wallet.example")
        self.assertFalse(certified.sees_private_part(self.owner))
        found = [(item.code, item.shop) for item in dashboard.summary()["items"] if item.code == "lightning_shared"]
        # The historic shop takes no payments: only the trading one is listed, and the share is seen all the same
        self.assertEqual(found, [("lightning_shared", self.shop)])
        Shop.objects.filter(pk=other.pk).update(lightning_address="pub@wallet.example")
        self.assertTrue(certified.sees_private_part(self.owner))

    def test_a_shop_not_certified_is_not_approved(self):
        other = Shop.objects.create(name="Pub", owner=make_user("other"))
        self.assertIsNone(self.act("approve_private_part", other).private_approved)

    def test_suspended_or_revoked_certificate_closes_it(self):
        self.act("approve_private_part")
        Shop.objects.filter(pk=self.shop.pk).update(suspended=True)
        self.assertFalse(certified.sees_private_part(self.owner))
        Shop.objects.filter(pk=self.shop.pk).update(suspended=False)
        shop = self.act("revoke_certificate")
        self.assertIsNone(shop.private_approved)
        self.assertFalse(certified.sees_private_part(self.owner))

    def test_the_filter_and_the_dashboard_list_the_waiting_shops(self):
        link = f"{URL}{self.shop.pk}/change/"
        self.assertIn(link, self.client.get(f"{URL}?certified=waiting").content.decode())
        codes = [(item.code, item.shop) for item in dashboard.summary()["items"]]
        self.assertIn(("private_waiting", self.shop), codes)
        self.assertContains(self.client.get("/admin/today/"), link)
        self.act("approve_private_part")
        self.assertNotIn(link, self.client.get(f"{URL}?certified=waiting").content.decode())
        self.assertNotIn("private_waiting", [item.code for item in dashboard.summary()["items"]])


@patch("shops.forms.public_host", return_value=True)
class OneOwnerPerAddress(TestCase):
    def setUp(self):
        self.owner = make_user("owner")
        self.shop = Shop.objects.create(name="Bar", owner=self.owner, lightning_address="bar@wallet.example")

    def new(self, owner, address):
        return NewShopForm({"name": "Pub", "lightning_address": address}, owner=owner)

    def test_another_owner_is_refused_however_it_is_written(self, _host):
        for address in ("bar@wallet.example", " Bar@Wallet.Example ", "lightning:BAR@wallet.example"):
            with self.subTest(address=address):
                form = self.new(make_user(f"u{len(address)}"), address)
                self.assertFalse(form.is_valid())
                self.assertEqual(form.errors.as_data()["lightning_address"][0].code, "taken")
                self.assertNotIn("Bar", str(form.errors))

    def test_an_address_stored_with_other_case_counts_too(self, _host):
        Shop.objects.filter(pk=self.shop.pk).update(lightning_address=" BAR@Wallet.example")
        self.assertFalse(self.new(make_user("other"), "bar@wallet.example").is_valid())

    def test_the_same_owner_may_use_it_on_several_shops(self, _host):
        self.assertTrue(self.new(self.owner, "Bar@wallet.example").is_valid())

    def test_a_shop_without_owner_does_not_count(self, _host):
        Shop.objects.create(name="Imported", lightning_address="free@wallet.example")
        self.assertTrue(self.new(make_user("other"), "free@wallet.example").is_valid())

    def test_the_wallet_of_an_existing_shop_too(self, _host):
        other = Shop.objects.create(name="Pub", owner=make_user("other"))
        self.assertFalse(WalletForm({"lightning_address": "bar@wallet.example"}, instance=other).is_valid())
        self.assertTrue(WalletForm({"lightning_address": "pub@wallet.example"}, instance=other).is_valid())

    def test_the_new_shop_page_says_it(self, _host):
        self.client.force_login(make_user("other"))
        page = self.client.post("/en/shops/new/", {"name": "Pub", "lightning_address": "bar@wallet.example"})
        self.assertContains(page, str(NewShopForm.TAKEN))
        self.assertEqual(Shop.objects.filter(name="Pub").count(), 0)


class SharedAddressAndCertificate(TestCase):
    """Data from before the rule: two owners with one address. Neither shop is certified, the dashboard says it."""

    def shop(self, name, owner, address="bar@wallet.example"):
        Trainee.objects.get_or_create(user=owner, defaults={"base_earned": NOW})
        return Shop.objects.create(name=name, owner=owner, lightning_address=address, lightning_status=LightningStatus.OK,
                                   lightning_checked_address=address)

    def test_same_owner_is_certified(self):
        owner = make_user("owner")
        self.shop("Bar", owner), self.shop("Pub", owner)
        self.assertEqual(certified.refresh(now=NOW), 2)
        self.assertNotIn("lightning_shared", [item.code for item in dashboard.summary()["items"]])

    def test_different_owners_are_not_and_are_listed(self):
        bar = self.shop("Bar", make_user("one"))
        pub = self.shop("Pub", make_user("two"), "BAR@wallet.example")
        self.assertEqual(certified.refresh(now=NOW), 0)
        found = [(item.code, item.shop) for item in dashboard.summary()["items"] if item.code == "lightning_shared"]
        self.assertEqual(found, [("lightning_shared", bar), ("lightning_shared", pub)])

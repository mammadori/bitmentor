"""The search of /admin/shops/shop/: what a helper needs when a merchant phones, and never a way to confirm a value the
user cannot see. The owner's recovery email is on no page a moderator opens: only a superuser searches by it."""

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse

from .admin import ShopAdmin
from .models import Profile, Shop
from .test_guard import Base, sign_in_as_staff_session
from .test_moderators import make_moderator
from .tests import admin_session

User = get_user_model()
LIST = "/admin/shops/shop/"


class Search(Base):
    def setUp(self):
        super().setUp()
        self.owner = User.objects.create_user("merchant-key")
        Profile.objects.create(user=self.owner, email="recovery@example.org")
        self.shop = Shop.objects.create(
            name="Red Bar",
            city="Townville",
            owner=self.owner,
            email="shop@example.net",
            lightning_address="redbar@wallet.example.com",
            external_id="list-0042",
        )
        Shop.objects.create(name="Blue Pub", city="Townville")

    def found(self, term, **params):
        page = self.client.get(LIST, {"q": term, **params})
        self.assertEqual(page.status_code, 200)
        text = page.content.decode()
        return [name for name in ("Red Bar", "Blue Pub") if f">{name}</a>" in text]

    def test_a_moderator_finds_a_shop_by_what_the_shop_page_shows(self):
        make_moderator(self.client)
        for term in ("redbar@wallet", "shop@example.net", "list-0042", "merchant-key", "Red", "Townville"):
            with self.subTest(term=term):
                self.assertIn("Red Bar", self.found(term))
        self.assertEqual(self.found("redbar@wallet"), ["Red Bar"])

    def test_a_moderator_cannot_confirm_the_owners_recovery_email(self):
        make_moderator(self.client)
        for term in ("recovery@example.org", "recovery", "RECOVERY@example.org"):
            with self.subTest(term=term):
                self.assertEqual(self.found(term), [])
        # Nor through the address of the list, which a search field would otherwise open (config/admin_secrets.py)
        for lookup in ("owner__profile__email", "owner__profile__email__startswith", "owner__email"):
            with self.subTest(lookup=lookup):
                page = self.client.get(LIST, {lookup: "recovery@example.org"})
                self.assertNotIn(">Red Bar</a>", page.content.decode())

    def test_a_staff_member_who_can_change_shops_cannot_either(self):
        user = User.objects.create_user("helper", is_staff=True)
        user.user_permissions.add(*Permission.objects.filter(codename__in=["view_shop", "change_shop"]))
        sign_in_as_staff_session(self.client, User.objects.get(pk=user.pk))
        self.assertEqual(self.found("recovery@example.org"), [])
        self.assertEqual(self.found("merchant-key"), ["Red Bar"])

    def test_a_superuser_finds_the_shop_by_the_whole_recovery_email_only(self):
        admin_session(self.client)
        self.assertEqual(self.found("recovery@example.org"), ["Red Bar"])
        self.assertEqual(self.found("Recovery@Example.org"), ["Red Bar"])
        # The whole address: a piece of it does not guess the rest
        self.assertEqual(self.found("recovery"), [])
        self.assertEqual(self.found("@example.org"), [])

    def test_the_search_fields_by_user(self):
        moderator = User.objects.create_user("volunteer", is_staff=True)
        boss = User.objects.create_superuser("chief")
        model_admin = ShopAdmin(Shop, None)
        request = type("Request", (), {})()
        request.user = moderator
        self.assertNotIn("=owner__profile__email", model_admin.get_search_fields(request))
        request.user = boss
        self.assertIn("=owner__profile__email", model_admin.get_search_fields(request))
        # What a search names is shown, so askable in the address: never the recovery email
        self.assertNotIn("profile", model_admin.askable())


class NoSearchWithoutTheList(TestCase):
    def test_a_staff_account_without_the_right_to_see_shops_gets_nothing(self):
        user = User.objects.create_user("nobody", is_staff=True)
        sign_in_as_staff_session(self.client, User.objects.get(pk=user.pk))
        self.assertEqual(self.client.get(reverse("admin:shops_shop_changelist"), {"q": "x"}).status_code, 403)

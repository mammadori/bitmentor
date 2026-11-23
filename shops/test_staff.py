"""Registers for whoever works with the owner (shops/staff.py): each one with its own key and name, put on a phone with
a one-time code, turned off alone, and never in the way of a payment already on its screen."""

import json
import re
from datetime import timedelta
from unittest.mock import patch
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib.auth import get_user_model
from django.templatetags.static import static
from django.test import TestCase
from django.utils import timezone

from config.community import community

from .models import Shop, StaffRegister
from .test_experimental import OnChain
from .tests import reset_limits, steady_limits


def staff_page(register):
    return f"/en/shops/register/p/{register.id}/?k={register.key}"


page = staff_page


class StaffRegisters(TestCase):
    def setUp(self):
        reset_limits()
        self.owner = get_user_model().objects.create(username="owner")
        self.shop = Shop.objects.create(owner=self.owner, name="Bar", lightning_address="till@ln.example.org")
        self.client.force_login(self.owner)
        self.manage = f"/en/shops/registers/{self.shop.id}/"

    def add(self, name="Anna"):
        response = self.client.post(self.manage, {"name": name})
        register = StaffRegister.objects.get(shop=self.shop, name=" ".join(name.split()))
        self.assertRedirects(response, f"/en/shops/registers/{self.shop.id}/{register.id}/")
        return register

    def put_on_phone(self, register, client=None):
        """The phone of the person: no account, it opens the code and presses the button."""
        phone = client or self.client_class()
        register.refresh_from_db()
        response = phone.post("/en/shops/register/open/", {"code": register.code})
        self.assertRedirects(response, page(register))
        return phone

    def test_adding_a_register_shows_a_one_time_code_and_ticks_the_first_day(self):
        register = self.add(" Anna   evening ")
        self.assertEqual(register.name, "Anna evening")
        page = self.client.get(f"/en/shops/registers/{self.shop.id}/{register.id}/")
        self.assertContains(page, f"/en/shops/register/open/?c={register.code}")
        self.assertContains(page, "The code works once")
        # The code page is never the register itself: a photo of it shows no key
        self.assertNotContains(page, register.key)
        self.assertEqual(page["Referrer-Policy"], "same-origin")
        self.shop.refresh_from_db()
        self.assertIsNotNone(self.shop.register_shared)

    def test_the_code_page_says_the_register_was_added_and_the_list_calls_it_by_its_title(self):
        added = self.client.post(self.manage, {"name": "Anna"}, follow=True)
        self.assertContains(added, '<p class="notice no-print" role="status">Register added: «Anna».</p>')
        self.assertContains(added, "<h1>The register for Anna</h1>")
        register = StaffRegister.objects.get(shop=self.shop)
        code_page = f"/en/shops/registers/{self.shop.id}/{register.id}/"
        # Said once, on arriving from the list
        self.assertNotContains(self.client.get(code_page), "Register added")
        self.assertContains(
            self.client.get(self.manage), f'<a class="button button-quiet" href="{code_page}">The register for Anna</a>'
        )

    def test_only_the_owner_sees_and_changes_the_registers(self):
        register = self.add()
        other = self.client_class()
        other.force_login(get_user_model().objects.create(username="other"))
        self.assertEqual(other.get(self.manage).status_code, 404)
        self.assertEqual(other.post(self.manage, {"name": "Mallory"}).status_code, 404)
        self.assertEqual(other.get(f"/en/shops/registers/{self.shop.id}/{register.id}/").status_code, 404)
        self.assertEqual(other.post(f"/en/shops/registers/{self.shop.id}/{register.id}/retire/").status_code, 404)
        self.assertEqual(self.client_class().get(self.manage).status_code, 302)
        register.refresh_from_db()
        self.assertIsNone(register.retired)
        self.assertEqual(StaffRegister.objects.count(), 1)

    def test_the_code_opens_the_register_once_and_only_with_the_button(self):
        register = self.add()
        phone = self.client_class()
        # A chat preview or a camera app opening the link does not use the code up
        for _ in range(3):
            self.assertContains(
                phone.get(f"/en/shops/register/open/?c={register.code}"), "Open the register on this phone"
            )
        code = register.code
        self.put_on_phone(register, phone)
        register.refresh_from_db()
        self.assertEqual(register.code, "")
        self.assertIsNotNone(register.opened)
        # The same code a second time, from a customer's photo: nothing
        response = self.client_class().post("/en/shops/register/open/", {"code": code})
        self.assertEqual(response.status_code, 404)
        self.assertContains(response, "This code opens nothing", status_code=404)
        self.assertNotContains(response, register.key, status_code=404)

    def test_the_codes_lifetime_follows_the_community(self):
        from .staff import CODE_LIFETIME

        self.assertEqual(CODE_LIFETIME, timedelta(hours=24))
        register = self.add()
        self.assertAlmostEqual(register.code_until - timezone.now(), timedelta(hours=24), delta=timedelta(minutes=1))
        with patch.dict(community(), {"limits": {"staff_code_hours": 72}}):
            self.client.post(f"/en/shops/registers/{self.shop.id}/{register.id}/")
        register.refresh_from_db()
        self.assertAlmostEqual(register.code_until - timezone.now(), timedelta(hours=72), delta=timedelta(minutes=1))
        # Out of its bounds the value is the default: never a code that lives for a week
        with patch.dict(community(), {"limits": {"staff_code_hours": 24 * 7}}):
            self.client.post(f"/en/shops/registers/{self.shop.id}/{register.id}/")
        register.refresh_from_db()
        self.assertAlmostEqual(register.code_until - timezone.now(), timedelta(hours=24), delta=timedelta(minutes=1))
        # Short or long, the code is one-time and ends at code_until
        with patch.dict(community(), {"limits": {"staff_code_hours": 1}}):
            self.client.post(f"/en/shops/registers/{self.shop.id}/{register.id}/")
        register.refresh_from_db()
        self.assertAlmostEqual(register.code_until - timezone.now(), timedelta(hours=1), delta=timedelta(minutes=1))
        with patch("shops.staff.timezone.now", return_value=timezone.now() + timedelta(hours=1, minutes=1)):
            self.assertEqual(
                self.client_class().post("/en/shops/register/open/", {"code": register.code}).status_code, 404
            )
        self.put_on_phone(register)
        self.assertEqual(self.client_class().post("/en/shops/register/open/", {"code": register.code}).status_code, 404)

    def test_an_old_code_opens_nothing(self):
        register = self.add()
        StaffRegister.objects.filter(pk=register.pk).update(code_until=timezone.now() - timedelta(minutes=1))
        register.refresh_from_db()
        self.assertEqual(self.client_class().post("/en/shops/register/open/", {"code": register.code}).status_code, 404)

    def test_a_new_code_replaces_the_old_one_and_keeps_the_phones(self):
        register = self.add()
        phone = self.put_on_phone(register)
        old = register.code
        self.client.post(f"/en/shops/registers/{self.shop.id}/{register.id}/")
        register.refresh_from_db()
        self.assertNotEqual(register.code, old)
        self.assertEqual(self.client_class().post("/en/shops/register/open/", {"code": old}).status_code, 404)
        self.assertNotContains(phone.get(staff_page(register)), "turned off this register")

    def test_the_staff_register_shows_only_what_takes_payments(self):
        register = self.add()
        phone = self.put_on_phone(register)
        page = phone.get(staff_page(register))
        self.assertContains(page, "Anna")
        self.assertNotIn("sync", page.context["register"])
        self.assertFalse(page.context["register"]["retired"])
        self.assertContains(page, "Something is not right?")
        self.assertContains(page, "I typed the wrong amount")
        self.assertContains(page, "Do not give money back yourself")
        self.assertNotContains(page, "Your shops")
        self.assertNotContains(page, "/shops/registers/")
        self.assertEqual(page["Referrer-Policy"], "same-origin")
        self.assertEqual(page["X-Robots-Tag"], "noindex, nofollow")
        # Even the owner, signed in on the same phone, sees the staff register as the staff do
        owner_page = self.client.get(staff_page(register))
        self.assertNotContains(owner_page, "Leave the panel (the register stays open)")
        manifest = phone.get(f"/register/p/{register.id}/manifest.webmanifest?k={register.key}").json()
        self.assertEqual(manifest["start_url"], staff_page(register))
        worker = phone.get(f"/en/shops/register/p/{register.id}/sw.js")
        self.assertEqual(worker.status_code, 200)

    def test_the_owners_register_stays_as_it_was(self):
        page = self.client.get(f"/en/shops/register/{self.shop.id}/")
        self.assertNotIn("retired", page.context["register"])
        self.assertContains(page, "Leave the panel (the register stays open)")
        self.assertNotContains(page, "Something is not right?")

    def test_turning_off_stops_new_payments_and_nothing_else(self):
        anna = self.add("Anna")
        bruno = self.add("Bruno")
        phone = self.put_on_phone(anna)
        other = self.put_on_phone(bruno)
        response = self.client.post(f"/en/shops/registers/{self.shop.id}/{anna.id}/retire/")
        self.assertRedirects(response, self.manage)
        anna.refresh_from_db()
        self.assertIsNotNone(anna.retired)
        page = phone.get(staff_page(anna))
        self.assertContains(page, "The owner turned off this register")
        # Its sales are only on this phone: the staff hands the copy to the owner before the page stops opening
        self.assertContains(page, "Today, download the encrypted copy of its sales")
        self.assertTrue(page.context["register"]["retired"])
        # Bruno's register goes on, and so does the owner's own
        self.assertFalse(other.get(staff_page(bruno)).context["register"]["retired"])
        self.assertNotContains(other.get(staff_page(bruno)), "turned off this register")
        manage = self.client.get(self.manage)
        self.assertEqual([r.name for r in manage.context["active"]], ["Bruno"])
        self.assertEqual([r.name for r in manage.context["retired"]], ["Anna"])
        # A turned off register cannot be put on a new phone
        self.assertEqual(self.client.get(f"/en/shops/registers/{self.shop.id}/{anna.id}/").status_code, 404)

    def test_wrong_keys_open_nothing(self):
        register = self.add()
        other = self.add("Bruno")
        for wrong in (
            "x" * 32,
            "short",
            "",
            other.key,
            register.key[:-1] + ("B" if register.key.endswith("A") else "A"),
        ):
            self.assertEqual(self.client.get(f"/en/shops/register/p/{register.id}/?k={wrong}").status_code, 404, wrong)
            self.assertEqual(
                self.client.get(f"/register/p/{register.id}/manifest.webmanifest?k={wrong}").status_code, 404
            )
            self.assertEqual(self.client.post("/en/shops/register/open/", {"code": wrong}).status_code, 404, wrong)
        # There is no address to send sales to, with or without the key
        self.assertEqual(
            self.client.post(f"/register/p/{register.id}/sale/?k={register.key}", {"amount": "5"}).status_code, 404
        )

    def test_the_key_and_the_code_never_go_in_a_path(self):
        """gunicorn, Caddy and Django's error log write paths down, not queries (config/gunicorn.py, Caddyfile)."""
        register = self.add()
        code_page = self.client.get(f"/en/shops/registers/{self.shop.id}/{register.id}/").content.decode()
        register.refresh_from_db()
        for path in re.findall(r'(?:href|src|action|data-[a-z-]+)="(/[^"?]*)', code_page):
            self.assertNotIn(register.code, path)
        phone = self.client_class()
        response = phone.post("/en/shops/register/open/", {"code": register.code})
        self.assertNotIn(register.key, urlsplit(response["Location"]).path)
        page = phone.get(response["Location"]).content.decode()
        paths = re.findall(r'(?:href|src|action|data-[a-z-]+)="(/[^"?]*)', page)
        self.assertTrue(paths)
        for path in paths:
            self.assertNotIn(register.key, path)
        # Nor in the page's data: the register sends nothing to the site, so its script never needs the key
        self.assertNotIn(f'data-key="{register.key}"', page)

    def test_caddy_logs_neither_the_key_nor_the_code(self):
        """The error log writes the whole request: the key's query and header and the code's query are deleted. The
        access log drops the query and all headers."""
        caddyfile = (settings.BASE_DIR / "Caddyfile").read_text()
        error_log = caddyfile.split("log default {")[1].split("\n\t}")[0]
        query = error_log.split("request>uri query {")[1].split("}")[0]
        self.assertIn("delete k", query)
        self.assertIn("delete c", query)
        self.assertIn("request>headers>X-Register-Key delete", error_log)
        access_log = caddyfile.split("(access-on) {")[1].split("\n}")[0]
        # One expression for the field: all of the query goes but its question mark (and the token in the path of a
        # team invite, team/tests.py)
        pattern, replacement = re.search(r"request>uri regexp (\S+) (\S+)\n", access_log).groups()
        written = re.sub(
            pattern, re.sub(r"\$\{(\d)\}", r"\\g<\1>", replacement), "/en/shops/register/p/x/?k=the-key&c=code"
        )
        self.assertEqual(written, "/en/shops/register/p/x/?")
        self.assertIn("request>headers delete", access_log)
        self.assertIn("%(U)s", (settings.BASE_DIR / "config" / "gunicorn.py").read_text())

    def test_a_code_of_a_shop_without_a_register_opens_nothing(self):
        register = self.add()
        Shop.objects.filter(pk=self.shop.pk).update(lightning_address="")
        register.refresh_from_db()
        self.assertEqual(self.client_class().post("/en/shops/register/open/", {"code": register.code}).status_code, 404)
        register.refresh_from_db()
        self.assertNotEqual(register.code, "")

    def test_a_shop_without_a_register_has_no_staff_registers(self):
        imported = Shop.objects.create(owner=self.owner, name="Imported")
        self.assertEqual(self.client.get(f"/en/shops/registers/{imported.id}/").status_code, 404)

    @patch.dict(community(), {"limits": {"staff_registers_per_shop": 2}})
    def test_a_shop_has_a_ceiling_of_registers_turned_on(self):
        self.add("A")
        self.add("B")
        response = self.client.post(self.manage, {"name": "C"})
        self.assertContains(response, "Too many registers turned on")
        self.assertFalse(StaffRegister.objects.filter(name="C").exists())

    @patch("shops.staff.OPENS_PER_IP", 3)
    def test_trying_codes_is_limited(self):
        self.enterContext(steady_limits())
        phone = self.client_class()
        codes = [phone.post("/en/shops/register/open/", {"code": "a" * 32}).status_code for _ in range(4)]
        self.assertEqual(codes, [404, 404, 404, 429])

    def rename(self, register, name, client=None):
        return (client or self.client).post(f"/en/shops/registers/{self.shop.id}/{register.id}/rename/", {"name": name})

    def test_the_owner_renames_a_register_and_its_page_follows(self):
        register = self.add()
        phone = self.put_on_phone(register)
        key = register.key
        response = self.rename(register, "  Anna   morning ")
        self.assertRedirects(response, self.manage, fetch_redirect_response=False)
        register.refresh_from_db()
        self.assertEqual(register.name, "Anna morning")
        # Nothing else of the register changes: the same key on the phone, no new code
        self.assertEqual((register.key, register.code), (key, ""))
        self.assertContains(self.client.get(self.manage), "«Anna» is now «Anna morning»")
        self.assertContains(phone.get(page(register)), 'data-till-name="Anna morning"')

    def test_a_rename_is_checked_as_a_new_name(self):
        register = self.add()
        for wrong in ("   ", "x" * 41):
            response = self.rename(register, wrong)
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, f'id="rename-{register.id}" open')
        register.refresh_from_db()
        self.assertEqual(register.name, "Anna")
        # Put into the page as text
        self.rename(register, "<b>Bo</b>")
        self.assertContains(self.client.get(self.manage), "&lt;b&gt;Bo&lt;/b&gt;")
        self.assertNotContains(self.client.get(self.manage), "<b>Bo</b>")

    def test_a_rename_does_not_take_the_name_of_another_register_turned_on(self):
        """The day closing tells the registers apart by name."""
        anna, evening = self.add("Anna"), self.add("Evening")
        response = self.rename(evening, " anna ")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Another register of this shop has this name")
        evening.refresh_from_db()
        self.assertEqual(evening.name, "Evening")
        # its own name again, in other capitals, is not another register's
        self.assertRedirects(self.rename(evening, "EVENING"), self.manage, fetch_redirect_response=False)
        # the name of one turned off is free
        self.client.post(f"/en/shops/registers/{self.shop.id}/{anna.id}/retire/")
        anna.refresh_from_db()
        self.assertIsNotNone(anna.retired)
        self.assertRedirects(self.rename(evening, "Anna"), self.manage, fetch_redirect_response=False)

    def test_only_the_owner_renames_and_only_a_register_turned_on(self):
        register = self.add()
        other = self.client_class()
        other.force_login(get_user_model().objects.create(username="other"))
        self.assertEqual(self.rename(register, "Mallory", other).status_code, 404)
        self.assertEqual(self.rename(register, "Mallory", self.client_class()).status_code, 302)
        # A register of another shop of someone else, through the owner's own shop
        theirs = StaffRegister.objects.create(
            shop=Shop.objects.create(
                owner=get_user_model().objects.get(username="other"), name="Other", lightning_address="o@ln.example.org"
            ),
            name="Eve",
        )
        self.assertEqual(self.rename(theirs, "Mallory").status_code, 404)
        self.assertEqual(self.client.get(f"/en/shops/registers/{self.shop.id}/{register.id}/rename/").status_code, 405)
        self.client.post(f"/en/shops/registers/{self.shop.id}/{register.id}/retire/")
        self.assertEqual(self.rename(register, "Mallory").status_code, 404)
        register.refresh_from_db()
        theirs.refresh_from_db()
        self.assertEqual((register.name, theirs.name), ("Anna", "Eve"))

    @patch("shops.staff.REGISTER_CHANGES", 1)
    def test_renames_count_with_the_other_changes(self):
        self.enterContext(steady_limits())
        register = self.add()
        response = self.rename(register, "Bruno")
        self.assertContains(response, "Too many changes")
        register.refresh_from_db()
        self.assertEqual(register.name, "Anna")

    def test_a_register_can_be_given_one_of_the_shops_places(self):
        from .models import ShopPlace

        # Without other places, no choice
        self.assertNotContains(self.client.get(self.manage), 'name="place"')
        harbour = ShopPlace.objects.create(shop=self.shop, address="Harbour 1", city="Town", latitude=1, longitude=2)
        market = ShopPlace.objects.create(shop=self.shop, address="<b>Market</b>", latitude=3, longitude=4)
        page = self.client.get(self.manage)
        self.assertContains(page, '<option value="" selected>No place chosen</option>', html=True)
        self.assertContains(page, f'<option value="{harbour.pk}">Harbour 1, Town</option>', html=True)
        response = self.client.post(self.manage, {"name": "Anna", "place": harbour.pk})
        anna = StaffRegister.objects.get(name="Anna")
        self.assertRedirects(response, f"/en/shops/registers/{self.shop.id}/{anna.id}/")
        self.assertEqual(anna.place, harbour)
        bruno = self.add("Bruno")
        self.assertIsNone(bruno.place)
        # Grouped by place, the ones without one last; the place's address as text
        page = self.client.get(self.manage).content.decode()
        self.assertLess(page.index("Harbour 1, Town</h3>"), page.index("<strong>Anna</strong>"))
        self.assertLess(page.index("<strong>Anna</strong>"), page.index("No place chosen</h3>"))
        self.assertLess(page.index("No place chosen</h3>"), page.index("<strong>Bruno</strong>"))
        self.assertNotIn("<b>Market</b>", page)
        # The rename form moves it, and the name stays
        response = self.client.post(
            f"/en/shops/registers/{self.shop.id}/{bruno.id}/rename/", {"name": "Bruno", "place": market.pk}
        )
        self.assertRedirects(response, self.manage, fetch_redirect_response=False)
        bruno.refresh_from_db()
        self.assertEqual((bruno.name, bruno.place), ("Bruno", market))
        # Removing the place leaves the register, without a place
        market.delete()
        bruno.refresh_from_db()
        self.assertIsNone(bruno.place)
        self.assertIsNone(bruno.retired)

    def test_a_place_of_another_shop_is_refused(self):
        from .models import ShopPlace

        ShopPlace.objects.create(shop=self.shop, address="Harbour 1", latitude=1, longitude=2)
        other = Shop.objects.create(
            owner=get_user_model().objects.create(username="other"), name="Other", lightning_address="o@ln.example.org"
        )
        theirs = ShopPlace.objects.create(shop=other, address="Elsewhere", latitude=5, longitude=6)
        response = self.client.post(self.manage, {"name": "Anna", "place": theirs.pk})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(StaffRegister.objects.filter(name="Anna").exists())
        register = self.add("Bruno")
        response = self.client.post(
            f"/en/shops/registers/{self.shop.id}/{register.id}/rename/", {"name": "Carla", "place": theirs.pk}
        )
        self.assertEqual(response.status_code, 200)
        register.refresh_from_db()
        self.assertEqual((register.name, register.place), ("Bruno", None))
        # A shop without other places takes no place, whatever is sent
        ShopPlace.objects.filter(shop=self.shop).delete()
        self.client.post(
            f"/en/shops/registers/{self.shop.id}/{register.id}/rename/", {"name": "Bruno", "place": theirs.pk}
        )
        register.refresh_from_db()
        self.assertIsNone(register.place)

    def test_the_place_changes_nothing_for_the_register(self):
        from .models import ShopPlace

        place = ShopPlace.objects.create(shop=self.shop, address="Harbour 1", latitude=1, longitude=2)
        response = self.client.post(self.manage, {"name": "Anna", "place": place.pk})
        register = StaffRegister.objects.get(name="Anna")
        self.assertRedirects(response, f"/en/shops/registers/{self.shop.id}/{register.id}/")
        code_page = self.client.get(f"/en/shops/registers/{self.shop.id}/{register.id}/")
        self.assertNotIn("Harbour", json.dumps(code_page.context["pairing"]))
        phone = self.put_on_phone(register)
        self.assertNotContains(phone.get(page(register)), "Harbour")

    def test_the_panel_leads_to_the_staff_registers(self):
        self.add()
        panel = self.client.get("/en/shops/")
        self.assertContains(panel, self.manage)
        self.assertContains(panel, "Registers for your staff: 1 turned on")
        self.assertContains(panel, "<summary>Registers for your staff</summary>")
        self.assertContains(panel, f'href="/en/shops/places/{self.shop.id}/">The other places of your shop</a>')

    def test_the_panel_names_the_registers_turned_on(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from .views import STAFF_NAMES_SHOWN

        names = [f"P{n}" for n in range(STAFF_NAMES_SHOWN + 2)] + ["<i>Zed</i>"]
        StaffRegister.objects.bulk_create(
            StaffRegister(shop=self.shop, name=name, created=timezone.now()) for name in names
        )
        StaffRegister.objects.create(shop=self.shop, name="Gone", retired=timezone.now())
        other = Shop.objects.create(owner=self.owner, name="Café", lightning_address="cafe@ln.example.org")
        StaffRegister.objects.create(shop=other, name="Bruno")
        with CaptureQueriesContext(connection) as queries:
            panel = self.client.get("/en/shops/")
        # One query for the registers of every shop, not one per shop
        self.assertEqual(sum('"shops_staffregister"' in query["sql"] for query in queries.captured_queries), 1)
        shops = {shop.name: shop for shop in panel.context["shops"]}
        self.assertEqual(shops["Bar"].staff_count, len(names))
        self.assertEqual(sorted(shops["Bar"].staff_names), sorted(names))
        self.assertEqual(len(shops["Bar"].staff_shown), STAFF_NAMES_SHOWN)
        self.assertContains(panel, f"Registers for your staff: {len(names)} turned on")
        self.assertContains(panel, f"…and {len(names) - STAFF_NAMES_SHOWN} more")
        self.assertNotContains(panel, "Gone")
        self.assertContains(panel, f'<p class="muted staff-names" id="staff-names-{other.id}">Bruno</p>', html=True)
        self.assertNotContains(panel, "<i>Zed</i>")
        StaffRegister.objects.filter(name="<i>Zed</i>").update(created=timezone.now() + timedelta(days=1))
        self.assertContains(self.client.get("/en/shops/"), "&lt;i&gt;Zed&lt;/i&gt;, ")

    def test_the_owners_pages_are_closed_to_search_engines(self):
        with self.settings(SITE_MODE="production"):
            self.assertIn("Disallow: /en/shops/registers/", self.client.get("/robots.txt").content.decode())


class StaffRegisterPage(TestCase):
    """The page of a staff register: the staff's words, and turned off it takes no new sale."""

    def setUp(self):
        reset_limits()
        self.owner = get_user_model().objects.create(username="owner")
        self.shop = Shop.objects.create(owner=self.owner, name="Bar", lightning_address="till@ln.example.org")
        self.register = StaffRegister.objects.create(shop=self.shop, name="Anna")

    def test_the_page_carries_the_staff_words(self):
        page = self.client.get(staff_page(self.register))
        data = page.context["register"]
        self.assertFalse(data["retired"])
        self.assertContains(page, f'data-worker="/en/shops/register/p/{self.register.id}/sw.js"')
        self.assertEqual(data["texts"]["to"], "")
        self.assertIn("owner", data["texts"]["no_check"])
        self.assertNotContains(page, "Paid into")

    def retire(self, minutes_ago):
        StaffRegister.objects.filter(pk=self.register.pk).update(
            retired=timezone.now() - timedelta(minutes=minutes_ago)
        )
        self.register.refresh_from_db()

    def test_the_owner_s_register_knows_the_registers_turned_off_for_good(self):
        recent = StaffRegister.objects.create(shop=self.shop, name="Bruno", retired=timezone.now())
        StaffRegister.objects.create(shop=self.shop, name="Carla")
        self.retire(25 * 60)
        owner_page = f"/en/shops/register/{self.shop.id}/"
        self.client.force_login(self.owner)
        page = self.client.get(owner_page)
        self.assertEqual(page.context["register"]["gone_tills"], [str(self.register.id)])
        self.assertNotIn(str(recent.id), page.context["register"]["gone_tills"])
        # Nobody else learns the shop's registers, nor does a staff register
        self.client.force_login(get_user_model().objects.create(username="other"))
        self.assertNotIn("gone_tills", self.client.get(owner_page).context["register"])
        self.client.logout()
        self.assertNotIn("gone_tills", self.client.get(staff_page(recent)).context["register"])

    def test_the_owner_s_register_names_the_newest_registers_turned_off_first(self):
        from .views import GONE_TILLS

        now = timezone.now()
        StaffRegister.objects.bulk_create(
            StaffRegister(shop=self.shop, name=f"Old {n}", retired=now - timedelta(days=400 - n % 300, minutes=n))
            for n in range(GONE_TILLS + 20)
        )
        newest = StaffRegister.objects.create(shop=self.shop, name="Last", retired=now - timedelta(days=2))
        self.client.force_login(self.owner)
        gone = self.client.get(f"/en/shops/register/{self.shop.id}/").context["register"]["gone_tills"]
        self.assertEqual(len(gone), GONE_TILLS)
        self.assertEqual(gone[0], str(newest.id))
        expected = list(
            StaffRegister.objects.filter(shop=self.shop, retired__isnull=False)
            .order_by("-retired")
            .values_list("id", flat=True)[:GONE_TILLS]
        )
        self.assertEqual(gone, [str(pk) for pk in expected])

    def test_turned_off_the_register_takes_no_new_sale(self):
        self.retire(0)
        page = self.client.get(staff_page(self.register))
        self.assertTrue(page.context["register"]["retired"])
        self.assertNotContains(page, "data-worker=")
        # The phone's copy goes away: the script that removes the service worker
        worker = self.client.get(f"/en/shops/register/p/{self.register.id}/sw.js").content
        self.assertEqual(worker, (settings.BASE_DIR / "static" / "register-sw-gone.js").read_bytes())

    def test_long_turned_off_the_old_key_opens_nothing_of_the_shop(self):
        """«Turned off» was kept only by the page's script: the old key went on opening the shop's register data. After
        the grace for the end of the shift the server answers 410, the page that the register is gone, with nothing of
        the shop; the manifest too, and the service worker is the one that removes itself."""
        self.retire(24 * 60 - 1)
        self.assertEqual(self.client.get(staff_page(self.register)).status_code, 200)
        self.retire(24 * 60 + 1)
        page = self.client.get(staff_page(self.register))
        self.assertContains(page, "This register is turned off", status_code=410)
        self.assertNotIn("register", page.context)
        for leak in ("till@ln.example.org", "Bar", "data-worker"):
            self.assertNotContains(page, leak, status_code=410)
        self.assertEqual(page["Cache-Control"], "no-store")
        manifest = self.client.get(f"/register/p/{self.register.id}/manifest.webmanifest?k={self.register.key}")
        self.assertEqual(manifest.status_code, 410)
        worker = self.client.get(f"/en/shops/register/p/{self.register.id}/sw.js").content
        self.assertEqual(worker, (settings.BASE_DIR / "static" / "register-sw-gone.js").read_bytes())
        # A wrong key still gets the plain 404, not the 410 that would say the register exists
        self.assertEqual(self.client.get(f"/en/shops/register/p/{self.register.id}/?k={'x' * 32}").status_code, 404)


class Pairing(OnChain, TestCase):
    """The pairing code: drawn on the owner's phone, with the on-chain key's child of the register and the owner's
    preferences in the link's fragment, which the site never sees. The site keeps the list of registers and their k."""

    def setUp(self):
        reset_limits()
        self.owner = get_user_model().objects.create(username="owner")
        self.shop = Shop.objects.create(
            owner=self.owner, name="Bar", city="Example", discount=10, lightning_address="Till@LN.example.org"
        )
        self.client.force_login(self.owner)

    def add(self, name):
        self.client.post(f"/en/shops/registers/{self.shop.id}/", {"name": name})
        return StaffRegister.objects.get(shop=self.shop, name=name)

    def code_page(self, register):
        return self.client.get(f"/en/shops/registers/{self.shop.id}/{register.id}/")

    def test_new_registers_share_the_shops_chain(self):
        # A register gets no child of its own (shops/chain_count.py): its code carries the owner's receive chain, drawn
        # on the owner's phone
        anna = self.add("Anna")
        self.assertNotIn("child", self.code_page(anna).context["pairing"])

    def test_the_site_gives_the_link_and_the_suggestions_never_a_key(self):
        register = self.add("Anna")
        page = self.code_page(register)
        data = page.context["pairing"]
        self.assertEqual(data["url"], f"{settings.PUBLIC_URL}/en/shops/register/open/?c={register.code}")
        self.assertEqual(
            (data["register"], data["lightning"], data["network"]), (str(register.id), "till@ln.example.org", "signet")
        )
        self.assertEqual((data["discount"], data["shop"]["name"], data["shop"]["town"]), (10, "Bar", "Example"))
        self.assertGreaterEqual(len(data["sources"]), 2)
        self.assertFalse({"key", "onchain", "xpub", "address"} & set(data))
        # The owner's phone opens the shop's chain on the site when it draws the code (shops/chain_count.py)
        self.assertEqual(data["chain_open"]["url"], f"/en/shops/register/{self.shop.id}/chain/")
        self.assertTrue(data["chain_open"]["csrf"])
        # The QR is drawn by the owner's phone, with the pairing in the fragment: the page has no picture of the link
        self.assertNotContains(page, "data:image/svg")
        self.assertNotContains(page, "#pair=")
        for script in (
            "vendor/qrcode/qrcode.js",
            "register-derive.js",
            "register-nostr.js",
            "register-pairing.js",
            "staff-pair.js",
        ):
            # static(): the name with the fingerprint in an image with collected files, the plain one without
            self.assertContains(page, f'<script src="{static(script)}"></script>')
        self.assertContains(page, '<script id="pairing-data" type="application/json">')

    def test_the_open_page_keeps_the_pairing_only_for_a_code_that_opens(self):
        register = self.add("Anna")
        register.refresh_from_db()
        phone = self.client_class()
        page = phone.get(f"/en/shops/register/open/?c={register.code}")
        self.assertContains(page, f'data-register="{register.id}"')
        self.assertContains(page, f'<script src="{static("staff-open.js")}"></script>')
        wrong = phone.get("/en/shops/register/open/?c=" + "x" * 32)
        self.assertNotContains(wrong, "staff-open.js", status_code=404)

    def test_the_staff_register_has_no_field_for_the_destination(self):
        register = self.add("Anna")
        register.refresh_from_db()
        phone = self.client_class()
        phone.post("/en/shops/register/open/", {"code": register.code})
        page = phone.get(staff_page(register))
        self.assertContains(page, f'data-staff="{register.id}"')
        for field in ('id="settings"', 'id="chain-key"', 'id="settings-open"', "<textarea", 'id="currency"'):
            self.assertNotContains(page, field)
        self.assertContains(page, 'id="paired"')
        # The owner's register has the settings, the practice register neither
        owner = self.client.get(f"/en/shops/register/{self.shop.id}/")
        self.assertContains(owner, 'id="chain-key"')
        self.assertContains(owner, 'id="settings-open"')
        practice = self.client.get(f"/en/shops/practice/{self.shop.id}/")
        self.assertNotContains(practice, 'id="chain-key"')
        self.assertContains(practice, 'data-practice="1"')

    def test_the_script_reads_the_pairing_only_of_this_register_and_only_fresh(self):
        # What the register does with it is tried in node (shops/test_register_paths.py, StaffPairing)
        opener = (settings.BASE_DIR / "static" / "staff-open.js").read_text()
        self.assertIn('document.querySelector("form[data-register]")', opener)
        self.assertIn("history.replaceState", opener)


DRAW = r"""
const fs = require("fs");
const [input, ...files] = process.argv.slice(2);
const given = JSON.parse(fs.readFileSync(input, "utf8"));
const elements = {};
const element = (id) => (elements[id] ||= { id, hidden: false, textContent: "", src: "", children: [],
  replaceChildren(...items) { this.children = items; }, addEventListener() {}, dataset: {} });
element("pairing-data").textContent = JSON.stringify(given.pairing);
globalThis.self = globalThis;
globalThis.document = { getElementById: element, createElement: () => ({ textContent: "" }) };
const kept = { [`register-settings:${given.pairing.shop_id}`]: JSON.stringify(given.settings) };
globalThis.localStorage = { getItem: (key) => kept[key] ?? null, setItem: (key, value) => { kept[key] = String(value); } };
for (const file of files) eval(fs.readFileSync(file, "utf8"));
setTimeout(async () => {
  const link = element("pair-link").textContent;
  const out = { link, error: element("pair-error").textContent, summary: element("pair-summary").children.map((c) => c.textContent),
    qr: element("pair-qr").src.startsWith("data:image/svg+xml,") };
  if (link) {
    out.pairing = registerPairing.decode(link.split("#pair=")[1], given.pairing.sources, given.pairing.public_relays);
    out.tills = JSON.parse(kept[`register-nostr-tills:${given.pairing.shop_id}`] || "{}");
    if (out.pairing.nostr) {
      out.tillPublic = registerNostr.publicKey(out.pairing.nostr.secret);
      out.tillKeys = JSON.parse(kept[`register-nostr-tillkeys:${given.pairing.shop_id}`] || "{}");
    }
    let key = given.settings.onchain?.key || given.account;
    for (const step of (given.settings.onchain?.path || "0").split("/")) key = await registerDerive.childKey(key, Number(step), "mainnet");
    out.expected = key;
  }
  process.stdout.write(JSON.stringify(out));
}, 1500);
"""


class PairingDrawn(OnChain, TestCase):
    """static/staff-pair.js in node, with the owner's settings on the phone: the code carries the register's child key
    and the owner's preferences, and nothing when the owner's register pays into another Lightning address."""

    def setUp(self):
        import os
        import shutil

        self.node = os.environ.get("NODE") or shutil.which("node")
        if not self.node:
            self.skipTest("node is missing: NODE=/path/to/node to draw the pairing code")
        owner = get_user_model().objects.create(username="owner")
        self.shop = Shop.objects.create(
            owner=owner, name="Bar", city="Example", lightning_address="till@ln.example.org"
        )
        self.register = StaffRegister.objects.create(
            shop=self.shop, name="Anna", code="c" * 32, code_until=timezone.now() + timedelta(hours=1)
        )

    def draw(self, settings_on_phone, register=None):
        import json
        import subprocess
        import tempfile
        from pathlib import Path

        from .staff import pairing_data
        from .test_explorer import with_register
        from .test_register_chain import BIP84_ACCOUNT

        with self.settings(NETWORK="mainnet"), with_register(**(register or {})):
            pairing = pairing_data(
                self.shop, self.register, "https://example.org/en/shops/register/open/?c=" + "c" * 32
            )
        static = settings.BASE_DIR / "static"
        with tempfile.TemporaryDirectory() as folder:
            harness, given = Path(folder) / "draw.js", Path(folder) / "given.json"
            harness.write_text(DRAW)
            given.write_text(json.dumps({"pairing": pairing, "settings": settings_on_phone, "account": BIP84_ACCOUNT}))
            result = subprocess.run(
                [
                    self.node,
                    str(harness),
                    str(given),
                    *(
                        str(static / name)
                        for name in (
                            "vendor/qrcode/qrcode.js",
                            "register-derive.js",
                            "register-nostr.js",
                            "register-pairing.js",
                            "staff-pair.js",
                        )
                    ),
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_the_code_carries_the_child_key_and_the_preferences(self):
        from .test_register_chain import BIP84_ACCOUNT

        out = self.draw(
            {
                "lightning": "till@ln.example.org",
                "onchain": {"key": BIP84_ACCOUNT},
                "discount": 7,
                "currency": "CHF",
                "confirmAbove": 2500,
                "sources": ["kraken", "bitstamp"],
                "note": "Thanks",
            }
        )
        self.assertTrue(out["qr"])
        self.assertTrue(
            out["link"].startswith("https://example.org/en/shops/register/open/?c=" + "c" * 32 + "#pair=1.")
        )
        pairing = out["pairing"]
        # The owner's receive chain …/0: the key of the chain, counted with the other registers through the site
        self.assertEqual(pairing["onchain"], {"key": out["expected"], "path": "0"})
        self.assertTrue(any("…/0/n" in line for line in out["summary"]))
        self.assertNotIn(BIP84_ACCOUNT, out["link"])
        self.assertEqual(
            (pairing["discount"], pairing["currency"], pairing["confirmAbove"], pairing["sources"], pairing["note"]),
            (7, "CHF", 2500, ["kraken", "bitstamp"], "Thanks"),
        )
        self.assertEqual(
            (pairing["lightning"], pairing["register"], pairing["shop"]["town"]),
            ("till@ln.example.org", str(self.register.id), "Example"),
        )

    def test_the_owners_path_goes_with_the_code(self):
        from .test_register_chain import BIP84_ACCOUNT

        out = self.draw({"lightning": "till@ln.example.org", "onchain": {"key": BIP84_ACCOUNT, "path": "0/5"}})
        self.assertEqual(out["pairing"]["onchain"], {"key": out["expected"], "path": "0/5"})
        self.assertTrue(any("…/0/5/n" in line for line in out["summary"]))
        # A path that is not one draws no code
        out = self.draw({"lightning": "till@ln.example.org", "onchain": {"key": BIP84_ACCOUNT, "path": "0'/5"}})
        self.assertEqual(out["link"], "")

    def test_with_the_owners_nostr_key_the_code_carries_a_key_for_the_register(self):
        owner = "7e7e9c42a91bfef19fa929e5fda1b72e0ebc1a4c1141673e2794234d86addf4e"
        on_phone = {"nostr": {"owner": owner, "relays": ["wss://nos.lol", "wss://evil.example.org"]}}
        out = self.draw(on_phone, {"relays": ["wss://ours.example.org"], "public_relays": ["wss://nos.lol"]})
        self.assertIn("#pair=2.", out["link"])
        nostr = out["pairing"]["nostr"]
        self.assertEqual((nostr["owner"], nostr["relays"]), (owner, ["wss://nos.lol"]))
        # The owner's phone keeps the register's public key, to know its copies; never the private one
        self.assertEqual(out["tills"], {str(self.register.id): [out["tillPublic"]]})
        self.assertNotIn(nostr["secret"], json.dumps(out["tills"]))
        # ... and the conversation key it shares with the owner, to read its copies with no private key; not the private key
        self.assertEqual(list(out["tillKeys"]), [out["tillPublic"]])
        self.assertEqual(out["tillKeys"][out["tillPublic"]]["owner"], owner)
        self.assertRegex(out["tillKeys"][out["tillPublic"]]["key"], r"^[0-9a-f]{64}$")
        self.assertNotIn(nostr["secret"], json.dumps(out["tillKeys"]))
        # Without the community's relays, no copy on Nostr: a version 1 code
        out = self.draw(on_phone)
        self.assertIn("#pair=1.", out["link"])
        self.assertIsNone(out["pairing"]["nostr"])

    def test_lightning_only_without_settings_on_this_phone(self):
        out = self.draw({})
        self.assertIsNone(out["pairing"]["onchain"])
        self.assertEqual(out["pairing"]["discount"], 0)

    def test_no_code_when_the_owners_register_pays_into_another_address(self):
        out = self.draw({"lightning": "other@ln.example.org"})
        self.assertEqual(out["link"], "")
        self.assertIn("other@ln.example.org", out["error"])

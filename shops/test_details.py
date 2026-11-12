"""The shop's business details and what its owner shows of them (shops/details.py): the business name and VAT number,
the ticks of «What customers see», every public output that must leave out what is hidden, and the places where the
details travel (the migration, the import, the move to another site)."""
import io
import json
import logging
import re
import tempfile
import uuid
from pathlib import Path
from unittest.mock import patch

import requests
from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.management import CommandError, call_command
from django.db import connection
from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext, override

from config.urls import ADMIN_SESSION
from website.views import InertShop

from . import details, moving
from .forms import ShopDetailsForm, ShopDisplayForm
from .models import Profile, Shop
from .nostr import shop_event
from .tests import admin_session

NAME = "Red Bar"
ADDRESS = "Hidden Street 7"
WEBSITE = "https://red-bar.example.org"
PHONE = "+39 0461 555 123"
EMAIL = "red@example.org"
LEGAL = "Red Bar Ltd of Mario Red"
CITY = "Example"
SECRET_SEED = "wild-flowers"


def italian(ten):
    """A partita IVA that passes the check: the ten digits and the one that completes them."""
    return next(ten + str(last) for last in range(10) if details.italian_check(ten + str(last)))


VAT = "IT" + italian("0123456789")
# Words that must never show where the owner did not ask for them
HIDDEN = {"address": ADDRESS, "phone": PHONE, "email": EMAIL, "legal_name": LEGAL, "vat_number": VAT}


def make_shop(owner=None, **fields):
    values = dict(name=NAME, address=ADDRESS, city=CITY, website=WEBSITE, phone=PHONE, email=EMAIL, legal_name=LEGAL,
                  vat_number=VAT, latitude=41.9, longitude=12.5, lightning=True,
                  lightning_address="bar@wallet.example.org", external_id="")
    values.update(fields)
    return Shop.objects.create(owner=owner, **values)


def an_owner(name="owner"):
    """An account that signs in with its email, so that a move finds its owner on the other site."""
    user = get_user_model().objects.create(username=name)
    Profile.objects.create(user=user, email=f"{name}@owners.example.org")
    return user


class Vat(SimpleTestCase):
    def test_signs_around_the_digits_go(self):
        self.assertEqual(details.normalize_vat(" it 012.345-678/90 "), "IT01234567890")
        self.assertEqual(details.normalize_vat(None), "")
        self.assertEqual(details.normalize_vat(""), "")

    def test_an_italian_number_has_eleven_digits_and_the_check(self):
        good = italian("1234567890")
        self.assertEqual(details.vat_doubt("IT" + good), "")
        self.assertEqual(details.vat_doubt(good), "")
        self.assertEqual(details.vat_doubt("it " + good[:3] + " " + good[3:]), "")
        wrong = good[:-1] + str((int(good[-1]) + 1) % 10)
        for number in ("IT" + wrong, "IT" + good[:-1], "IT" + good + "0", "ITABCDEFGHIJK"):
            self.assertIn("eleven digits", details.vat_doubt(number), number)

    def test_the_known_numbers(self):
        # Numbers the tax office itself prints in its examples
        for number in ("IT00743110157", "IT01234567897", "12345678903"):
            self.assertEqual(details.vat_doubt(number), "", number)
        self.assertNotEqual(details.vat_doubt("IT12345678900"), "")

    def test_another_country_is_looked_at_for_its_letters_only(self):
        for number in ("DE123456789", "FR12345678901", "ATU12345678", "IE1234567T", "EL123456789", "XI123456789", "NL123456789B01",
                       "ESX1234567X"):
            self.assertEqual(details.vat_doubt(number), "", number)
        self.assertIn("not those of a country of the EU", details.vat_doubt("US123456789"))
        self.assertIn("not those of a country of the EU", details.vat_doubt("GB123456789"))
        self.assertIn("few digits or letters", details.vat_doubt("DE1"))
        self.assertIn("few digits or letters", details.vat_doubt("DE12345678901234"))
        self.assertIn("few digits or letters", details.vat_doubt("DE123@56789"))

    def test_a_bare_number_that_is_not_italian_asks_for_the_letters(self):
        self.assertIn("two letters of the country", details.vat_doubt("123456789"))
        self.assertIn("two letters of the country", details.vat_doubt("hello"[:1] + "12"))

    def test_nothing_written_is_nothing_to_doubt(self):
        self.assertEqual(details.vat_doubt(""), "")
        self.assertEqual(details.vat_doubt("  "), "")

    def test_control_and_invisible_characters_never_stay_in_the_number(self):
        for text in ("IT01234567890\x00", "\u202eIT01234567890", "IT\u200b01234567890", "I\ufeffT01234567890", "IT\n01234567890",
                     "IT\u2060012\u200d34567890"):
            self.assertEqual(details.normalize_vat(text), "IT01234567890", repr(text))

    def test_a_capital_that_is_two_letters_is_measured_after_it_grows(self):
        self.assertEqual(details.normalize_vat("ß" * 17), "SS" * 17)

    def test_a_strange_value_never_raises(self):
        for text in ("‮", "😀", "%" * 40, "1" * 40, "I" * 3, "--", "٣٤٥٦٧٨٩٠١٢٣"):
            self.assertIsInstance(details.vat_doubt(text), str)


class Defaults(TestCase):
    def test_the_website_and_the_address_are_shown_and_the_rest_is_not(self):
        shop = make_shop()
        self.assertEqual(sorted(details.shown(shop)), ["address", "website"])
        self.assertEqual([details.chosen(shop, name) for name in details.CHOICES], [True, True, False, False, False, False])
        self.assertEqual(shop.public_details, {"website": WEBSITE, "address": ADDRESS})

    def test_a_detail_shown_but_empty_is_not_a_line(self):
        shop = make_shop(website="", phone="", show_phone=True)
        self.assertEqual(details.shown(shop), {"address": ADDRESS})
        self.assertEqual(details.rows(shop, details.shown(shop)), [{"name": "address", "value": ADDRESS, "hidden": False}])

    def test_the_place_is_the_address_or_the_town(self):
        shop = make_shop()
        self.assertEqual(shop.public_place, ADDRESS)
        shop.show_address = False
        self.assertEqual(shop.public_place, CITY)
        self.assertEqual(make_shop(name="Other", address="").public_place, CITY)

    def test_every_choice_has_its_field_its_label_and_a_place(self):
        for name in details.CHOICES:
            self.assertTrue(hasattr(Shop, name))
            self.assertTrue(hasattr(Shop, details.flag(name)))
            self.assertIn(name, details.LABELS)
            self.assertIn(name, details.PAGE_ORDER)
        self.assertEqual(set(details.PAGE_ONLY) | {"website", "address"}, set(details.CHOICES))


class PublicOutputs(TestCase):
    """Whatever shows a shop to someone who is not its owner leaves out what is hidden: not empty, not there."""

    def setUp(self):
        self.owner = an_owner()
        self.shop = make_shop(owner=self.owner)

    def everywhere(self, shop=None):
        """The public outputs, as text, with what each one is."""
        shop = shop or self.shop
        anonymous = Client()
        found = {}
        for language in ("en", "it"):
            found[f"page {language}"] = anonymous.get(f"/{language}/shops/s/{shop.id}/").content.decode()
            found[f"map {language}"] = anonymous.get(f"/{language}/shops/map/").content.decode()
        found["json"] = anonymous.get("/shops.json").content.decode()
        found["nostr"] = json.dumps(shop_event("01" * 32, shop, "https://example.org/s/x/"), ensure_ascii=False)
        return found

    def test_by_default_only_the_address_and_the_website_come_out(self):
        for where, text in self.everywhere().items():
            for name in ("phone", "email", "legal_name", "vat_number"):
                self.assertNotIn(HIDDEN[name], text, (where, name))
            if where.startswith("page") or where == "json":
                self.assertIn(ADDRESS, text, where)
        self.assertIn(WEBSITE, self.everywhere()["json"])

    def test_the_page_shows_what_is_chosen_with_its_names(self):
        Shop.objects.filter(pk=self.shop.pk).update(show_phone=True, show_email=True, show_legal_name=True, show_vat_number=True)
        page = Client().get(f"/en/shops/s/{self.shop.id}/").content.decode()
        self.assertIn(f'<a href="tel:{PHONE}">{PHONE}</a>', page)
        self.assertIn(f'<a href="mailto:{EMAIL}">{EMAIL}</a>', page)
        self.assertIn(f"Business name</span> {LEGAL}", page)
        self.assertIn(f"VAT number</span> {VAT}", page)
        self.assertIn(f'<a href="{WEBSITE}" rel="noopener nofollow">Website</a>', page)
        self.assertLess(page.index(ADDRESS), page.index("tel:"))
        italian_page = Client().get(f"/it/shops/s/{self.shop.id}/").content.decode()
        self.assertIn("Ragione sociale</span>", italian_page)
        self.assertIn("Partita IVA</span>", italian_page)

    def test_a_shop_page_with_nothing_chosen_has_only_the_name_and_the_town(self):
        Shop.objects.filter(pk=self.shop.pk).update(show_website=False, show_address=False)
        page = Client().get(f"/en/shops/s/{self.shop.id}/").content.decode()
        for value in HIDDEN.values():
            self.assertNotIn(value, page)
        self.assertNotIn(WEBSITE, page)
        self.assertIn(NAME, page)
        self.assertIn(CITY, page)

    def test_the_json_has_no_hidden_detail_at_all(self):
        Shop.objects.filter(pk=self.shop.pk).update(show_website=False, show_address=False)
        row = next(shop for shop in Client().get("/shops.json").json()["shops"] if shop["name"] == NAME)
        for key in ("address", "website", "phone", "email", "legal_name", "vat_number"):
            self.assertNotIn(key, row)
        self.assertEqual(row["city"], CITY)
        Shop.objects.filter(pk=self.shop.pk).update(show_website=True, show_address=True)
        row = next(shop for shop in Client().get("/shops.json").json()["shops"] if shop["name"] == NAME)
        self.assertEqual((row["address"], row["website"]), (ADDRESS, WEBSITE))

    def test_the_json_keeps_the_website_and_the_address_even_empty_while_they_are_shown(self):
        make_shop(owner=an_owner("two"), name="Plain", address="", website="")
        row = next(shop for shop in Client().get("/shops.json").json()["shops"] if shop["name"] == "Plain")
        self.assertEqual((row["address"], row["website"]), ("", ""))

    def test_the_json_never_carries_the_phone_the_email_the_business_name_or_the_vat_number(self):
        Shop.objects.filter(pk=self.shop.pk).update(show_phone=True, show_email=True, show_legal_name=True, show_vat_number=True)
        text = Client().get("/shops.json").content.decode()
        for name in ("phone", "email", "legal_name", "vat_number"):
            self.assertNotIn(HIDDEN[name], text)

    def test_the_nostr_event_says_the_town_when_the_address_is_hidden(self):
        event = shop_event("01" * 32, self.shop, "https://example.org/s/x/")
        self.assertEqual(dict(event["tags"])["location"], ADDRESS)
        Shop.objects.filter(pk=self.shop.pk).update(show_address=False)
        self.shop.refresh_from_db()
        event = shop_event("01" * 32, self.shop, "https://example.org/s/x/")
        self.assertEqual(dict(event["tags"])["location"], CITY)
        text = json.dumps(event, ensure_ascii=False)
        for value in HIDDEN.values():
            self.assertNotIn(value, text)

    def test_the_nostr_event_never_carries_the_contacts_or_the_business_details_even_when_they_are_shown(self):
        Shop.objects.filter(pk=self.shop.pk).update(show_phone=True, show_email=True, show_legal_name=True, show_vat_number=True)
        self.shop.refresh_from_db()
        text = json.dumps(shop_event("01" * 32, self.shop, "https://example.org/s/x/"), ensure_ascii=False)
        for name in ("phone", "email", "legal_name", "vat_number"):
            self.assertNotIn(HIDDEN[name], text)

    def test_a_suspended_shop_keeps_only_its_address(self):
        Shop.objects.filter(pk=self.shop.pk).update(suspended=True, show_phone=True, show_email=True, show_legal_name=True,
                                                    show_vat_number=True)
        page = Client().get(f"/en/shops/s/{self.shop.id}/").content.decode()
        for name in ("phone", "email", "legal_name", "vat_number"):
            self.assertNotIn(HIDDEN[name], page)
        self.assertNotIn(WEBSITE, page)
        self.assertIn(ADDRESS, page)
        Shop.objects.filter(pk=self.shop.pk).update(show_address=False)
        self.assertNotIn(ADDRESS, Client().get(f"/en/shops/s/{self.shop.id}/").content.decode())

    def test_the_printed_sheets_and_the_sign_say_only_the_name(self):
        self.client.force_login(self.owner)
        Shop.objects.filter(pk=self.shop.pk).update(show_phone=True, show_email=True, show_legal_name=True, show_vat_number=True)
        for name, args in (("shop_sign", [self.shop.id]), ("till_card", [self.shop.id]), ("customer_card", [self.shop.id]),
                           ("customer", [self.shop.id]), ("customer_help", [self.shop.id])):
            response = self.client.get(reverse(name, args=args))
            self.assertEqual(response.status_code, 200, name)
            text = response.content.decode()
            for key, value in HIDDEN.items():
                self.assertNotIn(value, text, (name, key))
            self.assertNotIn(WEBSITE, text, name)

    def test_a_page_of_the_community_sees_empty_what_is_hidden(self):
        inert = InertShop(self.shop)
        self.assertEqual(str(inert.phone), "")
        self.assertEqual(str(inert.vat_number), "")
        self.assertEqual(str(inert.legal_name), "")
        self.assertEqual(str(inert.email), "")
        self.assertIn("Street", str(inert.address))
        Shop.objects.filter(pk=self.shop.pk).update(show_phone=True)
        self.shop.refresh_from_db()
        self.assertIn("0461", str(InertShop(self.shop).phone))

    def test_a_new_page_cannot_leak_by_reading_the_column_alone(self):
        """The one way to the details is details.shown. Under shops/, website/, templates/, config/ and the
        scripts of static/, in Python, templates, texts and JavaScript, any `x.phone`, `x.email`, `x.legal_name`,
        `x.vat_number`, `x.address` or `x.website` is either on a receiver that is not a shop (a profile, a form, the
        community, a Lightning address) or one of the few places that are for the owner or the association, named below. A
        page that reads `shop.phone` directly shows it whatever the owner chose, and fails here."""
        root = Path(__file__).resolve().parent.parent
        pattern = re.compile(r"""([\w\])"']+(?:\.\w+)*)\.(phone|email|legal_name|vat_number|address|website)\b""")
        not_shops = {"self", "profile", "community", "form", "outcome", "drill", "data", "answer", "onchain", "delivery", "say",
                     # A sale of the register and its on-chain address, a destination pasted in the settings (static/register*.js)
                     "entry", "sale", "mine", "place", "pasted"}
        # (file, receiver, detail): the owner's callback phone, the admin's own columns, the map's JSON, the comments that
        # name what must not be written
        named = {("shops/admin.py", "obj", "vat_number"), ("shops/admin.py", "row.shop", "phone"),
                 ("shops/helpdesk.py", "shop", "phone"), ("shops/details.py", "shop", "phone"),
                 ("static/map.js", "shop", "address"), ("static/map.js", "shop", "website"),
                 ("website/views.py", "shop.shop", "phone"), ("static/register-derive.js", "found", "address"),
                 # OpenStreetMap fills a field only when the shop has it empty: it reads the column to see that it is empty
                 ("shops/osm.py", "shop", "address"), ("shops/osm.py", "shop", "website"), ("shops/osm.py", "shop", "phone")}
        found = []
        for folder in ("shops", "website", "templates", "config", "static"):
            for path in (root / folder).rglob("*"):
                relative = path.relative_to(root).as_posix()
                if (not path.is_file() or path.suffix not in (".py", ".html", ".txt", ".js") or "/vendor/" in relative
                        or "/migrations/" in relative or "/test" in relative):
                    continue
                for number, line in enumerate(path.read_text().splitlines(), 1):
                    for receiver, detail in pattern.findall(line):
                        receiver = receiver.lstrip("\"'")
                        if receiver.rpartition(".")[2] in not_shops or (relative, receiver, detail) in named:
                            continue
                        found.append((relative, number, receiver, detail))
        self.assertEqual(found, [])


class DisplayPage(TestCase):
    def setUp(self):
        self.owner = an_owner()
        self.shop = make_shop(owner=self.owner)
        self.url = f"/en/shops/preview/{self.shop.id}/"
        self.client.force_login(self.owner)

    def test_it_shows_every_choice_with_its_value_and_a_preview(self):
        page = self.client.get(self.url)
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page["X-Robots-Tag"], "noindex, nofollow")
        text = page.content.decode()
        for name in details.CHOICES:
            self.assertIn(f'name="show_{name}"', text)
        for value in (ADDRESS, PHONE, EMAIL, LEGAL, VAT):
            self.assertIn(value, text)
        self.assertIn("What customers see", text)
        self.assertIn('<ul class="facts" id="preview-facts">', text)
        # The preview carries every filled line; the hidden ones are marked, which the script and the server both use
        for name in ("address", "website"):
            self.assertRegex(text, rf'<li data-detail="{name}">')
        for name in ("phone", "email", "legal_name", "vat_number"):
            self.assertRegex(text, rf'<li data-detail="{name}" hidden>')
        # Two ticks are on, as the starting values say
        self.assertRegex(text, r'name="show_website"[^>]*checked')
        self.assertRegex(text, r'name="show_address"[^>]*checked')
        self.assertNotRegex(text, r'name="show_phone"[^>]*checked')
        # The preview has no links that leave the page
        preview = text[text.index('id="preview-facts"'):text.index("</ul>", text.index('id="preview-facts"'))]
        self.assertNotIn("<a ", preview)

    def test_saving_changes_the_six_ticks_and_nothing_else(self):
        before = Shop.objects.values().get(pk=self.shop.pk)
        response = self.client.post(self.url, {"show_phone": "on", "show_vat_number": "on"})
        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        after = Shop.objects.values().get(pk=self.shop.pk)
        changed = {key for key in before if before[key] != after[key]}
        self.assertEqual(changed, {"show_website", "show_address", "show_phone", "show_vat_number"})
        self.shop.refresh_from_db()
        self.assertEqual([details.chosen(self.shop, name) for name in details.CHOICES], [False, False, True, False, False, True])
        page = self.client.get(self.url)
        self.assertContains(page, "Saved. This is what your customers see now.")
        self.assertRegex(page.content.decode(), r'<li data-detail="phone">')

    def test_the_name_is_not_a_choice(self):
        self.assertNotIn("show_name", ShopDisplayForm().fields)
        self.assertEqual(set(ShopDisplayForm().fields), {f"show_{name}" for name in details.CHOICES})

    def test_a_choice_for_an_empty_detail_is_kept(self):
        Shop.objects.filter(pk=self.shop.pk).update(phone="")
        page = self.client.get(self.url).content.decode()
        self.assertIn("(not written yet)", page)
        self.assertIn("is not written yet.", page)
        self.client.post(self.url, {"show_phone": "on", "show_website": "on", "show_address": "on"})
        self.shop.refresh_from_db()
        self.assertTrue(self.shop.show_phone)

    def test_the_details_form_does_not_carry_the_ticks(self):
        """Saving the details of a shop must never switch the ticks off: they are another form."""
        self.assertFalse({f"show_{name}" for name in details.CHOICES} & set(ShopDetailsForm().fields))
        self.assertFalse({f"show_{name}" for name in details.CHOICES} & set(ShopDetailsForm.Meta.fields))
        Shop.objects.filter(pk=self.shop.pk).update(show_phone=True, show_address=False)
        self.client.post(f"/en/shops/edit/{self.shop.id}/", {"name": NAME, "phone": "0461 1"})
        self.shop.refresh_from_db()
        self.assertEqual((self.shop.show_phone, self.shop.show_address, self.shop.show_website), (True, False, True))

    def test_only_the_owner_signed_in(self):
        anonymous = Client()
        self.assertEqual(anonymous.get(self.url).status_code, 302)
        self.assertEqual(anonymous.post(self.url, {"show_phone": "on"}).status_code, 302)
        other = Client()
        other.force_login(an_owner("other"))
        self.assertEqual(other.get(self.url).status_code, 404)
        self.assertEqual(other.post(self.url, {"show_phone": "on"}).status_code, 404)
        self.shop.refresh_from_db()
        self.assertFalse(self.shop.show_phone)

    def test_the_staff_is_a_visitor_here_as_everywhere(self):
        staff = Client()
        admin_session(staff)
        self.assertEqual(staff.get(self.url).status_code, 302)
        self.assertEqual(staff.post(self.url, {"show_phone": "on"}).status_code, 302)
        self.shop.refresh_from_db()
        self.assertFalse(self.shop.show_phone)

    def test_a_post_without_the_token_is_refused(self):
        guarded = Client(enforce_csrf_checks=True)
        guarded.force_login(self.owner)
        self.assertEqual(guarded.post(self.url, {"show_phone": "on"}).status_code, 403)
        self.shop.refresh_from_db()
        self.assertFalse(self.shop.show_phone)

    def test_a_vat_number_that_looks_wrong_is_said_here_too(self):
        Shop.objects.filter(pk=self.shop.pk).update(vat_number="IT12345678900")
        self.assertContains(self.client.get(self.url), "checks the others")
        Shop.objects.filter(pk=self.shop.pk).update(vat_number=VAT)
        self.assertNotContains(self.client.get(self.url), "checks the others")

    def test_a_ticked_box_for_an_empty_detail_says_it_shows_as_soon_as_it_is_written(self):
        Shop.objects.filter(pk=self.shop.pk).update(phone="", email="", show_phone=True, show_email=False)
        page = self.client.get(self.url).content.decode()
        ticked = "(not written yet)"
        # One line at the top counts what is missing, and each empty detail says so next to its box; the preview first
        self.assertEqual(page.count("details are not written yet."), 1)
        self.assertLess(page.index('name="show_phone"'), page.index("(not written yet)"))
        self.assertLess(page.index('id="preview-title"'), page.index('name="show_phone"'))
        self.assertLess(page.index(ticked), page.index('name="show_email"'))
        self.assertIn("A ticked box stays on for a detail you write later", page)
        # Where the detail is written, or the box is off, there is nothing to say
        self.assertNotIn(ticked, make_shop_page(self))

    def test_extra_fields_in_the_post_are_ignored(self):
        guarded = Client(enforce_csrf_checks=True)
        guarded.force_login(self.owner)
        guarded.get(self.url)
        token = guarded.cookies["csrftoken"].value
        guarded.post(self.url, {"csrfmiddlewaretoken": token, "show_phone": "on", "name": "Hacked", "phone": "000", "owner": "",
                                "suspended": "on", "listed": "", "show_website": "false"})
        self.shop.refresh_from_db()
        self.assertEqual((self.shop.name, self.shop.phone, self.shop.suspended, self.shop.listed, self.shop.owner_id),
                         (NAME, PHONE, False, True, self.owner.pk))
        self.assertEqual([details.chosen(self.shop, name) for name in details.CHOICES], [False, False, True, False, False, False])

    def test_html_in_the_values_is_escaped_for_the_owner_and_for_everybody(self):
        Shop.objects.filter(pk=self.shop.pk).update(legal_name="<script>alert(1)</script>", vat_number="<b>x</b>",
                                                    phone='"><img src=x onerror=1>', show_legal_name=True, show_vat_number=True,
                                                    show_phone=True)
        for page in (self.client.get(self.url), Client().get(f"/en/shops/s/{self.shop.id}/")):
            text = page.content.decode()
            for raw in ("<script>alert(1)", "<img src=x", "<b>x</b>"):
                self.assertNotIn(raw, text)
            self.assertIn("&lt;script&gt;alert(1)", text)

    def test_the_panel_and_the_edit_page_lead_to_it(self):
        self.assertContains(self.client.get("/en/shops/"), f'href="{self.url}"')
        self.assertContains(self.client.get(f"/en/shops/edit/{self.shop.id}/"), f'href="{self.url}"')

    def test_every_language_has_it(self):
        self.assertContains(self.client.get(f"/it/shops/preview/{self.shop.id}/"), "Cosa vedono i clienti")
        with override("de"):
            for english, german in (("What customers see", "Was Ihre Kunden sehen"), ("VAT number", "USt-IdNr."),
                                    ("Business name", "Firmenname")):
                self.assertEqual(gettext(english), german)


def make_shop_page(case):
    """The «What customers see» page of a shop that has the phone written and the box ticked."""
    Shop.objects.filter(pk=case.shop.pk).update(phone=PHONE, email=EMAIL, show_phone=True)
    return case.client.get(case.url).content.decode()


class OwnerForm(TestCase):
    def setUp(self):
        self.owner = an_owner()
        self.shop = make_shop(owner=self.owner, legal_name="", vat_number="")
        self.client.force_login(self.owner)
        self.url = f"/en/shops/edit/{self.shop.id}/"

    def save(self, **fields):
        return self.client.post(self.url, {"name": NAME, **fields}, follow=True)

    def test_the_business_details_are_saved_and_the_number_is_tidied(self):
        page = self.save(legal_name=f"  {LEGAL} ", vat_number=" it 01.234.567.89-" + VAT[-1])
        self.shop.refresh_from_db()
        self.assertEqual((self.shop.legal_name, self.shop.vat_number), (LEGAL, VAT))
        self.assertContains(page, "Saved.")
        self.assertNotContains(page, "may have a mistake")

    def test_a_script_saving_without_end_is_stopped_and_keeps_what_was_typed(self):
        from .tests import steady_limits

        with steady_limits(), patch("shops.views.EDITS_PER_USER", 1):
            self.save(legal_name=LEGAL)
            response = self.client.post(self.url, {"name": "Another name"})
        self.assertEqual(response.status_code, 429)
        self.assertContains(response, "wait a few minutes", status_code=429)
        self.assertContains(response, "Another name", status_code=429)
        self.shop.refresh_from_db()
        self.assertEqual(self.shop.name, NAME)

    def test_a_number_of_a_strange_shape_is_saved_with_a_word(self):
        page = self.save(vat_number="7654Z")
        self.shop.refresh_from_db()
        self.assertEqual(self.shop.vat_number, "7654Z")
        self.assertContains(page, "We saved your VAT number, but it may have a mistake.")
        self.assertContains(page, "two letters of the country")
        # The number is not in the message
        self.assertNotContains(page, "7654Z")

    def test_an_italian_number_with_a_wrong_check_is_saved_with_a_word(self):
        page = self.save(vat_number="IT12345678900")
        self.shop.refresh_from_db()
        self.assertEqual(self.shop.vat_number, "IT12345678900")
        self.assertContains(page, "the last one checks the others")

    def test_a_number_too_long_for_the_column_is_refused_like_any_text(self):
        response = self.client.post(self.url, {"name": NAME, "vat_number": "IT" + "1" * 40})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "longer than a VAT number can be")
        self.shop.refresh_from_db()
        self.assertEqual(self.shop.vat_number, "")

    def test_a_business_name_goes_through_the_rules_of_the_other_texts(self):
        self.save(legal_name="Red‮ Ltd")
        self.shop.refresh_from_db()
        self.assertEqual(self.shop.legal_name, "Red Ltd")
        response = self.client.post(self.url, {"name": NAME, "legal_name": "x" * 201})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "at most 200 characters")

    def test_a_seed_phrase_is_refused_in_the_new_fields_too(self):
        seed = " ".join(["abandon"] * 11 + ["about"])
        for field in ("legal_name", "vat_number"):
            response = self.client.post(self.url, {"name": NAME, field: seed})
            self.assertEqual(response.status_code, 200, field)
            self.shop.refresh_from_db()
            self.assertEqual(getattr(self.shop, field), "", field)

    def test_the_form_says_where_the_choice_is(self):
        page = self.client.get(self.url).content.decode()
        self.assertIn('name="legal_name"', page)
        self.assertIn('name="vat_number"', page)
        self.assertIn("only if you turn it on", page)
        self.assertNotIn("public email", page.lower())
        self.assertNotIn("public phone", page.lower())

    def test_a_new_shop_takes_them_too(self):
        with patch("shops.views.refusal", return_value=None):
            response = self.client.post("/en/shops/new/", {
                "name": "Second", "lightning_address": "mario@example.org", "legal_name": LEGAL, "vat_number": "12 34"},
                follow=True)
        shop = Shop.objects.get(name="Second")
        self.assertEqual((shop.legal_name, shop.vat_number), (LEGAL, "1234"))
        self.assertContains(response, "may have a mistake")
        # A new shop starts with the starting values
        self.assertEqual([details.chosen(shop, name) for name in details.CHOICES], [True, True, False, False, False, False])


def admin_post_data(response):
    """What the browser would send back for the change form just shown: every field as it is."""
    form = response.context["adminform"].form
    data = {}
    for name, field in form.fields.items():
        value = form.initial.get(name, field.initial)
        if isinstance(field, forms.BooleanField):
            if value:
                data[name] = "on"
        elif value is not None:
            data[name] = str(value)
    data.update({"closures-TOTAL_FORMS": "0", "closures-INITIAL_FORMS": "0", "closures-MIN_NUM_FORMS": "0",
                 "closures-MAX_NUM_FORMS": "1000"})
    return data


class AdminForm(TestCase):
    def setUp(self):
        admin_session(self.client)

    def test_the_association_writes_the_two_fields_and_a_strange_number_is_saved_with_a_warning(self):
        shop = make_shop(legal_name="", vat_number="", name="Loaded")
        url = f"/admin/shops/shop/{shop.id}/change/"
        page = self.client.get(url)
        self.assertContains(page, 'name="legal_name"')
        self.assertContains(page, 'name="vat_number"')
        data = {**admin_post_data(page), "legal_name": LEGAL, "vat_number": "xx 123 456"}
        response = self.client.post(url, data, follow=True)
        shop.refresh_from_db()
        self.assertEqual((shop.legal_name, shop.vat_number), (LEGAL, "XX123456"))
        self.assertContains(response, "may have a mistake")

    def test_a_good_number_is_saved_without_a_word(self):
        shop = make_shop(legal_name="", vat_number="", name="Loaded")
        url = f"/admin/shops/shop/{shop.id}/change/"
        data = {**admin_post_data(self.client.get(url)), "vat_number": VAT.lower()}
        response = self.client.post(url, data, follow=True)
        shop.refresh_from_db()
        self.assertEqual(shop.vat_number, VAT)
        self.assertNotContains(response, "may have a mistake")

    def test_the_ticks_belong_to_the_owner(self):
        owned = make_shop(owner=an_owner(), name="Owned")
        loaded = make_shop(name="Loaded")
        for shop, editable in ((owned, False), (loaded, True)):
            page = self.client.get(f"/admin/shops/shop/{shop.id}/change/").content.decode()
            for name in details.CHOICES:
                self.assertEqual(f'name="show_{name}"' in page, editable, (shop.name, name))

    def test_the_association_sets_the_ticks_of_a_shop_without_an_owner(self):
        shop = make_shop(name="Loaded")
        url = f"/admin/shops/shop/{shop.id}/change/"
        self.client.post(url, {**admin_post_data(self.client.get(url)), "show_phone": "on"})
        shop.refresh_from_db()
        self.assertTrue(shop.show_phone)

    def test_two_actions_show_or_hide_the_contacts_of_shops_without_an_owner_only(self):
        loaded = make_shop(name="Loaded")
        owned = make_shop(owner=an_owner(), name="Owned", show_phone=False, show_email=False)
        both = [loaded.id, owned.id]
        response = self.client.post("/admin/shops/shop/", {"action": "show_contacts", "_selected_action": both}, follow=True)
        self.assertContains(response, "1 shops now show their phone and email.")
        loaded.refresh_from_db()
        owned.refresh_from_db()
        self.assertEqual((loaded.show_phone, loaded.show_email), (True, True))
        self.assertEqual((owned.show_phone, owned.show_email), (False, False))
        self.client.post("/admin/shops/shop/", {"action": "hide_contacts", "_selected_action": both}, follow=True)
        loaded.refresh_from_db()
        self.assertEqual((loaded.show_phone, loaded.show_email), (False, False))

    def test_the_list_filter_finds_the_numbers_that_look_wrong(self):
        make_shop(name="Fine")
        make_shop(name="Odd", vat_number="123")
        make_shop(name="Quiet", vat_number="")
        listing = self.client.get("/admin/shops/shop/?vat=doubtful").content.decode()
        self.assertIn(">Odd<", listing)
        self.assertNotIn(">Fine<", listing)
        self.assertNotIn(">Quiet<", listing)
        written = self.client.get("/admin/shops/shop/?vat=written").content.decode()
        self.assertIn(">Fine<", written)
        self.assertNotIn(">Quiet<", written)
        self.assertIn(">Quiet<", self.client.get("/admin/shops/shop/?vat=empty").content.decode())

    def test_the_admin_can_search_by_business_name_and_number(self):
        make_shop(name="Loaded")
        self.assertContains(self.client.get("/admin/shops/shop/?q=Ltd"), ">Loaded<")
        self.assertContains(self.client.get(f"/admin/shops/shop/?q={VAT}"), ">Loaded<")


def staff_with(*codenames):
    """A staff account that has only these permissions on the shops, signed in to the admin."""
    user = get_user_model().objects.create_user("staffer", password="x", is_staff=True)
    user.user_permissions.add(*Permission.objects.filter(codename__in=codenames))
    client = Client()
    client.force_login(user)
    session = client.session
    session[ADMIN_SESSION] = True
    session.save()
    return client


class ViewOnlyStaff(TestCase):
    """The actions of the shop list change shops: a staff account that can only look at them cannot run any."""

    def post(self, client, action, *shops):
        return client.post("/admin/shops/shop/", {"action": action, "_selected_action": [shop.id for shop in shops], "index": "0"},
                           follow=True)

    def test_an_account_that_can_only_view_cannot_show_or_hide_the_contacts(self):
        loaded = make_shop(name="Loaded")
        client = staff_with("view_shop")
        page = client.get("/admin/shops/shop/")
        self.assertEqual(page.status_code, 200)
        for action in ("show_contacts", "hide_contacts", "revoke_certificate", "allow_certificate"):
            self.assertNotContains(page, f'value="{action}"')
            self.post(client, action, loaded)
        loaded.refresh_from_db()
        self.assertEqual((loaded.show_phone, loaded.show_email), (False, False))
        self.post(client, "show_contacts", loaded)
        loaded.refresh_from_db()
        self.assertFalse(loaded.show_phone)

    def test_an_account_that_can_only_view_cannot_post_the_change_form(self):
        loaded = make_shop(name="Loaded")
        client = staff_with("view_shop")
        response = client.post(f"/admin/shops/shop/{loaded.id}/change/", {"name": "x", "show_phone": "on"})
        self.assertEqual(response.status_code, 403)
        loaded.refresh_from_db()
        self.assertEqual((loaded.name, loaded.show_phone), ("Loaded", False))

    def test_an_account_that_can_change_runs_them(self):
        loaded = make_shop(name="Loaded")
        client = staff_with("view_shop", "change_shop")
        self.assertContains(client.get("/admin/shops/shop/"), 'value="show_contacts"')
        self.post(client, "show_contacts", loaded)
        loaded.refresh_from_db()
        self.assertEqual((loaded.show_phone, loaded.show_email), (True, True))

    def test_a_hand_made_post_cannot_tick_for_an_owned_shop(self):
        """On a shop with an owner the ticks are not in the form the admin builds, so a post that carries them changes
        nothing, and a post that clears the owner and ticks in one go does not tick either."""
        owned = make_shop(owner=an_owner(), name="Owned")
        client = staff_with("view_shop", "change_shop")
        url = f"/admin/shops/shop/{owned.id}/change/"
        data = {**admin_post_data(client.get(url)), "owner": "", "show_phone": "on", "show_vat_number": "on"}
        client.post(url, data, follow=True)
        owned.refresh_from_db()
        self.assertEqual((owned.owner_id, owned.show_phone, owned.show_vat_number), (None, False, False))


class AdminVat(TestCase):
    def setUp(self):
        admin_session(self.client)
        self.client.raise_request_exception = False

    def save(self, shop, number):
        url = f"/admin/shops/shop/{shop.id}/change/"
        return self.client.post(url, {**admin_post_data(self.client.get(url)), "vat_number": number}, follow=True)

    def test_a_capital_that_grows_is_refused_by_the_form_and_not_by_the_database(self):
        shop = make_shop(name="Loaded", vat_number="")
        response = self.save(shop, "ß" * 17)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "longer than a VAT number can be")
        shop.refresh_from_db()
        self.assertEqual(shop.vat_number, "")
        self.save(shop, "ß" * 16)
        shop.refresh_from_db()
        self.assertEqual(shop.vat_number, "SS" * 16)

    def test_the_admin_keeps_the_number_as_the_owners_form_does(self):
        shop = make_shop(name="Loaded", vat_number="")
        self.save(shop, "it \u202e012.345.678-97")
        shop.refresh_from_db()
        self.assertEqual(shop.vat_number, "IT01234567897")

    def test_the_search_finds_a_number_written_with_dots_dashes_and_spaces(self):
        make_shop(name="Loaded")
        make_shop(name="Other", vat_number="DE123456789", legal_name="Another")
        for term in (VAT, VAT.lower(), "it 012.345.678-97", "IT-0123-45678-97", "012.345.678-97"[:7]):
            listing = self.client.get("/admin/shops/shop/", {"q": term}).content.decode()
            self.assertIn(">Loaded<", listing, term)
            self.assertNotIn(">Other<", listing, term)
        # An ordinary search is the ordinary search, and the filters still hold
        self.assertIn(">Other<", self.client.get("/admin/shops/shop/", {"q": "another"}).content.decode())
        filtered = self.client.get("/admin/shops/shop/", {"q": "it 012.345.678-97", "vat": "empty"}).content.decode()
        self.assertNotIn(">Loaded<", filtered)

    def test_a_tick_in_the_list_does_not_repeat_the_warning_about_the_number(self):
        shop = make_shop(name="Loaded", vat_number="123")
        response = self.client.post("/admin/shops/shop/", {
            "form-TOTAL_FORMS": "1", "form-INITIAL_FORMS": "1", "form-MIN_NUM_FORMS": "0", "form-MAX_NUM_FORMS": "1000",
            "form-0-id": str(shop.pk), "form-0-suspended": "on", "form-0-closed_on": "", "_save": "Save"}, follow=True)
        shop.refresh_from_db()
        self.assertTrue(shop.suspended)
        self.assertNotContains(response, "may have a mistake")
        # The form that writes the number still says it, and only when the number was written
        url = f"/admin/shops/shop/{shop.id}/change/"
        again = self.client.post(url, {**admin_post_data(self.client.get(url)), "name": "Renamed"}, follow=True)
        self.assertNotContains(again, "may have a mistake")
        written = self.client.post(url, {**admin_post_data(self.client.get(url)), "vat_number": "456"}, follow=True)
        self.assertContains(written, "may have a mistake")


class InertShopForPages(TestCase):
    """The shop that a page of the community reads (website/views.py): its texts are inert, and it reaches nothing else."""

    def render(self, source, shop):
        from django.template import engines

        return engines["django"].from_string(source).render({"shop": InertShop(shop)})

    def test_the_shop_behind_it_is_out_of_reach(self):
        shop = make_shop(owner=an_owner())
        text = self.render("[{{ shop.shop.phone }}][{{ shop.owner.username }}][{{ shop.owner }}][{{ shop.staff_registers }}]"
                           "[{{ shop.get_mode_display }}]", shop)
        self.assertEqual(text, "[][][][][]")
        # What starts with an underscore is not passed on, from Python either: the shop behind is not an attribute to read
        with self.assertRaises(AttributeError):
            InertShop(shop)._anything
        self.assertNotIn("0461", text)

    def test_public_details_arrive_inert(self):
        shop = make_shop(show_legal_name=True, show_phone=True, legal_name="[x](javascript:alert(1))", phone="<b>1</b>")
        inert = InertShop(shop)
        self.assertEqual(set(inert.public_details), {"website", "address", "phone", "legal_name"})
        for value in inert.public_details.values():
            self.assertNotRegex(str(value), r"[\[\]()<>:/]")
        self.assertIn("&#91;x&#93;&#40;javascript", str(inert.public_details["legal_name"]))
        self.assertEqual(str(InertShop(make_shop(name="Two", show_address=False)).public_place), CITY)

    def test_what_the_pages_use_still_works(self):
        shop = make_shop(discount=7, show_phone=False)
        text = self.render("{{ shop.name }}|{{ shop.city }}|{{ shop.discount }}|{{ shop.id }}|{{ shop.external_id }}|{{ shop.phone }}", shop)
        self.assertEqual(text, f"{NAME}|{CITY}|7|{shop.id}||")


class GoingBack(TestCase):
    """deploy/release.sh puts the previous image back on a database this release migrated (shops/test_going_back.py): that
    image writes a shop without naming the new columns, so each of them, NOT NULL as it is, says its starting value in the
    database too, the same as the one a new shop gets."""
    NEW = ("legal_name", "vat_number", *moving.SHOWN)

    def test_each_new_column_has_a_db_default_equal_to_its_python_default(self):
        for name in self.NEW:
            field = Shop._meta.get_field(name)
            self.assertFalse(field.null, name)
            self.assertEqual(field.db_default, field.get_default(), name)

    def test_the_database_itself_holds_the_default_of_each(self):
        with connection.cursor() as cursor:
            columns = {column.name: column for column in connection.introspection.get_table_description(cursor, "shops_shop")}
        for name in self.NEW:
            self.assertIsNotNone(columns[name].default, name)
            self.assertFalse(columns[name].null_ok, name)

    def test_a_row_inserted_without_them_starts_as_a_new_shop_does(self):
        quote = connection.ops.quote_name
        # Only the columns of the shop table that have no default in the database are named: the previous image names those
        # and the ones before this release; here, the columns that have none, whoever added them
        with connection.cursor() as cursor:
            columns = connection.introspection.get_table_description(cursor, "shops_shop")
        named = [column.name for column in columns if column.default is None and not column.null_ok]
        values = {"id": Shop._meta.pk.get_db_prep_save(uuid.uuid4(), connection), "name": "Written by the old image"}
        for name in named:
            field = next(field for field in Shop._meta.concrete_fields if field.column == name)
            values.setdefault(name, field.get_db_prep_save(timezone.now() if name == "created" else field.get_default(), connection))
        with connection.cursor() as cursor:
            cursor.execute(f"INSERT INTO shops_shop ({', '.join(map(quote, values))}) VALUES ({', '.join(['%s'] * len(values))})",
                           list(values.values()))
        shop = Shop.objects.get(name="Written by the old image")
        self.assertEqual((shop.legal_name, shop.vat_number), ("", ""))
        self.assertEqual([details.chosen(shop, name) for name in details.CHOICES], [True, True, False, False, False, False])


class ImportShops(TestCase):
    def run_file(self, rows):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as file:
            json.dump(rows, file)
        self.addCleanup(Path(file.name).unlink)
        out = io.StringIO()
        call_command("import_shops", file.name, stdout=out)
        return out.getvalue()

    def test_the_two_fields_go_in_with_the_others(self):
        out = self.run_file([{"external_id": 1, "name": "Loaded", "legal_name": LEGAL, "vat_number": "it 01234 567 897"}])
        shop = Shop.objects.get(external_id="1")
        self.assertEqual((shop.legal_name, shop.vat_number), (LEGAL, "IT01234567897"))
        self.assertNotIn("mistake", out)
        # Loaded shops start with the starting values: the number is not shown
        self.assertEqual([details.chosen(shop, name) for name in details.CHOICES], [True, True, False, False, False, False])

    def test_running_it_again_updates_and_does_not_duplicate(self):
        self.run_file([{"external_id": 1, "name": "Loaded", "vat_number": "DE123456789"}])
        self.run_file([{"external_id": 1, "name": "Loaded", "vat_number": "DE987654321", "legal_name": "New name"}])
        self.assertEqual(Shop.objects.count(), 1)
        shop = Shop.objects.get()
        self.assertEqual((shop.vat_number, shop.legal_name), ("DE987654321", "New name"))

    def test_a_shop_that_has_an_owner_keeps_what_its_owner_wrote(self):
        shop = make_shop(owner=an_owner(), external_id="5", legal_name="Mine", vat_number="DE111111111")
        self.run_file([{"external_id": 5, "name": "Loaded", "legal_name": "Theirs", "vat_number": "DE222222222"}])
        shop.refresh_from_db()
        self.assertEqual((shop.legal_name, shop.vat_number), ("Mine", "DE111111111"))

    def test_a_strange_number_goes_in_with_a_word_that_does_not_repeat_the_number(self):
        out = self.run_file([{"external_id": 1, "name": "Loaded", "vat_number": "98765"}])
        self.assertEqual(Shop.objects.get().vat_number, "98765")
        self.assertIn("Shop 1: the VAT number may have a mistake", out)
        self.assertNotIn("98765", out)

    def test_the_word_about_a_strange_number_has_not_even_its_first_letters(self):
        out = self.run_file([{"external_id": 1, "name": "Loaded", "vat_number": "zz 123 456"}])
        self.assertIn("the VAT number may have a mistake: look at it in the admin.", out)
        self.assertNotIn("ZZ", out.upper().replace("MISTAKE", "").replace("ADMIN", ""))
        self.assertNotIn("123", out)
        self.assertEqual(Shop.objects.get().vat_number, "ZZ123456")

    def test_the_same_checks_as_the_other_fields(self):
        for bad in ({"legal_name": "x" * 201}, {"vat_number": "1" * 40}):
            with self.assertRaises(CommandError):
                self.run_file([{"external_id": 1, "name": "Loaded", **bad}])
        self.assertFalse(Shop.objects.exists())


class Moving(TestCase):
    def export(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "shops.json"
        call_command("export_shops", str(path), stdout=io.StringIO())
        return json.loads(path.read_text())

    def importing(self, document, *options):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        source = Path(folder.name) / "in.json"
        source.write_text(json.dumps(document))
        out = io.StringIO()
        call_command("import_moved_shops", str(source), *options, "--owners", str(Path(folder.name) / "owners.csv"), stdout=out)
        return out.getvalue()

    def test_the_file_is_version_2_and_carries_the_details_and_the_choices(self):
        make_shop(owner=an_owner(), show_phone=True, show_address=False)
        document = self.export()
        self.assertEqual(document["version"], 2)
        row = document["shops"][0]
        self.assertEqual((row["legal_name"], row["vat_number"]), (LEGAL, VAT))
        self.assertEqual({key: row[key] for key in moving.SHOWN},
                         {"show_website": True, "show_address": False, "show_phone": True, "show_email": False,
                          "show_legal_name": False, "show_vat_number": False})

    def test_the_move_brings_the_details_and_the_choices(self):
        make_shop(owner=an_owner(), show_phone=True, show_address=False, show_vat_number=True)
        document = self.export()
        Shop.objects.all().delete()
        self.importing(document, "--apply")
        shop = Shop.objects.get()
        self.assertEqual((shop.legal_name, shop.vat_number), (LEGAL, VAT))
        self.assertEqual([details.chosen(shop, name) for name in details.CHOICES], [True, False, True, False, False, True])

    def test_export_import_export_gives_the_same_details(self):
        make_shop(owner=an_owner(), show_email=True, show_legal_name=True)
        first = self.export()
        Shop.objects.all().delete()
        self.importing(first, "--apply")
        second = self.export()
        keep = ("legal_name", "vat_number", *moving.SHOWN)
        self.assertEqual({key: first["shops"][0][key] for key in keep}, {key: second["shops"][0][key] for key in keep})

    def test_a_file_of_version_1_is_read_too_and_its_shops_keep_phone_and_email_on_show(self):
        make_shop(owner=an_owner(), name="Owned")
        make_shop(name="Loaded", external_id="3")
        document = self.export()
        document["version"] = 1
        for row in document["shops"]:
            for key in moving.NEW_IN_2:
                del row[key]
        Shop.objects.all().delete()
        out = self.importing(document, "--apply")
        self.assertIn("2 moved", out)
        owned, loaded = Shop.objects.get(name="Owned"), Shop.objects.get(name="Loaded")
        self.assertEqual((owned.legal_name, owned.vat_number), ("", ""))
        self.assertEqual([details.chosen(owned, name) for name in details.CHOICES], [True, True, True, True, False, False])
        self.assertEqual([details.chosen(loaded, name) for name in details.CHOICES], [True, True, False, False, False, False])

    def test_a_file_of_version_2_needs_every_key_and_a_file_of_version_1_none_of_the_new(self):
        make_shop(owner=an_owner())
        document = self.export()
        broken = json.loads(json.dumps(document))
        del broken["shops"][0]["vat_number"]
        with self.assertRaisesMessage(CommandError, "missing keys ['vat_number']"):
            self.importing(broken)
        old = json.loads(json.dumps(document))
        old["version"] = 1
        with self.assertRaisesMessage(CommandError, "unexpected keys"):
            self.importing(old)
        future = json.loads(json.dumps(document))
        future["version"] = 3
        with self.assertRaisesMessage(CommandError, "reads versions 1, 2"):
            self.importing(future)

    def test_the_new_keys_are_checked_for_their_kind(self):
        make_shop(owner=an_owner())
        for key, value in (("vat_number", 5), ("legal_name", ["x"]), ("show_phone", "yes"), ("show_address", 1)):
            document = self.export()
            document["shops"][0][key] = value
            with self.assertRaises(CommandError, msg=key):
                self.importing(document)

    def test_a_strange_number_arrives_as_written_with_a_note_that_does_not_repeat_it(self):
        make_shop(owner=an_owner(), vat_number="7654Z")
        document = self.export()
        Shop.objects.all().delete()
        out = self.importing(document, "--apply")
        self.assertEqual(Shop.objects.get().vat_number, "7654Z")
        self.assertIn("Its VAT number may have a mistake", out)
        self.assertNotIn("7654Z", out)

    def test_the_note_about_a_strange_number_has_not_even_its_first_letters(self):
        make_shop(owner=an_owner(), vat_number="ZZ123 SECRET")
        document = self.export()
        Shop.objects.all().delete()
        out = self.importing(document, "--apply")
        self.assertIn("Its VAT number may have a mistake: look at it in the admin. It arrives as written.", out)
        # Not "123" alone: a random part of the output (a code, a time) can hold those digits
        for word in ("ZZ", "SECRET", "ZZ123", "letters", "country"):
            self.assertNotIn(word, out)
        self.assertEqual(Shop.objects.get().vat_number, "ZZ123SECRET")

    def test_values_too_long_for_the_columns_refuse_the_shop_and_not_the_import(self):
        make_shop(owner=an_owner(), name="Long one", vat_number="")
        document = self.export()
        document["shops"][0]["vat_number"] = "X" * 40
        document["shops"][0]["legal_name"] = "Y" * 300
        Shop.objects.all().delete()
        out = self.importing(document, "--apply")
        self.assertIn("not moved", out)
        self.assertIn("vat_number", out)
        self.assertFalse(Shop.objects.exists())
        for value in ("X" * 40, "Y" * 300):
            self.assertNotIn(value, out)

    def test_a_dry_run_writes_nothing_and_prints_neither_the_number_nor_the_name(self):
        make_shop(owner=an_owner())
        document = self.export()
        Shop.objects.all().delete()
        out = self.importing(document)
        self.assertFalse(Shop.objects.exists())
        for value in (VAT, LEGAL):
            self.assertNotIn(value, out)


class Privacy(TestCase):
    """The business name and the VAT number go to the association's database and to the owner's own screens, and nowhere
    else: not to the helper's diagnosis, not to the logs, not to the reports of the commands."""

    def setUp(self):
        self.owner = an_owner()
        self.shop = make_shop(owner=self.owner, lightning_address="", lightning=False,
                              show_legal_name=True, show_vat_number=True)
        self.records = []

        class Collect(logging.Handler):
            def emit(handler, record):
                self.records.append(record.getMessage())

        handler = Collect(level=logging.DEBUG)
        root = logging.getLogger()
        before = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        self.addCleanup(lambda: (root.removeHandler(handler), root.setLevel(before)))

    def diagnose(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with patch("shops.lnaddress.fetch", side_effect=requests.ConnectionError):
            try:
                call_command("diagnose_shop", str(self.shop.id), *args, stdout=out, stderr=err)
            except SystemExit:
                pass
        return out.getvalue() + err.getvalue()

    def test_the_diagnosis_leaves_them_out_in_words_and_in_json(self):
        for args in ((), ("--json",), ("--language", "it")):
            text = self.diagnose(*args)
            self.assertIn(NAME, text)
            for value in (LEGAL, VAT, VAT[2:]):
                self.assertNotIn(value, text, args)

    def test_a_shop_cannot_be_found_by_them_either(self):
        for text in (VAT, LEGAL):
            out, err = io.StringIO(), io.StringIO()
            with self.assertRaises(SystemExit):
                call_command("diagnose_shop", text, stdout=out, stderr=err)

    def test_nothing_the_site_does_with_the_details_writes_them_in_a_log(self):
        self.client.force_login(self.owner)
        self.client.post(f"/en/shops/edit/{self.shop.id}/", {"name": NAME, "legal_name": "Secret Name Ltd", "vat_number": "IT123"})
        self.client.get(f"/en/shops/preview/{self.shop.id}/")
        self.client.post(f"/en/shops/preview/{self.shop.id}/", {"show_vat_number": "on"})
        Client().get(f"/en/shops/s/{self.shop.id}/")
        Client().get("/shops.json")
        self.diagnose("--json")
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "out.json"
        out = io.StringIO()
        call_command("export_shops", str(path), stdout=out)
        self.assertNotIn("Secret Name", out.getvalue())
        self.assertNotIn("IT123", out.getvalue())
        joined = "\n".join(self.records)
        for word in ("Secret Name", "IT123", LEGAL, VAT):
            self.assertNotIn(word, joined)

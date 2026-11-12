"""The merchant's path in the panel (shops/journey.py): each milestone in its states, the certificate and what it waits
for, the map card, and a panel that asks no other server."""
import re
import socket
from datetime import date
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.template.loader import render_to_string
from django.test import TestCase, override_settings
from django.utils import timezone
from django.utils.translation import override

from . import first_day, journey
from .models import LightningStatus, Shop, Trainee

CALLED_OUT = AssertionError("The panel must not ask another server")


def nobody_outside():
    """Any HTTP request or socket opened while the panel loads fails the test."""
    return (patch("requests.sessions.Session.request", side_effect=CALLED_OUT),
            patch.object(socket, "create_connection", side_effect=CALLED_OUT))


def block(page, shop):
    html = page.content.decode()
    start = html.find(f'id="journey-{shop.id}"')
    if start < 0:
        return ""
    # The path ends where the register for the staff begins: the first day inside it has its own <details>
    return html[start:html.index('<details class="handover"', start)]


def milestones(html):
    return dict(re.findall(r'data-milestone="(\w+)" data-state="(\w+)"', html))


@override_settings(TELEGRAM_BOT_TOKEN="")
class Milestones(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = get_user_model().objects.create(username="k")
        self.shop = Shop.objects.create(owner=self.owner, name="Bar", 
                                        lightning_address="bar@example.org", latitude=1, longitude=2)

    def watch_ok(self):
        now = timezone.now()
        Shop.objects.filter(pk=self.shop.pk).update(lightning_status=LightningStatus.OK, lightning_checked=now,
                                                    lightning_worked=now, lightning_checked_address="bar@example.org")
        self.shop.refresh_from_db()

    def steps(self, shop=None):
        path = journey.summary(shop or self.shop)
        return path, {step["key"]: step["state"] for step in path["steps"]}

    def test_a_new_shop(self):
        path, states = self.steps()
        self.assertEqual(states, {"lightning": "waiting", "first_day": "todo", "certificate": "todo"})
        # The address is about to be checked: the next step is the first day, the merchant's to walk
        self.assertEqual((path["done"], path["total"], path["next"]), (0, 3, "first_day"))
        self.assertFalse(path["complete"])

    def test_lightning_in_its_states(self):
        self.watch_ok()
        self.assertEqual(self.steps()[1]["lightning"], "done")
        Shop.objects.filter(pk=self.shop.pk).update(lightning_status=LightningStatus.NO_ANSWER,
                                                    lightning_since=timezone.now())
        self.shop.refresh_from_db()
        self.assertEqual(self.steps()[1]["lightning"], "todo")
        Shop.objects.filter(pk=self.shop.pk).update(suspended=True)
        self.shop.refresh_from_db()
        self.assertEqual(self.steps()[1]["lightning"], "off")

    def test_the_first_day_is_one_milestone(self):
        now = timezone.now()
        self.watch_ok()
        Shop.objects.filter(pk=self.shop.pk).update(first_sale=now, sign_printed=now)
        self.shop.refresh_from_db()
        path, states = self.steps()
        self.assertEqual(states["first_day"], "todo")
        self.assertEqual(path["next"], "first_day")
        Shop.objects.filter(pk=self.shop.pk).update(register_shared=now)
        self.shop.refresh_from_db()
        path, states = self.steps()
        self.assertEqual(states["first_day"], "done")
        self.assertEqual(path["next"], "certificate")
        Shop.objects.filter(pk=self.shop.pk).update(certified_month=date(2026, 10, 1))
        self.shop.refresh_from_db()
        path, states = self.steps()
        self.assertTrue(path["complete"])
        self.assertIsNone(path["next"])

    def test_no_milestone_about_payments(self):
        self.assertEqual([step["key"] for step in self.steps()[0]["steps"]], ["lightning", "first_day", "certificate"])

    def test_the_certificate_says_what_it_waits_for(self):
        certificate = self.steps()[0]["steps"][2]
        self.assertEqual((certificate["state"], certificate["needs_lightning"], certificate["needs_pass"]), ("todo", True, True))
        self.watch_ok()
        certificate = self.steps()[0]["steps"][2]
        self.assertEqual((certificate["needs_lightning"], certificate["needs_pass"]), (False, True))
        Trainee.objects.create(user=self.owner, base_earned=timezone.now())
        certificate = self.steps()[0]["steps"][2]
        self.assertEqual((certificate["needs_lightning"], certificate["needs_pass"]), (False, False))
        # The address the guardian found working is an older one: it does not count
        Shop.objects.filter(pk=self.shop.pk).update(lightning_address="new@example.org")
        self.shop.refresh_from_db()
        self.assertTrue(self.steps()[0]["steps"][2]["needs_lightning"])

    def test_the_certificate_done_and_revoked(self):
        Shop.objects.filter(pk=self.shop.pk).update(certified_month=date(2026, 10, 1))
        self.shop.refresh_from_db()
        path, states = self.steps()
        self.assertEqual(states["certificate"], "done")
        self.assertTrue(path["card"]["certified"])
        Shop.objects.filter(pk=self.shop.pk).update(certified_month=None, certified_revoked=True)
        self.shop.refresh_from_db()
        self.assertEqual(self.steps()[1]["certificate"], "off")

    def test_without_the_badge_no_certificate_milestone(self):
        with patch("shops.certified.enabled", return_value=False):
            self.assertNotIn("certificate", self.steps()[1])

    def test_the_map_card(self):
        self.assertEqual(self.steps()[0]["card"]["on_map"], True)
        for change, why in (({"latitude": None}, "position"), ({"listed": False}, "unlisted"),
                            ({"suspended": True}, "suspended")):
            Shop.objects.filter(pk=self.shop.pk).update(**change)
            self.shop.refresh_from_db()
            self.assertEqual(self.steps()[0]["card"], {"on_map": False, "why": why})

    def test_no_path_without_a_register_of_this_site_or_for_a_historic_shop(self):
        self.assertTrue(journey.applies(self.shop))
        self.assertFalse(journey.applies(Shop(name="Imported", lightning=True)))
        self.assertFalse(journey.applies(Shop(name="Old", lightning_address="old@example.org", closed_on=date(2024, 6, 30))))


@override_settings(TELEGRAM_BOT_TOKEN="")
class Panel(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = get_user_model().objects.create(username="k")
        self.client.force_login(self.owner)
        self.shop = Shop.objects.create(owner=self.owner, name="Bar", 
                                        lightning_address="bar@example.org", latitude=1, longitude=2, discount=10,
                                        city="Town")

    def panel(self, language="en"):
        """The panel as the owner sees it, asking no other server."""
        request, connect = nobody_outside()
        with request as asked, connect as opened:
            page = self.client.get(f"/{language}/shops/")
        asked.assert_not_called()
        opened.assert_not_called()
        self.assertEqual(page.status_code, 200)
        return page

    def test_a_new_shop_walks_its_path(self):
        html = block(self.panel(), self.shop)
        self.assertIn(" open ", html.split(">")[0] + " ")
        self.assertIn("Setting up the shop: 0 of 3 done", html)
        self.assertEqual(milestones(html), {"lightning": "waiting", "first_day": "todo", "certificate": "todo"})
        self.assertIn('class="journey-next"', html)
        self.assertIn("Next step", html)
        # The map card, rendered here: name, place and discount, no request to the map's tiles
        self.assertIn("Your card on the map, now", html)
        self.assertIn('<span class="pin pin-discount" aria-hidden="true">-10%</span>', html)
        self.assertIn("<strong>Bar</strong>", html)
        # The first day sits inside its milestone, with its forms: the panel works without JavaScript
        self.assertIn(f'id="first-day-{self.shop.id}"', html)
        self.assertEqual(html.count('class="step-tick"'), 3)
        # The block starts inside the path's own <details>: the one left is the first day's
        self.assertEqual(html.count("<details"), 1)

    def test_the_whole_path_folds(self):
        now = timezone.now()
        Shop.objects.filter(pk=self.shop.pk).update(
            first_sale=now, sign_printed=now, register_shared=now, lightning_status=LightningStatus.OK,
            lightning_checked=now, lightning_worked=now, lightning_checked_address="bar@example.org",
            certified_month=date(2026, 10, 1))
        page = self.panel()
        html = block(page, self.shop)
        self.assertNotIn(" open", html.split(">")[0])
        self.assertIn("Setting up the shop: all done. Well done!", html)
        self.assertIn("Your Lightning address works: checked today at", html)
        # Said once, in the path: no line under the shop's name, and its anchor is on the milestone
        self.assertNotIn("it works.</p>", page.content.decode().split(f'id="journey-{self.shop.id}"')[0])
        self.assertEqual(page.content.decode().count(f'id="lightning-{self.shop.id}"'), 1)

    def test_a_broken_address_points_to_its_alert(self):
        Shop.objects.filter(pk=self.shop.pk).update(
            lightning_status=LightningStatus.NO_ANSWER, lightning_checked=timezone.now(),
            lightning_since=timezone.now(), lightning_checked_address="bar@example.org")
        page = self.panel().content.decode()
        self.assertEqual(milestones(block(self.panel(), self.shop))["lightning"], "todo")
        self.assertIn("the message above says what to do", page)
        self.assertEqual(page.count(f'id="lightning-{self.shop.id}"'), 1)

    def test_off_the_map(self):
        Shop.objects.filter(pk=self.shop.pk).update(suspended=True)
        html = block(self.panel(), self.shop)
        self.assertIn("the community has taken the shop off it", html)
        self.assertEqual(milestones(html)["lightning"], "off")
        Shop.objects.filter(pk=self.shop.pk).update(suspended=False, latitude=None)
        html = block(self.panel(), self.shop)
        self.assertIn("the position is missing", html)
        self.assertIn(f'href="/en/shops/edit/{self.shop.id}/"', html)

    def test_no_path_for_historic_or_imported_shops(self):
        imported = Shop.objects.create(owner=self.owner, name="Imported", lightning=True)
        historic = Shop.objects.create(owner=self.owner, name="Old", lightning_address="old@example.org",
                                       closed_on=date(2024, 6, 30))
        page = self.panel()
        self.assertEqual(block(page, imported), "")
        self.assertEqual(block(page, historic), "")

    def test_nothing_about_other_shops(self):
        other = get_user_model().objects.create(username="other")
        Shop.objects.create(owner=other, name="Rival", 
                            lightning_address="rival@example.org", latitude=1, longitude=2)
        page = self.panel().content.decode()
        self.assertNotIn("Rival", page)
        self.assertNotIn("rival@example.org", page)
        self.assertEqual(page.count('class="journey"'), 1)

    def test_the_certificate_in_the_panel(self):
        html = block(self.panel(), self.shop)
        self.assertNotIn('data-milestone="payments"', html)
        self.assertEqual(milestones(html)["certificate"], "todo")
        self.assertIn("A shop is certified when its Lightning address works and its owner has earned the basic pass", html)
        self.assertIn("has not passed the site's check yet", html)
        self.assertIn("the badge also needs your basic pass", html)
        now = timezone.now()
        Shop.objects.filter(pk=self.shop.pk).update(certified_month=date(2026, 10, 1), lightning_worked=now)
        html = block(self.panel(), self.shop)
        self.assertEqual(milestones(html)["certificate"], "done")
        self.assertIn("Certified since October 2026", html)
        # The card shows the badge and the icon as the map draws them
        self.assertIn('class="certified"', html)
        self.assertIn("paid-lightning paid-week", html)

    def test_the_panel_speaks_the_language(self):
        html = block(self.panel("it"), self.shop)
        self.assertIn("Preparare il negozio: 0 su 3 fatti", html)
        self.assertIn("La tua scheda sulla mappa, adesso", html)
        self.assertIn("Prossimo passo", html)
        # German is not among the example community's languages: the path rendered on its own
        self.shop.first_day = first_day.summary(self.shop)
        with override("de"):
            html = render_to_string("shops/journey.html", {
                "shop": self.shop, "path": journey.summary(self.shop), "community": {"email": "info@example.org"}})
        self.assertIn("Den Laden einrichten: 0 von 3 erledigt", html)
        self.assertIn("Ihr Eintrag auf der Karte, jetzt", html)
        self.assertIn("Nächster Schritt", html)
        self.assertIn('data-count="Den Laden einrichten: {done} von {total} erledigt"', html)

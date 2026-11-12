"""The first day in the panel (shops/first_day.py) and the card for whoever stands at the till."""
import re
from datetime import date
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from .models import Shop
from .tests import steady_limits

PANEL = "/en/shops/"
def first_day_block(page, shop):
    html = page.content.decode()
    start = html.find(f'id="first-day-{shop.id}"')
    return "" if start < 0 else html[start:html.index("</details>", start)]


def states(block):
    return dict(re.findall(r'data-step="(\w+)" data-state="(\w+)"', block))


@override_settings(TELEGRAM_BOT_TOKEN="")
class Panel(TestCase):
    def setUp(self):
        cache.clear()
        self.owner = get_user_model().objects.create(username="k")
        self.client.force_login(self.owner)
        self.shop = Shop.objects.create(owner=self.owner, name="Bar", 
                                        lightning_address="bar@example.org")

    def panel(self, language="en"):
        page = self.client.get(f"/{language}/shops/")
        self.assertEqual(page.status_code, 200)
        return page

    def test_a_new_shop_starts_with_every_step_to_do(self):
        block = first_day_block(self.panel(), self.shop)
        self.assertIn(" open ", block.split(">")[0] + " ")
        self.assertEqual(states(block), {"sale": "todo", "map": "todo", "sign": "todo", "staff": "todo"})
        self.assertIn("The first day: 0 of 4 done", block)
        self.assertIn("When it says “Payment received”, the register works.", block)
        self.assertIn(f'href="/en/shops/register/{self.shop.id}/"', block)
        self.assertIn(f'href="/en/shops/card/{self.shop.id}/"', block)

    def test_a_complete_shop_folds_into_one_line(self):
        now = timezone.now()
        Shop.objects.filter(pk=self.shop.pk).update(first_sale=now, sign_printed=now, register_shared=now, latitude=1, longitude=2)
        block = first_day_block(self.panel(), self.shop)
        self.assertNotIn(" open", block.split(">")[0])
        self.assertIn("<summary>The first day: all done</summary>", block)
        self.assertEqual(set(states(block).values()), {"done"})

    def test_the_first_sale_is_the_merchants_tick(self):
        """The site sees no sale: the step is done when the merchant says so, and only then."""
        self.assertEqual(states(first_day_block(self.panel(), self.shop))["sale"], "todo")
        self.client.post(f"/en/shops/first-day/{self.shop.id}/", {"step": "sale", "done": "1"})
        self.assertEqual(states(first_day_block(self.panel(), self.shop))["sale"], "done")

    def test_no_first_day_without_a_register_or_for_a_historic_shop(self):
        imported = Shop.objects.create(owner=self.owner, name="Imported")
        historic = Shop.objects.create(owner=self.owner, name="Old", lightning_address="old@example.org", closed_on=date(2024, 6, 30))
        page = self.panel()
        self.assertEqual(first_day_block(page, imported), "")
        self.assertEqual(first_day_block(page, historic), "")
        self.assertNotIn(f"/en/shops/card/{imported.id}/", page.content.decode())
        # A historic shop still takes payments: its staff keeps the card
        self.assertIn(f"/en/shops/card/{historic.id}/", page.content.decode())

    def test_a_shop_off_the_map_is_not_held_back_by_the_map(self):
        now = timezone.now()
        Shop.objects.filter(pk=self.shop.pk).update(first_sale=now, sign_printed=now, register_shared=now, suspended=True)
        block = first_day_block(self.panel(), self.shop)
        self.assertEqual(states(block)["map"], "off")
        self.assertIn('write to <a href="mailto:info@example.org">', block)
        self.assertIn("The first day: all done", block)
        Shop.objects.filter(pk=self.shop.pk).update(suspended=False, listed=False, register_shared=None)
        block = first_day_block(self.panel(), self.shop)
        self.assertIn("as agreed with the community", block)
        self.assertIn("The first day: 2 of 3 done", block)

    def test_the_panel_speaks_the_language(self):
        block = first_day_block(self.panel("it"), self.shop)
        self.assertIn("Il primo giorno: 0 passi fatti su 4", block)
        self.assertIn("Quando dice «Pagamento ricevuto», la cassa funziona.", block)


class Ticks(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create(username="k")
        self.client.force_login(self.owner)
        self.shop = Shop.objects.create(owner=self.owner, name="Bar", lightning_address="bar@example.org")
        self.url = f"/en/shops/first-day/{self.shop.id}/"

    def test_the_merchant_ticks_and_unticks(self):
        response = self.client.post(self.url, {"step": "sign", "done": "1"})
        self.assertRedirects(response, f"/en/shops/#first-day-{self.shop.id}", fetch_redirect_response=False)
        self.shop.refresh_from_db()
        self.assertIsNotNone(self.shop.sign_printed)
        self.assertIsNone(self.shop.register_shared)
        self.client.post(self.url, {"step": "staff", "done": "1"})
        self.client.post(self.url, {"step": "sign", "done": "0"})
        self.shop.refresh_from_db()
        self.assertIsNone(self.shop.sign_printed)
        self.assertIsNotNone(self.shop.register_shared)

    def test_the_first_sale_is_ticked_and_unticked_too(self):
        self.client.post(self.url, {"step": "sale", "done": "1"})
        self.shop.refresh_from_db()
        self.assertIsNotNone(self.shop.first_sale)
        self.client.post(self.url, {"step": "sale", "done": "0"})
        self.shop.refresh_from_db()
        self.assertIsNone(self.shop.first_sale)

    def test_only_the_steps_the_site_cannot_know(self):
        for step in ("map", "first_sale", ""):
            self.assertEqual(self.client.post(self.url, {"step": step, "done": "1"}).status_code, 400, step)
        self.shop.refresh_from_db()
        self.assertIsNone(self.shop.first_sale)
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_a_script_ticking_without_end_is_stopped(self):
        with steady_limits(), patch("shops.views.TICKS_PER_USER", 2):
            for _round in range(2):
                self.client.post(self.url, {"step": "sign", "done": "1"})
            response = self.client.post(self.url, {"step": "staff", "done": "1"})
        self.assertRedirects(response, f"/en/shops/#first-day-{self.shop.id}", fetch_redirect_response=False)
        self.shop.refresh_from_db()
        self.assertIsNone(self.shop.register_shared)

    def test_works_without_javascript(self):
        """Plain forms with the CSRF token: no script needed to tick."""
        page = self.client.get(PANEL).content.decode()
        forms = re.findall(rf'<form method="post" action="{self.url}" class="step-tick">.*?</form>', page)
        self.assertEqual(len(forms), 3)
        for form in forms:
            self.assertIn("csrfmiddlewaretoken", form)

    def test_nobody_else_ticks(self):
        other = get_user_model().objects.create(username="other")
        self.client.force_login(other)
        self.assertEqual(self.client.post(self.url, {"step": "sign", "done": "1"}).status_code, 404)
        self.client.logout()
        self.assertEqual(self.client.post(self.url, {"step": "sign", "done": "1"}).status_code, 302)
        self.shop.refresh_from_db()
        self.assertIsNone(self.shop.sign_printed)

    def test_the_tick_needs_the_csrf_token(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        self.assertEqual(client.post(self.url, {"step": "sign", "done": "1"}).status_code, 403)
        self.shop.refresh_from_db()
        self.assertIsNone(self.shop.sign_printed)
        # With the token of the panel's own form it goes through
        page = client.get(PANEL).content.decode()
        token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page)[1]
        self.assertEqual(client.post(self.url, {"step": "sign", "done": "1", "csrfmiddlewaretoken": token}).status_code, 302)
        self.shop.refresh_from_db()
        self.assertIsNotNone(self.shop.sign_printed)

    def test_no_ticks_for_a_shop_without_a_register(self):
        imported = Shop.objects.create(owner=self.owner, name="Imported")
        url = f"/en/shops/first-day/{imported.id}/"
        self.assertEqual(self.client.post(url, {"step": "sign", "done": "1"}).status_code, 404)


@override_settings(TELEGRAM_BOT_TOKEN="")
class TillCard(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create(username="k")
        self.client.force_login(self.owner)
        self.shop = Shop.objects.create(owner=self.owner, name="Central Bar", 
                                        lightning_address="bar@example.org")

    def card(self, shop=None, language="en", query=""):
        return self.client.get(f"/{language}/shops/card/{(shop or self.shop).id}/{query}")

    def test_the_card_uses_the_words_of_the_register(self):
        page = self.card(language="it")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page["X-Robots-Tag"], "noindex, nofollow")
        self.assertContains(page, "Central Bar")
        for words in ("Scrivi anche i centesimi: 1, 5, 0 fa 1,50.", "«In attesa del pagamento»", "«Pagamento ricevuto»",
                      "«Il codice non si legge? Mostralo come testo»", "«Nuovo pagamento»"):
            self.assertContains(page, words)

    def test_what_depends_on_how_the_shop_gets_paid(self):
        page = self.card()
        self.assertContains(page, "With Lightning it takes a few seconds.")
        self.assertNotContains(page, "Confirming the payment")
        self.assertNotContains(page, "withdrew the payment")
        self.assertContains(page, "a Lightning payment is always the exact amount of the code")

    def test_who_to_ask(self):
        page = self.card()
        self.assertContains(page, "About the register and the site: info@example.org")
        self.assertContains(page, 'class="till-line"', count=2)

    @override_settings(PUBLIC_URL="https://website.example.org")
    def test_the_register_code_only_when_asked(self):
        page = self.card()
        self.assertNotContains(page, 'class="till-qr"')
        self.assertNotContains(page, f"/shops/register/{self.shop.id}/")
        self.assertContains(page, f'href="/en/shops/card/{self.shop.id}/?qr=1"')
        page = self.card(query="?qr=1")
        self.assertContains(page, 'class="till-qr"')
        self.assertContains(page, f"https://website.example.org/en/shops/register/{self.shop.id}/")
        self.assertContains(page, "data:image/svg+xml")
        # The other languages keep the choice
        self.assertContains(page, f'href="/it/shops/card/{self.shop.id}/?qr=1" hreflang="it"')

    def test_no_inline_scripts(self):
        """The site's CSP allows no inline script: every script is a file."""
        html = self.card(query="?qr=1").content.decode()
        scripts = re.findall(r"<script\b[^>]*>", html)
        self.assertTrue(scripts)
        for tag in scripts:
            self.assertIn(" src=", tag)
        self.assertNotRegex(html, r"\son[a-z]+=")

    def test_one_main_and_one_title(self):
        html = self.card().content.decode()
        self.assertEqual(html.count("<main>"), 1)
        self.assertEqual(html.count("<h1"), 1)
        self.assertIn('name="viewport" content="width=device-width', html)

    def test_only_your_own_shops_with_a_register(self):
        other = Shop.objects.create(owner=get_user_model().objects.create(username="other"), name="Other", lightning_address="o@example.org")
        imported = Shop.objects.create(owner=self.owner, name="Imported")
        self.assertEqual(self.card(other).status_code, 404)
        self.assertEqual(self.card(imported).status_code, 404)
        self.client.logout()
        response = self.card()
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith("/en/shops/?next="))

    def test_historic_and_suspended_shops_keep_the_card(self):
        """Their register keeps working: whoever stands at the till still needs the card."""
        Shop.objects.filter(pk=self.shop.pk).update(closed_on=date(2024, 6, 30), suspended=True)
        self.assertEqual(self.card().status_code, 200)

    def test_the_print_rules_are_the_cards_only(self):
        """An A4 sheet with its margins for the card, through a named page: the sign and the other pages print as before."""
        css = (settings.BASE_DIR / "static" / "base.css").read_text()
        self.assertIn("@page till-card { size: A4;", css)
        self.assertIn("page: till-card;", css)
        self.assertNotRegex(css, r"@page\s*\{")
        self.assertIn(".till-columns { columns: 2;", css)

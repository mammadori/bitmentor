"""A closing day (`Shop.closed_on`) acts only from the day after: up to and including that day the shop is an ordinary
one that says «closes on …»; from the next day it is historic (`Shop.closed`, `models.closed_for_good`). Each family of
readers is tried on the day before, the closing day itself and the day after, with the clock patched."""
import json
import subprocess
from contextlib import contextmanager
from datetime import date, timedelta
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from . import certified, customer, first_day, journey, lnaddress, moving, vacation
from .diagnosis import Run, check_shop
from .directory import payable_shops, public_shops, statistics, waits_for_wallet
from .models import Closure, Shop, address_pending, closed_for_good, still_trading
from .nostr import shop_event

CLOSING_DAY = date(2026, 11, 30)
# The day before and the closing day itself: an ordinary shop. The day after: historic
DAYS = ((CLOSING_DAY - timedelta(days=1), False), (CLOSING_DAY, False), (CLOSING_DAY + timedelta(days=1), True))


@contextmanager
def on(day):
    with patch("shops.models.timezone.localdate", return_value=day):
        yield


class ClosingDay(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user("owner")
        self.shop = Shop.objects.create(name="Bar", city="Example", latitude=1, longitude=1, discount=9, lightning=True,
                                        owner=self.owner, lightning_address="bar@wallet.example",
                                        closed_on=CLOSING_DAY)
        # A Lightning address that worked: the icons' bands have something to say
        Shop.objects.filter(pk=self.shop.pk).update(lightning_worked=timezone.now())

    def fresh(self):
        return Shop.objects.prefetch_related("closures", "places").get(pk=self.shop.pk)

    def test_the_property_and_the_filters(self):
        for day, closed in DAYS:
            with self.subTest(day=day), on(day):
                self.assertEqual(self.fresh().closed, closed)
                self.assertEqual(Shop.objects.filter(closed_for_good()).exists(), closed)
                self.assertEqual(Shop.objects.filter(still_trading()).exists(), not closed)
                self.assertEqual(Shop.objects.filter(closed_for_good(day)).exists(), closed)
        open_shop = Shop.objects.create(name="Open")
        self.assertFalse(open_shop.closed)
        self.assertIn(open_shop, Shop.objects.filter(still_trading()))
        self.assertNotIn(open_shop, Shop.objects.filter(closed_for_good()))

    def test_the_note(self):
        for day, closed in DAYS:
            with self.subTest(day=day), on(day):
                note = self.fresh().historic_note
                self.assertEqual(note, "Historic · closed on 30 November 2026" if closed else "Closes on 30 November 2026")
                # The shop page: a notice of a closing to come, or of a historic shop
                page = self.client.get(f"/en/shops/s/{self.shop.id}/").content.decode()
                kind = "shop-historic" if closed else "shop-closing"
                self.assertIn(f'<p class="notice {kind}" role="note">{note}</p>', page)
                # The owner's panel: «until then nothing changes» up to and including the closing day
                self.client.force_login(self.owner)
                panel = self.client.get("/en/shops/").content.decode()
                self.client.logout()
                self.assertIn(note, panel)
                self.assertEqual("Until then nothing changes: it stays among the places to pay." in panel, not closed)
                self.assertEqual("The register stays yours and keeps working" in panel, closed)

    def test_customers_can_pay_there_until_the_closing_day(self):
        for day, closed in DAYS:
            with self.subTest(day=day), on(day):
                shop = self.fresh()
                self.assertEqual(customer.payable(shop) is None, closed)
                page = self.client.get(f"/en/shops/s/{shop.id}/")
                self.assertEqual('id="shop-pay-title"' in page.content.decode(), not closed)
                self.assertEqual("if you pay in bitcoin" in page.content.decode(), not closed)
                self.assertEqual("Accepts Lightning payments" in page.content.decode(), not closed)
                self.assertEqual("data-historic" in page.content.decode(), closed)
                self.assertContains(page, shop.historic_note)
                # The card for customers without a wallet, from the panel
                self.client.force_login(self.owner)
                self.assertEqual(self.client.get(f"/en/shops/customer-card/{shop.id}/").status_code, 404 if closed else 200)
                self.client.logout()

    def test_the_places_to_pay_and_their_counts(self):
        for day, closed in DAYS:
            with self.subTest(day=day), on(day):
                self.assertEqual(payable_shops().filter(pk=self.shop.pk).exists(), not closed)
                self.assertTrue(public_shops().filter(pk=self.shop.pk).exists())
                self.assertEqual(statistics()["shops"], 0 if closed else 1)
                self.assertEqual(statistics()["discounts"], 0 if closed else 1)
                page = self.client.get("/en/shops/map/").content.decode()
                self.assertIn(f'<span id="shop-count">{0 if closed else 1}</span>', page)
                self.assertEqual('<li class="historic">' in page, closed)
                self.assertEqual("-9%" in page, not closed)
                self.assertIn(self.fresh().historic_note, page)

    def test_the_json_and_nostr_keep_the_date_and_follow_the_day(self):
        for day, closed in DAYS:
            with self.subTest(day=day), on(day):
                found = self.client.get("/shops.json").json()["shops"][0]
                self.assertEqual(found["closed_on"], "2026-11-30")
                self.assertEqual(found["discount"], 0 if closed else 9)
                self.assertEqual(found["paid"] is None, closed)
                tags = dict(shop_event("01" * 32, self.fresh(), "https://x.test/s")["tags"])
                self.assertEqual(tags["closed_on"], "2026-11-30")
                self.assertEqual(tags.get("discount"), None if closed else "9")

    def test_the_icons(self):
        for day, closed in DAYS:
            with self.subTest(day=day), on(day):
                self.assertEqual(certified.marks(self.fresh()) == [], closed)

    def test_a_closure_for_a_while_counts_until_the_closing_day(self):
        Closure.objects.create(shop=self.shop, first_day=date(2000, 1, 1))
        self.client.force_login(self.owner)
        for day, closed in DAYS:
            with self.subTest(day=day), on(day):
                shop = self.fresh()
                self.assertEqual(vacation.is_closed(shop), not closed)
                self.assertEqual(vacation.info(shop) is None, closed)
                # The panel's card to close for a while
                panel = self.client.get("/en/shops/").content.decode()
                self.assertEqual(f'href="/en/shops/break/{shop.id}/"' in panel or "I am back today" in panel, not closed)

    def test_the_first_day_the_journey_and_the_moves(self):
        for day, closed in DAYS:
            with self.subTest(day=day), on(day):
                shop = self.fresh()
                self.assertEqual(first_day.applies(shop), not closed)
                self.assertEqual(journey.applies(shop), not closed)
                self.assertEqual(moving.has_register(shop), not closed)
                self.assertEqual(moving.shop_entry(shop)["closed_on"], "2026-11-30")

    def test_a_shop_waiting_for_its_wallet_waits_until_the_closing_day(self):
        Shop.objects.filter(pk=self.shop.pk).update(lightning_address="", awaiting_wallet=True)
        for day, closed in DAYS:
            with self.subTest(day=day), on(day):
                self.assertEqual(waits_for_wallet(self.fresh()), not closed)
                self.assertEqual(public_shops().filter(pk=self.shop.pk).exists(), closed)

    def test_the_known_providers(self):
        other = get_user_model().objects.create_user("other")
        Shop.objects.create(name="Pub", owner=other, lightning_address="pub@wallet.example", approved_on=CLOSING_DAY)
        Shop.objects.filter(pk=self.shop.pk).update(approved_on=CLOSING_DAY)
        for day, closed in DAYS:
            with self.subTest(day=day), on(day):
                self.assertEqual(lnaddress.known_domains(), [] if closed else ["wallet.example"])

    def test_the_diagnosis(self):
        for day, closed in DAYS:
            with self.subTest(day=day), on(day):
                run = Run(self.fresh(), 10)
                check_shop(run)
                codes = {finding.code for finding in run.findings}
                self.assertEqual("shop_historic" in codes, closed)
                self.assertEqual("shop_closing" in codes, not closed)


class AddressPending(TestCase):
    """The guardian does not watch a historic shop, so a new address of its would wait for ever: a historic shop has no
    address pending, and its owner keeps the certified ownership and the private part as an owner who never changed it."""

    def setUp(self):
        now = timezone.now()
        self.owners, self.shops = [], []
        for name, changed in (("kept", None), ("changed", now - timedelta(days=365))):
            owner = get_user_model().objects.create_user(name)
            address = f"{name}@wallet.example"
            shop = Shop.objects.create(name=name, owner=owner, lightning_address=address, lightning=True,
                                       closed_on=CLOSING_DAY)
            # Certified, approved for the private part, and the guardian saw the (new) address work
            Shop.objects.filter(pk=shop.pk).update(
                certified_month=date(2026, 9, 1), private_approved=now, lightning_changed=changed,
                lightning_checked_address=address, lightning_worked=now - timedelta(days=1), lightning_status="ok",
                lightning_checked=now - timedelta(hours=1))
            self.owners.append(owner)
            self.shops.append(shop)

    def seen(self):
        return [(certified.owns_certified_shop(owner), certified.sees_private_part(owner)) for owner in self.owners]

    def test_both_owners_are_treated_the_same_after_the_closing_day(self):
        with on(CLOSING_DAY):
            self.assertEqual(self.seen(), [(True, True), (True, True)])
        with on(CLOSING_DAY + timedelta(days=1)):
            call_command("check_lightning", stdout=StringIO(), stderr=StringIO())
            changed = Shop.objects.get(pk=self.shops[1].pk)
            # The guardian forgot what it knew of the address of a shop it no longer watches
            self.assertEqual(changed.lightning_checked_address, "")
            self.assertFalse(changed.address_pending)
            self.assertFalse(Shop.objects.filter(address_pending()).exists())
            self.assertEqual(self.seen(), [(True, True), (True, True)])
        # On the closing day itself the same shop, still watched, would wait for the guardian
        with on(CLOSING_DAY):
            self.assertTrue(Shop.objects.get(pk=self.shops[1].pk).address_pending)
            self.assertEqual(list(Shop.objects.filter(address_pending())), [self.shops[1]])

    def test_the_panel_of_a_historic_shop_promises_no_check(self):
        from . import address_change

        shop, owner = self.shops[0], self.owners[0]
        self.client.force_login(owner)
        for day, closed in DAYS:
            with self.subTest(day=day), on(day):
                Shop.objects.filter(pk=shop.pk).update(lightning_changed=None, lightning_address="kept@wallet.example")
                self.assertTrue(address_change.apply(shop.pk, owner, "kept@wallet.example", "new@wallet.example"))
                panel = self.client.get("/en/shops/").content.decode()
                self.assertIn("you changed the Lightning address", panel)
                self.assertEqual("We are checking the new address" in panel, not closed)


@override_settings(PUBLIC_LIST_SECONDS=86400)
class Midnight(TestCase):
    """Nothing is saved when a shop becomes historic: a public list kept from yesterday must not be given out today."""

    def setUp(self):
        cache.clear()
        Shop.objects.create(name="Bar", latitude=1, longitude=1, discount=9, closed_on=CLOSING_DAY)

    def test_the_cached_lists_follow_the_day(self):
        with on(CLOSING_DAY):
            self.assertEqual(self.client.get("/shops.json").json()["shops"][0]["discount"], 9)
            self.assertIn('<span id="shop-count">1</span>', self.client.get("/en/shops/map/").content.decode())
        with on(CLOSING_DAY + timedelta(days=1)):
            self.assertEqual(self.client.get("/shops.json").json()["shops"][0]["discount"], 0)
            self.assertIn('<span id="shop-count">0</span>', self.client.get("/en/shops/map/").content.decode())


class MapScript(SimpleTestCase):
    """static/map.js draws the pins from /shops.json: a shop is historic from the day after its closing day, by the
    site's own day (data-today), or by the phone's for a template without it."""

    def historic(self, today, closed_on):
        from .test_scripts import STATIC, node_or_skip

        node = node_or_skip(self)
        script = (STATIC / "map.js").read_text()
        part = script[script.index("const pad = "):script.index("// Recognizable without colors too")]
        harness = (f"const element = {{ dataset: {json.dumps({'today': today} if today else {})} }};\n{part}\n"
                   f"process.stdout.write(JSON.stringify([historic({{ closed_on: {json.dumps(closed_on)} }}), today]));")
        result = subprocess.run([node, "-e", harness], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_the_three_days(self):
        self.assertEqual(self.historic("2026-11-29", "2026-11-30"), [False, "2026-11-29"])
        self.assertEqual(self.historic("2026-11-30", "2026-11-30"), [False, "2026-11-30"])
        self.assertEqual(self.historic("2026-12-01", "2026-11-30"), [True, "2026-12-01"])
        self.assertEqual(self.historic("2026-12-01", None), [False, "2026-12-01"])

    def test_without_the_site_s_day_the_phone_s(self):
        historic, today = self.historic("", "2000-01-01")
        self.assertTrue(historic)
        self.assertRegex(today, r"^\d{4}-\d{2}-\d{2}$")
        self.assertEqual(self.historic("not a day", "2999-01-01")[0], False)

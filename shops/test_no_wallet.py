"""A shop saved before its owner has a wallet: off the map and without a register until the guide adds the wallet."""
from datetime import date
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase

from config.community import community, shop_form_links
from website.apps import skipped_form_links

from .directory import public_shops
from .models import Profile, RequestCount, Shop
from .test_moving import Files

WALLET = {"name": "Example Wallet", "link": "https://wallet.example.org/", "platforms": ["android", "ios"],
          "description": {"en": "Free, and the keys stay with you.", "it": "Gratis, e le chiavi restano a te."},
          "open_source": True, "self_custody": True, "free": True}


def with_wallets(*wallets, **form):
    data = {**community(), "shop_form": {**form, "wallets": list(wallets)}}
    return patch("config.community.community", return_value=data)


class Base(TestCase):
    def setUp(self):
        cache.clear()
        RequestCount.objects.all().delete()
        self.user = get_user_model().objects.create(username="k")
        self.client.force_login(self.user)
        probe = patch("shops.views.refusal", return_value=None)
        probe.start()
        self.addCleanup(probe.stop)

    def new(self, **fields):
        return self.client.post("/en/shops/new/", {"name": "Bar", "latitude": "41.9", "longitude": "12.5", **fields})

    def waiting(self):
        self.new(later="on")
        return Shop.objects.get()


class SaveWithoutWallet(Base):
    def test_ticked_and_empty_saves_the_shop_without_a_register(self):
        response = self.new(later="on")
        shop = Shop.objects.get()
        self.assertRedirects(response, f"/en/shops/wallet/{shop.id}/?saved=1")
        self.assertTrue(shop.awaiting_wallet)
        self.assertEqual((shop.lightning, shop.lightning_address, shop.has_register), (False, "", False))

    def test_not_ticked_and_empty_is_still_an_error(self):
        response = self.new()
        self.assertContains(response, "Add your Lightning address.")
        self.assertFalse(Shop.objects.exists())

    def test_a_written_address_counts_even_ticked(self):
        self.new(later="on", lightning_address="mario@example.org")
        shop = Shop.objects.get()
        self.assertFalse(shop.awaiting_wallet)
        self.assertTrue(shop.has_register)

    def test_a_wrong_address_is_not_saved_as_no_wallet(self):
        response = self.new(later="on", lightning_address="not an address")
        self.assertContains(response, "This is not a Lightning address")
        self.assertFalse(Shop.objects.exists())

    def test_the_form_offers_it(self):
        page = self.client.get("/en/shops/new/")
        self.assertContains(page, 'name="later"')
        self.assertContains(page, 'aria-describedby="later-help"')


class OffTheMap(Base):
    def test_not_on_the_map_nor_in_the_lists(self):
        shop = self.waiting()
        self.assertNotContains(self.client.get("/en/shops/map/"), str(shop.id))
        self.assertNotIn(str(shop.id), self.client.get("/shops.json").content.decode())

    def test_no_register(self):
        shop = self.waiting()
        self.assertEqual(self.client.get(f"/en/shops/register/{shop.id}/").status_code, 404)
        self.assertEqual(self.client.post(f"/register/{shop.id}/sale/", "{}", content_type="application/json").status_code, 404)

    def test_its_page_asks_nobody_to_pay_and_is_not_indexed(self):
        shop = self.waiting()
        page = self.client.get(f"/en/shops/s/{shop.id}/")
        self.assertNotContains(page, "Ask to pay in bitcoin")
        self.assertEqual(page["X-Robots-Tag"], "noindex, nofollow")

    def test_the_panel_shows_the_last_step(self):
        shop = self.waiting()
        panel = self.client.get("/en/shops/")
        self.assertContains(panel, "Last step: the wallet.")
        self.assertContains(panel, f'href="/en/shops/wallet/{shop.id}/"')
        self.assertNotContains(panel, "No register: payments for this shop are not set up on this site.")
        self.assertNotContains(panel, f"/en/shops/sign/{shop.id}/")
        self.assertNotContains(panel, "Register for whoever works with you")

    def test_the_edit_page_points_to_the_wallet(self):
        shop = self.waiting()
        page = self.client.get(f"/en/shops/edit/{shop.id}/")
        self.assertContains(page, f'href="/en/shops/wallet/{shop.id}/"')
        self.assertNotContains(page, "The payment server is not available.")


class Guide(Base):
    def test_lists_the_community_wallets(self):
        shop = self.waiting()
        with with_wallets(WALLET):
            page = self.client.get(f"/en/shops/wallet/{shop.id}/")
        self.assertContains(page, '<a href="https://wallet.example.org/">Example Wallet</a>')
        self.assertContains(page, "Android, iPhone")
        self.assertContains(page, "Free, and the keys stay with you.")
        self.assertContains(page, 'id="lightning-check"')

    def test_without_a_list_the_link_of_today(self):
        shop = self.waiting()
        data = {**community(), "shop_form": {"wallet": "https://wallets.example.org/"}}
        with patch("config.community.community", return_value=data):
            page = self.client.get(f"/en/shops/wallet/{shop.id}/")
        self.assertContains(page, 'href="https://wallets.example.org/"')

    def test_without_anything_it_says_whom_to_ask(self):
        shop = self.waiting()
        page = self.client.get(f"/en/shops/wallet/{shop.id}/")
        self.assertContains(page, f"mailto:{community()['email']}")

    def test_only_the_owner(self):
        shop = self.waiting()
        self.client.force_login(get_user_model().objects.create(username="other"))
        self.assertEqual(self.client.get(f"/en/shops/wallet/{shop.id}/").status_code, 404)
        self.assertEqual(self.client.post(f"/en/shops/wallet/{shop.id}/", {"lightning_address": "x@example.org"}).status_code, 404)
        self.client.logout()
        self.assertEqual(self.client.get(f"/en/shops/wallet/{shop.id}/").status_code, 302)

    def test_post_needs_csrf(self):
        shop = self.waiting()
        from django.test import Client
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        self.assertEqual(client.post(f"/en/shops/wallet/{shop.id}/", {"lightning_address": "x@example.org"}).status_code, 403)

    def test_the_wallet_makes_the_register_and_the_map(self):
        shop = self.waiting()
        response = self.client.post(f"/en/shops/wallet/{shop.id}/", {"lightning_address": " Mario@Example.org. "})
        self.assertRedirects(response, "/en/shops/?saved=1", fetch_redirect_response=False)
        shop.refresh_from_db()
        self.assertEqual((shop.lightning_address, shop.lightning, shop.awaiting_wallet, shop.name),
                         ("mario@example.org", True, False, "Bar"))
        self.assertContains(self.client.get("/en/shops/map/"), str(shop.id))
        # Once there is a wallet the guide never opens again: the destination does not change from here
        self.assertRedirects(self.client.get(f"/en/shops/wallet/{shop.id}/"), "/en/shops/", fetch_redirect_response=False)
        self.client.post(f"/en/shops/wallet/{shop.id}/", {"lightning_address": "thief@example.org"})
        shop.refresh_from_db()
        self.assertEqual(shop.lightning_address, "mario@example.org")

    def test_the_details_stay_as_they_are(self):
        shop = self.waiting()
        Shop.objects.filter(pk=shop.pk).update(website="https://bar.example.org", discount=10, description="Good")
        self.client.post(f"/en/shops/wallet/{shop.id}/", {"lightning_address": "mario@example.org", "name": "Other",
                                                          "website": "", "discount": "0"})
        shop.refresh_from_db()
        self.assertEqual((shop.name, shop.website, shop.discount, shop.description),
                         ("Bar", "https://bar.example.org", 10, "Good"))

    def test_empty_is_an_error_and_nothing_is_made(self):
        shop = self.waiting()
        response = self.client.post(f"/en/shops/wallet/{shop.id}/", {})
        self.assertContains(response, "Add your Lightning address.")
        shop.refresh_from_db()
        self.assertTrue(shop.awaiting_wallet)

    def test_a_refused_address_leaves_the_shop_waiting(self):
        shop = self.waiting()
        with patch("shops.views.refusal", return_value="does not answer"):
            response = self.client.post(f"/en/shops/wallet/{shop.id}/", {"lightning_address": "mario@example.org"})
        self.assertContains(response, "does not answer")
        shop.refresh_from_db()
        self.assertEqual((shop.lightning_address, shop.awaiting_wallet), ("", True))

    def test_suspended_or_historic_takes_no_register(self):
        shop = self.waiting()
        for change in ({"suspended": True}, {"suspended": False, "closed_on": "2025-01-01"}):
            Shop.objects.filter(pk=shop.pk).update(**change)
            self.assertRedirects(self.client.get(f"/en/shops/wallet/{shop.id}/"), "/en/shops/",
                                 fetch_redirect_response=False)

    def test_a_shop_with_a_register_never_opens_it(self):
        made = Shop.objects.create(name="Made", owner=self.user, lightning_address="a@example.org")
        for url in (f"/en/shops/wallet/{made.id}/", f"/en/shops/connect/{made.id}/"):
            self.assertRedirects(self.client.get(url), "/en/shops/", fetch_redirect_response=False)

    def test_a_directory_shop_given_to_its_owner_opens_it_and_stays_on_the_map(self):
        """The rule of the moved shops (shops/moving.py): a shop of the owner without any register may connect one.
        It was on the map before, as a shop with its own till, and stays there."""
        imported = Shop.objects.create(name="Old", owner=self.user, external_id="7", latitude=41.9, longitude=12.5)
        self.assertContains(self.client.get(f"/en/shops/wallet/{imported.id}/"), 'id="step-choose"')
        self.assertIn(imported, public_shops())
        panel = self.client.get("/en/shops/")
        self.assertNotContains(panel, "Last step: the wallet.")
        self.assertContains(panel, "No register: payments for this shop are not set up on this site.")

    def test_one_page_at_both_addresses(self):
        shop = self.waiting()
        for url in (f"/en/shops/wallet/{shop.id}/", f"/en/shops/connect/{shop.id}/"):
            self.assertContains(self.client.get(url), 'id="step-paste"')

    def test_limited_like_new_shops(self):
        shop = self.waiting()
        with patch("config.ratelimit.over_limit", return_value=True):
            response = self.client.post(f"/en/shops/wallet/{shop.id}/", {"lightning_address": "mario@example.org"})
        self.assertContains(response, "Too many shops in a short time")
        self.assertEqual(Shop.objects.get(pk=shop.pk).lightning_address, "")


class CommunityWallets(TestCase):
    def test_read_in_the_language(self):
        with with_wallets(WALLET):
            found = shop_form_links("it")["wallets"]
        self.assertEqual(found, [{"name": "Example Wallet", "link": "https://wallet.example.org/",
                                  "description": "Gratis, e le chiavi restano a te.", "platforms": ["android", "ios"],
                                  "methods": ["lightning", "onchain"]}])

    def test_none_without_the_three_promises(self):
        """MiCAR: only open source, non-custodial and free tools. The community states it for every wallet."""
        for missing in ("open_source", "self_custody", "free"):
            entry = {key: value for key, value in WALLET.items() if key != missing}
            problems = []
            with with_wallets(entry, {**WALLET, missing: "yes"}):
                self.assertEqual(shop_form_links("en", problems.append)["wallets"], [])
            self.assertEqual(len(problems), 2)
            self.assertIn(f"`{missing}: true`", problems[0])

    def test_wrong_wallets_are_left_out_and_said(self):
        wrong = [{**WALLET, "link": "javascript:alert(1)"}, {**WALLET, "link": "http://wallet.example.org/"},
                 {**WALLET, "name": ""}, {**WALLET, "name": "x" * 41}, {**WALLET, "platforms": ["nokia"]},
                 {**WALLET, "description": "x" * 201}, "Example Wallet"]
        problems = []
        with with_wallets(*wrong, WALLET):
            found = shop_form_links("en", problems.append)["wallets"]
        self.assertEqual([wallet["name"] for wallet in found], ["Example Wallet"])
        self.assertEqual(len(problems), len(wrong))
        self.assertTrue(all(problem.startswith("`shop_form.wallets`, wallet ") for problem in problems))

    def test_the_check_says_it(self):
        with with_wallets({**WALLET, "free": False}):
            problems = skipped_form_links(None)
        self.assertEqual([problem.id for problem in problems], ["website.W006"])
        self.assertIn("self-custody and free", problems[0].msg)

    def test_the_software_names_no_wallet(self):
        self.assertEqual(shop_form_links("en")["wallets"], [])


class MovedShop(Base):
    """A shop moved here with a register on the other site waits like one saved without a wallet: the same guide."""

    def setUp(self):
        super().setUp()
        self.shop = Shop.objects.create(owner=self.user, name="Moved", latitude=41.9, longitude=12.5,
                                        moved_register=True, moved_from="x", moved_in="2026-11-04T10:00Z")

    def test_off_the_map_without_payments_on_its_page(self):
        self.assertNotIn(self.shop, public_shops())
        page = self.client.get(f"/en/shops/s/{self.shop.id}/")
        self.assertNotContains(page, "Ask to pay in bitcoin")
        self.assertEqual(page["X-Robots-Tag"], "noindex, nofollow")

    def test_the_panel_says_it_arrived_once_and_links_the_guide(self):
        panel = self.client.get("/en/shops/")
        self.assertContains(panel, "Your shop has arrived here")
        self.assertNotContains(panel, "Last step: the wallet.")
        self.assertContains(panel, f'href="/en/shops/connect/{self.shop.id}/"')
        self.assertNotContains(panel, "Register for whoever works with you")
        self.assertContains(self.client.get(f"/en/shops/connect/{self.shop.id}/"), 'id="step-choose"')

    def test_historic_is_on_the_map_with_its_pin_and_opens_nothing(self):
        Shop.objects.filter(pk=self.shop.pk).update(closed_on="2020-01-01")
        self.assertIn(self.shop, public_shops())
        self.assertRedirects(self.client.get(f"/en/shops/connect/{self.shop.id}/"), "/en/shops/",
                             fetch_redirect_response=False)

    def test_the_wallet_ends_the_wait(self):
        self.client.post(f"/en/shops/connect/{self.shop.id}/", {"lightning_address": "mario@example.org"})
        self.shop.refresh_from_db()
        self.assertTrue(self.shop.has_register)
        self.assertIn(self.shop, public_shops())
        self.assertNotContains(self.client.get("/en/shops/"), "Your shop has arrived here")


class EdgeCases(Base):
    """The shop without a wallet, at its edges: the guide's own limit, a wallet connected while the details were being
    edited, and nothing promised while the shop waits."""

    def test_the_guide_has_a_limit_of_its_own(self):
        shop = self.waiting()
        names = []

        def limit(name, *args):
            names.append(name)
            return False

        with patch("config.ratelimit.over_limit", side_effect=limit):
            self.client.post(f"/en/shops/wallet/{shop.id}/", {"lightning_address": "mario@example.org"})
        self.assertEqual([name.partition(":")[0] for name in names], ["wallet-user", "wallet", "wallet-net"])

    def test_editing_the_details_never_undoes_a_wallet_connected_meanwhile(self):
        shop = self.waiting()
        from .forms import ShopDetailsForm
        valid = ShopDetailsForm.is_valid

        def connected_meanwhile(form):
            Shop.objects.filter(pk=shop.pk).update(lightning_address="mario@example.org", awaiting_wallet=False)
            return valid(form)

        with patch.object(ShopDetailsForm, "is_valid", connected_meanwhile):
            self.client.post(f"/en/shops/edit/{shop.id}/", {"name": "New Bar", "latitude": "41.9",
                                                                 "longitude": "12.5", "discount": "3"})
        shop.refresh_from_db()
        self.assertEqual((shop.name, shop.discount, shop.lightning_address, shop.awaiting_wallet),
                         ("New Bar", 3, "mario@example.org", False))

    def test_no_promise_no_sign_no_window_while_waiting(self):
        shop = self.waiting()
        Shop.objects.filter(pk=shop.pk).update(discount=10)
        page = self.client.get(f"/en/shops/s/{shop.id}/")
        self.assertNotContains(page, "if you pay in bitcoin")
        self.assertContains(page, "This shop does not take bitcoin yet")
        self.assertEqual(self.client.get(f"/en/shops/sign/{shop.id}/").status_code, 404)
        self.assertEqual(self.client.get(f"/en/shops/window/{shop.id}/").status_code, 404)

    def test_the_sign_comes_back_with_the_wallet(self):
        shop = self.waiting()
        Shop.objects.filter(pk=shop.pk).update(lightning_address="mario@example.org", awaiting_wallet=False)
        self.assertEqual(self.client.get(f"/en/shops/sign/{shop.id}/").status_code, 200)

    def test_an_address_sent_again_goes_to_the_guide_and_is_said(self):
        shop = self.waiting()
        response = self.new(lightning_address="mario@example.org")
        self.assertRedirects(response, f"/en/shops/wallet/{shop.id}/", fetch_redirect_response=False)
        self.assertContains(self.client.get(response["Location"]), "Your shop was already saved a moment ago")
        self.assertEqual(Shop.objects.count(), 1)


class MovingWithoutWallet(Files, TestCase):
    """A shop saved without a wallet moves to another site still waiting for it: never a place to pay without a
    register there."""

    def test_it_arrives_waiting_for_its_wallet(self):
        owner = get_user_model().objects.create(username="email:0123")
        Profile.objects.create(user=owner, email="anna@example.org")
        Shop.objects.create(owner=owner, name="No wallet", latitude=42.0, longitude=12.6, discount=10,
                            awaiting_wallet=True)
        document = self.export()
        row = document["shops"][0]
        self.assertEqual((row["register"], row["lightning"]), (True, False))
        Shop.objects.all().delete()
        Profile.objects.all().delete()
        get_user_model().objects.all().delete()
        self.run_import(document, "--apply")
        arrived = Shop.objects.get(name="No wallet")
        self.assertTrue(arrived.moved_register)
        self.assertEqual(arrived.lightning_address, "")
        self.assertNotIn(arrived, public_shops())

    def test_a_historic_one_moves_as_a_place_of_the_map(self):
        Shop.objects.create(name="Closed", latitude=42.0, longitude=12.6, awaiting_wallet=True, closed_on=date(2020, 1, 1))
        self.assertFalse(self.export()["shops"][0]["register"])


class ChangedWhileConnecting(Base):
    def test_a_shop_suspended_meanwhile_gets_no_register(self):
        shop = self.waiting()

        def probe(*args, **options):
            Shop.objects.filter(pk=shop.pk).update(suspended=True)
            return None

        with patch("shops.views.refusal", side_effect=probe):
            response = self.client.post(f"/en/shops/wallet/{shop.id}/", {"lightning_address": "mario@example.org"})
        self.assertRedirects(response, "/en/shops/", fetch_redirect_response=False)
        shop.refresh_from_db()
        self.assertEqual((shop.lightning_address, shop.awaiting_wallet), ("", True))
        self.assertContains(self.client.get("/en/shops/"), "reload the page")

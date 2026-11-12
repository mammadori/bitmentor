"""The training site (shops/demo.py, manage.py demo_data): a small made-up community in every state, which asks nothing
of any provider, refuses to run next to real money, can be run again to put everything back, and takes
away all of it and only it."""
import os
import socket
import subprocess
import sys
from contextlib import ExitStack
from datetime import timedelta
from io import StringIO
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import requests
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone


from . import dashboard, demo, directory, first_day, journey, lightning_watch, lnaddress
from . import forms as shop_forms
from .models import AccountChange, Closure, Profile, Shop, StaffRegister, Trainee
from .tests import admin_session

SITE = "https://demo.example.test"
OUTSIDE = AssertionError("The demo community must not ask any other server")
User = get_user_model()


def nobody_outside():
    """Any HTTP request, socket or email fails the test."""
    return (patch("requests.sessions.Session.request", side_effect=OUTSIDE),
            patch.object(socket, "create_connection", side_effect=OUTSIDE),
            patch.object(socket, "getaddrinfo", side_effect=OUTSIDE))


def run(*args, **options):
    out = StringIO()
    call_command("demo_data", *args, stdout=out, **options)
    return out.getvalue()


def made(**options):
    with ExitStack() as stack:
        for guard in nobody_outside():
            stack.enter_context(guard)
        return run(**options)


def made_shops():
    made()
    return list(Shop.objects.all())


def demo_shop(slug):
    return Shop.objects.get(external_id=demo.PREFIX + slug)


def token_of(url):
    return parse_qs(urlsplit(url).query)["token"][0]


def real_shop_with_owner():
    """A shop of a real merchant, with everything a demo shop has, that the demo must never touch."""
    user = User.objects.create(username="email:feedface", is_active=True)
    Profile.objects.create(user=user, email="real@example.org")
    shop = Shop.objects.create(name="Real Bakery", owner=user, external_id="shop-1",
                               lightning_address="bake@example.org", latitude=1, longitude=2)
    StaffRegister.objects.create(shop=shop, name="Real helper")
    return shop


def census():
    return {name: model.objects.count() for name, model in (
        ("shops", Shop), ("users", User), ("profiles", Profile), ("staff", StaffRegister), ("passes", Trainee),
        ("changes", AccountChange))}


@override_settings(PUBLIC_URL=SITE, TELEGRAM_BOT_TOKEN="", NETWORK="signet", SITE_MODE="development")
class Refusals(TestCase):
    def test_mainnet_is_refused_and_nothing_is_made(self):
        with override_settings(NETWORK="mainnet"):
            with self.assertRaisesMessage(CommandError, "mainnet"):
                run()
            with self.assertRaises(demo.Refused):
                demo.populate()
        self.assertEqual(census(), {name: 0 for name in census()})

    def test_production_mode_is_refused_and_nothing_is_made(self):
        with override_settings(SITE_MODE="production"):
            with self.assertRaisesMessage(CommandError, "production"):
                run()
            with self.assertRaises(demo.Refused):
                demo.populate()
        self.assertEqual(census(), {name: 0 for name in census()})

    def test_sign_in_links_are_refused_there_too(self):
        made()
        for setting in ({"NETWORK": "mainnet"}, {"SITE_MODE": "production"}):
            with override_settings(**setting), self.assertRaises(CommandError):
                run("--links")

    def test_a_development_site_is_a_training_ground(self):
        with override_settings(SITE_MODE="development"):
            made()
            self.assertEqual(Shop.objects.filter(external_id__startswith=demo.PREFIX).count(), len(demo.SHOPS))

    def test_taking_it_away_is_always_allowed(self):
        made()
        with override_settings(NETWORK="mainnet", SITE_MODE="production"):
            run("--remove")
        self.assertEqual(Shop.objects.count(), 0)

    def test_a_site_with_a_real_shop_is_refused_whatever_the_mode_and_the_variable(self):
        Shop.objects.create(name="Real Bakery")
        for setting in ({}, {"SITE_MODE": "maintenance"}, {"SITE_MODE": "maintenance", "DEMO_ALLOWED": True},
                        {"DEMO_ALLOWED": True}):
            with override_settings(**setting):
                with self.assertRaisesMessage(CommandError, "1 shops"):
                    run()
                with self.assertRaises(demo.Refused):
                    demo.populate()
        self.assertEqual(Shop.objects.count(), 1)

    def test_a_merchants_account_is_refused_but_a_staff_account_is_fine(self):
        user = User.objects.create(username="email:cafe")
        with self.assertRaisesMessage(CommandError, "1 accounts"):
            run()
        user.delete()
        User.objects.create_superuser("boss")
        User.objects.create(username="ops", is_staff=True)
        made()
        self.assertEqual(Shop.objects.count(), len(demo.SHOPS))
        # the demo's own owners never count as somebody else's, on the next run either
        made()
        self.assertEqual(Shop.objects.count(), len(demo.SHOPS))

    def test_a_site_that_is_not_in_development_needs_the_variable_on_purpose(self):
        with override_settings(SITE_MODE="maintenance"):
            with self.assertRaisesMessage(CommandError, "BITMENTOR_DEMO=1"):
                run()
            self.assertEqual(Shop.objects.count(), 0)
            with override_settings(DEMO_ALLOWED=True):
                made()
                self.assertEqual(Shop.objects.count(), len(demo.SHOPS))
                run("--links")
        # never past mainnet or production, variable or not
        for setting in ({"NETWORK": "mainnet"}, {"SITE_MODE": "production"}):
            with override_settings(DEMO_ALLOWED=True, **setting), self.assertRaises(CommandError):
                run()

    def test_the_variable_is_read_from_the_environment(self):
        base = {"PATH": os.environ["PATH"], "COMMUNITY_DIR": os.environ.get("COMMUNITY_DIR", "examples/community"),
                "SECRET_KEY": "x", "DJANGO_SETTINGS_MODULE": "config.settings"}
        for value, wanted in (("1", "True"), ("", "False"), ("yes", "False"), ("0", "False")):
            out = subprocess.run([sys.executable, "-c", "from django.conf import settings; print(settings.DEMO_ALLOWED)"],
                                 env={**base, "BITMENTOR_DEMO": value}, capture_output=True, text=True)
            self.assertEqual(out.stdout.strip(), wanted, out.stderr[-300:])

    def test_remove_and_links_do_not_go_together(self):
        with self.assertRaises(CommandError):
            run("--remove", "--links")


@override_settings(PUBLIC_URL=SITE, TELEGRAM_BOT_TOKEN="", NETWORK="signet", SITE_MODE="development")
class Populate(TestCase):
    def setUp(self):
        cache.clear()

    def test_makes_the_shops_and_marks_every_one_of_them(self):
        output = made()
        self.assertEqual(Shop.objects.count(), len(demo.SHOPS))
        self.assertEqual(len(demo.SHOPS), 11)
        self.assertEqual(Shop.objects.filter(external_id__startswith=demo.PREFIX).count(), len(demo.SHOPS))
        for spec in demo.SHOPS:
            self.assertIn(spec["name"], output)
        # Every owner is a plain account with no password and an email nobody can read; none is staff
        owners = User.objects.all()
        self.assertEqual(owners.count(), 9)
        for user in owners:
            self.assertTrue(user.username.startswith(demo.USERNAME_PREFIX))
            self.assertFalse(user.has_usable_password())
            self.assertFalse(user.is_staff or user.is_superuser)
            self.assertTrue(user.profile.email.endswith("@" + demo.MAIL_DOMAIN))
        # One owner has two shops, and one shop waits for its owner
        self.assertEqual(demo_shop("door").owner, demo_shop("books").owner)
        self.assertIsNone(demo_shop("kitchen").owner)

    def test_the_names_are_made_up(self):
        names = os.environ.get("FORBIDDEN_NAMES")
        for spec in demo.SHOPS:
            if names:
                self.assertNotRegex(spec["name"] + spec["description"], f"(?i){names}")
        self.assertEqual(len({spec["name"] for spec in demo.SHOPS}), len(demo.SHOPS))
        # No real city either: every shop is in the same sample town
        self.assertEqual({shop.city for shop in made_shops()}, {demo.LOOKS["town"]})

    def test_the_addresses_cannot_be_reached(self):
        made()
        for shop in Shop.objects.exclude(lightning_address=""):
            self.assertTrue(shop.lightning_address.endswith("." + demo.MAIL_DOMAIN))
        self.assertEqual(Shop.objects.exclude(lightning_address="").count(), 8)

    def test_the_addresses_are_well_formed(self):
        made()
        for shop in Shop.objects.exclude(lightning_address=""):
            self.assertRegex(shop.lightning_address, shop_forms.LIGHTNING_ADDRESS)

    def test_every_state_is_there(self):
        made()
        loaf, door, books, petals = (demo_shop(slug) for slug in ("loaf", "door", "books", "petals"))
        hardware, gelato, tailor, herbs = (demo_shop(slug) for slug in ("hardware", "gelato", "tailor", "herbs"))
        kitchen, pencil, records = (demo_shop(slug) for slug in ("kitchen", "pencil", "records"))
        self.assertTrue(loaf.certified and hardware.certified and records.certified)
        self.assertFalse(door.certified)
        # A certificate needs the owner's basic pass: the owners of the certified shops have it, the others do not
        for shop in (loaf, hardware, records):
            self.assertIsNotNone(shop.owner.trainee.base_earned, shop.name)
        self.assertFalse(Trainee.objects.filter(user=door.owner).exists())
        self.assertIsNone(books.first_sale)
        self.assertIsNotNone(books.sign_printed)
        self.assertIsNone(books.register_shared)
        self.assertTrue(petals.awaiting_wallet)
        self.assertEqual(petals.lightning_address, "")
        self.assertEqual(gelato.lightning_status, "no_answer")
        self.assertIsNone(kitchen.owner)
        self.assertTrue(kitchen.claim_hash)
        self.assertIsNotNone(pencil.moved_in)
        self.assertIsNotNone(pencil.owner)
        self.assertEqual(pencil.lightning_address, "")
        self.assertLess(records.closed_on, timezone.localdate())
        self.assertEqual(StaffRegister.objects.filter(shop=loaf).count(), 2)
        self.assertEqual(StaffRegister.objects.filter(shop=tailor, retired__isnull=False).count(), 1)

    def test_the_dashboard_tells_every_story(self):
        made()
        found = dashboard.summary()
        by_code = {}
        for item in found["items"]:
            by_code.setdefault(item.code, set()).add(item.shop.name if item.shop else None)
        self.assertEqual(by_code["lightning_no_answer"], {"Moonlight Gelato"})
        self.assertEqual(by_code["moved_unclaimed"], {"Grandma's Kitchen"})
        self.assertEqual(by_code["moved_no_wallet"], {"Pencil Stationery"})
        # The timers are not running on a training site, and the data is as of now: the page does not say they are late
        self.assertNotIn("guardian_late", by_code)
        # The site counts no sales: nothing about them
        self.assertFalse([code for code in by_code if code.startswith(("payments", "reserve", "first_day_stuck"))])
        # The trading registers whose address works: all but the gelato shop, the historic one and the moved ones
        self.assertEqual(found["counts"]["working"], 6)
        self.assertGreaterEqual(found["counts"]["to_look"], 1)

    def test_the_paths_of_the_merchants_are_where_the_story_says(self):
        made()
        now = timezone.now()
        steps = {slug: {step["key"]: step["state"] for step in journey.summary(demo_shop(slug))["steps"]}
                 for slug in ("loaf", "door", "books", "hardware", "gelato")}
        self.assertEqual(steps["loaf"]["certificate"], journey.DONE)
        self.assertEqual(steps["hardware"]["certificate"], journey.DONE)
        self.assertEqual(steps["door"]["certificate"], journey.TODO)
        self.assertEqual(steps["books"]["first_day"], journey.TODO)
        self.assertEqual(steps["gelato"]["lightning"], journey.TODO)
        self.assertFalse([steps for steps in steps.values() if "payments" in steps])
        # The door shop's certificate waits for the owner's basic pass only: its address works
        door = journey.summary(demo_shop("door"), now=now)["steps"][-1]
        self.assertEqual((door["needs_pass"], door["needs_lightning"]), (True, False))
        # The first sale of the half-done shop is the owner's tick, not ticked yet
        books = {step["key"]: step["state"] for step in first_day.steps(demo_shop("books"))}
        self.assertEqual(books["sale"], first_day.TODO)

    def test_running_it_again_changes_nothing_but_the_dates(self):
        made()
        before = census()
        keys = {shop.external_id: shop.pk for shop in Shop.objects.all()}
        registers = set(StaffRegister.objects.values_list("pk", "key"))
        links_before = {owner["name"] for owner in demo.links()["owners"]}
        later = timezone.now() + timedelta(days=3)
        demo.populate(now=later)
        self.assertEqual(census(), before)
        self.assertEqual({shop.external_id: shop.pk for shop in Shop.objects.all()}, keys)
        self.assertEqual(set(StaffRegister.objects.values_list("pk", "key")), registers)
        self.assertEqual({owner["name"] for owner in demo.links()["owners"]}, links_before)
        # The dates followed: the shop made 2 days before the first run is made 2 days before the second
        self.assertAlmostEqual(demo_shop("books").created.timestamp(), (later - timedelta(days=2)).timestamp(), delta=5)
        self.assertGreater(demo_shop("gelato").lightning_checked, timezone.now() + timedelta(days=2))

    def test_running_it_again_puts_back_what_the_training_changed(self):
        made()
        door = User.objects.get(username="demo:door")
        kitchen = demo_shop("kitchen")
        # A volunteer, in the part of the owner: takes the shop waiting for its owner, renames another, adds a register
        # and takes away an owner's basic pass, writes to the owner's email, turns a register off
        Shop.objects.filter(pk=kitchen.pk).update(owner=door, claim_hash="")
        Shop.objects.filter(pk=demo_shop("loaf").pk).update(name="Renamed", suspended=True)
        StaffRegister.objects.create(shop=demo_shop("loaf"), name="Extra")
        StaffRegister.objects.filter(shop=demo_shop("loaf"), name="Anna").update(retired=timezone.now())
        Trainee.objects.filter(user=demo_shop("loaf").owner).delete()
        Profile.objects.filter(user=door).update(email="mine@example.org")
        outbox, changes = len(mail.outbox), AccountChange.objects.count()
        made()
        kitchen.refresh_from_db()
        self.assertIsNone(kitchen.owner)
        self.assertTrue(kitchen.claim_hash)
        self.assertEqual(demo_shop("loaf").name, "The Daily Loaf")
        self.assertFalse(demo_shop("loaf").suspended)
        self.assertFalse(StaffRegister.objects.filter(name="Extra").exists())
        self.assertIsNone(StaffRegister.objects.get(shop=demo_shop("loaf"), name="Anna").retired)
        self.assertIsNotNone(Trainee.objects.get(user=demo_shop("loaf").owner).base_earned)
        self.assertEqual(Profile.objects.get(user=door).email, "door@" + demo.MAIL_DOMAIN)
        # Nobody was told, nothing was written down: these accounts are not anybody's
        self.assertEqual((len(mail.outbox), AccountChange.objects.count()), (outbox, changes))

    def test_it_asks_nobody_and_writes_to_nobody(self):
        with ExitStack() as stack:
            for guard in nobody_outside():
                stack.enter_context(guard)
            demo.populate()
            demo.populate()
            demo.links()
            demo.remove()
        self.assertEqual(mail.outbox, [])
        self.assertEqual(AccountChange.objects.count(), 0)

    def test_a_site_with_something_real_is_not_a_training_site_even_after_the_demo_was_made(self):
        made()
        real = real_shop_with_owner()
        before = census()
        with self.assertRaisesMessage(CommandError, "not empty"):
            run()
        with self.assertRaises(CommandError):
            run("--links")
        self.assertEqual(census(), before)
        real.refresh_from_db()
        self.assertEqual((real.name, real.lightning_address, real.owner.username),
                         ("Real Bakery", "bake@example.org", "email:feedface"))
        # Taking the demo away is still allowed, and leaves the real shop; then the site has nothing of the demo
        run("--remove")
        self.assertEqual(Shop.objects.count(), 1)


@override_settings(PUBLIC_URL=SITE, TELEGRAM_BOT_TOKEN="", NETWORK="signet", SITE_MODE="development")
class Remove(TestCase):
    def test_takes_away_all_of_it_and_only_it(self):
        made()
        real = real_shop_with_owner()
        self.assertGreater(Shop.objects.count(), 1)
        output = made(remove=True)
        self.assertEqual(census(), {"shops": 1, "users": 1, "profiles": 1, "staff": 1, "passes": 0, "changes": 0})
        self.assertIn("Removed 11 demo shops, 9 owners", output)
        real.refresh_from_db()
        self.assertEqual(real.lightning_address, "bake@example.org")
        self.assertEqual(real.owner.profile.email, "real@example.org")
        self.assertTrue(StaffRegister.objects.filter(name="Real helper").exists())

    def test_writes_down_no_deleted_account(self):
        made()
        made(remove=True)
        self.assertEqual(AccountChange.objects.count(), 0)
        self.assertEqual(mail.outbox, [])
        self.assertEqual(Shop.objects.count() + User.objects.count(), 0)

    def test_the_guardian_of_the_accounts_watches_again_afterwards(self):
        made()
        run("--remove")
        user = User.objects.create(username="email:aa")
        Shop.objects.create(name="A shop", owner=user)
        user.delete()
        self.assertEqual(AccountChange.objects.count(), 1)

    def test_a_demo_account_that_owns_a_real_shop_stays(self):
        made()
        door = User.objects.get(username="demo:door")
        real = Shop.objects.create(name="Handed over", owner=door, external_id="shop-2")
        run("--remove")
        self.assertTrue(User.objects.filter(pk=door.pk).exists())
        self.assertTrue(Shop.objects.filter(pk=real.pk).exists())
        self.assertEqual(Shop.objects.count(), 1)

    def test_a_shop_with_a_similar_name_is_not_a_demo_shop(self):
        made()
        Shop.objects.create(name="Cousin", external_id="demo:cousin")
        User.objects.create(username="demo:cousin")
        run("--remove")
        self.assertEqual(set(Shop.objects.values_list("name", flat=True)), {"Cousin"})
        self.assertTrue(User.objects.filter(username="demo:cousin").exists())

    def test_an_owner_who_wrote_their_own_email_in_the_panel_is_still_a_demo_owner(self):
        made()
        Profile.objects.filter(user__username="demo:door").update(email="volunteer@example.org")
        made()
        self.assertEqual(Profile.objects.get(user__username="demo:door").email, "door@" + demo.MAIL_DOMAIN)
        Profile.objects.filter(user__username="demo:door").update(email="volunteer@example.org")
        run("--remove")
        self.assertEqual(User.objects.count(), 0)

    def test_nothing_to_remove_is_not_an_error(self):
        self.assertIn("Removed 0 demo shops", made(remove=True))

    def test_what_a_volunteer_added_to_a_demo_shop_goes_with_it(self):
        made()
        shop = demo_shop("loaf")
        Closure.objects.create(shop=shop, first_day=timezone.localdate())
        run("--remove")
        self.assertEqual(Closure.objects.count(), 0)
        self.assertEqual(StaffRegister.objects.count(), 0)


@override_settings(PUBLIC_URL=SITE, TELEGRAM_BOT_TOKEN="", NETWORK="signet", SITE_MODE="development")
class PeriodicCommands(TestCase):
    """The commands that ask the providers leave the demo shops alone: their addresses do not answer, so they would only
    overwrite the stories with failures."""

    def setUp(self):
        cache.clear()

    def test_the_guardian_does_not_probe_or_forget_a_demo_shop(self):
        made()
        real = Shop.objects.create(name="Real", lightning_address="real@example.org")
        self.assertEqual(list(lightning_watch.watched()), [real])
        self.assertEqual(list(lightning_watch.due(timezone.now())), [real])
        gelato = demo_shop("gelato")
        with patch("shops.lnaddress.probe") as probe, patch.object(lightning_watch, "network_ok", return_value=True):
            probe.return_value = lnaddress.OK
            call_command("check_lightning", "--all", "--pause", "0", stdout=StringIO(), stderr=StringIO())
            probed = [call.args[0] for call in probe.call_args_list]
        self.assertEqual(probed, ["real@example.org"])
        after = demo_shop("gelato")
        self.assertEqual((after.lightning_status, after.lightning_failures, after.lightning_since),
                         (gelato.lightning_status, gelato.lightning_failures, gelato.lightning_since))
        self.assertEqual(demo_shop("loaf").lightning_status, "ok")

    def test_the_demo_shops_stay_out_of_nostr(self):
        from .models import real

        made()
        Shop.objects.create(name="Real", latitude=1, longitude=2)
        self.assertEqual(list(real(Shop.objects.all()).values_list("name", flat=True)), ["Real"])


@override_settings(PUBLIC_URL=SITE, TELEGRAM_BOT_TOKEN="", NETWORK="signet", SITE_MODE="development")
class SignIn(TestCase):
    def setUp(self):
        cache.clear()
        made()

    def sign_in(self, name):
        link = next(owner["url"] for owner in demo.links()["owners"] if owner["name"] == name)
        self.assertTrue(link.startswith(SITE + reverse("recovery_login") + "?token="))
        token = token_of(link)
        # The link opens a page with a button, and the button signs in
        self.assertEqual(self.client.get(reverse("recovery_login"), {"token": token}).status_code, 200)
        response = self.client.post(reverse("recovery_login"), {"token": token})
        self.assertRedirects(response, reverse("shops"), fetch_redirect_response=False)
        return token

    def test_the_command_prints_a_link_for_every_owner_and_for_the_shop_waiting(self):
        output = run("--links")
        for name in ("loaf", "door", "petals", "hardware", "gelato", "tailor", "herbs", "pencil", "records"):
            self.assertIn(f"  {name} (", output)
        self.assertIn("Blue Door Cafe, Paper & Ink Books", output)
        self.assertIn(reverse("claim_shop") + "?code=", output)
        self.assertNotIn("kitchen (", output)

    def test_a_link_signs_in_once(self):
        token = self.sign_in("loaf")
        self.assertEqual(self.client.get(reverse("shops")).status_code, 200)
        self.client.logout()
        self.assertEqual(self.client.post(reverse("recovery_login"), {"token": token}).status_code, 404)
        # A new run makes a new link
        self.sign_in("loaf")

    def test_every_owner_opens_their_panel_and_their_pages(self):
        pages = {"loaf": ["The Daily Loaf"], "door": ["Blue Door Cafe", "Paper &amp; Ink Books"], "petals": ["Petal Flowers"],
                 "hardware": ["Hardware Hub"], "gelato": ["Moonlight Gelato"], "tailor": ["Needle &amp; Thread Tailor"],
                 "herbs": ["Chamomile Herbalist"], "pencil": ["Pencil Stationery"], "records": ["The Old Record Shop"]}
        failing = patch("requests.sessions.Session.request", side_effect=requests.ConnectionError("nobody here"))
        with failing:
            for name, names in pages.items():
                self.client.logout()
                self.sign_in(name)
                panel = self.client.get(reverse("shops"))
                self.assertEqual(panel.status_code, 200, name)
                for shop_name in names:
                    self.assertContains(panel, shop_name)
                for shop in Shop.objects.filter(owner__username=f"demo:{name}"):
                    self.assertEqual(self.client.get(reverse("shop_detail", args=[shop.pk])).status_code, 200, shop.name)
                    if shop.has_register:
                        self.assertEqual(self.client.get(reverse("register", args=[shop.pk])).status_code, 200, shop.name)
                        self.assertEqual(self.client.get(reverse("practice", args=[shop.pk])).status_code, 200, shop.name)
                        self.assertEqual(self.client.get(reverse("staff_registers", args=[shop.pk])).status_code, 200, shop.name)

    def test_the_panel_of_the_shop_without_a_wallet_offers_to_connect_one(self):
        self.sign_in("petals")
        shop = demo_shop("petals")
        self.assertEqual(self.client.get(reverse("connect_wallet", args=[shop.pk])).status_code, 200)

    def test_the_public_pages_show_the_shops_that_are_on_the_map(self):
        failing = patch("requests.sessions.Session.request", side_effect=requests.ConnectionError("nobody here"))
        with failing:
            page = self.client.get(reverse("shop_map"))
            self.assertEqual(page.status_code, 200)
            shown = self.client.get("/shops.json")
            self.assertEqual(shown.status_code, 200)
            names = {shop["name"] for shop in shown.json()["shops"]} if isinstance(shown.json(), dict) else set()
            for shop in directory.public_shops():
                self.assertEqual(self.client.get(reverse("shop_detail", args=[shop.pk])).status_code, 200, shop.name)
        if names:
            self.assertIn("The Daily Loaf", names)
            # Waiting for a wallet: not on the map
            self.assertNotIn("Petal Flowers", names)
            self.assertNotIn("Grandma's Kitchen", names)

    def test_the_claim_link_gives_the_shop_to_whoever_takes_it_and_a_new_run_waits_for_the_next_one(self):
        self.sign_in("pencil")
        link = demo.links()["claim"][0]["url"]
        self.assertEqual(self.client.get(link).status_code, 200)
        code = parse_qs(urlsplit(link).query)["code"][0]
        response = self.client.post(reverse("claim_shop"), {"code": code})
        self.assertEqual(response.status_code, 302)
        kitchen = demo_shop("kitchen")
        self.assertEqual(kitchen.owner.username, "demo:pencil")
        self.assertEqual(self.client.get(link).status_code, 404)
        changes = AccountChange.objects.count()
        made()
        kitchen.refresh_from_db()
        self.assertIsNone(kitchen.owner)
        self.assertEqual(self.client.get(link).status_code, 200)
        self.assertEqual(AccountChange.objects.count(), changes)

    def test_the_codes_cannot_be_guessed_without_the_secret_key(self):
        one = demo.claim_code("kitchen")
        self.assertRegex(one, r"^[0-9a-f]{32}$")
        with override_settings(SECRET_KEY="another"):
            self.assertNotEqual(demo.claim_code("kitchen"), one)

    def test_staff_accounts_are_never_made_and_cannot_be_reached_with_these_links(self):
        self.assertFalse(User.objects.filter(is_staff=True).exists())
        self.assertFalse(User.objects.filter(is_superuser=True).exists())


@override_settings(PUBLIC_URL=SITE, TELEGRAM_BOT_TOKEN="", NETWORK="signet", SITE_MODE="development")
class AssociationPages(TestCase):
    def setUp(self):
        cache.clear()
        made()

    def test_the_dashboard_page_lists_the_stories(self):
        admin_session(self.client)
        with patch("requests.sessions.Session.request", side_effect=requests.ConnectionError("no one")):
            page = self.client.get(reverse("admin_dashboard"))
        self.assertEqual(page.status_code, 200)
        for name in ("Moonlight Gelato", "Grandma&#x27;s Kitchen", "Pencil Stationery"):
            self.assertContains(page, name)

    def test_the_dashboard_command_prints_them(self):
        out = StringIO()
        with self.assertRaises(SystemExit) as stopped:
            call_command("dashboard", stdout=out)
        self.assertEqual(stopped.exception.code, 2)
        self.assertIn("Moonlight Gelato", out.getvalue())

    def test_the_admin_lists_open(self):
        admin_session(self.client)
        for name in ("shops_shop", "shops_trainee"):
            self.assertEqual(self.client.get(reverse(f"admin:{name}_changelist")).status_code, 200, name)


@override_settings(PUBLIC_URL=SITE, TELEGRAM_BOT_TOKEN="", NETWORK="signet", SITE_MODE="development")
class AroundTheDemo(TestCase):
    """What the training site must not change in the rest of the software: the real shops' checks, the morning summary,
    and the export."""

    def setUp(self):
        cache.clear()
        made()

    def real_shop(self, **fields):
        values = {"name": "Real Bakery", "lightning_address": "bake@example.org", "latitude": 1, "longitude": 2}
        values.update(fields)
        return Shop.objects.create(**values)

    def test_fresh_demo_dates_do_not_hide_that_nothing_reads_the_real_shops(self):
        # On the training site alone, nothing is late: nothing there is read by a timer, by design
        codes = {item.code for item in dashboard.summary()["items"]}
        self.assertNotIn("guardian_late", codes)
        # With a real shop that was never read, the demo's dates must not say it is all fine
        self.real_shop()
        found = {item.code: item for item in dashboard.summary()["items"]}
        self.assertIn("guardian_late", found)
        self.assertIsNone(found["guardian_late"].shop)

    def test_the_morning_summary_is_never_about_demo_shops(self):
        # Only demo shops have things to do: no message, no call to Telegram or the mailbox
        with patch("shops.management.commands.dashboard.send_mail") as sent, \
                patch("shops.management.commands.dashboard.telegram") as chat:
            out = StringIO()
            call_command("dashboard", "--send", stdout=out)
        self.assertIn("Nothing to do today", out.getvalue())
        sent.assert_not_called()
        chat.assert_not_called()
        # A real shop with a problem: the message names it, and none of the demo shops
        self.real_shop(lightning_status="no_answer", lightning_since=timezone.now() - timedelta(days=2),
                       lightning_checked=timezone.now(), lightning_worked=timezone.now() - timedelta(days=3),
                       lightning_checked_address="bake@example.org", first_sale=timezone.now() - timedelta(days=30))
        with patch("shops.management.commands.dashboard.send_mail") as sent:
            with self.assertRaises(SystemExit) as stopped:
                call_command("dashboard", "--send", stdout=StringIO())
        self.assertEqual(stopped.exception.code, 10)
        text = sent.call_args.args[1]
        self.assertIn("Real Bakery", text)
        for spec in demo.SHOPS:
            self.assertNotIn(spec["name"], text)
        # What the shell shows is the whole list, for whoever looks at it
        out = StringIO()
        with self.assertRaises(SystemExit):
            call_command("dashboard", stdout=out)
        self.assertIn("Moonlight Gelato", out.getvalue())

    def test_the_export_for_another_site_leaves_demo_shops_out(self):
        from . import moving

        owner = User.objects.create(username="email:bake")
        real = self.real_shop(owner=owner)
        self.assertEqual([shop.pk for shop in moving.exported_shops()], [real.pk])
        document, _lost = moving.export()
        self.assertEqual([entry["name"] for entry in document["shops"]], ["Real Bakery"])

"""Moving shops between two bitmentor sites (shops/moving.py): export_shops, import_moved_shops, the panel's notice and
the page that connects the wallet of a moved shop."""
import csv
import json
import os
import stat
import tempfile
from datetime import date
from io import StringIO
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings

from . import moving
from .directory import public_shops
from .models import Profile, Shop, claim_digest
from .tests import steady_limits

KEY = "02" + "ab" * 32
OTHER_KEY = "03" + "cd" * 32


class Files:
    def setUp(self):
        super().setUp()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def path(self, name="shops.json"):
        return str(self.folder / name)

    def export(self, name="shops.json"):
        call_command("export_shops", self.path(name), stdout=StringIO())
        return json.loads(Path(self.path(name)).read_text())

    def write(self, document, name="in.json"):
        Path(self.path(name)).write_text(json.dumps(document) if not isinstance(document, str) else document)
        return self.path(name)

    def run_import(self, document, *options):
        out = StringIO()
        if "--apply" in options and "--owners" not in options:
            self.listings = getattr(self, "listings", 0) + 1
            options = (*options, "--owners", self.path(f"owners-{self.listings}.csv"))
        call_command("import_moved_shops", self.write(document), *options, stdout=out)
        return out.getvalue()

    def listing(self, name):
        with open(self.path(name), newline="", encoding="utf-8") as file:
            return {row["shop"]: row for row in csv.DictReader(file)}


def source_site():
    """A small site as the old one would be: a shop with a register and a wallet owner, one with an email owner, an
    unclaimed shop of the directory, a historic one."""
    users = get_user_model().objects
    wallet = users.create(username=KEY)
    Profile.objects.create(user=wallet, email="mario@example.org")
    mail = users.create(username="email:0123")
    Profile.objects.create(user=mail, email="anna@example.org")
    Shop.objects.create(owner=wallet, name="Central Bar", description="Coffee\nand cake", address="Main Street 1",
                        city="Example", latitude=41.9, longitude=12.55, website="https://bar.example.org",
                        email="bar@example.org", phone="+39 0461 000", discount=5, nip05_name="bar",
                        nostr_pubkey="a" * 64, telegram_chat="12345",
                        lightning_address="bar@wallet.example.org", lightning=True, certified_month=date(2026, 9, 1),
                        sign_printed="2026-10-01T10:00Z")
    Shop.objects.create(owner=mail, name="Bookshop", latitude=42.0, longitude=12.6,
                        lightning_address="anna@wallet.example.org", lightning=True)
    cyclist = users.create(username=OTHER_KEY)
    Shop.objects.create(owner=cyclist, name="Cyclist", latitude=42.05, longitude=12.65,
                        lightning_address="bike@wallet.example.org", lightning=True)
    Shop.objects.create(name="Directory shop", external_id="42", latitude=42.1, longitude=12.7, lightning=True)
    Shop.objects.create(name="Old shop", external_id="7", latitude=42.2, longitude=12.8, closed_on=date(2020, 1, 31),
                        suspended=True, listed=False)


def code_of(link):
    """The code of a one-time link: in the query, never in the path."""
    parts = urlsplit(link)
    assert parts.path.endswith("/shops/claim/"), link
    return parse_qs(parts.query)["code"][0]


def comparable(document):
    """The shops without their id, which changes, and their owner, who is found again by other ways (an email account,
    or the one-time link)."""
    return sorted(({key: value for key, value in row.items() if key not in ("id", "owner")} for row in document["shops"]),
                  key=lambda row: row["name"])


class Export(Files, TestCase):
    def setUp(self):
        super().setUp()
        source_site()

    def test_the_file_holds_details_and_owners_never_payments(self):
        document = self.export()
        self.assertEqual((document["format"], document["version"]), (moving.FORMAT, moving.VERSION))
        text = Path(self.path()).read_text()
        for secret in (KEY, OTHER_KEY, "bar@wallet.example.org", "anna@wallet.example.org", "2026-09", "2026-10-01",
                       "certified"):
            self.assertNotIn(secret, text)
        bar = next(row for row in document["shops"] if row["name"] == "Central Bar")
        self.assertEqual(bar["owner"], {"email": "mario@example.org", "wallet": True})
        self.assertTrue(bar["register"])
        self.assertFalse(bar["lightning"])
        self.assertEqual(bar["telegram_chat"], "12345")
        directory = next(row for row in document["shops"] if row["name"] == "Directory shop")
        self.assertEqual((directory["owner"], directory["register"], directory["lightning"]), (None, False, True))
        library = next(row for row in document["shops"] if row["name"] == "Bookshop")
        self.assertEqual(library["owner"], {"email": "anna@example.org", "wallet": False})
        cyclist = next(row for row in document["shops"] if row["name"] == "Cyclist")
        self.assertEqual(cyclist["owner"], {"email": "", "wallet": True})

    def test_the_file_is_private_and_never_overwritten(self):
        self.export()
        self.assertEqual(stat.S_IMODE(os.stat(self.path()).st_mode), 0o600)
        with self.assertRaisesMessage(CommandError, "already exists"):
            self.export()

    def test_a_historic_shop_moves_without_a_register_to_wait_for(self):
        Shop.objects.filter(name="Central Bar").update(closed_on=date(2026, 1, 31))
        bar = next(row for row in self.export()["shops"] if row["name"] == "Central Bar")
        self.assertFalse(bar["register"])

    def test_staff_owners_do_not_travel(self):
        admin = get_user_model().objects.create(username="admin", is_staff=True)
        Profile.objects.create(user=admin, email="admin@example.org")
        Shop.objects.create(owner=admin, name="Staff shop")
        out = StringIO()
        call_command("export_shops", self.path(), stdout=out)
        rows = {row["name"]: row for row in json.loads(Path(self.path()).read_text())["shops"]}
        self.assertIsNone(rows["Staff shop"]["owner"])
        self.assertNotIn("admin@example.org", Path(self.path()).read_text())
        self.assertIn("Staff shop", out.getvalue())


class Import(Files, TestCase):
    def setUp(self):
        super().setUp()
        source_site()
        self.document = self.export()
        # The arrival site starts empty
        Shop.objects.all().delete()
        Profile.objects.all().delete()
        get_user_model().objects.all().delete()

    def test_a_dry_run_writes_nothing_and_says_what_apply_would_do(self):
        out = self.run_import(self.document)
        self.assertIn("Dry run, nothing written", out)
        self.assertIn("5 moved, 0 already here, 0 not moved, of 5.", out)
        self.assertFalse(Shop.objects.exists())
        self.assertFalse(get_user_model().objects.exists())

    def test_shops_arrive_with_their_owners_a_new_id_and_no_payments(self):
        out = self.run_import(self.document, "--apply")
        self.assertIn("Import done", out)
        bar = Shop.objects.get(name="Central Bar")
        source_id = next(row["id"] for row in self.document["shops"] if row["name"] == "Central Bar")
        self.assertNotEqual(str(bar.pk), source_id)
        self.assertEqual(bar.moved_from, source_id)
        self.assertIsNotNone(bar.moved_in)
        self.assertTrue(bar.moved_register)
        # A wallet signs in with another key on another site: the owner's email gets a new account here
        self.assertTrue(bar.owner.username.startswith("email:"))
        self.assertEqual(bar.owner.profile.email, "mario@example.org")
        self.assertFalse(get_user_model().objects.filter(username__in=(KEY, OTHER_KEY)).exists())
        self.assertEqual(bar.lightning_address, "")
        self.assertIsNone(bar.certified_month)
        self.assertIsNone(bar.sign_printed)
        self.assertEqual((bar.description, bar.discount, bar.nip05_name, bar.latitude), ("Coffee\nand cake", 5, "bar", 41.9))
        library = Shop.objects.get(name="Bookshop")
        self.assertTrue(library.owner.username.startswith("email:"))
        self.assertEqual(library.owner.profile.email, "anna@example.org")
        old = Shop.objects.get(name="Old shop")
        self.assertEqual((old.closed_on, old.suspended, old.listed, old.owner), (date(2020, 1, 31), True, False, None))
        cyclist = Shop.objects.get(name="Cyclist")
        self.assertIsNone(cyclist.owner)
        self.assertEqual(len(cyclist.claim_hash), 64)

    def test_a_shop_that_had_a_register_waits_off_the_map_until_its_wallet(self):
        self.run_import(self.document, "--apply")
        on_map = set(public_shops().values_list("name", flat=True))
        self.assertEqual(on_map, {"Directory shop"})

    def test_a_waiting_shop_that_becomes_historic_is_back_on_the_map(self):
        self.run_import(self.document, "--apply")
        Shop.objects.filter(name="Bookshop").update(closed_on=date(2026, 12, 31))
        self.assertIn("Bookshop", set(public_shops().values_list("name", flat=True)))

    def test_importing_twice_changes_nothing(self):
        self.run_import(self.document, "--apply")
        before = list(Shop.objects.order_by("pk").values())
        out = self.run_import(self.document, "--apply")
        self.assertIn("0 moved, 5 already here", out)
        self.assertEqual(list(Shop.objects.order_by("pk").values()), before)
        self.assertEqual(get_user_model().objects.count(), 2)

    def test_export_import_export_gives_the_same_shops(self):
        self.run_import(self.document, "--apply")
        again = self.export("again.json")
        self.assertEqual(comparable(again), comparable(self.document))

    def test_an_account_already_here_never_gets_a_shop_from_the_file(self):
        """A changed file must not give a shop, or the private part, to an account that exists: only its link does."""
        users = get_user_model().objects
        anna = users.create(username="email:ffff")
        Profile.objects.create(user=anna, email="anna@example.org")
        out = self.run_import(self.document, "--apply", "--owners", self.path("o.csv"))
        library = Shop.objects.get(name="Bookshop")
        self.assertIsNone(library.owner)
        self.assertFalse(anna.shops.exists())
        self.assertIn("already has an account here", out)
        row = self.listing("o.csv")["Bookshop"]
        self.assertEqual((row["sign_in"], row["to_do"]), ("email", "open_link_then_connect_wallet"))
        self.assertIn("/shops/claim/", row["link"])
        self.assertEqual(library.claim_hash, claim_digest(code_of(row["link"])))

    def test_no_email_is_ever_written_on_an_account_here(self):
        empty = get_user_model().objects.create(username=KEY)
        self.run_import(self.document, "--apply")
        self.assertFalse(Profile.objects.filter(user=empty).exclude(email="").exists())
        self.assertFalse(empty.shops.exists())

    def test_a_configured_shop_here_is_never_overwritten(self):
        owner = get_user_model().objects.create(username=OTHER_KEY)
        here = Shop.objects.create(owner=owner, name="Mine", external_id="42", lightning_address="mine@ln.example.org")
        out = self.run_import(self.document, "--apply")
        self.assertIn("already set up here", out)
        here.refresh_from_db()
        self.assertEqual((here.name, here.owner, here.lightning_address), ("Mine", owner, "mine@ln.example.org"))
        self.assertFalse(Shop.objects.filter(name="Directory shop").exists())

    def test_an_unclaimed_directory_shop_here_takes_the_moved_details(self):
        here = Shop.objects.create(name="Directory shop (old text)", external_id="42")
        self.run_import(self.document, "--apply")
        here.refresh_from_db()
        self.assertEqual(here.name, "Directory shop")
        self.assertIsNotNone(here.moved_in)
        self.assertEqual(Shop.objects.filter(external_id="42").count(), 1)

    def test_a_taken_or_reserved_nostr_name_is_left_out(self):
        Shop.objects.create(name="Other", nip05_name="Bar")
        out = self.run_import(self.edited("Bookshop", nip05_name="admin"), "--apply")
        self.assertEqual(Shop.objects.get(name="Central Bar").nip05_name, "")
        self.assertEqual(Shop.objects.get(name="Bookshop").nip05_name, "")
        self.assertIn("Nostr name bar cannot be used here", out)
        self.assertIn("Nostr name admin cannot be used here", out)

    def edited(self, name, /, **values):
        document = json.loads(json.dumps(self.document))
        row = next(row for row in document["shops"] if row["name"] == name)
        row.update(values)
        return document

    def test_rows_go_through_the_shop_form_rules(self):
        cases = {"discount": 150, "latitude": 200, "website": "javascript:alert(1)", "email": "not an email",
                 "description": "x" * 1001, "name": "", "nostr_pubkey": "npub1xyz",
                 "address": "abandon " * 11 + "about", "external_id": "x" * 65, "closed_on": "31/01/2020"}
        for name, value in cases.items():
            with self.subTest(name):
                self.addCleanup(Shop.objects.all().delete)
                out = self.run_import(self.edited("Central Bar", **{name: value}), "--apply")
                Shop.objects.all().delete()
                self.assertIn("4 moved, 0 already here, 1 not moved", out)
                self.assertIn(f"why: {name}:", out)

    def test_escape_sequences_never_reach_the_terminal(self):
        document = self.edited("Central Bar", name="Bar\x1b[2J")
        document["site"] = "https://x\x1b]0;owned\x07"
        out = self.run_import(document)
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\x07", out)

    def test_invisible_controls_are_taken_out_of_the_texts(self):
        self.run_import(self.edited("Central Bar", name="Central‮ Bar"), "--apply")
        self.assertTrue(Shop.objects.filter(name="Central Bar").exists())

    def refused(self, document, reason):
        with self.assertRaisesMessage(CommandError, reason):
            self.run_import(document, "--apply")
        self.assertFalse(Shop.objects.exists())

    def test_hostile_or_broken_files_are_refused_whole(self):
        good = json.dumps(self.document)
        shop = self.document["shops"][0]
        cases = [
            ({**self.document, "version": 3}, "version 3"),
            ({**self.document, "version": True}, "version True"),
            ({**self.document, "format": "other"}, "not a file written by export_shops"),
            ({**self.document, "extra": 1}, "unexpected or missing keys"),
            ([], "not a file written by export_shops"),
            ({**self.document, "shops": {}}, "shops is not a list"),
            (self.edited(shop["name"], extra="x"), "unexpected keys ['extra']"),
            ({**self.document, "shops": [{k: v for k, v in shop.items() if k != "owner"}]}, "missing keys ['owner']"),
            (self.edited(shop["name"], discount=True), "discount is not a whole number"),
            (self.edited(shop["name"], discount="5"), "discount is not a whole number"),
            (self.edited(shop["name"], latitude="45"), "latitude is not a number"),
            (self.edited(shop["name"], listed="yes"), "listed is not true or false"),
            (self.edited(shop["name"], name=["a"]), "name is not a text"),
            (self.edited(shop["name"], description="x" * 5001), "description is not a text"),
            (self.edited(shop["name"], id="../../etc"), "id is not a UUID"),
            (self.edited(shop["name"], owner={"linking_key": KEY, "email": ""}), "owner is not an object"),
            (self.edited(shop["name"], owner={"email": "", "wallet": "yes"}), "wallet is not true or false"),
            ({**self.document, "shops": [shop, shop]}, "the same shop id appears twice"),
            ({**self.document, "shops": ["x"]}, "shop 1 is not an object"),
            (good.replace('"discount": 5', '"discount": 5, "discount": 0'), "a key appears twice"),
            (good.replace('"latitude": 41.9', '"latitude": NaN'), "not a number JSON allows"),
            (good.replace('"latitude": 41.9', '"latitude": 1e999'), "not a finite number"),
            (good[:-10], "not valid JSON"),
            ("[" * 100000 + "]" * 100000, "not valid JSON"),
        ]
        for document, reason in cases:
            with self.subTest(reason):
                self.refused(document, reason)

    def test_a_file_that_is_not_utf8_or_too_large_is_refused(self):
        Path(self.path("bad.json")).write_bytes(b'{"format": "\xff"}')
        with self.assertRaisesMessage(CommandError, "not UTF-8"):
            call_command("import_moved_shops", self.path("bad.json"), stdout=StringIO())
        with patch("shops.moving.MAX_BYTES", 100), patch("shops.management.commands.import_moved_shops.MAX_BYTES", 100):
            with self.assertRaisesMessage(CommandError, "larger than"):
                self.run_import(self.document)

    def test_the_owners_list_says_what_to_do_in_the_site_language(self):
        self.run_import(self.document, "--apply", "--owners", self.path("owners.csv"), "--language", "it")
        self.assertEqual(stat.S_IMODE(os.stat(self.path("owners.csv")).st_mode), 0o600)
        rows = self.listing("owners.csv")
        self.assertEqual(set(rows), {"Central Bar", "Bookshop", "Cyclist"})
        bar, cyclist = rows["Central Bar"], rows["Cyclist"]
        self.assertEqual((bar["email"], bar["sign_in"], bar["to_do"], bar["link"]), ("mario@example.org", "email", "connect_wallet", ""))
        self.assertIn("Central Bar", bar["message"])
        self.assertIn("mario@example.org", bar["message"])
        self.assertEqual((cyclist["email"], cyclist["sign_in"], cyclist["to_do"]), ("", "wallet", "open_link_then_connect_wallet"))
        self.assertIn(cyclist["link"], cyclist["message"])
        self.assertIn("wallet", cyclist["message"])

    def test_a_dry_run_lists_no_link_that_does_not_exist(self):
        self.run_import(self.document, "--owners", self.path("dry.csv"))
        cyclist = self.listing("dry.csv")["Cyclist"]
        self.assertEqual(cyclist["link"], "")
        self.assertIn("--apply", cyclist["message"])
        self.assertFalse(Shop.objects.exists())

    def test_apply_needs_the_list_of_owners(self):
        with self.assertRaisesMessage(CommandError, "--apply needs --owners"):
            call_command("import_moved_shops", self.write(self.document), "--apply", stdout=StringIO())
        self.assertFalse(Shop.objects.exists())

    def test_new_codes_replace_the_lost_links(self):
        self.run_import(self.document, "--apply", "--owners", self.path("a.csv"))
        old = self.listing("a.csv")["Cyclist"]["link"]
        out = self.run_import(self.document, "--apply", "--new-codes", "--owners", self.path("b.csv"))
        new = self.listing("b.csv")
        self.assertEqual(set(new), {"Cyclist"})
        self.assertNotEqual(new["Cyclist"]["link"], old)
        self.assertIn("old one no longer works", out)
        self.assertEqual(Shop.objects.get(name="Cyclist").claim_hash, claim_digest(code_of(new["Cyclist"]["link"])))

    def test_only_one_shop_gets_a_new_link(self):
        """--only with --new-codes: a new link for that shop alone, in a list of owners with the same row a full run
        writes; the other shops waiting keep the links already sent. By the id here or by the file's id."""
        document = self.edited("Bookshop", owner={"email": "", "wallet": True})
        self.run_import(document, "--apply", "--owners", self.path("a.csv"))
        first = self.listing("a.csv")
        library = Shop.objects.get(name="Bookshop")
        kept = Shop.objects.get(name="Cyclist").claim_hash
        out = self.run_import(document, "--apply", "--new-codes", "--only", str(library.pk), "--owners", self.path("b.csv"))
        alone = self.listing("b.csv")
        self.assertEqual(set(alone), {"Bookshop"})
        self.assertIn("0 moved, 1 already here, 0 not moved, of 1.", out)
        self.assertNotIn("Cyclist", out)
        self.assertNotEqual(alone["Bookshop"]["link"], first["Bookshop"]["link"])
        self.assertEqual(Shop.objects.get(pk=library.pk).claim_hash, claim_digest(code_of(alone["Bookshop"]["link"])))
        self.assertEqual(Shop.objects.get(name="Cyclist").claim_hash, kept)
        self.run_import(document, "--apply", "--new-codes", "--owners", self.path("all.csv"))
        everyone = self.listing("all.csv")
        self.assertEqual(set(everyone), {"Bookshop", "Cyclist"})
        for column in ("shop", "email", "sign_in", "to_do"):
            self.assertEqual(alone["Bookshop"][column], everyone["Bookshop"][column])
        self.run_import(document, "--apply", "--new-codes", "--only", library.moved_from.upper(), "--owners", self.path("c.csv"))
        self.assertEqual(set(self.listing("c.csv")), {"Bookshop"})

    def test_only_a_moved_shop_that_is_in_the_file(self):
        self.run_import(self.document, "--apply")
        here = Shop.objects.create(name="Born here")
        cases = [(str(here.pk), "was not moved here"), ("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "is not in"),
                 ("999999", "--only takes"), ("x\x1b[2J", "--only takes")]
        for number, (only, reason) in enumerate(cases):
            with self.subTest(only), self.assertRaisesMessage(CommandError, reason):
                self.run_import(self.document, "--apply", "--new-codes", "--only", only, "--owners", self.path(f"{number}.csv"))
            # Refused before the list of owners is opened: no empty file to mistake for one
            self.assertFalse(os.path.exists(self.path(f"{number}.csv")))
        with self.assertRaises(CommandError) as refused:
            self.run_import(self.document, "--only", "x\x1b[2J")
        self.assertNotIn("\x1b", str(refused.exception))

    def test_two_shops_of_one_new_email_go_to_one_account(self):
        document = self.edited("Cyclist", owner={"email": "Anna@Example.org", "wallet": False})
        out = self.run_import(document, "--apply")
        library, cyclist = Shop.objects.get(name="Bookshop"), Shop.objects.get(name="Cyclist")
        self.assertIsNotNone(library.owner)
        self.assertEqual(cyclist.owner, library.owner)
        self.assertEqual(cyclist.claim_hash, "")
        self.assertNotIn("already has an account", out)

    def test_new_codes_never_for_a_shop_already_taken(self):
        self.run_import(self.document, "--apply")
        owner = get_user_model().objects.create(username=KEY)
        cyclist = Shop.objects.get(name="Cyclist")
        cyclist.owner = owner
        cyclist.save()
        self.run_import(self.document, "--apply", "--new-codes", "--owners", self.path("c.csv"))
        self.assertEqual(self.listing("c.csv"), {})
        self.assertEqual(Shop.objects.get(name="Cyclist").claim_hash, "")

    def test_new_codes_in_a_dry_run_say_they_do_nothing(self):
        self.run_import(self.document, "--apply")
        before = Shop.objects.get(name="Cyclist").claim_hash
        self.assertIn("only with --apply", self.run_import(self.document, "--new-codes"))
        self.assertEqual(Shop.objects.get(name="Cyclist").claim_hash, before)

    def test_the_report_says_what_was_not_moved(self):
        self.assertIn("the certificate starts again", self.run_import(self.document))

    def test_import_shops_never_rewrites_a_moved_shop(self):
        self.run_import(self.document, "--apply")
        Path(self.path("directory.json")).write_text(json.dumps([{"external_id": "42", "name": "Rewritten"}]))
        call_command("import_shops", self.path("directory.json"), stdout=StringIO())
        self.assertTrue(Shop.objects.filter(name="Directory shop", external_id="42").exists())
        self.assertFalse(Shop.objects.filter(name="Rewritten").exists())

    def test_an_id_in_another_spelling_is_the_same_shop(self):
        self.run_import(self.document, "--apply")
        document = json.loads(json.dumps(self.document))
        for row in document["shops"]:
            row["id"] = row["id"].upper()
        self.assertIn("0 moved, 5 already here", self.run_import(document, "--apply"))

    def test_a_replaced_directory_shop_keeps_what_this_site_decided(self):
        Shop.objects.create(name="Directory", external_id="42", suspended=True, listed=False, closed_on=date(2021, 5, 1))
        self.run_import(self.document, "--apply")
        shop = Shop.objects.get(external_id="42")
        self.assertEqual((shop.name, shop.suspended, shop.listed), ("Directory shop", True, False))
        # The file says it is not historic: the closing day of the old directory entry goes
        self.assertIsNone(shop.closed_on)

    def test_several_shops_with_the_same_external_id_are_reported(self):
        Shop.objects.create(name="A", external_id="42")
        Shop.objects.create(name="B", external_id="42")
        self.assertIn("2 shops here have the external id 42", self.run_import(self.document))

    def test_a_shop_name_like_a_formula_stays_text_in_the_owners_list(self):
        self.run_import(self.edited("Central Bar", name="=HYPERLINK(1)"), "--apply", "--owners", self.path("o.csv"))
        self.assertIn("'=HYPERLINK(1)", Path(self.path("o.csv")).read_text())

    def test_the_owners_list_never_overwrites_a_file_and_nothing_is_imported(self):
        Path(self.path("o.csv")).write_text("keep")
        with self.assertRaisesMessage(CommandError, "already exists"):
            self.run_import(self.document, "--apply", "--owners", self.path("o.csv"))
        self.assertEqual(Path(self.path("o.csv")).read_text(), "keep")
        self.assertFalse(Shop.objects.exists())


@override_settings(NETWORK="signet")
class ConnectWallet(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create(username=KEY)
        self.client.force_login(self.user)
        self.shop = Shop.objects.create(owner=self.user, name="Bar", latitude=41.9, longitude=12.5,
                                        moved_register=True, moved_from="x", moved_in="2026-11-04T10:00Z")
        probe = patch("shops.views.refusal", return_value=None)
        probe.start()
        self.addCleanup(probe.stop)

    def url(self, shop=None):
        return f"/en/shops/connect/{(shop or self.shop).id}/"

    def test_the_panel_says_the_shop_arrived_and_what_to_do(self):
        page = self.client.get("/en/shops/")
        self.assertContains(page, "Your shop has arrived here")
        self.assertContains(page, f'href="{self.url()}"')
        self.assertNotContains(page, "No register: payments for this shop are not set up on this site.")

    def test_connecting_makes_the_register_and_puts_the_shop_on_the_map(self):
        self.assertNotIn(self.shop, public_shops())
        response = self.client.post(self.url(), {"lightning_address": "bar@example.org"})
        self.assertRedirects(response, "/en/shops/?saved=1", fetch_redirect_response=False)
        self.shop.refresh_from_db()
        self.assertEqual(self.shop.lightning_address, "bar@example.org")
        self.assertTrue(self.shop.lightning)
        self.assertEqual(self.shop.name, "Bar")
        self.assertIn(self.shop, public_shops())
        self.assertNotContains(self.client.get("/en/shops/"), "Your shop has arrived here")

    def test_once_connected_the_page_never_opens_again(self):
        Shop.objects.filter(pk=self.shop.pk).update(lightning_address="bar@example.org")
        response = self.client.post(self.url(), {"lightning_address": "thief@example.org"})
        self.assertRedirects(response, "/en/shops/", fetch_redirect_response=False)
        self.shop.refresh_from_db()
        self.assertEqual(self.shop.lightning_address, "bar@example.org")

    def test_only_the_owner_signed_in(self):
        other = Shop.objects.create(owner=get_user_model().objects.create(username=OTHER_KEY), name="Other")
        self.assertEqual(self.client.get(self.url(other)).status_code, 404)
        self.client.logout()
        self.assertEqual(self.client.get(self.url()).status_code, 302)
        self.assertEqual(self.client.post(self.url(), {"lightning_address": "x@example.org"}).status_code, 302)
        self.assertEqual(Shop.objects.get(pk=self.shop.pk).lightning_address, "")

    def test_a_suspended_shop_takes_no_register(self):
        Shop.objects.filter(pk=self.shop.pk).update(suspended=True)
        self.assertRedirects(self.client.post(self.url(), {"lightning_address": "bar@example.org"}), "/en/shops/",
                             fetch_redirect_response=False)
        self.assertEqual(Shop.objects.get(pk=self.shop.pk).lightning_address, "")

    def test_a_historic_shop_takes_no_register(self):
        Shop.objects.filter(pk=self.shop.pk).update(closed_on=date(2020, 1, 1))
        self.assertRedirects(self.client.get(self.url()), "/en/shops/", fetch_redirect_response=False)

    def test_a_refused_address_writes_nothing(self):
        with patch("shops.views.refusal", return_value="does not tell the register"):
            self.assertContains(self.client.post(self.url(), {"lightning_address": "bar@example.org"}), "does not tell the register")
        self.assertEqual(Shop.objects.get(pk=self.shop.pk).lightning_address, "")

    def test_the_form_has_only_the_lightning_address_and_needs_it(self):
        page = self.client.get(self.url())
        self.assertContains(page, 'name="lightning_address"')
        self.assertNotContains(page, 'name="name"')
        self.assertNotContains(page, 'name="destination"')
        self.assertContains(self.client.post(self.url(), {}), "Add your Lightning address.")

    def test_a_directory_shop_given_to_its_owner_gets_a_quiet_button(self):
        Shop.objects.filter(pk=self.shop.pk).update(moved_register=False, moved_in=None)
        page = self.client.get("/en/shops/")
        self.assertNotContains(page, "Your shop has arrived here")
        self.assertContains(page, f'href="{self.url()}"')


class Claim(TestCase):
    """The one-time link of a shop that waits for its owner."""
    CODE = "A" * 31 + "b"

    def setUp(self):
        self.shop = Shop.objects.create(name="Cyclist", moved_register=True, moved_in="2026-11-04T10:00Z", moved_from="x",
                                        claim_hash=claim_digest(self.CODE))
        self.user = get_user_model().objects.create(username=KEY)
        self.client.force_login(self.user)

    def get(self, code=None):
        return self.client.get("/en/shops/claim/", {"code": code or self.CODE})

    def post(self, code=None):
        return self.client.post("/en/shops/claim/", {"code": code or self.CODE})

    def test_an_address_of_another_owner_is_refused(self):
        Shop.objects.filter(pk=self.shop.pk).update(lightning_address="Cyclist@wallet.example")
        Shop.objects.create(name="Other", owner=get_user_model().objects.create(username="other"),
                            lightning_address="cyclist@wallet.example")
        page = self.post()
        self.assertContains(page, "already used by another shop", status_code=409)
        self.assertNotContains(page, "Other", status_code=409)
        self.assertIsNone(Shop.objects.get(pk=self.shop.pk).owner)
        # The same owner may have it on several shops
        Shop.objects.filter(name="Other").update(owner=self.user)
        self.assertEqual(self.post().status_code, 302)
        self.assertEqual(Shop.objects.get(pk=self.shop.pk).owner, self.user)

    def test_the_owner_confirms_and_the_link_is_spent(self):
        self.assertContains(self.get(), "Take back Cyclist")
        self.assertIsNone(Shop.objects.get(pk=self.shop.pk).owner)
        self.assertRedirects(self.post(), "/en/shops/", fetch_redirect_response=False)
        shop = Shop.objects.get(pk=self.shop.pk)
        self.assertEqual((shop.owner, shop.claim_hash), (self.user, ""))
        self.assertContains(self.client.get("/en/shops/"), "Your shop has arrived here")
        other = get_user_model().objects.create(username=OTHER_KEY)
        self.client.force_login(other)
        self.assertEqual(self.post().status_code, 404)
        self.assertEqual(Shop.objects.get(pk=self.shop.pk).owner, self.user)

    def test_a_wrong_code_finds_nothing(self):
        for code in ("B" * 32, "short", "A" * 31 + "!"):
            with self.subTest(code):
                self.assertEqual(self.post(code).status_code, 404)
        self.assertIsNone(Shop.objects.get(pk=self.shop.pk).owner)

    def test_only_signed_in_and_never_staff(self):
        self.client.logout()
        response = self.get()
        self.assertEqual(response.status_code, 302)
        location = urlsplit(response["Location"])
        # Signing in keeps the code for afterwards, in the query: never in a path the access logs write
        self.assertNotIn(self.CODE, location.path)
        self.assertEqual(parse_qs(location.query)["next"], [f"/en/shops/claim/?code={self.CODE}"])
        self.assertEqual(self.post().status_code, 302)
        staff = get_user_model().objects.create(username="admin", is_staff=True)
        self.client.force_login(staff)
        # A staff account is a visitor outside /admin/ (config/guard.py): it does not even get to the «gone» page
        response = self.post()
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith("/en/shops/?next="))
        self.assertIsNone(Shop.objects.get(pk=self.shop.pk).owner)

    def test_a_shop_with_an_owner_cannot_be_taken(self):
        owner = get_user_model().objects.create(username=OTHER_KEY)
        Shop.objects.filter(pk=self.shop.pk).update(owner=owner)
        self.assertEqual(self.post().status_code, 404)
        self.assertEqual(Shop.objects.get(pk=self.shop.pk).owner, owner)

    def test_too_many_tries_wait(self):
        self.enterContext(steady_limits())
        with patch("shops.views.CLAIMS_PER_IP", 2):
            for _ in range(2):
                self.get("B" * 32)
            self.assertEqual(self.post().status_code, 429)
        self.assertIsNone(Shop.objects.get(pk=self.shop.pk).owner)

    def test_the_page_sends_the_code_back_in_the_body(self):
        page = self.get()
        self.assertContains(page, f'<input type="hidden" name="code" value="{self.CODE}">')
        self.assertContains(page, 'action="/en/shops/claim/"')

    def test_an_owner_set_from_the_admin_spends_the_link(self):
        shop = Shop.objects.get(pk=self.shop.pk)
        shop.owner = self.user
        shop.save()
        self.assertEqual(Shop.objects.get(pk=self.shop.pk).claim_hash, "")
        shop.owner = None
        shop.claim_hash = claim_digest(self.CODE)
        shop.save()
        shop.owner = self.user
        shop.save(update_fields=["owner"])
        self.assertEqual(Shop.objects.get(pk=self.shop.pk).claim_hash, "")

    def test_the_directory_and_its_numbers_skip_a_shop_waiting_for_its_wallet(self):
        Shop.objects.filter(pk=self.shop.pk).update(external_id="42", latitude=41.9, longitude=12.5)
        self.assertEqual(self.client.get("/en/shops/e/42/").status_code, 404)
        Shop.objects.filter(pk=self.shop.pk).update(lightning_address="bar@example.org")
        self.assertEqual(self.client.get("/en/shops/e/42/").status_code, 302)

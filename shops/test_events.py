"""The events the community's own code can listen to (shops/events.py): each sent once per fact, with its arguments,
after the commit and never after a rollback; a receiver that raises breaks nothing and is written in the log."""
import json
import tempfile
from datetime import timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.db import transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from config.community import community

from . import approval, events, lightning_watch
from .models import LightningStatus, Profile, Shop, claim_digest
from .test_demo import made
from .test_moving import Files, source_site

User = get_user_model()


class Heard:
    """with Heard(events.x) as heard: the calls the signal made, as dictionaries of its arguments."""

    def __init__(self, signal, raising=False):
        self.signal, self.raising, self.calls = signal, raising, []

    def receiver(self, sender, **kwargs):
        kwargs.pop("signal")
        self.calls.append({"sender": sender, **kwargs})
        if self.raising:
            raise RuntimeError("a mistake in the community's receiver")

    def __enter__(self):
        self.signal.connect(self.receiver, weak=False)
        return self.calls

    def __exit__(self, *exc):
        self.signal.disconnect(self.receiver)


class Helper(TestCase):
    def test_nothing_happens_without_receivers(self):
        with patch("shops.events.transaction.on_commit") as later:
            events.send(events.shop_created, Shop, shop=None, user=None, source="site")
        later.assert_not_called()

    def test_sent_after_the_commit_and_never_after_a_rollback(self):
        with Heard(events.shop_created) as heard:
            with self.captureOnCommitCallbacks(execute=True):
                events.send(events.shop_created, Shop, shop="x", user=None, source="site")
                self.assertEqual(heard, [])
            self.assertEqual(heard, [{"sender": Shop, "shop": "x", "user": None, "source": "site"}])
            with self.captureOnCommitCallbacks(execute=True) as callbacks:
                try:
                    with transaction.atomic():
                        events.send(events.shop_created, Shop, shop="y", user=None, source="site")
                        raise ValueError
                except ValueError:
                    pass
            self.assertEqual(callbacks, [])
        self.assertEqual(len(heard), 1)

    def test_a_receiver_that_raises_is_logged_and_the_others_still_hear(self):
        with Heard(events.shop_created, raising=True) as first, Heard(events.shop_created) as second, \
                self.assertLogs("shops.events", "ERROR") as logs, self.captureOnCommitCallbacks(execute=True):
            events.send(events.shop_created, Shop, shop="x", user=None, source="site")
        self.assertEqual((len(first), len(second)), (1, 1))
        line = logs.output[0]
        self.assertIn("shop_created", line)
        self.assertIn("Heard.receiver", line)
        self.assertIn("RuntimeError", line)

    def test_now_is_at_once_even_inside_a_transaction(self):
        with Heard(events.theme_file_served) as heard, transaction.atomic():
            events.send(events.theme_file_served, None, now=True, path="a", first_byte=True, client_kind="browser")
            self.assertEqual(len(heard), 1)


class Created(Files, TestCase):
    def test_from_the_sites_form(self):
        user = User.objects.create(username="k")
        self.client.force_login(user)
        with Heard(events.shop_created) as heard, patch("shops.views.refusal", return_value=None), \
                self.captureOnCommitCallbacks(execute=True):
            self.client.post("/en/shops/new/", {"name": "Bar", "lightning_address": "mario@example.org"})
            # The same form sent twice finds the first shop: no second event
            self.client.post("/en/shops/new/", {"name": "Bar", "lightning_address": "mario@example.org"})
        shop = Shop.objects.get(name="Bar")
        self.assertEqual(heard, [{"sender": Shop, "shop": shop, "user": user, "source": "site"}])

    def test_from_the_sites_form_without_a_wallet(self):
        user = User.objects.create(username="k")
        self.client.force_login(user)
        with Heard(events.shop_created) as heard, self.captureOnCommitCallbacks(execute=True):
            self.client.post("/en/shops/new/", {"name": "Bar", "lightning_address": "", "later": "on"})
        self.assertEqual([(call["shop"].name, call["source"]) for call in heard], [("Bar", "site")])

    def test_a_refused_address_creates_nothing_and_says_nothing(self):
        self.client.force_login(User.objects.create(username="k"))
        with Heard(events.shop_created) as heard, patch("shops.views.refusal", return_value="no"), \
                self.captureOnCommitCallbacks(execute=True):
            self.client.post("/en/shops/new/", {"name": "Bar", "lightning_address": "mario@example.org"})
        self.assertEqual(heard, [])

    def test_from_the_admin(self):
        from config.urls import ADMIN_SESSION

        staff = User.objects.create_superuser("boss", password="x")
        self.client.force_login(staff)
        session = self.client.session
        session[ADMIN_SESSION] = True
        session.save()
        field = Shop._meta.get_field("approved_on")
        with patch.object(field, "get_default", lambda: None):
            form = self.client.get("/admin/shops/shop/add/").context["adminform"].form
            data = {name: value for name, value in form.initial.items() if value is not None and not callable(value)}
            data.update({"name": "Written", "discount": 0, "closures-TOTAL_FORMS": 0, "closures-INITIAL_FORMS": 0,
                         "places-TOTAL_FORMS": 0, "places-INITIAL_FORMS": 0})
            with Heard(events.shop_created) as heard, self.captureOnCommitCallbacks(execute=True):
                self.client.post("/admin/shops/shop/add/", data)
                shop = Shop.objects.get(name="Written")
                # A change of the same shop is not a creation
                self.client.post(f"/admin/shops/shop/{shop.pk}/change/", {**data, "name": "Rewritten"})
        self.assertEqual(heard, [{"sender": Shop, "shop": shop, "user": staff, "source": "admin"}])

    def test_from_import_shops_one_for_each_new_shop(self):
        rows = [{"external_id": "1", "name": "One"}, {"external_id": "2", "name": "Two"}]
        Path(self.path("list.json")).write_text(json.dumps(rows))
        with Heard(events.shop_created) as heard, self.captureOnCommitCallbacks(execute=True):
            call_command("import_shops", self.path("list.json"), stdout=StringIO())
            # Again: the shops are updated, none is new
            call_command("import_shops", self.path("list.json"), stdout=StringIO())
        self.assertEqual(sorted((call["shop"].name, call["source"], call["user"]) for call in heard),
                         [("One", "import", None), ("Two", "import", None)])

    def test_an_import_that_fails_halfway_says_nothing(self):
        rows = [{"external_id": "1", "name": "One"}, {"external_id": "2", "name": "Two", "discount": 500}]
        Path(self.path("list.json")).write_text(json.dumps(rows))
        with Heard(events.shop_created) as heard, self.captureOnCommitCallbacks(execute=True), \
                self.assertRaises(CommandError):
            call_command("import_shops", self.path("list.json"), stdout=StringIO())
        self.assertEqual(heard, [])
        self.assertFalse(Shop.objects.exists())

    def moved_document(self):
        source_site()
        document = self.export()
        Shop.objects.all().delete()
        Profile.objects.all().delete()
        User.objects.all().delete()
        return document

    def test_from_a_move_once_for_each_new_shop_and_never_in_a_dry_run(self):
        document = self.moved_document()
        with Heard(events.shop_created) as heard, self.captureOnCommitCallbacks(execute=True):
            self.run_import(document)
            self.assertEqual(heard, [])
            self.run_import(document, "--apply")
            # Already here: nothing new
            self.run_import(document, "--apply")
        self.assertEqual(len(heard), len(document["shops"]))
        self.assertEqual({call["source"] for call in heard}, {"move"})
        self.assertEqual({call["user"] for call in heard}, {None})
        self.assertEqual(sorted(call["shop"].name for call in heard), sorted(Shop.objects.values_list("name", flat=True)))

    def test_a_moved_shop_taking_the_place_of_one_of_the_list_is_not_new(self):
        document = self.moved_document()
        directory = next(row for row in document["shops"] if row["name"] == "Directory shop")
        Shop.objects.create(name="Directory shop", external_id=directory["external_id"])
        with Heard(events.shop_created) as heard, self.captureOnCommitCallbacks(execute=True):
            self.run_import(document, "--apply")
        self.assertNotIn("Directory shop", [call["shop"].name for call in heard])
        self.assertEqual(len(heard), len(document["shops"]) - 1)

    def test_an_unclaimed_shop_of_the_list_given_to_its_owner_by_a_move_is_claimed(self):
        document = self.moved_document()
        bar = next(row for row in document["shops"] if row["name"] == "Central Bar")
        bar["external_id"] = "99"
        here = Shop.objects.create(name="Bar of the list", external_id="99")
        Shop.objects.filter(pk=here.pk).update(private_approved=timezone.now(), private_approved_by="staff")
        with Heard(events.shop_claimed) as heard, Heard(events.shop_created) as created:
            with self.captureOnCommitCallbacks(execute=True):
                self.run_import(document)
            self.assertEqual(heard, [])
            with self.captureOnCommitCallbacks(execute=True):
                self.run_import(document, "--apply")
                self.run_import(document, "--apply")
        self.assertEqual(len(heard), 1)
        shop = Shop.objects.get(pk=here.pk)
        self.assertEqual((heard[0]["shop"].pk, heard[0]["user"], heard[0]["via"]), (here.pk, shop.owner, "move"))
        self.assertEqual(heard[0]["shop"].owner.profile.email, "mario@example.org")
        self.assertEqual(heard[0]["shop"].name, "Central Bar")
        self.assertNotIn(here.pk, [call["shop"].pk for call in created])

    def test_a_shop_new_here_with_its_owner_is_created_not_claimed(self):
        document = self.moved_document()
        with Heard(events.shop_claimed) as heard, self.captureOnCommitCallbacks(execute=True):
            self.run_import(document, "--apply")
        self.assertEqual(heard, [])
        self.assertTrue(Shop.objects.filter(name="Central Bar", owner__isnull=False).exists())

    @override_settings(TELEGRAM_BOT_TOKEN="", NETWORK="signet", SITE_MODE="development")
    def test_from_the_demo_once(self):
        with Heard(events.shop_created) as heard:
            with self.captureOnCommitCallbacks(execute=True):
                made()
            count = len(heard)
            with self.captureOnCommitCallbacks(execute=True):
                made()
        self.assertTrue(count)
        self.assertEqual(len(heard), count)
        self.assertEqual({(call["source"], call["user"]) for call in heard}, {("demo", None)})
        self.assertEqual(count, Shop.objects.count())


class Claimed(TestCase):
    CODE = "A" * 31 + "b"

    def setUp(self):
        self.shop = Shop.objects.create(name="Cyclist", moved_register=True, moved_in="2026-11-04T10:00Z", moved_from="x",
                                        claim_hash=claim_digest(self.CODE))
        self.user = User.objects.create(username="k")
        self.client.force_login(self.user)

    def test_once_with_the_new_owner(self):
        with Heard(events.shop_claimed) as heard, self.captureOnCommitCallbacks(execute=True):
            self.client.get("/en/shops/claim/", {"code": self.CODE})
            self.assertEqual(self.client.post("/en/shops/claim/", {"code": self.CODE}).status_code, 302)
            self.assertEqual(self.client.post("/en/shops/claim/", {"code": self.CODE}).status_code, 404)
        self.assertEqual(len(heard), 1)
        self.assertEqual((heard[0]["shop"], heard[0]["user"], heard[0]["via"]), (self.shop, self.user, "claim_link"))
        self.assertEqual((heard[0]["shop"].owner, heard[0]["shop"].claim_hash), (self.user, ""))

    def test_a_receiver_that_raises_does_not_stop_the_claim(self):
        with Heard(events.shop_claimed, raising=True), self.assertLogs("shops.events", "ERROR"), \
                self.captureOnCommitCallbacks(execute=True):
            response = self.client.post("/en/shops/claim/", {"code": self.CODE})
        self.assertRedirects(response, "/en/shops/", fetch_redirect_response=False)
        self.assertEqual(Shop.objects.get(pk=self.shop.pk).owner, self.user)

    def test_the_receiver_gets_the_row_as_it_is_after_the_claim(self):
        """The UPDATE also takes away the approval of the private part: a receiver that saves the shop writes nothing
        stale back."""
        Shop.objects.filter(pk=self.shop.pk).update(private_approved=timezone.now(), private_approved_by="staff")
        seen = []

        def saves(sender, shop, **kwargs):
            seen.append((shop.owner, shop.claim_hash, shop.private_approved, shop.private_approved_by))
            shop.save()

        events.shop_claimed.connect(saves, weak=False)
        self.addCleanup(events.shop_claimed.disconnect, saves)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post("/en/shops/claim/", {"code": self.CODE})
        self.assertEqual(seen, [(self.user, "", None, "")])
        shop = Shop.objects.get(pk=self.shop.pk)
        self.assertEqual((shop.owner, shop.private_approved, shop.claim_hash), (self.user, None, ""))


class Approved(TestCase):
    def setUp(self):
        field = Shop._meta.get_field("approved_on")
        with patch.object(field, "get_default", lambda: None):
            self.shop = Shop.objects.create(name="Bar", latitude=41.9, longitude=12.5)
        self.staff = User.objects.create(username="staff", is_staff=True)

    def test_one_approved_shop_by_a_user_once(self):
        with Heard(events.shop_approved) as heard, self.captureOnCommitCallbacks(execute=True):
            approval.approve_or_hold(Shop.objects.all(), self.staff)
            approval.approve_or_hold(Shop.objects.all(), self.staff)
        self.assertEqual(heard, [{"sender": Shop, "shop": self.shop, "by": "staff"}])
        self.assertIsNotNone(heard[0]["shop"].approved_on)

    def test_with_by(self):
        with Heard(events.shop_approved) as heard, self.captureOnCommitCallbacks(execute=True):
            outcome = approval.approve_or_hold(Shop.objects.all(), None, by="[team key 3] Anna")
        self.assertEqual(outcome.approved, 1)
        self.assertEqual([call["by"] for call in heard], ["[team key 3] Anna"])
        self.assertEqual(Shop.objects.get(pk=self.shop.pk).approved_by, "[team key 3] Anna")

    def test_none_for_a_held_shop(self):
        with Heard(events.shop_approved) as heard, patch("shops.approval.approval_signals.asks", return_value=True), \
                self.captureOnCommitCallbacks(execute=True):
            outcome = approval.approve_or_hold(Shop.objects.all(), self.staff)
        self.assertEqual((outcome.approved, heard), (0, []))

    def test_none_when_somebody_else_approved_it_meanwhile(self):
        def somebody_else_first(shops):
            # Between the reading of the waiting shops and their approval
            Shop.objects.filter(pk=self.shop.pk).update(approved_on=timezone.now(), approved_by="other")
            return {shop.pk: [] for shop in shops}

        with Heard(events.shop_approved) as heard, \
                patch("shops.approval.approval_signals.signals_of", side_effect=somebody_else_first), \
                self.captureOnCommitCallbacks(execute=True):
            outcome = approval.approve_or_hold(Shop.objects.all(), self.staff)
        self.assertEqual((outcome.approved, heard), (0, []))

    def test_none_after_a_rollback(self):
        with Heard(events.shop_approved) as heard, self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    approval.approve_or_hold(Shop.objects.all(), self.staff)
                    raise ValueError
            except ValueError:
                pass
        self.assertEqual(heard, [])

    def test_none_for_a_shop_approved_as_it_is_brought(self):
        with Heard(events.shop_approved) as heard, self.captureOnCommitCallbacks(execute=True):
            shop = Shop(name="Imported")
            approval.stamp(shop, "import")
            shop.save()
        self.assertEqual(heard, [])


@override_settings(TELEGRAM_BOT_TOKEN="", TELEGRAM_MODERATION_CHAT="")
class Guardian(TestCase):
    def setUp(self):
        self.shop = Shop.objects.create(owner=User.objects.create(username="k"), name="Bar",
                                        lightning_address="bar@example.org")
        self.now = timezone.now()

    def probe(self, result, after):
        self.now += after
        with patch("shops.lightning_watch.timezone.now", return_value=self.now):
            return lightning_watch.record(self.shop.pk, "bar@example.org", result)[1]

    def test_broken_and_working_once_each_at_the_change_of_status(self):
        with Heard(events.lightning_address_broken) as broken, Heard(events.lightning_address_working) as working, \
                self.captureOnCommitCallbacks(execute=True):
            self.probe(None, timedelta(0))
            self.probe(LightningStatus.NO_ANSWER, timedelta(hours=6))
            self.assertEqual(broken, [])
            self.assertEqual(self.probe(LightningStatus.NO_ANSWER, timedelta(hours=1)), lightning_watch.BROKE)
            self.probe(LightningStatus.NO_ANSWER, timedelta(hours=1))
            self.assertEqual(working, [])
            self.assertEqual(self.probe(None, timedelta(hours=1)), lightning_watch.FIXED)
            self.probe(None, timedelta(hours=6))
        self.assertEqual(broken, [{"sender": Shop, "shop": self.shop}])
        self.assertEqual(working, [{"sender": Shop, "shop": self.shop}])
        self.assertEqual(broken[0]["shop"].lightning_status, LightningStatus.NO_ANSWER)

    def test_an_unsure_probe_says_nothing(self):
        with Heard(events.lightning_address_broken) as broken, Heard(events.lightning_address_working) as working, \
                self.captureOnCommitCallbacks(execute=True):
            for _ in range(3):
                self.probe(lightning_watch.UNSURE, timedelta(hours=1))
        self.assertEqual((broken, working), ([], []))


class AddressChanged(TestCase):
    def setUp(self):
        self.shop = Shop.objects.create(owner=User.objects.create(username="k"), name="Bar",
                                        lightning_address="bar@example.org")

    def test_save_update_and_bulk_update_each_once(self):
        with Heard(events.lightning_address_changed) as heard, self.captureOnCommitCallbacks(execute=True):
            self.shop.lightning_address = "new@example.org"
            self.shop.save()
            Shop.objects.filter(pk=self.shop.pk).update(lightning_address="other@example.org")
            self.shop.lightning_address = ""
            Shop.objects.bulk_update([self.shop], ["lightning_address"])
            # Saves that leave the address alone say nothing
            self.shop.name = "Renamed"
            self.shop.save()
            Shop.objects.filter(pk=self.shop.pk).update(lightning_address="")
        self.assertEqual([(call["before"], call["after"]) for call in heard],
                         [("bar@example.org", "new@example.org"), ("new@example.org", "other@example.org"),
                          ("other@example.org", "")])
        self.assertEqual({call["shop"].pk for call in heard}, {self.shop.pk})

    def test_a_new_shop_is_no_change_and_a_rollback_says_nothing(self):
        with Heard(events.lightning_address_changed) as heard, self.captureOnCommitCallbacks(execute=True):
            Shop.objects.create(name="New", lightning_address="new@example.org")
            try:
                with transaction.atomic():
                    Shop.objects.filter(pk=self.shop.pk).update(lightning_address="x@example.org")
                    raise ValueError
            except ValueError:
                pass
        self.assertEqual(heard, [])

    def test_a_receiver_that_raises_does_not_stop_the_change(self):
        with Heard(events.lightning_address_changed, raising=True), self.assertLogs("shops.events", "ERROR"), \
                self.captureOnCommitCallbacks(execute=True):
            Shop.objects.filter(pk=self.shop.pk).update(lightning_address="new@example.org")
        self.assertEqual(Shop.objects.get(pk=self.shop.pk).lightning_address, "new@example.org")


BROWSER = {"Sec-Fetch-Mode": "navigate", "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Firefox/140.0"}


class ThemeFileServed(TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        theme = Path(folder.name) / "theme"
        (theme / "media").mkdir(parents=True)
        (theme / "media" / "book.zip").write_bytes(bytes(range(100)))
        (theme / "style.css").write_text("a{}")
        patcher = patch("website.views.COMMUNITY_DIR", Path(folder.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def listed(self, patterns=("media/*.zip",)):
        return patch.dict(community(), {"announced_files": list(patterns)})


    def test_only_for_the_listed_files(self):
        with self.listed(), Heard(events.theme_file_served) as heard:
            self.assertEqual(self.client.get("/theme/media/book.zip", headers=BROWSER).status_code, 200)
            self.assertEqual(self.client.get("/theme/style.css", headers=BROWSER).status_code, 200)
            self.assertEqual(self.client.get("/theme/media/none.zip").status_code, 404)
        self.assertEqual(heard, [{"sender": None, "path": "media/book.zip", "first_byte": True, "client_kind": "browser"}])

    def test_never_without_the_key(self):
        self.assertNotIn("announced_files", community())
        with Heard(events.theme_file_served) as heard:
            response = self.client.get("/theme/media/book.zip")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(heard, [])

    def test_first_byte_with_and_without_range(self):
        with self.listed(), Heard(events.theme_file_served) as heard:
            self.assertEqual(self.client.get("/theme/media/book.zip", headers={"Range": "bytes=0-9"}).status_code, 206)
            self.assertEqual(self.client.get("/theme/media/book.zip", headers={"Range": "bytes=10-19"}).status_code, 206)
            self.assertEqual(self.client.get("/theme/media/book.zip", headers={"Range": "bytes=500-"}).status_code, 416)
        self.assertEqual([call["first_byte"] for call in heard], [True, False])

    def test_the_path_is_the_one_inside_the_theme_however_written(self):
        with self.listed(), Heard(events.theme_file_served) as heard:
            self.client.get("/theme//media/./book.zip")
        self.assertEqual([call["path"] for call in heard], ["media/book.zip"])

    def test_the_two_kinds_of_client(self):
        with self.listed(), Heard(events.theme_file_served) as heard:
            self.client.get("/theme/media/book.zip", headers=BROWSER)
            self.client.get("/theme/media/book.zip", headers={"User-Agent": "curl/8.5.0"})
            self.client.get("/theme/media/book.zip", headers={"Sec-Fetch-Mode": "navigate", "User-Agent": "Googlebot/2.1"})
        self.assertEqual([call["client_kind"] for call in heard], ["browser", "program", "program"])
        # Nothing of the request goes to the receivers
        self.assertEqual({tuple(sorted(call)) for call in heard}, {("client_kind", "first_byte", "path", "sender")})

    def test_a_head_or_an_unchanged_file_sends_no_file_and_says_nothing(self):
        with self.listed(), Heard(events.theme_file_served) as heard:
            modified = self.client.get("/theme/media/book.zip")["Last-Modified"]
            self.assertEqual(self.client.get("/theme/media/book.zip", headers={"If-Modified-Since": modified}).status_code,
                             304)
            self.client.head("/theme/media/book.zip")
        self.assertEqual(len(heard), 1)

    def test_a_receiver_that_raises_does_not_stop_the_file(self):
        with self.listed(), Heard(events.theme_file_served, raising=True), self.assertLogs("shops.events", "ERROR"):
            response = self.client.get("/theme/media/book.zip")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), bytes(range(100)))


class DocumentedList(TestCase):
    """docs/architecture.md, "The community's own code", is the one list: every event of shops/events.py is in it, every
    event and every template block it names exists. The image has no docs/: then there is nothing to check."""

    def section(self, text, title):
        part = text.split(f"\n### {title}\n", 1)[1]
        return part.split("\n### ", 1)[0]

    def test_the_list_and_the_software_agree(self):
        from django.conf import settings

        for signal, name in events.NAMES.items():
            self.assertIs(getattr(events, name), signal)
        document = Path(settings.BASE_DIR) / "docs" / "architecture.md"
        if not document.exists():
            return  # the image leaves docs/ out (.dockerignore): there only the names are checked
        text = document.read_text()
        listed = [line.split("`")[1] for line in self.section(text, "Events").splitlines() if line.startswith("| `")]
        self.assertEqual(sorted(listed), sorted(events.NAMES.values()))
        for name in listed:
            self.assertEqual(events.NAMES[getattr(events, name)], name)
        rows = [line for line in self.section(text, "Template blocks").splitlines() if line.startswith("| `")]
        self.assertTrue(rows)
        for row in rows:
            cells = row.split("|")
            template = cells[1].split("`")[1]
            source = (Path(settings.BASE_DIR) / "templates" / template).read_text()
            blocks = cells[2].split("`")[1::2]
            self.assertTrue(blocks, row)
            for block in blocks:
                self.assertIn(f"{{% block {block} %}}", source, f"{template}: {block}")


class AnnouncedFilesCheck(TestCase):
    """`announced_files` is read like the other keys of community.yml: what cannot be used is left out, and said."""

    def warnings(self, value):
        from django.core import checks

        with patch.dict(community(), {"announced_files": value}):
            patterns = events.announced_patterns()[0]
            found = [warning for warning in checks.run_checks() if warning.id == "shops.W014"]
        return patterns, [warning.msg for warning in found]

    def test_without_the_key_nothing(self):
        self.assertNotIn("announced_files", community())
        self.assertEqual(events.announced_patterns(), ([], []))

    def test_a_good_list_says_nothing(self):
        self.assertEqual(self.warnings(["media/*.zip", " books/*.epub "]), (["media/*.zip", "books/*.epub"], []))

    def test_a_text_instead_of_a_list(self):
        patterns, warnings = self.warnings("media/*.zip")
        self.assertEqual(patterns, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("`announced_files`", warnings[0])

    def test_entries_that_are_not_patterns_or_too_long_are_left_out(self):
        patterns, warnings = self.warnings(["media/*.zip", 3, "", "x" * (events.PATTERN_LENGTH + 1)])
        self.assertEqual(patterns, ["media/*.zip"])
        self.assertEqual(len(warnings), 3)
        self.assertTrue(all("`announced_files`: entry" in warning for warning in warnings))

    def test_too_many_patterns(self):
        patterns, warnings = self.warnings([f"m/{n}.zip" for n in range(events.MAX_ANNOUNCED + 5)])
        self.assertEqual(len(patterns), events.MAX_ANNOUNCED)
        self.assertEqual(len(warnings), 1)

    def test_a_wrong_value_announces_nothing_and_breaks_nothing(self):
        with patch.dict(community(), {"announced_files": "media/*"}), Heard(events.theme_file_served) as heard:
            self.assertEqual(self.client.get("/theme/logo.svg").status_code, 200)
        self.assertEqual(heard, [])

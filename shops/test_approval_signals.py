"""Risk signals when a new shop is approved (shops/approval_signals.py): a copy of a shop already on the site is evident on
/admin/today/ and in the moderators' message, and is approved only by a person who ticks that they checked."""

import datetime
from unittest.mock import patch

from django.conf import settings
from django.contrib.admin.models import LogEntry
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.utils import timezone, translation
from django.utils.translation import gettext as _

from . import approval, approval_signals
from .approval_signals import (
    NAMESAKE,
    NAMESAKE_NOTE,
    NEAR,
    NEW_OWNER,
    OTHER_LIGHTNING,
    SAME_CONTACT,
    asks,
    codes,
    same_phone,
    signals,
    website_key,
)
from .models import Profile, Shop
from .test_approval import approval_off

User = get_user_model()
OLD = timezone.now() - datetime.timedelta(days=400)


def owner(name, new=True, email=""):
    user = User.objects.create(
        username=name, date_joined=timezone.now() - datetime.timedelta(minutes=2) if new else OLD
    )
    if email:
        Profile.objects.update_or_create(user=user, defaults={"email": email})
    return user


def shop(name, by=None, waiting=False, **fields):
    return Shop.objects.create(name=name, owner=by, approved_on=None if waiting else timezone.now(), **fields)


class Staff(TestCase):
    def sign_in(self, *codes):
        from config.urls import ADMIN_SESSION

        self.staff = User.objects.create(username="staff", is_staff=True)
        self.staff.user_permissions.add(*Permission.objects.filter(content_type__app_label="shops", codename__in=codes))
        self.client.force_login(self.staff)
        session = self.client.session
        session[ADMIN_SESSION] = True
        session.save()


class TheRealCase(Staff):
    """A fake shop: the same name and website as an existing one, two house numbers away, its owner's account two minutes
    old with no email and no other shop, a Lightning address at another provider."""

    def setUp(self):
        self.real = shop(
            "Caffè Roma",
            owner("real", new=False, email="real@mail.example"),
            city="Torino",
            latitude=45.07,
            longitude=7.68,
            website="https://www.cafferoma.example/",
            phone="+39 011 1234567",
            lightning_address="real@walleta.example",
        )
        self.fake = shop(
            "Caffe Roma",
            owner("fake"),
            waiting=True,
            city="torino",
            latitude=45.0703,
            longitude=7.6803,
            website="http://cafferoma.example",
            lightning_address="fake@walletb.example",
        )
        self.sign_in("view_shop", "change_shop")

    def test_all_five(self):
        found = signals(self.fake)
        self.assertEqual(codes(found), [NAMESAKE, NEAR, SAME_CONTACT, OTHER_LIGHTNING, NEW_OWNER])
        self.assertEqual([signal.asks for signal in found], [True, False, True, False, False])
        text = " ".join(signal.text for signal in found)
        self.assertIn("Caffè Roma", text)
        self.assertIn("at another provider", text)
        for secret in ("cafferoma", "walleta", "walletb", "1234567", "real@"):
            self.assertNotIn(secret, text)

    def test_the_page_shows_the_warning_and_the_checkbox(self):
        page = self.client.get("/admin/today/")
        self.assertContains(page, 'class="approval-check"')
        self.assertContains(page, "dashboard-suspect")
        self.assertContains(page, "Check before approving")
        self.assertContains(page, 'name="checked" value="1" required')
        self.assertContains(page, "Approve anyway")
        self.assertContains(page, f'href="/admin/shops/shop/{self.real.pk}/change/"')
        for secret in ("cafferoma.example", "walleta", "walletb", "1234567"):
            self.assertNotContains(page, secret)

    def test_refused_without_the_checkbox(self):
        response = self.client.post(f"/admin/today/approve/{self.fake.pk}/", follow=True)
        self.fake.refresh_from_db()
        self.assertTrue(self.fake.needs_approval)
        self.assertContains(response, "is not approved: it looks like a shop already on the site")
        self.assertFalse(LogEntry.objects.exists())

    def test_approved_with_it_and_recorded(self):
        self.client.post(f"/admin/today/approve/{self.fake.pk}/", {"checked": "1"})
        self.fake.refresh_from_db()
        self.assertFalse(self.fake.needs_approval)
        self.assertEqual(
            LogEntry.objects.get().change_message,
            "Shop approved; signals: namesake, near, same_contact, other_lightning, new_owner",
        )

    def test_the_shop_page_in_the_admin_shows_them(self):
        self.assertContains(
            self.client.get(f"/admin/shops/shop/{self.fake.pk}/change/"), "a shop of another owner in the same area"
        )

    def test_the_message_to_the_moderators(self):
        with patch("shops.approval.threading.Thread") as thread, self.captureOnCommitCallbacks(execute=True):
            approval.tell_moderators(self.fake)
        with patch("shops.notifications.deliver_to_moderators", return_value=("x", [])) as deliver:
            thread.call_args.kwargs["target"](*thread.call_args.kwargs["args"])
        subject, text = deliver.call_args.args
        with translation.override(settings.LANGUAGE_CODE):
            heading = f"⚠ {_('Check before approving')}"
        lines = text.splitlines()
        self.assertEqual(lines[0], heading)
        self.assertTrue(subject.startswith(heading))
        self.assertEqual(len([line for line in lines if line.startswith("- ")]), 6)
        self.assertIn("Caffè Roma", text)
        for secret in ("cafferoma", "walleta", "walletb", "1234567", "@"):
            self.assertNotIn(secret, text)


class Quiet(Staff):
    def test_the_same_owners_second_shop_is_a_branch(self):
        mario = owner("mario")
        shop("Bar Mario", mario, city="Roma", website="https://bar.example")
        second = shop("Bar Mario", mario, waiting=True, city="Roma", website="https://bar.example")
        self.assertEqual(signals(second), [])

    def test_an_honest_new_owner_with_a_unique_shop(self):
        shop("Pizzeria Napoli", owner("other", new=False), city="Roma", latitude=41.9, longitude=12.5)
        new = shop(
            "Libreria Sole",
            owner("new"),
            waiting=True,
            city="Roma",
            latitude=41.9001,
            longitude=12.5001,
            website="https://sole.example",
            phone="06 1234567",
            lightning_address="me@wallet.example",
        )
        self.assertEqual(signals(new), [])
        self.sign_in("view_shop", "change_shop")
        page = self.client.get("/admin/today/")
        self.assertNotContains(page, "approval-")
        self.assertNotContains(page, "dashboard-suspect")
        self.assertContains(page, "Approve: on the map and the lists")
        self.client.post(f"/admin/today/approve/{new.pk}/")
        new.refresh_from_db()
        self.assertFalse(new.needs_approval)
        self.assertEqual(LogEntry.objects.get().change_message, "Shop approved")

    def test_a_chain_in_another_town_is_a_note(self):
        shop(
            "Burger Planet",
            owner("chain", new=False),
            city="Milano",
            latitude=45.46,
            longitude=9.19,
            website="https://burgerplanet-milano.example",
            lightning_address="milano@a.example",
        )
        new = shop(
            "Burger Planet",
            owner("branch"),
            waiting=True,
            city="Roma",
            latitude=41.9,
            longitude=12.5,
            website="https://burgerplanet-roma.example",
            lightning_address="roma@b.example",
        )
        found = signals(new)
        self.assertEqual(codes(found), [NAMESAKE_NOTE])
        self.assertFalse(any(signal.asks for signal in found))
        self.sign_in("view_shop", "change_shop")
        page = self.client.get("/admin/today/")
        self.assertContains(page, 'class="approval-note"')
        self.assertNotContains(page, 'name="checked"')
        self.assertEqual(approval.approve(Shop.objects.filter(pk=new.pk), self.staff), 1)


class Contacts(TestCase):
    def test_websites(self):
        self.assertEqual(website_key("https://www.Bar.example/"), website_key("http://bar.example/index.html"))
        self.assertEqual(website_key("https://www.facebook.com/BarA/"), website_key("facebook.com/bara"))
        self.assertNotEqual(website_key("https://facebook.com/bara"), website_key("https://facebook.com/barb"))
        self.assertEqual(website_key("https://www.facebook.com/"), "")
        self.assertEqual(website_key("https://linktr.ee"), "")

    def test_a_shared_host_with_different_paths_is_nothing_the_same_path_is_a_signal(self):
        shop("First", owner("a", new=False), website="https://www.facebook.com/uno.bar")
        waiting = shop("Second", owner("b"), waiting=True, website="https://facebook.com/due.bar")
        self.assertEqual(signals(waiting), [])
        same = shop("Third", owner("c"), waiting=True, website="https://m.facebook.com/Uno.Bar/")
        self.assertIn(SAME_CONTACT, codes(signals(same)))
        root = shop("Fourth", owner("d"), waiting=True, website="https://facebook.com/")
        shop("Fifth", owner("e", new=False), website="https://www.facebook.com")
        self.assertEqual(signals(root), [])

    def test_phones(self):
        self.assertTrue(same_phone("+39 06 1234 5678", "06 12345678"))
        self.assertTrue(same_phone("0039 06-1234-5678", "+39 (06) 12345678"))
        self.assertTrue(same_phone("+44 20 7946 0000", "020 7946 0000"))
        self.assertTrue(same_phone("333 123 4567", "3331234567"))
        self.assertFalse(same_phone("+39 612345678", "+33 612345678"))
        self.assertFalse(same_phone("12345", "12345"))
        self.assertFalse(same_phone("", ""))
        self.assertFalse(same_phone("06 1234567", "06 7654321"))

    def test_a_phone_or_an_email_names_only_the_kind(self):
        shop("Ferramenta", owner("a", new=False), phone="+39 06 1234 5678", email="Info@Ferr.example", show_phone=False)
        waiting = shop("Nuova", owner("b"), waiting=True, phone="06 12345678", email="info@ferr.example")
        found = signals(waiting)
        self.assertEqual(codes(found), [SAME_CONTACT, NEW_OWNER])
        self.assertIn("phone, email", found[0].text)
        self.assertNotIn("1234", found[0].text)
        self.assertNotIn("ferr", found[0].text)


class Others(TestCase):
    def test_an_unowned_imported_namesake(self):
        shop("Gelateria Luna", None, city="Bologna", external_id="7")
        waiting = shop("Gelateria Luna", owner("x", new=False, email="x@mail.example"), waiting=True, city="Bologna")
        self.assertEqual(codes(signals(waiting)), [NAMESAKE])

    def test_names_with_accents_and_punctuation(self):
        shop("Caffè-Roma!", owner("a", new=False), city="Torino")
        waiting = shop("caffe   roma", owner("b", new=False), waiting=True, city="TORINO ")
        found = signals(waiting)
        self.assertEqual(codes(found), [NAMESAKE])
        self.assertTrue(found[0].text.startswith("The same name as «Caffè-Roma!»"))

    def test_a_suspended_and_a_historic_shop_count(self):
        shop("Osteria", owner("a", new=False), city="Pisa", suspended=True)
        shop("Bottega", owner("b", new=False), city="Pisa", closed_on=datetime.date(2020, 1, 1))
        self.assertEqual(codes(signals(shop("Osteria", owner("c", new=False), waiting=True, city="Pisa"))), [NAMESAKE])
        self.assertEqual(codes(signals(shop("Bottega", owner("d", new=False), waiting=True, city="Pisa"))), [NAMESAKE])

    def test_a_namesake_close_by_in_another_town_is_in_the_area(self):
        shop("Forno", owner("a", new=False), city="Sesto", latitude=45.0, longitude=9.0)
        waiting = shop("Forno", owner("b", new=False), waiting=True, city="Milano", latitude=45.01, longitude=9.0)
        self.assertEqual(codes(signals(waiting)), [NAMESAKE])

    def test_an_approved_shop_has_none(self):
        shop("Forno", owner("a", new=False), city="Pisa")
        self.assertEqual(signals(shop("Forno", owner("b"), city="Pisa")), [])


class Approve(Staff):
    def setUp(self):
        shop("Enoteca", owner("a", new=False), city="Siena")
        self.flagged = shop("Enoteca", owner("b"), waiting=True, city="Siena")
        self.clean = shop("Panificio", owner("c"), waiting=True, city="Siena")
        self.sign_in("view_shop", "change_shop")

    def test_by_default_a_flagged_shop_is_skipped(self):
        everything = Shop.objects.filter(pk__in=[self.flagged.pk, self.clean.pk])
        self.assertEqual(approval.approve(everything, self.staff), 1)
        self.assertEqual(approval.held(everything), [self.flagged])
        self.assertEqual(approval.approve(everything, self.staff, confirmed=True), 1)
        self.flagged.refresh_from_db()
        self.assertFalse(self.flagged.needs_approval)
        self.assertEqual(approval.held(everything), [])

    def test_the_admin_action_skips_and_says_so(self):
        response = self.client.post(
            "/admin/shops/shop/",
            {"action": "approve_shops", "_selected_action": [str(self.flagged.pk), str(self.clean.pk)]},
            follow=True,
        )
        self.flagged.refresh_from_db()
        self.clean.refresh_from_db()
        self.assertTrue(self.flagged.needs_approval)
        self.assertFalse(self.clean.needs_approval)
        self.assertContains(response, "Approved: 1 shops.")
        self.assertContains(response, "Not approved: 1 shops that look like a shop already on the site")

    def test_flagged_ones_come_first(self):
        page = self.client.get("/admin/today/").content.decode()
        self.assertLess(page.index(f"approve-{self.flagged.pk}"), page.index(f"approve-{self.clean.pk}"))

    def test_with_the_approval_off_nothing_changes(self):
        with approval_off():
            self.assertEqual(signals(self.flagged), [])
            self.assertNotContains(self.client.get("/admin/today/"), "approval-check")
            self.assertEqual(approval.approve(Shop.objects.filter(pk=self.flagged.pk), self.staff), 1)
        self.assertEqual(LogEntry.objects.get().change_message, "Shop approved")

    def test_the_message_without_signals_is_todays(self):
        with patch("shops.approval.threading.Thread") as thread, self.captureOnCommitCallbacks(execute=True):
            approval.tell_moderators(self.clean)
        with patch("shops.notifications.deliver_to_moderators", return_value=("x", [])) as deliver:
            thread.call_args.kwargs["target"](*thread.call_args.kwargs["args"])
        with patch("shops.notifications.deliver_to_moderators", return_value=("x", [])) as today:
            thread.call_args.kwargs["target"]()
        self.assertEqual(deliver.call_args, today.call_args)
        self.assertEqual(len(deliver.call_args.args[1].splitlines()), 3)


class FalsePositives(Staff):
    """In a big town, names alike kilometres apart are other shops: a note, never a box to tick."""

    PAIRS = (
        ("Pizzeria Roma", "Pizzeria Rosa"),
        ("Bar Mario", "Bar Maria"),
        ("Ristorante Da Gino", "Ristorante Da Pino"),
        ("Panificio Rossi", "Panificio Russo"),
        ("Bar Sport", "Bar Sport 2"),
        ("Osteria", "Osteria del Ponte"),
        ("Mario", "Bar Mario"),
    )

    def test_alike_names_far_apart_in_the_same_town_do_not_ask(self):
        for index, (first, second) in enumerate(self.PAIRS):
            with self.subTest(first=first, second=second):
                existing = shop(first, owner(f"a{index}", new=False), city="Roma", latitude=41.90, longitude=12.50)
                waiting = shop(
                    second,
                    owner(f"b{index}"),
                    waiting=True,
                    city="Roma",
                    latitude=41.91,
                    longitude=12.51,
                    lightning_address="x@other.example",
                )
                found = signals(waiting)
                self.assertFalse(asks(found), [signal.text for signal in found])
                self.assertEqual(codes(found), [NAMESAKE_NOTE])
                existing.delete()
                waiting.delete()

    def test_alike_next_door_asks(self):
        shop("Bar Mario", owner("a", new=False), city="Roma", latitude=41.9, longitude=12.5)
        waiting = shop("Bar Maria", owner("b", new=False), waiting=True, city="Roma", latitude=41.9005, longitude=12.5)
        self.assertEqual(codes(signals(waiting)), [NAMESAKE, NEAR])
        self.assertTrue(asks(signals(waiting)))

    def test_alike_with_a_contact_asks(self):
        shop("Osteria", owner("a", new=False), city="Roma", latitude=41.90, longitude=12.50, phone="06 1234567")
        waiting = shop(
            "Osteria del Ponte",
            owner("b", new=False),
            waiting=True,
            city="Roma",
            latitude=41.91,
            longitude=12.51,
            phone="+39 06 1234567",
        )
        self.assertEqual(codes(signals(waiting)), [NAMESAKE, SAME_CONTACT])

    def test_for_names_only_alike_the_distance_decides_not_the_town(self):
        shop("Forno Sole", owner("a", new=False), city="Roma", latitude=41.90, longitude=12.50)
        waiting = shop("Forno Sale", owner("b", new=False), waiting=True, city="Roma", latitude=41.95, longitude=12.55)
        self.assertEqual(codes(signals(waiting)), [NAMESAKE_NOTE])


class Advice(TestCase):
    def test_an_unowned_shop_is_taken_instead(self):
        shop("Gelateria Luna", None, city="Bologna")
        found = signals(shop("Gelateria Luna", owner("x"), waiting=True, city="Bologna"))
        self.assertIn("without an owner", next(signal.advice for signal in found if signal.advice))
        self.assertNotIn("Ask the owner", next(signal.advice for signal in found if signal.advice))

    def test_a_historic_shop_may_be_reopening(self):
        shop("Bottega", owner("a", new=False), city="Pisa", closed_on=datetime.date(2020, 1, 1))
        found = signals(shop("Bottega", owner("b", new=False), waiting=True, city="Pisa"))
        self.assertIn("reopening", next(signal.advice for signal in found if signal.advice))

    def test_an_open_shop_with_an_owner(self):
        shop("Bottega", owner("a", new=False), city="Pisa")
        found = signals(shop("Bottega", owner("b", new=False), waiting=True, city="Pisa"))
        self.assertIn("Ask the owner of «Bottega»", next(signal.advice for signal in found if signal.advice))


class Websites(TestCase):
    def test_the_host_only_for_a_site_of_its_own(self):
        self.assertEqual(website_key("https://mario.example/Home"), "mario.example")
        self.assertEqual(website_key("mario.example/index.html"), website_key("http://www.mario.example"))
        self.assertNotEqual(website_key("https://shop.mario.example"), website_key("https://mario.example"))

    def test_unicode_and_punycode_are_one_host(self):
        self.assertEqual(website_key("https://caffè.example/"), website_key("http://xn--caff-8oa.example"))

    def test_an_address_that_cannot_be_read_has_no_key(self):
        self.assertEqual(website_key("mario[.it"), "")
        self.assertEqual(website_key("http://[::1"), "")
        shop("A", owner("a", new=False), website="mario[.it")
        self.assertEqual(signals(shop("B", owner("b"), waiting=True, website="mario[.it")), [])


class FailSafe(Staff):
    def setUp(self):
        shop("Enoteca", owner("a", new=False), city="Siena")
        self.flagged = shop("Enoteca", owner("b"), waiting=True, city="Siena")
        self.sign_in("view_shop", "change_shop")

    def broken(self):
        return patch("shops.approval_signals.others", side_effect=RuntimeError("broken"))

    def test_the_day_page_says_the_checks_could_not_be_run_and_lets_a_person_approve(self):
        with self.broken(), self.assertLogs("shops.admin", "ERROR"):
            page = self.client.get("/admin/today/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "The checks could not be run")
        self.assertContains(page, "The checks could not be run: I looked at this shop myself.")
        self.assertContains(page, "Approve anyway")
        self.assertNotContains(page, "Approve: on the map and the lists")
        with self.broken(), self.assertLogs("shops.approval", "ERROR"):
            self.client.post(f"/admin/today/approve/{self.flagged.pk}/", {"checked": "1"})
        self.flagged.refresh_from_db()
        self.assertFalse(self.flagged.needs_approval)
        self.assertEqual(LogEntry.objects.get().change_message, "Shop approved; the checks could not be run")

    def test_the_messages_say_the_checks_could_not_be_run(self):
        with self.broken(), self.assertLogs("shops.approval", "ERROR"):
            response = self.client.post(f"/admin/today/approve/{self.flagged.pk}/", follow=True)
        self.assertContains(response, "the checks for copies of other shops could not be run")
        with self.broken(), self.assertLogs("shops.approval", "ERROR"):
            response = self.client.post(
                "/admin/shops/shop/",
                {"action": "approve_shops", "_selected_action": [str(self.flagged.pk)]},
                follow=True,
            )
        self.assertContains(response, "because the checks for copies of other shops could not be run")
        self.assertNotContains(response, "look like a shop already on the site")

    def test_approve_fails_closed(self):
        with self.broken(), self.assertLogs("shops.approval", "ERROR"):
            self.assertEqual(approval.approve(Shop.objects.filter(pk=self.flagged.pk), self.staff), 0)
        self.flagged.refresh_from_db()
        self.assertTrue(self.flagged.needs_approval)
        with self.broken(), self.assertLogs("shops.approval", "ERROR"):
            self.client.post(f"/admin/today/approve/{self.flagged.pk}/")
        self.flagged.refresh_from_db()
        self.assertTrue(self.flagged.needs_approval)

    def test_confirmed_approves_as_before(self):
        with self.broken(), self.assertLogs("shops.approval", "ERROR"):
            self.assertEqual(approval.approve(Shop.objects.filter(pk=self.flagged.pk), self.staff, confirmed=True), 1)

    def test_a_held_shop_leaves_a_warning_with_no_name(self):
        with self.assertLogs("shops.approval", "WARNING") as logs:
            approval.approve(Shop.objects.filter(pk=self.flagged.pk), self.staff)
        self.assertIn("1 shops held", logs.output[0])
        self.assertNotIn("Enoteca", logs.output[0])


class EditAfterApproval(TestCase):
    """An owner approved with an innocent name who then takes the name and the website of a real shop."""

    def setUp(self):
        from django.core.cache import cache

        cache.clear()
        self.real = shop("Caffè Roma", owner("real", new=False), city="Torino", website="https://cafferoma.example")
        self.owner = owner("fake")
        self.mine = shop("Bar Nuovo", self.owner, city="Torino", latitude=45.07, longitude=7.68)
        self.client.force_login(self.owner)

    def edit(self, **changes):
        url = f"/en/shops/edit/{self.mine.pk}/"
        data = {
            name: value for name, value in self.client.get(url).context["form"].initial.items() if value is not None
        }
        data.update(changes)
        with (
            patch("shops.approval.threading.Thread") as thread,
            patch("shops.approval_signals.others", wraps=approval_signals.others) as computed,
            self.captureOnCommitCallbacks(execute=True),
        ):
            response = self.client.post(url, data)
        self.assertEqual(response.status_code, 302)
        return thread, computed

    def test_the_bypass_is_told_to_the_moderators(self):
        thread, _computed = self.edit(name="Caffè Roma", website="https://www.cafferoma.example/")
        with patch("shops.notifications.deliver_to_moderators", return_value=("x", [])) as deliver:
            thread.call_args.kwargs["target"](*thread.call_args.kwargs["args"])
        text = deliver.call_args.args[1]
        with translation.override(settings.LANGUAGE_CODE):
            heading = f"⚠ {_('Check this shop')}"
        self.assertEqual(text.splitlines()[0], heading)
        self.assertIn(f"/admin/shops/shop/{self.mine.pk}/change/", text)
        self.assertNotIn("cafferoma", text)
        self.mine.refresh_from_db()
        self.assertFalse(self.mine.needs_approval)

    def test_an_edit_of_nothing_watched_computes_nothing(self):
        thread, computed = self.edit(description="Coffee and cakes")
        computed.assert_not_called()
        thread.assert_not_called()

    def test_a_shop_already_flagged_sends_nothing_new(self):
        Shop.objects.filter(pk=self.mine.pk).update(name="Caffè Roma")
        self.mine.refresh_from_db()
        thread, computed = self.edit(city="torino")
        computed.assert_called()
        thread.assert_not_called()

    def test_going_back_and_forth_sends_one_message_a_day(self):
        thread, _computed = self.edit(name="Caffè Roma")
        self.assertEqual(thread.call_count, 1)
        self.mine.refresh_from_db()
        thread, _computed = self.edit(name="Bar Nuovo")
        thread.assert_not_called()
        self.mine.refresh_from_db()
        thread, _computed = self.edit(name="Caffè Roma")
        thread.assert_not_called()

    def test_another_shop_copied_the_same_day_is_told(self):
        shop("Pasticceria Dolce", owner("sweet", new=False), city="Torino")
        thread, _computed = self.edit(name="Caffè Roma")
        self.assertEqual(thread.call_count, 1)
        self.mine.refresh_from_db()
        thread, _computed = self.edit(name="Pasticceria Dolce")
        self.assertEqual(thread.call_count, 1)
        self.assertIn("Pasticceria Dolce", "\n".join(thread.call_args.kwargs["args"][1]))

    def test_an_edit_that_reorders_the_other_shops_tells_nothing(self):
        for index in range(4):
            shop(
                "Caffè Roma",
                owner(f"r{index}", new=False),
                city="Torino",
                latitude=45.07 + index / 1000,
                longitude=7.69,
            )
        Shop.objects.filter(pk=self.mine.pk).update(name="Caffè Roma")
        self.mine.refresh_from_db()
        # Moving the pin puts other namesakes among the first three: none is new
        thread, _computed = self.edit(latitude="45.0731", longitude="7.6901")
        thread.assert_not_called()


class SameNameInTown(TestCase):
    def test_the_same_name_in_the_same_town_asks_whatever_the_pins(self):
        shop("Bar Mario", owner("a", new=False), city="Roma", latitude=41.90, longitude=12.50)
        waiting = shop("Bar Mario", owner("b", new=False), waiting=True, city="Roma", latitude=41.919, longitude=12.50)
        self.assertEqual(codes(signals(waiting)), [NAMESAKE])
        self.assertTrue(asks(signals(waiting)))


class SharedHosts(TestCase):
    def key(self, url):
        return website_key(url)

    def test_google_in_every_country(self):
        self.assertNotEqual(
            self.key("https://google.it/maps/place/bar-uno"), self.key("https://google.it/maps/place/bar-due")
        )
        self.assertEqual(
            self.key("https://www.google.it/maps/place/Bar-Uno/"), self.key("google.it/maps/place/bar-uno")
        )
        self.assertEqual(self.key("https://maps.google.co.uk"), "")
        self.assertNotEqual(self.key("https://g.co/kgs/abc"), self.key("https://g.co/kgs/xyz"))
        self.assertNotEqual(self.key("https://maps.apple.com/?q=uno"), self.key("https://maps.apple.com/?q=due"))

    def test_short_links(self):
        for host in ("bit.ly", "tinyurl.com", "amzn.to", "t.co", "is.gd", "rb.gy", "cutt.ly"):
            with self.subTest(host=host):
                self.assertNotEqual(self.key(f"https://{host}/Ab1"), self.key(f"https://{host}/aB1"))
                self.assertEqual(self.key(f"https://{host}/Ab1"), self.key(f"http://{host}/Ab1/"))
                self.assertEqual(self.key(f"https://{host}"), "")

    def test_directories_and_delivery(self):
        for host in (
            "tripadvisor.co.uk",
            "tripadvisor.fr",
            "booking.com",
            "thefork.it",
            "ubereats.com",
            "deliveroo.de",
            "glovoapp.com",
            "justeat.ch",
            "paginegialle.it",
            "yelp.it",
        ):
            with self.subTest(host=host):
                self.assertNotEqual(self.key(f"https://{host}/bar-uno"), self.key(f"https://{host}/bar-due"))
                self.assertEqual(self.key(f"https://www.{host}/Bar-Uno"), self.key(f"https://{host}/bar-uno/"))

    def test_site_builders(self):
        for host in (
            "wordpress.com",
            "blogspot.com",
            "myshopify.com",
            "webnode.it",
            "jimdofree.com",
            "jimdosite.com",
            "altervista.org",
            "weebly.com",
            "squarespace.com",
            "business.site",
            "wixsite.com",
            "canva.site",
            "github.io",
            "netlify.app",
            "vercel.app",
            "pages.dev",
        ):
            with self.subTest(host=host):
                self.assertEqual(self.key(f"https://bar.{host}"), self.key(f"https://bar.{host}/menu"))
                self.assertEqual(self.key(f"https://bar.{host}"), f"bar.{host}")
                self.assertNotEqual(self.key(f"https://bar.{host}"), self.key(f"https://pub.{host}"))
                self.assertNotEqual(self.key(f"https://{host}/uno"), self.key(f"https://{host}/due"))
                self.assertEqual(self.key(f"https://{host}/"), "")

    def test_two_shops_on_one_short_link_match(self):
        shop("Uno", owner("a", new=False), website="https://bit.ly/Ab1")
        self.assertEqual(
            codes(signals(shop("Due", owner("b", new=False), waiting=True, website="https://bit.ly/Ab1"))),
            [SAME_CONTACT],
        )
        self.assertEqual(signals(shop("Tre", owner("c", new=False), waiting=True, website="https://bit.ly/Zz9")), [])


class MoreAdvice(TestCase):
    def test_a_moved_shop_waiting_for_its_claim_link(self):
        Shop.objects.filter(pk=shop("Gelateria Luna", None, city="Bologna").pk).update(claim_hash="0" * 64)
        found = signals(shop("Gelateria Luna", owner("x", new=False), waiting=True, city="Bologna"))
        self.assertIn("claim link", next(signal.advice for signal in found if signal.advice))

    def test_an_imported_shop_is_given_by_the_staff(self):
        shop("Gelateria Luna", None, city="Bologna")
        found = signals(shop("Gelateria Luna", owner("x", new=False), waiting=True, city="Bologna"))
        self.assertIn("give that shop to this owner", next(signal.advice for signal in found if signal.advice))

    def test_a_shop_that_closes_later_may_have_a_successor(self):
        shop(
            "Bottega", owner("a", new=False), city="Pisa", closed_on=timezone.localdate() + datetime.timedelta(days=30)
        )
        found = signals(shop("Bottega", owner("b", new=False), waiting=True, city="Pisa"))
        advice = next(signal.advice for signal in found if signal.advice)
        self.assertIn("successor", advice)
        self.assertNotIn("reopening", advice)

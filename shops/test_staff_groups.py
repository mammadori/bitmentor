"""The staff groups of community.yml (shops/staff_groups.py): made and kept as written, never with the rights to change
shops, owners, accounts, keys or addresses."""

import logging
from io import StringIO
from unittest.mock import patch

from django.contrib.auth.models import Group
from django.core import checks
from django.core.management import CommandError, call_command
from django.test import TestCase

from config.community import community

from . import moderators, staff_groups

MEETUPS = set(staff_groups.BUNDLES["meetups"]["rights"])
MODERATION = staff_groups.bundle_rights("moderation")


def with_groups(groups):
    return patch("shops.staff_groups.community", new=lambda: {**community(), "staff_groups": groups})


def rights_of(name):
    return set(Group.objects.get(name=name).permissions.values_list("content_type__app_label", "codename"))


class Configured(TestCase):
    def test_without_the_key_the_moderators_group_as_before(self):
        groups, problems = staff_groups.configured()
        self.assertEqual((groups, problems), ({moderators.GROUP: MODERATION}, []))

    def test_bundles_and_single_rights_make_the_groups(self):
        with with_groups({"Curators": ["moderation", "meetups"], "Readers": ["meetups.view_meetup"]}):
            staff_groups.sync()
        self.assertEqual(rights_of("Curators"), MODERATION | MEETUPS)
        self.assertEqual(rights_of("Readers"), {("meetups", "view_meetup")})

    def test_a_right_outside_the_bundles_is_never_given(self):
        dangerous = ["shops.change_shop", "auth.change_user", "auth.add_group", "shops.view_profile", "nonsense", 3]
        with with_groups({"Curators": ["meetups", *dangerous]}):
            staff_groups.sync()
            problems = staff_groups.problems()
        self.assertEqual(rights_of("Curators"), MEETUPS)
        self.assertEqual(len(problems), len(dangerous))
        self.assertTrue(all("skipped" in problem for problem in problems))

    def test_no_bundle_holds_a_right_to_change_a_shop_an_owner_an_account_a_key_or_an_address(self):
        for label, codename in staff_groups.in_bundles():
            self.assertNotIn(label, {"auth", "admin", "sessions", "contenttypes"}, codename)
            if codename.split("_", 1)[0] in {"add", "change", "delete"}:
                self.assertNotIn(
                    codename.split("_", 1)[1], {"shop", "profile", "user", "group", "permission", "logindevice"}
                )

    def test_rights_given_by_hand_are_taken_away_and_the_log_says_so(self):
        with with_groups({"Curators": ["meetups"]}):
            staff_groups.sync()
            group = Group.objects.get(name="Curators")
            group.permissions.add(*Group.objects.get(name=moderators.GROUP).permissions.all())
            with self.assertLogs("shops.staff_groups", logging.ERROR):
                staff_groups.sync()
        self.assertEqual(rights_of("Curators"), MEETUPS)

    def test_a_group_no_longer_named_is_left_as_it_is(self):
        before = rights_of(moderators.GROUP)
        with with_groups({"Curators": ["moderation"]}):
            staff_groups.sync()
            call_command("migrate", verbosity=0, interactive=False)
        self.assertEqual(rights_of(moderators.GROUP), before)
        self.assertEqual(rights_of("Curators"), MODERATION)

    def test_migrate_keeps_the_groups_of_community_yml(self):
        with with_groups({"Curators": ["meetups"]}):
            call_command("migrate", verbosity=0, interactive=False)
        self.assertEqual(rights_of("Curators"), MEETUPS)

    def test_a_wrong_block_falls_back_to_the_moderators_and_check_says_so(self):
        for wrong in ("Curators", ["meetups"], {}):
            with self.subTest(wrong=wrong), with_groups(wrong):
                found = [check for check in checks.run_checks() if check.id == "shops.W017"]
                self.assertTrue(found)
                self.assertEqual(staff_groups.configured()[0], {moderators.GROUP: MODERATION})

    def test_a_wrong_name_is_skipped_and_a_group_that_is_not_a_list_gets_no_right(self):
        with with_groups({"": ["meetups"], "x" * 151: ["meetups"], "Cura\x00tors": ["meetups"], "Curators": "meetups"}):
            groups, problems = staff_groups.configured()
        self.assertEqual(groups, {"Curators": set()})
        self.assertEqual(len(problems), 4)

    def test_a_group_left_by_hand_with_more_is_emptied_when_its_entry_is_wrong(self):
        with with_groups({"Curators": ["meetups"]}):
            staff_groups.sync()
        with with_groups({"Curators": "meetups"}), self.assertLogs("shops.staff_groups", logging.ERROR):
            staff_groups.sync()
        self.assertEqual(rights_of("Curators"), set())

    def test_one_group_that_fails_does_not_stop_the_others(self):
        real = staff_groups.sync_group

        def broken(name, *args):
            if name == "A":
                raise RuntimeError("cannot save")
            return real(name, *args)

        with (
            with_groups({"A": ["meetups"], "B": ["meetups"]}),
            patch.object(staff_groups, "sync_group", side_effect=broken),
            self.assertLogs("shops.staff_groups", logging.ERROR),
        ):
            staff_groups.sync()
        self.assertEqual(rights_of("B"), MEETUPS)

    def test_a_redefined_moderators_group_is_kept_as_written_by_the_command_too(self):
        with with_groups({moderators.GROUP: ["shops.view_helprequest", "meetups"]}):
            call_command("moderators", "sync", stdout=StringIO())
            self.assertEqual(rights_of(moderators.GROUP), {("shops", "view_helprequest")} | MEETUPS)
            from django.contrib.auth import get_user_model

            get_user_model().objects.create(username="vol", is_active=True)
            call_command("moderators", "add", "vol", stdout=StringIO())
            self.assertEqual(rights_of(moderators.GROUP), {("shops", "view_helprequest")} | MEETUPS)

    def test_the_moderators_command_does_not_bring_back_a_group_no_longer_named(self):
        Group.objects.filter(name=moderators.GROUP).delete()
        with with_groups({"Curators": ["meetups"]}):
            call_command("moderators", "sync", stdout=StringIO())
        self.assertFalse(Group.objects.filter(name=moderators.GROUP).exists())
        self.assertEqual(rights_of("Curators"), MEETUPS)

    def test_the_moderators_command_adds_nobody_to_a_group_no_longer_kept(self):
        from django.contrib.auth import get_user_model

        get_user_model().objects.create(username="anna", is_active=True)
        with with_groups({"Curators": ["meetups"]}), self.assertRaises(CommandError):
            call_command("moderators", "add", "anna", stdout=StringIO())
        self.assertFalse(get_user_model().objects.get(username="anna").groups.exists())

    def test_names_that_differ_only_in_spaces_are_one_group(self):
        with with_groups({"Curators": ["meetups.view_meetup"], " Curators ": ["meetups.view_reading"]}):
            staff_groups.sync()
        self.assertEqual(rights_of("Curators"), {("meetups", "view_meetup"), ("meetups", "view_reading")})

"""The moderators' group (shops/moderators.py) and `manage.py moderators`: the exact rights, on a new database and on one
brought up to date, what a moderator can and cannot do in /admin/, and the command with the register of the changes to the
owners' accounts."""
import logging
from io import StringIO
from unittest.mock import patch

from django.apps import apps as global_apps
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core import mail
from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import moderators
from .models import AccountChange, Closure, HelpRequest, Profile, Shop
from .test_dashboard import nobody_outside, register
from .test_guard import Base, sign_in_as_staff_session

User = get_user_model()

# Written out here, apart from the list in the code: a right added to the group by mistake fails this test
EXPECTED = {
    "shops.view_shop", "shops.view_helprequest", "shops.change_helprequest", "shops.view_accountchange",
}


def rights_of(group):
    return {f"{app}.{code}" for app, code in group.permissions.values_list("content_type__app_label", "codename")}


def the_group():
    return Group.objects.get(name="Moderators")


def run(*args):
    """The command's text; its error, when it refuses, comes out as CommandError."""
    out = StringIO()
    call_command("moderators", *args, stdout=out)
    return out.getvalue()


class Without:
    """The registry of the apps as `migrate` leaves it, without the apps of `labels`: an installation that does not have them."""

    def __init__(self, *labels):
        self.labels = labels

    def get_app_config(self, label):
        if label in self.labels:
            raise LookupError(label)
        return global_apps.get_app_config(label)

    def get_model(self, *args):
        return global_apps.get_model(*args)


# ---------------------------------------------------------------------------------------------------------------------
# The group
# ---------------------------------------------------------------------------------------------------------------------

class TheGroup(TestCase):
    def test_a_new_database_has_the_group_with_exactly_these_rights(self):
        """The test database is made by `migrate` from nothing: this is what a new installation starts with."""
        self.assertEqual(Group.objects.filter(name="Moderators").count(), 1)
        self.assertEqual(rights_of(the_group()), EXPECTED)
        self.assertEqual({f"{label}.{code}" for label, code, _ in moderators.PERMISSIONS}, EXPECTED)

    def test_every_right_of_the_list_exists(self):
        for label, code, what in moderators.PERMISSIONS:
            self.assertTrue(Permission.objects.filter(content_type__app_label=label, codename=code).exists(), code)
            self.assertTrue(what)

    def test_nothing_that_changes_a_shop_an_owner_an_account_a_key_or_an_address(self):
        """Not even by a right that looks harmless: only the changes the work asks for, and never an add or a delete."""
        rights = rights_of(the_group())
        self.assertEqual({right for right in rights if right.split(".")[1].startswith(("add_", "delete_"))}, set())
        self.assertEqual({right for right in rights if ".change_" in right}, {"shops.change_helprequest"})
        for model in ("shop", "user", "group", "permission", "profile", "closure", "accountchange", "drop"):
            self.assertFalse([right for right in rights if right.endswith(f"_{model}") and ".view_" not in right], model)
        self.assertEqual({right for right in rights if right.endswith(("_user", "_group", "_permission", "_drop"))}, set())

    def test_a_database_that_is_brought_up_to_date_gets_the_group(self):
        """A site that runs since before the group: its next `migrate` makes it, though there is nothing to migrate."""
        Group.objects.all().delete()
        call_command("migrate", verbosity=0, interactive=False)
        self.assertEqual(rights_of(the_group()), EXPECTED)

    def test_migrate_takes_away_a_right_given_by_hand_and_gives_back_one_taken(self):
        group = the_group()
        group.permissions.add(Permission.objects.get(codename="change_shop"), Permission.objects.get(codename="change_user"))
        group.permissions.remove(Permission.objects.get(codename="view_accountchange"))
        call_command("migrate", verbosity=0, interactive=False)
        self.assertEqual(rights_of(the_group()), EXPECTED)

    def test_what_is_taken_away_reaches_the_log_the_site_really_writes(self):
        """Not assertLogs, which changes the levels it checks: the records that the site's own configuration (settings.LOGGING,
        everything below ERROR dropped) hands to a handler. The first version logged a warning, which nobody ever saw."""
        group = the_group()
        group.permissions.add(Permission.objects.get(codename="change_shop"), Permission.objects.get(codename="change_user"))
        handed = []
        logger, handlers = logging.getLogger("shops.moderators"), []
        while logger:
            handlers += logger.handlers
            logger = logger.parent if logger.propagate else None
        self.assertTrue(handlers, "the configuration has a handler for this logger")
        patchers = [patch.object(handler, "handle", side_effect=lambda record: handed.append(record)) for handler in handlers]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        call_command("migrate", verbosity=0, interactive=False)
        self.assertEqual(len(handed), 1)
        self.assertIn("auth.change_user, shops.change_shop", handed[0].getMessage())
        self.assertEqual(rights_of(the_group()), EXPECTED)

    def test_a_group_that_already_has_the_name_is_put_right_too(self):
        Group.objects.all().delete()
        old = Group.objects.create(name="Moderators")
        old.permissions.add(Permission.objects.get(codename="delete_shop"))
        moderators.sync()
        self.assertEqual(Group.objects.filter(name="Moderators").count(), 1)
        self.assertEqual(rights_of(the_group()), EXPECTED)

    def test_the_members_stay_when_the_rights_are_put_right(self):
        member = User.objects.create_user("volunteer", is_staff=True)
        the_group().user_set.add(member)
        call_command("migrate", verbosity=0, interactive=False)
        self.assertEqual(list(the_group().user_set.all()), [member])

    def test_running_it_again_changes_nothing(self):
        before = (the_group().pk, rights_of(the_group()), Permission.objects.count())
        moderators.sync()
        moderators.sync()
        self.assertEqual((the_group().pk, rights_of(the_group()), Permission.objects.count()), before)
        self.assertEqual(Group.objects.filter(name="Moderators").count(), 1)

    def test_an_installation_without_an_app_gets_the_rights_of_the_others(self):
        Group.objects.all().delete()
        with_another_app = (*moderators.PERMISSIONS, ("meetups", "view_meetup", "A right of another app"))
        with patch.object(moderators, "PERMISSIONS", with_another_app):
            moderators.sync(apps=Without("meetups"))
            self.assertEqual(rights_of(the_group()), EXPECTED)
            # The app comes back with the next migrate on an installation that has it
            moderators.sync()
            self.assertEqual(rights_of(the_group()), EXPECTED | {"meetups.view_meetup"})

    def test_an_installation_with_only_the_accounts_still_makes_an_empty_group(self):
        Group.objects.all().delete()
        moderators.sync(apps=Without("shops"))
        self.assertEqual(rights_of(the_group()), set())

    def test_a_right_the_list_has_and_the_app_does_not_is_logged_and_does_not_stop_the_rest(self):
        Group.objects.all().delete()
        invented = (*moderators.PERMISSIONS, ("shops", "fly_shop", "A right that does not exist"))
        with patch.object(moderators, "PERMISSIONS", invented), self.assertLogs("shops.moderators", logging.ERROR) as logs:
            moderators.sync()
        self.assertIn("shops.fly_shop", logs.output[0])
        self.assertEqual(rights_of(the_group()), EXPECTED)

    def test_a_failure_while_migrating_is_logged_and_does_not_stop_the_start(self):
        with patch.object(moderators, "sync", side_effect=RuntimeError("no database")), \
                self.assertLogs("shops.moderators", logging.ERROR):
            moderators.on_post_migrate(sender=None)

    def test_migrate_runs_it_for_the_shops_app_only_and_once(self):
        with patch.object(moderators, "sync") as sync:
            call_command("migrate", verbosity=0, interactive=False)
        self.assertEqual(sync.call_count, 1)

    def test_the_difference_with_the_list(self):
        group = the_group()
        self.assertEqual(moderators.difference(group), (set(), set()))
        group.permissions.add(Permission.objects.get(codename="change_shop"))
        group.permissions.remove(Permission.objects.get(codename="view_accountchange"))
        self.assertEqual(moderators.difference(group), ({("shops", "view_accountchange")}, {("shops", "change_shop")}))


# ---------------------------------------------------------------------------------------------------------------------
# What a moderator can do in /admin/
# ---------------------------------------------------------------------------------------------------------------------

def make_moderator(client, name="volunteer"):
    """A staff account in the group, signed in as /admin/ signs it in, and nothing else."""
    user = User.objects.create_user(name, password="x", is_staff=True)
    user.groups.add(the_group())
    sign_in_as_staff_session(client, User.objects.get(pk=user.pk))
    return user


class InTheAdmin(Base):
    def setUp(self):
        super().setUp()
        self.moderator = make_moderator(self.client)
        self.owner = User.objects.create_user("merchant")
        self.shop = register("Bar")
        Shop.objects.filter(pk=self.shop.pk).update(owner=self.owner)

    def test_the_home_of_the_admin_offers_the_work_and_nothing_else(self):
        page = self.client.get("/admin/").content.decode()
        self.assertIn('href="/admin/today/"', page)
        for model in ("shops/shop", "shops/helprequest", "shops/accountchange"):
            self.assertIn(f'href="/admin/{model}/"', page, model)
        for model in ("auth/user", "auth/group", "shops/closure"):
            self.assertNotIn(f'href="/admin/{model}/"', page, model)

    def test_the_lists_open_and_the_accounts_and_the_groups_do_not(self):
        for name in ("shops_shop", "shops_helprequest", "shops_accountchange"):
            self.assertEqual(self.client.get(reverse(f"admin:{name}_changelist")).status_code, 200, name)
        for name in ("auth_user", "auth_group"):
            self.assertEqual(self.client.get(reverse(f"admin:{name}_changelist")).status_code, 403, name)
        self.assertEqual(self.client.get(reverse("admin:auth_user_change", args=[self.owner.pk])).status_code, 403)
        self.assertEqual(self.client.get(reverse("admin:auth_user_add")).status_code, 403)

    def test_today_opens_with_its_links(self):
        row = HelpRequest.objects.create(shop=self.shop, symptom="pay", channel="page")
        outside, sockets = nobody_outside()
        with outside, sockets:
            page = self.client.get("/admin/today/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, f'href="/admin/shops/shop/{self.shop.pk}/change/"')
        self.assertContains(page, f'href="/admin/shops/helprequest/{row.pk}/change/"')
        self.assertEqual(self.client.get(f"/admin/shops/shop/{self.shop.pk}/change/").status_code, 200)

    def test_the_link_to_a_request_is_not_shown_to_who_could_not_open_it(self):
        row = HelpRequest.objects.create(shop=self.shop, symptom="pay", channel="page")
        self.moderator.groups.clear()
        self.moderator.user_permissions.add(Permission.objects.get(codename="view_shop"))
        sign_in_as_staff_session(self.client, User.objects.get(pk=self.moderator.pk))
        outside, sockets = nobody_outside()
        with outside, sockets:
            page = self.client.get("/admin/today/")
        self.assertContains(page, f'href="/admin/shops/shop/{self.shop.pk}/change/"')
        self.assertNotContains(page, f"/admin/shops/helprequest/{row.pk}/change/")

    def test_a_shop_is_read_and_never_changed_or_added_or_deleted(self):
        url = reverse("admin:shops_shop_change", args=[self.shop.pk])
        page = self.client.get(url).content.decode()
        self.assertNotIn('name="name"', page)
        self.assertNotIn('name="_save"', page)
        self.assertNotIn("claim_hash", page)
        before = Shop.objects.values().get(pk=self.shop.pk)
        for post in ({"name": "Taken", "owner": self.moderator.pk, "suspended": "on", "_save": "Save"},
                     {"owner": self.moderator.pk, "telegram_chat": "999", "_save": "Save"}):
            self.assertEqual(self.client.post(url, post).status_code, 403)
        self.assertEqual(self.client.get(reverse("admin:shops_shop_add")).status_code, 403)
        self.assertEqual(self.client.get(reverse("admin:shops_shop_delete", args=[self.shop.pk])).status_code, 403)
        self.assertEqual(self.client.post(reverse("admin:shops_shop_delete", args=[self.shop.pk]), {"post": "yes"}).status_code, 403)
        # The list's own boxes (suspended, historic) are for who can change
        self.client.post(reverse("admin:shops_shop_changelist"), {
            "form-TOTAL_FORMS": 1, "form-INITIAL_FORMS": 1, "form-0-id": self.shop.pk, "form-0-suspended": "on",
            "form-0-closed_on": "2026-01-01", "_save": "Save"})
        self.assertEqual(Shop.objects.values().get(pk=self.shop.pk), before)

    def test_the_certificate_actions_need_the_right_to_change_a_shop(self):
        """They did not ask for it, so whoever could see the list could revoke a certificate."""
        Shop.objects.filter(pk=self.shop.pk).update(certified_month=timezone.localdate())
        url = reverse("admin:shops_shop_changelist")
        self.assertNotContains(self.client.get(url), "revoke_certificate")
        self.client.post(url, {"action": "revoke_certificate", "_selected_action": [self.shop.pk]})
        self.shop.refresh_from_db()
        self.assertEqual((self.shop.certified_month is not None, self.shop.certified_revoked), (True, False))
        # The ones who can change shops keep the actions
        self.moderator.user_permissions.add(Permission.objects.get(codename="change_shop"))
        sign_in_as_staff_session(self.client, User.objects.get(pk=self.moderator.pk))
        self.assertContains(self.client.get(url), "revoke_certificate")
        self.client.post(url, {"action": "revoke_certificate", "_selected_action": [self.shop.pk]})
        self.shop.refresh_from_db()
        self.assertEqual((self.shop.certified_month, self.shop.certified_revoked), (None, True))

    def test_closures_are_not_theirs_to_write(self):
        # The closures are written under a shop, and a shop is not theirs to change
        self.client.post(reverse("admin:shops_shop_change", args=[self.shop.pk]), {
            "closure_set-TOTAL_FORMS": 1, "closure_set-INITIAL_FORMS": 0, "closure_set-0-first_day": "2026-11-05",
            "_save": "Save"})
        self.assertFalse(Closure.objects.exists())

    def test_the_requests_for_help_are_read_and_marked(self):
        row = HelpRequest.objects.create(shop=self.shop, symptom="pay", channel="page")
        url = reverse("admin:shops_helprequest_changelist")
        self.assertContains(self.client.get(url), "Mark as solved")
        self.assertEqual(self.client.get(reverse("admin:shops_helprequest_change", args=[row.pk])).status_code, 200)
        self.client.post(url, {"action": "solve", "_selected_action": [row.pk]})
        row.refresh_from_db()
        self.assertEqual((row.resolved is not None, row.resolved_by), (True, "volunteer"))
        self.client.post(url, {"action": "reopen", "_selected_action": [row.pk]})
        row.refresh_from_db()
        self.assertIsNone(row.resolved)
        # Nothing is added or deleted
        self.assertEqual(self.client.get(reverse("admin:shops_helprequest_add")).status_code, 403)
        self.assertEqual(self.client.get(reverse("admin:shops_helprequest_delete", args=[row.pk])).status_code, 403)

    def test_the_register_of_the_changes_is_read_only(self):
        AccountChange.objects.create(shops="Bar", changed="groups", via="admin")
        row = AccountChange.objects.get()
        self.assertEqual(self.client.get(reverse("admin:shops_accountchange_changelist")).status_code, 200)
        self.assertEqual(self.client.get(reverse("admin:shops_accountchange_change", args=[row.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse("admin:shops_accountchange_delete", args=[row.pk])).status_code, 403)
        self.assertEqual(self.client.get(reverse("admin:shops_accountchange_add")).status_code, 403)

    def test_a_staff_account_without_the_group_sees_none_of_this(self):
        stranger = User.objects.create_user("other", password="x", is_staff=True)
        sign_in_as_staff_session(self.client, stranger)
        self.assertEqual(self.client.get("/admin/today/").status_code, 403)
        for name in ("shops_shop", "shops_helprequest"):
            self.assertEqual(self.client.get(reverse(f"admin:{name}_changelist")).status_code, 403, name)

    def test_the_group_is_not_enough_without_staff_status(self):
        """The group gives the rights; the admin opens only for staff."""
        user = User.objects.create_user("without", password="x")
        user.groups.add(the_group())
        sign_in_as_staff_session(self.client, user)
        self.assertEqual(self.client.get("/admin/today/").status_code, 302)


# ---------------------------------------------------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------------------------------------------------

class Command(TestCase):
    def test_add_makes_the_account_staff_and_a_member(self):
        User.objects.create_user("maria", password="x")
        out = run("add", "maria")
        user = User.objects.get(username="maria")
        self.assertEqual((user.is_staff, user.is_superuser, list(user.groups.values_list("name", flat=True))),
                         (True, False, ["Moderators"]))
        self.assertIn("maria is a moderator and now has access to /admin/.", out)
        self.assertNotIn("no password", out)

    def test_add_is_the_same_when_run_again(self):
        User.objects.create_user("maria", password="x")
        run("add", "maria")
        self.assertIn("is a moderator already. Nothing changed.", run("add", "maria"))
        self.assertEqual(the_group().user_set.count(), 1)

    def test_add_to_an_account_that_was_staff_only_joins_the_group(self):
        User.objects.create_user("maria", password="x", is_staff=True)
        self.assertEqual(run("add", "maria"), "maria is a moderator.\n")

    def test_add_says_when_the_account_has_no_password(self):
        User.objects.create_user("maria")
        self.assertIn("`manage.py changepassword maria`", run("add", "maria"))

    def test_add_makes_the_group_if_it_is_gone(self):
        Group.objects.all().delete()
        User.objects.create_user("maria", password="x")
        run("add", "maria")
        self.assertEqual(rights_of(the_group()), EXPECTED)
        self.assertEqual(the_group().user_set.count(), 1)

    def test_add_for_an_account_that_does_not_exist_changes_nothing(self):
        with self.assertRaisesRegex(CommandError, "There is no account called 'nobody'"):
            run("add", "nobody")
        self.assertEqual(User.objects.count(), 0)

    def test_the_name_is_exact(self):
        User.objects.create_user("Maria", password="x")
        with self.assertRaises(CommandError):
            run("add", "maria")
        self.assertFalse(User.objects.get().is_staff)

    def test_add_refuses_the_owner_of_a_shop_and_changes_nothing_and_writes_nothing(self):
        owner = User.objects.create_user("merchant", password="x")
        Profile.objects.create(user=owner, email="owner@example.org")
        Shop.objects.create(owner=owner, name="Red Bar")
        with self.assertRaisesRegex(CommandError, "owns 1 shop.*Red Bar.*second account.*Nothing changed"):
            run("add", "merchant")
        owner.refresh_from_db()
        self.assertEqual((owner.is_staff, owner.groups.count()), (False, 0))
        self.assertFalse(AccountChange.objects.exists())
        self.assertEqual(mail.outbox, [])

    def test_add_refuses_who_is_switched_off_or_has_every_right_already(self):
        User.objects.create_user("switched_off", password="x", is_active=False)
        User.objects.create_superuser("boss")
        for name, words in (("switched_off", "switched off"), ("boss", "is a superuser")):
            with self.assertRaisesRegex(CommandError, words):
                run("add", name)
            user = User.objects.get(username=name)
            self.assertEqual(user.groups.count(), 0)
        self.assertFalse(User.objects.get(username="switched_off").is_staff)

    def test_remove_takes_the_staff_status_with_the_group(self):
        User.objects.create_user("maria", password="x")
        run("add", "maria")
        out = run("remove", "maria")
        user = User.objects.get(username="maria")
        self.assertEqual((user.is_staff, user.groups.count()), (False, 0))
        self.assertIn("maria is not a moderator any more and no longer has access to /admin/.", out)

    def test_remove_keeps_the_staff_status_of_who_has_other_rights(self):
        other = Group.objects.create(name="Other")
        for name, setup in (("with_group", lambda user: user.groups.add(other)),
                            ("with_permission", lambda user: user.user_permissions.add(Permission.objects.get(codename="view_shop")))):
            user = User.objects.create_user(name, password="x")
            run("add", name)
            setup(user)
            out = run("remove", name)
            user = User.objects.get(username=name)
            self.assertEqual((user.is_staff, user.groups.filter(name="Moderators").exists()), (True, False), name)
            self.assertIn("stays staff", out)

    def test_remove_does_not_touch_a_superuser(self):
        boss = User.objects.create_superuser("boss")
        the_group().user_set.add(boss)
        run("remove", "boss")
        boss.refresh_from_db()
        self.assertEqual((boss.is_staff, boss.is_superuser, boss.groups.count()), (True, True, 0))

    def test_remove_from_someone_who_is_not_a_moderator_changes_nothing(self):
        User.objects.create_user("office_boss", password="x", is_staff=True)
        self.assertIn("is not a moderator. Nothing changed.", run("remove", "office_boss"))
        self.assertTrue(User.objects.get().is_staff)
        Group.objects.all().delete()
        self.assertIn("is not a moderator. Nothing changed.", run("remove", "office_boss"))
        with self.assertRaises(CommandError):
            run("remove", "nobody")

    def test_an_action_is_needed_and_a_name_for_add_and_remove(self):
        for args in ((), ("add",), ("remove",), ("fire", "x")):
            with self.assertRaises(CommandError, msg=args):
                run(*args)

    def test_add_puts_the_group_right_before_adding(self):
        """Nobody joins a group that has more than the work asks, whatever was done to it since the last migrate."""
        User.objects.create_user("maria", password="x")
        the_group().permissions.add(Permission.objects.get(codename="change_shop"))
        the_group().permissions.remove(Permission.objects.get(codename="view_accountchange"))
        run("add", "maria")
        self.assertEqual(rights_of(the_group()), EXPECTED)

    def test_add_changes_nothing_when_it_fails_after_the_sync(self):
        """All or nothing, the sync included: the group's rights are put right in the same transaction."""
        User.objects.create_user("maria", password="x")
        the_group().permissions.add(Permission.objects.get(codename="change_shop"))
        with patch.object(User, "save", side_effect=RuntimeError("database down")), self.assertRaises(RuntimeError):
            run("add", "maria")
        self.assertEqual(the_group().user_set.count(), 0)
        self.assertIn("shops.change_shop", rights_of(the_group()))

    def test_sync_puts_the_rights_right_and_says_what_it_did(self):
        self.assertIn("already has exactly the rights of the list. Nothing changed.", run("sync"))
        group = the_group()
        group.permissions.add(Permission.objects.get(codename="change_shop"))
        group.permissions.remove(Permission.objects.get(codename="view_accountchange"))
        out = run("sync")
        self.assertIn("given back  shops.view_accountchange", out)
        self.assertIn("taken away  shops.change_shop", out)
        self.assertEqual(rights_of(the_group()), EXPECTED)
        Group.objects.all().delete()
        self.assertIn("did not exist: made, with 4 rights", run("sync"))
        self.assertEqual(rights_of(the_group()), EXPECTED)

    def test_sync_leaves_the_members_alone(self):
        User.objects.create_user("maria", password="x")
        run("add", "maria")
        run("sync")
        self.assertEqual(the_group().user_set.count(), 1)

    def test_list_shows_what_else_a_member_has(self):
        other = Group.objects.create(name="Other")
        user = User.objects.create_user("maria", password="x")
        run("add", "maria")
        user.groups.add(other)
        user.user_permissions.add(Permission.objects.get(codename="view_shop"), Permission.objects.get(codename="change_shop"))
        User.objects.create_user("pia", password="x")
        run("add", "pia")
        out = run("list")
        self.assertIn("  maria  (also in group Other; 2 right(s) of its own)", out)
        self.assertIn("  pia\n", out)

    def test_list(self):
        self.assertIn("Members: 0. `manage.py moderators add <name>`", run("list"))
        User.objects.create_user("maria", password="x")
        User.objects.create_user("without")
        run("add", "maria")
        run("add", "without")
        out = run("list")
        for right in EXPECTED:
            self.assertIn(f"  {right}: ", out)
        self.assertNotIn("MISSING", out)
        self.assertNotIn("NOT ON THE LIST", out)
        self.assertIn("Members: 2", out)
        self.assertIn("  maria\n", out)
        self.assertIn("  without  (no password: it cannot sign in at /admin/)", out)

    def test_list_says_what_is_wrong(self):
        group = the_group()
        group.permissions.add(Permission.objects.get(codename="change_shop"))
        group.permissions.remove(Permission.objects.get(codename="view_accountchange"))
        owner = User.objects.create_user("merchant", password="x", is_staff=True)
        Shop.objects.create(owner=owner, name="Bar")
        off = User.objects.create_user("switched_off", password="x", is_active=False)
        plain = User.objects.create_user("plain", password="x")
        boss = User.objects.create_superuser("boss")
        group.user_set.add(owner, off, plain, boss)
        out = run("list")
        self.assertIn("MISSING  shops.view_accountchange", out)
        self.assertIn("NOT ON THE LIST  shops.change_shop: `manage.py migrate` takes it away", out)
        self.assertIn("owns 1 shop(s): as staff it cannot use them", out)
        self.assertIn("switched off: it cannot sign in", out)
        self.assertIn("not staff: it cannot open /admin/", out)
        self.assertIn("superuser: it has every right", out)
        # list only reads
        self.assertEqual(Permission.objects.filter(group=group, codename="change_shop").count(), 1)

    def test_list_without_the_group(self):
        Group.objects.all().delete()
        self.assertIn("does not exist yet", run("list"))
        self.assertFalse(Group.objects.exists())

    def test_names_with_escape_sequences_are_not_printed_as_they_are(self):
        user = User.objects.create_user("a\x1b[2Jb", password="x")
        run("add", "a\x1b[2Jb")
        self.assertNotIn("\x1b", run("list"))
        with self.assertRaises(CommandError) as raised:
            run("add", "x\x1b[2Jy")
        self.assertNotIn("\x1b", str(raised.exception))
        self.assertTrue(user.pk)


@override_settings(LANGUAGE_CODE="en", TELEGRAM_BOT_TOKEN="1:x", TELEGRAM_MODERATION_CHAT="-100", PUBLIC_URL="https://site.example.org")
class TheRegister(Base):
    """The command works through the models, so the register of the changes to the owners' accounts sees what it does, as it
    sees the admin (shops/guard.py). It records the accounts that own a shop, and `add` refuses those; `remove` takes
    rights away from them, which is always allowed, and the register says so."""

    def setUp(self):
        super().setUp()
        self.owner = User.objects.create_user("merchant", password="x", is_staff=True)
        Profile.objects.create(user=self.owner, email="owner@example.org")
        Shop.objects.create(owner=self.owner, name="Red Bar", telegram_chat="111")
        self.owner.groups.add(the_group())
        AccountChange.objects.all().delete()
        patcher = patch("shops.guard.telegram", return_value=True)
        self.telegram = patcher.start()
        self.addCleanup(patcher.stop)

    def test_remove_from_an_owner_is_one_row_and_one_message_to_each(self):
        with self.captureOnCommitCallbacks(execute=True):
            out = run("remove", "merchant")
        self.assertIn("no longer has access to /admin/", out)
        row = AccountChange.objects.get()
        self.assertEqual((row.changed, row.via, row.actor, row.shops), ("groups,is_staff", "command", "", "Red Bar"))
        self.assertEqual(row.user, self.owner)
        self.assertEqual(row.told, "email,telegram,association")
        self.assertEqual(row.not_told, "")
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["owner@example.org"])
        chats = {call.args[0] for call in self.telegram.call_args_list}
        self.assertEqual(chats, {"-100", "111"})
        self.assertTrue(Profile.objects.get(user=self.owner).account_changed)

    def test_a_group_given_to_an_owner_by_hand_is_seen_as_any_group_change(self):
        other = User.objects.create_user("other", password="x")
        Profile.objects.create(user=other, email="other@example.org")
        Shop.objects.create(owner=other, name="Pub")
        with self.captureOnCommitCallbacks(execute=True):
            other.groups.add(the_group())
        self.assertEqual(AccountChange.objects.get().changed, "groups")

    def test_an_account_without_a_shop_leaves_no_row_because_the_register_is_for_owners(self):
        User.objects.create_user("maria", password="x")
        with self.captureOnCommitCallbacks(execute=True):
            run("add", "maria")
            run("remove", "maria")
        self.assertFalse(AccountChange.objects.exists())
        self.assertEqual(mail.outbox, [])

    def test_the_command_is_not_a_way_around_the_register(self):
        """No update() and no SQL: the change goes through save() and the group's own methods, which the signals see."""
        with patch("shops.guard.record") as record, self.captureOnCommitCallbacks(execute=True):
            run("remove", "merchant")
        codes = [call.args[1] for call in record.call_args_list]
        self.assertIn(["is_staff"], codes)
        self.assertIn(["groups"], codes)

    def test_a_failure_in_the_middle_leaves_the_account_as_it_was(self):
        """All or nothing: the group is taken away and the staff status with it, or neither."""
        with patch.object(User, "save", side_effect=RuntimeError("database down")), self.assertRaises(RuntimeError):
            run("remove", "merchant")
        self.assertEqual(list(self.owner.groups.values_list("name", flat=True)), ["Moderators"])
        self.assertTrue(User.objects.get(pk=self.owner.pk).is_staff)

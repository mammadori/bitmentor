"""The changes to the account of a shop's owner (shops/guard.py): written in a register, told to the owner and to the
association, shown to the owner's panel."""
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core import mail
from django.db import transaction
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone

from config.guard import current_request

from . import guard
from .models import AccountChange, Profile, Shop
from .test_guard import Base, sign_in_as_staff_session
from .tests import admin_session

User = get_user_model()


# ---------------------------------------------------------------------------------------------------------------------

def in_admin(user=None, path="/admin/auth/user/1/change/"):
    """Code that runs as if a page of the admin was being served, with this staff user signed in."""
    request = RequestFactory().post(path)
    request.user = user or User.objects.get_or_create(username="boss", defaults={"is_staff": True})[0]
    return _InRequest(request)


class _InRequest:
    def __init__(self, request):
        self.request = request

    def __enter__(self):
        self.token = current_request.set(self.request)

    def __exit__(self, *exc):
        current_request.reset(self.token)


@override_settings(LANGUAGE_CODE="en", TELEGRAM_BOT_TOKEN="1:x", TELEGRAM_MODERATION_CHAT="-100",
                   PUBLIC_URL="https://site.example.org")
class Watch(Base):
    def setUp(self):
        super().setUp()
        self.owner = User.objects.create(username="key1")
        Profile.objects.create(user=self.owner, email="owner@example.org")
        self.shop = Shop.objects.create(owner=self.owner, name="Bar", telegram_chat="111")
        self.other = Shop.objects.create(owner=self.owner, name="Pub", telegram_chat="222")
        patcher = patch("shops.guard.telegram")
        self.telegram = patcher.start()
        # What shops.notifications.telegram says when Telegram took the message
        self.telegram.return_value = True
        self.addCleanup(patcher.stop)

    def run_commit(self, change):
        with self.captureOnCommitCallbacks(execute=True):
            change()

    def only(self):
        return AccountChange.objects.get()

    def test_a_password_set_by_the_admin_is_written_and_said_to_everyone(self):
        def change():
            with in_admin():
                self.owner.set_password("a password nobody told the owner")
                self.owner.save()
        self.run_commit(change)
        row = self.only()
        self.assertEqual((row.changed, row.via, row.actor, row.user), ("password", "admin", "boss", self.owner))
        self.assertEqual(sorted(row.shops.split(", ")), ["Bar", "Pub"])
        self.assertEqual((row.told, row.not_told), ("email,telegram,association", ""))
        # The owner by email, on the recovery address
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["owner@example.org"])
        self.assertIn("A password was set or changed on your account.", message.body)
        self.assertIn("Bar, Pub", message.body)
        self.assertIn("write to info@example.org now", message.body)
        self.assertNotIn("nobody told", message.body + message.subject)
        # In the Telegram chat of each of their shops, and the association's
        chats = sorted(call.args[0] for call in self.telegram.call_args_list)
        self.assertEqual(chats, ["-100", "111", "222"])
        association = next(call.args[1] for call in self.telegram.call_args_list if call.args[0] == "-100")
        self.assertIn("Bar, Pub", association)
        self.assertIn("By boss, from the admin.", association)
        self.assertIn("https://site.example.org/admin/shops/accountchange/", association)
        self.assertNotIn("nobody told", association)
        # The account is marked
        self.assertTrue(guard.recently_changed(self.owner))

    def test_the_name_of_a_shop_is_not_a_link_in_the_associations_chat(self):
        Shop.objects.filter(pk=self.shop.pk).update(name="Bar https://evil.example/x")
        self.run_commit(lambda: (setattr(self.owner, "is_active", False), self.owner.save()))
        association = next(call.args[1] for call in self.telegram.call_args_list if call.args[0] == "-100")
        self.assertNotIn("evil.example", association)
        self.assertIn("evil.example", self.only().shops)

    def test_each_watched_field(self):
        for field, value, code in (("is_staff", True, "is_staff"), ("is_superuser", True, "is_superuser"),
                                   ("is_active", False, "is_active"), ("username", "other-key", "username")):
            AccountChange.objects.all().delete()
            with self.subTest(field):
                self.run_commit(lambda: (setattr(self.owner, field, value), self.owner.save()))
                self.assertEqual(self.only().changed, code)
                setattr(self.owner, field, {"is_staff": False, "is_superuser": False, "is_active": True,
                                            "username": "key1"}[field])
                User.objects.filter(pk=self.owner.pk).update(**{field: getattr(self.owner, field)})

    def test_a_save_that_changes_nothing_watched_says_nothing(self):
        self.owner.first_name = "Mario"
        self.owner.save()
        self.owner.save(update_fields=["last_login"])
        self.owner.last_login = timezone.now()
        self.owner.save(update_fields=["last_login"])
        self.assertFalse(AccountChange.objects.exists())
        self.assertEqual(len(mail.outbox), 0)
        self.telegram.assert_not_called()
        self.assertFalse(guard.recently_changed(self.owner))

    def test_the_same_values_saved_again_say_nothing(self):
        self.run_commit(lambda: self.owner.save())
        self.assertFalse(AccountChange.objects.exists())

    def test_django_rewriting_the_hash_when_someone_signs_in_is_not_a_new_password(self):
        self.owner.set_password("something long enough 123")
        User.objects.filter(pk=self.owner.pk).update(password=self.owner.password)
        self.owner.refresh_from_db()
        self.owner.set_password("something long enough 123")
        self.owner.save(update_fields=["password"])
        self.assertFalse(AccountChange.objects.exists())
        # But a password where there was none is always a new one, whatever the fields saved
        owner = User.objects.create(username="key2")
        Shop.objects.create(owner=owner, name="Shop two")
        owner.set_password("a first password")
        owner.save(update_fields=["password"])
        self.assertEqual(AccountChange.objects.get().changed, "password")

    def test_groups_and_permissions_from_either_side(self):
        group = Group.objects.create(name="helpers")
        permission = Permission.objects.get(codename="view_shop")
        self.run_commit(lambda: self.owner.groups.add(group))
        self.assertEqual(self.only().changed, "groups")
        AccountChange.objects.all().delete()
        self.run_commit(lambda: self.owner.groups.add(group))
        self.assertFalse(AccountChange.objects.exists(), "already in it: nothing changed")
        self.run_commit(lambda: group.user_set.remove(self.owner))
        self.assertEqual(self.only().changed, "groups")
        AccountChange.objects.all().delete()
        self.run_commit(lambda: self.owner.user_permissions.add(permission))
        self.assertEqual(self.only().changed, "permissions")
        AccountChange.objects.all().delete()
        self.run_commit(lambda: permission.user_set.clear())
        self.assertEqual(self.only().changed, "permissions")
        AccountChange.objects.all().delete()
        self.owner.groups.add(group)
        AccountChange.objects.all().delete()
        self.run_commit(lambda: group.user_set.clear())
        self.assertEqual(self.only().changed, "groups")
        AccountChange.objects.all().delete()
        self.owner.groups.add(group)
        AccountChange.objects.all().delete()
        self.run_commit(lambda: self.owner.groups.clear())
        self.assertEqual(self.only().changed, "groups")

    def test_a_user_with_no_shop_is_not_watched(self):
        volunteer = User.objects.create(username="volunteer", is_staff=True)
        self.run_commit(lambda: (volunteer.set_password("a long enough password"), volunteer.save()))
        volunteer.groups.add(Group.objects.create(name="g"))
        self.assertFalse(AccountChange.objects.exists())
        self.assertEqual(len(mail.outbox), 0)

    def test_one_form_of_the_admin_is_one_message(self):
        """The user is saved, then the groups: two changes of the same commit make one row and one message each."""
        group = Group.objects.create(name="helpers")

        def change():
            with in_admin():
                self.owner.is_staff = True
                self.owner.set_password("some password again")
                self.owner.save()
                self.owner.groups.add(group)
        self.run_commit(change)
        row = self.only()
        self.assertEqual(row.changed, "groups,is_staff,password")
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(sum(1 for call in self.telegram.call_args_list if call.args[0] == "-100"), 1)
        self.assertIn("The rights of your account were changed.", mail.outbox[0].body)
        self.assertEqual(mail.outbox[0].body.count("The rights of your account were changed."), 1)

    def test_a_change_rolled_back_says_nothing_and_does_not_swallow_the_next(self):
        try:
            with transaction.atomic():
                self.owner.is_staff = True
                self.owner.save()
                raise RuntimeError("the form was refused")
        except RuntimeError:
            pass
        self.assertFalse(AccountChange.objects.exists())
        self.owner.refresh_from_db()
        self.assertFalse(self.owner.is_staff)

        def change():
            self.owner.is_active = False
            self.owner.save()
        self.run_commit(change)
        row = self.only()
        self.assertEqual(row.changed, "is_active")
        self.assertEqual(len(mail.outbox), 1)

    def test_from_a_command_or_a_shell_it_is_written_too(self):
        self.run_commit(lambda: (setattr(self.owner, "username", "new-key"), self.owner.save()))
        row = self.only()
        self.assertEqual((row.via, row.actor), ("command", ""))
        self.assertIn("From a command or the shell.", next(
            call.args[1] for call in self.telegram.call_args_list if call.args[0] == "-100"))

    def test_the_password_page_of_the_admin_end_to_end(self):
        admin_session(self.client)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(f"/admin/auth/user/{self.owner.pk}/password/", {
                "usable_password": "true", "password1": "correct-horse-battery-staple-9",
                "password2": "correct-horse-battery-staple-9"})
        self.assertEqual(response.status_code, 302)
        row = self.only()
        self.assertEqual((row.changed, row.via, row.actor), ("password", "admin", "boss"))
        self.assertEqual(len(mail.outbox), 1)
        self.assertNotIn("correct-horse", mail.outbox[0].body + row.changed + row.shops)

    def test_the_form_of_the_user_in_the_admin_end_to_end(self):
        admin_session(self.client)
        url = f"/admin/auth/user/{self.owner.pk}/change/"
        data = {"username": "key1", "is_active": "on", "is_staff": "on", "date_joined_0": "2026-01-01",
                "date_joined_1": "10:00:00"}
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(url, {**data, "_save": "Save"})
        self.assertEqual(response.status_code, 302)
        row = self.only()
        self.assertEqual((row.changed, row.via, row.actor), ("is_staff", "admin", "boss"))

    def test_a_shop_handed_to_another_account_is_told_to_the_owner_it_leaves(self):
        thief = User.objects.create(username="key-thief")

        def change():
            with in_admin():
                self.shop.owner = thief
                self.shop.save()
        self.run_commit(change)
        row = self.only()
        self.assertEqual((row.user, row.changed, row.shops, row.via), (self.owner, "shop_owner", "Bar", "admin"))
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("A shop of yours was handed to another account.", mail.outbox[0].body)
        self.assertTrue(Profile.objects.get(user=self.owner).account_changed)

    def test_the_chat_of_the_notices_pointed_elsewhere_is_told_to_the_chat_it_leaves(self):
        def change():
            with in_admin():
                self.shop.telegram_chat = "999"
                self.shop.save()
        self.run_commit(change)
        row = self.only()
        self.assertEqual((row.changed, row.shops), ("shop_chat", "Bar"))
        chats = sorted(call.args[0] for call in self.telegram.call_args_list)
        self.assertEqual(chats, ["-100", "111"])
        self.assertNotIn("999", "".join(str(call) for call in self.telegram.call_args_list))

    def test_the_owner_editing_their_own_shop_on_the_site_is_not_a_change_to_watch(self):
        request = RequestFactory().post("/en/shops/edit/x/")
        request.user = self.owner
        token = current_request.set(request)
        try:
            self.shop.telegram_chat = "555"
            self.shop.save()
        finally:
            current_request.reset(token)
        self.assertFalse(AccountChange.objects.exists())

    def test_a_shop_that_gets_its_first_owner_is_not_a_change_of_anybodys_account(self):
        free = Shop.objects.create(name="Imported")
        with in_admin():
            free.owner = self.owner
            free.save()
        self.assertFalse(AccountChange.objects.exists())

    def test_the_account_deleted_is_written_and_the_row_outlives_it(self):
        with self.captureOnCommitCallbacks(execute=True):
            with in_admin():
                self.owner.delete()
        row = self.only()
        self.assertEqual((row.changed, row.user), ("deleted", None))
        self.assertEqual(sorted(row.shops.split(", ")), ["Bar", "Pub"])
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Your account was deleted.", mail.outbox[0].body)
        self.assertFalse(Shop.objects.exists())

    def change(self):
        """A change of the sign-in name, each time another one; the register of the last one."""
        AccountChange.objects.all().delete()
        self.changes = getattr(self, "changes", 0) + 1
        with self.captureOnCommitCallbacks(execute=True):
            self.owner.username = f"other-{self.changes}"
            self.owner.save()
        return self.only()

    def test_what_could_not_be_said_is_written_and_nothing_fails(self):
        with patch("shops.guard.send_mail", side_effect=OSError):
            self.telegram.side_effect = guard.NotificationFailed("500")
            row = self.change()
        self.assertEqual((row.told, row.not_told), ("", "email,telegram,association"))
        # A Telegram that does not answer is not asked again for the next chat: the association was the first and only try
        self.assertEqual([call.args[0] for call in self.telegram.call_args_list], ["-100"])

    def test_the_association_is_told_first_then_the_owner(self):
        self.change()
        self.assertEqual([call.args[0] for call in self.telegram.call_args_list], ["-100", "111", "222"])

    def test_a_telegram_that_refuses_is_not_a_telegram_that_told(self):
        """telegram() says False when the chat is wrong or the bot blocked: nothing arrived."""
        self.telegram.side_effect = lambda chat, text: chat != "111"
        row = self.change()
        self.assertEqual((row.told, row.not_told), ("email,association", "telegram"))
        self.telegram.side_effect = lambda chat, text: chat != "-100"
        AccountChange.objects.all().delete()
        row = self.change()
        self.assertEqual((row.told, row.not_told), ("email,telegram", "association"))

    def test_the_chats_beyond_the_limit_are_never_written_to_and_so_not_told(self):
        for number in range(3, 8):
            Shop.objects.create(owner=self.owner, name=f"Shop {number}", telegram_chat=f"{number}00")
        row = self.change()
        self.assertEqual((row.told, row.not_told), ("email,association", "telegram"))
        # The association and MAX_CHATS chats, no more
        self.assertEqual(len(self.telegram.call_args_list), 1 + guard.MAX_CHATS)

    def test_each_channel_on_its_own(self):
        """Whatever goes wrong in one, the others are still tried, and the register has the rest."""
        with patch("shops.guard.send_mail", side_effect=ValueError), self.assertLogs("shops.guard", "ERROR") as log:
            row = self.change()
        self.assertEqual((row.told, row.not_told), ("telegram,association", "email"))
        self.assertIn("email failed with ValueError", log.output[0])
        AccountChange.objects.all().delete()
        self.telegram.side_effect = RuntimeError("the bot's token must not be written")
        with self.assertLogs("shops.guard", "ERROR") as log:
            row = self.change()
        self.assertEqual((row.told, row.not_told), ("email", "telegram,association"))
        self.assertNotIn("token", " ".join(log.output))

    def test_the_round_has_a_time_limit(self):
        with patch("shops.guard.DELIVERY_SECONDS", 0):
            row = self.change()
        self.assertEqual((row.told, row.not_told), ("", "email,telegram,association"))
        self.telegram.assert_not_called()
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(TELEGRAM_BOT_TOKEN="", TELEGRAM_MODERATION_CHAT="")
    def test_without_the_bot_the_register_says_nobody_but_the_email_was_told(self):
        self.telegram.return_value = None  # no bot, no chat: nothing sent
        self.run_commit(lambda: (setattr(self.owner, "is_active", False), self.owner.save()))
        row = self.only()
        self.assertEqual((row.told, row.not_told), ("email", "telegram,association"))

    def test_an_owner_with_no_email_and_no_chat_is_told_by_the_panel(self):
        owner = User.objects.create(username="key3")
        Shop.objects.create(owner=owner, name="Lone")
        self.run_commit(lambda: (setattr(owner, "username", "key3b"), owner.save()))
        self.assertEqual(len(mail.outbox), 0)
        client = self.client_class()
        client.force_login(owner)
        panel = client.get("/en/shops/")
        self.assertContains(panel, "someone who runs the site changed your account")
        self.assertContains(panel, "The name your account signs in with was replaced by another one.")
        self.assertContains(panel, "write to info@example.org now")
        # After two weeks the panel is quiet again; the register is not
        AccountChange.objects.update(created=timezone.now() - timedelta(days=15))
        self.assertNotContains(client.get("/en/shops/"), "someone who runs the site changed your account")
        self.assertEqual(AccountChange.objects.count(), 1)

    def test_the_owner_is_told_in_the_language_of_the_community(self):
        for language, expected in (("it", "Qualcuno che gestisce il sito ha cambiato il tuo account su Example community:"),
                                   ("de", "Jemand, der die Website betreibt, hat Ihr Konto auf Example community geändert:")):
            mail.outbox.clear()
            AccountChange.objects.all().delete()
            with self.subTest(language), override_settings(LANGUAGE_CODE=language):
                self.run_commit(lambda: (setattr(self.owner, "is_active", not self.owner.is_active), self.owner.save()))
                self.assertIn(expected, mail.outbox[0].body)
                self.assertIn("info@example.org", mail.outbox[0].body)

    def test_recently_changed_has_a_window(self):
        self.assertFalse(guard.recently_changed(self.owner))
        self.run_commit(lambda: (setattr(self.owner, "is_active", False), self.owner.save()))
        self.assertTrue(guard.recently_changed(self.owner))
        Profile.objects.filter(user=self.owner).update(account_changed=timezone.now() - timedelta(days=3))
        self.assertTrue(guard.recently_changed(self.owner))
        self.assertFalse(guard.recently_changed(self.owner, timedelta(days=2)))
        self.assertFalse(guard.recently_changed(None))


class Register(Base):
    """The register in the admin: to read, and nobody changes it there."""

    def setUp(self):
        super().setUp()
        admin_session(self.client)
        owner = User.objects.create(username="key1")
        Shop.objects.create(owner=owner, name="Red Bar")
        with in_admin(User.objects.get(username="boss")):
            owner.username = "key9"
            owner.save()
        self.row = AccountChange.objects.get()

    def test_it_reads_in_a_sentence(self):
        page = self.client.get("/admin/shops/accountchange/")
        self.assertContains(page, "The account of Red Bar was changed from the admin by boss.")
        self.assertContains(page, "name it signs in with")
        detail = self.client.get(f"/admin/shops/accountchange/{self.row.pk}/change/")
        self.assertEqual(detail.status_code, 200)
        self.assertNotContains(detail, 'name="actor"')

    def test_who_was_told_is_said_in_words(self):
        AccountChange.objects.filter(pk=self.row.pk).update(told="email,telegram", not_told="association")
        page = self.client.get("/admin/shops/accountchange/").content.decode()
        self.assertIn("email, Telegram chat of the shop", page)
        self.assertIn("chat of the association", page)
        self.assertIn("?via__exact=admin", page)

    def test_it_reads_in_italian_for_whoever_runs_the_site_in_italian(self):
        page = self.client.get("/admin/shops/accountchange/", headers={"Accept-Language": "it"})
        self.assertContains(page, "L&#x27;account di Red Bar è stato modificato dall&#x27;admin (utente: boss).")
        self.assertContains(page, "nome con cui entra")

    def test_nobody_adds_changes_or_deletes_there_not_even_the_superuser(self):
        self.assertEqual(self.client.get("/admin/shops/accountchange/add/").status_code, 403)
        self.assertEqual(self.client.get(f"/admin/shops/accountchange/{self.row.pk}/delete/").status_code, 403)
        self.assertEqual(self.client.post(f"/admin/shops/accountchange/{self.row.pk}/delete/", {"post": "yes"}).status_code, 403)
        self.client.post("/admin/shops/accountchange/", {"action": "delete_selected", "_selected_action": [self.row.pk],
                                                          "post": "yes"})
        self.client.post(f"/admin/shops/accountchange/{self.row.pk}/change/", {"actor": "someone else"})
        self.row.refresh_from_db()
        self.assertEqual((AccountChange.objects.count(), self.row.actor), (1, "boss"))
        page = self.client.get("/admin/shops/accountchange/").content.decode()
        self.assertNotIn("delete_selected", page)

    def test_the_dashboard_says_how_many_lately(self):
        self.assertContains(self.client.get("/admin/today/"), "1 change to the account of a shop's owner in the last two weeks")
        AccountChange.objects.update(created=timezone.now() - timedelta(days=20))
        self.assertNotContains(self.client.get("/admin/today/"), "in the last two weeks")

    def test_a_staff_member_who_may_only_see_shops_does_not_see_the_register(self):
        clerk = User.objects.create_user("clerk", is_staff=True)
        clerk.user_permissions.add(Permission.objects.get(codename="view_shop"))
        sign_in_as_staff_session(self.client, clerk)
        self.assertEqual(self.client.get("/admin/shops/accountchange/").status_code, 403)
        self.assertNotContains(self.client.get("/admin/today/"), "in the last two weeks")


class Migrations(TestCase):
    def test_the_models_and_the_migrations_agree(self):
        from django.core.management import call_command

        call_command("makemigrations", "shops", check=True, dry_run=True, verbosity=0)

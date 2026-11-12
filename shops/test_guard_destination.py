"""A change of where a shop's money goes (shops/guard.py, destination_changed and deliver_destination): written in the
register, said after the commit to the owner and the association, with the addresses short; the owner's first connection
of a wallet is only confirmed to the owner; whatever fails in telling never stops the change."""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.db import transaction
from django.test import RequestFactory, TestCase, override_settings

from config.guard import current_request

from . import guard
from .models import DEMO_PREFIX, AccountChange, Profile, Shop
from .notifications import NotificationFailed

User = get_user_model()
OLD = "marco.rossi@ln.example.org"
NEW = "thief@elsewhere.example"


class as_visitor:
    """Code that runs as if a page of the site (or of the admin) was served to this user."""

    def __init__(self, user, path="/en/shops/wallet/1/"):
        self.request = RequestFactory().post(path)
        self.request.user = user

    def __enter__(self):
        self.token = current_request.set(self.request)

    def __exit__(self, *exc):
        current_request.reset(self.token)


@override_settings(LANGUAGE_CODE="en", TELEGRAM_BOT_TOKEN="1:x", TELEGRAM_MODERATION_CHAT="-100",
                   PUBLIC_URL="https://site.example.org")
class Destination(TestCase):
    def setUp(self):
        self.owner = User.objects.create(username="key1")
        Profile.objects.create(user=self.owner, email="owner@example.org")
        self.shop = Shop.objects.create(owner=self.owner, name="Bar", telegram_chat="111", lightning_address=OLD)
        patcher = patch("shops.guard.telegram", return_value=True)
        self.telegram = patcher.start()
        self.addCleanup(patcher.stop)

    def change(self, to=NEW, shop=None, by=None):
        shop = shop or self.shop
        with self.captureOnCommitCallbacks(execute=True):
            if by:
                with as_visitor(by):
                    Shop.objects.get(pk=shop.pk)
                    shop.lightning_address = to
                    shop.save()
            else:
                shop.lightning_address = to
                shop.save()
        return AccountChange.objects.order_by("-pk").first()

    def without_address(self):
        """A shop with no destination yet, and nothing in the register or the profile about how it got there."""
        Shop.objects.filter(pk=self.shop.pk).update(lightning_address="")
        self.shop.refresh_from_db()
        AccountChange.objects.all().delete()
        Profile.objects.filter(user=self.owner).update(account_changed=None)
        self.telegram.reset_mock()
        mail.outbox.clear()

    def messages(self):
        return [call.args for call in self.telegram.call_args_list]

    def test_a_replaced_address_is_an_alarm_to_the_owner_and_the_association_with_the_addresses_short(self):
        row = self.change()
        self.assertEqual((row.changed, row.via, row.shops, row.user), ("shop_lightning", "command", "Bar", self.owner))
        self.assertEqual((row.told, row.not_told), ("email,telegram,association", ""))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["owner@example.org"])
        self.assertIn("Someone who runs the site changed where a shop of yours gets paid", mail.outbox[0].body)
        self.assertIn("ma…@ln.example.org → th…@elsewhere.example", mail.outbox[0].body)
        self.assertIn("Shop: Bar.", mail.outbox[0].body)
        chats = {chat: text for chat, text in self.messages()}
        self.assertEqual(set(chats), {"-100", "111"})
        self.assertIn("The register: https://site.example.org/admin/shops/accountchange/", chats["-100"])
        self.assertIn("From a command or the shell.", chats["-100"])
        # The whole address never leaves the site in a notice
        for text in [mail.outbox[0].body, *chats.values()]:
            self.assertNotIn(OLD, text)
            self.assertNotIn(NEW, text)
        # The owner's profile says that the account was changed by someone else
        self.assertTrue(guard.recently_changed(self.owner))

    def test_the_owner_s_first_wallet_is_only_confirmed_to_the_owner(self):
        self.without_address()
        row = self.change(to=OLD, by=self.owner)
        self.assertEqual((row.via, row.actor), ("owner", ""))
        self.assertEqual((row.told, row.not_told), ("email,telegram", ""))
        self.assertIn("You changed where a shop of yours gets paid", mail.outbox[0].body)
        self.assertIn("nothing → ma…@ln.example.org", mail.outbox[0].body)
        self.assertEqual({chat for chat, _text in self.messages()}, {"111"})
        self.assertFalse(guard.recently_changed(self.owner))

    def test_replacing_an_address_is_an_alarm_even_when_the_owner_s_account_asks(self):
        # A stolen account must not be able to confirm its own change to a channel the thief may have changed first
        row = self.change(by=self.owner)
        self.assertEqual(row.via, "site")
        self.assertEqual({chat for chat, _text in self.messages()}, {"-100", "111"})
        self.assertIn("association", row.told)
        self.assertTrue(guard.recently_changed(self.owner))

    def test_somebody_else_connecting_a_wallet_to_an_empty_shop_is_not_the_owner(self):
        self.without_address()
        row = self.change(to=OLD, by=User.objects.create(username="someone"))
        self.assertEqual(row.via, "site")
        self.assertIn("association", row.told)

    def test_what_could_not_be_told_is_written_down(self):
        self.telegram.return_value = False
        with override_settings(TELEGRAM_MODERATION_CHAT=""):
            row = self.change()
        self.assertEqual((row.told, row.not_told), ("email", "telegram,association"))

    def test_a_telegram_that_is_down_is_not_asked_again_for_the_next_chat(self):
        Shop.objects.create(owner=self.owner, name="Pub", telegram_chat="222")
        self.telegram.side_effect = NotificationFailed("500")
        row = self.change()
        self.assertEqual(self.telegram.call_count, 1)
        self.assertEqual((row.told, row.not_told), ("email", "telegram,association"))

    def test_a_mail_server_that_is_down_does_not_stop_the_other_channels(self):
        with patch("shops.guard.send_mail", side_effect=OSError("connection refused")):
            row = self.change()
        self.assertEqual((row.told, row.not_told), ("telegram,association", "email"))

    def test_a_channel_that_crashes_is_only_that_channel_s_failure(self):
        with patch("shops.guard.send_mail", side_effect=RuntimeError("a bug in a mail backend")), \
                self.assertLogs("shops.guard", "ERROR") as logged:
            row = self.change()
        self.assertEqual((row.told, row.not_told), ("telegram,association", "email"))
        # Only the kind of error is logged, never its text
        self.assertTrue(any("RuntimeError" in line for line in logged.output))
        self.assertFalse(any("mail backend" in line for line in logged.output))

    def test_without_an_email_or_a_chat_only_the_association_hears_it(self):
        Profile.objects.filter(user=self.owner).update(email="")
        Shop.objects.filter(pk=self.shop.pk).update(telegram_chat="")
        row = self.change()
        self.assertEqual((row.told, row.not_told), ("association", ""))
        self.assertEqual(mail.outbox, [])

    def test_a_user_with_many_shops_hears_it_in_a_few_chats_and_the_register_says_the_rest_were_not_told(self):
        for number in range(guard.MAX_CHATS + 2):
            Shop.objects.create(owner=self.owner, name=f"Other{number}", telegram_chat=str(300 + number))
        row = self.change()
        owners_chats = [chat for chat, _text in self.messages() if chat != "-100"]
        self.assertEqual(len(owners_chats), guard.MAX_CHATS)
        self.assertEqual(owners_chats, sorted(owners_chats))
        self.assertIn("telegram", row.not_told)

    def test_the_text_for_the_owner_tells_whom_to_write_to(self):
        with patch("shops.guard.community", return_value={**guard.community(), "email": "help@site.example.org"}):
            self.change()
        self.assertIn("write to help@site.example.org now", mail.outbox[0].body)
        mail.outbox.clear()
        with patch("shops.guard.community", return_value={k: v for k, v in guard.community().items() if k != "email"}):
            self.change(to="third@elsewhere.example")
        self.assertIn("tell the association now", mail.outbox[0].body)

    def test_nothing_is_said_for_a_row_that_no_longer_exists(self):
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            self.shop.lightning_address = NEW
            self.shop.save()
        AccountChange.objects.all().delete()
        for callback in callbacks:
            callback()
        self.assertEqual(self.messages(), [])
        self.assertEqual(mail.outbox, [])

    def test_a_change_rolled_back_says_nothing(self):
        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    self.shop.lightning_address = NEW
                    self.shop.save()
                    raise RuntimeError("rolled back")
            except RuntimeError:
                pass
        self.assertEqual(self.messages(), [])
        self.assertEqual(AccountChange.objects.count(), 0)

    def test_the_training_site_and_shops_without_owner_are_not_watched(self):
        demo = Shop.objects.create(owner=self.owner, name="Demo", external_id=f"{DEMO_PREFIX}1", lightning_address=OLD)
        self.assertIsNone(self.change(shop=demo))
        orphan = Shop.objects.create(name="Orphan", lightning_address=OLD)
        self.assertIsNone(self.change(shop=orphan))
        self.assertEqual(AccountChange.objects.count(), 0)

    def test_an_update_of_many_shops_is_watched_like_a_save(self):
        with self.captureOnCommitCallbacks(execute=True):
            Shop.objects.filter(pk=self.shop.pk).update(lightning_address=NEW)
        row = AccountChange.objects.get()
        self.assertEqual((row.changed, row.via), ("shop_lightning", "command"))
        self.assertIn("association", row.told)

    def test_a_guardian_that_fails_never_stops_the_change(self):
        with patch("shops.guard.destination_lines", side_effect=RuntimeError("bug")), \
                self.assertLogs("shops.guard", "ERROR") as logged:
            self.shop.lightning_address = NEW
            self.shop.save()
        self.shop.refresh_from_db()
        self.assertEqual(self.shop.lightning_address, NEW)
        self.assertTrue(any("RuntimeError" in line for line in logged.output))

    def test_the_address_is_cut_short_whatever_its_shape(self):
        self.assertEqual(guard.short_address("marco.rossi@ln.example.org"), "ma…@ln.example.org")
        self.assertEqual(guard.short_address("ab@ln.example.org"), "ab@ln.example.org")
        self.assertEqual(guard.short_address("noat-address"), "no…")
        self.assertEqual(guard.short_address("ab"), "ab")
        self.assertEqual(guard.short_address(None), "")
        self.assertEqual(guard.short_address("  a@b.example "), "a@b.example")

    def test_the_lines_of_a_notice_say_nothing_for_an_empty_side(self):
        self.assertEqual(guard.destination_text([("lightning_address", "", "ma…@x.example")]),
                         ["Lightning address: nothing → ma…@x.example"])
        self.assertEqual(guard.destination_text([("lightning_address", "ma…@x.example", "")]),
                         ["Lightning address: ma…@x.example → nothing"])

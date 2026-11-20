"""TELEGRAM_MODERATION_CHAT with several chats separated by commas: each moderator in a chat of their own."""

from types import SimpleNamespace
from unittest.mock import patch

import requests
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings

from . import guard
from .forms import ShopAdminForm, ShopDetailsForm
from .models import SHOP_CHATS, AccountChange, Shop
from .notifications import NotificationFailed, chats, deliver_to_moderators, telegram
from .test_guard_watch import in_admin

User = get_user_model()

POST = "shops.notifications.requests.post"


def answers(by_chat):
    """A Telegram that answers each chat with its status, or raises its exception."""

    def post(url, json, timeout):
        answer = by_chat[json["chat_id"]]
        if isinstance(answer, Exception):
            raise answer
        return SimpleNamespace(status_code=answer)

    return post


@override_settings(TELEGRAM_BOT_TOKEN="t")
class SeveralChats(TestCase):
    def test_the_list_is_read_with_spaces_and_empty_places(self):
        self.assertEqual(chats(" 11, 22 ,,@name_x ,"), ["11", "22", "@name_x"])
        self.assertEqual(chats(""), [])
        self.assertEqual(chats(None), [])
        self.assertEqual(chats("-100123"), ["-100123"])

    def test_one_chat_is_one_call_as_before(self):
        with patch(POST) as post:
            post.return_value.status_code = 200
            self.assertIs(telegram("11", "hello"), True)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.kwargs["json"], {"chat_id": "11", "text": "hello"})
        self.assertIsNone(telegram(" , ", "hello"))

    def test_every_chat_receives_the_same_message(self):
        with patch(POST) as post:
            post.return_value.status_code = 200
            self.assertIs(telegram("11, 22,33", "hello"), True)
        self.assertEqual(
            [call.kwargs["json"] for call in post.call_args_list],
            [{"chat_id": chat, "text": "hello"} for chat in ("11", "22", "33")],
        )

    def test_one_chat_that_takes_it_is_enough_and_the_others_are_in_the_log(self):
        with (
            patch(POST, side_effect=answers({"11": 403, "22": 502, "33": 200})) as post,
            self.assertLogs("shops.notifications", "ERROR") as log,
        ):
            self.assertIs(telegram("11,22,33", "hello"), True)
        self.assertEqual(post.call_count, 3)  # the first failures do not stop the others
        text = "\n".join(log.output)
        self.assertIn("chat 1 of 3 refused", text)
        self.assertIn("chat 2 of 3 did not take", text)

    def test_when_none_takes_it_the_answer_is_the_one_of_a_single_chat(self):
        with patch(POST, side_effect=answers({"11": 403, "22": 400})), self.assertLogs("shops.notifications", "ERROR"):
            self.assertIs(telegram("11,22", "hello"), False)
        # trying again may work for one of them: the caller tries again
        with (
            patch(POST, side_effect=answers({"11": 403, "22": 502})),
            self.assertLogs("shops.notifications", "ERROR"),
            self.assertRaises(NotificationFailed),
        ):
            telegram("11,22", "hello")

    def test_a_telegram_that_cannot_be_reached_is_asked_once(self):
        """Ten seconds of waiting for each chat would hold the merchant's page: the list stops at the first one."""
        broken = requests.ConnectionError("https://api/bott/x")
        with (
            patch(POST, side_effect=answers({"11": broken, "22": 200, "33": 200})) as post,
            self.assertLogs("shops.notifications", "ERROR") as log,
            self.assertRaises(NotificationFailed) as raised,
        ):
            telegram("11,22,33", "hello")
        self.assertEqual(post.call_count, 1)
        self.assertNotIn("bott", "\n".join(log.output) + str(raised.exception))  # the URL holds the token
        # the ones before it have the message: no second try, nothing repeated
        with (
            patch(POST, side_effect=answers({"11": 200, "22": broken, "33": 200})) as post,
            self.assertLogs("shops.notifications", "ERROR"),
        ):
            self.assertIs(telegram("11,22,33", "hello"), True)
        self.assertEqual(post.call_count, 2)

    def test_a_busy_telegram_does_not_stop_the_list(self):
        with (
            patch(POST, side_effect=answers({"11": 429, "22": 502, "33": 200})) as post,
            self.assertLogs("shops.notifications", "ERROR"),
        ):
            self.assertIs(telegram("11,22,33", "hello"), True)
        self.assertEqual(post.call_count, 3)

    def test_one_chat_writes_no_line_about_its_place(self):
        with patch(POST, side_effect=answers({"11": 403})), self.assertLogs("shops.notifications", "ERROR") as log:
            self.assertIs(telegram("11", "hello"), False)
        self.assertNotIn("chat 1 of", "\n".join(log.output))

    def test_a_shop_chat_can_be_a_short_list(self):
        """Was `test_a_shop_chat_cannot_be_a_list`: a shop now takes up to SHOP_CHATS chats (ShopChats below)."""
        form = ShopDetailsForm({"name": "Bar", "telegram_chat": "22,33"})
        self.assertTrue(form.is_valid(), form.errors)
        form = ShopDetailsForm({"name": "Bar", "telegram_chat": "22,33,44,55"})
        self.assertFalse(form.is_valid())
        self.assertIn("telegram_chat", form.errors)

    @override_settings(TELEGRAM_MODERATION_CHAT="11,22")
    def test_the_email_is_used_only_when_no_chat_took_it(self):
        with patch("config.community.community", return_value={"name": "X", "email": "staff@example.org"}):
            with (
                patch(POST, side_effect=answers({"11": 403, "22": 200})),
                self.assertLogs("shops.notifications", "ERROR"),
            ):
                self.assertEqual(deliver_to_moderators("Subject", "hello"), ("TELEGRAM_MODERATION_CHAT", []))
            self.assertEqual(mail.outbox, [])
            with (
                patch(POST, side_effect=answers({"11": 403, "22": 403})),
                self.assertLogs("shops.notifications", "ERROR"),
            ):
                where, problems = deliver_to_moderators("Subject", "hello")
            self.assertEqual(where, "the community's email")
            self.assertEqual(len(problems), 1)
            self.assertEqual(len(mail.outbox), 1)

    @override_settings(TELEGRAM_MODERATION_CHAT="11, -100123 ,@Moderators_x")
    def test_none_of_the_moderators_chats_is_a_shop_chat(self):
        for chat in ("11", "-100123", "@moderators_X", "0011"):
            with self.subTest(chat=chat):
                form = ShopDetailsForm({"name": "Bar", "telegram_chat": chat})
                self.assertFalse(form.is_valid())
                self.assertIn("telegram_chat", form.errors)
        form = ShopDetailsForm({"name": "Bar", "telegram_chat": "22"})
        form.is_valid()
        self.assertNotIn("telegram_chat", form.errors)


def shop_form(chat):
    return ShopDetailsForm({"name": "Bar", "telegram_chat": chat})


@override_settings(TELEGRAM_BOT_TOKEN="t", TELEGRAM_MODERATION_CHAT="11, @Moderators_x")
class ShopChats(TestCase):
    """A shop's Telegram chat: up to SHOP_CHATS, separated by commas, each one a chat that gets its own copy."""

    def test_up_to_three_chats_kept_without_spaces(self):
        form = shop_form(" 22 , @Shop_bar,-100123 ")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["telegram_chat"], "22,@Shop_bar,-100123")
        shop = form.save()
        self.assertEqual(Shop.objects.get(pk=shop.pk).telegram_chat, "22,@Shop_bar,-100123")
        self.assertEqual(shop_form("").is_valid(), True)

    def test_one_chat_as_before(self):
        form = shop_form("22")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["telegram_chat"], "22")

    def test_more_than_three_are_refused(self):
        self.assertEqual(SHOP_CHATS, 3)
        form = shop_form("22,33,44,55")
        self.assertFalse(form.is_valid())
        self.assertIn("telegram_chat", form.errors)

    def test_each_one_must_be_a_chat(self):
        for chat in ("22,abc", "22;33", "22 33", "@abc,22", "22,@name x", "--5", "22,https://t.me/x", "@name_x,-"):
            with self.subTest(chat=chat):
                form = shop_form(chat)
                self.assertFalse(form.is_valid())
                self.assertIn("telegram_chat", form.errors)

    def test_a_chat_written_twice_is_refused(self):
        for chat in ("22,22", "22,0022", "@Name_x,@name_X", "22,33,22"):
            with self.subTest(chat=chat):
                form = shop_form(chat)
                self.assertFalse(form.is_valid())
                self.assertEqual(form.errors["telegram_chat"], ["The same chat is written twice."])

    def test_no_chat_of_the_list_can_be_a_moderators_chat(self):
        for chat in ("22,11", "@moderators_X,22", "22,33,011"):
            with self.subTest(chat=chat):
                form = shop_form(chat)
                self.assertFalse(form.is_valid())
                self.assertEqual(form.errors["telegram_chat"], ["This chat cannot receive the site's messages."])

    def test_too_long_is_refused(self):
        form = shop_form(",".join(["@" + "a" * 60] * 2 + ["22"]))
        self.assertFalse(form.is_valid())
        self.assertIn("telegram_chat", form.errors)

    def test_the_admin_form_checks_how_many_and_how_each_is_written(self):
        for chat, valid in (("22,33,44", True), ("22,33,44,55", False), ("22,abc", False)):
            with self.subTest(chat=chat):
                form = ShopAdminForm({"name": "Bar", "telegram_chat": chat, "discount": 0})
                form.is_valid()
                self.assertEqual("telegram_chat" not in form.errors, valid)


@override_settings(
    LANGUAGE_CODE="en", TELEGRAM_BOT_TOKEN="1:x", TELEGRAM_MODERATION_CHAT="-100", PUBLIC_URL="https://site.example.org"
)
class GuardTellsEachChat(TestCase):
    """shops/guard.py writes to each chat of a shop on its own, so that «told» means that every one of them took it."""

    def setUp(self):
        self.owner = User.objects.create(username="key1")
        self.shop = Shop.objects.create(owner=self.owner, name="Bar", telegram_chat="111,@bar_chat")
        patcher = patch("shops.guard.telegram", return_value=True)
        self.telegram = patcher.start()
        self.addCleanup(patcher.stop)

    def change(self):
        with self.captureOnCommitCallbacks(execute=True), in_admin():
            self.owner.username = "other"
            self.owner.save()
        return AccountChange.objects.get()

    def test_the_chats_of_a_shop_are_told_one_by_one(self):
        self.assertEqual(guard.owned_chats([self.shop]), ["111", "@bar_chat"])
        row = self.change()
        self.assertEqual([call.args[0] for call in self.telegram.call_args_list], ["-100", "111", "@bar_chat"])
        self.assertEqual((row.told, row.not_told), ("telegram,association", ""))

    def test_one_chat_that_refuses_makes_the_channel_not_told(self):
        self.telegram.side_effect = lambda chat, text: chat != "@bar_chat"
        row = self.change()
        self.assertEqual((row.told, row.not_told), ("association", "telegram"))

    def test_a_changed_chat_is_told_to_each_chat_it_left(self):
        with self.captureOnCommitCallbacks(execute=True), in_admin():
            self.shop.telegram_chat = "999"
            self.shop.save()
        row = AccountChange.objects.get()
        self.assertEqual(row.changed, "shop_chat")
        self.assertEqual(sorted(call.args[0] for call in self.telegram.call_args_list), ["-100", "111", "@bar_chat"])
        self.assertNotIn("999", "".join(str(call) for call in self.telegram.call_args_list))

    def test_the_limit_counts_chats_not_shops(self):
        Shop.objects.create(owner=self.owner, name="Pub", telegram_chat="222,333,444")
        Shop.objects.create(owner=self.owner, name="Inn", telegram_chat="555")
        row = self.change()
        # Five chats of six are written to, the sixth never: the channel is not told
        self.assertEqual(len(self.telegram.call_args_list), 1 + guard.MAX_CHATS)
        self.assertEqual((row.told, row.not_told), ("association", "telegram"))

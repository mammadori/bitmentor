"""The owner is told when the shop is approved (shops/approval.py, tell_owner): by email and in the shop's Telegram chats,
once per approval, after the commit, never blocking or breaking the approval."""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core import mail
from django.db import transaction
from django.test import TestCase, override_settings

from config.community import community

from . import approval
from .models import DEMO_PREFIX, Profile, Shop
from .notifications import NotificationFailed


def approval_off():
    return patch("config.features.community", new=lambda: {**community(), "features": {"approval": False}})


@override_settings(TELEGRAM_BOT_TOKEN="", TELEGRAM_MODERATION_CHAT="", PUBLIC_URL="https://example.org")
class Notice(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create(username="owner")
        Profile.objects.create(user=self.owner, email="mario@example.org")
        self.shop = Shop.objects.create(name="Bar Sport", owner=self.owner, latitude=41.9, longitude=12.5,
                                        lightning_address="mario@wallet.example", approved_on=None)
        self.staff = get_user_model().objects.create(username="staff", is_staff=True)

    def approve(self, **kwargs):
        with self.captureOnCommitCallbacks(execute=True):
            if "by" in kwargs:
                return approval.approve_or_hold(Shop.objects.filter(pk=self.shop.pk), None, **kwargs)
            return approval.approve_or_hold(Shop.objects.filter(pk=self.shop.pk), self.staff, **kwargs)

    def admin(self):
        from config.urls import ADMIN_SESSION

        self.staff.user_permissions.add(*Permission.objects.filter(content_type__app_label="shops",
                                                                   codename__in=("view_shop", "change_shop")))
        self.client.force_login(self.staff)
        session = self.client.session
        session[ADMIN_SESSION] = True
        session.save()

    def test_an_email_with_the_links_and_no_address(self):
        self.approve()
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["mario@example.org"])
        self.assertIn("Bar Sport is on the map", message.subject)
        self.assertIn(f"https://example.org/en/shops/s/{self.shop.id}/", message.body)
        self.assertIn("https://example.org/en/shops/", message.body)
        self.assertIn("print the sign", message.body)
        self.assertNotIn("mario@wallet.example", message.body + message.subject)

    def test_the_one_click_of_today(self):
        self.admin()
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(f"/admin/today/approve/{self.shop.id}/")
        self.assertEqual(len(mail.outbox), 1)

    def test_the_admin_action(self):
        self.admin()
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post("/admin/shops/shop/", {"action": "approve_shops", "_selected_action": [str(self.shop.pk)]})
        self.assertEqual(len(mail.outbox), 1)

    def test_an_approval_by_a_team_key(self):
        self.approve(by="[team key 1] Anna")
        self.assertEqual(len(mail.outbox), 1)

    def test_once_only(self):
        self.approve()
        self.approve()
        self.assertEqual(len(mail.outbox), 1)

    def test_nothing_for_a_held_shop(self):
        with patch("shops.approval.approval_signals.asks", return_value=True):
            outcome = self.approve()
        self.assertEqual(outcome.approved, 0)
        self.assertEqual(mail.outbox, [])

    def test_nothing_for_a_rollback(self):
        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    approval.approve_or_hold(Shop.objects.filter(pk=self.shop.pk), self.staff)
                    raise RuntimeError("undo")
            except RuntimeError:
                pass
        self.assertEqual(mail.outbox, [])

    def test_nothing_for_a_stamp(self):
        shop = Shop(name="Imported", owner=self.owner, approved_on=None)
        with self.captureOnCommitCallbacks(execute=True):
            approval.stamp(shop, "import")
            shop.save()
        self.assertEqual(mail.outbox, [])

    def test_nothing_with_the_approval_off(self):
        with approval_off():
            self.approve()
        self.assertEqual(mail.outbox, [])

    def test_nothing_for_a_demo_shop(self):
        Shop.objects.filter(pk=self.shop.pk).update(external_id=f"{DEMO_PREFIX}1")
        self.approve()
        self.assertEqual(mail.outbox, [])

    def test_an_owner_without_email_and_chat_hears_nothing(self):
        Profile.objects.filter(user=self.owner).update(email="")
        with patch("shops.notifications.telegram") as telegram:
            self.approve()
        self.assertEqual(mail.outbox, [])
        telegram.assert_not_called()

    def told(self, **change):
        """The email of an approval of the shop changed so."""
        Shop.objects.filter(pk=self.shop.pk).update(**change)
        self.approve()
        self.assertEqual(len(mail.outbox), 1)
        return mail.outbox[0]

    def assert_only_approved(self, message):
        self.assertIn("Bar Sport is approved", message.subject)
        self.assertNotIn("on the map now", message.body)

    def test_without_a_position_only_approved_and_how_to_put_it_there(self):
        message = self.told(latitude=None, longitude=None)
        self.assert_only_approved(message)
        self.assertIn("as soon as its position is in its details", message.body)
        self.assertIn("print the sign", message.body)

    def test_waiting_for_the_wallet_no_sign_and_no_page(self):
        message = self.told(lightning_address="", awaiting_wallet=True)
        self.assert_only_approved(message)
        self.assertIn("as soon as you add the wallet", message.body)
        self.assertNotIn("print the sign", message.body)
        self.assertNotIn(f"/shops/s/{self.shop.id}/", message.body)
        self.assertIn("https://example.org/en/shops/", message.body)
        # And indeed the sign would not be served
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(f"/en/shops/sign/{self.shop.id}/").status_code, 404)

    def test_suspended_unlisted_or_closed_only_approved_without_the_sign(self):
        from datetime import timedelta

        from django.utils import timezone

        for change in ({"suspended": True}, {"listed": False}, {"closed_on": timezone.localdate() - timedelta(days=2)}):
            with self.subTest(change):
                mail.outbox.clear()
                Shop.objects.filter(pk=self.shop.pk).update(approved_on=None, suspended=False, listed=True, closed_on=None)
                message = self.told(**change)
                self.assert_only_approved(message)
                self.assertNotIn("as soon as", message.body)
                if "listed" in change:
                    self.assertIn("print the sign", message.body)
                else:
                    self.assertNotIn("print the sign", message.body)

    def test_as_soon_as_only_when_that_is_all_that_is_missing(self):
        wallet = {"lightning_address": "", "awaiting_wallet": True}
        nowhere = {"latitude": None, "longitude": None}
        for change in ({"listed": False, **wallet}, {"suspended": True, **wallet}, {**nowhere, **wallet},
                       {"listed": False, **nowhere}):
            with self.subTest(change):
                mail.outbox.clear()
                Shop.objects.filter(pk=self.shop.pk).update(
                    approved_on=None, suspended=False, listed=True, latitude=41.9, longitude=12.5,
                    lightning_address="mario@wallet.example", awaiting_wallet=False)
                message = self.told(**change)
                self.assert_only_approved(message)
                self.assertNotIn("as soon as", message.body)

    def test_a_failing_mail_server_never_breaks_the_approval(self):
        with patch("config.mail.send_mail", side_effect=OSError("down")), self.assertLogs("shops.approval", "ERROR"):
            outcome = self.approve()
        self.assertEqual(outcome.approved, 1)
        self.shop.refresh_from_db()
        self.assertFalse(self.shop.needs_approval)

    @override_settings(TELEGRAM_BOT_TOKEN="t")
    def test_each_chat_of_the_shop_with_the_name_as_alerts_show_it(self):
        Shop.objects.filter(pk=self.shop.pk).update(telegram_chat="-1, -2", name="Bar @evil www.x.com")
        with patch("shops.notifications.telegram", return_value=True) as telegram:
            self.approve()
        self.assertEqual([call.args[0] for call in telegram.call_args_list], ["-1", "-2"])
        text = telegram.call_args.args[1]
        self.assertIn(f"https://example.org/en/shops/s/{self.shop.id}/", text)
        self.assertNotIn("@evil", text)
        self.assertNotIn("www.x.com", text)
        self.assertNotIn("mario@wallet.example", text)

    @override_settings(TELEGRAM_BOT_TOKEN="t")
    def test_a_failing_telegram_never_breaks_the_approval_nor_the_email(self):
        Shop.objects.filter(pk=self.shop.pk).update(telegram_chat="-1")
        with patch("shops.notifications.telegram", side_effect=NotificationFailed("500")), \
                self.assertLogs("shops.approval", "ERROR"):
            outcome = self.approve()
        self.assertEqual(outcome.approved, 1)
        self.assertEqual(len(mail.outbox), 1)

    @override_settings(MAIL_IN_BACKGROUND=True)
    def test_the_approver_does_not_wait(self):
        with patch("shops.approval.threading.Thread") as thread:
            self.approve()
        thread.assert_called_once()
        self.assertEqual(mail.outbox, [])

    def test_the_communities_signal_is_sent_as_before(self):
        from . import events

        heard = []

        def receiver(sender, shop, by, **kwargs):
            heard.append(by)
        events.shop_approved.connect(receiver)
        self.addCleanup(events.shop_approved.disconnect, receiver)
        self.approve()
        self.assertEqual(heard, ["staff"])

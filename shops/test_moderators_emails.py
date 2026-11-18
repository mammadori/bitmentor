"""`moderators_emails` in community.yml: the moderators' messages by email, when Telegram does not take them, to a few
addresses of their own instead of the community's public `email`."""
from io import StringIO
from smtplib import SMTPException
from unittest.mock import patch

from django.core import mail
from django.core.cache import cache
from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings

from config.community import community

from .apps import wrong_moderators_emails
from .management.commands import preflight
from .notifications import (
    MAX_MODERATORS_EMAILS,
    deliver_to_moderators,
    moderators_emails,
    read_moderators_emails,
)
from .test_dashboard import broken


def configured(**keys):
    """community.yml with these keys, as the site reads it (the cached dict, so that every reader sees them)."""
    return patch.dict(community(), keys)


@override_settings(TELEGRAM_BOT_TOKEN="", TELEGRAM_MODERATION_CHAT="")
class ModeratorsEmails(TestCase):
    def test_without_the_key_the_communitys_email_as_before(self):
        self.assertNotIn("moderators_emails", community())
        self.assertEqual(read_moderators_emails(), ([], []))
        with configured(email="staff@example.org"):
            self.assertEqual(deliver_to_moderators("Subject", "hello"), ("the community's email", []))
        self.assertEqual([message.to for message in mail.outbox], [["staff@example.org"]])

    def test_each_address_gets_a_message_of_its_own_and_the_public_email_none(self):
        with configured(email="public@example.org", moderators_emails=["anna@example.org", " bruno@example.net "]):
            self.assertEqual(deliver_to_moderators("Subject", "hello"), ("the community's email", []))
        # One message each: no address sees the others, and the public one is not written to
        self.assertEqual([message.to for message in mail.outbox], [["anna@example.org"], ["bruno@example.net"]])
        self.assertTrue(all(not message.cc and not message.bcc for message in mail.outbox))

    def test_telegram_first_the_emails_only_when_it_does_not_take_it(self):
        with override_settings(TELEGRAM_BOT_TOKEN="t", TELEGRAM_MODERATION_CHAT="11"), \
                configured(moderators_emails=["anna@example.org"]):
            with patch("shops.notifications.telegram", return_value=True):
                self.assertEqual(deliver_to_moderators("Subject", "hello"), ("TELEGRAM_MODERATION_CHAT", []))
            self.assertEqual(mail.outbox, [])
            with patch("shops.notifications.telegram", return_value=False):
                where, problems = deliver_to_moderators("Subject", "hello")
        self.assertEqual((where, len(problems)), ("the community's email", 1))
        self.assertEqual(mail.outbox[0].to, ["anna@example.org"])

    def test_one_address_that_fails_does_not_stop_the_others_and_says_no_address(self):
        real = mail.get_connection

        def send(subject, text, sender, recipients, kind=""):
            if recipients == ["anna@example.org"]:
                raise SMTPException("anna@example.org refused")
            mail.EmailMessage(subject, text, None, recipients, connection=real()).send()
            return 1

        with configured(moderators_emails=["anna@example.org", "bruno@example.net"]), \
                patch("shops.notifications.send_mail", side_effect=send):
            where, problems = deliver_to_moderators("Subject", "hello")
        self.assertEqual(where, "the community's email")
        self.assertEqual(problems, ["`moderators_emails`: address 1 of 2 failed (SMTPException)"])
        self.assertEqual([message.to for message in mail.outbox], [["bruno@example.net"]])
        # None went: nowhere, and the community's email is not tried behind the moderators' back
        with configured(email="public@example.org", moderators_emails=["anna@example.org"]), \
                patch("shops.notifications.send_mail", side_effect=OSError) as sent:
            where, problems = deliver_to_moderators("Subject", "hello")
        self.assertIsNone(where)
        self.assertEqual(sent.call_count, 1)
        self.assertNotIn("anna", " ".join(problems))

    def test_addresses_that_cannot_be_used_are_left_out_and_checked(self):
        bad = ["not an address", "anna@example.org", "ANNA@example.org", 42, "x@example.org\nBcc: y@example.org",
               "bruno@example.net, eve@example.com", "carla@example.org"]
        with configured(moderators_emails=bad):
            addresses, problems = read_moderators_emails()
            warnings = wrong_moderators_emails(None)
        self.assertEqual(addresses, ["anna@example.org", "carla@example.org"])
        self.assertEqual(len(problems), 5)
        self.assertEqual({warning.id for warning in warnings}, {"shops.W013"})
        # The check says where, never the value: the log of web is no place for the addresses
        self.assertFalse(any("example" in problem for problem in problems))

    def test_no_more_than_the_limit(self):
        many = [f"m{number}@example.org" for number in range(MAX_MODERATORS_EMAILS + 2)]
        with configured(moderators_emails=many):
            addresses, problems = read_moderators_emails()
            deliver_to_moderators("Subject", "hello")
        self.assertEqual(addresses, many[:MAX_MODERATORS_EMAILS])
        self.assertEqual(len(problems), 1)
        self.assertEqual(len(mail.outbox), MAX_MODERATORS_EMAILS)

    def test_a_key_that_is_not_a_list_or_has_nothing_usable_falls_back_to_the_communitys_email(self):
        for value in ("anna@example.org", {"anna": "anna@example.org"}, [], ["nobody"]):
            with self.subTest(value=value), configured(email="public@example.org", moderators_emails=value):
                mail.outbox = []
                self.assertEqual(moderators_emails(), [])
                self.assertIn("the community's `email` is used", wrong_moderators_emails(None)[-1].msg)
                self.assertEqual(deliver_to_moderators("Subject", "hello")[0], "the community's email")
                self.assertEqual([message.to for message in mail.outbox], [["public@example.org"]])

    def test_the_public_email_keeps_its_type_and_meaning(self):
        """`email` stays the address the pages show: the new key changes none of its readers."""
        with configured(email="public@example.org", moderators_emails=["anna@example.org"]):
            page = self.client.get("/en/")
        self.assertContains(page, "public@example.org")
        self.assertNotContains(page, "anna@example.org")


@override_settings(TELEGRAM_BOT_TOKEN="", TELEGRAM_MODERATION_CHAT="", LANGUAGE_CODE="en")
class MorningSummary(TestCase):
    def setUp(self):
        cache.clear()
        broken()

    def send(self):
        out, err = StringIO(), StringIO()
        try:
            call_command("dashboard", "--send", stdout=out, stderr=err)
        except SystemExit:
            pass  # 10: there is something to do
        return out.getvalue(), err.getvalue()

    def test_the_summary_goes_to_each_address(self):
        with configured(email="public@example.org", moderators_emails=["anna@example.org", "bruno@example.net"]):
            out, _err = self.send()
        self.assertIn("Summary sent to the community's email", out)
        self.assertEqual([message.to for message in mail.outbox], [["anna@example.org"], ["bruno@example.net"]])

    def test_a_summary_that_no_address_took_is_tried_again_next_run(self):
        with configured(email="public@example.org", moderators_emails=["anna@example.org"]), \
                patch("shops.management.commands.dashboard.send_mail", side_effect=OSError), \
                self.assertRaises(CommandError) as raised:
            self.send()
        self.assertIn("address 1 of 1 failed (OSError)", str(raised.exception))
        self.assertNotIn("anna", str(raised.exception))


class Preflight(TestCase):
    def test_the_key_counts_as_a_mail_fallback(self):
        on = override_settings(TEAM_ACCESS=True, ADMIN_ALLOWED_IPS="203.0.113.7", TELEGRAM_BOT_TOKEN="",
                               TELEGRAM_MODERATION_CHAT="", EMAIL_HOST="mail.example.org")
        with on, configured(email=""):
            self.assertTrue(any("nobody will be told" in note for note in preflight.team_notes()))
        with on, configured(email="", moderators_emails=["anna@example.org"]):
            self.assertFalse(any("nobody will be told" in note for note in preflight.team_notes()))
        # An address that cannot be used is no fallback
        with on, configured(email="", moderators_emails=["nobody"]):
            self.assertTrue(any("nobody will be told" in note for note in preflight.team_notes()))

"""manage.py notify_test --moderators: one test message, the way the moderators' messages go."""
from io import StringIO
from unittest.mock import patch

from django.conf import settings
from django.core import mail
from django.core.mail.backends.locmem import EmailBackend
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import translation

from config.community import community

TELEGRAM = {"TELEGRAM_BOT_TOKEN": "secret-token", "TELEGRAM_MODERATION_CHAT": "-100111,-100222",
            "EMAIL_HOST": "mail.example.net"}
NO_TELEGRAM = {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_MODERATION_CHAT": "", "EMAIL_HOST": "mail.example.net"}
# The site's own sending, as in production: the counting backend in front of the real one
COUNTED = {"EMAIL_BACKEND": "config.mailbudget.CountingBackend",
           "EMAIL_REAL_BACKEND": "django.core.mail.backends.locmem.EmailBackend"}
# A mail backend of the community (docs/configuration.md), which needs no EMAIL_HOST
COMMUNITY_BACKEND = {**NO_TELEGRAM, "EMAIL_HOST": "", "NETWORK": "signet", "EMAIL_BACKEND": "config.mailbudget.CountingBackend",
                     "EMAIL_REAL_BACKEND": "shops.test_notify_test.CommunityBackend"}


class CommunityBackend(EmailBackend):
    """A community's backend: it sends (here, to the test outbox) without the site's mail server."""


class NotifyTest(TestCase):
    def run_it(self, *args, status=200, **keys):
        """(stdout, stderr, the CommandError or None, the posts to Telegram)."""
        out, errors = StringIO(), StringIO()
        with patch("shops.notifications.requests.post") as post, patch.dict(community(), keys):
            post.return_value.status_code = status
            try:
                call_command("notify_test", *args, stdout=out, stderr=errors)
                error = None
            except CommandError as raised:
                error = raised
        return out.getvalue(), errors.getvalue(), error, post

    def assertNoSecrets(self, *texts):
        for text in texts:
            for secret in ("secret-token", "-100111", "-100222", "staff@example.org", "anna@example.org"):
                self.assertNotIn(secret, str(text))

    @override_settings(**TELEGRAM)
    def test_with_telegram_every_chat_of_the_list(self):
        out, errors, error, post = self.run_it("--moderators", email="staff@example.org")
        self.assertIsNone(error)
        self.assertEqual(out, "sent to: TELEGRAM_MODERATION_CHAT\n")
        self.assertEqual(errors, "")
        self.assertEqual([call.kwargs["json"]["chat_id"] for call in post.call_args_list], ["-100111", "-100222"])
        text = post.call_args.kwargs["json"]["text"]
        self.assertIn(settings.SITE_HOST, text)
        self.assertIn(community()["name"], text)
        self.assertIn("test message", text)
        self.assertEqual(mail.outbox, [])
        self.assertNoSecrets(out, errors)

    @override_settings(**TELEGRAM)
    def test_telegram_refusing_falls_back_on_the_mail(self):
        out, errors, error, _post = self.run_it("--moderators", status=403, email="staff@example.org")
        self.assertIsNone(error)
        self.assertEqual(out, "sent to: the community's email\n")
        self.assertIn("Telegram refused it", errors)
        self.assertEqual([message.to for message in mail.outbox], [["staff@example.org"]])
        self.assertNoSecrets(out, errors)

    @override_settings(**NO_TELEGRAM)
    def test_with_mail_only(self):
        out, errors, error, post = self.run_it("--moderators", email="staff@example.org",
                                               moderators_emails=["anna@example.org"])
        self.assertIsNone(error)
        self.assertEqual(out, "sent to: the community's email\n")
        post.assert_not_called()
        # The path of the real messages: `moderators_emails` before the community's `email`
        self.assertEqual([message.to for message in mail.outbox], [["anna@example.org"]])
        self.assertIn("test message", mail.outbox[0].body)
        self.assertIn("Test message", mail.outbox[0].subject)
        self.assertNoSecrets(out, errors)

    @override_settings(**NO_TELEGRAM)
    def test_with_nothing_it_fails(self):
        out, errors, error, _post = self.run_it("--moderators", email="")
        self.assertIsNotNone(error)
        self.assertNotEqual(error.returncode, 0)
        self.assertIn("Nothing was delivered", str(error))
        self.assertIn("no `email` in community.yml", errors)
        self.assertEqual(out, "")
        self.assertEqual(mail.outbox, [])

    @override_settings(**NO_TELEGRAM)
    def test_a_mail_server_that_fails_is_said_without_the_address(self):
        with patch("shops.notifications.send_mail", side_effect=OSError):
            out, errors, error, _post = self.run_it("--moderators", email="staff@example.org")
        self.assertIsNotNone(error)
        self.assertIn("the community's email failed (OSError)", errors)
        self.assertNoSecrets(out, errors, error)

    @override_settings(**{**NO_TELEGRAM, "EMAIL_HOST": "", "NETWORK": "signet"})
    def test_mail_written_only_to_the_log_is_not_delivered(self):
        _out, errors, error, _post = self.run_it("--moderators", email="staff@example.org")
        self.assertIsNotNone(error)
        self.assertIn("did not leave this machine", errors)

    @override_settings(**TELEGRAM)
    def test_without_an_audience_it_explains_and_exits_2(self):
        _out, _errors, error, post = self.run_it()
        self.assertEqual(error.returncode, 2)
        self.assertIn("--moderators", str(error))
        post.assert_not_called()
        self.assertEqual(mail.outbox, [])

    @override_settings(**TELEGRAM, LANGUAGE_CODE="it")
    def test_in_the_communitys_first_language(self):
        """The first language of community.yml (LANGUAGE_CODE), whatever language is active."""
        with translation.override("de"):
            post = self.run_it("--moderators")[3]
        self.assertIn("Questo è un messaggio di prova", post.call_args.kwargs["json"]["text"])

    @override_settings(**NO_TELEGRAM, **COUNTED)
    def test_a_mail_kept_back_by_the_budget_is_not_sent(self):
        """The counting backend drops it without an error: «sent» would be the false positive this command is for."""
        with patch("config.mailbudget.blocked", return_value="limits.emails_per_month"):
            out, errors, error, _post = self.run_it("--moderators", email="staff@example.org")
        self.assertIsNotNone(error)
        self.assertNotEqual(error.returncode, 0)
        self.assertIn("the email budget kept it back", errors)
        self.assertNotIn("sent to", out)
        self.assertEqual(mail.outbox, [])

    @override_settings(**COMMUNITY_BACKEND)
    def test_a_community_mail_backend_sends_without_email_host(self):
        out, errors, error, _post = self.run_it("--moderators", email="staff@example.org")
        self.assertIsNone(error, errors)
        self.assertEqual(out, "sent to: the community's email\n")
        self.assertEqual([message.to for message in mail.outbox], [["staff@example.org"]])


class MailLeaves(TestCase):
    """config.mail.leaves: whether an email reaches a mailbox, the one answer preflight and notify_test share."""
    def test_which_backends_send(self):
        from config.mail import leaves

        smtp, console = "django.core.mail.backends.smtp.EmailBackend", "django.core.mail.backends.console.EmailBackend"
        for backend, host, expected in ((smtp, "mail", True), (smtp, "", False), (console, "mail", True),
                                        (console, "", False), ("shops.test_notify_test.CommunityBackend", "", True),
                                        ("django.core.mail.backends.locmem.EmailBackend", "mail", False),
                                        ("django.core.mail.backends.dummy.EmailBackend", "", False)):
            with self.subTest(backend=backend, host=host), override_settings(EMAIL_REAL_BACKEND=backend, EMAIL_HOST=host):
                self.assertIs(leaves(), expected)

"""Every email has an HTML copy with real links (config/mail.py)."""

from django.core import mail
from django.test import SimpleTestCase, TestCase, override_settings

from config.mail import html, send_mail


class Html(SimpleTestCase):
    def test_an_address_alone_is_a_button_and_one_in_a_sentence_a_link(self):
        page = html(
            "Take your shop:\nhttps://example.org/it/shops/claim/?code=ab-12&x=1\n\nOr open https://example.org/a."
        )
        self.assertIn('<a href="https://example.org/it/shops/claim/?code=ab-12&amp;x=1" style=', page)
        self.assertIn('<a href="https://example.org/a">https://example.org/a</a>.', page)

    def test_nothing_written_becomes_markup(self):
        page = html('Shop <b>"x"</b> https://example.org/"><script>')
        self.assertNotIn("<b>", page)
        self.assertNotIn("<script>", page)
        self.assertIn('<a href="https://example.org/">', page)

    def test_no_remote_content(self):
        self.assertNotRegex(html("Hi https://example.org/"), r"<img|src=|<link|<script")


class Sent(TestCase):
    def test_plain_text_as_always_plus_the_html_with_the_same_link(self):
        self.assertEqual(send_mail("S", "Open:\nhttps://example.org/x?code=1", None, ["a@example.org"]), 1)
        sent = mail.outbox[0]
        self.assertEqual(sent.body, "Open:\nhttps://example.org/x?code=1")
        content, kind = sent.alternatives[0]
        self.assertEqual(kind, "text/html")
        self.assertIn('href="https://example.org/x?code=1"', content)


class EveryMail(SimpleTestCase):
    def test_every_sender_uses_the_one_with_the_html(self):
        import importlib
        import pathlib

        import config.mail

        root = pathlib.Path(config.mail.__file__).resolve().parent.parent
        for path in root.rglob("*.py"):
            if ".venv" in path.parts or path.name.startswith("test") or "migrations" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("from django.core.mail import send_mail", text, path)
            self.assertNotIn("send_mass_mail", text, path)
        views = importlib.import_module("shops.views")
        # The communities' own code sends from here too, and patches these names in its tests: they stay
        self.assertIs(views.send_mail, config.mail.send_mail)
        self.assertIs(views.send_later, config.mail.send_later)


class Parentheses(SimpleTestCase):
    def test_a_closing_parenthesis_of_the_address_stays_one_of_a_sentence_goes(self):
        self.assertIn('href="https://a.example/x_(y)"', html("See https://a.example/x_(y) now"))
        self.assertIn('href="https://a.example/x"', html("(see https://a.example/x)"))


class Senders(SimpleTestCase):
    @override_settings(MERCHANT_FROM_EMAIL="")
    def test_with_no_merchant_sender_the_default_one(self):
        from config.mail import merchant_sender

        self.assertIsNone(merchant_sender())

    @override_settings(
        MERCHANT_FROM_EMAIL="Shops <shops@example.org>",
        DEFAULT_FROM_EMAIL="noreply@example.org",
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    )
    def test_the_owner_gets_the_shops_address_the_links_the_noreply_one(self):
        from django.core import mail

        from config.mail import merchant_sender, send_mail

        send_mail("a", "b", merchant_sender(), ["o@example.org"])
        send_mail("c", "d", None, ["o@example.org"])
        self.assertEqual([m.from_email for m in mail.outbox], ["Shops <shops@example.org>", "noreply@example.org"])

    def test_the_mails_to_the_owner_use_the_merchant_sender(self):
        from pathlib import Path

        for name in ("guard.py", "lightning_watch.py", "vacation.py"):
            self.assertIn("merchant_sender()", (Path(__file__).parent.parent / "shops" / name).read_text(), name)

    @override_settings(
        EMAIL_ENVELOPE_FROM="shops@example.org",
        DEFAULT_FROM_EMAIL="Valley (automatic) <shops@example.org>",
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    )
    def test_the_envelope_is_its_own_address_the_from_the_one_shown(self):
        from django.core import mail

        send_mail("a", "b", None, ["o@example.org"])
        sent = mail.outbox[-1]
        self.assertEqual(sent.from_email, "shops@example.org")
        # the whole name reaches the reader: in quotes, or the part in brackets would be a comment and left out
        self.assertEqual(sent.message()["From"], '"Valley (automatic)" <shops@example.org>')

    def test_a_sender_keeps_its_whole_name(self):
        from config.mail import sender

        for written, sent in (
            ("Shops (automatic) <shops@example.org>", '"Shops (automatic)" <shops@example.org>'),
            ('"Shops (automatic)" <shops@example.org>', '"Shops (automatic)" <shops@example.org>'),
            ("Shops: news <shops@example.org>", '"Shops: news" <shops@example.org>'),
            ("Bar, Caffè & Co <shops@example.org>", '"Bar, Caffè & Co" <shops@example.org>'),
            ("Shops <shops@example.org>", "Shops <shops@example.org>"),
            ("shops@example.org", "shops@example.org"),
            ("<shops@example.org>", "<shops@example.org>"),
            ("", ""),
        ):
            self.assertEqual(sender(written), sent, written)
        with self.settings(
            DEFAULT_FROM_EMAIL="Shops (automatic) <shops@example.org>",
            EMAIL_ENVELOPE_FROM="",
            EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        ):
            from django.core import mail

            send_mail("a", "b", None, ["o@example.org"])
            self.assertEqual(mail.outbox[-1].message()["From"], '"Shops (automatic)" <shops@example.org>')


class Kinds(SimpleTestCase):
    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_each_email_says_its_kind_to_the_backend(self):
        from django.core import mail

        send_mail("a", "b", None, ["o@example.org"], kind="link")
        self.assertEqual(mail.outbox[-1].kind, "link")

    def test_every_sender_says_a_known_kind(self):
        import re
        from pathlib import Path

        from config.mail import KINDS

        root = Path(__file__).parent.parent
        calls = [
            call
            for path in root.glob("shops/**/*.py")
            if not path.name.startswith("test")
            for call in re.findall(r"send_mail\((.*)", path.read_text())
        ]
        self.assertTrue(calls)
        for call in calls:
            kind = re.search(r'kind="(\w+)"', call)
            self.assertTrue(kind and kind.group(1) in KINDS, call)


@override_settings(MAIL_IN_BACKGROUND=True, EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class Later(SimpleTestCase):
    """The emails that leave after the page has answered (send_later). No database here: the thread has its own connections."""

    def wait(self):
        import threading

        for thread in threading.enumerate():
            if thread.name == "mail":
                thread.join(5)
                self.assertFalse(thread.is_alive())

    def free(self):
        import config.mail

        taken = 0
        while config.mail.WAITING.acquire(blocking=False):
            taken += 1
        for _ in range(taken):
            config.mail.WAITING.release()
        return taken

    def test_the_caller_does_not_wait_for_the_mail_server(self):
        import threading
        from unittest.mock import patch

        import config.mail

        hold, started, callers = threading.Event(), threading.Event(), []

        def slow(*args, **kwargs):
            callers.append(threading.current_thread().name)
            started.set()
            hold.wait(5)
            return 1

        with patch("config.mail.send_mail", side_effect=slow):
            config.mail.send_later("Subject", "Text", ["a@example.org"], kind="link")
            # Back here while the mail server still has not answered
            self.assertTrue(started.wait(5))
            self.assertEqual([thread.is_alive() for thread in threading.enumerate() if thread.name == "mail"], [True])
            hold.set()
            self.wait()
        self.assertEqual(callers, ["mail"])
        self.assertEqual(self.free(), 8)

    def test_it_arrives_with_its_html_and_kind(self):
        import config.mail

        mail.outbox = []
        config.mail.send_later("Subject", "Open https://example.org/x", ["a@example.org"], kind="link")
        self.wait()
        self.assertEqual([(m.subject, m.to, m.kind) for m in mail.outbox], [("Subject", ["a@example.org"], "link")])
        self.assertIn('href="https://example.org/x"', mail.outbox[0].alternatives[0][0])

    def test_one_that_does_not_leave_is_an_error_in_the_log_and_is_told_to_the_caller(self):
        from smtplib import SMTPDataError, SMTPRecipientsRefused
        from unittest.mock import patch

        import config.mail

        # A mail server's refusal quotes the address, and so does Django's «Invalid address»
        for refusal in (
            {"side_effect": OSError("no route to a@example.org")},
            {"side_effect": SMTPRecipientsRefused({"a@example.org": (550, b"<a@example.org>: user unknown")})},
            {"side_effect": SMTPDataError(554, b"a@example.org refused")},
            {"side_effect": ValueError("Invalid address a@example.org")},
            {"return_value": 0},
        ):
            told = []
            with patch("config.mail.send_mail", **refusal), self.assertLogs("config.mail", "ERROR") as log:
                config.mail.send_later(
                    "Subject", "Text", ["a@example.org"], kind="link", failed=lambda told=told: told.append(1)
                )
                self.wait()
            self.assertEqual((len(log.output), told), (1, [1]), refusal)
            # The log says which email, not to whom
            self.assertIn("Subject", log.output[0])
            self.assertNotIn("a@example.org", "\n".join(log.output))
            self.assertEqual(self.free(), 8)

    def test_what_follows_a_failure_cannot_break_the_thread(self):
        from unittest.mock import patch

        import config.mail

        def broken():
            raise RuntimeError

        with patch("config.mail.send_mail", return_value=0), self.assertLogs("config.mail", "ERROR") as log:
            config.mail.send_later("Subject", "Text", ["a@example.org"], failed=broken)
            self.wait()
        self.assertEqual(len(log.output), 2)
        self.assertEqual(self.free(), 8)

    def test_with_too_many_on_their_way_the_request_sends_its_own(self):
        import threading
        from unittest.mock import patch

        import config.mail

        callers = []
        for _ in range(8):
            config.mail.WAITING.acquire()
        try:
            with patch(
                "config.mail.send_mail",
                side_effect=lambda *a, **k: callers.append(threading.current_thread().name) or 1,
            ):
                config.mail.send_later("Subject", "Text", ["a@example.org"])
        finally:
            for _ in range(8):
                config.mail.WAITING.release()
        self.assertEqual(callers, [threading.current_thread().name])

    def test_the_text_is_fixed_in_the_request_s_language(self):
        """The thread has no active language: a lazy text must be read before it starts."""
        from unittest.mock import patch

        from django.utils.translation import gettext_lazy, override

        import config.mail

        seen = []
        with patch("config.mail.send_mail", side_effect=lambda subject, *a, **k: seen.append(subject) or 1):
            with override("it"):
                config.mail.send_later(gettext_lazy("Today"), "Text", ["a@example.org"])
            self.wait()
        self.assertEqual(seen, ["Oggi"])

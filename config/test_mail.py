"""Every email has an HTML copy with real links (config/mail.py)."""
from django.core import mail
from django.test import SimpleTestCase, TestCase, override_settings

from config.mail import html, send_mail


class Html(SimpleTestCase):
    def test_an_address_alone_is_a_button_and_one_in_a_sentence_a_link(self):
        page = html("Take your shop:\nhttps://example.org/it/shops/claim/?code=ab-12&x=1\n\nOr open https://example.org/a.")
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
        self.assertIs(importlib.import_module("shops.views").send_mail, config.mail.send_mail)


class Parentheses(SimpleTestCase):
    def test_a_closing_parenthesis_of_the_address_stays_one_of_a_sentence_goes(self):
        self.assertIn('href="https://a.example/x_(y)"', html("See https://a.example/x_(y) now"))
        self.assertIn('href="https://a.example/x"', html("(see https://a.example/x)"))


class Senders(SimpleTestCase):
    @override_settings(MERCHANT_FROM_EMAIL="")
    def test_with_no_merchant_sender_the_default_one(self):
        from config.mail import merchant_sender
        self.assertIsNone(merchant_sender())

    @override_settings(MERCHANT_FROM_EMAIL="Shops <shops@example.org>", DEFAULT_FROM_EMAIL="noreply@example.org",
                       EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
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

    @override_settings(EMAIL_ENVELOPE_FROM="shops@example.org", DEFAULT_FROM_EMAIL="Valley (automatic) <shops@example.org>",
                       EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_the_envelope_is_its_own_address_the_from_the_one_shown(self):
        from django.core import mail
        send_mail("a", "b", None, ["o@example.org"])
        sent = mail.outbox[-1]
        self.assertEqual(sent.from_email, "shops@example.org")
        self.assertEqual(sent.message()["From"], "Valley (automatic) <shops@example.org>")


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
        calls = [call for path in root.glob("shops/**/*.py") if not path.name.startswith("test")
                 for call in re.findall(r"send_mail\((.*)", path.read_text())]
        self.assertTrue(calls)
        for call in calls:
            kind = re.search(r'kind="(\w+)"', call)
            self.assertTrue(kind and kind.group(1) in KINDS, call)

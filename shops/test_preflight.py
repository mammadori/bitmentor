"""manage.py preflight: every point of the "before mainnet" list that can be checked from the machine stops it."""
import smtplib
from io import StringIO
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings


COMMAND = "shops.management.commands.preflight"


class Preflight(TestCase):
    GOOD = {"SITE_SCHEME": "https", "NETWORK": "mainnet", "DEBUG": False, "SECRET_KEY": "x" * 60, "EMAIL_HOST": "mail",
            "DEFAULT_FROM_EMAIL": "Community <noreply@community.example.net>", "SECURE_HSTS_SECONDS": 31536000}

    def run_it(self, mail=None, aaaa=(), **changes):
        """The errors written by the command, one per line; empty if everything is ready."""
        connection = mail or MagicMock()
        with override_settings(**{**self.GOOD, **changes}), \
                patch(f"{COMMAND}.get_connection", return_value=connection) as get_connection, \
                patch(f"{COMMAND}.site_ipv6", return_value=set(aaaa)):
            self.get_connection = get_connection
            try:
                call_command("preflight", stderr=(errors := StringIO()), stdout=StringIO())
            except CommandError:
                self.assertTrue(errors.getvalue())
            else:
                self.assertEqual(errors.getvalue(), "")
        return errors.getvalue().splitlines()

    def assertStops(self, mentions, **kwargs):
        errors = self.run_it(**kwargs)
        self.assertTrue(any(mentions in line for line in errors), errors)

    def test_a_ready_install_passes(self):
        self.assertEqual(self.run_it(), [])

    def test_each_missing_piece_stops_it(self):
        for change in ({"NETWORK": "signet"}, {"SECRET_KEY": "short"}, {"EMAIL_HOST": ""}, {"SECURE_HSTS_SECONDS": 86400},
                       {"SECURE_HSTS_SECONDS": 31535999}, {"DEBUG": True}):
            with self.subTest(change):
                self.assertEqual(len(self.run_it(**change)), 1)

    def test_debug_is_named(self):
        self.assertStops("DEBUG", DEBUG=True)

    def test_hsts_of_a_year_or_more_passes(self):
        self.assertEqual(self.run_it(SECURE_HSTS_SECONDS=63072000), [])

    # Mail

    def test_mail_from_the_default_address_stops_it(self):
        for sender in ("noreply@localhost", "", "noreply@example.org", "no-at-sign"):
            with self.subTest(sender):
                self.assertStops("DEFAULT_FROM_EMAIL", DEFAULT_FROM_EMAIL=sender)

    def test_a_mail_server_that_refuses_stops_it(self):
        for error in (smtplib.SMTPAuthenticationError(535, b"no"), ConnectionRefusedError(), TimeoutError()):
            with self.subTest(error):
                connection = MagicMock()
                connection.open.side_effect = error
                self.assertStops(type(error).__name__, mail=connection)

    def test_the_mail_check_connects_and_sends_nothing(self):
        connection = MagicMock()
        self.run_it(mail=connection)
        self.get_connection.assert_called_once_with(fail_silently=False)
        connection.open.assert_called_once_with()
        connection.close.assert_called_once_with()
        connection.send_messages.assert_not_called()

    def test_without_a_mail_server_no_connection_is_tried(self):
        self.assertEqual(len(self.run_it(EMAIL_HOST="")), 1)
        self.get_connection.assert_not_called()

    def test_every_problem_is_listed_at_once(self):
        errors = self.run_it(NETWORK="signet", DEBUG=True, EMAIL_HOST="")
        self.assertEqual(len(errors), 3, errors)


class IPv6(TestCase):
    """An AAAA record with a Docker network without IPv6: every IPv6 visitor would come from the gateway."""
    GOOD, run_it, assertStops = Preflight.GOOD, Preflight.run_it, Preflight.assertStops
    LOOPBACK = "00000000000000000000000000000001 01 80 10 80       lo\n"
    LINK = "fe8000000000000000420afffe000002 0c 40 20 80     eth0\n"
    ULA = "fd123456789a00010000000000000003 0c 40 00 00     eth0\n"

    def interfaces(self, text):
        path = MagicMock()
        path.read_text.return_value = text
        return path

    def aaaa(self, *addresses):
        return patch(f"{COMMAND}.socket.getaddrinfo", return_value=[(10, 1, 6, "", (a, 443, 0, 0)) for a in addresses])

    def test_own_ipv6_leaves_out_loopback_and_link_local(self):
        from .management.commands.preflight import own_ipv6
        self.assertFalse(own_ipv6(self.interfaces(self.LOOPBACK + self.LINK)))
        self.assertTrue(own_ipv6(self.interfaces(self.LOOPBACK + self.LINK + self.ULA)))
        missing = MagicMock()
        missing.read_text.side_effect = FileNotFoundError
        self.assertIsNone(own_ipv6(missing))

    def test_only_public_addresses_of_the_name_count(self):
        from .management.commands.preflight import site_ipv6
        with self.aaaa("::1", "fe80::1%eth0", "fd00::5", "2a01:4f8::1"):
            self.assertEqual(site_ipv6("website.example.net"), {"2a01:4f8::1"})
        with patch(f"{COMMAND}.socket.getaddrinfo", side_effect=OSError("no AAAA")):
            self.assertEqual(site_ipv6("website.example.net"), set())

    def test_an_aaaa_record_without_ipv6_on_the_network_stops_it(self):
        with patch(f"{COMMAND}.own_ipv6", return_value=False):
            self.assertStops("compose.ipv6.yml", aaaa=["2a01:4f8::1"])
            self.assertStops("ip6tables", aaaa=["2a01:4f8::1"])

    def test_with_ipv6_on_the_network_or_without_aaaa_it_passes(self):
        for addresses, own in ((("2a01:4f8::1",), True), ((), False), (("2a01:4f8::1",), None)):
            with self.subTest(addresses=addresses, own=own), patch(f"{COMMAND}.own_ipv6", return_value=own):
                self.assertEqual(self.run_it(aaaa=addresses), [])

    def test_behind_another_proxy_the_aaaa_record_is_the_proxys(self):
        with patch(f"{COMMAND}.own_ipv6", return_value=False):
            self.assertEqual(self.run_it(aaaa=["2a01:4f8::1"], SITE_SCHEME="http"), [])


class PrivacyPageNote(TestCase):
    def test_placeholders_in_the_privacy_page_are_a_note_not_an_error(self):
        from shops.management.commands.preflight import privacy_notes
        notes = privacy_notes()  # the example community ships the template
        self.assertEqual(len(notes), 1)
        self.assertIn("docs/privacy.md", notes[0])

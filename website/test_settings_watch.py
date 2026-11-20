"""The settings watch (website/settings_watch.py): the moderators hear when the site starts with a security setting
different from the previous start."""

import runpy
from datetime import timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.core import mail
from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from config.community import community
from shops.management.commands import preflight

from . import settings_watch as watch
from .apps import wrong_settings_watch
from .models import SettingsWatch, WatchedSetting

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "123456:secret-token-of-the-bot"
KEY = "the-secret-key-of-the-site-long-enough-for-anything-at-all"
# A site with a moderators' chat, as the tests start it
CHANNEL = {
    "TELEGRAM_BOT_TOKEN": TOKEN,
    "TELEGRAM_MODERATION_CHAT": "11",
    "SECRET_KEY": KEY,
    "ADMIN_SECOND_FACTOR": "passkey",
    "ADMIN_ALLOWED_IPS": "192.0.2.0/24",
    "DEBUG": False,
}


def configured(**keys):
    return patch.dict(community(), keys)


class Base(TestCase):
    def setUp(self):
        self.telegram = patch("shops.notifications.telegram", return_value=True).start()
        self.addCleanup(patch.stopall)

    def compare(self, **changed):
        with override_settings(**{**CHANNEL, **changed}):
            return watch.compare()

    def sent(self):
        """The texts that went to Telegram, every chat."""
        return [call.args[1] for call in self.telegram.call_args_list]


class FirstStartAndChanges(Base):
    def test_the_first_start_records_and_tells_nothing(self):
        self.assertEqual(self.compare(), "recorded")
        self.assertEqual(self.sent(), [])
        self.assertEqual(WatchedSetting.objects.count(), len(watch.WATCHED))
        self.assertEqual(json_of("ADMIN_SECOND_FACTOR"), '"passkey"')
        self.assertIsNone(watch.last_told())
        self.assertEqual(self.compare(), "unchanged")
        self.assertEqual(self.sent(), [])

    def test_a_change_is_told_once_with_before_and_after(self):
        self.compare()
        self.assertEqual(self.compare(TEAM_AREA=True), "told")
        self.assertEqual(len(self.sent()), 1)
        text = self.sent()[0]
        self.assertIn("⚠ lowered: TEAM_AREA: off → on", text)
        self.assertIn("If nobody of the team did this, look at the machine's .env.", text)
        self.assertEqual(self.telegram.call_args.args[0], "11")
        self.assertIsNotNone(watch.last_told())
        # Told: the next start with the same settings says nothing
        self.assertEqual(self.compare(TEAM_AREA=True), "unchanged")
        self.assertEqual(len(self.sent()), 1)
        # And going back is a change too
        self.assertEqual(self.compare(), "told")
        self.assertIn("· TEAM_AREA: on → off", self.sent()[1])

    def test_lowerings_come_first_and_are_marked(self):
        self.compare()
        self.compare(
            TEAM_AREA=True,
            ADMIN_SECOND_FACTOR="optional",
            ADMIN_ALLOWED_IPS="",
            DEBUG=True,
            ADMIN_SESSION_HOURS=48,
            SITE_MODE="maintenance",
            ADMIN_WALLET_SIGN_IN=True,
            TEAM_ACCESS=True,
            REFUSE_PROBES="off",
            TRUSTED_PROXIES="10.0.0.0/8",
        )
        lines = self.sent()[0].splitlines()
        changes = [line for line in lines if line.startswith(("⚠", "·"))]
        self.assertEqual(
            changes,
            [
                "⚠ lowered: ADMIN_SECOND_FACTOR: passkey → optional",
                "⚠ lowered: ADMIN_ALLOWED_IPS: removed: 1, now empty",
                "⚠ lowered: ADMIN_SESSION_HOURS: 24 → 48",
                "⚠ lowered: ADMIN_WALLET_SIGN_IN: off → on",
                "⚠ lowered: TEAM_ACCESS: off → on",
                "⚠ lowered: TEAM_AREA: off → on",
                "⚠ lowered: REFUSE_PROBES: on → off",
                "⚠ lowered: TRUSTED_PROXIES: 127.0.0.1/32 → 10.0.0.0/8",
                "⚠ lowered: DEBUG: off → on",
                "· SITE_MODE: development → maintenance",
            ],
        )

    def test_the_way_back_up_is_not_a_lowering(self):
        self.compare(
            ADMIN_SESSION_HOURS=48,
            ADMIN_WALLET_SIGN_IN=True,
            TEAM_ACCESS=True,
            REFUSE_PROBES="off",
            TRUSTED_PROXIES="10.0.0.0/8",
        )
        self.compare(ADMIN_SESSION_HOURS=8, TRUSTED_PROXIES="")
        self.assertNotIn("⚠", self.sent()[0])
        self.assertIn("· ADMIN_SESSION_HOURS: 48 → 8", self.sent()[0])
        self.assertIn("· TRUSTED_PROXIES: 10.0.0.0/8 → (empty)", self.sent()[0])
        self.assertIn("· REFUSE_PROBES: off → on", self.sent()[0])

    def test_raising_is_not_a_lowering(self):
        self.compare(ADMIN_SECOND_FACTOR="optional", ADMIN_ALLOWED_IPS="")
        self.compare(ADMIN_SECOND_FACTOR="required", ADMIN_ALLOWED_IPS="192.0.2.1")
        self.assertNotIn("⚠", self.sent()[0])
        self.compare(ADMIN_SECOND_FACTOR="optional", ADMIN_ALLOWED_IPS="192.0.2.1")
        self.assertIn("⚠ lowered: ADMIN_SECOND_FACTOR: required → optional", self.sent()[1])

    def test_the_addresses_of_who_reaches_the_admin_are_counted_never_shown(self):
        """Every moderator reads the message, and those addresses are people's homes."""
        self.compare(ADMIN_ALLOWED_IPS="192.0.2.7 198.51.100.0/24", DEV_ALLOWED_IPS="203.0.113.9")
        self.compare(ADMIN_ALLOWED_IPS="192.0.2.8, 198.51.100.0/24 2001:db8::1", DEV_ALLOWED_IPS="")
        text = "\n".join(self.sent())
        self.assertIn("· ADMIN_ALLOWED_IPS: removed: 1, added: 2", text)
        self.assertIn("· DEV_ALLOWED_IPS: removed: 1, now empty", text)
        for address in ("192.0.2.", "198.51.100.", "203.0.113.", "2001:db8"):
            self.assertNotIn(address, text)

    def test_the_wallet_sign_in_to_the_admin_is_watched(self):
        self.compare(ADMIN_WALLET_SIGN_IN=False)
        self.compare(ADMIN_WALLET_SIGN_IN=True)
        self.assertIn("ADMIN_WALLET_SIGN_IN: off → on", self.sent()[-1])

    def test_the_message_has_the_site_and_the_time_and_nothing_else(self):
        self.compare()
        when = timezone.now().replace(year=2026, month=11, day=19, hour=10, minute=5)
        with override_settings(**{**CHANNEL, "TEAM_AREA": True}):
            watch.compare(now=when)
        lines = self.sent()[0].splitlines()
        self.assertIn("Example community", lines[0])
        self.assertIn("2026", lines[1])
        self.assertEqual(len(lines), 4)

    def test_a_new_setting_of_the_list_is_recorded_silently_a_removed_one_forgotten(self):
        self.compare()
        added = watch.Watched("SOMETHING_NEW", lambda: "x")
        with patch.object(watch, "WATCHED", (*watch.WATCHED, added)):
            self.assertEqual(self.compare(), "recorded")
            self.assertEqual(json_of("SOMETHING_NEW"), '"x"')
        self.assertEqual(self.sent(), [])
        self.assertEqual(self.compare(), "unchanged")
        self.assertFalse(WatchedSetting.objects.filter(name="SOMETHING_NEW").exists())
        self.assertEqual(self.sent(), [])

    def test_a_setting_that_cannot_be_read_is_left_as_it_was(self):
        self.compare()
        broken = watch.Watched("TEAM_AREA", MagicMock(side_effect=RuntimeError(TOKEN)))
        others = [entry for entry in watch.WATCHED if entry.name != "TEAM_AREA"]
        with patch.object(watch, "WATCHED", (*others, broken)), self.assertLogs("website.settings_watch") as logs:
            self.assertEqual(self.compare(), "unchanged")
        self.assertEqual(
            logs.output, [("WARNING:website.settings_watch:settings watch: TEAM_AREA cannot be read (RuntimeError)")]
        )
        self.assertEqual(json_of("TEAM_AREA"), "false")


def json_of(name):
    return WatchedSetting.objects.get(name=name).value


class Secrets(Base):
    def everything_said(self):
        """The messages, the log and `show`, together."""
        out = StringIO()
        call_command("settings_watch", "show", stdout=out)
        return "\n".join(self.sent()) + out.getvalue()

    def test_a_secret_is_never_stored_said_logged_or_shown(self):
        self.compare()
        row = WatchedSetting.objects.get(name="TELEGRAM_BOT_TOKEN")
        self.assertEqual((row.secret, row.value), (True, ""))
        self.assertEqual(len(row.fingerprint), 64)
        old_print = row.fingerprint
        new = "123456:another-token-entirely"
        # A message that does not leave writes its reasons in the log
        with (
            self.assertLogs("website.settings_watch") as logs,
            patch("shops.notifications.deliver_to_moderators", return_value=(None, ["Telegram did not take it"])),
        ):
            self.compare(TELEGRAM_BOT_TOKEN=new)
        with override_settings(**{**CHANNEL, "TELEGRAM_BOT_TOKEN": new}):
            watch.compare()
            said = self.everything_said() + "\n".join(logs.output)
        new_print = WatchedSetting.objects.get(name="TELEGRAM_BOT_TOKEN").fingerprint
        self.assertNotEqual(old_print, new_print)
        self.assertIn("· TELEGRAM_BOT_TOKEN: changed", said)
        self.assertIn("TELEGRAM_BOT_TOKEN: recorded", said)
        for secret in (
            TOKEN,
            new,
            KEY,
            old_print,
            new_print,
            old_print[:16],
            new_print[:16],
            WatchedSetting.objects.get(name="SECRET_KEY").fingerprint[:16],
        ):
            self.assertNotIn(secret, said)
        self.assertNotIn(KEY, "".join(WatchedSetting.objects.values_list("value", "fingerprint").first()))

    def test_the_tokens_change_is_detected_and_set_and_removed_said(self):
        self.compare(TELEGRAM_BOT_TOKEN="")
        # The first start had no token, so no channel: it recorded. Now the token reaches the chat
        self.assertEqual(self.compare(TELEGRAM_BOT_TOKEN=TOKEN, NOSTR_SECRET_KEY="nsec1"), "told")
        self.assertIn("· NOSTR_SECRET_KEY: set", self.sent()[0])
        self.assertIn("· TELEGRAM_BOT_TOKEN: set", self.sent()[0])
        self.compare(TELEGRAM_BOT_TOKEN="999:new")
        self.assertIn("· TELEGRAM_BOT_TOKEN: changed", self.sent()[1])
        self.assertIn("· NOSTR_SECRET_KEY: removed", self.sent()[1])

    def test_secret_keys_change_is_detected_and_the_other_secrets_recorded_again(self):
        self.compare()
        token_print = WatchedSetting.objects.get(name="TELEGRAM_BOT_TOKEN").fingerprint
        self.assertEqual(self.compare(SECRET_KEY="x" * 60), "told")
        text = self.sent()[0]
        self.assertIn("· SECRET_KEY: changed", text)
        self.assertNotIn("TELEGRAM_BOT_TOKEN", text)
        self.assertIn("because SECRET_KEY changed", text)
        self.assertNotEqual(WatchedSetting.objects.get(name="TELEGRAM_BOT_TOKEN").fingerprint, token_print)
        self.assertEqual(self.compare(SECRET_KEY="x" * 60), "unchanged")

    def test_the_fingerprint_is_keyed_and_bound_to_the_name(self):
        with override_settings(SECRET_KEY=KEY):
            one = watch.fingerprint("TELEGRAM_BOT_TOKEN", TOKEN)
            self.assertNotEqual(one, watch.fingerprint("NOSTR_SECRET_KEY", TOKEN))
            self.assertEqual(watch.fingerprint("TELEGRAM_BOT_TOKEN", ""), "")
        with override_settings(SECRET_KEY=KEY + "!"):
            self.assertNotEqual(one, watch.fingerprint("TELEGRAM_BOT_TOKEN", TOKEN))


class Credentials(Base):
    def test_the_credentials_of_an_address_are_neither_stored_nor_sent_but_their_change_is_told(self):
        api = "https://bot:hunter2-pass@proxy.example.net/tg"
        self.compare(TELEGRAM_API=api)
        row = WatchedSetting.objects.get(name="TELEGRAM_API")
        self.assertEqual(row.value, '"https://proxy.example.net/tg"')
        self.assertEqual(len(row.fingerprint), 64)
        self.assertEqual(self.compare(TELEGRAM_API=api), "unchanged")
        # The password alone
        self.assertEqual(self.compare(TELEGRAM_API="https://bot:other-pass@proxy.example.net/tg"), "told")
        self.assertIn("· TELEGRAM_API: the credentials in the address changed", self.sent()[0])
        # The host and the credentials
        self.compare(TELEGRAM_API="https://bot:third-pass@other.example.net/tg")
        self.assertIn("· TELEGRAM_API: https://proxy.example.net/tg → https://other.example.net/tg", self.sent()[1])
        # Taken away
        self.compare(TELEGRAM_API="https://other.example.net/tg")
        self.assertIn("· TELEGRAM_API: the credentials in the address changed", self.sent()[2])
        out = StringIO()
        call_command("settings_watch", "show", stdout=out)
        said = "\n".join(self.sent()) + out.getvalue() + str(list(WatchedSetting.objects.values_list("value")))
        for secret in ("hunter2", "other-pass", "third-pass", "bot:"):
            self.assertNotIn(secret, said)

    def test_a_new_secret_key_does_not_make_the_credentials_look_changed(self):
        self.compare(EMAIL_HOST="smtp://user:pw@mail.example.net")
        self.compare(EMAIL_HOST="smtp://user:pw@mail.example.net", SECRET_KEY="y" * 60)
        self.assertNotIn("EMAIL_HOST", self.sent()[0])
        self.assertEqual(self.compare(EMAIL_HOST="smtp://user:pw@mail.example.net", SECRET_KEY="y" * 60), "unchanged")

    def test_shown_never_writes_credentials(self):
        self.assertEqual(
            watch.shown("https://u:p@a.example/x, smtp://q@b.example"), "https://a.example/x, smtp://b.example"
        )
        self.assertEqual(watch.shown(["https://u:p@a.example"]), "https://a.example")


class Delivery(Base):
    def test_two_starts_at_once_send_once(self):
        self.compare()

        def meanwhile(*args):
            # The other container starts while this one is sending: it finds the comparison taken
            self.assertEqual(self.compare(TEAM_AREA=True), "busy")
            return "TELEGRAM_MODERATION_CHAT", []

        with patch("shops.notifications.deliver_to_moderators", side_effect=meanwhile) as delivered:
            self.assertEqual(self.compare(TEAM_AREA=True), "told")
        self.assertEqual(delivered.call_count, 1)
        self.assertEqual(self.compare(TEAM_AREA=True), "unchanged")
        # A claim left by a start that died ends: a later start compares
        SettingsWatch.objects.filter(pk=1).update(claimed_until=timezone.now() + timedelta(minutes=1))
        self.assertEqual(self.compare(), "busy")
        SettingsWatch.objects.filter(pk=1).update(claimed_until=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.compare(), "told")

    def test_the_claim_is_held_while_sending_and_only_this_starts_is_let_go(self):
        claim = watch.Claim()
        start = timezone.now()
        self.assertTrue(claim.take(start))
        self.assertFalse(watch.Claim().take(start))
        later = start + timedelta(minutes=4)
        with patch("django.utils.timezone.now", return_value=later):
            claim.hold()
        self.assertEqual(SettingsWatch.objects.get(pk=1).claimed_until, later + watch.CLAIM)
        # Past this start's claim another took it: letting go or holding changes only one's own
        other = watch.Claim()
        self.assertTrue(other.take(later + watch.CLAIM + timedelta(seconds=1)))
        claim.hold()
        claim.release()
        self.assertEqual(SettingsWatch.objects.get(pk=1).claimed_until, other.until)
        other.release()
        self.assertIsNone(SettingsWatch.objects.get(pk=1).claimed_until)

    def test_each_mail_holds_the_claim(self):
        self.compare()
        with (
            patch.object(watch.Claim, "hold") as hold,
            override_settings(EMAIL_HOST="mail.example.org"),
            configured(moderators_emails=["a@example.org", "b@example.org"]),
        ):
            self.compare()
        # The main delivery, then each address: the community's email before, the two new ones
        self.assertEqual(hold.call_count, 4)

    def test_an_unsent_message_is_sent_at_the_next_start_and_only_then_marked(self):
        self.compare()
        with (
            patch("shops.notifications.deliver_to_moderators", return_value=(None, ["Telegram did not take it (502)"])),
            self.assertLogs("website.settings_watch") as logs,
        ):
            self.assertEqual(self.compare(TEAM_AREA=True), "not told")
        self.assertIn("the next start tries again (Telegram did not take it (502))", logs.output[0])
        self.assertEqual(json_of("TEAM_AREA"), "false")
        self.assertIsNone(watch.last_told())
        self.assertEqual(self.compare(TEAM_AREA=True), "told")
        self.assertIn("TEAM_AREA: off → on", self.sent()[0])
        self.assertEqual(json_of("TEAM_AREA"), "true")
        self.assertIsNotNone(watch.last_told())

    def test_a_new_chat_is_told_and_the_old_one_too(self):
        self.compare(TELEGRAM_MODERATION_CHAT="11, 12")
        self.compare(TELEGRAM_MODERATION_CHAT="13,12")
        chats = [call.args[0] for call in self.telegram.call_args_list]
        self.assertEqual(chats, ["13,12", "11"])
        # Counts only: the old chat does not learn the new one, nor the new one the old
        self.assertIn("· TELEGRAM_MODERATION_CHAT: removed: 1, added: 1", self.sent()[0])

    def test_the_chat_emptied_is_told_to_the_old_one(self):
        self.compare()
        with override_settings(EMAIL_HOST=""):
            self.assertEqual(self.compare(TELEGRAM_MODERATION_CHAT=""), "told")
        self.assertEqual([call.args[0] for call in self.telegram.call_args_list], ["11"])

    @override_settings(EMAIL_HOST="mail.example.org")
    def test_new_moderators_emails_reach_the_old_and_the_new_addresses(self):
        with configured(moderators_emails=["old@example.org"]):
            self.compare()
        with configured(moderators_emails=["new@example.org", "OLD@example.org", "more@example.org"]):
            self.compare()
        with configured(moderators_emails=["new@example.org"]):
            self.compare()
        # Telegram took the message; the addresses got it by email, old and new, each once
        self.assertEqual(len(self.sent()), 2)
        self.assertEqual(
            [message.to for message in mail.outbox],
            [
                ["old@example.org"],
                ["new@example.org"],
                ["more@example.org"],
                ["new@example.org"],
                ["OLD@example.org"],
                ["more@example.org"],
            ],
        )
        # The message names no address: each reads only how many went and came
        self.assertIn("· moderators_emails: removed: 0, added: 2", self.sent()[0])
        self.assertIn("· moderators_emails: removed: 2, added: 0", self.sent()[1])
        for message in [*self.sent(), *(message.body for message in mail.outbox)]:
            self.assertNotIn("@example.org", message)

    @override_settings(EMAIL_HOST="mail.example.org", TELEGRAM_BOT_TOKEN="")
    def test_by_email_the_current_addresses_once_and_the_old_ones(self):
        with configured(moderators_emails=["old@example.org"]):
            self.compare(TELEGRAM_BOT_TOKEN="")
        with configured(moderators_emails=["new@example.org"]):
            self.assertEqual(self.compare(TELEGRAM_BOT_TOKEN=""), "told")
        self.assertEqual([message.to for message in mail.outbox], [["new@example.org"], ["old@example.org"]])

    def test_no_channel_nothing_sent_nothing_fails(self):
        self.compare(TELEGRAM_BOT_TOKEN="", TELEGRAM_MODERATION_CHAT="")
        self.assertEqual(self.compare(TELEGRAM_BOT_TOKEN="", TELEGRAM_MODERATION_CHAT="", TEAM_AREA=True), "recorded")
        self.assertEqual((self.sent(), mail.outbox), ([], []))
        self.assertEqual(json_of("TEAM_AREA"), "true")
        self.assertIsNone(watch.last_told())


class Quiet(Base):
    def test_switched_off_the_moderators_are_told_once_then_nothing_stored(self):
        self.compare()
        with configured(settings_watch={"enabled": False}):
            self.assertEqual(self.compare(TEAM_AREA=True), "off, told")
            self.assertFalse(WatchedSetting.objects.exists())
            self.assertEqual(self.compare(TEAM_AREA=True), "off")
            self.assertFalse(WatchedSetting.objects.exists())
        self.assertEqual(len(self.sent()), 1)
        self.assertIn("The settings watch was turned off in community.yml", self.sent()[0])
        self.assertIn("If nobody of the team did this, look at community.yml on the machine.", self.sent()[0])
        # On again: the first start records, silently
        self.assertEqual(self.compare(), "recorded")
        self.assertEqual(len(self.sent()), 1)

    def test_switched_off_and_not_told_the_record_stays_until_it_is(self):
        self.compare()
        with configured(settings_watch={"enabled": False}):
            with (
                patch("shops.notifications.deliver_to_moderators", return_value=(None, ["Telegram did not take it"])),
                self.assertLogs("website.settings_watch"),
            ):
                self.assertEqual(self.compare(), "off, not told")
            self.assertTrue(WatchedSetting.objects.exists())
            self.assertIsNone(SettingsWatch.objects.get(pk=1).claimed_until)
            self.assertEqual(self.compare(), "off, told")
            self.assertFalse(WatchedSetting.objects.exists())

    def test_switched_off_with_nobody_to_tell_it_forgets_at_once(self):
        self.compare()
        with configured(settings_watch={"enabled": False}):
            self.assertEqual(self.compare(TELEGRAM_BOT_TOKEN="", TELEGRAM_MODERATION_CHAT=""), "off")
        self.assertFalse(WatchedSetting.objects.exists())
        self.assertEqual(self.sent(), [])

    def test_forget_from_the_machine_says_nothing(self):
        self.compare()
        call_command("settings_watch", "forget", stdout=StringIO())
        self.assertFalse(WatchedSetting.objects.exists())
        self.assertEqual(self.sent(), [])

    def test_wrong_values_of_the_switch_leave_it_on_and_are_checked(self):
        self.assertEqual(wrong_settings_watch(None), [])
        for value, problem in (
            ("no", "is a block with the key enabled"),
            ({"enabled": "no"}, "must be true or false"),
            ({"enable": False}, "is not a setting of the settings watch"),
        ):
            with self.subTest(value=value), configured(settings_watch=value):
                self.assertTrue(watch.enabled())
                found = wrong_settings_watch(None)
                self.assertEqual([check.id for check in found], ["website.W013"])
                self.assertIn(problem, found[0].msg)

    def test_no_table_yet_nothing_happens(self):
        with patch("django.db.connection.introspection.table_names", return_value=["django_migrations"]):
            self.assertEqual(self.compare(), "no tables")
            self.assertEqual(watch.at_start(), "no tables")
            out = StringIO()
            call_command("settings_watch", "show", stdout=out)
            self.assertIn("not migrated yet", out.getvalue())
        self.assertFalse(WatchedSetting.objects.exists())

    def test_any_error_inside_is_caught_and_the_claim_let_go(self):
        self.compare()
        with (
            patch.object(watch, "changes_of", side_effect=RuntimeError(TOKEN)),
            self.assertLogs("website.settings_watch") as logs,
        ):
            self.assertEqual(watch.at_start(), "failed")
        self.assertEqual(
            logs.output,
            [
                (
                    "WARNING:website.settings_watch:settings watch: the comparison failed "
                    "(RuntimeError); the site starts all the same"
                )
            ],
        )
        self.assertIsNone(SettingsWatch.objects.get(pk=1).claimed_until)
        with patch.object(watch, "tables_ready", side_effect=OSError), self.assertLogs("website.settings_watch"):
            self.assertEqual(watch.at_start(), "failed")
        # The command the start runs never fails either
        with patch.object(watch, "compare", side_effect=Exception), self.assertLogs("website.settings_watch"):
            call_command("settings_watch", "compare")


class Command(Base):
    def test_show_and_forget(self):
        out = StringIO()
        call_command("settings_watch", "show", stdout=out)
        self.assertIn("Nothing recorded yet", out.getvalue())
        self.compare(TELEGRAM_BOT_TOKEN="")
        out = StringIO()
        call_command("settings_watch", "show", stdout=out)
        shown = out.getvalue()
        self.assertIn("ADMIN_SECOND_FACTOR: passkey\n", shown)
        self.assertIn("SECRET_KEY: recorded\n", shown)
        self.assertIn("TELEGRAM_BOT_TOKEN: (empty)\n", shown)
        self.assertIn("Last change told to the moderators: never", shown)
        call_command("settings_watch", "forget", stdout=StringIO())
        self.assertFalse(WatchedSetting.objects.exists())
        # The next start records again, without a message
        self.assertEqual(self.compare(TEAM_AREA=True), "recorded")
        self.assertEqual(self.sent(), [])


class Preflight(Base):
    def test_one_calm_note(self):
        self.assertEqual(
            preflight.settings_watch_notes(),
            [
                (
                    "the settings watch is on: the moderators hear of a security setting changed between two starts (no change "
                    "told yet)"
                )
            ],
        )
        self.compare()
        self.compare(TEAM_AREA=True)
        self.assertIn("the last change was told on 20", preflight.settings_watch_notes()[0])
        with configured(settings_watch={"enabled": False}):
            self.assertIn("the settings watch is off", preflight.settings_watch_notes()[0])
        out = StringIO()
        # A test site is not ready for mainnet: only the note counts here
        with override_settings(TELEGRAM_BOT_TOKEN="", TELEGRAM_MODERATION_CHAT=""), self.assertRaises(CommandError):
            call_command("preflight", stdout=out, stderr=StringIO())
        self.assertIn("good to know: the settings watch is on", out.getvalue())


class Translations(Base):
    def test_the_message_in_the_sites_language(self):
        self.compare()
        for language, phrases in (
            (
                "it",
                [
                    "Se nessuno della squadra",
                    "⚠ abbassato: ADMIN_SECOND_FACTOR: passkey → optional",
                    "· TELEGRAM_BOT_TOKEN: cambiato",
                    "ora vuoto",
                ],
            ),
            (
                "de",
                [
                    "Wenn niemand aus dem Team",
                    "⚠ gesenkt: ADMIN_SECOND_FACTOR: passkey → optional",
                    "· TELEGRAM_BOT_TOKEN: geändert",
                    "jetzt leer",
                ],
            ),
        ):
            with self.subTest(language=language), override_settings(LANGUAGE_CODE=language):
                self.telegram.reset_mock()
                self.compare(ADMIN_SECOND_FACTOR="optional", TELEGRAM_BOT_TOKEN="1:x", ADMIN_ALLOWED_IPS="")
                for phrase in phrases:
                    self.assertIn(phrase, self.sent()[0])
                call_command("settings_watch", "forget", stdout=StringIO())
                self.compare()

    def test_the_new_lines_in_the_sites_language(self):
        for language, phrases in (
            (
                "it",
                [
                    "TELEGRAM_MODERATION_CHAT: tolti: 1, aggiunti: 1",
                    "TELEGRAM_API: sono cambiate le credenziali nell'indirizzo",
                    "Il controllo delle impostazioni è stato spento",
                    "controlla community.yml sulla macchina",
                ],
            ),
            (
                "de",
                [
                    "TELEGRAM_MODERATION_CHAT: entfernt: 1, hinzugefügt: 1",
                    "TELEGRAM_API: Die Zugangsdaten in der Adresse haben sich geändert",
                    "Die Überwachung der Einstellungen wurde in community.yml ausgeschaltet",
                    "prüfen Sie community.yml auf der Maschine",
                ],
            ),
        ):
            with self.subTest(language=language), override_settings(LANGUAGE_CODE=language):
                self.telegram.reset_mock()
                call_command("settings_watch", "forget", stdout=StringIO())
                self.compare(TELEGRAM_API="https://a:b@p.example")
                self.compare(TELEGRAM_API="https://a:c@p.example", TELEGRAM_MODERATION_CHAT="12")
                with configured(settings_watch={"enabled": False}):
                    self.compare()
                said = "\n".join(self.sent())
                for phrase in phrases:
                    self.assertIn(phrase, said)


class Start(TestCase):
    def test_gunicorns_master_runs_it_once_when_ready_and_never_fails(self):
        hooks = runpy.run_path(str(ROOT / "config" / "gunicorn.py"))
        server = MagicMock()
        with patch("subprocess.Popen") as popen:
            hooks["when_ready"](server)
        self.assertEqual(popen.call_args.args[0][1:], ["manage.py", "settings_watch", "compare"])
        self.assertEqual(Path(popen.call_args.kwargs["cwd"]), ROOT)
        with patch("subprocess.Popen", side_effect=OSError):
            hooks["when_ready"](server)
        server.log.warning.assert_called_once()

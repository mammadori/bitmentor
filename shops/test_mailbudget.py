"""The email budget (config/mailbudget.py): the count in the database, the totals, the reserve for security notices, the
cap per recipient, the month rollover and the larger allowance of the staff."""
from datetime import timedelta
from unittest.mock import patch

from django.core import mail
from django.core.mail import send_mail
from django.urls import reverse
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone

from config import limits, mailbudget

from .models import EmailSent
from .test_limits import row, set_limits
from .tests import reset_limits

COUNTING = override_settings(EMAIL_BACKEND="config.mailbudget.CountingBackend",
                             EMAIL_REAL_BACKEND="django.core.mail.backends.locmem.EmailBackend")


def ask(address="visitor@example.org"):
    return send_mail("Security", "text", None, [address])


@COUNTING
class Counting(TestCase):
    def setUp(self):
        reset_limits()

    def test_every_email_sent_is_counted_and_the_count_is_in_the_database(self):
        ask("a@example.org")
        ask("b@example.org")
        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual((mailbudget.month_count(), mailbudget.day_count()), (2, 2))
        self.assertEqual(EmailSent.objects.count(), 2)
        self.assertNotIn("example.org", "".join(EmailSent.objects.values_list("recipient", flat=True)))

    def test_the_count_is_read_from_the_rows_not_from_memory(self):
        EmailSent.objects.create(recipient="x")
        self.assertEqual((mailbudget.month_count(), mailbudget.day_count()), (1, 1))

    def test_the_default_of_the_day_is_a_tenth_of_the_month(self):
        self.assertEqual(limits.get("emails_per_day"), 100)
        with set_limits(emails_per_month=300):
            self.assertEqual(limits.get("emails_per_day"), 30)
            self.assertTrue(row("emails_per_day")["default"])
        with set_limits(emails_per_month=300, emails_per_day=50):
            self.assertEqual(limits.get("emails_per_day"), 50)
        with set_limits(emails_per_month=0):
            self.assertEqual(limits.get("emails_per_day"), 0)

    def test_over_the_day_nothing_is_sent_and_nothing_crashes(self):
        with set_limits(emails_per_month=1000, emails_per_day=2), self.assertLogs("config.mailbudget", "WARNING"):
            self.assertEqual([ask(), ask(), ask()], [1, 1, 0])
        self.assertEqual(len(mail.outbox), 2)

    def test_over_the_month_nothing_is_sent_even_if_the_day_is_free(self):
        with set_limits(emails_per_month=3, emails_per_day=0):
            ask(), ask(), ask()
            with self.assertLogs("config.mailbudget", "WARNING"):
                self.assertEqual(ask(), 0)

    def test_zero_is_no_limit(self):
        with set_limits(emails_per_month=0):
            for _ in range(12):
                ask()
        self.assertEqual(len(mail.outbox), 12)

    def test_the_month_starts_again(self):
        first, today = mailbudget.starts()
        EmailSent.objects.create(sent=first - timedelta(seconds=1), recipient="x")
        self.assertEqual(mailbudget.month_count(), 0)
        EmailSent.objects.create(sent=first, recipient="y")
        self.assertEqual(mailbudget.month_count(), 1)
        self.assertEqual(mailbudget.day_count(), 1 if today == first.date() or timezone.localdate().day == 1 else 0)

    def test_old_rows_are_dropped(self):
        EmailSent.objects.create(sent=timezone.now() - timedelta(days=90), recipient="x")
        ask()
        self.assertEqual(EmailSent.objects.count(), 1)


@COUNTING
class Reserve(TestCase):
    def setUp(self):
        reset_limits()

    def test_a_visitor_stops_at_80_percent_and_security_mail_goes_on(self):
        with set_limits(emails_per_month=10, emails_per_day=0, emails_per_address_day=0):
            for i in range(8):
                self.assertTrue(mailbudget.visitor_allowed(f"v{i}@example.org"))
                ask(f"v{i}@example.org")
            with self.assertLogs("config.mailbudget", "WARNING"):
                self.assertFalse(mailbudget.visitor_allowed("late@example.org"))
            self.assertEqual([ask("owner@example.org"), ask("owner@example.org")], [1, 1])
            with self.assertLogs("config.mailbudget", "WARNING"):
                self.assertEqual(ask("owner@example.org"), 0)

    def test_the_day_has_its_reserve_too(self):
        with set_limits(emails_per_month=0, emails_per_day=5):
            for _ in range(4):
                ask()
            with self.assertLogs("config.mailbudget", "WARNING"):
                self.assertFalse(mailbudget.visitor_allowed("x@example.org"))
            self.assertEqual(ask(), 1)

    def test_one_recipient_gets_at_most_the_cap_a_day(self):
        with set_limits(emails_per_address_day=2):
            for _ in range(2):
                self.assertTrue(mailbudget.visitor_allowed("Same@example.org"))
                ask("same@example.org")
            with self.assertLogs("config.mailbudget", "WARNING"):
                self.assertFalse(mailbudget.visitor_allowed("SAME@example.org"))
            self.assertTrue(mailbudget.visitor_allowed("other@example.org"))

    def test_the_dashboard_and_preflight_show_the_usage_and_warn_at_80(self):
        with set_limits(emails_per_month=5, emails_per_day=0):
            for _ in range(4):
                ask()
            found = row("emails_per_month")
            self.assertEqual((found["used"], found["limit"], found["level"]), (4, 5, "near"))
            self.assertTrue(any("emails_per_month" in note for note in limits.notes()))


@COUNTING
class Staff(TestCase):
    def setUp(self):
        reset_limits()

    def request(self, **attributes):
        request = RequestFactory().get("/")
        for name, value in attributes.items():
            setattr(request, name, value)
        return request

    def test_the_staff_has_three_times_the_cap(self):
        staff = mailbudget.factor(self.request(staff_user=object()))
        self.assertEqual((staff, mailbudget.factor(self.request(staff_user=None))), (3, 1))
        with set_limits(emails_per_address_day=2):
            for _ in range(5):
                ask("t@example.org")
            self.assertTrue(mailbudget.visitor_allowed("t@example.org", staff))
            ask("t@example.org")
            with self.assertLogs("config.mailbudget", "WARNING"):
                self.assertFalse(mailbudget.visitor_allowed("t@example.org", staff))

    def test_the_staff_is_not_stopped_by_the_reserve_only_by_the_total(self):
        with set_limits(emails_per_month=10, emails_per_day=0, emails_per_address_day=0):
            for i in range(8):
                ask(f"s{i}@example.org")
            with self.assertLogs("config.mailbudget", "WARNING"):
                self.assertFalse(mailbudget.visitor_allowed("n@example.org"))
            self.assertTrue(mailbudget.visitor_allowed("n@example.org", 3))
            ask(), ask()
            with self.assertLogs("config.mailbudget", "WARNING"):
                self.assertEqual(ask(), 0)

    def test_the_factor_is_a_limit(self):
        with set_limits(emails_staff_factor=2):
            self.assertEqual(mailbudget.factor(self.request(staff_user=object())), 2)

    def test_an_allowed_address_counts_as_staff(self):
        with patch("website.visibility.from_allowed_address", return_value=True):
            self.assertEqual(mailbudget.factor(self.request()), 3)


@COUNTING
class Pages(TestCase):
    def setUp(self):
        reset_limits()

    def test_the_sign_in_page_says_it_cannot_send_instead_of_pretending(self):
        with set_limits(emails_per_month=10, emails_per_day=0):
            for _ in range(8):
                ask()
            mail.outbox.clear()
            with self.assertLogs("config.mailbudget", "WARNING"):
                response = self.client.post(reverse("recovery"), {"email": "someone@example.org"})
        self.assertContains(response, "cannot send more emails", status_code=503)
        self.assertNotContains(response, "Email sent to", status_code=503)
        self.assertEqual(mail.outbox, [])

    def test_the_sign_in_page_sends_while_there_is_room(self):
        response = self.client.post(reverse("recovery"), {"email": "someone@example.org"})
        self.assertContains(response, "Email sent to")
        self.assertEqual(mailbudget.month_count(), 1)

    def test_one_address_cannot_spend_the_visitors_share_alone(self):
        """Sign-in links to made-up recipients from one address stop at `emails_per_sender_day`: the others still get theirs."""
        with set_limits(emails_per_sender_day=3):
            for number in range(3):
                response = self.client.post(reverse("recovery"), {"email": f"made-up-{number}@example.org"},
                                            REMOTE_ADDR="203.0.113.7")
                self.assertContains(response, "Email sent to")
            with self.assertLogs("config.mailbudget", "WARNING"):
                refused = self.client.post(reverse("recovery"), {"email": "made-up-3@example.org"}, REMOTE_ADDR="203.0.113.7")
            other = self.client.post(reverse("recovery"), {"email": "owner@example.org"}, REMOTE_ADDR="203.0.113.8")
        self.assertContains(refused, "try again tomorrow", status_code=429)
        self.assertNotContains(refused, "Email sent to", status_code=429)
        self.assertGreater(int(refused["Retry-After"]), 0)
        self.assertContains(other, "Email sent to")
        self.assertEqual(len(mail.outbox), 4)
        self.assertEqual([message.to for message in mail.outbox][-1], ["owner@example.org"])

    def test_the_wider_network_of_an_address_has_three_times_as_many(self):
        """An IPv6 /48 holds 65,536 /64 networks, free with a tunnel: together they get three times one address's emails."""
        with set_limits(emails_per_sender_day=1):
            answers = [self.client.post(reverse("recovery"), {"email": f"made-up-{number}@example.org"},
                                        REMOTE_ADDR=f"2001:db8:5:{number}::1").status_code for number in range(4)]
        self.assertEqual(answers, [200, 200, 200, 429])

    def test_a_request_refused_for_another_reason_is_not_counted(self):
        """A recipient asked too often in a few minutes is refused before the day's count (three go, two do not): the
        address has one email left, not none."""
        with set_limits(emails_per_sender_day=4):
            for _ in range(5):
                self.client.post(reverse("recovery"), {"email": "same@example.org"}, REMOTE_ADDR="203.0.113.7")
            response = self.client.post(reverse("recovery"), {"email": "another@example.org"}, REMOTE_ADDR="203.0.113.7")
        self.assertContains(response, "Email sent to")

    def test_the_onion_s_visitors_share_a_count_of_their_own(self):
        """Caddy passes every onion visitor with the onion service's own address: together they have what a wider network
        has, and they do not use up the day of whoever comes from that address on the site."""
        onion = {"REMOTE_ADDR": "172.18.0.5", "headers": {"X-Entrance": "onion"}}
        with set_limits(emails_per_sender_day=1):
            answers = [self.client.post(reverse("recovery"), {"email": f"made-up-{number}@example.org"}, **onion).status_code
                       for number in range(4)]
            site = self.client.post(reverse("recovery"), {"email": "owner@example.org"}, REMOTE_ADDR="172.18.0.5",
                                    headers={"X-Entrance": "site"})
        self.assertEqual(answers, [200, 200, 200, 429])
        self.assertContains(site, "Email sent to")

    def test_an_email_that_does_not_leave_is_not_counted(self):
        """The mail server refuses: the address keeps its day, and the mailbox its count of the last minutes."""
        with set_limits(emails_per_sender_day=2):
            with patch("shops.views.send_link", return_value=False):
                for number in range(4):
                    self.client.post(reverse("recovery"), {"email": f"made-up-{number}@example.org"}, REMOTE_ADDR="203.0.113.7")
            answers = [self.client.post(reverse("recovery"), {"email": f"real-{number}@example.org"},
                                        REMOTE_ADDR="203.0.113.7").status_code for number in range(3)]
        self.assertEqual(answers, [200, 200, 429])

    def test_an_address_over_its_day_does_not_use_up_a_mailbox_s_minutes(self):
        """Refused for the day, the address must not make the owner of a mailbox hear «a link was already sent»."""
        with set_limits(emails_per_sender_day=1):
            self.client.post(reverse("recovery"), {"email": "made-up@example.org"}, REMOTE_ADDR="203.0.113.7")
            for _ in range(5):
                refused = self.client.post(reverse("recovery"), {"email": "owner@example.org"}, REMOTE_ADDR="203.0.113.7")
            owner = self.client.post(reverse("recovery"), {"email": "owner@example.org"}, REMOTE_ADDR="203.0.113.8")
        self.assertEqual(refused.status_code, 429)
        self.assertContains(owner, "Email sent to")

    def test_zero_is_no_daily_limit_for_an_address(self):
        with set_limits(emails_per_sender_day=0):
            answers = [self.client.post(reverse("recovery"), {"email": f"made-up-{number}@example.org"},
                                        REMOTE_ADDR="203.0.113.7").status_code for number in range(6)]
        self.assertEqual(answers, [200] * 6)

    def test_the_advice_does_not_send_an_email_account_to_the_wallet(self):
        """An account made with an email has no wallet: the page must not tell everybody to sign in with one."""
        self.assertIn("If you made your account with a Lightning wallet", str(mailbudget.message()))


@COUNTING
class OneAddress(TestCase):
    def setUp(self):
        reset_limits()

    def test_the_staff_is_told_when_one_address_gets_more_than_its_day_of_emails(self):
        from . import dashboard

        with set_limits(emails_per_address_day=2):
            for _ in range(3):
                ask("owner@example.org")
            ask("other@example.org")
            self.assertEqual(mailbudget.crowded(), 1)
            codes = [item.code for item in dashboard.limit_items([])]
        self.assertEqual(codes, ["emails_one_address"])

    def test_a_full_email_limit_gives_its_own_advice(self):
        from . import dashboard

        found = dashboard.limit_items([{"key": "emails_per_month", "level": "full", "what": "emails", "used": 3, "limit": 3}])
        self.assertIn("security notices", found[0].what)

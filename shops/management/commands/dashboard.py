"""What to do today about the shops' registers, in text: the same list as /admin/today/ (shops/dashboard.py), for a
daily message or a look from the shell. Only reads the database, except --send, which keeps what it sent.

With --send, the morning message of bitmentor-dashboard@<project>.timer: a short summary to TELEGRAM_MODERATION_CHAT,
or to the community's email when the site has no Telegram or Telegram refuses it, with the link to /admin/today/. It
goes only when the things to do changed since the last summary (something new, or something solved), the new ones
first, and as a reminder when the same list has waited a week; never when there is nothing to do. The summary names
the shops and says what happened, never more: no owner, email or address. What was sent is kept in
DashboardDigest (codes and shops, no text), and only once it was delivered.

Exit code: 0 nothing to do, or only things to know; 2 at least one problem or one thing to look at (1 stays an error of
the command itself). With --send: 10 a summary was sent, 0 none was needed, 1 it could not be delivered."""
import sys
from datetime import timedelta
from smtplib import SMTPException

from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand, CommandError
from django.urls import reverse
from django.utils import timezone, translation
from django.utils.formats import date_format
from django.utils.translation import gettext as _
from django.utils.translation import ngettext

from config.community import community
from shops.dashboard import INFO, PROBLEM, WARNING, plain, summary
from shops.models import DashboardDigest, is_demo
from shops.notifications import NotificationFailed, alert_text, telegram

SOMETHING_TO_DO = 2
# Not 2, which argparse also uses for a wrong option: the unit counts this one as a success
SENT = 10

SIGNS = {PROBLEM: "✗", WARNING: "!", INFO: "·"}
# Lines of things to do in the message: the page has the rest
SENT_LINES = 10
# The same list again, as a reminder, after this long
REMIND_EVERY = timedelta(days=7)


def to_do(found):
    return [item for item in found["items"] if item.level != INFO]


def key(item):
    """What the summary remembers of an item: its code and its shop, nothing a person wrote."""
    return f"{item.code}:{item.shop.pk if item.shop else ''}"


def site_label():
    """The community and the site, and the network when it is a test one: the site of development and the one in
    production may write to the same chat."""
    where = settings.SITE_HOST if settings.NETWORK == "mainnet" else f"{settings.SITE_HOST}, {settings.NETWORK}"
    return f"{alert_text(str(community().get('name') or settings.SITE_HOST))} ({where})"


def line(item):
    name = alert_text(item.shop.name) if item.shop else _("The whole site")
    return f"{SIGNS[item.level]} {name}: {item.what}"


def message(items, new, solved, since=None):
    """The summary: (subject, text). The new things first, then those still to do, how many were solved since the last
    summary, and the link. `since` is the date of the last summary, for a reminder with nothing new."""
    subject = ngettext("%(site)s: %(count)d thing to do about the shops' registers",
                       "%(site)s: %(count)d things to do about the shops' registers", len(items)) % {
        "site": site_label(), "count": len(items)}
    lines = [subject]
    if since:
        lines.append(_("Nothing new since the summary of %(date)s: a reminder, once a week.")
                     % {"date": date_format(timezone.localtime(since), "DATE_FORMAT")})
    fresh = [item for item in items if key(item) in new]
    old = [item for item in items if key(item) not in new]
    room = SENT_LINES
    for title, group in ((_("New:"), fresh), (_("Still to do:"), old)):
        if fresh and old and group[:room]:
            lines.append(title)
        lines += [line(item) for item in group[:room]]
        room -= len(group[:room])
    if len(items) > SENT_LINES:
        lines.append(_("…and {count} more").format(count=len(items) - SENT_LINES))
    if solved:
        lines.append(ngettext("%(count)d thing solved since the last summary.",
                              "%(count)d things solved since the last summary.", solved) % {"count": solved})
    lines.append(_("What to do, and the words for the merchant: {link}").format(
        link=f"{settings.PUBLIC_URL}{reverse('admin_dashboard')}"))
    return subject, "\n".join(lines)


class Command(BaseCommand):
    help = "What to do today about the shops' registers, most urgent first (read-only without --send)"
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("--language", default=settings.LANGUAGE_CODE, help="The language of the text")
        parser.add_argument("--all", action="store_true", help="Also the things to know, not only what to do")
        parser.add_argument("--send", action="store_true", help="Send a short summary to the moderators' Telegram chat "
                            "(or the community's email) when the things to do changed, or a week later; exit 10 when "
                            "one was sent")

    def handle(self, *args, language, all, send=False, **options):
        with translation.override(language):
            found = summary()
            if send:
                if self.send(found):
                    sys.exit(SENT)
                return
            self.show(found, all)
        if to_do(found):
            sys.exit(SOMETHING_TO_DO)

    def show(self, found, all):
        counts = found["counts"]
        self.stdout.write(_("Registers: %(working)d with a Lightning address that works, %(to_look)d shops to look at.")
                          % counts)
        items = [item for item in found["items"] if all or item.level != INFO]
        hidden = len(found["items"]) - len(items)
        for item in items:
            name = plain(item.shop.name) if item.shop else _("The whole site")
            line = f"{SIGNS[item.level]} {name}: {item.what}"
            if item.since:
                line += f" ({item.when})"
            self.stdout.write(line)
            if item.wrote:
                self.stdout.write(f"    «{plain(item.wrote)}»")
            self.stdout.write(f"    {item.do}")
            if item.command:
                self.stdout.write(f"    {item.command}")
        if hidden:
            self.stdout.write(ngettext("%(count)d more thing to know: --all, or /admin/today/.",
                                       "%(count)d more things to know: --all, or /admin/today/.", hidden)
                              % {"count": hidden})

    def send(self, found):
        """The summary when the list changed or waited a week; True when one was delivered. Without the shops of the
        training site (manage.py demo_data): a message to the moderators' chat is never about made-up shops."""
        items = [item for item in to_do(found) if not (item.shop and is_demo(item.shop))]
        keys = sorted({key(item) for item in items})
        last = DashboardDigest.objects.order_by("pk").first()
        before = set(last.items) if last else set()
        now = timezone.now()
        if not items:
            if before:
                # Nothing to do any more: the next problem is new again
                DashboardDigest.objects.filter(pk=last.pk).update(items=[])
            self.stdout.write("Nothing to do today: no message.")
            return False
        new, solved = set(keys) - before, len(before - set(keys))
        reminder = not new and not solved
        if reminder and now - last.sent < REMIND_EVERY:
            self.stdout.write(f"The same {len(keys)} things to do as in the summary of {last.sent:%Y-%m-%d}: no message "
                              "(a reminder goes a week after it).")
            return False
        subject, text = message(items, new, solved, since=last.sent if reminder else None)
        where = self.deliver(subject, text)
        if last:
            DashboardDigest.objects.filter(pk=last.pk).update(sent=now, items=keys)
        else:
            DashboardDigest.objects.create(sent=now, items=keys)
        self.stdout.write(f"Summary sent to {where}: {len(keys)} things to do, {len(new)} new, {solved} solved.")
        return True

    def deliver(self, subject, text):
        """To the moderators' chat, else to the community's email: where it went, or CommandError."""
        problems = []
        if settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_MODERATION_CHAT:
            try:
                if telegram(settings.TELEGRAM_MODERATION_CHAT, text):
                    return "TELEGRAM_MODERATION_CHAT"
                problems.append("Telegram refused it (a wrong chat, or a bot that is not in it)")
            except NotificationFailed as error:
                problems.append(f"Telegram did not take it ({error})")
        address = community().get("email")
        if isinstance(address, str) and "@" in address:
            try:
                send_mail(subject, text, None, [address.strip()])
                if problems:
                    self.stderr.write(f"{problems[0]}: sent by email instead.")
                return "the community's email"
            except (OSError, SMTPException, ValueError) as error:
                problems.append(f"the community's email failed ({type(error).__name__})")
        else:
            problems.append("no `email` in community.yml to fall back on")
        raise CommandError(f"The summary was not delivered: {'; '.join(problems)}. The next run tries again.")

"""A test message, to see that a channel of the site's messages reaches someone.

    manage.py notify_test --moderators    one message to the people who look after the site

It goes through shops.notifications.deliver_to_moderators, exactly as the morning summary, the new shops and the other
moderators' messages do: to every chat of TELEGRAM_MODERATION_CHAT, else by email (`moderators_emails`, else the
community's `email`). It says where it went, or why it went nowhere, without chats or addresses, and exits with an error
when nothing was delivered: here, unlike preflight, a channel that reaches nobody is the failure being looked for.
`--moderators` is required for now: other audiences may come later."""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import translation
from django.utils.translation import gettext as _

from config import mail
from shops.notifications import deliver_to_moderators


def moderators_message():
    """(subject, text) of the test, in the community's first language, with the site's name as the other messages."""
    from shops.management.commands.dashboard import site_label

    with translation.override(settings.LANGUAGE_CODE):
        subject = _("Test message")
        text = _("This is a test message from the site, sent with manage.py notify_test. If you read it, the messages "
                 "for the people who look after the site reach you here. There is nothing to do.")
        return f"{site_label()}: {subject}", f"{site_label()}\n{text}"


class Command(BaseCommand):
    help = "Sends a test message to the people who look after the site (--moderators), the way the real ones go"

    def add_arguments(self, parser):
        parser.add_argument("--moderators", action="store_true",
                            help="To TELEGRAM_MODERATION_CHAT, else by email, as the moderators' messages go")

    def handle(self, *args, moderators=False, **options):
        if not moderators:
            raise CommandError("Say who gets the test message: --moderators (the people who look after the site, the "
                               "way the morning summary goes). Nothing was sent.", returncode=2)
        subject, text = moderators_message()
        where, problems = deliver_to_moderators(subject, text)
        for problem in problems:
            self.stderr.write(problem)
        if where is not None and where != "TELEGRAM_MODERATION_CHAT" and not mail.leaves():
            # A test network's mail without a server: written to the log of web, read by nobody (as preflight says)
            self.stderr.write("the email did not leave this machine (only the log of web has it): EMAIL_HOST is empty")
            where = None
        if where is None:
            raise CommandError("Nothing was delivered: the moderators would not hear from the site. Set "
                               "TELEGRAM_BOT_TOKEN and TELEGRAM_MODERATION_CHAT, or EMAIL_HOST with `moderators_emails` "
                               "or `email` in community.yml (docs/operations.md).")
        self.stdout.write(f"sent to: {where}")

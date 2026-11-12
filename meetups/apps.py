from django.apps import AppConfig
from django.core import checks
from django.utils.translation import gettext_lazy as _


def wrong_settings(app_configs, **kwargs):
    """A wrong value in the `meetups` block is left out and the board stays up: `manage.py check` and the log of `web` say
    what was left out, as for the events. A warning, not an error, which would stop `migrate` and with it
    the whole site."""
    from .conf import problems

    return [checks.Warning(problem, hint="The format is in docs/features/meetups.md and next to the `meetups` block in "
                                         "examples/community/community.yml", id="meetups.W001") for problem in problems()]


class MeetupsConfig(AppConfig):
    name = "meetups"
    verbose_name = _("Meetups")

    def ready(self):
        from django.db.models.signals import post_delete, post_save

        from . import board
        from .models import Meetup

        checks.register(wrong_settings)
        # The board and the feed are kept for a few minutes: a meetup saved or deleted (by the staff in /admin/, by the
        # reader of Nostr) makes the next request work them out again
        post_save.connect(board.forget_on_change, sender=Meetup)
        post_delete.connect(board.forget_on_change, sender=Meetup)

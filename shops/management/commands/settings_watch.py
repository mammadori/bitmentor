"""The settings watch (website/settings_watch.py): what the last start recorded of the security settings, and forgetting
it. `compare` is what the start of the site runs (config/gunicorn.py)."""

from django.core.management.base import BaseCommand
from django.utils import timezone

from website import settings_watch


class Command(BaseCommand):
    help = (
        "The security settings recorded at the last start, which the moderators hear of when they change: show, "
        "forget (the next start records again without a message), compare (what the start of the site runs)"
    )
    # The start runs it beside gunicorn: the checks are migrate's, already done
    requires_system_checks = ()

    def add_arguments(self, parser):
        parser.add_argument("action", choices=["show", "forget", "compare"])

    def handle(self, *args, action, **options):
        if action == "compare":
            # Never an error, never output: the log says what went wrong, the start goes on
            settings_watch.at_start()
            return
        if not settings_watch.tables_ready():
            self.stdout.write("Nothing recorded: the database is not migrated yet (manage.py migrate)")
            return
        if action == "forget":
            settings_watch.forget()
            self.stdout.write("Forgotten: the next start records the settings again, without a message")
            return
        self.show()

    def show(self):
        from website.models import WatchedSetting

        if not settings_watch.enabled():
            self.stdout.write("The settings watch is off (settings_watch: {enabled: false} in community.yml)")
        rows = {row.name: row for row in WatchedSetting.objects.all()}
        if not rows:
            self.stdout.write("Nothing recorded yet: the next start of the site records the settings")
            return
        for entry in settings_watch.WATCHED:
            row = rows.get(entry.name)
            if row is None:
                value = "not recorded yet"
            elif row.secret:
                # Never the value, never the fingerprint
                value = "recorded" if row.fingerprint else "(empty)"
            else:
                value = settings_watch.shown(settings_watch.loaded(row.value))
            self.stdout.write(f"{entry.name}: {value}")
        told = settings_watch.last_told()
        self.stdout.write(
            f"Last change told to the moderators: {timezone.localtime(told):%Y-%m-%d %H:%M}"
            if told
            else "Last change told to the moderators: never"
        )

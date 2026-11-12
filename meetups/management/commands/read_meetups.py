"""Reads the certified organisers' calendar events (NIP-52) from the relays and updates the meetups of the board."""
import time

from django.core.management.base import BaseCommand, CommandError

from meetups import conf, relays
from meetups.models import Reading
from meetups.nostr_read import ingest, trusted_authors

KEEP_READINGS = 60
DEFAULT_DAYS = 400


class Command(BaseCommand):
    help = ("Reads the calendar events (NIP-52) of the trusted authors from the relays of `meetups` in community.yml and "
            "updates the board. bitmentor-meetups@<project>.timer runs it every day. It only reads: nothing is sent to a relay.")

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="read and check, but change nothing")
        parser.add_argument("--days", type=int, default=DEFAULT_DAYS, help="ask for the events published in the last N days")
        parser.add_argument("--relay", action="append", default=[], help="read this relay instead of the configured ones")

    def handle(self, *args, **options):
        if not conf.enabled():
            self.stdout.write("The `meetups` block is not in community.yml: nothing to read.")
            return
        problems = []
        urls = options["relay"] or conf.relays(problems.append)
        authors = list(trusted_authors())
        for problem in problems:
            self.stderr.write(problem)
        if not urls or not authors:
            self.stdout.write("Nothing to read: " + ("no relays" if not urls else "no trusted authors")
                              + " (see docs/features/meetups.md).")
            return
        since = int(time.time()) - max(1, min(options["days"], 3650)) * 86400
        events, answered = relays.read_all(urls, authors, since, self.stdout.write)
        if answered == 0:
            self.save(Reading(relays_asked=len(urls), relays_answered=0, detail="no relay answered"), options)
            raise CommandError("No relay answered")
        if options["dry_run"]:
            from django.db import transaction

            with transaction.atomic():
                report = ingest(events)
                transaction.set_rollback(True)
        else:
            report = ingest(events)
        self.stdout.write(f"{report.seen} events: {report.created} new, {report.updated} updated, {report.cancelled} cancelled, "
                          f"{report.older} older than what we have, {report.kept} left alone for the staff, "
                          f"{report.rejected_total} rejected")
        for line in report.detail().splitlines():
            self.stdout.write(f"  {line}")
        self.save(Reading(relays_asked=len(urls), relays_answered=answered, events_seen=report.seen, created=report.created,
                          updated=report.updated, cancelled=report.cancelled, rejected=report.rejected_total,
                          detail=report.detail()), options)

    def save(self, reading, options):
        if options["dry_run"]:
            return
        reading.save()
        old = Reading.objects.order_by("-when").values_list("pk", flat=True)[KEEP_READINGS:]
        Reading.objects.filter(pk__in=list(old)).delete()

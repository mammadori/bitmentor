"""Reads OpenStreetMap and keeps the shops and their entries in step (shops/osm.py): proposes a link for the shops that are
beyond doubt the same place (the association confirms it in /admin/), fills the address, town and position a confirmed shop
has empty, offers its website and phone, lists the places that take bitcoin and are not among the shops,
and works out the changes OpenStreetMap should get from what this site knows. It never writes to OpenStreetMap: that is
`manage.py osm_upload`, run by hand.

With --send, the run of bitmentor-osm@<project>.timer, once a month: a short summary to TELEGRAM_MODERATION_CHAT, or to
the community's email when the site has no Telegram or Telegram refuses it, only when there is something to look at.
Exit code: 0 done; 1 the reading failed (Overpass down, too slow, an answer that cannot be right: nothing is changed and
the failure is kept); with --send, 10 when a summary was sent.

Without --apply nothing is written, not even the report: it only prints what a run would do."""
import sys

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.urls import reverse
from django.utils import translation

from shops import osm, osm_net
from shops.notifications import deliver_to_moderators

SENT = 10


class Command(BaseCommand):
    help = "Reads OpenStreetMap, links and fills the shops, and prepares the changes to propose (never writes there)"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Print what would be done and write nothing")
        parser.add_argument("--accept-fewer", action="store_true", help="Trust an answer with less than half the elements of "
                            "the last reading (a region that really shrank); otherwise such an answer is refused")
        parser.add_argument("--send", action="store_true", help="Send the monthly summary to the moderators, when there is "
                            "something to look at; exit 10 when one was sent")

    def handle(self, *args, dry_run=False, send=False, accept_fewer=False, **options):
        config = osm.settings(warn=lambda problem: self.stderr.write(f"community.yml: {problem}"))
        if config is None:
            self.stdout.write("OpenStreetMap is off: there is no `osm` section with an area in community.yml.")
            return
        previous = osm.previous_run()
        last = osm.last_run()
        try:
            data = osm.load(osm_net.read_area(config))
            elements, skipped = osm.parse(data)
            if not accept_fewer:
                # Against the last reading that worked, told or not: it is the size that matters here
                osm.plausible(len(elements), last.report if last else None)
        except osm.OsmError as error:
            if not dry_run:
                osm.save_run(error=str(error))
            raise CommandError(f"OpenStreetMap was not read: {error}. Nothing was changed; the next run tries again.") from None
        before = previous.report if previous else None
        report = osm.synchronize(config, elements, skipped, apply=not dry_run, previous=before)
        if not dry_run:
            # What the readings since the last summary that went out did once is told in the next one that does
            report = osm.carry(report, previous)
        found = osm.counts(report)
        self.stdout.write(
            f"{report['elements']} elements read ({skipped} left out, {report['unreliable_elements']} not read whole). "
            f"Links proposed now: {found['link_proposals']} ({report['waiting_links']} already waiting), suggested: "
            f"{found['suggested']}, shops with fields filled: {found['filled']}, with a contact offered: {found['offers']}, "
            f"doubtful: {found['ambiguous']}, linked but not found: {found['missing']}, candidates: "
            f"{found['total_candidates']} ({found['candidates']} new), OpenStreetMap says no longer: {found['says_no']}, checked "
            f"there later than here: {found['newer']}. Proposed changes: {found['proposals']} in {found['shops']} shops.")
        if dry_run:
            return
        # A reading made by hand, without --send, tells nobody: the next one does not take it as the last that was told
        run = osm.save_run(report if send else {**report, "silent": True})
        if send and osm.worth_telling(report, before):
            with translation.override(settings.LANGUAGE_CODE):
                subject, text = osm.summary(report, before, f"{settings.PUBLIC_URL}{reverse('admin:shops_osmrun_changelist')}")
            where, problems = deliver_to_moderators(subject, text)
            if where is None:
                # Kept, but the next reading does not take it as the one that told: what was new is told then
                run.report = {**report, "undelivered": True}
                run.save(update_fields=["report"])
                raise CommandError(f"The summary was not delivered: {'; '.join(problems)}. The next run tries again.")
            self.stdout.write(f"Summary sent to {where}.")
            sys.exit(SENT)
        if send:
            self.stdout.write("Nothing new since the last summary: no message.")

"""Probes the Lightning address of the shops that are due (shops/lightning_watch.py), one at a time, with a pause
between them and a cap per run. bitmentor-sentinel@<project>.timer runs it every hour: a working address is probed
every 6 hours, a failing or broken one every hour."""
import logging
import time

from django.core.management.base import BaseCommand, CommandError
from django.db.models import F
from django.utils import timezone

from shops import certified, lightning_watch, vacation


class Command(BaseCommand):
    help = "Probes the Lightning address of the shops with a register, and tells merchant and association what changed"

    def add_arguments(self, parser):
        parser.add_argument("--pause", type=float, default=3, help="seconds between two shops (3)")
        parser.add_argument("--limit", type=int, default=100, help="at most this many shops in a run (100)")
        parser.add_argument("--minutes", type=float, default=45,
                            help="no new probe after this many minutes, so runs do not pile up (45)")
        parser.add_argument("--all", action="store_true", help="every watched shop, not only the ones due")

    def handle(self, *args, pause, limit, minutes, all, **options):
        forgotten = lightning_watch.forget_unwatched()
        # The shops that reopened this morning hear «welcome back, try a small payment», and the fault the guardian found
        # while they were closed (shops/vacation.py). Before the probes, which can take most of an hour, and whether or
        # not this server resolves names
        try:
            welcomed = len(vacation.welcome_back())
        except Exception:  # noqa: BLE001 (a message that cannot go out must not stop the guardian)
            welcomed = 0
            logging.getLogger(__name__).exception("welcome back failed")
        if welcomed:
            self.stdout.write(f"{welcomed} shops welcomed back")
        if all:
            shops = lightning_watch.watched().order_by(F("lightning_checked").asc(nulls_first=True), "pk")
        else:
            shops = lightning_watch.due(timezone.now())
        ids = list(shops.values_list("pk", flat=True)[:max(limit, 0)])
        if ids and not lightning_watch.network_ok(lightning_watch.watched()):
            # Every address would look dead because of us: nothing is probed, and the next run tries again
            self.stderr.write("this server resolves no names: no address probed")
            return
        deadline = time.monotonic() + minutes * 60
        # First all the probes, then what they mean: if most fail at once, it is not the shops
        results = []
        # What raised where nothing should: the run goes on for the other shops, and ends as failed so that it is seen
        errors = 0
        for index, pk in enumerate(ids):
            if index:
                time.sleep(pause)
            if time.monotonic() > deadline:
                break
            try:
                found = lightning_watch.probe_shop(pk)
            except Exception:  # noqa: BLE001 (whatever one shop's provider answers, the others are still probed)
                logging.getLogger(__name__).exception("the probe of shop %s failed: skipped, the run goes on", pk)
                errors += 1
                continue
            if found is not None:
                results.append((pk, *found))
        # Shops already broken are expected to fail: they do not make a run look like a network problem
        fresh = [result for _pk, _address, result, broken in results if not broken and result != lightning_watch.UNSURE]
        failed = sum(1 for result in fresh if result is not None)
        mass = (failed, len(fresh)) if len(fresh) >= lightning_watch.MASS_FAILURE_MIN and failed * 2 > len(fresh) else None
        changes = []
        counts = {"ok": 0, "failed": 0, "unsure": 0}
        for pk, address, result, _broken in results:
            if mass and result is not None:
                result = lightning_watch.UNSURE
            counts["ok" if result is None else "unsure" if result == lightning_watch.UNSURE else "failed"] += 1
            shop, change, _recorded = lightning_watch.record(pk, address, result)
            if change:
                changes.append((shop, change))
                try:
                    lightning_watch.tell_owner(shop, change)
                except Exception:  # noqa: BLE001 (a message that cannot go out must not stop the guardian)
                    logging.getLogger(__name__).exception("shop %s: the owner could not be told", shop.pk)
                    errors += 1
                self.stdout.write(f"{shop.pk} {change}: {shop.lightning_status}")
        lightning_watch.tell_association(changes, mass=mass)
        # The certificates: an address that works and an owner with the basic pass, whatever came last
        certifications = certified.refresh()
        if certifications:
            self.stdout.write(f"{certifications} shops certified")
        if mass:
            self.stdout.write(f"{mass[0]} of {mass[1]} failed at once: nothing counted, no merchant told")
        self.stdout.write(f"{len(results)} probed, {counts['ok']} working, {counts['failed']} failed, "
                          f"{counts['unsure']} unsure, {len(changes)} changed, {len(ids) - len(results)} skipped, "
                          f"{forgotten} no longer watched")
        if errors:
            # Everything else was probed, recorded and told: the unit still ends as failed, and its alarm says to look
            raise CommandError(f"{errors} shops raised an error the guardian does not know (see the log above): "
                               "they are skipped at every run until it is fixed")

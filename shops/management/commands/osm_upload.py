"""Sends to OpenStreetMap the changes this site proposes for its shops' entries (shops/osm.py). By hand, and only by hand:
no timer runs it, and it has no option that skips the question.

An experimental feature (config/features.py): off unless community.yml says `experimental: {osm_upload: true}`. Off, the
command says so and ends cleanly: it reads nothing, writes no file and sends nothing.

OpenStreetMap has a code of conduct for automated edits: they need to be documented, discussed with the local community
and made from an account of their own. So:
- it reads OpenStreetMap again first, and proposes against what is there now;
- it touches only the bitcoin tags of the settings (osm.discount_tag, osm.operator, and the bitcoin payment tags), for the
  shops linked to an element, a few at a time;
- WITHOUT `OSM_TOKEN` (the OAuth2 token of the account that signs) it sends nothing: it writes an osmChange file, to
  review and load with an editor such as JOSM, and prints a link to iD for each element;
- WITH the token, it lists every change, asks you to type `upload`, and only then opens a changeset (a comment that says
  the data come from the payments the site recorded, `source` the site, and `created_by`), uploads, closes it, and marks
  the merchants' requests it did as done. The token is never printed, logged or written anywhere.

The token goes in the environment for this one run, and never on the command line (where `ps` and the shell's history
would keep it) nor in the .env:
    read -rs OSM_TOKEN; export OSM_TOKEN
    docker compose exec -e OSM_TOKEN web python manage.py osm_upload

The proposals that need a person's judgement (an operator someone else wrote, a "no" turned into a "yes") are listed but
sent only with --with-review."""
import os
import re
import sys
import tempfile

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from config import features
from shops import osm, osm_edit, osm_net

CHANGESET = re.compile(r"^[0-9]{1,15}$")
CONFIRMATION = "upload"


def interactive():
    return sys.stdin.isatty()


def ask(prompt):
    return input(prompt)


class Command(BaseCommand):
    help = "Writes the proposed changes as an osmChange file and links; with OSM_TOKEN and your confirmation, uploads them"

    def add_arguments(self, parser):
        parser.add_argument("--osc", help="Where to write the osmChange file (a file that does not exist yet); '-' for the "
                            "standard output (default: a new file with a name of its own in the temporary folder)")
        parser.add_argument("--with-review", action="store_true", help="Also the changes that need your judgement")
        parser.add_argument("--limit", type=int, default=osm_edit.MAX_ELEMENTS,
                            help=f"At most this many elements in the changeset ({osm_edit.MAX_ELEMENTS})")

    def handle(self, *args, osc=None, with_review=False, limit=osm_edit.MAX_ELEMENTS, **options):
        if not features.enabled(features.OSM_UPLOAD):
            # Like the commands of a feature turned off: said, and a clean end (exit code 0)
            (self.stderr if osc == "-" else self.stdout).write(
                "Writing to OpenStreetMap is an experimental feature, and it is off: nothing was read and nothing was sent. "
                "To turn it on, write `osm_upload: true` in an `experimental` block of community.yml and restart the site "
                "(docs/features/openstreetmap.md). Until then the report of the last reading (/admin/shops/osmrun/) lists "
                "what OpenStreetMap should have, for a person to change by hand on openstreetmap.org.")
            return
        config = osm.settings(warn=lambda problem: self.stderr.write(f"community.yml: {problem}"))
        if config is None:
            raise CommandError("OpenStreetMap is off: there is no `osm` section with an area in community.yml.")
        if not 1 <= limit <= osm_edit.MAX_ELEMENTS:
            raise CommandError(f"--limit must be between 1 and {osm_edit.MAX_ELEMENTS}")
        out = self.stderr if osc == "-" else self.stdout
        try:
            elements, _skipped = osm.parse(osm.load(osm_net.read_area(config, meta=True)), meta=True)
        except osm.OsmError as error:
            raise CommandError(f"OpenStreetMap was not read: {error}. Nothing was sent.") from None
        now = timezone.now()
        edits, later = osm_edit.plan(config, elements, now, review=with_review, limit=limit)
        if not edits:
            out.write("Nothing to send: every linked shop's entry already says what this site knows.")
            return
        total = sum(len(edit.changes) for edit in edits)
        out.write(f"{total} changes in {len(edits)} elements" + (f" ({later} more elements wait for the next run)" if later else "") + ":")
        for edit in edits:
            out.write(f"\n{edit.shop.name}  ({edit.element.ref})")
            for line in osm_edit.lines(edit):
                out.write(line)
            editor, page = osm_edit.links(edit)
            out.write(f"    edit by hand: {editor}\n    see it:       {page}")
        token = settings.OSM_TOKEN
        if not token:
            document = osm_edit.osm_change(edits)
            if osc == "-":
                self.stdout.write(document.decode())
            else:
                path = self.write_file(osc, document)
                out.write(f"\nNo OSM_TOKEN: nothing was sent. The osmChange file is {path}: look at it, load it in an editor "
                          "(JOSM: File > Open) and upload it with your own account, or use the links above.")
            return
        self.upload(config, edits, now, out)

    def write_file(self, path, document):
        """The osmChange file, readable by its owner only, in a file that did not exist: the default has a name nobody can
        guess (a name that can be guessed in /tmp could be taken by somebody who points it at another file), and a chosen one is
        refused if it exists or is a link."""
        try:
            if path:
                handle = os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600), "wb")
            else:
                descriptor, path = tempfile.mkstemp(prefix="osm-changes-", suffix=".osc")
                handle = os.fdopen(descriptor, "wb")
        except FileExistsError:
            raise CommandError(f"{path} exists already: choose a file that does not exist, or none") from None
        except OSError as error:
            raise CommandError(f"the file could not be written ({type(error).__name__})") from None
        with handle:
            handle.write(document)
        return path

    def upload(self, config, edits, now, out):
        if not interactive():
            raise CommandError("Uploading asks you a question: run it in a terminal (docker compose exec, without -T).")
        answer = ask(f"\nSend these changes to OpenStreetMap with the account of OSM_TOKEN, in one changeset "
                     f"({osm.changeset_comment(config)!r})? Type {CONFIRMATION!r} to send, anything else to stop: ")
        if answer.strip() != CONFIRMATION:
            out.write("Nothing was sent.")
            return
        token = settings.OSM_TOKEN
        try:
            created = osm_net.api(config, "PUT", "changeset/create", token, osm_edit.changeset_xml(config, settings.SITE_HOST))
            changeset = created.decode("ascii", "replace").strip()
            if not CHANGESET.match(changeset):
                raise osm.OsmError("the server did not give a changeset number")
        except osm.OsmError as error:
            raise CommandError(f"The changeset was not opened: {error}. Nothing was sent.") from None
        try:
            osm_net.api(config, "POST", f"changeset/{changeset}/upload", token, osm_edit.osm_change(edits, changeset))
        except osm.OsmError as error:
            raise CommandError(f"The upload failed ({error}); changeset {changeset} was closed. If the server says the "
                               "version is old, someone edited an element meanwhile: run the command again.") from None
        finally:
            # Whatever happened, an error of the server or of ours, a stop with Ctrl-C: a changeset left open locks nothing,
            # but it stays open for an hour under the name of the account
            self.close(config, changeset, token)
        out.write(f"\nUploaded: https://www.openstreetmap.org/changeset/{changeset}")
        self.record(edits, changeset, now, out)

    def close(self, config, changeset, token):
        try:
            osm_net.api(config, "PUT", f"changeset/{changeset}/close", token)
        except osm.OsmError:
            self.stderr.write(f"Changeset {changeset} could not be closed: it closes by itself within an hour.")

    def record(self, edits, changeset, now, out):
        """What was sent is no longer proposed, and the merchants who asked for it are told it is done."""
        run = osm.last_run()
        sent = {(str(edit.shop.pk), tag) for edit in edits for tag, _old, _new, _kind in edit.changes}
        if run is not None:
            kept = []
            for item in run.report.get("proposals", []):
                left = [change for change in item["changes"] if (item["shop"], change["tag"]) not in sent]
                if left:
                    kept.append({**item, "changes": left})
            run.report = {**run.report, "proposals": kept}
            run.uploaded, run.changeset = now, changeset
            run.save(update_fields=["report", "uploaded", "changeset"])
        left = {item["shop"] for item in (run.report.get("proposals", []) if run else [])}
        done = osm_edit.finish_requests([edit.shop for edit in edits if str(edit.shop.pk) not in left])
        if done:
            out.write(f"{done} requests of merchants marked as done.")

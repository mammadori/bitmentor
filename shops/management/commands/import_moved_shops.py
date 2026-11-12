"""Reads a file written by export_shops on another bitmentor site and brings its shops here (shops/moving.py).

A dry run unless --apply: the same import, rolled back, so the report says exactly what --apply would do. With
--owners, a CSV with one row per moved shop with an owner: where to write, the one-time link when the shop waits for
its owner, and the message telling them what to do. --apply needs --owners: the links exist only in that file. The
file is opened before the import, readable by its owner only, and never over an existing one. With --only, one shop
of the file and no other: with --new-codes, a new link for that shop alone, when its owner lost theirs."""
import csv
import os
import unicodedata
import uuid

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from config import features
from shops.models import Shop
from shops.moving import (CONFLICT, FILLED, HERE, MAX_BYTES, NEW, REFUSED, FileRefused, claim_link, import_document,
                           owner_message, read)
from shops.views import spreadsheet_text

LABELS = {NEW: "moved", FILLED: "moved, in place of the unclaimed directory shop with the same external id",
          HERE: "already here: left as it is", CONFLICT: "not moved", REFUSED: "not moved"}


def plain(text):
    """A name from the file as the terminal shows it: an escape sequence in it must not rewrite the report."""
    return "".join("?" if unicodedata.category(char) in ("Cc", "Cf") and char != "\u200d" else char for char in str(text))


class Command(BaseCommand):
    help = "Brings here the shops of a file written by export_shops: a dry run unless --apply"

    def add_arguments(self, parser):
        parser.add_argument("path")
        parser.add_argument("--apply", action="store_true", help="Write the shops; without it nothing is written")
        parser.add_argument("--owners", metavar="CSV", help="The list of owners to tell, with their one-time links and "
                            "messages (needed with --apply)")
        parser.add_argument("--new-codes", action="store_true", help="New one-time links for the shops already moved "
                            "that still wait for their owner: the old links stop working")
        parser.add_argument("--only", metavar="ID", help="Only this shop of the file, and no other: its id here (as "
                            "in the administration) or its id in the file")
        parser.add_argument("--language", default=settings.LANGUAGE_CODE, help="The language of the owners' messages")

    def handle(self, *args, path, apply, owners, language, new_codes, only=None, **options):
        if apply and not owners:
            raise CommandError("--apply needs --owners: the one-time links for the owners are written only there")
        if language not in dict(settings.LANGUAGES):
            raise CommandError(f"Language {language!r} is not one of this site: {', '.join(dict(settings.LANGUAGES))}")
        try:
            with open(path, "rb") as file:
                # One byte more than the limit, so read() can tell a file that is too large
                data = file.read(MAX_BYTES + 1)
        except OSError as error:
            raise CommandError(f"Cannot read {path}: {error.strerror}") from None
        try:
            document = read(data)
        except FileRefused as error:
            raise CommandError(f"File refused, nothing written: {error}") from None
        if only:
            # Before the list of owners is opened: a wrong id leaves no empty file behind
            chosen = self.only_id(only)
            document = {**document, "shops": [row for row in document["shops"] if row["id"] == chosen]}
            if not document["shops"]:
                raise CommandError(f"The shop {only} is not in {path}: it came here with another file. Nothing written.")
        # Opened before the import: a name already taken must not leave the links of a done import nowhere
        try:
            listing = os.fdopen(os.open(owners, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", newline="",
                                encoding="utf-8") if owners else None
        except FileExistsError:
            raise CommandError(f"{owners} already exists: choose another name") from None
        try:
            if new_codes and not apply:
                self.stdout.write("--new-codes makes links only with --apply: in a dry run nothing changes.")
            outcomes = import_document(document, apply=apply, new_codes=new_codes and apply)
            self.report(document, outcomes, apply)
            if listing:
                self.owners_list(listing, outcomes, language)
                self.stdout.write(f"The list of owners is in {owners}: it holds their emails and one-time links, "
                                  "keep it private.")
        finally:
            if listing:
                listing.close()

    def only_id(self, only):
        """The shop's id in the file: from its id here, as the administration and the dashboard show it, or from the
        file's id itself. A shop born here, or not moved, is refused."""
        try:
            chosen = str(uuid.UUID(only))
        except ValueError:
            raise CommandError(f"--only takes the shop's id here or its id in the file: {plain(only)}") from None
        here = Shop.objects.filter(pk=chosen).values_list("moved_from", flat=True).first()
        if here is None:
            return chosen
        if not here:
            raise CommandError(f"The shop {chosen} was not moved here from another site. Nothing written.")
        return here

    def report(self, document, outcomes, apply):
        write = self.stdout.write
        write(("Import done" if apply else "Dry run, nothing written: add --apply to write") +
              f". From {plain(document['site'])} ({plain(document['network'])}) to {settings.PUBLIC_URL} ({settings.NETWORK}), "
              f"exported {plain(document['exported'])}.")
        counts = {status: sum(outcome.status == status for outcome in outcomes) for status in LABELS}
        write(f"{counts[NEW] + counts[FILLED]} moved, {counts[HERE]} already here, "
              f"{counts[CONFLICT] + counts[REFUSED]} not moved, of {len(outcomes)}.")
        write("Not moved: the certificate starts again with payments on this network.")
        for outcome in outcomes:
            write(f"- {plain(outcome.name) or '(no name)'} [{outcome.row['id']}]: {LABELS[outcome.status]}")
            for reason in outcome.reasons:
                write(f"    why: {plain(reason)}")
            for note in outcome.notes:
                write(f"    note: {plain(note)}")
            if outcome.status in (NEW, FILLED):
                if outcome.shop.owner_id:
                    owner = f"given to a new account for {plain(outcome.email)}"
                elif outcome.sign_in:
                    owner = "waits for its owner: one-time link in the list of owners"
                else:
                    owner = "no owner"
                wallet = "register, Lightning address, on-chain key" if features.enabled(features.ONCHAIN) \
                    else "register, Lightning address"
                todo = f"to connect the wallet ({wallet}), print the new sign" \
                    if outcome.shop.moved_register else "nothing to connect"
                write(f"    {owner}; {todo}")
            elif outcome.code:
                write("    new one-time link in the list of owners; the old one no longer works")

    def owners_list(self, file, outcomes, language):
        rows = [outcome for outcome in outcomes
                if outcome.sign_in and (outcome.status in (NEW, FILLED) or outcome.code)]
        writer = csv.writer(file)
        writer.writerow(["shop", "email", "sign_in", "to_do", "link", "message"])
        for outcome in rows:
            todo = "connect_wallet" if outcome.shop.moved_register else "nothing"
            if not outcome.shop.owner_id:
                todo = "open_link" if todo == "nothing" else f"open_link_then_{todo}"
            writer.writerow([spreadsheet_text(outcome.name), spreadsheet_text(outcome.email), outcome.sign_in, todo,
                             claim_link(outcome, language),
                             spreadsheet_text(owner_message(outcome, settings.PUBLIC_URL, language))])
        self.stdout.write(f"{len(rows)} owners to tell.")

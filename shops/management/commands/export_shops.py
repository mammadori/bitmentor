"""Writes the shops of this site to a file that import_moved_shops reads on another bitmentor site (shops/moving.py).

The file holds the shops' details (the business name and the VAT number too) and their owners' login key and recovery
email: personal data, written readable by its owner only. Nothing about payments, sales or secrets."""
import json
import os

from django.core.management.base import BaseCommand, CommandError

from shops.moving import export


class Command(BaseCommand):
    help = "Writes the shops to a JSON file for import_moved_shops on another site: details and owners, never payments"

    def add_arguments(self, parser):
        parser.add_argument("path", help="The file to write; it must not exist yet")

    def handle(self, *args, path, **options):
        document, lost = export()
        # O_EXCL: never over an existing file, and never readable by others (it holds the owners' emails)
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise CommandError(f"{path} already exists: choose another name") from None
        except OSError as error:
            raise CommandError(f"Cannot write {path}: {error.strerror}") from None
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(document, file, ensure_ascii=False, indent=1, sort_keys=True)
            file.write("\n")
        self.stdout.write(f"{len(document['shops'])} shops written to {path} (format {document['format']} "
                          f"version {document['version']}, from {document['network']}).")
        for shop in lost:
            self.stdout.write(f"- {shop.name} ({shop.pk}): its owner signs in with a password or has neither a wallet "
                              "key nor an email; the shop moves without an owner.")
        self.stdout.write("The file holds the owners' emails and the shops' business details: keep it private and delete it after the import.")

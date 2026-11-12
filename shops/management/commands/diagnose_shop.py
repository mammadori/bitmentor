"""What to tell a merchant who calls: everything that matters about one shop's register, and what to do.

`manage.py diagnose_shop <shop>`, where <shop> is the shop's id, the owner's recovery email or the shop's public email,
the number in the imported list, the shop's id on the site it moved from, or a piece of the name. One line per check, with
its sign, the sentence for the merchant in the community's first language, what the helper does after "→" when the
check has something to do, and, below, the detail for whoever helps. `--json` for machines (the same: code, level,
say, detail, do).

Exit code: 0 nothing wrong (warnings allowed), 1 at least one problem, 2 no single shop found (none, or several:
they are listed). Read-only: see shops/diagnosis.py. docs/helpdesk.md explains every line.
"""
import json
import sys

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone, translation
from django.utils.translation import gettext as _

from shops.diagnosis import OK, PROBLEM, TOTAL_SECONDS, WARNING, diagnose, find_shops, worst
from shops.notifications import plain

SIGNS = {OK: "✓", WARNING: "!", PROBLEM: "✗"}
NOT_FOUND = 2


class Command(BaseCommand):
    help = "Tells in plain words what is wrong with a shop's register and what to do (read-only)"
    # The output is read on the phone with a merchant: the site's own warnings belong to `manage.py check`
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("shop", help="id (or the id on the site it moved from), recovery or public email, imported list number, or a piece of the name")
        parser.add_argument("--json", action="store_true", help="machine-readable output")
        parser.add_argument("--language", default="", help="language of the sentences (default: the community's first)")
        parser.add_argument("--seconds", type=float, default=TOTAL_SECONDS, help="time limit for all the checks")

    def handle(self, *args, **options):
        language = options["language"] or settings.LANGUAGE_CODE
        shops = find_shops(options["shop"])
        if len(shops) != 1:
            with translation.override(language):
                self.not_one(shops, options)
            sys.exit(NOT_FOUND)
        shop = shops[0]
        with translation.override(language):
            findings = diagnose(shop, options["seconds"])
        level = worst(findings)
        if options["json"]:
            self.stdout.write(json.dumps({
                "shop": {"id": str(shop.id), "name": shop.name}, "language": language, "worst": level,
                "at": timezone.now().isoformat(timespec="seconds"),
                "findings": [finding.as_dict() for finding in findings]}, ensure_ascii=False, indent=2))
        else:
            self.stdout.write(f"{plain(shop.name)} · {plain(shop.city) or '-'} · {shop.id}")
            for finding in findings:
                self.stdout.write(f"{SIGNS[finding.level]} {finding.say}")
                if finding.do:
                    self.stdout.write(f"    → {finding.do}")
                self.stdout.write(f"    [{finding.code}] {finding.detail}")
        if level == PROBLEM:
            sys.exit(1)

    def not_one(self, shops, options):
        if options["json"]:
            self.stdout.write(json.dumps({"found": [{"id": str(shop.id), "name": shop.name, "city": shop.city}
                                                    for shop in shops]}, ensure_ascii=False, indent=2))
            return
        if not shops:
            self.stderr.write(_("No shop found for “{text}”.").format(text=options["shop"]))
            return
        self.stderr.write(_("{count} shops found: run it again with the id of the right one.").format(count=len(shops)))
        for shop in shops:
            self.stderr.write(f"  {shop.id}  {plain(shop.name)} · {plain(shop.city) or '-'}")

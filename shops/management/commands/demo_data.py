"""manage.py demo_data: a training site in one command (shops/demo.py, docs/features/demo.md)."""
from django.core.management.base import BaseCommand, CommandError

from shops import demo


class Command(BaseCommand):
    help = ("Fills a test installation with a small made-up community in every state a merchant or the association "
            "meets, or puts it back as it was (the dates move to today). --remove takes it away. Never on mainnet "
            "or in production mode; asks nothing of any provider.")

    def add_arguments(self, parser):
        parser.add_argument("--remove", action="store_true", help="remove every demo shop, owner, and only those")
        parser.add_argument("--links", action="store_true",
                            help="print the one-time sign-in link of each demo owner (15 minutes, once) and the link "
                                 "that takes the shop waiting for its owner; for a site whose email goes to the console")

    def handle(self, *args, remove, links, **options):
        if remove and links:
            raise CommandError("--remove and --links do not go together.")
        try:
            if remove:
                self.removed()
            elif links:
                self.links()
            else:
                self.made()
        except demo.Refused as error:
            raise CommandError(str(error)) from error

    def made(self):
        made = demo.populate()
        self.stdout.write(f"Demo community ready: {len(made['shops'])} shops, {len(made['owners'])} owners.\n")
        for spec in demo.SHOPS:
            self.stdout.write(f"  {spec['name']:<26} {spec['story']}")
        self.stdout.write("\nSign in as an owner: manage.py demo_data --links. The association's list: /admin/today/. "
                          "Run this command again to put everything back as it was, with the dates moved to today. "
                          "Take it all away: manage.py demo_data --remove.")

    def links(self):
        found = demo.links()
        if not found["owners"]:
            raise CommandError("There is no demo community: run manage.py demo_data first.")
        self.stdout.write("One-time sign-in links, valid for 15 minutes (a new run of --links makes new ones):\n")
        for owner in found["owners"]:
            self.stdout.write(f"  {owner['name']} ({', '.join(owner['shops'])})\n    {owner['url']}")
        for claim in found["claim"]:
            self.stdout.write(f"\nTo take {claim['shop']}, sign in as an owner first, then open:\n    {claim['url']}")

    def removed(self):
        count = demo.remove()
        self.stdout.write(f"Removed {count['shops']} demo shops, {count['owners']} owners. Nothing else was touched.")

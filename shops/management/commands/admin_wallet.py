"""The wallets that sign the staff accounts in to /admin/ (config/admin_wallet.py, ADMIN_WALLET_SIGN_IN), from the machine.

    manage.py admin_wallet list               the switch and the level, which staff accounts have wallets, how many
    manage.py admin_wallet reset <username>   remove all the wallets of an account: its phone is lost

Whoever has a shell on the machine has everything already, and removes here the wallets of a lost phone. The account
then signs in with its password (and its second factor, if it has one); its sessions opened with those wallets end at
their next page. The reset is written where the change of a password is (config/second_factor.py, `record_change`) and
told to the people who look after the site. Nothing here prints a key."""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from config import admin_wallet, second_factor
from shops.management.commands.admin_totp import staff_accounts
from shops.management.commands.moderators import plain
from shops.models import AdminWallet

User = get_user_model()


def where_it_stands():
    """One line: the switch, the level, and what they mean for the wallets."""
    level = f"ADMIN_SECOND_FACTOR is {second_factor.mode()}"
    if not admin_wallet.on():
        return f"ADMIN_WALLET_SIGN_IN is off ({level}): no wallet signs in to /admin/."
    if not admin_wallet.signs_in():
        return (f"ADMIN_WALLET_SIGN_IN is on, but {level}: no wallet signs in to /admin/ at this level, the linked ones "
                "stay unused.")
    return (f"ADMIN_WALLET_SIGN_IN is on and {level}: a linked wallet signs in to /admin/ an account without a second "
            "factor, in place of its password.")


class Command(BaseCommand):
    help = "The wallets that sign the staff accounts in to /admin/: list them, or reset an account's"

    def add_arguments(self, parser):
        actions = parser.add_subparsers(dest="action", required=True, metavar="{list,reset}")
        actions.add_parser("list", help="The switch, the level, and which staff accounts have wallets")
        actions.add_parser("reset", help="Remove all the wallets of an account (a lost phone)").add_argument(
            "username", help="The account's sign-in name, as in /admin/ (Users)")

    def handle(self, *args, action, username=None, **options):
        {"list": self.show, "reset": self.reset}[action](username)

    def show(self, username):
        counts = {}
        for user_id in AdminWallet.objects.values_list("user_id", flat=True):
            counts[user_id] = counts.get(user_id, 0) + 1
        accounts = list(staff_accounts())
        self.stdout.write(where_it_stands())
        self.stdout.write(f"Staff accounts: {len(accounts)}, with a wallet: {sum(1 for user in accounts if counts.get(user.pk))}")
        for user in accounts:
            self.stdout.write(f"  {plain(user.get_username())}  {counts.get(user.pk, 0)}")
        # A wallet of an account that is no longer staff signs in nobody, and stays until it is reset
        others = sorted(set(counts) - {user.pk for user in accounts})
        if others:
            self.stdout.write(f"Accounts no longer staff with a wallet (they do not sign in): {len(others)}")

    def reset(self, username):
        try:
            user = User._default_manager.get_by_natural_key(username)
        except User.DoesNotExist:
            raise CommandError(f"There is no account called {plain(username)!r}. Nothing changed.") from None
        name = plain(user.get_username())
        told = []
        with transaction.atomic():
            removed, _kinds = AdminWallet.objects.filter(user=user).delete()
            if removed:
                second_factor.record_change(user, "Wallets removed with manage.py admin_wallet reset",
                                            admin_wallet.RESET, wait=True, code=admin_wallet.CHANGE,
                                            subject=admin_wallet.SUBJECT,
                                            report=lambda where, problems: told.append((where, problems)))
        if not removed:
            self.stdout.write(f"{name} has no wallet. Nothing changed.")
            return
        self.stdout.write(f"{name}: {removed} wallet{'s' if removed != 1 else ''} removed; the account signs in with "
                          "its password, and can link a new wallet in /admin/second-factor/.")
        for where, problems in told:
            self.stdout.write(f"told: {where}" if where else f"not told: {'; '.join(problems) or 'no way to tell them'}")

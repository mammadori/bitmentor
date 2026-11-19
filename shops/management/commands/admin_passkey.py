"""The passkeys of the staff accounts' sign-in to /admin/ (config/second_factor.py, config/webauthn.py), from the machine.

    manage.py admin_passkey list               which staff accounts have passkeys, and how many
    manage.py admin_passkey reset <username>   remove all the passkeys of an account: its phone or key is lost

Whoever has a shell on the machine has everything already, and removes here the passkeys of a lost phone or key. The
account then signs in as its other second factors say: a TOTP code where the mode accepts one, the password alone if
it has nothing else (with ADMIN_SECOND_FACTOR required or passkey it is sent to add one at once). Its sessions opened
with those passkeys end at their next page. The reset is written where the change of a password is
(config/second_factor.py, `record_change`). Nothing here prints a key."""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from config import second_factor
from shops.management.commands.admin_totp import staff_accounts
from shops.management.commands.moderators import plain
from shops.models import AdminPasskey, AdminPasskeyHandle

User = get_user_model()


class Command(BaseCommand):
    help = "The passkeys of the staff accounts' sign-in to /admin/: list them, or reset an account's"

    def add_arguments(self, parser):
        actions = parser.add_subparsers(dest="action", required=True, metavar="{list,reset}")
        actions.add_parser("list", help="Which staff accounts have passkeys")
        actions.add_parser("reset", help="Remove all the passkeys of an account (a lost phone or key)").add_argument(
            "username", help="The account's sign-in name, as in /admin/ (Users)")

    def handle(self, *args, action, username=None, **options):
        {"list": self.show, "reset": self.reset}[action](username)

    def show(self, username):
        counts = {}
        for user_id in AdminPasskey.objects.values_list("user_id", flat=True):
            counts[user_id] = counts.get(user_id, 0) + 1
        accounts = list(staff_accounts())
        self.stdout.write(f"ADMIN_SECOND_FACTOR is {second_factor.mode()}. Staff accounts: {len(accounts)}, with a "
                          f"passkey: {sum(1 for user in accounts if counts.get(user.pk))}")
        for user in accounts:
            self.stdout.write(f"  {plain(user.get_username())}  {counts.get(user.pk, 0)}")

    def reset(self, username):
        try:
            user = User._default_manager.get_by_natural_key(username)
        except User.DoesNotExist:
            raise CommandError(f"There is no account called {plain(username)!r}. Nothing changed.") from None
        name = plain(user.get_username())
        told = []
        with transaction.atomic():
            removed, _kinds = AdminPasskey.objects.filter(user=user).delete()
            # A new passkey gets a new opaque id: nothing ties it to the lost ones
            AdminPasskeyHandle.objects.filter(user=user).delete()
            if removed:
                second_factor.record_change(user, "Passkeys removed with manage.py admin_passkey reset",
                                            second_factor.PASSKEY_RESET, wait=True,
                                            report=lambda where, problems: told.append((where, problems)))
        if not removed:
            self.stdout.write(f"{name} has no passkey. Nothing changed.")
            return
        left = second_factor.accepted_factors(user)
        if left.totp is not None:
            after = "it signs in with its code from the app."
        elif left.bootstrap is not None:
            after = ("the next sign-in asks for its code from the app once, only to add a new passkey "
                     f"({self.mode_name()}).")
        elif second_factor.required():
            after = f"the next sign-in asks for the password alone, then sends to add a new one ({self.mode_name()})."
        else:
            after = "the next sign-in asks for the password alone; the account can add a new one in /admin/second-factor/."
        self.stdout.write(f"{name}: {removed} passkey{'s' if removed != 1 else ''} removed; {after}")
        # The message went after the commit, here and now: whether it arrived, and where
        for where, problems in told:
            self.stdout.write(f"told: {where}" if where else f"not told: {'; '.join(problems) or 'no way to tell them'}")

    @staticmethod
    def mode_name():
        return f"ADMIN_SECOND_FACTOR={second_factor.mode()}"

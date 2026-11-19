"""The second factor of the staff accounts' sign-in to /admin/ (config/second_factor.py), from the machine.

    manage.py admin_totp list               which staff accounts have one (a code from an app), and which do not
    manage.py admin_totp reset <username>   remove an account's code from an app: its phone is lost

There are no recovery codes: whoever has a shell on the machine has everything already, and removes the second factor
of a lost phone here (passkeys: `admin_passkey`). The account then signs in with its passkeys, if it has any, or with
the password alone (with ADMIN_SECOND_FACTOR required or passkey it is sent to add a new one at once), and its
sessions opened with the old one end at their next page. The reset is written where the
change of a password is (config/second_factor.py, `record_change`). Nothing here reads or prints a secret."""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from config import second_factor
from shops.management.commands.moderators import plain
from shops.models import AdminSecondFactor

User = get_user_model()


def staff_accounts():
    """The accounts that can sign in to /admin/: active, staff (a superuser without staff status cannot)."""
    return User.objects.filter(is_active=True, is_staff=True).order_by("username")


class Command(BaseCommand):
    help = "The second factor of the staff accounts' sign-in to /admin/: list them, or reset an account's"

    def add_arguments(self, parser):
        actions = parser.add_subparsers(dest="action", required=True, metavar="{list,reset}")
        actions.add_parser("list", help="Which staff accounts have a second factor")
        actions.add_parser("reset", help="Remove an account's second factor (a lost phone)").add_argument(
            "username", help="The account's sign-in name, as in /admin/ (Users)")

    def handle(self, *args, action, username=None, **options):
        {"list": self.show, "reset": self.reset}[action](username)

    def show(self, username):
        with_factor = set(AdminSecondFactor.objects.filter(confirmed__isnull=False).values_list("user_id", flat=True))
        accounts = list(staff_accounts())
        self.stdout.write(f"ADMIN_SECOND_FACTOR is {second_factor.mode()}. "
                          f"Staff accounts: {len(accounts)}, with a second factor: "
                          f"{sum(1 for user in accounts if user.pk in with_factor)}")
        for user in accounts:
            self.stdout.write(f"  {plain(user.get_username())}  {'on' if user.pk in with_factor else 'off'}")

    def reset(self, username):
        try:
            user = User._default_manager.get_by_natural_key(username)
        except User.DoesNotExist:
            raise CommandError(f"There is no account called {plain(username)!r}. Nothing changed.") from None
        name = plain(user.get_username())
        told = []
        with transaction.atomic():
            confirmed = AdminSecondFactor.objects.filter(user=user, confirmed__isnull=False).exists()
            removed, _kinds = AdminSecondFactor.objects.filter(user=user).delete()
            if confirmed:
                second_factor.record_change(user, "Second factor removed with manage.py admin_totp reset",
                                            second_factor.RESET, wait=True,
                                            report=lambda where, problems: told.append((where, problems)))
        if not confirmed:
            self.stdout.write(f"{name} has no second factor{' (a set-up not finished was removed)' if removed else ''}. "
                              "Nothing else changed.")
            return
        if second_factor.accepted_factors(user):
            after = ". The account signs in with its passkeys."
        elif second_factor.required():
            after = (". The next sign-in asks for the password alone, then sends to add a new one "
                     f"(ADMIN_SECOND_FACTOR={second_factor.mode()}).")
        else:
            after = ". The next sign-in asks for the password alone; the account can set up a new one in /admin/second-factor/."
        self.stdout.write(f"{name}: the second factor is removed{after}")
        # The message went after the commit, here and now: whether it arrived, and where
        for where, problems in told:
            self.stdout.write(f"told: {where}" if where else f"not told: {'; '.join(problems) or 'no way to tell them'}")

"""Marks the password an account has now as temporary: whoever signs in to the admin with it is sent to change it
(config/temporary.py). For the accounts of the staff, whose password the association hands over in person.

    manage.py temporary_password <username>           mark the current password as temporary
    manage.py temporary_password <username> --clear   take the mark away

The command reads and prints no password, and not the hash either: it only copies the account's current hash into its
Profile. Another password, from the person or from the admin's user form, is another hash and ends the mark by itself;
to hand over a new password, set it first and run this command after."""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from config import temporary


class Command(BaseCommand):
    help = "Marks the current password of an account as temporary: it must be changed at the first sign-in to /admin/"

    def add_arguments(self, parser):
        parser.add_argument("username")
        parser.add_argument("--clear", action="store_true", help="take the mark away instead")

    def handle(self, *args, username, clear, **options):
        User = get_user_model()
        try:
            user = User._default_manager.get_by_natural_key(username)
        except User.DoesNotExist:
            raise CommandError("There is no account with that name.") from None
        if clear:
            temporary.clear(user)
            self.stdout.write(f"{username}: the password is no longer marked as temporary.")
            return
        if not temporary.has_password(user):
            raise CommandError("This account has no password to mark: set one first.")
        if not user.is_staff:
            # Marked all the same: it holds as soon as the account is staff. Only staff members sign in with a password
            self.stderr.write(f"{username} is not a staff account: the mark has no effect until it is.")
        temporary.mark(user)
        self.stdout.write(f"{username}: the password is temporary. The first sign-in to /admin/ asks for a new one.")

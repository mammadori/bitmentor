"""Who the moderators are: the members of the group of shops/moderators.py.

    manage.py moderators list             the group's rights, its members, and what is wrong with either
    manage.py moderators sync             put the group's rights right now, as `migrate` does at every start
    manage.py moderators add <name>       an existing account becomes a moderator (and staff, if it was not)
    manage.py moderators remove <name>    it stops being one (and stops being staff, if nothing else made it so)

Every change goes through the models (`user.save()`, `user.groups.add()`), as a form of the admin does, never through
`update()` or SQL: the register of the changes to the owners' accounts (shops/guard.py) sees it, and tells the owner and
the association, whenever the account belongs to a shop's owner. `add` refuses such an account: a staff account is a
visitor on every page of a shop (config/guard.py), so making a shop's owner a moderator would take the shop's panel away
from them. `remove` accepts it, since taking rights away is always allowed, and the register says so.

All or nothing: a change is made in one transaction."""
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from shops import moderators
from shops.models import Shop

User = get_user_model()


def plain(text):
    """A name as the terminal shows it: an escape sequence in a name must not rewrite the report."""
    return "".join("?" if not char.isprintable() else char for char in str(text))


class Command(BaseCommand):
    help = "The moderators' group: list it, add an account to it, take an account out of it"

    def add_arguments(self, parser):
        actions = parser.add_subparsers(dest="action", required=True, metavar="{list,add,remove}")
        actions.add_parser("list", help="The group's rights and members")
        actions.add_parser("sync", help="Put the group's rights right now, as `migrate` does at every start")
        actions.add_parser("add", help="Make an existing account a moderator").add_argument(
            "username", help="The account's sign-in name, as in /admin/ (Users)")
        actions.add_parser("remove", help="Take an account out of the moderators").add_argument(
            "username", help="The account's sign-in name")

    def handle(self, *args, action, username=None, **options):
        {"list": self.show, "sync": self.sync, "add": self.add, "remove": self.remove}[action](username)

    def account(self, username):
        try:
            return User.objects.get_by_natural_key(username)
        except User.DoesNotExist:
            raise CommandError(f"There is no account called {plain(username)!r}. A superuser makes it in /admin/, under "
                               "Users: a name and a password. Nothing changed.") from None

    def shops(self, user):
        return list(Shop.objects.filter(owner=user).order_by("name").values_list("name", flat=True))

    def show(self, username):
        group = Group.objects.filter(name=moderators.GROUP).first()
        if group is None:
            self.stdout.write(f"The group {moderators.GROUP} does not exist yet: `manage.py migrate` makes it "
                              "(`manage.py moderators add <name>` too).")
            return
        missing, extra = moderators.difference(group)
        have = moderators.wanted() - missing
        self.stdout.write(f"Group {group.name}: what its members can do in /admin/")
        for label, codename, what in moderators.PERMISSIONS:
            if (label, codename) in missing:
                self.stdout.write(f"  MISSING  {label}.{codename}: {what}")
            elif (label, codename) in have:
                self.stdout.write(f"  {label}.{codename}: {what}")
        for label, codename in sorted(extra):
            self.stdout.write(f"  NOT ON THE LIST  {label}.{codename}: `manage.py migrate` takes it away")
        members = list(group.user_set.order_by("username"))
        self.stdout.write(f"Members: {len(members)}" + ("" if members else ". `manage.py moderators add <name>`"))
        for user in members:
            notes = []
            if not user.is_active:
                notes.append("switched off: it cannot sign in")
            if not user.is_staff:
                notes.append("not staff: it cannot open /admin/")
            if user.is_superuser:
                notes.append("superuser: it has every right, with or without the group")
            if not user.has_usable_password():
                notes.append("no password: it cannot sign in at /admin/")
            owned = self.shops(user)
            if owned:
                notes.append(f"owns {len(owned)} shop(s): as staff it cannot use them")
            # What else the account has: a moderator with more than the group is more than a moderator
            others = list(user.groups.exclude(pk=group.pk).order_by("name").values_list("name", flat=True))
            if others:
                notes.append(f"also in group {', '.join(plain(name) for name in others)}")
            own = user.user_permissions.count()
            if own:
                notes.append(f"{own} right(s) of its own")
            self.stdout.write(f"  {plain(user.get_username())}" + (f"  ({'; '.join(notes)})" if notes else ""))

    def sync(self, username):
        group = Group.objects.filter(name=moderators.GROUP).first()
        missing, extra = moderators.difference(group) if group else (moderators.wanted(), set())
        moderators.sync()
        if not group:
            self.stdout.write(f"The group {moderators.GROUP} did not exist: made, with {len(missing)} rights.")
        elif missing or extra:
            for label, codename in sorted(missing):
                self.stdout.write(f"  given back  {label}.{codename}")
            for label, codename in sorted(extra):
                self.stdout.write(f"  taken away  {label}.{codename}")
            self.stdout.write(f"The group {moderators.GROUP} now has exactly the rights of the list.")
        else:
            self.stdout.write(f"The group {moderators.GROUP} already has exactly the rights of the list. Nothing changed.")

    def add(self, username):
        user = self.account(username)
        name = plain(user.get_username())
        owned = self.shops(user)
        if owned:
            raise CommandError(
                f"{name} owns {len(owned)} shop(s): {', '.join(plain(shop) for shop in owned[:3])}"
                f"{', …' if len(owned) > 3 else ''}. A staff account is a visitor on every page of a shop, so a moderator "
                "cannot also use their shop's panel or register. Use a second account for the moderators' work. "
                "Nothing changed.")
        if not user.is_active:
            raise CommandError(f"The account {name} is switched off: it could not sign in. Switch it on in /admin/ first. "
                               "Nothing changed.")
        if user.is_superuser:
            raise CommandError(f"{name} is a superuser: it has every right already, and a group would add nothing. "
                               "Nothing changed.")
        with transaction.atomic():
            # The group is put right first, as `migrate` does: made if somebody deleted it, and with the rights of the
            # list and no others, so nobody joins a group that has more than the work asks
            moderators.sync()
            group = Group.objects.get(name=moderators.GROUP)
            made_staff = not user.is_staff
            joined = not user.groups.filter(pk=group.pk).exists()
            if made_staff:
                user.is_staff = True
                user.save(update_fields=["is_staff"])
            if joined:
                user.groups.add(group)
        if not (made_staff or joined):
            self.stdout.write(f"{name} is a moderator already. Nothing changed.")
            return
        self.stdout.write(f"{name} is a moderator" + (" and now has access to /admin/." if made_staff else "."))
        if not user.has_usable_password():
            self.stdout.write(f"{name} has no password: `manage.py changepassword {name}` gives one, or they cannot sign "
                              "in at /admin/.")

    def remove(self, username):
        user = self.account(username)
        name = plain(user.get_username())
        group = Group.objects.filter(name=moderators.GROUP).first()
        if group is None or not user.groups.filter(pk=group.pk).exists():
            self.stdout.write(f"{name} is not a moderator. Nothing changed.")
            return
        with transaction.atomic():
            user.groups.remove(group)
            # Staff only because of this group: with nothing else to see in /admin/, it goes too. A superuser, or
            # someone with another group or a right of their own, keeps it
            left = user.is_staff and not user.is_superuser and not user.groups.exists() and not user.user_permissions.exists()
            if left:
                user.is_staff = False
                user.save(update_fields=["is_staff"])
        self.stdout.write(f"{name} is not a moderator any more" + (" and no longer has access to /admin/." if left else "."))
        if user.is_staff:
            self.stdout.write(f"{name} stays staff: it has other rights. `/admin/auth/user/` shows which.")

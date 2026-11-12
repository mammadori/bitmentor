"""The moderators' group: the rights of the volunteers who look after the shops, kept in one place and by the code.

A moderator reads `/admin/today/`, answers the merchants' requests for help, reads the register of the changes to the
owners' accounts. Nothing else. Before this
group, each right was given by hand, one by one, in `/admin/`: a right forgotten meant a link that answered 403, a right
given in excess meant a volunteer who could do more than the work asks.

The group is called GROUP and has exactly the rights of PERMISSIONS, and no other. `manage.py migrate` (which the site
runs at every start) makes it and puts it right again: a right added to the group by hand is taken away, one that is
missing comes back, a group someone deleted is made again. To give a volunteer more, give the volunteer a group of their
own: this one is the definition of the moderator, the same on every installation.

Why not a data migration: the rights of a model are born after all the migrations have run (the `post_migrate` of
`django.contrib.auth`), so on a new database a migration finds none of them; and a migration is written once, while the
list changes with the work. `post_migrate` runs on a new database and on one that is brought up to date, and each time
asks the apps for their rights first (`create_permissions`), so it does not depend on the order of the apps.

An installation that does not have an app of the list simply has no
rights of that app to give: they are skipped, and the rest of the group is made all the same.

What is not here, on purpose: any right to change a shop, an owner, an account, a key or an address. The destination of
a shop's money must not become easier to change for whoever runs the site, and a moderator's work does not need it:
suspending a shop, marking it historic, writing a closure, revoking a certificate and handing a shop to an owner are
for the people who have all the rights (docs/features/staff-and-shops.md).
"""
import logging
from functools import reduce
from operator import or_

from django.apps import apps as global_apps
from django.db import DEFAULT_DB_ALIAS, router, transaction
from django.db.models import Q

logger = logging.getLogger(__name__)

GROUP = "Moderators"

# (app, right, what the moderator does with it). A right to change always comes with the one to see: the dashboard
# decides which links to show by the right to see, and Django's admin by either.
PERMISSIONS = (
    # /admin/today/ (the permission it asks) and the shop pages it links to, which open to read, never to change
    ("shops", "view_shop", "Read /admin/today/ and open the shops it points to, to read only"),
    # The merchants' requests for help: read them with their diagnosis, mark them solved or waiting again
    ("shops", "view_helprequest", "Open the requests for help, and the link to them on /admin/today/"),
    ("shops", "change_helprequest", "The actions Mark as solved and Mark as waiting again"),
    # The register of what the staff changed in the accounts of shop owners: a second pair of eyes on the people who
    # can change them
    ("shops", "view_accountchange", "Read the register of the changes to the owners' accounts"),
)


def wanted(apps=global_apps):
    """The rights of the list that this installation can give: the ones of apps it has."""
    found = set()
    for label, codename, _what in PERMISSIONS:
        try:
            apps.get_app_config(label)
        except LookupError:
            continue
        found.add((label, codename))
    return found


def sync(apps=global_apps, using=DEFAULT_DB_ALIAS):
    """Makes the group, and gives it exactly the rights of PERMISSIONS that this installation has. Returns the rights it
    now has, as a set of (app, right), or None if the database has no accounts' tables to put them in.

    `apps` is the registry whose models are used: the one `migrate` passes at its end (the state of the migrations), or
    the real one."""
    try:
        Group = apps.get_model("auth", "Group")
        Permission = apps.get_model("auth", "Permission")
    except LookupError:
        return None
    if not router.allow_migrate_model(using, Group):
        return None
    from django.contrib.auth.management import create_permissions

    expected = wanted(apps)
    # The rights of a model are made after the migrations, app by app: ask the apps of the list for theirs, so that
    # it does not matter which comes first
    for label in sorted({label for label, _codename in expected}):
        # The app's own configuration, as the signal passes it: the registry of `migrate` holds only stubs of them
        create_permissions(global_apps.get_app_config(label), verbosity=0, using=using, apps=apps)
    rights = Permission.objects.using(using).filter(
        reduce(or_, (Q(content_type__app_label=label, codename=codename) for label, codename in expected), Q(pk__in=[])))
    found = {(right.content_type.app_label, right.codename) for right in rights.select_related("content_type")}
    for label, codename in sorted(expected - found):
        # A right the app should have and does not: a mistake in the list, which the tests catch. It must not stop
        # the site from starting
        logger.error("The right %s.%s of the moderators' group does not exist", label, codename)
    with transaction.atomic(using=using):
        group, _made = Group.objects.using(using).get_or_create(name=GROUP)
        before = set(group.permissions.values_list("content_type__app_label", "codename"))
        group.permissions.set(rights)
    if before - found:
        # Somebody gave the group more than the work asks: it is taken away, and the log says so. At ERROR, the only level
        # the site's logging lets through (settings.LOGGING): a warning would never be seen
        logger.error("Rights taken away from the moderators' group, which are not on the list: %s",
                     ", ".join(f"{label}.{codename}" for label, codename in sorted(before - found)))
    return found


def on_post_migrate(sender, apps=global_apps, using=DEFAULT_DB_ALIAS, **kwargs):
    """`migrate` ends: the group is made or put right. A failure is logged and does not stop the start of the site, which
    an error here would do at every start: the group stays as it was, which gives nobody more than it did."""
    try:
        sync(apps=apps, using=using)
    except Exception:
        logger.exception("The moderators' group could not be updated")


def difference(group):
    """How the group differs from the list: (rights it lacks, rights it has beyond the list), as sets of (app, right)."""
    have = set(group.permissions.values_list("content_type__app_label", "codename"))
    expected = wanted()
    return expected - have, have - expected

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

from django.apps import apps as global_apps
from django.db import DEFAULT_DB_ALIAS, router

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


def listed():
    """The rights the group should have: the list above, or what `staff_groups` in community.yml gives it
    (shops/staff_groups.py). None when community.yml names other groups and not this one: then nobody keeps it."""
    from . import staff_groups

    groups = staff_groups.configured()[0]
    if GROUP not in groups:
        return None
    return groups[GROUP]


def wanted(apps=global_apps):
    """The rights of the list that this installation can give: the ones of apps it has."""
    from . import staff_groups

    return staff_groups.wanted(listed() or set(), apps)


def sync(apps=global_apps, using=DEFAULT_DB_ALIAS):
    """Makes the group and gives it exactly the rights it should have (`listed`), as `migrate` does. Returns the rights
    it now has, or None without the accounts' tables or when community.yml no longer keeps this group."""
    from . import staff_groups

    try:
        Group = apps.get_model("auth", "Group")
        apps.get_model("auth", "Permission")
    except LookupError:
        return None
    if not router.allow_migrate_model(using, Group) or listed() is None:
        return None
    return staff_groups.sync_group(GROUP, wanted(apps), apps, using)


def difference(group):
    """How the group differs from the list: (rights it lacks, rights it has beyond the list), as sets of (app, right)."""
    have = set(group.permissions.values_list("content_type__app_label", "codename"))
    expected = wanted()
    return expected - have, have - expected

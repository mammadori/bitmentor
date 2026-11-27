"""The staff's groups, as the community names them: `staff_groups` in community.yml, made and put right at every migrate.

Each group gets exactly the rights it is given, and no other: a right added by hand in /admin/ is taken away at the next
start, a missing one comes back, a group someone deleted is made again (as the moderators' group, shops/moderators.py,
which is the default). A group is given:

- a **bundle**: a set of rights with a name, for one piece of work (BUNDLES, documented in docs/features/staff-groups.md);
- or a **single right** of a bundle, as `app.codename`, for a finer grain: a group that only reads the meetups gets
  `meetups.view_meetup`. Only rights that appear in a bundle: each of them was looked at for what it lets see or do,
  and a right of another model could show the owners' personal data or keys.

What no group can get, whatever community.yml says: the rights to change shops, owners, accounts, groups, keys or
addresses. The destination of a shop's money must not become easier to change for whoever runs the site: those are for
the superusers (docs/features/staff-and-shops.md). A right that is not allowed is skipped and `manage.py check` says so
(shops.W017); the rest of the group is made all the same.

Without `staff_groups` the site has the moderators' group, as before. With it, only the groups it names are kept in
order: a group that is no longer named (Moderators too) stays as it is, for the administrators to empty or delete.
"""

import logging
from functools import reduce
from operator import or_

from django.apps import apps as global_apps
from django.db import DEFAULT_DB_ALIAS, router, transaction
from django.db.models import Q

from config.community import community

from . import moderators

logger = logging.getLogger(__name__)

# (app, right) of each bundle, and what it is for
BUNDLES = {
    "moderation": {
        "what": "The moderators' work: /admin/today/, the requests for help, the register of the owners' accounts",
        # Read from moderators.PERMISSIONS when asked (bundle_rights): one list, the moderators' own
        "rights": None,
    },
    "meetups": {
        "what": "Keep the meetups and the list of the trusted authors; read the readings of Nostr",
        "rights": (
            *(("meetups", f"{action}_meetup") for action in ("add", "change", "delete", "view")),
            *(("meetups", f"{action}_trustedauthor") for action in ("add", "change", "delete", "view")),
            ("meetups", "view_reading"),
        ),
    },
}
DEFAULT = {moderators.GROUP: ["moderation"]}
NAME_LENGTH = 150


def bundle_rights(name):
    if name == "moderation":
        return {(label, codename) for label, codename, _what in moderators.PERMISSIONS}
    return set(BUNDLES[name]["rights"])


def in_bundles():
    return {right for name in BUNDLES for right in bundle_rights(name)}


def allowed(label, codename):
    """Whether a single right may be given to a group: only one of a bundle."""
    return (label, codename) in in_bundles()


def configured():
    """The groups as community.yml asks them, read as far as they can be: ({name: {(app, right)}}, [problems])."""
    raw = community().get("staff_groups")
    if raw is None:
        raw = DEFAULT
    problems = []
    if not isinstance(raw, dict) or not raw:
        # A slip in the block must not stop the cleaning of the rights given by hand: the default group stays kept
        problems.append(
            "`staff_groups` must be a mapping, a group's name to a list of bundles or rights, with at least one group: "
            f"the default applies, the group {moderators.GROUP} with `moderation`"
        )
        raw = DEFAULT
    groups = {}
    for name, items in raw.items():
        if not isinstance(name, str) or not name.strip() or len(name) > NAME_LENGTH or not name.isprintable():
            problems.append(f"`staff_groups`: the name {name!r} is not a name of at most {NAME_LENGTH} characters")
            continue
        if not isinstance(items, list):
            # Kept with no right: whatever was given by hand is taken away, nothing is given by mistake
            problems.append(f"`staff_groups.{name}` must be a list of bundles or rights: the group gets no right")
            items = []
        rights = set()
        for item in items:
            if isinstance(item, str) and item in BUNDLES:
                rights |= bundle_rights(item)
            elif isinstance(item, str) and item.count(".") == 1 and allowed(*item.split(".")):
                rights.add(tuple(item.split(".")))
            else:
                problems.append(
                    f"`staff_groups.{name}`: {item!r} is neither a bundle ({', '.join(BUNDLES)}) nor a right a group "
                    "may have: skipped"
                )
        # "Curators" and " Curators " are one group in /admin/: their rights add up
        groups[name.strip()] = groups.get(name.strip(), set()) | rights
    return groups, problems


def wanted(rights, apps=global_apps):
    """The rights of the list this installation can give: the ones of apps it has."""
    found = set()
    for label, codename in rights:
        try:
            apps.get_app_config(label)
        except LookupError:
            continue
        found.add((label, codename))
    return found


def sync_group(name, expected, apps, using):
    """Makes the group and gives it exactly `expected` (as far as the rights exist). Returns the rights it now has."""
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    from django.contrib.auth.management import create_permissions

    for label in sorted({label for label, _codename in expected}):
        create_permissions(global_apps.get_app_config(label), verbosity=0, using=using, apps=apps)
    rights = Permission.objects.using(using).filter(
        reduce(or_, (Q(content_type__app_label=label, codename=codename) for label, codename in expected), Q(pk__in=[]))
    )
    found = {(right.content_type.app_label, right.codename) for right in rights.select_related("content_type")}
    for label, codename in sorted(expected - found):
        logger.error("The right %s.%s of the staff group %r does not exist", label, codename, name)
    with transaction.atomic(using=using):
        group, _made = Group.objects.using(using).get_or_create(name=name)
        before = set(group.permissions.values_list("content_type__app_label", "codename"))
        group.permissions.set(rights)
    if before - found:
        logger.error(
            "Rights taken away from the staff group %r, which community.yml does not give it: %s",
            name,
            ", ".join(f"{label}.{codename}" for label, codename in sorted(before - found)),
        )
    return found


def sync(apps=global_apps, using=DEFAULT_DB_ALIAS):
    """Every group of community.yml made and put right. Returns {name: rights}, or None without the accounts' tables."""
    try:
        Group = apps.get_model("auth", "Group")
        apps.get_model("auth", "Permission")
    except LookupError:
        return None
    if not router.allow_migrate_model(using, Group):
        return None
    groups, problems = configured()
    for problem in problems:
        logger.error("%s", problem)
    done = {}
    for name, rights in groups.items():
        try:
            done[name] = sync_group(name, wanted(rights, apps), apps, using)
        except Exception:
            # One group that cannot be saved does not leave the others with the rights given by hand
            logger.exception("The staff group %r could not be updated", name)
    return done


def on_post_migrate(sender, apps=global_apps, using=DEFAULT_DB_ALIAS, **kwargs):
    """`migrate` ends: the groups are made or put right. A failure is logged and does not stop the start of the site:
    the groups stay as they were, which gives nobody more than before."""
    try:
        sync(apps=apps, using=using)
    except Exception:
        logger.exception("The staff groups could not be updated")


def problems():
    """What `manage.py check` says about `staff_groups` (shops.W017)."""
    return configured()[1]

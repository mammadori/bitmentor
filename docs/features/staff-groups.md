# The staff groups

The staff of a site (who keeps the shops, the meetups, the help requests) works in `/admin/` through **groups**: a
group has rights, an account in the group has them too. bitmentor makes the groups a community asks for, and keeps
their rights exactly as asked: at every `manage.py migrate`, which the site runs at every start, a right added to a
group by hand is taken away, one that is missing comes back, and a group someone deleted is made again. The log says
what it took away.

Who has every right is a **superuser**: that is not a group, and nothing here changes it.

## Without configuration

A site without `staff_groups` in `community.yml` has one group, **Moderators**, with the `moderation` bundle below, as
before this feature. `manage.py moderators add <name>` puts an account in it ([The
moderators](../operations.md#the-moderators)).

## `staff_groups` in community.yml

A mapping: the group's name (as `/admin/` shows it, at most 150 characters) and the list of what it gets, bundles or
single rights.

```yaml
staff_groups:
  Curators: [moderation, meetups]
  Meetup readers: [meetups.view_meetup]
```

- With the key, **only the groups it names** are kept in order. A group no longer named, Moderators too, stays as it is:
  an administrator moves its members to the new group and then deletes it in `/admin/auth/group/`.
- An account is put in a group in `/admin/` (Users, the account, Groups), and needs staff status to open `/admin/`.
  `manage.py moderators add` works only on the Moderators group.
- A bundle or a right that is not on the lists below is skipped, the rest of the group is made, and `manage.py check`
  says so (`shops.W017`). A group whose entry is not a list gets no right at all. A block that is not a mapping, or an
  empty one, counts as missing: the Moderators group with `moderation`.
- A name already used by a group made by hand takes that group over: it keeps its members and gets exactly the rights
  written here. Names are as `/admin/` shows them, capitals included: `moderators` is not `Moderators`.
- The `moderators` command (`list`, `add`, `sync`) follows `community.yml`: with Moderators redefined it uses those
  rights, and without it `add` refuses and `list` says that nobody keeps the group.
- The team area (`/team/`, [Team access](team-access.md)) is a separate way in, with its own key: what it shows follows
  the `moderation` bundle, whatever the groups are.
- Changing the block takes effect at the next start of the site (or with `manage.py migrate`).

## The bundles

| Bundle | What it lets do | Rights |
| --- | --- | --- |
| `moderation` | The moderators' work: read `/admin/today/` and the shops it points to (never change them), read and mark the requests for help, read the register of the changes to the owners' accounts | `shops.view_shop`, `shops.view_helprequest`, `shops.change_helprequest`, `shops.view_accountchange` |
| `meetups` | Keep the meetups and the list of the authors whose Nostr events are trusted ([Meetups](meetups.md)); read the readings of Nostr | `meetups.add_meetup`, `meetups.change_meetup`, `meetups.delete_meetup`, `meetups.view_meetup`, the same four of `trustedauthor`, `meetups.view_reading` |

A group may also get **single rights of a bundle**, written `app.codename` as in the table, for a finer grain: a group
that only reads the meetups, or that reads the requests for help without marking them.

## What no group can get

Only the rights of the bundles can be given. The others, and above all the rights to change shops, owners, accounts,
groups, keys or Lightning addresses, stay with the superusers whatever `community.yml` says: the destination of a
shop's money must not become easier to change for whoever runs the site ([Staff and shops kept
apart](staff-and-shops.md)). A right to read another model is not offered either: it could show the owners' personal
data. A community that needs one more piece of work asks for a new bundle in bitmentor, where its rights are looked at
one by one.

The code is in `shops/staff_groups.py`; the `moderation` bundle is the list of `shops/moderators.py`.


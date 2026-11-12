# The association's handbook

For the volunteers who run a bitmentor site for their community. It lists the tasks by how often they come: every
day, every week, when a merchant calls, before a release, and when something breaks. Each task says why it matters,
the exact command, what you should see, and what to do if it goes wrong.

The details of each part are in other documents; this page links to them. The personal data the site keeps, for how long and
what to answer to someone who asks about it are in [Privacy and data protection](privacy.md); the software is provided
"as is", without warranty, and the community that runs the site is responsible for its legal compliance.

## Before you start

This page is written for a machine installed from images, in `/opt/<project>` ([installation.md](installation.md)). In this
page `<project>` is the folder's name, for example `shops`, and `$D` is the folder:

```bash
D=/opt/<project>
```

Commands for the site go through `$D/bin/compose.sh`: it is `docker compose` with the files and images of the
installed version. You can run it from any folder. For example:

```bash
$D/bin/compose.sh ps
$D/bin/compose.sh exec web python manage.py dashboard
```

On an installation from the sources ([One machine](single-machine.md)), use `docker compose` in the clone wherever this
page says `$D/bin/compose.sh`; the timers are cron lines there ([Regular jobs](single-machine.md#regular-jobs)).

What you need:

- an account on the site for `/admin/` and `/admin/today/`: staff status and the **Moderators** group, which
  `manage.py moderators add <name>` gives and nothing else is needed ([The moderators](#the-moderators)); whoever runs the
  site has every right already;
- if you also have a shop: a second, ordinary account for it. A staff account is a visitor outside `/admin/`, and the
  pages of a shop do not open for it ([Staff and shops kept apart](features/staff-and-shops.md));
- a place in the association's Telegram chats: the machine's alerts go to the chats of `alerts.env`, the shop alerts
  of the site (Lightning addresses, requests for help) to `TELEGRAM_MODERATION_CHAT`;
- for the commands, a shell on the machine as root (or a user in the `docker` group).

Four rules never change for whoever answers a merchant. They are in [helpdesk.md](helpdesk.md#never):

- never change where a shop's money goes on the merchant's behalf;
- never recommend an exchange or a custodial wallet: only open source, non-custodial, free tools, chosen by the
  community;
- never ask for a seed, a private key or a password;
- never tell a merchant a sale is paid because the register or the customer says so: what counts is what arrived in
  their wallet.

A fifth is for whoever has rights over accounts: never give a merchant's account a password or staff status, and never
replace its sign-in name. The site writes it in a register and tells the merchant and the association
([Staff and shops kept apart](features/staff-and-shops.md)).

**A new staff account.** Create it in `/admin/auth/user/` with staff status (or with `createsuperuser`) and hand the
password over in person; then mark it with `$D/bin/compose.sh exec -T web python manage.py temporary_password <username>`.
From the first sign-in every page of `/admin/` sends that person to the change of password, until they choose a new one
(the mark ends by itself). If you set the password again, mark it again; `--clear` takes the mark away.

## Every day

### Read what to do today

**Why.** A Lightning address that stopped working, a merchant who asked for help: the merchant often does not notice
the first one. The site does, and puts it all on one page.

**How.** Open `/admin/today/` (the link is at the top of `/admin/`). Or, on the machine:

```bash
$D/bin/compose.sh exec -T web python manage.py dashboard
```

`--all` adds the things to know, not only what to do. `--language it` gives the text in another language.

**What you should see.** On top: how many registers take payments, how many shops to look at, how many are closed for a while.
Then one line per thing to do, the most urgent first: the shop, what happened, since when, what to do now, what to
say to the merchant, and often the `diagnose_shop` command for that shop. The command exits with 0 when there is
nothing to do, 2 when there is something to do, 1 when the command itself failed.

**If something is there.** Each code is explained in [helpdesk.md](helpdesk.md#before-the-phone-rings). Call or write
to the merchant with the words on the line. For more about one shop, run `diagnose_shop` (below).

**If the page says the guardian is late** (`guardian_late`): `check_lightning` has not run for a while, so the
Lightning lines may be old. See
[The timers](#check-the-timers) below.

The list comes by itself: every morning `bitmentor-dashboard@<project>.timer` runs `manage.py dashboard --send`,
which writes a short summary to `TELEGRAM_MODERATION_CHAT` (or to the `email` of `community.yml`, without Telegram or
when Telegram refuses it) only when the things to do changed since the last summary, the new ones first, and again a
week later if nothing changed: the site, the shops' names, what happened, and the link to `/admin/today/`. Nothing
else about the shops or their owners. No message means nothing new. To see the whole list, without sending:
`manage.py dashboard`. When neither Telegram nor the email takes the summary, the unit tries again every 10 minutes,
four times, then `check.sh` alerts: "the morning summary … did not arrive".

More: [What to do today](features/dashboard.md).

### Read the alerts

**Why.** The machine checks itself every 10 minutes (`check.sh`): containers, site, certificate, disk, memory,
release and the nightly backup. It writes to Telegram only when the problems change, and again every 6 hours while
they last. The site writes about shops in `TELEGRAM_MODERATION_CHAT`: Lightning addresses that stop or start working,
requests for help.

**What you should see.** Nothing, most days. Each alert of the machine is one line per problem.

**If there is an alert.** Find it in [When something breaks](#when-something-breaks) below.

### Look at `/health/`

`https://<site>/health/` answers 200 when the database answers, 503 otherwise, with the reason `database`. It also
says the versions, the mode and since when. It is open in every mode.

An external check on `/health/` that alerts a person must run from another network: if the machine goes down,
`check.sh` goes down with it ([Going to mainnet](mainnet.md#to-decide-before-opening), item 5).

## Every week

### Check the timers

**Why.** Seven timers keep the site honest. If one stops, only some of them alert you: the table in [The timers of a
production machine](#the-timers-of-a-production-machine) says which.

```bash
systemctl list-timers 'bitmentor-*'
```

**What you should see.** One line for each timer of your project that you turned on, from that table, each with a
recent "last" time and a "next" one. `bitmentor-backup` is there once `backup.env` is ready. `bitmentor-dashboard`,
before the morning summary, also runs the job that forgets [the old requests for help](#forget-old-requests-for-help).

The last runs:

```bash
journalctl -u bitmentor-sentinel@<project> -n 5
journalctl -u bitmentor-dashboard@<project> -n 5
journalctl -u bitmentor-osm@<project> -n 5
journalctl -u bitmentor-meetups@<project> -n 5
```

**If one is missing or failing.** Start it again with `systemctl enable --now bitmentor-<name>@<project>.timer`, and
read its log with `journalctl -u bitmentor-<name>@<project> -n 50`.

### Check the backup

**Why.** A backup that nobody tried is a hope, not a backup.

```bash
cat $D/state/backup.ok
```

**What you should see.** The time of last night's backup, and under it the copy's name, size and sha256. `check.sh`
already alerts when it is more than 26 hours old. If `state/backup.error` exists, the last night failed:
`journalctl -u bitmentor-backup@<project> -n 30` says which check stopped it (a dump that does not open, a missing key,
the destination that does not answer).

**Once a month**, open the latest copy where the key is, and once in a while try the full restore: both in
[Backup and restore](backup.md), which also covers an installation from the sources and a machine that has no backup yet.

### Check the configuration

```bash
$D/bin/compose.sh exec web python manage.py preflight
```

**What you should see.** On mainnet it must end with `All ready` (on signet it always reports that `NETWORK` is not
mainnet: that is expected). Lines starting with `good to know:` are notes, not problems: Lightning addresses that do
not work, a guardian that stopped, features turned off, closures running.

**If it reports problems.** Each line says what is wrong: the mail server, `SECRET_KEY`, `DEBUG`, HSTS, IPv6.

### Look at the things to know

```bash
$D/bin/compose.sh exec -T web python manage.py dashboard --all
```

Shops without a position, moved shops still waiting for their owner or their wallet.
None of them is urgent; a short message to the merchant is often enough.

### Look at the meetups

Only if the community has the `meetups` block. In `/admin/` → Meetups, against the group where the meetings are announced:
a meeting that was cancelled or moved there (the Nostr events do not say so) is marked *Cancelled* or *Postponed*, and one
that is wrong is corrected: a correction is never overwritten by the daily reading. Do it also the day before each usual
date. *Readings of Nostr* shows that the reading ran and how many events it refused; the keys it trusts are in *Trusted
authors*, each with a note on who certified it ([Meetups](features/meetups.md)).

### Forget old requests for help

**Why.** A request for help ([Something wrong?](features/help.md)) keeps, besides the shop, the symptom and the dates, the line the
merchant wrote, a copy of what the site knew about the shop when they asked (their Lightning address, when they last signed
in), the account that asked and the volunteer who solved it. It is personal data, and a year after the merchant
was answered it is of no use to anyone.

**What happens.** `bitmentor-dashboard@<project>.timer` runs `manage.py forget_old_requests` every morning, before the summary.
A request **solved** more than 365 days ago loses all that: the line, the copy of the diagnosis, the
account, how the merchant wanted to be called back and who solved it. The row stays, with the shop, the symptom, when it was asked and
when it was solved, so that the counts do not change. A request still open or waiting, one solved less than a year ago, and one marked
as waiting again are not touched, however old they are. Running it again does nothing.

**The number of days** is the one the community writes in `community.yml`, and the timer uses only that:

```yaml
privacy:
  help_requests_days: 365    # 30 to 3650; 0 or false: the timer never forgets
  email_prints_days: 62      # 32 to 3650: the email budget's prints of the recipients
```

Without the block it is 365. A wrong number counts as 365, and `manage.py check` says so (`shops.W004`). `0` or `false` turns the
forgetting off: the command says so and does nothing. **By hand**, `--days` wins over the file (for that run only, not for the timer):

```bash
$D/bin/compose.sh exec web python manage.py forget_old_requests --dry-run
$D/bin/compose.sh exec web python manage.py forget_old_requests --days 180
```

`--dry-run` says how many rows would be forgotten and changes nothing; `--days` is counted from the day the request was solved, not from
the day it was asked. Under 30 days the command refuses (the panel shows a solved request for three days, and what is forgotten cannot be
given back): `--force` allows it, and `--days 0 --force` forgets every solved request now.

**When it stops by itself.** If the last reading the guardian wrote on a shop's Lightning address is more than two days old, or
later than now, the machine's clock may be wrong, and the command forgets nothing and says so (the unit shows as failed in
`journalctl -u bitmentor-dashboard@<project>`). Look at the date of the machine, and run `check_lightning`: the next run goes on. `--force` does not skip this. A site where nothing is read for two days (every shop closed, or the timers stopped) does not
forget until something is read.

**After that** the request is still in `/admin/shops/helprequest/`, solved, without its line and its diagnosis, and it is no longer in
the history of the merchant's help page. A request that nobody ever solved is not forgotten: it is on top of `/admin/today/` until someone does.

**What the command cannot forget.** The merchant's line also went to the moderators' Telegram chat (`TELEGRAM_MODERATION_CHAT`), in the
message that told you about the request, and nothing on the site can delete a message there: **turn on the automatic deletion of
messages in that chat** (a month is plenty; the request itself stays in `/admin/` until it is forgotten). Without Telegram nothing else
leaves the site. And the nightly copies ([Backup and restore](backup.md)) keep the line as it was: the destination keeps the first copy of each
month for 12 months by default (`MONTHS` in `keep-backups.sh`), so a line can stay in a copy for another year after it is forgotten, up
to about two years after it was written.

### Look at the changes to the shops' accounts

In `/admin/`, **Account changes** (`/admin/shops/accountchange/`), newest first: every time the password, the staff
status, the sign-in name, the groups, the owner of a shop or the chat of its notices changed, from `/admin/` or from a
command, with who did it and who was told. `/admin/today/` shows how many there were in the last two weeks.

**What you should see.** Only what the volunteers did on purpose. A row nobody can explain is a problem: change the
passwords of the staff accounts (`/admin/auth/user/`), and ask the shop's owner by phone whether the Lightning address
is still theirs.
The same message went to the owner and to `TELEGRAM_MODERATION_CHAT`. The register cannot be edited or deleted from the
admin; whoever has the shell or the database could, so these are the people to keep few.

### Look at the moderators

```bash
$D/bin/compose.sh exec -T web python manage.py moderators list
```

**What you should see.** The rights of the group, none of them marked `MISSING` or `NOT ON THE LIST`, and a member for
each volunteer who moderates today: every name is a person you know, and none has a note beside it. A volunteer who
left is taken out with `moderators remove <name>` ([The moderators](#the-moderators)). The register of the changes to
the shops' accounts does not see this list change, since a moderator owns no shop: this look, by two people if you can,
is the check.

### Test the alert bot (once a month)

```bash
$D/bin/check.sh $D --test "Test message"
```

**What you should see.** The message in every chat of `alerts.env`. If it does not arrive, check
`TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_IDS` in `alerts.env`: without them the machine only writes to its log.

## When a merchant calls

Follow [When a merchant calls](helpdesk.md). The short version:

```bash
$D/bin/compose.sh exec web python manage.py diagnose_shop "<shop>"
```

`<shop>` is the shop's id (or the id it had on the site it moved from), an email of the owner or of the shop, the
number in the imported list, or a piece of the name. Each line has a sign (`✗` problem, `!` to know, `✓` fine), the
sentence to read to the merchant, when there is something to do an arrow (`→`) with what you do, and a detail for you. It only reads: it changes nothing. Exit code 0 when there is no problem, 1 when there is at least one, 2 when
no single shop was found.

## The moderators

A moderator is a volunteer who looks after the shops day by day: reads the list in `/admin/today/`, answers the requests
for help. They do all of it without being able to change a shop, its
owner, an account, a key or an address. They sign in at `/admin/` with a staff account in the **Moderators** group.

The group is made by the site: `manage.py migrate`, which runs at every start of `web`, creates it with exactly the
rights below and puts it right again if somebody added a right by hand, took one away or deleted the group. There is
nothing to set up, and it works on a new installation and on one that is updated.

To make someone a moderator, the account must exist: a superuser makes it in `/admin/` under **Users**, with a name and
a password handed over in person, and ticks nothing else (the paragraph **A new staff account**, above, says how to make
that password temporary). Then:

```bash
$D/bin/compose.sh exec -T web python manage.py moderators add <name>
$D/bin/compose.sh exec -T web python manage.py moderators list
$D/bin/compose.sh exec -T web python manage.py moderators remove <name>
$D/bin/compose.sh exec -T web python manage.py moderators sync
```

- **add** first puts the group's rights right, as `migrate` does (so nobody joins a group with more than the work asks),
  then gives the account staff status, if it did not have it, and puts it in the group. It stops, changing nothing,
  when the account does not exist, is switched off, is a superuser (it has every right already) or **owns a shop**: a
  staff account is a visitor on every page of a shop ([Staff and shops kept apart](features/staff-and-shops.md)), so the
  owner would lose the panel and the register of their own shop. A volunteer who also has a shop uses a second account
  for the work. If the account has no password, it says so: `manage.py changepassword <name>` gives one.
- **remove** takes the account out of the group and, if nothing else made it staff (no superuser status, no other
  group, no right of its own), takes staff status away too. It works from the next page the person opens. It also works
  on an account that owns a shop, and then the register of the changes to the owners' accounts has a row and the owner
  is told, as for any change made by hand.
- **list** shows the rights, the members, and what is wrong: a right of the list the group lacks (`MISSING`), a right it
  has beyond the list (`NOT ON THE LIST`: the next `migrate` takes it away), a member who is switched off, is not staff,
  has no password or owns a shop, and what else a member has: other groups (`also in group …`) and rights of their own
  (`N right(s) of its own`). A moderator who has more than the group is more than a moderator: look at why.
- **sync** puts the group's rights right at once and says what it gave back and what it took away. The group is put right
  by itself at every start of `web`; when you cannot wait for the next one (a right added by hand that must go now),
  run `moderators sync` or `$D/bin/compose.sh exec -T web python manage.py migrate`: the web log (at ERROR, the only level
  the site writes) names every right that was taken away.

**What a moderator can do.** These rights and no others:

| Right | What it opens in `/admin/` | Why a moderator needs it |
|---|---|---|
| `shops.view_shop` | `/admin/today/`, the list of shops and each shop's page, to read only | The list of what to do is their day, and each line links to the shop |
| `shops.view_helprequest`, `shops.change_helprequest` | The requests for help, and the actions **Mark as solved** and **Mark as waiting again** | To answer a merchant who asked. Without the first, the line on `/admin/today/` has no link; without the second, a request cannot be closed ([Something wrong?](features/help.md)) |
| `shops.view_accountchange` | The register of the changes to the owners' accounts, to read only | A second pair of eyes on what the staff changes in a merchant's account; `/admin/today/` counts its rows |

**What a moderator cannot do.** Anything that changes a shop or an account. Nobody in the group can suspend a shop from
the map, mark it historic, write a closure under it, revoke or allow its certificate, open or close its private part, hand it to another owner, or edit a
shop's form (they can read it); they cannot see or change users, groups and rights, and they delete nothing. These jobs are for whoever has all the rights, a superuser: when a
moderator needs one done (the calls in [helpdesk.md](helpdesk.md) say which), they ask. It is on purpose: where the money
of a shop goes must not get easier to change for the people who run the site. To give a volunteer more, give them a
group of their own, not this one: the Moderators group is the same on every installation, and `migrate` puts it right.

## Before a release

A release happens when the machine's tag (`active`) moves to a new version: `release.sh` then installs it by itself
within a minute ([releases.md](releases.md#releases-from-images)). Only the maintainer moves the tag. Around it:

1. **The tests** pass in the CI of the tag. A community that extends the image runs its own too.
2. **The configuration**: `preflight`, as in [Every week](#check-the-configuration).
3. **A sale**, on a test site before the release and on the site itself after it: open a shop's register with a
   Lightning address of the association kept for this, never a merchant's, make a sale of a small amount and pay it. The register must say
   it is paid. The site does not handle the payment (the register talks to the shop's Lightning address), so what a
   release can break is the register page itself: [releases.md](releases.md#before-a-release).

After the release:

```bash
cat $D/state/version
curl -s https://<site>/health/
```

The version is the new one, and `/health/` says it too. If the new version did not start, `release.sh` has already
gone back to the previous one and written `state/failed`; `check.sh` alerts about it.

### Who sees the site

The mode is not part of a release. Only the person who runs the machine changes it:

```bash
$D/bin/mode.sh $D maintenance
```

`development` (only the addresses in `DEV_ALLOWED_IPS`), `maintenance` (the under-construction page for everyone),
`production` (open, only with `NETWORK=mainnet`). If the new mode does not start, the script puts the previous one
back. More: [configuration.md](configuration.md#who-sees-the-site).

## A new installation

Most communities start with [One machine, from the sources](single-machine.md): a clone, a `.env`, `docker compose up`.
With several people, a CI and releases from images, follow [Advanced installation](installation.md): `deploy/install.sh`
prepares the folder, the timers start the stack, and the script's last list says what is left. Two installations on one
machine, for example signet and mainnet, go behind a shared entry point
([installation.md](installation.md#two-installations-on-one-machine)).

## The timers of a production machine

A machine installed from images does its regular work with seven systemd timers. Each is a template, `bitmentor-<name>@.timer`
with its `.service`, started for one project as `bitmentor-<name>@<project>.timer` (`<project>` is the folder's name in
`/opt`). The units are shared by every installation on the machine; the scripts they run come with the release, in
`/opt/<project>/current/deploy` (`release.sh`, `check.sh` and `mode.sh` are also copied to `bin/` by each release).

| Timer | When it runs | What it does | What it needs before | If it stops |
|---|---|---|---|---|
| `release` | 2 minutes after boot, then a minute after each run | Installs the version the `active` tag points to, and goes back if it does not start ([releases.md](releases.md#releases-from-images)) | `release.env` and the registry | `check` alerts: "the automatic release stopped with an error" |
| `check` | 5 minutes after boot, then 10 minutes after each run | The machine checks itself and writes to Telegram only when the problems change ([Read the alerts](#read-the-alerts)) | `alerts.env` with `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_IDS`; without it, it only writes its log | Nobody: this is the watcher. Use an external check on `/health/` |
| `sentinel` | 15 minutes after boot, then an hour after each run | `check_lightning`: tries the Lightning addresses of the shops that are due, tells the merchant by email and the moderators on Telegram when one stops or starts working ([guardian](features/lightning-guardian.md)) | The email settings in the `.env` (`EMAIL_HOST`…), and `TELEGRAM_BOT_TOKEN` and `TELEGRAM_MODERATION_CHAT` in the `.env`. Without them the status is written all the same, and nobody is told | `/admin/today/` says `guardian_late`. No alert from `check` |
| `dashboard` | Every day at 06:30 UTC (up to 15 minutes late); at boot if the machine was off | `dashboard --send`: the morning summary of [what to do today](features/dashboard.md), only when it changed, and again a week later | `TELEGRAM_BOT_TOKEN` and `TELEGRAM_MODERATION_CHAT` in the `.env`, or else `email` in `community.yml` with the email settings. With none of them it cannot deliver | It tries again every 10 minutes, four times; then `check` alerts: "the morning summary … did not arrive" |
| `osm` | The first of every month at 03:30 UTC (up to 2 hours late); at boot if missed | `osm_sync --send`: reads OpenStreetMap, proposes links and the tags it should have, fills what the shops have empty, and sends the moderators a summary of what is new; never writes there ([OpenStreetMap](features/openstreetmap.md)). Does nothing, and exits 0, without an `osm` section in `community.yml` | An `osm` section in `community.yml`; `TELEGRAM_BOT_TOKEN` and `TELEGRAM_MODERATION_CHAT` in the `.env`, or else `email` in `community.yml` | It tries again every hour; the fifth start within twelve hours is refused, and `check` alerts: "the monthly reading of OpenStreetMap failed" |
| `meetups` | Every day at 05:40 UTC (up to 20 minutes late); at boot if missed | `read_meetups`: reads from the relays the calendar events (NIP-52) of the trusted authors and updates the board of the meetups; it only reads ([Meetups](features/meetups.md)) | The `meetups` block of `community.yml` with its relays and its trusted authors. Without the block it does nothing | It tries again every 30 minutes, three times, when no relay answered; then `check` alerts: "the reading of the meetups from Nostr failed" |
| `backup` | Every night at 03:30 UTC (up to 30 minutes late); at boot if missed | The encrypted copy of the folder and the database, sent off the machine ([Backup and restore](backup.md)) | `backup.env`, `age`, `rsync` and the keys: see [Turn on the backup](backup.md#turn-on-the-backup) | `check` alerts when the last copy is more than 26 hours old |

Two different Telegram settings: `alerts.env` is for the machine's alerts (`check`), and the site's `.env` is for the
messages of the site itself (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_MODERATION_CHAT`). The moderators' chat is the second one.
The bot must be in that chat.

**To turn them on**, as root, after the release that has them is installed:

```bash
P=<project>
cp -n /opt/$P/current/deploy/systemd/bitmentor-* /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now bitmentor-sentinel@$P.timer bitmentor-dashboard@$P.timer \
  bitmentor-osm@$P.timer bitmentor-meetups@$P.timer
systemctl list-timers 'bitmentor-*'
```

`cp -n` copies only the units that are missing; to bring the ones already there up to date, copy without `-n` (the
units are shared, so do it when every installation on the machine is on the new release), and run `daemon-reload`. Turn
on `bitmentor-backup@$P.timer` the same way once `backup.env` is ready. For a new installation, `deploy/install.sh` prints
these same commands ([installation.md](installation.md)).

**Check each one the first time** instead of waiting for its hour: `systemctl start bitmentor-dashboard@$P.service`, then
`journalctl -u bitmentor-dashboard@$P -n 20`; the same for `sentinel` and `osm` (which says it is off without an `osm`
section). `dashboard` says nothing when there is nothing new, and exits 0. In `/admin/today/`, `guardian_late` goes away
once `sentinel` has run.

## The backup

How to turn on the nightly encrypted copy, open a copy and restore the site: [Backup and restore](backup.md).

## Going to mainnet, and moving the shops

**The rule.** Mainnet is a new installation, with `NETWORK=mainnet` from the start: a site that ran on signet is never
switched over. [Going to mainnet](mainnet.md) has the decisions for the person responsible for the community and the
checks before opening: read it before anything else.

**The exception: moving the shops.** When the signet site and the new mainnet site both run, the merchants who signed
up on signet can move to mainnet without starting over. On the old site:

```bash
$D/bin/compose.sh exec web python manage.py export_shops /tmp/shops.json
```

Copy the file from the old `web` container to the new one (for example with `bin/compose.sh cp` on each machine; not
tried yet). It
holds the owners' emails and the shops' business names and VAT numbers: keep it private, and delete it after the import. On the new site, first a dry run, then the
real import with the list of owners to tell:

```bash
$D/bin/compose.sh exec web python manage.py import_moved_shops /tmp/shops.json
$D/bin/compose.sh exec web python manage.py import_moved_shops /tmp/shops.json --apply --owners /tmp/owners.csv
```

**What you should see.** The dry run reports exactly what `--apply` will do: the shops moved, and each shop left out
with the reason. `owners.csv` has one row per owner with the one-time link and the message to send them. Running the
import again changes nothing.

**What does not move.** Where the money goes (the Lightning address; an on-chain key, where on-chain is turned on, was never on the site) and the
certificate. Sales were never on the site: they stay on the merchants' phones. Each merchant connects the wallet again
on the new site, with the same checks as a new shop.

**If an owner lost the link.** `--only <id> --new-codes` makes a new link for that shop only; the old one stops
working, the other owners keep theirs. The dashboard's line for that shop has the command, with the id. Without
`--only`, `--new-codes` remakes the links of every shop still waiting.
All the rules are in [Moving the shops to another site](features/moving-shops.md).

## When something breaks

First, two commands that say a lot:

```bash
$D/bin/compose.sh ps
curl -s https://<site>/health/
```

### A Lightning provider is down

**What you see.** Many shops of the same provider at once in the guardian's message, or in `/admin/today/` with
`lightning_no_answer`. If more than half of the addresses of a run fail at once, the guardian counts nothing and tells
no merchant: only the association hears it.

**What merchants see.** The register cannot get an invoice, so no Lightning QR. Where the community turned on-chain on
(an experimental feature, off by default), a shop that set it up on its phone still shows the on-chain one.

**What to do.** Nothing on the site: it is not the site's fault, and the address stays the merchant's choice. Tell
the merchants of that provider what is happening, and suggest on-chain where on-chain is turned on and the shop has it. Never change an address
for them. When the provider is back, the guardian sees it within the hour, and sends the merchant a short email.

To try one shop now: the "Try again now" button in the merchant's panel, or `diagnose_shop` for that shop. More:
[The guardian of the Lightning addresses](features/lightning-guardian.md).

### The site is down

**What you see.** The alert says "the site does not answer" or a status code on `/health/`; the external check alerts.

**What merchants see.** On a phone that opened the register before, the register still opens (its saved copy) and
takes payments: it talks to the shop's Lightning address, not to the site. The map and the pages do not answer.

**What to do.**

```bash
$D/bin/compose.sh ps
$D/bin/compose.sh logs --tail 200 web
$D/bin/compose.sh logs --tail 200 caddy
journalctl -u bitmentor-release@<project> -n 50
```

- A release that did not start: `release.sh` already went back to the previous version and wrote `state/failed`.
  Tell the maintainer; to stay on an earlier version, the tag goes back on an earlier image
  ([releases.md](releases.md#releases-from-images)). The database stays migrated:
  what the previous image can and cannot do on it, and the dump that undoes it, are in [Going back](releases.md#going-back).
- A container stopped: `$D/bin/compose.sh up -d --no-build`.
- The disk is full (the alert says "disk …: only …% free"): free space, then start again.
- The certificate expires within 14 days (the alert says "the certificate of … expires within 14 days"): Caddy renews it by itself, so look at its log;
  behind the shared entry point the certificate is the entry point's.
- The machine does not answer: only the external check sees it. Reach the machine through its provider.

To show the under-construction page while you work: `$D/bin/mode.sh $D maintenance`, and back with the previous mode
when it is fixed. `/health/` stays open in every mode.

## Where the rest is

- [Configuration of the site](configuration.md): every setting of the `.env`.
- [Releases, checks and backups](releases.md): how releases from images work, and what to try before a release.
- [Features](features/README.md): what the site does and how to turn each part on.
- [Limitations](limitations.md): what bitmentor does not do.

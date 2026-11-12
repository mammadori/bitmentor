# Backup and restore

What is lost if the machine dies: the shops' settings, the accounts, the training, the requests for help. Not the
money: it is in the merchants' wallets. Not the sales: they are on the merchants' phones and in their copies. So the
copy is small, one database and a few files, and it matters all the same.

In short:

- **One machine, from the sources:** `./backup.sh <folder>` every night from cron, and copy that folder, the `.env` and
  the community folder off the machine ([below](#one-machine-from-the-sources)).
- **A machine installed from images:** `deploy/backup.sh` every night, encrypted with two age keys of two people, sent
  off the machine with rsync ([Turn on the backup](#turn-on-the-backup)).
- **Open a copy once a month**, on the computer of a key holder ([Take a copy off the machine](#take-a-copy-off-the-machine)),
  and try the [full restore](#full-restore) before opening the site and after every Postgres upgrade.

The details for whoever sets it up are further down.

## One machine, from the sources

`backup.sh` in the clone dumps the site database, reads every dump back before it counts, and keeps 14 days:

```bash
./backup.sh /var/backups/bitmentor
```

A line for `/etc/cron.d/bitmentor-backup`, every night at 03:00 (with the clone's real path):

```text
0 3 * * *  root  /path/to/the/clone/backup.sh /var/backups/bitmentor
```

The dumps are not encrypted: copy them off the machine, with the `.env` and your community folder. Without the `.env`
the dump cannot be used by this installation. To restore one:
`docker compose exec -T postgres pg_restore -U postgres -d bitmentor --clean --if-exists < file.dump`.

The encrypted copy below also runs on a clone: see [A machine installed from the sources](#a-machine-installed-from-the-sources).

## Turn on the backup

**Why.** Without it, a disk that fails, a volume deleted by mistake or a bad upgrade loses the shops' settings, the
accounts, the training and the requests for help. The merchants' money is not lost (it is in their wallets), and their
sales are on their phones, never here. The nightly copy is made by `deploy/backup.sh`, which comes with every release;
what it holds and how the destination works are [below](#what-the-copy-holds).

**What to prepare, once.**

1. **Two age keys, of two people.** Each of the two on their own computer (`apt install age`, or the age of their
   system): `age-keygen -o backup-<project>.key`. It prints a line that starts with `age1`: it is public, send it to
   whoever sets the backup up. The file stays with its owner, and on paper in a safe place: it opens every copy, and
   if both are lost, so are the copies. The script refuses to run with fewer than two different keys.
2. **A destination**: a computer or a host that is not this machine (better: not the same provider), with rsync and ssh,
   set up as in [Destination](#destination). In short: a user of its own, a drop box that the machine's key
   writes into, an archive that only root writes into (`archive-copies.sh`, from cron), and `keep-backups.sh` for how
   long to keep the copies.
3. **On the machine**: `apt install age rsync`.

**The commands, in order** (as root on the machine, `D=/opt/<project>`):

```bash
ssh-keygen -t ed25519 -N '' -C backup-<project> -f /root/.ssh/backup-<project>
cat /root/.ssh/backup-<project>.pub        # this line goes to the destination, as in "Destination" below
ssh-keyscan -t ed25519 -p <port> <host> > /root/.ssh/known_hosts-backup   # -p only if it is not 22
ssh-keygen -lf /root/.ssh/known_hosts-backup   # compare the fingerprint with the one the destination's owner reads on the host
cp $D/current/deploy/examples/backup.env $D/backup.env && chmod 600 $D/backup.env
$EDITOR $D/backup.env                     # BACKUP_RECIPIENTS (the two age1… lines), BACKUP_REMOTE, BACKUP_SSH_KEY, BACKUP_SSH_KNOWN_HOSTS
systemctl cat bitmentor-backup@.service >/dev/null || { cp $D/current/deploy/systemd/bitmentor-backup@.* /etc/systemd/system/ && systemctl daemon-reload; }
systemctl start bitmentor-backup@<project>.service     # the first copy, now: a minute or two
journalctl -u bitmentor-backup@<project> -n 20
systemctl enable --now bitmentor-backup@<project>.timer
```

**What you should see.** In the journal, one line for the database (`bitmentor: … bytes, … tables, read in full`), then
the name of the copy with its size and sha256, and `shipped to …`. `cat $D/state/backup.ok` has the time and the same
sha256. On the destination, within a quarter of an hour, `archive-copies.sh` prints `archived …` with the same
sha256. `systemctl list-timers 'bitmentor-backup*'` shows the next night.

**The first copy must be opened**, by one of the two key holders, before calling this done:
[Take a copy off the machine](#take-a-copy-off-the-machine).

**If it fails.** `state/backup.error` appears, and within 10 minutes the alert bot says "backup failed". The
journal says why:

- `BACKUP_RECIPIENTS is missing`, or `at least two different keys are needed`: `backup.env` is not filled in.
- `the … dump is truncated or damaged`, `is empty`, `does not start with`: a copy that would not open was not kept. Look at
  the disk (`df -h /var/lib/docker /opt`) and at the Postgres log; the next night tries again.
- `little space in …/backup`: the disk is nearly full, probably because the copies could not be shipped and piled up. Free
  space (old copies in `backup/daily` that did reach the destination), then fix the destination.
- `BACKUP_REMOTE is missing`, or a message from ssh or rsync: the destination. Try it by hand with the same key:
  `ssh -i /root/.ssh/backup-<project> -o UserKnownHostsFile=/root/.ssh/known_hosts-backup <user>@<host>` must be
  answered by `rrsync` with an error (the key only sends copies; it opens no shell); a "host key" error means `known_hosts-backup`
  is not the one.
- A copy that is on the machine but did not reach the destination is sent again the next night, with the others.
- A failed run is tried again by the unit 10 minutes later, up to three times in an hour, and each try makes a copy of its
  own and, if it fails, sends its own alert (a failed night can leave four copies on the machine and send four alerts; the
  destination takes at most five a day). After that it waits for the next night. A boot is waited for: the script gives the containers five minutes to come up before it dumps.

### A machine installed from the sources

A development or test machine (a clone: no `release.env`, no `current/`), where age is
not installed and there is no destination off the machine. The same script runs, with three more lines in `backup.env`:

```text
BACKUP_RECIPIENTS="age1… age1…"
BACKUP_AGE=docker run --rm -i --pull=never --network none bitmentor/age:latest
BACKUP_TEST_FOLDER=/var/backups/bitmentor
```

`BACKUP_AGE` is the command that runs age: the whole plaintext goes through it, so an image you build yourself, never
pulled, and no network. Build it once:

```bash
printf 'FROM debian:trixie-slim\nRUN apt-get update -qq && apt-get install -y -qq --no-install-recommends age && rm -rf /var/lib/apt/lists/*\nENTRYPOINT ["age"]\n' | docker build -t bitmentor/age:latest -
```

A release machine refuses `BACKUP_AGE`: it has `apt install age`. `BACKUP_TEST_FOLDER` is a folder of the same machine, for a test: it
protects from a mistake, not from a failure, and a release machine (one with `release.env`) refuses it. The project is
the folder's name, as for Docker Compose (`BACKUP_PROJECT` if `COMPOSE_PROJECT_NAME` in the `.env` says another). The timer needs a drop-in, since there is no `current/`:

```text
# /etc/systemd/system/bitmentor-backup@<project>.service.d/sources.conf
[Service]
ExecStart=
ExecStart=/opt/%i/deploy/backup.sh /opt/%i
ExecStartPost=/opt/%i/deploy/examples/keep-backups.sh /var/backups/bitmentor
```

(`ExecStartPost` prunes the folder: 30 days, then the first of each month.) Try it by hand first:
`/opt/<project>/deploy/backup.sh /opt/<project>`.

## Take a copy off the machine

**Why.** A copy on the machine protects from a mistake, not from a failure of the machine. And a copy nobody has opened
may not open on the day it is needed. The machine cannot open it itself (the private key is elsewhere, on purpose): it
checks that it read every dump and that the file looks like an age file, and no more.

**With the destination** (rsync), the copies arrive by themselves. The person who has a key checks them, on their own
computer, with age installed. Fetch the latest copy from the destination's `archive` (a read-only account of the keeper,
not the machine's key, which cannot read), then:

```bash
scp <keeper>@<host>:/srv/backup/<project>/archive/<latest copy>.tar.age .
sha256sum <latest copy>.tar.age              # the same as in the journal of the machine and in the destination's line
verify-copy.sh --key backup-<project>.key <latest copy>.tar.age
```

(`verify-copy.sh` is in `deploy/examples/` of the software, and in `current/deploy/examples/` on the machine: copy it to your computer.) It ends with `All good.`
or says what is wrong, one line per problem: a file missing, a hash that does not match, a dump that stops halfway, a
copy older than 30 hours (the copies stopped coming). Once a month at least, each of the two key holders in turn, so that
both keys are tried. An old copy, opened on purpose, needs `--max-age 0`. On the destination itself, from cron,
`verify-copy.sh --quiet /srv/backup/<project>/archive` without a key checks only that the copies keep arriving
([Destination](#destination)).

**By hand, for who has no destination yet.** The machine makes the copy and checks it, but does not ship it, and does not
write `backup.ok`:

```bash
# on the machine:
$D/current/deploy/backup.sh $D --here-only    # it prints the file's name; a minute or two
# on your computer (/opt/<project> is written out, since $D is not set there):
scp root@<machine>:/opt/<project>/backup/daily/<copy>.tar.age .   # the file is encrypted, it can travel
ssh root@<machine> sha256sum /opt/<project>/backup/daily/<copy>.tar.age   # compare with sha256sum of the file you have
verify-copy.sh --key backup-<project>.key <copy>.tar.age
ssh root@<machine> rm /opt/<project>/backup/daily/<copy>.tar.age   # the machine does not delete it by itself
```

The file then lives on your computer: put it where a disk failure of that computer would not take it too (a second disk,
a USB key in a drawer). Do not turn the nightly timer on for a machine without a destination: it would fail every night,
and the alert would repeat. This hand copy is a stopgap for the first weeks; the destination comes first.

To open it by hand instead of `verify-copy.sh`: `mkdir copy && age -d -i backup-<project>.key <copy>.tar.age | tar -x -C copy`
([Opening a copy](#opening-a-copy)). The folder holds the `.env` with all the passwords: delete it
when done.

## For developers

### What the copy holds

`deploy/backup.sh`, every night with `bitmentor-backup@<project>.timer`, puts into an encrypted file:

- the site's database, `bitmentor` (`pg_dump -Fc`), and the Postgres users;
- `.env`, `release.env`, `alerts.env`, `backup.env`, `compose.locale.yml` and `state/`;
- `SHA256SUMS`, the hash of each of those files.

It does not contain the community folder, which has its own repository, or the certificates. Neither this script nor
`backup.sh` copies the onion service's key, in the `tor_data` volume: if that volume is lost, Tor makes a new key and
the site gets a new onion address, to write again in `ONION_LOCATION` and wherever the old one was given out. The file is named `<project>-YYYYMMDDTHHMMSSZ.tar.age`. It stays 14 days in
`backup/daily` (`BACKUP_LOCAL_DAYS`) and is shipped with rsync. It waits for a release in progress, and holds
releases back while it copies. If it fails, it alerts through `check.sh` and writes `state/backup.error`; if it
succeeds, it writes `state/backup.ok` (the time, then the copy's name, size and sha256). If `state/backup.ok` is more
than 26 hours old, that is if a night was skipped, `check.sh` reports it; as long as the backup has never succeeded,
it counts from when `backup.env` appeared.

**A copy that does not open is an error.** Before encrypting, the script reads back every dump with `pg_restore --list`
and then to the last block (`pg_restore -f -`: a dump cut near its end passes `--list`, as tried), lists the archive,
and needs the `.env` in. After encrypting it checks the age header and that the file is not smaller than what it holds.
It cannot decrypt: the private key is not on the machine, and must not be. Opening the copy is done where the key is
([Opening a copy](#opening-a-copy)): do it, at least once a month.
It also refuses to start when the disk of `backup/` has less than three times the largest copy kept there plus 512 MiB
free: when the shipments fail the copies pile up on the machine, and this stops them before the disk is full (and alerts).

**Why two keys.** Each key opens all the copies, so one person losing the only key loses every copy: the script
refuses to run with fewer than two different keys.

### Keys

On a computer of the person who will keep them, not on the machine: `age-keygen -o backup-<project>.key`. The `age1…`
line goes into `BACKUP_RECIPIENTS`; the private key stays there and on paper. Two people, two keys: each one opens all
the copies.

### Destination

A host elsewhere, with rsync and ssh, and a user of its own for the copies. **Read this twice:** the key the site's
machine uses is restricted with `rrsync -wo`, and that key cannot read, but it **can delete and overwrite** everything
in the folder it writes to (tried: `rsync -r --delete` from an empty folder empties it, and a file of the same name is
replaced). `-no-del` closes the first (tried: `--delete` is refused), not the second. Whoever takes over the site's
machine has that key. So that folder is only a drop box, and the copies that count are moved by root, out of its reach,
with `archive-copies.sh`:

```text
/srv/backup/<project>/inbox    the ssh user writes here (rrsync -wo -no-del); owned by that user
/srv/backup/<project>/archive    owned by root, not writable by the ssh user: the copies that count
/srv/backup/<project>/refused   owned by root: what archive-copies.sh refused, for 30 days and up to 1 GiB
```

`inbox` must be a filesystem of its own, or have a quota for the user `copies`: that key can write as much as it likes
there, and a full disk would stop everything else on the destination. The archive's disk is the one to watch.

On the destination, as root: the user, the folders, the key, and the cron lines.

```bash
useradd -m -s /bin/bash copies && passwd -l copies
install -d -o copies -g copies -m 755 /srv/backup/<project>/inbox
install -d -o root -g root -m 755 /srv/backup/<project>/archive
install -m 755 deploy/examples/archive-copies.sh deploy/examples/keep-backups.sh deploy/examples/verify-copy.sh /usr/local/bin/
# in ~copies/.ssh/authorized_keys (owned by root, not by copies), the machine's public key, restricted to the drop box:
#   command="rrsync -wo -no-del /srv/backup/<project>/inbox",restrict ssh-ed25519 AAAA… backup-<project>
# in /etc/cron.d/bitmentor-backup (cron mails whatever these print to MAILTO: the destination needs a mail program, or
# the output is lost; a refusal, or copies that stopped coming, would go unseen):
#   MAILTO=association@example.org
#   */15 * * * *  root  /usr/local/bin/archive-copies.sh /srv/backup/<project>/inbox /srv/backup/<project>/archive
#   30 5 * * *    root  /usr/local/bin/keep-backups.sh /srv/backup/<project>/archive
#   45 5 * * *    root  /usr/local/bin/verify-copy.sh --quiet /srv/backup/<project>/archive
```

The last line is `verify-copy.sh` without a key: with `--quiet` it prints nothing while the latest copy of each
project has the age header and is under 30 hours old, and says what is wrong (cron mails it) when copies stop arriving. That is the alarm that
still works when the site's machine is dead, which `check.sh` on it cannot be. `archive/.last-run` says when
`archive-copies.sh` last ran and how it ended.

`BACKUP_REMOTE` is then `copies@host:`, with the path relative to the drop box. The people who keep a key read `archive` with
accounts of their own, read-only, never with the machine's key: give them a group, and `GROUP=<group> MODE=640` in the
cron line of `archive-copies.sh` gives the copies to it (the default is root only, mode 600).

`archive-copies.sh` trusts nothing in the drop box, since everything in it was written by the machine's key. It copies
each finished copy into `archive` and prints its sha256, which must be the one in the machine's journal. It never
overwrites an archived copy. It refuses a name that is not `<project>-YYYYMMDDTHHMMSSZ.tar.age` with digits in the date, a
date in the future or older than 18 days, a file that is not an age file or is under 1000 bytes, a file with an archived
name and another content, more than 5 copies of a project for one day, and a disk with less than twice the file's size plus
1 GiB free (this one stays in the drop box, for later). Those limits are there because `keep-backups.sh` prunes by the
names and the counts: files invented by the key must not be able to push the real copies out of the archive.

A refusal is reported once, in the run's output (the first 20 of a run). What was refused and has the shape of a real
copy (a valid name, the age header, 1000 bytes or more) is **never deleted by the script**, whatever its size: it moves to
`refused`, under its name followed by `.<epoch>.<n>`, for 30 days; over 1 GiB the oldest of those go, except the newest
three, and a refused copy that would not fit stays in the drop box and is reported again. What has not that shape was
invented, and is deleted at once. A copy that is good (its sha256 is in the machine's journal) and was refused, for
example for its date after an interruption of the destination, goes into the archive by hand:
`f=<name in refused>; install -m 600 "refused/$f" "archive/${f%.*.*}"`.

An archived copy is replaced in the drop box by an empty file with the same name and date (so the script does not read the
same gigabytes again every quarter of an hour, and `rsync --ignore-existing` on the machine still finds the name), which
goes after 17 days: more than the 14 days the machine keeps, because the machine sends every copy it still has that is
not in the drop box (with `BACKUP_LOCAL_DAYS` above 17 it sends the older ones again every night: wasteful, and the
script says nothing about them). The 14 days are also what lets the machine survive an interruption of the destination:
it prunes its copies only after a shipment that worked, so after two weeks without a destination it still has them all,
and the destination accepts copies up to 18 days old (`INBOX_DAYS` plus one).

`keep-backups.sh` keeps in `archive` the copies of the last 30 days, the first of each month for 12 months, and in
any case the latest three. It leaves a project alone, and exits 1, when its latest copy is in the future or 3 days old
(`STALE_DAYS`: copies stopped, or this machine's clock is ahead), and it deletes at most half of a project's copies in one
run. A project that is retired on purpose is declared with an empty file `archive/.retired-<project>` (made by root): its
old copies then age out as usual, the latest three staying.
**What none of this can know is what is inside a file.** Whoever has the key can still send, up to 5 a day, files with a
valid age header and nothing inside, under plausible names. That can take the place of the first-of-the-month copy of
the current month, and can fill the day's five places so that the real copy of that day is refused (it stays on the
machine for 14 days, and the run reports it). Only opening the copies ([below](#opening-a-copy)) notices: the person who
keeps a key opens the latest one every month, and an old one now and then (`--max-age 0`, for a copy that is not the
latest).

On the machine:

```bash
ssh-keygen -t ed25519 -N '' -C backup-<project> -f /root/.ssh/backup-<project>
ssh-keyscan -t ed25519 -p <port> <host> > /root/.ssh/known_hosts-backup   # and compare its fingerprint with the host's real one
cp current/deploy/examples/backup.env backup.env && chmod 600 backup.env   # and fill it in
```

`BACKUP_SSH_KNOWN_HOSTS=/root/.ssh/known_hosts-backup` in `backup.env` makes ssh trust this host key for the backup
only (without it, `/root/.ssh/known_hosts`). The guide to turn it all on, step by step, is [above](#turn-on-the-backup).

### Opening a copy

Where the key is (a computer of a person who keeps one, never the machine), with age installed:

```bash
current/deploy/examples/verify-copy.sh --key backup-<project>.key <project>-<date>.tar.age
```

It decrypts into a private temporary folder, checks every file against `SHA256SUMS`, reads each database dump to the
last block with `pg_restore` (the same major version as the database, 18: without it on the computer, it runs the
official `postgres` image with docker), lists the archives, and tells whether the copy is more than 30 hours old
(`--max-age`). It prints no secret and removes the folder at the end. By hand:

```bash
mkdir copy && age -d -i backup-<project>.key <project>-<date>.tar.age | tar -x -C copy
```

Inside: `database/bitmentor.dump`, `database/roles.sql`, `folder/` with the files,
`folder/state.tar` and `SHA256SUMS`. It contains the `.env` with all the passwords: open it on a trusted computer,
and delete it afterwards. Without the key, `verify-copy.sh <folder>` checks only the age header, the size and the age
of the latest copy of each project: it can run on the destination from cron, to know that copies keep arriving.

### Commands on a machine installed from images

The restore below uses Compose the way `release.sh` and `mode.sh` use it, with this function:

```bash
cd /opt/<project> && . ./release.env && v=$(cat state/version)
dc() {
  local file=(-f current/docker-compose.yml -f current/compose.release.yml)
  [ -f current/compose.community.yml ] && file+=(-f current/compose.community.yml)
  [ -f compose.locale.yml ] && file+=(-f compose.locale.yml)
  WEB_IMAGE="$WEB_IMAGE_REPO:$v" CADDY_IMAGE="$CADDY_IMAGE_REPO:$v" \
    docker compose -p "$PROJECT" --project-directory "$PWD/current" --env-file "$PWD/.env" "${file[@]}" "$@"
}
```

### Full restore

On a new machine, or on the same one after a failure, from a copy opened in `copy/`.

**Where the copy is opened.** On the computer of a key holder, never on the machine being restored: check it first with
`verify-copy.sh` ([Opening a copy](#opening-a-copy)), open it into `copy/` there, and take to the machine only what
the steps below copy (`copy/folder/`, `copy/database/*.dump` and `roles.sql`), for example with `scp -r` into
a folder of `root` on the machine. The private key never goes there. At the end delete that `copy/` from the machine:
it holds the `.env` with every password. (The steps say `copy/` as if the machine had it; that is where the files land.)

**If the machine was taken over or compromised**, restore from a copy older than the incident, and treat every secret in
it as exposed: new Postgres passwords (`ALTER ROLE`, and the `.env`), new `SECRET_KEY`, new Telegram token, new Nostr
key, new ssh keys; and the destination of the backup stops trusting the old machine's key (step 6). Check the shops'
Lightning addresses against what their owners say: they decide where the money goes.

1. **Machine:** Docker, `age`, `rsync`, `/opt/<project>` with `bin/` and the units as in [releases.md](releases.md#releases-from-images), timers off. If the
   machine changes, DNS and the entry point must be moved to the new one.
2. **Folder files:** from `copy/folder/` into `/opt/<project>`. One by one: `copy/folder/*` does not match the
   `.env`, a hidden file, and it would bring `state.tar` along. Then `state/`, where `state/version` says which version
   to install:

   ```bash
   cd /opt/<project>
   for file in .env release.env alerts.env backup.env compose.locale.yml; do
     [ -f "copy/folder/$file" ] && cp -p "copy/folder/$file" "$file"
   done
   chmod 600 .env release.env alerts.env backup.env compose.locale.yml   # alerts.env holds the Telegram token
   mkdir -p state bin && tar -xf copy/folder/state.tar
   ```
3. **Release files**, as `release.sh` does:

   ```bash
   cd /opt/<project> && . ./release.env && v=$(cat state/version)
   docker pull "$WEB_IMAGE_REPO:$v" && docker pull "$CADDY_IMAGE_REPO:$v"
   mkdir -p "releases/$v" && id=$(docker create "$WEB_IMAGE_REPO:$v")
   for file in docker-compose.yml compose.release.yml postgres-init.sh tor.sh deploy; do
     docker cp "$id:/app/$file" "releases/$v/$file"
   done
   docker cp "$id:/community/deploy/compose.yml" "releases/$v/compose.community.yml" || true
   docker cp "$id:/app/compose.ipv6.yml" "releases/$v/compose.ipv6.yml" || true
   docker rm "$id" && cp -a "releases/$v" current
   for script in release.sh check.sh mode.sh; do install -m 755 "current/deploy/$script" "bin/$script"; done
   ```

   Then the `dc` function of [Commands on a machine installed from images](#commands-on-a-machine-installed-from-images) (if the `.env` has `IPV6_SUBNET`, with `-f current/compose.ipv6.yml` too, as
   `bin/compose.sh` does).
4. **Postgres and the database:** on a new volume `postgres-init.sh` creates the site's user and database with the
   passwords of the restored `.env`.

   ```bash
   dc up -d postgres
   # -h 127.0.0.1: on a new volume the image starts a first server that answers on its socket only, while it runs
   # postgres-init.sh; the real one answers on the network once the site's database exists
   for i in $(seq 90); do docker exec "$PROJECT-postgres-1" pg_isready -h 127.0.0.1 -U postgres -q && break; sleep 2; done
   docker exec -i "$PROJECT-postgres-1" pg_restore -U postgres -d bitmentor --clean --if-exists < copy/database/bitmentor.dump
   ```

   `roles.sql` is needed only if other users had been created by hand. The site's user and its password are the ones of
   the restored `.env`: `postgres-init.sh` made them.
5. **Start and checks:** `dc up -d`, then `dc exec web python manage.py preflight`, the number of shops
   (`dc exec web python manage.py shell -c "from shops.models import Shop; print(Shop.objects.count())"`), a shop read
   by `dc exec web python manage.py diagnose_shop "<a shop>"`, and a sign-in.
6. **The backup's ssh key, before its timer.** The restored `backup.env` names `/root/.ssh/backup-<project>` and
   `known_hosts-backup`, which a new machine does not have, and on the destination the old machine's key is still
   authorized: if the old machine was lost or taken over, that is a key that can write there. Make a new key on this
   machine, put its public key in the destination's `authorized_keys` in place of the old one (the line of the old
   machine goes), scan the host key again (all as in [Destination](#destination)), try
   `current/deploy/backup.sh /opt/<project> --here-only` and then `systemctl start bitmentor-backup@<project>.service`,
   and only then enable its timer.
7. **Timers** on as in [The timers of a production machine](operations.md#the-timers-of-a-production-machine), and the mode that was in use: it is in the restored `.env`. The backup's
   timer comes after step 6.

**What stays on the machine in clear.** `release.sh` keeps a dump of the site database taken before each release in
`backup/before-*.dump`, not encrypted, as long as `BACKUP_KEEP` says: a machine that is taken over gives them away.
Delete the old ones you do not need.

**What is lost:** whatever changed on the site after the copy: new accounts, shops, edits. No sale is lost: sales are on
the registers' phones and in the merchants' wallets ([What the site keeps](../README.md#what-the-site-keeps)).

### Testing the restore

At least once before opening, and after every Postgres upgrade: on another machine, or on the same one with another
project name and a different folder. On the same machine, after step 2 and before the rest:

- `PROJECT=<test-name>`, the test folder's name, in its `release.env`: containers, networks and volumes take their names from it, and
  the test must not touch the real ones;
- in `compose.locale.yml`, no ports for `caddy` (`ports: !reset []`), which the real installation holds.

Then `dc down -v` on the test, and not on production.

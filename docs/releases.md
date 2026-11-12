# Releases, checks and backups

How a machine installed from images gets new versions, how it checks itself, and what to run before opening the site
to the merchants and before a release. The tasks, by frequency, are in the [handbook](operations.md).

## Releases from images

Nothing is built on a production machine: it installs images that have already been tested.

- A tag on this repository builds, tests and publishes the site image and the Caddy image with its rules
  (an example CI in [examples/ci/gitlab-ci.yml](../examples/ci/gitlab-ci.yml), `Dockerfile.caddy`). A community extends them with its own pages and rules, and puts on its image
  the tag `active`, which the machine follows. The tag says which version to install, not who sees the site: the mode
  stays in the machine's `.env`, and only whoever runs the machine changes it, with `deploy/mode.sh`.
- `compose.release.yml`, on top of `docker-compose.yml`, uses `WEB_IMAGE` and `CADDY_IMAGE` instead of the sources.
- `deploy/release.sh`, every minute, looks at the tag: when it points to a new version, it copies the compose files
  out of the image, saves the site database, updates and checks that the site responds; if it does not respond, it goes
  back to the previous version. `deploy/check.sh`, every 10 minutes, checks containers, site, certificate, disk,
  memory, release and, if there is a `backup.env`, the nightly backup (alarm if `state/backup.ok` is more than 26 hours
  old), and writes to Telegram only when the problems change, with a reminder every 6 hours while they last.
  `/health/` also reports the versions, the mode and since when: the date is written by `deploy/mode.sh` at every
  change.
- The feature switches (`features` in `community.yml`: [Turning features off](features/README.md#turning-features-off))
  change nothing for a community that writes no such block.

On the machine, in `/opt/<project>`: the stack's `.env`, `release.env` (`PROJECT`, `SITE`, `WEB_IMAGE_REPO`,
`CADDY_IMAGE_REPO`, `TAG`, usually `active`, and on production `WEB_DIGEST` and `CADDY_DIGEST`: [Pinning the images by
digest](#pinning-the-images-by-digest)), `alerts.env` (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_IDS`, and
`TELEGRAM_CHAT_IDS_BEFORE` for the chats that get the alerts alone until the site has been in `production` for a week),
an optional `compose.locale.yml`; then the scripts in `bin/` and the units of `deploy/systemd` in `/etc/systemd/system`:

```bash
systemctl enable --now bitmentor-release@<project>.timer bitmentor-check@<project>.timer \
  bitmentor-sentinel@<project>.timer bitmentor-dashboard@<project>.timer \
  bitmentor-osm@<project>.timer bitmentor-meetups@<project>.timer
```

The seven timers (these six, and `bitmentor-backup@<project>.timer` once `backup.env` is ready), what each runs, when
and what it needs, are in [The timers of a production machine](operations.md#the-timers-of-a-production-machine).

A group called **Moderators** that already exists on a site is taken over at the first `migrate` of the release that has
the moderators' group: its rights are put back to the list ([The moderators](operations.md#the-moderators)), and a right
added by hand is taken away. Before such a release, look at **Groups** in `/admin/`.

```bash
/opt/<project>/bin/check.sh /opt/<project> --test "Test message"
```

```bash
/opt/<project>/bin/mode.sh /opt/<project> maintenance
```

The state is in `state/`: `version`, `previous`, `history`, and `failed` if the last one did not start. To roll
back by hand, put the tag back on an earlier image (with digests: write back the earlier `TAG` and its two digests).
The database copies taken before each release are in `backup/`.

### Pinning the images by digest

A tag in a registry can be moved by anyone who can push to it, and `release.sh` runs as root the scripts and compose
files it copies out of the image. So `release.env` can pin the exact images, next to the tag:

```bash
TAG=v2026.11.10
WEB_DIGEST=sha256:<64 lowercase hex>
CADDY_DIGEST=sha256:<64 lowercase hex>
```

- `release.sh` then pulls `WEB_IMAGE_REPO@WEB_DIGEST` and `CADDY_IMAGE_REPO@CADDY_DIGEST`, checks that the local image
  carries that digest, and that the version label of the web image is `TAG` (unless `TAG` is `active`). Any mismatch, a
  malformed digest or only one of the two: it refuses (`refused: ...`, exit 1) and changes nothing.
- The containers run the digests (`releases/<version>/images`, copied into `current/`), also for a rollback and for
  `mode.sh`: the rollback never pulls a tag again. The tag stays for people: logs, `state/`, `/health/`, and a local
  `repo:<version>` tag on the images.
- The version label of the image must look like a CI version, `vYYYY.MM.DD` or `vYYYY.MM.DD-N`, with or without digests;
  otherwise `release.sh` refuses it.
- **Without digests**, `release.sh` refuses when the `.env` says `SITE_MODE=production` or `NETWORK=mainnet`: there a
  moved tag reaches real money, and the release is done by hand anyway, so writing two more lines costs nothing. On
  test and development machines it pulls by tag and writes a warning, so that a machine that follows `active` keeps
  updating by itself. An existing production `release.env` without digests stops releasing (nothing running changes)
  until the digests are added. The `.env` is read as written by hand too (`export`, spaces around `=`, quotes).
- On production or mainnet, a version installed by tag (no `releases/<version>/images`) is never run again: the
  rollback to it is refused (`refused: no rollback to ...`, the failed release stays down) and `mode.sh` refuses to
  recreate it. Install a release by digest first.

The digests come from the registry, of the tag the CI built (not of `active`, which moves):

```bash
docker buildx imagetools inspect --format '{{json .Manifest.Digest}}' <web repo>:v2026.11.10
docker buildx imagetools inspect --format '{{json .Manifest.Digest}}' <caddy repo>:v2026.11.10
# or: crane digest <web repo>:v2026.11.10
```

Read them from a machine you trust, right after the CI pipeline of the tag, and write them with the tag in
`release.env`. Signing the images (cosign) is a later step, not done yet.

### Going back

When a release does not answer, `release.sh` puts the previous image back, **on the database the release has already
migrated**: migrations are not undone. So the previous image has to keep writing to a database it does not fully know,
and it can.

- **New columns.** Every NOT NULL column that a release adds to a table that was there before has a `db_default`, the
  same value as its Python `default`. The previous image leaves those columns out of its INSERTs and the database fills
  them: it creates and edits shops, and a shop it creates starts with no Lightning status, which is what a new row
  means.
- **New tables.** On PostgreSQL the foreign keys from the tables a release adds to the tables that were there before
  cascade, or set NULL, in the database too, as the models say. The previous image deletes a shop or an account the way it
  knows, and these go with a shop: closures, help requests, staff registers, training declarations.
  Kept, with the link set to NULL: the help requests and account changes of an account that is deleted.
- **What the previous image does not do.** Whatever it does not know it does not show: the new pages and features are
  gone while it runs. The data they wrote stays in the database, and is there again when the release is installed again.
- **Limits.**
  - SQLite (development and tests) cannot change a constraint in place, so there the foreign keys stay plain:
    production runs PostgreSQL.
  - The unique constraints of the release stay: the previous image gets an IntegrityError where it used to let a
    duplicate through (a Nostr name, for one).
  - A table or a foreign key added after the previous release gets this care only if its migration does: a new NOT NULL
    column with a `db_default` (and the same `default`). Foreign keys need nothing: after every `migrate` a handler
    (`shops/going_back.py`) gives each foreign key of the site's apps the rule of its model in the database, changing only
    those that differ. `shops/test_going_back.py` checks all of them; run it on PostgreSQL with `POSTGRES_HOST`,
    `POSTGRES_USER` and `POSTGRES_PASSWORD` pointing at one.
- **The whole way back is the dump** that `release.sh` takes before it updates, `backup/before-<version>.dump`. It
  does not restore over the migrated database: `pg_restore --clean` leaves the shops and the accounts, which the new tables
  point to, and `django_migrations` goes back to what the dump says while the new tables stay. With `web` stopped
  (`dc` is the function of [Backup and restore](backup.md#commands-on-a-machine-installed-from-images)), drop the database and make it again, then restore:

  ```bash
  dc stop web
  docker exec "$PROJECT-postgres-1" psql -U postgres -c 'DROP DATABASE bitmentor' -c 'CREATE DATABASE bitmentor OWNER bitmentor'
  docker exec -i "$PROJECT-postgres-1" pg_restore -U postgres -d bitmentor --no-owner --role=bitmentor \
    < backup/before-<version>.dump
  ```

  Then start the previous image (`release.sh` already put it back, or `dc up -d web`). What was written after the dump is
  lost.

## Before opening the site

`manage.py preflight` checks the configuration, the mail server and IPv6, and exits with an error if anything is
missing; it also notes the Lightning addresses that do not work and a guardian that has stopped.

`/health/` answers 200 when the database answers, 503 otherwise, with the reason (`database`), the versions and the
mode. Connect it to an external check that alerts a person.

A backup every night, and a restore tried once: [Backup and restore](backup.md).

Docker images are pinned by digest in `docker-compose.yml` and `Dockerfile`, Python dependencies by hash in
`requirements.lock`, generated from `requirements.txt` with `pip-compile --generate-hashes` (the checks image adds
`requirements-test.lock`, from `requirements-test.txt`, never in production). They are updated by hand,
reading the release notes and testing on a test site.

### coincurve, until version 22 is on PyPI

coincurve 22.0.0 is the first version with wheels for Python 3.14, and it is not on PyPI yet. Until it is,
`requirements.txt` and `requirements.lock` take upstream commit `2d11b11` in one of two ways:

- **on x86_64 Linux, a wheel** attached to this repository's GitHub release `deps/coincurve-2d11b11`, pinned by its
  hash. Its version is `22.0.0.dev0`, so it cannot be taken for the official one. `bash deploy/coincurve-wheel.sh`
  builds it again in a pinned image, with the tests of coincurve, and prints its sha256: the same bytes every time, to
  compare with the hash in `requirements.lock`;
- **anywhere else, from source**: pip builds that commit (it needs `git`, a C compiler, `cmake`, `make`, `pkg-config`
  and Python's headers). The Docker image does it in a stage of its own, which stays out of the final image, where
  there is no wheel, and on x86_64 too with `docker build --build-arg COINCURVE_FROM_SOURCE=1 .`.

**When coincurve 22 is on PyPI**, one change puts things back to normal: in `requirements.txt` the two `coincurve @ …`
lines become `coincurve==22.*`; `requirements.lock` is generated again (`pip-compile --generate-hashes`); the first
stage of the `Dockerfile` and the `awk` line that skips coincurve go, and so do `deploy/coincurve-wheel.sh`, the
paragraph in the README and this section. The release `deps/coincurve-2d11b11` can then be deleted: versions already
installed keep working, and only a new build of an old version would miss it.

## Before a release

The site handles no payment: the register on the phone talks to the shop's Lightning address. What a release
can break is the register page itself, and the guardian. Before every release, on a test site:

- open a shop's register with a test Lightning address kept for this (never a merchant's), make a sale of a small
  amount and pay it, and check that the register sees it paid (LUD-21 `verify`);
- run `manage.py check_lightning` and look at its report ([The guardian of the Lightning addresses](features/lightning-guardian.md));
- run `manage.py preflight`.

## Mainnet

Mainnet is a new installation: the decisions and the checks before opening are in [Going to mainnet](mainnet.md), the
backup and the full restore in [Backup and restore](backup.md).

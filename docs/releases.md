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
`CADDY_IMAGE_REPO`, `TAG`, usually `active`, on production `WEB_DIGEST` and `CADDY_DIGEST`, and `VERSION_RE` for
whoever names the versions of their own images: [Pinning the images by digest](#pinning-the-images-by-digest)), `alerts.env` (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_IDS`, and
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
- The version label of the image (the `VERSION` build argument) must be a version, with or without digests: a release
  of the source, `0.1.4` or `v0.1.4`, or a build named after its day, `v2026.11.10` or `v2026.11.10-2`. Otherwise
  `release.sh` refuses it: the label becomes the name of a folder on the machine.
- **Your own names.** Whoever builds images of their own can name the versions otherwise: `VERSION_RE` in
  `release.env` is the bash regular expression a version must match, in place of the two shapes above. For example
  `VERSION_RE='^[0-9]+\.[0-9]+\.[0-9]+-mine\.[0-9]+$'` takes `0.1.5-mine.2`, the second build of yours on bitmentor
  0.1.5. Whatever it says, a version is made of letters, digits, dots, dashes and underscores, starts with a letter or
  a digit, has no two dots in a row and is 64 characters at most.
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

### From 0.1.8 to 0.1.9

- The migrations `shops` 0007 to 0009 run by themselves: a staff register's place, up to three Telegram chats for a
  shop, the help text of `closed_on`.
- A shop with a closing day still to come is again a place to pay (map, counts, discount) until that day: it used to
  be historic at once.
- Shops closed more than a year ago get their register back, with the staff registers, the wallet connection and the
  change of the Lightning address: the register of a closed shop has no end. From the day after its closing day a
  shop is no longer watched by the Lightning guardian nor listed in `/admin/today/`, also when it closed less than a
  year ago.
- For a community's own code: `approval.approve()` skips the shops with a signal that asks for a check unless it is
  called with `confirmed=True`, and logs how many it held. `register_kept()`, `keeps_register()` and
  `REGISTER_AFTER_CLOSING` are kept for imports and no longer restrict anything. Copies of the software's templates:
  see [The community's own code](architecture.md#the-communitys-own-code).
- Existing installations keep Docker's own log files: moving to the machine's journal is by hand
  ([Logs of the containers](configuration.md#logs-of-the-containers)).

### From 0.1.9 to 0.2.0

- The migrations run by themselves: `shops` 0010 to 0014, `team` 0002 to 0004, `website` 0003. `shops` 0011 adds who
  set a new shop aside and when; the `team` ones add the record of what is done in the volunteers' area and which keys
  may approve the new shops. The others have their own sections below.
- A `.env` and a `community.yml` need no change. Every new setting about access has the default that keeps the site as
  it was: `ADMIN_SECOND_FACTOR=optional`, `ADMIN_WALLET_SIGN_IN=off`, `TEAM_AREA=off`, and
  `team_area: {staff_register_names: false}`.
- On from the first start, each with its switch:
  - the owner's panel by stages ([The panel by stages](features/panel-stages.md)); `features: {stages: false}` gives
    back the panel as it was;
  - Caddy answers the robots' probes itself, `REFUSE_PROBES=off` turns it off
    ([Every variable](configuration.md#every-variable));
  - the settings watch, which sends nothing at its first start; `settings_watch: {enabled: false}` turns it off;
  - the day's list names the owners stuck in the merchant's training for more than `training.nudge_days` days (3; `0`
    never), only where the community has the `training` block.
- Where new shops wait for an approval: the owner is told when the shop is approved (email and the shop's Telegram
  chats), and the printed sign and the card for customers wait for it
  ([The sign waits for the approval](features/shop-page.md#the-sign-waits-for-the-approval)). A shop that is not for
  the map can be set aside from `/admin/today/` instead of approved.
- A Lightning address the shop's form refuses now says why (no such address, no LNURL-pay, no LUD-21, and so on:
  [Wallets](wallets.md)); the addresses accepted and refused are the same as before.
- German: the catalog now says «die Wallet» and «Thekenschein» (it said «das Wallet» and «Händlerschein»), and
  «Sie» everywhere. A
  community that keeps its own German texts beside the software's reads them again for the same words.
- `cryptography` is now 50.x in `requirements.lock` (it verifies the passkeys), and the image takes `zlib1g` from
  Debian's security repository.
- Moderators: `manage.py preflight` warns when they have no channel, `manage.py notify_test --moderators` sends them a
  test message, and only mail that really left counts as sent.
- For a community's own code: events to listen to and template blocks to fill
  ([The community's own code](architecture.md#the-communitys-own-code)); the warnings of any code reach the log with
  no configuration. `python -m config.pot --since 0.1.9` lists the texts that changed, for the translations a
  community keeps.

### The second factor and going back

- The migration `shops` 0010 runs by itself: a new table for the second factor of the staff accounts
  (`AdminSecondFactor`). Nothing changes until an account turns it on, or `ADMIN_SECOND_FACTOR=required` is set
  ([Configuration](configuration.md#admin-and-access-log)); the two new settings have defaults, and compose passes them.
- Back on a release without it, the table stays and nobody reads it: an account with a second factor signs in to
  `/admin/` with the password alone, and its session lasts as a password's does. Sessions opened that way end at their
  next page of `/admin/` once the release with the second factor is installed again, and the second factors are there
  again as they were. If `/admin/` is open to every address, going back takes that defence away while it lasts.
- Templates of the admin copied into a community's own folder: the second factor's pages are
  `admin/second_factor.html` and `admin/second_factor_code.html`, and `admin/base_site.html` has a link to it.

### Passkeys and going back

- The migration `shops` 0012 runs by itself: two new tables for the passkeys of the staff accounts (`AdminPasskey`,
  `AdminPasskeyHandle`), with database defaults. Nothing changes until an account adds one.
- The second factor's setting is `ADMIN_SECOND_FACTOR` (`optional`, `required`, `passkey`), `optional` by default.
- Back on a release without passkeys, the tables stay and nobody reads them: an account with only passkeys signs in to
  `/admin/` with the password alone (an account with a code from an app too, before the second factor existed), and
  with `ADMIN_SECOND_FACTOR=passkey` the previous image does not know the setting either. If `/admin/` is open to every
  address, going back takes that defence away while it lasts. Installing the release with passkeys again brings them
  back as they were, and sessions opened meanwhile end at their next page of `/admin/`.

### The settings watch and going back

- The migration `website` 0003 runs by itself: two new tables, `WatchedSetting` (with database defaults) and
  `SettingsWatch`, linked to nothing. The first start after it records the security settings without a message
  ([The settings watch](configuration.md#the-settings-watch)). `web` now also gets `REFUSE_PROBES` and
  `TRUSTED_PROXIES` from the compose file, with Caddy's defaults: a `.env` needs no change.
- Back on a release without it, the tables stay and nobody reads them: no message while it runs. Installing the release
  again compares with what was recorded before going back, so a setting changed meanwhile is told at that start.

### Wallet sign-in to `/admin/` and going back

- The migrations `shops` 0013 and 0014 run by themselves: two new tables, the wallets the staff accounts link to sign
  in to `/admin/` (`AdminWallet`) and the codes a wallet signed (`AdminWalletChallenge`), with database defaults.
  Nothing changes until `ADMIN_WALLET_SIGN_IN=on` (default `off`, and compose passes it).
- Back on a release without it, the tables stay and nobody reads them: no wallet signs in, the login page has no link,
  and the sessions opened with a wallet go on as sessions of the password alone while that release runs. Installing
  the release with it again brings the wallets back as they were; a session opened with a wallet then holds again only
  while its wallet, the switch and the level still allow it. The previous image does not know `ADMIN_WALLET_SIGN_IN`
  and ignores it.
- A key linked to a staff account is refused at the shops' sign-in by this release only: a wallet linked here that
  scans the shops' sign-in while the previous image runs opens an empty account of its own there, as any new wallet
  does (no shop, no right in `/admin/`).
- Templates of the admin copied into a community's own folder: the new pages are `admin/wallet_login.html` and
  `admin/login_with_wallet.html` (the login page with the link, used only when the wallet signs in), and
  `admin/second_factor.html` has a new section.

### From 0.2.0 to 0.2.1

- No migration, nothing to change. New, for a community with a test site and a real one: one of `register.relays` may
  be written `"wss://relay.{site_host}"` (in quotes), a name under the site's own
  ([Copies on Nostr](features/nostr-copies.md#turning-it-on)). Every address written before means what it meant.
- An example git hook, `examples/git-hooks/commit-msg`, for whoever works on the code: off until it is linked.

### From 0.3.0 to 0.4.0

- **One migration** (`shops.0015`): ten columns more on the other places of a shop. It only adds; every place that is
  there is approved and on the map, as it was. No new variable in the `.env`; one new optional key in
  `community.yml`, `fallback_language`. There was no 0.3.1: what was meant for it is here.
- **The other places of a shop** ([A shop with several places](features/shop-places.md)):
  - the owner says for each place whether it is on the map, and can write its phone;
  - a place its owner adds, and a new address or point of an approved one, **wait for the community's approval**
    where new shops do (`features: {approval: false}` turns both off): they were on the map as they were saved. One
    line of `/admin/today/` for each shop says what its owner asked, and one button approves all of it;
  - «Remove» hides a place at once and keeps it 30 days for the owner to take back; it deleted it.
- `/shops.json`: `places` has only the places the public sees, and each has `phone` when its owner wrote one. A shop
  whose places all wait, are off the map or were taken away has no `places`, as a shop with none.
- For a community's own code: `ShopPlace.objects.create(...)` and a place saved in `/admin/` are approved as they
  are saved, as before. `shop.places.all()` now also has the places that wait, are off the map or were taken away:
  what shows them to the public reads `place.shown`, or `shops.places.shown(shop)`.
- **The VAT number of a shop assumes no country**: a number with the letters of a country outside the EU, or with
  none, is saved with no doubt (it was «may have a mistake», and eleven bare digits were taken for an Italian
  number). The checks of the numbers that start with `IT` or with another EU country's letters are as they were.
  Two texts gone ([What customers see of a shop](features/shop-details.md#the-vat-number)).
- 28 new texts to translate in a catalog of your own, all about the places (`python -m config.pot --since 0.3.0`
  lists them).
- **Five more languages**: the software's texts (panel, register, map, emails) now come in Spanish, French, Dutch,
  Portuguese and Slovenian (`es`, `fr`, `nl`, `pt`, `sl`), beside Italian and German. None of the five has been read
  by a native speaker yet, and each says so: the language switcher shows them with the `*`
  ([Adding a language](languages.md#read-by-a-native-speaker-or-not)).
- What changes for a site that already lists one of these languages in `languages`:
  - with no catalog of its own for it, the software's texts were in English and are now in that language, and the
    language gets the `*` in the switcher (`native_reviewed` in `community.yml` takes it away);
  - with a catalog of its own, that catalog still comes first, text by text: only the texts it leaves out change, from
    English to the software's translation. Whether the language has the `*` is still what that catalog says.
- **A text of `community.yml` missing in a language is shown in English** when English is one of the site's
  languages, and only then in the default language (it was the default language straight away). It changes what a
  visitor sees only on a site whose default language is not English, which has English, and where a text written
  per language lacks a third language: that visitor read the default language and now reads English.
  `fallback_language: false` in `community.yml` keeps it as it was, and the key can name another language, for all
  or one at a time ([Adding a language](languages.md#a-text-missing-in-a-language)). One new optional key.
- With images, the new catalogs are compiled at the build like the others; from the sources, run
  `manage.py compilemessages` once.
- Going back to 0.3.0: it runs on the migrated database, but it knows none of the new columns, so it shows on the
  map **every** place, the ones that wait, the ones off the map and the ones taken away included, and its «Remove»
  deletes. Before going back for longer than a moment, look at what `/admin/today/` has waiting.

### From 0.2.1 to 0.3.0

- No migration and no new variable in the `.env`; one new text, one new optional key in `community.yml`. What changes
  is mostly under the site: the newest stable release of everything it stands on.
- **The language switcher marks the languages no native speaker has read** with a small `*`, and one line explains
  it (in the open menu of a small screen; on a large one when the switcher is pointed at). Italian has been read;
  German and the English the texts are written in have not. The community's own language, the first of `languages`,
  never has the mark, so a site whose first language is English and whose second is Italian sees nothing new.
  `native_reviewed` in `community.yml` has the last word, for one language or for all, and a catalog of the
  community can say it of itself ([Adding a language](languages.md#read-by-a-native-speaker-or-not)). One new text
  to translate in a catalog of your own: "Not read by a native speaker yet."
- **Django 6.1** (it was 5.2). For a site nothing to do. For a community's own code, which uses Django directly, read
  [Django's notes for 6.0 and 6.1](https://docs.djangoproject.com/en/6.1/releases/): `first()` and `last()` no longer
  order by the primary key after an empty `order_by()`; the `EMAIL_*` settings, `get_connection()` and `fail_silently`
  still work and are deprecated in favour of `MAILERS` (the software keeps reading `EMAIL_HOST` and the others from
  the `.env`, and will move by itself before Django removes them); PostgreSQL 15 at least.
- A sender written with brackets or a colon in its name, `DEFAULT_FROM_EMAIL=Shops (automatic) <shops@example.org>`,
  keeps its whole name: the software puts it in quotes, because Django 6.1 reads the part in brackets as a comment
  and leaves it out.
- **gunicorn 27** (it was 23), with its new control socket turned off; **websockets 17.4** (it was 15); requests
  2.34, psycopg 3.3, Markdown 3.11.3; **PostgreSQL 18.7** in `docker-compose.yml` (a minor version: it starts on the
  data of 18.6 as they are).
- The images are built on newer bases: **Caddy 2.11.9** (it was 2.10.2, with known advisories in Caddy itself, in the
  Go libraries inside it and in Alpine's OpenSSL), and a newer pip in the site's image. The Caddyfile is the same; a
  community that builds its own Caddy image from the software's follows by itself at its next build.
- The site's image asks Debian's mirrors over https while it is built (it was plain http): a machine that builds
  it must reach `deb.debian.org` on port 443.
- The two icons of Leaflet's layers control, which its style sheet names, are now there with the rest of Leaflet.
- The Python code was formatted with ruff in one go ([ruff.toml](../ruff.toml)): whoever keeps patches of their own
  on top of the software rebases them once, and may run `ruff format` on their side first to make it easier.
- Going back to 0.2.1 needs nothing: no table and no column changed.

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
  - Back on 0.1.8, a shop with several Telegram chats, or a `TELEGRAM_MODERATION_CHAT` with commas, gets no Telegram
    messages (0.1.8 reads the list as one chat), and the owner's form refuses the value until it is one chat.
  - Back on 0.1.9, the shops set aside from `/admin/today/` are waiting shops again for it: on its list of the day and
    in its counts, still off the map, until the release is installed again. Approving one there approves it for good:
    it keeps its set-aside date, but a shop set aside counts only while it is not approved, so back on this release it
    is simply approved (not under the filter «set aside», nothing on its page about it).
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
reading the release notes and testing on a test site, and a new version is taken only once it has been out for a week
(most packages published with something bad inside are found within days): only a fix for a known vulnerability
does not wait.

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

In the notes of the release, say which texts changed: `python -m config.pot --since <the previous tag>` lists the ones
that are new and the ones that are gone (a reworded text is one of each), so that a community with a catalog of its own
([Adding a language](languages.md)), or with pages that quote the words of a button, knows what to look at.

## Mainnet

Mainnet is a new installation: the decisions and the checks before opening are in [Going to mainnet](mainnet.md), the
backup and the full restore in [Backup and restore](backup.md).

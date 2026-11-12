# Advanced installation: images, CI and a release timer

The way for a community with several people and a release process. For a single server without a registry or a CI,
[One machine, from the sources](single-machine.md) is simpler.

From a clone of the software to a site that answers, on the public signet or on mainnet, installed from the
community's images ([releases.md](releases.md#releases-from-images)). One script prepares the folder; the release timer starts the
stack; what is left is said at the end, in order.

**Before you open the site to the public,** read [Privacy and data protection](privacy.md): the community that runs the site is the
controller of its visitors' and merchants' data, and fills in the privacy page. bitmentor is provided "as is", without warranty,
and is not legal advice (see [LICENSE](../LICENSE)).

Mainnet is always a new installation, with `--network mainnet`: a signet site is never switched over. Read
[Going to mainnet](mainnet.md) first.

## Which network

| | Public signet | Mainnet |
|---|---|---|
| What for | The community's test site: real wallets in test mode, test coins, nothing of value | Real payments |
| `SITE_MODE` | `development` or `maintenance`: `production` starts only on mainnet | `development` until the checks of [Going to mainnet](mainnet.md), then `production` |
| What changes | the invoices and address prefixes the register expects (and, with on-chain turned on, the explorers it asks), sign-in links in the web log without `EMAIL_HOST`, `demo_data` and `FAKE_EXPLORER` allowed | real money: `demo_data`, `FAKE_EXPLORER` and `DEBUG` are refused |

The server runs no Bitcoin node and no payment server: the registers ask the merchants' own Lightning addresses
(LNURL-pay and LUD-21 `verify`) and, where on-chain is turned on, a public explorer, straight from the phone. `NETWORK` only tells the
site and the registers which network the money is on.

## What you need

- A machine with Docker 27 or later and Docker Compose 2.26 or later, `flock` (util-linux) and systemd; 2 GB of
  memory and a few GB of disk for the images, Postgres and the backups are enough.
- A name in DNS for the site, for example `shops.example.org`.
- The community's images in a registry the machine can pull from: the site image (with the community in
  `/community`) and the Caddy image, with the tag `active` on the version to install.
- A clone of this software on the machine, at the same version as the images or close to it: the script and the
  examples come from the clone, everything else comes later from the images.

## The community's images

The machine installs two images: the site with the community inside, in `/community`, and Caddy with the software's
rules. A community builds its own on top of the software's, in its own repository and CI. The site image, with the
community folder next to this `Dockerfile`:

```dockerfile
FROM registry.example.org/bitmentor/web:v2026.11.10
COPY --chown=app . /community
```

A `locale/` in the community folder needs its `.mo` files compiled before the build (`msgfmt`, or
`manage.py compilemessages` with `COMMUNITY_DIR` set).

And Caddy, with the community's own rules (optional) and its under-construction page:

```dockerfile
FROM registry.example.org/bitmentor/caddy:v2026.11.10
COPY caddy/ /etc/caddy/local/
COPY maintenance/ /srv/maintenance/
```

Tag both with the same version, then move `active` (or write the digests in `release.env`: see
[Pinning the images by digest](releases.md#pinning-the-images-by-digest)). An example CI that tests and publishes the
software's images is [examples/ci/gitlab-ci.yml](../examples/ci/gitlab-ci.yml).

## 1. Prepare the folder

As root, from the clone:

```bash
deploy/install.sh /opt/shops --site shops.example.org --network signet \
  --web-image registry.example.org/community/web --caddy-image registry.example.org/community/caddy
```

Without the options it asks for them; `--yes` asks nothing and stops on what is missing. The folder's name is the
project's name (`shops` here): Docker names containers, networks and volumes after it, and the systemd units run
`/opt/<project>`.

It writes, only where they are missing:

- `.env`, from `.env.example`, readable by root only: `NETWORK`, `SITE_HOST`, `SITE_MODE=development`, and
  `SECRET_KEY`, `POSTGRES_PASSWORD` and `WEB_DB_PASSWORD` generated on the spot. The secrets never appear on the screen or on a command line;
- `release.env`: `PROJECT`, `SITE`, `WEB_IMAGE_REPO`, `CADDY_IMAGE_REPO`, `TAG` (`active`, or `--tag`);
- `compose.locale.yml`, from `deploy/examples/compose.locale.yml`, for the local changes of this installation;
- `bin/release.sh`, `bin/check.sh`, `bin/mode.sh`, which each release then replaces with its own, and
  `bin/compose.sh`, `docker compose` with the installed version's files and images.

With `--entry shared` it also writes `SITE_SCHEME=http` and, in `TRUSTED_PROXIES`, the subnet of the Docker network
`bitmentor-entry`: the entry point holds TLS and speaks HTTP to this Caddy, which must believe the visitor's address
it passes on ([configuration.md](configuration.md#behind-another-proxy)).

It refuses, writing nothing:

- a folder whose project already has Docker volumes but no `.env`: they belong to another installation, maybe of
  another network, and the new passwords would not open them;
- a site name already served by an installation next to this one (`/opt/*/release.env`);
- ports 80 and 443 taken, with the direct entry point: see “Two installations on one machine”;
- `--entry shared` without the Docker network `bitmentor-entry`: start the entry point first, below;
- a folder with a `.env` and no `release.env`: an installation from the sources, which it does not touch;
- names, images, networks or ports written wrong.

Run again, it writes nothing that exists, checks that what you give agrees with what is written (another `--network`
against the `.env` is refused), and says again what is left. It never starts anything.

## 2. Read what is left

The script ends with a numbered list, which shrinks as the steps get done:

1. **DNS**: the name points to the machine. AAAA records only after [configuration.md](configuration.md#ipv6), “IPv6”.
2. **Start**: the systemd units, then the timers:

   ```bash
   cp -n deploy/systemd/bitmentor-* /etc/systemd/system/ && systemctl daemon-reload
   systemctl enable --now bitmentor-release@shops.timer bitmentor-check@shops.timer \
     bitmentor-sentinel@shops.timer bitmentor-dashboard@shops.timer \
     bitmentor-osm@shops.timer bitmentor-meetups@shops.timer
   journalctl -fu bitmentor-release@shops
   ```

   The units are templates shared by every installation on the machine: `cp -n` copies only the missing ones, and
   the script prints only those. `bitmentor-backup@shops.timer` comes once `backup.env` is ready
   ([Turn on the backup](backup.md#turn-on-the-backup)). What each timer does, when it runs, what it needs and how to
   update the units is in [The timers of a production machine](operations.md#the-timers-of-a-production-machine): the
   Telegram chat of the moderators (`TELEGRAM_MODERATION_CHAT`) and the email settings are the ones to have ready.

   Within a minute `release.sh` pulls the version tagged `active`, starts the stack and checks that the site answers:
   the log ends with `installed <version>`. `/health/` answers 200 once the site reaches its database.
3. **The site's first administrator**: `bin/compose.sh exec web python manage.py createsuperuser`.
4. **Who sees the site** in development: your addresses in `DEV_ALLOWED_IPS`, then `bin/compose.sh up -d web caddy`.
5. **Email** and **alerts** (`alerts.env`, [releases.md](releases.md#releases-from-images)).
6. **The check**: `bin/compose.sh exec web python manage.py check` says `System check identified no issues` when the
   configuration holds together.
7. On mainnet, the decisions and checks of [Going to mainnet](mainnet.md): `preflight` without options must say
   `All ready` before real payments.

## The network

`NETWORK` is the only setting that picks the network: `install.sh` refuses a `--network` that disagrees with the
`.env`, and `manage.py check` and `preflight` (which wants `mainnet`) say what does not add up. A database that has run
on one network should not be moved to the other: start the other network with a new installation.

## Two installations on one machine

For example the community's mainnet site and its public signet site. The project name, which is the folder's name,
keeps containers, networks and volumes apart; two more things must be different:

- **ports 80 and 443**: only one Caddy can hold them. Put both behind the shared entry point, below: `--entry shared`
  uncomments the `#@shared` lines of the example;
- **`IPV6_SUBNET`**, if IPv6 is on: a /64 of its own for each ([configuration.md](configuration.md#ipv6)).

Two clones [from the sources](single-machine.md) follow the same rule: two folders with different names, and both
behind the shared entry point. In each clone, copy `deploy/examples/compose.locale.yml` next to `docker-compose.yml`,
delete its `#@direct` line, remove `#@shared ` from the others and write the folder's name in place of `<project>`;
then in the `.env`: `COMPOSE_FILE=docker-compose.yml:compose.locale.yml`, `SITE_SCHEME=http`, and the entry point's
subnet in `TRUSTED_PROXIES`.

## The shared entry point

A Caddy of its own, in front of every installation on the machine: it holds ports 80 and 443 and the certificates,
and forwards each name over HTTP to the alias `caddy-<project>` on the Docker network `bitmentor-entry`. A minimal
one is in `deploy/examples/entry`:

```bash
cp -r deploy/examples/entry /opt/entry
# in /opt/entry/Caddyfile, one block per installation: its name -> caddy-<project>:80
cd /opt/entry && docker compose up -d
```

It creates the network with a fixed subnet (`172.31.250.0/24`, to change if taken), which `install.sh --entry shared`
then writes in `TRUSTED_PROXIES`. After a change to its Caddyfile: `docker compose restart caddy`. The DNS of every
name points to the machine, and the certificates are the entry point's: `check.sh` checks the one on port 443,
the entry point's.

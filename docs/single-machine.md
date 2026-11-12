# One machine, from the sources

The whole stack on one server: the site, Postgres and Caddy, which gets the HTTPS certificates by itself. There is no
Bitcoin node and no payment server: the registers are paid straight into the merchants' own wallets. No registry, no CI: the server builds the site from this repository, and you update it with
`git pull` and one command. For a community with several people, automatic tests and a release process, see
[Advanced installation](installation.md).

Before opening the site to the public, read [Privacy and data protection](privacy.md) and fill in the privacy page.
The software is provided "as is", without warranty; you are responsible for your installation and its legal compliance.

## What you need

- A Linux server with Docker and Docker Compose 2.26 or newer, 1 or 2 CPUs and 2 GB of memory.
- Disk: a few GB for the images, the database and the backups.
- A name pointing to the server (A record), for example `shops.example.org`.
- Ports 80 and 443 open: Caddy needs them for the certificates.

Signet or mainnet? A first try on signet, the test network, shows the site with coins worth nothing. But few wallets
give a Lightning address that works with the register (LUD-21 `verify`) on signet, so a real sale is hard to try
there. If you find none, try the register on a mainnet installation in `development` mode, paying a few hundred sats:
only the addresses in `DEV_ALLOWED_IPS` see it. Mainnet is always a new installation, never a signet site switched
over: [Going to mainnet](mainnet.md).

## 1. The software and your community

```bash
git clone https://github.com/mammadori/bitmentor.git && cd bitmentor
```

Your community is a folder of its own, outside the software: name, languages, pages, theme. Start from the example and
rewrite it; [Architecture](architecture.md) says what goes where.

```bash
cp -r examples/community ../my-community
```

## 2. The .env

The three secrets are generated here and never shown again:

```bash
cp .env.example .env && for k in SECRET_KEY POSTGRES_PASSWORD WEB_DB_PASSWORD; do sed -i "s/^$k=.*/$k=$(openssl rand -hex 32)/" .env; done && chmod 600 .env
```

Then write in `.env`:

- `SITE_HOST`: the name.
- `COMMUNITY_DIR`: the full path of your community folder, for example `/home/me/my-community`.
- `DEV_ALLOWED_IPS`: your own address. The site starts in `development` mode: only these addresses see it, the others
  see an under-construction page.

Every other setting is explained in [Configuration of the site](configuration.md); the defaults are right for a start.
A missing or wrong value stops the start with a sentence that says what to do.

## 3. Start

```bash
docker compose up -d
```

The first time it builds the site and downloads the other images, a few minutes. `/health/` answers 200 once the site
reaches its database.

The project is the folder's name, `bitmentor` here: Docker Compose names containers, networks and volumes after it
(the database is the volume `bitmentor_postgres_datadir`). Use lowercase letters, digits, `-` and `_`, and do not rename
the folder of a site that holds data: under another name Compose starts on new, empty volumes, and the data stays in
the old ones (`docker volume ls`). `COMPOSE_PROJECT_NAME=<name>` in the `.env` keeps a name whatever the folder is called.

## 4. Check and open

```bash
docker compose exec web python manage.py preflight
```

It lists what is still missing (email, HSTS, mainnet) and what is fine. The first account of
the site's administration:

```bash
docker compose exec web python manage.py createsuperuser
```

Without email (`EMAIL_HOST` and the others in `.env`) nobody gets a sign-in link: set it before inviting the shops.
When everything is ready, `SITE_MODE=production` opens the site to everyone; it is accepted on mainnet only.

## Updates

```bash
git pull && docker compose up -d --build
```

The site migrates its database when it starts. Read the [release notes](releases.md) before an update: some ask for a
change to the `.env`, or for a new database.

## Regular jobs

A machine installed from images runs these with systemd timers
([The timers of a production machine](operations.md#the-timers-of-a-production-machine), which says what each does).
On one machine, put them in cron: as root, in `/etc/cron.d/bitmentor`, with the real path of your clone instead of
`/opt/bitmentor`. All times are UTC.

```text
# The guardian: tries the shops' Lightning addresses that are due, every hour
5 * * * *   root  cd /opt/bitmentor && docker compose exec -T web python manage.py check_lightning > /dev/null
# Every morning: the job that forgets old personal data, then the summary of what to do today
22 6 * * *  root  cd /opt/bitmentor && docker compose exec -T web python manage.py forget_old_requests
30 6 * * *  root  cd /opt/bitmentor && docker compose exec -T web python manage.py dashboard --send
# The meetups published on Nostr, every day (does nothing without the meetups block)
40 5 * * *  root  cd /opt/bitmentor && docker compose exec -T web python manage.py read_meetups
# OpenStreetMap, the first of each month (does nothing without an osm section)
30 3 1 * *  root  cd /opt/bitmentor && docker compose exec -T web python manage.py osm_sync --send
# The backup (docs/backup.md), in a folder of its own for each installation on the machine
0 3 * * *   root  /opt/bitmentor/backup.sh /var/backups/bitmentor
```

Cron mails what a job prints to root (the guardian's errors too: only its count goes to `/dev/null`): the machine needs a mail program for you to see a failure. `/admin/today/` also
says when the guardian has not run for a while (`guardian_late`).

## Backups

`backup.sh` dumps the database every night (the last line above); copy the folder, the `.env` and your community
folder off the machine. The details, and the encrypted copy sent off the machine, are in
[Backup and restore](backup.md).

## Going to mainnet

A new clone with its own `.env` and `NETWORK=mainnet`, in a folder with another name (`git clone <url> shops-mainnet`):
the folder's name is the project's, so the new site has its own containers and its own database. On a new machine, or
on the signet one after `docker compose down` in the signet folder: only one Caddy can hold ports 80 and 443. Both
sites running on one machine need a proxy in front of them:
[Two installations on one machine](installation.md#two-installations-on-one-machine).
Then the decisions and the checks of [Going to mainnet](mainnet.md).

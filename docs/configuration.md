# Configuration of the site

The settings in the stack's `.env` that decide how the site runs and who sees it, and the limits of `community.yml`. A
new installation starts from [One machine](single-machine.md) (or [the advanced installation](installation.md)); this
page explains each setting. [Every variable](#every-variable) is listed at the end.

## Turning features off

The badge of the certified shops, the shops closed for a while and the practice register are on unless `community.yml` has a `features` block that says
`false` for them. What each switch takes away and what it keeps is in
[Turning features off](features/README.md#turning-features-off). `manage.py check` and `manage.py preflight` say which
are off.

## Experimental features

On-chain at the register and writing to OpenStreetMap (`manage.py osm_upload`) are off unless `community.yml` has an
`experimental` block that says `true` for them. What each one turns on, and what experimental means, is in
[Experimental features](features/README.md#experimental-features). `manage.py check` warns about a name or a value it
cannot use (`shops.W011`), and `manage.py preflight` says which are on.

## Limits

An open site that nobody watches every day must not fill up with spam or with data nobody asked for. The `limits` block of
`community.yml` sets how much the site keeps and how often one visitor can ask; every key is optional, a missing or wrong
value is the default (`manage.py check` says which is wrong), and `0` means no limit. The code reads them all from
`config/limits.py`, which is also where the defaults are changed for everybody.

| Key | Default | What it limits |
|---|---|---|
| `shops` | 500 | Shops on the site (the demo shops do not count) |
| `shops_waiting_wallet` | 100 | Shops saved without a wallet yet: sign-ups not finished |
| `accounts` | 2000 | Accounts of merchants and visitors who signed in (staff left out) |
| `help_requests` | 5000 | Requests for help kept, solved ones too |
| `nostr_meetups` | 5000 | Meetups copied from Nostr and kept |
| `emails_per_month` | 1000 | Emails sent this month: the mail provider's quota (see below) |
| `emails_per_day` | a tenth of `emails_per_month` (100) | Emails sent today |
| `emails_per_address_day` | 5 | Emails a visitor can make the site send to the same address in a day |
| `emails_per_sender_day` | 10 | Emails one network address can make the site send in a day, whoever they are for (three times as many for its wider IPv6 network) |
| `emails_staff_factor` | 3 | Times more emails per address and per network address for the staff and the allowed addresses |
| `staff_registers_per_shop` | 30 | Staff registers turned on at the same time in one shop |
| `accounts_per_address_hour` | 20 | New accounts from one network address in an hour |
| `shops_per_address_hour` | 30 | New shops or wallets connected from one network address in an hour |
| `help_requests_per_shop_day` | 3 | Requests for help from one shop in a day |

The first seven are totals. When one is reached the site refuses, with a polite message, whatever would add one more (a
new shop, a new account…), keeps and deletes everything it already has, and writes a line in the log of `web`.
Whoever is already signed in goes on as before. The staff sees every total against its limit (for example `37/500` shops)
in [the dashboard](features/dashboard.md), with a line to look at from 80% and a problem at the limit, and in
`manage.py preflight`, which also lists the limits changed from their defaults. `manage.py check --database default` warns
about the totals near the limit too. When it is spam, clear it in `/admin/` before raising the limit; when the community
grows, raise the number and restart the site.

**The email budget.** The mail provider allows a fixed number of emails a month: set `emails_per_month` to that number
(`0` is no limit).

- Every email the site sends is counted in the database, so the count survives restarts.
- At `emails_per_month`, or at `emails_per_day` (by default a tenth of the month, so that one day cannot use it up),
  nothing is sent: a warning goes to the log and the request goes on.
- The sign-in and confirmation links a visitor asks for stop earlier: at 80% of the month and of the day, and at
  `emails_per_address_day` for the same recipient. A visitor refused is told to try later, and that an account made
  with a Lightning wallet can still sign in with it.
- One visitor cannot use up that share alone: a network address makes the site send at most `emails_per_sender_day`
  emails a day, whoever they are for. Many addresses together still can: watch the count in `/admin/today/`. Many
  people behind one address (an office, a workshop where everybody signs in) share that number: raise it for the
  occasion, or put the address in `DEV_ALLOWED_IPS`, which has `emails_staff_factor` times as many. The visitors of the
  onion address all arrive as one: together they have three times the number, counted apart from the site's visitors.
- The rest is kept for the security notices and the owners' emails (Lightning address checks, address changes,
  vacations, the staff's dashboard): they stop only at the total.
- The staff and the allowed addresses (`SITE_MODE`, below) get `emails_staff_factor` times the per-address cap and the
  per-network-address rate, and skip the 80% reserve. They all still count in the total.
- The dashboard and `manage.py preflight` show the month and the day, with a warning from 80%.

The other limits are per address, author or shop, in a window of an hour or a day. Other caps are not settings: the sizes
of what the site accepts from the network (a body of 1 MB, the Overpass and Nostr readings, a request to a Lightning
address) are fixed in the code next to what they protect, and the rate limits of the other forms (sign-in, checks of a
Lightning address, OpenStreetMap, training) too.

**What the site does not store, and the Nostr relay.** The encrypted copies of the registers (sales, day closings) and the
backups made by the owners live on their phones and on the relays they choose, never in the site's database, so there is no
limit here for their size. The community's Nostr relay is not part of this software: the values it should enforce are
recommended in [the Nostr copies](features/nostr-copies.md#what-the-communitys-relay-must-enforce) and kept in
`RELAY_RECOMMENDED` in `config/limits.py`; nothing enforces them.

## The network

`NETWORK` in `.env` picks the network for the whole stack: `mainnet` or `signet`. The server runs no node: the network
tells the site and the registers which invoices and addresses to expect (and, with on-chain turned on, which explorers
to ask about on-chain payments), and it decides what a test site allows (see [installation.md](installation.md#which-network)). `manage.py preflight` stops
when it is not `mainnet`.

Without `EMAIL_HOST` a test network (`signet`) does not send emails: it writes them to the log of the `web` container,
sign-in links included, so a test site works without a mail server. Whoever reads that log (`docker compose logs web`,
or a log collector) can then sign in as any shop, for 15 minutes per link. A signet site that real people use needs
`EMAIL_HOST`. On mainnet, without it, sending fails and the page says so: no link reaches the log.

## The site's name and processes

`SITE_HOST` is the site's name. Absolute links, that is the register and login QR codes, the sign, email
links, sitemap, canonical URLs and previews, are always `https://SITE_HOST`, never the
name the visitor came in with: someone who opens the site over the onion does not put that address in links that get
shared.
Only the five-minute try of the README, with `DEBUG=1` and no `SITE_HOST`, writes them as `http://localhost:8000`.

`WEB_CONCURRENCY` and `WEB_THREADS` are gunicorn's processes and threads (`config/gunicorn.py`): if unset, 3 and 8, for
a 4-CPU machine. After a change: `docker compose up -d web`.

`LIGHTNING_CHANGE_DAYS` (7 if unset) is how many days must pass between two changes of a shop's Lightning address by
its owner ([Changing a shop's Lightning address](features/address-change.md)).

## Who sees the site

`SITE_MODE` in `.env` decides who sees the site:

| Mode | Who sees what |
|---|---|
| `development`, the default | The site only for the addresses in `DEV_ALLOWED_IPS`, separated by spaces; everyone else sees the under-construction page. No indexing |
| `maintenance` | The under-construction page for everyone, including those in `DEV_ALLOWED_IPS` |
| `production` | The site open and indexable. Starts only with `NETWORK=mainnet` |

`BITMENTOR_DEMO=1` is for a training site only: it lets `manage.py demo_data` fill an empty site that is in maintenance
mode too (development mode needs no setting). Mainnet, production and any site with a real shop or account are refused
whatever it says: see [A training site in five minutes](features/demo.md).

`FAKE_EXPLORER=1` is for a test site on signet with on-chain turned on (`experimental.onchain`): the register asks the
site itself whether an on-chain payment arrived, and `/dev/explorer/` pretends to pay an address (see
[On-chain at the register](features/onchain.md)). The site refuses to start with it on mainnet, and its pages answer 404
there, and wherever on-chain is off, anyway.

`TEAM_ACCESS=on` lets the team in from any address while the site is in `development`: a person invited in `/admin/`
signs with a Lightning wallet, and that browser counts as an allowed address for a week. Off by default, when nothing of
it exists; it does nothing in `maintenance` and `production`. With it on, its few addresses under `/team/` are open
from outside in `development`, and a browser with a pass reaches `/admin/` like an allowed address does: set
`ADMIN_ALLOWED_IPS` if the team is not the staff. See [Team access with a Lightning wallet](features/team-access.md).

In every mode `/health/` for monitoring, and the paths in `OPEN_PATHS`, for
example `/extra /extra/*`, stay open. The default under-construction page is `maintenance/index.html`; a community sets its
own with `MAINTENANCE_DIR`. The rules are in `caddy/<mode>.caddy`. After a change: `docker compose up -d web caddy`.

Caddy must see the real address of each visitor. Docker keeps it on IPv4; on IPv6, if the site has an AAAA record,
everyone arrives from the gateway of the Docker network, which therefore must not go in `DEV_ALLOWED_IPS`, unless
IPv6 is on in the Docker network: see [IPv6](#ipv6).

## Behind another proxy

If a proxy in front holds TLS, for example because the machine hosts several installations, Caddy answers over HTTP:
`SITE_SCHEME=http`, and in `TRUSTED_PROXIES` the proxy's network, from which it accepts the visitor's real address.
Ports 80 and 443 are published by the proxy, not by this Caddy: `compose.locale.yml` takes them away from it, as in
[Two installations on one machine](installation.md#two-installations-on-one-machine).

## IPv6

Off by default: without an AAAA record the site is reached over IPv4 only, and nothing changes. To serve IPv6 visitors,
Caddy must see each one's address, or they all arrive from the gateway of the Docker network: they share the request
limits (admin login, email links, new shops), and `DEV_ALLOWED_IPS` and `ADMIN_ALLOWED_IPS` cannot tell
them apart. `compose.ipv6.yml` turns IPv6 on in the stack's Docker network, so Docker translates the address instead
of handing the connection to `docker-proxy`:

1. the Docker daemon translates IPv6 with ip6tables: the default from Docker 27; on Docker 26, `"ip6tables": true` and
   `"experimental": true` in `/etc/docker/daemon.json`, then a restart of the daemon. Without it, with a firewall that
   drops forwarded traffic (ufw, for one), the containers do not even reach each other over IPv6;
2. in `.env`, `IPV6_SUBNET`: a ULA /64, one of its own for each installation on the machine, for example
   `fd12:3456:789a:1::/64` (pick random hex digits after `fd`). From the sources, also
   `COMPOSE_FILE=docker-compose.yml:compose.ipv6.yml`; `deploy/release.sh` and `deploy/mode.sh` add the file by
   themselves when `IPV6_SUBNET` is set;
3. the network changes only when it is created again, a minute without the site. From the sources:
   `docker compose down && docker compose up -d`. From images, first install a release that has `compose.ipv6.yml`
   (the `release.sh` of that release then copies the file into `current/` by itself, within a minute of
   `IPV6_SUBNET` appearing), then in `/opt/<project>`, with the images of `state/version` (`WEB_IMAGE_REPO` and
   `CADDY_IMAGE_REPO` from `release.env`), adding `-f current/compose.community.yml` and `-f compose.locale.yml` if
   they exist:

   ```bash
   . ./release.env; v=$(cat state/version)
   c() { WEB_IMAGE=$WEB_IMAGE_REPO:$v CADDY_IMAGE=$CADDY_IMAGE_REPO:$v docker compose -p "$PROJECT" \
     --project-directory "$PWD/current" --env-file "$PWD/.env" -f current/docker-compose.yml \
     -f current/compose.release.yml -f current/compose.ipv6.yml "$@"; }
   c down && c up -d --no-build
   ```

   Docker Compose keeps an existing network as it is, without a word: nothing changes until these commands;
4. the firewall lets in ports 80 and 443 over IPv6 too, and only then the AAAA record of `SITE_HOST` goes in the DNS.

`manage.py preflight` stops when `SITE_HOST` has an AAAA record and the site's container has no IPv6 address (not
with `SITE_SCHEME=http`: behind another proxy the AAAA record is the proxy's). An IPv6 address in the container does
not prove that the daemon translates with ip6tables, so check by hand, with `ACCESS_LOG=on`: a request from an IPv6 machine
must show its own /48, not the one of `IPV6_SUBNET`.

Docker before version 28 also lets a machine on the same local network (the same L2 segment as the server) reach the
containers' ports directly, `web` included, without Caddy. On IPv6 too, once the network has it: prefer Docker 28 or
later, or drop forwarded traffic towards the Docker networks that does not come in through the published ports.
Addresses of IPv6 visitors count by /64, or /48 where a network that keeps trying matters (`shops.views.client_ip`);
`DEV_ALLOWED_IPS` and `ADMIN_ALLOWED_IPS` take IPv6 addresses and networks, for example `2001:db8:1::/48`.

## `/admin/` and access log

`/admin/` asks only for the password, with no second factor. With `ADMIN_ALLOWED_IPS`, addresses or networks
separated by spaces, for example `203.0.113.7 2001:db8:1::/48`, Caddy opens it only to those and answers 403 to
everyone else, onion included. Without the variable it stays open to everyone. Behind another proxy, the visitor's real
address is what counts, as long as the proxy is in `TRUSTED_PROXIES`. Set it: open to everyone, the only defence of
the staff accounts is the password and the sign-in limits, 10 tries per address, 30 per /48 network and 20 per account
every 15 minutes. The limit per account also stops whoever tries from many addresses. While it lasts, a browser that
signed in to that account before (a signed cookie, `admin_device`, kept 180 days and void once the password changes)
can still sign in; a new browser has to wait the end of the window. In production set `ADMIN_ALLOWED_IPS`: from an
address in it nobody else can use up the limits.

With `ACCESS_LOG=on` Caddy writes the access log, in JSON, to `/data/access/` in its volume:
`docker compose exec caddy tail -f /data/access/access.log`. It is there to reconstruct an incident, so it keeps
little, and only what is needed:

- a new file every MiB, ten at most, the old ones compressed and deleted after 7 days. On an almost idle site the
  current file can hold older lines until it reaches one MiB;
- only the network of each address, /24 on IPv4 and /48 on IPv6, with Caddy's `ip_mask` filter: enough to recognise
  a network that keeps trying, not to tell who a person is;
- time, method, host, path, status, duration and size. No query string, where access tokens and signatures travel,
  and no headers, so neither cookies nor referer. The one path that carries a token, the link of a
  [team invite](features/team-access.md), is written without it (`/team/join//`), and so is the code a team
  sign-in polls (`/team/login//`).

The default is `off`, no log. After a change: `docker compose up -d caddy`. Caddy's own log of errors (a 502 during a
release, for one), always on, keeps the addresses the same way, /24 or /48, and drops from the query and the headers
what opens something.

## Every variable

The `.env` of the stack. Empty or missing means the default.

| Variable | Default | What it is |
|---|---|---|
| `NETWORK` | `signet` | `signet` or `mainnet`: [The network](#the-network) |
| `SITE_HOST` | none, required | The site's name: [The site's name](#the-sites-name-and-processes) |
| `SITE_MODE` | `development` | Who sees the site: [Who sees the site](#who-sees-the-site) |
| `SITE_MODE_SINCE` | written by `deploy/mode.sh` | Since when the mode is in use, shown by `/health/` |
| `DEV_ALLOWED_IPS` | empty | Addresses that see the site in `development` |
| `TEAM_ACCESS` | `off` | `on`: in `development` the team also gets in by signing with a Lightning wallet, from any address: [Team access](features/team-access.md) |
| `OPEN_PATHS` | empty | Paths open in every mode, for example `/extra /extra/*` |
| `MAINTENANCE_DIR` | the software's page | Folder with the under-construction page (`index.html`) |
| `ADMIN_ALLOWED_IPS` | empty: from anywhere | Who reaches `/admin/`: [`/admin/` and access log](#admin-and-access-log). Caddy applies it; `web` gets it too, only for `manage.py preflight` to say when it is empty with team access on |
| `SOURCE_URL` | `https://github.com/mammadori/bitmentor` | The link to the source code on every page (the footer of the site and of the admin, the register's menu, the tools of the printable sheets), as section 13 of the AGPLv3 asks: point it to your version if you changed the code |
| `ACCESS_LOG` | `off` | `on`: Caddy's access log, addresses reduced to their network, 7 days |
| `ACCESS_LOG_KEEP` | `168h` | How long Caddy keeps the access log (a Caddy duration) |
| `LOG_IPV4_MASK`, `LOG_IPV6_MASK` | `24`, `48` | How much of a visitor's address Caddy's logs keep: the prefix length; `32` and `128` keep the whole address |
| `LOG_MAX_SIZE`, `LOG_MAX_FILE` | `20m`, `5` | Docker's limit on each container's output, per file and number of files |
| `COMMUNITY_DIR` | `./examples/community` | The community folder ([Architecture](architecture.md)) |
| `SECRET_KEY` | none, required | Django's secret: `openssl rand -hex 32` |
| `POSTGRES_PASSWORD` | none, required | The Postgres administrator's password, set for good at the first start |
| `WEB_DB_PASSWORD` | none, required | The site's database password, set for good at the first start |
| `SITE_SCHEME`, `TRUSTED_PROXIES` | `https`, empty | [Behind another proxy](#behind-another-proxy) |
| `HSTS_SECONDS` | `86400` | HSTS: one day while trying the domain, one year (`31536000`) in production |
| `IPV6_SUBNET` | empty: no IPv6 | A ULA /64 for the stack's network: [IPv6](#ipv6) |
| `ONION_LOCATION` | empty | The site's onion address, `http://<56 letters>.onion`: browsers that support Tor are told about it |
| `WEB_CONCURRENCY`, `WEB_THREADS` | 3, 8 | Gunicorn's processes and threads |
| `LIGHTNING_CHANGE_DAYS` | 7 | Days between two changes of a shop's Lightning address by its owner |
| `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD` | empty, `587` | The mail server (STARTTLS on 587) |
| `DEFAULT_FROM_EMAIL` | `noreply@localhost` | The sender, on the community's domain |
| `TELEGRAM_BOT_TOKEN` | empty | The bot that writes the site's messages |
| `TELEGRAM_MODERATION_CHAT` | empty | The moderators' chat: a chat number or an `@name`. Alerts about Lightning addresses and requests for help, and the morning summary |
| `TELEGRAM_API` | `https://api.telegram.org` | Another address for Telegram's API (a test server) |
| `NOSTR_SECRET_KEY`, `NOSTR_RELAYS` | empty | The community's Nostr key and relays (separated by commas), for `manage.py publish_nostr` |
| `OSM_TOKEN` | empty | For `manage.py osm_upload`, an experimental command that is off by default. Passed only by hand, never in the `.env`: [OpenStreetMap](features/openstreetmap.md) |
| `BITMENTOR_DEMO` | empty | `1`: a training site in maintenance mode ([demo](features/demo.md)) |
| `FAKE_EXPLORER` | empty | `1`: the fake on-chain explorer, on a signet test site only, with on-chain turned on |

Not in the `.env`: `SITE_VERSION`, a version of the community's own that `/health/` shows next to the software's, goes
in the community's image (`ENV SITE_VERSION=…`); `POSTGRES_HOST`, `POSTGRES_USER`, `POSTGRES_DB`, `SQLITE_PATH` and
`DEBUG` are for development and tests outside Docker Compose.

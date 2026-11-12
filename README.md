<h1><picture><source media="(prefers-color-scheme: dark)" srcset="docs/img/bitmentor-logo-dark.svg"><img src="docs/img/bitmentor-logo.svg" alt="bitmentor" width="420"></picture></h1>

Tools that help the shops of a community accept bitcoin. Each shop is paid with Lightning, into its own wallet,
through the wallet's Lightning address.

The community holds no funds and no keys. Seeds, wallet API keys or credentials that can spend never go on the server,
and the server runs no Bitcoin node and no payment server: the register in the browser asks the shop's Lightning
address for each invoice (LUD-16, LNURL-pay) and checks the payment with its LUD-21 `verify`.

The merchant picks the wallet. For a corner shop the easy one is self-custodial on [Spark](https://www.spark.money/):
it receives with the phone switched off, with no channels and no liquidity to manage. A merchant who runs a Lightning
node of their own uses its address in the same way. See [Choosing wallets for your community](docs/wallets.md).
On-chain payments at the register are an experimental feature for special cases, off unless the community turns it
on: see [On-chain at the register](docs/features/onchain.md).

## What the site keeps

The site keeps no sales and no amounts. The history of the sales stays on the register's phone. The merchant keeps
copies as an encrypted file, or, if the community turns it on, on a Nostr relay ([Copies on Nostr](docs/features/nostr-copies.md)).
There is one exception, and only where the community turned on-chain on (it is experimental, and off by default):
for the shops that use it, a counter of the addresses handed out (`ChainCount`), so that two registers never show
the same address. It tells how many on-chain sales a shop started, never amounts or payments.

bitmentor is run by an association of volunteers for its community: a map of the shops, a register in the browser for
each shop, a sign with a QR code, and the pages of the community. The software contains no data of any community: see
[Community](#community).

![How bitmentor works: customers pay the shops' own wallets; the community's site shows the map and gives every shop a register in the browser, which asks the shop's Lightning address for invoices and checks them itself; the site holds no funds, no keys and no sales](docs/img/architecture.svg)

## Origin

<!-- origin -->
bitmentor was created for the [Bitcoin Valley](https://bitcoinvalley.eu) and first implemented there, as an easy tool
for bitcoin communities: what one town built for its shops, any other can install for its own.
<!-- /origin -->

This section is the only place where the software names a community: the name checks of the tests skip it.

## Status

bitmentor is young, and this is its first public version.

- **Not in production yet.** So far it has run on test sites only. The register was tried on mainnet with real
  payments and one wallet ([Choosing wallets](docs/wallets.md)): no community runs its shops on it yet.
- **No independent security review.** The register's cryptography in the browser is written for this project and
  checked against the public test vectors, not audited: see [Limitations](docs/limitations.md).
- **It may change without a way to upgrade.** Until a first stable version the names in `community.yml` and the
  database can change from one version to the next, with no migration.
- **Versions** are named by their day, `vYYYY.MM.DD` (`-N` for another one on the same day): the list is the tags of
  the repository.

Found a security problem? Tell us privately: [SECURITY.md](SECURITY.md).

## No warranty, no legal advice

bitmentor is provided "as is", on a best-effort basis, without warranty of any kind (see the [LICENSE](LICENSE), GNU AGPLv3). The
developers and contributors accept no liability for data, funds, availability or compliance, and nothing here or in the
documents is legal, tax or financial advice. **Each community is responsible for its own installation, its configuration
and its legal compliance**, the protection of personal data included (the GDPR applies in the EU/EEA and to sites aimed at
people there; check the law of your own country): read [Privacy and data protection](docs/privacy.md) before going live.

## Where to start

| You are | Read |
|---|---|
| A volunteer who runs the site | [The association's handbook](docs/operations.md): what to do every day, every week, before a release, and when something breaks |
| The person who answers a merchant's call | [When a merchant calls](docs/helpdesk.md) (start from the requests in `/admin/today/`), and [a training site in five minutes](docs/features/demo.md) to practise first |
| Installing a new site | [Three ways to run it](#three-ways-to-run-it): usually [one machine](docs/single-machine.md) |
| Going to mainnet | [Going to mainnet](docs/mainnet.md): a new installation, and [Backup and restore](docs/backup.md) |
| A community that wants to use bitmentor | [Community](#community), [Choosing wallets](docs/wallets.md), then [Features](docs/features/README.md) and [Architecture](docs/architecture.md) |
| A developer | [Try it](#1-try-it) and [Tests](#tests) |

## Documents

- [The association's handbook](docs/operations.md): the tasks by frequency, with the commands.
- [When a merchant calls](docs/helpdesk.md): `manage.py diagnose_shop`, the calls and what to say.
- [One machine, from the sources](docs/single-machine.md): the simple installation, without a registry or a CI.
- [Advanced installation](docs/installation.md): images, CI and `deploy/install.sh`.
- [Going to mainnet](docs/mainnet.md): the decisions and the steps; moving shops from signet as the exception.
- [Backup and restore](docs/backup.md): the nightly encrypted copy, taking it off the machine, the full restore.
- [Choosing wallets for your community](docs/wallets.md): what a wallet must do, what we suggest, how to try one.
- [Privacy and data protection](docs/privacy.md): who is the controller, what the software stores and for how long, the GDPR checklist and the privacy page template; the disclaimer.
- [Configuration of the site](docs/configuration.md): the network, the site's name, who sees the site,
  [Behind another proxy](docs/configuration.md#behind-another-proxy), [IPv6](docs/configuration.md#ipv6),
  [`/admin/` and access log](docs/configuration.md#admin-and-access-log).
- [Releases, checks and backups](docs/releases.md): [Releases from images](docs/releases.md#releases-from-images),
  [Before opening the site](docs/releases.md#before-opening-the-site), [Before a release](docs/releases.md#before-a-release).
- [Features](docs/features/README.md): what the site does and how to turn each part on, Onion and Nostr included.
  The longer ones have a page of their own:
  - [The shop page](docs/features/shop-page.md#the-shop-page) and
    [The new shop form](docs/features/shop-page.md#the-new-shop-form);
  - [What customers see of a shop](docs/features/shop-details.md): the business name and the VAT number, and the owner's
    choice of what the page, the map and the lists show;
  - [The guardian of the Lightning addresses](docs/features/lightning-guardian.md);
  - [The customer at the till](docs/features/customer-path.md), with no wallet or no idea how to pay;
  - [Certified shops](docs/features/certified-shops.md);
  - [What to do today](docs/features/dashboard.md), the association's list in `/admin/today/`, sent every morning when there is something to do;
  - [The merchant's training, the «pass»](docs/features/merchant-training.md): modules the community writes, a quiz with no
    penalty, declarations kept as the written proof, badges, and the real till that opens with the basic pass;
  - [History and day closing](docs/features/end-of-day.md): the till's history, day closing and end of shift, worked
    out on the phone, with the CSV for the accountant and an encrypted copy to bring back on another phone;
  - [Copies on Nostr](docs/features/nostr-copies.md): the same copies on a relay, optional;
  - [Registers for the staff](docs/features/staff-registers.md): one per person, paired with a one-time code, turned off alone;
  - [On-chain at the register](docs/features/onchain.md), for special cases: experimental, off by default;
  - [The practice register](docs/features/practice.md): pretend sales, nothing real;
  - [Changing the Lightning address](docs/features/address-change.md): a new wallet, the same shop and sign;
  - [Blossom](docs/features/blossom.md): the site's files by their hash, read-only;
  - [Moving the shops to another site](docs/features/moving-shops.md);
  - [Closed for a while](docs/features/vacation.md): holidays, works, an illness, with no alarms and no fading icons;
  - [Meetups](docs/features/meetups.md): the board of the community's get-togethers, by the staff and from Nostr, with a calendar feed;
  - [Something wrong?](docs/features/help.md): the merchant sees what the site knows and what to do, and asks the
    association for help with one tap, with the diagnosis attached;
  - [Staff and shops kept apart](docs/features/staff-and-shops.md): what the people who run the site can and cannot do
    to a shop, and the register of what they change in an owner's account;
  - [OpenStreetMap](docs/features/openstreetmap.md): the shops linked to their entries on the free map (a link the
    association confirms), read once a month to fill what the shops have empty, with the tags to propose back, written
    only by a person, and the merchant's page «Your shop on OpenStreetMap»;
  - [The moderators](docs/operations.md#the-moderators): the volunteers' group in `/admin/`, with only the rights its
    work asks, and `manage.py moderators`;
  - [Public and private parts of a page](docs/features/private-pages.md);
  - [Team access with a Lightning wallet](docs/features/team-access.md), optional: the team sees a site under
    construction from any address;
  - [A training site in five minutes](docs/features/demo.md): `manage.py demo_data`, a made-up community in every state
    for the volunteers to learn on.
- [Turning features off](docs/features/README.md#turning-features-off): the badge, closed shops and the practice register
  can each be switched off in `community.yml`, with nothing deleted.
- [Experimental features](docs/features/README.md#experimental-features): on-chain at the register and writing to
  OpenStreetMap are off unless a community turns them on in `community.yml`.
- [Architecture](docs/architecture.md): software and community kept apart, the Nostr profile.
- [Limitations](docs/limitations.md): what bitmentor does not do, and why.

## Three ways to run it

| | For | Needs |
|---|---|---|
| [1. Try it](#1-try-it) | seeing the site and its features on your computer, in five minutes | Python 3.14 |
| [2. One machine](#2-one-machine) | a community that runs its own site on one server | a server with Docker, a domain name |
| [3. Advanced: images, CI and releases](docs/installation.md) | a community with several people, automatic tests and a release process | a container registry, a CI, the above |

Start from 1. Most communities never need more than 2.

### 1. Try it

No Docker, no domain: the site with the example community, SQLite, and made-up shops.

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

```bash
export SECRET_KEY=try DEBUG=1 && .venv/bin/python manage.py migrate && .venv/bin/python manage.py demo_data
```

```bash
.venv/bin/python manage.py runserver
```

Open `http://localhost:8000`. `.venv/bin/python manage.py demo_data --links` prints the sign-in links of the made-up owners: see
[A training site in five minutes](docs/features/demo.md). To try your own community, copy `examples/community` and
start with `COMMUNITY_DIR=<the copy>`. On Debian or Ubuntu `python3 -m venv` needs `apt install python3-venv`. The
Italian and German pages need `manage.py compilemessages` (from the `gettext` package); English works without.

coincurve 22.0.0, the first with Python 3.14 wheels, is not on PyPI yet. Until it is, on x86_64 Linux
`requirements.txt` downloads a wheel of upstream commit 2d11b11 from this repository's release
[deps/coincurve-2d11b11](https://github.com/mammadori/bitmentor/releases/tag/deps/coincurve-2d11b11), pinned by hash
in `requirements.lock`; `deploy/coincurve-wheel.sh` rebuilds it byte for byte
([more](docs/releases.md#coincurve-until-version-22-is-on-pypi)). Anywhere else (ARM, macOS) pip builds
that commit from source, in about a minute: it needs `git`, a C compiler, `cmake`, `make`, `pkg-config` and Python's
headers (on Debian or Ubuntu: `apt install git build-essential cmake pkg-config python3-dev`). The Docker image builds
it by itself where there is no wheel, and on x86_64 too with `--build-arg COINCURVE_FROM_SOURCE=1`.

The made-up shops have no real Lightning address: their registers cannot take real payments, but the practice
register works.

The same with Docker instead of Python, with a `../my-community` folder (copy `examples/community`):

```bash
docker build -t bitmentor-web .
docker run --rm -p 8000:8000 -e SECRET_KEY=test -e DEBUG=1 -e SQLITE_PATH=/tmp/db.sqlite3 \
  -e COMMUNITY_DIR=/community -v "$PWD/../my-community:/community:ro" bitmentor-web
```

### 2. One machine

The whole stack on one server, from this repository: the site, Postgres and Caddy with its certificates. No registry
and no CI: you update with `git pull` and one command. Step by step:
[One machine, from the sources](docs/single-machine.md).

## Community

The software contains no data of any community: see [docs/architecture.md](docs/architecture.md) and
`examples/community`, a complete community to copy and rewrite. A community's name, texts, theme, pages and
languages live in its own folder, given to the site with `COMMUNITY_DIR`.

## Tests

In the environment of [Try it](#1-try-it), compile the Italian and German texts first (`compilemessages` needs the
`gettext` package): the tests of those languages fail without them.

```bash
export COMMUNITY_DIR=examples/community SECRET_KEY=test && .venv/bin/python manage.py compilemessages
```

```bash
.venv/bin/python manage.py test
```

The register's JavaScript is tested with Node.js, and its layout with `chromium-headless-shell`: without them those
tests are skipped (`REQUIRE_NODE=1` and `REQUIRE_BROWSER=1` make a missing one a failure). `--parallel` makes the run
several times shorter.

`FORBIDDEN_NAMES`, a regular expression, turns on the check that no community names appear in the repository.

The documents have a check of their own, without Django: every relative link points to a file that exists, and every
`#anchor` to a heading of that file.

```bash
python3 deploy/tests/test_documents.py
```

[examples/ci/gitlab-ci.yml](examples/ci/gitlab-ci.yml) is an example CI that runs both and publishes the images on a tag.

## Credits

Written by Marco Amadori with Claude, Anthropic's AI model, as a coding assistant.

## License

GNU Affero General Public License v3 ([LICENSE](LICENSE)); MIT or other licenses on request to the authors ([COPYING.md](COPYING.md)); the few third-party files keep their own ([THIRD-PARTY.md](THIRD-PARTY.md)). Contributions: [CONTRIBUTING.md](CONTRIBUTING.md). The license's disclaimer of warranty and limitation of liability apply to the software and its documents; see [No warranty, no legal advice](#no-warranty-no-legal-advice).

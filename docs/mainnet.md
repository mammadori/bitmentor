# Going to mainnet

**The rule: mainnet is a new installation.** Install the site again with `NETWORK=mainnet` from the first start, on
[one machine](single-machine.md) or [from images](installation.md), in a folder with another name when it is on the
signet site's machine: the folder's name is the project's, and keeps the two databases apart
([Two installations on one machine](installation.md#two-installations-on-one-machine)). A site that ran on signet is never switched over:
its accounts, shops and Lightning addresses are test data. When the signet site already has real merchants, they can
bring their shops along: that is the one exception, [Moving the shops from signet](#moving-the-shops-from-signet).

The server runs no Bitcoin node and no payment server: the registers are paid straight into the merchants' own wallets,
through their Lightning addresses. So mainnet changes little on the machine. What changes is that the money is real.

> **Before going live with real payments.** The software is provided "as is", without warranty of any kind (GNU AGPLv3
> [license](../LICENSE)); its developers accept no liability for funds, data or compliance, and this is not legal, tax or
> financial advice. The community is responsible for its installation, for what its merchants and visitors are told, and
> for the laws that apply to it (data protection, consumer, tax and, in some countries, rules on crypto-asset services).
> Fill in the privacy page and the record of processing ([Privacy and data protection](privacy.md)) first.

## To decide before opening

The person responsible for the community decides these.

| # | What | Why | Proposal |
|---|---|---|---|
| 1 | **Who can change where the money goes** | Whoever holds the database, an administrator account or root on the machine can change a shop's Lightning address, so the page of the shop and a register paired afterwards would ask somebody else's wallet for the invoices. | Two people with root, nobody else; the `.env` readable only by root |
| 2 | **Email** | Without email nobody can get back into their account. You need an SMTP server on port 587 with STARTTLS (465 does not work: `EMAIL_USE_TLS` is fixed), a sender on the domain in `DEFAULT_FROM_EMAIL`, SPF, DKIM and DMARC in the DNS, and a delivery test to Gmail and to a provider common in your country. | A transactional email provider with DKIM on the community's domain |
| 3 | **Real test payments** | Every wallet the community suggests must be tried with real sats before a merchant relies on it. You need a few thousand sats, someone to pay, and a receiving wallet of each kind. | A paid, a cancelled and an expired sale for each wallet |
| 4 | **Backup destination** | The encrypted copy made every night goes off the machine: a host with rsync over ssh, two age keys of two different people. | 30 days of copies, then the first of each month for 12 months ([Backup and restore](backup.md)) |
| 5 | **External monitoring** | `check.sh` runs on the same machine: if the machine goes down, it goes down with it. You need a check from outside on `/health/` that alerts a person. | A monitoring service on another network |
| 6 | **`/admin/`** | It has no second factor: restrict it by address with `ADMIN_ALLOWED_IPS`. | The addresses of the administrators |
| 7 | **Privacy page and legal compliance** | The privacy page is a template with placeholders, and the controller must be named. See [Privacy and data protection](privacy.md). | The page filled in, the processors' agreements accepted, the record of processing written |
| 8 | **When to open** | Only the person who runs the machine sets `SITE_MODE=production`. | After the checks below and a tested restore |

## The steps

1. **Install** with `NETWORK=mainnet` and `SITE_MODE=development`: [one machine](single-machine.md), or
   `deploy/install.sh … --network mainnet` ([installation.md](installation.md)). `FAKE_EXPLORER` and `BITMENTOR_DEMO`
   stay empty: the site refuses them on mainnet. On-chain at the register and writing to OpenStreetMap are experimental
   and off: leave `experimental` out of `community.yml` for a first installation
   ([Experimental features](features/README.md#experimental-features)). Keep `HSTS_SECONDS=86400` for now.
2. **Email** in the `.env` (item 2): `EMAIL_HOST`, `EMAIL_PORT=587`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`,
   `DEFAULT_FROM_EMAIL`.
3. **The backup** (item 4): [Backup and restore](backup.md). The first copy must reach the destination and open.
4. **The timers**, on a machine from images: [The timers of a production machine](operations.md#the-timers-of-a-production-machine).
   On one machine, the cron lines of [Regular jobs](single-machine.md#regular-jobs).
5. **Preflight.** `HSTS_SECONDS=31536000` in the `.env`, restart `web` and `caddy`, then
   `manage.py preflight`. It checks `NETWORK=mainnet`, `DEBUG` off, `SECRET_KEY`, email (the sender, and that the server
   accepts the connection and the credentials), HSTS of at least one year and IPv6, and notes the Lightning addresses
   that do not work. It must print `All ready`. A year of HSTS is binding: from here on the domain and its subdomains
   stay on HTTPS.
6. **Real payments** (item 3), still in `development`: only the addresses in `DEV_ALLOWED_IPS` see the site. A test shop
   for each wallet: a paid sale, a cancelled one and an expired one, at the owner's register and at a staff register; the
   money in the wallet; the day closing and the copies on the phone. Then a sign-in email to Gmail and to a provider
   common in your country, which must arrive and not end up in spam. At the end, delete the test shops.
7. **Before opening**: a [restore](backup.md#full-restore), tested; the external monitoring (item 5); `/admin/`
   restricted (item 6); who keeps root and the administrator accounts (item 1).
8. **Opening**: `SITE_MODE=production`, when the person responsible decides so. On a machine from images,
   `bin/mode.sh /opt/<project> production`; on one machine, edit the `.env` and `docker compose up -d web caddy`. The
   site refuses `production` when it is not on mainnet.

The stack is the site, Postgres and Caddy (and Tor, if the community turns it on): 2 GB of memory and a few GB of disk
are enough.

## Moving the shops from signet

The exception to the rule. The new mainnet site runs next to the signet one; the merchants who signed up on signet get a
one-time link to take their shop on mainnet. `manage.py export_shops` on the old site, `manage.py import_moved_shops` on
the new one: the commands are in the handbook,
[Going to mainnet, and moving the shops](operations.md#going-to-mainnet-and-moving-the-shops), and every rule in
[Moving the shops to another site](features/moving-shops.md).

What does not move: the Lightning address (a signet wallet is worthless on mainnet: each merchant connects a mainnet
wallet, with the same checks as a new shop), the certificate, the accounts of the volunteers, the training. Sales were
never on the site: they stay on the merchants' phones.

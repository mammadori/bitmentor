# Privacy and data protection: a guide for whoever runs the site

This page is for the people who install bitmentor for a community. It says what the software stores and where, how
long, which outside services your installation talks to, and what you still have to do yourself. The text visitors read
is the community's own [privacy page](#the-privacy-page-visitors-read), which you fill in from the template in
`examples/community/pages/`.

> **Read this first.**
>
> - **This is information, not legal advice.** Nothing here, and nothing in the template page, is a legal opinion or
>   a guarantee that your installation complies with any law.
> - **The software is provided "as is", on a best-effort basis, without warranty of any kind** (see the
>   [license](../LICENSE): GNU AGPLv3, whose warranty and liability clauses (sections 15 and 16) apply to everything in this repository, the
>   documents included). The developers and contributors accept no liability for data, funds, availability or legal
>   compliance, and are not data controllers or processors of your site.
> - **bitmentor applies no law of its own.** It cannot know where it runs. It ships defaults that keep little and keep
>   it briefly, and every one of them can be changed or switched off: the IP masking and retention of the logs
>   (`LOG_IPV4_MASK`, `LOG_IPV6_MASK`, `ACCESS_LOG`, `ACCESS_LOG_KEEP`, `LOG_MAX_SIZE`, `LOG_MAX_FILE`), how long the
>   site keeps what it knows (`privacy:` in `community.yml`). The choice is yours.
> - **You are responsible** for your installation, its configuration, the services you connect to it and your
>   compliance with the law that applies to you. The defaults are a starting point that tries to keep little and to
>   keep it briefly; they are not a legal assessment.
> - **Check the law of your own country**, and ask a lawyer or your data protection authority if in doubt. The rules
>   below are those of the GDPR; other countries have their own (the UK, Switzerland, Brazil and others have similar
>   laws, with differences), and some EU states add rules of their own.

## Who is responsible

Under the GDPR the **controller** is whoever decides why and how personal data is processed. For a bitmentor site
that is **the community that runs it**: the association, company or group of volunteers that owns the domain, chooses
the configuration and answers to the visitors and merchants. It is not the developers of bitmentor, who do not host
your site, do not receive your data and cannot see it.

So the controller's name and contact details belong on the privacy page, and one person (or a small group) of the
community should be named as the contact for requests. If the community is not a legal entity, find out who is
personally responsible under your national law, and consider forming an association before you open the site to the
public.

## Does the GDPR apply to you?

The GDPR applies to controllers established in the EU/EEA, and also to sites outside it that are **aimed at people in
the EU/EEA** (for example in an EU language, with local shops on a map). A community in the EU/EEA, or serving people
there, should assume it applies. Outside the EU/EEA, check your national law: it may be similar, or different in
important ways (age of consent, breach notification, regulator, fines).

Two good habits are worth having in any country: collect only what the service needs, and delete it when it is no
longer needed. The software is built that way; the sections below show how far.

## What the software stores, where and for how long

"Retention" is what the software does by itself with the default settings. Anything marked **you decide** depends on
how you run the machine.

| Data | Why | Where | Retention |
|---|---|---|---|
| **Account of a merchant or visitor who signs in**: a public key made by their wallet for this site only (LNURL-auth), an optional recovery email, the account's sign-in name | To let the person in and to give them access to their shop | Database (`shops.Profile`, the Django user) | As long as the account exists. Deleted by the staff on request (see [Rights](#rights-of-the-people-concerned)). The software does not delete idle accounts: **you decide** a policy |
| **Shops**: name, description, address, position, phone, email, website, business name, VAT number, Lightning address, Nostr key, Telegram chat of the notices, opening and closure dates, the guardian's reading of the Lightning address | The shop page and map, the register, the notices to the owner | Database (`shops.Shop`) | As long as the shop is on the site. Phone, email, website, address, business name and VAT number are shown only if the owner ticks "show"; the owner can hide or remove the shop. The shop page is public by the owner's choice |
| **Requests for help** ("Something wrong?"): the shop, the symptom, the line the merchant wrote, a copy of what the site knew (Lightning address, last sign-in), the account that asked, who solved it | To help the merchant | Database (`shops.HelpRequest`); the line also goes to the moderators' Telegram chat (`TELEGRAM_MODERATION_CHAT`) | `privacy.help_requests_days` (default **365**, 30 to 3650) after the request was solved; then the personal part is emptied and the row stays for the counts ([how](features/help.md), [handbook](operations.md#forget-old-requests-for-help)). **Telegram copies are not reached**: turn on automatic deletion of messages in that chat. Backups keep older copies (see below) |
| **Email budget**: for each email sent, the time and a keyed hash of the recipient (never the address or the text) | To stay inside the mail provider's quota and cap what one address receives | Database (`shops.EmailSent`) | **62 days** (rows older than two months are dropped as new ones are written) |
| **Request counters** (limits per network address, account or email) | To stop abuse | Database (`shops.RequestCount`), keyed by a hash | Each counter lasts its window (minutes to a day) and expired ones are deleted as new ones are written |
| **Sign-in challenges** (LNURL-auth) | To sign in with a wallet | Database (`shops.LoginChallenge`) | Minutes: expired ones are deleted when a new one is made |
| **Team access** (only with `TEAM_ACCESS=on`, off by default): for each person of the team who sees the site under construction from any address, the name the staff typed, a role, the public key their wallet made for this site only (LNURL-auth) and three times: when the key was made, when it last gave a browser a pass, when it was revoked; the invites (name, role, a hash of the link, when it was made, when it expires and when it was used); the staff's actions on a key in the admin's log; the sign-in challenges | To let the team see a site under construction | Database (`team.TeamKey`, `team.TeamInvite`, `team.TeamChallenge`); the name also goes to the moderators' Telegram chat, or to the community's email, when the key is made and at every new browser | Keys: until the staff deletes them in `/admin/` (a revoked key stays, to keep that wallet out). Invites: 30 days after their last day, deleted when a new one is made. Challenges: minutes. No network address and no email are stored ([how](features/team-access.md)) |
| **On-chain address counter** (only if you turned on-chain on, an experimental feature that is off by default, and only for shops that take on-chain payments): per chain, a SHA-256 of the extended public key (never the key) and the next index, so that two registers never show the same address | So that the shop's registers share one count | Database (`shops.ChainCount`) | As long as the shop exists. It tells how many on-chain sales the shop started, never amounts, addresses or payments ([on-chain](features/onchain.md)) |
| **Account changes log**: what changed in an owner's account or shop (never the values), when, by whom, who was told, the shop's name | To see unwanted changes | Database (`shops.AccountChange`), read only | **Kept** (it is a security record that outlives the account). No automatic deletion: **you decide**, and the shop names in it are personal data if the shop is a sole trader's |
| **Training records** (if you use the training): which modules were passed, the declarations ticked | Proof that the merchant read the information | Database | The declarations outlive the account and the shop: the link to them is emptied, and the account's name and the shop's name stay in the row, as the proof. Nothing in `/admin/` deletes one; **you decide** a policy, and whoever has the database applies it |
| **Sessions and cookies** | Technical only (see [Cookies](#cookies-and-similar-technologies)) | Database (sessions) and the browser | Session cookie: Django's default (2 weeks); the admin device cookie 180 days |
| **Server logs** | Diagnosis and security | Docker logs of each container; optional Caddy access log | See [Server logs](#server-logs) |
| **Nightly backups** | Recovery | Encrypted files on the destination you choose | Up to about **2 years**: see [Backups](#backups) |
| **Nostr** (public shop profiles, encrypted register copies) | Optional features | Relays you list | **Cannot be erased**: see [Nostr and Blossom](#nostr-and-blossom) |

There is no analytics, no advertising and no profiling in the software, and no third-party script is loaded: the
Content-Security-Policy of the site allows only the sources listed in the next sections.

### What the site does not store

The site never sees a sale made at a register: the register asks the merchant's Lightning address directly from the
phone, and the sales stay on the phone. The one exception is in the table above: the on-chain address counter. The site holds no funds, no keys and no recovery words. Customers of the
merchants are not users of the site and have no account.

### Cookies and similar technologies

The site sets only technical cookies and does not use tracking: the session cookie after sign-in, the CSRF cookie of the
forms, `admin_device` (a signed cookie that lets a staff browser sign in again, 180 days) and a cookie that remembers a
staff member's choice to see the private parts of the pages. With team access on (`TEAM_ACCESS`), a team member's
browser also holds `team_pass` (the signed pass, 7 days) and, for the five minutes of a sign-in, `team_k1`. Cookies strictly necessary for a service the user asked for
do not need consent under the ePrivacy rules in most EU countries, but **check your own country's rules** before you
add anything (statistics, embedded videos, a chat widget). If you add them, you probably need a consent banner and a
longer privacy page.

### Server logs

What is logged, verified in `Caddyfile`, `config/gunicorn.py` and `docker-compose.yml`:

- **Caddy access log**: **off by default** (`ACCESS_LOG=off`). With `ACCESS_LOG=on` it is written to Caddy's volume
  (`/data/access/`): time, method, host, path, status, duration and size; of the visitor's address only the network
  (`/24` on IPv4, `/48` on IPv6 by default: `LOG_IPV4_MASK`, `LOG_IPV6_MASK`; 32 and 128 keep the whole address); no query string and no headers (so no cookies and no referer). Files of 1 MiB, at most
  10, old ones removed after **7 days** (`ACCESS_LOG_KEEP`, a Caddy duration such as `24h` or `720h`).
- **Caddy error log**: always on, to the container's output. It keeps the same masked network and removes from the query
  and headers what opens something (tokens, register keys, `next`, referer, `X-Forwarded-For`).
- **Gunicorn (the Django container)**: an access line for each request with time, method, path (no query), status and
  duration, to the container's output; **no address, no user agent, no referer**. Errors at ERROR level only.
- **Docker keeps container output** in `json-file` with `max-size: 20m` and `max-file: 5` (`LOG_MAX_SIZE`, `LOG_MAX_FILE`). This is a limit on **size, not on
  time**: up to 100 MB per service, overwritten only as it fills. On a quiet site the lines of months can stay there, so
  whatever is logged to the container output (the masked network addresses of Caddy's error log, the paths in gunicorn's
  lines, a visitor's address if you add it to a log format) stays for months, not days. If that is too long for you,
  rotate by time (a systemd timer that truncates the logs, or a logging driver with a time limit) or log less. Nothing in
  the software deletes them by date: **you decide**.
- **What is outside the repository**: the hosting provider's own logs, the shared entry proxy of
  [the installation guide](installation.md#the-shared-entry-point) if you use one (it sees the full address of the
  visitor: check what it logs), the machine's systemd journal, and the firewall.
- Without `EMAIL_HOST` (development only) sign-in links are written to the web log: never run production like that.

Under the GDPR an IP address is personal data. Truncated addresses and a seven-day access log are a deliberate
data-minimising choice; whether you can rely on a legitimate interest for the logs in your country is for you to
confirm.

### Backups

`deploy/backup.sh` makes a nightly copy of the database and of the folder's configuration files, encrypts it with `age`
to the public keys you set, and ships it off the machine. The machine keeps its copies for `BACKUP_LOCAL_DAYS` (14). The
destination decides how long to keep the rest: with `deploy/examples/keep-backups.sh` the defaults are **all copies of
the last 30 days (`DAYS`), the first of each month for 12 months (`MONTHS`)**, and always the latest three, so data can
remain in a copy up to about **two years** after it was written, and what was deleted from the site is still in the
older copies. A deletion request is satisfied on the live site at once, and in the backups when the copy ages out:
**say so on the privacy page**, and restore with care (after a restore from an old copy, delete again whoever asked to
be erased: keep a short list of the requests, kept only as long as needed). If you want shorter retention, set `DAYS` and
`MONTHS` in the environment of the pruning script. The copies are encrypted, but they contain emails, public keys and the
configuration; protect the keys and the destination ([Backup and restore](backup.md)). `backup.sh` at the root of the repository
(for a test installation) is **not** encrypted.

### Nostr and Blossom

- **Public Nostr events** (shop profiles, the Nostr names the site serves): published on relays that
  you and others control. A relay may copy and keep an event, and you cannot make other people's relays delete it. Treat
  them as **public and permanent**. Publish only what the owner agreed to, and tell them.
- **Encrypted register copies** (optional, `register.relays` in `community.yml`): the register in the merchant's
  browser stores an encrypted copy on the relays; the site never sees them. They are encrypted, but they cannot be
  erased from third-party relays, and metadata (the relay sees the connection, the time, the size) is visible. If you
  run the relay yourself you can delete from it; limits to ask of a relay are in [Nostr copies](features/nostr-copies.md).
  The merchant should know this before turning it on.
- **Blossom** (optional, `blossom: true`): serves read-only the community's own public theme files by hash. It stores
  nothing about visitors.

## Outside services contacted by the visitors' browsers

The site does not load code from other origins, but some features make the **visitor's browser connect directly to a
third party**, which then sees the visitor's IP address and the browser's details. The CSP allows exactly these:

- **OpenStreetMap tiles** (`tile.openstreetmap.org`): **every visitor who opens a map** (the shop map, the route page,
  the shop and shop-form pages) contacts the OpenStreetMap Foundation's servers. This is a transfer of the visitor's IP
  address to a third party that you do not control, without the visitor asking for a third party. It is a real
  privacy risk for a site whose users care about privacy, and a point that the GDPR authorities have looked at for
  maps and fonts. Options, from the simplest: (1) name it in the privacy page, as the template does; (2) load the map
  only after the visitor clicks ("show the map"), which needs a change of the templates; (3) serve the tiles yourself,
  through a **tile proxy/cache** on your own domain (the browser then contacts only you; mind the
  [tile usage policy](https://operations.osmfoundation.org/policies/tiles/) and allow the proxy in the CSP in
  `config/middleware.py`), or from a commercial tile provider with which you sign a DPA. Today the software
  offers option (1) only.
- **The register** (opened by a merchant, not a visitor): the merchant's phone asks directly their **Lightning address
  provider** for invoices, the **price sources** you list (`register.price_sources`: kraken, coinbase, bitstamp, mempool,
  coingecko, blockchain) for the exchange rate, the **Nostr relays** you and the merchant list and, only if you turned
  on-chain on (an experimental feature, off by default), the **explorer** (`register.explorers`, mempool.space by
  default) for on-chain payments. Those parties see the phone's IP address and
  what is asked. Choose them knowing this; the merchant's own wallet provider is the merchant's choice and
  responsibility.
- **Telegram**, if you set a bot: the site sends it notices to merchants' chats and to the moderators' chat. See below.
- **OpenStreetMap data** (if you turn on `osm`): the server reads the OpenStreetMap API once a month; no visitor data is
  involved.
- **Meetups**, if turned on: the server reads the relays you list.

## Legal bases

Under the GDPR you need one for each purpose. The ones below are a typical reading; confirm them for your case:

| Purpose | Usual basis (art. 6) |
|---|---|
| Accounts, shops, the register's service, notices to the owner | Contract (6.1.b), or the legitimate interest of a community in running its directory (6.1.f) |
| Publishing the shop on the map and the shop page | Consent (6.1.a) of the owner (they tick "show" and "listed") |
| Requests for help | Contract / legitimate interest in supporting the merchant |
| Email budget, rate limits, logs, security of the account | Legitimate interest (6.1.f) in the security of the service |
| Backups | Legitimate interest (6.1.f); the same purpose as the data they copy |
| Nostr events | Consent of the owner, with the warning that it cannot be undone |
| Legal duties (accounting, a request from an authority) | Legal obligation (6.1.c) |

If you rely on legitimate interest, write down the balancing test (a short note is enough). Children: the software does
not ask for an age; decide whether your community admits minors, and what your country requires (in the EU the digital
age of consent is between 13 and 16 depending on the state).

## Rights of the people concerned

Everybody has, in the EU, the rights of access, rectification, erasure, restriction, portability and objection (arts. 15
to 21), and the right to **complain to the supervisory authority** of their country (the list is on the website of the
European Data Protection Board, edpb.europa.eu). You answer within one month, extendable in difficult cases.

How the staff can do it today:

| Request | How |
|---|---|
| Access, portability | `/admin/` shows an account, its shops and requests for help; the staff reads them out or copies them into a file for the person. There is no self-service export |
| Rectification | The merchant edits the shop in their panel; the staff can edit in `/admin/` |
| Erasure of an account | In `/admin/auth/user/`: deleting the user deletes their shops, and with them the shops' requests for help. Also: empty the person's lines in `/admin/shops/helprequest/` if you cannot wait for the timer, delete the Telegram messages by hand, and note that backups and Nostr copies remain (see above). `AccountChange` keeps the names of the shops: edit them if the request requires it. The training declarations of the person stay, with the account's name and the shops' names (`shops_declaration`): if the request requires it they are deleted in the database, since `/admin/` only shows them |
| Erasure of help requests | `manage.py forget_old_requests --days 0 --force` forgets every solved request; use `--dry-run` first. For one person, edit it in `/admin/` |
| Objection to the shop being listed | The owner switches the shop off in their panel; the staff can suspend it |
| Withdrawal of consent | As above for each purpose; Nostr events cannot be recalled |

Keep a note of each request (date, what was asked, what you did) without more personal data than needed.

## What you need to arrange

For each outside party that handles personal data **on your behalf** (a *processor*), the GDPR asks for a written
**data processing agreement (art. 28)**, usually accepted online in the provider's console, and, if the processor is
outside the EU/EEA, a valid **transfer mechanism** (chapter V: an adequacy decision, the standard contractual clauses,
or another one). Check the provider's DPA, where the data is, and the sub-processors.

| Party | What it handles | What to do |
|---|---|---|
| **Hosting provider / server** | Everything on the machine | DPA; choose the region; know who has root (the handbook asks for as few people as possible) |
| **Mail provider** (SMTP) | Emails and recipients' addresses, sign-in links | DPA; sender domain with SPF, DKIM, DMARC; check where it processes the data |
| **Telegram** | Notices to merchants and the moderators' chat, with the merchant's line | Telegram is a controller of its own and is outside the EU; the merchant who gives their chat id chooses it; tell the moderators; turn on automatic deletion of messages. Consider whether you need this feature |
| **Backup destination** | The encrypted copies | DPA if it is a third party; you keep the keys; know the country |
| **Nostr relay** (if you run the community's relay with a third-party host) | Encrypted copies, public events | DPA with the host; the other relays are independent parties |
| **DNS / CDN / entry proxy** | The visitors' addresses | Check what it logs |
| **Tile provider** (if you change it) | Visitors' addresses | DPA |
| **Payment, wallet and Lightning address providers** of the merchants | Their own | Not yours: the merchant's relationship |

### The record of processing (art. 30)

Many small communities are exempt if occasional and low risk, but you handle accounts and a directory on a regular
basis, so keep a simple record anyway. A minimal checklist, which this page can answer in large part:

- [ ] Name and contact of the controller (and of the DPO, if you have to appoint one)
- [ ] Purposes of each processing (the table above)
- [ ] Categories of people (merchants, visitors who sign in, staff) and of data
- [ ] Recipients (the providers above, the relays) and transfers outside the EU/EEA, with the mechanism
- [ ] Retention (the table above and the backup rule)
- [ ] Security measures (encrypted backups, `ADMIN_ALLOWED_IPS`, few people with root, the logs above)
- [ ] The procedure for requests and for a data breach (72 hours to notify the authority when there is a risk to people)
- [ ] The balancing tests, if you use legitimate interest
- [ ] A data protection impact assessment is probably not needed; confirm for your case

## The privacy page visitors read

`examples/community/pages/en/privacy.md` (and `it`) is a complete **template** with placeholders in square brackets
(`[CONTROLLER NAME]`, `[CONTACT]`, `[COUNTRY / SUPERVISORY AUTHORITY]`, `[HOSTING PROVIDER]`, `[MAIL PROVIDER]`,
`[BACKUP DESTINATION]`, and so on). Copy it into your community's `pages/<language>/`, fill in every placeholder, remove
what does not apply to the features you turned off, translate it, and remove the template notice at the top.
Values it can take by itself from `community.yml` are written as `{{ community.email }}` and
`{{ community.privacy.help_requests_days|default:365 }}`. `manage.py preflight` prints a "good to know" note while a page
named `privacy` still has the placeholders.

## No warranty

The software is released under the GNU AGPLv3: it is provided "as is", without warranty of any kind, and its authors
and contributors are not liable for any claim or damage arising from it. That includes data protection fines, loss or
disclosure of data, loss of funds, downtime, and any wrong or outdated statement in these documents. The documents and
templates are written in good faith, as a best effort, and may be incomplete or out of date for your country. They are
not legal, tax or financial advice. Use them as a checklist, check the law that applies to you, and get professional
advice where the risk justifies it.

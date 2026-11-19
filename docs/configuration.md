# Configuration of the site

The settings in the stack's `.env` that decide how the site runs and who sees it, and the limits of `community.yml`. A
new installation starts from [One machine](single-machine.md) (or [the advanced installation](installation.md)); this
page explains each setting. [Every variable](#every-variable) is listed at the end.

## Turning features off

The badge of the certified shops, the shops closed for a while, the practice register, the wait for an approval of
new shops and the owner's panel by stages are on unless `community.yml` has a `features` block that says
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
value is the default (`manage.py check` says which is wrong), and `0` means no limit (except `staff_code_hours`, from 1 to 72). The code reads them all from
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
| `staff_code_hours` | 24 | Hours a [staff register](features/staff-registers.md)'s one-time pairing code works: from 1 to 72, and `0` is not allowed |

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
`EMAIL_HOST`. On mainnet, without it, sending fails: no link reaches the log, only the error.

The sign-in and confirmation links, and the notices about a wallet connected or taken away, leave after the page has
answered: the page does not wait for the mail server, and it answers the same whether the email leaves or not (with
more than eight emails on their way in one process, the next request sends its own and waits, as a brake). An email that does not leave is an error in the log of `web` (`email: a "link" email (…) could not be
sent: SMTPRecipientsRefused`, with the subject and the kind of error, never the recipient or the link): `manage.py preflight` checks the mail server. A `web`
that is stopped waits for the emails on their way; one that is killed loses them, and the person asks for a new link.

## A mail backend of the community

The site sends every email with one mail server (`EMAIL_HOST`). A community that wants more, for example one provider
for the automatic emails and another for those a person may answer, writes a Django mail backend of its own and names
it in `EMAIL_REAL_BACKEND`. No change to bitmentor is needed, and the next versions keep it working.

- Put the module in the `python/` folder of the community package (`COMMUNITY_DIR/python/`): the site can import it.
- Each message carries `message.kind`: `link` (sign-in and email confirmation links), `staff` (summaries and alerts to
  the staff), `merchant` (emails to a shop's owner) or empty. `message.from_email` is the sender chosen for it.
- The email budget still counts every message, whatever the backend: set `emails_per_month` to the smallest quota.

```python
# COMMUNITY_DIR/python/community_mail.py, with EMAIL_REAL_BACKEND=community_mail.Backend
import os

from django.core.mail import get_connection
from django.core.mail.backends.base import BaseEmailBackend


class Backend(BaseEmailBackend):
    def send_messages(self, messages):
        sent = 0
        for message in messages:
            if getattr(message, "kind", "") == "link":
                connection = get_connection("django.core.mail.backends.smtp.EmailBackend")
            else:
                connection = get_connection("django.core.mail.backends.smtp.EmailBackend",
                                            host=os.environ["OTHER_EMAIL_HOST"], port=465, use_tls=False,
                                            use_ssl=True, username=os.environ["OTHER_EMAIL_USER"],
                                            password=os.environ["OTHER_EMAIL_PASSWORD"])
            sent += connection.send_messages([message]) or 0
        return sent
```

The variables the module reads go in the `.env` and in an `environment:` of your own compose override.

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

`TEAM_AREA=on`, with `TEAM_ACCESS=on`, adds the volunteers' area: a team key with the role `moderator` signs in at
`/team/`, also while the site is in `production`, and sees there the day's list and the requests for help, which it can
mark solved. No account and no right in `/admin/`. Off by default; only `web` reads it. See
[The volunteers' area](features/team-access.md#the-volunteers-area).

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

`/admin/` asks for the password and, for a staff account that added one, a second factor: a passkey (made by the
phone, the computer or a security key) or a 6-digit code from an authenticator app on the phone (TOTP, any app that
reads an `otpauth://` QR code). With `ADMIN_ALLOWED_IPS`,
addresses or networks separated by spaces, for example `203.0.113.7 2001:db8:1::/48`, Caddy opens it only to those
and answers 403 to everyone else, onion included. Without the variable it stays open to everyone. Behind another
proxy, the visitor's real address is what counts, as long as the proxy is in `TRUSTED_PROXIES`. The sign-in limits
are 10 tries per address and 30 per /48 network every 15 minutes, passwords, codes, passkeys and wallets together, and, per
account, 20 wrong passwords every 15 minutes and, apart from them, 5 wrong codes every 15 minutes and 20 a day. The limits per
account also stop whoever tries from many addresses: whoever knows the password gets at most 20 guesses of the code in
a day of the counter, each with three chances in a million (the code of the half minute before and after is accepted
too). The windows are fixed, not sliding: the day starts at midnight UTC, so up to 40 guesses can fall within 24 hours
across a midnight. While they last, a browser that signed in to that account before (a signed cookie,
`admin_device`, kept 180 days, void once the password changes and, for an account with a second factor, valid only
if it was given by a sign-in with one of the second factors the account has now) can still sign in, with the per-address limits only; a new
browser has to wait the end of the window. From an address in `ADMIN_ALLOWED_IPS` nobody else can use up the limits.

**The second factor.** `ADMIN_SECOND_FACTOR` is a level, chosen by whoever installs the site. Three, from the
lightest:

- `optional` (the default): **to try the technology and the team first.** `/admin/` is as it always was: the password,
  and nothing in between, no banner, no message. A quiet **Second factor** link among the user tools at the top of
  `/admin/` (`/admin/second-factor/`) lets each staff account add one for itself, if it wants: passkeys (as many as it
  likes, each with a name), or a code from an app (the page shows a QR code and the key as text, once, and only a
  right code from the app turns it on), or both; either signs in. The same page renames and removes the passkeys and
  turns the code off with a code, the last one too: the account is then back to the password alone. Accounts with
  the password alone, with a code and with a passkey live side by side.
- `required`: a staff account without a second factor (a passkey or a code) is sent, right after the password, to
  add one, and reaches no other page of `/admin/` until it has (a temporary password is changed first). With it,
  `/admin/` open to every address has a password and a device behind it, and `ADMIN_ALLOWED_IPS` becomes a choice, no
  longer the only defence: staff on a phone or on a new network can sign in. Moderators sign in the same way.
- `passkey`: **for a community with real shops.** As `required`, but only passkeys sign in. An account that has a
  code from an app and no passkey yet gives its code once more after the password, and that code opens only the page
  where a passkey is added (and the sign-out, and the change of a temporary password): nothing else of `/admin/`, no
  known browser, and the code cannot be turned off from there. Once the account has a passkey the code is neither
  asked for nor accepted, and the sessions it opened end. An account with no second factor at all is sent to add a
  passkey right after the password (see «What it does not protect»). The page sets no new code up.

Moving up a level is a choice, never forced, and it can be undone: change the variable and restart. Any other value
stops the site from starting. In `required` and
`passkey` mode the page refuses to remove the last second factor the mode accepts, and says why: add another one
first.

**Sign-in with a wallet: the hobby level (`ADMIN_WALLET_SIGN_IN`).** For whoever tries the site with a few friends,
or a team learning the tools: nobody has to know what a code from an app or a passkey is. `off` by default, and then
none of it exists: no link, no section on the page, its addresses answer 404, `/admin/` is exactly as before. With
`ADMIN_WALLET_SIGN_IN=on` **and** `ADMIN_SECOND_FACTOR=optional`:

- Each staff account, signed in to `/admin/` **with its password**, links **its own** Lightning wallet on its
  **Second factor** page (section **Sign in with a wallet**): the page shows a QR code, the wallet signs it
  (LNURL-auth), the page shows the start and the end of the key that signed and the person confirms it with a name (if
  their own wallet reported an error, someone else's signed first: cancel). Up to 5 wallets per account, each renamed
  and removed there, also only from a session opened with the password; nobody sees or removes another account's
  wallets, a superuser neither.
- The login page has one more link, **Sign in with your wallet**: a QR code to scan with a linked wallet, and the
  browser is in `/admin/`, as after a right password. **One step, not two**: whoever holds the unlocked phone with that
  wallet, and one of the browsers below, has that account's `/admin/`. The password keeps working.
- **Only on a browser where the account already used its password, and only while that password stands.** A wallet
  signs whatever code it is shown: whoever opened the wallet's sign-in page in their own browser and got a staff member
  to scan its code would otherwise be in `/admin/` themselves. So the wallet opens `/admin/` only in a browser that
  carries the account's `admin_device` cookie, the one a right password leaves there (180 days, bound to the account's
  current password), and in no other: there the answer is the same as for every refusal, and the page says to sign in
  with the password the first time on a browser. **Never approve in the wallet a code you did not open yourself, on
  the site's own login page, in your own browser.** A change of password stops every wallet until the new password is
  used once in that browser; the wallets stay linked and work again there. A sign-in with the wallet does not renew the
  cookie: the password is used at least once in 180 days on each browser.
- Only for an account **without a second factor**: an account that added a code from an app or a passkey chose more
  than one step, and its wallet does not open its `/admin/` (the page says to sign in with the password). A wallet is
  an alternative to the password alone, never a way around a second factor, and never a second factor itself.
- A session opened with a wallet lasts `ADMIN_SESSION_HOURS` at most, and ends at its next page when that wallet is
  removed (not when another one is), when the account adds a second factor, or when the switch goes off or the level
  rises.
  It does less than a session opened with the password: it neither manages the wallets nor adds a second factor
  (the page says to sign in with the password), so whoever holds the unlocked phone for a moment cannot add a way in
  of their own or lock the account's owner out.
- **When the community gets real shops, move to `required` or `passkey`**: at those levels a wallet never opens
  `/admin/`, whatever `ADMIN_WALLET_SIGN_IN` says. The linked wallets stay stored and unused, the page says so,
  `manage.py check` warns that the two settings disagree (`shops.W016`, not an error: the site starts) and
  `manage.py preflight` notes it. Set `ADMIN_WALLET_SIGN_IN=off` then.
- **One key, one role.** A Lightning wallet gives this site the same key every time (LNURL-auth keys are per domain):
  the key of a shop's owner (its wallet sign-in) or of a team key cannot be linked to a staff account, and a key linked
  to a staff account opens no shop account, makes none, connects to none, and joins no team. **Use a different wallet,
  or a different account in your wallet, for a shop and for the staff.** Each side checks again after writing; only the
  key's own holder, signing on both sides at the same instant, could slip one role past the other.
- **Only at the site's own address** (`PUBLIC_URL`), as passkeys: the wallet signs for the address of the callback,
  always `PUBLIC_URL/lnurl-auth/staff/`. On the onion or another name of the machine the pages say so and show no
  code: sign in with the password there. The wallet's own call reaches the site like the shops' wallet sign-in does,
  outside `/admin/`, so `ADMIN_ALLOWED_IPS` still decides who reaches `/admin/` (the browser), not where the phone is;
  on a site under construction the phone must reach the site, as for the shops' wallet sign-in.
- **The binding.** The code is fresh, lasts five minutes and signs in once; it is kept in the browser's own session on
  the server, so a code signed for one browser signs in nobody else. The site writes nothing for a code until a wallet
  signs it: it knows its own codes by their HMAC, and counts the signatures of live codes by the wallet's address and
  network (20 and 60 in ten minutes). A refused wallet (not linked, of an account no longer staff or active, of an
  account with a second factor, in a browser that is not a known one of the account) gets one and the same answer,
  creates nothing, and counts against the sign-in's limits by address like a wrong password.
- Every sign-in with a wallet is one line in the account's history in the admin (which wallet's key is not written),
  and updates the account's last sign-in as the password does; nobody is told of an ordinary sign-in. Linking and
  removing a wallet and `manage.py admin_wallet reset <username>` are written there too and told to the people who
  look after the site; `manage.py admin_wallet list` says the switch, the level and which accounts have wallets.

**From `required` to `passkey`**, account by account: one with a passkey signs in with it, as before (its sessions
opened with a code end at their next page); one with only a code gives it once more, and adds a passkey on the page
that code opens; one with neither (possible only if it was made after the switch, or reset) adds a passkey right after
the password. Back to `required` or `optional`, the codes count again.

**What a passkey protects against, and when.** A code from an app can be phished: a fake page that looks like this
one asks for the password and the code and uses them at once, within the code's half minute. A passkey cannot: the
browser lets it sign only for the site it was made for, and the site keeps only its public key, nothing that signs in
if the database leaks. But while a code is also accepted (`optional`, `required`), the phisher simply asks for the
code instead: **the protection of a passkey is real only with `ADMIN_SECOND_FACTOR=passkey`**. In that mode the
reserve is a second passkey (and the reset from the machine), not a code from an app: the page urges at least two,
on two different devices.

- **Scope.** A passkey belongs to the site's host, `SITE_HOST`, and works only at `PUBLIC_URL`
  (`https://SITE_HOST`): a passkey made on a test site does not work on the real one, none works on another name of
  the machine or on a subdomain, and none works on the onion (there the page says so, and offers the code where the
  mode accepts one). The README's local try uses `localhost`, which browsers accept without HTTPS. Changing
  `SITE_HOST` makes every passkey useless: reset them (`manage.py admin_passkey reset <username>`) and add new ones.
- **Synced or not.** The page shows for each passkey whether it is synced (to a phone's or a password manager's
  account) or on that device only. A synced passkey is as safe as the account that syncs it: whoever takes over that
  account has it. For superusers, prefer a hardware security key, which keeps it on the device.
- **What is checked.** The site asks for no attestation and checks none, so any authenticator works (no allow-list
  of brands); it checks the browser's origin, the site's host, the challenge (five minutes, once), that the person
  was present, the signature, that the passkey is still as syncable as at registration (its BE flag; whether it is
  synced now is read again at each sign-in), and the authenticator's sign count, which must grow when it counts at
  all: when it does not, the sign-in is refused and the moderators are told that the passkey may have been copied.
  A challenge is used once: the session forgets it when it is taken, and its hash is written in the database (the
  request counters' table, for five minutes) so that two requests of the same session at the same moment cannot both
  use it; if the database cannot write it, the passkey is refused.
- **No sign-in without a username.** The password always comes first; the passkey is the second step.

**Who is told.** Turning the code on, turning it off and `admin_totp reset`, adding and removing a passkey and
`admin_passkey reset`, and a passkey whose count did not grow are written in the account's history in the admin and
told to the people who look after the site (the moderators' chat, or the community's email), for every staff account.
The first wrong code of a day after a right password is told too, once per account and day: the password is known,
maybe to someone else; whoever typed the code sees only that it is wrong. So is the first refused passkey of a day
after a right password, with a count of its own. A refused passkey answers the same whatever the reason, and counts
against the address limits like a wrong password; it has no budget of its own (it cannot be guessed).

**What it does not protect.** The first set-up trusts whoever has the password: with `required` or `passkey`,
whoever learns the password of an account that never added a second factor (neither a passkey nor a code) can add
their own app or passkey; with `optional`, they can add one and lock the real person out. An account that has a code
is not exposed this way in `passkey` mode: its first passkey needs the code too. Add it right after the account is made
([handbook](operations.md)), look at `manage.py admin_totp list` and `manage.py admin_passkey list` for the accounts
without one, and treat a second factor nobody expected as a password that is known: `manage.py admin_totp reset
<username>` or `manage.py admin_passkey reset <username>`, then a new password.

Whoever knows the password can also use up the day's wrong codes every day, and keep the real person out of every new
browser: a browser that signed in with the second factor before is not affected, and the notice of the wrong code
says it is happening. The way out is a new password, from that known browser or with `manage.py changepassword
<username>` on the machine: without it nobody reaches the code's page any more.

After a right password the account is not signed in yet: the browser
has five minutes to use a passkey or type the code, and only a right one opens `/admin/`. A code is accepted for 30 seconds either
side of its own half minute, and only once. The codes depend on the clock: keep the machine's clock right (NTP, on by
default on most systems). If the server's clock jumps ahead and a code is accepted then, no code works until the real
time catches up with that moment (a code is never accepted twice, nor one older than the last): `manage.py admin_totp
reset` is the way out. A refused code says that it is not right or already used, and to set the phone's clock to
the network's time if it keeps failing. A session opened with a second factor ends `ADMIN_SESSION_HOURS` (default
24, at least 1) after it was opened, whatever the activity: then password and second factor again; it ends earlier
when the factor it was opened with is removed or reset, or the mode no longer accepts it (removing one passkey ends
the sessions opened with it, not those opened with another). Sessions opened with the password alone, of accounts
without a second factor, last as before. A lost phone or key: remove its passkey from another one, or `manage.py
admin_passkey reset <username>` and `manage.py admin_totp reset <username>`
([handbook](operations.md#a-staff-member-lost-the-phone)). `manage.py preflight` notes, calmly and never as a problem,
when `/admin/` is open to every address with `ADMIN_SECOND_FACTOR=optional`, and how many staff accounts have no
second factor the mode accepts; on a local try (`DEBUG`, or `SITE_HOST` unset or `localhost`) it says nothing of it.

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

## The settings watch

When the site starts with a security setting different from the previous start, the moderators get one message, in
the site's language: which setting, the value before and the value now, the time of the start, and «if nobody of the
team did this, look at the machine's `.env`». It is there for the mistake and the unannounced change: a level lowered
«for a moment» and forgotten, a deploy that starts with an old `.env`. **It is no defence against whoever controls the
machine**, who can turn it off or change the database. Someone who edits the `.env` can also redirect or suppress every
message, this one included: changing the moderators' chat and the bot token together, for one. The watch catches
mistakes and changes a colleague did not announce, nothing more.

What is watched (the list is `WATCHED` in `website/settings_watch.py`, one line per setting):

- `/admin/` and the sign-ins: `ADMIN_SECOND_FACTOR`, `ADMIN_ALLOWED_IPS`, `ADMIN_SESSION_HOURS`, `ADMIN_WALLET_SIGN_IN`, `TEAM_ACCESS`,
  `TEAM_AREA`, `SITE_MODE`, `DEV_ALLOWED_IPS`;
- Caddy's `REFUSE_PROBES` and `TRUSTED_PROXIES` (the compose file passes them to `web` too, only for this);
- where the moderators' messages go: `TELEGRAM_MODERATION_CHAT` and the moderators' email addresses
  (`moderators_emails`, else the community's `email`). A change of these is told to the old recipients too, and the
  addresses get it by email, old and new. The message names no address and no chat: it says how many were removed
  and how many added («removed: 2, added: 1»), so that a new address written wrong learns nothing of the others;
- the names and links of the site: `ALLOWED_HOSTS`, `PUBLIC_URL`, `SITE_SCHEME`, `ONION_LOCATION`, `HSTS_SECONDS`,
  `DEBUG`;
- who carries the sign-in links and the messages: `EMAIL_HOST`, `EMAIL_REAL_BACKEND`, `TELEGRAM_API`. An address
  with a user and a password in it (`https://user:password@proxy.example/…`) is stored and shown without them; they are
  kept only as a keyed fingerprint, so a change of them alone is told as «the credentials in the address changed»;
- the addresses in `ADMIN_ALLOWED_IPS` and `DEV_ALLOWED_IPS` are never in the message, which every moderator reads:
  they are the team's homes and offices. It says how many were removed and added, or «now empty». On the machine,
  `manage.py settings_watch show` prints them, as the `.env` does;
- secrets: `SECRET_KEY`, `TELEGRAM_BOT_TOKEN`, `NOSTR_SECRET_KEY`. Their values are never stored, written or shown:
  only a fingerprint keyed with a key derived from `SECRET_KEY` (HMAC-SHA256), and the message says only «changed»,
  «set» or «removed». After a change of `SECRET_KEY` the other secrets cannot be compared once: the message says so.

Marked as a lowering, first in the list: `ADMIN_SECOND_FACTOR` going down (`passkey` → `required` → `optional`),
`ADMIN_ALLOWED_IPS` emptied, `ADMIN_SESSION_HOURS` raised, `ADMIN_WALLET_SIGN_IN`, `TEAM_ACCESS` or `TEAM_AREA` turned
on, `REFUSE_PROBES` turned off, `TRUSTED_PROXIES` changed to another value that is not empty (it decides whose word on
the visitor's address is believed), and `DEBUG` turned on.

The first start records without a message, and so does a setting a new release adds to the list; one taken off the list
is forgotten. Without a moderators' channel (Telegram, or email that leaves the machine) the message goes only to the
old recipients of a change of the chat or of the addresses, when there are some and they can still be reached; with
nobody to tell, the change is recorded without a message. The comparison runs once per start of `web`, beside
gunicorn (`when_ready` in `config/gunicorn.py`): the start waits for nothing, and two containers that start together
send one message. Whatever goes wrong is a warning in the log of `web`, and the site starts all the same; a message that
did not leave is sent at the next start. `manage.py runserver` and the other commands compare nothing.

On by default, since it does nothing without a channel. To turn it off, in `community.yml`:

```yaml
settings_watch:
  enabled: false
```

Turned off, the next start tells the moderators once that the watch was turned off, then deletes what was recorded,
and from then on nothing is compared (if that message cannot leave, the record stays and the next start tries again;
with nobody to tell, it is deleted at once). `manage.py settings_watch show` prints what is recorded (the secrets only
as «recorded»), `manage.py settings_watch forget` clears it without a message, and `manage.py preflight` says whether
the watch is on and when it last told a change: [the handbook](operations.md#a-setting-changed).

## Logs of the containers

Each container writes its lines to its output, and Docker keeps them. Where is a choice of the installation:

- **In Docker's own files** (`json-file`), what `docker-compose.yml` says: at most `LOG_MAX_SIZE` for each file and
  `LOG_MAX_FILE` files for each container. They belong to the container: **a release makes new containers, and the
  lines of the old ones go with them**, so after a release that went wrong the lines from before it are gone.
- **In the machine's systemd journal** (`journald`): the lines stay after a release, next to those of the timers
  (`bitmentor-release@`, `bitmentor-backup@`…), and `journalctl` reads them by time. `deploy/install.sh` chooses this
  for a new installation on a machine that has the journal and a Docker that can write to it (`--logs journal`;
  `--logs files` keeps Docker's files). It writes the choice in the installation's `compose.locale.yml`.

Reading them, with the journal:

```bash
journalctl CONTAINER_NAME=<project>-web-1 --since "1 hour ago"
journalctl CONTAINER_NAME=<project>-caddy-1 -f
```

`bin/compose.sh logs web` works with both. Reading the journal needs root, or a user in the `systemd-journal` group.

**An installation that exists** changes nothing by itself: its `compose.locale.yml` stays as it is. To move it to the
journal, add to that file, under `services:` (in place of `services: {}`), the lines marked `@journal` in
`deploy/examples/compose.locale.yml`, with the `caddy:` line marked `@caddy` when the file has no `caddy:` yet: one
`logging:` under each service. Then `bin/compose.sh up -d`: the containers are
made again, and the lines Docker kept for the old ones go with them, this once. From the sources
([One machine](single-machine.md)) the same lines go in a file of yours named in `COMPOSE_FILE`, after
`docker-compose.yml`. Docker Desktop and machines without systemd have no journal: keep Docker's files there.

**How long the journal keeps them is the machine's choice**, not the site's, and with the journal `LOG_MAX_SIZE` and
`LOG_MAX_FILE` do nothing. Set it in `/etc/systemd/journald.conf` (then `systemctl restart systemd-journald`): it
holds for the whole journal of the machine, not only for the site.

```
[Journal]
MaxRetentionSec=1month
SystemMaxUse=1G
```

`MaxRetentionSec` is a limit in time, which Docker's files do not have: see what the lines hold in
[Privacy](privacy.md#server-logs) before choosing it. The journal also drops lines when one service writes too many
in a short time (`RateLimitIntervalSec`, `RateLimitBurst`), and it counts all the containers together, as Docker's
own: on a busy site look for "Suppressed … messages" in the journal and raise the burst if you find it.

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
| `TEAM_AREA` | `off` | `on`, with `TEAM_ACCESS=on`: a team key with the role moderator signs in at `/team/`, in `development` and `production`, and sees the day's list and the requests for help there: [The volunteers' area](features/team-access.md#the-volunteers-area) |
| `OPEN_PATHS` | empty | Paths open in every mode, for example `/extra /extra/*` |
| `MAINTENANCE_DIR` | the software's page | Folder with the under-construction page (`index.html`) |
| `ADMIN_ALLOWED_IPS` | empty: from anywhere | Who reaches `/admin/`: [`/admin/` and access log](#admin-and-access-log). Caddy applies it; `web` gets it too, only for `manage.py preflight` to say when it is empty with team access on or with `ADMIN_SECOND_FACTOR=optional` |
| `ADMIN_SECOND_FACTOR` | `optional` | The second factor of `/admin/`: `optional`, each staff account adds a passkey or a code from an app for itself; `required`, every staff account must add one at its next sign-in; `passkey`, the same and only passkeys count, the mode that resists phishing: [`/admin/` and access log](#admin-and-access-log) |
| `ADMIN_SESSION_HOURS` | `24` | Hours a session of `/admin/` opened with a second factor lasts, whatever the activity; at least 1 |
| `ADMIN_WALLET_SIGN_IN` | `off` | `on`, with `ADMIN_SECOND_FACTOR=optional` only: a staff account without a second factor may link its own Lightning wallet and sign in to `/admin/` with it instead of the password, one step: the hobby level, to try the site with a few friends. Any other value stops the start: [Sign-in with a wallet](#admin-and-access-log) |
| `SOURCE_URL` | `https://github.com/mammadori/bitmentor` | The link to the source code on every page (the footer of the site and of the admin, the register's menu, the tools of the printable sheets), as section 13 of the AGPLv3 asks: point it to your version if you changed the code |
| `REFUSE_PROBES` | `on` | Caddy answers 404 by itself to the requests of robots that try every site for what this one does not have (WordPress, PHP, a forgotten `.env` or `.git`, phpMyAdmin…): they never reach Django nor the web log. No address or file of the site has these shapes, and `/static/`, `/theme/` and `/.well-known/` are left alone. Not a defence, only less noise. `off` sends them on to the site, as before; any other value stops Caddy |
| `ACCESS_LOG` | `off` | `on`: Caddy's access log, addresses reduced to their network, 7 days |
| `ACCESS_LOG_KEEP` | `168h` | How long Caddy keeps the access log (a Caddy duration) |
| `ACCESS_LOG_SIZE`, `ACCESS_LOG_FILES` | `1MiB`, `10` | The size of each file of the access log and how many old ones stay. The log ends at whichever limit comes first: with the defaults a site that writes more than about 10 MiB in `ACCESS_LOG_KEEP` keeps fewer days than that, and raises these (for example `10MiB` and `20`) |
| `LOG_IPV4_MASK`, `LOG_IPV6_MASK` | `24`, `48` | How much of a visitor's address Caddy's logs keep: the prefix length; `32` and `128` keep the whole address |
| `LOG_MAX_SIZE`, `LOG_MAX_FILE` | `20m`, `5` | Docker's limit on each container's output, per file and number of files. They do nothing when the containers write to the journal: [Logs of the containers](#logs-of-the-containers) |
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
| `DEFAULT_FROM_EMAIL` | `noreply@localhost` | The sender, on the community's domain: sign-in and confirmation links, summaries and alerts to the staff |
| `MERCHANT_FROM_EMAIL` | empty (= `DEFAULT_FROM_EMAIL`) | The sender of the emails to a shop's owner, where a reply makes sense: Lightning alerts, a change of where the shop is paid, back from holidays. Same domain, allowed by the same SMTP account |
| `EMAIL_ENVELOPE_FROM` | empty (= the sender) | The envelope sender (Return-Path), for a mail server that accepts only its own account there while `From` shows another address. Both senders can carry a name: `Name <address>` |
| `EMAIL_REAL_BACKEND` | empty (SMTP with `EMAIL_HOST`) | A mail backend of the community, as a Python path ([A mail backend of the community](#a-mail-backend-of-the-community)) |
| `BLOSSOM_HOSTS` | empty | The hosts that are only the read-only Blossom server, at their root, separated by commas; the first is the main one, the others the same server under another name ([Blossom](features/blossom.md)) |
| `TELEGRAM_BOT_TOKEN` | empty | The bot that writes the site's messages |
| `TELEGRAM_MODERATION_CHAT` | empty | The moderators' chat: a chat number or an `@name`. Alerts about Lightning addresses and requests for help, and the morning summary. Several chats, separated by commas, receive the same messages: for example the private chat of each moderator with the bot (the number of the person, who must have written to the bot once), instead of a group. A message counts as delivered when at least one chat took it; the chats that did not are in the `web` log by their place in the list. When no chat took it, only the morning summary, the new shops, team access, OpenStreetMap and Blossom go by email instead: to the community's `email`, or to each address of `moderators_emails` (up to five, one message each: see examples/community/community.yml). Alerts about Lightning addresses and requests for help stay only in `/admin/`. A moderator's chat cannot also be the chat of a shop |
| `TELEGRAM_API` | `https://api.telegram.org` | Another address for Telegram's API (a test server) |
| `NOSTR_SECRET_KEY`, `NOSTR_RELAYS` | empty | The community's Nostr key and relays (separated by commas), for `manage.py publish_nostr` |
| `OSM_TOKEN` | empty | For `manage.py osm_upload`, an experimental command that is off by default. Passed only by hand, never in the `.env`: [OpenStreetMap](features/openstreetmap.md) |
| `SHOW_VERSION` | `1` | `0`: the footer does not show the version in use (`/health/` still does) |
| `BITMENTOR_DEMO` | empty | `1`: a training site in maintenance mode ([demo](features/demo.md)) |
| `FAKE_EXPLORER` | empty | `1`: the fake on-chain explorer, on a signet test site only, with on-chain turned on |

Not in the `.env`: `SITE_VERSION`, a version of the community's own that `/health/` shows next to the software's, goes
in the community's image (`ENV SITE_VERSION=…`). The footer of every page shows the version in use: `SITE_VERSION`,
otherwise the software's; without a release version the git commit (`-dirty` with uncommitted changes), or
`no-version`. A community with its own footer includes `partials/version.html` or uses `{{ version_in_use }}`.

Also not in the `.env`: `POSTGRES_HOST`, `POSTGRES_USER`, `POSTGRES_DB`, `SQLITE_PATH` and
`DEBUG` are for development and tests outside Docker Compose.

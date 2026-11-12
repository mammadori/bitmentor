# Features

What bitmentor does, and how to turn each part on. The longer explanations are in the pages linked from the table.

Three of the features below can be turned off by a community, one by one: see [Turning features off](#turning-features-off).
Two are experimental, and off unless a community turns them on: see [Experimental features](#experimental-features).

| Feature | How to turn it on |
|---|---|
| Sign-in with LNURL-auth, or a link by email | Always on; email needs `EMAIL_HOST` |
| The register in the browser: it asks the shop's Lightning address for each invoice (LNURL-pay) and checks the payment with LUD-21 `verify`; the sales stay on the phone. One screen like a phone app: the amount, the keypad and one round menu button (history and day closing, settings, practice and training, help, leaving the panel). The page loads only what the keypad needs; the QR code, the Nostr copies and (where it is turned on) on-chain load at their first use, and the copy for when the site does not answer keeps them all | Always on; `register` in `community.yml` sets the price sources, the `tolerance` and `max_rate_age`. Wallets: [Choosing wallets](../wallets.md) |
| Registers for the staff, one per person, paired from the owner's phone with a one-time code | Always on: see [Registers for the staff](staff-registers.md) |
| The practice register: four exercises with a pretend customer (five with on-chain turned on), nothing real | Always on: see [The practice register](practice.md) |
| On-chain at the register, for special cases, set up on the owner's phone | **Experimental, off by default.** `experimental: {onchain: true}` in `community.yml`, then the register's settings; `register.explorers`: see [On-chain at the register](onchain.md) |
| Close the day: history, day closing, CSV and encrypted copy, all on the phone | Always on: see [History and day closing](end-of-day.md) |
| Copies on Nostr: each register keeps an encrypted copy on the community's relays | `register.relays` in `community.yml`: see [Copies on Nostr](nostr-copies.md) |
| The owner changes the shop's Lightning address: same shop, same sign | Always on; `LIGHTNING_CHANGE_DAYS`: see [Changing a shop's Lightning address](address-change.md) |
| The guardian probes each shop's Lightning address and tells the merchant when it stops working | `manage.py check_lightning` with its timer: see [The guardian](lightning-guardian.md) |
| Certified shops: a working address and an owner with the basic pass | The same timer: see [Certified shops](certified-shops.md) |
| Map, search, discounts | Always on |
| The shop page behind the sign's QR code | Always on; `shop_page` in `community.yml`: see [The shop page](shop-page.md#the-shop-page) |
| The customer at the till with no wallet: three screens and plain answers | Always on; `shop_page.wallets`: see [The customer at the till](customer-path.md) |
| What customers see of a shop: the owner chooses what is shown | Always on: see [What customers see of a shop](shop-details.md) |
| Closed for a while: holidays or works said kindly, with no alarms | Always on: see [Closed for a while](vacation.md) |
| Historic shop: stays on the map with its own marker | `closed_on` in `/admin/` or in the `import_shops` JSON |
| Suspension from maps and lists, never blocking the register | `/admin/` |
| The merchant's training, the «pass» | `training` in `community.yml`: see [The merchant's training](merchant-training.md) |
| The merchant's path in the panel, from a working register to a certified shop | Always on (`shops/journey.py`) |
| Something wrong?: the merchant asks the association for help with one tap | Always on: see [Something wrong?](help.md) |
| What to do today, the association's list | `/admin/today/`: see [What to do today](dashboard.md) |
| Diagnosis of a shop for whoever answers a merchant's call | `manage.py diagnose_shop`: see [helpdesk.md](../helpdesk.md) |
| Staff and shops kept apart: the staff cannot pass for a merchant | Always on: see [Staff and shops kept apart](staff-and-shops.md) |
| The moderators' group, with only the rights its work needs | `manage.py moderators add <name>`: see [The moderators](../operations.md#the-moderators) |
| Meetups of the community, with a calendar feed | `meetups` in `community.yml`: see [Meetups](meetups.md) |
| OpenStreetMap: shops linked to the free map, read once a month, with the tags it should have for a person to change by hand | `osm` in `community.yml`: see [OpenStreetMap](openstreetmap.md) |
| Writing to OpenStreetMap: `manage.py osm_upload`, run by a person, sends those tags | **Experimental, off by default.** `experimental: {osm_upload: true}` in `community.yml`: see [Automatic edits](openstreetmap.md#automatic-edits) |
| Public and private parts of a page | `::: private` blocks: see [Public and private parts of a page](private-pages.md) |
| Team access to a site under construction: a person whose address changes signs with a Lightning wallet, and that browser counts as an allowed address | **Optional, off by default.** `TEAM_ACCESS=on` in `.env`, then invites in `/admin/`: see [Team access with a Lightning wallet](team-access.md) |
| Read-only Blossom server for the theme's files | `blossom: true`: see [Blossom](blossom.md) |
| Shops signed on Nostr, and NIP-05 names | `NOSTR_SECRET_KEY`, `NOSTR_RELAYS`, `manage.py publish_nostr` |
| Onion address | `docker compose --profile tor up -d`; `ONION_LOCATION` in `.env` |
| Telegram notifications | `TELEGRAM_BOT_TOKEN` |
| Donations with QR | `donations` in `community.yml` |
| Previews for shared links | `share_image` in `community.yml` |
| Route | `route` in `community.yml` |
| Import of existing shops | `manage.py import_shops file.json` |
| Moving shops to another bitmentor site | `manage.py export_shops`, `import_moved_shops`: see [Moving the shops](moving-shops.md) |
| A training site in one command, with made-up shops in every state | `manage.py demo_data`: see [A training site in five minutes](demo.md) |

## Turning features off

Three parts of the site are optional. A community that writes nothing keeps them all, and sees no change. To turn one
off, write `false` in a `features` block of `community.yml` (the block, commented, is in
`examples/community/community.yml`):

```yaml
features:
  certified: false
  vacation: false
  practice: false
```

A feature that is off goes quiet everywhere: it is not in the merchant's panel, in the public pages, in the maps, in
the messages (Telegram, email, the morning summary), and its addresses answer 404 to everybody, signed in or not. Its
commands end cleanly (exit code 0) and say it is off, and their timers do nothing. **Nothing is deleted:** the data stay
in the database, and turned on again (remove the line, or write `true`, then restart: `docker compose restart web` when
you run from the sources, a new image of the community on a release machine, since the file is read once when the
site starts) everything is back as it was. A name that is not a feature, or a value that is not `true` or `false`, does nothing (the feature stays
on): `manage.py check` warns (`shops.W003`), and `manage.py preflight` lists the features that are off.

| Switch | What goes away | What stays |
|---|---|---|
| `certified` | The "Certified shop" badge and its sentence on the shop page, the list and the map; the `certified` field of `/shops.json` (absent, not `false`); the private part of the pages for the owners of certified shops (`::: private` blocks, [Public and private parts of a page](private-pages.md)); the certificate's milestone in the merchant's path; the "Certified" column, filter and the revoke and allow actions in `/admin/`; the certificate lines of the diagnosis | The icons of how recently the Lightning address answered (they are not the badge), and the certificates already given or revoked: `check_lightning` keeps working them out, in silence, so they are all there when it is turned on |
| `vacation` | The "Closing for a while?" and "Welcome back" cards in the panel; the four addresses `/shops/break/…` (404); "Closed for holidays · back on…" on the shop page, the list and the map card; `break` in `/shops.json` (absent) and the `break` tag of the Nostr event; the welcome back message of `check_lightning`; the "closed for a while" count and the "closed for over three months" line of the dashboard; the closure lines of the diagnosis. The icons and the path stop forgiving the days a shop was closed | The closures written, and the Closures table in `/admin/`, so that the association can shorten or remove one. One that has a day to open again and is running keeps the dashboard and the guardian quiet until that day, and at most 92 days from its first (a shop away on holiday raises no false alarm, a fault never stays quiet for months); if the Lightning address is broken meanwhile, the dashboard has one line to know (`closed_lightning`) and the association is told the day the closure ends, without the welcome back to the merchant. One with no day ("until I say so") counts for nothing at once, because nobody could end it. A shop that sells while closed has its alarms as always. The register is never touched. `manage.py preflight` counts the closures running |
| `practice` | The "Practise without money" buttons and links (panel, the owner's and the staff's register, the first day); `/shops/practice/…` and `/shops/register/p/…/practice/` (404); the practice lines of the diagnosis | The practice register writes nothing: there is nothing to keep |

## Experimental features

Two parts of the site are experimental. They are off, and a community that writes nothing never meets them. To turn one
on, write `true` in an `experimental` block of `community.yml` (the block, commented, is in
`examples/community/community.yml`), then restart as for a feature turned off:

```yaml
experimental:
  onchain: true
  osm_upload: true
```

Experimental means this, here: the feature works and has its tests, but it has had less review and less real use than
the rest, it may change or go in a later version, and it is not what a community should start with. Turn one on when you
need it, after reading its page.

Off, an experimental feature is as quiet as a feature turned off: nothing in the panels, the public pages, the maps and
the messages, its addresses answer 404, its commands end cleanly (exit code 0) and say it is off. **Nothing is
deleted**, on the site or on the merchants' phones: turned on, everything is back as it was, with two things to know for
on-chain. A sale that was waiting for an on-chain payment when it went off is no longer looked at: the history says at
once to check it in the wallet, and a cancelled one gets no late look. And a staff register paired while it was off
carries no on-chain: it needs a new code afterwards. Only a YAML `true` (not a text in quotes) turns one on: a name that
is not an experimental feature, or a value that is not true or false, does nothing (the feature stays off):
`manage.py check` warns (`shops.W011`), and `manage.py preflight` lists the experimental features that are on.

| Switch | What it turns on | While it is off |
|---|---|---|
| `onchain` | [On-chain at the register](onchain.md): the on-chain part of the register's settings, "OK, on-chain" under the keypad, the count of the addresses on the site (`ChainCount`) and the owner's own explorer, the explorers in the register's content security policy, the on-chain key in a staff register's pairing code, the tick "I take on-chain payments too" in the new shop form with its sign on the shop page, the map, `/shops.json` and the tags for OpenStreetMap, the on-chain exercise of the practice register, the tick and the own explorer in a shop's form in `/admin/`, and the fake explorer of test sites (`FAKE_EXPLORER`) | The register takes Lightning only. A phone that had on-chain set up keeps its key, its count and its history, and offers no on-chain sale; the on-chain sales already made stay in the history and in the CSV, and one left waiting ends as "to check in the wallet", because no explorer is asked. The count on the site and the ticks of the shops stay in the database |
| `osm_upload` | `manage.py osm_upload`, which sends to OpenStreetMap the tags the monthly reading prepared ([Automatic edits](openstreetmap.md#automatic-edits)), and the lines of `/admin/today/` and of the monthly message that tell the staff to run it | [OpenStreetMap](openstreetmap.md) is still read, and the report still lists the tags OpenStreetMap should have: for a person to change by hand on openstreetmap.org. The merchants' requests are done by hand in the editor. The command says it is off and sends nothing |

In `community.yml`, `timezone` (for dates, UTC otherwise) and `map.featured_city` (limits the featured shops to one
city) are optional.

The look and the links, all in `community.yml` (see `examples/community/community.yml`):

- `theme_color` and `theme_color_dark`: the browser's bar colour, in light and dark mode. Without the dark one, the
  site stays light.
- `menu` and `footer_menu`: the pages in the top menu and in the footer, each a `slug`, an optional `path` and a label
  for each language.
- `links`: the community's profiles (a `label` and a `url`), shown in the footer and given to search engines as
  `sameAs`.

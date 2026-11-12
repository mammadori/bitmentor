# Copies on Nostr

An optional second place for the register's copies, besides the encrypted file ([History and day closing](end-of-day.md)).

In short, for organisers:

- The community lists one or more Nostr relays in `community.yml`. Without them, the merchants copy their history with
  the encrypted file, and nothing else changes.
- The owner writes their npub in the register's settings. From then on the register keeps an encrypted copy of its
  history on the relays, and the staff registers send their sales to the owner as they happen.
- Everything is encrypted to the owner (NIP-44). The relay sees only encrypted events; the site sees nothing at all.
- Choose a relay that keeps events for months, and that the community trusts to stay up
  ([what it must enforce](#what-the-communitys-relay-must-enforce)).

## Turning it on

1. The community lists its relays in `community.yml`:

   ```yaml
   register:
     relays: [wss://relay.example.org]          # always used; without it, no copy on Nostr
                                                 # /relay alone means wss://SITE_HOST/relay: the community
                                                 # must run that relay itself; bitmentor does not ship one
     public_relays: [wss://nos.lol]             # public relays an owner may add on the phone
   ```

   Each value is a `wss://` address with a domain name and a plain path, no port, query or fragment; anything else is
   left out and `manage.py check` says so (`shops.W007`). At most 8 of each.
2. The owner writes their **npub** in the register's settings, on the phone, and ticks the public relays to add.
3. The staff registers paired **after** that get it in their pairing code (version 2, see
   [On-chain at the register](onchain-notes.md#staff-registers-the-pairing-code)): the owner's public key, a private key the
   owner's phone makes for that register, and the public relays the owner chose. The owner's phone keeps, for each such
   code (the last 10 per register), that register's public key and the **conversation key** it shares with the owner
   (NIP-44's is symmetric: the staff private key with the owner's public key gives the same key as the owner's private
   key with the staff public key). It does not keep the staff private key: the conversation key opens that register's
   copies and cannot sign as it. A public relay the community drops
   from `public_relays` later is left out when the register reads its pairing again, and the register goes on with the
   others; a pairing that no longer reads at all (say the community dropped a price source it names) is not silently
   forgotten: the staff register says it needs a new code.

## For developers

The code is in `static/register-nostr.js`, `static/register-relays.js` and `static/register.js`.

### What is sent

One **replaceable event of kind 30078** (NIP-78) per register and period:

- the owner's register: one per **month**, rewritten a few minutes after a sale, at the day closing and with «Send the
  copy now»; closed months stay as they are, unless an import changes them;
- a staff register: one per **business day**, rewritten twenty seconds after a sale and when its screen is left, so the
  owner sees the sale.

A period is sent again only when its finished sales change (a sale still waiting is not sent), and it counts as sent
when at least one relay accepted all of it. The event:

- `d` tag: `bitmentor-register:<register>:<YYYY-MM or YYYY-MM-DD>[:<part>]`, where `<register>` is the first 32 hex
  characters of HMAC-SHA256 of `bitmentor-register:<the register's id>` keyed with the NIP-44 conversation key of the
  register's key and the owner's. The shop's id is public (it is in the shop's page address), so a plain hash of it
  would tell a relay which shop a copy is of; this one only the register and the owner can work out. The owner needs
  nothing kept for it: the nsec and the copy's author give the same key, on any phone. The owner's register checks it:
  a copy of a known key whose `<register>` is not the one of the register that key is for is not taken;
- `p` tag: the owner's public key, to find the copies;
- content: `1z.` and the gzip of the JSON `{"v": 1, "till", "who", "period", "sales": [...]}` in base64 (`1j.` and the
  JSON where the browser has no `CompressionStream`), encrypted with **NIP-44 version 2** to the owner's public key;
- signed (BIP340) with the register's own key: made on the owner's phone (and kept there), or in the pairing code for a
  staff register.

NIP-44 encrypts at most 65535 bytes. A period too big for one event is split in 2, 4, 8… parts of consecutive sales
(`:1`, `:2`… in the `d` tag), the same parts at every rewrite, so each replaces its own. A sale is about 150 bytes
compressed: a month of a small shop is one event of some tens of KB.

### Bringing the sales back

**The staff registers' sales need nothing from the owner.** The owner's register asks the relays for the copies encrypted
to the owner's key when it opens, when the history is opened and every minute while the page is open, and opens each
copy of a staff register with the conversation key its phone made for it at pairing: no nsec, no button. A key this phone
no longer holds (a code drawn before this phone kept the conversation keys, keys forgotten, another phone, a key made for
another npub) is not read automatically: the register says how many copies it could not read, and the owner gives the
nsec as below. A phone that knows no staff register and has no nsec does not ask the relays at all.

**The owner's nsec** is needed only for what truly needs it: the owner's own months on a new phone (their key was on the
old phone), the staff registers whose keys this phone does not hold, and the copies of keys it does not know (below). In
the history of the owner's register, the owner gives their **nsec**. It stays in the page's memory until the page is
closed: never stored, never sent. Its field is in no form (nothing can submit it to the site, and the browser has no
form to offer to remember), masked without being a password field where the browser can (a password field where it
cannot), and emptied as soon as it is read, also when the key is wrong. The register asks the relays for the events of kind 30078 with the owner's `p`,
checks each signature, opens it (a wrong key or a changed event opens nothing) and merges the sales into the book, each
sale once ([the merge](end-of-day.md#the-encrypted-copy)). Then, every minute while the page is open, it asks again for
what changed (this is the same pull the owner's register makes with no nsec): the staff registers' sales arrive as they happen. «What changed» is what is dated after the last pull
less two days, for a staff register whose clock is behind or whose event reaches a relay late; an event dated more
than a day ahead of this phone's clock is not taken.

- **The owner's own months**, on a new phone: the history comes back.
- **The staff registers' days**: their sales stay marked **«reported»** by that register; the on-chain ones that came
  in or changed are compared with the owner's key, as in an imported file ([On-chain](onchain.md)). Their Lightning
  sales that carry the provider's `verify` URL are checked by the owner's register with the provider and, when it
  confirms the payment, counted apart from the ones to check in the wallet ([History and day closing](end-of-day.md)); the staff
  register adds that URL (and the hash) to its finished sales: a sale without them stays reported. The day closing counts them
  apart, as it does the sales confirmed by hand: the owner checks them in the wallet. A sale the owner confirmed by hand
  never becomes «paid» from a copy, and only whole cents and whole sats within bounds are taken.
- **A copy whose author this phone does not know** (neither this register's key nor one it made for a staff register:
  anyone who knows the owner's npub can encrypt to it) stays out of the book: the register only says how many it left
  out. Its sales are not counted, not kept, and cannot fill the phone. One case needs them: after a restore on a new
  phone the owner's own old months are among them, as their key was on the old phone. So when the owner brings the
  sales back with the nsec, and such copies open with it, the register asks, saying how many copies, sales and keys:
  only if the owner says yes are they taken, every sale «reported», the newest copies first, at most 5000 sales at a
  time (the rest comes back with the encrypted file). Such a copy never changes a sale of this register already on the
  phone. The pulls every minute never take them. The keys of the staff
  registers were on the old phone too: until the owner pairs them again from the new one, their copies are of
  unknown keys as well.

The register asks each relay for at most 500 events and stops listening to a relay past 500 events, 1000 messages or
8 MB for one request, ignored messages included; it drops a message over 90 KB (an event of a copy is under 88 KB) and
waits twelve seconds at most. Of all the relays together it keeps at most 2000 events, one from each relay in turn, so
a relay that sends a lot cannot crowd out the others.

### The CSP

The register's `connect-src` gets the `wss://` hosts of `register.relays` and `register.public_relays` and no other:
a register cannot write to a relay that is not in `community.yml`. An owner who wants another public relay asks the
community to add it to `public_relays`: an open `wss:` in the CSP would let a changed page send the sales anywhere.

The CSP is the same for every register of the community: it lists every host of `public_relays`, also those this owner
did not tick. The register itself writes only to the relays chosen (and the community's), but the CSP does not hold a
changed page to that choice: it holds it to the community's list. A CSP made for each request from the owner's choice
is not possible (the choice stays on the phone, the site does not know it) and would not be worth it: the encrypted
copies are as safe on any of those relays.

### What the community's relay must enforce

The relay is not part of bitmentor, and the site enforces none of this: the values are only recommendations (kept with the
other limits in `config/limits.py`, `RELAY_RECOMMENDED`; see [Limits](../configuration.md#limits)). To keep its cost
bounded, whoever runs it should configure:

- **only kind 30078**, and only with a `d` tag starting with `bitmentor-register:` and a `p` tag;
- **maximum event size**: 256 KB (a part is under 90 KB);
- **a quota per author key**: for example 2 MB and 200 events, and a rate limit (a staff register rewrites its day at
  most every twenty seconds while selling);
- **replaceable semantics** (NIP-01 for kinds 30000–39999): only the newest event per author, kind and `d` is kept;
- ideally **only the paired registers' keys**: the relay cannot learn them from the site (they never reach it), so this
  needs a whitelist the owner gives the relay's operator, or authentication (NIP-42) with a list kept by the community.

With the estimate of the plan (10–30 sales a day, 100–150 KB a month per register), 50 registers make 5–8 MB a month,
under 100 MB a year. Owners who add public relays cost the community nothing for those.

### Limits

- The copies are as private as the owner's nsec: whoever has it reads every copy. Whoever reads the owner's phone
  storage reads the staff registers' copies too (it holds their conversation keys), which that phone's history shows
  anyway; they cannot sign as a staff register with them, and the owner's own copies stay behind the nsec. The relays see who writes (the
  register's key), to whom (the owner's key), when and how big, not the sales nor which shop. In detail, a relay
  sees: the register's public key, the same for all its copies; the owner's public key, the same for all the shop's
  registers, so it learns how many registers write to one owner; the period of each event in its `d` tag (a month or
  a day) and its part; the moment of every rewrite (`created_at`, and when the event arrives), which follows the
  sales: a staff register rewrites its day twenty seconds after a sale; the size of each event, padded by NIP-44 to
  a power-of-two step but still growing with the number of sales; and the network address of the phone that sends or
  asks (a relay can link it to the keys). An owner's npub that is public elsewhere ties all of this to that person.
- A staff register holds its own private key, in its pairing. Whoever controls that phone can send false sales signed
  as that register: they stay «reported», and the owner compares them with the wallet. That key speaks only for its
  own register: the owner's phone knows which register each key it made is for, and a sale in its copy that says it is
  of another register (the owner's own, whose id is public), or that has the id of a sale of another register already
  on the owner's phone, is left out. Only the owner's register's own key writes
  sales that count as the owner's, unreported. The encrypted file has no author key, only a passphrase: a staff
  register's file speaks only for that register (a sale in it that says it is the owner's is left out), and a file that
  says it is of the importing register counts as its own (sales not reported, day closings taken) only on a phone with
  no sales yet or when the owner ticks «It is a copy of this register» in the import form; otherwise its sales come in
  «reported», to check in the wallet, and its day closings stay out ([The encrypted copy](end-of-day.md#the-encrypted-copy)).
- **Taking a key back.** The owner's phone keeps the keys it made for each staff register (up to the last 10 codes
  drawn for it). When the owner turns a staff register off and the day of grace is over, the owner's register page
  carries that register's id (only to the owner) and the phone forgets its keys, conversation keys included: its copies
  are of an unknown key from then on. In the register's settings, «Forget the staff registers' Nostr keys» forgets them all at once, for a phone
  lost or given away: the registers still in use are paired again. A key cannot be taken back from the relays: what it
  wrote before stays there, unread by anyone without the owner's nsec.
- The page that asks for the nsec is served by the site: a changed page could take it. An owner who does not want to
  type the nsec on the register can bring back the copies with the encrypted file instead.
- The arithmetic of the keys is done with JavaScript's BigInt, which does not take constant time. These keys sign and
  open the copies of a register; they do not hold money.
- NIP-44 is tested against the official test vectors (`shops/testdata/nip44.vectors.json`, from
  github.com/paulmillr/nip44, checksum as published in NIP-44), BIP340 against its vectors
  (`shops/testdata/bip340-vectors.csv`), and the whole path, a staff register to the owner's through a relay in memory,
  in `shops/test_register_flow.py`.

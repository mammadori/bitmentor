# On-chain at the register: design notes

These are the developers' design notes of an experimental feature, not a guide: what a community needs is in
[On-chain at the register](onchain.md), and the staff registers are in [Registers for the staff](staff-registers.md).

> **Experimental, off by default.** Everything on-chain below is there only in a community that wrote
> `experimental: {onchain: true}` in `community.yml`: see [On-chain at the register](onchain.md) and
> [Experimental features](README.md#experimental-features). Without it the register takes Lightning only, the settings
> have no on-chain part, and a pairing code carries no on-chain key.

The details behind [On-chain at the register](onchain.md), for developers: how the register picks addresses, which
payment closes a sale, and what a tampered server can do.

## The owner's settings, on the phone

From the register's menu (the round button at the top of the keypad), **Settings**. Everything here stays in the phone's storage (`register-settings:<shop>`),
and reaches a staff register only through its pairing code. The screen is in titled groups, the essentials first: «Where
the money goes» (the Lightning address, shown: it is changed on the site), «Prices» (currency, discount), «On-chain
(optional)» (key, threshold), «Look» (colour, line); the rest under «Advanced» (the starting number, the path, the own
explorer, the price sources, Nostr). Each field has at most one line of help, the long explanations open under a small
summary; a field is checked when it is left (and again as it is typed while it is wrong), and its error shows next to
it, as on Save:

- **On-chain destination.** An extended public key of the wallet (zpub or xpub, vpub or tpub on signet: native segwit;
  ypub or upub: P2SH-P2WPKH), or a fixed address. A private key is refused. A new destination is saved only after the
  register has shown the key's first receive address (or, for a fixed address, the warning) and the owner has ticked
  that it is the wallet's. A fixed address works but is discouraged: every payment lands on the same address, two sales
  of the same amount at the same time cannot be told apart, and whoever sees the address sees every sale. The register
  leaves out the payments already there when a sale starts, those another sale took and a replacement (RBF) of a
  payment that was there (one that spends the same outputs), but a payment arriving late for a cancelled sale, or a
  replacement of a payment that came during an earlier sale, can still be taken for the sale on the screen.
- **Wait for a confirmation above** an amount, in the register's currency: above it an on-chain sale is paid only with
  one confirmation; at or below, a payment seen in the mempool is enough. The community's default is
  `register.confirm_above` (0: every sale waits). The amount takes a point or a comma before the cents; one that is not
  an amount is refused with a message, never saved as 0.
- **Discount for paying in bitcoin.** It starts from the shop's discount, or the community's suggestion
  (`register.discount`) when the shop has none. It is on at every sale, shown with the amount to pay. Only when the owner
  ticks «Let the registers remove the discount on a sale» (`discountRemovable`, off by default; `dr` in the pairing
  code) is the line a button whose tap removes it for that sale, on the owner's register and the staff ones alike;
  otherwise it is a line to read. The sale keeps the price before the discount and the percentage.
- **Currency** (three letters).
- **Advanced: price sources.** At least two, chosen only among those the community lists in `register.price_sources`
  (each one a source the code knows, `PRICE_SOURCES` in `shops/register.py`: never an address typed by the owner). The
  register's `connect-src` holds only those. The choice goes to the staff registers with the pairing code, which
  checks it against the community's list again; a staff register has no settings and cannot change it.
- **Advanced: your own explorer** (optional). The https address of the API of an Esplora or mempool explorer that the shop
  runs itself, for example the mempool app of its own node (`https://mynode.example.org/api`). See «Your own explorer»
  below.
- **Colour** of the register and a **line** shown on it, both carried to the staff registers.

The owner's phone also remembers the Lightning address it takes payments into. If the site gives a different one, the
register stops and says so, until the owner confirms the change was theirs.

## An on-chain sale

«OK, on-chain» is a link under the keypad. An on-chain sale asks at least 1,000 sats: below a few hundred (dust)
wallets refuse to pay, and the fee would eat most of it; the register says to take such an amount with Lightning. For each sale the register derives a new address in the browser
(`static/register-derive.js`): `…/0/n` of the owner's key, the wallet's own receive chain as standard wallets use it,
so that its payments show in any wallet with no setup, on every register of the shop, the owner's and the staff ones,
with the index from one count on the site (below, «One chain for all the shop's registers»). The owner may set another
path in the settings' «Advanced» (below).
Because `…/0/n` is the wallet's own chain, the wallet may hand out the same address to someone else, and a payment to it
could be for something else: the settings say so where the owner pastes the key, and a sale closes only on a payment
that fits it (below, «Which payment closes a sale»). It takes the index on the phone before showing the address (two
tabs never get the same one), and skips an address a sale on the phone already has and one the explorer says has
already received something (a new phone, a restored copy). The count goes with the key and the child: a new key, or a
staff register paired again with another key, starts from 0. A phone with no count for its key (a restored copy, or a
count of an older version, or a count past the last index of the key's chain) starts after the highest address of that
key in its history, checked against the key. That index is taken as it is when one of the register's own sales has it
(not one reported by another register or by a copy without an author) and it is no more than 50 places past the number
of its own sales there. One only reported within that bound (the owner's old months brought back from copies of an
unknown key after losing the phone) is taken only if the explorer says its address received a payment: a forged index
has none. An index further on is never taken, whatever the explorer says (it may be wrong, or lie): it would push the
next payments past the wallet's gap limit. The highest index within the bound that counts (an own one, or a reported
one the explorer says was paid) is taken instead, and with none of them 0.

A brand-new key on the phone (the first setup, a key changed in the settings, a staff register's first pairing) has no
count of its own either: it starts from 0 as such a restored count. Its first on-chain sale asks the explorer about the
address and the places after it (about a dozen questions), and is refused, to take with Lightning, when the explorer
does not answer; from then on the count is the phone's own.

Such a restored count is not the phone's own until the explorer has checked one of its addresses. Until then the
register hands out an address only when the explorer says it is unused, and so are a few places after it (1, 2, 3, 5,
10 … up to 1,000 further on): with months of history missing it may start among old addresses, and an old sale that
expired unpaid looks unused too, so its address would go to a new customer and a late payment of the old one would
close the new sale. When the explorer does not answer, it refuses the on-chain sale and says to take it with Lightning.
A staff register with a count of its own hands out its next address when the explorer does not answer, as before. The
owner's register never does: its addresses are the wallet's receive addresses, which the wallet may have given to
someone else, and without the explorer's answer a payment of theirs already in the mempool would look like this sale's.
The transactions in that answer (none, for an address handed out as unused) are kept as the sale's `before`, never
its payment. Several used
addresses in a row (three, counting those of sales on the phone, skipped without asking) make the register leap forward
(1, 2, 4, 8 … places, then halving the stretch) instead of asking one by one; it asks the explorer at most 100 times for
one sale, and past that says it found no address that is surely unused, to take the sale with Lightning. One
sale never moves the count more than 50 places past what the phone knows by itself: its own count (or a sale of its
own further on in the history, checked against the key, within 50 places plus one for each own sale at the count or
after it: one forged far on does not lift it, however long the history), or, with no count, the number of its own sales in the history
(as the bound above). An explorer that says every address up to there was paid (a community's explorer broken, or
changed to lie) would otherwise send every later payment where the wallet does not look: past the bound the register
refuses the on-chain sale and puts the count back where it was before that sale, with none of the leaps it made (an
explorer that lies cannot move it a little further at each try), unless another tab took an index or the owner saved
a starting number meanwhile: that count stays. It says that many addresses after the last one the
phone knows look used, by the explorer's answers: if the wallet really used them, to set the starting number in the
settings (below), otherwise to ask the community about its explorer. A staff register says to tell the owner, who sets
the starting number on the owner's register. When no question to the
explorer was asked (only addresses of sales on the phone in the way), the register says instead that it found no
address that is surely unused.

**Where the addresses start.** When the owner saves a new key (or path), the register asks the explorer once for the
wallet's first unused receive address, as a wallet does: past the last used one, 20 unused in a row mean the end (leaps
over long used runs and halves back, at most 200 questions). That number becomes the start of the count on the phone
(`start`, with `first` as below), and the sales go on from it; the site's count follows at the first sale, as always.
Up to 50 it is saved at once: a sale would go that far on the explorer's word anyway. Further on, the register shows the
address there and saves it only after the owner has compared it with the wallet and ticked the box, as for a number
typed: an explorer that lies must not send the payments past the wallet's gap limit. When the explorer does not answer
the key is saved without a start, and the first sale looks as a restored chain does.

**Starting number** (Advanced, an override). A shop whose wallet already used more than 50 receive addresses past what
the phone knows (a phone restored with too little history, or a scan the owner did not confirm) gets that refusal at
every on-chain sale. The way out is the owner's: in the settings, «Start at address number», the number after the last used address of the register's path in the wallet
(Sparrow's address list shows it). The register shows the address at that number (`…/0/N` of the key) and saves it only
after the owner has compared it with the wallet and ticked the box, as for a new key. The count of the owner's chain of
the current key is then N, and the sales go to N, N+1, …; the gap count starts there too. Until the explorer has said
that one address from N on is unused, the register hands out none without that answer (when the explorer does not
answer, it refuses the on-chain sale): a number typed one too low is an address the wallet used, and an old payment to
it would close the new sale. From the first such answer the count is the phone's own (an answer about an earlier start, from a sale already
running in another tab, does not end the wait of a number saved meanwhile). A number typed too high is not
caught: the addresses below it that the wallet never used count towards its gap limit, but the gap count starts at N
and does not show them, so the wallet may need a larger gap limit; type the number after the last used address, not
more. The number counts only for the owner's register: a staff register has
its own chain and a count on its own phone.
The screen shows the QR code (a BIP21 URI with the amount), the amount in sats and BTC, and the address with its place in the key («On-chain address …/0/7 of your key»),
so the owner can find it in the wallet. A transaction another sale on the phone already took is never this sale's.

**The owner's wallet and the register's addresses.** The owner's register uses the wallet's receive chain `…/0/n`, so
any wallet shows its payments. Earlier versions used `…/2/n`, which a wallet sees only with a descriptor
`wpkh([fingerprint/84h/0h/0h]xpub…/2/*)` (Sparrow, a Bitcoin Core descriptor wallet): sales made then keep their
address and their place («…/2/7») in the history, and their money stays where it is.

**Which payment closes a sale.** A transaction to the sale's address counts only when both hold:

- **its time**: the register saw it in the mempool during the sale (from its creation to its end, the day of the sale),
  or its block's time is in that span widened by two hours on each side (a block's timestamp may be that far off).
  The moment the register first sees a transaction in the mempool is kept with the sale (`seenAt`), so a payment seen
  in time still counts when its block comes days later (the week of a seen sale, or of a cancelled one). A confirmed
  transaction without a block time is not placed in time and does not count;
- **its amount**: at least what the sale asks and at most 10% more. Less is «Less than asked has arrived», as before;
  much more is «More than asked has arrived … it may be a payment for something else».

**The confidence of a payment in the mempool.** While a payment waits for its block, the register says how sure it is,
in one line with its main reason («sure», «probable» or «low, wait for a confirmation») and as a small bar of three
steps; tapping the bar opens every reason looked at (for example «not marked as replaceable (no RBF)», «fee 12 sat/vB,
at least the next block's 9», «it spends a payment not confirmed yet»). It uses only the explorer's answers, on the same
explorers, so the CSP does not change:

- the transaction itself, from the address's list: its fee and weight (fee rate in sat/vB), the sequence of its inputs,
  and for a confirmed one its block height;
- the current rates, `GET /api/v1/fees/recommended` (`fastestFee` for the next block, `halfHourFee`, `hourFee`), asked at
  most every five minutes;
- its unconfirmed ancestors, `GET /api/v1/cpfp/<txid>` (mempool.space only: how many, and the package's effective fee
  rate), for at most three payments in the mempool at each check;
- the last block's height, `GET /api/blocks/tip/height`, at most every minute, for the confirmations.

An Esplora without one of these endpoints leaves that fact out. The rules (`confidence` in `static/register-chain.js`):

- confirmed in a block: sure (with its confirmations when the heights are known);
- the fee rate, or with unconfirmed parents the package's effective rate (CPFP), below the next hour's rate: low, it
  may wait long, or be dropped or replaced;
- between that and the half-hour rate: probable; at or above it: sure (the reason says whether it reaches the next
  block's rate);
- an unconfirmed parent: at most probable, since if the parent is dropped or replaced the payment goes with it;
- replaceability signalled (BIP125: an input with a sequence below `0xfffffffe`): at most probable. Since Bitcoin Core
  28 full-RBF is the default, so any payment in the mempool can in practice be replaced: the signal is only a hint (and
  most wallets set it), the fee level counts more;
- fee rates unknown, or the ancestors unknown (an explorer without `/v1/cpfp`): probable; «sure» needs both.

Amounts: a sale above `confirm_above` waits for a block as before, whatever the confidence; one at or below it is paid
from the mempool only when the confidence is not low, otherwise it stays «seen» until its block. No known small
JavaScript library scores zero-confirmation risk (wallet libraries leave it to the application), so the register has
these few rules of its own.

When two payments in the sale's time together make much more than asked, «over» wins over «paid»: the register does
not pick one, and the merchant confirms by hand after looking in the wallet.

**What remains.** If the wallet gave one of these receive addresses to someone else, and that person pays exactly the
sale's amount (up to 10% more) during the sale, the register takes it for the sale's payment: nothing in the chain tells
the two apart. That risk is accepted, and the owner is warned where the key is set, and told not to hand out the
wallet's receive addresses while the register is in use.

A payment out of the sale's time is shown as such («A payment of … reached this address before this sale or long after
it»). In both cases the sale is not paid, the register offers to confirm it by hand after looking in the wallet, and at
the end of its day it becomes «not checked». The span is checked against the phone's clock and the blocks' times; a
transaction already in the mempool when the sale starts is ruled out by the explorer check that hands out only an
unused address.

**The gap limit.** A wallet stops looking after 20 unused addresses in a row. A sale started and never paid (cancelled,
expired) uses its address all the same, so after 20 or more unpaid on-chain sales in a row the wallet may not show the
next payments until its gap limit is raised. The owner's register counts, from the history on the phone, how many of its
chain's latest addresses went unpaid in a row: the settings show the count, and above 15 the register's menu says to raise the
gap limit in the wallet. It counts from the newest sale of the phone paid to the key (or from the starting number the
owner set), never from addresses the register skipped on the explorer's word alone: if the explorer was wrong, those
are empty in the wallet, which then needs the larger gap limit. After a leap over addresses that were really used, the
hint may show until the next paid sale.

**Several phones.** The index comes from the site's count (above), so two phones with the owner's register, or the
owner's and a staff register, never get the same one. The owner should still not hand out receive addresses from the
wallet app while the registers are in use: the wallet does not share the count, and may give out an address a register
is showing (the explorer check skips only addresses already paid).

The register then asks the explorer every ten seconds (`static/register-chain.js`): «seen, waiting for a confirmation»
and then «payment received». Less than asked is said as such and never counts as paid. A seen payment can be left to
finish later («Next customer»): the sales left waiting are checked again when the register opens and every minute, and
after a day one the explorer never saw ends as expired, and one with less than asked, or that the explorer could not
tell, as «not checked», to look for in the wallet. The sale on the screen ends the same way after its day, and one seen
in the mempool is left to its confirmation. A payment seen in the mempool may wait days for a confirmation: after its
day it is looked at every ten minutes, and only after a week without a confirmation does it become «not checked»; an
explorer that does not answer never ends it before. When the explorer does not answer, the merchant can look in the
wallet and confirm by hand, as with a Lightning address without LUD-21; the button goes away when the explorer answers
again. With a fixed address the register needs the
explorer at the start of the sale, to know which payments were already there.

The history, the day closing and the CSV count on-chain sales on their own line, with the address and the transaction.

## Where the register sees a payment

The register asks a public explorer for the transactions of the sale's address: mempool.space's API by default, or an
instance of the same API that the community runs. In `community.yml`:

```yaml
register:
  explorers:
    mainnet: [https://mempool.space/api]
    signet: [https://mempool.space/signet/api]
```

`explorers` can be one list, used on any network, or one list per network. Each value is an `https://` address with a
domain name and a plain path, no port, query or fragment; anything else is left out, and `manage.py check` says so
(`shops.W006`). Only the origins of these explorers are added to the register's `connect-src`: the page cannot talk to
any other host (plus the shop's own explorer, if the owner set one: [below](#your-own-explorer)). The explorer sees the address the register asks about, and the address the request comes from.

## Staff registers: the pairing code

The owner adds a register for a person in «Registers for your staff», then opens its code page **on the phone with the
owner's register**. That page draws the QR code there (`static/staff-pair.js`): the one-time link of the site, and after
`#pair=` the pairing (`static/register-pairing.js`, compact JSON in base64url):

- the shop's Lightning address as the site has it (no code if the owner's register pays into another one);
- on-chain: the extended public key of the shop's chain (the owner's key at the path, `…/0` by default) and the path,
  or the fixed address. The register takes …/n of it with the index from the site's count (above);
- the shop's name, town and colour, the owner's line, the discount, the currency, the price sources and the threshold
  for a confirmation;
- with the owner's Nostr public key in the settings and relays in `community.yml`: that key, a private key the owner's
  phone makes for this register (it keeps the public one and the key that register and the owner share, to read its
  copies without the owner's private key) and the public relays the owner chose
  ([Copies on Nostr](nostr-copies.md)). Such a code is version 2; without it the code stays version 1, which older
  registers read too, and an older register reading version 2 asks to reload the page.

The fragment of a link never reaches the server: the site never sees the key nor the preferences. The opening page
keeps the fragment for a moment, only for a valid code and for that register (`static/staff-open.js`), and the register
page takes it, checks all of it (anything unknown or out of bounds refuses the whole code, except a public relay the
community no longer lists, which is left out) and keeps it. Opened on
another phone, the code page cannot know the owner's settings: the code carries Lightning only, and says so.

A staff register has no field to change where the money goes. Without a pairing, or when the site's Lightning address
is not the one in its pairing, it takes no payment and says to ask the owner. To pair it again (new settings, new key,
new address) the owner makes a new code and the person scans it. The site keeps the list of registers, to turn one
off, and the `k` of those paired before the shared chain.

### One chain for all the shop's registers

Every register of the shop hands out the addresses of the owner's receive
chain, `…/0/n` (or the owner's path), so that every payment shows in the owner's ordinary wallet. Two registers must
never show the same address, and the wallet must not see more than 20 unused addresses in a row (its gap limit). The
choice: **one count per shop and chain, on the site** (`shops/chain_count.py`, `ChainCount`). When a register prepares
an on-chain sale it asks the site for the next index (`POST …/chain/`, at least its own count); the site gives each index
once, under a row lock, and never takes one back (a register that cancels leaves a hole of one: taking it back would
let any register hand out an address still on another screen). The alternative, blocks of
indexes reserved for each register, was set aside: a block small enough to stay within the gap limit (say 5 each) runs
out within an hour at a busy till and needs the site anyway, and an idle register leaves its block unused, a hole in
the wallet's chain; with one counter the only holes are sales started and never paid, as with one register alone.

What the site learns: how many on-chain sales the shop started, and a SHA-256 of the chain's extended key that names the
count (it cannot derive an address from it, nor from an index). The owner's register asks as the signed-in owner (the
shop's id is public), with the page's CSRF token; a staff register with its key, while turned on. Without the site's
answer no address is handed out («The site does not answer…»), and an owner's register not signed in says to sign in.
Only the owner opens a chain's count (the owner's register's first claim, or the owner's phone when it draws a pairing
code): a staff register only counts on one that exists, and cannot fill the shop's 20 chains. A staff register may not
ask more than 19 places past the count (within the wallet's gap limit), and each register has its own budget of 300
requests an hour; the owner may ask any start, to set it where the wallet is. The register still asks the explorer about every index (the
wallet may have used the address) and goes on to the next one when it is used. The gap hint of the owner's settings
counts the addresses after the owner's last paid sale, those the other registers took included: it may say more than the wallet sees, never less.

The path. In the settings' «Advanced» the owner may write the path of the receive chain after the pasted key: empty is
`0` (`…/0/n`); otherwise one to four whole steps separated by «/», none hardened (an extended public key cannot derive
those), each below 2^31, for example `0/5` for a wallet that receives on `…/0/5/n`. A new path, like a new key, is
shown with its first address and saved only after the owner ticks that the wallet shows the same; the settings show
the path in use (`…/0/n`). Every register uses it: the owner's at once, a staff register when the owner pairs it again
(the code carries the chain's own extended key, `…/<path>`, and the path, `oc: {x, p}`). That key lets the staff phone
derive every receive address of that chain, as the owner's wallet does: it is watch-only. Changing it does not lose
the old addresses: each sale keeps its address and its path (`path` in the history, «…/0/7»), the payment is checked on
that address, and the new path has its own count on the site and on the phone.

### Your own explorer

A merchant with a Bitcoin node of their own can check the payments against it. In the settings' «Advanced» the owner
types the base address of an Esplora or mempool-compatible API (`/address/<address>/txs` and, for the confidence,
`/v1/fees/recommended`, `/v1/cpfp/<txid>` and `/blocks/tip/height`: the mempool app has them all, a plain Esplora lacks
the last-but-one and the register then says «probable»). Saving it needs the register to be reloaded.

- **Order.** The owner's register and every staff register of the shop (they get it with the shop's data, so a staff
  register needs no new pairing) ask the shop's explorer first, for the transactions, the fee rates, the tip and the
  ancestors. On a network error, a timeout, an answer that is not OK or one that is not an explorer's, they ask the
  community's explorers in order, as before. An empty list of transactions from the shop's explorer (a node still
  syncing, or without its backend, answers so) is checked with the community's too, and stands only when none of them
  answers. The next round asks the shop's again first.
- **What the owner sees.** The confidence's reasons (the small bar, opened by a tap) end with the explorer that answered:
  «answered by your own explorer», «answered by mempool.space: your own explorer did not answer» or, with none set,
  «answered by mempool.space».
- **What it must be.** An `https://` address with a domain name (the register is an https page: a plain `http://` one
  would be blocked as mixed content), no user or password in it, no query or fragment, a plain path, at most 200
  characters; a port is allowed. The same checks run on the site, which keeps the address on the shop (it is not a
  secret) because the register's CSP must open that origin (`connect-src`, with the port): the site never fetches it,
  the phone's browser does.
- **What the node must do.** Answer the browser: CORS has to allow this site's origin (`Access-Control-Allow-Origin`),
  and the certificate has to be valid for the browser (a reverse proxy with a real certificate; a self-signed one on a
  local network does not work). A node reachable only from the shop's network only helps registers on that network, and
  the community's explorers cover the rest. A bare Bitcoin Core RPC cannot be used: it has no address index and no
  CORS, and the browser cannot hold its password.
- **Lightning** is unchanged: the register already asks the shop's own Lightning address provider (LUD-21), which is the
  shop's node if it runs one.
- **Trust.** Like any explorer, the one that answers can lie about a payment. A shop's own node removes the community's
  explorer from the picture while it answers, and the address is kept by the site, so a tampered server could replace
  it, just as it could serve a changed page (see below).

## What a tampered server can do

The site serves the register's page and scripts. Whoever controls the server, or the phone, can change the page, and a
changed page can show any address: that limit cannot be removed by the page itself. What the register does is make a
change visible to the owner:

- the destination, the preferences and the counters never leave the phones, so changing data on the server (the shop's
  Lightning address, a register's `k`) cannot move the on-chain money; a changed Lightning address stops the owner's
  register and the staff registers paired before it;
- every on-chain address is shown with its place in the key, to be found in the wallet;
- when the owner imports a staff register's copy at the end of a shift, or pulls it from Nostr (there, the sales that
  came in or changed), the owner's register derives again, from the key on its own phone, the address of every
  on-chain sale from its `k` and `n` (paid, seen, or still waiting or not checked when the copy was made: only those
  that ended unpaid are left out), and names the sales whose address is not the one the key gives. A sale the copy
  could not bring in (one it says is of another register) moved nothing here, and is not named. The address compared is the one the book keeps: a sale already on the phone keeps its address when a copy changes it, so a copy that first brings a sale as unpaid with one address and then as paid with another is checked on the first. That catches a changed page that writes down where the money went, not one that writes
  the right address and shows the customer another QR code: only the wallet catches that. The owner compares the
  history with the wallet: a reported payment missing from the wallet is an alarm.

## The fake explorer

On signet a test payment may not reach a public explorer, and the tests must not ask the network. With
`FAKE_EXPLORER=1` in `.env` the register asks the site itself (`/dev/explorer/api/address/<address>/txs`), which
answers like mempool.space with the answers recorded in `shops/testdata/explorer.json` and with the payments pretended
on `/dev/explorer/`: an address and the sats, in the mempool or confirmed, then the confirmation. Nothing moves any
money. The page needs no login, so it takes at most 60 pretend payments an hour from one visitor's address and 600 in
all, and keeps the pretend payments of at most 200 addresses (the oldest are forgotten). The site refuses to start with `FAKE_EXPLORER=1` on mainnet (so in production too), and the pages answer 404
unless the flag is on and the network is not mainnet.

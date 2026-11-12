# On-chain at the register

> **Experimental, off by default.** On-chain is the most complex part of the money path: the addresses are derived in
> the browser, the payments are seen through public explorers, and the site keeps a count. It works and is tested, but
> it has had less review and less real use than Lightning, and it may change or go: see
> [Experimental features](README.md#experimental-features). A community turns it on in `community.yml`, then restarts
> the site:
>
> ```yaml
> experimental:
>   onchain: true
> ```
>
> Without it nothing below exists: the register takes Lightning only, and its pages, the map, `/shops.json` and the
> addresses of the count say nothing of on-chain.

The register takes Lightning through the shop's Lightning address. On-chain is for special cases: a large sale, or a
customer whose wallet has no Lightning. The merchant sets it up on the phone with the register, not on the site.

What organisers need to know, once it is on:

- The site never learns where on-chain money goes, and never sees an on-chain payment.
- The owner pastes the wallet's extended public key (or a fixed address) in the register's settings. The key stays on
  the phone, and reaches a staff register only through its pairing code.
- The register shows a new address of the owner's wallet for each sale, and checks the payment with a public explorer
  (mempool.space by default).
- The site keeps one thing: a counter of the addresses handed out (`ChainCount`), so two registers of a shop never show
  the same address. It holds no amounts. See [What the site keeps](../../README.md#what-the-site-keeps).
- A flag in the shop's form, «I take on-chain payments too», lets the map, the shop page, `/shops.json` and
  OpenStreetMap (`payment:onchain=yes`) say it.

## For developers

- **Settings, on the phone** (`register-settings:<shop>`): the destination (zpub/xpub, vpub/tpub on signet, ypub/upub;
  or a fixed address, discouraged), «wait for a confirmation above» an amount (`register.confirm_above`), the discount
  (and whether the registers may remove it on a sale: off by default),
  the currency, the price sources (only those in `register.price_sources`), an optional explorer of the shop's own,
  the colour and a line.
- **Addresses.** «OK, on-chain» is a link under the keypad. Each sale takes `…/0/n` of the owner's key, the wallet's
  own receive chain, derived in the browser (`static/register-derive.js`). The index comes from the site's count
  (`shops/chain_count.py`, `POST …/chain/`). Saving a new key asks the explorer once for the wallet's first unused
  address and starts the count there; a number typed under Advanced overrides it. The register skips addresses the
  explorer says were used. At least
  1,000 sats per sale.
- **Which payment counts.** A payment to the sale's address during the sale, for at least the amount asked and at most
  10% more. Above `confirm_above` it waits for a block; below, a mempool payment counts when its confidence is not low
  (`static/register-chain.js`).
- **Explorers.** `register.explorers` in `community.yml`, one list or one per network, `https://` only; only their
  origins enter the register's `connect-src` (`shops.W006` warns about bad values). The explorer sees the address asked
  about and the visitor's IP address.
- **Tests.** With `FAKE_EXPLORER=1` (never on mainnet, and only with on-chain turned on) the register asks
  `/dev/explorer/` on the site, which answers from `shops/testdata/explorer.json` and from pretend payments.
- **The switch.** `config/features.py` reads `experimental.onchain`; the register page then carries `chain_on`
  (`shops/register.py`, `onchain_data`), which `static/register.js` reads before it offers on-chain at all. Turned off
  again, a phone keeps its key, its count and its history: it stops offering on-chain and asks no explorer. An on-chain
  sale that was waiting goes at once to "not checked: look in your wallet", and one that was cancelled gets no late
  look: turned on again, those sales are not asked about any more. A staff register paired while on-chain was off has
  no on-chain until it is paired again.

The full design (restored counts, the starting number, the gap limit, staff pairing, a shop's own explorer, what a
tampered server can do) is in [the design notes](onchain-notes.md).

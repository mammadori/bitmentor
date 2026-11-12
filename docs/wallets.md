# Choosing wallets for your community

The site never touches the money. Each shop is paid into its own wallet, through the wallet's Lightning address. The
community's job is to suggest a few wallets that work well at a till, and to list them in `community.yml`
(`shop_form.wallets`, `shop_page.wallets`).

## What a wallet must do

- **A Lightning address** (LUD-16), like `shop@wallet.example`. The register asks it for an invoice at each sale.
- **LUD-21 `verify`.** With each invoice the wallet gives an address where anyone can ask «is it paid?». The register
  asks it, and says «paid» on its own. Without it the merchant must look in the wallet at every sale.
- **Its own domain.** The invoices and the «is it paid?» address must be on the address's own domain or a subdomain of
  it (`shop@wallet.example` answered by `wallet.example` or `api.wallet.example`): the register talks to nobody else,
  so that a changed answer cannot send it to another site. An address on the merchant's own domain that only forwards
  to a service elsewhere does not work.
- **Answers to the browser (CORS).** The register runs in the phone's browser and asks the wallet's server directly.
  The server must allow that (`Access-Control-Allow-Origin`).
- **Receives with the phone off**, or at least with the wallet app closed: the register is often on another phone.

The site checks the first three when the merchant pastes the address, before the register is made, and refuses an
address that fails one, saying which. CORS and the phone switched off show only in a real try (below).

## What we suggest

- **A self-custodial wallet on Spark.** The merchant keeps the keys (the recovery words). It receives with the phone
  switched off, with no channels and no liquidity to manage. Blink in its non-custodial mode was tested on mainnet on
  11 November 2026: the register saw the payment `settled` through `verify` with the wallet's phone switched off. Other
  Spark wallets with a Lightning address, such as Glow (breez.tips) and Cake Wallet, are worth trying the same way. On
  12 November 2026 an address of each of the three (`blink.sv`, `breez.tips`, `cake.cash`) passed the site's check and
  answered the browser: `verify` is there, on the address's own domain, with CORS open. That is the check, not a
  payment with the phone off: only Blink had that.
- **The merchant's own Lightning node**, for those who run one: its Lightning address works in the same way, if it
  offers LUD-21.
- **On-chain** only for special cases, and only where the community turned it on: it is an experimental feature, off
  by default, set up by the owner on the register's phone. See [On-chain](features/onchain.md).

A custodial wallet works too, if it meets the requirements above. Then the money is held by the provider: say so to
the merchant.

## How to try a wallet

1. Install it on a phone and create a Lightning address.
2. On your site, make a shop with that address. The form says whether `verify` works.
3. Switch the wallet's phone off. From another phone, pay a small amount at the shop's register.
4. The register must say «paid» by itself. Then switch the wallet's phone on: the money must be there.

Do it on mainnet with a few hundred sats: few wallets offer a Lightning address with `verify` on signet.

## Tell the others what you tried

The project does not test every wallet, and wallets change: one that worked may lose `verify` with an update, one that
failed may work now. The communities that use bitmentor are the ones who know. When you try a wallet with the steps of
[How to try a wallet](#how-to-try-a-wallet), write it in the project's issue tracker
(<https://github.com/mammadori/bitmentor/issues>), whether it worked or not:

- the wallet and its version, and the date you tried it;
- the network (mainnet or signet);
- whether it worked, and if not at which step it stopped and what the form or the register said.

The date and the version are what make a report useful to the next community: without them nobody knows whether it
still holds.

## For developers

The check is in `shops/lnaddress.py` (`probe`): it asks the address for an invoice of its smallest amount (at least 1 sat), then asks its `verify`
URL and reads `settled`, paying nothing, and accepts both only from the address's own domain (`same_provider`). The
register does the same in the browser at each sale (`static/register.js`, `trusted`), where CORS also applies.

# The shop page and the new shop form

## The shop page

The QR code on a shop's sign opens the shop's page, so whoever reads it is usually standing at the till. Besides
name, discount, the details the owner chose to show ([What customers see of a shop](shop-details.md)) and map, the page has:

- **How to pay here**: three steps that follow how the shop takes bitcoin. A shop with a register takes Lightning, and
  on-chain too when the owner ticked it. An imported shop has its own till: there the Lightning flag of the list
  decides, and without it the shop takes on-chain. All of on-chain only where the community turned
  [on-chain at the register](onchain.md) on (experimental, off by default): without it the page never says on-chain, the
  owner has no tick, and the shop with its own till and no Lightning flag is asked "to pay in bitcoin", no more.
- **Show at the till**: a full-screen card to turn towards the cashier, with the sentence to ask to pay in bitcoin (with
  Lightning, when the shop has it, and with the discount, when there is one) in the community's main language, the
  first one in `languages`. Below, smaller, the same sentence in the visitor's language, when it is another one. A tap
  or Esc closes it.
- **Never paid with bitcoin?** Two links under the steps: "I don't have a wallet" and "I have a wallet, but I'm not sure
  how to pay". They lead to the customer's pages, which need nobody's explanation: see
  [The customer at the till](customer-path.md).
- **Nearby**: up to four other places to pay within 1.2 km, the closest first, with the distance and the minutes on
  foot. They are computed on the server from the shop's position: no request from the page, no visitor position.
  Suspended shops, shops off the maps, historic shops and shops without a position are left out.

A historic shop and a suspended one have neither the steps nor the card.

The community decides where the page sends people, in `community.yml`:

```yaml
shop_page:
  # The community's page on wallets, linked from the start of the customer's pages (customer-path.md)
  wallet: learn/wallet/
  # The community's pages about a shop, under its external_id (the number in the imported list)
  stories:
    "12":
      - path: walk/#bakery
        text: {en: "It is a stop on the walk", it: "È una tappa della passeggiata"}
      - path: history/#2015
        text: {en: "The first payment, 2015", it: "Il primo pagamento, 2015"}
```

Write texts and `external_id`s between quotes: in a `{…}` a comma ends the text, a bare `2015` is a number, and YAML
reads `012:` as an octal number, 10.

A link is the path of a page after `/<lang>/`, as in `menu`, or a full `https://` address; both can also be written per
language, `{en: …, it: …}`. `www.example.org/…`, `mailto:` and other schemes are not links to pages, and stay out. A text without the page's language uses the default one. bitmentor names no wallet: the
community picks the page. `stories` appear under "In our story" on every page of that shop but a suspended one, a
historic one included. A link or a text that cannot be used stays off the page, and `manage.py check` says why, in
every language (`website.W002`).

## The new shop form

The Lightning address is the hardest field, so it is tried as soon as the merchant pastes it or leaves the field, with
the same rules and the same probe as on submit (`shops/lnaddress.py`: HTTPS only, public hosts only, no redirects,
limits on time and size). The result appears under the field: it works, with the provider's domain, or what is wrong
and what to do: not an address, a payment request instead of the address, BOLT12, a provider that does not exist (with
"Did you mean …?" when a provider one typing slip away is used by at least two active shops of different owners), an
address that does not answer, a wallet that does not tell the register when a payment arrives. The page and the server
tidy what gets pasted: spaces, quotes, a final full stop, `lightning:`, capitals, an
`https://provider/.well-known/lnurlp/name` address.

The check is `POST /<lang>/shops/new/lightning-address/`: logged-in users only, with CSRF, 12 checks per person, 40
per IP address and 80 per /48 network every 10 minutes, 6 seconds at most for the whole check, DNS included, and two
checks at a time per `web` process, in threads of their own: the others answer "busy" at once. It answers the tidy address, a state, our own
message and at most a suggested address: nothing of what the provider said. A working address is remembered for 15
minutes, a problem for 30 seconds, for each person. The submit is still the real check: it skips only the probe of the
very same address that has just worked for the same person, and probes any other one. Without JavaScript the submit does the check, as before.

Under the field, "Where do I find it?" says where wallets keep the address. The community can link its own page about
the wallets that work with the register, the same kind of link as in `shop_page`; `manage.py check` says when it cannot
be used (`website.W006`):

```yaml
shop_form:
  wallet: learn/wallet/
```

### A merchant without a wallet

Many merchants who sign up have no wallet yet. The form lets them tick "I do not have a wallet yet" and leave the
field empty: the shop is saved with its page, but without a register, without a destination and off the map, the
lists, `/shops.json` and Nostr (`Shop.awaiting_wallet`). The panel shows "Last step: the wallet", and
`/<lang>/shops/wallet/<id>/` walks the merchant through it: choose a wallet, install it, create it (the words on
paper, never on the site), copy the Lightning address, paste it, where it is tried live as in the form. Saving gives the
shop its register, with the same checks as a new shop, and the shop goes on the map. The page is for the owner only,
with CSRF and the same limit as new shops; it opens only while the shop has no Lightning address, and never for a
historic or suspended shop. Until the wallet the shop has no sign, no
window and no discount on its page, which says it does not take bitcoin yet. Moved with `export_shops`, it travels
with `register: true` and arrives on the other site waiting for its wallet too.

The wallets the guide offers come from the community, never from the software. Only open source, non-custodial and
free wallets may be suggested (MiCAR: never an exchange or a custodial wallet), and the software cannot check that: the
community states it for every wallet with the three `true`, and a wallet without them is left out. `manage.py check`
says what it left out and why (`website.W006`). Without `wallets` the guide links to `wallet`, or says whom to write to.

```yaml
shop_form:
  wallet: learn/wallet/
  wallets:
    - name: Example Wallet
      link: https://wallet.example.org/     # or a page of the site
      platforms: [android, ios]             # android, ios, windows, macos, linux, web
      description: {en: "Free, and the keys stay with you.", it: "Gratis, e le chiavi restano a te."}
      open_source: true
      self_custody: true
      free: true
```

The edit form does not change where the money goes: a new wallet is a change of address, same shop and same sign
([Changing a shop's Lightning address](address-change.md)).

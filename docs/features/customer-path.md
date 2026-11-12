# The customer at the till: no wallet, or no idea how to pay

A merchant takes bitcoin only if the customer knows how to pay, and the merchant, who knows nothing about Lightning,
cannot help. The QR code on the sign opens the shop page, so whoever reads it stands at the till with the phone in
hand. The shop page leads them to two paths that need nobody's explanation. They are open to anyone: no sign-in,
nothing to fill in, nothing kept about the visitor, and no request from the page to any other server.

## Where they are

Under the shop pages, in every language (`shops/customer.py`, the pictures in `shops/customer_art.py`):

| Page | Address | For whom |
|---|---|---|
| Start | `/shops/customer/<shop>/` | The two ways in, with the discount in money ("On a bill of 20.00 EUR, you save 2.00 EUR"). It is where the merchant's sheet leads |
| Choose | `/shops/customer/<shop>/1/` | Who has no wallet: the community's wallets, for the visitor's phone |
| Install | `/shops/customer/<shop>/2/?w=<wallet>` | Install the chosen wallet, make it, write the words on paper |
| Get ready | `/shops/customer/<shop>/3/?w=<wallet>` | The first sats, and how to pay here |
| How to pay | `/shops/customer/<shop>/help/` | Who has a wallet and does not know what to do: what to scan, what to expect, what to do when the wallet says error |

The shop page's "How to pay here" box has the two links, under the three steps. For a historic shop, a suspended one or
one waiting for its owner's wallet, every page sends the visitor to the shop page, which says why nobody can pay there:
a sheet that outlives the shop does not lead to an error. The pages are kept out of search engines and told to caches
to keep a copy per phone only on the first screen, when the visitor has not chosen one (`Vary: User-Agent`).

## Who has no wallet

Three screens, each with one thing to do and a picture:

1. **Choose.** The wallets come from the community, never from the software, and follow the same rule as the
   merchants' list (MiCAR): only open source, non-custodial and free wallets, which the community states with the three
   `true`; one without them is left out and `manage.py check` says so (`website.W008`). The list starts with the wallets
   that run on the visitor's phone: the make is read from the browser once, to choose the list, and forgotten; "Show the
   wallets for: Android, iPhone, All" changes it with a link. A shop that takes only Lightning, or only on-chain,
   offers the wallets that can pay it (`methods`). On-chain is said of a shop only where the community turned
   [on-chain at the register](onchain.md) on, an experimental feature that is off by default: without it a shop with a
   register is a Lightning shop, and of a shop with its own till and no Lightning flag the pages say only "bitcoin".
2. **Install and make it.** Open the wallet's own page, install it from the phone's store, check the maker's name
   (fake copies exist), make the wallet and write the words on paper, never anywhere else: "Whoever has the words has
   your money". The page never asks for them.
3. **Get ready.** On a test network (signet) the screen explains that the sats are for trying and the wallet must be set
   to the same network, how to copy the receiving address, and says whom to write to for a few test sats. On mainnet the screen says the wallet is empty and what to do: ask a
   friend who has bitcoin to send a small amount, or follow the community's own page (`first_bitcoin`). The software
   names no company, sells nothing and links only what the community writes there: that the page keeps to MiCAR (no
   exchange, no custodial service) is for the community to guarantee, as for the wallets. It ends with what
   happens at the till and the way back to the shop.

## Who has a wallet

One page, in plain words: ask to pay in bitcoin (with the line that fits the shop: Lightning, or, where on-chain is turned on, on-chain or both), look at
the code on the till, scan it with the wallet's own scanner, check the amount and confirm, wait for the tick (the
cashier waits for it on the till: only the till knows the money arrived). Then what to expect (a few seconds with
Lightning, about ten minutes on-chain where a shop takes it, the discount to ask for at the till, as the card to show the cashier does) and, in answers that open without JavaScript,
what to do when:

- it does not scan (the wallet's scanner, brightness, the code as text);
- there is not enough money;
- the code is not valid or the network is wrong (on signet: the wallet must be on the test network; on mainnet: the
  other way round);
- the code has expired (ask for a new one, nothing was charged);
- the payment failed (Lightning shops only);
- the customer paid and the till shows nothing (do not pay again; the cashier looks at the till, not the phone);
- the customer paid too little or too much.

The pictures are drawn by the server as SVG, without any text: a phone, a sheet of paper, a code. The "code" is a drawing
that looks like a QR code and is not one, so no camera reads it. They carry the colours of the community's theme, dark
mode included, and are hidden from screen readers: the words carry the meaning.

## The sheet for the till

In the panel, "Card for customers without a wallet" opens `/shops/customer-card/<shop>/`, an A4 sheet (the code on
white, in a dark theme too) in the language chosen, to print: "No wallet yet? Scan this code with your phone", a QR code that opens the start page, three small
pictures (choose and install, get the first bitcoin, scan and pay) and the discount. The merchant points at it when a
customer says they have no wallet, and explains nothing. Only for a shop where a customer can pay, and only for its
owner. A link on the sheet opens the customer's pages, so the merchant sees what the customer sees.

## What the community writes

In `community.yml`, `shop_page` (see [The shop page](shop-page.md#the-shop-page)):

```yaml
shop_page:
  wallet: learn/wallet/            # the community's page on wallets, linked from the start page and from the first screen
  first_bitcoin: learn/first-bitcoin/   # on mainnet: where a customer finds a first bitcoin; optional
  wallets:                         # optional: the wallets for customers; without it, shop_form.wallets
    - name: Example Wallet
      link: https://wallet.example.org/
      platforms: [android, ios]    # android, ios, windows, macos, linux, web
      methods: [lightning, onchain]   # what the wallet can send; both if omitted
      description: {en: "Free, and the keys stay with you.", it: "Gratis, e le chiavi restano a te."}
      open_source: true
      self_custody: true
      free: true
```

`wallets` uses the same fields and the same rules as `shop_form.wallets` of the merchants' guide (a wallet that receives
to a Lightning address is not always the best to pay with, so a community can list others), with `methods` in addition.
Without any list the first screen links to `wallet`, or says whom to write to. `manage.py check` says what it left out
and why (`website.W008`).

## What it does not do

- No JavaScript, no form, no cookie, no request from the page to any server: the page works the same with scripts off.
- It stores nothing and asks for nothing. The wallet's page, chosen by the community, opens in the visitor's browser
  with `rel="noopener noreferrer"`; the software names no wallet and links to none of its own.
- It never touches the register, the payment of a sale or the destination of a shop's funds.
- It does not check that the wallets of the list are what the community says: it asks the community to state it.

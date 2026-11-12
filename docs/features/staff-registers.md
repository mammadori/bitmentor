# Registers for the staff

The owner's register is the shop's id, which the shop page shows: it is for the owner, and it cannot be turned off. A
**staff register** is a register for one person who works with the owner. It has a name («Anna», «evening shift»), its
own secret key, and it does one thing: it takes payments for the shop, into the owner's wallet. The person sees where
the money goes and nothing else of the owner's wallet, sales or settings, and has no field to change any of it. Like
the owner's register it keeps its sales on its own phone: the site never receives them. Always on; a shop can have
`staff_registers_per_shop` of them turned on at once (30, [Limits](../configuration.md#limits)).

The code is in `shops/staff.py`, `static/staff-pair.js`, `static/staff-open.js` and `static/register-pairing.js`.

## Pairing one, with a one-time code

1. **The owner adds the register.** In the panel, «Registers for your staff» (`/shops/registers/<shop>/`): a name, which
   shows at the top of that register's screen, and «Add the register».
2. **The owner shows its code**, on the phone with the owner's own register. The page draws a QR code in the browser
   (`static/staff-pair.js`): the site's one-time link, and after `#pair=` the pairing (below). The owner checks the
   summary under the code: the Lightning address, the discount, the currency. The code can be scanned from the screen,
   printed as a card, or sent as a link in a private message. It works **once**, for **24 hours**: a photo of it is no
   use afterwards. «Make a new code» replaces it.
3. **The person scans it** with the camera of the phone that will take payments. The link opens a page with one
   button, «Open the register on this phone»: a page and not a redirect, because chats and camera apps open links by
   themselves to preview them and would use the code up. The first phone that presses gets the register; the code then
   opens nothing. The same address may try 30 codes in ten minutes.
4. **The register stays on that phone**, at `/shops/register/p/<register>/?k=<key>`: the person adds the page to the
   home screen and opens it like an app.

The key and the one-time code travel in the query and in the body of a form, never in a path: the logs of gunicorn and
of bitmentor's Caddy drop them, and the register's pages are served with `Referrer-Policy: same-origin`, `no-store` and
`noindex`.

## What the pairing carries

The pairing is the part of the link after `#pair=`. A browser never sends that part to a server: the site sees the
one-time code, not the pairing. It is compact JSON in base64url (`static/register-pairing.js`), made on the owner's
phone from what the site says and from the settings of the owner's register on that phone:

- the register's id;
- the shop's **Lightning address**, as the site has it. If the owner's register on that phone takes payments into
  another address, no code is drawn until they are the same;
- the shop's name, town and colour, and the line the owner wrote for the registers;
- the discount and whether a register may take it off a sale, the currency, the price sources and the amount above
  which an on-chain sale waits for a confirmation;
- with the owner's Nostr public key in the settings and relays in `community.yml`: that key, a private key made for
  this register and the public relays the owner chose, so that the register sends its sales to the owner, encrypted,
  as they happen ([Copies on Nostr](nostr-copies.md));
- with on-chain turned on (experimental, [On-chain at the register](onchain.md)): the key of the shop's receive chain,
  or the fixed address.

Opened in a browser that does not hold the owner's register, the code page cannot know those settings: the code
carries the Lightning address and the site's starting values, as the summary under it shows.

On the person's phone the opening page keeps the pairing for a moment, only for a code that opens a register, and the
register page takes it within 15 minutes if it is for that register and whole: anything unknown or out of bounds
refuses the whole code. The register keeps it and reads it again at every opening. **Without a pairing, or when the
shop's Lightning address is no longer the one in its pairing, a staff register takes no payment** and says to ask the
owner. To pair it again, after new settings or a new address ([Changing the Lightning address](address-change.md)),
the owner makes a new code and the person scans it with the same phone. A new code does not take the register away
from a phone that has it: for that, turn it off.

## Turning one off

«Turn off «Anna»» in the list stops that register at once, and only that one: no new payment starts from it, and its
code, if still waiting, opens nothing. Do it when a person leaves or a phone is lost.

For **24 hours** its page still opens with its key, so that nothing is hidden from the person at the counter:

- a sale already on its screen goes on to its end, and says how it ended;
- the page says that the owner turned the register off, and to download today the encrypted copy of its sales
  («History and end of shift») and give it to the owner;
- the copy of the page kept on the phone for when the site does not answer is removed.

After that the key opens only a page that says the register is turned off (HTTP 410). A register turned off does not
come back: the owner adds a new one.

## What the staff reads at the first opening

When the community has a `training` block ([The merchant's training](merchant-training.md)), a staff register opens the
first time on **three rules of the counter**, before the keypad:

1. A sale is paid only when the register says so. The customer's phone and a screenshot do not count.
2. Never give the recovery words of a wallet to anyone.
3. If in doubt, call the owner.

These are the software's; a community writes its own in `training.staff_rules`. The person presses «I understood», or
«Practise first, without money» ([The practice register](practice.md)). A register that accepted them once is never
stopped for them again, even when the text changes: it could be in the middle of a sale. The staff registers of a shop
also follow their owner's pass: while the owner's register is locked for the training, so are they.

In the register's menu, «Something is not right?» answers the questions of the counter (did the customer pay, a wrong
amount, the phone switched off, a customer who paid twice), and the menu has the practice and «History and end of
shift».

## What it does not do

- A staff register cannot be limited to an amount or to hours, and it does not sign in: whoever holds the phone, or
  its address with the key, takes payments for the shop until the owner turns it off.
- Its pairing holds the shop's Lightning address, which the phone needs to ask for invoices without the site. Turning
  the register off does not take that address back ([Limitations](../limitations.md)).
- Its sales reach the owner in the copy the person downloads at the end of the shift, or on Nostr as they happen:
  never through the site.

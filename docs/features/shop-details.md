# What customers see of a shop

The site keeps a shop's details in its database, and the owner chooses which of them customers see. The association
writes or loads them; nobody but the owner changes what is shown for a shop that has an owner.

## The details and the choices

| Detail | Starts as | Where it shows when it is on |
|---|---|---|
| Name | always shown, not a choice | everywhere |
| Website | shown (if the shop has one) | the shop page, the map's popup, `/shops.json` |
| Address | shown | the shop page, the map's popup and list, `/shops.json`, the Nostr event's `location` |
| Phone | hidden | the shop page only |
| Email | hidden | the shop page only |
| Business name | hidden | the shop page only |
| VAT number | hidden | the shop page only |

- The business name (`legal_name`, the name on the invoices) and the VAT number (`vat_number`, the *partita IVA* or the
  number of another country of the EU) are new. The owner writes them in the shop's form, under «Business details»; the
  association in `/admin/shops/shop/`.
- A detail that is **hidden is not there at all**: not empty, not in the page's source, not a key of `/shops.json`. A
  detail that is shown but empty is no line.
- The map and its list, `/shops.json` and the Nostr events never carry the phone, the email, the business name or the
  VAT number, shown or not: a list of every shop's contacts, ready to download, is not what a customer needs. The page
  of the shop is where they are.
- **The pin stays.** Hiding the address hides the street, not the position: the pin is where the owner put it, and the
  Nostr event keeps its geohash. A shop that wants to be off the map altogether stays off it, as agreed with the
  community (`listed`), which only the association changes.
- A shop the association suspended keeps its page for the sign's QR code, with no contacts: the address (if shown) and
  nothing else. Printed sheets, the sign, the card for the till, the card for customers without a wallet and the TV in the
  window say the shop's name and town, never these details.
- A page of the community that writes `{{ shop.phone }}` (the featured shops of `community.yml` pages) gets an empty text
  for a hidden detail. The shop it reads is made inert: texts become entities, numbers and ids stay, and the owner, the
  registers or the shop behind it are out of reach (`{{ shop.shop.phone }}` is empty too).

## The owner's page

`/shops/preview/<shop>/`, «What customers see», from the panel and from the edit form. One tick for each detail,
with its value beside it, and the shop page as it will be on the right (below on a phone). It works without
JavaScript: the preview changes when the choices are saved. With JavaScript (`static/details.js`) it changes as the
boxes are ticked. A tick for an empty detail is kept for when the owner writes it, and the page says so on that line:
the box is ticked, so as soon as the detail is written, customers see it. Only the six ticks are saved, and
only by the shop's owner; the shop's form does not carry them, so saving the details never switches a tick off.

## The VAT number

Kept without spaces, dots, dashes or slashes, without control or invisible characters (a null byte, a zero-width space,
the marks that reverse a line), in capitals: `it 012.345.678-97` is `IT01234567897`. The length is checked on the tidy
number, in the owner's form and in `/admin/` alike (a capital `ß` is two letters).
The check is light and **never blocks**: a number of a strange shape is saved as written, and the owner is told once
(«We saved your VAT number, but it may have a mistake») with what to look at; the association decides.

- Italy: `IT` and eleven digits, the last one checks the others (the Luhn sum). A bare number of eleven digits that
  passes is taken for Italian.
- Another country of the EU (Greece is `EL`, Northern Ireland `XI`): its two letters, then two to twelve digits or
  letters.
- Other two letters, or a bare number that is not Italian: a doubt, with a sentence that says how to write it.
- Only the length is refused: more than 32 signs once tidied is not a VAT number, and the column would not hold it.

In `/admin/shops/shop/` the filter «VAT number» has «May have a mistake», and a save that wrote such a number warns (a
tick in the list does not repeat it). The list searches the business name and the number, also when the term is written
with dots, dashes or spaces.

## What the association does

- For a **shop that has an owner**, the ticks are read-only in `/admin/`: the owner decides. The association writes the
  business name and the VAT number only to correct a mistake the owner asked about.
- For a **shop with no owner** (loaded from a list, or moved and not claimed yet), the ticks are editable, and two
  actions on the list, «Show the phone and the email…» and «Hide the phone and the email…», work on the selected shops
  without an owner and leave the others alone. They need the right to change shops: an account that can only look at them is
  not offered them, and a post that names one does nothing (the same for the two actions on the certificate). Such a shop
  starts with the phone and the email hidden.
- `import_shops` takes `legal_name` and `vat_number` for shops no owner has taken, with the checks of the other fields
  (a number that looks wrong goes in as written, with a generic word in the output: never the number, nor the letters it
  starts with; the same for `import_moved_shops`). It does not set the ticks.
- `manage.py publish_nostr` replaces the event of a shop that hides its address: the next run says the town. A relay may
  keep older copies of an event: that is the relay's, not the site's.

## Existing shops

Shops made before these choices started where the pages were. Before them the phone and the email of
a shop were on its page for everybody, because the owner had written them as public. A **shop with an owner** keeps the
phone and the email on show when they are filled; a **shop with no owner** takes the starting values, which hide them.
Everything else starts as the table says.

## Privacy

The business name and the VAT number go to the association's database, the owner's screens, the move file between two
installations and the shop page when the owner turns them on. They are never in `diagnose_shop`'s text or JSON (and a
shop is not found by them), in a log line, in a Nostr event, in the output of `import_shops`, `export_shops` or
`import_moved_shops` (the notes say «may have a mistake», never the number), or in the record of the changes made to an
owner's account. `shops/test_details.py` walks the public outputs (the shop page in every language, the map, the JSON,
the Nostr event, the printed sheets, the TV, the pages of the community) with every detail hidden, and the sources (Python,
templates, texts, the scripts of `static/`) for a page that reads `x.phone`, `x.email`, `x.legal_name`, `x.vat_number`,
`x.address` or `x.website` on anything that is not a known non-shop receiver. `shops/test_details_walk.py` walks every
route of the site, as a stranger and as the owner, with a shop whose details are hidden and written as words nobody else
uses: a stranger finds none on any route, and the owner only on the two pages that are the owner's own.

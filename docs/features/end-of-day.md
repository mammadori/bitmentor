# History and day closing

What every shopkeeper knows from the till: at the end of the day, the closing. In bitmentor it is worked out **on the
phone**, from the register's own history. The site never receives a sale, an amount or a count
([What the site keeps](../../README.md#what-the-site-keeps)).

In short, for organisers:

- The history of the sales lives in the phone's browser. Lose the phone without a copy, and the history is gone; the
  money is not, it is in the wallet.
- The merchant keeps copies: a CSV for the accountant, and an encrypted file to bring the history to a new phone or to
  hand a staff register's sales to the owner. With the owner's Nostr key, also an encrypted copy on a relay
  ([Copies on Nostr](nostr-copies.md)).
- The register reminds the merchant to make a copy, every week until they choose another rhythm.
- A sale is paid when the shop's provider proves it (LUD-21 `verify`), or when the merchant confirms it by hand after
  looking in the wallet.

## The history on the phone

Every sale the register takes stays in the phone's storage for the site (`localStorage`), with its amount, the sats,
the exchange rate with the sources it came from (which ones, when, with what value), how it ended and, on a staff
register, the register's name. A finished sale keeps only that, a few hundred bytes; the invoice and its QR code are kept
only while the sale waits. When the phone has no more room the register says so.

From the register's menu (the round button at the top of the keypad), **History and day closing** (on a staff register: **History and end of shift**) opens:

- **The day closing.** For the chosen day: what was collected, by method and currency, in money and sats; how many of
  those the merchant marked as paid by hand (a wallet without LUD-21 `verify`) and how many a staff register reported, to
  check in the wallet, and how many of its Lightning sales this register checked itself with the payment provider
  (counted apart: no need to look in the wallet); how many were paid after the merchant cancelled them, to settle with the customer; the sales still
  waiting for a payment or a confirmation, those not checked and those not paid (expired or cancelled), only counted;
  the list of the day's sales, where a sale marked as paid by hand on a staff register says which one. **Close the day** (on a staff register **End the shift**) writes down when the day was closed.
- **The copies.** The CSV for the accountant, for one month or all, and the full encrypted copy (below). With the
  owner's Nostr key, also a copy on Nostr and the staff registers' sales as they happen ([Copies on Nostr](nostr-copies.md)).
- **The reminder.** The merchant chooses how often to be reminded of a copy: never (by hand), every day, every week,
  every month or at every closing; every week until they choose. The date of the last copy is at the top of the history,
  and when it is older than the choice the reminder shows in the register's menu too, with a dot on its button.

The first time the register opens it says, in one sentence, that the sales stay only on the phone and in the copies,
and that losing the phone without a copy loses the history but not the money. The same sentence stays in the history.
After a tap the register asks the browser to keep the site's data (`navigator.storage.persist()`), and says so when the
browser does not promise it.

## For developers

The code is in `static/register.js` and `static/register-book.js`.

### Paid after Cancel

A customer may pay just after the merchant pressed Cancel. While the invoice of a cancelled Lightning sale lasts, the
register keeps what it needs to look again, at opening and every minute; after its end, until the provider answers that
it was not paid (a provider that does not answer at the last look may still tell a payment made in time), at most a
day. On chain, for an address of its own, it looks for a week: every minute on the sale's day, then every ten minutes,
since a payment may wait days in the mempool for its confirmation. A copy where the sale was still waiting does not stop
these looks.

A Lightning sale that ended «not checked» because the provider did not answer when its invoice ended is looked at in
the same way, every minute for a day after its end: paid, it becomes paid (not marked, nobody cancelled it); not paid,
expired; still no answer after the day, it stays «not checked», to look for in the wallet. If the payment arrives, the sale becomes paid, marked «paid after it was cancelled» in the history, the closing
and the CSV: the money is in the wallet, and the merchant settles with the customer (the goods, or a refund).

### How a Lightning payment is proven

The register asks the shop's provider whether the invoice is paid (LUD-21 `verify`). «Paid» counts only with proof that
the payment is this sale's: the invoice in the provider's answer is the sale's own, or the answer carries a preimage
whose SHA-256 is the sale's payment hash. An answer about another invoice, or with a preimage of another hash, is not a
payment. When neither can be checked (no invoice in the answer and no WebCrypto to hash the preimage, on a page that is
not secure), or the provider has no `verify` at all, the register does not say «paid»: the merchant looks in the wallet
and confirms by hand, and the history counts those sales apart. Before showing an invoice the register also checks its
amount, its network, its expiry and its age (a provider or a cache answering again with an old invoice, maybe already
paid, is refused), that it has one payment hash and one expiry only (a wallet could pay another hash than the one the
register checks), and that no other sale on the phone has it.

### The phone's clock

The register page carries the site's time. When the phone's clock is more than half a minute off, the register keeps the
difference and uses the site's time for what is compared with the outside: an invoice's age and expiry, a sale's end,
the age of an exchange rate. A copy of the page kept for when the site does not answer uses the difference measured
before. With no difference measured before, a page that would set the clock back by more than 15 minutes is not trusted
(it may be a copy saved days ago): the phone's own clock is used. The moment of a sale, and so its day, stays the phone's own clock, as the merchant sees it.

### Which day a sale is in

The day it was started at the till, on the phone's clock. Until 05:00 a sale is still in the day before, so a shift
that passes midnight closes its own day (as `SHIFT_NIGHT_HOUR` in `shops/business_day.py`). A day is a calendar day,
not 24 hours.

### The CSV for the accountant

One row per sale, in time order, no total. The columns: date and time, business day, amount, currency, status, payment
method, sats, exchange rate, how the rate was chosen, the rate's sources with their values and times, the payment hash,
the staff register, the register that reported the sale (those are to check in the wallet), the discount for paying in bitcoin with the price before it, the on-chain address and transaction,
and the sale's ID. The file has a BOM for Excel, and `;` with decimal commas where the
page's language writes decimals with a comma, `,` and points elsewhere. A cell that starts like a formula gets a `'`.

### The encrypted copy

A JSON file protected by a passphrase of at least 10 characters, to bring the history back on a new phone or to hand a
staff register's sales to the owner at the end of a shift. The owner imports it in their own register: the sales
already there stay, and each sale is counted once (for a sale in both, the copy that knows more about how it ended:
only its state moves forward, with the transaction when the phone has none; the amount, the sats, the method and the
address stay the phone's). A sale still waiting in the copy comes in as «not checked», and never turns a sale still
waiting on the phone into one: the phone goes on checking it. A sale that was only started in the copy («waiting for
payment») never takes the place of one that ended on the phone. A sale that came in still waiting is replaced by the next
copy of the same register that says how it ended (a staff register's sale cancelled after the first copy, say), and
only by a copy that speaks for that register: the staff register's own, or this register's own copy taken as its own.
A file without an author or a stranger's copy on Nostr never ends it, and it stays to check in the wallet. Nor does such
a copy (one not taken as this register's own) change any sale of this register already on the phone: one that expired,
or is to check in the wallet, never becomes «paid» from it; it is left out. The import says how many were left out, and to tick that the file is a copy of this register when it is, and only if the owner made it on this register. A sale
another register reported stays reported in the owner's own copy too: brought back on a new phone, it is still to check
in the wallet. The day closings come back only with a register's own copy.

The file does not say who wrote it: whoever knows a staff register's passphrase could write one that says it is the
owner's. So a copy that says it is of this register counts as its own, its sales not reported and its day closings
taken, only on a phone with no sales yet (a new phone bringing its history back) or when the merchant ticks «It is a
copy of this register» in the import form. Otherwise its sales come in as reported, to check in the wallet.

What comes from a copy is checked as coming from elsewhere: only whole cents from 0 and whole sats within the 21
million bitcoin; a sale the merchant confirmed by hand never becomes «paid» from a copy; a sale of another register (a
staff register's) stays marked **«reported»** by it, and the day closing counts those apart.

A staff register's **Lightning** sale is the exception that the owner's register can settle by itself. The sale carries
the payment hash and the provider's `verify` URL (LUD-21; a staff register keeps the URL of its finished sales for this).
The owner's register, when the sale comes in from a file or from Nostr and then every minute while the page is open, calls
that URL (never another host than the shop's own Lightning address: a forged copy cannot point elsewhere) and marks the
sale **«checked with the provider»** only when the provider says it is settled **and** its answer carries the invoice, whose
payment hash is the sale's and whose amount is the sale's sats (a preimage, if given, must hash to it). The day closing
counts those apart from the ones still to check, and in the CSV they have no «Reported by». What stays **«reported»**: a
sale marked as paid by hand, one without a `verify` URL (a wallet without LUD-21), a
provider that gives no invoice or another, one whose hash another sale in the book has too (one payment cannot confirm
two sales), and any sale the provider did not confirm. A copy alone never makes a sale checked: the mark is set only by
that call and is never taken from a file or from Nostr. The register asks about at most eight sales at a time, one after
the other, and a sale that was not confirmed waits ten minutes before it is asked again, three times at most until the
page is opened again; a confirmed one is kept on the phone and not asked again. This needs no relay: it works the same
for the encrypted file at the end of the shift. On-chain sales are not covered: only the wallet shows them. Another register's copy
speaks only for that register: a sale in it that says it is of the register importing it is left out. The same rules
apply to the [copies on Nostr](nostr-copies.md), where the owner's phone also knows which key writes for which
register.

The format, version 1:

```json
{"format": "bitmentor-register-copy", "version": 1, "kdf": "PBKDF2-SHA256", "iterations": 600000,
 "cipher": "AES-256-GCM", "salt": "<16 bytes, base64>", "iv": "<12 bytes, base64>", "data": "<base64>"}
```

The key is PBKDF2 with SHA-256 over the passphrase (Unicode NFC, UTF-8) and the salt. The data is AES-256-GCM, with the
JSON array `[format, version, kdf, iterations, cipher, salt, iv]` as additional data, so a file whose header was changed
does not open. Inside: `{"sales": [...], "closings": {"YYYY-MM-DD": ms}, "till": ..., "who": ..., "exported": ms}`.
`shops/test_register_book.py` opens a copy made by the register with Python, so the format stays readable.

### Limits

- The history is as safe as the phone and its copies. The site cannot give back a lost history.
- Whoever holds the phone can change its page: the owner compares a staff register's history with their wallet.
- The browser's storage for a site holds some megabytes: years of sales for a small shop, but not forever. Keep copies.
- The practice register keeps its pretend sales apart, and «Start the practice again» clears them. Its copies are
  named as the practice's, and the real register does not import them; a pretend sale is left out of a real register
  whatever way it comes in (inside a real register's file, a pull from Nostr, a copy of a key the phone does not know).

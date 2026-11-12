# Moving the shops to another site

A mainnet site is a new installation. Moving shops is the exception: when merchants already signed up on another site
(a signet trial, or another bitmentor), they move to the new one without starting over.

```sh
# On the old site: writes the file (readable by its owner only, never over an existing file)
docker compose exec web python manage.py export_shops /tmp/shops.json
# On the new site: a dry run first, then the real thing, with the list of owners to tell
docker compose exec web python manage.py import_moved_shops /tmp/shops.json
docker compose exec web python manage.py import_moved_shops /tmp/shops.json --apply --owners /tmp/owners.csv
# One owner lost the link? A new one for that shop only (its id here, or in the file); the old link stops working
docker compose exec web python manage.py import_moved_shops /tmp/shops.json --only <id> --apply --new-codes --owners /tmp/owners2.csv
# The whole list went lost? New links for every shop still waiting
docker compose exec web python manage.py import_moved_shops /tmp/shops.json --apply --new-codes --owners /tmp/owners3.csv
```

- **What moves**: the shop's details (name, description, address, position, city, website, email and phone, business
  name and VAT number, discount, Nostr key and name, Telegram chat for the shop's notices), what its owner chose to show of them
  ([What customers see of a shop](shop-details.md)), what the association decided (listed, suspended, historic,
  external id), and how to reach the owner: the recovery email and whether they
  sign in with a wallet. Not the wallet's login key: LNURL-auth gives each domain its own key, so on the new site the
  same wallet signs in as someone else. Staff accounts sign in with a password, which never travels: their shops arrive
  without an owner.
- **What never moves**: the Lightning address and what the guardian found, the first day's ticks, the certificate
  (earned on one network, it starts again on the other), passwords, sessions, secrets. A historic shop moves without a register to wait for. There are no photos or opening hours to move: the shop has
  none.
- **The file** is JSON, `format: bitmentor-shops`, `version: 2`; a version 1 file, which has no business details, is
  read too, and its shops start with these choices: the phone and the email shown for a shop that had an
  owner. A file of another version, with a key more or less, a
  value of the wrong type, the same key twice, `NaN` or a number too large, or larger than 20 MB is refused whole:
  nothing is written. It holds the owners' emails and the shops' business names and VAT numbers: keep it private and delete it after the import.
- **The import** is a dry run unless `--apply`, in one transaction: the dry run is the same import rolled back, so it
  reports exactly what `--apply` will do. Each shop goes through the rules of the shop form and of the model: a shop
  that fails them is not moved, with the reason. Running it twice changes nothing: a shop already moved is found by
  its id on the old site. A shop set up here (with an owner and a Lightning address) is never overwritten; an
  unclaimed directory shop with the same external id takes the moved details, but stays suspended or off the map if
  it was here. A Nostr name taken or reserved here is left out, with a note.
- **Owners**: a file never gives a shop to an account that already exists here, and never writes an email on one (a
  changed file would otherwise hand over shops, or whole accounts through the email recovery). When the owner's email
  has no account here, a new account is made for it and the shop is in it: only whoever reads that email can sign in.
  Every other owned shop (wallet-only owners, emails that already have an account) arrives without an owner and with
  a **one-time link**, `/shops/claim/?code=…` (in the query, like the sign-in links, so access logs do not keep
  it): the owner signs in here, with the wallet or the email, opens it and
  confirms. Two shops of the same new email go to the one account the import made. Only the SHA-256 of the code is
  kept, and it is emptied as soon as the shop has an owner, also one set from `/admin/`; the link works once, never for staff accounts, with a limit of tries.
  `--apply` needs `--owners`, since the links are written only there; `--new-codes` gives new links to the shops that
  still wait, and the old ones stop working. `--only <id>` takes one shop of the file and leaves the others alone:
  with `--new-codes`, a new link for that shop only, under the same lock (an owner taking the shop meanwhile keeps
  it), in a list with the same row a full run writes. The id is the shop's here, as `/admin/` and
  [the dashboard](dashboard.md) show it, or its id in the file; a shop not in the file, or born here, is refused
  before anything is written.
- **A new id**: the id opens the register, and a register link given out on the old site must not open the new one.
- **The merchant** signs in as before and finds the shop with a notice, "Your shop has arrived here", and a button to
  connect the wallet (`/<lang>/shops/connect/<id>/`, the same guide as for
  ["A merchant without a wallet"](shop-page.md#a-merchant-without-a-wallet)): the same
  checks as a new shop, for the Lightning address only, and only while the shop has none
  and is neither suspended nor historic. Until then a shop that had a register stays off the map. Then the usual path:
  print the new sign, share the new register link.
- **`--owners owners.csv`**: one row per moved shop with an owner: name, email, how they sign in, what to do, the
  one-time link, and the message to send them, in the site's language (`--language` for another). It is opened before
  the import (never over an existing file, readable by its owner only): it holds emails and links, keep it private.

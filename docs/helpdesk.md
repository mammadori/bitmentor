# When a merchant calls

A merchant phones the association: "the register does not work", "the customer paid but it says waiting". Whoever
answers does not need to know Lightning: one command tells, in plain words, what matters about
that shop's register, what is wrong and what to say.

To practise before the first call, on made-up shops in every state, see [A training site in five minutes](features/demo.md).

```bash
docker compose exec web python manage.py diagnose_shop "<shop>"
```

`<shop>` is the shop's id (the long code in the register link), the owner's recovery email or the shop's email (also when the owner hides it from customers),
the number in the imported list, a piece of the name, or the id the shop had on the site it moved from (the code in
the old register link). If more than one shop matches, the command lists them and stops: run it again with the id.

Every line has a sign, the sentence to read to the merchant, below it, when there is something for you to do, an arrow
(`→`) with what you do, and, in brackets, the code and the detail for you:

```text
✗ Your Lightning wallet is not answering: the register cannot show the Lightning payment code. Open the wallet app and check that it works.
    [lightning_no_answer] bar@wallet.example; 4.2 s: LNURL-pay metadata or invoice not obtained
! There is no email to get back in: ...
    [owner_no_recovery_email] signs in with: wallet key 02ab12cd…; last login 2026-10-30 18:02 UTC; no recovery email
! These registers wait for the person to scan their code: Anna. ...
    → Nothing is broken: the person scans the code with their phone before it expires. ...
    [staff_code_waiting] Anna made 2026-10-31 09:12 UTC, code valid until 2026-11-01 09:12 UTC
```

`✗` is a problem, `!` something to know, `✓` fine. Problems come first. The sentences are in the community's first
language (`languages` in `community.yml`); `--language it` picks another one, `--json` gives the same for a program
(`code`, `level`, `say`, `detail` and `do`, the arrow's text; empty when there is nothing for you to do).
The exit code is 0 when there is no problem, 1 when there is at least one, 2 when no single shop was found.

A community can turn some features off (`features` in `community.yml`: [Turning features off](features/README.md#turning-features-off)).
Then the lines about the certificate, the closures and the practice register do not come. If a merchant speaks of a
button you cannot find, `manage.py preflight` says which features are off.

The command never changes the database or the shop: it only reads. The lines that come from what the site keeps about
the shop (the closures, the staff registers, a moved shop, a changed account, the certificate, the customers' guide,
the training) read the database only, through the same functions the panel and the dashboard use, and they come even
when the slow check ran out of time. One thing is not a pure read: the Lightning check asks the merchant's provider for
a real invoice, as the register does, which nobody pays (the merchant may see it in the wallet as unpaid or expired).

The site keeps no sales: the register's history is on the merchant's phone ([History and day closing](features/end-of-day.md);
the one exception is in [What the site keeps](../README.md#what-the-site-keeps)).
No line of the command, and nothing on `/admin/`, says whether a sale was paid: the merchant's wallet does. It takes at most about 40 seconds (`--seconds` to change it); a
check that does not finish in time says so (`time_up`): run it again.

## When the merchant asked from the panel

A merchant who presses **Something wrong?** in the panel ([Something wrong?](features/help.md)) has already seen, in
their own words, what the site knows about their shop and the steps to take, and has chosen a symptom. If that was not
enough, they asked for help, and you start from their request, not from a blank phone call:

1. **Where it arrives.** A message in `TELEGRAM_MODERATION_CHAT` (shop, symptom, how they want to be called back, their
   line, the codes below), and the request on top of `/admin/today/` until someone marks it solved. Without Telegram,
   or when Telegram refuses, only the dashboard: the merchant has been told so, and given the community's email.
2. **Read the request** (the link in the message, or `/admin/shops/helprequest/`): the symptom, the line, **where to call
   back** (the recovery email, the shop's Telegram chat or its phone, as the merchant chose; «I will look at my panel»
   means they wait for «Solved»), and the diagnosis made at the moment they asked, line by line, with the code of each in
   brackets and its detail for you. The codes in the Telegram message are the ones in the table below and in the calls
   after it, plus `address_broken` and `address_works`, which come from the guardian of the Lightning address.
3. **Run `diagnose_shop`** if you need what has changed since, or the checks that the request could not finish in time (a
   line `time_up` says so).
4. **Call back** by the way they chose, with the steps of the matching call below, and mark the request **solved** in
   `/admin/` (the action «Mark as solved»: it keeps who and when). The merchant sees «Solved» in their panel.

A merchant asks for help at most three times a day (`limits.help_requests_per_shop_day`); if you see the same shop again, that is the signal to call, not to wait.

A moderator has the permissions on the requests (`view_helprequest`, `change_helprequest`) through the **Moderators** group,
which `manage.py moderators add <name>` gives ([The moderators](operations.md#the-moderators)); if the link to a request
does not open, ask whoever runs the site to add you. The same group does not allow what changes a shop: marking it historic,
writing a closure, revoking a certificate, giving a moved shop to its owner. Where the calls below
say to do one of these in `/admin/`, ask whoever has all the rights.

## Before the phone rings

`/admin/today/`, linked from the top of `/admin/`, lists what to do about every register, the most urgent first. Each
line says the shop, what happened in plain words, since when, what to do now, what to say to the merchant (after
"To the merchant:"), and, when it helps, the `diagnose_shop` command for that shop. On top: how many registers take
payments, how many shops to look at, how many are closed for a while. The same list in text:
`docker compose exec web python manage.py dashboard` (`--all` adds the things to know; exit code 2 when there is something to do).

| Code | Level | What it means, and where to go on |
|---|---|---|
| `help_request` | Problem, on top | A merchant asked for help from the panel and nobody has marked it solved: see [When the merchant asked from the panel](#when-the-merchant-asked-from-the-panel) |
| `lightning_no_answer` | Problem | The Lightning address stopped answering, or never answered: see call 2 |
| `lightning_no_confirmation` | Problem | The address receives, but does not tell the register: see call 1 |
| `lightning_shared` | Problem | The shop's Lightning address is also on a shop of another owner: find out whose wallet it is; until then neither shop is certified |
| `limit_<name>` | Problem at the limit, to look at near it | A total of the site (shops, requests, emails…) is at 80% of its limit or at it: clear what nobody needs, or raise `limits.<name>` in `community.yml`. Spam or growth: look before raising |
| `private_waiting` | To look at | A certified shop waits for a person to open the private part to its owner ([Certified shops](features/certified-shops.md)) |
| `emails_one_address` | To look at | Some addresses received more emails today than `limits.emails_per_address_day`: a shop in trouble, or someone using the sign-in against an address |
| `guardian_late` | To look at | `check_lightning` has not run for a while: the Lightning lines may be old |
| `osm_request` | To look at, on top | The merchant asked the association to put the shop on OpenStreetMap ([OpenStreetMap](features/openstreetmap.md)) |
| `closed_lightning` | To know | A shop closed for a while whose Lightning address does not work: nobody is told until it opens again |
| `closed_long` | To know | A shop closed for a while for over three months: ask whether it is coming back, or mark it as historic (the icons forgive at most 92 closed days in a year) |
| `no_position` | To know | Not on the map: the merchant adds the address in the shop page |
| `moved_unclaimed`, `moved_no_owner`, `moved_no_wallet` | To know | A moved shop waiting for its owner (send again the link from the list of owners; if it is lost, the line's command remakes it for that shop only, with `--only <id> --new-codes`), for an owner (assign it here once they have an account), or for its wallet: [Moving the shops to another site](features/moving-shops.md) |
| `osm_failed`, `osm_late`, `osm_links`, `osm_contacts`, `osm_proposals` | To know | The monthly reading of OpenStreetMap, and what waits for a person ([OpenStreetMap](features/openstreetmap.md)) |

The page only reads the database. For what the merchant's provider says now, `diagnose_shop`, one shop at a time.

## Never

- **Never change where a shop's money goes on the merchant's behalf**: not the Lightning address, not in `/admin/`.
  Only the merchant, signed in, sets up where they get paid; an on-chain key (where the community turned on-chain on)
  is only on their phone and the site never sees it. Someone on the phone who asks you to "just put my new address" may not be the merchant.
- **Never recommend an exchange or a custodial wallet**, not even "just for now". Only open-source, non-custodial,
  free tools, like those on the wallet page the community chose.
- Never ask for a seed, a private key or a password. The site never needs them.
- Never tell a merchant a sale is paid because the register or the customer says so: what counts is what arrived in
  their wallet.

## The calls

### 1. "The customer paid, but the register says waiting"

Ask: how long ago? What does the customer's wallet say (sent, pending, failed)? Where the community turned on-chain on
(it is experimental, and off by default): Lightning or on-chain?

Look at:

- `lightning_no_confirmation`: the merchant's wallet receives, but does not tell the register when a payment arrives
  (no LUD-21 `verify`). The money is there. Say: "Check the payment in your wallet app: if it is there, the sale is
  paid." For the future the merchant needs a wallet with a Lightning address that confirms payments.
- `lightning_slow`: the wallet answers too slowly; the register keeps asking. Say: "Check the payment in your wallet app."
- On-chain (only where the community turned it on): the register on the phone asks a public block explorer, and waits for a confirmation above the amount the
  merchant chose ([On-chain at the register](features/onchain.md)). A payment with a low fee arrives late, on its own: the
  customer must not pay again.
- `lightning_ok` and still waiting: the problem is on the phone (no connection, the page left open for hours). Ask to
  check the payment in the wallet app, then reload the register.

Do not: create a new sale for the same purchase until you know where the first payment went.

### 2. "The QR code does not come out"

Ask: what does the register show (an error, a blank page, a spinner)? Where on-chain is turned on: Lightning only,
on-chain only, or both?

Look at:

- `lightning_no_answer`, `lightning_slow`: the merchant's Lightning wallet does not answer. Say: "Open the wallet app
  and check that it works, then try again."
- `register_unavailable`: the register cannot take payments; the detail says why (the community lists fewer than two
  price sources, or the saved address cannot be used). Tell whoever runs the site.
- `training_locked`: the register waits for the owner's basic pass: see call 12.
- The command says nothing wrong: the problem is on the phone. Ask to reload the register page, or to open it in
  another browser. A register that cannot read the bitcoin price from enough sources says so on its screen.

### 3. "The customer paid too much", "paid only part" or "wants the money back"

The site keeps no sales and cannot see these: the register's history on the phone has them, and the merchant's wallet
has the money. Say: "Open the history in your register: it shows the sale, how much was asked and what arrived." The
merchant gives money back from their own wallet, to an address the customer gives them, and notes it in the history.

Do not: make the refund yourself, or promise one in the association's name.

### 4. "I changed wallet"

Look at `lightning_ok` (or the `lightning_*` problems): its detail has the Lightning address the register asks now.
Read it to the merchant and ask them to compare it with their wallet.

- If it is the old wallet and the merchant can still open it, the money is there.
- The merchant changes the address in the shop's page, signed in: the site tries it before saving, and the account
  change is told to the owner. On-chain, where it is turned on, the key is changed in the register's settings, on the
  phone; the staff registers are paired again with a new code.

Do not: change the address yourself, in `/admin/` or anywhere else, even if the merchant asks.

### 5. "I cannot sign in any more"

Look at `owner_recovery_email` and `owner_no_recovery_email`. The detail says how the merchant signs in (wallet key or
email link), when they last did, and the recovery email masked (`m***@e***.org`).

- With a recovery email: "On the site, choose to sign in with email, and open the link we send you. It lasts 15
  minutes and works once." Ask them to look in the spam folder.
- Without one, and the wallet they signed in with is lost: the account cannot be recovered from here. The register
  still works on the phones that have it, and the money still arrives in their wallet. Tell whoever runs the site:
  giving someone access to a shop is a decision to take in person, never on the phone.
- `owner_is_staff`: a staff account signs in only from `/admin/`, and does not open the pages of a shop, not even its own
  ([Staff and shops kept apart](features/staff-and-shops.md)). The shop needs an ordinary account, the one the owner
  signs in with by wallet or email.

### 6. "The site does not answer"

The register on a phone that opened it before still opens, from its saved copy, and takes payments: it talks to the
shop's Lightning address, not to the site. Say: "Go on with the register; check each payment in your wallet app if the
register cannot." Then look at [The site is down](operations.md#the-site-is-down).

### 7. "I lost my phone" or "I changed phone"

The history of the register is on the phone, and in the copies the merchant downloaded (CSV and the encrypted copy, or
the copy on Nostr): see [History and day closing](features/end-of-day.md). The money is in the wallet, not in the phone's
register. On the new phone the merchant opens the register from the panel and imports the latest copy. Without a copy,
the history of that phone is lost; the money is not. A lost phone with a staff register: the owner turns that register
off in «Registers for your staff».

### 8. "I want to close" or "stop for a while"

Nothing to run. The merchant deletes the shop from its page: the register stops and the shop leaves the map. The money
already received stays in their wallet. For a shop that closed for good but should stay in the community's history,
the association can mark it as historic in `/admin/` (`shop_historic`; needs an administrator): it stays on the map with its own marker.
`shop_suspended` means the association took the shop off the map; the register still works.

For a while only (holidays, works, an illness): the merchant uses "Closing for a while?" in the panel, or, if they phone, the
association adds a closure under the shop in `/admin/` (needs an administrator; "Closures": the first day may be in the past, and the day it opens
again may be left empty). The shop stays on the map saying "Closed for holidays · back on …", the register keeps working, and
the dashboard says nothing about it until it opens again ([Closed for a while](features/vacation.md)); call 10 reads what the map says.

### 9. "My employee…" or "Anna's register does not work"

Look at the `staff_*` lines. The names are the ones the owner chose in "Registers for your staff"; the keys and the codes
are never shown.

- "…needs the register": the owner adds a register in «Registers for your staff» and the person scans its code. The
  register only receives money into the merchant's wallet: nobody can take money out of it.
- "…left and still has the register": the owner turns it off (`staff_off`). It cannot be turned on again; a new one is made.
- "…says a customer paid": what counts is the merchant's wallet, not the phone.
- `staff_code_waiting`: the register was made, and the code on its card still works: the person only has to scan it
  with their phone (it works once and lasts 24 hours). Nobody makes another code while this one is valid.
- `staff_unopened`: the register was never opened on a phone and its code is gone (used or expired). The owner opens
  "Registers for your staff", chooses the register, presses to make a new code, and the person scans it. The code is not
  read out on the phone, and not even you can see it.
- `staff_no_register`: the shop has no register of its own yet: every staff link answers "not found" until the wallet is
  connected (see call 13).
- `staff_ok` and still nothing: the register is fine on the site's side (the detail says when it was opened): the
  problem is the person's phone. Ask them to reload the register page.

### 10. "I am closed for holidays, but the map…"

Look at `closed_now`, `closed_soon`, `closed_planned`, `closed_hidden`, `closed_long` and `closed_ignored`. `closed_now`
reads the very notice the map and the shop page show ("Closed for holidays · back on 14 November"), so you can compare
it with what the merchant sees.

- The register keeps working, the shop stays on the map and no alarm goes off for the dashboard or the guardian while
  the shop is closed. Say: "Your register works, nothing to do."
- "The map does not say I am closed": a closure still to come is shown only 7 days before it starts (`closed_planned`
  says when). A shop marked historic ignores its closures (`closed_ignored`); a shop that is off the map has nobody to
  read the notice (`closed_hidden`: look at the `shop_` lines).
- "I am open again but the map still says closed": the merchant presses "I am back today" in the panel, or you remove or
  shorten the closure under the shop in `/admin/` ("Closures").
- `closed_long` (over three months) is the moment to ask whether the shop is coming back; if it closed for good
  you can mark it as historic in `/admin/`.

Do not: change the dates of a closure on the phone for someone you cannot recognise: the merchant does it in the panel.

### 11. "A customer has no wallet" or "does not know how to pay"

The merchant does not have to explain anything. In the panel, "Card for customers without a wallet" is a sheet to print
and keep at the till: the customer scans its code and the phone takes them through, in three short steps. A customer who
has a wallet reads "I have a wallet, but I'm not sure how to pay" on the shop page: what to scan, what to expect, what to
do when the wallet says error. Nothing is stored and nobody has to sign in: see
[The customer at the till](features/customer-path.md). If the merchant says the customer's list of wallets is empty or
wrong, the community's `shop_page.wallets` is the place (`manage.py check` says what it left out, `website.W008`).

Look at `customer_path_ok`, `customer_wallets_empty` and `customer_path_off`:

- `customer_wallets_empty`: no wallet of the community's list sends the way the shop gets paid, or the community listed
  none. Say: "Thank you, we are fixing the customers' list of wallets." The fix is in the community's
  `shop_page.wallets`, not in the shop.
- `customer_path_off`: the shop is suspended or historic, so the customer who scans the card lands on the shop page and
  not on the guide. It opens again when the shop takes payments.
- `practice_offer`: the owner has not ticked the first sale yet. The merchant (or whoever works with them) can learn
  the register with a pretend customer, nothing charged: "Practise without money" in the panel, which works even
  before the wallet is connected.

### 12. "The register says it opens soon"

Only where the community has the training and the site runs on mainnet. Look at `training_locked`: the owner has not
the basic pass, so the register page and new sales wait; a sale already
on a screen still ends. Nothing is broken and nothing has to be changed: tell the owner to open «Your merchant pass» in
the panel (a few short modules, no penalty for a wrong answer), and that the practice register works meanwhile.
`training_missing` says the register still works and the pass is missing; `training_done`, that it is there.
Never mark the pass for the merchant: it is earned on the site, and the declarations are theirs (see
[The merchant's training](features/merchant-training.md)).

### 13. "I cannot find my shop after the move" or "my shop is not on the map yet"

Search with what the merchant has: the name, the email, or the id in the register link of the old site (it works too).
Look at `moved_unclaimed`, `moved_no_owner`, `moved_no_wallet` and `shop_waiting_wallet`
([Moving the shops to another site](features/moving-shops.md)).

- `moved_unclaimed`: the shop arrived without an owner, with a one-time link. Say: "Open the link we sent you, sign in
  and take your shop." If the link is lost, the line's detail has the command that makes a new one for that shop only
  (`import_moved_shops <file> --only <id> --new-codes --apply --owners <new-list>.csv`): the old link stops working, the
  other shops keep theirs.
- `moved_no_owner`: nobody to give the shop to. Giving someone a shop is a decision to take in person, never on the
  phone; once the merchant has signed in here, an administrator assigns it to their account in `/admin/`, as the line says.
- `moved_no_wallet`, `shop_waiting_wallet`: the owner has the shop but no wallet is connected, so there is no register
  and a shop that had one stays off the map. Say: "Open your panel and choose Connect your wallet." The guide takes a
  few minutes. Meanwhile "Practise without money" works. You never type an address or a key for them.

### 14. "I got an email: someone changed my account"

Look at `account_changed`. It reads the register of `/admin/` ("Account changes"): the last 14 days of changes to the
account that owns the shop now (the 20 latest), with what changed, by which page
or command, by whom, and who was told (email, Telegram chat of the shop, the association's chat). The sentence is the one
the owner got in the email. It is a problem when the change touches who can sign in, who owns the shop, where its
Lightning payments go or who gets its notices (the sign-in name, the owner, the Lightning address, the shop's chat,
staff rights, deletion), a warning for the rest (password, switched on or off, groups).

- `account_changed_other`: a change that names this shop but was done to another account (the owner before it was
  handed over, an account deleted). It is a fact about the shop, said in the third person, and never a problem for
  the owner who has it now: do not read it to them as if it were theirs. If the person on the phone is the one who had
  the shop before, ask whether they know why.
- Ask whether they expected it (a password you reset at their request, a shop handed over after the move). If so, nothing
  to do.
- If not: change nothing and tell whoever runs the site, who can see in `/admin/` who did it. It is not for the phone to
  decide whether it was a mistake.

Do not: ask for a password or a code, or "fix" the account yourself.

### 15. "How far am I from the certificate?"

Look at the `certificate_*` lines. A shop is certified when its Lightning address works (the guardian's last check, on
the address it has now) and the owner has the basic pass of the training ([Certified shops](features/certified-shops.md)).

- `certificate_waiting` says which of the two is missing; the sentence under the arrow is the rule itself, to read.
- `certificate_ready`: both are there; the guardian's next check gives the certificate.
- `certificate_done`: certified, since the month it says.
- `certificate_revoked`: the association revoked it in `/admin/` (Shop list action "Revoke the certificate", done by
  an administrator); ask them before saying more.

## Every line

| Code | Sign | What it means |
|---|---|---|
| `shop_visible` | ✓ | The shop is on the map (not while it waits for its wallet) |
| `shop_suspended` | ! | Taken off maps and lists by the association; the register works |
| `shop_historic` | ! | Marked historic (`closed_on`) |
| `shop_unlisted` | ! | The owner chose not to be on the map |
| `shop_no_position` | ! | No coordinates: not on the map |
| `shop_no_owner` | ! | Imported, nobody claimed it: no register |
| `owner_recovery_email` | ✓ | The owner can sign in with an email link |
| `owner_no_recovery_email` | ! | Losing the wallet means losing the account |
| `owner_is_staff` | ! | A staff account owns the shop |
| `lightning_ok` | ✓ | The Lightning address answers and confirms payments (LUD-21) |
| `lightning_no_answer` | ✗ | No LNURL-pay answer or no invoice |
| `lightning_no_confirmation` | ✗ | Invoices, but no way to know they are paid |
| `lightning_slow` | ✗ | No verdict within 15 seconds |
| `register_ready` | ✓ | The register page can take payments: the address and the price sources it uses |
| `register_unavailable` | ✗ | The register cannot take payments, and why |
| `first_day_done` | ✓ | The first day is complete |
| `first_day_todo` | ! | The first day steps still to do |
| `shop_waiting_wallet` | ! | Saved before having a wallet: no register, off the map |
| `moved_unclaimed` | ! | Moved here, the one-time link not used yet |
| `moved_no_owner` | ! | Moved here with nobody to give it to |
| `moved_no_wallet` | ! | Moved here and taken, but no wallet connected |
| `closed_now` | ✓ | Closed for a while today: the notice of the map |
| `closed_soon`, `closed_planned` | ✓ | A closure still to come (shown 7 days before) |
| `closed_hidden` | ! | Closed, but the shop is off the map: nobody reads the notice |
| `closed_long` | ! | Closed for over three months |
| `closed_ignored` | ! | Closures saved on a historic shop |
| `staff_ok` | ✓ | Staff registers opened and working |
| `staff_code_waiting` | ! | Staff registers never opened, with a code that still works (24 hours, once) |
| `staff_unopened` | ! | Staff registers never opened on a phone, and the code is gone: make a new one |
| `staff_off` | ! | Staff registers turned off |
| `staff_no_register` | ✗ | Staff registers on a shop with no register of its own |
| `account_changed` | ! / ✗ | The account that owns the shop was changed from the administration in the last 14 days |
| `account_changed_other` | ! | A change that names this shop, done to another account (the owner before, one deleted) |
| `certificate_done` | ✓ | Certified since a month |
| `certificate_ready` | ✓ | Everything for the certificate; the guardian's next check gives it |
| `certificate_waiting` | ✓ | What the certificate still waits for |
| `certificate_revoked` | ! | Revoked by the association |
| `customer_path_ok` | ✓ | The customers' guide has wallets for the way the shop gets paid |
| `customer_wallets_empty` | ! | No wallet to suggest to a customer |
| `customer_path_off` | ! | Suspended or historic: the card leads to the shop page |
| `practice_offer` | ✓ | The first sale is not ticked yet: the practice register is there |
| `training_locked` | ! | The register waits for the owner's basic pass (mainnet, `gate: unlock`, from the `since` day if the community wrote one) |
| `training_missing` | ✓ | The owner has not done the training; the register still works |
| `training_done` | ✓ | The owner has the basic pass |
| `time_up` | ! | Some checks were skipped for lack of time (the lines from the database come all the same) |
| `check_failed` | ! | A check failed unexpectedly: the detail names the error, the log has it |

## For developers

`shops.diagnosis.diagnose(shop)` returns the same findings as data (`code`, `level`, `say`, `detail`, `do`), so a page
can show them; `find_shops(text)` is the search. The codes are stable: add new ones, do not rename them. A new check
that only reads the database goes in `LOCAL_CHECKS`, so that it comes even when the Lightning probe used up the time; it asks the
module that owns the rule (as `check_closure` asks `vacation`) and never copies it, and a test keeps it in step with
the dashboard where both say the same thing (`moving.waiting_for` and `dashboard.moved_items`).

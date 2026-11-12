# Closed for a while

A shop that closes for two weeks in August, for works, or for a bereavement or an illness should not look abandoned.
Without this, the map shows it open, its icons fade, the guardian of the Lightning address writes if the address stops answering, and a
customer arrives in front of a closed shutter. `closed_on` is something else: a shop that closed for good, kept on the
map as history ([Shop page](shop-page.md), [Features](README.md)).

**Can be turned off.** `features.vacation: false` in `community.yml` takes the card, the pages, the notice on the map and the welcome back away, and keeps the closures written (and the Closures table in `/admin/`): a closure with a day to open again keeps the alarms quiet until that day, and the association is told then of a Lightning address that broke meanwhile. See [Turning features off](README.md#turning-features-off).

A closure is a range of days of the community's calendar (`Closure`, `shops/vacation.py`): the first closed day, the day
the shop opens again (or none: "I open again when I say so"), and a reason from a list: holidays, building works,
personal reasons, other. Days, never instants: the hour, the time zone of whoever looks and the change of summer time
cannot move a day. There is one closure at a time to edit; the past ones stay, because the icons count
only the days the shop was open.

## Who says it, and how

- **The merchant**, in the panel (`/shops/`): on each shop, "Closing for a while?" opens a card with two ways: the
  page `/shops/break/<shop>/` (closed from a day, until a day or "until I say so", with the reason), or "Closed from
  today, until I say so" in one tap (reason: other). A closure to come can be changed or cancelled; one in course can be
  shortened, lengthened, or ended with "I am back today". The days are the community's: today and later for the first
  day, a day after it for the day the shop opens again, at most a year.
- **The association**, in `/admin/`, under the shop: the "Closures" table, with the first day free (a merchant phones to
  say they closed on Monday). The same rules apply from there.

## What the public sees

The shop stays on the map, in the counters and in the lists, with its own pin. It only says, kindly, "Closed for
holidays · back on 17 November 2026" (or "back soon" when no day is set), in the list, in the map card and on the shop
page. A closure still to come is said from 7 days before it starts ("Closed for holidays from 9 November · back on 23
November"). A historic shop (`closed_on`) says nothing of this. `/shops.json` has a `break` object (`state` "closed" or
"soon", `reason`, `from`, `back`, ISO dates, or `null`) and the Nostr event of the shop a `["break", reason, back]` tag
while it is closed (republished each time it changes, so the day the shop opens again the relays get it without the tag).
There is never a free text: the reason comes from a list, and "personal reasons" says nothing more.

## What does not change, and why

- **The register.** A closed shop can sell; if someone pays, it is paid. Nothing here touches the register, the sign,
  the staff registers or where the money goes. The site sees no sale either way.
- **The icons of the map.** The bands (week, month, year, older) count the days the shop was open: from the last day the
  Lightning address answered the guardian (`lightning_worked`) to today, minus the closed days among them. The forgiven
  days are a budget: of the last 365 days at most 92 closed ones are forgiven in all (the first ones in time), so a
  closure "until I say so", or closures one after the other, cannot keep the icons alive for ever: beyond the budget the
  shop looks quiet again.
- **The certificate.** The rule does not move for a closure: a closed shop is not asked less or more (a Lightning address
  that works and the owner's basic pass), and a certified shop stays certified (it is a fact of the past, only the
  association revokes it).
- **Alarms.** For a shop closed today the dashboard (`/admin/today/` and `manage.py dashboard`, so the
  morning message too) lists no problem with the Lightning address; on top, how many shops
  are closed for a while. The site sees no sale, so it cannot tell a shop that sells while it says it is closed:
  the faults kept quiet are still lines to know. Two lines to know, never in the morning
  message: `closed_lightning` (closed, and the address does not work: it is said when the shop opens again) and, after
  three months with closures one after the other counted as one, `closed_long`, to ask whether the shop is coming back.
  The path says "You are closed for
  now: there is nothing to check". The guardian ([The guardian of the Lightning addresses](lightning-guardian.md))
  keeps probing the address and the panel shows what it finds, but nobody is written to (not the merchant, not the
  association) while the shop is closed.

## When the shop opens again

From 8:00 of the community's morning of the day the shop opens again, `manage.py check_lightning` (the guardian's timer,
every hour) writes once to the merchant: "Welcome back! … try a small payment of your own", by email (the address to get
back in, as the guardian does) and on the shop's Telegram chat, whichever the merchant has; if the Lightning address does
not work, the same message says so (and asks for the small payment once it works again), and the association hears it
on Telegram. A merchant who ends the closure by hand ("I am back today", or the form with today as the day, or a closure
that began today and is cancelled) is not written to, since they are in the panel, but the association hears about a
fault of the address at that moment, whichever way the shop reopens. For two weeks the panel shows a "Welcome back" card on the shop; once the guardian finds
the Lightning address working on or after the day it opened again, the card says so, and the merchant closes it with
"Got it".

## Limits

- A closure is not carried by `export_shops` and `import_moved_shops`: a moved shop starts open.
- The card knows the address works again at the guardian's next probe, not at once.
- The message of the day it opens again is sent by the guardian's timer: without it nobody is written to (the card in
  the panel is there all the same).
- Only the owner closes from the panel; a staff register cannot.

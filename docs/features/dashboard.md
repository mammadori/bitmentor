# What to do today

Every morning, `/admin/today/` (linked from the top of `/admin/`) says what to do about all the registers at once, the
most urgent first: Lightning addresses that do not work and since when (or that take payments but do not tell the
register), shops without a position, moved shops still waiting for their owner or their wallet, shops closed for a
while for over three months, the requests for help and for OpenStreetMap, and the lines the community's own code adds
([`dashboard_items`](../architecture.md#the-communitys-own-code)). Each line has the shop,
what happened in plain words, since when, what to do now, the words for the merchant and the command to run. On top,
how many registers take payments, how many shops to look at, how many are closed for a while. It only reads the
database, with the same few queries for any number of shops, and works without JavaScript: what it knows of the
Lightning addresses comes from `check_lightning`, and when it has not run for a while the page says so. The site sees
no sale, so nothing here is about payments or takings. Staff in the
Moderators group (or with the permission to see shops) can open it ([The moderators](../operations.md#the-moderators)). `manage.py dashboard` prints the same list (`--all` with the things to know
too, `--language it`) and exits with 2 when there is something to do (1 is an error of the command).

**The morning message.** `bitmentor-dashboard@<project>.timer` (`deploy/dashboard.sh`, every day at 06:30 UTC) runs
`manage.py dashboard --send`. It writes only when the things to do (problems and things to look at, not the things to
know) changed since the last summary: something new, or something solved. Then one short message to
`TELEGRAM_MODERATION_CHAT`, or to the `email` of `community.yml` (to each address of `moderators_emails` instead, one
message each, when the community writes that key) when the site has no Telegram bot or chat, or when Telegram refuses it
(a wrong chat, a bot thrown out): the community and the site (with the network off mainnet, since
development and production may share a chat), the new things first, then those still to do, up to ten lines (sign,
shop name, what happened), how many were solved, and the link to `/admin/today/`. The same list a week after the last
summary goes again, as a reminder; nothing to do, no message. It never writes more than the shops' names: no owner,
email, address, amount or command, which stay on the page; a name cannot mention anyone (`@`) or start a bot command.
What was sent is kept in the database (`DashboardDigest`: codes and shops, no text), only once delivered. Exit 10
means a summary was sent, and the unit counts it as a success; 0, none was needed; 1, it could not be delivered: the
unit tries again every 10 minutes, four times, then stays failed and `check.sh` alerts
(`journalctl -u bitmentor-dashboard@<project>`).

**Requests for help.** A merchant who asked the association for help from the panel ([Something wrong?](help.md)) is a
line on top of everything else, whatever else is wrong, the oldest first, as a problem: the symptom, since when, the
line they wrote (on the page and in `manage.py dashboard`, never in the morning message, which names the shop and the
symptom), how they want to be called back, the command and the links to the request and to the shop. The line stays
until someone marks the request solved in `/admin/shops/helprequest/`; it does not depend on Telegram having
taken the message.

**OpenStreetMap.** A merchant who asked the association to do their entry on OpenStreetMap ("The association does it for
me", [OpenStreetMap](openstreetmap.md)) is a line after the requests for help: the shop, since when, what to do, the
link to the request in `/admin/shops/osmrequest/`, until someone marks it done there. For a community that uses
OpenStreetMap there is also a line to know while tags of its shops there are not what the site knows, for a person to
change by hand (where the community turned on `osm_upload`, an experimental command that is off by default, the two
lines name it: for an update, and for the changes that wait for a person to send them), one for the links proposed and one for the contacts offered that wait for the association to
confirm or accept them, and one when the last reading failed or none worked for 45 days. The site never sends them by
itself.

**New shops that wait for an approval.** Each is a line with an **Approve** button. A new shop is compared with every
other shop of another owner (approved, waiting, suspended or closed; a shop with no owner counts as another owner's,
and another shop of the same owner is a branch, never a signal), and what makes it look like a copy is on its line
(`shops/approval_signals.py`, computed when the page opens, stored nowhere). "The same area" is within 2 km when both
shops have a position, else the same town; for the very same name the same town counts too, whatever the pins (the
owner places the pin).

- `namesake`: the same name as a shop in the same area, or a name very like it within 300 m or with a contact in
  common. "Very like" is the threshold OpenStreetMap's reading uses by default to call two names the same place
  (`name_similarity` 0.85: a letter or two apart, or every word of a name of 4 letters or more inside the other);
  capitals, accents, punctuation and spaces do not count;
- `namesake_note`: a name very like it further away in the same area («Bar Mario» and «Bar Maria» a kilometre apart),
  or the same name in another town (a chain, perhaps). Only a quiet note: no box to tick;
- `same_contact`: the same website, the same phone (the digits, with or without the country, at least 7) or the same
  email. A website is its host, without `www.` or `m.`, in its ASCII form, whatever the path. On a host of many
  businesses (Facebook, Instagram, Linktree, Google in any country, Apple Maps, TripAdvisor, Booking, TheFork, Uber Eats,
  Deliveroo, Glovo, Just Eat, PagineGialle, Yelp…) it is the page, never the host alone; on a short link (bit.ly,
  tinyurl.com, amzn.to…) the link as written; on a site builder (WordPress.com, Blogspot, Shopify, Webnode, Jimdo,
  Altervista, Weebly, Squarespace, business.site, Wix, Canva, GitHub Pages, Netlify, Vercel, Cloudflare Pages) the
  shop's own subdomain, or the page on the builder's bare host. The line says which kind matched, never the
  value. A chain that gives every branch the same website matches too: the box is then quick to tick;
- `near`: with a namesake, how many metres apart, under 150 m;
- `other_lightning`: the other shop is paid at another Lightning address ("at another provider" when the domain
  differs), never the address itself;
- `new_owner`: the owner's account was opened less than 24 hours before the shop, has no recovery email and no other
  shop. Nearly every honest merchant signs up and writes the shop in the same minute, so this is never a signal alone.

A shop with a `namesake` or `same_contact` signal has a block of its own, "Check before approving", above its button,
its tag reads "Check first", and it comes first among the shops that wait. The block says what to check about each
other shop: ask its owner. A shop on the site without an owner cannot be taken by the merchant alone: if it is the same
business, give that shop to this owner in `/admin/`, or, for a shop moved from another site, send the merchant its
claim link ([Moving shops](moving-shops.md)), instead of approving a second one. A shop that closes on a later date is
still open: the new one may be its successor or a move, so ask its owner; a shop that closed already: check whether the
new one is its reopening. The button reads **Approve anyway** and works only with the box "I checked what the lines above say"
ticked; the server refuses it otherwise, and so do the admin action (which approves the others and says how many it
skipped) and `approval.approve()` without `confirmed=True` (which writes a warning in the log with how many it held).
When the signals cannot be computed (an error, written in the log), every waiting shop says "The checks could not be
run", asks whoever runs the site to look at the log, and is approved one by one with the box "The checks could not be
run: I looked at this shop myself" (its history says so); the admin action and `approval.approve()` without
`confirmed=True` approve nothing, and the action says why. The shop's history records the approval with the signals that were there ("Shop
approved; signals: namesake, same_contact"): for the association, the tick means a person looked and it is on record.
The shop's page in `/admin/` lists the same signals; the list of shops does not. The moderators' message about a new
shop starts with "⚠ Check before approving" and the signals' sentences, with the shops' names only. The owner is told
nothing about them.

**An approved shop changed by its owner.** When an owner's edit of an approved shop (name, website, phone, email, town
or position) gives it a `namesake` or `same_contact` signal it did not have before, the moderators get a message that
starts with "⚠ Check this shop", with the signals and the link to the shop in `/admin/`. The shop stays on the map until
a person acts (suspends it, for example): an honest edit never takes a shop away. The owner is told nothing. The
comparison takes every shop the edited one looked like before, not only the first three, and each shop, signal and other
shop is told once a day: an owner who goes back and forth between their own name and a copied one sends one message.

These are hints, not a defence: whoever changes the name, the website and the street passes, and so do an email with
dots or a `+tag` (`mario.rossi@` and `mariorossi+x@`), a look-alike letter in a very short name, or a phone written so
that its country cannot be told from its first digits (the country is guessed from 1 to 3 digits). With
`features: {approval: false}` there are no signals at all, also for the shops that were already waiting: they are
approved as before, with one click and no box.

A shop closed for a while ([Closed for a while](vacation.md)) has no alarm for its Lightning address until it opens
again (only a line to know, `closed_lightning`, when the address is broken meanwhile); the page says how many are closed.

**Limits.** Under the list, the totals the site keeps against their limits ([Limits](../configuration.md#limits): shops, accounts,
requests for help, meetups from Nostr), for example `37/500` shops, and whether each is the default or set in
`community.yml`. From 80% a total is a line to look at; at the limit it is a problem, since the site refuses what would add
one more. Nothing is ever deleted for it. `manage.py dashboard` and the morning message carry those lines like the others.

What each line means, and where to go on: [helpdesk.md](../helpdesk.md#before-the-phone-rings). How the association
uses it every day: [the handbook](../operations.md#every-day).

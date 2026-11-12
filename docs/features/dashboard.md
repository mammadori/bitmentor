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
`TELEGRAM_MODERATION_CHAT`, or to the `email` of `community.yml` when the site has no Telegram bot or chat, or when
Telegram refuses it (a wrong chat, a bot thrown out): the community and the site (with the network off mainnet, since
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

A shop closed for a while ([Closed for a while](vacation.md)) has no alarm for its Lightning address until it opens
again (only a line to know, `closed_lightning`, when the address is broken meanwhile); the page says how many are closed.

**Limits.** Under the list, the totals the site keeps against their limits ([Limits](../configuration.md#limits): shops, accounts,
requests for help, meetups from Nostr), for example `37/500` shops, and whether each is the default or set in
`community.yml`. From 80% a total is a line to look at; at the limit it is a problem, since the site refuses what would add
one more. Nothing is ever deleted for it. `manage.py dashboard` and the morning message carry those lines like the others.

What each line means, and where to go on: [helpdesk.md](../helpdesk.md#before-the-phone-rings). How the association
uses it every day: [the handbook](../operations.md#every-day).

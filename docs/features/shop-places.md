# A shop with several places

A business with several branches is one shop: one owner, one register, one Lightning address, and a point on the map
for each place. The shop's own position is the first place; the others are written by the owner on
«The other places of your shop» in the panel, under «Your page», or «More than one place? Add the others» in the
shop's own form (`/shops/places/<shop>/`), up to 20, or by the
community in the shop's page of `/admin/`. A place decides nothing about where the money goes.

## What the owner writes

For each place: the address, the town and the point on the map, and two things for the customers:

- **Show this place on the map.** Off, the place stays in the panel and for the [staff's registers](staff-registers.md),
  and customers do not see it: a warehouse, an office. A new place starts as the shop itself is: on the map when the
  shop is in the lists.
- **The phone of this place**, optional. Written, customers see it next to the place; empty, none is shown. It is a
  number and nothing else (digits, spaces and `+ ( ) . / -`): it is shown with no approval.

## What waits for an approval

Where the community approves the new shops (the default: [Turning features off](README.md#turning-features-off),
`approval`), it approves their places too. Two things wait for someone of the community:

- a new place its owner writes;
- a new address, town or point of a place already approved. Until it is approved customers keep seeing the address
  before, and the owner reads in the page which one waits.

Nothing else waits: the phone, taking a place off the map or putting it back, taking a place away.

What the same owner asks piles up in one line of `/admin/today/`, which says each thing asked (a new place, a new
address and the one it replaces), each with its point: the point is what goes on the map, and the same words can
hide a place moved elsewhere. One button approves all of it; it needs the permission to change shops. The button
approves what the line said: if the owner changed or added something meanwhile, nothing is approved and the page
asks to read the line again. The places of a shop that is itself still waiting are not asked about on their own:
the shop's line names them, and they are approved with the shop. The addresses that wait are shown only to whoever
approves (the page of the day in `/admin/`, and the page of a team key that approves new shops, for the places of the
shop it approves): the day's list of the [team's area](team-access.md) says that places wait, not which. In the shop's page of `/admin/` each place says
its state (the ones taken away are not there); an address the staff corrects there on a place with a request waiting is the address, and the request is
dropped. A
place written by anybody but the owner (in `/admin/`, by an import, by a community's own code) is approved as it is
saved. A community that turned `approval` off has no wait: places go on the map as they are saved. The keys of the
[team's area](team-access.md) that approve new shops are not asked about places.

## Taking a place away, and back

«Remove» hides a place from the customers at once and puts it under «Places you took away», where the owner takes it
back for 30 days: it comes back as it was, on the map with no new approval if it was approved. After 30 days it is
deleted: when its owner opens the page, and for every shop whenever the page of the day is built, which the daily
`manage.py dashboard` does too. The places taken away do not count towards the 20.

## What the public reads

Only the places that are approved, wanted on the map by their owner and not taken away:

- the map shows a pin for each, one line in the list, and finds the shop by the town of any place; «near me» goes by
  the closest;
- the shop's page lists them under «Also here», each with its phone if it has one;
- `/shops.json` gives them as `places` (`address`, `city`, `latitude`, `longitude`, and `phone` when one is written)
  beside the shop's own fields, only for a shop that has some. The address is there only when the owner shows the
  shop's;
- [OpenStreetMap](openstreetmap.md#a-shop-with-several-places) reads and proposes only for these.

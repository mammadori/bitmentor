# Meetups

The community's get-togethers on a page of the site: the next ones and the last ones, with the day and the hour, the city,
the place, the topic, whether it is on, and the link to the group. The staff writes them in `/admin/`; a daily reading
can add the ones that certified organisers publish on Nostr. Visitors get the page, a short list or a single line for a
home page, and an iCalendar feed to put the meetups in their own calendar.

It shows no names, no photos and no counts of people: only when, where, what and whether it is happening. A meetup that
is cancelled or postponed stays on the board, and says so.

## Turn it on

A `meetups` block in `community.yml`. Without it, or empty, nothing exists: the pages answer 404 and the tags draw nothing.

```yaml
meetups:
  link: https://t.me/example            # the group; or {it: https://…, en: https://…}; used when a meetup has no link of its own
  past_months: 12                       # how far back the last meetups go (1 to 36)
  relays: [wss://relay.example.org]     # where the daily reading looks; without it, NOSTR_RELAYS of the site
  trusted:                              # Nostr authors whose events are read; the staff adds more in /admin/
    - npub: npub1…
      note: Certified by the staff on 2026-11-05, organiser of the Example meetup
  cities:                               # the places the board is about (see below)
    - {name: Example, latitude: 41.9022, longitude: 12.4970, radius_km: 15}
  event_links: false                    # true: show the link inside a Nostr event (off: only the meetup's own and `link`)
```

| Key | Default | What it does |
|---|---|---|
| `link` | none | An `https://` address, one or one per language. The link of a meetup that has none |
| `past_months` | 12 | A whole number from 1 to 36. Older meetups are kept but not shown |
| `relays` | the site's `NOSTR_RELAYS` | `wss://` addresses with a name (at most 8). The site's own relays may also be `ws://` |
| `trusted` | none | `npub` and a `note` for each (at most 100). The note is needed: it says who vouches for the author |
| `cities` | none | `name`, `latitude`, `longitude` and `radius_km` (1 to 200, 15 if missing); at most 50 |
| `event_links` | `false` | Only `true` turns it on |

A wrong value is left out, the rest stays, and `manage.py check` and the log of `web` at startup say which (`meetups.W001`).

## What visitors see

- **The page**, `/<lang>/meetups/` (in every language, in the sitemap): *Next meetups* from the soonest, then *Latest
  meetups* from the most recent, a button for the calendar and a line saying that dates can change. It needs no JavaScript.
  Put it in the menu with `path: meetups/` like any page of the site. If the community has a page called `meetups`, that
  page takes the address (with the `meetups` block or without it) and writes the board where it wants (below).
- **A piece for any page** of the community, written in its Markdown:

  ```text
  {% include "website/meetup_board.html" %}      the whole board, to put under the page's own heading (its lists start at h3)
  {% meetup_board upcoming=3 past=3 level=3 %}   a short list: how many, and the level of its headings (2 to 4)
  {% meetup_next %}                      one line: "Next meetup: Friday 9 October, 19:00 · Example, The Bar"
  ```

  A page can use the block as its switch: `{% if community.meetups %}{% include "website/meetup_board.html" %}{% else %}…{% endif %}`.
  The tags need no `{% load %}`. The board is worked out once every five minutes for everybody and again at once when
  a meetup is saved or deleted (by the staff, or by the reading of Nostr); only as many meetups are read from the table as
  are shown (100 of each list at most). It is not part of the static copy of the site (`X-Static-Copy`), which gets
  nothing from these tags.
- **The calendar**, `/meetups.ics`: the same meetups (the window of the board) as an iCalendar file in the community's
  first language. Times are UTC, so no time zone is needed; a cancelled meetup, or the old day of a postponed one, is
  `STATUS:CANCELLED`. It has no organiser, no attendees and no description from outside. It is public, kept for 15
  minutes (and worked out again after a change of the meetups) and limited to 120 requests an hour for each connection.
  It holds at most 1000 events: every meetup still to come and, with the room left, the most recent past ones.

A meetup that began less than three hours ago still counts as next. An hour is shown in the zone of the meetup, and the zone
is named only when it is not the community's.

## What the staff does

`/admin/` → Meetups.

| Field | |
|---|---|
| Day, Hour, Time zone | The hour is the clock at the meetup, in the zone written there (the community's by default). Leave the hour empty and tick “no time, only the day” when only the day is known |
| City, Place, Title or topic | Plain text |
| Link | `https://` only. Without it the board links `meetups.link` |
| Status | *Confirmed*, *Cancelled*, *Postponed* (with the day it moves to, if known), *Informal get-together* |
| Shown on the site | Untick to take it off the board and the calendar without deleting it |

The list has actions to mark meetups confirmed or cancelled and to show or hide them. The group *Site staff* gets the
permissions to keep meetups and the list of trusted authors when the update that brings this feature runs, **if the group
exists**; the update does not create it, and a site without it is left alone.

Everything the staff writes keeps its author (`entered_by`), seen only in `/admin/`, never on the site.

## Nostr

`manage.py read_meetups`, run every day by `bitmentor-meetups@<project>.timer` (05:40 UTC, with up to 20 minutes of delay,
retried every 30 minutes up to three times when no relay answers), reads the calendar events of NIP-52 (kinds 31922 for a
day, 31923 for a moment) and the deletions of NIP-09 from the trusted authors, on the relays of the block. It only reads:
it signs nothing and sends nothing to a relay. `--dry-run` shows what it would do, `--days N` changes how far back it asks
(400), `--relay URL` reads another relay.

**Whose events.** Only those of the trusted authors: the `trusted` keys of the block and the active ones in `/admin/` →
Trusted authors, each with a note on who certified it. Add a key, with its note, to start reading it; untick *read* to
stop (what it published stays). The relays are asked for those authors only, and an event from anyone else is dropped
whatever the relay says.

**What is checked** on every event, before anything is read from it:

1. its shape and size: only the seven fields of NIP-01 are looked at (whatever else it carries is dropped), a few kilobytes
   in all, a few hundred short tags; anything else is not an event;
2. that the author is on the list, and then the id and the signature (NIP-01), for the newest version of each event: a
   newer version nobody signed does not push out the one the author signed;
3. that it was not made in the future;
4. the date: a start that is not a number or a day, before 2009, more than three years ahead or more than three years
   ago is refused. A time zone that is not a real zone name is the community's;
5. the text: title and place become plain text (tags and entities removed, no control or direction characters, cut to
   the length of the field); the image, the description and the participants are ignored; a link only with
   `event_links: true`, only `https://`, never with a name or password.

An event refused never stops the others; the reading says how many and why (`manage.py read_meetups`, and the readings
in `/admin/`). The copies that several relays send of one event count once; then one reading takes the 3000 newest events
(the report says how many it left out), creates at most 200 meetups, and the site keeps at most 5000 from Nostr.

**Replacing.** An event is identified by its kind, its author and its `d` tag; the newest version replaces the older (the
same instant: the lowest id). A meetup the staff corrected is **never** overwritten: correcting a meetup that came from
Nostr ticks *corrected by hand* by itself; unticking it gives the meetup back to the reader. A meetup read from Nostr
cannot be deleted in `/admin/` (the next reading would bring it back): untick *shown on the site* instead.

**Cancelling.** A deletion by the author cancels the meetups it names, if they have not happened yet and no newer version
came after it: the meetup stays on the board as cancelled. A deletion after the day is a clean-up of the relay, and
changes nothing. A title that says it is off (“cancelled”, “annullato”, “abgesagt”…) makes the meetup cancelled. A
postponement has no Nostr event: the staff writes it.

**Cities.** Authors such as aggregators publish for a whole country. With `cities`, an event is kept only if its
position (the `g` geohash, or `lat` and `lon`) is within `radius_km` of one of them, and the city is that one; an event
without a position is kept if its place or title names a city. Without `cities` everything from a trusted author is kept,
with no city. A city name at the end of the place is not repeated.

**Copies.** Two authors can publish the same evening with times hours apart. In the same city, within three hours, the
board shows one: the staff's own, else the one the staff corrected, else the newest. Two meetups of the same author, or
two the staff wrote, are never taken for copies, and neither are meetups with no city (a community without `cities`).

**The relays.** A relay that is not in `NOSTR_RELAYS` must be `wss://` and its name must lead only to public addresses:
it is resolved once, every address is checked, and the check is repeated on the address connected to. No proxy from the
environment, a few seconds to connect, 45 seconds for each relay. Of what a relay sends, a frame bigger than an event may
be is dropped before it is parsed, JSON that nests too deep is skipped, only events of the authors asked for are kept, at
most 1500 events and 8 MiB from each. Whatever a relay does, even something unexpected, is one relay failing; the command fails (and the unit shows as failed, and `check.sh` says
so) only when no relay answered.

## Limits

- Nostr events do not say that a meeting was cancelled or moved: the aggregators do not follow either (a cancellation is
  only on Telegram, a postponement leaves the old event). The staff writes those corrections, once a week or the day
  before the usual date; a corrected meetup is never overwritten.
- Relays have different coverage and keep events for a different time: the daily reading keeps what it saw. An event no
  relay has any more is not removed.
- The board takes no sign-ups and shows no attendees.
- The reading depends on the authors being right; that is why the list of trusted ones is short, written by the staff,
  with a note.

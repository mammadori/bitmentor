# OpenStreetMap

Many of a community's shops are already on OpenStreetMap, the free map that anyone can edit, with tags that say they take
bitcoin: `currency:XBT=yes`, `payment:lightning`, `payment:onchain`, the date of the last check, sometimes a discount and
the operator. This site knows other things about the same shops: whether they closed, how they get paid, their discount,
the last day their Lightning address answered. Once a month bitmentor reads OpenStreetMap, and prepares what
OpenStreetMap should say of the community's shops.

**It reads OpenStreetMap by itself. It never writes there by itself.** That rule is not a detail, see
[Automatic edits](#automatic-edits).

> **Writing is experimental, and off by default.** The tags the reading prepares are for a person to change by hand on
> openstreetmap.org. The command that sends them, `manage.py osm_upload`, writes on a commons in the community's name:
> it works and is tested, but it has had less review and less real use, and it may change or go
> ([Experimental features](README.md#experimental-features)). A community turns it on in `community.yml`, then restarts
> the site:
>
> ```yaml
> experimental:
>   osm_upload: true
> ```
>
> Without it the command says it is off and sends nothing, and nothing on the site tells the staff to run it.

It is off until `community.yml` has an `osm` section with an area.

## Turn it on

```yaml
osm:
  bbox: [41.6, 12.1, 42.2, 12.9]          # south, west, north, east, in degrees (at most 20 each way)
  discount_tag: "currency:XBT:discount"    # the tag where the community writes a discount, like "10%" (none: no discount proposals)
  operator:                                # the tag and the value for the shops of this site (none: no operator proposals)
    tag: "payment:bitcoin:operator"
    value: "operator.example"
  name_similarity: 0.85                    # how alike two names must be to be the same place (0.5 to 1)
  distance_metres: 100                     # and how close (5 to 2000 metres)
  auto_link: true                          # link the matches that are beyond doubt; false: only list them
  comment:                                 # the changeset's comment: a line of up to 150 characters, or one per language; the
                                           # site adds "Data from the Lightning addresses checked by <site>"
    en: "Update bitcoin payment tags"
  # overpass_url: https://overpass-api.de/api/interpreter   # the defaults, https and public servers only
  # api_url: https://api.openstreetmap.org/api/0.6
  # timeout: 60                            # seconds Overpass has, 5 to 180
  # max_megabytes: 8                       # the largest answer taken, 1 to 32
```

Only `bbox` is needed. A value that cannot be used is left at its default and `manage.py check` says so
(`website.W012`). Then:

1. `docker compose exec web python manage.py osm_sync --dry-run` prints what a reading would do and writes nothing.
2. `bitmentor-osm@<project>.timer` runs `deploy/osm.sh` on the first of every month (about 03:30 UTC, with a random
   delay), which runs `manage.py osm_sync --send`. `cp current/deploy/systemd/bitmentor-osm@.* /etc/systemd/system/`,
   then `systemctl enable --now bitmentor-osm@<project>.timer`; `deploy/install.sh` lists it. A reading that fails
   (Overpass busy or slow) is tried again every hour; the fifth start within twelve hours is refused (`StartLimitBurst=4`
   and `StartLimitIntervalSec=12h`, in the `[Unit]` section, where systemd reads them), the unit becomes `failed`, and
   `check.sh` alerts. The failures are kept apart from the readings that worked (five and twelve), so that a bad week
   never pushes out the last good reading, and a failure just like the one before is not written again.

## The link between a shop and an element

A shop has a field, `osm_ref` (`node/123456`, `way/123456` or `relation/123456`: the last part of its address on
openstreetmap.org). Two shops never share an element.

- **The association** sets it in `/admin/`, in the shop's page, where a link to the element lets it check the place.
  The shops can be filtered by "OpenStreetMap: linked / link proposed / not linked", and the list has a column with the
  element proposed for each shop (its name, how far it is, how alike), to confirm from the list. A link written by hand is the link:
  whatever the reading had proposed is gone.
- **The reading only proposes.** For a **safe** match (the name is alike, `name_similarity`, over the shop's name and the
  element's `name`, `official_name`, `brand` and `alt_name`, where capitals, accents and punctuation do not count and
  a whole word of one inside the other counts as 0.9; the place is close, `distance_metres`; no other element fits that
  shop and no other shop fits that element) it puts the element next to the shop as a **link proposed**
  (`osm_proposed`, `auto_link: true`). Anybody can put a place on OpenStreetMap next to a shop, with the shop's name:
  a match is a proposal, not a fact. **Nothing is read from the element, and nothing is written for it, until the
  association confirms** it in `/admin/` (the action "Confirm the OpenStreetMap link proposed", after looking at the
  element through the link in the shop's page; "Refuse" clears it and the element is never proposed again for that
  shop). A **doubtful** match (two elements fit, two shops fit one element) goes in the report and is never proposed; so
  does a name that fits at up to five times the distance, and a shop without a position: for a person to look at, and to
  link in `/admin/` if they agree. `auto_link: false` makes every match only a suggestion in the report.
  `/admin/today/` has a line while links wait for confirmation.

## What the reading does

`manage.py osm_sync` asks Overpass for every element in the area with `currency:XBT`, `payment:bitcoin`,
`payment:lightning` or `payment:onchain`, then:

- **For a shop whose link is confirmed, fills empty fields**, and only those. By itself: the address (`addr:street` and
  `addr:housenumber`), the town (`addr:city`), and the position, only if it falls inside `bbox` and the shop has neither
  coordinate. The website (`website` or `contact:website`, only `http` and `https`) and the phone are **offered, not
  written**: a stranger can write a website on a map to send customers somewhere else, so they wait in
  `/admin/` ("Offered by OpenStreetMap" in the shop's page) for the action "Accept the website and phone offered by
  OpenStreetMap", which writes them to the fields that are still empty, or "Decline". A field somebody wrote, the owner or
  the association, is never touched, not even when it is different: the write is a conditional update, so an owner who
  saves at the same moment wins. The email is not taken, and neither is the discount: a discount promised to
  customers is not read from a map anyone can edit.
  **Every value written is in the report** (`/admin/shops/osmrun/`, "Fields written"), so that it can be undone, and
  the shop remembers what it did (`osm_notes`, never shown to the owner, with who confirmed a link or accepted the
  contacts, and when: the shop's page in `/admin/` shows it as "OpenStreetMap decisions"): a field that was filled and was emptied
  again by the owner on purpose is not filled again, a value that was accepted or declined is not offered again, an
  element that was refused is not proposed again.
- **Lists the candidates**: elements with `currency:XBT=yes` that are not ours (not linked, not in doubt). They are a
  list to know: they are never created as shops, and nothing is proposed for them or for any place that is not one of
  our shops (a closed place on OpenStreetMap that is not ours is not our task). The monthly message names the new ones.
- **Reports** the linked shops whose element is not there any more (deleted, or without the bitcoin tags), the shops that
  OpenStreetMap marks as taking no bitcoin (`currency:XBT=no`, or, with no `currency:XBT`, every payment method at `no`, or
  a single `payment:lightning=no`: the same reading decides the proposals below), and the shops whose element was checked there **after** the last day this site's
  guardian found the shop's Lightning address working, or after its closing day: they take bitcoin without our registers, or they still say yes after we closed them. The day
  OpenStreetMap vouches for is the most recent of `check_date:currency:XBT`, `check_date:payment:*`, `check_date` and
  `survey:date`: any of them is somebody looking.
- **Prepares the proposals** for OpenStreetMap, below. It writes them in the report (`/admin/shops/osmrun/`, the last
  twelve are kept), nowhere else.

Everything Overpass says is read as written by strangers: the answer is parsed field by field, an element that is not
what OpenStreetMap writes (no type or id) is left out and counted; one with a bad part (a tag too long or with a line
break, a coordinate out of its bounds) loses that part, is marked "not read whole", is never linked, proposed for or
given back to the server, and is not taken for a deleted one. `NaN`, absurd coordinates and numbers like `1e-999999999`
(a valid JSON number that written out in full would take a gigabyte: coordinates keep nine decimals) are refused, and a
text goes into a field only if it passes that field's rules. An answer with a `remark` (Overpass runs out of time or memory and sends
part of the data with a 200) is refused whole, because a partial list would say that shops left OpenStreetMap; so is one
with less than half the elements of the last reading (`--accept-fewer` if it is true). A reading that fails
changes nothing and is kept as failed. The requests go through the protections of the other servers the site does not
control: https only, public servers only (checked on the connection really made), no redirect, a limit on the time of
the whole request and on the size of the answer, read as it arrives. The `User-Agent` says `bitmentor` and the site.

## The proposals

For each shop with a link that the site shows (not suspended, not off the lists, not waiting for its wallet, not a
made-up one) the tags OpenStreetMap should have, and the difference from what it has:

| Tag | What the site proposes |
|---|---|
| `currency:XBT` | `yes`; `no` for a shop whose closing day (`closed_on`) has passed. A closing day still to come is a shop that still takes bitcoin. |
| `payment:lightning`, `payment:onchain` | `yes` for the way the shop gets paid (Lightning with a register; on-chain when its owner says the register takes it, and only where [on-chain at the register](onchain.md) is turned on). Never `no` for the other: the site does not know. For a closed shop, the ones that say `yes` become `no`. |
| `discount_tag` | the shop's discount as `10%`; for a closed shop the tag is taken away. |
| `check_date:currency:XBT` | the last day the shop's Lightning address answered the guardian (`lightning_worked`), or the closing day for a closed shop. |
| `operator.tag` | the community's value, for a shop that is open. Never for a closed one. |

**When the two differ, the dates decide.** The site's date is the last day the Lightning address answered (or the closing day); OpenStreetMap's is
the most recent of its checks (see above). A tag that is missing is added whatever the dates say, as long as nothing says
no. A value that differs is changed only if the site's date is **more recent** than OpenStreetMap's (or OpenStreetMap has
none). If OpenStreetMap has a check more recent than ours, nothing is proposed and the shop is listed as "checked there
after our last check". After a "no" of that kind that is more recent than our date (or when we have no date
to set against it) **nothing at all is proposed for the shop**: not the Lightning tag, the discount or the operator,
which would be a lie about a place that does not take bitcoin.

**Some proposals are for a person to decide** (`review`): turning a `no` into a `yes` (when our date is more recent than
their check, or theirs has no date), with every tag that goes with it, and taking away a discount that the site does not
know. They are listed in the report and by `osm_upload`, marked, and sent only with `--with-review`.

**The operator** is added next to the others, never in their place: if the tag already lists `old.example`, the
proposal is `old.example;operator.example` (OpenStreetMap writes several operators with `;`; empty values and spaces
are dropped, and the comparison ignores capitals). That is a normal update, not a matter for a person.

## Automatic edits

OpenStreetMap has a code of conduct for automated edits: they have to be documented, discussed with the local
community, and made from an account of their own. So bitmentor never writes to OpenStreetMap by itself. The timer
**reads**, fills **the site's own** empty fields, and **prepares**: the report in `/admin/shops/osmrun/` lists the tags
OpenStreetMap should have, and a person can change them by hand on openstreetmap.org, with their own account.

Only `manage.py osm_upload`, run by a person in a terminal, can write. It is experimental and off unless
`community.yml` says `experimental: {osm_upload: true}`; off, it says so and ends without reading or sending anything.
Turned on:

- it reads OpenStreetMap again first, so that it proposes against what is there now, and sends the version it saw:
  if somebody edited the element meanwhile, the server refuses and nothing is overwritten;
- it touches only the bitcoin tags of the table above (the code refuses any other tag), only for linked shops, a
  hundred elements at most at a time (`--limit`);
- **without `OSM_TOKEN`** it sends nothing. It writes an osmChange file and prints, for each element, the changes and a
  link to the editor (iD) on it, to do it by hand or to load the file in an editor such as JOSM and upload it with your own
  account. The file (`--osc FILE`, `-` for the standard output) must be a file that does not exist yet and is never
  a link; by default it is a new file with a name nobody can guess, in the temporary folder, readable by its owner only;
- **with `OSM_TOKEN`** (the OAuth2 token, scope `write_api`, of the account of the person who signs) it lists every
  change, and waits until you type `upload`. There is no option that skips the question, and without a terminal it
  refuses. **Pass the token for that one run, do not keep it in the `.env`** (the `.env` feeds the container that serves
  the site):

  ```bash
  read -rs OSM_TOKEN; export OSM_TOKEN      # it asks for the token without showing it, nothing goes in the history
  docker compose exec -e OSM_TOKEN web python manage.py osm_upload
  ```

  Not `-e OSM_TOKEN=<the token>`: a token on the command line stays in the shell's history and in the list of processes.

  Then it opens a changeset with a `comment` (the community's line, and the sentence "Data from the Lightning addresses
  checked by <site>": nobody went to look, the site checked the shops' Lightning addresses), `source=<site>` and
  `created_by=bitmentor`, uploads,
  and closes it whatever happens, an error or a Ctrl-C, and prints its address. The token is sent in one header, only to a
  server of OpenStreetMap by exact name (`api.openstreetmap.org`, or `master.apis.dev.openstreetmap.org` for trying;
  `api_url` must be one of them: a setting that is not is refused, and so is an address with a backslash, a percent
  sign or a space, or that `requests` and our own parser read as two different hosts), checked again on the address about
  to be asked, and is never printed;
- what was sent is no longer proposed, and the merchants' requests it did are marked done.

Before turning it on and before the first upload, tell the local OpenStreetMap community what this does (the forum and the wiki of OpenStreetMap
have a place for it), use an account of the association and not a personal one, and look at the first changeset.

## The merchant

In the panel each shop has "Your shop on OpenStreetMap", when the community uses it. The page says, from the database
only (no server is asked while it loads):

- if the shop is linked: a link to its element, and which lines are missing or different there, as of the last
  reading, with the day of that reading. The proposals that need a person are not shown to the merchant, and the page
  then says that a person of the association has to look at it, instead of saying that all is well; it says so, too,
  when OpenStreetMap says the shop does not take bitcoin any more, and when the last reading is more than 45 days old;
- if it is not: what is needed to add it (a name, a place on the map, the kind of place, which the merchant chooses in
  the editor) and the lines about bitcoin to write;
- the steps to do it alone, in plain words, with the editor already on the shop's place or element and the lines to
  copy. A tag is explained once;
- a tick, **The association does it for me**, and a button. It makes an `OsmRequest` (shop, date, what is asked: put the
  shop there or update its entry, the lines, the state and who did it), tells `TELEGRAM_MODERATION_CHAT` as a request
  for help does, and appears as a line on top of `/admin/today/` and in `/admin/shops/osmrequest/`. Only a queue:
  a person of the staff does it, by hand in the editor (or with `osm_upload`, where it is turned on), and marks it done
  (or writes why it could not be done) in `/admin/`; the merchant sees the state in the panel. One request is open at a time for a shop; three a day for a
  shop, ten an hour for a person, and a ceiling on the messages to the chat, beyond which the request is saved and only
  in `/admin/`. The request carries nothing the merchant wrote. A request left open by a previous owner of the shop is
  closed ("the shop changed owner") when the new owner asks, and is never shown to them as theirs.

## For the association

- `/admin/today/`: a line for each open request; a line "tags of our shops on OpenStreetMap are not what this site
  knows" while there are some, for a person to change by hand (with `osm_upload` turned on: "changes proposed for
  OpenStreetMap wait for a person to send them", and the command); a line for the links proposed and one for the contacts offered that wait for the
  association; a line when the last reading failed, or none worked for 45 days.
- The monthly message (`TELEGRAM_MODERATION_CHAT`, else the community's email, **only when there is something new since
  the last summary**; at most 3900 characters): how many changes are proposed in all, and by name what is new: links
  proposed, fields written, contacts offered, the new places, the shops OpenStreetMap marks as no longer taking bitcoin,
  the shops checked there after our last check. The same proposals month after month are not news: they wait on
  `/admin/today/`. Names that come from OpenStreetMap go through the same cleaning as a shop's name in an alert (no
  address, no mention, no command).
  What a reading does once (a link proposed, a field written, a contact offered) is not found again by the next, so it is
  carried to the next summary that goes out: a summary that did not go out, or a reading made by hand without `--send`,
  never takes its news with it.
- Each month: read the message, open the report in `/admin/shops/osmrun/`, confirm or refuse the links proposed, accept
  or decline the contacts offered, link in `/admin/` the doubtful ones that are right. The tags the report proposes
  are then changed by hand on openstreetmap.org, or, with `osm_upload` turned on, sent with
  `read -rs OSM_TOKEN; export OSM_TOKEN` and `docker compose exec -e OSM_TOKEN web python manage.py osm_upload`.

## What it does not do

- It does not create shops from the candidates, nor elements on OpenStreetMap: a new entry is made by the merchant or by
  a person, in the editor, with the lines the page gives.
- It does not change a name, an address, a category or a position on OpenStreetMap.
- It does not read the discount from OpenStreetMap, nor the email.
- The house number goes after the street (`Main Street 12`), as in most of Europe; the position is a point, the centre of
  a way or a relation.
- It reads the whole area in one request: a region, not a country.

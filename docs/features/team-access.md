# Team access with a Lightning wallet

Optional, and off by default. While a site is under construction (`SITE_MODE=development`) only the addresses in
`DEV_ALLOWED_IPS` see it. That does not work for somebody whose address changes: a phone, a home line, a person
travelling. With team access such a person signs a code with their Lightning wallet (LNURL-auth, the same way a merchant
signs in) and their **browser** gets a pass: for a week it sees the site as if it came from an allowed address, from any
network.

It is not an account. A team key signs in to nothing, owns no shop and gives no right in `/admin/`, which keeps its
password and its own rule by address (`ADMIN_ALLOWED_IPS`): see [A pass and `/admin/`](#a-pass-and-admin).
[Staff accounts never sign in with a wallet](staff-and-shops.md), and this does not change it.

## Turning it on

One line in the stack's `.env`, then `docker compose up -d web caddy`:

```
TEAM_ACCESS=on
```

Caddy and Django read the same variable; nothing else needs configuring. `manage.py preflight` says when it is on, with
how many keys and open invites. A value that is not `on` or `off` (nothing is `off`) stops Django from starting in
every mode, and Caddy in `development` and `maintenance`, the modes whose rules read it.

Off, nothing of the feature exists: every address under `/team/` answers 404, to every method, `/admin/` shows nothing of it, Caddy's
rules are the ones it had without it, and a pass left in a browser gives nothing. The tables stay in the database, empty
or as they were. It does something only in `development` mode and only on the site's own address: in `maintenance`
everything stays closed, as for the allowed addresses; in `production` the site is open and the feature does nothing; on
the onion address it does not exist. The one exception is [the volunteers' area](#the-volunteers-area), a second
switch (`TEAM_AREA`), off by default: with it, team keys also sign in while the site is in `production`.

The team hears of every new key and of every new browser that gets a pass, in the moderators' chat
(`TELEGRAM_MODERATION_CHAT`) or, without Telegram, at the community's email (or `moderators_emails`). Telegram is optional. The message leaves
after the person's answer has been sent, so a chat or a mail server that does not answer neither delays nor fails a
sign-in: a message that cannot be sent is a line in the log of `web`. `manage.py preflight` warns when the site has
neither, and nobody would be told (with the moderators' other messages, [operations](../operations.md#the-moderators-messages)).

## Inviting somebody

1. In `/admin/`, **Team invites → Add**: the person's name and a role (team, staff or moderator). The role is a label,
   except `moderator` when [the volunteers' area](#the-volunteers-area) is on: that key opens it. The page that follows shows a link and its QR code, **once**: the site keeps a hash of the link, not the
   link. The invite is good for 24 hours and for one wallet.
2. Send the link by a channel you trust. Whoever opens it first and signs becomes the key, so it is as good as a
   password until it is used.
3. The person opens it (`/team/join/<token>/`, a page of the site that is open from outside) and scans the code with
   their wallet, or taps it on the phone. The wallet makes a key for this site only (its linking key): that key, with
   the name and the role, is the person's **team key**. The invite is used up, and the browser that showed the page
   gets its first pass.

A wallet that already has a team key, or had one that was revoked, cannot take another invite: delete the old key
first, if that is what you want. An invite lost or sent to the wrong person: delete it in `/admin/`, which revokes it,
and make another. Invites disappear from the list 30 days after their last day.

## Signing in, later

On the under-construction page, a gesture the public does not know shows a sign-in code. On the software's default page
(`maintenance/index.html`) it is five quick taps on the title, or `#team` at the end of the address
(`https://example.org/#team`). The person scans the code with the same wallet and the page reloads as the real site. A
wallet whose key is not an active team key gets nothing: the page says so and stays what it was.

A community with its own page (`MAINTENANCE_DIR`) adds its own gesture: see
[For your own under-construction page](#for-your-own-under-construction-page).

## Revoking

In `/admin/`, **Team keys**:

- **Revoke** (an action on the selected keys): the wallet signs in no more, and every pass it gave stops at the next
  request. The row stays, so the same wallet cannot come back with a new invite.
- **End the passes**: every browser of that key has to sign in again, and the key stays. For a browser that was lost,
  or a pass somebody got by a trick (below).
- Deleting a key forgets it, name and key: its passes stop too, and the wallet could be invited again.

Both actions are written in the history of each key (the admin's log), with who did it and when.

## What a pass gives

Exactly what an address in `DEV_ALLOWED_IPS` gives on a site under construction, and no more:

- the site instead of the under-construction page, on every path;
- the [private part of the pages](private-pages.md), the pages marked `visibility: private` or `staff`, and the line at
  the top that says so;
- the larger email allowance of the allowed addresses (`emails_staff_factor`, [Limits](../configuration.md#limits)).

A pass opens the site's own host and nothing beside it: a service on a host of its own (a Blossom host of
`BLOSSOM_HOSTS`, a relay on `relay.<SITE_HOST>`) never receives the cookie, which has no `Domain`, and never asks
for it. While the site is under construction those hosts open to the addresses in `DEV_ALLOWED_IPS` only, so a team
member who came in with a pass from another address sees the site and not them.

It signs in to no shop and to no account, and the role changes nothing here: the role `moderator` opens
[the volunteers' area](#the-volunteers-area), when it is on.

### A pass and `/admin/`

A pass is not a right in `/admin/`: the password is asked as always, and a team key is no account. But who may
*reach* `/admin/` is decided by address alone, with `ADMIN_ALLOWED_IPS`, and a pass does not change that rule either
way:

- with `ADMIN_ALLOWED_IPS` empty, the default, `/admin/` is open to whoever sees the site. On a site under
  construction that used to mean the addresses in `DEV_ALLOWED_IPS`; with team access it also means every browser with
  a pass, which gets the sign-in page of `/admin/` and can try a password. That is on purpose: staff whose address
  changes must be able to reach it;
- with `ADMIN_ALLOWED_IPS` set, only those addresses reach `/admin/`, pass or no pass.

**If the team is wider than the staff, set `ADMIN_ALLOWED_IPS`.** `manage.py preflight` says so when team access is on
and the variable is empty.

## The volunteers' area

Optional, and off by default. The volunteers who look after the shops day by day (the moderators) work in `/admin/`
with a staff account and a password, often behind `ADMIN_ALLOWED_IPS`: from a phone or a new network they cannot, and
the admin is more than the job needs. The area gives them the day's work on pages of the site itself, made for a phone,
with the same team keys: no password, no account.

### Turning it on

Team access must be on too. In the stack's `.env`, then `docker compose up -d web`:

```
TEAM_ACCESS=on
TEAM_AREA=on
```

Only Django reads `TEAM_AREA`: Caddy's rules do not change. A value that is not `on` or `off` stops Django from
starting. `TEAM_AREA=on` without `TEAM_ACCESS=on` does nothing, and `manage.py preflight` says so; with both on it says
how many moderator keys can open the area, and how many of them may approve new shops. Off, everything is exactly as without it: every address of the area answers
404 to every method (`/team/` itself answers as any address the site does not have), `/admin/` shows nothing of it, and
in `production` team access does nothing.

### Who opens it

A **team key whose role is `moderator`**: invite the person in `/admin/` (**Team invites → Add**, role moderator), as
[above](#inviting-somebody). With the area on, invites can be made and taken while the site is in `production` too.
Keys with the role `team` or `staff` open nothing new. A staff account opens nothing either: the area reads the pass,
never the session of `/admin/`, and a pass never signs anybody in to an account (`request.user` stays a visitor).

The person signs in at **`/team/`**, a page of the site: it shows a sign-in code, they scan it with the wallet of their
key (or tap it on the phone), and the page opens the area. The code is the one of `/team/login/`: five minutes, bound to
the browser that asked, the same limits. On a site under construction `/team/` is reached like the rest of the site: from
an allowed address, or with a pass got from the gesture of the under-construction page. A pass got on a site under
construction still opens the area once the site is in `production`: it names the key, not the mode.

The pass is the same `team_pass` of seven days, checked against the database at every request: **Revoke** and **End
the passes** in `/admin/` take the area away at the next page, and so does deleting the key. The team hears of every new
browser, as always: on an open site the notice says "signed in with their wallet in a new browser".

In `maintenance` the area does not exist (Caddy shows the maintenance page to everybody, and Django answers 404); on the
onion address it does not exist either.

### What it opens

These pages, and nothing else:

- **`/team/today/`**: the day's list, the same items as `/admin/today/`, made by the same code
  (`shops/dashboard.py`), in the visitor's language. Without the links into `/admin/`, without the button that approves
  a shop, and with only what the **Moderators** group sees there ([The moderators](../operations.md#the-moderators)). Its lines
  about requests for help, OpenStreetMap requests and new shops link to the area's own pages.
- **`/team/help/`**: the requests for help still open, oldest first, with what a volunteer needs to answer: the shop,
  the symptom, the line the merchant wrote, how they asked to be called back with the shop's phone or Telegram chat, and
  what the site knew when they asked; the ones solved in the last three days below. **Mark as solved** and **Mark as
  waiting again** do what the admin's actions do: the merchant sees «Solved» in the panel, and the request leaves the
  day's list or comes back to it. Who solved it is written as `[team key 7] Mara` (the key's number and name), which no
  staff username can be; a request solved from `/admin/` says only "from the administration". Of the copy of the
  diagnosis the page keeps only what is about the shop, its register and its Lightning address: the findings about the
  owner's account and how they sign in (a piece of their wallet key, their last sign-in, their recovery email, a staff
  account) are left out, by their codes (`team/area.py`, `DIAGNOSIS_HIDDEN`); a code the list does not know yet is
  left out too, until somebody decides. The findings about the changes to the owner's account (`account_changed*`, with
  who made them) are kept: they are what `/team/changes/` shows. The findings that name the merchant's **staff
  registers** (`staff_off`, `staff_unopened`, `staff_code_waiting`, `staff_ok`: names that are often the first names of
  the merchant's employees) are **the community's choice, off by default** (`DIAGNOSIS_BY_CHOICE`): off, one neutral
  line says there is something about the registers of the staff and that whoever has the administration sees it, so
  the volunteer knows to ask, never the names; the community shows them with this in `community.yml`
  (anything but `true` or `false` counts as off, and `manage.py check` warns, `team.W001`):

  ```yaml
  team_area:
    staff_register_names: true
  ```

  `/admin/` is unchanged: the Moderators group sees the whole diagnosis there.
- **`/team/shops/`**, only for a key an administrator allowed to approve (below): the new shops that wait.
- **`/team/osm/`**: the merchants' requests for the community to put the shop on OpenStreetMap or update its entry,
  with the lines to write, a link to the shop's entry and to the editor, and **Mark as done**, the admin's own action.
  The work is done by the volunteer on openstreetmap.org with their own account: the site writes nothing there. A
  request that cannot be done is answered by an administrator, who writes the merchant why (the area has no field for a
  text to the merchant). The ones closed in the last seven days are listed below, to read; who closed one from
  `/admin/` is not named.
- **`/team/changes/`**: the register of the changes to the owners' accounts, to read, newest first, fifty a page:
  when, what changed (the admin's words), the shops, where it came from (the admin, a command, the owner's panel…),
  **who did it** and who was told. The one place of the area that shows staff usernames, on purpose: seeing who changed a
  merchant's account is the point of this register, the same second pair of eyes the Moderators group has in
  `/admin/`. The register keeps the names of what changed, never the values: no address, no email, no key.
- **`/team/me/`**: the key's name and role and when this browser's pass ends, with **End this browser's pass**. That
  button deletes the cookie in this browser, and nothing more: it is not a revoke on the server. A pass copied
  elsewhere keeps working until an administrator ends the key's passes (**End the passes**) or seven days go by.

What it **never** shows or does: a Lightning address in full as the site itself writes it (every address on these
pages is cut as the notices cut it, `ma…@provider`, and so are its URL forms, `https://provider/.well-known/lnurlp/ma…`
and `lnurlp://…`, also in what the merchant wrote and in the diagnosis), a recovery email (a merchant who asked to be
called back by email is written to by somebody with `/admin/`), accounts (but the staff usernames of the register of
changes), keys, a link into `/admin/`; it changes no shop, owner or address, and approves a shop only as below. There is
no «refuse»: a volunteer who doubts leaves the shop waiting and tells an administrator.

### The area and the Moderators group

For the day's list, the requests for help and the register of the changes to the accounts, the area follows the
**Moderators** group: it takes the rights from the group's list (`shops/moderators.py`), so the two cannot drift apart,
and shows no more than the group sees in `/admin/` (less: no recovery email, no shop's page). It adds **two things of its
own**, which the group does not have, on purpose (`team/area.py`, `BEYOND_THE_GROUP`):

- **marking an OpenStreetMap request as done**: the request holds the shop's public data, the volunteer does the work on
  openstreetmap.org with their own account, and the site writes nothing anywhere;
- **approving new shops**, only for the keys an administrator marked, and never a shop that looks like a copy (below).

### Approving new shops

Approving puts a shop on the map: in `/admin/` it needs `shops.change_shop`, more than the Moderators group has. So it is
not for every volunteer: an administrator ticks **may approve new shops** on the invite, or later on the key's page in
`/admin/` (the change is in the key's history). Only staff who may approve shops themselves (`shops.change_shop`) can
tick it: for anybody else it is read-only on both pages, and the server ignores it in a form made by hand. The «You»
page says whether a key may. The flag is read again in the same transaction as each approval: one taken away, or a key
revoked, a moment before is honoured.

**An invite with this box ticked is worth the right to approve**: whoever opens the link first and signs gets it. Send it
by a channel you trust, delete the invite if it went astray, and revoke the key if it was taken.

Such a key sees `/team/shops/`: the same shops `/admin/today/` lists as waiting, in the same order, with what a person
needs to judge them (the name, the town, the address, website, phone and email as the shop's page would show them, the
description, when it was written) and never the owner's account or the Lightning address. **Approve** puts the shop on
the map, `approved_by` says `[team key 7] Ada`, and the log of the area keeps the row; there is no line in the admin's
history of the shop, which needs an account: the shop's page in `/admin/` shows who approved it and when
(`approved by`, `approved on`).

**A shop that looks like a copy of another** (`shops/approval_signals.py`: the same name nearby, the same website or
phone…) **is never approved from the area**: the page shows the warning and its reasons, and says that an administrator
approves it from `/admin/today/`, where the box «I checked» is. The server refuses it whatever the form sends, and when
the checks cannot be run nothing is approved from the area. A note that asks nothing (a namesake far away) is shown and
does not stop the approval. A key that may not approve sees the waiting shops in the day's list only, without a button;
its POST is a 404.

The cut of the addresses is a courtesy for what the site writes and for the obvious forms a person types, not a
guarantee about free text: a merchant can write an address, an email or a phone number in the line they send in a way
no pattern catches, and the page shows that line.

Every page is `no-store`, not indexed and sends no referrer. Every change is a POST with Django's CSRF token, checked
after the gate: a POST from whoever the area is not for gets the 404 of an address that does not exist, also without a
token, never a 403 that would tell the area is there (the sign-in page `/team/`, public while the area is on, answers
405 to a POST). The pages
have a limit of 300 requests in ten minutes per address (900 per /48 network), the changes 60 (180), the sign-in page the
limits of the sign-in codes. Each change reads its request for help again, and one that changes nothing (already solved,
already waiting) is not written.

### The log of what volunteers do

Every change made in the area writes a row, in the same transaction as the change: the key, its name at that moment,
what was done (marked a request solved or waiting again, approved a shop, marked an OpenStreetMap request done, ended
the pass of a browser), what it was done to (the request's or the shop's number and name) and when. It is in `/admin/` under **Volunteers' actions**, to read only, with filters by key
and by action; each key's page links to its rows. Deleting a key keeps its rows, with the name. Nothing deletes them by
itself: see [privacy.md](../privacy.md#what-the-software-stores-where-and-for-how-long).

### The weakness, with the area on

**A wallet signs whatever code it is shown** ([below](#what-to-know-before-turning-it-on)): somebody who talks a
moderator into scanning a code of theirs gets a pass of that moderator's key in their own browser. With the area on, that
pass opens the requests for help of the merchants, with the line they wrote and the phone or chat they gave, and lets
them be marked solved. What limits it is what limits team access, and the area is kept small on purpose: no address, no
email, no change to a shop; the team is told of every new browser; the log names the key behind every change. If a
browser was not theirs: **End the passes** of that key at once, then read its rows in the log. Tell the moderators:
**scan a sign-in code only on the page you opened yourself**. The sign-in page says it too.

## What to know before turning it on

**A wallet signs whatever code it is shown.** This is the known weakness of LNURL-auth, and nothing here removes it:
somebody opens the sign-in on their own browser, sends the code to a team member ("scan this, it fixes your access"),
and if the member scans it the pass goes to the browser that asked, not to the member's. What limits it:

- the wallet's prompt names the site's own domain: a code from a look-alike site signs for another domain and opens
  nothing here;
- the team is told of every new browser that gets a pass: a sign-in the person did not make shows there;
- "End the passes" and "Revoke" take effect at the next request;
- a pass lasts seven days and is not renewed by use;
- what it opens is a site under construction: no account, no shop, no money, and of `/admin/` at most the page that
  asks for the password ([above](#a-pass-and-admin)); with the volunteers' area, also the area
  ([its weakness](#the-weakness-with-the-area-on)).

**With the area on, the sign-in page of an open site is public.** On a site in `production` anybody can open `/team/`
and ask for codes. The limits by address hold, but strangers asking from many addresses can fill the 5000 sign-in codes
alive: then new sign-ins answer «too many requests» until the requests stop and the codes expire (five minutes). Nobody
with a pass is touched, and the invites' codes are counted apart. The remedy is the one below for a site under
construction: find the networks in Caddy's access log (`ACCESS_LOG=on`, the lines of `/team/` and `/team/login/`), close
them at the firewall or at the entry proxy.

Tell the team: **scan a sign-in code only on the page you opened yourself.** The invite's page says it too.

The rest, for whoever reviews it:

- **The pass** is the `team_pass` cookie: `HttpOnly`, `Secure`, `SameSite=Lax`, for the whole site (Caddy must see it
  on every path) and for this host only. Its value is signed with `SECRET_KEY` and names the key and a stamp of the
  key that changes when the key is revoked or its passes are ended. It is checked against the database at every
  request, by one function (`team/access.py`, `holder`) that both Caddy (`/team/check/`) and Django
  (`from_allowed_address`) ask. Seven days, then the wallet signs again. A new `SECRET_KEY` ends them all.
- **Only over HTTPS.** The cookie is `Secure`, so the five-minute try of the README over `http://localhost` cannot use
  the feature, which needs Caddy anyway. Behind another proxy that holds TLS (`SITE_SCHEME=http`) it works: the browser
  speaks HTTPS to that proxy.
- **Invites**: 256 random bits, stored as a SHA-256, used by one conditional `UPDATE` (two wallets signing at the same
  instant make one key), 24 hours.
- **Two secrets are in a path**, which logs write: the token of an invite (`/team/join/<token>/`) and the code a
  sign-in page polls (`/team/login/<k1>/`). The code of an invite's page is worth the invite until a wallet signs it,
  so that page polls an address with nothing in it (`/team/poll/`: the cookie names the code), the database keeps the
  code under an ordinary id, and no page of `/admin/` shows it or lets a list be filtered by it. Whatever follows
  `/team/join/` or `/team/login/` is cut from Caddy's error log, gunicorn's access log and Django's own log lines
  (`/team/join/-/`) and from Caddy's access log (`/team/join//`), also when the address is written with capitals,
  doubled slashes or something before `/team/`: `Caddyfile`, `config/quiet.py`. A field of Caddy's logs takes one
  filter, and in the error log the path's is a second filter around the first: check it after a change of Caddy's
  version (below). The page tells the browser to send no referrer. The link still stays in the history of the browser
  that opened it, spent.
- **Sign-in codes** last five minutes, are signed once (the first signature counts) and are bound to the browser that
  asked with the `__Host-team_k1` cookie (`HttpOnly`, `Secure`, `SameSite=Lax`, `Path=/`, no `Domain`): anybody can see
  a QR code, only that browser gets its answer. The `__Host-` name makes the browser refuse that cookie from anywhere
  but this host: a site on a sibling subdomain cannot plant its own signed code in a team member's browser (cookie
  tossing), have them scan it, and poll for the pass itself. A cookie of the old name, `team_k1`, is read by nothing.
  `team_pass` keeps its name, which Caddy's rule matches: a subdomain could plant one, but only a pass signed with
  `SECRET_KEY`, which it cannot make; planting its owner's own valid pass would only make the browser act as that key,
  and the log would name it.
- **Limits** in ten minutes, by address and three times as many by /48 network: 30 codes, 20 signatures, 30 invite
  pages, 600 polls. `/team/check/` has no counter: it is asked for every file of a page a team member opens, a counter
  would write to the database each time, and a forged pass is refused by its signature before the database is read.
  Caddy asks it only for a cookie that has the shape of a pass.
- **How many codes at once.** At most 5000 sign-in codes are alive at the same time, and, counted apart, 500 codes of
  invite pages, which only somebody with an open invite can make: strangers asking for sign-in codes from many
  addresses cannot keep an invited person out. When the 5000 are reached the sign-in answers 429 (the default page
  shows the sentence) and the log of `web` says "team access: 5000 sign-in codes alive". Nobody who has a pass is
  touched, and codes expire in five minutes, so it ends by itself when the requests stop. If they do not: find the
  networks in Caddy's access log (`ACCESS_LOG=on`, the lines of `/team/login/`), close them at the firewall or at the
  entry proxy, or turn `TEAM_ACCESS` off for a while and use `DEV_ALLOWED_IPS`.
- **Only the addresses that exist.** From outside Caddy lets through the addresses listed
  [below](#for-your-own-under-construction-page), each written exactly, only with GET and only on the site's own
  entrance. Any other address under `/team/` is the under-construction page, like the rest of the site; the addresses
  of [the volunteers' area](#the-volunteers-area) too, which a browser reaches with a valid pass, as it reaches every
  other page, with any method (its changes are POSTs). On an open site Caddy lets everything through to Django, which
  answers 404 to whoever the area is not for.
- **Wallets are not robots.** The rule that refuses robots by their name (`Go-http-client`, `python-requests`…) lets
  `/team/auth/` through, as it does `/health/`: a wallet written in Go or in Python signs like any other.
- **Headers.** Caddy sends the check the visitor's own headers. Django reads the cookie, `X-Entrance` and
  `X-Forwarded-For`, which Caddy writes itself, and nothing else; nothing of the answer is copied onto the request.
  Another site block of `caddy.d` that sends to `web` must write `request_header X-Entrance onion`, as for the
  [private part of the pages](private-pages.md).
- **Data**: the name the admin typed, the role and the wallet's linking key, which is a key made for this site alone.
  No address, no email. With the volunteers' area, the log of what they did there. See
  [privacy.md](../privacy.md#what-the-software-stores-where-and-for-how-long).

## Checking it after a change

The tests of `team/` cover Django's side with real signatures. What Caddy does is checked by running it: a stack in
`development` mode with `TEAM_ACCESS=on` and `ACCESS_LOG=on`, and `curl` from an address that is not in
`DEV_ALLOWED_IPS`:

- no cookie: the under-construction page on every path, and no line in the log of `web` (`docker compose logs web`);
- a valid `team_pass` cookie: the site on every path, with a `GET /team/check/` 204 in the log of `web` for each;
- the same cookie with its last characters changed, or after "Revoke": the under-construction page, with a
  `Set-Cookie: team_pass=; … Max-Age=0`, and a `GET /team/check/` 401;
- the link of an invite and a whole sign-in: `/team/join/-/` and `/team/login/-/` in the log of `web`,
  `/team/join//` and `/team/login//` in `/data/access/access.log`, and, with `web` stopped (a 502), `/team/join/-/` in
  the log of `caddy`; then search all three for the token and for the codes (the `k1` of each LNURL): nothing;
- `/team/x/`, `/TEAM/login/`, `//team/login/`, `POST /team/login/`: the under-construction page (a 405 for the POST,
  from Caddy), no line in the log of `web`;
- `/team/auth/` with `User-Agent: Go-http-client/2.0`: Django's JSON answer, not a 403;
- `TEAM_ACCESS=off`: the under-construction page for `/team/login/` too, and for a request with the cookie, with no
  line in the log of `web`.
- With `TEAM_AREA=on`: `/team/today/` and `POST /team/me/end/` without a cookie: the under-construction page (a 405
  for the POST, from Caddy), no line in the log of `web`; with a moderator's valid cookie: the area, with a
  `GET /team/check/` 204 before each in the log of `web`.

## For your own under-construction page

These addresses, and only these, stay reachable from outside while the site is under construction. Your page calls
the first two; the wallet, Caddy and the invite's own page call the others.

| Address | Who calls it | What it answers |
|---|---|---|
| `GET /team/login/` | your page | JSON: `svg` (the QR code as inline SVG), `lnurl` (the same code as text, for a `lightning:` link) and `poll` (the address to ask next). It sets the `__Host-team_k1` cookie |
| `GET /team/login/<k1>/` | your page, every two seconds | JSON: `state` is `pending`, `ok`, `denied` (the wallet is not an active team key) or `expired` (five minutes passed, or this is not the browser the code was made for). With `ok` it sets the `team_pass` cookie: reload the page |
| `/team/auth/` | the wallet | LNURL-auth's callback (LUD-04) |
| `GET /team/poll/` | the invite's page | The same answer as the poll above, for the code the `__Host-team_k1` cookie names: an invite's code is never in an address |
| `/team/check/` | Caddy | 204 with a valid pass, 401 without |
| `GET /team/join.js` | the invite's page | Its script |
| `/team/join/<token>/` | the invited person | The invite's page |

Past a limit the first two answer 429 with `{"error": "…"}` and a `Retry-After` header. With the feature off they do
not answer JSON at all: Caddy serves your own page at any address, so treat an answer that is not JSON as "off".

A minimal script, to put in a file of your `MAINTENANCE_DIR` and call from whatever gesture you choose. If your page
sets a content security policy, `script-src 'self'` and `img-src 'self' data:` are enough for it:

```js
// team.js: signIn(document.querySelector("#team")) shows the code inside that element and reloads when a team wallet signed
async function signIn(box) {
  const asked = { credentials: "same-origin", cache: "no-store" };
  let code;
  try { code = await (await fetch("/team/login/", asked)).json(); } catch { return; }  // off: not JSON
  if (!code.svg) return;                                                               // 429: code.error says why
  const image = new Image();
  image.alt = "Team sign-in code";
  image.src = "data:image/svg+xml," + encodeURIComponent(code.svg);
  const link = document.createElement("a");
  link.href = "lightning:" + code.lnurl;                                               // a tap opens the wallet on a phone
  link.append(image);
  box.replaceChildren(link);
  const timer = setInterval(async () => {
    let state;
    try { state = (await (await fetch(code.poll, asked)).json()).state; } catch { return; }
    if (!state || state === "pending") return;
    clearInterval(timer);
    if (state === "ok") location.reload();
    else box.textContent = state === "denied" ? "This wallet is not in the team." : "";
  }, 2000);
}
```

Your page stays a static page: it asks nothing until the gesture, so with the feature off, or for a visitor who never
makes the gesture, the under-construction page costs what it always did. Caddy asks Django about a visitor only when
the request carries a `team_pass` cookie.

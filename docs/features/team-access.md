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
the onion address it does not exist.

The team hears of every new key and of every new browser that gets a pass, in the moderators' chat
(`TELEGRAM_MODERATION_CHAT`) or, without Telegram, at the community's email. Telegram is optional. The message leaves
after the person's answer has been sent, so a chat or a mail server that does not answer neither delays nor fails a
sign-in: a message that cannot be sent is a line in the log of `web`. `manage.py preflight` says when the site has
neither, and nobody would be told.

## Inviting somebody

1. In `/admin/`, **Team invites → Add**: the person's name and a role (team, staff or moderator: for now only a
   label). The page that follows shows a link and its QR code, **once**: the site keeps a hash of the link, not the
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

It signs in to no shop and to no account, and the role changes nothing yet.

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
  asks for the password ([above](#a-pass-and-admin)).

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
  asked with the `team_k1` cookie (`HttpOnly`, `SameSite=Lax`, only for `/team/`): anybody can see a QR code, only that
  browser gets its answer.
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
  entrance. Any other address under `/team/` is the under-construction page, like the rest of the site.
- **Wallets are not robots.** The rule that refuses robots by their name (`Go-http-client`, `python-requests`…) lets
  `/team/auth/` through, as it does `/health/`: a wallet written in Go or in Python signs like any other.
- **Headers.** Caddy sends the check the visitor's own headers. Django reads the cookie, `X-Entrance` and
  `X-Forwarded-For`, which Caddy writes itself, and nothing else; nothing of the answer is copied onto the request.
  Another site block of `caddy.d` that sends to `web` must write `request_header X-Entrance onion`, as for the
  [private part of the pages](private-pages.md).
- **Data**: the name the admin typed, the role and the wallet's linking key, which is a key made for this site alone.
  No address, no email. See [privacy.md](../privacy.md#what-the-software-stores-where-and-for-how-long).

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

## For your own under-construction page

These addresses, and only these, stay reachable from outside while the site is under construction. Your page calls
the first two; the wallet, Caddy and the invite's own page call the others.

| Address | Who calls it | What it answers |
|---|---|---|
| `GET /team/login/` | your page | JSON: `svg` (the QR code as inline SVG), `lnurl` (the same code as text, for a `lightning:` link) and `poll` (the address to ask next). It sets the `team_k1` cookie |
| `GET /team/login/<k1>/` | your page, every two seconds | JSON: `state` is `pending`, `ok`, `denied` (the wallet is not an active team key) or `expired` (five minutes passed, or this is not the browser the code was made for). With `ok` it sets the `team_pass` cookie: reload the page |
| `/team/auth/` | the wallet | LNURL-auth's callback (LUD-04) |
| `GET /team/poll/` | the invite's page | The same answer as the poll above, for the code the `team_k1` cookie names: an invite's code is never in an address |
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

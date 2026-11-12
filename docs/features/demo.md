# A training site in five minutes

A new installation is empty: no shops, no certificates, an empty list for the day. Nearly everything the site
does for the people who run it is understood only with data in front of you. `manage.py demo_data` fills a **test**
installation with a small made-up community, in every state a merchant or the association meets, so that the volunteers
learn on it (who answers the phone, who helps a merchant sign up) and a merchant can see the site before signing up.

Nothing in it is real. It runs only on an **empty** test installation (a laptop, a development site made for training),
and the command refuses everywhere else:

- never on mainnet, and never in production mode;
- only in development mode, unless `BITMENTOR_DEMO=1` is set in `.env` on purpose (then maintenance mode too, on an empty
  site; the variable never lifts any other refusal);
- never beside anything real: one shop, one merchant's account that the command did not
  make, and it stops, whatever is set. Staff accounts are fine: somebody has to look at the dashboard. The site that
  serves real customers, even on the public signet, is therefore never touched, and neither is a copy of it.

## Make it

```bash
docker compose exec web python manage.py demo_data
```

It prints the shops and what each one is for. Run it again whenever you want: it puts everything back as it was, with
the dates moved to today. Do that before every training: no timer runs on a training site, so the shops' dates stay as
of the last run (the panels say «checked 4 days ago» after four days).

To sign in as an owner (a merchant) without a mailbox, ask for the sign-in links:

```bash
docker compose exec web python manage.py demo_data --links
```

Each link is the one the «send me a link» page would email: it lasts 15 minutes and works once, and opens a page with
a button. The owners have no password, and the accounts are plain merchants: none of them is staff. To see the
association's side, `/admin/today/` needs a staff account of your own (`manage.py createsuperuser`, then the admin's
password page). Use the owners' links, not an email of your own: a merchant's account that is not a demo owner makes
the site «not empty», and the next run refuses (delete that account in `/admin/` to go on).

To take it all away, and only it:

```bash
docker compose exec web python manage.py demo_data --remove
```

## The eleven shops

| Shop | What it is for | Where to look |
|---|---|---|
| The Daily Loaf | A shop where everything works: certified, its Lightning address answering, a staff register | The map and its page: the badge and the full icon. The owner's panel: every step of the path done. [Staff and shops kept apart](staff-and-shops.md) |
| Blue Door Cafe | The Lightning address works; the certificate waits for the owner's basic pass. Its owner has two shops | The panel: the certificate's milestone and what is missing |
| Paper & Ink Books | The first day half done: register ready, sign printed, the first sale not ticked yet. Same owner as the café | The first day in the panel. The practice register: see [The practice register](practice.md) |
| Petal Flowers | Just signed up, no wallet yet: the page is ready, the shop is not on the map | The panel, «connect your wallet». The customer's side: [The customer at the till](customer-path.md) |
| Hardware Hub | Certified long ago | The map: the badge, and the icon of how recently the address answered |
| Moonlight Gelato | The Lightning address stopped answering three days ago | `/admin/today/`: a problem, with what to say. The owner's panel says the same in plain words. [The guardian](lightning-guardian.md) |
| Needle & Thread Tailor | A staff register in use, and one turned off | The owner's panel, the staff registers |
| Chamomile Herbalist | Everything in order, without the certificate | The panel, the path |
| Grandma's Kitchen | Moved here from another site, waiting for its owner. Not on the map | `/admin/today/`: «the owner has not taken the shop yet». The claim link: see below. [Moving the shops](moving-shops.md) |
| Pencil Stationery | Moved here; the owner took it but has not connected a wallet | `/admin/today/`: «Moved here… no register yet» |
| The Old Record Shop | A historic shop: closed, certified when it traded, on the map with its own marker | The map's marker |

## A tour for the volunteers

About half an hour, with one person playing the merchant and one the association.

1. **The list for the day.** Open `/admin/today/`. On top, three numbers (registers taking payments, shops to look at,
   closed for a while). Below, problems first, then what to look at, then what to know. Read one line out loud: the shop, what
   happened, since when, what to do, what to say to the merchant. Do it for the Gelato and the moved shops.
2. **A call comes in.** The Gelato's owner phones: «customers cannot pay me». Sign in as `gelato` (from `--links`) and
   see what the merchant sees in the panel, then read [When a merchant calls](../helpdesk.md). The call does not need
   the shell: the panel and the list already say it.
3. **A merchant signs up.** Sign in as `petals`: the shop's page is ready and nothing else. Look at the steps to connect
   a wallet, but see the warning in «What is not real» before finishing them.
4. **The first day.** Sign in as `books`: the steps of the first day, and «Practise without money». Do the exercises of
   the practice register: a pretend customer pays, says they paid while the register waits, does not pay, and the
   phone switches off during a sale.
5. **The staff.** Sign in as `tailor`: the staff registers, one turned off. The sales of a staff register stay on
   its phone and reach the owner's, never the site: what does the owner do at the end of the shift? What does the
   association say when a phone is lost?
6. **A moved shop.** `demo_data --links` also prints the link that takes Grandma's Kitchen. Sign in as any owner (say
   `pencil`), open that link and press the button: the shop is in the account. Run `demo_data` again to give it back to
   its waiting state and do it with someone else.
7. **The certificate and the map.** Open the map: the Daily Loaf has the badge, the Record Shop its own marker. In the panel of
   `door`, the path shows what the café still needs for the certificate (the owner's basic pass).
8. **Show it to a merchant.** Open the shop page of the Daily Loaf and the customer's path from it: this is what a
    customer meets at the till.

## What is not real

- **The Lightning addresses.** The Lightning addresses of the demo shops end in `.invalid`, which belongs to nobody:
  nothing can be paid to them, and the register cannot make an invoice. Practise payments on the practice register.
- **The periodic commands skip them.** `check_lightning` and `publish_nostr` leave demo shops alone, or they would
  overwrite the stories with their failures (and publish made-up shops). What the guardian would write (its last
  probes, and the certificate that follows) is written by `demo_data` directly, as of the moment you run it. After a
  day, run it again. For the same reason the list for the day leaves their dates out of its «not checked for a while»
  warnings, the morning summary (`manage.py dashboard --send`) and the export of the shops (`export_shops`) leave them
  out, and `/admin/today/` and `manage.py dashboard` show them, since that is what they are for.

## What it never does

- It refuses to run on `NETWORK=mainnet`, with `SITE_MODE=production`, outside development mode without
  `BITMENTOR_DEMO=1`, and on any site that has something real (`--remove` always works: taking away is safe).
- It asks nothing of a Lightning provider, of an explorer or of Telegram, and sends no email.
- Every thing it makes is marked: the shops by an `external_id` that starts with `bitmentor-demo:`, the owners by the
  user name `demo:<name>` (the nine names it knows, and no other). `--remove` takes away exactly those. A demo account that owns a shop that is not a demo one stays.
- It never makes a staff account, and never sets a password.

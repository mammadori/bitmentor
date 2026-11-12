# The guardian of the Lightning addresses

A merchant takes the Lightning address from their wallet, and the site tries it once, when the shop is created. Then
the provider can change, the address be deleted, the wallet stop telling the register that a payment arrived: without
a check, the first to find out is a customer at the counter. `manage.py check_lightning` tries the address of every
shop with a register and Lightning, not suspended and not closed yet, with the same probe as the creation of the shop
(`shops/lnaddress.py`: HTTPS only, public hosts only, no redirects, limits of time and size): its metadata, an invoice
that nobody pays, and whether that invoice can say it was paid. It only reads: it never changes where a shop's money
goes, and the register is never blocked by it.

- **When.** A working address every 6 hours; after a failed probe, and while it does not work, every hour. A fault
  counts after two failed probes in a row at least 45 minutes apart, so a hiccup of the provider alerts nobody. One
  shop at a time, 3 seconds apart, at most 100 shops and 45 minutes per run (`--pause`, `--limit`, `--minutes`;
  `--all` tries every shop now). Run it every hour: `bitmentor-sentinel@<project>.timer` on a machine installed from
  images (10 minutes at most, holding the release lock so that no release starts halfway), otherwise
  `docker compose exec -T web python manage.py check_lightning` from cron.
- **Not the server's fault.** A provider answering 429 or 5xx is "do not know", not a failure. If this server resolves
  no names at all, the run is skipped. If more than half of the addresses of a run fail at once (at least 4, the ones
  already broken left out), nothing is counted and no merchant is told: only the association hears it. At most 4
  emails a day per shop, and 4 lines to the association, for an address that goes up and down.
- **Who hears about it, only when the status changes.** The merchant by email, in the community's main language:
  what happened, what customers see, what to do now; and a short email when it works again. Only to the email to get
  back in, confirmed with a link: not to the shop's public email, where anyone can write any address. The
  association on Telegram in
  `TELEGRAM_MODERATION_CHAT`, one message per run with the shops and the reason, and the link to the shops with a
  problem in `/admin/`.
- **Where it shows.** In the merchant's panel, a warning on the shop with what to do and a "Try again now" button
  (three times every ten minutes per shop, 15 seconds at most), or, in the merchant's path, "Your Lightning address works: checked today at 9:00". In
  `/admin/`, a column and a filter on the status, with since when. `manage.py preflight` says how many addresses do
  not work, and whether the guardian has stopped, without failing for it.

A shop closed for a while ([Closed for a while](vacation.md)) is still probed and the panel shows what is found, but nobody is
written to: when it opens again the merchant hears "welcome back" (with the fault, if there is one) and the association hears about the fault.

A shop with another Lightning address starts from scratch; a suspended or closed shop, or one without Lightning, is
not tried and loses its status.

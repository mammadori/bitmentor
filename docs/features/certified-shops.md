# Certified shops

A certified shop is a shop whose Lightning address works and whose owner has done the community's training: the
guardian of the Lightning addresses found the address the shop has today working (`lightning_status` OK,
[The guardian of the Lightning addresses](lightning-guardian.md)), and the owner has earned the basic pass of
[the merchant's training](merchant-training.md) (`Trainee.base_earned`). The site sees no sale, so the certificate rests
on what it can check, the address, and on the merchant's commitment, the training. Its page, its line in the list and
its card on the map carry a small "Certified shop". Once certified,
always certified: it is a fact of the past (`certified_month`, the first calendar month in the community's time zone in
which all of it held). The association can revoke a certificate from `/admin/` (action "Revoke the certificate", for
whoever has the right to change shops: not the moderators' group); the site then never certifies that shop again, until
"Allow the certificate again", which certifies it at once if it meets the rule, or when it gets there.

**The private part is opened by hand.** The badge is automatic, the private part of the pages
([Public and private parts of a page](private-pages.md)) is not: the owner of a certified shop sees it only once a person
of the association approved that shop in `/admin/` (action "Open the private part to the owner", for whoever may change
shops). The shop keeps when and who (`private_approved`, `private_approved_by`, read-only), and the shop's history in
the admin has the line. "Close the private part to the owner" takes it back; revoking the certificate takes it back
too, and a certificate allowed again waits for a new approval. A certified shop not approved yet, with a register that still trades, is a line of the
association's dashboard (`/admin/today/`) and the filter "Certified, private part waiting for approval" of the list.
A suspended shop gives no private part. The approval is for the owner who had the shop: when the shop changes owner
(from `/admin/`, a command or a shell) it is taken away, and the admin's history says so.

**One Lightning address, one owner.** Several shops of the same owner may share a Lightning address; shops of
different owners never. The new shop form, the wallet form and the one-time link that hands a moved shop to its owner refuse an address
already on a shop of another owner,
without saying which shop or whose; the address is compared as the register writes it (lowercase, no spaces), so
`Bar@Wallet.example` and ` bar@wallet.example` are one. A shop with no owner yet (imported, not claimed) does not
count. A shop whose address is also on a shop of another owner, historic shops included, is not certified (one
already certified stays so) and gives no private part until the share is resolved.
Data written before the rule is not changed: each such shop that still trades (the other may be
historic) is a problem in `/admin/today/` ("Its
Lightning address is also on a shop of another owner"), until one of the owners changes address. A share between two
shops that both no longer trade is not listed: neither takes payments, and the dashboard reads no more shops for it.

**A new address waits for the guardian.** When the owner changes the Lightning address from the panel
([Changing a shop's Lightning address](address-change.md)), the badge and the private part wait until the guardian sees
the new address work; the certificate and the approval are kept.

**The badge can be turned off.** `features.certified: false` in `community.yml` takes the badge, the certificate's
milestone and the private part for certified owners (approved or not) away, and keeps the icons of how recently the Lightning address
answered and every certificate already given: see [Turning features off](README.md#turning-features-off).

- **When it is given.** `shops.certified.refresh` certifies the shops that meet the rule (their address on no shop of
  another owner) and are not certified yet: after
  a probe of the address that worked, after an owner earns the basic pass, and at every run of the guardian
  (`manage.py check_lightning`, every hour with `bitmentor-sentinel@<project>.timer`). A shop whose owner changes its
  Lightning address meets the rule again only once the guardian has found the new one working.
- **What the merchant sees.** In the panel the rule is written out, with what is still missing (the address, the pass);
  a certified shop's page and the badge on the map say what it means, true whatever the rule was when the shop was
  certified.
- **A shop closed for a while.** The rule does not move for a closure ([Closed for a while](vacation.md)). Only "how
  recent" below counts the days the shop was open.
- **How recent.** For each shop whose Lightning address has answered the guardian at least once, the map shows a
  Lightning icon, full when the address last answered (`lightning_worked`) in the last week, then paler for the last
  month, the last year, and older, counted in whole days of the community's calendar, without the days the shop was
  closed for a while (a budget of 92 days in the last 365). Where the community turned on-chain on (experimental, off by default), a
  shop whose owner says the register takes on-chain too (`Shop.onchain`) has an on-chain icon, with no band: the site sees no on-chain payment. The map card says each icon in
  words.
- **What leaves the site.** `/shops.json`, the map and the pages get `certified` (true or false) and, in `paid`, the
  Lightning band (`week`, `month`, `year`, `older`), or `null`: never dates. The fields on the shop (`certified_month`,
  `certified_revoked`, `private_approved`, `private_approved_by`) are written only by the site's rule and the admin
  actions, shown read-only in `/admin/` with a "Certified" column and filter, and read by
  `shops.certified.owns_certified_shop(user)` and, for the private part, `sees_private_part(user)`.

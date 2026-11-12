# Changing a shop's Lightning address

The owner of a shop with a register changes its Lightning address from the panel («Edit», then «Change the Lightning
address», at `/shops/lightning-address/<id>/`). The shop stays the same: its page, its place on the map, its history, its
registers and its printed sign, whose code leads to the shop's page (`/shops/s/<id>/`) and not to an address. No new shop,
no duplicate on the map. The code is in `shops/address_change.py`.

**Who, and how.** Only the owner, from their own panel. The administration never changes where a shop's money goes
([Staff and shops kept apart](staff-and-shops.md)): staff and moderators who are asked tell the merchant where the button
is. The page asks a sign-in of the last 15 minutes in that browser, with the wallet or with the email link: an open
session left on a phone, or a stolen cookie, is not enough; the page offers «Sign in again» and comes back. A suspended shop, or one
closed more than a year ago, cannot change it; a shop with no register yet connects its first wallet as before (`/shops/wallet/<id>/`).

**The checks.** The new address goes through the same rules and the same live probe as the first one (LUD-16, and LUD-21
`verify` so that the register sees payments arrive), and the rule «one Lightning address, one owner»
([Certified shops](certified-shops.md)): an address on a shop of another owner is refused. The write happens only if the
shop is still as the owner saw it.

**The pace.** At most one change every `LIGHTNING_CHANGE_DAYS` days per shop (environment variable, 7 by default), to slow
down whoever took over an account. The same wait follows the confirmation of a new recovery email
(`Profile.email_changed`): whoever holds an open session could set an email of theirs, sign in with its link and so
look like a recent sign-in, and the confirmation of the change would then go to that email. Sooner, only the association can help, and not by changing the address: by talking to
the merchant.

**Who is told.** The change is written in the register of account changes and confirmed to the owner by email and in the
Telegram chats of their shops, with the address short, and the association hears it in the moderation chat
(`shops/guard.py`). The panel says it for 14 days, next to the shop, with «if it was not you, write to the association».

**The guardian and the badge.** The guardian of the Lightning addresses starts again from scratch on the new address at
its next run. Until it sees the new address work, a certified shop does not show the badge and its owner does not see the
private part of the pages; `certified_month` and the association's approval are kept, since the owner did not change, and
both come back on their own.

**The registers.** The owner's phone remembers the address it takes payments into: on the next opening it stops and shows
the old and the new address until the owner confirms. The staff registers stop until the owner gives each a new pairing
code. A register left open keeps the old address until it is opened again. For three days the old address's provider stays
in the register's Content Security Policy, so the sales already shown on the old address are still checked with its
`verify` and recognised as paid.

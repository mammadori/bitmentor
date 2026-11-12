# Staff and shops kept apart

The people who run the site (the staff, who sign in at `/admin/`) must not be able to pass for a merchant, and must not
find it easier than the merchant to change where a shop's money goes or who controls the shop. This page says what the
site does about it, what it only watches, and what nothing on the site can stop.

## What the staff can and cannot do

**In `/admin/`, with the permissions the account was given:** see the shops, suspend them from the map, make them
historic, revoke or allow the certificate, delete a shop, change a shop's public
details (what customers see of them is the owner's choice, [see here](shop-details.md)), create and change accounts (password, staff status, groups), read the registers.

**The moderators' group.** Whoever only looks after the shops (reads the day's list, answers the requests for help) needs none of the rights that change a shop. They belong to the **Moderators**
group, which the site makes by itself with exactly the rights that work asks and no right to change a shop, an owner, an
account, a key or an address; the rights to change shops, to make accounts and to give groups stay with the few who
run the site ([The moderators](../operations.md#the-moderators)). The actions that revoke or allow a certificate ask for
the right to change a shop too.

**Not even there:** the shop's destination (its Lightning address) can be *seen* in `/admin/` by whoever can see a shop,
read only, and is in no form that changes it. The on-chain destination (where the community turned on-chain on: it is
experimental, off by default) is never on the site: it stays on the owner's phone ([On-chain at the register](onchain.md)). A shop's *owner* and the *Telegram chat* of its notices can be changed there, by whoever has the right
to change shops: changing them is watched (see below).

**Registers for the shop's staff and their on-chain chain.** Only with on-chain turned on: a shop's registers for its
employees (`StaffRegister`, not the site's staff) take on-chain payments on the owner's receive chain, `…/0/n` (or the path the owner set), like
the owner's register, so that they show in the owner's wallet; the site keeps one count per shop and chain and gives
each index once (`ChainCount`, [On-chain at the register](onchain-notes.md#one-chain-for-all-the-shops-registers)). It knows
a hash of the chain's key and a number, never the key nor an address: changing them moves no money, at worst it skips
addresses or makes a register refuse on-chain sales.

**Not read by asking either.** Django's admin filters a list by any field of the model if the address says so
(`?claim_hash__startswith=ab`), and the answer, one row or none, would give a value away one character at a time to
whoever may see the list. On every admin of the site (`config/admin_secrets.py`) a filter on a field that no page, column,
filter or search of that admin shows is refused with a 400: the claim of a shop, for one. A field added to a model is
hidden that way until the admin shows it. `config/test_admin_secrets.py` goes
through every admin, for a superuser and for an account that can only see.

**Outside `/admin/`, never:** use the pages and actions of a merchant. A staff account (`is_staff` or `is_superuser`)
and any session that was born from the admin's password are, on every page outside `/admin/`, a visitor who is not
signed in. So the panel, the shop's edit form, the recovery email and its confirmation, the guide to connect the wallet,
the registers of the employees, the printable sign, the card for the
till, the first day, the practice register, the claim link of a moved shop:
all of them answer the staff as they answer a stranger. A staff account already could not sign in with the wallet or
the email link; now it cannot use a session of the admin for a shop either, and taking `is_staff` away afterwards does
not help: the session remembers where it was born.

**What the staff can still do, and the site only watches:** an ordinary account (not staff) that the staff changed. For
example the staff replaces the `username` of a merchant's account (the key their wallet signs in with) with a key the
staff controls, and then signs in with the wallet as that merchant would. Or hands a shop to another account, or points
the chat that receives the shop's notices somewhere else. The site cannot refuse this, since it does not know whose
hands are on the wallet; it makes it **visible** (see «The watch»).

**What nothing on the site can stop:** whoever has a shell on the machine or the database can do everything, and can
also delete the register and the messages' trace: keep the shell to the fewest people, and let two people look at what
changes. The site is a witness, not a lock; against someone who holds the keys
only another person's eyes help.

**Where the money goes.** The Lightning address is never written from `/admin/`: it is read-only for everybody, superusers included, and a write that comes from a page
of the administration (any module, any action, a hand-made POST) is refused. Every change of that column, from a `save()`,
an `update()` or a `bulk_update()`, from a command or a shell, is written in the register of account changes and said to the
owner (email and the chats of their shops) and to the association; the owner's first connection of a wallet from the
panel is only confirmed to the owner, and the owner's change of an existing address from the panel
([Changing a shop's Lightning address](address-change.md)) is confirmed to the owner and told to the association. The notices carry the Lightning address short
(`ma…@provider`), never whole. At the till, the register of the owner and of the staff and the card for the till say
«Paying to:» with the Lightning address whole, so that whoever
stands there sees where the money goes; the plain link of a register, which opens for whoever knows the shop's number, says
the address short (`ma…@provider`) on its screens. That is not a secret kept from whoever opens it: the same page carries the
address whole in the register's data (`register_data`), since the phone asks it for every invoice. The short form only
keeps the address off the screens of a register opened by a stranger. Not seen: SQL, `QuerySet.raw` and fixtures loaded with `loaddata`, which skip Django.

## The guard

`config/guard.py`, the middleware `StaffApart`, sits right after the accounts. Outside `/admin/` (the only place where
the staff is the staff) it replaces `request.user` with a visitor who is not signed in, whatever the account. Nothing
below it, view, template or helper, ever sees a staff account on a page of the site: a page added tomorrow is covered
without anyone having to remember it.

- A staff member who opens a shop's page lands on the sign-in page, which says why in a line and has a button to sign
  out of the administration.
- Signing in again as a merchant (the wallet, the email link) ends a session the admin's password made: an account
  that lost `is_staff` is the owner again as soon as the owner signs in.
- A staff password handed over in person is marked with `manage.py temporary_password <username>`
  (`config/temporary.py`): whoever signs in to the admin with it is sent to the admin's change of password from every page
  of `/admin/` but the change itself, its «done» page and the sign-out. Outside `/admin/` the staff is a visitor and
  nothing sends it anywhere. The mark is the hash the password had, so it ends by itself when the password changes; an
  account that is not staff is never sent anywhere.
- A volunteer who also has a shop uses two accounts: the staff account for `/admin/`, and an ordinary one for the shop
  (sign in with the wallet or the email, as every merchant does).
- `shops/test_guard.py` keeps it so: it walks **every URL of the site**, GET and POST, with a staff account that owns
  a shop and a session from the admin's password, and wants, for each, the same answer a stranger gets and a view that
  saw a visitor not signed in. A new page needs no entry anywhere; a way around the middleware fails the test. It also checks that the
  admin still opens, and that a page invented in the test is covered.

## The watch

`shops/guard.py`. For the owner of at least one shop, these changes are watched:

| What changes | Code in the register |
|---|---|
| the password, `is_staff`, `is_superuser`, `is_active` or the `username` of the account | `password`, `is_staff`, `is_superuser`, `is_active`, `username` |
| the groups or the permissions of the account (from the user's side or from the group's) | `groups`, `permissions` |
| the account deleted (its shops go with it) | `deleted` |
| a shop that changes owner, or whose notices' chat changes, from `/admin/` or a command (the owner's own edit form is the site's way: not watched) | `shop_owner`, `shop_chat` |

Each change does four things, in the same transaction as the change:

1. **Writes it in the register** (`AccountChange`): what changed, when, who (the staff account, when the change came
   from a page of `/admin/`), from where (`the admin`, `the pages of the site`, or `a command or the shell`), the names
   of the shops, who was told and who could not be reached. The names of what changed, never their values: no password
   and no key is written there.
2. **Marks the account**: `Profile.account_changed` takes the date.
3. **Tells the owner**, once the change is saved for good: an email to the recovery email, and a message in the Telegram
   chat of each of their shops (for a changed chat, the chat it *left*). In the community's first language, with the
   sentence in everyday words and what to do: «if you know why, nothing; if not, write to the association now».
4. **Tells the association**, in `TELEGRAM_MODERATION_CHAT`, with the link to the register. Without the chat, or without
   the bot, it only stays in the register; the register says so («not told»).

The association is told first, then the owner's email, then the owner's chats. «Told» means that the server took the
message: Telegram that refuses it (a wrong chat, a blocked bot), does not answer, or is left without time counts as not
told, and so do the chats beyond the first five of an owner with many shops. A round of messages starts no new attempt
after 20 seconds, and after Telegram fails to answer it does not try the next chat; each channel fails on its own.

One form of `/admin/` that saves the user and then the groups makes one row and one message, not two. A failed email or
Telegram never fails the change; the register says what did not arrive. An owner with neither email nor chat is told by
the **panel**: for two weeks it opens with the date and the sentences of what changed. **The panel does not reach an
owner the change put out:** a replaced `username` (their wallet now opens an empty account), an account switched off,
one that became staff, or a new password that ends their session cannot see the panel. For them the email and the chat
are the only messages, and the association has to call.

**The register** is `/admin/shops/accountchange/`, newest first, in sentences: «The account of Red Bar was changed
from the admin by boss.» It is read only: nobody adds, changes or deletes a row there, not even a superuser. The
permission to see it is `view_accountchange` (a superuser has it). `/admin/today/` says how many changes there were in
the last two weeks, with the link.

### Using the mark

Today no function acts on the mark; the panel only says it. A sensitive action of the future (changing where the funds
go, handing a shop over, adding an email) can ask more care while an account is recent:

```python
from shops import guard

if guard.recently_changed(request.user):   # the last two weeks; recently_changed(user, timedelta(days=3)) for another window
    ...  # ask for the link by email again, or wait, or send it to the association
```

### What it does not see

A change that skips the signals (`QuerySet.update()` on the accounts, SQL) is not seen, and neither is a change to
a group's permissions (the group's members are not touched in their own account). Everything done with the shell or the
database can also be erased.

The changes to the moderators' group are not seen either, since a moderator owns no shop and the register is for the
owners' accounts: `manage.py moderators add` refuses an account that owns a shop, and `remove` works on one, and then
writes a row. Who the moderators are is checked by looking, with `manage.py moderators list`
([the handbook](../operations.md#look-at-the-moderators)).

### When the message arrives

For the association: open the register. If it was a volunteer doing what was agreed, nothing. If nobody knows: first
look at who has staff accounts (`/admin/auth/user/`), change their passwords, and ask the owner by a channel the
account's change did not touch (a phone call to the number of the shop). Then check the shop's Lightning address with
the owner.

## The web server's logs

Caddy's error log (a 502 during a release writes the whole request) drops from the query the secrets that open
something: `k` (the key of a staff register), `c` (the one-time code of a register), `code` (the link that takes a
moved shop), `token` (the links in the emails, to sign in and to confirm the recovery address) and `next` (the page to
go back to after signing in, which carries the others inside it: `next=/…/claim/?code=…`), and the `Referer` and
`X-Register-Key` headers. The access log, when turned on, keeps no query and no header. `config/test_server.py` reads
the `Caddyfile` for it, and lists every query parameter the site reads: a new one is either dropped there or written
down as harmless, with the reason. A proxy in front of the site has to do the same.

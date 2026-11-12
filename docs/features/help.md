# Something wrong?

A merchant at the till, with a customer in front of them, who sees the payment not going through has nothing to look at
and nobody to ask but a phone call, and whoever answers must run `diagnose_shop` from a terminal. «Something wrong?»
puts the diagnosis in the merchant's hands, in their words, and, when it is not enough, one tap sends the association a
request that already carries it.

## What the merchant sees

In the panel, each shop has a card with the button **Something wrong?**, and the owner finds the same link at the top of
the register, where they are at the till (`/shops/help/<shop>/`, for the shop's owner
only: another merchant gets a 404, a visitor the sign-in page, and the staff and the sessions of `/admin/` are visitors
here too, see [Staff and shops kept apart](staff-and-shops.md)). The page starts from the symptom, as buttons large
enough for a thumb:

| Button | What the page looks at |
|---|---|
| A customer cannot pay | the shop's state (suspended, no register, waiting for a wallet), the guardian's last verdict on the Lightning address, the register, a closure; with «Look again»: the Lightning address, asked just now |
| A payment stays on «waiting» | the same; the sales and how each ended are on the phone that took them, never on the site |
| The register does not open | the owner's account (a recovery email, a staff account), the changes made to the account, the registers of the staff (never opened, expired code, turned off), a shop moved here |
| I lost my phone or changed phone | the same, with the steps to get back in and to turn off the staff registers on the old phone |
| Too much change given, or a customer paid too much | a moved shop or one waiting for its wallet; the steps to give the extra back from the wallet |
| Something else | everything the site reads from its own database |

Under each button: **what the site knows** about that shop (a problem or something to look at first, what works folded
under it), in the same words `diagnose_shop` uses with the merchant (they already speak to them in the second person),
and **what to do now**, step by step. The words never name the protocols: the details for whoever helps stay in the
request.

**The page loads from the database only.** Nothing slow is asked while it loads: no Lightning
provider. «Look again, asking your Lightning wallet» (a POST, up to 20 seconds, with the diagnosis' own time limits per
call: `diagnosis.CALL_TIMEOUT`) adds the check that needs it, and says so. It is limited to 6 per shop per hour and 30
per person, since the Lightning check asks the merchant's provider for a real invoice that nobody pays.

## Asking the association

On the same page: «Ask for help». A line of plain text, optional (up to 200 characters; a private key or a seed phrase
is refused, and one line only), and how to be called back, among what the shop has: the recovery email (shown masked),
the shop's Telegram chat, the phone number of the shop page, or «I will look at my panel».

The request (`HelpRequest`, `shops/helpdesk.py`):

1. is counted under a lock on the shop: **3 per shop in a rolling day** (`limits.help_requests_per_shop_day`), and 30 per person per hour. The
   same symptom asked twice within two minutes by the same person is one request (a double tap); a second line written
   in the second tap is added to the first request;
2. is saved at once, so that nothing is lost whatever happens next;
3. gets its **diagnosis**: the checks of the symptom, the one that asks the Lightning provider too, in the association's
   language and within 10 seconds (`ASK_SECONDS`); as with `diagnose_shop`, the Lightning check asks the merchant's
   provider for an invoice that nobody pays. If the provider does not answer that is itself what the diagnosis says. A
   diagnosis that breaks never loses the request;
4. is written to `TELEGRAM_MODERATION_CHAT`: the shop's name, the symptom, how to call back (only the way, never the
   address or the number), the merchant's line as plain text (no web address, no `@` mention, no `/` command), the
   **codes** of the problems and of what to look at (`lightning_no_answer`, `staff_off`, see [When a merchant
   calls](../helpdesk.md)) and the link to the request in `/admin/`. Never a key, a token, an
   address or an email. A shop of the training site ([A training site in five minutes](demo.md)) never writes to the chat.

The messages to the chat have a ceiling of their own: **20 an hour for all the shops** and 6 an hour for each network
(`MESSAGES_IN_ALL`, `MESSAGES_PER_IP`). Beyond it the request is saved with `delivery` `failed` and is only on the
dashboard, and the merchant is told so: a chat that nobody can follow is worse than a list. The first line of the
message is the community and the site, with the network when it is not mainnet, as in the morning message.

If Telegram is not set up, refuses or does not answer, the request stays: it is on the dashboard all the same, and the
merchant is told that the message did not go out and that the request is saved (with the community's email for an
emergency). `HelpRequest.delivery` says which: `sent`, `refused`, `off` (no bot or no chat), `failed`.

## What the association sees

- **`/admin/today/`**: the requests not yet solved are on top of everything else, the oldest first, as a problem: the
  symptom, since when, the merchant's line, how to call back, the `diagnose_shop` command and the links to the request
  and to the shop. `manage.py dashboard` prints them too, and the morning message counts them (naming the shop and the
  symptom, never the merchant's line).
- **`/admin/shops/helprequest/`**: the list (waiting or solved, by symptom), and each request with the diagnosis as
  the helper reads it (a line per check, its code and its detail, with no secret by design: see
  [helpdesk.md](../helpdesk.md)), and where to call back (the recovery email, the shop's chat, the shop's phone, read at
  that moment). Nothing here is edited or deleted. The action **Mark as solved** writes who and when
  (`resolved_by`, `resolved`); **Mark as waiting again** undoes it.

The request keeps who asked (`asked_by`): after a change of owner the new owner does not read the old one's requests, which
the association still sees.

**Permissions.** The requests have their own permissions in `/admin/` (`view_helprequest`, and `change_helprequest` for the
two actions). Both are in the **Moderators** group, which the site makes and keeps right by itself at every start (`migrate`),
and which `manage.py moderators add <name>` gives to a volunteer: nothing is to be given by hand, in Groups or anywhere
else ([The moderators](../operations.md#the-moderators)). For an account with rights of its own: without `view_helprequest`
the line is on the dashboard but its link to the request is not shown (and the request's page answers 403), without
`change_helprequest` the actions are not offered.

The merchant sees in the panel «You asked for help today at 10:32», and after the action «Solved today at 11:05» for
three days; the page keeps the latest five requests.

Besides the checks of the diagnosis, the page and the request add what the site keeps on the shop and the diagnosis does
not ask: `address_broken` or `address_works` (the guardian's last verdict on the Lightning address, [The guardian of the
Lightning addresses](lightning-guardian.md)), for the symptoms where a customer cannot pay or a payment waits.

## How long a request is kept

A request holds things about a person: the line the merchant wrote, the copy of what the site knew about the shop when they asked
(the diagnosis: their Lightning address, the start of their wallet key, when they last signed in, their recovery email as
`m***@e***.org`), the account that asked, how they wanted to be called back and the volunteer who solved it. A year after it was
solved none of that is of any use, so `manage.py forget_old_requests` puts it away:

- **What goes:** `note`, `summary`, `asked_by`, `channel` (back to «they will look at their panel») and `resolved_by`.
- **What stays:** the shop, the symptom, `created`, `resolved` and `delivery`, so that the counts do not change.
- **Which requests:** the ones **solved** more than the number of days ago, counted from `resolved`, not from `created`. A request that
  is open, waiting, or marked as waiting again is not touched, however old; neither is one solved less than that.
- **How many days:** `privacy.help_requests_days` in `community.yml` (365 if it is not written; 30 to 3650; `0` or `false` and the
  timer never forgets). It is the number the timer uses; `--days` on the command line is for a run by hand, and under 30 it asks for
  `--force`.
- **How it runs:** by itself every morning, with `bitmentor-dashboard@<project>.timer`, before the morning summary; running it again
  does nothing. `--dry-run` says how many ([the handbook](../operations.md#forget-old-requests-for-help)). If the machine's clock looks
  wrong (the guardian's last probe of a Lightning address is more than two days old, or in the future) it forgets nothing and says so.

A forgotten request is no longer in the merchant's history (it is no longer tied to their account) and the association still sees it,
solved, in `/admin/shops/helprequest/`.

**What it does not reach.** The merchant's line is also in the message the moderators' Telegram chat received when the request was made:
turn on the automatic deletion of messages in that chat. And the backups made before keep the line as it was, for as long as the
destination keeps them: by default the first copy of each month for 12 months, so up to about two years after the line was written.

## Limits, on purpose

- The merchant cannot write more than a line, and the answer does not come back through the site: the association calls
  back, and marks the request solved. There is no chat, no ticket thread.
- Nothing here changes the register, the sign, the staff registers or **where the money goes**: «Something wrong?»
  never offers to set an address or a key, and the steps never ask for one. A merchant who changed wallet is told to ask
  for help, and the association shows them how: only they connect the new wallet.
- The page is an aid, not a monitor: it says what the site knows now. What happened at the till stays in the merchant's
  eyes: the steps start from there.

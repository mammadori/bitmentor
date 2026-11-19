# The panel by stages

A new merchant opening the panel used to meet everything at once. With the stages, the top of the panel shows, for each
shop, only the stage it is at: one step, one button. Everything the panel had before stays below, unchanged and closed,
under «Everything, if you want»: closed, never hidden, never removed. Nothing is locked by the stages: the only things
that wait are the ones that already waited for safety (the till waiting for the basic pass of the training, and
[the sign waiting for the approval](shop-page.md#the-sign-waits-for-the-approval)).

On by default; a community turns it off with `features: {stages: false}` ([Turning features off](README.md#turning-features-off)):
the panel is then exactly the one without stages.

## The stages

Each stage opens when its condition holds; the panel shows the latest one that has something to do. Nothing is stored:
the stage is worked out at each visit from what the site already knows (`shops/stages.py`).

| Stage | Opens | The step |
|---|---|---|
| 1. The shop and the wallet | From the sign-up, until the shop has a Lightning address that answers (also when the guardian finds it broken) | Connect the wallet |
| 2. The trial | When the wallet is there, unless the till waits for the pass (4) | The practice register, then a first real payment of the owner's own, ticked as on the first day (`first_sale`) |
| 3. The first takings | From the first payment ticked | The real register; with the training's `quick_start`, the amount it may take before the basic pass and, on the phone, how much of it is still left; the pass proposed |
| 4. The basic pass | While the training locks the till (`gate: unlock` on a real-money site, without a quick start): then it is the only step, from the sign-up on, before the trial too, since the till takes no real payment. Once a quick start is used up the register itself asks for the pass (only the phone knows), and stage 3 says so on that phone | That the till waits for the pass; the terms and the basic modules |
| 5. On the map | When the shop is on the public lists: approved (`approved_on`, whatever `features.approval` says now: a new shop saved with the approval off has it at once, a shop still waiting from before does not), listed, not suspended, with a position | The sign to print, the card for the till, the shop's page; until the sign is ticked as printed |
| 6. The routine | `routine_days` after the first sale (7), or `routine_sales` sales on the phone (5) | Closing the day, the file for the accountant, registers for the staff |
| 7. The rest | Always, closed under each shop | On-chain (when turned on), Nostr copies (with `register.relays`), closing for a while, OpenStreetMap, the discount, changing the Lightning address: only those that exist on the site |
| 8. Going further | `game_days` after the first sale (30), or `game_sales` sales on the phone (20) | The other modules of the training and its badges |

A stage whose facts do not exist is skipped, never shown empty: a community without the `training` block has no stages
4 and 8 and no quick-start wording; a shop not on the map has no stage 5 (nor has a shop the community set aside, which walks the others as it is, with
the line on top saying that it will not go on the map instead of telling it to wait for an approval); a historic shop has no stage at all, with or
without a register, and neither has a shop with its own till. An owner with several shops sees each shop's stage, one under
the other.

The notices about safety stay on top, outside the fold, as before: the account changed by the staff, the wallet to
confirm, the Lightning address that does not answer or was changed, a suspended or unlisted shop, a shop waiting for
its approval, the shop waiting for its wallet, «Something wrong?». The answers of «Try again now» about the Lightning
address are said once, on top. «Your account» stays outside too.

The training's card («Your merchant pass») is inside the fold in every state: the stages say what matters from it (stage
3 how much the quick start still takes, stage 4 that the till waits for the pass). With the stages off it is where it
always was, on top while it holds the till.

A link of the site that leads into the closed part (the first day after a tick or the practice, «Closing for a while?»
after a change) opens the folds around its target: the panel's script does it when the page opens and when the fragment
changes. Without JavaScript the messages on top still say what happened.

## Sales or days, whichever comes first

The site never sees a sale and keeps no count of them. The days since the first sale are the server's; the sales are
counted on the phone: a small script of the panel (`static/stages.js`) reads the register's own history of each shop in
that browser (the same one the register keeps, `register-ledger:<shop>`), counts the paid and confirmed sales, and when
there are enough shows the later stage in place of the current one. For the quick start it adds up the payments
taken (paid, confirmed, or seen on the chain, in the community's currency) as the register does, and says how much is
left. It only reads; nothing is written or sent anywhere.
That is why the same owner may see a stage open on the shop's phone before the computer: the computer has no history
of the register, so there only the days count. Without JavaScript the days decide.

## The numbers

```yaml
stages:
  routine_days: 7
  routine_sales: 5
  game_days: 30
  game_sales: 20
```

Days from 1 to 365, sales from 1 to 1000. A missing value is the default; a wrong one counts as the default, and
`manage.py check` says so (`shops.W015`).

## For a community's own templates

`shops/panel.html` keeps its blocks, ids, classes and variables: a template that extends it and overrides `head` or
`content` renders as before. The new part is in a block of its own, `stages` (`shops/stages.html`, each step in
`shops/stage_card.html`); the variables `stages` (whether the stages are on) and, on each shop, `stage` and `stage_rest`
are new.

The block and the fold go together: a template that empties `{% block stages %}` keeps only the closed «Everything, if you
want». To have the panel without stages, turn the feature off (`features: {stages: false}`) instead of emptying the block.

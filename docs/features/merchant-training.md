# The merchant's training, the «pass»

A shop is run by one person who has to know a few things nobody else can know for them: the wallet and its recovery
words are theirs, a sale is paid only when the money is in their wallet, the code at their till can be swapped, and a
self-custodial wallet (for example on Spark) has no one to call when the words are lost. bitmentor does not hold funds and keys, and an association of volunteers must
not become the one who runs a shop. The pass is a short path, with a little game, that makes the merchant **informed and
accompanied**, and keeps the written proof that they were.

It is off unless `community.yml` has a `training` block. The software has no text of its own for the modules: the
community writes them (and the questions, and the statements), so the community answers for what it says.

Where the community turned on-chain on (it is experimental, and off by default: [On-chain at the register](onchain.md)),
it has a small place in the pass, as in the practice register: one of the cases to teach, the remedy when the
customer does not understand the Lightning code (their wallet may pay only on-chain), never a module of its own nor the
first way to take a payment. The software follows that: «OK, on-chain» is a small link under the keypad, and the
practice offers it only in its exercise «The customer does not understand your code» ([The practice register](practice.md)).

- [What the merchant sees](#what-the-merchant-sees)
- [What the community writes](#what-the-community-writes)
- [The declarations: the proof](#the-declarations-the-proof)
- [The game: levels and badges](#the-game-levels-and-badges)
- [The unlock of the real till](#the-unlock-of-the-real-till)
- [The registers of whoever works with the owner](#the-registers-of-whoever-works-with-the-owner)
- [What the association sees and exports](#what-the-association-sees-and-exports)
- [Going back, tests and limits](#going-back-tests-and-limits)

## What the merchant sees

- **A card in the panel**, on top: «Your merchant pass», with a bar («Your path: 3 of 8 done»), the badges, and one
  button that goes on from where they stopped. Messages only when they matter: the register waiting for the pass,
  the yearly review, a text that changed.
- **The path** (`/shops/training/`): the modules in order with their state (to do, questions done and to confirm, done,
  the text changed), and under them the steps to declare after a guide, and the review.
- **A module** (`/shops/training/<id>/`): a button to the community's page to read, then the questions, then the
  statements. **The quiz has no penalty and no limit**: who gets one wrong reads the line that explains it, the right
  answers stay chosen, and tries again. The statements appear after the questions.
- **Everything works without JavaScript**: plain forms, the answers graded on the server, which is the only place that
  knows the right ones. No script was added to these pages.

When the community has `merchant_terms` (or `help_terms`: both names are read), a page of the community that says what the
association's help is and what stays with the merchant, accepting it is the **first statement of the path**, as the module
`terms`.

## What the community writes

The `training` block of `community.yml` (a complete example, commented, is in `examples/community/community.yml`):

```yaml
training:
  gate: unlock          # or reminder: only reminders, never a locked register
  quick_start: 20       # optional: the owner's register takes this much in total before the pass (0 or missing: off)
  since: 2026-12-01     # optional: the day the training starts to count (missing: at once)
  review_months: 12     # the short review
  refresh_days: 30      # days after the review is due before the «Trained merchant» sign goes
  public_sign: true     # the sign on the shop page, the map and /shops.json
  guides:               # pages for the steps declared after a guide
    backup: backup-and-withdrawal/#backup
    withdrawal: backup-and-withdrawal/#withdrawal
  staff_rules:          # the three rules of the counter, one text per rule (or per language)
    - {en: "Paid only when the register says so…"}
  modules:
    - id: wallet                       # letters, digits and dashes; not terms, review or declaration
      title: {en: "The wallet is yours"}
      page: learn-counter/             # a page of the site, as in shop_page
      base: true                       # it counts for the basic pass (the default)
      questions:                       # 2 to 4 are a good number, up to 8
        - text: {en: "Who must know the recovery words?"}
          options: [{en: "Only you"}, {en: "The volunteers"}, {en: "Whoever asks kindly"}]
          answer: 1                    # the number of the right option, from 1
          explain: {en: "Only you: they are the key to your money."}
      declarations:
        - id: words-mine
          text: {en: "I alone keep the recovery words."}
```

- A text is a string or a string for each language, up to 600 characters. A language that is missing falls back to the
  first one the community wrote.
- What cannot be used is left out and the training goes on without it; `manage.py check` says what and where
  (`shops.W005`). A `training` block with no usable module keeps the training off.
- `gate`, `review_months`, `refresh_days` and `public_sign` have the starting values above when the community does not
  write them; `quick_start` and `since` are off.

## The declarations: the proof

Every statement the merchant ticks is a row of `Declaration`, written once and never changed:

| What | Notes |
|---|---|
| `key` | `module:statement`, or `step:practice`, `terms:accept`, `staff:rules` |
| `version` | the SHA-256 of the key and of the text **in every language**: any change of any translation is another version, a different order of the languages is not |
| `text`, `language` | the text exactly as it was shown, and the language of the page |
| `made` | the moment |
| `user`, `who` | the account, and its name as it was (the row says who even when the account is gone) |
| `shop`, `shop_name` | the shop the merchant was working on (the oldest with a register, or the one chosen on the path), and its name as it was |
| `register` | for the three rules of a staff register: the register |

**No address of the connection, and nothing of the phone.** The rows outlive the shop and the account (`SET NULL`, with the
names copied) and nobody edits or deletes them in `/admin/`.

When the community changes a text, the statement is **asked again**: the module goes back to «the text changed», the old
row stays, the new one is another row. The basic pass, once earned, is not taken back, and nothing locks for it.

## The game: levels and badges

Four badges, and the level is how many are earned:

| Badge | How it is earned |
|---|---|
| Basic pass | every module that counts, and the terms if the community has them |
| Practice done | the owner says it, after the exercises of the practice register (`practice` in `guides` is the register itself) |
| Backup checked | the owner says it, after the community's guide |
| Withdrawal tried | the owner says it, after the community's guide |

Where the community turned the practice register off (`features.practice`), the «Practice done» step is not offered and the badges are three.

The day closing lives on the merchant's phone ([History and day closing](end-of-day.md)): the site cannot see it, so it
gives no badge. The bar in the panel counts the modules and the
badges after the first.

The **«Trained merchant»** sign goes on the shop page, on the map card and in `/shops.json` (`trained`) while the owner has
the pass and the review is not late; `public_sign: false` keeps it off. The **certificate** of a shop (the badge of
[Certified shops](certified-shops.md)) needs the basic pass too, with a Lightning address that works: until then the panel says
what is missing, and the certificate comes as soon as the pass is earned. A shop already certified keeps it: it
is a fact of the past.

Every `review_months` a short review asks one question from each module, without penalty; it is reminded in the panel
before it is due and after, and the sign waits `refresh_days` more days. A late review locks nothing.

## The unlock of the real till

The register that takes real money (`NETWORK=mainnet`) **opens with the basic pass**. The rules, all of them made so that
nothing stops a sale in the middle of a day:

- **Only where the money is real.** Signet and the practice register always work, for every shop, with or without the pass.
- **Only the page.** The register page answers «The register opens soon» and a short explanation (HTTP 403). A sale
  already on a screen goes on to its end, and its history stays on the phone. The copy of the register kept on the phone
  goes with the till (its service worker removes it).
- **From the `since` day, or at once.** With `gate: unlock` on a real-money site, the register of an owner without the
  basic pass is locked from the day the community wrote in `since`; when `since` is missing, from the moment the training
  is turned on. Before that day nothing is asked and nothing locks. The day begins with the **business day**: until 05:00
  it is still the day before, so the lock never falls in the middle of an evening. A community that turns the training on
  for shops that already sell **sets `since` ahead and tells its merchants first**; `manage.py preflight` says how many
  registers wait for their owner's pass while `gate: unlock` has no `since`. A `since` that is not a day
  (`2026-12-01`) locks nothing: the training waits, and `manage.py check` says so, until it is written right.
- **More days for some owners** only through the community's own code: `training_grace_until` gives an owner a last
  day, the register stays open until that day ends and the panel counts the days; `training_practice_done` counts the
  practice as done for whoever practised elsewhere ([The community's own code](../architecture.md#the-communitys-own-code)).
- **`gate: reminder`** turns the unlock into reminders: the panel asks for the pass and nothing locks.
- **Never a lock because of a text, a review or an error.** A text that changed, a late review, a database that does not
  answer: the till opens. Demo shops and shops with no owner are never locked.
- The staff registers follow their owner: the owner's lock is theirs, and the page says to tell the owner.
- **The quick start** (`quick_start`, optional): the community can let the owner's register take its first payments at once,
  up to a small total in the community's currency (say 20), and ask for the pass only after that. See below.

### The quick start

The idea: a new merchant should be able to charge the first customers today, and do the training when the shop is really
running. With `quick_start: 20`, while the owner has no pass (and only then):

- **The owner's register opens**, signed in as the owner, and shows a line: «Quick start: 12.50 EUR of 20.00 EUR left
  before the training», with a link to the path. The panel says the same.
- **The staff registers stay locked** until the owner has the pass.
- **At the limit** a sale that would go over is refused with the amount that is left («Type less, or finish the training
  to take any amount»), and when nothing is left the register says «Quick start used up» and points to the training.
  Whatever is on the screen goes on to its end. After the pass the line is gone and nothing is counted any more.
- The practice register is not affected, and `gate: reminder` has no use for it (nothing locks).

How it is counted: **on the owner's phone only**, by adding up the sales of its history that are paid or seen on the chain (a QR
left unpaid, or a sale still to check in the wallet, uses up nothing), in the community's currency, at the amount typed less any discount. The
site never sees a sale, and keeps no counter: that is the design of the register, and a counter on the site would be a
second record of the sales. A history restored from a copy counts. What is accepted: whoever clears the browser's data (or
uses another phone) starts the quick start again; the quick start is a convenience for the first days, not a barrier against
a dishonest owner, who has the key to the wallet anyway. It counts only while the register works in the community's currency.


## The registers of whoever works with the owner

At the **first opening** of a register, before the first sale, the person reads three rules and presses «I understood»:
*a sale is paid only when the register says so; never give the recovery words to anyone; if in doubt, call the owner.* The
community can write its own (`training.staff_rules`, up to six, one text for each language); without them the software's
English ones are used. «I understood» is kept like a declaration for the register (no account: the register's name, its
shop, the text shown, the version). A register that has accepted them once is never stopped by them again, since it could be in the middle of a
sale: a new text of the rules is asked of the registers paired after it. The page has a
button to the staff practice register too.

## What the association sees and exports

- **`/admin/shops/declaration/`**: the declarations, newest first, by statement, language, shop, who and version. To look
  at and to export: the action **Export the selected declarations as a spreadsheet (CSV)** (with «Select all» it is every
  row). Columns: `made, who, shop, shop_id, register_id, statement, version, language, text`. A text a spreadsheet would
  read as a formula is defused.
- **`/admin/shops/trainee/`**: who has the basic pass and since when, and the last review.
- Nothing is edited from the admin: not the declarations, not the pass. The pass is earned on the site.

## Going back, tests and limits

- The tables are in `shops` `0001_initial`; on PostgreSQL their foreign keys cascade in the database too, so the previous
  image can delete a shop or an account (`shops/going_back.py`, checked by `shops/test_going_back.py`).
- `shops/test_training.py` (`QuickStart`) and `shops/test_register_flow.py` (`LightningFlow`): the quick start, in the page and on the phone.
- `shops/test_training.py`: the quiz and the declarations (text changes included), the unlock and the `since` day at the
  hours that matter, the staff registers, permissions, CSRF and the staff as visitors, the export.
- The pass proves that the merchant **was informed**, not that they understood or that they will do it: it is a note, not an
  exam. The questions the community writes decide how much it is worth.
- A shop with several owners over time keeps one pass for each account; a new owner has to do the path.
- The yearly review and the texts that change never lock a till: the community can lengthen `refresh_days`, not make them lock.
- The basic pass is for the account, the steps and the closed days are for the shop they were made for.

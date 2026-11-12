# The practice register

A merchant who knows nothing of Lightning should not learn the register with a customer waiting. The practice register
is the shop's own register, with the same screens and the same words, where nothing is real.

**Can be turned off.** `features.practice: false` in `community.yml` takes the buttons and the links away and the two addresses answer 404. See [Turning features off](README.md#turning-features-off).

## Where it is

- `/shops/practice/<shop>/`: linked as «Practise without money» from the register's menu, from the panel and from the
  «Try the register» step of the first day. Whoever can open the shop's register can open it; for a shop with no
  wallet yet, only its owner (a historic shop without a register has none).
- `/shops/register/p/<register>/practice/?k=<key>`: the practice of a staff register, with the staff's words (refunds and
  overpayments go to the owner). Not for a register the owner turned off.

## What happens there

A band on every screen says that nothing is a payment, and the code on the screen is a QR of that sentence: no wallet
can pay it. The panel of the pretend customer offers only the moves that make sense for the sale on the screen: pays,
does not pay (the code expires), the connection drops and comes back, the phone switches off. After each move the
panel says what happened and what to do now, and the register answers as the real one does.

Four exercises tick themselves: take 3.50, the customer says they paid but the register is waiting, the customer does
not pay, and the phone switches off during a sale. A community that turned on-chain on (an experimental feature, off by
default: [On-chain at the register](onchain.md)) has a fifth, the customer does not understand the code. It is the only
place of on-chain in the practice: «Does not understand the code» says the customer's wallet may pay only on-chain, and
only then does «OK, on-chain» show; the merchant cancels, takes the amount again on-chain and the customer pays (a
word, `practice`, in place of an address, and the practice's own QR code: no wallet can pay it). On-chain is a remedy
when Lightning does not work for the customer, never the first way to take a payment; in the real register too «OK,
on-chain» is a small link under the keypad, shown only when the owner set up on-chain. Each has its question and answer, readable
without JavaScript too. The last step leads to the real thing: the first day in the panel, connecting the wallet, or
the staff's real register.

## Why nothing is real

- The page is `templates/shops/register.html` with `static/register.js` unchanged; `static/practice.js`, loaded first,
  answers the register's calls in the browser from a pretend sale kept in `localStorage`, and refuses every other
  request.
- The practice register has no address to pay into and no address to call: without `practice.js` no payment can start.
- The pages ask nothing of any provider and write nothing: no sale on the site or in the register's history, no first sale.
- No receipt is printed, and the band prints black on white.
- Its history, CSV and encrypted copy are the practice's own: the files have «practice» in their name, the real
  register refuses a copy of the practice (its sales are pretend, and would count as collected), and the practice
  refuses a real register's copy.

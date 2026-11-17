# The register's settings

From the register's menu (the round button at the top of the keypad), **Settings**. Everything here stays in the
phone's storage (`register-settings:<shop>`), and reaches a staff register only through its pairing code
([Registers for the staff](staff-registers.md)). The screen is in titled groups, the essentials first: «Where the money
goes» (the Lightning address, shown: it is changed on the site), «Prices» (currency, discount), «Look» (colour, line);
the rest under «Advanced» (the price sources, Nostr). Each field has at most one line of help, the long explanations
open under a small summary; a field is checked when it is left (and again as it is typed while it is wrong), and its
error shows next to it, as on Save.

- **Discount for paying in bitcoin.** It starts from the shop's discount, or the community's suggestion
  (`register.discount`) when the shop has none. It is on at every sale, shown with the amount to pay. Only when the owner
  ticks «Let the registers remove the discount on a sale» (`discountRemovable`, off by default; `dr` in the pairing
  code) is the line a button whose tap removes it for that sale, on the owner's register and the staff ones alike;
  otherwise it is a line to read. The sale keeps the price before the discount and the percentage. With «Round the
  discounted price down to a whole amount» (`discountRound`, off by default; `dw` in the pairing code) the price after
  the discount goes down to the whole unit, for shops with round prices: 14.00 less 10% is 12.00, not 12.60. It never
  goes up, an amount under one unit keeps its cents, and so does one that rounding would discount by more than twice
  the percentage (2.00 less 10% stays 1.80).
- **Currency** (three letters).
- **Advanced: price sources.** At least two, chosen only among those the community lists in `register.price_sources`
  (each one a source the code knows, `PRICE_SOURCES` in `shops/register.py`: never an address typed by the owner). The
  register's `connect-src` holds only those. The choice goes to the staff registers with the pairing code, which
  checks it against the community's list again; a staff register has no settings and cannot change it.
- **Colour** of the register and a **line** shown on it, both carried to the staff registers.

The owner's phone also remembers the Lightning address it takes payments into. If the site gives a different one, the
register stops and says so, until the owner confirms the change was theirs.

A community that turned on the experimental on-chain payments has an «On-chain (optional)» group too, and more under
«Advanced»: see [On-chain at the register: design notes](onchain-notes.md#the-owners-settings-on-the-phone).

## On a PC or a tablet

The register is made for a phone. On a wide screen (900 px or more, and at least 560 px high: a PC or a tablet on the
counter, not a phone held sideways) it becomes a till. Nothing to set: it follows the window.

- **The day beside the keypad.** «Today»: what was collected, and the last sales with how each ended. The same lines
  as the day closing, read from the phone's storage; nothing is sent. It is shown with the keypad only, never next to
  the code the customer scans.
- **The amount is typed.** Where there is a keyboard and a mouse, the keys leave the screen: the amount is typed
  (digits, Backspace, Esc to clear) and Enter is OK. «Show the keys» under the amount brings them back, and the
  choice stays on that device (`register-keys:<shop>`). A touch screen keeps the keys.
- **Keys for the other screens**, shown on their buttons: Esc cancels a payment in wait (it asks first, as the
  button does) or leaves the choice of the rate, Enter starts a new payment when one ends, P prints the receipt, D
  turns the discount on and off where the owner allows it.
- **The history opens beside the keypad**, which stays and still takes payments; a payment closes it. The settings
  take the keypad's place, as on a phone: no sale starts while they are being changed.
- **The menu is a panel on the right**; a click on the rest of the page closes it.
- **The code to scan takes the height of the screen**, with the amount in large type: a PC's screen is often turned
  to the customer.

A [staff register](staff-registers.md) has less here too, as on a phone: beside the keypad it shows its last sales,
without what the day collected (that is in «History and end of shift»); it has no «Show the keys», and follows the
device it is on; it has no settings, and D works only if the owner lets the registers remove the discount.

The practice register keeps its own wide layout, with the customer's phone beside it.

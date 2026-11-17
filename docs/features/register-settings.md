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

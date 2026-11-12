# Limitations

- The register asks for invoices and checks payments straight from the phone, through the merchant's Lightning address
  (LNURL-pay) and its LUD-21 `verify`: a provider without `verify`, or one that refuses the browser's requests (CORS),
  cannot be used by the register. The page for a new shop tries the address before accepting it.
- The sales live only on the phones and in the copies the merchant keeps (the CSV, the encrypted file, the encrypted
  copy on Nostr): the site never sees them. The one exception, the on-chain address counter, is in
  [What the site keeps](../README.md#what-the-site-keeps). A phone lost without a copy loses its
  history, not the money, which is in the merchant's wallet.
- Whoever controls a phone can change its page. The owner sees it by comparing the register's history with the wallet.
- OpenStreetMap is read once a month and never written by the site itself: a person changes the entries by hand, or
  runs `manage.py osm_upload`, which is experimental and off by default. A shop that stops taking bitcoin stays marked
  as taking it there until somebody does. The tags come from what the site knows (the last
  time the shop's Lightning address answered, the closing day), not from a visit. A match between a shop and an element
  is decided by the name and the distance; a doubtful one waits for the association. See
  [OpenStreetMap](features/openstreetmap.md).
- On-chain at the register is experimental and off by default ([Experimental features](features/README.md#experimental-features)):
  the register takes Lightning only unless the community turns it on. Turned on, it needs a public explorer that answers
  the phone, or the community's own: the register shows what it asked and when, and the merchant checks a doubtful
  payment in the wallet.
- The owner's register is the shop's id, which the shop page shows: it cannot be turned off. Staff registers can, but
  their pairing carries the shop's Lightning address (the phone needs it to ask for invoices without the site), and a
  register turned off still opens for a day, so that a sale on its screen ends.
- Whoever controls the server controls the register: the page is code the site serves, and a changed page can ask for
  invoices from another address. The phone remembers the shop's Lightning address and stops when the site gives another
  one, but a changed page can skip that too. What does not depend on the server is the merchant's wallet: the
  money that arrived is there, and at the day closing the merchant compares it with the register's history.
- «Paid» is what the shop's Lightning provider says: the register asks the `verify` address of the invoice and
  believes the answer, checking the payment's proof (the preimage) when the provider sends it. A provider that lies,
  lies about its own customer's money.
- The cryptography the register runs in the browser (the derivation of on-chain addresses, the encrypted copies on
  Nostr) is written for this project and checked against the public test vectors (BIP32, BIP84, BIP340, NIP-44). It
  has not been audited by anyone else.
- Signing in with a wallet (LNURL-auth) can be relayed. A sign-in code belongs to the browser that shows it: whoever
  gets a merchant to scan a code shown on someone else's screen (sent in a message, or on a page that looks like help)
  signs that other browser in as the merchant. The wallet shows the site's real name either way, so a merchant should scan a
  sign-in code only on the device they are signing in on. A code lasts five minutes. Whoever got in can change the
  shop's Lightning address, once in seven days: the register's phone remembers the address and stops when the site gives another
  one, until the owner confirms it there, and the change is told to the owner by email and in the shop's Telegram
  chats, and to the association ([Changing a shop's Lightning address](features/address-change.md)).
- There is no «sign out everywhere». A session lasts two weeks and ends only in the browser that signs out: a
  phone lost, or a browser someone else got signed in, stays in until then. Changing the Lightning address
  asks for a sign-in of the last 15 minutes.
- The administrators' sign-in in `/admin/` is a password, with no second factor: restrict it by address with
  `ADMIN_ALLOWED_IPS` ([configuration.md](configuration.md#admin-and-access-log)).
- Team access (`TEAM_ACCESS`, optional and off by default) gives the pass to the browser that showed the code, whoever
  scanned it: a team member talked into scanning somebody else's sign-in code opens the site under construction for
  them, for up to seven days or until the team ends that key's passes. The team is told of every new pass. See
  [Team access with a Lightning wallet](features/team-access.md#what-to-know-before-turning-it-on).
- The limits by address (sign-in tries, emails asked, new accounts) count every visitor of the onion address as one:
  Caddy sees them all come from the onion service. One of them can use up a limit for the others, never for the
  visitors of the site's own address.

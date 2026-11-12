"""The register's book (static/register-book.js): the history that stays on the phone, its day closing, the CSV for the
accountant, the merge of two histories and the encrypted copy.

Without a JavaScript engine the script is read as text, and the encrypted copy is opened here with Python from a file
the script made (register-copy-v1.json): the format is a promise to the merchants, who must be able to open an old copy
on a new phone. With node (NODE=/path/to/node, or node on the PATH) the script itself runs too."""
import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from django.conf import settings
from django.test import SimpleTestCase

from .business_day import SHIFT_NIGHT_HOUR
from .test_scripts import assert_talks_to_nobody

BOOK = settings.BASE_DIR / "static" / "register-book.js"
COPY = Path(__file__).parent / "testdata" / "register-copy-v1.json"
# The passphrase of register-copy-v1.json
PASSPHRASE = "café under the arcade"


def open_copy(envelope, passphrase):
    """The copy opened as the format says (static/register-book.js, `open`), independently of the script."""
    salt, iv = base64.b64decode(envelope["salt"]), base64.b64decode(envelope["iv"])
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=envelope["iterations"])
    key = kdf.derive(passphrase.encode())
    additional = json.dumps([envelope[name] for name in ("format", "version", "kdf", "iterations", "cipher", "salt", "iv")],
                            separators=(",", ":"), ensure_ascii=False).encode()
    return json.loads(AESGCM(key).decrypt(iv, base64.b64decode(envelope["data"]), additional))


class Source(SimpleTestCase):
    def setUp(self):
        self.script = BOOK.read_text()

    def test_the_business_day_ends_where_the_site_says(self):
        self.assertIn(f"const NIGHT_HOUR = {SHIFT_NIGHT_HOUR};", self.script)

    def test_the_copy_is_encrypted_with_a_slow_key_and_an_authenticated_cipher(self):
        iterations = int(re.search(r"const ITERATIONS = (\d+);", self.script)[1])
        self.assertGreaterEqual(iterations, 600000)
        self.assertIn('{ name: "PBKDF2", hash: "SHA-256", salt, iterations }', self.script)
        self.assertIn('{ name: "AES-GCM", length: 256 }', self.script)
        # A fresh salt and a fresh IV for every copy, kept in the file
        self.assertIn("crypto.getRandomValues(new Uint8Array(16))", self.script)
        self.assertIn("crypto.getRandomValues(new Uint8Array(12))", self.script)
        self.assertIn("additionalData: additional(head)", self.script)

    def test_the_book_talks_to_nobody(self):
        assert_talks_to_nobody(self, "register-book.js", also=("localStorage",))

    def test_the_csv_cells_cannot_be_formulas(self):
        self.assertIn(r"const FORMULA = /^[=+\-@\t\r]/;", self.script)

    def test_the_register_pages_load_the_book_before_the_register(self):
        # Outside the scripts block: the practice register, which replaces the block, has it too
        page = (settings.BASE_DIR / "templates" / "shops" / "register.html").read_text()
        self.assertLess(page.index("<script src=\"{% static 'register-book.js' %}\"></script>"), page.index("{% block scripts %}"))


class CopyFormat(SimpleTestCase):
    """register-copy-v1.json was made by the script (seal) with PASSPHRASE: version 1 opens as the format says."""

    def test_a_copy_made_by_the_register_opens_with_its_passphrase(self):
        envelope = json.loads(COPY.read_text())
        self.assertEqual((envelope["format"], envelope["version"], envelope["kdf"], envelope["cipher"]),
                         ("bitmentor-register-copy", 1, "PBKDF2-SHA256", "AES-256-GCM"))
        payload = open_copy(envelope, PASSPHRASE)
        self.assertEqual([sale["status"] for sale in payload["sales"]], ["paid", "confirmed"])
        self.assertEqual(payload["sales"][0]["sources"][0]["source"], "kraken")

    def test_a_changed_header_or_another_passphrase_does_not_open(self):
        from cryptography.exceptions import InvalidTag

        envelope = json.loads(COPY.read_text())
        with self.assertRaises(InvalidTag):
            open_copy(envelope, "another passphrase")
        with self.assertRaises(InvalidTag):
            open_copy({**envelope, "cipher": "AES-256-GCM "}, PASSPHRASE)


HARNESS = r"""
const fs = require("fs");
eval(fs.readFileSync(process.argv[2], "utf8"));
const book = globalThis.registerBook;
const at = (text) => new Date(text).getTime();
(async () => {
  const out = {};
  out.days = [book.businessDay(at("2026-11-09T04:59:00")), book.businessDay(at("2026-11-09T05:00:00")),
    book.businessDay(at("2026-03-29T02:30:00"))];
  const mine = [
    { id: "a", at: at("2026-11-09T10:00:00"), cents: 350, status: "pending", pr: "lnbc1", verify: "https://x/v", qr: "data:",
      sats: 3500 },
    { id: "p", at: at("2026-11-09T10:30:00"), cents: 350, status: "pending", verify: "https://x/v", sats: 3500 },
    { id: "b", at: at("2026-11-09T11:00:00"), cents: 500, status: "paid", sats: 5000 },
    { id: "u", at: at("2026-11-09T12:00:00"), cents: 100, status: "unchecked" },
  ];
  const theirs = [
    { id: "a", at: at("2026-11-09T10:00:00"), cents: 1, status: "paid", sats: 1, method: "onchain", address: "bc1qevil",
      currency: "XXX", extra: "x", txid: "cd".repeat(32), late: true },
    { id: "p", at: at("2026-11-09T10:30:00"), cents: 350, status: "seen", sats: 3500 },
    { id: "b", at: at("2026-11-09T11:00:00"), cents: 500, status: "expired" },
    { id: "u", at: at("2026-11-09T12:00:00"), cents: 100, status: "pending", verify: "https://evil/" },
    { id: "c", at: at("2026-11-09T09:00:00"), cents: 200, status: "pending", verify: "https://evil/", who: "Anna" },
    { id: "", at: 1, cents: 1, status: "paid" },
    { id: "d", at: 1, cents: 1, status: "toString" },
    "nonsense",
  ];
  out.merge = book.merge(mine, theirs);
  out.again = book.merge(out.merge.list, theirs);
  const sales = [
    { id: "1", at: at("2026-11-09T09:00:00"), cents: 350, currency: "EUR", status: "paid", sats: 3500 },
    { id: "2", at: at("2026-11-10T01:00:00"), cents: 150, currency: "EUR", status: "confirmed", sats: 1500 },
    { id: "3", at: at("2026-11-09T12:00:00"), cents: 999, currency: "EUR", status: "expired", sats: 9990 },
    { id: "4", at: at("2026-11-10T06:00:00"), cents: 100, currency: "EUR", status: "paid", sats: 1000 },
    { id: "5", at: at("2026-11-09T13:00:00"), cents: 400, currency: "EUR", status: "unchecked", sats: 4000 },
  ];
  out.totals = book.dayTotals(sales, "2026-11-09");
  out.discounted = [book.discounted(1500, 10), book.discounted(999, 10), book.discounted(1000, 0), book.discounted(1000, 150)];
  const chain = { id: "oc", at: at("2026-11-09T09:00:00"), cents: 900, currency: "EUR", status: "seen", method: "onchain",
    sats: 9000, address: "bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu", path: "2", index: 7, txid: "ab".repeat(32),
    before: ["x"], list: 1000, discount: 10 };
  out.chainMerge = book.merge([], [chain, { ...chain, id: "bad", path: "-1", index: 2 ** 31 }]);
  // From another register: never «paid» over a sale confirmed by hand, amounts in whole cents and sats, kept as reported
  const byHand = { id: "h", at: at("2026-11-09T10:00:00"), cents: 300, status: "confirmed", till: "owner" };
  out.foreign = book.merge([byHand], [
    { ...byHand, status: "paid", sats: 3000, till: "staff-1" },
    { id: "s1", at: at("2026-11-09T10:05:00"), cents: 200, status: "paid", sats: 2000, till: "staff-1", who: "Anna" },
    { id: "s2", at: at("2026-11-09T10:06:00"), cents: 200, status: "paid", sats: 2000, till: "owner", reported: "" },
    { id: "f1", at: 1, cents: 1.5, status: "paid" }, { id: "f2", at: 1, cents: -1, status: "paid" },
    { id: "f3", at: 1, cents: 1e12, status: "paid" }, { id: "f4", at: 1, cents: 1, sats: 0.5, status: "paid" },
    { id: "f5", at: 1, cents: 1, sats: 3e15, status: "paid" }, { id: "f6", at: 1, cents: 1, sats: -2, status: "paid" },
  ], { own: "owner" });
  out.untrusted = book.merge([], [{ id: "o1", at: 1, cents: 1, status: "paid", till: "owner" }], { own: "owner", trusted: false });
  // A staff register's copy speaks only for that register: a sale it says is the owner's own stays out
  out.pinned = book.merge([], [
    { id: "o2", at: 1, cents: 100000, status: "paid", till: "owner" },
    { id: "s3", at: 1, cents: 100, status: "paid", till: "owner:staff-1" },
    { id: "s4", at: 1, cents: 100, status: "paid" },
  ], { own: "owner", from: "owner:staff-1" });
  // A copy brought back on a new phone keeps which sales another register reported: they stay to check in the wallet
  const oldPhone = book.merge([{ id: "m", at: 1, cents: 1, status: "paid", till: "owner" }],
    [{ id: "x", at: 1, cents: 99900, status: "paid", till: "owner", sats: 1 }], { own: "owner", trusted: false }).list;
  out.restored = book.merge([], oldPhone, { own: "owner", trusted: true }).list;
  out.indexes = [book.clean({ id: "i", at: 1, cents: 1, status: "paid", path: "2147483647", index: 0x7fffffff }),
    book.clean({ id: "j", at: 1, cents: 1, status: "paid", path: "3", index: 0x7ffffffe })];
  // A copy without trust (a stranger's, a file without an author) that knows the id of a sale of this register's own
  // changes nothing of it: refused. Its other sales come in, reported
  const ownSales = [{ id: "o1", at: 1, cents: 1000, status: "expired", till: "owner", method: "lightning" },
    { id: "o2", at: 1, cents: 1000, status: "unchecked", wasWaiting: true, till: "owner", method: "onchain" }];
  out.strangerOverOwn = book.merge(ownSales, [{ id: "o1", at: 1, cents: 1000, status: "paid", till: "owner" },
    { id: "o2", at: 1, cents: 1000, status: "paid", till: "owner" }, { id: "n", at: 1, cents: 5, status: "paid", till: "x" }],
  { own: "owner", trusted: false });
  out.restoredLong = book.clean({ id: "y", at: 1, cents: 1, status: "paid", reported: "r".repeat(500) }).reported.length;
  // A staff sale that came in while it was waiting ends as that register says in its next copy, and no other register
  // can end it
  const staff = "owner:staff-1";
  const waited = book.merge([], [{ id: "z", at: 1, cents: 1200, status: "pending", till: staff }], { own: "owner", from: staff });
  out.staffWaiting = waited.list[0];
  out.staffEnded = book.merge(waited.list, [{ id: "z", at: 1, cents: 1200, status: "cancelled", till: staff }],
    { own: "owner", from: staff });
  out.staffStillWaiting = book.merge(waited.list, [{ id: "z", at: 1, cents: 1200, status: "seen", till: staff }],
    { own: "owner", from: staff });
  out.staffOther = book.merge(waited.list, [{ id: "z", at: 1, cents: 1200, status: "expired", till: "owner:staff-2" }],
    { own: "owner", from: "owner:staff-2" });
  // Only a copy trusted for that register ends a sale that came in while waiting: not a file without an author, not
  // a stranger's copy on Nostr (both trusted: false), whatever till they name
  const restored = book.merge([], [{ id: "o", at: 1, cents: 1000, status: "pending", till: "owner", method: "onchain" }],
    { own: "owner", trusted: true });
  out.ownWaiting = restored.list[0];
  out.untrustedEnds = book.merge(restored.list, [{ id: "o", at: 1, cents: 1, status: "expired", till: "owner" }],
    { own: "owner", trusted: false });
  out.strangerEnds = book.merge(waited.list, [{ id: "z", at: 1, cents: 1200, status: "cancelled", till: staff }],
    { own: "owner", trusted: false });
  out.ownEnds = book.merge(restored.list, [{ id: "o", at: 1, cents: 1000, status: "expired", till: "owner" }],
    { own: "owner", trusted: true });
  // An own trusted copy does not end a staff register's sale either: only that register's copy does
  out.ownEndsStaff = book.merge(waited.list, [{ id: "z", at: 1, cents: 1200, status: "cancelled", till: staff }],
    { own: "owner", trusted: true });
  // A staff register's copy that gives a sale the id of one of the owner's own does not change it
  out.sameId = book.merge([{ id: "o4", at: 1, cents: 1000, status: "expired", till: "owner", method: "lightning" },
    { id: "o5", at: 1, cents: 1000, status: "expired", method: "lightning" }],
  [{ id: "o4", at: 1, cents: 1000, status: "paid", till: staff }, { id: "o5", at: 1, cents: 1000, status: "paid", till: staff }],
  { own: "owner", from: staff });
  // The practice's pretend sales stay out of a real register whatever the way in, and a real one's out of the practice
  const pretend = { id: "pr", at: 1, cents: 350, status: "paid", till: "practice:/shops/practice/shop-1/" };
  out.practiceIn = [book.merge([], [pretend], { own: "owner" }), book.merge([], [pretend], { own: "owner", trusted: false }),
    book.merge([], [pretend], { own: "owner", from: pretend.till }),
    book.merge([], [pretend, { id: "re", at: 1, cents: 1, status: "paid", till: "owner" }, { id: "old", at: 1, cents: 1, status: "paid" }],
      { own: pretend.till })].map((merged) => [merged.list.map((entry) => entry.id), merged.refused]);
  // A copy made while the sales were waiting does not take the place of how they ended on this phone
  out.pendingCopy = book.merge([{ id: "a", at: 1, cents: 350, status: "cancelled", till: "owner" },
    { id: "b", at: 1, cents: 500, status: "expired", till: "owner" }],
  [{ id: "a", at: 1, cents: 350, status: "pending", till: "owner" }, { id: "b", at: 1, cents: 500, status: "pending", till: "owner" }],
  { own: "owner" });
  out.foreignTotals = book.dayTotals(out.foreign.list, "2026-11-09");
  out.chainCompact = book.compact({ ...chain, status: "paid" });
  out.chainTotals = book.dayTotals([{ ...chain, status: "paid" }, { ...chain, id: "s" }], "2026-11-09");
  out.csvChain = book.csv([{ ...chain, status: "paid" }], { lang: "en", columns: ["a"] });
  out.dayList = book.days(sales);
  // Checked with the provider (register.js sets `verified`; a copy or a file never brings it)
  const staffSale = { id: "v1", at: at("2026-11-09T09:00:00"), cents: 100, currency: "EUR", status: "paid", sats: 1000,
    till: "shop:staff", reported: "shop:staff", hash: "ab".repeat(32), verify: "https://ln.example/verify/1" };
  const sale2 = { ...staffSale, id: "v2", hash: "cd".repeat(32) };
  out.verifiedTotals = book.dayTotals([{ ...staffSale, verified: true }, sale2, { ...sale2, id: "v3", status: "confirmed" },
    { ...staffSale, id: "v4", verified: true, status: "confirmed" }], "2026-11-09").lines[0];
  out.verifiedClean = book.merge([], [{ ...staffSale, verified: true }], { own: "shop", from: "shop:staff" }).list[0];
  out.verifiedKept = book.merge([{ ...staffSale, verified: true, status: "unchecked" }], [staffSale],
    { own: "shop", from: "shop:staff" }).list[0];
  out.trimmed = [book.trim({ ...staffSale, reported: undefined, verified: true }), book.trim({ ...staffSale, reported: undefined }, true),
    book.trim(staffSale), book.trim(staffSale).verified];
  out.verifiedCsv = book.csv([{ ...staffSale, verified: true }, sale2], { lang: "en", columns: ["a"], states: { paid: "Paid" },
    verified: "checked" }).split("\r\n").slice(1, 3);
  out.compact = book.compact({ id: "1", at: 1, cents: 1, status: "paid", pr: "lnbc", qr: "data:", verify: "v", expires: 2 });
  out.pending = book.compact({ id: "1", at: 1, cents: 1, status: "pending", pr: "lnbc", qr: "data:", verify: "v", expires: 2 });
  // Cancelled while its invoice lasts: still looked at; over, only the history
  const open = { id: "1", at: 1, cents: 1, status: "cancelled", pr: "lnbc", verify: "v", expires: Date.now() + 60000 };
  out.cancelled = [book.compact(open), book.compact({ ...open, expires: 2 }), book.compact({ ...open, verify: "" })];
  // A copy where the sale was still waiting does not stop this phone looking at its cancelled one for a late payment
  out.openMerge = book.merge([open], [{ ...open, status: "seen" }, { id: "x", at: 1, cents: 1, status: "unchecked" }]).list;
  out.closedMerge = book.merge([{ ...open, expires: 2 }], [{ ...open, status: "seen" }]).list;
  out.lateTotals = book.dayTotals([{ ...sales[0], late: true }], "2026-11-09").lines[0].late;
  out.lateCsv = book.csv([{ ...sales[0], late: true }], { lang: "en", columns: ["a"], states: { paid: "Paid" }, late: "after cancel" });
  const row = [{ id: "1", at: at("2026-11-09T09:05:03"), cents: 350, currency: "EUR", status: "paid", sats: 3500, rate: 81234.5,
    how: "agreed", sources: [{ source: "kraken", price: 81200, at: Date.UTC(2026, 10, 9, 8, 5) }, { source: "coinbase", price: 81269 }],
    hash: "abc", who: "=HYPERLINK(1)", reported: "owner:staff-1" }];
  const columns = ["When", "Day", "Amount", "Currency", "Status", "Method", "Sats", "Rate", "How", "Sources", "Hash", "Who",
    "Reported", "Discount", "List", "Address", "Tx", "ID"];
  out.csvIt = book.csv(row, { lang: "it", columns, states: { paid: "Pagato" }, hows: { agreed: "two agreed" } });
  out.csvEn = book.csv(row, { lang: "en", columns, month: "2026-11" });
  out.csvOtherMonth = book.csv(row, { lang: "en", columns, month: "2026-10" });
  const now = at("2026-11-09T12:00:00");
  out.due = [
    book.backupDue("manual", 0, at("2026-10-01T12:00:00"), now),
    book.backupDue("weekly", 0, 0, now),
    book.backupDue("daily", at("2026-11-08T11:00:00"), at("2026-11-08T13:00:00"), now),
    book.backupDue("daily", at("2026-11-08T13:00:00"), at("2026-11-08T14:00:00"), now),
    book.backupDue("weekly", at("2026-11-01T11:00:00"), at("2026-11-05T11:00:00"), now),
    book.backupDue("monthly", at("2026-10-20T11:00:00"), at("2026-11-05T11:00:00"), now),
    book.backupDue("closing", at("2026-11-08T11:00:00"), at("2026-11-09T06:00:00"), now),
    book.backupDue("closing", at("2026-11-08T11:00:00"), at("2026-11-09T02:00:00"), now),
  ];
  const a = { source: "kraken", price: 100 };
  const b = { source: "coinbase", price: 103 };
  const c = { source: "bitstamp", price: 100.5 };
  out.agree = [book.agreeing([a, b], 1), book.agreeing([a, b, c], 1), book.agreeing([a, { ...a }], 1), book.agreeing([], 1)];
  const payload = { sales: [{ id: "1", at: 1, cents: 1, status: "paid" }], till: "t" };
  const sealed = await book.seal(payload, "a passphrase ok", 100000);
  out.sealed = sealed;
  out.opened = await book.open(sealed, "a passphrase ok");
  const reason = async (envelope, passphrase) => {
    try { await book.open(envelope, passphrase); return "opened"; } catch (error) { return error.reason; }
  };
  out.reasons = [
    await reason(sealed, "wrong"),
    await reason({ ...sealed, iterations: 100001 }, "a passphrase ok"),
    await reason({ ...sealed, iterations: 10 }, "a passphrase ok"),
    await reason({ ...sealed, version: 2 }, "a passphrase ok"),
    await reason({ ...sealed, format: "other" }, "a passphrase ok"),
    await reason({ ...sealed, salt: "!!" }, "a passphrase ok"),
    await reason(null, "x"),
  ];
  if (process.argv[3]) {
    out.fixture = await book.open(JSON.parse(fs.readFileSync(process.argv[3], "utf8")), process.argv[4]);
  }
  process.stdout.write(JSON.stringify(out));
})().catch((error) => { console.error(error); process.exit(1); });
"""


class Running(SimpleTestCase):
    """The script itself, in node, in the time zone of a community."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        node = os.environ.get("NODE") or shutil.which("node")
        if not node:
            raise __import__("unittest").SkipTest("node is missing: NODE=/path/to/node to run the register's book")
        with tempfile.TemporaryDirectory() as folder:
            harness = Path(folder) / "harness.js"
            harness.write_text(HARNESS)
            result = subprocess.run([node, str(harness), str(BOOK), str(COPY), PASSPHRASE], capture_output=True, text=True,
                                    env={**os.environ, "TZ": "Europe/Rome"}, timeout=60)
        if result.returncode:
            raise AssertionError(result.stderr)
        cls.out = json.loads(result.stdout)

    def test_the_night_belongs_to_the_day_before(self):
        self.assertEqual(self.out["days"], ["2026-11-08", "2026-11-09", "2026-03-28"])

    def test_two_histories_become_one_with_each_sale_once(self):
        merged = self.out["merge"]
        by_id = {sale["id"]: sale for sale in merged["list"]}
        self.assertEqual([sale["id"] for sale in merged["list"]], ["c", "a", "p", "b", "u"])
        self.assertEqual((merged["added"], merged["updated"]), (1, 1))
        # The copy that knows more wins, without what only a waiting sale needs and without unknown fields. Only how it
        # ended moves forward: the amount, the sats, the method and the address stay this phone's
        self.assertEqual((by_id["a"]["status"], by_id["a"]["txid"], by_id["a"]["late"]), ("paid", "cd" * 32, True))
        self.assertEqual((by_id["a"]["cents"], by_id["a"]["sats"]), (350, 3500))
        for field in ("pr", "verify", "qr", "extra", "method", "address", "currency"):
            self.assertNotIn(field, by_id["a"])
        # A sale still waiting here goes on waiting here, to be checked: «seen» in a file does not make it «not checked»
        self.assertEqual((by_id["p"]["status"], by_id["p"]["verify"]), ("pending", "https://x/v"))
        # A paid sale stays paid, and one still waiting in the file never replaces what this phone knows
        self.assertEqual(by_id["b"]["status"], "paid")
        self.assertEqual(by_id["u"]["status"], "unchecked")
        # A sale waiting in the file cannot be checked from here: to look for in the wallet, without the provider's URL
        self.assertEqual(by_id["c"]["status"], "unchecked")
        self.assertNotIn("verify", by_id["c"])
        self.assertEqual(by_id["c"]["who"], "Anna")
        # Importing the same file again changes nothing
        self.assertEqual((self.out["again"]["added"], self.out["again"]["updated"]), (0, 0))

    def test_the_day_closing_counts_what_was_collected_by_method(self):
        totals = self.out["totals"]
        self.assertEqual([sale["id"] for sale in totals["sales"]], ["1", "3", "5", "2"])
        self.assertEqual(totals["lines"], [
            {"method": "lightning", "currency": "EUR", "count": 2, "cents": 500, "sats": 5000, "byHand": 1,
             "reported": 0, "verified": 0, "late": 0}])
        self.assertEqual(totals["counts"], {"paid": 1, "expired": 1, "unchecked": 1, "confirmed": 1})
        self.assertEqual(self.out["dayList"], ["2026-11-10", "2026-11-09"])

    def test_a_sale_cancelled_while_its_invoice_lasts_is_still_looked_at(self):
        still, over, unchecked = self.out["cancelled"]
        self.assertEqual(still["verify"], "v")
        self.assertEqual(over, {"id": "1", "at": 1, "cents": 1, "status": "cancelled"})
        [kept, _other] = self.out["openMerge"]
        self.assertEqual((kept["status"], kept["verify"]), ("cancelled", "v"))
        # One no longer looked at knows less than the copy: «not checked», to look for in the wallet
        self.assertEqual(self.out["closedMerge"][0]["status"], "unchecked")
        self.assertNotIn("pr", unchecked)
        # Paid after it was cancelled: counted, and said apart
        self.assertEqual(self.out["lateTotals"], 1)
        self.assertIn(",Paid (after cancel),", self.out["lateCsv"])

    def test_a_finished_sale_keeps_only_what_the_history_needs(self):
        self.assertEqual(self.out["compact"], {"id": "1", "at": 1, "cents": 1, "status": "paid"})
        self.assertEqual(set(self.out["pending"]), {"id", "at", "cents", "status", "pr", "qr", "verify", "expires"})

    def test_the_csv_follows_the_language_and_keeps_the_trace_of_the_rate(self):
        italian = self.out["csvIt"]
        self.assertTrue(italian.startswith("﻿When;Day;Amount;"))
        row = italian.split("\r\n")[1].split(";")
        self.assertEqual(row[:9], ["2026-11-09 09:05:03", "2026-11-09", "3,50", "EUR", "Pagato", "lightning", "3500",
                                   "81234,50", "two agreed"])
        self.assertEqual(row[9], "kraken 81200,00 2026-11-09T08:05:00.000Z + coinbase 81269,00")
        # A name that looks like a formula stays text
        self.assertEqual(row[11], "'=HYPERLINK(1)")
        # The register that reported the sale: the accountant knows what to check in the wallet
        self.assertEqual(row[12], "owner:staff-1")
        english = self.out["csvEn"].split("\r\n")[1].split(",")
        self.assertEqual(english[2], "3.50")
        self.assertEqual(english[8], "agreed")
        self.assertEqual(self.out["csvOtherMonth"], "﻿When,Day,Amount,Currency,Status,Method,Sats,Rate,How,Sources,Hash,Who,Reported,Discount,List,Address,Tx,ID\r\n")

    def test_the_discount_is_rounded_to_the_cent(self):
        self.assertEqual(self.out["discounted"], [1350, 899, 1000, 1000])

    def test_an_on_chain_sale_keeps_its_address_and_where_it_comes_from(self):
        merged = {sale["id"]: sale for sale in self.out["chainMerge"]["list"]}
        sale = merged["oc"]
        # In the mempool in the file: this phone cannot know how it went on, it is to look for in the wallet
        self.assertEqual(sale["status"], "unchecked")
        self.assertEqual((sale["address"], sale["path"], sale["index"], sale["list"], sale["discount"]),
                         ("bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu", "2", 7, 1000, 10))
        self.assertNotIn("before", sale)
        # Indexes that no key has are dropped
        self.assertNotIn("path", merged["bad"])
        self.assertNotIn("index", merged["bad"])
        self.assertNotIn("before", self.out["chainCompact"])
        self.assertEqual(self.out["chainCompact"]["txid"], "ab" * 32)
        totals = self.out["chainTotals"]
        self.assertEqual(totals["lines"], [
            {"method": "onchain", "currency": "EUR", "count": 1, "cents": 900, "sats": 9000, "byHand": 0,
             "reported": 0, "verified": 0, "late": 0}])
        self.assertEqual(totals["counts"], {"paid": 1, "seen": 1})
        row = self.out["csvChain"].split("\r\n")[1].split(",")
        self.assertEqual(row[12:], ["", "10", "10.00", "bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu", "ab" * 32, "oc"])

    def test_sales_checked_with_the_provider_are_counted_apart_and_never_believed_from_a_copy(self):
        # Reported and checked: apart from the ones to check in the wallet; one marked as paid by hand never counts as checked
        line = self.out["verifiedTotals"]
        self.assertEqual((line["count"], line["verified"], line["reported"], line["byHand"]), (4, 1, 3, 2))
        # A copy or a file never brings «verified»; the URL and the hash come, the sale stays reported
        clean = self.out["verifiedClean"]
        self.assertNotIn("verified", clean)
        self.assertEqual((clean["reported"], clean["verify"], clean["hash"]), ("shop:staff", "https://ln.example/verify/1", "ab" * 32))
        # What this phone checked stays checked when a later copy of the sale comes in
        self.assertTrue(self.out["verifiedKept"]["verified"])
        # The check URL stays on a staff register's finished sales and on the reported ones, not on this register's own
        own_trim, staff_trim, reported_trim, _ = self.out["trimmed"]
        self.assertNotIn("verify", own_trim)
        self.assertNotIn("verified", own_trim)
        self.assertEqual(staff_trim["verify"], "https://ln.example/verify/1")
        self.assertEqual(reported_trim["verify"], "https://ln.example/verify/1")
        # The CSV says which were checked and does not name a register to check them with
        checked, to_check = [row.split(",") for row in self.out["verifiedCsv"]]
        self.assertIn("(checked)", checked[4])
        self.assertEqual((checked[12], to_check[12]), ("", "shop:staff"))

    def test_sales_from_another_register_are_reported_and_never_upgraded_from_by_hand(self):
        merged = {sale["id"]: sale for sale in self.out["foreign"]["list"]}
        self.assertEqual(sorted(merged), ["h", "s1", "s2"])
        self.assertEqual(merged["h"]["status"], "confirmed")
        self.assertNotIn("reported", merged["h"])
        self.assertEqual(merged["s1"]["reported"], "staff-1")
        self.assertNotIn("reported", merged["s2"])
        self.assertEqual((self.out["foreign"]["added"], self.out["foreign"]["updated"]), (2, 0))
        self.assertEqual(self.out["untrusted"]["list"][0]["reported"], "owner")
        self.assertEqual(self.out["foreignTotals"]["lines"][0]["reported"], 1)

    def test_a_copy_cannot_give_an_index_past_the_key_s_chain(self):
        last, below = self.out["indexes"]
        self.assertEqual((last["path"], last.get("index")), ("2147483647", None))
        self.assertEqual((below["path"], below["index"]), ("3", 0x7FFFFFFE))

    def test_a_copy_without_trust_does_not_change_this_register_s_own_sales(self):
        result = self.out["strangerOverOwn"]
        by_id = {sale["id"]: sale for sale in result["list"]}
        self.assertEqual((by_id["o1"]["status"], by_id["o2"]["status"]), ("expired", "unchecked"))
        self.assertNotIn("reported", by_id["o1"])
        self.assertEqual((result["updated"], result["refused"], result["added"]), (0, 2, 1))
        self.assertEqual([sale["id"] for sale in result["accepted"]], ["n"])
        self.assertEqual(by_id["n"]["reported"], "x")

    def test_a_restored_copy_keeps_what_was_reported(self):
        [sale] = [entry for entry in self.out["restored"] if entry["id"] == "x"]
        self.assertEqual((sale["status"], sale["reported"]), ("paid", "owner"))
        self.assertEqual(self.out["restoredLong"], 120)

    def test_a_staff_sale_imported_while_waiting_ends_as_its_register_says(self):
        waiting = self.out["staffWaiting"]
        self.assertEqual((waiting["status"], waiting["wasWaiting"], waiting["reported"]), ("unchecked", True, "owner:staff-1"))
        ended = self.out["staffEnded"]
        self.assertEqual(ended["updated"], 1)
        self.assertEqual((ended["list"][0]["status"], ended["list"][0]["reported"]), ("cancelled", "owner:staff-1"))
        self.assertNotIn("wasWaiting", ended["list"][0])
        # Still waiting in the next copy too: nothing new
        self.assertEqual(self.out["staffStillWaiting"]["updated"], 0)
        # Another register's copy does not speak for it
        self.assertEqual(self.out["staffOther"]["updated"], 0)
        self.assertEqual(self.out["staffOther"]["list"][0]["status"], "unchecked")

    def test_only_a_copy_trusted_for_the_register_ends_a_sale_that_came_in_waiting(self):
        self.assertEqual((self.out["ownWaiting"]["status"], self.out["ownWaiting"]["wasWaiting"]), ("unchecked", True))
        for name in ("untrustedEnds", "strangerEnds", "ownEndsStaff"):
            merged = self.out[name]
            self.assertEqual(merged["updated"], 0, name)
            self.assertEqual((merged["list"][0]["status"], merged["list"][0]["wasWaiting"]), ("unchecked", True), name)
        # This register's own trusted copy does
        ended = self.out["ownEnds"]
        self.assertEqual((ended["updated"], ended["list"][0]["status"]), (1, "expired"))
        self.assertNotIn("wasWaiting", ended["list"][0])

    def test_a_staff_copy_does_not_change_a_sale_of_another_register_with_the_same_id(self):
        merged = self.out["sameId"]
        self.assertEqual([(sale["id"], sale["status"]) for sale in merged["list"]], [("o4", "expired"), ("o5", "expired")])
        self.assertEqual((merged["updated"], merged["refused"]), (0, 2))

    def test_the_practice_and_a_real_register_never_take_each_other_s_sales(self):
        self.assertEqual(self.out["practiceIn"], [[[], 1], [[], 1], [[], 1], [["pr", "old"], 1]])

    def test_a_copy_made_while_waiting_does_not_undo_how_a_sale_ended(self):
        merged = self.out["pendingCopy"]
        self.assertEqual([sale["status"] for sale in merged["list"]], ["cancelled", "expired"])
        self.assertEqual(merged["updated"], 0)

    def test_a_staff_copy_cannot_add_the_owner_s_own_sales(self):
        pinned = self.out["pinned"]
        self.assertEqual([(sale["id"], sale["reported"]) for sale in pinned["list"]], [("s3", "owner:staff-1")])
        self.assertEqual((pinned["added"], pinned["refused"]), (1, 2))

    def test_a_copy_is_due_as_the_merchant_chose(self):
        self.assertEqual(self.out["due"], [False, False, True, False, True, False, False, True])

    def test_two_sources_must_agree(self):
        disagree, third, same, none = self.out["agree"]
        self.assertIsNone(disagree)
        self.assertEqual([answer["source"] for answer in third], ["kraken", "bitstamp"])
        # The same source twice is not an agreement
        self.assertIsNone(same)
        self.assertIsNone(none)

    def test_the_copy_opens_with_its_passphrase_only(self):
        self.assertEqual(self.out["opened"], {"sales": [{"id": "1", "at": 1, "cents": 1, "status": "paid"}], "till": "t"})
        self.assertEqual(self.out["reasons"], ["passphrase", "passphrase", "format", "version", "format", "format", "format"])
        # What the script seals, Python opens as the format says
        self.assertEqual(open_copy(self.out["sealed"], "a passphrase ok")["till"], "t")
        # And the copy of version 1 kept with the tests still opens in the script
        self.assertEqual(len(self.out["fixture"]["sales"]), 2)

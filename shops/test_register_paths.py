"""The paths of the register (static/register.js) where money or access is decided and that the flows in
test_register_flow.py do not walk: Cancel, the Lightning address pinned on the phone, the price sources, the sales left
waiting, the merchant's own word, the day closing and the files of the history. Each test runs the whole register in
node with its own steps (`script`, the body of an async function inside the harness)."""
import json
import subprocess
import time

from .test_register_chain import REGISTER
from .test_register_flow import ADDRESS, STATIC, RegisterRun, invoice

OWNER = {"ledger": "shop-1", "shop": "shop-1"}
SETTINGS = {"register-settings:shop-1": json.dumps({"lightning": ADDRESS})}
PAID = {"status": "OK", "settled": True, "preimage": "00" * 32}
UNPAID = {"status": "OK", "settled": False, "preimage": None}

# Types an amount, presses OK and waits for the sale in the book, or for the rate question
SELL = """
async function sell(typed, method = "lightning") {
  const before = ledger().length;
  key("Escape");
  for (const k of typed) key(k);
  if (method === "onchain") listeners["chain-ok:click"].forEach((fn) => fn());
  else key("Enter");
  await until(() => ledger().length > before || element("question").textContent || element("error").textContent, 3000);
  return ledger().at(-1);
}
const click = (id) => Promise.all((listeners[`${id}:click`] || []).map((fn) => fn()));
const submit = (id) => Promise.all((listeners[`${id}:submit`] || []).map((fn) => fn({ preventDefault() {}, target: { reset() {} } })));
"""


class Paths(RegisterRun):
    def steps(self, script, storage=None, dataset=OWNER, **extra):
        return self.run_register(dataset, {**SETTINGS, **(storage or {})}, script=SELL + script, **{"discount": 0, **extra})

    def lightning(self, script, verify=None, no_verify=False, storage=None, sats=10000, **extra):
        pr, payment_hash = invoice(sats)
        return self.steps(script, storage, ln={"invoices": [pr], "verify": {0: {**(verify or UNPAID), "pr": pr}},
                                             "noVerify": no_verify}, **extra)


class Cancel(Paths):
    def test_cancel_with_the_provider_saying_unpaid_cancels(self):
        out = self.lightning("""
          await sell("1000");
          await click("cancel");
          return { ledger: ledger(), confirmed };""")
        self.assertEqual([sale["status"] for sale in out["ledger"]], ["cancelled"])
        self.assertEqual(len(out["confirmed"]), 1)

    def test_cancel_not_confirmed_leaves_the_sale_waiting(self):
        out = self.lightning("""
          await sell("1000");
          await click("cancel");
          return ledger();""", confirm=False)
        self.assertEqual([sale["status"] for sale in out], ["pending"])

    def test_a_payment_just_before_cancel_ends_the_sale_paid(self):
        out = self.lightning("""
          await sell("1000");
          ln.verify[0] = { ...ln.verify[0], settled: true, preimage: "00".repeat(32) };
          await click("cancel");
          await until(() => ledger()[0].status !== "pending", 1000);
          return ledger();""")
        self.assertEqual([sale["status"] for sale in out], ["paid"])

    def test_cancel_of_an_on_chain_sale_seen_in_the_mempool_leaves_it_to_its_confirmation(self):
        from .test_register_chain import BIP84_ACCOUNT

        storage = {"register-settings:shop-1": json.dumps({"lightning": ADDRESS, "onchain": {"key": BIP84_ACCOUNT},
                                                           "currency": "EUR", "sources": ["kraken", "coinbase"]})}
        out = self.steps("""
          const sale = await sell("1000", "onchain");
          await until(() => element("to").textContent, 5000);
          const address = ledger().at(-1).address;
          chainAnswer = (asked) => (asked === address ? [{ txid: "b".repeat(64), status: { confirmed: false },
            vout: [{ scriptpubkey_address: asked, value: ledger().at(-1).sats }] }] : []);
          await click("cancel");
          return ledger();""", storage)
        self.assertEqual([sale["status"] for sale in out], ["seen"])


class Pinned(Paths):
    def test_a_changed_lightning_address_blocks_until_the_owner_takes_it(self):
        storage = {"register-settings:shop-1": json.dumps({"lightning": "old@ln.example.org"})}
        out = self.lightning("""
          const blocked = element("blocked-text").textContent;
          const refused = await sell("1000");
          await click("pin-ok");
          const sale = await sell("1000");
          return { blocked, refused: refused || null, sale, saved: JSON.parse(kept.get("register-settings:shop-1")) };""",
                             storage=storage)
        self.assertIn("old@ln.example.org", out["blocked"])
        self.assertIn(ADDRESS, out["blocked"])
        self.assertIsNone(out["refused"])
        self.assertEqual(out["saved"]["lightning"], ADDRESS)
        self.assertEqual(out["sale"]["status"], "pending")


class Prices(Paths):
    def test_two_sources_that_disagree_let_the_merchant_choose(self):
        out = self.lightning("""
          await sell("1000");
          const question = element("question").textContent;
          const choices = element("q:#rate .choices").children;
          await choices[1].on.click();
          await until(() => ledger().length, 1000);
          return { question, choices: choices.map((item) => item.textContent), sale: ledger()[0], error: element("error").textContent };""",
                             rates={"kraken": {"result": {"X": {"c": ["100000"]}}},
                                    "coinbase": {"data": {"currency": "EUR", "amount": "125000"}}},
                             sources=["kraken", "coinbase"], sats=8000)
        self.assertIn("The two sources do not agree", out["question"], out)
        self.assertIn("sale", out, out)
        self.assertEqual(len(out["choices"]), 2)
        # 10.00 EUR at 125,000 EUR per bitcoin, as the merchant chose
        self.assertEqual((out["sale"]["sats"], out["sale"]["how"], out["sale"]["rate"]), (8000, "chosen", 125000))

    def test_each_source_is_read_its_own_way(self):
        """Four sources that all disagree: each one's price, read with its own parser, is put before the merchant."""
        out = self.lightning("""
          await sell("1000");
          return { question: element("question").textContent, ledger: ledger() };""",
                             rates={"bitstamp": {"last": "101000"}, "mempool.space/api/v1/prices": {"EUR": 111000},
                                    "coingecko": {"bitcoin": {"eur": 121000}}, "blockchain.info": {"EUR": {"last": 131000}}},
                             sources=["bitstamp", "mempool", "coingecko", "blockchain"])
        self.assertIn("The sources do not agree", out["question"])
        for name, price in (("Bitstamp", "101"), ("mempool.space", "111"), ("CoinGecko", "121"), ("blockchain.info", "131")):
            self.assertRegex(out["question"], rf"{price}[,.]000\.00 \({name}\)")
        self.assertEqual(out["ledger"], [])

    def test_sources_that_agree_charge_at_once_with_their_middle(self):
        out = self.lightning("""
          return (await sell("1000")) || { question: element("question").textContent, error: element("error").textContent, asked };""",
                             rates={"bitstamp": {"last": "100000"}, "coingecko": {"bitcoin": {"eur": 100500}}},
                             sources=["bitstamp", "coingecko"], sats=9975)
        self.assertEqual((out.get("how"), out.get("rate")), ("agreed", 100250), out)
        self.assertEqual([item["source"] for item in out["sources"]], ["bitstamp", "coingecko"])

    def test_a_single_source_is_only_offered(self):
        out = self.lightning("""
          await sell("1000");
          return { question: element("question").textContent, ledger: ledger() };""",
                             rates={"coinbase": None}, sources=["kraken", "coinbase"])
        self.assertIn("Only one source answers", out["question"])
        self.assertEqual(out["ledger"], [])

    def test_with_no_source_the_last_good_rate_is_offered_while_not_too_old(self):
        rates = {"kraken": None, "coinbase": None}
        script = """
          await sell("1000");
          return { question: element("question").textContent, buttons: (element("q:#rate .choices").children || []).length };"""
        for minutes, said, buttons in ((10, "The last good rate is", 1), (200, "the last good rate is too old", 0),
                                       (None, "the register cannot calculate", 0)):
            storage = {} if minutes is None else {"register-rate:EUR": json.dumps(
                {"price": 90000, "at": time.time() * 1000 - minutes * 60000, "sources": []})}
            with self.subTest(minutes):
                out = self.lightning(script, storage=storage, rates=rates, sources=["kraken", "coinbase"])
                self.assertIn(said, out["question"])
                self.assertEqual(out["buttons"], buttons)


class Waiting(Paths):
    def test_sales_left_waiting_are_ended_at_opening(self):
        """The page closed during a sale: at the next opening each Lightning sale left waiting is looked at again."""
        now = time.time() * 1000
        pr, payment_hash = invoice(10000)
        sale = {"cents": 1000, "currency": "EUR", "status": "pending", "sats": 10000, "pr": pr, "hash": payment_hash,
                "method": "lightning", "till": "shop-1"}
        verify = lambda at: f"https://ln.example.org/verify/{at}"  # noqa: E731
        sales = [
            {**sale, "id": "no verify, over", "at": now - 900000, "expires": now - 1000},
            {**sale, "id": "no verify, still on", "at": now - 60000, "expires": now + 600000},
            {**sale, "id": "paid", "at": now - 900000, "expires": now - 1000, "verify": verify(0)},
            {**sale, "id": "unpaid, over", "at": now - 900000, "expires": now - 1000, "verify": verify(1)},
            {**sale, "id": "no answer, over", "at": now - 900000, "expires": now - 1000, "verify": verify(2)},
        ]
        out = self.run_register(OWNER, {**SETTINGS, "register-ledger:shop-1": json.dumps(sales)}, discount=0,
                                ln={"invoices": [pr] * 3, "verify": {0: {**PAID, "pr": pr}, 1: {**UNPAID, "pr": pr},
                                                                     2: {"status": "ERROR", "reason": "down"}}},
                                script="await wait(200); return ledger();")
        self.assertEqual({entry["id"]: entry["status"] for entry in out}, {
            "no verify, over": "unchecked", "no verify, still on": "pending", "paid": "paid", "unpaid, over": "expired",
            "no answer, over": "unchecked"})

    def test_without_verify_the_merchant_says_it_is_paid(self):
        out = self.lightning("""
          await sell("1000");
          const shown = !element("seen").hidden;
          await click("seen");
          return { shown, ledger: ledger(), result: element("result").textContent };""", no_verify=True)
        self.assertTrue(out["shown"])
        self.assertEqual([sale["status"] for sale in out["ledger"]], ["confirmed"])
        self.assertTrue(out["result"])

    TAP = """
          key("Escape");
          for (const k of "1000") key(k);
          const on = element("discount").textContent;
          const button = !element("discount").hidden;
          const fixed = element("discount-fixed").hidden ? null : element("discount-fixed").textContent;
          await click("discount");
          const off = element("discount").textContent;
          const sale = await sell("1000");
          return { on, off, button, fixed, sale };"""

    def test_the_discount_is_taken_away_for_one_sale_with_a_tap_when_the_owner_allows_it(self):
        allowed = {"register-settings:shop-1": json.dumps({"lightning": ADDRESS, "discountRemovable": True})}
        out = self.lightning(self.TAP, storage=allowed, discount=10)
        self.assertTrue(out["button"])
        self.assertIsNone(out["fixed"])
        self.assertIn("9.00", out["on"])
        self.assertNotEqual(out["on"], out["off"])
        self.assertEqual((out["sale"]["cents"], out["sale"].get("discount")), (1000, None))

    def test_by_default_the_discount_cannot_be_taken_away(self):
        out = self.lightning(self.TAP, discount=10, sats=9000)
        self.assertFalse(out["button"])
        self.assertIn("9.00", out["fixed"])
        self.assertNotIn("Tap", out["fixed"])
        # A tap that reaches the button all the same changes nothing: the sale has the discount
        self.assertEqual((out["sale"]["cents"], out["sale"]["list"], out["sale"]["discount"]), (900, 1000, 10))

    def test_a_staff_register_follows_the_owners_choice(self):
        script = r"""
const fs = require("fs");
for (const f of process.argv.slice(1)) eval(fs.readFileSync(f, "utf8"));
const base = { register: "%s", lightning: "%s", confirmAbove: 0, discount: 10, currency: "EUR",
  sources: ["kraken", "coinbase"], shop: { name: "Bar" } };
const sources = ["kraken", "coinbase", "bitstamp"];
const off = registerPairing.encode(base, sources);
const on = registerPairing.encode({ ...base, discountRemovable: true }, sources);
process.stdout.write(JSON.stringify({ off, on, read: [registerPairing.decode(off, sources).discountRemovable,
  registerPairing.decode(on, sources).discountRemovable] }));
""" % (REGISTER, ADDRESS)
        made = subprocess.run([self.node, "-e", script, str(STATIC / "register-pairing.js")], capture_output=True,
                              text=True, timeout=60)
        self.assertEqual(made.returncode, 0, made.stderr)
        codes = json.loads(made.stdout)
        self.assertEqual(codes["read"], [False, True])
        ledger = f"shop-1:{REGISTER}"
        dataset = {"ledger": ledger, "shop": "shop-1", "staff": REGISTER, "tillName": "Anna"}
        for code, removable in ((codes["off"], False), (codes["on"], True)):
            out = self.lightning(self.TAP, storage={f"register-pairing:{ledger}": json.dumps({"code": code, "at": 1})},
                                 dataset=dataset, chain_claim={"url": "/c/", "key": "k" * 40},
                                 sats=10000 if removable else 9000)
            self.assertEqual(out["button"], removable)
            self.assertEqual(out["fixed"] is None, removable)
            self.assertEqual(out["sale"]["cents"], 1000 if removable else 900)


class History(Paths):
    def sold(self):
        now = time.time() * 1000
        return {"register-ledger:shop-1": json.dumps([
            {"id": "one", "at": now, "cents": 1250, "label": "12.50 EUR", "currency": "EUR", "status": "paid",
             "sats": 12500, "method": "lightning", "till": "shop-1"}])}

    def test_the_day_closing_is_kept_and_said(self):
        out = self.steps("""
          const day = registerBook.businessDay(Date.now());
          element("day").value = day;
          await click("close-day");
          return { day, closings: JSON.parse(kept.get("register-closings:shop-1")), say: element("history-say").textContent };""",
                       self.sold())
        self.assertIn(out["day"], out["closings"])
        self.assertTrue(out["say"].startswith("Closed at"), out["say"])

    def test_the_csv_has_the_sales_of_the_month(self):
        out = self.steps("""
          await submit("csv-form");
          return await downloaded();""", self.sold())
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0]["name"].endswith("-all.csv"), out[0]["name"])
        self.assertIn("12.50", out[0]["text"])
        # A heading for every column
        lines = [line for line in out[0]["text"].lstrip("\ufeff").splitlines() if line]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0].count(","), lines[1].count(","))

    def test_the_copy_is_refused_with_a_short_or_mismatched_passphrase_and_comes_back_only_with_its_own(self):
        out = self.steps("""
          const said = [];
          for (const [pass, again] of [["short", "short"], ["a passphrase ok", "another one"], ["a passphrase ok", "a passphrase ok"]]) {
            element("export-pass").value = pass;
            element("export-again").value = again;
            await submit("export-form");
            said.push(element("history-say").textContent);
          }
          const files = await downloaded();
          const tries = [];
          for (const [content, pass] of [[files[0]?.text, "the wrong one"], ["not json", "a passphrase ok"],
                                         ['{"hello": 1}', "a passphrase ok"]]) {
            element("import-file").files = [{ size: content.length, text: async () => content }];
            element("import-pass").value = pass;
            await submit("import-form");
            tries.push(element("history-say").textContent);
          }
          return { said, files, tries, ledger: ledger() };""", self.sold())
        self.assertIn("at least", out["said"][0])
        self.assertEqual(out["said"][1], "The two passphrases are different.")
        self.assertTrue(out["said"][2].startswith("Copy downloaded."), out["said"][2])
        self.assertEqual(len(out["files"]), 1)
        self.assertTrue(out["files"][0]["name"].endswith(".json"))
        self.assertNotIn("12.50", out["files"][0]["text"])
        self.assertEqual(out["tries"], ["Wrong passphrase, or a damaged file.", "This file is not a copy of a register.",
                                        "This file is not a copy of a register."])
        self.assertEqual([sale["id"] for sale in out["ledger"]], ["one"])


class Rate(Paths):
    def test_a_third_source_settles_two_that_disagree_and_every_answer_keeps_its_moment(self):
        out = self.lightning("""
          return { sale: await sell("1000"), asked: asked.filter((url) => !url.includes("ln.example.org")) };""",
                             rates={"kraken": {"result": {"X": {"c": ["100000"]}}},
                                    "coinbase": {"data": {"currency": "EUR", "amount": "125000"}}, "bitstamp": {"last": "100100"}},
                             sources=["kraken", "coinbase", "bitstamp"], sats=9995)
        sale = out["sale"]
        self.assertEqual((sale["how"], sale["rate"]), ("agreed", 100050))
        self.assertEqual([item["source"] for item in sale["sources"]], ["kraken", "bitstamp"])
        self.assertTrue(all(isinstance(item["at"], (int, float)) and item["price"] for item in sale["sources"]))
        # The first two together, then the third: each once
        self.assertEqual(len(out["asked"]), 3)


class OneLookAtATime(Paths):
    def test_the_round_the_screen_and_cancel_together_end_the_sale_once(self):
        """The provider says paid while the round, the screen turning back on (twice) and Cancel all look: the sale ends
        once, paid, and its finished line in the book is small."""
        out = self.lightning("""
          await sell("1000");
          ln.verify[0] = { ...ln.verify[0], settled: true, preimage: "00".repeat(32) };
          const screen = listeners["document:visibilitychange"];
          await Promise.all([...screen.map((fn) => fn()), ...screen.map((fn) => fn()), click("cancel")]);
          await until(() => ledger()[0].status !== "pending", 1000);
          await wait(50);
          return { ledger: ledger(), result: element("result").textContent };""")
        self.assertEqual([sale["status"] for sale in out["ledger"]], ["paid"])
        self.assertNotIn("qr", out["ledger"][0])

    def test_a_long_history_is_kept_whole(self):
        now = time.time() * 1000
        old = [{"id": f"s{n}", "at": now - 86400000 - n, "cents": 100, "currency": "EUR", "status": "paid", "sats": 1000,
                "method": "lightning", "till": "shop-1"} for n in range(3000)]
        out = self.lightning("""
          await sell("1000");
          await click("cancel");
          return ledger().length;""", storage={"register-ledger:shop-1": json.dumps(old)})
        self.assertEqual(out, 3001)


class Retired(Paths):
    def test_a_register_turned_off_takes_no_sale(self):
        out = self.lightning("""
          await sell("1000");
          return { ledger: ledger(), asked };""", retired=True)
        self.assertEqual(out["ledger"], [])
        self.assertFalse([url for url in out["asked"] if "ln.example.org" in url])


class KeepTheData(Paths):
    def test_the_first_opening_says_where_the_sales_are_and_asks_the_browser_after_the_tap(self):
        script = """
          const first = !element("keep-first").hidden;
          const before = persistAsked;
          await click("keep-ok");
          return { first, before, after: persistAsked, hidden: element("keep-first").hidden,
            warned: kept.get("register-warned:shop-1") };"""
        out = self.steps(script, persisted=False)
        self.assertEqual((out["first"], out["before"], out["after"], out["hidden"], out["warned"]), (True, 0, 1, True, "true"))
        self.assertFalse(self.steps(script, {"register-warned:shop-1": "true"})["first"])

    def test_every_reminder_choice_of_the_page_is_kept(self):
        out = self.steps("""
          const kept_choices = [];
          for (const every of ["manual", "daily", "weekly", "monthly", "closing"]) {
            listeners["copy-every:change"].forEach((fn) => fn({ target: { value: every } }));
            kept_choices.push(JSON.parse(kept.get("register-copy:shop-1")).every);
          }
          return kept_choices;""")
        self.assertEqual(out, ["manual", "daily", "weekly", "monthly", "closing"])


class StaffPairing(Paths):
    """A pairing code opened on this phone (static/staff-open.js) is taken only by its register, only fresh, and only
    while the shop's Lightning address is the one in the code."""

    def code(self, register, lightning=ADDRESS):
        script = ('const fs = require("fs"); eval(fs.readFileSync(process.argv[1], "utf8"));'
                  f'process.stdout.write(registerPairing.encode({{ register: {json.dumps(register)}, '
                  f'lightning: {json.dumps(lightning)}, confirmAbove: 0, discount: 0, currency: "EUR", '
                  'sources: ["kraken", "coinbase"], shop: { name: "Bar", town: "Example" } }, ["kraken", "coinbase", "bitstamp"]));')
        return subprocess.run([self.node, "-e", script, str(STATIC / "register-pairing.js")], capture_output=True,
                              text=True, timeout=60, check=True).stdout

    def open(self, pending):
        dataset = {"ledger": f"shop-1:{REGISTER}", "shop": "shop-1", "staff": REGISTER, "tillName": "Anna"}
        return self.run_register(dataset, {"register-pairing-pending": json.dumps(pending)}, discount=0, script="""
          await wait(50);
          return { blocked: element("blocked-text").textContent, paired: Boolean(kept.get(`register-pairing:${given.dataset.ledger}`)),
            pending: kept.get("register-pairing-pending") ?? null };""")

    def test_only_its_register_only_fresh(self):
        now = time.time() * 1000
        mine = self.code(REGISTER)
        taken = self.open({"register": REGISTER, "at": now, "code": mine})
        self.assertEqual((taken["paired"], taken["blocked"]), (True, ""))
        for name, pending in (("another register", {"register": "other", "at": now, "code": mine}),
                              ("too old", {"register": REGISTER, "at": now - 16 * 60000, "code": mine}),
                              ("the code of another", {"register": REGISTER, "at": now, "code": self.code("0000aaaa-0000-4000-8000-000000000009")})):
            with self.subTest(name):
                out = self.open(pending)
                self.assertFalse(out["paired"])
                self.assertIn("not paired yet", out["blocked"])
                # Read once: it does not wait on the phone for a later opening
                self.assertIn(out["pending"], (None, "null"))

    def test_a_changed_lightning_address_stops_the_paired_register(self):
        out = self.open({"register": REGISTER, "at": time.time() * 1000, "code": self.code(REGISTER, "old@ln.example.org")})
        self.assertTrue(out["paired"])
        self.assertIn("changed after this register was paired", out["blocked"])

"""On-chain at the register, all in the browser: the addresses derived from the key on the merchant's phone
(static/register-derive.js), what an explorer's answer says about a sale (static/register-chain.js) and the pairing code
of a staff register (static/register-pairing.js).

The derivation is checked against the published test vectors of BIP32 and BIP84: an address derived wrong is money
sent where nobody can spend it. The scripts run in node (NODE=/path/to/node, or node on the PATH); without node only
what can be read in the source is checked."""
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase
from .test_scripts import assert_talks_to_nobody

STATIC = settings.BASE_DIR / "static"
SCRIPTS = [STATIC / name for name in ("register-derive.js", "register-chain.js", "register-pairing.js")]
EXPLORER = Path(__file__).parent / "testdata" / "explorer.json"

# BIP32, test vector 2: the master key and its child 0; test vector 1: m/0H and its child 1 (non hardened)
BIP32_MASTER = ("xpub661MyMwAqRbcFW31YEwpkMuc5THy2PSt5bDMsktWQcFF8syAmRUapSCGu8ED9W6oDMSgv6Zz8idoc4a6mr8BDzTJY47LJhkJ8"
                "UB7WEGuduB")
BIP32_MASTER_0 = ("xpub69H7F5d8KSRgmmdJg2KhpAK8SR3DjMwAdkxj3ZuxV27CprR9LgpeyGmXUbC6wb7ERfvrnKZjXoUmmDznezpbZb7ap6r1D3tgFx"
                  "HmwMkQTPH")
BIP32_0H = ("xpub68Gmy5EdvgibQVfPdqkBBCHxA5htiqg55crXYuXoQRKfDBFA1WEjWgP6LHhwBZeNK1VTsfTFUHCdrfp1bgwQ9xv5ski8PX9rL2dZX"
            "vgGDnw")
BIP32_0H_1 = ("xpub6ASuArnXKPbfEwhqN6e3mwBcDTgzisQN1wXN9BJcM47sSikHjJf3UFHKkNAWbWMiGj7Wf5uMash7SyYq527Hqck2AxYysAA7xmA"
              "LppuCkwQ")
# BIP84: the account key of «abandon abandon … about» and its addresses m/84'/0'/0'/0/0, /0/1 and /1/0
BIP84_ACCOUNT = ("zpub6rFR7y4Q2AijBEqTUquhVz398htDFrtymD9xYYfG1m4wAcvPhXNfE3EfH1r1ADqtfSdVCToUG868RvUUkgDKf31mGDtKsAYz2oz"
                 "2AGutZYs")
BIP84_RECEIVE = ["bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu", "bc1qnjg0jd8228aq7egyzacy8cys3knf9xvrerkf9g"]
BIP84_CHANGE = "bc1q8c6fshw2dlwun7ekn9qwf37cu2rn755upcp6el"
# The same account key written for signet (vpub) and as a tpub: what a merchant of the test network pastes
VPUB = ("vpub5YvMuJNjRSYon44z9QmCfdf8SqJRVNvz6m55Qy5iVjZQxDfUgtiQjnc7CC1fAbED2tAGCZRERUfvtn2DstZGU6HMns6dXXH2wujSc"
        "2wfi2x")
TPUB = ("tpubDCxX2sYFS5bDkSe5GKKYHjBW7tgyN1R3UchpLJvdbf54ohxeGRtd8MbDUe1cguVHe4vnK68DsuD5MXjxi9EXx16rb9EnNsaF5KT99C"
        "inaJz")
REGISTER = "0f0e0d0c-0b0a-4908-8706-050403020100"

HARNESS = r"""
const fs = require("fs");
for (const file of process.argv.slice(2, 5)) eval(fs.readFileSync(file, "utf8"));
const input = JSON.parse(fs.readFileSync(process.argv[5], "utf8"));
const derive = globalThis.registerDerive;
const chain = globalThis.registerChain;
const pairing = globalThis.registerPairing;
const reason = async (work) => {
  try { await work(); return "ok"; } catch (error) { return error.reason || String(error); }
};
(async () => {
  const out = {};
  out.bip32 = [await derive.childKey(input.master, 0, "mainnet"), await derive.childKey(input.zeroH, 1, "mainnet")];
  out.receive = [await derive.addressAt(input.account, [0, 0], "mainnet"), await derive.addressAt(input.account, [0, 1], "mainnet")];
  out.change = await derive.addressAt(input.account, [1, 0], "mainnet");
  // A staff register's key …/k gives the same addresses as the owner's key at k/n
  const staff = await derive.childKey(input.account, 3, "mainnet");
  out.staffKey = staff;
  out.staff = [await derive.addressAt(staff, [0], "mainnet"), await derive.addressAt(input.account, [3, 0], "mainnet")];
  out.parsedStaff = await derive.parseKey(staff, "mainnet").then((key) => ({ depth: key.depth, kind: key.kind }));
  out.destination = await derive.destination(input.account, "mainnet");
  out.destinations = [];
  for (const text of input.destinations) out.destinations.push(await derive.destination(text, input.network));
  out.keyReasons = [];
  for (const text of input.badKeys) out.keyReasons.push(await reason(() => derive.parseKey(text, "mainnet")));
  out.payment = input.payments.map(([txs, address, sats, before, wait, spent]) => chain.payment(txs, address, sats, before,
    wait, spent));
  // The sale's time: only a payment in it, of about the amount asked, closes the sale
  const T = 1762700000000;
  const H = 3600000;
  const tx = (value, status, txid = "b".repeat(64)) => [{ txid, status, vout: [{ scriptpubkey_address: input.address, value }] }];
  const block = (at) => ({ confirmed: true, block_time: Math.floor(at / 1000) });
  const within = (txs, seenAt = {}, now = T + 5 * 60000) => chain.payment(txs, input.address, 9000, [], true, [],
    { from: T, to: T + H, now, seenAt });
  out.windows = [
    within(tx(9000, block(T + 600000))), within(tx(9000, block(T - 3 * H))), within(tx(9000, block(T + 4 * H))),
    within(tx(9000, { confirmed: false })), within(tx(9000, block(T + 30 * H)), { ["b".repeat(64)]: T + 60000 }, T + 30 * H),
    within(tx(10800, block(T + 600000))), within(tx(9900, block(T + 600000))), within(tx(9000, { confirmed: true })),
    within(tx(9000, { confirmed: false }), {}, T + 4 * H),
  ].map((result) => [result.state, result.outside, Object.keys(result.seenAt).length]);
  // The confidence of a payment in the mempool: fee rate against the current rates, replaceability
  const fees = { fastestFee: 20, halfHourFee: 10, hourFee: 5, economyFee: 2, minimumFee: 1 };
  const mem = (fee, sequence = 0xffffffff) => [{ txid: "c".repeat(64), status: { confirmed: false }, fee, weight: 400,
    vin: [{ txid: "d".repeat(64), vout: 0, sequence }], vout: [{ scriptpubkey_address: input.address, value: 9000 }] }];
  const quick = (txs, f = fees) => chain.payment(txs, input.address, 9000, [], false, [],
    { from: T, to: T + H, now: T + 60000, seenAt: {}, fees: f, ancestors: { ["c".repeat(64)]: { count: 0, rate: null } } });
  out.confidence = [quick(mem(2000)), quick(mem(700)), quick(mem(300)), quick(mem(2000, 0xfffffffd)), quick(mem(2000), null),
    quick(mem(2000, 1), null), quick(tx(9000, block(T + 60000)))].map((result) => [result.state, result.confidence.level,
    result.confidence.reason]);
  // The transaction's own facts: its parents (CPFP) and the confirmations
  const parent = (count, rate) => ({ [("c".repeat(64))]: chain.ancestry({ ancestors: Array(count).fill({}), effectiveFeePerVsize: rate }) });
  const full = (txs, more) => chain.payment(txs, input.address, 9000, [], false, [],
    { from: T, to: T + H, now: T + 60000, seenAt: {}, fees, ...more }).confidence;
  out.facts = [full(mem(2500), {}), full(mem(2500), { ancestors: parent(0, 25) }), full(mem(2500), { ancestors: parent(1, 25) }),
    full(mem(300), { ancestors: parent(1, 7) }), full(mem(2000, 0xfffffffd), {}),
    full([{ ...tx(9000, { ...block(T + 60000), block_height: 900000 })[0] }], { tip: 900002 }),
    full(tx(9000, block(T + 60000)), { tip: 900002 })];
  out.ancestry = [chain.ancestry({ ancestors: [] }), chain.ancestry({ ancestors: [{}], effectiveFeePerVsize: "x" }), chain.ancestry({}),
    chain.tipHeight(900000), chain.tipHeight("900000"), chain.cpfpUrl("https://m.example/api/", "c".repeat(64)),
    chain.tipUrl("https://m.example/api")];
  out.feeRates = [chain.feeRates(fees), chain.feeRates({ hourFee: "5" }), chain.feeRates(null),
    chain.feeRates({ halfHourFee: 10, hourFee: 5 })];
  out.feesUrl = chain.feesUrl("https://mempool.space/api/");
  out.spent = chain.spent(input.payments.at(-1)[0], input.address);
  out.known = chain.known(input.payments[0][0], input.address);
  out.waits = [chain.waits(1000, 0), chain.waits(1000, 1000), chain.waits(1001, 1000), chain.waits(1, null)];
  out.url = chain.txsUrl("https://mempool.space/signet/api/", "tb1qx");
  out.uri = [chain.uri("bc1qx", 123456789), chain.uri("bc1qx", 100000000), chain.uri("bc1qx", 1)];
  const sources = ["kraken", "coinbase", "bitstamp"];
  const good = { register: input.register, lightning: "shop@wallet.example", onchain: { key: staff, path: "0" },
    confirmAbove: 5000, discount: 10, currency: "EUR", sources: ["kraken", "coinbase"],
    shop: { name: "Bar Aurora «by Anna»", colour: "#224455", town: "Example" }, note: "Thank you!" };
  const code = pairing.encode(good, sources);
  out.code = code;
  out.decoded = pairing.decode(code, sources);
  out.fixed = pairing.decode(pairing.encode({ ...good, onchain: { address: input.address }, note: "" }, sources), sources);
  out.lightningOnly = pairing.decode(pairing.encode({ ...good, onchain: null }, sources), sources);
  // The shop's chain: the chain's key and the owner's path; paths that are not one are refused
  out.shared = ["0", "0/5", "1/2/3/4"].map((path) => pairing.decode(pairing.encode({ ...good, onchain: { key: staff, path } },
    sources), sources).onchain);
  out.badPaths = [];
  for (const path of ["", "0'", "0h", "1/2/3/4/5", "01", "2147483648", "-1", "a", "0//1", 0]) {
    out.badPaths.push(await reason(() => pairing.encode({ ...good, onchain: { key: staff, path } }, sources)));
  }
  out.steps = [pairing.steps("0"), pairing.steps("2147483647/0"), pairing.steps("2147483648"), pairing.steps(null)];
  const wrap = (data) => `1.${Buffer.from(JSON.stringify(data)).toString("base64url")}`;
  const base = JSON.parse(Buffer.from(code.slice(2), "base64url").toString());
  out.pairingReasons = [];
  for (const variant of [
    { ...base, extra: 1 }, { ...base, v: 3 }, { ...base, r: "x" }, { ...base, ln: "nobody" }, { ...base, ln: "a b@c.de" },
    { ...base, oc: { x: staff, k: 1 } }, { ...base, oc: { x: staff, k: 3 } }, { ...base, oc: { x: staff, k: 3, a: input.address } }, { ...base, oc: { a: "<script>" } },
    { ...base, cf: -1 }, { ...base, d: 101 }, { ...base, d: 9.5 }, { ...base, cu: "eur" }, { ...base, ps: ["kraken"] },
    { ...base, ps: ["kraken", "evil"] }, { ...base, ps: ["kraken", "kraken"] }, { ...base, s: { n: "" } },
    { ...base, s: { n: "A", c: "red" } }, { ...base, s: { n: "A\u0000" } }, { ...base, m: "x".repeat(141) }, [base],
  ]) out.pairingReasons.push(await reason(() => pairing.decode(wrap(variant), sources)));
  for (const text of ["3.e30", "1.!!", "1." + "A".repeat(3000), "nonsense", "1.bnVsbA", null]) {
    out.pairingReasons.push(await reason(() => pairing.decode(text, sources)));
  }
  out.encodeBad = await reason(() => pairing.encode({ ...good, discount: 200 }, sources));
  // Version 2: the copies on Nostr, with only the public relays of community.yml
  const relays = ["wss://nos.lol", "wss://relay.example.org"];
  const nostr = { owner: "ab".repeat(32), secret: "cd".repeat(32), relays: ["wss://nos.lol"] };
  const two = pairing.encode({ ...good, nostr }, sources, relays);
  out.two = [two.slice(0, 2), pairing.decode(two, sources, relays).nostr, pairing.decode(code, sources, relays).nostr, code.slice(0, 2)];
  // A relay the community no longer lists is left out, the pairing kept
  out.dropped = pairing.decode(two, sources, ["wss://relay.example.org"]).nostr;
  const base2 = JSON.parse(Buffer.from(two.slice(2), "base64url").toString());
  const wrap2 = (data) => `2.${Buffer.from(JSON.stringify(data)).toString("base64url")}`;
  out.twoReasons = [];
  for (const variant of [
    { ...base2, no: { ...nostr, r: ["wss://nos.lol", "wss://nos.lol"] } }, { ...base2, no: { ...nostr, r: [1] } },
    { ...base2, no: { p: nostr.owner, k: nostr.secret } },
    { ...base2, no: { ...base2.no, p: "AB".repeat(32) } }, { ...base2, no: { ...base2.no, k: "cd" } },
    { ...base2, no: { ...base2.no, x: 1 } }, { ...base2, no: undefined }, { ...base2, v: 1 },
  ]) out.twoReasons.push(await reason(() => pairing.decode(wrap2(variant), sources, relays)));
  out.twoReasons.push(await reason(() => pairing.decode(wrap({ ...base, no: base2.no }), sources, relays)));
  process.stdout.write(JSON.stringify(out));
})().catch((error) => { console.error(error); process.exit(1); });
"""


class Source(SimpleTestCase):
    def test_the_scripts_talk_to_nobody(self):
        for script in SCRIPTS:
            assert_talks_to_nobody(self, script.name, also=("localStorage", "document."))

    def test_the_register_pages_load_them_before_the_register(self):
        page = (settings.BASE_DIR / "templates" / "shops" / "register.html").read_text()
        self.assertLess(page.index("<script src=\"{% static 'register-pairing.js' %}\"></script>"), page.index("{% block scripts %}"))
        # On-chain is loaded at its first use, from the addresses the page gives (static/register.js, need)
        for name in ("derive", "chain"):
            self.assertIn(f"data-{name}=\"{{% static 'register-{name}.js' %}}\"", page)


class Settings(SimpleTestCase):
    def test_the_price_sources_are_in_the_owners_advanced_section(self):
        page = (settings.BASE_DIR / "templates" / "shops" / "register.html").read_text()
        advanced, sources = page.index('<details id="advanced"'), page.index('<fieldset id="sources"')
        end = page.index("</details>\n    <p id=\"settings-say\"", advanced)
        self.assertLess(page.index('{% if not staff and not practice %}<section id="settings"'), advanced)
        self.assertLess(advanced, sources)
        self.assertLess(sources, end)
        # The owner is told that these are the wallet's own receive addresses, under «How on-chain works»
        self.assertIn("the same addresses your wallet may give to someone else", page)


class Running(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        node = os.environ.get("NODE") or shutil.which("node")
        if not node:
            raise __import__("unittest").SkipTest("node is missing: NODE=/path/to/node to run the register's on-chain scripts")
        recorded = json.loads(EXPLORER.read_text())["addresses"]
        address, confirmed, change = BIP84_RECEIVE[0], BIP84_RECEIVE[1], BIP84_CHANGE
        cls.address = address
        replacement = {"txid": "e" * 64, "status": {"confirmed": False}, "vin": [{"txid": "d" * 64, "vout": 1}, {"txid": "bad"}],
                       "vout": [{"scriptpubkey_address": address, "value": 9000}]}
        data = {
            "master": BIP32_MASTER, "zeroH": BIP32_0H, "account": BIP84_ACCOUNT, "register": REGISTER, "address": address,
            "network": "signet",
            "destinations": [
                VPUB, "tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx", "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
                "tprv8ZgxMBicQKsPd7Uf69XL1XwhmjHopUGep8GuEiJDZmbQz6o58LninorQAfcKZWARbtRtfnLcJ5MQ2AtHcQJCCRUcMRvmDUjyEmNU"
                "WwGP3", "tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsy", ""],
            "badKeys": [BIP32_MASTER[:-1] + "C", BIP32_MASTER[:50], "0OIl", TPUB],
            "payments": [
                [recorded[address], address, 9000, [], False], [recorded[address], address, 9000, [], True],
                [recorded[confirmed], confirmed, 9000, [], True], [recorded[change], change, 9000, [], False],
                [[], address, 9000, [], False], [recorded[confirmed], confirmed, 9000, ["c" * 64], True],
                [recorded[address], "bc1qother", 9000, [], False], [{"error": "no"}, address, 9000, [], False],
                [recorded[change] * 2, change, 9000, [], False],
                [[replacement], address, 9000, [], False], [[replacement], address, 9000, [], False, ["d" * 64 + ":1"]]],
        }
        with tempfile.TemporaryDirectory() as folder:
            harness, given = Path(folder) / "harness.js", Path(folder) / "input.json"
            harness.write_text(HARNESS)
            given.write_text(json.dumps(data))
            result = subprocess.run([node, str(harness), *map(str, SCRIPTS), str(given)], capture_output=True, text=True,
                                    timeout=120)
        if result.returncode:
            raise AssertionError(result.stderr)
        cls.out = json.loads(result.stdout)

    def test_bip32_public_children(self):
        self.assertEqual(self.out["bip32"], [BIP32_MASTER_0, BIP32_0H_1])

    def test_bip84_addresses(self):
        self.assertEqual(self.out["receive"], BIP84_RECEIVE)
        self.assertEqual(self.out["change"], BIP84_CHANGE)
        self.assertEqual(self.out["destination"], {"kind": "key", "key": BIP84_ACCOUNT, "first": BIP84_RECEIVE[0]})

    def test_a_staff_register_pays_into_the_owner_key_at_its_own_child(self):
        first, owner_view = self.out["staff"]
        self.assertEqual(first, owner_view)
        self.assertTrue(self.out["staffKey"].startswith("zpub"))
        self.assertEqual(self.out["parsedStaff"], {"depth": 4, "kind": "p2wpkh"})

    def test_what_the_merchant_pastes(self):
        vpub, signet, mainnet, private, typo, empty = self.out["destinations"]
        self.assertEqual(vpub["kind"], "key")
        self.assertTrue(vpub["first"].startswith("tb1q"))
        self.assertEqual(signet, {"kind": "address", "address": "tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx"})
        # A mainnet address on signet, a private key, a typo: refused, with the reason
        self.assertEqual(mainnet, {"kind": None, "reason": "network"})
        self.assertEqual(private, {"kind": None, "reason": "private"})
        self.assertEqual(typo["kind"], None)
        self.assertEqual(empty["kind"], None)
        self.assertEqual(self.out["keyReasons"], ["checksum", "format", "format", "network"])

    def test_what_the_explorer_says(self):
        seen, waiting, confirmed, partial, empty, earlier, other, broken, twice, new, replaced = self.out["payment"]
        self.assertEqual(seen["state"], "paid")
        self.assertEqual(waiting["state"], "seen")
        self.assertEqual(confirmed["state"], "paid")
        self.assertEqual((partial["state"], partial["seen"]), ("partial", 5000))
        self.assertEqual(empty["state"], "none")
        # On a fixed address a payment that was there before the sale is not this sale's
        self.assertEqual(earlier["state"], "none")
        self.assertEqual(other["state"], "none")
        self.assertEqual(broken["state"], "unknown")
        # The same transaction listed twice is counted once
        self.assertEqual((twice["state"], twice["seen"]), ("partial", 5000))
        # A transaction that spends what one there at the start spent replaces it (RBF): not a new payment
        self.assertEqual((new["state"], replaced["state"]), ("paid", "none"))
        self.assertEqual(self.out["spent"], ["d" * 64 + ":1"])
        self.assertEqual(self.out["known"], ["a" * 64])
        self.assertEqual(self.out["waits"], [True, False, True, True])
        self.assertEqual(self.out["url"], "https://mempool.space/signet/api/address/tb1qx/txs")
        self.assertEqual(self.out["uri"], ["bitcoin:bc1qx?amount=1.23456789", "bitcoin:bc1qx?amount=1",
                                           "bitcoin:bc1qx?amount=0.00000001"])

    def test_only_a_payment_in_the_sales_time_and_of_its_amount_closes_it(self):
        self.assertEqual(self.out["windows"], [
            ["paid", 0, 0],
            # A block from before the sale (beyond the two hours of a block's clock), or long after its end: not this sale's
            ["outside", 9000, 0], ["outside", 9000, 0],
            # In the mempool during the sale: seen, and the moment kept
            ["seen", 0, 1],
            # Seen in time, its block after the end: still this sale's
            ["paid", 0, 1],
            # Much more than asked: maybe a payment for something else; up to 10% more is taken
            ["over", 0, 0], ["paid", 0, 0],
            # A confirmed payment without a block time cannot be placed in time: not taken
            ["outside", 9000, 0],
            # First seen in the mempool long after the sale's end: not taken, nor remembered
            ["outside", 9000, 0],
        ])

    def test_the_confidence_of_a_payment_in_the_mempool(self):
        # A sale that needs no confirmation: paid from the mempool unless the confidence is low
        self.assertEqual(self.out["confidence"], [
            ["paid", "sure", "fee_next"],          # 20 sat/vB, the next block's rate
            ["paid", "likely", "fee_hour"],        # 7 sat/vB: between the hour and the half-hour rates
            ["seen", "wait", "fee_low"],           # 3 sat/vB, below the hour's rate: may wait long or be replaced
            ["paid", "likely", "rbf"],             # a good fee, but it signals replaceability
            ["paid", "likely", "fee_unknown"],     # an explorer without fee rates
            ["paid", "likely", "fee_unknown"],     # …and replaceable: a hint only, most wallets signal it
            ["paid", "sure", "confirmed"],
        ])
        self.assertEqual(self.out["feeRates"], [{"next": 20, "halfHour": 10, "hour": 5}, None, None,
                                                {"next": 10, "halfHour": 10, "hour": 5}])

    def test_the_reasons_come_from_the_transaction_itself(self):
        facts = self.out["facts"]
        # 25 sat/vB, above the next block's 20, no RBF: sure, and every fact listed for the register's bar
        # Without the ancestors (an explorer without /v1/cpfp): probable, never sure
        self.assertEqual(facts[0], {"level": "likely", "reason": "parents_unknown", "reasons": [
            {"code": "fee_next", "rate": 25, "target": 20}, {"code": "parents_unknown"}, {"code": "no_rbf"}]})
        self.assertEqual(facts[1]["level"], "sure")
        self.assertEqual(facts[1]["reasons"], [{"code": "fee_next", "rate": 25, "target": 20}, {"code": "no_parent"},
                                               {"code": "no_rbf"}])
        # An unconfirmed parent: at most probable, whatever the fee
        self.assertEqual((facts[2]["level"], facts[2]["reason"]), ("likely", "parent"))
        self.assertIn({"code": "parent", "count": 1}, facts[2]["reasons"])
        # A low fee lifted by its package (CPFP): the effective rate counts, 7 between the hour's and the half hour's
        self.assertEqual((facts[3]["level"], facts[3]["reasons"][0]), ("likely", {"code": "fee_hour", "rate": 7, "target": 10}))
        self.assertEqual((facts[4]["level"], facts[4]["reasons"][-1]), ("likely", {"code": "rbf"}))
        # Confirmed: with the confirmations when the block's height and the tip are known
        self.assertEqual(facts[5], {"level": "sure", "reason": "confirmed", "reasons": [{"code": "confirmations", "count": 3}]})
        self.assertEqual(facts[6]["reasons"], [{"code": "confirmed"}])
        self.assertEqual(self.out["ancestry"], [{"count": 0, "rate": None}, {"count": 1, "rate": None}, None, 900000, None,
                                                "https://m.example/api/v1/cpfp/" + "c" * 64,
                                                "https://m.example/api/blocks/tip/height"])
        self.assertEqual(self.out["feesUrl"], "https://mempool.space/api/v1/fees/recommended")

    def test_the_pairing_code_comes_back_whole(self):
        decoded = self.out["decoded"]
        self.assertEqual(decoded["register"], REGISTER)
        self.assertEqual(decoded["onchain"], {"key": self.out["staffKey"], "path": "0"})
        self.assertEqual((decoded["discount"], decoded["confirmAbove"], decoded["currency"]), (10, 5000, "EUR"))
        self.assertEqual(decoded["shop"], {"name": "Bar Aurora «by Anna»", "colour": "#224455", "town": "Example"})
        self.assertEqual(self.out["fixed"]["onchain"], {"address": self.address})
        self.assertIsNone(self.out["lightningOnly"]["onchain"])
        # Small enough for a QR code a phone reads from another phone's screen
        self.assertLess(len(self.out["code"]), 700)

    def test_version_2_carries_the_nostr_copies(self):
        prefix, nostr, none, old = self.out["two"]
        self.assertEqual((prefix, old), ("2.", "1."))
        self.assertEqual(nostr, {"owner": "ab" * 32, "secret": "cd" * 32, "relays": ["wss://nos.lol"]})
        self.assertIsNone(none)
        self.assertEqual(self.out["twoReasons"], ["format"] * 9)
        self.assertEqual(self.out["dropped"], {"owner": "ab" * 32, "secret": "cd" * 32, "relays": []})

    def test_a_pairing_code_out_of_bounds_is_refused_whole(self):
        reasons = self.out["pairingReasons"]
        self.assertEqual(reasons[1], "version")
        self.assertEqual(reasons[-6], "version")
        self.assertEqual([reason for i, reason in enumerate(reasons) if i not in (1, len(reasons) - 6)],
                         ["format"] * (len(reasons) - 2))
        self.assertEqual(self.out["encodeBad"], "format")

    def test_the_shops_chain_in_the_pairing(self):
        staff = self.out["staffKey"]
        self.assertEqual(self.out["shared"], [{"key": staff, "path": "0"}, {"key": staff, "path": "0/5"},
                                              {"key": staff, "path": "1/2/3/4"}])
        self.assertEqual(self.out["badPaths"], ["format"] * 10)
        self.assertEqual(self.out["steps"], [[0], [2147483647, 0], None, None])


# BIP173 and BIP350 test vectors: (address, network, what checkAddress says). Valid segwit v0 and taproot are accepted; the
# other valid ones (v2 to v16, a v1 program that is not 32 bytes) are well written but not payable here, «version»;
# the invalid ones are refused as «format» or «checksum», never read as an address
BECH32_VECTORS = [
    ("BC1QW508D6QEJXTDG4Y5R3ZARVARY0C5XW7KV8F3T4", "mainnet", True, None),
    ("bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4", "mainnet", True, None),
    ("tb1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3q0sl5k7", "signet", True, None),
    ("bc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vqzk5jj0", "mainnet", True, None),
    ("bc1pw508d6qejxtdg4y5r3zarvary0c5xw7kw508d6qejxtdg4y5r3zarvary0c5xw7kt5nd6y", "mainnet", False, "version"),
    ("BC1SW50QGDZ25J", "mainnet", False, "version"),
    ("bc1zw508d6qejxtdg4y5r3zarvaryvaxxpcs", "mainnet", False, "version"),
    ("tb1qqqqqp399et2xygdj5xreqhjjvcmzhxw4aywxecjdzew6hylgvsesrxh6hy", "signet", True, None),
    # Of the other network
    ("bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4", "signet", False, "network"),
    ("tb1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3q0sl5k7", "mainnet", False, "network"),
    # Invalid: another human part, wrong checksum, version 17, programs too short or too long, version 0 of 16 bytes,
    # mixed case, bad padding, empty data
    ("tc1qw508d6qejxtdg4y5r3zarvary0c5xw7kg3g4ty", "mainnet", False, "format"),
    ("bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t5", "mainnet", False, "format"),
    ("BC13W508D6QEJXTDG4Y5R3ZARVARY0C5XW7KN40WF2", "mainnet", False, "format"),
    ("bc1rw5uspcuh", "mainnet", False, "format"),
    ("bc10w508d6qejxtdg4y5r3zarvary0c5xw7kw508d6qejxtdg4y5r3zarvary0c5xw7kw5rljs90", "mainnet", False, "format"),
    ("BC1QR508D6QEJXTDG4Y5R3ZARVARYV98GJ9P", "mainnet", False, "format"),
    ("tb1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3q0sL5k7", "signet", False, "format"),
    ("bc1zw508d6qejxtdg4y5r3zarvaryvqyzf3du", "mainnet", False, "format"),
    ("tb1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3pjxtptv", "signet", False, "format"),
    ("bc1gmk9yu", "mainnet", False, "format"),
    # BIP350: the checksum constant follows the version (bech32 for 0, bech32m for the rest)
    ("bc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vqh2y7hd", "mainnet", False, "format"),
    # The BIP173 version 1 vector, in bech32: no longer an address
    ("bc1pw508d6qejxtdg4y5r3zarvary0c5xw7kw508d6qejxtdg4y5r3zarvary0c5xw7k7grplx", "mainnet", False, "format"),
    ("tb1z0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vqglt7rf", "signet", False, "format"),
    ("BC1S0XLXVLHEMJA6C4DQV22UAPCTQUPFHLXM9H8Z3K2E72Q4K9HCZ7VQ54WELL", "mainnet", False, "format"),
    ("bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kemeawh", "mainnet", False, "format"),
    ("tb1q0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vq24jc47", "signet", False, "format"),
    ("bc1p38j9r5y49hruaue7wxjce0updqjuyyx0kh56v8s25huc6995vvpql3jow4", "mainnet", False, "format"),
    ("BC130XLXVLHEMJA6C4DQV22UAPCTQUPFHLXM9H8Z3K2E72Q4K9HCZ7VQ7ZWS8R", "mainnet", False, "format"),
    ("bc1pw5dgrnzv", "mainnet", False, "format"),
    ("bc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7v8n0nx0muaewav253zgeav", "mainnet", False, "format"),
]


class Bech32Strict(SimpleTestCase):
    def test_bip173_and_bip350_vectors(self):
        node = os.environ.get("NODE") or shutil.which("node")
        if not node:
            self.skipTest("node is missing")
        script = ("require(process.argv[1]);const d=globalThis.registerDerive;const v=JSON.parse(process.argv[2]);"
                  "(async()=>console.log(JSON.stringify(await Promise.all(v.map(async([a,n])=>{const r=await d.checkAddress(a,n);"
                  "return [r.ok,r.reason||null]})))))()")
        result = subprocess.run([node, "-e", script, str(STATIC / "register-derive.js"),
                                 json.dumps([[a, n] for a, n, _ok, _why in BECH32_VECTORS])],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        for (address, network, ok, why), (got_ok, got_why) in zip(BECH32_VECTORS, json.loads(result.stdout), strict=True):
            with self.subTest(address=address, network=network):
                self.assertEqual((got_ok, got_why), (ok, why))

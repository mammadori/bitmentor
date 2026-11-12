"""The register's copies on Nostr (static/register-nostr.js): NIP-44 version 2, BIP340 signatures, NIP-19 keys and the
events of a copy.

NIP-44 is checked against the official test vectors (testdata/nip44.vectors.json, from github.com/paulmillr/nip44;
its sha256 is the one NIP-44 publishes) and the signatures against BIP340's (testdata/bip340-vectors.csv, from
github.com/bitcoin/bips): a copy encrypted wrong is a copy nobody opens. The scripts run in node (NODE=/path/to/node, or
node on the PATH); without node only the files are checked."""
import hashlib
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
SCRIPTS = [STATIC / "register-derive.js", STATIC / "register-nostr.js"]
TESTDATA = Path(__file__).parent / "testdata"
NIP44 = TESTDATA / "nip44.vectors.json"
BIP340 = TESTDATA / "bip340-vectors.csv"
# The checksum NIP-44 publishes for its vectors
NIP44_SHA256 = "269ed0f69e4c192512cc779e78c555090cebc7c785b609e338a62afc3ce25040"
# NIP-19's examples
NPUB = "npub10elfcs4fr0l0r8af98jlmgdh9c8tcxjvz9qkw038js35mp4dma8qzvjptg"
NPUB_HEX = "7e7e9c42a91bfef19fa929e5fda1b72e0ebc1a4c1141673e2794234d86addf4e"
NSEC = "nsec1vl029mgpspedva04g90vltkh6fvh240zqtv9k0t9af8935ke9laqsnlfe5"
NSEC_HEX = "67dea2ed018072d675f5415ecfaed7d2597555e202d85b3d65ea4e58d2d92ffa"

HARNESS = r"""
const fs = require("fs");
const crypto = require("crypto");
for (const file of process.argv.slice(2, 4)) eval(fs.readFileSync(file, "utf8"));
const n = globalThis.registerNostr;
const v = JSON.parse(fs.readFileSync(process.argv[4], "utf8")).v2;
const H = n.fromHex;
const sha = (text) => crypto.createHash("sha256").update(text).digest("hex");
const reason = async (work) => {
  try { await work(); return "ok"; } catch (error) { return error.reason || String(error); }
};
(async () => {
  const out = { wrong: [] };
  const wrong = (what) => out.wrong.push(what);
  for (const t of v.valid.get_conversation_key) {
    if (n.hex(await n.conversationKey(t.sec1, t.pub2)) !== t.conversation_key) wrong(`conversation key ${t.pub2}`);
  }
  const keys = v.valid.get_message_keys;
  for (const k of keys.keys) {
    const found = await n.messageKeys(H(keys.conversation_key), H(k.nonce));
    if (n.hex(found.chachaKey) !== k.chacha_key || n.hex(found.chachaNonce) !== k.chacha_nonce
        || n.hex(found.hmacKey) !== k.hmac_key) wrong(`message keys ${k.nonce}`);
  }
  for (const [length, padded] of v.valid.calc_padded_len) if (n.paddedLength(length) !== padded) wrong(`padding ${length}`);
  for (const t of v.valid.encrypt_decrypt) {
    const conversation = await n.conversationKey(t.sec1, n.publicKey(t.sec2));
    const back = await n.conversationKey(t.sec2, n.publicKey(t.sec1));
    if (n.hex(conversation) !== t.conversation_key || n.hex(back) !== t.conversation_key) wrong(`pair ${t.sec1}`);
    if (await n.encrypt(t.plaintext, conversation, H(t.nonce)) !== t.payload) wrong(`encrypt ${t.payload}`);
    if (await n.decrypt(t.payload, conversation) !== t.plaintext) wrong(`decrypt ${t.payload}`);
  }
  for (const t of v.valid.encrypt_decrypt_long_msg) {
    const plaintext = t.pattern.repeat(t.repeat);
    if (sha(plaintext) !== t.plaintext_sha256) wrong("long plaintext");
    const payload = await n.encrypt(plaintext, H(t.conversation_key), H(t.nonce));
    if (sha(payload) !== t.payload_sha256) wrong(`long ${t.repeat}`);
    if (await n.decrypt(payload, H(t.conversation_key)) !== plaintext) wrong(`long back ${t.repeat}`);
  }
  out.invalidKeys = [];
  for (const t of v.invalid.get_conversation_key) out.invalidKeys.push(await reason(() => n.conversationKey(t.sec1, t.pub2)));
  out.invalidPayloads = [];
  for (const t of v.invalid.decrypt) out.invalidPayloads.push(await reason(() => n.decrypt(t.payload, H(t.conversation_key))));
  out.invalidLengths = [];
  for (const length of v.invalid.encrypt_msg_lengths) {
    out.invalidLengths.push(await reason(() => n.encrypt("a".repeat(length), H("01".repeat(32)))));
  }
  out.counts = { keys: v.valid.get_conversation_key.length, pairs: v.valid.encrypt_decrypt.length,
    long: v.valid.encrypt_decrypt_long_msg.length, invalidKeys: v.invalid.get_conversation_key.length,
    invalidPayloads: v.invalid.decrypt.length };
  // RFC 8439, 2.4.2: the sunscreen
  const sunscreen = "Ladies and Gentlemen of the class of '99: If I could offer you only one tip for the future, sunscreen would be it.";
  out.chacha = n.hex(n.chacha20(H("000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"),
    H("000000000000004a00000000"), new TextEncoder().encode(sunscreen), 1));
  // BIP340
  out.bip340 = [];
  const rows = fs.readFileSync(process.argv[5], "utf8").trim().split(/\r?\n/).slice(1).map((row) => row.split(","));
  for (const [index, secret, publicKey, aux, message, signature, result] of rows) {
    const low = (value) => value.toLowerCase();
    let signed = "";
    if (secret) {
      signed = n.hex(await n.sign(low(secret), H(low(message)), H(low(aux)))) === low(signature)
        && n.publicKey(low(secret)) === low(publicKey);
    }
    const verified = await n.verify(H(low(publicKey)), H(low(message)), H(low(signature)));
    out.bip340.push([index, signed, verified === (result === "TRUE")]);
  }
  // NIP-19
  out.npub = [n.readPublic(process.argv[6]), n.npub(n.readPublic(process.argv[6])), n.readSecret(process.argv[7]),
    await reason(() => n.readPublic(process.argv[6].slice(0, -1) + "q")), await reason(() => n.readPublic(process.argv[7])),
    await reason(() => n.readSecret("00".repeat(32)))];
  // A copy: the register's key signs, the owner's opens
  const owner = n.newSecret();
  const till = n.newSecret();
  const ownerPublic = n.publicKey(owner);
  const sales = Array.from({ length: 3 }, (_, i) => ({ id: `s${i}`, at: 1762700000000 + i, cents: 350, status: "paid",
    sats: 3500, till: "reg-1" }));
  const [event] = await n.copyEvents({ sales, till: "reg-1", who: "Anna", period: "2026-11", owner: ownerPublic,
    secret: till, createdAt: 1762700000 });
  out.event = { kind: event.kind, tags: event.tags, author: event.pubkey === n.publicKey(till),
    checked: await n.checkEvent(event) };
  out.opened = await n.openCopy(event, owner);
  out.openedByTill = (await n.openCopy(event, till, ownerPublic)).sales.length;
  out.copyReasons = [
    await reason(() => n.openCopy({ ...event, content: event.content.replace(/^.{10}/, "AAAAAAAAAA") }, owner)),
    await reason(() => n.openCopy(event, n.newSecret())),
    await reason(() => n.openCopy({ ...event, tags: [["d", "bitmentor-register:reg-2:2026-11"], ["p", ownerPublic]] }, owner)),
  ];
  // A copy whose register or period says otherwise than its d tag, signed properly: refused
  const lying = await n.signEvent({ created_at: 1, kind: n.KIND, tags: [["d", "bitmentor-register:reg-2:2026-11"]],
    content: await n.encrypt(await n.pack({ v: 1, till: "reg-1", period: "2026-11", sales: [] }),
      await n.conversationKey(till, ownerPublic)) }, till);
  out.copyReasons.push(await reason(() => n.openCopy(lying, owner)));
  const newer = await n.signEvent({ created_at: 1, kind: n.KIND, tags: [["d", "bitmentor-register:reg-2:2026-11"]],
    content: await n.encrypt("2z.abc", await n.conversationKey(till, ownerPublic)) }, till);
  out.copyReasons.push(await reason(() => n.openCopy(newer, owner)));
  // A month too big for one event: parts of the same d tag family, each under NIP-44's limit, all opening
  const many = Array.from({ length: 4000 }, (_, i) => ({ id: crypto.randomUUID(), at: 1762700000000 + i * 1000,
    cents: 100 + i, status: "paid", sats: 1000 + i * 7, label: `€ ${i}`, rate: 81234.5, how: "agreed",
    hash: crypto.randomBytes(32).toString("hex"), sources: [{ source: "kraken", price: 81200 + i, at: 1762700000000 + i }] }));
  const parts = await n.copyEvents({ sales: many, till: "reg-1", period: "2026-11", owner: ownerPublic, secret: till,
    createdAt: 1762700000 });
  out.parts = parts.map((part) => part.tags[0][1]);
  out.partsLength = Math.max(...parts.map((part) => part.content.length));
  let back = 0;
  for (const part of parts) back += (await n.openCopy(part, owner)).sales.length;
  out.partsBack = back;
  out.d = [n.readD("bitmentor-register:reg-1:2026-11"), n.readD("bitmentor-register:reg-1:2026-11-09:3"),
    n.readD("bitmentor-register:reg 1:2026-11"), n.readD("other:reg-1:2026-11"), n.readD("bitmentor-register:reg-1:11")];
  out.filter = n.filter(ownerPublic, 5);
  // A small event that would open into more than the limit is refused
  const bomb = Buffer.from(require("zlib").gzipSync(Buffer.alloc(5 * 1024 * 1024, 32))).toString("base64");
  out.bomb = await reason(() => n.unpack(`1z.${bomb}`));
  process.stdout.write(JSON.stringify(out));
})().catch((error) => { console.error(error); process.exit(1); });
"""


class Files(SimpleTestCase):
    def test_the_vectors_are_the_published_ones(self):
        self.assertEqual(hashlib.sha256(NIP44.read_bytes()).hexdigest(), NIP44_SHA256)
        self.assertTrue(BIP340.read_text().startswith("index,secret key,public key,aux_rand,message,signature"))

    def test_the_copies_talk_to_nobody(self):
        assert_talks_to_nobody(self, "register-nostr.js", also=("localStorage",))


class Running(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        node = os.environ.get("NODE") or shutil.which("node")
        if not node:
            raise __import__("unittest").SkipTest("node is missing: NODE=/path/to/node to run the Nostr copies")
        with tempfile.TemporaryDirectory() as folder:
            harness = Path(folder) / "harness.js"
            harness.write_text(HARNESS)
            result = subprocess.run([node, str(harness), *map(str, SCRIPTS), str(NIP44), str(BIP340), NPUB, NSEC],
                                    capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise AssertionError(result.stderr)
        cls.out = json.loads(result.stdout)

    def test_nip44_matches_every_valid_vector(self):
        self.assertEqual(self.out["wrong"], [])
        self.assertEqual(self.out["counts"], {"keys": 35, "pairs": 10, "long": 3, "invalidKeys": 8, "invalidPayloads": 12})

    def test_nip44_refuses_every_invalid_vector(self):
        self.assertEqual(self.out["invalidKeys"], ["key"] * 8)
        self.assertNotIn("ok", self.out["invalidPayloads"])
        self.assertEqual(self.out["invalidLengths"], ["length"] * 4)

    def test_chacha20_matches_rfc_8439(self):
        self.assertTrue(self.out["chacha"].startswith("6e2e359a2568f98041ba0728dd0d6981e97e7aec1d4360c20a27afccfd9fae0b"))
        self.assertTrue(self.out["chacha"].endswith("5af90bbf74a35be6b40b8eedf2785e42874d"))

    def test_signatures_match_bip340(self):
        self.assertEqual(len(self.out["bip340"]), 19)
        for index, signed, verified in self.out["bip340"]:
            self.assertIn(signed, ("", True), index)
            self.assertTrue(verified, index)

    def test_npub_and_nsec(self):
        self.assertEqual(self.out["npub"], [NPUB_HEX, NPUB, NSEC_HEX, "key", "key", "key"])

    def test_a_copy_opens_with_the_owners_key_or_the_registers(self):
        self.assertEqual(self.out["event"]["kind"], 30078)
        self.assertEqual(self.out["event"]["tags"][0], ["d", "bitmentor-register:reg-1:2026-11"])
        self.assertEqual(self.out["event"]["tags"][1][0], "p")
        self.assertTrue(self.out["event"]["author"])
        self.assertTrue(self.out["event"]["checked"])
        opened = self.out["opened"]
        self.assertEqual((opened["till"], opened["who"], opened["period"], opened["part"]), ("reg-1", "Anna", "2026-11", 0))
        self.assertEqual([sale["id"] for sale in opened["sales"]], ["s0", "s1", "s2"])
        self.assertEqual(self.out["openedByTill"], 3)

    def test_a_copy_changed_or_for_someone_else_does_not_open(self):
        self.assertEqual(self.out["copyReasons"], ["signature", "decrypt", "signature", "format", "version"])

    def test_a_big_month_is_split_in_parts_under_the_limit(self):
        parts = self.out["parts"]
        self.assertGreater(len(parts), 1)
        self.assertEqual(parts[0], "bitmentor-register:reg-1:2026-11")
        self.assertEqual(parts[1:], [f"bitmentor-register:reg-1:2026-11:{i}" for i in range(1, len(parts))])
        self.assertLessEqual(self.out["partsLength"], 87472)
        self.assertEqual(self.out["partsBack"], 4000)

    def test_d_tags_and_filter(self):
        self.assertEqual(self.out["d"][:2], [{"till": "reg-1", "period": "2026-11", "part": 0},
                                             {"till": "reg-1", "period": "2026-11-09", "part": 3}])
        self.assertEqual(self.out["d"][2:], [None, None, None])
        self.assertEqual(self.out["filter"]["kinds"], [30078])
        self.assertEqual(self.out["filter"]["since"], 5)

    def test_a_copy_cannot_open_into_more_than_the_limit(self):
        self.assertEqual(self.out["bomb"], "size")

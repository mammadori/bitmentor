"""The register (static/register.js) in node, for whoever uses it with a screen reader: where the focus goes on each
screen, what the status line says and how often, the QR code's text alternative, the copy of the invoice, and the field
a failed form points to. The page is the stand-in of test_register_flow.py, which here also keeps attributes and focus."""
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from .test_register_flow import ADDRESS, FILES, HARNESS, STATIC, invoice, texts

SETUP, _ = HARNESS.split("for (const file of files) eval")
for old, new in (
        ("setAttribute() {}, removeAttribute() {}, focus() {},",
         "attributes: {}, setAttribute(k, v) { this.attributes[k] = String(v); }, removeAttribute(k) { delete this.attributes[k]; },"
         " focus() { document.activeElement = this; },"),
        ("querySelectorAll() { return []; },",
         'querySelectorAll(s) { return s === "[aria-invalid]" ? Object.values(elements).filter((e) => "aria-invalid" in e.attributes) : []; },')):
    assert SETUP.count(old) == 1, old
    SETUP = SETUP.replace(old, new)

SCENARIO = SETUP + r"""
// The status line counts its writes: a poll that finds nothing new writes nothing
let writes = 0;
let said = "";
Object.defineProperty(element("state"), "textContent", { get: () => said, set: (v) => { said = v; writes++; } });
element("q:#pay .waiting").textContent = " Waiting for payment ";
element("qr").alt = "Code for the customer to scan";
element("copy-invoice").textContent = "Copy the text";
element("copy-invoice").dataset.copied = "Copied";
const clipboard = [];
navigator.clipboard = { writeText: async (value) => { clipboard.push(value); } };
for (const file of files) eval(fs.readFileSync(file, "utf8"));
const wait = (ms) => new Promise((resolve) => realTimeout(resolve, ms));
const key = (k) => listeners["document:keydown"].forEach((fn) => fn({ key: k, target: {}, preventDefault() {} }));
const fire = async (name, event = {}) => { for (const fn of listeners[name]) await fn({ preventDefault() {}, ...event }); };
(async () => {
  await wait(300);
  const out = { copyHidden: element("copy-invoice").hidden };
  for (const k of "1000") key(k);
  key("Enter");
  await wait(300);
  const before = writes;
  await wait(300);
  Object.assign(out, { payFocus: document.activeElement?.id, alt: element("qr").alt, state: said, error: element("error").textContent,
    quietPolls: writes === before, payHidden: element("pay").hidden });
  await fire("copy-invoice:click");
  Object.assign(out, { clipboard, copied: said, copyLabel: element("copy-invoice").textContent });
  // Paid: the result screen, its button focused, the result said once
  ln.verify[0] = given.paid;
  await wait(300);
  Object.assign(out, { doneFocus: document.activeElement?.id, doneState: said, result: element("result").textContent });
  await fire("again:click");
  Object.assign(out, { keypadFocus: document.activeElement?.id, copyLabelAgain: element("copy-invoice").textContent });
  // The history: a passphrase too short points to its field; the next try starts clean
  element("export-pass").value = "short";
  await fire("export-form:submit", { target: { reset() {} } });
  out.short = { ...element("export-pass").attributes, focus: document.activeElement?.id, say: element("history-say").textContent };
  element("export-pass").value = "a long passphrase";
  element("export-again").value = "another one";
  await fire("export-form:submit", { target: { reset() {} } });
  out.mismatch = { pass: { ...element("export-pass").attributes }, again: { ...element("export-again").attributes },
    focus: document.activeElement?.id };
  // The settings: a currency that is not one
  element("currency").value = "EU";
  await fire("settings-form:submit", { target: {} });
  out.currency = { ...element("currency").attributes, focus: document.activeElement?.id, say: element("settings-say").textContent };
  process.stdout.write(JSON.stringify(out), () => process.exit(0));
})().catch((error) => { console.error(error); process.exit(1); });
"""


class Announced(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.node = os.environ.get("NODE") or shutil.which("node")
        if not cls.node:
            raise __import__("unittest").SkipTest("node is missing: NODE=/path/to/node to run the register")

    def test_a_sale_said_and_focused_screen_by_screen(self):
        pr, _hash = invoice(10000)
        data = {"address": ADDRESS, "domain": "ln.example.org", "currency": "EUR", "sources": ["kraken", "coinbase"],
                "tolerance": 1.0, "max_age": 120, "network": "mainnet", "explorers": ["https://mempool.example.org/api"],
                "discount": 0, "confirm_above": 0, "shop": {"name": "Bar"}, "texts": texts(), "relays": [],
                "public_relays": []}
        given = {"data": data, "dataset": {"ledger": "shop-1", "shop": "shop-1"},
                 "storage": {"register-settings:shop-1": json.dumps({"lightning": ADDRESS})},
                 "ln": {"invoices": [pr], "verify": {"0": {"status": "OK", "settled": False, "pr": pr}}},
                 "paid": {"status": "OK", "settled": True, "preimage": "00" * 32, "pr": pr}}
        with tempfile.TemporaryDirectory() as folder:
            harness, path = Path(folder) / "a11y.js", Path(folder) / "given.json"
            harness.write_text(SCENARIO)
            path.write_text(json.dumps(given))
            result = subprocess.run([self.node, str(harness), str(path), *(str(STATIC / name) for name in FILES),
                                     str(STATIC / "register.js")], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        words = data["texts"]
        # Waiting for payment: the focus on the code, whose alternative says the network and the amount
        self.assertEqual((out["payHidden"], out["error"]), (False, ""))
        self.assertEqual(out["payFocus"], "qr")
        self.assertTrue(out["alt"].startswith("Code for the customer to scan: Lightning, "), out["alt"])
        self.assertIn("10,000 sat", out["alt"])
        # One sentence for the screen, and polls that find nothing new say nothing
        self.assertIn("10,000 sat.", out["state"])
        self.assertTrue(out["state"].endswith("Waiting for payment"), out["state"])
        self.assertTrue(out["quietPolls"])
        # The invoice copied as text, and said
        self.assertFalse(out["copyHidden"])
        self.assertEqual(out["clipboard"], [pr])
        self.assertEqual((out["copied"], out["copyLabel"]), ("Copied", "Copied"))
        # Paid: the next action has the focus, the result is said
        self.assertEqual(out["doneFocus"], "again")
        self.assertEqual(out["doneState"], out["result"])
        self.assertTrue(out["result"])
        # A new sale: back on the amount, and the copy button with its own name again at the next sale
        self.assertEqual(out["keypadFocus"], "amount")
        # The form's failures point to the field, which takes the focus and is described by the message
        self.assertEqual(out["short"], {"aria-invalid": "true", "aria-describedby": "history-say", "focus": "export-pass",
                                        "say": words["short"].replace("{count}", "10")})
        self.assertEqual(out["mismatch"], {"pass": {}, "again": {"aria-invalid": "true", "aria-describedby": "history-say"},
                                           "focus": "export-again"})
        # A settings field is described by its own error, next to it, and by its help line
        self.assertEqual(out["currency"], {"aria-invalid": "true", "aria-describedby": "currency-error currency-help", "focus": "currency",
                                           "say": words["bad_currency"]})

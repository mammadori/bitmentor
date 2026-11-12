"""What holds for every script in static/: each one parses, and the ones that must talk to nobody have no way to. The
behaviour of the scripts is tried in node by the other tests; these are the tripwires a change trips at once."""
import os
import shutil
import subprocess

from django.conf import settings
from django.test import SimpleTestCase

STATIC = settings.BASE_DIR / "static"
# Every way a page's script can send something away
TALKING = ("fetch(", "XMLHttpRequest", "sendBeacon", "WebSocket")


def node_or_skip(test):
    node = os.environ.get("NODE") or shutil.which("node")
    if not node:
        test.skipTest("node is missing: NODE=/path/to/node to run the scripts")
    return node


def assert_talks_to_nobody(test, name, also=()):
    """The script `name` of static/ has none of the ways to send anything (nor the words in `also`)."""
    text = (STATIC / name).read_text()
    for word in (*TALKING, *also):
        test.assertNotIn(word, text, f"{name}: {word}")


class Scripts(SimpleTestCase):
    def test_every_script_parses(self):
        node = node_or_skip(self)
        # Every script the site serves, the vendored ones too
        scripts = sorted(STATIC.rglob("*.js"))
        self.assertGreater(len(scripts), 20)
        for script in scripts:
            with self.subTest(script.name):
                result = subprocess.run([node, "--check", str(script)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_register_sends_nothing_but_what_it_must(self):
        """One fetch for other servers (ask), the register's own files read again for its copy on the phone, the shop's
        count of on-chain addresses (a chain's hash and an index: shops/chain_count.py) and the owner's own explorer
        address (shops/chain_count.py, owner_explorer): no sale. Two of them POST, the count and the explorer. A new
        one is a decision, not an accident."""
        script = (STATIC / "register.js").read_text()
        self.assertEqual(script.count("fetch("), 4)
        self.assertEqual(script.count('method: "POST"'), 2)
        self.assertNotIn("data.sync", script)
        for word in ("XMLHttpRequest", "sendBeacon"):
            self.assertNotIn(word, script)

    def test_the_link_button_goes_off_at_the_first_submit_and_back_on_when_the_page_comes_back(self):
        node = node_or_skip(self)
        harness = r"""
const fs = require("fs");
const on = {};
const button = { disabled: false };
const form = { addEventListener: (type, fn) => { on[`form:${type}`] = fn; }, querySelector: () => button };
globalThis.document = { querySelector: (selector) => (selector === "form[data-once]" ? form : null) };
globalThis.addEventListener = (type, fn) => { on[`window:${type}`] = fn; };
eval(fs.readFileSync(process.argv[1], "utf8"));
const states = [button.disabled];
on["form:submit"]();
states.push(button.disabled);
on["window:pageshow"]();
states.push(button.disabled);
process.stdout.write(JSON.stringify(states));
"""
        result = subprocess.run([node, "-e", harness, str(STATIC / "link_confirm.js")], capture_output=True, text=True,
                                timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "[false,true,false]")

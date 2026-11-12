"""Where the registers keep their encrypted copies on Nostr: `register.relays` and `register.public_relays` in
community.yml, the register's data and its CSP, which opens only those relays."""
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import checks
from django.test import SimpleTestCase, TestCase

from .models import Shop
from .register import relay_problems, relay_url, relays
from .test_explorer import with_register
from .test_register import register_data


class Relays(SimpleTestCase):
    def test_only_a_wss_address_of_a_domain_name(self):
        self.assertEqual(relay_url("wss://Relay.Example.org/"), "wss://relay.example.org")
        self.assertEqual(relay_url("wss://relay.example.org/nostr"), "wss://relay.example.org/nostr")
        for wrong in ("ws://relay.example.org", "https://relay.example.org", "wss://relay.example.org:444",
                      "wss://a@relay.example.org", "wss://relay.example.org?x=1", "wss://relay.example.org#x",
                      "wss://127.0.0.1", "wss://*.example.org", "wss://relay.example.org; script-src *",
                      "wss://relay.example.org/a b", None, 3, ["wss://relay.example.org"]):
            self.assertIsNone(relay_url(wrong), wrong)

    def test_a_path_alone_is_a_relay_of_the_site_itself(self):
        with self.settings(SITE_HOST="dev.example.org"):
            self.assertEqual(relay_url("/relay"), "wss://dev.example.org/relay")
            self.assertEqual(relay_url(" /relay/ "), "wss://dev.example.org/relay")
            for wrong in ("//evil.example.org/relay", "/relay?x=1", "/a b", "/relay#x"):
                self.assertIsNone(relay_url(wrong), wrong)
        with self.settings(SITE_HOST="localhost"):
            # a site without a domain name has no relay of its own
            self.assertIsNone(relay_url("/relay"))

    def test_the_community_relays_and_the_public_ones(self):
        self.assertEqual(relays(), {"relays": [], "public_relays": []})
        with with_register(relays="wss://ours.example.org", public_relays=["wss://nos.lol", "wss://ours.example.org/",
                                                                            "ws://bad.example.org"]):
            self.assertEqual(relays(), {"relays": ["wss://ours.example.org"], "public_relays": ["wss://nos.lol"]})
            self.assertEqual(relay_problems(), ["register.public_relays: 'ws://bad.example.org' is not a wss address of a "
                                                "relay (wss://host/path): it is not used"])
            self.assertEqual([problem.id for problem in checks.run_checks() if problem.id.startswith("shops.")],
                             ["shops.W007"])
        many = [f"wss://r{i}.example.org" for i in range(20)]
        with with_register(relays=many, public_relays=many):
            self.assertEqual(relays(), {"relays": many[:8], "public_relays": many[8:16]})


class RegisterPolicy(TestCase):
    def setUp(self):
        owner = get_user_model().objects.create(username="k")
        self.shop = Shop.objects.create(owner=owner, name="Bar", city="Example", lightning_address="till@ln.example.org")
        self.url = f"/en/shops/register/{self.shop.id}/"

    def test_the_csp_opens_only_the_relays_of_community_yml(self):
        with with_register(relays=["wss://ours.example.org/nostr", "wss://evil.example.org:444"],
                           public_relays=["wss://nos.lol"]):
            response = self.client.get(self.url)
        connect = response.headers["Content-Security-Policy"].split("connect-src ")[1].split(";")[0].split()
        self.assertIn("wss://ours.example.org", connect)
        self.assertIn("wss://nos.lol", connect)
        self.assertFalse([origin for origin in connect if "evil" in origin or origin in ("wss:", "*", "wss://*")])
        data = register_data(response.content.decode())
        self.assertEqual((data["relays"], data["public_relays"]), (["wss://ours.example.org/nostr"], ["wss://nos.lol"]))

    def test_the_nsec_field_is_in_no_form(self):
        with with_register(relays=["wss://ours.example.org"]):
            page = self.client.get(self.url).content.decode()
        field = page.split('id="nostr-secret"')[1].split(">")[0]
        self.assertIn('type="text"', field)
        self.assertIn('autocomplete="off"', field)
        self.assertNotIn("<form", page.split('id="nostr-pull"')[0].rsplit("</form>", 1)[-1])
        self.assertIn('<button type="button" id="nostr-pull-go"', page)

    def test_without_relays_the_csp_opens_none(self):
        response = self.client.get(self.url)
        self.assertNotIn("wss:", response.headers["Content-Security-Policy"])


RELAYS_HARNESS = r"""
const fs = require("fs");
eval(fs.readFileSync(process.argv[2], "utf8"));
// Relays in memory, each answering a REQ in its own way
// Each event's id is its content's (filled in when sent, as NIP-01 makes it); its name is in its tag
const event = (name, size = 10, sig = "good") => ({ pubkey: "a", created_at: 1, kind: 30078, tags: [["n", name]],
  content: "x".repeat(size), sig });
const contentId = async (e) => [...new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(
  JSON.stringify([0, e.pubkey, e.created_at, e.kind, e.tags, e.content]))))].map((b) => b.toString(16).padStart(2, "0")).join("");
const behaviours = {
  // Floods: more events than any relay may send, never EOSE
  flood: (sub, reply) => { for (let i = 0; i < 700; i++) reply(["EVENT", sub, event(`${this_url}-${i}`)]); },
  // A message too big, then a good event
  big: (sub, reply) => { reply(["EVENT", sub, event("too-big", 95 * 1024)]); reply(["EVENT", sub, event("small")]); reply(["EOSE", sub]); },
  // Fills its budget with messages for another subscription, then a good event
  budget: (sub, reply) => {
    for (let i = 0; i < 100; i++) reply(["EVENT", "other", event(`b${i}`, 89 * 1024)]);
    reply(["EVENT", sub, event("after-budget")]);
    reply(["EOSE", sub]);
  },
  fair: (sub, reply) => { for (let i = 0; i < 30; i++) reply(["EVENT", sub, event(`fair-${i}`)]); reply(["EOSE", sub]); },
  // Sends a genuine event's id and content with a false signature, and an event whose id is not its content's
  liar: (sub, reply) => {
    reply(["EVENT", sub, event("genuine", 10, "false")]);
    reply(["EVENT", sub, { ...event("wrong-id"), id: "f".repeat(64) }]);
    reply(["EOSE", sub]);
  },
  honest: (sub, reply) => { reply(["EVENT", sub, event("genuine")]); reply(["EOSE", sub]); },
};
let this_url = "";
// Once the limits are tried, the relays that fail: the register's waits of seconds become a few ms
let fast = false;
const realTimeout = setTimeout;
globalThis.setTimeout = (fn, ms) => realTimeout(fn, fast ? Math.min(ms, 10) : ms);
const nameOf = (url) => url.split("//")[1].split(".")[0].replace(/\d+$/, "");
class Socket {
  constructor(url) {
    this.url = url;
    const name = nameOf(url);
    // The browser refuses the address; the connection fails; the relay never opens
    if (name === "refused") throw new Error("refused");
    if (name === "broken") realTimeout(() => this.onerror?.(), 1);
    else if (name !== "asleep") realTimeout(() => this.onopen?.(), 1);
  }
  send(text) {
    const message = JSON.parse(text);
    const name = nameOf(this.url);
    // Accepts the first event, then hangs up; or answers nothing at all
    if (name === "hangup") {
      if (this.hung) return;
      this.hung = true;
      realTimeout(() => {
        if (message[0] === "EVENT") this.onmessage?.({ data: JSON.stringify(["OK", message[1].id, true, ""]) });
        if (message[0] === "REQ") this.onmessage?.({ data: JSON.stringify(["EVENT", message[1], this.gift]) });
        this.onclose?.();
      }, 1);
      return;
    }
    if (name === "mute") return;
    if (message[0] !== "REQ") return;
    const frames = [];
    this_url = this.url;
    behaviours[name](message[1], (value) => frames.push(JSON.stringify(value)));
    (async () => {
      for (const data of frames) {
        const value = JSON.parse(data);
        if (value[0] === "EVENT" && value[2].id === undefined) value[2].id = await contentId(value[2]);
        if (this.onmessage) this.onmessage({ data: JSON.stringify(value) });
        await null;
      }
    })();
  }
  close() {}
}
(async () => {
  const out = {};
  const ids = async (urls) => (await registerRelays.request(urls, {}, { Socket })).events.map((e) => e.tags[0][1]);
  out.flood = await ids(["wss://flood.example.org"]);
  out.big = await ids(["wss://big.example.org"]);
  out.budget = await ids(["wss://budget.example.org"]);
  out.fair = await ids(["wss://flood1.example.org", "wss://flood2.example.org", "wss://flood3.example.org",
    "wss://flood4.example.org", "wss://flood5.example.org", "wss://fair.example.org"]);
  out.liar = (await registerRelays.request(["wss://liar.example.org", "wss://honest.example.org"], {}, { Socket }))
    .events.map((e) => `${e.tags[0][1]}:${e.sig}`);
  fast = true;
  const failing = ["wss://refused.example.org", "wss://broken.example.org", "wss://asleep.example.org", "wss://mute.example.org"];
  out.failingRequest = await registerRelays.request(failing, {}, { Socket });
  const sent = [{ ...event("one"), id: "1" }, { ...event("two"), id: "2" }];
  out.failingPublish = await registerRelays.publish([...failing, "wss://hangup.example.org"], sent, { Socket });
  // A relay that hangs up after one event: what came is kept
  const gift = event("before-hangup");
  gift.id = await contentId(gift);
  Socket.prototype.gift = gift;
  out.hangup = (await registerRelays.request(["wss://hangup.example.org"], {}, { Socket })).events.map((e) => e.tags[0][1]);
  process.stdout.write(JSON.stringify(out), () => process.exit(0));
})().catch((error) => { console.error(error); process.exit(1); });
"""


class RelayLimits(SimpleTestCase):
    """A relay is someone else's server: what it may send for one request is bounded, and no relay crowds out the others."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        node = os.environ.get("NODE") or shutil.which("node")
        if not node:
            raise __import__("unittest").SkipTest("node is missing: NODE=/path/to/node to run the relays")
        with tempfile.TemporaryDirectory() as folder:
            harness = Path(folder) / "relays.js"
            harness.write_text(RELAYS_HARNESS)
            result = subprocess.run([node, str(harness), str(settings.BASE_DIR / "static" / "register-relays.js")],
                                    capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise AssertionError(result.stderr)
        cls.out = json.loads(result.stdout)

    def test_at_most_500_events_from_one_relay(self):
        self.assertEqual(len(self.out["flood"]), 500)

    def test_a_message_too_big_is_dropped(self):
        self.assertEqual(self.out["big"], ["small"])

    def test_a_relay_past_its_budget_is_no_longer_heard(self):
        self.assertEqual(self.out["budget"], [])

    def test_every_relay_gets_its_share(self):
        fair = self.out["fair"]
        self.assertEqual(len(fair), 2000)
        self.assertEqual(len([value for value in fair if value.startswith("fair-")]), 30)

    def test_relays_that_fail_to_open_or_never_answer_give_nothing_and_no_error(self):
        """Refused by the browser, a socket error, no opening before the time limit, no answer before the time limit:
        each is one relay failing. A relay that hangs up has given what it gave."""
        self.assertEqual(self.out["failingRequest"], {"events": [], "answered": 1})
        self.assertEqual(self.out["failingPublish"], {"wss://refused.example.org": 0, "wss://broken.example.org": 0,
                                                      "wss://asleep.example.org": 0, "wss://mute.example.org": 0,
                                                      "wss://hangup.example.org": 1})
        self.assertEqual(self.out["hangup"], ["before-hangup"])

    def test_a_false_signature_does_not_hide_the_genuine_event(self):
        # The liar answers first with the genuine event's id: the genuine one from the other relay is still there, for
        # the caller to check; an event whose id is not its content's is dropped
        self.assertEqual(sorted(self.out["liar"]), ["genuine:false", "genuine:good"])

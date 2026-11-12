"""The practice register (shops/practice.py): the real register's screens with nothing real behind them. No request to
any provider, no write to the database, no sale anywhere, and open to the shop without a wallet."""
import json
import os
import re
import shutil
import subprocess
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from config import features

from .models import Shop, StaffRegister
from .test_experimental import on
from .tests import reset_limits
from .test_scripts import assert_talks_to_nobody

WRITES = re.compile(r"^\s*(INSERT|UPDATE|DELETE)", re.I)


def practice_data(response):
    found = re.search(r'<script id="practice-data" type="application/json">(.*?)</script>', response.content.decode(), re.S)
    return json.loads(found.group(1))


class Practice(TestCase):
    def setUp(self):
        reset_limits()
        self.owner = get_user_model().objects.create(username="owner")
        self.shop = Shop.objects.create(owner=self.owner, name="Bar", lightning_address="till@ln.example.org")
        self.client.force_login(self.owner)
        self.url = f"/en/shops/practice/{self.shop.id}/"

    def open(self, url, client=None):
        """The page, with every outgoing request made to fail loudly, counting the writes."""
        with (patch("requests.Session.request", side_effect=AssertionError("network used")),
              CaptureQueriesContext(connection) as queries):
            response = (client or self.client).get(url)
        writes = [query["sql"] for query in queries.captured_queries if WRITES.match(query["sql"])]
        self.assertEqual(writes, [])
        return response

    def test_the_real_register_with_a_band_and_nothing_real(self):
        response = self.open(self.url)
        self.assertContains(response, 'class="practice-band"')
        self.assertContains(response, "No real money: nothing here is a payment.")
        page = response.content.decode()
        # The real screens and the real script, after the one that answers its calls
        for screen in ('id="keypad"', 'id="step"', 'id="rate"', 'id="pay"', 'id="done"'):
            self.assertIn(screen, page)
        # With the hashed names of a built image too (practice.1a2b3c.js)
        practice, register = (re.search(rf'/static/{name}(\.\w+)?\.js"', page) for name in ("practice", "register"))
        self.assertLess(practice.start(), register.start())
        self.assertNotIn('rel="manifest"', page)
        self.assertNotIn("data-worker", page)
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(response["X-Robots-Tag"], "noindex, nofollow")
        self.assertNotIn("Practise without money", page)

    def test_the_register_has_no_address_to_pay_into(self):
        found = re.search(r'<script id="register-data" type="application/json">(.*?)</script>',
                          self.open(self.url).content.decode(), re.S)
        data = json.loads(found.group(1))
        self.assertEqual((data["address"], data["domain"], data["sources"]), ("", "", []))
        self.assertEqual(data["texts"]["to"], "")

    def test_the_code_on_the_screen_is_a_sentence_no_wallet_can_pay(self):
        data = practice_data(self.open(self.url))
        self.assertTrue(data["qr"].startswith("data:image/svg+xml"))
        self.assertIn("PRACTICE ONLY", data["text"])
        for scheme in ("bitcoin:", "lightning:", "lnbc", "lntb", "lnurl"):
            self.assertNotIn(scheme, data["text"].lower())
        self.assertIn("{name}", data["texts"]["done"])

    def test_nothing_counts_as_a_sale(self):
        staff = StaffRegister.objects.create(shop=self.shop, name="Anna")
        self.open(self.url)
        self.open(f"/en/shops/register/p/{staff.id}/practice/?k={staff.key}", self.client_class())
        self.shop.refresh_from_db()
        self.assertIsNone(self.shop.first_sale)

    def test_the_last_step_leads_to_the_first_day(self):
        response = self.open(self.url)
        self.assertContains(response, f'href="/en/shops/#first-day-{self.shop.id}"')
        self.assertContains(response, "Go to the first day")
        self.assertContains(response, 'href="/en/shops/">Your shops</a>')

    def test_a_shop_without_a_wallet_practises_too_but_only_its_owner(self):
        shop = Shop.objects.create(owner=self.owner, name="New", awaiting_wallet=True)
        url = f"/en/shops/practice/{shop.id}/"
        response = self.open(url)
        self.assertContains(response, 'class="practice-band"')
        self.assertContains(response, f'href="/en/shops/connect/{shop.id}/"')
        self.assertEqual(self.client_class().get(url).status_code, 404)
        other = self.client_class()
        other.force_login(get_user_model().objects.create(username="other"))
        self.assertEqual(other.get(url).status_code, 404)

    def test_whoever_opens_the_register_by_its_link_can_practise(self):
        response = self.open(self.url, self.client_class())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'href="/en/shops/register/{self.shop.id}/">Open the real register</a>')
        self.assertNotContains(response, "first-day-")

    def test_the_real_register_links_to_the_practice_and_stays_the_same(self):
        response = self.client.get(f"/en/shops/register/{self.shop.id}/")
        self.assertContains(response, f'href="{self.url}">Practise without money</a>')
        self.assertContains(response, 'rel="manifest"')
        self.assertNotContains(response, "practising")
        self.assertNotContains(response, "practice-band")

    def test_the_panel_and_the_first_day_link_to_the_practice(self):
        waiting = Shop.objects.create(owner=self.owner, name="New", awaiting_wallet=True)
        panel = self.client.get("/en/shops/")
        self.assertContains(panel, f'href="{self.url}"', count=2)
        self.assertContains(panel, f'href="/en/shops/practice/{waiting.id}/"')
        self.assertContains(panel, "Practise first, without money")

    def test_a_historic_shop_practises_only_while_its_register_works(self):
        # Its register still takes payments: the practice stays, in the panel and on the page
        self.shop.closed_on = timezone.now().date()
        self.shop.save()
        self.assertContains(self.client.get("/en/shops/"), f'href="{self.url}"')
        self.assertEqual(self.open(self.url).status_code, 200)
        # Without a register a historic shop has nothing to learn: no link, and no page even for its owner
        old = Shop.objects.create(owner=self.owner, name="Old", closed_on=timezone.now().date())
        self.assertNotContains(self.client.get("/en/shops/"), f'href="/en/shops/practice/{old.id}/"')
        self.assertEqual(self.client.get(f"/en/shops/practice/{old.id}/").status_code, 404)

    def test_the_title_and_the_code_say_practice_and_there_is_no_receipt(self):
        response = self.open(self.url)
        self.assertContains(response, "<title>Practice: Bar</title>")
        self.assertContains(response, 'alt="Practice: code for the customer to scan, which no wallet can pay"')
        css = (settings.BASE_DIR / "static" / "practice.css").read_text()
        self.assertIn(".practising #print { display: none !important; }", css)
        self.assertRegex(css, r"@media print \{ \.practice-band \{[^}]*background: #fff; color: #000;")
        real = self.client.get(f"/en/shops/register/{self.shop.id}/")
        self.assertContains(real, "<title>Bar</title>")
        self.assertContains(real, 'alt="Code for the customer to scan"')

    @on(features.ONCHAIN)
    def test_the_pretend_customer_has_the_moves_of_the_exercises(self):
        page = self.open(self.url).content.decode()
        self.assertEqual(re.findall(r'data-move="(\w+)"', page), ["pay", "none", "down", "up", "phone", "confused"])
        self.assertEqual(re.findall(r'data-exercise="(\w+)"', page),
                         ["take", "offline", "expired", "phone", "onchain", "real"])
        # On-chain is one exercise, the remedy when the customer does not understand the code; its words are there
        self.assertIn("pays only on-chain", practice_data(self.open(self.url))["texts"]["confused"])


class StaffPractice(TestCase):
    def setUp(self):
        reset_limits()
        owner = get_user_model().objects.create(username="owner")
        self.shop = Shop.objects.create(owner=owner, name="Bar", lightning_address="till@ln.example.org")
        self.staff = StaffRegister.objects.create(shop=self.shop, name="Anna")
        self.url = f"/en/shops/register/p/{self.staff.id}/practice/"

    def test_with_its_key_the_staff_register_has_its_practice_with_its_words(self):
        response = self.client.get(f"{self.url}?k={self.staff.key}")
        self.assertContains(response, 'class="practice-band"')
        self.assertContains(response, 'class="staff-name">Anna<')
        self.assertContains(response, "Do not give money back yourself: tell the owner the time and the amount.")
        self.assertIn("Lightning wallet", practice_data(response)["texts"]["offline"])
        self.assertEqual(response["Referrer-Policy"], "same-origin")
        self.assertEqual(response["Cache-Control"], "no-store")
        real = f"/en/shops/register/p/{self.staff.id}/?k={self.staff.key}"
        self.assertContains(response, f'href="{real}">Back to the register</a>')
        self.assertContains(response, f'href="{real}">Open the real register</a>')

    def test_without_the_key_or_turned_off_there_is_none(self):
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertEqual(self.client.get(f"{self.url}?k={'0' * 43}").status_code, 404)
        self.staff.retired = timezone.now()
        self.staff.save()
        self.assertEqual(self.client.get(f"{self.url}?k={self.staff.key}").status_code, 404)

    def test_the_staff_register_links_to_its_own_practice(self):
        response = self.client.get(f"/en/shops/register/p/{self.staff.id}/?k={self.staff.key}")
        self.assertContains(response, f'href="{self.url}?k={self.staff.key}">Practise without money</a>')
        self.assertNotContains(response, f"/en/shops/practice/{self.shop.id}/")
        self.staff.retired = timezone.now()
        self.staff.save()
        response = self.client.get(f"/en/shops/register/p/{self.staff.id}/?k={self.staff.key}")
        self.assertNotContains(response, "Practise without money")


class PracticeScript(TestCase):
    """practice.js talks to nobody: it gives the register a pretend backend, and no address of another site is in it."""

    def test_the_script_answers_the_register_and_refuses_the_rest(self):
        assert_talks_to_nobody(self, "practice.js", also=("http://", "https://"))


# practice.js alone in node, with a page of stand-ins: the on-chain exercise
PRACTICE_HARNESS = r"""
const fs = require("fs");
globalThis.window = globalThis;
// The pretend provider's quarter of a second, made nothing
const realTimeout = setTimeout;
globalThis.setTimeout = (fn) => realTimeout(fn, 0);
// A memory spoiled by hand or by an old version (process.argv[4]): the practice starts afresh
const kept = new Map(process.argv[4] ? [["practice-state:practice:/p/", process.argv[4]]] : []);
// A page without the on-chain exercise (on-chain at the register, an experimental feature, is off), with the exercise
// of the phone already done: the Lightning ones are then all there is
const noChain = process.argv[3] === "no-chain";
if (noChain) kept.set("practice-done:practice:/p/", '["phone"]');
globalThis.localStorage = { getItem: (k) => (kept.has(k) ? kept.get(k) : null), setItem: (k, v) => kept.set(k, String(v)),
  removeItem: (k) => kept.delete(k) };
const elements = {};
const listeners = {};
const fake = (id) => ({ id, hidden: true, textContent: "", dataset: {}, classList: { toggle() {} }, focus() {},
  querySelector: () => fake(), addEventListener(type, fn) { (listeners[`${id}:${type}`] ||= []).push(fn); } });
const element = (id) => (elements[id] ||= fake(id));
element("practice-data").textContent = JSON.stringify({ qr: "data:practice", texts: { confused: "C", chain_ready: "R",
  pay: "P", first: "F", ready: "Y", done: "{name}", all: "A", offline: "O" } });
globalThis.document = { body: { dataset: { ledger: "practice:/p/" } }, getElementById: element,
  querySelector: (s) => (noChain && s.includes('"onchain"') ? null : element(`q:${s}`)), querySelectorAll: () => [],
  dispatchEvent() {}, activeElement: null };
globalThis.location = { pathname: "/p/", reload() {} };
eval(fs.readFileSync(process.argv[2], "utf8"));
const move = (name) => listeners["q:.moves:click"][0]({ target: { closest: () => ({ dataset: { move: name }, hidden: false }) } });
const backend = globalThis.registerBackend;
const failure = (promise) => promise.then(() => null, (error) => [error.message, error.shown]);
// The Lightning exercises: paid, left without paying, paid while the register is offline
async function lightning() {
  const out = {};
  const first = await backend.invoiceFor(3500000);
  out.unpaid = [await backend.settled(first), backend.expired(first)];
  move("pay");
  out.paid = await backend.settled(first);
  const second = await backend.invoiceFor(1000000);
  move("none");
  out.gone = [await backend.settled(second), backend.expired(second)];
  const third = await backend.invoiceFor(1000000);
  move("down");
  out.offline = [await failure(backend.invoiceFor(1000000)), await failure(backend.settled(third))];
  move("pay");
  move("up");
  out.back = await backend.settled(third);
  out.done = JSON.parse(kept.get("practice-done:practice:/p/"));
  out.said = element("customer-say").textContent;
  out.never = await backend.settled({ pr: "practice:not-made" });
  return out;
}
(async () => {
  if (process.argv[3] === "lightning" || noChain) {
    const first = await failure(backend.invoiceFor(1000));
    if (first) {
      process.stdout.write(JSON.stringify({ error: first }));
      return;
    }
    process.stdout.write(JSON.stringify(await lightning()));
    return;
  }
  const out = { address: backend.practiceChain, qr: backend.practiceQr, before: [backend.chainOffered(), element("chain-ok").hidden] };
  await backend.invoiceFor(3500000);
  move("confused");
  out.after = [backend.chainOffered(), element("chain-ok").hidden, element("customer-say").textContent];
  out.start = await backend.chainTxs("practice");
  out.unpaid = await backend.chainTxs("practice", { sats: 3500 });
  move("pay");
  const paid = await backend.chainTxs("practice", { sats: 3500 });
  out.paid = paid.map((tx) => [tx.status.confirmed, tx.vout[0].value, Math.abs(tx.status.block_time - Date.now() / 1000) < 60]);
  out.done = JSON.parse(kept.get("practice-done:practice:/p/"));
  process.stdout.write(JSON.stringify(out));
})().catch((error) => { console.error(error); process.exit(1); });
"""


class PracticeOnChain(TestCase):
    def practise(self, *mode):
        node = os.environ.get("NODE") or shutil.which("node")
        if not node:
            self.skipTest("node is missing")
        result = subprocess.run([node, "-e", PRACTICE_HARNESS, "-", str(settings.BASE_DIR / "static" / "practice.js"), *mode],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_the_lightning_exercises_end_as_the_pretend_customer_decides(self):
        out = self.practise("lightning")
        self.assertEqual((out["unpaid"], out["paid"]), ([False, False], True))
        # The customer who leaves: not paid, and the code is over at once
        self.assertEqual(out["gone"], [False, True])
        # The connection down: no invoice and no check, said to the merchant; paid meanwhile, it shows once back
        self.assertEqual(out["offline"], [["O", True], ["O", True]])
        self.assertTrue(out["back"])
        self.assertEqual(sorted(out["done"]), ["expired", "offline", "take"])
        self.assertFalse(out["never"])
        # The on-chain exercise is still to do: not all done
        self.assertFalse(out["said"].endswith(" A"))

    def test_without_the_on_chain_exercise_the_lightning_ones_are_all_there_is(self):
        """The page of a community that has not turned on-chain on (an experimental feature) lists no on-chain exercise:
        the practice is complete without it."""
        out = self.practise("no-chain")
        self.assertEqual(sorted(out["done"]), ["expired", "offline", "phone", "take"])
        self.assertTrue(out["said"].endswith(" A"), out["said"])

    def test_a_spoiled_memory_starts_the_practice_afresh(self):
        for spoiled in ("[1, 2]", '{"sales": [1]}', '"text"', "not json", '{"sales": {"x": 1}, "offline": true}'):
            with self.subTest(spoiled):
                out = self.practise("lightning", spoiled)
                if "offline" in spoiled:
                    # A sound memory is kept: the connection was down, and the first invoice says so
                    self.assertEqual(out, {"error": ["O", True]})
                    continue
                self.assertEqual((out["unpaid"], out["paid"]), ([False, False], True))

    def test_on_chain_comes_only_after_the_customer_does_not_understand_the_code(self):
        out = self.practise()
        # An address no wallet can pay, and the practice's code
        self.assertEqual((out["address"], out["qr"]), ("practice", "data:practice"))
        self.assertEqual(out["before"], [False, True])
        self.assertEqual(out["after"], [True, False, "C"])
        self.assertEqual((out["start"], out["unpaid"]), ([], []))
        # The customer pays: the amount asked, in a block of now (the register's time window)
        self.assertEqual(out["paid"], [[True, 3500, True]])
        self.assertIn("onchain", out["done"])

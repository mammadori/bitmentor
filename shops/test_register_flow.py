"""The whole register (static/register.js) in node, with a page made of stand-ins and the network answered here: an
on-chain sale of the owner's register from the key on the phone, and a staff register paired with the owner's code.
What it checks is where the money goes: the address of each sale is the one the owner's key gives for its place, the
discount is applied, and the explorer's answer ends the sale. The screens themselves are tried by hand in a browser."""
import base64
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

from .test_register_chain import BIP84_ACCOUNT, BIP84_RECEIVE, REGISTER

STATIC = settings.BASE_DIR / "static"
FILES = ["vendor/qrcode/qrcode.js", "register-book.js", "register-derive.js", "register-chain.js", "register-pairing.js",
         "register-nostr.js", "register-relays.js"]
RELAY = "wss://relay.example.org"
# The owner's Nostr private key in these tests
OWNER_SECRET = "11" * 32
ADDRESS = "till@ln.example.org"
SOURCES = ["kraken", "coinbase", "bitstamp"]

HARNESS = r"""
const fs = require("fs");
const [input, ...files] = process.argv.slice(2);
const given = JSON.parse(fs.readFileSync(input, "utf8"));
globalThis.self = globalThis;
globalThis.window = globalThis;
// A phone whose clock is off by this many ms
if (given.clockAhead) { const realNow = Date.now; Date.now = () => realNow() + given.clockAhead; }
// An insecure page: no WebCrypto
if (given.noSubtle) Object.defineProperty(globalThis, "crypto", { value: { getRandomValues: (a) => a.fill(7) }, configurable: true });
globalThis.Option = function Option(text, value) { return { text, value }; };
const realTimeout = setTimeout;
// Delays are shortened so the tests run fast, but not the relay limits of static/register-relays.js (8 s to open a
// socket, 12 s to wait for answers): the fake relay answers in ~1 ms of real time, and with those cut to 5 ms a busy
// machine could fire the limit first, so the register would see a relay that did not answer and drop its events.
const RELAY_LIMITS = [8000, 12000];
globalThis.setTimeout = (fn, ms) => realTimeout(fn, RELAY_LIMITS.includes(ms) ? ms : Math.min(ms, 5));
globalThis.setInterval = () => 0;
const elements = {};
// The files the register hands to the browser: their name and content
const downloads = [];
const blobs = new Map();
URL.createObjectURL = (blob) => { const href = `blob:${blobs.size}`; blobs.set(href, blob); return href; };
URL.revokeObjectURL = () => {};
const downloaded = async () => Promise.all(downloads.map(async ({ name, href }) => ({ name, text: await blobs.get(href).text() })));
const listeners = {};
function fake(id) {
  return { id, hidden: false, textContent: "", value: "", checked: false, src: "", dataset: {}, files: [], options: [],
    style: { setProperty() {}, removeProperty() {} }, classList: { toggle() {}, add() {}, remove() {} },
    setAttribute() {}, removeAttribute() {}, focus() {}, closest() { return fake(); }, matches() { return false; },
    querySelector() { return fake(); }, querySelectorAll() { return []; }, replaceChildren(...children) { this.children = children; },
    append() {}, remove() {},
    addEventListener(type, fn) { (listeners[`${id}:${type}`] ||= []).push(fn); (this.on ||= {})[type] = fn; } };
}
const element = (id) => (elements[id] ||= fake(id));
element("register-data").textContent = JSON.stringify(given.data);
const kept = new Map(Object.entries(given.storage));
globalThis.localStorage = { getItem: (k) => (kept.has(k) ? kept.get(k) : null), setItem: (k, v) => kept.set(k, String(v)),
  removeItem: (k) => kept.delete(k) };
globalThis.document = {
  body: { dataset: given.dataset, append() {} }, documentElement: { lang: "en", style: { setProperty() {}, removeProperty() {} } },
  getElementById: element, querySelector: (s) => element(`q:${s}`),
  createElement: (tag) => Object.assign(fake(), tag === "a" ? { click() { downloads.push({ name: this.download, href: this.href }); } } : {}),
  querySelectorAll: (s) => (given.copy && s.includes("script[src]") ? given.copy.scripts.map((src) => ({ src })) : []),
  addEventListener(type, fn) { (listeners[`document:${type}`] ||= []).push(fn); }, hidden: false, activeElement: null };
globalThis.getComputedStyle = () => ({ backgroundColor: "rgb(0, 0, 0)", color: "rgb(255, 255, 255)", getPropertyValue: () => "" });
globalThis.matchMedia = () => ({ addEventListener() {} });
// The browser's answer on keeping the site's data (given.data.persisted), and how many times it was asked to
let persistAsked = 0;
Object.defineProperty(globalThis, "navigator", { value: { storage: { persisted: async () => given.data.persisted ?? true,
  persist: async () => { persistAsked += 1; return true; } }, userActivation: {},
  ...(given.copy ? { serviceWorker: { register: async () => ({}) } } : {}) } });
// The copy for when the site does not answer: what the page puts in the cache, and what the site answers for each file
const cached = new Map(Object.entries(given.copy?.before || {}));
if (given.copy) {
  globalThis.caches = { open: async () => ({
    put: async (url, response) => cached.set(url, response.body),
    keys: async () => [...cached.keys()].map((url) => ({ url: `https://example.org${url}` })),
    delete: async (request) => cached.delete(new URL(request.url).pathname) }) };
}
const answered = (file) => ({ ok: file.status === 200, status: file.status, redirected: Boolean(file.redirected), body: file.body,
  headers: { has: (name) => name in file.headers, get: (name) => file.headers[name] ?? null }, text: async () => file.body });
globalThis.location = { pathname: "/register/", search: "", href: "https://example.org/register/", origin: "https://example.org" };
const ledgerKey = `register-ledger:${given.dataset.ledger}`;
function putInBook(entry) {
  const book = JSON.parse(kept.get(ledgerKey) || "[]").filter((item) => item.id !== entry.id);
  kept.set(ledgerKey, JSON.stringify([...book, entry]));
}
const confirmed = [];
globalThis.confirm = (question) => { confirmed.push(question); return given.confirm !== false; };
const asked = [];
// A lying explorer (given.data.liar): the first that many addresses asked about received something, the others nothing
const liedTo = new Map();
let chainAnswer = (address) => {
  if (given.data.liar === undefined) return (given.data.atStart || {})[address] || [];
  if (!liedTo.has(address)) liedTo.set(address, liedTo.size);
  return liedTo.get(address) < given.data.liar
    ? [{ txid: "e".repeat(64), status: { confirmed: true }, vout: [{ scriptpubkey_address: address, value: 5000 }] }] : [];
};
// The shop's Lightning provider (LNURL-pay and LUD-21 verify): the invoices it hands out in turn, the last one again and
// again, and what verify says of each
const ln = given.ln || {};
let served = 0;
function lnAnswer(url) {
  if (url.includes("/.well-known/lnurlp/")) {
    return { tag: "payRequest", callback: "https://ln.example.org/callback", minSendable: 1000, maxSendable: 1e11 };
  }
  if (url.includes("/callback?")) {
    const at = Math.min(served++, ln.invoices.length - 1);
    // A provider without LUD-21 (ln.noVerify): nobody tells the register that the money arrived
    return { status: "OK", pr: ln.invoices[at], ...(ln.noVerify ? {} : { verify: `https://ln.example.org/verify/${at}` }) };
  }
  const at = Number(url.split("/verify/")[1]);
  // Meanwhile something else changes that sale in the book (a copy brought in, a pull)
  if (ln.meanwhile && ln.meanwhile.at === at) putInBook(ln.meanwhile.entry);
  return (ln.verify || {})[at] ?? { status: "OK", settled: false, preimage: null, pr: ln.invoices[at] };
}
// The site's count of the shop's on-chain addresses (shops/chain_count.py): what the registers of the shop took before
// (given.data.shopCount), the questions it got, and a site that does not answer (given.data.claimDown)
let shopCount = given.data.shopCount ?? 0;
const claims = [];
globalThis.fetch = async (url, options = {}) => {
  if (given.data.chain_claim && url === given.data.chain_claim.url) {
    const body = JSON.parse(options.body);
    claims.push(body);
    if (given.data.claimDown) return { ok: false, status: given.data.claimDown, json: async () => ({}) };
    if (body.back !== undefined) {
      if (shopCount === body.back + 1) shopCount = body.back;
      return { ok: true, status: 200, json: async () => ({}) };
    }
    const index = Math.max(shopCount, body.at_least);
    shopCount = index + 1;
    return { ok: true, status: 200, json: async () => ({ index }) };
  }
  asked.push(url);
  if (given.copy && given.copy.files[url]) return answered(given.copy.files[url]);
  let body;
  // The price sources a test answers itself (given.data.rates): part of the URL, and the answer, or null for no answer
  const rate = Object.entries(given.data.rates || {}).find(([part]) => url.includes(part));
  if (rate && rate[1] === null) return { ok: false, status: 503, text: async () => "" };
  if (rate) body = rate[1];
  else if (url.includes("kraken")) body = { result: { XXBTZEUR: { c: ["100000.0"] } } };
  else if (url.includes("coinbase")) body = { data: { currency: "EUR", amount: "100000" } };
  else if (url.includes("/address/")) {
    // An explorer that does not answer at all
    if (given.data.explorerDown) return { ok: false, status: 503, text: async () => "" };
    // The shop's own explorer that does not answer (given.data.ownDown): a network error, or an answer that is not OK
    if (given.data.ownDown && given.data.own_explorer && url.startsWith(given.data.own_explorer)) {
      if (given.data.ownDown === "network") throw new TypeError("network");
      // A node still syncing, or without its backend: OK, and no transactions
      if (given.data.ownDown === "empty") return answered({ status: 200, body: "[]", headers: {} });
      return { ok: false, status: 502, text: async () => "" };
    }
    const address = decodeURIComponent(url.split("/address/")[1].split("/")[0]);
    // Another tab writes the count while this sale asks the explorer (given.data.chainMeanwhile: at which question)
    const meanwhile = given.data.chainMeanwhile;
    if (meanwhile && asked.filter((item) => item.includes("/address/")).length === meanwhile.at) {
      kept.set(meanwhile.key, meanwhile.value);
    }
    body = chainAnswer(address);
  }
  else if (url.startsWith("https://ln.example.org/") && ln.invoices) body = lnAnswer(url);
  else return { ok: false, status: 404, text: async () => "" };
  return { ok: true, status: 200, text: async () => JSON.stringify(body) };
};
// A relay in memory (NIP-01): replaceable events by author, kind and d; a REQ answers what is encrypted to the owner
const relay = given.relay || [];
const dOf = (event) => event.tags.find((tag) => tag[0] === "d")?.[1];
globalThis.WebSocket = class {
  constructor(url) { this.url = url; asked.push(url); realTimeout(() => this.onopen?.(), 1); }
  send(text) {
    const message = JSON.parse(text);
    const reply = (value) => realTimeout(() => this.onmessage?.({ data: JSON.stringify(value) }), 1);
    if (message[0] === "EVENT") {
      const event = message[1];
      const at = relay.findIndex((old) => old.pubkey === event.pubkey && old.kind === event.kind && dOf(old) === dOf(event));
      if (at >= 0) relay.splice(at, 1);
      relay.push(event);
      reply(["OK", event.id, true, ""]);
    } else if (message[0] === "REQ") {
      const wanted = message[2]["#p"];
      for (const event of relay.filter((item) => item.tags.some((tag) => tag[0] === "p" && wanted.includes(tag[1])))) {
        reply(["EVENT", message[1], event]);
      }
      reply(["EOSE", message[1]]);
    }
  }
  close() {}
};
for (const file of files) eval(fs.readFileSync(file, "utf8"));
// A sale recorded while the owner's phone opens the copies of a pull
if (given.duringPull) {
  const open = registerNostr.openCopy;
  registerNostr.openCopy = async (...args) => {
    if (given.duringPull) putInBook(given.duringPull);
    given.duringPull = null;
    return open(...args);
  };
}
const wait = (ms) => new Promise((resolve) => realTimeout(resolve, ms));
// Waits until `done()` holds, or `ms` at most: the register answers in a few ms, a busy machine may take longer
async function until(done, ms) {
  for (let waited = 0; waited < ms && !done(); waited += 10) await wait(10);
}
const key = (k) => listeners["document:keydown"].forEach((fn) => fn({ key: k, target: {}, preventDefault() {} }));
const ledger = () => JSON.parse(kept.get(`register-ledger:${given.dataset.ledger}`) || "[]");
async function lightning() {
  const out = { sales: [] };
  for (const typed of ln.typed) {
    key("Escape");
    for (const k of typed) key(k);
    key("Enter");
    await wait(400);
    out.sales.push({ error: element("error").textContent, ledger: ledger(), seen: !element("seen").hidden });
    listeners["again:click"].forEach((fn) => fn());
  }
  // Copies brought in from the relay by the register's own pull, then each checked with the provider: both take as long as
  // the machine needs (decryption, signatures), so wait for the checks to be asked rather than a fixed time
  if (given.relay && given.relay.length) {
    await until(() => ledger().length && asked.filter((url) => url.includes("/verify/")).length >= ledger().length, 8000);
  }
  await wait(400);
  out.ledger = ledger();
  out.verifyAsked = asked.filter((url) => url.includes("/verify/"));
  out.clock = kept.get("register-clock") || null;
  return out;
}
(async () => {
  await wait(300);
  // A test's own steps (given.data.script): the body of an async function that sees this harness, and returns the result
  if (given.data.script) {
    const steps = eval(`(async () => { ${given.data.script} })`);
    process.stdout.write(JSON.stringify(await steps()), () => process.exit(0));
    return;
  }
  if (given.copy) {
    process.stdout.write(JSON.stringify(Object.fromEntries(cached)));
    process.exit(0);
  }
  if (ln.typed) {
    process.stdout.write(JSON.stringify(await lightning()), () => process.exit(0));
    return;
  }
  // The sale on the screen when the page opened: what the register shows, then the explorer answers `reopen` instead
  if (given.reopen) {
    const out = { seenBefore: !element("seen").hidden, errorBefore: element("error").textContent };
    chainAnswer = (address) => given.reopen[address] || [];
    await wait(given.reopenWait || 200);
    Object.assign(out, { seenAfter: !element("seen").hidden, errorAfter: element("error").textContent, ledger: ledger() });
    process.stdout.write(JSON.stringify(out), () => process.exit(0));
    return;
  }
  const out = { blocked: element("blocked-text").textContent, chainHidden: element("chain-ok").hidden, error: element("error").textContent,
    gap: element("chain-gap").textContent, gapCount: element("chain-gap-count").textContent };
  for (const k of given.typed || "1000") key(k);
  out.discountText = element("discount").textContent;
  out.discountFixed = element("discount-fixed").hidden ? null : element("discount-fixed").textContent;
  const onChain = !element("chain-ok").hidden && (listeners["chain-ok:click"] || []).length > 0;
  listeners["chain-ok:click"]?.forEach((fn) => fn());
  // An on-chain sale derives addresses and asks the explorer: under load (the suite in parallel) it may take a few
  // seconds, so when the button was there, wait for an address or an error
  if (onChain) await until(() => element("to").textContent || element("error").textContent, 5600);
  else await wait(100);
  out.to = element("to").textContent;
  out.chainError = element("error").textContent;
  const sale = ledger().at(-1);
  out.sale = sale;
  if (sale) {
    chainAnswer = (address) => (address === sale.address ? [{ txid: "a".repeat(64), status: { confirmed: false },
      vout: [{ scriptpubkey_address: address, value: sale.sats }] }] : []);
    await until(() => element("chain-state").textContent, 300);
    out.seen = element("chain-state").textContent;
    out.why = (element("confidence-why").children || []).map((item) => item.textContent);
    out.bar = element("confidence").hidden ? null : [element("confidence-label").textContent, element("confidence").dataset.level];
    chainAnswer = (address) => (address === sale.address ? [{ txid: "a".repeat(64),
      status: { confirmed: true, block_time: Math.floor(Date.now() / 1000) },
      vout: [{ scriptpubkey_address: address, value: sale.sats }] }] : []);
    await until(() => ledger().find((entry) => entry.id === sale.id)?.status !== "pending", 300);
    out.final = ledger().find((entry) => entry.id === sale.id);
  }
  out.asked = asked.filter((url) => url.includes("/address/"));
  out.claims = claims;
  out.shopCount = shopCount;
  out.ledger = ledger();
  out.next = kept.get(`register-chain-next:${given.dataset.ledger}`);
  out.paired = kept.get(`register-pairing:${given.dataset.ledger}`) ? true : false;
  out.nostrState = element("nostr-state").textContent;
  out.tillkeys = kept.get(`register-nostr-tillkeys:${given.dataset.shop}`) || null;
  out.verifyAsked = asked.filter((url) => url.includes("/verify/"));
  // A staff register's copy imported at the end of a shift (the history screen's form)
  if (given.importSales) {
    const envelope = await registerBook.seal({ sales: given.importSales, till: given.importTill || "staff",
      closings: { "2026-11-01": 1 } }, "a passphrase ok", 100000);
    const content = JSON.stringify(envelope);
    element("import-file").files = [{ size: content.length, text: async () => content }];
    element("import-pass").value = "a passphrase ok";
    element("import-own").checked = Boolean(given.importOwn);
    for (const fn of listeners["import-form:submit"]) await fn({ preventDefault() {}, target: { reset() {} } });
    out.imported = element("history-say").textContent;
    out.afterImport = ledger();
    // The owner's register checks the staff registers' Lightning sales with the provider: it takes a moment
    await wait(400);
    out.afterVerify = ledger();
    out.verifyAsked = asked.filter((url) => url.includes("/verify/"));
    out.closings = kept.get(`register-closings:${given.dataset.ledger}`) || null;
  }
  // The owner gives the private key in the history screen: the copies encrypted to it come into the book
  if (given.pull) {
    element("nostr-secret").value = given.pull;
    // Pressed twice while the relays answer: the second press starts nothing
    const presses = [];
    for (let i = 0; i < (given.pullTwice ? 2 : 1); i++) {
      for (const fn of listeners["nostr-pull-go:click"] || []) presses.push(fn());
      if (given.pullTwice && !i) element("nostr-secret").value = given.pull;
    }
    await Promise.all(presses);
    out.relayAsked = asked.filter((url) => url.startsWith("wss:")).length;
    out.secretLeft = element("nostr-secret").value;
    out.pulled = element("history-say").textContent;
    out.after = ledger();
    out.confirmed = confirmed;
  }
  // The owner saves the settings with these fields
  if (given.settings) {
    // Opened first, the form shows what is saved
    if (given.openSettings) {
      for (const fn of listeners["settings-open:click"]) await fn();
      out.shownThreshold = element("confirm-above").value;
    }
    for (const [id, value] of Object.entries(given.settings)) element(id).value = value;
    element("sources").querySelectorAll = () => given.data.sources.map((value) => ({ value }));
    for (const fn of listeners["settings-form:submit"]) await fn({ preventDefault() {} });
    // The owner ticks the box after the check, and saves again
    if (given.settingsTick) {
      out.firstSay = element("settings-say").textContent;
      out.startCheck = element("chain-start-check").textContent;
      out.keyCheck = element("chain-check").textContent;
      out.nextBefore = kept.get(`register-chain-next:${given.dataset.ledger}`);
      element(given.settingsTick).checked = true;
      for (const fn of listeners["settings-form:submit"]) await fn({ preventDefault() {} });
    }
    out.nextAfter = kept.get(`register-chain-next:${given.dataset.ledger}`);
    out.startCheckAfter = element("chain-start-check").textContent;
    out.settingsSay = element("settings-say").textContent;
    out.settings = JSON.parse(kept.get(`register-settings:${given.dataset.shop}`) || "null");
  }
  // The day closing of a day, as the history shows it
  if (given.showDay) {
    for (const fn of listeners["day:change"]) fn({ target: { value: given.showDay } });
    out.dayTotals = (element("day-totals").children || []).map((item) => item.textContent);
    out.daySales = (element("day-sales").children || []).map((item) => item.textContent);
  }
  await wait(500);
  out.relay = relay;
  out.sent = kept.get(`register-nostr-sent:${given.dataset.ledger}`);
  out.tills = kept.get(`register-nostr-tills:${given.dataset.shop}`);
  // A big answer is written whole before leaving
  process.stdout.write(JSON.stringify(out), () => process.exit(0));
})().catch((error) => { console.error(error); process.exit(1); });
"""


def texts():
    from .register import texts as all_texts

    return {key: str(value) if isinstance(value, str) else value for key, value in all_texts().items()}


class RegisterRun(SimpleTestCase):
    """Runs the whole register in node. The tests are in the classes below, split by theme so that they run in
    parallel."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.node = os.environ.get("NODE") or shutil.which("node")
        if not cls.node:
            raise __import__("unittest").SkipTest("node is missing: NODE=/path/to/node to run the register")

    def derived(self, key, path):
        script = ('const fs = require("fs"); eval(fs.readFileSync(process.argv[1], "utf8"));'
                  f'registerDerive.addressAt({json.dumps(key)}, {json.dumps(path)}, "mainnet").then((a) => process.stdout.write(a));')
        return subprocess.run([self.node, "-e", script, str(STATIC / "register-derive.js")], capture_output=True, text=True,
                              timeout=60, check=True).stdout

    def derived_many(self, key, child, indexes):
        script = ('const fs = require("fs"); eval(fs.readFileSync(process.argv[1], "utf8"));'
                  f'Promise.all({json.dumps(list(indexes))}.map((i) => registerDerive.addressAt({json.dumps(key)}, '
                  f'[{child}, i], "mainnet"))).then((a) => process.stdout.write(JSON.stringify(a)));')
        return json.loads(subprocess.run([self.node, "-e", script, str(STATIC / "register-derive.js")], capture_output=True,
                                         text=True, timeout=60, check=True).stdout)

    def chain_settings(self):
        return {"register-settings:shop-1": json.dumps({"lightning": ADDRESS, "onchain": {"key": BIP84_ACCOUNT},
                                                        "discount": 0})}

    def chain_history(self, indexes, **fields):
        now = time.time() * 1000
        addresses = self.derived_many(BIP84_ACCOUNT, 0, indexes)
        return [{"id": f"s{index}", "at": now - 3 * 86400000, "cents": 900, "currency": "EUR", "status": "paid",
                 "method": "onchain", "sats": 9000, "address": address, "path": "0", "index": index, "till": "shop-1",
                 **fields} for index, address in zip(indexes, addresses)]

    def used_range(self, to, holes=()):
        """What the explorer answers for the addresses of the owner's chain below `to` (but `holes`): paid"""
        indexes = [index for index in range(to) if index not in holes]
        return {address: [{"txid": "e" * 64, "status": {"confirmed": True},
                           "vout": [{"scriptpubkey_address": address, "value": 5000}]}]
                for address in self.derived_many(BIP84_ACCOUNT, 0, indexes)}

    def run_register(self, dataset, storage, import_sales=None, relay=None, pull=None, ln=None, copy=None, typed=None,
                     import_till=None, confirm=True, no_subtle=False, clock_ahead=0, import_own=False, during_pull=None,
                     pull_twice=False, reopen=None, reopen_wait=None, show_day=None, settings=None, open_settings=False,
                     settings_tick=None, **extra):
        data = {"address": ADDRESS, "domain": "ln.example.org", "currency": "EUR", "sources": SOURCES, "tolerance": 1.0,
                "max_age": 120, "network": "mainnet", "explorers": ["https://mempool.example.org/api"], "discount": 10,
                "confirm_above": 0, "shop": {"name": "Bar", "town": "Example", "colour": "#224455"}, "texts": texts(),
                # On-chain turned on by the community (an experimental feature: shops/register.py, onchain_data); the
                # register of a community that has not is in shops/test_experimental.py
                "chain_on": True, "relays": [RELAY], "public_relays": [],
                "chain_claim": {"url": "/en/shops/register/shop-1/chain/", "csrf": "token"}, **extra}
        with tempfile.TemporaryDirectory() as folder:
            harness, given = Path(folder) / "flow.js", Path(folder) / "given.json"
            harness.write_text(HARNESS)
            given.write_text(json.dumps({"data": data, "dataset": dataset, "storage": storage, "importSales": import_sales,
                                         "importTill": import_till, "confirm": confirm, "relay": relay, "pull": pull, "ln": ln,
                                         "copy": copy, "typed": typed, "noSubtle": no_subtle,
                                         "clockAhead": clock_ahead, "importOwn": import_own,
                                         "duringPull": during_pull, "pullTwice": pull_twice, "reopen": reopen,
                                         "reopenWait": reopen_wait, "showDay": show_day, "settings": settings,
                                         "openSettings": open_settings, "settingsTick": settings_tick}))
            result = subprocess.run([self.node, str(harness), str(given), *(str(STATIC / name) for name in FILES),
                                     str(STATIC / "register.js")], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def own_explorer_sale(self, **extra):
        return self.run_register({"ledger": "shop-1", "shop": "shop-1"}, {"register-settings:shop-1": json.dumps({
            "lightning": ADDRESS, "onchain": {"key": BIP84_ACCOUNT}, "discount": 10, "confirmAbove": 0,
            "currency": "EUR", "sources": ["kraken", "coinbase"]})}, own_explorer="https://node.example.org:4081/api", **extra)


class SaleFlow(RegisterRun):
    """An on-chain sale of the owner's register: the address it takes, cancel, copies of the book."""

    def test_an_address_a_sale_on_this_phone_has_is_never_given_again(self):
        """A sale shown and not paid yet (another tab, a counter behind) keeps its address: the next sale skips it. A
        counter of another key, or of the old kind, starts from 0."""
        settings = {"register-settings:shop-1": json.dumps({"lightning": ADDRESS, "onchain": {"key": BIP84_ACCOUNT},
                                                            "discount": 0})}
        shown = {"id": "shown", "at": time.time() * 1000, "cents": 500, "currency": "EUR", "status": "pending",
                 "method": "onchain", "sats": 500, "address": self.derived(BIP84_ACCOUNT, [0, 0]), "path": "0", "index": 0,
                 "expires": time.time() * 1000 + 3600000}
        for counter in (json.dumps({"of": "another key/0", "next": 9}), "5"):
            out = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, {
                **settings, "register-ledger:shop-1": json.dumps([shown]), "register-chain-next:shop-1": counter})
            sale = out["sale"]
            self.assertNotEqual(sale["id"], "shown")
            self.assertEqual((sale["index"], sale["address"]), (1, self.derived(BIP84_ACCOUNT, [0, 1])))
            self.assertEqual(json.loads(out["next"]), {"of": f"{BIP84_ACCOUNT}/0", "next": 2})

    def test_a_payment_out_of_the_sales_time_or_much_bigger_does_not_close_it(self):
        """The owner's addresses are the wallet's own receive addresses: the wallet may give one out elsewhere, so a
        payment before the sale, or of another amount, is not taken for it."""
        settings = {"register-settings:shop-1": json.dumps({"lightning": ADDRESS, "onchain": {"key": BIP84_ACCOUNT}})}
        now = time.time() * 1000
        old, big, good = (self.derived(BIP84_ACCOUNT, [0, i]) for i in range(3))
        base = {"at": now - 60000, "cents": 900, "currency": "EUR", "status": "pending", "method": "onchain", "sats": 9000,
                "path": "0", "before": [], "wait": False, "expires": now + 3600000}
        sales = [{**base, "id": "old", "address": old, "index": 0}, {**base, "id": "big", "address": big, "index": 1},
                 {**base, "id": "good", "address": good, "index": 2}]
        tx = lambda address, value, at: [{"txid": hashlib.sha256(address.encode()).hexdigest(),  # noqa: E731
                                          "status": {"confirmed": True, "block_time": int(at / 1000)},
                                          "vout": [{"scriptpubkey_address": address, "value": value}]}]
        out = self.run_register({"ledger": "shop-1", "shop": "shop-1"},
                                {**settings, "register-ledger:shop-1": json.dumps(sales)},
                                atStart={old: tx(old, 9000, now - 3 * 86400000), big: tx(big, 50000, now),
                                         good: tx(good, 9000, now)})
        by_id = {sale["id"]: sale["status"] for sale in out["ledger"]}
        self.assertEqual((by_id["old"], by_id["big"], by_id["good"]), ("pending", "pending", "paid"))

    def test_an_on_chain_payment_after_cancel_is_paid_and_marked(self):
        settings = {"register-settings:shop-1": json.dumps({"lightning": ADDRESS, "onchain": {"key": BIP84_ACCOUNT}})}
        address = self.derived(BIP84_ACCOUNT, [0, 0])
        now = time.time() * 1000
        cancelled = {"id": "late", "at": now - 60000, "cents": 900, "currency": "EUR", "status": "cancelled",
                     "method": "onchain", "sats": 9000, "address": address, "path": "0", "index": 0, "before": [],
                     "wait": False, "expires": now + 3600000}
        paid = [{"txid": "f" * 64, "status": {"confirmed": True, "block_time": int(now / 1000)},
                 "vout": [{"scriptpubkey_address": address, "value": 9000}]}]
        # Days later: a payment the register saw in the mempool during the sale is still taken when its block comes, for
        # a week
        days_later = self.derived(BIP84_ACCOUNT, [0, 1])
        week = self.derived(BIP84_ACCOUNT, [0, 2])
        paid_later = [{**paid[0], "vout": [{"scriptpubkey_address": days_later, "value": 9000}]}]
        sales = [cancelled, {**cancelled, "id": "days later", "address": days_later, "index": 1, "at": now - 4 * 86400000,
                             "expires": now - 3 * 86400000, "seenAt": {"f" * 64: now - 3 * 86400000 - 60000}},
                 {**cancelled, "id": "week", "address": week, "index": 2, "expires": now - 7 * 86400000}]
        out = self.run_register({"ledger": "shop-1", "shop": "shop-1"},
                                {**settings, "register-ledger:shop-1": json.dumps(sales)},
                                atStart={address: paid, days_later: paid_later})
        by_id = {sale["id"]: sale for sale in out["ledger"]}
        late = by_id["late"]
        self.assertEqual((late["id"], late["status"], late["late"], late["txid"]), ("late", "paid", True, "f" * 64))
        self.assertNotIn("expires", late)
        self.assertEqual((by_id["days later"]["status"], by_id["days later"]["late"]), ("paid", True))
        # Past its week and still nothing: only the history
        self.assertEqual(by_id["week"]["status"], "cancelled")
        self.assertNotIn("expires", by_id["week"])
        # The next sale never takes a cancelled sale's address
        self.assertEqual(out["sale"]["index"], 3)

    def test_a_file_that_says_it_is_this_register_s_own_copy(self):
        """The encrypted file has no author: its sales are this register's own only on a phone with no sales yet, or when
        the merchant ticks that it is a copy of this register. Otherwise they are reported, and its closings stay out."""
        mine = {"id": "here", "at": time.time() * 1000, "cents": 100, "currency": "EUR", "status": "paid", "till": "shop-1"}
        theirs = [{"id": "claimed", "at": time.time() * 1000, "cents": 99900, "currency": "EUR", "status": "paid",
                   "sats": 999000, "till": "shop-1"}]
        settings = {"register-settings:shop-1": json.dumps({"lightning": ADDRESS})}
        cases = [({"register-ledger:shop-1": json.dumps([mine])}, False, "shop-1"),
                 ({"register-ledger:shop-1": json.dumps([mine])}, True, None), ({}, False, None)]
        for storage, ticked, reported in cases:
            out = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, {**settings, **storage}, import_sales=theirs,
                                    import_till="shop-1", import_own=ticked)
            claimed = next(sale for sale in out["afterImport"] if sale["id"] == "claimed")
            self.assertEqual(claimed.get("reported"), reported, (storage, ticked))
            self.assertEqual(out["closings"] is None, reported is not None)
            self.assertEqual("came in as reported" in out["imported"], reported is not None)
        # The owner's own copy without the box ticked, on a phone with sales: the ones it has are left as they are, and
        # the message says how many and to tick the box
        changed = [{**mine, "status": "expired"}]
        out = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, {**settings, **cases[0][0]}, import_sales=changed,
                                import_till="shop-1")
        self.assertIn("Copy imported: 0 new sales, 0 updated. 1 sales of this register's own were left as they were. If "
                      "the file is a copy of this register, and only if you made it yourself on this register, tick «It "
                      "is a copy of this register»", out["imported"])
        self.assertEqual(next(sale for sale in out["afterImport"] if sale["id"] == "here")["status"], "paid")
        out = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, {**settings, **cases[0][0]}, import_sales=changed,
                                import_till="shop-1", import_own=True)
        self.assertNotIn("left as they were", out["imported"])
        # Another register's copy: the «from» rule, whatever the box says
        out = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, settings, import_sales=theirs, import_till="staff",
                                import_own=True)
        self.assertNotIn("claimed", [sale["id"] for sale in out["afterImport"]])

    def test_the_practice_and_the_real_register_do_not_take_each_other_s_copies(self):
        """The practice's pretend sales never come into the real register as collected, nor the other way round."""
        practice = "practice:/shops/practice/shop-1/"
        pretend = [{"id": "pretend", "at": time.time() * 1000, "cents": 350, "currency": "EUR", "status": "paid",
                    "sats": 3500, "till": practice}]
        out = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, {"register-settings:shop-1": json.dumps({"lightning": ADDRESS})},
                                import_sales=pretend, import_till=practice)
        self.assertIn("copy of the practice register", out["imported"])
        self.assertEqual(out["afterImport"], [])
        real = [{**pretend[0], "id": "real", "till": "shop-1"}]
        out = self.run_register({"ledger": practice, "shop": "shop-1", "practice": "1"}, {}, import_sales=real,
                                import_till="shop-1")
        self.assertIn("copy of a real register", out["imported"])
        self.assertEqual(out["afterImport"], [])
        # A pretend sale inside a real register's own copy stays out too
        out = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, {"register-settings:shop-1": json.dumps({"lightning": ADDRESS})},
                                import_sales=[*pretend, *real], import_till="shop-1")
        self.assertEqual([sale["id"] for sale in out["afterImport"]], ["real"])


class ChainHistoryFlow(RegisterRun):
    """The chain of addresses after a history brought back or a lying explorer."""

    def test_the_owner_sees_how_many_addresses_went_unpaid_in_a_row(self):
        """Sales never paid use addresses: past 15 in a row the keypad says to raise the wallet's gap limit. The count
        starts after the newest paid sale of this key, or the newest address the explorer said was used."""
        settings = {"register-settings:shop-1": json.dumps({"lightning": ADDRESS, "onchain": {"key": BIP84_ACCOUNT}})}
        now = time.time() * 1000
        paid = {"id": "paid", "at": now - 60000, "cents": 900, "currency": "EUR", "status": "paid", "method": "onchain",
                "sats": 9000, "address": self.derived(BIP84_ACCOUNT, [0, 3]), "path": "0", "index": 3}
        # A sale of an earlier key at a later index does not count as paid on this key's chain
        other_key = {**paid, "id": "other key", "index": 10, "address": "bc1qsomewhereelse"}
        counter = {"register-chain-next:shop-1": json.dumps({"of": f"{BIP84_ACCOUNT}/0", "next": 17})}
        many = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, {**settings, **counter})
        self.assertIn("not paid in a row: 17.", many["gap"])
        self.assertIn("gap limit", many["gap"])
        few = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, {
            **settings, **counter, "register-ledger:shop-1": json.dumps([paid, other_key])})
        self.assertEqual(few["gap"], "")
        self.assertEqual(few["gapCount"], "On-chain addresses of this register not paid in a row: 13.")
        # Addresses skipped on the explorer's word (a `used` in the count kept on the phone) do not count: the owner's
        # starting number does
        skipped = {"register-chain-next:shop-1": json.dumps({"of": f"{BIP84_ACCOUNT}/0", "next": 17, "used": 12})}
        self.assertIn("not paid in a row: 17.", self.run_register({"ledger": "shop-1", "shop": "shop-1"},
                                                                  {**settings, **skipped})["gap"])
        started = {"register-chain-next:shop-1": json.dumps({"of": f"{BIP84_ACCOUNT}/0", "next": 17, "start": 12})}
        out = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, {**settings, **started})
        self.assertEqual((out["gap"], out["gapCount"]), ("", "On-chain addresses of this register not paid in a row: 5."))

    def test_a_restored_history_does_not_use_up_the_tries_on_its_own_addresses(self):
        """A phone with the history of a copy and no count (or the count of the old kind, a number): the next sale starts
        after the history's highest index of the key, without asking the explorer about those addresses."""
        now = time.time() * 1000
        sales = [{"id": f"s{i}", "at": now - 3 * 86400000, "cents": 900, "currency": "EUR", "status": "paid",
                  "method": "onchain", "sats": 9000, "address": self.derived(BIP84_ACCOUNT, [0, i]), "path": "0", "index": i,
                  "till": "shop-1"} for i in range(22)]
        for counter in ({"register-chain-next:shop-1": "21"}, {}):
            out = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, {
                **self.chain_settings(), "register-ledger:shop-1": json.dumps(sales), **counter})
            self.assertEqual(out["chainError"], "", counter)
            self.assertEqual((out["sale"]["index"], out["sale"]["address"]), (22, self.derived(BIP84_ACCOUNT, [0, 22])))
            self.assertEqual(json.loads(out["next"]), {"of": f"{BIP84_ACCOUNT}/0", "next": 23})

    def test_a_forged_index_in_the_history_does_not_move_the_chain(self):
        """Where a restored chain starts: only this register's own sales count, not a reported one, and not one far past
        the number of sales in the history (a forged copy): the next payments stay within the wallet's gap limit. An
        index at the end of the key's chain, or a count past it, does not stop every sale."""
        now = time.time() * 1000
        base = {"at": now - 3 * 86400000, "cents": 900, "currency": "EUR", "status": "paid", "method": "onchain",
                "sats": 9000, "path": "0", "till": "shop-1"}
        sales = [{**base, "id": "mine", "address": self.derived(BIP84_ACCOUNT, [0, 0]), "index": 0},
                 {**base, "id": "reported", "address": self.derived(BIP84_ACCOUNT, [0, 30]), "index": 30,
                  "reported": "shop-1:staff"},
                 {**base, "id": "far", "address": self.derived(BIP84_ACCOUNT, [0, 200]), "index": 200},
                 {**base, "id": "last", "address": "bc1qforged", "index": 0x7fffffff}]
        counters = ({}, {"register-chain-next:shop-1": json.dumps({"of": f"{BIP84_ACCOUNT}/0", "next": 2 ** 31 + 1})})
        for counter in counters:
            out = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, {
                **self.chain_settings(), "register-ledger:shop-1": json.dumps(sales), **counter})
            self.assertEqual(out["chainError"], "", counter)
            self.assertEqual((out["sale"]["index"], out["sale"]["address"]), (1, self.derived(BIP84_ACCOUNT, [0, 1])))
            self.assertEqual(json.loads(out["next"]), {"of": f"{BIP84_ACCOUNT}/0", "next": 2})

    def test_a_history_with_months_missing_starts_after_the_used_addresses(self):
        """A copy with only the last 30 sales (indexes 40–69) and no count: the highest index is within the bound for 30
        sales (30 + 50). When the newest sale went unpaid, its address all the same. With no history, the register leaps
        over the used addresses (and over an old unpaid one among them) with a bounded number of questions, never handing
        out an old address, up to 50 past what the phone knows."""
        settings = self.chain_settings()
        sales = self.chain_history(range(40, 70))
        # The history, the addresses the explorer says were paid, and where the next sale goes: the newest sale that
        # expired unpaid keeps its address all the same
        cases = [(sales, self.used_range(70), 70),
                 ([*sales[:-1], {**sales[-1], "status": "expired"}], self.used_range(69), 70),
                 ([], self.used_range(45, holes=[1, 20]), 45)]
        for history, used, expected in cases:
            out = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, {
                **settings, "register-ledger:shop-1": json.dumps(history)}, atStart=used)
            self.assertEqual(out["chainError"], "", len(history))
            self.assertEqual((out["sale"]["index"], out["sale"]["address"]),
                             (expected, self.derived(BIP84_ACCOUNT, [0, expected])), len(history))
            # Bounded: the sale's own address is then asked again and again, while it waits for its payment
            self.assertLess(len(set(out["asked"])), 100)
            next_ = json.loads(out["next"])
            self.assertEqual((next_["next"], next_.get("unsure")), (expected + 1, None))

    def test_a_history_far_past_what_the_phone_knows_gives_no_on_chain_sale(self):
        """A history whose highest index is far past the number of its sales (470–499 for 30 sales), or no history and an
        explorer that says 300 addresses were paid: the register does not leap that far on the explorer's word (it may
        be wrong, or lie), nor does it save such a count. No on-chain sale: the merchant looks into it."""
        for history, used in ((self.chain_history(range(470, 500)), self.used_range(500)), ([], self.used_range(300)),
                              (self.chain_history(range(60, 100), reported="?"), self.used_range(100))):
            out = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, {
                **self.chain_settings(), "register-ledger:shop-1": json.dumps(history)}, atStart=used)
            self.assertIn("look used (by the explorer's answers). If your wallet really used them, set the starting "
                          "number in Settings", out["chainError"])
            self.assertEqual(len(out["ledger"]), len(history))
            # Nothing saved: the count is as it was before the sale (none)
            self.assertIsNone(out.get("next"))

    def test_a_lying_explorer_moves_the_chain_no_further_than_50(self):
        """An explorer that says the first addresses it is asked about were paid, and none after: one sale moves the count
        at most 50 past the phone's own count (or past the number of its own sales, with no count of its own), and
        beyond it gives no on-chain address. A few lies leave a sale within the bound."""
        counted = {"register-chain-next:shop-1": json.dumps({"of": f"{BIP84_ACCOUNT}/0", "next": 5})}
        for counter, start in ((counted, 5), ({}, 0)):
            for lies in (3, 12, 30):
                out = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, {**self.chain_settings(), **counter},
                                        liar=lies)
                if lies == 3:
                    self.assertEqual(out["chainError"], "", counter)
                    self.assertEqual(out["sale"]["index"], start + 3, counter)
                elif lies == 30:
                    self.assertIn("look used (by the explorer's answers)", out["chainError"], counter)
                if out["chainError"]:
                    self.assertEqual(out["ledger"], [], (counter, lies))
                    # Refused: the count is back where it was before the sale, with none of the leaps
                    before = counter.get("register-chain-next:shop-1")
                    self.assertEqual(out.get("next") and json.loads(out["next"]), before and json.loads(before),
                                     (counter, lies))
                else:
                    self.assertLessEqual(out["sale"]["index"], start + 50, (counter, lies))
                    self.assertLessEqual(json.loads(out["next"])["next"], start + 51, (counter, lies))

    def test_a_lying_explorer_does_not_creep_the_count_over_retries(self):
        """The same lying explorer, sale after sale: each refused sale leaves the count where it was, so retries do not
        move it a little further each time."""
        storage = {**self.chain_settings(),
                   "register-chain-next:shop-1": json.dumps({"of": f"{BIP84_ACCOUNT}/0", "next": 5})}
        for _ in range(3):
            out = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, storage, liar=30)
            self.assertIn("look used (by the explorer's answers)", out["chainError"])
            self.assertEqual(json.loads(out["next"]), {"of": f"{BIP84_ACCOUNT}/0", "next": 5})
            storage["register-chain-next:shop-1"] = out["next"]

    def test_the_count_bound_starts_from_the_registers_own_sales_ahead_of_it(self):
        """A count behind the history (own sales 5–80, checked against the key, with the count at 5): the bound counts
        from the highest of them, and the next sale goes after them. Sales only reported in the way, with no question
        to the explorer: no message blaming the explorer."""
        counted = {"register-chain-next:shop-1": json.dumps({"of": f"{BIP84_ACCOUNT}/0", "next": 5})}
        own = self.chain_history(range(5, 81))
        out = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, {
            **self.chain_settings(), **counted, "register-ledger:shop-1": json.dumps(own)}, atStart=self.used_range(81))
        self.assertEqual(out["chainError"], "")
        self.assertEqual(out["sale"]["index"], 81)
        reported = self.chain_history(range(5, 81), reported="shop-1:staff")
        out = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, {
            **self.chain_settings(), **counted, "register-ledger:shop-1": json.dumps(reported)})
        self.assertNotIn("explorer's answers", out["chainError"])
        self.assertIn("did not find one that is surely unused", out["chainError"])
        self.assertEqual(out["asked"], [])
        self.assertEqual(json.loads(out["next"]), {"of": f"{BIP84_ACCOUNT}/0", "next": 5})


class ChainStartFlow(RegisterRun):
    """Where the owner's addresses start, and the count shared between tabs."""

    def test_the_owner_sets_the_path_of_the_addresses_after_seeing_its_first_one(self):
        fields = {"chain-key": BIP84_ACCOUNT, "currency": "EUR", "discount-percent": "0", "confirm-above": "0"}
        out = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, self.chain_settings(),
                                settings={**fields, "chain-path": " 0/5 "}, settings_tick="chain-confirm")
        self.assertEqual(out["firstSay"], "Tick the box to confirm.")
        self.assertIn(f"…/0/5/0: {self.derived(BIP84_ACCOUNT, [0, 5, 0])}", out["keyCheck"])
        self.assertEqual(out["settings"]["onchain"], {"key": BIP84_ACCOUNT, "path": "0/5"})
        # «0», or empty, is the receive chain: nothing to tick again for the same key
        out = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, self.chain_settings(), settings={**fields, "chain-path": "0"})
        self.assertEqual(out["settings"]["onchain"], {"key": BIP84_ACCOUNT})
        for typed in ("0'/5", "0h", "1/2/3/4/5", "2147483648", "a"):
            out = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, self.chain_settings(),
                                    settings={**fields, "chain-path": typed})
            self.assertIn("Not a path", out["settingsSay"], typed)
            self.assertNotIn("path", out["settings"]["onchain"], typed)
        out = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, self.chain_settings(),
                                settings={**fields, "chain-key": BIP84_RECEIVE[1], "chain-path": "0/5"})
        self.assertIn("needs an extended public key", out["settingsSay"])

    def test_the_owner_sets_where_the_on_chain_addresses_start(self):
        """A shop restored with too little history: the owner types the number after the last used address in the
        wallet, sees the address there, ticks the box, and the count starts from it, sure, with the gap count from it."""
        counted = {"register-chain-next:shop-1": json.dumps({"of": f"{BIP84_ACCOUNT}/0", "next": 5})}
        fields = {"chain-key": BIP84_ACCOUNT, "currency": "EUR", "discount-percent": "0", "confirm-above": "0"}
        out = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, {**self.chain_settings(), **counted},
                                settings={**fields, "chain-start": " 120 "}, settings_tick="chain-start-confirm")
        self.assertEqual(out["firstSay"], "Tick the box to confirm.")
        self.assertIn(f"…/0/120 of your key: {self.derived(BIP84_ACCOUNT, [0, 120])}", out["startCheck"])
        # Not saved before the box is ticked (the sale on the screen took 5)
        self.assertEqual(json.loads(out["nextBefore"]), {"of": f"{BIP84_ACCOUNT}/0", "next": 6})
        saved = json.loads(out["nextAfter"])
        self.assertEqual(saved, {"of": f"{BIP84_ACCOUNT}/0", "next": 120, "start": 120, "first": True})
        self.assertIn("The next on-chain sale uses the address …/0/120.", out["settingsSay"])
        later = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, {
            **self.chain_settings(), "register-chain-next:shop-1": out["nextAfter"]}, atStart=self.used_range(120))
        self.assertEqual((later["gap"], later["chainError"], later["sale"]["index"]), ("", "", 120))
        self.assertEqual(json.loads(later["next"]), {"of": f"{BIP84_ACCOUNT}/0", "next": 121, "start": 120})
        # Not a number, past the key's chain: refused; with a fixed address, no start
        for typed in ("abc", "-1", "1.5", "2147483647"):
            out = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, {**self.chain_settings(), **counted},
                                    settings={**fields, "chain-start": typed})
            self.assertIn("whole number", out["settingsSay"], typed)
            self.assertEqual(json.loads(out["nextAfter"])["next"], 6, typed)
        fixed = {**fields, "chain-key": BIP84_RECEIVE[1], "chain-start": "120"}
        out = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, {"register-settings:shop-1": json.dumps(
            {"lightning": ADDRESS, "onchain": {"address": BIP84_RECEIVE[1]}})}, settings=fixed)
        self.assertIn("needs an extended public key", out["settingsSay"])

    def test_a_new_key_starts_at_the_wallets_first_unused_address(self):
        """No number to type: saving a new key asks the explorer once for the first unused receive address (past the
        last used one, 20 unused in a row mean the end) and the count starts there. Within 50 it is saved at once; further
        on the owner sees that address and ticks the box, as for a number typed (an explorer that lies must not send the
        payments past the wallet's gap limit). Without the explorer the key is saved, and the first sale looks."""
        lightning_only = {"register-settings:shop-1": json.dumps({"lightning": ADDRESS})}
        fields = {"chain-key": BIP84_ACCOUNT, "currency": "EUR", "discount-percent": "0", "confirm-above": "0"}
        dataset = {"ledger": "shop-1", "shop": "shop-1"}
        # A wallet that used 0-6 and 9 (a hole at 7 and 8): it goes on at 10
        out = self.run_register(dataset, lightning_only, settings=fields, settings_tick="chain-confirm",
                                atStart=self.used_range(10, holes=(7, 8)))
        self.assertEqual(json.loads(out["nextAfter"]), {"of": f"{BIP84_ACCOUNT}/0", "next": 10, "start": 10, "first": True})
        self.assertIn("…/0/10", out["settingsSay"])
        self.assertEqual(out["settings"]["onchain"], {"key": BIP84_ACCOUNT})
        # A fresh wallet: from 0, nothing to keep
        out = self.run_register(dataset, lightning_only, settings=fields, settings_tick="chain-confirm")
        self.assertIsNone(out.get("nextAfter"))
        self.assertEqual(out["settings"]["onchain"], {"key": BIP84_ACCOUNT})
        # Far on: shown, and not saved before the box is ticked
        out = self.run_register(dataset, lightning_only, settings=fields, settings_tick="chain-confirm",
                                atStart=self.used_range(60))
        self.assertIn("number 60", out["settingsSay"] + self.texts_of(out))
        self.assertIsNone(out.get("nextAfter"))
        # The explorer does not answer: the key is saved, the first sale looks
        out = self.run_register(dataset, lightning_only, settings=fields, settings_tick="chain-confirm", explorerDown=True)
        self.assertIsNone(out.get("nextAfter"))
        self.assertEqual(out["settings"]["onchain"], {"key": BIP84_ACCOUNT})
        self.assertIn("first on-chain sale", out["settingsSay"])

    def texts_of(self, out):
        return " ".join(str(out.get(key) or "") for key in ("startCheckAfter", "keyCheck", "firstSay"))

    def test_a_starting_number_waits_for_the_explorers_first_answer(self):
        """The owner typed the last used number (one too low): its address received a payment. With the explorer not
        answering, a fresh start hands out no address (an old payment would close the sale); once the explorer has said
        one address from there is unused, the count is the phone's own again."""
        started = {"of": f"{BIP84_ACCOUNT}/0", "next": 119, "start": 119, "first": True}
        dataset = {"ledger": "shop-1", "shop": "shop-1"}
        busy = {**self.used_range(120), self.derived(BIP84_ACCOUNT, [0, 119]): {"error": "busy"}}
        for down in ({"atStart": busy}, {"explorerDown": True}):
            out = self.run_register(dataset, {**self.chain_settings(), "register-chain-next:shop-1": json.dumps(started)},
                                    **down)
            self.assertIn("The explorer does not answer", out["chainError"], down)
            self.assertEqual(out["ledger"], [], down)
            self.assertEqual(json.loads(out["next"]), started, down)
        out = self.run_register(dataset, {**self.chain_settings(), "register-chain-next:shop-1": json.dumps(started)},
                                atStart=self.used_range(120))
        self.assertEqual((out["chainError"], out["sale"]["index"]), ("", 120))
        self.assertEqual(json.loads(out["next"]), {"of": f"{BIP84_ACCOUNT}/0", "next": 121, "start": 119})
        # The owner's register uses the wallet's receive chain: even with a count of its own, no address without the
        # explorer's answer (a payment of someone else to it may sit in the mempool)
        later = self.run_register(dataset, {**self.chain_settings(), "register-chain-next:shop-1": out["next"]},
                                  explorerDown=True)
        self.assertIn("only after the explorer says it is unused", later["chainError"])
        self.assertEqual(json.loads(later["next"])["next"], 121)

    def test_an_own_sale_forged_far_on_does_not_lift_the_bound(self):
        """One sale of the register's own at index 5000, its address right for the key (anyone with the key can write
        it), does not move the bound: a lying explorer still gets the refusal, and the count stays."""
        counted = {"register-chain-next:shop-1": json.dumps({"of": f"{BIP84_ACCOUNT}/0", "next": 5})}
        forged = self.chain_history([5000], status="expired")
        out = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, {
            **self.chain_settings(), **counted, "register-ledger:shop-1": json.dumps(forged)}, liar=16)
        self.assertIn("look used (by the explorer's answers)", out["chainError"])
        self.assertEqual(json.loads(out["next"]), {"of": f"{BIP84_ACCOUNT}/0", "next": 5})

    def test_an_own_sale_forged_far_on_a_long_history_does_not_lift_the_bound(self):
        """On a register with a long history the window past the count grows only with the own sales at the count or
        after it, not with the whole history: one forged own sale at 650 over 300 paid ones lifts nothing."""
        dataset = {"ledger": "shop-1", "shop": "shop-1"}
        counted = {"register-chain-next:shop-1": json.dumps({"of": f"{BIP84_ACCOUNT}/0", "next": 300})}
        history = self.chain_history(range(300)) + self.chain_history([650], status="expired")
        out = self.run_register(dataset, {**self.chain_settings(), **counted,
                                          "register-ledger:shop-1": json.dumps(history)}, atStart=self.used_range(690))
        self.assertIn("look used (by the explorer's answers)", out["chainError"])
        self.assertEqual(json.loads(out["next"]), {"of": f"{BIP84_ACCOUNT}/0", "next": 300})

    def test_a_starting_number_saved_during_a_sale_still_waits_for_the_explorer(self):
        """The owner saves a starting number in another tab while a sale looks for its address: the new start is
        handed out only on the explorer's answer, and the answer about an earlier start does not end the new wait."""
        dataset = {"ledger": "shop-1", "shop": "shop-1"}
        key = "register-chain-next:shop-1"
        other = {"of": f"{BIP84_ACCOUNT}/0", "next": 9, "start": 9, "first": True}
        busy = {**self.used_range(6), self.derived(BIP84_ACCOUNT, [0, 9]): {"error": "busy"}}
        out = self.run_register(dataset, {**self.chain_settings(), key: json.dumps({"of": f"{BIP84_ACCOUNT}/0", "next": 5})},
                                atStart=busy, chainMeanwhile={"at": 1, "key": key, "value": json.dumps(other)})
        self.assertIn("The explorer does not answer", out["chainError"])
        self.assertTrue(json.loads(out["next"])["first"])
        started = {"of": f"{BIP84_ACCOUNT}/0", "next": 5, "start": 5, "first": True}
        out = self.run_register(dataset, {**self.chain_settings(), key: json.dumps(started)}, atStart=self.used_range(5),
                                chainMeanwhile={"at": 1, "key": key, "value": json.dumps(other)})
        self.assertEqual(out["sale"]["index"], 5)
        self.assertTrue(json.loads(out["next"])["first"])
        later = self.run_register(dataset, {**self.chain_settings(), key: out["next"]}, explorerDown=True)
        self.assertIn("The explorer does not answer", later["chainError"])

    def test_a_starting_number_saved_in_another_tab_is_never_lost_nor_skipped(self):
        """The owner saves a starting number in another tab while a sale runs. A restored chain's check of the addresses
        after it does not end the new number's wait; an answer missing while the number was saved hands out nothing; and
        a sale refused as too far does not put back the count from before the number."""
        dataset = {"ledger": "shop-1", "shop": "shop-1"}
        key = "register-chain-next:shop-1"
        account = f"{BIP84_ACCOUNT}/0"
        nine = {"of": account, "next": 9, "start": 9, "first": True}
        out = self.run_register(dataset, self.chain_settings(),
                                chainMeanwhile={"at": 2, "key": key, "value": json.dumps(nine)})
        self.assertTrue(json.loads(out["next"])["first"])
        busy = {**self.used_range(5), self.derived(BIP84_ACCOUNT, [0, 5]): {"error": "busy"}}
        out = self.run_register(dataset, {**self.chain_settings(), key: json.dumps({"of": account, "next": 5})},
                                atStart=busy, chainMeanwhile={"at": 1, "key": key, "value": json.dumps(nine)})
        self.assertIn("The explorer does not answer", out["chainError"])
        self.assertEqual(out["ledger"], [])
        hundred = {"of": account, "next": 100, "start": 100, "first": True}
        out = self.run_register(dataset, {**self.chain_settings(), key: json.dumps({"of": account, "next": 5})},
                                atStart=self.used_range(6),
                                chainMeanwhile={"at": 1, "key": key, "value": json.dumps(hundred)})
        self.assertEqual(json.loads(out["next"])["start"], 100)

    def test_a_refused_sale_leaves_a_count_another_tab_wrote(self):
        """A sale refused for going too far puts the count back only if it still holds what that sale wrote: another tab
        that took an index (or the owner who saved a starting number) meanwhile keeps its count."""
        counted = {"register-chain-next:shop-1": json.dumps({"of": f"{BIP84_ACCOUNT}/0", "next": 5})}
        other = json.dumps({"of": f"{BIP84_ACCOUNT}/0", "next": 9, "start": 9, "first": True})
        out = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, {**self.chain_settings(), **counted}, liar=30,
                                chainMeanwhile={"at": 5, "key": "register-chain-next:shop-1", "value": other})
        self.assertIn("look used (by the explorer's answers)", out["chainError"])
        self.assertEqual(json.loads(out["next"]), json.loads(other))

    def test_addresses_skipped_on_the_explorers_word_keep_the_gap_hint(self):
        """The count leaps over addresses the explorer says were used: if it lied, they are empty in the wallet, so the
        unpaid run counts from the phone's own paid sales only, and the hint to raise the gap limit stays."""
        counted = {"register-chain-next:shop-1": json.dumps({"of": f"{BIP84_ACCOUNT}/0", "next": 0})}
        # 0, 1, 2 used one by one, then the leap: 3, 4, 6, 10, 18 used, 34 not, and back to 19
        out = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, {**self.chain_settings(), **counted}, liar=8)
        self.assertEqual(out["sale"]["index"], 19)
        self.assertEqual(json.loads(out["next"]), {"of": f"{BIP84_ACCOUNT}/0", "next": 20})
        later = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, {
            **self.chain_settings(), "register-chain-next:shop-1": out["next"]})
        self.assertIn("not paid in a row: 20.", later["gap"])
        self.assertIn("gap limit", later["gap"])
        # Once the sale there is paid, the run starts after it
        paid = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, {
            **self.chain_settings(), "register-chain-next:shop-1": out["next"],
            "register-ledger:shop-1": json.dumps(out["ledger"])})
        self.assertEqual(paid["gap"], "")

    def test_a_history_brought_back_from_strangers_copies(self):
        """After losing the phone, the owner's own months come back as reported (their key was on the old phone): the
        highest of them counts once the explorer says its address was paid."""
        sales = self.chain_history(range(40), reported="?")
        out = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, {
            **self.chain_settings(), "register-ledger:shop-1": json.dumps(sales)}, atStart=self.used_range(40))
        self.assertEqual(out["chainError"], "")
        self.assertEqual(out["sale"]["index"], 40)
        # A reported far index that the explorer does not know (a forged copy) moves nothing
        forged = [*self.chain_history([0, 1, 2]), *self.chain_history([100000], reported="?")]
        out = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, {
            **self.chain_settings(), "register-ledger:shop-1": json.dumps(forged)}, atStart=self.used_range(3))
        self.assertEqual(out["chainError"], "")
        self.assertEqual(out["sale"]["index"], 3)
        self.assertEqual(json.loads(out["next"]), {"of": f"{BIP84_ACCOUNT}/0", "next": 4})

    def test_a_restored_chain_gives_no_address_the_explorer_has_not_checked(self):
        """No count of its own on this phone and an explorer that does not answer: no on-chain sale, whatever the history
        says, and the sale stays to take with Lightning. A count of this phone's own still gives its next address."""
        sales = self.chain_history(range(470, 500))
        for history in (sales, self.chain_history(range(40), reported="?"), []):
            out = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, {
                **self.chain_settings(), "register-ledger:shop-1": json.dumps(history)}, explorerDown=True)
            self.assertIn("gives an address only when the explorer says it is unused", out["chainError"])
            self.assertEqual(len(out["ledger"]), len(history))
            self.assertTrue(json.loads(out["next"])["unsure"])
            # Still not: the count it took is not a count of its own
            again = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, {
                **self.chain_settings(), "register-ledger:shop-1": json.dumps(history),
                "register-chain-next:shop-1": out["next"]}, explorerDown=True)
            self.assertIn("gives an address only when", again["chainError"])
        counted = {"register-chain-next:shop-1": json.dumps({"of": f"{BIP84_ACCOUNT}/0", "next": 500})}
        out = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, {
            **self.chain_settings(), "register-ledger:shop-1": json.dumps(sales), **counted}, explorerDown=True)
        self.assertIn("only after the explorer says it is unused", out["chainError"])
        self.assertEqual((len(out["ledger"]), json.loads(out["next"])["next"]), (len(sales), 500))


class ChainSaleFlow(RegisterRun):
    """The sale on the screen, the confirmations, and which explorer answers."""

    def test_a_payment_seen_in_the_mempool_waits_its_week(self):
        """A sale seen in the mempool and left to its confirmation: an explorer that does not answer after its day does
        not end it; after its week without a confirmation it is to check in the wallet. A sale with less than asked is
        to check in the wallet after its day, and is not looked at every minute for ever."""
        now = time.time() * 1000
        addresses = [self.derived(BIP84_ACCOUNT, [0, i]) for i in range(4)]
        base = {"at": now - 2 * 86400000, "cents": 900, "currency": "EUR", "method": "onchain", "sats": 9000, "path": "0",
                "before": [], "wait": True, "expires": now - 86400000, "till": "shop-1"}
        sales = [{**base, "id": "seen down", "status": "seen", "address": addresses[0], "index": 0, "txid": "b" * 64},
                 {**base, "id": "seen a week", "status": "seen", "address": addresses[1], "index": 1, "txid": "b" * 64,
                  "at": now - 8 * 86400000, "expires": now - 7 * 86400000},
                 {**base, "id": "partial", "status": "pending", "address": addresses[2], "index": 2},
                 {**base, "id": "never", "status": "pending", "address": addresses[3], "index": 3}]
        mempool = lambda address, value: [{"txid": "b" * 64, "status": {"confirmed": False},  # noqa: E731
                                           "vout": [{"scriptpubkey_address": address, "value": value}]}]
        out = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, {
            **self.chain_settings(), "register-ledger:shop-1": json.dumps(sales),
            "register-chain-next:shop-1": json.dumps({"of": f"{BIP84_ACCOUNT}/0", "next": 4})},
            atStart={addresses[0]: {"error": "busy"}, addresses[1]: mempool(addresses[1], 9000),
                     addresses[2]: mempool(addresses[2], 4000)})
        by_id = {sale["id"]: sale["status"] for sale in out["ledger"]}
        self.assertEqual([by_id[name] for name in ("seen down", "seen a week", "partial", "never")],
                         ["seen", "unchecked", "unchecked", "expired"])

    def test_the_sale_on_the_screen(self):
        """The explorer comes back: the button to confirm by hand goes away. At the end of its day, a payment seen in the
        mempool is left to its confirmation, not «not checked»."""
        now = time.time() * 1000
        address = self.derived(BIP84_ACCOUNT, [0, 0])
        sale = {"id": "open", "at": now, "cents": 900, "currency": "EUR", "status": "pending", "method": "onchain",
                "sats": 9000, "address": address, "path": "0", "index": 0, "before": [], "wait": True, "label": "9.00 EUR",
                "rate": 100000, "sources": [], "expires": now + 3600000, "till": "shop-1"}
        storage = {**self.chain_settings(), "register-open:shop-1": json.dumps("open"),
                   "register-chain-next:shop-1": json.dumps({"of": f"{BIP84_ACCOUNT}/0", "next": 1})}
        seen = [{"txid": "b" * 64, "status": {"confirmed": False}, "vout": [{"scriptpubkey_address": address, "value": 9000}]}]
        out = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, {**storage, "register-ledger:shop-1": json.dumps([sale])},
                                atStart={address: {"error": "busy"}}, reopen={address: seen})
        self.assertTrue(out["seenBefore"])
        self.assertIn("explorer does not answer", out["errorBefore"])
        self.assertFalse(out["seenAfter"])
        self.assertEqual(out["errorAfter"], "")
        ending = {**sale, "expires": now + 2500}
        out = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, {**storage, "register-ledger:shop-1": json.dumps([ending])},
                                atStart={address: seen}, reopen={address: seen}, reopen_wait=3000)
        self.assertEqual(out["ledger"][0]["status"], "seen")

    def test_the_confirmation_threshold_is_saved_as_typed_or_refused(self):
        storage = {"register-settings:shop-1": json.dumps({"lightning": ADDRESS, "confirmAbove": 5000})}
        fields = {"currency": "EUR", "discount-percent": "0", "chain-key": ""}
        for typed, saved in (("49,90", 4990), ("49.9", 4990), (" 100 ", 10000), ("", 0), ("50.", 5000), (".5", 50),
                             (",05", 5), ("10000000.00", 1000000000), ("99999999,99", 9999999999)):
            out = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, storage,
                                    settings={**fields, "confirm-above": typed})
            self.assertEqual((out["settingsSay"], out["settings"]["confirmAbove"]),
                             ("Saved on this phone. Staff registers get the new settings only with a new pairing code.", saved))
        # A threshold saved past what the field takes is shown at the most it takes: the other settings still save
        big = {"register-settings:shop-1": json.dumps({"lightning": ADDRESS, "confirmAbove": 2 * 10 ** 12})}
        out = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, big, settings={"discount-percent": "5"},
                                open_settings=True)
        self.assertEqual(out["shownThreshold"], "99999999.99")
        self.assertEqual((out["settings"]["confirmAbove"], out["settings"]["discount"]), (9999999999, 5))
        for typed in ("fifty", "1.000,50", "-5", "1e3", ".", "1.234", "100000000"):
            out = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, storage,
                                    settings={**fields, "confirm-above": typed})
            self.assertIn("Write the amount as a number", out["settingsSay"], typed)
            self.assertEqual(out["settings"]["confirmAbove"], 5000)

    def test_too_little_for_on_chain(self):
        settings = {"register-settings:shop-1": json.dumps({"lightning": ADDRESS, "onchain": {"key": BIP84_ACCOUNT}})}
        # 0.50 EUR less 10%, at 100,000 EUR per bitcoin: 450 sats, below the least on-chain
        out = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, settings, typed="50")
        self.assertIn("Too little for on-chain: the least is 1,000 sat", out["chainError"])
        self.assertIsNone(out.get("sale"))
        self.assertIsNone(out.get("next"))

    def test_a_transaction_another_sale_took_is_not_this_sale_payment(self):
        """A fixed address: another tab's sale already ended with this transaction. It does not close this one too."""
        fixed = BIP84_RECEIVE[1]
        settings = {"register-settings:shop-1": json.dumps({"lightning": ADDRESS, "onchain": {"address": fixed}})}
        other = {"id": "other", "at": time.time() * 1000, "cents": 900, "currency": "EUR", "status": "paid",
                 "method": "onchain", "sats": 9000, "address": fixed, "txid": "a" * 64}
        out = self.run_register({"ledger": "shop-1", "shop": "shop-1"},
                                {**settings, "register-ledger:shop-1": json.dumps([other])})
        self.assertEqual((out["sale"]["address"], out["sale"]["sats"]), (fixed, 9000))
        self.assertEqual(out["final"]["status"], "pending")
        self.assertEqual(out["seen"], "")

    def test_the_owner_takes_an_on_chain_sale_into_the_next_address_of_the_key(self):
        settings_key = "register-settings:shop-1"
        out = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, {settings_key: json.dumps({
            "lightning": ADDRESS, "onchain": {"key": BIP84_ACCOUNT}, "discount": 10, "confirmAbove": 0,
            "currency": "EUR", "sources": ["kraken", "coinbase"]})})
        self.assertEqual(out["blocked"], "")
        self.assertFalse(out["chainHidden"])
        # The owner did not let the registers remove the discount: a line to read, not a button
        self.assertIn("9.00", out["discountFixed"])
        sale = out["sale"]
        # 10.00 typed, 10% off, at 100,000 EUR per bitcoin: 9,000 sats into …/0/0 of the key, the wallet's own receive
        # chain, so that the payment shows in any wallet
        self.assertEqual((sale["method"], sale["cents"], sale["list"], sale["discount"], sale["sats"]), ("onchain", 900, 1000, 10, 9000))
        first = self.derived(BIP84_ACCOUNT, [0, 0])
        self.assertEqual(first, BIP84_RECEIVE[0])
        self.assertEqual((sale["address"], sale["index"], sale["path"]), (first, 0, "0"))
        self.assertIn(f"…/0/0 of your key: {first}", out["to"])
        self.assertEqual(json.loads(out["next"]), {"of": f"{BIP84_ACCOUNT}/0", "next": 1})
        # Every sale waits for a confirmation here (threshold 0): seen in the mempool first, then paid
        self.assertIn("waiting for a confirmation", out["seen"])
        # With the confidence and its reason: this explorer gives no fee rates
        self.assertIn("Confidence: probable (the explorer does not say the fee level).", out["seen"])
        # …and as the small bar that opens the reasons
        self.assertEqual(out["bar"], ["Confidence: probable", "likely"])
        self.assertEqual((out["final"]["status"], out["final"]["txid"]), ("paid", "a" * 64))
        # The explorer is the community's, and only for the sale's address
        self.assertTrue(all(url.startswith("https://mempool.example.org/api/address/") for url in out["asked"]))

    def test_the_shops_own_explorer_is_asked_first_and_said_in_the_reasons(self):
        out = self.own_explorer_sale()
        self.assertEqual(out["final"]["status"], "paid")
        addresses = [url for url in out["asked"] if "/address/" in url]
        # Always asked first; before the payment its empty answers are checked with the community's
        self.assertTrue(addresses[0].startswith("https://node.example.org:4081/api/address/"))
        self.assertEqual(out["why"][-1], "answered by your own explorer")

    def test_the_community_explorers_answer_when_the_own_one_does_not(self):
        for down in ("network", "status"):
            with self.subTest(down=down):
                out = self.own_explorer_sale(ownDown=down)
                self.assertEqual(out["final"]["status"], "paid")
                addresses = [url for url in out["asked"] if "/address/" in url]
                # Asked first at every round, answered by the community's
                self.assertTrue(addresses[0].startswith("https://node.example.org:4081/api/"))
                self.assertTrue(any(url.startswith("https://mempool.example.org/api/address/") for url in addresses))
                self.assertEqual(out["why"][-1], "answered by mempool.example.org: your own explorer did not answer")

    def test_an_empty_answer_of_the_own_explorer_is_checked_with_the_community_ones(self):
        out = self.own_explorer_sale(ownDown="empty")
        self.assertEqual(out["final"]["status"], "paid")
        self.assertEqual(out["why"][-1], "answered by mempool.example.org")

    def test_without_an_own_explorer_the_reasons_name_the_community_one(self):
        out = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, {"register-settings:shop-1": json.dumps({
            "lightning": ADDRESS, "onchain": {"key": BIP84_ACCOUNT}, "discount": 10, "confirmAbove": 0,
            "currency": "EUR", "sources": ["kraken", "coinbase"]})})
        self.assertEqual(out["why"][-1], "answered by mempool.example.org")


class StaffChainFlow(RegisterRun):
    """The staff registers' on-chain sales as the owner sees them, and the day closing."""

    def test_a_staff_register_paired_now_takes_the_shops_receive_chain_from_the_sites_count(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        harness = Path(folder.name) / "code.js"
        harness.write_text(r"""
const fs = require("fs");
for (const f of process.argv.slice(3)) eval(fs.readFileSync(f, "utf8"));
(async () => {
  const key = await registerDerive.childKey(process.argv[2], 0, "mainnet");
  const code = registerPairing.encode({ register: "%s", lightning: "%s", onchain: { key, path: "0" }, confirmAbove: 0,
    discount: 0, currency: "EUR", sources: ["kraken", "coinbase"], shop: { name: "Bar Anna", town: "Example" } },
    ["kraken", "coinbase", "bitstamp"]);
  process.stdout.write(JSON.stringify({ code, key }));
})();
""" % (REGISTER, ADDRESS))
        made = subprocess.run([self.node, str(harness), BIP84_ACCOUNT, str(STATIC / "register-derive.js"),
                               str(STATIC / "register-pairing.js")], capture_output=True, text=True, timeout=60)
        self.assertEqual(made.returncode, 0, made.stderr)
        made = json.loads(made.stdout)
        ledger = f"shop-1:{REGISTER}"
        dataset = {"ledger": ledger, "shop": "shop-1", "staff": REGISTER, "tillName": "Anna"}
        kept = {f"register-pairing:{ledger}": json.dumps({"code": made["code"], "at": 1})}
        claim = {"url": f"/en/shops/register/p/{REGISTER}/chain/", "key": "k" * 40}
        # The owner's register already took 0 and 1: this one gets 2, …/0/2 of the owner's key, the wallet's own
        out = self.run_register(dataset, kept, chain_claim=claim, shopCount=2)
        sale = out["sale"]
        self.assertEqual((sale["address"], sale["index"], sale["path"]), (self.derived(BIP84_ACCOUNT, [0, 2]), 2, "0"))
        self.assertEqual(out["final"]["status"], "paid")
        self.assertEqual(out["shopCount"], 3)
        # It asks with its key and the chain's hash, nothing else
        self.assertEqual(set(out["claims"][0]), {"chain", "at_least", "k"})
        self.assertEqual(out["claims"][0]["k"], "k" * 40)
        self.assertNotIn(made["key"], json.dumps(out["claims"]))
        # Without the site, no address: another register may have the next one on its screen
        down = self.run_register(dataset, kept, chain_claim=claim, claimDown=503)
        self.assertIsNone(down.get("sale"))
        self.assertIn("The site does not answer", down["chainError"])
        # A pairing kept on the phone that no longer reads (the community dropped a price source it names) says so
        broken = self.run_register(dataset, kept, sources=["coinbase", "bitstamp"])
        self.assertIn("no longer matches", broken["blocked"])
        self.assertIsNone(broken.get("sale"))
        # A pairing for another register is not taken
        other = {"register-pairing-pending": json.dumps({"register": "11111111-1111-4111-8111-111111111111",
                                                         "code": made["code"], "at": time.time() * 1000})}
        self.assertFalse(self.run_register(dataset, other)["paired"])

    def test_the_owners_register_takes_its_index_from_the_site_and_follows_the_path(self):
        settings_key = "register-settings:shop-1"
        on_phone = {"lightning": ADDRESS, "onchain": {"key": BIP84_ACCOUNT}, "confirmAbove": 0, "currency": "EUR",
                    "sources": ["kraken", "coinbase"]}
        out = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, {settings_key: json.dumps(on_phone)}, shopCount=5)
        self.assertEqual((out["sale"]["address"], out["sale"]["index"]), (self.derived(BIP84_ACCOUNT, [0, 5]), 5))
        # The phone's count follows the site's
        self.assertEqual(json.loads(out["next"])["next"], 6)
        # Another path set in the settings: …/0/5/n, its own count
        custom = {**on_phone, "onchain": {"key": BIP84_ACCOUNT, "path": "0/5"}}
        out = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, {settings_key: json.dumps(custom)})
        self.assertEqual((out["sale"]["address"], out["sale"]["index"], out["sale"]["path"]),
                         (self.derived(BIP84_ACCOUNT, [0, 5, 0]), 0, "0/5"))
        self.assertIn("…/0/5/0 of your key", out["to"])
        # Not signed in: no page token, no address, and the register says why
        out = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, {settings_key: json.dumps(on_phone)}, chain_claim=None)
        self.assertIsNone(out.get("sale"))
        self.assertIn("sign in", out["chainError"])

    def test_the_owner_sees_the_staff_sales_paid_to_addresses_not_of_the_key(self):
        from .test_register_chain import BIP84_CHANGE

        owner = {"register-settings:shop-1": json.dumps({"lightning": ADDRESS, "onchain": {"key": BIP84_ACCOUNT}})}
        sale = {"at": 1762700000000, "cents": 500, "currency": "EUR", "status": "paid", "method": "onchain", "sats": 5000,
                "label": "5.00 EUR"}
        # …/0/1 of the key is BIP84_RECEIVE[1]: right; the change address given as …/0/0 is not
        sale["till"] = "staff"
        sales = [{**sale, "id": "good", "address": BIP84_RECEIVE[1], "path": "0", "index": 1},
                 {**sale, "id": "bad", "address": BIP84_CHANGE, "path": "0", "index": 0},
                 # Exported while waiting: the customer of a changed page may have paid it just then
                 {**sale, "id": "waiting", "status": "pending", "address": BIP84_CHANGE, "path": "0", "index": 3},
                 # Never paid: no money went anywhere, no alarm
                 {**sale, "id": "unpaid", "status": "expired", "address": BIP84_CHANGE, "path": "0", "index": 2},
                 {**sale, "id": "lightning", "method": "lightning"},
                 # A sale the staff register's file says is the owner's own stays out
                 {**sale, "id": "forged", "method": "lightning", "till": "shop-1"},
                 # and so does an on-chain one: refused, it is no alarm either
                 {**sale, "id": "forged chain", "till": "shop-1", "address": BIP84_CHANGE, "path": "0", "index": 4}]
        out = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, owner, sales)
        self.assertIn("Copy imported: 5 new sales", out["imported"])
        ids = {entry["id"] for entry in out["afterImport"]}
        self.assertLessEqual({"bad", "good", "lightning", "unpaid", "waiting"}, ids)
        self.assertNotIn("forged", ids)
        self.assertNotIn("forged chain", ids)
        self.assertIn("2 on-chain sales of this copy were paid or asked for payment to addresses that are not of your current "
                      "key", out["imported"])
        self.assertIn("only the wallet shows that a payment really arrived", out["imported"])
        # This register's own copy, after the key changed: its sales of the earlier key are no alarm
        own = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, owner,
                                [{**sale, "id": "earlier key", "till": "shop-1", "address": BIP84_CHANGE, "path": "0", "index": 0},
                                 {**sale, "id": "earlier waiting", "status": "pending", "till": "shop-1",
                                  "address": BIP84_CHANGE, "path": "0", "index": 1}],
                                import_till="shop-1", import_own=True)
        self.assertIn("Copy imported: 2 new sales", own["imported"])
        self.assertNotIn("Careful", own["imported"])
        # A staff copy that first brought a sale as expired with another address, then as paid with the right one: the
        # book keeps the first address, and that is the one compared with the key
        first = {**sale, "id": "switched", "status": "expired", "address": BIP84_CHANGE, "path": "0", "index": 5,
                 "reported": "staff"}
        again = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, {
            **owner, "register-ledger:shop-1": json.dumps([first])},
            [{**first, "status": "paid", "address": self.derived(BIP84_ACCOUNT, [0, 5])}])
        self.assertIn("updated", again["imported"])
        stored = next(entry for entry in again["afterImport"] if entry["id"] == "switched")
        self.assertEqual((stored["status"], stored["address"]), ("paid", BIP84_CHANGE))
        self.assertIn("Careful: 1 on-chain sales", again["imported"])
        # The same sale twice in a copy is named once
        bad = {**sale, "id": "twice", "address": BIP84_CHANGE, "path": "0", "index": 6}
        twice = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, owner, [bad, {**bad, "status": "pending"}])
        self.assertIn("Careful: 1 on-chain sales", twice["imported"])

    def test_the_day_closing_says_what_waits_and_who_marked_a_sale_as_paid(self):
        at = time.mktime((2026, 11, 9, 12, 0, 0, 0, 0, -1)) * 1000
        later = time.time() * 1000 + 3600000
        base = {"currency": "EUR", "sats": 9000, "cents": 900, "label": "9.00 EUR"}
        sales = [{**base, "id": "chain", "at": at, "status": "paid", "method": "onchain"},
                 {**base, "id": "anna", "at": at + 1000, "status": "confirmed", "till": "shop-1:anna", "who": "Anna",
                  "reported": "shop-1:anna"},
                 {**base, "id": "copy", "at": at + 2000, "status": "confirmed", "till": "shop-1", "reported": "shop-1"},
                 {**base, "id": "waiting", "at": at + 3000, "status": "pending", "expires": later},
                 {**base, "id": "mempool", "at": at + 4000, "status": "seen", "method": "onchain", "expires": later,
                  "address": BIP84_RECEIVE[0], "before": []}]
        out = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, {
            "register-settings:shop-1": json.dumps({"lightning": ADDRESS}), "register-ledger:shop-1": json.dumps(sales)},
            show_day="2026-11-09")
        totals = "\n".join(out["dayTotals"])
        self.assertIn("On-chain: 1 paid", totals)
        self.assertNotIn("onchain", totals)
        self.assertIn("Still waiting for a payment or a confirmation: 2.", totals)
        lines = "\n".join(out["daySales"])
        self.assertIn("Marked as paid by Anna", lines)
        self.assertIn("Marked as paid by hand on another phone", lines)
        self.assertNotIn("by you", lines)


class NostrFlow(RegisterRun):
    """A staff register paired with the owner's Nostr key sends its sale to the relay as the day's event, encrypted to the
    owner; the owner's register, given the owner's private key, brings it into its book as reported by that register."""

    def test_a_staff_sale_reaches_the_owner_through_the_relay(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        harness = Path(folder.name) / "code.js"
        harness.write_text(r"""
const fs = require("fs");
for (const f of process.argv.slice(3)) eval(fs.readFileSync(f, "utf8"));
(async () => {
  const key = await registerDerive.childKey(process.argv[2], 0, "mainnet");
  const owner = registerNostr.publicKey("%s");
  const secret = registerNostr.newSecret();
  const code = registerPairing.encode({ register: "%s", lightning: "%s", onchain: { key, path: "0" }, confirmAbove: 0,
    discount: 0, currency: "EUR", sources: ["kraken", "coinbase"], shop: { name: "Bar Anna", town: "Example" },
    nostr: { owner, secret, relays: ["wss://nos.lol"] } }, ["kraken", "coinbase", "bitstamp"], ["wss://nos.lol"]);
  process.stdout.write(JSON.stringify({ code, owner, till: registerNostr.publicKey(secret) }));
})();
""" % (OWNER_SECRET, REGISTER, ADDRESS))
        made = subprocess.run([self.node, str(harness), BIP84_ACCOUNT, str(STATIC / "register-derive.js"),
                               str(STATIC / "register-nostr.js"), str(STATIC / "register-pairing.js")],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(made.returncode, 0, made.stderr)
        made = json.loads(made.stdout)
        ledger = f"shop-1:{REGISTER}"
        pending = json.dumps({"register": REGISTER, "code": made["code"], "at": time.time() * 1000})
        # Paired with a public relay the community has dropped since: still paired, the community's relay only
        staff = self.run_register({"ledger": ledger, "shop": "shop-1", "staff": REGISTER, "tillName": "Anna"},
                                  {"register-pairing-pending": pending},
                                  chain_claim={"url": f"/en/shops/register/p/{REGISTER}/chain/", "key": "k" * 40})
        self.assertEqual(staff["blocked"], "")
        self.assertEqual(staff["final"]["status"], "paid")
        # One event, the day's, from the register's key, encrypted to the owner, on the community's relay only
        [event] = staff["relay"]
        self.assertEqual((event["kind"], event["pubkey"]), (30078, made["till"]))
        self.assertEqual(event["tags"][1], ["p", made["owner"]])
        self.assertRegex(event["tags"][0][1], r"^bitmentor-register:[0-9a-f]{32}:\d{4}-\d{2}-\d{2}$")
        self.assertNotIn(REGISTER, json.dumps(event))
        self.assertNotIn("shop-1", json.dumps(event))
        # The register's part of the d tag is not a plain hash of its id, which anyone could work out from the shop's
        import hashlib

        for name in (ledger, "shop-1", REGISTER):
            plain_hash = hashlib.sha256(f"bitmentor-register:{name}".encode()).hexdigest()[:32]
            self.assertNotIn(plain_hash, event["tags"][0][1])
        # The owner's register, with the owner's key in the settings and the staff register's key known on this phone
        owner = {"register-settings:shop-1": json.dumps({"lightning": ADDRESS, "nostr": {"owner": made["owner"]}}),
                 "register-nostr-tills:shop-1": json.dumps({REGISTER: [made["till"]]})}
        out = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, owner, relay=staff["relay"], pull=OWNER_SECRET)
        self.assertIn("1 new sales", out["pulled"])
        [sale] = [entry for entry in out["after"] if entry["till"] == ledger]
        self.assertEqual((sale["id"], sale["status"], sale["who"], sale["reported"]),
                         (staff["final"]["id"], "paid", "Anna", ledger))
        # Another private key opens nothing and changes nothing
        other = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, owner, relay=staff["relay"], pull="22" * 32)
        self.assertIn("not the one", other["pulled"])
        self.assertEqual((out["secretLeft"], other["secretLeft"]), ("", ""))
        self.assertFalse([entry for entry in other["after"] if entry.get("till") == ledger])

    def copy_events(self, secret, ledger, sales, period=None, created=None):
        """The events of a copy of `sales` as the register with `secret` and `ledger` writes them, to the test owner."""
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        harness = Path(folder.name) / "copy.js"
        harness.write_text(r"""
const fs = require("fs");
const given = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
for (const f of process.argv.slice(3)) eval(fs.readFileSync(f, "utf8"));
(async () => {
  const owner = registerNostr.publicKey(given.ownerSecret);
  const till = await registerNostr.tillTag(given.ledger, given.secret, owner);
  const events = await registerNostr.copyEvents({ sales: given.sales, till, who: "x", period: given.period, owner,
    secret: given.secret, createdAt: given.created });
  const conversation = registerNostr.hex(await registerNostr.conversationKey(given.secret, owner));
  process.stdout.write(JSON.stringify({ events, owner, conversation }));
})();
""")
        given = {"ownerSecret": OWNER_SECRET, "secret": secret, "ledger": ledger, "sales": sales,
                 "period": period or time.strftime("%Y-%m-%d"), "created": created or int(time.time())}
        (Path(folder.name) / "given.json").write_text(json.dumps(given))
        made = subprocess.run([self.node, str(harness), str(Path(folder.name) / "given.json"), str(STATIC / "register-derive.js"),
                               str(STATIC / "register-nostr.js")], capture_output=True, text=True, timeout=60)
        self.assertEqual(made.returncode, 0, made.stderr)
        made = json.loads(made.stdout)
        self.owner_public = made["owner"]
        self.conversation = made["conversation"]
        return made["events"]

    def owner_storage(self, tills):
        return {"register-settings:shop-1": json.dumps({"lightning": ADDRESS, "nostr": {"owner": self.owner_public}}),
                "register-nostr-tills:shop-1": json.dumps(tills)}

    def test_a_staff_register_key_cannot_add_the_owner_s_own_sales(self):
        staff_secret = "33" * 32
        ledger = f"shop-1:{REGISTER}"
        now = int(time.time() * 1000)
        sale = {"at": now, "cents": 100000, "currency": "EUR", "status": "paid", "label": "1000.00 EUR"}
        # A staff phone in someone's control writes a sale «of the owner's register» (its till is the shop's id, public)
        events = self.copy_events(staff_secret, ledger, [{**sale, "id": "forged", "till": "shop-1"},
                                                         {**sale, "id": "honest", "till": ledger, "cents": 100}])
        out = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, self.owner_storage({REGISTER: [events[0]["pubkey"]]}),
                                relay=events, pull=OWNER_SECRET)
        by_id = {entry["id"]: entry for entry in out["after"]}
        self.assertNotIn("forged", by_id)
        self.assertEqual((by_id["honest"]["status"], by_id["honest"]["reported"]), ("paid", ledger))
        self.assertIn("1 copies that are not yours", out["pulled"])

    def test_a_staff_copy_on_nostr_is_compared_with_the_owner_s_key(self):
        """A staff register's copy pulled from Nostr: its on-chain sales whose address is not the owner's key's are named,
        as from a file; a sale it says is the owner's own is refused, and no alarm."""
        from .test_register_chain import BIP84_CHANGE

        staff_secret = "33" * 32
        ledger = f"shop-1:{REGISTER}"
        sale = {"at": int(time.time() * 1000), "cents": 500, "currency": "EUR", "status": "paid", "method": "onchain",
                "sats": 5000, "till": ledger, "label": "5.00 EUR"}
        events = self.copy_events(staff_secret, ledger, [
            {**sale, "id": "good", "address": BIP84_RECEIVE[1], "path": "0", "index": 1},
            {**sale, "id": "bad", "address": BIP84_CHANGE, "path": "0", "index": 0},
            {**sale, "id": "forged", "till": "shop-1", "address": BIP84_CHANGE, "path": "0", "index": 2}])
        storage = self.owner_storage({REGISTER: [events[0]["pubkey"]]})
        settings = json.loads(storage["register-settings:shop-1"])
        storage["register-settings:shop-1"] = json.dumps({**settings, "onchain": {"key": BIP84_ACCOUNT}})
        out = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, storage, relay=events, pull=OWNER_SECRET)
        ids = {entry["id"] for entry in out["after"]}
        self.assertLessEqual({"good", "bad"}, ids)
        self.assertNotIn("forged", ids)
        self.assertIn("1 on-chain sales of this copy were paid or asked for payment to addresses that are not of your "
                      "current key", out["pulled"])

    def test_a_staff_copy_on_nostr_that_switches_the_address_is_checked_on_the_stored_one(self):
        """A staff register's copy that first said a sale ended unpaid with another address, then paid with the right
        one: the book keeps the first address, and that is the one compared with the owner's key."""
        from .test_register_chain import BIP84_CHANGE

        staff_secret = "33" * 32
        ledger = f"shop-1:{REGISTER}"
        sale = {"id": "switched", "at": int(time.time() * 1000), "cents": 500, "currency": "EUR", "status": "paid",
                "method": "onchain", "sats": 5000, "till": ledger, "label": "5.00 EUR", "path": "0", "index": 1,
                "address": BIP84_RECEIVE[1]}
        events = self.copy_events(staff_secret, ledger, [sale])
        storage = self.owner_storage({REGISTER: [events[0]["pubkey"]]})
        settings = json.loads(storage["register-settings:shop-1"])
        storage["register-settings:shop-1"] = json.dumps({**settings, "onchain": {"key": BIP84_ACCOUNT}})
        storage["register-ledger:shop-1"] = json.dumps([{**sale, "status": "expired", "address": BIP84_CHANGE,
                                                         "reported": ledger}])
        out = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, storage, relay=events, pull=OWNER_SECRET)
        stored = next(entry for entry in out["after"] if entry["id"] == "switched")
        self.assertEqual((stored["status"], stored["address"]), ("paid", BIP84_CHANGE))
        self.assertIn("1 on-chain sales of this copy were paid or asked for payment to addresses that are not of your "
                      "current key", out["pulled"])

    def test_copies_of_keys_this_phone_does_not_know_stay_out_unless_the_owner_takes_them(self):
        stranger = "44" * 32
        now = int(time.time() * 1000)
        sales = [{"id": f"s{i}", "at": now, "cents": 100, "currency": "EUR", "status": "paid", "till": "shop-1"}
                 for i in range(5001)]
        events = self.copy_events(stranger, "shop-1", sales)
        storage = self.owner_storage({})
        # Asked, the owner says no: nothing of it in the book, only counted
        out = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, storage, relay=events, pull=OWNER_SECRET,
                                confirm=False)
        self.assertFalse([entry for entry in out["after"] if entry["id"].startswith("s")])
        self.assertIn("1 copies from keys this phone does not know were left out", out["pulled"])
        [question] = out["confirmed"]
        self.assertIn("1 copies on Nostr (5001 sales) come from 1 keys", question)
        # The owner takes them (after a lost phone): all reported, at most 5000 at once
        out = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, storage, relay=events, pull=OWNER_SECRET)
        taken = [entry for entry in out["after"] if entry["id"].startswith("s")]
        self.assertEqual(len(taken), 5000)
        self.assertTrue(all(entry["reported"] == "shop-1" for entry in taken))
        self.assertIn("5000 sales taken as reported, 1 left out", out["pulled"])

    def test_the_keys_of_a_register_turned_off_for_good_are_forgotten(self):
        staff_secret = "33" * 32
        ledger = f"shop-1:{REGISTER}"
        events = self.copy_events(staff_secret, ledger, [{"id": "late", "at": int(time.time() * 1000), "cents": 100,
                                                          "currency": "EUR", "status": "paid", "till": ledger}])
        other = "11111111-1111-4111-8111-111111111111"
        storage = self.owner_storage({REGISTER: [events[0]["pubkey"]], other: ["ab" * 32]})
        out = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, storage, relay=events, pull=OWNER_SECRET,
                                confirm=False, gone_tills=[REGISTER])
        self.assertEqual(json.loads(out["tills"]), {other: ["ab" * 32]})
        self.assertNotIn("late", [entry["id"] for entry in out["after"]])
        self.assertIn("1 copies from keys this phone does not know were left out", out["pulled"])

    def test_a_copy_from_the_future_is_not_taken(self):
        staff_secret = "33" * 32
        ledger = f"shop-1:{REGISTER}"
        sale = {"at": int(time.time() * 1000), "cents": 100, "currency": "EUR", "status": "paid", "till": ledger}
        soon = self.copy_events(staff_secret, ledger, [{**sale, "id": "soon"}], period="2026-01-01",
                                created=int(time.time()) + 3600)
        later = self.copy_events(staff_secret, ledger, [{**sale, "id": "later"}], period="2026-01-02",
                                 created=int(time.time()) + 3 * 86400)
        out = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, self.owner_storage({REGISTER: [soon[0]["pubkey"]]}),
                                relay=soon + later, pull=OWNER_SECRET)
        ids = [entry["id"] for entry in out["after"]]
        self.assertIn("soon", ids)
        self.assertNotIn("later", ids)
        self.assertIn("1 copies that are not yours", out["pulled"])

    def test_a_sale_recorded_during_a_pull_is_kept(self):
        staff_secret = "33" * 32
        ledger = f"shop-1:{REGISTER}"
        now = int(time.time() * 1000)
        events = self.copy_events(staff_secret, ledger, [{"id": "staff", "at": now, "cents": 100, "currency": "EUR",
                                                          "status": "paid", "till": ledger}])
        meanwhile = {"id": "meanwhile", "at": now, "cents": 250, "currency": "EUR", "status": "paid", "till": "shop-1"}
        out = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, self.owner_storage({REGISTER: [events[0]["pubkey"]]}),
                                relay=events, pull=OWNER_SECRET, during_pull=meanwhile)
        ids = [entry["id"] for entry in out["after"]]
        self.assertIn("staff", ids)
        self.assertIn("meanwhile", ids)

    def tills_and_keys(self, register_key, conversation, owner=None):
        """The owner's phone after pairing a staff register: its public key and the conversation key it made for it."""
        return {"register-nostr-tillkeys:shop-1": json.dumps({register_key: {"owner": owner or self.owner_public,
                                                                             "key": conversation}})}

    def test_the_owner_reads_the_staff_copies_with_no_nsec_when_the_phone_holds_their_keys(self):
        staff_secret = "33" * 32
        ledger = f"shop-1:{REGISTER}"
        now = int(time.time() * 1000)
        events = self.copy_events(staff_secret, ledger, [{"id": "staff", "at": now, "cents": 100, "currency": "EUR",
                                                          "status": "paid", "till": ledger, "who": "Anna"}])
        storage = {**self.owner_storage({REGISTER: [events[0]["pubkey"]]}),
                   **self.tills_and_keys(events[0]["pubkey"], self.conversation)}
        # No pull button pressed, no nsec: the sale arrives by itself, reported by that register
        out = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, storage, relay=events)
        [sale] = [entry for entry in out["ledger"] if entry["id"] == "staff"]
        self.assertEqual((sale["status"], sale["reported"], sale["who"]), ("paid", ledger, "Anna"))
        self.assertNotIn("cannot be read", out["nostrState"])
        # The same phone with the old storage (the public keys only): nothing arrives, and the owner is told
        old = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, self.owner_storage({REGISTER: [events[0]["pubkey"]]}),
                                relay=events)
        self.assertNotIn("staff", [entry["id"] for entry in old["ledger"]])
        self.assertIn("1 copies of staff registers cannot be read on this phone", old["nostrState"])

    def test_the_staff_conversation_key_is_used_only_for_its_own_register_and_owner(self):
        staff_secret = "33" * 32
        ledger = f"shop-1:{REGISTER}"
        now = int(time.time() * 1000)
        sale = {"at": now, "cents": 100, "currency": "EUR", "status": "paid", "till": ledger}
        events = self.copy_events(staff_secret, ledger, [{**sale, "id": "staff"}])
        pub = events[0]["pubkey"]
        conversation = self.conversation
        tills = {REGISTER: [pub]}
        # A key made for another owner's public key says nothing
        other = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, {
            **self.owner_storage(tills), **self.tills_and_keys(pub, conversation, owner="ab" * 32)}, relay=events)
        self.assertNotIn("staff", [entry["id"] for entry in other["ledger"]])
        # A key no longer listed in the register's keys (forgotten) says nothing either
        forgotten = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, {
            **self.owner_storage({}), **self.tills_and_keys(pub, conversation)}, relay=events)
        self.assertNotIn("staff", [entry["id"] for entry in forgotten["ledger"]])
        # A garbage key opens nothing
        bad = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, {
            **self.owner_storage(tills), **self.tills_and_keys(pub, "zz" * 32)}, relay=events)
        self.assertNotIn("staff", [entry["id"] for entry in bad["ledger"]])
        # A copy of that key naming another register in its d tag is not taken, whatever it opens with
        wrong = self.copy_events(staff_secret, "shop-1:other", [{**sale, "id": "elsewhere"}])
        refused = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, {
            **self.owner_storage(tills), **self.tills_and_keys(pub, self.conversation)}, relay=wrong)
        self.assertNotIn("elsewhere", [entry["id"] for entry in refused["ledger"]])
        # A sale of the owner's own register in a staff copy is left out, as with the nsec
        forged = self.copy_events(staff_secret, ledger, [{**sale, "id": "forged", "till": "shop-1"}])
        out = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, {
            **self.owner_storage(tills), **self.tills_and_keys(pub, self.conversation)}, relay=forged)
        self.assertNotIn("forged", [entry["id"] for entry in out["ledger"]])

    def test_the_owner_s_own_copies_and_strangers_still_need_the_nsec(self):
        stranger = "44" * 32
        now = int(time.time() * 1000)
        events = self.copy_events(stranger, "shop-1", [{"id": "s", "at": now, "cents": 100, "currency": "EUR",
                                                        "status": "paid", "till": "shop-1"}])
        # The stranger's key has a conversation key on this phone, but is not one this phone made for a register
        storage = {**self.owner_storage({}), **self.tills_and_keys(events[0]["pubkey"], self.conversation)}
        out = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, storage, relay=events)
        self.assertNotIn("s", [entry["id"] for entry in out["ledger"]])

    def test_the_keys_of_a_register_turned_off_go_with_its_conversation_key(self):
        staff_secret = "33" * 32
        ledger = f"shop-1:{REGISTER}"
        events = self.copy_events(staff_secret, ledger, [{"id": "late", "at": int(time.time() * 1000), "cents": 100,
                                                          "currency": "EUR", "status": "paid", "till": ledger}])
        pub = events[0]["pubkey"]
        other = "11111111-1111-4111-8111-111111111111"
        storage = {**self.owner_storage({REGISTER: [pub], other: ["ab" * 32]}), "register-nostr-tillkeys:shop-1": json.dumps(
            {pub: {"owner": self.owner_public, "key": self.conversation}, "ab" * 32: {"owner": self.owner_public, "key": "cd" * 32}})}
        out = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, storage, relay=events, gone_tills=[REGISTER])
        self.assertEqual(list(json.loads(out["tillkeys"])), ["ab" * 32])
        self.assertNotIn("late", [entry["id"] for entry in out["ledger"]])

    def test_a_staff_lightning_sale_arrives_by_nostr_and_is_checked_with_the_provider(self):
        staff_secret = "33" * 32
        ledger = f"shop-1:{REGISTER}"
        pr, payment_hash = invoice(10000)
        pr2, hash2 = invoice(20000, preimage="01" * 32)
        now = int(time.time() * 1000)
        sale = {"at": now, "cents": 1000, "currency": "EUR", "status": "paid", "till": ledger, "sats": 10000}
        events = self.copy_events(staff_secret, ledger, [
            {**sale, "id": "good", "hash": payment_hash, "verify": "https://ln.example.org/verify/0"},
            {**sale, "id": "lying", "hash": hash2, "verify": "https://ln.example.org/verify/1"}])
        storage = {**self.owner_storage({REGISTER: [events[0]["pubkey"]]}),
                   **self.tills_and_keys(events[0]["pubkey"], self.conversation)}
        verify = {0: {"status": "OK", "settled": True, "preimage": "00" * 32, "pr": pr},
                  # settled, but its invoice is for 20000 sats and the copy says 10000
                  1: {"status": "OK", "settled": True, "preimage": "01" * 32, "pr": pr2}}
        out = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, storage, relay=events,
                                ln={"invoices": [pr, pr2], "verify": verify}, typed=[])
        await_ledger = {entry["id"]: entry for entry in out["ledger"]}
        self.assertEqual((await_ledger["good"]["reported"], await_ledger["good"].get("verified")), (ledger, True))
        self.assertEqual((await_ledger["lying"]["reported"], await_ledger["lying"].get("verified")), (ledger, None))

    def test_a_staff_lightning_sale_in_a_file_is_checked_without_any_relay(self):
        """A community with no relays: the encrypted file at the end of the shift carries the check URL and the owner's
        register checks the sale with the provider when the file is imported."""
        ledger = f"shop-1:{REGISTER}"
        pr, payment_hash = invoice(10000)
        now = int(time.time() * 1000)
        sale = {"at": now, "cents": 1000, "currency": "EUR", "status": "paid", "till": ledger, "sats": 10000,
                "label": "10.00 EUR", "hash": payment_hash, "verify": "https://ln.example.org/verify/0"}
        verify = {0: {"status": "OK", "settled": True, "preimage": "00" * 32, "pr": pr}}
        sales = [{**sale, "id": "checked"}, {**sale, "id": "forged claim", "hash": "ab" * 32, "verified": True,
                                              "verify": "https://ln.example.org/verify/1"}]
        verify[1] = {"status": "OK", "settled": False, "pr": pr}
        out = self.run_register({"ledger": "shop-1", "shop": "shop-1"},
                                {"register-settings:shop-1": json.dumps({"lightning": ADDRESS})},
                                import_sales=sales, import_till=ledger, relays=[], ln={"invoices": [pr], "verify": verify},
                                typed=[])
        # In the file: «reported», and a `verified` the file brings is not believed
        right = {entry["id"]: entry for entry in out["afterImport"]}
        self.assertEqual((right["checked"]["reported"], right["checked"].get("verified")), (ledger, None))
        self.assertIsNone(right["forged claim"].get("verified"))
        after = {entry["id"]: entry for entry in out["afterVerify"]}
        self.assertTrue(after["checked"]["verified"])
        self.assertIsNone(after["forged claim"].get("verified"))

    def test_the_pull_button_pressed_twice_pulls_once(self):
        stranger = "44" * 32
        events = self.copy_events(stranger, "shop-1", [{"id": "s", "at": int(time.time() * 1000), "cents": 100,
                                                        "currency": "EUR", "status": "paid", "till": "shop-1"}])
        out = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, self.owner_storage({}), relay=events,
                                pull=OWNER_SECRET, confirm=False, pull_twice=True)
        # One request to the one relay, one question about the copies of unknown keys
        self.assertEqual(out["relayAsked"], 1)
        self.assertEqual(len(out["confirmed"]), 1)

    def test_a_staff_register_key_s_copy_of_another_register_is_not_taken(self):
        staff_secret = "33" * 32
        now = int(time.time() * 1000)
        # The copy says, in its d tag, it is of the owner's register: not the register that key is for
        events = self.copy_events(staff_secret, "shop-1", [{"id": "forged", "at": now, "cents": 100, "currency": "EUR",
                                                            "status": "paid", "till": f"shop-1:{REGISTER}"}])
        out = self.run_register({"ledger": "shop-1", "shop": "shop-1"}, self.owner_storage({REGISTER: [events[0]["pubkey"]]}),
                                relay=events, pull=OWNER_SECRET)
        self.assertNotIn("forged", [entry["id"] for entry in out["after"]])
        self.assertIn("0 new sales", out["pulled"])
        self.assertIn("1 copies that are not yours", out["pulled"])


CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def invoice(sats, preimage="00" * 32, made=None, prefix="lnbc", expiry=600, extra=()):
    """A BOLT 11 invoice as the register reads it: amount, moment, payment hash and expiry. The register does not check the
    signature (the customer's wallet does), so signature and checksum are zeros here."""
    import hashlib

    def words(value, count):
        return [(value >> (5 * (count - 1 - i))) & 31 for i in range(count)]

    payment_hash = hashlib.sha256(bytes.fromhex(preimage)).digest()
    made = int(time.time() if made is None else made)
    hashed = words(int.from_bytes(payment_hash, "big") << 4, 52)
    data = [*words(made, 7), 1, 1, 20, *hashed, 6, 0, 3, *words(expiry, 3), *extra, *[0] * (104 + 6)]
    msat = sats * 1000
    amount = f"{msat // 100000}u" if msat % 100000 == 0 else f"{msat * 10}p"
    return f"{prefix}{amount}1{''.join(CHARSET[word] for word in data)}", payment_hash.hex()


class LightningFlow(RegisterRun):
    """A Lightning sale of the owner's register against a provider answered here: one payment closes one sale only."""
    owner = {"ledger": "shop-1", "shop": "shop-1"}

    def sell(self, invoices, verify=None, typed=("1000",), network="mainnet", storage=None, whole=False, ln_meanwhile=None,
             **extra):
        storage = {"register-settings:shop-1": json.dumps({"lightning": ADDRESS}), **(storage or {})}
        out = self.run_register(self.owner, storage, ln={"typed": list(typed), "invoices": invoices, "verify": verify or {},
                                                         "meanwhile": ln_meanwhile},
                                discount=0, network=network, **extra)
        return out if whole else out["sales"]

    def quick(self, typed, ledger=(), **extra):
        """Sales of the owner's register in the quick start of 20.00 EUR, after `ledger`."""
        from .views import quick_texts

        # 100,000 EUR per bitcoin: one cent is 10 sats
        invoices = [invoice(int(amount) * 10)[0] for amount in typed]
        words = {**texts(), **{key: str(value) for key, value in quick_texts().items()}}
        return self.sell(invoices, typed=typed, whole=True, storage={"register-ledger:shop-1": json.dumps(list(ledger))},
                         quick_start={"cents": 2000, "currency": "EUR"}, texts=words, **extra)

    def test_the_quick_start_stops_a_sale_over_what_is_left(self):
        before = {"id": "a", "at": time.time() * 1000 - 60000, "cents": 1500, "currency": "EUR", "status": "paid", "sats": 15000}
        out = self.quick(("600",), [before])
        self.assertEqual(out["sales"][0]["error"], "This sale is 6.00 EUR and only 5.00 EUR of the quick start is left. "
                                                   "Type less, or finish the training to take any amount.")
        self.assertEqual([sale["id"] for sale in out["ledger"]], ["a"])
        # What is left is taken, to the last cent
        out = self.quick(("500",), [before])
        self.assertEqual(out["sales"][0]["error"], "")
        self.assertEqual(sorted(sale["cents"] for sale in out["ledger"]), [500, 1500])

    def test_the_quick_start_counts_what_is_paid_and_not_an_unpaid_qr(self):
        now = time.time() * 1000 - 60000
        sale = lambda name, cents, status, currency="EUR": {"id": name, "at": now, "cents": cents, "currency": currency,  # noqa: E731
                                                            "status": status, "sats": 1000}
        book = [sale("a", 500, "paid"), sale("b", 300, "confirmed"), sale("c", 200, "pending"), sale("d", 100, "unchecked"),
                sale("e", 400, "seen"),
                # Not taken: a QR still open or left unpaid with nothing to check it, cancelled, expired, another currency
                sale("f", 700, "cancelled"), sale("g", 700, "expired"), sale("h", 700, "paid", "USD")]
        # 500+300+400 = 1200: 8.00 left
        self.assertEqual(self.quick(("801",), book)["sales"][0]["error"][:37], "This sale is 8.01 EUR and only 8.00 E")
        self.assertEqual(self.quick(("800",), book)["sales"][0]["error"], "")

    def test_the_quick_start_used_up_says_so_and_a_history_brought_back_counts(self):
        book = [{"id": "a", "at": time.time() * 1000 - 60000, "cents": 2000, "currency": "EUR", "status": "paid", "sats": 1}]
        out = self.quick(("100",), book)
        self.assertEqual(out["sales"][0]["error"], "Quick start used up. Finish the training to keep taking payments.")
        self.assertEqual(len(out["ledger"]), 1)

    def test_the_quick_start_counts_only_in_the_communitys_currency(self):
        from .views import quick_texts

        out = self.sell(
            [invoice(10000)[0]], typed=("100",), whole=True, quick_start={"cents": 2000, "currency": "EUR"},
            texts={**texts(), **{key: str(value) for key, value in quick_texts().items()}},
            storage={"register-settings:shop-1": json.dumps({"lightning": ADDRESS, "currency": "USD", "sources": SOURCES})})
        self.assertEqual(out["sales"][0]["error"], "The quick start counts in EUR: set that currency in the settings.")
        self.assertEqual(out["ledger"], [])

    def test_without_a_quick_start_nothing_is_counted(self):
        book = [{"id": "a", "at": time.time() * 1000 - 60000, "cents": 99900, "currency": "EUR", "status": "paid", "sats": 1}]
        out = self.sell([invoice(5000)[0]], typed=("500",), whole=True, storage={"register-ledger:shop-1": json.dumps(book)})
        self.assertEqual(out["sales"][0]["error"], "")

    def test_a_payment_after_cancel_is_paid_and_marked(self):
        """The customer pays just after Cancel: while its invoice lasts the register looks again, and the sale becomes
        paid, marked as paid after it was cancelled. Once the invoice is over, it keeps only what the history needs."""
        pr, _hash = invoice(10000)
        now = time.time() * 1000
        cancelled = {"id": "late", "at": now - 60000, "cents": 1000, "currency": "EUR", "status": "cancelled", "sats": 10000,
                     "pr": pr, "hash": _hash, "verify": "https://ln.example.org/verify/0", "expires": now + 600000}
        verify = lambda at: f"https://ln.example.org/verify/{at}"  # noqa: E731
        paid = {"status": "OK", "settled": True, "preimage": "00" * 32, "pr": pr}
        down = {"status": "ERROR", "reason": "down"}
        unpaid = {"status": "OK", "settled": False, "pr": pr}
        sales = [
            cancelled,
            # Over and answered «not paid»: only the history
            {**cancelled, "id": "over", "expires": now - 1000, "verify": verify(1)},
            # Over, and the provider did not answer: looked at again, not dropped
            {**cancelled, "id": "silent", "expires": now - 1000, "verify": verify(2)},
            # Paid just before its end, and told only now
            {**cancelled, "id": "told late", "expires": now - 3600000, "verify": verify(3)},
            # A day after its end the provider still does not answer: dropped, to look for in the wallet
            {**cancelled, "id": "gone", "expires": now - 25 * 3600000, "verify": verify(4)},
        ]
        out = self.sell([pr] * 5, {0: paid, 1: unpaid, 2: down, 3: paid, 4: down}, typed=(), whole=True,
                        storage={"register-ledger:shop-1": json.dumps(sales)})
        by_id = {sale["id"]: sale for sale in out["ledger"]}
        self.assertEqual((by_id["late"]["status"], by_id["late"]["late"]), ("paid", True))
        self.assertNotIn("verify", by_id["late"])
        self.assertEqual((by_id["told late"]["status"], by_id["told late"]["late"]), ("paid", True))
        for gone in ("over", "gone"):
            self.assertEqual(by_id[gone]["status"], "cancelled")
            self.assertNotIn("verify", by_id[gone])
        self.assertEqual((by_id["silent"]["status"], by_id["silent"]["verify"]), ("cancelled", verify(2)))

    def test_an_answer_does_not_write_back_an_older_sale(self):
        """While the provider answers about a sale left waiting, a copy brings that sale in as paid: the answer («not
        paid», and the invoice over) does not put it back as expired."""
        pr, _hash = invoice(10000)
        now = time.time() * 1000
        waiting = {"id": "w", "at": now - 900000, "cents": 1000, "currency": "EUR", "status": "pending", "sats": 10000,
                   "pr": pr, "hash": _hash, "verify": "https://ln.example.org/verify/0", "expires": now - 1000}
        out = self.sell([pr], {0: {"status": "OK", "settled": False, "pr": pr}}, typed=(), whole=True,
                        storage={"register-ledger:shop-1": json.dumps([waiting])},
                        ln_meanwhile={"at": 0, "entry": {**waiting, "status": "paid"}})
        self.assertEqual([sale["status"] for sale in out["ledger"] if sale["id"] == "w"], ["paid"])

    def test_a_late_paid_answer_moves_forward_a_sale_nobody_knew_about_but_not_one_the_merchant_decided(self):
        """While the provider answers «paid», the sale meanwhile ended «not checked» or «expired»: it becomes paid. One
        the merchant cancelled, or confirmed by hand, in the meantime is left as it is."""
        pr, _hash = invoice(10000)
        now = time.time() * 1000
        waiting = {"id": "w", "at": now - 900000, "cents": 1000, "currency": "EUR", "status": "pending", "sats": 10000,
                   "pr": pr, "hash": _hash, "verify": "https://ln.example.org/verify/0", "expires": now - 1000}
        paid = {"status": "OK", "settled": True, "preimage": "00" * 32, "pr": pr}
        for meanwhile, expected in (("unchecked", "paid"), ("expired", "paid"), ("cancelled", "cancelled"),
                                    ("confirmed", "confirmed")):
            with self.subTest(meanwhile=meanwhile):
                out = self.sell([pr], {0: paid}, typed=(), whole=True,
                                storage={"register-ledger:shop-1": json.dumps([waiting])},
                                ln_meanwhile={"at": 0, "entry": {**waiting, "status": meanwhile}})
                self.assertEqual([sale["status"] for sale in out["ledger"] if sale["id"] == "w"], [expected])

    def test_an_invoice_served_again_does_not_close_a_second_sale(self):
        # 10.00 EUR at 100,000 EUR per bitcoin: 10,000 sats, twice; the provider (or a cache) gives the same invoice again
        pr, _hash = invoice(10000)
        first, second = self.sell([pr], {0: {"status": "OK", "settled": True, "preimage": "00" * 32, "pr": pr}},
                                  typed=("1000", "1000"))
        self.assertEqual([(sale["status"], sale["hash"]) for sale in first["ledger"]], [("paid", _hash)])
        self.assertIn("already used", second["error"])
        self.assertEqual(len(second["ledger"]), 1)

    def test_an_old_invoice_or_one_of_another_network_is_not_shown(self):
        [old] = self.sell([invoice(10000, made=time.time() - 600, expiry=3600)[0]])
        self.assertIn("old or already used", old["error"])
        self.assertEqual(old["ledger"], [])
        [testnet] = self.sell([invoice(10000, prefix="lntb")[0]])
        self.assertIn("wrong invoice", testnet["error"])
        self.assertEqual(testnet["ledger"], [])
        [mainnet_on_signet] = self.sell([invoice(10000)[0]], network="signet")
        self.assertIn("wrong invoice", mainnet_on_signet["error"])
        [signet] = self.sell([invoice(10000, prefix="lntbs")[0]], network="signet")
        self.assertEqual(signet["error"], "")
        self.assertEqual(signet["ledger"][0]["status"], "pending")

    def test_paid_only_for_this_invoice_and_its_preimage(self):
        pr, _hash = invoice(10000, preimage="ab" * 32)
        cases = {
            # Some providers leave the invoice out: the preimage of the sale's payment hash is proof enough
            "no invoice in the answer": ({"status": "OK", "settled": True, "preimage": "AB" * 32}, "paid"),
            "no invoice, preimage in base64": ({"status": "OK", "settled": True,
                                                "preimage": base64.b64encode(bytes.fromhex("ab" * 32)).decode()}, "paid"),
            "no invoice, another preimage": ({"status": "OK", "settled": True, "preimage": "cd" * 32}, "pending"),
            "no invoice, no preimage": ({"status": "OK", "settled": True}, "pending"),
            "another preimage": ({"status": "OK", "settled": True, "preimage": "cd" * 32, "pr": pr}, "pending"),
            "a preimage of another size": ({"status": "OK", "settled": True, "preimage": "ab" * 31, "pr": pr}, "pending"),
            "an error": ({"status": "ERROR", "reason": "x", "settled": True, "pr": pr}, "pending"),
            "another invoice": ({"status": "OK", "settled": True, "pr": invoice(10000, preimage="cd" * 32)[0]}, "pending"),
            "right": ({"status": "OK", "settled": True, "preimage": "AB" * 32, "pr": pr.upper()}, "paid"),
            "right, no preimage": ({"status": "OK", "settled": True, "pr": pr}, "paid"),
        }
        for name, (answer, status) in cases.items():
            [sale] = self.sell([pr], {0: answer})
            self.assertEqual(sale["ledger"][0]["status"], status, name)
        # Paid, says the provider, with nothing that proves whose payment it is: the merchant is told to look in the wallet
        # and gets the button, the register does not spin on «cannot check»
        [sale] = self.sell([pr], {0: {"status": "OK", "settled": True}})
        self.assertIn("cannot check that it is this sale's", sale["error"])
        self.assertTrue(sale["seen"])

    def test_without_webcrypto_only_the_invoice_in_the_answer_proves_the_payment(self):
        pr, _hash = invoice(10000, preimage="ab" * 32)
        [with_pr] = self.sell([pr], {0: {"status": "OK", "settled": True, "preimage": "ab" * 32, "pr": pr}}, no_subtle=True)
        self.assertEqual(with_pr["ledger"][0]["status"], "paid")
        [without] = self.sell([pr], {0: {"status": "OK", "settled": True, "preimage": "ab" * 32}}, no_subtle=True)
        self.assertEqual(without["ledger"][0]["status"], "pending")
        self.assertIn("cannot check that it is this sale's", without["error"])
        self.assertTrue(without["seen"])

    def test_a_sale_left_waiting_without_a_good_answer_ends_not_checked(self):
        pr, _hash = invoice(10000)
        now = time.time() * 1000
        waiting = {"id": "w", "at": now - 7200000, "cents": 1000, "currency": "EUR", "status": "pending", "sats": 10000,
                   "pr": pr, "hash": _hash, "verify": "https://ln.example.org/verify/0", "expires": now - 1000}
        out = self.sell([pr], {0: {"status": "ERROR", "reason": "down"}}, typed=(), whole=True,
                        storage={"register-ledger:shop-1": json.dumps([waiting])})
        self.assertEqual(out["ledger"][0]["status"], "unchecked")
        # Looked at again for a day: what the check needs stays
        self.assertEqual(out["ledger"][0]["verify"], "https://ln.example.org/verify/0")

    def test_a_sale_not_checked_is_looked_at_again_for_a_day(self):
        """The provider did not answer when the invoice ended: the register asks again every minute. Paid, it is paid
        (not «after cancel»); not paid, expired; still no answer, it waits; a day after, only the history."""
        pr, _hash = invoice(10000)
        now = time.time() * 1000
        verify = lambda at: f"https://ln.example.org/verify/{at}"  # noqa: E731
        base = {"at": now - 7200000, "cents": 1000, "currency": "EUR", "status": "unchecked", "sats": 10000, "pr": pr,
                "hash": _hash, "expires": now - 3600000}
        sales = [{**base, "id": "paid", "verify": verify(0)}, {**base, "id": "unpaid", "verify": verify(1)},
                 {**base, "id": "silent", "verify": verify(2)},
                 {**base, "id": "old", "verify": verify(3), "expires": now - 25 * 3600000}]
        answers = {0: {"status": "OK", "settled": True, "preimage": "00" * 32, "pr": pr},
                   1: {"status": "OK", "settled": False, "pr": pr}, 2: {"status": "ERROR"}, 3: {"status": "ERROR"}}
        out = self.sell([pr] * 4, answers, typed=(), whole=True, storage={"register-ledger:shop-1": json.dumps(sales)})
        by_id = {sale["id"]: sale for sale in out["ledger"]}
        self.assertEqual((by_id["paid"]["status"], by_id["paid"].get("late")), ("paid", None))
        self.assertEqual(by_id["unpaid"]["status"], "expired")
        self.assertNotIn("verify", by_id["unpaid"])
        self.assertEqual((by_id["silent"]["status"], by_id["silent"]["verify"]), ("unchecked", verify(2)))
        self.assertEqual(by_id["old"]["status"], "unchecked")
        self.assertNotIn("verify", by_id["old"])

    def test_the_owner_checks_the_staff_registers_lightning_sales_with_the_provider(self):
        """A staff sale reported as paid with a check URL of the shop's own provider becomes «verified» only when the
        provider says it is settled, with the invoice of that hash and of that amount. Everything else stays reported."""
        now = time.time() * 1000
        ledger = f"shop-1:{REGISTER}"
        url = lambda at: f"https://ln.example.org/verify/{at}"  # noqa: E731
        made = {n: invoice(10000, preimage=f"{n:02x}" * 32) for n in range(1, 16)}
        settled = lambda n, **change: {"status": "OK", "settled": True, "preimage": f"{n:02x}" * 32,  # noqa: E731
                                       "pr": made[n][0], **change}

        def staff_sale(name, n, **change):
            return {"id": name, "at": now - 60000, "cents": 1000, "currency": "EUR", "status": "paid", "sats": 10000,
                    "till": ledger, "reported": ledger, "hash": made[n][1], "verify": url(n), **change}

        sales = [
            staff_sale("ok", 1),
            # A provider that leaves the invoice out: the amount cannot be checked
            staff_sale("no invoice", 2),
            # The sale says 5000 sats, the provider's settled invoice is for 10000
            staff_sale("other amount", 3, sats=5000),
            # The provider answers with the invoice of another hash
            staff_sale("other hash", 4),
            staff_sale("not settled", 5),
            staff_sale("wrong preimage", 6),
            # Not the shop's provider: never asked
            staff_sale("elsewhere", 7, verify="https://evil.example.com/verify/7"),
            staff_sale("http", 8, verify="http://ln.example.org/verify/8"),
            # Marked as paid by hand, or without a check URL: stays reported
            staff_sale("by hand", 9, status="confirmed"),
            {k: v for k, v in staff_sale("no url", 10).items() if k != "verify"},
            # Two sales with one hash: one payment cannot confirm both
            staff_sale("twin 1", 11), staff_sale("twin 2", 11),
            # Already verified: not asked again
            staff_sale("done", 12, verified=True),
            # This register's own sale is not touched
            {"id": "own", "at": now - 30000, "cents": 100, "currency": "EUR", "status": "paid", "sats": 10000,
             "till": "shop-1", "hash": made[13][1], "verify": url(13)}]
        answers = {1: settled(1), 2: {k: v for k, v in settled(2).items() if k != "pr"},
                   3: settled(3), 4: settled(15), 5: settled(5, settled=False),
                   6: settled(6, preimage="aa" * 32), 7: settled(7), 8: settled(8), 9: settled(9),
                   11: settled(11), 12: {"status": "ERROR"}, 13: settled(13)}
        out = self.sell([made[1][0]], answers, typed=(), whole=True, storage={"register-ledger:shop-1": json.dumps(sales)})
        by_id = {sale["id"]: sale for sale in out["ledger"]}
        self.assertTrue(by_id["ok"]["verified"])
        self.assertTrue(by_id["done"]["verified"])
        for name in ("no invoice", "other amount", "other hash", "not settled", "wrong preimage", "elsewhere", "http",
                     "by hand", "no url", "twin 1", "twin 2", "own"):
            self.assertIsNone(by_id[name].get("verified"), name)
            self.assertEqual(by_id[name].get("reported"), None if name == "own" else ledger, name)
        self.assertEqual(by_id["by hand"]["status"], "confirmed")
        # Only the provider's own address was asked, and not for the sales that were left out beforehand
        asked = out["verifyAsked"]
        self.assertTrue(all(address.startswith("https://ln.example.org/verify/") for address in asked), asked)
        for left_out in (7, 8, 9, 10, 11, 12, 13):
            self.assertNotIn(url(left_out), asked)
        self.assertEqual(len(asked), len(set(asked)))

    def test_staff_sales_checked_in_batches_not_all_at_once(self):
        pr, payment_hash = invoice(10000)
        now = time.time() * 1000
        ledger = f"shop-1:{REGISTER}"
        sales = []
        answers = {}
        for n in range(20):
            sale_pr, sale_hash = invoice(10000, preimage=f"{n + 1:02x}" * 32)
            sales.append({"id": f"s{n}", "at": now - 60000 + n, "cents": 1000, "currency": "EUR", "status": "paid",
                          "sats": 10000, "till": ledger, "reported": ledger, "hash": sale_hash,
                          "verify": f"https://ln.example.org/verify/{n}"})
            answers[n] = {"status": "OK", "settled": True, "preimage": f"{n + 1:02x}" * 32, "pr": sale_pr}
        out = self.sell([pr] * 20, answers, typed=(), whole=True, storage={"register-ledger:shop-1": json.dumps(sales)})
        # One round asks for at most eight; the rest waits for the next round (every minute)
        self.assertEqual(len(out["verifyAsked"]), 8)
        self.assertEqual(sum(1 for sale in out["ledger"] if sale.get("verified")), 8)

    def test_a_phone_clock_minutes_off_is_set_right_by_the_site_time(self):
        """A phone ten minutes ahead: without the site's time every fresh invoice looks old; with it, the sale goes on. A
        saved copy of the page (its time not newer than the last seen) uses the difference measured before."""
        ahead = 10 * 60000
        site = time.time() * 1000
        [refused] = self.sell([invoice(10000, expiry=3600)[0]], clock_ahead=ahead)
        self.assertIn("old or already used", refused["error"])
        [taken] = self.sell([invoice(10000, expiry=3600)[0]], clock_ahead=ahead, now=site)
        self.assertEqual(taken["error"], "")
        self.assertEqual(taken["ledger"][0]["status"], "pending")
        measured = {"register-clock": json.dumps({"server": site + 60000, "offset": -ahead})}
        [saved_page] = self.sell([invoice(10000, expiry=3600)[0]], clock_ahead=ahead, now=site, storage=measured)
        self.assertEqual(saved_page["error"], "")
        stale = {"register-clock": json.dumps({"server": site + 60000, "offset": 0})}
        [unknown] = self.sell([invoice(10000, expiry=3600)[0]], clock_ahead=ahead, now=site, storage=stale)
        self.assertIn("old or already used", unknown["error"])
        # Really old, on a phone ten minutes behind: refused, and the merchant reads that the clock is off
        [old] = self.sell([invoice(10000, made=time.time() - 1200, expiry=3600)[0]], clock_ahead=-ahead, now=site)
        self.assertIn("old or already used", old["error"])
        self.assertIn("clock is 10 min away", old["error"])
        # The ledger's check stays whatever the clock: the same invoice never closes two sales
        pr, _hash = invoice(10000, expiry=3600)
        first, second = self.sell([pr], typed=("1000", "1000"), clock_ahead=ahead, now=site)
        self.assertEqual(first["error"], "")
        self.assertIn("already used", second["error"])

    def test_a_page_saved_days_ago_does_not_set_the_clock_back(self):
        """No difference saved, and the service worker gives a copy of the page saved three days ago: its time is not the
        phone's clock, and an invoice made twenty minutes ago stays old."""
        days_ago = (time.time() - 3 * 86400) * 1000
        expired = invoice(10000, made=time.time() - 1200, expiry=3600)[0]
        out = self.sell([expired], now=days_ago, whole=True)
        self.assertIn("old or already used", out["sales"][0]["error"])
        self.assertIsNone(out["clock"])
        # A copy as old as the clock difference allowed at most: still taken as the phone's clock being off, as on a
        # first visit (the test above), and saved
        out = self.sell([invoice(10000, expiry=3600)[0]], now=(time.time() - 10 * 60) * 1000, whole=True)
        self.assertEqual(out["sales"][0]["error"], "")
        self.assertLess(json.loads(out["clock"])["offset"], -9 * 60000)

    def test_an_invoice_with_two_payment_hashes_is_refused(self):
        """A provider could put a second payment hash after the first: a wallet pays the first (lnd) while the till
        would check the second, whose preimage the provider knows. Two hashes, or two expiries, and the invoice is not
        shown."""
        other = hashlib.sha256(bytes.fromhex("11" * 32)).digest()
        second_hash = [1, 1, 20, *[(int.from_bytes(other, "big") << 4 >> (5 * (51 - i))) & 31 for i in range(52)]]
        [twice] = self.sell([invoice(10000, expiry=3600, extra=second_hash)[0]])
        self.assertIn("wrong invoice", twice["error"])
        self.assertEqual(twice["ledger"], [])
        [expiry_twice] = self.sell([invoice(10000, expiry=60, extra=[6, 0, 3, 0, 3, 16])[0]])
        self.assertIn("wrong invoice", expiry_twice["error"])
        # One of each, as wallets make them: shown
        [single] = self.sell([invoice(10000, expiry=3600)[0]])
        self.assertEqual(single["error"], "")

    def test_a_rate_from_the_future_is_not_a_fresh_one(self):
        # A rate kept with a moment in the future (the phone's clock went back) at half the price: the register asks the
        # sources again, 10,000 sats and not 20,000
        rate = {"price": 50000, "at": time.time() * 1000 + 3600000, "sources": []}
        [sale] = self.sell([invoice(10000)[0]], storage={"register-rate:EUR": json.dumps(rate)})
        self.assertEqual(sale["error"], "")
        self.assertEqual(sale["ledger"][0]["sats"], 10000)


SW_HARNESS = r"""
const fs = require("fs");
const given = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
const made = (file) => file && { ok: file.status === 200, status: file.status, body: file.body, type: file.type || "basic",
  redirected: Boolean(file.redirected), url: file.url || "",
  headers: { has: (name) => name in file.headers, get: (name) => file.headers[name] ?? null } };
globalThis.Response = { redirect: (url, status) => ({ status, body: `redirect to ${url}` }) };
let handler;
globalThis.self = { registration: { scope: "https://example.org/register/" }, skipWaiting() {},
  clients: { claim() {} }, addEventListener: (type, fn) => { if (type === "fetch") handler = fn; } };
globalThis.location = { origin: "https://example.org" };
globalThis.caches = { open: async () => ({ match: async (request) => made(given.saved[new URL(request.url).pathname]) }) };
let network;
globalThis.fetch = async () => made(network);
eval(fs.readFileSync(process.argv[2], "utf8"));
(async () => {
  const out = [];
  for (const [path, mode, answer] of given.cases) {
    network = answer;
    let answering;
    handler({ request: { method: "GET", url: `https://example.org${path}`, mode }, respondWith: (p) => { answering = p; } });
    out.push((await answering).body);
  }
  process.stdout.write(JSON.stringify(out));
})();
"""

PAGE = {"status": 200, "body": "register", "headers": {"Content-Type": "text/html; charset=utf-8",
                                                       "Content-Security-Policy": "default-src 'self'", "X-Register": "1"}}
SCRIPT = {"status": 200, "body": "script", "headers": {"Content-Type": "text/javascript",
                                                       "Content-Security-Policy": "default-src 'self'"}}
# Caddy's works page: maintenance, or an address outside the whitelist. The same page for every path, without the CSP
WORKS = {"status": 200, "body": "works", "headers": {"Content-Type": "text/html; charset=utf-8"}}


class OfflineCopy(RegisterRun):
    """The copy of the register for when the site does not answer: Caddy's works page never takes its place."""

    def keep(self, page, script, before=None):
        storage = {"register-settings:shop-1": json.dumps({"lightning": ADDRESS})}
        return self.run_register({"ledger": "shop-1", "shop": "shop-1", "worker": "/register/sw.js"}, storage, copy={
            "files": {"/register/": page, "/static/register.js": script}, "scripts": ["https://example.org/static/register.js"],
            "before": before or {}})

    def test_the_page_keeps_only_the_site_own_register(self):
        self.assertEqual(self.keep(PAGE, SCRIPT), {"/register/": "register", "/static/register.js": "script"})
        before = {"/register/": "old register", "/static/register.js": "old script"}
        # In maintenance every path is the works page: the copy from before stays
        self.assertEqual(self.keep(WORKS, WORKS, before), before)
        # A page of the site that is not the register (no X-Register), a redirect, a page in place of the script
        other = {**PAGE, "headers": {**PAGE["headers"]}}
        del other["headers"]["X-Register"]
        self.assertEqual(self.keep(other, PAGE, before), before)
        self.assertEqual(self.keep({**PAGE, "redirected": True}, {**SCRIPT, "status": 404}, before), before)

    def test_the_service_worker_serves_the_copy_in_place_of_the_works_page(self):
        saved = {"/register/": {**PAGE, "body": "saved register"}, "/static/register.js": {**SCRIPT, "body": "saved script"}}
        missing = {"status": 404, "body": "not found", "headers": {**PAGE["headers"]}}
        cases = [["/register/", "navigate", WORKS], ["/static/register.js", "no-cors", WORKS],
                 ["/static/register.js", "no-cors", PAGE], ["/static/register.js", "no-cors", SCRIPT],
                 ["/register/", "navigate", PAGE], ["/register/", "navigate", missing],
                 ["/static/register.js", "no-cors", {**SCRIPT, "status": 502}],
                 # The staff rules or an expired login: the site sends elsewhere, and the browser follows
                 ["/register/", "navigate", {"status": 0, "type": "opaqueredirect", "body": "", "headers": {}}],
                 ["/register/", "navigate", {**PAGE, "body": "rules", "redirected": True, "url": "https://example.org/rules/"}],
                 # A script is never a navigation: the saved one stays
                 ["/static/register.js", "no-cors", {"status": 0, "type": "opaqueredirect", "body": "", "headers": {}}]]
        with tempfile.TemporaryDirectory() as folder:
            harness, given = Path(folder) / "sw.js", Path(folder) / "given.json"
            harness.write_text(SW_HARNESS)
            given.write_text(json.dumps({"saved": saved, "cases": cases}))
            result = subprocess.run([self.node, str(harness), str(STATIC / "register-sw.js"), str(given)],
                                    capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), ["saved register", "saved script", "saved script", "script", "register",
                                                     "not found", "saved script", "",
                                                     "redirect to https://example.org/rules/", "saved script"])

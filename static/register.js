// The register. For each sale the phone works out the sats with the rate of public price sources, asks the shop's
// Lightning address for the invoice (LNURL-pay, only from the address's own domain) and checks the payment with the
// provider's verify URL (LUD-21). Every sale stays on the phone, in the ledger below: the site never receives it
// (shops/register.py).
// On-chain is an experimental feature the community turns on (`experimental.onchain` in community.yml; the page then
// says `chain_on`, shops/register.py): without it this register takes Lightning only, whatever this phone holds (CHAIN_ON).
// With it, when the owner set it up on this phone (the settings screen) or a staff register was paired with the
// owner's code: a new address for each sale, derived here from the extended public key (register-derive.js), and the
// payment seen by asking the community's explorer (register-chain.js). The key, the addresses and the preferences never
// reach the site. What the site could still do is change this page itself: the register shows each address with its
// number, so the owner can find it in the wallet, and the owner checks the staff registers' addresses when importing
// their copies (docs/features/onchain.md, «What a tampered server can do»).
// With the owner's Nostr public key, the register keeps an encrypted copy of its sales on the community's relays, and a
// staff register sends its sales to the owner as they happen (register-nostr.js, register-relays.js,
// docs/features/nostr-copies.md).
// The practice register (static/practice.js, loaded first) puts its own pretend payments in place of the network:
// `window.registerBackend`, with the same three calls as `real` below.
(() => {
  const body = document.body;
  const text = body.dataset;
  const $ = (id) => document.getElementById(id);
  const data = JSON.parse($("register-data").textContent);
  const menu = $("menu");
  // The logic of the history on the phone (static/register-book.js, loaded first)
  const book = window.registerBook;
  // The pairing (register-pairing.js, loaded first)
  const pairing = window.registerPairing;
  // What the keypad does not need is loaded the first time it is used, so the register opens fast: the QR code at the
  // first sale, on-chain (register-derive.js, register-chain.js) and the Nostr copies (register-nostr.js,
  // register-relays.js) when this register uses them, or when the history or the settings open. Their addresses are in
  // the page (#register-modules), and the copy for when the site does not answer keeps them too (keepCopy). A file
  // already on the page (the tests load them all first) is not asked for again
  const LAZY = { ...$("register-modules")?.dataset };
  const GLOBALS = { qrcode: "qrcode", derive: "registerDerive", chain: "registerChain", nostr: "registerNostr",
    relays: "registerRelays" };
  // The Nostr copies use the curve of register-derive.js
  const NEEDS = { nostr: ["derive"] };
  const EVERYTHING = Object.keys(GLOBALS);
  const loading = {};
  let derive;
  let chain;
  let nostr;
  let relayNet;
  function bindModules() {
    derive = window.registerDerive;
    chain = window.registerChain;
    nostr = window.registerNostr;
    relayNet = window.registerRelays;
  }
  bindModules();
  function loadModule(name) {
    loading[name] ||= Promise.all((NEEDS[name] || []).map(loadModule)).then(() => new Promise((resolve, reject) => {
      if (window[GLOBALS[name]] || !LAZY[name]) {
        resolve();
        return;
      }
      const script = document.createElement("script");
      script.src = LAZY[name];
      script.onload = resolve;
      script.onerror = () => reject(new Error(name));
      document.head.append(script);
    })).catch((error) => {
      delete loading[name];
      throw error;
    });
    return loading[name];
  }
  async function need(...names) {
    await Promise.all(names.map(loadModule));
    bindModules();
  }
  const words = data.texts;
  // On-chain at the register, an experimental feature: on only when the site says so. Off, the register offers no
  // on-chain sale, asks no explorer and shows no on-chain setting; what this phone holds of it (the key in the settings
  // or in the pairing, the count of the addresses, the on-chain sales of the history) stays as it is, for when it is on
  const CHAIN_ON = data.chain_on === true;
  const staff = text.staff || "";
  const practice = Boolean(text.practice || window.registerBackend);
  const lang = document.documentElement.lang;
  const LEDGER = `register-ledger:${text.ledger}`;
  const OPEN = `register-open:${text.ledger}`;
  // The last good rate of the currency in use: the owner may change the currency in the settings
  const rateKey = () => `register-rate:${data.currency}`;
  const MIN_MSAT = 1000;
  const ASK_WAIT = 5000;
  const CHECK_EVERY = 3000;
  // A good rate less than a minute old is used for the next sale, without asking again
  const FRESH = 60000;
  const MAX_BYTES = 64 * 1024;
  // An explorer's list of transactions is bigger than a provider's answer: up to 75 transactions
  const MAX_CHAIN_BYTES = 1024 * 1024;
  // The explorer is asked less often than the Lightning provider: a public service, and a block every ten minutes
  const CHAIN_EVERY = 10000;
  const CHAIN_RECHECK = 60000;
  // An on-chain sale comes back on the screen for a day
  const CHAIN_LIFETIME = 24 * 3600000;
  // The least an on-chain sale asks: below a few hundred sats (dust) wallets refuse to pay, and above it the network's fee
  // would still eat most of it. Lightning takes small amounts
  const MIN_CHAIN_SATS = 1000;
  const SETTINGS = `register-settings:${text.shop}`;
  const PAIRED = `register-pairing:${text.ledger}`;
  const PENDING = "register-pairing-pending";
  // The next index of this register's chain, with the key and path it counts for: {of: "<key>/<path>", next}
  const NEXT = `register-chain-next:${text.ledger}`;
  // A pairing code opened more than this long ago is not applied: it was not this opening's
  const PENDING_LIFETIME = 15 * 60000;
  const CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l";
  const MSAT_PER_UNIT = { m: 1e8, u: 1e5, n: 100, p: 0.1 };
  // The invoice's prefix on each network (BOLT 11): an invoice of another network is not shown. On signet some wallets
  // still write testnet's
  const INVOICE_PREFIXES = { mainnet: ["bc"], signet: ["tbs", "tb"], testnet: ["tb"], regtest: ["bcrt"] };
  // An invoice made longer ago than this, plus the margin for a phone's clock that is a little off, is not this sale's:
  // a provider or a cache in between answering again with an old one (maybe already paid) for the same amount
  const INVOICE_AGE = 2 * 60000;
  const CLOCK_SKEW = 3 * 60000;
  // How to read the price from each source, with the names from shops/register.py
  const SOURCES = {
    kraken: { name: "Kraken", url: (c) => `https://api.kraken.com/0/public/Ticker?pair=XBT${c}`,
      price: (d) => Object.values(d.result || {})[0]?.c?.[0] },
    coinbase: { name: "Coinbase", url: (c) => `https://api.coinbase.com/v2/prices/BTC-${c}/spot`,
      price: (d, c) => (d.data?.currency === c ? d.data.amount : null) },
    bitstamp: { name: "Bitstamp", url: (c) => `https://www.bitstamp.net/api/v2/ticker/btc${c.toLowerCase()}/`,
      price: (d) => d.last },
    mempool: { name: "mempool.space", url: () => "https://mempool.space/api/v1/prices", price: (d, c) => d[c] },
    coingecko: { name: "CoinGecko", url: (c) => `https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=${c.toLowerCase()}`,
      price: (d, c) => d.bitcoin?.[c.toLowerCase()] },
    blockchain: { name: "blockchain.info", url: () => "https://blockchain.info/ticker", price: (d, c) => d[c]?.last },
  };
  let cents = 0;
  // The discount for paying in bitcoin is on at every new sale; when the owner allows it, a tap takes it away for that sale
  let discountOn = false;
  let blocked = "";
  let paired = null;
  let sale = null;
  let timer = null;
  let checking = false;
  let wake = null;
  let shown = "keypad";

  const say = (template, values = {}) => template.replace(/\{(\w+)\}/g, (all, key) => (key in values ? values[key] : all));
  const fiat = (value) => value.toLocaleString(lang, { style: "currency", currency: data.currency, maximumFractionDigits: 2 });
  const sats = (value) => `${value.toLocaleString(lang)} sat`;
  const moneyOf = (value) => `${(value / 100).toLocaleString(lang, { minimumFractionDigits: 2, useGrouping: false })} ${data.currency}`;
  const money = () => moneyOf(cents);
  // The phone's clock against the site's: a phone whose clock is some minutes off would refuse every invoice as old, or
  // take an old rate for a fresh one. The page carries the site's time (`now`, in shops/register.py); the difference is
  // kept, so that a copy of the page served while the site does not answer (register-sw.js), whose time is the one it
  // was saved at, is recognised (its time is not newer than the last one seen) and uses the difference measured before.
  // The few seconds the page takes to arrive are not a clock that is off
  const CLOCK = "register-clock";
  const CLOCK_NOISE = 30000;
  // With no difference saved (a first visit, or storage cleared while the saved copy stayed), a page that would set the
  // clock back by more than this may be a copy saved days ago: its age is not the phone's clock, and taken as such it
  // would make expired invoices look fresh. It is not used: a phone that is really this far ahead refuses invoices as
  // old, says how far its clock is, and is set right by the next page from the site after the clock is fixed
  const CLOCK_BACK_UNSAVED = 15 * 60000;
  function measureClock() {
    const saved = load(CLOCK, null);
    const server = Number(data.now);
    const known = Number.isFinite(saved?.server);
    if (Number.isFinite(server) && server > 0 && !(known && server <= saved.server)) {
      const offset = server - Date.now();
      if (!known && offset < -CLOCK_BACK_UNSAVED) return 0;
      store(CLOCK, { server, offset });
      return offset;
    }
    return Number.isFinite(saved?.offset) ? saved.offset : 0;
  }
  const measured = measureClock();
  const clockOffset = Math.abs(measured) < CLOCK_NOISE ? 0 : measured;
  // The time to compare invoices, expiries and rates with: the phone's, set right by the site's when it is off
  const now = () => Date.now() + clockOffset;
  // The browser vibrates only after a tap on the page: a reopened register that finds a paid sale has not had one yet
  const buzz = (pattern) => navigator.userActivation?.hasBeenActive !== false && navigator.vibrate?.(pattern);
  function load(key, fallback) {
    try {
      return JSON.parse(localStorage.getItem(key)) ?? fallback;
    } catch {
      return fallback;
    }
  }

  // Whether it was kept: without storage, or with the phone's room for the site full, the sale is still paid, but the
  // history on the phone loses it
  function store(key, value) {
    try {
      if (value === null) localStorage.removeItem(key);
      else localStorage.setItem(key, JSON.stringify(value));
      return true;
    } catch {
      return false;
    }
  }

  async function keepAwake(wanted) {
    try {
      if (wanted && !wake && navigator.wakeLock) {
        wake = await navigator.wakeLock.request("screen");
        wake.addEventListener("release", () => { wake = null; });
      } else if (!wanted && wake) {
        await wake.release();
      }
    } catch { /* the screen may turn off: the sale resumes when it wakes */ }
  }

  // The sale's state for screen reader users: each sentence once, when it changes
  function announce(message) {
    if ($("state").textContent !== message) $("state").textContent = message;
  }

  // Errors are an alert: rewriting them unchanged on every check would make them repeat every three seconds
  function warn(message) {
    if ($("error").textContent !== message) $("error").textContent = message;
  }

  // Where focus goes on each screen: never on a control that acts by itself, like Cancel
  const FOCUS = { keypad: "amount", step: "step-text", rate: "question", pay: "qr", done: "again", history: "history-title",
    settings: "settings-title" };

  function show(section) {
    for (const id of Object.keys(FOCUS)) if ($(id)) $(id).hidden = id !== section;
    // The menu is opened from the keypad only: during a sale nothing else is on the screen
    $("menu-open").hidden = section !== "keypad";
    closeMenu(false);
    keepAwake(section === "pay");
    const changed = section !== shown;
    shown = section;
    const focused = document.activeElement;
    if (changed && (!focused || focused === document.body || focused.closest(".register-screen[hidden]"))) $(FOCUS[section]).focus();
  }

  // The text on the OK key and on the check mark is read against the colour the theme chose, light or dark: the theme's
  // colour if enough, else the page's text or background, else black or white. Contrast as in WCAG, at least 4.5:1.
  function luminance(color) {
    const channels = color.match(/[\d.]+/g).slice(0, 3).map((value) => {
      const c = Number(value) / 255;
      return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
    });
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
  }

  // The colours are read with the transitions off: right after the shop's colour is set, the key is still fading from
  // the theme's colour, and the ink would be chosen for the old one (dark ink on a dark key)
  function keyColour(key, property) {
    const before = key.style.transition;
    key.style.transition = "none";
    const value = getComputedStyle(key)[property];
    key.style.transition = before;
    return value;
  }

  function bestInk(background, candidates, probe) {
    const contrast = (color) => {
      probe.style.color = color;
      const ink = luminance(getComputedStyle(probe).color);
      return (Math.max(ink, background) + 0.05) / (Math.min(ink, background) + 0.05);
    };
    return candidates.find((color) => contrast(color) >= 4.5) || candidates.reduce((a, b) => (contrast(a) >= contrast(b) ? a : b));
  }

  function setKeyInk() {
    const root = document.documentElement;
    root.style.removeProperty("--pos-on");
    root.style.removeProperty("--pos-clear");
    const page = getComputedStyle(document.body);
    const probe = document.createElement("span");
    document.body.append(probe);
    const ok = document.querySelector(".keys .ok");
    const candidates = [getComputedStyle(root).getPropertyValue("--pos-on").trim(), page.color, page.backgroundColor, "#000", "#fff"]
      .filter(Boolean);
    root.style.setProperty("--pos-on", bestInk(luminance(keyColour(ok, "backgroundColor")), candidates, probe));
    // «C» is written in the accent colour: on a light key a light accent is hard to read, then it takes the text colour
    const clear = document.querySelector(".keys .clear");
    if (clear) {
      const accent = keyColour(clear, "color");
      root.style.setProperty("--pos-clear", bestInk(luminance(keyColour(clear, "backgroundColor")), [accent, page.color, "#000", "#fff"], probe));
    }
    probe.remove();
  }

  // A request to another server: without cookies, without saying which till it comes from, never past the time limit
  async function ask(url, wait = ASK_WAIT, most = MAX_BYTES) {
    const stop = new AbortController();
    const late = setTimeout(() => stop.abort(), wait);
    try {
      const response = await fetch(url, {
        signal: stop.signal, credentials: "omit", referrerPolicy: "no-referrer", cache: "no-store", redirect: "error" });
      if (!response.ok) throw new Error(String(response.status));
      const content = await response.text();
      if (content.length > most) throw new Error("too big");
      return JSON.parse(content);
    } finally {
      clearTimeout(late);
    }
  }

  // --- Where the money goes and the preferences ---
  // The owner's register keeps its settings on this phone (SETTINGS); a staff register has those the owner put in its
  // pairing code (PAIRED), and no field to change them: it is paired again only with a new code of the owner

  const serverSources = [...data.sources];

  function ownerSettings() {
    const saved = load(SETTINGS, {});
    return saved && typeof saved === "object" && !Array.isArray(saved) ? saved : {};
  }

  // The register's preferences: a staff register's pairing, else the owner's settings, else what the site suggests
  function preferences(saved, pairedWith) {
    const from = pairedWith || saved;
    const sources = (Array.isArray(from.sources) ? from.sources : []).filter((name) => serverSources.includes(name));
    const onchain = from.onchain && typeof from.onchain === "object" ? from.onchain : null;
    return {
      discount: Number.isInteger(from.discount) && from.discount >= 0 && from.discount <= 100 ? from.discount : data.discount || 0,
      // Whether the registers may take the discount away from a sale: the owner's choice, off unless chosen
      discountRemovable: from.discountRemovable === true,
      confirmAbove: Number.isSafeInteger(from.confirmAbove) && from.confirmAbove >= 0 ? from.confirmAbove : data.confirm_above || 0,
      currency: /^[A-Z]{3}$/.test(from.currency || "") ? from.currency : data.currency,
      sources: sources.length >= 2 ? sources : serverSources,
      onchain: CHAIN_ON && onchain && (typeof onchain.key === "string" || typeof onchain.address === "string") ? onchain : null,
      shop: { ...(data.shop || {}), ...(pairedWith ? pairedWith.shop : saved.colour ? { colour: saved.colour } : {}) },
      note: typeof from.note === "string" ? from.note.slice(0, 140) : "",
    };
  }

  let prefs = preferences({}, null);

  // The pairing kept on this phone, checked again at every opening: a damaged one is no pairing. One that no longer
  // reads (the site changed what it is checked against, say the price sources) is kept, and the register says so
  // instead of looking as if it was never paired
  let pairingUnreadable = false;
  function readPairing() {
    pairingUnreadable = false;
    const kept = load(PAIRED, null);
    if (typeof kept?.code !== "string") return null;
    try {
      return { ...pairing.decode(kept.code, serverSources, data.public_relays || []), at: kept.at };
    } catch {
      pairingUnreadable = true;
      return null;
    }
  }

  function setUp() {
    blocked = "";
    paired = staff && !practice ? readPairing() : null;
    const saved = staff || practice ? {} : ownerSettings();
    // The practice has on-chain only as the remedy of one exercise (static/practice.js): an address no wallet can pay
    prefs = practice ? { ...preferences({}, null), discount: 0, onchain: CHAIN_ON && backend.practiceChain ? { address: backend.practiceChain } : null }
      : preferences(saved, paired);
    data.currency = prefs.currency;
    data.sources = prefs.sources;
    if (practice) {
      // Nothing to protect: the practice pays nobody
    } else if (staff) {
      if (!paired) blocked = pairingUnreadable ? words.pairing_unreadable : words.unpaired;
      // The address of the pairing is the one the owner saw when making the code: the site's must be the same
      else if (paired.lightning !== data.address) blocked = words.ln_changed;
    } else if (!saved.lightning) {
      // The owner's phone remembers the Lightning address it takes payments into: a change shows, and waits for the owner
      store(SETTINGS, { ...saved, lightning: data.address });
    } else if (saved.lightning !== data.address) {
      blocked = say(words.pin_changed, { before: saved.lightning, after: data.address });
    }
    showPreferences();
  }

  // A pairing code just opened on this phone (static/staff-open.js keeps it for a moment): it becomes this register's
  // only if it is for this register, recent, and whole. Returns what to tell, or ""
  async function takePending() {
    const pending = load(PENDING, null);
    if (!pending || !staff || practice) return "";
    store(PENDING, null);
    if (pending.register !== staff || !(Date.now() - pending.at < PENDING_LIFETIME)) return "";
    try {
      const found = pairing.decode(pending.code, serverSources, data.public_relays || []);
      if (found.register !== staff) throw new Error("register");
      await need("derive", "chain", "nostr", "relays");
      if (found.onchain?.key) await derive.parseKey(found.onchain.key, data.network);
      if (found.onchain?.address && !(await derive.checkAddress(found.onchain.address, data.network)).ok) throw new Error("address");
      if (found.nostr) {
        nostr.readPublic(found.nostr.owner);
        nostr.readSecret(found.nostr.secret);
      }
      store(PAIRED, { code: pending.code, at: Date.now() });
      return say(words.paired_now, { address: found.lightning });
    } catch {
      return words.pairing_wrong;
    }
  }

  function showPreferences() {
    $("blocked").hidden = !blocked;
    $("blocked-text").textContent = blocked;
    $("pin-ok").hidden = !blocked || staff || practice || !ownerSettings().lightning || ownerSettings().lightning === data.address;
    $("note").hidden = !prefs.note;
    $("note").textContent = prefs.note;
    if (paired) {
      $("shop-name").textContent = prefs.shop.name;
      $("shop-town").textContent = prefs.shop.town || "";
      $("shop-town").hidden = !prefs.shop.town;
    }
    if (prefs.shop.colour) document.documentElement.style.setProperty("--pos-primary", prefs.shop.colour);
    setKeyInk();
    // In the practice «OK, on-chain» shows only when the exercise offers it (the customer does not understand the code)
    // The page has the button only while on-chain is on
    if ($("chain-ok")) {
      $("chain-ok").hidden = !prefs.onchain || Boolean(blocked) || Boolean(data.retired)
        || (practice && !backend.chainOffered?.());
    }
    if ($("paired")) {
      $("paired").hidden = !paired;
      $("paired").textContent = paired ? [say(words.paired, { date: when(paired.at) }), CHAIN_ON && paired.onchain ? words.paired_chain : ""]
        .filter(Boolean).join(" ") : "";
    }
    discountOn = prefs.discount > 0;
    render();
    showGap();
  }

  // --- The exchange rate ---

  // One source's answer, with the moment it was read: every sale keeps which sources, when and with what value
  async function price(name) {
    const source = SOURCES[name];
    const value = Number(source.price(await ask(source.url(data.currency)), data.currency));
    if (!Number.isFinite(value) || value <= 0) throw new Error(name);
    return { source: name, price: value, at: now() };
  }

  // Answers until two different sources agree within the tolerance: the first two of the list together, then one more
  // at a time, in place of one that does not answer or to settle two that disagree
  async function prices() {
    const waiting = [...data.sources];
    const answers = [];
    while (waiting.length && !book.agreeing(answers, data.tolerance)) {
      const round = waiting.splice(0, Math.max(1, 2 - answers.length));
      for (const result of await Promise.allSettled(round.map(price))) {
        if (result.status === "fulfilled") answers.push(result.value);
      }
    }
    return answers;
  }

  function lastGood() {
    const saved = load(rateKey(), null);
    // A rate from the future (a phone whose clock went back) is no good rate: its age cannot be told
    return saved && Number.isFinite(saved.price) && Number.isFinite(saved.at) && saved.at <= now() ? saved : null;
  }

  function age(at) {
    const minutes = Math.max(1, Math.round((now() - at) / 60000));
    return minutes < 120 ? say(words.minutes, { count: minutes }) : say(words.hours, { count: Math.round(minutes / 60) });
  }

  const sourceNames = (list) => list.map((item) => SOURCES[item.source]?.name || item.source).join(" + ");
  const label = (answer) => `${fiat(answer.price)} (${sourceNames([answer])})`;

  // The rate to use, or the choices to put before the merchant: it never chooses silently
  async function quote() {
    const recent = lastGood();
    if (recent && now() - recent.at < FRESH) return { price: recent.price, sources: recent.sources, how: "agreed" };
    const answers = await prices();
    const pair = book.agreeing(answers, data.tolerance);
    if (pair) {
      const good = { price: (pair[0].price + pair[1].price) / 2, sources: pair, at: now() };
      store(rateKey(), good);
      return { price: good.price, sources: pair, how: "agreed" };
    }
    if (answers.length === 2) {
      const [first, second] = answers;
      return { question: say(words.disagree, { first: label(first), second: label(second) }), choices: answers, how: "chosen" };
    }
    if (answers.length > 2) {
      return { question: say(words.disagree_many, { rates: answers.map(label).join(", ") }), choices: answers, how: "chosen" };
    }
    if (answers.length === 1) {
      return { question: say(words.single, { first: label(answers[0]) }), choices: answers, how: "chosen" };
    }
    // No source answers: the last good rate, marked as old, if it is not too old
    const saved = lastGood();
    if (saved && now() - saved.at <= data.max_age * 60000) {
      const choice = { source: "saved", price: saved.price, sources: saved.sources };
      return { question: say(words.stale, { first: fiat(saved.price), age: age(saved.at) }), choices: [choice], how: "stale" };
    }
    return { question: saved ? words.too_old : words.no_rate, choices: [], how: "none" };
  }

  // --- The Lightning invoice ---

  // The provider's server: the address's domain or a subdomain of it, the ones the till's CSP leaves open
  function trusted(url) {
    try {
      const parsed = new URL(url);
      const host = parsed.hostname.toLowerCase();
      return parsed.protocol === "https:" && !parsed.port && (host === data.domain || host.endsWith(`.${data.domain}`));
    } catch {
      return false;
    }
  }

  const fail = (message) => Object.assign(new Error(message), { shown: true });

  function hex(fiveBits) {
    let acc = 0;
    let bits = 0;
    let out = "";
    for (const value of fiveBits) {
      acc = (acc << 5) | value;
      bits += 5;
      while (bits >= 8) {
        bits -= 8;
        out += ((acc >> bits) & 255).toString(16).padStart(2, "0");
      }
      acc &= (1 << bits) - 1;
    }
    return out;
  }

  // What the invoice asks for, when it expires and the payment hash: BOLT 11 without the signature, what the till needs
  function decode(invoice) {
    const value = String(invoice).toLowerCase();
    const split = value.lastIndexOf("1");
    const human = /^ln(bcrt|bc|tbs|tb|sb)(\d+)?([munp])?$/.exec(value.slice(0, split));
    const fiveBits = [...value.slice(split + 1)].map((c) => CHARSET.indexOf(c));
    if (!human || !human[2] || fiveBits.includes(-1) || fiveBits.length < 7 + 104 + 6) return null;
    const fields = fiveBits.slice(0, -104 - 6);
    const timestamp = fields.slice(0, 7).reduce((total, word) => total * 32 + word, 0);
    let expiry = 3600;
    let hash = "";
    const seen = new Set();
    for (let i = 7; i + 3 <= fields.length;) {
      const length = fields[i + 1] * 32 + fields[i + 2];
      const field = fields.slice(i + 3, i + 3 + length);
      // A payment hash or an expiry given twice is refused: wallets keep the first one (lnd) or refuse the invoice, and
      // a till reading another hash than the one the wallet pays could be shown a payment that is not this one
      if (fields[i] === 1 || fields[i] === 6) {
        if (seen.has(fields[i])) return null;
        seen.add(fields[i]);
      }
      if (fields[i] === 6) expiry = field.reduce((total, word) => total * 32 + word, 0);
      if (fields[i] === 1 && length === 52) hash = hex(field).slice(0, 64);
      i += 3 + length;
    }
    const msat = Number(human[2]) * (human[3] ? MSAT_PER_UNIT[human[3]] : 1e11);
    return { msat, made: timestamp * 1000, expires: (timestamp + expiry) * 1000, hash, prefix: human[1] };
  }

  function qrImage(content) {
    const modules = qrcode(content);
    const size = modules.length + 4;
    let path = "";
    modules.forEach((row, y) => row.forEach((dark, x) => { if (dark) path += `M${x + 2},${y + 2}h1v1h-1z`; }));
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${size} ${size}" shape-rendering="crispEdges">`
      + `<rect width="${size}" height="${size}" fill="#fff"/><path d="${path}" fill="#000"/></svg>`;
    return `data:image/svg+xml,${encodeURIComponent(svg)}`;
  }

  // LNURL-pay as in the check in shops/lnaddress.py: payRequest, limits, callback with amount in msat, then `verify`
  async function invoiceFor(msat) {
    const [user] = data.address.split("@");
    let metadata;
    let answer;
    try {
      metadata = await ask(`https://${data.domain}/.well-known/lnurlp/${encodeURIComponent(user)}`);
    } catch {
      throw fail(words.no_answer);
    }
    if (metadata?.tag !== "payRequest" || typeof metadata.callback !== "string") throw fail(words.no_answer);
    if (!trusted(metadata.callback)) throw fail(words.elsewhere);
    const least = Math.max(Number(metadata.minSendable) || MIN_MSAT, MIN_MSAT);
    if (msat < least || msat > Number(metadata.maxSendable)) throw fail(words.limits);
    try {
      answer = await ask(`${metadata.callback}${metadata.callback.includes("?") ? "&" : "?"}amount=${msat}`);
    } catch {
      throw fail(words.no_answer);
    }
    if (answer?.status === "ERROR" || typeof answer?.pr !== "string") throw fail(words.no_answer);
    const facts = decode(answer.pr);
    // An invoice for an amount different from the one asked, of another network or without a payment hash is not shown
    // to the customer
    if (!facts || facts.msat !== msat || facts.expires <= now() || !/^[0-9a-f]{64}$/.test(facts.hash)
      || !(INVOICE_PREFIXES[data.network] || ["bc"]).includes(facts.prefix)) throw fail(words.bad_invoice);
    // An invoice this phone already has (another sale's, paid or not) or an old one would let one payment close two
    // sales: refused, and the merchant tries again
    const pr = answer.pr.toLowerCase();
    if (ledger().some((entry) => entry.hash === facts.hash || (typeof entry.pr === "string" && entry.pr.toLowerCase() === pr))) {
      throw fail(words.old_invoice);
    }
    if (now() - facts.made > INVOICE_AGE + CLOCK_SKEW) {
      // An invoice that looks old to a phone whose clock is far from the site's may only be a clock to set right
      throw fail(Math.abs(measured) > CLOCK_SKEW
        ? `${words.old_invoice} ${say(words.clock_off, { minutes: Math.round(Math.abs(measured) / 60000) })}` : words.old_invoice);
    }
    return { pr: answer.pr, qr: qrImage(`lightning:${answer.pr}`),
      verify: typeof answer.verify === "string" && trusted(answer.verify) ? answer.verify : "", ...facts };
  }

  async function sha256Hex(hexText) {
    const bytes = Uint8Array.from(hexText.match(/../g), (pair) => parseInt(pair, 16));
    return [...new Uint8Array(await crypto.subtle.digest("SHA-256", bytes))].map((b) => b.toString(16).padStart(2, "0")).join("");
  }

  // A preimage as providers write it: 64 hexadecimal characters, or the 32 bytes in base64 (some write it so, as LND's
  // REST API does). Lowercase hex, or null for anything else. The two cannot be mistaken: base64 of 32 bytes is 43 or 44
  // characters long
  function preimageHex(value) {
    if (typeof value !== "string") return null;
    if (/^[0-9a-f]{64}$/i.test(value)) return value.toLowerCase();
    if (!/^[A-Za-z0-9+/_-]{43}=?$/.test(value)) return null;
    try {
      const bytes = atob(value.replace(/-/g, "+").replace(/_/g, "/").replace(/=?$/, "="));
      return bytes.length === 32 ? [...bytes].map((c) => c.charCodeAt(0).toString(16).padStart(2, "0")).join("") : null;
    } catch {
      return null;
    }
  }

  // Whether the provider says the invoice is paid (LUD-21): true, false, or an error when it does not answer or does not
  // answer for this invoice. «Paid» needs proof that it is this sale's payment: the invoice in the answer, or a preimage
  // whose hash is the sale's payment hash (some providers leave the invoice out). A preimage given must be the payment
  // hash's. When neither can be checked (no invoice in the answer and no WebCrypto to hash the preimage, on an insecure
  // page) the error says `unverifiable`: the merchant looks in the wallet
  async function settled(entry) {
    const status = await ask(entry.verify);
    if (!status || typeof status !== "object" || status.status === "ERROR") throw new Error("provider error");
    const pr = typeof status.pr === "string" ? status.pr.toLowerCase() : "";
    if (pr && pr !== String(entry.pr).toLowerCase()) throw new Error("other invoice");
    if (status.settled !== true) return false;
    if (status.preimage !== undefined && status.preimage !== null) {
      const preimage = preimageHex(status.preimage);
      if (!preimage) throw new Error("bad preimage");
      if (crypto.subtle) {
        if (typeof entry.hash !== "string" || await sha256Hex(preimage) !== entry.hash.toLowerCase()) {
          throw new Error("wrong preimage");
        }
        return true;
      }
    }
    if (pr) return true;
    throw Object.assign(new Error("cannot tell whose payment"), { unverifiable: true });
  }

  // Whether the provider says a staff register's Lightning sale is paid, for the owner's register to check it itself (a
  // copy alone proves nothing: whoever has the register's key writes what they like). The URL must be the shop's own
  // provider's (trusted), and the answer must carry the invoice, whose payment hash is the sale's and whose amount is the
  // sale's sats (the copy's amount is the register's word, the provider's invoice is the provider's); a preimage, if
  // given, must hash to it. Without the invoice in the answer the amount cannot be checked: an error, and the sale stays
  // «reported». True or false when the provider says so, an error when it does not answer or does not match
  async function settledReported(entry) {
    if (!trusted(entry.verify) || !crypto.subtle || !/^[0-9a-f]{64}$/i.test(entry.hash || "")
      || !Number.isSafeInteger(entry.sats) || entry.sats <= 0) throw new Error("cannot check");
    const status = await ask(entry.verify);
    if (!status || typeof status !== "object" || status.status === "ERROR") throw new Error("provider error");
    if (status.settled !== true) return false;
    const facts = typeof status.pr === "string" ? decode(status.pr) : null;
    if (!facts || facts.hash !== entry.hash.toLowerCase() || facts.msat !== entry.sats * 1000
      || !(INVOICE_PREFIXES[data.network] || ["bc"]).includes(facts.prefix)) throw new Error("other invoice");
    if (status.preimage !== undefined && status.preimage !== null) {
      const preimage = preimageHex(status.preimage);
      if (!preimage || await sha256Hex(preimage) !== facts.hash) throw new Error("wrong preimage");
    }
    return true;
  }

  // --- On-chain ---

  // The explorers to ask, in order: the shop's own first (its owner's node, when the owner set one: the owner's and the
  // staff registers alike), then the community's. The CSP opens only these
  const explorerList = () => [...(data.own_explorer ? [data.own_explorer] : []),
    ...(data.explorers || []).filter((api) => api !== data.own_explorer)];

  // Which explorer gave each address's last list of transactions, for the confidence's reasons: {own, host, ownFailed}.
  // Kept per address, so a check of another sale running at the same time cannot change it
  const sources = new Map();
  const hostOf = (api) => { try { return new URL(api, location.href).host; } catch { return ""; } };

  // The address's transactions, from the first explorer that answers: on a network error, a timeout or an answer that is
  // not OK (or not a list) the next one is asked. An empty list from the shop's own explorer is not trusted alone (a node
  // still syncing, or without its backend, answers so): the next one is asked too, and the empty list stands only when
  // none of them answers
  async function chainTxs(address) {
    let last = new Error("no explorer");
    let ownFailed = false;
    let ownEmpty = null;
    for (const api of explorerList()) {
      const own = api === data.own_explorer;
      try {
        const answer = await ask(chain.txsUrl(api, address), ASK_WAIT, MAX_CHAIN_BYTES);
        const listed = own ? chain.outputs(answer, address) : null;
        if (own && !listed) throw new Error("not an explorer's answer");
        if (own && !listed.length && !(Array.isArray(answer) && answer.length)) { ownEmpty = answer; continue; }
        sources.set(address, { own, host: hostOf(api), ownFailed });
        return answer;
      } catch (error) {
        last = error;
        if (own) ownFailed = true;
      }
    }
    if (ownEmpty !== null) {
      sources.set(address, { own: true, host: hostOf(data.own_explorer), ownFailed: false });
      return ownEmpty;
    }
    throw last;
  }

  // The current fee rates, for the confidence of a payment in the mempool: from the first explorer that gives them (the
  // same origins as the transactions, already in the CSP), asked at most every five minutes; null when none does
  const FEES_EVERY = 5 * 60000;
  let feesKept = { at: 0, fees: null };
  async function chainFees() {
    if (now() - feesKept.at < FEES_EVERY) return feesKept.fees;
    let fees = null;
    for (const api of explorerList()) {
      try {
        const answer = await ask(chain.feesUrl(api), ASK_WAIT, MAX_BYTES);
        fees = chain.feeRates(answer) ? answer : null;
        if (fees) break;
      } catch { /* the next one */ }
    }
    feesKept = { at: now(), fees };
    return fees;
  }

  // The facts of the transactions themselves, for the confidence: the last block's height (asked at most every minute)
  // and, for the payments in the mempool, their unconfirmed ancestors (mempool.space's /v1/cpfp; an Esplora without it
  // answers an error and the parents are not said). From the first explorer that answers; never an error
  const TIP_EVERY = 60000;
  let tipKept = { at: 0, tip: null };
  async function chainExtra(txids) {
    const first = async (url) => {
      for (const api of explorerList()) {
        try { return await ask(url(api), ASK_WAIT, MAX_BYTES); } catch { /* the next one */ }
      }
      return null;
    };
    if (now() - tipKept.at >= TIP_EVERY) tipKept = { at: now(), tip: chain.tipHeight(await first(chain.tipUrl)) };
    const ancestors = {};
    for (const txid of txids.slice(0, 3)) {
      const found = chain.ancestry(await first((api) => chain.cpfpUrl(api, txid)));
      if (found) ancestors[txid] = found;
    }
    return { tip: tipKept.tip, ancestors };
  }

  const backend = window.registerBackend ? { chainTxs, ...window.registerBackend } : { quote, invoiceFor, settled, chainTxs };

  // The transactions other sales on this phone already took on this address: never this sale's payment too (a fixed
  // address, two tabs that showed the same address)
  const takenBy = (mine) => ledger().filter((entry) => entry.id !== mine.id && entry.address === mine.address
    && typeof entry.txid === "string").map((entry) => entry.txid);

  // How an on-chain sale stands (register-chain.js), or an error when no explorer gives an answer. Only a payment in the
  // sale's time counts, from its creation to its end (with the margin of a block's clock): an address of the wallet's
  // receive chain may also be given out by the wallet, and a payment to it before or after the sale is not this sale's.
  // The moment the register first sees a transaction in the mempool is kept with the sale (`seenAt`), so that a payment
  // seen in time still counts when its block comes after the sale's end
  // With on-chain off no explorer is asked: an on-chain sale left waiting in the history gets no answer, and
  // recheckChain marks it at once as one to check in the wallet
  async function chainState(entry) {
    if (!CHAIN_ON) throw new Error("on-chain is off");
    const seenAt = entry.seenAt && typeof entry.seenAt === "object" ? entry.seenAt : {};
    const window = Number.isFinite(entry.at) && Number.isFinite(entry.expires)
      ? { from: entry.at, to: entry.expires, now: now(), seenAt } : null;
    sources.delete(entry.address);
    const txs = await backend.chainTxs(entry.address, entry);
    const source = sources.get(entry.address);
    if (window && source) window.source = source;
    // The fee rates only when something is in the mempool
    const listed = window ? chain.outputs(txs, entry.address) || [] : [];
    const waiting = listed.filter((item) => !item.confirmed && !(entry.before || []).includes(item.txid))
      .map((item) => item.txid);
    if (waiting.length) window.fees = await (backend.chainFees || chainFees)().catch(() => null);
    if (listed.length) {
      Object.assign(window, await (backend.chainExtra || chainExtra)(waiting).catch(() => ({})));
    }
    const result = chain.payment(txs, entry.address, entry.sats,
      [...(entry.before || []), ...takenBy(entry)], entry.wait !== false, entry.spent || [], window);
    if (result.state === "unknown") throw new Error("not an explorer's answer");
    if (Object.keys(result.seenAt).length > Object.keys(seenAt).length) {
      entry.seenAt = result.seenAt;
      recordAgain(entry, (fresh) => ({ ...fresh, seenAt: result.seenAt }));
    }
    return result;
  }

  // The last index of a key's chain (BIP32: from 2^31 on they are hardened, which an account key cannot derive)
  const MAX_INDEX = 0x7fffffff;
  // A history's highest index counts only this far past the number of this chain's sales in it: a copy that says a sale
  // was at a far index (a forged one, say) would push the next payments past the wallet's gap limit
  const LEDGER_JUMP = 50;
  // This chain's on-chain sales in the history, with a place in the key, and how far past the number of this register's
  // own ones (not reported) an index of it counts
  // The chain of a sale's address: its path after the owner's key («0», the wallet's receive chain, or the path the
  // owner set in the settings)
  const tagOf = (entry) => (typeof entry.path === "string" ? entry.path : null);
  const chainSales = (tag) => ledger().filter((entry) => entry.method === "onchain" && tagOf(entry) === tag
    && fitsIndex(entry.index) && typeof entry.address === "string");
  const ownBound = (sales) => sales.filter((entry) => !entry.reported).length + LEDGER_JUMP;
  const fitsIndex = (value) => Number.isSafeInteger(value) && value >= 0 && value < MAX_INDEX;
  // Finding a new sale's address: the most questions to the explorer, the most indexes looked at, and after how many
  // used addresses in a row the search leaps forward
  const MAX_ASKS = 100;
  const MAX_LOOKS = 400;
  const LEAP_AFTER = 3;
  // A chain restored from a history hands out an address only if these places after it are unused too
  const AHEAD = [1, 2, 3, 5, 10, 20, 50, 100, 200, 500, 1000];
  // Where a restored chain starts: the most history indexes checked against the key, and questions to the explorer
  const MAX_VERIFY = 100;
  const MAX_START_ASKS = 5;
  // The most the confirmation threshold takes, in cents (99,999,999.99: eight digits before the cents)
  const MAX_THRESHOLD = 9999999999;

  // The next index of this register's chain, taken at once: read and written with nothing in between, so that another
  // tab of the same register never gets the same one. A new key, or a pairing with another key or path, or a count
  // that is missing (a restored copy) or of the old kind (a number), starts from `start`. `unsure`: the count does not
  // come from this phone's own sales yet (a restored copy): it stays so, and moves up to `start`, until the explorer has
  // said that one of its addresses is unused (sureIndex). `start` of the count: the number the owner set in the settings
  // (saveSettings), kept with the count of its key; `first` with it until the explorer has said that an address from
  // there is unused (a number typed one too low would otherwise hand out a used address when the explorer does not
  // answer). Returns the index and the count as written, for a compare-and-set rollback
  function claimIndex(of, start = 0, unsure = false) {
    const saved = load(NEXT, null);
    const same = saved?.of === of && fitsIndex(saved.next);
    let next = same ? saved.next : fitsIndex(start) ? start : 0;
    if (same && saved.unsure === true && fitsIndex(start)) next = Math.max(next, start);
    const wrote = { ...(same && fitsIndex(saved.start) ? { start: saved.start } : {}), of, next: next + 1,
      ...(unsure || (same && saved.unsure === true) ? { unsure: true } : {}),
      ...(same && saved.first === true ? { first: true } : {}) };
    store(NEXT, wrote);
    return { index: next, wrote };
  }

  // The count changed only if it is still this key's: to `next` at least, or no longer unsure (nor waiting for the
  // explorer's first answer). Returns the count as written, null if not
  // With `start`, the wait for the first answer ends only for that starting number: one saved meanwhile keeps its own
  function moveCount(of, { next, sure = false, start }) {
    const saved = load(NEXT, null);
    if (saved?.of !== of || !fitsIndex(saved.next)) return null;
    const moved = { ...saved };
    if (fitsIndex(next) && next > saved.next) moved.next = next;
    if (sure) {
      delete moved.unsure;
      if (start === undefined || (saved.start ?? null) === start) delete moved.first;
    }
    store(NEXT, moved);
    return moved;
  }

  // An index taken and not handed out goes back, when no other tab took one after it. Returns the count as written
  function giveBack(of, index) {
    const saved = load(NEXT, null);
    if (saved?.of !== of || saved.next !== index + 1) return null;
    const back = { ...saved, next: index };
    store(NEXT, back);
    return back;
  }

  // The count put back to `before` only if it still holds what this sale last wrote (`wrote`): another tab may have
  // taken an index meanwhile, or the owner saved a starting number, and that stays
  function rollBack(before, wrote) {
    if (wrote && JSON.stringify(load(NEXT, null)) === JSON.stringify(wrote)) store(NEXT, before);
  }

  // A wallet stops looking for payments after 20 unused addresses in a row (the gap limit). On-chain sales never paid
  // (cancelled, expired) use addresses all the same: the owner's register says how many of its chain's latest addresses
  // went unpaid in a row, and above GAP_HINT tells to raise the wallet's gap limit. Counted from this phone's history:
  // the indexes after the last one with a payment (paid, seen, confirmed by hand, a transaction), or after the number the
  // owner set as the start. Never from the explorer's word alone: addresses it said were used and the register skipped
  // may be empty in the wallet (an explorer that is wrong, or lies), and then the wallet needs the larger gap limit
  const GAP_HINT = 15;
  let gapRound = 0;
  async function unpaidRun() {
    const onchain = prefs.onchain;
    if (staff || practice || !onchain?.key) return null;
    const place = await chainPlace(onchain);
    const saved = load(NEXT, null);
    if (saved?.of !== `${onchain.key}/${place.tag}` || !Number.isSafeInteger(saved.next)) return 0;
    const sales = chainSales(place.tag).filter((entry) => entry.index < saved.next).sort((a, b) => b.index - a.index);
    const paid = (entry) => ["paid", "confirmed", "seen"].includes(entry.status) || typeof entry.txid === "string";
    let last = fitsIndex(saved.start) ? saved.start - 1 : -1;
    // The newest paid sale of this key (a sale of an earlier key may have the same index)
    for (const entry of sales.filter(paid).slice(0, 50)) {
      if (entry.index <= last) break;
      if (await derive.addressAt(place.key, [entry.index], data.network) === entry.address) {
        last = entry.index;
        break;
      }
    }
    // With the shop's registers sharing the chain the indexes between this phone's sales may be other registers' sales,
    // which this phone does not see: the run counted here may be longer than the wallet's, never shorter (the safe side)
    return Math.max(0, saved.next - 1 - last);
  }

  async function showGap() {
    if (!$("chain-gap")) return;
    const round = ++gapRound;
    let run = null;
    try {
      run = await unpaidRun();
    } catch { /* a key that does not read: the settings say so */ }
    if (round !== gapRound) return;
    $("chain-gap").hidden = !(run > GAP_HINT);
    $("chain-gap").textContent = run > GAP_HINT ? `${say(words.chain_gap, { count: run })} ${words.chain_gap_hint}` : "";
    if ($("chain-gap-count")) {
      $("chain-gap-count").hidden = run === null;
      $("chain-gap-count").textContent = run === null ? ""
        : [say(words.chain_gap, { count: run }), run > GAP_HINT ? words.chain_gap_hint : ""].filter(Boolean).join(" ");
    }
  }

  // The owner's path for the on-chain addresses (the settings' «Advanced»), the receive chain by default
  const ownerPath = (onchain) => (typeof onchain?.path === "string" ? onchain.path : pairing.DEFAULT_PATH);

  // Where this register's on-chain addresses are: `key` the chain's extended key (the addresses are …/n of it), `tag`
  // its path after the owner's key (what the sales keep). The shop's registers share it through the site's count
  async function chainPlace(onchain) {
    const path = staff ? onchain.path : ownerPath(onchain);
    const steps = pairing.steps(path);
    if (!steps) throw fail(staff ? words.chain_pairing_key : words.chain_no_key);
    let key = onchain.key;
    try {
      // A staff register's key is already the chain's: the owner's phone derived it for the pairing
      if (!staff) for (const step of steps) key = await derive.childKey(key, step, data.network);
    } catch {
      throw fail(staff ? words.chain_pairing_key : words.chain_no_key);
    }
    return { key, tag: path, path };
  }

  // The chain's name for the site: a SHA-256 of its extended key, so that the site keeps a count per chain without
  // knowing the key (with it, it could not derive one address)
  async function chainName(key) {
    const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(`bitmentor-chain:${key}`));
    return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
  }

  // The next index of the shop's chain from the site (shops/chain_count.py), at least `atLeast`. The owner's register
  // asks as the signed-in owner, a staff register with its key
  async function claimShared(place, atLeast) {
    if (backend.chainClaim) return backend.chainClaim(place, atLeast);
    const claim = data.chain_claim;
    if (!claim?.url) throw fail(words.chain_sign_in);
    const body = { chain: await chainName(place.key), at_least: atLeast, ...(claim.key ? { k: claim.key } : {}) };
    let response;
    try {
      response = await fetch(claim.url, { method: "POST", credentials: "same-origin", cache: "no-store", redirect: "error",
        headers: { "Content-Type": "application/json", ...(claim.csrf ? { "X-CSRFToken": claim.csrf } : {}) },
        body: JSON.stringify(body) });
    } catch {
      throw fail(words.chain_claim_down);
    }
    if (response.status === 412) throw fail(words.chain_not_opened);
    if (response.status === 403) throw fail(staff ? words.chain_claim_down : words.chain_sign_in);
    if (response.status === 409) throw fail(staff ? words.chain_far_staff : words.chain_far);
    if (!response.ok) throw fail(words.chain_claim_down);
    const answer = await response.json().catch(() => null);
    if (!fitsIndex(answer?.index) || answer.index < atLeast) throw fail(words.chain_claim_down);
    return answer.index;
  }

  // The address of a new sale. With a key: the next index of the shop's chain (…/0/n of the owner's key, or the path the
  // owner set), skipping an address a sale on this phone already has (shown and not paid yet, or
  // cancelled) and those the explorer says already received something, since this phone's count may be behind (a new
  // phone, a restored copy). With a fixed address: the payments already there, and what they spend, are not this sale's.
  // A count of this phone's own (`counted`) hands out its next address when the explorer does not answer, as always,
  // except a starting number the owner just set (`first`): until the explorer has said one address from there is unused,
  // as a restored chain, it hands out none without that answer (a number typed one too low is a used address). A
  // chain that starts from a history (a restored copy, a count of the old kind) hands out only an address the explorer
  // said is unused, with none used shortly after it (an old sale that expired unpaid looks unused too): otherwise it
  // could give an old customer's address again
  async function chainAddress() {
    const onchain = prefs.onchain;
    if (onchain.address) {
      const txs = await backend.chainTxs(onchain.address);
      if (!chain.outputs(txs, onchain.address)) throw new Error("not an explorer's answer");
      return { address: onchain.address, before: chain.known(txs, onchain.address), spent: chain.spent(txs, onchain.address) };
    }
    const place = await chainPlace(onchain);
    const { tag } = place;
    const of = `${onchain.key}/${tag}`;
    const pathOf = (index) => [index];
    const addressOf = async (index) => {
      try {
        return await derive.addressAt(place.key, pathOf(index), data.network);
      } catch {
        throw fail(staff ? words.chain_pairing_key : words.chain_no_key);
      }
    };
    // An index taken and not handed out goes back on this phone; the site's count never goes back (shops/chain_count.py)
    const release = (index) => giveBack(of, index);
    const saved = load(NEXT, null);
    const counted = saved?.of === of && fitsIndex(saved.next) && saved.unsure !== true;
    // What this sale last wrote to the count, for the rollback
    let wrote = null;

    const taken = new Set(ledger().map((entry) => entry.address).filter((address) => typeof address === "string"));
    let asks = 0;
    // The explorer's answers, by address: a sale's address takes from it what was already there (its `before`)
    const answers = new Map();
    const handOut = async (index) => {
      const address = await addressOf(index);
      const txs = answers.get(address) || [];
      return { address, index, path: place.path, before: chain.known(txs, address), spent: chain.spent(txs, address) };
    };
    // The owner's register uses the wallet's receive chain, whose addresses the wallet may hand out too: one is never
    // handed out without the explorer's answer, else a payment of someone else already in the mempool could close the sale
    // Whether the address at `index` has received anything: «taken» when a sale on this phone has it (no question),
    // else the explorer's answer, null when it gives none
    const usedAt = async (index) => {
      if (!fitsIndex(index)) return "taken";
      const address = await addressOf(index);
      if (taken.has(address)) return "taken";
      if (asks >= MAX_ASKS) throw fail(words.chain_used_up);
      asks += 1;
      try {
        const txs = await backend.chainTxs(address);
        const outputs = chain.outputs(txs, address);
        if (outputs) answers.set(address, txs);
        return outputs ? outputs.length > 0 : null;
      } catch {
        return null;
      }
    };
    // How far this sale may move the chain: LEDGER_JUMP past what this phone knows by itself (its own count, or a sale
    // of its own further on in the history, checked against the key; with no count, the number of its own sales in the
    // history). Only the explorer says the addresses past it were used, and one that says so wrongly (a community's
    // explorer changed, or broken) would send every later payment past the wallet's gap limit, where the wallet does
    // not see it: beyond, no on-chain sale. When the wallet really used them, the owner sets the start in the settings
    let limit = Math.min(counted
      ? Math.max(saved.next, await ownAhead(place.key, tag, pathOf, saved.next) + 1) + LEDGER_JUMP
      : ownBound(chainSales(tag)) + 1, MAX_INDEX - 1);
    // Refused for going too far: the count goes back to where it was before this sale, with none of its leaps (an
    // explorer that lies would otherwise move it a little further at each try). Only the explorer's answers can send
    // a sale this far: with none asked (addresses of sales on this phone), it is the phone's own count that ran out
    const tooFar = () => {
      // A starting number the owner saved meanwhile (in another tab) stays: it is the way out of this refusal
      if (load(NEXT, null)?.start === saved?.start) rollBack(saved, wrote);
      return fail(limit >= MAX_INDEX - 1 || !asks ? words.chain_used_up : staff ? words.chain_far_staff : words.chain_far);
    };
    // From an index known to be used, the first unused one after a used one: in leaps (1, 2, 4, …) until one is unused,
    // then halving the stretch between, no further than `limit`. Some unused addresses (sales that went unpaid) may be
    // left behind: skipping moves no money. When the explorer stops answering, where the search got to
    const leap = async (from) => {
      let low = from;
      let high = null;
      for (let step = 1; high === null; step *= 2) {
        const probe = Math.min(from + step, limit);
        if (probe <= low) throw tooFar();
        const used = await usedAt(probe);
        if (used === null) return low + 1;
        if (used) {
          if (probe >= limit) throw tooFar();
          low = probe;
        } else high = probe;
      }
      while (high - low > 1) {
        const middle = low + Math.floor((high - low) / 2);
        const used = await usedAt(middle);
        if (used === null) return high;
        if (used) low = middle;
        else high = middle;
      }
      return high;
    };
    const start = counted ? 0 : await restoredStart(place.key, tag, pathOf);
    let inRow = 0;
    for (let looked = 0; looked < MAX_LOOKS; looked += 1) {
      const claimed = claimIndex(of, start, !counted);
      let { index } = claimed;
      wrote = claimed.wrote;
      // The shop's chain: the site gives the index, never one it gave another register, and at least this phone's own
      // (no question to the explorer before it: a register that cannot reach the site hands out no address). One further
      // on was taken by the other registers: this phone's count follows, and the bound moves with it
      let given;
      try {
        given = await claimShared(place, index);
      } catch (error) {
        giveBack(of, index);
        throw error;
      }
      if (given !== index) {
        wrote = moveCount(of, { next: given + 1 }) || wrote;
        limit = Math.min(Math.max(limit, given + LEDGER_JUMP), MAX_INDEX - 1);
        index = given;
      }
      // Read again at each claim: the owner may have saved a starting number in another tab meanwhile
      const firstNow = claimed.wrote.first === true;
      if (index > limit) {
        wrote = release(index) || wrote;
        throw tooFar();
      }
      const used = await usedAt(index);
      if (used === null) {
        release(index);
        throw fail(counted ? words.chain_owner_down : words.chain_unchecked);
      }
      if (!used) {
        if (!counted) {
          // Nothing used shortly after it either: a chain restored from a history with months missing may start among
          // old addresses, and one that went unpaid looks unused
          let later = -1;
          for (const ahead of AHEAD.filter((step) => fitsIndex(index + step))) {
            const there = await usedAt(index + ahead);
            if (there === null) {
              release(index);
              throw fail(words.chain_unchecked);
            }
            if (there) later = index + ahead;
          }
          if (later >= 0) {
            const next = await leap(later);
            wrote = moveCount(of, { next }) || wrote;
            inRow = 0;
            continue;
          }
          moveCount(of, { sure: true, start: claimed.wrote.start ?? null });
        } else if (firstNow) moveCount(of, { sure: true, start: claimed.wrote.start ?? null });
        return handOut(index);
      }
      inRow += 1;
      // Many used in a row (a count far behind, a history with months missing): leap forward instead of one by one
      if (inRow >= LEAP_AFTER) {
        const next = await leap(index);
        wrote = moveCount(of, { next }) || wrote;
        inRow = 0;
      }
    }
    throw fail(words.chain_used_up);
  }

  // The highest index of this register's own sales (not reported) of this chain at `from` or after it, checked against
  // the key (a sale of an earlier key may have the same path): the count may be behind the history (a copy of this
  // register imported as its own), and no further than LEDGER_JUMP plus the own sales at `from` or after it: one forged
  // far on would lift the bound with it. -1 if none
  async function ownAhead(key, tag, pathOf, from) {
    const sales = chainSales(tag);
    // Only the own sales at `from` or after it widen the window, one place each: the history below the count is in it
    const bound = from + sales.filter((entry) => !entry.reported && entry.index >= from).length + LEDGER_JUMP;
    const ahead = sales.filter((entry) => !entry.reported && entry.index >= from && entry.index <= bound)
      .sort((a, b) => b.index - a.index);
    for (const entry of ahead.slice(0, MAX_VERIFY)) {
      try {
        if (await derive.addressAt(key, pathOf(entry.index), data.network) === entry.address) return entry.index;
      } catch {
        return -1;
      }
    }
    return -1;
  }

  // Where a chain with no count on this phone starts (a restored copy, a count of the old kind): after the highest index
  // of this key and path in the history, checked against the key (a sale of an earlier key may have the same path).
  // That index is taken as it is when a sale of this register's own has it and it is no more than LEDGER_JUMP past the
  // number of this register's own sales there. One only reported (by another register, or a copy without an author: the
  // old months a lost phone wrote, say) is taken only if the explorer says its address received something: a forged
  // index has nothing. One further on is never taken, whatever the explorer says (it may be wrong, or lie): the next
  // sale would go past the wallet's gap limit. The highest index within the bound that counts is taken instead (an own
  // one, or a reported one the explorer says was paid), and with none 0; chainAddress then asks the explorer about every
  // address it hands out, and leaps no further than the same bound
  async function restoredStart(key, tag, pathOf) {
    const sales = chainSales(tag);
    const bound = ownBound(sales);
    const checked = new Map();
    const verified = [];
    for (const entry of sales.sort((a, b) => b.index - a.index)) {
      if (checked.size >= MAX_VERIFY) break;
      if (!checked.has(entry.index)) {
        try {
          checked.set(entry.index, await derive.addressAt(key, pathOf(entry.index), data.network));
        } catch {
          return 0;
        }
      }
      if (checked.get(entry.index) !== entry.address) continue;
      const last = verified.at(-1);
      if (last?.index === entry.index) last.own ||= !entry.reported;
      else verified.push({ index: entry.index, address: entry.address, own: !entry.reported });
    }
    let asked = 0;
    for (const place of verified) {
      if (place.index > bound) continue;
      if (place.own) return place.index + 1;
      if (asked >= MAX_START_ASKS) continue;
      asked += 1;
      try {
        if ((chain.outputs(await backend.chainTxs(place.address), place.address) || []).length) return place.index + 1;
      } catch { /* the explorer does not answer: not taken */ }
    }
    return 0;
  }

  // Where the money of an on-chain sale goes, written out with the address's place in the key: the owner finds it in
  // the wallet, and an address that is not there is an alarm
  function chainTo(entry) {
    if (entry.index === undefined) return say(words.to_fixed, { address: entry.address });
    if (staff) return say(words.to_staff, { address: entry.address });
    return say(words.to_chain, { path: `${tagOf(entry) ?? 0}/${entry.index}`, address: entry.address });
  }
  const expired = (entry) => (backend.expired ? backend.expired(entry) : now() > entry.expires);

  // --- The ledger on the phone ---

  // The quick start (shops/training.py): until the owner has the training, the register takes payments up to a small total.
  // The sum is made here, from the sales on this phone (the site never sees them): those paid or seen on the chain. A QR
  // left unpaid, or a sale still to check in the wallet, does not use it up. It follows the history, so a restored copy counts too; clearing the browser
  // starts it again, and that is accepted. Only sales in the community's currency count
  const quick = data.quick_start || null;
  const quickLeft = () => (quick ? Math.max(0, quick.cents - ledger()
    .filter((entry) => ["seen", "paid", "confirmed"].includes(entry.status) && entry.currency === quick.currency)
    .reduce((total, entry) => total + (Number.isFinite(entry.cents) ? entry.cents : 0), 0)) : Infinity);
  function showQuick() {
    if (!quick || !$("quick-start")) return;
    const left = quickLeft();
    $("quick-start").hidden = false;
    $("quick-text").textContent = left > 0 ? say(words.quick_left, { left: moneyOf(left), limit: moneyOf(quick.cents) })
      : words.quick_done;
  }

  // Each sale has a UUID: where randomUUID is missing (insecure page, old browser) it is made with getRandomValues
  const uuid = () => crypto.randomUUID?.() || "10000000-1000-4000-8000-100000000000".replace(/[018]/g,
    (c) => (c ^ (crypto.getRandomValues(new Uint8Array(1))[0] & (15 >> (c / 4)))).toString(16));

  const ledger = () => {
    const list = load(LEDGER, []);
    return Array.isArray(list) ? list : [];
  };

  // A staff register keeps the provider's check URL of its finished sales: its copies carry it, and the owner's register
  // uses it to check them (verifyReported)
  const keep = { compact: (entry) => book.compact(entry, Boolean(staff)), trim: (entry) => book.trim(entry, Boolean(staff)) };

  // The whole history stays: a finished sale keeps only what the history needs (register-book.js, compact), a few
  // hundred bytes, so the phone's room for the site holds years of sales
  function record(entry) {
    const list = ledger().filter((item) => item.id !== entry.id);
    list.push(keep.compact(entry));
    if (!store(LEDGER, list)) warn(words.full);
    showLedger();
    showCopies();
    if (!["pending", "seen"].includes(entry.status)) scheduleNostr(false);
  }

  // A sale looked at again after a wait (a provider's or an explorer's answer, in the rounds below), changed from the
  // book as it is now: if meanwhile something else changed its state (a copy brought in, a pull, another look) it is
  // left as it is, and an older read of it is never written back over the newer one. The one exception is a good
  // answer (paid, seen on the chain): it may still move forward a sale that meanwhile ended «not checked» or «expired»,
  // which only mean that nobody knew yet; never one the merchant decided (cancelled, confirmed by hand) or a newer paid
  const UNKNOWN = ["unchecked", "expired"];
  function recordAgain(seen, change, forward = false) {
    const fresh = ledger().find((item) => item.id === seen.id);
    if (!fresh || (fresh.status !== seen.status && !(forward && UNKNOWN.includes(fresh.status)))) return;
    record(change(fresh));
  }

  const STATES = () => ({
    pending: words.pending, paid: words.paid, confirmed: words.confirmed, expired: words.expired,
    cancelled: words.cancelled, unchecked: words.unchecked, seen: words.seen_state });

  // A sale's state in words: one marked as paid by hand on another register (a staff register's, reported) says by whom
  const isVerified = (entry) => Boolean(entry.reported) && entry.verified === true && entry.status === "paid";
  const byOther = (entry) => entry.status === "confirmed" && Boolean(entry.reported);
  const stateOf = (entry, states = STATES()) => (byOther(entry)
    ? (entry.who ? say(words.confirmed_by, { who: entry.who }) : words.confirmed_elsewhere)
    : states[entry.status] || entry.status);
  // A sale's line in the history: when, how much, how it ended and, on another register's sale, which one
  const saleLine = (entry, time, states) => [time, entry.label, Number.isFinite(entry.sats) ? sats(entry.sats) : "",
    stateOf(entry, states), entry.late ? words.late_mark : "", isVerified(entry) ? words.verified_mark : "",
    byOther(entry) ? "" : entry.who].filter(Boolean).join(" · ");
  const methodName = (method) => (method === "onchain" ? words.method_onchain : "Lightning");

  function showLedger() {
    showGap();
    showQuick();
    const list = ledger();
    const log = $("ledger");
    log.hidden = !list.length;
    if (!list.length) return;
    log.querySelector("summary").textContent = say(words.log, { count: list.length });
    const states = STATES();
    log.querySelector("ul").replaceChildren(...list.slice().reverse().slice(0, 30).map((entry) => {
      const item = document.createElement("li");
      item.textContent = saleLine(entry, new Date(entry.at).toLocaleString(lang), states);
      return item;
    }));
  }

  // Sales left waiting (page closed, phone off) are checked again on opening; the one that was on the screen comes back
  async function recheck() {
    await recheckChain();
    for (const entry of ledger().filter((item) => item.status === "pending" && item.method !== "onchain")) {
      if (entry.id === load(OPEN, null) && now() < entry.expires) continue;
      if (!entry.verify) {
        if (now() > entry.expires) recordAgain(entry, (fresh) => ({ ...fresh, status: "unchecked" }));
        continue;
      }
      try {
        if (await backend.settled(entry)) recordAgain(entry, (fresh) => ({ ...fresh, status: "paid" }), true);
        else if (expired(entry)) recordAgain(entry, (fresh) => ({ ...fresh, status: "expired" }));
      } catch {
        // No good answer: once the invoice is over it may have been paid, so it is to check in the wallet; before, again
        // at the next opening
        if (expired(entry)) recordAgain(entry, (fresh) => ({ ...fresh, status: "unchecked" }));
      }
    }
  }

  // Cancelled sales a customer may still have paid (book.stillOpen): a customer who pays after Cancel has paid all the
  // same. Such a sale becomes «paid», marked as paid after it was cancelled, for the merchant to settle with the
  // customer. A Lightning one keeps what the check needs until the provider answers, after its invoice is over, that it
  // was not paid (a look that fails at the end is not the last one), or a day after its end. A Lightning sale that ended
  // «not checked» (the provider did not answer when its invoice ended) is looked at the same way: paid, it becomes paid;
  // not paid, expired; a day after its end, only the history, still to check in the wallet. At opening and every minute
  let rechecking = false;
  async function recheckCancelled() {
    for (const entry of ledger().filter((item) => ["cancelled", "unchecked"].includes(item.status) && item.method !== "onchain"
      && item.verify && !item.reported)) {
      const cancelled = entry.status === "cancelled";
      let paid;
      try {
        paid = await backend.settled(entry);
      } catch { /* not now: again at the next round */ }
      if (paid === true) recordAgain(entry, (fresh) => ({ ...fresh, status: "paid", ...(cancelled ? { late: true } : {}) }));
      else if (paid === false && expired(entry)) {
        recordAgain(entry, (fresh) => keep.trim(cancelled ? fresh : { ...fresh, status: "expired" }));
      } else if (!book.stillOpen(entry, now())) recordAgain(entry, keep.trim);
    }
  }

  // When each on-chain sale cancelled more than a day ago was last looked at: those are looked at every ten minutes
  const CHAIN_LATE_EVERY = 10 * 60000;
  const lookedLate = new Map();

  // On-chain sales left waiting, or seen in the mempool and waiting for their confirmation: at opening and every minute.
  // A day after the sale, one the explorer never saw is over, and one it could not tell, or with less than asked, is to
  // check in the wallet. One seen in the mempool may wait days for its confirmation: after its day it is looked at every
  // ten minutes, and only after its week, without a confirmation, is it to check in the wallet; an explorer that does not
  // answer never ends it before. A cancelled one with an address of its own is looked at for its day too
  async function recheckChain() {
    if (rechecking) return;
    rechecking = true;
    try {
      await recheckCancelled();
      for (const entry of ledger().filter((item) => item.method === "onchain" && item.status === "cancelled"
        && Number.isFinite(item.expires))) {
        const open = book.stillOpen(entry, now());
        // A fixed address is not looked at after Cancel: another payment to it could be taken for this sale's
        if (!Number.isSafeInteger(entry.index)) {
          recordAgain(entry, keep.trim);
          continue;
        }
        if (open && now() > entry.expires && now() - (lookedLate.get(entry.id) || 0) < CHAIN_LATE_EVERY) continue;
        lookedLate.set(entry.id, now());
        // Past its week: one last look, then only the history
        try {
          const result = await chainState(entry);
          if (result.state === "paid") {
            recordAgain(entry, (fresh) => ({ ...fresh, status: "paid", late: true, txid: result.txids[0] || fresh.txid }));
            continue;
          }
        } catch { /* not now: again at the next round, for the sale's week */ }
        if (!open) recordAgain(entry, keep.trim);
      }
      for (const entry of ledger().filter((item) => item.method === "onchain" && ["pending", "seen"].includes(item.status))) {
        if (entry.id === sale?.id) continue;
        const seen = entry.status === "seen";
        if (seen && expired(entry)) {
          if (now() - (lookedLate.get(entry.id) || 0) < CHAIN_LATE_EVERY) continue;
          lookedLate.set(entry.id, now());
        }
        const over = seen ? now() > entry.expires + book.LATE_CHAIN : expired(entry);
        const unchecked = (fresh) => ({ ...fresh, status: "unchecked" });
        try {
          const result = await chainState(entry);
          if (result.state === "paid") recordAgain(entry, (fresh) => ({ ...fresh, status: "paid", txid: result.txids[0] || fresh.txid }), true);
          else if (result.state === "seen" && !seen) {
            recordAgain(entry, (fresh) => ({ ...fresh, status: "seen", txid: result.txids[0] }), true);
          } else if (result.state === "none" && !seen && over) recordAgain(entry, (fresh) => ({ ...fresh, status: "expired" }));
          else if (over) recordAgain(entry, unchecked);
        } catch {
          // With on-chain off nobody is asked, now or later: saying "waiting, the register looks again" would not be
          // true. The sale goes at once to "not checked: look in your wallet"
          if (over || !CHAIN_ON) recordAgain(entry, unchecked);
        }
      }
    } finally {
      rechecking = false;
    }
  }

  // --- The history: the sales of this phone, the day's closing and the copies ---
  // All of it stays on the phone. The site never sees it: the merchant takes it away as a CSV for the accountant or as
  // an encrypted copy (register-book.js) to bring back on another phone

  const COPY = `register-copy:${text.ledger}`;
  const CLOSINGS = `register-closings:${text.ledger}`;
  const WARNED = `register-warned:${text.ledger}`;
  const EVERY = ["manual", "daily", "weekly", "monthly", "closing"];
  const MIN_PASSPHRASE = 10;
  const MAX_COPY = 20 * 1024 * 1024;

  // The merchant's reminder (weekly until they choose another) and the moment of the last encrypted copy (0: never)
  function copies() {
    const saved = load(COPY, {});
    return { every: EVERY.includes(saved?.every) ? saved.every : "weekly", last: Number.isFinite(saved?.last) ? saved.last : 0 };
  }

  // When each business day was closed: {"YYYY-MM-DD": moment}
  function closings() {
    const saved = load(CLOSINGS, {});
    return saved && typeof saved === "object" && !Array.isArray(saved) ? saved : {};
  }

  function copyDue() {
    const { every, last } = copies();
    const unsaved = ledger().reduce((oldest, entry) => (entry.at > last ? Math.min(oldest, entry.at) : oldest), Infinity);
    return book.backupDue(every, last, Number.isFinite(unsaved) ? unsaved : 0);
  }

  const when = (at) => new Date(at).toLocaleString(lang, { dateStyle: "medium", timeStyle: "short" });
  const dayName = (day) => new Date(`${day}T12:00:00`).toLocaleDateString(lang,
    { weekday: "short", day: "numeric", month: "short", year: "numeric" });
  const monthName = (month) => new Date(`${month}-15T12:00:00`).toLocaleDateString(lang, { month: "long", year: "numeric" });

  function amount(cents, currency) {
    try {
      return (cents / 100).toLocaleString(lang, { style: "currency", currency });
    } catch {
      return `${(cents / 100).toFixed(2)} ${currency}`;
    }
  }

  function line(content) {
    const item = document.createElement("li");
    item.textContent = content;
    return item;
  }

  function tellHistory(message) {
    $("history-say").textContent = message;
  }

  // A form that cannot go on: the field to put right is marked, takes the focus and is described by the message, so a
  // screen reader reads it at once with the field; the next try starts clean
  function flag(id, said) {
    const field = $(id);
    // A field inside a closed section (Advanced) is opened first, to be seen and reached
    const section = field.closest("details");
    if (section) section.open = true;
    field.setAttribute("aria-invalid", "true");
    field.setAttribute("aria-describedby", said);
    field.focus();
  }

  function unflag(id) {
    for (const field of $(id).querySelectorAll("[aria-invalid]")) {
      field.removeAttribute("aria-invalid");
      field.removeAttribute("aria-describedby");
    }
  }

  // The date of the last copy, and the reminder when it is older than the merchant chose: in the history and on the keypad
  function showCopies() {
    const { every, last } = copies();
    const due = copyDue();
    const state = $("copy-state");
    state.textContent = [last ? say(words.last_copy, { date: when(last) }) : words.no_copy, due ? words.copy_due : ""]
      .filter(Boolean).join(" ");
    state.classList.toggle("copy-due", due);
    $("copy-reminder").hidden = !due;
    $("copy-every").value = every;
  }

  function showDay(day) {
    const totals = book.dayTotals(ledger(), day);
    const states = STATES();
    const closed = closings()[day];
    $("day-closed").textContent = Number.isFinite(closed) ? say(words.closed, { time: when(closed) }) : "";
    const lines = totals.lines.map((total) => line([say(words.day_line, {
      method: methodName(total.method), count: total.count,
      amount: amount(total.cents, total.currency), sats: sats(total.sats) }),
    total.byHand ? say(words.by_hand, { count: total.byHand }) : "",
    total.verified ? say(words.verified_count, { count: total.verified }) : "",
    total.reported ? say(words.reported_count, { count: total.reported }) : "",
    total.late ? say(words.late_count, { count: total.late }) : ""].filter(Boolean).join(" ")));
    if (!lines.length) lines.push(line(words.no_paid));
    const { unchecked = 0, expired: gone = 0, cancelled = 0, pending = 0, seen = 0 } = totals.counts;
    if (pending + seen) lines.push(line(say(words.waiting_count, { count: pending + seen })));
    if (unchecked) lines.push(line(say(words.unchecked_count, { count: unchecked })));
    if (gone + cancelled) lines.push(line(say(words.not_paid, { count: gone + cancelled })));
    $("day-totals").replaceChildren(...lines);
    $("day-sales").replaceChildren(...totals.sales.slice().reverse().map((entry) => line(
      saleLine(entry, new Date(entry.at).toLocaleTimeString(lang, { timeStyle: "short" }), states))));
    $("day-sales").closest("details").hidden = !totals.sales.length;
  }

  function showHistory() {
    const list = ledger();
    const today = book.businessDay(Date.now());
    const dayList = book.days(list);
    if (!dayList.includes(today)) dayList.unshift(today);
    const days = $("day");
    const chosen = dayList.includes(days.value) ? days.value : today;
    days.replaceChildren(...dayList.map((day) => new Option(dayName(day), day, false, day === chosen)));
    const monthList = $("csv-month");
    const kept = monthList.value;
    monthList.replaceChildren(monthList.options[0],
      ...book.months(list).map((month) => new Option(monthName(month), month, false, month === kept)));
    showDay(chosen);
    showCopies();
  }

  // Asks the browser not to clear the site's data when it needs room. Only after a tap (Understood, the history): some
  // browsers ask the merchant, and a question nobody asked for at every opening would be noise
  async function keepData(ask) {
    let kept = null;
    try {
      kept = await navigator.storage.persisted();
      if (!kept && ask) kept = await navigator.storage.persist();
    } catch { /* an old browser says nothing: the note stays hidden */ }
    $("not-kept").hidden = kept !== false;
  }

  function openHistory() {
    tellHistory("");
    showHistory();
    show("history");
    keepData(true);
    autoPull();
  }

  // A file made here and handed to the browser: nothing leaves the phone except into the merchant's downloads
  function download(name, type, content) {
    const link = document.createElement("a");
    link.href = URL.createObjectURL(new Blob([content], { type }));
    link.download = name;
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(link.href), 60000);
  }

  // The practice's files say so in their name: its pretend sales are never taken for real ones
  const fileName = (part, extension) => `${text.file}${practice ? "-practice" : ""}-${part}.${extension}`;

  // Whether a register's ledger is the practice's (register-book.js)
  const { ofPractice } = book;

  function csvDownload(event) {
    event.preventDefault();
    const month = $("csv-month").value;
    download(fileName(month || "all", "csv"), "text/csv;charset=utf-8",
      book.csv(ledger(), { lang, columns: words.csv_columns, states: STATES(), hows: words.hows, month, late: words.late_mark,
        verified: words.verified_mark,
        label: (entry) => stateOf(entry) }));
  }

  async function copyDownload(event) {
    event.preventDefault();
    const form = event.target;
    const passphrase = $("export-pass").value;
    unflag("export-form");
    if (passphrase.length < MIN_PASSPHRASE) {
      tellHistory(say(words.short, { count: MIN_PASSPHRASE }));
      flag("export-pass", "history-say");
      return;
    }
    if (passphrase !== $("export-again").value) {
      tellHistory(words.mismatch);
      flag("export-again", "history-say");
      return;
    }
    tellHistory(words.busy);
    const now = Date.now();
    let envelope;
    try {
      envelope = await book.seal({ sales: ledger(), closings: closings(), till: text.ledger, who: text.tillName || "",
        exported: now }, passphrase);
    } catch {
      tellHistory(words.no_crypto);
      return;
    }
    download(fileName(book.businessDay(now), "json"), "application/json", JSON.stringify(envelope));
    store(COPY, { ...copies(), last: now });
    form.reset();
    showCopies();
    tellHistory(words.saved);
  }

  // A copy brought back: this phone's own (a new phone) or a staff register's (the end of a shift). Each sale once; the
  // day closings come back only with this register's own copy
  async function copyUpload(event) {
    event.preventDefault();
    const form = event.target;
    const file = $("import-file").files[0];
    if (!file) return;
    unflag("import-form");
    tellHistory(words.busy);
    let payload;
    try {
      if (file.size > MAX_COPY) throw new Error("size");
      payload = await book.open(JSON.parse(await file.text()), $("import-pass").value);
    } catch (error) {
      tellHistory({ passphrase: words.wrong, version: words.newer }[error.reason] || words.not_copy);
      flag(error.reason === "passphrase" ? "import-pass" : "import-file", "history-say");
      return;
    }
    if (!Array.isArray(payload?.sales)) {
      tellHistory(words.not_copy);
      flag("import-file", "history-say");
      return;
    }
    // The practice's pretend sales never come into a real register, nor a real register's into the practice
    if (ofPractice(payload.till) !== practice) {
      tellHistory(practice ? words.real_copy : words.practice_copy);
      flag("import-file", "history-say");
      return;
    }
    if (!practice) payload.sales = payload.sales.filter((entry) => !ofPractice(entry?.till));
    // A staff register's sales stay «reported» by it, and never become «paid» over one confirmed here by hand. The
    // on-chain ones are checked against the owner's key (foreignSales): an address that is not the key's is named, but
    // a changed staff page could also record the right address while showing the customer another one. Only the
    // wallet tells that: a reported payment that is not there
    // Another register's copy speaks only for that register: a sale it says is this register's own stays out.
    // The file has no author: anyone who has the passphrase of a staff register's copy could write one that says it is
    // this register's. Its sales are this register's own (not reported) only on a phone with no sales yet (a new phone
    // bringing its history back) or when the merchant ticks that it is a copy of this register; otherwise they come in
    // as reported, to check in the wallet, and so do its day closings stay out
    const fromOther = typeof payload.till === "string" && payload.till !== text.ledger;
    const ownCopy = !fromOther && (!ledger().length || Boolean($("import-own")?.checked));
    const result = book.merge(ledger(), payload.sales, fromOther ? { own: text.ledger, from: payload.till }
      : { own: text.ledger, trusted: ownCopy });
    if (!store(LEDGER, result.list)) {
      tellHistory(words.full);
      return;
    }
    if (ownCopy && payload.till === text.ledger && payload.closings && typeof payload.closings === "object") {
      const mine = closings();
      for (const [day, at] of Object.entries(payload.closings)) {
        if (/^\d{4}-\d{2}-\d{2}$/.test(day) && Number.isFinite(at) && !(mine[day] >= at)) mine[day] = at;
      }
      store(CLOSINGS, mine);
    }
    form.reset();
    showLedger();
    showHistory();
    scheduleNostr(false);
    verifyReported();
    let imported = say(words.imported, { added: result.added, updated: result.updated });
    if (!fromOther && !ownCopy && result.added + result.updated) imported = `${imported} ${words.imported_unsure}`;
    // This register's own sales the copy would change, left alone: the box was not ticked
    if (!fromOther && !ownCopy && result.refused) imported = `${imported} ${say(words.imported_refused, { count: result.refused })}`;
    tellHistory(imported);
    // Only the sales the merge did not refuse: one left out moved nothing here. This register's own copy, trusted: its
    // on-chain sales of an earlier key are not an alarm
    const foreign = await foreignSales(stored(result.accepted, result.list)
      .filter((entry) => !(ownCopy && entry?.till === text.ledger)));
    if (foreign.length) imported = `${imported} ${foreignSaid(foreign)}`;
    // Another register's on-chain sales: the right address in the copy does not prove the customer was shown it
    if (payload.till !== text.ledger && result.accepted.some((entry) => entry?.method === "onchain")) {
      imported = `${imported} ${words.imported_check}`;
    }
    tellHistory(imported);
  }

  // The on-chain sales of an imported copy (a staff register's, at the end of the shift) whose address is not the one the
  // owner's key gives for its place (path and index), or not the owner's fixed address: a staff register whose page
  // was changed may show here, not always (it can write the right address and show another QR code: only the wallet
  // catches that). Only on the owner's register, with the destination on this phone, and only the sales said to be paid
  // (or seen in the mempool) or that may still be: one still waiting or not checked when the copy was made (the customer
  // of a changed page may have paid it just then). One that ended unpaid (expired, cancelled) moved no money
  const MAYBE_PAID = ["paid", "confirmed", "seen", "unchecked", "pending"];
  // The sales a merge accepted as the book now has them: an update keeps what this phone had (the address, say), so a
  // copy that first brings a sale ended unpaid with one address, then paid with another, is checked on the first one
  // Each sale once: a copy that brings the same sale twice names it once
  const stored = (accepted, list) => {
    const byId = new Map(list.map((entry) => [entry.id, entry]));
    const seen = new Set();
    return accepted.filter((entry) => {
      if (seen.has(entry?.id)) return false;
      seen.add(entry?.id);
      return true;
    }).map((entry) => byId.get(entry?.id) || entry);
  };
  const foreignSaid = (foreign) => say(words.imported_foreign, { count: foreign.length,
    times: foreign.slice(0, 10).map((entry) => `${when(entry.at)} ${entry.label || ""}`.trim()).join(", ") });
  async function foreignSales(sales) {
    const mine = staff || practice ? null : ownerSettings().onchain;
    if (!mine || !Array.isArray(sales)) return [];
    const found = [];
    for (const entry of sales.filter((item) => book.valid(item) && item.method === "onchain"
      && MAYBE_PAID.includes(item.status)).slice(0, 5000)) {
      let expected = null;
      try {
        if (mine.address) expected = mine.address;
        else if (pairing.steps(tagOf(entry)) && Number.isSafeInteger(entry.index)) {
          expected = await derive.addressAt(mine.key, [...pairing.steps(tagOf(entry)), entry.index], data.network);
        }
      } catch { /* nothing to compare with: not the owner's */ }
      if (typeof entry.address !== "string" || expected !== entry.address) found.push(entry);
    }
    return found;
  }

  function closeDay() {
    const day = $("day").value;
    const now = Date.now();
    store(CLOSINGS, { ...closings(), [day]: now });
    scheduleNostr(true);
    showDay(day);
    showCopies();
    // The merchant who chose a copy at every closing gets the form ready
    if (copies().every === "closing") {
      tellHistory(words.copy_now);
      $("export-pass").focus();
    } else {
      tellHistory(say(words.closed, { time: when(now) }));
    }
  }

  // --- The copies on Nostr ---
  // The owner's register rewrites the event of the month, a few minutes after a sale and at the closing; a staff
  // register the event of the day, a few seconds after a sale, so that the owner sees it. Both encrypt to the owner's
  // public key and sign with the register's own key: the owner's register makes one here, a staff register has the one
  // the owner's phone made for it, in the pairing. The owner's register reads the staff registers' with the conversation key its phone kept at pairing, and its own with the owner's private key,
  // kept in this page only

  const NOSTR_KEY = `register-nostr-key:${text.ledger}`;
  const NOSTR_SENT = `register-nostr-sent:${text.ledger}`;
  const NOSTR_PULLED = `register-nostr-pulled:${text.ledger}`;
  const TILLS = `register-nostr-tills:${text.shop}`;
  // The conversation keys of those registers with the owner's, made when each code was drawn (static/staff-pair.js)
  const TILL_KEYS = `register-nostr-tillkeys:${text.shop}`;
  const OWNER_DELAY = 5 * 60000;
  const STAFF_DELAY = 20000;
  const PULL_EVERY = 60000;
  // The pulls every minute ask for what changed since the last one less this margin (seconds): a staff register's clock
  // may be behind, or its event reach a relay late. An event dated more than FUTURE_LIMIT ahead is not taken
  const PULL_MARGIN = 2 * 86400;
  const FUTURE_LIMIT = 86400;
  const MAX_PERIODS = 24;
  // The most sales the owner takes at once from copies of keys this phone does not know (takeStrangers)
  const MAX_STRANGER_SALES = 5000;
  let nostrTimer = 0;
  let nostrBusy = false;
  let nostrFailed = false;
  // The owner's private key: only in this page, for as long as it is open
  let ownerSecret = "";
  let pulling = 0;
  // How many staff registers' copies the last pull could not open: their keys are not on this phone
  let needKeys = 0;

  const publicRelays = () => (Array.isArray(data.public_relays) ? data.public_relays : []);

  // Where and how this register keeps its copy, or null: no relays in community.yml, no owner's key, the practice
  function nostrSetup() {
    if (practice || !nostr || !relayNet || !Array.isArray(data.relays) || !data.relays.length) return null;
    const choose = (list) => [...new Set([...data.relays, ...(Array.isArray(list) ? list : [])
      .filter((url) => publicRelays().includes(url))])];
    if (staff) {
      const copy = paired?.nostr;
      return copy ? { owner: copy.owner, secret: copy.secret, relays: choose(copy.relays), period: book.businessDay } : null;
    }
    const chosen = ownerSettings().nostr;
    if (typeof chosen?.owner !== "string") return null;
    let secret = load(NOSTR_KEY, "");
    if (typeof secret !== "string" || !/^[0-9a-f]{64}$/.test(secret)) {
      secret = nostr.newSecret();
      if (!store(NOSTR_KEY, secret)) return null;
    }
    return { owner: chosen.owner, secret, relays: choose(chosen.relays), period: book.monthOf };
  }

  // What a period's copy holds, in short: the copy is sent again only when it changes
  function fingerprint(sales) {
    let hash = 0x811c9dc5;
    for (const char of JSON.stringify(sales.map((entry) => [entry.id, entry.status, entry.txid || "", entry.verify ? 1 : 0]))) {
      hash = Math.imul(hash ^ char.charCodeAt(0), 0x01000193) >>> 0;
    }
    return `${sales.length}.${hash.toString(16)}`;
  }

  function nostrSent() {
    const saved = load(NOSTR_SENT, {});
    return saved && typeof saved === "object" && !Array.isArray(saved) ? saved : {};
  }

  function showNostr(message) {
    if (!$("nostr")) return;
    const setup = nostrSetup();
    $("nostr").hidden = !setup;
    if (!setup) return;
    const sent = nostrSent();
    $("nostr-state").textContent = message || [Number.isFinite(sent.at)
      ? say(words.nostr_sent, { date: when(sent.at), count: sent.relays || 0 }) : words.nostr_never,
    nostrFailed ? words.nostr_failed : "", needKeys ? say(words.nostr_need_key, { count: needKeys }) : ""]
      .filter(Boolean).join(" ");
  }

  function scheduleNostr(now) {
    if (!nostrSetup()) return;
    clearTimeout(nostrTimer);
    nostrTimer = setTimeout(publishCopies, now ? 0 : staff ? STAFF_DELAY : OWNER_DELAY);
  }

  // Sends the periods whose sales changed since their last copy, the newest first: a staff register's days, the owner's
  // months. A period is sent when at least one relay took every event of it
  async function publishCopies() {
    const setup = nostrSetup();
    if (!setup || nostrBusy) return;
    nostrBusy = true;
    nostrFailed = false;
    try {
      const finished = ledger().filter((entry) => !["pending", "seen"].includes(entry.status));
      const periods = [...new Set(finished.map((entry) => setup.period(entry.at)))].sort().reverse().slice(0, MAX_PERIODS);
      const till = await nostr.tillTag(text.ledger, setup.secret, setup.owner);
      let sent = nostrSent();
      for (const period of periods) {
        const sales = finished.filter((entry) => setup.period(entry.at) === period).map(keep.trim);
        const mark = fingerprint(sales);
        if (sent.periods?.[period] === mark) continue;
        // A replaceable event is replaced only by a newer one
        const createdAt = Math.max(Math.floor(Date.now() / 1000), (Number(sent.created) || 0) + 1);
        const events = await nostr.copyEvents({ sales, till, who: text.tillName || "", period, owner: setup.owner,
          secret: setup.secret, createdAt });
        const result = await relayNet.publish(setup.relays, events);
        const took = Object.values(result).filter((count) => count === events.length).length;
        if (!took) {
          nostrFailed = true;
          break;
        }
        sent = { ...sent, created: createdAt, at: Date.now(), relays: took,
          periods: { ...(sent.periods || {}), [period]: mark } };
        store(NOSTR_SENT, sent);
      }
    } catch {
      nostrFailed = true;
    } finally {
      nostrBusy = false;
      showNostr();
    }
  }

  // The staff registers turned off for good on the site (gone_tills, only on the owner's own page) lose their keys
  // here: their copies are of unknown keys from now on. `all`: the owner asked to forget every staff register's key
  function forgetTills(all) {
    const gone = Array.isArray(data.gone_tills) ? data.gone_tills : [];
    const tills = load(TILLS, null);
    if (!tills || typeof tills !== "object" || Array.isArray(tills)) return;
    const kept = all ? {} : Object.fromEntries(Object.entries(tills).filter(([register]) => !gone.includes(register)));
    if (Object.keys(kept).length === Object.keys(tills).length) return;
    store(TILLS, kept);
    // Their conversation keys go with them
    const listed = new Set(Object.values(kept).flat());
    const keys = load(TILL_KEYS, {});
    if (keys && typeof keys === "object" && !Array.isArray(keys)) {
      store(TILL_KEYS, Object.fromEntries(Object.entries(keys).filter(([key]) => listed.has(key))));
    }
  }

  // The keys this phone made for the shop's staff registers (static/staff-pair.js), each with the one register it speaks
  // for: {public key: "<shop>:<register>"}, the ledger of that register
  function tillKeys() {
    const tills = load(TILLS, {});
    const pinned = new Map();
    if (!tills || typeof tills !== "object" || Array.isArray(tills)) return pinned;
    for (const [register, keys] of Object.entries(tills)) {
      for (const key of Array.isArray(keys) ? keys : []) {
        if (typeof key === "string" && !pinned.has(key)) pinned.set(key, `${text.shop}:${register}`);
      }
    }
    return pinned;
  }

  // What this phone holds to read each staff register's copies without the owner's private key: {public key: conversation
  // key (bytes)}. Only for a key still listed in tillKeys, and made for this owner's key: a code drawn before the owner
  // changed the public key says nothing. A key without one (a code drawn before this phone kept them, a forgotten key,
  // another phone) is read with the owner's private key instead
  function tillConversations(owner) {
    const keys = load(TILL_KEYS, {});
    const found = new Map();
    if (!keys || typeof keys !== "object" || Array.isArray(keys)) return found;
    const listed = tillKeys();
    for (const [key, held] of Object.entries(keys)) {
      if (!listed.has(key) || held?.owner !== owner || typeof held.key !== "string" || !/^[0-9a-f]{64}$/.test(held.key)) continue;
      found.set(key, nostr.fromHex(held.key, 32));
    }
    return found;
  }

  // The copies encrypted to the owner, from every relay, into this phone's book: the owner's own and the staff
  // registers'. Only copies of this register's key and of the keys this phone made for staff registers come in. A copy
  // of any other key (anyone who knows the owner's npub can write one) stays out of the book, only counted: on a full
  // pull, asked by the owner, those that open are kept in `strangers` for the owner to take or not (pullNow). `full`
  // asks for everything, otherwise what changed since the last time, with two days of margin
  async function pullCopies(full) {
    const setup = nostrSetup();
    if (!setup || staff || (full && !ownerSecret)) return null;
    const conversations = tillConversations(setup.owner);
    if (!ownerSecret && !tillKeys().size) return null;
    const since = full ? 0 : Math.max(0, (Number(load(NOSTR_PULLED, 0)) || 0) - PULL_MARGIN);
    const started = Math.floor(Date.now() / 1000);
    const answer = await relayNet.request(setup.relays, nostr.filter(setup.owner, since));
    if (!answer.answered) return { failed: true };
    const mine = nostr.publicKey(setup.secret);
    const pinned = tillKeys();
    // The copies taken, merged only after the last wait: a sale recorded while the copies open (some milliseconds
    // each) is in the book read then, and not written over by an older read
    const taken = [];
    let refused = 0;
    let unknown = 0;
    // Copies of a staff register's key this phone cannot read without the owner's private key, and of this register's own
    let needKey = 0;
    const strangers = [];
    for (const event of answer.events) {
      // An event from the future (a phone's clock far ahead, or made so) would be asked again at every pull
      if (!(Number(event?.created_at) <= started + FUTURE_LIMIT)) {
        refused += 1;
        continue;
      }
      const ledgerOf = event?.pubkey === mine ? text.ledger : pinned.get(event?.pubkey);
      if (!ledgerOf) {
        unknown += 1;
        if (!full || !ownerSecret) continue;
      }
      // A staff register's copy opens with the conversation key this phone made with it, or the owner's private key;
      // this register's own copy (a new phone) only with the owner's private key
      const conversation = ledgerOf && event.pubkey !== mine && !ownerSecret ? conversations.get(event.pubkey) : null;
      if (!ownerSecret && !conversation) {
        if (ledgerOf && event.pubkey !== mine) needKey += 1;
        continue;
      }
      let copy;
      try {
        copy = await nostr.openCopy(event, ownerSecret, event?.pubkey, conversation);
      } catch {
        refused += 1;
        continue;
      }
      if (!ledgerOf) {
        strangers.push(copy);
        continue;
      }
      // A copy of a known key names the register it is of in its d tag: the one that key is for, or it is not taken
      if (copy.till !== await nostr.tillTag(ledgerOf, ownerSecret, copy.author, conversation)) {
        refused += 1;
        continue;
      }
      // Only this register's own key writes this register's own sales; a staff register's key speaks only for that
      // register, whatever its copy says
      taken.push([copy.sales, copy.author === mine ? { own: text.ledger } : { own: text.ledger, from: ledgerOf }]);
    }
    let list = ledger();
    let added = 0;
    let updated = 0;
    // A staff register's on-chain sales that came in or changed: compared with the owner's key, as from a file
    const was = new Map(list.map((entry) => [entry.id, entry.status]));
    const staffSales = [];
    for (const [sales, how] of taken) {
      const result = book.merge(list, sales, how);
      ({ list } = result);
      added += result.added;
      updated += result.updated;
      if (result.refused) refused += 1;
      if (how.from !== undefined) {
        const now = new Map(list.map((entry) => [entry.id, entry.status]));
        staffSales.push(...result.accepted.filter((entry) => was.get(entry.id) !== now.get(entry.id)));
      }
    }
    if ((added || updated) && !store(LEDGER, list)) return { full: true };
    store(NOSTR_PULLED, started);
    if (added || updated) changedBook();
    const foreign = await foreignSales(stored(staffSales, list));
    return { added, updated, refused, unknown, strangers, foreign, needKey };
  }

  function changedBook() {
    showLedger();
    if (!$("history").hidden) showHistory();
    scheduleNostr(false);
    verifyReported();
  }

  // The staff registers' Lightning sales, checked here. A sale a staff register reported as paid, with the provider's
  // check URL (LUD-21) on the shop's own provider, is asked about there: when the provider says it is settled, with the
  // invoice of this sale's hash and amount, it is marked `verified` and no longer counts as to check in the wallet. A
  // copy alone never does it. Left reported: marked as paid by hand, without a check URL, a hash another sale in the book
  // has too (one payment cannot confirm two sales), an answer that does not match. Called when sales come in (a pull, an
  // imported file) and every minute; the book remembers the result (`verified`). At most VERIFY_BATCH sales at a time,
  // one request after the other; a sale that was not confirmed (not settled, no answer, mismatch) waits VERIFY_RETRY
  // before it is asked again, and is left alone after VERIFY_TRIES tries until the page is opened again
  const VERIFY_BATCH = 8;
  const VERIFY_RETRY = 10 * 60000;
  const VERIFY_TRIES = 3;
  const verifyTried = new Map();
  let verifying = false;
  async function verifyReported() {
    if (verifying || staff || practice) return;
    verifying = true;
    try {
      const list = ledger();
      const hashes = new Map();
      for (const entry of list) {
        if (typeof entry.hash === "string" && entry.hash) hashes.set(entry.hash.toLowerCase(), (hashes.get(entry.hash.toLowerCase()) || 0) + 1);
      }
      const due = (entry) => {
        const tried = verifyTried.get(entry.id);
        return !tried || (tried.tries < VERIFY_TRIES && Date.now() - tried.at >= VERIFY_RETRY);
      };
      const todo = list.filter((entry) => entry.reported && entry.verified !== true && entry.status === "paid"
        && (entry.method || "lightning") === "lightning" && typeof entry.verify === "string" && trusted(entry.verify)
        && /^[0-9a-f]{64}$/i.test(entry.hash || "") && hashes.get(entry.hash.toLowerCase()) === 1 && due(entry))
        .slice(0, VERIFY_BATCH);
      let changed = false;
      for (const entry of todo) {
        const tried = verifyTried.get(entry.id) || { tries: 0 };
        verifyTried.set(entry.id, { tries: tried.tries + 1, at: Date.now() });
        let paid = false;
        try {
          paid = await settledReported(entry);
        } catch { /* stays reported */ }
        if (paid !== true) continue;
        // Only if the sale is still what was checked: a staff register's, paid, with the same hash and URL
        const fresh = ledger().find((item) => item.id === entry.id);
        if (!fresh || !fresh.reported || fresh.status !== "paid" || fresh.hash !== entry.hash || fresh.verify !== entry.verify) continue;
        if (store(LEDGER, ledger().map((item) => (item.id === entry.id ? { ...item, verified: true } : item)))) changed = true;
      }
      if (changed) {
        showLedger();
        if (!$("history").hidden) showHistory();
      }
    } finally {
      verifying = false;
    }
  }

  // The copies of keys this phone does not know, taken only when the owner says so: after losing the phone, the owner's
  // own old months are among them (their key was on the old phone), but so may be anything written by whoever knows the
  // owner's npub. All their sales are «reported», the newest copies first, at most MAX_STRANGER_SALES sales
  function takeStrangers(strangers) {
    let list = ledger();
    let added = 0;
    let left = 0;
    let room = MAX_STRANGER_SALES;
    for (const copy of [...strangers].sort((a, b) => b.at - a.at)) {
      const sales = copy.sales.slice(0, room);
      left += copy.sales.length - sales.length;
      if (!sales.length) continue;
      const result = book.merge(list, sales, { own: text.ledger, trusted: false });
      ({ list } = result);
      added += result.added;
      room -= sales.length;
    }
    if (added && !store(LEDGER, list)) return null;
    if (added) changedBook();
    return { added, left };
  }

  // One pull at a time: Enter or the button pressed again while the relays answer starts no second one (nor a second
  // question about the copies of unknown keys), and the pull every minute waits for its next turn
  let pullBusy = false;

  // The owner's nsec is read from its field and the field emptied at once, whatever follows. No form: nothing can submit
  // it to the site, and a browser has no form to offer to remember
  async function pullNow() {
    const typed = $("nostr-secret").value;
    $("nostr-secret").value = "";
    const setup = nostrSetup();
    if (!setup || pullBusy) return;
    pullBusy = true;
    try {
      await pullOnce(setup, typed);
    } finally {
      pullBusy = false;
    }
  }

  // The staff registers' sales as they arrive, while the page is open: at opening, when the history is opened and every
  // minute, with the keys this phone made for them (tillConversations) or, once given, the owner's private key. Nothing
  // is asked of the relays when there is nothing this phone could open
  function autoPull() {
    if (pullBusy || staff || !nostrSetup()) return;
    pullBusy = true;
    pullCopies(false).then((result) => {
      if (!result || result.failed) return;
      needKeys = result.needKey || 0;
      showNostr();
      if (result.foreign?.length) tellHistory(foreignSaid(result.foreign));
    }).catch(() => {}).finally(() => { pullBusy = false; });
  }
  if (!staff && !practice) {
    pulling = setInterval(autoPull, PULL_EVERY);
    setTimeout(autoPull, 2000);
    // Without relays (copies by the encrypted file only) the staff registers' sales are still checked, as they come in
    setInterval(verifyReported, PULL_EVERY);
    setTimeout(verifyReported, 3000);
  }

  async function pullOnce(setup, typed) {
    let secret;
    try {
      secret = nostr.readSecret(typed);
    } catch {
      tellHistory(words.nostr_bad_secret);
      return;
    }
    if (nostr.publicKey(secret) !== setup.owner) {
      tellHistory(words.nostr_other_secret);
      return;
    }
    ownerSecret = secret;
    tellHistory(words.busy);
    const result = await pullCopies(true);
    needKeys = 0;
    showNostr();
    if (!result || result.failed) tellHistory(words.nostr_no_relay);
    else if (result.full) tellHistory(words.full);
    else {
      const told = [say(words.nostr_pulled, result)];
      if (result.foreign.length) told.push(foreignSaid(result.foreign));
      const sales = result.strangers.reduce((sum, copy) => sum + copy.sales.length, 0);
      if (result.strangers.length && confirm(say(words.nostr_strangers_ask, { copies: result.strangers.length,
        keys: new Set(result.strangers.map((copy) => copy.author)).size, sales }))) {
        const taken = takeStrangers(result.strangers);
        told.push(taken ? say(words.nostr_strangers_taken, taken) : words.full);
      } else if (result.unknown) {
        told.push(say(words.nostr_strangers, { count: result.unknown }));
      }
      tellHistory(told.join(" "));
    }
  }

  if (!staff && !practice) forgetTills(false);
  $("nostr-forget")?.addEventListener("click", () => {
    if (!confirm(words.nostr_forget_ask)) return;
    forgetTills(true);
    tellSettings(words.nostr_forgotten);
  });
  if ($("nostr")) {
    $("nostr-send").addEventListener("click", () => {
      tellHistory(words.busy);
      publishCopies().then(() => tellHistory(nostrFailed ? words.nostr_failed : ""));
    });
    $("nostr-pull-go")?.addEventListener("click", pullNow);
    $("nostr-secret")?.addEventListener("keydown", (event) => {
      if (event.key !== "Enter") return;
      event.preventDefault();
      pullNow();
    });
    // Where a text field cannot be masked, a password field, still outside any form
    if ($("nostr-secret") && !globalThis.CSS?.supports?.("-webkit-text-security", "disc")) $("nostr-secret").type = "password";
  }
  // A staff register leaving the screen sends what it has at once
  document.addEventListener("visibilitychange", () => {
    if (document.hidden && staff) publishCopies();
  });

  // --- The screens ---

  // What the customer pays: the amount typed, less the discount when it is on
  const charged = () => (discountOn ? book.discounted(cents, prefs.discount) : cents);

  function render() {
    $("amount").textContent = money();
    // The hint on the cents is there until a key is pressed: then the discount takes its line
    $("hint").hidden = cents > 0;
    const button = $("discount");
    const fixed = $("discount-fixed");
    // Not removable (the owner's choice, the default): the discount is a line to read, not a button
    if (!prefs.discountRemovable) discountOn = prefs.discount > 0;
    // Only once there is an amount: «−5%: 0.00 to pay» before typing says nothing
    button.hidden = !(prefs.discount > 0 && prefs.discountRemovable && cents > 0);
    fixed.hidden = !(prefs.discount > 0 && cents > 0) || !button.hidden;
    if (button.parentElement) button.parentElement.hidden = button.hidden && fixed.hidden;
    if (!fixed.hidden) fixed.textContent = say(words.discount_fixed, { percent: prefs.discount, amount: fiat(charged() / 100) });
    if (button.hidden) return;
    button.setAttribute("aria-pressed", String(discountOn));
    button.textContent = discountOn ? say(words.discount_on, { percent: prefs.discount, amount: fiat(charged() / 100) })
      : say(words.discount_off, { percent: prefs.discount });
  }

  function reset() {
    clearTimeout(timer);
    timer = null;
    sale = null;
    store(OPEN, null);
    cents = 0;
    discountOn = prefs.discount > 0;
    // The zeroed amount is already read by the focus arriving there: for a moment the amount is not a live region, or
    // «0,00» would be heard twice
    const amount = $("amount");
    amount.setAttribute("aria-live", "off");
    render();
    warn("");
    $("state").textContent = "";
    $("done").classList.remove("done-expired");
    $("chain-state").hidden = true;
    showConfidence(null);
    $("later").hidden = true;
    show("keypad");
    setTimeout(() => amount.removeAttribute("aria-live"), 150);
  }

  function step(message) {
    $("step-text").textContent = message;
    show("step");
    announce(message);
  }

  function askMerchant(question, choices, how) {
    $("question").textContent = question;
    const buttons = choices.map((choice) => {
      const button = document.createElement("button");
      button.className = "button";
      button.textContent = say(words.use, { rate: fiat(choice.price) });
      button.addEventListener("click", () => charge(choice.price, choice.sources || [choice], how));
      return button;
    });
    document.querySelector("#rate .choices").replaceChildren(...buttons);
    show("rate");
    if (!buttons.length) warn(question);
    announce(question);
  }

  async function start(method = "lightning") {
    if (data.retired) return;
    if (blocked) {
      warn(blocked);
      return;
    }
    if (method === "onchain" && !prefs.onchain) return;
    if (quick) {
      const left = quickLeft();
      if (data.currency !== quick.currency) {
        warn(say(words.quick_currency, { currency: quick.currency }));
        return;
      }
      if (left <= 0 || charged() > left) {
        warn(left <= 0 ? words.quick_done : say(words.quick_over, { amount: moneyOf(charged()), left: moneyOf(left) }));
        return;
      }
    }
    clearTimeout(timer);
    // Which register took it, for the history once merged with the others (`who`: the staff register's name). `cents` is
    // what the customer pays; `list` the amount typed, before the discount
    const due = charged();
    sale = { id: uuid(), at: Date.now(), cents: due, label: moneyOf(due), currency: data.currency, method, till: text.ledger,
      who: text.tillName || "", ...(due !== cents ? { list: cents, discount: prefs.discount } : {}) };
    const mine = sale;
    warn("");
    step(words.rate);
    // The QR code and, for on-chain, the addresses load while the rate is asked
    const ready = need("qrcode", ...(method === "onchain" ? ["derive", "chain"] : []));
    let rate;
    try {
      rate = await backend.quote();
    } catch {
      rate = { question: words.no_rate, choices: [], how: "none" };
    }
    try {
      await ready;
    } catch {
      if (mine !== sale) return;
      sale = null;
      show("keypad");
      warn(words.not_loaded);
      return;
    }
    if (mine !== sale) return;
    if (rate.price) charge(rate.price, rate.sources, rate.how);
    else askMerchant(rate.question, rate.choices, rate.how);
  }

  async function charge(rate, sources, how) {
    const mine = sale;
    // A second tap on the same choice does not ask for a second invoice
    if (!mine || mine.how) return;
    const amount = Math.round(((mine.cents / 100) / rate) * 1e8);
    Object.assign(mine, { sats: amount, rate, how,
      sources: sources.map(({ source, price: value, at }) => ({ source, price: value, at })) });
    if (mine.method === "onchain") {
      if (amount < MIN_CHAIN_SATS) {
        mine.how = undefined;
        askMerchant(say(words.chain_small, { sats: sats(MIN_CHAIN_SATS) }), [], how);
        return;
      }
      chargeChain(mine, how);
      return;
    }
    step(words.invoice);
    let invoice;
    try {
      invoice = await backend.invoiceFor(amount * 1000);
    } catch (error) {
      if (mine !== sale) return;
      mine.how = undefined;
      askMerchant(error.shown ? error.message : words.no_answer, [], how);
      return;
    }
    if (mine !== sale) return;
    Object.assign(mine, { pr: invoice.pr, qr: invoice.qr, verify: invoice.verify, expires: invoice.expires, hash: invoice.hash,
      status: "pending" });
    record(mine);
    store(OPEN, mine.id);
    pay(mine);
  }

  async function chargeChain(mine, how) {
    step(words.new_address);
    let place;
    try {
      place = await chainAddress();
    } catch (error) {
      if (mine !== sale) return;
      mine.how = undefined;
      askMerchant(error.shown ? error.message : prefs.onchain.address ? words.chain_fixed_down : words.chain_no_key, [], how);
      return;
    }
    if (mine !== sale) return;
    const uri = chain.uri(place.address, mine.sats);
    Object.assign(mine, { address: place.address, index: place.index, path: place.path, before: place.before,
      spent: place.spent, pr: uri,
      qr: backend.practiceQr || qrImage(uri), verify: "", hash: "", expires: now() + CHAIN_LIFETIME, wait: chain.waits(mine.cents, prefs.confirmAbove),
      status: "pending" });
    record(mine);
    store(OPEN, mine.id);
    pay(mine);
  }

  function pay(entry) {
    $("due").textContent = `${entry.label} · ${sats(entry.sats)}`;
    $("rate-used").textContent = say(words.agreed, { rate: fiat(entry.rate), sources: sourceNames(entry.sources || []) });
    const onchain = entry.method === "onchain";
    // Where the money goes, written out: the merchant recognises their address, or notices it is not theirs
    $("to").textContent = onchain ? chainTo(entry) : words.to ? say(words.to, { address: data.address }) : "";
    $("to").hidden = !$("to").textContent;
    $("qr").src = entry.qr;
    $("pay-method").textContent = methodName(entry.method);
    // The code's text alternative says what it asks: the network and the amount
    $("qr").alt = `${QR_ALT}: ${$("pay-method").textContent}, ${$("due").textContent}`;
    $("copy-invoice").textContent = COPY_LABEL;
    $("invoice").textContent = onchain ? `${entry.address} · ${chain.uri("", entry.sats).split("=")[1]} BTC` : entry.pr;
    // Without LUD-21 nobody tells the register that the money arrived: the merchant looks in the wallet and says so.
    // On-chain the explorer tells; only when it does not answer does the merchant look in the wallet
    const manual = !onchain && !entry.verify;
    $("no-check").hidden = !manual;
    $("no-check").textContent = manual ? words.no_check : "";
    $("seen").hidden = !manual;
    $("seen").textContent = words.seen;
    $("chain-state").hidden = true;
    showConfidence(null);
    $("later").hidden = true;
    $("later").textContent = words.chain_later;
    show("pay");
    // One sentence: a second one written at once would take the place of the first before it is read
    announce([`${$("due").textContent}.`, document.querySelector("#pay .waiting").textContent.trim(), manual ? words.no_check : ""]
      .filter(Boolean).join(" "));
    watch();
  }

  // The invoice or the address as text, for a customer whose phone does not scan: copied with one tap where the browser
  // allows it, otherwise selected for the phone's own Copy
  const QR_ALT = $("qr").alt;
  const COPY_LABEL = $("copy-invoice").textContent;
  $("copy-invoice").hidden = !navigator.clipboard?.writeText;
  $("copy-invoice").addEventListener("click", async () => {
    const button = $("copy-invoice");
    try {
      await navigator.clipboard.writeText(sale?.method === "onchain" ? sale.address : sale?.pr || "");
      button.textContent = button.dataset.copied;
      announce(button.dataset.copied);
    } catch {
      window.getSelection?.().selectAllChildren($("invoice"));
    }
  });

  function watch() {
    clearTimeout(timer);
    timer = setTimeout(check, CHECK_EVERY);
  }

  // One look at a time. The round, the screen turning back on and the reopened page all call check(): a look that starts
  // while another is still waiting for the provider only puts the round back on, so two answers never race to end the
  // same sale (one «paid», the other «expired»)
  async function check() {
    const mine = sale;
    if (!mine || mine.status !== "pending") return;
    if (checking) {
      watch();
      return;
    }
    clearTimeout(timer);
    checking = true;
    try {
      if (mine.method === "onchain") {
        await checkChain(mine);
        return;
      }
      let unknown = !mine.verify;
      if (mine.verify) {
        try {
          const paid = await backend.settled(mine);
          // Cancelled, or ended by the last look of Cancel, while the provider was answering
          if (mine !== sale || mine.status !== "pending") return;
          warn("");
          if (paid) {
            finish("paid");
            return;
          }
        } catch (error) {
          if (mine !== sale || mine.status !== "pending") return;
          // Paid, says the provider, but nothing proves it is this sale's: the merchant looks in the wallet and says so
          if (error.unverifiable) $("seen").hidden = false;
          warn(error.unverifiable ? words.unverifiable : words.check_failed);
          unknown = true;
        }
      }
      // Expired while the provider did not answer: it may have been paid, so it is not «expired» but to check in the wallet
      if (expired(mine)) finish(unknown ? "unchecked" : "expired");
      else watch();
    } finally {
      checking = false;
    }
  }

  // One look at the explorer for the on-chain sale on the screen. At the end of its day: seen in the mempool, it is left
  // to its confirmation (as «Next customer»); never seen, expired; less than asked, or no answer, to check in the wallet
  async function checkChain(mine) {
    let state = "";
    try {
      const result = await chainState(mine);
      if (mine !== sale || mine.status !== "pending") return;
      ({ state } = result);
      warn("");
      // The explorer answers again: the register sees the payment itself
      $("seen").hidden = true;
      if (result.txids[0]) mine.txid = result.txids[0];
      if (result.state === "paid") {
        finish("paid");
        return;
      }
      const message = result.state === "seen"
        ? `${words.chain_seen} ${say(words[`confidence_${result.confidence.level}`], { why: whyText(result.confidence.reasons
          .find((item) => item.code === result.confidence.reason) || { code: result.confidence.reason }) })}`
        : result.state === "partial"
        ? say(words.chain_partial, { seen: sats(result.seen), sats: sats(mine.sats) }) : result.state === "over"
          ? say(words.chain_over, { seen: sats(result.seen), sats: sats(mine.sats) }) : result.state === "outside"
            ? say(words.chain_outside, { seen: sats(result.outside) }) : "";
      $("chain-state").textContent = message;
      $("chain-state").hidden = !message;
      showConfidence(result.state === "seen" ? result.confidence : null);
      // Too much, or out of the sale's time: the merchant looks in the wallet and decides
      if (["over", "outside"].includes(result.state)) $("seen").hidden = false;
      $("later").hidden = result.state !== "seen";
      if (message) announce(message);
    } catch {
      if (mine !== sale || mine.status !== "pending") return;
      warn(words.chain_down);
      $("seen").hidden = false;
    }
    if (!expired(mine)) timer = setTimeout(check, CHAIN_EVERY);
    else if (state === "seen") later();
    else finish(state === "none" ? "expired" : "unchecked");
  }

  const whyText = (item) => say(words[`why_${item.code}`] || item.code, { rate: item.rate, target: item.target, count: item.count,
    host: item.host });

  // The confidence of a payment in the mempool as a small bar of three steps; touched, it opens the reasons
  function showConfidence(found) {
    const button = $("confidence");
    if (!button) return;
    button.hidden = !found;
    if (!found) {
      $("confidence-why").hidden = true;
      button.setAttribute("aria-expanded", "false");
      return;
    }
    const steps = { wait: 1, likely: 2, sure: 3 }[found.level] || 1;
    button.dataset.level = found.level;
    button.querySelectorAll(".confidence-step").forEach((step, n) => step.classList.toggle("on", n < steps));
    $("confidence-label").textContent = words[`level_${found.level}`];
    const list = $("confidence-why");
    list.replaceChildren(...found.reasons.map((item) => {
      const line = document.createElement("li");
      line.textContent = whyText(item);
      return line;
    }));
  }

  function finish(status) {
    const mine = sale;
    showConfidence(null);
    clearTimeout(timer);
    mine.status = status;
    record(mine);
    store(OPEN, null);
    const paid = status === "paid" || status === "confirmed";
    const message = STATES()[status];
    $("done").classList.toggle("done-expired", !paid);
    $("result").textContent = message;
    const parts = [mine.label, new Date().toLocaleString(lang), methodName(mine.method), sats(mine.sats)];
    if (mine.hash) parts.push(`#${mine.hash.slice(0, 8)}`);
    if (mine.txid) parts.push(`tx ${mine.txid.slice(0, 8)}`);
    $("receipt").textContent = parts.join(" · ");
    $("print").hidden = !paid;
    warn("");
    show("done");
    announce(message);
    if (paid) {
      buzz(200);
      chime();
      const screen = $("done");
      screen.classList.remove("flash");
      void screen.offsetWidth;  // the animation starts again for the next payment
      screen.classList.add("flash");
    }
  }

  // The sound of a payment, made here (no file to download): a quick upward spark, the Lightning, landing on a dominant
  // seventh rolled upwards that resolves on C, the coins and the sale closed. Each phone chooses it in the menu; on by default. The browser plays sound only after
  // a tap on the page, as for the vibration
  const SOUND_KEY = "register-sound";
  const soundOn = () => localStorage.getItem(SOUND_KEY) !== "off";
  let audio = null;
  function chime() {
    if (!soundOn() || navigator.userActivation?.hasBeenActive === false) return;
    const Context = window.AudioContext || window.webkitAudioContext;
    if (!Context) return;
    try {
      audio = audio || new Context();
      if (audio.state === "suspended") audio.resume();
      const t = audio.currentTime + 0.02;
      const out = audio.createGain();
      out.gain.value = 0.25;
      out.connect(audio.destination);
      const tone = (type, from, to, start, length, peak) => {
        const osc = audio.createOscillator();
        const gain = audio.createGain();
        osc.type = type;
        osc.frequency.setValueAtTime(from, start);
        if (to !== from) osc.frequency.exponentialRampToValueAtTime(to, start + length * 0.6);
        gain.gain.setValueAtTime(0.0001, start);
        gain.gain.exponentialRampToValueAtTime(peak, start + 0.01);
        gain.gain.exponentialRampToValueAtTime(0.0001, start + length);
        osc.connect(gain).connect(out);
        osc.start(start);
        osc.stop(start + length + 0.02);
      };
      tone("sawtooth", 300, 2400, t, 0.07, 0.35);            // the spark
      // A dominant seventh (G B D F), rolled upwards like a harp: bright, and still a little open
      [783.99, 987.77, 1174.66, 1396.91].forEach((freq, i) => tone("sine", freq, freq, t + 0.06 + i * 0.03, 0.3, 0.6));
      tone("triangle", 392, 392, t + 0.06, 0.3, 0.3);  // G4 under them, a little body
      // ...and it resolves on C: the perfect cadence, the sale closed
      tone("sine", 2093, 2093, t + 0.2, 0.38, 0.7);       // C7
      tone("triangle", 523.25, 523.25, t + 0.2, 0.38, 0.3);  // C5 under it
    } catch (error) {
      // No sound is never a reason to stop a sale
    }
  }
  if ($("sound-on")) {
    $("sound-on").checked = soundOn();
    $("sound-on").addEventListener("change", (event) => {
      localStorage.setItem(SOUND_KEY, event.target.checked ? "on" : "off");
      if (event.target.checked) chime();
    });
  }

  function confirmAmount(method = "lightning") {
    if (cents > 0) start(method);
    else warn(text.noAmount);
  }

  function press(digit) {
    if (cents < 10000000) cents = cents * 10 + digit;
    warn("");
    render();
  }

  document.querySelector(".keys").addEventListener("click", (event) => {
    const button = event.target.closest("button");
    if (!button) return;
    if (button.classList.contains("ok")) {
      confirmAmount();
    } else if (button.classList.contains("clear")) {
      cents = 0;
      render();
    } else {
      press(Number(button.textContent));
    }
  });
  document.addEventListener("keydown", (event) => {
    if ($("keypad").hidden || menu.open || event.ctrlKey || event.metaKey || event.altKey) return;
    // Enter on a link, on the exit or on a key reached with Tab does what it says; on a key just tapped,
    // or with focus nowhere, it stays OK, as at a till with a physical keyboard
    const control = event.target.closest?.("a, button, input, select, textarea, summary");
    if (event.key === "Enter" && control && (!control.closest(".keys") || control.matches(":focus-visible"))) return;
    if (/^[0-9]$/.test(event.key)) {
      press(Number(event.key));
    } else if (event.key === "Backspace") {
      cents = Math.floor(cents / 10);
      render();
    } else if (event.key === "Escape") {
      cents = 0;
      render();
    } else if (event.key === "Enter") {
      event.preventDefault();
      confirmAmount();
    }
  });
  $("seen").addEventListener("click", () => finish("confirmed"));
  $("confidence")?.addEventListener("click", () => {
    const open = $("confidence").getAttribute("aria-expanded") !== "true";
    $("confidence").setAttribute("aria-expanded", String(open));
    $("confidence-why").hidden = !open;
  });
  // An on-chain payment seen in the mempool, waiting for its confirmation: the register goes on, and checks it later
  function later() {
    const mine = sale;
    if (!mine || mine.status !== "pending") return;
    clearTimeout(timer);
    mine.status = "seen";
    record(mine);
    reset();
    announce(words.seen_state);
  }
  $("later").addEventListener("click", later);
  $("chain-ok")?.addEventListener("click", () => confirmAmount("onchain"));
  $("discount").addEventListener("click", () => {
    if (!prefs.discountRemovable) return;
    discountOn = !discountOn;
    render();
  });
  $("pin-ok").addEventListener("click", () => {
    store(SETTINGS, { ...ownerSettings(), lightning: data.address });
    setUp();
  });
  $("again").addEventListener("click", reset);
  $("print").addEventListener("click", () => print());
  $("rate-back").addEventListener("click", reset);
  $("cancel").addEventListener("click", async () => {
    if (!confirm(text.cancelWarning)) return;
    const mine = sale;
    // On-chain, one last look at the explorer: paid, it ends paid; seen in the mempool, it is not cancelled but left to
    // its confirmation
    if (mine?.method === "onchain" && mine.status === "pending") {
      try {
        const result = await chainState(mine);
        if (mine !== sale || mine.status !== "pending") return;
        if (result.state === "paid") {
          finish("paid");
          return;
        }
        if (result.state === "seen") {
          later();
          return;
        }
      } catch { /* cancel anyway: if it arrives, it shows in the wallet */ }
    }
    // Before giving up, one last look: the customer may have paid just then
    if (mine?.verify) {
      try {
        const paid = await backend.settled(mine);
        // The round may have ended it meanwhile: a sale ends once
        if (mine !== sale || mine.status !== "pending") return;
        if (paid) {
          finish("paid");
          return;
        }
      } catch { /* cancel anyway: if it arrives, it shows in the wallet */ }
    }
    if (mine !== sale || mine?.status !== "pending") return;
    mine.status = "cancelled";
    record(mine);
    reset();
    announce(words.cancelled);
  });
  // When the screen turns back on the phone restarts here: it checks at once, without waiting for the next round
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) return;
    if (sale?.status === "pending") check();
    keepAwake(!$("pay").hidden);
  });

  // --- The owner's settings: on this phone only ---

  // The destination last shown for a check: saving it needs the box ticked after seeing its first address. The same for
  // the start of the on-chain addresses: the key and the number last shown with its address
  let shownDestination = "";
  let shownStart = "";
  // The explorer's first unused address of the key being saved: asked once per key and path, {of, index}
  let scanned = null;

  // The first unused receive address of the owner's chain, as a wallet finds it: past the last used address, GAP_LIMIT
  // unused in a row mean the end. Leaps over long used runs (1, 2, 4, …) and halves back, so that a wallet with hundreds
  // of used addresses takes a few dozen questions. Throws when the explorer does not answer, or after SCAN_ASKS questions
  const GAP_LIMIT = 20;
  const SCAN_ASKS = 200;
  async function firstUnused(onchain) {
    const place = await chainPlace(onchain);
    const known = new Map();
    let asks = 0;
    const used = async (index) => {
      if (!fitsIndex(index)) throw new Error("index");
      if (known.has(index)) return known.get(index);
      if (++asks > SCAN_ASKS) throw new Error("asks");
      const address = await derive.addressAt(place.key, [index], data.network);
      const outputs = chain.outputs(await backend.chainTxs(address), address);
      if (!outputs) throw new Error("explorer");
      known.set(index, outputs.length > 0);
      return outputs.length > 0;
    };
    let last = -1;
    for (;;) {
      let step = 1;
      while (await used(last + step)) {
        last += step;
        step *= 2;
      }
      let high = last + step;
      while (high - last > 1) {
        const middle = last + Math.floor((high - last) / 2);
        if (await used(middle)) last = middle;
        else high = middle;
      }
      // The next GAP_LIMIT after the first unused one, together: one used among them and the search goes on from it
      const window = Array.from({ length: GAP_LIMIT - 1 }, (_, at) => last + 2 + at);
      const answers = await Promise.all(window.map(used));
      const later = window.filter((index, at) => answers[at]).pop();
      if (later === undefined) return last + 1;
      last = later;
    }
  }

  function hideStart() {
    shownStart = "";
    // The form has the on-chain fields only while on-chain is on
    if (!CHAIN_ON) return;
    $("chain-start").value = "";
    $("chain-start-check").textContent = "";
    $("chain-start-line").hidden = true;
    $("chain-start-confirm").checked = false;
  }

  function tellSettings(message) {
    $("settings-say").textContent = message;
  }

  // A field's own error, next to it (…-error), and in the line by the Save button: the field is marked and focused
  function fieldError(id, message) {
    const shown = $(`${id}-error`);
    const field = $(id);
    if (shown) {
      shown.textContent = message;
      shown.hidden = !message;
    }
    if (message) field.setAttribute("aria-invalid", "true");
    else field.removeAttribute("aria-invalid");
    // The help line stays described; the error comes first while there is one
    const help = $(`${id}-help`) ? `${id}-help` : "";
    const describedBy = [message && shown ? `${id}-error` : "", help].filter(Boolean).join(" ");
    if (describedBy) field.setAttribute("aria-describedby", describedBy);
    else field.removeAttribute("aria-describedby");
  }

  function refuse(id, message) {
    tellSettings(message);
    flag(id, "settings-say");
    fieldError(id, message);
  }

  // The checks of each field as it is typed or left, before Save: "" when it is fine
  const fieldChecks = {
    currency: (value) => {
      const typed = value.trim().toUpperCase();
      try {
        if (!/^[A-Z]{3}$/.test(typed)) throw new Error(typed);
        (1).toLocaleString(lang, { style: "currency", currency: typed });
        return "";
      } catch {
        return words.bad_currency;
      }
    },
    "discount-percent": (value) => (/^\s*\d{0,3}\s*$/.test(value) && !(Number(value) > 100) ? "" : words.bad_discount),
    "confirm-above": (value) => (typedAmount(value) === null ? words.bad_amount : ""),
    "chain-start": (value) => (!value.trim() || (/^\d{1,10}$/.test(value.trim()) && fitsIndex(Number(value.trim())))
      ? "" : words.start_bad),
    "chain-path": (value) => {
      const typed = value.trim().replace(/^m?\/+|\/+$/g, "");
      return !typed || pairing.steps(typed) ? "" : words.path_bad;
    },
    "own-explorer": (value) => (!value.trim() || (/^https:\/\//i.test(value.trim()) && value.trim().length <= 200)
      ? "" : words.own_explorer_bad),
    "nostr-owner": (value) => {
      if (!value.trim()) return "";
      try {
        nostr.readPublic(value);
        return "";
      } catch {
        return words.nostr_bad_public;
      }
    },
    "chain-key": async (value) => {
      if (!value.trim()) return "";
      const pasted = await derive.destination(value.trim(), data.network);
      return pasted.kind ? "" : { network: words.wrong_network, private: words.private_key }[pasted.reason] || words.bad_destination;
    },
  };

  // A field checked when it is left; while typing, only one already marked wrong (the mark goes as soon as it is right)
  async function checkField(id, leaving) {
    const field = $(id);
    if (!field || !$(`${id}-error`)) return;
    if (!leaving && field.getAttribute("aria-invalid") !== "true") return;
    fieldError(id, await fieldChecks[id](field.value));
  }

  function openSettings() {
    const saved = ownerSettings();
    // The form has the on-chain fields (the key, the path, the threshold for a block) only while on-chain is on
    if (CHAIN_ON) {
      $("chain-key").value = saved.onchain?.key || saved.onchain?.address || "";
      $("chain-path").value = typeof saved.onchain?.path === "string" ? saved.onchain.path : "";
      // The path in use, written out, for the owner to compare with the wallet
      $("chain-path-now").textContent = saved.onchain?.key ? say(words.path_now, { path: `${ownerPath(saved.onchain)}/n` }) : "";
      $("chain-check").textContent = "";
      $("chain-confirm-line").hidden = true;
      $("chain-confirm").checked = false;
      // A threshold saved before, past what the field takes, is shown at the most it takes: saving the other settings
      // keeps working
      $("confirm-above").value = (Math.min(prefs.confirmAbove, MAX_THRESHOLD) / 100).toFixed(2);
    }
    if ($("own-explorer")) $("own-explorer").value = data.own_explorer || "";
    shownDestination = "";
    hideStart();
    $("discount-percent").value = prefs.discount;
    $("discount-removable").checked = prefs.discountRemovable;
    $("settings-lightning").textContent = data.address;
    for (const id of Object.keys(fieldChecks)) if ($(`${id}-error`)) fieldError(id, "");
    $("currency").value = prefs.currency;
    const legend = $("sources").querySelector("legend");
    $("sources").replaceChildren(legend, ...serverSources.map((name) => {
      const label = document.createElement("label");
      label.className = "check";
      const box = document.createElement("input");
      Object.assign(box, { type: "checkbox", value: name, checked: prefs.sources.includes(name) });
      label.append(box, ` ${SOURCES[name]?.name || name}`);
      return label;
    }));
    $("colour").value = /^#[0-9a-f]{6}$/i.test(prefs.shop.colour || "") ? prefs.shop.colour : "#000000";
    $("note-text").value = prefs.note;
    if ($("nostr-owner")) {
      const chosen = saved.nostr || {};
      $("nostr-owner").value = typeof chosen.owner === "string" ? nostr.npub(chosen.owner) : "";
      for (const box of $("nostr-relays")?.querySelectorAll("input") || []) {
        box.checked = Array.isArray(chosen.relays) && chosen.relays.includes(box.value);
      }
    }
    tellSettings("");
    show("settings");
  }

  // The confirmation threshold as typed, in cents: a point or a comma before the cents («50.», «.5» too), up to
  // MAX_THRESHOLD, empty is 0; null when it is not an amount (never saved as 0, which would make every sale wait for a
  // block)
  function typedAmount(value) {
    const typed = value.trim().replace(",", ".");
    if (!typed) return 0;
    return /^(\d{1,8}(\.\d{0,2})?|\.\d{1,2})$/.test(typed) ? Math.round(Number(typed) * 100) : null;
  }

  async function saveSettings(event) {
    event.preventDefault();
    unflag("settings-form");
    for (const id of Object.keys(fieldChecks)) if ($(`${id}-error`)) fieldError(id, "");
    const saved = ownerSettings();
    // With on-chain off the form has no on-chain fields: what this phone holds of it (the destination, the threshold
    // for a block, the count of the addresses) is saved again as it is, never emptied
    const typed = CHAIN_ON ? $("chain-key").value.trim() : "";
    let onchain = CHAIN_ON ? null : saved.onchain;
    if (typed) {
      const pasted = await derive.destination(typed, data.network);
      if (!pasted.kind) {
        refuse("chain-key", { network: words.wrong_network, private: words.private_key }[pasted.reason] || words.bad_destination);
        return;
      }
      onchain = pasted.kind === "key" ? { key: pasted.key } : { address: pasted.address };
      // The path after the key, when not the receive chain: only steps that are not hardened, at most four
      const pathTyped = $("chain-path").value.trim().replace(/^m?\/+|\/+$/g, "");
      if (pathTyped && pathTyped !== pairing.DEFAULT_PATH) {
        if (!onchain.key || !pairing.steps(pathTyped)) {
          refuse("chain-path", onchain.key ? words.path_bad : words.path_no_key);
          return;
        }
        onchain.path = pathTyped;
      }
      let first = pasted.first;
      if (onchain.path) {
        try {
          first = await derive.addressAt(onchain.key, [...pairing.steps(onchain.path), 0], data.network);
        } catch {
          refuse("chain-path", words.path_bad);
          return;
        }
      }
      // A new destination, or a new path, is saved only after the owner has seen its first address (or the warning of a
      // fixed one) and ticked the box: a key pasted wrong, or not of this wallet, would send the money where nobody
      // spends it
      const before = `${saved.onchain?.key || saved.onchain?.address || ""} ${saved.onchain?.path || ""}`;
      const shownNow = `${typed} ${onchain.path || ""}`;
      if (`${onchain.key || onchain.address} ${onchain.path || ""}` !== before
          && !(shownDestination === shownNow && $("chain-confirm").checked)) {
        $("chain-check").textContent = pasted.kind !== "key" ? words.fixed_warning : onchain.path
          ? say(words.path_first, { path: `${onchain.path}/0`, address: first }) : say(words.key_first, { address: first });
        $("chain-confirm-line").hidden = false;
        $("chain-confirm").checked = false;
        shownDestination = shownNow;
        tellSettings(words.tick);
        flag("chain-confirm", "settings-say chain-check");
        return;
      }
    }
    // Where the on-chain addresses start. By default, for a new key (or path), the register asks the explorer once for
    // the wallet's first unused receive address (firstUnused) and keeps it as the start of its count; the owner may type
    // a number instead (Advanced), for a wallet whose history the explorer does not see whole. Either way the count of
    // the owner's chain of this key starts from that number, as sure as the phone's own. A number past LEDGER_JUMP is
    // saved only after the owner has seen the address there and ticked the box, whoever found it: a number typed wrong,
    // or an explorer that lies, would send the payments past the wallet's gap limit. One within it is what a sale would
    // reach on the explorer's word anyway
    const startTyped = CHAIN_ON ? $("chain-start").value.trim() : "";
    let startAt = null;
    let found = null;
    const newChain = Boolean(onchain?.key) && (onchain.key !== (saved.onchain?.key || "")
      || ownerPath(onchain) !== ownerPath(saved.onchain));
    if (!startTyped && newChain) {
      const of = `${onchain.key}/${ownerPath(onchain)}`;
      if (scanned?.of !== of) {
        tellSettings(words.scan_checking);
        scanned = { of, index: await firstUnused(onchain).catch(() => null) };
      }
      found = scanned.index;
      if (found !== null && found > LEDGER_JUMP) {
        $("chain-start").value = String(found);
      } else if (found !== null && found > 0) startAt = found;
    }
    const startField = CHAIN_ON ? $("chain-start").value.trim() : "";
    if (startField && startAt === null) {
      const startTyped = startField;
      if (!onchain?.key) {
        refuse("chain-start", words.start_no_key);
        return;
      }
      startAt = /^\d{1,10}$/.test(startTyped) ? Number(startTyped) : -1;
      if (!fitsIndex(startAt)) {
        refuse("chain-start", words.start_bad);
        return;
      }
      const shown = `${onchain.key}/${ownerPath(onchain)}/${startAt}`;
      if (!(shownStart === shown && $("chain-start-confirm").checked)) {
        let address;
        try {
          address = await derive.addressAt(onchain.key, [...pairing.steps(ownerPath(onchain)), startAt], data.network);
        } catch {
          refuse("chain-start", words.start_bad);
          return;
        }
        $("chain-start-check").textContent = [found === startAt ? say(words.scan_far, { index: startAt }) : "",
          say(words.start_first, { path: `${ownerPath(onchain)}/${startAt}`, address })].filter(Boolean).join(" ");
        $("chain-start-line").hidden = false;
        $("chain-start-confirm").checked = false;
        shownStart = shown;
        tellSettings(words.tick);
        flag("chain-start-confirm", "settings-say chain-start-check");
        return;
      }
    }
    const currency = $("currency").value.trim().toUpperCase();
    try {
      if (!/^[A-Z]{3}$/.test(currency)) throw new Error(currency);
      (1).toLocaleString(lang, { style: "currency", currency });
    } catch {
      refuse("currency", words.bad_currency);
      return;
    }
    const confirmAbove = CHAIN_ON ? typedAmount($("confirm-above").value) : saved.confirmAbove ?? undefined;
    if (confirmAbove === null) {
      refuse("confirm-above", words.bad_amount);
      return;
    }
    if (fieldChecks["discount-percent"]($("discount-percent").value)) {
      refuse("discount-percent", words.bad_discount);
      return;
    }
    const sources = [...$("sources").querySelectorAll("input:checked")].map((box) => box.value)
      .filter((name) => serverSources.includes(name));
    if (sources.length < 2) {
      refuse("sources", words.few_sources);
      return;
    }
    let nostrOwner = saved.nostr;
    if ($("nostr-owner")) {
      nostrOwner = null;
      if ($("nostr-owner").value.trim()) {
        try {
          nostrOwner = { owner: nostr.readPublic($("nostr-owner").value),
            relays: [...($("nostr-relays")?.querySelectorAll("input:checked") || [])].map((box) => box.value)
              .filter((url) => publicRelays().includes(url)) };
        } catch {
          refuse("nostr-owner", words.nostr_bad_public);
          return;
        }
      }
    }
    // The shop's own explorer: kept on the site (not a secret: the address of an API), for the content security policy and
    // for the staff registers; the site never asks it. A new one is used after the register reloads
    let explorerSaved = false;
    const explorerTyped = $("own-explorer") ? $("own-explorer").value.trim() : (data.own_explorer || "");
    if (explorerTyped !== (data.own_explorer || "")) {
      if (explorerTyped && (!/^https:\/\//i.test(explorerTyped) || explorerTyped.length > 200)) {
        refuse("own-explorer", words.own_explorer_bad);
        return;
      }
      let response;
      try {
        response = await fetch(data.explorer_save, { method: "POST", credentials: "same-origin", cache: "no-store",
          redirect: "error", headers: { "Content-Type": "application/json", "X-CSRFToken": data.chain_claim?.csrf || "" },
          body: JSON.stringify({ explorer: explorerTyped }) });
      } catch {
        tellSettings(words.own_explorer_failed);
        return;
      }
      if (response.status === 400) {
        refuse("own-explorer", words.own_explorer_bad);
        return;
      }
      const answer = response.ok ? await response.json().catch(() => null) : null;
      if (typeof answer?.explorer !== "string") {
        tellSettings(words.own_explorer_failed);
        return;
      }
      explorerSaved = true;
      if (answer.explorer) data.own_explorer = answer.explorer;
      else delete data.own_explorer;
      $("own-explorer").value = answer.explorer;
    }
    const keyChanged = (onchain?.key || "") !== (saved.onchain?.key || "") || ownerPath(onchain) !== ownerPath(saved.onchain);
    store(SETTINGS, { ...saved, onchain, sources, currency, nostr: nostrOwner || undefined,
      discount: Math.min(100, Math.max(0, Math.round(Number($("discount-percent").value) || 0))),
      discountRemovable: $("discount-removable").checked === true,
      confirmAbove,
      colour: /^#[0-9a-f]{6}$/i.test($("colour").value) ? $("colour").value : "",
      note: $("note-text").value.replace(/[\u0000-\u001f\u007f]/g, " ").trim().slice(0, 140) });
    // A new key starts from its first address; the ones already used are skipped at the next sale
    if (keyChanged) store(NEXT, null);
    if (startAt !== null) store(NEXT, { of: `${onchain.key}/${ownerPath(onchain)}`, next: startAt, start: startAt, first: true });
    // Copies for another owner's key are all made again
    if ((nostrOwner?.owner || "") !== (saved.nostr?.owner || "")) store(NOSTR_SENT, null);
    scheduleNostr(true);
    showNostr();
    setUp();
    if (CHAIN_ON) {
      $("chain-check").textContent = "";
      $("chain-confirm-line").hidden = true;
    }
    shownDestination = "";
    hideStart();
    scanned = null;
    tellSettings([startAt === null ? words.settings_saved
      : `${words.settings_saved} ${say(words.start_set, { path: `${ownerPath(onchain)}/${startAt}` })}`,
    newChain && !startTyped && found === null ? words.scan_down : "",
    explorerSaved ? words.own_explorer_saved : ""].filter(Boolean).join(" "));
  }

  if ($("settings")) {
    $("settings-open").addEventListener("click", () => opening(openSettings));
    $("settings-form").addEventListener("submit", saveSettings);
    for (const id of Object.keys(fieldChecks)) {
      $(id)?.addEventListener("focusout", () => checkField(id, true));
      $(id)?.addEventListener("input", () => checkField(id, false));
    }
    $("settings-back").addEventListener("click", () => show("keypad"));
  }

  // The till's copy for when the site does not answer: the page and its files, nothing else. The page fills it, and it
  // updates with the site; the files of an old version leave the copy.
  // Only the site's own answers go in it: Django puts its CSP on everything and the register page says X-Register,
  // while Caddy's works page (maintenance, or an address outside the whitelist) is a 200 without either, for every path.
  // A page never takes the place of a script or a style, and a redirect (the staff rules) is not the register
  const isHtml = (response) => (response.headers.get("Content-Type") || "").toLowerCase().startsWith("text/html");
  const keepable = (response, page) => response.ok && !response.redirected && response.headers.has("Content-Security-Policy")
    && (page ? isHtml(response) && response.headers.get("X-Register") === "1" : !isHtml(response));

  const FINGERPRINTED = /\.[0-9a-f]{12}\.\w+$/;
  async function keepCopy() {
    if (!("serviceWorker" in navigator) || !("caches" in self) || !text.worker) return;
    try {
      await navigator.serviceWorker.register(text.worker);
      // With its query: a staff register's page answers only with the key in it (?k=)
      const page = location.pathname + location.search;
      const files = [page, ...[...document.querySelectorAll(
        "link[rel=stylesheet], link[rel=manifest], script[src], .register-top img")].map((element) => element.href || element.src), ...Object.values(LAZY)]
        .map((address) => new URL(address, location.href))
        .filter((address) => address.origin === location.origin)
        .map((address) => address.pathname + address.search);
      const cache = await caches.open(`register:${location.pathname}`);
      // A file with its fingerprint in the name never changes: one already kept is not asked for again
      const kept = new Set((await cache.keys()).map((request) => new URL(request.url).pathname));
      // One by one: a missing file (a theme without theme.css) must not leave everything else without a copy. A file
      // the site does not give as it should keeps its earlier copy
      await Promise.allSettled(files.filter((file) => !(FINGERPRINTED.test(file) && kept.has(file))).map(async (file) => {
        const response = await fetch(file, { cache: "no-cache", credentials: "same-origin" });
        if (keepable(response, file === page)) await cache.put(file, response);
      }));
      for (const request of await cache.keys()) {
        const address = new URL(request.url);
        if (!files.includes(address.pathname + address.search)) await cache.delete(request);
      }
    } catch { /* without a copy the till still works, as long as the site answers */ }
  }

  for (const button of document.querySelectorAll("[data-history]")) button.addEventListener("click", () => opening(openHistory));
  $("history-back").addEventListener("click", () => {
    showLedger();
    show("keypad");
  });
  $("day").addEventListener("change", (event) => showDay(event.target.value));
  $("close-day").addEventListener("click", closeDay);
  $("copy-every").addEventListener("change", (event) => {
    store(COPY, { ...copies(), every: event.target.value });
    showCopies();
  });
  $("csv-form").addEventListener("submit", csvDownload);
  $("export-form").addEventListener("submit", copyDownload);
  $("import-form").addEventListener("submit", copyUpload);
  // The first time, one sentence on where the sales are: it stays in the history too
  $("keep-first").hidden = Boolean(load(WARNED, false));
  $("keep-ok").addEventListener("click", () => {
    store(WARNED, true);
    $("keep-first").hidden = true;
    // It is read in the menu: the focus stays there
    $("menu-close").focus();
    keepData(true);
  });
  // The menu: one button on the keypad opens everything that is not taking a payment (history, settings, practice, help,
  // leaving the panel). A modal dialog: the browser keeps the focus in it and closes it with Escape
  function closeMenu(focusButton) {
    if (!menu.open) return;
    menu.close?.();
    menu.open = false;
    if (focusButton) $("menu-open").focus();
  }
  $("menu-open").addEventListener("click", () => {
    if (menu.showModal) menu.showModal();
    else menu.open = true;
    $("menu-open").setAttribute("aria-expanded", "true");
  });
  menu.addEventListener("close", () => $("menu-open").setAttribute("aria-expanded", "false"));
  // Closed with Escape: the focus goes back to the menu button
  menu.addEventListener("cancel", () => setTimeout(() => $("menu-open").focus()));
  $("menu-close").addEventListener("click", () => closeMenu(true));
  // A screen opened from the menu loads its code first: the history, the settings
  async function opening(open) {
    try {
      await need(...EVERYTHING);
    } catch {
      warn(words.not_loaded);
      return;
    }
    closeMenu(false);
    open();
  }

  keepData(false);

  setUp();
  matchMedia("(prefers-color-scheme: dark)").addEventListener?.("change", setKeyInk);
  showLedger();
  showCopies();
  // On-chain and the Nostr copies are loaded now only if this register uses them; the keypad works meanwhile
  const usesChain = CHAIN_ON && (Boolean(prefs.onchain) || ledger().some((entry) => entry.method === "onchain" || entry.address));
  const usesNostr = !practice && Array.isArray(data.relays) && data.relays.length > 0
    && (staff ? Boolean(paired?.nostr) || Boolean(load(PENDING, null)) : typeof ownerSettings().nostr?.owner === "string");
  const usesPairing = Boolean(staff && load(PENDING, null));
  need(...(usesChain || usesPairing ? ["derive", "chain"] : []), ...(usesNostr || usesPairing ? ["nostr", "relays"] : []))
    .then(started, () => {
      warn(words.not_loaded);
      started();
    });
  // And the QR code, which every sale needs, when the phone has nothing else to do
  const qrSoon = () => need("qrcode").catch(() => {});
  if (window.requestIdleCallback) requestIdleCallback(qrSoon, { timeout: 3000 });
  else setTimeout(qrSoon, 1500);

  // The rest of the opening, once what this register uses is loaded
  function started() {
    // The sale that was on the screen when the page closed comes back, as long as its code has not expired
    // (an on-chain one only while on-chain is on: off, it stays in the history, where recheckChain ends it)
    const open = ledger().find((entry) => entry.id === load(OPEN, null) && entry.status === "pending"
      && (CHAIN_ON || entry.method !== "onchain"));
    if (open && now() < open.expires) {
      sale = open;
      // pay() puts the round on; the first look is at once, and it takes the round's place
      pay(open);
      check();
    } else {
      store(OPEN, null);
    }
    recheck();
    setInterval(recheckChain, CHAIN_RECHECK);
    showNostr();
    scheduleNostr(false);
    // A pairing code just opened becomes this register's: then the register starts again from it
    takePending().then((message) => {
      if (!message) return;
      setUp();
      showNostr();
      warn(message);
    });
    // The copy for when the site does not answer is made a little later: the first taps on the keypad come first
    setTimeout(keepCopy, 2000);
  }
})();

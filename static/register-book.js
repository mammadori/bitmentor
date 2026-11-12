// The register's book: the logic behind the history that stays on the phone (static/register.js). The business day,
// the day's totals, the CSV for the accountant, the merge of two histories, the encrypted copy, when a copy is due and
// which exchange rates agree. No page and no network here: the same file runs in the browser and in the tests
// (shops/test_register_book.py).
(() => {
  // A shift that runs past midnight is still the day before until this hour (SHIFT_NIGHT_HOUR in shops/business_day.py)
  const NIGHT_HOUR = 5;
  const COLLECTED = ["paid", "confirmed"];
  const FINAL = ["paid", "confirmed", "unchecked", "expired", "cancelled"];
  // Which of two copies of the same sale wins in a merge: the one that knows more about how it ended
  // «seen»: an on-chain payment in the mempool, waiting for the confirmation the merchant asked for
  const RANK = { pending: 1, cancelled: 2, expired: 2, unchecked: 3, seen: 4, confirmed: 5, paid: 6 };
  // What a finished sale keeps: the invoice, its QR code and the provider's check URL are needed only while it waits. The
  // URL stays on a staff register's sales (`keepVerify`: its copies carry it) and on the sales it reported to the owner's
  // register, which uses it to check them itself; `verified` is set only by that check, so it is never taken from a copy
  // or a file (see clean)
  // On-chain: the address, where it comes from (its path after the key and its index, to derive it again) and the transaction
  const KEPT = ["id", "at", "cents", "label", "currency", "sats", "rate", "how", "sources", "status", "hash", "till", "who",
    "method", "list", "discount", "address", "path", "index", "txid", "reported", "late", "wasWaiting", "verify",
    "verified"];
  // What a sale brought from elsewhere may say it cost: whole cents, whole sats, within bounds
  const MAX_CENTS = 1e9;
  const MAX_SATS = 21e14;
  // The encrypted copy. A new format gets a new version; old versions keep opening
  const FORMAT = "bitmentor-register-copy";
  const VERSION = 1;
  const KDF = "PBKDF2-SHA256";
  const CIPHER = "AES-256-GCM";
  // OWASP's figure for PBKDF2 with SHA-256 (2023). A copy asking for fewer or far more is not a copy made here
  const ITERATIONS = 600000;
  const MIN_ITERATIONS = 100000;
  const MAX_ITERATIONS = 10000000;
  const DAY_MS = 86400000;
  const EVERY = { daily: DAY_MS, weekly: 7 * DAY_MS, monthly: 31 * DAY_MS };
  const FORMULA = /^[=+\-@\t\r]/;

  const pad = (value) => String(value).padStart(2, "0");
  const dayOf = (date) => `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
  const drop = (object) => Object.fromEntries(Object.entries(object).filter(([, value]) => value !== undefined));
  const short = (value, size = 80) => (typeof value === "string" ? value.slice(0, size) : undefined);
  const finite = (value) => (Number.isFinite(value) ? value : undefined);
  // Whether a register's ledger is the practice's (static/practice.js, shops/practice.py)
  const ofPractice = (till) => typeof till === "string" && till.startsWith("practice:");

  // The business day of a moment, in the phone's time zone, as YYYY-MM-DD
  function businessDay(at) {
    const date = new Date(at);
    if (date.getHours() < NIGHT_HOUR) date.setDate(date.getDate() - 1);
    return dayOf(date);
  }

  const monthOf = (at) => businessDay(at).slice(0, 7);
  const days = (list) => [...new Set(list.map((entry) => businessDay(entry.at)))].sort().reverse();
  const months = (list) => [...new Set(list.map((entry) => monthOf(entry.at)))].sort().reverse();

  // How long after its end a cancelled sale is still looked at. Lightning: an invoice paid just before it expired may be
  // told only later, by a provider that did not answer at the last look; the register drops it at the first good answer
  // after the end, or after this. On-chain, with an address of its own (the end is a day after the sale): a payment
  // may wait days in the mempool for its confirmation, so it is looked at for a week, every ten minutes after its day
  const LATE_LIGHTNING = DAY_MS;
  const LATE_CHAIN = 6 * DAY_MS;

  // A cancelled sale that a customer may still have paid, or a Lightning sale that ended «not checked» because the
  // provider did not answer: the register keeps checking it (static/register.js), so it keeps what the check needs
  const stillOpen = (entry, now = Date.now()) => Number.isFinite(entry.expires) && (entry.status === "cancelled"
    ? (entry.method === "onchain" ? Number.isSafeInteger(entry.index) && now < entry.expires + LATE_CHAIN
      : Boolean(entry.verify) && now < entry.expires + LATE_LIGHTNING)
    : entry.status === "unchecked" && entry.method !== "onchain" && Boolean(entry.verify)
      && now < entry.expires + LATE_LIGHTNING);

  // Only what the history keeps of a sale: what goes into a copy
  const trim = (entry, keepVerify = false) => Object.fromEntries(KEPT.filter((key) => entry[key] !== undefined
    && (key !== "verify" || keepVerify || entry.reported) && (key !== "verified" || entry.reported)).map((key) => [key, entry[key]]));

  // A finished sale without what only a waiting one needs; a waiting one as it is
  function compact(entry, keepVerify = false) {
    if (!FINAL.includes(entry.status) || stillOpen(entry)) return entry;
    return trim(entry, keepVerify);
  }

  // A sale as the book can count it: an id, a moment, an amount and a known state
  function valid(entry) {
    return Boolean(entry) && typeof entry === "object" && !Array.isArray(entry) && typeof entry.id === "string"
      && entry.id.length > 0 && entry.id.length <= 64 && Number.isFinite(entry.at) && Number.isFinite(entry.cents)
      && typeof entry.status === "string" && Object.hasOwn(RANK, entry.status);
  }

  // A sale from elsewhere (a copy, a staff register, a relay) the book can take: a valid one with an amount in whole
  // cents and, if any, whole sats, both within bounds
  function sound(entry) {
    return valid(entry) && Number.isSafeInteger(entry.cents) && entry.cents >= 0 && entry.cents <= MAX_CENTS
      && (entry.sats === undefined || (Number.isSafeInteger(entry.sats) && entry.sats >= 0 && entry.sats <= MAX_SATS));
  }

  // A sale read from a file: only the known fields, of the right kind and size. One that was still waiting in the copy
  // cannot be checked from here any more: it is «not checked», to look for in the wallet, and marked `wasWaiting`, so
  // that a later copy of the same register saying how it ended takes its place. `reported` (the register that reported
  // it) stays: a copy brought back on a new phone keeps the sales to check in the wallet apart
  function clean(entry) {
    const waiting = ["pending", "seen"].includes(entry.status);
    const sources = Array.isArray(entry.sources)
      ? entry.sources.slice(0, 10).filter((item) => item && typeof item === "object")
        .map((item) => drop({ source: short(item.source, 40), price: finite(item.price), at: finite(item.at) }))
      : undefined;
    // An index below 2^31 - 1, so that the next one is still a key's (from 2^31 on they are hardened)
    const index = (value) => (Number.isSafeInteger(value) && value >= 0 && value < 0x7fffffff ? value : undefined);
    // The chain's path after the owner's key (register-pairing.js): one to four steps, not hardened
    const path = (value) => (typeof value === "string" && /^\d{1,10}(\/\d{1,10}){0,3}$/.test(value)
      && value.split("/").every((step) => Number(step) < 0x80000000) ? value : undefined);
    return drop({
      id: entry.id, at: entry.at, cents: entry.cents, label: short(entry.label), currency: short(entry.currency, 8),
      sats: finite(entry.sats), rate: finite(entry.rate), how: short(entry.how, 20), sources,
      status: waiting ? "unchecked" : entry.status, hash: short(entry.hash, 64),
      till: short(entry.till, 120), who: short(entry.who), method: short(entry.method, 20), list: finite(entry.list),
      discount: finite(entry.discount), address: short(entry.address, 90), path: path(entry.path),
      index: index(entry.index), txid: short(entry.txid, 64), late: entry.late === true || undefined,
      reported: short(entry.reported, 120) || undefined,
      // The provider's check URL of a Lightning sale: the register only calls it if it is the shop's own provider's
      verify: !waiting && typeof entry.verify === "string" && entry.verify.startsWith("https://") ? short(entry.verify, 400) : undefined,
      wasWaiting: waiting || (entry.status === "unchecked" && entry.wasWaiting === true) || undefined });
  }

  // Two histories become one, each sale once: for a sale in both, the copy that knows more about how it ended. Only how
  // it ended moves forward (and the transaction, when this phone has none): the amount, the sats, the method and the
  // address stay this phone's. A sale still waiting in the file never replaces this phone's own when this phone still
  // checks it (waiting here, or cancelled and still looked at for a late payment), and one the merchant marked as paid by hand never becomes «paid» from elsewhere: only
  // the merchant's own check says so.
  // `own` is this register's id: a sale of another register (a staff register's) is kept as «reported» by it, as is
  // every sale when `trusted` is false (a copy whose author this phone does not know). Reported is not checked: the
  // owner compares it with the wallet.
  // `from` is the one register a copy may speak for (a staff register's copy, signed with the key the owner's phone made
  // for that register): its sales are all reported by that register, and a sale it says is of another register (the
  // owner's own, say), or has the id of a sale here of another register, is left out and counted in `refused`. A sale
  // of the practice on a real register (`own`), or of a real register on the practice, is left out the same way.
  // Without `from`, a sale is this register's own, not reported,
  // only when `trusted` and its till is `own`; a sale of this register's own copy keeps the `reported` it had there.
  // A copy that is not `trusted` never changes a sale of `own` already here (refused). `accepted`: the copy's sales not
  // refused.
  // A sale still waiting in the file («pending») knows no more than one that ended here, and never replaces it. A sale
  // that came in while it was waiting (`wasWaiting`) is replaced by a later copy of the same register that says how it
  // ended, whatever that is: that register knew, the earlier copy did not. Only a copy trusted for that register does
  // so: `from` for a staff register's sales, a trusted copy of `own` for this register's
  function merge(mine, theirs, { own, trusted = true, from } = {}) {
    const all = new Map(mine.filter(valid).map((entry) => [entry.id, entry]));
    let added = 0;
    let updated = 0;
    let refused = 0;
    // The sales of the copy that were not refused, as the copy has them: those to compare with the owner's key
    const accepted = [];
    const reporter = (entry) => {
      if (from !== undefined) return from;
      if (own === undefined || (trusted && entry.till === own)) return undefined;
      return short(entry.till, 120) || "?";
    };
    const practice = ofPractice(own);
    for (const entry of (Array.isArray(theirs) ? theirs : []).filter(sound)) {
      // A copy of a register speaks only for it; the practice's pretend sales never come into a real register, nor a
      // real register's into the practice, whatever the way in (a file, a pull, a stranger's copy)
      if ((from !== undefined && entry.till !== from)
        || (own !== undefined && typeof entry.till === "string" && ofPractice(entry.till) !== practice)) {
        refused += 1;
        continue;
      }
      const have = all.get(entry.id);
      // Nor does it change a sale of another register that has the same id (the owner's own, say); and a copy without
      // trust (a stranger's, a file without an author) never changes this register's own sale: it could end an expired
      // one, or one to check in the wallet, as paid
      if (have && ((from !== undefined && have.till !== from)
        || (from === undefined && !trusted && own !== undefined && have.till === own))) {
        refused += 1;
        continue;
      }
      accepted.push(entry);
      if (!have) {
        const known = clean(entry);
        all.set(entry.id, drop({ ...known, reported: reporter(entry) ?? known.reported }));
        added += 1;
        continue;
      }
      const known = clean(entry);
      // How it ended, from the register that took it, in place of what an earlier copy said while it was waiting. Only a
      // copy that speaks for that register: a staff register's own (`from`), or this register's own trusted copy for
      // its own sales. A file without an author, or a stranger's copy, could otherwise end it «expired» and hide that it
      // is to check in the wallet
      const speaksFor = (from !== undefined && from === have.till)
        || (from === undefined && trusted && own !== undefined && have.till === own && known.till === own);
      const ended = have.wasWaiting === true && have.status === "unchecked" && !known.wasWaiting
        && typeof have.till === "string" && speaksFor;
      if (ended || (RANK[entry.status] > RANK[have.status] && entry.status !== "pending" && known.status !== have.status
        && !(have.status === "confirmed" && entry.status === "paid")
        && !(["seen", "unchecked"].includes(entry.status)
          && (["pending", "seen"].includes(have.status) || stillOpen(have))))) {
        all.set(entry.id, compact(drop({ ...have, status: known.status, txid: have.txid || known.txid,
          late: have.late || known.late, reported: have.reported || (reporter(entry) ?? known.reported),
          hash: have.hash || known.hash, verify: have.verify || known.verify,
          wasWaiting: known.wasWaiting })));
        updated += 1;
      }
    }
    return { list: [...all.values()].sort((a, b) => a.at - b.at), added, updated, refused, accepted };
  }

  // The day's closing: the sales of a business day, and what was collected by method and currency. «confirmed» is
  // counted, but apart too: the merchant marked it as paid, nobody checked it; so is a sale reported by another register
  function dayTotals(list, day) {
    const sales = list.filter((entry) => businessDay(entry.at) === day).sort((a, b) => a.at - b.at);
    const lines = new Map();
    const counts = {};
    for (const entry of sales) {
      counts[entry.status] = (counts[entry.status] || 0) + 1;
      if (!COLLECTED.includes(entry.status)) continue;
      const method = entry.method || "lightning";
      const key = `${method} ${entry.currency}`;
      const line = lines.get(key) || { method, currency: entry.currency, count: 0, cents: 0, sats: 0, byHand: 0, reported: 0,
        verified: 0, late: 0 };
      line.count += 1;
      line.cents += entry.cents;
      line.sats += Number.isFinite(entry.sats) ? entry.sats : 0;
      if (entry.status === "confirmed") line.byHand += 1;
      // A staff register's sale this register checked itself with the payment provider is counted apart from the ones
      // still to check in the wallet
      if (entry.reported && entry.verified === true && entry.status === "paid") line.verified += 1;
      else if (entry.reported) line.reported += 1;
      if (entry.late) line.late += 1;
      lines.set(key, line);
    }
    return { day, sales, lines: [...lines.values()], counts };
  }

  // The amount to charge with the discount for paying in bitcoin (in %), in cents, rounded to the cent
  const discounted = (cents, percent) => (percent > 0 && percent <= 100 ? Math.round((cents * (100 - percent)) / 100) : cents);

  // Excel separates the columns with the system's list separator: «;» where decimals have a comma, «,» where they have
  // a point. The language of the page chooses, as the site's CSV did
  function separators(lang) {
    const decimal = (1.5).toLocaleString(lang).includes(",") ? "," : ".";
    return { decimal, separator: decimal === "," ? ";" : "," };
  }

  // A cell the spreadsheet does not mistake for a formula, quoted when it has to be
  function cell(value, separator) {
    let content = value === undefined || value === null ? "" : String(value);
    if (FORMULA.test(content)) content = `'${content}`;
    return /["\r\n]/.test(content) || content.includes(separator) ? `"${content.replaceAll('"', '""')}"` : content;
  }

  const stamp = (at) => {
    const date = new Date(at);
    return `${dayOf(date)} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
  };

  const checked = (entry) => Boolean(entry.reported) && entry.verified === true && entry.status === "paid";

  // One row per sale, in time order, no total: the spreadsheet makes it. `columns` and `states` are the page's words;
  // `month` (YYYY-MM, of the business day) limits the rows to one month; `late` is added to the status of a sale paid
  // after it was cancelled; `label` (if given) words a sale's status in place of `states`. The column after the staff register's name says which register reported the sale: those are
  // to check in the wallet
  function csv(list, { lang, columns, states = {}, hows = {}, month = "", late = "late", verified = "verified", label }) {
    const status = label || ((entry) => states[entry.status] || entry.status);
    const { decimal, separator } = separators(lang);
    const figure = (value) => (Number.isFinite(value) ? value.toFixed(2).replace(".", decimal) : "");
    const trace = (sources) => (Array.isArray(sources) ? sources : []).map((item) => [item.source, figure(item.price),
      Number.isFinite(item.at) ? new Date(item.at).toISOString() : ""].filter(Boolean).join(" ")).join(" + ");
    const rows = list.filter((entry) => valid(entry) && (!month || monthOf(entry.at) === month)).sort((a, b) => a.at - b.at)
      .map((entry) => [stamp(entry.at), businessDay(entry.at), figure(entry.cents / 100), entry.currency,
        `${status(entry)}${entry.late ? ` (${late})` : ""}${checked(entry) ? ` (${verified})` : ""}`, entry.method || "lightning", Number.isFinite(entry.sats) ? entry.sats : "",
        figure(entry.rate), hows[entry.how] || entry.how || "", trace(entry.sources), entry.hash || "", entry.who || "",
        checked(entry) ? "" : entry.reported || "", Number.isFinite(entry.discount) && entry.discount ? entry.discount : "",
        Number.isFinite(entry.list) && entry.discount ? figure(entry.list / 100) : "", entry.address || "", entry.txid || "",
        entry.id]);
    // The BOM: without it Excel reads accents as Windows-1252
    return `﻿${[columns, ...rows].map((row) => row.map((value) => cell(value, separator)).join(separator)).join("\r\n")}\r\n`;
  }

  // Whether a copy is due. `every` is the merchant's choice; `last` the moment of the last copy (0: never); `unsaved` the
  // moment of the oldest sale after it (0: none). «closing» wants a copy once a business day with sales is over
  function backupDue(every, last, unsaved, now = Date.now()) {
    if (!unsaved || every === "manual") return false;
    if (every === "closing") return businessDay(unsaved) < businessDay(now);
    return now - (last || unsaved) > (EVERY[every] || EVERY.weekly);
  }

  // The pair of answers from different sources that agree best within the tolerance (in %), or null
  function agreeing(answers, tolerance) {
    const gap = (a, b) => (Math.abs(a.price - b.price) / Math.min(a.price, b.price)) * 100;
    let best = null;
    answers.forEach((first, i) => answers.slice(i + 1).forEach((second) => {
      if (first.source === second.source) return;
      const apart = gap(first, second);
      if (apart <= tolerance && (!best || apart < gap(...best))) best = [first, second];
    }));
    return best;
  }

  // --- The encrypted copy: PBKDF2-SHA256 for the key, AES-256-GCM for the data ---
  // The file is JSON: {format, version, kdf, iterations, cipher, salt, iv, data}, with salt, iv and data in base64. The
  // header is authenticated with the data (additional data: the JSON array of format, version, kdf, iterations, cipher,
  // salt and iv), so a file whose settings were changed does not open. Inside: {sales, closings, till, who, exported}.

  const encoder = new TextEncoder();

  function toBase64(bytes) {
    let binary = "";
    for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
    return btoa(binary);
  }

  const fromBase64 = (value) => Uint8Array.from(atob(value), (c) => c.charCodeAt(0));
  const additional = (head) => encoder.encode(JSON.stringify([head.format, head.version, head.kdf, head.iterations,
    head.cipher, head.salt, head.iv]));
  const failure = (reason) => Object.assign(new Error(reason), { reason });

  async function key(passphrase, salt, iterations) {
    const base = await crypto.subtle.importKey("raw", encoder.encode(passphrase.normalize("NFC")), "PBKDF2", false,
      ["deriveKey"]);
    return crypto.subtle.deriveKey({ name: "PBKDF2", hash: "SHA-256", salt, iterations }, base,
      { name: "AES-GCM", length: 256 }, false, ["encrypt", "decrypt"]);
  }

  async function seal(payload, passphrase, iterations = ITERATIONS) {
    const salt = crypto.getRandomValues(new Uint8Array(16));
    const iv = crypto.getRandomValues(new Uint8Array(12));
    const head = { format: FORMAT, version: VERSION, kdf: KDF, iterations, cipher: CIPHER, salt: toBase64(salt),
      iv: toBase64(iv) };
    const data = await crypto.subtle.encrypt({ name: "AES-GCM", iv, additionalData: additional(head) },
      await key(passphrase, salt, iterations), encoder.encode(JSON.stringify(payload)));
    return { ...head, data: toBase64(new Uint8Array(data)) };
  }

  // The payload of a copy, or an error whose `reason` says why not: «format» (not a copy), «version» (a newer one),
  // «passphrase» (wrong passphrase or a damaged file: AES-GCM cannot tell them apart)
  async function open(envelope, passphrase) {
    if (!envelope || typeof envelope !== "object" || envelope.format !== FORMAT) throw failure("format");
    if (envelope.version !== VERSION || envelope.kdf !== KDF || envelope.cipher !== CIPHER) throw failure("version");
    const { iterations } = envelope;
    if (!Number.isInteger(iterations) || iterations < MIN_ITERATIONS || iterations > MAX_ITERATIONS) throw failure("format");
    let salt;
    let iv;
    let data;
    try {
      salt = fromBase64(envelope.salt);
      iv = fromBase64(envelope.iv);
      data = fromBase64(envelope.data);
    } catch {
      throw failure("format");
    }
    if (salt.length < 16 || iv.length !== 12) throw failure("format");
    let plain;
    try {
      plain = await crypto.subtle.decrypt({ name: "AES-GCM", iv, additionalData: additional(envelope) },
        await key(passphrase, salt, iterations), data);
    } catch {
      throw failure("passphrase");
    }
    try {
      return JSON.parse(new TextDecoder().decode(plain));
    } catch {
      throw failure("format");
    }
  }

  globalThis.registerBook = {
    NIGHT_HOUR, ITERATIONS, FORMAT, VERSION, LATE_CHAIN, ofPractice, businessDay, monthOf, days, months, compact, stillOpen, trim, valid, sound, clean,
    merge, dayTotals,
    csv, backupDue, agreeing, discounted, seal, open };
})();

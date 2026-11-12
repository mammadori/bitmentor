// The pairing of a staff register: what the owner's phone puts in the QR code that opens a register on an employee's
// phone (static/staff-pair.js), and how that phone reads it back (static/register.js). The code carries where the money
// goes (the shop's Lightning address and, for on-chain, the key of the shop's chain or a fixed address), who the shop is and the owner's preferences. It travels in the fragment of the opening link (#pair=…),
// which the browser never sends to the site: the site does not see the key nor the preferences.
// No page and no network here: the same file runs in the browser and in the tests (shops/test_register_chain.py).
//
// Version 1, JSON with short names, in base64url after «1.»:
//   v 1 · r the staff register's id · ln the Lightning address · oc on-chain, {x: the chain's extended key, p: its path} or {a: address}
//   (absent: Lightning only) · cf wait for a confirmation above this many cents · d discount in % · cu currency ·
//   ps price sources · s the shop {n name, c colour #rrggbb, t town} · m a line of the owner on the register ·
//   dr true when the owner lets the registers remove the discount on a sale (absent: they cannot)
// Version 2, «2.»: version 1 and no, the copies on Nostr (docs/features/nostr-copies.md): {p the owner's Nostr public
//   key, k the private key the owner's phone made for this register, r the public relays the owner added (of those
//   in community.yml)}. A code without Nostr is made as version 1.
// Anything else, or anything out of bounds, and the whole code is refused: a register is paired with all of it or none.
// Only a public relay the community no longer lists (it changes community.yml after the pairing) is dropped instead:
// the pairing kept on the phone is read again at every opening, and must not be lost for it.
(() => {
  const VERSION = 2;
  const MAX_TEXT = 2400;
  const KEYS = ["v", "r", "ln", "oc", "cf", "d", "cu", "ps", "s", "m", "dr"];
  const HEX64 = /^[0-9a-f]{64}$/;
  const MAX_RELAYS = 8;
  const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
  // As shops/register.py: a user of letters, digits and ._+- and a domain name
  const LIGHTNING = /^[a-z0-9._+-]{1,64}@(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$/;
  const BASE58 = /^[1-9A-HJ-NP-Za-km-z]{100,120}$/;
  const ADDRESS = /^[0-9A-Za-z]{14,90}$/;
  const CURRENCY = /^[A-Z]{3}$/;
  const COLOUR = /^#[0-9a-fA-F]{6}$/;
  const MAX_CENTS = 1e9;
  const MAX_CHILD = 0x7fffffff;
  // Every register of the shop, the owner's and the staff ones, hands out the addresses of one chain of the
  // owner's key, its receive chain …/0 unless the owner set another path in the settings, with the index given by the
  // site (one count per shop and chain: shops/chain_count.py), so that no two registers use the same address and the
  // payments show in the owner's wallet. A path is one to four steps after the key, not hardened (an extended public
  // key cannot derive hardened ones), each below 2^31
  const PATH = /^(0|[1-9][0-9]{0,9})(\/(0|[1-9][0-9]{0,9})){0,3}$/;
  const DEFAULT_PATH = "0";
  // The steps of a path, or null when it is not one
  function steps(path) {
    if (typeof path !== "string" || !PATH.test(path)) return null;
    const found = path.split("/").map(Number);
    return found.every((step) => step <= MAX_CHILD) ? found : null;
  }

  const failure = (reason) => Object.assign(new Error(reason), { reason });
  const plain = (value) => Boolean(value) && typeof value === "object" && !Array.isArray(value);
  const only = (object, keys) => Object.keys(object).every((key) => keys.includes(key));
  // A short text without control characters
  const text = (value, size) => typeof value === "string" && value.length <= size && !/[\u0000-\u001f\u007f]/.test(value);
  const integer = (value, least, most) => Number.isSafeInteger(value) && value >= least && value <= most;

  function toBase64Url(bytes) {
    let binary = "";
    for (const b of bytes) binary += String.fromCharCode(b);
    return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  function fromBase64Url(value) {
    if (!/^[A-Za-z0-9_-]*$/.test(value)) throw failure("format");
    const padded = value.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (value.length % 4)) % 4);
    return Uint8Array.from(atob(padded), (c) => c.charCodeAt(0));
  }

  // The pairing as the register uses it, or an error whose `reason` says why not. `sources` are the price sources the
  // site knows: a pairing cannot open the register to others
  function check(data, sources, relays = []) {
    if (!plain(data)) throw failure("format");
    if (Number.isInteger(data.v) && data.v > VERSION) throw failure("version");
    if (data.v !== 1 && data.v !== 2) throw failure("format");
    if (!only(data, data.v === 2 ? [...KEYS, "no"] : KEYS)) throw failure("format");
    if (typeof data.r !== "string" || !UUID.test(data.r)) throw failure("format");
    if (typeof data.ln !== "string" || !LIGHTNING.test(data.ln)) throw failure("format");
    let onchain = null;
    if (data.oc !== undefined) {
      if (!plain(data.oc)) throw failure("format");
      if (only(data.oc, ["x", "p"]) && typeof data.oc.x === "string" && BASE58.test(data.oc.x) && steps(data.oc.p)) {
        // The shop's chain: the key is already the chain's (the owner's key at the path), the register takes …/n of it
        onchain = { key: data.oc.x, path: data.oc.p };
      } else if (only(data.oc, ["a"]) && typeof data.oc.a === "string" && ADDRESS.test(data.oc.a)) {
        onchain = { address: data.oc.a };
      } else {
        throw failure("format");
      }
    }
    if (!integer(data.cf, 0, MAX_CENTS) || !integer(data.d, 0, 100)) throw failure("format");
    if (typeof data.cu !== "string" || !CURRENCY.test(data.cu)) throw failure("format");
    if (!Array.isArray(data.ps) || data.ps.length < 2 || data.ps.length > 10 || new Set(data.ps).size !== data.ps.length
        || !data.ps.every((name) => typeof name === "string" && sources.includes(name))) throw failure("format");
    const shop = data.s;
    if (!plain(shop) || !only(shop, ["n", "c", "t"]) || !text(shop.n, 120) || !shop.n.trim()
        || (shop.c !== undefined && !(typeof shop.c === "string" && COLOUR.test(shop.c)))
        || (shop.t !== undefined && !text(shop.t, 100))) throw failure("format");
    if (data.m !== undefined && !text(data.m, 140)) throw failure("format");
    if (data.dr !== undefined && data.dr !== true) throw failure("format");
    let nostr = null;
    if (data.v === 2) {
      const copy = data.no;
      if (!plain(copy) || !only(copy, ["p", "k", "r"]) || typeof copy.p !== "string" || !HEX64.test(copy.p)
          || typeof copy.k !== "string" || !HEX64.test(copy.k) || !Array.isArray(copy.r) || copy.r.length > MAX_RELAYS
          || new Set(copy.r).size !== copy.r.length || !copy.r.every((url) => typeof url === "string" && url.length <= 300)) {
        throw failure("format");
      }
      // A relay the community no longer lists is left out, not the pairing: the register keeps sending to the others
      nostr = { owner: copy.p, secret: copy.k, relays: copy.r.filter((url) => relays.includes(url)) };
    }
    return { register: data.r, lightning: data.ln, onchain, confirmAbove: data.cf, discount: data.d,
      discountRemovable: data.dr === true, currency: data.cu,
      sources: [...data.ps], shop: { name: shop.n, colour: shop.c || "", town: shop.t || "" }, note: data.m || "", nostr };
  }

  // The text of the code from what the owner's phone knows, checked as the employee's phone will check it. `relays` are
  // the public relays of community.yml
  function encode(pairing, sources, relays = []) {
    const data = { v: pairing.nostr ? 2 : 1, r: pairing.register, ln: pairing.lightning };
    if (pairing.onchain?.key && pairing.onchain.path !== undefined) data.oc = { x: pairing.onchain.key, p: pairing.onchain.path };
    else if (pairing.onchain?.address) data.oc = { a: pairing.onchain.address };
    Object.assign(data, { cf: pairing.confirmAbove, d: pairing.discount, cu: pairing.currency, ps: pairing.sources,
      s: { n: pairing.shop.name, ...(pairing.shop.colour ? { c: pairing.shop.colour } : {}),
        ...(pairing.shop.town ? { t: pairing.shop.town } : {}) } });
    if (pairing.note) data.m = pairing.note;
    if (pairing.discountRemovable === true) data.dr = true;
    if (pairing.nostr) {
      data.no = { p: pairing.nostr.owner, k: pairing.nostr.secret,
        r: (pairing.nostr.relays || []).filter((url) => relays.includes(url)) };
    }
    check(data, sources, relays);
    const result = `${data.v}.` + toBase64Url(new TextEncoder().encode(JSON.stringify(data)));
    if (result.length > MAX_TEXT) throw failure("size");
    return result;
  }

  // The pairing in a code, or an error: «format» (not a pairing, or a damaged one), «version» (made by a newer register)
  function decode(value, sources, relays = []) {
    if (typeof value !== "string" || value.length > MAX_TEXT) throw failure("format");
    const dot = value.indexOf(".");
    const version = Number(value.slice(0, dot));
    if (dot < 1 || !Number.isInteger(version)) throw failure("format");
    if (version > VERSION) throw failure("version");
    if (value.slice(0, dot + 1) !== "1." && value.slice(0, dot + 1) !== "2.") throw failure("format");
    let data;
    try {
      data = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(fromBase64Url(value.slice(dot + 1))));
    } catch {
      throw failure("format");
    }
    if (Number.isInteger(data?.v) && data.v > VERSION) throw failure("version");
    if (data?.v !== version) throw failure("format");
    return check(data, sources, relays);
  }

  globalThis.registerPairing = { VERSION, DEFAULT_PATH, steps, encode, decode };
})();

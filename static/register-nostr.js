// The register's copies on Nostr: the sales of a month (the owner's register) or of a day (a staff register) in a
// replaceable event of kind 30078 (NIP-78), compressed and encrypted with NIP-44 version 2 to the owner's Nostr public
// key, signed with the register's own key, made on the phone and never sent to the site. Only the owner's private key,
// or the key of the register that wrote it, opens one (docs/features/nostr-copies.md).
// No page and no network here: the relays are static/register-relays.js. The same file runs in the browser and in the
// tests (shops/test_register_nostr.py: the official NIP-44 test vectors and BIP340's).
// Needs static/register-derive.js first: the curve's arithmetic is shared. ChaCha20 (RFC 8439) is written here, as
// WebCrypto has none; SHA-256, HMAC and the random bytes are WebCrypto's. BigInt arithmetic does not take constant time:
// the keys here sign and open the copies of a register, they do not hold money.
(() => {
  const { P, N, G, mod, power, add, multiply, affine, toNumber, fromNumber, join } = globalThis.registerDerive.curve;
  const KIND = 30078;
  // The `d` tag: PREFIX:<register>:<YYYY-MM or YYYY-MM-DD>[:<part>]
  const PREFIX = "bitmentor-register";
  // What a copy carries, before compression. A new format gets a new version; old ones keep opening
  const VERSION = 1;
  // NIP-44 version 2 encrypts 1 to 65535 bytes: a month too big for one event is split in parts below this
  const MAX_PLAIN = 65535;
  const PART_BYTES = 60000;
  // A copy opened is never allowed to grow past this when uncompressed: a small event cannot fill the phone
  const MAX_UNPACKED = 4 * 1024 * 1024;
  const MAX_SALES = 20000;
  const HEX64 = /^[0-9a-f]{64}$/;
  const PERIOD = /^\d{4}-\d{2}(?:-\d{2})?$/;
  const TILL = /^[A-Za-z0-9_-]{1,64}$/;

  const encoder = new TextEncoder();
  const failure = (reason) => Object.assign(new Error(reason), { reason });
  const hex = (bytes) => Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  function fromHex(text, length) {
    if (typeof text !== "string" || !/^(?:[0-9a-fA-F]{2})*$/.test(text) || (length && text.length !== length * 2)) {
      throw failure("hex");
    }
    return Uint8Array.from(text.match(/../g) || [], (pair) => parseInt(pair, 16));
  }
  const random = (length) => crypto.getRandomValues(new Uint8Array(length));

  function toBase64(bytes) {
    let binary = "";
    for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
    return btoa(binary);
  }
  function fromBase64(text) {
    if (typeof text !== "string" || !/^[A-Za-z0-9+/]*={0,2}$/.test(text) || text.length % 4) throw failure("base64");
    return Uint8Array.from(atob(text), (c) => c.charCodeAt(0));
  }

  // --- Hashes: WebCrypto ---
  const sha256 = async (bytes) => new Uint8Array(await crypto.subtle.digest("SHA-256", bytes));
  async function hmac(keyBytes, ...parts) {
    const key = await crypto.subtle.importKey("raw", keyBytes, { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
    return new Uint8Array(await crypto.subtle.sign("HMAC", key, join(...parts)));
  }
  async function tagged(tag, ...parts) {
    const prefix = await sha256(encoder.encode(tag));
    return sha256(join(prefix, prefix, ...parts));
  }

  // --- Keys: x-only public keys of BIP340 ---
  function secretNumber(secret) {
    const d = toNumber(typeof secret === "string" ? fromHex(secret, 32) : secret);
    if (d <= 0n || d >= N) throw failure("key");
    return d;
  }
  // The point of an x-only public key, with the even y
  function liftX(bytes) {
    if (bytes.length !== 32) throw failure("key");
    const x = toNumber(bytes);
    if (x >= P) throw failure("key");
    const c = mod(x ** 3n + 7n);
    const y = power(c, (P + 1n) / 4n);
    if (mod(y * y) !== c) throw failure("key");
    return [x, y & 1n ? P - y : y, 1n];
  }
  const xOnly = (point) => fromNumber(affine(point)[0], 32);

  function publicKey(secret) {
    return hex(xOnly(multiply(secretNumber(secret), G)));
  }

  // A new private key, in hex: 32 random bytes that are a valid scalar
  function newSecret() {
    for (;;) {
      const bytes = random(32);
      const d = toNumber(bytes);
      if (d > 0n && d < N) return hex(bytes);
    }
  }

  // --- BIP340 signatures ---
  async function sign(secret, message, aux = random(32)) {
    const d0 = secretNumber(secret);
    const [px, py] = affine(multiply(d0, G));
    const d = py & 1n ? N - d0 : d0;
    const pBytes = fromNumber(px, 32);
    const masked = toNumber(await tagged("BIP0340/aux", aux)) ^ d;
    const k0 = mod(toNumber(await tagged("BIP0340/nonce", fromNumber(masked, 32), pBytes, message)), N);
    if (k0 === 0n) throw failure("nonce");
    const [rx, ry] = affine(multiply(k0, G));
    const k = ry & 1n ? N - k0 : k0;
    const rBytes = fromNumber(rx, 32);
    const e = mod(toNumber(await tagged("BIP0340/challenge", rBytes, pBytes, message)), N);
    const signature = join(rBytes, fromNumber(mod(k + e * d, N), 32));
    // As BIP340 recommends: a signature is checked before it leaves, so a fault in the arithmetic never gives out a
    // wrong one (which could reveal the key)
    if (!(await verify(pBytes, message, signature))) throw failure("signature");
    return signature;
  }

  async function verify(publicBytes, message, signature) {
    if (signature.length !== 64) return false;
    let point;
    try {
      point = liftX(publicBytes);
    } catch {
      return false;
    }
    const r = toNumber(signature.subarray(0, 32));
    const s = toNumber(signature.subarray(32));
    if (r >= P || s >= N) return false;
    const e = mod(toNumber(await tagged("BIP0340/challenge", signature.subarray(0, 32), publicBytes, message)), N);
    const [x, y, z] = point;
    const sum = add(multiply(s, G), multiply(N - e, [x, y, z]));
    if (sum[2] === 0n) return false;
    const [rx, ry] = affine(sum);
    return !(ry & 1n) && rx === r;
  }

  // --- ChaCha20, RFC 8439: 32-byte key, 12-byte nonce, a 32-bit block counter ---
  function chacha20(key, nonce, data, counter = 0) {
    const words = (bytes) => Array.from({ length: bytes.length / 4 }, (_, i) => (bytes[i * 4] | (bytes[i * 4 + 1] << 8)
      | (bytes[i * 4 + 2] << 16) | (bytes[i * 4 + 3] << 24)) >>> 0);
    const start = [0x61707865, 0x3320646e, 0x79622d32, 0x6b206574, ...words(key), counter, ...words(nonce)];
    const x = new Uint32Array(16);
    const out = new Uint8Array(data.length);
    const rotate = (v, n) => (v << n) | (v >>> (32 - n));
    const quarter = (a, b, c, d) => {
      x[a] += x[b]; x[d] = rotate(x[d] ^ x[a], 16);
      x[c] += x[d]; x[b] = rotate(x[b] ^ x[c], 12);
      x[a] += x[b]; x[d] = rotate(x[d] ^ x[a], 8);
      x[c] += x[d]; x[b] = rotate(x[b] ^ x[c], 7);
    };
    for (let at = 0; at < data.length; at += 64) {
      x.set(start);
      for (let i = 0; i < 10; i++) {
        quarter(0, 4, 8, 12); quarter(1, 5, 9, 13); quarter(2, 6, 10, 14); quarter(3, 7, 11, 15);
        quarter(0, 5, 10, 15); quarter(1, 6, 11, 12); quarter(2, 7, 8, 13); quarter(3, 4, 9, 14);
      }
      for (let i = 0; i < 16 && at + i * 4 < data.length; i++) {
        const word = (x[i] + start[i]) >>> 0;
        for (let j = 0; j < 4 && at + i * 4 + j < data.length; j++) out[at + i * 4 + j] = data[at + i * 4 + j] ^ ((word >>> (8 * j)) & 255);
      }
      start[12] = (start[12] + 1) >>> 0;
    }
    return out;
  }

  // --- NIP-44 version 2 ---
  // The key two parties share: HKDF-extract(salt "nip44-v2", the x of the ECDH point, unhashed)
  async function conversationKey(secret, publicHex) {
    const shared = xOnly(multiply(secretNumber(secret), liftX(fromHex(publicHex, 32))));
    return hmac(encoder.encode("nip44-v2"), shared);
  }

  // HKDF-expand(conversation key, nonce, 76): ChaCha20's key and nonce, and the HMAC's key
  async function messageKeys(conversation, nonce) {
    if (conversation.length !== 32 || nonce.length !== 32) throw failure("key");
    const blocks = [];
    let previous = new Uint8Array(0);
    for (let i = 1; i <= 3; i++) {
      previous = await hmac(conversation, previous, nonce, Uint8Array.of(i));
      blocks.push(previous);
    }
    const okm = join(...blocks);
    return { chachaKey: okm.slice(0, 32), chachaNonce: okm.slice(32, 44), hmacKey: okm.slice(44, 76) };
  }

  function paddedLength(length) {
    if (length <= 32) return 32;
    const next = 2 ** (Math.floor(Math.log2(length - 1)) + 1);
    const chunk = next <= 256 ? 32 : next / 8;
    return chunk * (Math.floor((length - 1) / chunk) + 1);
  }

  function pad(plaintext) {
    const bytes = encoder.encode(plaintext);
    if (bytes.length < 1 || bytes.length > MAX_PLAIN) throw failure("length");
    const out = new Uint8Array(2 + paddedLength(bytes.length));
    out[0] = bytes.length >> 8;
    out[1] = bytes.length & 255;
    out.set(bytes, 2);
    return out;
  }

  function unpad(padded) {
    const length = (padded[0] << 8) | padded[1];
    if (length < 1 || length > MAX_PLAIN || padded.length !== 2 + paddedLength(length)) throw failure("padding");
    try {
      return new TextDecoder("utf-8", { fatal: true }).decode(padded.subarray(2, 2 + length));
    } catch {
      throw failure("padding");
    }
  }

  async function encrypt(plaintext, conversation, nonce = random(32)) {
    const keys = await messageKeys(conversation, nonce);
    const ciphertext = chacha20(keys.chachaKey, keys.chachaNonce, pad(plaintext));
    const mac = await hmac(keys.hmacKey, nonce, ciphertext);
    return toBase64(join(Uint8Array.of(2), nonce, ciphertext, mac));
  }

  // The plaintext, or an error whose `reason` says why not: «version», «length», «mac», «padding»
  async function decrypt(payload, conversation) {
    if (typeof payload !== "string" || !payload.length || payload[0] === "#") throw failure("version");
    if (payload.length < 132 || payload.length > 87472) throw failure("length");
    let data;
    try {
      data = fromBase64(payload);
    } catch {
      throw failure("length");
    }
    if (data.length < 99 || data.length > 65603) throw failure("length");
    if (data[0] !== 2) throw failure("version");
    const nonce = data.subarray(1, 33);
    const ciphertext = data.subarray(33, data.length - 32);
    const keys = await messageKeys(conversation, nonce);
    const expected = await hmac(keys.hmacKey, nonce, ciphertext);
    const mac = data.subarray(data.length - 32);
    let difference = 0;
    for (let i = 0; i < 32; i++) difference |= expected[i] ^ mac[i];
    if (difference) throw failure("mac");
    return unpad(chacha20(keys.chachaKey, keys.chachaNonce, ciphertext));
  }

  // --- npub and nsec: bech32 (NIP-19) ---
  const CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l";
  function polymod(values) {
    const GEN = [0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3];
    let chk = 1;
    for (const value of values) {
      const top = chk >>> 25;
      chk = ((chk & 0x1ffffff) << 5) ^ value;
      for (let i = 0; i < 5; i++) if ((top >>> i) & 1) chk ^= GEN[i];
    }
    return chk >>> 0;
  }
  const expand = (hrp) => [...[...hrp].map((c) => c.charCodeAt(0) >> 5), 0, ...[...hrp].map((c) => c.charCodeAt(0) & 31)];
  function regroup(values, from, to, padding) {
    let acc = 0;
    let bits = 0;
    const out = [];
    for (const value of values) {
      acc = (acc << from) | value;
      bits += from;
      while (bits >= to) {
        bits -= to;
        out.push((acc >> bits) & ((1 << to) - 1));
      }
    }
    if (padding && bits) out.push((acc << (to - bits)) & ((1 << to) - 1));
    else if (!padding && (bits >= from || ((acc << (to - bits)) & ((1 << to) - 1)))) throw failure("bech32");
    return out;
  }
  function toBech32(hrp, bytes) {
    const data = regroup(bytes, 8, 5, true);
    const check = polymod([...expand(hrp), ...data, 0, 0, 0, 0, 0, 0]) ^ 1;
    const sum = Array.from({ length: 6 }, (_, i) => (check >>> (5 * (5 - i))) & 31);
    return `${hrp}1${[...data, ...sum].map((v) => CHARSET[v]).join("")}`;
  }
  // The 32 bytes, in hex, of an npub or nsec (`hrp`), or an error «bech32»
  function fromBech32(text, hrp) {
    const value = typeof text === "string" ? text.trim().toLowerCase() : "";
    if (!value.startsWith(`${hrp}1`) || value.length > 100) throw failure("bech32");
    const data = [...value.slice(hrp.length + 1)].map((c) => CHARSET.indexOf(c));
    if (data.length < 7 || data.includes(-1) || polymod([...expand(hrp), ...data]) !== 1) throw failure("bech32");
    const bytes = regroup(data.slice(0, -6), 5, 8, false);
    if (bytes.length !== 32) throw failure("bech32");
    return hex(bytes);
  }
  // An npub (or 64 hex characters) as a public key in hex, checked to be on the curve, or an error «key»
  function readPublic(text) {
    let found;
    try {
      found = HEX64.test(String(text).trim().toLowerCase()) ? String(text).trim().toLowerCase() : fromBech32(text, "npub");
      liftX(fromHex(found, 32));
    } catch {
      throw failure("key");
    }
    return found;
  }
  // An nsec (or 64 hex characters) as a private key in hex, or an error «key»
  function readSecret(text) {
    try {
      const value = String(text).trim();
      const found = HEX64.test(value.toLowerCase()) ? value.toLowerCase() : fromBech32(value, "nsec");
      secretNumber(found);
      return found;
    } catch {
      throw failure("key");
    }
  }
  const npub = (publicHex) => toBech32("npub", fromHex(publicHex, 32));

  // --- Events, NIP-01 ---
  async function eventId(event) {
    return hex(await sha256(encoder.encode(JSON.stringify([0, event.pubkey, event.created_at, event.kind, event.tags,
      event.content]))));
  }

  async function signEvent(template, secret) {
    const event = { pubkey: publicKey(secret), created_at: template.created_at, kind: template.kind, tags: template.tags,
      content: template.content };
    event.id = await eventId(event);
    event.sig = hex(await sign(secret, fromHex(event.id, 32)));
    return event;
  }

  // Whether an event is well made, its id is its content's and the signature is its author's
  async function checkEvent(event) {
    if (!event || typeof event !== "object" || !HEX64.test(event.id) || !HEX64.test(event.pubkey)
        || !/^[0-9a-f]{128}$/.test(event.sig) || !Number.isSafeInteger(event.created_at) || event.kind !== KIND
        || !Array.isArray(event.tags) || !event.tags.every((tag) => Array.isArray(tag) && tag.every((v) => typeof v === "string"))
        || typeof event.content !== "string") return false;
    if (await eventId(event) !== event.id) return false;
    return verify(fromHex(event.pubkey, 32), fromHex(event.id, 32), fromHex(event.sig, 64));
  }

  const tag = (event, name) => event.tags.find((item) => item[0] === name)?.[1];

  // --- What a copy carries: JSON, compressed with gzip where the browser can ---
  async function streamed(bytes, stream, most) {
    const reader = new Blob([bytes]).stream().pipeThrough(stream).getReader();
    const chunks = [];
    let size = 0;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.length;
      if (size > most) {
        await reader.cancel();
        throw failure("size");
      }
      chunks.push(value);
    }
    return join(...chunks);
  }

  async function pack(object) {
    const json = encoder.encode(JSON.stringify(object));
    if (typeof CompressionStream === "function") {
      return `${VERSION}z.${toBase64(await streamed(json, new CompressionStream("gzip"), Infinity))}`;
    }
    return `${VERSION}j.${new TextDecoder().decode(json)}`;
  }

  async function unpack(text) {
    let json;
    if (text.startsWith(`${VERSION}z.`)) {
      if (typeof DecompressionStream !== "function") throw failure("browser");
      try {
        json = await streamed(fromBase64(text.slice(3)), new DecompressionStream("gzip"), MAX_UNPACKED);
      } catch (error) {
        throw failure(error.reason === "size" ? "size" : "format");
      }
    } else if (text.startsWith(`${VERSION}j.`)) {
      json = encoder.encode(text.slice(3));
    } else {
      throw failure(/^\d+[a-z]\./.test(text) ? "version" : "format");
    }
    try {
      return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(json));
    } catch {
      throw failure("format");
    }
  }

  // --- The copies ---
  const dTag = (till, period, part) => `${PREFIX}:${till}:${period}${part ? `:${part}` : ""}`;

  function readD(value) {
    const parts = typeof value === "string" ? value.split(":") : [];
    if (parts[0] !== PREFIX || parts.length < 3 || parts.length > 4 || !TILL.test(parts[1]) || !PERIOD.test(parts[2])
        || (parts.length === 4 && !/^[1-9]\d{0,2}$/.test(parts[3]))) return null;
    return { till: parts[1], period: parts[2], part: parts[3] ? Number(parts[3]) : 0 };
  }

  // The events of one register's period (a month, YYYY-MM, or a day, YYYY-MM-DD), signed with the register's key and
  // encrypted to the owner. Part 0 is the d tag without a part; a period too big for one event is split in 2, 4, 8…
  // parts of consecutive sales, the same parts at every rewrite, so the newer replaces the older on the relays
  async function copyEvents({ sales, till, who = "", period, owner, secret, createdAt }) {
    if (!TILL.test(till) || !PERIOD.test(period)) throw failure("format");
    const conversation = await conversationKey(secret, owner);
    for (let count = 1; count <= 512; count *= 2) {
      const size = Math.ceil(sales.length / count) || 1;
      const texts = [];
      for (let part = 0; part < count; part++) {
        texts.push(await pack({ v: VERSION, till, who, period, sales: sales.slice(part * size, (part + 1) * size) }));
      }
      if (texts.some((value) => encoder.encode(value).length > PART_BYTES)) continue;
      const events = [];
      for (const [part, value] of texts.entries()) {
        events.push(await signEvent({ created_at: createdAt, kind: KIND, content: await encrypt(value, conversation),
          tags: [["d", dTag(till, period, part)], ["p", owner]] }, secret));
      }
      return events;
    }
    throw failure("size");
  }

  // What a copy says, checked: {author, till, who, period, sales, at}, or an error whose `reason` says why not. `secret`
  // is the owner's private key, or the private key of the register that wrote it with `peer` the owner's public key
  // `conversation`: the conversation key itself (the owner's phone keeps it for each staff register it paired, in place of
  // that register's private key: it reads that register's copies and cannot sign as it), used in place of `secret`
  async function openCopy(event, secret, peer = event?.pubkey, conversation = null) {
    if (!(await checkEvent(event))) throw failure("signature");
    const where = readD(tag(event, "d"));
    if (!where) throw failure("format");
    let plain;
    try {
      plain = await decrypt(event.content, conversation || await conversationKey(secret, peer));
    } catch (error) {
      throw failure(error.reason === "version" ? "version" : "decrypt");
    }
    const data = await unpack(plain);
    if (!data || typeof data !== "object" || Array.isArray(data)) throw failure("format");
    if (Number.isInteger(data.v) && data.v > VERSION) throw failure("version");
    if (data.v !== VERSION || data.till !== where.till || data.period !== where.period || !Array.isArray(data.sales)
        || data.sales.length > MAX_SALES) throw failure("format");
    return { author: event.pubkey, till: where.till, period: where.period, part: where.part, at: event.created_at,
      who: typeof data.who === "string" ? data.who.slice(0, 80) : "", sales: data.sales };
  }

  // The register's name in the d tag: an HMAC of the register's id under the key the register and the owner share
  // (NIP-44's conversation key of the register's key and the owner's). The shop's id is public, a plain hash of it would
  // tell a relay which shop a copy is of; this one only the register and the owner can work out. The owner does it from
  // the nsec and the author of the copy, on any phone: `secret` and `peer` are the register's private key and the
  // owner's public one, or the owner's private key and the register's public one
  async function tillTag(ledger, secret, peer, conversation = null) {
    return hex(await hmac(conversation || await conversationKey(secret, peer), encoder.encode(`${PREFIX}:${ledger}`))).slice(0, 32);
  }

  // The filter that asks the relays for the copies encrypted to an owner, since a moment (seconds)
  const filter = (owner, since) => ({ kinds: [KIND], "#p": [owner], ...(since ? { since } : {}), limit: 500 });

  globalThis.registerNostr = {
    KIND, PREFIX, VERSION, hex, fromHex, publicKey, newSecret, sign, verify, chacha20, conversationKey,
    messageKeys, paddedLength, encrypt, decrypt, readPublic, readSecret, npub, eventId, signEvent, checkEvent, pack,
    unpack, readD, copyEvents, openCopy, filter, tillTag };
})();

// On-chain addresses, derived in the merchant's own browser from the extended public key that stays on the phone
// (static/register.js) or in the pairing of a staff register (static/register-pairing.js): the site never sees the key.
// No dependencies: secp256k1 with BigInt (only public points: addition and scalar multiplication), BIP32 public child
// derivation, HMAC-SHA512 and SHA-256 from WebCrypto, RIPEMD-160, bech32/bech32m and Base58Check. The same file runs in
// the browser and in the tests (shops/test_register_chain.py).
// Covers: keys xpub/zpub (mainnet) and tpub/vpub (signet), native segwit (P2WPKH, as BIP84), and ypub/upub (P2SH-P2WPKH,
// as BIP49); an xpub or a tpub is taken as native segwit. Not covered: P2TR, legacy P2PKH and descriptors for derived
// addresses. A fixed address may be of any standard kind.
(() => {
  const P = 2n ** 256n - 2n ** 32n - 977n;
  const N = 0xfffffffffffffffffffffffffffffffebaaedce6af48a03bbfd25e8cd0364141n;
  const G = [0x79be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798n,
    0x483ada7726a3c4655da4fbfc0e1108a8fd17b448a68554199c47d08ffb10d4b8n, 1n];
  const mod = (a, m = P) => ((a % m) + m) % m;
  const power = (base, exponent, m = P) => {
    let result = 1n;
    base = mod(base, m);
    for (; exponent > 0n; exponent >>= 1n, base = (base * base) % m) if (exponent & 1n) result = (result * base) % m;
    return result;
  };
  const invert = (a) => power(a, P - 2n);

  // Jacobian points [X, Y, Z] on y^2 = x^3 + 7; Z = 0 is the point at infinity
  const INFINITY = [0n, 1n, 0n];
  function double([x, y, z]) {
    if (z === 0n || y === 0n) return INFINITY;
    const s = mod(4n * x * y * y);
    const m = mod(3n * x * x);
    const x2 = mod(m * m - 2n * s);
    return [x2, mod(m * (s - x2) - 8n * y ** 4n), mod(2n * y * z)];
  }
  function add(a, b) {
    if (a[2] === 0n) return b;
    if (b[2] === 0n) return a;
    const z1 = a[2] * a[2], z2 = b[2] * b[2];
    const u1 = mod(a[0] * z2), u2 = mod(b[0] * z1);
    const s1 = mod(a[1] * z2 * b[2]), s2 = mod(b[1] * z1 * a[2]);
    if (u1 === u2) return s1 === s2 ? double(a) : INFINITY;
    const h = mod(u2 - u1), r = mod(s2 - s1);
    const h2 = mod(h * h), h3 = mod(h2 * h), v = mod(u1 * h2);
    const x3 = mod(r * r - h3 - 2n * v);
    return [x3, mod(r * (v - x3) - s1 * h3), mod(h * a[2] * b[2])];
  }
  function multiply(k, point) {
    let result = INFINITY;
    for (; k > 0n; k >>= 1n, point = double(point)) if (k & 1n) result = add(result, point);
    return result;
  }
  function affine([x, y, z]) {
    if (z === 0n) throw new Error("infinity");
    const i = invert(z), i2 = (i * i) % P;
    return [mod(x * i2), mod(y * i2 * i)];
  }
  function decompress(bytes) {
    if (bytes.length !== 33 || (bytes[0] !== 2 && bytes[0] !== 3)) throw new Error("key");
    const x = toNumber(bytes.slice(1));
    let y = power(x ** 3n + 7n, (P + 1n) / 4n);
    if (mod(y * y) !== mod(x ** 3n + 7n)) throw new Error("key");
    if ((y & 1n) !== BigInt(bytes[0] & 1)) y = P - y;
    return [x, y, 1n];
  }
  function compress([x, y]) {
    const out = new Uint8Array(33);
    out[0] = 2 + Number(y & 1n);
    out.set(fromNumber(x, 32), 1);
    return out;
  }

  function toNumber(bytes) {
    let n = 0n;
    for (const b of bytes) n = (n << 8n) | BigInt(b);
    return n;
  }
  function fromNumber(n, length) {
    const out = new Uint8Array(length);
    for (let i = length - 1; i >= 0; i--, n >>= 8n) out[i] = Number(n & 255n);
    return out;
  }
  const join = (...parts) => {
    const out = new Uint8Array(parts.reduce((sum, part) => sum + part.length, 0));
    let at = 0;
    for (const part of parts) {
      out.set(part, at);
      at += part.length;
    }
    return out;
  };
  const hex = (bytes) => Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  const sha256 = async (bytes) => new Uint8Array(await crypto.subtle.digest("SHA-256", bytes));
  async function hmac512(keyBytes, data) {
    const key = await crypto.subtle.importKey("raw", keyBytes, { name: "HMAC", hash: "SHA-512" }, false, ["sign"]);
    return new Uint8Array(await crypto.subtle.sign("HMAC", key, data));
  }

  // RIPEMD-160
  const RL = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 7, 4, 13, 1, 10, 6, 15, 3, 12, 0, 9, 5, 2, 14, 11, 8,
    3, 10, 14, 4, 9, 15, 8, 1, 2, 7, 0, 6, 13, 11, 5, 12, 1, 9, 11, 10, 0, 8, 12, 4, 13, 3, 7, 15, 14, 5, 6, 2,
    4, 0, 5, 9, 7, 12, 2, 10, 14, 1, 3, 8, 11, 6, 15, 13];
  const RR = [5, 14, 7, 0, 9, 2, 11, 4, 13, 6, 15, 8, 1, 10, 3, 12, 6, 11, 3, 7, 0, 13, 5, 10, 14, 15, 8, 12, 4, 9, 1, 2,
    15, 5, 1, 3, 7, 14, 6, 9, 11, 8, 12, 2, 10, 0, 4, 13, 8, 6, 4, 1, 3, 11, 15, 0, 5, 12, 2, 13, 9, 7, 10, 14,
    12, 15, 10, 4, 1, 5, 8, 7, 6, 2, 13, 14, 0, 3, 9, 11];
  const SL = [11, 14, 15, 12, 5, 8, 7, 9, 11, 13, 14, 15, 6, 7, 9, 8, 7, 6, 8, 13, 11, 9, 7, 15, 7, 12, 15, 9, 11, 7, 13, 12,
    11, 13, 6, 7, 14, 9, 13, 15, 14, 8, 13, 6, 5, 12, 7, 5, 11, 12, 14, 15, 14, 15, 9, 8, 9, 14, 5, 6, 8, 6, 5, 12,
    9, 15, 5, 11, 6, 8, 13, 12, 5, 12, 13, 14, 11, 8, 5, 6];
  const SR = [8, 9, 9, 11, 13, 15, 15, 5, 7, 7, 8, 11, 14, 14, 12, 6, 9, 13, 15, 7, 12, 8, 9, 11, 7, 7, 12, 7, 6, 15, 13, 11,
    9, 7, 15, 11, 8, 6, 6, 14, 12, 13, 5, 14, 13, 13, 7, 5, 15, 5, 8, 11, 14, 14, 6, 14, 6, 9, 12, 9, 12, 5, 15, 8,
    8, 5, 12, 9, 12, 5, 14, 6, 8, 13, 6, 5, 15, 13, 11, 11];
  const KL = [0, 0x5a827999, 0x6ed9eba1, 0x8f1bbcdc, 0xa953fd4e];
  const KR = [0x50a28be6, 0x5c4dd124, 0x6d703ef3, 0x7a6d76e9, 0];
  const rol = (x, n) => (x << n) | (x >>> (32 - n));
  function mix(round, x, y, z) {
    switch (round) {
      case 0: return x ^ y ^ z;
      case 1: return (x & y) | (~x & z);
      case 2: return (x | ~y) ^ z;
      case 3: return (x & z) | (y & ~z);
      default: return x ^ (y | ~z);
    }
  }
  function ripemd160(message) {
    const length = message.length;
    const padded = new Uint8Array(((length + 8) >> 6 << 6) + 64);
    padded.set(message);
    padded[length] = 0x80;
    const view = new DataView(padded.buffer);
    view.setUint32(padded.length - 8, (length * 8) >>> 0, true);
    view.setUint32(padded.length - 4, Math.floor(length / 0x20000000), true);
    let h = [0x67452301, 0xefcdab89, 0x98badcfe, 0x10325476, 0xc3d2e1f0];
    for (let offset = 0; offset < padded.length; offset += 64) {
      const x = Array.from({ length: 16 }, (_, i) => view.getUint32(offset + i * 4, true));
      let [al, bl, cl, dl, el] = h;
      let [ar, br, cr, dr, er] = h;
      for (let j = 0; j < 80; j++) {
        const round = j >> 4;
        let t = (rol((al + mix(round, bl, cl, dl) + x[RL[j]] + KL[round]) | 0, SL[j]) + el) | 0;
        al = el; el = dl; dl = rol(cl, 10); cl = bl; bl = t;
        t = (rol((ar + mix(4 - round, br, cr, dr) + x[RR[j]] + KR[round]) | 0, SR[j]) + er) | 0;
        ar = er; er = dr; dr = rol(cr, 10); cr = br; br = t;
      }
      const t = (h[1] + cl + dr) | 0;
      h = [t, (h[2] + dl + er) | 0, (h[3] + el + ar) | 0, (h[4] + al + br) | 0, (h[0] + bl + cr) | 0];
    }
    const out = new Uint8Array(20);
    h.forEach((word, i) => new DataView(out.buffer).setUint32(i * 4, word >>> 0, true));
    return out;
  }
  const hash160 = async (bytes) => ripemd160(await sha256(bytes));

  // Base58Check and bech32
  const BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";
  const failure = (reason) => Object.assign(new Error(reason), { reason });
  async function checksum(payload) {
    return (await sha256(await sha256(payload))).slice(0, 4);
  }
  async function base58check(payload) {
    let n = toNumber(join(payload, await checksum(payload)));
    let text = "";
    for (; n > 0n; n /= 58n) text = BASE58[Number(n % 58n)] + text;
    for (const b of payload) {
      if (b !== 0) break;
      text = "1" + text;
    }
    return text;
  }
  // The payload of a Base58Check text, or an error: «format» (not Base58, wrong length), «checksum» (a typo)
  async function base58decode(text, length) {
    if (typeof text !== "string" || !text || text.length > 120) throw failure("format");
    let n = 0n;
    let zeros = 0;
    for (const c of text) {
      const i = BASE58.indexOf(c);
      if (i < 0) throw failure("format");
      n = n * 58n + BigInt(i);
    }
    for (const c of text) {
      if (c !== "1") break;
      zeros += 1;
    }
    const size = n === 0n ? 0 : Math.ceil(n.toString(16).length / 2);
    const bytes = join(new Uint8Array(zeros), fromNumber(n, size));
    if (bytes.length !== length + 4) throw failure("format");
    if (hex(await checksum(bytes.slice(0, length))) !== hex(bytes.slice(length))) throw failure("checksum");
    return bytes.slice(0, length);
  }
  const CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l";
  const BECH32 = 1;
  const BECH32M = 0x2bc830a3;
  function polymod(values) {
    let chk = 1;
    for (const v of values) {
      const top = chk >>> 25;
      chk = ((chk & 0x1ffffff) << 5) ^ v;
      [0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3].forEach((g, i) => { if ((top >>> i) & 1) chk ^= g; });
    }
    return chk >>> 0;
  }
  const expand = (hrp) => [...Array.from(hrp, (c) => c.charCodeAt(0) >> 5), 0, ...Array.from(hrp, (c) => c.charCodeAt(0) & 31)];
  function regroup(values, from, to, pad) {
    let acc = 0;
    let bits = 0;
    const out = [];
    for (const value of values) {
      acc = (acc << from) | value;
      bits += from;
      for (; bits >= to; bits -= to) out.push((acc >> (bits - to)) & ((1 << to) - 1));
      acc &= (1 << bits) - 1;
    }
    if (pad && bits) out.push((acc << (to - bits)) & ((1 << to) - 1));
    if (!pad && (bits >= from || acc)) return null;
    return out;
  }
  function segwit(hrp, version, program) {
    const words = [version, ...regroup(program, 8, 5, true)];
    const check = polymod([...expand(hrp), ...words, 0, 0, 0, 0, 0, 0]) ^ (version ? BECH32M : BECH32);
    const sum = Array.from({ length: 6 }, (_, i) => (check >>> (5 * (5 - i))) & 31);
    return `${hrp}1${[...words, ...sum].map((w) => CHARSET[w]).join("")}`;
  }
  // {version, program} of a segwit address of this human part, or null (BIP173 and BIP350)
  function segwitDecode(text, hrp) {
    if (text !== text.toLowerCase() && text !== text.toUpperCase()) return null;
    const lower = text.toLowerCase();
    const split = lower.lastIndexOf("1");
    if (lower.slice(0, split) !== hrp || lower.length > 90 || lower.length - split - 1 < 6) return null;
    const words = [...lower.slice(split + 1)].map((c) => CHARSET.indexOf(c));
    if (words.includes(-1) || !words.length) return null;
    const version = words[0];
    const constant = polymod([...expand(hrp), ...words]);
    if (version > 16 || constant !== (version ? BECH32M : BECH32)) return null;
    const program = regroup(words.slice(1, -6), 5, 8, false);
    if (!program || program.length < 2 || program.length > 40) return null;
    if (version === 0 && program.length !== 20 && program.length !== 32) return null;
    return { version, program };
  }

  // BIP32 public child, non hardened
  async function child(chain, publicKey, index) {
    if (!Number.isInteger(index) || index < 0 || index >= 0x80000000) throw failure("index");
    const i = await hmac512(chain, join(publicKey, fromNumber(BigInt(index), 4)));
    const left = toNumber(i.slice(0, 32));
    if (left >= N) throw failure("index");
    const point = add(multiply(left, G), decompress(publicKey));
    return { chain: i.slice(32), key: compress(affine(point)) };
  }

  // The networks: the version of the extended key, the human part of segwit addresses and the Base58 prefixes
  const NETWORKS = {
    mainnet: { hrp: "bc", pubkeyHash: 0x00, scriptHash: 0x05,
      versions: { "0488b21e": "p2wpkh", "04b24746": "p2wpkh", "049d7cb2": "p2sh" } },
    signet: { hrp: "tb", pubkeyHash: 0x6f, scriptHash: 0xc4,
      versions: { "043587cf": "p2wpkh", "045f1cf6": "p2wpkh", "044a5262": "p2sh" } },
  };
  const net = (network) => NETWORKS[network] || (() => { throw failure("network"); })();

  // An extended public key of this network: {kind, version, depth, parent, number, chain, key}, or an error whose
  // `reason` says why not: «format», «checksum», «network» (a key of the other network), «version» (a private key or
  // an unknown kind), «key» (not a point of the curve)
  async function parseKey(text, network) {
    const data = await base58decode(String(text).trim(), 78);
    const version = hex(data.slice(0, 4));
    const kind = net(network).versions[version];
    if (!kind) {
      const other = Object.entries(NETWORKS).some(([name, item]) => name !== network && item.versions[version]);
      throw failure(other ? "network" : "version");
    }
    const key = data.slice(45);
    try {
      decompress(key);
    } catch {
      throw failure("key");
    }
    return { kind, version: data.slice(0, 4), depth: data[4], parent: data.slice(5, 9), number: data.slice(9, 13),
      chain: data.slice(13, 45), key };
  }

  function serialize(parsed) {
    return base58check(join(parsed.version, Uint8Array.of(parsed.depth), parsed.parent, parsed.number, parsed.chain,
      parsed.key));
  }

  // The extended public key of the non-hardened child `index`, written with the same version: what a staff register
  // gets (…/k), so that its addresses are …/k/n of the owner's key and no index is shared between phones
  async function childKey(text, index, network) {
    const parent = await parseKey(text, network);
    if (parent.depth >= 255) throw failure("index");
    const { chain, key } = await child(parent.chain, parent.key, index);
    const fingerprint = (await hash160(parent.key)).slice(0, 4);
    return serialize({ ...parent, depth: parent.depth + 1, parent: fingerprint, number: fromNumber(BigInt(index), 4),
      chain, key });
  }

  // The address of `path` (non-hardened indexes) under the key: [0, n] is the n-th receive address of an account key,
  // [n] the n-th address of a staff register's key
  async function addressAt(text, path, network) {
    const parsed = await parseKey(text, network);
    let node = { chain: parsed.chain, key: parsed.key };
    for (const index of path) node = await child(node.chain, node.key, index);
    const keyHash = await hash160(node.key);
    const { hrp, scriptHash } = net(network);
    if (parsed.kind === "p2wpkh") return segwit(hrp, 0, keyHash);
    const script = await hash160(join(Uint8Array.of(0, 20), keyHash));
    return base58check(join(Uint8Array.of(scriptHash), script));
  }

  // Whether `text` is a well-written address of this network: {ok: true, address, type} or {ok: false, reason}, with
  // reason «network» for an address of the other network. Segwit v0 and v1 (taproot), P2PKH and P2SH
  async function checkAddress(text, network) {
    const value = String(text).trim();
    const { hrp, pubkeyHash, scriptHash } = net(network);
    const found = segwitDecode(value, hrp);
    if (found) {
      if (found.version > 1 || (found.version === 1 && found.program.length !== 32)) return { ok: false, reason: "version" };
      return { ok: true, address: value.toLowerCase(), type: found.version ? "p2tr" : found.program.length === 20 ? "p2wpkh" : "p2wsh" };
    }
    const others = Object.entries(NETWORKS).filter(([name]) => name !== network).map(([, item]) => item);
    if (others.some((item) => segwitDecode(value, item.hrp))) return { ok: false, reason: "network" };
    try {
      const data = await base58decode(value, 21);
      if (data[0] === pubkeyHash) return { ok: true, address: value, type: "p2pkh" };
      if (data[0] === scriptHash) return { ok: true, address: value, type: "p2sh" };
      return { ok: false, reason: others.some((item) => [item.pubkeyHash, item.scriptHash].includes(data[0])) ? "network" : "version" };
    } catch (error) {
      return { ok: false, reason: error.reason === "checksum" ? "checksum" : "format" };
    }
  }

  // What the merchant pasted as the on-chain destination: {kind: "key", key, first} with the first receive address to
  // compare with the wallet, {kind: "address", address}, or {kind: null, reason}
  async function destination(text, network) {
    const value = String(text || "").trim();
    if (/^[xyztuv]pub/.test(value)) {
      try {
        await parseKey(value, network);
        return { kind: "key", key: value, first: await addressAt(value, [0, 0], network) };
      } catch (error) {
        return { kind: null, reason: error.reason || "format" };
      }
    }
    if (/^[xyztuv]prv/.test(value)) return { kind: null, reason: "private" };
    const found = await checkAddress(value, network);
    return found.ok ? { kind: "address", address: found.address } : { kind: null, reason: found.reason };
  }

  // The curve's arithmetic is shared with the Nostr copies (static/register-nostr.js), which also need it for private keys
  const curve = { P, N, G, INFINITY, mod, power, add, multiply, affine, toNumber, fromNumber, join };
  globalThis.registerDerive = { parseKey, childKey, addressAt, checkAddress, destination, hex, curve };
})();

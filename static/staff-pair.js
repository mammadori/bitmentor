// The pairing code of a staff register, drawn on the owner's phone (templates/shops/staff_code.html). The site gives the
// one-time link, the shop's Lightning address, the register's child number and the suggestions; this page adds what only
// the owner's phone knows, the settings of the owner's register (static/register.js): the on-chain key, of which the code
// carries only the shop's chain (the owner's key at the receive path), or the fixed address, and the preferences. All of it goes in the link's
// fragment (#pair=…), which the browser never sends to the site (static/register-pairing.js).
// With the owner's Nostr public key in those settings, the code carries a private key made here for the register, with
// which it signs its sales sent to the owner; this phone keeps the public one, to know that register's copies apart
// from anyone else's, and the conversation key the register and the owner share (the owner's register reads its copies with it, with
// no private key to type; it cannot sign as the register) (static/register-nostr.js, docs/features/nostr-copies.md).
(() => {
  const $ = (id) => document.getElementById(id);
  const box = $("pairing-data");
  if (!box) return;
  const data = JSON.parse(box.textContent);
  const words = data.texts;
  const say = (template, values) => template.replace(/\{(\w+)\}/g, (all, key) => (key in values ? values[key] : all));

  function saved() {
    try {
      const value = JSON.parse(localStorage.getItem(`register-settings:${data.shop_id}`));
      return value && typeof value === "object" && !Array.isArray(value) ? value : {};
    } catch {
      return {};
    }
  }

  // The same picture as the register's QR codes (static/register.js)
  // The Nostr keys this phone made for the shop's staff registers: {register: [public keys, newest last]}
  const TILLS = `register-nostr-tills:${data.shop_id}`;
  // With each public key, the conversation key it shares with the owner's (NIP-44): the owner's register reads that
  // register's copies with it, without the owner's private key. It cannot sign as the register: the register's private
  // key is only in the code. {public key: {owner, key}}; keys no register's list names any more are dropped
  const TILL_KEYS = `register-nostr-tillkeys:${data.shop_id}`;
  function remember(register, publicKey, conversation, owner) {
    let tills = {};
    try {
      tills = JSON.parse(localStorage.getItem(TILLS)) || {};
    } catch { /* none yet */ }
    if (!tills || typeof tills !== "object" || Array.isArray(tills)) tills = {};
    const known = Array.isArray(tills[register]) ? tills[register].filter((key) => key !== publicKey) : [];
    tills[register] = [...known, publicKey].slice(-10);
    localStorage.setItem(TILLS, JSON.stringify(tills));
    let keys = {};
    try {
      keys = JSON.parse(localStorage.getItem(TILL_KEYS)) || {};
    } catch { /* none yet */ }
    if (!keys || typeof keys !== "object" || Array.isArray(keys)) keys = {};
    keys[publicKey] = { owner, key: conversation };
    const listed = new Set(Object.values(tills).flat());
    localStorage.setItem(TILL_KEYS, JSON.stringify(Object.fromEntries(Object.entries(keys).filter(([key]) => listed.has(key)))));
  }

  function qrImage(content) {
    const modules = qrcode(content);
    const size = modules.length + 8;
    let path = "";
    modules.forEach((row, y) => row.forEach((dark, x) => { if (dark) path += `M${x + 4},${y + 4}h1v1h-1z`; }));
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${size} ${size}" shape-rendering="crispEdges">`
      + `<rect width="${size}" height="${size}" fill="#fff"/><path d="${path}" fill="#000"/></svg>`;
    return `data:image/svg+xml,${encodeURIComponent(svg)}`;
  }

  function line(content) {
    const item = document.createElement("li");
    item.textContent = content;
    return item;
  }

  function failed(message) {
    $("pair-error").textContent = message;
    $("pair-error").hidden = false;
    $("pair-qr").hidden = true;
  }

  async function draw() {
    const mine = saved();
    // The owner's register remembers the Lightning address it takes payments into: a different one here is not paired
    if (mine.lightning && mine.lightning !== data.lightning) {
      failed(say(words.pin_changed, { before: mine.lightning, after: data.lightning }));
      return;
    }
    const lines = [say(words.lightning, { address: data.lightning })];
    let onchain = null;
    // On-chain at the register is an experimental feature (`chain_on` in the page's data, shops/staff.py): off, the code
    // carries no on-chain key or address and the summary says nothing of it, whatever this phone's settings hold
    if (data.chain_on !== true) {
      // Lightning only
    } else if (typeof mine.onchain?.key === "string") {
      // The shop's chain, the owner's path (the receive chain unless the settings say another): the register gets the
      // chain's key, and the site gives every register its indexes from one count (static/register.js, claimShared)
      const path = typeof mine.onchain.path === "string" ? mine.onchain.path : registerPairing.DEFAULT_PATH;
      const steps = registerPairing.steps(path);
      if (!steps) throw new Error("path");
      let key = mine.onchain.key;
      for (const step of steps) key = await registerDerive.childKey(key, step, data.network);
      onchain = { key, path };
      // The shop's count of this chain on the site, opened by the owner (shops/chain_count.py): a staff register only
      // counts on a chain the owner opened. Named by a SHA-256 of the chain's key, as the register does
      if (data.chain_open) {
        const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(`bitmentor-chain:${key}`));
        const chain = [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
        const response = await fetch(data.chain_open.url, { method: "POST", credentials: "same-origin", redirect: "error",
          headers: { "Content-Type": "application/json", "X-CSRFToken": data.chain_open.csrf },
          body: JSON.stringify({ chain, open: true }) });
        if (!response.ok) throw new Error("chain");
      }
      lines.push(say(words.chain_key, { path: `${path}/n`, address: await registerDerive.addressAt(key, [0], data.network) }));
    } else if (typeof mine.onchain?.address === "string") {
      onchain = { address: mine.onchain.address };
      lines.push(say(words.chain_fixed, { address: mine.onchain.address }));
    } else {
      lines.push(words.lightning_only);
    }
    const chosen = (Array.isArray(mine.sources) ? mine.sources : []).filter((name) => data.sources.includes(name));
    const preferences = {
      confirmAbove: Number.isSafeInteger(mine.confirmAbove) ? mine.confirmAbove : data.confirm_above,
      discount: Number.isInteger(mine.discount) ? mine.discount : data.discount,
      discountRemovable: mine.discountRemovable === true,
      currency: typeof mine.currency === "string" && mine.currency ? mine.currency : data.currency,
      sources: chosen.length >= 2 ? chosen : data.sources,
    };
    let nostr = null;
    if (typeof mine.nostr?.owner === "string" && data.relays.length) {
      const secret = registerNostr.newSecret();
      nostr = { owner: mine.nostr.owner, secret, relays: (Array.isArray(mine.nostr.relays) ? mine.nostr.relays : [])
        .filter((url) => data.public_relays.includes(url)) };
      lines.push(say(words.nostr, { owner: registerNostr.npub(mine.nostr.owner) }));
    }
    const code = registerPairing.encode({
      register: data.register, lightning: data.lightning, onchain, ...preferences,
      shop: { ...data.shop, ...(typeof mine.colour === "string" && mine.colour ? { colour: mine.colour } : {}) },
      note: typeof mine.note === "string" ? mine.note : "", nostr }, data.sources, data.public_relays);
    // Only a code drawn is a key given: this phone trusts its copies from now on
    if (nostr) {
      const conversation = registerNostr.hex(await registerNostr.conversationKey(nostr.secret, nostr.owner));
      remember(data.register, registerNostr.publicKey(nostr.secret), conversation, nostr.owner);
    }
    // The threshold for a block is named only with on-chain (the sentence without it has no {above})
    lines.push(say(words.preferences, { discount: preferences.discount, currency: preferences.currency,
      above: `${(preferences.confirmAbove / 100).toFixed(2)} ${preferences.currency}` }));
    const url = `${data.url}#pair=${code}`;
    $("pair-qr").src = qrImage(url);
    $("pair-qr").hidden = false;
    $("pair-link").textContent = url;
    $("pair-summary").replaceChildren(...lines.map(line));
  }

  // The link copied with one tap where the browser allows it, otherwise selected for the phone's own Copy; the button
  // and a status line (for screen readers) say it was copied
  const copy = $("pair-copy");
  if (copy) {
    copy.hidden = !navigator.clipboard?.writeText;
    copy.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText($("pair-link").textContent);
        copy.textContent = copy.dataset.copied;
        $("pair-say").textContent = copy.dataset.copied;
      } catch {
        window.getSelection?.().selectAllChildren($("pair-link"));
      }
    });
  }

  draw().catch(() => failed(words.failed));
})();

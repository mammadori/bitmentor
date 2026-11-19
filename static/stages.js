// The stages of the owner's panel on a phone with sales (shops/stages.py). The site knows only the days since the first
// sale; the sales are on the phone, in the register's own ledger of each shop (static/register.js, register-ledger:<id>).
// Here they are read, never written or sent:
// - when a shop has at least the community's number of paid or confirmed sales for a later stage, that stage shows in
//   place of the current one, on this phone only; another phone, a computer or a browser without the ledger keeps what
//   the days say;
// - the quick start of the training: what is left of it, counted as the register counts it (quickLeft);
// - a link into the closed part of the panel (#first-day-<id>, #break-<id>…) opens the folds around its target.
// Without JavaScript nothing changes.
(() => {
  const COUNTED = ["paid", "confirmed"];
  // What uses up the quick start, as static/register.js counts it
  const QUICK = ["seen", "paid", "confirmed"];

  function ledgerOf(storage, shop) {
    let list;
    try {
      list = JSON.parse(storage.getItem(`register-ledger:${shop}`));
    } catch {
      return [];
    }
    return Array.isArray(list) ? list.filter((entry) => entry && typeof entry === "object") : [];
  }

  // The paid or confirmed sales of `shop` in this browser's ledger: 0 without one, or with one that cannot be read
  function salesOf(storage, shop) {
    return ledgerOf(storage, shop).filter((entry) => COUNTED.includes(entry.status)).length;
  }

  // The cents of the quick start still left on this phone, or null without a ledger for the shop
  function quickLeft(storage, shop, cents, currency) {
    const list = ledgerOf(storage, shop);
    if (!list.length) return null;
    const used = list.filter((entry) => QUICK.includes(entry.status) && entry.currency === currency)
      .reduce((total, entry) => total + (Number.isFinite(entry.cents) ? entry.cents : 0), 0);
    return Math.max(0, cents - used);
  }

  // For each shop on the panel, the latest stage its sales reach takes the place of the current one
  function promote(root, storage) {
    for (const card of root.querySelectorAll("[data-stage-shop]")) {
      const offers = [...card.querySelectorAll("[data-stage-sales]")];
      if (!offers.length) continue;
      const count = salesOf(storage, card.dataset.stageShop);
      const reached = offers.filter((offer) => {
        const needed = Number(offer.dataset.stageSales);
        return Number.isInteger(needed) && needed > 0 && count >= needed;
      }).pop();
      if (!reached) continue;
      for (const other of card.querySelectorAll("[data-stage-now], [data-stage-sales]")) other.hidden = other !== reached;
    }
  }

  function showQuick(root, storage) {
    for (const line of root.querySelectorAll("[data-quick-shop]")) {
      const cents = Number(line.dataset.quickCents);
      if (!Number.isInteger(cents) || cents <= 0) continue;
      const left = quickLeft(storage, line.dataset.quickShop, cents, line.dataset.quickCurrency);
      if (left === null) continue;
      line.textContent = left > 0
        ? line.dataset.text.replace("{left}", `${(left / 100).toFixed(2)} ${line.dataset.quickCurrency}`.trim())
        : line.dataset.done;
      line.hidden = false;
    }
  }

  // The element a fragment names, when it sits in a closed <details>: every closed one around it opens, and it comes into
  // view. True when something was opened
  function reveal(root, hash) {
    if (!hash || hash.length < 2) return false;
    let id;
    try {
      id = decodeURIComponent(hash.slice(1));
    } catch {
      return false;
    }
    const target = root.getElementById(id);
    if (!target) return false;
    let opened = false;
    for (let node = target.parentElement; node; node = node.parentElement) {
      if (node.tagName === "DETAILS" && !node.open) {
        node.open = true;
        opened = true;
      }
    }
    if (opened) target.scrollIntoView?.();
    return opened;
  }

  globalThis.panelStages = { salesOf, quickLeft, promote, showQuick, reveal };
  if (typeof document !== "undefined") {
    let storage = null;
    try {
      storage = window.localStorage;
    } catch {
      // No storage in this browser: the days decide
    }
    if (storage) {
      try {
        promote(document, storage);
        showQuick(document, storage);
      } catch {
        // A ledger that cannot be read changes nothing
      }
    }
    reveal(document, location.hash);
    window.addEventListener("hashchange", () => reveal(document, location.hash));
  }
})();

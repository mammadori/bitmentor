// The practice register (shops/practice.py). Loaded before register.js, which stays the real one: here the rate, the
// invoice and the payment check never leave the phone. This file gives register.js a pretend backend
// (`window.registerBackend`), whose payments the panel of the pretend customer decides. Nothing is sent anywhere.
(() => {
  const $ = (id) => document.getElementById(id);
  const ledger = document.body.dataset.ledger;
  const data = JSON.parse($("practice-data").textContent);
  const say = data.texts;
  const STATE = `practice-state:${ledger}`;
  const DONE = `practice-done:${ledger}`;
  // Which opening of the page made a sale: a sale that comes back after a reload is the switched-off phone
  const LOAD = Array.from(crypto.getRandomValues(new Uint8Array(8)), (b) => b.toString(16).padStart(2, "0")).join("");
  const LATENCY = 250;
  const LIFETIME = 15 * 60000;
  // One cent is ten sats: the numbers on the screen stay easy to follow
  const PRICE = 100000;
  // The exercises the page lists: the on-chain one only while on-chain at the register, an experimental feature, is on
  // (templates/shops/practice.html)
  const EXERCISES = ["take", "offline", "expired", "phone", "onchain"]
    .filter((key) => document.querySelector(`[data-exercise="${key}"]`));
  // On-chain is here only as the remedy of one exercise: the customer does not understand the Lightning code. The
  // address is a word, not an address: no wallet can pay it
  const CHAIN = "practice";
  const wait = (ms) => new Promise((resolve) => { setTimeout(resolve, ms); });

  function read(key, fallback) {
    try {
      return JSON.parse(localStorage.getItem(key)) ?? fallback;
    } catch {
      return fallback;
    }
  }

  function write(key, value) {
    try {
      if (value === null) localStorage.removeItem(key);
      else localStorage.setItem(key, JSON.stringify(value));
    } catch { /* without memory the practice still works, it only forgets on reload */ }
  }

  // Kept in memory too: a phone without localStorage must not lose the sale between two calls
  let memory = read(STATE, null);
  // A memory spoiled by hand or by an old version (not an object, sales not a map) starts the practice afresh
  const sound = (state) => state && typeof state === "object" && !Array.isArray(state) && state.sales
    && typeof state.sales === "object" && !Array.isArray(state.sales);
  const load = () => (sound(memory) ? memory : { sales: {}, last: null, offline: false });
  function save(state) {
    memory = state;
    write(STATE, state);
  }

  function done() {
    const list = read(DONE, []);
    return Array.isArray(list) ? list : [];
  }

  function mark(exercise) {
    const list = done();
    if (list.includes(exercise) || !EXERCISES.includes(exercise)) return;
    list.push(exercise);
    write(DONE, list);
    showDone();
    const name = document.querySelector(`[data-exercise="${exercise}"] strong`).textContent;
    const finished = say.done.replace("{name}", name);
    // After what the customer did and what to do now, never in place of it
    tell(`${$("customer-say").textContent} ${finished}${EXERCISES.every((key) => list.includes(key)) ? ` ${say.all}` : ""}`.trim());
  }

  // What the register has just learned, as an exercise asks it
  function noticed(sale) {
    if (sale.paid && sale.sats === 3500) mark("take");
    if (sale.paid && sale.whileOffline) mark("offline");
    if (sale.paid && sale.load !== LOAD) mark("phone");
    if (sale.gone) mark("expired");
    if (sale.paid && sale.chain) mark("onchain");
  }

  const offline = () => Object.assign(new Error(say.offline), { shown: true });

  window.registerBackend = {
    async quote() {
      await wait(LATENCY);
      return { price: PRICE, sources: [{ source: "practice", price: PRICE }], how: "agreed" };
    },
    async invoiceFor(msat) {
      await wait(LATENCY);
      const state = load();
      if (state.offline) throw offline();
      const pr = `practice:${Date.now()}:${LOAD}`;
      state.sales = Object.fromEntries([...Object.entries(state.sales).slice(-9),
        [pr, { sats: msat / 1000, paid: false, gone: false, load: LOAD, expires: Date.now() + LIFETIME }]]);
      state.last = pr;
      save(state);
      update();
      tell(say.ready);
      return { pr, qr: data.qr, verify: "practice", expires: Date.now() + LIFETIME, hash: "" };
    },
    async settled(entry) {
      await wait(LATENCY);
      const state = load();
      if (state.offline) throw offline();
      const sale = state.sales[entry.pr];
      if (!sale) return false;
      noticed(sale);
      return sale.paid;
    },
    practiceChain: CHAIN,
    practiceQr: data.qr,
    // «OK, on-chain» shows only after the customer did not understand the Lightning code
    chainOffered: () => Boolean(load().chainOffered),
    // The explorer of the practice. Without a sale (the register prepares the address) an on-chain sale starts; with
    // one, its pretend payment once the customer paid: the amount asked, confirmed now
    async chainTxs(address, entry) {
      await wait(LATENCY);
      const state = load();
      if (state.offline) throw offline();
      if (!entry) {
        const key = `chain:${Date.now()}`;
        state.sales = Object.fromEntries([...Object.entries(state.sales).slice(-9),
          [key, { chain: true, paid: false, gone: false, load: LOAD, expires: Date.now() + LIFETIME,
          // Each pretend payment its own transaction: the register never takes one another sale took
          txid: Array.from(crypto.getRandomValues(new Uint8Array(32)), (x) => x.toString(16).padStart(2, "0")).join("") }]]);
        state.last = key;
        save(state);
        update();
        tell(say.chain_ready);
        return [];
      }
      const sale = state.sales[state.last];
      if (!sale?.chain || !sale.paid) return [];
      noticed(sale);
      return [{ txid: sale.txid, status: { confirmed: true, block_time: Math.floor(Date.now() / 1000) },
        vin: [], vout: [{ scriptpubkey_address: address, value: entry.sats }] }];
    },
    // The customer who leaves without paying: the code is over at once, without waiting a quarter of an hour
    expired(entry) {
      const sale = load().sales[entry.pr];
      return Date.now() > entry.expires || Boolean(sale?.gone);
    },
  };

  // The register checks at once, as when the screen comes back on, instead of waiting for its next round
  const nudge = () => document.dispatchEvent(new Event("visibilitychange"));

  function tell(message) {
    $("customer-say").textContent = message;
  }

  const MOVES = {
    pay(sale, state) {
      tell(say.pay);
      sale.whileOffline = state.offline;
      sale.paid = true;
    },
    none(sale) {
      tell(say.none);
      sale.gone = true;
    },
    down(sale, state) {
      tell(say.down);
      state.offline = true;
    },
    // The customer's wallet does not read the Lightning code: on-chain becomes possible, after Cancel
    confused(sale, state) {
      tell(say.confused);
      state.chainOffered = true;
      document.getElementById("chain-ok").hidden = false;
    },
    up(sale, state) {
      tell(say.up);
      state.offline = false;
    },
  };

  // Which moves make sense now: only what a customer could do with the sale on the screen
  function possible(sale, state) {
    const moves = new Set([state.offline ? "up" : "down"]);
    const open = sale && !sale.paid && !sale.gone && Date.now() < sale.expires;
    if (open) ["pay", ...(sale.chain ? [] : ["none", "phone", "confused"])].forEach((move) => moves.add(move));
    return moves;
  }

  function update() {
    const state = load();
    const moves = possible(state.sales[state.last], state);
    for (const button of document.querySelectorAll("[data-move]")) {
      const hide = !moves.has(button.dataset.move);
      // The focus does not get lost with a button that disappears: it goes to the first move still there
      if (hide && document.activeElement === button) {
        button.hidden = true;
        document.querySelector("[data-move]:not([hidden])")?.focus();
      }
      button.hidden = hide;
    }
    $("practice-again").hidden = !state.last && !done().length;
  }

  function showDone() {
    const list = done();
    const marks = document.querySelector(".exercise-list").dataset;
    for (const item of document.querySelectorAll("[data-exercise]")) {
      const finished = list.includes(item.dataset.exercise);
      item.classList.toggle("exercise-done", finished);
      const state = item.querySelector(".exercise-state");
      if (state) state.textContent = finished ? marks.done : marks.todo;
    }
    document.querySelector(".exercise-real").classList.toggle("exercise-ready", EXERCISES.every((key) => list.includes(key)));
    // Above the keypad, the task at hand: whoever opens the practice sees at once that there is one
    const now = $("exercise-now");
    if (now?.dataset?.text) {
      const next = EXERCISES.findIndex((key) => !list.includes(key));
      now.textContent = next < 0 ? now.dataset.all
        : now.dataset.text.replace("{number}", next + 1).replace("{total}", EXERCISES.length)
          .replace("{title}", document.querySelector(`[data-exercise="${EXERCISES[next]}"] strong`).textContent);
    }
  }

  document.querySelector(".moves").addEventListener("click", (event) => {
    const button = event.target.closest("[data-move]");
    if (!button || button.hidden) return;
    const state = load();
    const sale = state.sales[state.last];
    if (button.dataset.move === "phone") {
      tell(say.phone);
      write("practice-phone", { at: Date.now(), path: location.pathname });
      location.reload();
      return;
    }
    MOVES[button.dataset.move](sale, state);
    save(state);
    update();
    nudge();
  });

  $("practice-again").addEventListener("click", () => {
    for (const key of [STATE, DONE, `register-ledger:${ledger}`, `register-open:${ledger}`, `register-closings:${ledger}`,
      `register-copy:${ledger}`]) write(key, null);
    memory = null;
    location.reload();
  });

  $("customer").hidden = false;
  const phone = read("practice-phone", null);
  write("practice-phone", null);
  // Reopened after «The phone switches off»: the panel says what just happened
  tell(phone && phone.path === location.pathname && Date.now() - phone.at < 60000 ? say.phone : say.first);
  showDone();
  update();
})();

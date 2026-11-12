// On-chain at the register: what a public explorer's answer says about a sale. The register asks the explorer the
// community chose (mempool.space's API, or an instance of the same API) for the transactions of the sale's address,
// and this file reads the answer. No page and no network here: the same file runs in the browser and in the tests
// (shops/test_register_chain.py). The site never sees the address nor the answer.
(() => {
  // A transaction id: 64 hexadecimal characters
  const TXID = /^[0-9a-f]{64}$/;
  // An explorer's answer is a list of transactions (GET /address/<address>/txs): newest first, the ones in the mempool
  // before the confirmed ones, at most 50 + 25. Anything else is not an answer
  const MAX_TXS = 200;

  // The address's transactions in a list the register can trust: [{txid, confirmed, sats, spends, time}] with the sats paid to
  // this address by each transaction and the outputs it spends («txid:n»); null if the answer is not a list of
  // transactions
  function outputs(txs, address) {
    if (!Array.isArray(txs) || txs.length > MAX_TXS) return null;
    const found = [];
    const listed = new Set();
    for (const tx of txs) {
      if (!tx || typeof tx !== "object" || typeof tx.txid !== "string" || !TXID.test(tx.txid) || !Array.isArray(tx.vout)) return null;
      // A transaction listed twice (the mempool's and the block's answer joined) is counted once
      if (listed.has(tx.txid)) continue;
      listed.add(tx.txid);
      let sats = 0;
      for (const out of tx.vout) {
        if (out && out.scriptpubkey_address === address && Number.isSafeInteger(out.value) && out.value > 0) sats += out.value;
      }
      const spends = (Array.isArray(tx.vin) ? tx.vin : []).filter((input) => input && typeof input.txid === "string"
        && TXID.test(input.txid) && Number.isSafeInteger(input.vout) && input.vout >= 0).map((input) => `${input.txid}:${input.vout}`);
      const confirmed = tx.status?.confirmed === true;
      // The block's time, in ms (the explorer gives seconds)
      const time = confirmed && Number.isSafeInteger(tx.status.block_time) && tx.status.block_time > 0
        ? tx.status.block_time * 1000 : null;
      // For the confidence of one in the mempool: whether it signals replaceability (BIP125: an input with a sequence
      // below 0xfffffffe) and its fee rate in sat/vB
      const replaceable = (Array.isArray(tx.vin) ? tx.vin : []).some((input) => Number.isSafeInteger(input?.sequence)
        && input.sequence < 0xfffffffe);
      const rate = Number.isSafeInteger(tx.fee) && tx.fee >= 0 && Number.isSafeInteger(tx.weight) && tx.weight > 0
        ? tx.fee / (tx.weight / 4) : null;
      // …and the block it is in, for the confirmations
      const height = confirmed && Number.isSafeInteger(tx.status.block_height) && tx.status.block_height >= 0
        ? tx.status.block_height : null;
      if (sats) found.push({ txid: tx.txid, confirmed, sats, spends, time, replaceable, rate, height });
    }
    return found;
  }

  // The transactions already there before the sale: on a fixed address the earlier payments are not this sale's
  const known = (txs, address) => (outputs(txs, address) || []).map((item) => item.txid);
  // …and what they spend: a transaction that spends the same as one of them replaces it (RBF), it is not a new payment
  const spent = (txs, address) => (outputs(txs, address) || []).flatMap((item) => item.spends).slice(0, 2000);

  // How far a block's time may be from the clock: a block's timestamp may be up to two hours off the real time (the
  // network's own rule), so a confirmed payment counts for a sale from two hours before it was created to two hours after
  // its end. A payment the register saw itself in the mempool counts at the moment it first saw it
  const MARGIN = 2 * 3600000;
  // More than asked by this much is not taken for this sale's payment: a wallet pays the amount of the code, and a much
  // larger payment to the address may be one for something else (the owner's register uses the wallet's own receive
  // addresses, which the wallet may also give out elsewhere)
  const OVER = 0.1;
  const most = (sats) => sats + Math.max(Math.floor(sats * OVER), 0);

  // When each transaction of the answer happened for this sale: the moment the register first saw it in the mempool
  // (`seenAt`, kept with the sale), else its block's time; one in the mempool now and never seen before is seen now
  function whenOf(item, seenAt, now) {
    if (Number.isFinite(seenAt[item.txid])) return seenAt[item.txid];
    if (item.confirmed) return Number.isFinite(item.time) ? item.time : null;
    return now;
  }

  const positive = (value) => Number.isFinite(value) && value > 0 && value < 1e5;

  // The current fee rates of mempool.space's API (GET /v1/fees/recommended), or null when the answer is not that.
  // `next` is the rate for the next block (fastestFee; the half hour's when the answer lacks it)
  function feeRates(answer) {
    if (!answer || typeof answer !== "object" || !positive(answer.halfHourFee) || !positive(answer.hourFee)) return null;
    return { next: positive(answer.fastestFee) ? answer.fastestFee : answer.halfHourFee, halfHour: answer.halfHourFee,
      hour: answer.hourFee };
  }

  // What mempool.space says of a transaction's unconfirmed ancestors (GET /v1/cpfp/<txid>): {count, rate} with `rate`
  // the effective fee rate of the package a miner would take (CPFP), null when it does not say; null when the answer is
  // not that (an Esplora without the endpoint)
  function ancestry(answer) {
    if (!answer || typeof answer !== "object" || !Array.isArray(answer.ancestors) || answer.ancestors.length > 1000) return null;
    return { count: answer.ancestors.length, rate: positive(answer.effectiveFeePerVsize) ? answer.effectiveFeePerVsize : null };
  }

  // The height of the last block (GET /blocks/tip/height), or null
  const tipHeight = (answer) => (Number.isSafeInteger(answer) && answer > 0 ? answer : null);

  const LEVELS = ["sure", "likely", "wait"];

  // How sure the payment is, from the transactions themselves: {level, reason, reasons}. `level` is «sure», «likely» or
  // «wait», `reason` the code of what decided it and `reasons` every fact looked at ({code, rate, target, count}), for the
  // register's list. A confirmed payment is sure (with its confirmations when the tip's height is known). In the mempool:
  // - its fee rate, or with unconfirmed parents the package's effective rate (CPFP), against the current rates: below
  //   the next hour's it may wait long, or be dropped or replaced: wait; below the half hour's: likely; else sure;
  // - an unconfirmed parent: at most likely, the parent may be replaced or dropped and this with it;
  // - replaceability signalled (BIP125: an input with a sequence below 0xfffffffe): at most likely. Since Bitcoin Core 28
  //   any payment in the mempool can be replaced, so the signal is a hint and never alone makes it wait;
  // - without the fee rates (an explorer without that endpoint) the fee is unknown: likely; without the ancestors
  //   (an explorer without /v1/cpfp) the parents are unknown: likely too, «sure» needs both.
  // `more` ({tip, ancestors: {txid: ancestry}}) is optional: without it confirmations and parents are not said
  function confidence(items, fees, more = {}) {
    let level = "sure";
    let reason = "confirmed";
    const reasons = [];
    const worse = (to, why) => {
      if (LEVELS.indexOf(to) > LEVELS.indexOf(level)) [level, reason] = [to, why];
    };
    const round = (rate) => Math.round(rate * 10) / 10;
    for (const item of items) {
      if (item.confirmed) {
        const count = Number.isSafeInteger(more.tip) && item.height !== null && more.tip >= item.height
          ? more.tip - item.height + 1 : null;
        reasons.push(count ? { code: "confirmations", count } : { code: "confirmed" });
        continue;
      }
      const parents = more.ancestors?.[item.txid] || null;
      const rate = parents && parents.count > 0 && parents.rate !== null ? parents.rate : item.rate;
      if (!fees || rate === null) {
        reasons.push({ code: "fee_unknown" });
        worse("likely", "fee_unknown");
      } else if (rate < fees.hour) {
        reasons.push({ code: "fee_low", rate: round(rate), target: fees.hour });
        worse("wait", "fee_low");
      } else if (rate < fees.halfHour) {
        reasons.push({ code: "fee_hour", rate: round(rate), target: fees.halfHour });
        worse("likely", "fee_hour");
      } else if (rate >= fees.next) {
        reasons.push({ code: "fee_next", rate: round(rate), target: fees.next });
        if (level === "sure") reason = "fee_next";
      } else {
        reasons.push({ code: "fee_good", rate: round(rate), target: fees.halfHour });
        if (level === "sure" && reason !== "fee_next") reason = "fee_good";
      }
      if (parents && parents.count > 0) {
        reasons.push({ code: "parent", count: parents.count });
        worse("likely", "parent");
      } else if (parents) reasons.push({ code: "no_parent" });
      else {
        // Not said (an explorer without /v1/cpfp, or past the three asked): a parent with a low fee may hold it back
        reasons.push({ code: "parents_unknown" });
        worse("likely", "parents_unknown");
      }
      reasons.push({ code: item.replaceable ? "rbf" : "no_rbf" });
      if (item.replaceable) worse("likely", "rbf");
    }
    return { level, reason, reasons };
  }

  // How a sale stands. `sats` is what it asks; `before` the transactions that are not this sale's (there when it started,
  // or another sale's); `wait` whether the merchant wants a confirmation for this amount; `spentBefore` the outputs the
  // transactions there at the start spend, so that a replacement of one of them is not taken for a payment. `window`
  // ({from, to, now, seenAt, fees, tip, ancestors}): the sale's time, from its creation to its end (ms), the moments the
  // register first saw its transactions in the mempool, the explorer's answers on the current fee rates, the last block's
  // height and the transactions' ancestors (see confidence); without it the time is not checked.
  // `confidence` says how sure the payment in the mempool is: a sale that needs no confirmation is paid from the mempool
  // only when it is not «wait». Returns {state, seen, confirmed, txids,
  // outside, seenAt}:
  // - "paid": enough arrived in the sale's time, not much more than asked (confirmed, if `wait`);
  // - "seen": the same in the mempool, waiting for a confirmation;
  // - "over": much more than asked arrived: maybe a payment for something else, never paid, the merchant decides;
  // - "partial": something arrived, less than asked: never paid, the merchant decides;
  // - "outside": nothing in the sale's time, but a payment to the address before or after it (`outside` sats);
  // - "none": nothing new;
  // - "unknown": the answer is not an explorer's.
  // `seenAt` is the moments to keep with the sale: those already there and the transactions in the mempool seen now
  function payment(txs, address, sats, before = [], wait = true, spentBefore = [], window = null) {
    const list = outputs(txs, address);
    if (!list) return { state: "unknown", seen: 0, confirmed: 0, txids: [], outside: 0, seenAt: window?.seenAt || {},
      confidence: { level: "wait", reason: "fee_unknown", reasons: [] } };
    const replaced = new Set(spentBefore);
    const fresh = list.filter((item) => !before.includes(item.txid) && !item.spends.some((output) => replaced.has(output)));
    const seenAt = { ...(window?.seenAt || {}) };
    let timely = fresh;
    if (window) {
      timely = fresh.filter((item) => {
        const at = whenOf(item, seenAt, window.now);
        return at !== null && at >= window.from - MARGIN && at <= window.to + MARGIN;
      });
      // Only a payment in the sale's time is remembered: one seen late stays late when it confirms
      for (const item of timely) if (!item.confirmed && !Number.isFinite(seenAt[item.txid])) seenAt[item.txid] = window.now;
    }
    const sum = (items) => items.reduce((total, item) => total + item.sats, 0);
    const seen = sum(timely);
    const confirmed = sum(timely.filter((item) => item.confirmed));
    const outside = sum(fresh) - seen;
    const txids = timely.map((item) => item.txid);
    const sure = confidence(timely, window?.fees ? feeRates(window.fees) : null,
      { tip: tipHeight(window?.tip), ancestors: window?.ancestors || {} });
    // Which explorer answered (`window.source`, {own, host, ownFailed}), last in the reasons: the owner's own, one of the
    // community's after the owner's did not answer, or a community's alone
    const from = window?.source;
    if (from && typeof from === "object" && typeof from.host === "string") {
      sure.reasons.push(from.own ? { code: "source_own" }
        : { code: from.ownFailed ? "source_community" : "source_public", host: from.host });
    }
    let state = "none";
    if (seen > most(sats)) state = "over";
    else if (confirmed >= sats || (!wait && seen >= sats && sure.level !== "wait")) state = "paid";
    else if (seen >= sats) state = "seen";
    else if (seen > 0) state = "partial";
    else if (outside > 0) state = "outside";
    return { state, seen, confirmed, txids, outside, seenAt, confidence: sure };
  }

  // Whether a sale of `cents` waits for a confirmation: above the merchant's threshold (in cents) it does, and without a
  // threshold every sale waits
  const waits = (cents, above) => !(Number.isFinite(above) && above >= 0) || cents > above;

  // Where to ask: the explorer's API (https://…/api, without the final slash) and the address
  const txsUrl = (api, address) => `${api.replace(/\/+$/, "")}/address/${encodeURIComponent(address)}/txs`;
  // …and the current fee rates (mempool.space's API; an Esplora without it answers an error, and the fee is unknown)
  const feesUrl = (api) => `${api.replace(/\/+$/, "")}/v1/fees/recommended`;
  // …the unconfirmed ancestors of a transaction (mempool.space only), and the last block's height
  const cpfpUrl = (api, txid) => `${api.replace(/\/+$/, "")}/v1/cpfp/${encodeURIComponent(txid)}`;
  const tipUrl = (api) => `${api.replace(/\/+$/, "")}/blocks/tip/height`;

  // The URI for the customer's wallet (BIP21): the amount in BTC, written without the float's rounding
  function uri(address, sats) {
    const whole = Math.floor(sats / 1e8);
    const rest = String(sats % 1e8).padStart(8, "0").replace(/0+$/, "");
    return `bitcoin:${address}?amount=${whole}${rest ? `.${rest}` : ""}`;
  }

  globalThis.registerChain = { feeRates, ancestry, tipHeight, confidence, feesUrl, cpfpUrl, tipUrl, outputs, known, spent, payment, waits, txsUrl, uri };
})();

// The wallet's code on /admin/ (config/admin_wallet.py): to sign in (templates/admin/wallet_login.html) or to link a
// wallet (templates/admin/second_factor.html). Asks the page's own status address every two seconds what became of the
// code: "ok" goes where the server says (on this site only), "signed" loads the page again (to confirm the wallet),
// "pending" waits, any other state shows its note and stops. Nothing else is sent.
(() => {
  const script = document.currentScript;
  const statusUrl = script && script.dataset.status;
  if (!statusUrl) return;
  const show = (state) => {
    for (const note of document.querySelectorAll("[data-state]")) note.hidden = note.dataset.state !== state;
  };
  const timer = setInterval(async () => {
    let answer;
    try {
      const response = await fetch(statusUrl, { credentials: "same-origin", headers: { Accept: "application/json" } });
      answer = await response.json();
    } catch {
      return; // try again on the next round
    }
    const state = answer && answer.state;
    if (!state || state === "pending") return;
    clearInterval(timer);
    if (state === "ok") {
      const next = new URL(String(answer.next || "/admin/"), location.href);
      location.assign(next.origin === location.origin ? next.href : "/admin/");
    } else if (state === "signed") {
      location.reload();
    } else {
      show(state);
    }
  }, 2000);
})();

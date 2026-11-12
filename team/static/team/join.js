// The page of a team invite (team/templates/team/join.html): asks what became of the code every two seconds, and opens
// the site when the wallet has signed. Served from /team/join.js, which stays open on a site under construction.
const script = document.currentScript;
const show = (state) => {
  for (const part of document.querySelectorAll("[data-state]")) part.hidden = part.dataset.state !== state;
};
const timer = setInterval(async () => {
  let state;
  try {
    const response = await fetch(script.dataset.poll, { credentials: "same-origin", cache: "no-store" });
    if (response.status === 429) return;
    state = (await response.json()).state;
  } catch { return; /* try again on the next round */ }
  if (state === "pending") return;
  clearInterval(timer);
  show(["ok", "denied"].includes(state) ? state : "expired");
  // replace, not assign: the invite's address does not stay behind the Back button
  if (state === "ok") location.replace(script.dataset.done);
}, 2000);

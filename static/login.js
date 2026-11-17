const statusUrl = document.currentScript.dataset.status;
setInterval(async () => {
  try {
    const response = await fetch(statusUrl);
    const answer = await response.json();
    // `done`: the page that connects a wallet to the account (shops/wallet_connect.html)
    if (answer.logged_in || answer.done) location.reload();
  } catch { /* try again on the next round */ }
}, 2000);
// The code lasts five minutes: if the page stays open, it gets a new one
setTimeout(() => location.reload(), 280000);

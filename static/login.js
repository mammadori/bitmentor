const statusUrl = document.currentScript.dataset.status;
setInterval(async () => {
  try {
    const response = await fetch(statusUrl);
    if ((await response.json()).logged_in) location.reload();
  } catch { /* try again on the next round */ }
}, 2000);
// The code lasts five minutes: if the page stays open, it gets a new one
setTimeout(() => location.reload(), 280000);

if (new URLSearchParams(location.search).has("saved")) {
  try { sessionStorage.removeItem("new-shop"); } catch { /* the draft stays, it does no harm */ }
}
for (const button of document.querySelectorAll("[data-copy]")) {
  button.addEventListener("click", async () => {
    const url = button.dataset.copy;
    if (navigator.share) {
      navigator.share({ url }).catch(() => {});
      return;
    }
    await navigator.clipboard?.writeText(url);
    button.textContent = button.dataset.copied;
    // The button text changes under the focus: not all screen readers announce it, the region does, the second time too
    const status = document.getElementById("copy-status");
    status.textContent = "";
    setTimeout(() => { status.textContent = button.dataset.copied; }, 100);
  });
}

// "Try again now" asks the wallet's provider and can take a while: the button says so, and a second press does not
// start a second check
for (const form of document.querySelectorAll("form.lightning-retry")) {
  const button = form.querySelector("button");
  const label = button.textContent;
  form.addEventListener("submit", (event) => {
    if (button.disabled) {
      event.preventDefault();
      return;
    }
    button.disabled = true;
    button.textContent = form.dataset.checking;
  });
  window.addEventListener("pageshow", () => {
    button.disabled = false;
    button.textContent = label;
  });
}

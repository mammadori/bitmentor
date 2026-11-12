// «Copy the lines» on the page about OpenStreetMap: the lines are in a read-only box, so without JavaScript they are
// still there to select and copy.
for (const button of document.querySelectorAll("[data-copy-text]")) {
  button.addEventListener("click", async () => {
    const box = document.querySelector(button.dataset.copyText);
    if (!box) return;
    try {
      await navigator.clipboard.writeText(box.value);
    } catch {
      box.focus();
      box.select();
      return;
    }
    const status = document.getElementById("osm-copy-status");
    status.textContent = "";
    // The text changes under the focus: not every screen reader says so, the region does, also the second time
    setTimeout(() => { status.textContent = button.dataset.copied; }, 100);
  });
}

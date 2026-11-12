// «What customers see»: the preview follows the ticks. Without this file the page works the same: the preview changes
// when the choices are saved. Each line of the preview carries data-detail, each box has the name of its detail.
(() => {
  const form = document.querySelector("form[data-display]");
  const facts = document.getElementById("preview-facts");
  const empty = document.getElementById("preview-empty");
  if (!form || !facts) return;
  const update = () => {
    let shown = 0;
    facts.querySelectorAll("li[data-detail]").forEach((line) => {
      const box = form.querySelector(`input[name="show_${line.dataset.detail}"]`);
      line.hidden = !(box && box.checked);
      if (!line.hidden) shown += 1;
    });
    if (empty) empty.hidden = shown > 0;
  };
  form.addEventListener("change", update);
  update();
})();

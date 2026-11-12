// The steps with a guide: ticking the box saves it, so the page does not need a «Save» button for each step.
// Without JavaScript the buttons stay.
for (const form of document.querySelectorAll("form.training-step-form")) {
  const box = form.querySelector("input[type=checkbox]");
  const save = form.querySelector(".training-step-save");
  if (!box || !save) continue;
  save.hidden = true;
  box.addEventListener("change", () => {
    if (box.checked) form.requestSubmit();
  });
}

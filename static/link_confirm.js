// The link works only once: on the first submit the button is disabled, so a second click does not find it already used.
// Without this script the form still works.
const form = document.querySelector("form[data-once]");
form.addEventListener("submit", () => {
  form.querySelector("button").disabled = true;
});
// Going back, or reloading in Firefox which remembers the state of buttons, the button would stay disabled
addEventListener("pageshow", () => {
  form.querySelector("button").disabled = false;
});

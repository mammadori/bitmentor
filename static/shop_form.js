const form = document.querySelector("form[data-saving]");
const submit = form.querySelector("button[type=submit]");
const submitLabel = submit.textContent;
const latitude = document.getElementById("id_latitude");
const longitude = document.getElementById("id_longitude");
// The map is on the shop's own forms; the wallet guide (shops/wallet_steps.html) has only the payment fields
const element = document.getElementById("pick-map");
const saved = Boolean(latitude?.value && longitude?.value);
let choose = null;
if (element) {
  const { lat, lon, zoom } = element.dataset;
  const center = saved ? [Number(latitude.value), Number(longitude.value)] : [Number(lat), Number(lon)];
  // As in the shops map: with focus on the container the arrows move the map, and the screen reader must
  // know what it is; Leaflet's texts in the page's language, English stays for a template that does not write them
  const quoted = (value) => value.replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;");
  element.setAttribute("role", "region");
  element.setAttribute("aria-label", element.dataset.label || "Map");
  L.Control.Zoom.mergeOptions({ zoomInTitle: element.dataset.zoomIn || "Zoom in", zoomOutTitle: element.dataset.zoomOut || "Zoom out" });
  const map = L.map(element, { scrollWheelZoom: false }).setView(center, saved ? 16 : Number(zoom));
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", { referrerPolicy: "strict-origin-when-cross-origin", maxZoom: 19, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors' }).addTo(map);
  const credits = map.attributionControl;
  if (element.dataset.library && typeof credits.options.prefix === "string") {
    credits.setPrefix(credits.options.prefix.replace(/title="[^"]*"/, `title="${quoted(element.dataset.library)}"`));
  }
  const marker = L.marker(center, { icon: L.divIcon({ className: "pin", iconSize: [18, 18] }), opacity: saved ? 1 : 0, keyboard: false }).addTo(map);
  const status = document.getElementById("pick-status");
  const here = document.getElementById("pick-here");
  choose = (latlng) => {
    marker.setLatLng(latlng).setOpacity(1);
    latitude.value = latlng.lat.toFixed(6);
    longitude.value = latlng.lng.toFixed(6);
    status.textContent = element.dataset.chosen;
    form.dispatchEvent(new Event("input"));
  };
  map.on("click", ({ latlng }) => choose(latlng));
  here.addEventListener("click", () => {
    status.textContent = here.dataset.searching;
    const refused = () => { status.textContent = here.dataset.denied; };
    if (!navigator.geolocation) refused();
    navigator.geolocation?.getCurrentPosition(({ coords }) => {
      const latlng = L.latLng(coords.latitude, coords.longitude);
      map.setView(latlng, 17);
      choose(latlng);
    }, refused, { enableHighAccuracy: true, timeout: 10000 });
  });
}

form.addEventListener("submit", () => {
  submit.disabled = true;
  submit.textContent = form.dataset.saving;
});
window.addEventListener("pageshow", () => {
  submit.disabled = false;
  submit.textContent = submitLabel;
});
// After a failed submit the focus goes to the first field to fix, in page order; if the error is not on a
// field (no way to get paid), to the error itself. Django marks the fields with aria-invalid.
const problem = form.querySelector('[aria-invalid="true"]') || form.querySelector(".errorlist.nonfield");
if (problem) {
  if (!problem.matches("input, textarea, select")) problem.tabIndex = -1;
  (problem.labels?.[0] || problem).scrollIntoView({ block: "center" });
  problem.focus({ preventScroll: true });
}

const draft = form.dataset.draft;
function read() {
  try {
    return JSON.parse(sessionStorage.getItem(draft) || "{}");
  } catch {
    return {};
  }
}
function store() {
  try {
    const values = Object.fromEntries(new FormData(form));
    delete values.csrfmiddlewaretoken;
    sessionStorage.setItem(draft, JSON.stringify(values));
  } catch { /* the draft is a convenience */ }
}
if (draft) {
  for (const [name, value] of Object.entries(read())) {
    if (form.elements[name] && !form.elements[name].value) form.elements[name].value = value;
  }
  form.addEventListener("input", store);
  if (choose && !saved && latitude.value && longitude.value) choose(L.latLng(Number(latitude.value), Number(longitude.value)));
}

// The Lightning address is tried as soon as it is pasted or when the field is left: the server brings it back to the right form,
// says whether the till will know about payments, and tries it again on submit anyway. Without JavaScript only the submit tries it.
const lightning = document.getElementById("id_lightning_address");
const check = document.getElementById("lightning-check");
// As tidy_lightning_address in shops/forms.py: spaces, quotes, the final dot, «lightning:», the LNURL URL
const AROUND = "\"'`«»“”„‘’‚‹›<>()[]{}.,;:!?";
const INVISIBLE = /[\u200b\u200c\u200e\u200f\u202a-\u202e\u2060-\u2069\ufeff]/g;
function trimmed(value) {
  let start = 0;
  let end = value.length;
  while (start < end && AROUND.includes(value[start])) start += 1;
  while (end > start && AROUND.includes(value[end - 1])) end -= 1;
  return value.slice(start, end);
}
function tidy(text) {
  let value = trimmed(text.replace(/\s+/g, "").replace(INVISIBLE, ""));
  value = trimmed(value.replace(/^lightning:(\/\/)?/i, ""));
  const url = value.match(/^(?:https|lnurlp):\/\/([^\/?#:@]+)\/\.well-known\/lnurlp\/([^\/?#@]+)\/?(?:[?#].*)?$/i);
  if (url) value = `${url[2]}@${url[1]}`;
  return value.toLowerCase();
}
if (lightning && check) {
  const text = check.dataset;
  const token = form.querySelector("[name=csrfmiddlewaretoken]").value;
  // The error of the last submit stays until the field changes; an address already refused is not tried again on leaving
  const sent = lightning.previousElementSibling?.matches(".errorlist") ? lightning.previousElementSibling : null;
  let asked = sent ? lightning.defaultValue : "";
  let pending = null;
  check.hidden = false;
  lightning.setAttribute("aria-describedby", [lightning.getAttribute("aria-describedby"), check.id].filter(Boolean).join(" "));
  const span = (className, content) => Object.assign(document.createElement("span"), { className, textContent: content });
  const show = (state, message = "", note = "", suggestion = "") => {
    const problem = !["", "checking", "ok", "busy", "unknown"].includes(state);
    check.dataset.state = state;
    check.classList.toggle("problem", problem);
    if (state !== "checking") check.style.minHeight = "";
    check.replaceChildren();
    if (note) check.append(span("check-note", note), " ");
    if (message) check.append(span("check-result", message));
    if (suggestion) {
      const use = Object.assign(document.createElement("button"), { type: "button", className: "linklike check-suggest" });
      use.textContent = text.suggest.replace("{address}", () => suggestion);
      // The button disappears with the new result: focus goes back to the field, it does not stay on nothing
      use.addEventListener("click", () => {
        lightning.value = suggestion;
        form.dispatchEvent(new Event("input"));
        lightning.focus();
        run();
      });
      check.append(" ", use);
    }
    if (problem) lightning.setAttribute("aria-invalid", "true");
    else lightning.removeAttribute("aria-invalid");
  };
  async function run() {
    const raw = lightning.value;
    const value = tidy(raw);
    const note = value && value !== raw ? text.tidied.replace("{address}", () => value) : "";
    if (value !== raw) {
      lightning.value = value;
      form.dispatchEvent(new Event("input"));
    }
    if (value === asked) return;
    asked = value;
    pending?.abort();
    pending = null;
    if (!value) {
      show("");
      return;
    }
    if (sent) sent.hidden = true;
    const controller = new AbortController();
    pending = controller;
    // The server stops trying sooner: this applies if it does not answer either
    const timer = setTimeout(() => controller.abort("slow"), 20000);
    // «Trying» does not shorten the region: what is below stays where it is until the result arrives
    check.style.minHeight = `${check.offsetHeight}px`;
    show("checking", text.checking, note);
    try {
      const response = await fetch(text.url, {
        method: "POST", body: new URLSearchParams({ address: value }), headers: { "X-CSRFToken": token },
        credentials: "same-origin", signal: controller.signal,
      });
      const answer = await response.json();
      if (pending !== controller) return;
      if (!answer.message) throw new Error(answer.state);
      if (answer.address && answer.address !== value && lightning.value === value) {
        lightning.value = asked = answer.address;
        form.dispatchEvent(new Event("input"));
      }
      show(answer.state, answer.message, note, answer.suggestion || "");
    } catch {
      if (pending !== controller) return;
      show("unknown", controller.signal.reason === "slow" ? text.slow : text.failed, note);
      asked = "";
    } finally {
      clearTimeout(timer);
      if (pending === controller) pending = null;
    }
  }
  lightning.addEventListener("input", (event) => {
    if (event.inputType === "insertFromPaste" || event.inputType === "insertFromDrop") run();
    else if (lightning.value !== asked && check.dataset.state) {
      // Whoever edits by hand no longer reads the result for the previous address
      pending?.abort();
      pending = null;
      asked = "";
      show("");
    }
  });
  // Whoever leaves the field by touching something else (the box below, the button) must find it where they pressed it:
  // with the mouse the field is left on press and the thing opens on release, and a result arriving in between would move
  // it. The check starts after the release, when the click has already arrived.
  let pressed = false;
  let waiting = false;
  const released = () => {
    pressed = false;
    if (!waiting) return;
    waiting = false;
    setTimeout(run, 0);
  };
  document.addEventListener("pointerdown", () => { pressed = true; }, true);
  document.addEventListener("pointerup", released, true);
  document.addEventListener("pointercancel", released, true);
  lightning.addEventListener("blur", () => {
    if (!pressed) {
      run();
      return;
    }
    waiting = true;
    setTimeout(() => waiting && released(), 1500);
  });
}

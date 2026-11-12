const element = document.getElementById("map");
const { url, lat, lon, zoom, failed, searching, denied, near } = element.dataset;
const $ = (id) => document.getElementById(id);
const still = matchMedia("(prefers-reduced-motion: reduce)").matches;
// Leaflet's texts and the pin names in the page's language: the template writes them, English stays for a
// community whose copy of it has none
const texts = {
  label: element.dataset.label || "Map",
  zoomIn: element.dataset.zoomIn || "Zoom in",
  zoomOut: element.dataset.zoomOut || "Zoom out",
  close: element.dataset.close || "Close",
  library: element.dataset.library,
  discount: element.dataset.discount || "{n}% discount",
  group: element.dataset.group || "{n} places: zoom in",
};
const quoted = (value) => value.replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;");
// With focus on the container the arrows move the map: a screen reader must be able to say what it is
element.setAttribute("role", "region");
element.setAttribute("aria-label", texts.label);
L.Control.Zoom.mergeOptions({ zoomInTitle: texts.zoomIn, zoomOutTitle: texts.zoomOut });
const map = L.map(element, { scrollWheelZoom: false, zoomAnimation: !still, fadeAnimation: !still, markerZoomAnimation: !still })
  .setView([Number(lat), Number(lon)], Number(zoom));
const tiles = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19,
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  referrerPolicy: "strict-origin-when-cross-origin",
});
// The tiles of the first draw are the biggest thing on the page on a phone: they go ahead of the other images.
// An image asks for its file as soon as the current task ends, so the priority set right after the address already counts
let early = true;
tiles.createTile = function (coords, done) {
  const tile = L.TileLayer.prototype.createTile.call(this, coords, done);
  if (early) tile.fetchPriority = "high";
  return tile;
};
tiles.once("load", () => { early = false; });
tiles.addTo(map);
const credits = map.attributionControl;
if (texts.library && typeof credits.options.prefix === "string") {
  credits.setPrefix(credits.options.prefix.replace(/title="[^"]*"/, `title="${quoted(texts.library)}"`));
}

// Leaflet names a pin after the text inside it, «-10%» or «5», and the title stays mute: aria-label gives the name.
// With titled the title too, in place of Leaflet's option. The name can be a function: it is computed when the pin
// enters the map, not for those shut in a cluster
function named(name, options, titled = false) {
  const pin = L.divIcon(options);
  pin.createIcon = (old) => {
    const box = L.DivIcon.prototype.createIcon.call(pin, old);
    const text = typeof name === "function" ? name() : name;
    box.setAttribute("aria-label", text);
    if (titled) box.title = text;
    return box;
  };
  return pin;
}
// Like the Leaflet.markercluster clusters, with a name: «12 places: zoom in», not «12»
function bunch(group) {
  const count = group.getChildCount();
  return named(texts.group.replace("{n}", count), {
    html: `<div><span>${count}</span></div>`,
    className: `marker-cluster marker-cluster-${count < 10 ? "small" : count < 100 ? "medium" : "large"}`,
    iconSize: L.point(40, 40),
  });
}

const cluster = L.markerClusterGroup({ showCoverageOnHover: false, maxClusterRadius: 44, iconCreateFunction: bunch });
map.addLayer(cluster);
document.querySelector(".map-tools").after($("shop-empty"));
let shops = [];
let visible = [];
// Where distances are measured from: empty until someone asks
let from = null;
let you = null;
// Filters a community adds in the template: a checkbox with data-words, words separated by spaces
const words = [...document.querySelectorAll(".map-tools [data-words]")];
// On a phone the list starts short: 62 rows are five screens before one can leave the page
const SHORT = 12;
const shortList = matchMedia("(max-width: 860px)");
let showAll = false;
// The «N shown» count is a live region: while typing in the search it updates when typing pauses, not on every letter
const PAUSE = 700;
let counting;
// A community may have its own copy of the template: the optional messages must not break it
const say = (message) => {
  if ($("shop-status") && message) $("shop-status").textContent = message;
};
const plain = (value) => (value || "").normalize("NFD").replace(/\p{M}/gu, "").toLowerCase();

// A historic shop has closed or is closing: it stays on the map as a place of history, but is not a place to pay
const historic = (shop) => Boolean(shop.closed_on);
// Recognizable without colors too: square and with a clock
const CLOCK = '<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="6"/><path d="M8 4.5V8l2.5 1.5"/></svg>';

// The name of a pin: the shop, and the discount or since when it is historic
function label(shop) {
  if (historic(shop)) return `${shop.name} · ${closure(shop)}`;
  // The pin shows the discount too: its name keeps it, and says the shop is closed for a while after it
  const name = shop.discount ? `${shop.name}, ${texts.discount.replace("{n}", shop.discount)}` : shop.name;
  const pause = breakNote(shop);
  return pause ? `${name} · ${pause}` : name;
}

const icon = (shop) => (historic(shop)
  ? named(() => label(shop), { className: "pin pin-historic", html: CLOCK, iconSize: [22, 22] }, true)
  : named(() => label(shop), {
    className: shop.discount ? "pin pin-discount" : "pin",
    html: shop.discount ? `-${shop.discount}%` : "",
    iconSize: shop.discount ? [40, 40] : [18, 18],
  }, true));

// Certified shop, how recently the Lightning address worked (shops/certified.py) and whether the owner says the till takes
// on-chain too: the JSON brings only flags and a band, never a date or a payment (the site sees no on-chain payment). The
// words come from the template (#paid-texts), with English for a community whose copy has none
const BANDS = ["week", "month", "year", "older"];
const paidTexts = (() => {
  try { return JSON.parse($("paid-texts")?.textContent || "{}"); } catch { return {}; }
})();
const english = {
  certified: "Certified shop",
  trained: "Trained merchant",
  lightning: { week: "Lightning, active in the last week", month: "Lightning, active in the last month", year: "Lightning, active in the last year", older: "Lightning, not active in the last year" },
  onchain: "On-chain payments too",
};
const SVG = "http://www.w3.org/2000/svg";
const SHAPES = {
  certified: ["M3.5 8.5l3 3 6-7"],
  trained: ["M8 1.5l5 2v4c0 3-2.2 5.3-5 6.5C5.2 12.8 3 10.5 3 7.5v-4z"],
  lightning: ["M9.5 1 3 9.5h4.5L6.5 15 13 6.5H8.5z"],
  onchain: ["M6.5 9.5l3-3M7.5 4.5l1-1a2.6 2.6 0 0 1 3.7 3.7l-1 1M8.5 11.5l-1 1a2.6 2.6 0 0 1-3.7-3.7l1-1", "line"],
};
function drawing(kind) {
  const svg = document.createElementNS(SVG, "svg");
  svg.setAttribute("viewBox", "0 0 16 16");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS(SVG, "path");
  path.setAttribute("d", SHAPES[kind][0]);
  if (SHAPES[kind][1]) path.setAttribute("class", SHAPES[kind][1]);
  svg.append(path);
  return svg;
}
// The badge and the icons of a shop, or null: the same markup as templates/shops/shop_marks.html. In the card
// (`spoken`) each icon has its words next to it: a tooltip never shows on a phone
function marks(shop, spoken = false) {
  const box = document.createElement("span");
  box.className = "marks";
  if (shop.certified === true) {
    const badge = document.createElement("span");
    badge.className = "certified";
    badge.append(drawing("certified"), paidTexts.certified || english.certified);
    // What the badge means (certified.MEANING, no numbers): on a mouse, over the badge
    if (paidTexts.rule) badge.title = paidTexts.rule;
    box.append(badge);
  }
  // The merchant has the basic pass of the training (shops/training.py): a sign, if the community wants it
  if (shop.trained === true) {
    const sign = document.createElement("span");
    sign.className = "certified trained";
    sign.append(drawing("trained"), paidTexts.trained || english.trained);
    box.append(sign);
  }
  const paid = historic(shop) ? null : shop.paid;
  const icons = BANDS.includes(paid?.lightning) ? [["lightning", paid.lightning,
    paidTexts.lightning?.[paid.lightning] || english.lightning[paid.lightning]]] : [];
  if (shop.onchain === true && !historic(shop)) icons.push(["onchain", "said", paidTexts.onchain || english.onchain]);
  icons.forEach(([kind, band, text]) => {
    const icon = document.createElement("span");
    icon.className = `paid paid-${kind} paid-${band}`;
    icon.append(drawing(kind));
    if (spoken) {
      const line = document.createElement("span");
      line.className = "paid-line";
      line.append(icon, text);
      box.append(line);
      return;
    }
    icon.setAttribute("role", "img");
    icon.setAttribute("aria-label", text);
    icon.title = text;
    box.append(icon);
  });
  return box.childElementCount ? box : null;
}

// The JSON is only one and carries the links in the default language: the card stays in the page's language
const language = document.documentElement.lang;
const card = (shop) => shop.url.replace(/^\/[^/]+\//, `/${language}/`);

// «Closes on» up to and including that day, then «closed on». The sentences come from the template, with a fallback
// for whoever has a copy without them; the date arrives as YYYY-MM-DD and is written in the page's language
const pad = (number) => String(number).padStart(2, "0");
const now = new Date();
const today = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
// The first date format loads the language data: on a slow phone more than 100 ms. It is prepared the first time it is
// needed, not on every map load: almost always no historic shop is in view at the start
let longDate;
function dateText(iso) {
  longDate ??= new Intl.DateTimeFormat(language || undefined, { day: "numeric", month: "long", year: "numeric", timeZone: "UTC" });
  const [year, month, day] = iso.split("-").map(Number);
  return longDate.format(Date.UTC(year, month - 1, day));
}
function closure(shop) {
  const phrase = shop.closed_on >= today
    ? element.dataset.closes || "Historic · closes on {date}"
    : element.dataset.closed || "Historic · closed on {date}";
  return phrase.replace("{date}", dateText(shop.closed_on));
}

// Closed for a while (shops/vacation.py): holidays, works… The JSON brings the reason and the days, the sentences come from
// the template (#paid-texts); a community's copy of it without them shows nothing. The shop stays on the map as it is
function breakNote(shop) {
  const found = shop.break;
  const words = paidTexts.break;
  if (!found || historic(shop) || !words || !words.what) return "";
  const phrase = words[`${found.state}_${found.back ? "back" : "open"}`];
  if (!phrase) return "";
  // «Until I say so» has no day to open again (found.back is null): a date is written only when there is one
  const when = (iso) => (iso ? dateText(iso) : "");
  return phrase.replace("{what}", words.what[found.reason] || words.what.other || "")
    .replace("{date}", when(found.state === "soon" ? found.from : found.back))
    .replace("{back}", when(found.back));
}

function note(shop, tag) {
  const line = document.createElement(tag);
  line.className = "historic-note";
  line.textContent = closure(shop);
  return line;
}

// The shop's website, if its owner shows it (it is not in the JSON otherwise) and it is a web address: the host is the
// text of the link, so that it needs no translation and says where it leads
function site(shop) {
  try {
    const url = new URL(shop.website);
    return url.protocol === "http:" || url.protocol === "https:" ? url : null;
  } catch {
    return null;
  }
}

function popup(shop) {
  const box = document.createElement("div");
  const name = document.createElement("a");
  name.href = card(shop);
  name.textContent = shop.name;
  const details = document.createElement("p");
  details.textContent = shop.address || shop.city;
  box.append(name, details);
  const web = site(shop);
  if (web) {
    const line = document.createElement("p");
    const link = document.createElement("a");
    link.href = web.href;
    link.rel = "noopener nofollow";
    link.textContent = web.hostname.replace(/^www\./, "");
    line.append(link);
    box.append(line);
  }
  const signs = marks(shop, true);
  if (signs) box.append(signs);
  if (historic(shop)) box.append(note(shop, "p"));
  const pause = breakNote(shop);
  if (pause) {
    const line = document.createElement("p");
    line.className = "break-note";
    line.textContent = pause;
    box.append(line);
  }
  return box;
}

const spot = (shop) => [shop.latitude, shop.longitude];

// Pins just outside the visible part stay in the page and Tab reaches them: the map brings them into view. After a
// frame, because Leaflet resets the scrolling the browser does to show the focus
element.addEventListener("focusin", (event) => {
  if (!event.target.classList.contains("leaflet-marker-icon")) return;
  requestAnimationFrame(() => {
    const pin = event.target.getBoundingClientRect(), box = element.getBoundingClientRect(), room = 40;
    const dx = pin.left < box.left + room ? pin.left - box.left - room : pin.right > box.right - room ? pin.right - box.right + room : 0;
    const dy = pin.top < box.top + room ? pin.top - box.top - room : pin.bottom > box.bottom - room ? pin.bottom - box.bottom + room : 0;
    if (dx || dy) map.panBy([dx, dy], { animate: !still });
  });
});

// From the keyboard Enter on a pin opens the popup: focus enters it, and when it closes, with Esc or with the cross,
// it returns to the pin. On its own Leaflet leaves focus on the pin, after the popup in Tab order, and closes
// with Esc only if focus is on the container. The popup is built when it opens, not for all shops at load.
function marker(shop) {
  const pin = L.marker(spot(shop), { icon: icon(shop) }).bindPopup(() => popup(shop));
  pin.on("popupopen", ({ popup: box }) => box.getElement().querySelector("a")?.focus({ preventScroll: true }));
  pin.on("popupclose", ({ popup: box }) => {
    const focus = document.activeElement;
    if (focus === document.body || box.getElement()?.contains(focus)) (pin.getElement() || element).focus({ preventScroll: true });
  });
  return pin;
}
map.on("popupopen", ({ popup: box }) => {
  box.getElement().querySelector(".leaflet-popup-close-button")?.setAttribute("aria-label", texts.close);
});
element.addEventListener("keydown", (event) => {
  if (event.key === "Escape") map.closePopup();
});
// For whoever extends the map from the theme: the "shops" event on #map says the results have changed
element.shopMap = { map, tiles, cluster, popup, closure, shops: () => shops, visible: () => visible };
const metres = (value) => (value < 1000
  ? `${Math.round(value / 10) * 10} m`
  : `${(value / 1000).toLocaleString(document.documentElement.lang, { maximumFractionDigits: 1 })} km`);
// The description says what kind of place it is: without links and without the dash that preceded them
// The trailing dash is removed by hand: /[\s\-–]+$/ on a description of only dashes takes quadratic time
const kind = (shop) => {
  const text = (shop.description || "").replace(/https?:\/\/\S+/g, "");
  let end = text.length;
  while (end && " \t\n\r-–".includes(text[end - 1])) end -= 1;
  return text.slice(0, end).trim();
};

function entry(shop) {
  const item = document.createElement("li");
  const link = document.createElement("a");
  link.href = card(shop);
  const name = document.createElement("strong");
  name.textContent = shop.name;
  const place = document.createElement("span");
  place.textContent = (from ? [metres(from.distanceTo(spot(shop))), shop.address || shop.city] : [shop.city, shop.address])
    .filter(Boolean).join(" · ");
  link.append(name, place);
  const signs = marks(shop);
  if (signs) link.append(signs);
  if (kind(shop)) {
    const what = document.createElement("small");
    what.textContent = kind(shop);
    link.append(what);
  }
  if (historic(shop)) {
    item.className = "historic";
    link.append(note(shop, "small"));
  }
  const pause = breakNote(shop);
  if (pause) {
    const line = document.createElement("small");
    line.className = "break-note";
    line.textContent = pause;
    link.append(line);
  }
  item.append(link);
  if (shop.discount && !historic(shop)) {
    const discount = document.createElement("b");
    discount.className = "discount";
    discount.textContent = `-${shop.discount}%`;
    item.append(discount);
  }
  return item;
}

// The first draw is done in two: the pins at once, the list and the count in the next task, so the phone does not stay
// blocked in a single task. turn says which draw is the latest: a list left behind does not write over a newer one
let turn = 0;
function render(wait = 0, later = false) {
  const mine = ++turn;
  const query = plain($("shop-search").value.trim());
  const onlyDiscount = $("shop-discount").checked;
  const onlyLightning = $("shop-lightning").checked;
  visible = shops.filter((shop) => {
    const text = plain(`${shop.name} ${shop.city} ${shop.address || ""} ${shop.description}`);
    return (!onlyDiscount || shop.discount) && (!onlyLightning || (shop.lightning && !historic(shop))) && text.includes(query)
      && words.every((box) => !box.checked || new RegExp(`\\b(${box.dataset.words.split(" ").join("|")})\\b`).test(text));
  });
  cluster.clearLayers();
  cluster.addLayers(visible.map(marker));
  const filtered = query || onlyDiscount || onlyLightning || words.some((box) => box.checked);
  // Outside the fetch an error of the list would not reach its catch: it says so here, as the catch would
  if (later) setTimeout(() => { try { if (mine === turn) list(filtered, wait); } catch { say(failed); } });
  else list(filtered, wait);
}

function list(filtered, wait) {
  const short = !filtered && !showAll && shortList.matches;
  $("shop-list").replaceChildren(...(short ? visible.slice(0, SHORT) : visible).map(entry));
  // The counter says how many places to pay: historic shops are not counted
  $("shop-count").textContent = shops.filter((shop) => !historic(shop)).length;
  $("shop-head")?.removeAttribute("hidden");
  clearTimeout(counting);
  const count = () => {
    const shown = $("shop-shown");
    // Like the counter in the title, only the places to pay: a historic pin is on the map but not counted
    const paying = visible.filter((shop) => !historic(shop)).length;
    $("shop-results").textContent = paying;
    // “1 shop shown”, not “1 shops shown”; a theme that rewrote the template without #shop-shown keeps the fixed word
    if (shown) shown.textContent = paying === 1 ? shown.dataset.one : shown.dataset.many;
  };
  if (wait) counting = setTimeout(count, wait);
  else count();
  $("shop-empty").hidden = visible.length > 0;
  const more = $("shop-more");
  if (more) {
    more.hidden = !short || visible.length <= SHORT;
    more.textContent = `${more.dataset.label} (${visible.length})`;
  }
  element.dispatchEvent(new CustomEvent("shops"));
}

// Historic shops go to the bottom of the list, even when looking for the nearest place
const last = (a, b) => Number(historic(a)) - Number(historic(b));
// The first comparison between names loads the language data, more than 100 ms on a slow phone: it is done as soon as the browser is
// idle, on its own, while the list of shops is still on its way. If the list arrives first, the sorting does it
let collator;
const byName = (a, b) => (collator ??= new Intl.Collator()).compare(a.name, b.name);
(window.requestIdleCallback || setTimeout)(() => { collator ??= new Intl.Collator(); });
const nearest = () => shops.sort((a, b) => last(a, b) || from.distanceTo(spot(a)) - from.distanceTo(spot(b)));

// Reorders from the given point, tightens the map on that point and the first results, on a phone scrolls to the list
function measure(here, message, scroll = true) {
  from = here;
  nearest();
  render();
  you ??= L.circleMarker(here, { radius: 8, weight: 3, color: "#fff", fillColor: "#1a73e8", fillOpacity: 1 }).addTo(map);
  you.setLatLng(here);
  map.fitBounds(L.latLngBounds([[here.lat, here.lng], ...visible.slice(0, 4).map(spot)]), { maxZoom: 16, padding: [24, 24] });
  say(message);
  if (scroll && shortList.matches) $("shop-list").scrollIntoView({ behavior: still ? "auto" : "smooth" });
}

function locate(found, refused) {
  if (!navigator.geolocation) {
    refused();
    return;
  }
  navigator.geolocation.getCurrentPosition(({ coords }) => found(L.latLng(coords.latitude, coords.longitude)), refused, { timeout: 10000 });
}

fetch(url).then((response) => response.json()).then((data) => {
  shops = data.shops.sort((a, b) => last(a, b) || b.discount - a.discount || byName(a, b));
  if (from) nearest();
  render(0, true);
}).catch(() => say(failed));

$("shop-more")?.addEventListener("click", () => {
  showAll = true;
  render();
  // The button disappears: focus goes to the first place that was not visible before, instead of going back to the top of the page
  $("shop-list").children[SHORT]?.querySelector("a")?.focus();
});
$("shop-search").addEventListener("input", () => render(PAUSE));
$("shop-discount").addEventListener("change", () => render());
$("shop-lightning").addEventListener("change", () => render());
words.forEach((box) => box.addEventListener("change", () => render()));
$("shop-near").addEventListener("click", () => {
  say(searching);
  locate((here) => measure(here, near), () => say(denied));
});

// A community's button: the filters already set, the results at once from the point written in the template,
// then from the real position if it is nearby. Whoever denies permission already has the answer.
const quick = $("shop-quick");
quick?.addEventListener("click", () => {
  $("shop-lightning").checked = true;
  words.forEach((box) => { box.checked = true; });
  const center = L.latLng(quick.dataset.from.split(","));
  measure(center, quick.dataset.center);
  locate((here) => {
    if (here.distanceTo(center) < Number(quick.dataset.radius)) measure(here, quick.dataset.near, false);
  }, () => {});
});

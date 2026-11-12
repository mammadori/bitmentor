// The card to turn towards the till: the button opens it, a tap anywhere or Esc closes it, and the focus goes back to
// the button. Without <dialog> the button goes away; without JavaScript base.html hides it (.needs-js).
const card = document.getElementById("counter-card");
const show = document.getElementById("show-counter");
if (card && show) {
  if (typeof card.showModal === "function") {
    show.addEventListener("click", () => card.showModal());
    card.addEventListener("click", () => card.close());
    card.addEventListener("close", () => show.focus());
  } else {
    show.hidden = true;
  }
}

const element = document.getElementById("map");
if (element && window.L) {
  const position = [Number(element.dataset.lat), Number(element.dataset.lon)];
  const map = L.map(element, { scrollWheelZoom: false }).setView(position, 15);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", { referrerPolicy: "strict-origin-when-cross-origin", maxZoom: 19, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors' }).addTo(map);
  // A historic shop has the same pin as the map: square, with a clock. The pin opens nothing, so it is kept out of
  // the Tab order (keyboard: false) rather than being a key with no name
  const { historic } = element.dataset;
  const CLOCK = '<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="6"/><path d="M8 4.5V8l2.5 1.5"/></svg>';
  L.marker(position, historic === undefined
    ? { icon: L.divIcon({ className: "pin", iconSize: [18, 18] }), keyboard: false }
    : { icon: L.divIcon({ className: "pin pin-historic", html: CLOCK, iconSize: [22, 22] }), title: historic, keyboard: false }).addTo(map);

  // With the position, OpenStreetMap draws the route at once; without it, it only asks where to start from
  const route = document.getElementById("shop-route");
  route?.addEventListener("click", (event) => {
    if (!navigator.geolocation) return;
    event.preventDefault();
    navigator.geolocation.getCurrentPosition(({ coords }) => {
      const here = L.latLng(coords.latitude, coords.longitude);
      const engine = here.distanceTo(position) < 5000 ? "foot" : "car";
      const stops = encodeURIComponent(`${coords.latitude},${coords.longitude};${position.join(",")}`);
      location.href = `https://www.openstreetmap.org/directions?engine=fossgis_osrm_${engine}&route=${stops}`;
    }, () => { location.href = route.href; }, { timeout: 8000, maximumAge: 60000 });
  });
}

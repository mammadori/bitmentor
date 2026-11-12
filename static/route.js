const element = document.getElementById("route-map");
const stops = JSON.parse(element.dataset.stops);
const still = matchMedia("(prefers-reduced-motion: reduce)").matches;
const map = L.map(element, { scrollWheelZoom: false, zoomAnimation: !still, fadeAnimation: !still, markerZoomAnimation: !still });
L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 19, referrerPolicy: "strict-origin-when-cross-origin", attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors' }).addTo(map);
const points = stops.map((stop) => [stop.latitude, stop.longitude]);
stops.forEach((stop, index) => {
  L.marker(points[index], { icon: L.divIcon({ className: "pin pin-discount", html: String(index + 1), iconSize: [32, 32] }), title: stop.name })
    .bindPopup(stop.name).addTo(map);
});
const main = points.filter((point, index) => stops[index].place === stops[0].place);
const frame = (group) => map.fitBounds(L.latLngBounds(group), { padding: [60, 60], maxZoom: 16 });
const buttons = document.querySelectorAll("[data-route-view]");
function view(button) {
  frame(button.dataset.routeView === "all" ? points : main);
  for (const other of buttons) other.setAttribute("aria-pressed", String(other === button));
}
for (const button of buttons) {
  button.hidden = main.length === points.length;
  button.addEventListener("click", () => view(button));
}
// Start from the place of the first stop: with a distant stop in frame the nearby pins overlapped and only a few
// pixels were left to tap (WCAG 2.5.8). "All" shows the other stops too
const start = [...buttons].find((button) => button.dataset.routeView === (main.length < points.length ? "main" : "all"));
if (start) view(start);
else frame(points);

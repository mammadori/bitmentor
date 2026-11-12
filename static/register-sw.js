// The register that opens even when the site does not answer. It only covers a shop's register, in its language: the scope
// is the register's path, more specific than the site's, and the rest of the site never passes through here.
// Deliberately minimal: a service worker stays on the phone even after a fix on the server. What to save is decided by the
// page (register.js), which updates with the site; here we only choose between the network and the saved copy.
const CACHE = `register:${new URL(self.registration.scope).pathname}`;
const WAIT = 4000;

const isHtml = (response) => (response.headers.get("Content-Type") || "").toLowerCase().startsWith("text/html");
const late = () => new Promise((_resolve, reject) => { setTimeout(reject, WAIT, new Error("timeout")); });

async function answer(request) {
  const cache = await caches.open(CACHE);
  const saved = await cache.match(request, { ignoreSearch: request.mode === "navigate" });
  if (!saved) return fetch(request);
  // A file with its fingerprint in the name (register.3f2a9c0d1e4b.js) never changes: the copy is right, and it is the one the
  // saved page expects. After a release the network would answer 404, and the copied register would be left without JavaScript
  if (/\.[0-9a-f]{12}\.\w+$/.test(new URL(request.url).pathname)) return saved;
  try {
    // With the network the site answers, even with a 404: the copy is for when the site is silent or broken. In maintenance
    // Caddy answers every path with the works page, 200 but without the CSP that Django puts on everything: that too
    // counts as site down, for the page and for its files. And a page never stands in for a saved script or style
    const response = await Promise.race([fetch(request), late()]);
    // A page that sends elsewhere (the staff rules, a login that expired) is the site answering: the browser follows it.
    // A navigation's fetch does not follow redirects itself (an opaque redirect); one that did is sent on to where it ended
    if (request.mode === "navigate" && response.type === "opaqueredirect") return response;
    if (request.mode === "navigate" && response.redirected && response.url) return Response.redirect(response.url, 302);
    const fromSite = response.headers.has("Content-Security-Policy") && (!response.ok || isHtml(response) === isHtml(saved));
    return response.status >= 500 || !fromSite ? saved : response;
  } catch {
    return saved;
  }
}

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
self.addEventListener("fetch", (event) => {
  const { request } = event;
  // Invoices, payments and other people's servers always go to the network: here only reads from the site, if a copy exists
  if (request.method !== "GET" || new URL(request.url).origin !== location.origin) return;
  event.respondWith(answer(request));
});

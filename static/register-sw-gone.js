// This shop's till can no longer take payments: the earlier service worker removes itself, along with the copy of the
// till it had saved. If the connection fails later the phone shows the browser's error, not the old till.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil((async () => {
  await caches.delete(`register:${new URL(self.registration.scope).pathname}`);
  await self.registration.unregister();
})()));

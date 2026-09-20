/* Minimal service worker: cache the app shell for offline use.
 *
 * Security review 2026-09-17:
 * - Same-origin shell only. Cross-origin requests (tiles, future CDN
 *   libraries) must pass through untouched - caching a foreign opaque
 *   response would pin whatever the network returns.
 * - The network fallback only caches clean 200 responses. Previously
 *   an error page (404/500, or a captive portal's 302->HTML) could be
 *   cached under the app-shell key and served FOREVER - bricking the
 *   PWA until its storage was manually cleared.
 * - The previous `.catch(() => hit)` handed back a stale shell on any
 *   network failure with no way to recover; failures now surface.
 */
const CACHE = "scope-shell-v1";
const SHELL = ["./", "./index.html", "./manifest.webmanifest", "./icon.svg"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))),
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET") return;
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin) return; // never touch cross-origin

  event.respondWith(
    caches.match(event.request).then(
      (hit) =>
        hit ||
        fetch(event.request).then((resp) => {
          if (resp && resp.ok) {
            const copy = resp.clone();
            caches.open(CACHE).then((c) => c.put(event.request, copy));
          }
          return resp;
        }),
    ),
  );
});

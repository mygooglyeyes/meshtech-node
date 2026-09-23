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
const CACHE = "scope-shell-v4";
const SHELL = ["./", "./index.html", "./manifest.webmanifest", "./icon.svg",
  "./icon-192.png", "./icon-512.png", "./icon-maskable-192.png",
  "./icon-maskable-512.png"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)));
  self.skipWaiting();
});

/* A waiting worker is told to take over immediately (index.html sends
 * SKIP_WAITING on updatefound) so updates land on the next load
 * without devtools dances. */
self.addEventListener("message", (event) => {
  if (event.data && event.data.type === "SKIP_WAITING") self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))),
  );
  self.clients.claim();
});

/* Stage 1 (2026-09-22): activated by index.html's register() call.
 * The v1 -> v2 cache bump is deliberate: the first registered worker
 * must not inherit any stale v1 entries from bench machines.
 */

/* STAGE 1 UPDATE RULE (2026-09-22, the cache-first trap caught live):
 * cache-first for the SHELL means an installed app never sees an
 * update - the old page serves forever (the same wedge Brett hit on
 * 2026-09-21, new clothes). Rule now: navigation requests (the page
 * itself) go NETWORK-FIRST with the cache only as offline fallback;
 * everything else same-origin stays cache-first (assets are
 * content-addressed by the build). Failures surface - never serve a
 * frozen stale shell (security review F2 holds).
 */
self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET") return;
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin) return; // never touch cross-origin

  if (event.request.mode === "navigate") {
    event.respondWith(
      fetch(event.request).then((resp) => {
        if (resp && resp.ok) {
          const copy = resp.clone();
          caches.open(CACHE).then((c) => c.put(event.request, copy));
        }
        return resp;
      }).catch(() =>
        caches.match(event.request).then((hit) => hit || Response.error())),
    );
    return;
  }

  // Network-first with cache fallback for everything else too:
  // app.js/app.css are REBUILT under fixed names, so cache-first would
  // pin a stale app forever (caught live 2026-09-22 - the page ran an
  // old bundle while dist had the new one). Cache stays the OFFLINE
  // copy, not the primary source.
  event.respondWith(
    fetch(event.request).then((resp) => {
      if (resp && resp.ok) {
        const copy = resp.clone();
        caches.open(CACHE).then((c) => c.put(event.request, copy));
      }
      return resp;
    }).catch(() =>
      caches.match(event.request).then((hit) => hit || Response.error())),
  );
});

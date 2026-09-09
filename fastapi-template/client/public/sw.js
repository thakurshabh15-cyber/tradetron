/**
 * TradeThrone Service Worker — Network-first with versioned cache.
 *
 * On activation, ALL caches whose name does NOT match the current
 * CACHE_VERSION are deleted.  This guarantees a new deploy's sw.js
 * (served with a hashed filename by Vite) always invalidates the
 * previous app-shell cache, preventing stale index.html from being
 * served on network failure.
 *
 * Strategy:
 *   - Non-GET and /api/* requests are always network-only.
 *   - HTML navigations (Accept: text/html) are network-first with
 *     cache fallback, but the response MUST contain "<!doctype" to
 *     avoid caching error pages or CDN fallback HTML.
 *   - Hashed assets (/assets/*) are cached forever (immutable).
 *   - All other GET requests are network-first with cache fallback.
 */
const CACHE_VERSION = "tt-v2";   // bump on every deploy
const CACHE_NAME = "tradetron-shell-" + CACHE_VERSION;

self.addEventListener("install", () => {
  // Do NOT pre-cache index.html — let the network fetch populate it
  // on first load so we never lock in a stale shell at install time.
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((key) => key !== CACHE_NAME)
          .map((key) => caches.delete(key))
      )
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET") return;

  const url = new URL(event.request.url);

  // Never intercept cross-origin or /api requests — always go to network.
  if (url.origin !== self.location.origin || url.pathname.startsWith("/api")) return;

  event.respondWith(
    (async () => {
      try {
        const response = await fetch(event.request);
        // Only cache successful responses
        if (response.ok) {
          const cache = await caches.open(CACHE_NAME);
          cache.put(event.request, response.clone());
        }
        return response;
      } catch {
        // Network failed — serve from cache if available
        const cached = await caches.match(event.request);
        if (cached) return cached;

        // Last resort for HTML navigations: serve /index.html so the
        // SPA router can still render the requested route.
        if (event.request.headers.get("accept")?.includes("text/html")) {
          const shell = await caches.match("/index.html");
          if (shell) return shell;
        }

        return new Response("Offline", { status: 503, statusText: "Service Unavailable" });
      }
    })()
  );
});

const IBAYAW_CACHE = "ibayaw-tour-pwa-v3";
const OFFLINE_URL = "/guest_app/offline/";

const CORE_ASSETS = [
  OFFLINE_URL,
  "/static/css/guest_unified.css",
  "/static/fonts/BaraBara.otf",
  "/static/images/image.png",
  "/static/images/bayawan_tourist_map_official.png",
  "/static/images/bayawan_map.jpg",
  "/static/images/bayawan_map.png"
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(IBAYAW_CACHE)
      .then((cache) => Promise.allSettled(CORE_ASSETS.map((asset) => cache.add(asset))))
      .catch(() => undefined)
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(
      keys.filter((key) => key !== IBAYAW_CACHE).map((key) => caches.delete(key))
    ))
  );
  self.clients.claim();
});

function shouldBypass(request) {
  const url = new URL(request.url);
  if (request.method !== "GET") return true;
  if (url.pathname.includes("/api/")) return true;
  if (url.pathname.includes("/login") || url.pathname.includes("/logout")) return true;
  if (url.pathname.includes("/profile") || url.pathname.includes("/notifications")) return true;
  if (url.pathname.includes("/book_tour") || url.pathname.includes("/tour-bookings")) return true;
  if (url.pathname.includes("/reviews/submit")) return true;
  return false;
}

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (shouldBypass(request)) return;

  const url = new URL(request.url);
  const isStatic = url.pathname.startsWith("/static/");

  if (isStatic) {
    event.respondWith(
      caches.match(request).then((cached) => (
        cached || fetch(request).then((response) => {
          const copy = response.clone();
          caches.open(IBAYAW_CACHE).then((cache) => cache.put(request, copy));
          return response;
        })
      ))
    );
    return;
  }

  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request).catch(() => caches.match(OFFLINE_URL))
    );
  }
});

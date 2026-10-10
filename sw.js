// Service worker for حاسبة أسعار الذهب | مجوهرات القدس
//
// The page itself (index.html / navigations) and price.json are both
// network-first: this is a live pricing calculator, so a returning visitor
// must always get the latest logic and the latest numbers, never a stale
// cached copy. The cache here only kicks in as an offline fallback.
// Rarely-changing static assets (logo, icons, manifest) are cache-first
// for a fast, app-like load.

const CACHE_NAME = 'alquds-gold-calc-v2';
const CORE_ASSETS = [
  './',
  './index.html',
  './logo.png',
  './manifest.json',
  './icons/icon-192.png',
  './icons/icon-512.png'
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME)
      .then((cache) => cache.addAll(CORE_ASSETS))
      .catch(() => {})
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key)))
    )
  );
  self.clients.claim();
});

function networkFirst(event) {
  event.respondWith(
    fetch(event.request)
      .then((res) => {
        const copy = res.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy));
        return res;
      })
      .catch(() => caches.match(event.request).then((cached) => cached || caches.match('./index.html')))
  );
}

function cacheFirst(event) {
  event.respondWith(
    caches.match(event.request).then((cached) => {
      const networkFetch = fetch(event.request)
        .then((res) => {
          const copy = res.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy));
          return res;
        })
        .catch(() => cached);
      return cached || networkFetch;
    })
  );
}

self.addEventListener('fetch', (event) => {
  if (event.request.method !== 'GET') return;

  const url = new URL(event.request.url);
  const isPage = event.request.mode === 'navigate' ||
    url.pathname.endsWith('index.html') ||
    url.pathname.endsWith('price.json');

  if (isPage) {
    networkFirst(event);
  } else {
    cacheFirst(event);
  }
});

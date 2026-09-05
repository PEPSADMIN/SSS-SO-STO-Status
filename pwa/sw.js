/* So Sto Status service worker
   - App-shell (HTML pages) cached network-first, with offline fallback.
   - Static assets (icons) cached cache-first.
   - API GET responses cached stale-while-revalidate so the last-seen data is
     available offline; live data is fetched when online.
   Auth is cookie-based, so credentials are included on every fetch.
*/
const CACHE = 'sss-tool-v2';
const APP_SHELL = [
  '/',
  '/login',
  '/manifest.webmanifest',
  '/static/icon-192.png',
  '/static/icon-512.png',
  '/static/apple-touch-icon.png'
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(APP_SHELL)).catch(() => {})
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

function isApiGet(url) {
  return url.pathname.startsWith('/api/') && url.method === 'GET';
}

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  // Login + app pages: network-first, fall back to cached shell when offline.
  if (!isApiGet(url)) {
    event.respondWith(
      fetch(req, { credentials: 'include' })
        .then((res) => {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(req, copy)).catch(() => {});
          return res;
        })
        .catch(async () => {
          const cached = await caches.match(req);
          if (cached) return cached;
          const shell = await caches.match('/');
          return shell || new Response('Offline', { status: 503, headers: { 'Content-Type': 'text/plain' } });
        })
    );
    return;
  }

  // API GET: stale-while-revalidate (instant cached data, refresh in background).
  event.respondWith(
    caches.open(CACHE).then(async (c) => {
      const cached = await c.match(req);
      const network = fetch(req, { credentials: 'include' })
        .then((res) => {
          c.put(req, res.clone()).catch(() => {});
          return res;
        })
        .catch(() => cached);
      return cached || network;
    })
  );
});

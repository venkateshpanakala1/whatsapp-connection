// Minimal service worker — its only job is to make the site installable as a
// PWA (Chrome/Samsung Internet require an active SW for the install prompt).
// API calls and pages always go to the network; nothing is cached, so
// logged-in state, live status and SSE streams behave exactly like the
// regular site.

const CACHE_NAME = 'v3-shell-v1';
const SHELL_ASSETS = ['/style.css', '/manifest.json'];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_ASSETS)).catch(() => {})
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const { request } = event;
  if (request.method !== 'GET') return;

  const url = new URL(request.url);
  const isShellAsset = SHELL_ASSETS.includes(url.pathname);
  if (!isShellAsset) return; // let the browser handle everything else normally

  event.respondWith(
    caches.match(request).then((cached) => cached || fetch(request))
  );
});

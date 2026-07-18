// Minimal service worker — its only job is to make the site installable as a
// PWA (Chrome/Samsung Internet require an active SW for the install prompt).
// API calls and pages always go to the network; nothing is cached, so
// logged-in state, live status and SSE streams behave exactly like the
// regular site.

const CACHE_NAME = 'v7-shell-v2';
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

// Fires even when no tab/window is open — this is what makes a reply arrive
// as a real OS notification regardless of whether the app is running.
self.addEventListener('push', (event) => {
  let data = {};
  try { data = event.data.json(); } catch (e) {}

  const title = data.title || 'New message';
  const options = {
    body: data.body || '',
    icon: '/icon-192.png',
    badge: '/icon-192.png',
    silent: false,             // play the OS/browser's default notification sound
    vibrate: [200, 100, 200],  // mobile: short-pause-short buzz
    data: { url: data.url || '/replies' },
  };

  event.waitUntil(
    Promise.all([
      self.registration.showNotification(title, options),
      // If the app happens to be open, tell it to refresh immediately
      // instead of waiting for its next poll tick.
      self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clients) => {
        clients.forEach((client) => client.postMessage({ type: 'new-reply' }));
      }),
    ])
  );
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = event.notification.data?.url || '/replies';
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((windowClients) => {
      for (const client of windowClients) {
        if (client.url.includes(url) && 'focus' in client) return client.focus();
      }
      if (self.clients.openWindow) return self.clients.openWindow(url);
    })
  );
});

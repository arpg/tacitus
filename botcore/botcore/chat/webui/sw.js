/* Tacitus web chat: the service worker. It keeps a copy of the app's
 * static files so the installed app opens without a network, and fetches
 * them fresh whenever it can; a fetch that has not answered in four
 * seconds (the host off the tailnet) gives way to the copy. API requests
 * (every path with /api/ in it: messages, files, the event stream,
 * pairing) and the relay's routes (/r/<install>/..., /healthz) are never
 * answered from or written to the cache; the worker leaves them to the
 * browser. */
'use strict';

const CACHE = 'tacitus-webchat-v2';
const SHELL = ['./', 'app.js', 'qr.js', 'app.css', 'manifest.webmanifest', 'favicon.svg', 'icon-192.png', 'icon-512.png'];
const RELAY_ROUTE = /\/r\/[a-z2-7]{26}\//;
const SLOW_MS = 4000;

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE)
    .then((cache) => cache.addAll(SHELL))
    .catch(() => {})
    .then(() => self.skipWaiting()));
});

self.addEventListener('activate', (event) => {
  event.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

// A push from the bot (web_push.py): {title, body, url, tag}. One
// notification per tag (a thread); a new one in the thread alerts again.
self.addEventListener('push', (event) => {
  let d = {};
  try { d = event.data ? event.data.json() : {}; } catch { /* not JSON: the title alone */ }
  event.waitUntil(self.registration.showNotification(String(d.title || 'Tacitus'), {
    body: String(d.body || ''),
    icon: new URL('icon-192.png', self.registration.scope).href,
    tag: d.tag ? String(d.tag) : undefined,
    renotify: Boolean(d.tag),
    data: { url: String(d.url || '') },
  }));
});

// A tap opens the app at the push's url: in the app's window last in front
// (on iOS the installed app's own), which gets the url as a message where
// navigate() fails, else in a new one. Anything outside the app opens the app.
function appUrl(raw) {
  const scope = new URL(self.registration.scope);
  try {
    const url = new URL(raw || '', scope);
    if (url.origin === scope.origin && url.pathname.startsWith(scope.pathname)) return url.href;
  } catch { /* not a URL */ }
  return scope.href;
}

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = appUrl(event.notification.data && event.notification.data.url);
  event.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((wins) => {
    const win = wins.find((w) => w.url.startsWith(self.registration.scope));
    if (!win) return self.clients.openWindow(url);
    return win.focus().then(() => win.navigate(url)).catch(() => win.postMessage({ open: url }));
  }));
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  const url = new URL(req.url);
  const scope = new URL(self.registration.scope);
  if (req.method !== 'GET' || url.origin !== scope.origin) return;
  if (url.pathname.includes('/api/')) return;
  if (RELAY_ROUTE.test(url.pathname) || url.pathname.endsWith('/healthz')) return;
  if (!url.pathname.startsWith(scope.pathname)) return;
  const copy = () => caches.match(req, { ignoreSearch: true })
    .then((hit) => hit || caches.match(new URL('./', scope).href));
  const net = fetch(req).then((res) => {
    if (res.ok && res.type === 'basic') {
      const fresh = res.clone();
      caches.open(CACHE).then((cache) => cache.put(req, fresh));
    }
    return res;
  });
  event.respondWith(new Promise((resolve) => {
    const slow = setTimeout(() => copy().then((hit) => hit && resolve(hit)), SLOW_MS);
    net.then((res) => { clearTimeout(slow); resolve(res); }).catch(() => {
      clearTimeout(slow);
      copy().then((hit) => resolve(hit || Response.error()));
    });
  }));
});

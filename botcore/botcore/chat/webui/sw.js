/* Tacitus web chat: the service worker's loader. The browser takes this file
 * and the bundle.js it imports only from the page's origin (the bot on the
 * tailnet, the relay on the relay's origin), so it stays small and stable,
 * and the worker's code is in bundle.js (docs/webchat.md, Code integrity).
 * At each start it runs the copy of bundle.js the worker kept, from Cache
 * Storage, when its sha256 is the trusted manifest's: a release the page
 * brought through the relay runs here from the next start. Else (a first
 * load, a refusal, a browser that refuses it) it runs the copy that came
 * with this file. The API (every path with /api/ in it), the relay's routes
 * and the signed manifest are left to the browser, never cached. */
'use strict';

importScripts('bundle.js');

// Shared with bundle.js: the caches' names and the interface (TacitusWorker.v).
const CACHE = 'tacitus-webchat-v3';
const STATE = `${CACHE}-bundle`;
const API = 1;
const RELAY_ROUTE = /\/r\/[a-z2-7]{26}\//;
const fetched = self.TacitusWorker;

// The kept bundle.js, as text, when it hashes to the trusted manifest; else null.
async function keptCode() {
  const at = (name) => new URL(name, self.registration.scope).href;
  const trusted = await caches.open(STATE).then((c) => c.match(at('__bundle/trusted'))).then((r) => (r ? r.json() : null));
  const want = trusted && trusted.files && trusted.files['bundle.js'];
  const hit = want && await caches.open(CACHE).then((c) => c.match(at('bundle.js')));
  if (!hit) return null;
  const bytes = new Uint8Array(await hit.arrayBuffer());
  const sum = new Uint8Array(await crypto.subtle.digest('SHA-256', bytes));
  return Array.from(sum, (b) => b.toString(16).padStart(2, '0')).join('') === want ? new TextDecoder().decode(bytes) : null;
}

// The one text this worker runs as code is that checked copy.
const worker = keptCode().then((code) => {
  if (!code) return null;
  self.TacitusWorker = null;
  (0, eval)(code);
  const kept = self.TacitusWorker;
  return kept && kept.v === API ? kept.start({ CACHE, STATE }) : null;
}).catch(() => null).then((w) => w || fetched.start({ CACHE, STATE }));

const run = (event, name) => event.waitUntil(worker.then((w) => w[name](event)));
self.addEventListener('install', (event) => run(event, 'install'));
self.addEventListener('activate', (event) => run(event, 'activate'));
self.addEventListener('push', (event) => run(event, 'push'));
self.addEventListener('notificationclick', (event) => run(event, 'click'));
self.addEventListener('message', (event) => run(event, 'message'));

self.addEventListener('fetch', (event) => {
  const req = event.request;
  const url = new URL(req.url);
  const scope = new URL(self.registration.scope);
  if (req.method !== 'GET' || url.origin !== scope.origin) return;
  if (url.pathname.includes('/api/')) return;
  if (RELAY_ROUTE.test(url.pathname) || url.pathname.endsWith('/healthz')) return;
  if (!url.pathname.startsWith(scope.pathname) || url.pathname.endsWith('/manifest.sig.json')) return;
  event.respondWith(worker.then((w) => w.serve(req)));
});

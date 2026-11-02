/* Tacitus web chat: the service worker. It keeps a copy of the app's
 * static files so the installed app opens without a network, and fetches
 * them fresh whenever it can; a fetch that has not answered in four
 * seconds (the host off the tailnet) gives way to the copy. API requests
 * (every path with /api/ in it: messages, files, the event stream,
 * pairing), the relay's routes (/r/<install>/..., /healthz) and the signed
 * manifest are never answered from or written to the cache; the worker
 * leaves them to the browser.
 *
 * It serves only code the bot signed (docs/webchat.md, Code integrity):
 * once the page hands it the bot's signed manifest (adopt), a file from the
 * network is served and kept only when its sha256 is the manifest's. Before
 * that (a first load) it keeps each file, and the first manifest must match
 * them all. A file that matches no signed manifest stops the app: the copy
 * goes, and each page shows a refusal with no script until "Check again".
 * Off the tailnet the page brings a new release from the bot through the
 * relay, and the worker keeps it only when each file matches the manifest. */
'use strict';

importScripts('bundle.js');

const CACHE = 'tacitus-webchat-v3';
// The trusted manifest, a refusal, a suspect file: kept with the copies they speak for.
const STATE = `${CACHE}-bundle`;
const SHELL = ['./', 'app.js', 'qr.js', 'bundle.js', 'app.css', 'manifest.webmanifest', 'favicon.svg', 'icon-192.png', 'icon-512.png'];
const RELAY_ROUTE = /\/r\/[a-z2-7]{26}\//;
const SLOW_MS = 4000;

const stateKey = (name) => new URL(`__bundle/${name}`, self.registration.scope).href;
const getState = (name) => caches.open(STATE).then((c) => c.match(stateKey(name))).then((r) => (r ? r.json() : null));
const setState = (name, value) => caches.open(STATE).then((c) => (value == null ? c.delete(stateKey(name))
  : c.put(stateKey(name), new Response(JSON.stringify(value)))));
const hashOf = async (res) => sha256Hex(new Uint8Array(await res.clone().arrayBuffer()));
const kept = (req) => caches.open(CACHE).then((c) => c.match(req, { ignoreSearch: true }));
const keep = (req, res) => caches.open(CACHE).then((c) => c.put(req, res.clone())).catch(() => {});

self.addEventListener('install', (event) => {
  // A worker that follows one with a trusted manifest keeps its checked copies.
  event.waitUntil(getState('trusted')
    .then((trusted) => trusted || caches.open(CACHE).then((cache) => cache.addAll(SHELL)))
    .catch(() => {})
    .then(() => self.skipWaiting()));
});

self.addEventListener('activate', (event) => {
  event.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((k) => k !== CACHE && k !== STATE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

// A push from the bot (web_push.py): {title, body, url, tag}, one notification per tag (a thread), a new
// one alerting again; {type: 'clear', tag} (the owner wrote there) closes that tag's notifications, shows none.
self.addEventListener('push', (event) => {
  let d = {}; try { d = event.data ? event.data.json() : {}; } catch { /* not JSON: the title alone */ }
  if (d.type === 'clear') return event.waitUntil(d.tag ? self.registration.getNotifications({ tag: String(d.tag) }).then((ns) => ns.forEach((n) => n.close())) : Promise.resolve());
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
  if (!url.pathname.startsWith(scope.pathname) || url.pathname.endsWith('/manifest.sig.json')) return;
  event.respondWith(serve(req, url, scope));
});

// The network's answer, or the kept copy when the network fails or is slow
// ({res, copy: true}); a page with no copy of its own gets the start page.
function fetchOrCopy(req, scope) {
  const copy = () => kept(req).then((hit) => hit
    || (req.mode === 'navigate' ? kept(new URL('./', scope).href) : undefined));
  const net = fetch(req);
  return new Promise((resolve) => {
    const slow = setTimeout(() => copy().then((hit) => hit && resolve({ res: hit, copy: true })), SLOW_MS);
    net.then((res) => { clearTimeout(slow); resolve({ res, copy: false }); }).catch(() => {
      clearTimeout(slow);
      copy().then((hit) => resolve({ res: hit || Response.error(), copy: true }));
    });
  });
}

async function serve(req, url, scope) {
  const refused = await getState('refused');
  if (refused) {
    if (req.mode !== 'navigate' || !url.searchParams.has('recheck')) return refusal(req, refused.file);
    // "Check again": a first load again, which the next manifest checks.
    await Promise.all(['refused', 'trusted', 'suspect'].map((k) => setState(k, null)));
    return Response.redirect(new URL('./', scope).href, 303);
  }
  const { res, copy } = await fetchOrCopy(req, scope);
  if (!res.ok || res.type !== 'basic' || copy) return res; // a kept copy was checked when kept
  const trusted = await getState('trusted');
  if (!trusted) return keep(req, res).then(() => res); // the first manifest checks it (adopt)
  const path = bundlePath(url, scope, trusted.files);
  if (trusted.files[path] === await hashOf(res)) return keep(req, res).then(() => res);
  // Not the signed file. The checked copy serves until the page brings the
  // bot's manifest, which tells a new release from a changed file (adopt).
  const old = await kept(req);
  if (old) return setState('suspect', true).then(() => old);
  await refuse(path);
  return refusal(req, path);
}

// Stops the app: no copy is kept, and each page of the app goes to the
// refusal (a page this worker does not control hears it as a message).
async function refuse(path) {
  await caches.delete(CACHE);
  await setState('refused', { file: path });
  const start = new URL('./', self.registration.scope).href;
  const wins = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
  wins.forEach((w) => w.navigate(start).catch(() => w.postMessage({ refused: path })));
  return { refused: path };
}

const esc = (s) => String(s).replace(/[&<>"]/g, (c) => `&#${c.charCodeAt(0)};`);

function refusal(req, path) {
  if (req.mode !== 'navigate') return Response.error();
  const page = '<!doctype html><html lang="en"><head><meta charset="utf-8">'
    + '<meta name="viewport" content="width=device-width, initial-scale=1"><title>Tacitus stopped</title></head><body>'
    + '<h1>Tacitus stopped</h1>'
    + `<p>This copy of the app is not the one your Tacitus signed: <code>${esc(path)}</code> does not match it. `
    + 'The phone deleted its copy of the app and runs none of it.</p>'
    + '<p>The server that sent the app sent a file your Tacitus did not sign: one from another version, '
    + 'or a changed file. Open the app on your own network, or tell whoever runs the away-from-home connection.</p>'
    + '<p><a href="./?recheck">Check again</a></p></body></html>';
  return new Response(page, { headers: { 'Content-Type': 'text/html; charset=utf-8',
    'Content-Security-Policy': "default-src 'none'; base-uri 'none'; form-action 'none'" } });
}

// A file the page brought ({type, bytes}, from the bot): it keeps the
// headers the bot sent with the kept copy, the page's security policy among them.
const KEEP_HEADERS = ['content-security-policy', 'x-content-type-options', 'referrer-policy', 'cross-origin-resource-policy'];
function brought(file, had) {
  const headers = new Headers({ 'content-type': String(file.type || 'application/octet-stream') });
  if (had) KEEP_HEADERS.forEach((k) => had.headers.has(k) && headers.set(k, had.headers.get(k)));
  return new Response(file.bytes, { headers });
}

// The page hands over the bot's signed manifest (it fetched it from the
// bot and checked it); `pin` keeps its key, so the relay's copy of the app
// takes a later one only under it. The first manifest must match every file
// kept so far. A kept copy it does not name is replaced by the network's,
// which must match; after a suspect file every file is fetched again.
// Off the tailnet (`relay`) it fetches nothing: for a manifest under the key
// it trusts, it answers the files whose copy the manifest does not name
// (`want`), and the page brings them from the bot through the relay
// (`files`). It keeps them only when all are there and each hashes to the manifest.
async function adopt(doc, pin, relay, files) {
  const old = await getState('trusted');
  if (relay && !old) return { want: [] }; // no manifest checked on the tailnet to build on
  const v = await verifyBundle(doc, null, old && (old.pin || relay) ? old.key : null);
  const suspect = await getState('suspect');
  if (old && old.hash === v.hash && !suspect && !relay) return { ok: true };
  const scope = new URL(self.registration.scope);
  const cache = await caches.open(CACHE);
  if (!old) {
    for (const req of await cache.keys()) {
      const path = bundlePath(new URL(req.url), scope, v.files);
      if (v.files[path] !== await hashOf(await cache.match(req))) return refuse(path);
    }
  }
  let updated = false;
  const put = [];
  const want = [];
  for (const [path, hash] of Object.entries(v.files)) {
    const url = new URL(path === 'index.html' ? './' : path, scope).href;
    const had = await cache.match(url);
    const good = Boolean(had) && await hashOf(had) === hash;
    if (good && (relay || !suspect)) continue;
    if (relay && !(files && files[path])) { want.push(path); continue; }
    if (relay && !had && path.endsWith('.html')) return { error: `no security policy to keep for ${path}` };
    const res = relay ? brought(files[path], had) : await fetch(url, { cache: 'no-store' }).catch(() => null);
    if (!res || !res.ok) continue; // out of reach: the copy stays
    if (await hashOf(res) !== hash) return relay ? { error: `${path} does not match the manifest` } : refuse(path);
    put.push([url, res]);
    updated ||= Boolean(had) && !good;
  }
  if (want.length) return { want };
  for (const [url, res] of put) await cache.put(url, res);
  await setState('trusted', { hash: v.hash, key: v.key, files: v.files, pin: Boolean(pin || (old && old.pin)) });
  await setState('suspect', null);
  return { ok: true, updated };
}

self.addEventListener('message', (event) => {
  const msg = event.data || {};
  const reply = (out) => event.ports[0] && event.ports[0].postMessage(out);
  const fail = (e) => reply({ error: String(e.message || e) });
  if (msg.bundle) event.waitUntil(adopt(msg.bundle, msg.pin).then(reply, fail));
  else if (msg.fromRelay) event.waitUntil(adopt(msg.fromRelay, false, true, msg.files).then(reply, fail));
  else if (msg.refuse) event.waitUntil(refuse(String(msg.refuse)).then(reply));
});

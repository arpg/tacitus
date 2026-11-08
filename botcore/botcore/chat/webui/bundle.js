/* Tacitus web chat: the check of the app's code (docs/webchat.md, Code
 * integrity), shared by the page (app.js) and the service worker, and the
 * service worker's own code (TacitusWorker), which sw.js runs.
 * The bot signs a manifest of the app's files at start (each file's sha256,
 * a time, its Ed25519 public key) over the manifest's canonical JSON: keys
 * sorted, no spaces, UTF-8. The bundle hash, the sha256 of those bytes, is
 * what a pairing QR code carries. relay_crypto.py writes the same bytes;
 * tests/test_web_bundle.py and tests/test_webui.py hold the two together. */
'use strict';

(() => {
  const PATH = /^[A-Za-z0-9_-][A-Za-z0-9._-]*(\/[A-Za-z0-9_-][A-Za-z0-9._-]*)*$/;
  const HEX = /^[0-9a-f]{64}$/;
  const B64 = /^[A-Za-z0-9_-]+$/;

  class BundleError extends Error {}

  const keysOf = (o) => Object.keys(o).sort().join(',');
  const isObject = (o) => Boolean(o) && typeof o === 'object' && !Array.isArray(o);

  function fromB64(text) {
    if (typeof text !== 'string' || !B64.test(text)) throw new BundleError('not base64url');
    const s = atob(text.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - (text.length % 4)) % 4));
    return Uint8Array.from(s, (c) => c.charCodeAt(0));
  }

  async function sha256Hex(data) {
    const bytes = typeof data === 'string' ? new TextEncoder().encode(data) : data;
    const sum = new Uint8Array(await crypto.subtle.digest('SHA-256', bytes));
    return Array.from(sum, (b) => b.toString(16).padStart(2, '0')).join('');
  }

  // Keys sorted at each level, no spaces. Only a manifest of the checked
  // shape comes here: its strings are plain ASCII and its numbers integers,
  // which JSON.stringify writes as Python's json.dumps does.
  function canonical(value) {
    if (!isObject(value)) return JSON.stringify(value);
    return `{${Object.keys(value).sort().map((k) => `${JSON.stringify(k)}:${canonical(value[k])}`).join(',')}}`;
  }

  function checkShape(m) {
    if (!isObject(m) || keysOf(m) !== 'built_at,files,key,v' || m.v !== 1) throw new BundleError('not a manifest');
    if (!Number.isSafeInteger(m.built_at) || m.built_at < 0) throw new BundleError('bad built_at');
    if (!isObject(m.files)) throw new BundleError('bad files');
    for (const [path, sum] of Object.entries(m.files)) {
      if (!PATH.test(path) || typeof sum !== 'string' || !HEX.test(sum)) throw new BundleError(`bad entry ${path}`);
    }
    if (fromB64(m.key).length !== 32) throw new BundleError('bad key');
  }

  // A served document {manifest, signature}, checked: its shape, its
  // Ed25519 signature under the key it names, that key against `pinnedKey`
  // (base64url) and its bundle hash against `expectedHash` (hex), each
  // when given. Without either, the channel the document came by is the
  // only anchor: this origin, or the relay's sealed channel to the bot.
  // Answers {hash, key, files, builtAt}; throws a BundleError otherwise
  // (a plain Error in a browser with no Ed25519 in WebCrypto).
  async function verifyBundle(doc, expectedHash, pinnedKey) {
    if (!isObject(doc) || keysOf(doc) !== 'manifest,signature') throw new BundleError('not a signed manifest');
    const m = doc.manifest;
    checkShape(m);
    if (pinnedKey && m.key !== pinnedKey) throw new BundleError('signed by another key');
    const bytes = new TextEncoder().encode(canonical(m));
    const sig = fromB64(doc.signature);
    let key;
    try {
      key = await crypto.subtle.importKey('raw', fromB64(m.key), { name: 'Ed25519' }, false, ['verify']);
    } catch {
      throw new Error('this browser cannot check an Ed25519 signature'); // not a refusal: no check ran
    }
    if (sig.length !== 64 || !(await crypto.subtle.verify({ name: 'Ed25519' }, key, sig, bytes))) {
      throw new BundleError('the signature does not check');
    }
    const hash = await sha256Hex(bytes);
    if (expectedHash && hash !== expectedHash) throw new BundleError('not the bundle the pairing named');
    return { hash, key: m.key, files: m.files, builtAt: m.built_at };
  }

  // The manifest's name for a URL under the app's scope: its path below the
  // scope, or index.html for the scope itself and for a route of the app
  // (a last part with no extension), which the server answers with it.
  function bundlePath(url, scope, files) {
    let rel = url.pathname.slice(scope.pathname.length);
    try { rel = decodeURIComponent(rel); } catch { /* kept as it came */ }
    if (files && Object.hasOwn(files, rel)) return rel;
    return !rel || !rel.split('/').pop().includes('.') ? 'index.html' : rel;
  }

  /* -- The service worker's code (sw.js runs it) ----------------------- */

  // It keeps a copy of the app's files so the installed app opens without a
  // network, and fetches them fresh when it can (four seconds, then the copy).
  // It serves only code the bot signed: with the signed manifest the page hands
  // it (adopt), a file is served and kept only when its sha256 is the
  // manifest's; the first manifest must match each file kept before it. A file
  // that matches no signed manifest stops the app until "Check again". Off the
  // tailnet, and always on the relay's origin, the page brings a new release
  // from the bot through the relay, with the security headers it names (HEADERS).

  // Named by sw.js (start): the copies; the trusted manifest, a refusal, a suspect file.
  let [CACHE, STATE] = ['', ''];
  const SHELL = ['./', 'app.js', 'qr.js', 'bundle.js', 'app.css', 'manifest.webmanifest', 'favicon.svg', 'icon-192.png', 'icon-512.png'];
  const SLOW_MS = 4000;
  // The bot signs the security headers a kept copy carries (the page's policy among them)
  // as a file of the app: {path: {header: value}}.
  const HEADERS = 'headers.json';
  const KEEP_HEADERS = ['content-security-policy', 'x-content-type-options', 'referrer-policy', 'cross-origin-resource-policy'];

  const scopeUrl = () => new URL(self.registration.scope);
  const urlOf = (path) => new URL(path === 'index.html' ? './' : path, self.registration.scope).href;
  const stateKey = (name) => new URL(`__bundle/${name}`, self.registration.scope).href;
  const getState = (name) => caches.open(STATE).then((c) => c.match(stateKey(name))).then((r) => (r ? r.json() : null));
  const setState = (name, value) => caches.open(STATE).then((c) => (value == null ? c.delete(stateKey(name))
    : c.put(stateKey(name), new Response(JSON.stringify(value)))));
  const hashOf = async (res) => sha256Hex(new Uint8Array(await res.clone().arrayBuffer()));
  // Copies are kept and found by their exact URL: one with a query string is never kept, so no
  // address the page was opened at (`?relay=`) holds a copy that outlives an adoption.
  const kept = (req) => caches.open(CACHE).then((c) => c.match(req));
  const keep = (req, res) => (new URL(req.url || req).search ? Promise.resolve()
    : caches.open(CACHE).then((c) => c.put(req, res.clone())).catch(() => {}));

  function install() {
    // A worker that follows one with a trusted manifest keeps its checked copies.
    return getState('trusted')
      .then((trusted) => trusted || caches.open(CACHE).then((cache) => cache.addAll(SHELL)))
      .catch(() => {})
      .then(() => self.skipWaiting());
  }

  function activate() {
    return caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE && k !== STATE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim());
  }

  // A push from the bot (web_push.py): {title, body, url, tag}, one notification per tag (a thread), a new
  // one alerting again, and the app's open pages hear {shown: tag}; {type: 'clear', tag, title} (the thread
  // is done with elsewhere) closes that tag's notifications (clearThread). Either may carry `close`, the tags
  // no clear reached here (Apple's push service takes none; one past its budget is skipped): closed once this
  // push's own notification shows, never the tag it shows, so a push still shows one, as WebKit requires.
  function push(event) {
    let d = {}; try { d = event.data ? event.data.json() : {}; } catch { /* not JSON: the title alone */ }
    if (d.type === 'clear') return clearThread(d);
    const tag = d.tag ? String(d.tag) : undefined;
    return self.registration.showNotification(String(d.title || 'Tacitus'), {
      body: String(d.body || ''),
      icon: new URL('icon-192.png', self.registration.scope).href,
      tag,
      renotify: Boolean(tag),
      data: { url: String(d.url || '') },
    }).then(() => closeTags(closing(d.close, tag)))
      .then(() => tag && windows().then((ws) => ws.forEach((w) => w.postMessage({ shown: tag }))));
  }

  const closing = (tags, keep) => new Set((Array.isArray(tags) ? tags : []).filter(Boolean).map(String).filter((t) => t !== keep));
  const closeTags = (tags) => (tags.size ? self.registration.getNotifications()
    .then((ns) => ns.forEach((n) => tags.has(n.tag) && n.close())).catch(() => {}) : undefined);

  const windows = () => self.clients.matchAll({ type: 'window', includeUncontrolled: true }).catch(() => []);

  // How an endpoint's browser treats a push that shows nothing (docs/webchat.md, Notifications): 'apple'
  // (WebKit revokes the subscription after three), 'mozilla' (Firefox spends a quota that ends it, unless a
  // notification of the site is open three seconds on), 'chromium' (Chrome and Edge show their own line past
  // a small budget, unless a notification of the app is open as the push ends or a window of it is in view), ''.
  function pushService(endpoint) {
    let host = '';
    try { host = new URL(String(endpoint || '')).hostname.toLowerCase(); } catch { return ''; }
    if (host === 'web.push.apple.com') return 'apple';
    if (host === 'updates.push.services.mozilla.com') return 'mozilla';
    return /^(fcm\.googleapis\.com|jmt\d+\.google\.com|[a-z0-9-]+\.notify\.windows\.com)$/.test(host) ? 'chromium' : '';
  }

  // What a clear shows once the thread's notifications are closed, which no browser counts as one shown:
  // 'replace' (a quiet "Handled." under the tag) or 'none'. `left`: the app's notifications still open;
  // `inView`: a window of the app in view.
  function clearPlan({ service, left, inView }) {
    if (service === 'apple') return 'replace';
    return left > 0 || (service === 'chromium' && inView) ? 'none' : 'replace';
  }

  // The quiet "Handled." closes after this long: past the push's end, when Chrome counts, and past Firefox's
  // three seconds. A worker stopped sooner leaves it, and the thread's next push replaces it.
  const HANDLED_MS = 8000;

  // A clear. A lookup that fails takes the answer that shows the replacement, and the promise never
  // rejects: WebKit counts a rejected push as one that showed nothing.
  function clearThread(d) {
    const reg = self.registration;
    const tag = d.tag ? String(d.tag) : '';
    const tags = closing([tag, ...(Array.isArray(d.close) ? d.close : [])]);
    const lookups = [reg.getNotifications().catch(() => []), reg.pushManager.getSubscription().catch(() => null), windows()];
    return Promise.all(lookups).then(([open, sub, wins]) => {
      const mine = open.filter((n) => tags.has(n.tag));
      mine.forEach((n) => n.close());
      const plan = clearPlan({ service: pushService(sub && sub.endpoint), left: open.length - mine.length,
        inView: wins.some((w) => w.visibilityState === 'visible') });
      if (plan === 'none') return undefined;
      return reg.showNotification(String(d.title || 'Tacitus'), { body: 'Handled.', icon: new URL('icon-192.png', reg.scope).href,
        tag: tag || undefined, silent: true, data: { url: '', handled: true } })
        // Outside the push's lifetime: Chrome counts the open notifications as it ends.
        .then(() => { setTimeout(() => reg.getNotifications(tag ? { tag } : undefined)
          .then((ns) => ns.forEach((n) => n.data && n.data.handled && n.close())).catch(() => {}), HANDLED_MS); });
    }).catch(() => {});
  }

  // A tap opens the app at the push's url: in the app's window last in front
  // (on iOS the installed app's own), which gets the url as a message where
  // navigate() fails, else in a new one. Anything outside the app opens the app,
  // at its #/ route: the bot's links name its own address, and this copy may
  // run at another (the website's copy, the bot's machine).
  function appUrl(raw) {
    const scope = scopeUrl();
    try {
      const url = new URL(raw || '', scope);
      if (url.origin === scope.origin && url.pathname.startsWith(scope.pathname)) return url.href;
      if (/^#\/[^#]+$/.test(url.hash)) return new URL(url.hash, scope).href;
    } catch { /* not a URL */ }
    return scope.href;
  }

  function click(event) {
    event.notification.close();
    const url = appUrl(event.notification.data && event.notification.data.url);
    return self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((wins) => {
      const win = wins.find((w) => w.url.startsWith(self.registration.scope));
      if (!win) return self.clients.openWindow(url);
      return win.focus().then(() => win.navigate(url)).catch(() => win.postMessage({ open: url }));
    });
  }

  // The network's answer, or the kept copy when the network fails or is slow
  // ({res, copy: true}); a page of the app gets the kept start page.
  function fetchOrCopy(req) {
    const copy = () => kept(req.mode === 'navigate' ? urlOf('index.html') : req);
    const net = fetch(req);
    return new Promise((resolve) => {
      const slow = setTimeout(() => copy().then((hit) => hit && resolve({ res: hit, copy: true })), SLOW_MS);
      net.then((res) => { clearTimeout(slow); resolve({ res, copy: false }); }).catch(() => {
        clearTimeout(slow);
        copy().then((hit) => resolve({ res: hit || Response.error(), copy: true }));
      });
    });
  }

  async function serve(req) {
    const url = new URL(req.url);
    const scope = scopeUrl();
    const refused = await getState('refused');
    if (refused) {
      if (req.mode !== 'navigate' || !url.searchParams.has('recheck')) return refusal(req, refused.file);
      // "Check again": a first load again, which the next manifest checks.
      await Promise.all(['refused', 'trusted', 'suspect'].map((k) => setState(k, null)));
      return Response.redirect(scope.href, 303);
    }
    const { res, copy } = await fetchOrCopy(req);
    if (!res.ok || res.type !== 'basic' || copy) return res; // a kept copy was checked when kept
    const trusted = await getState('trusted');
    if (!trusted) return keep(req, res).then(() => res); // the first manifest checks it (adopt)
    const path = bundlePath(url, scope, trusted.files);
    // A file the manifest does not name (the website's release.txt, a stray link) is no
    // code of the app: it is not served, and it stops nothing.
    if (!Object.hasOwn(trusted.files, path)) return new Response('Not found', { status: 404 });
    if (trusted.files[path] === await hashOf(res)) {
      const named = trusted.headers && trusted.headers[path];
      const out = named ? keepAs(await res.arrayBuffer(), res.headers.get('content-type'), named) : res;
      return keep(urlOf(path), out).then(() => out);
    }
    // Not the signed file. The checked copy serves until the page brings the
    // bot's manifest, which tells a new release from a changed file (adopt).
    const old = await kept(urlOf(path));
    if (old) return setState('suspect', true).then(() => old);
    await refuse(path);
    return refusal(req, path);
  }

  // Stops the app: no copy is kept, and each page of the app goes to the
  // refusal (a page this worker does not control hears it as a message).
  async function refuse(path) {
    await caches.delete(CACHE);
    await setState('refused', { file: path });
    const wins = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    wins.forEach((w) => w.navigate(scopeUrl().href).catch(() => w.postMessage({ refused: path })));
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

  // A copy to keep: `body` of `type`, with the KEEP_HEADERS the release
  // names for it (`named`), else those of `from` (the copy it replaces).
  function keepAs(body, type, named, from) {
    const headers = new Headers({ 'content-type': String(type || 'application/octet-stream') });
    KEEP_HEADERS.forEach((k) => { const value = named ? named[k] : from && from.headers.get(k); if (value) headers.set(k, value); });
    return new Response(body, { headers });
  }

  // The release's headers block: the copy this adoption keeps, or the one
  // kept before; null when the manifest names none (an older bot) or its
  // copy is out of reach. A block of another shape keeps nothing.
  async function headersBlock(files, put) {
    const res = !files[HEADERS] ? null : put.has(HEADERS) ? put.get(HEADERS)[1] : await kept(urlOf(HEADERS));
    if (!res || await hashOf(res) !== files[HEADERS]) return null;
    const block = await res.clone().json().catch(() => null);
    const ok = isObject(block) && Object.entries(block).every(([path, named]) => Object.hasOwn(files, path)
      && isObject(named) && Object.entries(named).every(([k, value]) => KEEP_HEADERS.includes(k) && typeof value === 'string'));
    if (!ok) throw new BundleError(`${HEADERS} is not a headers block`);
    return block;
  }

  // The page hands over the bot's signed manifest, checked; `pin` keeps its
  // key (the relay's copy of the app). The first manifest must match each
  // kept file. From this origin, a kept copy the manifest does not name is
  // replaced by the network's, which must match (after a suspect file, each).
  // `sealed`: the manifest came from the bot through the relay. The worker
  // fetches nothing; it answers the files it lacks (`want`) and the page
  // brings them (`files`). Files are kept all or none, with the release's
  // headers, before the manifest is trusted: the copy moves only to the bot's.
  // `replace`: the website's copy of the app (a computer set up with a code),
  // which may be another release than the bot's: at the first manifest every
  // kept entry that is not a file the manifest names, at its exact URL, goes,
  // each kept file that differs is replaced by the bot's, and none stops the
  // app. That first adoption reports `updated` once it writes headers, so the
  // page reloads under the bot's policy rather than the website's.
  async function adopt(doc, pin, sealed, files, replace) {
    const old = await getState('trusted');
    if (sealed && !old && !pin) return { want: [] }; // the bot's own copy checks first on the tailnet
    const v = await verifyBundle(doc, null, old && (old.pin || sealed) ? old.key : null);
    const suspect = await getState('suspect');
    if (old && old.hash === v.hash && !suspect && !sealed) return { ok: true };
    const cache = await caches.open(CACHE);
    if (!old && replace) {
      const named = new Set(Object.keys(v.files).map(urlOf));
      for (const req of await cache.keys()) if (!named.has(req.url)) await cache.delete(req);
    }
    if (!old && !replace) {
      for (const req of await cache.keys()) {
        const path = bundlePath(new URL(req.url), scopeUrl(), v.files);
        if (v.files[path] !== await hashOf(await cache.match(req))) return refuse(path);
      }
    }
    let updated = false;
    const put = new Map(); // path -> [the copy it replaces, the new copy]
    const good = new Map(); // path -> a kept copy that matches
    const want = [];
    for (const [path, hash] of Object.entries(v.files)) {
      const had = await cache.match(urlOf(path));
      const same = Boolean(had) && await hashOf(had) === hash;
      if (same) good.set(path, had);
      if (same && (sealed || !suspect)) continue;
      if (sealed && !(files && files[path])) { want.push(path); continue; }
      const res = sealed ? new Response(files[path].bytes, { headers: { 'content-type': String(files[path].type || '') } })
        : await fetch(urlOf(path), { cache: 'no-store' }).catch(() => null);
      if (!res || !res.ok) continue; // out of reach: the copy stays
      if (await hashOf(res) !== hash) return sealed ? { error: `${path} does not match the manifest` } : refuse(path);
      put.set(path, [had, res]);
      updated ||= Boolean(had) && !same;
    }
    if (want.length) return { want };
    const block = await headersBlock(v.files, put);
    for (const [path, [had, res]] of put) {
      const named = block && block[path];
      if (sealed && !named && !had && path.endsWith('.html')) return { error: `no security policy to keep for ${path}` };
      if (sealed || named) put.set(path, [had, keepAs(await res.arrayBuffer(), res.headers.get('content-type'), named, had)]);
      updated ||= Boolean(replace && !old && named);
    }
    // A release that changes only the headers: each kept copy takes them.
    for (const [path, named] of Object.entries(block || {})) {
      const had = !put.has(path) && good.get(path);
      if (!had || KEEP_HEADERS.every((k) => (named[k] || null) === had.headers.get(k))) continue;
      put.set(path, [had, keepAs(await had.arrayBuffer(), had.headers.get('content-type'), named)]);
      updated ||= Boolean(old || replace);
    }
    for (const [path, [, res]] of put) await cache.put(urlOf(path), res);
    await setState('trusted', { hash: v.hash, key: v.key, files: v.files, headers: block, pin: Boolean(pin || (old && old.pin)) });
    await setState('suspect', null);
    return { ok: true, updated };
  }

  function message(event) {
    const msg = event.data || {};
    const reply = (out) => event.ports[0] && event.ports[0].postMessage(out);
    const fail = (e) => reply({ error: String(e.message || e) });
    if (msg.bundle) return adopt(msg.bundle, msg.pin).then(reply, fail);
    if (msg.fromRelay) return adopt(msg.fromRelay, msg.pin, true, msg.files, msg.replace).then(reply, fail);
    if (msg.refuse) return refuse(String(msg.refuse)).then(reply);
    return undefined;
  }

  // sw.js calls start once, then hands each event to its function. `v` numbers this interface.
  const start = (names) => { [CACHE, STATE] = [names.CACHE, names.STATE]; return { install, activate, push, click, message, serve }; };

  Object.assign(self, { BundleError, verifyBundle, sha256Hex, bundlePath,
    TacitusWorker: { v: 1, start, pushService, clearPlan } });
})();

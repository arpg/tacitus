/* Tacitus web chat: the check of the app's code (docs/webchat.md, Code
 * integrity). The page (app.js) and the service worker (sw.js) share it.
 *
 * The bot signs a manifest of the app's files when it starts: each file's
 * sha256, a time, and the bot's Ed25519 public key, signed over the
 * manifest's canonical JSON (keys sorted, no spaces, UTF-8). The bundle
 * hash is the sha256 of those bytes; a pairing QR code carries it.
 * relay_crypto.py writes the same bytes; tests/test_web_bundle.py and
 * tests/test_webui.py hold the two together.
 */
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

  Object.assign(self, { BundleError, verifyBundle, sha256Hex, bundlePath });
})();

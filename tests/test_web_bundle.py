"""The web app's signed bundle (docs/webchat.md, Code integrity).

The manifest's canonical form and bundle hash, the Ed25519 signature and
what it refuses, the files a signing covers, the routes that serve the
signed manifest, the signing key in the relay key file, a phone's
attestation recorded on its device (which grants nothing; the owner's
`cards` flag does), and a file changed on disk found and signed again at
refresh. The app's side (bundle.js, the service worker)
is in tests/test_webui.py; the relay's gate in tests/test_relay.py.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "botcore"))

pytest.importorskip("cryptography")

from botcore.chat import relay_client as rcl  # noqa: E402
from botcore.chat import relay_crypto as rc  # noqa: E402
from botcore.chat import web_bundle  # noqa: E402
from botcore.chat.web_frontend import WebFrontend  # noqa: E402

NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))
OWN_URL = "https://host.example.test/chat"
KEY_A = bytes(range(32))
KEY_B = bytes(range(1, 33))


def _manifest(private: bytes = KEY_A, **over) -> dict:
    m = {"v": 1, "built_at": 1793400000, "key": rc.b64e(rc.signing_keypair(private)[1]),
         "files": {"app.js": "a" * 64, "img/icon.png": "b" * 64, "index.html": "c" * 64}}
    m.update(over)
    return m


# -- the manifest and its signature ---------------------------------------

def test_the_manifest_is_canonical_json():
    """Keys sorted at each level, no spaces, UTF-8: the bytes the app
    writes too (bundle.js)."""
    m = _manifest()
    shuffled = {"files": dict(reversed(list(m["files"].items()))), "v": 1, "key": m["key"],
                "built_at": m["built_at"]}
    want = ('{"built_at":1793400000,"files":{"app.js":"' + "a" * 64 + '","img/icon.png":"'
            + "b" * 64 + '","index.html":"' + "c" * 64 + '"},"key":"' + m["key"] + '","v":1}')
    assert rc.canonical_json(m) == rc.canonical_json(shuffled) == want.encode()
    assert rc.bundle_hash(m) == hashlib.sha256(want.encode()).hexdigest()


def test_the_bundle_hash_is_stable_across_serialisations():
    """The hash is of the canonical bytes, whatever JSON carried the
    manifest; signing it twice gives the same document (Ed25519 is
    deterministic)."""
    m = _manifest()
    h = rc.bundle_hash(m)
    for text in (json.dumps(m), json.dumps(m, indent=3, sort_keys=True),
                 json.dumps(dict(reversed(list(m.items()))), separators=(", ", " : "))):
        assert rc.bundle_hash(json.loads(text)) == h
    doc = rc.sign_manifest(m, KEY_A)
    assert doc == rc.sign_manifest(json.loads(json.dumps(m, indent=1)), KEY_A)
    assert rc.bundle_hash(rc.verify_manifest(json.loads(json.dumps(doc, indent=2)))) == h


def test_a_signed_manifest_checks_and_a_wrong_key_or_change_is_refused():
    m = _manifest()
    doc = rc.sign_manifest(m, KEY_A)
    pub_a, pub_b = rc.signing_keypair(KEY_A)[1], rc.signing_keypair(KEY_B)[1]
    assert set(doc) == {"manifest", "signature"} and len(rc.b64d(doc["signature"])) == 64
    assert rc.verify_manifest(doc) == m
    assert rc.verify_manifest(doc, pub_a, rc.bundle_hash(m)) == m
    with pytest.raises(rc.ManifestError, match="another key"):
        rc.verify_manifest(doc, pub_b)
    with pytest.raises(rc.ManifestError, match="another bundle"):
        rc.verify_manifest(doc, pub_a, "0" * 64)
    # Signed by B but naming A's key; a changed file; a changed signature.
    forged = {"manifest": m, "signature": rc.sign_manifest(_manifest(KEY_B), KEY_B)["signature"]}
    changed = {"manifest": {**m, "files": {**m["files"], "app.js": "f" * 64}},
               "signature": doc["signature"]}
    sig = bytearray(rc.b64d(doc["signature"]))
    sig[0] ^= 1
    for bad in (forged, changed, {"manifest": m, "signature": rc.b64e(bytes(sig))}):
        with pytest.raises(rc.ManifestError, match="signature"):
            rc.verify_manifest(bad)
    # A document or manifest with a field it does not know.
    with pytest.raises(rc.ManifestError):
        rc.verify_manifest({**doc, "note": "x"})
    with pytest.raises(rc.ManifestError):
        rc.verify_manifest({"manifest": {**m, "note": "x"}, "signature": doc["signature"]})
    # The key a manifest names is the key that signs it.
    with pytest.raises(rc.ManifestError, match="another key"):
        rc.sign_manifest(m, KEY_B)


@pytest.mark.parametrize("over", [
    {"v": 2}, {"v": True}, {"built_at": 1.5}, {"built_at": -1}, {"built_at": True},
    {"files": []}, {"files": {"../x.js": "a" * 64}}, {"files": {"/x.js": "a" * 64}},
    {"files": {".hidden": "a" * 64}}, {"files": {"a b.js": "a" * 64}},
    {"files": {"x.js": "A" * 64}}, {"files": {"x.js": "a" * 63}}, {"key": "short"},
    {"key": rc.b64e(bytes(32)) + "="},
])
def test_a_manifest_of_another_shape_is_refused(over):
    with pytest.raises(rc.ManifestError):
        rc.check_manifest(_manifest(**over))


# -- the files a signing covers -------------------------------------------

def test_a_signing_covers_every_file_but_dotfiles_and_links_out(tmp_path):
    root = tmp_path / "webui"
    (root / "img").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html>")
    (root / "img" / "a.png").write_bytes(b"\x89PNG")
    (root / ".swap").write_text("editor")
    (root / "manifest.sig.json").write_text("{}")  # never a file of the app
    (root / "odd name.js").write_text("1")
    (tmp_path / "secret.txt").write_text("outside")
    (root / "out.txt").symlink_to(tmp_path / "secret.txt")
    os.utime(root / "index.html", (1793400000, 1793400000))
    os.utime(root / "img" / "a.png", (1793400100, 1793400100))
    bundle = web_bundle.Bundle(root, KEY_A)
    assert bundle.files == {"index.html": hashlib.sha256(b"<!doctype html>").hexdigest(),
                            "img/a.png": hashlib.sha256(b"\x89PNG").hexdigest()}
    assert sorted(bundle.skipped) == ["odd name.js", "out.txt"]
    assert bundle.manifest["built_at"] == 1793400100  # the newest file
    assert rc.verify_manifest(json.loads(bundle.body), rc.signing_keypair(KEY_A)[1],
                              bundle.hash) == bundle.manifest
    # Nothing changed, nothing to report; the same files sign to the same hash.
    assert bundle.changes() == [] and web_bundle.Bundle(root, KEY_A).hash == bundle.hash
    assert web_bundle.Bundle(tmp_path / "missing", KEY_A).files == {}


# -- the web front end ----------------------------------------------------

def _web(tmp_path, **cfg) -> WebFrontend:
    root = tmp_path / "webui"
    root.mkdir(exist_ok=True)
    (root / "index.html").write_text("<!doctype html><title>t</title>")
    (root / "app.js").write_text("console.log(1)")
    f = WebFrontend({"port": 0, "public_url": OWN_URL, **cfg},
                    store_path=tmp_path / "priv" / "webchat.sqlite", webui_dir=root,
                    trusted_config=True)
    f.poll_s = f.pair_delay_s = 0.02
    return f


def _call(f, method, path, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json"} if body is not None else {}
    if method == "POST":
        h["Origin"] = f"http://127.0.0.1:{f.port}"
    h.update(headers or {})
    req = urllib.request.Request(f.url + path, data=data, method=method, headers=h)
    try:
        with NO_PROXY.open(req, timeout=5) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        with e:
            return e.code, e.headers, e.read()


def _owner(f) -> dict:
    return {"Cookie": f"ea_web={f.pair_token()}"}


def test_the_bot_serves_its_signed_manifest(tmp_path):
    """`<base>/manifest.sig.json` is public data, served without auth and
    never cached; `/api/bundle` is the same document for the owner (the
    route the relay reaches), and `/api/me` names the bundle hash."""
    f = _web(tmp_path)
    f.start()
    try:
        status, h, body = _call(f, "GET", "/manifest.sig.json")
        assert status == 200 and h["Cache-Control"] == "no-store"
        assert h["Content-Type"].startswith("application/json")
        assert h["Cross-Origin-Resource-Policy"] == "same-origin"
        doc = json.loads(body)
        m = rc.verify_manifest(doc, expected_hash=f.bundle_hash())
        assert m["files"] == {n: hashlib.sha256((tmp_path / "webui" / n).read_bytes()).hexdigest()
                              for n in ("index.html", "app.js")}
        assert m["key"] == f.bundle.public
        assert _call(f, "GET", "/api/bundle")[0] == 401
        assert _call(f, "GET", "/api/bundle", headers=_owner(f))[2] == body
        assert json.loads(_call(f, "GET", "/api/me", headers=_owner(f))[2])["bundle"] == f.bundle_hash()
        assert _call(f, "POST", "/manifest.sig.json", {})[0] == 404
        # A file of that name on disk is not the manifest.
        (tmp_path / "webui" / "manifest.sig.json").write_text('{"forged": true}')
        assert _call(f, "GET", "/manifest.sig.json")[2] == body
        # Without a relay the key is this process's own: a new start, a new key.
        first = f.bundle.public
    finally:
        f.stop()
    g = _web(tmp_path)
    g.start()
    try:
        assert g.bundle.public != first and g.bundle_hash()
    finally:
        g.stop()


def test_a_changed_file_on_disk_is_signed_again_at_the_check(tmp_path, caplog):
    """A file changed on disk after the bot signed (a deploy that did not
    restart it) is named by the check, which signs the files again at
    once; the manifest served next names the new files."""
    f = _web(tmp_path)
    f.start()
    try:
        before = f.bundle_hash()
        assert f.check_bundle() == []
        (tmp_path / "webui" / "app.js").write_text("console.log('changed')")
        (tmp_path / "webui" / "new.css").write_text("body{}")
        with caplog.at_level(logging.WARNING, logger="chat.web"):
            assert f.check_bundle() == ["app.js", "new.css"]
        assert f.check_bundle() == []
        after = f.bundle_hash()
        assert after != before
        assert any("signed the app again" in r.getMessage() and "app.js, new.css" in r.getMessage()
                   for r in caplog.records)
        assert not any("no longer match" in r.getMessage() for r in caplog.records)
        m = json.loads(_call(f, "GET", "/manifest.sig.json")[2])["manifest"]
        assert rc.bundle_hash(m) == after
        assert m["files"]["app.js"] == hashlib.sha256(b"console.log('changed')").hexdigest()
        assert "new.css" in m["files"]
    finally:
        f.stop()


def test_serving_a_changed_file_signs_the_files_again_first(tmp_path, caplog):
    """The first request for a file that changed on disk signs the files
    again, so the phone that got it finds a manifest naming it; a served
    file that differs from the disk too is logged once and stays unsigned."""
    f = _web(tmp_path)
    f.start()
    try:
        before = f.bundle_hash()
        (tmp_path / "webui" / "app.js").write_text("console.log('deployed')")
        with caplog.at_level(logging.WARNING, logger="chat.web"):
            status, _, body = _call(f, "GET", "/app.js")
        assert status == 200 and body == b"console.log('deployed')"
        assert f.bundle_hash() != before and f.check_bundle() == []
        m = json.loads(_call(f, "GET", "/manifest.sig.json")[2])["manifest"]
        assert m["files"]["app.js"] == hashlib.sha256(body).hexdigest()
        assert not any("no longer match" in r.getMessage() for r in caplog.records)
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="chat.web"):
            assert f.note_served("app.js", b"something else") is False
            assert f.note_served("app.js", b"something else") is False
        lines = [r.getMessage() for r in caplog.records if "no longer match" in r.getMessage()]
        assert len(lines) == 1 and "served" in lines[0], lines
    finally:
        f.stop()


def test_the_signing_key_lives_in_the_relay_key_file(tmp_path):
    """Made on first use, kept in the key file (mode 0600) beside the
    relay's X25519 key, the same for every reader; a broken pair is refused."""
    path = tmp_path / "relay-key.json"
    store = rcl.KeyStore.create(path, relay_url="https://relay.example.net")
    assert "sign_private" not in json.loads(path.read_text())
    priv, pub = store.signing_keypair()
    data = json.loads(path.read_text())
    assert data["sign_private"] == rc.b64e(priv) and data["sign_public"] == rc.b64e(pub)
    assert (path.stat().st_mode & 0o777) == 0o600
    assert rcl.KeyStore(path).signing_keypair() == (priv, pub) == store.signing_keypair()
    assert data["bot_private"] and data["relay_token"]  # the relay's identity is kept
    data["sign_public"] = rc.b64e(rc.signing_keypair(KEY_B)[1])
    path.write_text(json.dumps(data))
    with pytest.raises(rcl.RelayError, match="sign_public"):
        rcl.KeyStore(path).signing_keypair()


def test_the_release_comes_from_the_code_tree(tmp_path):
    assert web_bundle.release(tmp_path) == ""
    (tmp_path / ".deployed-commit").write_text("0123456789abcdef0123\n")
    assert web_bundle.release(tmp_path) == "commit 0123456789ab"
    (tmp_path / ".version").write_text("v1.2.3\n")
    assert web_bundle.release(tmp_path) == "v1.2.3"
    (tmp_path / ".version").write_text("1.2.3; rm -rf\n")
    assert web_bundle.release(tmp_path) == "commit 0123456789ab"


def test_the_owners_cli_computes_the_bots_bundle_hash(tmp_path):
    """A front end that is not serving (the owner's CLI, for a pairing
    link's hash) signs with the relay key file's key, so its hash is the
    serving bot's for the same files."""
    f = _web(tmp_path, relay={"url": "http://127.0.0.1:9", "wait": 1})
    f.start()
    try:
        served = f.bundle_hash()
        key_file = tmp_path / "priv" / "webchat-relay.json"
        cli = WebFrontend({"relay": {"url": "http://127.0.0.1:9", "key_file": str(key_file)}},
                          store_path=tmp_path / "priv" / "webchat.sqlite",
                          webui_dir=tmp_path / "webui")
        assert cli.bundle_hash() == served != ""
    finally:
        f.stop()
    # With no relay block an idle front end signs nothing.
    assert WebFrontend({}, store_path=tmp_path / "x" / "s.sqlite").bundle_hash() == ""


def test_the_attest_route_records_the_check_and_grants_nothing(tmp_path):
    """A phone posts, through the relay, the bundle hash it checked. The
    bot records `bundle_verified` with the hash and its release only for
    the hash it signs now; another hash clears it. The record changes no
    access: the owner's approval with `cards` does. The route takes a
    relayed device, from the owner only."""
    f = _web(tmp_path, relay={"url": "http://127.0.0.1:9", "wait": 1,
                              "relay_origin_access": "chat"})
    f.start()
    try:
        assert f.relay is not None
        store = f.relay.store
        # The bot signs with the key file's key, which /api/relay/status names.
        assert f.bundle.public == rc.b64e(store.signing_keypair()[1])
        st = json.loads(_call(f, "GET", "/api/relay/status", headers=_owner(f))[2])
        assert st["sign_public"] == f.bundle.public and st["bundle"] == f.bundle_hash()
        pairing = store.add_pairing(relay_url=f.relay.relay_url, origin="relay")
        device = store.register(pairing.pairing_id, bytes(range(32)), "test phone")["id"]
        assert _call(f, "POST", "/api/relay/approve", {"device": device}, _owner(f))[0] == 200
        assert f.relay.level(store.device_by_id(device)) == "chat"
        relayed = {**_owner(f), "X-Tacitus-Relay": device}

        status, _h, body = _call(f, "POST", "/api/relay/attest", {"bundle": f.bundle_hash()}, relayed)
        out = json.loads(body)
        assert status == 200 and out["ok"] is True and out["bundle_verified"] is True
        assert out["release"] == web_bundle.release()
        rec = store.device_by_id(device)
        assert rec["bundle_verified"] is True and rec["attested_bundle"] == f.bundle_hash()
        assert "bundle" not in rec  # the hash a device pairs against is another field
        assert f.relay.level(rec) == "chat"  # a check by code from the relay grants nothing
        row = json.loads(_call(f, "GET", "/api/relay/devices", headers=_owner(f))[2])[0]
        assert (row["id"], row["bundle_verified"], row["cards_allowed"]) == (device, True, False)

        # Only the owner lets the device press card buttons, and takes it back.
        assert _call(f, "POST", "/api/relay/approve", {"device": device, "cards": "yes"},
                     _owner(f))[0] == 400
        assert _call(f, "POST", "/api/relay/approve", {"device": device, "cards": True},
                     relayed)[0] == 403
        assert _call(f, "POST", "/api/relay/approve", {"device": device, "cards": True},
                     _owner(f))[0] == 200
        assert f.relay.level(store.device_by_id(device)) == "all"
        assert _call(f, "POST", "/api/relay/approve", {"device": device}, _owner(f))[0] == 200
        assert f.relay.level(store.device_by_id(device)) == "chat"

        status, _h, body = _call(f, "POST", "/api/relay/attest", {"bundle": "0" * 64}, relayed)
        assert status == 409 and json.loads(body)["bundle_verified"] is False
        rec = store.device_by_id(device)
        assert rec["bundle_verified"] is False and "attested_bundle" not in rec

        for body, headers, want in (({"bundle": "xyz"}, relayed, 400),
                                    ({"bundle": f.bundle_hash()}, _owner(f), 400),
                                    ({"bundle": f.bundle_hash()}, {**relayed, "Cookie": "ea_web=no"}, 401),
                                    ({"bundle": f.bundle_hash()},
                                     {**_owner(f), "X-Tacitus-Relay": "0" * 16}, 404)):
            assert _call(f, "POST", "/api/relay/attest", body, headers)[0] == want, (body, want)
    finally:
        f.stop()


def test_a_browser_s_connection_notes_are_logged_for_the_owner(tmp_path, caplog):
    """What the app saw while it could not connect (POST /diag) goes to the
    log, bounded to twenty entries of two thousand characters, for the
    owner alone; the body must carry a list."""
    f = _web(tmp_path)
    f.start()
    try:
        entries = [{"what": "boot", "why": "no pairing", "idb": "ok", "standalone": True}] + [{"n": i} for i in range(25)]
        with caplog.at_level(logging.INFO, logger="chat.web"):
            status, _, body = _call(f, "POST", "/api/diag", {"entries": entries}, _owner(f))
        assert status == 200 and json.loads(body) == {"ok": True, "logged": 20}
        lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("web chat diag: ")]
        assert len(lines) == 20 and '"what": "boot"' in lines[0] and '"idb": "ok"' in lines[0]
        assert _call(f, "POST", "/api/diag", {"entries": "boot"}, _owner(f))[0] == 400
        assert _call(f, "POST", "/api/diag", {"entries": entries})[0] == 401
    finally:
        f.stop()

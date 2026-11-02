"""scripts/webchat-backup.sh: the nightly snapshot of the web chat's own data.

Pins:
  * a snapshot is <private dir>/backups/webchat/<date>/, mode 0700, every
    file 0600: the store copied through SQLite's backup API (a write still
    open in another connection is not in it, and the copy passes its
    check), the uploads, the state labels, the push key, the owner's web
    ids, the relay key file, the pairing token, chat.json, the room record,
    the card registry and the closure labels, with MANIFEST.tsv naming
    where each came from;
  * the store, the token and the relay key follow chat.json's web block;
  * dated snapshots past seven days go, a labelled one stays;
  * a run prints one line (none with --quiet);
  * --restore prints the steps and changes nothing; --list lists.
Every run is against a scratch private directory.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import sqlite3
import stat
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "webchat-backup.sh"
sys.path.insert(0, str(ROOT / "botcore"))

from botcore.chat.web_frontend import WebStore  # noqa: E402

TODAY = time.strftime("%Y-%m-%d")


def _run(priv: Path, *args: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["EA_PRIVATE_DIR"] = str(priv)
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True,
                          env=env, timeout=120)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(text)
    path.chmod(0o600)
    return path


def _private(tmp_path: Path, chat: dict | None = None) -> Path:
    """A private dir with a store of two messages and every file beside it."""
    priv = tmp_path / "private"
    priv.mkdir(mode=0o700)
    store = WebStore(priv / "webchat.sqlite")
    store.upsert_channel("main", "ea", "main")
    for text in ("first", "second"):
        store.add_message("main", mid=None, thread_key=None, author="bot", kind="message",
                          text=text)
    store.add_file("f1", "main", "notes.txt", b"file bytes", "text/plain")
    store.close()
    _write(priv / "thread-state-labels.jsonl", '{"state": "done"}\n')
    _write(priv / "webchat-owner-ids.json", '{"v": 1, "ids": []}')
    _write(priv / "webchat-vapid.json", '{"private": "made-up"}')
    _write(priv / "webchat-relay.json", '{"v": 1}')
    _write(priv / "webchat-pair.token", "t" * 43 + "\n")
    _write(priv / "chat.json", json.dumps(chat or {"frontend": "zulip", "shadow": "web"}))
    _write(priv / "owner-rooms.json", '{"rooms": {}}')
    _write(priv / "chat-cards.json", "{}")
    _write(priv / "evals" / "closure-labels.jsonl", "{}\n")
    return priv


def _tree(root: Path) -> dict:
    """Every file under `root` with its mode and a hash of its bytes."""
    out = {}
    for p in sorted(root.rglob("*")):
        st = os.lstat(p)
        out[str(p.relative_to(root))] = (stat.S_IMODE(st.st_mode),
                                         hashlib.sha256(p.read_bytes()).hexdigest()
                                         if p.is_file() else "")
    return out


def test_a_snapshot_holds_every_file_owner_only(tmp_path):
    priv = _private(tmp_path)
    r = _run(priv)
    assert r.returncode == 0, r.stderr
    snap = priv / "backups" / "webchat" / TODAY
    assert r.stdout.count("\n") == 1 and f"web chat backup written: {snap} (" in r.stdout
    for d in (priv / "backups", priv / "backups" / "webchat", snap):
        assert stat.S_IMODE(d.stat().st_mode) == 0o700, d
    names = {line.split("\t")[0] for line in (snap / "MANIFEST.tsv").read_text().splitlines()}
    assert names == {"webchat.sqlite", "webchat-files", "thread-state-labels.jsonl",
                     "webchat-owner-ids.json", "webchat-vapid.json", "webchat-relay.json",
                     "webchat-pair.token", "chat.json", "owner-rooms.json", "chat-cards.json",
                     "evals"}
    for p in snap.rglob("*"):
        mode = stat.S_IMODE(p.stat().st_mode)
        assert mode & 0o077 == 0, (p, oct(mode))
        if p.is_file():
            assert mode == 0o600, (p, oct(mode))
    with sqlite3.connect(snap / "webchat.sqlite") as conn:
        assert [r[0] for r in conn.execute("SELECT text FROM messages ORDER BY seq")] == \
            ["first", "second"]
    kept = list((snap / "webchat-files").iterdir())
    assert len(kept) == 1 and kept[0].read_bytes() == b"file bytes"
    assert (snap / "evals" / "closure-labels.jsonl").read_text() == "{}\n"
    # a second run the same day replaces the day's snapshot
    assert _run(priv, "--quiet").stdout == ""
    assert sorted(p.name for p in (priv / "backups" / "webchat").iterdir()) == [TODAY]


def test_the_copy_is_consistent_while_a_write_is_open(tmp_path):
    priv = _private(tmp_path)
    writer = sqlite3.connect(priv / "webchat.sqlite", isolation_level=None)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO messages(channel, id, thread_key, author, kind, text) "
                   "VALUES ('main', 'w99', 't99', 'bot', 'message', 'not yet committed')")
    try:
        r = _run(priv)
    finally:
        writer.execute("ROLLBACK")
        writer.close()
    assert r.returncode == 0, r.stderr
    with sqlite3.connect(priv / "backups" / "webchat" / TODAY / "webchat.sqlite") as conn:
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        texts = [r[0] for r in conn.execute("SELECT text FROM messages")]
    assert "not yet committed" not in texts and texts == ["first", "second"]


def test_the_paths_follow_the_web_block(tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir(mode=0o700)
    priv = _private(tmp_path, {"frontend": "web", "web": {
        "store_path": str(other / "chat.sqlite"),
        "pair_token_file": str(other / "sign-in.token"),
        "relay": {"key_file": str(other / "relay-key.json")}}})
    (priv / "webchat.sqlite").rename(other / "chat.sqlite")
    _write(other / "sign-in.token", "s" * 43)
    _write(other / "relay-key.json", '{"v": 1, "made": "up"}')
    _write(other / "thread-state-labels.jsonl", '{"beside": "the store"}\n')
    r = _run(priv)
    assert r.returncode == 0, r.stderr
    snap = priv / "backups" / "webchat" / TODAY
    manifest = dict(line.split("\t") for line in (snap / "MANIFEST.tsv").read_text().splitlines())
    assert manifest["webchat.sqlite"] == str(other / "chat.sqlite")
    assert manifest["webchat-pair.token"] == str(other / "sign-in.token")
    assert manifest["webchat-relay.json"] == str(other / "relay-key.json")
    assert (snap / "thread-state-labels.jsonl").read_text() == '{"beside": "the store"}\n'


def test_old_dated_snapshots_go_and_labelled_ones_stay(tmp_path):
    priv = _private(tmp_path)
    root = priv / "backups" / "webchat"
    root.mkdir(parents=True, mode=0o700)
    old = time.time() - 10 * 86400
    for name in ("2026-01-01", "2026-01-02-cutover"):
        (root / name).mkdir()
        (root / name / "MANIFEST.tsv").write_text("x\n")
        os.utime(root / name, (old, old))
    (root / "2026-01-05").mkdir()
    r = _run(priv, "--label", "cutover")
    assert r.returncode == 0, r.stderr
    assert sorted(p.name for p in root.iterdir()) == ["2026-01-02-cutover", "2026-01-05",
                                                      f"{TODAY}-cutover"]
    assert _run(priv).returncode == 0
    assert TODAY in {p.name for p in root.iterdir()}
    listed = _run(priv, "--list").stdout
    assert f"{TODAY}\t" in listed and f"{TODAY}-cutover\t" in listed
    assert "2026-01-05" not in listed       # no manifest: not a snapshot
    bad = _run(priv, "--label", "Not A Label")
    assert bad.returncode == 2 and "label" in bad.stderr


def test_restore_prints_the_steps_and_changes_nothing(tmp_path):
    priv = _private(tmp_path)
    assert _run(priv, "--label", "cutover").returncode == 0
    before = _tree(priv)
    r = _run(priv, "--restore", f"{TODAY}-cutover")
    assert r.returncode == 0, r.stderr
    assert _tree(priv) == before
    out = r.stdout
    snap = priv / "backups" / "webchat" / f"{TODAY}-cutover"
    assert "Nothing has changed yet" in out
    # The stop and start lines name the host's service manager.
    stop, start = (("launchctl bootout", "launchctl bootstrap") if platform.system() == "Darwin"
                   else ("systemctl stop ea-chat-bot", "systemctl start ea-chat-bot"))
    assert out.index(stop) < out.index("mv ") < out.index("cp -p") < out.index(start)
    assert f"mv '{priv / 'webchat.sqlite'}'" in out
    assert f"cp -p '{snap / 'webchat.sqlite'}' '{priv / 'webchat.sqlite'}' && chmod 600" in out
    assert f"cp -pR '{snap / 'webchat-files'}' '{priv / 'webchat-files'}'" in out
    assert "ea-doctor.py" in out
    missing = _run(priv, "--restore", "1999-01-01")
    assert missing.returncode == 1 and "no snapshot" in missing.stderr
    assert _run(priv, "--restore", "../x").returncode == 1


def test_no_store_is_an_error(tmp_path):
    priv = tmp_path / "private"
    priv.mkdir(mode=0o700)
    r = _run(priv)
    assert r.returncode == 1 and "no web chat store" in r.stderr
    assert not (priv / "backups" / "webchat" / TODAY).exists()

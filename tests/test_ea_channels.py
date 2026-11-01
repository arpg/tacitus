#!/usr/bin/env python3
"""The project/session channel registry (preprocessor/ea_channels.py).

The registry is what makes a private channel a project's room
(#ea-<project>) or a session's mirror (#tmp-<id8>), and what the web
board and App Home read to show the same badge.

Usage:
    python3 tests/test_ea_channels.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

EA_DIR = Path(tempfile.mkdtemp(prefix="ea-test-channels-"))
os.environ["EA_DIR"] = str(EA_DIR)
(EA_DIR / "config").mkdir(parents=True, exist_ok=True)

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "preprocessor"))

import ea_channels as ch  # noqa: E402

SID = "d488ed1f-f075-4ddd-bbf2-eb382ad3eb87"


def _fresh():
    ch.REGISTRY = EA_DIR / "config" / "ea-channels.json"
    ch.REGISTRY.unlink(missing_ok=True)


def test_slugs_carry_the_markers():
    assert ch.slug("Field trial / coastal sensor network (A1, B2, C3)") == "ea-field-trial-coastal-sensor-network-a1-b2-c3"
    assert ch.slug("ea-already") == "ea-already"
    assert ch.slug("") == "ea-channel"
    assert len(ch.slug("x" * 200)) <= 80
    assert ch.session_slug(SID) == "tmp-d488ed1f"
    assert ch.session_slug(SID.upper()) == "tmp-d488ed1f"
    try:
        ch.session_slug("nope")
    except ValueError:
        pass
    else:
        raise AssertionError("a non-id must not slug")


def test_project_channel_round_trip_and_badges():
    _fresh()
    e = ch.register("C0PROJ0001", "field-trial", "ea-field-trial",
                    url="https://app.slack.com/client/T1/C0PROJ0001", created_by="slack")
    assert e["kind"] == "project" and e["project"] == "field-trial"
    assert ch.for_project("field-trial") == ["C0PROJ0001"]
    assert ch.scope_of("C0PROJ0001")["kind"] == "project"
    assert ch.by_project()["field-trial"] == {
        "id": "C0PROJ0001", "name": "ea-field-trial",
        "url": "https://app.slack.com/client/T1/C0PROJ0001",
        "archived": False, "archived_by": "",
    }
    # re-pointing records what it replaced
    e = ch.register("C0PROJ0001", "jrnl-ae", "ea-jrnl-ae")
    assert e["replaced_project"] == "field-trial"
    assert ch.for_project("field-trial") == []
    # a project room's specialist session is kept on the row
    assert ch.set_session("C0PROJ0001", SID.upper(), model="claude-fable-5-1", effort="xhigh")
    assert ch.scope_of("C0PROJ0001")["session_id"] == SID
    assert ch.scope_of("C0PROJ0001")["model"] == "claude-fable-5-1"
    assert ch.scope_of("C0PROJ0001")["effort"] == "xhigh"
    assert ch.set_archived("C0PROJ0001", True) and ch.scope_of("C0PROJ0001")["archived"] is True
    assert not ch.set_archived("C0NOPE0001", True)
    assert not ch.set_session("C0PROJ0001", "nope") and not ch.set_session("C0NOPE0001", SID)
    # the user renamed the room in Slack: the badge follows
    assert ch.rename("C0PROJ0001", "#ea-jrnl")
    assert ch.scope_of("C0PROJ0001")["name"] == "ea-jrnl"
    assert not ch.rename("C0NOPE0001", "x") and not ch.rename("C0PROJ0001", "")
    assert ch.unregister("C0PROJ0001") and ch.scope_of("C0PROJ0001") is None
    assert not ch.unregister("C0PROJ0001")


def test_session_channel_round_trip():
    _fresh()
    e = ch.register("G0SESS0001", name="tmp-d488ed1f", kind="session",
                    session_id=SID.upper(), model="claude-fable-5-1")
    assert e["kind"] == "session" and e["session_id"] == SID and e["model"] == "claude-fable-5-1"
    assert ch.for_session(SID) == ["G0SESS0001"]
    assert ch.by_project() == {}  # a session room is not a project badge
    # kind is inferred from what is given
    assert ch.register("G0SESS0002", session_id=SID)["kind"] == "session"
    assert ch.register("C0PROJ0002", "jrnl-ae")["kind"] == "project"


def test_registration_refuses_bad_input():
    _fresh()
    for cid, kw in (("bad", {"project": "x"}), ("C0OK000001", {}),
                    ("C0OK000001", {"kind": "session", "session_id": "nope"}),
                    ("C0OK000001", {"kind": "room", "project": "x"})):
        try:
            ch.register(cid, **kw)
        except ValueError:
            continue
        raise AssertionError(f"accepted {cid} {kw}")
    assert ch.load() == {}


def test_room_ids_of_every_frontend_register():
    """A room is keyed by the id its frontend gives: a Slack channel, a
    Zulip stream, a web chat room. Nothing else is a room id, the web
    chat's own channels and reserved ids included."""
    _fresh()
    for cid in ("C0PROJ0001", "G0SESS0001", "stream:42", "r1", "r27", "r1000"):
        assert ch.register(cid, "atlas", "ea-atlas")["project"] == "atlas", cid
        assert ch.scope_of(cid)["kind"] == "project", cid
    assert ch.register("r3", name="tmp-d488ed1f", session_id=SID)["kind"] == "session"
    assert ch.for_session(SID) == ["r3"]
    for cid in ("main", "dm", "needs", "r", "r0", "r01", "R1", "r1x", "rr1", "r1 x",
                "stream:", "stream:x", "dm:7", "D0DIRECT01", "c0lower001", "C0SHRT",
                "t1", "w1", "r-1"):
        try:
            ch.register(cid, "atlas", "ea-atlas")
        except ValueError:
            continue
        raise AssertionError(f"accepted {cid!r}")
    assert set(ch.load()) == {"C0PROJ0001", "G0SESS0001", "stream:42", "r1", "r27", "r1000",
                              "r3"}


def test_writes_are_in_place_and_keep_doc_keys():
    """The config dir is not group-writable for the bot's user, so the
    file is rewritten in place under a lock; `_doc`/`_example` keys
    survive and never read as channels."""
    _fresh()
    ch.REGISTRY.write_text(json.dumps({"_doc": "keep me", "_example": {"x": 1}}))
    ch.register("C0PROJ0009", "garden-shed", "ea-garden-shed")
    raw = json.loads(ch.REGISTRY.read_text())
    assert raw["_doc"] == "keep me" and "C0PROJ0009" in raw
    assert list(ch.load()) == ["C0PROJ0009"]
    assert oct(ch.REGISTRY.stat().st_mode & 0o777) == "0o664"
    # twelve concurrent registrations, one file, all present
    script = (
        "import os, sys\n"
        f"os.environ['EA_DIR'] = {str(EA_DIR)!r}\n"
        f"sys.path.insert(0, {str(REPO / 'preprocessor')!r})\n"
        "import ea_channels as ch\n"
        "n = int(sys.argv[1])\n"
        "ch.register(f'C0CONC{n:04d}', f'proj-{n}', f'ea-proj-{n}')\n")
    procs = [subprocess.Popen([sys.executable, "-c", script, str(i)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
             for i in range(12)]
    for p in procs:
        _, err = p.communicate(timeout=60)
        assert p.returncode == 0, err.decode()[-300:]
    data = ch.load()
    assert all(f"C0CONC{i:04d}" in data for i in range(12)), sorted(data)
    assert "C0PROJ0009" in data


def test_cli_round_trip():
    _fresh()
    env = dict(EA_DIR=str(EA_DIR), PATH="/usr/bin:/bin")
    cli = str(REPO / "preprocessor" / "ea_channels.py")
    r = subprocess.run([sys.executable, cli, "register", "--channel", "C0CLI00001",
                        "--project", "jrnl-ae", "--name", "ea-jrnl-ae"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0 and json.loads(r.stdout)["ok"], r.stdout + r.stderr
    r = subprocess.run([sys.executable, cli, "register", "--channel", "G0CLI00002",
                        "--session", SID], capture_output=True, text=True, env=env)
    assert json.loads(r.stdout)["kind"] == "session"
    r = subprocess.run([sys.executable, cli, "list"], capture_output=True, text=True, env=env)
    assert {x["channel"] for x in json.loads(r.stdout)} == {"C0CLI00001", "G0CLI00002"}
    r = subprocess.run([sys.executable, cli, "slug", "Garden shed"], capture_output=True, text=True, env=env)
    assert r.stdout.strip() == "ea-garden-shed"
    r = subprocess.run([sys.executable, cli, "register", "--channel", "nope", "--project", "x"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 1


def test_set_effort_and_resolve():
    _fresh()
    ch.register("C0PROJ0007", "lab-visits", "ea-lab-visits")
    ch.register("C0PROJ0008", "budget", "ea-budget")
    assert ch.resolve("C0PROJ0007") == "C0PROJ0007"
    assert ch.resolve("#ea-lab-visits") == "C0PROJ0007"
    assert ch.resolve("budget") == "C0PROJ0008"
    assert ch.resolve("nothing") == ""
    assert ch.set_effort("C0PROJ0007", "xhigh") is True
    assert ch.load()["C0PROJ0007"]["effort"] == "xhigh"
    assert ch.set_effort("C0NOTREG01", "high") is False


def test_set_effort_cli_moves_every_project_room():
    _fresh()
    ch.register("C0PROJ0009", "alpha", "ea-alpha")
    ch.register("C0PROJ0010", "beta", "ea-beta")
    cli = str(REPO / "preprocessor" / "ea_channels.py")
    env = dict(os.environ, EA_DIR=str(EA_DIR))
    r = subprocess.run([sys.executable, cli, "set-effort", "--all-projects", "--effort", "high"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["ok"] and {x["channel"] for x in out["rooms"]} == {"C0PROJ0009", "C0PROJ0010"}
    assert all(v.get("effort") == "high" for v in ch.load().values())
    r = subprocess.run([sys.executable, cli, "set-effort", "--channel", "ea-zzz", "--effort", "high"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 1


def test_template_documents_both_kinds():
    raw = json.loads((REPO / "config" / "ea-channels.json").read_text())
    kinds = {v["kind"] for v in raw["_example"].values()}
    assert kinds == {"project", "session"}
    assert "#ea-" in raw["_doc"] and "#tmp-" in raw["_doc"]
    # the template itself registers nothing
    assert ch.load(REPO / "config" / "ea-channels.json") == {}


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  ✅ {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  ❌ {t.__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\n  {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)

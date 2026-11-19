"""Which project room a ledger row goes to.

A row a project room's turn adds is linked to the room's project as it
is written (the bot sets EA_ROOM_PROJECT for the turn); generic words in
a project's name score nothing in the word matcher; a tie that name
words decide places nothing; the owner's "this belongs elsewhere" moves
a row for good; the one-time repair finds the rows rooms created before.

The fault this guards: a row created in one project's room had no link,
and a second project whose name is only generic words ("... project and
proposal") tied with it on those words, so the morning briefing posted
the row in both rooms. Every name, id and text here is made up."""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "preprocessor"))
import briefing_rooms as br  # noqa: E402
import project_store as ps  # noqa: E402
import remote_briefing as rb  # noqa: E402

PS_CLI = str(ROOT / "preprocessor" / "project_store.py")
LEDGER_CLI = str(ROOT / "preprocessor" / "ledger.py")
NO_VOTES = {"messages": {}, "conversations": {}}

# The room's project: its rows name it by an alias only.
TERN = ("tern-geo", "Tern voyages: multi-sensor mapping", "Tern,Tern mapping")
# A project whose name is generic words and a short acronym.
ORCA = ("orca-bid", "ORC project and proposal", "ORC")
ROW_A = ("tern-edits-1120", "deadline",
         "Tern: collaborators finish proposal edits by Friday",
         "Waiting on others until Friday. Source: project chat t100.")
ROW_B = ("tern-handoff-1123", "task",
         "Tern: send the integrated proposal to the outside reviewer by Monday",
         "Set after the edits finish. Source: project chat t100.")


@pytest.fixture(autouse=True)
def private(tmp_path, monkeypatch):
    """No test reads the owner's mail tagger log."""
    d = tmp_path / "private"
    d.mkdir(mode=0o700)
    monkeypatch.setenv("EA_PRIVATE_DIR", str(d))
    return d


def cli(ea_dir: Path, script: str, *argv: str, room: str = "") -> tuple[int, dict]:
    env = {k: v for k, v in os.environ.items() if k != "EA_ROOM_PROJECT"}
    env["EA_DIR"] = str(ea_dir)
    if room:
        env["EA_ROOM_PROJECT"] = room
    p = subprocess.run([sys.executable, script, *argv], capture_output=True, text=True, env=env)
    try:
        return p.returncode, json.loads(p.stdout)
    except json.JSONDecodeError:
        return p.returncode, {"raw": p.stdout, "stderr": p.stderr}


def add_project(ea_dir: Path, pid: str, name: str, aliases: str = "") -> None:
    rc, out = cli(ea_dir, PS_CLI, "add-project", "--id", pid, "--name", name,
                  "--domain", "research", "--aliases", aliases)
    assert rc == 0, out


def add_row(ea_dir: Path, row, *, room: str = "", project: str = "") -> dict:
    lid, typ, desc, notes = row
    argv = ["add", "--id", lid, "--type", typ, "--description", desc, "--notes", notes]
    if project:
        argv += ["--project", project]
    rc, out = cli(ea_dir, LEDGER_CLI, *argv, room=room)
    assert rc == 0, out
    return out


def conn_of(ea_dir: Path) -> sqlite3.Connection:
    c = sqlite3.connect(ea_dir / "state" / "obligations-ledger.db")
    c.row_factory = sqlite3.Row
    return c


@pytest.fixture
def store(tmp_path):
    ea = tmp_path / "ea"
    (ea / "state").mkdir(parents=True)
    add_project(ea, *TERN)
    add_project(ea, *ORCA)
    return ea


# ── the word matcher ──────────────────────────────────────────────────

def test_a_name_of_generic_words_scores_nothing_and_ties_with_no_one(store):
    """The two rows name their project by its alias. The other project's
    name is "project" and "proposal" past its acronym, and the rows carry
    both words: they gave it the full-name score and a tie."""
    c = conn_of(store)
    for row in (ROW_A, ROW_B):
        text = f"{row[2]} {row[3]}"
        hits = ps.match_projects(text, c)
        assert [h["id"] for h in hits] == ["tern-geo"], hits
        assert hits[0]["by"] == ["alias"]
    # the acronym still names it
    assert ps.match_projects("ORC: budget for the proposal", c)[0]["id"] == "orca-bid"


def test_a_full_name_needs_every_word_and_one_distinctive(store):
    add_project(store, "rl-1", "Reef lab move")
    add_project(store, "rs-1", "Reef study")
    c = conn_of(store)

    def score(text, pid):
        return {h["id"]: h["score"] for h in ps.match_projects(text, c)}.get(pid, 0)

    assert score("the reef lab move is on Friday", "rl-1") == 3
    # "study" is generic: the full name "reef study" still needs "reef"
    assert score("a study of anything", "rs-1") == 0
    assert score("plans for the reef study", "rs-1") == 3
    # one distinctive word of a longer name is not the name
    assert score("tern sightings", "tern-geo") == 3  # the alias
    assert score("multi-sensor rigs", "tern-geo") == 2  # two distinctive name words
    assert score("voyages", "tern-geo") == 0


def test_a_word_four_project_names_share_names_none_of_them(store):
    for i, rest in enumerate(("survey", "drill", "barge", "dive")):
        add_project(store, f"d-{i}", f"Delta {rest}")
    c = conn_of(store)
    assert "delta" in ps.generic_name_words(r["name"] for r in c.execute("SELECT name FROM projects"))
    hits = {h["id"]: h["score"] for h in ps.match_projects("delta survey", c)}
    assert hits == {"d-0": 3}
    assert ps.match_projects("delta delta delta", c) == []


def test_the_id_is_a_phrase_never_part_of_another_word(store):
    add_project(store, "ledges", "Ledges climbing")
    add_project(store, "ms-geo", "Masters in geography")
    c = conn_of(store)
    ids = lambda t: [h["id"] for h in ps.match_projects(t, c)]  # noqa: E731
    assert "ledges" not in ids("pledges for the fund drive")
    assert "ledges" in ids("photos from the ledges")
    assert "ms-geo" in ids("MS GEO admissions update")
    assert "ms-geo" in ids("the ms-geox course list")  # a hyphenated id inside a token


# ── the briefing's rooms ──────────────────────────────────────────────

def _entries(rows):
    return [{"ref": f"L{i}", "section": "top", "line": f"*{r[2]}*", "ledger_id": r[0],
             "conversation_id": "", "title": r[2], "summary": r[3]} for i, r in enumerate(rows, 1)]


def test_the_rows_post_in_their_room_only(store):
    """The morning post: both rows unlinked (made outside a room turn),
    each room bound to its project. Only the Tern room gets them, as
    items and in "Also open here"."""
    for row in (ROW_A, ROW_B):
        add_row(store, row)
    c = conn_of(store)
    targets = {"tern-geo": {"channel": "r13", "room": "ea-tern-geo"},
               "orca-bid": {"channel": "r12", "room": "ea-orca-bid"}}
    open_rows = [{"id": r[0], "description": r[2], "notes": r[3]} for r in (ROW_A, ROW_B)]
    posts = br.plan_posts(_entries([ROW_A]), heading="Morning briefing", open_rows=open_rows,
                          conn=c, targets=targets, clean=rb._clean, votes=NO_VOTES)
    assert [p["project"] for p in posts] == ["tern-geo"]
    assert "Also open here (1)" in posts[0]["text"]
    others = br.also_open("orca-bid", open_rows, set(), c, idx=br.index(c, NO_VOTES))
    assert others == []


def test_a_tie_on_name_words_places_nothing(store):
    c = conn_of(store)
    idx = br.index(c, NO_VOTES)
    tie = lambda text, conn: [{"id": "tern-geo", "score": 3, "by": ["alias"]},  # noqa: E731
                              {"id": "orca-bid", "score": 3, "by": ["name"]}]
    assert br.projects_for({"title": "x"}, idx, c, matcher=tie) == []
    got = br.placement({"title": "x"}, idx, c, matcher=tie)
    assert got["by"] == "none" and got["tie"] == ["tern-geo", "orca-bid"]
    both = lambda text, conn: [{"id": "tern-geo", "score": 3, "by": ["alias"]},  # noqa: E731
                               {"id": "orca-bid", "score": 3, "by": ["id"]}]
    assert br.projects_for({"title": "x"}, idx, c, matcher=both) == ["tern-geo", "orca-bid"]


# ── a room's turn links what it adds ──────────────────────────────────

def test_a_row_added_in_a_room_goes_under_the_rooms_project(store):
    out = add_row(store, ROW_A, room="tern-geo")
    assert out["project"] == {"linked": "tern-geo", "by": "room"}
    c = conn_of(store)
    assert br.placement({"ledger_id": ROW_A[0], "title": "ORC orca-bid"}, br.index(c, NO_VOTES),
                        c)["projects"] == ["tern-geo"]
    # outside a room, nothing is linked
    out = add_row(store, ("plain-1", "task", "Book the van", ""))
    assert "project" not in out
    assert conn_of(store).execute("SELECT * FROM project_ledger WHERE ledger_id='plain-1'"
                                  ).fetchone() is None


def test_another_projects_row_names_it_and_a_bad_name_writes_nothing(store):
    out = add_row(store, ("orca-budget-1", "task", "Budget table", ""), room="tern-geo",
                  project="orca-bid")
    assert out["project"] == {"linked": "orca-bid", "by": "--project"}
    out = add_row(store, ("van-1", "task", "Book the van", ""), room="tern-geo", project="none")
    assert out["project"]["linked"] == "none"
    c = conn_of(store)
    assert br.placement({"ledger_id": "van-1", "title": "Tern van"}, br.index(c, NO_VOTES),
                        c)["by"] == "loose"
    rc, out = cli(store, LEDGER_CLI, "add", "--id", "x-1", "--type", "task", "--description", "x",
                  "--project", "no-such-project")
    assert rc == 1 and "no-such-project" in out["error"]
    assert conn_of(store).execute("SELECT 1 FROM ledger WHERE id='x-1'").fetchone() is None


def test_a_row_changed_in_a_room_is_linked_only_when_nothing_else_claims_it(store):
    add_row(store, ("van-2", "task", "Book the van", ""))
    add_row(store, ("orca-2", "task", "ORC budget table", ""))
    add_row(store, ("loose-2", "task", "Lunch order", ""))
    cli(store, PS_CLI, "loose", "--mark", "loose-2")
    rc, out = cli(store, LEDGER_CLI, "update", "--id", "van-2", "--notes", "Friday", room="tern-geo")
    assert out["project"] == {"linked": "tern-geo", "by": "room"}
    # its words name another project: the room is no proof (a row the
    # briefing posted in the wrong room, acted on there)
    rc, out = cli(store, LEDGER_CLI, "update", "--id", "orca-2", "--notes", "x", room="tern-geo")
    assert "linked" not in out["project"] and "orca-bid" in out["project"]["skipped"]
    rc, out = cli(store, LEDGER_CLI, "update", "--id", "loose-2", "--notes", "x", room="tern-geo")
    assert "linked" not in out["project"]
    # a linked row stays where it is
    cli(store, PS_CLI, "link-ledger", "--project", "orca-bid", "--ledger-id", "van-2")
    rc, out = cli(store, LEDGER_CLI, "update", "--id", "van-2", "--notes", "y", room="orca-bid")
    links = [r[0] for r in conn_of(store).execute(
        "SELECT project_id FROM project_ledger WHERE ledger_id='van-2' ORDER BY project_id")]
    assert links == ["orca-bid", "tern-geo"]


# ── the owner moves a row ─────────────────────────────────────────────

def test_why_is_this_here_and_the_move_that_sticks(store):
    add_row(store, ROW_A)
    cli(store, PS_CLI, "link-ledger", "--project", "orca-bid", "--ledger-id", ROW_A[0])
    rc, why = cli(store, PS_CLI, "placement", "--ledger-id", ROW_A[0])
    assert rc == 0 and why["projects"] == ["orca-bid"] and why["by"] == "link"
    assert "linked to orca-bid" in why["why"] and "move-ledger" in why["fix"]
    rc, out = cli(store, PS_CLI, "move-ledger", "--ledger-id", ROW_A[0], "--project", "tern-geo",
                  "--quote", "this belongs in the Tern room")
    assert rc == 0 and out["from"] == ["orca-bid"] and out["to"] == "tern-geo"
    c = conn_of(store)
    assert [r[0] for r in c.execute("SELECT project_id FROM project_ledger WHERE ledger_id=?",
                                    (ROW_A[0],))] == ["tern-geo"]
    ev = c.execute("SELECT kind, evidence, note FROM ledger_events WHERE entry_id=?",
                   (ROW_A[0],)).fetchone()
    assert ev["kind"] == "placed" and json.loads(ev["evidence"])["to"] == "tern-geo"
    assert "this belongs in the Tern room" in c.execute(
        "SELECT text FROM project_events WHERE project_id='tern-geo'").fetchone()[0]
    # none: no room gets it, whatever its words say
    rc, out = cli(store, PS_CLI, "move-ledger", "--ledger-id", ROW_A[0], "--project", "none")
    rc, why = cli(store, PS_CLI, "placement", "--ledger-id", ROW_A[0])
    assert why["projects"] == [] and why["by"] == "loose"
    rc, out = cli(store, PS_CLI, "move-ledger", "--ledger-id", ROW_A[0], "--project", "nope")
    assert rc == 1


def test_a_link_ends_a_loose_mark(store):
    add_row(store, ("van-3", "task", "Book the van", ""))
    cli(store, PS_CLI, "loose", "--mark", "van-3")
    cli(store, PS_CLI, "link-ledger", "--project", "tern-geo", "--ledger-id", "van-3")
    c = conn_of(store)
    assert c.execute("SELECT 1 FROM loose_ledger WHERE ledger_id='van-3'").fetchone() is None


# ── the one-time repair ───────────────────────────────────────────────

def _repair():
    spec = importlib.util.spec_from_file_location("repair_room_links",
                                                  ROOT / "scripts" / "repair_room_links.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_the_repair_finds_rows_made_in_rooms_and_writes_nothing_by_itself(store, tmp_path):
    rp = _repair()
    for row in (ROW_A, ("orca-memo-1", "task", "Memo on the plan", ""),
                ("priority-Zx0000000Q=", "response_needed", "Reply to the panel", ""),
                ("two-rooms-1", "task", "Pick a date", "")):
        add_row(store, row)
    c = conn_of(store)
    made = {r["id"]: datetime.fromisoformat(r["created_at"].replace("Z", "+00:00"))
            for r in c.execute("SELECT id, created_at FROM ledger")}
    rooms = {"r13": "tern-geo", "r12": "orca-bid"}
    threads = {"t100": "r13", "t200": "r12", "t300": "main"}
    m = timedelta(minutes=1)
    windows = [(made["orca-memo-1"] - m, made["orca-memo-1"] + m, "t200"),
               (made["two-rooms-1"] - m, made["two-rooms-1"] + m, "t300")]
    got = {r["id"]: r for r in rp.candidates(c, rooms, threads, windows)}
    assert got[ROW_A[0]]["action"] == "link" and got[ROW_A[0]]["project"] == "tern-geo"
    assert "t100" in got[ROW_A[0]]["evidence"]
    # the intake's rows are created at any time: a turn's window is no evidence
    assert "priority-Zx0000000Q=" not in got
    # turns in two channels at once: a report line, no link
    assert got["two-rooms-1"]["action"] == "skip"
    assert c.execute("SELECT COUNT(*) FROM project_ledger").fetchone()[0] == 0


def test_the_repair_reads_turn_windows_from_the_bot_log(tmp_path):
    rp = _repair()
    log = tmp_path / "bot.log"
    log.write_text("\n".join([
        "2026-11-18 07:59:15,311 INFO Session: NEW 0a1b2c3d… for thread t100",
        "2026-11-18 07:59:20,000 INFO Session: RESUME 1234abcd… for thread t200",
        "2026-11-18 07:59:54,928 INFO Posted chunk 1/1 to Slack thread t100",
        "2026-11-18 08:00:01,000 INFO something else",
    ]) + "\n")
    w = {k: (a, b) for a, b, k in rp.turn_windows(log)}
    assert (w["t100"][1] - w["t100"][0]).total_seconds() == pytest.approx(39, abs=1)
    assert w["t200"][1] - w["t200"][0] == rp.TURN_MAX  # no end line
    assert w["t100"][0].tzinfo is not None
    assert rp.turn_windows(tmp_path / "missing.log") == []


# ── the bot passes the room to every run of the turn ──────────────────

BOT_SRC = (ROOT / "ea-chat-bot.py").read_text()


def test_the_bot_sets_the_room_project_for_every_run_of_a_room_turn():
    import threading
    import time
    import uuid
    ns = {"threading": threading, "time": time, "uuid": uuid,
          "TIMEOUT_SECONDS": 600, "ROOM_TIMEOUT_SECONDS": 1200}
    start = BOT_SRC.index("# Dispatch run identity")
    end = BOT_SRC.index("# Watchdog: track last message received", start)
    exec(compile(BOT_SRC[start:end], "ea-chat-bot.py[block]", "exec"), ns)
    ns["_register_dispatch"]("run-1", "r13", "t100", room_project="tern-geo")
    ns["_register_dispatch"]("run-2", "main", "t101")
    assert ns["_room_env"]("run-1") == {"EA_ROOM_PROJECT": "tern-geo"}
    assert ns["_room_env"]("run-2") == {} and ns["_room_env"]("nope") == {}
    assert ns["ROOM_PROJECT_ENV"] == ps.ROOM_PROJECT_ENV
    cmd = BOT_SRC[BOT_SRC.index("def _sudo_claude_cmd("):BOT_SRC.index("\n@dataclass")]
    assert 'env_vars += [f"{k}={v}" for k, v in _room_env(run_id).items()]' in cmd
    assert "env=_room_env(run_id))" in BOT_SRC  # the Codex arm
    assert "session_key=session_key, room_project=room_project)" in BOT_SRC
    assert "`--project <id>`" in BOT_SRC and "move-ledger" in BOT_SRC  # the room preamble

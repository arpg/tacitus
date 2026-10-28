#!/usr/bin/env python3
"""Tests for the project store (projects/people relational memory).

Usage:
    python3 tests/test_project_store.py
"""

import json
import os
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "preprocessor"))

CLI = str(PROJECT_ROOT / "preprocessor" / "project_store.py")


def run(ea_dir, *argv):
    env = dict(os.environ, EA_DIR=str(ea_dir))
    p = subprocess.run([sys.executable, CLI, *argv],
                       capture_output=True, text=True, env=env)
    try:
        return p.returncode, json.loads(p.stdout)
    except json.JSONDecodeError:
        return p.returncode, p.stdout


def _seed(d):
    run(d, "add-project", "--id", "orion", "--name", "Northwind ORION",
        "--domain", "research", "--aliases", "ORION,EX-UNI-NW")
    run(d, "add-project", "--id", "ai-dir", "--name", "AI Faculty Director",
        "--domain", "service")
    run(d, "add-project", "--id", "bs-ai", "--name", "BS in AI",
        "--domain", "service", "--parent", "ai-dir")
    run(d, "add-person", "--email", "kai.brightwater15@example.edu", "--name", "Hale Brightwater",
        "--org", "MIT")
    run(d, "link-person", "--project", "orion", "--email", "kai.brightwater15@example.edu",
        "--role", "Collaborator")


def test_add_and_get_roundtrip(d):
    _seed(d)
    rc, out = run(d, "get", "--project", "orion")
    assert rc == 0 and out["name"] == "Northwind ORION"
    assert out["people"][0]["email"] == "kai.brightwater15@example.edu"  # lowercased
    rc, out = run(d, "get", "--project", "ai-dir")
    assert out["children"] == ["bs-ai"]


def test_domain_validation_and_slug(d):
    rc, out = run(d, "add-project", "--id", "x-p", "--name", "X",
                  "--domain", "hobby")
    assert rc == 1 and "domain" in out["error"]
    rc, out = run(d, "add-project", "--id", "Bad Slug", "--name", "X",
                  "--domain", "research")
    assert rc == 1 and "slug" in out["error"]


def test_parent_must_exist(d):
    rc, out = run(d, "add-project", "--id", "orphan", "--name", "O",
                  "--domain", "research", "--parent", "nope")
    assert rc == 1 and "parent" in out["error"]


def test_journal_and_pack(d):
    _seed(d)
    run(d, "journal", "--project", "orion", "--kind", "decision",
        "--text", "moved biweekly sync to Wednesdays")
    run(d, "file-message", "--project", "orion", "--conversation-id", "c1",
        "--subject", "ORION sync notes", "--from", "kai.brightwater15@example.edu",
        "--gist", "agreed on eval plan")
    rc, out = run(d, "pack", "--project", "orion")
    brief = out["brief"]
    assert "Northwind ORION" in brief
    assert "moved biweekly sync" in brief
    assert "agreed on eval plan" in brief
    assert "Hale Brightwater" in brief


def test_match_scores_aliases_and_people(d):
    _seed(d)
    import importlib
    import project_store
    importlib.reload(project_store)
    os.environ["EA_DIR"] = str(d)
    # module-level DB_PATH was bound at import; reload with env set
    project_store.DB_PATH = Path(d) / "state" / "obligations-ledger.db"
    hits = project_store.match_projects("quick question about the ORION review")
    assert hits and hits[0]["id"] == "orion" and hits[0]["score"] >= 3
    hits = project_store.match_projects("did kai.brightwater15@example.edu reply about the eval?")
    assert any(h["id"] == "orion" for h in hits)
    assert project_store.match_projects("nothing relevant here") == []


def test_one_lowercase_word_alias_needs_a_second_signal(d):
    """A common-word alias alone stays under the floor of 3 that places
    mail in a room or links a row; a name-like alias or a phrase does
    not, and the common word still adds to another signal."""
    import importlib
    import project_store
    run(d, "add-project", "--id", "dock-repair", "--name", "Lake dock repair",
        "--domain", "personal", "--aliases", "foundation,pilings,Marlowe,dock rebuild")
    importlib.reload(project_store)
    project_store.DB_PATH = Path(d) / "state" / "obligations-ledger.db"

    def score(text):
        return {h["id"]: h["score"] for h in project_store.match_projects(text)}.get("dock-repair", 0)

    assert score("Request for evaluation from the Science Foundation") == 2
    assert score("Notice of hearing on the settlement of the pilings case") == 2
    assert score("Marlowe sent the drawings") == 3
    assert score("quote for the dock rebuild") == 3
    assert score("foundation and pilings quote") == 4
    assert project_store.alias_weight("ORION") == 3
    assert project_store.alias_weight("4417") == 3
    assert project_store.alias_weight("foundation") == 2


def test_list_projects_carries_parent_summary_and_people(d):
    """The mail tagger's vocabulary: every project with its parent,
    summary, aliases and the addresses of its people."""
    _seed(d)
    import importlib
    import project_store
    importlib.reload(project_store)
    project_store.DB_PATH = Path(d) / "state" / "obligations-ledger.db"
    rows = {r["id"]: r for r in project_store.list_projects()}
    assert rows["orion"]["people"] == ["kai.brightwater15@example.edu"]
    assert rows["orion"]["aliases"] == "ORION,EX-UNI-NW"
    assert rows["bs-ai"]["parent_id"] == "ai-dir" and rows["bs-ai"]["people"] == []


def test_index_brief_lists_every_active_project_by_domain(d):
    """Cross-project awareness: one screen, one line per project, grouped
    by domain, with parent, open count and next action; cut on a line
    boundary."""
    _seed(d)
    run(d, "update-project", "--id", "orion", "--next-actions", "send the SOW")
    rc, out = run(d, "index")
    assert rc == 0 and isinstance(out, str), out
    lines = out.splitlines()
    assert lines[0].startswith("PROJECT INDEX (3 active")
    assert "[research]" in lines and "[service]" in lines
    assert any(l.startswith("- orion | Northwind ORION | 0 open | next: send the SOW") for l in lines), lines
    assert any("- bs-ai | BS in AI (under ai-dir) | 0 open" in l for l in lines), lines
    rc, short = run(d, "index", "--max-chars", "80")
    assert short.splitlines()[-1].startswith("- ... (cut"), short


def test_horizon_and_dormancy_scan(d):
    """PARA: a project has an end and can go dormant; an area or goal is
    standing context and never does. The scan ignores bookkeeping
    journal notes (rooms opened, dormancy set) as activity."""
    _seed(d)
    rc, out = run(d, "update-project", "--id", "ai-dir", "--horizon", "area")
    assert rc == 0 and out["ok"], out
    rc, out = run(d, "add-project", "--id", "ladder", "--name", "Advisory ladder",
                  "--domain", "professional", "--horizon", "goal")
    assert rc == 0 and out["ok"], out
    rc, out = run(d, "add-project", "--id", "bad", "--name", "x", "--domain", "research",
                  "--horizon", "epoch")
    assert rc != 0
    rc, listing = run(d, "list")
    hz = {p["id"]: p.get("horizon") for p in (listing if isinstance(listing, list) else listing["projects"])}
    assert hz["ai-dir"] == "area" and hz["ladder"] == "goal" and hz["orion"] == "project"
    rc, idx = run(d, "index")
    assert "- ai-dir | AI Faculty Director [area]" in idx and "[goal]" in idx
    # every seeded project was created just now: nothing is quiet yet
    rc, out = run(d, "dormancy-scan", "--days", "30")
    assert out["candidates"] == []
    # age orion (project) and ai-dir (area) by rewriting their creation and
    # adding only bookkeeping notes; only the project is a candidate
    import sqlite3
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    iso = lambda days: (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = sqlite3.connect(str(d / "state" / "obligations-ledger.db"))
    conn.execute("UPDATE projects SET created_at=?, updated_at=?", (iso(270), iso(29)))
    conn.execute("INSERT INTO project_events (project_id,ts,kind,text) VALUES "
                 "('orion',?,'note','Slack channel #ea-orion opened for this project')", (iso(29),))
    conn.execute("INSERT INTO project_events (project_id,ts,kind,text) VALUES "
                 "('bs-ai',?,'decision','go with the two-track plan')", (iso(10),))
    conn.commit(); conn.close()
    rc, out = run(d, "dormancy-scan", "--days", "30")
    ids = [c["id"] for c in out["candidates"]]
    assert "orion" in ids and "ai-dir" not in ids and "ladder" not in ids and "bs-ai" not in ids, ids
    rc, out = run(d, "dormancy-scan", "--days", "30", "--apply")
    assert out["applied"] and "orion" in [c["id"] for c in out["candidates"]]
    rc, listing = run(d, "list", "--status", "dormant")
    rows = listing if isinstance(listing, list) else listing["projects"]
    assert [p["id"] for p in rows] == ["orion"]
    rc, out = run(d, "dormancy-scan", "--days", "30")
    assert out["candidates"] == []  # dormant rows are not rescanned


def test_ledger_join_absent_table_is_tolerated(d):
    _seed(d)
    rc, out = run(d, "pack", "--project", "orion")
    assert rc == 0 and out["open_items"] == []


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for t in tests:
        with tempfile.TemporaryDirectory() as d:
            try:
                t(Path(d))
                print(f"  ✅ {t.__name__}")
                passed += 1
            except Exception as e:
                print(f"  ❌ {t.__name__}: {e}")
                traceback.print_exc()
                failed += 1
    print(f"\n  {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()

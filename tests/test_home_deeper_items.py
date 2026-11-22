"""The deeper briefing (preprocessor/home_deeper.py) together with the Home
item it fills (preprocessor/home_items.py), as they run on main: the item's
block read from the stored briefing, the refresh the app asks for, a step
that runs as the item's own choice with its Undo, the version both read,
and a kept item. Made-up data only (tests/home_fixtures.py)."""
from __future__ import annotations

import json
import sys
from datetime import timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "preprocessor"))
sys.path.insert(0, str(REPO / "tests"))

import home_deeper as D  # noqa: E402
import home_items as I  # noqa: E402
from test_home_items import TODAY, Ledger, home  # noqa: E402

WAIT = (TODAY + timedelta(days=4)).isoformat()


def answer() -> str:
    return json.dumps({
        "history": ["The records show the work is still open."],
        "stakes": ["The work is due as the row says."],
        "unknown": ["The records do not show who else is involved."],
        "steps": [{"kind": "mark_done", "say": "", "question": "", "date": "", "minutes": 0, "ask": "", "why": ""},
                  {"kind": "wait_until", "say": "", "question": "", "date": WAIT, "minutes": 0, "ask": "",
                   "why": ""}]})


@pytest.fixture
def real(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(D, "LOCAL", lambda p: calls.append(p) or {"ok": True, "text": answer(), "reason": "",
                                                                   "usage": {"in": 900, "out": 200}})
    monkeypatch.setattr(D, "HOSTED", lambda p: (_ for _ in ()).throw(AssertionError("no hosted call")))
    monkeypatch.setattr(D, "_store_path", lambda: tmp_path / "remote-llm" / D.STORE_FILE)
    monkeypatch.setattr(D, "_log", lambda rec: None)
    monkeypatch.setattr(D, "_sources", lambda: ({}, ""))
    monkeypatch.setattr(D, "_briefing_now", lambda: {})
    monkeypatch.setattr(D, "_project", lambda lid, conn=None: None)
    monkeypatch.setattr(D, "local_ready", lambda cfg=None: True)
    monkeypatch.setattr(D, "_level", lambda item, row: "full")
    import briefing_review
    import ledger
    monkeypatch.setattr(briefing_review, "read_ledger", lambda ids, convs, **kw: {"rows": {}, "events": {},
                                                                                    "by_conv": {}, "naming": {}})
    monkeypatch.setattr(ledger, "with_owner_decisions", lambda rows, db_path=None: rows)
    return calls


def test_the_item_reads_the_stored_briefing_and_a_step_runs_as_its_own_choice(real, monkeypatch):
    led = Ledger(monkeypatch)
    fe, h, _bot, clock = home()
    fe._home_deeper = D.Jobs(fe, clock=lambda: clock[0], background=False)
    ref = "item:deadline-w1"
    assert h.item(ref)["deeper"] == {"state": "none"}  # nothing stored; the read starts nothing
    assert real == []
    assert h.deeper(ref) == {"ok": True, "state": "ready"}  # the refresh the app asks for
    it = h.item(ref)
    block = it["deeper"]
    assert block["state"] == "ready" and block["by"] == "local" and "stale" not in block
    assert [s["label"] for s in block["steps"]] == ["Mark it done", f"Wait until {D._short_day(TODAY + timedelta(days=4))}",
                                                    "Continue in a conversation"]
    assert h.deeper(ref) == {"ok": True, "state": "ready"} and len(real) == 1  # a repeat costs nothing
    # the stored briefing's version is the item's own
    rec = D.Store().get("deadline-w1")
    assert rec["version"] == it["row_version"]
    # Wait runs as the item's Waiting on someone, with its undo, on the day the step named
    out, _after = h.act(ref, "step:s2")
    assert out["ok"] and out["undo"]
    clock[0] += I.UNDO_S
    h.acts.commit_due()
    args = dict(zip(led.calls[-1][2::2], led.calls[-1][3::2]))
    assert args.get("--follow-up-after") == WAIT or WAIT in json.dumps(led.calls[-1])


def test_a_kept_item_with_no_local_model_reads_unavailable_with_its_line(real, monkeypatch):
    monkeypatch.setattr(D, "local_ready", lambda cfg=None: False)
    fe, h, _bot, clock = home("kept")
    fe._home_deeper = D.Jobs(fe, clock=lambda: clock[0], background=False)
    kept = [x for x in h.payload()["left"] if x.get("kept")]
    assert kept
    it = h.item(kept[0]["ref"])
    assert it["deeper"]["state"] == "unavailable" and "own model" in it["deeper"]["note"]
    assert h.deeper(kept[0]["ref"])["state"] == "unavailable" and real == []

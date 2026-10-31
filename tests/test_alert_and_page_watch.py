"""alert.py and page_watch.py: the loud channel for a moment with a clock time."""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "preprocessor"))
import alert  # noqa: E402
import page_watch as pw  # noqa: E402
import resident_io as rio  # noqa: E402


@pytest.fixture(autouse=True)
def direct_resident_io(monkeypatch):
    """Scratch paths, written directly: no resident child in tests."""
    monkeypatch.setattr(rio, "_owner_check", lambda: False)


@pytest.fixture(autouse=True)
def web_pushes(monkeypatch):
    """The web chat pushes alert.py asks for, recorded: a test never reads
    the owner's chat block or reaches a push service."""
    import chat.direct as direct
    calls: list = []
    monkeypatch.setattr(direct, "web_notify",
                        lambda text, **kw: calls.append(("notify", text, kw)) or {"queued": True})
    monkeypatch.setattr(direct, "web_cancel",
                        lambda tag, **kw: calls.append(("cancel", tag, kw)) or True)
    return calls


def test_parse_at_defaults_to_mountain_time():
    dt = alert.parse_at("2026-10-06T10:00")
    assert dt.tzinfo is not None and dt.utcoffset() == timedelta(hours=-6)
    assert alert.parse_at("2026-10-06T10:00-04:00").utcoffset() == timedelta(hours=-4)


def test_default_text_names_the_moment_and_the_row():
    row = {"description": "Buy 2 tickets for the science show. On sale Tue Oct 6 at 10:00am MT.",
           "notes": "Source: https://www.example.edu/today/2026/09/23/science-show ."}
    text = alert.default_text(row, "show-tickets-nov4", alert.parse_at("2026-10-06T10:00"), 15)
    assert text.startswith(":rotating_light: In 15 minutes, at 10:00 am MT: Buy 2 tickets")
    assert "https://www.example.edu/today/2026/09/23/science-show" in text
    assert text.endswith("Ledger `show-tickets-nov4`.")


def test_schedule_without_a_credential_stages_for_flush(tmp_path, monkeypatch):
    """A lane that cannot reach the owner's chat credential (the resident
    runs as ea-bot) must not lose the alert: it is staged, and the
    owner's cron flushes it."""
    monkeypatch.setattr(alert, "PENDING", tmp_path / "pending-alerts.jsonl")
    monkeypatch.setattr(alert, "_row", lambda lid: {"id": lid, "description": "Tickets at 10:00am"})
    import chat
    monkeypatch.setattr(chat, "load_frontend", lambda **kw: (_ for _ in ()).throw(KeyError("ZULIP_API_KEY")))
    at = datetime.now(timezone.utc) + timedelta(days=2)
    out = alert.schedule("tix", at, 15, None)
    assert out.get("staged") is True, out
    lines = (tmp_path / "pending-alerts.jsonl").read_text().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["id"] == "tix"
    # a moment already past is refused, not staged
    out = alert.schedule("tix", datetime.now(timezone.utc) - timedelta(minutes=1), 15, None)
    assert "past" in out.get("error", "")


def test_schedule_records_on_the_row_and_is_idempotent(monkeypatch, web_pushes):
    calls = []
    monkeypatch.setattr(alert, "_ledger", lambda args: calls.append(args) or {})
    row = {"id": "tix", "description": "Tickets at 10:00am"}
    monkeypatch.setattr(alert, "_row", lambda lid: row)

    class FE:
        owner_id = "8"
    import chat.direct as direct
    monkeypatch.setattr(direct, "frontend", lambda: FE())
    monkeypatch.setattr(direct, "schedule_direct", lambda text, when, fe: 77)
    at = datetime.now(timezone.utc) + timedelta(hours=3)
    out = alert.schedule("tix", at, 15, None)
    assert out["ok"] and out["scheduled_id"] == 77 and out["kind"] == "scheduled"
    # the web chat gets the same text, queued for the moment the alert fires
    assert web_pushes == [("notify", out["text"], {"tag": "alert:tix",
                                                   "when": at - timedelta(minutes=15)})]
    assert out["web"] == {"queued": True}
    assert calls and calls[0][:3] == ["update", "--id", "tix"]
    assert any(a.startswith("alert_scheduled_id=77") for a in calls[0])
    # the row now carries the alert (ledger.py merges metadata keys into the row): a second schedule does nothing
    row.update({"alert_scheduled_id": "77", "alert_at": at.isoformat()})
    out2 = alert.schedule("tix", at, 15, None)
    assert out2.get("already") is True
    # lead already passed but the moment is ahead: send now instead
    sent = []
    monkeypatch.setattr(direct, "send_direct", lambda text, fe: sent.append(text) or {"id": 5})
    row["alert_scheduled_id"] = ""
    out3 = alert.schedule("tix", datetime.now(timezone.utc) + timedelta(minutes=5), 15, None)
    assert out3["ok"] and out3["kind"].startswith("sent now") and sent
    # sent now: the push goes now, and one queued for the row before is dropped
    assert web_pushes[-2:] == [("cancel", "alert:tix", {}),
                               ("notify", sent[0], {"tag": "alert:tix"})]
    assert sent[0].endswith("Ledger `tix`."), sent[0]
    # a custom text gets the ledger tag too, so `list` can name the row
    out4 = alert.schedule("tix", datetime.now(timezone.utc) + timedelta(minutes=5), 15, "custom words")
    assert sent[-1] == "custom words\nLedger `tix`."


def test_page_watch_condition_and_single_fire(tmp_path, monkeypatch):
    """`absent` fires when the sentence disappears, once per flip; a thin
    page (fetch failure) never fires; `present` fires on appearance."""
    monkeypatch.setattr(pw, "WATCHES", tmp_path / "watches.json")
    monkeypatch.setattr(pw, "STATE", tmp_path / "state.json")
    sold = "x" * 300 + " Tickets are sold out, and the waitlist is completely full. " + "y" * 300
    opened = "x" * 300 + " A limited number of tickets are available. " + "y" * 300
    pages = {"u": sold}
    monkeypatch.setattr(pw, "fetch", lambda url: pages[url])
    notified = []
    monkeypatch.setattr(pw, "notify", lambda w, why: notified.append((w["id"], why)) or {"direct": {"id": 1}})
    pw._save(pw.WATCHES, [{"id": "show-tickets", "url": "u", "absent": "Tickets are sold out", "present": "",
                           "message": "Show page changed", "ledger_id": "", "every": 15}])
    r1 = pw.check(force=True)
    assert r1["watches"][0]["fired"] is False and notified == []
    pages["u"] = opened
    r2 = pw.check(force=True)
    assert r2["watches"][0]["fired"] is True and notified == [("show-tickets", "pattern gone")]
    # still open on the next check: no second notification
    pw.check(force=True)
    assert len(notified) == 1
    # page goes back to sold out, then opens again: fires again
    pages["u"] = sold
    pw.check(force=True)
    pages["u"] = opened
    pw.check(force=True)
    assert len(notified) == 2
    # a failed or empty fetch is not a change
    pages["u"] = ""
    r = pw.check(force=True)
    assert r["watches"][0]["fired"] is False and "too short" in r["watches"][0]["why"]
    # the interval is honoured without --force
    r = pw.check()
    assert "skipped" in r["watches"][0]
    assert pw.condition_holds(opened, {"present": "tickets are available"}) == (True, "pattern appeared")

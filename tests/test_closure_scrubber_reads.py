#!/usr/bin/env python3
"""The Graph reads of a live closure check (closure_scrubber.read_graph).

A live check reads Sent Items, the inbox, the archive and the calendar.
These tests pin what bounds those reads: the fields each one selects, the
page size and cap, the calendar window sized to the open scheduling rows,
the run's deadline, and its single retry ladder. The last test runs the
scrubber's own tests again with every message cut down to the selected
fields, so the evidence the reads carry is the evidence the signals use.
"""

import os
import sys
import tempfile
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# No test here reads or writes the real EA state.
os.environ["EA_DIR"] = tempfile.mkdtemp(prefix="scrubber-reads-")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "preprocessor"))
sys.path.insert(0, str(HERE))

import closure_scrubber as cs  # noqa: E402
import retry_policy  # noqa: E402

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
NEXT = "https://graph.microsoft.com/v1.0/me/next-page"


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def page(n=3, more=True):
    data = {"value": [{"subject": f"m{i}"} for i in range(n)]}
    if more:
        data["@odata.nextLink"] = NEXT
    return data


def http_error(status):
    err = requests.exceptions.HTTPError(f"{status} Server Error")
    err.response = type("Resp", (), {"status_code": status})()
    return err


def retried(answer):
    """An answer that came after the call waited to retry once."""
    def give(graph):
        graph.waits += 1
        return answer
    return give


class FakeGraph:
    """graph_client's graph_get and retry_waits, by script: each call takes
    the next answer (a page, an exception to raise, or a callable that
    returns one) and, with a clock, costs `cost` seconds."""

    def __init__(self, answers=(), clock=None, cost=0.0):
        self.answers = list(answers)
        self.calls = []
        self.waits = 0
        self.clock = clock
        self.cost = cost

    def retry_waits(self):
        return self.waits

    def graph_get(self, endpoint, params=None, _retries=6, *, lane=None,
                  deadline=None):
        self.calls.append({"endpoint": endpoint, "params": params,
                           "retries": _retries, "deadline": deadline})
        if self.clock is not None:
            self.clock.t += self.cost
        answer = self.answers.pop(0) if self.answers else page(more=False)
        if callable(answer):
            answer = answer(self)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def endpoints(self):
        return [c["endpoint"].split("?")[0] for c in self.calls]


def row(rid, created, *, kind="scheduling", description="Meet about the review"):
    return {"id": rid, "type": kind, "description": description,
            "date_identified": created}


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ── the calendar window ────────────────────────────────────────────────

def test_calendar_window_spans_the_open_scheduling_rows():
    ahead = NOW + timedelta(days=cs.CALENDAR_DAYS_AHEAD)
    floor = NOW - timedelta(days=cs.CALENDAR_DAYS_BACK)
    assert cs.CALENDAR_DAYS_BACK == 7 and cs.CALENDAR_DAYS_AHEAD == 14
    # No row the calendar signal walks: no read at all.
    task = row("t", iso(NOW - timedelta(days=2)), kind="task",
               description="Send the budget sheet")
    assert cs.calendar_window([task], NOW) is None
    assert cs.calendar_window([], NOW) is None
    # The window opens at the earliest scheduling row.
    three = row("a", iso(NOW - timedelta(days=3)))
    one = row("b", iso(NOW - timedelta(days=1)))
    assert cs.calendar_window([task, one, three], NOW) == (
        NOW - timedelta(days=3), ahead)
    # A row older than the floor, or with no readable start, opens it at the floor.
    old = row("c", iso(NOW - timedelta(days=40)))
    assert cs.calendar_window([one, old], NOW) == (floor, ahead)
    assert cs.calendar_window([row("d", "")], NOW) == (floor, ahead)
    assert cs.calendar_window([row("e", "2026-09-14")], NOW)[0] == datetime(
        2026, 9, 14, tzinfo=timezone.utc)
    # A row whose notes alone sound like scheduling is walked for the
    # audit log, so it sizes the window too.
    noted = {"id": "f", "type": "task", "description": "Budget sheet",
             "notes": "they asked to meet", "date_identified": iso(NOW)}
    assert cs.calendar_window([noted], NOW) == (NOW, ahead)


# ── fields, pages and caps ─────────────────────────────────────────────

def test_each_read_selects_its_fields_and_stops_at_its_cap():
    graph = FakeGraph([page(100)] * 40)
    out = cs.read_graph(graph, [row("a", iso(NOW - timedelta(days=2)))],
                        now=NOW, clock=Clock())
    assert out["stopped"] == ""
    mail_pages = cs.MAIL_CAP // cs.PAGE_SIZE
    cal_pages = cs.CALENDAR_CAP // cs.PAGE_SIZE
    assert (mail_pages, cal_pages) == (5, 3)
    assert out["calls"] == len(graph.calls) == 3 * mail_pages + cal_pages
    assert [len(out[k]) for k in ("sent", "inbox", "archive", "calendar")] == [
        cs.MAIL_CAP, cs.MAIL_CAP, cs.MAIL_CAP, cs.CALENDAR_CAP]
    firsts = [c for c in graph.calls if c["params"] is not None]
    assert [c["endpoint"] for c in firsts] == [
        "/me/mailFolders/sentitems/messages", "/me/mailFolders/inbox/messages",
        "/me/mailFolders/archive/messages", "/me/calendarView"]
    since = (NOW - timedelta(days=14)).isoformat()
    for call, fields in zip(firsts, (cs.SENT_FIELDS, cs.INBOX_FIELDS,
                                     cs.ARCHIVE_FIELDS)):
        assert call["params"] == {
            "$select": fields, "$filter": f"receivedDateTime ge {since}",
            "$orderby": "receivedDateTime desc", "$top": 100}
    cal = firsts[3]["params"]
    assert cal["$select"] == "subject,start,organizer,attendees"
    assert cal["$top"] == 100
    assert cal["startDateTime"] == (NOW - timedelta(days=2)).isoformat()
    assert cal["endDateTime"] == (NOW + timedelta(days=14)).isoformat()
    # Later pages follow Graph's link, with no parameters of their own.
    assert all(c["endpoint"] == NEXT for c in graph.calls if c["params"] is None)
    # Every call carries the run's deadline and keeps its own retries.
    assert all(c["deadline"] == 1000.0 + cs.READ_DEADLINE_S
               and c["retries"] == 6 for c in graph.calls)


def test_no_scheduling_row_means_no_calendar_read():
    graph = FakeGraph()
    out = cs.read_graph(graph, [row("t", iso(NOW), kind="task",
                                    description="Send the budget sheet")],
                        now=NOW, clock=Clock())
    assert out["calls"] == 3 and out["calendar"] == []
    assert "/me/calendarView" not in graph.endpoints()


# ── the deadline ───────────────────────────────────────────────────────

def test_the_deadline_ends_the_reads_and_logs_one_line(capsys):
    clock = Clock()
    graph = FakeGraph([page(100)] * 40, clock=clock, cost=70.0)
    out = cs.read_graph(graph, [row("a", iso(NOW))], now=NOW, clock=clock)
    # 70 s a call: four calls pass 240 s, all of them within the sent read.
    assert out["calls"] == 4 and len(out["sent"]) == 400
    assert out["inbox"] == out["archive"] == out["calendar"] == []
    assert out["stopped"] == "the 240-second limit passed during the sent read"
    lines = [l for l in capsys.readouterr().err.splitlines() if l.strip()]
    assert lines == ["  closure_scrubber: Graph reads ended early, the 240-second "
                     "limit passed during the sent read; not read: inbox, archive, "
                     "calendar; the next run reads again"]


def test_a_read_that_starts_past_the_deadline_is_not_made(capsys):
    clock = Clock()
    graph = FakeGraph([page(5, more=False), page(5, more=False)],
                      clock=clock, cost=130.0)
    out = cs.read_graph(graph, [], now=NOW, clock=clock)
    assert out["calls"] == 2 and len(out["sent"]) == len(out["inbox"]) == 5
    assert out["stopped"] == "the 240-second limit passed before the archive read"
    assert "not read: archive;" in capsys.readouterr().err


# ── one retry ladder per run ───────────────────────────────────────────

def test_a_504_after_the_ladder_was_used_ends_the_reads(capsys):
    graph = FakeGraph([retried(page(5, more=False)), http_error(504)])
    out = cs.read_graph(graph, [row("a", iso(NOW))], now=NOW, clock=Clock())
    # The sent read waited to retry, so the inbox read had one attempt.
    assert [c["retries"] for c in graph.calls] == [6, 1]
    assert out["calls"] == 2 and len(out["sent"]) == 5
    assert out["stopped"] == ("the inbox read failed (HTTP 504) after the run "
                              "had used its retries")
    err = capsys.readouterr().err
    assert "WARNING: Outlook inbox read failed: 504 Server Error" in err
    assert "not read: inbox, archive, calendar;" in err


def test_a_failed_ladder_leaves_one_attempt_for_each_later_call():
    graph = FakeGraph([http_error(504), page(5, more=False), page(5, more=False),
                       http_error(429)])
    out = cs.read_graph(graph, [row("a", iso(NOW))], now=NOW, clock=Clock())
    assert [c["retries"] for c in graph.calls] == [6, 1, 1, 1]
    assert out["sent"] == [] and len(out["inbox"]) == len(out["archive"]) == 5
    assert out["calendar"] == []
    assert out["stopped"].startswith("the calendar read failed (HTTP 429)")


def test_a_page_after_the_ladder_keeps_the_pages_before_it():
    graph = FakeGraph([retried(page(100)), http_error(503)])
    out = cs.read_graph(graph, [], now=NOW, clock=Clock())
    assert len(out["sent"]) == 100 and out["calls"] == 2
    assert out["stopped"].startswith("the sent read failed (HTTP 503)")


def test_a_terminal_error_skips_only_its_read():
    graph = FakeGraph([page(5, more=False), page(5, more=False), http_error(404),
                       page(5, more=False)])
    out = cs.read_graph(graph, [row("a", iso(NOW))], now=NOW, clock=Clock())
    assert out["stopped"] == "" and out["archive"] == []
    assert len(out["calendar"]) == 5 and out["calls"] == 4
    assert [c["retries"] for c in graph.calls] == [6, 6, 6, 6]


def test_an_open_breaker_ends_the_reads():
    graph = FakeGraph([retry_policy.CircuitOpen("graph-background is open",
                                                service="graph-background")])
    out = cs.read_graph(graph, [row("a", iso(NOW))], now=NOW, clock=Clock())
    assert out["calls"] == 1
    assert out["stopped"] == "the sent read met an open circuit breaker"


# ── the selected fields carry every signal's evidence ──────────────────

def _only(item, fields):
    if not isinstance(item, dict):
        return item
    keep = set(fields.split(","))
    return {k: v for k, v in item.items() if k in keep}


def _evidence(result):
    return ([(c["id"], c["reason"], c["action"]) for c in result["closures"]],
            [{k: v for k, v in s.items() if k != "ts"} for s in result["suppressed"]])


def test_the_selected_fields_keep_the_evidence_of_every_scrubber_test():
    """Each test of test_closure_scrubber.py runs again with its Outlook
    messages and events cut down to the fields the live reads select, and
    each pass it makes is first run dry both ways: the closures and the
    suppressions must match. A signal that reads a field the reads leave
    out fails here."""
    import test_closure_scrubber as base

    real = cs.scrub_closures
    fed = []

    def projected(**kw):
        cut = dict(kw)
        for key, fields in (("outlook_sent", cs.SENT_FIELDS),
                            ("outlook_inbox", cs.INBOX_FIELDS),
                            ("outlook_archive", cs.ARCHIVE_FIELDS),
                            ("calendar_events", cs.CALENDAR_FIELDS)):
            if kw.get(key):
                cut[key] = [_only(m, fields) for m in kw[key]]
                fed.append(key)
        if cut != kw:
            full = real(**{**kw, "dry_run": True})
            assert _evidence(real(**{**cut, "dry_run": True})) == _evidence(full)
        return real(**cut)

    tests = [(k, v) for k, v in sorted(vars(base).items())
             if k.startswith("test_") and callable(v)]
    failed = []
    cs.scrub_closures = projected
    try:
        with tempfile.TemporaryDirectory() as td:
            for name, fn in tests:
                sub = Path(td) / name
                sub.mkdir()
                try:
                    base._call_test(fn, sub)
                except Exception as e:
                    failed.append(f"{name}: {e!r}")
    finally:
        cs.scrub_closures = real
    assert not failed, failed
    assert set(fed) == {"outlook_sent", "outlook_inbox", "outlook_archive",
                        "calendar_events"}, set(fed)
    assert len(fed) >= 30, f"only {len(fed)} inputs carried Outlook data"


def test_an_acknowledgment_in_the_body_alone_reaches_the_inbox_read(tmp_path):
    """The scrubber's tests put each acknowledgment in a subject too; this
    one is only in the body, so the inbox read must keep bodyPreview."""
    import ledger as ledger_mod
    import test_closure_scrubber as base

    db_path = base._new_db(tmp_path)
    base._add(db_path, "e-body", conv_id="convBODY", description="Send the form",
              date_identified="2026-05-25T10:00:00Z")
    message = {"conversationId": "convBODY", "receivedDateTime": "2026-05-26T16:00:00Z",
               "subject": "Re: the form", "isRead": True, "importance": "normal",
               "bodyPreview": "Thanks for submitting your form, we'll be in touch.",
               "from": {"emailAddress": {"address": "office@example.edu"}}}

    def run(inbox):
        return cs.scrub_closures(
            open_entries=ledger_mod.get_open_entries(db_path=db_path),
            outlook_sent=[], outlook_inbox=inbox, db_path=db_path, dry_run=True)

    assert _evidence(run([message]))[0][0][1].startswith("inbound_ack")
    assert _evidence(run([_only(message, cs.INBOX_FIELDS)])) == _evidence(run([message]))


if __name__ == "__main__":
    import inspect
    import io

    class _Capsys:
        def readouterr(self):
            return type("Out", (), {"err": sys.stderr.getvalue()})()

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for t in tests:
        real_err = sys.stderr
        sys.stderr = io.StringIO()
        try:
            params = inspect.signature(t).parameters
            args = [_Capsys() if p == "capsys" else Path(tempfile.mkdtemp())
                    for p in params]
            t(*args)
            sys.stderr = real_err
            print(f"  ✅ {t.__name__}")
            passed += 1
        except Exception as e:
            sys.stderr = real_err
            print(f"  ❌ {t.__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\n  {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)

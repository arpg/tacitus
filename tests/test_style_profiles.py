#!/usr/bin/env python3
"""Tests for preprocessor/style_profiles.py (persistent per-contact style).

Profiles and few-shot snippets accumulate in SQLite, rather than being
recomputed from a 14-day sent window each briefing.
Covers: feature extraction, watermark idempotency, running averages,
snippet rotation (latest 3), quoted-history stripping, and get_profile.
"""

import os
import sys
import sqlite3
import tempfile
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
# Sandbox before import: owner_trust counts a process whose EA_DIR is the
# live tree as the owner, so a run of this file alone must not see it.
os.environ["EA_DIR"] = tempfile.mkdtemp(prefix="ea-test-style-profiles-")
sys.path.insert(0, str(HERE.parent / "preprocessor"))

import style_profiles as sp  # noqa: E402


def _msg(addr, sent_at, body, name="Alice Smith"):
    return {
        "sentDateTime": sent_at,
        "toRecipients": [{"emailAddress": {"address": addr, "name": name}}],
        "bodyPreview": body,
    }


BODY = ("Hi Alice,\n\nSounds good, I'll see you Thursday at 2. "
        "Don't worry about the slides.\n\nThanks,\nKit")


def test_profile_accumulates():
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "p.db"
        sp.update_from_messages(
            [_msg("alice@x.edu", "2026-06-01T10:00:00Z", BODY)], db_path=db)
        r = sp.update_from_messages(
            [_msg("alice@x.edu", "2026-06-02T10:00:00Z",
                  "Hi Alice,\n\nWorks for me.\n\nThanks,\nKit")],
            db_path=db)
        assert r["processed"] == 1
        p = sp.get_profile("alice@x.edu", db_path=db)
        assert p["samples"] == 2
        assert p["greeting"] == "Hi {name},"
        assert p["closing"].lower().startswith("thanks")
        assert p["contractions"].startswith("sometimes (50%")
        assert "uses_contractions" not in p
        assert len(p["recent_examples"]) == 2


def test_watermark_idempotent():
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "p.db"
        batch = [_msg("kit.bancroft59@example.edu", "2026-06-01T10:00:00Z", BODY)]
        r1 = sp.update_from_messages(batch, db_path=db)
        r2 = sp.update_from_messages(batch, db_path=db)  # same batch again
        assert r1["processed"] == 1
        assert r2["processed"] == 0 and r2["skipped"] == 1
        assert sp.get_profile("kit.bancroft59@example.edu", db_path=db)["samples"] == 1


def test_snippet_rotation_keeps_latest_three():
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "p.db"
        msgs = [_msg("kit.bancroft59@example.edu", f"2026-06-0{i}T10:00:00Z",
                     f"Hi Alice,\n\nNote number {i} here with enough text.")
                for i in range(1, 6)]
        sp.update_from_messages(msgs, db_path=db)
        p = sp.get_profile("kit.bancroft59@example.edu", db_path=db)
        assert p["samples"] == 5
        assert len(p["recent_examples"]) == 3
        assert "number 5" in p["recent_examples"][0]["snippet"]
        assert "number 3" in p["recent_examples"][-1]["snippet"]


def test_quoted_history_stripped():
    body = ("Hi Bob,\n\nYes, approved.\n\nKit\n"
            "________________________________\n"
            "From: Bob Jones <bob@y.org>\nSent: Monday\n"
            "A very long quoted thread that is not Kit's writing at all "
            "and would poison the length statistics badly " * 5)
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "p.db"
        sp.update_from_messages(
            [_msg("bob@y.org", "2026-06-01T10:00:00Z", body, name="Bob")],
            db_path=db)
        p = sp.get_profile("bob@y.org", db_path=db)
        assert p["avg_words"] < 15, f"quoted thread leaked: {p['avg_words']}"
        assert "quoted thread" not in p["recent_examples"][0]["snippet"]


def _multi(addrs, sent_at, body):
    return {
        "sentDateTime": sent_at,
        "toRecipients": [{"emailAddress": {"address": a, "name": a}}
                         for a in addrs],
        "body": {"contentType": "text", "content": body},
    }


def test_bare_sign_off_reported_over_stale_closing():
    """One 'Best' must not pin a contact who signs off bare 9 times.

    The legacy `closing` column is last-write-wins and cannot be cleared
    by a later bare email, which had every draft to Gus closing
    'Best' against a 93%-bare record.
    """
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "p.db"
        msgs = [_msg("zion.corvino32@example.edu", "2026-06-01T10:00:00Z",
                     "Hi Gus,\n\nThat works for me.\n\nBest,\nKit")]
        for i in range(2, 11):
            msgs.append(_msg("zion.corvino32@example.edu", f"2026-06-{i:02d}T10:00:00Z",
                             "Hi Gus,\n\nAgreed on the timeline.\n\nKit"))
        sp.update_from_messages(msgs, db_path=db)
        p = sp.get_profile("zion.corvino32@example.edu", db_path=db)
        assert p["closing"] == "Best"          # legacy field, unchanged
        assert p["bare_close_rate"] == 0.9
        assert p["sign_off"].startswith("bare")


def test_median_not_dragged_by_one_long_email():
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "p.db"
        msgs = [_msg("harper.juniper70@example.edu", f"2026-06-{i:02d}T10:00:00Z",
                     "Hi Bob,\n\n" + " ".join(["word"] * 30) + "\n\nKit")
                for i in range(1, 10)]
        msgs.append(_msg("harper.juniper70@example.edu", "2026-06-20T10:00:00Z",
                         "Hi Bob,\n\n" + " ".join(["word"] * 1500) + "\n\nKit"))
        sp.update_from_messages(msgs, db_path=db)
        p = sp.get_profile("harper.juniper70@example.edu", db_path=db)
        assert p["median_words"] < 60          # the outlier does not move it
        assert p["avg_words"] > 150            # the legacy mean does
        assert p["max_words"] > 1400


def test_every_addressee_of_a_small_thread_is_credited():
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "p.db"
        sp.update_from_messages(
            [_multi(["kit.bancroft59@example.edu", "harper.juniper70@example.edu", "yael.marchetti63@example.edu"],
                    "2026-06-01T10:00:00Z",
                    "Hi all,\n\nLet's meet Thursday to settle it.\n\nKit")],
            db_path=db)
        for a in ("kit.bancroft59@example.edu", "harper.juniper70@example.edu", "yael.marchetti63@example.edu"):
            assert sp.get_profile(a, db_path=db)["messages"] == 1


def test_broadcast_is_not_a_per_contact_model():
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "p.db"
        r = sp.update_from_messages(
            [_multi([f"p{i}@x.edu" for i in range(8)],
                    "2026-06-01T10:00:00Z",
                    "Hi all,\n\nThe retreat is moved to Friday.\n\nKit")],
            db_path=db)
        assert r["processed"] == 0
        assert sp.get_profile("dana.westbrook88@example.edu", db_path=db) is None


def test_zoom_invite_boilerplate_is_not_his_prose():
    """Outlook appends it to every hold; counting it as style makes a
    contact he meets with weekly look like someone he never writes to."""
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "p.db"
        sp.update_from_messages([_msg(
            "dana.galloway76@example.edu", "2026-06-01T10:00:00Z",
            "Hi Casey,\n\nBack Thursday if you want to reschedule.\n\nKit"
            "\n\nKit Calloway is inviting you to a scheduled Zoom "
            "meeting.\nJoin Zoom Meeting\nhttps://example.zoom.us/j/123\n"
            "Meeting ID: 123 456 7890\nOne tap mobile\n")], db_path=db)
        snippet = sp.get_profile("dana.galloway76@example.edu", db_path=db)["recent_examples"][0]
        assert "Zoom" not in snippet["snippet"]
        assert "reschedule" in snippet["snippet"]


def test_html_body_keeps_its_line_structure():
    """Flattening block tags to spaces puts the whole email on one line,
    and the closing lives on the last line."""
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "p.db"
        sp.update_from_messages([{
            "sentDateTime": "2026-06-01T10:00:00Z",
            "toRecipients": [{"emailAddress": {"address": "jules.isenberg6@example.edu",
                                               "name": "H"}}],
            "body": {"contentType": "html",
                     "content": "<div>Hi Harper,</div><div>&nbsp;</div>"
                                "<div>Ship it when the eval clears.</div>"
                                "<div>Best,</div><div>Kit</div>"},
        }], db_path=db)
        p = sp.get_profile("jules.isenberg6@example.edu", db_path=db)
        assert p["greeting"] == "Hi {name},"
        assert p["closing"] == "Best"
        assert "&nbsp;" not in p["recent_examples"][0]["snippet"]


def test_full_body_sync_upgrades_a_preview_without_double_counting():
    """The briefing records a 255-char preview; the cron sync later sees
    the real body. The watermark would lock the second pass out, and
    re-running it must not count the message twice."""
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "p.db"
        preview = "Hi Alice,\n\nHere is the plan for Thursday, we should"
        sp.update_from_messages(
            [_msg("alice@x.edu", "2026-06-01T10:00:00Z", preview)],
            db_path=db)
        assert sp.get_profile("alice@x.edu", db_path=db)["messages"] == 1

        full = {
            "sentDateTime": "2026-06-01T10:00:00Z",
            "toRecipients": [{"emailAddress": {"address": "alice@x.edu",
                                               "name": "Alice"}}],
            "body": {"contentType": "text",
                     "content": preview + " start with the eval harness "
                                          "and hold the rest.\n\nKit"},
        }
        sp.update_from_messages([full], db_path=db, reprocess=True)
        p = sp.get_profile("alice@x.edu", db_path=db)
        assert p["messages"] == 1                     # not counted twice
        assert p["samples"] == 1
        assert "eval harness" in p["recent_examples"][0]["snippet"]


def test_unknown_contact_returns_none():
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "p.db"
        sp.update_from_messages([], db_path=db)
        assert sp.get_profile("dov.fairbanks76@example.org", db_path=db) is None


def test_short_or_recipientless_skipped():
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "p.db"
        r = sp.update_from_messages([
            {"sentDateTime": "2026-06-01T10:00:00Z", "toRecipients": [],
             "bodyPreview": BODY},
            _msg("kit.bancroft59@example.edu", "2026-06-01T11:00:00Z", "ok"),
        ], db_path=db)
        assert r["processed"] == 0 and r["skipped"] == 2


# ---------------------------------------------------------------------------
# sync from the resident: mediated, structured, idempotent
# ---------------------------------------------------------------------------

import contextlib  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402

import mail_mediation as M  # noqa: E402


def _run_main(argv):
    """main(argv) -> (exit code, stdout)."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        try:
            rc = sp.main(argv)
        except SystemExit as e:
            rc = e.code
    return rc, out.getvalue()


def _sync_with(sent, db):
    """`sync --days 1` with Sent Items stubbed to `sent` (a list, or an
    exception to raise), writing to `db`."""
    real_fetch, real_db = sp._fetch_sent, sp.DB_PATH

    def fetch(days):
        if isinstance(sent, Exception):
            raise sent
        return list(sent)
    sp._fetch_sent, sp.DB_PATH = fetch, Path(db)
    try:
        return _run_main(["sync", "--days", "1"])
    finally:
        sp._fetch_sent, sp.DB_PATH = real_fetch, real_db


def test_the_mediator_runs_sync_as_the_owner_and_nothing_else():
    import mail_mediator
    assert M.SAFE["style_profiles"] == {"sync"}
    assert "style_profiles" not in M.GATED
    for verb in ("get", "stats"):
        assert not M.is_safe("style_profiles", verb)
    assert mail_mediator.TOOLS["style_profiles"].name == "style_profiles.py"
    assert M.parser_options("style_profiles", "sync") is not None
    assert M.sanitize("style_profiles", "sync", ["sync", "--days", "21"]) is None


def test_sync_goes_to_the_mediator_and_get_stays_local():
    calls = []

    def forward(tool, argv, transform=None):
        calls.append((tool, list(argv)))
        print(json.dumps({"status": "ok", "fetched": 0}))
        raise SystemExit(0)

    real = M.forward_or_none
    M.forward_or_none = forward
    try:
        rc, out = _run_main(["sync", "--days", "1"])
        assert rc == 0 and json.loads(out)["status"] == "ok"
        assert calls == [("style_profiles", ["sync", "--days", "1"])]
        with tempfile.TemporaryDirectory() as td:
            real_db = sp.DB_PATH
            sp.DB_PATH = Path(td) / "p.db"
            try:
                rc, out = _run_main(["get", "--contact", "alice@x.edu"])
            finally:
                sp.DB_PATH = real_db
        assert rc == 0 and json.loads(out)["found"] is False
        assert len(calls) == 1                       # get never forwards
    finally:
        M.forward_or_none = real


def test_a_failed_read_is_one_json_error_and_keeps_the_profiles():
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "p.db"
        sp.update_from_messages(
            [_msg("alice@x.edu", "2026-06-01T10:00:00Z", BODY)], db_path=db)
        rc, out = _sync_with(PermissionError(13, "Permission denied",
                                             "msal_token_cache.json"), db)
        res = json.loads(out)
        assert rc == 1 and res["status"] == "error"
        assert "Sent Items" in res["message"] and res["profiles"] == "unchanged"
        assert sp.get_profile("alice@x.edu", db_path=db)["samples"] == 1


def test_sync_reports_counts_and_contacts_and_repeats_without_double_counting():
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "p.db"
        sent = [_msg("alice@x.edu", "2026-06-01T10:00:00Z", BODY),
                _msg("kit.bancroft59@example.edu", "2026-06-02T10:00:00Z",
                     "Hi Kit,\n\nThe draft looks right to me.\n\nThanks")]
        rc, out = _sync_with(sent, db)
        res = json.loads(out)
        assert rc == 0 and res["status"] == "ok"
        assert res["fetched"] == 2 and res["processed"] == 2
        assert res["contacts_updated"] == 2
        assert res["contacts"] == ["alice@x.edu", "kit.bancroft59@example.edu"]
        rc, out = _sync_with(sent, db)               # the same week again
        assert rc == 0 and json.loads(out)["status"] == "ok"
        p = sp.get_profile("alice@x.edu", db_path=db)
        assert p["messages"] == 1 and p["samples"] == 1


# ── EA-written mail, closings, contraction guidance ──────────────────

import ea_written  # noqa: E402

EA_BODY = ("Hi Alice,\n\nThe lab can host the visit on the ninth. We need the "
           "mount finished before then, and two people for the outdoor runs. "
           "Could you confirm who will come?\n\nKit")
OWN_BODY = "Hi Alice,\n\nCan't make Tuesday, sorry! Let's do Friday.\n\nKit"


def _conv(msg, conv):
    return {**msg, "conversationId": conv}


def _ea(conv="c-ea", ts="2026-06-01T09:00:00Z"):
    return ea_written.Matcher([{"ts": ts, "conversation_id": conv, "text": EA_BODY}])


def test_mail_the_ea_wrote_is_left_out():
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "p.db"
        r = sp.update_from_messages(
            [_conv(_msg("alice@x.edu", "2026-06-01T10:00:00Z", EA_BODY), "c-ea"),
             _conv(_msg("alice@x.edu", "2026-06-02T10:00:00Z", OWN_BODY), "c-own")],
            db_path=db, matcher=_ea())
        assert r["processed"] == 1 and r["ea_written"] == 1 and r["ea_check"] is True
        p = sp.get_profile("alice@x.edu", db_path=db)
        assert p["samples"] == 1 and p["messages"] == 1
        assert [e["snippet"] for e in p["recent_examples"]] == [OWN_BODY]


def test_purge_removes_ea_mail_already_in_a_profile():
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "p.db"
        ea_msg = _conv(_msg("alice@x.edu", "2026-06-01T10:00:00Z", EA_BODY), "c-ea")
        own = _conv(_msg("alice@x.edu", "2026-06-02T10:00:00Z", OWN_BODY), "c-own")
        only_ea = _conv(_msg("bo@x.edu", "2026-06-01T10:00:00Z", EA_BODY, name="Bo"), "c-ea")
        empty = ea_written.Matcher([])
        sp.update_from_messages([ea_msg, own, only_ea], db_path=db, matcher=empty)
        before = sp.get_profile("alice@x.edu", db_path=db)
        assert before["samples"] == 2
        r = sp.purge_ea_written([ea_msg, own, only_ea], matcher=_ea(), db_path=db)
        assert r == {"purged": 2, "purged_contacts": 2, "ea_check": True}
        p = sp.get_profile("alice@x.edu", db_path=db)
        assert p["samples"] == 1 and p["messages"] == 1
        assert p["avg_words"] == len(OWN_BODY.split())
        assert p["contraction_rate"] == 1.0
        assert [e["snippet"] for e in p["recent_examples"]] == [OWN_BODY]
        # A contact the EA alone wrote to has no profile left.
        assert sp.get_profile("bo@x.edu", db_path=db) is None
        # A second purge finds nothing.
        assert sp.purge_ea_written([ea_msg], matcher=_ea(), db_path=db)["purged"] == 0


def test_purge_without_the_record_checks_nothing():
    with tempfile.TemporaryDirectory() as td:
        r = sp.purge_ea_written([], matcher=ea_written.Matcher([], available=False),
                                db_path=Path(td) / "p.db")
        assert r == {"purged": 0, "purged_contacts": 0, "ea_check": False}


def test_the_first_checked_sync_reads_back_far_and_purges_once():
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "p.db"
        ea_msg = _conv(_msg("alice@x.edu", "2026-06-01T10:00:00Z", EA_BODY), "c-ea")
        sp.update_from_messages([ea_msg], db_path=db, matcher=ea_written.Matcher([]))
        days = []
        real_fetch, real_db, real_load = sp._fetch_sent, sp.DB_PATH, ea_written.load
        sp._fetch_sent = lambda d: days.append(d) or [ea_msg]
        sp.DB_PATH, ea_written.load = db, (lambda folder=None: _ea())
        try:
            rc, out = _run_main(["sync", "--days", "21"])
            res = json.loads(out)
            assert rc == 0 and res["purged"] == 1 and res["ea_written"] == 1
            assert sp.get_profile("alice@x.edu", db_path=db) is None
            rc, out = _run_main(["sync", "--days", "21"])
            assert rc == 0 and json.loads(out)["days"] == 21
        finally:
            sp._fetch_sent, sp.DB_PATH, ea_written.load = real_fetch, real_db, real_load
        assert days == [sp.FIRST_PURGE_DAYS, 21]


def test_a_long_line_starting_with_thanks_is_no_closing():
    body = ("Hi Alice,\n\nSee the attached plan.\n\nThanks, this is great and we "
            "will name your group as a partner on the proposal.\n\nKit")
    assert sp._extract_features(body)["closing"] == ""
    assert sp._extract_features("Hi Alice,\n\nSee you then.\n\nThanks,\nKit")[
        "closing"] == "Thanks"
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "p.db"
        sp.update_from_messages([_msg("alice@x.edu", "2026-06-01T10:00:00Z", body)],
                                db_path=db, matcher=ea_written.Matcher([]))
        # A row stored before the cap still reads back with no closing.
        con = sqlite3.connect(db)
        long = "Thanks, this is great and we will name your group as a partner"
        con.execute("UPDATE profiles SET closing = ?", (long,))
        con.execute("UPDATE msgs SET closing = ?", (long,))
        con.commit()
        con.close()
        p = sp.get_profile("alice@x.edu", db_path=db)
        assert p["closing"] == "" and p["sign_off"].startswith("bare")
        assert p["closings_seen"] == {}


def test_contraction_guidance_follows_the_rate():
    assert sp.contraction_guidance(0.0).startswith("rarely (0%")
    assert sp.contraction_guidance(0.33).startswith("sometimes (33%")
    assert "do not write them all out" in sp.contraction_guidance(0.33)
    assert sp.contraction_guidance(0.8).startswith("usually (80%")


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

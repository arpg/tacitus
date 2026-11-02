#!/usr/bin/env python3
"""Unit tests for preprocessor/graph_client.py reply-building and preflight."""

import os
import sys
import tempfile
import time
import traceback
from pathlib import Path
from unittest.mock import patch

# No test may touch production state. The graph_get retry tests below drive
# the REAL circuit breaker, which persists to EA_DIR/state/circuit-breakers.json;
# with EA_DIR unset that resolves to ~/.ea and a test run would rewrite the
# live Graph breaker counters. Set before the import,
# because retry_policy binds STATE_FILE at module load.
os.environ["EA_DIR"] = tempfile.mkdtemp(prefix="ea-test-graph-")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "preprocessor"))

import graph_client as gc


# --------------------------------------------------------------------------
# _normalize_plaintext_quoted
# --------------------------------------------------------------------------

PLAINTEXT_WRAPPED_IN_BODY = (
    "<html><head></head><body>"
    "________________________________________\r\n"
    "From: Gus Bancroft\r\n"
    "Sent: Monday, 20 April 2026 23:55:58\r\n"
    "To: Kit Calloway\r\n"
    "Subject: Lab Windows Servers for simulation?\r\n"
    "\r\n"
    "Hello Kit,\r\n"
    "\r\n"
    "Long story short I am building an open source robot.\r\n"
    "1. Question one?\r\n"
    "2. Question two?\r\n"
    "</body></html>"
)


def test_normalize_plaintext_converts_newlines_to_br():
    """Plain-text wrapped in <body> gets newlines converted to <br>."""
    out = gc._normalize_plaintext_quoted(PLAINTEXT_WRAPPED_IN_BODY)
    # Every newline in the inner body should have become a <br>
    assert "<br>" in out
    assert "From: Gus Bancroft<br>" in out
    assert "Sent: Monday, 20 April 2026 23:55:58<br>" in out
    assert "To: Kit Calloway<br>" in out
    assert "Subject: Lab Windows Servers for simulation?<br>" in out
    # Raw \r\n should be gone from the converted region
    assert "\r\n" not in out


def test_normalize_plaintext_preserves_structured_html():
    """HTML that already has <br>/<div>/<p> is left alone."""
    structured = (
        "<html><body>"
        "<div>Line one</div>\n<div>Line two</div>\n"
        "</body></html>"
    )
    out = gc._normalize_plaintext_quoted(structured)
    assert out == structured  # no change


def test_normalize_plaintext_preserves_html_with_br():
    """<br>-formatted HTML is left alone even when it also has newlines."""
    h = "<html><body>Line one<br>\nLine two<br>\n</body></html>"
    out = gc._normalize_plaintext_quoted(h)
    assert out == h


def test_normalize_plaintext_no_body_tag_untouched():
    """Content without <body> wrapper passes through."""
    raw = "Just some text\nwith newlines"
    assert gc._normalize_plaintext_quoted(raw) == raw


def test_normalize_plaintext_empty_body_untouched():
    """Body with no newlines passes through unchanged."""
    h = "<html><body>Single line of text.</body></html>"
    assert gc._normalize_plaintext_quoted(h) == h


# --------------------------------------------------------------------------
# _build_reply_html — end-to-end on the plaintext fixture
# --------------------------------------------------------------------------

def test_build_reply_preserves_quoted_line_breaks_from_plaintext():
    """A plain-text original is normalized BEFORE the reply is inserted,
    so its line breaks survive the HTML contentType PATCH instead of
    collapsing into one run-on paragraph."""
    new_body = "Hi Gus,\n\nThanks for reaching out.\n\nBest,\nKit"
    combined = gc._build_reply_html(new_body, PLAINTEXT_WRAPPED_IN_BODY, content_type="Text")

    # The reply itself is at the top
    assert combined.index("Hi Gus,") < combined.index("From: Gus Bancroft")

    # Quoted headers each have their own line break — NOT run-on
    assert "From: Gus Bancroft<br>" in combined
    assert "Sent: Monday, 20 April 2026 23:55:58<br>" in combined
    assert "To: Kit Calloway<br>" in combined
    assert "Hello Kit,<br>" in combined

    # EA_REPLY markers wrap the new portion
    assert "EA_REPLY_START" in combined
    assert "EA_REPLY_END" in combined


def test_build_reply_with_structured_html_quoted_unchanged():
    """When the quoted thread already has proper HTML structure, it is
    inserted as-is — we don't double-break anything."""
    structured = (
        "<html><body>"
        "<div id=\"divRplyFwdMsg\"><b>From:</b> Alice<br><b>Sent:</b> Today</div>"
        "<div>Hi Kit,</div><div><br></div><div>Question?</div>"
        "</body></html>"
    )
    combined = gc._build_reply_html("Thanks!", structured, content_type="Text")
    # Original structure preserved
    assert "<div id=\"divRplyFwdMsg\">" in combined
    assert combined.count("<div>Hi Kit,</div>") == 1


# --------------------------------------------------------------------------
# preflight_check — em-dash detection
# --------------------------------------------------------------------------

def _fake_draft(body_content, body_type="text", subject="Test", to=("rio.sorensen85@example.com",)):
    return {
        "subject": subject,
        "body": {"content": body_content, "contentType": body_type},
        "toRecipients": [{"emailAddress": {"address": t}} for t in to],
        "ccRecipients": [],
    }


def test_preflight_flags_em_dash_in_new_text():
    """Em-dash in drafted reply is a hard error."""
    body = (
        "Hi there — this is my reply.\n\nBest,\nKit\n\n"
        "-----Original Message-----\nFrom: someone@example.com"
    )
    with patch.object(gc, "get_message_full", return_value=_fake_draft(body)):
        result = gc.preflight_check("fake-id")
    assert any("em-dash" in e.lower() for e in result["errors"])
    assert result["ok"] is False


def test_preflight_flags_en_dash_in_new_text():
    """En-dash (U+2013) is also flagged."""
    body = "Hi – just checking in.\n\nBest,\nKit"
    with patch.object(gc, "get_message_full", return_value=_fake_draft(body)):
        result = gc.preflight_check("fake-id")
    assert any("dash" in e.lower() for e in result["errors"])


def test_preflight_allows_em_dash_only_in_quoted_thread():
    """Em-dash in someone else's quoted reply should NOT block — we only
    police the user's own new text."""
    body = (
        "Hi there, this is my clean reply.\n\nBest,\nKit\n\n"
        "-----Original Message-----\n"
        "From: Gus\nSubject: hey\n\n"
        "I wrote this — with an em-dash — and you can't stop me."
    )
    with patch.object(gc, "get_message_full", return_value=_fake_draft(body)):
        result = gc.preflight_check("fake-id")
    assert not any("dash" in e.lower() for e in result["errors"]), \
        f"Expected no dash errors, got: {result['errors']}"


def test_preflight_flags_em_dash_in_html_draft():
    """Em-dash inside HTML tags (text between tags) is still flagged."""
    body = (
        "<div>Hi there <span>— this is my reply</span>.</div>"
        "<!-- EA_REPLY_END --><hr>"
        "<div>From: Someone</div>"
    )
    with patch.object(gc, "get_message_full",
                      return_value=_fake_draft(body, body_type="html")):
        result = gc.preflight_check("fake-id")
    assert any("dash" in e.lower() for e in result["errors"])


def test_preflight_clean_reply_passes():
    """A clean reply with no em-dashes should not trigger the dash check."""
    body = (
        "Hi there, thanks for the note.\n\nBest,\nKit\n\n"
        "-----Original Message-----\nFrom: someone\n"
    )
    with patch.object(gc, "get_message_full", return_value=_fake_draft(body)):
        result = gc.preflight_check("fake-id")
    assert not any("dash" in e.lower() for e in result["errors"])


# --------------------------------------------------------------------------
# graph_get retry behavior on a 5xx from Graph.
# An except clause that catches only ConnectionError/Timeout lets
# resp.raise_for_status() short-circuit the retry loop on every 504.
# --------------------------------------------------------------------------

import requests


class _FakeResp:
    """Minimal stand-in for a requests.Response for HTTPError tests."""
    def __init__(self, status, headers=None, json_data=None):
        self.status_code = status
        self.headers = headers or {}
        self._json = json_data or {}
    def raise_for_status(self):
        if self.status_code >= 400:
            err = requests.exceptions.HTTPError(f"{self.status_code}")
            err.response = self
            raise err
    def json(self):
        return self._json


def _patched_graph_get(mock_responses):
    """Run gc.graph_get with requests.get returning a queued sequence
    of fake responses, and time.sleep neutered so the test runs fast."""
    state = {"i": 0, "waits": []}
    def fake_requests_get(*a, **kw):
        r = mock_responses[state["i"]]
        state["i"] += 1
        return r
    def fake_sleep(s):
        state["waits"].append(s)
    with patch("requests.get", side_effect=fake_requests_get), \
         patch.object(gc, "_headers", return_value={}), \
         patch.object(gc, "_api_base", return_value="https://graph"), \
         patch("time.sleep", side_effect=fake_sleep):
        result = gc.graph_get("/me/messages")
    return result, state


def test_graph_get_retries_on_504_and_eventually_succeeds():
    responses = [
        _FakeResp(504),
        _FakeResp(504),
        _FakeResp(200, json_data={"value": [{"id": "ok"}]}),
    ]
    result, state = _patched_graph_get(responses)
    assert result == {"value": [{"id": "ok"}]}
    # Should have slept twice (after each 504) before succeeding.
    assert len(state["waits"]) == 2
    assert state["waits"][0] >= 5  # follows the 5,15,45,90,180 schedule


def test_graph_get_retries_on_503_and_502():
    """502/503 are also retried (Graph backend overload)."""
    responses = [
        _FakeResp(503),
        _FakeResp(502),
        _FakeResp(200, json_data={"value": []}),
    ]
    result, _ = _patched_graph_get(responses)
    assert result == {"value": []}


def test_graph_get_honors_retry_after_on_429():
    """429 with Retry-After header: wait the server-specified seconds."""
    responses = [
        _FakeResp(429, headers={"Retry-After": "12"}),
        _FakeResp(200, json_data={"ok": True}),
    ]
    _, state = _patched_graph_get(responses)
    assert state["waits"] == [12]


def test_graph_get_does_not_retry_on_401():
    """4xx auth errors are terminal — retrying won't change the answer."""
    responses = [_FakeResp(401)]
    try:
        _patched_graph_get(responses)
        raise AssertionError("expected HTTPError to propagate")
    except requests.exceptions.HTTPError as e:
        assert e.response.status_code == 401


def test_graph_get_does_not_retry_on_500():
    """500 is terminal — Graph returns it for malformed queries / bugs."""
    responses = [_FakeResp(500)]
    try:
        _patched_graph_get(responses)
        raise AssertionError("expected HTTPError to propagate")
    except requests.exceptions.HTTPError as e:
        assert e.response.status_code == 500


def test_graph_get_gives_up_after_max_retries():
    """If 504 persists past the schedule, raise to surface the outage."""
    responses = [_FakeResp(504)] * 6
    try:
        _patched_graph_get(responses)
        raise AssertionError("expected HTTPError to propagate")
    except requests.exceptions.HTTPError as e:
        assert e.response.status_code == 504


# --------------------------------------------------------------------------
# Throttling: the 429 note, the person's wait budget, the plain words.
# --------------------------------------------------------------------------

import logging
from contextlib import contextmanager

import retry_policy as rp


@contextmanager
def _fresh_breakers():
    """A breaker state of the test's own, so a run that spends its retries
    opens no breaker another test reads."""
    before = rp.STATE_FILE
    with tempfile.TemporaryDirectory() as td:
        rp.STATE_FILE = Path(td) / "circuit-breakers.json"
        gc._breakers.clear()
        try:
            yield
        finally:
            rp.STATE_FILE = before
            gc._breakers.clear()


@contextmanager
def _notes():
    """The graph_client notes, as (level name, message) pairs."""
    seen = []

    class _Keep(logging.Handler):
        def emit(self, record):
            seen.append((record.levelname, record.getMessage()))
    h = _Keep()
    gc.log.addHandler(h)
    try:
        yield seen
    finally:
        gc.log.removeHandler(h)


_THROTTLE_BODY = ('{"error":{"code":"ApplicationThrottled","message":"Application is '
                  'over its MailboxConcurrency limit.","innerError":{"date":"x",'
                  '"request-id":"' + "r" * 300 + '"}}}')


def _throttled(ra="3"):
    r = _FakeResp(429, headers={"Retry-After": ra})
    r.text = _THROTTLE_BODY
    return r


def _lane_get(responses, lane):
    state = {"i": 0, "waits": []}

    def fake_requests_get(*a, **kw):
        r = responses[state["i"]]
        state["i"] += 1
        return r
    with patch("requests.get", side_effect=fake_requests_get), \
         patch.object(gc, "_headers", return_value={}), \
         patch.object(gc, "_api_base", return_value="https://graph"), \
         patch("time.sleep", side_effect=state["waits"].append):
        try:
            return gc.graph_get("/me/messages", lane=lane), state
        except Exception as e:
            return e, state


def test_a_429_is_noted_once_per_call_with_retry_after_and_the_body():
    with _fresh_breakers(), _notes() as seen:
        result, state = _lane_get([_throttled(), _throttled(), _FakeResp(200, json_data={"ok": 1})],
                                  "background")
    assert result == {"ok": 1} and state["waits"] == [3, 3]
    warnings = [m for level, m in seen if level == "WARNING"]
    assert len(warnings) == 1, seen
    assert "Retry-After 3" in warnings[0] and "MailboxConcurrency" in warnings[0]
    assert "/me/messages" in warnings[0] and "r" * 201 not in warnings[0]
    body = warnings[0].split("body: ", 1)[1]
    assert len(body) <= 200


def test_an_interactive_call_stops_at_its_time_budget():
    """The person waits, and the mediator ends its child at 120 s: an
    interactive call stops once its next wait would pass the budget."""
    assert gc._INTERACTIVE_BUDGET_S == 45
    with _fresh_breakers():
        before = gc.throttled_calls()
        result, state = _lane_get([_throttled("30"), _throttled("30"), _FakeResp(200)],
                                  "interactive")
        assert isinstance(result, gc.GraphThrottled), result
        assert state["i"] == 2 and state["waits"] == [30]
        assert gc.throttled_calls() == before + 1
        # Graph asking for more than the budget: no wait at all
        result, state = _lane_get([_throttled("120"), _FakeResp(200)], "interactive")
        assert isinstance(result, gc.GraphThrottled) and state["waits"] == []
        assert result.retry_after == 120
        # a background call keeps its full ladder
        result, state = _lane_get([_throttled("30"), _throttled("30"), _FakeResp(200, json_data={})],
                                  "background")
        assert result == {} and state["waits"] == [30, 30]


def test_graph_throttled_is_the_429_http_error_in_plain_words():
    with _fresh_breakers():
        result, _ = _lane_get([_throttled("2")] * 6, "background")
    assert isinstance(result, requests.exceptions.HTTPError)
    assert result.response.status_code == 429 and result.retry_after == 2
    assert result.endpoint == "/me/messages"
    assert str(result) == gc.THROTTLED_TEXT
    assert "graph.microsoft.com" not in str(result)


def test_the_throttle_line_names_a_reopen_time_when_one_is_known():
    soon = time.time() + 600
    line = gc.throttle_line(rp.CircuitOpen("x", service="graph-background", reopens_at=soon))
    assert line.startswith("Microsoft is limiting mail access right now. Try again in a few minutes (after ")
    assert line.endswith(").") and "\n" not in line
    # a time under a minute away is not worth naming
    assert gc.throttle_line(rp.CircuitOpen("x", service="graph", reopens_at=time.time() + 5)) \
        == gc.THROTTLED_TEXT
    plain_429 = requests.exceptions.HTTPError("429")
    plain_429.response = _FakeResp(429)
    assert gc.throttle_line(plain_429) == gc.THROTTLED_TEXT
    assert gc.throttle_line(rp.CircuitOpen("x", service="remote")) is None
    assert gc.throttle_line(ValueError("no")) is None
    not_found = requests.exceptions.HTTPError("404")
    not_found.response = _FakeResp(404)
    assert gc.throttle_line(not_found) is None


# --------------------------------------------------------------------------
# update_draft — quoted thread survives a revision
#
# Outlook's sanitizer strips standalone HTML comments from stored bodies, so
# the EA_REPLY_START/END markers never come back on a round trip. Falling
# through to a wholesale replace would delete the quoted thread on every
# revision, and CLAUDE.md routes ALL revisions through update_draft.
# --------------------------------------------------------------------------

SANITIZED_REPLY_DRAFT = (
    '<html><head><style><!-- p{margin:0} --></style></head>'
    '<body dir="ltr">'
    '<div class="elementToProof">Hi Yuki,</div>'
    '<div class="elementToProof"><br></div>'
    '<div class="elementToProof">Original reply text.</div>'
    '<br>'
    '<hr tabindex="-1" style="display:inline-block; width:98%">'
    '<div id="divRplyFwdMsg" dir="ltr"><b>From:</b> CS Team<br>'
    '<b>Sent:</b> Tuesday, July 28, 2026</div>'
    '<div>Quoted original message.</div>'
    '</body></html>'
)


def test_split_quoted_thread_finds_boundary_without_markers():
    """Boundary is located structurally, above the <hr> preceding divRplyFwdMsg."""
    idx = gc._split_quoted_thread(SANITIZED_REPLY_DRAFT)
    assert idx is not None
    assert SANITIZED_REPLY_DRAFT[idx:].startswith("<hr")


def test_split_quoted_thread_none_for_plain_draft():
    """A non-reply draft has no quoted thread and must not be carved up."""
    plain = '<html><body><div>Just a new email.</div></body></html>'
    assert gc._split_quoted_thread(plain) is None


def _patched_update(current_body, new_body):
    captured = {}

    def fake_patch(path, payload):
        captured["payload"] = payload
        return {}

    with patch.object(gc, "get_message_full",
                      return_value={"body": {"content": current_body}}), \
            patch.object(gc, "graph_patch", fake_patch):
        gc.update_draft("fake-id", new_body)
    return captured["payload"]["body"]["content"]


def test_update_draft_preserves_quoted_thread_after_sanitizing():
    """Revising a marker-less reply keeps divRplyFwdMsg and drops only the
    old reply text."""
    out = _patched_update(SANITIZED_REPLY_DRAFT, "Revised reply text.")
    assert "Revised reply text." in out
    assert "Original reply text." not in out
    assert 'id="divRplyFwdMsg"' in out
    assert "Quoted original message." in out


def test_update_draft_is_idempotent_across_repeated_edits():
    """Editing twice must not strand a second <hr> or duplicate the thread."""
    once = _patched_update(SANITIZED_REPLY_DRAFT, "First revision.")
    twice = _patched_update(once, "Second revision.")
    assert "First revision." not in twice
    assert "Second revision." in twice
    assert twice.count('id="divRplyFwdMsg"') == 1
    assert twice.count("<hr") == 1


def test_update_draft_plain_draft_replaced_wholesale():
    """With no quoted thread there is nothing to preserve — replace the body."""
    plain = '<html><body><div>Old text.</div></body></html>'
    out = _patched_update(plain, "New text.")
    assert out == "New text."


# --------------------------------------------------------------------------
# list_folder_messages — server-side newest-first listing (the list-mail verb)
#
# "What is newest in Sent Items?" previously meant either a mailbox-wide
# $search or pulling 60 days of the folder and sorting client-side (30s+ on
# sentitems, and repeated 2-minute timeouts in practice). These pin the
# single-GET contract: Graph applies the ordering AND the cap before it
# sends anything back.
# --------------------------------------------------------------------------


def _captured_list(**kwargs):
    """Run list_folder_messages against a stubbed graph_get, capturing the
    endpoint and query params it built."""
    seen = {"calls": 0}

    def fake_get(endpoint, params=None):
        seen["calls"] += 1
        seen["endpoint"] = endpoint
        seen["params"] = params
        return {"value": [{"id": "m1"}], "@odata.nextLink": "https://more"}

    with patch.object(gc, "graph_get", side_effect=fake_get):
        results, has_more = gc.list_folder_messages(**kwargs)
    return seen, results, has_more


def test_list_folder_messages_orders_and_caps_server_side():
    seen, results, has_more = _captured_list(folder="inbox", top=5)
    assert seen["endpoint"] == "/me/mailFolders/inbox/messages"
    assert seen["params"]["$orderby"] == "receivedDateTime desc"
    assert seen["params"]["$top"] == 5
    # same compact field set search_messages returns, so the two verbs'
    # JSON output is interchangeable
    assert seen["params"]["$select"] == gc.MAIL_FIELDS
    assert "$filter" not in seen["params"]
    assert results == [{"id": "m1"}]
    assert has_more is True


def test_list_folder_messages_sent_folder_orders_by_sent_time():
    seen, _, _ = _captured_list(folder="sentitems", top=25,
                                order_by="sentDateTime")
    assert seen["endpoint"] == "/me/mailFolders/sentitems/messages"
    assert seen["params"]["$orderby"] == "sentDateTime desc"


def test_list_folder_messages_since_filters_the_sorted_property():
    """--since filters the SAME property it sorts on. Graph rejects some
    $filter+$orderby mixes ('restriction or sort order is too complex'),
    and matching the properties is what keeps this one accepted."""
    seen, _, _ = _captured_list(folder="sentitems", top=10,
                                order_by="sentDateTime",
                                since="2026-07-01T00:00:00Z")
    assert seen["params"]["$filter"] == "sentDateTime ge 2026-07-01T00:00:00Z"
    assert seen["params"]["$orderby"] == "sentDateTime desc"


def test_list_folder_messages_does_not_paginate():
    """One GET even when Graph advertises a nextLink — the whole point is to
    stop pulling the folder. The extra pages surface as has_more instead."""
    seen, results, has_more = _captured_list(folder="sentitems", top=1)
    assert seen["calls"] == 1
    assert len(results) == 1
    assert has_more is True


# ---------------------------------------------------------------------------
# Body rendering: hard wraps vs breaks the author meant
# ---------------------------------------------------------------------------

def _breaks(html_out):
    import re
    return len(re.findall(r"(?i)<br\s*/?>", html_out))


def test_hard_wrapped_prose_is_rejoined():
    """Bodies arrive wrapped at a source column. A newline inside a
    paragraph must not become a <br>, or prose renders broken
    mid-sentence ("is now on<br>leave")."""
    body = ("One thing worth sorting before we meet: since Harper set "
            "your goals in October and is now on\nleave, we should make "
            "sure the evaluation routes to me.")
    out = gc._render_reply_html(body, "Text")
    assert _breaks(out) == 0
    assert "is now on leave" in out


def test_short_lines_keep_the_break_the_author_wanted():
    """A list of proposed times and a signature are not wrapped prose."""
    for body, want in (
        ("Wed Aug 19, 10:30-11:15am\nThu Aug 20, 9:00-9:45am\n"
         "Thu Aug 20, 1:00-1:45pm", 2),
        ("Thanks,\nKit", 1),
        ("Kit Calloway\nDepartment of Computer Science\n"
         "Example University", 2),
    ):
        assert _breaks(gc._render_reply_html(body, "Text")) == want, body


def test_a_list_under_a_long_lead_in_is_not_swallowed():
    """The lead-in is long enough to look wrapped; the colon and the
    bullet markers are what stop the join."""
    body = ("Here is the plan and it is long enough to pass the join "
            "threshold easily:\n- first item\n- second item")
    out = gc._render_reply_html(body, "Text")
    assert _breaks(out) == 2


def test_authored_br_tags_do_not_render_as_escaped_text():
    """The reported symptom: a body written with <br> separators came out
    as visible &lt;br&gt; to the recipient."""
    out = gc._render_reply_html("Hi Vale,<br><br>Great seeing you.", "Text")
    assert "&lt;br&gt;" not in out
    assert "<div>Hi Vale,</div>" in out


def test_a_bare_angle_bracket_is_still_escaped():
    out = gc._render_reply_html("Rates where a < b and b > c hold.", "Text")
    assert "&lt; b" in out and "&gt; c" in out


def test_paragraph_gaps_survive_unwrapping():
    body = ("First paragraph that is long enough to be a candidate for "
            "joining across lines.\n\nSecond paragraph.")
    out = gc._render_reply_html(body, "Text")
    assert "<div><br></div>" in out


# --------------------------------------------------------------------------
# Weekday/date consistency + ASCII dash
# --------------------------------------------------------------------------
import datetime as _dt

def test_weekday_date_mismatch_flags_wrong_weekday():
    """A stated weekday that does not match the date is flagged."""
    ms = gc.weekday_date_mismatches("Let's do Thursday Aug 21 at 1pm.",
                                    today=_dt.date(2026, 8, 19))
    assert len(ms) == 1
    assert ms[0]["stated"] == "Thursday" and ms[0]["actual"] == "Friday"
    assert "Aug 21 2026" == ms[0]["date"]


def test_weekday_date_correct_is_clean():
    for text in ("Friday Aug 21 at 1pm", "Thursday Aug 20 works",
                 "Fri, August 21 at noon"):
        assert gc.weekday_date_mismatches(text, today=_dt.date(2026, 8, 19)) == [], \
            f"false positive on {text!r}"


def test_weekday_date_no_false_positive_on_ranges_or_bare_dates():
    for text in ("I work 9-5 Mon-Fri", "our Aug 21 sync", "see you in Aug",
                 "the --online flag"):
        assert gc.weekday_date_mismatches(text, today=_dt.date(2026, 8, 19)) == []


def test_weekday_date_resolves_across_year_boundary():
    """A December send naming an early-January weekday resolves to next year."""
    # Jan 2 2027 is a Saturday; naming it 'Friday' must flag.
    ms = gc.weekday_date_mismatches("Friday Jan 2",
                                    today=_dt.date(2026, 12, 30))
    assert ms and ms[0]["date"] == "Jan 2 2027"


def test_weekday_date_resolves_to_nearest_occurrence():
    """A bare month+day has no year; it resolves to the occurrence nearest
    the send date, which for any month+day is within ~6 months."""
    # From Aug 19 2026, "Aug 21" is this year's Friday; calling it Monday flags.
    ms = gc.weekday_date_mismatches("Monday Aug 21", today=_dt.date(2026, 8, 19))
    assert ms and ms[0]["date"] == "Aug 21 2026" and ms[0]["actual"] == "Friday"


# --------------------------------------------------------------------------
# The library gate. docs/SECURITY.md promises a second, independent gate
# under the CLI: "Even if one validation layer is somehow bypassed, the
# other still blocks the action." update_event consults it on every
# change, not only an attendee change: a pure retime is the ordinary
# reschedule, and Outlook mails it to every attendee.
# --------------------------------------------------------------------------

def _no_network():
    """Fail loudly if a blocked call still reaches Graph."""
    def boom(*a, **kw):
        raise AssertionError("a blocked call reached the network")
    return patch.multiple(gc, graph_patch=boom, graph_delete=boom,
                          graph_post=boom)


def test_update_event_retime_without_a_token_is_blocked():
    with _no_network():
        for kwargs in ({"start": _dt.datetime(2026, 9, 1, 9, 0)},
                       {"subject": "Renamed"},
                       {"location": "Zoom"},
                       {"body": "notes"},
                       {"start": _dt.datetime(2026, 9, 1, 9, 0),
                        "end": _dt.datetime(2026, 9, 1, 10, 0)}):
            r = gc.update_event("AAMkEVENT=", **kwargs)
            assert r["status"] == "blocked", (kwargs, r)


def test_update_event_forged_token_is_blocked():
    with _no_network():
        for token in ("pre-approved:anything", "pre-approved:", "cli-approved:x",
                      "", None, "whatever"):
            r = gc.update_event("AAMkEVENT=", start=_dt.datetime(2026, 9, 1, 9),
                                confirm_token=token)
            assert r["status"] == "blocked", (token, r)


def test_update_event_with_a_real_approval_proceeds():
    seen = {}

    def fake_patch(path, body):
        seen["path"], seen["body"] = path, body
        return {"id": "AAMkEVENT="}

    with patch.object(gc, "graph_patch", fake_patch):
        tok = gc.issue_inprocess_approval("update_event", "AAMkEVENT=")
        r = gc.update_event("AAMkEVENT=", start=_dt.datetime(2026, 9, 1, 9),
                            confirm_token=tok)
    assert seen["path"] == "/me/events/AAMkEVENT="
    assert "start" in seen["body"] and r.get("id") == "AAMkEVENT="


def test_update_event_approval_is_bound_to_the_event_and_one_time():
    with _no_network():
        # minted for THIS event only (and a wrong use burns it, by design)
        tok = gc.issue_inprocess_approval("update_event", "AAMkEVENT=")
        assert gc.update_event("AAMkOTHER=", subject="x",
                               confirm_token=tok)["status"] == "blocked"
        # and for THIS action only
        tok = gc.issue_inprocess_approval("update_event", "AAMkEVENT=")
        assert gc.delete_event("AAMkEVENT=", confirm_token=tok)["status"] == "blocked"
    tok = gc.issue_inprocess_approval("update_event", "AAMkEVENT=")
    with patch.object(gc, "graph_patch", lambda *a, **k: {"id": "ok"}):
        assert gc.update_event("AAMkEVENT=", subject="x",
                               confirm_token=tok).get("id") == "ok"
    with _no_network():   # consumed by that first use
        assert gc.update_event("AAMkEVENT=", subject="x",
                               confirm_token=tok)["status"] == "blocked"


def test_create_and_delete_event_and_send_are_gated_unconditionally():
    """The other three never had the conditional-gate bug; pin it."""
    with _no_network():
        assert gc.create_event("Hold", _dt.datetime(2026, 9, 1, 9),
                               _dt.datetime(2026, 9, 1, 10))["status"] == "blocked"
        assert gc.create_event("Hold", _dt.datetime(2026, 9, 1, 9),
                               _dt.datetime(2026, 9, 1, 10),
                               attendees=[])["status"] == "blocked"
        assert gc.delete_event("AAMkEVENT=")["status"] == "blocked"
        assert gc.send_draft("AAMkDRAFT=")["status"] == "blocked"
        assert gc.send_draft("AAMkDRAFT=",
                             confirm_token="pre-approved:x")["status"] == "blocked"



# --------------------------------------------------------------------------
# MSAL token cache persistence
# --------------------------------------------------------------------------
# Concurrent truncate-and-write leaves msal_token_cache.json as a complete
# object plus trailing garbage, which every import of graph_client then
# raises on. The loader keeps the first complete object and rewrites the
# file; the writer is atomic (temp file + rename) under an exclusive lock.

import json as _json
import subprocess as _subprocess


def _cache_sandbox():
    d = Path(tempfile.mkdtemp(prefix="ea-test-msal-"))
    (d / "auth").mkdir()
    gc.CACHE_FILE = d / "auth" / "msal_token_cache.json"
    return d


_CACHE_DOC = {"AccessToken": {}, "Account": {}, "IdToken": {},
              "RefreshToken": {"k": {"secret": "r", "credential_type": "RefreshToken"}},
              "AppMetadata": {}}


def cachetest_load_keeps_the_first_object_and_rewrites_trailing_bytes():
    _cache_sandbox()
    good = _json.dumps(_CACHE_DOC)
    gc.CACHE_FILE.write_text(good + "}")          # the live corruption
    cache = gc._load_cache()
    assert cache.find("RefreshToken"), "the cached refresh token was lost"
    assert _json.loads(gc.CACHE_FILE.read_text()) == _CACHE_DOC  # rewritten clean
    gc.CACHE_FILE.write_text("  " + good + "\n{\"half\": ")  # a torn second write
    assert gc._load_cache().find("RefreshToken")
    assert _json.loads(gc.CACHE_FILE.read_text()) == _CACHE_DOC


def cachetest_load_still_fails_loudly_when_no_object_exists():
    _cache_sandbox()
    gc.CACHE_FILE.write_text("not json at all")
    try:
        gc._load_cache()
    except _json.JSONDecodeError:
        return
    raise AssertionError("a cache with no JSON object must not load as empty")


def cachetest_save_is_atomic_and_leaves_no_temp_files():
    _cache_sandbox()
    cache = gc._load_cache()
    cache.deserialize(_json.dumps(_CACHE_DOC))
    cache.has_state_changed = True
    gc._save_cache(cache)
    assert _json.loads(gc.CACHE_FILE.read_text())["RefreshToken"]
    assert oct(gc.CACHE_FILE.stat().st_mode & 0o777) == "0o660"
    leftovers = [p.name for p in gc.CACHE_FILE.parent.iterdir()
                 if p.name.startswith("msal_token_cache.json.")
                 and not p.name.endswith(".lock")]
    assert leftovers == [], leftovers
    assert (gc.CACHE_FILE.parent / "msal_token_cache.lock").exists()


def cachetest_concurrent_saves_never_corrupt_the_file():
    """Twelve processes, each writing a cache of a different length,
    all at once: the file is always exactly one of them."""
    d = _cache_sandbox()
    script = (
        "import os, sys, json\n"
        f"os.environ['EA_DIR'] = {str(d)!r}\n"
        f"sys.path.insert(0, {str(Path(__file__).resolve().parent.parent / 'preprocessor')!r})\n"
        "import graph_client as gc\n"
        f"gc.CACHE_FILE = __import__('pathlib').Path({str(gc.CACHE_FILE)!r})\n"
        "n = int(sys.argv[1])\n"
        "doc = {'AccessToken': {}, 'Account': {}, 'IdToken': {}, 'AppMetadata': {},\n"
        "       'RefreshToken': {'k': {'secret': 'x' * (50 * n), 'credential_type': 'RefreshToken'}}}\n"
        "for _ in range(20):\n"
        "    gc._write_cache_file(json.dumps(doc))\n"
    )
    procs = [_subprocess.Popen([sys.executable, "-c", script, str(i)],
                               stdout=_subprocess.PIPE, stderr=_subprocess.PIPE)
             for i in range(12)]
    for p in procs:
        _, err = p.communicate(timeout=120)
        assert p.returncode == 0, err.decode()[-400:]
    doc = _json.loads(gc.CACHE_FILE.read_text())   # would raise on interleaving
    assert len(doc["RefreshToken"]["k"]["secret"]) % 50 == 0



# --------------------------------------------------------------------------
# EA holds on Outlook: created tentative and tagged, released by clear_hold
# without a token, and clear_hold refuses everything that is not such a hold.
# --------------------------------------------------------------------------

def _hold_event(**over):
    ev = {"id": "AAMkHOLD=", "subject": "HOLD: Advising / Tam",
          "showAs": "tentative", "categories": [gc.EA_HOLD_CATEGORY],
          "attendees": [], "isOrganizer": True,
          "start": {"dateTime": "2026-09-22T10:00:00.0000000"}}
    ev.update(over)
    return ev


def test_create_event_hold_is_tentative_and_tagged():
    seen = {}

    def fake_post(path, body):
        seen["path"], seen["body"] = path, body
        return {"id": "AAMkNEW="}

    with patch.object(gc, "graph_post", fake_post):
        tok = gc.issue_inprocess_approval("event", "HOLD: x")
        gc.create_event("HOLD: x", _dt.datetime(2026, 9, 22, 10), _dt.datetime(2026, 9, 22, 10, 30),
                        confirm_token=tok, hold=True)
    assert seen["body"]["showAs"] == "tentative"
    assert seen["body"]["categories"] == [gc.EA_HOLD_CATEGORY]
    # a plain event keeps the busy default and no tag
    with patch.object(gc, "graph_post", fake_post):
        tok = gc.issue_inprocess_approval("event", "Real meeting")
        gc.create_event("Real meeting", _dt.datetime(2026, 9, 22, 10), _dt.datetime(2026, 9, 22, 11),
                        confirm_token=tok)
    assert seen["body"]["showAs"] == "busy" and "categories" not in seen["body"]
    # a hold never carries attendees
    with _no_network():
        tok = gc.issue_inprocess_approval("event", "HOLD: y")
        r = gc.create_event("HOLD: y", _dt.datetime(2026, 9, 22, 10), _dt.datetime(2026, 9, 22, 11),
                            attendees=["flo.thornbury67@example.org"], confirm_token=tok, hold=True)
        assert r["status"] == "blocked"


def test_create_event_online_writes_the_institutions_join_block():
    """An online event carries 'Join <Platform> meeting' and the link from
    institution.video(); the legacy zoom_link is only the fallback, and
    a placeholder location becomes the link. isOnlineMeeting stays false."""
    import institution
    seen = {}

    def fake_post(path, body):
        seen["body"] = body
        return {"id": "AAMkNEW="}

    with patch.object(gc, "graph_post", fake_post), \
         patch.object(institution, "video", lambda: {"platform": "Microsoft Teams", "link": "https://teams.example/me"}), \
         patch.object(institution, "video_meeting_id", lambda: "222 333"):
        tok = gc.issue_inprocess_approval("event", "Sync")
        gc.create_event("Sync", _dt.datetime(2026, 9, 22, 10), _dt.datetime(2026, 9, 22, 11),
                        attendees=["flo.thornbury67@example.org"], body="Agenda", location="Zoom",
                        is_online=True, confirm_token=tok)
    body = seen["body"]
    assert body["body"]["content"] == "Agenda\n\nJoin Microsoft Teams meeting\nhttps://teams.example/me\nMeeting ID: 222 333\n"
    assert "Zoom" not in body["body"]["content"]
    assert body["location"]["displayName"] == "https://teams.example/me"
    assert body["isOnlineMeeting"] is False
    # no link configured: no join block, no location invented
    with patch.object(gc, "graph_post", fake_post), \
         patch.object(institution, "video", lambda: {"platform": "", "link": ""}):
        tok = gc.issue_inprocess_approval("event", "Sync")
        gc.create_event("Sync", _dt.datetime(2026, 9, 22, 10), _dt.datetime(2026, 9, 22, 11),
                        is_online=True, confirm_token=tok)
    assert seen["body"]["body"]["content"] == "" and "location" not in seen["body"]


def test_preflight_teams_link_check_follows_the_configured_platform():
    """A Teams link in the new text is an error only when the install
    names another platform; with no platform known, nothing says Teams
    is wrong."""
    body = "Join here: https://teams.microsoft.com/l/meetup-join/abc\n\nKit\n"
    with patch.object(gc, "get_message_full", return_value=_fake_draft(body)), \
         patch.object(gc, "_video_platform", lambda: "Zoom"):
        errs = gc.preflight_check("fake-id")["errors"]
    assert any("Teams link" in e and "uses Zoom" in e for e in errs)
    with patch.object(gc, "get_message_full", return_value=_fake_draft(body)), \
         patch.object(gc, "_video_platform", lambda: ""):
        assert not any("Teams" in e for e in gc.preflight_check("fake-id")["errors"])
    with patch.object(gc, "get_message_full", return_value=_fake_draft(body)), \
         patch.object(gc, "_video_platform", lambda: "Microsoft Teams"):
        assert not any("Teams" in e for e in gc.preflight_check("fake-id")["errors"])


def test_the_unused_msal_refresh_path_is_gone():
    assert not hasattr(gc, "_authenticate_msal_tokens")


def test_clear_hold_deletes_only_a_tagged_tentative_attendee_less_hold():
    deleted = []
    with patch.object(gc, "get_event", lambda eid: _hold_event()), \
         patch.object(gc, "graph_delete", lambda path: deleted.append(path)):
        r = gc.clear_hold("AAMkHOLD=")
    assert r["status"] == "hold_cleared" and deleted == ["/me/events/AAMkHOLD="]
    assert r["start"] == "2026-09-22T10:00:00"


def test_clear_hold_refuses_everything_else_and_touches_nothing():
    cases = {
        "untagged": _hold_event(categories=[]),
        "other tag": _hold_event(categories=["Blue category"]),
        "busy": _hold_event(showAs="busy"),
        "free": _hold_event(showAs="free"),
        "attendees": _hold_event(attendees=[{"emailAddress": {"address": "harper.fairbanks70@example.edu"}}]),
        "not organiser": _hold_event(isOrganizer=False),
        "organiser not said": {k: v for k, v in _hold_event().items() if k != "isOrganizer"},
    }
    for label, ev in cases.items():
        with patch.object(gc, "get_event", lambda eid, ev=ev: ev), _no_network():
            r = gc.clear_hold("AAMkX=")
        assert r["status"] == "refused", (label, r)
        assert r["reason"] and "pending-event-delete" in r["hint"], label
    # tag match is case-insensitive
    with patch.object(gc, "get_event", lambda eid: _hold_event(categories=["ea hold"])), \
         patch.object(gc, "graph_delete", lambda path: None):
        assert gc.clear_hold("AAMkHOLD=")["status"] == "hold_cleared"


def test_delete_event_still_needs_a_token_even_for_a_hold():
    """clear_hold is the only un-gated release; the general verb is unchanged."""
    with _no_network():
        assert gc.delete_event("AAMkHOLD=")["status"] == "blocked"


# --------------------------------------------------------------------------
# resolve_folder: a folder the user made is found by its own name
# --------------------------------------------------------------------------

_ADMISSIONS_ID = "AAMkAQQQQQQQQQQQQQAQQQQQAdmissionsFolderIdQQQQQQQQQQQQ="


def _folders(path, params=None):
    tree = {"/me/mailFolders": [{"id": "AAMkInboxId", "displayName": "Inbox", "childFolderCount": 1},
                                {"id": _ADMISSIONS_ID, "displayName": "admissions-inquiries"},
                                {"id": "AAMkResearchId", "displayName": "Research", "childFolderCount": 1},
                                {"id": "AAMkOtherId", "displayName": "Other", "childFolderCount": 1}],
            "/me/mailFolders/AAMkInboxId/childFolders": [{"id": "AAMkChildId", "displayName": "Receipts"}],
            "/me/mailFolders/AAMkResearchId/childFolders": [
                {"id": "AAMkCandId", "displayName": "Phd_Candidates", "childFolderCount": 1}],
            "/me/mailFolders/AAMkCandId/childFolders": [{"id": "AAMkNotesA", "displayName": "Notes"}],
            "/me/mailFolders/AAMkOtherId/childFolders": [{"id": "AAMkNotesB", "displayName": "Notes"}]}
    return {"value": tree[path]}


def test_resolve_folder_passes_well_known_names_and_ids():
    gc._FOLDER_IDS.clear()
    with patch.object(gc, "graph_get", side_effect=AssertionError("no lookup")):
        assert gc.resolve_folder("inbox") == "inbox"
        assert gc.resolve_folder("SentItems") == "sentitems"
        assert gc.resolve_folder(_ADMISSIONS_ID) == _ADMISSIONS_ID


def test_resolve_folder_finds_a_user_folder_by_name():
    gc._FOLDER_IDS.clear()
    calls = []
    with patch.object(gc, "graph_get", lambda p, params=None: calls.append(p) or _folders(p)):
        assert gc.resolve_folder("admissions-inquiries") == _ADMISSIONS_ID
        assert gc.resolve_folder("Admissions-Inquiries") == _ADMISSIONS_ID
        assert gc.resolve_folder("receipts") == "AAMkChildId"
    # the tree is walked once per new name; a case variant is the same name
    assert calls.count("/me/mailFolders") == 2, calls


def test_resolve_folder_finds_a_folder_at_any_depth_or_by_its_path():
    gc._FOLDER_IDS.clear()
    with patch.object(gc, "graph_get", lambda p, params=None: _folders(p)):
        assert gc.resolve_folder("PhD_Candidates") == "AAMkCandId"
        assert gc.resolve_folder("research/phd_candidates") == "AAMkCandId"
        assert gc.resolve_folder("Research/Phd_Candidates/Notes") == "AAMkNotesA"
        try:
            gc.resolve_folder("Notes")
        except LookupError as e:
            assert "Research/Phd_Candidates/Notes" in str(e) and "Other/Notes" in str(e)
        else:
            raise AssertionError("two folders named Notes resolved to one")


def test_resolve_folder_refuses_an_unknown_name():
    gc._FOLDER_IDS.clear()
    with patch.object(gc, "graph_get", lambda p, params=None: _folders(p)):
        try:
            gc.resolve_folder("no-such-folder")
        except LookupError as e:
            assert "no-such-folder" in str(e)
        else:
            raise AssertionError("an unknown folder name resolved")


def test_list_folder_messages_uses_the_folder_id():
    gc._FOLDER_IDS.clear()
    paths = []

    def fake(path, params=None):
        paths.append(path)
        return _folders(path) if not path.endswith("/messages") else {"value": []}
    with patch.object(gc, "graph_get", fake):
        gc.list_folder_messages("admissions-inquiries", top=5)
    assert paths[-1] == f"/me/mailFolders/{_ADMISSIONS_ID}/messages", paths


# ── tokens from owner plugins' auth sources ───────────────────────────

def _jwt(claims: dict) -> str:
    import base64
    body = base64.urlsafe_b64encode(_json.dumps(claims).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJub25lIn0.{body}.sig"


def _with_sources(tokens, body):
    """Run body(calls) with the plugin auth sources yielding `tokens`, no
    MSAL account and an empty source-token slot."""
    import plugin_registry
    calls = []

    def fake(kind="graph", lock=None):
        calls.append(kind)
        yield from tokens
    with patch.object(plugin_registry, "owner_auth_tokens", fake), \
         patch.object(gc, "_SOURCE_TOKEN", {"token": None, "exp": 0.0}), \
         patch.object(gc._app, "get_accounts", lambda: []):
        body(calls)


def test_a_source_token_is_used_after_msal_and_kept_until_it_expires():
    tok = _jwt({"aud": "https://graph.microsoft.com", "exp": int(time.time()) + 3600})

    def body(calls):
        assert gc.authenticate() == tok
        assert gc.authenticate() == tok and gc._api_base() == gc.GRAPH_URL
        assert calls == ["graph"]                  # asked once, then kept
    _with_sources([tok], body)


def test_an_outlook_audience_source_token_routes_to_outlook_rest():
    tok = _jwt({"aud": gc.OUTLOOK_AUDIENCE, "exp": int(time.time()) + 3600})
    _with_sources([tok], lambda calls: _assert_eq(gc._api_base(), gc.OUTLOOK_REST_URL))


def test_a_source_token_without_an_expiry_is_not_kept():
    def body(calls):
        assert gc._source_token() == "opaque-token"
        assert gc._source_token() == "opaque-token"
        assert calls == ["graph", "graph"]
        assert gc._api_base() == gc.GRAPH_URL
    _with_sources(["opaque-token"], body)


def test_no_source_token_and_no_terminal_raises_with_the_next_step():
    def body(calls):
        with patch.object(sys.stdin, "isatty", lambda: False):
            try:
                gc.authenticate()
            except RuntimeError as e:
                assert "owner plugin" in str(e)
            else:
                raise AssertionError("authenticate() returned without a token")
    _with_sources([], body)


def _assert_eq(a, b):
    assert a == b, (a, b)


# --------------------------------------------------------------------------
# Event times. Graph reads dateTime in the timeZone sent beside it, so an
# offset-bearing time is sent as the user's local time with no offset: the
# instant the confirmation card showed.
# --------------------------------------------------------------------------

def test_an_offset_time_is_sent_as_the_users_local_time():
    from zoneinfo import ZoneInfo
    import user_tz
    east = _dt.timezone(_dt.timedelta(hours=-4))
    seen = {}
    start = _dt.datetime(2026, 10, 20, 9, 0, tzinfo=east)      # 9:00 EDT is 7:00 MDT
    end = _dt.datetime(2026, 10, 20, 10, 0, tzinfo=east)
    with patch.object(user_tz, "zone", lambda *a: ZoneInfo("America/Denver")), \
            patch.object(gc, "_get_user_timezone", lambda: "America/Denver"), \
            patch.object(gc, "graph_post", lambda p, b: seen.update(post=b) or {"id": "c"}), \
            patch.object(gc, "graph_patch", lambda p, b: seen.update(patch=b) or {"id": "u"}):
        gc.create_event("Sync", start, end,
                        confirm_token=gc.issue_inprocess_approval("event", "Sync"))
        gc.update_event("AAMkEV=", start=start, end=end,
                        confirm_token=gc.issue_inprocess_approval("update_event", "AAMkEV="))
        utc = _dt.datetime(2026, 10, 20, 15, 30, tzinfo=_dt.timezone.utc)
        assert gc.to_user_local(utc) == _dt.datetime(2026, 10, 20, 9, 30)
    for body in (seen["post"], seen["patch"]):
        assert body["start"] == {"dateTime": "2026-10-20T07:00:00", "timeZone": "America/Denver"}
        assert body["end"] == {"dateTime": "2026-10-20T08:00:00", "timeZone": "America/Denver"}
    naive = _dt.datetime(2026, 10, 20, 9, 0)
    assert gc.to_user_local(naive) is naive


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    tests += [v for k, v in sorted(globals().items()) if k.startswith("cachetest_")]
    passed = failed = 0
    for test in tests:
        try:
            test()
            print(f"  \u2705 {test.__name__}")
            passed += 1
        except Exception as e:
            print(f"  \u274c {test.__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\n  {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)

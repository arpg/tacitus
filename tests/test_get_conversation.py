#!/usr/bin/env python3
"""get-conversation, and the two smaller reads it grew out of.

Two reads that keep a Slack reply from costing dozens of Graph calls:

  * Reconstructing ONE thread by search-mail plus a get-message per hit
    pays for hits that are not even in the thread, because search-mail
    matches loosely. `get-conversation` is one GET on the exact join
    key.
  * get-message reads ccRecipients as well as toRecipients, so a caller
    who set a CC and read the draft back does not see `cc: []`.
"""

import os
import sys
import tempfile
import traceback
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

# Never touch production state (graph_client binds EA_DIR at import).
os.environ["EA_DIR"] = tempfile.mkdtemp(prefix="ea-test-conv-")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "preprocessor"))

import graph_actions as ga
import graph_client as gc
import mail_mediation as mm


def _msg(mid, sender, when, text, *, to=(), cc=(), draft=False, subject="Re: T"):
    return {
        "id": mid, "subject": subject,
        "from": {"emailAddress": {"address": sender, "name": sender}},
        "toRecipients": [{"emailAddress": {"address": a}} for a in to],
        "ccRecipients": [{"emailAddress": {"address": a}} for a in cc],
        "receivedDateTime": when, "isDraft": draft, "hasAttachments": False,
        "body": {"contentType": "text", "content": text},
        "conversationId": "AAQkCONV=",
    }


# ── the body extractors ────────────────────────────────────────────────

def test_html_body_becomes_text_and_keeps_urls():
    html = ('<html><style>p{color:red}</style><body><p>Hi Kit,</p>'
            '<p>See <a href="https://x.edu/rfp">the RFP</a>.</p>'
            '<script>evil()</script></body></html>')
    text = ga._body_to_text(html, "html")
    assert "color:red" not in text and "evil()" not in text
    assert "Hi Kit," in text
    assert "the RFP [https://x.edu/rfp]" in text


def test_plain_body_is_returned_untouched():
    assert ga._body_to_text("Hi Kit,\n\nYes.", "text") == "Hi Kit,\n\nYes."


def test_quoted_history_is_cut_at_the_first_divider():
    """The whole token argument for get-conversation: message N of a thread
    quotes messages 1..N-1, which the response already carries as their own
    entries."""
    body = ("Works for me.\n\n"
            "________________________________\n"
            "From: Gale Hargrove\nSent: Monday\nSubject: Overflow\n\n"
            "Can we talk Thursday?")
    own = ga._own_text(body)
    assert own == "Works for me."
    assert "Gale Hargrove" not in own


def test_outlook_html_quote_header_is_cut():
    """The common case, and the one a name-on-the-same-line regex misses.
    Once the tags are gone, an Outlook HTML quote header puts every label
    on its own line: "From:\\nGus <...>\\nSent:\\n...". Found on the
    live Overflow thread, where 4 of 10 messages still carried
    their quoted history after the first pass."""
    body = ("Let's take a look at the 111 space.\n"
            "Kit\n"
            "From:\n"
            "Gus Rasmussen <lane.nakashima17@example.edu>\n"
            "Sent:\n"
            "Thursday, July 2, 2026 2:53 PM\n"
            "To:\n"
            "Kit Calloway\n")
    own = ga._own_text(body)
    assert own == "Let's take a look at the 111 space.\nKit"
    assert "Gus" not in own


def test_on_x_wrote_divider_is_also_cut():
    body = "Sounds good.\n\nOn Mon, Aug 24, 2026 at 9:02 AM Gale wrote:\n> old"
    assert ga._own_text(body) == "Sounds good."


def test_own_text_leaves_an_unquoted_body_alone():
    assert ga._own_text("Just the one paragraph.") == "Just the one paragraph."


# ── graph_client.get_conversation_messages ─────────────────────────────

def test_conversation_fetch_is_one_call_filtered_on_the_join_key():
    seen = {}

    def fake_get(endpoint, params=None, **kw):
        seen["endpoint"], seen["params"] = endpoint, params
        return {"value": [
            _msg("m2", "hollis.quillon51@example.edu", "2026-08-20T18:00:00Z", "second"),
            _msg("m1", "avery.foxworth36@example.edu", "2026-08-19T18:00:00Z", "first"),
        ]}

    with patch.object(gc, "graph_get", fake_get):
        msgs, truncated = gc.get_conversation_messages("AAQkCONV=")
    assert seen["endpoint"] == "/me/messages"
    assert seen["params"]["$filter"] == "conversationId eq 'AAQkCONV='"
    assert truncated is False
    # Oldest-first: a thread reads top to bottom.
    assert [m["id"] for m in msgs] == ["m1", "m2"]


def test_conversation_fetch_drops_drafts_unless_asked():
    payload = {"value": [
        _msg("m1", "hollis.quillon51@example.edu", "2026-08-19T18:00:00Z", "first"),
        _msg("d1", "avery.foxworth36@example.edu", "2026-08-21T18:00:00Z", "unsent",
             draft=True),
    ]}
    with patch.object(gc, "graph_get", lambda *a, **k: payload):
        assert [m["id"] for m in gc.get_conversation_messages("C")[0]] == ["m1"]
        keep = gc.get_conversation_messages("C", include_drafts=True)[0]
        assert [m["id"] for m in keep] == ["m1", "d1"]


def test_single_quotes_in_a_conversation_id_are_odata_escaped():
    """A stray quote would otherwise terminate the literal and 400."""
    seen = {}

    def fake_get(endpoint, params=None, **kw):
        seen["f"] = params["$filter"]
        return {"value": []}

    with patch.object(gc, "graph_get", fake_get):
        gc.get_conversation_messages("AA'QK")
    assert seen["f"] == "conversationId eq 'AA''QK'"


def test_more_messages_than_top_reports_truncation():
    with patch.object(gc, "graph_get",
                      lambda *a, **k: {"value": [], "@odata.nextLink": "u"}):
        assert gc.get_conversation_messages("C")[1] is True


def test_top_is_clamped_to_a_sane_graph_page():
    seen = {}

    def fake_get(endpoint, params=None, **kw):
        seen["top"] = params["$top"]
        return {"value": []}

    with patch.object(gc, "graph_get", fake_get):
        gc.get_conversation_messages("C", top=5000)
        assert seen["top"] == 100
        gc.get_conversation_messages("C", top=0)
        assert seen["top"] == 1


# ── the CLI verb ───────────────────────────────────────────────────────

def _conversation_output(**overrides):
    import io
    import json
    args = SimpleNamespace(conversation_id="AAQkCONV=", top=50, max_chars=2000,
                           full=False, include_drafts=False)
    for k, v in overrides.items():
        setattr(args, k, v)
    buf = io.StringIO()
    with patch("sys.stdout", buf):
        ga.cmd_get_conversation(args)
    return json.loads(buf.getvalue())


def test_get_conversation_prints_the_whole_thread_in_order():
    thread = [
        _msg("m1", "hollis.quillon51@example.edu", "2026-08-19T18:00:00Z",
             "Can we talk Thursday?", to=["avery.foxworth36@example.edu"],
             cc=["hale.zielinski59@example.edu"]),
        _msg("m2", "avery.foxworth36@example.edu", "2026-08-20T18:00:00Z",
             "Works for me.\n\nFrom: Gale Hargrove\nCan we talk Thursday?",
             to=["hollis.quillon51@example.edu"]),
    ]
    with patch.object(ga.graph, "get_conversation_messages",
                      lambda *a, **k: (thread, False)):
        out = _conversation_output()
    assert out["message_count"] == 2
    assert [m["id"] for m in out["messages"]] == ["m1", "m2"]
    assert out["messages"][0]["cc"] == ["hale.zielinski59@example.edu"]
    # Every participant, so the caller never needs a second read to answer
    # "who is on this thread?"
    assert set(out["participants"]) == {
        "hollis.quillon51@example.edu", "avery.foxworth36@example.edu", "hale.zielinski59@example.edu"}
    # Quoted history stripped by default.
    assert out["messages"][1]["text"] == "Works for me."


def test_full_flag_keeps_the_quoted_history():
    thread = [_msg("m2", "avery.foxworth36@example.edu", "2026-08-20T18:00:00Z",
                   "Works for me.\n\nFrom: Gale Hargrove\nCan we talk?")]
    with patch.object(ga.graph, "get_conversation_messages",
                      lambda *a, **k: (thread, False)):
        out = _conversation_output(full=True)
    assert "Gale Hargrove" in out["messages"][0]["text"]
    assert "_note" not in out


def test_max_chars_bounds_each_message():
    thread = [_msg("m1", "harper.juniper70@example.edu", "2026-08-19T18:00:00Z", "x" * 9000)]
    with patch.object(ga.graph, "get_conversation_messages",
                      lambda *a, **k: (thread, False)):
        out = _conversation_output(max_chars=100)
    assert len(out["messages"][0]["text"]) == 100


def test_empty_conversation_says_so_instead_of_looking_like_a_read_thread():
    with patch.object(ga.graph, "get_conversation_messages",
                      lambda *a, **k: ([], False)):
        out = _conversation_output()
    assert out["message_count"] == 0 and "hint" in out


def test_graph_failure_is_reported_not_raised():
    def boom(*a, **k):
        raise RuntimeError("Graph 503")

    with patch.object(ga.graph, "get_conversation_messages", boom):
        out = _conversation_output()
    assert "Graph 503" in out["error"]


def test_truncation_is_announced():
    thread = [_msg("m1", "harper.juniper70@example.edu", "2026-08-19T18:00:00Z", "hi")]
    with patch.object(ga.graph, "get_conversation_messages",
                      lambda *a, **k: (thread, True)):
        out = _conversation_output(top=1)
    assert "--top" in out["truncated"]


# ── get-message CC, the four wasted calls ──────────────────────────────

def test_get_message_reports_cc():
    import io
    import json
    msg = _msg("m1", "hollis.quillon51@example.edu", "2026-08-19T18:00:00Z", "hi",
               to=["avery.foxworth36@example.edu"], cc=["casey.underhill83@example.edu"])
    buf = io.StringIO()
    with patch.object(ga.graph, "get_message_full", lambda *a, **k: msg), \
            patch("sys.stdout", buf):
        ga.cmd_get_message(SimpleNamespace(message_id="m1", raw_html=False))
    out = json.loads(buf.getvalue())
    assert [r["address"] for r in out["cc"]] == ["casey.underhill83@example.edu"]
    assert [r["address"] for r in out["to"]] == ["avery.foxworth36@example.edu"]


def test_get_message_still_strips_html_bodies():
    import io
    import json
    msg = _msg("m1", "harper.juniper70@example.edu", "2026-08-19T18:00:00Z", "")
    msg["body"] = {"contentType": "html",
                   "content": "<html><body><p>Hi Kit,</p></body></html>"}
    buf = io.StringIO()
    with patch.object(ga.graph, "get_message_full", lambda *a, **k: msg), \
            patch("sys.stdout", buf):
        ga.cmd_get_message(SimpleNamespace(message_id="m1", raw_html=False))
    out = json.loads(buf.getvalue())
    assert out["body"] == "Hi Kit,"
    assert out["body_type"] == "text (extracted from html)"


# ── the join key on the two listing verbs ──────────────────────────────

def test_search_and_list_rows_carry_the_conversation_id():
    """Without this, a search hit -> its thread still costs a get-message."""
    import io
    import json
    rows = [_msg("m1", "harper.juniper70@example.edu", "2026-08-19T18:00:00Z", "hi")]
    for row in rows:
        row["bodyPreview"] = "hi"

    buf = io.StringIO()
    with patch.object(ga.graph, "search_messages", lambda *a, **k: (rows, False)), \
            patch("sys.stdout", buf):
        ga.cmd_search_mail(SimpleNamespace(query="overflow", top=10, folder=None))
    assert json.loads(buf.getvalue())[0]["conversation_id"] == "AAQkCONV="

    buf = io.StringIO()
    with patch.object(ga.graph, "list_folder_messages",
                      lambda *a, **k: (rows, False)), \
            patch("sys.stdout", buf):
        ga.cmd_list_mail(SimpleNamespace(folder="inbox", top=25, since=None))
    assert json.loads(buf.getvalue())[0]["conversation_id"] == "AAQkCONV="


def test_search_and_list_rows_flag_the_users_own_drafts():
    """"Finish the draft I wrote" needs the draft told apart from the
    thread's messages: a row from the user's address with isDraft set."""
    import io
    import json
    own = _msg("d1", "emerson.vasquez27@example.edu", "2026-08-19T18:00:00Z", "hi")
    own["isDraft"] = True
    other = _msg("m1", "harper.juniper70@example.edu", "2026-08-19T17:00:00Z", "hi")
    for row in (own, other):
        row["bodyPreview"] = "hi"
    for cmd, verb, kw in ((ga.cmd_search_mail, "search_messages", dict(query="x", top=10, folder=None)),
                          (ga.cmd_list_mail, "list_folder_messages", dict(folder="inbox", top=25, since=None))):
        buf = io.StringIO()
        with patch.object(ga.graph, verb, lambda *a, **k: ([own, other], False)), \
                patch.object(ga, "_user_addresses", lambda: {"emerson.vasquez27@example.edu"}), \
                patch("sys.stdout", buf):
            cmd(SimpleNamespace(**kw))
        out = json.loads(buf.getvalue())
        assert (out[0]["is_draft"], out[0]["own"]) == (True, True)
        assert (out[1]["is_draft"], out[1]["own"]) == (False, False)


# ── a folder-scoped miss is not proof of absence ───────────────────────
# Archiving a conversation carries the user's sent copy out of Sent
# Items, so `search-mail --query X --folder sentitems` can return a bare
# `[]` for a reply that exists. The same query with no --folder finds it,
# so a scoped miss must say so rather than read as "never sent".

def _empty_folder_search(folder):
    import io
    import json
    buf = io.StringIO()
    with patch.object(ga.graph, "search_messages", lambda *a, **k: ([], False)), \
            patch("sys.stdout", buf):
        ga.cmd_search_mail(SimpleNamespace(query="Gala", top=10,
                                           folder=folder))
    return json.loads(buf.getvalue())


def test_folder_scoped_miss_says_it_is_not_evidence_of_absence():
    out = _empty_folder_search("sentitems")
    assert len(out) == 1, f"expected a lone explanatory note, got {out}"
    note = out[0]["_note"]
    assert "sentitems" in note
    assert "NOT evidence" in note
    # The note has to name the two recovery moves, or it is just a longer
    # way of printing nothing.
    assert "NO --folder" in note
    assert "get-conversation" in note


def test_a_folderless_miss_stays_bare():
    """Only folder scope carries the hidden caveat. A search across all
    folders that finds nothing really did look everywhere, and bolting the
    note onto it would train the reader to skip it."""
    import io
    import json
    buf = io.StringIO()
    with patch.object(ga.graph, "search_messages", lambda *a, **k: ([], False)), \
            patch("sys.stdout", buf):
        ga.cmd_search_mail(SimpleNamespace(query="Gala", top=10, folder=None))
    assert json.loads(buf.getvalue()) == []


def test_a_folder_search_with_hits_is_not_annotated():
    import io
    import json
    row = _msg("m1", "avery.foxworth36@example.edu", "2026-08-24T15:51:07Z", "hi",
               subject="RE: Gala Invitation for Sept 3")
    row["bodyPreview"] = "I am sorry, I cannot make the Sept 3 gala."
    buf = io.StringIO()
    with patch.object(ga.graph, "search_messages", lambda *a, **k: ([row], False)), \
            patch("sys.stdout", buf):
        ga.cmd_search_mail(SimpleNamespace(query="Gala", top=10,
                                           folder="sentitems"))
    out = json.loads(buf.getvalue())
    assert len(out) == 1 and "_note" not in out[0], out
    assert out[0]["conversation_id"] == "AAQkCONV="


# ── mediation ──────────────────────────────────────────────────────────

def test_get_conversation_is_a_safe_mediated_read():
    """It only reads mail. ea-bot must be able to run it under mediation;
    a verb missing from SAFE is refused, not forwarded."""
    assert mm.is_safe("graph_actions", "get-conversation")
    assert mm.is_allowed("graph_actions", "get-conversation",
                         ["get-conversation", "--conversation-id", "C"])
    assert not mm.is_gated("graph_actions", "get-conversation")


def test_every_mediated_verb_has_a_subparser():
    """A verb named in SAFE/GATED but missing from argparse is forwarded
    to the mediator and dies there in argparse with exit 2, which reads to
    the caller as a Graph failure rather than a typo.
    graph_actions builds its parser inline under __main__, so read the
    source rather than importing a parser that does not exist."""
    import re
    src = (Path(__file__).resolve().parent.parent
           / "preprocessor" / "graph_actions.py").read_text()
    verbs = set(re.findall(r"""sub\.add_parser\(\s*["']([a-z-]+)["']""", src))
    declared = mm.SAFE["graph_actions"] | mm.GATED["graph_actions"]
    assert "get-conversation" in verbs
    assert not (declared - verbs), f"declared but unparseable: {declared - verbs}"




def _capture(fn, args):
    """Run a CLI verb and return its (stdout, stderr)."""
    import io
    out, err = io.StringIO(), io.StringIO()
    with patch("sys.stdout", out), patch("sys.stderr", err):
        fn(args)
    return out.getvalue(), err.getvalue()


_ROW_KEYS = {"id", "subject", "from", "is_draft", "own", "received", "has_attachments",
             "conversation_id", "to", "cc", "preview"}



def test_truncated_listings_keep_every_row_the_same_shape():
    """A truncated listing must not append a {"_note": ...} object to the
    array it fills with messages, or a caller looping
    `for m in rows: m["received"]` dies on the last row. The array holds
    messages and nothing else."""
    import json
    rows = [_msg("m1", "harper.juniper70@example.edu", "2026-08-19T18:00:00Z", "hi")]
    rows[0]["bodyPreview"] = "hi"

    for fn, attr, args in (
        (ga.cmd_search_mail, "search_messages",
         SimpleNamespace(query="overflow", top=5, folder=None)),
        (ga.cmd_list_mail, "list_folder_messages",
         SimpleNamespace(folder="inbox", top=5, since=None)),
    ):
        with patch.object(ga.graph, attr, lambda *a, **k: (rows, True)):
            out, err = _capture(fn, args)
        payload = json.loads(out)
        assert "_note" not in out
        assert [set(r) for r in payload] == [_ROW_KEYS]
        for m in payload:               # the loop a note row would break
            assert m["received"] == "2026-08-19T18:00:00Z"
        # the pagination hint is still told to the reader, on stderr
        assert "--top 10" in err



def test_a_note_row_never_travels_with_message_rows():
    """The rule both listing verbs follow, written so the NEXT reason to add a
    note has to obey it too: a results array carries message rows, or it
    carries one note saying why it carries none. Never a mix.

    The other note these verbs emit is the zero-hit --folder warning: an
    empty scoped search is not evidence of absence, because archiving a
    thread moves the user's own sent copy out of sentitems. That note
    stands in an array with nothing else in it, so no caller meets it
    mid-loop."""
    import json
    rows = [_msg("m1", "harper.juniper70@example.edu", "2026-08-19T18:00:00Z", "hi")]
    rows[0]["bodyPreview"] = "hi"

    def check(payload):
        notes = [r for r in payload if set(r) != _ROW_KEYS]
        assert not notes or len(payload) == len(notes), (
            f"a non-message row rides with {len(payload) - len(notes)} "
            f"message rows: {notes}")

    for attr, value, args in (
        # a truncated hit list, the case where a note could mix in
        ("search_messages", (rows, True),
         SimpleNamespace(query="x", top=5, folder=None)),
        # a scoped search that found nothing: a note here is alone, so allowed
        ("search_messages", ([], False),
         SimpleNamespace(query="x", top=5, folder="sentitems")),
        ("search_messages", (rows, False),
         SimpleNamespace(query="hi", top=5, folder="sentitems")),
    ):
        with patch.object(ga.graph, attr, lambda *a, **k: value):
            out, _ = _capture(ga.cmd_search_mail, args)
        check(json.loads(out))



def test_untruncated_listings_say_nothing_on_stderr():
    import json
    rows = [_msg("m1", "harper.juniper70@example.edu", "2026-08-19T18:00:00Z", "hi")]
    rows[0]["bodyPreview"] = "hi"
    with patch.object(ga.graph, "search_messages", lambda *a, **k: (rows, False)):
        out, err = _capture(ga.cmd_search_mail,
                            SimpleNamespace(query="x", top=5, folder=None))
    assert len(json.loads(out)) == 1 and err == ""



def test_calendar_view_prints_exactly_one_json_document():
    """One object per run, whatever the range: pagination happens inside
    graph_client.graph_get_all, not by printing a page at a time.
    Concatenated JSON documents make json.loads raise "Extra data" and
    force the caller to raw_decode from each bracket."""
    import json
    events = [{"id": f"AAMk{i}", "subject": f"E{i}",
               "start": {"dateTime": "2026-08-25T16:00:00.0000000"},
               "end": {"dateTime": "2026-08-25T17:00:00.0000000"},
               "isAllDay": False, "location": {"displayName": "Zoom"},
               "organizer": {"emailAddress": {"name": "Gale"}},
               "attendees": [{}], "responseStatus": {"response": "accepted"},
               "seriesMasterId": "", "type": "singleInstance",
               "showAs": "busy"} for i in range(250)]
    with patch.object(ga.graph, "get_calendar_view", lambda *a, **k: events):
        out, err = _capture(ga.cmd_calendar_view,
                            SimpleNamespace(start=None, end=None, days=365))
    doc, end = json.JSONDecoder().raw_decode(out.lstrip())
    assert out.lstrip()[end:].strip() == "", "a second document follows"
    assert doc["count"] == 250 and err == ""
    # Graph's UTC times stay as they were; the same instant in the user's
    # zone rides beside them with its offset, and the document says so.
    e0 = doc["events"][0]
    assert e0["start"] == "2026-08-25T16:00:00" and e0["start_local"] == "2026-08-25T10:00:00-06:00"
    assert e0["end_local"] == "2026-08-25T11:00:00-06:00"
    assert "start/end are UTC" in doc["times"] and "America/Denver" in doc["times"]


# ── mediation ──────────────────────────────────────────────────────────

def test_a_folder_search_asks_graph_to_search_that_folder():
    """--folder runs Graph's $search on the folder itself (its whole
    history, bodies included), not a filter over its recent messages."""
    seen = []
    with patch.object(ga.graph, "graph_get",
                      lambda path, params=None: seen.append((path, params["$search"]))
                      or {"value": []}), \
            patch.object(ga.graph, "resolve_folder",
                         lambda name: "FOLDERID" if name == "Admissions" else name):
        ga.graph.search_messages("Gala", folder="Admissions")
        ga.graph.search_messages("Gala")
    assert seen == [("/me/mailFolders/FOLDERID/messages", '"Gala"'),
                    ("/me/messages", '"Gala"')]


# ── a throttled verb ends in one plain line ──────────────────────────

def _throttled_run(fn, args):
    """Run a verb the way graph_actions' __main__ does; (exit code,
    stdout, stderr)."""
    import contextlib
    import io
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            with ga._plain_throttle_exit():
                fn(args)
        except SystemExit as e:
            code = e.code
    return code, out.getvalue(), err.getvalue()


def _graph_throttled():
    import requests
    resp = requests.Response()
    resp.status_code = 429
    return gc.GraphThrottled(gc.THROTTLED_TEXT, response=resp, endpoint="/me/messages",
                             retry_after=5, retry_at=None)


def test_a_throttled_verb_exits_with_one_plain_line():
    """search-mail lets a Graph error through; a 429 (or an open breaker)
    ends the run with exit 75 and one plain line, never a traceback."""
    import retry_policy
    from time import time as _now

    def boom(*a, **k):
        raise _graph_throttled()
    with patch.object(ga.graph, "search_messages", boom):
        code, out, err = _throttled_run(ga.cmd_search_mail,
                                        SimpleNamespace(query="x", top=5, folder=None))
    assert code == ga.THROTTLED_EXIT == 75
    assert out == "" and err == gc.THROTTLED_TEXT + "\n"
    assert err == "Microsoft is limiting mail access right now. Try again in a few minutes.\n"

    def shut(*a, **k):
        raise retry_policy.CircuitOpen("graph-background circuit open (3 consecutive failures)",
                                       service="graph-background", reopens_at=_now() + 300)
    with patch.object(ga.graph, "search_messages", shut):
        code, out, err = _throttled_run(ga.cmd_search_mail,
                                        SimpleNamespace(query="x", top=5, folder=None))
    assert code == 75 and len(err.splitlines()) == 1
    assert err.startswith("Microsoft is limiting mail access right now. Try again in a few minutes (after ")
    assert "Traceback" not in err and "circuit" not in err


def test_any_other_error_still_raises():
    def boom(*a, **k):
        raise ValueError("not a throttle")
    with patch.object(ga.graph, "search_messages", boom):
        try:
            _throttled_run(ga.cmd_search_mail, SimpleNamespace(query="x", top=5, folder=None))
        except ValueError:
            return
    raise AssertionError("a non-throttle error was swallowed")


def test_the_cli_runs_every_verb_inside_the_plain_exit():
    """__main__ dispatches inside _plain_throttle_exit, defaults its lane to
    interactive and keeps the retry notes off stderr."""
    src = (Path(__file__).resolve().parent.parent / "preprocessor" / "graph_actions.py").read_text()
    main = src[src.rindex('if __name__ == "__main__":'):]
    assert "with _plain_throttle_exit():\n        args.func(args)" in main
    assert "graph.notes_to_file(" in main
    top = src[src.index('if __name__ == "__main__":'):src.index("import graph_client as graph")]
    assert "set_graph_lane(_retry_policy.INTERACTIVE, only_if_unset=True)" in top


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for test in tests:
        try:
            test()
            print(f"  ✅ {test.__name__}")
            passed += 1
        except Exception as e:
            print(f"  ❌ {test.__name__}: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\n  {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)

#!/usr/bin/env python3
"""Deferred sends must be reachable, and reachable ONLY through the button.

`schedule-send` demands a one-time `send` confirm token, so without a
:lock: Schedule button to mint one the verb can only ever print
{"status": "blocked"} while the */5 cron runs against an empty queue.
The button is the half that makes the feature reachable, and the only
half that may mint the token.

What this file pins, in the order the flow runs:

  1. STAGING     the LLM writes ~/.ea/state/pending-schedule-send.json, and
                 that file is claimed by the same atomic-rename protocol as
                 the calendar files -- exactly one consumer, no silent drop,
                 and it belongs to a sweeper so an out-of-band write still
                 surfaces buttons.
  2. THE BUTTON  only handle_approve_schedule_send reaches
                 _execute_schedule_send, and only _execute_schedule_send
                 invokes request-confirm + schedule-send.
  3. THE TOKEN   end to end for real: mint the one-time token, run
                 cmd_schedule_send, let it queue through scheduled_sends.py,
                 then let cmd_check accept the HMAC it signed.
  4. CANCEL      discards the request; nothing is queued and no token is minted.
  5. NO CLICK    staging the file alone queues nothing, and an entry nobody
                 signed is refused at send time.

Nothing here touches live state: EA_DIR is repointed at a sandbox before the
modules that read it are imported, and every test rebinds the module paths.
"""

import io
import json
import os
import re
import sys
import tempfile
import time
import traceback
import types
import uuid
from contextlib import contextmanager, redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "preprocessor"))
# The bot imports `preprocessor.<module>` from the repo root (as it runs).
sys.path.insert(1, str(ROOT))

import chat as _chat  # noqa: E402
from chat.memory_frontend import MemoryFrontend  # noqa: E402
from chat.slack_frontend import render_card  # noqa: E402

BOT_SRC = (ROOT / "ea-chat-bot.py").read_text()

_SANDBOX = tempfile.mkdtemp(prefix="ea-schedule-send-tests-")
os.environ["EA_DIR"] = _SANDBOX
# _post_slack is a no-op without these; keep it that way even if the runner
# has them exported.
os.environ.pop("SLACK_BOT_TOKEN", None)
os.environ.pop("EA_SLACK_CHANNEL_ID", None)

import scheduled_sends as S  # noqa: E402

DRAFT = "AAMkADScheduleDraftExample="
FUTURE = "2030-04-21T09:00"
PAST = "2020-01-01T00:00"
FP = "sha256:" + "ab" * 32       # the content the owner reviewed


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _fn_body(name: str) -> str:
    """Source of one top-level function, up to the next top-level def."""
    start = BOT_SRC.index(f"\ndef {name}(")
    end = BOT_SRC.index("\ndef ", start + 1)
    return BOT_SRC[start:end]


def _handler_body(action_id: str) -> str:
    start = BOT_SRC.index(f'@app.action("{action_id}")')
    end = BOT_SRC.index("\n" + "@app.action(", start + 1)
    return BOT_SRC[start:end]


def _exec_block(start_marker: str, end_marker: str, ns: dict) -> dict:
    start = BOT_SRC.index(start_marker)
    end = BOT_SRC.index(end_marker, start)
    exec(compile(BOT_SRC[start:end], "ea-chat-bot.py[block]", "exec"), ns)
    return ns


def _claim_ns(tmp: Path) -> dict:
    # Claims wait in the owner-private dir (here a sandbox beside state/).
    ns = {"re": re, "json": json, "os": os, "time": time, "uuid": uuid,
          "Path": Path, "EA_DIR": tmp,
          "_chat": types.SimpleNamespace(private_dir=lambda: tmp / "private"),
          "log": types.SimpleNamespace(warning=lambda *a, **k: None,
                                       info=lambda *a, **k: None,
                                       error=lambda *a, **k: None)}
    _exec_block("# === staged-action claim protocol",
                "def _handle_context_export", ns)
    return ns


def _stage(tmp: Path, name: str, payload):
    (tmp / "state").mkdir(parents=True, exist_ok=True)
    f = tmp / "state" / name
    f.write_text(json.dumps(payload))
    return f


@contextmanager
def _sandbox(with_key: bool = True):
    """A private EA_DIR laid out the way scheduled_sends expects, exported so
    a real child process lands in the SAME sandbox."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        (tmp / "state").mkdir()
        (tmp / "auth").mkdir()
        old = (S.SENDS_FILE, S.HMAC_KEY_FILE, S._post_slack,
               os.environ.get("EA_DIR"))
        S.SENDS_FILE = tmp / "state" / "scheduled-sends.json"
        S.HMAC_KEY_FILE = tmp / "auth" / "scheduled-send-hmac.key"
        posts: list[str] = []
        S._post_slack = posts.append
        os.environ["EA_DIR"] = str(tmp)
        if with_key:
            S.HMAC_KEY_FILE.write_bytes(
                b"0123456789abcdef0123456789abcdef0123456789")
            S.HMAC_KEY_FILE.chmod(0o600)
        try:
            yield tmp, posts
        finally:
            S.SENDS_FILE, S.HMAC_KEY_FILE, S._post_slack = old[:3]
            if old[3] is None:
                os.environ.pop("EA_DIR", None)
            else:
                os.environ["EA_DIR"] = old[3]


class _FakeGraph:
    """Stands in for graph_client so a test can never reach the network."""

    def __init__(self, result=None, raises=False):
        self.calls = []
        self.result = result or {"status": "sent"}
        self.raises = raises

    def draft_fingerprint(self, message_id):
        if self.raises:
            raise RuntimeError("draft not found")
        return FP

    def send_draft(self, message_id, confirm_token=None, fingerprint=None):
        self.calls.append((message_id, confirm_token))
        if self.raises:
            raise RuntimeError("draft not found")
        return self.result


@contextmanager
def _fake_graph_client(result=None, raises=False):
    fake = _FakeGraph(result, raises)
    had = "graph_client" in sys.modules
    saved = sys.modules.get("graph_client")
    sys.modules["graph_client"] = fake
    try:
        yield fake
    finally:
        if had:
            sys.modules["graph_client"] = saved
        else:
            sys.modules.pop("graph_client", None)


def _graph_actions(tmp: Path):
    import graph_actions as ga
    ga.PENDING_CONFIRMS_FILE = tmp / "pending-confirmations.json"
    ga.PENDING_CONFIRMS_LOCK = tmp / "pending-confirmations.lock"
    return ga


# ---------------------------------------------------------------------------
# 1. Staging: the file is a first-class staged action
# ---------------------------------------------------------------------------

def test_the_staged_file_is_registered_in_the_claim_protocol():
    with tempfile.TemporaryDirectory() as td:
        ns = _claim_ns(Path(td))
    names = dict(ns["STAGED_ACTION_FILES"])
    assert names.get("schedule_send") == "pending-schedule-send.json", (
        "pending-schedule-send.json must be a staged action file, or the "
        "dispatch scan never claims it and no Schedule button appears")


def test_every_staged_file_belongs_to_exactly_one_sweeper():
    """A staged file in no sweeper set is invisible whenever it is written
    out of band, which is the silent drop this protocol exists to stop.
    In two sets, two sweepers race for it."""
    with tempfile.TemporaryDirectory() as td:
        ns = _claim_ns(Path(td))
    swept = (list(ns["CALENDAR_ACTION_FILES"])
             + list(ns["SCHEDULE_ACTION_FILES"])
             + list(ns["CHANNEL_ACTION_FILES"])
             + list(ns["PUBLISH_ACTION_FILES"])
             + list(ns["GOOGLE_SIGNIN_ACTION_FILES"])
             + list(ns["PLUGIN_ACTION_FILES"]))
    assert sorted(swept) == sorted(ns["STAGED_ACTION_FILES"]), (
        f"sweeper sets {swept} do not partition "
        f"{list(ns['STAGED_ACTION_FILES'])}")


def test_the_schedule_send_sweeper_actually_runs():
    body = _fn_body("_pending_events_poller")
    assert "SCHEDULE_ACTION_FILES" in body, (
        "pending-schedule-send.json written out of band (a cron, a plugin, a "
        "maintenance shell) would never surface a Schedule button")
    assert "CALENDAR_ACTION_FILES" in body
    assert "min_age_s=min_age" in body, (
        "the schedule sweeper must inherit the age gate, or it steals a live "
        "dispatch's staged request mid-turn")


def test_staged_request_is_claimed_once_and_consumed():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        f = _stage(tmp, "pending-schedule-send.json",
                   {"draft_id": DRAFT, "send_after": FUTURE,
                    "description": "Eli follow-up",
                    "ledger_id": "followup-eli-1",
                    "thread_ts": "1755.9"})
        claim = _claim_ns(tmp)["_claim_event_actions"]
        actions = claim()
        assert len(actions) == 1, actions
        act = actions[0]
        assert act["action"] == "schedule_send"
        assert act["draft_id"] == DRAFT
        assert act["send_after"] == FUTURE
        assert act["ledger_id"] == "followup-eli-1"
        assert act["_route"]["thread_ts"] == "1755.9", (
            "the staged request must be able to pin its button to a thread")
        assert not f.exists(), "the staged file must be consumed"
        assert claim() == [], "a consumed request must not be claimed twice"


def test_two_consumers_cannot_both_claim_one_request():
    """The rename IS the claim: a racing sweeper and dispatch scan must not
    both post a Schedule button for one staged send."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _stage(tmp, "pending-schedule-send.json",
               {"draft_id": DRAFT, "send_after": FUTURE})
        a = _claim_ns(tmp)["_claim_event_actions"]
        b = _claim_ns(tmp)["_claim_event_actions"]
        won = [len(a()), len(b())]
        assert sorted(won) == [0, 1], won


def test_an_array_stages_several_requests():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _stage(tmp, "pending-schedule-send.json",
               [{"draft_id": "A=", "send_after": FUTURE},
                {"draft_id": "B=", "send_after": FUTURE}])
        actions = _claim_ns(tmp)["_claim_event_actions"]()
        assert [a["draft_id"] for a in actions] == ["A=", "B="]


def test_an_abandoned_claim_is_recovered():
    """A bot restart between claim and post must not lose the request."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        ns = _claim_ns(tmp)
        claims = tmp / "private" / "event-claims"
        claims.mkdir(parents=True, exist_ok=True)
        orphan = claims / "pending-schedule-send.json.999.deadbeef"
        orphan.write_text(json.dumps({"draft_id": DRAFT,
                                      "send_after": FUTURE}))
        os.utime(orphan, (time.time() - 5000, time.time() - 5000))
        actions = ns["_recover_abandoned_claims"](900)
        assert [a["action"] for a in actions] == ["schedule_send"]


def test_the_poster_renders_a_schedule_button():
    section_start = BOT_SRC.index("def _post_staged_action(")
    section = BOT_SRC[section_start:BOT_SRC.index("\ndef ", section_start + 1)]
    assert 'kind == "schedule_send"' in section, (
        "a claimed schedule_send action must reach a confirmation poster; "
        "without this branch it is claimed, dropped and never seen again")
    poster = _fn_body("_post_schedule_send_confirmation")
    assert "_schedule_card(" in poster and "_post_card(" in poster
    card = _fn_body("_schedule_card")
    assert '_chat.CardAction("approve_schedule_send"' in card
    assert '_chat.CardAction("cancel_schedule_send"' in card
    assert ":lock:" in poster and ":lock:" in card


def test_the_poster_refuses_a_request_it_cannot_honour():
    """Better a visible refusal than a button whose click will fail, or a
    queue entry that can never come due."""
    poster = _fn_body("_post_schedule_send_confirmation")
    assert "datetime.fromisoformat(send_after)" in poster, (
        "an unparseable send_after must be refused before the button")
    assert "is in the past" in poster, (
        "a past send_after can never come due; refuse it at the button")
    assert "_fetch_draft_body(draft_id)" in poster, (
        "the block must show the real draft, not the LLM's summary of it")
    assert "_scheduling_preflight" in poster, (
        "a proposed meeting time that is stale now will be staler when the "
        "deferred send fires")


# --- the poster, actually executed -----------------------------------------

class _FakeSlack:
    """Cards go through the frontend; .posts is each card as Slack renders
    it (text and blocks). Text-only posts land in .frontend.posts."""

    def __init__(self):
        self.frontend = MemoryFrontend()

    @property
    def posts(self) -> list:
        out = []
        for c in self.frontend.cards:
            text, blocks = render_card(c["card"])
            out.append({"channel": c["channel"], "thread_ts": c["thread_ts"],
                        "text": text, "blocks": blocks})
        return out


def _poster_ns(draft=None, dm="D-OWNER"):
    """Run the real _post_schedule_send_confirmation against fakes."""
    from datetime import datetime as _dt
    slack = _FakeSlack()
    noted = []
    ns = {
        "re": re, "json": json, "datetime": _dt,
        "_chat": _chat,
        "FRONTEND": slack.frontend,
        "_CARDS": _chat.CardRegistry(Path(tempfile.mkdtemp()) / "cards.json"),
        "log": types.SimpleNamespace(warning=lambda *a, **k: None,
                                     info=lambda *a, **k: None,
                                     error=lambda *a, **k: None),
        "_fetch_draft_body": lambda did, **kw: (
            draft if draft is not None else
            {"to": "eli@example.edu", "cc": "", "subject": "Re: TTX",
             "body": "Numbers Friday.", "conversation_id": "C1"}),
        "_scheduling_preflight": lambda body, *a, **k: "",
        "_resolve_dm_channel": lambda: dm,
        "_note_draft_confirmation": lambda *a, **k: noted.append(a),
        # per-run tally and the DM pointer are side effects outside this test
        "_note_run_post": lambda kind: None,
        "_post_dm_breadcrumb": lambda *a, **k: None,
    }
    _exec_block("# \u2500\u2500 Confirmation cards", "# \u2500\u2500 End confirmation cards", ns)
    _exec_block("def _format_send_after(", "def _execute_schedule_send(", ns)
    _exec_block("def _plain_mail_id(", "def _mediation_enabled(", ns)
    _exec_block("def _post_schedule_send_confirmation(",
                "def _resolve_dm_channel(", ns)
    _exec_block("def _owner_card_channel(", "def _deliver_confirmation(", ns)
    return ns["_post_schedule_send_confirmation"], slack, noted


def test_with_no_owner_dm_the_card_goes_nowhere_else():
    """A card carries the whole draft and its buttons; the thread it was
    asked from can hold other people. With no owner DM, one line in the
    thread names the setting and no card is posted."""
    post_fn, slack, noted = _poster_ns(dm=None)
    post_fn("C-EA", "1755.1", DRAFT, FUTURE, "Eli follow-up", "led-1")
    assert slack.posts == [] and not noted
    [line] = slack.frontend.posts
    assert line["channel"] == "C-EA" and "no DM with the owner" in line["text"]


def test_the_poster_builds_a_real_block_with_both_buttons():
    post_fn, slack, noted = _poster_ns()
    post_fn("C-EA", "1755.1", DRAFT, FUTURE, "Eli follow-up", "led-1")
    assert len(slack.posts) == 1
    post = slack.posts[0]
    actions = [b for b in post["blocks"] if b["type"] == "actions"][0]
    ids = [e["action_id"] for e in actions["elements"]]
    assert ids == ["approve_schedule_send", "cancel_schedule_send"]
    value = json.loads(actions["elements"][0]["value"])
    assert value["draft_id"] == DRAFT
    assert value["send_after"] == FUTURE
    assert value["ledger_id"] == "led-1"
    assert value["thread_ts"] == "1755.1"
    assert value["ea_channel"] == "C-EA"
    assert "confirm_token" not in value and "token" not in value, (
        "no credential may sit in a Slack payload for 48 hours")
    assert "eli@example.edu" in post["text"]
    assert noted, "the draft must be registered as having live buttons"


def test_the_poster_posts_no_button_for_a_bad_request():
    for bad in (("", FUTURE), (DRAFT, ""), (DRAFT, "next tuesday"),
                (DRAFT, PAST), ("r1234567", FUTURE)):
        post_fn, slack, noted = _poster_ns()
        post_fn("C-EA", "1755.1", bad[0], bad[1], "x", "")
        assert slack.posts == [], (
            f"{bad} produced a clickable Schedule button")
        assert len(slack.frontend.posts) == 1, bad
        assert ":warning:" in slack.frontend.posts[0]["text"], bad
        assert not noted, bad


def test_the_poster_reports_a_draft_it_cannot_fetch():
    post_fn, slack, noted = _poster_ns(draft={"error": "404 not found"})
    post_fn("C-EA", "1755.1", DRAFT, FUTURE, "x", "")
    assert slack.posts == []
    assert "404 not found" in slack.frontend.posts[0]["text"]
    assert not noted


def test_a_long_description_cannot_blow_the_slack_value_limit():
    post_fn, slack, _ = _poster_ns()
    post_fn("C-EA", "1755.1", DRAFT, FUTURE, "x" * 5000, "")
    actions = [b for b in slack.posts[0]["blocks"]
               if b["type"] == "actions"][0]
    for element in actions["elements"]:
        assert len(element["value"]) <= 2000, (
            "Slack rejects a button value over 2000 chars, and the whole "
            "block would fail to post")


# ---------------------------------------------------------------------------
# 2. The button is the only route to the token
# ---------------------------------------------------------------------------

def _owning_toplevel_def(offset: int) -> str:
    head = BOT_SRC.rfind("\ndef ", 0, offset)
    return BOT_SRC[head + len("\ndef "):BOT_SRC.index("(", head)].strip()


def test_only_the_approve_handler_reaches_the_executor():
    owners = {_owning_toplevel_def(m.start()) for m in
              re.finditer(r"_execute_schedule_send\(", BOT_SRC)}
    assert owners == {"_execute_schedule_send",
                      "handle_approve_schedule_send"}, (
        f"_execute_schedule_send is reached from {sorted(owners)}; anything "
        f"outside handle_approve_schedule_send un-gates the deferred send")
    assert "_execute_schedule_send(" in _handler_body("approve_schedule_send")


def test_the_approve_handler_checks_owner_and_staleness():
    handler = _handler_body("approve_schedule_send")
    assert "_verify_button_user(ev)" in handler, (
        "anyone in the channel could otherwise queue a send as Kit")
    assert '_reject_if_stale_confirmation(ev, "scheduled-send")' in handler, (
        "a Schedule button posted days ago must not still queue a stale "
        "draft (MAX_CONFIRM_AGE_HOURS)")


def test_the_token_is_minted_at_click_time_not_carried_in_the_payload():
    body = _fn_body("_execute_schedule_send")
    assert '"request-confirm", "--action", "send"' in body, (
        "the executor must mint the one-time token itself")
    assert "schedule-send" in body and "--confirm-token=" in body
    poster = _fn_body("_post_schedule_send_confirmation")
    assert "request-confirm" not in poster and "confirm_token" not in poster, (
        "a token in the button payload would sit in Slack for 48h; mint it "
        "at click time like _execute_send does")


def test_the_executor_preflights_before_queueing():
    body = _fn_body("_execute_schedule_send")
    assert '"preflight-check", "--message-id"' in body, (
        "nobody is watching when a deferred send fires, so the preflight "
        "matters more here than on an immediate send")
    assert body.index('"preflight-check"') < body.index('"request-confirm"'), (
        "a failed preflight must not burn the user's approval")


def test_a_queued_draft_gets_no_second_send_button():
    body = _fn_body("_execute_schedule_send")
    assert '_mark_draft_confirmation(draft_id, "consumed")' in body, (
        "a draft revised after queueing would otherwise also get an "
        "immediate Send block, so the mail could go out twice")


def test_the_llm_is_told_to_stage_not_to_call_the_verb():
    assert "pending-schedule-send.json" in BOT_SRC, (
        "the prompt must document the staging path, or the feature is "
        "reachable in code and invisible to the model")
    stage_doc = BOT_SRC[BOT_SRC.index("To send a draft LATER"):]
    stage_doc = stage_doc[:stage_doc.index("{_PS['output_only']}")]
    assert "Do NOT call graph_actions.py schedule-send" in stage_doc
    assert "NEVER pick the time yourself" in stage_doc, (
        "snooze/defer durations are always user-specified")


# ---------------------------------------------------------------------------
# 3. End to end: a click's token is what the cron accepts
# ---------------------------------------------------------------------------

def test_click_to_cron_end_to_end():
    """The whole chain for real, with only Graph faked: one-time token ->
    cmd_schedule_send -> a genuine scheduled_sends.py child -> cmd_check."""
    with _sandbox() as (tmp, posts):
        ga = _graph_actions(tmp)
        token = ga._create_confirmation("send", DRAFT, "clicked Schedule",
                                        fingerprint=FP)

        args = types.SimpleNamespace(
            draft_id=DRAFT, send_after=PAST,   # already due, so check fires it
            description="Eli follow-up", ledger_id=None,
            confirm_token=token)
        real_fp = ga.graph.draft_fingerprint
        ga.graph.draft_fingerprint = lambda did: FP   # the draft is unchanged
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                ga.cmd_schedule_send(args)
            assert json.loads(buf.getvalue())["status"] == "scheduled", buf.getvalue()

            queued = json.loads(S.SENDS_FILE.read_text())
            assert len(queued) == 1
            assert queued[0]["draft_id"] == DRAFT
            assert queued[0]["fingerprint"] == FP
            assert S.verify_send_token(queued[0]["confirm_token"], DRAFT, PAST,
                                       fingerprint=FP), (
                "the queue entry must carry the crh-signed HMAC, not the "
                "expired one-time token")

            # the one-time token is spent: a replay cannot queue a second send
            buf2 = io.StringIO()
            with redirect_stdout(buf2):
                ga.cmd_schedule_send(args)
            assert json.loads(buf2.getvalue())["status"] == "blocked"
        finally:
            ga.graph.draft_fingerprint = real_fp

        with _fake_graph_client() as fake:
            out = io.StringIO()
            with redirect_stdout(out):
                S.cmd_check(types.SimpleNamespace())
            result = json.loads(out.getvalue())
        assert result["sent"] == 1, (result, posts)
        assert result["refused"] == 0
        assert fake.calls[0][0] == DRAFT
        assert json.loads(S.SENDS_FILE.read_text()) == []


def test_a_send_after_with_an_offset_does_not_wedge_the_queue():
    """An offset-aware send_after must not raise against a naive now()
    and abort the pass, stranding every OTHER queued send."""
    with _sandbox() as (tmp, posts):
        S._save_sends([
            {"draft_id": "AWARE=", "send_after": "2020-01-01T00:00:00-06:00",
             "description": "aware", "ledger_id": "",
             "confirm_token": S.make_send_token(
                 "AWARE=", "2020-01-01T00:00:00-06:00", FP), "fingerprint": FP},
            {"draft_id": DRAFT, "send_after": PAST, "description": "naive",
             "ledger_id": "",
             "confirm_token": S.make_send_token(DRAFT, PAST, FP), "fingerprint": FP},
        ])
        with _fake_graph_client() as fake:
            out = io.StringIO()
            with redirect_stdout(out):
                S.cmd_check(types.SimpleNamespace())
            result = json.loads(out.getvalue())
        assert result["sent"] == 2, (result, posts)
        assert {c[0] for c in fake.calls} == {"AWARE=", DRAFT}


def test_a_permanently_failing_send_is_given_up_not_retried_forever():
    """A deleted draft re-queued unchanged would warn on every cron tick,
    forever. Give it up after MAX_SEND_ATTEMPTS."""
    with _sandbox() as (tmp, posts):
        S._save_sends([{"draft_id": DRAFT, "send_after": PAST,
                        "description": "doomed", "ledger_id": "",
                        "confirm_token": S.make_send_token(DRAFT, PAST, FP), "fingerprint": FP}])
        for _ in range(S.MAX_SEND_ATTEMPTS + 2):
            with _fake_graph_client(raises=True):
                with redirect_stdout(io.StringIO()):
                    S.cmd_check(types.SimpleNamespace())
        assert json.loads(S.SENDS_FILE.read_text()) == [], (
            "the entry must be dropped once it has exhausted its attempts")
        assert len(posts) == S.MAX_SEND_ATTEMPTS, posts
        assert "GIVEN UP" in posts[-1]


# ---------------------------------------------------------------------------
# 4. Cancel
# ---------------------------------------------------------------------------

def test_cancel_queues_nothing_and_mints_nothing():
    handler = _handler_body("cancel_schedule_send")
    assert "_verify_button_user(ev)" in handler
    assert "_execute_schedule_send" not in handler, (
        "Cancel must not reach the executor")
    assert "request-confirm" not in handler and "schedule-send" not in handler
    assert "_replace_buttons_with_status(" in handler, (
        "the buttons must come down so the request cannot be clicked twice")
    assert "Scheduled send cancelled" in handler


def test_cancel_runs_on_an_action_event():
    """The converted handler takes the ActionEvent the frontend dispatches:
    the card's own channel and id for the status line, the value's thread
    for the follow-up question."""
    frontend = MemoryFrontend()
    replaced = []
    ns = {"json": json, "_chat": _chat, "FRONTEND": frontend,
          "app": frontend.registry,
          "_verify_button_user": lambda ev: ev.user_id == "U_OWNER",
          "_replace_buttons_with_status": lambda ch, ts, txt: replaced.append((ch, ts, txt))}
    start = BOT_SRC.index("\ndef _card_value(")
    exec(compile(BOT_SRC[start:BOT_SRC.index("\n\n", start + 1)], "bot[fn]", "exec"), ns)
    start = BOT_SRC.index('@app.action("cancel_schedule_send")')
    exec(compile(BOT_SRC[start:BOT_SRC.index("\n" + "@app.action(", start + 1)], "bot[handler]",
                 "exec"), ns)
    card = _chat.MessageRef("D_OWNER", "1755.9", "1755.9")
    ev = _chat.ActionEvent("cancel_schedule_send", "U_OWNER", card,
                           {"thread_ts": "1755.1", "ea_channel": "C-EA"})
    assert frontend.registry.dispatch_action(ev)
    assert replaced and replaced[0][:2] == ("D_OWNER", "1755.9")
    assert frontend.posts[-1]["channel"] == "C-EA"
    assert frontend.posts[-1]["thread_ts"] == "1755.1"
    replaced.clear()
    other = _chat.ActionEvent("cancel_schedule_send", "U_OTHER", card,
                              {"thread_ts": "1755.1", "ea_channel": "C-EA"})
    frontend.registry.dispatch_action(other)
    assert replaced == [], "only the owner can cancel"


def test_cancel_keeps_the_draft():
    handler = _handler_body("cancel_schedule_send")
    assert "delete-draft" not in handler and "delete_draft" not in handler, (
        "cancelling the SCHEDULE must not destroy the draft")


# ---------------------------------------------------------------------------
# 4b. The queue is one file, written from two processes
# ---------------------------------------------------------------------------
# `cancel` runs bot-side on a click; `check` runs from the */5 crh cron.
# An unlocked read-modify-write of scheduled-sends.json lets the cron's
# _save_sends(remaining) rewrite the queue from a list it read BEFORE the
# cancel landed, so the cancelled send comes back and goes out on the
# next tick.

def test_every_queue_mutation_takes_the_lock():
    src = (ROOT / "preprocessor" / "scheduled_sends.py").read_text()
    for fn in ("def cmd_add", "def cmd_check", "def cmd_cancel"):
        start = src.index(fn)
        end = src.index("\ndef ", start + 1)
        assert "_with_sends_lock" in src[start:end], (
            f"{fn} does a read-modify-write of the queue outside the lock")


def test_cancel_waits_for_a_pass_that_is_already_running():
    """Mutual exclusion for real: a cancel issued while a check pass holds
    the lock lands AFTER it, so neither writes over the other's list."""
    import threading
    with _sandbox() as (tmp, _posts):
        S._save_sends([
            {"draft_id": DRAFT, "send_after": FUTURE, "description": "keep",
             "ledger_id": "", "confirm_token": S.make_send_token(DRAFT, FUTURE, FP), "fingerprint": FP},
            {"draft_id": "OTHER=", "send_after": FUTURE, "description": "drop",
             "ledger_id": "",
             "confirm_token": S.make_send_token("OTHER=", FUTURE, FP), "fingerprint": FP},
        ])
        order = []
        holding = threading.Event()

        def _hold():
            def _slow():
                holding.set()
                time.sleep(0.4)
                order.append("pass-done")
                return {}
            S._with_sends_lock(_slow)

        t = threading.Thread(target=_hold)
        t.start()
        holding.wait(2)
        with redirect_stdout(io.StringIO()):
            S.cmd_cancel(types.SimpleNamespace(draft_id="OTHER="))
        order.append("cancel-done")
        t.join(5)
        assert order == ["pass-done", "cancel-done"], order
        left = [s["draft_id"] for s in json.loads(S.SENDS_FILE.read_text())]
        assert left == [DRAFT], left


def test_a_cancel_is_not_resurrected_by_the_pass_that_was_mid_flight():
    """The concrete loss: cancel drops an entry, the cron pass then saves
    its own `remaining`. Under the lock the pass reads the post-cancel
    queue, so the cancelled send stays gone."""
    with _sandbox() as (tmp, _posts):
        S._save_sends([
            {"draft_id": DRAFT, "send_after": FUTURE, "description": "future",
             "ledger_id": "", "confirm_token": S.make_send_token(DRAFT, FUTURE, FP), "fingerprint": FP},
            {"draft_id": "OTHER=", "send_after": FUTURE, "description": "drop",
             "ledger_id": "",
             "confirm_token": S.make_send_token("OTHER=", FUTURE, FP), "fingerprint": FP},
        ])
        with redirect_stdout(io.StringIO()):
            S.cmd_cancel(types.SimpleNamespace(draft_id="OTHER="))
            with _fake_graph_client():
                S.cmd_check(types.SimpleNamespace())
        left = [s["draft_id"] for s in json.loads(S.SENDS_FILE.read_text())]
        assert left == [DRAFT], left


def test_the_lock_follows_the_queue_file():
    """The tests repoint SENDS_FILE at a sandbox; the lock must move with
    it, or a test run would touch the deployed lock path."""
    with _sandbox() as (tmp, _posts):
        assert str(S._sends_lock_path()).startswith(str(tmp))


# ---------------------------------------------------------------------------
# 5. No click: no token, no send
# ---------------------------------------------------------------------------

def test_staging_the_file_alone_queues_nothing():
    """The LLM's whole capability is writing the staging file. Claiming it
    must not, on its own, put anything in the send queue."""
    with _sandbox() as (tmp, _posts):
        _stage(tmp, "pending-schedule-send.json",
               {"draft_id": DRAFT, "send_after": PAST,
                "description": "unapproved"})
        _claim_ns(tmp)["_claim_event_actions"]()
        assert not S.SENDS_FILE.exists() or \
            json.loads(S.SENDS_FILE.read_text()) == [], (
            "claiming a staged request must only post buttons")


def test_schedule_send_without_a_token_is_blocked():
    with _sandbox() as (tmp, _posts):
        ga = _graph_actions(tmp)
        args = types.SimpleNamespace(draft_id=DRAFT, send_after=FUTURE,
                                     description="x", ledger_id=None,
                                     confirm_token=None)
        buf = io.StringIO()
        with redirect_stdout(buf):
            ga.cmd_schedule_send(args)
        assert json.loads(buf.getvalue())["status"] == "blocked"
        assert not S.SENDS_FILE.exists() or \
            json.loads(S.SENDS_FILE.read_text()) == []


def test_schedule_send_with_a_forged_token_is_blocked():
    with _sandbox() as (tmp, _posts):
        ga = _graph_actions(tmp)
        args = types.SimpleNamespace(draft_id=DRAFT, send_after=FUTURE,
                                     description="x", ledger_id=None,
                                     confirm_token="not-a-real-token")
        buf = io.StringIO()
        with redirect_stdout(buf):
            ga.cmd_schedule_send(args)
        assert json.loads(buf.getvalue())["status"] == "blocked"
        assert not S.SENDS_FILE.exists() or \
            json.loads(S.SENDS_FILE.read_text()) == []


def test_a_token_for_another_draft_does_not_schedule_this_one():
    with _sandbox() as (tmp, _posts):
        ga = _graph_actions(tmp)
        token = ga._create_confirmation("send", "AAMkOTHER=", "other draft")
        args = types.SimpleNamespace(draft_id=DRAFT, send_after=FUTURE,
                                     description="x", ledger_id=None,
                                     confirm_token=token)
        buf = io.StringIO()
        with redirect_stdout(buf):
            ga.cmd_schedule_send(args)
        assert json.loads(buf.getvalue())["status"] == "blocked"


def test_an_entry_nobody_signed_is_refused_at_send_time():
    """The queue file is ea-bot-writable, so the signature is the authority.
    A hand-written entry must be dropped, loudly, with no Graph call."""
    with _sandbox() as (tmp, posts):
        S._save_sends([{"draft_id": DRAFT, "send_after": PAST,
                        "description": "prompt-injected", "ledger_id": "",
                        "confirm_token": "pre-approved:anything"}])
        with _fake_graph_client() as fake:
            out = io.StringIO()
            with redirect_stdout(out):
                S.cmd_check(types.SimpleNamespace())
            result = json.loads(out.getvalue())
        assert result["sent"] == 0 and result["refused"] == 1
        assert fake.calls == [], "no Graph call may happen for a forged entry"
        assert any("REFUSED" in p for p in posts), posts


def test_a_retimed_entry_no_longer_verifies():
    """Signed for next Monday, edited to fire now."""
    with _sandbox() as (tmp, posts):
        signed = S.make_send_token(DRAFT, FUTURE, FP)
        S._save_sends([{"draft_id": DRAFT, "send_after": PAST,
                        "description": "re-timed", "ledger_id": "",
                        "confirm_token": signed, "fingerprint": FP}])
        with _fake_graph_client() as fake:
            out = io.StringIO()
            with redirect_stdout(out):
                S.cmd_check(types.SimpleNamespace())
            result = json.loads(out.getvalue())
        assert result["refused"] == 1 and fake.calls == []


def test_the_llm_cannot_call_the_verbs_at_all():
    """Belt to the token's braces: both CLI entry points are on the deny
    list, so the model cannot even attempt the un-gated path."""
    deny = (ROOT / "scripts" / "disallowed-tools.txt").read_text()
    assert "graph_actions.py schedule-send" in deny
    assert "scheduled_sends.py add" in deny
    med = (ROOT / "preprocessor" / "mail_mediation.py").read_text()
    gated = med[med.index("GATED = {"):]
    assert '"schedule-send"' in gated[:gated.index("}")], (
        "schedule-send must stay GATED for ea-bot under mail mediation")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS {t.__name__}")
            passed += 1
        except Exception as e:
            traceback.print_exc()
            print(f"  FAIL {t.__name__}: {e}")
            failed += 1
    print(f"\n  {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)

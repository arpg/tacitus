#!/usr/bin/env python3
"""The worked examples in docs/llm-lanes.md, produced by the real code.

Every example runs the lane's own functions (mail_gate, mail_privacy's
mediator checks, email_writer, draft_ab) on a made-up mailbox in a
throwaway private dir, with a stand-in for the remote endpoint. Nothing
reaches the network, the mailbox or the real private store, and no real
mail is read.

    llm_lane_examples.py            print every example as Markdown
    llm_lane_examples.py --check docs/llm-lanes.md
                                    exit 1 when a block in the doc no
                                    longer matches what the code prints
    llm_lane_examples.py --write docs/llm-lanes.md
                                    rewrite the doc's example blocks, and
                                    fill each {{example:<name>}} placeholder
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "preprocessor"))
# The digest prints local times; the examples pin one zone so the doc
# renders the same on every machine.
os.environ["TZ"] = "America/Boise"
time.tzset()

NOW = datetime(2026, 10, 13, 15, 0, tzinfo=timezone.utc)      # Tue 09:00 MT
USER = "kit@example.edu"
CONFIG = {
    "endpoint": "https://models.example.edu/v1", "model": "qwen3.8-mm",
    "mail": "enforce", "scheduling": "shadow", "drafting": "remote", "digests": "remote",
    "shadow_until": "2026-11-02",
    "keep_from_anthropic": {"domains": ["mil", "contractor.example.com"], "addresses": [], "admissions": True},
}

# The made-up mailbox: what arrived, and what the remote model made of it.
MAIL = [
    {"id": "msg-slides", "conv": "conv-slides", "from": "pat.rivera@example.edu",
     "subject": "Slides for Thursday's review", "hours_ago": 2,
     "preview": "Hi Kit, could you send me the final slides for the program review by "
                "Wednesday noon? I want to merge them into the master deck before Thursday.",
     "result": {"state": "needs-reply", "needs_reply": True, "domain": "research",
                "summary": "Pat is assembling the master deck for Thursday's program review.",
                "ask": "Send the final slides.", "deadline": "2026-10-14"}},
    {"id": "msg-meet", "conv": "conv-meet", "from": "sam.okafor@example.edu",
     "subject": "Meeting about my qualifying exam", "hours_ago": 3,
     "preview": "Hi Professor Smith, would you have 30 minutes next week? I am free "
                "Monday 10-11 or Tuesday 2-3.",
     "result": {"state": "scheduling", "needs_reply": True, "domain": "teaching",
                "summary": "Sam wants 30 minutes next week about the qualifying exam.",
                "ask": "Pick a meeting time.", "scheduling": "proposes-times",
                "offered_times": ["2026-10-19T10:00", "2026-10-20T14:00"]}},
    {"id": "msg-news", "conv": "conv-news", "from": "news@cs.example.edu",
     "subject": "CS department weekly", "hours_ago": 5,
     "preview": "This week: new faculty lunch, colloquium schedule, building access...",
     "result": {"state": "fyi", "needs_reply": False, "domain": "service",
                "summary": "Department newsletter: faculty lunch Friday, colloquium schedule posted."}},
    {"id": "msg-talk", "conv": "conv-talk", "from": "seminar@robotics.example.edu",
     "subject": "Seminar: learned radar odometry", "hours_ago": 6,
     "preview": "Join us Thursday at 3pm for a talk on learned radar odometry...",
     "result": {"state": "fyi", "needs_reply": False, "domain": "research",
                "summary": "Seminar on learned radar odometry, Thursday 3 pm.",
                "deadline": "2026-10-16"}},
    {"id": "msg-kept", "conv": "conv-kept", "from": "j.doe@contractor.example.com",
     "subject": "Follow-up on the sensing white paper", "hours_ago": 4,
     "preview": "Kit, following our call, can you confirm the scope paragraph by Friday?",
     "kept": "listed domain contractor.example.com",
     "result": {"state": "needs-reply", "needs_reply": True, "domain": "professional",
                "summary": "J. Doe asks Kit to confirm the scope paragraph of the white paper.",
                "ask": "Confirm the scope paragraph.", "deadline": "2026-10-16"}},
    {"id": "msg-cfp", "conv": "conv-cfp", "from": "events@robotics.example.org",
     "subject": "Workshop on field robots: call for papers", "hours_ago": 72,
     "preview": "Submissions are due October 15...",
     "result": {"state": "fyi", "needs_reply": False, "domain": "research",
                "summary": "Call for papers for the field robots workshop.",
                "deadline": "2026-10-15"}},
    {"id": "msg-new", "conv": "conv-new", "from": "alex.kim@example.edu",
     "subject": "Quick question on the budget", "hours_ago": 0.1,
     "preview": "Do you want the travel line at 4k or 6k?", "result": None},
    {"id": "msg-late", "conv": "conv-late", "from": "dana.lee@example.edu",
     "subject": "Room change for Friday", "hours_ago": 6,
     "preview": "We moved Friday's meeting to room 831.", "result": None},
]


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _received(m: dict) -> datetime:
    return NOW - timedelta(hours=m["hours_ago"])


def _graph_message(m: dict) -> dict:
    return {"id": m["id"], "conversationId": m["conv"], "receivedDateTime": _iso(_received(m)),
            "from": {"emailAddress": {"address": m["from"]}}, "subject": m["subject"],
            "bodyPreview": m["preview"]}


def _detector_item(m: dict) -> dict:
    """The shape detect_needs_response gives a message."""
    return {"message_id": m["id"], "conversation_id": m["conv"], "subject": m["subject"],
            "from": m["from"], "received": _iso(_received(m)), "body_preview": m["preview"]}


def _setup(tmp: Path):
    os.environ["EA_PRIVATE_DIR"] = str(tmp / "private")
    (tmp / "private").mkdir(mode=0o700)
    import owner_trust
    import remote_llm
    import mail_privacy
    import mail_gate
    import draft_ab
    remote_llm._owner = lambda: True
    remote_llm._breaker = lambda *a: None
    mail_privacy._user_addresses = lambda: {USER}
    mail_gate._list_senders = lambda: set()
    draft_ab._style_profile = lambda addr: json.dumps(
        {"found": True, "contact": addr, "greeting": "Hi Pat,", "sign_off": "Kit"})
    owner_trust.write_owner_file(tmp / "private" / remote_llm.CONFIG_FILE,
                                 json.dumps(CONFIG).encode(), mode=0o600)
    for m in MAIL:
        if m["result"] is None:
            continue
        res = {"sensitivity": "normal", "confidential_kind": "none", "ask": "", "deadline": "",
               "deadline_text": "", "scheduling": "none", "offered_times": [], "people": [],
               "tone": [], **m["result"]}
        remote_llm.append(mail_privacy.EXTRACTIONS, {
            "key": f"k-{m['id']}", "id": m["id"], "conversation_id": m["conv"],
            "received": _iso(_received(m)), "from": m["from"], "subject": m["subject"],
            "status": "ok", "keep_from_anthropic": m.get("kept", ""), "result": res})
    mail_privacy._INDEX_CACHE.update(mtime=None, index=None)


def _block(obj) -> str:
    return obj if isinstance(obj, str) else json.dumps(obj, indent=2, ensure_ascii=False)


def build() -> dict[str, str]:
    """{example name: text}, every one printed by the lane's own code."""
    out: dict[str, str] = {}
    with tempfile.TemporaryDirectory(prefix="llm-lane-examples-") as td:
        _setup(Path(td))
        import mail_gate
        import mail_privacy
        import email_writer
        import draft_ab

        # The gate over one run: the inbox it fetched, then a detector's output.
        inbox = [_graph_message(m) for m in MAIL]
        gate = mail_gate.Gate(inbox, label="intake-check", graph=object(), budget_s=0.01, now=NOW)
        items = [_detector_item(m) for m in MAIL]
        out["gate-before"] = _block(items[0])
        passed = gate.items(items)
        out["gate-after"] = _block(passed[0])
        out["gate-unread"] = _block(next(i for i in passed if i.get("remote_unread")))
        out["gate-classes"] = "\n".join(
            f"{m['id']:<12} {gate.classify(_detector_item(m))[0]}" for m in MAIL)
        out["gate-summary"] = _block({"mail_gate": gate.summary()})
        out["gate-ready"] = _block(sorted(m["id"] for m in gate.ready(inbox)))

        # The remote digest of what the gate left out.
        posted = []
        mail_gate.digest(now=NOW, post=lambda text, now: posted.append(text) or True)
        out["digest"] = posted[0]

        # A cron lane's reads of a kept thread, and a room's.
        argv = ["get-conversation", "--conversation-id", "conv-kept"]
        out["kept-refused"] = _block({
            "cron lane": mail_privacy.release_precheck("graph_actions", "get-conversation",
                                                       argv, "cron-morning-briefing-20261013"),
            "room or thread": mail_privacy.release_precheck("graph_actions", "get-conversation",
                                                            argv, "run-7f3a")})
        rows = [{"id": "msg-slides", "conversation_id": "conv-slides", "subject": MAIL[0]["subject"]},
                {"id": "msg-kept", "conversation_id": "conv-kept", "subject": MAIL[4]["subject"]}]
        pruned, _ = mail_privacy.release_filter("graph_actions", "search-mail",
                                                ["search-mail", "--query", "paper"],
                                                "cron-morning-briefing-20261013", json.dumps(rows))
        out["kept-pruned"] = _block(json.loads(pruned))

        # The writer: a brief in, a body out; and a fallback.
        brief = ("Recipient: pat.rivera@example.edu (Pat Rivera, colleague).\n"
                 "Thread: Pat asks for the final program-review slides by Wednesday noon.\n"
                 "The reply must say: yes, Kit sends them Tuesday evening; the budget "
                 "slide is still waiting on the sponsor's numbers.")
        out["writer-brief"] = brief

        def remote(messages, **kw):
            return {"content": ("Hi Pat,\n\nYes, I will send the final slides Tuesday "
                                "evening. The budget slide is still waiting on the "
                                "sponsor's numbers.\n\nKit"),
                    "contents": [], "latency_ms": 2140}

        body_file = Path(td) / "reply-body.txt"
        answer, code = email_writer.write("pat.rivera@example.edu", brief, str(body_file),
                                          chat=remote)
        answer["out"] = "/home/ea-bot/.ea/state/reply-body.txt"
        out["writer-ok"] = f"exit {code}\n" + _block(answer)

        def asks(messages, **kw):
            return {"content": "NEED: Which day should the slides go out?", "contents": [],
                    "latency_ms": 1800}
        answer, code = email_writer.write("pat.rivera@example.edu", brief, str(body_file),
                                          chat=asks)
        out["writer-fallback"] = f"exit {code}\n" + _block(answer)

        # The send measure: the bot matches the draft to the writer's record,
        # and the Send button records how far the user moved it.
        os.environ["EA_RUN_ID"] = ""
        sent_html = []

        class Graph:
            def __init__(self, text):
                self.text = text

            def get_message_full(self, mid):
                html = "".join(f"<p>{ln}</p>" for ln in self.text.splitlines())
                sent_html.append(mid)
                return {"conversationId": "conv-slides",
                        "body": {"contentType": "HTML", "content": f"<html><body>{html}</body></html>"},
                        "toRecipients": [{"emailAddress": {"address": "pat.rivera@example.edu"}}]}

        shown = body_file.read_text() if body_file.exists() else ""
        g = Graph(shown)
        draft_ab.note_writer_draft("draft-1", "", graph=g)
        g.text = shown.replace("Tuesday evening", "tonight")
        draft_ab.note_send("draft-1", draft_ab.snapshot("draft-1", graph=g))
        rep = draft_ab.report()["remote_writer"]
        rep["since"] = "2026-10-13T15:00:00Z"
        out["send-measure"] = _block({"remote_writer": rep})

        out["list-digest"] = _list_digest_post()
        out["admissions-digest"] = _admissions_post()
        out["briefing-morning"], out["briefing-afternoon"] = _briefings()
        out["briefing-brief"] = _briefing_brief()

        # Sealed mail: what a room's get-conversation returns, with the
        # user's own reply read inline; and the thread brief on the row.
        out["sealed-conversation"] = _block(_sealed_conversation())
        out["thread-brief"] = _block(_thread_brief())
        out["escalation-prompt"] = _escalation_prompt()

        # The ghost week: the read record, the judge's grade of it, a
        # scheduling turn's grade, and the lines the daily report posts.
        out["ghost-read"], out["ghost-grade"], out["ghost-sched-grade"], out["ghost-summary"] = _ghost()
    return out


OWN_REPLY = {"id": "msg-slides-reply", "conversation_id": "conv-slides",
             "subject": "RE: Slides for Thursday's review", "from": {"address": USER},
             "to": ["pat.rivera@example.edu"], "cc": [], "sent": _iso(NOW - timedelta(hours=1)),
             "is_draft": False, "has_attachments": False,
             "text": "Yes, I will send them Tuesday evening. The budget slide is still waiting on the sponsor's numbers."}


def _sealed_conversation() -> dict:
    """get-conversation on conv-slides, sealed: the raw verb's output goes
    through mail_privacy.seal_output with a stand-in for the remote model
    reading the user's own reply."""
    import mail_privacy
    raw = {"conversation_id": "conv-slides", "subject": MAIL[0]["subject"], "message_count": 2,
           "participants": [MAIL[0]["from"], USER],
           "messages": [
               {"id": MAIL[0]["id"], "subject": MAIL[0]["subject"], "from": {"address": MAIL[0]["from"]},
                "to": [USER], "cc": [], "sent": _iso(_received(MAIL[0])), "is_draft": False,
                "has_attachments": False, "text": MAIL[0]["preview"]},
               dict(OWN_REPLY)],
           "_note": "Quoted history stripped from each message."}

    def reads_own(messages, **kw):
        return ({"sensitivity": "normal", "confidential_kind": "none", "needs_reply": False,
                 "ask": "", "deadline": "", "deadline_text": "", "scheduling": "none",
                 "offered_times": [], "people": [], "tone": [], "state": "fyi",
                 "domain": "research",
                 "summary": "Kit will send the slides Tuesday evening; the budget slide waits on the sponsor's numbers."},
                {"latency_ms": 1900})

    out = mail_privacy.seal_output("get-conversation", json.loads(json.dumps(raw)),
                                   cfg=dict(CONFIG, mail="sealed"), graph=object(),
                                   chat=reads_own, budget_s=5)
    mail_privacy._INDEX_CACHE.update(mtime=None, index=None)
    return out


GHOST_CONFIG = dict(CONFIG, mail="ghost", shadow_until="2026-10-19",
                    ghost={"mail": {"from": "2026-10-13", "until": "2026-10-19"},
                           "scheduling": {"from": "2026-10-13", "until": "2026-10-19"}})
GHOST_SEALED_NOTE = "Sealed mail: the text of each message is replaced by the remote model's record"


def _ghost() -> tuple[str, str, str, str]:
    """One room read of the slides thread in the ghost week: the lane got
    the text, the sealed copy was recorded on the side, and the judge
    (a stand-in here) graded it against the draft the turn wrote. Then
    one scheduling shadow turn graded the same way, and the daily lines."""
    import ghost_week
    ghost_week._draft_outcomes = lambda runs: {"drafts": 0}
    mail_privacy = sys.modules["mail_privacy"]
    mail_privacy._INDEX_CACHE.update(mtime=None, index=None)
    raw = {"conversation_id": "conv-slides", "subject": MAIL[0]["subject"], "message_count": 2,
           "participants": [MAIL[0]["from"], USER],
           "messages": [
               {"id": MAIL[0]["id"], "subject": MAIL[0]["subject"], "from": {"address": MAIL[0]["from"]},
                "to": [USER], "cc": [], "sent": _iso(_received(MAIL[0])), "is_draft": False,
                "has_attachments": False, "text": MAIL[0]["preview"] + " The template is at https://example.edu/deck-template."},
               dict(OWN_REPLY)]}
    sealed = _sealed_conversation()
    rec = ghost_week.record_read("get-conversation", ["--conversation-id", "conv-slides"], "1a12-room",
                                 json.dumps(raw), served="text", sealed=sealed, seal_s=1.9,
                                 now=NOW + timedelta(minutes=5))
    read = dict(rec, id="<read id>")
    ghost_week.remote_llm.append(ghost_week.TURNS, {
        "ts": _iso(NOW + timedelta(minutes=6)), "run_id": "1a12-room", "lane": "room", "room": "ea-program-review",
        "user_msg": "reply to Pat and point her at the template",
        "reply": ":email: Draft created to Pat Rivera re: Slides for Thursday's review.",
        "tools": ["Bash: graph_actions.py get-conversation --conversation-id conv-slides",
                  "Bash: graph_actions.py create-reply --message-id msg-slides ..."],
        "asked_for_text": False})
    ghost_week._journal_drafts = lambda run_id: ["draft-1"] if run_id == "1a12-room" else []
    ghost_week._draft_bodies = lambda ids: [{"to": [MAIL[0]["from"]], "subject": "RE: " + MAIL[0]["subject"],
                                            "body": "Hi Pat,\n\nThe slides come Tuesday evening, on the template at "
                                                    "https://example.edu/deck-template.\n\nKit"}]
    answers = [{"facts_used": [
                    {"fact": "Pat needs the final slides by Wednesday noon", "in_record": True, "category": "date"},
                    {"fact": "Kit promised them Tuesday evening", "in_record": True, "category": "commitment"},
                    {"fact": "the template URL https://example.edu/deck-template", "in_record": False, "category": "url"}],
                "record_errors": [{"field": "deadline", "record": "2026-10-14", "thread": "Wednesday noon (2026-10-14 12:00)"}],
                "verdict": "degraded",
                "enhancement": "Carry every URL from the message in a `links` field of the record."}]

    def judge(prompt, schema, which):
        return answers.pop(0), {"cost_usd": 0.02}
    ghost_week.grade_mail(runner=judge, now=NOW + timedelta(hours=1))
    grade = [g for g in ghost_week.remote_llm.read(ghost_week.MAIL_GRADES) if g.get("verdict")][0]
    grade = {k: v for k, v in grade.items() if k not in ("ts", "read_id", "cost_usd")}

    ghost_week.remote_llm.append(ghost_week.SCHED_TURNS, {
        "run_id": "1a13-sched", "ts": _iso(NOW + timedelta(hours=2)), "thread_ts": "stream:5:topic:Sam", "lane": "sched-channel",
        "status": "ok", "latency_s": 28.0,
        "user_msg": "find 30 minutes with Sam next week",
        "task_block": "Sam Okafor asks for 30 minutes next week about the qualifying exam; offered Mon 10-11 or Tue 2-3.",
        "solver_options": [["2026-10-20T14:00:00-06:00", "2026-10-20T14:30:00-06:00"]],
        "reply": "Tue Oct 20 at 2:00 pm is open. Monday 10 to 11 is taken by the lab meeting. Shall I hold Tuesday?",
        "writes": [{"tool": "ea-managed-hold", "args": {"start": "2026-10-20T14:00:00-06:00",
                                                        "end": "2026-10-20T14:30:00-06:00", "summary": "Hold: Sam"}}],
        "claude": {"reply": "Tuesday Oct 20, 2:00 to 2:30 pm works; Monday is blocked by the lab meeting. "
                            "I held Tuesday on the EA calendar.", "elapsed_s": 44.0, "drafts": [],
                   "tool_calls": [["Bash", 3.0, "scheduling_engine.py validate-draft --times ..."]]},
        "checks": {"shadow": {"times": ["2026-10-20T14:00"], "invented": [], "breaks": [], "mode": "validate",
                              "validity": {"named": 1, "checked": 1, "valid": 1}},
                   "claude": {"times": ["2026-10-20T14:00"], "breaks": [], "mode": "validate", "staged": 0},
                   "mode_agree": True, "overlap": 1.0}})
    answers.append({"shadow_grade": "edit", "claude_grade": "send", "better": "claude",
                    "problems": [{"kind": "missing-write",
                                  "note": "asked whether to hold instead of holding; the rules say hold the top option"}],
                    "enhancement": "Say in the skill that an EA-Managed hold on the top option is placed, not offered."})
    ghost_week.grade_sched(runner=judge, now=NOW + timedelta(hours=3))
    sg = [g for g in ghost_week.remote_llm.read(ghost_week.SCHED_GRADES) if g.get("shadow_grade")][0]
    sg = {k: v for k, v in sg.items() if k not in ("ts", "cost_usd")}
    lines = ghost_week.summary_lines(cfg=GHOST_CONFIG, today=datetime(2026, 10, 15).date())
    return _block(read), _block(grade), _block(sg), "\n".join(lines)


def _thread_brief() -> dict:
    import mail_privacy
    mail_privacy._INDEX_CACHE.update(mtime=None, index=None)
    return mail_privacy.thread_brief("conv-slides", now=NOW)


def _briefing_json() -> dict:
    """A gated morning briefing JSON for the made-up Monday."""
    return {
        "today_top": [{"ref": "msg-slides", "reason": "needs a reply"},
                      {"ref": "row-nsf", "reason": "due in 2d"}],
        "new_needs_response": [
            {"message_id": "msg-slides", "conversation_id": "conv-slides",
             "subject": "Slides for Thursday's review", "from": "pat.rivera@example.edu",
             "from_name": "Pat Rivera",
             "body_preview": "Pat is assembling the master deck. Ask: Send the final slides. Due: 2026-10-14",
             "remote_record": {"ask": "Send the final slides.", "deadline": "2026-10-14"}},
            {"message_id": "msg-agenda", "conversation_id": "conv-agenda",
             "subject": "Faculty meeting agenda", "from": "dean.office@example.edu",
             "from_name": "Dean's Office", "body_preview": "The dean asks for agenda items.",
             "remote_record": {"ask": "Send agenda items.",
                               "people": [{"name": "J. Smith", "role": "dean"}]}},
            {"message_id": "msg-survey", "conversation_id": "conv-survey",
             "subject": "Co-authoring the survey", "from": "lee.wong@example.edu",
             "from_name": "Lee Wong", "body_preview": "Lee asks Kit for the related-work section.",
             "remote_record": {"ask": "Write the related-work section."}}],
        "open_obligations": [
            {"id": "row-slides", "description": "Reply to Pat about the slides",
             "source_message_id": "msg-slides", "conversation_id": "conv-slides"},
            {"id": "row-nsf", "description": "NSF annual report", "deadline": "2026-10-14"},
            {"id": "row-travel", "description": "Travel form for the Portland trip"}],
        # Graph's calendar view: UTC wall-clock with timeZone "UTC".
        "todays_events": [{"subject": "Lab meeting",
                           "start": {"dateTime": "2026-10-12T15:30:00.0000000", "timeZone": "UTC"},
                           "end": {"dateTime": "2026-10-12T16:30:00.0000000", "timeZone": "UTC"}},
                          {"subject": "Thesis committee",
                           "start": {"dateTime": "2026-10-12T21:00:00.0000000", "timeZone": "UTC"},
                           "end": {"dateTime": "2026-10-12T22:00:00.0000000", "timeZone": "UTC"}}],
        "tomorrows_events": [{"subject": "Program review",
                              "start": {"dateTime": "2026-10-13T14:00:00.0000000", "timeZone": "UTC"}}],
        "availability_merged": [{"start": "2026-10-12T19:00:00+00:00", "end": "2026-10-12T20:30:00+00:00"}],
        "handled_by_reply": [{"subject": "x"}, {"subject": "y"}],
        "delta_summary": {"open_obligations": 3, "total_inbox": 41},
        "recently_closed": [], "new_scheduling_requests": [{"subject": "meeting"}],
        "scheduling_channel_active": True,
    }


def _briefings() -> tuple[str, str]:
    """The morning briefing and the afternoon update as remote_briefing
    renders them, with the remote model's picks given."""
    import remote_briefing as rb
    rb._rules = lambda: ""
    rb._profile = lambda: "The user: a professor."
    morning_plan = {
        "get_done_today": [{"ref": "R1", "when": "in the 1-2:30 gap", "why": "Pat merges the deck Wednesday"},
                           {"ref": "L1", "when": "before your 9:30", "why": "due Wednesday"}],
        "flexes": [{"ref": "L2", "line": "file it this week"}],
        "drafts": [{"ref": "R1", "reply_must_say": "Yes, the final slides go out tonight."},
                   {"ref": "R2", "reply_must_say": "Two items: the lab move and the open hires."}],
        "needs_you": [],
        "escalate": [{"ref": "R3", "why": "a section of a paper to write",
                      "task": "draft the related-work section from the survey thread"}]}
    afternoon_plan = {"since_morning": [{"ref": "R3", "line": "Lee is waiting on the section"}],
                      "pending": [{"ref": "L2", "line": "still not filed"}]}

    def model(plan):
        return lambda messages, **kw: (json.loads(json.dumps(plan)), {"latency_ms": 10})
    when = datetime(2026, 10, 12, 10, 0, tzinfo=timezone.utc)
    morning, _ = rb.run("morning-briefing", _briefing_json(), run_id="cron-morning-briefing-20261012",
                        now=when, chat=model(morning_plan),
                        drafter=lambda c, says, run_id: {"ok": True, "draft_id": "draft-1"},
                        queue=lambda items, agent, now: len(items),
                        audit=lambda agent, text, b, dry_run, chat, rendered=None: {
                            "closed": ["row-nsf"], "parked": [], "unsure_rows": [],
                            "closed_rows": [{"id": "row-nsf", "why": "submitted Friday in Research.gov",
                                             "description": "NSF annual report"}]})
    afternoon, _ = rb.run("afternoon-catchup", _briefing_json(), run_id="cron-afternoon-catchup-20261012",
                          now=when.replace(hour=21), chat=model(afternoon_plan),
                          audit=lambda *a, **k: {})
    return morning, afternoon


def _briefing_brief() -> str:
    """The brief remote_briefing.make_draft hands the writer for one draft:
    built in code from the item's facts and the remote model's pick."""
    import remote_briefing as rb
    seen = []

    def write(to, brief, out, conversation_id=""):
        seen.append(brief)
        return {"status": "fallback", "reason": "example"}, 4
    rb.make_draft({"from": "pat.rivera@example.edu", "who": "Pat Rivera",
                   "title": "Slides for Thursday's review",
                   "summary": "Pat is assembling the master deck. Ask: Send the final slides. Due: 2026-10-14",
                   "ask": "Send the final slides.", "message_id": "msg-slides",
                   "conversation_id": "conv-slides"},
                  "Yes, the final slides go out tonight.", run_id="cron-morning-briefing-20261012",
                  write=write)
    return seen[0]


SURVEY_BRIEF = {
    "conversation_id": "AAQkADconv-survey", "subject": "Co-authoring the survey",
    "messages": [
        {"id": "msg-survey-1", "from": "lee.wong@example.edu", "received": "2026-10-09T16:00:00Z",
         "own": False, "gist": "Lee shares the survey outline and asks Kit to write the related-work section.",
         "state": "todo", "ask": "Write the related-work section.", "deadline": "2026-10-23",
         "offered_times": []},
        {"id": "msg-survey-2", "from": USER, "received": "2026-10-09T18:00:00Z", "own": True,
         "gist": "Kit agreed and asked for the current draft."},
        {"id": "msg-survey-3", "from": "lee.wong@example.edu", "received": "2026-10-12T15:00:00Z",
         "own": False, "gist": "Lee sends the draft and the reference list.",
         "state": "todo", "ask": "Write the related-work section.", "deadline": "2026-10-23",
         "offered_times": []}],
    "ask": "Write the related-work section.", "deadline": "2026-10-23", "state": "todo"}


def _escalation_prompt(brief: dict | None = SURVEY_BRIEF) -> str:
    """What the Claude session gets for one hand-off: the bot's own prompt
    builder, executed from its source, with the row's thread brief."""
    import re
    src = (REPO / "ea-chat-bot.py").read_text()

    def fn(name):
        start = src.index(f"\ndef {name}(")
        return src[start:src.index("\ndef ", start + 1)]
    ns = {"re": re}
    exec(fn("_untrusted_field"), ns)
    exec(fn("_remote_escalation_item"), ns)
    exec(fn("_remote_escalation_prompt"), ns)
    return ns["_remote_escalation_prompt"]({
        "conversation_id": "AAQkADconv-survey", "ledger_id": "row-survey",
        "subject": "Co-authoring the survey", "from_name": "Lee Wong",
        "why": "a section of a paper to write",
        "task": "draft the related-work section from the survey thread",
        "brief": brief})


class _Model:
    """The remote model's answers, one per call, as chat_json returns them."""

    def __init__(self, *answers):
        self.answers = list(answers)

    def __call__(self, messages, **kw):
        return json.loads(json.dumps(self.answers.pop(0))), {"latency_ms": 10}


def _quiet(fn):
    import contextlib
    import io
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return fn()


def _list_digest_post() -> str:
    """One mailing-list digest judged by the remote model and rendered by
    code. The model's answer carries an em-dash and a typed URL; the code
    removes both."""
    import remote_digests as rd
    from datetime import date
    entry = {
        "list": "robo-list", "kind": "digest", "mode": "filter", "follow_links": False,
        "emoji": ":newspaper:", "notes": "", "message_id": "list-1",
        "subject": "robo-list Digest, Vol 12, Issue 40",
        "from": "robo-list-request@lists.example.org", "from_name": "",
        "received": "2026-10-13T12:00:00Z", "items_total": 5, "parse_warnings": [],
        "items": [
            {"n": 1, "title": "Release of the Snowfall LiDAR dataset", "tags": ["dataset"],
             "from_name": "R. Author", "body": "We release 40K LiDAR sweeps in falling snow.",
             "links": ["https://example.org/snowfall-paper", "https://github.com/example/snowfall"]},
            {"n": 2, "title": "Workshop on field robots: deadline extended", "tags": ["meetings"],
             "body": "Extended to Nov 1.", "links": []},
            {"n": 4, "title": "Special issue on aerial autonomy", "tags": ["cfp"], "body": "Call.",
             "links": []},
        ],
        "dropped": [{"n": 3, "title": "Postdoc position", "tags": ["jobs"], "reason": "tag:jobs"},
                    {"n": 5, "title": "PhD positions", "tags": ["jobs"], "reason": "tag:jobs"}],
        "rules": {"scopes": ["newsletters", "newsletters:robo-list"], "instructions": [],
                  "prefer": [], "avoid": [], "facts": [], "calibration": []},
    }
    answer = {"list_title": "robo-list",
              "picks": [{"n": 1, "title": "Snowfall LiDAR dataset", "detail": "R. Author, Example University",
                         "bullets": ["40K LiDAR sweeps in falling snow \u2014 with labels",
                                     "Benchmark at https://unknown.example/x for detection."],
                         "links": [{"index": 1, "label": "Code"}, {"index": 0, "label": "Paper"}]}],
              "skipped": [{"n": 2, "category": "workshop or CFP notices"},
                          {"n": 4, "category": "special issues"}]}
    data = {"mode": "list-digest", "digests": [entry], "errors": [],
            "profile": {"name": "Kit", "role_description": "Associate Professor",
                        "bio_blurb": "Robot perception in degraded conditions."}}
    posts = []
    _quiet(lambda: rd.run_list_digest(
        collect=lambda: data, chat=_Model(answer), has_chat=True, today=date(2026, 10, 13),
        post=lambda channel, text: posts.append(text) or True,
        archive=lambda d, disposition: {"archived": 1, "failed": 0}))
    return posts[0]


def _admissions_post() -> str:
    import remote_digests as rd

    def msg(subject, text):
        return {"subject": subject, "received": "2026-10-08T10:00:00Z", "has_attachments": False,
                "kinds": ["subject:prospective-student"], "text": text,
                "message_id": "x", "conversation_id": "c"}
    data = {"mode": "admissions-digest", "window_days": 7, "messages": 4, "senders": 3, "errors": [],
            "profile": {"name": "Kit", "bio_blurb": "Robot perception."},
            "digests": [
                {"from": "ana@uni-a.example.edu", "name": "Ana Example", "domain": "uni-a.example.edu",
                 "count": 1, "follow_ups": 0,
                 "messages": [msg("Prospective PhD student, Fall 2027", "I built a radar odometry stack.")]},
                {"from": "bo@uni-b.example.edu", "name": "Bo Sample", "domain": "uni-b.example.edu",
                 "count": 2, "follow_ups": 1,
                 "messages": [msg("PhD inquiry", "Dear Professor, I admire your lab."),
                              msg("Re: PhD inquiry", "Following up.")]},
                {"from": "cy@uni-c.example.edu", "name": "", "domain": "uni-c.example.edu",
                 "count": 1, "follow_ups": 0,
                 "messages": [msg("Seeking PhD positions", "I found a gap in robustness.")]}]}
    answer = {"senders": [
        {"i": 1, "shortlist": True, "strength": 4, "school": "Uni A",
         "subject": "Prospective PhD student, Fall 2027",
         "fit": "built a radar odometry stack, close to the lab's work", "pattern": "real-fit"},
        {"i": 2, "shortlist": False, "strength": 1, "school": "", "subject": "PhD inquiry",
         "fit": "", "pattern": "templated"},
        {"i": 3, "shortlist": False, "strength": 2, "school": "", "subject": "", "fit": "",
         "pattern": "generic-gap"}]}
    posts = []
    _quiet(lambda: rd.run_admissions_digest(
        collect=lambda: data, chat=_Model(answer), enabled=lambda: True, has_chat=True,
        prefs_data={"rules": [], "calibration": []},
        now=datetime(2026, 10, 12, 14, 0, tzinfo=timezone.utc),
        post=lambda channel, text: posts.append(text) or True))
    return posts[0]


ORDER = ("gate-classes", "gate-before", "gate-after", "gate-unread", "gate-summary",
         "gate-ready", "digest", "kept-refused", "kept-pruned", "writer-brief", "writer-ok",
         "writer-fallback", "send-measure", "list-digest", "admissions-digest",
         "briefing-morning", "briefing-afternoon", "briefing-brief", "sealed-conversation",
         "thread-brief", "escalation-prompt", "ghost-read", "ghost-grade", "ghost-sched-grade",
         "ghost-summary")


def _lang(text: str) -> str:
    try:
        json.loads(text)
        return "json"
    except ValueError:
        return "text"


def markdown(ex: dict[str, str]) -> str:
    return "\n\n".join(_block_of(name, ex[name]) for name in ORDER) + "\n"


def _block_of(name: str, text: str) -> str:
    return f"<!-- example: {name} -->\n```{_lang(text)}\n{text}\n```"


def write(doc: Path, ex: dict[str, str]) -> int:
    """Rewrite each example block in place and fill each placeholder.
    Returns how many blocks were written."""
    import re
    text = doc.read_text()
    n = 0
    for name in ORDER:
        new = _block_of(name, ex[name])
        pat = re.compile(r"<!-- example: " + re.escape(name) + r" -->\n```\w*\n.*?\n```", re.S)
        if pat.search(text):
            text = pat.sub(lambda m: new, text, count=1)
            n += 1
        elif "{{example:" + name + "}}" in text:
            text = text.replace("{{example:" + name + "}}", new)
            n += 1
    doc.write_text(text)
    return n


def check(doc: Path, ex: dict[str, str]) -> list[str]:
    """The names of examples whose block in the doc differs from the code's."""
    text = doc.read_text()
    bad = []
    for name in ORDER:
        if _block_of(name, ex[name]) not in text:
            bad.append(name)
    return bad


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", metavar="DOC", help="compare the doc's example blocks with the code")
    ap.add_argument("--write", metavar="DOC", help="rewrite the doc's example blocks from the code")
    a = ap.parse_args(argv)
    ex = build()
    if a.write:
        print(f"{write(Path(a.write), ex)} examples written")
        return 0
    if a.check:
        bad = check(Path(a.check), ex)
        if bad:
            print("examples out of date: " + ", ".join(bad), file=sys.stderr)
            return 1
        print(f"{len(ORDER)} examples match")
        return 0
    print(markdown(ex), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())

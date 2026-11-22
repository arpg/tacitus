#!/usr/bin/env python3
"""Whether a reply may be drafted without the owner's word in this turn.

The scheduled lanes draft replies the owner did not ask for in that turn:
the remote briefing's plan and its reply queue, and the Claude morning
sweep. Whatever a model picked, code holds these rules there:

  1. The owner already decided the thread: a close of one of its rows in
     his words, or by his click (ledger.owner_decisions). When his words
     say no reply goes out, a follow-up gets no draft and one quiet
     question under the item's title: "They asked again. On Nov 14 you
     said no reply. Still no reply?". The Replies card's Not needed is
     that answer.
  2. The ask is his own choice: yes or no, join or not, one option or
     another (the thread record's `decide` state, or the ask's words). It
     becomes a decision with answer buttons; the draft comes after his
     answer.
  3. The reply says yes or no to taking part in something: a program, a
     committee, a panel, a board.
  4. The reply goes to many people: a reply-all that reaches
     MANY_RECIPIENTS others or more.
  5. The thread is kept from hosted models (keep_gate): on the machine,
     no scheduled lane drafts on it; at `facts`, only the local writer's
     body goes (the remote briefing), never a hosted model's.

A meeting request is the scheduling lane's: rules 2 and 3 never read a
thread whose record is a meeting request or a scheduling row, so a yes
with times to a one-to-one meeting is still drafted. Rules 1 and 4 hold
for every thread.

check() gives the remote briefing the decision for a candidate before it
asks the writer. cron_refusal() is the same rule at the last step, in
graph_actions create-reply (and mail_actions for Gmail) for every run whose
id is a scheduled lane's (`cron-...`): a reply a scheduled lane may not
draft is refused there whoever asks for it. A chat turn, where the owner
asked for the reply, is not gated here.

Pure but for cron_refusal's ledger read and keep check (rule 5, which
reads the owner's mail records and fails closed).
"""
from __future__ import annotations

import os
import re
from datetime import date

try:
    import mail_completion
except ModuleNotFoundError:
    from preprocessor import mail_completion

# A reply-all that reaches this many people besides the owner is his call.
MANY_RECIPIENTS = 10
CRON_PREFIX = "cron-"

# The ask is his choice. The record writes the ask about him in the third
# person ("Decide whether to join...", "Reply with a 'Yes' or 'No'...").
_YES_NO = re.compile(r"\byes['\"\u2019\u201d]?\s*(?:or|/)\s*['\"\u2018\u201c]?no\b", re.I)
_DECIDE = re.compile(
    r"\bdecid(?:e|ing)\b|\b(?:make|give|share|needs?) (?:a |your |his |their )?decision\b|"
    r"\b(?:accept|join)\s+or\s+(?:decline|not)\b|\bopt (?:in|out)\b",
    re.I)
_CHOICE = re.compile(
    r"\bwhether (?:or not )?(?:to|he|you|chris|the user)\b[^.]{0,40}?\b(?:will|would|wants?|can|"
    r"join|participat\w*|accept|attend|serve|agree|like|plans?|take part)\b|\brsvp\b|"
    r"\b(?:confirm|indicate|let \w+ know)\b[^.]{0,30}\b(?:participation|interest|attendance|"
    r"availability to serve)\b",
    re.I)
# A reply that says yes or no to an invitation to take part in something:
# a program, a committee, a panel, a board. A yes to a meeting ("happy to
# join the call", "I can attend on Tuesday at 2") is the scheduling lane's
# and is not this, and an invitation the owner sends ("I would like to
# invite you to serve") is neither.
_MEETING_WORDS = r"(?:call|meeting|zoom|teams|chat|discussion|session|lunch|coffee|visit|talk|seminar)"
_BODY_WORDS = (r"program\w*|pilot|committee|board|panel|council|cohort|initiative|consortium|"
               r"network|working group|task force|search|study|fellowship|role|position|advisory|"
               r"commission|jury|team")
_BODIES = f"(?:{_BODY_WORDS})"
_ANSWER = re.compile(
    r"\b(?:yes|happy to|glad to|delighted to|pleased to|honou?red to|would (?:like|love) to|"
    r"i(?:'d| would) (?:like|love) to|i will|i'll|i accept|i am in|i'm in|count me in|"
    r"no(?:,| thank)|unfortunately|regret|sorry|declin\w*|not able to|unable to|can(?:no|')t|"
    r"won'?t be able|will not|have to pass|must pass)\b(?:(?!invit)[^.]){0,60}?"
    r"(?:\b(?:participat\w*|take part|serve|be part of|be a part of|sit on|volunteer|sign up|"
    r"nominat\w*)\b(?!\s+(?:in |on |at |for )?(?:the |a |an |our |your |this |that )?"
    + _MEETING_WORDS + r"\b)"
    r"|\b(?:join|attend|accept|be on)\b(?:(?!\b" + _MEETING_WORDS + r"\b)[^.]){0,40}?\b"
    + _BODIES + r"\b)",
    re.I)
# "I accept your invitation to join the board", "I must decline the
# invitation to serve".
_ANSWER_INVITE = re.compile(
    r"\b(?:accept\w*|declin\w*)\b[^.]{0,20}\binvitation\b(?:(?!\b" + _MEETING_WORDS
    + r"\b)[^.]){0,50}?\b(?:" + _BODY_WORDS + r"|participat\w*|serve|take part)\b",
    re.I)
# An invitation to take part in something that is not a meeting: a team,
# a board, a program, a panel. An invitation to a repository, a document
# or a call is not this.
_INVITED = re.compile(
    r"\binvit\w*\b(?:(?!\b" + _MEETING_WORDS + r"\b)[^.]){0,80}?"
    r"(?:\b(?:participat\w*|serve|take part|be part|sit on|nominat\w*)\b"
    r"|\b(?:join|be on)\b(?:(?!\b" + _MEETING_WORDS + r"\b)[^.]){0,40}?\b" + _BODIES + r"\b)|"
    r"\b(?:would|will|do|are|is) (?:you|he|chris|the user) (?:like to|want to|be willing to|"
    r"be interested in|be able to|interested in|willing to)\s+(?:join|participat\w*|serve|take part|"
    r"be part|chair|lead)\b",
    re.I)
_DECLINE = re.compile(r"\b(declin\w*|turn (it|this|them) down|not able to|unable to|can(no|')t)\b", re.I)


def autonomous_run(run_id: str) -> bool:
    """A scheduled lane's run (run-agent.sh labels it `cron-<agent>-<ts>`):
    no owner asked for its drafts in that turn."""
    return str(run_id or "").startswith(CRON_PREFIX)


def _meeting(scheduling: str, state: str) -> bool:
    return str(scheduling or "none").strip().lower() not in ("", "none") or state == "scheduling"


def _keep_gate():
    try:
        import keep_gate
    except ModuleNotFoundError:  # imported as preprocessor.<mod>
        from preprocessor import keep_gate
    return keep_gate


def meeting_row(row: dict) -> bool:
    """A ledger row about placing a meeting: a row of a type that matches
    the calendar (ledger.CALENDAR_MATCH_TYPES), a sched- row, or a thread
    whose record is a meeting request."""
    try:
        import ledger as _ledger
    except ModuleNotFoundError:  # imported as preprocessor.<mod>
        from preprocessor import ledger as _ledger
    brief = row.get("thread_brief") if isinstance(row.get("thread_brief"), dict) else {}
    return (_ledger.canonical_type(row.get("type")) in _ledger.CALENDAR_MATCH_TYPES
            or str(row.get("id") or "").startswith("sched-")
            or _meeting(str(brief.get("scheduling") or "none"), str(brief.get("state") or "")))


def _brief_states(brief: dict | None) -> list[str]:
    """The states of the other side's messages since the owner last wrote
    in the thread, and the thread's own."""
    if not isinstance(brief, dict):
        return []
    out = [str(brief.get("state") or "")]
    for m in brief.get("messages") or []:
        if not isinstance(m, dict):
            continue
        if m.get("own"):
            out = [str(brief.get("state") or "")]
        else:
            out.append(str(m.get("state") or ""))
    return out


def owner_choice(ask: str, state: str = "", scheduling: str = "none", brief: dict | None = None) -> bool:
    """The answer the thread wants is the owner's choice: the record says
    `decide` (the thread's, or a message's since he last wrote), or the
    ask is yes or no, whether, accept or decline, or an invitation to join
    something. Never for a meeting request (the thread's `scheduling` or
    `scheduling` state): placing a meeting is the scheduling lane's."""
    # A meeting request is the scheduling lane's, whatever its words:
    # "decide on a time", "yes or no for Tuesday" are the solver's.
    if _meeting(scheduling, state):
        return False
    if state == "decide" or "decide" in _brief_states(brief):
        return True
    asks = " ".join(x for x in [ask or "", str((brief or {}).get("ask") or "")] if x)
    return bool(_YES_NO.search(asks) or _DECIDE.search(asks) or _CHOICE.search(asks)
                or _INVITED.search(asks))


def answers_invitation(text: str) -> str:
    """'accepts' or 'declines' when the reply says yes or no to an
    invitation (to join, take part, serve, attend), else ''."""
    m = _ANSWER.search(str(text or "")) or _ANSWER_INVITE.search(str(text or ""))
    if not m:
        return ""
    return "declines" if _DECLINE.search(m.group(0)) or re.match(
        r"(?i)no\b|unfortunately|regret|sorry|won'?t|will not|have to pass|must pass", m.group(0)) \
        else "accepts"


def recipients(item: dict, own=()) -> int | None:
    """How many people a reply-all to the item reaches besides the owner:
    its sender, To and Cc. None when the item does not say."""
    to, cc = item.get("to"), item.get("cc")
    if not isinstance(to, list) and not isinstance(cc, list):
        return None
    mine = {str(a).lower() for a in own if a}
    people = {str(a).strip().lower() for a in [item.get("from")] + list(to or []) + list(cc or [])
              if a and str(a).strip()}
    return len(people - mine)


def _day(at: str) -> str:
    try:
        d = date.fromisoformat(str(at or "")[:10])
    except ValueError:
        return ""
    return f"{d:%b} {d.day}"


def no_reply_said(owner_said) -> dict | None:
    """The newest of the owner's decisions on the thread that says no
    reply goes out, or None."""
    return next((d for d in owner_said or () if isinstance(d, dict) and d.get("no_reply")), None)


def _clip(text, cap: int) -> str:
    t = " ".join(str(text or "").split()).replace("—", ", ").replace("*", "").replace("`", "")
    if len(t) <= cap:
        return t
    cut = t[:cap - 1]
    return (cut[:cut.rfind(" ")] if " " in cut else cut).rstrip(" ,;:") + "…"


def yes_no(ask: str) -> bool:
    return bool(re.search(r"\byes['\"’”]?\s*(?:or|/)\s*['\"‘“]?no\b", ask or "", re.I))


def check(item: dict, says: str = "", *, own=()) -> dict | None:
    """None when a scheduled lane may draft the reply `says` for `item` (a
    remote briefing candidate: title, ask, state, scheduling, brief,
    owner_said, kind, to/cc, keep_level). Else the decision for the owner:
    {"rule", "why" (a few words), "line" (the question, one line),
    "choices" (answers for buttons, maybe none)}. The line never carries
    the answer a model proposed: the answer is his to give."""
    title = _clip(item.get("title") or "this thread", 90)
    ask = str(item.get("ask") or "")
    if mail_completion.non_email_work(item):
        return {"rule": "non_email", "why": "the request needs work, not an email reply",
                "line": ask or "Complete the requested work; keep it open until it is delivered.",
                "choices": []}
    brief = item.get("brief") if isinstance(item.get("brief"), dict) else None
    if brief is None and isinstance(item.get("thread"), list):
        # A reply-queue entry: its last messages, each with its state.
        brief = {"messages": item["thread"]}
    said = no_reply_said(item.get("owner_said"))
    if said:
        when = _day(said.get("at"))
        return {"rule": "no_reply", "why": "you said no reply earlier", "choices": [],
                "line": f"They asked again. {'On ' + when + ' you' if when else 'Earlier you'} "
                        "said no reply. Still no reply?"}
    question = f"They ask: {_clip(ask, 140)}" if ask else f"Your answer on {title}"
    if item.get("keep_level") == "local":
        # Rule 5: no scheduled run drafts on a thread kept on the machine.
        return {"rule": "kept", "why": "kept on your own model", "choices": [],
                "line": f"Reply needs you (kept on your own model): {question}"}
    picks = ["Yes", "No"] if yes_no(ask) else []
    state, scheduling = str(item.get("state") or ""), str(item.get("scheduling") or "none")
    if owner_choice(ask, state, scheduling, brief):
        return {"rule": "owner_choice", "why": "the answer is your decision", "line": question,
                "choices": picks}
    answer = "" if _meeting(scheduling, state) else answers_invitation(says)
    if answer:
        return {"rule": "invitation", "why": f"it {answer} an invitation", "line": question,
                "choices": picks}
    n = recipients(item, own)
    if n is not None and n >= MANY_RECIPIENTS:
        return {"rule": "many", "why": f"a reply to all {n} people on it", "line": question,
                "choices": picks}
    return None


# ── the last step: create-reply in a scheduled lane ───────────────────

def cron_refusal(*, run_id: str, conversation_id: str = "", ledger_ids=(), body: str = "",
                 recipient_count: int | None = None, decisions=None, rows=None,
                 keep_records=None, local_writer: bool | None = None) -> str:
    """'' when a scheduled lane may make this reply draft, else why not, in
    plain words. `rows` (the ledger rows the draft names, as
    ledger.get_entry gives them) and `decisions` (the owner's on the
    conversation and on those rows' conversations) are read from the
    ledger when not given; `keep_records` stands in for the mail's
    records in the keep check (keep_gate.levels). `local_writer` says the
    body is the local writer's (the remote briefing sets
    keep_gate.LOCAL_WRITER_ENV for its own create-reply; read from the
    environment when not given). A chat turn's run is never refused here."""
    if not autonomous_run(run_id):
        return ""
    if decisions is None or rows is None:
        try:
            import ledger as _ledger
        except ModuleNotFoundError:  # imported as preprocessor.<mod>
            from preprocessor import ledger as _ledger
        if rows is None:
            rows = []
            for lid in ledger_ids or ():
                try:
                    row = _ledger.get_entry(lid)
                except Exception:
                    row = None
                if row:
                    rows.append(row)
        if decisions is None:
            convs = {conversation_id} | {_ledger._conversation_of(r) for r in rows}
            try:
                found = _ledger.owner_decisions(convs)
            except Exception:
                found = {}
            decisions = [d for c in convs for d in found.get(c) or []]
    if any(mail_completion.non_email_work(r) for r in rows or ()):
        return "this request requires completing and delivering work, not an email reply"
    said = no_reply_said(list(decisions or []) + [d for r in rows or () for d in r.get("owner_said") or []])
    if said:
        when = _day(said.get("at"))
        return (f"the owner said no reply on this thread{' on ' + when if when else ''} "
                f"(\"{_clip(said.get('said'), 100)}\")")
    # Rule 5 (keep_gate, failing closed): no scheduled lane drafts on a
    # thread kept on the machine; on a `facts` thread only the local
    # writer's body goes, never a hosted model's.
    kg = _keep_gate()
    if local_writer is None:
        local_writer = os.environ.get(kg.LOCAL_WRITER_ENV) == "local"
    kept = kg.refusal(rows or (), conversation_id=conversation_id, records=keep_records,
                      local_writer=local_writer)
    if kept:
        return kept
    meeting = False
    for row in rows or ():
        brief = row.get("thread_brief") if isinstance(row.get("thread_brief"), dict) else None
        if meeting_row(row):
            meeting = True
            continue
        if brief and owner_choice(str(brief.get("ask") or ""), str(brief.get("state") or ""),
                                  str(brief.get("scheduling") or "none"), brief):
            return "the answer is the owner's decision"
    answer = "" if meeting else answers_invitation(body)
    if answer:
        return f"the reply {answer} an invitation"
    if recipient_count is not None and recipient_count >= MANY_RECIPIENTS:
        return f"a reply to all {recipient_count} people on the thread"
    return ""


NEEDS_OWNER_HINT = ("A scheduled run may not draft this reply. Record the question for the owner "
                    "instead: ledger.py note-reply --id <row> --outcome decision --note \"<the "
                    "question, one line>\" (with --choice for two or three short answers). Do not "
                    "retry; --force does not apply here.")

#!/usr/bin/env python3
"""Pre-send open-claim audit: "are these really open?"

A briefing asserts which ledger items are open. The closure scrubber
catches most finished ones before the briefing runs, but the briefing
skill cannot see mailbox facts that are not in its prompt (a mail to the
person named in a row, a "Declined:" for the invite a row asks about,
archived reminders for every id a row names). This module asks the
question in code, per item, after the draft validates and before it
posts:

  prepare  read the draft the agent produced, find every open ledger row
           it presents as open (its id, its today_top slot, or two of its
           distinctive words), gather the contrary evidence the scrubber's
           own helpers can find for each (the user's sent mail on the
           topic, inbound on the topic, calendar events, closed rows that
           carry the same manuscript/proposal id), and write one short
           prompt covering only the items that HAVE such evidence.
  apply    read the verdicts (JSON), close the rows judged done (through
           `ledger.py close`, so the email is archived and the audit note
           lands in last_action), record each in the missed-closure log
           (the scrubber backlog, source "pre-send-audit"), append the
           run to ~/.ea/logs/open-claims-audit.jsonl, and write the note
           that makes the agent rewrite its draft.

Open is not enough: a row the draft names that a future follow_up_after
or snooze_until parks is not actionable yet. prepare lists it under
`parked` with no question asked (the date decides, not a model), and
apply tells the rewrite to drop it from the post.

run-agent.sh runs prepare -> a second, tool-less Claude call (only when
some row has evidence) -> apply -> one rewrite of the draft, for the
three agents that assert what is open (morning-briefing,
afternoon-catchup, weekly-audit). Nothing here posts.

A row the user confirmed "still open" through the dashboard (item
feedback, `confirmed_open_at`) is not asked about again for
CONFIRMED_OPEN_DAYS.

Usage:
    python3 open_claims_audit.py prepare --agent morning-briefing \
        --post-file draft.txt --briefing briefing-latest.json \
        --out audit.json --prompt-file prompt.txt [--no-fetch]
    python3 open_claims_audit.py apply --audit audit.json \
        --verdicts-file verdicts.txt --note-file note.txt [--no-close]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import closure_scrubber as cs  # noqa: E402
import ledger as ledger_mod  # noqa: E402
import log_missed_closure as lmc  # noqa: E402
import resident_io  # noqa: E402

EA_DIR = Path(os.environ.get("EA_DIR", str(Path.home() / ".ea")))
AUDIT_LOG = EA_DIR / "logs" / "open-claims-audit.jsonl"

AUDIT_AGENTS = ("morning-briefing", "afternoon-catchup", "weekly-audit")
CONFIRMED_OPEN_DAYS = 7
MAX_CLAIMS = 12          # the prompt stays short; the queue is five items
MAX_RENDERED = 20        # rows the post itself shows: every one is asked
MAX_EVIDENCE = 8         # lines per item
RARE_DF = 2              # a word at most this many open rows share is distinctive
VERDICTS = ("open", "done", "unsure")


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── which rows does the draft present as open? ────────────────────────

# Words that are rare across ledger rows but common in briefing PROSE.
# Without them a row matches on words drawn from different lines of the
# draft, none of which is about that row.
_PROSE_NOISE: set[str] = {
    "already", "message", "messages", "inbox", "monitor", "pattern", "third",
    "answers", "answer", "declined", "accepted", "accept", "submission",
    "reviewer", "reviews", "party", "staged", "draft", "drafts", "thread",
    "threads", "reply", "replied", "waiting", "worth", "still", "session",
    "dedicated", "confirm", "confirmed", "overdue", "blocker", "minimum",
    "decide", "decision", "queue", "untangling", "slice", "handled",
    "quietly", "flexes", "gap", "gaps", "morning", "afternoon", "today",
    "tomorrow", "week", "weeks", "days", "hours", "email", "emails",
    "invite", "invites", "response", "responses", "campaign", "program",
    "proposal", "letter", "section", "items", "item", "bundle",
}


_EMOJI_CODE_RE = re.compile(r":[a-z0-9_+-]+:")


def _post_lines(post: str) -> list[set[str]]:
    """Token bag per line of the draft: a row is mentioned by ONE line,
    never by two words that happen to sit in different bullets. Emoji
    shortcodes (':white_check_mark:') are markup, not words."""
    return [cs._extract_content_tokens(_EMOJI_CODE_RE.sub(" ", ln))
            for ln in (post or "").splitlines() if ln.strip()]


_IDENTIFIER_TOKEN_RE = re.compile(r"^(?:\d{7,}|[a-z]{2,8}-\d{2,4}-\d{3,6}|[a-z]{2,5}\d{5,})$")


def _is_identifier(tok: str) -> bool:
    """A proposal number, a manuscript id, a ticket: one of these on a
    line of the draft is a mention by itself."""
    return bool(_IDENTIFIER_TOKEN_RE.match(tok))


def _distinctive(entry: dict, df: dict[str, int]) -> set[str]:
    """Words of this row that few other open rows use and that prose does
    not use for everything. Short words and the generic vocabulary never
    identify a row; a four-letter acronym does when no other row has it;
    an identifier always does."""
    toks = cs._entry_match_tokens(entry) - cs._GENERIC_NOISE_TOKENS - _PROSE_NOISE
    out: set[str] = set()
    for t in toks:
        if _is_identifier(t):
            out.add(t)
        elif t.isdigit():
            continue
        elif len(t) >= 5 and df.get(t, 0) <= RARE_DF:
            out.add(t)
        elif len(t) == 4 and df.get(t, 0) <= 1:
            out.add(t)
    return out


def why_mentioned(entry: dict, post: str, post_lines: list[set[str]],
                  df: dict[str, int], today_refs: set[str],
                  min_tokens: int = 2) -> str:
    """'' when the draft does not present this row as open; else why we
    think it does: its id, its today_top slot, or `min_tokens` of its own
    words on one line of the draft (a person's name alone is not a
    mention)."""
    eid = entry.get("id") or ""
    if eid and eid in post:
        return "id"
    if eid and eid in today_refs:
        return "today_top"
    distinct = _distinctive(entry, df)
    if not distinct:
        return ""
    person = cs._person_tokens(entry.get("from_name") or "") | cs._person_tokens(
        " ".join(n for n in _named_people_of(entry)))
    best: set[str] = set()
    for line in post_lines:
        hits = distinct & line
        if any(_is_identifier(h) for h in hits):
            return "identifier:" + ",".join(sorted(h for h in hits if _is_identifier(h))[:2])
        if len(hits) > len(best):
            best = hits
    if len(best) >= min_tokens and (best - person):
        return "tokens:" + ",".join(sorted(best)[:4])
    return ""


def _named_people_of(entry: dict) -> set[str]:
    return cs._named_people(entry.get("description") or "")


def _recently_confirmed_open(entry: dict, now: datetime) -> bool:
    raw = entry.get("confirmed_open_at") or ""
    if not raw:
        return False
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    return (now - ts) < timedelta(days=CONFIRMED_OPEN_DAYS)


def extract_claims(post: str, open_entries: list[dict],
                   today_top: list[dict] | None = None,
                   now: datetime | None = None) -> list[dict]:
    """The open rows the draft asserts, each with why we think so.

    A row parked by a future follow_up_after or snooze_until that the
    draft names comes back with `parked_until` set, whatever its status
    and whatever the user confirmed: it is not actionable yet, so the
    question is not "is it done?" but "why is it in the post?". Parked
    claims come first and do not count against MAX_CLAIMS."""
    now = now or datetime.now(timezone.utc)
    local_today = now.astimezone().date()
    post = post or ""
    post_lines = _post_lines(post)
    df = cs.build_token_df(open_entries)
    today_refs = {str(t.get("ref") or "") for t in (today_top or [])
                  if t.get("source") == "obligation"}
    claims: list[dict] = []
    parked: list[dict] = []
    for e in open_entries:
        until = ledger_mod.parked_until(e, local_today)
        if not until and (e.get("status") or "open") in ("snoozed", "follow_up"):
            continue
        # Dropping a row is an edit to the post, so a parked row needs a
        # firmer mention than a question does: three of its words on one
        # line, not two prose words that happen to match.
        why = why_mentioned(e, post, post_lines, df, today_refs,
                            min_tokens=3 if until else 2)
        if not why:
            continue
        if until:
            parked.append({"id": e.get("id") or "", "why_mentioned": why,
                           "entry": e, "parked_until": until})
            continue
        if _recently_confirmed_open(e, now):
            continue
        claims.append({"id": e.get("id") or "", "why_mentioned": why,
                       "entry": e})
    # today_top rows first, then by id, and never more than the prompt
    # can carry: the queue is what the user reads first.
    claims.sort(key=lambda c: (0 if c["why_mentioned"] == "today_top" else
                               1 if c["why_mentioned"] == "id" else 2,
                               c["id"]))
    parked.sort(key=lambda c: c["id"])
    return parked + claims[:MAX_CLAIMS]


# ── what says otherwise? ──────────────────────────────────────────────

def _addr(rec: dict) -> str:
    return (((rec or {}).get("emailAddress") or {}).get("address") or "").strip().lower()


def _people_on(msg: dict) -> tuple[set[str], set[str]]:
    """(addresses, lowercased display names) of everyone on a message."""
    addrs: set[str] = set()
    names: set[str] = set()
    for field in ("from", "toRecipients", "ccRecipients"):
        recs = msg.get(field) or []
        if isinstance(recs, dict):
            recs = [recs]
        for r in recs:
            a = _addr(r)
            if a:
                addrs.add(a)
            n = " ".join((((r or {}).get("emailAddress") or {}).get("name") or "").lower().split())
            if n:
                names.add(n)
    return addrs, names


def evidence_for(entry: dict, *, inbox: list, sent: list, events: list,
                 closed_index: dict[str, list] | None = None,
                 closed_rows: list[dict] | None = None,
                 df: dict[str, int] | None = None,
                 archive: list | None = None) -> list[str]:
    """Contrary evidence for one row, at the scrubber's own standard: two
    distinctive words of the row in a subject, or one word when the mail
    is with the row's own correspondent (by address, or by a name the
    row spells out); a meeting response only when the row is RSVP-shaped
    and quotes the invite; closed rows that carry the same manuscript or
    proposal id. Every line names its date, so the model can see whether
    it postdates the row.

    Mail in the row's own thread after the row, the user's or anyone's,
    is evidence whatever its words, and so is mail from the person a
    waiting row waits on: their answer is how that row ends. Archived
    mail counts as inbound, since the user often archives the answer the
    row waited for. A row closed since this one was made, in the same
    thread or with the same correspondent, comes with its notes: that is
    where the answer's facts were written down."""
    lines: list[str] = []
    # (rank, line): a closed row's notes, then the row's own thread, then
    # the waited-on person's mail on the topic, then the rest of theirs
    strong: list[tuple[int, str]] = []
    distinct = _distinctive(entry, df or {})
    entry_date = cs._entry_iso(entry)
    own_addr = (entry.get("from_email") or "").strip().lower()
    conv = entry.get("conversation_id") or ""
    waiting = ledger_mod.is_waiting(entry)
    named = _named_people_of(entry)
    text = f"{entry.get('description') or ''} {(entry.get('notes') or '')[:400]}"
    rsvp_shaped = bool(cs._RSVP_SHAPE_RE.search(text))
    text_loose = cs._loose_text(text)

    def overlap_ok(subject: str, addrs: set[str], names: set[str]) -> set[str]:
        hits = distinct & cs._extract_content_tokens(subject)
        if len(hits) >= 2:
            return hits
        with_person = (own_addr and own_addr in addrs) or bool(named & names)
        return hits if (hits and with_person) else set()

    for m in (sent or [])[:300]:
        ts = m.get("sentDateTime") or m.get("receivedDateTime") or ""
        if entry_date and ts and ts < entry_date:
            continue
        subject = (m.get("subject") or "").strip()
        addrs, names = _people_on(m)
        low = subject.lower()
        prefix = next((p for p in cs._MEETING_RESPONSE_PREFIXES if low.startswith(p)), "")
        if prefix:
            title = cs._loose_text(subject[len(prefix):])
            if rsvp_shaped and len(title) >= 8 and f" {title} " in f" {text_loose} ":
                lines.append(f"sent {ts}: subject={subject[:80]!r} (the invite this row names)")
            continue
        to = ", ".join(sorted(a for a in addrs if a != _addr(m.get("from") or {})))[:90]
        if conv and m.get("conversationId") == conv:
            strong.append((1, f"sent {ts}: in this row's thread, subject={subject[:80]!r} to={to}"))
            continue
        if cs._is_auto_generated_subject(subject):
            continue
        hits = overlap_ok(subject, addrs, names)
        if hits:
            lines.append(f"sent {ts}: subject={subject[:80]!r} to={to} overlap={sorted(hits)[:4]}")

    mail = [("inbox", m) for m in (inbox or [])[:300]] + \
           [("archived", m) for m in (archive or [])[:300]]
    for where, m in mail:
        ts = m.get("receivedDateTime") or ""
        if entry_date and ts and ts < entry_date:
            continue
        subject = (m.get("subject") or "").strip()
        frm = _addr(m.get("from") or {})
        if conv and m.get("conversationId") == conv:
            strong.append((1, f"{where} {ts}: in this row's thread, subject={subject[:80]!r} from={frm}"))
            continue
        if waiting and own_addr and frm == own_addr:
            on_topic = bool(distinct & cs._extract_content_tokens(subject))
            strong.append((2 if on_topic else 3, f"{where} {ts}: from the person this row "
                                                 f"waits on, subject={subject[:80]!r}"))
            continue
        if cs._is_auto_generated_subject(subject):
            continue
        addrs, names = _people_on(m)
        hits = overlap_ok(subject, addrs, names)
        if hits:
            lines.append(f"{where} {ts}: subject={subject[:80]!r} from={frm} overlap={sorted(hits)[:4]}")

    eid = entry.get("id") or ""
    for r in closed_rows or []:
        closed_at = r.get("closed_at") or ""
        if r.get("id") == eid or not closed_at or (entry_date and closed_at < entry_date):
            continue
        same_thread = bool(conv) and r.get("conversation_id") == conv
        same_person = bool(own_addr) and (r.get("from_email") or "").lower() == own_addr
        if not (same_thread or same_person):
            continue
        notes = " ".join((r.get("notes") or "").split())[:240]
        strong.append((0, f"closed row {closed_at[:16]}: {r.get('id')} "
                          f"{(r.get('description') or '')[:80]!r}"
                          + (" (same thread)" if same_thread else " (same correspondent)")
                          + (f" notes={notes!r}" if notes else "")))

    for ev in (events or [])[:200]:
        subject = ev.get("subject") or ""
        hits = distinct & cs._extract_content_tokens(subject)
        if len(hits) >= 2:
            start = ((ev.get("start") or {}).get("dateTime") or "") if isinstance(ev.get("start"), dict) else str(ev.get("start") or "")
            lines.append(f"calendar event {start[:16]}: subject={subject[:80]!r} overlap={sorted(hits)[:4]}")

    keys = set()
    if closed_index:
        keys, _uncovered, _ns = cs.sibling_keys(entry.get("description") or "", closed_index)
    if keys and closed_index:
        by_id = {r.get("id"): r for r in (closed_rows or [])}
        for k in sorted(keys):
            for ts, human, sid in closed_index.get(k, []):
                if not human or (entry_date and ts and ts < entry_date):
                    continue
                row = by_id.get(sid) or {}
                lines.append(f"closed row {ts}: {sid} "
                             f"{(row.get('description') or '')[:70]!r} (same id {k})")
    # dedupe, keep order (the row's own thread and person first), cap
    seen: set[str] = set()
    out: list[str] = []
    for ln in [ln for _rank, ln in sorted(strong, key=lambda x: x[0])] + lines:
        if ln in seen:
            continue
        seen.add(ln)
        out.append(ln)
        if len(out) >= MAX_EVIDENCE:
            break
    return out


# ── the question ──────────────────────────────────────────────────────

def build_prompt(agent: str, claims: list[dict]) -> str:
    """One short prompt: the rows the post shows, and the other rows it
    names that have evidence."""
    lines = [
        f"You are the pre-send check for the EA's {agent}. A draft message is "
        "about to be posted. It presents the ledger items below as OPEN. "
        "For each item, decide from the evidence whether it is still open.",
        "",
        "Rules:",
        "- Answer 'done' only when the evidence shows the obligation itself was "
        "discharged: the user replied to that person about that topic, accepted "
        "or declined that invite, sent that deliverable, or every reminder about "
        "it was closed.",
        "- A 'waiting' row waits on someone else. It is done when that person "
        "answered with what the row waits for: their mail after the row, or a "
        "closed row whose notes record their answer.",
        "- The row's own last action and notes are evidence too: they can say the "
        "deliverable went out.",
        "- A related thread, a meeting about the topic, or a matching name is NOT "
        "completion. When the evidence is ambiguous, answer 'unsure'.",
        "- Use only the evidence listed. Do not invent evidence. You have no tools.",
        "",
        "Items:",
    ]
    for n, c in enumerate(claims, 1):
        e = c["entry"]
        lines.append(f"[{n}] id: {c['id']} | type: {e.get('type') or ''} | "
                     f"created: {(e.get('date_identified') or '')[:10]} | "
                     f"deadline: {e.get('deadline') or '-'}")
        lines.append(f"    claim: {(e.get('description') or '')[:300]}")
        if e.get("from_email"):
            lines.append(f"    correspondent: {e['from_email']}")
        la = " ".join((e.get("last_action") or "").split())
        if la:
            lines.append(f"    last action: {la[:220]}")
        notes = " ".join((e.get("notes") or "").split())
        if notes:
            lines.append(f"    notes: {notes[:300]}")
        lines.append("    evidence:")
        for ev in c.get("evidence") or []:
            lines.append(f"      - {ev}")
        if not c.get("evidence"):
            lines.append("      - (no mail, calendar or closed-row evidence)")
    lines += [
        "",
        "Answer with ONE JSON object and nothing else:",
        '{"verdicts": [{"id": "<id>", "verdict": "open|done|unsure", '
        '"why": "<one sentence that cites the evidence line>"}]}',
    ]
    return "\n".join(lines)


_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_verdicts(text: str) -> list[dict]:
    """The verdict list out of a model reply, tolerant of code fences and
    prose around the JSON. Unknown verdicts read as 'unsure'."""
    m = _JSON_RE.search(text or "")
    if not m:
        return []
    raw = m.group(0)
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        # a trailing fence or comma: try the outermost balanced object
        depth, end = 0, -1
        for i, ch in enumerate(raw):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        try:
            obj = json.loads(raw[:end + 1]) if end > 0 else {}
        except json.JSONDecodeError:
            return []
    out: list[dict] = []
    for v in (obj.get("verdicts") or []) if isinstance(obj, dict) else []:
        if not isinstance(v, dict) or not v.get("id"):
            continue
        verdict = str(v.get("verdict") or "").strip().lower()
        out.append({"id": str(v["id"]), "verdict": verdict if verdict in VERDICTS else "unsure",
                    "why": " ".join(str(v.get("why") or "").split())[:300]})
    return out


# ── prepare ───────────────────────────────────────────────────────────

def _rendered_claims(rendered: list[str], open_entries: list[dict], found: list[dict],
                     now: datetime | None) -> list[dict]:
    """Claims for the rows a renderer says its post shows, by id. A row
    the text guess already found keeps its entry; a parked row stays with
    the parked claims; a row the user confirmed open this week is not
    asked again."""
    now = now or datetime.now(timezone.utc)
    by_id = {e.get("id"): e for e in open_entries if e.get("id")}
    parked = {c["id"] for c in found if c.get("parked_until")}
    out: list[dict] = []
    for rid in dict.fromkeys(r for r in rendered if r):
        e = by_id.get(rid)
        if e is None or rid in parked or _recently_confirmed_open(e, now):
            continue
        out.append({"id": rid, "why_mentioned": "rendered", "entry": e})
    return out[:MAX_RENDERED]


def _fetch_archive(days: int = 14) -> tuple[list, str]:
    """The Archive folder's recent mail: the user archives the answer a
    row waited for, so the inbox alone hides it."""
    try:
        import graph_client as graph
        return graph.get_messages("archive", days_back=days), "graph_client"
    except Exception as e:
        direct = f"{type(e).__name__}"
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    payload, err = lmc._graph_cli_json("list-mail", "--folder", "archive",
                                       "--top", "200", "--since", since)
    if err:
        return [], f"unavailable ({direct}; {err})"
    return lmc._normalize_cli_messages(payload), "CLI"


def without_kept(msgs: list) -> list:
    """`msgs` less the mail kept from Anthropic (mail_privacy's keep
    list): the verdict may run on Claude. Fails closed: when the gate
    cannot say, no mail goes."""
    if not msgs:
        return []
    try:
        import mail_privacy
        import remote_llm
        rows, _refusal = mail_privacy._prune_kept(list(msgs), mail_privacy.load_index(),
                                                  remote_llm.config())
        return [m for m in rows if isinstance(m, dict) and not m.get("_note")]
    except Exception as e:
        print(f"open_claims_audit: kept-mail filter failed, no mail evidence: {str(e)[:120]}",
              file=sys.stderr)
        return []


def prepare(*, agent: str, post: str, briefing: dict | None,
            open_entries: list[dict] | None = None,
            inbox: list | None = None, sent: list | None = None,
            events: list | None = None, closed_rows: list[dict] | None = None,
            no_fetch: bool = False, now: datetime | None = None,
            rendered: list[str] | None = None, archive: list | None = None,
            claude_safe: bool = False) -> dict:
    """The audit record before the question is asked. Injectable inputs
    keep the tests off Graph; the CLI fetches what is not given.

    `rendered` is the ledger ids a renderer knows its post shows (the
    remote briefing renders from refs, so it does not have to guess from
    words). Each is asked whether or not mail evidence turned up: its own
    notes and last action can say it is done. `claude_safe` drops kept
    mail from the evidence before the prompt is written."""
    open_entries = open_entries if open_entries is not None else ledger_mod.get_open_entries()
    today_top = (briefing or {}).get("today_top") or []
    found = extract_claims(post, open_entries, today_top, now=now)
    shown = _rendered_claims(rendered or [], open_entries, found, now)
    shown_ids = {c["id"] for c in shown}
    claims = shown + [c for c in found if not c.get("parked_until") and c["id"] not in shown_ids]
    record = {
        "ts": _now_iso(), "agent": agent, "post_chars": len(post or ""),
        "open_rows": len(open_entries), "claims": [], "asked": 0,
        "parked": [{"id": c["id"], "why_mentioned": c["why_mentioned"],
                    "parked_until": c["parked_until"],
                    "description": (c["entry"].get("description") or "")[:160]}
                   for c in found if c.get("parked_until")],
        "fetch": "", "prompt": "",
    }
    if not claims:
        return record
    if inbox is None and sent is None and events is None:
        if no_fetch:
            inbox, sent, events = [], [], []
            record["fetch"] = "skipped (--no-fetch)"
        else:
            inbox, sent, events, status = lmc._fetch_evidence()
            record["fetch"] = status.get("route", "")
    if archive is None:
        if no_fetch:
            archive = []
        else:
            archive, route = _fetch_archive()
            record["fetch_archive"] = route
    if claude_safe:
        inbox, sent, archive = without_kept(inbox), without_kept(sent), without_kept(archive)
    if closed_rows is None:
        try:
            closed_rows = ledger_mod.get_closed_entries_brief()
        except Exception:
            closed_rows = []
    closed_index = cs.build_closed_identifier_index(closed_rows)
    df = cs.build_token_df(open_entries)
    asked: list[dict] = []
    for c in claims:
        ev = evidence_for(c["entry"], inbox=inbox or [], sent=sent or [],
                          events=events or [], closed_index=closed_index,
                          closed_rows=closed_rows, df=df, archive=archive or [])
        c["evidence"] = ev
        record["claims"].append({"id": c["id"], "why_mentioned": c["why_mentioned"],
                                 "evidence": ev,
                                 "description": (c["entry"].get("description") or "")[:160]})
        if ev or c["why_mentioned"] == "rendered":
            asked.append(c)
    record["asked"] = len(asked)
    if asked:
        record["prompt"] = build_prompt(agent, asked)
    return record


# ── apply ─────────────────────────────────────────────────────────────

def _close_row(ledger_id: str, note: str) -> dict:
    """Propose the close through the ledger CLI. A model's verdict is not
    decisive evidence (closure.py): the row stays open with the verdict on
    it, and the user confirms on the card in one click."""
    r = subprocess.run(
        [sys.executable, str(_HERE / "ledger.py"), "propose", "--id", ledger_id,
         "--signal", "llm:open-claims-audit", "--why", note],
        capture_output=True, text=True, timeout=120)
    try:
        out = json.loads((r.stdout or "").strip().splitlines()[-1])
    except Exception:
        out = {}
    ok = r.returncode == 0 and bool(out.get("ok"))
    return {"ok": ok, "error": ("" if ok else (out.get("error") or r.stderr or "propose failed")[:200])}


def _human_date(iso: str) -> str:
    try:
        d = datetime.strptime(iso[:10], "%Y-%m-%d")
    except ValueError:
        return iso
    return f"{d:%a %b} {d.day}"


def build_note(done: list[dict], unsure: list[dict],
               parked: list[dict] | None = None) -> str:
    """The text appended to the agent's prompt for its one rewrite."""
    parked = parked or []
    if not done and not unsure and not parked:
        return ""
    lines = ["PRE-SEND AUDIT RESULT: a code check ran on your previous draft. "
             "That draft is NOT posted. Write the full message again with these "
             "corrections, everything else unchanged."]
    if parked:
        lines.append("")
        lines.append("These items are parked in the ledger. The user cannot act on "
                     "them before the date shown. Remove each one from the message: "
                     "from the queue, the flexes and every other section. Do not "
                     "mention them at all. Each comes back by itself on its date. "
                     "If an item below is not in the message, change nothing for it:")
        for p in parked:
            lines.append(f"  - {p['id']}: {p['description'][:100]} -- not before "
                         f"{_human_date(p['parked_until'])}")
    if done:
        lines.append("")
        lines.append("These items were presented as open. The evidence says they look "
                     "done; the ledger holds them for the user's one-click confirm, and "
                     "the bot posts that card. Take them out of the queue and every "
                     "other section, and list each on one 'Looks done (confirm in the "
                     "card)' line with its evidence, in a few words:")
        for d in done:
            lines.append(f"  - {d['id']}: {d['description'][:100]} -- {d['why']}")
    if unsure:
        lines.append("")
        lines.append("These items have ambiguous evidence. Keep them, and add the "
                     "evidence to their line in one short clause ending with "
                     "'still open?', so the user can confirm or close:")
        for u in unsure:
            lines.append(f"  - {u['id']}: {u['description'][:100]} -- {u['why']}")
    return "\n".join(lines)


def apply(*, audit: dict, verdicts: list[dict], close: bool = True,
          log_missed: bool = True, fetch_for_log: bool = True,
          log_path: Path | None = None) -> dict:
    """Act on the verdicts. Returns the summary; `note` is what the agent
    gets for its rewrite ('' when nothing changes)."""
    # Only rows that were ASKED about (they had evidence) may be acted on.
    # A verdict about any other row is the model inventing a question.
    by_id = {c["id"]: c for c in audit.get("claims") or [] if c.get("evidence")}
    done: list[dict] = []
    unsure: list[dict] = []
    failed: list[dict] = []
    agent = audit.get("agent") or ""
    for v in verdicts:
        c = by_id.get(v["id"])
        if not c:
            print(f"open_claims_audit: ignoring a verdict for {v['id']!r}, "
                  f"which was not asked about", file=sys.stderr)
            continue
        row = {"id": v["id"], "why": v.get("why") or "",
               "description": c.get("description") or ""}
        if v["verdict"] == "done":
            if close:
                res = _close_row(v["id"], f"pre-send audit ({agent}): {row['why'][:200]}")
                if not res["ok"]:
                    failed.append(dict(row, error=res["error"]))
                    continue
            if log_missed:
                try:
                    lmc.log_missed_closure(
                        ledger_id=v["id"],
                        user_message=f"pre-send audit ({agent}): {row['why'][:200]}",
                        source="pre-send-audit", no_fetch=not fetch_for_log)
                except Exception as e:  # the log is best-effort
                    print(f"open_claims_audit: missed-closure log failed for "
                          f"{v['id']}: {e}", file=sys.stderr)
            done.append(row)
        elif v["verdict"] == "unsure":
            unsure.append(row)
    parked = list(audit.get("parked") or [])
    note = build_note(done, unsure, parked)
    summary = {
        "ts": _now_iso(), "agent": agent, "asked": audit.get("asked", 0),
        "verdicts": [{k: v[k] for k in ("id", "verdict", "why")} for v in verdicts],
        "closed": [d["id"] for d in done], "unsure": [u["id"] for u in unsure],
        "parked": [p["id"] for p in parked],
        "failed": failed, "rewrite": bool(note),
        "claims": [{"id": c["id"], "why_mentioned": c.get("why_mentioned"),
                    "evidence_n": len(c.get("evidence") or [])}
                   for c in audit.get("claims") or []],
    }
    _append(summary, log_path)
    summary["note"] = note
    return summary


def _append(record: dict, log_path: Path | None = None) -> bool:
    """Append the run to the audit log. run-agent.sh runs this as the
    owner and the log is in the shared ~/.ea/logs, so the append is made
    with the resident's access (resident_io); a failure costs one stderr
    line and returns False."""
    path = log_path or AUDIT_LOG
    return resident_io.append_text(path, json.dumps(record, ensure_ascii=False) + "\n")


# ── CLI ───────────────────────────────────────────────────────────────

def _cmd_prepare(args) -> int:
    post = Path(args.post_file).read_text() if args.post_file else ""
    briefing = None
    if args.briefing and Path(args.briefing).exists():
        try:
            briefing = json.loads(Path(args.briefing).read_text())
        except (json.JSONDecodeError, OSError) as e:
            print(f"open_claims_audit: briefing unreadable: {e}", file=sys.stderr)
    # EA_AUDIT_NO_FETCH=1 is the shell test's way of keeping run-agent.sh's
    # real audit block off Graph; production never sets it.
    no_fetch = args.no_fetch or os.environ.get("EA_AUDIT_NO_FETCH") == "1"
    record = prepare(agent=args.agent, post=post, briefing=briefing,
                     no_fetch=no_fetch)
    prompt = record.pop("prompt", "")
    if args.prompt_file:
        Path(args.prompt_file).write_text(prompt)
    if args.out:
        Path(args.out).write_text(json.dumps(record, indent=1, ensure_ascii=False))
    if not record["asked"] and not record["parked"]:
        # a run with nothing to ask is still a run: the log says so
        _append({"ts": record["ts"], "agent": args.agent, "asked": 0,
                 "claims": [{"id": c["id"], "why_mentioned": c["why_mentioned"],
                             "evidence_n": 0} for c in record["claims"]],
                 "fetch": record.get("fetch", "")})
    # "<asked> <parked>": run-agent.sh asks the model only for the first,
    # and runs apply (and the rewrite) when either is non-zero.
    print(f"{record['asked']} {len(record['parked'])}")
    return 0


def _cmd_apply(args) -> int:
    audit = json.loads(Path(args.audit).read_text())
    text = Path(args.verdicts_file).read_text() if Path(args.verdicts_file).exists() else ""
    verdicts = parse_verdicts(text)
    if not verdicts and audit.get("asked"):
        print("open_claims_audit: no verdicts parsed from the reply", file=sys.stderr)
    summary = apply(audit=audit, verdicts=verdicts, close=not args.no_close,
                    log_missed=not args.no_close)
    note = summary.pop("note", "")
    if args.note_file:
        Path(args.note_file).write_text(note)
    print(json.dumps({"asked": summary["asked"], "closed": summary["closed"],
                      "unsure": summary["unsure"], "parked": summary["parked"],
                      "failed": len(summary["failed"]),
                      "rewrite": summary["rewrite"]}))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare", help="find the open claims and their contrary evidence")
    p.add_argument("--agent", required=True)
    p.add_argument("--post-file", required=True)
    p.add_argument("--briefing", default="")
    p.add_argument("--out", default="")
    p.add_argument("--prompt-file", default="")
    p.add_argument("--no-fetch", action="store_true")
    p.set_defaults(fn=_cmd_prepare)
    p = sub.add_parser("apply", help="act on the verdicts and write the rewrite note")
    p.add_argument("--audit", required=True)
    p.add_argument("--verdicts-file", required=True)
    p.add_argument("--note-file", default="")
    p.add_argument("--no-close", action="store_true",
                   help="record verdicts without closing rows (dry run)")
    p.set_defaults(fn=_cmd_apply)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())

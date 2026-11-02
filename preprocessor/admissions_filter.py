#!/usr/bin/env python3
"""Admissions-inquiry filter: unsolicited PhD/MS applicant mail leaves the
inbox the moment it is seen, and is triaged once a week.

Prospective students write in volume ("Prospective PhD student, Fall
2027", "I read your radar paper and noticed a gap ... I am seeking PhD
positions"), often machine-written and followed up two or three times.
None of it is an obligation, and every triage detector that sees it
turns it into a needs-response row.

Division of labour:
  * `sweep` (this module, as the owner, zero Claude tokens): reads the
    recent inbox, judges each new outside message with `classify`, and
    moves the matches to the Outlook folder FOLDER_NAME. It runs from a
    five-minute cron around the clock and at the start of every
    preprocess mode, so no other detector sees an inquiry first.
  * `collect` (preprocess --mode admissions-digest): the last week of the
    folder, grouped by sender, bodies trimmed, for skills/admissions-digest.md,
    which ranks them for the weekly post. Nothing in the weekly run moves,
    archives or answers mail.

What is never moved, whatever the text says: the user's own mail; any
sender on an internal domain; a sender the user has written to (the style
profile store, plus the last SENT_LOOKBACK_DAYS of sent mail); a
conversation the user has sent in; a message with an internal colleague on it
(an introduction); a message that answers something the user wrote; a
configured mailing list; a VIP. A message the user drags back to the
inbox is recognised on the next sweep and its sender and conversation
are never moved again.

The feature is per install: off unless ea-config.json sets
`admissions_filter.enabled` to true. Off, the cron sweep, the preprocess
hook and the weekly collection do nothing.

State (owner-private, 0600): STATE_FILE holds the folder id, the
messages moved (for restore detection and the weekly reasons), the
senders and conversations the user restored, and the verdicts already
reached so a body is fetched once. MOVE_LOG is one JSON line per move.

Usage:
    python3 admissions_filter.py sweep [--days 3] [--dry-run]
    python3 admissions_filter.py backfill --days 60 [--dry-run]
    python3 admissions_filter.py collect [--days 7] [--output FILE]
    python3 admissions_filter.py status
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import owner_trust as _owner_trust  # noqa: E402
import atomic_io as _atomic  # noqa: E402

EA_DIR = Path(os.environ.get("EA_DIR", Path.home() / ".ea"))
FOLDER_NAME = "admissions-inquiries"
PROFILE_FILE = EA_DIR / "config" / "user-profile.json"
VIP_FILE = EA_DIR / "config" / "vip_senders.txt"
STYLE_DB = EA_DIR / "state" / "style-profiles.db"

SWEEP_DAYS = 3
SENT_LOOKBACK_DAYS = 30
VERDICT_TTL_DAYS = 21
BODY_CHARS = 8000          # judged text per message
DIGEST_BODY_CHARS = 2500   # handed to the weekly skill per message
MATCH_SCORE = 2

# Internal mail: the institution's domains and its related campuses
# (the `institution` block of user-profile.json; preprocessor/institution.py).


def enabled() -> bool:
    """The install's switch (ea-config.json `admissions_filter.enabled`).
    Every entry point that moves or reads the folder checks it."""
    try:
        import ea_config
        return ea_config.admissions_filter_enabled()
    except Exception:
        return False


def _owner_state_dir() -> Path:
    """The owner-private directory in an owner process (the cron, the
    preprocessor), the EA tree's state directory in any other (a test)."""
    if _owner_trust.is_owner_process():
        return _owner_trust.private_dir()
    return EA_DIR / "state"


STATE_FILE = _owner_state_dir() / "admissions-filter.json"
MOVE_LOG = _owner_state_dir() / "admissions-filter.jsonl"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ── state ─────────────────────────────────────────────────────────────

def _empty_state() -> dict:
    return {"folder_id": "", "moved": {}, "allowed_senders": [],
            "allowed_conversations": [], "verdicts": {}, "keep_folder_ids": {}}


def load_state(path: Path | None = None) -> dict:
    path = path or STATE_FILE
    try:
        if _owner_trust.is_owner_process() and path.parent == _owner_trust.private_dir():
            raw = _owner_trust.read_owner_file(path).decode("utf-8")
        else:
            raw = path.read_text()
        data = json.loads(raw)
    except FileNotFoundError:
        return _empty_state()
    except (OSError, ValueError):
        return _empty_state()
    base = _empty_state()
    if isinstance(data, dict):
        base.update({k: v for k, v in data.items() if k in base})
    return base


def save_state(state: dict, path: Path | None = None) -> None:
    path = path or STATE_FILE
    text = json.dumps(state, indent=1, sort_keys=True)
    if _owner_trust.is_owner_process() and path.parent == _owner_trust.private_dir():
        _owner_trust.write_owner_file(path, text, mode=0o600)
    else:
        _atomic.write_text(path, text)


def _append_log(rec: dict, path: Path | None = None) -> None:
    path = path or MOVE_LOG
    line = json.dumps(rec, ensure_ascii=False) + "\n"
    try:
        if _owner_trust.is_owner_process() and path.parent == _owner_trust.private_dir():
            try:
                old = _owner_trust.read_owner_file(path)
            except FileNotFoundError:
                old = b""
            _owner_trust.write_owner_file(path, old + line.encode("utf-8"), mode=0o600)
        else:
            with open(path, "a") as f:
                f.write(line)
    except OSError as e:
        print(f"admissions_filter: move log append failed: {e}", file=sys.stderr)


def message_key(msg: dict) -> str:
    """Stable across folder moves (Outlook mints a new id on every move):
    the conversation and the received instant."""
    return f"{msg.get('conversationId') or ''}|{msg.get('receivedDateTime') or ''}"


# ── text ──────────────────────────────────────────────────────────────

_QUOTE_START = re.compile(
    r"^(?:\s*>|\s*From:\s|\s*On .{4,120} wrote:\s*$|\s*-{2,}\s*Original Message|\s*_{8,}\s*$|\s*Sent from my )",
    re.I | re.M)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")


def html_to_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style).*?</\1>", " ", html or "")
    html = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</tr>", "\n", html)
    text = _TAG_RE.sub(" ", html)
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                 ("&#39;", "'"), ("&quot;", '"'), ("&rsquo;", "'"), ("&ldquo;", '"'),
                 ("&rdquo;", '"'), ("&ndash;", "-"), ("&mdash;", "-")):
        text = text.replace(a, b)
    return text


def body_text(full: dict) -> str:
    """The new text of a message: HTML stripped, quoted history cut."""
    body = (full or {}).get("body") or {}
    content = body.get("content") or ""
    if (body.get("contentType") or "").lower() == "html":
        content = html_to_text(content)
    m = _QUOTE_START.search(content)
    if m and m.start() > 40:
        content = content[:m.start()]
    lines = [_WS_RE.sub(" ", ln).strip() for ln in content.splitlines()]
    return "\n".join(ln for ln in lines if ln)[:BODY_CHARS]


def _norm(text: str) -> str:
    t = (text or "").lower().replace("’", "'")
    t = re.sub(r"\bph\.?\s?d\.?(?=\W|$)", "phd", t)
    t = re.sub(r"\bm\.\s?s\.(?=\W|$)", "ms", t)
    t = re.sub(r"\bm\.?\s?sc\.?(?=\W|$)", "msc", t)
    return t


_DEG = r"(?:phd|doctoral|doctorate|ms|msc|master(?:'?s)?|graduate)(?:/[a-z]{2,4})?"
# Each pattern is a kind of applicant intent; a message scores once per kind.
_SUBJECT_PATTERNS = [
    ("prospective-student", re.compile(
        rf"\b(?:prospective|aspiring|future|incoming|potential)\b.{{0,30}}\b(?:{_DEG}|student|applicant)\b")),
    ("degree-ask", re.compile(
        rf"\b{_DEG}\b.{{0,35}}\b(?:applicant|application|inquiry|enquiry|candidate|candidacy|"
        rf"position|positions|opportunit\w*|vacanc\w*|opening|admission\w*|supervis\w*|student)\b")),
    ("intake-term", re.compile(
        r"\b(?:fall|spring|autumn|winter)\s*'?20\d\d\b.{0,40}\b(?:phd|doctoral|ms|graduate|intake|admission\w*|cycle|program\w*|applicant|student)\b"
        r"|\b(?:phd|doctoral|ms|graduate)\b.{0,40}\b(?:fall|spring|autumn)\s*'?20\d\d\b")),
]
_BODY_PATTERNS = [
    ("prospective-student", re.compile(
        rf"\b(?:prospective|aspiring|future|incoming|potential)\s+(?:{_DEG}\s+)?(?:student|applicant|candidate|researcher)\b"), 2),
    ("degree-position", re.compile(
        rf"\b{_DEG}\s+(?:position|positions|opening|openings|opportunit\w*|vacanc\w*|admission\w*|"
        rf"supervision|studentship|program\w*\s+(?:in|at)\s+(?:your|the)\s+(?:lab|group))\b"), 2),
    ("applying", re.compile(
        rf"\b(?:apply|applying|application|applicant|admission)\b[^.\n]{{0,60}}\b{_DEG}\b"
        rf"|\b{_DEG}\b[^.\n]{{0,40}}\b(?:apply|applying|application|applicant|admission)\b"), 1),
    ("intake-term", re.compile(
        r"\b(?:fall|spring|autumn)\s*'?20\d\d\b[^.\n]{0,40}\b(?:phd|doctoral|ms|graduate|intake|admission\w*|cycle|program\w*)\b"
        r"|\b(?:phd|doctoral|ms|graduate)\b[^.\n]{0,40}\b(?:fall|spring|autumn)\s*'?20\d\d\b"), 1),
    ("join-group", re.compile(
        r"\bjoin(?:ing)?\s+(?:your|the)\s+(?:lab|group|team|research group|research team)\b"
        r"|\b(?:under|with)\s+your\s+(?:supervision|guidance|mentorship)\b"
        r"|\b(?:accepting|taking on|recruiting|admitting)\s+(?:any\s+)?(?:new\s+)?(?:phd\s+|graduate\s+|ms\s+)?students\b"), 1),
    ("seeking-degree", re.compile(
        rf"\b(?:seeking|looking for|searching for|in search of|hope to pursue|wish to pursue|plan to pursue|"
        rf"intend to pursue)\b[^.\n]{{0,40}}\b{_DEG}\b"), 1),
    ("cv-attached", re.compile(
        r"\b(?:attached|enclosed|attaching)\b[^.\n]{0,40}\b(?:cv|resume|curriculum vitae|transcripts?)\b"
        r"|\b(?:cv|resume|curriculum vitae)\b[^.\n]{0,25}\b(?:attached|enclosed)\b"), 1),
]
# A third party introduces a student: a referral the user should see.
_REFERRAL = re.compile(
    r"\b(?:introduce|introducing|recommend|recommending|refer|referring)\b[^.\n]{0,60}\b(?:student|intern|mentee|him|her)\b"
    r"|\b(?:my|our) (?:former |current |undergraduate |graduate )?(?:student|intern|mentee|advisee|ra)\b[^.\n]{0,80}\b(?:phd|graduate|apply|applying|interested)\b"
    r"|\b(?:he|she|they|[a-z]+) (?:is|are) (?:in (?:his|her|their) (?:final|last|senior)|interested in pursuing|applying to|finishing (?:his|her|their))\b")

# The sender answers something the user wrote: a conversation, not a cold ask.
_ANSWERS_USER = re.compile(
    r"\bthank(?:s| you)\b[^.\n]{0,25}\b(?:your|the)\s+(?:kind\s+|quick\s+|prompt\s+|timely\s+)?(?:reply|response|email back|note back)\b"
    r"|\bas you (?:suggested|recommended|mentioned)\b|\bper our (?:conversation|call|chat|meeting)\b"
    r"|\b(?:great|nice|good) (?:to meet|meeting|talking|speaking) (?:with )?you\b")


def score_text(subject: str, body: str) -> tuple[int, list[str]]:
    """Applicant-intent score of one message and the kinds that fired.
    A subject kind counts 3 (a subject that announces an applicant is the
    message), a body kind its weight, each kind once."""
    subj, text = _norm(subject), _norm(body)
    score, kinds = 0, []
    for kind, rx in _SUBJECT_PATTERNS:
        if rx.search(subj):
            score += 3
            kinds.append(f"subject:{kind}")
    seen = {k.split(":", 1)[1] for k in kinds}
    for kind, rx, weight in _BODY_PATTERNS:
        if kind in seen:
            continue
        if rx.search(text):
            score += weight
            kinds.append(kind)
            seen.add(kind)
    return score, kinds


# ── who is this? ──────────────────────────────────────────────────────

def _addr(rec) -> str:
    return (((rec or {}).get("emailAddress") or {}).get("address") or "").strip().lower()


def _name(rec) -> str:
    return (((rec or {}).get("emailAddress") or {}).get("name") or "").strip()


def _name_key(name: str) -> str:
    """'Li, Jayden' and 'Jayden Li' are one person: sorted lowercase words
    of two or more letters, at least two of them."""
    words = sorted(w for w in re.findall(r"[a-z]{2,}", (name or "").lower()))
    return " ".join(words) if len(words) >= 2 else ""


def is_internal(addr: str) -> bool:
    """An address on one of the institution's domains or related campuses."""
    try:
        import institution
    except ImportError:
        from preprocessor import institution
    return institution.is_internal_or_related(addr)


def _user_addresses() -> set[str]:
    out = set()
    try:
        prof = json.loads(PROFILE_FILE.read_text())
        for k in ("email", "alt_emails", "aliases"):
            v = prof.get(k)
            for a in ([v] if isinstance(v, str) else (v or [])):
                if a:
                    out.add(str(a).strip().lower())
    except (OSError, ValueError):
        pass
    return out


def _known_contacts(db: Path | None = None) -> tuple[set[str], set[str]]:
    """(addresses, name keys) of everyone the user has written to, from the
    style profile store (sent mail since its first sync)."""
    db = db or STYLE_DB
    addrs, names = set(), set()
    try:
        import ledger  # guarded_connect: the owner-trust checks on a shared DB
        con = ledger.guarded_connect(db, timeout=10, read_only=True)
        for contact, display in con.execute("SELECT contact, display_name FROM profiles"):
            if contact:
                addrs.add(contact.strip().lower())
            k = _name_key(display or "")
            if k:
                names.add(k)
        con.close()
    except Exception as e:
        print(f"admissions_filter: style profiles unreadable ({e}); "
              f"relying on recent sent mail only", file=sys.stderr)
    return addrs, names


def _vips() -> set[str]:
    try:
        return {ln.strip().lower() for ln in VIP_FILE.read_text().splitlines()
                if ln.strip() and not ln.startswith("#")}
    except OSError:
        return set()


def build_context(sent: list | None = None, lists: list | None = None,
                  style_db: Path | None = None, state: dict | None = None) -> dict:
    """Everything `classify` needs besides the message. `sent` is the
    user's recent sent mail (Graph shape)."""
    known_addrs, known_names = _known_contacts(style_db)
    sent_convs, sent_to = set(), set()
    for m in sent or []:
        if m.get("conversationId"):
            sent_convs.add(m["conversationId"])
        for f in ("toRecipients", "ccRecipients"):
            for r in m.get(f) or []:
                a = _addr(r)
                if a:
                    sent_to.add(a)
                k = _name_key(_name(r))
                if k:
                    known_names.add(k)
    state = state or _empty_state()
    return {
        "user": _user_addresses(),
        "known_addrs": known_addrs | sent_to,
        "known_names": known_names,
        "sent_convs": sent_convs,
        "vips": _vips(),
        "lists": lists,
        "allowed_senders": {a.lower() for a in state.get("allowed_senders") or []},
        "allowed_convs": set(state.get("allowed_conversations") or []),
    }


def exclusion(msg: dict, ctx: dict) -> str:
    """Why this message must stay in the inbox whatever it says, or ''."""
    frm = _addr(msg.get("from"))
    if not frm:
        return "no sender"
    if frm in ctx["user"]:
        return "from the user"
    if is_internal(frm):
        return "internal sender"
    if frm in ctx["allowed_senders"]:
        return "sender restored by the user"
    conv = msg.get("conversationId") or ""
    if conv and conv in ctx["allowed_convs"]:
        return "conversation restored by the user"
    if conv and conv in ctx["sent_convs"]:
        return "the user sent in this conversation"
    if frm in ctx["known_addrs"]:
        return "the user has written to this sender"
    nk = _name_key(_name(msg.get("from")))
    if nk and nk in ctx["known_names"]:
        return "the user has written to someone of this name"
    if frm in ctx["vips"]:
        return "VIP sender"
    for f in ("toRecipients", "ccRecipients"):
        for r in msg.get(f) or []:
            a = _addr(r)
            if a and a not in ctx["user"] and is_internal(a):
                return "an internal colleague is on the message"
    if ctx.get("lists") is not None:
        try:
            import list_digest
            if list_digest.match_list(msg, ctx["lists"]):
                return "mailing list"
        except Exception:
            pass
    return ""


def classify(msg: dict, body: str, ctx: dict) -> dict:
    """{'match': bool, 'score', 'kinds', 'excluded': reason or ''}."""
    why = exclusion(msg, ctx)
    if why:
        return {"match": False, "score": 0, "kinds": [], "excluded": why}
    text = _norm(body)
    if _ANSWERS_USER.search(text):
        return {"match": False, "score": 0, "kinds": [],
                "excluded": "answers something the user wrote"}
    if _REFERRAL.search(text):
        return {"match": False, "score": 0, "kinds": [],
                "excluded": "a third party introduces a student"}
    score, kinds = score_text(msg.get("subject") or "", body)
    return {"match": score >= MATCH_SCORE, "score": score, "kinds": kinds, "excluded": ""}


def _prefilter(msg: dict, ctx: dict) -> bool:
    """Worth a body fetch? Outside, not excluded by header facts."""
    return not exclusion(msg, ctx)


# ── Outlook ───────────────────────────────────────────────────────────

def ensure_folder(graph, state: dict) -> str:
    """The id of FOLDER_NAME at the top of the mailbox, created when it is
    missing. The id is cached in state and re-checked when a move fails."""
    fid = state.get("folder_id") or ""
    if fid:
        return fid
    data = graph.graph_get("/me/mailFolders",
                           {"$filter": f"displayName eq '{FOLDER_NAME}'", "$top": 5})
    hits = data.get("value") or []
    if hits:
        fid = hits[0]["id"]
    else:
        fid = graph.graph_post("/me/mailFolders", {"displayName": FOLDER_NAME})["id"]
    state["folder_id"] = fid
    return fid


# Rows the triage detectors open on their own for a message; a row the
# user or the EA added by hand never carries these prefixes.
AUTO_ROW_PREFIXES = ("response-", "thread-")
OPEN_STATUSES = "open,waiting,follow_up,snoozed"


def close_auto_rows(conversation_id: str) -> list[str]:
    """Close the detector-made ledger rows of a conversation the filter
    moved (an intake pass may have seen the mail first). Through the
    ledger CLI, with --skip-archive: the mail has already left the inbox.
    Returns the closed ids; a failure costs a stderr line."""
    import subprocess
    if not conversation_id:
        return []
    here = Path(__file__).resolve().parent
    try:
        r = subprocess.run([sys.executable, str(here / "ledger.py"), "search",
                            "--conversation-id", conversation_id, "--status", OPEN_STATUSES],
                           capture_output=True, text=True, timeout=60)
        rows = json.loads(r.stdout or "{}").get("entries") or []
    except Exception as e:
        print(f"admissions_filter: ledger search failed: {e}", file=sys.stderr)
        return []
    closed = []
    for row in rows:
        rid = row.get("id") or ""
        if not rid.startswith(AUTO_ROW_PREFIXES):
            continue
        r = subprocess.run([sys.executable, str(here / "ledger.py"), "close", "--id", rid,
                            "--skip-archive", "--user-rule", "admissions filter", "--note",
                            f"admissions filter: moved to the {FOLDER_NAME} folder; "
                            f"a prospective-student inquiry is not an obligation"],
                           capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            closed.append(rid)
        else:
            print(f"admissions_filter: close {rid} failed: {(r.stderr or '')[-200:]}",
                  file=sys.stderr)
    return closed


def _default_graph():
    import graph_client as graph
    return graph


# ── sweep ─────────────────────────────────────────────────────────────

def sweep(*, days: int = SWEEP_DAYS, dry_run: bool = False, graph=None,
          inbox: list | None = None, sent: list | None = None,
          lists: list | None = None, style_db: Path | None = None,
          state_path: Path | None = None, log_path: Path | None = None,
          now: datetime | None = None, closer=None) -> dict:
    """Judge the recent inbox and move the inquiries. Injectable inputs
    keep the tests off Graph. Returns a summary; never raises for one bad
    message."""
    now = now or _now()
    graph = graph or _default_graph()
    closer = closer or close_auto_rows
    state = load_state(state_path)
    if inbox is None:
        inbox = graph.get_messages("inbox", days_back=days)
    if lists is None:
        try:
            import list_digest
            lists = list_digest.configured_lists()
        except Exception:
            lists = []
    # Sent mail is the costly read, so it is fetched only when some inbox
    # message is new and passes the header checks that need no sent mail.
    pre = build_context([], lists, style_db, state)
    pending = [m for m in inbox
               if state["moved"].get(message_key(m))
               or ((dry_run or message_key(m) not in state["verdicts"])
                   and _prefilter(m, pre))]
    if not pending:
        return {"moved": [], "restored": [], "checked": 0,
                **({"would_move": []} if dry_run else {})}
    if sent is None:
        try:
            sent = graph.get_messages("sentitems", days_back=SENT_LOOKBACK_DAYS)
        except Exception as e:
            print(f"admissions_filter: sent mail unavailable ({e}); "
                  f"not moving anything this run", file=sys.stderr)
            return {"moved": [], "restored": [], "checked": 0, "error": "sent mail unavailable"}
    ctx = build_context(sent, lists, style_db, state)

    # A message we moved that is back in the inbox was put there by the
    # user: never move its sender or conversation again.
    restored = []
    for m in inbox:
        rec = state["moved"].get(message_key(m))
        if rec and not dry_run:
            frm = _addr(m.get("from"))
            if frm and frm not in state["allowed_senders"]:
                state["allowed_senders"].append(frm)
            conv = m.get("conversationId") or ""
            if conv and conv not in state["allowed_conversations"]:
                state["allowed_conversations"].append(conv)
            state["moved"].pop(message_key(m), None)
            restored.append({"from": frm, "subject": m.get("subject") or ""})
            _append_log({"ts": _iso(now), "event": "restored", "from": frm,
                         "subject": (m.get("subject") or "")[:120]}, log_path)
    if restored:
        ctx = build_context(sent, lists, style_db, state)

    cutoff = _iso(now - timedelta(days=VERDICT_TTL_DAYS))
    state["verdicts"] = {k: v for k, v in state["verdicts"].items()
                         if (v.get("ts") or "") >= cutoff}
    moved, checked, would = [], 0, []
    folder_id = ""
    for m in inbox:
        key = message_key(m)
        if key in state["verdicts"] and not dry_run:
            continue
        if not _prefilter(m, ctx):
            continue
        checked += 1
        try:
            full = graph.get_message_full(m["id"])
        except Exception as e:
            print(f"admissions_filter: body fetch failed for {m.get('subject')!r}: {e}",
                  file=sys.stderr)
            continue
        verdict = classify(m, body_text(full), ctx)
        rec = {"from": _addr(m.get("from")), "name": _name(m.get("from")),
               "subject": (m.get("subject") or "")[:200],
               "received": m.get("receivedDateTime") or "",
               "score": verdict["score"], "kinds": verdict["kinds"]}
        if not verdict["match"]:
            if not dry_run:
                state["verdicts"][key] = {"ts": _iso(now), "match": False,
                                          "score": verdict["score"],
                                          "excluded": verdict["excluded"]}
            continue
        if dry_run:
            would.append(rec)
            continue
        try:
            folder_id = folder_id or ensure_folder(graph, state)
            try:
                res = graph.graph_post(f"/me/messages/{m['id']}/move",
                                       {"destinationId": folder_id})
            except Exception:
                # a stale cached folder id (deleted or renamed by hand)
                state["folder_id"] = ""
                folder_id = ensure_folder(graph, state)
                res = graph.graph_post(f"/me/messages/{m['id']}/move",
                                       {"destinationId": folder_id})
        except Exception as e:
            print(f"admissions_filter: move failed for {m.get('subject')!r}: {e}",
                  file=sys.stderr)
            continue
        rec["moved_at"] = _iso(now)
        rec["new_id"] = (res or {}).get("id", "")
        try:
            rec["ledger_closed"] = closer(m.get("conversationId") or "")
        except Exception as e:
            rec["ledger_closed"] = []
            print(f"admissions_filter: ledger cleanup failed: {e}", file=sys.stderr)
        state["moved"][key] = rec
        state["verdicts"][key] = {"ts": _iso(now), "match": True, "score": verdict["score"]}
        moved.append(rec)
        _append_log({"ts": _iso(now), "event": "moved", **{k: rec[k] for k in
                     ("from", "name", "subject", "received", "score", "kinds")}}, log_path)
    # moved records older than the restore horizon are history in the log
    old = _iso(now - timedelta(days=90))
    state["moved"] = {k: v for k, v in state["moved"].items()
                      if (v.get("moved_at") or "") >= old}
    if not dry_run:
        save_state(state, state_path)
    out = {"moved": moved, "restored": restored, "checked": checked}
    if dry_run:
        out["would_move"] = would
    return out


def moved_ids(summary: dict) -> set[str]:
    """Never needed by callers that refetch the inbox after a sweep; kept
    for a caller holding an inbox list from before it."""
    return {m.get("new_id") for m in summary.get("moved") or [] if m.get("new_id")}


def preprocess_hook() -> None:
    """Run from every preprocess mode before it reads the inbox. A failure
    costs one stderr line, never the run."""
    if not enabled():
        return
    try:
        res = sweep()
        if res.get("moved") or res.get("restored"):
            print(f"  Admissions filter: moved {len(res['moved'])}, "
                  f"restored {len(res['restored'])}", file=sys.stderr)
    except Exception as e:
        print(f"  Admissions filter skipped: {e}", file=sys.stderr)


# ── filing a replied conversation ─────────────────────────────────────

# Folders a filed conversation never takes mail out of: the user's own
# copies, unsent drafts, and what the user threw away.
KEEP_FOLDERS = ("sentitems", "drafts", "deleteditems")
_CONV_FIELDS = ("id,subject,from,toRecipients,ccRecipients,receivedDateTime,"
                "sentDateTime,conversationId,parentFolderId,isDraft,bodyPreview")


def _keep_folder_ids(graph, state: dict) -> set[str]:
    ids = state.setdefault("keep_folder_ids", {})
    for wk in KEEP_FOLDERS:
        if not ids.get(wk):
            try:
                ids[wk] = graph.graph_get(f"/me/mailFolders/{wk}", {"$select": "id"})["id"]
            except Exception:
                ids[wk] = ""
    return {v for v in ids.values() if v}


def is_admissions_conversation(msgs: list, graph, user: set[str]) -> bool:
    """Did an outside applicant open this conversation? The first message
    not from the user decides: an outside sender (not internal) whose subject and
    new text score as applicant intent."""
    first = next((m for m in sorted(msgs, key=lambda x: x.get("receivedDateTime") or "")
                  if _addr(m.get("from")) not in user), None)
    if not first:
        return False
    frm = _addr(first.get("from"))
    if not frm or is_internal(frm):
        return False
    try:
        body = body_text(graph.get_message_full(first["id"]))
    except Exception:
        body = first.get("bodyPreview") or ""
    if _REFERRAL.search(_norm(body)):
        return False
    score, _ = score_text(first.get("subject") or "", body)
    return score >= MATCH_SCORE


def file_conversation(conversation_id: str, *, force: bool = False, graph=None,
                      state_path: Path | None = None, log_path: Path | None = None,
                      now: datetime | None = None) -> dict:
    """Move the inbound mail of an applicant conversation the user answered
    into FOLDER_NAME (inbox or archive alike), leaving the user's sent
    copies, drafts and deleted items where they are. `force` skips the
    applicant check (the user named these people). Each moved message is
    tracked like a sweep move, so dragging one back allowlists it."""
    now = now or _now()
    graph = graph or _default_graph()
    if not conversation_id:
        return {"admissions": False, "moved": 0}
    state = load_state(state_path)
    msgs = graph.graph_get_all("/me/messages", {
        "$filter": f"conversationId eq '{conversation_id}'",
        "$select": _CONV_FIELDS, "$top": 50})
    msgs = [m for m in msgs if not m.get("isDraft")]
    user = _user_addresses()
    if not force and not is_admissions_conversation(msgs, graph, user):
        return {"admissions": False, "moved": 0}
    folder_id = ensure_folder(graph, state)
    keep = _keep_folder_ids(graph, state) | {folder_id}
    moved = 0
    for m in msgs:
        if _addr(m.get("from")) in user or m.get("parentFolderId") in keep:
            continue
        try:
            res = graph.graph_post(f"/me/messages/{m['id']}/move", {"destinationId": folder_id})
        except Exception as e:
            print(f"admissions_filter: filing {m.get('subject')!r} failed: {e}", file=sys.stderr)
            continue
        moved += 1
        rec = {"from": _addr(m.get("from")), "name": _name(m.get("from")),
               "subject": (m.get("subject") or "")[:200],
               "received": m.get("receivedDateTime") or "", "score": 0,
               "kinds": ["filed-after-reply"], "moved_at": _iso(now),
               "new_id": (res or {}).get("id", "")}
        state["moved"][message_key(m)] = rec
        _append_log({"ts": _iso(now), "event": "filed", **{k: rec[k] for k in
                     ("from", "name", "subject", "received")}}, log_path)
    save_state(state, state_path)
    return {"admissions": True, "moved": moved}


def file_senders(addresses: list[str], *, graph=None, state_path: Path | None = None,
                 log_path: Path | None = None) -> dict:
    """File every conversation these senders started or wrote in (the
    user named them, so no applicant check)."""
    graph = graph or _default_graph()
    convs: dict[str, str] = {}
    for a in addresses:
        a = a.strip().lower()
        for m in graph.graph_get_all("/me/messages", {
                "$filter": f"from/emailAddress/address eq '{a}'",
                "$select": "id,conversationId", "$top": 50}):
            if m.get("conversationId"):
                convs[m["conversationId"]] = a
    out = {"conversations": len(convs), "moved": 0}
    for cid in convs:
        out["moved"] += file_conversation(cid, force=True, graph=graph,
                                          state_path=state_path, log_path=log_path)["moved"]
    return out


# ── weekly collection ─────────────────────────────────────────────────

def _profile() -> dict:
    try:
        prof = json.loads(PROFILE_FILE.read_text())
    except (OSError, ValueError):
        return {}
    return {k: prof.get(k, "") for k in ("name", "role_description", "department",
                                         "university", "bio_blurb")}


def collect(*, days: int = 7, graph=None, state_path: Path | None = None,
            now: datetime | None = None) -> dict:
    """The input of skills/admissions-digest.md: the folder's last `days`
    of mail grouped by sender, each message with its trimmed new text and
    the reasons the sweep moved it."""
    now = now or _now()
    graph = graph or _default_graph()
    state = load_state(state_path)
    out = {"mode": "admissions-digest", "generated_at": _iso(now), "window_days": days,
           "folder": FOLDER_NAME, "profile": _profile(), "digests": [], "senders": 0,
           "messages": 0, "errors": []}
    try:
        fid = ensure_folder(graph, state)
        save_state(state, state_path)
        msgs = graph.get_messages(fid, days_back=days)
    except Exception as e:
        out["errors"].append(f"folder read failed: {e}")
        return out
    by_sender: dict[str, dict] = {}
    for m in sorted(msgs, key=lambda x: x.get("receivedDateTime") or ""):
        frm = _addr(m.get("from"))
        try:
            text = body_text(graph.get_message_full(m["id"]))[:DIGEST_BODY_CHARS]
        except Exception as e:
            text = (m.get("bodyPreview") or "")[:DIGEST_BODY_CHARS]
            out["errors"].append(f"{m.get('subject')!r}: body fetch failed: {e}")
        rec = state["moved"].get(message_key(m)) or {}
        g = by_sender.setdefault(frm, {"from": frm, "name": _name(m.get("from")),
                                       "domain": frm.rsplit("@", 1)[-1], "messages": []})
        g["messages"].append({
            "subject": m.get("subject") or "", "received": m.get("receivedDateTime") or "",
            "has_attachments": bool(m.get("hasAttachments")),
            "kinds": rec.get("kinds", []), "text": text,
            "message_id": m.get("id") or "", "conversation_id": m.get("conversationId") or "",
        })
    for g in by_sender.values():
        g["count"] = len(g["messages"])
        g["follow_ups"] = sum(1 for x in g["messages"]
                              if re.match(r"^\s*(re|fw|fwd)\s*:", x["subject"], re.I))
    out["digests"] = sorted(by_sender.values(), key=lambda g: g["messages"][0]["received"])
    out["senders"] = len(by_sender)
    out["messages"] = len(msgs)
    return out


# ── CLI ───────────────────────────────────────────────────────────────

def _print(obj) -> None:
    print(json.dumps(obj, indent=1, ensure_ascii=False))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("sweep", help="judge the recent inbox and move inquiries")
    p.add_argument("--days", type=int, default=SWEEP_DAYS)
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("backfill", help="sweep a longer window of the inbox once")
    p.add_argument("--days", type=int, default=60)
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("collect", help="the weekly digest input")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--output")
    sub.add_parser("status", help="folder id, counts, restored senders")
    p = sub.add_parser("file-conversation",
                       help="file an answered applicant conversation's inbound mail")
    p.add_argument("--conversation-id", required=True)
    p.add_argument("--force", action="store_true", help="skip the applicant check")
    p = sub.add_parser("file-senders", help="file every conversation of these senders")
    p.add_argument("--sender", action="append", required=True)
    args = ap.parse_args(argv)

    if args.cmd in ("file-conversation", "file-senders"):
        if not enabled():
            _print({"admissions": False, "moved": 0, "disabled": True})
            return 0
        if args.cmd == "file-conversation":
            _print(file_conversation(args.conversation_id, force=args.force))
        else:
            _print(file_senders(args.sender))
        return 0
    if args.cmd in ("sweep", "backfill", "collect") and not enabled():
        if args.cmd == "collect":
            res = {"mode": "admissions-digest", "digests": [], "disabled": True}
            if args.output:
                Path(args.output).write_text(json.dumps(res))
            else:
                _print(res)
        elif args.cmd == "backfill" or args.dry_run:
            print("admissions filter is off for this install "
                  "(ea-config.json admissions_filter.enabled)")
        return 0
    if args.cmd in ("sweep", "backfill"):
        res = sweep(days=args.days, dry_run=args.dry_run)
        stamp = _iso(_now())
        if args.dry_run:
            _print(res)
        elif res.get("moved") or res.get("restored") or args.cmd == "backfill":
            print(f"[{stamp}] moved {len(res['moved'])}, restored "
                  f"{len(res['restored'])}, checked {res['checked']}")
            for m in res["moved"]:
                print(f"  moved: {m['from']} | {m['subject'][:80]} | {','.join(m['kinds'])}"
                      + (f" | closed {','.join(m['ledger_closed'])}" if m.get("ledger_closed") else ""))
            for r in res["restored"]:
                print(f"  restored by you: {r['from']} | {r['subject'][:80]}")
        return 0
    if args.cmd == "collect":
        res = collect(days=args.days)
        if args.output:
            Path(args.output).write_text(json.dumps(res, indent=1, ensure_ascii=False))
        else:
            _print(res)
        return 0
    if args.cmd == "status":
        st = load_state()
        _print({"folder": FOLDER_NAME, "folder_id": st["folder_id"],
                "moved_tracked": len(st["moved"]), "verdicts_cached": len(st["verdicts"]),
                "allowed_senders": st["allowed_senders"],
                "allowed_conversations": len(st["allowed_conversations"])})
        return 0
    return 1


if __name__ == "__main__":
    # A tick: its Graph calls run in the background lane unless its caller
    # set one (retry_policy.graph_lane).
    import retry_policy
    retry_policy.set_graph_lane(retry_policy.BACKGROUND, only_if_unset=True)
    sys.exit(main())

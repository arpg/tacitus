#!/usr/bin/env python3
"""Email writer: the remote model writes an email body for a Claude lane,
in place of the `email-author` subagent (docs/remote-llm.md).

    email_writer.py write --to <addr> --brief-file <path> --out <path>
                          [--conversation-id <id>] [--draft-id <id>]

The brief is what the caller would give the subagent: the recipient, the
request, what the reply must say. The remote model gets the subagent's
context (draft_ab.remote_messages: its instructions, the style profile of
`--to`, the voice guide, the brief and the text of any resident file the
brief names), the thread's last messages read from the mailbox (with
--conversation-id, quoted history cut) and the current draft (with
--draft-id, for a revision). Its answer goes through the A/B's
post-processing: notes to the caller split off, dashes fixed, and an
empty answer or a `NEED:` refused.

Run as ea-bot, the CLI forwards through mail_mediation to the owner-side
mediator, which re-runs it as the owner, so the endpoint key and the mailbox
token stay owner-side. --brief-file is an input the mediator stages by
copy and --out an output it delivers back into ea-bot's allowed roots;
both are read and written here only as files this user owns.

Switch: `"drafting": "remote"` in remote-llm.json. Any other value turns
the writer off, and so does a process that is not the owner's (remote_llm
reads its config only there): the resident on an install without mail
mediation runs this CLI itself and always gets the writer-off fallback,
before the brief is read.

stdout is one JSON object:
    {"status": "ok", "writer": "remote", "out": <path>, "words": n,
     "notes": "<notes to the caller, or ''>", "body": "<the body>"}   exit 0
    {"status": "fallback", "reason": "...", "need": "<question>"}     exit 4
The body is in --out, ready for --body-file; `body` is there for the
caller to check facts against. A fallback (writer off, `NEED:`, empty,
late, endpoint error) means: write the body with the email-author subagent
as before; `need` is present only for a `NEED:`. A bad argument is
{"status": "error", ...} on stderr with exit 2. Exit 3 is not this
script's: mail_mediation's shim exits 3 when the owner-side mediator does
not answer in time, and the caller treats it as a fallback too.

Store: <private_dir>/remote-llm/draft-writer.jsonl, one record per call
that reaches the writer. Each brief sent to the endpoint is also recorded
in the owner-private outbound log (outbound_log.py: size, hash, first 200
characters, size of the whole request), which the daily outbound review
counts. `recent_bodies(since)` reads the bodies back, so
the bot can match a draft to the writer output it holds.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
if __name__ == "__main__":
    from mail_mediation import forward_or_none
    forward_or_none("email_writer", sys.argv[1:])

import draft_ab  # noqa: E402
import outbound_log  # noqa: E402
import owner_trust  # noqa: E402
import remote_llm  # noqa: E402

STORE = "draft-writer.jsonl"
PURPOSE = "draft-writer"
BRIEF_CHARS = 40_000
THREAD_CHARS = 12_000
MESSAGE_CHARS = 4_000
DRAFT_CHARS = 8_000
REMOTE_TIMEOUT_S = 90
# The whole run, the style-profile read and the mailbox reads included,
# ends inside the mediator's 120-second limit on a child, so a late
# answer still reaches the caller as a fallback.
BUDGET_S = 105
EXIT_FALLBACK = 4
_ADDR = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")


class ArgError(Exception):
    """A request the writer cannot take as given."""


class _Late(BaseException):
    """The run reached its time budget. A BaseException, so no handler on
    the way up mistakes it for an ordinary failure and carries on."""


@contextmanager
def _budget(seconds: float):
    """Raise _Late in the main thread once `seconds` have passed."""
    if (seconds <= 0 or not hasattr(signal, "SIGALRM")
            or threading.current_thread() is not threading.main_thread()):
        yield
        return

    def _late(signum, frame):
        raise _Late()

    old = signal.signal(signal.SIGALRM, _late)
    signal.alarm(max(1, int(seconds)))
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _when(value) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(value or "").strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


# ── the inputs ────────────────────────────────────────────────────────

def recipient(addr: str) -> str:
    """`--to` as the address the style profile is read for."""
    a = (addr or "").strip().lower()
    if not _ADDR.fullmatch(a):
        raise ArgError(f"--to {addr!r} is not an email address")
    return a


def read_brief(path: str) -> str:
    """The brief, cut to BRIEF_CHARS. The file must be one this user owns
    in a directory this user owns, as the mediator's staged copy is; a
    path in the resident's space is refused, never followed."""
    try:
        raw = owner_trust.read_owner_file(path, allow_private_group=True)
    except FileNotFoundError:
        raise ArgError(f"--brief-file {path!r} does not exist") from None
    except (owner_trust.OwnerTrustError, OSError) as e:
        raise ArgError(f"--brief-file is not readable as this user's own file: {e}") from None
    brief = raw.decode("utf-8", errors="replace")[:BRIEF_CHARS]
    if not brief.strip():
        raise ArgError("--brief-file is empty")
    return brief


def _who(ea: dict | None) -> str:
    name = ((ea or {}).get("name") or "").strip()
    addr = ((ea or {}).get("address") or "").strip()
    if name and addr and name.lower() != addr.lower():
        return f"{name} <{addr}>"
    return addr or name


def _people(msg: dict, key: str) -> str:
    return ", ".join(w for w in (_who(r.get("emailAddress")) for r in msg.get(key) or []) if w)


def _header(msg: dict) -> str:
    when = (msg.get("receivedDateTime") or msg.get("sentDateTime") or "")[:16].replace("T", " ")
    parts = [f"From: {_who((msg.get('from') or {}).get('emailAddress'))}"]
    for label, key in (("To", "toRecipients"), ("Cc", "ccRecipients")):
        people = _people(msg, key)
        if people:
            parts.append(f"{label}: {people}")
    return f"--- {when} | " + " | ".join(parts) + " ---"


def thread_text(conversation_id: str, *, graph) -> tuple[str, int]:
    """(text, messages shown): the thread's last messages, oldest first,
    each cut to its own words and to MESSAGE_CHARS, about THREAD_CHARS
    in all. The newest message always shows."""
    msgs, _ = graph.get_conversation_messages(conversation_id)
    blocks, total = [], 0
    for m in reversed(msgs or []):
        own = draft_ab.own_text(m)
        if not own:
            continue
        block = f"{_header(m)}\n{own[:MESSAGE_CHARS]}"
        if blocks and total + len(block) > THREAD_CHARS:
            break
        blocks.append(block)
        total += len(block)
    return "\n\n".join(reversed(blocks)), len(blocks)


def draft_text(draft_id: str, *, graph) -> tuple[str, str]:
    """(the current draft's own words, its conversation id)."""
    msg = graph.get_message_full(draft_id) or {}
    return draft_ab.own_text(msg)[:DRAFT_CHARS], msg.get("conversationId") or ""


def messages(brief: str, to: str, *, thread: str = "", draft: str = "") -> list[dict]:
    """The subagent's context for the brief, with the thread and the
    current draft added to the request."""
    msgs = draft_ab.remote_messages(brief, to)
    user = msgs[-1]["content"]
    if thread:
        user += f"\n\n=== The thread (oldest first) ===\n{thread}"
    if draft:
        user += ("\n\n=== The current draft. Revise it as the request says; keep what "
                 f"the request does not change. ===\n{draft}")
    msgs[-1] = {**msgs[-1], "content": user}
    return msgs


# ── the call ──────────────────────────────────────────────────────────

def note_brief(brief: str, rec: dict) -> None:
    """Record, owner-side, the brief about to go to the remote endpoint: its
    size, a hash and its first characters, and the size of the whole
    request (the brief carries ea-bot's words and the files it names). The
    daily outbound review counts these (outbound_review.py)."""
    try:
        endpoint = str(remote_llm.config().get("endpoint") or "")
    except Exception:
        endpoint = ""
    outbound_log.note_owner(
        "remote_brief", "email_writer", host=outbound_log.host_of(endpoint),
        run_id=rec.get("run_id", ""), recipient_domain=rec.get("recipient_domain", ""),
        conversation_id=rec.get("conversation_id", ""), draft_id=rec.get("draft_id", ""),
        sent_chars=rec.get("sent_chars", 0), **outbound_log.brief_fields(brief))


def _compose_and_write(brief: str, to: str, conversation_id: str, draft_id: str,
                       rec: dict, *, graph, chat, t0: float, budget: float) -> tuple[str, dict]:
    thread = draft = ""
    if draft_id:
        try:
            draft, draft_conv = draft_text(draft_id, graph=graph)
        except Exception as e:
            return "", {"reason": f"draft unreadable: {str(e)[:200]}", "kind": "error"}
        rec["conversation_id"] = rec["conversation_id"] or draft_conv
        rec["draft_chars"] = len(draft)
    if conversation_id:
        try:
            thread, rec["thread_messages"] = thread_text(conversation_id, graph=graph)
        except Exception as e:
            # The brief carries what the reply must say; the thread only
            # adds to it, so the body is still written without it.
            rec["thread_error"] = str(e)[:200]
    msgs = messages(brief, to, thread=thread, draft=draft)
    rec["sent_chars"] = sum(len(str(m.get("content") or "")) for m in msgs)
    note_brief(brief, rec)
    left = budget - (time.monotonic() - t0) - 2
    return draft_ab.remote_body(brief, to, chat=chat, messages=msgs, purpose=PURPOSE,
                                timeout=max(5, min(REMOTE_TIMEOUT_S, left)))


WRITER_OFF = {"status": "fallback", "reason": "writer off"}


def writer_on() -> bool:
    """The drafting switch at `remote`, or the routing table's `drafting`
    entry (model_routing.py) where it names local or hosted."""
    cfg = remote_llm.config()
    return remote_llm.local_for("drafting", cfg.get("drafting") == "remote", cfg)


def write(to: str, brief: str, out: str, *, conversation_id: str = "", draft_id: str = "",
          graph=None, chat=None, budget: float = BUDGET_S) -> tuple[dict, int]:
    """(the JSON answer, exit code). Writes the body to `out` and records
    the call; never raises past the writer's own failures."""
    if not writer_on():
        return dict(WRITER_OFF), EXIT_FALLBACK
    t0 = time.monotonic()
    rec = {"ts": _now_iso(), "run_id": os.environ.get("EA_RUN_ID", "")[:64],
           "recipient_domain": to.rsplit("@", 1)[-1],
           "conversation_id": conversation_id, "draft_id": draft_id,
           "brief": brief[:20000]}
    try:
        with _budget(budget):
            if graph is None and (conversation_id or draft_id):
                import graph_client as graph
            body, meta = _compose_and_write(brief, to, conversation_id, draft_id, rec,
                                            graph=graph, chat=chat, t0=t0, budget=budget)
    except _Late:
        body, meta = "", {"reason": f"late: no body within {int(budget)} seconds",
                          "kind": "timeout"}
    except Exception as e:
        body, meta = "", {"reason": f"writer failed: {type(e).__name__}: {str(e)[:200]}",
                          "kind": "error"}
    notes = meta.get("notes", "")
    rec.update(body=body, notes=notes,
               latency_ms=meta.get("latency_ms") or int((time.monotonic() - t0) * 1000))
    if body:
        try:
            owner_trust.write_owner_file(out, body, mode=0o600)
        except (owner_trust.OwnerTrustError, OSError) as e:
            body, meta = "", {"reason": f"--out not writable as this user's own file: {e}",
                              "kind": "error"}
            rec["body"] = ""
    if body:
        rec.update(status="ok", words=len(body.split()), dashes_fixed=meta.get("dashes_fixed", 0))
        answer = {"status": "ok", "writer": "remote", "out": out, "words": rec["words"],
                  "notes": notes, "body": body}
        code = 0
    else:
        rec.update(status=meta.get("kind") or "error", reason=meta.get("reason", "unusable"))
        answer = {"status": "fallback", "reason": rec["reason"]}
        if meta.get("kind") == "need":
            rec["need"] = answer["need"] = meta.get("need", "")
        code = EXIT_FALLBACK
    try:
        remote_llm.append(STORE, rec)
    except OSError as e:
        print(f"email_writer: record not written: {e}", file=sys.stderr)
    return answer, code


def recent_bodies(since_iso: str) -> list[dict]:
    """The bodies the writer produced at or after `since_iso`, oldest
    first: [{ts, run_id, draft_id, conversation_id, recipient_domain,
    body, notes}]."""
    since = _when(since_iso)
    if since is None:
        raise ValueError(f"not an ISO time: {since_iso!r}")
    keys = ("ts", "run_id", "draft_id", "conversation_id", "recipient_domain", "body", "notes")
    out = []
    for r in remote_llm.read(STORE):
        when = _when(r.get("ts"))
        if r.get("status") == "ok" and r.get("body") and when is not None and when >= since:
            out.append({k: r.get(k, "") for k in keys})
    return out


# ── CLI ───────────────────────────────────────────────────────────────

def _error(message: str) -> int:
    print(json.dumps({"status": "error", "message": message}), file=sys.stderr)
    return 2


def build_parser() -> argparse.ArgumentParser:
    """The CLI's argument parser, also what the mediator's argv tripwire
    parses with (tests/test_mediator_argv_tripwire.py)."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], allow_abbrev=False)
    sub = ap.add_subparsers(dest="verb", required=True)
    p = sub.add_parser("write", help="write one email body with the remote model", allow_abbrev=False)
    p.add_argument("--to", required=True, help="the recipient whose style profile applies")
    p.add_argument("--brief-file", required=True,
                   help="what the caller would give the email-author subagent")
    p.add_argument("--out", required=True, help="where the body is written")
    p.add_argument("--conversation-id", default="", help="add the thread's last messages")
    p.add_argument("--draft-id", default="", help="revise this draft")
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    a = ap.parse_args(argv)
    try:
        import mail_mediation as _mm_child
    except ModuleNotFoundError:
        from preprocessor import mail_mediation as _mm_child
    _mm_child.check_child_parse(ap, a)  # the verb the mediator checked
    try:
        to = recipient(a.to)
    except ArgError as e:
        return _error(str(e))
    if not writer_on():
        # The caller falls back to the email-author subagent. The brief is
        # not read: a resident run checks it against the owner's rules, which
        # the resident's own state directory (group-writable) never passes.
        print(json.dumps(WRITER_OFF))
        return EXIT_FALLBACK
    try:
        brief = read_brief(a.brief_file)
    except ArgError as e:
        return _error(str(e))
    try:
        answer, code = write(to, brief, a.out, conversation_id=a.conversation_id.strip(),
                             draft_id=a.draft_id.strip())
    except Exception as e:
        answer = {"status": "fallback", "reason": f"writer failed: {type(e).__name__}"}
        code = EXIT_FALLBACK
    print(json.dumps(answer))
    return code


if __name__ == "__main__":
    sys.exit(main())

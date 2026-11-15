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

With the `voice_examples` switch on, the instructions also carry a few
of the user's own sent emails closest to the brief (sent_index.py: same
recipient first, then anyone; never mail the EA wrote, never this
conversation), with what they do counted, and the style profile keeps
its numbers but not its own samples. The record lists the examples by
id, date and score.

A brief can carry a `KNOW:` part (facts and limits that keep the email
accurate) beside `SAY:` (what it says). A body that restates a KNOW line
gets one revision call, and a sentence that survives it is cut; the
record notes `know_leaks` and `know_fix`.

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
    # The thread read runs for the owner unless its caller set the lane.
    import retry_policy as _retry_policy
    _retry_policy.set_graph_lane(_retry_policy.INTERACTIVE, only_if_unset=True)

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


def messages(brief: str, to: str, *, thread: str = "", draft: str = "",
             examples: str = "", profile: str | None = None) -> list[dict]:
    """The subagent's context for the brief, with the user's own emails
    (`examples`, sent_index.render) added to the instructions and the
    thread and the current draft added to the request. `profile`
    replaces the live style-profile read."""
    msgs = draft_ab.remote_messages(brief, to, profile=profile)
    if examples:
        msgs[0] = {**msgs[0], "content": f"{msgs[0]['content']}\n\n{examples}"}
    user = msgs[-1]["content"]
    if thread:
        user += f"\n\n=== The thread (oldest first) ===\n{thread}"
    if draft:
        user += ("\n\n=== The current draft. Revise it as the request says; keep what "
                 f"the request does not change. ===\n{draft}")
    msgs[-1] = {**msgs[-1], "content": user}
    return msgs


# ── the brief's KNOW part ─────────────────────────────────────────────
#
# A brief has two parts: SAY, what the email says, and KNOW, facts and
# limits that keep it accurate ("no claim that the system works yet",
# "the core scope has no flight requirement"). A writer that states a
# KNOW line hands the recipient a disclaimer or a scope fence nobody
# volunteers, and recipients read that as a model's writing. The writer
# is told so; a body that still restates a KNOW line gets one revision
# call, and a sentence that survives it is cut.

_PART = re.compile(r"^\s*(SAY|KNOW)\s*:\s*(.*)$", re.IGNORECASE)
_SENTENCE = re.compile(r"(?<=[.!?;])\s+|\n+")
_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = frozenset("""the and for but not nor with from that this these those there their
they them then than into onto over under about above below also just only very
any all each some such our ours your yours its his her who whom which what when
where how why are was were been being has have had does did doing will would
shall should can could may might must yet now one two say says said""".split())
# A sentence restates a KNOW line when it holds this share of the line's
# content words, and the line has at least KNOW_MIN of them.
LEAK_SHARE = 0.6
KNOW_MIN = 3
FIX_MIN_S = 20


def brief_parts(brief: str) -> tuple[str, list[str]]:
    """(the brief without its KNOW part, the KNOW part's sentences). A
    brief with no `KNOW:` heading has no KNOW part."""
    keep, know, part = [], [], "say"
    for line in (brief or "").splitlines():
        m = _PART.match(line)
        if m:
            part = m.group(1).lower()
            line = m.group(2)
        (know if part == "know" else keep).append(line)
    sentences = [x.strip(" -*\u2022\t") for x in _SENTENCE.split("\n".join(know))]
    return "\n".join(keep), [x for x in sentences if x]


def _content(text: str) -> set[str]:
    out = set()
    for w in _TOKEN.findall((text or "").lower()):
        if len(w) < 3 or w in _STOP:
            continue
        out.add(w[:-1] if len(w) > 4 and w.endswith("s") else w)
    return out


def _covers(line: set[str], sentence: set[str]) -> bool:
    return len(line) >= KNOW_MIN and len(line & sentence) / len(line) >= LEAK_SHARE


def know_leaks(brief: str, body: str) -> list[str]:
    """The body's sentences that restate a KNOW line the SAY part does
    not also carry."""
    say, know = brief_parts(brief)
    if not know:
        return []
    said = [_content(x) for x in _SENTENCE.split(say)]
    lines = [k for k in (_content(x) for x in know)
             if not any(_covers(k, s) for s in said)]
    leaks = []
    for sentence in _SENTENCE.split(body or ""):
        words = _content(sentence)
        if len(words) >= KNOW_MIN and any(_covers(k, words) for k in lines):
            leaks.append(sentence.strip())
    return leaks


def cut_sentences(body: str, sentences: list[str]) -> str:
    """The body without these sentences, its paragraphs kept."""
    paras = []
    for para in body.split("\n\n"):
        for s in sentences:
            para = para.replace(s, "")
        para = re.sub(r"[ \t]{2,}", " ", para).strip()
        if para:
            paras.append(para)
    return "\n\n".join(paras)


def _drop_know(brief: str, body: str, meta: dict, msgs: list[dict], rec: dict, *,
               chat, left: float) -> tuple[str, dict]:
    """The body with no sentence that restates a KNOW line: one revision
    call when there is time for it, then a cut of what is left."""
    leaks = know_leaks(brief, body)
    if not leaks:
        return body, meta
    rec["know_leaks"] = leaks[:10]
    fixed = ""
    if left >= FIX_MIN_S:
        ask = ("These sentences state what the brief put under KNOW, which is for "
               "accuracy only and never goes in the email:\n"
               + "\n".join(f"- {x}" for x in leaks)
               + "\nWrite the email again without them, and without any sentence "
                 "that only makes sense next to them. Change nothing else.")
        revised, rmeta = draft_ab.remote_body(
            brief, "", chat=chat, purpose=PURPOSE, timeout=max(5, min(REMOTE_TIMEOUT_S, left)),
            messages=[*msgs, {"role": "assistant", "content": body},
                      {"role": "user", "content": ask}])
        if revised and not know_leaks(brief, revised):
            fixed, meta = revised, {**meta, **rmeta}
            rec["know_fix"] = "revised"
    if not fixed:
        fixed = cut_sentences(body, know_leaks(brief, body))
        rec["know_fix"] = "cut"
    return fixed, meta


# ── the user's own emails ─────────────────────────────────────────────

def examples_on(cfg: dict | None = None) -> bool:
    """The `voice_examples` switch: the writer is shown the user's own
    emails closest to the brief (sent_index.py)."""
    cfg = cfg if cfg is not None else remote_llm.config()
    return cfg.get("voice_examples") == "on"


def numbers_only(profile: str) -> str:
    """A style profile without its sample emails: the rates and lengths.
    With the user's own emails in the prompt, the profile's few latest
    samples only add noise, and until the profiles are purged of
    EA-written mail (ea_written.py) some of them are the writer's."""
    try:
        p = json.loads(profile)
    except ValueError:
        return profile
    if isinstance(p, dict):
        p.pop("recent_examples", None)
        p.pop("closing", None)
    return json.dumps(p)


def voice_examples(brief: str, to: str, conversation_id: str, rec: dict) -> str:
    """The prompt section of the user's own emails for this brief, or ''
    (switch off, empty index, endpoint down). Matched on the SAY part:
    what the email says, not the limits behind it."""
    if not examples_on():
        return ""
    t0 = time.monotonic()
    try:
        import sent_index
        say, _ = brief_parts(brief)
        found = sent_index.examples(say, to, conversation_id=conversation_id)
    except Exception as e:
        rec["examples_error"] = f"{type(e).__name__}: {str(e)[:160]}"
        return ""
    rec["examples"] = [{"id": x["id"][-16:], "sent_at": x["sent_at"], "score": x["score"],
                        "same_recipient": to.lower() in x["recipients"]} for x in found]
    rec["examples_ms"] = int((time.monotonic() - t0) * 1000)
    return sent_index.render(found, counts=True)


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
    examples = voice_examples(brief, to, rec["conversation_id"], rec)
    profile = numbers_only(draft_ab._style_profile(to)) if examples and to else None
    msgs = messages(brief, to, thread=thread, draft=draft, examples=examples, profile=profile)
    rec["sent_chars"] = sum(len(str(m.get("content") or "")) for m in msgs)
    note_brief(brief, rec)
    left = budget - (time.monotonic() - t0) - 2
    body, meta = draft_ab.remote_body(brief, to, chat=chat, messages=msgs, purpose=PURPOSE,
                                      timeout=max(5, min(REMOTE_TIMEOUT_S, left)))
    if body:
        body, meta = _drop_know(brief, body, meta, msgs, rec, chat=chat,
                                left=budget - (time.monotonic() - t0) - 2)
    return body, meta


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

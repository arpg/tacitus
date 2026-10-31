#!/usr/bin/env python3
"""Drafting A/B (experimental): Claude or the remote model writes each
email body, chosen at random and blind, and the user's edits before
sending are the score (docs/remote-llm.md, "Drafting A/B").

Only drafts whose body came from the `email-author` subagent in a bot
run take part: that call's prompt is the context both writers get. A
draft is paired with the call whose returned text it holds
(`author_match`); a draft the writer (`email_writer.py`) made in a run
that also called the subagent for another email is the writer's draft
and keeps its body. For a draft drawn to the remote arm, the remote model receives what the
subagent receives: its instructions (.claude/agents/email-author.md),
the recipient's style profile and the voice guide it would have read
with its tools, the caller's prompt, and the text of any file that
prompt names. Its body goes through the same em-dash fix, then replaces
Claude's body in the draft before the Send card is posted. A remote body
that is empty, asks for more (`NEED:`) or is late is dropped and Claude's
body stays, recorded as a fallback.

The user is not told which arm wrote a draft. When he presses Send the
bot reads the body he is sending and records how far it moved from the
body he was shown (word-level edit ratio, unchanged or not, revision
rounds). Cancel records a rejection. The other arm's version is kept too
(Claude's from the draft as created, the remote one generated in the
background), as a secondary measure.

Switch: `"drafting"` in remote-llm.json. `"ab"` runs the experiment
(with `"qwen_share"`, default 0.5) within shadow_until. `"remote"` makes
the remote model the writer of every such draft, with no draw and no end
date: Claude's body stays only as the fallback, and the send measure
still runs. Store: <private_dir>/remote-llm/draft-ab.jsonl (assignments,
each with its mode) and draft-ab-outcomes.jsonl.
"""
from __future__ import annotations

import difflib
import json
import os
import random
import re
import string
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import remote_llm  # noqa: E402

ASSIGN = "draft-ab.jsonl"
OUTCOMES = "draft-ab-outcomes.jsonl"
REPO = Path(__file__).resolve().parent.parent
AGENT_FILE = REPO / ".claude" / "agents" / "email-author.md"
VOICE_GUIDE = REPO / "docs" / "ea" / "drafting-voice.md"
EA_BOT_HOME = Path(os.environ.get("EA_BOT_HOME", "/home/ea-bot"))
REMOTE_TIMEOUT_S = 60
UNCHANGED = 0.02
_PATH = re.compile(r"(/home/ea-bot/[\w./-]+)")
_ADDR = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_QUOTE_START = re.compile(
    r"^(?:\s*>|\s*From:\s|\s*On .{4,120} wrote:\s*$|\s*-{2,}\s*Original Message|\s*_{8,}\s*$)",
    re.I | re.M)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def mode(cfg: dict | None = None, today: date | None = None) -> str:
    """`remote` when the remote model is the writer, `ab` while the
    experiment is on and the run's end date is ahead, else `off`. The
    routing table's `drafting` entry (model_routing.py) outranks the
    switch: `local` is `remote`, `hosted` is `off`."""
    cfg = cfg if cfg is not None else remote_llm.config()
    if not cfg.get("endpoint"):
        return "off"
    routed = remote_llm.routed("drafting", cfg)
    if routed != "auto":
        return "remote" if routed == "local" else "off"
    if cfg.get("drafting") == "remote":
        return "remote"
    if cfg.get("drafting") != "ab":
        return "off"
    until = remote_llm._until(cfg)
    if until is None or (today or date.today()) > until:
        return "off"
    return "ab"


def _share(cfg: dict) -> float:
    return float(cfg.get("qwen_share", 0.5))


# ── text ──────────────────────────────────────────────────────────────

def own_text(msg: dict) -> str:
    """The draft's own words: the HTML cut where the quoted thread starts,
    as plain text, cut again at any quote divider left."""
    body = (msg or {}).get("body") or {}
    content = body.get("content") or ""
    if (body.get("contentType") or "").lower() == "html":
        try:
            import graph_client
            cut = graph_client._split_quoted_thread(content)
            if cut is not None:
                content = content[:cut]
        except Exception:
            pass
        import admissions_filter
        content = admissions_filter.html_to_text(content)
    m = _QUOTE_START.search(content)
    if m:
        content = content[:m.start()]
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in content.splitlines()]
    return "\n".join(ln for ln in lines if ln).strip()


def _words(text: str) -> list[str]:
    return re.findall(r"\S+", text or "")


_PUNCT = string.punctuation + "\u201c\u201d\u2018\u2019\u2026"


def _norm_words(text: str) -> list[str]:
    return [w for w in (x.strip(_PUNCT).lower() for x in _words(text)) if w]


def edit_ratio(shown: str, sent: str) -> float:
    """1 minus the word-level similarity of the two texts: 0 is untouched,
    1 is rewritten."""
    a, b = _words(shown), _words(sent)
    if not a and not b:
        return 0.0
    return round(1 - difflib.SequenceMatcher(None, a, b, autojunk=False).ratio(), 4)


AUTHOR_MATCH = 0.6


def author_match(result: str, body: str) -> float:
    """The share of a draft's own words that the subagent's returned text
    holds in order: 1 when that text is the body, near 0 when it is
    another email's. The result may carry a hand-back preamble or be cut
    short; the matched words count, the rest of the result does not."""
    rw, bw = _norm_words(result), _norm_words(body)
    if not rw or not bw:
        return 0.0
    sm = difflib.SequenceMatcher(None, bw, rw, autojunk=False)
    return round(sum(b.size for b in sm.get_matching_blocks()) / len(bw), 4)


# ── the context the email-author subagent gets ────────────────────────

def author_calls(tool_calls) -> list[dict]:
    """The email-author calls of one run: [{prompt, result}], in order.
    `result` is the text the subagent returned, the body it wrote."""
    out = []
    for t in tool_calls or []:
        name = t[0] if t else ""
        args = t[4] if len(t) > 4 and isinstance(t[4], dict) else {}
        if name in ("Agent", "Task") and args.get("subagent_type") == "email-author":
            prompt = str(args.get("prompt") or "")
            if prompt.strip():
                out.append({"prompt": prompt, "result": str(t[3] or "") if len(t) > 3 else ""})
    return out


def _agent_instructions() -> str:
    text = AGENT_FILE.read_text()
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4:]
    return text.strip()


def _style_profile(addr: str) -> str:
    """What `style_profiles.py get --contact` prints, run as the resident
    exactly as the subagent runs it."""
    import subprocess
    cmd = ["sudo", "-n", "-u", "ea-bot", "env", f"HOME={EA_BOT_HOME}",
           f"EA_DIR={EA_BOT_HOME / '.ea'}", f"PATH={os.environ.get('PATH', '/usr/bin:/bin')}",
           "python3", str(EA_BOT_HOME / "ea-system" / "preprocessor" / "style_profiles.py"),
           "get", "--contact", addr]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        return (p.stdout or "").strip()[:20000]
    except Exception as e:
        return json.dumps({"found": False, "error": str(e)[:100]})


def _named_files(prompt: str) -> str:
    """The text of any resident file the prompt names, as the subagent
    could Read it."""
    import resident_io
    parts = []
    for path in sorted(set(_PATH.findall(prompt)))[:6]:
        text = resident_io.read_text(path.rstrip(".,;:)"))
        if text:
            parts.append(f"=== FILE {path} ===\n{text[:20000]}")
    return "\n\n".join(parts)


USER_PROFILE = EA_BOT_HOME / ".ea" / "config" / "user-profile.json"


def sender_note() -> str:
    """Who the remote model writes as, from user-profile.json: the name
    the user goes by, which is how he signs. The subagent knows it from
    its session; the remote model has only this. The mailbox address
    carries the full given name, so the note says not to take it from
    there."""
    import resident_io
    try:
        name = str(json.loads(resident_io.read_text(USER_PROFILE) or "{}").get("name") or "").strip()
    except (ValueError, OSError):
        name = ""
    if not name:
        return ""
    first = name.split()[0]
    return (f"=== THE SENDER ===\nYou write as {name}. He signs with \"{first}\" alone on "
            f"the last line, unless his examples to this contact show another sign-off. "
            f"Never take his name from an email address or a display name in the thread.")


def remote_messages(prompt: str, recipient: str, profile: str | None = None) -> list[dict]:
    """The remote model's input: the subagent's instructions with the two
    tool reads already done, who the user is, and the caller's prompt.
    `profile` replaces the live style-profile read (the backtest passes
    one cut to the draft's date)."""
    if profile is None:
        profile = _style_profile(recipient) if recipient else json.dumps({"found": False})
    sender = sender_note()
    system = (
        f"{_agent_instructions()}\n\n"
        + (f"{sender}\n\n" if sender else "")
        + "You have no tools. The two reads the instructions ask for are done "
        "and their output is below.\n\n"
        f"=== OUTPUT OF style_profiles.py get --contact {recipient} ===\n{profile}\n\n"
        f"=== {VOICE_GUIDE.relative_to(REPO)} ===\n{VOICE_GUIDE.read_text()}")
    files = _named_files(prompt)
    user = prompt + (f"\n\n{files}" if files else "")
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


_GREETING = re.compile(
    r"^(hi|hello|dear|hey|good (morning|afternoon|evening)|greetings|"
    r"[A-Z][\w'.-]+( [A-Z][\w'.-]+){0,2},)(\s|$)", re.I)
_NOTE_LINE = re.compile(r"^\s*(-{3,}|notes?\b|caller\b|for the caller|\[note|\(note)", re.I)


def split_notes(text: str) -> tuple[str, str]:
    """(the email, notes to the caller). A writer sometimes wraps the body
    in a line for its caller before the greeting or a notes section after
    the sign-off; the email is what goes in the draft."""
    lines = (text or "").strip().splitlines()
    notes = []
    for i, ln in enumerate(lines[:6]):
        if _GREETING.match(ln.strip()):
            if i:
                notes += lines[:i]
                lines = lines[i:]
            break
    for i, ln in enumerate(lines[2:], 2):
        if _NOTE_LINE.match(ln):
            notes += lines[i:]
            lines = lines[:i]
            break
    return "\n".join(lines).strip(), "\n".join(notes).strip()


def finish_body(text: str) -> tuple[str, dict]:
    """(body, metadata) from a writer's raw output: notes to the caller
    split off and dashes fixed. Body '' when the output cannot be used,
    with a `reason` and a `kind` in the metadata: `empty`, or `need` for
    an answer that asks for more (`NEED:`), its question in `need`."""
    text = (text or "").strip()
    if not text:
        return "", {"reason": "empty", "kind": "empty"}
    if text.startswith("NEED:"):
        return "", {"reason": "asked for more: " + text[:200], "kind": "need",
                    "need": text[len("NEED:"):].strip()[:500]}
    try:
        from style_guard import strip_em_dashes
    except ModuleNotFoundError:
        from preprocessor.style_guard import strip_em_dashes
    body, notes = split_notes(text)
    meta = {"notes": notes[:2000]} if notes else {}
    body, n = strip_em_dashes(body)
    meta["dashes_fixed"] = n
    return body, meta


def remote_body(prompt: str, recipient: str, *, chat=None, messages: list[dict] | None = None,
                purpose: str = "draft-ab", timeout: float = REMOTE_TIMEOUT_S) -> tuple[str, dict]:
    """(body, metadata) from the remote model; body '' with a reason in
    the metadata when it cannot be used. `messages` replaces the context
    built from the prompt (the email writer adds the thread and the
    current draft to it)."""
    chat = chat or remote_llm.chat
    t0 = time.monotonic()
    try:
        res = chat(messages or remote_messages(prompt, recipient), purpose=purpose,
                   max_tokens=1500, timeout=timeout)
    except remote_llm.RemoteLLMError as e:
        kind = ("empty" if isinstance(e, remote_llm.ModelOutputError)
                else "timeout" if "timed out" in str(e) else "error")
        return "", {"reason": f"remote failed: {str(e)[:200]}", "kind": kind,
                    "latency_ms": int((time.monotonic() - t0) * 1000)}
    body, meta = finish_body(res.get("content") or "")
    return body, {"latency_ms": res.get("latency_ms") or int((time.monotonic() - t0) * 1000),
                  **meta}


# ── assignment, at draft time ─────────────────────────────────────────

def _pick_call(calls: list[dict], claude_text: str) -> dict | None:
    """The email-author call that wrote this draft: the one whose returned
    text the body holds (author_match at or above AUTHOR_MATCH). None when
    no call did: the body came from the writer, or Claude wrote it itself,
    and a call for another email in the same run must not claim it."""
    scored = [(author_match(c.get("result", ""), claude_text), c) for c in calls]
    if not scored:
        return None
    score, call = max(scored, key=lambda s: s[0])
    return call if score >= AUTHOR_MATCH else None


def prompt_recipient(prompt: str) -> str:
    """The first address in the caller's prompt that is not the user's:
    the contact the subagent looks up."""
    try:
        import mail_tagger
        mine = mail_tagger._user_addresses()
    except Exception:
        mine = set()
    for a in _ADDR.findall(prompt or ""):
        if a.lower() not in mine:
            return a.lower()
    return ""


def _recipient(msg: dict, prompt: str) -> str:
    """Whom the style profile is read for: the contact the prompt names,
    as the subagent decides, else the draft's first To address."""
    named = prompt_recipient(prompt)
    if named:
        return named
    for r in msg.get("toRecipients") or []:
        a = ((r.get("emailAddress") or {}).get("address") or "").lower()
        if a:
            return a
    return ""


def _update_locked(graph, draft_id: str, body: str) -> bool:
    """Put `body` in the draft under its owner-side draft lock (draft_lock),
    the lock a send holds from its content check to the send, so this edit
    never lands between the two. False, with the draft unchanged, when the
    lock is busy (a send is under way)."""
    try:
        import draft_lock
    except ModuleNotFoundError:  # imported as preprocessor.<mod>
        from preprocessor import draft_lock
    try:
        with draft_lock.hold([draft_id]):
            graph.update_draft(draft_id, body)
    except draft_lock.Busy:
        return False
    return True


def assign(draft_id: str, tool_calls, *, run_id: str = "", thread_ts: str = "",
           graph=None, chat=None, rng=None, background: bool = True) -> dict:
    """Give one new Outlook draft its writer and, for the remote one, put
    the remote body in it: drawn in the `ab` mode, always the remote model
    in the `remote` mode. Returns the assignment record (also stored).
    Never raises; with the switch off, or no email-author call whose text
    this draft holds, returns {} and touches nothing (the remote mode
    records the writer's own draft, see note_writer_draft)."""
    try:
        cfg = remote_llm.config()
        m = mode(cfg)
        if m == "off":
            return {}
        calls = author_calls(tool_calls)
        if not calls and m != "remote":
            return {}
        if graph is None:
            import graph_client as graph
        msg = graph.get_message_full(draft_id)
        claude_text = own_text(msg)
        call = _pick_call(calls, claude_text)
        if call is None:
            return (note_writer_draft(draft_id, run_id, thread_ts, graph=graph, msg=msg)
                    if m == "remote" else {})
        recipient = _recipient(msg, call["prompt"])
        arm = "qwen" if m == "remote" or (rng or random).random() < _share(cfg) else "claude"
        rec = {"ts": _now_iso(), "draft_id": draft_id, "run_id": run_id, "thread_ts": thread_ts,
               "mode": m,
               "conversation_id": msg.get("conversationId") or "",
               "recipient_domain": recipient.rsplit("@", 1)[-1] if recipient else "",
               "arm": arm, "claude_body": claude_text, "prompt": call["prompt"][:20000]}
        if arm == "qwen":
            body, meta = remote_body(call["prompt"], recipient, chat=chat)
            rec["remote_meta"] = meta
            if body and not _update_locked(graph, draft_id, body):
                rec.update(shown="claude", fallback="draft lock busy")
            elif body:
                rec.update(qwen_body=body, shown="qwen")
                try:
                    import draft_capture
                    draft_capture.capture(draft_id, body, verb="draft-ab",
                                          conversation_id=rec["conversation_id"])
                except Exception:
                    pass
            else:
                rec.update(shown="claude", fallback=meta.get("reason", "unusable"))
        else:
            rec["shown"] = "claude"
        remote_llm.append(ASSIGN, rec)
        if m == "ab" and rec["shown"] == "claude" and background and not rec.get("qwen_body"):
            # The remote version of a Claude-arm draft, for the secondary
            # measure; off the draft's path.
            threading.Thread(target=_shadow_remote, args=(rec, recipient, chat),
                             daemon=True).start()
        return rec
    except Exception as e:
        print(f"draft_ab.assign: {e}", file=sys.stderr)
        return {}


WRITER_MATCH = 0.6


def note_writer_draft(draft_id: str, run_id: str, thread_ts: str = "", *, graph=None,
                      msg: dict | None = None) -> dict:
    """A draft whose body came from the remote writer (email_writer.py):
    recorded as mode `writer` so the send measure covers it. It is matched
    to the writer's record of this run by text. Never raises."""
    try:
        import email_writer
        since = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
        bodies = [b for b in email_writer.recent_bodies(since)
                  if not run_id or b.get("run_id") == run_id]
        if not bodies:
            return {}
        if msg is None:
            if graph is None:
                import graph_client as graph
            msg = graph.get_message_full(draft_id)
        text = own_text(msg)
        best = min(bodies, key=lambda b: edit_ratio(b["body"], text))
        if 1 - edit_ratio(best["body"], text) < WRITER_MATCH:
            return {}
        rec = {"ts": _now_iso(), "draft_id": draft_id, "run_id": run_id, "thread_ts": thread_ts,
               "mode": "writer", "conversation_id": msg.get("conversationId") or "",
               "recipient_domain": best.get("recipient_domain", ""), "arm": "qwen",
               "shown": "qwen", "qwen_body": text, "claude_body": ""}
        remote_llm.append(ASSIGN, rec)
        return rec
    except Exception as e:
        print(f"draft_ab.note_writer_draft: {e}", file=sys.stderr)
        return {}


def _shadow_remote(rec: dict, recipient: str, chat=None) -> None:
    try:
        body, meta = remote_body(rec["prompt"], recipient, chat=chat)
        if body:
            remote_llm.append(ASSIGN, {"ts": _now_iso(), "draft_id": rec["draft_id"],
                                       "shadow_qwen_body": body, "remote_meta": meta})
    except Exception:
        pass


def assignment(draft_id: str) -> dict | None:
    """The draft's assignment, with the remote version of a Claude-arm
    draft merged in once the background call has written it."""
    base, extra = None, {}
    for r in remote_llm.read(ASSIGN):
        if r.get("draft_id") != draft_id:
            continue
        if "arm" in r:
            base = r
        elif r.get("shadow_qwen_body"):
            extra["shadow_qwen_body"] = r["shadow_qwen_body"]
    return {**base, **extra} if base else None


# ── outcomes, at send or cancel ───────────────────────────────────────

def _revisions(draft_id: str, since: str) -> int:
    try:
        import draft_events
        import resident_io
        n = 0
        for line in (resident_io.read_text(draft_events.JOURNAL) or "").splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("draft_id") == draft_id and e.get("action") == "updated" and e.get("ts", "") > since:
                n += 1
        return n
    except Exception:
        return 0


def snapshot(draft_id: str, *, graph=None) -> str | None:
    """The body about to be sent, for a draft in the experiment; None for
    any other draft (no Graph call then). The Send button calls this
    before sending and note_send after the mail has gone."""
    try:
        if not assignment(draft_id):
            return None
        if graph is None:
            import graph_client as graph
        return own_text(graph.get_message_full(draft_id))
    except Exception as e:
        print(f"draft_ab.snapshot: {e}", file=sys.stderr)
        return None


def note_send(draft_id: str, sent: str | None) -> None:
    """Record how far the body sent moved from the body shown."""
    try:
        a = assignment(draft_id)
        if not a or sent is None:
            return
        shown = a.get("qwen_body") if a.get("shown") == "qwen" else a.get("claude_body", "")
        other = a.get("claude_body") if a.get("shown") == "qwen" else a.get("shadow_qwen_body")
        rec = {"ts": _now_iso(), "draft_id": draft_id, "outcome": "sent", "arm": a["arm"],
               "shown": a.get("shown"), "edit_ratio": edit_ratio(shown, sent),
               "words_shown": len(_words(shown)), "words_sent": len(_words(sent)),
               "revisions": _revisions(draft_id, a["ts"]), "sent_body": sent}
        rec["unchanged"] = rec["edit_ratio"] <= UNCHANGED
        if other:
            rec["other_edit_ratio"] = edit_ratio(other, sent)
        remote_llm.append(OUTCOMES, rec)
    except Exception as e:
        print(f"draft_ab.note_send: {e}", file=sys.stderr)


def note_cancel(draft_id: str) -> None:
    try:
        a = assignment(draft_id)
        if a:
            remote_llm.append(OUTCOMES, {"ts": _now_iso(), "draft_id": draft_id,
                                         "outcome": "cancelled", "arm": a["arm"],
                                         "shown": a.get("shown"),
                                         "revisions": _revisions(draft_id, a["ts"])})
    except Exception:
        pass


# ── the scorecard ─────────────────────────────────────────────────────

def _boot_diff(a: list[float], b: list[float], n: int = 2000, seed: int = 9) -> tuple:
    """95% bootstrap interval for mean(a) - mean(b)."""
    if len(a) < 2 or len(b) < 2:
        return (None, None)
    rng = random.Random(seed)
    diffs = []
    for _ in range(n):
        sa = [rng.choice(a) for _ in a]
        sb = [rng.choice(b) for _ in b]
        diffs.append(sum(sa) / len(sa) - sum(sb) / len(sb))
    diffs.sort()
    return (round(diffs[int(0.025 * n)], 3), round(diffs[int(0.975 * n)], 3))


def _writer_stats(ds: list[str], outcomes: dict) -> tuple[dict, list[float]]:
    sent = [outcomes[d] for d in ds if outcomes.get(d, {}).get("outcome") == "sent"]
    rs = [o["edit_ratio"] for o in sent]
    return {
        "drafts": len(ds), "sent": len(sent),
        "cancelled": sum(1 for d in ds if outcomes.get(d, {}).get("outcome") == "cancelled"),
        "unchanged": sum(1 for o in sent if o.get("unchanged")),
        "edit_ratio_mean": round(sum(rs) / len(rs), 3) if rs else None,
        "edit_ratio_median": sorted(rs)[len(rs) // 2] if rs else None,
        "revisions_mean": round(sum(o.get("revisions", 0) for o in sent) / len(sent), 2) if sent else None,
        "words_shown_mean": round(sum(o["words_shown"] for o in sent) / len(sent)) if sent else None,
        "other_closer": sum(1 for o in sent if o.get("other_edit_ratio") is not None
                            and o["other_edit_ratio"] < o["edit_ratio"]),
    }, rs


def report() -> dict:
    """The experiment's drafts per shown writer: drafts, sent, sent
    unchanged, edit ratio, revision rounds, cancelled, fallbacks; and the
    interval for the difference in edit ratio (remote minus Claude). The
    drafts made in the `remote` mode are a block of their own."""
    assigns, written = {}, {}
    for r in remote_llm.read(ASSIGN):
        if "arm" in r:
            (written if r.get("mode") in ("remote", "writer") else assigns)[r["draft_id"]] = r
    outcomes = {}
    for r in remote_llm.read(OUTCOMES):
        outcomes[r["draft_id"]] = r
    out = {"mode": mode(), "drafts": len(assigns),
           "fallbacks": sum(1 for a in assigns.values() if a.get("fallback"))}
    ratios = {}
    for shown in ("claude", "qwen"):
        out[shown], ratios[shown] = _writer_stats(
            [d for d, a in assigns.items() if a.get("shown") == shown], outcomes)
    out["edit_ratio_diff_ci"] = _boot_diff(ratios["qwen"], ratios["claude"])
    if written:
        out["remote_writer"] = {
            **_writer_stats(list(written), outcomes)[0],
            "since": min(a["ts"] for a in written.values()),
            "fell_back_to_claude": sum(1 for a in written.values() if a.get("fallback"))}
    return out


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("report")
    sub.add_parser("status")
    a = ap.parse_args(argv)
    if a.cmd == "report":
        print(json.dumps(report(), indent=1))
    else:
        print(json.dumps({"mode": mode(), "assigned": len([r for r in remote_llm.read(ASSIGN)
                                                           if "arm" in r])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Drafting backtest (experimental): the drafting A/B replayed on the
past weeks' drafts (docs/remote-llm.md, "Drafting backtest").

For every body the email-author subagent wrote in the window, the
backtest takes the prompt the subagent got, the body that went into the
draft (Claude's), and what the user sent in that thread afterwards. The
remote model writes its own body from the same prompt, with the context
draft_ab gives it, except that the recipient's style profile keeps only
emails sent before the draft (so the email the user sent cannot leak in).
Then:

  * edit ratio of each body against what the user sent. Biased toward
    Claude: the user edited Claude's body, so what he sent is anchored on
    it;
  * a Sonnet judge, blind, without the sent email: which body would he
    rather send as is (accuracy, voice, concision), run in both orders so
    the judge's position bias cancels;
  * a Sonnet judge with the sent email: which body is closer to it.

Mail labelled keep_from_frontier never goes to the judge: it is scored
by edit ratio alone.

Verbs (owner process):
    scan [--days 21]     pair subagent calls with drafts (reads ea-bot's transcripts as ea-bot)
    build                Claude's body, the sent email, the recipient, the kept label
    generate             the remote model's bodies (four at a time)
    judge                the Sonnet judgments (batches of four, three calls at a time)
    report [--examples N]   the scorecard; with --examples a private side-by-side file

The user's own blind votes on the same pairs come from draft_review.py.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import remote_llm  # noqa: E402

DIR = "draft-backtest"
PAIRS = f"{DIR}/pairs.jsonl"
ITEMS = f"{DIR}/items.jsonl"
REMOTE = f"{DIR}/remote.jsonl"
JUDGED = f"{DIR}/judged.jsonl"
JUDGE_MODEL = "sonnet"
BATCH = 4
JUDGE_PARALLEL = 3
TRANSCRIPTS = "/home/ea-bot/.claude/projects"
_HEREDOC = re.compile(r"<<\s*'?EOF'?\s*\n(.*?)\nEOF", re.S)
_CONV = re.compile(r"--conversation-id[= ]+['\"]?([A-Za-z0-9=+/_-]{20,})")
# The statuses graph_actions prints for a new draft, a new reply draft and
# a reply that reused the thread's existing draft.
_CREATED = re.compile(r'"(?:reply_)?draft_created"|"reused_existing_draft"')


def _kept_item(x: dict) -> bool:
    """A backtest item kept from hosted models, under either name of the
    label (items built before the rename carry keep_from_anthropic)."""
    return bool(x.get("keep_from_frontier") or x.get("keep_from_anthropic"))


def _parse(s) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ── scan, as the resident ─────────────────────────────────────────────

def _blocks(event: dict) -> list[dict]:
    content = (event.get("message") or {}).get("content")
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _result_text(block: dict) -> str:
    c = block.get("content")
    if isinstance(c, str):
        return c
    return " ".join(x.get("text", "") for x in c or [] if isinstance(x, dict))


_AGENT_ID = re.compile(r"agentId:\s*([0-9a-f]{8,})")
_REPORT_START = "The report follows:"
MATCH_MIN = 0.4


def _similar(a: str, b: str) -> float:
    import difflib
    wa, wb = (a or "").split(), (b or "").split()
    if not wa or not wb:
        return 0.0
    return difflib.SequenceMatcher(None, wa, wb, autojunk=False).ratio()


def _handback_report(body: str) -> str:
    """The subagent's own output inside a hand-back message: the indented
    text after the frame."""
    i = body.find(_REPORT_START)
    text = body[i + len(_REPORT_START):] if i >= 0 else body
    lines = [ln[2:] if ln.startswith("  ") else ln for ln in text.strip("\n").splitlines()]
    return "\n".join(lines).strip()


def pair_transcript(events: list[dict]) -> list[dict]:
    """Each email-author call of one session paired with the Outlook draft
    its output went into. The call's output comes from its hand-back
    message (or the tool result when it answered in line); a new draft is
    paired with the open call whose output it matches best, else the most
    recent open call; a draft revision closes the most recent open call."""
    out, calls, by_tool, by_agent, uses = [], [], {}, {}, {}
    for e in events:
        if e.get("isSidechain"):
            continue
        ts = e.get("timestamp") or ""
        att = e.get("attachment")
        if isinstance(att, dict) and isinstance(att.get("origin"), dict):
            o = att["origin"]
            if o.get("name") == "email-author" and o.get("from") in by_agent and o.get("body"):
                by_agent[o["from"]]["report"] = _handback_report(o["body"])
            continue
        for b in _blocks(e):
            if b.get("type") == "tool_use":
                inp = b.get("input") or {}
                uses[b.get("id")] = inp
                if inp.get("subagent_type") == "email-author" and str(inp.get("prompt") or "").strip():
                    c = {"agent_ts": ts, "prompt": inp["prompt"], "open": True,
                         "background": bool(inp.get("run_in_background"))}
                    calls.append(c)
                    by_tool[b.get("id")] = c
            elif b.get("type") == "tool_result":
                text = _result_text(b)
                tid = b.get("tool_use_id")
                if tid in by_tool:
                    m = _AGENT_ID.search(text)
                    if m:
                        by_agent[m.group(1)] = by_tool[tid]
                    if "report was delivered" not in text and text.strip():
                        by_tool[tid]["report"] = text.strip()[:8000]
                    continue
                open_calls = [c for c in calls if c["open"]]
                if re.search(r'"(?:draft_)?updated"', text) and open_calls:
                    open_calls[-1]["open"] = False      # a revision used it
                    continue
                if not _CREATED.search(text) or not open_calls:
                    continue
                cmd = str((uses.get(tid) or {}).get("command") or "")
                if "gmail_client" in cmd:
                    continue
                try:
                    data = json.loads(text[text.find("{"):text.rfind("}") + 1])
                except ValueError:
                    data = {}
                draft_id = data.get("id") or data.get("draft_id") or ""
                if not draft_id:
                    continue
                heredoc = _HEREDOC.search(cmd)
                body = heredoc.group(1) if heredoc else ""
                scored = [(c, _similar(c.get("report", ""), body)) for c in open_calls if c.get("report")]
                best = max(scored, key=lambda x: x[1]) if scored and body else (None, 0.0)
                if best[0] is not None and best[1] >= MATCH_MIN:
                    c, how, sim = best[0], "content", round(best[1], 3)
                else:
                    c, how, sim = open_calls[-1], "latest", None
                c["open"] = False
                conv = _CONV.search(cmd)
                out.append({"agent_ts": c["agent_ts"], "prompt": c["prompt"],
                            "background": c["background"], "agent_text": c.get("report", ""),
                            "draft_id": draft_id, "draft_ts": ts, "command_body": body,
                            "conversation_id": data.get("conversation_id") or (conv.group(1) if conv else ""),
                            "pairing": how, "pairing_similarity": sim})
    return out


def scan_resident(days: int) -> list[dict]:
    """Run as ea-bot: every pair in the transcripts touched in the window."""
    import glob
    import time
    cut = time.time() - days * 86400
    pairs = []
    for f in glob.glob(f"{TRANSCRIPTS}/**/*.jsonl", recursive=True):
        if os.path.getmtime(f) < cut:
            continue
        events = []
        with open(f, errors="replace") as fh:
            for line in fh:
                try:
                    events.append(json.loads(line))
                except ValueError:
                    pass
        for p in pair_transcript(events):
            p["session"] = Path(f).stem
            p["lane"] = Path(f).parent.name
            pairs.append(p)
    return pairs


def scan(days: int = 21) -> dict:
    """The owner's side: run the scan as the resident, keep the result."""
    code = (f"import sys, json; sys.path.insert(0, {str(Path(__file__).parent)!r}); "
            f"import draft_backtest as d; print(json.dumps(d.scan_resident({int(days)})))")
    p = subprocess.run(["sudo", "-n", "-u", "ea-bot", "env", "HOME=/home/ea-bot",
                        "python3", "-c", code], capture_output=True, text=True, timeout=600,
                       cwd="/")
    pairs = json.loads(p.stdout or "[]")
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    pairs = [x for x in pairs if (x.get("agent_ts") or "") >= since[:19]]
    remote_llm.purge([PAIRS])
    for x in pairs:
        remote_llm.append(PAIRS, x)
    return {"pairs": len(pairs), "stderr": (p.stderr or "")[-300:]}


# ── build: Claude's body, the sent email ──────────────────────────────

def _captured() -> dict[str, dict]:
    try:
        import draft_capture
        return {c.get("draft_id"): c for c in draft_capture.load_all() if c.get("draft_id")}
    except Exception:
        return {}


def build(*, graph=None, log=print) -> dict:
    import draft_ab
    import mail_tuning
    import mail_privacy
    if graph is None:
        import graph_client as graph
    caps = _captured()
    user = mail_tuning._user_addresses()
    cfg = remote_llm.config()
    pairs = remote_llm.read(PAIRS)
    remote_llm.purge([ITEMS])
    convs: dict[str, list] = {}
    made = 0
    for i, p in enumerate(pairs, 1):
        cap = caps.get(p["draft_id"]) or {}
        # Claude's body for the head-to-head is the subagent's own output:
        # the same input the remote model gets. The draft as created (the
        # creating command's body, else the capture) is what the user saw,
        # so it is the one his edits are measured against.
        shown = p.get("command_body") or ""
        shown_source = "command"
        if not shown and cap.get("body"):
            shown, shown_source = cap["body"], "capture"
        claude = p.get("agent_text") or shown
        cid = p.get("conversation_id") or cap.get("conversation_id") or ""
        match = _similar(claude, shown) if claude and shown else None
        item = {**p, "claude_body": claude.strip(), "claude_shown": shown.strip(),
                "conversation_id": cid,
                "claude_source": "subagent" if p.get("agent_text") else shown_source,
                "shown_source": shown_source,
                "main_session_changed": match is not None and match < 0.9,
                # A pairing by recency whose output does not match the draft
                # is a mispair, and a revised capture already had feedback.
                "suspect_pair": match is not None and match < MATCH_MIN,
                "claude_revised": not p.get("agent_text") and shown_source == "capture"
                and int(cap.get("revisions") or 1) > 1}
        if cid:
            if cid not in convs:
                try:
                    convs[cid] = mail_tuning.conversation(graph, cid)
                except Exception:
                    convs[cid] = []
            t0 = _parse(p.get("draft_ts"))
            mine = [m for m in convs[cid]
                    if mail_tuning._addr(m.get("from")) in user and not m.get("isDraft")
                    and t0 and (_parse(m.get("sentDateTime")) or t0) > t0]
            if mine:
                m = mine[0]
                item["sent_body"] = draft_ab.own_text(m)
                item["sent_ts"] = m.get("sentDateTime")
            # Kept from frontier models: any participant of the thread on the list.
            for m in convs[cid]:
                people = ([mail_tuning._addr(m.get("from"))] + mail_tuning._addrs(m.get("toRecipients"))
                          + mail_tuning._addrs(m.get("ccRecipients")))
                why = next((remote_llm.keep_reason(a, cfg) for a in people
                            if remote_llm.keep_reason(a, cfg)), "")
                if why:
                    item["keep_from_frontier"] = why
                    break
        # The contact the subagent would look up: the one the prompt names,
        # else the draft's first To address.
        to = [a for a in cap.get("to") or [] if isinstance(a, str)]
        item["recipient"] = draft_ab.prompt_recipient(p.get("prompt") or "") or (to[0].lower() if to else "")
        item["missing_files"] = _missing_files(p.get("prompt") or "")
        remote_llm.append(ITEMS, item)
        made += 1
        if i % 25 == 0:
            log(f"  built {i}/{len(pairs)}")
    items = remote_llm.read(ITEMS)
    return {"items": made, "with_claude_body": sum(1 for x in items if x["claude_body"]),
            "sent": sum(1 for x in items if x.get("sent_body")),
            "kept": sum(1 for x in items if _kept_item(x)),
            "prompts_naming_missing_files": sum(1 for x in items if x["missing_files"])}


def _missing_files(prompt: str) -> list[str]:
    import draft_ab
    import resident_io
    return [p for p in sorted(set(draft_ab._PATH.findall(prompt)))
            if resident_io.read_text(p.rstrip(".,;:)")) is None]


# ── generate: the remote bodies ───────────────────────────────────────

def profile_before(raw: str, before: str) -> str:
    """The style profile with only the examples sent before `before`."""
    try:
        prof = json.loads(raw)
    except ValueError:
        return raw
    if isinstance(prof, dict) and isinstance(prof.get("recent_examples"), list):
        prof["recent_examples"] = [x for x in prof["recent_examples"]
                                   if str((x or {}).get("sent_at") or "") < before]
        prof["examples_cut_before"] = before
    return json.dumps(prof)


def generate(*, chat=None, log=print) -> dict:
    import draft_ab
    items = [x for x in remote_llm.read(ITEMS) if x.get("claude_body") and not x.get("missing_files")
             and not x.get("claude_revised") and not x.get("suspect_pair")]
    done = {r["draft_id"] for r in remote_llm.read(REMOTE)}
    todo = [x for x in items if x["draft_id"] not in done]
    real_profile = draft_ab._style_profile

    def one(x):
        before = (x.get("draft_ts") or x.get("agent_ts") or "")[:19]
        # The profile as it stood when the draft was written.
        prof = profile_before(real_profile(x["recipient"]), before) if x.get("recipient") else None
        msgs = draft_ab.remote_messages(x["prompt"], x.get("recipient", ""),
                                        profile=prof)
        try:
            res = (chat or remote_llm.chat)(msgs, purpose="draft-backtest", max_tokens=1500,
                                             timeout=draft_ab.REMOTE_TIMEOUT_S * 3)
            body = (res.get("content") or "").strip()
        except remote_llm.RemoteLLMError as e:
            return {"draft_id": x["draft_id"], "error": str(e)[:200]}
        from style_guard import strip_em_dashes
        body, n = strip_em_dashes(body)
        return {"draft_id": x["draft_id"], "qwen_body": body, "need": body.startswith("NEED:"),
                "dashes_fixed": n, "latency_ms": res.get("latency_ms")}

    with ThreadPoolExecutor(max_workers=remote_llm.max_parallel()) as pool:
        for i, r in enumerate(pool.map(one, todo), 1):
            remote_llm.append(REMOTE, r)
            if i % 20 == 0:
                log(f"  generated {i}/{len(todo)}")
    rows = remote_llm.read(REMOTE)
    return {"generated": sum(1 for r in rows if r.get("qwen_body") and not r.get("need")),
            "need": sum(1 for r in rows if r.get("need")), "errors": sum(1 for r in rows if r.get("error"))}


# ── judge ─────────────────────────────────────────────────────────────

RUBRIC = """The professor's drafting rules: default to short (his median email is 36 words); never an em-dash or en-dash; state the fact and stop, never a closing clause about why it matters; hedge judgments, state facts flat; no adverbs that do no technical work (directly, exactly, genuinely, clearly, simply) and no "X is Y, not Z"; drop what the recipient already knows; names exactly as in the thread; greeting line and sign-off like his past emails to that person."""

QUALITY = """You compare email drafts written for the user, a professor, by two writers from the same instructions. {rubric}

For each numbered item you get the TASK the writers received, then DRAFT A and DRAFT B. Decide which draft he would rather send as it is: it says what the task requires and nothing it does not support (no invented facts, dates, commitments or names), it follows his rules, and it reads like him. Score each draft 1 to 5 on accuracy, voice and concision. Answer with JSON only:
{{"items": [{{"i": 1, "better": "A" | "B" | "tie", "A": {{"accuracy": 4, "voice": 4, "concision": 5}}, "B": {{"accuracy": 3, "voice": 4, "concision": 4}}, "why": "one short clause"}}]}}

{items}"""

CLOSER = """You compare two email drafts with the email that the user actually sent in their place. For each numbered item: DRAFT A, DRAFT B, then SENT. Decide which draft is closer to SENT in what it says and in how it says it (length, greeting, sign-off, wording). Answer with JSON only:
{{"items": [{{"i": 1, "closer": "A" | "B" | "tie"}}]}}

{items}"""


def _task(x: dict) -> str:
    return (x.get("prompt") or "")[:6000]


def _judge_batch(kind: str, batch: list[tuple[dict, str, str]]) -> list[dict]:
    """One Sonnet call over up to BATCH items. Each tuple: item, the body
    shown as A, the body shown as B. Notes a writer left for its caller are
    cut from both first: they never reach a draft."""
    import draft_ab
    import mail_tuning
    batch = [(x, draft_ab.split_notes(a)[0], draft_ab.split_notes(b)[0]) for x, a, b in batch]
    parts = []
    for j, (x, a, b) in enumerate(batch, 1):
        if kind == "quality":
            parts.append(f"=== ITEM {j} ===\n--- TASK ---\n{_task(x)}\n--- DRAFT A ---\n{a}\n--- DRAFT B ---\n{b}")
        else:
            parts.append(f"=== ITEM {j} ===\n--- DRAFT A ---\n{a}\n--- DRAFT B ---\n{b}\n"
                         f"--- SENT ---\n{x.get('sent_body', '')}")
    tmpl = QUALITY if kind == "quality" else CLOSER
    prompt = tmpl.format(rubric=RUBRIC, items="\n\n".join(parts))
    got = mail_tuning._json_in(mail_tuning.run_claude(prompt, JUDGE_MODEL)).get("items") or []
    return [g for g in got if isinstance(g, dict)]


def judge(*, log=print, seed: int = 21) -> dict:
    """Quality in both orders (A=Claude then A=remote) and closeness in one
    random order, for every item outside the kept list with both bodies."""
    remote = {r["draft_id"]: r for r in remote_llm.read(REMOTE)
              if r.get("qwen_body") and not r.get("need")}
    items = [x for x in remote_llm.read(ITEMS)
             if x["draft_id"] in remote and x.get("claude_body") and not _kept_item(x)]
    rng = random.Random(seed)
    jobs = []
    for order in ("claude_first", "remote_first"):
        for k in range(0, len(items), BATCH):
            part = items[k:k + BATCH]
            batch = [(x, x["claude_body"], remote[x["draft_id"]]["qwen_body"]) if order == "claude_first"
                     else (x, remote[x["draft_id"]]["qwen_body"], x["claude_body"]) for x in part]
            jobs.append(("quality", order, batch))
    sent_items = [x for x in items if x.get("sent_body")]
    for k in range(0, len(sent_items), BATCH):
        part = sent_items[k:k + BATCH]
        batch, orders = [], []
        for x in part:
            flip = rng.random() < 0.5
            orders.append("remote_first" if flip else "claude_first")
            q = remote[x["draft_id"]]["qwen_body"]
            batch.append((x, q, x["claude_body"]) if flip else (x, x["claude_body"], q))
        jobs.append(("closer", orders, batch))

    def run(job):
        kind, order, batch = job
        return job, _judge_batch(kind, batch)

    remote_llm.purge([JUDGED])
    with ThreadPoolExecutor(max_workers=JUDGE_PARALLEL) as pool:
        for n, ((kind, order, batch), got) in enumerate(pool.map(run, jobs), 1):
            by_i = {g.get("i"): g for g in got}
            for j, (x, _a, _b) in enumerate(batch, 1):
                g = by_i.get(j)
                if not g:
                    continue
                o = order if isinstance(order, str) else order[j - 1]
                remote_llm.append(JUDGED, {"draft_id": x["draft_id"], "kind": kind, "order": o, **g})
            log(f"  judged {n}/{len(jobs)} batches")
    return {"items": len(items), "batches": len(jobs)}


# ── report ────────────────────────────────────────────────────────────

def _writer(letter: str, order: str) -> str:
    """Which writer a judge's A or B was."""
    first = "claude" if order == "claude_first" else "remote"
    other = "remote" if first == "claude" else "claude"
    return first if letter == "A" else other if letter == "B" else "tie"


def _ci(wins: int, n: int) -> tuple:
    import math
    if not n:
        return (None, None)
    p, z = wins / n, 1.96
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    r = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (round((c - r) / d, 2), round((c + r) / d, 2))


def report(examples: int = 0) -> dict:
    import draft_ab
    items = {x["draft_id"]: x for x in remote_llm.read(ITEMS)}
    remote = {r["draft_id"]: r for r in remote_llm.read(REMOTE)}
    judged = remote_llm.read(JUDGED)
    out = {"pairs": len(items),
           "with_both_bodies": sum(1 for d, x in items.items() if x.get("claude_body")
                                   and (remote.get(d) or {}).get("qwen_body")),
           "remote_asked_for_more": sum(1 for r in remote.values() if r.get("need")),
           "remote_errors": sum(1 for r in remote.values() if r.get("error")),
           "skipped_missing_files": sum(1 for x in items.values() if x.get("missing_files")),
           "skipped_claude_revised": sum(1 for x in items.values() if x.get("claude_revised")),
           "skipped_suspect_pair": sum(1 for x in items.values() if x.get("suspect_pair")),
           "claude_source": {s: sum(1 for x in items.values() if x.get("claude_source") == s)
                             for s in ("subagent", "command", "capture")},
           "pairing": {s: sum(1 for x in items.values() if x.get("pairing") == s)
                       for s in ("content", "latest")},
           "main_session_changed_subagent_text": sum(1 for x in items.values()
                                                     if x.get("main_session_changed"))}
    # Edit ratio against what was sent.
    er_c, er_r = [], []
    for d, x in items.items():
        q = (remote.get(d) or {}).get("qwen_body")
        if x.get("sent_body") and x.get("claude_body") and q and not remote[d].get("need"):
            er_c.append(draft_ab.edit_ratio(x.get("claude_shown") or draft_ab.split_notes(x["claude_body"])[0],
                                            x["sent_body"]))
            er_r.append(draft_ab.edit_ratio(draft_ab.split_notes(q)[0], x["sent_body"]))
    out["sent_pairs"] = len(er_c)
    if er_c:
        out["edit_ratio_mean"] = {"claude": round(sum(er_c) / len(er_c), 3),
                                  "remote": round(sum(er_r) / len(er_r), 3)}
        out["edit_ratio_median"] = {"claude": sorted(er_c)[len(er_c) // 2],
                                    "remote": sorted(er_r)[len(er_r) // 2]}
        out["claude_sent_unchanged"] = sum(1 for v in er_c if v <= draft_ab.UNCHANGED)
        out["remote_closer_by_edit"] = sum(1 for a, b in zip(er_c, er_r) if b < a)
    # Quality: an item counts as a win only when both orders agree.
    q = {}
    for r in judged:
        if r["kind"] == "quality":
            q.setdefault(r["draft_id"], {})[r["order"]] = r
    wins = {"claude": 0, "remote": 0, "split_or_tie": 0}
    scores = {"claude": {"accuracy": [], "voice": [], "concision": []},
              "remote": {"accuracy": [], "voice": [], "concision": []}}
    for d, both in q.items():
        picks = {_writer(r.get("better"), o) for o, r in both.items()}
        if len(both) == 2 and len(picks) == 1 and "tie" not in picks:
            wins[picks.pop()] += 1
        else:
            wins["split_or_tie"] += 1
        for o, r in both.items():
            for letter in ("A", "B"):
                w = _writer(letter, o)
                for k in ("accuracy", "voice", "concision"):
                    v = (r.get(letter) or {}).get(k)
                    if isinstance(v, (int, float)):
                        scores[w][k].append(v)
    decided = wins["claude"] + wins["remote"]
    out["quality"] = {**wins, "judged": len(q),
                      "remote_win_share_of_decided": round(wins["remote"] / decided, 3) if decided else None,
                      "ci_95": _ci(wins["remote"], decided),
                      "mean_scores": {w: {k: round(sum(v) / len(v), 2) if v else None
                                          for k, v in s.items()} for w, s in scores.items()}}
    c = {"claude": 0, "remote": 0, "tie": 0}
    for r in judged:
        if r["kind"] == "closer":
            c[_writer(r.get("closer"), r["order"])] += 1
    out["closer_to_sent"] = {**c, "note": "anchored: the user edited Claude's body"}
    import draft_review
    if draft_review.votes():
        out["user_votes"] = draft_review.summary()
    if examples:
        _write_examples(examples, items, remote, q)
    return out


def _write_examples(n: int, items: dict, remote: dict, q: dict) -> None:
    rows = [d for d in q if items.get(d, {}).get("sent_body")][:n]
    lines = ["# Drafting backtest: examples (private)", ""]
    for i, d in enumerate(rows, 1):
        x, r = items[d], remote[d]
        verdicts = "; ".join(f"{o}: {v.get('better')} ({v.get('why', '')})" for o, v in q[d].items())
        lines += [f"## {i}. {x.get('agent_ts', '')[:16]} to {x.get('recipient')}", "",
                  "**Task (excerpt)**", "", "> " + _task(x)[:600].replace("\n", "\n> "), "",
                  "**Claude**", "", "```", x["claude_body"], "```", "",
                  "**Remote**", "", "```", r["qwen_body"], "```", "",
                  "**Sent**", "", "```", x.get("sent_body", ""), "```", "",
                  f"Judge: {verdicts}", ""]
    rel = f"{DIR}/examples.md"
    path = remote_llm.store_dir() / rel
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    remote_llm._owner_trust.write_owner_file(path, "\n".join(lines).encode(), mode=0o600)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("scan")
    p.add_argument("--days", type=int, default=21)
    sub.add_parser("build")
    sub.add_parser("generate")
    sub.add_parser("judge")
    p = sub.add_parser("report")
    p.add_argument("--examples", type=int, default=0)
    a = ap.parse_args(argv)
    log = lambda s: print(s, flush=True)  # noqa: E731
    res = {"scan": lambda: scan(a.days), "build": lambda: build(log=log),
           "generate": lambda: generate(log=log), "judge": lambda: judge(log=log),
           "report": lambda: report(a.examples)}[a.cmd]()
    print(json.dumps(res, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

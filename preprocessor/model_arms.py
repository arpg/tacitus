#!/usr/bin/env python3
"""model_arms: the A/B between Claude and the on-prem model, one arm per
chat topic.

The bot draws an arm for a topic on its first message (`arm_for`) and
keeps it: every later turn in that topic runs on the same arm. `remote`
runs the turn on the on-prem model through the tool gateway, in the same
Claude Code harness with the same allowlist; `claude` is the bot as it
is. The user can pin a topic with `use claude` or `use qwen` (`pin`) and
ask `which model` (`arm_of`). Both arms post under the same prefix.

The measure is outcomes, not opinion. Every turn is noted (`note_turn`:
arm, lane, elapsed, cost, tools, the drafts it created, a fallback to
Claude, the backstop's verdict, a reply-check bounce); every Send and
Cancel on a draft (`note_draft`, with the edit ratio against the body
the turn wrote); every Create, Reschedule and Delete button
(`note_event_outcome`). `report` reads them per lane and arm; the 17:30
report carries `summary_lines`.

Config, in remote-llm.json: {"ab": {"share": 0.5, "lanes": ["sched-channel",
"main", "dm"], "from": "2026-10-13", "until": "2026-10-27"}}. Off when
the share is 0, the lanes are empty or the window is closed; a topic
drawn earlier keeps its arm for its life. `enable`/`disable` write it.
Owner process: the store is the owner's private remote-llm directory.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import owner_trust as _owner_trust  # noqa: E402
import remote_llm as _rl  # noqa: E402

ARMS_FILE = "ab-arms.json"
TURNS = "ab-turns.jsonl"
DRAFTS = "ab-drafts.jsonl"
EVENTS = "ab-events.jsonl"
ARMS = ("claude", "remote", "pipeline", "codex")
LANES = ("main", "dm", "sched-channel", "room", "proactive")
DEFAULT_DAYS = 14
EDITED_AT = 0.15          # edit ratio at or above which a sent draft counts as edited
_ALIASES = {"claude": "claude", "qwen": "remote", "remote": "remote", "on-prem": "remote",
            "onprem": "remote", "local": "remote", "pipeline": "pipeline", "codex": "codex"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso() -> str:
    return _now().isoformat(timespec="seconds")


def _key(channel: str, thread_ts: str) -> str:
    return f"{channel}|{thread_ts}"


# ── config ────────────────────────────────────────────────────────────

def settings(cfg: dict | None = None) -> dict:
    cfg = cfg if cfg is not None else _rl.config()
    ab = cfg.get("ab") if isinstance(cfg.get("ab"), dict) else {}
    kind = str(ab.get("arm") or "remote")
    return {"share": float(ab.get("share") or 0.0), "lanes": list(ab.get("lanes") or []),
            "from": str(ab.get("from") or ""), "until": str(ab.get("until") or ""),
            "arm": kind if kind in ARMS and kind != "claude" else "remote"}


def enabled(cfg: dict | None = None, today: date | None = None) -> bool:
    """Drawing is on: a share, lanes, the endpoint, and today inside the
    window (an empty bound is open on that side)."""
    cfg = cfg if cfg is not None else _rl.config()
    s = settings(cfg)
    if not cfg.get("endpoint") or s["share"] <= 0 or not s["lanes"]:
        return False
    today = today or date.today()
    try:
        if s["from"] and today < date.fromisoformat(s["from"]):
            return False
        if s["until"] and today > date.fromisoformat(s["until"]):
            return False
    except ValueError:
        return False
    return True


def remote_model(cfg: dict | None = None) -> str:
    cfg = cfg if cfg is not None else _rl.config()
    return str(cfg.get("model") or "")


def _raw_config() -> tuple[Path, dict]:
    path = _owner_trust.private_dir() / _rl.CONFIG_FILE
    try:
        raw = json.loads(_owner_trust.read_owner_file(path))
    except FileNotFoundError:
        raise _rl.RemoteLLMError(f"{path} does not exist; write the endpoint and model first") from None
    if not isinstance(raw, dict):
        raise _rl.RemoteLLMError(f"{path} is not a JSON object")
    return path, raw


def enable(share: float = 0.5, lanes: list[str] | None = None, days: int = DEFAULT_DAYS,
           today: date | None = None, arm: str = "remote") -> dict:
    """Write the `ab` block: a share, the lanes, a window of `days` from
    today, and which arm a drawn topic gets (`remote`, the on-prem model
    in the Claude Code harness; `pipeline`, sched_pipeline). Owner only."""
    if arm not in ARMS or arm == "claude":
        raise ValueError("arm is remote or pipeline")
    if not _rl._owner():
        raise _rl.RemoteLLMError("only the owner can set the A/B")
    lanes = [l for l in (lanes or ["sched-channel"]) if l in LANES]
    if not lanes:
        raise ValueError(f"lanes are among {', '.join(LANES)}")
    share = min(1.0, max(0.0, float(share)))
    today = today or date.today()
    from datetime import timedelta
    block = {"share": share, "lanes": lanes, "from": today.isoformat(),
             "until": (today + timedelta(days=int(days))).isoformat(), "arm": arm}
    path, raw = _raw_config()
    raw["ab"] = block
    _owner_trust.write_owner_file(path, (json.dumps(raw, indent=2) + "\n").encode(), mode=0o600)
    return block


def disable() -> dict:
    """Close the window today; topics already drawn keep their arm."""
    if not _rl._owner():
        raise _rl.RemoteLLMError("only the owner can set the A/B")
    path, raw = _raw_config()
    block = raw.get("ab") if isinstance(raw.get("ab"), dict) else {}
    block["until"] = (date.today() - __import__("datetime").timedelta(days=1)).isoformat()
    raw["ab"] = block
    _owner_trust.write_owner_file(path, (json.dumps(raw, indent=2) + "\n").encode(), mode=0o600)
    return block


# ── the arms ──────────────────────────────────────────────────────────

def _arms_path() -> Path:
    return _rl.store_dir() / ARMS_FILE


def _load_arms() -> dict:
    try:
        data = json.loads(_owner_trust.read_owner_file(_arms_path()))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def _save_arms(arms: dict) -> None:
    _owner_trust.write_owner_file(_arms_path(), (json.dumps(arms, indent=1) + "\n").encode(), mode=0o600)


def arm_of(channel: str, thread_ts: str) -> dict | None:
    """The topic's row {arm, lane, assigned, pinned, reason} or None."""
    return _load_arms().get(_key(channel, thread_ts))


def arm_for(channel: str, thread_ts: str, lane: str, *, cfg: dict | None = None,
            rng=None, today: date | None = None) -> str:
    """The arm this turn runs on. A topic drawn or pinned earlier keeps
    its arm whatever the config says now; a new topic in a lane the A/B
    covers draws by the share while the window is open; anything else is
    Claude, and is not recorded."""
    cfg = cfg if cfg is not None else _rl.config()
    if not _rl._owner():
        return "claude"
    arms = _load_arms()
    row = arms.get(_key(channel, thread_ts))
    if row and row.get("arm") in ARMS:
        return row["arm"]
    if not enabled(cfg, today) or lane not in settings(cfg)["lanes"]:
        return "claude"
    draw = (rng or random).random()
    arm = settings(cfg)["arm"] if draw < settings(cfg)["share"] else "claude"
    arms[_key(channel, thread_ts)] = {"arm": arm, "lane": lane, "assigned": _iso(), "pinned": False,
                                      "reason": "drawn", "draw": round(draw, 3)}
    _save_arms(arms)
    return arm


def pin(channel: str, thread_ts: str, arm: str, *, lane: str = "", reason: str = "user") -> dict:
    """Fix a topic's arm (`use claude`, `use qwen`). A pin away from a
    drawn arm is an escalation and counts against that arm."""
    arm = _ALIASES.get(str(arm).strip().lower(), "")
    if arm not in ARMS:
        raise ValueError("the arms are claude and qwen")
    arms = _load_arms()
    k = _key(channel, thread_ts)
    old = arms.get(k) or {}
    row = {"arm": arm, "lane": old.get("lane") or lane, "assigned": old.get("assigned") or _iso(),
           "pinned": True, "reason": reason, "pinned_at": _iso(), "from_arm": old.get("arm") or ""}
    arms[k] = row
    _save_arms(arms)
    if old.get("arm") and old.get("arm") != arm:
        _rl.append(TURNS, {"ts": _iso(), "kind": "escalation", "channel": channel, "thread_ts": thread_ts,
                           "lane": row["lane"], "arm": old["arm"], "to": arm, "reason": reason})
    return row


# ── the outcomes ──────────────────────────────────────────────────────

def note_turn(*, run_id: str, channel: str, thread_ts: str, lane: str, arm: str, model: str,
              rc: int, elapsed_s: float, num_turns: int = 0, cost_usd: float | None = None,
              tools: int = 0, drafts: dict | None = None, events_staged: int = 0,
              fallback: str = "", backstop: dict | None = None, bounced: bool = False,
              error: str = "", withdrawn: int = 0, redo: str = "") -> None:
    """One turn's record. `drafts` maps each draft id the turn created to
    the body it wrote (the edit measure at send time). A remote turn the
    backstop withheld carries `withdrawn` (the drafts it made that were
    deleted) and `fallback`; Claude's turn on the same message carries
    `redo` with that reason, so each arm's row is its own work."""
    if arm not in ARMS:
        return
    rec = {"ts": _iso(), "kind": "turn", "run_id": run_id, "channel": channel, "thread_ts": thread_ts,
           "lane": lane, "arm": arm, "model": model, "rc": int(rc), "elapsed_s": round(float(elapsed_s), 1),
           "num_turns": int(num_turns), "cost_usd": cost_usd, "tools": int(tools),
           "drafts": {k: str(v or "")[:6000] for k, v in (drafts or {}).items()},
           "events_staged": int(events_staged), "fallback": fallback,
           "backstop": (backstop or {}).get("verdict", ""),
           "backstop_issues": [str(i.get("detail") or "")[:1000] for i in (backstop or {}).get("issues") or []],
           "bounced": bool(bounced), "error": str(error or "")[:200],
           "withdrawn": int(withdrawn), "redo": str(redo or "")}
    _rl.append(TURNS, rec)


def _turn_for_draft(draft_id: str) -> dict | None:
    for t in reversed(_rl.read(TURNS)):
        if t.get("kind") == "turn" and draft_id in (t.get("drafts") or {}):
            return t
    return None


def note_draft(draft_id: str, outcome: str, *, sent_body: str | None = None) -> dict | None:
    """A Send or Cancel on a draft a noted turn created. Returns the
    record, or None when no arm's turn made the draft."""
    t = _turn_for_draft(draft_id)
    if t is None:
        return None
    ratio = None
    if outcome == "sent" and sent_body is not None:
        try:
            import draft_ab
            ratio = draft_ab.edit_ratio(str(t["drafts"].get(draft_id) or ""), str(sent_body))
        except Exception:
            ratio = None
        if ratio is not None and ratio >= EDITED_AT:
            outcome = "sent_edited"
    rec = {"ts": _iso(), "draft_id": draft_id, "run_id": t.get("run_id"), "thread_ts": t.get("thread_ts"),
           "lane": t.get("lane"), "arm": t.get("arm"), "outcome": outcome, "edit_ratio": ratio}
    _rl.append(DRAFTS, rec)
    return rec


def note_event_outcome(kind: str, thread_ts: str, event: dict, ok: bool) -> dict | None:
    """A Create, Reschedule or Delete button in a topic that has an arm."""
    arms = _load_arms()
    row = next((r for k, r in arms.items() if k.endswith("|" + str(thread_ts))), None)
    if not row:
        return None
    rec = {"ts": _iso(), "kind": str(kind), "thread_ts": thread_ts, "lane": row.get("lane"),
           "arm": row.get("arm"), "ok": bool(ok), "subject": str((event or {}).get("subject") or "")[:80]}
    _rl.append(EVENTS, rec)
    return rec


# ── the report ────────────────────────────────────────────────────────

def _pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(q * (len(xs) - 1) + 0.5))], 1)


def report(since: date | None = None) -> dict:
    """Per lane and arm: turns, errors, fallbacks, backstop verdicts,
    bounces, drafts and their outcomes, events, latency, cost,
    escalations."""
    def recent(rows):
        if not since:
            return rows
        return [r for r in rows if str(r.get("ts") or "")[:10] >= since.isoformat()]
    turns = recent(_rl.read(TURNS))
    drafts = recent(_rl.read(DRAFTS))
    events = recent(_rl.read(EVENTS))
    out: dict = {"lanes": {}, "arms": {}}

    def bucket(d: dict, lane: str, arm: str) -> dict:
        return d.setdefault(lane, {}).setdefault(arm, {
            "turns": 0, "errors": 0, "fallbacks": {}, "backstop": {"ok": 0, "withheld": 0},
            "bounced": 0, "drafts": 0, "withdrawn": 0, "redo": 0, "sent": 0, "sent_edited": 0,
            "cancelled": 0, "events": {}, "escalations": 0, "elapsed_s": [], "cost_usd": 0.0, "tools": 0})
    for t in turns:
        if t.get("kind") == "escalation":
            bucket(out["lanes"], t.get("lane", "?"), t.get("arm", "?"))["escalations"] += 1
            continue
        b = bucket(out["lanes"], t.get("lane", "?"), t.get("arm", "?"))
        b["turns"] += 1
        b["errors"] += 1 if t.get("rc") else 0
        if t.get("fallback"):
            b["fallbacks"][t["fallback"]] = b["fallbacks"].get(t["fallback"], 0) + 1
        if t.get("backstop") == "ok":
            b["backstop"]["ok"] += 1
        elif t.get("backstop") == "withhold":
            b["backstop"]["withheld"] += 1
        b["bounced"] += 1 if t.get("bounced") else 0
        b["drafts"] += len(t.get("drafts") or {})
        b["withdrawn"] += int(t.get("withdrawn") or 0)
        b["redo"] += 1 if t.get("redo") else 0
        b["elapsed_s"].append(float(t.get("elapsed_s") or 0))
        b["cost_usd"] += float(t.get("cost_usd") or 0)
        b["tools"] += int(t.get("tools") or 0)
    for d in drafts:
        b = bucket(out["lanes"], d.get("lane", "?"), d.get("arm", "?"))
        if d.get("outcome") in ("sent", "sent_edited", "cancelled"):
            b[d["outcome"]] += 1
    for e in events:
        b = bucket(out["lanes"], e.get("lane", "?"), e.get("arm", "?"))
        k = f"{e.get('kind')}:{'ok' if e.get('ok') else 'failed'}"
        b["events"][k] = b["events"].get(k, 0) + 1
    for lane, arms in out["lanes"].items():
        for arm, b in arms.items():
            xs = b.pop("elapsed_s")
            b["latency_s"] = {"p50": _pct(xs, 0.5), "p95": _pct(xs, 0.95)}
            b["cost_usd"] = round(b["cost_usd"], 2)
            b["sent_unchanged_share"] = (round(b["sent"] / b["drafts"], 2) if b["drafts"] else None)
            a = out["arms"].setdefault(arm, {"turns": 0, "drafts": 0, "sent": 0, "sent_edited": 0,
                                             "cancelled": 0, "fallbacks": 0, "withheld": 0, "escalations": 0})
            a["turns"] += b["turns"]
            a["drafts"] += b["drafts"]
            a["sent"] += b["sent"]
            a["sent_edited"] += b["sent_edited"]
            a["cancelled"] += b["cancelled"]
            a["fallbacks"] += sum(b["fallbacks"].values())
            a["withheld"] += b["backstop"]["withheld"]
            a["escalations"] += b["escalations"]
    arms = _load_arms()
    out["topics"] = {arm: sum(1 for r in arms.values() if r.get("arm") == arm) for arm in ARMS}
    out["pinned"] = sum(1 for r in arms.values() if r.get("pinned"))
    out["settings"] = settings()
    out["enabled"] = enabled()
    return out


def _report_text(r: dict) -> str:
    s = r["settings"]
    lines = [f"Model A/B ({'on' if r['enabled'] else 'off'}; share {s['share']}, lanes {', '.join(s['lanes']) or '-'}, "
             f"window {s['from'] or '-'}..{s['until'] or '-'}): topics {json.dumps(r['topics'])}, pinned {r['pinned']}"]
    for lane in sorted(r["lanes"]):
        for arm in ARMS:
            b = r["lanes"][lane].get(arm)
            if not b:
                continue
            lines.append(
                f"  {lane}/{arm}: turns {b['turns']} (errors {b['errors']}, fallbacks {json.dumps(b['fallbacks'])}, "
                f"backstop {json.dumps(b['backstop'])}, redo {b['redo']}, bounced {b['bounced']}, "
                f"escalations {b['escalations']}); "
                f"drafts {b['drafts']} (withdrawn {b['withdrawn']}): sent {b['sent']}, edited {b['sent_edited']}, "
                f"cancelled {b['cancelled']} "
                f"(unchanged share {b['sent_unchanged_share']}); events {json.dumps(b['events'])}; "
                f"latency s {json.dumps(b['latency_s'])}; cost ${b['cost_usd']}; tools {b['tools']}")
    return "\n".join(lines)


def summary_lines(cfg: dict | None = None) -> list[str]:
    """The 17:30 report's lines: nothing while the A/B is off and no
    topic has an arm."""
    try:
        r = report()
    except Exception as e:
        return [f"Model A/B: report failed: {str(e)[:120]}"]
    if not r["enabled"] and not any(r["topics"].values()):
        return []
    return _report_text(r).splitlines()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("enable", help="open the window: a share, the lanes, the days")
    s.add_argument("--share", type=float, default=0.5)
    s.add_argument("--lanes", default="sched-channel", help=f"comma-separated, among {', '.join(LANES)}")
    s.add_argument("--days", type=int, default=DEFAULT_DAYS)
    s.add_argument("--arm", default="remote", choices=("remote", "pipeline"),
                   help="what a drawn topic runs on: the on-prem model in the harness, or the pipeline")
    sub.add_parser("disable", help="close the window today (drawn topics keep their arm)")
    s = sub.add_parser("report", help="the scorecard")
    s.add_argument("--since", default="")
    s.add_argument("--json", action="store_true")
    sub.add_parser("arms", help="every topic's arm")
    s = sub.add_parser("pin", help="pin one topic")
    s.add_argument("--channel", required=True)
    s.add_argument("--thread", required=True)
    s.add_argument("--arm", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "enable":
        print(json.dumps(enable(a.share, [l.strip() for l in a.lanes.split(",") if l.strip()], a.days, arm=a.arm)))
    elif a.cmd == "disable":
        print(json.dumps(disable()))
    elif a.cmd == "report":
        r = report(date.fromisoformat(a.since) if a.since else None)
        print(json.dumps(r, indent=1) if a.json else _report_text(r))
    elif a.cmd == "arms":
        for k, v in sorted(_load_arms().items(), key=lambda kv: kv[1].get("assigned", "")):
            print(f"{k}  {v.get('arm')}  {v.get('lane')}  {v.get('assigned')}"
                  f"{'  pinned (' + str(v.get('reason')) + ')' if v.get('pinned') else ''}")
    elif a.cmd == "pin":
        print(json.dumps(pin(a.channel, a.thread, a.arm, reason="cli")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

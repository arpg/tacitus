#!/usr/bin/env python3
"""Tiers: who the user is talking to in a chat topic.

The tiers, by name:

  local   the on-prem model through the tool gateway (the `remote` arm of
          model_arms); every scheduling write it makes passes the Claude
          backstop before a button;
  opus    the default colleague (the bot's own model, EA_MODEL);
  fable   the frontier model, for the hard calls;
  codex   Codex through its own harness (codex_turn).

The hosted tiers come from the hosted models' registry
(ea_config.HOSTED_MODELS): a new hosted model brings its tier, its
aliases and its footer name with its entry.

Four ways to pick one, strongest first:

  1. a tag on the message, for that turn only: `@fable ...`,
     `ask fable: ...` (also `@opus`, `@local`; the tag is stripped before
     the prompt is built);
  2. a sticky pin on the topic: `use fable` / `use opus` / `use local`
     (`escalate` is `use fable`, `hand back` is `use opus`), kept on the
     topic's session row as `tier`;
  3. the topic's name: `[fable] budget plan` (a Zulip topic rename
     is one click, and the bot follows renames);
  4. the lane's default: the owner's routing table (model_routing.py)
     when the lane's task (`chat` for main, dm and room; `scheduling` for
     sched-channel and proactive) is `local` or `hosted`, else ea-config
     `tiers.lanes` (main, dm, sched-channel, room, proactive). The table's
     hosted model for the task (its override, else the one switch
     `hosted_default`; Settings: Claude or Codex) sits over the tiers
     block: Codex makes a hosted pick `codex`, Claude takes a `codex`
     lane default back to the bot's own model. With neither set, a
     `codex` lane default stands.

With none of them the turn runs where today's knobs put it: an adopted
session's own model, a room's specialist, the scheduler model, the
A/B's drawn arm, or EA_MODEL; a `hosted` table entry moves only a pick
that would run on `local` there. Two rules sit over every pick but the
per-message tag: a high-stakes message (the autonomy rails as a routing
rule: money, contracts, deans, declines, personnel) never runs on
`local`, and Fable has a daily turn budget (`tiers.fable_daily_turns`,
0 for none). Both are said in the reply footer, which names the tier
that wrote every reply.

Turns are recorded per tier in the private store (`tier-turns.jsonl`)
for the 17:30 report. Nothing here reaches the network.
"""
from __future__ import annotations

import re
import sys
import urllib.parse
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ea_config as _ea_config  # noqa: E402
import model_routing as _routing  # noqa: E402
import remote_llm as _rl  # noqa: E402

TIER_TURNS = "tier-turns.jsonl"
FABLE_MODEL = "claude-fable-5-1"

# The tiers, the names a message or a pin may use for them, and the names
# the footer says, from the hosted models' registry (ea_config.HOSTED_MODELS):
# each hosted model is its tiers ("opus" and "fable" are Claude's) and its
# key names its default tier, so a new hosted model adds its tier here
# with no edit.
_LOCAL_ALIASES = {"local": "local", "qwen": "local", "remote": "local", "on-prem": "local",
                  "onprem": "local"}
TIERS = ("local",) + tuple(t for m in _routing.hosted_models() for t in m.tiers)
ALIASES = {**{t: t for t in TIERS if t != "local"},
           **{m.key: m.tier for m in _routing.hosted_models()}, **_LOCAL_ALIASES}
_ALIAS_RE = "(" + "|".join(sorted((re.escape(a) for a in ALIASES if a not in ("on-prem", "onprem")),
                                  key=len, reverse=True) + ["on-?prem"]) + ")"

TAG_RE = re.compile(r"^\s*(?:@" + _ALIAS_RE + r"\b[:,]?|ask\s+" + _ALIAS_RE + r"\s*[:,])\s*", re.I)
_USE_RE = re.compile(r"^\s*(?:use|switch\s+to)\s+" + _ALIAS_RE +
                     r"(?:\s+(?:here|for\s+this\s+(?:topic|thread|conversation)))?\s*[.!]?\s*$", re.I)
_ESCALATE_RE = re.compile(r"^\s*escalate(?:\s+(?:this|it))?(?:\s+to\s+fable)?\s*[.!]?\s*$", re.I)
_HAND_BACK_RE = re.compile(r"^\s*hand\s+(?:it\s+|this\s+)?back(?:\s+to\s+(?:opus|claude))?\s*[.!]?\s*$", re.I)
_WHICH_RE = re.compile(r"^\s*(?:which|what)\s+(?:model|tier)(?:\s+is\s+this)?\s*\??\s*$", re.I)
_TOPIC_PREFIX_RE = re.compile(r"^\s*\[\s*" + _ALIAS_RE + r"\s*\]", re.I)

# The autonomy rails as a routing rule: a message that names one of these
# is a judgment call, and never runs on the on-prem model unless the user
# tagged it `@local` themselves.
STAKES_RE = re.compile(
    r"\$\s?\d|\bmoney\b|\bbudget\b|\bcontract\b|\blegal\b|\blawyer|\bdean\b|\bprovost\b|\bchancellor\b|"
    r"\bprogram (?:officer|manager)\b|\bdecline\b|\bturn (?:it |them |this |that )?down\b|\btenure\b|"
    r"\bpromotion\b|\bgrievance\b|\bpersonnel\b|\bHR\b|\bresign|\bcomplaint\b|\bnegotiat",
    re.I)

NAMES = {"local": "the on-prem model", "opus": "Opus", "fable": "Fable",
         **{m.tier: m.name for m in _routing.hosted_models() if not m.base}}


def tier_of(alias: str) -> str:
    return ALIASES.get(str(alias or "").strip().lower().replace("onprem", "on-prem"), "")


# ── the four ways ─────────────────────────────────────────────────────

# The room or DM preamble the bot puts before the user's words
# ("[From the private #ea-x channel, ...]\n\n", "[From your direct message
# ...]\n\n"): the tag and the commands sit after it.
_PREAMBLE_RE = re.compile(r"^\s*\[From [^\]]{0,600}\]\s*")


def _after_preamble(text: str) -> tuple[str, str]:
    """(the bot's preamble, the user's own words)."""
    t = text or ""
    m = _PREAMBLE_RE.match(t)
    return (t[:m.end()], t[m.end():]) if m else ("", t)


def split_tag(text: str) -> tuple[str, str]:
    """(tier, the message without its tag): `@fable can you ...` and
    `ask fable: ...` name a tier for one turn. ('', text) otherwise.
    A room or DM preamble before the tag is kept."""
    head, body = _after_preamble(text)
    m = TAG_RE.match(body)
    if not m:
        return "", text
    tier = tier_of(m.group(1) or m.group(2))
    rest = body[m.end():]
    return (tier, head + rest) if tier and rest.strip() else ("", text)


def parse_command(text: str) -> dict | None:
    """`use fable` / `switch to local` / `escalate` / `hand back` ->
    {"cmd": "use", "tier": t}; `which model` -> {"cmd": "which"}; else
    None. Answered by the bot itself, with no model call."""
    t = _after_preamble(text)[1]
    m = _USE_RE.match(t)
    if m:
        return {"cmd": "use", "tier": tier_of(m.group(1))}
    if _ESCALATE_RE.match(t):
        return {"cmd": "use", "tier": "fable"}
    if _HAND_BACK_RE.match(t):
        return {"cmd": "use", "tier": "opus"}
    if _WHICH_RE.match(t):
        return {"cmd": "which"}
    return None


def topic_name(thread_ts: str) -> str:
    """The Zulip topic behind a thread key (`stream:<id>:topic:<quoted>`),
    or '' for any other key."""
    key = str(thread_ts or "")
    if ":topic:" not in key:
        return ""
    return urllib.parse.unquote(key.split(":topic:", 1)[1])


def topic_tier(thread_ts: str) -> str:
    """The tier a topic's name asks for (`[fable] budget plan`), or ''."""
    m = _TOPIC_PREFIX_RE.match(topic_name(thread_ts))
    return tier_of(m.group(1)) if m else ""


def tier_of_model(model: str, arm: str = "") -> str:
    """Which tier a run's model and arm amount to, for the footer."""
    if _routing.is_local(arm) or str(model or "").endswith("+slim"):
        return "local"
    for name in (arm, model):
        m = _routing.for_model(name)
        if m and not m.base:
            return m.tier
    return "fable" if "fable" in str(model or "").lower() else "opus"


def arm_of(tier: str) -> str:
    """The model_arms arm a tier runs on: `remote` for `local`, else its
    hosted model's arm (`claude` for `opus` and `fable`)."""
    if tier == "local":
        return "remote"
    m = _routing.for_model(tier)
    return m.arm if m else _routing.base().arm


def high_stakes(text: str) -> bool:
    return bool(STAKES_RE.search(text or ""))


# ── config ────────────────────────────────────────────────────────────

def settings(cfg: dict | None = None) -> dict:
    return _ea_config.get_tiers() if cfg is None else _ea_config.tiers_from_block(cfg)


def model_for(tier: str, *, default_model: str, cfg: dict | None = None) -> tuple[str, str]:
    """(model, effort) for an explicit Claude tier; `opus` with no model
    of its own is the bot's default model (EA_MODEL). Effort '' keeps
    whatever the lane had."""
    s = settings(cfg)
    if tier == "fable":
        return s["models"].get("fable") or FABLE_MODEL, s["efforts"].get("fable") or ""
    m = _routing.for_model(tier)
    if m and not m.base:
        # A hosted model on a harness of its own runs the model its harness
        # is set up with (Codex: the resident's config.toml); its key is the
        # label the bot records and the footer names.
        return m.key, ""
    return s["models"].get("opus") or default_model, s["efforts"].get("opus") or ""


def model_alias(name: str, *, default_model: str, cfg: dict | None = None) -> str:
    """`adopt session <id> as fable`: a tier name becomes its model id; any
    other name is a model id already."""
    tier = tier_of(name)
    if _routing.is_base(tier):
        return model_for(tier, default_model=default_model, cfg=cfg)[0]
    return name


# ── the pick ──────────────────────────────────────────────────────────

def _lane_route(lane: str, routing: dict | None) -> str:
    """The routing table's value for the lane's task; `auto` when it
    cannot be read, which leaves the pick as it was before the table."""
    try:
        return _routing.lane_route(lane, routing)
    except Exception:
        return "auto"


def _lane_hosted(lane: str, routing: dict | None) -> str:
    """The hosted model the owner chose for the lane's task (the task's
    override, else the switch), '' when neither is set or the table
    cannot be read."""
    try:
        return _routing.lane_hosted(lane, routing)
    except Exception:
        return ""


def resolve(*, lane: str, user_msg: str = "", tag: str = "", thread_ts: str = "",
            session_meta: dict | None = None, arm: str = "", cfg: dict | None = None,
            fable_turns_today: int | None = None, remote_cfg: dict | None = None,
            routing: dict | None = None, local_chat_ready: bool | None = None) -> dict:
    """The tier this turn runs on and why.

    {"tier", "source", "explicit", "note", "floor"}: `explicit` is True when one of
    the four ways named the tier (the bot then sets the model itself);
    False when the tier only describes where today's knobs put the turn
    (`session` for an adopted or room session's own model, `draw` for an
    A/B arm, `default`; `routing` for the owner's routing table, block
    `routing`, default ea_config.get_routing()). `note` is 'stakes',
    'budget' or 'no-local' when a rule moved the pick to `opus`, else ''.
    'no-local': the install has no remote endpoint (`remote_cfg`, default
    remote_llm.config()), so a local pick runs on Claude. `floor` is
    'local_chat' (and `note` too) when the chat floor moved a local pick
    on a chat or scheduling lane to the hosted model: while
    `local_chat_ready` (default ea_config.local_chat_ready()) is false,
    nothing, an `@local` tag and a `use local` pin included, runs those
    lanes on the owner's own model."""
    meta = session_meta or {}
    s = settings(cfg)
    route = _lane_route(lane, routing)
    hosted = _routing.for_model(_lane_hosted(lane, routing))
    # A hosted pick's tier: the chosen model's own (`opus` for Claude, the
    # bot's own model; `codex` for Codex), the base's with no choice.
    hosted_tier = (hosted or _routing.base()).tier
    # The choice is the hosted model a lane runs on: a lane default of
    # another hosted model gives way to it (a `fable` default under Codex,
    # a `codex` default under Claude), one of its own tiers stays (`fable`
    # under Claude), and `local` stays. Claude, the base, is the bot's own
    # model with no explicit pick, so a room keeps its specialist's model.
    lane_tier = s["lanes"].get(lane) if s["lanes"].get(lane) in TIERS else ""
    lane_source = f"{lane} default"
    if hosted and lane_tier != "local":
        own = _routing.family(lane_tier) == hosted.key
        if hosted.base and not own:
            lane_tier = ""
        elif not hosted.base:
            lane_tier, lane_source = (lane_tier if own else hosted.tier), "routing"
    tier, source = "", ""
    if tag in TIERS:
        tier, source = tag, "tag"
    elif meta.get("tier") in TIERS:
        tier, source = meta["tier"], "pinned"
    elif topic_tier(thread_ts):
        tier, source = topic_tier(thread_ts), "topic name"
    elif route == "local":
        tier, source = "local", "routing"
    elif lane_tier and not (route == "hosted" and lane_tier == "local"):
        tier, source = lane_tier, lane_source
    elif route == "hosted" and lane_tier == "local" and hosted and not hosted.base:
        tier, source = hosted.tier, "routing"
    explicit = bool(tier)
    if not tier:
        if meta.get("model"):
            tier, source = tier_of_model(meta["model"]), "session"
        elif _routing.is_local(arm):
            tier, source = "local", "draw"
        else:
            # `hosted` passed over a local lane default: the bot's own model.
            tier, source = "opus", ("routing" if route == "hosted" and lane_tier == "local"
                                    else "default")
    if route == "hosted" and tier == "local" and source not in ("tag", "pinned", "topic name"):
        tier, source, explicit = hosted_tier, "routing", True
    note = floor = ""
    if tier == "local" and _chat_floored(lane, local_chat_ready):
        tier, explicit = hosted_tier, True
        note = floor = _routing.LOCAL_CHAT_FLOOR
    if tier == "local" and not (remote_cfg if remote_cfg is not None else _rl.config()).get("endpoint"):
        tier, note, explicit = hosted_tier, "no-local", True
    if tier == "local" and source != "tag" and high_stakes(user_msg):
        tier, note, explicit = hosted_tier, "stakes", True
    cap = int(s.get("fable_daily_turns") or 0)
    if tier == "fable" and cap > 0:
        used = _fable_turns_today() if fable_turns_today is None else fable_turns_today
        if used >= cap:
            tier, note, explicit = "opus", "budget", True
    return {"tier": tier, "source": source, "explicit": explicit, "note": note, "floor": floor}


def _chat_floored(lane: str, ready: bool | None = None) -> bool:
    """The chat floor holds the lane's task (model_routing.CHAT_FLOORED):
    a local pick there runs on the hosted model. `ready` stands in for
    ea_config.local_chat_ready()."""
    task = _routing.LANE_TASK.get(lane, "")
    if task not in _routing.CHAT_FLOORED:
        return False
    return not ready if ready is not None else _routing.chat_floor_holds(task)


def chat_floor_note(lane: str) -> str:
    """The note a refused `@local` tag or `use local` pin answers with on
    a lane the chat floor holds, in plain words; '' elsewhere."""
    return _routing.LOCAL_CHAT_NOTE[0] if _chat_floored(lane) else ""


def label(tier: str, *, backstop: dict | None = None, note: str = "") -> str:
    """The footer's name for the tier that wrote a reply."""
    if tier == "local":
        text = "local, checked by Claude" if backstop else "local"
    else:
        text = tier or "opus"
    if note == "stakes":
        text += " (stakes rule)"
    elif note == "budget":
        text += " (Fable budget spent)"
    elif note == "no-local":
        text += " (no local model set up)"
    elif note == _routing.LOCAL_CHAT_FLOOR:
        text += " (your own model: not yet)"
    return text


def describe(tier: str, source: str) -> str:
    """One line for `which model`."""
    why = {"tag": "for this message", "pinned": "pinned by you", "topic name": "from the topic name",
           "session": "the session's own model", "draw": "drawn by the model A/B",
           "routing": "the routing table in Settings", "default": "the default"}.get(source, source)
    return f"This topic runs on {NAMES.get(tier, tier)} ({why})."


# ── the record ────────────────────────────────────────────────────────

def note_turn(*, tier: str, source: str, lane: str, model: str, channel: str = "",
              thread_ts: str = "", note: str = "", elapsed_s: float = 0.0) -> None:
    """One line per chat turn, owner-only; never raises."""
    try:
        if not _rl._owner():
            return
        _rl.append(TIER_TURNS, {"ts": datetime.now().astimezone().isoformat(timespec="seconds"),
                                "tier": tier, "source": source, "note": note, "lane": lane,
                                "model": model, "channel": channel, "thread_ts": thread_ts,
                                "elapsed_s": round(float(elapsed_s or 0), 1)})
    except Exception:
        pass


def _rows(since: date | None) -> list[dict]:
    try:
        rows = _rl.read(TIER_TURNS)
    except Exception:
        return []
    if since is None:
        return rows
    return [r for r in rows if str(r.get("ts") or "")[:10] >= since.isoformat()]


def fable_turns_today(today: date | None = None) -> int:
    d = (today or date.today()).isoformat()
    return sum(1 for r in _rows(None) if r.get("tier") == "fable" and str(r.get("ts") or "")[:10] == d)


_fable_turns_today = fable_turns_today


def summary_lines(since: date | None = None, cfg: dict | None = None) -> list[str]:
    """The 17:30 report's lines: turns per tier (today unless `since`),
    how each was picked, the rules that moved a pick, and the budget."""
    rows = _rows(since or date.today())
    if not rows:
        return []
    by_tier: dict[str, int] = {}
    by_source: dict[str, int] = {}
    notes: dict[str, int] = {}
    for r in rows:
        by_tier[r.get("tier") or "?"] = by_tier.get(r.get("tier") or "?", 0) + 1
        by_source[r.get("source") or "?"] = by_source.get(r.get("source") or "?", 0) + 1
        if r.get("note"):
            notes[r["note"]] = notes.get(r["note"], 0) + 1
    tiers = ", ".join(f"{t} {by_tier[t]}" for t in TIERS if t in by_tier)
    picked = ", ".join(f"{k} {v}" for k, v in sorted(by_source.items(), key=lambda kv: -kv[1]))
    line = f"Tiers ({since.isoformat() + '..' if since else 'today'}): {tiers}; picked by {picked}"
    if notes:
        line += "; moved to opus by " + ", ".join(f"{k} {v}" for k, v in sorted(notes.items()))
    cap = int(settings(cfg).get("fable_daily_turns") or 0)
    if cap:
        line += f"; Fable budget {by_tier.get('fable', 0)}/{cap}"
    return [line]


def main(argv=None) -> int:
    import argparse
    import json
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("report", help="turns per tier")
    r.add_argument("--since", help="YYYY-MM-DD (default: today)")
    r.add_argument("--json", action="store_true")
    sub.add_parser("settings", help="the tiers block with every key defaulted")
    a = ap.parse_args(argv)
    if a.cmd == "settings":
        print(json.dumps(settings(), indent=2))
        return 0
    since = date.fromisoformat(a.since) if a.since else None
    lines = summary_lines(since)
    if a.json:
        print(json.dumps({"lines": lines, "rows": _rows(since or date.today())}, indent=2))
    else:
        print("\n".join(lines) or "no turns recorded")
    return 0


if __name__ == "__main__":
    sys.exit(main())

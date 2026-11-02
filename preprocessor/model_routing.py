#!/usr/bin/env python3
"""Model routing: which model does each task, from one owner-private table.

docs/llm-lanes.md section 1.1. The table names a task the way the owner
thinks of it and gives it one of three values:

  local   the owner's own OpenAI-compatible endpoint (remote-llm.json,
          remote_llm.py); with no endpoint set up the task reads hosted,
          as `@local` does in chat;
  hosted  Claude;
  auto    the remote layer's switches and the tiers block decide, as they
          did before the table. Every task but `review` and `thread_state`
          defaults to auto.

`thread_state` (the web chat's conversation states, docs/webchat-state.md)
has no switch of its own and defaults to local: `local` runs it on the
local model when one is set up, and `auto` too, with Claude taking the
threads left when the local model does not answer; with none set up, or
with `hosted`, Claude judges them, one call a tick (web_thread_state.py).

Each lane asks at its own decision point: `local()` for the switches a
lane reads from remote-llm.json, `mail_mode()` and `scheduling_mode()`
inside remote_llm.mode(), and `lane_route()` inside model_tiers.resolve.
The table never turns on a lane the endpoint cannot serve, and never
passes a ghost-week gate: `local` on scheduling acts through the tier
lane default (the tool gateway, every write checked by the Claude
backstop), not through `scheduling enforce`. Two floors sit under every
value:

  stakes  `review` (the briefing's still-open check, the nightly ledger
          reconcile, the backstop on local scheduling writes, the
          hand-offs from the briefings and the audit) is hosted only. In
          chat and scheduling turns the per-message stakes rule
          (model_tiers.STAKES_RE) sits over a `local` value as it sits
          over a lane default, and the briefing's rails stay in
          remote_briefing.
  keep    `mail`, `tagging` and `digests` read new mail in an owner
          process, so `hosted` there would hand kept mail to Claude.
          While the keep list (`keep_from_anthropic` in remote-llm.json)
          keeps anything (a domain, an address, or the admissions rule,
          on unless set false), `hosted` reads `auto` and saving it is
          refused.

The table is <private_dir>/routing.json (0600), read by
ea_config.get_routing in owner processes and written by `save`: the web
chat's Settings screen (WebSettings) and `set` below. Each change is one
line in the remote-llm store's routing-changes.jsonl: when, through what
(`via`), what changed, and for a web save the credential that signed the
request in (`cookie` or `header`) and, on a relayed request, the device.

Two voices. What the Settings screen, the `routing` chat command and
`show` print is in plain words for the owner: a label and a line per
task, why it runs where it runs, the floors and every refusal, with no
term of the code (tests/plain_words.py holds the words they never use).
`show --verbose` and the doctor keep the technical voice: the switches,
the lanes and the local model's last error. The task keys are the same
in both, in the config and on the command line.

CLI (as the owner):
    model_routing.py show [--json] [--verbose]  each task: the value, the model it runs on now, why
    model_routing.py set <task> <value>         write one task (validated, 0600)
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ea_config as _ea_config  # noqa: E402

VALUES = _ea_config.ROUTING_VALUES
FLOORS = _ea_config.ROUTING_FLOORS
KEEP_FLOORED = ("mail", "tagging", "digests")
CHANGES = "routing-changes.jsonl"
# (label, one line on what the task does) in plain words, in the order
# the screen shows. The screen says "your own model" for local and
# "Claude" for hosted, as its key does.
TASKS = {
    "mail": ("Reading your mail",
             "Reads each new message and notes what it asks for, by when and from whom."),
    "tagging": ("Sorting mail", "Puts Outlook categories on new mail."),
    "drafting": ("Writing replies", "Writes email replies in your voice. Letters always come from Claude."),
    "digests": ("Mail digests", "Sums up mailing-list and admissions mail in short digests."),
    "briefing": ("Your briefing",
                 "Writes your morning briefing and the afternoon update. Claude takes the hard items."),
    "audit": ("The weekly check",
              "Goes over everything still open each Sunday. Claude takes the items it flags."),
    "scheduling": ("Finding meeting times",
                   "Answers meeting requests and finds times. "
                   "Claude checks every draft and invite your own model makes."),
    "chat": ("Talking with you",
             "Answers you in the chat. A message about money, a contract, a dean or a decline "
             "always goes to Claude."),
    "thread_state": ("Noticing what needs you",
                     "Decides which conversations need you, which wait on someone else "
                     "and which are done."),
    "review": ("Checking its own work",
               "Checks that briefing items are still open, goes over your open items each night "
               "and checks the drafts and invites your own model makes for meetings."),
}
# What each task does in the code's terms (`show --verbose`; the docs).
TECH = {
    "mail": "Reads each new message into a record: its class, ask, deadline and people.",
    "tagging": "Puts Outlook categories on new mail.",
    "drafting": "Writes email bodies in your voice from the brief. Letters stay on the hosted model.",
    "digests": "Reads mailing-list and admissions mail into the digests.",
    "briefing": "Writes the morning briefing and the afternoon update. Hard items go to the hosted model.",
    "audit": "Writes the weekly audit. Flagged items go to the hosted model.",
    "scheduling": "Answers the scheduling channel and the proactive scheduler. "
                  "The hosted model checks each local write.",
    "chat": "Answers you in topics, rooms and the DM. A message about money, a contract, "
            "a dean or a decline goes to the hosted model.",
    "thread_state": "Reads a web chat thread the rules cannot settle and says if it needs you, "
                    "waits or is done. Without a local model the hosted model reads it.",
    "review": "Checks if briefing items are still open, reconciles the ledger each night "
              "and reviews local scheduling writes.",
}
assert tuple(TASKS) == tuple(TECH) == _ea_config.ROUTING_TASKS
# The floors' lines under a row: (plain, technical).
STAKES_FLOOR = ("Always Hosted: these are judgment calls, so only Claude makes them.",
                "Stakes floor: these are judgment calls, so only the hosted model does them.")
KEEP_FLOOR = ("Hosted is off while you keep some mail away from Claude (a domain, an address "
              "or admissions mail), so that mail never reaches Claude.",
              "Keep floor: hosted is not available while the keep list keeps anything "
              "(a domain, an address or the admissions rule), because mail it keeps "
              "must not reach the hosted model.")
# Where each chat lane's turns happen, as the web chat names them.
PLACES = {"main": "Ask Tacitus", "dm": "Direct messages", "room": "project rooms"}
# The chat lanes (ea-chat-bot.py _turn_lane) and the task each belongs to.
LANE_TASK = {"main": "chat", "dm": "chat", "room": "chat",
             "sched-channel": "scheduling", "proactive": "scheduling"}


class RoutingRefused(ValueError):
    """A table that a floor, a value or the process refuses. The message
    says which in plain words (the Settings screen shows it); `tech` says
    it in the code's terms (the CLI prints it)."""

    def __init__(self, plain: str, tech: str = ""):
        super().__init__(plain)
        self.tech = tech or plain


def _remote_cfg(cfg: dict | None) -> dict:
    if cfg is not None:
        return cfg
    import remote_llm
    return remote_llm.config()


# ── the table ─────────────────────────────────────────────────────────

def table(routing: dict | None = None) -> dict[str, str]:
    """{task: value} with the floors applied. `routing` is a routing
    block ({"tasks": {...}}); default ea_config.get_routing()."""
    block = routing if routing is not None else _ea_config.get_routing()
    return dict(_ea_config.routing_from_block(block)["tasks"])


def value(task: str, routing: dict | None = None) -> str:
    return table(routing).get(task, "auto")


def local_ready(cfg: dict) -> bool:
    """The remote config names an endpoint and a model (remote_llm.config
    returns neither outside an owner process)."""
    return bool(cfg.get("endpoint") and cfg.get("model"))


def keep_listed(cfg: dict) -> bool:
    """The keep list keeps something: it names a domain or an address, or
    its admissions rule is on (remote_llm.config() reads a missing value
    as on)."""
    ks = cfg.get("keep_from_anthropic") or {}
    return bool(ks.get("domains") or ks.get("addresses") or ks.get("admissions"))


def choice(task: str, *, cfg: dict | None = None, routing: dict | None = None) -> str:
    """What the table asks of the task once the endpoint and the keep
    floor apply: `local`, `hosted` or `auto` (the switches decide)."""
    v = value(task, routing)
    if v == "local":
        return "local" if local_ready(_remote_cfg(cfg)) else "hosted"
    if v == "hosted" and task in KEEP_FLOORED and keep_listed(_remote_cfg(cfg)):
        return "auto"
    return v


def local(task: str, *, switch_local: bool, cfg: dict | None = None,
          routing: dict | None = None) -> bool:
    """True when the local model does `task` now. `switch_local` is what
    the lane's own switch says, which `auto` keeps."""
    c = choice(task, cfg=cfg, routing=routing)
    return bool(switch_local) if c == "auto" else c == "local"


def mail_mode(raw: str, cfg: dict | None = None, routing: dict | None = None) -> str:
    """The mail part's mode after the table: `local` lifts off and shadow
    to enforce (sealed and ghost stay); `hosted` turns the gate off."""
    c = choice("mail", cfg=cfg, routing=routing)
    if c == "local" and raw in ("off", "shadow"):
        return "enforce"
    if c == "hosted":
        return "off"
    return raw


def scheduling_mode(raw: str, cfg: dict | None = None, routing: dict | None = None) -> str:
    """The scheduling agent's mode after the table: `hosted` stops it;
    `local` and `auto` keep the switch (enforce stays behind its gate)."""
    return "off" if choice("scheduling", cfg=cfg, routing=routing) == "hosted" else raw


def lane_route(lane: str, routing: dict | None = None) -> str:
    """The table's value for a chat lane's task (LANE_TASK), `auto` for any
    other lane. model_tiers.resolve applies the endpoint and stakes rules."""
    task = LANE_TASK.get(lane)
    return value(task, routing) if task else "auto"


# ── the local model ───────────────────────────────────────────────────

def _signal(rec: dict) -> bool | None:
    """What one call record says about the endpoint: True it answered,
    False it failed, None neither (a full queue, an empty completion, a
    request the server refused as bad)."""
    if rec.get("ok"):
        return True
    err = str(rec.get("error") or "")
    if err.startswith(("no free request slot", "empty completion", "completion is not")) or \
            (err.startswith("HTTP 4") and not err.startswith(("HTTP 408", "HTTP 429"))):
        return None
    return False


def _last_call(path: Path, tail_bytes: int = 65536) -> dict | None:
    """The newest call record in calls.jsonl that says something about the
    endpoint, from the file's last `tail_bytes`."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - tail_bytes))
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and not str(rec.get("purpose") or "").startswith("mail-tune") \
                and _signal(rec) is not None:
            return rec
    return None


def _ago(ts: str) -> str:
    try:
        secs = time.time() - datetime.fromisoformat(ts).timestamp()
    except (TypeError, ValueError):
        return "some time"
    if secs < 90:
        return "1 min"
    if secs < 5400:
        return f"{secs / 60:.0f} min"
    if secs < 172800:
        return f"{secs / 3600:.0f} h"
    return f"{secs / 86400:.0f} days"


def local_status(cfg: dict | None = None, *, verbose: bool = False) -> dict:
    """{"configured", "model", "reachable", "detail"}: the endpoint's state
    from what the lanes already record (the request breaker and the last
    call in calls.jsonl), with no call of its own. `reachable` is None
    when nothing says either way. `detail` is plain words, or with
    `verbose` the technical cause (the file, the secret, the last error).
    `remote_llm.py ping` makes a live call."""
    cfg = _remote_cfg(cfg)

    def say(plain: str, tech: str) -> str:
        return tech if verbose else plain
    if not local_ready(cfg):
        return {"configured": False, "model": "", "reachable": None,
                "detail": say("Your own model is not set up.",
                              "No local model is set up (remote-llm.json names no endpoint).")}
    import remote_llm
    out = {"configured": True, "model": cfg["model"], "reachable": None, "detail": ""}
    if not remote_llm.api_key():
        out.update(reachable=False, detail=say(
            "Its access key is missing.", f"{remote_llm.SECRET_NAME} is not in the private secrets.env."))
        return out
    breaker = remote_llm._breaker("")
    if breaker is not None and breaker.is_open():
        out.update(reachable=False, detail=say(
            "It failed several times in a row; Tacitus tries it again in 15 minutes.",
            "It failed several times in a row; calls pause for 15 minutes."))
        return out
    rec = _last_call(remote_llm.store_dir() / "calls.jsonl")
    if rec is None:
        out["detail"] = say("Tacitus has not used it yet.", "No call yet.")
    elif rec.get("ok"):
        out.update(reachable=True, detail=f"Answered {_ago(rec.get('ts'))} ago.")
    else:
        ago = _ago(rec.get("ts"))
        out.update(reachable=False, detail=say(
            f"It did not answer when last asked, {ago} ago.",
            f"The last call failed {ago} ago: {str(rec.get('error') or '')[:120]}"))
    return out


# ── what each task runs on now ────────────────────────────────────────

# What `auto` did, in plain words, by where it put the task.
AUTO_SAYS = {"local": "Auto picked your own model", "hosted": "Auto picked Claude",
             "mixed": "Auto is trying both models side by side"}
JUDGMENT = "only Claude makes these judgment calls"
NOT_SET_UP = "your own model is not set up, so Claude does it"


def _and(items: list[str]) -> str:
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def _switch_state(task: str, cfg: dict, tiers: dict, done: dict) -> tuple[str, str, str]:
    """(local | hosted | mixed, plain why, technical why): where the remote
    layer's switches and the tiers block put the task, which `auto` keeps.
    `done` holds the rows made so far ({task: effective}); tagging follows
    the mail row."""
    import remote_llm

    def onoff(flag: bool) -> str:
        return "local" if flag else "hosted"
    if task == "mail":
        m = remote_llm.switch_mode("mail", cfg)
        eff = onoff(m in ("enforce", "ghost", "sealed"))
        return eff, AUTO_SAYS[eff], f"the mail switch is {m}"
    if task == "tagging":
        on = done.get("mail") == "local"
        return (onoff(on), f'Auto does what "{TASKS["mail"][0]}" does',
                "it follows the mail gate, which is " + ("on" if on else "off"))
    if task in ("drafting", "digests", "briefing", "audit"):
        key = {"briefing": "briefings", "audit": "audits"}.get(task, task)
        val = cfg.get(key) or "off"
        eff = "mixed" if val == "ab" else onoff(val == "remote")
        return eff, AUTO_SAYS[eff], f"the {key} switch is {val}"
    if task == "thread_state":
        if local_ready(cfg):
            return ("local", "Auto uses your own model, and Claude takes what it leaves",
                    "it runs on the local model; the hosted model takes the threads left "
                    "when it does not answer")
        return "hosted", NOT_SET_UP, "no local model is set up; the hosted model judges, one call a tick"
    lanes = tiers.get("lanes") or {}
    if task == "scheduling":
        m = remote_llm.switch_mode("scheduling", cfg)
        lane = lanes.get("sched-channel") == "local"
        eff = onoff(m == "enforce" or lane)
        return eff, AUTO_SAYS[eff], (f"the scheduling switch is {m}"
                                     + ("; the channel's tier is local" if lane else ""))
    if task == "chat":
        on = [k for k in ("main", "dm", "room") if lanes.get(k) == "local"]
        if not on:
            return ("hosted", "Auto picked Claude; start a message with @local to have your own "
                              "model answer it",
                    "no chat lane's tier is local; @local or `use local` picks it per topic")
        if len(on) == 3:
            return "local", AUTO_SAYS["local"], f"the tier is local in {', '.join(on)}"
        return ("mixed", f"Auto picked your own model for {_and([PLACES[k] for k in on])}, "
                         "and Claude for the rest", f"the tier is local in {', '.join(on)}")
    return "hosted", JUDGMENT, "the stakes floor"


def rows(*, cfg: dict | None = None, routing: dict | None = None,
         tiers: dict | None = None, verbose: bool = False) -> list[dict]:
    """One row per task for the screen, the doctor and the chat command:
    {"task", "label", "what", "value", "allowed", "locked", "floor",
    "effective" (local | hosted | mixed), "why"}. `what`, `floor` and
    `why` are plain words, or with `verbose` the code's terms."""
    cfg = _remote_cfg(cfg)
    tiers = tiers if tiers is not None else _ea_config.get_tiers()
    tab = table(routing)
    kept = keep_listed(cfg)
    pick = 1 if verbose else 0
    out, done = [], {}
    for task, (label, what) in TASKS.items():
        v = tab[task]
        row = {"task": task, "label": label, "what": TECH[task] if verbose else what, "value": v,
               "allowed": list(VALUES), "locked": False, "floor": ""}
        if task in FLOORS:
            row.update(allowed=[FLOORS[task]], locked=True, floor=STAKES_FLOOR[pick])
        elif task in KEEP_FLOORED and kept:
            row.update(allowed=["local", "auto"], floor=KEEP_FLOOR[pick])
        c = choice(task, cfg=cfg, routing={"tasks": tab})
        judged = "the hosted model judges, one call a tick" if task == "thread_state" else ""
        if task in FLOORS:
            eff, why = FLOORS[task], (JUDGMENT, "the stakes floor")
        elif c == "local":
            eff, why = "local", ("set to Local", "set to local")
        elif c == "hosted" and v == "local":
            eff, why = "hosted", (f"set to Local, but {NOT_SET_UP}",
                                  "set to local, but no local model is set up"
                                  + (f"; {judged}" if judged else ""))
        elif c == "hosted":
            eff, why = "hosted", ("set to Hosted", "set to hosted" + (f": {judged}" if judged else ""))
        else:
            eff, plain, tech = _switch_state(task, cfg, tiers, done)
            why = (plain, tech)
            if v == "hosted":
                why = (f"Hosted waits while you keep some mail away from Claude; {plain}",
                       f"hosted waits on the keep floor; {tech}")
        row.update(effective=eff, why=why[pick])
        done[task] = eff
        out.append(row)
    return out


def payload(*, cfg: dict | None = None, routing: dict | None = None,
            tiers: dict | None = None, verbose: bool = False) -> dict:
    """The Settings screen's data (GET /api/settings/routing), in plain
    words; `verbose` gives the code's terms (`show --verbose`, the doctor)."""
    cfg = _remote_cfg(cfg)
    source = "given" if routing is not None else _ea_config.get_routing_source()[1]
    return {"local": local_status(cfg, verbose=verbose), "source": source, "values": list(VALUES),
            "rows": rows(cfg=cfg, routing=routing, tiers=tiers, verbose=verbose)}


def lines(data: dict | None = None) -> list[str]:
    """The table as chat lines (the `routing` command, `show`)."""
    data = data if data is not None else payload()
    loc = data["local"]
    head = (f"Local model {loc['model']}: {loc['detail']}" if loc["configured"] else loc["detail"])
    out = [f":control_knobs: *Model routing.* {head}"]
    for r in data["rows"]:
        out.append(f"- *{r['label']}* (`{r['task']}`): {r['value']}, now {r['effective']} ({r['why']})")
    if str(data.get("source", "")).startswith("refused"):
        out.append(f"routing.json is {data['source']}; every task reads auto.")
    out.append("Change it in the web chat's Settings, or as the owner: "
               "`python3 preprocessor/model_routing.py set <task> local|hosted|auto`.")
    return out


# ── saving ────────────────────────────────────────────────────────────

def _refusals(tasks: dict, kept: bool) -> list[tuple[str, str]]:
    """(plain, technical) for each task `save` refuses: an unknown task, a
    value that is not local, hosted or auto, a value under the stakes
    floor, and `hosted` under the keep floor while the keep list keeps
    anything. The technical text is ea_config.routing_problems' own."""
    out = []
    for task, v in tasks.items():
        tech = _ea_config.routing_problems({"tasks": {task: v}})
        name = f'"{TASKS[task][0]}"' if task in TASKS else ""
        if task not in TASKS:
            plain = f"Tacitus has no task named {task!r}."
        elif v not in VALUES:
            plain = f"{name} takes Local, Hosted or Auto."
        elif tech:
            plain = f"{name} always stays on Hosted: {JUDGMENT}."
        elif v == "hosted" and task in KEEP_FLOORED and kept:
            plain = f"{name} cannot be Hosted while you keep some mail away from Claude."
            tech = [f"{task} cannot be hosted while the keep list has entries (the keep floor)"]
        else:
            continue
        out.append((plain, "; ".join(tech)))
    return out


def save(tasks, *, cfg: dict | None = None, via: str = "cli", who: dict | None = None) -> dict:
    """Merge `tasks` ({task: value}) over the table in force and write
    <private_dir>/routing.json (0600). Owner processes only. Raises
    RoutingRefused for an unknown task or value, a floored value, or
    `hosted` on a keep-floored task while the keep list keeps anything.
    `who` ({"credential", "device"}, from the web front end) goes into
    the change log line."""
    if not isinstance(tasks, dict) or not tasks:
        raise RoutingRefused("Choose Local, Hosted or Auto for at least one task.",
                             "tasks must be a JSON object that names at least one task")
    problems = _refusals(tasks, keep_listed(_remote_cfg(cfg)))
    if problems:
        raise RoutingRefused(" ".join(p for p, _t in problems), "; ".join(t for _p, t in problems))
    ot = _ea_config._owner_trust()
    if not ot.is_owner_process():
        raise RoutingRefused("Tacitus could not save: only the part of Tacitus that runs as you "
                             "may change these settings.",
                             "only the owner's processes write the routing table")
    current, _source = _ea_config.get_routing_source()
    block = _ea_config.routing_from_block({"tasks": {**current["tasks"], **tasks}})
    try:
        ot.write_owner_file(ot.private_dir() / ot.ROUTING_FILE,
                            (json.dumps(block, indent=2) + "\n").encode(), mode=0o600)
    except (OSError, ot.OwnerTrustError) as e:
        raise RoutingRefused("Tacitus could not write the settings file.",
                             f"routing.json was not written: {e}") from None
    try:
        import remote_llm
        changed = {k: [current["tasks"][k], v] for k, v in block["tasks"].items()
                   if current["tasks"][k] != v}
        line = {"ts": datetime.now().astimezone().isoformat(timespec="seconds"),
                "via": via, "changed": changed}
        for key in ("credential", "device"):
            if (who or {}).get(key):
                line[key] = str(who[key])[:80]
        remote_llm.append(CHANGES, line)
    except Exception:
        pass
    return block


class WebSettings:
    """The web chat's Settings screen: botcore's WebFrontend serves it at
    /api/settings/routing (ea-chat-bot.py registers it)."""

    def get(self) -> dict:
        return payload()

    def save(self, body: dict, who: dict | None = None) -> dict:
        save(body.get("tasks"), via="web", who=who)
        return {**payload(), "saved": True}


# ── the doctor ────────────────────────────────────────────────────────

def doctor_row(*, cfg: dict | None = None) -> tuple[str, str, str]:
    """(PASS | WARN | FAIL, detail, fix) for ea-doctor's `model routing`
    row: each task's model now, and the local model's state."""
    cfg = _remote_cfg(cfg)
    source = _ea_config.get_routing_source()[1]
    data = payload(cfg=cfg, verbose=True)
    by: dict[str, list[str]] = {}
    for r in data["rows"]:
        by.setdefault(r["effective"], []).append(r["task"])
    detail = "; ".join(f"{k}: {', '.join(by[k])}" for k in ("local", "mixed", "hosted") if k in by)
    loc = data["local"]
    detail += (f"; local model {loc['model']}: {loc['detail']}" if loc["configured"]
               else "; no local model")
    detail += f" ({source})"
    if source.startswith("refused"):
        return "FAIL", detail, ("routing.json must be the owner's, mode 0600, one JSON object; "
                                "remove it to read every task as auto")
    # thread_state is local by default and runs on the hosted model without
    # a local model, so it is no reason to warn.
    unready = [r["task"] for r in data["rows"] if r["value"] == "local"
               and r["effective"] != "local" and r["task"] != "thread_state"]
    if unready:
        return "WARN", detail, (f"{', '.join(unready)} set to local with no local model; "
                                "`remote_llm.py init` sets one up")
    if by.get("local") and loc["reachable"] is False:
        return "WARN", detail, "the local model does not answer; `remote_llm.py ping` checks it now"
    return "PASS", detail, ""


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("show", help="each task: the value, the model it runs on now, why")
    s.add_argument("--json", action="store_true")
    s.add_argument("--verbose", action="store_true",
                   help="the code's terms: the switches, the lanes, the local model's last error")
    p = sub.add_parser("set", help="write one task into the owner-private routing.json")
    p.add_argument("task", choices=list(TASKS))
    p.add_argument("value", choices=list(VALUES))
    a = ap.parse_args(argv)
    if a.cmd == "set":
        try:
            block = save({a.task: a.value})
        except RoutingRefused as e:
            print(f"set refused: {e.tech}", file=sys.stderr)
            return 2
        print(json.dumps({"task": a.task, "value": block["tasks"][a.task]}))
        return 0
    data = payload(verbose=a.verbose)
    print(json.dumps(data, indent=2) if a.json else "\n".join(lines(data)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

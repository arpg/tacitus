#!/usr/bin/env python3
"""Model routing: which model does each task, from one owner-private table.

docs/llm-lanes.md section 1.1. The table names a task the way the owner
thinks of it and gives it one of three values:

  local   the owner's own OpenAI-compatible endpoint (remote-llm.json,
          remote_llm.py); with no endpoint set up the task reads hosted,
          as `@local` does in chat;
  hosted  a hosted model: Claude, or Codex where the task has a Codex
          route (the `hosted` map below);
  auto    the remote layer's switches and the tiers block decide, as they
          did before the table. Every task but `review` and `thread_state`
          defaults to auto.

Which hosted model a task uses, whenever it runs hosted (by its value or
by auto), is one switch with overrides: `hosted_default` (`claude` or
`codex`) for every task, and the block's `hosted` map for a task the
owner set apart (`hosted_map`). A model is a choice only for the tasks
its entry in ea_config.HOSTED_MODELS lists. Codex takes `chat` and
`scheduling`, whose lanes then run on the `codex` tier (model_tiers.resolve
puts the choice over the tiers block); `briefing`, `audit` and `digests`,
whose Claude lane run-agent.sh then hands to Codex (`harness`); and
`tagging`, `thread_state` and `review`, whose tool-less calls hosted_text
makes on Codex with every tool off. `mail` hosted is the gate off and
`drafting` hosted is the turn's own model, so neither has a choice of
its own: they read Claude whatever the switch says, and `show` says why.
With no switch set and no override, a task runs where it ran before the
switch: the chat and scheduling lanes' tiers block, Claude for the rest.

The policy code never names a hosted model: it asks `is_hosted`,
`is_local`, `is_base` or `for_model` here, so the next hosted model is
one entry in ea_config.HOSTED_MODELS and its harness.

`thread_state` (the web chat's conversation states, docs/webchat-state.md)
has no switch of its own and defaults to local: `local` runs it on the
local model when one is set up, and `auto` too, with the hosted model
taking the threads left when the local model does not answer; with none
set up, or with `hosted`, the hosted model judges them, one call a tick
(web_thread_state.py).

Each lane asks at its own decision point: `local()` for the switches a
lane reads from remote-llm.json, `mail_mode()` and `scheduling_mode()`
inside remote_llm.mode(), and `lane_route()` inside model_tiers.resolve.
The table never turns on a lane the endpoint cannot serve, and never
passes a ghost-week gate: `local` on scheduling acts through the tier
lane default (the tool gateway, every write checked by the hosted
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
          process, so `hosted` there would hand kept mail to a hosted
          model (Claude or Codex).
          While the keep list (`keep_from_frontier` in remote-llm.json,
          read under its old name `keep_from_anthropic` too) keeps
          anything (a domain, an address, or the admissions rule, on
          unless set false), `hosted` reads `auto` and saving it is
          refused. Every privacy decision keys on the model's class
          (`is_hosted`), never on its name.

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
    model_routing.py show [--json] [--verbose]  the hosted model, then each task: the value,
                                                its hosted model (default or override), the
                                                model it runs on now, why
    model_routing.py set-hosted claude|codex    the switch: every task without an override
    model_routing.py set-hosted <m> --task <t>  an override for one task
    model_routing.py set-hosted --clear-task <t> [--clear-task <t2>]
                                                the task follows the switch again
    model_routing.py set <task> <value>         write one task (validated, 0600): local,
                                                hosted or auto, or claude or codex for its
                                                hosted model (an override)
    model_routing.py preset list                the built-in presets and the saved ones
    model_routing.py preset apply <name>        write the preset's tasks, switch and overrides
    model_routing.py preset save <name> [--note <text>]
                                                keep the table in force under a name
    model_routing.py harness --agent <agent>    claude or codex: where run-agent.sh runs
                                                the agent's Claude lane

The chat floor. While ea-config's `local_chat_ready` is false, `chat` and
`scheduling` never run on the owner's own model: `set chat local` and a
preset that names it are refused with LOCAL_CHAT_NOTE, the screen and
`show` say "not yet" on those rows, and model_tiers.resolve moves any
local pick on their lanes (the table, a lane default, an A/B draw, an
`@local` tag, a `use local` pin) to the hosted model with `floor:
"local_chat"`. The scheduling agent's `enforce` reads `shadow` under it.

Presets, the owner's stash of settings: the built-ins (BUILTIN_PRESETS,
defined here and never written) and the ones `preset save` keeps under
`presets` in routing.json. `apply` writes a preset's task values and its
switch (a preset without one leaves the switch as it is) and keeps the
per-task overrides only where the preset names them.
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ea_config as _ea_config  # noqa: E402

VALUES = _ea_config.ROUTING_VALUES
FLOORS = _ea_config.ROUTING_FLOORS
HOSTED = _ea_config.ROUTING_HOSTED
CODEX_TASKS = _ea_config.ROUTING_CODEX_TASKS
REGISTRY = _ea_config.HOSTED_MODELS
BASE = _ea_config.HOSTED_BASE
KEEP_FLOORED = ("mail", "tagging", "digests")
CHANGES = "routing-changes.jsonl"
MODEL_NAMES = {k: m["name"] for k, m in REGISTRY.items()}
# In a save's `hosted` map, this value drops the task's override: the task
# follows the switch again.
FOLLOW = "default"
# The cron agents whose Claude lane is a task's hosted part: run-agent.sh
# asks `harness` before it starts one.
AGENT_TASK = {"morning-briefing": "briefing", "afternoon-catchup": "briefing",
              "weekly-audit": "audit", "list-digest": "digests", "admissions-digest": "digests"}
# (label, one line on what the task does) in plain words, in the order
# the screen shows. The screen says "your own model" for local and names
# the hosted model (Claude or Codex) for hosted.
TASKS = {
    "mail": ("Reading your mail",
             "Reads each new message and notes what it asks for, by when and from whom."),
    "tagging": ("Sorting mail", "Puts Outlook categories on new mail."),
    "drafting": ("Writing replies", "Writes email replies in your voice. Letters always come from a hosted model."),
    "digests": ("Mail digests", "Sums up mailing-list and admissions mail in short digests."),
    "briefing": ("Your briefing",
                 "Writes your morning briefing and the afternoon update. Hard items go to a hosted model."),
    "audit": ("The weekly check",
              "Goes over everything still open each Sunday. Flagged items go to a hosted model."),
    "scheduling": ("Finding meeting times",
                   "Answers meeting requests and finds times. "
                   "A hosted model checks every draft and invite your own model makes."),
    "chat": ("Talking with you",
             "Answers you in the chat. A message about money, a contract, a dean or a decline "
             "never goes to your own model."),
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
STAKES_FLOOR = ("Always Claude or Codex: these are judgment calls.",
                "Stakes floor: these are judgment calls, so only a hosted model does them.")
KEEP_FLOOR = ("Claude and Codex are off here while you keep some mail away from hosted models "
              "(a domain, an address or admissions mail), so that mail never reaches them.",
              "Keep floor: hosted is not available while the keep list keeps anything "
              "(a domain, an address or the admissions rule), because mail it keeps "
              "must not reach a hosted model.")
# Under the row, what Hosted means for a task with no Claude-or-Codex
# choice: (plain, technical).
HOSTED_NOTES = {
    "mail": ("On Claude or Codex: the model that does each task reads the whole message.",
             "Hosted turns the mail gate off: the lanes, on Claude or Codex, read the mail itself."),
    "drafting": ("On Claude or Codex: the model that asks for the reply writes it.",
                 "Hosted turns the writer off: the turn's own model (Claude or Codex) writes the body."),
}
# Behind the "?" beside a task's name on the Settings screen: what the
# task is, in plain words, for the names that do not say it themselves.
HELP = {
    "mail": ("When a new email comes in, Tacitus reads it once and writes a short note: what the "
             "sender wants from you, by when, and who is involved. Your briefing, your to-do list "
             "and your replies work from these notes. On your own model, the full text of each email "
             "stays with your model. On Claude or Codex, the full email goes to that model."),
    "tagging": ("Puts colored labels (Outlook categories) on each new email: the project it belongs "
                "to, and what it wants from you, such as needs a reply, to do or just for your "
                "information. In Outlook you can search and filter by these labels. Sorting only "
                "adds labels. It never moves, answers or deletes an email."),
    "drafting": ("Writes the text of an email reply in your voice, from a short note on what the "
                 "reply must say. The reply waits as a draft, and nothing goes out until you tap "
                 "Send. On your own model, your model writes it. On Claude or Codex, the model that "
                 "asked for the reply writes it. Letters, such as recommendations, always come "
                 "from Claude or Codex."),
}
# Under a chat row set to Codex on a host with no mediator, what Codex
# cannot do there. With the mediator its network tools run outside the
# sandbox (mail_mediation's resident-net) and the row says nothing.
CODEX_NOTE = ("Codex cannot open web links, Drive, GitHub or Overleaf on this computer, "
              "because mail mediation is off. Setup turns it on.",
              "No mediator on this host: Codex runs with no network, so URL, Drive, GitHub, "
              "Overleaf and plugin turns work from prefetched pages and cached clones only; "
              "deploy/enable-mail-mediation.sh installs it.")
def _mediated() -> bool:
    """Does this host run the mediator?"""
    import mail_mediation
    return mail_mediation.mediation_active_for_resident()


# What a harness cannot do in chat, by harness (HOSTED_MODELS[...]["harness"]):
# the note, and whether the limit holds on this host. The chat row shows the
# note while chat runs on a model of that harness and the limit holds.
HARNESS_LIMITS = {"codex": (CODEX_NOTE, lambda: not _mediated())}


def harness_limit(harness: str, pick: int = 0) -> str:
    """What `harness` cannot do in chat on this host, (plain,
    technical)[pick]; '' for nothing."""
    note, holds = HARNESS_LIMITS.get(harness, (None, None))
    return note[pick] if note and holds() else ""


# The chat floor: the tasks that hold a conversation, which never run on
# the owner's own model while ea-config's `local_chat_ready` is false
# (chat_floor_holds). The note, (plain, technical), is what a refusal, a
# row, a refused `@local` tag and a refused `use local` pin say; the
# floor's name is what model_tiers.resolve carries as `floor`.
CHAT_FLOORED = ("chat", "scheduling")
LOCAL_CHAT_NOTE = ("Your own model does not hold up in conversation yet. Talking with you and "
                   "finding meeting times stay on Claude or Codex for now.",
                   "Chat floor: chat and scheduling never run on the local model while "
                   "local_chat_ready is false in ea-config.json.")
LOCAL_CHAT_FLOOR = "local_chat"
# The built-in presets, defined here and never written: a label and a line
# in plain words, the task values, and the switch (`hosted_default`; absent
# leaves it as it is). A preset's `hosted` map is its overrides; absent, it
# has none. `private` keeps mail work where the remote switches put it,
# `auto`, and the conversations, the meeting times and the checks hosted.
_ALL_HOSTED = {**{t: "hosted" for t in TASKS}, "thread_state": "local"}
BUILTIN_PRESETS = {
    "private": {"label": "Private",
                "note": "Mail, replies, digests, your briefing and the weekly check go to your own "
                        "model where it is set up for them. Claude or Codex talks with you, finds "
                        "meeting times and checks the work.",
                "tasks": {"mail": "auto", "tagging": "auto", "drafting": "auto", "digests": "auto",
                          "briefing": "auto", "audit": "auto", "scheduling": "hosted",
                          "chat": "hosted", "thread_state": "local", "review": "hosted"}},
    "claude": {"label": "All Claude",
               "note": "Claude does every task. Your own model notices what needs you.",
               "tasks": dict(_ALL_HOSTED), "hosted_default": "claude"},
    "codex": {"label": "All Codex",
              "note": "Codex does every task. Your own model notices what needs you.",
              "tasks": dict(_ALL_HOSTED), "hosted_default": "codex"},
}
assert all(set(p["tasks"]) == set(TASKS) for p in BUILTIN_PRESETS.values())
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


# ── the hosted models ─────────────────────────────────────────────────

# The names that mean the owner's own model wherever the code records one:
# a tier, an arm, the release log's `local:<model>`, the tool gateway's
# `<model>+slim` and the pipeline's `pipeline/<model>`. Every other name,
# the empty one included, is a hosted (frontier) model: Claude, Codex or
# any model added later (is_hosted).
LOCAL_NAMES = ("local", "remote", "pipeline", "qwen", "on-prem", "onprem")


@dataclass(frozen=True)
class HostedModel:
    """One entry of ea_config.HOSTED_MODELS, by its key."""
    key: str
    name: str
    harness: str
    tier: str
    tiers: tuple
    arm: str
    names: tuple
    tasks: tuple | None

    @property
    def base(self) -> bool:
        """The bot's own model: every task runs on it, and a hosted model
        that gives no answer hands its call to it."""
        return self.key == BASE

    def takes(self, task: str) -> bool:
        return _ea_config.hosted_takes(self.key, task)


def _entry(key: str) -> HostedModel:
    m = REGISTRY[key]
    return HostedModel(key=key, name=m["name"], harness=m["harness"], tier=m["tier"],
                       tiers=tuple(m["tiers"]), arm=m["arm"], names=tuple(m["names"]),
                       tasks=None if m["tasks"] is None else tuple(m["tasks"]))


def hosted_models() -> list[HostedModel]:
    """Every hosted model, the base first."""
    return [_entry(k) for k in REGISTRY]


def base() -> HostedModel:
    return _entry(BASE)


def is_local(name: str) -> bool:
    """`name` is the local model: a tier or arm (`local`, `remote`,
    `pipeline`), `local:<model>`, the tool gateway's `<model>+slim` or
    `pipeline/<model>`."""
    n = str(name or "").strip().lower()
    return (n in LOCAL_NAMES or n.startswith(("local:", "pipeline/")) or n.endswith("+slim"))


def for_model(name: str) -> HostedModel | None:
    """The hosted model `name` belongs to: a HOSTED_MODELS key, a chat tier
    (`opus`, `fable`, `codex`), an arm, or a model id a run records
    (`claude-opus-5-5[1m]`, `sonnet`, `codex`, `gpt-...`). None for a
    local name and for a name no entry claims."""
    n = str(name or "").strip().lower()
    if not n or is_local(n):
        return None
    for key, m in REGISTRY.items():
        if n == key or n == m["arm"] or n in m["tiers"] or n.startswith(tuple(m["names"])):
            return _entry(key)
    return None


def family(name: str) -> str:
    """The HOSTED_MODELS key `name` belongs to; '' for none."""
    m = for_model(name)
    return m.key if m else ""


def is_hosted(name: str) -> bool:
    """True when `name` is a hosted model: any name that is not the local
    model's. The one class check the privacy paths make (keep levels, the
    keep floor, the release counts), so a switch between hosted models, or
    a new one, changes none of them. An unknown or empty name counts as
    hosted, so a privacy check fails closed; a routing branch that needs
    the model's registry entry asks `for_model`."""
    return not is_local(name)


def is_base(name: str) -> bool:
    """`name` is the base hosted model, the one the bot's own harness runs."""
    m = for_model(name)
    return bool(m and m.base)


def harness_of(name: str) -> str:
    """The harness that runs `name` (`claude`, `codex`); '' for a local or
    unknown name."""
    m = for_model(name)
    return m.harness if m else ""


def allowed(task: str) -> list[str]:
    """The hosted models `task` can take, the base first."""
    return [k for k in REGISTRY if _ea_config.hosted_takes(k, task)]


# ── the table ─────────────────────────────────────────────────────────

def _block(routing: dict | None) -> dict:
    """The routing block as routing_from_block gives it; default
    ea_config.get_routing()."""
    return _ea_config.routing_from_block(routing if routing is not None else _ea_config.get_routing())


def table(routing: dict | None = None) -> dict[str, str]:
    """{task: value} with the floors applied. `routing` is a routing
    block ({"tasks": {...}, "hosted": {...}, "hosted_default": ...});
    default ea_config.get_routing()."""
    return dict(_block(routing)["tasks"])


def hosted_default(routing: dict | None = None) -> str:
    """The switch: the hosted model every task without an override runs
    on; '' when the owner has not set it (Claude, and the chat and
    scheduling lanes keep their tiers block)."""
    return _block(routing).get("hosted_default", "")


def overrides(routing: dict | None = None) -> dict[str, str]:
    """{task: model}: the tasks the owner set apart from the switch."""
    return dict(_block(routing)["hosted"])


def hosted_map(routing: dict | None = None) -> dict[str, str]:
    """{task: model} for every task: the task's override, else the switch
    (`hosted_default`), else the base (Claude). A model the task cannot
    take reads the base."""
    block = _block(routing)
    default = block.get("hosted_default") or BASE
    out = {}
    for task in TASKS:
        m = block["hosted"].get(task) or default
        out[task] = m if _ea_config.hosted_takes(m, task) else BASE
    return out


def hosted_source(task: str, routing: dict | None = None) -> str:
    """Where the task's hosted model comes from: `override` (its own
    entry), `default` (the switch, or the base with none set), or `fixed`
    (the task has no choice of its own, or the switch names a model that
    cannot take it, so it stays on the base)."""
    block = _block(routing)
    if len(allowed(task)) < 2:
        return "fixed"
    if task in block["hosted"]:
        return "override"
    default = block.get("hosted_default") or BASE
    return "default" if _ea_config.hosted_takes(default, task) else "fixed"


def _chosen(task: str, block: dict) -> bool:
    """The owner chose the task's hosted model: an override or the switch."""
    return task in block["hosted"] or bool(block.get("hosted_default"))


# The lane whose tiers block named a hosted model for a task before the
# switch existed: with neither the switch nor an override set, it still does.
_TIERS_LANE = {"chat": "main", "scheduling": "sched-channel"}


def hosted_model(task: str, *, routing: dict | None = None, tiers: dict | None = None) -> str:
    """The hosted model (a HOSTED_MODELS key) `task` uses when it runs
    hosted: `hosted_map`. With no switch and no override set, the chat and
    scheduling tasks take the model the tiers block's lane default names
    (`codex` on the main or the scheduling channel's lane), as before the
    switch; every other task the base."""
    block = _block(routing)
    if _chosen(task, block) or task not in _TIERS_LANE:
        return hosted_map(block)[task]
    tiers = tiers if tiers is not None else _ea_config.get_tiers()
    named = family((tiers.get("lanes") or {}).get(_TIERS_LANE[task]))
    return named if named and _ea_config.hosted_takes(named, task) else BASE


def lane_hosted(lane: str, routing: dict | None = None) -> str:
    """The hosted model the owner chose for a chat lane's task (LANE_TASK):
    the override, else the switch; '' when neither is set, so the tiers
    block's lane default stands. model_tiers.resolve puts it over the
    tiers block."""
    task = LANE_TASK.get(lane)
    if not task:
        return ""
    block = _block(routing)
    return hosted_map(block)[task] if _chosen(task, block) else ""


def harness(agent: str, routing: dict | None = None) -> str:
    """The harness that runs the agent's Claude lane (`claude` or
    `codex`): its task's hosted model (AGENT_TASK, `hosted_map`), so the
    switch moves the briefings, the audit and the digests with the rest.
    run-agent.sh asks before it starts the lane; EA_HARNESS still forces
    Codex."""
    task = AGENT_TASK.get(agent)
    return REGISTRY[hosted_map(routing)[task] if task else BASE]["harness"]


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
    ks = cfg.get("keep_from_frontier")
    if not isinstance(ks, dict):
        ks = cfg.get("keep_from_anthropic") or {}
    return bool(ks.get("domains") or ks.get("addresses") or ks.get("admissions"))


def chat_floor_holds(task: str = "") -> bool:
    """The chat floor is in force (ea_config.local_chat_ready is false)
    and, with `task`, holds that task (CHAT_FLOORED). A config that cannot
    be read keeps the floor."""
    if task and task not in CHAT_FLOORED:
        return False
    try:
        return not _ea_config.local_chat_ready()
    except Exception:
        return True


def choice(task: str, *, cfg: dict | None = None, routing: dict | None = None) -> str:
    """What the table asks of the task once the endpoint, the chat floor
    and the keep floor apply: `local`, `hosted` or `auto` (the switches
    decide)."""
    v = value(task, routing)
    if v == "local" and chat_floor_holds(task):
        return "hosted"
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
    `local` and `auto` keep the switch (enforce stays behind its gate).
    Under the chat floor `enforce` reads `shadow`: the agent runs beside
    the hosted turn and answers none."""
    if choice("scheduling", cfg=cfg, routing=routing) == "hosted":
        return "off"
    return "shadow" if raw == "enforce" and chat_floor_holds("scheduling") else raw


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

# What `auto` did, in plain words, by where it put the task. `{hosted}`
# is the hosted model's name.
AUTO_SAYS = {"local": "Auto picked your own model", "hosted": "Auto picked {hosted}",
             "mixed": "Auto is trying your own model and {hosted} side by side"}
JUDGMENT = "these are judgment calls, so your own model never makes them"
NOT_SET_UP = "your own model is not set up, so {hosted} does it"


def _and(items: list[str]) -> str:
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def _switch_state(task: str, cfg: dict, tiers: dict, done: dict, name: str) -> tuple[str, str, str]:
    """(local | hosted | mixed, plain why, technical why): where the remote
    layer's switches and the tiers block put the task, which `auto` keeps.
    `done` holds the rows made so far ({task: effective}); tagging follows
    the mail row. `name` is the task's hosted model, as the screen says it."""
    import remote_llm

    def onoff(flag: bool) -> str:
        return "local" if flag else "hosted"

    def says(eff: str) -> str:
        return AUTO_SAYS[eff].format(hosted=name)
    if task == "mail":
        m = remote_llm.switch_mode("mail", cfg)
        eff = onoff(m in ("enforce", "ghost", "sealed"))
        return eff, says(eff), f"the mail switch is {m}"
    if task == "tagging":
        on = done.get("mail") == "local"
        return (onoff(on), f'Auto does what "{TASKS["mail"][0]}" does',
                "it follows the mail gate, which is " + ("on" if on else "off"))
    if task in ("drafting", "digests", "briefing", "audit"):
        key = {"briefing": "briefings", "audit": "audits"}.get(task, task)
        val = cfg.get(key) or "off"
        eff = "mixed" if val == "ab" else onoff(val == "remote")
        return eff, says(eff), f"the {key} switch is {val}"
    if task == "thread_state":
        if local_ready(cfg):
            return ("local", f"Auto uses your own model, and {name} takes what it leaves",
                    "it runs on the local model; the hosted model takes the threads left "
                    "when it does not answer")
        return ("hosted", NOT_SET_UP.format(hosted=name),
                "no local model is set up; the hosted model judges, one call a tick")
    lanes = tiers.get("lanes") or {}
    if task == "scheduling":
        m = remote_llm.switch_mode("scheduling", cfg)
        lane = lanes.get("sched-channel") == "local"
        eff = onoff(m == "enforce" or lane)
        return eff, says(eff), (f"the scheduling switch is {m}"
                                + ("; the channel's tier is local" if lane else ""))
    if task == "chat":
        on = [k for k in ("main", "dm", "room") if lanes.get(k) == "local"]
        if not on:
            return ("hosted", f"Auto picked {name}; start a message with @local to have your own "
                              "model answer it",
                    "no chat lane's tier is local; @local or `use local` picks it per topic")
        if len(on) == 3:
            return "local", says("local"), f"the tier is local in {', '.join(on)}"
        return ("mixed", f"Auto picked your own model for {_and([PLACES[k] for k in on])}, "
                         f"and {name} for the rest", f"the tier is local in {', '.join(on)}")
    return "hosted", JUDGMENT, "the stakes floor"


def runs_on(effective: str, hosted: str) -> str:
    """What the task runs on now, as the screen says it."""
    name = MODEL_NAMES.get(hosted, hosted)
    return {"local": "your own model", "hosted": name,
            "mixed": f"your own model and {name}"}.get(effective, effective)


def _fixed_why(task: str, asked: str, pick: int) -> str:
    """Why a `fixed` task stays on the base, (plain, technical)[pick]."""
    b = MODEL_NAMES[BASE]
    if len(allowed(task)) < 2:
        return ("it has no hosted model of its own to pick",
                "no hosted model of its own: " + HOSTED_NOTES.get(task, ("", "it takes only the base"))[1]
                )[pick]
    name = MODEL_NAMES.get(asked, asked)
    return (f"{name} cannot do it, so it stays on {b}",
            f"{asked} cannot take {task}; it reads {BASE}")[pick]


def rows(*, cfg: dict | None = None, routing: dict | None = None,
         tiers: dict | None = None, verbose: bool = False) -> list[dict]:
    """One row per task for the screen, the doctor and the chat command:
    {"task", "label", "what", "value", "allowed", "locked", "floor",
    "effective" (local | hosted | mixed), "why", "hosted" (claude |
    codex: the model its hosted part uses), "hosted_allowed" (the hosted
    models it can take), "hosted_source" (default | override | fixed:
    hosted_source), "hosted_why" (why a fixed task stays where it is, ''
    otherwise), "hosted_note", "runs_on" (what it runs on now, named),
    "help" (the "?" text, '' for none)}. `what`, `floor`, `hosted_note`,
    `hosted_why` and `why` are plain words, or with `verbose` the code's
    terms; `help` is plain words always."""
    cfg = _remote_cfg(cfg)
    tiers = tiers if tiers is not None else _ea_config.get_tiers()
    block = _block(routing)
    tab = table(block)
    kept = keep_listed(cfg)
    pick = 1 if verbose else 0
    asked = block.get("hosted_default") or BASE
    out, done = [], {}
    for task, (label, what) in TASKS.items():
        v = tab[task]
        hm = hosted_model(task, routing=block, tiers=tiers)
        name = MODEL_NAMES[hm] if not verbose else f"the hosted model ({hm})"
        source = hosted_source(task, block)
        row = {"task": task, "label": label, "what": TECH[task] if verbose else what, "value": v,
               "allowed": list(VALUES), "locked": False, "floor": "", "hosted": hm,
               "hosted_allowed": allowed(task), "hosted_source": source,
               "hosted_why": _fixed_why(task, asked, pick) if source == "fixed" else "",
               "hosted_note": HOSTED_NOTES[task][pick] if task in HOSTED_NOTES else "",
               "help": HELP.get(task, "")}
        if task == "chat":
            row["hosted_note"] = harness_limit(REGISTRY[hm]["harness"], pick) or row["hosted_note"]
        if task in FLOORS:
            row.update(allowed=[FLOORS[task]], locked=True, floor=STAKES_FLOOR[pick])
        elif task in KEEP_FLOORED and kept:
            row.update(allowed=["local", "auto"], floor=KEEP_FLOOR[pick])
        floored = chat_floor_holds(task)
        row["not_yet"] = floored
        if floored:
            row.update(allowed=[a for a in row["allowed"] if a != "local"], floor=LOCAL_CHAT_NOTE[pick])
        c = choice(task, cfg=cfg, routing={"tasks": tab})
        judged = "the hosted model judges, one call a tick" if task == "thread_state" else ""
        if task in FLOORS:
            eff, why = FLOORS[task], (JUDGMENT, "the stakes floor")
        elif c == "local":
            eff, why = "local", ("set to your own model", "set to local")
        elif floored and v != "hosted":
            eff, why = "hosted", (f"{name} does it: your own model is not ready for conversation yet",
                                  f"set to {v}, on {hm}: the chat floor holds")
        elif c == "hosted" and v == "local":
            eff, why = "hosted", (f"set to your own model, but {NOT_SET_UP.format(hosted=name)}",
                                  "set to local, but no local model is set up"
                                  + (f"; {judged}" if judged else ""))
        elif c == "hosted":
            eff, why = "hosted", (f"set to {name}",
                                  f"set to hosted, on {hm}" + (f": {judged}" if judged else ""))
        else:
            eff, plain, tech = _switch_state(task, cfg, tiers, done, name)
            why = (plain, tech)
            if v == "hosted":
                why = (f"{name} waits while you keep some mail away from hosted models; {plain}",
                       f"hosted waits on the keep floor; {tech}")
        row.update(effective=eff, why=why[pick], runs_on=runs_on(eff, hm))
        done[task] = eff
        out.append(row)
    return out


def payload(*, cfg: dict | None = None, routing: dict | None = None,
            tiers: dict | None = None, verbose: bool = False) -> dict:
    """The Settings screen's data (GET /api/settings/routing), in plain
    words; `verbose` gives the code's terms (`show --verbose`, the doctor)."""
    cfg = _remote_cfg(cfg)
    source = "given" if routing is not None else _ea_config.get_routing_source()[1]
    default = hosted_default(routing)
    return {"local": local_status(cfg, verbose=verbose), "source": source, "values": list(VALUES),
            "hosted_values": list(HOSTED), "hosted_names": dict(MODEL_NAMES),
            "hosted_default": default or BASE, "hosted_default_set": bool(default),
            "presets": preset_list(routing), "preset_now": preset_now(routing),
            "rows": rows(cfg=cfg, routing=routing, tiers=tiers, verbose=verbose)}


def lines(data: dict | None = None) -> list[str]:
    """The table as chat lines (the `routing` command, `show`): the hosted
    model first, then each task with its hosted model marked `default`
    (it follows the switch), `override` (its own choice) or `fixed` (with
    why)."""
    data = data if data is not None else payload()
    loc = data["local"]
    head = (f"Local model {loc['model']}: {loc['detail']}" if loc["configured"] else loc["detail"])
    out = [f":control_knobs: *Model routing.* {head}"]
    default = MODEL_NAMES.get(data["hosted_default"], data["hosted_default"])
    out.append(f"*Hosted model:* {default} for every task not set apart"
               + ("." if data.get("hosted_default_set") else " (not chosen yet)."))
    for r in data["rows"]:
        mark = r.get("hosted_source", "default")
        if mark == "fixed":
            mark = f"fixed: {r['hosted_why']}"
        out.append(f"- *{r['label']}* (`{r['task']}`): {r['value']}, hosted on "
                   f"{MODEL_NAMES.get(r['hosted'], r['hosted'])} ({mark}), now {r['runs_on']} ({r['why']})"
                   + ("; your own model: not yet" if r.get("not_yet") else ""))
    if any(r.get("not_yet") for r in data["rows"]):
        out.append(LOCAL_CHAT_NOTE[0])
    if data.get("presets"):
        out.append("Presets: " + ", ".join(f"{p['label']} (`{p['name']}`)" for p in data["presets"])
                   + (f"; now {data['preset_now']}" if data.get("preset_now") else "") + ".")
    if str(data.get("source", "")).startswith("refused"):
        out.append(f"routing.json is {data['source']}; every task reads auto.")
    out.append("Change it in the web chat's Settings, or as the owner: "
               "`python3 preprocessor/model_routing.py set-hosted claude|codex [--task <task>]`, "
               "`set-hosted --clear-task <task>`, `set <task> local|hosted|auto`, or "
               "`preset apply <name>`.")
    return out


# ── saving ────────────────────────────────────────────────────────────

def _refusals(tasks: dict, kept: bool, hosted: dict | None = None,
              default=None) -> list[tuple[str, str]]:
    """(plain, technical) for each task `save` refuses: an unknown task, a
    value that is not local, hosted or auto, a value under the stakes
    floor, and `hosted` under the keep floor while the keep list keeps
    anything; in `hosted`, a model that is not a hosted model (FOLLOW
    drops an override), and a model for a task it cannot take; for the
    switch (`default`), a model that is not a hosted model. The technical
    text is ea_config.routing_problems' own."""
    out = []
    names = " or ".join(MODEL_NAMES.values())
    if default is not None and default not in HOSTED:
        out.append((f"The hosted model is {names}.",
                    "; ".join(_ea_config.routing_problems({"hosted_default": default}))))
    for task, m in (hosted or {}).items():
        tech = _ea_config.routing_problems({"hosted": {task: m}})
        name = f'"{TASKS[task][0]}"' if task in TASKS else ""
        if task not in TASKS:
            plain = f"Tacitus has no task named {task!r}."
        elif m == FOLLOW:
            continue
        elif m not in HOSTED:
            plain = f"{name} takes {names} as its hosted model."
        elif tech:
            plain = f"{name} has no hosted model of its own to pick. " + (
                HOSTED_NOTES[task][0] if task in HOSTED_NOTES else f"{MODEL_NAMES[m]} cannot do it yet.")
        else:
            continue
        out.append((plain, "; ".join(tech)))
    for task, v in tasks.items():
        tech = _ea_config.routing_problems({"tasks": {task: v}})
        name = f'"{TASKS[task][0]}"' if task in TASKS else ""
        if task not in TASKS:
            plain = f"Tacitus has no task named {task!r}."
        elif v not in VALUES:
            plain = f"{name} takes your own model, Claude or Codex, or Auto."
        elif tech:
            plain = f"{name} always stays on Claude or Codex: {JUDGMENT}."
        elif v == "local" and chat_floor_holds(task):
            plain, tech = LOCAL_CHAT_NOTE[0], [f"{task}: {LOCAL_CHAT_NOTE[1]}"]
        elif v == "hosted" and task in KEEP_FLOORED and kept:
            plain = f"{name} cannot use Claude or Codex while you keep some mail away from hosted models."
            tech = [f"{task} cannot be hosted while the keep list has entries (the keep floor)"]
        else:
            continue
        out.append((plain, "; ".join(tech)))
    return out


def save(tasks, *, hosted=None, hosted_default=None, cfg: dict | None = None, via: str = "cli",
         who: dict | None = None, replace_hosted: bool = False, presets: dict | None = None,
         preset: str = "") -> dict:
    """Merge `tasks` ({task: value}), `hosted` ({task: claude | codex},
    or FOLLOW to drop the task's override) and the switch `hosted_default`
    (claude | codex) over the table in force and write
    <private_dir>/routing.json (0600). Owner processes only. Overrides
    stay when the switch moves. Raises RoutingRefused for an unknown task
    or value, a floored value, `hosted` on a keep-floored task while the
    keep list keeps anything, or a model for a task it cannot take. `who`
    ({"credential", "device"}, from the web front end) goes into the
    change log line. `replace_hosted` makes `hosted` the whole override
    map (a preset); `presets` ({name: snapshot}) is merged into the saved
    presets; `preset` names the preset applied, for the log."""
    tasks = {} if tasks is None else tasks
    hosted = {} if hosted is None else hosted
    if not isinstance(tasks, dict) or not isinstance(hosted, dict) or \
            not (tasks or hosted or hosted_default or presets or replace_hosted):
        raise RoutingRefused("Choose your own model, Claude or Codex, or Auto for at least one task.",
                             "tasks and hosted must be JSON objects that name at least one task, "
                             "or hosted_default a hosted model")
    problems = _refusals(tasks, keep_listed(_remote_cfg(cfg)), hosted, hosted_default)
    if problems:
        raise RoutingRefused(" ".join(dict.fromkeys(p for p, _t in problems)),
                             "; ".join(t for _p, t in problems))
    ot = _ea_config._owner_trust()
    if not ot.is_owner_process():
        raise RoutingRefused("Tacitus could not save: only the part of Tacitus that runs as you "
                             "may change these settings.",
                             "only the owner's processes write the routing table")
    current, _source = _ea_config.get_routing_source()
    picked = {k: v for k, v in {**({} if replace_hosted else current.get("hosted", {})), **hosted}.items()
              if hosted.get(k) != FOLLOW}
    raw = {"tasks": {**current["tasks"], **tasks}, "hosted": picked}
    if hosted_default or current.get("hosted_default"):
        raw["hosted_default"] = hosted_default or current["hosted_default"]
    if current.get("presets") or presets:
        raw["presets"] = {**current.get("presets", {}), **(presets or {})}
    block = _ea_config.routing_from_block(raw)
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
        was = current.get("hosted", {})
        changed.update({f"hosted.{k}": [was.get(k, ""), block["hosted"].get(k, "")]
                        for k in {**was, **block["hosted"]}
                        if was.get(k, "") != block["hosted"].get(k, "")})
        if current.get("hosted_default", "") != block.get("hosted_default", ""):
            changed["hosted_default"] = [current.get("hosted_default", ""), block["hosted_default"]]
        changed.update({f"presets.{k}": ["", "saved"] for k in (presets or {})})
        line = {"ts": datetime.now().astimezone().isoformat(timespec="seconds"),
                "via": via, "changed": changed}
        if preset:
            line["preset"] = preset
        for key in ("credential", "device"):
            if (who or {}).get(key):
                line[key] = str(who[key])[:80]
        remote_llm.append(CHANGES, line)
    except Exception:
        pass
    return block


# ── presets ───────────────────────────────────────────────────────────

def presets(routing: dict | None = None) -> dict[str, dict]:
    """Every preset by name: the built-ins, then the owner's saved ones."""
    return {**BUILTIN_PRESETS, **_block(routing).get("presets", {})}


def preset_list(routing: dict | None = None) -> list[dict]:
    """[{"name", "label", "note", "builtin"}] for the screen and `preset
    list`, in plain words."""
    return [{"name": k, "label": p.get("label") or k, "note": str(p.get("note") or ""),
             "builtin": k in BUILTIN_PRESETS} for k, p in presets(routing).items()]


def _preset_effect(p: dict, block: dict) -> tuple:
    """What applying `p` over `block` leaves: (tasks, switch, overrides)."""
    tasks = {**block["tasks"], **p.get("tasks", {}), **FLOORS}
    return tasks, p.get("hosted_default") or block.get("hosted_default", ""), dict(p.get("hosted") or {})


def preset_now(routing: dict | None = None) -> str:
    """The name of the preset the table in force matches, '' for none; a
    saved preset before a built-in that makes the same table."""
    block = _block(routing)
    now = (block["tasks"], block.get("hosted_default", ""), block["hosted"])
    ordered = {**block.get("presets", {}), **BUILTIN_PRESETS}
    return next((k for k, p in ordered.items() if _preset_effect(p, block) == now), "")


def apply_preset(name: str, *, cfg: dict | None = None, via: str = "cli",
                 who: dict | None = None) -> dict:
    """Write the preset's task values and switch, and make its overrides
    the whole override map. Under the keep floor a `hosted` value on a
    keep-floored task is written as `auto`, and `notes` says so; a value
    a floor refuses (the chat floor, the stakes floor) refuses the whole
    preset. Returns {"block", "notes"}."""
    p = presets().get(str(name or ""))
    if p is None:
        raise RoutingRefused(f"Tacitus has no preset named {name!r}.", f"no preset {name!r}")
    tasks = dict(p.get("tasks") or {})
    notes = []
    held = [t for t in KEEP_FLOORED if tasks.get(t) == "hosted"] if keep_listed(_remote_cfg(cfg)) else []
    if held:
        tasks.update({t: "auto" for t in held})
        notes.append(f"{_and([TASKS[t][0] for t in held])} stay on Auto while you keep some mail "
                     "away from hosted models.")
    block = save(tasks, hosted=dict(p.get("hosted") or {}), hosted_default=p.get("hosted_default") or None,
                 replace_hosted=True, cfg=cfg, via=via, who=who, preset=name)
    return {"block": block, "notes": notes}


def save_preset(name: str, note: str = "", *, via: str = "cli", who: dict | None = None) -> dict:
    """Keep the table in force (task values, switch, overrides) under
    `name` in routing.json's `presets`; a name a built-in has is refused."""
    name = str(name or "").strip()
    if name.lower() in BUILTIN_PRESETS:
        raise RoutingRefused("That name belongs to a preset Tacitus has. Pick another name.",
                             f"{name!r} is a built-in preset")
    current, _source = _ea_config.get_routing_source()
    snap = {"label": name, "tasks": dict(current["tasks"]), "hosted": dict(current["hosted"]),
            "note": str(note or "")[:200],
            "saved": datetime.now().astimezone().isoformat(timespec="seconds")}
    if current.get("hosted_default"):
        snap["hosted_default"] = current["hosted_default"]
    why = _ea_config.preset_problems(name, snap)
    if why:
        raise RoutingRefused("A preset's name is up to 40 letters, digits, spaces, dashes or underscores.",
                             why)
    return save({}, presets={name: snap}, via=via, who=who)


class WebSettings:
    """The web chat's Settings screen: botcore's WebFrontend serves it at
    /api/settings/routing (ea-chat-bot.py registers it). A body with
    `preset` applies one (a name) or keeps the table in force as one
    ({"save": name, "note": text}); any other body saves the values."""

    def get(self) -> dict:
        return payload()

    def save(self, body: dict, who: dict | None = None) -> dict:
        p = body.get("preset")
        if isinstance(p, str):
            out = apply_preset(p, via="web", who=who)
            return {**payload(), "saved": True, "notes": out["notes"]}
        if isinstance(p, dict):
            save_preset(p.get("save"), p.get("note") or "", via="web", who=who)
            return {**payload(), "saved": True, "notes": []}
        if p is not None:
            raise RoutingRefused("Pick a preset by its name.", "preset takes a name or {save, note}")
        save(body.get("tasks"), hosted=body.get("hosted"), hosted_default=body.get("hosted_default"),
             via="web", who=who)
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
    if data["hosted_default_set"]:
        detail += f"; hosted default: {data['hosted_default']}"
    for m in hosted_models():
        on = [r["task"] for r in data["rows"] if r["hosted"] == m.key and not m.base]
        if on:
            detail += f"; hosted on {m.key}: {', '.join(on)}"
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
    s = sub.add_parser("show", help="the hosted model, then each task: the value, its hosted model "
                                    "(default or override), the model it runs on now, why")
    s.add_argument("--json", action="store_true")
    s.add_argument("--verbose", action="store_true",
                   help="the code's terms: the switches, the lanes, the local model's last error")
    p = sub.add_parser("set", help="write one task into the owner-private routing.json")
    p.add_argument("task", choices=list(TASKS))
    p.add_argument("value", choices=list(VALUES) + list(HOSTED),
                   help="local, hosted or auto; or claude or codex for the task's hosted model (an override)")
    sh = sub.add_parser("set-hosted", help="the hosted model for every task, or one task's override")
    sh.add_argument("model", nargs="?", choices=list(HOSTED),
                    help="claude or codex: every task without an override, or with --task that task")
    sh.add_argument("--task", choices=list(TASKS), help="set an override for this task only")
    sh.add_argument("--clear-task", action="append", default=[], choices=list(TASKS),
                    help="drop this task's override, so it follows the hosted model again (repeatable)")
    pr = sub.add_parser("preset", help="list, apply or save a preset")
    pr.add_argument("action", choices=["list", "apply", "save"])
    pr.add_argument("name", nargs="?")
    pr.add_argument("--note", default="", help="save: one line on what the preset is for")
    h = sub.add_parser("harness", help="claude or codex: where run-agent.sh runs the agent's Claude lane")
    h.add_argument("--agent", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "harness":
        try:
            print(harness(a.agent))
        except Exception as e:
            print(f"harness: the routing table is unread ({e}); {REGISTRY[BASE]['harness']}", file=sys.stderr)
            print(REGISTRY[BASE]["harness"])
        return 0
    if a.cmd == "preset":
        if a.action == "list":
            now = preset_now()
            for p in preset_list():
                print(f"{p['name']}{' (now)' if p['name'] == now else ''}: {p['label']}. {p['note']}".rstrip())
            return 0
        if not a.name:
            ap.error(f"preset {a.action} needs a name")
        try:
            if a.action == "apply":
                out = apply_preset(a.name)
                block, notes = out["block"], out["notes"]
            else:
                block, notes = save_preset(a.name, a.note), []
        except RoutingRefused as e:
            print(f"preset {a.action} refused: {e.tech}", file=sys.stderr)
            return 2
        print(json.dumps({"preset": a.name, "tasks": block["tasks"],
                          "hosted_default": block.get("hosted_default", ""),
                          "overrides": block["hosted"], "notes": notes}))
        return 0
    if a.cmd == "set-hosted":
        if not a.model and not a.clear_task:
            ap.error("set-hosted needs a model, --clear-task, or both")
        if a.task and not a.model:
            ap.error("--task needs a model")
        hosted = {t: FOLLOW for t in a.clear_task}
        if a.task:
            hosted[a.task] = a.model
        try:
            block = save({}, hosted=hosted, hosted_default=None if a.task else a.model)
        except RoutingRefused as e:
            print(f"set-hosted refused: {e.tech}", file=sys.stderr)
            return 2
        print(json.dumps({"hosted_default": block.get("hosted_default", ""),
                          "overrides": block["hosted"], "hosted": hosted_map(block)}))
        return 0
    if a.cmd == "set":
        model = a.value in HOSTED
        try:
            block = save({} if model else {a.task: a.value},
                         hosted={a.task: a.value} if model else None)
        except RoutingRefused as e:
            print(f"set refused: {e.tech}", file=sys.stderr)
            return 2
        print(json.dumps({"task": a.task, "value": block["tasks"][a.task],
                          "hosted": hosted_model(a.task, routing=block)}))
        return 0
    data = payload(verbose=a.verbose)
    print(json.dumps(data, indent=2) if a.json else "\n".join(lines(data)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

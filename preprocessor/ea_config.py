"""
Operational configuration loader for the EA system.

Reads ~/.ea/config/ea-config.json and provides typed accessors for
system-wide toggles (master switch, eval controls, log level).

Missing file = everything enabled at default levels. Malformed JSON
falls back to defaults silently. Reads fresh from disk on each call
so changes take effect immediately (no restart needed for cron jobs;
bot restart needed for log level).
"""

import json
import logging
import os
import sys
from pathlib import Path

EA_DIR = Path(os.environ.get("EA_DIR", Path.home() / ".ea"))
CONFIG_FILE = EA_DIR / "config" / "ea-config.json"

# Install profiles: the three modes. "reviewer" has no chat: the EA reads
# mail and calendars, writes drafts to the mailbox and its reports to files
# on the schedule the user picks (with no bot to read a staged event there
# are no holds). "assistant" is one chat: the EA channel, the DM for cards
# and buttons, scheduling and digests in the same app, the resident and
# plugins. "collaborator" is "assistant" plus project rooms. The `slack`
# feature means a chat front end of any kind (`chat` is its CLI name).
PROFILES = ("reviewer", "assistant", "collaborator")
DEFAULT_PROFILE = "assistant"
PROFILE_FEATURES = {
    "reviewer":     {"slack": False, "buttons": False, "drafting": True, "resident": False, "rooms": False, "plugins": False},
    "assistant":    {"slack": True,  "buttons": True,  "drafting": True, "resident": True,  "rooms": False, "plugins": True},
    "collaborator": {"slack": True,  "buttons": True,  "drafting": True, "resident": True,  "rooms": True,  "plugins": True},
}
# A config that names a mode writes "profile_version" beside it. A stored
# profile without it is an old name: it reads as the mode it was, with the
# switch it implied, until `normalize-profile` renames it. The old
# "assistant" had project rooms, so there it reads as "collaborator".
PROFILE_VERSION = 2
LEGACY_PROFILES = {
    "email": ("reviewer", {}),
    "review-only": ("reviewer", {"drafting": False}),
    "assistant": ("collaborator", {}),
}
# What a person reads when a mode is chosen: one plain line each.
PROFILE_SUMMARY = {
    "reviewer": "No chat. It reads your mail and calendar, leaves reply drafts in your "
                "mailbox, and writes you a report when you want one.",
    "assistant": "One chat on your phone. It drafts replies, finds meeting times and asks "
                 "before anything goes out.",
    "collaborator": "The assistant, plus a separate chat room for each big project.",
}
# reports.schedule: when the report agents run from cron. "none" means on
# demand only (run-agent.sh morning-briefing --now).
REPORT_SCHEDULES = ("weekdays", "daily", "none")
_LOG = logging.getLogger("ea_config")
_LEGACY_LOGGED: set = set()

DEFAULTS = {
    "profile": DEFAULT_PROFILE,
    # On or off in every mode: false keeps the EA from writing drafts (the
    # launchers add scripts/disallowed-tools.no-drafting.txt). Only a
    # literal false turns it off.
    "drafting": True,
    "reports": {"schedule": "weekdays"},
    "ea_enabled": True,
    # The nightly full-mode evals and the weekly release tests are gated by
    # the owner (<EA_PRIVATE_DIR>/evals.json, tests/evals/eval_gates.py),
    # not here: they start a tool-using `claude -p` as the owner.
    "eval_postcommit_enabled": True,
    "log_level": "INFO",
    # Project rooms: a reply that states a status the turn never read, or
    # says something does not exist, goes back to its session once with
    # the sentences to verify before it posts (ea-chat-bot.py _reply_check).
    "room_reply_check": True,
    # Admissions filter (preprocessor/admissions_filter.py): cold mail from
    # prospective PhD/MS students moves to the Outlook folder
    # admissions-inquiries and is summarised weekly. Off unless the install
    # turns it on; a faculty install with heavy applicant mail sets true.
    "admissions_filter": {"enabled": False},
    # Guestless events (ea-chat-bot.py _create_guestless_event): a staged
    # Outlook event with no attendees is created at once, with no Create
    # card. On unless the install turns it off; off, every event is carded.
    "guestless_events": {"enabled": True},
    # The closure hill climb (preprocessor/closure_policy.py): a signal the
    # owner keeps confirming closes at once (a note with a Reopen button),
    # and one whose closes he reopens proposes again. On unless the install
    # turns it off; off, every signal does what closure.py says.
    "closure": {"hill_climb": True},
    # Mail tagger (preprocessor/mail_tagger.py): new inbox mail gets Outlook
    # categories (project or domain, state, topic) from one small model
    # call per batch, on a two-minute owner cron. Off unless turned on.
    "mail_tagger": {"enabled": False, "model": "haiku"},
    # How chat conversations map onto Claude sessions. "per-topic": every
    # topic (thread) in the main channel and the DM is its own session,
    # as project rooms already are; the resident pane takes no chat turns.
    # "resident": the main channel and the DM share the resident pane's one
    # session, and a topic gets its own only when the pane is busy.
    "chat_sessions": "per-topic",
    # Automatic updates (scripts/update.py, docs/updates.md): "auto" follows
    # the profile's channel (latest on assistant, stable otherwise),
    # "latest", "stable" or "off". Empty means not set: a code tree the
    # post-commit hook writes stays the hook's, and the updater leaves it.
    # The owner-private update.json outranks this value.
    "update": {"channel": ""},
    # Timezone-aware quiet hours: proactive Slack posts that would land
    # during this local window are held and flushed as one digest when the
    # window ends (see preprocessor/quiet_hours.py). The window is
    # interpreted in the ACTIVE timezone (travel_mode.timezone when
    # travelling, else the home timezone from scheduling-preferences.json).
    "quiet_hours": {
        "enabled": True,
        "start": "22:00",
        "end": "07:30",
        "allow_urgent": False,
        # When true, quiet hours engage ONLY while travel mode is active;
        # at home the EA posts immediately. Set false to hold overnight at
        # home as well.
        "only_when_traveling": True,
    },
    # Travel mode: while enabled and on/before `until`, the active timezone
    # for quiet-hours math becomes `timezone` instead of the home tz. It
    # reverts after `until` with no rewrite needed.
    "travel_mode": {
        "enabled": False,
        "timezone": None,
        "until": None,
    },
    # Per-lane model overrides for the Slack bot. room_specialist is the
    # persistent session each #ea-<project> room runs on; scheduler is the
    # scheduling channel's per-thread session (empty: the per-thread
    # default). The resident and the per-thread default come from the
    # environment (EA_RESIDENT_MODEL, EA_MODEL); email and letter bodies
    # are written by the email-author subagent, whose model is in
    # .claude/agents/email-author.md.
    "models": {
        "room_specialist": "claude-opus-5-5[1m]",
        "scheduler": "",
    },
    # Per-lane effort levels, passed as --effort beside the model. The
    # CLI's default effort differs by model, so a model change on its own
    # would change effort silently. Empty means the CLI default. A room
    # that needs more pins its own (ea_channels.py set-effort).
    "efforts": {
        "room_specialist": "high",
        "scheduler": "",
    },
    # A project room's specialist session starts over when its transcript
    # passes rotate_bytes (a follow-up in a thread the session already
    # served waits until twice that). The project pack and journal carry
    # the room's durable context into the new session. 0 turns it off.
    "rooms": {
        "rotate_bytes": 1_000_000,
    },
    # Tiers (preprocessor/model_tiers.py): who the user is talking to in
    # a chat topic. `default` is the tier a turn runs on with nothing
    # else said; `models`/`efforts` name the Claude tiers (an empty opus
    # model is EA_MODEL); `lanes` gives a lane (main, dm, sched-channel,
    # room, proactive) its own default tier; `fable_daily_turns` caps
    # Fable's turns a day (0: no cap). A message tag, a topic pin or a
    # topic name outranks the lane default; a high-stakes message never
    # runs on `local` unless the user tagged it so.
    "tiers": {
        "default": "opus",
        "models": {"opus": "", "fable": "claude-fable-5-1"},
        "efforts": {"opus": "", "fable": ""},
        "lanes": {},
        "fable_daily_turns": 0,
    },
    # Model routing (preprocessor/model_routing.py): which model does each
    # task. `local` is the owner's own endpoint (remote-llm.json), `hosted`
    # is a hosted model (HOSTED_MODELS), `auto` leaves the task where the
    # remote layer's switches and the tiers block put it. Two floors in
    # code sit under every value: `review` is hosted only (the stakes
    # floor), and `mail` and `tagging` take `hosted` only while the keep
    # list names no one (the keep floor). Which hosted model: the one
    # switch `hosted_default` (claude or codex; absent until the owner
    # sets it), with the `hosted` map's per-task overrides over it.
    # Owner processes read <private_dir>/routing.json, which the web chat's
    # Settings screen writes; this file's copy only shapes what the
    # resident shows (see get_routing). `thread_state` (the web chat's
    # conversation states, docs/webchat-state.md) is local by default: with
    # no local model the hosted model judges, one call a tick.
    "routing": {
        "tasks": {"mail": "auto", "tagging": "auto", "drafting": "auto", "digests": "auto",
                  "briefing": "auto", "audit": "auto", "scheduling": "auto", "chat": "auto",
                  "thread_state": "local", "review": "hosted"},
        "hosted": {},
    },
    # The chat floor (model_routing.CHAT_FLOORED): while this is false, the
    # `chat` and `scheduling` tasks never run on the owner's own model,
    # whatever the table, a lane default, an A/B draw, an `@local` tag or a
    # `use local` pin says; they run on the hosted model instead. The local
    # model does not hold up in conversation yet; set true once it does.
    "local_chat_ready": False,
    # Where mail and calendars come from (docs/macos-native-accounts.md).
    # mail.backend: "graph" (Outlook through Microsoft Graph, with any
    # Gmail accounts beside it), "gmail" (Gmail only) or "macos" (the
    # accounts in the Mac's Internet Accounts, through Mail.app).
    # calendar.backend: "graph" (Outlook, plus the Google accounts in
    # scheduling-preferences.json), "google" (Google only) or "macos"
    # (every calendar EventKit sees). availability.provider: whose
    # free/busy the solver reads, apart from the two backends: "graph",
    # "owa" (the outlook-web-auth plugin's token) or "none" (the invitees
    # pick; availability.py). On a Mac the owner-private
    # native-accounts.json outranks these values for owner processes.
    "mail": {"backend": "graph"},
    "calendar": {"backend": "graph"},
    "availability": {"provider": "graph"},
    # Chat platform the bot and the notifier talk to: "slack" (the
    # default), "web" (the bot's own web chat) or "memory" (tests). Owner
    # processes read this block from <private_dir>/chat.json (see
    # get_chat); this file's copy shapes the resident's wording.
    "chat": {
        "frontend": "slack",
        # A second front end that mirrors the first ("web"), or "" for none.
        "shadow": "",
        # The bot's own web chat (docs/webchat.md). Empty pair_token_file and
        # store_path mean the defaults beside the store (chat.load_frontend).
        # backfill_days: the days of the primary's history a shadow web chat
        # copies in when the bot starts; 0 copies none. trust_tailscale_header
        # false turns the Tailscale sign-in off (the pairing cookie only).
        # sched false drops the Scheduling channel the web chat makes when it
        # runs alone (web_sched_on); scheduling requests then wait in the EA
        # channel. notify maps a notifier channel key to a web channel
        # ({"health": {"channel": "<id>"}}); a key it does not name goes to
        # the EA channel (chat_notify).
        "web": {
            "bind": "127.0.0.1",
            "port": 8470,
            "base_path": "/chat",
            "public_url": "",
            "tailscale_login": "",
            "trust_tailscale_header": True,
            "pair_token_file": "",
            "store_path": "",
            "backfill_days": 7,
            "sched": True,
        },
    },
}

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


def load(config_path: Path | None = None) -> dict:
    """Load operational config, merging with defaults.

    Keys starting with '_' are stripped (documentation fields).
    Missing or malformed file returns all defaults.
    """
    path = config_path or CONFIG_FILE
    if path.exists():
        try:
            data = json.loads(path.read_text())
            merged = dict(DEFAULTS)
            merged.update({k: v for k, v in data.items()
                           if not k.startswith("_")})
            return merged
        except (json.JSONDecodeError, OSError):
            pass
    return dict(DEFAULTS)


def is_ea_enabled(config_path: Path | None = None) -> bool:
    """Master switch. When False, cron agents and Slack Claude calls pause."""
    return bool(load(config_path).get("ea_enabled", True))


def is_eval_postcommit_enabled(config_path: Path | None = None) -> bool:
    """Post-commit hook unit test + prompt-only eval toggle."""
    return bool(load(config_path).get("eval_postcommit_enabled", True))


def get_quiet_hours(config_path: Path | None = None) -> dict:
    """Quiet-hours config with all subkeys defaulted (load() does a shallow
    merge, so a partial block in the file would otherwise drop subkeys)."""
    cfg = dict(DEFAULTS["quiet_hours"])
    val = load(config_path).get("quiet_hours")
    if isinstance(val, dict):
        cfg.update(val)
    return cfg


def _owner_trust():
    try:
        import owner_trust
    except ModuleNotFoundError:  # imported as preprocessor.ea_config
        from preprocessor import owner_trust
    return owner_trust


def get_chat(config_path: Path | None = None) -> dict:
    """Chat frontend config with every key defaulted: the top level and
    the web block (a partial block in the file keeps the defaults it does
    not name). A `zulip` key in the file is ignored.

    The block says which server the bot talks to and whose actions count
    as the owner's, and ea-config.json is ea-bot-owned. So with no
    explicit `config_path`, an owner process (owner_trust.is_owner_process)
    takes the block only from `<private_dir>/chat.json`: a missing one, or
    one that fails the owner checks, raises OwnerTrustError (the message
    names the seed step), so no owner process runs a frontend on ea-bot's
    copy. Other processes read ea-config.json, where the block only shapes
    wording."""
    return get_chat_source(config_path)[0]


def get_chat_source(config_path: Path | None = None) -> tuple[dict, bool]:
    """(get_chat's block, True when it came from the owner-private
    chat.json). Only such a block is trusted to say where the bot serves
    the web chat and who signs in to it (docs/webchat.md). The web block's
    `relay` block (docs/webchat-relay.md) holds trust settings too (which
    relay, what a relay-origin device may do): a block read from anywhere
    else loses it."""
    val = _owner_trust().owner_chat_block() if config_path is None else None
    trusted = val is not None
    if val is None:
        val = load(config_path).get("chat")
    cfg = chat_from_block(val)
    if not trusted:
        cfg["web"].pop("relay", None)
    return cfg, trusted


def chat_from_block(val) -> dict:
    """A chat block merged over DEFAULTS["chat"] (see get_chat)."""
    base = DEFAULTS["chat"]
    cfg = {k: v for k, v in base.items() if k != "web"}
    web = dict(base["web"])
    if isinstance(val, dict):
        cfg.update({k: v for k, v in val.items() if k not in ("zulip", "web")})
        wval = val.get("web")
        if isinstance(wval, dict):
            web.update(wval)
    cfg["web"] = web
    return cfg


def web_sched_on(chat: dict | None) -> bool:
    """True when the chat block runs the web chat alone with its
    Scheduling channel: `"frontend": "web"` and the web block's `sched`
    not turned off (false, or "false", "0", "no" or "off"), the rule
    botcore's web_frontend.sched_enabled applies."""
    chat = chat if isinstance(chat, dict) else {}
    if chat.get("frontend") != "web":
        return False
    web = chat.get("web") if isinstance(chat.get("web"), dict) else {}
    val = web.get("sched", True)
    return val is not False and str(val).strip().lower() not in ("false", "0", "no", "off")


def get_models(config_path: Path | None = None) -> dict:
    """Per-lane models with every key defaulted."""
    cfg = dict(DEFAULTS["models"])
    val = load(config_path).get("models")
    if isinstance(val, dict):
        cfg.update({k: v for k, v in val.items() if isinstance(v, str) and v})
    return cfg


def get_efforts(config_path: Path | None = None) -> dict:
    """Per-lane effort levels with every key defaulted. A value that is
    not an effort level is ignored, so a typo cannot break every claude
    call; an empty string means the CLI default for that model."""
    cfg = dict(DEFAULTS["efforts"])
    val = load(config_path).get("efforts")
    if isinstance(val, dict):
        cfg.update({k: v for k, v in val.items()
                    if isinstance(v, str) and v in EFFORT_LEVELS + ("",)})
    return cfg


TIER_NAMES = ("local", "opus", "fable", "codex")


def tiers_from_block(val) -> dict:
    """A tiers block merged over DEFAULTS["tiers"]: unknown tier names,
    lanes and effort levels are dropped, so a typo cannot route a lane
    to nothing."""
    base = DEFAULTS["tiers"]
    cfg = {"default": base["default"], "models": dict(base["models"]),
           "efforts": dict(base["efforts"]), "lanes": {}, "fable_daily_turns": 0}
    if not isinstance(val, dict):
        return cfg
    if val.get("default") in TIER_NAMES:
        cfg["default"] = val["default"]
    for key in ("models", "efforts"):
        sub = val.get(key)
        if isinstance(sub, dict):
            for k, v in sub.items():
                if k in ("opus", "fable") and isinstance(v, str) and (
                        key == "models" or v in EFFORT_LEVELS + ("",)):
                    cfg[key][k] = v
    lanes = val.get("lanes")
    if isinstance(lanes, dict):
        cfg["lanes"] = {k: v for k, v in lanes.items() if isinstance(k, str) and v in TIER_NAMES}
    try:
        cfg["fable_daily_turns"] = max(0, int(val.get("fable_daily_turns") or 0))
    except (TypeError, ValueError):
        pass
    return cfg


def get_tiers(config_path: Path | None = None) -> dict:
    """The tiers block with every key defaulted (see tiers_from_block)."""
    return tiers_from_block(load(config_path).get("tiers"))


def local_chat_ready(config_path: Path | None = None) -> bool:
    """The owner's own model may hold a conversation: false keeps the
    chat floor (model_routing.CHAT_FLOORED) in force."""
    return load(config_path).get("local_chat_ready") is True


ROUTING_TASKS = tuple(DEFAULTS["routing"]["tasks"])
ROUTING_VALUES = ("local", "hosted", "auto")
# The stakes floor: a task that is a judgment call by nature takes only
# this value, whatever a file says.
ROUTING_FLOORS = {"review": "hosted"}
# The hosted models, one entry each. A new hosted model is one entry here
# plus its harness (docs/model-routing.md, "Adding a hosted model"); the
# policy code asks model_routing (is_hosted, is_local, is_base, for_model)
# and never names a model.
#   name     what the owner reads;
#   harness  the program that runs it: `claude` (Claude Code, the bot's
#            own) or `codex` (the Codex CLI); a second model on a harness
#            that exists needs no new code;
#   tier     the chat tier it runs as (model_tiers), and `tiers` every
#            chat tier that is this model;
#   arm      its arm in model_arms;
#   names    the prefixes of its model ids and aliases ("claude-opus-5-5",
#            "sonnet", "codex", "gpt-..."), so a recorded model id finds
#            its entry;
#   tasks    the routing tasks it can take; None for every task.
# The first entry is the base: the bot's own harness runs it, every task
# can run on it, and a hosted model that gives no answer hands the call
# to it. `mail` hosted is the gate off and `drafting` hosted is the
# turn's own model, so neither is in any other entry's tasks: they have
# no choice of their own.
HOSTED_MODELS = {
    "claude": {"name": "Claude", "harness": "claude", "tier": "opus", "tiers": ("opus", "fable"),
               "arm": "claude", "names": ("claude", "opus", "fable", "sonnet", "haiku"),
               "tasks": None},
    "codex": {"name": "Codex", "harness": "codex", "tier": "codex", "tiers": ("codex",),
              "arm": "codex", "names": ("codex", "gpt"),
              "tasks": ("tagging", "digests", "briefing", "audit", "scheduling", "chat",
                        "thread_state", "review")},
}
HOSTED_BASE = next(iter(HOSTED_MODELS))
# Which hosted model a task uses when it runs hosted (by its value or by
# auto): the routing block's `hosted_default` (the one switch), with the
# `hosted` map's per-task overrides over it. A model is a choice for a
# task only where its entry lists the task.
ROUTING_HOSTED = tuple(HOSTED_MODELS)
ROUTING_CODEX_TASKS = HOSTED_MODELS["codex"]["tasks"]


def hosted_takes(model: str, task: str) -> bool:
    """`model` (a HOSTED_MODELS key) can take routing task `task`."""
    tasks = (HOSTED_MODELS.get(model) or {"tasks": ()})["tasks"]
    return tasks is None or task in tasks


def routing_problems(val) -> list[str]:
    """Why a routing block cannot be taken as written: not an object, an
    unknown task, a value that is not local, hosted or auto, or a value
    under a floor. [] for a block that routing_from_block keeps whole."""
    if val is None:
        return []
    if not isinstance(val, dict):
        return ["the routing block is not a JSON object"]
    tasks = val.get("tasks", {})
    if not isinstance(tasks, dict):
        return ["routing.tasks is not a JSON object"]
    out = [f"routing takes its values under tasks, hosted, hosted_default and presets, not {k!r}"
           for k in val if k not in ROUTING_KEYS and not k.startswith("_")]
    if "hosted_default" in val and val["hosted_default"] not in ROUTING_HOSTED:
        out.append(f"hosted_default takes {' or '.join(ROUTING_HOSTED)}, not {val['hosted_default']!r}")
    presets = val.get("presets", {})
    if not isinstance(presets, dict):
        out.append("routing.presets is not a JSON object")
        presets = {}
    for name, p in presets.items():
        why = preset_problems(name, p)
        if why:
            out.append(f"preset {name!r}: {why}")
    hosted = val.get("hosted", {})
    if not isinstance(hosted, dict):
        out.append("routing.hosted is not a JSON object")
        hosted = {}
    for task, value in hosted.items():
        if task not in ROUTING_TASKS:
            out.append(f"no task {task!r} in hosted; the tasks are {', '.join(ROUTING_TASKS)}")
        elif value not in ROUTING_HOSTED:
            out.append(f"hosted {task} takes {' or '.join(ROUTING_HOSTED)}, not {value!r}")
        elif not hosted_takes(value, task):
            out.append(f"{task} has no {HOSTED_MODELS[value]['name']} route; "
                       "its hosted part follows the model that runs it")
    for task, value in tasks.items():
        if task not in ROUTING_TASKS:
            out.append(f"no task {task!r}; the tasks are {', '.join(ROUTING_TASKS)}")
        elif value not in ROUTING_VALUES:
            out.append(f"{task} takes local, hosted or auto, not {value!r}")
        elif task in ROUTING_FLOORS and value not in (ROUTING_FLOORS[task], "auto"):
            out.append(f"{task} is {ROUTING_FLOORS[task]} only (the stakes floor)")
    return out


# The keys a routing block takes: the values per task, the per-task hosted
# overrides, the one hosted switch and the owner's saved presets.
ROUTING_KEYS = ("tasks", "hosted", "hosted_default", "presets")
PRESET_NAME_MAX = 40


def preset_problems(name, p) -> str:
    """Why a saved preset cannot be kept as written, '' when it can: its
    name (1 to PRESET_NAME_MAX letters, digits, spaces, dashes or
    underscores) and its tasks, overrides and switch, each as the table
    takes them."""
    if not isinstance(name, str) or not name.strip() or len(name) > PRESET_NAME_MAX \
            or not all(c.isalnum() or c in " -_" for c in name):
        return "a preset name is 1 to 40 letters, digits, spaces, dashes or underscores"
    if not isinstance(p, dict) or not isinstance(p.get("tasks", {}), dict) \
            or not isinstance(p.get("hosted", {}), dict):
        return "a preset is an object with tasks and hosted objects"
    bad = [t for t, v in p.get("tasks", {}).items() if t not in ROUTING_TASKS or v not in ROUTING_VALUES]
    bad += [t for t, m in p.get("hosted", {}).items()
            if t not in ROUTING_TASKS or m not in ROUTING_HOSTED or not hosted_takes(m, t)]
    if bad:
        return f"it names {', '.join(sorted(set(bad)))} with a value the table does not take"
    if p.get("hosted_default") not in (None, "", *ROUTING_HOSTED):
        return f"its hosted_default takes {' or '.join(ROUTING_HOSTED)}"
    return ""


def routing_from_block(val) -> dict:
    """A routing block merged over DEFAULTS["routing"]: an unknown task or
    value is dropped, and a floored task takes its floor, so no file can
    route a judgment call to the local model. `hosted_default` is kept
    only when the file sets it to a hosted model; absent, no switch is set."""
    tasks = dict(DEFAULTS["routing"]["tasks"])
    given = val.get("tasks") if isinstance(val, dict) else None
    if isinstance(given, dict):
        tasks.update({k: v for k, v in given.items() if k in tasks and v in ROUTING_VALUES})
    tasks.update(ROUTING_FLOORS)
    hosted = val.get("hosted") if isinstance(val, dict) else None
    picked = {k: v for k, v in hosted.items() if k in tasks and v in ROUTING_HOSTED
              and hosted_takes(v, k)} if isinstance(hosted, dict) else {}
    out = {"tasks": tasks, "hosted": picked}
    default = val.get("hosted_default") if isinstance(val, dict) else None
    if default in ROUTING_HOSTED:
        out["hosted_default"] = default
    elif _old_screen_switch(picked):
        # A table from before the switch, saved by the screen that wrote
        # every task's model: the switch it implies, with an override only
        # for each task whose entry differs. The next save writes it in
        # this shape.
        out["hosted_default"] = _old_screen_switch(picked)
        out["hosted"] = {k: v for k, v in picked.items()
                         if k in _choosable() and v != out["hosted_default"]}
    presets = val.get("presets") if isinstance(val, dict) else None
    if isinstance(presets, dict):
        kept = {k: v for k, v in presets.items() if not preset_problems(k, v)}
        if kept:
            out["presets"] = kept
    return out


def _choosable() -> list[str]:
    """The routing tasks that have a choice of hosted model."""
    return [t for t in ROUTING_TASKS if sum(hosted_takes(m, t) for m in ROUTING_HOSTED) > 1]


def _old_screen_switch(picked: dict) -> str:
    """The switch a table from before it implies, when `picked` names a
    model for every task that has a choice (the shape the earlier Settings
    screen saved): the one model they all name, else the `chat` entry's
    model, the one the owner talks to. '' for any other table, whose
    entries stay overrides as written."""
    choosable = _choosable()
    if not choosable or any(t not in picked for t in choosable):
        return ""
    named = {picked[t] for t in choosable}
    return named.pop() if len(named) == 1 else picked.get("chat", "")


def get_routing_source(config_path: Path | None = None) -> tuple[dict, str]:
    """(the routing block with every task defaulted, where it came from).

    The block moves mail between models, and ea-config.json is ea-bot's to
    write. So with no explicit `config_path`, an owner process takes it
    only from `<private_dir>/routing.json` ("routing.json"); with no such
    file every task is at its default ("default"), which is the remote
    layer's switches as they are. A file that fails the owner checks or is
    not a JSON object reads as the defaults too ("refused: <why>"), so a
    lane never stops on it; the doctor's routing row says so. Other
    processes read ea-config.json ("ea-config.json"), where the block only
    shapes what the resident shows."""
    if config_path is None:
        ot = _owner_trust()
        try:
            block = ot.owner_routing_block()
        except ot.OwnerTrustError as e:
            return routing_from_block(None), f"refused: {e}"
        if block is not None:
            return routing_from_block(block), "routing.json"
        if ot.is_owner_process():
            return routing_from_block(None), "default"
    return routing_from_block(load(config_path).get("routing")), "ea-config.json"


def get_routing(config_path: Path | None = None) -> dict:
    """The routing block with every task defaulted and the floors applied
    (see get_routing_source)."""
    return get_routing_source(config_path)[0]


def is_room_reply_check_enabled(config_path: Path | None = None) -> bool:
    """Whether project-room replies get the verify-before-assert pass."""
    return bool(load(config_path).get("room_reply_check", DEFAULTS["room_reply_check"]))


def get_travel_mode(config_path: Path | None = None) -> dict:
    """Travel-mode config with all subkeys defaulted."""
    cfg = dict(DEFAULTS["travel_mode"])
    val = load(config_path).get("travel_mode")
    if isinstance(val, dict):
        cfg.update(val)
    return cfg


def get_log_level(config_path: Path | None = None) -> str:
    """Log level string (DEBUG, INFO, WARNING, ERROR)."""
    level = load(config_path).get("log_level", "INFO")
    if isinstance(level, str) and level.upper() in ("DEBUG", "INFO", "WARNING", "ERROR"):
        return level.upper()
    return "INFO"


CHAT_SESSION_MODES = ("per-topic", "resident")


def get_room_rotate_bytes(config_path: Path | None = None) -> int:
    """The transcript size at which a project room's session starts over
    (`rooms.rotate_bytes`); 0 turns rotation off. A value that is not a
    non-negative integer reads as the default."""
    val = (load(config_path).get("rooms") or {})
    val = val.get("rotate_bytes") if isinstance(val, dict) else None
    if isinstance(val, int) and not isinstance(val, bool) and val >= 0:
        return val
    return DEFAULTS["rooms"]["rotate_bytes"]


def get_chat_sessions(config_path: Path | None = None) -> str:
    """'per-topic' or 'resident'; anything else reads as the default."""
    val = load(config_path).get("chat_sessions")
    return val if val in CHAT_SESSION_MODES else DEFAULTS["chat_sessions"]


def admissions_filter_enabled(config_path: Path | None = None) -> bool:
    """Is the admissions filter on for this install? Only a literal true
    turns it on."""
    val = load(config_path).get("admissions_filter")
    return isinstance(val, dict) and val.get("enabled") is True


def guestless_events_enabled(config_path: Path | None = None) -> bool:
    """Is a guestless Outlook event created with no Create card? Only a
    literal false turns it off."""
    val = load(config_path).get("guestless_events")
    return not (isinstance(val, dict) and val.get("enabled") is False)


def closure_hill_climb_enabled(config_path: Path | None = None) -> bool:
    """Does the closure hill climb move signals? Only a literal false turns
    it off."""
    val = load(config_path).get("closure")
    return not (isinstance(val, dict) and val.get("hill_climb") is False)


def mail_tagger_enabled(config_path: Path | None = None) -> bool:
    """Is the mail tagger on for this install? Only a literal true turns it on."""
    val = load(config_path).get("mail_tagger")
    return isinstance(val, dict) and val.get("enabled") is True


def obligation_split_enabled(config_path: Path | None = None) -> bool:
    """Does the briefing footer split the open rows by list? Only a literal
    true turns it on; off, the footer keeps its one total."""
    val = load(config_path).get("obligation_split")
    return isinstance(val, dict) and val.get("enabled") is True


def mail_tagger_model(config_path: Path | None = None) -> str:
    """The model the tagger calls; a non-string value reads as the default."""
    val = load(config_path).get("mail_tagger")
    model = val.get("model") if isinstance(val, dict) else None
    return model if isinstance(model, str) and model.strip() else DEFAULTS["mail_tagger"]["model"]


BACKEND_CHOICES = {"mail": ("backend", ("graph", "gmail", "macos")),
                   "calendar": ("backend", ("graph", "google", "macos")),
                   "availability": ("provider", ("graph", "owa", "none"))}


def get_backends(config_path: Path | None = None) -> dict:
    """{"mail", "calendar", "availability"}: mail.backend, calendar.backend
    and availability.provider, each its default when absent or unknown.
    native_accounts.backends() lays the owner-private choice over these."""
    cfg = load(config_path)
    out = {}
    for block, (key, choices) in BACKEND_CHOICES.items():
        val = cfg.get(block)
        val = val.get(key) if isinstance(val, dict) else None
        out[block] = val if val in choices else DEFAULTS[block][key]
    return out


UPDATE_CHANNELS = ("auto", "latest", "stable", "off")


def get_update_channel(config_path: Path | None = None) -> str:
    """update.channel as written, or "" when it is absent or not one of
    UPDATE_CHANNELS. "" is not "off": scripts/update.py decides what an
    unset channel means from the code tree (docs/updates.md)."""
    block = load(config_path).get("update")
    val = block.get("channel") if isinstance(block, dict) else None
    return val if val in UPDATE_CHANNELS else ""


def stored_config(config_path: Path | None = None) -> dict:
    """The config file as written, without the defaults; {} when it is
    missing or not a JSON object."""
    path = config_path or CONFIG_FILE
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def resolve_profile(stored: dict) -> tuple[str, dict, bool]:
    """(mode, the switches an old name implies, True when the stored value
    is an old name) for a config as written. No profile, or an unknown one,
    reads as the default mode."""
    val = stored.get("profile")
    if val in PROFILES and (val not in LEGACY_PROFILES
                            or stored.get("profile_version") == PROFILE_VERSION):
        return val, {}, False
    if val in LEGACY_PROFILES:
        mode, implied = LEGACY_PROFILES[val]
        return mode, dict(implied), True
    return DEFAULT_PROFILE, {}, False


def profile_name(name: str) -> tuple[str, dict]:
    """(mode, implied switches) for a name a person types (--profile): a
    mode is itself, and "email" and "review-only" are their modes now."""
    if name in PROFILES:
        return name, {}
    if name in LEGACY_PROFILES:
        mode, implied = LEGACY_PROFILES[name]
        return mode, dict(implied)
    raise ValueError(f"unknown profile {name!r}: {', '.join(PROFILES)}")


def _note_legacy(stored_name: str, mode: str) -> None:
    if stored_name in _LEGACY_LOGGED:
        return
    _LEGACY_LOGGED.add(stored_name)
    _LOG.warning("ea-config.json: profile %r is an old name and reads as %r; "
                 "`python3 preprocessor/ea_config.py normalize-profile` renames it",
                 stored_name, mode)


def get_profile(config_path: Path | None = None) -> str:
    """The install's mode. An old name reads as its mode (one log line per
    process); an unknown value reads as the default."""
    stored = stored_config(config_path)
    mode, _, legacy = resolve_profile(stored)
    if legacy:
        _note_legacy(stored.get("profile"), mode)
    return mode


def drafting_on(stored: dict, implied: dict | None = None) -> bool:
    """False when the config, or the old name it holds, turns drafting off."""
    return not (stored.get("drafting") is False or (implied or {}).get("drafting") is False)


def profile_features(profile: str | None = None, config_path: Path | None = None) -> dict:
    """What a mode turns on, with every key present. `profile` names a mode
    (an old name is mapped); without it, the install's own. The install's
    drafting switch applies either way."""
    stored = stored_config(config_path)
    if profile:
        try:
            mode, implied = profile_name(profile)
        except ValueError:
            mode, implied, _ = resolve_profile(stored)
    else:
        mode, implied, legacy = resolve_profile(stored)
        if legacy:
            _note_legacy(stored.get("profile"), mode)
    feats = dict(PROFILE_FEATURES[mode])
    if feats["drafting"] and not drafting_on(stored, implied):
        feats["drafting"] = False
    return feats


def get_report_schedule(config_path: Path | None = None) -> str:
    """reports.schedule: weekdays, daily or none (on demand); anything else
    reads as weekdays."""
    block = load(config_path).get("reports")
    val = block.get("schedule") if isinstance(block, dict) else None
    return val if val in REPORT_SCHEDULES else DEFAULTS["reports"]["schedule"]


def renamed_config(stored: dict) -> dict | None:
    """The config with an old profile name replaced by its mode, the
    version marker and the switch the old name implied; None when the
    stored name is already a mode."""
    mode, implied, legacy = resolve_profile(stored)
    if not legacy:
        return None
    out = dict(stored)
    out["profile"] = mode
    out["profile_version"] = PROFILE_VERSION
    if implied.get("drafting") is False:
        out["drafting"] = False
    return out


def _write_config(path: Path, data: dict) -> None:
    """Replace ea-config.json atomically. It is resident space, so the write
    goes through resident_io: as the resident when the owner runs this,
    in place when the resident does. Raises OSError when it fails."""
    try:
        import resident_io
    except ModuleNotFoundError:  # imported as preprocessor.ea_config
        from preprocessor import resident_io
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:
        mode = 0o664
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    if not resident_io.replace_text(path, text, mode):
        raise OSError(f"could not write {path}")


def normalize_profile(config_path: Path | None = None, yes: bool = False,
                      ask=None) -> int:
    """Rename an old profile name in ea-config.json, with the user's yes:
    `yes`, or `ask(question)` returning True. 0 when renamed or nothing to
    rename, 3 when the rename was declined or could not be asked."""
    path = config_path or CONFIG_FILE
    stored = stored_config(path)
    new = renamed_config(stored)
    if new is None:
        print(f"profile: {get_profile(path)} (nothing to rename)")
        return 0
    old, mode = stored.get("profile"), new["profile"]
    lines = [f"This install's profile is stored under its old name `{old}`.",
             f"It already works as `{mode}`: {PROFILE_SUMMARY[mode]}"]
    if new.get("drafting") is False and stored.get("drafting") is not False:
        lines.append("The rename also records `\"drafting\": false`, which `review-only` meant.")
    print("\n".join(lines))
    if not yes:
        if ask is None:
            if not sys.stdin.isatty():
                print("Nothing changed. Run again with --yes to rename it.")
                return 3
            ask = lambda q: input(q).strip().lower() in ("y", "yes")  # noqa: E731
        if not ask(f"Rename it to `{mode}` in {path}? [y/N] "):
            print("Nothing changed.")
            return 3
    _write_config(path, new)
    print(f"Renamed: profile `{mode}`.")
    return 0


def set_profile(name: str, config_path: Path | None = None) -> int:
    """Record a mode (an old name maps to its mode) with the version
    marker. Between the two chat modes this is the whole switch: the bot
    reads the mode on each room request. A switch to or from reviewer also
    changes the cron lines and the bot service, so it names setup.sh."""
    path = config_path or CONFIG_FILE
    try:
        mode, implied = profile_name(name)
    except ValueError as e:
        print(f"ea_config.py: {e}", file=sys.stderr)
        return 2
    stored = stored_config(path)
    before = resolve_profile(stored)[0]
    out = dict(stored)
    out["profile"] = mode
    out["profile_version"] = PROFILE_VERSION
    if implied.get("drafting") is False:
        out["drafting"] = False
    if out != stored:
        _write_config(path, out)
    print(f"profile: {mode}. {PROFILE_SUMMARY[mode]}")
    if PROFILE_FEATURES[before]["slack"] != PROFILE_FEATURES[mode]["slack"]:
        print(f"The chat comes or goes with this change: run "
              f"./scripts/install/setup.sh --profile {mode} --install-cron, then "
              f"python3 scripts/ea-doctor.py --profile {mode}.")
    return 0


def _main() -> int:
    import argparse
    # allow_abbrev=False: a shortened flag is an error, not a silent match.
    ap = argparse.ArgumentParser(description="operational config reader", allow_abbrev=False)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("profile", allow_abbrev=False,
                   help="print the install's mode (an old name reads as its mode)")
    p = sub.add_parser("features", allow_abbrev=False,
                       help="print the mode's feature flags as JSON")
    p.add_argument("--profile", default=None, choices=[*PROFILES, *LEGACY_PROFILES])
    p = sub.add_parser("feature", allow_abbrev=False, help="exit 0 when the named feature is on")
    # "chat" is the platform-neutral name for the "slack" feature.
    p.add_argument("name", choices=sorted([*PROFILE_FEATURES["collaborator"], "chat"]))
    sub.add_parser("reports", allow_abbrev=False,
                   help="print reports.schedule (weekdays, daily or none)")
    p = sub.add_parser("normalize-profile", allow_abbrev=False,
                       help="rename a profile name from before the three modes to its "
                            "mode, after a yes")
    p.add_argument("--yes", action="store_true", help="rename without asking")
    p = sub.add_parser("set-profile", allow_abbrev=False, help="record a mode in ea-config.json")
    p.add_argument("name", choices=[*PROFILES, *LEGACY_PROFILES])
    a = ap.parse_args()
    if a.cmd == "profile":
        print(get_profile())
        return 0
    if a.cmd == "features":
        print(json.dumps(profile_features(a.profile)))
        return 0
    if a.cmd == "reports":
        print(get_report_schedule())
        return 0
    if a.cmd == "normalize-profile":
        return normalize_profile(yes=a.yes)
    if a.cmd == "set-profile":
        return set_profile(a.name)
    name = "slack" if a.name == "chat" else a.name
    return 0 if profile_features()[name] else 1


if __name__ == "__main__":
    raise SystemExit(_main())

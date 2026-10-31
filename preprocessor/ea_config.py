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
import os
from pathlib import Path

EA_DIR = Path(os.environ.get("EA_DIR", Path.home() / ".ea"))
CONFIG_FILE = EA_DIR / "config" / "ea-config.json"

# Install profiles. "assistant" is the full system; "email" is the EA
# without a chat platform (drafts and briefings in files; the user sends
# from Outlook, and with no bot to read a staged event there are no
# holds); "review-only" is "email" without drafting.
PROFILES = ("email", "review-only", "assistant")
PROFILE_FEATURES = {
    "email":       {"slack": False, "buttons": False, "drafting": True,  "resident": False, "rooms": False, "plugins": False},
    "review-only": {"slack": False, "buttons": False, "drafting": False, "resident": False, "rooms": False, "plugins": False},
    "assistant":   {"slack": True,  "buttons": True,  "drafting": True,  "resident": True,  "rooms": True,  "plugins": True},
}

DEFAULTS = {
    "profile": "assistant",
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
    # is Claude, `auto` leaves the task where the remote layer's switches
    # and the tiers block put it. Two floors in code sit under every value:
    # `review` is hosted only (the stakes floor), and `mail` and `tagging`
    # take `hosted` only while the keep list names no one (the keep floor).
    # Owner processes read <private_dir>/routing.json, which the web chat's
    # Settings screen writes; this file's copy only shapes what the
    # resident shows (see get_routing).
    "routing": {
        "tasks": {"mail": "auto", "tagging": "auto", "drafting": "auto", "digests": "auto",
                  "briefing": "auto", "audit": "auto", "scheduling": "auto", "chat": "auto",
                  "review": "hosted"},
    },
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
    # default), "zulip", "web" (the bot's own web chat) or "memory"
    # (tests). The zulip block names the server, the bot, the owner and
    # the private streams; notify maps each notifier channel key to a
    # stream; each post opens its own topic unless the entry pins one with
    # "topic". The Zulip API key is a secret (ZULIP_API_KEY,
    # owner_trust.owner_secret), never config. Owner processes read this
    # block from <private_dir>/chat.json (see get_chat); this file's copy
    # shapes the resident's wording.
    "chat": {
        "frontend": "slack",
        # A second front end that mirrors the first ("web"), or "" for none.
        "shadow": "",
        "zulip": {
            "site": "",
            "bot_email": "",
            "owner_user_id": "",
            "stream": "ea",
            "sched_stream": "",
            "dm_stream": "ea-dm",
            "notify": {
                "ea-assistant": {"stream": "ea"},
                "scheduling": {"stream": "ea"},
                "personal": {"stream": "ea"},
            },
        },
        # The bot's own web chat (docs/webchat.md). Empty pair_token_file and
        # store_path mean the defaults beside the store (chat.load_frontend).
        # backfill_days: the days of the primary's history a shadow web chat
        # copies in when the bot starts; 0 copies none. trust_tailscale_header
        # false turns the Tailscale sign-in off (the pairing cookie only).
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
    """Chat frontend config with every key defaulted: the top level, the
    zulip block, each notify entry and the web block (a partial block in
    the file keeps the defaults it does not name).

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
    zbase = base["zulip"]
    cfg = {k: v for k, v in base.items() if k not in ("zulip", "web")}
    zulip = {k: v for k, v in zbase.items() if k != "notify"}
    notify = {k: dict(v) for k, v in zbase["notify"].items()}
    web = dict(base["web"])
    if isinstance(val, dict):
        cfg.update({k: v for k, v in val.items() if k not in ("zulip", "web")})
        wval = val.get("web")
        if isinstance(wval, dict):
            web.update(wval)
        zval = val.get("zulip")
        if isinstance(zval, dict):
            zulip.update({k: v for k, v in zval.items() if k != "notify"})
            nval = zval.get("notify")
            if isinstance(nval, dict):
                for key, entry in nval.items():
                    if isinstance(entry, dict):
                        notify[key] = {**notify.get(key, {}), **entry}
    zulip["notify"] = notify
    cfg["zulip"] = zulip
    cfg["web"] = web
    return cfg


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


ROUTING_TASKS = tuple(DEFAULTS["routing"]["tasks"])
ROUTING_VALUES = ("local", "hosted", "auto")
# The stakes floor: a task that is a judgment call by nature takes only
# this value, whatever a file says.
ROUTING_FLOORS = {"review": "hosted"}


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
    out = [f"routing takes its values under tasks, not {k!r}" for k in val
           if k != "tasks" and not k.startswith("_")]
    for task, value in tasks.items():
        if task not in ROUTING_TASKS:
            out.append(f"no task {task!r}; the tasks are {', '.join(ROUTING_TASKS)}")
        elif value not in ROUTING_VALUES:
            out.append(f"{task} takes local, hosted or auto, not {value!r}")
        elif task in ROUTING_FLOORS and value not in (ROUTING_FLOORS[task], "auto"):
            out.append(f"{task} is {ROUTING_FLOORS[task]} only (the stakes floor)")
    return out


def routing_from_block(val) -> dict:
    """A routing block merged over DEFAULTS["routing"]: an unknown task or
    value is dropped, and a floored task takes its floor, so no file can
    route a judgment call to the local model."""
    tasks = dict(DEFAULTS["routing"]["tasks"])
    given = val.get("tasks") if isinstance(val, dict) else None
    if isinstance(given, dict):
        tasks.update({k: v for k, v in given.items() if k in tasks and v in ROUTING_VALUES})
    tasks.update(ROUTING_FLOORS)
    return {"tasks": tasks}


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


def get_profile(config_path: Path | None = None) -> str:
    """The install profile; an unknown value reads as the default."""
    val = load(config_path).get("profile")
    return val if val in PROFILES else DEFAULTS["profile"]


def profile_features(profile: str | None = None, config_path: Path | None = None) -> dict:
    """What a profile turns on, with every key present."""
    return dict(PROFILE_FEATURES[profile if profile in PROFILES else get_profile(config_path)])


def _main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="operational config reader")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("profile", help="print the install profile")
    p = sub.add_parser("features", help="print the profile's feature flags as JSON")
    p.add_argument("--profile", default=None, choices=PROFILES)
    p = sub.add_parser("feature", help="exit 0 when the named feature is on")
    # "chat" is the platform-neutral name for the "slack" feature.
    p.add_argument("name", choices=sorted([*PROFILE_FEATURES["assistant"], "chat"]))
    a = ap.parse_args()
    if a.cmd == "profile":
        print(get_profile())
        return 0
    if a.cmd == "features":
        print(json.dumps(profile_features(a.profile)))
        return 0
    name = "slack" if a.name == "chat" else a.name
    return 0 if profile_features()[name] else 1


if __name__ == "__main__":
    raise SystemExit(_main())

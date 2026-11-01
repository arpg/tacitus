#!/usr/bin/env python3
"""
Usage tips: one more way to use the EA, suggested once a week.

Most people use the EA as a chat box: they ask, it answers. Much of what
it does (rooms, the phone setup, standing rules) they never need, and
some they would use if they knew it was there. This module notices what
the person has not used yet and, once a week, suggests one thing in plain
words with one line to try. The order is the order a new person meets
things; a tip whose feature the person already uses is never suggested.

The catalogue is code (CATALOGUE): each tip has an id, a one-line pitch,
an example line to try, a short why, a detector (`uses`) that says whether
the person already uses it, and `ready`, which holds a tip back until it
fits (the room tip waits for 20 conversations; the model settings wait for
a local model; the phone tips wait for the web chat). The detectors read
data the bot already keeps and never write it (Usage): the web chat's
store, the draft journal, the ledger, the preferences store, the snooze
file, the uploads, the room registry and the routing table.

Cadence (`post`, called by the morning briefing's hourly weekday cron
tick in run-agent.sh): the first tip 7 days after the install's first
briefing (the onboarding week comes first), then at most one tip per 7
days, only on a weekday morning (07:00 to 12:00 local) after that day's
briefing posted, never on a day the briefing carried an onboarding
question, and never in quiet hours. Posting a tip closes the one before
it: one tip is open at a time.

The owner answers in the tip's own thread, and the chat bot handles the
words itself, with no model call (ea-chat-bot.py, _handle_usage_tip_reply):
  more      the next tip now, in the same thread
  not now   no tips for 14 days; this tip comes back after that, twice at most
  never     no more tips: a rule in the preferences store (kind
  stop      instruction, scope briefing, id usage-tips-stop) that outranks
            this code; retiring the rule turns the tips back on
Any other message in the thread is a normal turn.

The post goes through chat_notify (the EA channel key, so Zulip, Slack and
the web chat all work), each tip its own thread, and on the web chat a
push notification at normal urgency (chat.direct.web_notify): a post in the
EA channel is not pushed on its own.

State is `~/.ea/state/usage-tips.json`, written through resident_io:
    {"version": 1, "first_briefing": "YYYY-MM-DD", "open": "<id>",
     "paused_until": "<iso>", "shown": {"<id>": "<iso>"},
     "answered": {"<id>": {"at": "<iso>", "reply": "more|not now|never"}},
     "dismissed": {"<id>": {"at": "<iso>", "why": "..."}},
     "not_now": {"<id>": {"until": "<iso>", "count": n}},
     "never": false, "never_at": "", "never_rule": ""}
`usage_tips.enabled` in ea-config.json (default true) switches them off.

Usage:
    usage_tips.py next [--json]      the tip that would post, and whether it may now (dry run)
    usage_tips.py post [--force]     post it when the cadence allows (--force: now)
    usage_tips.py reply --word <more|not now|never> [--id <tip>]
    usage_tips.py status [--json]
    usage_tips.py list [--json]      the catalogue in order, with what is known of each tip
    usage_tips.py reset              forget every tip shown and answered
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

try:
    import ea_config as _ea_config
    import owner_trust as _owner_trust
    import preferences as _prefs
    import resident_io as _rio
except ModuleNotFoundError:  # imported as preprocessor.<mod>
    from preprocessor import ea_config as _ea_config
    from preprocessor import owner_trust as _owner_trust
    from preprocessor import preferences as _prefs
    from preprocessor import resident_io as _rio

FIRST_AFTER = timedelta(days=7)    # the onboarding week comes first
TIP_GAP = timedelta(days=7)        # one tip a week
NOT_NOW = timedelta(days=14)       # "not now" pauses the tips this long
NOT_NOW_LIMIT = 2                  # a tip put off this often is not asked again
MORNING = (7, 12)                  # local hours a tip may post: [start, end)
ROOM_AFTER = 20                    # conversations before the room tip fits
CHANNEL_KEY = "ea-assistant"       # chat_notify's key for the EA channel
PUSH_TAG = "usage-tip"
SOURCE = "usage-tips"
STOP_RULE_ID = "usage-tips-stop"
STOP_RULE_TEXT = ("No usage tips: do not post the weekly suggestion of something new to try "
                  "with the assistant.")


def _ea_dir(ea_dir=None) -> Path:
    return Path(ea_dir) if ea_dir else Path(os.environ.get("EA_DIR", Path.home() / ".ea"))


def state_file(ea_dir=None) -> Path:
    return _ea_dir(ea_dir) / "state" / "usage-tips.json"


def prefs_file(ea_dir=None) -> Path:
    return _ea_dir(ea_dir) / "config" / "preferences.json"


def config_file(ea_dir=None) -> Path:
    return _ea_dir(ea_dir) / "config" / "ea-config.json"


def _read_json(path: Path):
    """A JSON file in the resident's space, read with its access; None when
    it is missing or not JSON."""
    text = _rio.read_text(path)
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _ledger():
    """ledger.py, whose guarded_connect is the owner-safe way to open a
    SQLite file (read-only here)."""
    try:
        import ledger
    except ModuleNotFoundError:
        from preprocessor import ledger
    return ledger


def _is_owner() -> bool:
    try:
        return bool(_owner_trust.is_owner_process())
    except Exception:
        return False


# ---------------------------------------------------------------------------
# What the person already uses (read-only)
# ---------------------------------------------------------------------------
# Words in the owner's own messages that show a habit. Lower case; matched
# on word boundaries.
SAID = {
    "draft": r"\b(draft|reply|respond|write back|write to|email back|answer (him|her|them))\b",
    "day": (r"\b(tomorrow|tonight|my day|my week|this week|next week|calendar|agenda"
            r"|what'?s on|what is on)\b"),
    "snooze": r"\bsnooz",
    "meeting": (r"\b(meet|meeting|find (a |some )?time|set up a call|book a call|catch up"
                r"|zoom call)\b"),
    "poll": r"\b(poll|doodle|when2meet|whenisgood|rallly|timeful)\b",
}
_SAID_RE = {k: re.compile(v, re.I) for k, v in SAID.items()}
_CHAT_RUN_RE = re.compile(r"^[0-9a-f]+-[0-9a-f]{6}$")


class Usage:
    """What the person already does, read once from the stores the bot
    keeps. Every read is read-only and fails soft: a store that is missing
    or unreadable reads as "not yet". The web chat's store and the routing
    table are owner-private, so a process that is not the owner's sees
    neither (and the tips that need them read as not yet).

    `private_dir`, `chat_cfg` and `local_model` default to what this
    process can see; tests pass them."""

    def __init__(self, ea_dir=None, *, private_dir=None, chat_cfg: dict | None = None,
                 local_model: bool | None = None):
        self.ea_dir = _ea_dir(ea_dir)
        if private_dir is not None:
            self.private_dir = Path(private_dir)
        else:
            self.private_dir = _owner_trust.private_dir() if _is_owner() else None
        self._chat = chat_cfg
        self._local = local_model
        self._web: dict | None = None
        self._texts: list[str] | None = None

    # -- configuration -----------------------------------------------------
    @property
    def chat(self) -> dict:
        if self._chat is None:
            try:
                self._chat = _ea_config.get_chat()
            except Exception:
                self._chat = {}
        return self._chat if isinstance(self._chat, dict) else {}

    @property
    def web_cfg(self) -> dict:
        web = self.chat.get("web")
        return web if isinstance(web, dict) else {}

    @property
    def web_on(self) -> bool:
        """The web chat runs, alone or beside Zulip."""
        return "web" in (self.chat.get("frontend"), self.chat.get("shadow"))

    @property
    def relay_on(self) -> bool:
        """A relay is set up, so a phone can pair to use the chat off the
        owner's own network."""
        return bool(self.web_on and self.web_cfg.get("relay"))

    @property
    def local_model(self) -> bool:
        """The owner's own model is set up (remote-llm.json names an
        endpoint and a model; read only in an owner process)."""
        if self._local is None:
            try:
                try:
                    import remote_llm
                except ModuleNotFoundError:
                    from preprocessor import remote_llm
                cfg = remote_llm.config()
                self._local = bool(cfg.get("endpoint") and cfg.get("model"))
            except Exception:
                self._local = False
        return bool(self._local)

    @property
    def web_store(self) -> Path:
        if self.web_cfg.get("store_path"):
            return Path(str(self.web_cfg["store_path"]))
        if self.private_dir is not None:
            return self.private_dir / "webchat.sqlite"
        return self.ea_dir / "state" / "webchat.sqlite"

    # -- the web chat's store ----------------------------------------------
    def web(self) -> dict:
        """Counts from the web chat's store, opened read-only; zeros when
        there is none."""
        if self._web is not None:
            return self._web
        out = {"texts": [], "threads": 0, "files": 0, "rooms": 0, "push": 0,
               "owner_states": 0, "card_kinds": []}
        path = self.web_store
        conn = None
        try:
            if not path.is_file() or path.is_symlink():
                raise FileNotFoundError(path)
            conn = _ledger().guarded_connect(path, timeout=5, read_only=True)
            q = conn.execute
            out["texts"] = [str(r[0] or "") for r in q(
                "SELECT text FROM messages WHERE author='owner' AND kind='message' "
                "ORDER BY seq DESC LIMIT 3000")]
            out["threads"] = int(q(
                "SELECT COUNT(*) FROM (SELECT MIN(seq) AS s FROM messages GROUP BY channel, "
                "thread_key) t JOIN messages m ON m.seq = t.s WHERE m.author='owner'"
            ).fetchone()[0])
            out["files"] = int(q("SELECT COUNT(*) FROM messages WHERE author='owner' AND "
                                 "file IS NOT NULL AND file != ''").fetchone()[0])
            out["rooms"] = int(q("SELECT COUNT(*) FROM channels WHERE kind='room'").fetchone()[0])
            out["push"] = int(q("SELECT COUNT(*) FROM push_subscriptions").fetchone()[0])
            out["owner_states"] = int(q("SELECT COUNT(*) FROM thread_state WHERE by='owner'")
                                      .fetchone()[0])
            kinds = []
            for (card,) in q("SELECT card FROM messages WHERE kind='status' AND card IS NOT NULL"):
                try:
                    kinds.append(str((json.loads(card) or {}).get("kind") or ""))
                except (ValueError, AttributeError):
                    continue
            out["card_kinds"] = kinds
        except Exception:
            pass
        finally:
            if conn is not None:
                conn.close()
        self._web = out
        return out

    def _beside_store(self, name: str) -> Path:
        return self.web_store.parent / name

    def relay_devices(self) -> int:
        """Phones the owner approved on the relay (the relay's key file;
        only the device list is read)."""
        relay = self.web_cfg.get("relay")
        key = relay.get("key_file") if isinstance(relay, dict) else None
        path = Path(str(key)) if key else self._beside_store("webchat-relay.json")
        try:
            if not path.is_file() or path.is_symlink():
                return 0
            devices = json.loads(path.read_text()).get("devices") or []
        except Exception:
            return 0
        return sum(1 for d in devices if isinstance(d, dict) and not d.get("pending"))

    def state_overrides(self) -> int:
        """Conversations the owner marked done (or back) by hand: the
        override rows, and the labels file the web chat writes beside its
        store for each one."""
        n = self.web()["owner_states"]
        path = self._beside_store("thread-state-labels.jsonl")
        try:
            if path.is_file() and not path.is_symlink() and path.stat().st_size > 0:
                n += 1
        except OSError:
            pass
        return n

    # -- the resident's files ------------------------------------------------
    def registry(self) -> dict:
        data = _read_json(self.ea_dir / "state" / "slack-sessions.json")
        return data if isinstance(data, dict) else {}

    def texts(self) -> list[str]:
        """The owner's own words: their messages in the web chat, and the
        title (first line) of each conversation in the session registry."""
        if self._texts is None:
            titles = []
            for row in self.registry().values():
                meta = row.get("meta") if isinstance(row, dict) else None
                if isinstance(meta, dict) and meta.get("title"):
                    titles.append(str(meta["title"]))
            self._texts = self.web()["texts"] + titles
        return self._texts

    def said(self, key: str) -> bool:
        pattern = _SAID_RE[key]
        return any(pattern.search(t) for t in self.texts())

    def conversations(self) -> int:
        """Conversations the owner has had: the web chat's threads the
        owner started, or the session registry's rows, whichever is more."""
        return max(self.web()["threads"], len(self.registry()))

    def chat_drafts(self) -> int:
        """Drafts made in a chat turn (the draft journal's `created` records
        whose run id is a chat dispatch's, not a cron lane's)."""
        text = _rio.read_text(self.ea_dir / "state" / "draft-events.jsonl") or ""
        n = 0
        for line in text.splitlines():
            if '"created"' not in line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("action") == "created" and _CHAT_RUN_RE.match(str(ev.get("run_id") or "")):
                n += 1
        return n

    def snoozes(self) -> int:
        """Snoozed emails: the snooze file's records and the ledger's
        snoozed rows."""
        data = _read_json(self.ea_dir / "state" / "snoozed-messages.json")
        n = len(data) if isinstance(data, list) else 0
        conn = None
        try:
            db = self.ea_dir / "state" / "obligations-ledger.db"
            if db.exists():
                conn = _ledger().guarded_connect(db, read_only=True)
                n += int(conn.execute(
                    "SELECT COUNT(*) FROM ledger WHERE status='snoozed' OR "
                    "(snooze_until IS NOT NULL AND snooze_until != '')").fetchone()[0])
        except Exception:
            pass
        finally:
            if conn is not None:
                conn.close()
        return n

    def own_rules(self) -> int:
        """Rules the person stated in chat (not the onboarding answers, the
        migration, or this module's own stop rule)."""
        data = _prefs.load(prefs_file(self.ea_dir))
        return sum(1 for r in data.get("rules", [])
                   if r.get("status", "active") == "active"
                   and _prefs.source_label(r.get("source")) == "chat")

    def uploads(self) -> int:
        """Files the owner attached to a message: the web chat's file
        messages, and the downloads the bot keeps for Zulip and Slack."""
        n = self.web()["files"]
        rows = _rio.list_dir(self.ea_dir / "uploads") or []
        return n + sum(1 for kind, _name in rows if kind == "f")

    def rooms(self) -> int:
        """Rooms the person has: the web chat's rooms, and the room
        registry's rows."""
        data = _read_json(self.ea_dir / "config" / "ea-channels.json")
        n = len(data) if isinstance(data, dict) else 0
        return self.web()["rooms"] + n

    def routing_set(self) -> bool:
        """The owner saved the routing table at least once (owner-private)."""
        if self.private_dir is None:
            return False
        try:
            if (self.private_dir / "routing.json").is_file():
                return True
            log = self.private_dir / "remote-llm" / "routing-changes.jsonl"
            return log.is_file() and log.stat().st_size > 0
        except OSError:
            return False


# ---------------------------------------------------------------------------
# The catalogue
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Tip:
    id: str
    pitch: str        # one line, the first line of the post
    example: str      # one line to try
    why: str          # one or two short sentences
    uses: Callable[[Usage], bool]
    ready: Callable[[Usage], bool] = field(default=lambda u: True)
    needs: str = ""   # when the tip fits, in words, for `list`


CATALOGUE: list[Tip] = [
    Tip("reply-draft",
        "I can write a reply to an email for you.",
        "Write a reply to Sam and say Tuesday works.",
        "You read it first and press Send. Nothing goes out without you.",
        uses=lambda u: u.chat_drafts() > 0 or u.said("draft")),
    Tip("tomorrow",
        "Ask me what your day looks like.",
        "What is on tomorrow?",
        "I check your calendar and your email and tell you what matters.",
        uses=lambda u: u.said("day")),
    Tip("snooze",
        "I can hide an email until the day you want it back.",
        "Snooze the email from the parking office until Monday.",
        "It leaves your inbox now and comes back that morning.",
        uses=lambda u: u.snoozes() > 0 or u.said("snooze")),
    Tip("standing-rule",
        "Tell me a rule once and I keep to it.",
        "Never show me emails from the parking office.",
        "I write it down in your words and follow it every day.",
        uses=lambda u: u.own_rules() > 0),
    Tip("phone-alerts",
        "Your phone can buzz when something needs you.",
        "Open the menu and switch on Notifications.",
        "On an iPhone, first tap Share, then Add to Home Screen, and open me from there.",
        uses=lambda u: u.web()["push"] > 0,
        ready=lambda u: u.web_on,
        needs="the web chat"),
    Tip("away-from-home",
        "You can use me on your phone away from your home or office network.",
        "Open the menu and tap Pair a phone.",
        "Then I work on mobile data and on any Wi-Fi, not only at your desk.",
        uses=lambda u: u.relay_devices() > 0,
        ready=lambda u: u.relay_on,
        needs="the web chat with a relay set up"),
    Tip("meeting-time",
        "I can find a meeting time that works for you.",
        "Find 30 minutes with Alex next week.",
        "I check your calendar and write the email with two or three times for you to approve.",
        uses=lambda u: u.said("meeting") or "event" in u.web()["card_kinds"]),
    Tip("poll",
        "I can fill in a scheduling poll from an email.",
        "Fill in the Doodle poll Jordan sent.",
        "I pick the times you are free and show you before I fill it in.",
        uses=lambda u: u.said("poll")),
    Tip("attach-file",
        "You can add a file or a photo to a message and ask me about it.",
        "Add the PDF, then type: Fill in this form for me.",
        "I read it and do the work: forms, receipts, letters.",
        uses=lambda u: u.uploads() > 0),
    Tip("needs-you",
        "Tap Mark done when something is finished.",
        "Open Needs you, pick one you finished, and tap Mark done.",
        "The list then shows only what still waits on you.",
        uses=lambda u: u.state_overrides() > 0,
        ready=lambda u: u.web_on,
        needs="the web chat"),
    Tip("project-room",
        "Give a big project its own room.",
        "Open a room for the spring fundraiser.",
        "Everything about it stays in one place, and I remember it there.",
        uses=lambda u: u.rooms() > 0,
        ready=lambda u: u.conversations() >= ROOM_AFTER,
        needs=f"{ROOM_AFTER} conversations or more"),
    Tip("model-settings",
        "You can choose which jobs run on your own AI model.",
        "Open the menu and tap Settings.",
        "Private mail can stay on your own machine, and harder jobs go to Claude.",
        uses=lambda u: u.routing_set(),
        ready=lambda u: u.local_model and u.web_on,
        needs="a local model and the web chat"),
]

BY_ID = {t.id: t for t in CATALOGUE}

REPLY_HINT = "Reply *more* for another idea, *not now*, or *never*."
REPLIES = {
    "not now": "OK, not now. I will check in again in two weeks.",
    "never": "OK, no more tips. I saved that as a rule: no usage tips. "
             "If you want them back one day, just tell me.",
    "never unsaved": "OK, no more tips. If you want them back one day, just tell me.",
    "none left": "That is every tip I have for now. Ask me anything, any time.",
}


def post_text(tip: Tip) -> str:
    """The tip as one short chat message: the pitch, the line to try, the
    why, and the three words the person can answer with."""
    return "\n".join((f":bulb: {tip.pitch}", f"Try: *{tip.example}*", tip.why, REPLY_HINT))


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
def _empty_state() -> dict:
    return {"version": 1, "first_briefing": "", "open": "", "paused_until": "",
            "shown": {}, "answered": {}, "dismissed": {}, "not_now": {},
            "never": False, "never_at": "", "never_rule": ""}


def _parse_state(text: str) -> dict:
    data = _empty_state()
    try:
        raw = json.loads(text) if text.strip() else {}
    except ValueError:
        return data
    if not isinstance(raw, dict):
        return data
    for k in ("shown", "answered", "dismissed", "not_now"):
        if isinstance(raw.get(k), dict):
            data[k] = raw[k]
    for k in ("first_briefing", "open", "paused_until", "never_at", "never_rule"):
        if isinstance(raw.get(k), str):
            data[k] = raw[k]
    data["never"] = bool(raw.get("never", False))
    return data


def load_state(ea_dir=None) -> dict:
    return _parse_state(_rio.read_text(state_file(ea_dir)) or "")


def _update_state(mutator, ea_dir=None):
    """Read-modify-write under resident_io's lock; `mutator(state)` edits in
    place and its return value comes back."""
    box: dict = {}

    def fn(text: str) -> str:
        state = _parse_state(text)
        box["result"] = mutator(state)
        return json.dumps(state, indent=2) + "\n"

    if not _rio.update_text(state_file(ea_dir), fn):
        raise OSError(f"usage_tips: could not update {state_file(ea_dir)}")
    return box.get("result")


def reset(ea_dir=None) -> dict:
    def wipe(s):
        s.clear()
        s.update(_empty_state())
    _update_state(wipe, ea_dir)
    return {"reset": True, "file": str(state_file(ea_dir))}


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------
def _quiet_hours():
    try:
        import quiet_hours
    except ModuleNotFoundError:
        from preprocessor import quiet_hours
    return quiet_hours


def local_now() -> datetime:
    """Now, in the person's current timezone (quiet_hours.active_timezone)."""
    try:
        return datetime.now(ZoneInfo(_quiet_hours().active_timezone()))
    except Exception:
        return datetime.now().astimezone()


def _stamp(now: datetime) -> str:
    return now.replace(microsecond=0).isoformat()


def _when(value: str, now: datetime) -> datetime | None:
    """An ISO time from the state, in `now`'s zone; None when unparsable."""
    try:
        t = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=now.tzinfo)
    return t.astimezone(now.tzinfo) if now.tzinfo else t


def _day(value: str) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# The cadence
# ---------------------------------------------------------------------------
def enabled(ea_dir=None) -> bool:
    """`usage_tips.enabled` in ea-config.json; missing means on."""
    block = _ea_config.load(config_file(ea_dir)).get("usage_tips")
    return bool(block.get("enabled", True)) if isinstance(block, dict) else True


def chat_profile(ea_dir=None) -> bool:
    """The install profile has a chat (the tips need one to post in)."""
    try:
        return bool(_ea_config.profile_features(config_path=config_file(ea_dir)).get("slack"))
    except Exception:
        return False


def stop_rule(ea_dir=None) -> dict | None:
    """The active rule in the preferences store that stops the tips."""
    for r in _prefs.load(prefs_file(ea_dir)).get("rules", []):
        if str(r.get("id", "")).startswith(STOP_RULE_ID) and r.get("status", "active") == "active":
            return r
    return None


def stopped(state: dict, ea_dir=None) -> bool:
    """The person said never. The rule in the preferences store decides: an
    active one stops the tips, and retiring it turns them back on. A stop
    the store never recorded (its write failed) holds on its own."""
    if stop_rule(ea_dir):
        return True
    return bool(state.get("never")) and not state.get("never_rule")


def briefing_posted_on(day: str, ea_dir=None) -> bool:
    """The morning briefing posted on local day `day` (run-agent.sh's
    post-success marker)."""
    data = _read_json(_ea_dir(ea_dir) / "state" / "agent-post-success.json")
    row = data.get("morning-briefing") if isinstance(data, dict) else None
    return isinstance(row, dict) and str(row.get("date") or "") == day


def _onboarding():
    try:
        import onboarding
    except ModuleNotFoundError:
        from preprocessor import onboarding
    return onboarding


def onboarding_asked_on(day: str, ea_dir=None) -> bool:
    """The briefing of local day `day` carried an onboarding question
    (answered or not): one question a morning is enough."""
    try:
        asked = _onboarding().load_state(ea_dir).get("asked", {})
        return any(str(at)[:10] == day for at in asked.values())
    except Exception:
        return False


def first_briefing(state: dict, ea_dir=None) -> str:
    """The install's first briefing day: the one the state recorded, else
    the earliest day onboarding asked a question or a briefing posted."""
    if _day(state.get("first_briefing", "")):
        return state["first_briefing"][:10]
    days = []
    try:
        days += [str(v)[:10] for v in _onboarding().load_state(ea_dir).get("asked", {}).values()]
    except Exception:
        pass
    data = _read_json(_ea_dir(ea_dir) / "state" / "agent-post-success.json")
    row = data.get("morning-briefing") if isinstance(data, dict) else None
    if isinstance(row, dict):
        days.append(str(row.get("date") or "")[:10])
    days = sorted(d for d in days if _day(d))
    return days[0] if days else ""


def clock_facts(now: datetime, ea_dir=None) -> dict:
    """What the cadence reads from the install, beyond the tips state."""
    day = now.date().isoformat()
    try:
        quiet = bool(_quiet_hours().should_defer("normal", now))
    except Exception:
        quiet = False
    return {"enabled": enabled(ea_dir), "chat": chat_profile(ea_dir),
            "briefing_today": briefing_posted_on(day, ea_dir),
            "onboarding_today": onboarding_asked_on(day, ea_dir),
            "quiet": quiet}


def last_shown(state: dict, now: datetime) -> datetime | None:
    times = [t for t in (_when(v, now) for v in state.get("shown", {}).values()) if t]
    return max(times) if times else None


def blocked(state: dict, now: datetime, *, facts: dict, is_stopped: bool,
            first: str, force: bool = False) -> str:
    """'' when a tip may post now, else why not. `force` skips the
    cadence (the day, the hour, the gaps) but never the switch, the
    profile or the person's never."""
    if not facts.get("enabled", True):
        return "usage_tips.enabled is false in ea-config.json"
    if not facts.get("chat", True):
        return "this install profile has no chat"
    if is_stopped:
        return f"the owner said never (rule {STOP_RULE_ID} in the preferences store)"
    if force:
        return ""
    first_day = _day(first)
    if first_day is None:
        return "no briefing has posted yet"
    if now.date() < first_day + FIRST_AFTER:
        return (f"the first tip comes on {(first_day + FIRST_AFTER).isoformat()}: "
                "the first week is for the onboarding questions")
    paused = _when(state.get("paused_until", ""), now)
    if paused and now < paused:
        return f"not now: paused until {_stamp(paused)}"
    last = last_shown(state, now)
    if last and now < last + TIP_GAP:
        return f"one tip a week: the last went out {_stamp(last)}"
    if now.weekday() >= 5:
        return "tips go out on weekdays"
    if not (MORNING[0] <= now.hour < MORNING[1]):
        return f"tips go out between {MORNING[0]:02d}:00 and {MORNING[1]:02d}:00 local"
    if not facts.get("briefing_today"):
        return "today's morning briefing has not posted yet"
    if facts.get("onboarding_today"):
        return "today's briefing carried an onboarding question"
    if facts.get("quiet"):
        return "quiet hours"
    return ""


def tip_status(tip: Tip, state: dict, usage: Usage, now: datetime) -> str:
    """Where one tip stands: dismissed, put off, waiting to fit, used
    already, or queued."""
    if tip.id in state.get("dismissed", {}):
        return f"done ({state['dismissed'][tip.id].get('why', '')})"
    nn = state.get("not_now", {}).get(tip.id) or {}
    until = _when(nn.get("until", ""), now)
    if until and now < until:
        return f"not now until {_stamp(until)}"
    try:
        if not tip.ready(usage):
            return f"waits for {tip.needs or 'its moment'}"
        if tip.uses(usage):
            return "used already"
    except Exception as e:
        return f"unknown ({type(e).__name__})"
    if state.get("open") == tip.id:
        return "open"
    return "queued"


def pick(state: dict, usage: Usage, now: datetime,
         catalogue: list[Tip] | None = None) -> Tip | None:
    """The first tip in catalogue order that is not done, not put off, fits
    now, and is for something the person does not use yet. The open tip is
    never picked again."""
    for tip in (CATALOGUE if catalogue is None else catalogue):
        if tip.id == state.get("open"):
            continue
        if tip_status(tip, state, usage, now) == "queued":
            return tip
    return None


def mark_shown(tip_id: str, now: datetime, ea_dir=None) -> None:
    """Record a tip as posted. The tip open before it, unanswered, is done:
    one tip is open at a time."""
    stamp = _stamp(now)

    def mark(s):
        prev = s.get("open")
        if prev and prev != tip_id and prev not in s["dismissed"]:
            s["dismissed"][prev] = {"at": stamp, "why": "no answer"}
        s["shown"][tip_id] = stamp
        s["open"] = tip_id
    _update_state(mark, ea_dir)


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------
def _chat_post(text: str, bypass_quiet: bool = False) -> bool:
    try:
        import chat_notify
    except ModuleNotFoundError:
        from preprocessor import chat_notify
    return bool(chat_notify.post(CHANNEL_KEY, text, _bypass_quiet=bypass_quiet))


def _web_push(text: str) -> dict:
    """A push to the web chat's phones; {"web": "off"} without a web chat.
    Never raises."""
    try:
        try:
            from chat import direct
        except ModuleNotFoundError:
            from preprocessor.chat import direct
        return direct.web_notify(text, tag=PUSH_TAG, urgency="normal")
    except Exception as e:
        return {"web": "error", "why": f"{type(e).__name__}: {e}"}


def next_tip(*, ea_dir=None, now: datetime | None = None, force: bool = False,
             usage: Usage | None = None, facts: dict | None = None) -> dict:
    """The tip that would post now and whether the cadence allows it. Writes
    nothing."""
    now = now or local_now()
    state = load_state(ea_dir)
    first = first_briefing(state, ea_dir)
    facts = clock_facts(now, ea_dir) if facts is None else facts
    reason = blocked(state, now, facts=facts, is_stopped=stopped(state, ea_dir),
                     first=first, force=force)
    tip = pick(state, usage or Usage(ea_dir), now)
    out = {"tip": tip.id if tip else None, "may_post": not reason and tip is not None,
           "why_not": reason or ("" if tip else "no tip left to suggest"),
           "first_briefing": first}
    if tip:
        out["text"] = post_text(tip)
    return out


def post(*, ea_dir=None, now: datetime | None = None, force: bool = False,
         usage: Usage | None = None, facts: dict | None = None,
         notify: Callable[[str], bool] | None = None,
         push: Callable[[str], dict] | None = None) -> dict:
    """Post the next tip when the cadence allows: in the EA channel through
    chat_notify, its own thread, then a web push. The state records the
    install's first briefing day the first time one is seen."""
    now = now or local_now()
    state = load_state(ea_dir)
    first = first_briefing(state, ea_dir)
    if first and not state.get("first_briefing"):
        _update_state(lambda s: s.__setitem__("first_briefing", s["first_briefing"] or first),
                      ea_dir)
    facts = clock_facts(now, ea_dir) if facts is None else facts
    reason = blocked(state, now, facts=facts, is_stopped=stopped(state, ea_dir),
                     first=first, force=force)
    if reason:
        return {"posted": False, "why": reason}
    tip = pick(state, usage or Usage(ea_dir), now)
    if tip is None:
        return {"posted": False, "why": "no tip left to suggest"}
    text = post_text(tip)
    sent = notify(text) if notify else _chat_post(text, bypass_quiet=force)
    if not sent:
        return {"posted": False, "why": "the chat post failed", "tip": tip.id}
    mark_shown(tip.id, now, ea_dir)
    pushed = push(tip.pitch) if push else _web_push(tip.pitch)
    return {"posted": True, "tip": tip.id, "text": text, "push": pushed}


# ---------------------------------------------------------------------------
# The owner's answer in a tip's thread
# ---------------------------------------------------------------------------
_WORDS = {
    "more": ("more", "another", "next", "one more", "more please", "another one",
             "more tips", "next tip"),
    "not now": ("not now", "later", "not today", "maybe later", "not now thanks",
                "not now thank you"),
    "never": ("never", "stop", "no more", "no more tips", "stop tips", "never again",
              "stop please", "never thanks"),
}
_WORD_OF = {phrase: word for word, phrases in _WORDS.items() for phrase in phrases}


def reply_word(text: str) -> str | None:
    """`more`, `not now` or `never` when the whole message is one of the
    tip words (any case, end marks ignored), else None."""
    norm = " ".join(re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()).split())
    if len(norm) > 24:
        return None
    return _WORD_OF.get(norm)


def _plain(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).split())


def tip_of_text(text: str) -> str | None:
    """The id of the tip whose pitch the text carries, else None."""
    plain = _plain(text)
    for tip in CATALOGUE:
        if _plain(tip.pitch) in plain:
            return tip.id
    return None


def tip_in_thread(messages: list[tuple[str, bool]]) -> str | None:
    """The tip a thread is about, from its messages oldest first as (text,
    from the bot): the first message must be the bot's tip, and the answer
    goes to the last tip the bot posted in it (`more` adds one). None for
    any other thread."""
    if not messages:
        return None
    text, from_bot = messages[0]
    if not from_bot or not tip_of_text(text):
        return None
    found = None
    for text, from_bot in messages:
        if from_bot:
            found = tip_of_text(text) or found
    return found


def _record_stop(quote: str, now: datetime, ea_dir=None) -> str:
    """The never rule in the preferences store, kind instruction and scope
    briefing, so it outranks this code; its id, or '' when the write
    failed. An active one is kept as it is."""
    held = stop_rule(ea_dir)
    if held:
        return str(held.get("id"))
    words = " ".join((quote or "never").split())[:200]

    def add(data: dict) -> dict:
        rule = _prefs.add_rule(data, kind="instruction", scopes=["briefing"],
                               text=STOP_RULE_TEXT, quote=words, source="chat",
                               rule_id=STOP_RULE_ID, date_str=now.date().isoformat())
        # preferences.py validates against its own source list; the stored
        # source names where this rule came from.
        rule["source"] = SOURCE
        return rule
    try:
        return str(_prefs.update(add, prefs_file(ea_dir))["id"])
    except Exception as e:
        print(f"usage_tips: the never rule was not recorded: {e}", file=sys.stderr)
        return ""


def handle_reply(tip_id: str, word: str, *, quote: str = "", ea_dir=None,
                 now: datetime | None = None, usage: Usage | None = None) -> dict:
    """Act on the owner's word in a tip's thread. Returns {"text": the reply
    to post in that thread, "word", "tip", "next": the tip posted for
    `more` or None}."""
    word = reply_word(word) or word
    if word not in _WORDS:
        raise ValueError(f"word is one of {tuple(_WORDS)}, got {word!r}")
    if tip_id not in BY_ID:
        raise KeyError(f"no usage tip with id {tip_id!r}")
    now = now or local_now()
    stamp = _stamp(now)
    out = {"word": word, "tip": tip_id, "next": None}

    def answered(s):
        s["answered"][tip_id] = {"at": stamp, "reply": word}
        if s.get("open") == tip_id:
            s["open"] = ""

    if word == "not now":
        until = _stamp(now + NOT_NOW)

        def later(s):
            answered(s)
            nn = s["not_now"].get(tip_id) or {}
            count = int(nn.get("count") or 0) + 1
            s["not_now"][tip_id] = {"until": until, "count": count}
            if count >= NOT_NOW_LIMIT:
                s["dismissed"][tip_id] = {"at": stamp, "why": "not now twice"}
            s["paused_until"] = until
        _update_state(later, ea_dir)
        out["text"] = REPLIES["not now"]
        return out

    if word == "never":
        rule_id = _record_stop(quote, now, ea_dir)

        def stop(s):
            answered(s)
            s["never"], s["never_at"], s["never_rule"] = True, stamp, rule_id
        _update_state(stop, ea_dir)
        out["rule"] = rule_id
        out["text"] = REPLIES["never" if rule_id else "never unsaved"]
        return out

    def moved_on(s):
        answered(s)
        s["dismissed"][tip_id] = {"at": stamp, "why": "more"}
    _update_state(moved_on, ea_dir)
    nxt = pick(load_state(ea_dir), usage or Usage(ea_dir), now)
    if nxt is None:
        out["text"] = REPLIES["none left"]
        return out
    mark_shown(nxt.id, now, ea_dir)
    out["next"] = nxt.id
    out["text"] = post_text(nxt)
    return out


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------
def status(*, ea_dir=None, now: datetime | None = None, usage: Usage | None = None) -> dict:
    now = now or local_now()
    state = load_state(ea_dir)
    usage = usage or Usage(ea_dir)
    first = first_briefing(state, ea_dir)
    first_day = _day(first)
    return {
        "enabled": enabled(ea_dir),
        "stopped": stopped(state, ea_dir),
        "stop_rule": (stop_rule(ea_dir) or {}).get("id", ""),
        "first_briefing": first,
        "first_tip_from": (first_day + FIRST_AFTER).isoformat() if first_day else "",
        "open": state.get("open", ""),
        "paused_until": state.get("paused_until", ""),
        "tips": [{"id": t.id, "status": tip_status(t, state, usage, now),
                  "shown": state["shown"].get(t.id, "")} for t in CATALOGUE],
        "next": next_tip(ea_dir=ea_dir, now=now, usage=usage),
        "file": str(state_file(ea_dir)),
    }


def status_text(st: dict) -> str:
    if not st["enabled"]:
        head = "Usage tips are off (usage_tips.enabled in ea-config.json)."
    elif st["stopped"]:
        rule = st["stop_rule"]
        head = ("Usage tips are stopped: the owner said never"
                + (f" (retire rule {rule} with preferences.py to turn them back on)." if rule
                   else "."))
    else:
        head = "Usage tips are on."
    lines = [head,
             f"first briefing: {st['first_briefing'] or 'none yet'}"
             + (f"; first tip from {st['first_tip_from']}" if st["first_tip_from"] else ""),
             f"open: {st['open'] or 'none'}"]
    if st["paused_until"]:
        lines.append(f"paused until: {st['paused_until']}")
    nxt = st["next"]
    lines.append(f"next: {nxt['tip'] or 'none'}; "
                 + ("may post now" if nxt["may_post"] else f"not now ({nxt['why_not']})"))
    lines += [f"  {t['id']}: {t['status']}" + (f" (shown {t['shown']})" if t["shown"] else "")
              for t in st["tips"]]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _cmd_next(args) -> int:
    res = next_tip(force=args.force)
    if args.json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
    else:
        print(f"next: {res['tip'] or 'none'}; "
              + ("may post now" if res["may_post"] else f"not now ({res['why_not']})"))
        if res.get("text"):
            print("\n" + res["text"])
    return 0


def _cmd_post(args) -> int:
    now = local_now()
    print(json.dumps({"at": _stamp(now), **post(now=now, force=args.force)}, ensure_ascii=False))
    return 0


def _cmd_reply(args) -> int:
    tip_id = args.id or load_state().get("open") or ""
    if not tip_id:
        print(json.dumps({"error": "no tip is open; pass --id"}))
        return 2
    try:
        res = handle_reply(tip_id, args.word, quote=args.word)
    except (KeyError, ValueError) as e:
        print(json.dumps({"error": str(e)}))
        return 2
    print(json.dumps(res, ensure_ascii=False))
    return 0


def _cmd_status(args) -> int:
    st = status()
    if args.json:
        print(json.dumps(st, indent=2, ensure_ascii=False))
    else:
        sys.stdout.write(status_text(st))
    return 0


def _cmd_list(args) -> int:
    usage, state, now = Usage(), load_state(), local_now()
    rows = [{"id": t.id, "pitch": t.pitch, "example": t.example, "why": t.why,
             "needs": t.needs, "status": tip_status(t, state, usage, now)} for t in CATALOGUE]
    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0
    for i, r in enumerate(rows, 1):
        print(f"{i:2d}. {r['id']} [{r['status']}]\n    {r['pitch']}\n    Try: {r['example']}"
              f"\n    {r['why']}" + (f"\n    Fits once there is {r['needs']}." if r["needs"] else ""))
    return 0


def _cmd_reset(args) -> int:
    print(json.dumps(reset()))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Weekly usage tips", allow_abbrev=False)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("next", help="the tip that would post now (dry run)", allow_abbrev=False)
    p.add_argument("--json", action="store_true")
    p.add_argument("--force", action="store_true", help="ignore the cadence")
    p = sub.add_parser("post", help="post the tip when the cadence allows", allow_abbrev=False)
    p.add_argument("--force", action="store_true", help="post now, whatever the cadence")
    p = sub.add_parser("reply", help="the owner's answer to a tip", allow_abbrev=False)
    p.add_argument("--word", required=True, help="more, not now or never")
    p.add_argument("--id", help="the tip (default: the open one)")
    p = sub.add_parser("status", help="where the tips stand", allow_abbrev=False)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("list", help="the catalogue in order", allow_abbrev=False)
    p.add_argument("--json", action="store_true")
    sub.add_parser("reset", help="forget every tip shown and answered", allow_abbrev=False)
    args = ap.parse_args(argv)
    return {"next": _cmd_next, "post": _cmd_post, "reply": _cmd_reply, "status": _cmd_status,
            "list": _cmd_list, "reset": _cmd_reset}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())

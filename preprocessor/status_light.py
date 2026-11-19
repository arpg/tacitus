"""The web chat's status light: is everything working right now.

The owner's bot (an owner process) answers that question from the state
the system already keeps, with no probe of its own. Each source below
reads one record and gives conditions; a condition has an id, a level
(`warn` or `problem`), a few words for the light's line, one plain
sentence, what the owner can do (with an action the app can offer, where
one exists), since when, and a technical detail and the log lines of its
component for the drill-down. The overall level is the worst condition's,
and `ok` with none. docs/webchat.md, Status light, has the table.

The sources:

  sentinel    the sentinel's results.json (every 10 minutes): the bot,
              the briefings, the schedule, the Claude and Outlook
              sign-ins, the mail mediator; and its own staleness
  breakers    circuit-breakers.json: Outlook not answering (inside the
              sentinel's 30-minute grace a warning, past it a problem),
              another service down
  boundary    the boundary audit's state (daily): a protection that does
              not hold, or a check to look at
  plugins     plugin-integrity-alerts.json, less what this process found
              passing since: an add-on switched off
  notices     the alerts the owner must read (chat_notify, a notice kind),
              kept in the web chat's store until marked read
  usage       the plan's saved rate-limit reading (rate_limits.py)
  local       the owner's own model (model_routing.local_status)
  relay       the relay client, when one runs
  update      the updater's status (update_status.doctor_row)
  drive       the Drive accounts user-profile.json lists with no sign-in

Noise never colours the light: the crontab-versus-master drift, the
resident pane (chat turns do not use it), a tuning or tool model's own
breaker, an update channel set in the wrong file or a tree not yet laid
out for updates (the doctor reports those).

Trust (docs/SECURITY.md, Owner trust rules). Every file the sources read
in the owner's private space is checked as an owner file. What comes from
the resident's space (the breakers, user-profile.json) adds a condition in
the fixed words here, with only checked fields (a known service name, a
count, a time, a fixed kind of error) in its detail, never its text. A
notice's text is the poster's own report, shown escaped in the drill-down.

The light's memory (when each condition began, what pushed, what cleared)
lives in the web chat's store (`light_memory`), so a restart keeps it. A
condition that turns red and may interrupt (`push`) sends one notification
that opens the status sheet; a second one waits at least PUSH_AGAIN_S, and
the notification clears once every condition that pushed has cleared.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

try:
    import log_redact
    import owner_trust
except ModuleNotFoundError:  # imported as preprocessor.status_light
    from preprocessor import log_redact, owner_trust

RANK = {"ok": 0, "warn": 1, "problem": 2}
# The sentinel runs every 10 minutes; results older than this mean it stopped.
SENTINEL_STALE_S = 40 * 60
# A Graph breaker inside the sentinel's grace is a warning (sentinel.GRAPH_OPEN_GRACE_S).
GRAPH_GRACE_S = 30 * 60
GRAPH_BREAKERS = ("graph", "graph-background")
# Readings and outages that must last this long before the light shows them.
LOCAL_GRACE_S = 10 * 60
RELAY_GRACE_S = 10 * 60
# A plan reading older than this says nothing about now.
USAGE_FRESH_S = 12 * 3600
USAGE_WARN_PERCENT = 90.0
# Recently cleared: what recovered in the last day, newest first.
CLEARED_KEEP_S = 24 * 3600
CLEARED_MAX = 20
# A condition that pushed does not push again for this long.
PUSH_AGAIN_S = 6 * 3600
# A summary asked for within this long of the last read is the cached one.
SUMMARY_FRESH_S = 10.0
# The drill-down's bounds: the newest lines of the window, each cut, all capped.
LOG_LINES = 20
LOG_LINE_CHARS = 300
LOG_TOTAL_CHARS = 6000
LOG_WINDOW_S = 3600
LOG_READ_BYTES = 256 * 1024
DETAIL_CHARS = 4000

_NAME_RE = re.compile(r"^[a-z0-9_-]{1,40}$")
_PLUGIN_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_PRINTABLE_RE = re.compile(r"[^\x20-\x7e]")

# Who can help, said the same way everywhere.
HELPER = "the person who set up Tacitus"


@dataclass
class Condition:
    """One thing that is not right now."""
    id: str
    level: str
    title: str
    line: str
    todo: str = ""
    component: str = ""
    detail: str = ""
    since: float | None = None
    until: float | None = None
    push: bool = False
    action: dict | None = None
    notice: int | None = None
    grace_s: float = 0.0

    def public(self) -> dict:
        out = {"id": self.id, "level": self.level, "title": self.title, "line": self.line,
               "todo": self.todo, "since": self.since, "read": self.notice is not None}
        if self.until:
            out["until"] = self.until
        if self.action:
            out["action"] = dict(self.action)
        return out


@dataclass
class Ctx:
    """Where the sources read, and now. Ctx.live() is the running bot's."""
    now: float
    private: Path
    logs: Path
    sentinel_dir: Path
    boundary_file: Path
    ea: Path
    live: Path
    store: Any = None
    relay: Any = None
    # {plugin: [problem]} for the installed plugins that fail their check.
    plugins_checked: Callable[[], dict] | None = None

    @classmethod
    def live_ctx(cls, store=None, relay=None) -> Ctx:
        try:
            from log_paths import owner_log_dir
        except ModuleNotFoundError:
            from preprocessor.log_paths import owner_log_dir
        home = Path.home()
        return cls(now=time.time(), private=owner_trust.private_dir(), logs=owner_log_dir(),
                   sentinel_dir=home / ".local" / "state" / "ea-sentinel",
                   boundary_file=home / ".local" / "state" / "ea-boundary-audit" / "state.json",
                   ea=owner_trust.ea_dir(),
                   live=Path(os.environ.get("EA_LIVE_DIR") or "/opt/ea-system"),
                   store=store, relay=relay, plugins_checked=_plugins_checked)


# ── reading ──────────────────────────────────────────────────────────

def _owner_json(path: Path) -> dict | None:
    """An owner file's JSON object: a regular file this user owns that no
    one else can write, one link, no link at its name. None when it is
    missing; OwnerTrustError or ValueError when it fails the checks or
    does not parse, which the source reports as a check it could not read."""
    try:
        raw = owner_trust.read_owner_file(path, check_dir=False)
    except FileNotFoundError:
        return None
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} is not a JSON object")
    return data


def _ts(value) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if value > 0 else None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()


def _iso(ts: float | None) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if ts else "?"


def _plain(text, limit: int = 200) -> str:
    """Text from a record, on one line, printable ASCII, cut."""
    return _PRINTABLE_RE.sub("?", " ".join(str(text or "").split()))[:limit]


# ── the sentinel ─────────────────────────────────────────────────────
# Each check's words: (level, title, line, todo, push). A check the
# sentinel adds later gets GENERIC_CHECK until it has its own.

SENTINEL_WORDS = {
    "service_active": ("problem", "Tacitus stopped", "Tacitus stopped running on its computer.",
                       f"Start it again on its computer, or ask {HELPER}. The details say how.", False),
    "service_enabled": ("warn", "Start at boot is off",
                        "Tacitus will not start by itself after its computer restarts.",
                        f"Ask {HELPER} to turn on its start at boot. The details say how.", False),
    "no_duplicate_bot": ("warn", "A second copy runs", "A second copy of Tacitus is running.",
                         f"Ask {HELPER} to stop the extra copy. The details say which.", False),
    "briefing_posted": ("problem", "No briefing today", "This morning's briefing did not arrive.",
                        "Ask Tacitus for a briefing if you need one now. It tries again tomorrow.",
                        False),
    "afternoon_posted": ("warn", "No afternoon update", "This afternoon's update did not arrive.",
                         "Nothing to do. It comes again tomorrow.", False),
    "weekly_audit": ("warn", "No weekly review", "The weekly review did not run this week.",
                     "Nothing to do now. It runs again on Sunday.", False),
    "cron_heartbeat": ("problem", "Scheduled work stopped",
                       "Tacitus's scheduled work stopped: briefings and mail checks do not run.",
                       f"Ask {HELPER} to check its schedule. The details say what to look at.", True),
    "recent_failures": ("warn", "A scheduled job failed", "A scheduled job failed in the last day.",
                        "Nothing to do unless it keeps happening.", False),
    "deferred_queue": ("warn", "Held messages are stuck",
                       "Messages held for your quiet hours did not go out.",
                       f"Ask {HELPER} to check the quiet-hours delivery.", False),
    "deploy_integrity": ("warn", "Running an older copy",
                         "Tacitus is running an older copy of itself than the one installed.",
                         f"Ask {HELPER} to restart it. The details say how.", False),
    "ea_bot_login": ("problem", "Claude sign-in needs you",
                     "Tacitus is signed out of Claude, so it cannot answer or write.",
                     f"Sign it in to Claude again on its computer, or ask {HELPER}. "
                     "The details say how.", True),
    "mail_sign_in": ("problem", "Mail sign-in needs you",
                     "Tacitus is signed out of Outlook, so it cannot read or send mail.",
                     f"Sign in to Outlook again on Tacitus's computer, or ask {HELPER}. "
                     "The details say how.", True),
    "mail_mediator": ("problem", "Mail helper stopped",
                      "The helper that reaches your mail stopped, so mail reads and drafts fail.",
                      f"Restart it on Tacitus's computer, or ask {HELPER}. The details say how.",
                      True),
}
GENERIC_CHECK = ("warn", "A check failed", "One of Tacitus's own checks failed.",
                 f"Ask {HELPER} to look at the details.", False)
# Never on the light: drift between the installed schedule and its saved
# copy (the symptom checks see what it breaks), the resident pane (chat
# turns do not run there), and the breakers (the breaker source reads them).
SENTINEL_NOISE = frozenset({"crontab_drift", "resident_pane", "circuit_breakers"})
# The deploy check when the program files are not protected: a problem.
_UNSAFE_TREE = ("problem", "Program files unprotected",
                "Tacitus's program files are not protected from its helper account.",
                f"Ask {HELPER} to run the hardening step. The details say how.", True)


def _sentinel_words(check: str, detail: str) -> tuple:
    if check == "deploy_integrity" and "UNSAFE" in detail:
        return _UNSAFE_TREE
    return SENTINEL_WORDS.get(check, GENERIC_CHECK)


def sentinel_results(ctx: Ctx) -> dict | None:
    return _owner_json(ctx.sentinel_dir / "results.json")


def src_sentinel(ctx: Ctx) -> list[Condition]:
    doc = sentinel_results(ctx)
    if doc is None:
        return []
    checked = _ts(doc.get("checked"))
    out = []
    if checked is None or ctx.now - checked > SENTINEL_STALE_S:
        out.append(Condition("sentinel", "warn", "Checks stopped",
                             "Tacitus's own checks stopped running, so this light may be out of date.",
                             f"Ask {HELPER} to check its schedule.", "sentinel",
                             f"The sentinel's last run: {_iso(checked)} (it runs every 10 minutes).",
                             since=checked))
    since = doc.get("since") if isinstance(doc.get("since"), dict) else {}
    for r in doc.get("results") or []:
        if not isinstance(r, dict) or r.get("ok") is not False:
            continue
        check = str(r.get("check") or "")
        if not _NAME_RE.match(check) or check in SENTINEL_NOISE:
            continue
        detail = str(r.get("detail") or "")[:DETAIL_CHARS]
        level, title, line, todo, push = _sentinel_words(check, detail)
        out.append(Condition(f"sentinel:{check}", level, title, line, todo, "sentinel",
                             f"Sentinel check {check}, last run {_iso(checked)}:\n{detail}",
                             since=_ts(since.get(check)), push=push))
    return out


def sentinel_cleared(ctx: Ctx) -> list[dict]:
    """The sentinel's own recoveries in the last day, as cleared rows: they
    explain a red light the bot did not see (it was down, say)."""
    try:
        doc = sentinel_results(ctx) or {}
    except Exception:
        return []
    out = []
    for c in doc.get("cleared") or []:
        if not isinstance(c, dict):
            continue
        check, until = str(c.get("check") or ""), _ts(c.get("until"))
        if not _NAME_RE.match(check) or check in SENTINEL_NOISE or not until \
                or ctx.now - until > CLEARED_KEEP_S:
            continue
        level, title, line, _todo, _push = _sentinel_words(check, "")
        out.append({"id": f"sentinel:{check}", "level": level, "title": title, "line": line,
                    "since": _ts(c.get("since")), "until": until, "how": "fixed"})
    return out


# ── the circuit breakers (resident space: fixed words, checked fields) ─

def _error_kind(error) -> str:
    text = str(error or "")
    if re.search(r"\b429\b", text):
        return "too many requests (HTTP 429)"
    if re.search(r"\bHTTP 5\d\d\b", text):
        return "server error (HTTP 5xx)"
    if "timeout" in text.lower() or "timed out" in text.lower():
        return "no answer in time"
    return "another error"


def src_breakers(ctx: Ctx) -> list[Condition]:
    try:
        import retry_policy
    except ModuleNotFoundError:
        from preprocessor import retry_policy
    graph, others = [], []
    for b in retry_policy.open_breakers():
        name = str(b.get("service") or "")
        if name in GRAPH_BREAKERS:
            graph.append(b)
        elif _NAME_RE.match(name) and not name.startswith("remote-llm"):
            others.append(b)
    out = []
    if graph:
        began = min((_ts(b.get("since")) or _ts(b.get("opened_at")) or ctx.now) for b in graph)
        kinds = {_error_kind(b.get("last_error")) for b in graph}
        throttled = kinds == {"too many requests (HTTP 429)"}
        long = ctx.now - began >= GRAPH_GRACE_S
        title = "Outlook is limiting Tacitus" if throttled else "Outlook is not answering"
        line = ("Microsoft is limiting how often Tacitus reads your mail." if throttled
                else "Outlook is not answering, so mail is not read for now.")
        detail = "\n".join(
            f"Breaker {b['service']}: open since {_iso(_ts(b.get('since')) or _ts(b.get('opened_at')))}, "
            f"{int(b.get('failures') or 0)} failures in a row, {int(b.get('opens') or 0)} openings; "
            f"last error: {_error_kind(b.get('last_error'))}" for b in graph)
        out.append(Condition("mail-service", "problem" if long else "warn", title, line,
                             "Nothing to do. Tacitus keeps trying, and the light turns green when "
                             "Outlook answers again.", "graph", detail, since=began))
    for b in others:
        out.append(Condition(f"service:{b['service']}", "warn", "A service is not answering",
                             "One of the services Tacitus uses is not answering. Tacitus keeps trying.",
                             "Nothing to do unless it lasts.", "",
                             f"Breaker {b['service']}: open since {_iso(_ts(b.get('opened_at')))}, "
                             f"{int(b.get('failures') or 0)} failures in a row; last error: "
                             f"{_error_kind(b.get('last_error'))}",
                             since=_ts(b.get("opened_at"))))
    return out


# ── the boundary audit ───────────────────────────────────────────────

def src_boundary(ctx: Ctx) -> list[Condition]:
    doc = _owner_json(ctx.boundary_file)
    if not doc:
        return []
    rows = []
    for entry in doc.get("fingerprint") or []:
        name, _, rest = str(entry).partition("|")
        status = rest.partition("|")[0]
        if status in ("FAIL", "WARN") and name:
            rows.append((status, _plain(name, 120)))
    if not rows:
        return []
    failed = any(s == "FAIL" for s, _n in rows)
    detail = (f"The boundary audit (scripts/boundary-audit.py), checked {_iso(_ts(doc.get('checked')))}:\n"
              + "\n".join(f"{s} {n}" for s, n in sorted(rows))
              + "\nIts table with each fix is in the owner's boundary-audit.log.")
    if failed:
        return [Condition("boundary", "problem", "A safety check failed",
                          "A daily safety check found a protection that does not hold.",
                          f"Ask {HELPER} to read the safety check. The details name what it found.",
                          "boundary", detail, push=True)]
    return [Condition("boundary", "warn", "A safety check needs a look",
                      "A daily safety check found something to look at.",
                      f"Ask {HELPER} to read the safety check. The details name what it found.",
                      "boundary", detail)]


# ── the plugins' integrity ───────────────────────────────────────────

def _plugins_checked() -> dict:
    """{plugin: [problem]} for the installed plugins whose files do not
    match the lock, by the check the bot runs before each turn
    (plugin_registry.verified_plugins, cached on each tree's signature)."""
    try:
        import plugin_registry
    except ModuleNotFoundError:
        from preprocessor import plugin_registry
    plugin_registry.verified_plugins()
    return plugin_registry.integrity_failures()


def src_plugins(ctx: Ctx) -> list[Condition]:
    """The plugins switched off for a change in their files. The record a
    post leaves (plugin-integrity-alerts.json) forgets a plugin only when
    the process that checks it finds it passing, so it can outlive the
    problem; the bot's own check of every installed plugin is the answer
    when it runs, and the record only when it cannot."""
    failing: set = set()
    try:
        if ctx.plugins_checked is None:
            raise LookupError("no check here")
        failing = set(ctx.plugins_checked())
    except Exception:
        record = _owner_json(ctx.private / "plugin-integrity-alerts.json") or {}
        failing = {n for n in record if isinstance(n, str)}
    out = []
    for name in sorted(failing):
        if not _PLUGIN_RE.match(name):
            continue
        out.append(Condition(
            f"plugin:{name}", "problem", "An add-on is switched off",
            "An add-on's files changed, so Tacitus switched it off.",
            f"If you did not change it, ask {HELPER} to check it. The details say how.", "",
            f"Plugin {name}: its files no longer match what the owner installed, so it gets no "
            f"skill, tool grant or scheduled run until they do.\n"
            f"Check it: python3 scripts/ea-plugin.py verify {name}\n"
            f"Restore it, or accept the change: python3 scripts/ea-plugin.py update {name}"))
    return out


# ── notices: the alerts the owner must read ──────────────────────────
# (level, title, line, todo, push) per kind (chat_notify.LIGHT_NOTICE_KINDS).

NOTICE_WORDS = {
    "tripwire": ("problem", "Private mail check failed",
                 "Mail kept for your own model could not be read safely.",
                 "Read the report in the details. Tacitus changes nothing by itself. "
                 "Tap Got it when you have read it.", True),
    "persistence": ("warn", "A setting changed",
                    "Tacitus's helper changed one of the files that guide its later work.",
                    f"Read the change in the details. If you did not expect it, ask {HELPER}. "
                    "Tap Got it when you have read it.", False),
    "outbound": ("problem", "A web safety check failed",
                 "A check on how Tacitus uses the web found a protection that did not hold.",
                 f"Read the report in the details and show it to {HELPER}. "
                 "Tap Got it when you have read it.", True),
}
GENERIC_NOTICE = ("warn", "A report to read", "Tacitus has a report for you to read.",
                  "Read it in the details. Tap Got it when you have read it.", False)


def _notice_words(kind: str) -> tuple:
    return NOTICE_WORDS.get(kind, GENERIC_NOTICE)


def src_notices(ctx: Ctx) -> list[Condition]:
    if ctx.store is None:
        return []
    out = []
    for n in ctx.store.light_notices(unread=True):
        level, title, line, todo, push = _notice_words(str(n.get("kind") or ""))
        out.append(Condition(f"notice:{int(n['id'])}", level, title, line, todo, "",
                             str(n.get("text") or ""), since=_ts(n.get("at")), push=push,
                             notice=int(n["id"])))
    return out


# ── the plan's usage ─────────────────────────────────────────────────

def src_usage(ctx: Ctx) -> list[Condition]:
    try:
        import rate_limits
    except ModuleNotFoundError:
        from preprocessor import rate_limits
    reading = rate_limits.load("resident", path=ctx.private / rate_limits.FILE_NAME)
    if not reading:
        return []
    taken = reading["ts"].timestamp()
    if ctx.now - taken > USAGE_FRESH_S:
        return []
    full, near = [], []
    for name, w in sorted(reading["windows"].items()):
        resets = _ts(w.get("resets_at"))
        if resets is not None and resets <= ctx.now:
            continue
        pct = float(w.get("percent_used") or 0)
        if w.get("status") == "rejected" or pct >= 100:
            full.append((name, pct, resets))
        elif pct >= USAGE_WARN_PERCENT or w.get("status") == "allowed_warning":
            near.append((name, pct, resets))
    if not full and not near:
        return []
    detail = f"The plan's reading from {_iso(taken)} ({_plain(reading.get('source'), 64)}):\n" + "\n".join(
        f"{n}: {p:.0f}% used, resets {_iso(r)}" for n, p, r in full + near)
    if full:
        until = max((r for _n, _p, r in full if r), default=None)
        return [Condition("usage", "problem", "Plan limit reached",
                          "Tacitus has used all of its plan for now, so it cannot answer.",
                          "Nothing to do. It works again when the plan resets.", "", detail,
                          until=until, push=True)]
    until = min((r for _n, _p, r in near if r), default=None)
    return [Condition("usage", "warn", "Plan nearly used",
                      "Tacitus has used most of its plan for now.",
                      "Nothing to do. It may stop answering for a while before the plan resets.",
                      "", detail, until=until)]


# ── the owner's own model ────────────────────────────────────────────

def src_local(ctx: Ctx) -> list[Condition]:
    try:
        import model_routing
    except ModuleNotFoundError:
        from preprocessor import model_routing
    st = model_routing.local_status()
    if not st.get("configured") or st.get("reachable") is not False:
        return []
    tech = model_routing.local_status(verbose=True).get("detail") or ""
    return [Condition("local", "warn", "Your own AI is not answering",
                      "Your own AI model is not answering, so the work it does waits.",
                      "Nothing to do if it is restarting. If it lasts, check the computer it runs on, "
                      "or move its work in Models.", "local",
                      f"Model {_plain(st.get('model'), 80)}: {tech}",
                      action={"label": "Open Models", "settings": "routing"}, grace_s=LOCAL_GRACE_S)]


# ── the relay ────────────────────────────────────────────────────────

def src_relay(ctx: Ctx) -> list[Condition]:
    relay = ctx.relay
    if relay is None:
        return []
    st = relay.status()
    if st.get("connected"):
        return []
    last_ok = _ts(st.get("last_ok"))
    return [Condition("relay", "warn", "Away from home is down",
                      "Your phone cannot reach Tacitus away from home right now. "
                      "On your own network it works.",
                      "Nothing to do. Tacitus keeps trying.", "",
                      f"The relay client is not connected; last answer {_iso(last_ok)}; "
                      f"last error: {_plain(st.get('last_error'), 160) or 'none'}",
                      since=last_ok, grace_s=RELAY_GRACE_S)]


# ── the updater ──────────────────────────────────────────────────────

UPDATE_WORDS = {
    "failed": ("An update did not install", "The last update did not install.",
               "Nothing to do. Tacitus tries again within the hour."),
    "stopped": ("Updates stopped", "Tacitus's updater stopped before it could install anything.",
                f"Ask {HELPER} to check the updater. The details say what it said."),
    "key": ("An update waits for you", "A new release is signed with a new key and waits.",
            f"Ask {HELPER} to check the new key before it installs."),
    "held": ("An update is on hold", "An update is on hold.",
             "Nothing to do. It installs with the next release."),
    "stale": ("Updates are not checked", "Tacitus has not looked for updates for two days.",
              f"Ask {HELPER} to check the update timer."),
}


def src_update(ctx: Ctx) -> list[Condition]:
    try:
        import update_status as us
    except ModuleNotFoundError:
        from preprocessor import update_status as us
    status, detail, fix = us.doctor_row(ctx.live, ctx.private, ctx.ea, ctx.now)
    if status == "PASS" or us.tree_kind(ctx.live) != "layout" or us.misplaced_channel(ctx.private, ctx.ea):
        return []
    st = us.load_status(ctx.private)
    code = ("failed" if (st.get("last_apply") or {}).get("result") == "failed"
            else "stopped" if (st.get("last_error") or {}).get("error")
            else "key" if (st.get("key_change") or {}).get("version")
            else "held" if (st.get("held_back") or {}).get("version") else "stale")
    title, line, todo = UPDATE_WORDS[code]
    return [Condition("update", "warn", title, line, todo, "update",
                      f"{detail}\nFix: {fix}" if fix else detail,
                      action={"label": "Show version", "say": "version"})]


# ── Google Drive (user-profile.json is resident space: a count only) ─

def src_drive(ctx: Ctx) -> list[Condition]:
    try:
        import resident_io
    except ModuleNotFoundError:
        from preprocessor import resident_io
    try:
        prof = json.loads(resident_io.read_text(ctx.ea / "config" / "user-profile.json") or "{}")
    except ValueError:
        return []
    listed = [str(a).strip().lower() for a in (prof.get("drive_accounts") or [])
              if isinstance(prof, dict) and isinstance(a, str) and a.strip()][:20]
    auth = ctx.ea / "auth"
    missing = [a for a in listed
               if not (auth / f"google_token_{a.replace('@', '_at_').replace('.', '_')}_drive.json").exists()]
    if not missing:
        return []
    return [Condition("drive", "warn", "Google Drive is not connected",
                      "Tacitus cannot open the Google Drive files your mail links to.",
                      "Tap Connect, and Tacitus sends you a link to sign in.", "",
                      f"{len(missing)} of the {len(listed)} Drive accounts in user-profile.json "
                      "have no Drive sign-in (docs/SETUP.md, Google Drive).",
                      action={"label": "Connect", "say": "connect Google Drive"})]


SOURCES: list[tuple[str, Callable[[Ctx], list[Condition]]]] = [
    ("sentinel", src_sentinel), ("breakers", src_breakers), ("boundary", src_boundary),
    ("plugins", src_plugins), ("notices", src_notices), ("usage", src_usage),
    ("local", src_local), ("relay", src_relay), ("update", src_update), ("drive", src_drive),
]


def conditions(ctx: Ctx, sources=None) -> list[Condition]:
    """Every source's conditions. A source that fails adds one warning in
    fixed words, with the kind of error and no text of it."""
    out = []
    for name, fn in sources or SOURCES:
        try:
            out.extend(fn(ctx))
        except Exception as e:
            out.append(Condition(f"source:{name}", "warn", "A check could not run",
                                 "Tacitus could not read one of its own checks.",
                                 f"Ask {HELPER} to look at the details.", "",
                                 f"The {name} source failed: {type(e).__name__}."))
    return out


def worst(conds) -> str:
    level = "ok"
    for c in conds:
        lv = c.level if isinstance(c, Condition) else c.get("level")
        if RANK.get(lv, 0) > RANK[level]:
            level = lv
    return level


def overall_line(conds: list) -> str:
    if not conds:
        return "All good"
    if len(conds) == 1:
        c = conds[0]
        return c.title if isinstance(c, Condition) else c["title"]
    return f"{len(conds)} things need a look"


# ── the drill-down's log lines ───────────────────────────────────────

def _tail(path: Path) -> list[str]:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - LOG_READ_BYTES))
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    lines = text.splitlines()
    return lines[1:] if size > LOG_READ_BYTES else lines


def _bounded(rows: list[tuple[float | None, str]], since: float | None, now: float) -> list[str]:
    """The newest LOG_LINES rows in [since - LOG_WINDOW_S, now], each cut and
    redacted, LOG_TOTAL_CHARS in all."""
    lo = (since or now) - LOG_WINDOW_S
    kept = [t for at, t in rows if at is None or lo <= at <= now + 60][-LOG_LINES:]
    out, total = [], 0
    for text in reversed(kept):
        line = log_redact.redact(_PRINTABLE_RE.sub("?", text))[:LOG_LINE_CHARS]
        if total + len(line) > LOG_TOTAL_CHARS:
            break
        out.append(line)
        total += len(line)
    return out[::-1]


_SENTINEL_LINE = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ) (PROBLEM ([a-z_]+): .*|all checks ok)$")
_GRAPH_LINE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ graph_client\[\d+\] [A-Z]+ "
                         r"(Graph API (?:HTTP \d{3}|timeout)\b[^\n]{0,120})$")
_MEDIATOR_LINE = re.compile(r"^(\d{9,11}) ([\w-]{1,40}) ([\w-]{1,40}) (rc=[1-9]\d{0,3}|[A-Z][A-Z-]{2,30})$")
_UPDATE_LINE = re.compile(r"^\[update\] ")


def log_lines(ctx: Ctx, c: Condition) -> tuple[list[str], str]:
    """(lines, the log they come from) for a condition's component: only
    owner logs that hold no mail, only lines of a known shape, bounded and
    redacted. Logs that can hold mail (the ledger sync, the tagger, the
    bot's own log) are never read here."""
    comp, since = c.component, c.since
    if comp == "sentinel":
        check = c.id.partition(":")[2]
        rows = []
        for ln in _tail(ctx.logs / "sentinel.log"):
            m = _SENTINEL_LINE.match(ln)
            if m and (m.group(3) == check if check else True):
                rows.append((_ts(m.group(1)), ln))
        return _bounded(rows, since, ctx.now), "sentinel.log"
    if comp == "graph":
        rows = [(_ts(m.group(1).replace(" ", "T")), f"{m.group(1)} {m.group(2)}")
                for m in map(_GRAPH_LINE.match, _tail(ctx.logs / "graph-client.log")) if m]
        return _bounded(rows, since, ctx.now), "graph-client.log"
    if comp == "mediator":
        rows = [(float(m.group(1)), f"{_iso(float(m.group(1)))} {m.group(2)} {m.group(3)} {m.group(4)}")
                for m in map(_MEDIATOR_LINE.match, _tail(ctx.logs / "mail-mediator.log")) if m]
        return _bounded(rows, since, ctx.now), "mail-mediator.log"
    if comp == "boundary":
        lines = _tail(ctx.logs / "boundary-audit.log")
        start = max((i for i, ln in enumerate(lines) if ln.startswith("STATUS ")), default=-1)
        rows = [(None, ln) for ln in lines[start + 1:] if re.match(r"^(warn|FAIL)\s", ln)]
        return _bounded(rows, None, ctx.now), "boundary-audit.log"
    if comp == "update":
        rows = [(None, ln) for ln in _tail(ctx.logs / "update.log") if _UPDATE_LINE.match(ln)]
        return _bounded(rows, None, ctx.now), "update.log"
    if comp == "local":
        rows = []
        for ln in _tail(ctx.private / "remote-llm" / "calls.jsonl"):
            try:
                rec = json.loads(ln)
            except ValueError:
                continue
            if not isinstance(rec, dict) or rec.get("ok") is not False:
                continue
            kind = _plain(str(rec.get("error") or "error").split(":", 1)[0], 60)
            rows.append((_ts(rec.get("ts")), f"{_plain(rec.get('ts'), 40)} {_plain(rec.get('purpose'), 40)} "
                                             f"failed: {kind}"))
        return _bounded(rows, since, ctx.now), "the local model's call log"
    return [], ""


# ── the light: memory, summary, pushes ───────────────────────────────

class Light:
    """The status light the web front end serves (WebFrontend.light): its
    summary, a condition's details, marking a notice read, and the tick.
    `push_on` says whether a red condition may push (the web chat runs
    alone: in shadow mode the primary's health channel has the alert)."""

    def __init__(self, fe, *, ctx: Callable[[], Ctx] | None = None,
                 push_on: Callable[[], bool] | None = None,
                 clock: Callable[[], float] = time.time, sources=None) -> None:
        self.fe = fe
        self._ctx = ctx or (lambda: Ctx.live_ctx(store=fe.store, relay=getattr(fe, "relay", None)))
        self.push_on = push_on or (lambda: True)
        self.clock = clock
        self.sources = sources
        self._cache: tuple[float, dict, list[Condition]] | None = None

    def _context(self) -> Ctx:
        ctx = self._ctx()
        ctx.now = self.clock()
        return ctx

    def refresh(self) -> tuple[dict, list[Condition]]:
        """Read every source, update the memory and give (summary, the
        conditions shown)."""
        ctx = self._context()
        now = ctx.now
        found = conditions(ctx, self.sources)
        store = self.fe.store
        mem = store.light_memory()
        seen = mem.get("seen") if isinstance(mem.get("seen"), dict) else {}
        active = mem.get("active") if isinstance(mem.get("active"), dict) else {}
        cleared = [c for c in (mem.get("cleared") or []) if isinstance(c, dict)]
        pushed = mem.get("pushed") if isinstance(mem.get("pushed"), dict) else {}
        shown, new_seen = [], {}
        for c in found:
            first = float(seen.get(c.id) or now)
            new_seen[c.id] = first
            if c.grace_s and now - first < c.grace_s:
                continue
            if c.since is None:
                c.since = float((active.get(c.id) or {}).get("since") or first)
            shown.append(c)
        new_active = {}
        for c in shown:
            rec = dict(active.get(c.id) or {})
            rec.update(level=c.level, title=c.title, line=c.line, since=c.since,
                       notice=c.notice is not None)
            new_active[c.id] = rec
        for cid, rec in active.items():
            if cid not in new_active:
                how = "read" if rec.get("notice") and self._read_notice(cid) else "fixed"
                cleared.append({"id": cid, "level": rec.get("level", "warn"), "title": rec.get("title", ""),
                                "line": rec.get("line", ""), "since": rec.get("since"), "until": now,
                                "how": how})
        cleared = self._merge_cleared(cleared, sentinel_cleared(ctx), now)
        mem.update(seen=new_seen, active=new_active, cleared=cleared, pushed={
            k: v for k, v in pushed.items() if isinstance(v, (int, float)) and now - v < PUSH_AGAIN_S})
        self._mem = mem
        shown.sort(key=lambda c: (-RANK[c.level], c.since or now))
        summary = {"level": worst(shown), "line": overall_line(shown),
                   "conditions": [c.public() for c in shown],
                   "cleared": [dict(c) for c in cleared],
                   "as_of": now}
        self._cache = (now, summary, shown)
        return summary, shown

    def _read_notice(self, cid: str) -> bool:
        if not cid.startswith("notice:"):
            return False
        try:
            rows = self.fe.store.light_notices(unread=False)
        except Exception:
            return False
        nid = cid.partition(":")[2]
        return any(str(r["id"]) == nid and r.get("read_at") for r in rows)

    @staticmethod
    def _merge_cleared(cleared: list[dict], extra: list[dict], now: float) -> list[dict]:
        rows = [c for c in cleared if now - float(c.get("until") or 0) <= CLEARED_KEEP_S]
        for e in extra:
            if not any(r["id"] == e["id"] and abs(float(r.get("until") or 0) - e["until"]) < 900
                       for r in rows):
                rows.append(e)
        rows.sort(key=lambda r: -float(r.get("until") or 0))
        return rows[:CLEARED_MAX]

    def _save(self) -> None:
        self.fe.store.set_light_memory(self._mem)

    @staticmethod
    def signature(summary: dict) -> str:
        key = [summary["level"], [(c["id"], c["level"], c["title"], c["line"], c["todo"])
                                  for c in summary["conditions"]],
               [(c["id"], c.get("until")) for c in summary["cleared"]]]
        return hashlib.sha256(json.dumps(key, sort_keys=True, default=str).encode()).hexdigest()[:16]

    def tick(self) -> dict:
        """One look: the memory saved, the tick's mark, a `light` event when
        the summary changed, one push for what newly turned red, and the
        notification cleared once everything that pushed has cleared."""
        summary, shown = self.refresh()
        mem, now = self._mem, summary["as_of"]
        sig = self.signature(summary)
        if sig != mem.get("sig"):
            mem["sig"] = sig
            self.fe.light_changed(summary)
        self._push(shown, mem, now)
        self._save()
        self.fe.store.light_mark()
        return summary

    def _push(self, shown: list[Condition], mem: dict, now: float) -> None:
        pushed = mem.setdefault("pushed", {})
        due = [c for c in shown if c.push and c.level == "problem" and not mem["active"][c.id].get("pushed")
               and now - float(pushed.get(c.id) or 0) >= PUSH_AGAIN_S]
        if due and self.push_on():
            body = due[0].line if len(due) == 1 else f"{len(due)} things need a look."
            url = f"{self.fe.app_url()}#/status"
            try:
                self.fe.notify("Tacitus", body, url, "status", urgency="high", dedupe=True)
            except Exception:
                pass
            for c in due:
                mem["active"][c.id]["pushed"] = now
                pushed[c.id] = now
            mem["push_open"] = True
        if mem.get("push_open") and not any(r.get("pushed") for r in mem["active"].values()):
            mem["push_open"] = False
            try:
                self.fe.light_clear()
            except Exception:
                pass

    def summary(self) -> dict:
        if self._cache and self.clock() - self._cache[0] < SUMMARY_FRESH_S:
            return self._cache[1]
        return self.tick()

    def _current(self, cid: str) -> Condition | None:
        if not self._cache or self.clock() - self._cache[0] >= SUMMARY_FRESH_S:
            self.tick()
        return next((c for c in self._cache[2] if c.id == cid), None) if self._cache else None

    def details(self, cid: str) -> dict | None:
        """A condition's technical detail and its log lines (redacted,
        bounded), for the owner's own web chat; None for an id not shown."""
        c = self._current(cid)
        if c is None:
            return None
        lines, source = log_lines(self._context(), c)
        return {"id": c.id, "detail": log_redact.redact(c.detail)[:DETAIL_CHARS],
                "log": lines, "log_from": source}

    def mark_read(self, cid: str) -> dict | None:
        """The owner read a notice: it leaves the light for Recently
        cleared; the new summary. None for an id that is no notice shown."""
        c = self._current(cid)
        if c is None or c.notice is None:
            return None
        self.fe.store.light_notice_read(c.notice, self.clock())
        self._cache = None
        return self.tick()

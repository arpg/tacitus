"""Dead-man's switch for the EA system.

Runs as the owner from cron every 10 minutes, independent of the
Slack bot process and the run-agent.sh pipeline it monitors. Zero Claude
tokens. Posts to Slack ONLY when something is wrong (plus one "recovered"
message when a problem clears).

The bot's internal watchdog can only protect a *running* process, and a
cron failure only lands in a log file. This sentinel is the outside
observer of both.

Checks:
  1.  ea-chat-bot.service is active        (systemctl is-active)
  2.  ea-chat-bot.service is enabled       (catches "disabled" BEFORE reboot)
  3.  no stale user-level bot unit running  (duplicate Socket Mode consumer)
  4.  morning briefing posted today         (agent-post-success.json marker)
  5.  afternoon catch-up posted today       (marker ts age, weekdays)
  6.  weekly audit posted in last 8 days
  7.  cron heartbeat                        (ledger-sync.log mtime, 15-min job)
  8.  recent hard failures in agent logs    (FATAL / RETRY ALSO FAILED / ...)
  9.  deferred-posts queue not stuck        (flush cron broken)
  10. installed crontab matches crontab.master
  11. circuit breakers not stuck open       (external service down)
  12. resident tmux Claude pane alive        (when resident mode on)
  13. ea-bot creds carry a refresh token (resident can re-authenticate)

Alerting:
  - One consolidated message to the 'ea-assistant' webhook, bypassing the
    quiet-hours deferral queue (the sentinel must not depend on the flush
    cron it monitors).
  - If the primary webhook fails: retry once, then fall back to the
    'personal' webhook with a prefix. If both fail, append a JSONL record
    to ~/.ea/logs/sentinel.log and exit non-zero. Never raises uncaught.
  - Anti-spam: the fingerprint (sorted failing check names, each with a
    digest of its failure detail) is stored in
    ~/.ea/state/sentinel-state.json. An unchanged fingerprint re-alerts at
    most every REALERT_HOURS; a changed fingerprint — a new check failing,
    OR a different failure of an already-failing check — alerts
    immediately; full recovery posts a single all-clear and resets state.

Usage:
    python3 preprocessor/sentinel.py check [--json] [--force-alert]
    python3 preprocessor/sentinel.py status
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

EA_DIR = Path(os.environ.get("EA_DIR", str(Path.home() / ".ea")))
# Anti-spam state lives OUTSIDE ~/.ea/state on purpose: that directory is
# group-writable by ea-bot (the LLM-driven user), and whoever can write
# this file can suppress sentinel alerts by forging the fingerprint +
# last_alert_ts. ~/.local/state is inside the owner's private home, which
# ea-bot cannot traverse.
STATE_FILE = (Path.home() / ".local" / "state" / "ea-sentinel"
              / "sentinel-state.json")
# Bootstrap anchor — separate from STATE_FILE because the anti-spam state
# is wiped on recovery, and this must persist: it records when the
# post-success marker file was FIRST seen, so per-agent grace windows
# (below) survive across alert/recover cycles.
BOOTSTRAP_FILE = (Path.home() / ".local" / "state" / "ea-sentinel"
                  / "bootstrap.json")
POST_SUCCESS_FILE = EA_DIR / "state" / "agent-post-success.json"
LAST_RUN_FILE = EA_DIR / "state" / "agent-last-run.json"
DEFERRED_FILE = EA_DIR / "state" / "deferred-posts.json"
from log_paths import owner_log, owner_log_dir  # noqa: E402
# ledger-sync.log and sentinel.log are written only by owner processes (the
# cron redirect and sentinel itself), so they live in the owner-private log
# dir, out of ea-bot's reach. LOG_DIR still scans the shared dir, where
# run-agent.sh writes the per-run agent logs sentinel monitors (phase 2).
LEDGER_SYNC_LOG = owner_log("ledger-sync.log")
# The per-run agent logs and failed-attempt files sentinel scans are written
# by run-agent.sh (the owner) into the owner-private dir, so scan there.
LOG_DIR = owner_log_dir()
SENTINEL_LOG = owner_log("sentinel.log")
# The owner keeps the cron schedule as a file in the home directory
# (docs/SETUP.md "Cron schedule"); the sentinel runs as the owner.
CRONTAB_MASTER = Path(os.environ.get("EA_CRONTAB_MASTER") or str(Path.home() / "crontab.master"))

def _bot_unit() -> str:
    """The bot's system unit: ea-chat-bot.service, or ea-slack-bot.service
    on a host where deploy/rename-chat-bot.sh has not run yet."""
    for unit in ("ea-chat-bot.service", "ea-slack-bot.service"):
        try:
            r = subprocess.run(["systemctl", "cat", unit], capture_output=True, timeout=10)
            if r.returncode == 0:
                return unit
        except Exception:
            pass
    return "ea-chat-bot.service"


SERVICE = _bot_unit()
# One reminder a day for an unchanged condition. The fingerprint carries a
# detail digest, so a NEW failure of an already-failing check still alerts
# at once and does not wait for this window.
REALERT_HOURS = 24
HEARTBEAT_MAX_AGE_MIN = 45      # ledger_sync runs every 15 min
DEFERRED_MAX_AGE_H = 12
WEEKLY_MAX_AGE_DAYS = 8
FAILURE_PATTERNS = ("FATAL", "RETRY ALSO FAILED", "OUTPUT VALIDATION FAILED")
MONITORED_AGENTS = ("morning-briefing", "afternoon-catchup", "weekly-audit")
EA_BOT_CREDENTIALS = "/home/ea-bot/.claude/.credentials.json"
EA_BOT_HOME = "/home/ea-bot"
# The one command that restores ea-bot's Claude login (docs/SETUP.md,
# "Common failures").
LOGIN_FIX = (f"cd / && sudo -u ea-bot env HOME={EA_BOT_HOME} "
             f"{EA_BOT_HOME}/.local/bin/claude, then /login, then Ctrl+C")
# The bot's runner logs this phrase when a turn met a lost login or a
# refused stored token (ea-chat-bot.py, _LOGIN_LOST_MARKERS).
BOT_LOG = "slack-bot.log"
LOGIN_LOST_MARKER = "Claude login is gone"
LOGIN_LOST_WINDOW_MIN = 60


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _systemctl(*args: str) -> str:
    """Run systemctl, return stripped stdout ('' on any error)."""
    try:
        out = subprocess.run(
            ["systemctl", *args], capture_output=True, text=True, timeout=10
        )
        return (out.stdout or "").strip()
    except Exception:
        return ""


def _load_json(path: Path) -> dict:
    """A JSON object from one of sentinel's own files in the owner's home."""
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _read_shared(path: Path) -> str | None:
    """A file in the resident's space (~/.ea/state), read with the
    resident's access, so a link at its name hands sentinel nothing the
    resident could not read itself; None when absent or unreadable."""
    try:
        import resident_io
    except ImportError:
        from preprocessor import resident_io
    return resident_io.read_text(path)


def _load_shared_json(path: Path) -> dict:
    """_load_json for a file in the resident's space."""
    try:
        data = json.loads(_read_shared(path) or "{}")
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _local_now() -> datetime:
    """Now in the user's active timezone (home or travel)."""
    try:
        import quiet_hours
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(quiet_hours.active_timezone()))
    except Exception:
        return datetime.now().astimezone()


def _marker(agent: str) -> dict:
    return _load_shared_json(POST_SUCCESS_FILE).get(agent) or {}


def _marker_age_hours(agent: str) -> float | None:
    ts = _marker(agent).get("ts")
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0
    except Exception:
        return None


def _ea_enabled() -> bool:
    try:
        import ea_config
        return ea_config.is_ea_enabled()
    except Exception:
        return True  # fail toward monitoring


def _marker_system_age_hours() -> float | None:
    """Hours since the sentinel FIRST saw the post-success marker file.

    None while the file doesn't exist. Grace is per-AGENT, not per-FILE:
    the first agent to write the marker file must not end the grace for
    the others, whose keys are legitimately missing until they next run.
    A missing key is excusable until one full cycle of that agent has
    elapsed since the marker system appeared."""
    if not POST_SUCCESS_FILE.exists():
        return None
    boot = _load_json(BOOTSTRAP_FILE)
    first_seen = boot.get("marker_first_seen_ts")
    if not first_seen:
        first_seen = time.time()
        try:
            BOOTSTRAP_FILE.parent.mkdir(parents=True, exist_ok=True)
            BOOTSTRAP_FILE.write_text(json.dumps(
                {"marker_first_seen_ts": first_seen}))
        except Exception:
            pass
    return (time.time() - first_seen) / 3600.0


# One full cycle per agent: a daily agent whose key is still missing
# 26h after the marker system appeared has genuinely failed to post;
# before that, the key's absence just means "posted before deployment".
GRACE_HOURS = {"morning-briefing": 26, "afternoon-catchup": 26,
               "weekly-audit": 8 * 24}


def _missing_key_in_grace(agent: str) -> bool:
    age = _marker_system_age_hours()
    return age is not None and age < GRACE_HOURS[agent]


def _ea_bot_refresh_token_present() -> bool | None:
    """Whether the ea-bot Claude credentials carry a refresh token.

    Returns True (present), False (missing/empty), or None (unreadable
    -> skip). The creds file is mode 600 ea-bot, so the sentinel (the owner)
    reads only a presence boolean via `sudo -n -u ea-bot` — the token is
    never read out or logged.

    Presence, not expiry: the OAuth ACCESS token is short-lived (~8h) and
    the resident auto-refreshes it on demand, so a near or past
    access-token expiry is normal during idle. The only locally-detectable
    unrecoverable state is a missing refresh token (creds wiped, or never
    logged in), which never self-heals. A present-but-server-rejected
    refresh token needs an API call to detect and is out of scope; it
    surfaces as dispatches failing, not as a file state."""
    try:
        out = subprocess.run(
            ["sudo", "-n", "-u", "ea-bot", "python3", "-c",
             "import json;d=json.load(open('" + EA_BOT_CREDENTIALS + "'));"
             "o=d.get('claudeAiOauth',d);"
             "print('1' if (o.get('refreshToken') or o.get('refresh_token')) "
             "else '0')"],
            capture_output=True, text=True, timeout=10)
        raw = out.stdout.strip()
        if raw not in ("0", "1"):
            return None
        return raw == "1"
    except Exception:
        return None


def _ea_bot_claude() -> str | None:
    """The Claude binary ea-bot runs, in the bot's order: its own install,
    then the system-wide ones. Tested as ea-bot, since the owner cannot
    always look into ea-bot's home."""
    for c in (EA_BOT_HOME + "/.local/bin/claude", "/usr/local/bin/claude", "/usr/bin/claude"):
        try:
            r = subprocess.run(["sudo", "-n", "-u", "ea-bot", "test", "-x", c],
                               capture_output=True, timeout=10, cwd="/")
        except Exception:
            return None
        if r.returncode == 0:
            return c
    return None


def _ea_bot_logged_in() -> bool | None:
    """Whether ea-bot's Claude Code has a login, by the CLI's own word
    (`claude auth status`, run as ea-bot). None when the probe cannot run:
    no sudo grant, no binary, or output that is not the CLI's JSON. The
    token itself is never read."""
    claude = _ea_bot_claude()
    if claude is None:
        return None
    try:
        out = subprocess.run(
            ["sudo", "-n", "-u", "ea-bot", "env", f"HOME={EA_BOT_HOME}", claude, "auth", "status"],
            capture_output=True, text=True, timeout=30, cwd="/")
        data = json.loads(out.stdout.strip() or "{}")
    except Exception:
        return None
    state = data.get("loggedIn") if isinstance(data, dict) else None
    return state if isinstance(state, bool) else None


def _login_lost_in_bot_log(window_min: int = LOGIN_LOST_WINDOW_MIN) -> bool:
    """Whether a chat turn met a lost login within the window, by the
    bot's own log line. `claude auth status` reports a stored token as a
    login whatever the server says of it, so a refused token is seen
    here, on the first turn that hits it, and nowhere else."""
    path = LOG_DIR / BOT_LOG
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 400_000))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return False
    cutoff = time.time() - window_min * 60
    for line in tail.splitlines():
        if LOGIN_LOST_MARKER not in line:
            continue
        try:
            ts = time.mktime(time.strptime(line[:19], "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            continue
        if ts >= cutoff:
            return True
    return False


# ---------------------------------------------------------------------------
# Checks — each returns (ok: bool, detail: str)
# ---------------------------------------------------------------------------

def check_service_active() -> tuple[bool, str]:
    state = _systemctl("is-active", SERVICE) or "unknown"
    return (state == "active",
            f"{SERVICE} is {state}" + ("" if state == "active"
                                       else " — bot is NOT listening"))


def check_service_enabled() -> tuple[bool, str]:
    state = _systemctl("is-enabled", SERVICE) or "unknown"
    return (state == "enabled",
            f"{SERVICE} is {state}" + ("" if state == "enabled"
                                       else " — will NOT survive a reboot"))


def check_no_user_unit() -> tuple[bool, str]:
    state = next((s for s in (_systemctl("--user", "is-active", u) for u in ("ea-chat-bot", "ea-slack-bot"))
                  if s == "active"), "")
    if state == "active":
        return (False, "stale USER-level ea-chat-bot unit is running — "
                       "duplicate Socket Mode consumer (2026-06-09 bug)")
    return (True, "no duplicate user-level bot")


def check_briefing_posted() -> tuple[bool, str]:
    if not POST_SUCCESS_FILE.exists():
        # Marker system not deployed / first run — stay quiet until
        # run-agent.sh has written its first post-success entry.
        return (True, "no post-success marker file yet (new install)")
    now = _local_now()
    if now.weekday() >= 5:
        return (True, "weekend — briefing not expected")
    if now.hour < 9:
        return (True, f"before 09:00 local ({now:%H:%M}) — not due yet")
    today = now.date().isoformat()
    if _marker("morning-briefing").get("date") == today:
        return (True, "morning briefing posted today")
    if not _marker("morning-briefing") and _missing_key_in_grace(
            "morning-briefing"):
        return (True, "no morning-briefing marker yet — marker system "
                      "deployed < 26h ago (bootstrap grace)")
    # Distinguish "ran but post failed" from "never ran" via the 4AM gate
    # marker that quiet_hours.due_local writes when the run starts.
    if _load_shared_json(LAST_RUN_FILE).get("morning-briefing") == today:
        return (False, "morning briefing RAN today but the Slack post never "
                       "succeeded (check run-agent logs)")
    return (False, "morning briefing NEVER RAN today (cron or 4AM-local "
                   "gate problem)")


def check_afternoon_posted() -> tuple[bool, str]:
    if not POST_SUCCESS_FILE.exists():
        return (True, "no post-success marker file yet (new install)")
    # Mirror check_briefing_posted: use the ACTIVE timezone and compare by
    # DATE ("did it post today?"), not a fixed marker-age window. A window
    # false-alarms whenever local != server time (travel) or the sentinel
    # runs long after a post that did succeed.
    now = _local_now()
    if now.weekday() >= 5:
        return (True, "weekend — catch-up not expected")
    # The catch-up cron fires 15:00 server (Mountain); its Slack post can be
    # pushed into the local evening by travel/quiet-hours, so stay quiet
    # until late local evening before treating it as missed.
    if now.hour < 18:
        return (True, f"before 18:00 local ({now:%H:%M}) — not due yet")
    today = now.date().isoformat()
    if _marker("afternoon-catchup").get("date") == today:
        return (True, "afternoon catch-up posted today")
    if not _marker("afternoon-catchup") and _missing_key_in_grace(
            "afternoon-catchup"):
        return (True, "no afternoon-catchup marker yet — marker system "
                      "deployed < 26h ago (bootstrap grace)")
    # A post still held in the quiet-hours deferral queue is the
    # deferred-queue check's concern, not a missed-catch-up alarm.
    try:
        if DEFERRED_FILE.exists() and json.loads(_read_shared(DEFERRED_FILE) or "[]"):
            return (True, "afternoon catch-up held in quiet-hours deferral "
                          "queue (see deferred-queue check)")
    except Exception:
        pass
    return (False, "afternoon catch-up did not post today "
                   "(no marker for today's local date)")


def check_weekly_audit() -> tuple[bool, str]:
    age = _marker_age_hours("weekly-audit")
    if age is not None and age <= WEEKLY_MAX_AGE_DAYS * 24:
        return (True, f"weekly audit posted {age / 24:.1f}d ago")
    if age is None and not POST_SUCCESS_FILE.exists():
        # Marker file doesn't exist at all yet (fresh deploy of the marker
        # system) — don't alarm until one full week has passed with it.
        return (True, "no post-success marker file yet (new install)")
    if age is None and _missing_key_in_grace("weekly-audit"):
        return (True, "no weekly-audit marker yet — marker system deployed "
                      "< 8d ago (bootstrap grace; next audit will record)")
    return (False, "weekly audit not posted in the last "
                   f"{WEEKLY_MAX_AGE_DAYS} days "
                   f"({'no marker' if age is None else f'{age / 24:.0f}d old'})")


def check_cron_heartbeat() -> tuple[bool, str]:
    if not LEDGER_SYNC_LOG.exists():
        return (False, "ledger-sync.log missing — cron canary has never run")
    age_min = (time.time() - LEDGER_SYNC_LOG.stat().st_mtime) / 60.0
    if age_min <= HEARTBEAT_MAX_AGE_MIN:
        return (True, f"cron heartbeat ok (ledger-sync {age_min:.0f}m ago)")
    return (False, f"cron heartbeat STALE — ledger-sync.log untouched for "
                   f"{age_min:.0f}m (15-min job; is cron running / crontab "
                   f"installed?)")


def check_recent_failures() -> tuple[bool, str]:
    cutoff = time.time() - 24 * 3600
    hits: list[str] = []
    try:
        for f in LOG_DIR.iterdir():
            if f.stat().st_mtime < cutoff:
                continue
            name = f.name
            if name.endswith(".txt") and "-failed-attempt-" in name:
                hits.append(f"failed-attempt file: {name}")
                continue
            if not (name.endswith(".log")
                    and name.startswith(MONITORED_AGENTS)):
                continue
            try:
                tail = f.read_text(errors="replace")[-20000:]
            except Exception:
                continue
            for pat in FAILURE_PATTERNS:
                if pat in tail:
                    hits.append(f"{name}: contains '{pat}'")
                    break
    except Exception as e:
        return (True, f"log scan skipped ({e})")
    if hits:
        return (False, "agent failures in last 24h: " + "; ".join(hits[:5]))
    return (True, "no hard failures in agent logs (24h)")


def check_deferred_queue() -> tuple[bool, str]:
    if not DEFERRED_FILE.exists():
        return (True, "no deferred-posts queue")
    text = _read_shared(DEFERRED_FILE)
    if text is None:
        return (False, "deferred-posts.json could not be read with the resident's access")
    try:
        queue = json.loads(text)
    except Exception:
        return (False, "deferred-posts.json is corrupt (unparseable JSON)")
    if not queue:
        return (True, "deferred-posts queue empty")
    age_h = (time.time() - DEFERRED_FILE.stat().st_mtime) / 3600.0
    if age_h <= DEFERRED_MAX_AGE_H:
        return (True, f"deferred queue has {len(queue)} item(s), "
                      f"{age_h:.1f}h old — within limits")
    return (False, f"deferred-posts queue STUCK: {len(queue)} item(s), "
                   f"untouched for {age_h:.0f}h — quiet-hours flush cron "
                   f"broken?")


# Cron lines a self-managing tool rewrites on its own schedule (e.g. the
# hf-trial tool tags every entry it owns with "# hft-managed"). Those change
# without a crontab.master update by design, so they are NOT config drift.
# Excluding them keeps the drift check meaningful for the crons that are
# supposed to stay tracked (EA jobs, manually-added watchers, etc.).
_CRON_SELF_MANAGED_MARKERS = ("# hft-managed",)


def _cron_lines(text: str) -> set[str]:
    """Active (non-comment, non-empty, non-env) cron lines, normalized.
    Self-managed lines (see _CRON_SELF_MANAGED_MARKERS) are skipped so a tool
    rewriting its own schedule does not register as crontab drift."""
    out = set()
    for line in text.splitlines():
        s = " ".join(line.split())
        if not s or s.startswith("#"):
            continue
        if any(m in s for m in _CRON_SELF_MANAGED_MARKERS):
            continue
        out.add(s)
    return out


# The owner's git checkout (EA_REPO_DIR), default: the tree this file is in.
REPO_DIR = Path(os.environ.get("EA_REPO_DIR") or str(Path(__file__).resolve().parents[1]))
DEPLOY_DIR = Path("/home/ea-bot/ea-system")
BOT_INSTANCE_FILE = Path("/home/ea-bot/.ea/state/bot-instance.json")
# The deploy hook's stamp while it waits for in-flight dispatches before
# the restart; the instance is behind the deployed tree on purpose then.
DEPLOY_PENDING_FILE = Path(os.environ.get("EA_PRIVATE_DIR")
                           or Path.home() / ".ea-private") / "deploy-pending.json"
DEPLOY_PENDING_MAX_S = 25 * 60   # the hook waits up to 20 minutes


def _deploy_pending(deployed_head: str) -> float | None:
    """Seconds the deploy hook has been waiting to restart the bot onto
    `deployed_head`, or None when no restart is pending (no stamp, a
    stamp for another commit, or one older than the hook's wait)."""
    try:
        d = json.loads(DEPLOY_PENDING_FILE.read_text())
        age = time.time() - float(d.get("since") or 0)
    except Exception:
        return None
    if str(d.get("commit") or "") != deployed_head or not 0 <= age <= DEPLOY_PENDING_MAX_S:
        return None
    return age


def _open_dir_nofollow(path: Path) -> int:
    """A descriptor for `path`, walked from / without following a link at
    any component; raises OSError otherwise."""
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for comp in str(path).strip("/").split("/"):
            nxt = os.open(comp, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                          dir_fd=fd)
            os.close(fd)
            fd = nxt
    except BaseException:
        os.close(fd)
        raise
    return fd


_UNSAFE_WHY = ""


def _owner_only(st: os.stat_result) -> bool:
    return st.st_uid == os.geteuid() and not (st.st_mode & 0o022)


def _git_in_deploy_tree(*args: str) -> subprocess.CompletedProcess | None:
    """git in the lanes' copy, with the deploy hook's check: the tree and
    its .git are real directories this user owns that no one else can
    write, reached without following a link; git runs in the checked
    directory itself (by descriptor) with every GIT_* variable cleared.
    None when the check fails, since the tree sits in the resident's home
    and git takes its configuration from it."""
    global _UNSAFE_WHY
    _UNSAFE_WHY = ""
    try:
        fd = _open_dir_nofollow(DEPLOY_DIR)
    except OSError as e:
        _UNSAFE_WHY = f"cannot open {DEPLOY_DIR} without following a link: {e}"
        return None
    try:
        import stat as _stat
        tree_st = os.fstat(fd)
        try:
            git_st = os.stat(".git", dir_fd=fd, follow_symlinks=False)
        except OSError as e:
            _UNSAFE_WHY = f".git: {e}"
            return None
        for name, st in ((".", tree_st), (".git", git_st)):
            if not _stat.S_ISDIR(st.st_mode):
                _UNSAFE_WHY = f"{name} is not a directory"
                return None
            if st.st_uid != os.geteuid():
                _UNSAFE_WHY = f"{name} is owned by uid {st.st_uid}, not {os.geteuid()}"
                return None
            if st.st_mode & 0o022:
                _UNSAFE_WHY = f"{name} is writable by others (mode {oct(_stat.S_IMODE(st.st_mode))})"
                return None
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        return subprocess.run(["git", "--git-dir=.git", "--work-tree=.", *args],
                              capture_output=True, text=True, timeout=10, env=env,
                              pass_fds=(fd,), preexec_fn=lambda: os.fchdir(fd))
    except OSError:
        return None
    finally:
        os.close(fd)


def check_deploy_integrity() -> tuple[bool, str]:
    """repo HEAD == deployed HEAD == the commit the RUNNING bot loaded.

    A running process can be many commits behind what everyone believes
    is live: a fix lands in the repo, and either the deploy or the restart
    never happens. Any of the three legs drifting is a page."""
    try:
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        repo_head = subprocess.run(["git", "-C", str(REPO_DIR), "rev-parse", "--short", "HEAD"],
                                   capture_output=True, text=True, timeout=10,
                                   env=env).stdout.strip()
        r = _git_in_deploy_tree("rev-parse", "--short", "HEAD")
    except Exception as e:
        return (True, f"deploy-integrity check skipped (git error: {e})")
    if r is None:
        return (False, f"DEPLOY TREE UNSAFE ({_UNSAFE_WHY}): {DEPLOY_DIR} or its .git is not a real "
                       f"directory owned by {os.getenv('USER') or 'the owner'} and "
                       f"writable by no one else; the deploy hook refuses it too")
    deployed_head = r.stdout.strip()
    release = _updater_release()
    if release is not None:
        # The updater's release layout (docs/updates.md): the release it
        # installed is what ea-bot's copy must match, not the owner's
        # checkout, which no longer deploys.
        repo_head = (deployed_head if release and deployed_head
                     and (deployed_head.startswith(release) or release.startswith(deployed_head))
                     else release)
    if not repo_head or not deployed_head:
        return (True, "deploy-integrity check skipped (unreadable HEADs)")
    if repo_head != deployed_head:
        return (False, f"DEPLOY DRIFT: repo HEAD {repo_head} != deployed "
                       f"{deployed_head} — a commit did not deploy "
                       f"(failed fetch? non-commit ref move?)")
    try:
        inst = json.loads(_read_shared(BOT_INSTANCE_FILE) or "")
    except Exception:
        return (True, f"repo==deployed at {repo_head}; no bot-instance stamp "
                      f"yet (pre-stamp bot or fresh install)")
    inst_commit = inst.get("commit", "")
    if inst_commit and inst_commit != deployed_head:
        # The deploy hook restarts the bot only when code the process
        # holds in memory changed; skills, docs, CLAUDE.md, config
        # templates and tests are read per dispatch from the deployed
        # tree. A docs-only commit therefore leaves the instance one
        # commit behind by design, and that is not a page.
        if _runtime_unchanged_between(inst_commit, deployed_head):
            return (True, f"deploy integrity OK: instance {inst_commit} is behind "
                          f"deployed {deployed_head} by non-runtime files only "
                          f"(read per dispatch, no restart needed)")
        pending = _deploy_pending(deployed_head)
        if pending is not None:
            return (True, f"restart pending: the deploy hook has waited {int(pending)}s "
                          f"for in-flight dispatches before restarting onto "
                          f"{deployed_head} (instance still {inst_commit}; "
                          f"no action needed)")
        return (False, f"STALE BOT INSTANCE: running {inst_commit} but "
                       f"deployed is {deployed_head} — restart failed or "
                       f"orphan process survived. "
                       f"`sudo systemctl restart ea-chat-bot`")
    return (True, f"deploy integrity OK: repo==deployed==instance "
                  f"@ {repo_head}")


def _updater_release(live: Path | None = None) -> str | None:
    """The short commit of the release the updater installed, when the
    services' tree is its release layout (a link, docs/updates.md); None
    for a plain tree, which the deploy hook keeps at the checkout's HEAD."""
    live = live or Path(os.environ.get("EA_LIVE_DIR") or "/opt/ea-system")
    if not live.is_symlink():
        return None
    try:
        return (live / ".deployed-commit").read_text().strip()
    except OSError:
        return ""


# The files whose change means the running process is stale. The deploy
# hook (.githooks/post-commit) decides a restart with the same pattern;
# it is read from there so the two cannot drift, with this copy as the
# fallback when the hook is unreadable.
_RUNTIME_PATTERN_FALLBACK = (r"^(ea-chat-bot\.py|preprocessor/.*\.py|botcore/botcore/.*\.py|"
                             r"scripts/disallowed-tools[^/]*\.txt|requirements\.txt)$")


def _runtime_pattern() -> str:
    try:
        hook = (REPO_DIR / ".githooks" / "post-commit").read_text()
        m = re.search(r"grep -E '(\^\(.*?\)\$)'", hook)
        if m:
            return m.group(1)
    except Exception:
        pass
    return _RUNTIME_PATTERN_FALLBACK


def _runtime_unchanged_between(old: str, new: str) -> bool:
    """True when no runtime file differs between two commits of the
    deployed tree. Unknown commits (a rebase, a shallow clone) read as
    changed, so the page still fires when in doubt."""
    r = _git_in_deploy_tree("diff", "--name-only", old, new)
    if r is None or r.returncode != 0:
        return False
    pat = re.compile(_runtime_pattern())
    return not any(pat.match(line.strip()) for line in r.stdout.splitlines()
                   if line.strip())


def _cron_brief(line: str, width: int = 90) -> str:
    """A cron line fit for a chat alert: KEY=value tokens redacted (a line
    can carry a credential), the EA checkout path shortened, cut to width.
    Enough to tell which job moved, so the fix can go the right way."""
    brief = re.sub(r"(\b[A-Za-z_][A-Za-z0-9_]*=)\S+", r"\1…", line)
    brief = brief.replace(str(REPO_DIR) + "/", "…/")
    return brief if len(brief) <= width else brief[:width - 1] + "…"


def check_crontab_drift() -> tuple[bool, str]:
    if not CRONTAB_MASTER.exists():
        return (True, "crontab.master not found — drift check skipped")
    try:
        installed = subprocess.run(["crontab", "-l"], capture_output=True,
                                   text=True, timeout=10).stdout
    except Exception as e:
        return (True, f"crontab -l failed ({e}) — drift check skipped")
    a, b = _cron_lines(installed), _cron_lines(CRONTAB_MASTER.read_text())
    if a == b:
        return (True, "installed crontab matches crontab.master")
    only_installed, only_master = sorted(a - b), sorted(b - a)
    shown = "".join(f"\n  + installed: {_cron_brief(x)}" for x in only_installed[:3])
    shown += "".join(f"\n  - master: {_cron_brief(x)}" for x in only_master[:3])
    return (False, f"crontab DRIFT: {len(only_installed)} line(s) only installed, "
                   f"{len(only_master)} only in master:{shown}\n"
                   f"  If the installed lines are the intended ones, "
                   f"`crontab -l > {CRONTAB_MASTER}`; if master is, "
                   f"`crontab {CRONTAB_MASTER}`.")


def check_circuit_breakers() -> tuple[bool, str]:
    try:
        import retry_policy
        open_now = retry_policy.open_breakers()
    except Exception as e:
        return (True, f"breaker check skipped ({e})")
    if not open_now:
        return (True, "no open circuit breakers")
    # The detail is fingerprinted, so it names the services and the kind
    # of error only. A failure count or a full error text would change
    # with every probe and re-alert on each one.
    names = ", ".join(
        f"{b['service']} ({_error_kind(b.get('last_error'))})" for b in open_now)
    return (False, f"circuit breaker OPEN: {names} — external service "
                   f"down; calls are failing fast")


def _error_kind(error) -> str:
    """'HTTP 502' from 'HTTP 502: <html>...'; the first words otherwise."""
    text = re.sub(r"\s+", " ", str(error or "?")).strip()
    head = text.split(":", 1)[0].strip()
    return (head or text)[:40] or "?"


def check_resident_pane() -> tuple[bool, str]:
    """Resident tmux Claude pane: when resident_mode is on the pane should
    exist. The bot auto-heals via ensure() per dispatch, so a persistent
    DOWN here means ensure is failing (sudo grant, claude boot error) and
    dispatches are silently falling back."""
    try:
        import resident_bridge
        if not resident_bridge.enabled():
            return (True, "resident mode disabled")
        if resident_bridge.pane_alive():
            return (True, "resident pane alive")
        return (False, "resident pane DOWN — dispatches falling back to "
                       "per-thread sessions; heal: "
                       "scripts/ea-resident-pane.sh ensure")
    except Exception as e:
        return (True, f"resident check skipped ({e})")


def check_ea_bot_login() -> tuple[bool, str]:
    """ea-bot's Claude login, the one every chat turn and cron lane runs
    on. A headless run with no login is not an error to the CLI: it
    answers the prompt with "Not logged in · Please run /login" and exit
    0, so the lanes keep going and post that line. The sentinel asks the
    CLI itself; when that probe cannot run it reads the credentials file,
    where a missing refresh token is the same unrecoverable state."""
    if _login_lost_in_bot_log():
        return (False, "a chat turn met a lost ea-bot Claude login within the last "
                       f"{LOGIN_LOST_WINDOW_MIN} min (the bot log) — a stored token the "
                       "server refuses still reads as logged in to `claude auth status`; "
                       "fix: renew it with scripts/install/ea-bot-token.sh, or remove "
                       "env.CLAUDE_CODE_OAUTH_TOKEN from ea-bot's .claude/settings.json "
                       "and use the browser login: " + LOGIN_FIX)
    logged_in = _ea_bot_logged_in()
    if logged_in is True:
        return (True, "ea-bot Claude login present (claude auth status)")
    if logged_in is False:
        return (False, "ea-bot's Claude login is GONE (claude auth status: not "
                       "logged in) — every chat turn and cron lane answers "
                       "'Not logged in'; fix: " + LOGIN_FIX)
    present = _ea_bot_refresh_token_present()
    if present is None:
        return (True, "ea-bot login check skipped (probe and creds unreadable)")
    if not present:
        return (False, "ea-bot has NO refresh token — its Claude login cannot "
                       "renew; fix: " + LOGIN_FIX)
    return (True, "ea-bot creds have a refresh token (auth self-heals)")


CHECKS = [
    ("service_active", check_service_active),
    ("service_enabled", check_service_enabled),
    ("no_duplicate_bot", check_no_user_unit),
    ("briefing_posted", check_briefing_posted),
    ("afternoon_posted", check_afternoon_posted),
    ("weekly_audit", check_weekly_audit),
    ("cron_heartbeat", check_cron_heartbeat),
    ("recent_failures", check_recent_failures),
    ("deferred_queue", check_deferred_queue),
    ("crontab_drift", check_crontab_drift),
    ("deploy_integrity", check_deploy_integrity),
    ("circuit_breakers", check_circuit_breakers),
    ("resident_pane", check_resident_pane),
    ("ea_bot_login", check_ea_bot_login),
]


# ---------------------------------------------------------------------------
# Alerting
# ---------------------------------------------------------------------------

def _log_local(record: dict) -> None:
    try:
        SENTINEL_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(SENTINEL_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception:
        pass


def _post_alert(text: str) -> bool:
    """Post to the health channel (the main EA channel when none is
    configured); retry once; fall back to 'personal'.

    Returns True if any channel accepted the message."""
    try:
        import slack_notifier
    except Exception as e:
        _log_local({"ts": datetime.now(timezone.utc).isoformat(),
                    "event": "import_failure", "error": str(e), "text": text})
        return False
    try:
        key = slack_notifier.health_channel_key()
    except Exception:
        key = "ea-assistant"
    for attempt in (1, 2):
        try:
            if slack_notifier.post_to_slack(key, text,
                                            priority="urgent",
                                            _bypass_quiet=True):
                return True
        except Exception:
            pass
        if attempt == 1:
            time.sleep(3)
    try:
        if slack_notifier.post_to_slack(
                "personal",
                f"[EA SENTINEL — fallback channel]\n{text}",
                priority="urgent", _bypass_quiet=True):
            return True
    except Exception:
        pass
    _log_local({"ts": datetime.now(timezone.utc).isoformat(),
                "event": "alert_delivery_failed", "text": text})
    return False


def _load_state() -> dict:
    return _load_json(STATE_FILE)


def _save_state(state: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=2))
    except Exception:
        pass


def run_checks() -> list[dict]:
    results = []
    for name, fn in CHECKS:
        try:
            ok, detail = fn()
        except Exception as e:
            ok, detail = True, f"check crashed ({type(e).__name__}: {e}) — skipped"
        results.append({"check": name, "ok": ok, "detail": detail})
    return results


def cmd_check(as_json: bool, force_alert: bool) -> int:
    if not _ea_enabled():
        if as_json:
            print(json.dumps({"skipped": "ea_enabled is false"}))
        return 0

    results = run_checks()
    failing = [r for r in results if not r["ok"]]
    # Check names PLUS a digest of the details: a different failure of an
    # already-failing check must alert immediately; only the byte-identical
    # condition is held to the REALERT_HOURS reminder cadence.
    fingerprint = ",".join(sorted(
        f"{r['check']}:{hashlib.sha256(r['detail'].encode()).hexdigest()[:10]}"
        for r in failing))
    state = _load_state()
    prev_fp = state.get("fingerprint", "")
    last_alert = state.get("last_alert_ts", 0)
    now_ts = time.time()

    if as_json:
        print(json.dumps({"results": results, "fingerprint": fingerprint},
                         indent=2))

    if failing:
        should_alert = (
            force_alert
            or fingerprint != prev_fp
            or (now_ts - last_alert) >= REALERT_HOURS * 3600
        )
        if should_alert:
            lines = [f":rotating_light: *EA SENTINEL* — "
                     f"{len(failing)} problem(s) detected"]
            lines += [f"• {r['detail']}" for r in failing]
            lines.append(f"_re-alerts every {REALERT_HOURS}h while unchanged; "
                         f"recovery posts automatically_")
            delivered = _post_alert("\n".join(lines))
            _save_state({"fingerprint": fingerprint,
                         "last_alert_ts": now_ts if delivered else last_alert,
                         "failing_since": state.get("failing_since")
                         or datetime.now(timezone.utc).isoformat()})
            _log_local({"ts": datetime.now(timezone.utc).isoformat(),
                        "event": "alert", "delivered": delivered,
                        "failing": [r["check"] for r in failing]})
        else:
            # keep state, just note the still-failing tick
            _save_state({**state, "fingerprint": fingerprint})
        if not as_json:
            for r in failing:
                print(f"PROBLEM: {r['detail']}")
        return 1

    # All clear — post recovery exactly once if we were failing.
    if prev_fp:
        _post_alert(":white_check_mark: *EA SENTINEL* — all checks "
                    f"recovered (was: {prev_fp})")
        _log_local({"ts": datetime.now(timezone.utc).isoformat(),
                    "event": "recovered", "was": prev_fp})
    _save_state({})
    if not as_json:
        print("all checks ok")
    return 0


def cmd_status() -> int:
    for r in run_checks():
        mark = "OK " if r["ok"] else "FAIL"
        print(f"  [{mark}] {r['check']}: {r['detail']}")
    state = _load_state()
    if state.get("fingerprint"):
        print(f"\n  active alert fingerprint: {state['fingerprint']}")
        print(f"  failing since: {state.get('failing_since', '?')}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="EA dead-man's switch")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_check = sub.add_parser("check", help="run checks, alert on problems")
    p_check.add_argument("--json", action="store_true")
    p_check.add_argument("--force-alert", action="store_true",
                         help="alert even if fingerprint unchanged/recent")
    sub.add_parser("status", help="print check results, never alert")
    args = ap.parse_args()
    if args.cmd == "check":
        return cmd_check(args.json, args.force_alert)
    return cmd_status()


if __name__ == "__main__":
    sys.exit(main())

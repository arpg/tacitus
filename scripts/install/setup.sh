#!/usr/bin/env bash
#
# EA System Setup & Installation
#
# The install starts with ./install.sh, which opens Claude Code on the
# setup skill (.claude/skills/setup/SKILL.md); the skill runs this script.
# docs/SETUP.md is the reference for each step.
# Run once on your always-on machine.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"   # the checkout root; this script lives in scripts/install/
EA_DIR="${EA_DIR:-$HOME/.ea}"

# --profile email|review-only|assistant. Default: what ea-config.json says,
# else assistant. The profile decides the cron lines and whether the Slack
# bot service is installed.
#
# --non-interactive (or EA_NONINTERACTIVE=1): nothing reads the terminal.
# The two sign-ins (Graph, Google) are skipped, each with the one command
# the user runs, and the cron entries are saved to the owner-private
# template instead of installed, unless --install-cron says to install
# them. A run whose standard input is not a terminal is non-interactive
# whatever the flags say: Claude Code's Bash tool, a pipe and cron give
# no terminal, and a prompt there would fail. The setup skill and the
# install tests (tests/install/smoke.sh) run it this way.
#
# --install-cron: install the cron entries without asking.
PROFILE=""
NONINTERACTIVE="${EA_NONINTERACTIVE:-0}"
INSTALL_CRON=0
ACKNOWLEDGED=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --profile) PROFILE="$2"; shift 2 ;;
        --profile=*) PROFILE="${1#--profile=}"; shift ;;
        --acknowledged) ACKNOWLEDGED=1; shift ;;
        --non-interactive) NONINTERACTIVE=1; shift ;;
        --install-cron) INSTALL_CRON=1; shift ;;
        -h|--help) echo "usage: scripts/install/setup.sh [--profile email|review-only|assistant] [--acknowledged] [--non-interactive] [--install-cron]"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done
NONINTERACTIVE_WHY="--non-interactive"
if [[ "$NONINTERACTIVE" != 1 && ! -t 0 ]]; then
    NONINTERACTIVE=1
    NONINTERACTIVE_WHY="standard input is not a terminal"
fi
if [[ -z "$PROFILE" ]]; then
    PROFILE="$(EA_DIR="$EA_DIR" python3 "$SCRIPT_DIR/preprocessor/ea_config.py" profile 2>/dev/null || echo assistant)"
fi
case "$PROFILE" in
    email|review-only|assistant) ;;
    *) echo "unknown profile: $PROFILE (email|review-only|assistant)" >&2; exit 2 ;;
esac
HAS_SLACK=0
[[ "$PROFILE" == "assistant" ]] && HAS_SLACK=1

# Load config and secrets as data, never as shell (scripts/owner-io.sh env):
# once the ea-bot user exists, ~/.ea is its space and its .env supplies only
# non-secret keys.
if [[ -f "$EA_DIR/.env" || -f "$EA_DIR/.env.secrets" ]]; then
    while IFS= read -r -d '' _env_key && IFS= read -r -d '' _env_val; do
        if [[ "$_env_key" =~ ^[A-Z][A-Z0-9_]*$ ]]; then
            export "$_env_key=$_env_val"
        fi
    done < <("$SCRIPT_DIR/scripts/owner-io.sh" env "$EA_DIR" "${EA_PRIVATE_DIR:-$HOME/.ea-private}")
    unset _env_key _env_val
fi

echo "=== EA System Setup ==="
echo "Install location: $EA_DIR"
echo "Profile: $PROFILE"
[[ "$NONINTERACTIVE" == 1 ]] && echo "Non-interactive run ($NONINTERACTIVE_WHY): nothing here reads the terminal."
echo "For detailed instructions, see docs/SETUP.md"
echo ""

# 1. Python deps
echo "Installing Python dependencies..."
# The pip of the python3 the scripts run, not whichever `pip` is first on
# PATH (on macOS the Homebrew python has no bare `pip`).
# An externally managed interpreter (PEP 668: Debian, Ubuntu, Homebrew)
# refuses a plain install; the flag is passed only to one that is, and the
# install runs once, so its own error is the one the user sees.
PIP_FLAGS=""
if python3 -c 'import os, sys, sysconfig; sys.exit(0 if os.path.exists(os.path.join(sysconfig.get_path("stdlib"), "EXTERNALLY-MANAGED")) else 1)'; then
    PIP_FLAGS="--break-system-packages"
fi
if ! python3 -m pip install $PIP_FLAGS -r "$SCRIPT_DIR/requirements.txt"; then
    echo "ERROR: pip could not install requirements.txt into $(command -v python3) ($(python3 -V 2>&1))." >&2
    echo "  The lines above name the package. A Python newer than its published wheels" >&2
    echo "  is the usual cause: install Python 3.12 and run again with it first on PATH." >&2
    exit 1
fi

# Git hooks. The post-commit and post-merge hooks in .githooks deploy each
# commit, and each pull, on main to ea-bot's copy and /opt/ea-system.
# A git checkout that names no hooks directory gets this one; one that
# names another keeps it.
if [[ -d "$SCRIPT_DIR/.githooks" ]] && git -C "$SCRIPT_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    HOOKS_PATH="$(git -C "$SCRIPT_DIR" config --get core.hooksPath || true)"
    if [[ -z "$HOOKS_PATH" ]]; then
        git -C "$SCRIPT_DIR" config core.hooksPath .githooks
        echo "Git hooks: core.hooksPath set to .githooks; git pull on main now deploys through the post-merge hook."
    else
        echo "Git hooks: core.hooksPath is already $HOOKS_PATH; left as it is."
    fi
fi

# 2. Directory structure
echo "Setting up directory structure..."
mkdir -p "$EA_DIR"/{auth,state,logs,config,receipts,letters,templates}

# Writes into ~/.ea/config go through scripts/owner-io.sh: before
# scripts/install/setup-ea-user.sh the directory is the owner's and the write is a plain
# one; after it the directory belongs to ea-bot and the write runs as
# ea-bot through the sudoers rule that script installs. So this script
# runs the same way on a fresh install and on a re-run (a profile change,
# or pointing the cron lines at the deployed tree).
OWNER_IO="$SCRIPT_DIR/scripts/owner-io.sh"
config_write() {
    # stdin becomes $EA_DIR/config/$1. One line names the fix when the
    # write is refused, in place of the traceback a bare cp would give.
    if ! "$OWNER_IO" write "$EA_DIR/config/$1" 2>/dev/null; then
        echo "scripts/install/setup.sh: cannot write $EA_DIR/config/$1 as $(id -un): the config directory belongs to ea-bot and sudo refused to write it as ea-bot. Fix: sudo bash $SCRIPT_DIR/scripts/install/setup-ea-user.sh (it installs the sudoers rule), then run ./scripts/install/setup.sh --profile $PROFILE again." >&2
        exit 1
    fi
}

# Copy every config template (don't overwrite existing). Regular files
# only: config/examples/ is a directory of worked examples, not a template.
for path in "$SCRIPT_DIR"/config/*; do
    [[ -f "$path" ]] || continue
    f="$(basename "$path")"
    if [[ ! -f "$EA_DIR/config/$f" ]]; then
        config_write "$f" < "$path"
        echo "  Created $EA_DIR/config/$f"
    else
        echo "  $EA_DIR/config/$f already exists, skipping"
    fi
done

# The profile lives in ea-config.json; every lane reads it from there.
# --acknowledged records, once, the date the user read the "Your
# responsibility" paragraph of the README (the setup skill shows it and
# passes the flag on a yes). The Python prints the updated file, or
# exits 3 when the file already records both, so an unchanged config is
# never rewritten.
set +e
PROFILE_JSON=$("$OWNER_IO" read "$EA_DIR/config/ea-config.json" 2>/dev/null | python3 -c '
import datetime, json, sys
profile, acknowledged = sys.argv[1], sys.argv[2] == "1"
try:
    d = json.loads(sys.stdin.read() or "{}")
    if not isinstance(d, dict):
        d = {}
except Exception:
    d = {}
ack = d.get("acknowledged") if isinstance(d.get("acknowledged"), dict) else {}
want_ack = acknowledged and not ack.get("responsibility")
if d.get("profile") == profile and not want_ack:
    sys.exit(3)
d["profile"] = profile
if want_ack:
    ack["responsibility"] = datetime.date.today().isoformat()
    d["acknowledged"] = ack
print(json.dumps(d, indent=2, ensure_ascii=False))
' "$PROFILE" "${ACKNOWLEDGED:-0}")
PROFILE_RC=$?
set -e
if [[ "$PROFILE_RC" == 0 ]]; then
    printf '%s\n' "$PROFILE_JSON" | config_write ea-config.json
    echo "  Profile recorded: $PROFILE${ACKNOWLEDGED:+ (responsibility notice acknowledged)}"
elif [[ "$PROFILE_RC" == 3 ]]; then
    echo "  Profile already recorded: $PROFILE"
else
    echo "scripts/install/setup.sh: could not read $EA_DIR/config/ea-config.json to record the profile" >&2
    exit 1
fi

# Make scripts executable
chmod +x "$SCRIPT_DIR/run-agent.sh"

# 3. Check for .env
if [[ ! -f "$EA_DIR/.env" ]]; then
    echo ""
    echo "=== Environment file ==="
    if [[ "$HAS_SLACK" == 1 ]]; then
        echo "You need to create $EA_DIR/.env with your Slack channel ids."
        echo "See .env.example in this repo and docs/SETUP.md, 'Zulip server' or 'Slack', for details."
    else
        echo "No $EA_DIR/.env yet. Graph device-code sign-in needs GRAPH_CLIENT_ID and"
        echo "GRAPH_AUTH_METHOD=device_code there; python3 scripts/install/user_init.py --phase auth writes them."
    fi
    echo ""
else
    echo ""
    echo "Found $EA_DIR/.env"
fi

# 4. Graph API authentication
echo ""
echo "=== Microsoft Graph API Authentication ==="
# The sign-in command: the confidential client aligns its scopes as it
# signs in (docs/SETUP.md 'Microsoft Graph app registration').
GRAPH_SIGNIN="python3 $SCRIPT_DIR/preprocessor/graph_client.py"
[[ "${GRAPH_AUTH_METHOD:-}" == confidential ]] && GRAPH_SIGNIN="$GRAPH_SIGNIN --align --write"
# The project's published app signs in an install with no registration of
# its own; empty until the maintainer registers it (docs/published-oauth-apps.md).
PUBLISHED_GRAPH_ID="$(python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import published_apps; print(published_apps.PUBLISHED_GRAPH_CLIENT_ID)' "$SCRIPT_DIR/preprocessor" 2>/dev/null || true)"
if [[ "$NONINTERACTIVE" == 1 ]]; then
    # Never a sign-in here: with no token it would stop at a browser or a
    # prompt that nobody can answer.
    if [[ -f "$EA_DIR/auth/msal_token_cache.json" ]]; then
        echo "A Graph token is already in $EA_DIR/auth; the sign-in is skipped."
        echo "  Check it with: python3 $SCRIPT_DIR/preprocessor/graph_actions.py calendar-view --days 1"
    elif [[ -z "${GRAPH_CLIENT_ID:-}" && -z "$PUBLISHED_GRAPH_ID" ]]; then
        echo "No Graph sign-in yet, and no GRAPH_CLIENT_ID in $EA_DIR/.env: write the app"
        echo "registration first (python3 $SCRIPT_DIR/scripts/install/user_init.py --phase auth), then sign in with:"
        echo "  $GRAPH_SIGNIN"
    else
        echo "No Graph sign-in yet. Sign in yourself with this one command"
        echo "(in Claude Code, type it with a ! in front):"
        echo "  $GRAPH_SIGNIN"
    fi
else
    echo "You will be given a URL and a code. Visit the URL in any browser and enter the code."
    echo ""
    python3 "$SCRIPT_DIR/preprocessor/graph_client.py"
fi

# 5. Google Calendar authentication
echo ""
echo "=== Google Calendar Authentication ==="
echo "This authenticates your personal Google Calendar accounts."
echo ""
if [[ "$NONINTERACTIVE" == 1 ]]; then
    echo "Non-interactive run: the Google sign-in is skipped. It asks you to paste the"
    echo "address your browser lands on, so run it yourself in a terminal (docs/SETUP.md 'Google OAuth app'):"
    echo "  python3 $SCRIPT_DIR/preprocessor/google_calendar_client.py --setup"
elif [[ ! -f "$EA_DIR/auth/google_client_secret.json" ]]; then
    echo "Google client secret not found."
    echo "See docs/SETUP.md, 'Google OAuth app', for creating Google OAuth credentials."
    echo ""
    echo "Skipping Google Calendar auth for now. You can set it up later."
else
    python3 "$SCRIPT_DIR/preprocessor/google_calendar_client.py" --setup
    echo ""
    echo "Testing Google Calendar connectivity..."
    python3 "$SCRIPT_DIR/preprocessor/google_calendar_client.py" --test
fi

# 6. Initialize state
if [[ ! -f "$EA_DIR/state/obligations-ledger.json" ]]; then
    echo "[]" > "$EA_DIR/state/obligations-ledger.json"
fi
echo '{}' > "$EA_DIR/state/seen-messages.json" 2>/dev/null || true
echo '{}' > "$EA_DIR/state/seen-conversations.json" 2>/dev/null || true

# 7. Install crontab
echo ""
echo "=== Crontab Installation ==="
echo ""

# The cron jobs are owner processes (docs/SECURITY.md, "Owner trust rules").
# They run the committed code: the deployed tree that
# deploy/harden-permissions.sh seeds and the post-commit hook keeps at HEAD,
# or this checkout until that step has run. Their output goes to the
# owner-private log directory (mode 700, ea-bot cannot reach it), never to
# ~/.ea/logs, which is ea-bot space. EA_LIVE_DIR names another deployed tree.
LIVE_DIR="${EA_LIVE_DIR:-/opt/ea-system}"
if [[ -x "$LIVE_DIR/run-agent.sh" && -x "$LIVE_DIR/run-plugin.sh" ]]; then
    CODE_DIR="$LIVE_DIR"
else
    CODE_DIR="$SCRIPT_DIR"
    echo "No deployed tree at $LIVE_DIR: the jobs run from this checkout."
    echo "Run scripts/install/setup.sh again after deploy/harden-permissions.sh to point them at $LIVE_DIR."
    echo ""
fi
OWNER_PRIVATE_DIR="$HOME/.ea-private"
OWNER_LOG_DIR="$OWNER_PRIVATE_DIR/logs"
(umask 077 && mkdir -p "$OWNER_LOG_DIR")
chmod 700 "$OWNER_PRIVATE_DIR" "$OWNER_LOG_DIR"

echo "The following cron entries will be added:"
echo ""

# The lines a profile gets follow its features (preprocessor/ea_config.py):
# the chat checks, the intraday intake, deferred sends and the calendar
# reconciler need the chat platform and its buttons; the style sync needs
# drafting; the briefings, the ledger sync, the closure scrubber, the
# quiet-hours flush and the persistence digest run on every profile.
HAS_DRAFTING=$(EA_DIR="$EA_DIR" python3 "$SCRIPT_DIR/preprocessor/ea_config.py" features --profile "$PROFILE" 2>/dev/null \
    | python3 -c 'import json, sys; print(1 if json.load(sys.stdin).get("drafting") else 0)' 2>/dev/null || echo 1)

SLACK_CHECK_ENTRIES=""
SLACK_MAINTENANCE_ENTRIES=""
if [[ "$HAS_SLACK" == 1 ]]; then
SLACK_CHECK_ENTRIES="# Scheduling check: hourly during business hours (posts to chat)
0 8-20 * * 1-5     $CODE_DIR/run-agent.sh scheduling-check >> $OWNER_LOG_DIR/scheduling-check.log 2>&1

# Priority check: every 30 min during business hours (posts to chat)
*/30 8-18 * * 1-5  $CODE_DIR/run-agent.sh priority-check >> $OWNER_LOG_DIR/priority-check.log 2>&1

# Intraday ledger capture of needs-response mail (silent; the items
# surface on the desk and in the rooms, not as posts)
*/10 7-19 * * 1-5  $CODE_DIR/run-agent.sh intake-check >> $OWNER_LOG_DIR/intake-check.log 2>&1
"
SLACK_MAINTENANCE_ENTRIES="# Deferred sends (the Schedule button). Needs the signing key from docs/SETUP.md
# 'Cron schedule'; without it the pass refuses and sends nothing.
*/5 * * * *        python3 $CODE_DIR/preprocessor/scheduled_sends.py check >> $OWNER_LOG_DIR/scheduled-sends.log 2>&1

# Calendar protection reconciler: standing attendee-less commitments become
# p0 [auto] holds on the EA-Managed calendar (docs/SETUP.md, 'The EA-Managed calendar')
7,37 * * * *       python3 $CODE_DIR/preprocessor/calendar_reconciler.py --apply >> $OWNER_LOG_DIR/calendar-reconciler.log 2>&1
"
fi

STYLE_ENTRIES=""
if [[ "$HAS_DRAFTING" == 1 ]]; then
STYLE_ENTRIES="# Weekly: refresh the per-contact writing-style profiles from sent mail
20 4 * * 1         python3 $CODE_DIR/preprocessor/style_profiles.py sync --days 21 >> $OWNER_LOG_DIR/style-profiles.log 2>&1
"
fi

CRONTAB_ENTRIES="
# ======================================================
# EA System -- Preprocessor (zero Claude tokens), profile: $PROFILE
# ======================================================
${SLACK_CHECK_ENTRIES}
# The preprocessor runs inside each run-agent.sh job below, so it has no
# line of its own.

# ======================================================
# EA System -- Claude Code agents (Max subscription quota)
# ======================================================
# Morning briefing: hourly on weekdays; run-agent.sh runs it once, at 4 AM local
# (quiet_hours.py local-due), on the briefing model (EA_MODEL, else opus).
0 * * * 1-5        $CODE_DIR/run-agent.sh morning-briefing >> $OWNER_LOG_DIR/morning.log 2>&1

# Afternoon catch-up: 3 PM weekdays, on the same model
0 15 * * 1-5       $CODE_DIR/run-agent.sh afternoon-catchup >> $OWNER_LOG_DIR/afternoon.log 2>&1

# Weekly deep audit: Sunday 8 PM (Opus)
0 20 * * 0         $CODE_DIR/run-agent.sh weekly-audit --model opus >> $OWNER_LOG_DIR/weekly.log 2>&1

# ======================================================
# EA System -- Background maintenance
# ======================================================
# Ledger sync: closes rows on sent mail and archive actions between briefings
*/15 * * * *       python3 $CODE_DIR/preprocessor/ledger_sync.py check >> $OWNER_LOG_DIR/ledger-sync.log 2>&1

# Closure evidence between briefings (the briefing hours run it already)
20 7-14,16-21 * * * python3 $CODE_DIR/preprocessor/closure_scrubber.py check --json >> $OWNER_LOG_DIR/closure-scrubber-cron.log 2>&1

# Quiet hours: deliver posts held during the local night once the window ends
*/15 * * * *       python3 $CODE_DIR/preprocessor/quiet_hours.py flush >> $OWNER_LOG_DIR/quiet-hours.log 2>&1

# Persistence digest: each change to the ea-bot files that steer later
# sessions (settings, memory, config, plugins, shell and Python start-up
# files) goes to the health channel; ea-doctor.py fails after 36 hours without a run
40 6 * * *         python3 $CODE_DIR/scripts/persistence-digest.py >> $OWNER_LOG_DIR/persistence-digest.log 2>&1

# Standing-rule digest: each new or changed rule in preferences.json
30 6 * * *         python3 $CODE_DIR/scripts/preferences-digest.py >> $OWNER_LOG_DIR/preferences-digest.log 2>&1

# Outbound review: web requests and model web-tool calls that could carry mail out
45 6 * * *         python3 $CODE_DIR/scripts/outbound-review.py >> $OWNER_LOG_DIR/outbound-review.log 2>&1
${SLACK_MAINTENANCE_ENTRIES}${STYLE_ENTRIES}"

# ======================================================
# Personal plugins (scan ~/.ea/plugins/*/plugin.json for enabled entries)
# ======================================================
# Each enabled plugin gets one cron entry fenced by '# EA Plugin: <name>'
# markers so we can prune cleanly on re-run. Disabled plugins are skipped
# entirely. Missing/malformed manifests warn but don't block. The umask
# keeps the plugin log directories the owner's alone (mode 700).
PLUGIN_ENTRIES=$(umask 077; python3 - "$EA_DIR" "$CODE_DIR" "$OWNER_LOG_DIR" "$SCRIPT_DIR" <<'PYEOF'
import json, re, sys
from pathlib import Path

ea_dir = Path(sys.argv[1])
code_dir = Path(sys.argv[2])
log_dir = Path(sys.argv[3])
# The owner's lock is read with this checkout's own plugin_registry.
own_tree = Path(sys.argv[4])
plugins_dir = ea_dir / "plugins"
if not plugins_dir.exists():
    sys.exit(0)

# The manifests are ea-bot space and their fields become lines of the
# owner's crontab. So a field is used only when it has its shape: a name
# and each argument are plain words, a schedule holds cron fields only
# (no '%', which cron reads as a newline), and text that reaches a comment
# is one line of printable characters.
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
SCHEDULE = re.compile(r"@(?:reboot|yearly|annually|monthly|weekly|daily|hourly)"
                      r"|[0-9A-Za-z*/,-]+(?:[ \t]+[0-9A-Za-z*/,-]+){4}")
ARGS = re.compile(r"(?:[A-Za-z0-9_=.,:+/-]+(?:[ \t]+[A-Za-z0-9_=.,:+/-]+)*)?")


def shaped(pattern, value):
    """`value` with single spaces, or None when it is not a string that
    `pattern` matches in full."""
    if not isinstance(value, str) or not pattern.fullmatch(value.strip()):
        return None
    return " ".join(value.split())


def comment(value):
    """`value` as one line of printable text, for a comment line."""
    return re.sub(r"[^\x20-\x7e]", "?", str(value))[:200]


lines = []
lines.append("")
lines.append("# ======================================================")
lines.append("# EA Personal Plugins (~/.ea/plugins/*/plugin.json)")
lines.append("# ======================================================")
# The owner's lock is read with this checkout's own plugin_registry. A
# plugin the lock names runs from the lock whatever its live manifest
# says (a resident that rewrites a locked plugin's plugin.json as v1
# gets no cron line from it). An older registry without locked_names
# still handles a v2 manifest through get_plugin.
sys.path.insert(0, str(own_tree / "preprocessor"))
try:
    import plugin_registry
    registry_error = ""
except Exception as e:
    plugin_registry = None
    registry_error = comment(e)
try:
    locked_names = set(plugin_registry.locked_names()) if plugin_registry else set()
except Exception:
    locked_names = set()
for manifest_path in sorted(plugins_dir.glob("*/plugin.json")):
    try:
        m = json.loads(manifest_path.read_text())
        if not isinstance(m, dict):
            raise ValueError("not a JSON object")
    except Exception as e:
        lines.append(f"# WARN: bad manifest {comment(manifest_path)}: {comment(e)}")
        continue
    locked = (manifest_path.parent.name in locked_names
              or str(m.get("name") or "") in locked_names)
    if m.get("manifest_version") == 2 or locked:
        # The lock says whether the plugin is enabled and holds the
        # schedules the owner accepted (docs/plugins.md).
        name = shaped(NAME, m.get("name") or manifest_path.parent.name)
        if not name:
            lines.append(f"# WARN: plugin in {comment(manifest_path.parent.name)} "
                         "has a name that is not a plain word; skipping")
            continue
        try:
            if plugin_registry is None:
                raise RuntimeError(registry_error or "plugin_registry is not importable")
            lp = plugin_registry.get_plugin(name)
        except Exception as e:
            lines.append(f"# WARN: plugin '{name}' (locked): the owner's lock "
                         f"is unreadable: {comment(e)}")
            continue
        if lp is None:
            continue
        log_path = f"{log_dir}/plugins/{name}/$(date +\\%Y-\\%m-\\%d).log"
        (log_dir / "plugins" / name).mkdir(parents=True, exist_ok=True)
        for sched in lp.manifest["capabilities"]["schedules"]:
            label = comment(sched.get("label", "default"))
            cron = shaped(SCHEDULE, sched.get("cron"))
            args = shaped(ARGS, " ".join(str(a) for a in sched.get("args") or []))
            if not cron or args is None:
                lines.append(f"# WARN: plugin '{name}' ({label}) has a schedule or "
                             "arguments that are not plain; skipping")
                continue
            arg_str = f" -- {args}" if args else ""
            lines.append(f"# EA Plugin: {name} ({label})")
            lines.append(f"{cron}  {code_dir}/run-plugin.sh {name}{arg_str} >> {log_path} 2>&1")
        continue
    if not m.get("enabled"):
        continue
    name = shaped(NAME, m.get("name") or manifest_path.parent.name)
    if not name:
        lines.append(f"# WARN: plugin in {comment(manifest_path.parent.name)} "
                     "has a name that is not a plain word; skipping")
        continue
    if not m.get("schedule"):
        lines.append(f"# WARN: plugin '{name}' has no schedule; skipping")
        continue
    schedule = shaped(SCHEDULE, m.get("schedule"))
    if not schedule:
        lines.append(f"# WARN: plugin '{name}' has a schedule that is not cron fields; skipping")
        continue
    log_path = f"{log_dir}/plugins/{name}/$(date +\\%Y-\\%m-\\%d).log"
    # The cron line redirects INTO this directory, so the shell opens the
    # file before run-plugin.sh starts. run-plugin.sh mkdirs it too, but
    # too late for its own cron line: without this the first scheduled run
    # of a new plugin dies at the redirect.
    (log_dir / "plugins" / name).mkdir(parents=True, exist_ok=True)
    lines.append(f"# EA Plugin: {name}")
    lines.append(f"{schedule}  {code_dir}/run-plugin.sh {name} >> {log_path} 2>&1")
    # Optional secondary schedules: list of {"cron": "...", "args": "..."}
    # entries, for a plugin whose nightly pass takes other collect.py
    # arguments than its daily one.
    extras = m.get("extra_schedules") or []
    if not isinstance(extras, list):
        extras = []
    for ex in extras:
        if not isinstance(ex, dict) or not ex.get("cron"):
            continue
        ex_label = comment(ex.get("label", "extra"))
        ex_cron = shaped(SCHEDULE, ex.get("cron"))
        ex_args = shaped(ARGS, ex.get("args") or "")
        if not ex_cron or ex_args is None:
            lines.append(f"# WARN: plugin '{name}' ({ex_label}) has a schedule or arguments "
                         "that are not plain; skipping")
            continue
        arg_str = f" -- {ex_args}" if ex_args else ""
        lines.append(f"# EA Plugin: {name} ({ex_label})")
        lines.append(f"{ex_cron}  {code_dir}/run-plugin.sh {name}{arg_str} >> {log_path} 2>&1")
print("\n".join(lines))
PYEOF
)
CRONTAB_ENTRIES="${CRONTAB_ENTRIES}${PLUGIN_ENTRIES}"

echo "$CRONTAB_ENTRIES"
echo ""
if [[ "$INSTALL_CRON" == 1 ]]; then
    REPLY=y
    echo "--install-cron: installing the cron entries."
elif [[ "$NONINTERACTIVE" == 1 ]]; then
    REPLY=n
    echo "Non-interactive run: the cron entries are saved, not installed (--install-cron installs them)."
else
    read -p "Install these cron entries? [y/N] " -n 1 -r
    echo ""
fi

if [[ $REPLY =~ ^[Yy]$ ]]; then
    # Strip EA core entries, the maintenance lines and any prior plugin
    # entries (we re-emit them from the current profile and manifest scan
    # above). No crontab yet, or no line left to keep, is not an error.
    KEPT_ENTRIES="$(crontab -l 2>/dev/null \
        | grep -v -e "EA System" -e "EA Personal Plugins" -e "EA Plugin:" \
                  -e "run-agent.sh" -e "run-plugin.sh" -e "preprocess.py" \
                  -e "preprocessor/ledger_sync.py" -e "preprocessor/closure_scrubber.py" \
                  -e "preprocessor/quiet_hours.py" -e "preprocessor/scheduled_sends.py" \
                  -e "preprocessor/calendar_reconciler.py" -e "preprocessor/style_profiles.py" \
                  -e "scripts/persistence-digest.py" -e "scripts/preferences-digest.py" \
                  -e "scripts/outbound-review.py" || true)"
    printf '%s\n%s\n' "$KEPT_ENTRIES" "$CRONTAB_ENTRIES" | crontab -
    echo "Crontab installed."
else
    # The saved entries become owner cron lines when installed, so they are
    # kept where only the owner can change them, not in ~/.ea.
    CRONTAB_TEMPLATE="$OWNER_PRIVATE_DIR/crontab-template.txt"
    echo "Skipped. You can install manually with: crontab -e"
    echo "Entries saved to $CRONTAB_TEMPLATE"
    (umask 077 && echo "$CRONTAB_ENTRIES" > "$CRONTAB_TEMPLATE")
fi

# 8. The chat bot service (assistant profile)
# The bot runs as a system unit (Linux) or a LaunchDaemon (macOS), both
# written by a root step once the chat platform is configured; this script
# runs as the owner and writes neither. docs/SETUP.md 'Services'.
if [[ "$HAS_SLACK" == 1 ]]; then
echo ""
echo "=== Chat bot service ==="
if [[ "$(uname -s)" == Darwin ]]; then
    echo "The bot runs as a LaunchDaemon. After the chat platform is configured:"
    echo "  sudo bash $SCRIPT_DIR/deploy/macos/install-services.sh"
else
    echo "The bot runs as the system unit ea-chat-bot.service. After the chat platform"
    echo "is configured, this writes it, hardened, and starts it:"
    echo "  sudo bash $SCRIPT_DIR/deploy/harden-permissions.sh"
fi
else
echo ""
echo "Profile $PROFILE has no chat platform: no bot service. Reports land in $EA_DIR/state/reports/<agent>-latest.md and go out by phone push when ~/.ea-private/push.json exists."
fi

# 9. Next steps
echo ""
echo "=== Setup Complete ==="
echo ""
echo "Next steps:"
echo "  1. Edit $EA_DIR/config/user-profile.json with your details"
echo "  2. Edit $EA_DIR/config/scheduling-preferences.json with your calendars"
echo "  3. Edit $EA_DIR/config/vip_senders.txt with your VIP contacts"
echo "  4. Add your projects: python3 $SCRIPT_DIR/preprocessor/project_store.py add --help"
echo "  5. Enable the Gmail and Google Calendar connectors (and Slack on a Slack install) in"
echo "     claude.ai Settings > Connectors for the account ea-bot logs in with; check with"
echo "     claude mcp list as ea-bot (docs/SETUP.md 'Claude Code connectors')"
echo "  6. Check: python3 $SCRIPT_DIR/scripts/ea-doctor.py --profile $PROFILE"
echo "  7. Test with: ./run-agent.sh morning-briefing --now   (--now skips the 4 AM gate)"
echo ""
echo "All times are in your machine's local timezone ($(date +%Z))."
echo "For detailed setup instructions, see docs/SETUP.md"

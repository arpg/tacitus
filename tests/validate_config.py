#!/usr/bin/env python3
"""
Validate EA configuration files.
Checks that required fields exist and aren't placeholders.

Usage:
    python3 tests/validate_config.py           # check ~/.ea/config/
    python3 tests/validate_config.py --ci      # check config/ templates (CI mode)
"""
import json
import os
import sys
from pathlib import Path

# --ci or CI=1: both mean "templates only, never read live ~/.ea".
CI_MODE = "--ci" in sys.argv or os.environ.get("CI") == "1"
CONFIG_DIR = Path("config") if CI_MODE else Path.home() / ".ea" / "config"

PASS = 0
FAIL = 0

PLACEHOLDER_VALUES = {
    "Your Name", "Your First Name", "Your Last Name",
    "you@example.edu", "you@gmail.com",
    "ai-assistant-yourname", "", "CYOUR_CHANNEL_ID",
}


def green(msg):
    global PASS
    print(f"  \033[32mPASS\033[0m {msg}")
    PASS += 1


def red(msg):
    global FAIL
    print(f"  \033[31mFAIL\033[0m {msg}")
    FAIL += 1


def check_json(path, required_keys, placeholder_ok=False):
    """Check a JSON file exists, parses, and has required keys."""
    if not path.exists():
        red(f"{path.name} not found at {path}")
        return None
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        red(f"{path.name} invalid JSON: {e}")
        return None
    green(f"{path.name} valid JSON")

    for key in required_keys:
        # Support nested keys like "submitter.email"
        parts = key.split(".")
        val = data
        for p in parts:
            if isinstance(val, dict):
                val = val.get(p)
            else:
                val = None
                break

        if val is None:
            red(f"{path.name}: missing key '{key}'")
        elif not placeholder_ok and isinstance(val, str) and val in PLACEHOLDER_VALUES:
            red(f"{path.name}: '{key}' is still a placeholder ({val!r})")
        else:
            green(f"{path.name}: '{key}' is set")

    return data


# ---- user-profile.json ----
print("User profile:")
check_json(
    CONFIG_DIR / "user-profile.json",
    ["name", "email", "role_description", "department", "slack_channel_name"],
    placeholder_ok=CI_MODE,
)

# ---- scheduling-preferences.json ----
print("\nScheduling preferences:")
check_json(
    CONFIG_DIR / "scheduling-preferences.json",
    # working_hours does not belong here: hours/lunch/no-meeting prefs
    # live as tokened recurring events (wh/nm/p0/p1/p2/wr) on the
    # EA-Managed calendar, and the solver reads them live. Its absence
    # is the DESIGNED state, so the validator must not demand it.
    ["timezone", "calendars"],
    placeholder_ok=CI_MODE,
)

# ---- autonomy-tiers.json ----
print("\nAutonomy tiers:")
check_json(
    CONFIG_DIR / "autonomy-tiers.json",
    ["task_tiers"],
    placeholder_ok=CI_MODE,
)

# ---- reimbursement-config.json ----
print("\nReimbursement config:")
check_json(
    CONFIG_DIR / "reimbursement-config.json",
    ["form_url", "submitter.first_name", "submitter.email"],
    placeholder_ok=CI_MODE,
)

# ---- ea-config.json (the template only) ----
# A live file may leave update out: an install the post-commit hook deploys
# has no channel (docs/updates.md), so only the template must carry one.
if CI_MODE:
    print("\nOperational config:")
    data = check_json(CONFIG_DIR / "ea-config.json", ["profile", "update.channel"],
                      placeholder_ok=True)
    channel = ((data or {}).get("update") or {}).get("channel")
    if channel in ("auto", "latest", "stable", "off"):
        green(f"ea-config.json: update.channel is {channel!r}")
    elif data is not None:
        red(f"ea-config.json: update.channel {channel!r} is not auto, latest, stable or off")
    # The routing block (preprocessor/model_routing.py): every task named,
    # each local, hosted or auto, and no value under a floor.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "preprocessor"))
    import ea_config
    routing = (data or {}).get("routing")
    named = set(((routing or {}).get("tasks") or {}) if isinstance(routing, dict) else ())
    problems = ea_config.routing_problems(routing)
    missing = [t for t in ea_config.ROUTING_TASKS if t not in named]
    if data is not None and not problems and not missing:
        green("ea-config.json: routing names every task with a value the floors allow")
    elif data is not None:
        red(f"ea-config.json: routing: {'; '.join(problems + [f'{t} is not named' for t in missing])}")

def _chat_frontend() -> str:
    try:
        cfg = json.loads((CONFIG_DIR / "ea-config.json").read_text())
        return str((cfg.get("chat") or {}).get("frontend") or "slack")
    except (OSError, ValueError):
        return "slack"


# ---- .env (only in local mode) ----
if not CI_MODE:
    print("\nEnvironment file:")
    env_path = Path.home() / ".ea" / ".env"
    if env_path.exists():
        green(f".env exists")
        # The Slack tokens live in .env.secrets beside it.
        env_text = env_path.read_text()
        secrets_path = env_path.parent / ".env.secrets"
        try:
            env_text += "\n" + secrets_path.read_text()
        except OSError:
            pass
        slack_vars = (["SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "EA_SLACK_CHANNEL_ID",
                       "EA_SLACK_CHANNEL_NAME"] if _chat_frontend() == "slack" else [])
        if not slack_vars:
            print(f"  chat frontend is {_chat_frontend()}: no Slack variables needed")
        for var in slack_vars:
            found = False
            for line in env_text.splitlines():
                if line.startswith(f"{var}="):
                    val = line.split("=", 1)[1].strip()
                    if val and "your-" not in val.lower() and "YOUR" not in val:
                        green(f"{var} is set")
                    else:
                        red(f"{var} is placeholder")
                    found = True
                    break
            if not found:
                red(f"{var} not in .env")
    else:
        red(f".env not found at {env_path}")

# ---- Summary ----
print(f"\n{'='*40}")
print(f"{PASS} passed, {FAIL} failed")
if FAIL == 0:
    print("All config checks passed!")
sys.exit(min(FAIL, 1))

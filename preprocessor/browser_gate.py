"""Owner approval for an exact form, answer plan and upload fingerprints.

Reads remain public. Data entry is an external disclosure even before
Submit (autosave and upload widgets), so fill and submit both need the
existing owner-card mechanism. No owner browser or credential is used.
"""
from __future__ import annotations

import hashlib
import json
import os
import pwd
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import plugin_cards as C

STAGE_FILE = "pending-browser.json"
CARD_ID = "browser_form"
TOKEN_ENV = "EA_BROWSER_APPROVAL"


def origin(url: str) -> str:
    if any(c.isspace() or ord(c) < 32 for c in url):
        raise ValueError("form URL contains whitespace or control characters")
    p = urlsplit(url)
    if p.scheme != "https" or not p.hostname or p.username or p.password:
        raise ValueError("form actions require an HTTPS URL without credentials")
    host = p.hostname.lower().encode("idna").decode("ascii")
    if ":" in host:
        host = f"[{host}]"
    return f"https://{host}" + (f":{p.port}" if p.port and p.port != 443 else "")


def validate(item: dict) -> dict:
    if not isinstance(item, dict) or set(item) != {
        "verb", "url", "actions", "origins", "submit_selector", "success_text"}:
        raise ValueError("invalid browser approval item")
    if item["verb"] not in {"fill", "submit"} or not isinstance(item["url"], str):
        raise ValueError("invalid browser action or URL")
    start = origin(item["url"])
    origins = item["origins"]
    if not isinstance(origins, list) or not 1 <= len(origins) <= 8:
        raise ValueError("approve between one and eight exact HTTPS origins")
    if any(not isinstance(o, str) or origin(o) != o for o in origins) or start not in origins:
        raise ValueError("origins must be exact HTTPS origins and include the form URL")
    if not isinstance(item["actions"], list) or len(item["actions"]) > 80:
        raise ValueError("invalid browser action plan")
    if len(json.dumps(item)) > 128 * 1024:
        raise ValueError("browser approval exceeds 128 KiB")
    if not all(isinstance(item[k], str) for k in ("submit_selector", "success_text")):
        raise ValueError("invalid submission selectors")
    if item["verb"] == "submit" and not all(item[k] for k in ("submit_selector", "success_text")):
        raise ValueError("submission needs its control and completion text")
    return item


def digest(item: dict) -> str:
    return hashlib.sha256(json.dumps(validate(item), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def mint(item: dict) -> str:
    validate(item)
    if not C._owner_trust.is_owner_process() or not C.mediated_install():
        raise ValueError("generic form writes require the owner's mediated confirmation service")
    return C.mint_token("core", CARD_ID, item)


def require(item: dict):
    if not C.mediated_install():
        raise ValueError("generic form writes require the owner's mediated confirmation service")
    raw = os.environ.get(TOKEN_ENV, "")
    if not raw or not C.consume_token(raw, "core", CARD_ID, validate(item)):
        raise ValueError("invalid, expired, used, or changed browser approval; stage this form again")


def render(item: dict) -> dict:
    """Never truncate a reviewed value into a different approved payload.

    Keep the whole card bounded; an oversized plan is refused rather than
    offering an approval button that cannot display what it authorizes.
    """
    validate(item)
    lines = [f"Form: {item['url']}", "Permitted origins: " + ", ".join(item["origins"]),
             "Entering answers and uploads can transmit data before Submit."]
    for a in item["actions"]:
        target = json.dumps(a.get("label") or a.get("selector"), ensure_ascii=False)
        if a.get("action") == "upload":
            lines.append(f"Upload {json.dumps(a.get('path'))} to {target}; SHA-256 {a.get('sha256')}")
        else:
            lines.append(f"{a.get('action')} {target}: {json.dumps(a.get('value', True), ensure_ascii=False)}")
    if item["verb"] == "submit":
        lines += ["Final control: " + json.dumps(item["submit_selector"]),
                  "Expected confirmation: " + json.dumps(item["success_text"])]
    # Refuse inert/hidden formatting rather than let a page disguise the
    # target or values on the card. HTML/Markdown renderer safety is shared.
    for line in lines:
        if C._ct.strip_inert(line) != line or C._ct.clean(line) != line:
            raise ValueError("form approval contains hidden formatting; use plain text")
    if len(lines) > 30 or sum(len(line) for line in lines) > 6000 or any(len(line) > 2000 for line in lines):
        raise ValueError("form plan is too large to review on one card; split it into smaller steps")
    return {"title": "Submit web form" if item["verb"] == "submit" else "Send answers to web form",
            "lines": [C._ct.inert(line, limit=2000) for line in lines],
            "label": "Submit form" if item["verb"] == "submit" else "Fill form"}


def execute(item: dict, token: str, *, tree: Path, ea_dir: str, runner=None) -> dict:
    validate(item)
    home = Path(pwd.getpwnam("ea-bot").pw_dir)
    # The plan is inline, so the resident cannot replace a plan file after
    # the click. Uploaded bytes are fingerprinted again by the worker.
    from plugin_cards import resident_python
    argv = ["sudo", "-n", "-u", "ea-bot", "env", "-i", f"HOME={home}",
            "PATH=/usr/local/bin:/usr/bin:/bin", f"EA_DIR={Path(ea_dir).resolve()}",
            "EA_BROWSER_DIRECT=1", f"{TOKEN_ENV}={token}", resident_python(home, Path(tree)),
            str(Path(tree) / "preprocessor" / "web_browser.py"), item["verb"],
            "--url", item["url"], "--actions-json", json.dumps(item["actions"]),
            "--origins-json", json.dumps(item["origins"])]
    if item["verb"] == "submit":
        argv += ["--submit-selector", item["submit_selector"], "--success-text", item["success_text"]]
    try:
        p = (runner or subprocess.run)(argv, capture_output=True, text=True, timeout=120,
                                       cwd="/", env={"PATH": "/usr/bin:/bin"})
        return json.loads(p.stdout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "submitted": None, "error": "Browser timed out. Status unknown; do not retry automatically."}
    except (OSError, ValueError) as e:
        return {"ok": False, "submitted": None, "error": str(e)[:500]}

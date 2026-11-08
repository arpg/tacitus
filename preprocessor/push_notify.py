#!/usr/bin/env python3
"""Phone push for installs with no chat platform, through ntfy.

A `reviewer` install has no chat channel: a report is a
file under ~/.ea/state/reports/ and a line on the web board. When the
owner sets up a push, run-agent.sh calls this module after it writes a
report, and the phone shows a short notice.

The settings sit in the owner-private `push.json` (beside secrets.env),
never in ea-config.json. ea-config.json is ea-bot space, and a
destination that an injected EA could rewrite would carry report text and
the topic token to a server of its choice.

  {"ntfy_url": "https://ntfy.sh",
   "topic": "<a long random name: on a public server it is the password>",
   "include_text": false,
   "click": "https://<the web board, optional>"}

With `include_text` false (the default) the push carries the report's
name only, and no mail content leaves the machine. With true it carries
the report text, cut to the ntfy message limit. A token for a protected
topic goes in secrets.env as NTFY_TOKEN. Plain http is accepted only for
a server on this machine.

  python3 push_notify.py report --agent morning-briefing < report.md
  python3 push_notify.py check    # validate the settings; sends nothing
  python3 push_notify.py test     # one test push

`report` exits 0 when it sent the push or when no push is set up, and 1
when a push is set up and the send failed.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import owner_trust as _owner_trust  # noqa: E402

SETTINGS_FILE = "push.json"
TOKEN_NAME = "NTFY_TOKEN"
# ntfy turns a longer message into an attachment; stay under it.
MAX_TEXT_BYTES = 3800
MIN_TOPIC_LEN = 16
TIMEOUT_S = 10
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


def settings_path() -> Path:
    return _owner_trust.private_dir() / SETTINGS_FILE


def load_settings() -> dict | None:
    """The push settings, or None when no push is set up. Read only in an
    owner process and only through the owner checks; raises
    OwnerTrustError when the file is there but fails them."""
    if not _owner_trust.is_owner_process():
        return None
    try:
        raw = _owner_trust.read_owner_file(settings_path())
    except FileNotFoundError:
        return None
    data = json.loads(raw.decode("utf-8"))
    return data if isinstance(data, dict) else {}


def problems(settings: dict) -> list[str]:
    """What is wrong with the settings, one line each; [] when usable."""
    out = []
    url = str(settings.get("ntfy_url") or "").strip()
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme == "https" and parsed.hostname:
        pass
    elif parsed.scheme == "http" and parsed.hostname in LOCAL_HOSTS:
        pass
    else:
        out.append("ntfy_url must be an https URL (http only for a server on this machine)")
    topic = str(settings.get("topic") or "").strip()
    if not topic:
        out.append("topic is missing")
    elif "/" in topic or not topic.replace("-", "").replace("_", "").isalnum():
        out.append("topic may hold letters, digits, '-' and '_' only")
    click = str(settings.get("click") or "").strip()
    if click and urllib.parse.urlparse(click).scheme not in ("https", "http"):
        out.append("click must be a web URL")
    return out


def warnings(settings: dict) -> list[str]:
    """Advice that does not stop a send."""
    out = []
    topic = str(settings.get("topic") or "").strip()
    host = urllib.parse.urlparse(str(settings.get("ntfy_url") or "")).hostname or ""
    if topic and len(topic) < MIN_TOPIC_LEN and host not in LOCAL_HOSTS:
        out.append(f"topic is shorter than {MIN_TOPIC_LEN} characters; on a shared server "
                   "anyone who guesses it reads the pushes")
    if settings.get("include_text") is True:
        out.append("include_text is true: report text, mail summaries included, goes to the ntfy server")
    return out


def title_for(agent: str) -> str:
    """'morning-briefing' -> 'EA: Morning briefing'."""
    words = agent.replace("_", "-").split("-")
    text = " ".join(w for w in words if w)
    return "EA: " + (text[:1].upper() + text[1:] if text else "report")


def _cut(text: str, limit: int = MAX_TEXT_BYTES) -> str:
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    return data[:limit].decode("utf-8", errors="ignore").rstrip() + "\n..."


def build_request(settings: dict, title: str, text: str, token: str = "") -> urllib.request.Request:
    """The POST for one push. The title travels as a header, so it is kept
    to ASCII."""
    url = str(settings["ntfy_url"]).strip().rstrip("/") + "/" + str(settings["topic"]).strip()
    if settings.get("include_text") is True and text.strip():
        body = _cut(text.strip())
    else:
        body = "A new report is ready on the board and in ~/.ea/state/reports/."
    headers = {"Title": title.encode("ascii", errors="ignore").decode("ascii") or "EA",
               "Tags": "memo"}
    if settings.get("include_text") is True:
        headers["Markdown"] = "yes"
    click = str(settings.get("click") or "").strip()
    if click:
        headers["Click"] = click
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return urllib.request.Request(url, data=body.encode("utf-8"), headers=headers, method="POST")


def send(req: urllib.request.Request, opener=None) -> None:
    """Send one push; raises OSError (urllib.error.URLError included) or
    RuntimeError on a non-2xx answer."""
    opener = opener or urllib.request.urlopen
    with opener(req, timeout=TIMEOUT_S) as resp:
        status = getattr(resp, "status", 200)
        if not 200 <= int(status) < 300:
            raise RuntimeError(f"ntfy answered HTTP {status}")


def push(agent: str, text: str, *, settings: dict | None = None, token: str | None = None,
         opener=None) -> str:
    """Push one report. Returns 'sent', 'off' (no push set up) or
    'invalid: <why>'; raises when the send itself fails."""
    if settings is None:
        settings = load_settings()
    if settings is None:
        return "off"
    bad = problems(settings)
    if bad:
        return "invalid: " + "; ".join(bad)
    if token is None:
        token = _owner_trust.owner_secret(TOKEN_NAME)
    send(build_request(settings, title_for(agent), text, token), opener=opener)
    return "sent"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("report", help="push one report; its text on stdin")
    r.add_argument("--agent", required=True)
    sub.add_parser("check", help="validate the settings; sends nothing")
    sub.add_parser("test", help="send one test push")
    a = ap.parse_args(argv)
    try:
        settings = load_settings()
    except (_owner_trust.OwnerTrustError, ValueError, OSError) as e:
        print(f"push_notify: {settings_path()} refused: {e}", file=sys.stderr)
        return 1
    if a.cmd == "check":
        if settings is None:
            print(f"no push set up ({settings_path()} is absent)")
            return 0
        for w in warnings(settings):
            print(f"warning: {w}")
        bad = problems(settings)
        for b in bad:
            print(f"error: {b}")
        return 1 if bad else 0
    if settings is None:
        print("push_notify: off (no push set up)")
        return 0
    text = sys.stdin.read() if a.cmd == "report" else "A test push from the EA."
    agent = a.agent if a.cmd == "report" else "test"
    try:
        result = push(agent, text, settings=settings)
    except (OSError, RuntimeError) as e:
        print(f"push_notify: send failed: {e}", file=sys.stderr)
        return 1
    if result.startswith("invalid"):
        print(f"push_notify: {result}", file=sys.stderr)
        return 1
    print(f"push_notify: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

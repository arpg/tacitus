#!/usr/bin/env python3
"""Watch a web page for a change and tell the owner loudly when it comes.

The case this exists for: a ticket page that says "sold out, waitlist
full, check back regularly". Nobody checks back regularly; a cron does.
A watch names a URL and a sentence that must disappear (`--absent`) or
appear (`--present`). `check` fetches every due watch, and when the
condition holds it sends the owner a direct message (chat.direct), posts
the same line to the personal channel, and notes it on the linked
ledger row. A watch fires once per flip: it is quiet until the page
goes back and changes again.

  page_watch.py add --id show-tickets --url URL --absent "Tickets are sold out" \\
      --message "Show page changed: spots may have opened" [--ledger-id ID] [--every 15]
  page_watch.py list
  page_watch.py remove --id show-tickets
  page_watch.py check [--id show-tickets] [--force]

Watches live in state/page-watches.json (group-writable, so any lane can
add one); last results in state/page-watch-state.json. `check` runs from
the owner's cron because the direct message needs the owner's chat
credential.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import resident_io  # noqa: E402  (writes into resident space go through it)
EA_DIR = Path(os.environ.get("EA_DIR", str(Path.home() / ".ea")))
WATCHES = EA_DIR / "state" / "page-watches.json"
STATE = EA_DIR / "state" / "page-watch-state.json"
FETCH = [sys.executable, str(HERE / "fetch_url.py")]
LEDGER = [sys.executable, str(HERE / "ledger.py")]
FETCH_CHARS = 60000


def _load(path: Path, default):
    text = resident_io.read_text(path)
    if not text:
        return default
    try:
        return json.loads(text)
    except Exception:
        return default


def _save(path: Path, data) -> None:
    resident_io.make_dir(path.parent)
    if not resident_io.replace_text(path, json.dumps(data, indent=1, ensure_ascii=False, default=str) + "\n"):
        raise RuntimeError(f"could not write {path}")


def fetch(url: str) -> str:
    """The page's extracted text, rendered with JavaScript when the plain
    fetch comes back thin."""
    def run(js: bool) -> str:
        cmd = FETCH + ["--url", url, "--max-chars", str(FETCH_CHARS)] + (["--js"] if js else [])
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        return r.stdout or ""
    text = run(False)
    if len(text.strip()) < 500:
        text = run(True) or text
    return re.sub(r"\s+", " ", text)


def condition_holds(text: str, watch: dict) -> tuple[bool, str]:
    """(fired, why). `absent` fires when the pattern is gone; `present`
    when it is there. An empty page never fires: a fetch failure is not
    a change."""
    if len(text.strip()) < 200:
        return False, "page too short to judge"
    if watch.get("absent"):
        hit = re.search(watch["absent"], text, re.IGNORECASE)
        return (hit is None), ("pattern gone" if hit is None else "pattern still present")
    if watch.get("present"):
        hit = re.search(watch["present"], text, re.IGNORECASE)
        return (hit is not None), ("pattern appeared" if hit else "pattern not yet present")
    return False, "watch has no pattern"


def notify(watch: dict, why: str) -> dict:
    line = f":rotating_light: {watch['message']}\n{watch['url']}\n({why}; watch `{watch['id']}`)"
    out: dict = {}
    try:
        from chat import direct  # type: ignore
        out["direct"] = direct.send_direct(line)
    except Exception as e:
        out["direct_error"] = str(e)
    try:
        from chat import direct  # type: ignore
        out["web"] = direct.web_notify(line, tag=f"watch:{watch['id']}")
    except Exception as e:
        out["web_error"] = str(e)
    try:
        import chat_notify  # type: ignore
        out["posted"] = chat_notify.post("personal", line, priority="urgent", _bypass_quiet=True)
    except Exception as e:
        out["post_error"] = str(e)
    if watch.get("ledger_id"):
        stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
        subprocess.run(LEDGER + ["update", "--id", watch["ledger_id"], "--last-action",
                                 f"{stamp}: page watch `{watch['id']}` fired ({why}): {watch['url']}"],
                       capture_output=True, text=True, timeout=60,
                       env={**os.environ, "EA_DIR": str(EA_DIR)})
    return out


def check(only: str = "", force: bool = False) -> dict:
    watches = _load(WATCHES, [])
    state = _load(STATE, {})
    now = datetime.now(timezone.utc)
    report = []
    for w in watches:
        wid = w["id"]
        if only and wid != only:
            continue
        st = state.setdefault(wid, {})
        every = int(w.get("every", 15))
        last = st.get("last_checked")
        if last and not force:
            try:
                if now - datetime.fromisoformat(last) < timedelta(minutes=every):
                    report.append({"id": wid, "skipped": f"checked {last}"})
                    continue
            except Exception:
                pass
        try:
            text = fetch(w["url"])
            fired, why = condition_holds(text, w)
            st.update({"last_checked": now.isoformat(), "last_chars": len(text),
                       "last_result": why, "error": ""})
        except Exception as e:
            st.update({"last_checked": now.isoformat(), "error": str(e)[:200]})
            st["errors"] = int(st.get("errors") or 0) + 1
            report.append({"id": wid, "error": str(e)[:200]})
            continue
        st["errors"] = 0
        was_firing = bool(st.get("firing"))
        st["firing"] = fired
        entry = {"id": wid, "fired": fired, "why": why, "chars": len(text)}
        if fired and not was_firing:
            entry["notified"] = notify(w, why)
            st["fired_at"] = now.isoformat()
            st["fired_count"] = int(st.get("fired_count") or 0) + 1
        report.append(entry)
    _save(STATE, state)
    return {"checked_at": now.isoformat(), "watches": report}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 allow_abbrev=False)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("add", allow_abbrev=False)
    p.add_argument("--id", required=True)
    p.add_argument("--url", required=True)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--absent", help="regex that must DISAPPEAR for the watch to fire")
    g.add_argument("--present", help="regex that must APPEAR for the watch to fire")
    p.add_argument("--message", required=True, help="what to tell the owner when it fires")
    p.add_argument("--ledger-id", default="")
    p.add_argument("--every", type=int, default=15, help="minutes between fetches")
    p = sub.add_parser("remove", allow_abbrev=False)
    p.add_argument("--id", required=True)
    sub.add_parser("list", allow_abbrev=False)
    p = sub.add_parser("check", allow_abbrev=False)
    p.add_argument("--id", default="")
    p.add_argument("--force", action="store_true", help="ignore the per-watch interval")
    a = ap.parse_args()
    if a.cmd == "add":
        watches = [w for w in _load(WATCHES, []) if w["id"] != a.id]
        watches.append({"id": a.id, "url": a.url, "absent": a.absent or "", "present": a.present or "",
                        "message": a.message, "ledger_id": a.ledger_id, "every": a.every,
                        "added": datetime.now(timezone.utc).isoformat()})
        _save(WATCHES, watches)
        out = {"ok": True, "watches": len(watches)}
    elif a.cmd == "remove":
        watches = _load(WATCHES, [])
        kept = [w for w in watches if w["id"] != a.id]
        _save(WATCHES, kept)
        out = {"ok": True, "removed": len(watches) - len(kept)}
    elif a.cmd == "list":
        state = _load(STATE, {})
        out = {"watches": [{**w, "state": state.get(w["id"], {})} for w in _load(WATCHES, [])]}
    else:
        out = check(a.id, a.force)
    print(json.dumps(out, indent=1, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())

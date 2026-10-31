#!/usr/bin/env python3
"""What the updater installed and what it decided, for everyone who reads it.

scripts/update.py installs signed releases (docs/updates.md). This module is
the read side it shares with the doctor (the `updates` row), the chat bot
(the `version` command) and run-agent.sh (the one briefing line): the
installed version, the effective channel, the status file with the release
notes the updater recorded, and the text each of them shows. Standard
library only; it reads, and it writes nothing but the owner-private marker
of what the last briefing said.

    python3 preprocessor/update_status.py version
    python3 preprocessor/update_status.py notes [--json]  # the installed release's notes
    python3 preprocessor/update_status.py briefing-line   # one line, or nothing
    python3 preprocessor/update_status.py mark-briefed    # after the briefing posted
    python3 preprocessor/update_status.py doctor --json
"""
from __future__ import annotations

import json
import os
import re
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
SETTINGS = ("auto", "latest", "stable", "off")
CHANNELS = ("latest", "stable")
DEFAULT_LIVE = "/opt/ea-system"
# The updater checks hourly; two days without a check means its timer is
# not running.
STALE_CHECK_S = 48 * 3600


def version_key(tag: str) -> tuple[int, int, int] | None:
    m = TAG_RE.match(tag or "")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def live_dir() -> Path:
    return Path(os.environ.get("EA_LIVE_DIR") or DEFAULT_LIVE)


def releases_dir(live: Path) -> Path:
    return live.with_name(live.name + ".releases")


def private_dir() -> Path:
    """owner_trust.private_dir(), spelled here so this module needs no
    other import: EA_PRIVATE_DIR, else .ea-private in the passwd home."""
    if os.environ.get("EA_PRIVATE_DIR"):
        return Path(os.environ["EA_PRIVATE_DIR"])
    try:
        import pwd
        return Path(pwd.getpwuid(os.geteuid()).pw_dir) / ".ea-private"
    except (ImportError, KeyError):
        return Path.home() / ".ea-private"


def ea_dir() -> Path:
    return Path(os.environ.get("EA_DIR") or Path.home() / ".ea")


def update_dir(private: Path) -> Path:
    return private / "update"


def status_file(private: Path) -> Path:
    return update_dir(private) / "status.json"


def briefed_file(private: Path) -> Path:
    return update_dir(private) / "briefed.json"


def in_container() -> bool:
    return Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()


def read_owner_json(path: Path, uid: int | None = None) -> dict | None:
    """A JSON object from an owner-private file: a regular file reached with
    no link at its last name, owned by this user (or `uid`, for root's
    layout step) and writable by no one else, checked on the descriptor it
    is read from. None when missing, refused or not an object."""
    try:
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                     | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if (not stat.S_ISREG(st.st_mode) or st.st_uid != (os.geteuid() if uid is None else uid)
                or st.st_mode & 0o022 or st.st_size > 1_000_000):
            return None
        with os.fdopen(fd, "rb", closefd=False) as fh:
            doc = json.loads(fh.read().decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    finally:
        os.close(fd)
    return doc if isinstance(doc, dict) else None


def _read_text(path: Path, limit: int = 256) -> str:
    try:
        with open(path, "rb") as fh:
            return fh.read(limit).decode("utf-8", "replace").strip()
    except OSError:
        return ""


def tree_kind(live: Path) -> str:
    """layout (the updater's link into its release directory), plain (a
    directory the post-commit hook or the install seeded), none, or
    container (the code is in the image)."""
    if in_container():
        return "container"
    if live.is_symlink():
        return "layout"
    if live.is_dir():
        return "plain"
    return "none"


def config_channel(ea: Path) -> str:
    """update.channel as ea-config.json says it, "" when it says none."""
    try:
        import ea_config
        return ea_config.get_update_channel(ea / "config" / "ea-config.json") or ""
    except Exception:
        return ""


def channel_setting(private: Path, ea: Path) -> tuple[str, str]:
    """(value, source): the owner-private update.json, the only place a
    channel other than auto counts. ea-config.json is the EA user's to
    write, so any update.channel there reads as "auto" (the doctor warns
    when it says more), and only update.json can turn updates off or pin
    a channel. ("", "unset") when neither names one of SETTINGS."""
    doc = read_owner_json(private / "update.json") or {}
    if doc.get("channel") in SETTINGS:
        return doc["channel"], "update.json"
    if config_channel(ea):
        return "auto", "ea-config.json"
    return "", "unset"


def misplaced_channel(private: Path, ea: Path) -> str:
    """The channel ea-config.json names when it is one auto does not mean
    (latest, stable or off) and update.json names none: a setting in the
    EA user's space that the updater reads as auto. "" otherwise."""
    doc = read_owner_json(private / "update.json") or {}
    if doc.get("channel") in SETTINGS:
        return ""
    val = config_channel(ea)
    return val if val in SETTINGS and val != "auto" else ""


def profile(ea: Path) -> str:
    try:
        import ea_config
        return ea_config.get_profile(ea / "config" / "ea-config.json")
    except Exception:
        return "assistant"


def effective_channel(live: Path, private: Path, ea: Path) -> tuple[str, str]:
    """(channel, source), channel one of latest, stable, off.

    With no setting, a plain tree belongs to the post-commit hook ("off",
    source "hook"): the maintainer's machine deploys its own commits and is
    never updated behind the hook. A release layout, or no tree yet, takes
    the profile's default. "auto" is the profile's default too."""
    value, source = channel_setting(private, ea)
    if not value:
        if tree_kind(live) == "plain":
            return "off", "hook"
        value, source = "auto", "default"
    if value == "auto":
        value = "latest" if profile(ea) == "assistant" else "stable"
    return value, source


def installed(live: Path) -> dict:
    """The version and commit the code tree carries: .version (a release
    tag the updater wrote) and .deployed-commit (the short commit every
    deploy writes)."""
    version = _read_text(live / ".version")
    return {"version": version if version_key(version) else "",
            "commit": _read_text(live / ".deployed-commit")[:40]}


def load_status(private: Path) -> dict:
    return read_owner_json(status_file(private)) or {}


# ── release notes ────────────────────────────────────────────────────

def _notes_entries(st: dict) -> list[dict]:
    """The release notes the status keeps (scripts/update.py
    record_notes), oldest first: the history, then the installed
    release's."""
    out = [h for h in (st.get("notes_history") or []) if isinstance(h, dict)]
    if isinstance(st.get("notes"), dict):
        out.append(st["notes"])
    return [e for e in out if version_key(str(e.get("tag") or ""))]


def installed_notes(live: Path, private: Path) -> dict:
    """The installed release's notes as the updater recorded them:
    {"tag", "previous", "notes_url", "count", "changes": [{"subject", "pr",
    "sha"}], "owner_steps": [{"step", "pr", "sha"}], "source"}. {} when it
    recorded none for that release."""
    tag = installed(live).get("version") or ""
    for e in reversed(_notes_entries(load_status(private))):
        if tag and e.get("tag") == tag:
            return e
    return {}


def _count(e: dict, listed: int) -> int:
    try:
        return max(int(e.get("count") or 0), listed)
    except (TypeError, ValueError):
        return listed


def notes_since(st: dict, before: str, tag: str) -> tuple[int, list[dict], list[dict]]:
    """(count, changes, owner steps) of the releases after `before` up to
    `tag`, from the notes the status keeps. With no `before` version, the
    notes of `tag` alone. A change two entries list counts once."""
    hi, lo = version_key(tag), version_key(before)
    if hi is None:
        return 0, [], []
    entries = []
    for e in _notes_entries(st):
        k = version_key(e["tag"])
        if k <= hi and (k > lo if lo is not None else e["tag"] == tag):
            entries.append(e)
    entries.sort(key=lambda e: version_key(e["tag"]))
    changes, steps, seen, seen_steps, unlisted, tags = [], [], set(), set(), 0, set()
    for e in entries:
        if e["tag"] in tags:
            continue
        tags.add(e["tag"])
        listed = [c for c in e.get("changes") or [] if isinstance(c, dict) and c.get("subject")]
        unlisted += _count(e, len(listed)) - len(listed)
        for c in listed:
            key = str(c.get("sha") or "")[:7] or str(c["subject"])
            if key not in seen:
                seen.add(key)
                changes.append(c)
        for s in e.get("owner_steps") or []:
            text = str((s or {}).get("step") or "") if isinstance(s, dict) else str(s)
            if text and text.lower() not in seen_steps:
                seen_steps.add(text.lower())
                steps.append(s if isinstance(s, dict) else {"step": text})
    return len(changes) + unlisted, changes, steps


def _short(text, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def change_summary(changes: list[dict], count: int, shown: int = 2, limit: int = 80) -> str:
    """"3 changes: <first>; <second>…": the count and the first `shown`
    subjects, with an ellipsis when there are more."""
    subjects = [_short(c.get("subject"), limit) for c in changes[:shown]]
    word = "change" if count == 1 else "changes"
    if not subjects:
        return f"{count} {word}"
    return f"{count} {word}: " + "; ".join(subjects) + ("…" if count > len(subjects) else "")


def notes_lines(notes: dict, shown: int = 3) -> list[str]:
    """The installed release's notes as the `version` answer shows them:
    the count, the first `shown` subjects, the owner steps and the link."""
    changes = [c for c in notes.get("changes") or [] if isinstance(c, dict)]
    n = _count(notes, len(changes))
    if not n and not notes.get("owner_steps"):
        return []
    head = (f"{n} {'change' if n == 1 else 'changes'} in {notes.get('tag')}"
            + (f" since {notes['previous']}" if notes.get("previous") else ""))
    out = [head + (":" if changes else ".")]
    for c in changes[:shown]:
        out.append(f"- {_short(c.get('subject'), 160)}" + (f" (#{c['pr']})" if c.get("pr") else ""))
    if n > min(shown, len(changes)) and changes:
        out.append(f"- and {n - min(shown, len(changes))} more")
    for s in notes.get("owner_steps") or []:
        text = s.get("step") if isinstance(s, dict) else s
        if text:
            out.append(f"Owner step: {_short(text, 300)}")
    if notes.get("notes_url"):
        out.append(f"Release notes: {notes['notes_url']}")
    return out


def _label(inst: dict) -> str:
    if inst.get("version"):
        return inst["version"] + (f" ({inst['commit']})" if inst.get("commit") else "")
    if inst.get("commit"):
        return f"commit {inst['commit']}"
    return "an unknown version"


def _when(iso: str) -> str:
    try:
        t = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return str(iso)
    return t.astimezone().strftime("%Y-%m-%d %H:%M")


def _age_s(iso: str, now: float) -> float | None:
    try:
        return now - datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def doctor_row(live: Path, private: Path, ea: Path,
               now: float | None = None) -> tuple[str, str, str]:
    """(status, detail, fix) for the doctor's `updates` row. PASS or WARN,
    never FAIL: an install that does not update still works."""
    import time as _time
    now = _time.time() if now is None else now
    kind = tree_kind(live)
    inst = installed(live)
    channel, source = effective_channel(live, private, ea)
    st = load_status(private)
    if kind == "container":
        return ("PASS", f"{_label(inst)}; the code is in the image",
                "docs/docker.md 'Upgrade'")
    stray = misplaced_channel(private, ea)
    if stray:
        return ("WARN", f"{_label(inst)}; update.channel {stray} is set in ea-config.json, which "
                        f"the EA user can write, so it counts as auto ({channel})",
                f"channel set in ea-bot space; move it to update.json: "
                f"{{\"channel\": \"{stray}\"}} in ~/.ea-private/update.json (0600), and "
                "remove it from ea-config.json (docs/updates.md, Channels)")
    if channel == "off":
        why = ("deployed by the post-commit hook" if source == "hook"
               else f"update.channel is off ({source})")
        return ("PASS", f"{_label(inst)}; automatic updates off, {why}", "")
    if kind != "layout":
        return ("WARN", f"{_label(inst)}; channel {channel} ({source}), but {live} is not "
                        "the release layout yet, so nothing installs",
                "sudo bash deploy/harden-permissions.sh (macOS: sudo bash "
                "deploy/macos/install-services.sh), docs/updates.md")
    parts = [f"{_label(inst)} on {channel}"]
    latest = st.get("latest_on_channel")
    if latest:
        parts.append(f"newest on the channel {latest}" + _behind(st, inst, latest))
    last = st.get("last_apply") or {}
    if last.get("at"):
        parts.append(f"last apply {_when(last['at'])} ({last.get('result', '?')}"
                     + (f" {last['to']}" if last.get("to") else "") + ")")
    held = st.get("held_back") or {}
    detail = "; ".join(parts)
    if last.get("result") == "failed":
        return ("WARN", f"{detail}: {last.get('detail', '?')}",
                "the next run tries again; a restart that sudo refuses needs the grant "
                "deploy/harden-permissions.sh writes (/etc/sudoers.d/ea-chat-bot)")
    err = st.get("last_error") or {}
    if err.get("error"):
        return ("WARN", f"{detail}; the last run stopped: {err['error'][:200]}",
                f"python3 {live}/scripts/update.py check says it again; with no release_key "
                "in ~/.ea-private/update.json and no .release-key.pub in the installed release "
                "there is no trust root yet (docs/updates.md, Supply chain)")
    change = st.get("key_change") or {}
    if change.get("version"):
        return ("WARN", f"{detail}; {change['version']} changes the release key and is not "
                        "installed",
                "check the new key with the maintainer's announcement, then add its line to "
                "release_key in ~/.ea-private/update.json (docs/updates.md, Supply chain)")
    if held.get("version"):
        return ("WARN", f"{detail}; {held['version']} held back: {held.get('reason', '?')}",
                "it installs with the next newer release, or by hand: python3 "
                f"{live}/scripts/update.py apply --tag {held['version']}")
    age = _age_s(st.get("checked_at", ""), now)
    if age is None or age > STALE_CHECK_S:
        return ("WARN", f"{detail}; " + ("no check yet" if age is None
                                         else f"last check {age / 3600:.0f} h ago"),
                "systemctl status tacitus-update.timer (macOS: the tacitus-update "
                "LaunchDaemon); ~/.ea-private/logs/update.log or journalctl -u tacitus-update")
    return ("PASS", detail, "")


def _behind(st: dict, inst: dict, latest: str) -> str:
    """" (N changes behind)" when the updater counted the changes between
    the installed release and a newer `latest`, else ""."""
    behind = st.get("behind") or {}
    n = behind.get("changes")
    lk, ik = version_key(latest), version_key(inst.get("version") or "")
    if behind.get("tag") != latest or not isinstance(n, int) or n <= 0 or lk is None \
            or (ik is not None and ik >= lk):
        return ""
    return f" ({n} {'change' if n == 1 else 'changes'} behind)"


def version_text(live: Path, private: Path, ea: Path, running: Path | None = None) -> str:
    """The `version` chat command's answer: the installed tag and commit,
    the channel, the newest release on it, the first three changes of the
    installed release with its owner steps and notes link, and any version
    held back. With `running` (the tree this process started from), a
    process behind the installed tree says so."""
    inst = installed(live)
    channel, source = effective_channel(live, private, ea)
    st = load_status(private)
    lines = [f":label: Tacitus {_label(inst)}."]
    if tree_kind(live) == "container":
        lines.append("The code is in the image; a rebuild updates it.")
    elif channel == "off":
        lines.append("Automatic updates are off"
                     + (": the post-commit hook deploys this install." if source == "hook"
                        else f" ({source})."))
    else:
        line = f"Channel: {channel}."
        if st.get("latest_on_channel"):
            line += f" Newest on the channel: {st['latest_on_channel']}."
        last = st.get("last_apply") or {}
        if last.get("at"):
            line += f" Last update: {_when(last['at'])} ({last.get('result', '?')})."
        lines.append(line)
    lines += notes_lines(installed_notes(live, private))
    held = (st.get("held_back") or {}).get("version")
    if held:
        lines.append(f"Update {held} is held back: {(st.get('held_back') or {}).get('reason', '?')}.")
    if running is not None:
        mine = installed(running)
        if (mine.get("version") or mine.get("commit")) and mine != inst:
            lines.append(f"This bot process started on {_label(mine)}; it moves at its next restart.")
    return "\n".join(lines)


def briefing_line(live: Path, private: Path) -> str:
    """One line for the morning briefing, or "": the installed version
    changed since the last briefing that posted, with the count and the
    first two subjects of the changes since then and any owner step, or a
    version was held back since then. Before the first marker there is
    nothing to compare, so nothing is said."""
    briefed = read_owner_json(briefed_file(private))
    if briefed is None:
        return ""
    inst = installed(live).get("version") or ""
    st = load_status(private)
    held = ((st.get("held_back") or {}).get("version")) or ""
    parts = []
    before = briefed.get("version") or ""
    if inst and inst != before:
        n, changes, steps = notes_since(st, before, inst)
        if n:
            parts.append(f"Updated to {inst} ({change_summary(changes, n)}).")
        else:
            parts.append(f"Updated to {inst}" + (f" (from {before})." if before else "."))
        texts = [_short(s.get("step"), 120) for s in steps if s.get("step")]
        if texts:
            parts.append(("Owner step: " if len(texts) == 1 else "Owner steps: ")
                         + "; ".join(texts[:2]) + ("…" if len(texts) > 2 else "") + ".")
    if held and held != (briefed.get("held") or ""):
        parts.append(f"Update {held} held back; running {inst or 'the installed release'}.")
    return (":gear: " + " ".join(parts)) if parts else ""


def mark_briefed(live: Path, private: Path) -> None:
    """Record what the briefing that just posted knew. Owner-private, a
    random temp name and a rename."""
    import tempfile
    doc = {"version": installed(live).get("version") or "",
           "held": ((load_status(private).get("held_back") or {}).get("version")) or "",
           "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    d = update_dir(private)
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(d), prefix=".briefed.")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(doc, fh)
        os.chmod(tmp, 0o600)
        os.replace(tmp, briefed_file(private))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="the updater's status, as the doctor, the bot "
                                             "and the briefing show it")
    ap.add_argument("cmd", choices=("version", "notes", "briefing-line", "mark-briefed", "doctor"))
    ap.add_argument("--live", default=None, help=f"the code tree (default {DEFAULT_LIVE})")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    live = Path(a.live) if a.live else live_dir()
    private, ea = private_dir(), ea_dir()
    if a.cmd == "version":
        print(version_text(live, private, ea))
    elif a.cmd == "notes":
        notes = installed_notes(live, private)
        if a.json:
            print(json.dumps(notes, indent=1))
        else:
            print("\n".join(notes_lines(notes, shown=len(notes.get("changes") or [])))
                  or "No release notes recorded for the installed release.")
    elif a.cmd == "briefing-line":
        line = briefing_line(live, private)
        if line:
            print(line)
    elif a.cmd == "mark-briefed":
        mark_briefed(live, private)
    else:
        status, detail, fix = doctor_row(live, private, ea)
        if a.json:
            print(json.dumps({"status": status, "detail": detail, "fix": fix}))
        else:
            print(f"{status} updates: {detail}" + (f"\n  fix: {fix}" if fix else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())

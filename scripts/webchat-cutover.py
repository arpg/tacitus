#!/usr/bin/env python3
"""The move from shadow mode (Zulip with the web chat beside it) to the web
chat alone, for the owner (docs/webchat.md, The move to the web chat alone;
docs/zulip-switch.md, Leave Zulip for the web chat).

    webchat-cutover.py plan [--zulip]          what apply would change; writes nothing
    webchat-cutover.py apply [--while-running] the move
    webchat-cutover.py teardown                the owner's steps that take Zulip down

`plan` reads chat.json, the web chat's store and the room, session and card
registries, and prints each change `apply` would make. With `--zulip` it
also reads Zulip (reads only) and counts the messages and files the export
would bring over.

`apply`, with the bot stopped:

1. Takes a snapshot first (scripts/webchat-backup.sh --label cutover), with a
   copy of the room and session registries beside it; the rotation never
   removes a labelled snapshot.
2. Works on a copy of the store, so a failure leaves the store as it was:
   every message Zulip holds in each mirrored channel comes in (all of
   them, not the week the shadow copies at start); a message the store
   holds already stays as it is. A card is the text Zulip shows, with its
   status line once it was answered. Each file Zulip holds that a message
   names, the ones mirrored before included, is downloaded into the
   uploads beside the store, and the message's links point at that copy.
3. Folds the mirrored channels into the web chat's own: the EA stream
   into `main`, the DM stream (and its done stream) into `dm`, the
   scheduling stream into `sched`, `mail-digest` into `digests`; every
   other stream stays a room under a web room id `r<n>`. Thread keys and
   message ids stay, so the sessions, the cards and the links that name
   them still find them; the store keeps a map from each old channel id to
   its new one (`channel_redirects`), which the web front end honours. The
   messages are numbered again in time order, so old history sits before
   the threads mirrored live.
4. Rewrites chat.json to `"frontend": "web"` with no `shadow` (the zulip
   block stays, for a switch back; the notifier keys that named a stream
   other than the EA stream name its web channel), puts the copy in place
   of the store, then moves the rooms in the room registry
   (ea-channels.json, with each room's web link) and the owner's room
   record, and points the session rows and the card registry at the new
   channel ids. A registry that does not move is named, with its fix.
5. Prints the Zulip teardown steps (`teardown` prints them again).

It refuses to run while the bot is up, unless `--while-running`: the
running bot keeps the old store open and writes into it (those writes are
lost when the copy takes its place), keeps the card registry in memory
(and writes it back over the moved one), and serves Zulip until it starts
again on the new chat.json. A switch back is the snapshot: put it back with
`scripts/webchat-backup.sh --restore <name>`. Run it as the owner, from
the owner's code tree (/opt/ea-system).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "preprocessor"))
sys.path.insert(0, str(REPO / "botcore"))

import owner_trust  # noqa: E402
from botcore.chat import web_frontend as wf  # noqa: E402
from botcore.chat.types import FileRef  # noqa: E402

BACKUP_SCRIPT = REPO / "scripts" / "webchat-backup.sh"
BACKUP_LABEL = "cutover"
# The digest channel and the notifier keys that always post there, as
# chat_notify names them (tests/test_webchat_cutover.py checks they agree).
DIGEST_STREAM = "mail-digest"
DIGEST_ID, DIGEST_NAME, DIGEST_KIND = "digests", "Mail digests", "digest"
DIGEST_KEYS = ("mail-digest", "list-digest", "admissions-digest", "briefing-preview")
FIXED = {"main": (wf.MAIN_ID, wf.MAIN_NAME, "main"),
         "dm": (wf.DM_ID, wf.DM_NAME, "dm"),
         "sched": (wf.SCHED_ID, wf.SCHED_NAME, "sched"),
         "digest": (DIGEST_ID, DIGEST_NAME, DIGEST_KIND)}
# A link to a file Zulip holds, as a message's Markdown writes it.
_UPLOAD_LINK_RE = re.compile(r"\((?P<site>https?://[^\s()/]+)?(?P<path>/user_uploads/[^\s()]+)\)")
SERVICE = "ea-chat-bot"
ZULIP_BACKEND = "http://127.0.0.1:8080"   # where deploy/zulip publishes the server, on loopback


def _platform() -> str:
    return sys.platform


def bot_label() -> str:
    """The chat bot's LaunchDaemon label on this host (the legacy prefix
    where that is what is installed), as the macOS installers read it."""
    try:
        import launchd_labels
        return launchd_labels.label(SERVICE)
    except ImportError:
        return f"org.ea-system.{SERVICE}"


def service_cmd(verb: str) -> str:
    """The owner's command that stops, starts or restarts the bot on this
    host. On macOS a stopped job is booted out (KeepAlive would restart a
    stopped one), so "start" is a bootstrap of its plist, not a kickstart,
    which cannot start an unloaded job; and the label is this install's
    (2026-11-09: the printed `org.ea-system` kickstart failed on an install
    under the legacy prefix whose job the owner had booted out)."""
    if _platform() == "darwin":
        label = bot_label()
        return {"stop": f"sudo launchctl bootout system/{label}",
                "start": f"sudo launchctl bootstrap system /Library/LaunchDaemons/{label}.plist",
                "restart": f"sudo launchctl kickstart -k system/{label}"}[verb]
    return f"sudo systemctl {verb} {SERVICE}"


def bot_python_hint() -> str:
    """Where the owner reads which Python the bot runs."""
    if _platform() == "darwin":
        return f"ProgramArguments in /Library/LaunchDaemons/{bot_label()}.plist names it"
    return f"systemctl show -p ExecStart {SERVICE} names it"


def zulip_port(zulip: dict) -> int | None:
    """The tailnet port the Zulip server is published on: the port of the
    zulip block's `site`, the URL the bot reaches it by (443 for a bare
    https URL). None when the block names no site."""
    site = str((zulip or {}).get("site") or "").strip()
    if not site:
        return None
    u = urllib.parse.urlsplit(site)
    try:
        port = u.port
    except ValueError:
        return None
    if not u.hostname:
        return None
    return port or {"https": 443, "http": 80}.get(u.scheme)


def _zulip_block(priv: Path | None) -> dict:
    """The zulip block of chat.json, whatever mode the block is in (after
    the move it runs the web chat alone and load_install refuses it)."""
    path = Path(priv or owner_trust.private_dir()) / owner_trust.CHAT_FILE
    try:
        chat = json.loads(owner_trust.read_owner_file(path))
    except (OSError, ValueError, owner_trust.OwnerTrustError):
        return {}
    zulip = chat.get("zulip") if isinstance(chat, dict) else None
    return zulip if isinstance(zulip, dict) else {}


class CutoverError(Exception):
    """A reason the move cannot go ahead; the message says what to do."""


@dataclass
class Move:
    """One mirrored channel and where it goes."""
    old: str
    name: str
    kind: str
    archived: bool
    description: str
    messages: int
    new: str
    new_name: str
    new_kind: str
    why: str


@dataclass
class Install:
    """The owner's chat settings and the web chat's files."""
    priv: Path
    chat_path: Path
    chat: dict
    web: dict
    zulip: dict
    store: Path

    @property
    def site(self) -> str:
        return str(self.zulip.get("site") or "").rstrip("/")

    @property
    def base_path(self) -> str:
        return wf._norm_base(self.web.get("base_path", wf.DEFAULT_BASE))

    @property
    def web_site(self) -> str:
        """Where the app's links start: public_url, else the base path."""
        return str(self.web.get("public_url") or "").rstrip("/") or self.base_path


@dataclass
class Report:
    """What apply did, for the summary it prints."""
    backup: str = ""
    exported: int = 0
    exported_by_channel: dict = field(default_factory=dict)
    files: int = 0
    files_failed: list = field(default_factory=list)
    links: int = 0
    moves: list = field(default_factory=list)
    rooms_moved: list = field(default_factory=list)
    owner_rooms_moved: list = field(default_factory=list)
    sessions: int = 0
    cards: int = 0
    failed: list = field(default_factory=list)   # registries the move could not write
    notify: dict = field(default_factory=dict)
    zulip: dict = field(default_factory=dict)    # the chat block's zulip settings, for the teardown steps


# reading the install

def load_install(priv: Path | None = None) -> Install:
    """chat.json through the owner checks, and the store it names. Raises
    CutoverError unless the block is Zulip with the web chat as its shadow."""
    priv = Path(priv or owner_trust.private_dir())
    path = priv / owner_trust.CHAT_FILE
    try:
        chat = json.loads(owner_trust.read_owner_file(path))
    except FileNotFoundError:
        raise CutoverError(f"{path} is missing: there is no web chat to move to") from None
    except (OSError, ValueError, owner_trust.OwnerTrustError) as e:
        raise CutoverError(f"{path} is not readable as the owner's chat settings: {e}") from None
    if not isinstance(chat, dict):
        raise CutoverError(f"{path} does not hold a chat block")
    if chat.get("frontend") == "web" and not chat.get("shadow"):
        raise CutoverError("chat.json already runs the web chat alone; there is nothing to move")
    if chat.get("frontend") != "zulip" or chat.get("shadow") != "web":
        raise CutoverError(f"chat.json runs frontend {chat.get('frontend')!r} with shadow "
                           f"{chat.get('shadow')!r}; the move starts from Zulip with the web "
                           "chat beside it (\"frontend\": \"zulip\", \"shadow\": \"web\")")
    web = chat.get("web") if isinstance(chat.get("web"), dict) else {}
    zulip = chat.get("zulip") if isinstance(chat.get("zulip"), dict) else {}
    store = Path(str(web.get("store_path") or "") or priv / wf.STORE_FILE)
    if not store.is_file():
        raise CutoverError(f"no web chat store at {store}: start the bot once in shadow mode")
    return Install(priv, path, chat, web, zulip, store)


def _connect(path: Path, *, read_only: bool = False, timeout: float = 30) -> sqlite3.Connection:
    """The store (or its work copy) through the owner-side guard
    (ledger.guarded_connect: a regular file, one link, no link on the way,
    never created), with rows by name."""
    import ledger
    conn = ledger.guarded_connect(path, timeout=timeout, read_only=read_only)
    conn.row_factory = sqlite3.Row
    return conn


def _ro(path: Path) -> sqlite3.Connection:
    return _connect(path, read_only=True, timeout=10)


def store_channels(path: Path) -> list[dict]:
    """The store's channel rows with their message counts, read-only."""
    with _ro(path) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT c.id, c.name, c.kind, c.archived, c.description, "
            "(SELECT COUNT(*) FROM messages m WHERE m.channel = c.id) AS messages "
            "FROM channels c")]


def _stream_number(cid: str) -> int:
    m = re.match(r"stream:(\d+)$", cid)
    return int(m.group(1)) if m else 1 << 30


def plan_moves(channels: list[dict], zulip: dict) -> list[Move]:
    """Where each mirrored channel goes (see the module docstring). A
    channel that has a web id already is not a mirror and is left out."""
    dm_name = str(zulip.get("dm_stream") or "ea-dm")
    done_name = str(zulip.get("dm_done_stream") or f"{dm_name}-done")
    taken = {c["id"] for c in channels}
    room = 1 + max([int(c["id"][1:]) for c in channels if re.fullmatch(r"r\d+", c["id"])] or [0])
    fixed_used: set[str] = set()
    moves = []
    for c in sorted(channels, key=lambda c: (_stream_number(c["id"]), c["id"])):
        cid, name, kind = c["id"], c["name"] or "", c["kind"] or "room"
        if not (cid.startswith("stream:") or cid.startswith("dm:")):
            continue
        slot, why = None, ""
        if kind in ("main", "dm", "sched") and kind not in fixed_used:
            slot, why = kind, f"the mirrored {kind} channel"
        elif name == DIGEST_STREAM and "digest" not in fixed_used:
            slot, why = "digest", "the mail digests"
        elif name == done_name:
            slot, why = "dm", "the cards settled outside chat belong to the DM"
        if slot:
            fixed_used.add(slot)
            new, new_name, new_kind = FIXED[slot]
        else:
            while f"r{room}" in taken:
                room += 1
            new, new_name, new_kind, why = f"r{room}", name or cid, "room", "a room"
            taken.add(new)
            room += 1
        moves.append(Move(cid, name, kind, bool(c["archived"]), c.get("description") or "",
                          int(c.get("messages") or 0), new, new_name, new_kind, why))
    return moves


def notify_map(install: Install, moves: list[Move]) -> dict:
    """The web block's `notify` entries for the zulip block's: a key whose
    stream moved to a web channel other than the EA channel names that
    channel. The EA channel is the default, and a digest key always posts
    to the digest channel, so neither needs an entry; a key the web block
    names already keeps its own."""
    by_name = {m.name: m.new for m in moves if m.name}
    notify = install.zulip.get("notify") if isinstance(install.zulip.get("notify"), dict) else {}
    have = install.web.get("notify") if isinstance(install.web.get("notify"), dict) else {}
    out = {}
    for key, entry in notify.items():
        if key in DIGEST_KEYS or key in have or not isinstance(entry, dict):
            continue
        new = by_name.get(str(entry.get("stream") or ""))
        if new and new != wf.MAIN_ID:
            out[key] = {"channel": new}
    return out


def zulip_files(path: Path, site: str) -> tuple[int, int]:
    """(rows whose file Zulip holds, links to Zulip's files in the text),
    read-only."""
    rows = links = 0
    with _ro(path) as conn:
        for r in conn.execute("SELECT text, file FROM messages"):
            f = wf._loads(r["file"])
            if isinstance(f, dict) and not f.get("id") and _zulip_path(f.get("url") or "", site):
                rows += 1
            links += len(_UPLOAD_LINK_RE.findall(r["text"] or ""))
    return rows, links


def _zulip_path(url: str, site: str) -> str:
    """The `/user_uploads/...` path of a link to a file Zulip holds, else ""."""
    if site and url.startswith(site + "/user_uploads/"):
        return url[len(site):]
    return url if url.startswith("/user_uploads/") else ""


# the bot's state

def bot_up(install: Install) -> str:
    """Why the bot looks up, "" when it does not: its service is active, or
    something listens on the web chat's port."""
    why = []
    if sys.platform == "darwin":
        try:
            import launchd_labels
            label = launchd_labels.label(SERVICE)
            r = subprocess.run(["launchctl", "print", f"system/{label}"], capture_output=True,
                               text=True, timeout=10)
            if re.search(r"^\s*pid = \d+", r.stdout, re.M):
                why.append(f"the {label} service runs")
        except (OSError, subprocess.SubprocessError, ImportError):
            pass
    elif shutil.which("systemctl"):
        try:
            r = subprocess.run(["systemctl", "is-active", SERVICE], capture_output=True,
                               text=True, timeout=10)
            if r.stdout.strip() == "active":
                why.append(f"{SERVICE}.service is active")
        except (OSError, subprocess.SubprocessError):
            pass
    host = str(install.web.get("bind") or wf.DEFAULT_BIND)
    host = {"0.0.0.0": "127.0.0.1", "::": "::1", "": "127.0.0.1"}.get(host, host)
    port = int(install.web.get("port") or wf.DEFAULT_PORT)
    try:
        with socket.create_connection((host, port), timeout=1):
            why.append(f"something listens on {host}:{port}, the web chat's port")
    except OSError:
        pass
    return "; ".join(why)


# plan

def plan_text(install: Install, moves: list[Move], *, zulip_counts: dict | None = None) -> str:
    out = ["Move to the web chat alone: the plan (nothing is written).", ""]
    out.append(f"chat.json   {install.chat_path}")
    out.append("            frontend zulip, shadow web  ->  frontend web, no shadow "
               "(the zulip block stays, for a switch back)")
    with _ro(install.store) as conn:
        total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    out.append(f"store       {install.store} ({total} messages)")
    out.append(f"snapshot    {install.priv / 'backups' / 'webchat'}/<today>-{BACKUP_LABEL}/ "
               f"(scripts/webchat-backup.sh --label {BACKUP_LABEL}), first")
    out += ["", "Channels (old id, Zulip name, kind -> web id, name):"]
    for m in moves:
        line = (f"  {m.old:<10} {m.name[:34]:<34} {m.kind:<6} -> {m.new:<8} {m.new_name}"
                f"{' (archived)' if m.archived else ''}; {m.messages} message(s) now")
        if zulip_counts is not None:
            got = zulip_counts.get(m.old)
            line += (f", {got['messages']} in Zulip, {got['files']} file(s)" if got
                     else ", Zulip unread")
        out.append(line + f"  [{m.why}]")
    if zulip_counts is None:
        out.append("  Every message Zulip holds in these channels comes over; `plan --zulip` "
                   "counts them.")
    rows, links = zulip_files(install.store, install.site)
    out += ["", f"Files: {rows} message(s) in the store link a file Zulip holds, and {links} "
                f"link(s) in the text name one; each is downloaded into "
                f"{install.store.parent / wf.FILES_DIR} and its links point there."]
    out += ["", "Registries:"]
    out += _registry_plan(install, moves)
    notify = notify_map(install, moves)
    out.append("  chat.json web.notify: " + (_notify_note(notify)[len("; notify "):]
                                             or "no entry"))
    redirects = {m.old: m.new for m in moves}
    out += ["", f"The store keeps the map of {len(redirects)} old channel id(s) to the new ones "
                "(channel_redirects); the web front end honours it."]
    out += ["", "Then, with the bot stopped: webchat-cutover.py apply, run by the Python the bot "
                "runs (it has the zulip package); start the bot; take Zulip down "
                "(webchat-cutover.py teardown)."]
    return "\n".join(out)


def _registry_plan(install: Install, moves: list[Move]) -> list[str]:
    import ea_channels
    import owner_rooms
    rooms = {m.old: m.new for m in moves if m.new_kind == "room"}
    out = []
    try:
        reg = ea_channels.load()
        moving = [k for k in reg if k in rooms]
        staying = [k for k in reg if k.startswith("stream:") and k not in rooms]
        out.append(f"  {ea_channels.REGISTRY}: {len(moving)} room(s) move to their r<n> id"
                   + (f"; {len(staying)} stay under a Zulip id (not mirrored): "
                      f"{', '.join(sorted(staying, key=_stream_number))}" if staying else ""))
    except Exception as e:
        out.append(f"  ea-channels.json: unread ({e})")
    try:
        record = owner_rooms.load() or {}
        out.append(f"  {owner_rooms.path()}: {sum(1 for k in record if k in rooms)} room(s) move")
    except Exception as e:
        out.append(f"  owner-rooms.json: unread ({e})")
    all_moves = {m.old: m.new for m in moves}
    try:
        import slack_sessions
        rows = slack_sessions._load()
        n = sum(1 for r in rows.values()
                if isinstance(r.get("meta"), dict) and r["meta"].get("channel") in all_moves)
        out.append(f"  {slack_sessions.REGISTRY}: {n} session row(s) name a moved channel "
                   f"(thread keys stay)")
    except Exception as e:
        out.append(f"  slack-sessions.json: unread ({e})")
    cards_path = install.priv / "chat-cards.json"
    try:
        cards = json.loads(owner_trust.read_owner_file(cards_path))
        hit = [r for r in cards.values() if isinstance(r, dict) and r.get("channel") in all_moves]
        live = sum(1 for r in hit if r.get("status") == "live")
        out.append(f"  {cards_path}: {len(hit)} card(s) move ({live} live, which keep working)")
    except FileNotFoundError:
        out.append(f"  {cards_path}: none")
    except Exception as e:
        out.append(f"  chat-cards.json: unread ({e})")
    return out


def count_zulip(primary: Any, moves: list[Move]) -> dict:
    """{old id: {"messages", "files"}} from Zulip's whole history, read only."""
    out = {}
    for m in moves:
        n = files = 0
        for msg in primary.channel_history(m.old):
            n += 1
            files += len(msg.files or [])
        out[m.old] = {"messages": n, "files": files}
    return out


# apply

def take_backup(install: Install) -> Path:
    """The labelled snapshot, and the room and session registries beside it."""
    env = {**os.environ, "EA_PRIVATE_DIR": str(install.priv)}
    r = subprocess.run(["bash", str(BACKUP_SCRIPT), "--label", BACKUP_LABEL],
                       capture_output=True, text=True, env=env, timeout=1800)
    if r.returncode != 0:
        raise CutoverError(f"the snapshot failed, so nothing moved: {r.stderr.strip()[:500]}")
    m = re.search(r"backup written: (.+) \(", r.stdout)
    snap = Path(m.group(1)) if m else Path("/nonexistent")
    if not (snap / "MANIFEST.tsv").is_file():
        raise CutoverError(f"the snapshot is not where it should be ({snap}); nothing moved")
    import ea_channels
    import resident_io
    import slack_sessions
    reg = snap / "registries"
    reg.mkdir(mode=0o700, exist_ok=True)
    for src in (ea_channels.REGISTRY, slack_sessions.REGISTRY):
        data = resident_io.read_bytes(src)
        if data is not None:
            owner_trust.write_owner_file(reg / Path(src).name, data, mode=0o600)
    return snap


def _work_copy(store: Path) -> Path:
    """A copy of the store beside it, through SQLite's backup API."""
    work = store.with_name(store.name + ".cutover")
    for p in (work, Path(str(work) + "-journal")):
        if os.path.lexists(p):
            p.unlink()
    os.close(os.open(work, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                     0o600))
    src = _connect(store, read_only=True)
    try:
        dst = _connect(work)
        try:
            with dst:
                src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return work


def export_rows(shadow: Any, web: Any, moves: list[Move], report: Report,
                log: Callable[[str], None]) -> None:
    """Every message the primary holds in the moving channels, into the
    store under the primary's ids; a row the store holds stays."""
    store = web.store
    for m in moves:
        n = 0
        for ref, author, text, files in shadow.export_history([m.old]):
            n += _store_row(web, store, ref, author, text, files)
        report.exported_by_channel[m.old] = n
        report.exported += n
        log(f"  {m.old} ({m.name}): {n} message(s) brought over")


def _store_row(web: Any, store: Any, ref: Any, author: str, text: str,
               files: list[FileRef]) -> int:
    """One message of the primary's past as history rows, as the shadow's
    history copy stores it (the first file on the row with the text, each
    further file on a row `<id>:<n>`); the rows it added."""
    author = wf.OWNER if author == wf.OWNER else wf.BOT
    key = ref.thread_key or ref.id
    if not files:
        if store.message(ref.channel, ref.id) is not None:
            return 0
        store.add_message(ref.channel, mid=ref.id, thread_key=key, author=author,
                          kind="message", text=text or "", posted_at=ref.posted_at, history=True)
        return 1
    added = 0
    for n, f in enumerate(files):
        mid = ref.id if n == 0 else f"{ref.id}:{n}"
        if store.message(ref.channel, mid) is not None:
            continue
        name = wf.safe_name(f.name)
        fj = web._file_json("", name, web._mimetype(name, f.mimetype), 0, f.url)
        store.add_message(ref.channel, mid=mid, thread_key=key, author=author, kind="file",
                          text=(text or "") if n == 0 else "", file=fj,
                          posted_at=ref.posted_at, history=True)
        added += 1
    return added


def fetch_files(primary: Any, web: Any, install: Install, report: Report,
                log: Callable[[str], None]) -> dict[str, dict]:
    """Download each file Zulip holds that a row names (its file, or a link
    in its text) and keep it in the store under its Zulip path; {path: the
    file's JSON as the store gives it} for each one kept."""
    store, site = web.store, install.site
    wanted: dict[str, str] = {}
    with _ro(web.store_path) as conn:
        for r in conn.execute("SELECT channel, text, file FROM messages"):
            f = wf._loads(r["file"])
            if isinstance(f, dict) and not f.get("id"):
                path = _zulip_path(f.get("url") or "", site)
                if path:
                    wanted.setdefault(path, r["channel"])
            for m in _UPLOAD_LINK_RE.finditer(r["text"] or ""):
                if not m.group("site") or m.group("site") == site:
                    wanted.setdefault(m.group("path"), r["channel"])
    kept: dict[str, dict] = {}
    with tempfile.TemporaryDirectory(prefix="webchat-cutover-") as tmp:
        for i, (path, channel) in enumerate(sorted(wanted.items())):
            name = urllib.parse.unquote(path.rsplit("/", 1)[-1]) or "file"
            ref = FileRef(name=name, url=f"{site}{path}" if site else path, id=path)
            try:
                local = primary.download(ref, Path(tmp) / str(i))
                data = Path(local).read_bytes() if local else None
            except Exception as e:
                local, data = None, None
                log(f"  {path}: not downloaded ({e})")
            if data is None:
                report.files_failed.append(path)
                continue
            safe = wf.safe_name(name)
            mimetype = web._mimetype(safe)
            store.add_file(path, channel, safe, data, mimetype)
            kept[path] = web._file_json(path, safe, mimetype, len(data))
    report.files = len(kept)
    log(f"  files: {len(kept)} kept here, {len(report.files_failed)} not downloaded")
    return kept


def _rewrite_value(value: Any, moves: dict[str, str]) -> tuple[Any, bool]:
    """`value` with every string equal to an old channel id replaced by its
    new id (a card's `ea_channel`, an action value's channel)."""
    if isinstance(value, str):
        return (moves[value], True) if value in moves else (value, False)
    hit = False
    if isinstance(value, dict):
        for k, v in list(value.items()):
            value[k], h = _rewrite_value(v, moves)
            hit = hit or h
    elif isinstance(value, list):
        for i, v in enumerate(value):
            value[i], h = _rewrite_value(v, moves)
            hit = hit or h
    return value, hit


def fold(work: Path, moves: list[Move], kept_files: dict[str, dict], install: Install,
         report: Report) -> None:
    """In one transaction on the work copy: rewrite the files' links, fold
    the channels into their new ids, number the messages again in time
    order, point the cards at the new channel ids, empty the event log and
    keep the redirect map. See the module docstring."""
    old_to_new = {m.old: m.new for m in moves}
    conn = _connect(work)
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        taken = {r["id"] for r in conn.execute("SELECT id FROM channels")} - set(old_to_new)
        clash = sorted({m.new for m in moves} & taken - {wf.MAIN_ID, wf.DM_ID, wf.SCHED_ID,
                                                          DIGEST_ID})
        if clash:
            raise CutoverError(f"the store holds channel(s) {', '.join(clash)} already; "
                               "nothing moved")
        _rewrite_links(conn, kept_files, install.site, report)
        before = {r["seq"]: r["channel"] for r in conn.execute("SELECT seq, channel FROM messages")}
        marks = {r["channel"]: int(r["seq"] or 0) for r in conn.execute("SELECT * FROM reads")}
        _fold_channels(conn, moves)
        _resequence(conn, before, marks, old_to_new)
        for r in conn.execute("SELECT seq, card FROM messages WHERE card IS NOT NULL").fetchall():
            card = wf._loads(r["card"])
            card, hit = _rewrite_value(card, old_to_new)
            if hit:
                conn.execute("UPDATE messages SET card=? WHERE seq=?", (json.dumps(card), r["seq"]))
        # The log's payloads carry the old ids and numbers; an open app reads
        # each channel again on the `channel` events that follow the move.
        conn.execute("DELETE FROM events")
        row = conn.execute("SELECT value FROM meta WHERE key=?", (wf.REDIRECTS_KEY,)).fetchone()
        redirects = wf._loads(row["value"]) if row else {}
        redirects = redirects if isinstance(redirects, dict) else {}
        redirects.update(old_to_new)
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                     (wf.REDIRECTS_KEY, json.dumps(redirects, sort_keys=True)))
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def _rewrite_links(conn: sqlite3.Connection, kept: dict[str, dict], site: str,
                   report: Report) -> None:
    """Point each row's file and each link in its text at the copy kept here."""
    def link(m: re.Match) -> str:
        if m.group("site") and m.group("site") != site:
            return m.group(0)
        f = kept.get(m.group("path"))
        if f is None:
            return m.group(0)
        report.links += 1
        return f"({f['url']})"

    for r in conn.execute("SELECT seq, text, file FROM messages").fetchall():
        sets: dict[str, Any] = {}
        f = wf._loads(r["file"])
        if isinstance(f, dict) and not f.get("id"):
            got = kept.get(_zulip_path(f.get("url") or "", site))
            if got is not None:
                sets["file"] = json.dumps({**got, "name": f.get("name") or got["name"]})
        text = _UPLOAD_LINK_RE.sub(link, r["text"] or "")
        if text != (r["text"] or ""):
            sets["text"] = text
        if sets:
            conn.execute(f"UPDATE messages SET {', '.join(f'{k}=?' for k in sets)} WHERE seq=?",
                         (*sets.values(), r["seq"]))


def _fold_channels(conn: sqlite3.Connection, moves: list[Move]) -> None:
    """Every row of a moving channel, in every table, takes the new id; the
    channel rows are replaced by the web chat's own."""
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT IN "
        "('channels', 'messages', 'events', 'sqlite_sequence')")]
    columns = {t: {c[1] for c in conn.execute(f"PRAGMA table_info({t})")} for t in tables}
    made: set[str] = set()
    for m in moves:
        conn.execute("DELETE FROM channels WHERE id=?", (m.old,))
        if m.new not in made:
            conn.execute("INSERT OR REPLACE INTO channels(id, name, kind, archived, description) "
                         "VALUES (?,?,?,?,?)", (m.new, m.new_name, m.new_kind,
                                                int(m.archived and m.new_kind == "room"),
                                                m.description))
            made.add(m.new)
        # A message the new channel holds already (one Zulip moved between two
        # streams that fold together) keeps the row there.
        conn.execute("DELETE FROM messages WHERE channel=? AND id IN "
                     "(SELECT id FROM messages WHERE channel=?)", (m.old, m.new))
        conn.execute("UPDATE messages SET channel=? WHERE channel=?", (m.new, m.old))
        for t in tables:
            for col in ("channel", "card_channel"):
                if col in columns[t]:
                    conn.execute(f"UPDATE OR REPLACE {t} SET {col}=? WHERE {col}=?",
                                 (m.new, m.old))
            if "tag" in columns[t]:
                # A push's tag is `<channel>/<thread key>`.
                head = m.old + "/"
                conn.execute(f"UPDATE OR REPLACE {t} SET tag = ? || substr(tag, ?) "
                             f"WHERE substr(tag, 1, ?) = ?",
                             (m.new + "/", len(head) + 1, len(head), head))


def _resequence(conn: sqlite3.Connection, before: dict[int, str], marks: dict[str, int],
                moves: dict[str, str]) -> None:
    """Number the messages again by time (posted_at, then the old number),
    and carry each number that names a message: the read marks, a queued
    push's message, the message a thread's state was judged at."""
    order = [r["seq"] for r in conn.execute("SELECT seq FROM messages ORDER BY posted_at, seq")]
    new_of = {old: i + 1 for i, old in enumerate(order)}
    conn.execute("UPDATE messages SET seq = -seq")
    conn.executemany("UPDATE messages SET seq=? WHERE seq=?",
                     [(new, -old) for old, new in new_of.items()])
    # A read mark counted the messages up to it in its own channel; it now
    # sits at the newest of those, wherever the history went.
    conn.execute("DELETE FROM reads")
    new_marks: dict[str, int] = {}
    for old_seq, old_channel in before.items():
        mark = marks.get(old_channel)
        if mark is None or old_seq > mark or old_seq not in new_of:
            continue
        channel = moves.get(old_channel, old_channel)
        new_marks[channel] = max(new_marks.get(channel, 0), new_of[old_seq])
    conn.executemany("INSERT INTO reads(channel, seq) VALUES (?,?)", list(new_marks.items()))
    for table, col in (("push_queue", "seq"), ("thread_state", "judged_seq")):
        for r in conn.execute(f"SELECT rowid, {col} FROM {table} WHERE {col} IS NOT NULL").fetchall():
            conn.execute(f"UPDATE {table} SET {col}=? WHERE rowid=?",
                         (new_of.get(int(r[1])), r[0]))


def move_registries(install: Install, moves: list[Move], report: Report) -> list[str]:
    """The room registry, the owner's room record, the session rows and the
    card registry, pointed at the new ids; what failed, each with its fix."""
    import ea_channels
    import owner_rooms
    import slack_sessions
    rooms = {m.old: m.new for m in moves if m.new_kind == "room"}
    every = {m.old: m.new for m in moves}
    urls = {new: f"{install.web_site}/#/{urllib.parse.quote(new, safe='')}"
            for new in rooms.values()}
    failed = []
    try:
        report.rooms_moved = ea_channels.move_channels(rooms, urls)
    except Exception as e:
        failed.append(f"{ea_channels.REGISTRY}: {e}; move each room by hand with "
                      f"preprocessor/ea_channels.py register --channel r<n> ...")
    try:
        report.owner_rooms_moved = owner_rooms.move(rooms)
    except Exception as e:
        failed.append(f"{owner_rooms.path()}: {e}")
    try:
        report.sessions = slack_sessions.move_channels(every)
    except Exception as e:
        failed.append(f"{slack_sessions.REGISTRY}: {e}")
    cards = install.priv / "chat-cards.json"
    if cards.exists():
        try:
            from botcore.chat.cards import CardRegistry
            report.cards = CardRegistry(cards).move_channels(every)
        except Exception as e:
            failed.append(f"{cards}: {e}")
    return failed


def rewrite_chat(install: Install, moves: list[Move], report: Report) -> None:
    """chat.json: the web chat alone, with the notifier keys that named a
    stream other than the EA stream pointing at its web channel."""
    block = json.loads(json.dumps(install.chat))
    block["frontend"] = "web"
    block.pop("shadow", None)
    web = block.get("web") if isinstance(block.get("web"), dict) else {}
    report.notify = notify_map(install, moves)
    if report.notify:
        web["notify"] = {**report.notify, **(web.get("notify") or {})}
    block["web"] = web
    gid = os.lstat(install.chat_path).st_gid
    owner_trust.write_owner_file(install.chat_path, json.dumps(block, indent=2) + "\n",
                                 mode=0o600, gid=gid if gid != os.getegid() else None)


def apply(install: Install, primary: Any, *, log: Callable[[str], None] = print) -> Report:
    """The move (see the module docstring); the caller checked that the bot
    is down. Raises CutoverError before the store is replaced when a step
    fails, with the store as it was."""
    from botcore.chat.shadow_frontend import ShadowFrontend
    report = Report(zulip=dict(install.zulip))
    journal = Path(str(install.store) + "-journal")
    if journal.exists() and journal.stat().st_size:
        raise CutoverError(f"{journal} is there: the store was left mid-write. Start the bot "
                           "once (SQLite then finishes or undoes the write), stop it, and run "
                           "apply again")
    log("1. The snapshot")
    report.backup = str(take_backup(install))
    log(f"  {report.backup}")
    moves = plan_moves(store_channels(install.store), install.zulip)
    report.moves = moves
    work = _work_copy(install.store)
    web = wf.WebFrontend(install.web, store_path=work, writer_only=True)
    try:
        web.start()
        shadow = ShadowFrontend(primary, web)
        log("2. The history, from Zulip")
        export_rows(shadow, web, moves, report, log)
        kept = fetch_files(primary, web, install, report, log)
        web.store.close()
        log("3. The channels fold into the web chat's own")
        fold(work, moves, kept, install, report)
        store = wf.WebStore(work)
        for new in dict.fromkeys(m.new for m in moves):
            store.touch_channel(new)
        store.close()
    except BaseException:
        for p in (work, Path(str(work) + "-journal")):
            if p.exists():
                p.unlink()
        raise
    log("4. chat.json, the store and the registries")
    try:
        rewrite_chat(install, moves, report)
    except BaseException:
        work.unlink()
        raise
    os.replace(work, install.store)
    failed = move_registries(install, moves, report)
    for line in failed:
        log(f"  not moved: {line}")
    report.failed = failed
    return report


def _notify_note(notify: dict) -> str:
    pairs = ", ".join(k + " -> " + v["channel"] for k, v in notify.items())
    return f"; notify {pairs}" if pairs else ""


def summary(report: Report) -> str:
    rooms = sum(1 for m in report.moves if m.new_kind == "room")
    lines = ["", "Moved to the web chat alone.",
             f"  snapshot: {report.backup} (put it back: scripts/webchat-backup.sh --restore "
             f"{Path(report.backup).name})",
             f"  history: {report.exported} message(s) brought over from Zulip",
             f"  files: {report.files} kept here, {report.links} link(s) pointed at them"
             + (f"; not downloaded: {', '.join(report.files_failed[:10])}"
                if report.files_failed else ""),
             f"  channels: {len(report.moves)} folded ({rooms} room(s) now r<n>)",
             f"  registries: {len(report.rooms_moved)} room(s), {len(report.owner_rooms_moved)} "
             f"in the room record, {report.sessions} session row(s), {report.cards} card(s)",
             "  chat.json: frontend web, no shadow" + _notify_note(report.notify)]
    if report.failed:
        # A registry the move could not write keeps its Zulip ids. The
        # store, chat.json and the other registries are moved; these need
        # the owner's hand (2026-11-09: a sessions registry the owner's own
        # processes had written, which the resident could not open).
        lines.append("  NOT moved (fix the file, then move it by hand or run apply again):")
        lines.extend(f"    {line}" for line in report.failed)
    lines += ["",
             f"Start the bot: {service_cmd('start')}, then open the web chat and run "
             "python3 /opt/ea-system/scripts/ea-doctor.py.",
             ""]
    return "\n".join(lines) + "\n" + teardown_text(report.zulip)


def teardown_text(zulip: dict | None = None) -> str:
    """The owner's steps that take the Zulip server down, once the web chat
    alone works (docs/zulip-switch.md, Uninstall Zulip). The tailnet port
    is the one the zulip block's `site` names; with no site the step says
    how to find it rather than guessing one (2026-11-09: the fixed 8443 was
    another service's port on one install, and Zulip was on 443)."""
    port = zulip_port(zulip or {})
    if port is not None:
        publish = f""" 2. Stop publishing the server on your tailnet (port {port}):
      sudo tailscale serve --https={port} off
 3. In the Tailscale admin console (Access controls), delete the rule and the
    two tests that name tag:ea-host:{port}; keep the tag if other rules use it."""
    else:
        publish = f""" 2. Stop publishing the server on your tailnet. Its port could not be
    determined (chat.json names no Zulip site): `tailscale serve status` lists
    the published ports; the one whose proxy is {ZULIP_BACKEND} is Zulip's:
      sudo tailscale serve --https=<that port> off
 3. In the Tailscale admin console (Access controls), delete the rule and the
    two tests that name tag:ea-host:<that port>; keep the tag if other rules
    use it."""
    return f"""Take Zulip down, once the web chat alone works (docs/zulip-switch.md, Uninstall Zulip).
Run these yourself, from your checkout; each step says what it removes.
 1. A last backup of the realm, kept as long as you want Zulip's own copy:
      deploy/zulip/backup.sh
{publish}
 4. The containers, their network and the volumes (the realm, every message):
      deploy/zulip/compose.sh down -v
    then the images: docker image ls | grep -i zulip, and docker image rm each.
 5. The nightly realm backup: crontab -e and delete the deploy/zulip/backup.sh
    line (and from crontab.master, if you keep one).
 6. The server's env file (database, queue and cache passwords):
      rm ~/.ea-private/zulip.env
 7. The bot's API key: delete the ZULIP_API_KEY line from
    ~/.ea-private/secrets.env, then {service_cmd('restart')}.
 8. Optional, the bot's Zulip state: rm ~/.ea-private/zulip-queue.json
    ~/.ea-private/zulip-moves.json. Keep chat-cards.json: the web chat uses it.
    The zulip block in chat.json is kept for a switch back; delete it when
    there will be none.
 9. Sign the Zulip apps out on your phone and laptop and remove them."""


# Zulip, as the bot reads it

def zulip_primary(install: Install) -> Any:
    """The Zulip front end on the chat block's settings, identified (the
    bot's own id, so its posts and the owner's web posts are told apart),
    with no event queue opened."""
    import ea_config
    from botcore.chat.zulip_frontend import ZulipFrontend
    key = owner_trust.owner_secret("ZULIP_API_KEY")
    if not key:
        raise CutoverError("ZULIP_API_KEY is not in ~/.ea-private/secrets.env; the export "
                           "reads Zulip as the bot")
    cfg = ea_config.chat_from_block(install.chat)["zulip"]
    try:
        fe = ZulipFrontend(cfg, api_key=key, private_dir=install.priv)
    except ModuleNotFoundError as e:
        raise CutoverError(f"this Python has no {e.name!r} package: run the script with the "
                           f"Python the bot runs ({bot_python_hint()})") from None
    prof = fe.client.get_profile()
    if not isinstance(prof, dict) or prof.get("result") != "success":
        raise CutoverError(f"Zulip did not answer as the bot: {prof}")
    fe.self_id = int(prof["user_id"])
    fe.bot_user_id = str(fe.self_id)
    return fe


def main(argv: list[str] | None = None, *, primary_factory: Callable[[Install], Any] = zulip_primary,
         priv: Path | None = None) -> int:
    ap = argparse.ArgumentParser(prog="webchat-cutover.py",
                                 description="Move from Zulip with the web chat beside it to "
                                             "the web chat alone.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan", help="what apply would change; writes nothing")
    p.add_argument("--zulip", action="store_true",
                   help="also read Zulip (reads only) and count what the export brings over")
    p = sub.add_parser("apply", help="the move")
    p.add_argument("--while-running", action="store_true",
                   help="go ahead with the bot up (what it writes meanwhile is lost)")
    sub.add_parser("teardown", help="print the steps that take Zulip down")
    a = ap.parse_args(argv)
    if a.cmd == "teardown":
        print(teardown_text(_zulip_block(priv)))
        return 0
    try:
        install = load_install(priv)
        if a.cmd == "plan":
            moves = plan_moves(store_channels(install.store), install.zulip)
            counts = count_zulip(primary_factory(install), moves) if a.zulip else None
            print(plan_text(install, moves, zulip_counts=counts))
            up = bot_up(install)
            if up:
                print(f"\nThe bot is up ({up}): stop it before apply "
                      f"({service_cmd('stop')}).")
            return 0
        up = bot_up(install)
        if up and not a.while_running:
            print(f"webchat-cutover: the bot is up ({up}). Stop it first "
                  f"({service_cmd('stop')}): while it runs it keeps the old store "
                  "open and writes into it, and those writes are lost when the new store takes "
                  "its place; it keeps the card registry in memory and writes it back over the "
                  "moved one; and it serves Zulip until it starts again on the new chat.json. "
                  "--while-running goes ahead anyway.", file=sys.stderr)
            return 2
        try:
            report = apply(install, primary_factory(install))
        except CutoverError:
            raise
        except Exception as e:
            print(f"webchat-cutover: the move stopped ({type(e).__name__}: {e}). The store, "
                  "the registries and chat.json are as they were; the snapshot under "
                  f"{install.priv / 'backups' / 'webchat'} stays. Fix the cause and run apply "
                  "again.", file=sys.stderr)
            return 1
        print(summary(report))
        if up:
            print("\nThe bot was up during the move: restart it now "
                  f"({service_cmd('restart')}).")
        return 1 if report.failed else 0
    except CutoverError as e:
        print(f"webchat-cutover: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

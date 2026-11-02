#!/usr/bin/env python3
"""A stand-in for the web chat server, for the web app's tests and for
working on the app by hand. Standard library only.

It serves botcore/botcore/chat/webui/ under /chat/ and fakes the web front
end's HTTP API with made-up data held in memory: channels (a DM, the main
and scheduling channels, a room and an archived room), threads, a draft
card with actions, a card with a select, a status line, file messages and
reactions. The event stream replays from Last-Event-ID or ?since= and then
pushes each new event; with neither it sends only what happens next.

A post gets an echo (an eyes reaction, a bot reply, a check); a card
click is recorded in `clicks` and the card turns into a status line. A
post that asks for a room ("open a room for X") gets a new room, named
from X, announced with a `channel` event, as the bot's reply says.
`emit_bot` (or POST /_stub/emit) posts a bot message from outside, and
`--tick N` posts one every N seconds. `drop_streams` closes the open
streams (and can refuse the next ones); `streams` records how each stream
request asked to resume. `--pair-token T` makes every API route answer
401 until the browser pairs with T. `relay_pairing=True` runs a relay that
pairs only through its own copy of the app (a bot with no public_url, on
this machine): /api/me says `relay`, `pairs` records each pairing,
`devices` are the paired phones "Pair a phone" lists, and `approvals`
records each Approve (with its `cards` choice). The Settings screen reads
`routing` (three made-up rows: one free, one under the keep floor, one
under the stakes floor) and `settings_posts` records each Save; a value a
row does not allow answers 400, as the bot's floors do. Only with
`settings=True` does /api/me name the page.

Each thread can have a state (`states`, seeded with made-up ones in three
channels): the thread rows carry it, GET /api/needs lists the threads
that need the owner or wait, and POST /api/threads/<ch>/<key>/state sets
the owner's own (recorded in `state_posts`). `set_state` changes one as
the bot's rules would; each change is a `thread_state` event. A state
carries `stale` as the bot's stale shelf sets it; `add_stale` adds made-up
threads whose state is days old and on the shelf.

With `sign=True` (or `--sign`) it signs the app's files as the bot does
(web_bundle, which needs the cryptography package) and serves the signed
manifest at /chat/manifest.sig.json, and a relay pairing carries its
hash; `files_served` maps a file's name to the bytes served in its place,
as a server that changed it would.

    python3 tests/webui_stub_server.py [--port 8471] [--tick 5] [--pair-token T] [--sign]
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import threading
import time
from email.parser import BytesParser
from email.policy import HTTP
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

WEBUI = Path(__file__).resolve().parent.parent / "botcore" / "botcore" / "chat" / "webui"
BASE = "/chat"
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".webmanifest": "application/manifest+json",
         ".svg": "image/svg+xml", ".png": "image/png"}
# The policy the real server is expected to send: nothing inline, nothing
# from elsewhere. The app must work under it.
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
       "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")

DM, MAIN, SCHED, ROOM, OLD = "stream:7", "stream:9", "stream:11", "r1", "r2"
# The install /api/relay/status names; a pairing link for another is refused.
STUB_INSTALL = "a" * 26
STUB_BOT_PUBLIC = "AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE"  # 32 bytes of 1, base64url
STUB_CLIENT_KEY = "c" * 43
STUB_BUNDLE = "b" * 64  # a made-up hash of the bot's signed app bundle, when it signs none
WEEKLY = "stream:9:topic:weekly%20plan"
ROOM_ASK = re.compile(r"^\s*open a (?:channel|room) for (?P<name>.+?)\s*$", re.I)
ROUTING = {
    "local": {"configured": True, "model": "local-27b", "reachable": True, "detail": "Answered 2 min ago."},
    "source": "routing.json", "values": ["local", "hosted", "auto"],
    "rows": [
        {"task": "drafting", "label": "Email bodies", "what": "Writes email bodies in your voice from the brief.",
         "value": "auto", "allowed": ["local", "hosted", "auto"], "locked": False, "floor": "",
         "effective": "local", "why": "the drafting switch is remote"},
        {"task": "mail", "label": "Read new mail", "what": "Reads each new message into a record.",
         "value": "auto", "allowed": ["local", "auto"], "locked": False,
         "floor": "Keep floor: hosted is not available while the keep list has entries.",
         "effective": "local", "why": "the mail switch is enforce"},
        {"task": "review", "label": "Judgment checks", "what": "Checks if briefing items are still open.",
         "value": "hosted", "allowed": ["hosted"], "locked": True,
         "floor": "Stakes floor: these are judgment calls, so only the hosted model does them.",
         "effective": "hosted", "why": "the stakes floor"},
    ],
}


def _b64(data: str | bytes) -> str:
    raw = data.encode() if isinstance(data, str) else data
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class Stub:
    """The in-memory store: channels, messages, files and the event log."""

    def __init__(self, tick: float = 0.0, pair_token: str | None = None, echo: bool = True,
                 sign: bool = False, relay_pairing: bool = False, settings: bool = False):
        self.cond = threading.Condition()
        self.settings = settings
        self.routing = json.loads(json.dumps(ROUTING))
        self.settings_posts: list[dict] = []
        # The signed bundle of WEBUI (web_bundle.Bundle) when `sign`; the
        # bytes served in place of a file, by name.
        self.bundle = None
        self.files_served: dict[str, bytes] = {}
        if sign:
            import sys
            sys.path.insert(0, str(WEBUI.parents[2]))
            from botcore.chat import relay_crypto, web_bundle
            self.bundle = web_bundle.Bundle(WEBUI, relay_crypto.signing_keypair()[0])
        self.pair_token = pair_token
        self.relay_pairing = relay_pairing
        self.pairs: list[dict] = []
        self.devices: list[dict] = []
        self.approvals: list[dict] = []
        self.diags: list[dict] = []  # what the app reported under /diag
        self.echo = echo
        self.closed = False
        self.seq = 0
        self.eseq = 0
        self.messages: list[dict] = []
        self.events: list[tuple[int, str, dict]] = []
        self.files: dict[str, tuple[str, str, bytes]] = {}
        self.clicks: list[dict] = []
        self.posts: list[dict] = []
        # (channel, thread key) -> {"state", "by", "reason", "at"}
        self.states: dict[tuple[str, str], dict] = {}
        self.state_posts: list[dict] = []
        # Each stream request: its Last-Event-ID header and its ?since=.
        self.streams: list[dict] = []
        self.refuse_streams = 0
        self.generation = 0
        self.channels = {
            DM: {"id": DM, "name": "", "kind": "dm", "archived": False, "unread": 0},
            MAIN: {"id": MAIN, "name": "ea", "kind": "main", "archived": False, "unread": 2},
            SCHED: {"id": SCHED, "name": "ea-scheduling", "kind": "sched", "archived": False, "unread": 0},
            ROOM: {"id": ROOM, "name": "ea-example-project", "kind": "room", "archived": False,
                   "unread": 0, "description": "A made-up project room"},
            OLD: {"id": OLD, "name": "ea-old-project", "kind": "room", "archived": True, "unread": 0},
        }
        self._seed()
        if tick > 0:
            threading.Thread(target=self._ticker, args=(tick,), daemon=True).start()

    def bundle_hash(self) -> str:
        """The hash a pairing link carries: the signed bundle's, else a made-up one."""
        return self.bundle.hash if self.bundle else STUB_BUNDLE

    # store

    def add(self, channel: str, text: str = "", *, author: str = "bot", kind: str = "message",
            thread_key: str | None = None, card: dict | None = None, file: dict | None = None,
            reactions: list | None = None, at: float | None = None, event: str | None = None) -> dict:
        with self.cond:
            self.seq += 1
            now = at or time.time()
            m = {"seq": self.seq, "channel": channel, "id": f"w{self.seq}",
                 "thread_key": thread_key or f"t{self.seq}", "author": author, "kind": kind,
                 "text": text, "card": card, "file": file, "reactions": list(reactions or []),
                 "posted_at": now, "updated_at": now}
            self.messages.append(m)
            if event:
                self._event(event, m)
            return m

    def _event(self, typ: str, m: dict) -> None:
        self.eseq += 1
        self.events.append((self.eseq, typ, self.view(m)))
        self.cond.notify_all()

    def change(self, m: dict, typ: str, **fields) -> None:
        with self.cond:
            m.update(fields, updated_at=time.time())
            self._event(typ, m)

    def emit_bot(self, channel: str = DM, text: str = "A new message from the stub.",
                 thread_key: str | None = None) -> dict:
        return self.add(channel, text, thread_key=thread_key, event="message")

    def find(self, channel: str, msg_id: str) -> dict | None:
        return next((m for m in self.messages if m["channel"] == channel and m["id"] == msg_id), None)

    def thread(self, channel: str, key: str) -> list[dict]:
        return [m for m in self.messages if m["channel"] == channel and m["thread_key"] == key]

    def view(self, m: dict) -> dict:
        rows = self.thread(m["channel"], m["thread_key"])
        out = dict(m)
        if rows and rows[0] is m:
            out["reply_count"] = len(rows) - 1
            out["last_at"] = rows[-1]["posted_at"]
        return out

    def parents(self, channel: str, limit: int, before: int | None) -> list[dict]:
        seen, out = set(), []
        for m in self.messages:
            if m["channel"] == channel and m["thread_key"] not in seen:
                seen.add(m["thread_key"])
                if before is None or m["seq"] < before:
                    out.append(self.row(m))
        out.sort(key=lambda p: p["seq"], reverse=True)
        return out[:limit]

    def row(self, m: dict) -> dict:
        """A thread row as the threads route gives it: the parent, its
        replies and its state."""
        st = self.states.get((m["channel"], m["thread_key"])) or {}
        return {**self.view(m), "state": st.get("state", "none"), "state_by": st.get("by", ""),
                "state_reason": st.get("reason", ""), "state_at": st.get("at", 0),
                "stale": bool(st.get("stale"))}

    def set_state(self, channel: str, thread_key: str, state: str, by: str = "rule",
                  reason: str = "", *, at: float | None = None, stale: bool = False) -> dict:
        """Set a thread's state and send the `thread_state` event."""
        with self.cond:
            st = {"state": state, "by": by, "reason": reason, "at": at or time.time(),
                  "stale": stale}
            self.states[(channel, thread_key)] = st
            self.eseq += 1
            self.events.append((self.eseq, "thread_state",
                                {"channel": channel, "thread_key": thread_key, **st}))
            self.cond.notify_all()
            return st

    def add_stale(self, items=None) -> list[dict]:
        """Threads whose state is days old and on the stale shelf, quietly
        (as history): [(channel, text, state, days)]; the first messages."""
        items = items or [(MAIN, "Reply to the visiting speaker about parking", "needs_you", 9),
                          (ROOM, "Check the draft budget table", "needs_you", 12),
                          (SCHED, "Find a time with the review panel", "waiting", 16)]
        out = []
        for channel, text, state, days in items:
            at = time.time() - days * 86400
            m = self.add(channel, text, author="owner", at=at)
            self.add(channel, "Done. One question is left for you.", thread_key=m["thread_key"], at=at + 60)
            self.states[(channel, m["thread_key"])] = {
                "state": state, "by": "model", "reason": "A question waits for you.", "at": at,
                "stale": True}
            out.append(m)
        return out

    def needs(self) -> list[dict]:
        """Every thread that needs the owner or waits, needs you first,
        then newest first, each with its channel's name."""
        out = [{**p, "channel_name": self.channels[c]["name"]} for c in self.channels
               for p in self.parents(c, 10 ** 6, None) if p["state"] in ("needs_you", "waiting")]
        out.sort(key=lambda p: (p["state"] != "needs_you", -p.get("last_at", p["posted_at"])))
        return out[:200]

    def add_room(self, project: str) -> dict:
        """A new room for `project`, sent as a `channel` event."""
        with self.cond:
            slug = re.sub(r"[^a-z0-9]+", "-", project.lower()).strip("-")
            row = {"id": f"r{len(self.channels) + 1}", "name": f"ea-{slug}", "kind": "room",
                   "archived": False, "unread": 0}
            self.channels[row["id"]] = row
            self.eseq += 1
            self.events.append((self.eseq, "channel", dict(row)))
            self.cond.notify_all()
            return row

    def drop_streams(self, refuse: int = 0) -> None:
        """Close every open event stream; the next `refuse` stream
        requests get a 503, as a server restarting would answer."""
        with self.cond:
            self.generation += 1
            self.refuse_streams = refuse
            self.cond.notify_all()

    def close(self) -> None:
        with self.cond:
            self.closed = True
            self.cond.notify_all()

    def _ticker(self, every: float) -> None:
        n = 0
        while not self.closed:
            time.sleep(every)
            n += 1
            self.emit_bot(MAIN, f"Tick {n} from the stub.", WEEKLY)

    def _seed(self) -> None:
        t0 = time.time() - 3 * 86400
        ask = self.add(DM, "Draft a reply to the budget question from the example lab.",
                       author="owner", reactions=["eyes", "white_check_mark"], at=t0)
        tk = ask["thread_key"]
        self.add(DM, "Draft created. **To:** someone@example.org\n\n- Point one\n- Point *two*\n\n"
                 "The row is `ledger 12`. See [the guide](https://example.org/guide) :memo:",
                 thread_key=tk, at=t0 + 60)
        self.add(DM, "", kind="card", thread_key=tk, at=t0 + 61, card={
            "kind": "send", "title": ":lock: *Confirm send*",
            "fields": [{"label": "To", "value": "someone@example.org"},
                       {"label": "Subject", "value": "Re: \ue000Budget\ue001 question \u202eevil"}],
            "lines": ["Reply-all to the thread. <https://example.org/thread|Open the thread>"],
            "body": "Hi there,\n\nThe budget covers both trips.\n\n<b>not bold</b>\n\nThanks",
            "quoted": "On Monday someone wrote:\n> Does the budget cover the trips?",
            "actions": [
                {"id": "send", "label": "Send", "value": {"draft": "d1"}, "style": "primary", "word": "send"},
                {"id": "edit", "label": ":pencil2: Edit", "value": {"draft": "d1"}, "style": "", "word": "edit"},
                {"id": "cancel", "label": "Cancel", "value": {"draft": "d1"}, "style": "danger", "word": "cancel"}],
            "select": None, "block_id": "", "footer": "Code ABCD", "fallback_text": "Confirm send",
            "thread_ts": tk, "ea_channel": DM, "content_fingerprint": "", "no_default": False})
        odd = self.add(DM, "Markup check: <img src=x onerror=\"window.__xss=1\"> "
                       "<script>window.__xss=2</script> [click](javascript:window.__xss=3)\n\n"
                       "| Item | Cost |\n|---|---|\n| Trip | 100 |\n\n```\nprint('<b>')\n```",
                       at=t0 + 3600)
        self.add(DM, ":white_check_mark: Sent to someone@example.org.", kind="status",
                 thread_key=odd["thread_key"], at=t0 + 3700)
        self.add(DM, "", kind="card", at=t0 + 7200, card={
            "kind": "room", "title": ":lock: *Open a room*", "fields": [],
            "lines": ["Pick a project for this thread."], "body": "", "quoted": "",
            "actions": [{"id": "create", "label": "Create", "value": "c", "style": "primary", "word": "create"},
                        {"id": "skip", "label": "Skip", "value": "s", "style": "", "word": "skip"}],
            "select": {"id": "pick_project", "placeholder": "Choose a project",
                       "options": [["Example project", "p1"], ["Another project", "p2"]]},
            "block_id": "", "footer": "", "fallback_text": "Open a room", "thread_ts": "",
            "ea_channel": DM, "content_fingerprint": "", "no_default": False})
        png = (WEBUI / "icon-192.png").read_bytes()
        self.files["f1"] = ("figure.png", "image/png", png)
        self.files["f2"] = ("notes.pdf", "application/pdf", b"%PDF-1.4 made-up\n")
        fig = self.add(DM, "Here is the figure.", author="owner", kind="file", at=t0 + 9000,
                       file={"id": "f1", "name": "figure.png", "url": "", "mimetype": "image/png",
                             "size": len(png)})
        self.add(DM, "", kind="file", thread_key=fig["thread_key"], at=t0 + 9100,
                 file={"id": "f2", "name": "notes.pdf", "url": "", "mimetype": "application/pdf",
                       "size": 17})
        self.add(MAIN, ":calendar: Weekly plan\n\n# Monday\nTwo meetings.", thread_key=WEEKLY, at=t0 + 100)
        self.add(MAIN, "Tuesday is open.", thread_key=WEEKLY, at=t0 + 200)
        self.add(MAIN, "Wednesday has a review.", thread_key=WEEKLY, at=t0 + 300)
        sched = self.add(SCHED, ":hourglass_flowing_sand: Finding a time for the review.", at=t0 + 400)
        room = self.add(ROOM, "Project notes live here.", at=t0 + 500)
        self.add(OLD, "This room is archived.", at=t0 - 86400)
        for m, state, reason in (
                (ask, "needs_you", "The card still has its Send button."),
                (odd, "done", "The card was acted on and nothing asks anything since."),
                (fig, "working", "You wrote last and the bot has not answered yet."),
                (sched, "waiting", "The other side has not picked a time yet."),
                (room, "needs_you", "The bot asked which project this is for.")):
            self.states[(m["channel"], m["thread_key"])] = {"state": state, "by": "rule", "reason": reason,
                                                            "at": m["posted_at"]}

    # inbound from the app

    def post(self, channel: str, text: str, thread_key: str | None, file: dict | None = None) -> dict:
        m = self.add(channel, text, author="owner", kind="file" if file else "message",
                     thread_key=thread_key, file=file, event="file" if file else "message")
        self.posts.append({"channel": channel, "text": text, "thread_key": thread_key, "file": file})
        if self.echo:
            def later():
                self.change(m, "reaction", reactions=["eyes"])
                ask = ROOM_ASK.match(text)
                reply = f"Stub reply to: {text[:40] or 'a file'}"
                if ask:
                    reply = f":file_folder: Opened #{self.add_room(ask['name'])['name']} for *{ask['name']}*."
                self.add(channel, reply, thread_key=m["thread_key"], event="message")
                self.change(m, "reaction", reactions=["white_check_mark"])
            threading.Timer(0.3, later).start()
        return self.view(m)

    def click(self, channel: str, msg_id: str, body: dict) -> bool:
        m = self.find(channel, msg_id)
        if not m or m["kind"] != "card":
            return False
        self.clicks.append({"channel": channel, "id": msg_id, **body})
        label = body.get("selected") or body.get("action_id")
        threading.Timer(0.2, lambda: self.change(
            m, "status", kind="status", text=f":white_check_mark: Done: {label}.")).start()
        return True


class Handler(BaseHTTPRequestHandler):
    stub: Stub

    def log_message(self, *args) -> None:
        pass

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store" if "json" in ctype else "no-cache")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, data, extra: dict | None = None) -> None:
        self._send(code, json.dumps(data).encode(), "application/json", extra)

    def _owner(self) -> bool:
        if not self.stub.pair_token:
            return True
        jar = SimpleCookie(self.headers.get("Cookie") or "")
        return "ea_web" in jar and jar["ea_web"].value == self.stub.pair_token

    def _body(self) -> bytes:
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def _parts(self) -> list[tuple[str, str | None, bytes]]:
        ctype = self.headers.get("Content-Type") or ""
        msg = BytesParser(policy=HTTP).parsebytes(b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + self._body())
        return [(p.get_param("name", header="content-disposition") or "", p.get_filename(),
                 p.get_payload(decode=True) or b"") for p in msg.iter_parts()]

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        path = url.path
        if path == BASE:
            self.send_response(301)
            self.send_header("Location", BASE + "/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if not path.startswith(BASE + "/"):
            return self._json(404, {"error": "not found"})
        rest = path[len(BASE) + 1:]
        if rest == "manifest.sig.json":
            if self.stub.bundle is None:
                return self._json(404, {"error": "not found"})
            return self._send(200, self.stub.bundle.body, "application/json")
        if not rest.startswith("api/"):
            return self._static(rest or "index.html")
        if not self._owner():
            return self._json(401, {"error": "not the owner"})
        parts = [unquote(p) for p in rest.split("/")[1:]]
        q = parse_qs(url.query)
        s = self.stub
        if parts == ["me"]:
            return self._json(200, {"owner": "owner", "bot": "bot", "public_url": None,
                                    "bundle": s.bundle.hash if s.bundle else "",
                                    "settings": ["routing"] if s.settings else [],
                                    **({"relay": True} if s.relay_pairing else {})})
        if parts == ["settings", "routing"] and s.settings:
            return self._json(200, s.routing)
        if parts == ["relay", "status"]:
            return self._json(200, {"enabled": True, "install_id": STUB_INSTALL, "connected": False,
                                    "relay_url": "https://relay.example.net",
                                    "bot_public": STUB_BOT_PUBLIC, "client_key": STUB_CLIENT_KEY,
                                    "pair_own": False, "pair_relay": s.relay_pairing, "relay_app": s.relay_pairing,
                                    "bundle": s.bundle_hash() if s.relay_pairing else None})
        if parts == ["relay", "devices"] and s.relay_pairing:
            return self._json(200, s.devices)
        if parts == ["channels"]:
            rows = []
            for c in s.channels.values():
                last = max((m["posted_at"] for m in s.messages if m["channel"] == c["id"]), default=None)
                rows.append({**c, "last_at": last})
            return self._json(200, rows)
        if len(parts) == 3 and parts[0] == "channels" and parts[2] == "threads":
            before = q.get("before", [None])[0]
            return self._json(200, s.parents(parts[1], int(q.get("limit", ["50"])[0]),
                                             int(before) if before else None))
        if len(parts) == 4 and parts[0] == "channels" and parts[2] == "threads":
            rows = [s.view(m) for m in s.thread(parts[1], parts[3])]
            return self._json(200 if rows else 404, rows if rows else {"error": "no such thread"})
        if len(parts) == 2 and parts[0] == "files" and parts[1] in s.files:
            name, mime, data = s.files[parts[1]]
            return self._send(200, data, mime, {"Content-Disposition": f'attachment; filename="{name}"'})
        if parts == ["needs"]:
            return self._json(200, {"threads": s.needs()})
        if parts == ["events"]:
            return self._events(q)
        return self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        s = self.stub
        if path == "/_stub/emit":
            data = json.loads(self._body() or b"{}")
            return self._json(200, s.emit_bot(data.get("channel", DM), data.get("text", "Hello"),
                                              data.get("thread_key")))
        if not path.startswith(BASE + "/api/"):
            return self._json(404, {"error": "not found"})
        parts = [unquote(p) for p in path[len(BASE) + 5:].split("/")]
        if parts == ["pair"]:
            try:
                token = json.loads(self._body() or b"{}").get("token")
            except ValueError:
                token = None
            if s.pair_token and token == s.pair_token:
                cookie = f"ea_web={token}; Path={BASE}; HttpOnly; SameSite=Strict"
                return self._json(200, {"ok": True}, {"Set-Cookie": cookie})
            return self._json(401, {"error": "not the owner"})
        if not self._owner():
            return self._json(401, {"error": "not the owner"})
        if len(parts) == 3 and parts[0] == "channels" and parts[1] in s.channels:
            if parts[2] == "messages":
                data = json.loads(self._body() or b"{}")
                if not str(data.get("text") or "").strip():
                    return self._json(400, {"error": "empty message"})
                return self._json(200, s.post(parts[1], data["text"], data.get("thread_key")))
            if parts[2] == "files":
                fields = {name: (fname, data) for name, fname, data in self._parts()}
                if "file" not in fields:
                    return self._json(400, {"error": "no file"})
                fname, data = fields["file"]
                fid = f"f{len(s.files) + 1}"
                mime = "image/png" if data.startswith(b"\x89PNG") else "application/octet-stream"
                s.files[fid] = (fname or "upload", mime, data)
                text = fields.get("text", (None, b""))[1].decode()
                tk = fields.get("thread_key", (None, b""))[1].decode() or None
                ref = {"id": fid, "name": fname, "url": "", "mimetype": mime, "size": len(data)}
                return self._json(200, s.post(parts[1], text, tk, file=ref))
        if parts == ["settings", "routing"] and s.settings:
            body = json.loads(self._body() or b"{}")
            s.settings_posts.append(body)
            rows = {r["task"]: r for r in s.routing["rows"]}
            for task, value in (body.get("tasks") or {}).items():
                if value not in rows[task]["allowed"]:
                    return self._json(400, {"error": f"{task} is {rows[task]['allowed'][0]} only (the stakes floor)"})
            for task, value in body["tasks"].items():
                rows[task]["value"] = value
                if value != "auto":
                    rows[task].update(effective=value, why=f"set to {value}")
            return self._json(200, {**s.routing, "saved": True})
        if parts == ["diag"]:
            s.diags.append(json.loads(self._body() or b"{}"))
            return self._json(200, {"ok": True})
        if parts == ["relay", "approve"] and s.relay_pairing:
            body = json.loads(self._body() or b"{}")
            s.approvals.append(body)
            for d in s.devices:
                if d["id"] == body.get("device"):
                    d.update(pending=False, cards_allowed=body.get("cards") is True)
            return self._json(200, {"ok": True})
        if parts == ["relay", "pair"] and s.relay_pairing:
            body = json.loads(self._body() or b"{}")
            s.pairs.append(body)
            if body.get("via") != "relay":
                return self._json(409, {"error": "the link would not open the app on the bot's "
                                                 "own origin: set public_url in the web block"})
            link = "https://relay.example.net/#tr2=" + _b64(json.dumps({
                "v": 2, "r": "https://relay.example.net", "i": STUB_INSTALL, "k": STUB_BOT_PUBLIC,
                "c": STUB_CLIENT_KEY, "p": "0" * 32, "s": _b64(bytes(32)),
                "x": int(time.time()) + 600, "b": s.bundle_hash(), "a": "https://relay.example.net/"}))
            return self._json(200, {"link": link, "qr": link, "expires": int(time.time()) + 600,
                                    "origin": "relay", "bundle": s.bundle_hash(), "access": "chat"})
        if len(parts) == 4 and parts[0] == "threads" and parts[3] == "state":
            body = json.loads(self._body() or b"{}")
            s.state_posts.append({"channel": parts[1], "thread_key": parts[2], **body})
            rows = s.thread(parts[1], parts[2])
            if not rows:
                return self._json(404, {"error": "no such thread"})
            if body.get("state") not in ("done", "needs_you", "waiting"):
                return self._json(400, {"error": "state must be done, needs_you or waiting"})
            s.set_state(parts[1], parts[2], body["state"], "owner", "You set this.")
            return self._json(200, s.row(rows[0]))
        if len(parts) == 4 and parts[0] == "cards" and parts[3] == "actions":
            body = json.loads(self._body() or b"{}")
            if s.click(parts[1], parts[2], body):
                return self._json(200, {"ok": True})
            return self._json(404, {"error": "no such card"})
        return self._json(404, {"error": "not found"})

    def _static(self, name: str) -> None:
        path = (WEBUI / name).resolve()
        if path.parent != WEBUI or not path.is_file():
            return self._json(404, {"error": "not found"})
        data = self.stub.files_served.get(path.name, None)
        self._send(200, path.read_bytes() if data is None else data,
                   TYPES.get(path.suffix, "application/octet-stream"),
                   {"Content-Security-Policy": CSP} if path.suffix == ".html" else None)

    def _events(self, q: dict) -> None:
        s = self.stub
        header = self.headers.get("Last-Event-ID")
        last = header or q.get("since", [""])[0]
        with s.cond:
            s.streams.append({"last_event_id": header, "since": q.get("since", [None])[0]})
            refuse = s.refuse_streams > 0
            s.refuse_streams -= refuse
            sent = int(last) if str(last).isdigit() else s.eseq
            generation = s.generation
        if refuse:
            return self._json(503, {"error": "stream refused"})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(b"retry: 300\n\n")
            while True:
                with s.cond:
                    if not any(e[0] > sent for e in s.events) and not s.closed:
                        s.cond.wait(timeout=15)
                    if s.closed or s.generation != generation:
                        return
                    todo = [e for e in s.events if e[0] > sent]
                if not todo:
                    self.wfile.write(b": ping\n\n")
                for seq, typ, data in todo:
                    self.wfile.write(f"id: {seq}\nevent: {typ}\ndata: {json.dumps(data)}\n\n".encode())
                    sent = seq
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return


def serve(port: int = 0, **kw) -> tuple[ThreadingHTTPServer, Stub]:
    """Start a stub on 127.0.0.1 in a daemon thread; (server, stub). The
    caller stops it with `stop(server, stub)`."""
    stub = Stub(**kw)
    handler = type("StubHandler", (Handler,), {"stub": stub})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, stub


def stop(server: ThreadingHTTPServer, stub: Stub) -> None:
    stub.close()
    server.shutdown()
    server.server_close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--port", type=int, default=8471)
    ap.add_argument("--tick", type=float, default=0.0, help="post a bot message every N seconds")
    ap.add_argument("--pair-token", default=None, help="require pairing with this token")
    ap.add_argument("--sign", action="store_true", help="sign the app's files as the bot does")
    args = ap.parse_args()
    server, stub = serve(args.port, tick=args.tick, pair_token=args.pair_token, sign=args.sign)
    print(f"stub web chat on http://127.0.0.1:{server.server_address[1]}{BASE}/", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        stop(server, stub)


if __name__ == "__main__":
    main()

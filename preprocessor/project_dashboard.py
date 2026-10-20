#!/usr/bin/env python3
"""Drag-and-drop dashboard over the project store.

A single stdlib HTTP server (no new dependencies) bound to the
Tailscale interface, serving one HTML page and a small JSON API. All
writes hit the same SQLite the CLI uses, with journal side effects, so
the Slack side and the dashboard can never disagree.

Filing is the whole point: the unfiled tray holds open ledger items
with no project, and dropping one on a card links it. Dragging a card
between domain columns re-domains it; dropping a card on another card
re-parents it.

Every string the board shows started life in someone else's email, so
the page escapes all of it and runs under a strict CSP whose script
nonce is minted here, per response. The mutation API is same-origin
only, and EA_DASH_TOKEN turns the whole surface into a bearer-token
one. The token comes from the environment, else the owner-private
secrets.env (`scripts/set-secret.sh EA_DASH_TOKEN`). It is optional on
loopback only: asked to bind any other address (the Tailscale IP
included) without it, the server binds 127.0.0.1 instead, since every
device on the tailnet could otherwise refile and rewrite the store. That
fallback prints a WARNING line to the journal and leaves FALLBACK_FILE in
the private directory, which ea-doctor.py reports; a start on the asked
address removes the file. The server does not exit, so a unit with
Restart=on-failure does not loop.

Usage:
    EA_DASH_TOKEN=<secret> python3 project_dashboard.py   # tailscale IP, else 127.0.0.1
    EA_DASH_BIND=127.0.0.1 python3 project_dashboard.py   # loopback, no token needed
    EA_DASH_BIND=127.0.0.1 EA_DASH_PORT=8377 python3 project_dashboard.py
    (with a token, open /?t=<secret>)
"""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import subprocess
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import project_store  # noqa: E402
from ledger import canonical_type  # noqa: E402

PORT = int(os.environ.get("EA_DASH_PORT", "8377"))
HTML_PATH = Path(__file__).resolve().parent / "dashboard.html"

LOOPBACK = "127.0.0.1"
# In the owner-private directory while the server runs on the loopback
# fallback (main); ea-doctor.py reads it.
FALLBACK_FILE = "dashboard-bind-fallback.json"


def _dash_token() -> str:
    """EA_DASH_TOKEN from the environment, else from the owner secrets."""
    val = os.environ.get("EA_DASH_TOKEN", "").strip()
    if val:
        return val
    try:
        import owner_trust
        return owner_trust.owner_secret("EA_DASH_TOKEN").strip()
    except Exception:
        return ""


# Shared secret for the whole HTTP surface: every request must carry ?t= /
# Bearer / X-EA-Token. Empty is allowed only on a loopback bind
# (check_bind).
DASH_TOKEN = _dash_token()

# 'nonce-...' rather than 'unsafe-inline': the page's own script block is
# nonced per response, so no attacker-supplied inline handler can run.
# Styles keep 'unsafe-inline' because the page uses style="" attributes,
# which no nonce can cover.
CSP_TEMPLATE = (
    "default-src 'none'; script-src 'nonce-{nonce}'; style-src 'unsafe-inline'; "
    "img-src 'self' data:; connect-src 'self'; form-action 'none'; "
    "base-uri 'none'; frame-ancestors 'none'"
)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def bind_address() -> str:
    """Explicit EA_DASH_BIND, else the Tailscale IP, else loopback."""
    explicit = os.environ.get("EA_DASH_BIND", "").strip()
    if explicit:
        return explicit
    try:
        out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True,
                             text=True, timeout=5).stdout.strip().splitlines()
        if out and re.fullmatch(r"100(\.\d{1,3}){3}", out[0].strip()):
            return out[0].strip()
    except Exception:
        pass
    return "127.0.0.1"


# ── Data layer (separable for tests) ────────────────────────────────────

def project_channels() -> dict:
    """project_id -> {"id", "name", "url"} from the channel registry;
    empty when none are registered or the module is unavailable."""
    try:
        import ea_channels
        return ea_channels.by_project()
    except Exception:
        return {}


def build_board() -> dict:
    conn = project_store._get_db()
    projects = [dict(r) for r in conn.execute(
        "SELECT id,name,domain,kind,status,parent_id,updated_at,"
        "substr(COALESCE(next_actions,''),1,90) AS next_actions "
        "FROM projects ORDER BY name")]
    counts = {}
    try:
        for r in conn.execute(
                "SELECT pl.project_id, COUNT(*) AS n FROM project_ledger pl "
                "JOIN ledger l ON l.id=pl.ledger_id "
                "WHERE l.status NOT IN ('completed','closed') "
                "GROUP BY pl.project_id"):
            counts[r["project_id"]] = r["n"]
    except Exception:
        pass
    people_n = {r["project_id"]: r["n"] for r in conn.execute(
        "SELECT project_id, COUNT(*) AS n FROM project_people GROUP BY project_id")}
    channels = project_channels()
    for p in projects:
        p["open_items"] = counts.get(p["id"], 0)
        p["people"] = people_n.get(p["id"], 0)
        # The #ea- badge: the same marker App Home and the Slack channel
        # topic carry, so a project reads as one thing across all three.
        if p["id"] in channels:
            p["channel"] = channels[p["id"]]
    domains = {d: [] for d in project_store.DOMAINS}
    for p in projects:
        domains.setdefault(p["domain"], []).append(p)
    return {"domains": domains, "generated_at": _now()}


def email_weblink(ledger_id: str) -> str:
    """Click-time resolver link for a ledger item's source email.

    /open?ledger=<id> fetches the LIVE webLink from Graph at click time,
    re-resolving by conversation when the stored id has gone stale, and
    302s to it; a static OWA deep link goes stale the moment the message
    is archived. Board links are same-origin relative; App Home gets the
    absolute tailnet form via absolute_email_link()."""
    return f"/open?ledger={ledger_id}" if ledger_id else ""


def absolute_email_link(rel: str) -> str:
    """The tailnet-absolute form for Slack. Carries the shared secret when
    one is configured, so App Home links keep resolving under the gate."""
    if not rel:
        return ""
    if DASH_TOKEN:
        rel += ("&" if "?" in rel else "?") + "t=" + quote(DASH_TOKEN, safe="")
    return f"http://{bind_address()}:{PORT}{rel}"


def resolve_email_weblink(ledger_id: str) -> tuple:
    """(url, error) — the live Outlook webLink for a ledger item.
    Runs owner-side (the dashboard's user), so graph_client is importable;
    mediation only constrains ea-bot."""
    conn = project_store._get_db()
    row = conn.execute(
        "SELECT source_message_id, conversation_id, description "
        "FROM ledger WHERE id=?", (ledger_id,)).fetchone()
    if not row:
        return "", f"no ledger item {ledger_id}"
    if not row["source_message_id"]:
        return "", f"item has no source email: {row['description'][:80]}"
    # The ledger is resident state: its ids go into a Graph path only when plain.
    import mail_mediation as _mm
    if not _mm.is_plain_id(row["source_message_id"]) or (
            row["conversation_id"] and not _mm.is_plain_id(row["conversation_id"])):
        return "", "the item's mail id is not a plain mail id"
    try:
        import graph_client as graph
    except Exception as e:
        return "", f"graph unavailable: {e}"
    try:
        msg = graph.graph_get(f"/me/messages/{row['source_message_id']}",
                              {"$select": "webLink"})
        if msg.get("webLink"):
            return msg["webLink"], ""
    except Exception:
        pass  # stale id — fall through to conversation re-resolution
    conv = row["conversation_id"]
    if conv:
        try:
            hits = graph.graph_get("/me/messages", {
                "$filter": f"conversationId eq '{str(conv).replace(chr(39), chr(39) * 2)}'",
                "$select": "webLink,receivedDateTime",
                "$orderby": "receivedDateTime desc", "$top": 1})
            vals = hits.get("value", [])
            if vals and vals[0].get("webLink"):
                return vals[0]["webLink"], ""
        except Exception as e:
            return "", f"re-resolution failed: {e}"
    return "", ("message not found (archived and moved?); search Outlook "
                f"for: {row['description'][:80]}")


def unfiled_items(limit: int = 40) -> list:
    conn = project_store._get_db()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT l.id, l.type, l.description, l.from_name, l.from_email, "
            "l.deadline, l.source_message_id FROM ledger l "
            "WHERE l.status NOT IN ('completed','closed') "
            "AND l.id NOT IN (SELECT ledger_id FROM project_ledger) "
            "AND l.id NOT IN (SELECT ledger_id FROM loose_ledger) "
            "AND l.id NOT IN (SELECT merged_id FROM item_merges) "
            "ORDER BY l.updated_at DESC LIMIT ?", (limit,))]
    except Exception as e:
        print(f"unfiled query failed: {e}", file=sys.stderr)
        rows = []
    merges = project_store.merge_info_for(conn)
    for r in rows:
        has_email = bool(r.pop("source_message_id", ""))
        r["email_link"] = email_weblink(r["id"]) if has_email else ""
        if r["id"] in merges:
            r["merge"] = merges[r["id"]]
    return rows


def loose_items(limit: int = 60) -> list:
    conn = project_store._get_db()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT l.id, l.description, l.from_name FROM ledger l "
            "JOIN loose_ledger x ON x.ledger_id=l.id "
            "WHERE l.status NOT IN ('completed','closed') "
            "ORDER BY l.updated_at DESC LIMIT ?", (limit,))]
    except Exception:
        return []


def project_detail(pid: str) -> dict | None:
    pack = project_store.build_pack(pid, max_messages=10)
    if not pack:
        return None
    conn = project_store._get_db()
    row = dict(conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone())
    pack["fields"] = {k: row.get(k) for k in
                      ("name", "domain", "kind", "status", "parent_id",
                       "summary", "next_actions", "aliases")}
    return pack


VALID_STATUS = ("active", "dormant", "closed")

# Ledger types where the user owes the action, as opposed to ones waiting
# on someone else or on a date. The ball being on the user is the desk's
# primary signal; activity by anyone else is not a reason to rank a
# project up.
_ON_YOU = {"response_needed", "deadline", "task", "scheduling"}


def apply_op(op: dict) -> dict:
    """Apply one dashboard mutation. Returns {ok} or {ok:False, error}."""
    conn = project_store._get_db()
    kind = op.get("op")
    pid = op.get("project", "")

    def touch_and_note(text):
        conn.execute("UPDATE projects SET updated_at=? WHERE id=?",
                     (_now(), pid))
        conn.execute("INSERT INTO project_events (project_id,ts,kind,text) "
                     "VALUES (?,?,?,?)", (pid, _now(), "note", text))

    # Project-less ops: item dispositions work on ledger items alone.
    if kind == "disposition":
        # done / not_mine / snooze / still_open, each with a note. The
        # feedback reaches the missed-closure log and the preferences
        # store, not just the ledger.
        import item_feedback
        return item_feedback.apply_op(op)
    if kind == "close_ledger":
        lid = op.get("ledger_id", "")
        if not lid:
            return {"ok": False, "error": "ledger_id required"}
        # Through the ledger CLI, never raw SQL: close carries semantics
        # (auto-archive of the source email, closed_at, audit note).
        r = subprocess.run(
            [sys.executable, str(Path(__file__).parent / "ledger.py"),
             "close", "--id", lid, "--user-said", "closed from the dashboard",
             "--note", "closed from dashboard quick action (user triage)"],
            capture_output=True, text=True, timeout=60)
        try:
            out = json.loads(r.stdout.strip().splitlines()[-1])
        except Exception:
            out = {}
        if r.returncode != 0 or not out.get("ok"):
            return {"ok": False,
                    "error": (out.get("error") or r.stderr or "close failed")[:200]}
        return {"ok": True}
    if kind == "mark_loose":
        lid = op.get("ledger_id", "")
        if not lid:
            return {"ok": False, "error": "ledger_id required"}
        conn.execute("INSERT OR REPLACE INTO loose_ledger (ledger_id,"
                     "marked_at) VALUES (?,?)", (lid, _now()))
        conn.commit()
        return {"ok": True}
    if kind == "unmark_loose":
        conn.execute("DELETE FROM loose_ledger WHERE ledger_id=?",
                     (op.get("ledger_id", ""),))
        conn.commit()
        return {"ok": True}

    if kind not in ("set_domain", "set_parent", "set_status", "set_summary",
                    "set_next_actions", "set_instructions", "link_ledger",
                    "journal_add", "link_person", "add_link"):
        return {"ok": False, "error": f"unknown op {kind!r}"}
    if not conn.execute("SELECT 1 FROM projects WHERE id=?", (pid,)).fetchone():
        return {"ok": False, "error": f"no project {pid}"}

    if kind == "set_domain":
        dom = op.get("value", "")
        if dom not in project_store.DOMAINS:
            return {"ok": False, "error": "bad domain"}
        conn.execute("UPDATE projects SET domain=? WHERE id=?", (dom, pid))
        touch_and_note(f"domain -> {dom} (via dashboard)")
    elif kind == "set_parent":
        parent = op.get("value") or None
        if parent:
            if parent == pid:
                return {"ok": False, "error": "cannot parent to self"}
            if not conn.execute("SELECT 1 FROM projects WHERE id=?",
                                (parent,)).fetchone():
                return {"ok": False, "error": f"no parent {parent}"}
            # No cycles: walk up from the proposed parent.
            seen, cur = set(), parent
            while cur:
                if cur == pid:
                    return {"ok": False, "error": "would create a cycle"}
                if cur in seen:
                    break
                seen.add(cur)
                row = conn.execute("SELECT parent_id FROM projects WHERE id=?",
                                   (cur,)).fetchone()
                cur = row["parent_id"] if row else None
        conn.execute("UPDATE projects SET parent_id=? WHERE id=?",
                     (parent, pid))
        touch_and_note(f"parent -> {parent or '(none)'} (via dashboard)")
    elif kind == "set_status":
        val = op.get("value", "")
        if val not in VALID_STATUS:
            return {"ok": False, "error": "bad status"}
        conn.execute("UPDATE projects SET status=? WHERE id=?", (val, pid))
        touch_and_note(f"status -> {val} (via dashboard)")
    elif kind in ("set_summary", "set_next_actions", "set_instructions"):
        col = {"set_summary": "summary", "set_next_actions": "next_actions",
               "set_instructions": "instructions"}[kind]
        conn.execute(f"UPDATE projects SET {col}=?, updated_at=? WHERE id=?",
                     (op.get("value", ""), _now(), pid))
    elif kind == "add_link":
        url = (op.get("url") or "").strip()
        if not re.match(r"^https?://", url):
            return {"ok": False, "error": "url must be http(s)"}
        conn.execute("INSERT OR REPLACE INTO project_links (project_id,url,"
                     "title,added_at) VALUES (?,?,?,?)",
                     (pid, url, op.get("title", ""), _now()))
        touch_and_note(f"link added: {url[:80]} (via dashboard)")
    elif kind == "link_ledger":
        lid = op.get("ledger_id", "")
        if not lid:
            return {"ok": False, "error": "ledger_id required"}
        conn.execute("INSERT OR IGNORE INTO project_ledger (project_id,"
                     "ledger_id) VALUES (?,?)", (pid, lid))
        touch_and_note(f"filed ledger item {lid} (via dashboard)")
    elif kind == "journal_add":
        text = (op.get("text") or "").strip()
        if not text:
            return {"ok": False, "error": "text required"}
        conn.execute("INSERT INTO project_events (project_id,ts,kind,text) "
                     "VALUES (?,?,?,?)",
                     (pid, _now(), op.get("kind", "note"), text))
        conn.execute("UPDATE projects SET updated_at=? WHERE id=?",
                     (_now(), pid))
    elif kind == "link_person":
        email = (op.get("email") or "").lower()
        if not conn.execute("SELECT 1 FROM people WHERE email=?",
                            (email,)).fetchone():
            return {"ok": False, "error": f"no person {email}"}
        conn.execute("INSERT INTO project_people (project_id,email,"
                     "role_in_project,note) VALUES (?,?,?,?) "
                     "ON CONFLICT(project_id,email) DO UPDATE SET "
                     "role_in_project=excluded.role_in_project",
                     (pid, email, op.get("role", ""), ""))
        touch_and_note(f"linked {email} (via dashboard)")
    conn.commit()
    return {"ok": True}


def build_desk(limit: int = 8) -> list:
    """The phone landing view: 'here's where you left off on the most
    urgent projects.' Ranked by deadline pressure of linked open items,
    momentum (recent journal/update activity), and open volume. Each
    entry carries the next-action question, the left-off trail, and
    links, so resuming work is one tap."""
    conn = project_store._get_db()
    now = datetime.now(timezone.utc)
    channels = project_channels()
    today = now.strftime("%Y-%m-%d")

    def _scheduled(it):
        # A future follow_up_after parks the item BY DESIGN (a pipeline
        # phase, a wait-until date). It is not the user's move today and
        # must not inflate on_you.
        fa = it.get("follow_up_after") or ""
        return bool(fa) and fa[:10] > today

    entries = []
    for p in conn.execute("SELECT * FROM projects WHERE status='active'"):
        pid = p["id"]
        score, why = 0, []
        deadline = None
        try:
            items = [dict(r) for r in conn.execute(
                "SELECT l.id, l.description, l.deadline, l.last_action, "
                "l.updated_at, l.source_message_id, l.type, "
                "l.follow_up_after "
                "FROM project_ledger pl JOIN ledger l "
                "ON l.id=pl.ledger_id WHERE pl.project_id=? "
                "AND l.status NOT IN ('completed','closed') "
                "AND l.id NOT IN (SELECT merged_id FROM item_merges) "
                "ORDER BY l.updated_at DESC", (pid,))]
        except Exception:
            items = []
        for it in items:
            if it.get("deadline"):
                try:
                    dl = datetime.fromisoformat(it["deadline"][:10]).replace(
                        tzinfo=timezone.utc)
                except ValueError:
                    continue
                days = (dl - now).days
                pts = 100 if days < 0 else 60 if days <= 2 else \
                    30 if days <= 7 else 5
                if pts > score or deadline is None:
                    deadline = it["deadline"][:10]
                score = max(score, pts)
        deadline_days = None
        if deadline:
            try:
                deadline_days = (datetime.fromisoformat(deadline).replace(
                    tzinfo=timezone.utc) - now).days
            except ValueError:
                pass
            why.append(f"deadline {deadline}")
        quiet_days = None
        try:
            upd = datetime.fromisoformat(
                p["updated_at"].replace("Z", "+00:00"))
            quiet_days = int((now - upd).total_seconds() // 86400)
        except ValueError:
            pass
        scheduled = sum(1 for it in items if _scheduled(it))
        on_you = sum(1 for it in items
                     if not _scheduled(it)
                     and canonical_type(it.get("type") or "task") in _ON_YOU)
        waiting = len(items) - on_you - scheduled
        score += min(on_you * 12, 36)
        score += min(waiting * 2, 6)
        if quiet_days is not None and quiet_days <= 2 and on_you:
            score += 8; why.append("in flight")
        if on_you and quiet_days is not None and quiet_days >= 7:
            why.append(f"stalled {quiet_days}d with your move pending")
        if p["next_actions"]:
            score += 5
        if score < 15:
            continue
        ev = conn.execute(
            "SELECT text FROM project_events WHERE project_id=? "
            "ORDER BY ts DESC, id DESC LIMIT 1", (pid,)).fetchone()
        left_off = (ev["text"] if ev else
                    (items[0]["last_action"] or items[0]["description"])
                    if items else "")
        links = [dict(r) for r in conn.execute(
            "SELECT url, title FROM project_links WHERE project_id=?", (pid,))]
        entries.append({
            "id": pid, "name": p["name"], "score": score,
            "channel": channels.get(pid),
            "why": ", ".join(why),
            "question": p["next_actions"] or "",
            "left_off": (left_off or "")[:160],
            "deadline": deadline, "deadline_days": deadline_days,
            "quiet_days": quiet_days, "on_you": on_you, "waiting": waiting,
            "scheduled": scheduled,
            "links": links,
            "open_items": len(items),
            "items": [{"description": (it["description"] or "")[:110],
                       "deadline": it.get("deadline"),
                       "email_link": (email_weblink(it["id"])
                                      if it.get("source_message_id") else "")}
                      for it in items[:3]],
        })
    entries.sort(key=lambda e: -e["score"])
    return entries[:limit]


def people_list() -> list:
    conn = project_store._get_db()
    people = [dict(r) for r in conn.execute(
        "SELECT email,name,org,role,notes FROM people ORDER BY name")]
    links = {}
    for r in conn.execute(
            "SELECT email, project_id, role_in_project FROM project_people"):
        links.setdefault(r["email"], []).append(
            {"project": r["project_id"], "role": r["role_in_project"]})
    for p in people:
        p["projects"] = links.get(p["email"], [])
    return people


# ── HTTP plumbing ───────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str,
              extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # No sniffing, no framing, and no Referer leaking ?t= to Outlook.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

    # ── Access control ────────────────────────────────────────────────
    def _authorized(self, query: dict) -> bool:
        """True when no shared secret is configured, or the request carries
        it (Authorization: Bearer, X-EA-Token, or ?t=)."""
        if not DASH_TOKEN:
            return True
        supplied = ""
        auth = self.headers.get("Authorization", "") or ""
        if auth.lower().startswith("bearer "):
            supplied = auth[7:].strip()
        if not supplied:
            supplied = (self.headers.get("X-EA-Token", "") or "").strip()
        if not supplied:
            supplied = (query.get("t") or [""])[0]
        return hmac.compare_digest(supplied.encode("utf-8", "replace"),
                                   DASH_TOKEN.encode("utf-8", "replace"))

    def _same_origin(self) -> bool:
        """Mutations must come from the board page itself.

        X-EA-Dashboard is the load-bearing half: a cross-site form POST
        cannot set a custom header, and a cross-origin fetch that sets one
        must first pass a preflight this server never answers. Origin and
        Sec-Fetch-Site are checked as well whenever the browser sends them.
        """
        if (self.headers.get("X-EA-Dashboard", "") or "").strip() != "1":
            return False
        site = (self.headers.get("Sec-Fetch-Site", "") or "").strip().lower()
        if site and site not in ("same-origin", "none"):
            return False
        origin = (self.headers.get("Origin", "") or "").strip()
        if origin:
            host = (self.headers.get("Host", "") or "").strip()
            if origin not in (f"http://{host}", f"https://{host}"):
                return False
        return True

    def _serve_html(self) -> None:
        """One fresh script nonce per response, in both the CSP header and
        the page's own meta tag."""
        nonce = secrets.token_urlsafe(18)
        try:
            html = HTML_PATH.read_text(encoding="utf-8")
        except OSError:
            return self._send(500, b"dashboard.html missing", "text/plain")
        html = html.replace("__CSP_NONCE__", nonce)
        self._send(200, html.encode("utf-8"), "text/html; charset=utf-8",
                   extra={"Content-Security-Policy":
                          CSP_TEMPLATE.format(nonce=nonce),
                          "X-Frame-Options": "DENY"})

    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        if not self._authorized(parse_qs(u.query)):
            return self._send(401, b"unauthorized", "text/plain")
        if u.path in ("/", "/index.html"):
            self._serve_html()
        elif u.path == "/api/board":
            self._json({"board": build_board(), "unfiled": unfiled_items(),
                        "loose": loose_items()})
        elif u.path == "/api/desk":
            self._json({"desk": build_desk()})
        elif u.path == "/open":
            lid = parse_qs(u.query).get("ledger", [""])[0]
            url, err = resolve_email_weblink(lid)
            if url:
                self.send_response(302)
                self.send_header("Location", url)
                # Keep any ?t= out of Outlook's Referer.
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
            else:
                self._send(404, ("Could not open the email: "
                                 + err).encode(), "text/plain; charset=utf-8")
        elif u.path == "/api/project":
            pid = parse_qs(u.query).get("id", [""])[0]
            detail = project_detail(pid)
            self._json(detail if detail else {"error": "not found"},
                       200 if detail else 404)
        elif u.path == "/api/people":
            self._json(people_list())
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):  # noqa: N802
        u = urlparse(self.path)
        path = u.path
        if not self._authorized(parse_qs(u.query)):
            return self._json({"ok": False, "error": "unauthorized"}, 401)
        if not self._same_origin():
            # The ledger-mutating half of the API: never reachable from
            # another page, and never from a bare form post.
            return self._json(
                {"ok": False,
                 "error": "cross-origin request refused (send the "
                          "X-EA-Dashboard: 1 header from the board page)"},
                403)
        try:
            n = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._json({"ok": False, "error": "bad json"}, 400)
        if path == "/api/op":
            try:
                return self._json(apply_op(body))
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 500)
        self._send(404, b"not found", "text/plain")

    def log_message(self, fmt, *args):  # quiet: journal is the audit trail
        pass


def is_loopback(addr: str) -> bool:
    """True for a loopback address (127/8, ::1) or the name localhost."""
    import ipaddress
    a = addr.strip().strip("[]")
    if a.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(a).is_loopback
    except ValueError:
        return False


def check_bind(addr: str, token: str) -> str | None:
    """Why the server may not start on `addr`, or None when it may."""
    if is_loopback(addr) or token:
        return None
    return (f"refusing to bind {addr}: a non-loopback address needs EA_DASH_TOKEN "
            "(scripts/set-secret.sh EA_DASH_TOKEN, or EA_DASH_BIND=127.0.0.1)")


def effective_bind(addr: str, token: str) -> tuple[str, str | None]:
    """(the address to bind, why it is not `addr`). Without a token a
    non-loopback address becomes loopback."""
    why = check_bind(addr, token)
    return (LOOPBACK, why) if why else (addr, None)


def _fallback_path() -> Path | None:
    try:
        import owner_trust
        return owner_trust.private_dir() / FALLBACK_FILE
    except Exception:
        return None


def record_fallback(wanted: str, why: str | None, path: Path | None = None) -> None:
    """Leave FALLBACK_FILE (mode 0600) while on the fallback; remove it
    otherwise. Best effort: a failure is printed, never raised."""
    path = path or _fallback_path()
    if path is None:
        return
    try:
        if why is None:
            path.unlink(missing_ok=True)
            return
        import owner_trust
        doc = {"wanted": wanted, "bound": LOOPBACK, "port": PORT, "why": why, "since": _now()}
        owner_trust.write_owner_file(path, json.dumps(doc, sort_keys=True), mode=0o600)
    except Exception as e:
        print(f"project dashboard: could not update {path}: {e}", file=sys.stderr, flush=True)


def main() -> None:
    wanted = bind_address()
    addr, why = effective_bind(wanted, DASH_TOKEN)
    if why:
        print(f"project dashboard: WARNING: {why}. Serving on {LOOPBACK} only; the board "
              f"is not reachable at {wanted} until EA_DASH_TOKEN is set and the service "
              "restarts.", file=sys.stderr, flush=True)
    record_fallback(wanted, why)
    srv = ThreadingHTTPServer((addr, PORT), Handler)
    print(f"project dashboard: http://{addr}:{PORT}/", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()

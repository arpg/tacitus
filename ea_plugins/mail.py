"""Mediated Outlook reads for plugins.

A plugin's collect.py runs as ea-bot, and under mail mediation the Graph
token cache belongs to the owner's account: `import graph_client` raises
PermissionError there, and the run dies before it reads a single message.
This module is the supported route. It shells out to
preprocessor/graph_actions.py, whose SAFE read verbs the owner-side mediator
executes and answers, and hands the results back in Graph's own message
shape (from.emailAddress.address, body.content, receivedDateTime), so a
parser written against graph_client.get_messages keeps working.

    from ea_plugins import mail
    msgs = mail.messages_from("alerts@example.com", lookback_days=8)
    for m in msgs:
        html = m["body"]["content"]

A plugin whose manifest declares `mail_triggers` also gets its matching
mail copied to it as the mail arrives (preprocessor/plugin_mail.py);
`delivered()` reads those copies, in the same shape:

    for m in mail.delivered(lookback_days=30):
        ...

Reads only. Nothing here drafts, sends, moves or deletes.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

_SCRIPT_DIR = Path(__file__).resolve().parent.parent
_GRAPH_CLI = _SCRIPT_DIR / "preprocessor" / "graph_actions.py"

# Graph caps a $search page at 250 rows and the CLI does not paginate, so a
# window holding more mail from one sender than this comes back truncated,
# and $search order is relevance, not date. messages_from warns on stderr
# when a search fills the cap.
SEARCH_CAP = 250
_TIMEOUT_S = 240  # the ea-bot side of the mediator itself gives up at 150 s

_ADDRESS_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


class MailError(RuntimeError):
    """The mediated CLI failed or answered with an error object."""


def _run_cli(argv: list[str]) -> str:
    """Run one graph_actions.py verb and return its stdout."""
    cmd = [sys.executable, str(_GRAPH_CLI), *argv]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          timeout=_TIMEOUT_S)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()[:400]
        raise MailError(f"graph_actions.py {argv[0]} failed "
                        f"(rc={proc.returncode}): {detail}")
    return proc.stdout


def _extract_json(text: str):
    """The verb's JSON document, tolerating a hint line printed around it."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    starts = [i for i in (text.find("["), text.find("{")) if i >= 0]
    if not starts:
        raise MailError(f"graph_actions.py returned no JSON: {text[:200]!r}")
    start = min(starts)
    end = max(text.rfind("]"), text.rfind("}"))
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        raise MailError("graph_actions.py returned unparseable JSON: "
                        f"{text[:200]!r}") from e


def kql_since(lookback_days: int, now: Optional[datetime] = None) -> str:
    """The KQL bound for `received>=`: the UTC calendar day lookback_days
    ago."""
    now = now or datetime.now(timezone.utc)
    return (now - timedelta(days=int(lookback_days))).strftime("%Y-%m-%d")


def search(query: str, *, top: int = SEARCH_CAP,
           folder: Optional[str] = None) -> list[dict]:
    """Rows as `graph_actions.py search-mail` prints them: id, subject,
    from, received, conversation_id, to, cc, preview, has_attachments.
    Without a folder the search is mailbox-wide."""
    argv = ["search-mail", "--query", query, "--top", str(int(top))]
    if folder:
        argv += ["--folder", folder]
    data = _extract_json(_run_cli(argv)) or []
    if isinstance(data, dict):
        if data.get("error"):
            raise MailError(str(data["error"]))
        data = [data]
    # A folder-scoped miss carries one `_note` row instead of messages.
    return [r for r in data if isinstance(r, dict) and r.get("id")]


def get_message(message_id: str, *, raw_html: bool = True) -> dict:
    """One message as `graph_actions.py get-message` prints it. With
    raw_html the body is the HTML Graph stored; otherwise extracted text."""
    argv = ["get-message", "--message-id", message_id]
    if raw_html:
        argv.append("--raw-html")
    data = _extract_json(_run_cli(argv))
    if not isinstance(data, dict):
        raise MailError(f"get-message {message_id[:16]}: no message object")
    if data.get("error"):
        raise MailError(str(data["error"]))
    return data


def _graph_shape(row: dict, full: Optional[dict] = None) -> dict:
    """A search-mail row, plus its get-message document when fetched, in
    the shape graph_client.get_messages returns."""
    full = full or {}
    sender = full.get("from") if isinstance(full.get("from"), dict) else {}
    addr = sender.get("address") or row.get("from") or ""
    msg = {
        "id": row.get("id") or full.get("id") or "",
        "subject": row.get("subject") or full.get("subject") or "",
        "from": {"emailAddress": {"address": addr,
                                  "name": sender.get("name", "")}},
        "receivedDateTime": row.get("received") or full.get("received") or "",
        "conversationId": (row.get("conversation_id")
                           or full.get("conversation_id") or ""),
        "hasAttachments": bool(row.get(
            "has_attachments", full.get("has_attachments", False))),
        "bodyPreview": row.get("preview") or "",
        "toRecipients": [{"emailAddress": {"address": a}}
                         for a in (row.get("to") or [])],
        "ccRecipients": [{"emailAddress": {"address": a}}
                         for a in (row.get("cc") or [])],
    }
    if full:
        ctype = (full.get("body_type") or "html").lower()
        msg["body"] = {"contentType": "html" if "html" in ctype else "text",
                       "content": full.get("body") or ""}
    return msg


def deleted_rows(since: str, *, top: int = SEARCH_CAP) -> list[dict]:
    """Deleted Items received on or after `since` (YYYY-MM-DD), newest
    first, as `graph_actions.py list-mail` prints them. A mailbox $search
    does not cover that folder."""
    argv = ["list-mail", "--folder", "deleteditems", "--since", since, "--top", str(int(top))]
    data = _extract_json(_run_cli(argv)) or []
    if isinstance(data, dict):
        if data.get("error"):
            raise MailError(str(data["error"]))
        data = data.get("messages") or []
    rows = [r for r in data if isinstance(r, dict) and r.get("id")]
    if len(rows) >= top:
        print(f"[ea_plugins.mail] Deleted Items listing cap {top} reached since {since}; "
              "older deleted mail in the window may be missing", file=sys.stderr)
    return rows


def messages_from(sender: str, lookback_days: int, *,
                  full_body: bool = True,
                  prefilter: Optional[Callable[[dict], bool]] = None,
                  top: int = SEARCH_CAP, include_deleted: bool = False) -> list[dict]:
    """Every message from `sender` received in the last `lookback_days`,
    newest first, in Graph's message shape.

    One mailbox-wide search (inbox and archive folders alike), then, with
    full_body, one get-message per row that passes `prefilter`, a callable
    on the search row (subject, preview, received, ...). A prefilter keeps
    the body fetches to the mail the caller's parser can use. The search
    does not reach Deleted Items; include_deleted adds that folder's
    newest `top` messages from the sender. Mail the user deletes before
    the plugin runs is better caught with a mail trigger (delivered()).
    """
    sender = (sender or "").strip()
    if not _ADDRESS_RE.match(sender):
        raise ValueError(f"sender must be one plain address, got {sender!r}")
    since = kql_since(lookback_days)
    rows = search(f"from:{sender} received>={since}", top=top)
    if len(rows) >= top:
        print(f"[ea_plugins.mail] search cap {top} reached for {sender} "
              f"since {since}; older mail in the window may be missing",
              file=sys.stderr)
    if include_deleted:
        have = {r["id"] for r in rows}
        rows += [r for r in deleted_rows(since, top=top) if r["id"] not in have]
    sender_lc = sender.lower()
    out: list[dict] = []
    for r in rows:
        if (r.get("from") or "").lower() != sender_lc:
            continue
        if (r.get("received") or "")[:10] < since:
            continue
        if prefilter is not None and not prefilter(r):
            continue
        full = get_message(r["id"], raw_html=True) if full_body else None
        out.append(_graph_shape(r, full))
    out.sort(key=lambda m: m.get("receivedDateTime") or "", reverse=True)
    return out


def _inbox_file(plugin: str) -> Path:
    ea_dir = Path(os.environ.get("EA_DIR") or (Path.home() / ".ea"))
    return ea_dir / "state" / "plugin-mail" / f"{plugin}.json"


def _read_delivered(plugin: str) -> list:
    """The copies on disk: the resident's inbox file, or for an owner-zone
    plugin running as the owner, the owner's queue."""
    path = _inbox_file(plugin)
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        raise MailError(f"{path}: unreadable: {e}") from e
    try:
        sys.path.insert(0, str(_SCRIPT_DIR / "preprocessor"))
        import owner_trust
        import plugin_mail
        if owner_trust.is_owner_process():
            return plugin_mail._read_owner_json(plugin_mail.queue_path(plugin), [])
    except Exception:
        pass
    return []


def delivered(lookback_days: Optional[int] = None, *, plugin: Optional[str] = None,
              trigger: Optional[str] = None, fetch_sealed: bool = True) -> list[dict]:
    """The mail the owner's mail triggers copied to this plugin, newest
    first, in Graph's message shape plus `trigger` (the trigger id). The
    copy holds the body as it was on arrival, so mail deleted or moved
    since is still here. A copy marked sealed carries no body; with
    fetch_sealed, the body is read through get-message, which gives the
    remote model's record in the sealed mode. `plugin` defaults to
    EA_PLUGIN_NAME."""
    plugin = plugin or os.environ.get("EA_PLUGIN_NAME") or ""
    if not plugin:
        raise ValueError("delivered() needs a plugin name (EA_PLUGIN_NAME is not set)")
    since = kql_since(lookback_days) if lookback_days is not None else ""
    out = []
    for rec in _read_delivered(plugin):
        if not isinstance(rec, dict) or not rec.get("id"):
            continue
        if trigger and rec.get("trigger") != trigger:
            continue
        if since and (rec.get("received") or "")[:10] < since:
            continue
        row = {"id": rec["id"], "subject": rec.get("subject") or "",
               "from": rec.get("from") or "", "received": rec.get("received") or "",
               "conversation_id": rec.get("conversation_id") or "",
               "to": rec.get("to") or []}
        full = None
        if "body" in rec:
            full = {"from": {"address": rec.get("from") or "",
                             "name": rec.get("from_name") or ""},
                    "body_type": rec.get("body_type") or "html", "body": rec["body"]}
        elif fetch_sealed:
            try:
                full = get_message(rec["id"], raw_html=True)
            except MailError as e:
                print(f"[ea_plugins.mail] {rec['id'][-12:]}: {e}", file=sys.stderr)
        msg = _graph_shape(row, full)
        msg["trigger"] = rec.get("trigger") or ""
        msg["internetMessageId"] = rec.get("internet_message_id") or ""
        out.append(msg)
    out.sort(key=lambda m: m.get("receivedDateTime") or "", reverse=True)
    return out

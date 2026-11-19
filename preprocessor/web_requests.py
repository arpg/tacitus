"""What the EA knows about each request in the web chat's Action requests
(docs/webchat.md, Action requests): the web front end's request_context.

For each card the front end names, read-only:
- the ledger rows its buttons name (`ledger_id`, `ledger_ids`, `row`,
  `rows` in an action's value) and the open rows of the mail
  conversation it answers (`conversation_id`): the earliest deadline of
  the open ones is the request's `due`;
- the project those rows belong to (project_store's `project_ledger`),
  else the project the mail conversation is filed under
  (`project_messages`): its name is the request's `project`;
- for an event card, the start of the staged event its button carries
  (`payload_of`, the bot's own read of the staged payload, which leaves it
  in place): the request's `starts`.

A store that cannot be read gives nothing; the blocks then show no
project and no deadline.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

try:
    import ledger as _ledger
    import web_thread_state as _wts
except ModuleNotFoundError:  # imported as preprocessor.<mod>
    from preprocessor import ledger as _ledger
    from preprocessor import web_thread_state as _wts

log = logging.getLogger("chat.web.requests")

OPEN = frozenset({"open", "follow_up"})
EVENT_KINDS = frozenset({"event", "event_update"})
CHUNK = 400


def _values(card: Any) -> list[dict]:
    out = []
    for a in (card or {}).get("actions") or [] if isinstance(card, dict) else []:
        v = a.get("value") if isinstance(a, dict) else None
        if isinstance(v, str) and v.startswith("{"):
            try:
                v = json.loads(v)
            except ValueError:
                v = None
        if isinstance(v, dict):
            out.append(v)
    return out


def _keys(card: Any) -> tuple[list[str], list[str]]:
    """(ledger ids, mail conversation ids) the card's buttons name."""
    ids: list[str] = []
    convs: list[str] = []
    for v in _values(card):
        for key in ("ledger_id", "row"):
            if isinstance(v.get(key), str) and v[key]:
                ids.append(v[key])
        for key in ("ledger_ids", "rows"):
            if isinstance(v.get(key), list):
                ids += [str(i) for i in v[key] if i]
        if isinstance(v.get("conversation_id"), str) and v["conversation_id"]:
            convs.append(v["conversation_id"])
    return list(dict.fromkeys(ids)), list(dict.fromkeys(convs))


def _projects(ids: list[str], convs: list[str], db_path: Path | None) -> tuple[dict, dict]:
    """({ledger id: project name}, {conversation id: project name})."""
    by_row: dict[str, str] = {}
    by_conv: dict[str, str] = {}
    if not ids and not convs:
        return by_row, by_conv
    conn = _ledger.guarded_connect(db_path or _ledger.DB_PATH, read_only=True)
    try:
        conn.row_factory = sqlite3.Row
        for values, sql, out, col in (
                (ids, "SELECT pl.ledger_id AS k, p.name AS name FROM project_ledger pl JOIN projects p "
                      "ON p.id = pl.project_id WHERE pl.ledger_id IN ({q})", by_row, "k"),
                (convs, "SELECT pm.conversation_id AS k, p.name AS name FROM project_messages pm JOIN "
                        "projects p ON p.id = pm.project_id WHERE pm.conversation_id IN ({q})",
                 by_conv, "k")):
            for i in range(0, len(values), CHUNK):
                part = values[i:i + CHUNK]
                try:
                    rows = conn.execute(sql.format(q=",".join("?" * len(part))), part).fetchall()
                except sqlite3.Error:
                    rows = []  # a ledger with no project store yet
                for r in rows:
                    out.setdefault(r[col], r["name"])
    finally:
        conn.close()
    return by_row, by_conv


def context(items: list[dict], *, payload_of: Callable[[dict], Any] | None = None,
            db_path: Path | None = None) -> dict[str, dict]:
    """{item id: {"project", "due", "starts"}} for the items the front end
    names ({"id", "card"}); an item with nothing known is left out."""
    keyed = {str(it.get("id")): (it.get("card") or {}, *_keys(it.get("card"))) for it in items or []}
    all_ids = sorted({i for _c, ids, _v in keyed.values() for i in ids})
    all_convs = sorted({c for _c, _i, convs in keyed.values() for c in convs})
    rows: list[dict] = []
    by_row: dict = {}
    by_conv: dict = {}
    try:
        rows = _wts.ledger_rows(all_ids, all_convs, db_path)
        by_row, by_conv = _projects(all_ids, all_convs, db_path)
    except Exception as e:
        log.warning("requests: the ledger could not be read: %s", e)
    out: dict[str, dict] = {}
    for mid, (card, ids, convs) in keyed.items():
        mine = [r for r in rows if r["id"] in ids or (r["conversation_id"] and r["conversation_id"] in convs)]
        due = sorted(r["deadline"][:19] for r in mine if r["status"] in OPEN and r["deadline"])
        project = next((by_row[r["id"]] for r in mine if r["id"] in by_row), "") or \
            next((by_conv[c] for c in convs if c in by_conv), "")
        starts = ""
        if payload_of is not None and isinstance(card, dict) and card.get("kind") in EVENT_KINDS:
            for v in _values(card):
                try:
                    got = payload_of(v)
                except Exception as e:
                    log.debug("requests: no staged event for a card: %s", e)
                    got = None
                if isinstance(got, dict) and (got.get("new_start") or got.get("start")):
                    starts = str(got.get("new_start") or got.get("start"))
                    break
        info = {k: v for k, v in (("project", project), ("due", due[0] if due else ""),
                                  ("starts", starts)) if v}
        if info:
            out[mid] = info
    return out

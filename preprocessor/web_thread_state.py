"""The EA's side of the web chat's conversation states (docs/webchat-state.md).

botcore's thread_state.ThreadStates runs the rules and the tick over the
web chat's store. This module gives it the two things only the EA has:

  ledger_rows(ledger_ids, conversation_ids)
      the ledger rows a batch of threads links to, by id or by
      conversation id: read-only queries on one connection
      (ledger.guarded_connect), never a write. Each row is {"id", "type",
      "status", "conversation_id", "deadline", "last_action"} in the
      ledger's own words (the canonical type and status; a `waiting`
      status stays `waiting`). The model reads the deadline and the last
      action of a row the rules leave to it.

  model_answer(prompt)
      the model's text for one thread, routed by the `thread_state` task of
      the routing table (model_routing.py). `local` and `auto` call the
      local endpoint (remote_llm.chat, purpose `chat-thread-state`: the bot's
      small jobs' breaker, and a short wait for a request slot), with the
      answer's JSON schema. With no local model, or with `hosted`, it
      returns None and logs why once an hour: the hosted step is not built,
      so the rules alone set the state there.

enable(frontend) hands both to a web front end (ea-chat-bot.py calls it
for the web chat, alone or as a shadow).
"""
from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

try:
    import ledger as _ledger
    import model_routing as _routing
    import remote_llm as _remote
    from chat import thread_state as _ts
except ModuleNotFoundError:  # imported as preprocessor.<mod>
    from preprocessor import ledger as _ledger
    from preprocessor import model_routing as _routing
    from preprocessor import remote_llm as _remote
    from preprocessor.chat import thread_state as _ts

log = logging.getLogger("chat.web.state")

TASK = "thread_state"
PURPOSE = "chat-thread-state"
TIMEOUT_S = 45
MAX_TOKENS = 300
# Ids one query names at most (SQLite's bound on parameters is higher).
CHUNK = 400
# Seconds between two log lines that say why no model judges.
QUIET_S = 3600

_said: dict[str, float] = {}


def _say_once(key: str, msg: str) -> None:
    now = time.monotonic()
    if now - _said.get(key, -QUIET_S - 1) >= QUIET_S:
        _said[key] = now
        log.info(msg)


def ledger_rows(ledger_ids: list, conversation_ids: list,
                db_path: Path | None = None) -> list[dict]:
    """The ledger rows named by id or by conversation id, read-only, in
    one connection; [] when none is named. Raises when the ledger cannot
    be opened (the engine logs it once and goes on without rows)."""
    ids = sorted({str(i) for i in ledger_ids or [] if i})
    convs = sorted({str(c) for c in conversation_ids or [] if c})
    if not ids and not convs:
        return []
    conn = _ledger.guarded_connect(db_path or _ledger.DB_PATH, read_only=True)
    found: dict[str, sqlite3.Row] = {}
    try:
        conn.row_factory = sqlite3.Row
        for col, values in (("id", ids), ("conversation_id", convs)):
            for i in range(0, len(values), CHUNK):
                part = values[i:i + CHUNK]
                for r in conn.execute(f"SELECT id, type, status, conversation_id, deadline, "
                                      f"last_action FROM ledger "
                                      f"WHERE {col} IN ({','.join('?' * len(part))})", part):
                    found[r["id"]] = r
    finally:
        conn.close()
    out = []
    for r in found.values():
        word = _ledger._word(r["status"])
        # A `waiting` status (and its spellings) stays a status: the rules
        # read it as waiting on someone else.
        status = "waiting" if word in _ledger.STATUS_IS_TYPE else _ledger.STATUS_ALIASES.get(word, word)
        out.append({"id": r["id"], "type": _ledger.canonical_type(r["type"]), "status": status,
                    "conversation_id": r["conversation_id"] or "",
                    "deadline": r["deadline"] or "", "last_action": r["last_action"] or ""})
    return out


def route() -> str:
    """Where the model step runs now: `local`, or `off` with no model to
    run it on (no local model set up, or `hosted`, which is not built)."""
    cfg = _remote.config()
    value = _routing.value(TASK)
    ready = _routing.local_ready(cfg)
    if value == "hosted":
        _say_once("hosted", "web chat: thread_state is hosted, and the hosted model step is not "
                            "built; the rules alone set the state (set it to local to use the "
                            "local model)")
        return "off"
    if not ready:
        _say_once("no-local", "web chat: no local model is set up for thread_state; the rules "
                              "alone set the state")
        return "off"
    return "local"


def model_answer(prompt: str) -> str | None:
    """The model's text for one thread's prompt, or None when no model can
    answer now (the tick stops asking until the next one)."""
    try:
        if route() != "local":
            return None
        res = _remote.chat([{"role": "user", "content": prompt}], purpose=PURPOSE,
                           schema=_ts.ANSWER_SCHEMA, schema_name="thread_state",
                           max_tokens=MAX_TOKENS, timeout=TIMEOUT_S)
    except Exception as e:
        log.warning("web chat: the local model did not judge a thread: %s", e)
        return None
    return str(res.get("content") or "")


def enable(frontend) -> bool:
    """Turn the conversation states on for a web front end; False for a
    front end that has none."""
    on = getattr(frontend, "enable_thread_state", None)
    if not callable(on):
        return False
    on(ledger=ledger_rows, model=model_answer)
    return True

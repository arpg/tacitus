"""The EA's side of the web chat's conversation states (docs/webchat-state.md).

botcore's thread_state.ThreadStates runs the rules and the tick over the
web chat's store. This module gives it what only the EA has:

  ledger_rows(ledger_ids, conversation_ids)
      the ledger rows a batch of threads links to, by id or by
      conversation id: read-only queries on one connection
      (ledger.guarded_connect), never a write. Each row is {"id", "type",
      "status", "conversation_id", "deadline", "last_action"} in the
      ledger's own words (the canonical type and status; a `waiting`
      status stays `waiting`). The model reads the deadline and the last
      action of a row the rules leave to it.

  route()
      which model judges this tick, from the `thread_state` task of the
      routing table (model_routing.py): `local` (set to local, with a local
      model), `auto` (set to auto, with a local model: the local model, and
      Claude for the threads left when it does not answer) or `hosted` (set
      to hosted, or no local model set up: `local` with no endpoint reads
      hosted, as the table reads it everywhere).

  model_answer(prompt)
      the local model's text for one thread, on the local and auto routes:
      the local endpoint (remote_llm.chat, purpose `chat-thread-state`: the
      bot's small jobs' breaker, and a short wait for a request slot), with
      the answer's JSON schema. None on the hosted route, or when the
      endpoint does not answer.

  hosted_answer(prompt)
      Claude's text for a batch of threads (thread_state.batch_prompt: up
      to 20 threads, one JSON array back), on the hosted and auto routes.
      One tool-less run of the Claude CLI as the resident
      (mail_privacy._run_claude, the call the nightly ledger reconcile, the
      briefing's still-open check and the scheduling backstop make), on the
      `thread_state` model of ea-config.json's `models`, else opus. It runs
      in the bot's own process on the engine's worker thread, never as a
      chat turn. A failure returns None (the states stay as they were) and
      is logged once an hour.

  quiet_now()
      True while the owner's quiet hours hold (quiet_hours.should_defer for
      a normal post: the window in the active timezone, at home only when
      the config says so). The engine posts no "Still need this?" nudge
      then. A check that fails counts as quiet.

  observe(event, ctx, ...)
      the engine's observer (thread_state.ThreadStates.observer). Each
      model answer and each owner tap becomes a record in the closure
      label store (closure_labels.py, docs/ea/closure-signals.md). A tap
      also closes the loop on the ledger where it is safe:

      - "Mark done" on a conversation whose linked rows are open closes
        them on the owner's word (`ledger.py close --user-said "marked
        done in the web chat" --skip-archive`: no mail moves) and logs
        each close with log_missed_closure.py, since a closure the owner
        had to state is a detector defect.
      - "Needs me" on a conversation whose row carries a close proposal
        rejects it (`ledger.py reject`); on one whose row a signal closed
        in the last REOPEN_DAYS days (not the owner's own word, nor a
        duplicate fold) reopens it (`ledger.py update --status open`,
        which vetoes the closing signal). Each then gets item_feedback's
        `still_open`.

      Nothing is written for an automated post (a briefing names many
      rows), for more than WRITEBACK_CAP rows, or for several open rows
      on different mail threads. Every write goes through those CLIs, as
      the bot's other ledger writes do; nothing writes the database here.

enable(frontend) hands them to a web front end (ea-chat-bot.py calls it
for the web chat, alone or as a shadow).
"""
from __future__ import annotations

import json
import logging
import sqlite3
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

try:
    import closure as _closure
    import closure_labels as _labels
    import ledger as _ledger
    import model_routing as _routing
    import remote_llm as _remote
    from chat import thread_state as _ts
except ModuleNotFoundError:  # imported as preprocessor.<mod>
    from preprocessor import closure as _closure
    from preprocessor import closure_labels as _labels
    from preprocessor import ledger as _ledger
    from preprocessor import model_routing as _routing
    from preprocessor import remote_llm as _remote
    from preprocessor.chat import thread_state as _ts

log = logging.getLogger("chat.web.state")

TASK = "thread_state"
PURPOSE = "chat-thread-state"
TIMEOUT_S = 45
MAX_TOKENS = 300
# The hosted call: the model when ea-config.json's `models` names none for
# the task (the tier the reconcile and the backstop use), and how long one
# run may take.
HOSTED_MODEL = "opus"
HOSTED_TIMEOUT_S = 300
# Ids one query names at most (SQLite's bound on parameters is higher).
CHUNK = 400
# Seconds between two log lines on the same thing (a hosted run that
# fails, quiet hours that cannot be read).
QUIET_S = 3600

_said: dict[str, float] = {}


def _say_once(key: str, msg: str, level: int = logging.INFO) -> None:
    now = time.monotonic()
    if now - _said.get(key, -QUIET_S - 1) >= QUIET_S:
        _said[key] = now
        log.log(level, msg)


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
    """Which model judges now: `local`, `auto` (the local model, then
    Claude for what it leaves when it does not answer) or `hosted`. The
    engine reads it once a tick."""
    value = _routing.value(TASK)
    if value == "hosted" or not _routing.local_ready(_remote.config()):
        return "hosted"
    return "auto" if value == "auto" else "local"


def model_answer(prompt: str) -> str | None:
    """The local model's text for one thread's prompt, or None when it
    cannot answer now (the tick stops asking it until the next one)."""
    try:
        if route() not in ("local", "auto"):
            return None
        res = _remote.chat([{"role": "user", "content": prompt}], purpose=PURPOSE,
                           schema=_ts.ANSWER_SCHEMA, schema_name="thread_state",
                           max_tokens=MAX_TOKENS, timeout=TIMEOUT_S)
    except Exception as e:
        log.warning("web chat: the local model did not judge a thread: %s", e)
        return None
    return str(res.get("content") or "")


def _claude(prompt: str) -> str:
    """One tool-less Claude run as the resident, as the reconcile and the
    backstop make theirs; its answer text. Raises on a failed run."""
    try:
        import ea_config
        import mail_privacy
    except ModuleNotFoundError:  # imported as preprocessor.<mod>
        from preprocessor import ea_config, mail_privacy
    model = ea_config.get_models().get(TASK) or HOSTED_MODEL
    r = mail_privacy._run_claude(prompt, model, "", timeout=HOSTED_TIMEOUT_S)
    if r.get("rc") or not r.get("text"):
        raise RuntimeError(f"claude rc={r.get('rc')}: {(r.get('stderr') or '')[-160:]}")
    return str(r["text"])


# The hosted runner; tests put a fake here.
CLAUDE = _claude


def hosted_answer(prompt: str) -> str | None:
    """Claude's text for a batch of threads, or None when the route is
    local or the run failed (logged once an hour; the states stay as they
    were)."""
    try:
        if route() not in ("hosted", "auto"):
            return None
        return CLAUDE(prompt)
    except Exception as e:
        _say_once("hosted-failed", f"web chat: the hosted model did not judge the thread states; "
                                   f"they stay as they were: {str(e)[:300]}", logging.WARNING)
        return None


def quiet_now() -> bool:
    """True while the owner's quiet hours hold; True when they cannot be
    read, so no nudge goes out at night by mistake."""
    try:
        try:
            import quiet_hours
        except ModuleNotFoundError:  # imported as preprocessor.<mod>
            from preprocessor import quiet_hours
        return bool(quiet_hours.should_defer("normal"))
    except Exception as e:
        _say_once("quiet", f"web chat: quiet hours could not be read, so no nudge goes out: {e}")
        return True


# ── the owner's tap and the model's answer: labels and the write-back ──

HERE = Path(__file__).resolve().parent
# The owner's words a write-back close records (closure.py: the owner's
# statement closes), and the source the missed-closure log names.
WEB_DONE_PHRASE = "marked done in the web chat"
WEB_DONE_NOTE = "done, per the owner (web chat)"
WEB_SOURCE = "web-chat"
WRITEBACK_CAP = 3
REOPEN_DAYS = 7
CLI_TIMEOUT_S = 180
# Rows a tap may close: the ledger's open statuses, and the older
# `waiting` status.
OPEN_STATUSES = tuple(_ledger.OPEN_STATUSES) + ("waiting",)
# Interaction types whose threads name rows in passing (a briefing lists
# many): a tap there writes nothing back.
NO_WRITEBACK = frozenset({"automated", "briefing_reply"})


def _entries(ids: list, db_path: Path | None = None) -> list[dict]:
    """The full ledger rows (metadata merged) for `ids`, read-only."""
    ids = sorted({str(i) for i in ids or [] if i})
    if not ids:
        return []
    conn = _ledger.guarded_connect(db_path or _ledger.DB_PATH, read_only=True)
    out: list[dict] = []
    try:
        conn.row_factory = sqlite3.Row
        for i in range(0, len(ids), CHUNK):
            part = ids[i:i + CHUNK]
            out += [_ledger._row_to_dict(r) for r in conn.execute(
                f"SELECT * FROM ledger WHERE id IN ({','.join('?' * len(part))})", part)]
    finally:
        conn.close()
    return out


def auto_closed(entry: dict) -> str:
    """The signal that closed a row with no word or click of the owner's,
    or "" (the owner closed it, a duplicate was folded, or it is open)."""
    if str(entry.get("status") or "") not in ("completed", "closed"):
        return ""
    cb = entry.get("closed_by") if isinstance(entry.get("closed_by"), dict) else {}
    last = entry.get("last_action") or ""
    sig = str(cb.get("signal") or "") or _closure.infer_signal(last)
    if sig in ("duplicate_of", "detector_sibling"):
        return ""
    if sig:
        return "" if sig in _closure.USER_SIGNALS else sig
    prov = _labels._provenance(last)
    return prov if prov in ("scrubber", "briefing_llm", "inbox_archive") else ""


def writeback_plan(ctx: dict, state: str, *, now: float,
                   entries: list[dict] | None = None) -> dict:
    """What a tap writes back: {"action", "ops": [(op, ledger id), ...]}.
    op is close, reject or reopen. The action says why nothing is written
    when the ops are empty (none, automated, ambiguous)."""
    if state not in ("done", "needs_you"):
        return {"action": "none", "ops": []}
    rows = ctx.get("ledger") or []
    if not rows:
        return {"action": "none", "ops": []}
    kind = (ctx.get("channel_row") or {}).get("kind")
    if _labels.interaction_type(ctx.get("messages") or [], kind,
                                ctx.get("origin") or "") in NO_WRITEBACK:
        return {"action": "automated", "ops": []}
    found = entries if entries is not None else _entries([r.get("id") for r in rows])
    found = [e for e in found if not _closure.is_plugin_row(e)]
    if state == "done":
        open_ = [e for e in found if str(e.get("status") or "") in OPEN_STATUSES]
        if not open_:
            return {"action": "none", "ops": []}
        convs = {str(e.get("conversation_id") or "") for e in open_}
        if len(open_) > WRITEBACK_CAP or (len(open_) > 1 and (len(convs) > 1 or "" in convs)):
            return {"action": "ambiguous", "ops": []}
        return {"action": "close", "ops": [("close", e["id"]) for e in open_]}
    since = now - REOPEN_DAYS * 86400
    ops = [("reject", e["id"]) for e in found
           if str(e.get("status") or "") in OPEN_STATUSES
           and isinstance(e.get("close_proposal"), dict)]
    for e in found:
        closed_at = _labels.epoch(e.get("closed_at"))
        if closed_at is None or closed_at < since:
            continue
        if auto_closed(e) or (e.get("last_action") or "") == WEB_DONE_NOTE:
            ops.append(("reopen", e["id"]))
    if not ops:
        return {"action": "none", "ops": []}
    if len(ops) > WRITEBACK_CAP:
        return {"action": "ambiguous", "ops": []}
    names = sorted({op for op, _ in ops})
    return {"action": "_".join(names), "ops": ops}


def _cli(script: str, *args: str) -> dict:
    """One of the EA's CLIs as this process (the bot runs its ledger verbs
    this way); its last JSON line, with `_exit`."""
    r = subprocess.run([sys.executable, str(HERE / script), *args], capture_output=True,
                       text=True, timeout=CLI_TIMEOUT_S, cwd=str(HERE.parent))
    out: dict = {}
    for line in reversed((r.stdout or "").strip().splitlines()):
        try:
            got = json.loads(line)
        except ValueError:
            continue
        if isinstance(got, dict):
            out = got
            break
    out["_exit"] = r.returncode
    return out


# The CLI runner; tests put a fake ledger here.
RUN = _cli


def run_writeback(plan: dict) -> list[dict]:
    """Carry out a plan's ops through the CLIs; one result per op."""
    today = date.today().isoformat()
    done = []
    for op, lid in plan.get("ops") or []:
        try:
            if op == "close":
                res = RUN("ledger.py", "close", "--id", lid, "--user-said", WEB_DONE_PHRASE,
                          "--note", WEB_DONE_NOTE, "--skip-archive")
                if res.get("action") == "closed":
                    RUN("log_missed_closure.py", "--ledger-id", lid, "--user-message",
                        WEB_DONE_PHRASE, "--source", WEB_SOURCE, "--quiet")
            elif op == "reject":
                res = RUN("ledger.py", "reject", "--id", lid, "--actor", WEB_SOURCE)
                RUN("item_feedback.py", "--id", lid, "--kind", "still_open", "--source", WEB_SOURCE)
            elif op == "reopen":
                res = RUN("ledger.py", "update", "--id", lid, "--status", "open", "--last-action",
                          f"{today}: reopened; the owner marked the conversation as needing "
                          f"them (web chat)")
                RUN("item_feedback.py", "--id", lid, "--kind", "still_open", "--source", WEB_SOURCE)
            else:
                continue
        except Exception as e:
            log.warning("web chat: the ledger write-back (%s %s) failed: %s", op, lid, e)
            done.append({"op": op, "id": lid, "ok": False})
            continue
        log.info("web chat: owner tap wrote back %s on ledger row %s: %s", op, lid,
                 res.get("action") or res.get("error") or res.get("_exit"))
        done.append({"op": op, "id": lid, "ok": bool(res.get("ok", res.get("_exit") == 0))})
    return done


def observe(event: str, ctx: dict, *, now: float, store=None, **info) -> None:
    """The engine's observer. A model answer becomes a label record; an
    owner tap becomes a label record (with what it writes back) and then
    the write-back itself."""
    if event == "model":
        _labels.record_model(ctx, answer=info.get("answer"), row=info.get("row"), now=now,
                             store=store, via=info.get("via"))
        return
    if event != "owner":
        return
    row, before = info.get("row") or {}, info.get("before") or {}
    try:
        plan = writeback_plan(ctx, str(row.get("state") or ""), now=now)
    except Exception as e:
        log.warning("web chat: the ledger write-back for an owner tap was not planned: %s", e)
        plan = {"action": "error", "ops": []}
    try:
        _labels.record_owner_tap(ctx, row=row, before=before, now=now, store=store,
                                 writeback=plan["action"])
    except Exception as e:
        log.warning("web chat: the owner tap was not recorded as a closure label: %s", e)
    run_writeback(plan)


def enable(frontend) -> bool:
    """Turn the conversation states on for a web front end; False for a
    front end that has none."""
    on = getattr(frontend, "enable_thread_state", None)
    if not callable(on):
        return False
    states = on(ledger=ledger_rows, model=model_answer, hosted=hosted_answer, route=route,
                quiet=quiet_now)
    if states is not None and hasattr(states, "observer"):
        states.observer = observe
    return True

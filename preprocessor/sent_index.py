#!/usr/bin/env python3
"""The user's own sent mail, indexed for the writer: real examples of
their voice, found by meaning with a local embedding model.

The writer drafts better from the user's real emails than from a
description of their style. For each draft it gets a few of them: the
ones to the same recipient that are closest to the brief, then the
closest ones to anyone (`examples`). Mail the EA wrote is never an
example (ea_written.py): an example of the writer's own prose would
teach it its own voice.

    sent_index.py sync [--since YYYY-MM-DD] [--max-pages N]
        Read Sent Items from the watermark (or --since) forward, page by
        page, and index each page as it arrives: own text, recipients,
        whether the EA wrote it, and its embedding. A failure stops the
        run with what it has; the next run resumes from the watermark.
    sent_index.py stats
    sent_index.py search --query TEXT [--to ADDR] [--before ISO]
        Ids, dates and scores only, never the text.

The weekly `style_profiles.py sync` also hands its fetched mail to
`add_fetched`, so the index keeps up with no read of its own.

Mail the EA may have written before its bodies were on record (from when
it began drafting to the first sync, less draft_capture's retention) is
never an example either (`unchecked`).

Store: <private_dir>/sent-index.db, owner-only. The embeddings come from
an OpenAI-compatible /v1/embeddings endpoint on the owner's machine
(`embed_endpoint` and `embed_model` in remote-llm.json; default a local
Ollama with EMBED_MODEL). Nothing here sends mail to a hosted service.
The writer only uses examples when the `voice_examples` switch is on.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

DB_NAME = "sent-index.db"
EMBED_ENDPOINT = "http://127.0.0.1:11434/v1"
EMBED_MODEL = "qwen3-embedding:0.6b"
EMBED_BATCH = 32
EMBED_TIMEOUT_S = 120
TEXT_CHARS = 6000
EMBED_CHARS = 2000
MIN_CHARS = 40
# Above this many recipients the message is an announcement, no model
# for a draft to one person (style_profiles.MAX_CONVERSATION_RECIPIENTS).
MAX_RECIPIENTS = 5
PAGE = 25
FIRST_SINCE = "2020-01-01"
# An example under this many words shows little of a voice; one over the
# cap is shown cut.
EXAMPLE_MIN_WORDS = 12
EXAMPLE_CHARS = 1500
K_SAME = 3
K_ANY = 3
# Two candidates this close are the same email to different people.
NEAR_DUPLICATE = 0.95
QUERY_INSTRUCT = ("Instruct: Given the brief for an email, retrieve emails of the "
                  "same kind that the same person wrote\nQuery: ")
SELECT = ("id,conversationId,sentDateTime,toRecipients,ccRecipients,subject,body")


class EmbedError(Exception):
    """The embedding endpoint gave no usable answer."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def db_path() -> Path:
    import owner_trust
    return owner_trust.private_dir() / DB_NAME


def embed_config(cfg: dict | None = None) -> tuple[str, str]:
    """(endpoint, model) for embeddings, from remote-llm.json."""
    if cfg is None:
        try:
            import remote_llm
            cfg = remote_llm.config()
        except Exception:
            cfg = {}
    return ((cfg.get("embed_endpoint") or EMBED_ENDPOINT).rstrip("/"),
            cfg.get("embed_model") or EMBED_MODEL)


def embed(texts: list[str], *, endpoint: str = "", model: str = "",
          post=None) -> list[list[float]]:
    """One vector per text, from the local endpoint. Raises EmbedError."""
    if not texts:
        return []
    if not endpoint or not model:
        endpoint, model = embed_config()
    if post is None:
        import requests
        post = requests.post
    out: list[list[float]] = []
    for i in range(0, len(texts), EMBED_BATCH):
        batch = [t[:EMBED_CHARS] for t in texts[i:i + EMBED_BATCH]]
        try:
            r = post(f"{endpoint}/embeddings", json={"model": model, "input": batch},
                     timeout=EMBED_TIMEOUT_S)
            r.raise_for_status()
            data = sorted(r.json()["data"], key=lambda d: d.get("index", 0))
            vecs = [d["embedding"] for d in data]
        except Exception as e:
            raise EmbedError(f"{endpoint}: {type(e).__name__}: {str(e)[:200]}") from e
        if len(vecs) != len(batch):
            raise EmbedError(f"{endpoint}: {len(vecs)} vectors for {len(batch)} texts")
        out.extend(vecs)
    return out


def _unit(vec) -> bytes:
    import numpy as np
    v = np.asarray(vec, dtype=np.float32)
    n = float(np.linalg.norm(v))
    return (v / n if n else v).tobytes()


# ── the store ─────────────────────────────────────────────────────────

def connect(path: Path | None = None) -> sqlite3.Connection:
    path = Path(path or db_path())
    new = not path.exists()
    if new:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600))
    con = sqlite3.connect(path, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript("""
        CREATE TABLE IF NOT EXISTS mail (
            id TEXT PRIMARY KEY,
            conversation_id TEXT DEFAULT '',
            sent_at TEXT DEFAULT '',
            recipients TEXT DEFAULT '[]',
            subject TEXT DEFAULT '',
            text TEXT DEFAULT '',
            words INTEGER DEFAULT 0,
            ea_written INTEGER DEFAULT 0,
            model TEXT DEFAULT '',
            emb BLOB
        );
        CREATE INDEX IF NOT EXISTS mail_sent ON mail(sent_at);
        CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
    """)
    return con


def ea_since(cfg: dict | None = None) -> str:
    """When the EA began drafting: `ea_drafting_since` in remote-llm.json,
    else the ledger's first row. '' when neither is known."""
    if cfg is None:
        try:
            import remote_llm
            cfg = remote_llm.config()
        except Exception:
            cfg = {}
    if cfg.get("ea_drafting_since"):
        return str(cfg["ea_drafting_since"])
    try:
        import ledger
        con = ledger.guarded_connect(ledger.DB_PATH, timeout=5, read_only=True)
        try:
            row = con.execute("SELECT MIN(created_at) FROM ledger").fetchone()
        finally:
            con.close()
        return (row[0] or "") if row else ""
    except Exception:
        return ""


def unchecked(con, cfg: dict | None = None) -> tuple[str, str]:
    """(from, until): sent mail no EA record covers. The EA drafted from
    `ea_since`, but its bodies are on record only from `checked_from`
    (the first sync, less draft_capture's retention), so mail between the
    two may be the EA's and is never an example. ('', '') when there is
    no such window."""
    lo, hi = ea_since(cfg), _state(con, "checked_from")
    return (lo, hi) if lo and hi and lo < hi else ("", "")


def _state(con, key: str, value: str | None = None) -> str:
    if value is not None:
        con.execute("INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)", (key, value))
        return value
    row = con.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else ""


def _recipients(m: dict) -> list[str]:
    out = []
    for field in ("toRecipients", "ccRecipients"):
        for r in m.get(field) or []:
            addr = ((r.get("emailAddress") or {}).get("address") or "").strip().lower()
            if addr and addr not in out:
                out.append(addr)
    return out


def add(con: sqlite3.Connection, messages: list[dict], *, matcher=None, embedder=None,
        model: str = "") -> dict:
    """Index these sent messages (Graph shape, with `body`). A message
    already indexed is skipped. Returns counts. Raises EmbedError when the
    endpoint fails, before anything of the batch is written."""
    import style_profiles
    if matcher is None:
        import ea_written
        matcher = ea_written.load()
    if embedder is None:
        endpoint, model = embed_config()
        def embedder(texts):
            return embed(texts, endpoint=endpoint, model=model)
    rows, skipped, ea = [], 0, 0
    for m in messages:
        mid, sent_at = m.get("id") or "", m.get("sentDateTime") or ""
        rcpt = _recipients(m)
        if not mid or not sent_at or not rcpt or len(rcpt) > MAX_RECIPIENTS:
            skipped += 1
            continue
        if con.execute("SELECT 1 FROM mail WHERE id = ?", (mid,)).fetchone():
            skipped += 1
            continue
        text = style_profiles._message_text(m)
        if len(text) < MIN_CHARS:
            skipped += 1
            continue
        conv = m.get("conversationId") or ""
        written = matcher.is_ea_written(text, conv, sent_at)
        ea += written
        rows.append((mid, conv, sent_at, json.dumps(rcpt), (m.get("subject") or "")[:300],
                     text[:TEXT_CHARS], len(text.split()), int(written)))
    vecs = embedder([f"{r[4]}\n\n{r[5]}" for r in rows]) if rows else []
    for r, v in zip(rows, vecs):
        con.execute("INSERT OR IGNORE INTO mail (id, conversation_id, sent_at, recipients, "
                    "subject, text, words, ea_written, model, emb) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (*r, model, _unit(v)))
    con.commit()
    return {"added": len(rows), "skipped": skipped, "ea_written": ea}


def add_fetched(messages: list[dict]) -> dict:
    """What a caller that already read Sent Items hands over (the weekly
    profile sync). Best-effort: a missing endpoint or a locked store is
    reported, never raised."""
    try:
        con = connect()
    except Exception as e:
        return {"error": f"store: {e}"}
    try:
        return add(con, messages)
    except Exception as e:
        return {"error": str(e)[:200]}
    finally:
        con.close()


# ── the read, at draft time ───────────────────────────────────────────

def _matrix(con, where: str, args: tuple, skip: set):
    """(ids, unit vectors) of the rows that qualify; vectors only, so a
    query over the whole archive does not load its text."""
    import numpy as np
    rows = [r for r in con.execute(f"SELECT id, emb FROM mail WHERE {where}", args)
            if r["emb"] and r["id"] not in skip]
    if not rows:
        return [], None
    return [r["id"] for r in rows], np.vstack(
        [np.frombuffer(r["emb"], dtype=np.float32) for r in rows])


def _rank(ids, mat, qv, k: int, picked: list, picked_vecs: list) -> None:
    """Add to `picked` (id, score) the best of `ids` until it holds k,
    skipping near duplicates of what it already holds."""
    import numpy as np
    if not ids:
        return
    scores = mat @ qv
    for i in np.argsort(-scores):
        if len(picked) >= k:
            break
        v = mat[i]
        if any(float(v @ p) >= NEAR_DUPLICATE for p in picked_vecs):
            continue
        picked.append((ids[i], round(float(scores[i]), 4)))
        picked_vecs.append(v)


def examples(brief: str, to: str = "", *, conversation_id: str = "", before: str = "",
             exclude_ids: tuple = (), k_same: int = K_SAME, k_any: int = K_ANY,
             con: sqlite3.Connection | None = None, embedder=None) -> list[dict]:
    """The user's own emails closest to `brief`: up to k_same to `to`,
    then up to k_any to anyone. Never mail the EA wrote, never the draft's
    own conversation; with `before`, only mail sent earlier (the
    evaluation drafts as of a date). [] when the store has none to give.
    Raises EmbedError when the endpoint fails."""
    import numpy as np
    own = con is None
    con = con or connect()
    try:
        where = ["ea_written = 0", "words >= ?"]
        args: list = [EXAMPLE_MIN_WORDS]
        if conversation_id:
            where.append("conversation_id != ?")
            args.append(conversation_id)
        if before:
            where.append("sent_at < ?")
            args.append(before)
        lo, hi = unchecked(con)
        if lo:
            where.append("NOT (sent_at >= ? AND sent_at < ?)")
            args += [lo, hi]
        skip = set(exclude_ids or ())
        if not con.execute(f"SELECT 1 FROM mail WHERE {' AND '.join(where)} LIMIT 1",
                           args).fetchone():
            return []
        embedder = embedder or (lambda texts: embed(texts))
        qv = np.frombuffer(_unit(embedder([QUERY_INSTRUCT + brief])[0]), dtype=np.float32)
        picked: list = []
        vecs: list = []
        addr = (to or "").strip().lower()
        if addr and k_same:
            ids, mat = _matrix(con, " AND ".join(where + ["recipients LIKE ?"]),
                               (*args, f'%"{addr}"%'), skip)
            _rank(ids, mat, qv, k_same, picked, vecs)
        if k_any:
            ids, mat = _matrix(con, " AND ".join(where), tuple(args),
                               skip | {i for i, _ in picked})
            _rank(ids, mat, qv, len(picked) + k_any, picked, vecs)
        out = []
        for mid, score in picked:
            r = con.execute("SELECT id, conversation_id, sent_at, recipients, subject, text, "
                            "words FROM mail WHERE id = ?", (mid,)).fetchone()
            out.append({**{k: r[k] for k in ("id", "conversation_id", "sent_at", "subject",
                                             "text", "words")},
                        "recipients": json.loads(r["recipients"] or "[]"), "score": score})
        return out
    finally:
        if own:
            con.close()


def render(found: list[dict]) -> str:
    """The prompt section that shows the writer the user's own emails."""
    if not found:
        return ""
    parts = ["=== The user's own emails (real, written and sent by them) ===",
             "Write the way these read: their length, their sentences, their asides "
             "and punctuation, how they open and close. They are about other "
             "matters: take no facts from them."]
    for i, x in enumerate(found, 1):
        to = ", ".join(x.get("recipients") or [])
        text = x["text"] if len(x["text"]) <= EXAMPLE_CHARS else x["text"][:EXAMPLE_CHARS] + " [...]"
        parts.append(f"--- Email {i} (to {to}, {str(x.get('sent_at'))[:10]}) ---\n{text}")
    return "\n\n".join(parts)


# ── the sync ──────────────────────────────────────────────────────────

def sync(*, since: str = "", max_pages: int = 0, con: sqlite3.Connection | None = None,
         fetch=None, matcher=None, embedder=None) -> dict:
    """Index Sent Items from the watermark (or `since`) forward, a page at
    a time; the watermark moves after each page. Stops at the first
    failure, keeping what it indexed."""
    own = con is None
    con = con or connect()
    totals = {"pages": 0, "added": 0, "skipped": 0, "ea_written": 0}
    try:
        if not _state(con, "checked_from"):
            # From here on every EA body is on record when its mail is
            # indexed: draft_capture keeps a month, the writer's logs more.
            import draft_capture
            from datetime import timedelta
            _state(con, "checked_from", (datetime.now(timezone.utc) - timedelta(
                days=draft_capture.RETENTION_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ"))
            con.commit()
        start = since or _state(con, "watermark") or FIRST_SINCE
        if len(start) == 10:
            start += "T00:00:00Z"
        if fetch is None:
            import graph_client as graph

            def fetch(url, params):
                return graph.graph_get(url, params)
        if matcher is None:
            import ea_written
            matcher = ea_written.load()
        if embedder is None:
            endpoint, model = embed_config()
            totals["model"] = model

            def embedder(texts):
                return embed(texts, endpoint=endpoint, model=model)
        url, params = "/me/mailfolders/sentitems/messages", {
            "$select": SELECT, "$filter": f"sentDateTime ge {start}",
            "$orderby": "sentDateTime asc", "$top": PAGE}
        while url:
            try:
                data = fetch(url, params)
                page = data.get("value") or []
                got = add(con, page, matcher=matcher, embedder=embedder,
                          model=totals.get("model", ""))
            except Exception as e:
                totals["stopped"] = f"{type(e).__name__}: {str(e)[:200]}"
                break
            totals["pages"] += 1
            for k in ("added", "skipped", "ea_written"):
                totals[k] += got[k]
            last = max((m.get("sentDateTime") or "" for m in page), default="")
            if last:
                _state(con, "watermark", last)
                con.commit()
            url, params = data.get("@odata.nextLink"), None
            if max_pages and totals["pages"] >= max_pages:
                break
        totals["watermark"] = _state(con, "watermark")
        return totals
    finally:
        if own:
            con.close()


def stats(con: sqlite3.Connection | None = None) -> dict:
    own = con is None
    con = con or connect()
    try:
        r = con.execute("SELECT COUNT(*) n, SUM(ea_written) ea, MIN(sent_at) lo, "
                        "MAX(sent_at) hi FROM mail").fetchone()
        lo, hi = unchecked(con)
        return {"messages": r["n"], "ea_written": r["ea"] or 0, "first": r["lo"] or "",
                "last": r["hi"] or "", "watermark": _state(con, "watermark"),
                "unchecked": {"from": lo, "until": hi} if lo else None}
    finally:
        if own:
            con.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="The user's sent mail, indexed for the writer",
                                 allow_abbrev=False)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("sync", allow_abbrev=False)
    p.add_argument("--since", default="")
    p.add_argument("--max-pages", type=int, default=0)
    sub.add_parser("stats", allow_abbrev=False)
    p = sub.add_parser("search", allow_abbrev=False)
    p.add_argument("--query", required=True)
    p.add_argument("--to", default="")
    p.add_argument("--before", default="")
    args = ap.parse_args(argv)
    import owner_trust
    if not owner_trust.is_owner_process():
        print(json.dumps({"status": "error", "message": "owner process only"}))
        return 2
    if args.cmd == "sync":
        t0 = time.monotonic()
        out = sync(since=args.since, max_pages=args.max_pages)
        print(json.dumps({"status": "ok", **out, "secs": round(time.monotonic() - t0)}))
    elif args.cmd == "stats":
        print(json.dumps(stats()))
    else:
        found = examples(args.query, args.to, before=args.before)
        print(json.dumps([{"id": x["id"][-16:], "sent_at": x["sent_at"],
                           "score": x["score"], "words": x["words"],
                           "to_count": len(x["recipients"])} for x in found], indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())

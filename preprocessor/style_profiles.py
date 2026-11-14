"""Persistent per-contact writing-style profiles.

The authoritative per-recipient style source. It accumulates rather
than sampling a window, so a contact outside the last two weeks of sent
mail still has a profile:
  - SQLite at ~/.ea/state/style-profiles.db (WAL, same conventions as
    the ledger). Coverage grows monotonically: every contact ever
    emailed keeps a durable profile.
  - Incremental: `update_from_messages(sent)` is called by the
    preprocessor with the sent mail it ALREADY fetched (zero extra API
    calls); a sentDateTime watermark makes re-runs idempotent.
  - Few-shot samples: the latest 3 real message snippets per contact
    (quoted history stripped). At draft time the model mimics actual
    examples, not feature descriptions.

CLI (allowlisted for the drafting lanes):
    python3 style_profiles.py get --contact alice@host.edu   # profile+samples
    python3 style_profiles.py sync [--days 180]              # backfill/cron
    python3 style_profiles.py stats

`sync` reads Sent Items, which needs the mail token. The resident holds
no token under mail mediation, so its `sync` goes to the mediator, which
runs it as the owner (mail_mediation.SAFE). `get` and `stats` read the
local database only and run in place everywhere.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

EA_DIR = Path(os.environ.get("EA_DIR", str(Path.home() / ".ea")))
DB_PATH = EA_DIR / "state" / "style-profiles.db"

MAX_SAMPLES_PER_CONTACT = 3
SNIPPET_CHARS = 700
# Above this many To: addresses the message is a broadcast, and its
# voice is no model for a draft to any single recipient. Five keeps
# genuine working threads, which run four or five deep.
MAX_CONVERSATION_RECIPIENTS = 5

_GREETING_RE = re.compile(
    r"^(hey|hi|hello|dear|good (?:morning|afternoon|evening))\b[ ,]*(\w*)",
    re.IGNORECASE)
_CLOSING_WORDS = ("best", "cheers", "thanks", "thank you", "regards",
                  "sincerely", "all the best", "take care", "warmly")
_CONTRACTION_RE = re.compile(
    r"\b(?:don't|can't|won't|isn't|aren't|it's|i'm|i'll|i've|we're|"
    r"we'll|that's|there's|doesn't|didn't|haven't|you're|let's)\b",
    re.IGNORECASE)
# Marking a judgment AS a judgment is the single strongest signal in
# the user's longer mail, and it is audience-dependent: densest to faculty
# peers, absent to students. A draft that asserts where he would hedge
# reads as someone else's writing.
_HEDGE_RE = re.compile(
    r"(?i)\b(?:i think|i believe|as i see it|imo|in my (?:view|opinion)|"
    r"it seems|seems like|my sense|i suspect|i'd say|i would say|my read|"
    r"i don't follow|not sure|perhaps|maybe|probably|i'm inclined)\b")
# Outlook quoted-history dividers — everything after is not the user's text
_QUOTE_SPLIT_RE = re.compile(
    r"_{10,}|(?:^|\n)From: .+?(?:\n|$)|(?:^|\n)On .{10,80} wrote:",
    re.IGNORECASE)


def _connect() -> sqlite3.Connection:
    """The profiles database, through ledger.guarded_connect: an owner
    process opens it only after the shared-database checks, and never
    creates the directory or the file itself."""
    try:
        import ledger
    except ImportError:
        from preprocessor import ledger
    if not ledger._is_owner():
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = ledger.guarded_connect(DB_PATH, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript("""
        CREATE TABLE IF NOT EXISTS profiles (
            contact TEXT PRIMARY KEY,
            display_name TEXT DEFAULT '',
            greeting TEXT DEFAULT '',
            closing TEXT DEFAULT '',
            sum_words INTEGER DEFAULT 0,
            samples INTEGER DEFAULT 0,
            contraction_hits INTEGER DEFAULT 0,
            last_sent_at TEXT DEFAULT '',
            updated_at TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS snippets (
            contact TEXT,
            sent_at TEXT,
            snippet TEXT,
            PRIMARY KEY (contact, sent_at)
        );
        CREATE TABLE IF NOT EXISTS sync_state (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        -- Per-message stats. The aggregate columns on `profiles` cannot
        -- answer what calibrates a draft: a mean word count follows one
        -- long outlier, and a last-write-wins `closing` reports a word
        -- for a contact who mostly signs off bare. Keep the rows and
        -- reduce on read.
        CREATE TABLE IF NOT EXISTS msgs (
            contact TEXT,
            sent_at TEXT,
            words INTEGER DEFAULT 0,
            greeting TEXT DEFAULT '',
            closing TEXT DEFAULT '',
            contractions INTEGER DEFAULT 0,
            questions INTEGER DEFAULT 0,
            exclamations INTEGER DEFAULT 0,
            hedges INTEGER DEFAULT 0,
            PRIMARY KEY (contact, sent_at)
        );
    """)
    return con


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_HTML_DROP_RE = re.compile(r"(?is)<(script|style|head)\b.*?</\1>")
_HTML_BREAK_RE = re.compile(r"(?i)</?(?:br|p|div|tr|li|h[1-6])\b[^>]*>")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
# Quoted history that survives the divider cut: Outlook buries these
# inside markup, so they only surface once the tags are gone.
_QUOTE_TAIL_RE = re.compile(
    r"(?im)^\s*(?:-{3,}\s*original message|sent from my |get outlook for )")
# Signature delimiters ("--" or "-- <first name>" on its own line) are
# Outlook's, not punctuation, and would otherwise read as dashes he typed.
_SIG_DELIM_RE = re.compile(r"(?m)^\s*-{2,}\s*$|^\s*-{2,}\s*(?:[A-Z][\w.\'-]*\s*){1,3}$")
# The two forms the no-em-dash rule covers. Counted apart because they
# behave differently: the true character is almost always pasted in,
# while the double hyphen is something he actually types in casual mail.
_TRUE_EM_DASH_RE = re.compile(r"—")
_DOUBLE_HYPHEN_RE = re.compile(r"(?<=\s)--(?=\s)|(?<=\w)--(?=\w)")
_MARKDOWN_RE = re.compile(r"\*\*\w|^#{2,}\s|^\s*```", re.MULTILINE)
# Meeting-invite boilerplate. Outlook sends it over the user's signature on
# every Zoom hold, so it reads as his prose and drags the profile of
# anyone he meets with regularly toward text he never wrote.
_INVITE_BOILERPLATE_RE = re.compile(
    r"(?im)^\s*(?:.{0,60}is inviting you to a scheduled zoom meeting"
    r"|join zoom meeting|microsoft teams meeting"
    r"|meeting id:\s*\d|one tap mobile|dial by your location"
    r"|_+ *$)")


def _html_to_text(body: str) -> str:
    """Markup to text, preserving line structure.

    Collapsing block tags to spaces would leave the whole email on one
    line, and the closing lives on the last line — every contact would
    then look like they get a bare sign-off.
    """
    if "<" not in (body or ""):
        return body or ""
    t = _HTML_DROP_RE.sub(" ", body)
    t = _HTML_BREAK_RE.sub("\n", t)
    t = _HTML_TAG_RE.sub("", t)
    t = html.unescape(t).replace(" ", " ").replace("\r", "")
    t = re.sub(r"[ \t]+", " ", t)
    return re.sub(r"\n{3,}", "\n\n", t)


def _own_text(body: str) -> str:
    """The user's text only — cut at the first quoted-history divider."""
    text = _QUOTE_SPLIT_RE.split(body or "", maxsplit=1)[0].strip()
    for pattern in (_QUOTE_TAIL_RE, _INVITE_BOILERPLATE_RE):
        m = pattern.search(text)
        if m:
            text = text[:m.start()].strip()
    return text


def _extract_features(text: str) -> dict:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    greeting = ""
    if lines:
        m = _GREETING_RE.match(lines[0])
        if m:
            word = m.group(1).capitalize()
            greeting = f"{word} {{name}}," if m.group(2) else f"{word},"
    closing = ""
    for ln in reversed(lines[-4:]):
        low = ln.lower().rstrip(",.!")
        for c in _CLOSING_WORDS:
            if low == c or low.startswith(c + ","):
                closing = ln.rstrip(",.")
                break
        if closing:
            break
    body = _SIG_DELIM_RE.sub("", text)
    return {
        "greeting": greeting,
        "closing": closing,
        "words": len(text.split()),
        "contractions": bool(_CONTRACTION_RE.search(text)),
        "questions": text.count("?"),
        "exclamations": text.count("!"),
        "hedges": len(_HEDGE_RE.findall(text)),
        "true_em_dashes": len(_TRUE_EM_DASH_RE.findall(body)),
        "double_hyphens": len(_DOUBLE_HYPHEN_RE.findall(body)),
        # Markdown in an email means the text was pasted from a document
        # or a model, so its punctuation is not evidence of his hand.
        "pasted_markdown": bool(_MARKDOWN_RE.search(body)),
    }


def update_from_messages(sent: list, db_path: Path | None = None,
                         reprocess: bool = False) -> dict:
    """Incrementally fold a batch of sent messages into the profiles.

    Idempotent via the sentDateTime watermark — safe to call with the
    same briefing batch repeatedly. Returns {"processed": n, "skipped": n}.

    `reprocess` ignores the watermark so a full-body sync can revisit
    messages the briefing already recorded from a 255-char preview.
    Per-message rows are keyed and updated in place, so the totals stay
    correct across the overlap. `contacts` lists the addresses whose
    profile the batch touched.
    """
    global DB_PATH
    if db_path:
        DB_PATH = Path(db_path)
    con = _connect()
    try:
        row = con.execute(
            "SELECT value FROM sync_state WHERE key='watermark'").fetchone()
        watermark = row["value"] if row else ""
        processed = skipped = 0
        new_watermark = watermark
        touched: set[str] = set()

        for m in sorted(sent or [], key=lambda x: x.get("sentDateTime") or ""):
            sent_at = m.get("sentDateTime") or ""
            if not sent_at or (sent_at <= watermark and not reprocess):
                skipped += 1
                continue
            to_list = m.get("toRecipients") or []
            if not to_list:
                skipped += 1
                continue
            # Credit every addressee of a small thread, not just the
            # first: crediting to_list[0] alone loses most of the record
            # for anyone written to in pairs. Above the threshold the
            # message is an announcement, whose voice is no model for a
            # draft to any one recipient.
            people = [(((r.get("emailAddress") or {}).get("address") or "")
                       .lower(),
                       ((r.get("emailAddress") or {}).get("name") or ""))
                      for r in to_list]
            people = [(a, n) for a, n in people if a]
            if not people or len(people) > MAX_CONVERSATION_RECIPIENTS:
                skipped += 1
                continue
            body = (m.get("body") or {}).get("content") if isinstance(
                m.get("body"), dict) else (m.get("bodyPreview") or "")
            if not body:
                body = m.get("bodyPreview") or ""
            text = _own_text(_html_to_text(body))
            if len(text) < 20:
                skipped += 1
                continue

            f = _extract_features(text)
            touched.update(addr for addr, _ in people)
            for addr, name in people:
                # The briefing folds in previews (255 chars) while the
                # cron sync fetches real bodies; whichever runs first
                # would otherwise own the watermark and lock the other
                # out. Keyed per message, a later pass with a fuller
                # body upgrades `msgs` and `snippets` in place, and only
                # a message not already counted moves the running
                # totals on `profiles`.
                seen = con.execute(
                    "SELECT 1 FROM msgs WHERE contact = ? AND sent_at = ?",
                    (addr, sent_at)).fetchone()
                if seen:
                    con.execute("""
                        UPDATE msgs SET words = ?, greeting = ?, closing = ?,
                            contractions = ?, questions = ?, exclamations = ?,
                            hedges = ? WHERE contact = ? AND sent_at = ?
                    """, (f["words"], f["greeting"], f["closing"],
                          1 if f["contractions"] else 0, f["questions"],
                          f["exclamations"], f["hedges"], addr, sent_at))
                    con.execute(
                        "INSERT OR REPLACE INTO snippets (contact, sent_at,"
                        " snippet) VALUES (?, ?, ?)",
                        (addr, sent_at, text[:SNIPPET_CHARS]))
                    continue
                con.execute("""
                    INSERT INTO profiles (contact, display_name, greeting,
                        closing, sum_words, samples, contraction_hits,
                        last_sent_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)
                    ON CONFLICT(contact) DO UPDATE SET
                        display_name = CASE WHEN excluded.display_name != ''
                            THEN excluded.display_name ELSE display_name END,
                        greeting = CASE WHEN excluded.greeting != ''
                            THEN excluded.greeting ELSE greeting END,
                        closing = CASE WHEN excluded.closing != ''
                            THEN excluded.closing ELSE closing END,
                        sum_words = sum_words + excluded.sum_words,
                        samples = samples + 1,
                        contraction_hits = contraction_hits
                            + excluded.contraction_hits,
                        last_sent_at = excluded.last_sent_at,
                        updated_at = excluded.updated_at
                """, (addr, name, f["greeting"], f["closing"], f["words"],
                      1 if f["contractions"] else 0, sent_at, _now_iso()))

                con.execute("""
                    INSERT OR REPLACE INTO msgs (contact, sent_at, words,
                        greeting, closing, contractions, questions,
                        exclamations, hedges)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (addr, sent_at, f["words"], f["greeting"], f["closing"],
                      1 if f["contractions"] else 0, f["questions"],
                      f["exclamations"], f["hedges"]))

                con.execute(
                    "INSERT OR REPLACE INTO snippets (contact, sent_at,"
                    " snippet) VALUES (?, ?, ?)",
                    (addr, sent_at, text[:SNIPPET_CHARS]))
                con.execute("""
                    DELETE FROM snippets WHERE contact = ? AND sent_at NOT IN (
                        SELECT sent_at FROM snippets WHERE contact = ?
                        ORDER BY sent_at DESC LIMIT ?)
                """, (addr, addr, MAX_SAMPLES_PER_CONTACT))

            processed += 1
            if sent_at > new_watermark:
                new_watermark = sent_at

        if new_watermark != watermark:
            con.execute(
                "INSERT OR REPLACE INTO sync_state (key, value) "
                "VALUES ('watermark', ?)", (new_watermark,))
        con.commit()
        return {"processed": processed, "skipped": skipped,
                "watermark": new_watermark, "contacts": sorted(touched)}
    finally:
        con.close()


def get_profile(contact: str, db_path: Path | None = None) -> dict | None:
    global DB_PATH
    if db_path:
        DB_PATH = Path(db_path)
    con = _connect()
    try:
        row = con.execute("SELECT * FROM profiles WHERE contact = ?",
                          (contact.lower(),)).fetchone()
        if not row:
            return None
        snippets = [dict(r) for r in con.execute(
            "SELECT sent_at, snippet FROM snippets WHERE contact = ? "
            "ORDER BY sent_at DESC", (contact.lower(),))]
        samples = row["samples"] or 1
        out = {
            "contact": row["contact"],
            "display_name": row["display_name"],
            "greeting": row["greeting"],
            "closing": row["closing"],
            "avg_words": round(row["sum_words"] / samples),
            "samples": samples,
            "uses_contractions": (row["contraction_hits"] / samples) >= 0.4,
            "last_sent_at": row["last_sent_at"],
            "recent_examples": snippets,
        }
        out.update(_reduce_msgs(con, contact.lower()))
        return out
    finally:
        con.close()


def _reduce_msgs(con: sqlite3.Connection, contact: str) -> dict:
    """Per-contact targets reduced from the retained per-message rows.

    Empty for a contact with no `msgs` rows; the aggregate fields on
    `profiles` still answer in that case.
    """
    rows = con.execute(
        "SELECT words, closing, questions, exclamations, hedges, "
        "contractions FROM msgs WHERE contact = ? ORDER BY words",
        (contact,)).fetchall()
    if not rows:
        return {}
    words = [r["words"] for r in rows]
    total = sum(words) or 1
    n = len(rows)
    bare = sum(1 for r in rows if not r["closing"])
    closings = Counter(r["closing"] for r in rows if r["closing"])

    def per_1k(field):
        return round(1000 * sum(r[field] for r in rows) / total, 1)

    return {
        "messages": n,
        "median_words": words[n // 2],
        "p90_words": words[min(n - 1, int(0.9 * n))],
        "max_words": words[-1],
        # The instruction, not the raw field: most contacts get no
        # closing word at all, just the name on its own line.
        "sign_off": ("bare — just the name, no closing word"
                     if bare > n / 2 else
                     f"{closings.most_common(1)[0][0]}, then the name"),
        "bare_close_rate": round(bare / n, 2),
        "closings_seen": dict(closings.most_common(3)),
        "questions_per_1k": per_1k("questions"),
        "exclamations_per_1k": per_1k("exclamations"),
        "hedges_per_1k": per_1k("hedges"),
        "contraction_rate": round(
            sum(r["contractions"] for r in rows) / n, 2),
    }


# How many touched addresses a sync result names; the count is always whole.
MAX_LISTED_CONTACTS = 50


def _sync_error(message: str) -> int:
    """Print a sync failure as one JSON object and return the exit code.
    Nothing was written: the fetch comes before the one transaction."""
    print(json.dumps({"status": "error", "message": message,
                      "profiles": "unchanged"}))
    return 1


def _fetch_sent(days: int) -> list:
    """Sent mail of the last `days` days, with real bodies.

    `get_sent` selects bodyPreview, which Outlook caps at 255 chars, so
    a profile built from it stores openings and never an ending. Ask for
    `body` explicitly and fall back to previews if the folder query is
    unavailable. Raises when neither read works.
    """
    import graph_client as graph
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        return graph.graph_get_all("/me/mailfolders/sentitems/messages", {
            "$select": graph.MAIL_FIELDS_FULL,
            "$filter": f"sentDateTime ge {cutoff}",
            "$orderby": "sentDateTime desc",
            "$top": 100,
        })
    except Exception as exc:
        print(json.dumps({"warning": f"full-body fetch failed ({exc}); "
                                     "falling back to previews"}),
              file=sys.stderr)
        return graph.get_sent(days_back=days)


def cmd_sync(args) -> int:
    """Backfill from sent mail, with real bodies. A failed read prints a
    JSON error and leaves every profile as it was."""
    try:
        sent = _fetch_sent(args.days)
    except Exception as exc:
        return _sync_error(f"could not read Sent Items: {exc}")
    try:
        result = update_from_messages(sent, reprocess=True)
    except Exception as exc:
        return _sync_error(f"could not update the profiles: {exc}")
    contacts = result.pop("contacts", [])
    print(json.dumps({"status": "ok", "fetched": len(sent), **result,
                      "contacts_updated": len(contacts),
                      "contacts": contacts[:MAX_LISTED_CONTACTS]}))
    return 0


def cmd_get(args):
    profile = get_profile(args.contact)
    if profile is None:
        print(json.dumps({
            "contact": args.contact, "found": False,
            "note": "No history with this contact — use "
                    "writing_style.default_* from the briefing."}))
        return
    print(json.dumps({"found": True, **profile}, indent=2))


def cmd_stats(args):
    con = _connect()
    try:
        n = con.execute("SELECT COUNT(*) c FROM profiles").fetchone()["c"]
        s = con.execute("SELECT COUNT(*) c FROM snippets").fetchone()["c"]
        w = con.execute("SELECT value FROM sync_state "
                        "WHERE key='watermark'").fetchone()
        print(json.dumps({"contacts": n, "snippets": s,
                          "watermark": w["value"] if w else ""}))
    finally:
        con.close()


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Per-contact style profiles", allow_abbrev=False)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("sync", allow_abbrev=False)
    p.add_argument("--days", type=int, default=14)
    p.set_defaults(fn=cmd_sync)
    p = sub.add_parser("get", allow_abbrev=False)
    p.add_argument("--contact", required=True)
    p.set_defaults(fn=cmd_get)
    p = sub.add_parser("stats", allow_abbrev=False)
    p.set_defaults(fn=cmd_stats)
    return ap


# The verbs that read the local database only, and run in place everywhere.
LOCAL_VERBS = ("get", "stats")


def main(argv: list[str] | None = None) -> int:
    import mail_mediation
    argv = sys.argv[1:] if argv is None else list(argv)
    if not argv or argv[0] not in LOCAL_VERBS:
        # The resident has no mail token: under mail mediation the owner's
        # mediator runs `sync` and this process prints its answer and exits;
        # any other verb is refused there. Elsewhere (the owner, a host with
        # no mediator) it returns.
        mail_mediation.forward_or_none("style_profiles", argv)
    ap = build_parser()
    args = ap.parse_args(argv)
    mail_mediation.check_child_parse(ap, args)  # the verb the mediator checked
    return args.fn(args) or 0


if __name__ == "__main__":
    sys.exit(main())

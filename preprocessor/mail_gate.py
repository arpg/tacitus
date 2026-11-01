#!/usr/bin/env python3
"""Mail gate: what Claude's bulk lanes may see of the mail when the mail
switch is `enforce` (docs/remote-llm.md, "Enforce").

The remote model reads every message first (mail_privacy.py). The
preprocessor's detectors still run on the raw inbox, because a reply the
remote model files as FYI still proves a thread is not dropped; this
module filters and rewrites what the detectors return before the ledger,
the scheduling queue or the briefing file sees it:

  * actionable mail (needs a reply, a task, a decision or a meeting time)
    passes as the remote model's record: summary, ask, due date, offered
    times. The subject, sender and ids stay, so a lane can name the item
    and pull the thread when it acts on it. The message text does not.
  * FYI and awaiting-reply mail is left out. The remote digest posts it.
  * mail on the keep_from_anthropic list is left out.
  * mail the remote model has not read yet is held: it is left out of
    this run and out of the run's seen sets, so a later run surfaces it.
    Mail still unread HOLD_LIMIT after it arrived passes as sender,
    subject and ids only, so an endpoint outage never loses it.

Bot runs (a room, a thread, the DM) pull single threads through the
mediator as before; the mediator keeps kept threads from the cron lanes
only (filter_release). Outside `enforce` every call passes its input
through unchanged.
"""
from __future__ import annotations

import json
import queue
import sys
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import remote_llm  # noqa: E402

HOLD_LIMIT = timedelta(hours=4)
READ_BUDGET_S = 60.0
PREVIEW_KEYS = ("body_preview", "bodyPreview", "preview", "snippet", "latest_preview")


def active(cfg: dict | None = None) -> bool:
    return remote_llm.enforcing("mail", cfg)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(s: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def remote_text(rec: dict) -> str:
    """The remote model's record as one short paragraph, in place of the
    message text."""
    res = rec.get("result") or {}
    parts = []
    if res.get("sensitivity") == "confidential":
        parts.append(f"[confidential: {res.get('confidential_kind') or 'other'}]")
    if res.get("summary"):
        parts.append(str(res["summary"]).strip())
    if res.get("ask"):
        parts.append(f"Ask: {str(res['ask']).strip()}")
    if res.get("deadline"):
        parts.append(f"Due: {res['deadline']}")
    times = [t for t in res.get("offered_times") or [] if t]
    if times:
        parts.append("Offered times: " + ", ".join(times[:8]))
    if res.get("links"):
        parts.append("Links: " + ", ".join(str(u) for u in res["links"][:4]))
    names = [a.get("name") for a in res.get("attachments") or [] if isinstance(a, dict) and a.get("name")]
    if names:
        parts.append("Files: " + ", ".join(names[:4]))
    return " ".join(parts)[:1200]


def _record_fields(rec: dict) -> dict:
    res = rec.get("result") or {}
    out = {k: res.get(k) for k in ("state", "needs_reply", "ask", "deadline", "scheduling",
                                    "offered_times", "people", "tone", "domain")}
    for k in ("links", "quotes", "attachments"):
        if res.get(k):
            out[k] = res[k]
    return out


def _swap_previews(item: dict, text: str) -> dict:
    out = {k: v for k, v in item.items() if k not in PREVIEW_KEYS}
    had = [k for k in PREVIEW_KEYS if k in item]
    for k in had or ["body_preview"]:
        out[k] = text
    return out


def actionable(rec: dict) -> bool:
    import mail_privacy
    res = rec.get("result") or {}
    return bool(res.get("needs_reply")) or res.get("state") in mail_privacy.ACTION_STATES


class Gate:
    """One preprocessor run's view of the mail. Build it over the messages
    the run fetched; then pass each detector's output through `items`, and
    the fetched list through `ready` before it marks anything seen."""

    def __init__(self, msgs: list[dict], *, label: str, graph=None, chat=None,
                 budget_s: float = READ_BUDGET_S, cfg: dict | None = None,
                 now: datetime | None = None):
        self.cfg = cfg if cfg is not None else remote_llm.config()
        self.on = active(self.cfg)
        self.label = label
        self.now = now or _now()
        self.counts: Counter = Counter()
        self.held: set[str] = set()
        self.error = ""
        self.msgs = {m.get("id"): m for m in msgs if m.get("id")}
        self.index = {"by_id": {}, "by_conv": {}, "by_key": {}}
        self.user: set[str] = set()
        if not self.on:
            return
        try:
            import mail_privacy
            self.user = mail_privacy._user_addresses()
            self.index = mail_privacy.load_index()
            unread = [m for m in msgs if self._needs_read(m)]
            if unread:
                self._read(unread, graph, chat, budget_s)
                self.index = mail_privacy.load_index()
        except Exception as e:
            # Unread mail stays held; nothing raw goes through on an error.
            self.error = str(e)[:200]
            print(f"mail_gate: {self.error}", file=sys.stderr)

    # ── reading ──────────────────────────────────────────────────────

    def _own(self, msg: dict) -> bool:
        import mail_privacy
        return mail_privacy._addr(msg.get("from")) in self.user

    def _needs_read(self, msg: dict) -> bool:
        return (not msg.get("isDraft") and not self._own(msg)
                and msg.get("id") not in self.index["by_id"])

    def _read(self, msgs: list[dict], graph, chat, budget_s: float) -> None:
        """Extract the unread messages now, four at a time, for at most
        budget_s. Workers are daemon threads: a late call never holds the
        preprocessor open, and a record that lands late serves the next run."""
        import mail_privacy
        graph = graph or mail_privacy._default_graph()
        todo: queue.Queue = queue.Queue()
        for m in msgs:
            todo.put(m)
        left = [len(msgs)]
        lock = threading.Lock()
        done = threading.Event()

        def work():
            while True:
                try:
                    m = todo.get_nowait()
                except queue.Empty:
                    return
                try:
                    rec = mail_privacy.process(m, graph=graph, cfg=self.cfg, chat=chat)
                    remote_llm.append(mail_privacy.EXTRACTIONS, rec)
                except Exception as e:
                    print(f"mail_gate: read failed: {str(e)[:120]}", file=sys.stderr)
                with lock:
                    left[0] -= 1
                    if left[0] == 0:
                        done.set()

        for _ in range(min(remote_llm.max_parallel(self.cfg), len(msgs))):
            threading.Thread(target=work, daemon=True).start()
        done.wait(timeout=budget_s)

    # ── classes ──────────────────────────────────────────────────────

    def _record(self, item: dict) -> dict | None:
        mid = item.get("message_id") or item.get("id") or ""
        rec = self.index["by_id"].get(mid)
        if rec or mid in self.msgs:
            return rec
        # An item about mail this run did not fetch: the thread's latest
        # record speaks for it.
        conv = item.get("conversation_id") or item.get("conversationId") or ""
        recs = [r for r in self.index["by_conv"].get(conv) or [] if r.get("status") == "ok"]
        if any(r.get("keep_from_anthropic") for r in self.index["by_conv"].get(conv) or []):
            return next(r for r in self.index["by_conv"][conv] if r.get("keep_from_anthropic"))
        return max(recs, key=lambda r: r.get("received") or "") if recs else None

    def _listed(self, item: dict) -> bool:
        """The keep list by address, for mail with no usable record."""
        import mail_privacy
        msg = self.msgs.get(item.get("message_id") or item.get("id") or "") or {}
        people = ([mail_privacy._addr(msg.get("from"))] + mail_privacy._addrs(msg.get("toRecipients"))
                  + mail_privacy._addrs(msg.get("ccRecipients")))
        frm = item.get("from")
        people.append(frm if isinstance(frm, str) else mail_privacy._addr(frm))
        return any(remote_llm.keep_reason(a, self.cfg) for a in people if a)

    def record_fields(self, item: dict) -> dict | None:
        """The remote record's fields (state, scheduling, offered_times, ask,
        ...) for one message, or None when the gate is off, the message is
        the user's own, or no good record exists yet."""
        if not self.on:
            return None
        msg = self.msgs.get(item.get("message_id") or item.get("id") or "")
        if msg is not None and self._own(msg):
            return None
        rec = self._record(item)
        if not rec or rec.get("status") != "ok":
            return None
        return _record_fields(rec)

    def classify(self, item: dict) -> tuple[str, dict | None]:
        """(class, record): action | fyi | kept | own | pending | unread."""
        mid = item.get("message_id") or item.get("id") or ""
        msg = self.msgs.get(mid)
        if msg is not None and self._own(msg):
            return "own", None
        rec = self._record(item)
        if rec is not None:
            import mail_privacy
            if mail_privacy._kept(rec):
                return "kept", rec
            if rec.get("status") == "ok":
                return ("action" if actionable(rec) else "fyi"), rec
        if self._listed(item):
            return "kept", None
        received = _parse((msg or {}).get("receivedDateTime") or item.get("received") or "")
        if received and self.now - received > HOLD_LIMIT:
            return "unread", None
        return "pending", None

    # ── the run's view ───────────────────────────────────────────────

    def items(self, items: list[dict]) -> list[dict]:
        """A detector's output as the lanes may see it."""
        if not self.on:
            return items
        out = []
        for it in items or []:
            cls, rec = self.classify(it)
            self.counts[cls] += 1
            if cls == "action":
                o = _swap_previews(it, remote_text(rec))
                o["remote_record"] = _record_fields(rec)
                out.append(o)
            elif cls in ("own", "unread"):
                o = _swap_previews(it, "")
                if cls == "unread":
                    o["remote_unread"] = True
                out.append(o)
            elif cls == "pending":
                self.held.add(it.get("message_id") or it.get("id") or "")
        return out

    def strip(self, items: list[dict]) -> list[dict]:
        """Items that stay whatever the class (counts, thread lists): kept
        threads go, the rest lose their message text."""
        if not self.on:
            return items
        out = []
        for it in items or []:
            cls, rec = self.classify(it)
            if cls == "kept":
                continue
            out.append(_swap_previews(it, remote_text(rec) if rec and rec.get("status") == "ok" else ""))
        return out

    def ready(self, msgs: list[dict]) -> list[dict]:
        """The fetched messages a run may mark seen: all but the held."""
        if not self.on:
            return msgs
        return [m for m in msgs if self.classify(m)[0] != "pending"]

    def summary(self) -> dict:
        if not self.on:
            return {"mode": "off"}
        held_msgs = sum(1 for m in self.msgs.values() if self.classify(m)[0] == "pending")
        out = {"mode": remote_llm.mode("mail", self.cfg),
               "note": ("Email text is not in this file. Actionable mail carries the remote "
                        "model's summary; FYI mail and mail kept from Claude are left out and "
                        "go to the remote digest. Pull a thread with get-conversation when "
                        "you act on it."),
               "messages_fetched": len(self.msgs), "held_for_remote_read": held_msgs,
               "items": dict(self.counts)}
        if self.error:
            out["error"] = self.error
        return out


def gate(msgs: list[dict], label: str, **kw) -> Gate:
    return Gate(msgs, label=label, **kw)


# ── the remote digest ────────────────────────────────────────────────
# The mail the gate leaves out, posted from the remote model's records by
# code alone, to a place the briefing's chat reader does not read
# (chat_notify.post_digest).

DIGEST_STATE = "digest-state.json"
DIGEST_KEY = "mail-digest"
DIGEST_CAP = 60
DIGEST_FIRST = timedelta(hours=24)
DOMAIN_ORDER = ("research", "teaching", "service", "professional", "personal")
COMING_DAYS = 7
COMING_CAP = 10


def _list_senders() -> set[str]:
    """Senders the list digest covers; their mail stays out of this one."""
    try:
        import list_digest
        return {str(a).lower() for x in list_digest.load_config().get("lists") or []
                for a in (x.get("senders") or []) + [x.get("list_address") or ""] if a}
    except Exception:
        return set()


def digest_rows(since: datetime, until: datetime) -> list[tuple[str, dict]]:
    """(kind, record) for the mail left out of the lanes that arrived in
    (since, until]: `kept` (any state) and `fyi`, oldest first."""
    import mail_privacy
    user = mail_privacy._user_addresses()
    lists = _list_senders()
    rows = []
    for rec in mail_privacy.load_index()["by_key"].values():
        got = _parse(rec.get("received") or "")
        if not got or not (since < got <= until):
            continue
        frm = (rec.get("from") or "").lower()
        if frm in user or frm in lists:
            continue
        if mail_privacy._kept(rec):
            rows.append(("kept", rec))
        elif rec.get("status") == "ok" and not actionable(rec):
            rows.append(("fyi", rec))
    rows.sort(key=lambda kr: kr[1].get("received") or "")
    return rows


def coming_up(since: datetime, until: datetime) -> list[dict]:
    """FYI mail that arrived before `since` with a date in the next
    COMING_DAYS: a dated FYI item stays in the digest until its day."""
    import mail_privacy
    user = mail_privacy._user_addresses()
    lists = _list_senders()
    today = until.astimezone().date()
    out = []
    for rec in mail_privacy.load_index()["by_key"].values():
        got = _parse(rec.get("received") or "")
        res = rec.get("result") or {}
        if not got or got > since or rec.get("status") != "ok" or mail_privacy._kept(rec):
            continue
        if actionable(rec) or (rec.get("from") or "").lower() in user | lists:
            continue
        try:
            due = datetime.strptime(str(res.get("deadline") or "")[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        if 0 <= (due - today).days <= COMING_DAYS:
            out.append(rec)
    out.sort(key=lambda r: (r.get("result") or {}).get("deadline") or "")
    return out[:COMING_CAP]


def _line(rec: dict) -> str:
    res = rec.get("result") or {}
    who = rec.get("from") or "unknown sender"
    subj = (rec.get("subject") or "(no subject)").strip()
    text = str(res.get("summary") or "").strip() or "(not read)"
    due = f" Due {res['deadline']}." if res.get("deadline") else ""
    return f"• *{who}*, {subj}: {text}{due}"


def render_digest(rows: list[tuple[str, dict]], since: datetime,
                  coming: list[dict] | None = None) -> str:
    if not rows:
        return ""
    kept = [r for k, r in rows if k == "kept"]
    fyi = [r for k, r in rows if k == "fyi"]
    local = since.astimezone()
    out = [f"*Mail the remote model held back from Claude since {local:%a %H:%M}* "
           f"({len(fyi)} FYI, {len(kept)} kept). Written by the remote model; Claude did not "
           "see these. Ask in #ea to act on one."]
    shown = 0
    if kept:
        out.append("\n*Kept from Claude*")
        for r in kept[:DIGEST_CAP]:
            res = r.get("result") or {}
            flag = " *Needs your reply.*" if res.get("needs_reply") else ""
            out.append(_line(r) + flag)
            shown += 1
    by_domain: dict[str, list] = {}
    for r in fyi:
        by_domain.setdefault((r.get("result") or {}).get("domain") or "other", []).append(r)
    for dom in list(DOMAIN_ORDER) + sorted(set(by_domain) - set(DOMAIN_ORDER)):
        recs = by_domain.get(dom) or []
        if not recs or shown >= DIGEST_CAP:
            continue
        out.append(f"\n*{dom.capitalize()}*")
        for r in recs:
            if shown >= DIGEST_CAP:
                break
            out.append(_line(r))
            shown += 1
    if len(rows) > shown:
        out.append(f"\n…and {len(rows) - shown} more.")
    if coming:
        out.append("\n*Coming up* (FYI mail from earlier with a date this week)")
        out += [_line(r) for r in coming]
    return "\n".join(out)


def _post_digest(text: str, now: datetime) -> bool:
    """On Zulip the `mail-digest` stream, a topic a day; on the web chat a
    new thread in the digest channel; on Slack the `mail-digest` webhook.
    With none of them (Slack without that webhook, or no chat) it posts
    nothing and says so: the EA channel is never the fallback."""
    import chat_notify
    topic = f"Mail digest {now.astimezone():%a %b %-d}"
    got = chat_notify.post_digest(DIGEST_KEY, text, topic=topic)
    if got is None:
        print(f"mail_gate digest: no digest channel on the {chat_notify.frontend_name()} "
              "chat (on Slack, map a `mail-digest` webhook); nothing posted", file=sys.stderr)
        return False
    return bool(got)


def digest(*, dry_run: bool = False, now: datetime | None = None, post=None) -> dict:
    """Post the left-out mail since the last digest. Runs only in enforce."""
    if not active():
        return {"skipped": "mail switch is not enforce"}
    now = now or _now()
    st = (remote_llm.read(DIGEST_STATE) or [{}])[-1]
    since = _parse(st.get("until") or "") or (now - DIGEST_FIRST)
    rows = digest_rows(since, now)
    text = render_digest(rows, since, coming_up(since, now))
    if dry_run:
        return {"rows": len(rows), "kept": sum(1 for k, _ in rows if k == "kept"),
                "chars": len(text)}
    if text and not (post or _post_digest)(text, now):
        return {"error": "post failed", "rows": len(rows)}
    remote_llm.append(DIGEST_STATE, {"ts": now.isoformat(timespec="seconds"),
                                     "until": now.isoformat(timespec="seconds"),
                                     "rows": len(rows)})
    return {"posted": bool(text), "rows": len(rows)}


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("digest", help="post the mail left out of the lanes since the last digest")
    d.add_argument("--dry-run", action="store_true", help="counts only; posts nothing")
    sub.add_parser("status", help="the switch and the last digest")
    a = ap.parse_args(argv)
    if a.cmd == "digest":
        print(json.dumps(digest(dry_run=a.dry_run)))
    else:
        last = (remote_llm.read(DIGEST_STATE) or [{}])[-1]
        print(json.dumps({"enforce": active(), "last_digest": last}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

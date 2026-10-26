#!/usr/bin/env python3
"""Plugin mail triggers: new mail handed to the plugins that asked for it.

A v2 plugin declares `capabilities.mail_triggers` (docs/plugins.md). The
mail tagger's tick (owner cron, every two minutes) calls route() with the
inbox rows of the tick and the categories they carry. A trigger of an
enabled plugin whose files match the lock matches a message when every
key it names matches:

  from          the sender, or the sender's domain for an `@domain` entry
  topics        the tagger's topic tag (funding, lor, ...)
  subject       one of the substrings is in the subject
  bulk          the message carries List-Unsubscribe, List-Id or a
                Precedence of bulk or list
  exclude_from  the sender is none of these

A matched message is copied as it arrives: ids, sender, subject, time,
the trigger and the body. A move or a delete changes a message's Graph id,
and Deleted Items is outside a mailbox $search, so a plugin that searched
for its mail at its own hour missed mail deleted in between. The copy
taken on arrival does not.

Where it goes:
  * queue, owner-private: one file per plugin, the canonical list.
  * inbox, resident: <EA_DIR>/state/plugin-mail/<plugin>.json, rewritten
    from the queue on each delivery, for an agent-zone plugin. An
    owner-zone plugin reads its queue (ea_plugins.mail.delivered()).

Mail with an address on the keep_from_anthropic list never goes to an
agent-zone plugin. With the mail switch `sealed`, an agent-zone plugin
gets the ids without the body; its own read is then sealed like any
lane's.

Usage:
    python3 plugin_mail.py status
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import atomic_io as _atomic  # noqa: E402
import owner_trust as _owner_trust  # noqa: E402
import resident_io as _rio  # noqa: E402

EA_DIR = Path(os.environ.get("EA_DIR", Path.home() / ".ea"))

MARKER = "auto-tagged"
RETAIN = timedelta(days=45)
QUEUE_CAP = 200
SEEN_TTL = timedelta(days=3)
BODY_CAP = 200_000
BULK_HEADERS = ("list-unsubscribe", "list-id")
BULK_PRECEDENCE = ("bulk", "list", "junk")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _owner_dir() -> Path:
    if _owner_trust.is_owner_process():
        return _owner_trust.private_dir()
    return EA_DIR / "state"


def queue_path(plugin: str, base: Path | None = None) -> Path:
    return (base or _owner_dir()) / f"plugin-mail-{plugin}.json"


def seen_path(base: Path | None = None) -> Path:
    return (base or _owner_dir()) / "plugin-mail-seen.json"


def inbox_path(plugin: str, ea_dir: Path | None = None) -> Path:
    return (ea_dir or EA_DIR) / "state" / "plugin-mail" / f"{plugin}.json"


def _read_owner_json(path: Path, default):
    try:
        if _owner_trust.is_owner_process() and path.parent == _owner_trust.private_dir():
            raw = _owner_trust.read_owner_file(path).decode("utf-8")
        else:
            raw = path.read_text()
        data = json.loads(raw)
    except (OSError, ValueError):
        return default
    return data if isinstance(data, type(default)) else default


def _write_owner_json(path: Path, obj) -> None:
    text = json.dumps(obj, indent=1, ensure_ascii=False)
    if _owner_trust.is_owner_process() and path.parent == _owner_trust.private_dir():
        _owner_trust.write_owner_file(path, text, mode=0o600)
    else:
        _atomic.write_text(path, text, mode=0o600)


# ── matching ─────────────────────────────────────────────────────────

def _addr(rec) -> str:
    return (((rec or {}).get("emailAddress") or {}).get("address") or "").strip().lower()


def _sender_in(sender: str, entries) -> bool:
    dom = "@" + sender.rpartition("@")[2]
    return any(sender == e or (e.startswith("@") and dom == e) for e in entries)


def topics_of(cats) -> set[str]:
    import plugin_manifest as PM
    return {c for c in cats or [] if c in PM.MAIL_TOPICS}


def prematch(trigger: dict, msg: dict, cats) -> bool:
    """Every key of the trigger but `bulk`, which needs the headers."""
    sender = _addr(msg.get("from"))
    if not sender:
        return False
    if trigger.get("from") and not _sender_in(sender, trigger["from"]):
        return False
    if trigger.get("exclude_from") and _sender_in(sender, trigger["exclude_from"]):
        return False
    if trigger.get("topics") and not (set(trigger["topics"]) & topics_of(cats)):
        return False
    subj = (msg.get("subject") or "").lower()
    if trigger.get("subject") and not any(s in subj for s in trigger["subject"]):
        return False
    return True


def is_bulk(headers) -> bool:
    for h in headers or []:
        name = str((h or {}).get("name") or "").lower()
        value = str((h or {}).get("value") or "").strip().lower()
        if name in BULK_HEADERS and value:
            return True
        if name == "precedence" and value in BULK_PRECEDENCE:
            return True
    return False


def _addresses(msg: dict) -> list[str]:
    out = [_addr(msg.get("from"))]
    for key in ("toRecipients", "ccRecipients"):
        out += [_addr(r) for r in msg.get(key) or []]
    return [a for a in out if a]


def _kept(msg: dict, cfg) -> str:
    try:
        import remote_llm
        cfg = cfg if cfg is not None else remote_llm.config()
        for a in _addresses(msg):
            why = remote_llm.keep_reason(a, cfg)
            if why:
                return why
    except Exception as e:
        # A keep list that cannot be read keeps the mail from the plugin.
        return f"keep list unreadable: {str(e)[:80]}"
    return ""


def _sealed(cfg) -> bool:
    try:
        import remote_llm
        return remote_llm.mode("mail", cfg) == "sealed"
    except Exception:
        return True


# ── the pass ─────────────────────────────────────────────────────────

def _default_triggers() -> list[tuple]:
    try:
        import plugin_registry
        return [(lp.name, lp.zone, t) for lp, t in plugin_registry.mail_triggers()]
    except Exception as e:
        print(f"plugin_mail: no plugin triggers ({str(e)[:120]})", file=sys.stderr)
        return []


def _fetch(graph, mid: str) -> dict:
    return graph.graph_get(f"/me/messages/{mid}", {
        "$select": "id,body,internetMessageId,internetMessageHeaders"})


def entry(msg: dict, full: dict, plugin: str, trigger_id: str, now: datetime,
          with_body: bool) -> dict:
    body = (full or {}).get("body") or {}
    rec = {"id": msg.get("id") or "", "conversation_id": msg.get("conversationId") or "",
           "internet_message_id": (full or {}).get("internetMessageId") or "",
           "from": _addr(msg.get("from")),
           "from_name": ((msg.get("from") or {}).get("emailAddress") or {}).get("name") or "",
           "subject": msg.get("subject") or "", "received": msg.get("receivedDateTime") or "",
           "to": [_addr(r) for r in msg.get("toRecipients") or [] if _addr(r)],
           "trigger": trigger_id, "plugin": plugin, "delivered_at": _iso(now)}
    if with_body:
        content = str(body.get("content") or "")
        rec["body_type"] = str(body.get("contentType") or "html").lower()
        rec["body"] = content[:BODY_CAP]
        rec["body_truncated"] = len(content) > BODY_CAP
    else:
        rec["sealed"] = True
    return rec


def _prune(items: list[dict], now: datetime) -> list[dict]:
    cut = _iso(now - RETAIN)
    items = [i for i in items if (i.get("delivered_at") or "") >= cut]
    items.sort(key=lambda i: i.get("received") or "")
    return items[-QUEUE_CAP:]


def deliver(plugin: str, zone: str, recs: list[dict], now: datetime, *,
            base: Path | None = None, ea_dir: Path | None = None) -> int:
    """Add recs to the plugin's queue and, for an agent-zone plugin, write
    the resident's inbox from it. Returns how many were new."""
    qp = queue_path(plugin, base)
    items = _read_owner_json(qp, [])
    have = {i.get("id") for i in items}
    new = [r for r in recs if r["id"] not in have]
    items = _prune(items + new, now)
    if zone == "agent":
        # Rewritten whole from the owner's copy, nothing read back from the
        # resident's space, and written with the resident's own access.
        # The queue follows, so a failed write is retried whole.
        ip = inbox_path(plugin, ea_dir)
        if not (_rio.make_dir(ip.parent)
                and _rio.replace_text(ip, json.dumps(items, indent=1, ensure_ascii=False),
                                      mode=0o640)):
            raise OSError(f"could not write {ip}")
    _write_owner_json(qp, items)
    return len(new)


def route(msgs: list[dict], *, graph, now: datetime | None = None, cats_by_id=None,
          tagged_only: bool = True, triggers: list[tuple] | None = None, cfg=None,
          dry_run: bool = False, base: Path | None = None,
          ea_dir: Path | None = None) -> dict:
    """Hand each message in msgs to every plugin whose trigger matches it.
    `cats_by_id` overrides a row's categories (the tagger's fresh tags).
    With tagged_only, a message the tagger has not tagged yet waits for a
    later tick, since its topic is not known. Returns a summary."""
    now = now or _now()
    if not msgs:
        return {"delivered": 0, "matched": 0}
    triggers = _default_triggers() if triggers is None else triggers
    if not triggers:
        return {"delivered": 0, "matched": 0}
    cats_by_id = cats_by_id or {}
    sp = seen_path(base)
    seen = _read_owner_json(sp, {})
    out: dict[tuple, list[dict]] = {}
    matched = skipped_kept = 0
    details = []
    for m in msgs:
        mid = m.get("id")
        if not mid or mid in seen:
            continue
        cats = cats_by_id.get(mid) or m.get("categories") or []
        if tagged_only and MARKER not in cats:
            continue
        hits = [(p, z, t) for p, z, t in triggers if prematch(t, m, cats)]
        full = None
        if hits:
            try:
                full = _fetch(graph, mid)
            except Exception as e:
                # Not marked seen: the next tick tries again.
                print(f"plugin_mail: fetch failed for {mid[-12:]}: {e}", file=sys.stderr)
                continue
        seen[mid] = _iso(now)
        bulk = is_bulk((full or {}).get("internetMessageHeaders"))
        done = set()
        for plugin, zone, t in hits:
            if plugin in done or (t.get("bulk") and not bulk):
                continue
            if zone == "agent":
                why = _kept(m, cfg)
                if why:
                    skipped_kept += 1
                    details.append({"plugin": plugin, "trigger": t["id"], "kept": why,
                                    "subject": (m.get("subject") or "")[:70]})
                    continue
            with_body = zone == "owner" or not _sealed(cfg)
            out.setdefault((plugin, zone), []).append(
                entry(m, full, plugin, t["id"], now, with_body))
            done.add(plugin)
            matched += 1
            details.append({"plugin": plugin, "trigger": t["id"],
                            "subject": (m.get("subject") or "")[:70]})
    delivered = 0
    if not dry_run:
        for (plugin, zone), recs in out.items():
            try:
                delivered += deliver(plugin, zone, recs, now, base=base, ea_dir=ea_dir)
            except Exception as e:
                print(f"plugin_mail: delivery to {plugin} failed: {e}", file=sys.stderr)
                for r in recs:
                    seen.pop(r["id"], None)
        cut = _iso(now - SEEN_TTL)
        _write_owner_json(sp, {k: v for k, v in seen.items() if v >= cut})
    return {"delivered": delivered, "matched": matched, "kept": skipped_kept,
            "details": details}


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] != ["status"]:
        print(__doc__.strip().rsplit("Usage:", 1)[-1].strip())
        return 2
    rows = []
    for plugin, zone, t in _default_triggers():
        items = _read_owner_json(queue_path(plugin), [])
        rows.append({"plugin": plugin, "zone": zone, "trigger": t["id"],
                     "queued": sum(1 for i in items if i.get("trigger") == t["id"]),
                     "last": max((i.get("delivered_at") or "" for i in items
                                  if i.get("trigger") == t["id"]), default="")})
    print(json.dumps(rows, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())

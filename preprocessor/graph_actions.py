#!/usr/bin/env python3
"""
CLI helper for Claude to perform Graph API actions.
Claude Code calls this via shell commands in the skill files.

Usage:
  python graph_actions.py create-draft --to addr --subject "New topic" --body "..."
      (create-draft opens a NEW thread. A reply-shaped subject is refused;
       to answer an existing thread use create-reply, which keeps the
       quoted history and will not draft over a reply the user already sent.)
  python graph_actions.py create-reply --message-id AAMk... --body "..."
  python graph_actions.py get-message --message-id AAMk...
  python graph_actions.py get-conversation --conversation-id AAQk...
      (whole thread in ONE call -- not search-mail + N get-messages)
  (For scheduling: use scheduling_engine.py propose-meeting)
  python graph_actions.py create-event --subject "..." --start "2026-03-25T10:00" --end "2026-03-25T11:00"
  python graph_actions.py create-event --subject "..." --start "2026-08-20T14:00" --end "2026-08-20T16:00" \
      --repeat weekly --repeat-days TU,TH --repeat-until 2026-12-03   (one recurring series)
  python graph_actions.py update-event --event-id AAMk... --start "2026-04-10T14:00" --end "2026-04-10T14:30" --confirm-token ...
  python graph_actions.py delete-event --event-id AAMk... --confirm-token ...
  python graph_actions.py get-event --event-id AAMk...
  python graph_actions.py create-todo --list-name "EA Obligations" --title "..." --body "..."
  python graph_actions.py search-mail --query "arctic edge receipts"
  python graph_actions.py list-mail --folder sentitems --top 25
  python graph_actions.py archive-message --message-id AAMk...
  (For a deferred send: do NOT call schedule-send. Write
   ~/.ea/state/pending-schedule-send.json with draft_id / send_after /
   description and the bot posts a :lock: Schedule button. The button is
   what mints the --confirm-token this verb requires.)
  python graph_actions.py snooze-message --message-id AAMk... --until "2026-04-21" --description "..."
  python graph_actions.py wake-snoozed
"""

import argparse
import fcntl
import html as html_mod
import json
import os
import re
import secrets
import stat
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
if __name__ == "__main__":
    # graph_client reads the token cache at import (module level), and ea-bot
    # has no token, so mediate/refuse HERE, before that import happens. A
    # run's short id handles (id_handles) are resolved before the request
    # leaves and added to the answer when it returns.
    import id_handles as _id_handles
    sys.argv[1:] = _id_handles.resolve_argv(sys.argv[1:])
    # A verb an owner-zone plugin declares on this tool runs as that plugin's
    # verb (plugin_verbs.py); a core verb always wins.
    import plugin_verbs as _plugin_verbs
    _plugin_verbs.forward_alias("graph_actions", sys.argv[1:])
    from mail_mediation import forward_or_none
    forward_or_none("graph_actions", sys.argv[1:], transform=_id_handles.annotate_output)
import graph_client as graph
import draft_events as _draft_events  # structured journal for the bot
try:
    import atomic_io as _atomic
    import owner_trust as _owner_trust
    import resident_io as _rio
except ModuleNotFoundError:  # imported as preprocessor.<mod>
    from preprocessor import atomic_io as _atomic
    from preprocessor import owner_trust as _owner_trust
    from preprocessor import resident_io as _rio
import draft_capture as _draft_capture  # EA draft bodies, for style diffs
import draft_fingerprint as _dfp  # what a send approval is bound to
import mail_links as _mail_links  # the remote resources a Send card names

# ---------------------------------------------------------------------------
# Confirmation gate: destructive actions (send, delete) require a one-time
# token that only the bot's approval flow can issue. This prevents prompt
# injection or rogue Claude calls from sending/deleting without user consent.
# ---------------------------------------------------------------------------
EA_DIR = Path(os.environ.get("EA_DIR", Path.home() / ".ea"))


def _owner_state_dir() -> Path:
    """Where this module keeps state no resident process reads: the
    owner-private directory in an owner process, the EA tree's state
    directory in any other (a test on a scratch tree, the resident on an
    install without mail mediation). The gated verbs that use it run only
    in the bot, an owner process."""
    if _owner_trust.is_owner_process():
        return _owner_trust.private_dir()
    return EA_DIR / "state"


PENDING_CONFIRMS_FILE = _owner_state_dir() / "pending-confirmations.json"
PENDING_CONFIRMS_LOCK = _owner_state_dir() / "pending-confirmations.lock"
TOKEN_TTL_MINUTES = 30  # Tokens expire after 30 min
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)


def _under(parent: str, root: str) -> bool:
    return parent == root or parent.startswith(root.rstrip(os.sep) + os.sep)


def _state_kind(path: Path) -> str:
    """Where a state path lies, for how this process reaches it:
    "private"   the owner-private directory (owner_trust's checked access)
    "resident"  resident space (the EA tree as resolved, or the resident's
                home) in an owner process: the resident's access (resident_io)
    "local"     anything else (any path in a non-owner process, or a path of
                the owner's own, such as a scratch directory): direct access
    """
    try:
        parent = os.path.realpath(Path(path).parent)
        if _under(parent, os.path.realpath(_owner_trust.private_dir())):
            return "private"
        if not _rio.is_owner():
            return "local"
        roots = [os.path.realpath(_owner_trust.ea_dir())]
        home = os.path.expanduser(f"~{_rio.RESIDENT_USER}")
        if not home.startswith("~"):
            roots.append(os.path.realpath(home))
    except OSError:
        return "resident"
    return "resident" if any(_under(parent, r) for r in roots) else "local"


def _read_state_text(path: Path) -> str | None:
    """The text of a state file this module read-modify-writes, or None
    when it is absent. A file in the owner-private directory is read
    through owner_trust's checks; one in resident space with the resident's
    access (resident_io), so a link at the name cannot hand this process a
    file only the owner may read, which the write would then publish.
    Raises OSError when the file is there but cannot be read."""
    kind = _state_kind(path)
    if kind == "private":
        try:
            return _owner_trust.read_owner_file(path).decode("utf-8")
        except FileNotFoundError:
            return None
    if kind == "local":
        try:
            return Path(path).read_text()
        except FileNotFoundError:
            return None
    text = _rio.read_text(path)
    if text is None and os.path.lexists(path):
        raise OSError(f"{path} is there but could not be read")
    return text


def _write_state_json(path: Path, obj, *, mode: int) -> None:
    """Replace a state file atomically: through owner_trust in the
    owner-private directory, through atomic_io (random temp name, then
    replace) anywhere else."""
    if _state_kind(path) == "private":
        _owner_trust.write_owner_file(path, json.dumps(obj, indent=2), mode=mode)
    else:
        _atomic.write_json(path, obj, mode=mode)


def _pending_entry_ok(token, entry) -> bool:
    if not (isinstance(token, str) and isinstance(entry, dict)):
        return False
    if not all(isinstance(entry.get(k), str) for k in ("action", "message_id", "expires")):
        return False
    if not isinstance(entry.get("fingerprint", ""), str):
        return False
    try:
        datetime.fromisoformat(entry["expires"])
    except ValueError:
        return False
    return True


def _load_pending() -> dict:
    """The stored confirmation tokens, token -> entry. An entry that is not
    that shape is dropped, so a rewrite carries nothing else."""
    try:
        text = _read_state_text(PENDING_CONFIRMS_FILE)
        data = json.loads(text) if text else {}
    except (OSError, ValueError, _owner_trust.OwnerTrustError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if _pending_entry_ok(k, v)}


def _save_pending(data: dict):
    _write_state_json(PENDING_CONFIRMS_FILE, data, mode=0o600)


def _with_lock(fn):
    """Execute fn() while holding an exclusive file lock on the pending confirmations."""
    return _with_file_lock(PENDING_CONFIRMS_LOCK, fn)


def _open_lock(lock_path: Path) -> int:
    """A descriptor to flock for `lock_path`. The lock file is never
    truncated and a link at its name is never followed. In the owner-private
    directory it is opened relative to the checked directory; in resident
    space (from an owner process) it is created with the resident's access
    (resident_io.ensure_file) and opened read-only, since flock needs no
    write access; elsewhere it is created directly. Only a single-linked
    regular file is accepted."""
    kind = _state_kind(lock_path)
    create = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | _O_CLOEXEC
    if kind == "private":
        with _owner_trust.open_owner_dir(lock_path.parent) as dfd:
            fd = os.open(lock_path.name, create, 0o600, dir_fd=dfd)
    elif kind == "resident":
        if not _rio.ensure_file(lock_path):
            raise OSError(f"cannot create the lock file {lock_path}")
        fd = os.open(lock_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | _O_CLOEXEC)
    else:
        Path(lock_path).parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, create, 0o664)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise OSError(f"{lock_path} is not a plain single-linked file")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _with_file_lock(lock_path: Path, fn):
    """Execute fn() holding an exclusive flock on `lock_path`.

    The generic form of the pending-confirmations lock, for the other state
    files this module read-modify-writes. Without it, two concurrent snoozes,
    or a snooze racing a wake pass, silently lose one entry and leave its
    mail archived.
    """
    fd = _open_lock(lock_path)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fn()
    finally:
        os.close(fd)   # releases the lock


def _new_token() -> str:
    """A URL-safe token that never begins with '-'.

    token_urlsafe uses the base64url alphabet, so roughly 1 in 64 tokens
    starts with '-'. Such a token is indistinguishable from an option flag
    when passed as `--confirm-token <token>`, and argparse rejects the whole
    command with "expected one argument". Call sites also use the
    `--flag=value` form; this keeps the two-argument spelling safe too.
    """
    while True:
        token = secrets.token_urlsafe(16)
        if not token.startswith("-"):
            return token


def _create_confirmation(action: str, message_id: str, description: str = "",
                         fingerprint: str = "") -> str:
    """Create a one-time confirmation token for a destructive action.

    Returns the token. Only callable from the bot's Python code — Claude
    cannot generate tokens because it doesn't know the signing secret.
    Uses file locking to prevent races when multiple sends happen at once.

    A `send` token carries `fingerprint`, the content fingerprint of the
    draft the owner reviewed on the card (draft_fingerprint.py); the send
    verbs refuse a send token without one.
    """
    def _inner():
        token = _new_token()
        pending = _load_pending()

        # Prune expired tokens
        now = datetime.now()
        pending = {
            k: v for k, v in pending.items()
            if datetime.fromisoformat(v["expires"]) > now
        }

        pending[token] = {
            "action": action,
            "message_id": message_id,
            "description": description,
            "fingerprint": fingerprint or "",
            "created": now.isoformat(),
            "expires": (now + timedelta(minutes=TOKEN_TTL_MINUTES)).isoformat(),
        }
        _save_pending(pending)
        return token
    return _with_lock(_inner)


def _validate_and_consume_token(token: str, action: str, message_id: str) -> dict | None:
    """Validate a confirmation token. Consumes it on success (one-time use).

    Returns the pending entry if valid, None if invalid/expired/wrong action.
    Uses file locking to prevent races when multiple sends happen at once.
    """
    def _inner():
        pending = _load_pending()
        entry = pending.get(token)

        if not entry:
            return None

        # Check expiry
        if datetime.fromisoformat(entry["expires"]) < datetime.now():
            del pending[token]
            _save_pending(pending)
            return None

        # Check action and message_id match
        if entry["action"] != action or entry["message_id"] != message_id:
            return None

        # Consume the token (one-time use)
        del pending[token]
        _save_pending(pending)
        return entry
    return _with_lock(_inner)


# A subject the caller typed with a reply or forward prefix. Outlook writes
# that prefix; a caller writing one by hand is describing a thread that
# already exists somewhere in the mailbox.
_REPLY_SUBJECT_RE = re.compile(r"^\s*(re|fw|fwd|aw|sv|antw)\s*(\[\d+\])?\s*:",
                               re.IGNORECASE)


def cmd_create_draft(args):
    # A reply-shaped subject means the thread already exists, so this call
    # would produce a threadless re-draft under an invented subject line.
    # create-reply carries the guard for that case: it refuses to draft when
    # the user already sent the latest message in the conversation, and
    # create-draft is the way around it. CLAUDE.md rule 5 forbids the
    # fallback in prose; this enforces it where the cron lane, which never
    # reads CLAUDE.md, cannot skip it.
    if (_REPLY_SUBJECT_RE.match(args.subject or "")
            and not getattr(args, "force_new_thread", False)):
        print(json.dumps({
            "status": "refused",
            "reason": ("create-draft cannot open a reply. This subject is "
                       "reply-shaped, so the message belongs to a thread "
                       "that already exists, and a plain draft would go out "
                       "with no quoted history under a subject line you "
                       "wrote yourself."),
            "do_this_instead": [
                "search-mail --query '<recipient or topic>' with NO --folder "
                "(folder scope misses archived threads) to get the real "
                "conversation_id and the real subject",
                "get-conversation --conversation-id <id> to read what the "
                "thread already says, INCLUDING whether the user replied",
                "create-reply --message-id <id> --conversation-id <id>",
            ],
            "subject": args.subject,
        }, indent=2))
        return
    to_addrs = [a.strip() for a in args.to.split(",")]
    cc_addrs = [a.strip() for a in args.cc.split(",")] if args.cc else None
    result = graph.create_draft(
        to=to_addrs,
        subject=args.subject,
        body=args.body,
        cc=cc_addrs,
        content_type=args.content_type,
    )
    _draft_events.record("created", result.get("id", ""), verb="create-draft",
                         ledger_ids=getattr(args, "ledger_id", None))
    _draft_capture.capture(
        result.get("id", ""), args.body, verb="create-draft",
        conversation_id=result.get("conversationId", ""),
        subject=args.subject, to=to_addrs)
    print(json.dumps({"status": "draft_created", "id": result.get("id", "")}, indent=2))


def _user_addresses() -> set:
    """Mailbox owner's own email address(es), lowercased, for detecting whether
    the user already replied in a thread."""
    addrs = set()
    try:
        prof = (Path(os.environ.get("EA_DIR", str(Path.home() / ".ea")))
                / "config" / "user-profile.json")
        d = json.loads(prof.read_text())
        if d.get("email"):
            addrs.add(str(d["email"]).lower())
    except Exception:
        pass
    return addrs


def _conversation_drafts(conversation_id: str) -> list:
    """Every draft Graph holds for a conversation, newest edit first.

    Graph is unreliable about $orderby next to a conversationId $filter, so
    the sort is requested AND redone locally, and a rejected $orderby falls
    back to the plain query rather than degrading to "no drafts found".
    """
    # The query goes in params: graph_client refuses a path with a query in it.
    quoted = str(conversation_id).replace("'", "''")
    base = {"$filter": f"conversationId eq '{quoted}'",
            "$select": "id,subject,lastModifiedDateTime", "$top": 10}
    for params in ({**base, "$orderby": "lastModifiedDateTime desc"}, base):
        try:
            drafts = graph.graph_get("/me/mailFolders/drafts/messages", params).get("value", [])
        except Exception as e:
            print(f"Warning: draft lookup failed ({'with' if '$orderby' in params else 'without'}"
                  f" $orderby): {e}", file=sys.stderr)
            continue
        drafts.sort(key=lambda d: d.get("lastModifiedDateTime") or "",
                    reverse=True)
        return drafts
    return []


def _ea_authored_draft_ids(conversation_id: str) -> list:
    """Drafts in this conversation that the EA WROTE, newest first.

    Provenance, not presence: a draft counts as ours only if draft_capture
    retained its body for this conversation or the draft-events journal
    recorded us mutating that id. Anything else is a hand-written draft, and
    overwriting one is unrecoverable, so it is left untouched.
    """
    drafts = _conversation_drafts(conversation_id)
    if not drafts:
        return []
    try:
        captured = _draft_capture.ids_for_conversation(conversation_id)
    except Exception:
        captured = set()
    try:
        journalled = _draft_events.known_draft_ids()
    except Exception:
        journalled = set()
    return [d["id"] for d in drafts
            if d.get("id") and (d["id"] in captured or d["id"] in journalled)]


def _latest_reached_sender(conv: str, message_id: str) -> bool:
    """Whether the user's latest message in the conversation went to the
    sender of the message being answered. A forward to a third person (an
    aside to a colleague) leaves the sender unanswered. Fails closed: when
    either side cannot be read, the user's latest message counts as the
    answer."""
    try:
        latest = graph.conversation_latest_message(conv) or {}
        target = graph.get_message_full(message_id) or {}
    except Exception:
        return True
    sender = (((target.get("from") or {}).get("emailAddress") or {})
              .get("address") or "").lower()
    if not sender or not latest.get("recipients") or sender in _user_addresses():
        return True
    return sender in latest["recipients"]


def cmd_create_reply(args):
    # Guard: if the user already sent the most recent message in this
    # conversation, they've handled the thread -- do NOT auto-draft a reply.
    # Fixes the "drafted a reply even though I already replied" bug: a briefing
    # snapshot surfaces the thread, but the user replies before the agent acts,
    # so the stale item must not produce a draft. A latest message that did not
    # go to the sender being answered (a forward to a colleague) is not an
    # answer, and the reply proceeds. `--force` overrides.
    if not getattr(args, "force", False):
        conv = getattr(args, "conversation_id", None)
        if not conv:
            try:
                conv = (graph.get_message_full(args.message_id) or {}).get("conversationId")
            except Exception:
                conv = None
        if conv:
            try:
                latest = graph.conversation_latest_sender(conv)
            except Exception:
                latest = None
            if (latest and latest in _user_addresses()
                    and _latest_reached_sender(conv, args.message_id)):
                _draft_events.record("skipped", "", verb="create-reply:user-already-replied")
                print(json.dumps({
                    "status": "skipped",
                    "reason": ("user already sent the latest message in this "
                               "conversation; not drafting a reply"),
                    "latest_sender": latest,
                    "override": ("when the user asked for this reply in the "
                                 "current thread, run the same create-reply "
                                 "with --force"),
                }, indent=2))
                return
    # Idempotency: one reply draft per conversation. A retry after a partial
    # failure, a double dispatch, or an orphaned pre-restart run must reuse
    # the existing draft, not mint a sibling. This is CLAUDE.md rule 5a
    # ("only create-reply once per thread") enforced in code.
    _conv_for_dedupe = getattr(args, "conversation_id", None) or (
        (graph.get_message_full(args.message_id) or {}).get("conversationId")
        if args.message_id else None)
    # Only ever REUSE a draft the EA wrote, and then the most recently
    # edited one. A conversation whose only draft is hand-written gets a
    # fresh reply draft instead; one extra draft is recoverable, an
    # overwritten hand-written one is not.
    if (_conv_for_dedupe and not getattr(args, "force", False)
            and not getattr(args, "to", None)):
        existing = _ea_authored_draft_ids(_conv_for_dedupe)
        if existing:
            eid = existing[0]
            graph.update_draft(eid, args.body,
                               getattr(args, "content_type", "Text"))
            _draft_events.record("updated", eid, verb="update-draft",
                                 ledger_ids=getattr(args, "ledger_id", None))
            _draft_capture.capture(eid, args.body, verb="update-draft",
                                   conversation_id=_conv_for_dedupe)
            print(json.dumps({
                "status": "reused_existing_draft",
                "reason": ("an EA-authored reply draft already exists in this "
                           "conversation; updated it in place instead of "
                           "creating a duplicate"),
                "id": eid,
            }, indent=2))
            return

    # Branch-off anchoring: when --to redirects a reply to a third party,
    # the quoted context should be the FULLEST version of the thread, so
    # anchor on the conversation's LATEST message rather than whichever
    # message the caller happened to find.
    if getattr(args, "to", None) and _conv_for_dedupe:
        try:
            latest = graph.graph_get("/me/messages", {
                "$filter": f"conversationId eq '{str(_conv_for_dedupe).replace(chr(39), chr(39) * 2)}'",
                "$orderby": "receivedDateTime desc", "$top": 1,
                "$select": "id,receivedDateTime"}).get("value", [])
            if latest and latest[0]["id"] != args.message_id:
                args.message_id = latest[0]["id"]
        except Exception:
            pass  # keep the caller's anchor

    cc_addrs = [a.strip() for a in args.cc.split(",")] if getattr(args, "cc", None) else None
    try:
        result = graph.create_draft(
            to=[],  # ignored for replies
            subject="",  # ignored for replies
            body=args.body,
            cc=cc_addrs,
            reply_to_id=args.message_id,
            content_type=args.content_type,
            reply_all=not args.sender_only,
            conversation_id=getattr(args, "conversation_id", None),
        )
    except graph.AmbiguousReplyTarget as e:
        print(json.dumps({"status": "refused", "reason": str(e)}, indent=2))
        return
    # --to override: redirect the reply draft to different recipients while
    # KEEPING the quoted thread. createReply fills recipients from the
    # thread, so without this the only way to redirect is a threadless
    # create-draft.
    if getattr(args, "to", None) and result.get("id"):
        graph.set_draft_recipients(
            result["id"],
            to=[a.strip() for a in args.to.split(",")],
            cc=[a.strip() for a in args.cc.split(",")] if getattr(args, "cc", None) else [])
    _draft_events.record("created", result.get("id", ""), verb="create-reply",
                         ledger_ids=getattr(args, "ledger_id", None))
    _draft_capture.capture(
        result.get("id", ""), args.body, verb="create-reply",
        conversation_id=(getattr(args, "conversation_id", None)
                         or result.get("conversationId", "")),
        subject=result.get("subject", ""),
        to=[a.get("emailAddress", {}).get("address", "")
            for a in (result.get("toRecipients") or [])])
    print(json.dumps({"status": "reply_draft_created", "id": result.get("id", "")}, indent=2))


# ---------------------------------------------------------------------------
# Message body -> readable text
# ---------------------------------------------------------------------------
# Raw HTML bodies run 30-60KB for a newsletter and buy the LLM nothing.
# Both get-message and get-conversation strip them, so the logic lives here
# rather than inline.
_HTML_DROP_RE = re.compile(r"<(style|script)[^>]*>.*?</\1>",
                           re.DOTALL | re.IGNORECASE)
# A link keeps its target beside its text, whether the href value is double
# quoted, single quoted or bare, so a card never shows link text alone.
_HTML_LINK_RE = re.compile(
    r"""<a\b[^>]*?\bhref\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))[^>]*>(.*?)</a>""",
    re.DOTALL | re.IGNORECASE)


def _link_text(m: "re.Match") -> str:
    return f"{m.group(4)} [{m.group(1) or m.group(2) or m.group(3) or ''}]"
# Outlook's quoted-history dividers. Everything after the first one is a
# message the thread already carries somewhere else, so in a conversation
# read it is pure duplication. Three shapes, because Outlook HTML and plain
# text quote differently:
#   1. the underscore rule Outlook draws above a forward
#   2. an HTML header, whose labels each land on their OWN line once the tags
#      are gone ("From:\n<name>\nSent:\n..."), so it is anchored on a
#      following Sent:/Date:/To: rather than on "From: <name>" alone
#   3. the plain-text one-liner, and Gmail's "On <date> X wrote:"
_QUOTE_SPLIT_RE = re.compile(
    r"_{10,}"
    r"|(?:^|\n)[ \t]*(?s:From:.{0,200}?\n[ \t]*(?:Sent|Date|To):)"
    r"|(?:^|\n)[ \t]*From: .+?(?:\n|$)"
    r"|(?:^|\n)[ \t]*On .{10,80} wrote:",
    re.IGNORECASE)


def _body_to_text(body: str, body_type: str) -> str:
    """HTML (or plain) message body -> line-preserving plain text."""
    body = body or ""
    if (body_type or "").lower() != "html":
        return body
    stripped = _HTML_DROP_RE.sub("", body)
    stripped = _HTML_LINK_RE.sub(_link_text, stripped)   # keep the URLs
    stripped = re.sub(r"<[^>]+>", "\n", stripped)
    stripped = html_mod.unescape(stripped)
    lines = [ln.strip() for ln in stripped.splitlines() if ln.strip()]
    return "\n".join(lines)


def _own_text(text: str) -> str:
    """Just this message's own words -- cut at the first quoted divider."""
    return _QUOTE_SPLIT_RE.split(text or "", maxsplit=1)[0].strip()


def _addrs(msg: dict, key: str) -> list:
    """Addresses out of a recipient collection, e.g. toRecipients."""
    return [(r.get("emailAddress") or {}).get("address", "")
            for r in (msg.get(key) or [])
            if (r.get("emailAddress") or {}).get("address")]


def cmd_get_conversation(args):
    """Print every message in one Outlook thread, oldest-first.

    One Graph call, in place of a search-mail plus one get-message per hit.
    """
    try:
        msgs, truncated = graph.get_conversation_messages(
            args.conversation_id, top=args.top,
            include_drafts=args.include_drafts)
    except Exception as e:
        print(json.dumps({
            "error": f"Could not fetch conversation: {e}",
            "conversation_id": args.conversation_id,
        }, indent=2))
        return
    if not msgs:
        print(json.dumps({
            "conversation_id": args.conversation_id,
            "message_count": 0,
            "messages": [],
            "hint": "No messages with that conversationId. It may be from "
                    "another mailbox, or the thread was deleted. Get a live "
                    "id from search-mail / list-mail output.",
        }, indent=2))
        return

    participants: list = []
    out_msgs = []
    for m in msgs:
        text = _body_to_text(m.get("body", {}).get("content", ""),
                             m.get("body", {}).get("contentType", ""))
        if not args.full:
            text = _own_text(text)
        sender = (m.get("from") or {}).get("emailAddress", {})
        for a in ([sender.get("address", "")]
                  + _addrs(m, "toRecipients") + _addrs(m, "ccRecipients")):
            if a and a.lower() not in [p.lower() for p in participants]:
                participants.append(a)
        out_msgs.append({
            "id": m.get("id"),
            "subject": m.get("subject"),
            "from": sender,
            "to": _addrs(m, "toRecipients"),
            "cc": _addrs(m, "ccRecipients"),
            "sent": m.get("receivedDateTime") or m.get("sentDateTime", ""),
            "is_draft": bool(m.get("isDraft")),
            "has_attachments": bool(m.get("hasAttachments")),
            "text": text[:args.max_chars],
        })
    output = {
        "conversation_id": args.conversation_id,
        "subject": msgs[-1].get("subject", ""),
        "message_count": len(out_msgs),
        "participants": participants,
        "messages": out_msgs,
    }
    if truncated:
        output["truncated"] = (
            f"More than {args.top} messages in this thread; raise --top.")
    if not args.full:
        output["_note"] = ("Quoted history stripped from each message "
                           "(the thread already carries it). Use --full to "
                           "keep it.")
    print(json.dumps(output, indent=2))


def cmd_get_message(args):
    try:
        msg = graph.get_message_full(args.message_id)
    except Exception as e:
        print(json.dumps({
            "error": f"Could not fetch message: {e}",
            "message_id": args.message_id,
        }, indent=2))
        return
    # Clean up for readability. `cc` is included so a caller that sets a CC
    # and reads the draft back does not conclude --cc failed and re-run
    # update-draft.
    output = {
        "id": msg.get("id"),
        "subject": msg.get("subject"),
        "from": msg.get("from", {}).get("emailAddress", {}),
        "to": [r.get("emailAddress", {}) for r in msg.get("toRecipients", [])],
        "cc": [r.get("emailAddress", {}) for r in msg.get("ccRecipients", [])],
        "received": msg.get("receivedDateTime"),
        "body": msg.get("body", {}).get("content", ""),
        "body_type": msg.get("body", {}).get("contentType", ""),
        "has_attachments": msg.get("hasAttachments"),
        "importance": msg.get("importance"),
        "conversation_id": msg.get("conversationId"),
    }
    # For HTML emails, extract clean text and drop the raw HTML.
    # The raw HTML (often 30-60KB for newsletters) massively inflates
    # LLM context for zero benefit. Use --raw-html when you need it.
    if output["body_type"] and output["body_type"].lower() == "html":
        text_body = _body_to_text(output["body"], output["body_type"])
        if getattr(args, 'raw_html', False):
            output["body_text"] = text_body[:5000]
        else:
            # Replace bloated HTML with clean text (saves 10-50K tokens)
            output["body"] = text_body[:5000]
            output["body_type"] = "text (extracted from html)"
    print(json.dumps(output, indent=2))


def _user_zone():
    """The user's zone as a ZoneInfo (user_tz.py)."""
    try:
        import user_tz
    except ImportError:
        from preprocessor import user_tz
    return user_tz.zone(graph.EA_DIR)


def cmd_calendar_view(args):
    """Outlook calendar events for a date range: the grounding primitive for
    'is X actually on the calendar?' checks, past the briefing's
    today/tomorrow window. Use --start/--end (ISO; naive = the user's zone) or
    --days. The my_response field (none/accepted/tentativelyAccepted/declined/
    organizer) answers 'did I accept the invite?'."""
    start = end = None
    if getattr(args, "start", None) and getattr(args, "end", None):
        try:
            mt = _user_zone()
        except Exception:
            mt = None

        def _parse(s):
            d = datetime.fromisoformat(s)
            if d.tzinfo is None and mt is not None:
                d = d.replace(tzinfo=mt)
            return d
        start, end = _parse(args.start), _parse(args.end)
        events = graph.get_calendar_view(start, end)
    else:
        events = graph.get_calendar_view(days_ahead=args.days)

    def _f(dct, *path, default=""):
        cur = dct
        for k in path:
            if not isinstance(cur, dict):
                return default
            cur = cur.get(k)
        return cur if cur is not None else default

    me = _user_addresses()
    import institution
    domain = next((a.split("@", 1)[1] for a in me if "@" in a), institution.own_suffix().lstrip("@"))

    def _others(e):
        """The people on the event besides the user: attendees (rooms and
        equipment left out) and the organizer. Raw `attendees` counts the
        user on an invite, so a 1:1 someone else sent reads as two."""
        people = set()
        for a in e.get("attendees", []) or []:
            if (a.get("type") or "").lower() == "resource":
                continue
            addr = (_f(a, "emailAddress", "address") or "").lower()
            if addr:
                people.add(addr)
        org = (_f(e, "organizer", "emailAddress", "address") or "").lower()
        if org:
            people.add(org)
        people -= me
        return people

    # Graph answers in UTC (no Prefer header is sent); `start`/`end` keep
    # that for every reader that parses them, and `start_local`/`end_local`
    # carry the same instant in the user's zone with its offset, so a
    # model never has to guess which zone an unlabelled time is in.
    try:
        _local = _user_zone()
    except Exception:
        _local = None

    def _local_iso(s: str) -> str:
        if not s or _local is None:
            return ""
        try:
            from datetime import timezone as _tz
            return datetime.fromisoformat(s[:19]).replace(tzinfo=_tz.utc).astimezone(_local).isoformat()
        except ValueError:
            return ""

    out = []
    for e in events:
        others = _others(e)
        tz_name = _f(e, "start", "timeZone") or "UTC"
        out.append({
            # The reschedule/cancel flow REQUIRES the event id
            # (pending-event-update.json carries event_id for the bot's
            # buttons). Exposing it is safe: every mutation verb is
            # confirm-token gated.
            "id": e.get("id", ""),
            "subject": e.get("subject", ""),
            "start": _f(e, "start", "dateTime")[:19],
            "end": _f(e, "end", "dateTime")[:19],
            "start_local": _local_iso(_f(e, "start", "dateTime")) if tz_name == "UTC" else "",
            "end_local": _local_iso(_f(e, "end", "dateTime")) if tz_name == "UTC" else "",
            "all_day": e.get("isAllDay", False),
            "location": _f(e, "location", "displayName"),
            "organizer": _f(e, "organizer", "emailAddress", "name"),
            "attendees": len(e.get("attendees", []) or []),
            # People besides the user, and how many are outside the user's
            # domain: the reconciler's 1:1 test and its movability level.
            "others": len(others),
            "others_external": sum(1 for a in others
                                   if not a.endswith("@" + domain)),
            "my_response": _f(e, "responseStatus", "response"),
            # Series identity + free/busy state for the calendar
            # reconciler: an occurrence of a standing series carries its
            # master id, and showAs distinguishes a real block from an
            # event marked Free.
            "series_master_id": e.get("seriesMasterId", ""),
            "event_type": e.get("type", ""),
            "show_as": e.get("showAs", ""),
            # The EA's own tentative holds, which clear-hold may release
            # without a button; everything else needs the Delete button.
            "categories": e.get("categories", []) or [],
            "ea_hold": graph.is_ea_hold(e)[0],
        })
    print(json.dumps({"count": len(out),
                      "times": "start/end are UTC wall-clock with no offset; start_local/end_local are "
                               f"{_local or 'UTC'} with the offset. Quote local times to people.",
                      "events": out}, indent=2))


def _recurrence_from_args(args, start, end):
    """Build the Graph recurrence object from the --repeat* flags.

    Returns None when no recurrence flags were given (single event, the
    behavior create-event has always had). Raises ValueError on any
    invalid combination so a half-understood flag set fails loudly
    instead of silently creating one event.
    """
    repeat = getattr(args, "repeat", None)
    days = getattr(args, "repeat_days", None)
    until = getattr(args, "repeat_until", None)
    count = getattr(args, "repeat_count", None)
    interval = getattr(args, "repeat_interval", 1)

    if not repeat:
        if days or until or count or (interval and int(interval) != 1):
            raise ValueError(
                "--repeat-days/--repeat-until/--repeat-count/--repeat-interval "
                "need --repeat daily|weekly.")
        return None

    # --end is the end of ONE occurrence, not of the series. Catching this
    # here stops a 2-hour lecture from becoming a 15-week-long block.
    if start.date() != end.date():
        raise ValueError(
            "--start and --end must be on the same day for a recurring event "
            "(they describe one occurrence). Use --repeat-until for the last "
            "date of the series.")

    return graph.build_recurrence(
        repeat=repeat,
        start=start,
        days=days,
        interval=interval,
        until=until,
        count=count,
    )


def cmd_create_event(args):
    # In the user's zone before anything reads the date (the recurrence
    # range): the card showed the staged time converted there.
    start = graph.to_user_local(datetime.fromisoformat(args.start))
    end = graph.to_user_local(datetime.fromisoformat(args.end))
    attendees = [a.strip() for a in args.attendees.split(",")] if args.attendees else None
    optional = ([a.strip() for a in args.optional_attendees.split(",") if a.strip()]
                if getattr(args, "optional_attendees", None) else None)

    # All event creation is button-gated, even personal holds with no attendees:
    # the user wants the Slack-button confirmation to be the single entry point.
    if not args.confirm_token:
        print(json.dumps({
            "error": "BLOCKED: create-event requires --confirm-token. "
                     "Write ~/.ea/state/pending-event.json and let the bot post a "
                     "confirmation block; the user clicks Create to approve.",
            "subject": args.subject,
            "attendees": attendees,
            "optional_attendees": optional,
            "start": args.start,
            "end": args.end,
        }))
        return

    # Recurrence is validated BEFORE the token is consumed: a typo in
    # --repeat-days should not burn a one-time approval token.
    try:
        recurrence = _recurrence_from_args(args, start, end)
    except ValueError as e:
        print(json.dumps({
            "error": f"BLOCKED: bad recurrence flags. {e}",
            "subject": args.subject,
        }))
        return

    entry = _validate_and_consume_token(args.confirm_token, "create_event", args.subject)
    if not entry:
        print(json.dumps({
            "error": "BLOCKED: invalid, expired, or already-used confirmation token.",
            "subject": args.subject,
        }))
        return

    confirm_pass = graph.issue_inprocess_approval("event", args.subject)
    result = graph.create_event(
        subject=args.subject,
        start=start,
        end=end,
        attendees=attendees,
        body=args.body or "",
        location=args.location or "",
        is_online=args.online,
        confirm_token=confirm_pass,
        recurrence=recurrence,
        hold=bool(getattr(args, "hold", False)),
        optional_attendees=optional,
    )
    out = {"status": "event_created", "id": result.get("id", "")}
    if recurrence:
        out["recurrence"] = recurrence
    print(json.dumps(out, indent=2))


def cmd_update_event(args):
    """Update (reschedule) an existing calendar event. Requires --confirm-token."""
    if not args.confirm_token:
        print(json.dumps({
            "error": "BLOCKED: update-event requires --confirm-token. "
                     "Post the reschedule details to Slack and wait for user approval.",
            "event_id": args.event_id,
        }))
        return

    entry = _validate_and_consume_token(args.confirm_token, "update_event", args.event_id)
    if not entry:
        print(json.dumps({
            "error": "BLOCKED: invalid, expired, or already-used confirmation token.",
            "event_id": args.event_id,
        }))
        return

    confirm_pass = graph.issue_inprocess_approval("update_event", args.event_id)
    start = graph.to_user_local(datetime.fromisoformat(args.start)) if args.start else None
    end = graph.to_user_local(datetime.fromisoformat(args.end)) if args.end else None
    attendees = [a.strip() for a in args.attendees.split(",")] if args.attendees else None
    optional = ([a.strip() for a in args.optional_attendees.split(",") if a.strip()]
                if getattr(args, "optional_attendees", None) else None)

    try:
        result = graph.update_event(
            event_id=args.event_id,
            subject=args.subject,
            start=start,
            end=end,
            attendees=attendees,
            optional_attendees=optional,
            body=args.body,
            location=args.location,
            confirm_token=confirm_pass,
        )
        if result.get("status") == "blocked":
            print(json.dumps(result, indent=2))
        else:
            print(json.dumps({"status": "event_updated", "id": result.get("id", args.event_id)}, indent=2))
    except Exception as e:
        print(json.dumps({
            "status": "failed",
            "error": str(e),
            "event_id": args.event_id,
        }, indent=2))


def cmd_delete_event(args):
    """Delete a calendar event. Requires --confirm-token."""
    if not args.confirm_token:
        print(json.dumps({
            "error": "BLOCKED: delete-event requires --confirm-token. "
                     "Post the event details to Slack and wait for user approval.",
            "event_id": args.event_id,
        }))
        return

    entry = _validate_and_consume_token(args.confirm_token, "delete_event", args.event_id)
    if not entry:
        print(json.dumps({
            "error": "BLOCKED: invalid, expired, or already-used confirmation token.",
            "event_id": args.event_id,
        }))
        return

    try:
        result = graph.delete_event(
            event_id=args.event_id,
            confirm_token=graph.issue_inprocess_approval(
                "delete_event", args.event_id),
        )
        print(json.dumps(result, indent=2))
    except Exception as e:
        print(json.dumps({
            "status": "failed",
            "error": str(e),
            "event_id": args.event_id,
        }, indent=2))


def cmd_get_event(args):
    """Retrieve a single calendar event by ID."""
    try:
        result = graph.get_event(args.event_id)
        print(json.dumps(result, indent=2))
    except Exception as e:
        print(json.dumps({"error": str(e), "event_id": args.event_id}, indent=2))


def cmd_clear_hold(args):
    """Release one EA hold on Outlook without a button. graph.clear_hold
    reads the event and deletes it only when it is tagged 'EA Hold', shown
    as tentative, has no attendees and the user organises it; anything
    else comes back refused with the failed check named."""
    try:
        print(json.dumps(graph.clear_hold(args.event_id), indent=2))
    except Exception as e:
        print(json.dumps({"status": "failed", "error": str(e),
                          "event_id": args.event_id}, indent=2))


def cmd_create_todo(args):
    # Find or create the list
    lists = graph.get_todo_lists()
    target_list = None
    for l in lists:
        if l["displayName"].lower() == args.list_name.lower():
            target_list = l
            break

    if not target_list:
        # Create the list
        target_list = graph.graph_post("/me/todo/lists", {"displayName": args.list_name})

    result = graph.create_todo_task(
        list_id=target_list["id"],
        title=args.title,
        body=args.body or "",
        due_date=args.due,
        importance=args.importance,
    )
    print(json.dumps({"status": "task_created", "id": result.get("id", "")}, indent=2))


def _pagination_hint(text: str):
    """Say "there is more" on stderr, not inside the results array.

    The rule search-mail and list-mail follow: a results array holds message
    rows, or it holds one note explaining why it holds no message rows, never
    both. A mixed array kills a caller looping over the rows, because the
    note row has none of the message keys. An envelope object
    ({"messages": [...], "note": ...}) is what get-conversation does, but not
    here: every consumer of these two verbs indexes the payload as a list
    (see log_missed_closure._normalize_cli_messages), and a dict would
    iterate as its keys and silently yield zero messages.
    """
    # Flush stdout first. stdout is block-buffered when piped and stderr is
    # not, so without this the hint overtakes the payload and a merged
    # capture reads "hint, then JSON", which makes json.loads raise
    # "Extra data" at char 0. With the hint after the payload, `raw_decode`
    # from the first bracket still works.
    sys.stdout.flush()
    print(f"  {text}", file=sys.stderr)

def cmd_search_mail(args):
    # --folder scopes the search to one folder (inbox, drafts, sentitems,
    # ... or one the user made): Graph's $search on that folder, its whole
    # history and the bodies too, not its subfolders.
    try:
        results, has_more = graph.search_messages(args.query, top=args.top,
                                                  folder=getattr(args, "folder", None))
    except LookupError as e:
        print(json.dumps({"error": str(e)}, indent=2))
        return
    output = []
    own_addrs = _user_addresses()
    for m in results:
        sender = m.get("from", {}).get("emailAddress", {}).get("address", "")
        output.append({
            "id": m.get("id"),
            "subject": m.get("subject"),
            "from": sender,
            # A draft the user wrote shows up here beside the thread's
            # messages, and "finish the draft I wrote" is a request to
            # update it, never to make a second one: the two flags say
            # which rows those are.
            "is_draft": bool(m.get("isDraft")),
            "own": sender.lower() in own_addrs if sender else False,
            "received": m.get("receivedDateTime", ""),
            "has_attachments": m.get("hasAttachments", False),
            # The join key for get-conversation. Without it, reading the
            # thread behind a hit meant a get-message first, and search-mail
            # matches loosely enough that some of those hits are not even in
            # the thread you are after.
            "conversation_id": m.get("conversationId", ""),
            "to": _addrs(m, "toRecipients"),
            "cc": _addrs(m, "ccRecipients"),
            "preview": m.get("bodyPreview", "")[:300],
        })

    # A folder-scoped miss is not evidence the mail does not exist, and a
    # bare `[]` reads as if it were. Archiving a conversation in Outlook
    # moves the user's own sent copy out of Sent Items, so the one folder a
    # reply-check looks in is the folder the reply leaves.
    if not results and getattr(args, "folder", None):
        output.append({"_note": (
            f"0 hits in folder '{args.folder}' over the last 60 days. This is "
            f"NOT evidence the message does not exist: --folder searches ONE "
            f"folder, and an archived conversation carries the user's own "
            f"sent copy out of sentitems with it. Before concluding anything "
            f"was never sent, never drafted or lost, re-run this query with "
            f"NO --folder (all folders), then get-conversation "
            f"--conversation-id <id> on the hit to read the whole thread.")})
    print(json.dumps(output, indent=2))
    if has_more:
        _pagination_hint(f"More results available. Refine --query or use "
                         f"--top {args.top * 2} to see more.")


# Folders ordered by when the user SENT the message; everything else is
# ordered by when it was delivered. Drafts are deliberately NOT here: an
# unsent draft has no meaningful sentDateTime.
_SENT_ORDER_FOLDERS = {"sentitems"}


def cmd_list_mail(args):
    """Newest-first listing of one folder, ordered server-side by Graph.

    One GET with $orderby + $top, so Graph does the sorting and the capping
    before it sends anything back. search-mail answers the same question with
    either a mailbox-wide $search or a 60-day client-side filter, both of
    which routinely run past two minutes.

    Output is the same JSON shape search-mail prints, so a caller can swap
    one verb for the other.
    """
    since = None
    if getattr(args, "since", None):
        from datetime import timezone
        try:
            d = datetime.fromisoformat(args.since)
        except ValueError:
            print(json.dumps({
                "error": f"--since is not an ISO date/datetime: {args.since!r}",
            }, indent=2))
            return
        if d.tzinfo is not None:
            d = d.astimezone(timezone.utc).replace(tzinfo=None)
        since = d.strftime("%Y-%m-%dT%H:%M:%SZ")

    order_by = ("sentDateTime" if args.folder.lower() in _SENT_ORDER_FOLDERS
                else "receivedDateTime")
    try:
        results, has_more = graph.list_folder_messages(
            args.folder, top=args.top, order_by=order_by, since=since)
    except Exception as e:
        print(json.dumps({
            "error": f"Could not list folder {args.folder!r}: {e}",
            "hint": "Folder is a well-known name (inbox, sentitems, drafts, "
                    "archive, deleteditems, junkemail), a folder's own name "
                    "(admissions-inquiries) or a folder id.",
        }, indent=2))
        return
    output = []
    own_addrs = _user_addresses()
    for m in results:
        sender = (m.get("from") or {}).get("emailAddress", {}).get("address", "")
        output.append({
            "id": m.get("id"),
            "subject": m.get("subject"),
            "from": sender,
            "is_draft": bool(m.get("isDraft")),
            "own": sender.lower() in own_addrs if sender else False,
            "received": m.get("receivedDateTime", ""),
            "has_attachments": m.get("hasAttachments", False),
            "conversation_id": m.get("conversationId", ""),
            # Recipients, because closure_scrubber's sender_reply_match
            # joins Sent mail on the recipient address. Under mail mediation
            # this verb is the only route ea-bot has to the Sent folder, so
            # without the field the signal reports "did not fire" on a join
            # key it never received.
            "to": [(r.get("emailAddress") or {}).get("address", "")
                   for r in (m.get("toRecipients") or [])],
            "cc": [(r.get("emailAddress") or {}).get("address", "")
                   for r in (m.get("ccRecipients") or [])],
            "preview": m.get("bodyPreview", "")[:300],
        })
    print(json.dumps(output, indent=2))
    if has_more:
        _pagination_hint(f"Older messages remain in {args.folder}. Use "
                         f"--top {args.top * 2} to see more.")


def cmd_get_attachments(args):
    attachments = graph.get_attachments(args.message_id)
    output = []
    for a in attachments:
        output.append({
            "id": a.get("id"),
            "name": a.get("name"),
            "content_type": a.get("contentType"),
            "size": a.get("size"),
        })
    print(json.dumps(output, indent=2))


def cmd_download_attachment(args):
    """Download a file attachment to disk.

    Pick the attachment by --attachment-id (exact) or --name-pattern
    (substring match on the attachment's filename, case-insensitive).
    Errors if zero or multiple matches, so skills can rely on
    deterministic behavior.
    """
    import base64

    attachments = graph.get_attachments(args.message_id)
    candidates = []
    if args.attachment_id:
        candidates = [a for a in attachments if a.get("id") == args.attachment_id]
    elif args.name_pattern:
        pat = args.name_pattern.lower()
        candidates = [a for a in attachments
                      if pat in (a.get("name") or "").lower()]
    else:
        print(json.dumps({
            "error": "Provide --attachment-id or --name-pattern",
        }))
        sys.exit(2)

    if not candidates:
        print(json.dumps({
            "error": "No attachment matched",
            "tried": {"attachment_id": args.attachment_id,
                      "name_pattern": args.name_pattern},
            "available": [{"name": a.get("name"), "id": a.get("id")}
                          for a in attachments],
        }))
        sys.exit(1)
    if len(candidates) > 1:
        print(json.dumps({
            "error": "Multiple attachments matched; narrow with --attachment-id",
            "matches": [{"name": a.get("name"), "id": a.get("id")}
                        for a in candidates],
        }))
        sys.exit(1)

    a = candidates[0]
    # The list endpoint doesn't include contentBytes; re-fetch the one we want
    full = graph.graph_get(f"/me/messages/{args.message_id}/attachments/{a['id']}")
    content_bytes = base64.b64decode(full["contentBytes"])
    out_path = Path(args.output).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(content_bytes)
    print(json.dumps({
        "saved": str(out_path),
        "name": a.get("name"),
        "size": len(content_bytes),
    }))


def cmd_scan_attachments(args):
    """Scan PDF attachments on a message for actionable info."""
    import pdf_utils
    hints = pdf_utils.scan_message_pdf_attachments(args.message_id, graph)
    if hints:
        print(json.dumps(hints, indent=2))
    else:
        print(json.dumps({"result": "no actionable info found in PDF attachments"}))


def cmd_mark_read(args):
    """Mark a message read in place (never moved). Ungated: only the
    user's own read flag changes."""
    print(json.dumps(graph.mark_message_read(args.message_id)))


def cmd_archive_message(args):
    """Move a message to the Archive folder. A message id is stale once the
    message has moved (Graph gives a message a new id per folder), so a 404
    is answered as {"archived": false, "gone": true}: the message is not in
    the inbox under that id, and the caller archives by conversation."""
    try:
        result = graph.graph_post(
            f"/me/messages/{args.message_id}/move",
            {"destinationId": "archive"},
        )
    except Exception as e:
        if "404" in str(e):
            print(json.dumps({"archived": False, "gone": True,
                              "message": "no message with this id: it moved or was deleted; "
                                         "archive-conversation covers what is still in the inbox"}))
            return
        raise
    print(json.dumps({"archived": True, "id": result.get("id", args.message_id)}))


def cmd_set_categories(args):
    """Set the Outlook categories on one message.

    Categories are plain strings on the message; Outlook colours them from
    the mailbox's master list, which this token cannot write
    (MailboxSettings.Read), so a new name shows uncoloured until the user
    assigns a colour once in Outlook. --replace overwrites the list; the
    default merges with what is already on the message so a tag the user
    set by hand survives.
    """
    new = [c.strip() for c in args.categories.split(",") if c.strip()]
    if args.replace:
        cats = new
    else:
        cur = graph.graph_get(
            f"/me/messages/{args.message_id}", {"$select": "categories"}
        ).get("categories") or []
        cats = list(dict.fromkeys(list(cur) + new))
    result = graph.graph_patch(f"/me/messages/{args.message_id}",
                               {"categories": cats})
    print(json.dumps({"id": result.get("id", args.message_id),
                      "categories": result.get("categories", cats)}))


def cmd_archive_conversation(args):
    """Archive every inbox message belonging to a conversation.

    Used by the 'Sent myself' button: once the user tells us they sent a
    reply from Outlook directly, we treat the thread as off their radar
    until it re-raises — move all inbox-side messages in that
    conversation to Archive.
    """
    cid = args.conversation_id
    # $filter on conversationId to find inbox messages in this thread
    resp = graph.graph_get("/me/mailFolders/inbox/messages", {
        "$filter": f"conversationId eq '{str(cid).replace(chr(39), chr(39) * 2)}'",
        "$select": "id,subject", "$top": 50})
    items = resp.get("value", [])

    archived_ids = []
    errors = []
    for m in items:
        mid = m.get("id")
        if not mid:
            continue
        try:
            graph.graph_post(f"/me/messages/{mid}/move",
                             {"destinationId": "archive"})
            archived_ids.append(mid)
        except Exception as e:
            errors.append({"id": mid, "error": str(e)})

    print(json.dumps({
        "conversation_id": cid,
        "archived_count": len(archived_ids),
        "archived_ids": archived_ids,
        "errors": errors,
    }))


def _snooze_lock(ea_dir: Path) -> Path:
    """The snooze lock, beside the snooze file in resident space. On an
    install without mail mediation the resident runs the snooze verbs
    itself, so the lock is shared and taken by _open_lock's rules."""
    return ea_dir / "state" / "snoozed-messages.lock"


def _load_snoozes(snooze_file: Path) -> list:
    """Snooze records, or [] when the file is absent. The file is read with
    the resident's access, and only dict records are kept, so a rewrite
    carries nothing but snooze records. A corrupt or unreadable file raises:
    silently starting from [] would archive-and-forget every live snooze."""
    text = _read_state_text(snooze_file)
    if text is None:
        return []
    data = json.loads(text)
    if not isinstance(data, list):
        return []
    return [e for e in data if isinstance(e, dict)]


def _save_snoozes(snooze_file: Path, snoozes: list) -> None:
    _atomic.write_json(snooze_file, snoozes)


def cmd_snooze_message(args):
    """Archive a message, set Outlook follow-up flag, and schedule resurface.

    Sets a native Outlook followUpFlag with dueDateTime so the snooze is
    visible in Outlook's UI. Also stores the snooze in a local JSON file
    so the morning briefing can move it back to inbox on the due date.
    """
    ea_dir = Path(os.environ.get("EA_DIR", Path.home() / ".ea"))
    snooze_file = ea_dir / "state" / "snoozed-messages.json"

    # Set Outlook follow-up flag with due date BEFORE archiving
    # (the message_id may change after a move operation)
    try:
        graph.graph_patch(f"/me/messages/{args.message_id}", {
            "flag": {
                "flagStatus": "flagged",
                "startDateTime": {
                    "dateTime": f"{args.until}T09:00:00",
                    "timeZone": graph._get_user_timezone(),
                },
                "dueDateTime": {
                    "dateTime": f"{args.until}T17:00:00",
                    "timeZone": graph._get_user_timezone(),
                },
            }
        })
        flag_set = True
    except Exception:
        # Flag is a nice-to-have; don't block the snooze if it fails
        flag_set = False

    # The conversation id is the only durable handle on this mail: Outlook
    # mints a NEW message id on every folder move, so the id stored below is
    # already one move away from stale. wake-snoozed re-resolves through it
    # when the stored id no longer exists.
    conv_id = ""
    try:
        conv_id = (graph.get_message_full(args.message_id) or {}).get(
            "conversationId", "") or ""
    except Exception:
        conv_id = ""

    # Archive the message from inbox
    new_msg = graph.graph_post(
        f"/me/messages/{args.message_id}/move",
        {"destinationId": "archive"},
    )
    new_id = new_msg.get("id", args.message_id)

    entry = {
        "message_id": new_id,
        "original_message_id": args.message_id,
        "conversation_id": conv_id,
        "snooze_until": args.until,
        "description": args.description,
        "ledger_id": args.ledger_id or "",
        "created": datetime.now().isoformat(),
    }

    def _append():
        snoozes = _load_snoozes(snooze_file)
        snoozes.append(entry)
        _save_snoozes(snooze_file, snoozes)

    _with_file_lock(_snooze_lock(ea_dir), _append)

    print(json.dumps({
        "snoozed": True,
        "archived_id": new_id,
        "snooze_until": args.until,
        "description": args.description,
        "outlook_flag": flag_set,
    }))


# A wake whose Graph move keeps failing is retried on every pass. Past this
# many attempts it is ESCALATED, reported in its own list on every run
# instead of hiding inside `descriptions`, but it is never dropped: the mail
# is sitting in Archive and the record is the only thing that knows it should
# come back.
MAX_WAKE_ATTEMPTS = 5


class UnsafeSnoozeId(ValueError):
    """A snooze record names an id that is not a plain mail id. It can never
    be woken, so it is escalated at once and kept."""


def _wake_one(entry: dict) -> None:
    """Move one snoozed message back to the inbox. Raises on failure.

    Re-resolves a stale message id through the conversation first: every
    folder move rewrites the id, so the id stored at snooze time is dead the
    moment anything else touches the mail.
    """
    try:
        import mail_mediation as _mm
    except ModuleNotFoundError:  # imported as preprocessor.<mod>
        from preprocessor import mail_mediation as _mm
    # The record is in resident space (snoozed-messages.json), and its ids go
    # into Graph paths and a conversation filter run with the owner's token:
    # each must be a plain mail id, the mediator's own test, before any use.
    msg_id = entry.get("message_id") or ""
    conv_id = entry.get("conversation_id") or ""
    if not isinstance(conv_id, str):
        conv_id = ""
    conv_id = conv_id.strip()
    if not _mm.is_plain_id(msg_id):
        raise UnsafeSnoozeId(f"message id {str(msg_id)[:24]!r} is not a plain mail id")
    if conv_id and not _mm.is_plain_id(conv_id):
        raise UnsafeSnoozeId(f"conversation id {conv_id[:24]!r} is not a plain mail id")

    def _move(mid: str) -> None:
        if not _mm.is_plain_id(mid):
            raise UnsafeSnoozeId(f"message id {str(mid)[:24]!r} is not a plain mail id")
        try:
            graph.graph_patch(f"/me/messages/{mid}",
                              {"flag": {"flagStatus": "notFlagged"}})
        except Exception:
            pass  # Flag clear is best-effort
        graph.graph_post(f"/me/messages/{mid}/move",
                         {"destinationId": "inbox"})

    try:
        _move(msg_id)
        return
    except Exception:
        if not conv_id:
            raise
    live_id = graph._resolve_latest_in_conversation(conv_id)
    if not live_id or live_id == msg_id:
        raise RuntimeError(
            f"message {msg_id[:24]}… is gone and conversation "
            f"{conv_id[:24]}… did not re-resolve")
    _move(live_id)
    entry["message_id"] = live_id


def wake_snoozed_emails() -> dict:
    """Move snoozed messages back to inbox if their snooze date has arrived.

    Module function for use by preprocess.py and cmd_wake_snoozed.
    Returns {"woken": N, "descriptions": [...], "remaining": N} plus
    "failed"/"escalated" when a due entry could not be moved.

    A failed move KEEPS the record: dropping it would delete the obligation
    while its mail stays in Archive.
    """
    from datetime import date

    ea_dir = Path(os.environ.get("EA_DIR", Path.home() / ".ea"))
    snooze_file = ea_dir / "state" / "snoozed-messages.json"

    if not snooze_file.exists():
        return {"woken": 0, "descriptions": [], "remaining": 0}

    today = date.today()
    woken: list = []
    failed: list = []
    escalated: list = []

    def _pass() -> list:
        snoozes = _load_snoozes(snooze_file)
        remaining = []
        for entry in snoozes:
            desc = entry.get("description") or "(no description)"
            raw_until = entry.get("snooze_until") or ""
            try:
                due = date.fromisoformat(raw_until) <= today
            except (TypeError, ValueError):
                # A malformed date escalates this one entry; it must not
                # abort the pass and leave every other due snooze archived.
                entry["wake_error"] = f"unreadable snooze_until {raw_until!r}"
                escalated.append(f"{desc} ({entry['wake_error']})")
                remaining.append(entry)
                continue
            if not due:
                remaining.append(entry)
                continue
            try:
                _wake_one(entry)
            except UnsafeSnoozeId as e:
                entry["wake_error"] = str(e)[:200]
                escalated.append(f"{desc} ({entry['wake_error']})")
                remaining.append(entry)          # never dropped
                continue
            except Exception as e:
                attempts = int(entry.get("wake_attempts") or 0) + 1
                entry["wake_attempts"] = attempts
                entry["wake_error"] = str(e)[:200]
                remaining.append(entry)          # never dropped
                label = (f"{desc} (move failed {attempts}/"
                         f"{MAX_WAKE_ATTEMPTS}: {entry['wake_error']})")
                if attempts >= MAX_WAKE_ATTEMPTS:
                    escalated.append(label)
                else:
                    failed.append(label)
                continue
            entry.pop("wake_attempts", None)
            entry.pop("wake_error", None)
            woken.append(desc)
        _save_snoozes(snooze_file, remaining)
        return remaining

    remaining = _with_file_lock(_snooze_lock(ea_dir), _pass)

    out = {"woken": len(woken), "descriptions": woken,
           "remaining": len(remaining)}
    if failed:
        out["failed"] = failed
    if escalated:
        # Loud on purpose: these have outlived the retry budget (or can never
        # come due) and need a human, not another silent pass.
        out["escalated"] = escalated
    return out


def cmd_wake_snoozed(args):
    """CLI wrapper for wake_snoozed_emails."""
    result = wake_snoozed_emails()
    print(json.dumps(result))


def cmd_update_draft(args):
    """Update the body (and optionally To/CC) of an existing draft."""
    result = graph.update_draft(args.message_id, args.body, args.content_type)
    if getattr(args, "to", None) or getattr(args, "cc", None):
        graph.set_draft_recipients(
            args.message_id,
            to=[a.strip() for a in args.to.split(",")] if getattr(args, "to", None) else None,
            cc=[a.strip() for a in args.cc.split(",")] if getattr(args, "cc", None) else None)
    _draft_events.record("updated", args.message_id, verb="update-draft",
                         ledger_ids=getattr(args, "ledger_id", None))
    # Overwrites the prior capture: the baseline for the style diff is the
    # last thing the EA wrote, not its first attempt.
    _draft_capture.capture(args.message_id, args.body, verb="update-draft",
                           conversation_id=(result or {}).get(
                               "conversationId", ""))
    print(json.dumps({"status": "updated", "message_id": args.message_id}))


def _not_a_draft(message_id: str) -> str:
    """'' when `message_id` is a draft, else why a draft-only verb refuses
    it. The draft verbs change drafts, which are disposable; received and
    sent mail is never theirs to touch."""
    try:
        msg = graph.graph_get(f"/me/messages/{message_id}", {"$select": "isDraft"})
    except Exception as e:
        return f"could not read the message: {str(e)[:200]}"
    if not (msg or {}).get("isDraft"):
        return "not a draft: this verb changes drafts only"
    return ""


def cmd_delete_draft(args):
    """Delete a draft message (no confirmation needed — drafts are disposable)."""
    why = _not_a_draft(args.message_id)
    if why:
        print(json.dumps({"status": "refused", "error": why, "message_id": args.message_id}))
        return
    graph.delete_draft(args.message_id)
    _draft_events.record("deleted", args.message_id, verb="delete-draft")
    # A discarded draft was never sent, so there is nothing to diff.
    _draft_capture.drop(args.message_id)
    print(json.dumps({"status": "deleted", "message_id": args.message_id}))


def cmd_request_confirm(args):
    """Request a confirmation token for a destructive action.

    This is called by the bot's Python code (not by Claude directly).
    The token is returned and stored in pending-confirmations.json.
    The bot posts it to Slack; the user approves; the bot passes the
    token to the gated command (send-draft, etc.).
    """
    fingerprint = getattr(args, "fingerprint", "") or ""
    if args.action == "send" and not fingerprint:
        print(json.dumps({"error": "a send token needs --fingerprint, the content "
                                   "fingerprint of the draft the owner reviewed"}))
        return
    token = _create_confirmation(args.action, args.message_id, args.description,
                                 fingerprint=fingerprint)
    print(json.dumps({
        "token": token,
        "action": args.action,
        "message_id": args.message_id,
        "description": args.description,
        "fingerprint": fingerprint,
        "expires_minutes": TOKEN_TTL_MINUTES,
    }, indent=2))


def cmd_preflight_check(args):
    """Run pre-send validation on a draft without sending it."""
    try:
        result = graph.preflight_check(args.message_id)
    except Exception as e:
        result = {
            "ok": False,
            "errors": [f"Could not fetch draft for preflight: {e}"],
            "warnings": [],
            "message_id": args.message_id,
        }
    print(json.dumps(result, indent=2))


def cmd_draft_snapshot(args):
    """What a Send card shows and the fingerprint of it, from one read.

    The bot renders a card from this and stores the fingerprint in its
    owner-private registry, so the content the owner reviews and the
    content the fingerprint covers come from the same read. The fields are
    the get-message ones plus bcc, the attachment list and the fingerprint
    (draft_fingerprint.py)."""
    try:
        fingerprint, msg, atts = _dfp.outlook_fingerprint(args.message_id, graph)
    except Exception as e:
        print(json.dumps({"error": f"Could not fetch draft: {e}",
                          "message_id": args.message_id}, indent=2))
        return

    def _rcpts(key):
        return [r.get("emailAddress", {}) for r in msg.get(key, []) or []]

    body = msg.get("body", {}) or {}
    output = {
        "id": msg.get("id"),
        "subject": msg.get("subject"),
        "to": _rcpts("toRecipients"),
        "cc": _rcpts("ccRecipients"),
        "bcc": _rcpts("bccRecipients"),
        "body": body.get("content", ""),
        "body_type": body.get("contentType", ""),
        "has_attachments": msg.get("hasAttachments"),
        "is_draft": msg.get("isDraft"),
        "conversation_id": msg.get("conversationId"),
        "attachments": [{"id": a.get("id"), "name": a.get("name"),
                         "content_type": a.get("contentType"), "size": a.get("size")}
                        for a in atts],
        "fingerprint": fingerprint,
        "reply_to": _rcpts("replyTo"),
        "remote_resources": [],
    }
    # The same text extraction as get-message, so a card renders the same,
    # but never cut: the bot compares the whole text with what its card can
    # show, and a card that cannot show all of it offers no Send button. The
    # extract drops tag attributes, so the resources the HTML loads when it
    # is opened (remote images, stylesheets) are listed beside it.
    if output["body_type"] and output["body_type"].lower() == "html":
        output["remote_resources"] = _mail_links.remote_resources(output["body"])
        output["body"] = _body_to_text(output["body"], output["body_type"])
        output["body_type"] = "text (extracted from html)"
    # The authored part and the quoted thread, so the two together are
    # exactly the text: the card shows the first in full and the second
    # collapsed. The quoted divider is text the model can write, so the
    # collapsed part is only text that ends an earlier message of this
    # thread, as this read finds it; without that, all of it is shown.
    text = output["body"] or ""
    m = _QUOTE_SPLIT_RE.search(text)
    cut = len(text)
    if m:
        cut = _mail_links.verified_quote_start(
            text, m.start(), _thread_texts(msg.get("conversationId") or "", msg.get("id")))
    output["body_own"] = text[:cut]
    output["body_quoted"] = text[cut:]
    print(json.dumps(output, indent=2))


def _thread_texts(conversation_id: str, draft_id) -> list:
    """The text of every sent or received message in a conversation, as
    get-message extracts it: what a draft's quoted part must match before
    a card collapses it. [] when the thread cannot be read."""
    if not conversation_id:
        return []
    try:
        msgs, _truncated = graph.get_conversation_messages(conversation_id, top=50)
    except Exception:
        return []
    out = []
    for m in msgs or []:
        if m.get("isDraft") or m.get("id") == draft_id:
            continue
        body = m.get("body") or {}
        out.append(_body_to_text(body.get("content", ""), body.get("contentType", "")))
    return out


def cmd_send_draft(args):
    """Send an existing draft message. Requires a valid confirmation token."""
    if not args.confirm_token:
        print(json.dumps({
            "error": "BLOCKED: send-draft requires --confirm-token. "
                     "Post the draft preview to Slack and wait for user approval. "
                     "The bot will provide the token when the user says 'send'.",
            "message_id": args.message_id,
        }))
        return

    entry = _validate_and_consume_token(args.confirm_token, "send", args.message_id)
    if not entry:
        print(json.dumps({
            "error": "BLOCKED: invalid, expired, or already-used confirmation token. "
                     "Request a new token via request-confirm.",
            "message_id": args.message_id,
        }))
        return
    fingerprint = entry.get("fingerprint") or ""
    if not fingerprint:
        print(json.dumps({
            "error": "BLOCKED: the confirmation token names no reviewed content "
                     "(fingerprint). The owner approves the content on a new card.",
            "message_id": args.message_id,
        }))
        return

    # Satisfy graph_client's defense-in-depth gate. The CLI layer validated
    # and consumed the real token above, so it mints a process-local approval
    # bound to this message id and the reviewed content; nothing on disk can
    # forge it. send_draft reads the draft again and sends only when its
    # content still has that fingerprint.
    try:
        result = graph.send_draft(
            args.message_id,
            confirm_token=graph.issue_inprocess_approval("send", args.message_id,
                                                         fingerprint),
            fingerprint=fingerprint)
    except Exception as e:
        print(json.dumps({
            "status": "failed",
            "error": str(e),
            "message_id": args.message_id,
        }, indent=2))
        return
    output = {"message_id": args.message_id, "confirmed_by": "user_approval"}
    output.update(result)
    print(json.dumps(output, indent=2))


def cmd_get_gmail_draft(args):
    """Fetch a Gmail draft's details for the confirmation block."""
    try:
        from gmail_client import get_draft
        result = get_draft(args.email, args.draft_id,
                           with_fingerprint=getattr(args, "with_fingerprint", False))
        print(json.dumps(result, indent=2))
    except Exception as e:
        print(json.dumps({"error": str(e)}))


def cmd_send_gmail_draft(args):
    """Send a Gmail draft. Requires a valid confirmation token."""
    if not args.confirm_token:
        print(json.dumps({
            "error": "BLOCKED: send-gmail-draft requires --confirm-token. "
                     "Post the draft preview to Slack and wait for user approval.",
            "draft_id": args.draft_id,
        }))
        return

    entry = _validate_and_consume_token(args.confirm_token, "send", args.draft_id)
    if not entry:
        print(json.dumps({
            "error": "BLOCKED: invalid, expired, or already-used confirmation token.",
            "draft_id": args.draft_id,
        }))
        return
    fingerprint = entry.get("fingerprint") or ""
    if not fingerprint:
        print(json.dumps({
            "error": "BLOCKED: the confirmation token names no reviewed content "
                     "(fingerprint). The owner approves the content on a new card.",
            "draft_id": args.draft_id,
        }))
        return

    try:
        from gmail_client import send_draft
        result = send_draft(args.email, args.draft_id, fingerprint=fingerprint)
        output = {"draft_id": args.draft_id, "confirmed_by": "user_approval"}
        output.update(result)
        print(json.dumps(output, indent=2))
    except Exception as e:
        print(json.dumps({
            "status": "failed",
            "error": str(e),
            "draft_id": args.draft_id,
        }, indent=2))


def cmd_attach_file(args):
    """Attach a file to an existing draft message."""
    import base64
    import requests as req

    why = _not_a_draft(args.message_id)
    if why:
        print(json.dumps({"error": why, "message_id": args.message_id}))
        return
    filepath = Path(args.file).expanduser().resolve()
    if not filepath.exists():
        print(json.dumps({"error": f"File not found: {filepath}"}))
        return

    with open(filepath, "rb") as f:
        content_bytes = base64.b64encode(f.read()).decode("utf-8")

    filename = args.name or filepath.name
    url = graph._check_endpoint(f"/me/messages/{args.message_id}/attachments")
    # Graph v1.0 expects the lower-case #microsoft.graph.fileAttachment
    # OData type; the old #Microsoft.OutlookServices.FileAttachment is
    # only valid on the legacy Outlook REST endpoint and returns 400
    # ("Invalid OData type specified") on Graph.
    on_graph = graph._api_base() != graph.OUTLOOK_REST_URL
    body = {
        "@odata.type": (
            "#microsoft.graph.fileAttachment" if on_graph
            else "#Microsoft.OutlookServices.FileAttachment"
        ),
        "name": filename,
        "contentType": args.content_type,
        "contentBytes": content_bytes,
    }
    if not on_graph:
        body = graph._pascalize(body)
        body["@odata.type"] = "#Microsoft.OutlookServices.FileAttachment"

    resp = req.post(url, headers=graph._headers(), json=body)
    if resp.ok:
        # A changed draft: the bot re-renders its live Send card.
        _draft_events.record("updated", args.message_id, verb="attach-file")
        print(json.dumps({"attached": True, "filename": filename}))
    else:
        print(json.dumps({"error": resp.text[:500], "status": resp.status_code}))


def cmd_remove_attachment(args):
    """Remove one attachment from a draft, named by --name (the file name,
    any case) or --attachment-id (from get-attachments). Swapping a file
    is remove-attachment, then attach-file."""
    why = _not_a_draft(args.message_id)
    if why:
        print(json.dumps({"error": why, "message_id": args.message_id}))
        return
    atts = graph.get_attachments(args.message_id)
    if args.attachment_id:
        hits = [a for a in atts if a.get("id") == args.attachment_id]
    else:
        hits = [a for a in atts if (a.get("name") or "").lower() == args.name.lower()]
    if not hits:
        print(json.dumps({"error": "no attachment by that name or id on this draft",
                          "attachments": [a.get("name") for a in atts]}))
        return
    if len(hits) > 1:
        print(json.dumps({"error": "more than one attachment has that name; pass --attachment-id",
                          "matches": [{"id": a.get("id"), "name": a.get("name"),
                                       "size": a.get("size")} for a in hits]}))
        return
    graph.graph_delete(f"/me/messages/{args.message_id}/attachments/{hits[0]['id']}")
    _draft_events.record("updated", args.message_id, verb="remove-attachment")
    print(json.dumps({"status": "removed", "removed": hits[0].get("name"),
                      "remaining": [a.get("name") for a in atts if a.get("id") != hits[0].get("id")]}))


def cmd_schedule_send(args):
    """Schedule a draft to send at a future date/time. Requires --confirm-token.

    Called by ea-chat-bot's _execute_schedule_send after the owner clicks
    :lock: Schedule, and by nothing else. The LLM stages the request as
    ~/.ea/state/pending-schedule-send.json and cannot obtain a token (see
    scripts/disallowed-tools.txt and mail_mediation.GATED).

    The token is validated and consumed here (proving the user approved).
    The draft is read again and must still have the content fingerprint the
    token carries. The queue entry then carries an HMAC over (draft_id,
    send_after, fingerprint), signed with an owner-only key, because the
    one-time token expires in 30 minutes while the send may fire days later.
    The signature is what the owner cron verifies: the queue file itself is
    writable by ea-bot and proves nothing.
    """
    token = getattr(args, "confirm_token", None)
    if not token:
        print(json.dumps({"status": "blocked", "error": "schedule-send requires --confirm-token"}))
        return

    # Everything that can fail without user involvement is checked BEFORE the
    # one-time token is consumed, so a missing key does not burn an approval.
    try:
        datetime.fromisoformat(args.send_after)
    except (TypeError, ValueError):
        print(json.dumps({"status": "blocked",
                          "error": f"--send-after {args.send_after!r} is not an "
                                   f"ISO datetime (e.g. 2026-04-21T09:00)"}))
        return
    import scheduled_sends as _sched
    try:
        _sched._load_key()
    except _sched.ApprovalKeyError as e:
        print(json.dumps({"status": "blocked",
                          "error": f"cannot sign the scheduled send: {e}"}))
        return

    entry = _validate_and_consume_token(token, "send", args.draft_id)
    if not entry:
        print(json.dumps({"status": "blocked", "error": "Invalid or expired confirmation token"}))
        return
    fingerprint = entry.get("fingerprint") or ""
    if not fingerprint:
        print(json.dumps({"status": "blocked",
                          "error": "the confirmation token names no reviewed content "
                                   "(fingerprint)"}))
        return
    try:
        current = graph.draft_fingerprint(args.draft_id)
    except Exception as e:
        print(json.dumps({"status": "blocked",
                          "error": f"could not read the draft to check its content: {e}"}))
        return
    if current != fingerprint:
        print(json.dumps({"status": "changed",
                          "error": "The draft changed after it was approved. Nothing "
                                   "was scheduled.",
                          "fingerprint": current}))
        return
    try:
        approval = _sched.make_send_token(args.draft_id, args.send_after, fingerprint)
    except _sched.ApprovalKeyError as e:
        print(json.dumps({"status": "blocked",
                          "error": f"cannot sign the scheduled send: {e}"}))
        return

    import subprocess
    cmd = [
        sys.executable,
        str(Path(__file__).parent / "scheduled_sends.py"),
        "add",
        "--draft-id", args.draft_id,
        "--send-after", args.send_after,
        "--description", args.description,
        f"--fingerprint={fingerprint}",
        f"--confirm-token={approval}",
    ]
    if args.ledger_id:
        cmd.extend(["--ledger-id", args.ledger_id])
    result = subprocess.run(cmd, capture_output=True, text=True)
    print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)


def _resolve_body(args):
    """Resolve --body from file or stdin to avoid shell escaping issues.

    Priority: --body-file > --body="-" (stdin) > --body "text"
    """
    if hasattr(args, "body_file") and args.body_file:
        with open(args.body_file) as f:
            args.body = f.read()
    elif hasattr(args, "body") and args.body == "-":
        args.body = sys.stdin.read()

    # Guard: reject empty body for draft/reply commands (prevents empty emails)
    cmd = getattr(args, "func", None)
    if cmd in (cmd_create_draft, cmd_create_reply, cmd_update_draft):
        body = getattr(args, "body", "") or ""
        if not body.strip() or body.strip() == "x":
            print(json.dumps({"error": "Empty body — write the body to reply-body.txt "
                               "first (Read then Write if file exists), then pass "
                               "--body-file. Do NOT send --body x alone."}),
                  file=sys.stderr)
            sys.exit(1)

    return args


def cmd_resolve_contact(args):
    """Resolve a person's name to their email via the People API."""
    import requests
    token = graph.authenticate()
    resp = requests.get("https://graph.microsoft.com/v1.0/me/people",
        params={"$search": f'"{args.name}"', "$top": args.top},
        headers={"Authorization": f"Bearer {token}"})
    if not resp.ok:
        print(json.dumps({"error": f"People API failed: {resp.status_code}"}))
        return
    results = []
    for p in resp.json().get("value", []):
        name = p.get("displayName", "")
        emails = [e.get("address", "") for e in p.get("scoredEmailAddresses", [])]
        if emails:
            results.append({"name": name, "email": emails[0]})
    print(json.dumps({"results": results}, indent=2))


def build_parser() -> argparse.ArgumentParser:
    """The CLI's argument parser, also what the mediator's argv tripwire
    parses with (tests/test_mediator_argv_tripwire.py)."""
    parser = argparse.ArgumentParser(description="Graph API actions for EA", allow_abbrev=False)
    sub = parser.add_subparsers(dest="command", required=True)

    # create-draft
    p = sub.add_parser("create-draft", allow_abbrev=False)
    p.add_argument("--to", required=True, help="Comma-separated recipients")
    p.add_argument("--cc", default=None, help="Comma-separated CC recipients")
    p.add_argument("--subject", required=True)
    p.add_argument("--body", required=True)
    p.add_argument("--body-file", default=None, help="Read body from file (overrides --body)")
    p.add_argument("--content-type", default="Text", choices=["Text", "HTML"])
    p.add_argument("--force-new-thread", action="store_true",
                   help="Allow a Re:/Fwd: subject on a genuinely NEW thread. "
                        "Never use this to answer an existing conversation: "
                        "that is create-reply, which also checks whether the "
                        "user already replied.")
    p.add_argument("--ledger-id", action="append", default=[],
                   help="Ledger row this draft delivers (repeatable, or "
                        "comma-separated). Sending the draft closes exactly "
                        "these rows; with none named, a send closes only a "
                        "reply row on its thread.")
    p.set_defaults(func=cmd_create_draft)

    # create-reply
    p = sub.add_parser("create-reply", allow_abbrev=False)
    p.add_argument("--message-id", required=True)
    p.add_argument("--body", required=True)
    p.add_argument("--to", default=None,
                   help="OVERRIDE recipients (comma-separated): the reply "
                        "keeps the quoted thread but goes to these addresses "
                        "instead of the thread's participants")
    p.add_argument("--body-file", default=None, help="Read body from file (overrides --body)")
    p.add_argument("--cc", default=None, help="Comma-separated CC recipients")
    p.add_argument("--content-type", default="Text", choices=["Text", "HTML"])
    p.add_argument("--sender-only", action="store_true",
                   help="Reply only to sender (default: reply-all)")
    p.add_argument("--conversation-id", default=None,
                   help="Conversation id (from the ledger). If the message-id "
                        "is stale (source email archived/moved -> 404), the "
                        "reply self-heals by re-resolving the live message in "
                        "this conversation instead of dropping the thread.")
    p.add_argument("--force", action="store_true",
                   help="Draft even if the user already sent the latest message in the thread.")
    p.add_argument("--ledger-id", action="append", default=[],
                   help="Ledger row this draft delivers (repeatable, or "
                        "comma-separated). Sending the draft closes exactly "
                        "these rows; with none named, a send closes only a "
                        "reply row on its thread.")
    p.set_defaults(func=cmd_create_reply)

    # update-draft (update body of existing draft, no new draft created)
    p = sub.add_parser("update-draft", allow_abbrev=False)
    p.add_argument("--message-id", required=True)
    p.add_argument("--body", required=True)
    p.add_argument("--to", default=None,
                   help="Replace the draft's To line (comma-separated)")
    p.add_argument("--cc", default=None,
                   help="Replace the draft's CC line (comma-separated)")
    p.add_argument("--body-file", default=None, help="Read body from file (overrides --body)")
    p.add_argument("--content-type", default="Text", choices=["Text", "HTML"])
    p.add_argument("--ledger-id", action="append", default=[],
                   help="Ledger row this draft delivers (repeatable, or "
                        "comma-separated). Sending the draft closes exactly "
                        "these rows; with none named, a send closes only a "
                        "reply row on its thread.")
    p.set_defaults(func=cmd_update_draft)

    # delete-draft
    p = sub.add_parser("delete-draft", allow_abbrev=False)
    p.add_argument("--message-id", required=True)
    p.set_defaults(func=cmd_delete_draft)

    # preflight-check
    p = sub.add_parser("preflight-check", allow_abbrev=False)
    p.add_argument("--message-id", required=True)
    p.set_defaults(func=cmd_preflight_check)

    # request-confirm (bot issues tokens for destructive actions)
    p = sub.add_parser("request-confirm", allow_abbrev=False)
    p.add_argument("--action", required=True,
                   choices=["send", "create_event", "update_event", "delete_event",
                            "create_google_event", "update_google_event",
                            "delete_google_event"])
    p.add_argument("--message-id", required=True, help="Draft or event ID")
    p.add_argument("--description", default="", help="Human label for Slack")
    p.add_argument("--fingerprint", default="",
                   help="send: the content fingerprint of the reviewed draft")
    p.set_defaults(func=cmd_request_confirm)

    # draft-snapshot (the bot's card read: the content and its fingerprint)
    p = sub.add_parser("draft-snapshot", allow_abbrev=False)
    p.add_argument("--message-id", required=True)
    p.set_defaults(func=cmd_draft_snapshot)

    # send-draft (requires confirmation token)
    p = sub.add_parser("send-draft", allow_abbrev=False)
    p.add_argument("--message-id", required=True)
    p.add_argument("--confirm-token", default=None, help="One-time approval token")
    p.set_defaults(func=cmd_send_draft)

    # get-message
    p = sub.add_parser("get-message", allow_abbrev=False)
    p.add_argument("--message-id", required=True)
    p.add_argument("--raw-html", action="store_true",
                   help="Include raw HTML body (default: text-extracted)")
    p.set_defaults(func=cmd_get_message)

    # get-conversation — the whole thread in ONE call
    p = sub.add_parser(
        "get-conversation",
        help="Read every message in one thread, oldest-first. Use this "
             "instead of search-mail + one get-message per hit. The "
             "conversation id is on every search-mail / list-mail row.",
        allow_abbrev=False)
    p.add_argument("--conversation-id", required=True,
                   help="conversationId from search-mail, list-mail, "
                        "get-message, or the ledger")
    p.add_argument("--top", type=int, default=50,
                   help="Max messages to fetch (default 50)")
    p.add_argument("--max-chars", type=int, default=2000,
                   help="Max characters of text per message (default 2000)")
    p.add_argument("--full", action="store_true",
                   help="Keep quoted history (default: strip it, since the "
                        "thread already carries the earlier messages)")
    p.add_argument("--include-drafts", action="store_true",
                   help="Include unsent drafts in the thread")
    p.set_defaults(func=cmd_get_conversation)

    # resolve-contact — find email by name via People API
    p = sub.add_parser("resolve-contact", help="Look up a person's email by name", allow_abbrev=False)
    p.add_argument("--name", required=True, help="Person's name to search for")
    p.add_argument("--top", type=int, default=3, help="Max results")
    p.set_defaults(func=cmd_resolve_contact)

    # create-event (requires --confirm-token when attendees are specified)
    p = sub.add_parser("create-event", allow_abbrev=False)
    p.add_argument("--subject", required=True)
    p.add_argument("--start", required=True, help="ISO datetime")
    p.add_argument("--end", required=True, help="ISO datetime")
    p.add_argument("--attendees", default=None, help="Comma-separated emails")
    p.add_argument("--optional-attendees", default=None,
                   help="Comma-separated emails invited as optional attendees")
    p.add_argument("--body", default="")
    p.add_argument("--location", default="")
    p.add_argument("--online", action="store_true")
    p.add_argument("--hold", action="store_true",
                   help="An EA hold: shown as tentative and tagged 'EA Hold', so "
                        "clear-hold can release it later without a button. No attendees.")
    p.add_argument("--confirm-token", default=None, help="Required when attendees specified")
    # Recurrence (optional). With no --repeat flag this stays a single event.
    # With --repeat, --start/--end describe ONE occurrence and the series end
    # comes from --repeat-until or --repeat-count.
    p.add_argument("--repeat", default=None, choices=["daily", "weekly"],
                   help="Create a recurring series instead of one event")
    p.add_argument("--repeat-days", default=None,
                   help="Weekly only: days as MO,TU,WE,TH,FR,SA,SU (e.g. TU,TH)")
    p.add_argument("--repeat-interval", type=int, default=1,
                   help="Every N weeks/days (default 1)")
    p.add_argument("--repeat-until", default=None,
                   help="Last date of the series, YYYY-MM-DD")
    p.add_argument("--repeat-count", type=int, default=None,
                   help="Number of occurrences (alternative to --repeat-until)")
    p.set_defaults(func=cmd_create_event)

    # update-event (reschedule — requires --confirm-token)
    p = sub.add_parser("update-event", allow_abbrev=False)
    p.add_argument("--event-id", required=True, help="Outlook event ID to update")
    p.add_argument("--subject", default=None)
    p.add_argument("--start", default=None, help="New start ISO datetime")
    p.add_argument("--end", default=None, help="New end ISO datetime")
    p.add_argument("--attendees", default=None, help="Comma-separated emails (replaces existing)")
    p.add_argument("--optional-attendees", default=None,
                   help="Comma-separated emails to make optional; an attendee already "
                        "optional stays optional")
    p.add_argument("--body", default=None)
    p.add_argument("--location", default=None)
    p.add_argument("--confirm-token", default=None, help="Required for reschedule")
    p.set_defaults(func=cmd_update_event)

    # delete-event (requires --confirm-token)
    p = sub.add_parser("delete-event", allow_abbrev=False)
    p.add_argument("--event-id", required=True, help="Outlook event ID to delete")
    p.add_argument("--confirm-token", default=None, help="Required for deletion")
    p.set_defaults(func=cmd_delete_event)

    # get-event
    p = sub.add_parser("get-event", allow_abbrev=False)
    p.add_argument("--event-id", required=True, help="Outlook event ID")
    p.set_defaults(func=cmd_get_event)

    # clear-hold: delete ONE EA hold (tagged, tentative, attendee-less,
    # user-organised) without a token; everything else is refused.
    p = sub.add_parser("clear-hold",
                       help="Release an Outlook hold the EA placed (tagged 'EA Hold', "
                            "tentative, no attendees). Refuses anything else.",
                       allow_abbrev=False)
    p.add_argument("--event-id", required=True, help="Outlook event ID of the hold")
    p.set_defaults(func=cmd_clear_hold)

    # calendar-view (Outlook date-range event read -- grounding for
    # "is X on the calendar?" checks; see cmd_calendar_view docstring)
    p = sub.add_parser(
        "calendar-view",
        help="List Outlook events for a date range (grounding for calendar "
             "checks). Use --start/--end ISO or --days.",
        allow_abbrev=False)
    p.add_argument("--start", default=None,
                   help="ISO start (naive = the user's zone), e.g. 2026-07-31")
    p.add_argument("--end", default=None, help="ISO end, e.g. 2026-08-01")
    p.add_argument("--days", type=int, default=14,
                   help="Days ahead if --start/--end omitted")
    p.set_defaults(func=cmd_calendar_view)

    # create-todo
    p = sub.add_parser("create-todo", allow_abbrev=False)
    p.add_argument("--list-name", default="EA Obligations")
    p.add_argument("--title", required=True)
    p.add_argument("--body", default="")
    p.add_argument("--due", default=None, help="Due date ISO format")
    p.add_argument("--importance", default="normal", choices=["low", "normal", "high"])
    p.set_defaults(func=cmd_create_todo)

    # search-mail
    p = sub.add_parser("search-mail", allow_abbrev=False)
    p.add_argument("--query", required=True)
    p.add_argument("--top", type=int, default=10)
    p.add_argument("--folder", default=None,
                   help="Scope to one folder (inbox, drafts, sentitems, ..., "
                        "or a folder's own name such as admissions-inquiries): "
                        "its whole history, bodies included, not its subfolders. "
                        "To just see the newest messages in a folder, use "
                        "list-mail instead.")
    p.set_defaults(func=cmd_search_mail)

    # list-mail (no query needed -- Graph does the ordering and the capping)
    p = sub.add_parser(
        "list-mail",
        help="List a folder newest-first, server-side ($orderby + $top). Use "
             "this instead of search-mail when you have no search terms.",
        allow_abbrev=False)
    p.add_argument("--folder", required=True,
                   help="Folder to list (inbox, sentitems, drafts, archive, ..., "
                        "or a folder's own name such as admissions-inquiries)")
    p.add_argument("--top", type=int, default=25,
                   help="Max messages to return (passed to Graph as $top)")
    p.add_argument("--since", default=None,
                   help="Only messages at/after this ISO date (UTC), "
                        "e.g. 2026-07-01")
    p.set_defaults(func=cmd_list_mail)

    # get-attachments
    p = sub.add_parser("get-attachments", allow_abbrev=False)
    p.add_argument("--message-id", required=True)
    p.set_defaults(func=cmd_get_attachments)

    # download-attachment
    p = sub.add_parser("download-attachment",
                       help="Download a file attachment to disk",
                       allow_abbrev=False)
    p.add_argument("--message-id", required=True)
    p.add_argument("--output", required=True,
                   help="Path to write the file to")
    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument("--attachment-id",
                     help="Exact attachment ID from get-attachments output")
    grp.add_argument("--name-pattern",
                     help="Case-insensitive substring match on filename "
                          "(e.g., 'budget' matches '26-0001_Example_Budget_v2.docx')")
    p.set_defaults(func=cmd_download_attachment)

    # scan-attachments
    p = sub.add_parser("scan-attachments", allow_abbrev=False)
    p.add_argument("--message-id", required=True)
    p.set_defaults(func=cmd_scan_attachments)

    # mark-read
    p = sub.add_parser("mark-read", allow_abbrev=False)
    p.add_argument("--message-id", required=True)
    p.set_defaults(func=cmd_mark_read)

    # archive-message
    p = sub.add_parser("archive-message", allow_abbrev=False)
    p.add_argument("--message-id", required=True)
    p.set_defaults(func=cmd_archive_message)

    # set-categories
    p = sub.add_parser(
        "set-categories",
        help="Set Outlook categories on a message (comma-separated). Merges "
             "with the existing list unless --replace.",
        allow_abbrev=False)
    p.add_argument("--message-id", required=True)
    p.add_argument("--categories", required=True,
                   help="Comma-separated category names, e.g. "
                        "'auto-tagged,course-site,needs-reply'")
    p.add_argument("--replace", action="store_true",
                   help="Overwrite the message's categories instead of merging")
    p.set_defaults(func=cmd_set_categories)

    # archive-conversation
    p = sub.add_parser("archive-conversation", allow_abbrev=False)
    p.add_argument("--conversation-id", required=True)
    p.set_defaults(func=cmd_archive_conversation)

    # snooze-message
    p = sub.add_parser("snooze-message", allow_abbrev=False)
    p.add_argument("--message-id", required=True)
    p.add_argument("--until", required=True, help="ISO date to resurface, e.g. 2026-04-21")
    p.add_argument("--description", required=True, help="Short label for morning briefing")
    p.add_argument("--ledger-id", default=None)
    p.set_defaults(func=cmd_snooze_message)

    # wake-snoozed (called by morning briefing to move due items back to inbox)
    p = sub.add_parser("wake-snoozed", allow_abbrev=False)
    p.set_defaults(func=cmd_wake_snoozed)

    # schedule-send
    p = sub.add_parser("schedule-send", allow_abbrev=False)
    p.add_argument("--draft-id", required=True)
    p.add_argument("--send-after", required=True, help="ISO datetime, e.g. 2026-04-21T09:00")
    p.add_argument("--description", required=True)
    p.add_argument("--ledger-id", default=None)
    p.add_argument("--confirm-token", default=None, help="Confirmation token (required)")
    p.set_defaults(func=cmd_schedule_send)

    # attach-file
    p = sub.add_parser("attach-file", allow_abbrev=False)
    p.add_argument("--message-id", required=True, help="Draft message ID")
    p.add_argument("--file", required=True, help="Path to file")
    p.add_argument("--name", default=None, help="Filename override")
    p.add_argument("--content-type", default="application/pdf")
    p.set_defaults(func=cmd_attach_file)

    # remove-attachment
    p = sub.add_parser("remove-attachment",
                       help="Remove one attachment from a draft (by file name or id)",
                       allow_abbrev=False)
    p.add_argument("--message-id", required=True, help="Draft message ID")
    which = p.add_mutually_exclusive_group(required=True)
    which.add_argument("--name", help="The attachment's file name (any case)")
    which.add_argument("--attachment-id", help="An id from get-attachments")
    p.set_defaults(func=cmd_remove_attachment)

    # get-gmail-draft (fetch draft details for confirmation block)
    p = sub.add_parser("get-gmail-draft", allow_abbrev=False)
    p.add_argument("--email", required=True, help="Gmail address")
    p.add_argument("--draft-id", required=True, help="Gmail draft ID")
    p.add_argument("--with-fingerprint", action="store_true",
                   help="Add the content fingerprint (the bot's card read)")
    p.set_defaults(func=cmd_get_gmail_draft)

    # send-gmail-draft (requires confirmation token, like Outlook send-draft)
    p = sub.add_parser("send-gmail-draft", allow_abbrev=False)
    p.add_argument("--email", required=True, help="Gmail address")
    p.add_argument("--draft-id", required=True, help="Gmail draft ID")
    p.add_argument("--confirm-token", default=None, help="One-time approval token")
    p.set_defaults(func=cmd_send_gmail_draft)
    return parser


if __name__ == "__main__":
    parser = build_parser()
    args = parser.parse_args()
    try:
        import mail_mediation as _mm_child
    except ModuleNotFoundError:
        from preprocessor import mail_mediation as _mm_child
    _mediated = _mm_child.check_child_parse(parser, args)  # the verb the mediator checked
    # Every id option is a plain mail id, whoever runs the tool.
    _mm_child.refuse_unplain_ids("graph_actions", getattr(args, "command", "") or "", sys.argv[1:])
    # An owner-side draft edit the mediator did not start holds the draft's
    # lock, as the mediator does for the resident (draft_lock.py).
    _mm_child.hold_owner_draft_lock("graph_actions", args.command, sys.argv[1:],
                                    mediated=_mediated)
    _resolve_body(args)
    args.func(args)

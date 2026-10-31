#!/usr/bin/env python3
"""Upload files, or post text into a thread, via the EA bot's credentials.

The credential comes by the owner_trust secrets rule
(`owner_trust.owner_secret`): the process environment, then the
owner-private secrets.env (owner processes only), then the legacy
~/.ea/.env.secrets when it is a regular file, not a symlink, owned by this
user and writable by no one else. Each file is checked on the descriptor it
is read from, so the file checked is the file read. ~/.ea/.env belongs to
the resident and never supplies the credential; it supplies only the Slack
channel settings in SETTING_KEYS, read as data. On Slack the credential is
SLACK_BOT_TOKEN; on Zulip, ZULIP_API_KEY. The caller never sees it. With a
credential the job goes through the configured chat frontend
(`chat.load_frontend(for_cli=True)`); with the web chat as a shadow, an
owner process's job is mirrored into it too (docs/webchat.md, CLI senders).

If NO credential is reachable (the resident runs as the ea-bot user, which
by design cannot read it), this falls back to bot-mediated delivery: the
job is staged via slack_outbox for the main bot to send with its own
credentials. That path requires --thread-ts and prints
{"status": "queued", ...} per job instead of sending directly.

Usage:
    chat_upload.py --channel C0XXXXXXXXX --file path1 [--file path2 ...]
                   [--thread-ts 1779902187.528899]
                   [--comment "optional initial comment"]
                   [--title "optional title applied to all files"]

    chat_upload.py --channel C0XXXXXXXXX --thread-ts 1779902187.528899 \\
                   --text "text-only reply into that thread"

--text (or --comment with no --file) posts a message with no attachment. It
requires --thread-ts: posting to the channel already works from anywhere via
the notifier (chat_notify), and threading is the thing the notifier cannot
do.

--thread-ts is a thread key: a Slack message ts, or a Zulip
`stream:<id>:topic:<topic>` key. It holds no whitespace.

Output: one JSON object per job on stdout (newline-delimited). For a direct
upload each has {"file_id", "permalink", "filename"}; a direct text post has
{"ts", "thread_ts"}; a staged delivery has {"status": "queued", ...}.

Exit code 0 on success, 1 on any failure (with error on stderr).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


try:
    import owner_trust as _owner_trust
except ImportError:
    from preprocessor import owner_trust as _owner_trust

# Non-secret Slack settings taken from ~/.ea/.env as data into the
# frontend's environment mapping (never os.environ). No secret comes from
# that file: the resident can write it.
SETTING_KEYS = frozenset({"EA_SLACK_CHANNEL_ID", "EA_SLACK_CHANNEL_NAME",
                          "EA_SLACK_OWNER_ID"})

# The credential each frontend needs; memory needs none.
CREDENTIAL_NAMES = {"slack": "SLACK_BOT_TOKEN", "zulip": "ZULIP_API_KEY"}


def _chat_pkg():
    try:
        import chat
    except ImportError:
        from preprocessor import chat
    return chat


def _frontend_name() -> str:
    try:
        import ea_config
    except ImportError:
        from preprocessor import ea_config
    try:
        return str(ea_config.get_chat().get("frontend") or "slack")
    except Exception:
        return "slack"


def _dotenv() -> Path:
    """The resident's ~/.ea/.env in the EA tree in use (EA_DIR, read at
    call time)."""
    return _owner_trust.ea_dir() / ".env"


def _load_credential(name: str) -> "str | None":
    """The named credential by the owner_trust secrets rule; None when none
    is reachable, and the caller then stages the job for the bot."""
    return _owner_trust.owner_secret(name) or None


def _settings() -> dict:
    """The SETTING_KEYS that ~/.ea/.env sets to a non-empty value, parsed as
    data."""
    values = _owner_trust.read_env_values(_dotenv(), SETTING_KEYS)
    return {k: v for k, v in values.items() if v}


def _frontend_env(name: str, value: str) -> dict:
    """The environment the frontend is built from: the process environment,
    then the settings from ~/.ea/.env, with the probed credential set."""
    env = dict(os.environ)
    for key, val in _settings().items():
        env.setdefault(key, val)
    env[name] = value
    return env


def _message_text(args) -> "str | None":
    """The text for a text-only post, or None if this is a file job.

    --text is explicit; --comment with no --file means the same thing (the
    comment has nothing to hang off, so it IS the message).
    """
    if args.text:
        return args.text
    if not args.file and args.comment:
        return args.comment
    return None


def _stage_for_delivery(args) -> int:
    """No credential in this context: stage the job for the main bot to send.

    This is the resident (ea-bot) path -- it has no bot credential by
    design, so the file (or thread message) rides through the bot instead.
    Requires a thread to land in.
    """
    try:
        from preprocessor import slack_outbox
    except ImportError:
        import slack_outbox  # when run as a script from preprocessor/
    if not args.thread_ts:
        print("ERROR: no SLACK_BOT_TOKEN available in this context, and "
              "--thread-ts is required to stage for bot-mediated delivery "
              "(pass the thread_ts from your dispatch marker).",
              file=sys.stderr)
        return 1
    text = _message_text(args)
    if text is not None:
        try:
            job = slack_outbox.stage_text(text, args.thread_ts)
        except ValueError as e:
            print(f"ERROR staging message for delivery: {e}", file=sys.stderr)
            return 1
        print(json.dumps({"status": "queued", "kind": "text",
                          "sidecar": job["sidecar"],
                          "thread_ts": args.thread_ts}))
        return 0
    try:
        staged = slack_outbox.stage(args.file, args.thread_ts,
                                    comment=args.comment, title=args.title)
    except ValueError as e:
        print(f"ERROR staging file for delivery: {e}", file=sys.stderr)
        return 1
    for s in staged:
        print(json.dumps({"status": "queued", "staged": s["staged"],
                          "filename": s["original_name"],
                          "thread_ts": args.thread_ts}))
    return 0


def _send_direct(args, text, fe) -> int:
    """Send through the frontend with this process's credential."""
    if text is not None:
        try:
            ref = fe.post(args.channel, text, thread_ts=args.thread_ts)
        except Exception as e:
            print(f"ERROR posting message: {e}", file=sys.stderr)
            return 1
        print(json.dumps({"ts": ref.id, "thread_ts": args.thread_ts}))
        return 0

    failures = 0
    for index, path_str in enumerate(args.file):
        path = Path(path_str)
        if not path.is_file():
            print(f"ERROR: not a file: {path}", file=sys.stderr)
            failures += 1
            continue
        # The comment only applies to the first upload to avoid repeating it.
        comment = args.comment if (args.comment and index == 0) else ""
        try:
            ref = fe.upload(args.channel, path, thread_ts=args.thread_ts or None,
                            filename=path.name, title=args.title or path.name,
                            comment=comment)
        except Exception as e:
            print(f"ERROR uploading {path.name}: {e}", file=sys.stderr)
            failures += 1
            continue
        print(json.dumps({"file_id": ref.id, "permalink": ref.url,
                          "filename": path.name}))
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter,
                                     allow_abbrev=False)
    parser.add_argument("--channel", required=True, help="Channel ID (e.g., C0XXXXXXXXX)")
    parser.add_argument(
        "--file",
        action="append",
        help="Path to a file to upload (repeatable)",
    )
    parser.add_argument("--thread-ts", help="Thread key to attach to (optional)")
    parser.add_argument(
        "--comment",
        help="Initial comment posted alongside the file (applied to the first "
             "upload). With no --file it becomes a text-only thread message.",
    )
    parser.add_argument(
        "--title",
        help="Title applied to each uploaded file (defaults to the filename)",
    )
    parser.add_argument(
        "--text",
        help="Post this text into --thread-ts with no attachment "
             "(the one thing the notifier cannot do)",
    )
    args = parser.parse_args()

    text = _message_text(args)
    if text is None and not args.file:
        parser.error("nothing to send: pass --file, --text, or --comment")
    if text is not None and not args.thread_ts:
        parser.error("a text-only post requires --thread-ts (to reach the "
                     "channel instead, use the chat_notify notifier)")
    if args.thread_ts and not _chat_pkg().THREAD_KEY_RE.match(args.thread_ts):
        parser.error("--thread-ts must be a thread key with no spaces "
                     "(Slack: 1712345678.123456)")

    name = _frontend_name()
    cred_name = CREDENTIAL_NAMES.get(name)
    if cred_name is None:
        env = dict(os.environ)
    else:
        credential = _load_credential(cred_name)
        if credential is None:
            return _stage_for_delivery(args)
        env = _frontend_env(cred_name, credential)

    try:
        fe = _chat_pkg().load_frontend(for_cli=True, env=env)
    except Exception as e:
        print(f"ERROR: chat frontend unavailable: {e}", file=sys.stderr)
        return 1
    return _send_direct(args, text, fe)


if __name__ == "__main__":
    sys.exit(main())

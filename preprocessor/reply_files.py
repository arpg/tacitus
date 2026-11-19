#!/usr/bin/env python3
"""The files a reply names, for the bot to attach to that reply.

A turn that makes a file for the user (a letter PDF, a spreadsheet, a
figure) ends with a reply that names it: "the PDF is at
`~/.ea/letters/x/x.pdf`". The user reads that reply in chat, where the path
is no use. So after the reply is posted the bot attaches each file the reply
names, when all of these hold:

  * the reply gives a full path: absolute, or `~/` for the resident's home
    (the reply is the resident's, so its `~` is the resident's);
  * the file's type is one a person opens (ATTACH_EXTENSIONS), so a `.tex`
    source or a JSON state file beside the PDF stays a path; a Markdown
    handoff for the Claude app (`<name>.claude.md`) is one too;
  * the turn uploaded no file of that name itself (chat_upload.py or
    slack_upload.py --file) and the same bytes have not already gone to
    that thread;
  * the file was last changed during the turn and is no larger than
    MAX_ATTACH_BYTES.

The last check runs as the resident, on the descriptor the bytes are read
from (slack_outbox.materialize_for_owner with newer_than and max_bytes, and
no owner fallback). The owner never reads a named path with its own access,
so a reply can only bring the user a file the resident could already read
and post with chat_upload.py.

This module is pure text and bookkeeping: it opens no file.
"""
from __future__ import annotations

import collections
import json
import os
import re
import shlex
import threading
from pathlib import PurePosixPath

# Types a person opens: documents, sheets, slides, images, archives.
ATTACH_EXTENSIONS = frozenset({
    "pdf", "docx", "doc", "odt", "rtf",
    "xlsx", "xls", "ods", "csv",
    "pptx", "ppt", "odp",
    "png", "jpg", "jpeg", "gif", "webp", "svg",
    "zip",
})
# A handoff file for the Claude app (skills/claude-handoff.md) is Markdown
# made for the user to share, unlike a `.md` source beside a PDF.
HANDOFF_SUFFIXES = (".claude.md",)
MAX_ATTACH_BYTES = 10 * 1024 * 1024
MAX_ATTACH_FILES = 4
# A file counts as the turn's when it changed after the turn began, less
# this margin for a write that lands in the same second the turn starts.
MTIME_SLACK_S = 5.0
# An async report has no turn start; a file it names counts when it changed
# within this window before the report was posted.
ASYNC_WINDOW_S = 2 * 3600

# A backquoted span, which may hold a path with spaces.
_CODE_SPAN_RE = re.compile(r"`([^`\n]+)`")
# A bare path token: `~/` or `/` not inside a word, URL or other path, up to
# whitespace or a character that does not belong in a path the EA writes.
_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9/~.:%@\\-])(~?/[^\s`'\"<>|()\[\]{}*,;]+)")
_TRAILING = ".,:;!?_*)]}'\""
_UPLOAD_SCRIPT_RE = re.compile(r"(?:chat|slack)_upload\.py")


def _attachable(path: str) -> bool:
    name = PurePosixPath(path).name.lower()
    return (PurePosixPath(name).suffix.lstrip(".") in ATTACH_EXTENSIONS
            or (name.endswith(HANDOFF_SUFFIXES) and len(name) > len(".claude.md")))


def _absolute(raw: str, home: str) -> str | None:
    raw = raw.strip().rstrip(_TRAILING)
    if raw.startswith("~/"):
        raw = home.rstrip("/") + raw[1:]
    if not raw.startswith("/") or raw.endswith("/"):
        return None
    path = os.path.normpath(raw)
    return path if _attachable(path) else None


def named_paths(text: str, home: str) -> list[str]:
    """The attachable absolute paths `text` names, in order of first
    mention, each once. `~/` expands to `home`; a bare file name or a
    relative path is not a path here."""
    found: list[str] = []

    def add(raw: str) -> None:
        path = _absolute(raw, home)
        if path and path not in found:
            found.append(path)

    text = text or ""
    for m in _CODE_SPAN_RE.finditer(text):
        span = m.group(1).strip()
        if span.startswith(("/", "~/")):
            add(span)
    for m in _TOKEN_RE.finditer(text):
        add(m.group(1))
    return found


def transcript_commands(raw: str) -> list[str]:
    """The Bash commands in a slice of a Claude transcript (JSONL)."""
    commands: list[str] = []
    for line in (raw or "").splitlines():
        if '"tool_use"' not in line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        message = row.get("message") if isinstance(row, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else []:
            if (isinstance(block, dict) and block.get("type") == "tool_use"
                    and isinstance(block.get("input"), dict)):
                command = block["input"].get("command")
                if isinstance(command, str):
                    commands.append(command)
    return commands


def uploaded_names(commands) -> set[str]:
    """The base names of the files the turn uploaded itself: every --file
    argument of a chat_upload.py or slack_upload.py command."""
    names: set[str] = set()
    for command in commands or ():
        if not isinstance(command, str) or not _UPLOAD_SCRIPT_RE.search(command):
            continue
        try:
            words = shlex.split(command)
        except ValueError:
            words = command.split()
        for i, word in enumerate(words):
            if word == "--file" and i + 1 < len(words):
                value = words[i + 1]
            elif word.startswith("--file="):
                value = word[len("--file="):]
            else:
                continue
            if value:
                names.add(os.path.basename(value.rstrip("/")))
    return names


class SentLog:
    """The (thread, sha256) pairs already posted, the newest `size` of them.
    The bot notes every file it posts to a thread here, from the outbox and
    from a reply, so the same bytes go to one thread once."""

    def __init__(self, size: int = 512):
        self._seen: collections.OrderedDict = collections.OrderedDict()
        self._size = size
        self._lock = threading.Lock()

    def first_time(self, thread: str | None, digest: str) -> bool:
        """True, and the pair noted, when it has not been noted before."""
        key = (str(thread or ""), digest)
        with self._lock:
            if key in self._seen:
                return False
            self._seen[key] = True
            while len(self._seen) > self._size:
                self._seen.popitem(last=False)
            return True

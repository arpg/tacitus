"""Stopping a turn that is still running.

A message like `stop` or `cancel` in a thread whose turn is in flight
ends that turn's Claude process instead of queueing behind it. The bot
keeps the live processes here by run id; the dispatch registry says
which run belongs to which thread. A stopped run reports `cancelled` to
its lane, which posts no reply; the drafts the run made before the stop
keep their cards, so the user cancels those there.

The words are the user's own: in a room or the DM the bot puts a header
before them, and the header does not count. A message the user deletes
while it waits behind a running turn is withdrawn and never starts.
"""
from __future__ import annotations

import re
import threading

# Short messages that mean "stop what you are doing", on their own or
# with a few words around them. A long message that happens to contain
# "stop" is an instruction, not a cancel.
_CANCEL = re.compile(
    r"^\W*(?:please\s+)?(?:(?:cancel|stop|abort|kill|halt)(?:\s+(?:that|this|it|the\s+(?:last\s+)?(?:request|turn|run|task)|now))?"
    r"|never\s*mind|nevermind|wrong\s+(?:channel|room|thread)|disregard(?:\s+(?:that|this))?)\W*$",
    re.I)
MAX_WORDS = 6
# The header the bot puts before the user's words in a room or the DM
# ("[From the private #ea-x channel, ...]\n\n", "[From your direct
# message ...]\n\n").
_PREAMBLE = re.compile(r"^\s*\[From [^\]]{0,600}\]\s*")


def own_words(text: str) -> str:
    """The user's words, without the room or DM header."""
    t = text or ""
    m = _PREAMBLE.match(t)
    return t[m.end():] if m else t


def is_cancel(text: str) -> bool:
    """True when the message is a cancel, not an instruction that mentions one."""
    t = " ".join(own_words(text).split())
    if not t or len(t.split()) > MAX_WORDS:
        return False
    return bool(_CANCEL.match(t))


class LiveRuns:
    """The Claude processes that are running now, by run id."""

    def __init__(self):
        self._lock = threading.Lock()
        self._procs: dict[str, object] = {}
        self._cancelled: set[str] = set()

    def register(self, run_id: str, proc) -> None:
        if not run_id:
            return
        with self._lock:
            self._procs[run_id] = proc
            self._cancelled.discard(run_id)

    def unregister(self, run_id: str) -> None:
        with self._lock:
            self._procs.pop(run_id, None)

    def live(self) -> list[str]:
        with self._lock:
            return list(self._procs)

    def stop(self, run_id: str, stopper) -> bool:
        """End the run's process with `stopper(proc)` and mark it cancelled.
        False when no process is live under that id."""
        with self._lock:
            proc = self._procs.get(run_id)
            if proc is None:
                return False
            self._cancelled.add(run_id)
        try:
            stopper(proc)
        except Exception:
            pass
        return True

    def cancelled(self, run_id: str) -> bool:
        with self._lock:
            return run_id in self._cancelled

    def forget(self, run_id: str) -> None:
        with self._lock:
            self._procs.pop(run_id, None)
            self._cancelled.discard(run_id)


def deleted_ids(event: dict) -> list[str]:
    """The message ids a delete event names: `message_id`, or
    `message_ids` for a client that takes bulk deletes."""
    ev = event or {}
    ids = ev.get("message_ids") or [ev.get("message_id")]
    return [str(i) for i in ids if i not in (None, "")]


class Withdrawn:
    """The ids of messages the user deleted, the newest KEEP of them."""

    KEEP = 500

    def __init__(self):
        self._lock = threading.Lock()
        self._ids: dict[str, None] = {}

    def add(self, msg_id) -> None:
        key = str(msg_id or "")
        if not key:
            return
        with self._lock:
            self._ids.pop(key, None)
            self._ids[key] = None
            while len(self._ids) > self.KEEP:
                self._ids.pop(next(iter(self._ids)))

    def has(self, msg_id) -> bool:
        with self._lock:
            return str(msg_id or "") in self._ids

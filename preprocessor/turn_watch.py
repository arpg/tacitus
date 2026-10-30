"""When a running turn is stopped by the clock.

A turn that keeps calling tools is working, however long it takes: the
heartbeat says what it is doing, and `cancel` ends it. The clock stops a
turn in two cases only: its stream has been quiet for `stall` seconds (a
hung tool call, a process that no longer answers), or it has run for
`ceiling` seconds in all. The runner feeds every stream line to
`alive()`; the watch thread checks both clocks every few seconds.
"""
from __future__ import annotations

import threading
import time

TICK_S = 5.0


class Watchdog:
    """Stops `proc` with `stopper(proc)` at the ceiling, or after `stall`
    quiet seconds (None: no stall limit). `expired` is set when it fired;
    `stalled` says which clock. `cancel()` ends the watch."""

    def __init__(self, proc, ceiling: float, stall: float | None, stopper,
                 clock=None, tick: float = TICK_S):
        self._proc = proc
        self._ceiling = float(ceiling)
        self._stall = float(stall) if stall else None
        self._stopper = stopper
        self._clock = clock or time.monotonic
        self._tick = tick
        self._lock = threading.Lock()
        self._started = self._clock()
        self._quiet_since = self._started
        self._done = threading.Event()
        self.expired = threading.Event()
        self.stalled = False
        self._thread = threading.Thread(target=self._watch, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def cancel(self) -> None:
        self._done.set()

    def alive(self) -> None:
        """A line arrived on the stream: the stall clock starts over."""
        with self._lock:
            self._quiet_since = self._clock()

    def check(self) -> bool:
        """One tick: True when a clock has run out (the process is stopped)."""
        now = self._clock()
        with self._lock:
            quiet = now - self._quiet_since
        over_ceiling = now - self._started >= self._ceiling
        over_stall = self._stall is not None and quiet >= self._stall
        if not (over_ceiling or over_stall):
            return False
        self.stalled = over_stall and not over_ceiling
        self.expired.set()
        self._done.set()
        try:
            self._stopper(self._proc)
        except Exception:
            pass
        return True

    def _watch(self) -> None:
        while not self._done.wait(self._tick):
            if self.check():
                return

    @property
    def limit(self) -> float:
        """The seconds of the clock that fired (or would fire first)."""
        return self._stall if self.stalled and self._stall else self._ceiling

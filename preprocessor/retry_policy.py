"""Circuit breaker for external-service calls.

The per-call retry/backoff in graph_client.graph_get outlasts a short
Graph blip but has no memory across calls, so an hour-long outage costs
every cron job the full backoff ladder. The breaker adds that memory:

  - record_failure() after a call exhausts its retries (or hits a
    terminal error worth counting); record_success() on any success.
  - After FAILURE_THRESHOLD consecutive failures the breaker OPENS:
    is_open() returns True and callers fail fast with CircuitOpen
    instead of re-walking the backoff ladder.
  - After COOLDOWN_SECONDS the breaker goes half-open: the next call is
    allowed through as the probe; success closes the breaker, failure
    re-opens it for another cooldown.

State lives in ~/.ea/state/circuit-breakers.json so the bot, cron
preprocessor runs, and ad-hoc CLI calls share one view. Writes are
best-effort atomic (tmp+rename); a lost race miscounts by one, which the
threshold makes harmless. sentinel.py reads the same file and alerts
when a breaker is open (check: circuit_breakers).

The file is in the resident's space (docs/SECURITY.md, "Owner trust
rules"), and a write carries every service's entry back into it. So the
read goes through resident_io.read_text, with the resident's access, and
keeps only entries in the shape this module writes; the write goes
through atomic_io. A link at the name is refused on the read and replaced
on the write, never followed, so no owner-only file reaches the state.

Graph runs two breakers, one per lane (graph_lane() below):

  - `graph` is the interactive lane: the owner's own mail actions
    (graph_actions.py from a chat turn, the mediator's SAFE ops on the
    owner's behalf). An open interactive breaker never fails a call fast:
    the call still makes one attempt, the half-open probe, and its answer
    closes or re-opens the breaker.
  - `graph-background` is every cron tick and agent lane (the mail
    tagger, ledger_sync, the briefing, the reconciler). It fails fast
    while open, as above.

A background failure counts only toward `graph-background`, so a mailbox
that Microsoft throttles for the ticks never blocks the person.

TickBackoff gives a tick the step after the breaker: a tick that ended in
HTTP 429 makes the next TICK_SKIPS ticks of that job skip, so a throttled
mailbox is not asked again every two minutes. Its state is
~/.ea/state/tick-backoff.json, beside the breakers, read and written the
same way.

Usage:
    from retry_policy import CircuitBreaker, CircuitOpen
    _breaker = CircuitBreaker("graph")
    def call():
        _breaker.raise_if_open()
        try:
            result = do_request()
        except TransientExhausted:
            _breaker.record_failure("...")
            raise
        _breaker.record_success()
        return result
"""
from __future__ import annotations

import json
import os
import time
try:
    import atomic_io as _atomic
    import resident_io as _rio
except ModuleNotFoundError:  # imported as preprocessor.<mod>
    from preprocessor import atomic_io as _atomic
    from preprocessor import resident_io as _rio
from pathlib import Path

EA_DIR = Path(os.environ.get("EA_DIR", str(Path.home() / ".ea")))
STATE_FILE = EA_DIR / "state" / "circuit-breakers.json"

FAILURE_THRESHOLD = 3        # consecutive exhausted-retry failures
COOLDOWN_SECONDS = 300       # open duration before a half-open probe
_CACHE_TTL = 1.0             # in-process read cache (graph_get is chatty)


class CircuitOpen(RuntimeError):
    """Raised by raise_if_open() when the service breaker is open.
    `service` is the breaker's key and `reopens_at` the epoch second its
    cooldown ends (the next call after it is the probe)."""

    def __init__(self, message: str = "", *, service: str = "",
                 reopens_at: float | None = None):
        super().__init__(message)
        self.service = service
        self.reopens_at = reopens_at


_NUMBER_FIELDS = ("failures", "last_failure_ts", "opened_at", "cooldown", "opens", "threshold")


def _valid_entry(entry: dict) -> dict:
    """The fields of one service's entry that have the type this module
    writes: numbers for the counts and times, a string for the error."""
    out = {}
    for field in _NUMBER_FIELDS:
        value = entry.get(field)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out[field] = value
    if isinstance(entry.get("last_error"), str):
        out["last_error"] = entry["last_error"][:300]
    return out


def _load_all() -> dict:
    """Every service's entry, read with the resident's access; {} when the
    file is missing, cannot be read (a link or a hard link at the name
    included) or is not a JSON object."""
    try:
        data = json.loads(_rio.read_text(STATE_FILE) or "{}")
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {service: _valid_entry(entry) for service, entry in data.items()
            if isinstance(service, str) and isinstance(entry, dict)}


def _save_all(state: dict) -> None:
    try:
        _atomic.write_json(STATE_FILE, state)
    except Exception:
        # State write failures must never break the actual API call path.
        pass


class CircuitBreaker:
    def __init__(self, service: str,
                 threshold: int = FAILURE_THRESHOLD,
                 cooldown: float = COOLDOWN_SECONDS):
        self.service = service
        self.threshold = threshold
        self.cooldown = cooldown
        self._cached: dict | None = None
        self._cached_at = 0.0

    def _entry(self, fresh: bool = False) -> dict:
        now = time.monotonic()
        if fresh or self._cached is None or (now - self._cached_at) > _CACHE_TTL:
            self._cached = _load_all().get(self.service) or {}
            self._cached_at = now
        return self._cached

    def _write(self, entry: dict | None) -> None:
        state = _load_all()
        if entry is None:
            state.pop(self.service, None)
        else:
            state[self.service] = entry
        _save_all(state)
        self._cached = entry or {}
        self._cached_at = time.monotonic()

    def is_open(self) -> bool:
        e = self._entry()
        if (e.get("failures") or 0) < self.threshold:
            return False
        opened_at = e.get("opened_at") or 0
        if (time.time() - opened_at) >= self.cooldown:
            return False  # half-open: let the next call probe
        return True

    def reopens_at(self) -> float | None:
        """The epoch second the open breaker's cooldown ends, or None when
        it is not open."""
        if not self.is_open():
            return None
        return (self._entry().get("opened_at") or 0) + self.cooldown

    def raise_if_open(self) -> None:
        if self.is_open():
            e = self._entry()
            opened_at = e.get("opened_at") or 0
            age = time.time() - opened_at
            raise CircuitOpen(
                f"{self.service} circuit open "
                f"({e.get('failures')} consecutive failures, opened "
                f"{age:.0f}s ago, cooldown {self.cooldown:.0f}s; "
                f"last error: {e.get('last_error', '?')})",
                service=self.service, reopens_at=opened_at + self.cooldown)

    def record_failure(self, error: str = "") -> None:
        e = self._entry(fresh=True)
        failures = (e.get("failures") or 0) + 1
        now = time.time()
        prev_opened = e.get("opened_at") or 0
        entry = {
            "failures": failures,
            "last_failure_ts": now,
            "last_error": str(error)[:300],
            "opened_at": prev_opened,
            # The breaker's own cooldown and the number of times it has
            # opened: open_breakers() reads both, so a breaker with a long
            # cooldown is not read as recovered between its probes, and
            # one that keeps reopening is read as a sustained outage.
            "cooldown": self.cooldown,
            "threshold": self.threshold,
            "opens": e.get("opens") or 0,
        }
        if failures >= self.threshold:
            # Stamp opened_at on the crossing failure and on any failure
            # during half-open probing.
            entry["opened_at"] = now
            if not prev_opened or (now - prev_opened) >= self.cooldown:
                entry["opens"] += 1
        self._write(entry)

    def record_success(self) -> None:
        # Only touch the file when there is something to clear.
        if self._entry().get("failures"):
            self._write(None)


# ── Graph lanes ──────────────────────────────────────────────────────
# The lane of a Graph call comes from its caller: an explicit `lane=` on
# graph_client's entry points, else EA_GRAPH_LANE, which a process sets
# for itself and its children (set_graph_lane), else background. The CLIs
# a chat turn runs (graph_actions, scheduling_engine, email_writer) make
# their own default interactive; the mediator passes the lane of each
# request to the child it runs.
GRAPH_LANE_ENV = "EA_GRAPH_LANE"
INTERACTIVE = "interactive"
BACKGROUND = "background"
GRAPH_BREAKERS = {INTERACTIVE: "graph", BACKGROUND: "graph-background"}


def graph_lane(lane: str | None = None) -> str:
    """The lane a Graph call runs in: `lane` when it names one, else the
    process's (EA_GRAPH_LANE), else background."""
    if lane in GRAPH_BREAKERS:
        return lane
    env = os.environ.get(GRAPH_LANE_ENV, "").strip()
    return env if env in GRAPH_BREAKERS else BACKGROUND


def set_graph_lane(lane: str, *, only_if_unset: bool = False) -> str:
    """Make `lane` the lane of this process and of every process it starts.
    With only_if_unset, a lane the caller already set wins. Returns the
    process's lane."""
    if lane not in GRAPH_BREAKERS:
        raise ValueError(f"unknown Graph lane {lane!r}")
    current = os.environ.get(GRAPH_LANE_ENV, "").strip()
    if not (only_if_unset and current in GRAPH_BREAKERS):
        os.environ[GRAPH_LANE_ENV] = lane
    return os.environ[GRAPH_LANE_ENV]


def graph_breaker_key(lane: str | None = None) -> str:
    """The breaker key of a lane: `graph` (interactive) or `graph-background`."""
    return GRAPH_BREAKERS[graph_lane(lane)]


def is_throttled(exc: BaseException | None) -> bool:
    """True when `exc`, or an exception it was raised from, is an HTTP 429
    answer (a requests HTTPError, or anything carrying `.response`)."""
    seen = 0
    while exc is not None and seen < 5:
        response = getattr(exc, "response", None)
        if getattr(response, "status_code", None) == 429:
            return True
        exc = exc.__cause__ or exc.__context__
        seen += 1
    return False


def find_circuit_open(exc: BaseException | None) -> CircuitOpen | None:
    """The CircuitOpen `exc` is, or was raised from, or None."""
    seen = 0
    while exc is not None and seen < 5:
        if isinstance(exc, CircuitOpen):
            return exc
        exc = exc.__cause__ or exc.__context__
        seen += 1
    return None


# ── tick backoff ─────────────────────────────────────────────────────
TICK_BACKOFF_FILE = EA_DIR / "state" / "tick-backoff.json"
TICK_SKIPS = 3


def _load_ticks() -> dict:
    """Every job's backoff entry: {"skip": int, "since": float, "reason":
    str}; {} when the file is missing, unreadable or of another shape."""
    try:
        data = json.loads(_rio.read_text(TICK_BACKOFF_FILE) or "{}")
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for job, entry in data.items():
        if not (isinstance(job, str) and isinstance(entry, dict)):
            continue
        skip = entry.get("skip")
        if not isinstance(skip, int) or isinstance(skip, bool) or skip <= 0:
            continue
        clean = {"skip": min(skip, 100)}
        since = entry.get("since")
        if isinstance(since, (int, float)) and not isinstance(since, bool):
            clean["since"] = since
        if isinstance(entry.get("reason"), str):
            clean["reason"] = entry["reason"][:200]
        out[job] = clean
    return out


def _save_ticks(state: dict) -> None:
    try:
        _atomic.write_json(TICK_BACKOFF_FILE, state)
    except Exception:
        pass


class TickBackoff:
    """The skip count of one periodic job (mail_tagger, ledger_sync).

    throttled() after a tick that ended in HTTP 429; take() at the start
    of each tick: it returns how many skips were left before this one
    (0 means run the tick) and uses one up."""

    def __init__(self, job: str, skips: int = TICK_SKIPS):
        self.job = job
        self.skips = skips

    def pending(self) -> int:
        return (_load_ticks().get(self.job) or {}).get("skip", 0)

    def take(self) -> int:
        state = _load_ticks()
        entry = state.get(self.job)
        if not entry:
            return 0
        left = entry["skip"]
        if left > 1:
            state[self.job] = {**entry, "skip": left - 1}
        else:
            state.pop(self.job, None)
        _save_ticks(state)
        return left

    def throttled(self, reason: str = "HTTP 429") -> None:
        state = _load_ticks()
        state[self.job] = {"skip": self.skips, "since": time.time(),
                           "reason": str(reason)[:200]}
        _save_ticks(state)


SUSTAINED_OPENS = 3     # a breaker that reopened this often stays listed until a success clears it


def open_breakers() -> list[dict]:
    """All currently-open breakers (for sentinel / status surfaces).

    A breaker is open from ITS OWN threshold, and one past its cooldown
    is half-open: it stays listed while it keeps failing within two of
    its own cooldowns (the entry records both; the module defaults are
    the fallback for an older entry). One that has
    opened SUSTAINED_OPENS times is a sustained outage and stays listed
    until a success clears the entry, however long the next probe takes:
    a service that answered nobody is not recovered."""
    out = []
    now = time.time()
    for service, e in _load_all().items():
        if (e.get("failures") or 0) < (e.get("threshold") or FAILURE_THRESHOLD):
            continue
        cooldown = float(e.get("cooldown") or COOLDOWN_SECONDS)
        sustained = (e.get("opens") or 0) >= SUSTAINED_OPENS
        if (now - (e.get("opened_at") or 0)) >= cooldown and not sustained:
            if (now - (e.get("last_failure_ts") or 0)) > cooldown * 2:
                continue
        out.append({"service": service, **e})
    return out


if __name__ == "__main__":
    print(json.dumps({"open": open_breakers(), "all": _load_all()}, indent=2))

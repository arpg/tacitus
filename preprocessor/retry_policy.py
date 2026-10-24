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
    """Raised by raise_if_open() when the service breaker is open."""


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

    def raise_if_open(self) -> None:
        if self.is_open():
            e = self._entry()
            age = time.time() - (e.get("opened_at") or 0)
            raise CircuitOpen(
                f"{self.service} circuit open "
                f"({e.get('failures')} consecutive failures, opened "
                f"{age:.0f}s ago, cooldown {self.cooldown:.0f}s; "
                f"last error: {e.get('last_error', '?')})")

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

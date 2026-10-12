"""Resilience primitives: circuit breaker and bulkhead.

The workflow engine owns its own retry/backoff (`core/workflow.py::_backoff`) because
backoff is a per-node property there. This kit holds only the two guards the writer
path uses: a breaker that opens on repeated infra failure and a bulkhead that bounds
concurrent writers.
"""
from __future__ import annotations

import threading
import time


class CircuitBreaker:
    """Closed -> Open (failure threshold) -> Half-open (probe) -> Closed (probe ok).

    The state machine is deliberately simple and observable; it protects the store
    and executor paths from cascading failures.
    """

    def __init__(self, name: str, *, failure_threshold: int = 5, window_seconds: float = 30.0,
                 open_seconds: float = 5.0, clock=None):
        self.name = name
        self._threshold = failure_threshold
        self._window = window_seconds
        self._open_seconds = open_seconds
        self._clock = clock or time
        self._state = "closed"
        self._failures: list[float] = []
        self._opened_at = 0.0
        self._lock = threading.Lock()

    def state(self) -> str:
        with self._lock:
            return self._state

    def _prune(self, now: float) -> None:
        self._failures = [f for f in self._failures if now - f <= self._window]

    def allow(self) -> bool:
        now = self._clock.monotonic() if hasattr(self._clock, "monotonic") else time.monotonic()
        with self._lock:
            if self._state == "closed":
                # `record_failure` opens the breaker the moment the threshold is
                # reached, so closed here always means "still under threshold".
                self._prune(now)
                return True
            if self._state == "open":
                if now - self._opened_at >= self._open_seconds:
                    self._state = "half_open"
                    return True  # allow exactly one probe
                return False
            return False  # half-open: only the pending probe goes through

    def record_success(self) -> None:
        with self._lock:
            if self._state in ("half_open", "open"):
                self._state = "closed"
            self._failures = []

    def record_failure(self) -> None:
        now = self._clock.monotonic() if hasattr(self._clock, "monotonic") else time.monotonic()
        with self._lock:
            if self._state == "half_open":
                self._state = "open"
                self._opened_at = now
                return
            self._prune(now)
            self._failures.append(now)
            if len(self._failures) >= self._threshold:
                self._state = "open"
                self._opened_at = now


class Bulkhead:
    """Bounded concurrency slot pool. `try_acquire` returns None when saturated
    so callers can apply backpressure instead of buffering unboundedly."""

    def __init__(self, name: str, max_concurrency: int):
        self.name = name
        self._max = max(1, max_concurrency)
        self._in_use = 0
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)

    def try_acquire(self) -> bool:
        with self._cond:
            if self._in_use >= self._max:
                return False
            self._in_use += 1
            return True

    def acquire(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._in_use >= self._max:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(remaining)
            self._in_use += 1
            return True

    def release(self) -> None:
        with self._cond:
            self._in_use = max(0, self._in_use - 1)
            self._cond.notify_all()

    @property
    def in_use(self) -> int:
        with self._lock:
            return self._in_use

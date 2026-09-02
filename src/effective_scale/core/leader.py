"""Lease-based leadership.

One row (`meta.leader`) holds {holder, nonce, expires_at}. A holder renews only if
it still owns the nonce; a lost/heartbeat-missed lease demotes immediately and cleanly.
Writes are epoch-scoped via `Kernel.write_epoch` so a stale leader's writes are
rejected — the worst case after split-brain is a brief write stall, never corruption.
"""
from __future__ import annotations

import copy
import threading
import time

from ..domain.ids import new_nonce


class LeaderElection:
    def __init__(self, store, holder_id: str, *, ttl: float = 5.0, heartbeat: float = 1.0,
                 clock=None, logger=None, on_lost=None):
        self.store = store
        self.holder_id = holder_id
        self.ttl = ttl
        self.heartbeat = heartbeat
        self.clock = clock or time
        self.logger = logger
        self.on_lost = on_lost
        self._nonce = ""
        self._is_leader = False
        self._terms = 1
        self._lock = threading.Lock()
        self._stop = threading.Event()

    # ---- ownership ---------------------------------------------------------
    def is_leader(self) -> bool:
        return self._is_leader

    def nonce(self) -> str:
        return self._nonce

    def acquire(self) -> bool:
        """Try to become leader. Returns True if we hold the lease after this call."""
        snap = self.store.snapshot()
        row = snap.meta.get("leader")
        now = self.clock.monotonic() if hasattr(self.clock, "monotonic") else time.monotonic()
        if row and row.get("expires_at", 0) > now and row.get("holder") != self.holder_id:
            self._is_leader = False
            return False
        self._nonce = new_nonce()
        self._is_leader = True
        self._terms += 1
        self.store.put_meta("leader", {
            "holder": self.holder_id, "nonce": self._nonce,
            "expires_at": now + self.ttl, "term": self._terms,
        })
        return True

    def renew(self) -> bool:
        """Extend our lease iff we still own it (nonce match). Demote otherwise."""
        if not self._is_leader:
            return False
        snap = self.store.snapshot()
        row = snap.meta.get("leader") or {}
        now = self.clock.monotonic() if hasattr(self.clock, "monotonic") else time.monotonic()
        if row.get("holder") != self.holder_id or row.get("nonce") != self._nonce:
            self._demote("renew_failed: lease owned by another holder")
            return False
        if row.get("expires_at", 0) < now - self.ttl:
            self._demote("renew_failed: clock skew beyond lease")
            return False
        new_row = copy.deepcopy(row)
        new_row["expires_at"] = now + self.ttl
        self.store.put_meta("leader", new_row)
        return True

    def _demote(self, reason: str) -> None:
        if self._is_leader:
            self._is_leader = False
            if self.logger:
                self.logger.log("leader.demoted", warn=True, holder=self.holder_id, reason=reason)
            if self.on_lost:
                self.on_lost(reason)

    # ---- loop ----------------------------------------------------------------
    def run(self) -> None:
        """Blocking loop for a dedicated thread: acquire, then heartbeat until lost/stopped."""
        while not self._stop.is_set():
            if not self._is_leader:
                if self.acquire():
                    if self.logger:
                        self.logger.log("leader.acquired", info=True, holder=self.holder_id)
                else:
                    self.clock.sleep(self.heartbeat)
                    continue
            if not self.renew():
                continue
            self._stop.wait(self.heartbeat)
        self._demote("shutdown")

    def stop(self) -> None:
        self._stop.set()

    @property
    def term(self) -> int:
        return self._terms


class SingleLeader:
    """Trivial leader for single-process/test runs: always leader."""

    def __init__(self, holder_id: str = "single"):
        self.holder_id = holder_id
        self._nonce = new_nonce()
        self._stop = threading.Event()
        self.term = 1

    def is_leader(self) -> bool:
        return True

    def nonce(self) -> str:
        return self._nonce

    def acquire(self) -> bool:
        return True

    def renew(self) -> bool:
        return True

    def run(self) -> None:
        self._stop.wait()

    def stop(self) -> None:
        self._stop.set()

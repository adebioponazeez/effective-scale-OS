"""Time port — injectable so all timing logic is testable and deterministic."""
from __future__ import annotations

import time
from typing import Protocol


class Clock(Protocol):
    def now(self) -> float: ...          # wall clock (unix seconds)
    def monotonic(self) -> float: ...    # monotonic seconds
    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(max(0.0, seconds))

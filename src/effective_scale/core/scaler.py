"""Auto-scaler: target-based with hysteresis, cooldown and scale-down protection.

Why not pure response? Three words: oscillation, oscillation, oscillation. Deadband
prevents 1 % metric noise from flipping replicas; cooldown prevents decision churn;
scale-down protection prevents a spike-echo from right-sizing us down onto the next
spike. The policy surface is a protocol, so a predictive/ML policy can be plugged in
without touching the kernel.
"""
from __future__ import annotations

import math
import time
from typing import Protocol


class ScalerPolicy(Protocol):
    def decide(self, workload, measured: dict[str, float], now: float) -> int | None: ...


class TargetScaler:
    def __init__(self, *, deadband: float = 0.10, cooldown: float = 60.0,
                 protect_after: float = 180.0, step: int | None = None,
                 clock=None):
        self._deadband = deadband
        self._cooldown = cooldown
        self._protect = protect_after
        self._step = step
        self._clock = clock or time
        self._last_action: dict[str, float] = {}
        self._last_up: dict[str, float] = {}

    def decide(self, workload, measured: dict[str, float], now: float) -> int | None:
        targets = workload.scaler.get("metrics") if isinstance(workload.scaler, dict) else None
        if not targets:
            return None

        candidates: list[float] = []
        for metric, cfg in targets.items():
            if not isinstance(cfg, dict):
                continue
            observed = measured.get(metric)
            target = float(cfg.get("target", 0))
            if observed is None or target <= 0:
                continue
            cand = math.ceil(workload.desired_replicas * float(observed) / target)
            low = float(cfg.get("min", 0))
            high = float(cfg.get("max", float("inf")))
            candidates.append(min(max(cand, low), high))

        if not candidates:
            return None

        desired = max(candidates)  # dominant (most demanding) metric
        desired = int(min(max(desired, workload.min_replicas), workload.max_replicas))
        current = workload.desired_replicas

        # 1) deadband: ignore small *relative* deltas, but never block a unit change
        #    from a tiny base (one replica on 2 is 50% — that MUST scale)
        if desired != current and abs(desired - current) <= self._deadband * max(1.0, float(current)):
            return None

        direction = "up" if desired > current else "down"
        last_action = self._last_action.get(workload.id, 0.0)
        # 2) cooldown
        if now - last_action < self._cooldown:
            return None
        # 3) scale-down protection right after an up-scale
        if direction == "down" and self._last_up.get(workload.id, 0.0) and \
                now - self._last_up[workload.id] < self._protect:
            return None
        # 4) bounded step
        if self._step is not None and abs(desired - current) > self._step:
            desired = current + (self._step if direction == "up" else -self._step)
        desired = int(min(max(desired, workload.min_replicas), workload.max_replicas))
        return desired if desired != current else None

    def record(self, workload_id: str, now: float, applied_desired: int, previous: int) -> None:
        self._last_action[workload_id] = now
        if applied_desired > previous:
            self._last_up[workload_id] = now

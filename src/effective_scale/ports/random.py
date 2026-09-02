"""Randomness port — jitter, nonces, ids. Seeded in tests for determinism."""
from __future__ import annotations

import random
from typing import Protocol


class RandomSource(Protocol):
    def uniform(self, a: float, b: float) -> float: ...
    def randint(self, a: int, b: int) -> int: ...
    def choice(self, seq): ...


class SecureRandom:
    def uniform(self, a: float, b: float) -> float:
        return random.uniform(a, b)

    def randint(self, a: int, b: int) -> int:
        return random.randint(a, b)

    def choice(self, seq):
        return random.choice(seq)

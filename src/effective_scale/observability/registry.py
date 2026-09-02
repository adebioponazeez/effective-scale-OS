"""Metrics registry: counters, gauges, histograms — Prometheus-style but JSON-exposed.

Thread-safe (the API reads while loops write). No external collector needed for v1;
the registry is the adapter surface for OTLP later (same three instrument types).
"""
from __future__ import annotations

import threading
from typing import Callable


class Counter:
    def __init__(self, name: str, help_text: str, labels: dict[str, str] | None = None):
        self.name = name
        self.help_text = help_text
        self.labels = labels or {}
        self._value = 0.0
        self._lock = threading.Lock()

    def inc(self, n: float = 1.0) -> None:
        with self._lock:
            self._value += n

    def value(self) -> float:
        with self._lock:
            return self._value


class Gauge:
    def __init__(self, name: str, help_text: str, labels: dict[str, str] | None = None):
        self.name = name
        self.help_text = help_text
        self.labels = labels or {}
        self._value = 0.0
        self._lock = threading.Lock()

    def set(self, value: float) -> None:
        with self._lock:
            self._value = float(value)

    def add(self, delta: float) -> None:
        with self._lock:
            self._value += float(delta)

    def value(self) -> float:
        with self._lock:
            return self._value


class Histogram:
    def __init__(self, name: str, help_text: str, buckets: tuple[float, ...] | None = None,
                 labels: dict[str, str] | None = None):
        self.name = name
        self.help_text = help_text
        self.buckets = buckets or (0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
        self.labels = labels or {}
        self._counts = [0] * (len(self.buckets) + 1)
        self._sum = 0.0
        self._count = 0
        self._lock = threading.Lock()

    def observe(self, value: float) -> None:
        with self._lock:
            self._sum += value
            self._count += 1
            for i, b in enumerate(self.buckets):
                if value <= b:
                    self._counts[i] += 1
            self._counts[-1] += 1

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "buckets": [{"le": b, "count": c} for b, c in zip(self.buckets, self._counts)],
                "count": self._count,
                "sum": self._sum,
            }


class Registry:
    def __init__(self) -> None:
        self._counters: dict[str, Counter] = {}
        self._gauges: dict[str, Gauge] = {}
        self._histograms: dict[str, Histogram] = {}
        self._lock = threading.Lock()

    def counter(self, name: str, help_text: str = "") -> Counter:
        with self._lock:
            return self._counters.setdefault(name, Counter(name, help_text))

    def gauge(self, name: str, help_text: str = "") -> Gauge:
        with self._lock:
            return self._gauges.setdefault(name, Gauge(name, help_text))

    def histogram(self, name: str, help_text: str = "") -> Histogram:
        with self._lock:
            return self._histograms.setdefault(name, Histogram(name, help_text))

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "counters": {k: v.value() for k, v in sorted(self._counters.items())},
                "gauges": {k: v.value() for k, v in sorted(self._gauges.items())},
                "histograms": {k: v.snapshot() for k, v in sorted(self._histograms.items())},
            }

"""Logging port — structured records, secret redaction, trace correlation."""
from __future__ import annotations

import json
import sys
import threading
from typing import Any, Protocol

_REDACT_KEYS = {"secret", "secret_hash", "token", "password", "authorization", "api_key"}


def redact(data: Any, *, depth: int = 0) -> Any:
    if depth > 6:
        return "<truncated>"
    if isinstance(data, dict):
        out = {}
        for k, v in data.items():
            if str(k).lower() in _REDACT_KEYS and v is not None:
                out[k] = "[REDACTED]"
            else:
                out[k] = redact(v, depth=depth + 1)
        return out
    if isinstance(data, (list, tuple)):
        return [redact(v, depth=depth + 1) for v in data]
    return data


_LEVEL_FLAGS = ("debug", "info", "warn", "error", "fatal")


def split_level(level: str, fields: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Normalise the two spellings of severity into `(level, fields)`.

    Call sites use the house style `logger.log("x", warn=True, error=str(exc))`:
    a *boolean* flag names the level while a non-boolean `error="..."` stays a
    payload field. `level="warn"` keeps working too; an explicit level wins.
    Flags are consumed so they never leak into the record as bogus fields, and
    severity is real — `JsonLogger(level="error")` must keep error records.
    """
    for flag in _LEVEL_FLAGS:
        if fields.get(flag) is True:
            if level == "info":
                level = flag
            del fields[flag]
    return level, fields


class Logger(Protocol):
    def log(self, event: str, level: str = "info", **fields: Any) -> None:
        """Emit one structured record; severity via `level=` or a bool flag."""


class JsonLogger:
    def __init__(self, stream=None, level: str = "info", redact_secrets: bool = True) -> None:
        self._stream = stream or sys.stdout
        self._level = level
        self._redact = redact_secrets
        self._lock = threading.Lock()

    def log(self, event: str, level: str = "info", **fields: Any) -> None:
        level, fields = split_level(level, dict(fields))
        if level not in ("debug", "info", "warn", "error", "fatal"):
            level = "info"
        order = {"debug": 0, "info": 1, "warn": 2, "error": 3, "fatal": 4}
        if order[level] < order.get(self._level, 1):
            return
        record = {"ts": _now(), "level": level, "event": event, **(fields if self._redact else fields)}
        if self._redact:
            record = redact(record)
        with self._lock:
            self._stream.write(json.dumps(record, sort_keys=True, default=str) + "\n")
            self._stream.flush()


class MemLogger:
    """Test double: keeps records, never writes to stdout."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def log(self, event: str, level: str = "info", **fields: Any) -> None:
        level, fields = split_level(level, dict(fields))
        self.records.append({"event": event, "level": level, **fields})


def _now() -> float:
    import time

    return time.time()

"""Deterministic id helpers for the execution slice."""
from __future__ import annotations

import re
import uuid


def new_execution_id() -> str:
    return f"exec-{uuid.uuid4().hex[:16]}"


def new_worker_id(prefix: str = "saf") -> str:
    import socket

    return f"{prefix}-{socket.gethostname().split('.')[0]}-{uuid.uuid4().hex[:6]}"


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value).lower()).strip("-") or "unnamed"

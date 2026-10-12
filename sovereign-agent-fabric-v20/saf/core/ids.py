"""Id helpers for the execution slice."""
from __future__ import annotations

import socket
import uuid


def new_execution_id() -> str:
    return f"exec-{uuid.uuid4().hex[:16]}"


def new_worker_id(prefix: str = "saf") -> str:
    """Human-readable, collision-resistant worker id: prefix-host-6hex."""
    return f"{prefix}-{socket.gethostname().split('.')[0]}-{uuid.uuid4().hex[:6]}"

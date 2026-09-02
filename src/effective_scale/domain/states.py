"""State machines with explicit transition tables — invariant enforcement lives here."""
from __future__ import annotations

from enum import Enum

from .errors import StateError


class WorkflowStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class NodeStatus(str, Enum):
    PENDING = "pending"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


class AttemptStatus(str, Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


class LeaseState(str, Enum):
    ACTIVE = "active"
    RENEWING = "renewing"
    EXPIRED = "expired"
    RELEASED = "released"


class EventStatus(str, Enum):
    PENDING = "pending"
    DELIVERED = "delivered"
    DEAD = "dead"


class NodeState(str, Enum):
    ACTIVE = "active"
    CORDONED = "cordoned"
    DRAINING = "draining"
    OFFLINE = "offline"


class WorkloadStatus(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    DELETING = "deleting"


_WF = {
    WorkflowStatus.PENDING: {WorkflowStatus.RUNNING, WorkflowStatus.CANCELLED},
    WorkflowStatus.RUNNING: {
        WorkflowStatus.SUCCEEDED,
        WorkflowStatus.FAILED,
        WorkflowStatus.CANCELLED,
        WorkflowStatus.TIMED_OUT,
    },
}
_NODE = {
    NodeStatus.PENDING: {NodeStatus.DISPATCHED, NodeStatus.SKIPPED, NodeStatus.CANCELLED},
    NodeStatus.DISPATCHED: {
        NodeStatus.RUNNING,
        NodeStatus.SUCCEEDED,
        NodeStatus.FAILED,
        NodeStatus.TIMED_OUT,
        NodeStatus.CANCELLED,
        NodeStatus.SKIPPED,
        NodeStatus.PENDING,
    },
    NodeStatus.RUNNING: {
        NodeStatus.SUCCEEDED,
        NodeStatus.FAILED,
        NodeStatus.TIMED_OUT,
        NodeStatus.CANCELLED,
        NodeStatus.SKIPPED,
    },
    NodeStatus.FAILED: {NodeStatus.PENDING},
    NodeStatus.TIMED_OUT: {NodeStatus.PENDING},
}
_ATTEMPT = {
    AttemptStatus.RUNNING: {
        AttemptStatus.SUCCEEDED,
        AttemptStatus.FAILED,
        AttemptStatus.TIMED_OUT,
        AttemptStatus.CANCELLED,
    }
}
_LEASE = {
    LeaseState.ACTIVE: {LeaseState.RENEWING, LeaseState.EXPIRED, LeaseState.RELEASED},
    LeaseState.RENEWING: {LeaseState.ACTIVE, LeaseState.EXPIRED, LeaseState.RELEASED},
}
_EVENT = {
    EventStatus.PENDING: {EventStatus.DELIVERED, EventStatus.DEAD},
    EventStatus.DELIVERED: set(),
    EventStatus.DEAD: set(),
}


def transition(kind: str, current: Enum, target: Enum) -> None:
    table = {
        "workflow": _WF,
        "node": _NODE,
        "attempt": _ATTEMPT,
        "lease": _LEASE,
        "event": _EVENT,
    }.get(kind)
    if table is None:
        raise ValueError(f"unknown state machine kind: {kind}")
    allowed = table.get(current, set())
    if target not in allowed:
        raise StateError(
            f"illegal {kind} transition: {current.value} -> {target.value}",
            details={"from": current.value, "to": target.value, "kind": kind},
        )

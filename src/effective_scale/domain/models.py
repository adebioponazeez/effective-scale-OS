"""Domain entities, value objects and validation (pure: no I/O, no adapters)."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .errors import ValidationError
from .ids import new_id
from .states import (
    AttemptStatus,
    EventStatus,
    LeaseState,
    NodeState,
    NodeStatus,
    WorkflowStatus,
    WorkloadStatus,
)


def _require(cond: bool, msg: str, details: dict | None = None) -> None:
    if not cond:
        raise ValidationError(msg, details=details)


# --------------------------------------------------------------------------
# Enums with wire values
# --------------------------------------------------------------------------


class SchedulingPolicy(str, Enum):
    FIFO = "fifo"
    PRIORITY = "priority"
    ROUND_ROBIN = "round_robin"
    BIN_PACK = "bin_pack"


def parse_enum(enum_cls, value: Any, name: str):
    if isinstance(value, enum_cls):
        return value
    try:
        return enum_cls(str(value).lower())
    except (ValueError, AttributeError):
        allowed = ", ".join(e.value for e in enum_cls)
        raise ValidationError(f"{name} must be one of: {allowed}", details={"field": name})


# --------------------------------------------------------------------------
# Control plane
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Namespace:
    name: str
    created_at: float

    @classmethod
    def create(cls, name: Any, now: float) -> "Namespace":
        _require(isinstance(name, str) and 1 <= len(name) <= 64, "namespace name must be 1-64 chars")
        _require(name.isalnum() and name[0].isalpha(), "namespace must be alnum, starting with a letter")
        return cls(name=name, created_at=now)


@dataclass(slots=True)
class Token:
    id: str
    namespace: str
    scopes: frozenset[str]
    secret_hash: str
    created_at: float
    expires_at: float | None = None

    @classmethod
    def create(cls, namespace: str, scopes: Any, secret_hash: str, now: float, ttl: float | None = None) -> "Token":
        valid_scopes = {"read", "write", "admin"}
        if isinstance(scopes, str):
            scopes = [s.strip() for s in scopes.split(",") if s.strip()]
        scopes = [str(s).lower() for s in (scopes or ["read"])]
        _require(all(s in valid_scopes for s in scopes), f"scopes must be subset of {sorted(valid_scopes)}")
        return cls(
            id=new_id(),
            namespace=namespace,
            scopes=frozenset(scopes),
            secret_hash=secret_hash,
            created_at=now,
            expires_at=now + ttl if ttl else None,
        )

    @property
    def scope_list(self) -> list[str]:
        return sorted(self.scopes)


# --------------------------------------------------------------------------
# Compute plane
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Workload:
    id: str
    namespace: str
    name: str
    image: str
    replicas: int
    min_replicas: int
    max_replicas: int
    cpu: int                    # millicores per replica
    memory: int                 # MiB per replica
    policy: SchedulingPolicy
    node_tags: tuple[str, ...]
    pin: str | None
    spread: bool
    priority: int
    status: WorkloadStatus
    desired_replicas: int
    created_at: float
    updated_at: float
    scaler: dict[str, Any] = field(default_factory=dict)
    health_path: str | None = None

    @classmethod
    def create(cls, namespace: str, data: dict[str, Any], now: float) -> "Workload":
        name = data.get("name")
        _require(isinstance(name, str) and 1 <= len(name) <= 128, "workload name is required (1-128 chars)")
        image = data.get("image")
        _require(isinstance(image, str) and image, "image is required")
        replicas = int(data.get("replicas", 1))
        cpu = int(data.get("cpu", 100))
        memory = int(data.get("memory", 128))
        _require(replicas >= 0, "replicas must be >= 0")
        _require(cpu > 0 and memory > 0, "cpu/memory must be > 0")
        min_replicas = int(data.get("min_replicas", 0))
        max_replicas = int(data.get("max_replicas", max(1, replicas * 4)))
        _require(0 <= min_replicas <= replicas <= max_replicas < 10_000, "min <= replicas <= max")
        scaler = data.get("scaler", {})
        _require(isinstance(scaler, dict), "scaler must be an object")
        tags = [str(t) for t in data.get("node_tags", [])]
        priority = int(data.get("priority", 0))
        return cls(
            id=new_id(),
            namespace=namespace,
            name=name,
            image=image,
            replicas=replicas,
            min_replicas=min_replicas,
            max_replicas=max_replicas,
            cpu=cpu,
            memory=memory,
            policy=parse_enum(SchedulingPolicy, data.get("policy", "bin_pack"), "policy"),
            node_tags=tuple(tags),
            pin=data.get("pin"),
            spread=bool(data.get("spread", False)),
            priority=priority,
            status=WorkloadStatus.ACTIVE,
            desired_replicas=replicas,
            created_at=now,
            updated_at=now,
            scaler=scaler,
            health_path=data.get("health_path"),
        )


@dataclass(slots=True)
class Node:
    id: str
    name: str
    tags: tuple[str, ...]
    capacity_cpu: int
    capacity_mem: int
    state: NodeState
    used_cpu: int = 0
    used_mem: int = 0
    created_at: float = 0.0
    last_heartbeat: float = 0.0

    @classmethod
    def create(cls, data: dict[str, Any], now: float) -> "Node":
        name = data.get("name")
        _require(isinstance(name, str) and name, "node name is required")
        cpu = int(data.get("cpu", 1000))
        mem = int(data.get("memory", 2048))
        _require(cpu > 0 and mem > 0, "node cpu/memory must be > 0")
        tags = [str(t) for t in data.get("tags", [])]
        return cls(
            id=new_id(),
            name=name,
            tags=tuple(tags),
            capacity_cpu=cpu,
            capacity_mem=mem,
            state=NodeState.ACTIVE,
            created_at=now,
            last_heartbeat=now,
        )

    @property
    def free_cpu(self) -> int:
        return self.capacity_cpu - self.used_cpu

    @property
    def free_mem(self) -> int:
        return self.capacity_mem - self.used_mem

    def can_host(self, cpu: int, mem: int) -> bool:
        return self.state == NodeState.ACTIVE and self.free_cpu >= cpu and self.free_mem >= mem


@dataclass(slots=True)
class Lease:
    id: str
    workload_id: str
    namespace: str
    node_id: str
    holder: str
    nonce: str
    expires_at: float
    state: LeaseState
    attempt: int = 1
    trace_id: str = ""
    created_at: float = 0.0
    cancellable: bool = False

    @property
    def is_expired(self, now: float) -> bool:
        return now >= self.expires_at

    def renew(self, nonce: str, old_expires: float, new_expires: float) -> "Lease":
        from .states import transition

        transition("lease", self.state, LeaseState.RENEWING)
        if nonce != self.nonce:
            raise ValidationError("lease renewal belongs to a different holder (nonce mismatch)")
        if old_expires != self.expires_at:
            raise ValidationError("lease renewal raced with a change (stale expires_at)")
        self.expires_at = new_expires
        self.state = LeaseState.ACTIVE
        return self


# --------------------------------------------------------------------------
# Workflow plane
# --------------------------------------------------------------------------


@dataclass(slots=True)
class WorkflowNode:
    id: str
    name: str
    depends_on: tuple[str, ...]
    workload_id: str | None
    event_topic: str | None
    retry_max: int
    backoff_base: float
    backoff_max: float
    jitter: float
    timeout: float | None
    max_concurrency: int
    status: NodeStatus
    attempts: int
    next_retry_at: float
    error: str | None
    finished_at: float | None
    started_at: float | None

    @classmethod
    def create(cls, id: str, data: dict[str, Any]) -> "WorkflowNode":
        name = data.get("name", id)
        _require(isinstance(name, str) and name, "node name is required")
        deps = [str(d) for d in data.get("depends_on", [])]
        _require(all(d != id for d in deps), "a node cannot depend on itself")
        workload_id = data.get("workload_id") or data.get("exec", {}).get("workload_id")
        event_topic = data.get("event_topic") or data.get("exec", {}).get("topic")
        _require(bool(workload_id) ^ bool(event_topic), "node must exec exactly one of workload_id | event_topic")
        retry_max = int(data.get("retry", {}).get("max", 3))
        _require(0 <= retry_max <= 100, "retry max must be 0..100")
        return cls(
            id=id,
            name=name,
            depends_on=tuple(deps),
            workload_id=workload_id,
            event_topic=event_topic,
            retry_max=retry_max,
            backoff_base=float(data.get("retry", {}).get("base_seconds", 1.0)),
            backoff_max=float(data.get("retry", {}).get("max_seconds", 60.0)),
            jitter=float(data.get("retry", {}).get("jitter", 0.2)),
            timeout=float(data["timeout"]) if data.get("timeout") else None,
            max_concurrency=int(data.get("max_concurrency", 16)),
            status=NodeStatus.PENDING,
            attempts=0,
            next_retry_at=0.0,
            error=None,
            finished_at=None,
            started_at=None,
        )


@dataclass(slots=True)
class Workflow:
    id: str
    namespace: str
    name: str
    nodes: dict[str, WorkflowNode]
    status: WorkflowStatus
    created_at: float
    updated_at: float
    timeout: float | None
    schedule: str | None
    dead_letter: bool
    trace_id: str = ""
    error: str | None = None
    cancel_requested: bool = False
    epoch: int = 1

    @classmethod
    def create(cls, namespace: str, data: dict[str, Any], now: float, trace_id: str = "") -> "Workflow":
        name = data.get("name")
        _require(isinstance(name, str) and 1 <= len(name) <= 128, "workflow name is required")
        raw_nodes = data.get("nodes") or []
        _require(isinstance(raw_nodes, list) and raw_nodes, "workflow needs at least one node")
        nodes: dict[str, WorkflowNode] = {}
        for raw in raw_nodes:
            _require(isinstance(raw, dict) and raw.get("id"), "each node needs an id")
            nid = str(raw["id"])
            _require(nid not in nodes, f"duplicate node id: {nid}")
            nodes[nid] = WorkflowNode.create(nid, raw)
        _validate_dag(nodes)
        timeout = float(data["timeout"]) if data.get("timeout") else None
        _require(timeout is None or timeout > 0, "workflow timeout must be > 0")
        return cls(
            id=new_id(),
            namespace=namespace,
            name=name,
            nodes=nodes,
            status=WorkflowStatus.PENDING,
            created_at=now,
            updated_at=now,
            timeout=timeout,
            schedule=data.get("schedule"),
            dead_letter=bool(data.get("dead_letter", True)),
            trace_id=trace_id,
        )


def _validate_dag(nodes: dict[str, WorkflowNode]) -> None:
    # Referential integrity + cycle detection (Kahn) at submit time.
    for nid, node in nodes.items():
        for dep in node.depends_on:
            _require(dep in nodes, f"node {nid} depends on unknown node {dep}")
    indeg = {nid: len(n.depends_on) for nid, n in nodes.items()}
    ready = [nid for nid, d in indeg.items() if d == 0]
    seen = 0
    while ready:
        cur = ready.pop()
        seen += 1
        for nid, node in nodes.items():
            if cur in node.depends_on:
                indeg[nid] -= 1
                if indeg[nid] == 0:
                    ready.append(nid)
    _require(seen == len(nodes), "workflow DAG contains a cycle")


@dataclass(slots=True)
class Attempt:
    id: str
    workflow_id: str
    node_id: str
    attempt_no: int
    lease_id: str | None
    status: AttemptStatus
    started_at: float
    finished_at: float | None
    error: str | None
    trace_id: str
    # External-worker protocol (ADR-006): a worker *claims* a lease-bound attempt
    # and receives a fencing `worker_nonce`; only that nonce may heartbeat or
    # complete the attempt. `deadline` is the worker-visible lease deadline.
    worker_id: str | None = None
    worker_nonce: str | None = None
    deadline: float | None = None
    result: dict[str, Any] | None = None

    @classmethod
    def start(cls, workflow_id: str, node_id: str, attempt_no: int, lease_id: str | None,
              now: float, trace_id: str) -> "Attempt":
        return cls(
            id=new_id(),
            workflow_id=workflow_id,
            node_id=node_id,
            attempt_no=attempt_no,
            lease_id=lease_id,
            status=AttemptStatus.RUNNING,
            started_at=now,
            finished_at=None,
            error=None,
            trace_id=trace_id,
        )

    @property
    def claimable(self) -> bool:
        """Lease-bound, running, unclaimed: a pull-based worker may take it."""
        return self.status == AttemptStatus.RUNNING and self.lease_id is not None \
            and self.worker_id is None


# --------------------------------------------------------------------------
# Data/event plane
# --------------------------------------------------------------------------


@dataclass(slots=True)
class EventMsg:
    id: str
    topic: str
    key: str
    partition: int
    seq: int
    payload: dict[str, Any]
    schema_version: int
    trace_id: str
    created_at: float
    status: EventStatus
    delivery_attempts: dict[str, int]  # group -> attempts

    @classmethod
    def create(cls, topic: str, key: str, partition: int, seq: int, payload: dict[str, Any],
               now: float, trace_id: str, schema_version: int = 1) -> "EventMsg":
        _require(isinstance(topic, str) and 1 <= len(topic) <= 128, "topic is required")
        _require(isinstance(payload, dict), "payload must be a JSON object")
        return cls(
            id=new_id(),
            topic=topic,
            key=str(key),
            partition=partition,
            seq=seq,
            payload=payload,
            schema_version=int(schema_version),
            trace_id=trace_id,
            created_at=now,
            status=EventStatus.PENDING,
            delivery_attempts={},
        )


@dataclass(slots=True)
class DlqEntry:
    id: str
    topic: str
    partition: int
    seq: int
    group: str
    error: str
    attempts: int
    created_at: float


# --------------------------------------------------------------------------
# Wire helpers
# --------------------------------------------------------------------------


def to_jsonable(obj: Any) -> Any:
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, (list, tuple, frozenset)):
        return [to_jsonable(x) for x in obj]
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    if hasattr(obj, "__dataclass_fields__"):
        # slots dataclasses have no __dict__; fields access is the portable path
        return {k: to_jsonable(getattr(obj, k)) for k in obj.__dataclass_fields__}
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


def dumps(obj: Any) -> str:
    return json.dumps(to_jsonable(obj), sort_keys=True, separators=(",", ":"))

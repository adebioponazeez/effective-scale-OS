"""Persistence port.

The Store is the ONLY place that knows about record layout. Adapters (in-memory, SQLite,
future Postgres) implement the same 12-ish operations. Correctness invariants (snapshot
swapping, single-writer discipline) live in the base class so every adapter inherits them.
"""
from __future__ import annotations

import abc
import threading
from typing import Any

from ..domain.models import (
    Attempt,
    DlqEntry,
    EventMsg,
    Lease,
    Namespace,
    Node,
    Token,
    Workflow,
    Workload,
)


class Snapshot:
    """Immutable-by-convention view of the world, atomically replaceable.

    Readers hold a reference; the writer swaps the whole object after committing a
    mutation. CPython reference assignment is atomic, so readers never observe a
    half-applied change.
    """

    __slots__ = (
        "namespaces", "tokens", "workloads", "nodes", "leases", "workflows",
        "attempts", "events", "events_index", "offsets", "dlqs", "idempotency",
        "audit", "meta", "seq_counters", "epoch", "idem_response",
    )

    def __init__(self) -> None:
        self.namespaces: dict[str, Namespace] = {}
        self.tokens: dict[str, Token] = {}
        self.workloads: dict[str, Workload] = {}
        self.nodes: dict[str, Node] = {}
        self.leases: dict[str, Lease] = {}
        self.workflows: dict[str, Workflow] = {}
        self.attempts: dict[str, Attempt] = {}
        self.events: dict[str, EventMsg] = {}
        self.events_index: dict[tuple[str, int], list[str]] = {}
        self.offsets: dict[tuple[str, str, int], int] = {}
        self.dlqs: dict[str, DlqEntry] = {}
        self.idempotency: dict[str, str] = {}            # key -> request_hash
        self.idem_response: dict[str, tuple[str, int, dict]] = {}  # key -> (body,status,meta)
        self.audit: list[dict[str, Any]] = []
        self.meta: dict[str, Any] = {}
        self.seq_counters: dict[tuple[str, int], int] = {}
        self.epoch: int = 1

    # Convenience lookups -------------------------------------------------
    def workload(self, wid: str) -> Workload | None:
        return self.workloads.get(wid)

    def node(self, nid: str) -> Node | None:
        return self.nodes.get(nid)

    def lease(self, lid: str) -> Lease | None:
        return self.leases.get(lid)

    def workflow(self, wid: str) -> Workflow | None:
        return self.workflows.get(wid)

    def events_for(self, topic: str, partition: int) -> list[EventMsg]:
        return [self.events[i] for i in self.events_index.get((topic, partition), [])]

    def next_seq(self, topic: str, partition: int) -> int:
        return self.seq_counters.get((topic, partition), 0) + 1


class Store(abc.ABC):
    """Single-writer store. All mutators must be called from the writer; all readers
    use `snapshot()` which returns the current immutable-by-convention Snapshot."""

    def __init__(self) -> None:
        self._snapshot = Snapshot()
        self._lock = threading.RLock()
        self._write_count = 0

    # Lifecycle -----------------------------------------------------------
    @abc.abstractmethod
    def open(self) -> None: ...

    @abc.abstractmethod
    def close(self) -> None: ...

    def snapshot(self) -> Snapshot:
        return self._snapshot

    def write_count(self) -> int:
        return self._write_count

    # Meta ----------------------------------------------------------------
    def put_meta(self, key: str, value: Any) -> None:
        with self._lock:
            self._snapshot.meta[key] = value
            self._persist_meta(key, value)
            self._after_write()

    def get_meta(self, key: str, default: Any = None) -> Any:
        return self._snapshot.meta.get(key, default)

    # Namespaces / tokens --------------------------------------------------
    def put_namespace(self, ns: Namespace) -> None:
        with self._lock:
            self._snapshot.namespaces[ns.name] = ns
            self._persist_namespace(ns)
            self._after_write()

    def delete_namespace(self, name: str) -> None:
        with self._lock:
            self._snapshot.namespaces.pop(name, None)
            self._persist_delete("namespaces", ("name", name))
            self._after_write()

    def put_token(self, token: Token) -> None:
        with self._lock:
            self._snapshot.tokens[token.id] = token
            self._persist_token(token)
            self._after_write()

    def delete_token(self, token_id: str) -> None:
        with self._lock:
            self._snapshot.tokens.pop(token_id, None)
            self._persist_delete("tokens", ("id", token_id))
            self._after_write()

    # Workloads -------------------------------------------------------------
    def put_workload(self, wl: Workload) -> None:
        with self._lock:
            self._snapshot.workloads[wl.id] = wl
            self._persist_workload(wl)
            self._after_write()

    def delete_workload(self, wid: str) -> None:
        with self._lock:
            self._snapshot.workloads.pop(wid, None)
            self._persist_delete("workloads", ("id", wid))
            self._after_write()

    # Nodes ------------------------------------------------------------------
    def put_node(self, node: Node) -> None:
        with self._lock:
            self._snapshot.nodes[node.id] = node
            self._persist_node(node)
            self._after_write()

    def delete_node(self, nid: str) -> None:
        with self._lock:
            self._snapshot.nodes.pop(nid, None)
            self._persist_delete("nodes", ("id", nid))
            self._after_write()

    # Leases ------------------------------------------------------------------
    def put_lease(self, lease: Lease) -> None:
        with self._lock:
            self._snapshot.leases[lease.id] = lease
            self._persist_lease(lease)
            self._after_write()

    def delete_lease(self, lid: str) -> None:
        with self._lock:
            self._snapshot.leases.pop(lid, None)
            self._persist_delete("leases", ("id", lid))
            self._after_write()

    # Workflows ---------------------------------------------------------------
    def put_workflow(self, wf: Workflow) -> None:
        with self._lock:
            self._snapshot.workflows[wf.id] = wf
            self._persist_workflow(wf)
            self._after_write()

    def delete_workflow(self, wid: str) -> None:
        with self._lock:
            self._snapshot.workflows.pop(wid, None)
            self._persist_delete("workflows", ("id", wid))
            self._after_write()

    # Attempts ------------------------------------------------------------------
    def put_attempt(self, a: Attempt) -> None:
        with self._lock:
            self._snapshot.attempts[a.id] = a
            self._persist_attempt(a)
            self._after_write()

    # Events / offsets / DLQ ------------------------------------------------------
    def put_event(self, ev: EventMsg) -> None:
        with self._lock:
            self._snapshot.events[ev.id] = ev
            self._snapshot.events_index.setdefault((ev.topic, ev.partition), []).append(ev.id)
            self._snapshot.seq_counters[(ev.topic, ev.partition)] = ev.seq
            self._persist_event(ev)
            self._after_write()

    def put_offset(self, group: str, topic: str, partition: int, seq: int) -> None:
        with self._lock:
            self._snapshot.offsets[(group, topic, partition)] = seq
            self._persist_offset(group, topic, partition, seq)
            self._after_write()

    def put_dlq(self, entry: DlqEntry) -> None:
        with self._lock:
            self._snapshot.dlqs[entry.id] = entry
            self._persist_dlq(entry)
            self._after_write()

    # Idempotency -------------------------------------------------------------------
    def put_idempotency(self, key: str, request_hash: str, status: int, body: dict) -> None:
        with self._lock:
            self._snapshot.idempotency[key] = request_hash
            self._snapshot.idem_response[key] = (body, status, {"replayed": True})
            self._persist_idempotency(key, request_hash, status, body)
            self._after_write()

    # Audit ------------------------------------------------------------------------
    def put_audit(self, entry: dict[str, Any]) -> None:
        with self._lock:
            self._snapshot.audit.append(entry)
            if len(self._snapshot.audit) > 10_000:  # bounded in-memory tail; DB keeps all
                self._snapshot.audit = self._snapshot.audit[-5_000:]
            self._persist_audit(entry)
            self._after_write()

    # ---- persistence hooks (adapter-specific) -------------------------------
    @abc.abstractmethod
    def _persist_meta(self, key: str, value: Any) -> None: ...

    @abc.abstractmethod
    def _persist_namespace(self, ns: Namespace) -> None: ...

    @abc.abstractmethod
    def _persist_token(self, token: Token) -> None: ...

    @abc.abstractmethod
    def _persist_workload(self, wl: Workload) -> None: ...

    @abc.abstractmethod
    def _persist_node(self, node: Node) -> None: ...

    @abc.abstractmethod
    def _persist_lease(self, lease: Lease) -> None: ...

    @abc.abstractmethod
    def _persist_workflow(self, wf: Workflow) -> None: ...

    @abc.abstractmethod
    def _persist_attempt(self, a: Attempt) -> None: ...

    @abc.abstractmethod
    def _persist_event(self, ev: EventMsg) -> None: ...

    @abc.abstractmethod
    def _persist_offset(self, group: str, topic: str, partition: int, seq: int) -> None: ...

    @abc.abstractmethod
    def _persist_dlq(self, entry: DlqEntry) -> None: ...

    @abc.abstractmethod
    def _persist_idempotency(self, key: str, request_hash: str, status: int, body: dict) -> None: ...

    @abc.abstractmethod
    def _persist_audit(self, entry: dict[str, Any]) -> None: ...

    @abc.abstractmethod
    def _persist_delete(self, table: str, key: tuple[str, str]) -> None: ...

    def _after_write(self) -> None:
        self._write_count += 1

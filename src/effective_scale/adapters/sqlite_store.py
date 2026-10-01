"""SQLite adapter: WAL + FULL fsync durability behind the same Store port."""
from __future__ import annotations

import json
import os
import sqlite3
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
    WorkflowNode,
    Workload,
    AttemptStatus,
    EventStatus,
    LeaseState,
    NodeState,
    NodeStatus,
    SchedulingPolicy,
    WorkflowStatus,
    WorkloadStatus,
)
from ..ports.store import Snapshot, Store
from .memory_store import MemoryStore

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS namespaces(name TEXT PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tokens(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS workloads(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS nodes(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS leases(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS workflows(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS attempts(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS offsets(g TEXT NOT NULL, t TEXT NOT NULL, p INTEGER NOT NULL,
                                   seq INTEGER NOT NULL, PRIMARY KEY(g,t,p));
CREATE TABLE IF NOT EXISTS dlq(id TEXT PRIMARY KEY, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS idempotency(k TEXT PRIMARY KEY, request_hash TEXT NOT NULL,
                                       status INTEGER NOT NULL, body TEXT NOT NULL,
                                       created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
                                 actor TEXT, action TEXT NOT NULL, resource TEXT,
                                 outcome TEXT NOT NULL, detail TEXT, trace_id TEXT);
CREATE INDEX IF NOT EXISTS idx_events_topic ON events(payload);
CREATE INDEX IF NOT EXISTS idx_workflows_ns ON workflows(payload);
"""



def _state(obj):
    """Slots-safe state extraction (dataclasses with __slots__ have no __dict__)."""
    return {k: getattr(obj, k) for k in obj.__dataclass_fields__}


def _j(obj: Any) -> str:
    from ..domain.models import dumps

    return dumps(obj)


class SQLiteStore(MemoryStore):
    """Durable adapter. Inherits memory indexing semantics from MemoryStore;
    every mutation is also fsync'd to SQLite. A mutation is visible in memory
    only after the SQL transaction commits (hooks run inside `_persist_*`)."""

    def __init__(self, path: str):
        super().__init__()
        self._path = path
        self._conn: sqlite3.Connection | None = None

    # Lifecycle --------------------------------------------------------------
    def open(self) -> None:
        if self._path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(self._path)), exist_ok=True)
        # check_same_thread=False is deliberate: the connection is used ONLY by the
        # single writer thread (ADR-005); all other threads read the in-memory snapshot.
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA foreign_keys=OFF")
        self._conn.executescript(_SCHEMA)
        self._conn.execute(
            "INSERT OR IGNORE INTO meta(k, v) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),)
        )
        self._conn.commit()
        self._check_schema()
        self._load_all()

    def ping(self) -> bool:
        """True only if the kernel's own connection can still execute a statement."""
        try:
            self._conn.execute("SELECT 1").fetchone()
            return True
        except Exception:  # noqa: BLE001 — any failure means "not ready", never a 500
            return False

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.commit()
            except sqlite3.ProgrammingError:
                pass  # already closed (crash simulation); nothing left to flush
            try:
                self._conn.close()
            except sqlite3.ProgrammingError:
                pass
            self._conn = None

    def _check_schema(self) -> None:
        row = self._conn.execute("SELECT v FROM meta WHERE k='schema_version'").fetchone()
        version = int(row["v"]) if row else 0
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"store schema v{version} is newer than supported v{SCHEMA_VERSION}; refusing to downgrade"
            )

    def _load_all(self) -> None:
        snap = self._snapshot
        snap.namespaces = {
            r["name"]: Namespace(**json.loads(r["payload"]))
            for r in self._conn.execute("SELECT name, payload FROM namespaces")
        }
        snap.tokens = {
            r["id"]: _token_from_state(json.loads(r["payload"]))
            for r in self._conn.execute("SELECT id, payload FROM tokens")
        }
        snap.workloads = {
            r["id"]: _workload_from_state(json.loads(r["payload"]))
            for r in self._conn.execute("SELECT id, payload FROM workloads")
        }
        snap.nodes = {
            r["id"]: _node_from_state(json.loads(r["payload"]))
            for r in self._conn.execute("SELECT id, payload FROM nodes")
        }
        snap.leases = {
            r["id"]: _lease_from_state(json.loads(r["payload"]))
            for r in self._conn.execute("SELECT id, payload FROM leases")
        }
        snap.workflows = {
            r["id"]: _workflow_from_state(json.loads(r["payload"]))
            for r in self._conn.execute("SELECT id, payload FROM workflows")
        }
        snap.attempts = {
            r["id"]: _attempt_from_state(json.loads(r["payload"]))
            for r in self._conn.execute("SELECT id, payload FROM attempts")
        }
        for r in self._conn.execute("SELECT id, payload FROM events"):
            ev = _event_from_state(json.loads(r["payload"]))
            snap.events[ev.id] = ev
            snap.events_index.setdefault((ev.topic, ev.partition), []).append(ev.id)
            snap.seq_counters[(ev.topic, ev.partition)] = max(
                snap.seq_counters.get((ev.topic, ev.partition), 0), ev.seq
            )
        for r in self._conn.execute("SELECT g, t, p, seq FROM offsets"):
            snap.offsets[(r["g"], r["t"], r["p"])] = r["seq"]
        snap.dlqs = {
            r["id"]: DlqEntry(**json.loads(r["payload"]))
            for r in self._conn.execute("SELECT id, payload FROM dlq")
        }
        for r in self._conn.execute("SELECT k, request_hash, status, body FROM idempotency"):
            snap.idempotency[r["k"]] = r["request_hash"]
            snap.idem_response[r["k"]] = (json.loads(r["body"]), r["status"], {"replayed": True})
        for r in self._conn.execute(
            "SELECT id, ts, actor, action, resource, outcome, detail, trace_id FROM audit ORDER BY id DESC LIMIT 10000"
        ):
            snap.audit.append(dict(r))
        snap.audit.reverse()
        for r in self._conn.execute("SELECT k, v FROM meta"):
            snap.meta[r["k"]] = json.loads(r["v"]) if r["k"] not in ("schema_version",) else int(r["v"])
        snap.meta.setdefault("schema_version", SCHEMA_VERSION)

    # ---- persistence hooks -------------------------------------------------
    def _upsert(self, table: str, key_col: str, key: str, payload: str) -> None:
        assert self._conn is not None
        with self._conn:
            self._conn.execute(
                f"INSERT INTO {table}({key_col}, payload) VALUES(?, ?) "
                f"ON CONFLICT({key_col}) DO UPDATE SET payload=excluded.payload",
                (key, payload),
            )

    def _persist_meta(self, key: str, value: Any) -> None:
        with self._conn:
            self._conn.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                               (key, _j(value)))

    def _persist_namespace(self, ns: Namespace) -> None:
        self._upsert("namespaces", "name", ns.name, _j(_state(ns)))

    def _persist_token(self, token: Token) -> None:
        self._upsert("tokens", "id", token.id, _j(_state(token)))

    def _persist_workload(self, wl: Workload) -> None:
        self._upsert("workloads", "id", wl.id, _j(_state(wl)))

    def _persist_node(self, node: Node) -> None:
        self._upsert("nodes", "id", node.id, _j(_state(node)))

    def _persist_lease(self, lease: Lease) -> None:
        self._upsert("leases", "id", lease.id, _j(_state(lease)))

    def _persist_workflow(self, wf: Workflow) -> None:
        state = {
            **_state(wf),
            "nodes": list(wf.nodes.values()),
        }
        self._upsert("workflows", "id", wf.id, _j(state))

    def _persist_attempt(self, a: Attempt) -> None:
        self._upsert("attempts", "id", a.id, _j(_state(a)))

    def _persist_event(self, ev: EventMsg) -> None:
        self._upsert("events", "id", ev.id, _j(_state(ev)))

    def _persist_offset(self, group: str, topic: str, partition: int, seq: int) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO offsets(g,t,p,seq) VALUES(?,?,?,?) ON CONFLICT(g,t,p) DO UPDATE SET seq=excluded.seq",
                (group, topic, partition, seq),
            )

    def _persist_dlq(self, entry: DlqEntry) -> None:
        self._upsert("dlq", "id", entry.id, _j(_state(entry)))

    def _persist_idempotency(self, key: str, request_hash: str, status: int, body: dict) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO idempotency(k, request_hash, status, body, created_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(k) DO UPDATE SET request_hash=excluded.request_hash, status=excluded.status, body=excluded.body",
                (key, request_hash, status, _j(body), _now()),
            )

    def _persist_audit(self, entry: dict[str, Any]) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO audit(ts, actor, action, resource, outcome, detail, trace_id) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    entry.get("ts", _now()),
                    entry.get("actor", ""),
                    entry.get("action", ""),
                    entry.get("resource", ""),
                    entry.get("outcome", "ok"),
                    _j(entry.get("detail", {})),
                    entry.get("trace_id", ""),
                ),
            )

    def _persist_delete(self, table: str, key: tuple[str, str]) -> None:
        allowed = {
            "namespaces": "namespaces", "tokens": "tokens", "workloads": "workloads",
            "nodes": "nodes", "leases": "leases", "workflows": "workflows",
        }
        sql_table = allowed.get(table)
        if sql_table is None:
            return
        col, val = key
        with self._conn:
            self._conn.execute(f"DELETE FROM {sql_table} WHERE {col}=?", (val,))


def _now() -> float:
    import time

    return time.time()


# ---- state reconstruction (JSON -> dataclass) -------------------------------------


def _token_from_state(s: dict) -> Token:
    return Token(id=s["id"], namespace=s["namespace"], scopes=frozenset(s["scopes"]),
                 secret_hash=s["secret_hash"], created_at=s["created_at"], expires_at=s.get("expires_at"))


def _workload_from_state(s: dict) -> Workload:
    return Workload(
        id=s["id"], namespace=s["namespace"], name=s["name"], image=s["image"],
        replicas=s["replicas"], min_replicas=s["min_replicas"], max_replicas=s["max_replicas"],
        cpu=s["cpu"], memory=s["memory"], policy=SchedulingPolicy(s["policy"]),
        node_tags=tuple(s["node_tags"]), pin=s.get("pin"), spread=s["spread"],
        priority=s["priority"], status=WorkloadStatus(s["status"]),
        desired_replicas=s["desired_replicas"], created_at=s["created_at"],
        updated_at=s["updated_at"], scaler=s.get("scaler", {}), health_path=s.get("health_path"),
    )


def _node_from_state(s: dict) -> Node:
    return Node(id=s["id"], name=s["name"], tags=tuple(s["tags"]), capacity_cpu=s["capacity_cpu"],
                capacity_mem=s["capacity_mem"], state=NodeState(s["state"]), used_cpu=s["used_cpu"],
                used_mem=s["used_mem"], created_at=s["created_at"], last_heartbeat=s["last_heartbeat"])


def _lease_from_state(s: dict) -> Lease:
    return Lease(id=s["id"], workload_id=s["workload_id"], namespace=s["namespace"],
                 node_id=s["node_id"], holder=s["holder"], nonce=s["nonce"], expires_at=s["expires_at"],
                 state=LeaseState(s["state"]), attempt=s["attempt"], trace_id=s.get("trace_id", ""),
                 created_at=s.get("created_at", 0.0), cancellable=s.get("cancellable", False))


def _node_from_state_wf(s: dict, nid: str) -> WorkflowNode:
    return WorkflowNode(
        id=nid, name=s["name"], depends_on=tuple(s["depends_on"]),
        workload_id=s.get("workload_id"), event_topic=s.get("event_topic"),
        retry_max=s["retry_max"], backoff_base=s["backoff_base"], backoff_max=s["backoff_max"],
        jitter=s["jitter"], timeout=s.get("timeout"), max_concurrency=s["max_concurrency"],
        status=NodeStatus(s["status"]), attempts=s["attempts"], next_retry_at=s["next_retry_at"],
        error=s.get("error"), finished_at=s.get("finished_at"), started_at=s.get("started_at"),
    )


def _workflow_from_state(s: dict) -> Workflow:
    nodes = {n["id"]: _node_from_state_wf(n, n["id"]) for n in s["nodes"]}
    return Workflow(
        id=s["id"], namespace=s["namespace"], name=s["name"], nodes=nodes,
        status=WorkflowStatus(s["status"]), created_at=s["created_at"], updated_at=s["updated_at"],
        timeout=s.get("timeout"), schedule=s.get("schedule"), dead_letter=s.get("dead_letter", True),
        trace_id=s.get("trace_id", ""), error=s.get("error"),
        cancel_requested=s.get("cancel_requested", False), epoch=s.get("epoch", 1),
    )


def _attempt_from_state(s: dict) -> Attempt:
    return Attempt(id=s["id"], workflow_id=s["workflow_id"], node_id=s["node_id"],
                   attempt_no=s["attempt_no"], lease_id=s.get("lease_id"),
                   status=AttemptStatus(s["status"]), started_at=s["started_at"],
                   finished_at=s.get("finished_at"), error=s.get("error"), trace_id=s.get("trace_id", ""),
                   worker_id=s.get("worker_id"), worker_nonce=s.get("worker_nonce"),
                   deadline=s.get("deadline"), result=s.get("result"))


def _event_from_state(s: dict) -> EventMsg:
    return EventMsg(id=s["id"], topic=s["topic"], key=s["key"], partition=s["partition"],
                    seq=s["seq"], payload=s["payload"], schema_version=s["schema_version"],
                    trace_id=s.get("trace_id", ""), created_at=s["created_at"],
                    status=EventStatus(s["status"]), delivery_attempts=dict(s.get("delivery_attempts", {})))

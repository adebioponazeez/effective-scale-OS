"""Workflow engine: durable DAG execution with retries, timeouts, cancel, recovery.

Lifecycle (each transition persisted before anything else happens):
  node:  pending -> dispatched -> running -> succeeded | failed | timed_out | cancelled | skipped
  wf:    pending -> running -> succeeded | failed | timed_out | cancelled

Invariants enforced here:
  * submit validates DAG (no cycles, no dangling deps) — a bad DAG never enters the store
  * dispatch is idempotent (workflow epoch + node status check; double tick is a no-op)
  * a node has at most one live attempt; attempts carry their own idempotency key
  * crash recovery fails RUNNING attempts (platform restart = worker continuity lost) and
    lets the retry policy re-dispatch — at-least-once with exactly-once *intent*
"""
from __future__ import annotations

import copy
import random
from typing import Any

from ..domain.errors import ConflictError, NotFoundError, ValidationError
from ..domain.models import Attempt, AttemptStatus
from ..domain.models import NodeStatus, Workflow, WorkflowNode, WorkflowStatus
from ..domain.states import transition
from ..ports.store import Store


def _backoff(node: WorkflowNode, rng) -> float:
    base = min(node.backoff_max, node.backoff_base * (2 ** max(0, node.attempts - 1)))
    jitter = base * node.jitter * (rng.uniform(-1.0, 1.0))
    return max(0.0, base + jitter)


class WorkflowEngine:
    def __init__(self, store: Store, *, clock, rng, logger, metrics, event_bus=None,
                 default_lease_seconds: float = 300.0):
        self.store = store
        self.clock = clock
        self.rng = rng
        self.logger = logger
        self.metrics = metrics
        self.event_bus = event_bus
        self.default_lease_seconds = default_lease_seconds
        self.metrics.counter("workflow.submitted", "workflows submitted")
        self.metrics.counter("workflow.finished", "workflows finished")
        self.metrics.counter("workflow.failed", "workflows failed")
        self.metrics.counter("workflow.cancelled", "workflows cancelled")
        self.metrics.counter("workflow.deadletter", "workflows dead-lettered")
        self.metrics.histogram("workflow.node_latency", "node execution latency seconds")

    # ------------------------------------------------------------------ commands

    def submit(self, namespace: str, data: dict[str, Any], *, trace_id: str = "") -> Workflow:
        wf = Workflow.create(namespace, data, self.clock.now(), trace_id=trace_id)
        self.store.put_workflow(wf)
        self.metrics.counter("workflow.submitted").inc()
        self.logger.log("workflow.submitted", info=True, workflow=wf.id, namespace=namespace,
                        nodes=len(wf.nodes), schedule=wf.schedule)
        if wf.schedule:
            self.logger.log("workflow.scheduled", info=True, workflow=wf.id, schedule=wf.schedule)
        return wf

    def run_clone(self, template_id: str, *, trace_id: str = "") -> Workflow:
        """Cron instantiation: clone a template into a fresh run whose nodes start pending."""
        snap = self.store.snapshot()
        template = snap.workflow(template_id)
        if template is None:
            raise NotFoundError(f"workflow template {template_id} not found")
        run = copy.deepcopy(template)
        run.id = f"{template.id}.{int(self.clock.now() * 1000)}"
        run.status = WorkflowStatus.PENDING
        run.schedule = None
        run.cancel_requested = False
        run.error = None
        run.trace_id = trace_id
        run.epoch += 1
        for node in run.nodes.values():
            node.status = NodeStatus.PENDING
            node.attempts = 0
            node.next_retry_at = 0.0
            node.error = None
            node.finished_at = None
            node.started_at = None
        self.store.put_workflow(run)
        self.metrics.counter("workflow.submitted").inc()
        return run

    def cancel(self, workflow_id: str, *, trace_id: str = "") -> Workflow:
        snap = self.store.snapshot()
        wf = snap.workflow(workflow_id)
        if wf is None:
            raise NotFoundError(f"workflow {workflow_id} not found")
        if wf.status in (WorkflowStatus.SUCCEEDED, WorkflowStatus.FAILED, WorkflowStatus.TIMED_OUT,
                         WorkflowStatus.CANCELLED):
            raise ConflictError(f"workflow already terminal: {wf.status.value}")
        wf = copy.deepcopy(wf)
        wf.cancel_requested = True
        for node in wf.nodes.values():
            if node.status in (NodeStatus.PENDING, NodeStatus.DISPATCHED):
                transition("node", node.status, NodeStatus.CANCELLED)
                node.status = NodeStatus.CANCELLED
                node.finished_at = self.clock.now()
                node.error = "cancelled"
            elif node.status == NodeStatus.RUNNING:
                node.error = "cancel requested"
        # running attempts are revoked: their completion is ignored as cancelled
        for attempt in snap.attempts.values():
            if attempt.workflow_id == workflow_id and attempt.status == AttemptStatus.RUNNING:
                attempt = copy.deepcopy(attempt)
                transition("attempt", attempt.status, AttemptStatus.CANCELLED)
                attempt.status = AttemptStatus.CANCELLED
                attempt.finished_at = self.clock.now()
                attempt.error = "cancelled"
                self.store.put_attempt(attempt)
        wf.updated_at = self.clock.now()
        self.store.put_workflow(wf)
        self.metrics.counter("workflow.cancelled").inc()
        self.logger.log("workflow.cancel_requested", info=True, workflow=workflow_id)
        self._maybe_finalize(wf.id, trace_id=trace_id)
        return wf

    def retry_node(self, workflow_id: str, node_id: str, *, trace_id: str = "") -> NodeStatus:
        snap = self.store.snapshot()
        wf = snap.workflow(workflow_id)
        if wf is None:
            raise NotFoundError(f"workflow {workflow_id} not found")
        node = wf.nodes.get(node_id)
        if node is None:
            raise NotFoundError(f"node {node_id} not found in workflow")
        if node.status not in (NodeStatus.FAILED, NodeStatus.TIMED_OUT):
            raise ConflictError(f"node is not retryable (status={node.status.value})")
        wf = copy.deepcopy(wf)
        node = wf.nodes[node_id]
        node.status = NodeStatus.PENDING
        node.error = None
        node.finished_at = None
        node.next_retry_at = 0.0
        wf.status = WorkflowStatus.RUNNING
        wf.updated_at = self.clock.now()
        self.store.put_workflow(wf)
        self.logger.log("workflow.node_retry", info=True, workflow=workflow_id, node=node_id)
        return node.status

    # ------------------------------------------------------------------ completion

    def attempt_complete(self, attempt_id: str, *, ok: bool, error: str | None = None,
                         trace_id: str = "", timed_out: bool = False) -> None:
        snap = self.store.snapshot()
        attempt = snap.attempts.get(attempt_id)
        if attempt is None or attempt.status != AttemptStatus.RUNNING:
            return  # stale/revoked completion — idempotent no-op
        wf = snap.workflow(attempt.workflow_id)
        if wf is None:
            return
        wf = copy.deepcopy(wf)
        node = wf.nodes.get(attempt.node_id)
        if node is None:
            return
        attempt = copy.deepcopy(attempt)
        now = self.clock.now()
        attempt.finished_at = now
        attempt.error = error
        if wf.cancel_requested:
            target = AttemptStatus.CANCELLED
        elif timed_out:
            target = AttemptStatus.TIMED_OUT
        elif ok:
            target = AttemptStatus.SUCCEEDED
        else:
            target = AttemptStatus.FAILED
        transition("attempt", attempt.status, target)
        attempt.status = target
        self.store.put_attempt(attempt)

        if wf.cancel_requested:
            if node.status == NodeStatus.RUNNING:
                transition("node", node.status, NodeStatus.CANCELLED)
                node.status = NodeStatus.CANCELLED
                node.finished_at = now
                node.error = "cancelled"
        elif ok and not timed_out:
            transition("node", node.status, NodeStatus.SUCCEEDED)
            node.status = NodeStatus.SUCCEEDED
            node.attempts = attempt.attempt_no
            node.finished_at = now
            node.started_at = attempt.started_at
            node.error = None
            self.metrics.histogram("workflow.node_latency").observe(max(0.0, now - attempt.started_at))
        else:
            node.attempts = attempt.attempt_no
            terminal = NodeStatus.TIMED_OUT if timed_out else NodeStatus.FAILED
            if node.attempts <= node.retry_max:
                # retryable: node returns to pending with backoff (timeouts retry too)
                transition("node", node.status, NodeStatus.PENDING)
                node.status = NodeStatus.PENDING
                node.next_retry_at = now + _backoff(node, self.rng)
                node.error = error or ("node timed out" if timed_out else "attempt failed")
                node.finished_at = None
            else:
                node.status = terminal
                node.finished_at = now
                node.error = error or ("node timed out" if timed_out else "attempt failed")
        wf.updated_at = now
        self.store.put_workflow(wf)
        self._maybe_finalize(wf.id, trace_id=trace_id)

    # ------------------------------------------------------------------ pump

    def tick(self, now: float | None = None) -> None:
        now = now if now is not None else self.clock.now()
        snap = self.store.snapshot()
        for wf_id in list(snap.workflows.keys()):
            wf = snap.workflow(wf_id)
            if wf is None or wf.status not in (WorkflowStatus.PENDING, WorkflowStatus.RUNNING):
                continue
            if wf.timeout and now > wf.created_at + wf.timeout:
                self._timeout_workflow(wf_id, trace_id=wf.trace_id)
                continue
            self._dispatch_ready(wf_id, now)
        self._sweep_timed_out_nodes(now)

    def recover(self, now: float | None = None) -> None:
        """Boot-time reconciliation: fail orphaned attempts, resume dispatch."""
        now = now if now is not None else self.clock.now()
        snap = self.store.snapshot()
        for attempt in list(snap.attempts.values()):
            if attempt.status != AttemptStatus.RUNNING:
                continue
            self.attempt_complete(attempt.id, ok=False, error="recovered_after_restart",
                                  trace_id=attempt.trace_id)
        # workflows whose last writer died mid-transition are re-pumped by tick()
        for wf in snap.workflows.values():
            if wf.status == WorkflowStatus.RUNNING:
                self.logger.log("workflow.recovered", info=True, workflow=wf.id)

    # ------------------------------------------------------------------ internals

    def _dispatch_ready(self, wf_id: str, now: float) -> None:
        snap = self.store.snapshot()
        wf = snap.workflow(wf_id)
        if wf is None or wf.status not in (WorkflowStatus.PENDING, WorkflowStatus.RUNNING) or wf.cancel_requested:
            return
        wf = copy.deepcopy(wf)
        # desired status per node id: skips/dispatches we intend to persist
        should_be: dict[str, tuple[NodeStatus, str | None]] = {}
        dispatched = False

        for nid in list(wf.nodes.keys()):
            node = wf.nodes[nid]
            if node.status != NodeStatus.PENDING or now < node.next_retry_at:
                continue
            deps = [wf.nodes[d] for d in node.depends_on]
            if any(d.status in (NodeStatus.FAILED, NodeStatus.TIMED_OUT) for d in deps):
                should_be[nid] = (NodeStatus.SKIPPED, "dependency failed")
                continue
            if any(d.status == NodeStatus.CANCELLED for d in deps):
                should_be[nid] = (NodeStatus.SKIPPED, "dependency cancelled")
                continue
            if not all(d.status == NodeStatus.SUCCEEDED for d in deps):
                continue
            snap = self.store.snapshot()  # fresh: sibling dispatch may have completed nodes
            active = sum(
                1 for n in snap.workflow(wf_id).nodes.values()
                if n.status in (NodeStatus.DISPATCHED, NodeStatus.RUNNING)
            )
            if active >= node.max_concurrency:
                continue  # bound parallel workload; defer to next tick
            if any(a.workflow_id == wf_id and a.node_id == nid and
                   a.status == AttemptStatus.RUNNING for a in snap.attempts.values()):
                continue  # duplicate dispatch guard (double-tick safety)
            if self._dispatch_node(wf, nid, now):
                dispatched = True
                should_be[nid] = (NodeStatus.DISPATCHED, None)
                # re-read: an event node may have completed to terminal inside _dispatch_node
                wf = copy.deepcopy(self.store.snapshot().workflow(wf_id))

        if not should_be and not dispatched:
            return
        # Phase B: persist only nodes that are still PENDING (never clobber terminal states)
        fresh = self.store.snapshot().workflow(wf_id)
        if fresh is None or fresh.status in (WorkflowStatus.SUCCEEDED, WorkflowStatus.FAILED,
                                             WorkflowStatus.TIMED_OUT, WorkflowStatus.CANCELLED):
            return
        fresh = copy.deepcopy(fresh)
        mutated = False
        for nid, (status, error) in should_be.items():
            node = fresh.nodes.get(nid)
            if node is None or node.status != NodeStatus.PENDING:
                continue  # already terminal (e.g., event node finished) — do not clobber
            transition("node", node.status, status)
            node.status = status
            node.error = error if status == NodeStatus.SKIPPED else node.error
            if status == NodeStatus.SKIPPED:
                node.finished_at = now
            mutated = True
        if fresh.status == WorkflowStatus.PENDING:
            transition("workflow", fresh.status, WorkflowStatus.RUNNING)
            fresh.status = WorkflowStatus.RUNNING
            mutated = True
        if mutated:
            fresh.updated_at = now
            self.store.put_workflow(fresh)
        self._maybe_finalize(wf_id, trace_id=fresh.trace_id)

    def _dispatch_node(self, wf: Workflow, node_id: str, now: float) -> bool:
        node = wf.nodes[node_id]
        attempt_no = node.attempts + 1
        lease_id: str | None = None
        if node.workload_id:
            snap = self.store.snapshot()
            # use an active slot of the workload (oldest first) — a lease is a slot, not an exec
            slots = [l for l in snap.leases.values()
                     if l.workload_id == node.workload_id and l.state.value == "active" and l.expires_at > now]
            if not slots:
                return False  # no slot: node stays pending; scheduler will create one (backpressure)
            slots.sort(key=lambda l: (l.created_at, l.id))
            lease_id = slots[0].id
        attempt = Attempt.start(wf.id, node.id, attempt_no, lease_id, now, wf.trace_id)
        self.store.put_attempt(attempt)
        node.started_at = now
        transition("node", node.status, NodeStatus.DISPATCHED)
        node.status = NodeStatus.DISPATCHED
        # persist the dispatched state BEFORE the attempt can complete (event nodes
        # complete inside this call; completion readers must see DISPATCHED, not PENDING)
        self.store.put_workflow(copy.deepcopy(wf))

        if node.event_topic:
            # event nodes complete the moment the event is durably published (fire-and-continue)
            try:
                if self.event_bus is not None:
                    self.event_bus.publish(
                        node.event_topic, key=f"{wf.id}:{node.id}",
                        payload={"workflow_id": wf.id, "node_id": node.id,
                                 "workflow_name": wf.name, "attempt_no": attempt_no},
                        schema_version=1, trace_id=wf.trace_id,
                    )
                self.attempt_complete(attempt.id, ok=True, trace_id=wf.trace_id)
            except Exception as exc:  # noqa: BLE001 — backpressure publishes are retryable
                self.attempt_complete(attempt.id, ok=False, error=f"publish_failed: {exc}",
                                      trace_id=wf.trace_id)
        elif node.workload_id:
            self.logger.log("workflow.attempt_dispatched", info=True, workflow=wf.id,
                            node=node.id, attempt=attempt.id, lease=lease_id)
        return True

    def _sweep_timed_out_nodes(self, now: float) -> None:
        snap = self.store.snapshot()
        for attempt in list(snap.attempts.values()):
            if attempt.status != AttemptStatus.RUNNING:
                continue
            wf = snap.workflow(attempt.workflow_id)
            if wf is None:
                continue
            node = wf.nodes.get(attempt.node_id)
            if node is None or node.timeout is None:
                continue
            if now > attempt.started_at + node.timeout:
                self.attempt_complete(attempt.id, ok=False, error="node_timeout",
                                      trace_id=attempt.trace_id, timed_out=True)

    def _timeout_workflow(self, wf_id: str, *, trace_id: str = "") -> None:
        snap = self.store.snapshot()
        wf = snap.workflow(wf_id)
        if wf is None or wf.status not in (WorkflowStatus.PENDING, WorkflowStatus.RUNNING):
            return
        wf = copy.deepcopy(wf)
        wf.cancel_requested = True
        for node in wf.nodes.values():
            if node.status in (NodeStatus.PENDING, NodeStatus.DISPATCHED):
                node.status = NodeStatus.SKIPPED
                node.finished_at = self.clock.now()
                node.error = "workflow timeout"
        for attempt in snap.attempts.values():
            if attempt.workflow_id == wf_id and attempt.status == AttemptStatus.RUNNING:
                a = copy.deepcopy(attempt)
                transition("attempt", a.status, AttemptStatus.TIMED_OUT)
                a.status = AttemptStatus.TIMED_OUT
                a.finished_at = self.clock.now()
                a.error = "workflow timeout"
                self.store.put_attempt(a)
        wf.status = WorkflowStatus.TIMED_OUT
        wf.updated_at = self.clock.now()
        self.store.put_workflow(wf)
        self.metrics.counter("workflow.failed").inc()
        self.logger.log("workflow.timed_out", info=True, workflow=wf_id)

    def _maybe_finalize(self, wf_id: str, *, trace_id: str = "") -> None:
        snap = self.store.snapshot()
        wf = snap.workflow(wf_id)
        if wf is None or wf.status in (WorkflowStatus.SUCCEEDED, WorkflowStatus.FAILED,
                                       WorkflowStatus.TIMED_OUT, WorkflowStatus.CANCELLED):
            return
        nodes = list(wf.nodes.values())
        if not all(n.status in (NodeStatus.SUCCEEDED, NodeStatus.FAILED, NodeStatus.TIMED_OUT,
                                NodeStatus.CANCELLED, NodeStatus.SKIPPED) for n in nodes):
            return
        wf = copy.deepcopy(wf)
        if wf.cancel_requested or any(n.status == NodeStatus.CANCELLED for n in nodes):
            wf.status = WorkflowStatus.CANCELLED
        elif any(n.status == NodeStatus.TIMED_OUT for n in nodes):
            wf.status = WorkflowStatus.TIMED_OUT
        elif any(n.status == NodeStatus.FAILED for n in nodes):
            wf.status = WorkflowStatus.FAILED
        elif all(n.status == NodeStatus.SUCCEEDED for n in nodes):
            wf.status = WorkflowStatus.SUCCEEDED
        else:
            return
        wf.updated_at = self.clock.now()
        self.store.put_workflow(wf)
        self.metrics.counter("workflow.finished").inc()
        if wf.status == WorkflowStatus.FAILED and wf.dead_letter:
            self.metrics.counter("workflow.deadletter").inc()
            self.logger.log("workflow.deadletter", warn=True, workflow=wf.id, error=wf.error)
        if self.event_bus is not None:
            try:
                self.event_bus.publish(
                    "workflow.finished", key=wf_id,
                    payload={"workflow_id": wf_id, "name": wf.name, "namespace": wf.namespace,
                             "outcome": wf.status.value, "nodes": len(nodes)},
                    schema_version=1, trace_id=trace_id or wf.trace_id,
                )
            except Exception:  # noqa: BLE001 — finalize must never raise into the writer
                self.logger.log("workflow.finalize_publish_failed", error=True, workflow=wf_id)
        self.logger.log("workflow.finalized", info=True, workflow=wf.id, status=wf.status.value)

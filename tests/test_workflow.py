import unittest

from effective_scale.adapters import MemoryStore
from effective_scale.core.events import EventBus
from effective_scale.core.workflow import WorkflowEngine
from effective_scale.domain.errors import ValidationError
from effective_scale.domain.models import AttemptStatus, Lease, NodeStatus, WorkflowStatus
from effective_scale.domain.states import LeaseState
from effective_scale.observability.registry import Registry
from effective_scale.ports.logger import MemLogger
from effective_scale.ports.random import SecureRandom
from tests.helpers import FakeClock


def _lease(store, wid: str, now: float, lid: str = "slot1") -> None:
    store.put_lease(Lease(id=lid, workload_id=wid, namespace="ns", node_id="n1",
                          holder="h", nonce="n", expires_at=now + 1000,
                          state=LeaseState.ACTIVE, created_at=now))


class WorkflowEngineTest(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.store.open()
        self.clock = FakeClock(1_000_000.0)
        self.metrics = Registry()
        self.bus = EventBus(self.store, clock=self.clock, logger=MemLogger(), metrics=self.metrics)
        self.engine = WorkflowEngine(self.store, clock=self.clock, rng=SecureRandom(),
                                     logger=MemLogger(), metrics=self.metrics, event_bus=self.bus)
        # one workload with a slot so node execs can dispatch
        from effective_scale.domain.models import Workload

        self.wl = Workload.create("ns", {"name": "worker", "image": "img", "replicas": 1}, now=now_f())
        self.store.put_workload(self.wl)
        _lease(self.store, self.wl.id, now_f())

    def submit(self, nodes, **kw):
        return self.engine.submit("ns", {"name": kw.pop("name", "wf"), "nodes": nodes, **kw},
                                  trace_id="test")

    def test_linear_dag_success(self):
        wf = self.submit([
            {"id": "a", "exec": {"workload_id": self.wl.id}, "timeout": 60},
            {"id": "b", "depends_on": ["a"], "exec": {"workload_id": self.wl.id}, "timeout": 60},
        ])
        self.engine.tick(self.clock.now())
        snap = self.store.snapshot()
        node = snap.workflow(wf.id).nodes["a"]
        self.assertEqual(node.status, NodeStatus.DISPATCHED)
        attempts = [a for a in snap.attempts.values() if a.workflow_id == wf.id]
        self.assertEqual(len(attempts), 1)
        self.engine.attempt_complete(attempts[0].id, ok=True)
        self.engine.tick(self.clock.now())
        self.engine.attempt_complete(
            [a.id for a in self.store.snapshot().attempts.values()
             if a.workflow_id == wf.id and a.node_id == "b"][0], ok=True)
        self.engine.tick(self.clock.now())
        wf = self.store.snapshot().workflow(wf.id)
        self.assertEqual(wf.status, WorkflowStatus.SUCCEEDED)
        self.assertTrue(all(n.status == NodeStatus.SUCCEEDED for n in wf.nodes.values()))

    def test_cycle_rejected(self):
        with self.assertRaises(ValidationError):
            self.submit([
                {"id": "a", "depends_on": ["b"], "exec": {"workload_id": self.wl.id}},
                {"id": "b", "depends_on": ["a"], "exec": {"workload_id": self.wl.id}},
            ])

    def test_dangling_dependency_rejected(self):
        with self.assertRaises(ValidationError):
            self.submit([{"id": "a", "depends_on": ["ghost"], "exec": {"workload_id": self.wl.id}}])

    def test_retry_with_backoff_then_fail(self):
        wf = self.submit([
            {"id": "a", "exec": {"workload_id": self.wl.id}, "retry": {"max": 2, "base_seconds": 5}},
        ])
        self.engine.tick(self.clock.now())
        # attempt 1 -> retry after ~5s backoff
        a1 = [a for a in self.store.snapshot().attempts.values()
              if a.workflow_id == wf.id and a.status == AttemptStatus.RUNNING][0]
        self.engine.attempt_complete(a1.id, ok=False, error="boom")
        self.clock.advance(6.0)
        self.engine.tick(self.clock.now())
        # attempt 2 -> retry after ~10s backoff (exponential)
        a2 = [a for a in self.store.snapshot().attempts.values()
              if a.workflow_id == wf.id and a.status == AttemptStatus.RUNNING][0]
        self.engine.attempt_complete(a2.id, ok=False, error="boom")
        self.clock.advance(12.0)
        self.engine.tick(self.clock.now())
        # attempt 3 fails -> retry budget (2) exhausted -> terminal FAILED
        a3 = [a for a in self.store.snapshot().attempts.values()
              if a.workflow_id == wf.id and a.status == AttemptStatus.RUNNING][0]
        self.engine.attempt_complete(a3.id, ok=False, error="boom")
        self.engine.tick(self.clock.now())
        wf = self.store.snapshot().workflow(wf.id)
        self.assertEqual(wf.nodes["a"].status, NodeStatus.FAILED)
        self.assertEqual(wf.status, WorkflowStatus.FAILED)

    def test_success_after_retry(self):
        wf = self.submit([
            {"id": "a", "exec": {"workload_id": self.wl.id}, "retry": {"max": 3, "base_seconds": 2}},
        ])
        self.engine.tick(self.clock.now())
        aid = [a for a in self.store.snapshot().attempts.values()
               if a.workflow_id == wf.id][0].id
        self.engine.attempt_complete(aid, ok=False, error="flaky")
        self.clock.advance(3.0)
        self.engine.tick(self.clock.now())
        aid = [a for a in self.store.snapshot().attempts.values()
               if a.workflow_id == wf.id and a.status == AttemptStatus.RUNNING][0].id
        self.engine.attempt_complete(aid, ok=True)
        self.engine.tick(self.clock.now())
        wf = self.store.snapshot().workflow(wf.id)
        self.assertEqual(wf.status, WorkflowStatus.SUCCEEDED)
        self.assertEqual(wf.nodes["a"].attempts, 2)

    def test_node_timeout_then_retry_then_terminal(self):
        wf = self.submit([
            {"id": "a", "exec": {"workload_id": self.wl.id}, "timeout": 10, "retry": {"max": 0}},
        ])
        self.engine.tick(self.clock.now())
        self.clock.advance(11.0)
        self.engine.tick(self.clock.now())
        wf = self.store.snapshot().workflow(wf.id)
        self.assertEqual(wf.nodes["a"].status, NodeStatus.TIMED_OUT)
        self.assertEqual(wf.status, WorkflowStatus.TIMED_OUT)

    def test_cancel_revokes_running_and_finalizes(self):
        wf = self.submit([{"id": "a", "exec": {"workload_id": self.wl.id}, "timeout": 60}])
        self.engine.tick(self.clock.now())
        self.engine.cancel(wf.id)
        wf = self.store.snapshot().workflow(wf.id)
        self.assertEqual(wf.status, WorkflowStatus.CANCELLED)
        self.assertTrue(all(a.status == AttemptStatus.CANCELLED
                            for a in self.store.snapshot().attempts.values()
                            if a.workflow_id == wf.id))
        # late completion is ignored (no state corruption)
        attempt = [a for a in self.store.snapshot().attempts.values() if a.workflow_id == wf.id][0]
        self.engine.attempt_complete(attempt.id, ok=True)
        self.assertEqual(self.store.snapshot().workflow(wf.id).status, WorkflowStatus.CANCELLED)

    def test_fan_out_and_join(self):
        wf = self.submit([
            {"id": "root", "exec": {"workload_id": self.wl.id}, "timeout": 60},
            {"id": "l", "depends_on": ["root"], "exec": {"workload_id": self.wl.id}, "timeout": 60},
            {"id": "r", "depends_on": ["root"], "exec": {"workload_id": self.wl.id}, "timeout": 60},
            {"id": "join", "depends_on": ["l", "r"], "exec": {"workload_id": self.wl.id}, "timeout": 60},
        ])
        self.engine.tick(self.clock.now())
        snap = self.store.snapshot()
        running = [a for a in snap.attempts.values() if a.workflow_id == wf.id]
        self.assertEqual(len(running), 1)
        self.engine.attempt_complete(running[0].id, ok=True)
        self.engine.tick(self.clock.now())
        branches = [a for a in self.store.snapshot().attempts.values()
                    if a.workflow_id == wf.id and a.node_id in ("l", "r")]
        self.assertEqual(len(branches), 2)
        for a in branches:
            self.engine.attempt_complete(a.id, ok=True)
        self.engine.tick(self.clock.now())
        join = [a for a in self.store.snapshot().attempts.values()
                if a.workflow_id == wf.id and a.node_id == "join"]
        self.assertEqual(len(join), 1)
        self.engine.attempt_complete(join[0].id, ok=True)
        self.engine.tick(self.clock.now())
        self.assertEqual(self.store.snapshot().workflow(wf.id).status, WorkflowStatus.SUCCEEDED)

    def test_event_node_publishes_and_completes(self):
        wf = self.submit([
            {"id": "emit", "event_topic": "etl.ready", "timeout": 30},
        ])
        self.engine.tick(self.clock.now())
        wf = self.store.snapshot().workflow(wf.id)
        self.assertEqual(wf.status, WorkflowStatus.SUCCEEDED)
        events = self.store.snapshot().events_for("etl.ready", 0) + \
            self.store.snapshot().events_for("etl.ready", 1) + \
            self.store.snapshot().events_for("etl.ready", 2) + \
            self.store.snapshot().events_for("etl.ready", 3)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].payload["node_id"], "emit")

    def test_no_slot_means_node_stays_pending(self):
        self.store.delete_lease("slot1")
        wf = self.submit([{"id": "a", "exec": {"workload_id": self.wl.id}, "timeout": 60}])
        self.engine.tick(self.clock.now())
        snap = self.store.snapshot()
        self.assertEqual(snap.workflow(wf.id).nodes["a"].status, NodeStatus.PENDING)
        self.assertEqual([a for a in snap.attempts.values() if a.workflow_id == wf.id], [])

    def test_recover_fails_orphaned_attempts_and_requeues(self):
        wf = self.submit([{"id": "a", "exec": {"workload_id": self.wl.id},
                           "timeout": 60, "retry": {"max": 3, "base_seconds": 0}}])
        self.engine.tick(self.clock.now())
        snap = self.store.snapshot()
        running = [a for a in snap.attempts.values() if a.workflow_id == wf.id][0]
        self.engine.recover(self.clock.now())
        # orphaned attempt failed, retry scheduled, node back to pending
        failed = self.store.snapshot().attempts[running.id]
        self.assertEqual(failed.status, AttemptStatus.FAILED)
        self.assertEqual(failed.error, "recovered_after_restart")
        self.assertEqual(self.store.snapshot().workflow(wf.id).status, WorkflowStatus.RUNNING)
        self.engine.tick(self.clock.now())
        attempts = [a for a in self.store.snapshot().attempts.values() if a.workflow_id == wf.id]
        self.assertEqual(len(attempts), 2)  # retried once, no duplicates beyond that
        self.assertEqual(attempts[1].attempt_no, 2)


def now_f() -> float:
    return 1_000_000.0


if __name__ == "__main__":
    unittest.main()

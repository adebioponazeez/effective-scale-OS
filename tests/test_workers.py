"""External worker protocol (ADR-006): discover -> claim -> heartbeat -> complete.

Two levels:
  * engine-level (FakeClock, no HTTP): fencing, namespaces, lease expiry sweeps;
  * HTTP-level (Harness): the exact loop an out-of-process worker runs.
"""
import unittest

from effective_scale.adapters import MemoryStore
from effective_scale.core.events import EventBus
from effective_scale.core.kernel import Config, Kernel
from effective_scale.core.workflow import WorkflowEngine
from effective_scale.domain.errors import ConflictError, NotFoundError
from effective_scale.domain.models import AttemptStatus, Lease, NodeStatus, Workload, WorkflowStatus
from effective_scale.domain.states import LeaseState
from effective_scale.observability.registry import Registry
from effective_scale.ports.logger import MemLogger
from effective_scale.ports.random import SecureRandom
from tests.helpers import FakeClock, Harness, wait_until


def _lease(store, wid: str, now: float, lid: str = "slot1", ttl: float = 1000.0) -> None:
    store.put_lease(Lease(id=lid, workload_id=wid, namespace="ns", node_id="n1",
                          holder="kernel", nonce="kernel-nonce", expires_at=now + ttl,
                          state=LeaseState.ACTIVE, created_at=now))


class EngineWorkerProtocolTest(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.store.open()
        self.clock = FakeClock(1_000_000.0)
        self.metrics = Registry()
        self.bus = EventBus(self.store, clock=self.clock, logger=MemLogger(), metrics=self.metrics)
        self.engine = WorkflowEngine(self.store, clock=self.clock, rng=SecureRandom(),
                                     logger=MemLogger(), metrics=self.metrics, event_bus=self.bus,
                                     default_lease_seconds=60.0)
        self.wl = Workload.create("ns", {"name": "worker", "image": "img", "replicas": 1},
                                  now=self.clock.now())
        self.store.put_workload(self.wl)
        _lease(self.store, self.wl.id, self.clock.now())

    def submit(self, nodes, **kw):
        return self.engine.submit("ns", {"name": kw.pop("name", "wf"), "nodes": nodes, **kw},
                                  trace_id="test")

    def running_attempt(self, wf_id: str, node_id: str = "a"):
        return [a for a in self.store.snapshot().attempts.values()
                if a.workflow_id == wf_id and a.node_id == node_id
                and a.status == AttemptStatus.RUNNING][0]

    def test_dispatch_creates_a_claimable_attempt(self):
        wf = self.submit([{"id": "a", "name": "cap://x", "exec": {"workload_id": self.wl.id},
                           "timeout": 60}])
        self.engine.tick(self.clock.now())
        a = self.running_attempt(wf.id)
        self.assertTrue(a.claimable)
        view = self.engine.attempts_view(namespace="ns", claimable=True)
        self.assertEqual([v["id"] for v in view], [a.id])
        self.assertEqual(view[0]["node_name"], "cap://x")
        self.assertEqual(view[0]["workflow_name"], "wf")
        # event nodes complete synchronously and are therefore never claimable
        wf2 = self.submit([{"id": "e", "event_topic": "t"}], name="wf2")
        self.engine.tick(self.clock.now())
        self.assertEqual(self.engine.attempts_view(namespace="ns", claimable=True,
                                                   workflow_id=wf2.id), [])

    def test_claim_binds_lease_and_fences_other_workers(self):
        wf = self.submit([{"id": "a", "exec": {"workload_id": self.wl.id}, "timeout": 60}])
        self.engine.tick(self.clock.now())
        a = self.running_attempt(wf.id)
        claimed = self.engine.claim_attempt(a.id, worker_id="w1", namespace="ns")
        self.assertTrue(claimed["nonce"])
        self.assertEqual(claimed["lease_id"], "slot1")
        self.assertEqual(self.store.snapshot().lease("slot1").holder, "w1")
        self.assertEqual(self.store.snapshot().lease("slot1").nonce, claimed["nonce"])
        # a different worker cannot take a live claim
        with self.assertRaises(ConflictError):
            self.engine.claim_attempt(a.id, worker_id="w2", namespace="ns")
        # the owner may re-claim, which rotates the token (stalled copy is fenced out)
        reclaimed = self.engine.claim_attempt(a.id, worker_id="w1", namespace="ns")
        self.assertNotEqual(reclaimed["nonce"], claimed["nonce"])
        with self.assertRaises(ConflictError):
            self.engine.attempt_complete(a.id, ok=True, namespace="ns", worker_id="w1",
                                         nonce=claimed["nonce"])
        self.engine.attempt_complete(a.id, ok=True, namespace="ns", worker_id="w1",
                                     nonce=reclaimed["nonce"])

    def test_completion_requires_live_nonce_after_claim(self):
        wf = self.submit([{"id": "a", "exec": {"workload_id": self.wl.id}, "timeout": 60}])
        self.engine.tick(self.clock.now())
        a = self.running_attempt(wf.id)
        self.engine.claim_attempt(a.id, worker_id="w1", namespace="ns")
        for bad in (None, "", "not-the-nonce"):
            with self.assertRaises(ConflictError):
                self.engine.attempt_complete(a.id, ok=True, namespace="ns", worker_id="w1", nonce=bad)
        # unclaimed attempts keep the legacy open contract (no fencing token issued)
        wf2 = self.submit([{"id": "a", "exec": {"workload_id": self.wl.id}, "timeout": 60}], name="wf2")
        self.engine.tick(self.clock.now())
        b = self.running_attempt(wf2.id)
        self.engine.attempt_complete(b.id, ok=True, namespace="ns")

    def test_namespace_scope_hides_other_tenants(self):
        wf = self.submit([{"id": "a", "exec": {"workload_id": self.wl.id}, "timeout": 60}])
        self.engine.tick(self.clock.now())
        a = self.running_attempt(wf.id)
        with self.assertRaises(NotFoundError):
            self.engine.claim_attempt(a.id, worker_id="w1", namespace="other")
        with self.assertRaises(NotFoundError):
            self.engine.attempt_complete(a.id, ok=True, namespace="other")
        with self.assertRaises(NotFoundError):
            self.engine.heartbeat_attempt(a.id, worker_id="w1", nonce="x", namespace="other")
        self.assertEqual(self.engine.attempts_view(namespace="other"), [])

    def test_heartbeat_renews_lease_and_is_fenced(self):
        wf = self.submit([{"id": "a", "exec": {"workload_id": self.wl.id}, "timeout": 600}])
        self.engine.tick(self.clock.now())
        a = self.running_attempt(wf.id)
        claimed = self.engine.claim_attempt(a.id, worker_id="w1", namespace="ns", ttl=30.0)
        first_expiry = claimed["lease_expires_at"]
        self.clock.advance(10)
        out = self.engine.heartbeat_attempt(a.id, worker_id="w1", nonce=claimed["nonce"],
                                            namespace="ns", ttl=30.0)
        self.assertGreater(out["lease_expires_at"], first_expiry)
        self.assertEqual(self.store.snapshot().attempts[a.id].deadline, out["deadline"])
        with self.assertRaises(ConflictError):
            self.engine.heartbeat_attempt(a.id, worker_id="w1", nonce="bogus", namespace="ns")
        with self.assertRaises(ConflictError):
            self.engine.heartbeat_attempt(a.id, worker_id="w2", nonce=claimed["nonce"], namespace="ns")

    def test_claim_rejects_dead_lease(self):
        wf = self.submit([{"id": "a", "exec": {"workload_id": self.wl.id}, "timeout": 60}])
        self.engine.tick(self.clock.now())
        a = self.running_attempt(wf.id)
        lease = self.store.snapshot().lease("slot1")
        lease = Lease(id=lease.id, workload_id=lease.workload_id, namespace=lease.namespace,
                      node_id=lease.node_id, holder=lease.holder, nonce=lease.nonce,
                      expires_at=self.clock.now() - 1, state=lease.state, created_at=lease.created_at)
        self.store.put_lease(lease)
        with self.assertRaises(ConflictError):
            self.engine.claim_attempt(a.id, worker_id="w1", namespace="ns")

    def test_expired_lease_fails_attempt_and_retries_node(self):
        wf = self.submit([{"id": "a", "exec": {"workload_id": self.wl.id}, "timeout": 600,
                           "retry": {"max": 3, "base_seconds": 0, "jitter": 0}}])
        self.engine.tick(self.clock.now())
        a = self.running_attempt(wf.id)
        self.engine.claim_attempt(a.id, worker_id="w1", namespace="ns", ttl=30.0)
        self.engine.tick(self.clock.now())
        # still inside the lease: the attempt is untouched
        self.assertEqual(self.store.snapshot().attempts[a.id].status, AttemptStatus.RUNNING)
        self.clock.advance(31)
        self.engine.tick(self.clock.now())
        failed = self.store.snapshot().attempts[a.id]
        self.assertEqual(failed.status, AttemptStatus.FAILED)
        self.assertEqual(failed.error, "lease_expired")
        self.assertEqual(self.store.snapshot().workflow(wf.id).nodes["a"].status, NodeStatus.PENDING)
        # no live slot -> still pending (backpressure, exactly like "no slot" today)
        self.engine.tick(self.clock.now())
        self.assertEqual(self.store.snapshot().workflow(wf.id).nodes["a"].status, NodeStatus.PENDING)
        # once the scheduler replaces the dead slot, the node dispatches attempt 2
        _lease(self.store, self.wl.id, self.clock.now(), lid="slot2")
        self.engine.tick(self.clock.now())
        attempts = [x for x in self.store.snapshot().attempts.values() if x.workflow_id == wf.id]
        self.assertEqual(len(attempts), 2)
        self.assertEqual({a.attempt_no for a in attempts}, {1, 2})

    def test_result_is_recorded_and_completion_is_idempotent(self):
        wf = self.submit([{"id": "a", "exec": {"workload_id": self.wl.id}, "timeout": 60}])
        self.engine.tick(self.clock.now())
        a = self.running_attempt(wf.id)
        claimed = self.engine.claim_attempt(a.id, worker_id="w1", namespace="ns")
        payload = {"summary": "done", "evidence_hash": "abc123"}
        out = self.engine.attempt_complete(a.id, ok=True, namespace="ns", worker_id="w1",
                                           nonce=claimed["nonce"], result=payload)
        self.assertFalse(out["idempotent"])
        self.assertEqual(out["node_status"], "succeeded")
        again = self.engine.attempt_complete(a.id, ok=True, namespace="ns", worker_id="w1",
                                             nonce=claimed["nonce"])
        self.assertTrue(again["idempotent"])
        self.assertEqual(self.store.snapshot().attempts[a.id].result, payload)
        self.assertEqual(self.store.snapshot().workflow(wf.id).status, WorkflowStatus.SUCCEEDED)

    def test_cancel_revokes_claimed_attempt(self):
        wf = self.submit([{"id": "a", "exec": {"workload_id": self.wl.id}, "timeout": 60}])
        self.engine.tick(self.clock.now())
        a = self.running_attempt(wf.id)
        claimed = self.engine.claim_attempt(a.id, worker_id="w1", namespace="ns")
        self.engine.cancel(wf.id)
        self.engine.attempt_complete(a.id, ok=True, namespace="ns", worker_id="w1",
                                     nonce=claimed["nonce"])
        snap = self.store.snapshot()
        self.assertEqual(snap.attempts[a.id].status, AttemptStatus.CANCELLED)
        self.assertEqual(snap.workflow(wf.id).status, WorkflowStatus.CANCELLED)


class ReapExpiredLeasesTest(unittest.TestCase):
    def test_kernel_reaps_expired_lease_and_frees_capacity(self):
        """Expired slots are reclaimed: bounded state, honest node accounting."""
        from effective_scale.domain.models import Node

        store = MemoryStore()
        store.open()
        clock = FakeClock(1_000_000.0)
        kernel = Kernel(store, config=Config(store_path=":memory:", auth_secret="x" * 20),
                        clock=clock, logger=MemLogger())
        node = Node.create({"name": "n1", "cpu": 1000, "memory": 1024}, clock.now())
        store.put_node(node)
        wl = Workload.create("ns", {"name": "w", "image": "i", "replicas": 1,
                                    "cpu": 100, "memory": 128}, now=clock.now())
        store.put_workload(wl)
        # simulate what the scheduler does on grant: the node consumes the slot
        node.used_cpu += wl.cpu
        node.used_mem += wl.memory
        store.put_node(node)
        store.put_lease(Lease(id="slot1", workload_id=wl.id, namespace="ns", node_id=node.id,
                              holder="w1", nonce="n", expires_at=clock.now() + 30,
                              state=LeaseState.ACTIVE, created_at=clock.now()))
        clock.advance(31)
        kernel._reap_expired_leases(clock.now())
        snap = store.snapshot()
        self.assertIsNone(snap.lease("slot1"))
        self.assertEqual(snap.node(node.id).used_cpu, 0)
        self.assertEqual(snap.node(node.id).used_mem, 0)
        self.assertEqual(snap.workload(wl.id).replicas, 0)  # slot returned to the pool
        self.assertTrue(any(e["action"] == "lease.expire" for e in snap.audit))
        # a live lease is never reaped
        store.put_lease(Lease(id="slot2", workload_id=wl.id, namespace="ns", node_id=node.id,
                              holder="w1", nonce="n", expires_at=clock.now() + 30,
                              state=LeaseState.ACTIVE, created_at=clock.now()))
        kernel._reap_expired_leases(clock.now())
        self.assertIsNotNone(store.snapshot().lease("slot2"))


class HttpWorkerProtocolTest(unittest.TestCase):
    """The exact loop an out-of-process SAF worker runs, over the public API."""

    def setUp(self):
        self.h = Harness()
        self.h.request("POST", "/v1/namespaces", {"name": "demo"}, admin=True)
        self.token = self.h.admin_token()
        self.h.request("POST", "/v1/nodes", {"name": "n1", "cpu": 2000, "memory": 4096}, admin=True)
        _, d, _ = self.h.request("POST", "/v1/workloads",
                                 {"name": "saf-worker", "image": "saf", "replicas": 1},
                                 token=self.token)
        self.wl_id = d["workload"]["id"]

    def tearDown(self):
        self.h.stop()

    def submit(self, nodes):
        _, d, _ = self.h.request("POST", "/v1/workflows", {"name": "saf/task", "nodes": nodes},
                                 token=self.token)
        return d["workflow"]["id"]

    def wait_claimable(self, wid):
        ok = wait_until(lambda: any(a.workflow_id == wid and a.claimable for a in
                                    self.h.kernel.store.snapshot().attempts.values()), timeout=5.0)
        self.assertTrue(ok, "attempt should become claimable")
        s, d, _ = self.h.request("GET", f"/v1/attempts?claimable=true&workflow_id={wid}",
                                 token=self.token)
        self.assertEqual(s, 200)
        self.assertEqual(d["count"], 1)
        return d["attempts"][0]

    def test_worker_loop_completes_a_two_node_workflow(self):
        wid = self.submit([
            {"id": "cap-0", "name": "cap://software/repository/inspect",
             "exec": {"workload_id": self.wl_id}, "timeout": 60, "max_concurrency": 1},
            {"id": "cap-1", "name": "cap://software/testing/execute", "depends_on": ["cap-0"],
             "exec": {"workload_id": self.wl_id}, "timeout": 60, "max_concurrency": 1},
        ])
        for step in range(2):
            attempt = self.wait_claimable(wid)
            self.assertEqual(attempt["node_name"],
                             ("cap://software/repository/inspect",
                              "cap://software/testing/execute")[step])
            self.assertFalse(attempt["claimed"])
            s, claimed, _ = self.h.request("POST", f"/v1/attempts/{attempt['id']}/claim",
                                           {"worker_id": "saf-1", "ttl_seconds": 60},
                                           token=self.token)
            self.assertEqual(s, 200, claimed)
            nonce = claimed["nonce"]
            s, hb, _ = self.h.request("POST", f"/v1/attempts/{attempt['id']}/heartbeat",
                                      {"worker_id": "saf-1", "nonce": nonce, "ttl_seconds": 60},
                                      token=self.token)
            self.assertEqual(s, 200, hb)
            s, done, _ = self.h.request("POST", f"/v1/attempts/{attempt['id']}/complete",
                                        {"ok": True, "worker_id": "saf-1", "nonce": nonce,
                                         "result": {"summary": "ok", "evidence_hash": "h" * 64}},
                                        token=self.token)
            self.assertEqual(s, 200, done)
        ok = wait_until(lambda: self.h.kernel.store.snapshot().workflow(wid).status.value == "succeeded",
                        timeout=5.0)
        self.assertTrue(ok)
        s, wf, _ = self.h.request("GET", f"/v1/workflows/{wid}", token=self.token)
        self.assertEqual(s, 200)
        self.assertEqual(len(wf["workflow"]["attempts"]), 2)
        self.assertEqual({a["result"]["evidence_hash"] for a in wf["workflow"]["attempts"]},
                         {"h" * 64})
        self.assertEqual({a["worker_id"] for a in wf["workflow"]["attempts"]}, {"saf-1"})

    def test_stale_nonce_is_rejected_over_http(self):
        wid = self.submit([{"id": "a", "name": "cap://x", "exec": {"workload_id": self.wl_id},
                            "timeout": 60}])
        attempt = self.wait_claimable(wid)
        s, claimed, _ = self.h.request("POST", f"/v1/attempts/{attempt['id']}/claim",
                                       {"worker_id": "saf-1"}, token=self.token)
        self.assertEqual(s, 200)
        s, d, _ = self.h.request("POST", f"/v1/attempts/{attempt['id']}/complete",
                                 {"ok": True, "worker_id": "saf-2", "nonce": "stale"},
                                 token=self.token)
        self.assertEqual(s, 409, d)
        self.assertEqual(d["error"]["code"], "conflict")
        s, d, _ = self.h.request("POST", f"/v1/attempts/{attempt['id']}/heartbeat",
                                 {"worker_id": "saf-1", "nonce": "stale"}, token=self.token)
        self.assertEqual(s, 409, d)
        # the legitimate holder still completes
        s, d, _ = self.h.request("POST", f"/v1/attempts/{attempt['id']}/complete",
                                 {"ok": True, "worker_id": "saf-1", "nonce": claimed["nonce"]},
                                 token=self.token)
        self.assertEqual(s, 200, d)

    def test_cross_namespace_worker_cannot_see_or_touch_attempts(self):
        wid = self.submit([{"id": "a", "name": "cap://x", "exec": {"workload_id": self.wl_id},
                            "timeout": 60}])
        attempt = self.wait_claimable(wid)
        self.h.request("POST", "/v1/namespaces", {"name": "other"}, admin=True)
        _, tok, _ = self.h.request("POST", "/v1/tokens",
                                   {"namespace": "other", "scopes": ["read", "write"]}, admin=True)
        other = tok["token"]
        s, d, _ = self.h.request("GET", "/v1/attempts", token=other)
        self.assertEqual(s, 200)
        self.assertEqual(d["count"], 0)
        s, d, _ = self.h.request("GET", f"/v1/attempts/{attempt['id']}", token=other)
        self.assertEqual(s, 404, d)
        s, d, _ = self.h.request("POST", f"/v1/attempts/{attempt['id']}/claim",
                                 {"worker_id": "w"}, token=other)
        self.assertEqual(s, 404, d)
        s, d, _ = self.h.request("POST", f"/v1/attempts/{attempt['id']}/complete",
                                 {"ok": True}, token=other)
        self.assertEqual(s, 404, d)

    def test_result_size_is_bounded(self):
        wid = self.submit([{"id": "a", "name": "cap://x", "exec": {"workload_id": self.wl_id},
                            "timeout": 60}])
        attempt = self.wait_claimable(wid)
        s, d, _ = self.h.request("POST", f"/v1/attempts/{attempt['id']}/complete",
                                 {"ok": False, "error": "boom",
                                  "result": {"blob": "x" * 70000}}, token=self.token)
        self.assertEqual(s, 400, d)
        self.assertEqual(d["error"]["code"], "validation_error")

    def test_attempt_requires_auth(self):
        wid = self.submit([{"id": "a", "name": "cap://x", "exec": {"workload_id": self.wl_id},
                            "timeout": 60}])
        attempt = self.wait_claimable(wid)
        s, d, _ = self.h.request("GET", "/v1/attempts")
        self.assertEqual(s, 401, d)
        s, d, _ = self.h.request("GET", f"/v1/attempts/{attempt['id']}")
        self.assertEqual(s, 401, d)


if __name__ == "__main__":
    unittest.main()

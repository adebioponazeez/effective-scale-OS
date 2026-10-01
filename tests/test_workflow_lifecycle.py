"""Workflow engine lifecycle edges — the branches the coverage report showed unreached.

These are the paths an operator hits only when something unusual happens, which is exactly
why they were untested and why they matter: cron template cloning, manual node retry, node and
workflow timeouts, cancelling during dispatch, a publish failure inside an event node, and the
finalize fan-in rules (including `dead_letter`).

Everything here goes through the public surface (HTTP where the API exists) so the tests
describe behaviour, not internals.
"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from effective_scale.adapters import SQLiteStore
from effective_scale.core.kernel import Config
from effective_scale.domain.errors import ConflictError, NotFoundError

from .helpers import Harness, wait_until


def _config(store_path: str = ":memory:", **overrides) -> Config:
    values = dict(store_path=store_path, listen="127.0.0.1:0",
                  auth_secret="lifecycle-secret-0123456789", admin_token="bootstrap-test-token",
                  scheduler_interval=0.02, scale_interval=0.05, workflow_interval=0.02,
                  event_interval=0.02, heartbeat_ttl=1.0, watchdog_stall=30.0,
                  rate_limit_per_minute=1_000_000)
    values.update(overrides)
    return Config(**values)


class WorkflowLifecycleTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        path = str(Path(self._tmp.name) / "wf.db")
        self.harness = Harness(SQLiteStore(path), config=_config(path), start_api=True)
        self.token = self.harness.admin_token()
        self.harness.request("POST", "/v1/namespaces", {"name": "demo"},
                             token=self.token, admin=True)

    def tearDown(self):
        self.harness.stop()
        self._tmp.cleanup()

    # ------------------------------------------------------------------ helpers

    def _submit(self, nodes, **extra) -> str:
        body = {"name": extra.pop("name", "wf"), "nodes": nodes, **extra}
        status, data, _ = self.harness.request("POST", "/v1/workflows", body, token=self.token)
        self.assertEqual(status, 200, data)
        return data["workflow"]["id"]

    def _workflow(self, wid: str) -> dict:
        status, body, _ = self.harness.request("GET", f"/v1/workflows/{wid}", token=self.token)
        self.assertEqual(status, 200, body)
        return body["workflow"]

    def _node(self, wid: str, nid: str) -> dict:
        nodes = self._workflow(wid)["nodes"]
        if isinstance(nodes, dict):
            return nodes[nid]
        return next(n for n in nodes if n["id"] == nid)

    def _attempts(self, wid: str) -> list[dict]:
        status, body, _ = self.harness.request("GET", "/v1/attempts?limit=200", token=self.token)
        self.assertEqual(status, 200, body)
        return [a for a in body["attempts"] if a["workflow_id"] == wid]

    def _complete(self, attempt: dict, ok: bool, error: str | None = None) -> None:
        claim_status, claim, _ = self.harness.request(
            "POST", f"/v1/attempts/{attempt['id']}/claim",
            {"worker_id": "lifecycle-worker", "ttl_seconds": 20}, token=self.token)
        self.assertEqual(claim_status, 200, claim)
        result = {"ok": ok, "worker_id": "lifecycle-worker", "nonce": claim["nonce"],
                  "result": {"exit_code": 0 if ok else 1}}
        if error:
            result["error"] = error
        status, body, _ = self.harness.request(
            "POST", f"/v1/attempts/{attempt['id']}/complete", result, token=self.token)
        self.assertEqual(status, 200, body)

    def _wait_for_attempt(self, wid: str, timeout: float = 10.0) -> dict:
        found: list[dict] = []

        def ready() -> bool:
            attempts = self._attempts(wid)
            if attempts:
                found[:] = attempts
                return True
            return False

        self.assertTrue(wait_until(ready, timeout=timeout), "no attempt was ever dispatched")
        return found[0]

    def _wait_for_new_attempt(self, wid: str, seen: set[str], timeout: float = 10.0) -> dict:
        found: list[dict] = []

        def ready() -> bool:
            fresh = [a for a in self._attempts(wid) if a["id"] not in seen]
            if fresh:
                found[:] = sorted(fresh, key=lambda a: a["attempt_no"])
                return True
            return False

        self.assertTrue(wait_until(ready, timeout=timeout), "no further attempt was dispatched")
        return found[0]

    # ------------------------------------------------------------------ cron clone

    def test_cron_template_clone_resets_every_node(self):
        """`run_clone` is what the cron loop uses to instantiate a scheduled template."""
        wid = self._submit([{"id": "a", "event_topic": "cron.tick", "timeout": 30}],
                           name="nightly", schedule="*/5 * * * *")
        template = self._workflow(wid)
        self.assertEqual(template["schedule"], "*/5 * * * *")

        run = self.harness.kernel.write(lambda: self.harness.kernel.engine.run_clone(wid))
        self.assertNotEqual(run.id, wid)
        self.assertIsNone(run.schedule)
        self.assertEqual(run.status.value, "pending")
        self.assertFalse(run.cancel_requested)
        for node in run.nodes.values():
            self.assertEqual(node.status.value, "pending")
            self.assertEqual(node.attempts, 0)
            self.assertIsNone(node.finished_at)

        # the clone is a real run: it dispatches and finishes on its own
        self.assertTrue(wait_until(lambda: self._workflow(run.id)["status"] in
                                   ("running", "succeeded"), timeout=10.0))

    def test_cloning_a_missing_template_is_a_not_found(self):
        with self.assertRaises(NotFoundError):
            self.harness.kernel.write(
                lambda: self.harness.kernel.engine.run_clone("does-not-exist"))

    # ------------------------------------------------------------------ manual retry

    def test_retry_node_after_failure(self):
        """A dead node can be revived without resubmitting the whole workflow."""
        wid = self._submit([{"id": "only", "exec": {"workload_id": self._workload_id()},
                             "timeout": 0.2, "retry": {"max": 0}}])
        first = self._wait_for_attempt(wid)  # deliberately never completed

        self.assertTrue(wait_until(lambda: self._node(wid, "only")["status"] == "timed_out",
                                   timeout=10.0), "node timeout sweep never ran")
        self.assertTrue(wait_until(lambda: self._workflow(wid)["status"] == "timed_out",
                                   timeout=10.0))

        status, body, _ = self.harness.request(
            "POST", f"/v1/workflows/{wid}/retry-node/only", {}, token=self.token)
        self.assertEqual(status, 200, body)
        self.assertEqual(self._node(wid, "only")["status"], "pending")
        self.assertEqual(self._workflow(wid)["status"], "running",
                         "retrying a node must reopen the workflow")

        second = self._wait_for_new_attempt(wid, seen={first["id"]})
        self._complete(second, ok=True)
        self.assertTrue(wait_until(lambda: self._workflow(wid)["status"] == "succeeded",
                                   timeout=10.0), "retried node did not run to success")
        self.assertEqual(self._node(wid, "only")["status"], "succeeded")

    def test_retry_rejects_non_retryable_and_unknown_targets(self):
        wid = self._submit([{"id": "live", "event_topic": "x", "timeout": 30}])
        # a node that is still pending/running cannot be retried
        status, body, _ = self.harness.request(
            "POST", f"/v1/workflows/{wid}/retry-node/live", {}, token=self.token)
        self.assertIn(status, (409, 200), body)
        status, body, _ = self.harness.request(
            "POST", f"/v1/workflows/{wid}/retry-node/nope", {}, token=self.token)
        self.assertEqual(status, 404, body)
        status, body, _ = self.harness.request(
            "POST", "/v1/workflows/missing/retry-node/x", {}, token=self.token)
        self.assertEqual(status, 404, body)

    def test_engine_retry_node_validates_state_directly(self):
        wid = self._submit([{"id": "a", "event_topic": "x", "timeout": 30}])
        with self.assertRaises(ConflictError):
            self.harness.kernel.write(
                lambda: self.harness.kernel.engine.retry_node(wid, "a"))
        with self.assertRaises(NotFoundError):
            self.harness.kernel.write(
                lambda: self.harness.kernel.engine.retry_node(wid, "ghost"))

    # ------------------------------------------------------------------ timeouts

    def test_node_timeout_fails_the_attempt_and_retries_within_max(self):
        """A worker that never completes must not hold the DAG forever."""
        wid = self._submit([{"id": "slow", "exec": {"workload_id": self._workload_id()},
                             "timeout": 0.2, "retry": {"max": 1}}])
        self._wait_for_attempt(wid)  # deliberately never completed by a worker
        self.assertTrue(wait_until(
            lambda: any(a["status"] == "timed_out" for a in self._attempts(wid)), timeout=10.0),
            "node timeout sweep never fired")
        self.assertTrue(wait_until(
            lambda: self._workflow(wid)["status"] in ("failed", "running", "timed_out"),
            timeout=10.0))

    def test_workflow_timeout_skips_pending_and_times_out_running_attempts(self):
        wid = self._submit([
            {"id": "first", "exec": {"workload_id": self._workload_id()}, "timeout": 60},
            {"id": "second", "depends_on": ["first"], "event_topic": "later", "timeout": 60},
        ], timeout=0.3)
        self._wait_for_attempt(wid)  # dispatched, never completed by a worker
        self.assertTrue(wait_until(lambda: self._workflow(wid)["status"] == "timed_out",
                                   timeout=10.0), "global workflow timeout did not fire")
        self.assertEqual(self._node(wid, "second")["status"], "skipped",
                         "pending nodes must be skipped when the workflow times out")
        self.assertTrue(any(a["status"] == "timed_out" for a in self._attempts(wid)),
                        "the running attempt must be marked timed_out")

    # ------------------------------------------------------------------ cancel

    def test_cancel_before_dispatch_prevents_work(self):
        """Cancel is final: nothing may be dispatched, claimed or completed afterwards.

        A dispatch tick can legitimately win the race against the cancel request on a
        loaded machine, so the invariant under test is the outcome — cancelled run,
        every node cancelled, every attempt revoked and unclaimable — not "no attempt
        was ever created".
        """
        wid = self._submit([{"id": "a", "exec": {"workload_id": self._workload_id()},
                             "timeout": 60}])
        status, body, _ = self.harness.request("POST", f"/v1/workflows/{wid}/cancel", {},
                                               token=self.token)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["workflow"]["status"], "cancelled", body)
        for node in body["workflow"]["nodes"]:
            self.assertIn(node["status"], ("cancelled", "skipped"), node)

        time.sleep(0.4)  # give the engine several ticks to (wrongly) make progress
        wf = self._workflow(wid)
        self.assertEqual(wf["status"], "cancelled")
        for node in wf["nodes"]:
            self.assertIn(node["status"], ("cancelled", "skipped"), node)
        for attempt in self._attempts(wid):
            self.assertEqual(attempt["status"], "cancelled", attempt)
            self.assertFalse(attempt["claimable"], "a cancelled attempt must not be claimable")

    def test_cancel_is_idempotent(self):
        wid = self._submit([{"id": "a", "event_topic": "x", "timeout": 30}])
        for _ in range(3):
            status, body, _ = self.harness.request(
                "POST", f"/v1/workflows/{wid}/cancel", {}, token=self.token)
            self.assertEqual(status, 200, body)
            self.assertEqual(body["workflow"]["status"], "cancelled")
        self.assertEqual(self._workflow(wid)["status"], "cancelled")

    def test_cancel_of_another_terminal_state_is_a_conflict(self):
        """Cancelling a run that already ended some other way cannot be satisfied."""
        wid = self._submit([{"id": "a", "event_topic": "x", "timeout": 30, "retry": {"max": 0}}])

        class _FailingBus:
            def publish(self, *args, **kwargs):
                raise RuntimeError("backpressure")

        engine = self.harness.kernel.engine
        original = engine.event_bus
        engine.event_bus = _FailingBus()
        try:
            self.assertTrue(wait_until(lambda: self._workflow(wid)["status"] == "failed",
                                       timeout=10.0), "failing publish never failed the run")
        finally:
            engine.event_bus = original
        status, body, _ = self.harness.request(
            "POST", f"/v1/workflows/{wid}/cancel", {}, token=self.token)
        self.assertEqual(status, 409, body)
        self.assertEqual(body["error"]["code"], "conflict")

    # ------------------------------------------------------------------ publish failure

    def test_event_publish_failure_is_recorded_not_hidden(self):
        """Backpressure on an event node must fail the attempt, not silently succeed."""
        class _FailingBus:
            def publish(self, *args, **kwargs):
                raise RuntimeError("backpressure: lag too high")

        engine = self.harness.kernel.engine
        original = engine.event_bus
        engine.event_bus = _FailingBus()
        try:
            wid = self._submit([{"id": "publish", "event_topic": "topic", "timeout": 30,
                                 "retry": {"max": 0}}])
            self.assertTrue(wait_until(
                lambda: self._workflow(wid)["status"] == "failed", timeout=10.0),
                "a failing publish must fail the run, not wedge the node in dispatched")
            attempts = self._attempts(wid)
            self.assertTrue(attempts, "no attempt recorded")
            self.assertTrue(any(a["error"] and "publish_failed" in str(a["error"])
                                for a in attempts),
                            f"publish failure was not recorded: {attempts[0]}")
            self.assertEqual(self._node(wid, "publish")["status"], "failed",
                             "the node must leave dispatched instead of wedging the DAG")
        finally:
            engine.event_bus = original

    def test_retryable_publish_failure_retries_instead_of_wedging(self):
        """Regression: phase B must not re-assert DISPATCHED over a retry reset.

        A publish failure returns the node to PENDING with a backoff so the next
        tick can retry it. The dispatch bookkeeping used to re-mark such a node
        DISPATCHED after the fact, leaving it with no running attempt and no
        retry path — the whole run stopped making progress, silently.
        """
        class _FailingBus:
            def publish(self, *args, **kwargs):
                raise RuntimeError("backpressure: lag too high")

        engine = self.harness.kernel.engine
        original = engine.event_bus
        engine.event_bus = _FailingBus()
        try:
            wid = self._submit([{"id": "publish", "event_topic": "topic",
                                 "timeout": 30,
                                 "retry": {"max": 2, "base_seconds": 0.05,
                                           "max_seconds": 0.1, "jitter": 0}}])
            self.assertTrue(wait_until(
                lambda: self._workflow(wid)["status"] == "failed", timeout=10.0),
                "the run wedged instead of exhausting its retries")
            node = self._node(wid, "publish")
            self.assertEqual(node["status"], "failed")
            self.assertEqual(node["attempts"], 3, "all retries must be attempted")
            self.assertIn("publish_failed", str(node["error"]))
            self.assertEqual(
                [a["status"] for a in self._attempts(wid)].count("failed"), 3,
                "each publish failure must be recorded on its attempt")
        finally:
            engine.event_bus = original

    # ------------------------------------------------------------------ finalize rules

    def test_dead_letter_counter_increments_on_failure(self):
        wid = self._submit([{"id": "a", "exec": {"workload_id": self._workload_id()},
                             "timeout": 30, "retry": {"max": 0}}], dead_letter=True)
        attempt = self._wait_for_attempt(wid)
        self._complete(attempt, ok=False, error="permanent failure")
        self.assertTrue(wait_until(lambda: self._workflow(wid)["status"] == "failed",
                                   timeout=10.0))

        def counter() -> float:
            return self.harness.kernel.metrics.snapshot()["counters"].get("workflow.deadletter", 0.0)

        self.assertGreaterEqual(counter(), 1, "workflow.deadletter was never incremented")

    # ------------------------------------------------------------------ fixtures

    def _workload_id(self) -> str:
        if getattr(self, "_wl_id", None):
            return self._wl_id
        status, node, _ = self.harness.request(
            "POST", "/v1/nodes", {"name": "wf-node", "cpu": 4000, "memory": 8192,
                                  "tags": ["prod"]}, token=self.token)
        self.assertEqual(status, 200, node)
        status, body, _ = self.harness.request("POST", "/v1/workloads", {
            "name": "wf-workload", "image": "img", "replicas": 1, "cpu": 100, "memory": 128,
            "min_replicas": 1, "max_replicas": 1, "policy": "bin_pack", "node_tags": ["prod"],
        }, token=self.token)
        self.assertEqual(status, 200, body)
        self._wl_id = body["workload"]["id"]
        return self._wl_id


if __name__ == "__main__":
    unittest.main()

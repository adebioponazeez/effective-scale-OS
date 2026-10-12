"""Soak: the kernel must survive sustained load with bounded state and live loops.

Crash-consistency is covered by tests/test_chaos.py; this file covers *longevity*, which
nothing asserted before. Four properties, all asserted here:

  * real work flows end-to-end over the worker protocol (claim -> complete), and every
    DAG reaches `succeeded` — not merely "terminal";
  * all background loops stay alive and on-cadence (no watchdog stall, no loop errors);
  * durable state stays bounded — attempts, leases and events do not grow without limit;
  * an abrupt restart mid-load resumes work without duplicating attempts.

The load runs against the real HTTP surface with a real SQLite store, so it exercises the
same path an operator would.
"""
from __future__ import annotations

import resource
import tempfile
import time
import unittest
from pathlib import Path

from .helpers import Harness, wait_until

from effective_scale.adapters import SQLiteStore  # noqa: E402  (helpers put src on the path)
from effective_scale.core.kernel import Config  # noqa: E402

WORKFLOWS = 25          # 25 DAGs x (2 worker nodes + 1 self-completing event node)
DAG_NODES = 3
RSS_BUDGET_MB = 96      # generous, but a per-cycle leak of KBs would blow through it
EVENT_TOPIC = "soak.ready"


def _soak_config(store_path: str) -> Config:
    """Same engine, generous rate limit: this test measures longevity, not admission control."""
    return Config(store_path=store_path, listen="127.0.0.1:0",
                  auth_secret="soak-secret-0123456789abcdef", admin_token="bootstrap-test-token",
                  scheduler_interval=0.02, scale_interval=0.05, workflow_interval=0.02,
                  event_interval=0.02, heartbeat_ttl=1.0, watchdog_stall=30.0,
                  rate_limit_per_minute=1_000_000)


def _rss_mb() -> float:
    # ru_maxrss is KiB on Linux, bytes on macOS
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return raw / 1024 if raw > 10**7 else raw / (1024 * 1024) * 1.048576


class SoakTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        path = str(Path(self._tmp.name) / "soak.db")
        self.harness = Harness(SQLiteStore(path), config=_soak_config(path), start_api=True)
        self.token = self.harness.admin_token()
        self._seed()

    def tearDown(self):
        self.harness.stop()
        self._tmp.cleanup()

    # ------------------------------------------------------------------ seeding

    def _seed(self):
        h = self.harness
        status, _, _ = h.request("POST", "/v1/namespaces", {"name": "demo"},
                                 token=self.token, admin=True)
        self.assertIn(status, (200, 409), "namespace bootstrap failed")
        for name in ("s1", "s2", "s3"):
            status, body, _ = h.request("POST", "/v1/nodes",
                                        {"name": name, "cpu": 4000, "memory": 8192, "tags": ["prod"]},
                                        token=self.token)
            self.assertEqual(status, 200, body)
        status, body, _ = h.request("POST", "/v1/workloads", {
            "name": "soak-api", "image": "ghcr.io/acme/soak:v1", "replicas": 2,
            "min_replicas": 1, "max_replicas": 4, "cpu": 250, "memory": 512,
            "policy": "bin_pack", "node_tags": ["prod"], "priority": 5,
        }, token=self.token)
        self.assertEqual(status, 200, body)
        self.workload_id = body["workload"]["id"]

    def _submit(self, index: int) -> str:
        nodes = [
            {"id": "a", "exec": {"workload_id": self.workload_id}, "timeout": 30},
            {"id": "b", "depends_on": ["a"], "event_topic": EVENT_TOPIC, "timeout": 30},
            {"id": "c", "depends_on": ["b"], "exec": {"workload_id": self.workload_id},
             "retry": {"max": 1}, "timeout": 30},
        ]
        status, body, _ = self.harness.request("POST", "/v1/workflows", {
            "name": f"soak-{index}", "nodes": nodes, "timeout": 120,
        }, token=self.token)
        self.assertEqual(status, 200, body)
        return body["workflow"]["id"]

    # ------------------------------------------------------------------ inspection

    def _workflow(self, wid: str) -> dict | None:
        status, body, _ = self.harness.request("GET", f"/v1/workflows/{wid}", token=self.token)
        return body.get("workflow") if status == 200 else None

    def _is_terminal(self, wid: str) -> bool:
        wf = self._workflow(wid)
        return bool(wf) and wf["status"] in ("succeeded", "failed", "timed_out", "cancelled")

    def _claimable(self) -> list[dict]:
        status, body, _ = self.harness.request("GET", "/v1/attempts?claimable=true&limit=100",
                                               token=self.token)
        return body["attempts"] if status == 200 else []

    def _count(self, path: str, field: str) -> int:
        status, body, _ = self.harness.request("GET", path, token=self.token)
        self.assertEqual(status, 200, body)
        value = body[field]
        return len(value) if isinstance(value, list) else value

    # ------------------------------------------------------------------ driving

    def _drive_until_terminal(self, workflow_ids: list[str], timeout: float) -> dict:
        """Act like a worker fleet until every workflow is terminal.

        Returns stats for the assertions: how many attempt round-trips were completed and
        whether the driver gave up while work was still logically possible.
        """
        deadline = time.time() + timeout
        completed = 0
        last_progress = time.time()
        gave_up = False
        while time.time() < deadline:
            if all(self._is_terminal(wid) for wid in workflow_ids):
                break
            attempts = self._claimable()
            if not attempts:
                # nothing claimable: allow the engine a grace window to dispatch the next
                # node before declaring the run stuck (dispatch happens on the next tick)
                if time.time() - last_progress > 5.0:
                    gave_up = True
                    break
                time.sleep(0.05)
                continue
            for attempt in attempts:
                aid = attempt["id"]
                status, claim, _ = self.harness.request(
                    "POST", f"/v1/attempts/{aid}/claim",
                    {"worker_id": "soak-worker", "ttl_seconds": 20}, token=self.token)
                if status != 200:
                    continue  # lost the race to another claimer
                status, _, _ = self.harness.request(
                    "POST", f"/v1/attempts/{aid}/complete",
                    {"ok": True, "worker_id": "soak-worker", "nonce": claim["nonce"],
                     "result": {"exit_code": 0}}, token=self.token)
                if status == 200:
                    completed += 1
                    last_progress = time.time()
        terminal = sum(1 for wid in workflow_ids if self._is_terminal(wid))
        return {"completed": completed, "terminal": terminal, "gave_up": gave_up,
                "states": _tally(self._workflow(wid) for wid in workflow_ids)}

    # ------------------------------------------------------------------ the tests

    def test_sustained_load_bounded_state_live_loops(self):
        rss_before = _rss_mb()
        workflow_ids = [self._submit(i) for i in range(WORKFLOWS)]
        stats = self._drive_until_terminal(workflow_ids, timeout=90.0)

        self.assertFalse(stats["gave_up"],
                         f"driver gave up with work pending: {stats['states']}")
        self.assertEqual(stats["terminal"], len(workflow_ids),
                         f"unfinished workflows: {stats['states']}")
        self.assertEqual(stats["states"].get("succeeded"), len(workflow_ids),
                         f"workflows did not succeed: {stats['states']}")
        # real worker traffic: every DAG has exactly two worker-run nodes (a and c);
        # node b is a self-completing event node, so N DAGs must yield exactly 2N claims
        self.assertEqual(stats["completed"], 2 * len(workflow_ids),
                         "attempt accounting is off: expected one completion per worker node")

        # loops alive and on-cadence
        status, body, _ = self.harness.request("GET", "/v1/status")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["loop_errors"], [], "a background loop raised during the soak")
        metrics = body.get("metrics", {})
        for loop in ("watchdog", "scheduler", "workflow", "events", "writer"):
            key = f"loop.{loop}.last_ts"
            if key in metrics:
                self.assertAlmostEqual(metrics[key], time.time(), delta=30.0,
                                       msg=f"{loop} loop is not making progress")

        # the event bus delivered what the DAGs published (at-least-once)
        status, events, _ = self.harness.request(
            "GET", f"/v1/events/{EVENT_TOPIC}?group=soak-reader&limit=100", token=self.token)
        self.assertEqual(status, 200, events)
        self.assertGreaterEqual(len(events["events"]), 1,
                                "workflow event nodes published nothing")
        for event in events["events"]:
            ack, _, _ = self.harness.request(
                "POST", f"/v1/events/{EVENT_TOPIC}/ack",
                {"group": "soak-reader", "event_id": event["id"], "ok": True}, token=self.token)
            self.assertEqual(ack, 200, "ack must succeed")

        # durable state stays bounded: attempts <= workflows x nodes x (1 + retries);
        # leases <= nodes x max_replicas
        attempts = self._count("/v1/attempts?limit=1000", "attempts")
        self.assertLessEqual(attempts, WORKFLOWS * DAG_NODES * 2)
        leases = self._count("/v1/leases", "leases")
        self.assertLessEqual(leases, 3 * 4, "leases leaked beyond nodes x max_replicas")

        growth = _rss_mb() - rss_before
        self.assertLess(growth, RSS_BUDGET_MB,
                        f"RSS grew {growth:.1f} MB over {WORKFLOWS} workflows")

    def test_restart_under_load_does_not_duplicate_work(self):
        workflow_ids = [self._submit(i) for i in range(8)]
        first = self._drive_until_terminal(workflow_ids, timeout=15.0)
        self.assertGreater(first["completed"], 0, "the first pass completed nothing")

        # abrupt restart on the same store (no graceful drain of the API)
        store_path = self.harness.kernel.config.store_path
        self.harness.stop()
        self.harness = Harness(SQLiteStore(store_path), config=_soak_config(store_path),
                               start_api=True)
        self.token = self.harness.admin_token()

        second = self._drive_until_terminal(workflow_ids, timeout=45.0)
        self.assertFalse(second["gave_up"],
                         f"after restart: driver gave up with work pending: {second['states']}")
        self.assertEqual(second["terminal"], len(workflow_ids),
                         f"workflows did not converge after restart: {second['states']}")
        self.assertEqual(second["states"].get("succeeded"), len(workflow_ids),
                         f"workflows did not succeed after restart: {second['states']}")

        # no duplicate attempts per (workflow, node): recovery must resume, not restart work
        status, body, _ = self.harness.request("GET", "/v1/attempts?limit=1000", token=self.token)
        self.assertEqual(status, 200, body)
        pairs = [(a["workflow_id"], a["node_id"]) for a in body["attempts"]]
        self.assertEqual(len(pairs), len(set(pairs)),
                         "duplicate attempts for the same workflow node after restart")


def _tally(workflows) -> dict:
    counts: dict[str, int] = {}
    for wf in workflows:
        key = wf["status"] if wf else "missing"
        counts[key] = counts.get(key, 0) + 1
    return counts


if __name__ == "__main__":
    unittest.main()

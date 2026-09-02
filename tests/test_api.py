import unittest

from effective_scale.core.kernel import Config
from tests.helpers import Harness, wait_until


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.h = Harness(start_api=True)
        self.token = self.h.admin_token()

    def tearDown(self):
        self.h.stop()

    # -- auth ---------------------------------------------------------------
    def test_401_without_token(self):
        status, data, _ = self.h.request("GET", "/v1/workloads")
        self.assertEqual(status, 401)
        self.assertEqual(data["error"]["code"], "unauthorized")

    def test_403_when_scopes_missing(self):
        self.h.request("POST", "/v1/namespaces", {"name": "demo"}, admin=True)
        read_only = self.h.request(
            "POST", "/v1/tokens", {"namespace": "demo", "scopes": ["read"]}, admin=True
        )[1]["token"]
        status, data, _ = self.h.request("POST", "/v1/workloads", {
            "name": "w", "image": "img", "replicas": 1}, token=read_only)
        self.assertEqual(status, 403, data)
        status, _, _ = self.h.request("GET", "/v1/workloads", token=read_only)
        self.assertEqual(status, 200)

    def test_namespace_isolation(self):
        self.h.request("POST", "/v1/namespaces", {"name": "a"}, admin=True)
        self.h.request("POST", "/v1/namespaces", {"name": "b"}, admin=True)
        ta = self.h.request("POST", "/v1/tokens", {"namespace": "a", "scopes": ["write"]},
                            admin=True)[1]["token"]
        tb = self.h.request("POST", "/v1/tokens", {"namespace": "b", "scopes": ["read"]},
                            admin=True)[1]["token"]
        _, created, _ = self.h.request("POST", "/v1/workloads",
                                       {"name": "secret-w", "image": "i", "replicas": 1}, token=ta)
        wid = created["workload"]["id"]
        status, data, _ = self.h.request("GET", f"/v1/workloads/{wid}", token=tb)
        self.assertEqual(status, 403, data)

    # -- idempotency ---------------------------------------------------------
    def test_idempotency_replay_and_conflict(self):
        self.h.request("POST", "/v1/namespaces", {"name": "demo"}, admin=True)
        body = {"name": "job", "nodes": [{"id": "a", "event_topic": "t"}]}
        s1, d1, _ = self.h.request("POST", "/v1/workflows", body, token=self.token,
                                   headers={"Idempotency-Key": "key-1"})
        self.assertEqual(s1, 200)
        s2, d2, _ = self.h.request("POST", "/v1/workflows", body, token=self.token,
                                   headers={"Idempotency-Key": "key-1"})
        self.assertEqual(s2, 200)
        self.assertTrue(d2.get("replayed"))
        self.assertEqual(d1["workflow"]["id"], d2["workflow"]["id"])
        conflict = dict(body)
        conflict["name"] = "different"
        s3, d3, _ = self.h.request("POST", "/v1/workflows", conflict, token=self.token,
                                   headers={"Idempotency-Key": "key-1"})
        self.assertEqual(s3, 409, d3)

    # -- validation ------------------------------------------------------------
    def test_400_on_bad_input(self):
        self.h.request("POST", "/v1/namespaces", {"name": "demo"}, admin=True)
        status, data, _ = self.h.request("POST", "/v1/workloads",
                                         {"name": "w", "image": "i", "replicas": -1}, token=self.token)
        self.assertEqual(status, 400)
        self.assertEqual(data["error"]["code"], "validation_error")

    # -- end-to-end: workload -> scheduler -> lease ---------------------------
    def test_scheduler_grants_leases_end_to_end(self):
        self.h.request("POST", "/v1/namespaces", {"name": "demo"}, admin=True)
        for i in range(3):
            self.h.request("POST", "/v1/nodes",
                           {"name": f"node-{i}", "cpu": 2000, "memory": 4096}, admin=True)
        _, d, _ = self.h.request("POST", "/v1/workloads",
                                 {"name": "web", "image": "img", "replicas": 2,
                                  "cpu": 250, "memory": 512}, token=self.token)
        wid = d["workload"]["id"]
        ok = wait_until(lambda: len([l for l in self.h.kernel.store.snapshot().leases.values()
                                     if l.workload_id == wid]) >= 2, timeout=5.0)
        self.assertTrue(ok, "scheduler should grant replicas")
        # scale down -> leases released
        self.h.request("POST", f"/v1/workloads/{wid}/scale", {"replicas": 1}, token=self.token)
        ok = wait_until(lambda: len([l for l in self.h.kernel.store.snapshot().leases.values()
                                     if l.workload_id == wid]) == 1, timeout=5.0)
        self.assertTrue(ok)

    # -- workflow via API -------------------------------------------------------
    def test_workflow_lifecycle_via_api(self):
        self.h.request("POST", "/v1/namespaces", {"name": "demo"}, admin=True)
        _, d, _ = self.h.request("POST", "/v1/workflows", {
            "name": "wf",
            "nodes": [{"id": "a", "event_topic": "boot", "timeout": 30},
                      {"id": "b", "depends_on": ["a"], "event_topic": "done", "timeout": 30}],
        }, token=self.token)
        wid = d["workflow"]["id"]
        ok = wait_until(lambda: self.h.kernel.store.snapshot().workflow(wid).status.value == "succeeded",
                        timeout=5.0)
        self.assertTrue(ok, "event-node workflow should complete")

    # -- events via API ---------------------------------------------------------
    def test_event_publish_fetch_ack(self):
        self.h.request("POST", "/v1/namespaces", {"name": "demo"}, admin=True)
        s, d, _ = self.h.request("POST", "/v1/events",
                                 {"topic": "orders", "key": "k1", "payload": {"id": 7}},
                                 token=self.token)
        self.assertEqual(s, 200)
        eid = d["event"]["id"]
        _, fetched, _ = self.h.request("GET", "/v1/events/orders?group=g", token=self.token)
        self.assertEqual(len(fetched["events"]), 1)
        self.h.request("POST", "/v1/events/orders/ack", {"group": "g", "event_id": eid, "ok": True},
                       token=self.token)
        _, after, _ = self.h.request("GET", "/v1/events/orders?group=g", token=self.token)
        self.assertEqual(after["events"], [])

    # -- rate limiting ------------------------------------------------------------
    def test_rate_limit_returns_429(self):
        self.h.kernel.config.rate_limit_per_minute = 3
        self.h.api.limiter._limit = 3
        self.h.api.limiter._windows.clear()  # setUp already consumed one slot
        statuses = []
        for _ in range(4):
            s, _, hdrs = self.h.request("GET", "/v1/health/live")
            statuses.append((s, hdrs.get("retry-after")))
        self.assertEqual(statuses[:3], [(200, None), (200, None), (200, None)])
        self.assertEqual(statuses[3][0], 429)
        self.assertTrue(statuses[3][1])

    def test_metrics_and_status(self):
        s, d, _ = self.h.request("GET", "/v1/metrics")
        self.assertEqual(s, 200)
        self.assertIn("counters", d["metrics"])
        s, d, _ = self.h.request("GET", "/v1/status")
        self.assertEqual(s, 200)
        self.assertTrue(d["leader"])

    def test_cancel_via_api_and_attempt_complete_returns_ok(self):
        self.h.request("POST", "/v1/namespaces", {"name": "demo"}, admin=True)
        self.h.request("POST", "/v1/nodes", {"name": "n1", "cpu": 2000, "memory": 4096}, admin=True)
        _, w, _ = self.h.request("POST", "/v1/workloads",
                                 {"name": "worker", "image": "i", "replicas": 1}, token=self.token)
        _, wf, _ = self.h.request("POST", "/v1/workflows", {
            "name": "wf", "nodes": [
                {"id": "a", "exec": {"workload_id": w["workload"]["id"]}, "timeout": 30}],
        }, token=self.token)
        wid = wf["workflow"]["id"]
        ok = wait_until(lambda: any(a.workflow_id == wid for a in
                                    self.h.kernel.store.snapshot().attempts.values()), timeout=5.0)
        self.assertTrue(ok)
        attempt = [a for a in self.h.kernel.store.snapshot().attempts.values()
                   if a.workflow_id == wid][0]
        s, _, _ = self.h.request("POST", f"/v1/attempts/{attempt.id}/complete",
                                 {"ok": True}, token=self.token)
        self.assertEqual(s, 200)
        ok = wait_until(lambda: self.h.kernel.store.snapshot().workflow(wid).status.value == "succeeded",
                        timeout=5.0)
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()

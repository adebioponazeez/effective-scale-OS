"""Chaos tests: crash recovery, duplicate-tick safety, WAL durability."""
import tempfile
import unittest
from pathlib import Path

from effective_scale.adapters import MemoryStore, SQLiteStore
from effective_scale.core.kernel import Config, Kernel
from tests.helpers import Harness, wait_until


class CrashRecoveryTest(unittest.TestCase):
    def tearDown(self):
        try:
            self._kernel.stop()
        except Exception:  # noqa: BLE001
            pass

    def _sqlite_kernel(self, path: str) -> Kernel:
        self._kernel = Kernel(SQLiteStore(path), config=Config(
            store_path=path, auth_secret="x" * 20, scheduler_interval=0.02,
            workflow_interval=0.02, event_interval=0.02))
        self._kernel.start()
        return self._kernel

    def test_wal_survives_abrupt_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "k.db")
            store = SQLiteStore(path)
            store.open()
            from effective_scale.domain.models import Workload

            wl = Workload.create("ns", {"name": "w", "image": "i", "replicas": 1}, now=1.0)
            store.put_workload(wl)
            store._conn.close()  # simulate power loss (no clean close)
            store2 = SQLiteStore(path)
            store2.open()
            self.assertIsNotNone(store2.snapshot().workload(wl.id))
            store2.close()

    def test_restart_requeues_workflow_without_double_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "k.db")
            k1 = self._sqlite_kernel(path)
            from effective_scale.domain.models import Workload, Node

            k1.engine.submit("ns", {
                "name": "wf",
                "nodes": [{"id": "a", "event_topic": "t", "timeout": 30}],
            })
            # wait until the event node dispatched + published
            ok = wait_until(lambda: any(ev.topic == "t" for ev in k1.store.snapshot().events.values()),
                            timeout=5.0)
            self.assertTrue(ok)
            wf1 = [w for w in k1.store.snapshot().workflows.values()][0]
            self.assertEqual(wf1.status.value, "succeeded")
            k1.stop()

            # restart on the same durable store: no duplication, state intact
            k2 = self._sqlite_kernel(path)
            wf2 = [w for w in k2.store.snapshot().workflows.values()][0]
            self.assertEqual(wf2.id, wf1.id)
            self.assertEqual(wf2.status.value, "succeeded")
            k2.stop()

    def test_double_tick_creates_single_attempt(self):
        store = MemoryStore()
        store.open()
        from effective_scale.core.workflow import WorkflowEngine
        from effective_scale.core.events import EventBus
        from effective_scale.observability.registry import Registry
        from effective_scale.ports.logger import MemLogger
        from effective_scale.ports.random import SecureRandom
        from tests.helpers import FakeClock

        clock = FakeClock()
        engine = WorkflowEngine(store, clock=clock, rng=SecureRandom(), logger=MemLogger(),
                                metrics=Registry(),
                                event_bus=EventBus(store, clock=clock, logger=MemLogger(),
                                                   metrics=Registry()))
        wf = engine.submit("ns", {"name": "wf", "nodes": [{"id": "a", "event_topic": "t"}]})
        engine.tick(clock.now())
        engine.tick(clock.now())  # duplicate tick MUST be a no-op
        attempts = [a for a in store.snapshot().attempts.values() if a.workflow_id == wf.id]
        self.assertEqual(len(attempts), 1)


class KernelCrashTest(unittest.TestCase):
    def test_store_dies_mid_write_readiness_fails_gracefully(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            h = Harness(SQLiteStore(str(Path(tmp) / "k.db")), start_api=True)
            sqlite = h.kernel.store
            # simulate store death: close DB under the kernel
            sqlite._conn.close()
            # API keeps serving; status reflects a degraded kernel, never a stack trace
            status, _, _ = h.request("GET", "/v1/status")
            self.assertIn(status, (200, 503))
            h.stop()

    def test_idle_event_bus_does_not_trip_watchdog(self):
        """Regression: an idle-but-alive event bus must never kill the kernel.

        The event loop previously marked progress only when it pumped at least
        one event; after the first publish, an idle bus stopped marking and the
        watchdog (stall = watchdog_stall) committed suicide at exactly 30 s —
        i.e. every long-running kernel died after its first burst of events.
        """
        import time
        from unittest.mock import patch

        from effective_scale.adapters import MemoryStore
        from effective_scale.core.kernel import Config, Kernel
        from effective_scale.ports.logger import MemLogger

        with patch("os._exit", side_effect=AssertionError("watchdog tried to exit")):
            h = Harness(MemoryStore(), config=Config(
                store_path=":memory:", auth_secret="x" * 20,
                scheduler_interval=0.02, workflow_interval=0.02,
                event_interval=0.02, scale_interval=0.02,
                heartbeat_ttl=1.0, watchdog_stall=0.5,
            ), start_api=False)
            try:
                # publish once so the "events" loop enters the progress map
                h.kernel.bus.publish("t", key="k", payload={"v": 1}, schema_version=1)
                ok = wait_until(lambda: "events" in h.kernel._last_progress, timeout=5.0)
                self.assertTrue(ok, "event loop never marked progress")
                # now go idle, far past the watchdog threshold
                time.sleep(1.5)
                self.assertFalse(h.kernel._stop.is_set(), "kernel was stopped by watchdog")
                stalls = [r for r in h.kernel.logger.records
                          if r.get("event") == "kernel.watchdog.stall"]
                self.assertEqual(stalls, [])
                self.assertTrue(h.kernel.leader.is_leader())
            finally:
                h.stop()


if __name__ == "__main__":
    unittest.main()

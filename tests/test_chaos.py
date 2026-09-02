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


if __name__ == "__main__":
    unittest.main()

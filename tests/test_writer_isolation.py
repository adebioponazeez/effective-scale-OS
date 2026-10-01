"""The resilience kit now has a production consumer: the client-facing write path.

Before this, `core/resilience.py` was tested in isolation and imported by nothing. The real
risk it guards is concrete: the writer queue is unbounded and every API mutation parks a
thread on a future, so a wedged writer or a dead store turns into unbounded memory growth and
a timeout storm. These tests pin the contract:

  * a saturated backlog sheds load with a fast 503 instead of buffering;
  * a writer that keeps failing trips the breaker, so later writes fail fast;
  * the breaker closes again after a successful probe;
  * loops and readiness probes are NOT blocked by either guard — they are the recovery path
    (a breaker must never prevent the system from noticing that it recovered).
"""
from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from effective_scale.adapters import MemoryStore, SQLiteStore
from effective_scale.core.kernel import Config, Kernel
from effective_scale.domain.errors import DomainError, Overloaded

from .helpers import Harness


def _config(store_path: str = ":memory:", **overrides) -> Config:
    values = dict(store_path=store_path, listen="127.0.0.1:0",
                  auth_secret="isolation-secret-0123456789", admin_token="bootstrap-test-token",
                  scheduler_interval=0.05, scale_interval=0.05, workflow_interval=0.05,
                  event_interval=0.05, heartbeat_ttl=1.0, watchdog_stall=30.0,
                  rate_limit_per_minute=1_000_000)
    values.update(overrides)
    return Config(**values)


def _boom(_unused=None):
    raise RuntimeError("dependency exploded")


class BulkheadTest(unittest.TestCase):
    def test_saturated_backlog_sheds_load(self):
        kernel = Kernel(MemoryStore(), config=_config(max_writer_backlog=1))
        kernel.start()
        blocker = threading.Event()
        try:
            # occupy the single slot with a write that stays in flight
            thread = threading.Thread(target=kernel.write, args=(lambda: blocker.wait(5.0),))
            thread.start()
            deadline = time.monotonic() + 2.0
            while kernel.write_bulkhead.in_use == 0 and time.monotonic() < deadline:
                time.sleep(0.01)

            with self.assertRaises(Overloaded) as ctx:
                kernel.write(lambda: None)
            self.assertEqual(ctx.exception.code, "overloaded")
            self.assertEqual(ctx.exception.http_status, 503)

            # the slot is released when the in-flight write finishes
            blocker.set()
            thread.join(timeout=5.0)
            self.assertTrue(kernel.write(lambda: "ok") == "ok")
            self.assertEqual(kernel.write_bulkhead.in_use, 0)
        finally:
            blocker.set()
            kernel.stop()

    def test_business_rejections_are_not_bulkhead_failures(self):
        """A conflict is a normal answer to a client, not evidence the dependency is sick."""
        kernel = Kernel(MemoryStore(), config=_config())
        kernel.start()
        try:
            for _ in range(10):
                with self.assertRaises(DomainError):
                    kernel.write(_raise_domain)
            self.assertEqual(kernel.writer_breaker.state(), "closed")
        finally:
            kernel.stop()


class BreakerTest(unittest.TestCase):
    def test_repeated_infrastructure_failure_trips_and_recovers(self):
        kernel = Kernel(MemoryStore(), config=_config(breaker_failure_threshold=3,
                                                      breaker_open_seconds=0.3))
        kernel.start()
        try:
            for _ in range(3):
                with self.assertRaises(RuntimeError):
                    kernel.write(_boom)
            self.assertEqual(kernel.writer_breaker.state(), "open")

            # fast fail: the store is never touched, so this is cheap
            started = time.monotonic()
            with self.assertRaises(Overloaded):
                kernel.write(lambda: "should not run")
            self.assertLess(time.monotonic() - started, 0.2, "an open breaker must fail fast")

            # half-open probe after the cool-down, then closed again
            time.sleep(0.35)
            self.assertEqual(kernel.write(lambda: "recovered"), "recovered")
            self.assertEqual(kernel.writer_breaker.state(), "closed")
        finally:
            kernel.stop()

    def test_loops_and_readiness_bypass_the_breaker(self):
        """The guards protect clients; they must not blind the system to its own recovery."""
        kernel = Kernel(MemoryStore(), config=_config(breaker_failure_threshold=1,
                                                      breaker_open_seconds=60.0))
        kernel.start()
        try:
            with self.assertRaises(RuntimeError):
                kernel.write(_boom)
            self.assertEqual(kernel.writer_breaker.state(), "open")
            with self.assertRaises(Overloaded):
                kernel.write(lambda: None)

            # readiness still probes for real (this is how the system learns it recovered)
            self.assertTrue(kernel.ready(timeout=2.0))
            # and loop-style unguarded writes still go through
            self.assertEqual(kernel.write(lambda: "loop", guarded=False), "loop")
        finally:
            kernel.stop()


class WriterIsolationApiTest(unittest.TestCase):
    def test_status_reports_writer_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "iso.db")
            harness = Harness(SQLiteStore(path), config=_config(path), start_api=True)
            try:
                status, body, _ = harness.request("GET", "/v1/status")
                self.assertEqual(status, 200, body)
                writer = body["writer"]
                self.assertEqual(writer["breaker"], "closed")
                self.assertEqual(writer["pending"], 0)
                self.assertEqual(writer["backlog_limit"], 1024)
                self.assertIn("queued", writer)
            finally:
                harness.stop()

    def test_api_mutation_is_shed_when_the_backlog_is_full(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "iso.db")
            harness = Harness(SQLiteStore(path), config=_config(path, max_writer_backlog=1),
                              start_api=True)
            blocker = threading.Event()
            try:
                token = harness.admin_token()
                thread = threading.Thread(
                    target=harness.kernel.write, args=(lambda: blocker.wait(5.0),))
                thread.start()
                deadline = time.monotonic() + 2.0
                while harness.kernel.write_bulkhead.in_use == 0 and time.monotonic() < deadline:
                    time.sleep(0.01)

                status, body, _ = harness.request(
                    "POST", "/v1/workloads",
                    {"name": "shed", "image": "img", "replicas": 1, "cpu": 10, "memory": 10},
                    token=token)
                self.assertEqual(status, 503, body)
                self.assertEqual(body["error"]["code"], "overloaded")
            finally:
                blocker.set()
                harness.stop()


def _raise_domain():
    raise DomainError("business rule says no")


if __name__ == "__main__":
    unittest.main()

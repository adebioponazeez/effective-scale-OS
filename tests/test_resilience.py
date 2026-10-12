import threading
import time
import unittest

from effective_scale.adapters import MemoryStore
from effective_scale.core.cron import CronSchedule
from effective_scale.core.leader import LeaderElection, SingleLeader
from effective_scale.core.resilience import Bulkhead, CircuitBreaker
from effective_scale.ports.logger import MemLogger
from tests.helpers import FakeClock, wait_until


class _MonoClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now


class CircuitBreakerTest(unittest.TestCase):
    def test_trips_and_recovers_with_probe(self):
        clock = _MonoClock()
        cb = CircuitBreaker("svc", failure_threshold=3, window_seconds=10,
                            open_seconds=5, clock=clock)
        for _ in range(3):
            self.assertTrue(cb.allow())
            cb.record_failure()
        self.assertFalse(cb.allow())  # open
        self.assertEqual(cb.state(), "open")
        clock.now = 6.0
        self.assertTrue(cb.allow())  # half-open probe
        cb.record_success()
        self.assertEqual(cb.state(), "closed")
        self.assertTrue(cb.allow())

    def test_half_open_admits_exactly_one_probe(self):
        """Half-open is a probe, not a reopening: everyone else keeps failing fast."""
        clock = _MonoClock()
        cb = CircuitBreaker("svc", failure_threshold=1, window_seconds=10,
                            open_seconds=5, clock=clock)
        cb.allow()
        cb.record_failure()
        self.assertEqual(cb.state(), "open")
        clock.now = 6.0
        self.assertTrue(cb.allow(), "the first caller after the cool-down is the probe")
        self.assertFalse(cb.allow(), "a second caller must not join the probe")
        self.assertEqual(cb.state(), "half_open")

    def test_failed_probe_reopens_the_breaker(self):
        clock = _MonoClock()
        cb = CircuitBreaker("svc", failure_threshold=1, window_seconds=10,
                            open_seconds=5, clock=clock)
        cb.record_failure()
        clock.now = 6.0
        self.assertTrue(cb.allow())            # half_open, probe in flight
        cb.record_failure()                    # probe failed
        self.assertEqual(cb.state(), "open")
        self.assertFalse(cb.allow(), "a failed probe must not relax the breaker")
        clock.now = 12.0
        self.assertTrue(cb.allow(), "the next probe is gated by the new cool-down")
        cb.record_success()
        self.assertEqual(cb.state(), "closed")
        self.assertTrue(cb.allow())

    def test_failure_window_expires(self):
        clock = _MonoClock()
        cb = CircuitBreaker("svc", failure_threshold=3, window_seconds=10, open_seconds=5,
                            clock=clock)
        cb.record_failure()          # t=0 (expires)
        clock.now = 12.0
        cb.record_failure()          # prunes the t=0 failure -> 1 of 3
        self.assertTrue(cb.allow())
        clock.now = 13.0
        cb.record_failure()          # 2 of 3 -> still closed
        self.assertTrue(cb.allow())
        cb.record_failure()          # 3 of 3 -> OPEN
        self.assertFalse(cb.allow())
        clock.now = 20.0
        self.assertTrue(cb.allow())  # half-open probe after open_seconds


class BulkheadTest(unittest.TestCase):
    def test_bounds_and_rejects(self):
        bh = Bulkhead("pool", 2)
        self.assertTrue(bh.acquire(0))
        self.assertTrue(bh.acquire(0))
        self.assertEqual(bh.in_use, 2)
        self.assertFalse(bh.try_acquire())
        bh.release()
        self.assertTrue(bh.try_acquire())
        self.assertEqual(bh.in_use, 2)
        bh.release()
        bh.release()
        self.assertEqual(bh.in_use, 0, "release is idempotent-safe and never negative")

    def test_acquire_waits_only_until_its_deadline(self):
        """A saturated bulkhead must shed in bounded time, never block a caller forever."""
        bh = Bulkhead("pool", 1)
        self.assertTrue(bh.acquire(0))
        started = time.monotonic()
        self.assertFalse(bh.acquire(0.05), "a full pool must refuse once the deadline passes")
        waited = time.monotonic() - started
        self.assertGreaterEqual(waited, 0.04)
        self.assertLess(waited, 1.0)
        bh.release()
        self.assertTrue(bh.acquire(0.05), "a freed slot must be usable immediately")
        bh.release()


class LeaderElectionTest(unittest.TestCase):
    def test_lease_handoff_after_expiry(self):
        store = MemoryStore()
        store.open()
        clock = FakeClock()
        a = LeaderElection(store, "a", ttl=5.0, heartbeat=1.0, clock=clock)
        b = LeaderElection(store, "b", ttl=5.0, heartbeat=1.0, clock=clock)
        self.assertTrue(a.acquire())
        self.assertFalse(b.acquire())
        self.assertTrue(a.renew())
        clock.advance(6.0)
        self.assertTrue(b.acquire(), "B should take over after lease expiry")
        self.assertFalse(a.renew(), "A must be demoted (nonce mismatch)")
        self.assertFalse(a.is_leader())

    def test_renew_blocked_after_skew(self):
        store = MemoryStore()
        store.open()
        clock = FakeClock()
        a = LeaderElection(store, "a", ttl=5.0, heartbeat=1.0, clock=clock)
        a.acquire()
        clock.advance(11.0)  # far beyond ttl without renew
        lost = []
        a.on_lost = lambda r: lost.append(r)
        self.assertFalse(a.renew())
        self.assertEqual(len(lost), 1)


class LeaderLoopTest(unittest.TestCase):
    """The heartbeat loop is what keeps a real deployment elected — or demotes it cleanly."""

    def _store(self) -> MemoryStore:
        store = MemoryStore()
        store.open()
        return store

    def test_run_loop_heartbeats_ticks_and_demotes_on_stop(self):
        store = self._store()
        log = MemLogger()
        ticks: list[int] = []
        lost: list[str] = []
        a = LeaderElection(store, "a", ttl=5.0, heartbeat=0.01, logger=log,
                           on_progress=lambda: ticks.append(1), on_lost=lost.append)
        thread = threading.Thread(target=a.run, daemon=True)
        thread.start()
        try:
            self.assertTrue(wait_until(a.is_leader, timeout=5.0), "loop never acquired the lease")
            self.assertTrue(wait_until(lambda: len(ticks) >= 3, timeout=5.0),
                            "a live loop must report progress every iteration")
            self.assertTrue(a.nonce())
            self.assertGreaterEqual(a.term, 2)
        finally:
            a.stop()
            thread.join(timeout=5.0)
        self.assertFalse(thread.is_alive(), "stop() must end the loop")
        self.assertFalse(a.is_leader(), "a stopped leader must step down, not linger")
        self.assertEqual(lost, ["shutdown"])
        events = [r["event"] for r in log.records]
        self.assertIn("leader.acquired", events)
        self.assertIn("leader.demoted", events)
        demoted = next(r for r in log.records if r["event"] == "leader.demoted")
        self.assertEqual(demoted["level"], "warn", "a demotion is a warning, not info")

    def test_standby_reports_progress_while_waiting_then_takes_over(self):
        """A standby is not stalled work: it must tick, and it must promote after expiry."""
        store = self._store()
        holder = LeaderElection(store, "a", ttl=0.3, heartbeat=0.01, clock=None)
        self.assertTrue(holder.acquire())
        ticks: list[int] = []
        standby = LeaderElection(store, "b", ttl=0.3, heartbeat=0.01,
                                 on_progress=lambda: ticks.append(1))
        thread = threading.Thread(target=standby.run, daemon=True)
        thread.start()
        try:
            self.assertTrue(wait_until(lambda: len(ticks) >= 3, timeout=5.0),
                            "a standby must keep reporting progress (watchdog liveness)")
            self.assertFalse(standby.is_leader())
            # the holder never renews: once the ttl lapses the standby must take over
            self.assertTrue(wait_until(standby.is_leader, timeout=5.0),
                            "standby never took over an expired lease")
        finally:
            standby.stop()
            thread.join(timeout=5.0)
        self.assertFalse(thread.is_alive())

    def test_renew_requires_leadership_and_reacquire_fences(self):
        store = self._store()
        clock = FakeClock()
        a = LeaderElection(store, "a", ttl=5.0, heartbeat=1.0, clock=clock)
        self.assertEqual(a.nonce(), "")
        self.assertFalse(a.renew(), "a node that never acquired cannot renew")
        self.assertTrue(a.acquire())
        first_nonce, first_term = a.nonce(), a.term
        self.assertTrue(a.renew())
        # re-acquiring (e.g. after a restart loop) must fence the previous term
        self.assertTrue(a.acquire())
        self.assertNotEqual(a.nonce(), first_nonce)
        self.assertGreater(a.term, first_term)

    def test_demote_outside_leadership_is_a_noop(self):
        store = self._store()
        log = MemLogger()
        lost: list[str] = []
        a = LeaderElection(store, "a", ttl=5.0, heartbeat=1.0, logger=log, on_lost=lost.append)
        a._demote("not a leader")
        self.assertEqual(log.records, [], "nothing to demote, nothing to log")
        self.assertEqual(lost, [])


class SingleLeaderTest(unittest.TestCase):
    """The in-process leader is always elected: loops must not double-elect in tests."""

    def test_contract(self):
        leader = SingleLeader("single-a")
        self.assertEqual(leader.holder_id, "single-a")
        self.assertTrue(leader.is_leader())
        self.assertTrue(leader.acquire())
        self.assertTrue(leader.renew())
        self.assertTrue(leader.nonce())
        self.assertEqual(leader.term, 1)
        leader.stop()
        leader.run()  # already stopped: run() returns instead of blocking forever


class CronTest(unittest.TestCase):
    def test_parse_and_match(self):
        import datetime as dt

        sched = CronSchedule("*/15 * * * *")
        self.assertTrue(sched.matches(dt.datetime(2026, 9, 2, 10, 0)))
        self.assertFalse(sched.matches(dt.datetime(2026, 9, 2, 10, 7)))
        nxt = sched.next_after(dt.datetime(2026, 9, 2, 10, 7))
        self.assertEqual(nxt, dt.datetime(2026, 9, 2, 10, 15))

    def test_invalid_expression_rejected(self):
        with self.assertRaises(ValueError):
            CronSchedule("not a cron")
        with self.assertRaises(ValueError):
            CronSchedule("61 * * * *")

    def test_dow_convention(self):
        import datetime as dt

        sched = CronSchedule("0 0 * * 0")  # Sunday
        self.assertTrue(sched.matches(dt.datetime(2026, 9, 6, 0, 0)))  # a Sunday
        self.assertFalse(sched.matches(dt.datetime(2026, 9, 7, 0, 0)))


if __name__ == "__main__":
    unittest.main()

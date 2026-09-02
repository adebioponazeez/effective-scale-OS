import unittest

from effective_scale.adapters import MemoryStore
from effective_scale.core.cron import CronSchedule
from effective_scale.core.leader import LeaderElection
from effective_scale.core.resilience import Bulkhead, CircuitBreaker
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
        self.assertFalse(bh.try_acquire())
        bh.release()
        self.assertTrue(bh.try_acquire())
        bh.release()
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

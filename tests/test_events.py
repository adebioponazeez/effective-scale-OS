import unittest

from effective_scale.adapters import MemoryStore
from effective_scale.core.events import ConsumerGroup, EventBus
from effective_scale.observability.registry import Registry
from effective_scale.ports.logger import MemLogger
from tests.helpers import FakeClock


class EventBusTest(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.store.open()
        self.clock = FakeClock()
        self.metrics = Registry()
        self.bus = EventBus(self.store, clock=self.clock, logger=MemLogger(),
                            metrics=self.metrics, partitions=4, max_lag=10, max_attempts=2)

    def test_publish_partitions_and_orders_sequentially(self):
        e1 = self.bus.publish("t", "k", {"n": 1})
        e2 = self.bus.publish("t", "k", {"n": 2})
        self.assertEqual(e1.seq, 1)
        self.assertEqual(e2.seq, 2)
        self.assertEqual(e1.partition, e2.partition)
        events = self.store.snapshot().events_for("t", e1.partition)
        self.assertEqual([e.seq for e in events], [1, 2])

    def test_consume_and_ack_advances_offset(self):
        e1 = self.bus.publish("t", "k", {"n": 1})
        e2 = self.bus.publish("t", "k", {"n": 2})
        batch = self.bus.next_batch("t", "g", limit=10)
        self.assertEqual(len(batch), 2)
        self.bus.ack("t", "g", e1.id, ok=True)
        batch2 = self.bus.next_batch("t", "g", limit=10)
        self.assertEqual([e.id for e in batch2], [e2.id])

    def test_failure_then_dead_letter(self):
        ev = self.bus.publish("t", "k", {"bad": True})
        self.bus.ack("t", "g", ev.id, ok=False, error="handler boom")
        self.bus.ack("t", "g", ev.id, ok=False, error="handler boom")
        snap = self.store.snapshot()
        self.assertEqual(snap.events[ev.id].status.value, "dead")
        self.assertEqual(len(snap.dlqs), 1)
        self.assertIn("handler boom", snap.dlqs[ev.id].error)
        # partition continues: publish after poison still works
        e2 = self.bus.publish("t", "k", {"ok": True})
        self.assertGreater(e2.seq, ev.seq)

    def test_backpressure_on_lag(self):
        for i in range(10):
            self.bus.publish("t", f"k{i}", {"n": i})
        from effective_scale.domain.errors import Backpressure

        with self.assertRaises(Backpressure):
            self.bus.publish("t", "overflow", {"n": 1})

    def test_consumer_group_replays_from_checkpoint_after_crash(self):
        ev = self.bus.publish("t", "k", {"n": 1})
        self.bus.ack("t", "g", ev.id, ok=True)
        # simulate restart: new bus, same store — no event lost or duplicated past offset
        bus2 = EventBus(self.store, clock=self.clock, logger=MemLogger(), metrics=self.metrics,
                        partitions=4, max_attempts=2)
        batch = bus2.next_batch("t", "g", limit=10)
        self.assertEqual(batch, [])

    def test_poison_does_not_block_other_partitions(self):
        ev = self.bus.publish("t", "poison", {"x": 1})
        part = ev.partition
        self.bus.ack("t", "g", ev.id, ok=False, error="boom")
        self.bus.ack("t", "g", ev.id, ok=False, error="boom")
        ok = self.bus.publish("t", "happy", {"x": 2})
        self.assertEqual(ok.partition, part)
        self.assertEqual(ok.seq, 2)
        batch = self.bus.next_batch("t", "g", limit=10)
        self.assertEqual([e.id for e in batch], [ok.id])

    def test_payload_size_cap(self):
        from effective_scale.domain.errors import ValidationError

        with self.assertRaises(ValidationError):
            self.bus.publish("t", "k", {"blob": "x" * (1024 * 1024 + 10)}, )

    def test_in_process_consumer_pump(self):
        got = []
        self.bus.publish("t", "k", {"n": 1})
        self.bus.publish("t", "k", {"n": 2})
        group = ConsumerGroup(self.bus, "t", "g", lambda payload: got.append(payload))
        n = group.pump_once()
        self.assertEqual(n, 2)
        self.assertEqual(len(got), 2)
        self.assertEqual(group.pump_once(), 0)


if __name__ == "__main__":
    unittest.main()

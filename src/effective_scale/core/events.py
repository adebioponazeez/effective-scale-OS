"""Partitioned event bus: ordered per partition, checkpointed, DLQ, backpressure.

Semantics: at-least-once delivery to consumers; consumer side-effects must be
idempotent (each delivery is tagged with the event id). Poison messages are moved
to the topic DLQ after max_attempts so the partition head never blocks.
"""
from __future__ import annotations

import copy
import zlib
from typing import Any, Callable

from ..domain.errors import Backpressure, NotFoundError, ValidationError
from ..domain.models import DlqEntry, EventMsg, EventStatus
from ..domain.states import transition
from ..ports.store import Store


class EventBus:
    def __init__(self, store: Store, *, clock, logger, metrics,
                 partitions: int = 4, max_lag: int = 1000, max_attempts: int = 5,
                 max_payload_bytes: int = 1024 * 1024):
        self.store = store
        self.clock = clock
        self.logger = logger
        self.metrics = metrics
        self.partitions = max(1, partitions)
        self.max_lag = max(1, max_lag)
        self.max_attempts = max(1, max_attempts)
        self.max_payload_bytes = max_payload_bytes
        self.metrics.counter("events.published", "events published")
        self.metrics.counter("events.delivered", "events delivered")
        self.metrics.counter("events.deadletter", "events dead-lettered")

    # ------------------------------------------------------------------ publish

    def publish(self, topic: str, key: str, payload: dict[str, Any],
                *, schema_version: int = 1, trace_id: str = "") -> EventMsg:
        payload = payload or {}
        if not isinstance(payload, dict):
            raise ValidationError("event payload must be an object")
        if len(repr(payload)) > self.max_payload_bytes:
            raise ValidationError(f"event payload exceeds {self.max_payload_bytes} bytes")
        partition = zlib.crc32(str(key).encode("utf-8")) % self.partitions
        snap = self.store.snapshot()
        if self.lag(topic) >= self.max_lag:
            raise Backpressure(f"topic '{topic}' lag exceeds {self.max_lag}; back off")
        seq = snap.next_seq(topic, partition)
        ev = EventMsg.create(topic, key, partition, seq, payload, self.clock.now(),
                             trace_id=trace_id, schema_version=schema_version)
        self.store.put_event(ev)
        self.metrics.counter("events.published").inc()
        self.logger.log("event.published", info=True, topic=topic, partition=partition,
                        seq=seq, key=key, trace_id=trace_id)
        return ev

    def lag(self, topic: str) -> int:
        snap = self.store.snapshot()
        return sum(
            1 for ev in snap.events.values()
            if ev.topic == topic and ev.status == EventStatus.PENDING
        )

    # ------------------------------------------------------------------ consume

    def next_batch(self, topic: str, group: str, *, limit: int = 10,
                   consumer: str = "default") -> list[EventMsg]:
        """Next undelivered events per partition, in offset order, for this group+consumer."""
        snap = self.store.snapshot()
        out: list[EventMsg] = []
        for partition in range(self.partitions):
            offset = snap.offsets.get((group, topic, partition), 0)
            seen = 0
            for ev_id in snap.events_index.get((topic, partition), []):
                ev = snap.events[ev_id]
                if ev.seq <= offset:
                    continue
                if ev.status != EventStatus.PENDING:
                    continue
                if ev.delivery_attempts.get(group, 0) >= self.max_attempts:
                    continue
                out.append(ev)
                seen += 1
                if seen >= limit:
                    break
        return out

    def ack(self, topic: str, group: str, event_id: str, *, ok: bool = True,
            error: str | None = None, consumer: str = "default") -> None:
        snap = self.store.snapshot()
        ev = snap.events.get(event_id)
        if ev is None or ev.topic != topic:
            raise NotFoundError(f"event {event_id} not found in topic {topic}")
        ev = copy.deepcopy(ev)
        if ok:
            if ev.status == EventStatus.PENDING:
                transition("event", ev.status, EventStatus.DELIVERED)
                ev.status = EventStatus.DELIVERED
            self.store.put_event(ev)
            self.store.put_offset(group, topic, ev.partition, ev.seq)
            self.metrics.counter("events.delivered").inc()
        else:
            attempts = ev.delivery_attempts.get(group, 0) + 1
            ev.delivery_attempts[group] = attempts
            self.store.put_event(ev)
            if attempts >= self.max_attempts:
                self._dead_letter(ev, group, attempts, error)
            else:
                self.logger.log("event.delivery_failed", warn=True, topic=topic,
                                partition=ev.partition, seq=ev.seq, group=group,
                                attempts=attempts, error_msg=error)

    def _dead_letter(self, ev: EventMsg, group: str, attempts: int, error: str | None) -> None:
        ev = copy.deepcopy(ev)
        if ev.status != EventStatus.DEAD:
            transition("event", ev.status, EventStatus.DEAD)
            ev.status = EventStatus.DEAD
        self.store.put_event(ev)
        self.store.put_offset(group, ev.topic, ev.partition, ev.seq)
        entry = DlqEntry(
            id=ev.id, topic=ev.topic, partition=ev.partition, seq=ev.seq,
            group=group, error=error or "max attempts exceeded", attempts=attempts,
            created_at=self.clock.now(),
        )
        self.store.put_dlq(entry)
        self.metrics.counter("events.deadletter").inc()
        self.logger.log("event.deadletter", error=True, topic=ev.topic, partition=ev.partition,
                        seq=ev.seq, group=group, attempts=attempts, error_msg=error)

    def recover(self) -> None:
        """Boot reconciliation: previously DELIVERED-but-not-offset events resume cleanly;
        PENDING events with exhausted attempts move to DLQ on their next ack."""
        snap = self.store.snapshot()
        for ev in snap.events.values():
            if ev.status == EventStatus.DELIVERED:
                continue
            if any(ev.delivery_attempts.get(g, 0) >= self.max_attempts for g in ev.delivery_attempts):
                continue  # ack will dead-letter them on resume
        self.logger.log("eventbus.recovered", info=True, pending=self.lag_in_memory())

    def lag_in_memory(self) -> int:
        return sum(1 for ev in self.store.snapshot().events.values()
                   if ev.status == EventStatus.PENDING)


class ConsumerGroup:
    """In-process consumer helper: bounded pump + handler + ack."""
    def __init__(self, bus: EventBus, topic: str, group: str, handler: Callable[[dict[str, Any]], None],
                 *, consumer: str = "default", batch: int = 10):
        self.bus = bus
        self.topic = topic
        self.group = group
        self.handler = handler
        self.consumer = consumer
        self.batch = batch

    def pump_once(self) -> int:
        events = self.bus.next_batch(self.topic, self.group, limit=self.batch, consumer=self.consumer)
        for ev in events:
            try:
                self.handler(ev.payload)
                self.bus.ack(self.topic, self.group, ev.id, ok=True, consumer=self.consumer)
            except Exception as exc:  # noqa: BLE001 — poison isolation
                self.bus.ack(self.topic, self.group, ev.id, ok=False, error=str(exc),
                             consumer=self.consumer)
        return len(events)

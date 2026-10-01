"""Kernel: wires store, leader, scheduler, scaler, workflow engine, event bus,
cron, watchdog and a single-writer queue into one coherent process.

Concurrency model (ADR-005 + ADR-004):
  * ONE writer thread owns all mutations (serialized write queue).
  * Reader threads (API) only take snapshot references — atomic swap, no locks.
  * Leadership gates the scheduler/scaler/workflow loops.
  * Watchdog converts stalls into a supervised crash (fail fast beats hang).
"""
from __future__ import annotations

import copy
import queue
import threading
import time
import traceback
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Any, Callable

from .. import __version__
from ..adapters import MemoryStore
from ..domain.ids import new_id, new_nonce
from ..domain.models import Lease, Namespace, Token, Workload
from ..domain.states import LeaseState, NodeState, transition
from ..observability.registry import Registry
from ..ports.logger import JsonLogger, Logger
from ..ports.clock import SystemClock
from ..ports.random import RandomSource, SecureRandom
from ..ports.store import Snapshot, Store
from .cron import CronSchedule
from .events import ConsumerGroup, EventBus
from .leader import LeaderElection, SingleLeader
from .scaler import TargetScaler
from .scheduler import Grant, Plan, Release, Scheduler
from .security import TokenService
from .workflow import WorkflowEngine


@dataclass
class Config:
    store_path: str = "data/effective_scale.db"      # ":memory:" allowed
    listen: str = "0.0.0.0:8080"
    auth_secret: str = ""                            # >= 16 chars; required for API writes
    admin_token: str = ""                            # bootstrap token for /v1/tokens
    scheduler_interval: float = 1.0
    scale_interval: float = 1.0
    workflow_interval: float = 0.5
    event_interval: float = 0.5
    cron_interval: float = 1.0
    lease_ttl: float = 300.0
    heartbeat_ttl: float = 5.0
    heartbeat_interval: float = 1.0
    watchdog_stall: float = 30.0
    leader_holder: str = "kernel-1"
    max_api_conns: int = 256
    max_workflow_pool: int = 64
    rate_limit_per_minute: int = 6000
    event_partitions: int = 4
    event_max_lag: int = 1000
    event_max_attempts: int = 5
    attempt_result_max_bytes: int = 65536
    log_level: str = "info"


class Kernel:
    def __init__(self, store: Store | None = None, *, config: Config | None = None,
                 clock=None, rng: RandomSource | None = None, logger: Logger | None = None,
                 metrics: Registry | None = None, single_leader: bool = True):
        self.config = config or Config()
        if not self.config.auth_secret:
            # ephemeral dev default: surface loudly but keep the API usable
            self.config.auth_secret = "dev-insecure-secret-change-me!"
        self.store = store or MemoryStore()
        self.clock = clock or SystemClock()
        self.rng = rng or SecureRandom()
        self.logger = logger or JsonLogger(level=self.config.log_level)
        self.metrics = metrics or Registry()
        self.auth = TokenService(self.config.auth_secret)
        self.scheduler = Scheduler(lease_ttl=self.config.lease_ttl)
        self.scaler = TargetScaler(clock=self.clock)
        self.bus = EventBus(
            self.store, clock=self.clock, logger=self.logger, metrics=self.metrics,
            partitions=self.config.event_partitions, max_lag=self.config.event_max_lag,
            max_attempts=self.config.event_max_attempts,
        )
        self.engine = WorkflowEngine(
            self.store, clock=self.clock, rng=self.rng, logger=self.logger,
            metrics=self.metrics, event_bus=self.bus,
            default_lease_seconds=self.config.lease_ttl,
        )
        if single_leader:
            self.leader = SingleLeader(self.config.leader_holder)
        else:
            self.leader = LeaderElection(
                self.store, self.config.leader_holder, ttl=self.config.heartbeat_ttl,
                heartbeat=self.config.heartbeat_interval, clock=self.clock, logger=self.logger,
                on_progress=lambda: self._mark("leader"),
            )
        self._wq: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._writer_local = threading.local()
        self._threads: list[threading.Thread] = []
        self._last_progress: dict[str, float] = {}
        self._loop_errors: list[str] = []
        self._metric_input: dict[str, dict[str, float]] = {}
        self._cron_gates: dict[str, float] = {}
        self._cron_cache: dict[str, CronSchedule] = {}
        self._consumers: list[ConsumerGroup] = []
        # A loop is only "stalled" if it missed its own cadence, not the global
        # stall threshold: a 60 s cron loop would otherwise look dead at 30 s.
        self._loop_intervals = {
            "scheduler": self.config.scheduler_interval,
            "scaler": self.config.scale_interval,
            "workflow": self.config.workflow_interval,
            "events": self.config.event_interval,
            "cron": self.config.cron_interval,
            "leader": self.config.heartbeat_interval,
        }

        for name in ("api", "scheduler", "scaler", "workflow", "events", "cron", "leader", "watchdog"):
            self.metrics.gauge(f"loop.{name}.last_ts", f"last progress of {name} loop")

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        self.store.open()
        self.logger.log("kernel.start", info=True, store=type(self.store).__name__,
                        holder=self.config.leader_holder, version_=__version__)
        self._write(lambda: self.bus.recover())
        self._write(lambda: self.engine.recover())
        self._write(lambda: self.leader.acquire())
        self._start_thread("writer", self._writer_loop)
        self._start_thread("scheduler", self._scheduler_loop)
        self._start_thread("scaler", self._scaler_loop)
        self._start_thread("workflow", self._workflow_loop)
        self._start_thread("events", self._event_loop)
        self._start_thread("cron", self._cron_loop)
        self._start_thread("watchdog", self._watchdog_loop)
        if not isinstance(self.leader, SingleLeader):
            # A real leader loop heartbeats and reports progress via on_progress;
            # SingleLeader has no thread, so no "leader" stall can exist.
            self._mark("leader")
            self._start_thread("leader", self.leader.run)
        else:
            # SingleLeader is by definition always-leader: no loop to monitor.
            self._last_progress.pop("leader", None)

    def stop(self) -> None:
        self.logger.log("kernel.shutdown_begin", info=True)
        self._stop.set()
        deadline = time.monotonic() + 10
        for t in self._threads:
            t.join(timeout=max(0.1, deadline - time.monotonic()))
        try:
            self._wq.put((None, None))
            self._wq.task_done()
        except Exception:  # noqa: BLE001 — best-effort stop
            pass
        if not isinstance(self.leader, SingleLeader):
            self.leader.stop()
        self.store.close()
        self.logger.log("kernel.shutdown_end", info=True)

    # ------------------------------------------------------------------ write path

    def write(self, fn: Callable[[], Any], timeout: float = 10.0) -> Any:
        """Serialize a mutation through the single writer. Used by API handlers.

        Reentrant-safe: if we are already the writer thread (a handler wrapped its
        own write() call), run directly — otherwise submitting to our own queue
        would deadlock.
        """
        if getattr(self._writer_local, "active", False):
            return fn()
        future: Future = Future()
        self._wq.put((fn, future))
        return future.result(timeout=timeout)

    def _writer_loop(self) -> None:
        self._writer_local.active = True
        while not self._stop.is_set():
            try:
                item = self._wq.get(timeout=0.2)
            except queue.Empty:
                continue
            if item is None or item[0] is None:
                self._wq.task_done()
                break
            fn, future = item
            try:
                future.set_result(fn())
            except Exception as exc:  # noqa: BLE001 — writer never dies
                future.set_exception(exc)
                self.logger.log("kernel.write_error", error=True, error_msg=str(exc))
            finally:
                self._wq.task_done()
                self._mark("writer")
        self._mark("writer")

    def _write(self, fn: Callable[[], Any]) -> Any:
        """Direct helper for loops already running inside the writer thread."""
        return fn()

    # ------------------------------------------------------------------ loops

    def _start_thread(self, name: str, target: Callable[[], None]) -> None:
        t = threading.Thread(target=self._loop_guard(name, target), name=f"es-{name}", daemon=True)
        t.start()
        self._threads.append(t)

    def _loop_guard(self, name: str, target: Callable[[], None]) -> Callable[[], None]:
        def run() -> None:
            try:
                target()
            except Exception:  # noqa: BLE001 — report, fail fast via watchdog
                self._loop_errors.append(name)
                self.logger.log("kernel.loop_crashed", fatal=True, loop=name,
                                detail=traceback.format_exc())
        return run

    def _mark(self, name: str, extra: dict | None = None) -> None:
        ts = self.clock.now()
        self._last_progress[name] = ts
        self.metrics.gauge(f"loop.{name}.last_ts").set(ts)

    def _scheduler_loop(self) -> None:
        while not self._stop.is_set():
            try:
                now = self.clock.now()
                if self.leader.is_leader():
                    snap = self.store.snapshot()
                    plan = self.scheduler.compute(snap, now)
                    if plan.grants or plan.releases:
                        self.write(lambda: self._apply_plan(plan, now), timeout=10)
                    if any(l.state == LeaseState.ACTIVE and l.expires_at <= now
                           for l in self.store.snapshot().leases.values()):
                        self.write(lambda: self._reap_expired_leases(now), timeout=10)
                self._mark("scheduler")
            finally:
                self.clock.sleep(self.config.scheduler_interval)

    def _apply_plan(self, plan: Plan, now: float) -> None:
        snap = self.store.snapshot()
        for grant in plan.grants:
            wl = snap.workload(grant.workload_id)
            node = snap.node(grant.node_id)
            if wl is None or node is None:
                continue
            wl = copy.deepcopy(wl)
            node = copy.deepcopy(node)
            lease = Lease(
                id=new_id(), workload_id=wl.id, namespace=wl.namespace, node_id=node.id,
                holder="kernel", nonce=new_nonce(), expires_at=now + grant.ttl,
                state=LeaseState.ACTIVE, attempt=0, created_at=now,
            )
            node.used_cpu += wl.cpu
            node.used_mem += wl.memory
            wl.replicas += 1
            wl.updated_at = now
            self.store.put_lease(lease)
            self.store.put_node(node)
            self.store.put_workload(wl)
            self._audit("system", "lease.grant", wl.id, "ok", {"node": node.id, "lease": lease.id})
            self.metrics.counter("scheduler.leases.granted").inc()
        for release in plan.releases:
            lease = snap.lease(release.lease_id)
            if lease is None or lease.state != LeaseState.ACTIVE:
                continue
            wl = snap.workload(lease.workload_id)
            node = snap.node(lease.node_id)
            if wl is not None:
                wl = copy.deepcopy(wl)
                wl.replicas = max(0, wl.replicas - 1)
                wl.updated_at = now
                self.store.put_workload(wl)
            if node is not None:
                node = copy.deepcopy(node)
                node.used_cpu = max(0, node.used_cpu - wl.cpu) if wl else node.used_cpu
                node.used_mem = max(0, node.used_mem - wl.memory) if wl else node.used_mem
                node.last_heartbeat = now
                self.store.put_node(node)
            self.store.delete_lease(lease.id)
            self._audit("system", "lease.release", lease.id, "ok", {"workload": lease.workload_id})
            self.metrics.counter("scheduler.leases.released").inc()

    def _reap_expired_leases(self, now: float) -> None:
        """Reclaim slots whose TTL passed: bounded state, honest capacity.

        A lease is a slot, not an execution. When its TTL lapses (no heartbeat
        renewed it) the slot is returned to the pool and the node/workload
        accounting is freed. Attempts on the dead slot are failed by the
        workflow sweeper with `lease_expired`, so retry policy still applies.
        """
        snap = self.store.snapshot()
        for lease in list(snap.leases.values()):
            if lease.state != LeaseState.ACTIVE or lease.expires_at > now:
                continue
            wl = snap.workload(lease.workload_id)
            node = snap.node(lease.node_id)
            if wl is not None:
                wl = copy.deepcopy(wl)
                wl.replicas = max(0, wl.replicas - 1)
                wl.updated_at = now
                self.store.put_workload(wl)
            if node is not None:
                node = copy.deepcopy(node)
                node.used_cpu = max(0, node.used_cpu - (wl.cpu if wl else 0))
                node.used_mem = max(0, node.used_mem - (wl.memory if wl else 0))
                node.last_heartbeat = now
                self.store.put_node(node)
            lease = copy.deepcopy(lease)
            transition("lease", lease.state, LeaseState.EXPIRED)
            lease.state = LeaseState.EXPIRED
            self.store.put_lease(lease)
            self.store.delete_lease(lease.id)
            self._audit("system", "lease.expire", lease.id, "ok",
                        {"workload": lease.workload_id, "node": lease.node_id})
            self.metrics.counter("scheduler.leases.expired").inc()

    def _scaler_loop(self) -> None:
        while not self._stop.is_set():
            try:
                now = self.clock.now()
                if self.leader.is_leader():
                    for wl in list(self.store.snapshot().workloads.values()):
                        if wl.status.value != "active" or not wl.scaler:
                            continue
                        measured = self._metric_input.get(wl.id, {})
                        before = wl.desired_replicas
                        desired = self.scaler.decide(wl, measured, now)
                        if desired is not None and desired != before:
                            self.write(lambda: self._apply_scale(wl.id, desired, now), timeout=10)
                            self.scaler.record(wl.id, now, desired, before)
                self._mark("scaler")
            finally:
                self.clock.sleep(self.config.scale_interval)

    def _apply_scale(self, workload_id: str, desired: int, now: float) -> None:
        wl = self.store.snapshot().workload(workload_id)
        if wl is None:
            return
        wl = copy.deepcopy(wl)
        previous = wl.desired_replicas
        wl.desired_replicas = desired
        wl.updated_at = now
        self.store.put_workload(wl)
        self._audit("scaler", "workload.scale", workload_id, "ok",
                    {"from": previous, "to": desired})
        self.metrics.counter("scaler.decisions").inc()
        self.metrics.gauge("scaler.desired.replicas").set(desired)

    def ingest_metrics(self, workload_id: str, metrics: dict[str, float]) -> None:
        """Workers post resource metrics; scaler reads the latest sample."""
        self._metric_input[workload_id] = {str(k): float(v) for k, v in metrics.items()}
        for k, v in self._metric_input[workload_id].items():
            self.metrics.gauge(f"metric.{workload_id}.{k}").set(v)

    def _workflow_loop(self) -> None:
        while not self._stop.is_set():
            try:
                now = self.clock.now()
                if self.leader.is_leader():
                    self.write(lambda: self.engine.tick(now), timeout=10)
                self._mark("workflow")
            finally:
                self.clock.sleep(self.config.workflow_interval)

    def _event_loop(self) -> None:
        while not self._stop.is_set():
            try:
                total = 0
                for consumer in self._consumers:
                    total += consumer.pump_once()
                self.metrics.gauge("eventbus.pending").set(self.bus.lag_in_memory())
            finally:
                # Progress must be marked on every iteration, not only when
                # events were pumped: an idle-but-alive bus (normal production
                # state) would otherwise trip the watchdog and kill the kernel.
                self._mark("events")
                self.clock.sleep(self.config.event_interval)

    def _cron_loop(self) -> None:
        while not self._stop.is_set():
            try:
                now = self.clock.now()
                if self.leader.is_leader():
                    for wf in list(self.store.snapshot().workflows.values()):
                        if not wf.schedule:
                            continue
                        sched = self._cron_cache.setdefault(wf.id, CronSchedule(wf.schedule))
                        nxt = self._cron_gates.get(wf.id)
                        if nxt is None:
                            nxt = sched.next_after(_from_ts(now)).timestamp()
                            self._cron_gates[wf.id] = nxt
                        if now >= nxt:
                            last = float(self.store.get_meta(f"cron_last.{wf.id}", 0.0))
                            if now - last < 60.0:
                                self._cron_gates[wf.id] = nxt + 60.0
                                continue
                            self.write(lambda: self._fire_cron(wf.id, now), timeout=10)
                            self._cron_gates[wf.id] = sched.next_after(_from_ts(now + 1)).timestamp()
                self._mark("cron")
            finally:
                self.clock.sleep(self.config.cron_interval)

    def _fire_cron(self, template_id: str, now: float) -> None:
        self.store.put_meta(f"cron_last.{template_id}", now)
        run = self.engine.run_clone(template_id, trace_id=f"cron:{template_id}")
        self._audit("cron", "workflow.schedule_fire", template_id, "ok", {"run": run.id})
        self.metrics.counter("cron.fired").inc()

    def _watchdog_loop(self) -> None:
        while not self._stop.is_set():
            try:
                now = self.clock.now()
                for name, ts in list(self._last_progress.items()):
                    if name in ("writer", "watchdog"):
                        continue
                    # Stall threshold = max(global stall, 3× the loop's own
                    # interval): a legitimately slow loop (e.g. cron=60 s) must
                    # not be declared stalled by a shorter global threshold.
                    interval = self._loop_intervals.get(name, 0.0)
                    limit = max(self.config.watchdog_stall, 3.0 * interval)
                    if now - ts > limit:
                        self.logger.log("kernel.watchdog.stall", fatal=True,
                                        loop=name, stalled_seconds=now - ts)
                        # fail fast: supervisor restart (k8s/systemd) is the recovery path
                        import os
                        os._exit(1)
                self._mark("watchdog")
            finally:
                self._stop.wait(min(1.0, self.config.watchdog_stall / 6))

    # ------------------------------------------------------------------ API-facing ops

    def create_namespace(self, name: str, actor: str = "api") -> Namespace:
        def op() -> Namespace:
            snap = self.store.snapshot()
            if name in snap.namespaces:
                return snap.namespaces[name]
            ns = Namespace.create(name, self.clock.now())
            self.store.put_namespace(ns)
            self._audit(actor, "namespace.create", name, "ok", {})
            return ns
        return self.write(op)

    def issue_token(self, namespace: str, scopes: list[str], actor: str = "admin",
                    ttl: float | None = None) -> tuple[Token, str]:
        def op() -> tuple[Token, str]:
            token_id = new_id()
            raw = self.auth.issue(token_id, namespace, scopes, ttl=ttl)
            token = Token.create(namespace, scopes, self.auth.store_hash(raw), self.clock.now(), ttl=ttl)
            token = Token(id=token_id, namespace=token.namespace, scopes=token.scopes,
                          secret_hash=token.secret_hash, created_at=token.created_at,
                          expires_at=token.expires_at)
            self.store.put_token(token)
            self._audit(actor, "token.issue", namespace, "ok", {"token_id": token_id, "scopes": scopes})
            return token, raw
        return self.write(op)

    def create_workload(self, namespace: str, data: dict[str, Any], actor: str = "api") -> Workload:
        def op() -> Workload:
            wl = Workload.create(namespace, data, self.clock.now())
            self.store.put_workload(wl)
            self._audit(actor, "workload.create", wl.id, "ok", {"name": wl.name, "desired": wl.desired_replicas})
            return wl
        return self.write(op)

    def scale_workload(self, workload_id: str, desired: int, actor: str = "api") -> Workload:
        def op() -> Workload:
            wl = self.store.snapshot().workload(workload_id)
            if wl is None:
                from ..domain.errors import NotFoundError
                raise NotFoundError(f"workload {workload_id} not found")
            if not (wl.min_replicas <= desired <= wl.max_replicas):
                from ..domain.errors import ValidationError
                raise ValidationError(f"desired out of range [{wl.min_replicas},{wl.max_replicas}]")
            self._apply_scale(workload_id, desired, self.clock.now())
            return self.store.snapshot().workload(workload_id)
        return self.write(op)

    def _audit(self, actor: str, action: str, resource: str, outcome: str,
               detail: dict | None = None, trace_id: str = "") -> None:
        self.store.put_audit({
            "ts": self.clock.now(), "actor": actor, "action": action,
            "resource": resource, "outcome": outcome, "detail": detail or {},
            "trace_id": trace_id,
        })

    def attach_consumer(self, topic: str, group: str, handler: Callable[[dict[str, Any]], None],
                        *, consumer: str = "default") -> None:
        self._consumers.append(ConsumerGroup(self.bus, topic, group, handler, consumer=consumer))

    def consume_now(self, topic: str, group: str, handler: Callable[[dict[str, Any]], None],
                    *, consumer: str = "default") -> int:
        return ConsumerGroup(self.bus, topic, group, handler, consumer=consumer).pump_once()

def _from_ts(ts: float):
    import datetime as dt

    return dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).replace(tzinfo=None)

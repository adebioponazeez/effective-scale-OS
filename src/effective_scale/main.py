"""Entry point: `python3 -m effective_scale` runs the kernel + API in-process."""
from __future__ import annotations

import argparse
import signal
import sys
import threading

from .adapters import MemoryStore, SQLiteStore
from .api.server import launch
from .core.kernel import Config, Kernel


def build_config(args: argparse.Namespace) -> Config:
    return Config(
        store_path=args.store,
        listen=args.listen,
        auth_secret=args.secret or "dev-insecure-secret-change-me!",
        admin_token=args.admin_token or "",
        scheduler_interval=args.scheduler_interval,
        scale_interval=args.scale_interval,
        workflow_interval=args.workflow_interval,
        event_interval=args.event_interval,
        lease_ttl=args.lease_ttl,
        heartbeat_ttl=args.heartbeat_ttl,
        watchdog_stall=args.watchdog_stall,
        leader_holder=args.holder,
        max_api_conns=args.max_conns,
        max_workflow_pool=args.workflow_pool,
        rate_limit_per_minute=args.rate_limit,
        event_partitions=args.event_partitions,
        event_max_lag=args.event_max_lag,
        event_max_attempts=args.event_max_attempts,
        log_level=args.log_level,
    )


def seed_demo(kernel: Kernel) -> None:
    """Idempotent demo data: one namespace, three nodes, one workload, one workflow."""
    from .domain.models import Node, Workload

    ns = kernel.create_namespace("demo", actor="bootstrap-admin")
    kernel._write(lambda: kernel.store.put_namespace(ns))
    kernel.issue_token("demo", ["read", "write", "admin"], ttl=86400 * 365, actor="bootstrap-admin")
    for name, cpu, mem in (("node-a", 2000, 4096), ("node-b", 2000, 4096), ("node-c", 4000, 8192)):
        node = Node.create({"name": name, "cpu": cpu, "memory": mem, "tags": ["prod"]}, kernel.clock.now())
        kernel._write(lambda n=node: kernel.store.put_node(n))

    wl = Workload.create("demo", {
        "name": "payment-api", "image": "ghcr.io/acme/payment-api:v1",
        "replicas": 1, "min_replicas": 1, "max_replicas": 6, "cpu": 250, "memory": 512,
        "policy": "bin_pack", "node_tags": ["prod"], "priority": 10,
        "scaler": {"cooldown_seconds": 30, "metrics": {"cpu": {"target": 0.65}}},
    }, kernel.clock.now())
    kernel._write(lambda: kernel.store.put_workload(wl))
    kernel.engine.submit("demo", {
        "name": "nightly-etl",
        "nodes": [
            {"id": "extract", "exec": {"workload_id": wl.id}, "timeout": 120},
            {"id": "transform", "depends_on": ["extract"], "event_topic": "etl.ready", "timeout": 60},
            {"id": "load", "depends_on": ["transform"], "exec": {"workload_id": wl.id},
             "retry": {"max": 2}, "timeout": 60},
        ],
        "timeout": 300,
    })
    kernel.logger.log("demo.seeded", info=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="effective_scale",
        description="effective-scale-OS: workloads, workflows, events, scaling.",
    )
    parser.add_argument("--store", default="data/effective_scale.db",
                        help="SQLite path (':memory:' for ephemeral)")
    parser.add_argument("--listen", default="0.0.0.0:8080")
    parser.add_argument("--secret", default="", help="HMAC auth secret (>=16 chars)")
    parser.add_argument("--admin-token", default="", help="bootstrap admin header token")
    parser.add_argument("--scheduler-interval", type=float, default=1.0)
    parser.add_argument("--scale-interval", type=float, default=1.0)
    parser.add_argument("--workflow-interval", type=float, default=0.5)
    parser.add_argument("--event-interval", type=float, default=0.5)
    parser.add_argument("--lease-ttl", type=float, default=300.0)
    parser.add_argument("--heartbeat-ttl", type=float, default=5.0)
    parser.add_argument("--watchdog-stall", type=float, default=30.0)
    parser.add_argument("--holder", default="kernel-1")
    parser.add_argument("--max-conns", type=int, default=256)
    parser.add_argument("--workflow-pool", type=int, default=64)
    parser.add_argument("--rate-limit", type=int, default=6000)
    parser.add_argument("--event-partitions", type=int, default=4)
    parser.add_argument("--event-max-lag", type=int, default=1000)
    parser.add_argument("--event-max-attempts", type=int, default=5)
    parser.add_argument("--log-level", default="info", choices=["debug", "info", "warn", "error"])
    parser.add_argument("--demo", action="store_true", help="seed demo data on start")
    parser.add_argument("--check", action="store_true", help="validate config and exit")
    args = parser.parse_args(argv)

    config = build_config(args)
    store = MemoryStore() if config.store_path == ":memory:" else SQLiteStore(config.store_path)
    kernel = Kernel(store, config=config)

    if args.check:
        print("config OK:", config.listen, "store:", config.store_path)
        return 0

    kernel.start()
    if args.demo:
        seed_demo(kernel)

    server = launch(kernel)
    stopped = threading.Event()

    def shutdown(signum, frame):  # noqa: ARG001
        if stopped.is_set():
            return
        stopped.set()
        server.stop()
        kernel.stop()
        print("shutdown complete")

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    try:
        while not stopped.is_set():
            stopped.wait(1.0)
    except KeyboardInterrupt:
        shutdown(None, None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
